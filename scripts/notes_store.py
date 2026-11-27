# /// script
# requires-python = ">=3.11,<3.14"
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
    resolve_active_brain_dir,
    safe_file_in_dir,
)
from evidence import EvidenceValidationError, find_evidence_anchor  # noqa: E402
from inbox_workflow import scan_inbox_payload_risks  # noqa: E402
from memory_format import evaluate_temporal_validity, normalize_temporal_bound_str  # noqa: E402
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
from recall import normalize_project_tag, project_tags_match  # noqa: E402

NOTE_ORIGINS = frozenset({"owner_note", "owner_import", "agent_capture", "web_clipper"})
AGENT_ORIGINS = frozenset({"agent_capture"})
MAX_NOTE_BYTES = 2_000_000
MAX_AGENT_CAPTURE_CHARS = 16_000
DISALLOWED_ACTORS = frozenset({"auto", "system", "llm", "model", "ollama", "assistant", "agent", ""})
_O_BINARY = getattr(os, "O_BINARY", 0)  # Windows: prevent CRLF translation that breaks hashes
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
_CHECKBOX_TASK_RE = re.compile(r"^\s*[-*+]\s*\[([ xX])\]\s+(.+)$")
_KEYWORD_TASK_RE = re.compile(r"^\s*(?:[-*+]\s+)?(TODO|ACTION|FIXME)\s*:\s*(.+)$")
_FENCE_OPEN_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_DUE_DATE_RE = re.compile(r"(?:@due\((\d{4}-\d{2}-\d{2})\)|\bdue:(\d{4}-\d{2}-\d{2})\b)", re.IGNORECASE)
_PRIORITY_RE = re.compile(r"(?:(?<!\S)!(high|medium|low)\b|\[(P1|P2|P3)\])", re.IGNORECASE)
_INLINE_TAG_RE = re.compile(r"(?<!\S)(?:#([A-Za-z0-9_:/.-]+)|(project:[A-Za-z0-9_:/.-]+))")
_SKIP_RAW_FILENAMES = frozenset({"reading-list.md", "readme.md"})
_SKIP_RAW_DIRS = frozenset({".history", ".trash", "inbox", "archive", "note-snapshots"})


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
_INITIAL_DEFAULT_PATHS = DEFAULT_PATHS


def _extract_derived_contexts(tags: tuple[str, ...]) -> list[str]:
    """Derive canonical project/context scope tags from merged note + verification tags."""
    contexts: list[str] = []
    seen: set[str] = set()
    for raw_tag in tags:
        tag = str(raw_tag or "").strip()
        if ":" not in tag:
            continue
        prefix, rest = tag.split(":", 1)
        p_low = prefix.strip().lower()
        val = rest.strip()
        if not val:
            continue
        if p_low == "ctx":
            canonical = f"context:{val}"
        elif p_low in {"project", "context", "domain", "topic"}:
            canonical = f"{p_low}:{val}"
        else:
            continue
        dedup_key = canonical.casefold()
        if dedup_key not in seen:
            seen.add(dedup_key)
            contexts.append(canonical)
    return contexts


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


_GENERIC_NOTE_MESSAGE = (
    "Not saved. This is general knowledge, or it is too thin to teach the brain. "
    "Add an operational rule for this system: a command, a named threshold, a config value, "
    "or which tool this project uses. Otherwise include two concrete details, such as a path and a file. "
    "A single filename is not enough."
)
_PATH_ANCHOR_RE = re.compile(r"(?:^|[\s\"'(])((?:\.\./|\./|/)[\w./-]{3,})", re.MULTILINE)
_FILE_ANCHOR_RE = re.compile(
    r"\b[\w.-]+\.(?:css|scss|sass|less|js|mjs|cjs|jsx|ts|tsx|py|pyi|go|rs|java|kt|rb|php|json|ya?ml|toml|ini|cfg|conf|md|html|htm|sql|sh|ps1|xml|lock|txt|vue|svelte)\b",
    re.IGNORECASE,
)
_FLAG_ANCHOR_RE = re.compile(r"\b[\w.-]+\s+--[\w-]+")
_UNIT_ANCHOR_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|ms|s|px|mb|gb)\b", re.IGNORECASE)
_VERSION_ANCHOR_RE = re.compile(r"\bv?\d+\.\d+\.\d+\b")
_CHOICE_ANCHOR_RE = re.compile(
    r"\b(?:this |our |the )?(?:repo|project|site|vault|app|service|checkout|codebase) uses "
    r"(?!best\b|proper\b|secure\b|good\b|https\b)[A-Za-z0-9_./+-]{2,}",
    re.IGNORECASE,
)
_ASSIGN_ANCHOR_RE = re.compile(r"\b[A-Za-z][\w.-]{1,40}[ \t]*(?:=|:)[ \t]*[^\s,]{1,48}")
_TICK_ANCHOR_RE = re.compile(r"`([^`\n]{3,80})`")
_GENERIC_TICK_RE = re.compile(r"^(?:https|http|input|password|user|data|token|secret|true|false|null|none)$", re.IGNORECASE)
_THRESHOLD_RE = re.compile(
    r"(?:`[^`\n]{2,40}`|\b[A-Za-z_][\w.-]{2,40})\s*(?:is\s+)?(?:>=|<=|==|>|<)\s*\d+(?:\.\d+)?"
)
_PROCEDURE_RE = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+\S", re.MULTILINE)
_STRONG_NOTE_SIGNALS = frozenset({"command", "choice", "threshold", "config"})


