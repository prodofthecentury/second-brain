# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = ["chromadb", "ollama"]
# ///
"""Compare raw lexical/dense candidate ranking and verify the trust gate."""

from __future__ import annotations

import json
import argparse
import os
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import ATOMS_DIR, CHROMA_PATH, EMBED_MODEL, LEXICAL_INDEX_PATH
from index_manifest import index_manifest_is_current, load_index_manifest
from lexical_store import search_lexical_index
from ollama_utils import embed_text
from recall import RecallOptions, search_lexical_memory, search_semantic_memory, validated_index_generation
from vector_store import get_vector_collection, close_vector_collection


ROOT = Path(__file__).resolve().parents[1]
EVALUATION_PATH = ROOT / "docs" / "retrieval-evaluation-proposed.json"
K = 5


def reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    return next((1.0 / position for position, name in enumerate(ranked, 1) if name in relevant), 0.0)


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(1.0 / __import__("math").log2(position + 1) for position, name in enumerate(ranked[:k], 1) if name in relevant)
    ideal_count = min(k, len(relevant))
    ideal = sum(1.0 / __import__("math").log2(position + 1) for position in range(1, ideal_count + 1))
    return dcg / ideal if ideal else 0.0


def dense_candidates(query: str, collection, manifest: dict) -> tuple[list[str], float]:
    started = time.perf_counter()
    embedding = embed_text(query, model=EMBED_MODEL)
    generation = validated_index_generation(
        manifest,
        atoms_dir=ATOMS_DIR,
        collection_name="atoms",
        embedding_model=EMBED_MODEL,
        embedding_dimensions=len(embedding),
    )
    result = collection.query(
        query_embeddings=[embedding],
        n_results=K,
        where={"$and": [{"embedding_model": EMBED_MODEL}, {"index_generation": generation}]},
        include=["metadatas", "distances"],
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    ranked = [item["filename"] for item in result["metadatas"][0]]
    return ranked, elapsed_ms


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Optional path for a JSON copy of the evaluation result.")
    args = parser.parse_args(argv)
    corpus = json.loads(EVALUATION_PATH.read_text(encoding="utf-8"))
    manifest = load_index_manifest(CHROMA_PATH)
    if not index_manifest_is_current(manifest, ATOMS_DIR):
        raise SystemExit("Dense evaluation refused: no current index manifest.")
    collection = get_vector_collection(CHROMA_PATH, name="atoms", create=False)
    if getattr(collection, "fallback_reason", None):
        raise SystemExit(f"Dense evaluation refused: native Chroma unavailable ({collection.fallback_reason}).")

    records = []
    lexical_times = []
    dense_times = []
    trusted_false_positives = 0
    try:
        for case in corpus["queries"]:
            query = case["query"]
            relevant = set(case["relevant"])

            start = time.perf_counter()
            lexical = search_lexical_index(query, LEXICAL_INDEX_PATH, n_results=K, atoms_dir=ATOMS_DIR)
            lexical_ms = (time.perf_counter() - start) * 1000
            lexical_ranked = [item.filename for item in lexical]

            dense_ranked, dense_ms = dense_candidates(query, collection, manifest)
            lexical_times.append(lexical_ms)
            dense_times.append(dense_ms)

            trusted_lexical, _ = search_lexical_memory(query, RecallOptions(n_results=K, follow_links=False))
            trusted_dense, _ = search_semantic_memory(query, RecallOptions(n_results=K, follow_links=False))
            trusted_result_count = len(trusted_lexical) + len(trusted_dense)
            false_positives = trusted_result_count if case.get("expect_no_factual_answer") else None
            if false_positives is not None:
                trusted_false_positives += false_positives

            records.append({
                "id": case["id"],
                "relevant": sorted(relevant),
                "lexical": {
                    "ranked": lexical_ranked,
                    "recall_at_k": len(relevant.intersection(lexical_ranked)) / len(relevant) if relevant else None,
                    "mrr": reciprocal_rank(lexical_ranked, relevant) if relevant else None,
                    "ndcg_at_k": ndcg_at_k(lexical_ranked, relevant, K) if relevant else None,
                    "latency_ms": round(lexical_ms, 3),
                },
                "dense": {
                    "ranked": dense_ranked,
                    "recall_at_k": len(relevant.intersection(dense_ranked)) / len(relevant) if relevant else None,
                    "mrr": reciprocal_rank(dense_ranked, relevant) if relevant else None,
                    "ndcg_at_k": ndcg_at_k(dense_ranked, relevant, K) if relevant else None,
                    "latency_ms": round(dense_ms, 3),
                },
                "trusted_result_count_across_both_paths": trusted_result_count,
                "trusted_false_positives": false_positives,
            })
    finally:
        close_vector_collection(collection)

    scored = [record for record in records if record["relevant"]]
    summary = {
        "evaluation_status": corpus["status"],
        "index_generation": manifest["generation_id"],
        "embedding_model": EMBED_MODEL,
        "embedding_dimensions": manifest["embedding_dimensions"],
        "k": K,
        "labelled_query_count": len(scored),
        "trap_query_count": len(records) - len(scored),
        "lexical_mean_recall_at_k": statistics.mean(record["lexical"]["recall_at_k"] for record in scored),
        "dense_mean_recall_at_k": statistics.mean(record["dense"]["recall_at_k"] for record in scored),
        "lexical_mean_mrr": statistics.mean(record["lexical"]["mrr"] for record in scored),
        "dense_mean_mrr": statistics.mean(record["dense"]["mrr"] for record in scored),
        "lexical_mean_ndcg_at_k": statistics.mean(record["lexical"]["ndcg_at_k"] for record in scored),
        "dense_mean_ndcg_at_k": statistics.mean(record["dense"]["ndcg_at_k"] for record in scored),
        "lexical_median_latency_ms": statistics.median(lexical_times),
        "dense_median_latency_ms": statistics.median(dense_times),
        "trusted_trap_false_positives": trusted_false_positives,
        "queries": records,
    }
    rendered = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.output:
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
