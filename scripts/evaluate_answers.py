# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["ollama", "chromadb"]
# ///
"""Run repeated answer-level grounding checks against the approved regression set."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from answer_policy import ANSWER_DRAFT_SCHEMA, AnswerRequest, build_grounding_prompt, validate_standard_answer
from atom_mutator import approved_revision_is_current, review_lock
from config import ATOMS_DIR, CHROMA_PATH, GEN_MODEL, LEXICAL_INDEX_PATH, SIDECAR_DIR, _is_reparse_or_link
from evaluation_dataset import validate_dataset
from host_adapters import (
    _redact_outbound_with_map,
    _rehydrate_draft_claims,
    restrict_security_file_permissions,
    validate_model_identifier,
)
from index_manifest import load_index_manifest
from lexical_store import lexical_index_is_current
from ollama_utils import _DEFAULT_OLLAMA_CHAT, _is_monkeypatched_ollama_fn, _resolve_local_ollama_rule_and_url, ollama_base_url
from recall import RecallOptions, search_memory

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "docs" / "retrieval-evaluation-proposed.json"
DEFAULT_OUTPUT_PATH = Path(SIDECAR_DIR) / "answer-evaluation-latest.json"


class EvaluationRunError(RuntimeError):
    """Raised when an answer evaluation is incomplete and cannot be scored."""


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * fraction + 0.999999)) - 1))
    return round(ordered[index], 3)


def score_evaluation(corpus: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Score validated complete answers against explicit expected claims and traps."""
    cases = {
        item["id"]: item
        for item in corpus.get("queries", [])
        if item.get("answerability") in {"answerable", "unanswerable"}
    }

    if not cases:
        raise EvaluationRunError("No answerable or unanswerable cases are available to score.")

    grouped: dict[str, list[dict[str, Any]]] = {case_id: [] for case_id in cases}
    for observation in observations:
        if not isinstance(observation, dict) or observation.get("case_id") not in cases:
            raise EvaluationRunError("An observation refers to an unknown or exploratory case.")
        claims = observation.get("claims")
        if not isinstance(claims, list) or any(not isinstance(claim, str) for claim in claims):
            raise EvaluationRunError("Every observation must contain a list of validated claim strings.")
        if not isinstance(observation.get("abstained"), bool):
            raise EvaluationRunError("Every observation must report whether the answer abstained.")
        grouped[observation["case_id"]].append(observation)

    positive_runs = 0
    positive_expected = 0
    positive_covered = 0
    full_positive_answers = 0
    unexpected_claims = 0
    displayed_claims = 0
    trap_runs = 0
    trap_leaks = 0
    invalid_abstentions = 0
    citation_count = 0
    all_claim_count = 0
    positive_latency: list[float] = []

    for case_id, case_runs in grouped.items():
        case = cases[case_id]
        expected_claims = {claim["text"] for claim in case.get("expected_claims", [])}
        for observation in case_runs:
            claims = observation["claims"]
            displayed_claims += len(claims)
            all_claim_count += len(claims)
            citation_count += int(observation.get("citation_count", 0))
            invalid_abstentions += int(bool(observation.get("invalid_draft")))
            if isinstance(observation.get("end_to_end_ms"), (int, float)) and case["answerability"] == "answerable":
                positive_latency.append(float(observation["end_to_end_ms"]))

            if case["answerability"] == "answerable":
                positive_runs += 1
                positive_expected += len(expected_claims)
                matches = expected_claims.intersection(claims)
                positive_covered += len(matches)
                if expected_claims and expected_claims.issubset(set(claims)):
                    full_positive_answers += 1
                unexpected_claims += len(set(claims) - expected_claims)
            else:
                trap_runs += 1
                if claims:
                    trap_leaks += 1

    return {
        "answerable_run_count": positive_runs,
        "positive_expected_claim_coverage": _ratio(positive_covered, positive_expected),
        "positive_complete_answer_rate": _ratio(full_positive_answers, positive_runs),
        "positive_abstention_rate": _ratio(
            sum(bool(item["abstained"]) for case_id, items in grouped.items()
                if cases[case_id]["answerability"] == "answerable" for item in items),
            positive_runs,
        ),
        "unexpected_displayed_claim_count": unexpected_claims,
        "displayed_claim_count": displayed_claims,
        "unexpected_displayed_claim_rate": _ratio(unexpected_claims, displayed_claims),
        "trap_run_count": trap_runs,
        "trap_claim_leak_count": trap_leaks,
        "trap_claim_leakage_rate": _ratio(trap_leaks, trap_runs),
        "trap_abstention_rate": _ratio(trap_runs - trap_leaks, trap_runs),
        "invalid_draft_abstention_count": invalid_abstentions,
        "validated_claim_citation_rate": _ratio(citation_count, all_claim_count),
        "positive_end_to_end_latency_ms": {
            "p50": round(statistics.median(positive_latency), 3) if positive_latency else None,
            "p95": _percentile(positive_latency, 0.95),
            "max": round(max(positive_latency), 3) if positive_latency else None,
        },
        "case_run_counts": {case_id: len(items) for case_id, items in grouped.items()},
    }


