# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = ["chromadb", "ollama"]
# ///
"""Compare raw lexical/dense candidate ranking and verify the trust gate."""

from __future__ import annotations

import json
import argparse
import hashlib
import os
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import ATOMS_DIR, CHROMA_PATH, EMBED_MODEL, LEXICAL_INDEX_PATH
from atom_mutator import approved_revision_is_current
from evaluation_dataset import load_dataset
from index_manifest import index_manifest_is_current, load_index_manifest
from lexical_store import search_lexical_index
from ollama_utils import EmbeddingUnavailableError, embed_text
from recall import (
    RecallOptions,
    search_hybrid_rrf_memory,
    search_lexical_memory,
    search_semantic_memory,
    validated_index_generation,
)
from vector_store import get_vector_collection, close_vector_collection


ROOT = Path(__file__).resolve().parents[1]
EVALUATION_PATH = ROOT / "docs" / "retrieval-evaluation-proposed.json"
K = 5
CANDIDATE_POOL = 20
RRF_K_CONSTANT = 60


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return round(ordered[idx], 3)


def reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    return next((1.0 / position for position, name in enumerate(ranked, 1) if name in relevant), 0.0)


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(1.0 / __import__("math").log2(position + 1) for position, name in enumerate(ranked[:k], 1) if name in relevant)
    ideal_count = min(k, len(relevant))
    ideal = sum(1.0 / __import__("math").log2(position + 1) for position in range(1, ideal_count + 1))
    return dcg / ideal if ideal else 0.0


def fuse_rrf_candidates(lexical_pool: list[str], dense_pool: list[str], k: int = K, rrf_k: int = RRF_K_CONSTANT) -> list[str]:
    scores: dict[str, float] = {}
    for rank, name in enumerate(lexical_pool):
        scores[name] = scores.get(name, 0.0) + (1.0 / (rrf_k + rank + 1))
    for rank, name in enumerate(dense_pool):
        scores[name] = scores.get(name, 0.0) + (1.0 / (rrf_k + rank + 1))
    return [name for name, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:k]]


def dense_candidates(query: str, collection, manifest: dict, n_results: int = CANDIDATE_POOL) -> tuple[list[str], float]:
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
        n_results=n_results,
        where={"$and": [{"embedding_model": EMBED_MODEL}, {"index_generation": generation}]},
        include=["metadatas", "distances"],
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    ranked = [item["filename"] for item in result["metadatas"][0]]
    return ranked, elapsed_ms


