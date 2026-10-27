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
import hashlib
import os
import shutil
import sys
import uuid

from config import ATOMS_DIR, CHROMA_PATH, EMBED_MODEL, ensure_dirs, is_content_markdown
from index_manifest import build_index_manifest, write_index_manifest
from memory_format import is_memory_document
from ollama_utils import embed_text, embedding_metadata
from vector_store import close_vector_collection, reset_vector_collection
from review_atom import review_lock


def serialized_vector_rebuild(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if kwargs.pop("_writer_lock_held", False):
            return function(*args, **kwargs)
        atoms_dir = kwargs.get("atoms_dir", ATOMS_DIR)
        lock_path = os.path.join(os.path.dirname(os.path.abspath(atoms_dir)), ".sidecar", "writer.lock")
        with review_lock(lock_path):
            return function(*args, **kwargs)
    return wrapped


def atom_filenames(atoms_dir: str = ATOMS_DIR) -> list[str]:
    if not os.path.isdir(atoms_dir):
        return []
    return sorted(
        filename
        for filename in os.listdir(atoms_dir)
        if is_content_markdown(filename)
    )


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
        shutil.rmtree(path)


def activate_staged_store(staging_path: str, active_path: str) -> str | None:
    """Swap a complete staging store into place and retain the previous generation as a rollback backup."""
    active_path = os.path.abspath(active_path)
    staging_path = os.path.abspath(staging_path)
    backup_path = None

    if os.path.exists(active_path):
        backup_path = f"{active_path}.previous-{uuid.uuid4().hex}"
        os.replace(active_path, backup_path)

    try:
        os.replace(staging_path, active_path)
    except Exception:
        if backup_path and os.path.exists(backup_path) and not os.path.exists(active_path):
            os.replace(backup_path, active_path)
        raise
    return backup_path


@serialized_vector_rebuild
def rebuild_vectors(
    atoms_dir: str = ATOMS_DIR,
    store_path: str = CHROMA_PATH,
    collection_name: str = "atoms",
    model: str = EMBED_MODEL,
    require_valid_memory: bool = True,
    prefer_json: bool = False,
) -> dict:
    ensure_dirs()
    # Do not discard a working derived index until the configured embedder is proven healthy.
    preflight_embedding = embed_text("Second Brain embedding rebuild preflight.", model=model)
    embedding_dimensions = len(preflight_embedding)
    generation_id = uuid.uuid4().hex

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
                with open(path, "rb") as file:
                    content_bytes = file.read()
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
                    content_sha256=hashlib.sha256(content_bytes).hexdigest(),
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

        manifest = build_index_manifest(
            atoms_dir=atoms_dir,
            collection_name=collection_name,
            embedding_model=model,
            embedding_dimensions=embedding_dimensions,
            indexed_count=stats["indexed"],
            generation_id=generation_id,
        )
        write_index_manifest(staging_path, manifest)
        close_vector_collection(collection)
        collection = None
        stats["backup_path"] = activate_staged_store(staging_path, store_path)
        stats["committed"] = True
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
