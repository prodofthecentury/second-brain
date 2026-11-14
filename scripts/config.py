# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""
Shared configuration and path utilities for all Second Brain scripts.
Import this in any script to get consistent paths and settings.
"""

import os
import datetime
import hashlib
import json
from pathlib import Path
import stat
import sys

# ─── Root Paths ──────────────────────────────────────────────────────────────
SCRIPTS_DIR  = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR     = os.path.dirname(SCRIPTS_DIR)


def _default_installed_vault() -> str | None:
    """Keep wheel-installed user data outside the immutable Python package."""
    if (Path(ROOT_DIR) / "pyproject.toml").exists():
        return None  # editable checkout: preserve the repository's brain/ path
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return str(Path(base) / "SecondBrain" / "brain")
    if sys.platform == "darwin":
        return str(Path.home() / "Library" / "Application Support" / "SecondBrain" / "brain")
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return str(Path(base) / "second-brain" / "brain")


BRAIN_DIR    = os.path.abspath(
    (os.environ.get("BRAIN_VAULT_DIR") or "").strip()
    or _default_installed_vault()
    or os.path.join(ROOT_DIR, "brain")
)

# ─── Vault Folders ───────────────────────────────────────────────────────────
RAW_DIR      = os.path.join(BRAIN_DIR, "0-raw")
INBOX_DIR    = os.path.join(RAW_DIR, "inbox")
DESK_DIR     = os.path.join(BRAIN_DIR, "1-desk")
ATOMS_DIR    = os.path.join(BRAIN_DIR, "2-atoms")
THREADS_DIR  = os.path.join(BRAIN_DIR, "3-threads")
EXPERIENCES_DIR = os.path.join(BRAIN_DIR, "4-experiences")
PLAYBOOKS_DIR = os.path.join(BRAIN_DIR, "5-playbooks")
SOURCES_DIR  = os.path.join(BRAIN_DIR, "sources")
BRIEFINGS_DIR= os.path.join(BRAIN_DIR, "briefings")
# Owner knowledge base: zero-friction, immediately searchable, never treated as verified claims.
NOTES_DIR    = os.path.join(BRAIN_DIR, "notes")
# Immutable note snapshots used as evidence sources for one-click verified atoms.
NOTE_SNAPSHOTS_DIR = os.path.join(RAW_DIR, "note-snapshots")

# ─── Special Files ───────────────────────────────────────────────────────────
READING_LIST = os.path.join(RAW_DIR, "reading-list.md")
HOUSE_RULES  = os.path.join(BRAIN_DIR, "house-rules.md")
CHROMA_PATH  = os.path.join(BRAIN_DIR, ".chroma")
SIDECAR_DIR  = os.path.join(BRAIN_DIR, ".sidecar")
LEXICAL_INDEX_PATH = os.path.join(SIDECAR_DIR, "lexical.sqlite3")
NOTES_INDEX_PATH   = os.path.join(SIDECAR_DIR, "notes.sqlite3")
TELEMETRY_DB_PATH  = os.path.join(SIDECAR_DIR, "telemetry.sqlite3")
ARCHIVE_MANIFEST = os.path.join(RAW_DIR, "archive", "manifest.jsonl")

# ─── Archive ─────────────────────────────────────────────────────────────────
def archive_dir():
    """Returns the current month's archive directory, creating it if needed."""
    path = os.path.join(RAW_DIR, "archive", datetime.datetime.now().strftime("%Y-%m"))
    os.makedirs(path, exist_ok=True)
    return path

# ─── Models ──────────────────────────────────────────────────────────────────
GEN_MODEL    = os.environ.get("BRAIN_GEN_MODEL",   "llama3.2:3b")
EMBED_MODEL  = os.environ.get("BRAIN_EMBED_MODEL", "nomic-embed-text")
VISION_MODEL = os.environ.get("BRAIN_VISION_MODEL","llava")
WHISPER_SIZE = os.environ.get("BRAIN_WHISPER_SIZE","base")   # tiny/base/small/medium/large

# ─── Supported File Types ────────────────────────────────────────────────────
AUDIO_EXTS   = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".webm"}
IMAGE_EXTS   = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
TEXT_EXTS    = {".md", ".txt"}
VAULT_README = "README.md"

# ─── Helpers ─────────────────────────────────────────────────────────────────
def ensure_dirs():
    """Create all vault directories if they don't exist."""
    for d in [RAW_DIR, INBOX_DIR, DESK_DIR, ATOMS_DIR, THREADS_DIR, EXPERIENCES_DIR, PLAYBOOKS_DIR, SOURCES_DIR, BRIEFINGS_DIR, NOTES_DIR, NOTE_SNAPSHOTS_DIR, CHROMA_PATH, SIDECAR_DIR]:
        os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.dirname(ARCHIVE_MANIFEST), exist_ok=True)

def vault_relative_path(path: str) -> str:
    """Return a brain-relative path when possible, otherwise an absolute path."""
    absolute = os.path.abspath(path)
    brain = os.path.abspath(BRAIN_DIR)
    try:
        if os.path.commonpath([brain, absolute]) == brain:
            return os.path.relpath(absolute, brain).replace("\\", "/")
    except ValueError:
        pass
    return absolute