def evaluate_quality_gate(metrics: dict[str, Any]) -> dict[str, Any]:
    """Apply strict regression conditions to the small, owner-reviewed pilot set."""
    failures = []
    if metrics.get("answerable_run_count", 0) <= 0:
        failures.append("no answerable runs were scored")
    if metrics.get("positive_complete_answer_rate") != 1.0:
        failures.append("not every answerable run covered all expected claims")
    if metrics.get("unexpected_displayed_claim_count") != 0:
        failures.append("unexpected claims were displayed")
    if metrics.get("trap_run_count", 0) <= 0:
        failures.append("no unanswerable trap runs were scored")
    if metrics.get("trap_claim_leak_count") != 0:
        failures.append("at least one trap produced a displayed claim")
    if metrics.get("validated_claim_citation_rate") != 1.0:
        failures.append("displayed claims did not all have verified citations")
    return {
        "passed": not failures,
        "failures": failures,
        "requirements": {
            "positive_complete_answer_rate": 1.0,
            "unexpected_displayed_claim_count": 0,
            "trap_claim_leak_count": 0,
            "validated_claim_citation_rate": 1.0,
        },
        "scope_note": "Regression gate for the current tiny pilot set; not evidence of general model quality.",
    }


def _write_json_atomically(output_path: Path, payload: dict[str, Any]) -> None:
    target = Path(os.path.abspath(str(output_path)))
    if os.path.lexists(str(target)) and _is_reparse_or_link(str(target)):
        raise PermissionError(f"Refusing to overwrite symlinked evaluation output: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse_or_link(str(target.parent)):
        raise PermissionError(f"Refusing to write evaluation output under symlinked directory: {target.parent}")

    lock_path = os.path.join(SIDECAR_DIR, "writer.lock")
    with review_lock(lock_path):
        descriptor, staging = tempfile.mkstemp(prefix=".answer-eval-", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staging, target)
            restrict_security_file_permissions(target)
        except Exception:
            try:
                os.unlink(staging)
            except OSError:
                pass
            raise


def run_evaluation(*, runs: int, model: str, dataset_path: Path = DATASET_PATH) -> dict[str, Any]:
    """Run the local Ollama model over every scored case and repeat."""
    if not isinstance(runs, int) or not 1 <= runs <= 50:
        raise EvaluationRunError("runs must be an integer from 1 through 50.")
    if not isinstance(model, str) or not model.strip():
        raise EvaluationRunError("A non-empty Ollama model name is required.")
    model = validate_model_identifier(model, provider="ollama")

    import ollama
    from hardware_profile import (
        call_ollama_chat_maybe_no_think,
        is_reasoning_model_name,
        strip_reasoning_think_blocks,
    )

    rule, base_url = _resolve_local_ollama_rule_and_url()
    ollama_client = ollama.Client(
        host=base_url,
        timeout=240.0 if is_reasoning_model_name(model) else 180.0,
        follow_redirects=False,
        trust_env=False,
    )

    decision_log = Path(ATOMS_DIR).parent / "0-raw" / "archive" / "decision-record.jsonl"
    dataset_bytes = dataset_path.read_bytes()
    corpus = validate_dataset(
        json.loads(dataset_bytes.decode("utf-8")),
        atoms_dir=ATOMS_DIR,
        approval_check=lambda path: approved_revision_is_current(
            str(path), decision_log_path=str(decision_log), atoms_dir=ATOMS_DIR,
        ),
    )
    scored_cases = [case for case in corpus["queries"] if case["answerability"] in {"answerable", "unanswerable"}]
    observations: list[dict[str, Any]] = []
    for run_number in range(1, runs + 1):
        for case in scored_cases:
            started = time.perf_counter()
            case_projects = (str(case["project_scope"]).strip(),) if case.get("project_scope") else ()
            try:
                direct, neighbors = search_memory(
                    case["query"],
                    RecallOptions(n_results=5, follow_links=True, projects=case_projects),
                )
            except Exception as exc:
                raise EvaluationRunError(f"Trusted recall failed for {case['id']}: {exc}") from exc
            retrieved = direct + neighbors
            retrieval_ms = (time.perf_counter() - started) * 1000
            if not retrieved:
                observations.append({
                    "case_id": case["id"], "run": run_number, "claims": [], "abstained": True,
                    "abstention_reason": "no_approved_context", "invalid_draft": False,
                    "citation_count": 0, "retrieved_atom_ids": [],
                    "retrieval_ms": round(retrieval_ms, 3), "end_to_end_ms": round(retrieval_ms, 3),
                })
                continue

            prompt = build_grounding_prompt(
                AnswerRequest(
                    query=case["query"],
                    result_limit=5,
                    project_scope=str(case["project_scope"]).strip() if case.get("project_scope") else None,
                ),
                retrieved,
            )
            redaction, pii_placeholder_map = _redact_outbound_with_map(
                prompt,
                redact_pii=rule.redact_pii,
                fail_on_secret_detection=rule.fail_on_secret_detection,
            )
            try:
                if _is_monkeypatched_ollama_fn(ollama.chat, _DEFAULT_OLLAMA_CHAT):
                    response = call_ollama_chat_maybe_no_think(
                        ollama.chat,
                        model=model,
                        messages=[{"role": "user", "content": redaction.sanitized_text}],
                        format=ANSWER_DRAFT_SCHEMA,
                        stream=False,
                    )
                else:
                    response = call_ollama_chat_maybe_no_think(
                        ollama_client.chat,
                        model=model,
                        messages=[{"role": "user", "content": redaction.sanitized_text}],
                        format=ANSWER_DRAFT_SCHEMA,
                        stream=False,
                    )
                raw_content = strip_reasoning_think_blocks(str(response["message"]["content"])) or "{}"
                draft_json = _rehydrate_draft_claims(raw_content, pii_placeholder_map)
            except Exception as exc:
                raise EvaluationRunError(f"Generation failed for {case['id']} run {run_number}: {exc}") from exc
            try:
                answer = validate_standard_answer(draft_json, retrieved, atoms_dir=ATOMS_DIR)
            except Exception as exc:
                raise EvaluationRunError(f"Answer validation failed for {case['id']} run {run_number}: {exc}") from exc
            observations.append({
                "case_id": case["id"], "run": run_number,
                "claims": [claim.text for claim in answer.claims],
                "abstained": not answer.answerable,
                "abstention_reason": answer.reason,
                "invalid_draft": answer.reason not in {
                    None, "The model did not provide a supported claim."
                },
                "citation_count": sum(len(claim.evidence) for claim in answer.claims),
                "retrieved_atom_ids": [item.filename for item in retrieved],
                "retrieved_atom_revisions": [
                    {"atom_id": item.filename,
                     "revision_sha256": hashlib.sha256(item.content.encode("utf-8")).hexdigest()}
                    for item in retrieved
                ],
                "retrieval_ms": round(retrieval_ms, 3),
                "end_to_end_ms": round((time.perf_counter() - started) * 1000, 3),
            })

    manifest = load_index_manifest(CHROMA_PATH)
    lexical_current = lexical_index_is_current(ATOMS_DIR, LEXICAL_INDEX_PATH)
    payload = {
        "schema_version": 1,
        "evaluation_type": "repeated_answer_grounding",
        "dataset_id": corpus["dataset_id"],
        "dataset_version": corpus["dataset_version"],
        "dataset_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
        "model": model,
        "retrieval_backend": "lexical" if lexical_current else "semantic",
        "index_generation": manifest.get("generation_id") if isinstance(manifest, dict) else None,
        "requested_runs_per_case": runs,
        "scored_case_count": len(scored_cases),
        "observations": observations,
        "metrics": score_evaluation(corpus, observations),
    }
    payload["quality_gate"] = evaluate_quality_gate(payload["metrics"])
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3, help="Complete independent outputs per scored case (1-50).")
    parser.add_argument("--model", default=GEN_MODEL, help="Installed Ollama generation model.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH), help="Local JSON result path (default is ignored brain sidecar).")
    args = parser.parse_args(argv)
    try:
        result = run_evaluation(runs=args.runs, model=args.model)
        _write_json_atomically(Path(args.output), result)
    except Exception as exc:
        print(f"Answer evaluation unavailable: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result["metrics"], indent=2))
    print(f"Saved answer evaluation: {args.output}")
    if not result["quality_gate"]["passed"]:
        print("Answer evaluation quality gate failed: " + "; ".join(result["quality_gate"]["failures"]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
