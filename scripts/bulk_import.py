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
from lexical_store import sync_lexical_index  # noqa: E402
from notes_store import (  # noqa: E402
    DEFAULT_PATHS,
    MAX_NOTE_BYTES,
    NotePaths,
    _O_BINARY,
    parse_note,
    render_note,
    slugify,
)

SKIP_DIRS = {".obsidian", ".git", ".trash", "node_modules", ".venv", "__pycache__", ".sidecar", ".chroma", ".history"}
IMPORT_EXTS = {".md", ".markdown", ".txt"}
MANIFEST_NAME = "notes-import-manifest.json"


@dataclass
class ImportStats:
    scanned: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_too_large: int = 0
    skipped_unreadable: int = 0
    elapsed_s: float = 0.0
    index: dict | None = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


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
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if Path(name).suffix.lower() in IMPORT_EXTS and not name.startswith("."):
                path = Path(dirpath) / name
                if not path.is_symlink():
                    yield path


def _note_name_for(rel: Path, used: set[str], notes_dir: Path) -> str:
    parts = [slugify(p, max_len=40) for p in rel.with_suffix("").parts]
    base = "--".join(parts)[:120].strip("-") or "note"
    name = f"{base}.md"
    n = 2
    while name in used or (notes_dir / name).exists():
        name = f"{base}-{n}.md"
        n += 1
    return name


def import_folder(
    source_root: str | Path,
    *,
    paths: NotePaths = DEFAULT_PATHS,
    tags: tuple[str, ...] = (),
    dry_run: bool = False,
    progress=None,
) -> ImportStats:
    root = Path(source_root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Import source is not a folder: {root}")
    notes_dir = Path(paths.notes_dir).resolve()
    brain = Path(paths.brain_dir).resolve()
    if root == brain or brain in root.parents or root in brain.parents:
        raise ValueError("Refusing to import the Second Brain vault into itself (or a folder containing it).")
    notes_dir.mkdir(parents=True, exist_ok=True)
    stats = ImportStats()
    started = time.perf_counter()
    root_tag = f"import:{slugify(root.name, max_len=40)}"
    all_tags = tuple(dict.fromkeys((root_tag, *tags)))

    with review_lock(paths.lock_path):
        manifest = _load_manifest(paths)
        used: set[str] = set()
        for src in iter_source_files(root):
            stats.scanned += 1
            key = str(src)
            try:
                raw = src.read_bytes()
                text = raw.decode("utf-8-sig")
            except (OSError, UnicodeDecodeError):
                stats.skipped_unreadable += 1
                continue
            if len(raw) > MAX_NOTE_BYTES:
                stats.skipped_too_large += 1
                continue
            src_sha = hashlib.sha256(raw).hexdigest()
            entry = manifest.get(key)
            if entry and entry.get("source_sha256") == src_sha and (notes_dir / entry["note"]).exists():
                stats.unchanged += 1
                used.add(entry["note"])
                continue
            rel = src.relative_to(root)
            meta, body = parse_note(text, src.name)
            title = str(meta.get("title") or src.stem)
            src_tags = tuple(str(t) for t in meta.get("tags", []) or [])
            mtime = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(src.stat().st_mtime))
            rendered = render_note(
                title=title,
                body=body,
                origin="owner_import",
                tags=tuple(dict.fromkeys((*all_tags, *src_tags)))[:32],
                created=str(meta.get("created") or mtime),
                updated=mtime,
                source_path=rel.as_posix(),
            ).encode("utf-8")
            if dry_run:
                stats.created += 0 if entry else 1
                stats.updated += 1 if entry else 0
                continue
            if entry and (notes_dir / entry["note"]).exists():
                atomic_write_bytes(str(notes_dir / entry["note"]), rendered)
                stats.updated += 1
                note_name = entry["note"]
            else:
                note_name = _note_name_for(rel, used, notes_dir)
                fd = os.open(notes_dir / note_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
                try:
                    os.write(fd, rendered)
                finally:
                    os.close(fd)
                stats.created += 1
            used.add(note_name)
            manifest[key] = {"note": note_name, "source_sha256": src_sha}
            if progress and stats.scanned % 250 == 0:
                progress(stats)
        if not dry_run:
            mp = _manifest_path(paths)
            mp.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(str(mp), json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8"))
            stats.index = sync_lexical_index(paths.notes_dir, paths.index_path)
    stats.elapsed_s = round(time.perf_counter() - started, 2)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="Folder to import (Obsidian vault, Markdown docs, notes).")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    stats = import_folder(
        args.source,
        tags=tuple(args.tag),
        dry_run=args.dry_run,
        progress=lambda s: print(f"  … {s.scanned} scanned", file=sys.stderr),
    )
    print(json.dumps(stats.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