def note_spec_signals(title: str, body: str) -> set[str]:
    """Concrete signals that make a note local to this system.

    A command, named threshold, config value, or project tool choice is enough on
    its own. A path, file, version, measurement, identifier, or procedure counts
    only together with a second concrete detail.
    """
    text = f"{title}\n{body}"
    signals: set[str] = set()
    if _PATH_ANCHOR_RE.search(text):
        signals.add("path")
    if _FILE_ANCHOR_RE.search(text):
        signals.add("file")
    if _FLAG_ANCHOR_RE.search(text):
        signals.add("command")
    if _UNIT_ANCHOR_RE.search(text):
        signals.add("measure")
    if _VERSION_ANCHOR_RE.search(text):
        signals.add("version")
    if _CHOICE_ANCHOR_RE.search(text):
        signals.add("choice")
    if _THRESHOLD_RE.search(text):
        signals.add("threshold")
    if len(_PROCEDURE_RE.findall(text)) >= 2:
        signals.add("procedure")
    for match in _ASSIGN_ANCHOR_RE.finditer(text):
        value = match.group(0).split("=", 1)[-1].split(":", 1)[-1].strip()
        if re.search(r"[\d/._]", value) and value.casefold() not in {"true", "false", "null", "none", "https"}:
            signals.add("config")
            break
    for match in _TICK_ANCHOR_RE.finditer(text):
        inner = match.group(1).strip()
        if re.search(r"[\d/._:]", inner) and not _GENERIC_TICK_RE.match(inner):
            signals.add("identifier")
            break
    return signals


def agent_capture_rejection(title: str, body: str) -> str | None:
    """Refuse an agent note that is general knowledge or only one thin detail.

    Owner-written notes are not checked. One operational rule is enough. Two
    different concrete details are enough. A filename by itself is not.
    """
    signals = note_spec_signals(title, body)
    if signals & _STRONG_NOTE_SIGNALS:
        return None
    if len(signals) >= 2:
        return None
    return _GENERIC_NOTE_MESSAGE


def _learn_saved_notes(paths: NotePaths) -> None:
    """Best-effort: a saved note becomes shared memory without blocking the save."""
    try:
        from agent_runtime import learn_from_notes

        learn_from_notes(brain_dir=paths.brain_dir)
    except Exception:
        return


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
        generic = agent_capture_rejection(clean_title, text_body)
        if generic:
            return {"status": "not_saved", "reason": "generic", "message": generic}
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
    saved = {"status": "saved", "filename": name, "sha256": content_sha256(rendered), "origin": origin}
    _learn_saved_notes(paths)
    return saved


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
    saved = {"status": "saved", "filename": filename, "sha256": content_sha256(rendered), "previous_sha256": current}
    _learn_saved_notes(paths)
    return saved


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


def _matches_project_scope(tags: tuple[str, ...] | list[str], scope_tag: str) -> bool:
    """Strict multi-project containment: require >=1 project:* tag and every project:* tag within scope_tag."""
    if not scope_tag:
        return True
    proj_tags = [str(t).strip() for t in tags if str(t).strip().lower().startswith("project:")]
    return bool(proj_tags) and all(bool(project_tags_match((t,), (scope_tag,))) for t in proj_tags)