def build_archive_manifest_entry(
    original_path: str,
    archived_path: str,
    source_filename: str,
    generated_atoms: list[str],
    processed_at: str | None = None,
    origin: str = "user_local",
    source_sha256: str | None = None,
    archived_sha256: str | None = None,
) -> dict:
    """Build an auditable manifest entry for one archived source capture."""
    return {
        "schema_version": 2,
        "processed_at": processed_at or datetime.datetime.now().isoformat(timespec="seconds"),
        "source_filename": os.path.basename(source_filename),
        "original_path": vault_relative_path(original_path),
        "archived_path": vault_relative_path(archived_path),
        "origin": origin,
        "source_sha256": source_sha256 or file_sha256(original_path),
        "archived_sha256": archived_sha256 or file_sha256(archived_path),
        "generated_atoms": list(generated_atoms or []),
    }


def file_sha256(path: str) -> str | None:
    """Return a stable hash for one evidence file without treating a missing file as valid evidence."""
    try:
        with open(path, "rb") as file:
            digest = hashlib.sha256()
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()

def append_archive_manifest_entry(entry: dict, manifest_path: str = ARCHIVE_MANIFEST):
    """Append one JSONL archive manifest entry."""
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    with open(manifest_path, "a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

def normalize_markdown_filename(value: str) -> str:
    """Return a markdown filename, preserving the caller's basename."""
    filename = str(value or "").strip()
    if not filename:
        return ""
    if not filename.lower().endswith(".md"):
        filename += ".md"
    return filename

_WINDOWS_RESERVED_STEMS = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
})


def _is_reparse_or_link(path: str) -> bool:
    """Return True if path itself (without following) is a symlink, junction, or reparse point."""
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if callable(isjunction) and isjunction(path):
        return True
    if os.name == "nt":
        try:
            attrs = getattr(os.lstat(path), "st_file_attributes", 0)
            if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                return True
        except OSError:
            return False
    return False


def resolve_under(base_dir: str, relative_path: str, must_exist: bool = True) -> str | None:
    """Resolve a relative path under a base directory, or return None."""
    if not relative_path:
        return None

    raw = str(relative_path).strip()
    if not raw or ":" in raw or "\x00" in raw or raw.startswith(("/", "\\")):
        return None

    rel_path = raw.replace("\\", os.sep).replace("/", os.sep)
    if os.path.isabs(rel_path):
        return None

    for segment in rel_path.split(os.sep):
        if not segment:
            continue
        seg_stem = os.path.splitext(segment)[0].rstrip(" .").lower()
        seg_first = segment.split(".", 1)[0].rstrip(" .").lower()
        if seg_stem in _WINDOWS_RESERVED_STEMS or seg_first in _WINDOWS_RESERVED_STEMS:
            return None

    try:
        base = os.path.realpath(base_dir)
        lexical = os.path.abspath(os.path.join(base, rel_path))
        candidate = os.path.realpath(lexical)
        if os.path.normcase(os.path.commonpath([base, lexical])) != os.path.normcase(base):
            return None
        if os.path.normcase(os.path.commonpath([base, candidate])) != os.path.normcase(base):
            return None
    except (OSError, ValueError):
        return None

    if must_exist:
        if not os.path.exists(candidate):
            return None
    else:
        if os.path.lexists(lexical) and _is_reparse_or_link(lexical):
            return None
        current = lexical
        while os.path.normcase(current) != os.path.normcase(base):
            if os.path.lexists(current) and _is_reparse_or_link(current):
                return None
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent

    return candidate

def safe_file_in_dir(
    directory: str,
    filename: str,
    allowed_exts: set[str] | None = None,
    must_exist: bool = True,
) -> str | None:
    """Resolve a single filename inside a directory, rejecting subpaths."""
    name = str(filename or "").strip().replace("\\", "/")
    if not name or name in {".", ".."} or "/" in name or ":" in name or "\x00" in name or os.path.isabs(name):
        return None

    stem = os.path.splitext(name)[0].rstrip(" .").lower()
    first_stem = name.split(".", 1)[0].rstrip(" .").lower()
    if stem in _WINDOWS_RESERVED_STEMS or first_stem in _WINDOWS_RESERVED_STEMS:
        return None

    if allowed_exts is not None:
        allowed = {ext.lower() for ext in allowed_exts}
        if os.path.splitext(name)[1].lower() not in allowed:
            return None

    return resolve_under(directory, name, must_exist=must_exist)

def safe_vault_reference(
    relative_path: str,
    allowed_exts: set[str] | None = None,
    must_exist: bool = True,
) -> str | None:
    """Resolve a relative vault reference under brain/."""
    rel_path = str(relative_path or "").strip()
    if allowed_exts is not None:
        allowed = {ext.lower() for ext in allowed_exts}
        if os.path.splitext(rel_path)[1].lower() not in allowed:
            return None
    return resolve_under(BRAIN_DIR, rel_path, must_exist=must_exist)

def is_content_markdown(filename: str) -> bool:
    """Return True for vault content notes, excluding directory documentation."""
    return filename.endswith(".md") and filename != VAULT_README

def read_house_rules() -> str:
    with open(HOUSE_RULES, "r", encoding="utf-8") as f:
        return f.read()

def today() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")

def now_slug() -> str:
    return datetime.datetime.now().strftime("%Y-%m%d-%H%M%S")