def _score_error(message: str) -> int:
    print(f"Evaluation unavailable: {message}", file=sys.stderr)
    return 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Optional path for a JSON copy of the evaluation result.")
    args = parser.parse_args(argv)
    try:
        corpus = load_dataset(
            EVALUATION_PATH,
            atoms_dir=ATOMS_DIR,
            approval_check=lambda path: approved_revision_is_current(str(path), atoms_dir=ATOMS_DIR),
        )
    except Exception as exc:
        return _score_error(f"dataset validation failed: {exc}")
    dataset_sha256 = hashlib.sha256(EVALUATION_PATH.read_bytes()).hexdigest()
    manifest = load_index_manifest(CHROMA_PATH)
    if not index_manifest_is_current(manifest, ATOMS_DIR):
        return _score_error("dense evaluation refused: no current index manifest")
    try:
        collection = get_vector_collection(CHROMA_PATH, name="atoms", create=False)
    except Exception as exc:
        return _score_error(f"native Chroma collection unavailable: {exc}")
    if getattr(collection, "fallback_reason", None):
        close_vector_collection(collection)
        return _score_error(f"native Chroma unavailable ({collection.fallback_reason})")

    records = []
    lexical_times = []
    dense_times = []
    rrf_times = []
    trusted_false_positives = 0
    try:
        for case in corpus["queries"]:
            query = case["query"]
            relevant = set(case["candidate_relevance"])

            start = time.perf_counter()
            lexical_pool_items = search_lexical_index(query, LEXICAL_INDEX_PATH, n_results=CANDIDATE_POOL, atoms_dir=ATOMS_DIR)
            lexical_ms = (time.perf_counter() - start) * 1000
            lexical_pool = [item.filename for item in lexical_pool_items]
            lexical_ranked = lexical_pool[:K]

            try:
                dense_pool, dense_ms = dense_candidates(query, collection, manifest, n_results=CANDIDATE_POOL)
            except EmbeddingUnavailableError as exc:
                return _score_error(str(exc))
            except Exception as exc:
                return _score_error(f"dense retrieval failed for {case['id']}: {exc}")
            dense_ranked = dense_pool[:K]

            rrf_start = time.perf_counter()
            rrf_ranked = fuse_rrf_candidates(lexical_pool, dense_pool, k=K)
            rrf_ms = lexical_ms + dense_ms + (time.perf_counter() - rrf_start) * 1000

            lexical_times.append(lexical_ms)
            dense_times.append(dense_ms)
            rrf_times.append(rrf_ms)

            proj_opt = (case["project_scope"],) if case.get("project_scope") else ()
            base_opts = RecallOptions(n_results=K, follow_links=False, projects=proj_opt)
            trusted_lexical, _ = search_lexical_memory(query, base_opts)
            trusted_dense, _ = search_semantic_memory(query, base_opts)
            trusted_rrf, _ = search_hybrid_rrf_memory(query, base_opts)
            trusted_lexical_filenames = [item.filename for item in trusted_lexical]
            trusted_dense_filenames = [item.filename for item in trusted_dense]
            trusted_rrf_filenames = [item.filename for item in trusted_rrf]
            trusted_result_count = len(trusted_lexical) + len(trusted_dense) + len(trusted_rrf)
            false_positives = trusted_result_count if case["answerability"] == "unanswerable" else None
            expected_atoms = set(case["expected_atom_ids"])
            lexical_expected_recall = (
                len(expected_atoms.intersection(lexical_ranked)) / len(expected_atoms)
                if case["answerability"] == "answerable" else None
            )
            dense_expected_recall = (
                len(expected_atoms.intersection(dense_ranked)) / len(expected_atoms)
                if case["answerability"] == "answerable" else None
            )
            rrf_expected_recall = (
                len(expected_atoms.intersection(rrf_ranked)) / len(expected_atoms)
                if case["answerability"] == "answerable" else None
            )
            trusted_lexical_expected_recall = (
                len(expected_atoms.intersection(trusted_lexical_filenames)) / len(expected_atoms)
                if case["answerability"] == "answerable" else None
            )
            trusted_dense_expected_recall = (
                len(expected_atoms.intersection(trusted_dense_filenames)) / len(expected_atoms)
                if case["answerability"] == "answerable" else None
            )
            trusted_rrf_expected_recall = (
                len(expected_atoms.intersection(trusted_rrf_filenames)) / len(expected_atoms)
                if case["answerability"] == "answerable" else None
            )
            if false_positives is not None:
                trusted_false_positives += false_positives

            records.append({
                "id": case["id"],
                "category": case.get("category", "unknown"),
                "answerability": case["answerability"],
                "split": case["split"],
                "project_scope": case.get("project_scope"),
                "candidate_relevance": sorted(relevant),
                "expected_atom_ids": case["expected_atom_ids"],
                "expected_claims": case["expected_claims"],
                "lexical": {
                    "ranked": lexical_ranked,
                    "recall_at_k": len(relevant.intersection(lexical_ranked)) / len(relevant) if relevant else None,
                    "mrr": reciprocal_rank(lexical_ranked, relevant) if relevant else None,
                    "ndcg_at_k": ndcg_at_k(lexical_ranked, relevant, K) if relevant else None,
                    "latency_ms": round(lexical_ms, 3),
                    "expected_atom_recall_at_k": lexical_expected_recall,
                    "trusted_expected_atom_recall": trusted_lexical_expected_recall,
                },
                "dense": {
                    "ranked": dense_ranked,
                    "recall_at_k": len(relevant.intersection(dense_ranked)) / len(relevant) if relevant else None,
                    "mrr": reciprocal_rank(dense_ranked, relevant) if relevant else None,
                    "ndcg_at_k": ndcg_at_k(dense_ranked, relevant, K) if relevant else None,
                    "latency_ms": round(dense_ms, 3),
                    "expected_atom_recall_at_k": dense_expected_recall,
                    "trusted_expected_atom_recall": trusted_dense_expected_recall,
                },
                "rrf_hybrid": {
                    "ranked": rrf_ranked,
                    "recall_at_k": len(relevant.intersection(rrf_ranked)) / len(relevant) if relevant else None,
                    "mrr": reciprocal_rank(rrf_ranked, relevant) if relevant else None,
                    "ndcg_at_k": ndcg_at_k(rrf_ranked, relevant, K) if relevant else None,
                    "latency_ms": round(rrf_ms, 3),
                    "expected_atom_recall_at_k": rrf_expected_recall,
                    "trusted_expected_atom_recall": trusted_rrf_expected_recall,
                },
                "trusted_result_count_across_all_paths": trusted_result_count,
                "trusted_false_positives": false_positives,
            })
    finally:
        close_vector_collection(collection)

    candidate_scored = [record for record in records if record["candidate_relevance"]]
    answerable = [record for record in records if record["answerability"] == "answerable"]
    preference = [record for record in records if record["answerability"] == "preference"]
    exploratory = [record for record in records if record["answerability"] == "exploratory"]
    unanswerable = [record for record in records if record["answerability"] == "unanswerable"]

    def _group_recall(group: list[dict], mode_key: str) -> float | None:
        vals = [r[mode_key]["recall_at_k"] for r in group if r[mode_key]["recall_at_k"] is not None]
        return round(statistics.mean(vals), 4) if vals else None

    split_summary = {}
    for split_name in ("regression", "development", "holdout"):
        sub = [r for r in candidate_scored if r["split"] == split_name]
        split_summary[split_name] = {
            "scored_query_count": len(sub),
            "lexical_recall_at_k": _group_recall(sub, "lexical"),
            "dense_recall_at_k": _group_recall(sub, "dense"),
            "rrf_hybrid_recall_at_k": _group_recall(sub, "rrf_hybrid"),
        }

    category_summary = {
        "answerable": {
            "count": len(answerable),
            "lexical_recall_at_k": _group_recall(answerable, "lexical"),
            "dense_recall_at_k": _group_recall(answerable, "dense"),
            "rrf_hybrid_recall_at_k": _group_recall(answerable, "rrf_hybrid"),
        },
        "preference": {
            "count": len(preference),
            "lexical_recall_at_k": _group_recall(preference, "lexical"),
            "dense_recall_at_k": _group_recall(preference, "dense"),
            "rrf_hybrid_recall_at_k": _group_recall(preference, "rrf_hybrid"),
        },
        "exploratory": {
            "count": len(exploratory),
            "lexical_recall_at_k": _group_recall(exploratory, "lexical"),
            "dense_recall_at_k": _group_recall(exploratory, "dense"),
            "rrf_hybrid_recall_at_k": _group_recall(exploratory, "rrf_hybrid"),
        },
    }

    summary = {
        "evaluation_status": corpus["status"],
        "dataset_id": corpus["dataset_id"],
        "dataset_version": corpus["dataset_version"],
        "dataset_sha256": dataset_sha256,
        "index_generation": manifest["generation_id"],
        "embedding_model": EMBED_MODEL,
        "embedding_dimensions": manifest["embedding_dimensions"],
        "k": K,
        "candidate_ranking_query_count": len(candidate_scored),
        "answerable_query_count": len(answerable),
        "preference_query_count": len(preference),
        "exploratory_candidate_query_count": len(exploratory),
        "unanswerable_query_count": len(unanswerable),
        "candidate_lexical_mean_recall_at_k": statistics.mean(record["lexical"]["recall_at_k"] for record in candidate_scored),
        "candidate_dense_mean_recall_at_k": statistics.mean(record["dense"]["recall_at_k"] for record in candidate_scored),
        "candidate_rrf_hybrid_mean_recall_at_k": statistics.mean(record["rrf_hybrid"]["recall_at_k"] for record in candidate_scored),
        "candidate_lexical_mean_mrr": statistics.mean(record["lexical"]["mrr"] for record in candidate_scored),
        "candidate_dense_mean_mrr": statistics.mean(record["dense"]["mrr"] for record in candidate_scored),
        "candidate_rrf_hybrid_mean_mrr": statistics.mean(record["rrf_hybrid"]["mrr"] for record in candidate_scored),
        "candidate_lexical_mean_ndcg_at_k": statistics.mean(record["lexical"]["ndcg_at_k"] for record in candidate_scored),
        "candidate_dense_mean_ndcg_at_k": statistics.mean(record["dense"]["ndcg_at_k"] for record in candidate_scored),
        "candidate_rrf_hybrid_mean_ndcg_at_k": statistics.mean(record["rrf_hybrid"]["ndcg_at_k"] for record in candidate_scored),
        "answerable_lexical_expected_atom_recall_at_k": statistics.mean(record["lexical"]["expected_atom_recall_at_k"] for record in answerable),
        "answerable_dense_expected_atom_recall_at_k": statistics.mean(record["dense"]["expected_atom_recall_at_k"] for record in answerable),
        "answerable_rrf_hybrid_expected_atom_recall_at_k": statistics.mean(record["rrf_hybrid"]["expected_atom_recall_at_k"] for record in answerable),
        "trusted_answerable_lexical_expected_atom_recall": statistics.mean(record["lexical"]["trusted_expected_atom_recall"] for record in answerable),
        "trusted_answerable_dense_expected_atom_recall": statistics.mean(record["dense"]["trusted_expected_atom_recall"] for record in answerable),
        "trusted_answerable_rrf_hybrid_expected_atom_recall": statistics.mean(record["rrf_hybrid"]["trusted_expected_atom_recall"] for record in answerable),
        "lexical_median_latency_ms": statistics.median(lexical_times),
        "lexical_p95_latency_ms": percentile(lexical_times, 95),
        "dense_median_latency_ms": statistics.median(dense_times),
        "dense_p95_latency_ms": percentile(dense_times, 95),
        "rrf_hybrid_median_latency_ms": statistics.median(rrf_times),
        "rrf_hybrid_p95_latency_ms": percentile(rrf_times, 95),
        "by_split": split_summary,
        "by_category": category_summary,
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
