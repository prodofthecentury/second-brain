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
import tempfile
import uuid


INDEX_MANIFEST_FILENAME = "index-manifest.json"
INDEX_SCHEMA_VERSION = 2
VECTOR_DISTANCE_METRIC = "cosine"


def atom_revisions(atoms_dir: str) -> list[dict[str, str]]:
    """Return the deterministic filename/hash set for indexable Markdown atoms."""
    if not os.path.isdir(atoms_dir):
        return []

    revisions = []
    for filename in sorted(os.listdir(atoms_dir)):
        if not filename.endswith(".md") or filename == "README.md":
            continue
        path = os.path.join(atoms_dir, filename)
        try:
            with open(path, "rb") as file:
                content = file.read()
        except OSError as exc:
            raise RuntimeError(f"Could not read atom revision {filename}: {exc}") from exc
        revisions.append(
            {
                "filename": filename,
                "content_sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return revisions


def revisions_fingerprint(revisions: list[dict[str, str]]) -> str:
    encoded = json.dumps(revisions, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_index_manifest(
    atoms_dir: str,
    collection_name: str,
    embedding_model: str,
    embedding_dimensions: int,
    indexed_count: int,
    generation_id: str | None = None,
) -> dict:
    revisions = atom_revisions(atoms_dir)
    return {
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
    return (
        manifest.get("atom_count") == len(revisions)
        and manifest.get("atom_revisions_sha256") == revisions_fingerprint(revisions)
    )
