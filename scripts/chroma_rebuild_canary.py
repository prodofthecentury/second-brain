# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = ["chromadb", "ollama"]
# ///
"""Exercise a real Chroma staged rebuild and directory activation on this OS."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

import chromadb

from index_manifest import load_index_manifest, index_manifest_is_current
from rebuild_vectors import rebuild_vectors


ATOM_A = """---
id: alpha
type: atom
memory_type: semantic
sources: []
origin: user_local
citation_integrity: missing
claim_state: candidate
review_status: pending
---

## Claim
Alpha has a forest marker.

## Evidence
Alpha forest marker evidence.
"""

ATOM_B = """---
id: beta
type: atom
memory_type: semantic
sources: []
origin: user_local
citation_integrity: missing
claim_state: candidate
review_status: pending
---

## Claim
Beta has a marine marker.

## Evidence
Beta marine marker evidence.
"""


def test_real_chroma_generation_can_be_closed_swapped_and_reopened() -> None:
    temp_root = Path(tempfile.mkdtemp(prefix="second-brain-chroma-canary-"))
    brain = temp_root / "brain"
    atoms = brain / "2-atoms"
    store = brain / ".chroma"
    atoms.mkdir(parents=True)
    (atoms / "alpha.md").write_text(ATOM_A, encoding="utf-8")
    (atoms / "beta.md").write_text(ATOM_B, encoding="utf-8")

    initial_client = chromadb.PersistentClient(path=str(store))
    initial = initial_client.get_or_create_collection(
        "atoms", configuration={"hnsw": {"space": "cosine"}}
    )
    initial.add(
        ids=["old-generation"],
        documents=["old generation sentinel"],
        embeddings=[[0.0, 1.0]],
    )
    initial_client.close()

    def deterministic_embed(text: str, model: str) -> list[float]:
        return [1.0, 0.0] if "forest marker" in text else [0.0, 1.0]

    with patch("rebuild_vectors.embed_text", side_effect=deterministic_embed):
        result = rebuild_vectors(
            atoms_dir=str(atoms),
            store_path=str(store),
            collection_name="atoms",
            model="canary-embedder",
        )

    assert result["committed"] is True, "staged generation was not committed"
    backup_path = result["backup_path"]
    assert backup_path and os.path.isdir(backup_path), "previous generation was not retained"

    manifest = load_index_manifest(str(store))
    assert manifest is not None, "active generation manifest is missing"
    assert index_manifest_is_current(manifest, str(atoms)), "active generation is stale"
    assert manifest["distance_metric"] == "cosine"
    assert manifest["indexed_count"] == 2

    active_client = chromadb.PersistentClient(path=str(store))
    active = active_client.get_collection("atoms")
    results = active.query(query_embeddings=[[1.0, 0.0]], n_results=2)
    assert results["ids"][0][0] == "alpha.md", results["ids"]
    active_client.close()

    backup_client = chromadb.PersistentClient(path=backup_path)
    backup = backup_client.get_collection("atoms")
    previous = backup.get(ids=["old-generation"])
    assert previous["ids"] == ["old-generation"]
    backup_client.close()

    try:
        shutil.rmtree(temp_root)
    except OSError as exc:
        raise AssertionError(f"canary could not clean its closed temporary stores: {exc}") from exc


if __name__ == "__main__":
    test_real_chroma_generation_can_be_closed_swapped_and_reopened()
    print("CHROMA_REBUILD_CANARY_OK")