def search_notes(
    query: str,
    *,
    limit: int = 8,
    project: str | None = None,
    paths: NotePaths = DEFAULT_PATHS,
) -> list[NoteRecord]:
    eff_limit = max(1, int(limit))
    scope_raw = (project or "").strip()
    scope_tag = normalize_project_tag(scope_raw) if scope_raw else ""
    fetch_limit = max(eff_limit * 5, 40) if scope_tag else eff_limit
    hits = _search_with_heal(
        lambda: search_lexical_index(query, paths.index_path, n_results=fetch_limit, atoms_dir=paths.notes_dir),
        paths,
    )
    records: list[NoteRecord] = []
    for h in hits:
        rec = _record_from_text(h.filename, h.content, h.content_sha256, query=query)
        if scope_tag and not _matches_project_scope(rec.tags, scope_tag):
            continue
        records.append(rec)
        if len(records) >= eff_limit:
            break
    return records


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
    valid_from: str | None = None,
    valid_until: str | None = None,
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
    try:
        norm_vf = normalize_temporal_bound_str(valid_from) if valid_from else ""
        norm_vu = normalize_temporal_bound_str(valid_until) if valid_until else ""
    except ValueError as exc:
        raise NoteError(f"Invalid temporal validity bound: {exc}") from exc
    if norm_vf or norm_vu:
        tv = evaluate_temporal_validity(norm_vf, norm_vu)
        if tv["status"] == "invalid_window":
            raise NoteError(f"Invalid temporal validity window: valid_from ({norm_vf}) > valid_until ({norm_vu}).")
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
        parent_rec = _record_from_text(filename, note_bytes.decode("utf-8"), note_sha)
        all_note_tags = _clean_tags((*parent_rec.tags, *(tags or ())))
        derived_contexts = _extract_derived_contexts(all_note_tags)

        auto_linked_filenames: list[str] = []
        auto_linked_stems: list[str] = []
        dup_candidates: list[dict[str, object]] = []
        contra_alerts: list[dict[str, object]] = []
        try:
            from knowledge_graph import suggest_atom_graph_links_for_claim

            graph_sug = suggest_atom_graph_links_for_claim(
                flat_claim,
                contexts=derived_contexts,
                brain_dir=paths.brain_dir,
                atoms_dir=paths.atoms_dir,
                include_pending=False,
                top_k=4,
            )
            for rel_item in graph_sug.get("related_links") or []:
                r_stem = str(rel_item.get("stem") or "").strip()
                r_fn = str(rel_item.get("filename") or "").strip()
                if r_stem and r_stem != stem and r_stem not in auto_linked_stems:
                    auto_linked_stems.append(r_stem)
                    if r_fn:
                        auto_linked_filenames.append(r_fn)
            dup_candidates = list(graph_sug.get("duplicate_candidates") or [])
            contra_alerts = list(graph_sug.get("contradiction_alerts") or [])
        except Exception:
            pass

        merged_frontmatter_links = [stem, *auto_linked_stems]
        related_wikilinks_suffix = (
            " Related atoms: " + ", ".join(f"[[{s}]]" for s in auto_linked_stems) + "."
            if auto_linked_stems
            else ""
        )

        atom_origin_map = {
            "owner_note": "user_local",
            "owner_import": "imported_unknown",
            "agent_capture": "mcp_local",
            "web_clipper": "web_clipper",
        }
        preserved_atom_origin = atom_origin_map.get(str(parent_rec.origin or "").strip(), "imported_unknown")

        def _build_note_atom_text(excerpt_val: str) -> str:
            temporal_lines = ""
            if norm_vf:
                temporal_lines += f"valid_from: {norm_vf}\n"
            if norm_vu:
                temporal_lines += f"valid_until: {norm_vu}\n"
            return (
                "---\n"
                f"id: note-{stem}-{hashlib.sha256(excerpt_val.encode('utf-8')).hexdigest()[:8]}\n"
                "type: atom\n"
                f"memory_type: {memory_type}\n"
                "certainty: established\n"
                f"created: {today}\n"
                f"last-reviewed: {today}\n"
                f"{temporal_lines}"
                "importance: 0.70\n"
                "confidence: 0.90\n"
                "sources:\n"
                f"  - {rel_source}\n"
                f"links: {json.dumps(merged_frontmatter_links)}\n"
                f"tags: {json.dumps(list(all_note_tags))}\n"
                f"contexts: {json.dumps(derived_contexts)}\n"
                "supersedes: []\n"
                "superseded_by: []\n"
                "contested_by: []\n"
                f"origin: {preserved_atom_origin}\n"
                "citation_integrity: missing\n"
                "claim_state: candidate\n"
                "epistemic_label: claim\n"
                "review_status: pending\n"
                "---\n\n"
                f"## Claim\n{flat_claim}\n\n"
                f"## Evidence\n{excerpt_val}\n\n"
                f"## Why It Matters\nVerified by the owner from note [[{stem}]].{related_wikilinks_suffix}\n"
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
        "valid_from": norm_vf,
        "valid_until": norm_vu,
        "auto_linked_atoms": auto_linked_filenames,
        "duplicate_candidates": dup_candidates,
        "contradiction_alerts": contra_alerts,
    }


