# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""Integrity manifest for one complete, rebuildable vector-index generation."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
import threading
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import _is_reparse_or_link


INDEX_MANIFEST_FILENAME = "index-manifest.json"
INDEX_SCHEMA_VERSION = 2
VECTOR_DISTANCE_METRIC = "cosine"
LARGE_VAULT_STAT_CACHE_THRESHOLD = 64

_STAT_HASH_LOCK = threading.Lock()
_STAT_HASH_CACHE: dict[str, tuple[int, int, int, str]] = {}
_MAX_STAT_CACHE_ENTRIES = 50_000
# abs_dir -> (stat signature tuple, revisions list) — whole-directory memo for large vaults
_DIR_MEMO: dict[str, tuple[tuple, list[dict[str, str]]]] = {}
# id(revisions) -> (revisions, fingerprint)
_FP_MEMO: dict[int, tuple[list[dict[str, str]], str]] = {}


def invalidate_atom_revision_cache(path: str | None = None) -> None:
    """Clear cached stat->sha256 entries (all or one specific file)."""
    with _STAT_HASH_LOCK:
        _DIR_MEMO.clear()
        if path is None:
            _STAT_HASH_CACHE.clear()
            _FP_MEMO.clear()
        else:
            _STAT_HASH_CACHE.pop(os.path.abspath(path), None)


def record_known_atom_hash(path: str, content_sha256: str) -> None:
    """Update stat->sha256 cache immediately after an atomic write."""
    abs_path = os.path.abspath(path)
    parent_dir = os.path.abspath(os.path.dirname(abs_path))
    try:
        st = os.stat(abs_path)
    except OSError:
        invalidate_atom_revision_cache(abs_path)
        return
    with _STAT_HASH_LOCK:
        _DIR_MEMO.pop(parent_dir, None)
        if len(_STAT_HASH_CACHE) >= _MAX_STAT_CACHE_ENTRIES:
            _STAT_HASH_CACHE.clear()
        _STAT_HASH_CACHE[abs_path] = (st.st_mtime_ns, st.st_ctime_ns, st.st_size, content_sha256)


def atom_revisions(atoms_dir: str) -> list[dict[str, str]]:
    """Return the deterministic filename/hash set for indexable Markdown atoms.

    Uses os.scandir() (zero extra per-file Win32 stat syscalls) and stat-backed
    SHA-256 caching on vaults above LARGE_VAULT_STAT_CACHE_THRESHOLD so 5,000+
    note checks complete in a few milliseconds while tiny test vaults hash raw
    bytes unconditionally on every call.
    """
    if not os.path.isdir(atoms_dir):
        return []

    abs_dir = os.path.abspath(atoms_dir)
    entries: list[os.DirEntry[str]] = []
    try:
        with os.scandir(abs_dir) as it:
            for entry in it:
                name = entry.name
                if (
                    name.endswith(".md")
                    and name != "README.md"
                    and entry.is_file(follow_symlinks=False)
                    and not entry.is_symlink()
                    and not _is_reparse_or_link(entry.path)
                ):
                    entries.append(entry)
    except OSError as exc:
        raise RuntimeError(f"Could not scan atoms directory {atoms_dir}: {exc}") from exc

    entries.sort(key=lambda e: e.name)
    use_stat_cache = len(entries) > LARGE_VAULT_STAT_CACHE_THRESHOLD
    revisions: list[dict[str, str]] = []

    if use_stat_cache:
        try:
            sigs = tuple((e.name, *_entry_sig(e)) for e in entries)
        except OSError as exc:
            raise RuntimeError(f"Could not stat atoms in {atoms_dir}: {exc}") from exc
        with _STAT_HASH_LOCK:
            memo = _DIR_MEMO.get(abs_dir)
        if memo is not None and memo[0] == sigs:
            return memo[1]

    for index, entry in enumerate(entries):
        filename = entry.name
        abs_path = entry.path
        if use_stat_cache:
            sig = sigs[index][1:]
            with _STAT_HASH_LOCK:
                cached = _STAT_HASH_CACHE.get(abs_path)
            if cached is not None and cached[:3] == sig:
                revisions.append({"filename": filename, "content_sha256": cached[3]})
                continue
            try:
                with open(abs_path, "rb") as file:
                    digest = hashlib.sha256(file.read()).hexdigest()
            except OSError as exc:
                raise RuntimeError(f"Could not read atom revision {filename}: {exc}") from exc
            with _STAT_HASH_LOCK:
                if len(_STAT_HASH_CACHE) >= _MAX_STAT_CACHE_ENTRIES:
                    _STAT_HASH_CACHE.clear()
                _STAT_HASH_CACHE[abs_path] = (*sig, digest)
            revisions.append({"filename": filename, "content_sha256": digest})
        else:
            try:
                with open(abs_path, "rb") as file:
                    content = file.read()
            except OSError as exc:
                raise RuntimeError(f"Could not read atom revision {filename}: {exc}") from exc
            digest = hashlib.sha256(content).hexdigest()
            revisions.append({"filename": filename, "content_sha256": digest})
    if use_stat_cache:
        with _STAT_HASH_LOCK:
            if len(_DIR_MEMO) > 64:
                _DIR_MEMO.clear()
            _DIR_MEMO[abs_dir] = (sigs, revisions)
    return revisions


