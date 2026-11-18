# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""Bulk import of Obsidian vaults, Markdown folders, and text trees into owner Notes.

* Idempotent: a manifest maps each source file to its note and content hash.
  Re-running imports only new or changed files; unchanged files are skipped.
* Batch-indexed: files are written first, then indexed once with an incremental
  sync, so 5,000+ notes import in seconds rather than one transaction per file.
* Non-destructive: source files are only read. Notes are created with the
  ``owner_import`` origin and remain *unverified* notes until the owner verifies excerpts.

Run:
    uv run --locked python scripts/bulk_import.py "C:/path/to/ObsidianVault"
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import atomic_write_bytes, review_lock  # noqa: E402
from config import _is_reparse_or_link, safe_file_in_dir  # noqa: E402
from lexical_store import sync_lexical_index  # noqa: E402
from notes_store import (  # noqa: E402
    DEFAULT_PATHS,
    MAX_NOTE_BYTES,
    NotePaths,
    _O_BINARY,
    _clean_tags,
    parse_note,
    render_note,
    slugify,
)

SKIP_DIRS = {
    ".obsidian",
    ".git",
    ".trash",
    "node_modules",
    ".venv",
    "__pycache__",
    ".sidecar",
    ".chroma",
    ".history",
    ".ssh",
    ".gnupg",
    ".aws",
    ".kube",
}
from doc_extractors import (
    DOC_EXTRACTOR_EXTS,
    DocumentExtractionError,
    MAX_DOC_BINARY_BYTES,
    extract_document,
)

IMPORT_EXTS = {".md", ".markdown", ".txt", *DOC_EXTRACTOR_EXTS}
MANIFEST_NAME = "notes-import-manifest.json"


@dataclass
class ImportStats:
    scanned: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_too_large: int = 0
    skipped_unreadable: int = 0
    conflicts_preserved: int = 0
    dry_run: bool = False
    notes_dir: str = ""
    elapsed_s: float = 0.0
    index: dict | None = None

    def as_dict(self) -> dict:
        out = dict(self.__dict__)
        out["imported"] = self.created
        out["skipped"] = self.skipped_too_large + self.skipped_unreadable
        return out


def _manifest_path(paths: NotePaths) -> Path:
    return Path(paths.brain_dir) / ".sidecar" / MANIFEST_NAME


def _load_manifest(paths: NotePaths) -> dict[str, dict[str, str]]:
    p = _manifest_path(paths)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def iter_source_files(root: Path):
    root_abs = os.path.abspath(str(root))
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in SKIP_DIRS
            and not d.startswith(".")
            and not _is_reparse_or_link(os.path.join(dirpath, d))
        )
        for name in sorted(filenames):
            if Path(name).suffix.lower() in IMPORT_EXTS and not name.startswith("."):
                path = Path(dirpath) / name
                if path.is_symlink() or _is_reparse_or_link(str(path)):
                    continue
                try:
                    if os.path.commonpath([root_abs, os.path.abspath(os.path.realpath(str(path)))]) != root_abs:
                        continue
                except ValueError:
                    continue
                yield path


def _note_name_for(rel: Path, used: set[str], notes_dir: Path) -> str:
    parts = [slugify(p, max_len=40) for p in rel.with_suffix("").parts]
    base = "--".join(parts)[:120].strip("-") or "note"
    name = f"{base}.md"
    n = 2
    while (
        name in used
        or safe_file_in_dir(str(notes_dir), name, allowed_exts={".md"}, must_exist=False) is None
        or (notes_dir / name).exists()
    ):
        name = f"{base}-{n}.md"
        n += 1
    return name