def rebuild_notes_index(paths: NotePaths = DEFAULT_PATHS) -> dict[str, object]:
    _ensure_dirs(paths)
    with review_lock(paths.lock_path):
        return rebuild_lexical_index(paths.notes_dir, paths.index_path)


def _split_cli_tags(raw_tags: list[str] | None) -> list[str]:
    return [
        part.strip()
        for raw in (raw_tags or [])
        for part in str(raw).split(",")
        if part.strip()
    ]


def _active_cli_note_paths() -> NotePaths:
    if DEFAULT_PATHS != _INITIAL_DEFAULT_PATHS:
        return DEFAULT_PATHS
    return NotePaths.for_brain(resolve_active_brain_dir())


def _parse_due_date(line_text: str) -> str | None:
    match = _DUE_DATE_RE.search(line_text)
    if not match:
        return None
    candidate = match.group(1) or match.group(2)
    try:
        datetime.date.fromisoformat(candidate)
    except ValueError:
        return None
    return candidate


def _parse_priority(line_text: str) -> str | None:
    match = _PRIORITY_RE.search(line_text)
    if not match:
        return None
    if match.group(1):
        return match.group(1).lower()
    p_code = (match.group(2) or "").upper()
    return {"P1": "high", "P2": "medium", "P3": "low"}.get(p_code)


def _extract_inline_tags(line_text: str) -> list[str]:
    found: list[str] = []
    for match in _INLINE_TAG_RE.finditer(line_text):
        raw_tok = (match.group(1) or match.group(2) or "").rstrip(".,;:!?)")
        cleaned = re.sub(r"[^A-Za-z0-9_:/.-]", "", raw_tok)[:64]
        if cleaned and cleaned not in found:
            found.append(cleaned)
    return found


def _frontmatter_body_start_index(lines: list[str]) -> int:
    if not lines or lines[0].strip() != "---":
        return 0
    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return 0
    if any(re.match(r"^[A-Za-z0-9_-]+\s*:", ln) for ln in lines[1:end_idx]):
        return end_idx + 1
    return 0


