# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""
Small vector-store adapter.

ChromaDB is preferred, but its native persistent backend can be unavailable on
some Windows runtimes. The JSON fallback keeps the local brain usable and
preserves the subset of the Chroma collection API this project needs.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
import tempfile
from typing import Any


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


class JsonVectorCollection:
    def __init__(self, path: str, name: str, fallback_reason: str | None = None, create_dir: bool = True):
        self.path = path
        self.name = name
        self.fallback_reason = fallback_reason
        self._items: dict[str, dict[str, Any]] | None = None
        self._loaded_stat: tuple[int, int] | None = None
        self._batch_depth: int = 0
        self._dirty: bool = False
        if create_dir:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)

    def _file_stat(self) -> tuple[int, int] | None:
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._dirty and self._items is not None:
            return self._items
        current_stat = self._file_stat()
        if current_stat is None:
            self._items = {}
            self._loaded_stat = None
            return self._items
        if self._items is not None and self._loaded_stat == current_stat:
            return self._items
        with open(self.path, "r", encoding="utf-8") as file:
            payload = json.load(file)
        raw_items = payload.get("items", {}) if isinstance(payload, dict) else {}
        self._items = dict(raw_items) if isinstance(raw_items, dict) else {}
        self._loaded_stat = current_stat
        return self._items

    def _save(self, items: dict[str, dict[str, Any]] | None = None) -> None:
        target_items = items if items is not None else (self._items if self._items is not None else {})
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        descriptor, tmp_path = tempfile.mkstemp(prefix=".vec-", suffix=".tmp", dir=parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(
                    {"name": self.name, "items": target_items},
                    file,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp_path, self.path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        self._items = target_items
        self._loaded_stat = self._file_stat()
        self._dirty = False

    def begin_batch(self) -> None:
        self._batch_depth += 1

    def flush(self) -> None:
        if self._dirty and self._items is not None:
            self._save(self._items)

    @contextmanager
    def batch(self):
        self.begin_batch()
        try:
            yield self
        except Exception:
            self._batch_depth = max(0, self._batch_depth - 1)
            if self._batch_depth == 0:
                self._dirty = False
                self._items = None
                self._loaded_stat = None
            raise
        else:
            self._batch_depth = max(0, self._batch_depth - 1)
            if self._batch_depth == 0:
                self.flush()

    def close(self) -> None:
        self._batch_depth = 0
        self.flush()

    def upsert(self, documents: list[str], embeddings: list[list[float]], metadatas: list[dict], ids: list[str]):
        items = self._load()
        for doc_id, document, embedding, metadata in zip(ids, documents, embeddings, metadatas):
            items[doc_id] = {
                "document": document,
                "embedding": embedding if isinstance(embedding, list) else [float(x) for x in embedding],
                "metadata": metadata,
            }
        self._items = items
        self._dirty = True
        if self._batch_depth == 0:
            self.flush()

    def count(self) -> int:
        return len(self._load())

    def delete(self, ids: list[str] | None = None, where: dict[str, Any] | None = None) -> None:
        items = self._load()
        removed = False
        if isinstance(ids, str):
            ids = [ids]
        if ids is not None:
            for doc_id in ids:
                if doc_id in items:
                    items.pop(doc_id, None)
                    removed = True
        elif where:
            to_drop = [
                doc_id
                for doc_id, item in items.items()
                if isinstance(item, dict) and self._matches_where(item.get("metadata", {}), where)
            ]
            for doc_id in to_drop:
                items.pop(doc_id, None)
                removed = True
        if removed:
            self._items = items
            self._dirty = True
            if self._batch_depth == 0:
                self.flush()

    def get(
        self,
        ids: list[str] | None = None,
        where: dict[str, Any] | None = None,
        include: list[str] | None = None,
    ) -> dict[str, list[Any]]:
        _ = include  # Accepted for ChromaDB API parity; all stored fields are cheap in memory.
        items = self._load()
        if isinstance(ids, str):
            ids = [ids]
        candidate_ids = [doc_id for doc_id in ids if doc_id in items] if ids is not None else list(items.keys())

        out_ids: list[str] = []
        out_embeddings: list[Any] = []
        out_metadatas: list[Any] = []
        out_documents: list[Any] = []
        for doc_id in candidate_ids:
            item = items.get(doc_id)
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata", {})
            meta_dict = metadata if isinstance(metadata, dict) else {}
            if not self._matches_where(meta_dict, where):
                continue
            out_ids.append(doc_id)
            out_embeddings.append(item.get("embedding", []))
            out_metadatas.append(meta_dict)
            out_documents.append(item.get("document", ""))
        return {
            "ids": out_ids,
            "embeddings": out_embeddings,
            "metadatas": out_metadatas,
            "documents": out_documents,
        }

    @staticmethod
    def _matches_where(metadata: dict[str, Any], where: dict[str, Any] | None) -> bool:
        if not where:
            return True
        if "$and" in where:
            clauses = where["$and"]
            return isinstance(clauses, list) and all(
                isinstance(clause, dict) and JsonVectorCollection._matches_where(metadata, clause)
                for clause in clauses
            )
        return all(metadata.get(key) == value for key, value in where.items())

    @staticmethod
    def _is_compatible_embedding(item: dict[str, Any], query_embedding: list[float]) -> bool:
        embedding = item.get("embedding", [])
        metadata = item.get("metadata", {})
        return (
            isinstance(embedding, list)
            and isinstance(metadata, dict)
            and bool(query_embedding)
            and len(embedding) == len(query_embedding)
            and metadata.get("embedding_dimensions") == len(embedding)
            and isinstance(metadata.get("embedding_model"), str)
            and bool(metadata["embedding_model"].strip())
        )

    def query(
        self,
        query_embeddings: list[list[float]],
        n_results: int = 5,
        where: dict[str, Any] | None = None,
    ) -> dict:
        items = self._load()
        query_embedding = query_embeddings[0] if query_embeddings else []
        eligible_items = [
            item
            for item in items.items()
            if self._is_compatible_embedding(item[1], query_embedding)
            and self._matches_where(item[1].get("metadata", {}), where)
        ]
        ranked = sorted(
            eligible_items,
            key=lambda item: cosine_similarity(query_embedding, item[1].get("embedding", [])),
            reverse=True,
        )[:n_results]

        documents = [item["document"] for _, item in ranked]
        metadatas = [item.get("metadata", {}) for _, item in ranked]
        ids = [doc_id for doc_id, _ in ranked]
        distances = [
            1 - cosine_similarity(query_embedding, item.get("embedding", []))
            for _, item in ranked
        ]
        return {
            "documents": [documents],
            "metadatas": [metadatas],
            "ids": [ids],
            "distances": [distances],
        }


def _close_chroma_client(client) -> None:
    if client is None:
        return
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
            return
        except Exception:
            pass
    system = getattr(client, "_system", None)
    stop = getattr(system, "stop", None)
    if callable(stop):
        try:
            stop()
        except Exception:
            pass


class ManagedChromaCollection:
    """Keep the owning client reachable so rebuilds can release file handles."""

    def __init__(self, client, collection):
        self._owner_client = client
        self._collection = collection
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._collection, name)

    def close(self):
        if not self._closed:
            _close_chroma_client(self._owner_client)
            self._closed = True


def close_vector_collection(collection):
    """Close an owned persistent client when a collection handle supports it."""
    close = getattr(collection, "close", None)
    if callable(close):
        close()


def json_collection_path(store_path: str, name: str) -> str:
    return os.path.join(store_path, f"{name}.json")


def get_vector_collection(
    store_path: str,
    name: str = "atoms",
    create: bool = True,
    announce: bool = False,
    prefer_json: bool = False,
):
    if create:
        os.makedirs(store_path, exist_ok=True)
    elif not os.path.isdir(store_path):
        return JsonVectorCollection(
            json_collection_path(store_path, name),
            name,
            fallback_reason="vector store directory not initialized",
            create_dir=False,
        )
    elif prefer_json or not os.path.isfile(os.path.join(store_path, "chroma.sqlite3")):
        return JsonVectorCollection(
            json_collection_path(store_path, name),
            name,
            fallback_reason="prefer_json requested" if prefer_json else "chroma.sqlite3 not initialized",
            create_dir=create,
        )
    if prefer_json:
        return JsonVectorCollection(
            json_collection_path(store_path, name),
            name,
            fallback_reason="prefer_json requested",
            create_dir=create,
        )

    client = None
    try:
        import chromadb

        client = chromadb.PersistentClient(path=store_path)
        if create:
            collection = client.get_or_create_collection(name=name, configuration={"hnsw": {"space": "cosine"}})
        else:
            collection = client.get_collection(name=name)
        return ManagedChromaCollection(client, collection)
    except Exception as exc:
        if client is not None:
            _close_chroma_client(client)
        if announce:
            print(f"[!] ChromaDB persistent store unavailable; using JSON vector fallback ({exc})")
        return JsonVectorCollection(
            json_collection_path(store_path, name),
            name,
            fallback_reason=str(exc),
            create_dir=create,
        )


def reset_vector_collection(
    store_path: str,
    name: str = "atoms",
    announce: bool = False,
    prefer_json: bool = False,
):
    """Reset a vector collection so it can be rebuilt from Markdown source."""
    os.makedirs(store_path, exist_ok=True)
    if not prefer_json:
        client = None
        try:
            import chromadb

            client = chromadb.PersistentClient(path=store_path)
            try:
                client.delete_collection(name=name)
            except Exception:
                pass
            collection = client.get_or_create_collection(
                name=name,
                configuration={"hnsw": {"space": "cosine"}},
            )
            return ManagedChromaCollection(client, collection)
        except Exception as exc:
            if client is not None:
                _close_chroma_client(client)
            if announce:
                print(f"[!] ChromaDB reset unavailable; using JSON vector fallback ({exc})")

    path = json_collection_path(store_path, name)
    collection = JsonVectorCollection(path, name)
    collection._save({})
    return collection
