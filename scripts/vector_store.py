# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""
Small vector-store adapter.

ChromaDB is preferred, but its native persistent backend can be unavailable on
some Windows runtimes. The JSON fallback keeps the local brain usable and
preserves the subset of the Chroma collection API this project needs.
"""

from __future__ import annotations

import json
import math
import os
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
    def __init__(self, path: str, name: str, fallback_reason: str | None = None):
        self.path = path
        self.name = name
        self.fallback_reason = fallback_reason
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def _load(self) -> dict[str, dict[str, Any]]:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r", encoding="utf-8") as file:
            payload = json.load(file)
        return payload.get("items", {})

    def _save(self, items: dict[str, dict[str, Any]]):
        with open(self.path, "w", encoding="utf-8") as file:
            json.dump({"name": self.name, "items": items}, file, ensure_ascii=False, indent=2)

    def upsert(self, documents: list[str], embeddings: list[list[float]], metadatas: list[dict], ids: list[str]):
        items = self._load()
        for doc_id, document, embedding, metadata in zip(ids, documents, embeddings, metadatas):
            items[doc_id] = {
                "document": document,
                "embedding": embedding,
                "metadata": metadata,
            }
        self._save(items)

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
            self._owner_client.close()
            self._closed = True


def close_vector_collection(collection):
    """Close an owned persistent client when a collection handle supports it."""
    close = getattr(collection, "close", None)
    if callable(close):
        close()


def json_collection_path(store_path: str, name: str) -> str:
    return os.path.join(store_path, f"{name}.json")


def get_vector_collection(store_path: str, name: str = "atoms", create: bool = True, announce: bool = False):
    os.makedirs(store_path, exist_ok=True)
    try:
        import chromadb

        client = chromadb.PersistentClient(path=store_path)
        if create:
            collection = client.get_or_create_collection(name=name, configuration={"hnsw": {"space": "cosine"}})
        else:
            collection = client.get_collection(name=name)
        return ManagedChromaCollection(client, collection)
    except Exception as exc:
        if announce:
            print(f"[!] ChromaDB persistent store unavailable; using JSON vector fallback ({exc})")
        return JsonVectorCollection(json_collection_path(store_path, name), name, fallback_reason=str(exc))


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
                client.close()
            if announce:
                print(f"[!] ChromaDB reset unavailable; using JSON vector fallback ({exc})")

    path = json_collection_path(store_path, name)
    collection = JsonVectorCollection(path, name)
    collection._save({})
    return collection
