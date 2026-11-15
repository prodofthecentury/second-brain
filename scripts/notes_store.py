# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""Zero-friction owner Notes layer with one-click verified-claim promotion.

Two memory tiers, kept structurally separate:

* **Notes** (``brain/notes/*.md``): the owner's knowledge base. Saving a note makes
  it searchable immediately (incremental FTS5 upsert); there is no review queue and
  no manual hash handling. Notes are always presented as *unverified* context and
  never rendered as verified factual claims.
* **Verified atoms** (``brain/2-atoms``): unchanged trust contract. A note excerpt
  becomes a verified atom only through ``verify_note_excerpt``. The program snapshots
  the note immutably, anchors the excerpt by byte offset and SHA-256, and records an
  owner approval. The owner clicks once and never handles a hash.

Provenance is set by the calling channel, never by note content. Agent captures
are risk-scanned, and anything suspicious (forged trust markers, prompt
overrides, beacons, credentials) is diverted to the quarantined inbox.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import (  # noqa: E402
    ReviewConflictError,
    ReviewTransitionError,
    apply_review_transition,
    atomic_write_bytes,
    content_sha256,
    review_lock,
)
from config import (  # noqa: E402
    ATOMS_DIR,
    BRAIN_DIR,
    INBOX_DIR,
    NOTES_DIR,
    NOTES_INDEX_PATH,
    NOTE_SNAPSHOTS_DIR,
    _WINDOWS_RESERVED_STEMS,
    _is_reparse_or_link,
    safe_file_in_dir,
)
from evidence import EvidenceValidationError, find_evidence_anchor  # noqa: E402
from inbox_workflow import scan_inbox_payload_risks  # noqa: E402
from lexical_store import (  # noqa: E402
    LexicalIndexUnavailable,
    lexical_index_is_current,
    list_indexed_atoms_fast,
    rebuild_lexical_index,
    search_lexical_index,
    sync_lexical_index,
    upsert_lexical_atoms,
)
from memory_format import split_frontmatter  # noqa: E402
from memory_review import parse_frontmatter_values, parse_scalar_list  # noqa: E402

NOTE_ORIGINS = frozenset({"owner_note", "owner_import", "agent_capture"})
AGENT_ORIGINS = frozenset({"agent_capture"})
MAX_NOTE_BYTES = 2_000_000
MAX_AGENT_CAPTURE_CHARS = 16_000
DISALLOWED_ACTORS = frozenset({"auto", "system", "llm", "model", "ollama", "assistant", "agent", ""})
_O_BINARY = getattr(os, "O_BINARY", 0)  # Windows: prevent CRLF translation that breaks hashes
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")


class NoteError(ValueError):
    """Raised when a note operation violates the notes contract."""


@dataclass(frozen=True)
class NotePaths:
    brain_dir: str = BRAIN_DIR
    notes_dir: str = NOTES_DIR
    index_path: str = NOTES_INDEX_PATH
    atoms_dir: str = ATOMS_DIR
    inbox_dir: str = INBOX_DIR
    snapshots_dir: str = NOTE_SNAPSHOTS_DIR

    @classmethod
    def for_brain(cls, brain_dir: str) -> "NotePaths":
        b = os.path.abspath(brain_dir)
        return cls(
            brain_dir=b,
            notes_dir=os.path.join(b, "notes"),
            index_path=os.path.join(b, ".sidecar", "notes.sqlite3"),
            atoms_dir=os.path.join(b, "2-atoms"),
            inbox_dir=os.path.join(b, "0-raw", "inbox"),
            snapshots_dir=os.path.join(b, "0-raw", "note-snapshots"),
        )

    @property
    def lock_path(self) -> str:
        return os.path.join(self.brain_dir, ".sidecar", "writer.lock")


DEFAULT_PATHS = NotePaths()


@dataclass(frozen=True)
class NoteRecord:
    filename: str
    title: str
    origin: str
    tags: tuple[str, ...]
    created: str
    updated: str
    sha256: str
    snippet: str
    links: tuple[str, ...] = ()
    body: str | None = None

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["tags"] = list(self.tags)
        data["links"] = list(self.links)
        if self.body is None:
            data.pop("body")
        return data


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(text: str, *, max_len: int = 64) -> str:
    slug = _SLUG_RE.sub("-", str(text or "").casefold()).strip("-")
    out = (slug[:max_len].rstrip("-")) or "note"
    if out.split(".", 1)[0].lower() in _WINDOWS_RESERVED_STEMS:
        out = f"note-{out}"
    return out


def _clean_line(value: str, max_len: int = 200) -> str:
    cleaned = "".join(ch for ch in str(value or "") if ord(ch) >= 32 or ch == "\t")
    return re.sub(r"\s+", " ", cleaned).strip()[:max_len]


def _clean_tags(tags) -> tuple[str, ...]:
    out: list[str] = []
    for tag in tags or ():
        t = re.sub(r"[^A-Za-z0-9_:/.-]", "", str(tag or "").strip())[:64]
        if t and t not in out:
            out.append(t)
    return tuple(out[:32])


def render_note(
    *,
    title: str,
    body: str,
    origin: str,
    tags: tuple[str, ...],
    created: str,
    updated: str,
    source_path: str | None = None,
) -> str:
    safe_origin = origin if origin in NOTE_ORIGINS else "owner_import"
    safe_created = _clean_line(created, 64).lstrip("-") or _now_iso()
    safe_updated = _clean_line(updated, 64).lstrip("-") or _now_iso()
    fm = [
        f"title: {json.dumps(title, ensure_ascii=False)}",
        "type: note",
        f"origin: {safe_origin}",
        f"created: {safe_created}",
        f"updated: {safe_updated}",
        f"tags: {json.dumps(list(tags), ensure_ascii=False)}",
    ]
    if source_path:
        fm.append(f"source_path: {json.dumps(source_path, ensure_ascii=False)}")
    return "---\n" + "\n".join(fm) + "\n---\n\n" + str(body or "").replace("\r\n", "\n").strip() + "\n"


def parse_note(text: str, filename: str = "") -> tuple[dict[str, object], str]:
    """Return (meta, body). Plain Markdown without frontmatter is a valid note."""
    parsed = split_frontmatter(text)
    if parsed is None:
        first_heading = re.search(r"^#\s+(.+)$", text or "", re.MULTILINE)
        return {"title": first_heading.group(1).strip() if first_heading else Path(filename).stem}, text or ""
    raw_meta, body_lines = parsed
    values = parse_frontmatter_values(raw_meta)
    meta: dict[str, object] = dict(values)
    title = values.get("title")
    if isinstance(title, str):
        try:
            meta["title"] = json.loads(title) if title.startswith('"') else title
        except json.JSONDecodeError:
            meta["title"] = title
    tags = values.get("tags", [])
    if isinstance(tags, str):
        tags = parse_scalar_list(tags)
    meta["tags"] = [str(t) for t in tags]
    if not meta.get("title"):
        meta["title"] = Path(filename).stem
    return meta, "\n".join(body_lines).strip()


def _snippet(body: str, query: str = "", width: int = 220) -> str:
    flat = re.sub(r"\s+", " ", body or "").strip()
    if not flat:
        return ""
    if query:
        for term in re.findall(r"\w+", query.casefold()):
            if len(term) < 3:
                continue
            pos = flat.casefold().find(term)
            if pos >= 0:
                start = max(0, pos - width // 3)
                prefix = "…" if start > 0 else ""
                return prefix + flat[start : start + width] + ("…" if start + width < len(flat) else "")
    return flat[:width] + ("…" if len(flat) > width else "")


def _record_from_text(filename: str, text: str, digest: str, *, query: str = "", include_body: bool = False) -> NoteRecord:
    meta, body = parse_note(text, filename)
    return NoteRecord(
        filename=filename,
        title=str(meta.get("title") or Path(filename).stem),
        origin=str(meta.get("origin") or "owner_note"),
        tags=tuple(str(t) for t in meta.get("tags", []) or []),
        created=str(meta.get("created") or ""),
        updated=str(meta.get("updated") or ""),
        sha256=digest,
        snippet=_snippet(body, query),
        links=tuple(dict.fromkeys(m.strip() for m in _WIKILINK_RE.findall(body))),
        body=body if include_body else None,
    )


def _resolve_note(paths: NotePaths, filename: str, *, must_exist: bool = True) -> Path:
    if os.path.lexists(paths.notes_dir) and _is_reparse_or_link(paths.notes_dir):
        raise PermissionError(f"Refusing to use symlinked directory: {paths.notes_dir}")
    name = Path(str(filename or "")).name
    if name != filename or not name.endswith(".md") or name.startswith(".") or name == "README.md":
        raise NoteError(f"Invalid note filename: {filename!r}")
    safe_target = safe_file_in_dir(paths.notes_dir, name, allowed_exts={".md"}, must_exist=must_exist)
    if not safe_target:
        if must_exist:
            raise NoteError(f"Note not found: {filename}")
        raise NoteError(f"Invalid note filename: {filename!r}")
    target = Path(safe_target)
    if must_exist:
        if not target.is_file() or _is_reparse_or_link(str(target)):
            raise NoteError(f"Note not found: {filename}")
    elif os.path.lexists(target) and _is_reparse_or_link(str(target)):
        raise NoteError("Refusing to write through a symlinked note path.")
    return target


def _ensure_dirs(paths: NotePaths) -> None:
    for d in (paths.notes_dir, os.path.dirname(paths.index_path)):
        os.makedirs(d, exist_ok=True)
        if _is_reparse_or_link(d):
            raise PermissionError(f"Refusing to use symlinked directory: {d}")


def _create_exclusive(directory: str, stem: str, data: bytes) -> str:
    """Create a new file without ever overwriting; returns the chosen basename."""
    suffix = 1
    while True:
        name = f"{stem}.md" if suffix == 1 else f"{stem}-{suffix}.md"
        path = safe_file_in_dir(directory, name, allowed_exts={".md"}, must_exist=False)
        if not path:
            raise NoteError(f"Invalid note filename: {name!r}")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
        except FileExistsError:
            suffix += 1
            continue
        try:
            if os.write(fd, data) != len(data):
                raise OSError("Incomplete note write.")
            os.fsync(fd)
        finally:
            os.close(fd)
        return name


def _quarantine_agent_capture(paths: NotePaths, title: str, body: str, risks: tuple[str, ...], source: str) -> dict:
    os.makedirs(paths.inbox_dir, exist_ok=True)
    if _is_reparse_or_link(paths.inbox_dir):
        raise PermissionError(f"Refusing to quarantine capture into symlinked inbox directory: {paths.inbox_dir}")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_source = re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9._:-]", "", str(source or "")))[:64].strip("-") or "agent"
    payload = (
        "<!-- second_brain_origin: api_local -->\n"
        f"<!-- capture_source: {safe_source} -->\n\n# {title}\n\n{body}\n"
    ).encode("utf-8")
    name = _create_exclusive(paths.inbox_dir, f"{stamp}-{slugify(title)}-agent", payload)
    return {
        "status": "quarantined",
        "filename": name,
        "location": "0-raw/inbox/",
        "risk_flags": list(risks),
        "reason": "Agent capture matched risk patterns and requires owner review.",
    }


def create_note(
    title: str,
    body: str,
    *,
    origin: str = "owner_note",
    tags=(),
    source_path: str | None = None,
    capture_source: str = "owner",
    paths: NotePaths = DEFAULT_PATHS,
    reindex: bool = True,
) -> dict[str, object]:
    """Create a new note and make it searchable immediately. Never overwrites."""
    if origin not in NOTE_ORIGINS:
        raise NoteError(f"Unsupported note origin: {origin!r}")
    clean_title = _clean_line(title) or _clean_line(str(body or "").strip().splitlines()[0] if str(body or "").strip() else "") or "Untitled"
    text_body = str(body or "")
    if origin in AGENT_ORIGINS:
        if len(text_body) > MAX_AGENT_CAPTURE_CHARS:
            raise NoteError(f"Agent capture exceeds {MAX_AGENT_CAPTURE_CHARS} characters.")
        risks = scan_inbox_payload_risks(f"{clean_title}\n{text_body}")
        if risks:
            return _quarantine_agent_capture(paths, clean_title, text_body, risks, capture_source)
    now = _now_iso()
    rendered = render_note(
        title=clean_title,
        body=text_body,
        origin=origin,
        tags=_clean_tags(tags),
        created=now,
        updated=now,
        source_path=source_path,
    ).encode("utf-8")
    if len(rendered) > MAX_NOTE_BYTES:
        raise NoteError("Note exceeds the maximum supported size (2 MB).")
    _ensure_dirs(paths)
    with review_lock(paths.lock_path):
        name = _create_exclusive(paths.notes_dir, slugify(clean_title), rendered)
        if reindex:
            upsert_lexical_atoms(paths.notes_dir, paths.index_path, [name])
    return {"status": "saved", "filename": name, "sha256": content_sha256(rendered), "origin": origin}


def update_note(
    filename: str,
    body: str,
    *,
    expected_sha256: str,
    title: str | None = None,
    tags=None,
    paths: NotePaths = DEFAULT_PATHS,
) -> dict[str, object]:
    """Compare-and-swap save. The app passes back the hash it loaded; the owner never sees it."""
    _ensure_dirs(paths)
    with review_lock(paths.lock_path):
        target = _resolve_note(paths, filename)
        original = target.read_bytes()
        current = content_sha256(original)
        if current != str(expected_sha256 or "").strip().lower():
            raise ReviewConflictError(
                "This note changed since you opened it (another window, editor, or sync). Reload to merge."
            )
        meta, _ = parse_note(original.decode("utf-8"), filename)
        origin = str(meta.get("origin") or "owner_note")
        if origin not in NOTE_ORIGINS:
            origin = "owner_import"
        new_title = _clean_line(title) if title is not None else str(meta.get("title") or Path(filename).stem)
        new_tags = _clean_tags(tags) if tags is not None else _clean_tags(meta.get("tags", []))
        rendered = render_note(
            title=new_title or Path(filename).stem,
            body=body,
            origin=origin,
            tags=new_tags,
            created=str(meta.get("created") or _now_iso()),
            updated=_now_iso(),
            source_path=str(meta["source_path"]).strip('"') if meta.get("source_path") else None,
        ).encode("utf-8")
        if len(rendered) > MAX_NOTE_BYTES:
            raise NoteError("Note exceeds the maximum supported size (2 MB).")
        if rendered == original:
            return {"status": "unchanged", "filename": filename, "sha256": current}
        history = Path(paths.notes_dir) / ".history"
        history.mkdir(exist_ok=True)
        backup = history / f"{filename}.{current[:16]}.bak"
        if _is_reparse_or_link(str(history)) or (os.path.lexists(backup) and _is_reparse_or_link(str(backup))):
            raise PermissionError("Refusing to write note history through a symlink or junction.")
        if not backup.exists():
            atomic_write_bytes(str(backup), original)
        atomic_write_bytes(str(target), rendered)
        upsert_lexical_atoms(paths.notes_dir, paths.index_path, [filename])
    return {"status": "saved", "filename": filename, "sha256": content_sha256(rendered), "previous_sha256": current}


def delete_note(filename: str, *, expected_sha256: str, paths: NotePaths = DEFAULT_PATHS) -> dict[str, object]:
    """Reversible delete: moves the note to notes/.trash."""
    with review_lock(paths.lock_path):
        target = _resolve_note(paths, filename)
        current = content_sha256(target.read_bytes())
        if current != str(expected_sha256 or "").strip().lower():
            raise ReviewConflictError("This note changed since you opened it. Reload before deleting.")
        trash = Path(paths.notes_dir) / ".trash"
        trash.mkdir(exist_ok=True)
        dest = trash / f"{current[:12]}-{filename}"
        if _is_reparse_or_link(str(trash)) or (os.path.lexists(dest) and _is_reparse_or_link(str(dest))):
            raise PermissionError("Refusing to move note to symlinked trash path.")
        os.replace(target, dest)
        upsert_lexical_atoms(paths.notes_dir, paths.index_path, [], [filename])
    return {"status": "trashed", "filename": filename, "trash_path": str(dest)}


def get_note(filename: str, *, paths: NotePaths = DEFAULT_PATHS) -> NoteRecord:
    target = _resolve_note(paths, filename)
    raw = target.read_bytes()
    return _record_from_text(filename, raw.decode("utf-8"), content_sha256(raw), include_body=True)


def ensure_notes_index(paths: NotePaths = DEFAULT_PATHS) -> dict[str, object]:
    """Self-heal the notes index after out-of-band edits (incremental diff, not full rebuild)."""
    _ensure_dirs(paths)
    with review_lock(paths.lock_path):
        return sync_lexical_index(paths.notes_dir, paths.index_path)


def _search_with_heal(fn, paths: NotePaths):
    try:
        return fn()
    except LexicalIndexUnavailable:
        ensure_notes_index(paths)
        return fn()


def search_notes(query: str, *, limit: int = 8, paths: NotePaths = DEFAULT_PATHS) -> list[NoteRecord]:
    hits = _search_with_heal(
        lambda: search_lexical_index(query, paths.index_path, n_results=limit, atoms_dir=paths.notes_dir),
        paths,
    )
    return [_record_from_text(h.filename, h.content, h.content_sha256, query=query) for h in hits]


def list_notes(
    *, query: str = "", limit: int = 100, offset: int = 0, paths: NotePaths = DEFAULT_PATHS
) -> tuple[list[NoteRecord], int]:
    if not os.path.isfile(paths.index_path) or not lexical_index_is_current(paths.notes_dir, paths.index_path):
        ensure_notes_index(paths)
    items, total = _search_with_heal(
        lambda: list_indexed_atoms_fast(paths.index_path, query=query, limit=limit, offset=offset),
        paths,
    )
    return [_record_from_text(i.filename, i.content, i.content_sha256, query=query) for i in items], total


def backlinks(filename: str, *, paths: NotePaths = DEFAULT_PATHS, limit: int = 50) -> list[NoteRecord]:
    """Notes linking to ``filename`` via [[wikilink]] (title, stem, [[target|alias]], or [[target#heading]])."""
    target_note = get_note(filename, paths=paths)
    title_cf = target_note.title.strip().casefold()
    stem_cf = Path(filename).stem.strip().casefold()
    valid_targets = {stem_cf, title_cf}
    if not os.path.isfile(paths.index_path) or not lexical_index_is_current(paths.notes_dir, paths.index_path):
        ensure_notes_index(paths)
    indexed, _ = _search_with_heal(
        lambda: list_indexed_atoms_fast(paths.index_path, query="", limit=100_000, offset=0),
        paths,
    )
    found: list[NoteRecord] = []
    for item in indexed:
        if item.filename == filename:
            continue
        if "[[" not in item.content:
            continue
        rec = _record_from_text(item.filename, item.content, item.content_sha256)
        targets = {
            t.split("|", 1)[0].split("#", 1)[0].strip().removesuffix(".md").casefold()
            for t in rec.links
            if t.strip()
        }
        if targets & valid_targets:
            found.append(rec)
            if len(found) >= max(1, limit):
                break
    return found


def verify_note_excerpt(
    filename: str,
    excerpt: str,
    *,
    actor: str,
    expected_note_sha256: str,
    claim: str | None = None,
    memory_type: str = "semantic",
    tags=(),
    paths: NotePaths = DEFAULT_PATHS,
) -> dict[str, object]:
    """One-click owner promotion of a note excerpt into a source-anchored, approved atom.

    The program performs every integrity step (immutable snapshot, byte-offset anchor,
    SHA-256 pinning, decision record). Only a human actor may call this; API surfaces
    must gate it behind the owner credential, never the agent token.
    """
    clean_actor = _clean_line(actor, 120)
    if clean_actor.casefold() in DISALLOWED_ACTORS:
        raise NoteError("Verification requires an explicit human owner actor.")
    if memory_type not in {"semantic", "episodic", "procedural", "self"}:
        raise NoteError(f"Unsupported memory_type: {memory_type!r}")
    raw_excerpt = re.sub(r"\s+", " ", str(excerpt or "")).strip()
    md_stripped_excerpt = re.sub(r"^(?:>+\s*|#{1,6}\s+)+", "", raw_excerpt).strip()
    candidates: list[str] = []
    if not re.match(r"^##\s+", raw_excerpt) and len(raw_excerpt) >= 12:
        candidates.append(raw_excerpt)
    if len(md_stripped_excerpt) >= 12 and md_stripped_excerpt not in candidates:
        candidates.append(md_stripped_excerpt)
    if not candidates:
        raise NoteError("Select at least 12 characters of the note to verify.")
    if max(len(c) for c in candidates) > 4000:
        raise NoteError("Verified excerpts are limited to 4,000 characters; select a single claim.")
    raw_claim = re.sub(r"\s+", " ", str(claim or candidates[0])).strip()
    flat_claim = re.sub(r"^(?:>+\s*|#{1,6}\s+|[-*]\s+)+", "", raw_claim).strip() or raw_claim
    if not flat_claim or len(flat_claim) > 2000:
        raise NoteError("Claim must be between 1 and 2,000 characters.")
    risks = scan_inbox_payload_risks(flat_claim + "\n" + "\n".join(candidates))
    if risks:
        raise NoteError(f"Cannot verify excerpt containing security risk flags: {', '.join(risks)}")

    os.makedirs(paths.snapshots_dir, exist_ok=True)
    os.makedirs(paths.atoms_dir, exist_ok=True)
    if _is_reparse_or_link(paths.snapshots_dir) or _is_reparse_or_link(paths.atoms_dir):
        raise PermissionError("Refusing to use symlinked snapshots or atoms directory.")
    with review_lock(paths.lock_path):
        target = _resolve_note(paths, filename)
        note_bytes = target.read_bytes()
        note_sha = content_sha256(note_bytes)
        if note_sha != str(expected_note_sha256 or "").strip().lower():
            raise ReviewConflictError("The note changed since you selected the excerpt. Reload and select again.")
        stem = Path(filename).stem
        snap_name = f"{stem}-{note_sha[:12]}.md"
        snap_path = os.path.join(paths.snapshots_dir, snap_name)
        if os.path.lexists(snap_path) and _is_reparse_or_link(snap_path):
            raise PermissionError("Refusing to use symlinked snapshot file.")
        if os.path.exists(snap_path):
            if content_sha256(Path(snap_path).read_bytes()) != note_sha:
                raise NoteError("Existing snapshot does not match; refusing to overwrite immutable evidence.")
        else:
            fd = os.open(snap_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o400 if os.name != "nt" else 0o600)
            try:
                os.write(fd, note_bytes)
                os.fsync(fd)
            finally:
                os.close(fd)
        rel_source = Path(os.path.relpath(snap_path, paths.brain_dir)).as_posix()
        today = datetime.date.today().isoformat()
        clean_tags = _clean_tags(tags)

        def _build_note_atom_text(excerpt_val: str) -> str:
            return (
                "---\n"
                f"id: note-{stem}-{hashlib.sha256(excerpt_val.encode('utf-8')).hexdigest()[:8]}\n"
                "type: atom\n"
                f"memory_type: {memory_type}\n"
                "certainty: established\n"
                f"created: {today}\n"
                f"last-reviewed: {today}\n"
                "importance: 0.70\n"
                "confidence: 0.90\n"
                "sources:\n"
                f"  - {rel_source}\n"
                f"links: [{json.dumps(stem)}]\n"
                f"tags: {json.dumps(list(clean_tags))}\n"
                "contexts: []\n"
                "supersedes: []\n"
                "superseded_by: []\n"
                "contested_by: []\n"
                "origin: user_local\n"
                "citation_integrity: missing\n"
                "claim_state: candidate\n"
                "epistemic_label: claim\n"
                "review_status: pending\n"
                "---\n\n"
                f"## Claim\n{flat_claim}\n\n"
                f"## Evidence\n{excerpt_val}\n\n"
                f"## Why It Matters\nVerified by the owner from note [[{stem}]].\n"
            )

        atom_text = ""
        last_exc: EvidenceValidationError | None = None
        for candidate_excerpt in candidates:
            candidate_text = _build_note_atom_text(candidate_excerpt)
            try:
                find_evidence_anchor(candidate_text, paths.brain_dir)
                atom_text = candidate_text
                break
            except EvidenceValidationError as exc:
                last_exc = exc
        if not atom_text:
            raise NoteError(f"Could not anchor the excerpt in the note: {last_exc}") from last_exc
        atom_bytes = atom_text.encode("utf-8")
        atom_name = _create_exclusive(paths.atoms_dir, f"note-{slugify(flat_claim, max_len=48)}", atom_bytes)
        atom_path = os.path.join(paths.atoms_dir, atom_name)
        upsert_lexical_atoms(paths.atoms_dir, os.path.join(paths.brain_dir, ".sidecar", "lexical.sqlite3"), [atom_name])
    # The writer lock is non-reentrant; approval acquires it itself and re-verifies by CAS.
    try:
        record = apply_review_transition(
            action="approve",
            atom_path=atom_path,
            expected_sha256=content_sha256(atom_bytes),
            actor=clean_actor,
            brain_dir=paths.brain_dir,
            atoms_dir=paths.atoms_dir,
        )
    except (ReviewConflictError, ReviewTransitionError) as exc:
        with review_lock(paths.lock_path):
            if os.path.isfile(atom_path):
                os.remove(atom_path)
            upsert_lexical_atoms(
                paths.atoms_dir,
                os.path.join(paths.brain_dir, ".sidecar", "lexical.sqlite3"),
                [],
                [atom_name],
            )
        raise NoteError(f"Atom created as pending ({atom_name}) failed approval and was rolled back: {exc}") from exc
    return {
        "status": "verified",
        "atom_filename": atom_name,
        "atom_sha256": record["content_sha256_after"],
        "evidence_source": rel_source,
        "claim": flat_claim,
        "memory_type": memory_type,
    }


def rebuild_notes_index(paths: NotePaths = DEFAULT_PATHS) -> dict[str, object]:
    _ensure_dirs(paths)
    with review_lock(paths.lock_path):
        return rebuild_lexical_index(paths.notes_dir, paths.index_path)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Second Brain owner notes (zero-friction capture).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="Create a note (searchable immediately).")
    add.add_argument("title")
    add.add_argument("body", nargs="?", default=None, help="Body text; reads stdin if omitted.")
    add.add_argument("--tag", action="append", default=[])
    s = sub.add_parser("search", help="Search notes.")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=8)
    ls = sub.add_parser("list", help="List notes.")
    ls.add_argument("--limit", type=int, default=50)
    sub.add_parser("reindex", help="Reconcile the notes index with disk.")
    v = sub.add_parser("verify", help="Promote a note excerpt to a verified atom (owner action).")
    v.add_argument("filename")
    v.add_argument("excerpt")
    v.add_argument("--actor", required=True)
    v.add_argument("--claim")
    v.add_argument("--memory-type", default="semantic", choices=["semantic", "episodic", "procedural", "self"])
    v.add_argument("--tag", action="append", default=[])
    args = parser.parse_args(argv)

    if args.cmd == "add":
        body = args.body if args.body is not None else sys.stdin.read()
        print(json.dumps(create_note(args.title, body, tags=args.tag), indent=2))
    elif args.cmd == "search":
        print(json.dumps([r.as_dict() for r in search_notes(args.query, limit=args.limit)], indent=2, ensure_ascii=False))
    elif args.cmd == "list":
        items, total = list_notes(limit=args.limit)
        print(json.dumps({"total": total, "items": [r.as_dict() for r in items]}, indent=2, ensure_ascii=False))
    elif args.cmd == "reindex":
        print(json.dumps(ensure_notes_index(), indent=2))
    elif args.cmd == "verify":
        note = get_note(args.filename)
        print(json.dumps(
            verify_note_excerpt(
                args.filename,
                args.excerpt,
                actor=args.actor,
                claim=args.claim,
                memory_type=args.memory_type,
                tags=args.tag,
                expected_note_sha256=note.sha256,
            ),
            indent=2,
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