def import_folder(
    source_root: str | Path,
    *,
    paths: NotePaths = DEFAULT_PATHS,
    tags: tuple[str, ...] = (),
    dry_run: bool = False,
    overwrite_modified: bool = False,
    progress=None,
) -> ImportStats:
    if _is_reparse_or_link(str(Path(source_root).expanduser())) or _is_reparse_or_link(str(paths.notes_dir)):
        raise PermissionError("Refusing to import from or into a symlink/junction directory.")
    root = Path(source_root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Import source is not a folder: {root}")
    notes_dir = Path(paths.notes_dir).resolve()
    brain = Path(paths.brain_dir).resolve()
    if root == brain or brain in root.parents or root in brain.parents:
        raise ValueError("Refusing to import the Second Brain vault into itself (or a folder containing it).")
    notes_dir.mkdir(parents=True, exist_ok=True)
    if _is_reparse_or_link(str(notes_dir)):
        raise PermissionError("Refusing to import into a symlink/junction notes directory.")
    stats = ImportStats(dry_run=bool(dry_run), notes_dir=str(notes_dir))
    started = time.perf_counter()
    root_tag = f"import:{slugify(root.name, max_len=40)}"
    all_tags = tuple(dict.fromkeys((root_tag, *tags)))

    with review_lock(paths.lock_path):
        manifest = _load_manifest(paths)
        used: set[str] = set()
        for src in iter_source_files(root):
            stats.scanned += 1
            rel = src.relative_to(root)
            rel_key = f"{root.name}/{rel.as_posix()}"
            legacy_key = str(src)
            ext = src.suffix.lower()
            try:
                raw = src.read_bytes()
            except OSError:
                stats.skipped_unreadable += 1
                continue
            max_raw = MAX_DOC_BINARY_BYTES if ext in DOC_EXTRACTOR_EXTS else MAX_NOTE_BYTES
            if len(raw) > max_raw:
                stats.skipped_too_large += 1
                continue
            src_sha = hashlib.sha256(raw).hexdigest()
            entry = manifest.get(rel_key) or manifest.get(legacy_key)
            existing_note_path = (
                safe_file_in_dir(str(notes_dir), str(entry.get("note", "")), allowed_exts={".md"}, must_exist=True)
                if isinstance(entry, dict) and isinstance(entry.get("note"), str)
                else None
            )
            if entry and entry.get("source_sha256") == src_sha and existing_note_path:
                note_basename = os.path.basename(existing_note_path)
                stats.unchanged += 1
                used.add(note_basename)
                if rel_key not in manifest and legacy_key in manifest:
                    manifest[rel_key] = manifest.pop(legacy_key)
                continue
            if ext in DOC_EXTRACTOR_EXTS:
                try:
                    extracted = extract_document(src, max_bytes=MAX_DOC_BINARY_BYTES)
                except (DocumentExtractionError, ValueError, OSError):
                    stats.skipped_unreadable += 1
                    continue
                doc_body = extracted.markdown.strip()
                if not doc_body:
                    stats.skipped_unreadable += 1
                    continue
                if len(doc_body.encode("utf-8")) > MAX_NOTE_BYTES:
                    doc_body = doc_body.encode("utf-8")[: MAX_NOTE_BYTES - 512].decode("utf-8", errors="ignore").rstrip()
                meta = {"title": extracted.title or src.stem, "tags": [f"format:{ext.lstrip('.')}"], "created": ""}
                body = doc_body
            else:
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    stats.skipped_unreadable += 1
                    continue
                meta, body = parse_note(text, src.name)
            title = str(meta.get("title") or src.stem)
            src_tags = tuple(str(t) for t in meta.get("tags", []) or [])
            mtime = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(src.stat().st_mtime))
            rendered = render_note(
                title=title,
                body=body,
                origin="owner_import",
                tags=_clean_tags((*all_tags, *src_tags)),
                created=str(meta.get("created") or mtime),
                updated=mtime,
                source_path=rel.as_posix(),
            ).encode("utf-8")
            rendered_sha = hashlib.sha256(rendered).hexdigest()
            if existing_note_path:
                if _is_reparse_or_link(existing_note_path):
                    raise PermissionError(
                        f"Refusing to overwrite symlinked note file: {os.path.basename(existing_note_path)}"
                    )
                recorded_note_sha = str((entry or {}).get("note_sha256") or "")
                current_note_bytes = Path(existing_note_path).read_bytes()
                current_note_sha = hashlib.sha256(current_note_bytes).hexdigest()
                if recorded_note_sha and current_note_sha != recorded_note_sha and not overwrite_modified:
                    stats.conflicts_preserved += 1
                    used.add(os.path.basename(existing_note_path))
                    continue
                if dry_run:
                    stats.updated += 1
                    continue
                if recorded_note_sha and current_note_sha != recorded_note_sha:
                    hist_dir = notes_dir / ".history"
                    hist_dir.mkdir(exist_ok=True)
                    if not _is_reparse_or_link(str(hist_dir)):
                        bak = hist_dir / f"{os.path.basename(existing_note_path)}.{current_note_sha[:16]}.bak"
                        if not bak.exists():
                            atomic_write_bytes(str(bak), current_note_bytes)
                atomic_write_bytes(existing_note_path, rendered)
                stats.updated += 1
                note_name = os.path.basename(existing_note_path)
            else:
                if dry_run:
                    stats.created += 1
                    continue
                note_name = _note_name_for(rel, used, notes_dir)
                fd = os.open(notes_dir / note_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
                try:
                    if os.write(fd, rendered) != len(rendered):
                        raise OSError("Incomplete imported note write.")
                    os.fsync(fd)
                finally:
                    os.close(fd)
                stats.created += 1
            used.add(note_name)
            manifest.pop(legacy_key, None)
            manifest[rel_key] = {"note": note_name, "source_sha256": src_sha, "note_sha256": rendered_sha}
            if progress and stats.scanned % 250 == 0:
                progress(stats)
        if not dry_run:
            mp = _manifest_path(paths)
            mp.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(str(mp), json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8"))
            stats.index = sync_lexical_index(paths.notes_dir, paths.index_path)
    stats.elapsed_s = round(time.perf_counter() - started, 2)
    return stats


def import_file(
    source_file: str | Path,
    *,
    paths: NotePaths = DEFAULT_PATHS,
    tags: tuple[str, ...] = (),
) -> dict:
    """Import a single local Markdown/text/PDF/DOCX/EPUB/HTML/CSV document into working notes."""
    from notes_store import create_note

    if _is_reparse_or_link(str(Path(source_file).expanduser())) or _is_reparse_or_link(str(paths.notes_dir)):
        raise PermissionError("Refusing to import from or into a symlink/junction path.")
    src = Path(source_file).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Document not found: {src}")
    ext = src.suffix.lower()
    if ext not in IMPORT_EXTS:
        raise ValueError(f"Unsupported import extension '{ext}'; expected one of {sorted(IMPORT_EXTS)}.")
    if ext in DOC_EXTRACTOR_EXTS:
        extracted = extract_document(src, max_bytes=MAX_DOC_BINARY_BYTES)
        body = extracted.markdown.strip()
        if not body:
            raise ValueError(f"Document contained no extractable text: {src.name}")
        if len(body.encode("utf-8")) > MAX_NOTE_BYTES:
            body = body.encode("utf-8")[: MAX_NOTE_BYTES - 512].decode("utf-8", errors="ignore").rstrip()
        title = extracted.title or src.stem
        merged_tags = (*tags, f"format:{ext.lstrip('.')}")
    else:
        raw = src.read_bytes()
        if len(raw) > MAX_NOTE_BYTES:
            raise ValueError(f"Document exceeds maximum note size ({MAX_NOTE_BYTES} bytes).")
        meta, body = parse_note(raw.decode("utf-8-sig"), src.name)
        title = str(meta.get("title") or src.stem)
        merged_tags = (*tags, *(str(t) for t in (meta.get("tags") or [])))
    created = create_note(
        title=title,
        body=body,
        tags=list(dict.fromkeys(t for t in merged_tags if t)),
        origin="owner_import",
        paths=paths,
    )
    return {**created, "source_file": src.name, "format": ext.lstrip(".")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="Folder to import (Obsidian vault, Markdown/PDF/DOCX/HTML docs, notes).")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--overwrite-modified",
        action="store_true",
        help="Overwrite locally edited imported notes after saving a .history/ backup.",
    )
    args = parser.parse_args(argv)
    stats = import_folder(
        args.source,
        tags=tuple(args.tag),
        dry_run=args.dry_run,
        overwrite_modified=args.overwrite_modified,
        progress=lambda s: print(f"  … {s.scanned} scanned", file=sys.stderr),
    )
    print(json.dumps(stats.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
