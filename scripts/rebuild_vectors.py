# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = [
#     "chromadb",
#     "ollama",
# ]
# ///
"""
Rebuild the vector recall layer from Markdown atoms.

Markdown files in brain/2-atoms are the durable source of truth. This command
resets the rebuildable vector collection and re-indexes those atom files.
"""

from __future__ import annotations

import argparse
from functools import wraps
import gc
import hashlib
import os
import shutil
import sys
import time
import uuid

from config import (
    ATOMS_DIR,
    CHROMA_PATH,
    EMBED_MODEL,
    _is_reparse_or_link,
    ensure_dirs,
    is_content_markdown,
    safe_file_in_dir,
)
from index_manifest import atom_revisions, build_index_manifest, write_index_manifest
from memory_format import is_memory_document
from ollama_utils import embed_text, embedding_metadata
from vector_store import close_vector_collection, reset_vector_collection
from review_atom import review_lock


def serialized_vector_rebuild(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if kwargs.pop("_writer_lock_held", False):
            return function(*args, _writer_lock_held=True, **kwargs)
        atoms_dir = kwargs.get("atoms_dir", ATOMS_DIR)
        lock_path = os.path.join(
            os.path.dirname(os.path.abspath(atoms_dir)),
            ".sidecar",
            "vector-rebuild.lock",
        )
        with review_lock(lock_path):
            return function(*args, _writer_lock_held=False, **kwargs)

    return wrapped


def atom_filenames(atoms_dir: str = ATOMS_DIR) -> list[str]:
    if not os.path.isdir(atoms_dir) or _is_reparse_or_link(atoms_dir):
        return []
    filenames: list[str] = []
    for filename in sorted(os.listdir(atoms_dir)):
        if not is_content_markdown(filename):
            continue
        path = safe_file_in_dir(atoms_dir, filename, allowed_exts={".md"})
        if not path or _is_reparse_or_link(path) or not os.path.isfile(path):
            continue
        filenames.append(filename)
    return filenames


def rebuild_staging_path(store_path: str) -> str:
    """Return an isolated sibling directory for a vector rebuild generation."""
    active_path = os.path.abspath(store_path)
    parent = os.path.dirname(active_path)
    name = os.path.basename(active_path)
    os.makedirs(parent, exist_ok=True)
    return os.path.join(parent, f".{name}.rebuild-{uuid.uuid4().hex}")


def remove_staging_store(path: str):
    """Remove only a staging directory created for the current rebuild attempt."""
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def prune_previous_generations(active_path: str, keep: int = 2) -> None:
    """Retain at most `keep` `.previous-*` rollback directories alongside `active_path`."""
    active_abs = os.path.abspath(active_path)
    parent = os.path.dirname(active_abs)
    prefix = f"{os.path.basename(active_abs)}.previous-"
    if not os.path.isdir(parent):
        return
    backups = []
    for entry in os.listdir(parent):
        if entry.startswith(prefix):
            full = os.path.join(parent, entry)
            if os.path.isdir(full):
                try:
                    backups.append((os.path.getmtime(full), full))
                except OSError:
                    continue
    backups.sort(key=lambda item: item[0], reverse=True)
    for _, old_dir in backups[keep:]:
        shutil.rmtree(old_dir, ignore_errors=True)


def _replace_dir_with_retry(src: str, dst: str) -> None:
    """Replace a directory, tolerating brief Windows handle-release delays on newly closed stores."""
    for delay in (0.05, 0.1, 0.2, 0.3, 0.5):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            gc.collect()
            time.sleep(delay)
    os.replace(src, dst)


def activate_staged_store(staging_path: str, active_path: str) -> str | None:
    """Swap a complete staging store into place and retain the previous generation as a rollback backup."""
    active_path = os.path.abspath(active_path)
    staging_path = os.path.abspath(staging_path)
    backup_path = None

    if os.path.exists(active_path):
        backup_path = f"{active_path}.previous-{uuid.uuid4().hex}"
        _replace_dir_with_retry(active_path, backup_path)

    try:
        _replace_dir_with_retry(staging_path, active_path)
    except Exception:
        if backup_path and os.path.exists(backup_path) and not os.path.exists(active_path):
            _replace_dir_with_retry(backup_path, active_path)
        raise
    prune_previous_generations(active_path, keep=2)
    return backup_path


@serialized_vector_rebuild
def rebuild_vectors(
    atoms_dir: str = ATOMS_DIR,
    store_path: str = CHROMA_PATH,
    collection_name: str = "atoms",
    model: str = EMBED_MODEL,
    require_valid_memory: bool = True,
    prefer_json: bool = False,
    *,
    _writer_lock_held: bool = False,
) -> dict:
    ensure_dirs()
    # Do not discard a working derived index until the configured embedder is proven healthy.
    preflight_embedding = embed_text("Second Brain embedding rebuild preflight.", model=model)
    embedding_dimensions = len(preflight_embedding)
    generation_id = uuid.uuid4().hex
    revisions_before = atom_revisions(atoms_dir)
    revision_sha_by_file = {r["filename"]: r["content_sha256"] for r in revisions_before}

    stats = {
        "indexed": 0,
        "skipped": 0,
        "failed": 0,
        "collection": collection_name,
        "store_path": store_path,
        "committed": False,
        "backup_path": None,
        "generation_id": generation_id,
    }
    staging_path = rebuild_staging_path(store_path)
    collection = None

    try:
        collection = reset_vector_collection(
            staging_path,
            name=collection_name,
            announce=True,
            prefer_json=prefer_json,
        )

        for filename in atom_filenames(atoms_dir):
            path = os.path.join(atoms_dir, filename)
            try:
                if _is_reparse_or_link(path):
                    raise PermissionError(f"Refusing to index symlink or reparse point atom: {filename}")
                with open(path, "rb") as file:
                    content_bytes = file.read()
                content_sha = hashlib.sha256(content_bytes).hexdigest()
                if revision_sha_by_file.get(filename) != content_sha:
                    raise RuntimeError(
                        f"Canonical atom {filename} changed during vector rebuild; rerun rebuild after edits finish."
                    )
                content = content_bytes.decode("utf-8")

                if require_valid_memory and not is_memory_document(content):
                    stats["skipped"] += 1
                    print(f"[SKIP] {filename}: not a valid atom memory document")
                    continue

                embedding = embed_text(content, model=model)
                if len(embedding) != embedding_dimensions:
                    raise RuntimeError(
                        f"embedding dimensions changed from {embedding_dimensions} to {len(embedding)}"
                    )
                metadata = embedding_metadata(
                    filename,
                    content,
                    model,
                    embedding,
                    content_sha256=content_sha,
                )
                metadata["index_generation"] = generation_id
                collection.upsert(
                    documents=[content],
                    embeddings=[embedding],
                    metadatas=[metadata],
                    ids=[filename],
                )
                stats["indexed"] += 1
                print(f"[OK  ] {filename}")
            except Exception as exc:
                stats["failed"] += 1
                print(f"[FAIL] {filename}: {exc}")
                raise RuntimeError(
                    f"Vector rebuild stopped at {filename}; the active index was preserved: {exc}"
                ) from exc

        close_vector_collection(collection)
        collection = None

        def _commit_staged_generation() -> None:
            current_revisions = atom_revisions(atoms_dir)
            if current_revisions != revisions_before:
                raise RuntimeError(
                    "Canonical atoms changed while vector rebuild was in progress; the active index was preserved."
                )
            manifest = build_index_manifest(
                atoms_dir=atoms_dir,
                collection_name=collection_name,
                embedding_model=model,
                embedding_dimensions=embedding_dimensions,
                indexed_count=stats["indexed"],
                generation_id=generation_id,
                skipped_count=stats["skipped"],
            )
            write_index_manifest(staging_path, manifest)
            stats["backup_path"] = activate_staged_store(staging_path, store_path)
            stats["committed"] = True

        if _writer_lock_held:
            _commit_staged_generation()
        else:
            writer_lock_path = os.path.join(
                os.path.dirname(os.path.abspath(atoms_dir)),
                ".sidecar",
                "writer.lock",
            )
            with review_lock(writer_lock_path):
                _commit_staged_generation()
    except Exception:
        if collection is not None:
            try:
                close_vector_collection(collection)
            except Exception:
                pass
        remove_staging_store(staging_path)
        raise

    return stats


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Rebuild Second Brain vector recall from Markdown atoms.")
    parser.add_argument("--atoms-dir", default=ATOMS_DIR, help="Directory containing atom Markdown files.")
    parser.add_argument("--store-path", default=CHROMA_PATH, help="Vector store path to rebuild.")
    parser.add_argument("--collection", default="atoms", help="Vector collection name.")
    parser.add_argument("--model", default=EMBED_MODEL, help="Embedding model to use.")
    parser.add_argument(
        "--include-invalid",
        action="store_true",
        help="Index Markdown files even if they do not match the atom memory format.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Force the JSON vector fallback instead of ChromaDB.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    stats = rebuild_vectors(
        atoms_dir=args.atoms_dir,
        store_path=args.store_path,
        collection_name=args.collection,
        model=args.model,
        require_valid_memory=not args.include_invalid,
        prefer_json=args.json,
    )
    print(
        "VECTOR_REBUILD_COMPLETE "
        f"indexed={stats['indexed']} skipped={stats['skipped']} failed={stats['failed']}"
    )
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
