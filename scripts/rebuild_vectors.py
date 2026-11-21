# /// script
# requires-python = ">=3.11,<3.14"
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
from collections.abc import Sequence
from contextlib import nullcontext
from functools import wraps
import gc
import hashlib
import math
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
from index_manifest import (
    INDEX_SCHEMA_VERSION,
    VECTOR_DISTANCE_METRIC,
    atom_revisions,
    build_index_manifest,
    load_index_manifest,
    write_index_manifest,
)
from memory_format import is_memory_document
from ollama_utils import embed_text, embedding_metadata
from vector_store import close_vector_collection, get_vector_collection, reset_vector_collection
from review_atom import review_lock

UPSERT_BATCH_SIZE = 128


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


def _load_reusable_vectors(
    store_path: str,
    collection_name: str,
    model: str,
    embedding_dimensions: int,
    revision_sha_by_file: dict[str, str],
) -> dict[str, list[float]]:
    """Read unchanged (content_sha256, embedding_model) vectors from the active store and close all handles."""
    if not os.path.isdir(store_path) or _is_reparse_or_link(store_path) or not revision_sha_by_file:
        return {}

    reusable: dict[str, list[float]] = {}
    prev_col = None
    try:
        prev_col = get_vector_collection(store_path, name=collection_name, create=False)
        payload = prev_col.get(include=["embeddings", "metadatas"])
        if isinstance(payload, dict):
            raw_ids = payload.get("ids")
            raw_embs = payload.get("embeddings")
            raw_metas = payload.get("metadatas")
            if raw_ids is not None and raw_embs is not None and raw_metas is not None:
                for idx, doc_id in enumerate(raw_ids):
                    if idx >= len(raw_embs) or idx >= len(raw_metas):
                        break
                    meta = raw_metas[idx]
                    emb = raw_embs[idx]
                    if not isinstance(doc_id, str) or not isinstance(meta, dict) or emb is None:
                        continue
                    expected_sha = revision_sha_by_file.get(doc_id)
                    if not expected_sha:
                        continue
                    if meta.get("content_sha256") != expected_sha or meta.get("embedding_model") != model:
                        continue
                    try:
                        vec = [float(x) for x in emb]
                    except (TypeError, ValueError):
                        continue
                    if (
                        len(vec) == embedding_dimensions
                        and all(math.isfinite(x) for x in vec)
                        and any(x != 0.0 for x in vec)
                    ):
                        reusable[doc_id] = vec
        payload = None
    except Exception:
        # Never block a clean full rebuild if the previous store is uninitialized or unreadable.
        reusable = {}
    finally:
        if prev_col is not None:
            try:
                close_vector_collection(prev_col)
            except Exception:
                pass
            prev_col = None
            gc.collect()
    return reusable


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
    if os.path.abspath(atoms_dir) == os.path.abspath(ATOMS_DIR) and os.path.abspath(store_path) == os.path.abspath(CHROMA_PATH):
        ensure_dirs()
    # Do not discard a working derived index until the configured embedder is proven healthy.
    preflight_embedding = embed_text("Second Brain vector rebuild preflight.", model=model)
    embedding_dimensions = len(preflight_embedding)
    generation_id = uuid.uuid4().hex
    revisions_before = atom_revisions(atoms_dir)
    revision_sha_by_file = {r["filename"]: r["content_sha256"] for r in revisions_before}

    reusable_vectors = _load_reusable_vectors(
        store_path=store_path,
        collection_name=collection_name,
        model=model,
        embedding_dimensions=embedding_dimensions,
        revision_sha_by_file=revision_sha_by_file,
    )

    reused_count = 0
    embedded_count = 0
    stats = {
        "indexed": 0,
        "reused": 0,
        "embedded": 0,
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

        batch_fn = getattr(collection, "batch", None)
        batch_ctx = batch_fn() if callable(batch_fn) else nullcontext()

        batch_ids: list[str] = []
        batch_docs: list[str] = []
        batch_embs: list[list[float]] = []
        batch_metas: list[dict] = []

        def _flush_upsert_batch(last_file: str) -> None:
            if not batch_ids:
                return
            try:
                collection.upsert(
                    documents=list(batch_docs),
                    embeddings=list(batch_embs),
                    metadatas=list(batch_metas),
                    ids=list(batch_ids),
                )
                batch_ids.clear()
                batch_docs.clear()
                batch_embs.clear()
                batch_metas.clear()
            except Exception as exc:
                stats["failed"] += 1
                print(f"[FAIL] {last_file}: {exc}")
                raise RuntimeError(
                    f"Vector rebuild stopped at {last_file}; the active index was preserved: {exc}"
                ) from exc

        with batch_ctx:
            last_seen_filename = "<batch>"
            for filename in atom_filenames(atoms_dir):
                last_seen_filename = filename
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

                    carried = reusable_vectors.get(filename)
                    if carried is not None:
                        embedding = carried
                        reused_count += 1
                        status_tag = "REUSE"
                    else:
                        embedding = embed_text(content, model=model)
                        if len(embedding) != embedding_dimensions:
                            raise RuntimeError(
                                f"embedding dimensions changed from {embedding_dimensions} to {len(embedding)}"
                            )
                        embedded_count += 1
                        status_tag = "OK  "

                    metadata = embedding_metadata(
                        filename,
                        content,
                        model,
                        embedding,
                        content_sha256=content_sha,
                    )
                    metadata["index_generation"] = generation_id

                    batch_ids.append(filename)
                    batch_docs.append(content)
                    batch_embs.append(embedding)
                    batch_metas.append(metadata)
                    print(f"[{status_tag}] {filename}")
                except Exception as exc:
                    stats["failed"] += 1
                    print(f"[FAIL] {filename}: {exc}")
                    raise RuntimeError(
                        f"Vector rebuild stopped at {filename}; the active index was preserved: {exc}"
                    ) from exc

                if len(batch_ids) >= UPSERT_BATCH_SIZE:
                    _flush_upsert_batch(filename)

            _flush_upsert_batch(last_seen_filename)

        stats["reused"] = reused_count
        stats["embedded"] = embedded_count
        stats["indexed"] = reused_count + embedded_count

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


def upsert_changed_vectors(
    atoms_dir: str = ATOMS_DIR,
    store_path: str = CHROMA_PATH,
    collection_name: str = "atoms",
    model: str | None = None,
    *,
    changed_filenames: Sequence[str] = (),
    deleted_filenames: Sequence[str] = (),
    require_valid_memory: bool = True,
    pre_verified_current: bool = False,
    prefer_json: bool = False,
    _writer_lock_held: bool = False,
) -> dict:
    """Perform an O(|changed|) in-place Chroma/JSON vector upsert + manifest refresh.

    Serializes all live ``.chroma`` mutations strictly under ``writer.lock`` (avoiding AB/BA
    lock inversion with ``vector-rebuild.lock``). Falls back to ``rebuild_vectors`` when no
    compatible active cosine manifest exists yet.
    """
    effective_model = (model or EMBED_MODEL).strip() or EMBED_MODEL
    if _is_reparse_or_link(atoms_dir) or (os.path.lexists(store_path) and _is_reparse_or_link(store_path)):
        raise PermissionError("Refusing to update vector store through a symlink or reparse point.")

    def _is_compatible(m: dict | None) -> bool:
        return (
            isinstance(m, dict)
            and m.get("schema_version") == INDEX_SCHEMA_VERSION
            and m.get("distance_metric") == VECTOR_DISTANCE_METRIC
            and m.get("collection_name") == collection_name
            and m.get("embedding_model") == effective_model
            and isinstance(m.get("embedding_dimensions"), int)
            and int(m["embedding_dimensions"]) > 0
            and isinstance(m.get("generation_id"), str)
            and bool(str(m["generation_id"]).strip())
        )

    initial_manifest = load_index_manifest(store_path) if os.path.isdir(store_path) else None
    if not _is_compatible(initial_manifest):
        return rebuild_vectors(
            atoms_dir=atoms_dir,
            store_path=store_path,
            collection_name=collection_name,
            model=effective_model,
            require_valid_memory=require_valid_memory,
            prefer_json=prefer_json,
            _writer_lock_held=_writer_lock_held,
        )

    writer_lock_path = os.path.join(
        os.path.dirname(os.path.abspath(atoms_dir)),
        ".sidecar",
        "writer.lock",
    )
    lock_ctx = nullcontext() if _writer_lock_held else review_lock(writer_lock_path)
    with lock_ctx:
        manifest = load_index_manifest(store_path) if os.path.isdir(store_path) else None
        if not _is_compatible(manifest):
            return rebuild_vectors(
                atoms_dir=atoms_dir,
                store_path=store_path,
                collection_name=collection_name,
                model=effective_model,
                require_valid_memory=require_valid_memory,
                prefer_json=prefer_json,
                _writer_lock_held=True,
            )

        assert isinstance(manifest, dict)
        generation_id = str(manifest["generation_id"])
        expected_dims = int(manifest["embedding_dimensions"])
        revisions_before = atom_revisions(atoms_dir)
        rev_sha_by_file = {r["filename"]: r["content_sha256"] for r in revisions_before}

        target_changed: set[str] = {str(f).strip() for f in (changed_filenames or ()) if str(f or "").strip()}
        target_deleted: set[str] = {str(f).strip() for f in (deleted_filenames or ()) if str(f or "").strip()}

        for fname in list(target_changed):
            if fname not in rev_sha_by_file:
                target_changed.discard(fname)
                target_deleted.add(fname)

        collection = get_vector_collection(
            store_path,
            name=collection_name,
            create=False,
            prefer_json=prefer_json,
        )
        try:
            if not pre_verified_current:
                existing_payload = collection.get(include=["metadatas"])
                raw_ids = existing_payload.get("ids") or []
                raw_metas = existing_payload.get("metadatas") or []
                stored_meta_by_id: dict[str, dict] = {}
                for idx, doc_id in enumerate(raw_ids):
                    if isinstance(doc_id, str):
                        meta_obj = raw_metas[idx] if idx < len(raw_metas) and isinstance(raw_metas[idx], dict) else {}
                        stored_meta_by_id[doc_id] = meta_obj
                for doc_id in stored_meta_by_id:
                    if doc_id not in rev_sha_by_file:
                        target_deleted.add(doc_id)
                for fname, disk_sha in rev_sha_by_file.items():
                    meta = stored_meta_by_id.get(fname)
                    if (
                        meta is None
                        or meta.get("content_sha256") != disk_sha
                        or meta.get("embedding_model") != effective_model
                        or meta.get("index_generation") != generation_id
                        or meta.get("embedding_dimensions") != expected_dims
                    ):
                        target_changed.add(fname)

            target_deleted -= target_changed

            batch_ids: list[str] = []
            batch_docs: list[str] = []
            batch_embs: list[list[float]] = []
            batch_metas: list[dict] = []

            for fname in sorted(target_changed):
                path = safe_file_in_dir(atoms_dir, fname, allowed_exts={".md"}, must_exist=True)
                if not path or _is_reparse_or_link(path):
                    raise PermissionError(f"Refusing to index missing or symlinked atom: {fname}")
                with open(path, "rb") as fh:
                    content_bytes = fh.read()
                content_sha = hashlib.sha256(content_bytes).hexdigest()
                if rev_sha_by_file.get(fname) != content_sha:
                    raise RuntimeError(f"Canonical atom {fname} changed concurrently during incremental vector upsert.")
                content = content_bytes.decode("utf-8")
                if require_valid_memory and not is_memory_document(content):
                    target_deleted.add(fname)
                    continue
                emb = embed_text(content, model=effective_model)
                if len(emb) != expected_dims:
                    raise RuntimeError(
                        f"Embedding dimensions changed from {expected_dims} to {len(emb)} during incremental upsert."
                    )
                meta = embedding_metadata(
                    fname,
                    content,
                    effective_model,
                    emb,
                    content_sha256=content_sha,
                )
                meta["index_generation"] = generation_id
                batch_ids.append(fname)
                batch_docs.append(content)
                batch_embs.append(emb)
                batch_metas.append(meta)

            batch_fn = getattr(collection, "batch", None)
            batch_ctx = batch_fn() if callable(batch_fn) else nullcontext()
            with batch_ctx:
                if target_deleted:
                    delete_fn = getattr(collection, "delete", None)
                    if callable(delete_fn):
                        delete_fn(ids=sorted(target_deleted))
                for start_idx in range(0, len(batch_ids), UPSERT_BATCH_SIZE):
                    end_idx = start_idx + UPSERT_BATCH_SIZE
                    collection.upsert(
                        documents=batch_docs[start_idx:end_idx],
                        embeddings=batch_embs[start_idx:end_idx],
                        metadatas=batch_metas[start_idx:end_idx],
                        ids=batch_ids[start_idx:end_idx],
                    )

            count_fn = getattr(collection, "count", None)
            if callable(count_fn):
                indexed_count = int(count_fn())
            else:
                indexed_count = len((collection.get(include=[]) or {}).get("ids") or [])
        finally:
            close_vector_collection(collection)

        skipped_count = max(0, len(revisions_before) - indexed_count)
        current_revisions = atom_revisions(atoms_dir)
        if current_revisions != revisions_before:
            raise RuntimeError("Canonical atoms changed while committing incremental vector manifest.")
        updated_manifest = build_index_manifest(
            atoms_dir=atoms_dir,
            collection_name=collection_name,
            embedding_model=effective_model,
            embedding_dimensions=expected_dims,
            indexed_count=indexed_count,
            generation_id=generation_id,
            skipped_count=skipped_count,
        )
        write_index_manifest(store_path, updated_manifest)

    return {
        "indexed": indexed_count,
        "reused": max(0, indexed_count - len(batch_ids)),
        "embedded": len(batch_ids),
        "deleted": len(target_deleted),
        "skipped": skipped_count,
        "failed": 0,
        "collection": collection_name,
        "store_path": store_path,
        "committed": True,
        "backup_path": None,
        "generation_id": generation_id,
        "incremental": True,
    }


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
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Perform an in-place incremental upsert/delete against the active generation manifest.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.incremental:
        stats = upsert_changed_vectors(
            atoms_dir=args.atoms_dir,
            store_path=args.store_path,
            collection_name=args.collection,
            model=args.model,
            require_valid_memory=not args.include_invalid,
            prefer_json=args.json,
        )
    else:
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
        f"indexed={stats['indexed']} reused={stats['reused']} embedded={stats['embedded']} "
        f"skipped={stats['skipped']} failed={stats['failed']}"
    )
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