def _entry_sig(entry: os.DirEntry[str]) -> tuple[int, int, int]:
    st = entry.stat(follow_symlinks=False)
    return (st.st_mtime_ns, st.st_ctime_ns, st.st_size)


def revisions_fingerprint(revisions: list[dict[str, str]]) -> str:
    with _STAT_HASH_LOCK:
        memo = _FP_MEMO.get(id(revisions))
    if memo is not None and memo[0] is revisions:
        return memo[1]
    encoded = json.dumps(revisions, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    if len(revisions) > LARGE_VAULT_STAT_CACHE_THRESHOLD:
        with _STAT_HASH_LOCK:
            if len(_FP_MEMO) > 64:
                _FP_MEMO.clear()
            # Holding a strong ref keeps id() stable; memoized lists are treated as immutable.
            _FP_MEMO[id(revisions)] = (revisions, digest)
    return digest


def build_index_manifest(
    atoms_dir: str,
    collection_name: str,
    embedding_model: str,
    embedding_dimensions: int,
    indexed_count: int,
    generation_id: str | None = None,
    skipped_count: int = 0,
) -> dict:
    revisions = atom_revisions(atoms_dir)
    manifest = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "generation_id": generation_id or uuid.uuid4().hex,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "collection_name": collection_name,
        "embedding_model": embedding_model,
        "embedding_dimensions": embedding_dimensions,
        "distance_metric": VECTOR_DISTANCE_METRIC,
        "indexed_count": indexed_count,
        "atom_count": len(revisions),
        "atom_revisions_sha256": revisions_fingerprint(revisions),
    }
    if skipped_count:
        manifest["skipped_count"] = int(skipped_count)
    return manifest


def index_manifest_path(store_path: str) -> str:
    return os.path.join(store_path, INDEX_MANIFEST_FILENAME)


def write_index_manifest(store_path: str, manifest: dict):
    """Atomically write the manifest into a staged or active index directory."""
    os.makedirs(store_path, exist_ok=True)
    destination = index_manifest_path(store_path)
    descriptor, temporary_path = tempfile.mkstemp(prefix=".index-manifest-", suffix=".tmp", dir=store_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(manifest, file, ensure_ascii=False, sort_keys=True, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, destination)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


def load_index_manifest(store_path: str) -> dict | None:
    try:
        with open(index_manifest_path(store_path), "r", encoding="utf-8") as file:
            manifest = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None
    return manifest if isinstance(manifest, dict) else None


def index_manifest_is_current(manifest: dict | None, atoms_dir: str) -> bool:
    """True only when the current canonical atom revisions equal the indexed generation."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != INDEX_SCHEMA_VERSION:
        return False
    revisions = atom_revisions(atoms_dir)
    indexed = manifest.get("indexed_count")
    skipped = manifest.get("skipped_count", 0)
    if not isinstance(indexed, int) or not isinstance(skipped, int) or skipped < 0:
        return False
    return (
        manifest.get("atom_count") == len(revisions)
        and (indexed + skipped) == len(revisions)
        and manifest.get("atom_revisions_sha256") == revisions_fingerprint(revisions)
    )