def _scan_file_action_items(
    *,
    filepath: Path,
    filename: str,
    source_kind: str,
    max_bytes: int,
) -> list[dict[str, object]]:
    try:
        st = filepath.stat()
    except OSError:
        return []
    if st.st_size <= 0 or st.st_size > max_bytes:
        return []
    try:
        raw_bytes = filepath.read_bytes()
    except OSError:
        return []
    digest = content_sha256(raw_bytes)
    full_text = raw_bytes.decode("utf-8", errors="replace")

    if filename.lower().endswith(".md"):
        meta, body_for_title = parse_note(full_text, filename)
        raw_title = str(meta.get("title") or meta.get("document_title") or "").strip()
        if not raw_title or raw_title == Path(filename).stem:
            first_h = re.search(r"^#\s+(.+)$", body_for_title or "", re.MULTILINE)
            if first_h:
                raw_title = first_h.group(1).strip()
        title = _clean_line(raw_title or Path(filename).stem)
        base_tags: list[str] = []
        for field_key in ("tags", "contexts"):
            vals = meta.get(field_key, [])
            if isinstance(vals, str):
                vals = parse_scalar_list(vals)
            if isinstance(vals, (list, tuple)):
                for v in vals:
                    sv = str(v).strip()
                    if sv and sv not in base_tags:
                        base_tags.append(sv)
    else:
        first_nonempty = next((ln.strip() for ln in full_text.splitlines() if ln.strip()), "")
        heading = re.match(r"^#\s+(.+)$", first_nonempty)
        title = _clean_line(heading.group(1) if heading else Path(filename).stem)
        base_tags = []

    lines = full_text.splitlines()
    start_idx = _frontmatter_body_start_index(lines) if filename.lower().endswith(".md") else 0

    in_fence_char: str | None = None
    in_fence_len: int = 0
    extracted: list[dict[str, object]] = []

    for idx in range(start_idx, len(lines)):
        line = lines[idx]
        line_number = idx + 1

        if in_fence_char is not None:
            if re.match(rf"^\s*{re.escape(in_fence_char)}{{{in_fence_len},}}\s*$", line):
                in_fence_char = None
                in_fence_len = 0
            continue

        fence_match = _FENCE_OPEN_RE.match(line)
        if fence_match:
            token = fence_match.group(1)
            rest = line[fence_match.end() :]
            # Single-line inline fence (e.g. ```foo```) does not open a multi-line block
            if token[0] == "`" and token in rest:
                continue
            in_fence_char = token[0]
            in_fence_len = len(token)
            continue

        cb_match = _CHECKBOX_TASK_RE.match(line)
        if cb_match:
            completed = cb_match.group(1) in ("x", "X")
            marker = "checkbox"
            raw_item_text = cb_match.group(2)
        else:
            kw_match = _KEYWORD_TASK_RE.match(line)
            if not kw_match:
                continue
            completed = False
            marker = kw_match.group(1).lower()
            raw_item_text = kw_match.group(2)

        item_text = _clean_line(raw_item_text, max_len=500)
        if not item_text:
            continue

        inline_tags = _extract_inline_tags(raw_item_text)
        merged_tags = list(dict.fromkeys([*base_tags, *inline_tags]))
        item_projects = list(
            dict.fromkeys(
                normalize_project_tag(t)
                for t in merged_tags
                if t.lower().startswith("project:") and normalize_project_tag(t)
            )
        )

        extracted.append(
            {
                "text": item_text,
                "completed": completed,
                "marker": marker,
                "source_kind": source_kind,
                "filename": filename,
                "title": title,
                "line_number": line_number,
                "sha256": digest,
                "tags": merged_tags,
                "projects": item_projects,
                "due_date": _parse_due_date(raw_item_text),
                "priority": _parse_priority(raw_item_text),
            }
        )
    return extracted


