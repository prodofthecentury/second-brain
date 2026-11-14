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

# ─── Root Paths ──────────────────────────────────────────────────────────────
SCRIPTS_DIR  = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR     = os.path.dirname(SCRIPTS_DIR)
BRAIN_DIR    = os.path.abspath(os.environ.get("BRAIN_VAULT_DIR", os.path.join(ROOT_DIR, "brain")))

# ─── Vault Folders ───────────────────────────────────────────────────────────
RAW_DIR      = os.path.join(BRAIN_DIR, "0-raw")
INBOX_DIR    = os.path.join(RAW_DIR, "inbox")
DESK_DIR     = os.path.join(BRAIN_DIR, "1-desk")
ATOMS_DIR    = os.path.join(BRAIN_DIR, "2-atoms")
THREADS_DIR  = os.path.join(BRAIN_DIR, "3-threads")
SOURCES_DIR  = os.path.join(BRAIN_DIR, "sources")
BRIEFINGS_DIR= os.path.join(BRAIN_DIR, "briefings")

# ─── Special Files ───────────────────────────────────────────────────────────
READING_LIST = os.path.join(RAW_DIR, "reading-list.md")
HOUSE_RULES  = os.path.join(BRAIN_DIR, "house-rules.md")
CHROMA_PATH  = os.path.join(BRAIN_DIR, ".chroma")
SIDECAR_DIR  = os.path.join(BRAIN_DIR, ".sidecar")
LEXICAL_INDEX_PATH = os.path.join(SIDECAR_DIR, "lexical.sqlite3")
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
    for d in [RAW_DIR, INBOX_DIR, DESK_DIR, ATOMS_DIR, THREADS_DIR, SOURCES_DIR, BRIEFINGS_DIR, CHROMA_PATH, SIDECAR_DIR]:
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

def resolve_under(base_dir: str, relative_path: str, must_exist: bool = True) -> str | None:
    """Resolve a relative path under a base directory, or return None."""
    if not relative_path:
        return None

    rel_path = str(relative_path).strip().replace("\\", os.sep).replace("/", os.sep)
    if os.path.isabs(rel_path):
        return None

    base = os.path.abspath(base_dir)
    candidate = os.path.abspath(os.path.join(base, rel_path))

    try:
        if os.path.commonpath([base, candidate]) != base:
            return None
    except ValueError:
        return None

    if must_exist and not os.path.exists(candidate):
        return None
    return candidate

def safe_file_in_dir(
    directory: str,
    filename: str,
    allowed_exts: set[str] | None = None,
    must_exist: bool = True,
) -> str | None:
    """Resolve a single filename inside a directory, rejecting subpaths."""
    name = str(filename or "").strip().replace("\\", "/")
    if not name or "/" in name or os.path.isabs(name):
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