def extract_action_items(
    *,
    paths: NotePaths = DEFAULT_PATHS,
    raw_dir: str | Path | None = None,
    include_completed: bool = False,
    include_raw: bool = True,
    project: str | None = None,
    tag: str | None = None,
) -> list[dict[str, object]]:
    """Model-free deterministic scanner for tasks and action items across working notes and local raw captures."""
    scope_raw = (project or "").strip()
    scope_tag = normalize_project_tag(scope_raw) if scope_raw else ""
    wanted_tag = (tag or "").strip().lstrip("#").casefold()
    wanted_proj_tag = normalize_project_tag(wanted_tag) if wanted_tag.startswith("project:") else ""

    all_items: list[dict[str, object]] = []

    notes_root = Path(paths.notes_dir)
    if os.path.lexists(str(notes_root)) and _is_reparse_or_link(str(notes_root)):
        raise PermissionError(f"Refusing to scan symlinked notes directory: {notes_root}")
    if notes_root.is_dir():
        for entry in sorted(os.listdir(notes_root)):
            if entry.startswith((".", "~")) or entry == "README.md" or not entry.lower().endswith(".md"):
                continue
            safe_path = safe_file_in_dir(str(notes_root), entry, allowed_exts={".md"}, must_exist=True)
            if not safe_path or _is_reparse_or_link(safe_path) or not os.path.isfile(safe_path):
                continue
            all_items.extend(
                _scan_file_action_items(
                    filepath=Path(safe_path),
                    filename=entry,
                    source_kind="note",
                    max_bytes=MAX_NOTE_BYTES,
                )
            )

    if include_raw:
        resolved_raw = Path(raw_dir) if raw_dir is not None else (Path(paths.brain_dir) / "0-raw")
        if os.path.lexists(str(resolved_raw)) and _is_reparse_or_link(str(resolved_raw)):
            raise PermissionError(f"Refusing to scan symlinked raw directory: {resolved_raw}")
        if resolved_raw.is_dir():
            for entry in sorted(os.listdir(resolved_raw)):
                if (
                    entry.startswith((".", "~"))
                    or entry.lower() in _SKIP_RAW_FILENAMES
                    or entry.lower() in _SKIP_RAW_DIRS
                ):
                    continue
                ext = os.path.splitext(entry)[1].lower()
                if ext not in {".md", ".txt"}:
                    continue
                safe_raw = safe_file_in_dir(str(resolved_raw), entry, allowed_exts={".md", ".txt"}, must_exist=True)
                if not safe_raw or _is_reparse_or_link(safe_raw) or not os.path.isfile(safe_raw):
                    continue
                all_items.extend(
                    _scan_file_action_items(
                        filepath=Path(safe_raw),
                        filename=entry,
                        source_kind="raw_capture",
                        max_bytes=MAX_NOTE_BYTES * 4,
                    )
                )

    filtered: list[dict[str, object]] = []
    for item in all_items:
        if not include_completed and bool(item["completed"]):
            continue
        if scope_tag:
            projs = list(item.get("projects") or [])
            if not projs or not all(bool(project_tags_match((str(p),), (scope_tag,))) for p in projs):
                continue
        if wanted_tag:
            tags_list = [str(t) for t in (item.get("tags") or [])]
            direct_hit = any(t.lstrip("#").casefold() == wanted_tag for t in tags_list)
            proj_hit = bool(
                wanted_proj_tag
                and any(
                    bool(project_tags_match((str(t),), (wanted_proj_tag,)))
                    for t in tags_list
                    if str(t).lower().startswith("project:")
                )
            )
            if not (direct_hit or proj_hit):
                continue
        filtered.append(item)
    return filtered


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Second Brain owner notes (zero-friction capture).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="Create a note (searchable immediately).")
    add.add_argument("pos_title", nargs="?", default=None, metavar="title", help="Note title (or pass --title).")
    add.add_argument("pos_body", nargs="?", default=None, metavar="body", help="Body text (or pass --body / --stdin).")
    add.add_argument("--title", dest="opt_title", default=None, help="Note title.")
    add.add_argument("--body", dest="opt_body", default=None, help="Markdown note body.")
    add.add_argument("--stdin", action="store_true", help="Read note body from stdin.")
    add.add_argument("--tag", "--tags", dest="tags", action="append", default=[], help="Tag or comma-separated tags (repeatable).")
    add.add_argument("--json", action="store_true", help="Emit JSON output.")
    s = sub.add_parser("search", help="Search notes.")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=8)
    s.add_argument("--project", default=None, help="Filter notes strictly to project:<slug>.")
    s.add_argument("--json", action="store_true", help="Emit JSON output.")
    ls = sub.add_parser("list", help="List notes.")
    ls.add_argument("--limit", type=int, default=50)
    ls.add_argument("--json", action="store_true", help="Emit JSON output.")
    reidx = sub.add_parser("reindex", help="Reconcile the notes index with disk.")
    reidx.add_argument("--json", action="store_true", help="Emit JSON output.")
    v = sub.add_parser("verify", help="Promote a note excerpt to a verified atom (owner action).")
    v.add_argument("filename", help="Working note filename in notes/ (e.g. my-note.md).")
    v.add_argument("pos_excerpt", nargs="?", default=None, metavar="excerpt", help="Exact excerpt to verify (or pass --excerpt).")
    v.add_argument("--excerpt", dest="opt_excerpt", default=None, help="Exact excerpt to verify.")
    v.add_argument("--actor", required=True, help="Human owner actor identity.")
    v.add_argument("--expected-sha256", dest="expected_sha256", default=None, help="Optional CAS SHA-256 of the note.")
    v.add_argument("--claim", default=None)
    v.add_argument("--memory-type", "--type", dest="memory_type", default="semantic", choices=["semantic", "episodic", "procedural", "self"])
    v.add_argument("--valid-from", default=None, help="Optional ISO-8601 start date/timestamp (YYYY-MM-DD).")
    v.add_argument("--valid-until", default=None, help="Optional ISO-8601 expiry date/timestamp (YYYY-MM-DD).")
    v.add_argument("--tag", "--tags", dest="tags", action="append", default=[], help="Tag or comma-separated tags (repeatable).")
    v.add_argument("--json", action="store_true", help="Emit JSON output.")
    t = sub.add_parser("tasks", help="Extract structured action items from working notes and raw captures.")
    t.add_argument("--include-completed", action="store_true", help="Include completed [x] tasks.")
    t.add_argument("--no-raw", action="store_true", help="Scan working notes only, skipping 0-raw/.")
    t.add_argument("--project", default=None, help="Filter action items strictly to project:<slug>.")
    t.add_argument("--tag", default=None, help="Filter action items by tag.")
    t.add_argument("--json", action="store_true", help="Emit JSON array of extracted action items.")
    args = parser.parse_args(argv)
    cli_paths = _active_cli_note_paths()

    if args.cmd == "add":
        if args.opt_title is not None:
            title = args.opt_title
            if args.opt_body is not None:
                if args.pos_title is not None or args.pos_body is not None:
                    add.error("Do not combine positional arguments with both --title and --body.")
                body = args.opt_body
            elif args.pos_title is not None and args.pos_body is None:
                body = args.pos_title
            elif args.stdin or not sys.stdin.isatty():
                body = sys.stdin.read()
            else:
                add.error("Provide --body, positional body text, or --stdin.")
        else:
            if args.pos_title is None:
                add.error("Provide a note title (positional or --title).")
            title = args.pos_title
            if args.opt_body is not None:
                if args.pos_body is not None:
                    add.error("Provide body either positionally or via --body, not both.")
                body = args.opt_body
            elif args.pos_body is not None:
                body = args.pos_body
            else:
                body = sys.stdin.read()
        print(json.dumps(create_note(title, body, tags=_split_cli_tags(args.tags), paths=cli_paths), indent=2, ensure_ascii=False))
    elif args.cmd == "search":
        print(
            json.dumps(
                [r.as_dict() for r in search_notes(args.query, limit=args.limit, project=args.project, paths=cli_paths)],
                indent=2,
                ensure_ascii=False,
            )
        )
    elif args.cmd == "list":
        items, total = list_notes(limit=args.limit, paths=cli_paths)
        print(json.dumps({"total": total, "items": [r.as_dict() for r in items]}, indent=2, ensure_ascii=False))
    elif args.cmd == "reindex":
        print(json.dumps(ensure_notes_index(paths=cli_paths), indent=2))
    elif args.cmd == "verify":
        if args.opt_excerpt is not None and args.pos_excerpt is not None:
            v.error("Provide excerpt either positionally or via --excerpt, not both.")
        excerpt = args.opt_excerpt if args.opt_excerpt is not None else args.pos_excerpt
        if not excerpt:
            v.error("Provide an excerpt to verify (positional or --excerpt).")
        expected_sha = args.expected_sha256 if args.expected_sha256 else get_note(args.filename, paths=cli_paths).sha256
        print(json.dumps(
            verify_note_excerpt(
                args.filename,
                excerpt,
                actor=args.actor,
                claim=args.claim,
                memory_type=args.memory_type,
                tags=_split_cli_tags(args.tags),
                valid_from=args.valid_from,
                valid_until=args.valid_until,
                expected_note_sha256=expected_sha,
                paths=cli_paths,
            ),
            indent=2,
            ensure_ascii=False,
        ))
    elif args.cmd == "tasks":
        items = extract_action_items(
            paths=cli_paths,
            include_completed=args.include_completed,
            include_raw=not args.no_raw,
            project=args.project,
            tag=args.tag,
        )
        if args.json:
            print(json.dumps(items, indent=2, ensure_ascii=False))
        elif not items:
            print("No matching action items.")
        else:
            for item in items:
                box = "[x]" if item["completed"] else "[ ]"
                meta_bits = []
                if item.get("priority"):
                    meta_bits.append(f"priority={item['priority']}")
                if item.get("due_date"):
                    meta_bits.append(f"due={item['due_date']}")
                if item.get("projects"):
                    meta_bits.append("projects=" + ",".join(str(p) for p in item["projects"]))
                suffix = f" ({'; '.join(meta_bits)})" if meta_bits else ""
                print(
                    f"{box} [{item['source_kind']}:{item['filename']}:{item['line_number']}] "
                    f"({item['marker']}) {item['text']}{suffix}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
