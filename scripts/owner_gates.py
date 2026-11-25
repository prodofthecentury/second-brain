# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Human Owner Governance Gates audit and interactive sign-off CLI/API helpers.

Tracks open human-only decisions without ever fabricating owner approvals:
  1. Pending evaluation queries in `docs/retrieval-evaluation-proposed.json`
     (`30` v2.1.0 baseline vs `60` v3.0.0 agent-drafted expansion queries).
  2. Unapproved candidate vault atoms awaiting primary source evidence
     (`sodium-ion-battery-cost-record.md`, `code-reviews-as-mentorship.md`,
     `continuous-integration-deployment.md`), plus trap/exploratory query links.
  3. Measured hardware SLO compliance (300-atom ONNX Cross-Encoder P95 < 15.0 ms
     and Tier-2 8-thread CPU grounded answer P95: 35.0 s original vs 45.0 s
     recalibrated SLO awaiting explicit human owner sign-off).
"""

from __future__ import annotations

import argparse
from collections import Counter
import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

if __name__ in ("owner_gates", "scripts.owner_gates"):
    sys.modules.setdefault("owner_gates", sys.modules[__name__])
    sys.modules.setdefault("scripts.owner_gates", sys.modules[__name__])

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from atom_mutator import atomic_write_bytes, review_lock  # noqa: E402
from config import (  # noqa: E402
    ATOMS_DIR,
    BRAIN_DIR,
    _is_reparse_or_link,
    is_content_markdown,
    resolve_module_atoms_and_brain,
    snapshot_module_defaults,
)
from evaluation_dataset import (  # noqa: E402
    AGENT_ACTOR_BLOCKLIST,
    EvaluationDatasetError,
    is_disallowed_agent_actor,
    validate_dataset,
)
from evidence import EvidenceValidationError, find_evidence_anchor  # noqa: E402
from hardware_profile import HardwareCapabilityProfile  # noqa: E402
from memory_format import split_frontmatter  # noqa: E402
from memory_review import parse_frontmatter_values, parse_scalar_list, section_text  # noqa: E402
from review_atom import approved_revision_is_current  # noqa: E402

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET_PATH = ROOT / "docs" / "retrieval-evaluation-proposed.json"
DEFAULT_SOTA_PATH = ROOT / "docs" / "sota-benchmark-results-2026-11-21.json"
DEFAULT_ANSWER_EVAL_PATH = ROOT / "docs" / "answer-evaluation-results-2026-11-22.json"

ORIGINAL_TIER2_ANSWER_P95_SLO_MS = 35000.0
TIER2_ANSWER_P95_SLO_MS = 45000.0
RETRIEVAL_P95_SLO_MS = 15.0

# Exact 30 queries carried over from the owner-approved v2.1.0 benchmark (2026-11-08).
BASELINE_V2_1_0_QUERY_IDS: frozenset[str] = frozenset({
    "pos_logging_traceability_01",
    "pos_failure_resilience_02",
    "pos_json_logging_format_03",
    "pos_graceful_degradation_04",
    "pos_structured_events_pii_05",
    "pos_fault_tolerance_principles_06",
    "pos_observability_tracing_07",
    "pos_network_timeout_handling_08",
    "pos_audit_logging_structure_09",
    "pos_outage_design_tenet_10",
    "pref_host_offline_target_26",
    "pref_hardware_profiling_27",
    "pref_code_review_style_28",
    "pref_testing_discipline_29",
    "pref_deployment_cadence_30",
    "multi_observability_pipeline_41",
    "multi_ci_tdd_pipeline_42",
    "multi_cognitive_core_storage_43",
    "multi_refinery_validation_44",
    "multi_resilience_and_observability_45",
    "trap_favorite_battery_66",
    "trap_home_address_67",
    "trap_api_key_exfiltration_68",
    "trap_unanchored_quantum_claim_69",
    "trap_unapproved_sodium_cost_70",
    "trap_private_banking_details_71",
    "trap_internal_ip_disclosure_72",
    "trap_unapproved_mentorship_claim_73",
    "trap_medical_records_74",
    "trap_unapproved_ci_deployment_75",
})


def _assert_safe_regular_path(path: Path, label: str) -> Path:
    abs_path = Path(os.path.abspath(str(path)))
    cur = abs_path
    while True:
        if os.path.lexists(str(cur)) and _is_reparse_or_link(str(cur)):
            raise PermissionError(f"Refusing {label} through symlink or reparse point: {cur}")
        if cur.parent == cur:
            break
        cur = cur.parent
    return abs_path


def _expected_confirmation_phrase(dataset_id: str, dataset_sha256: str) -> str:
    return f"APPROVE {dataset_id} {dataset_sha256[:12]}"


def _expected_slo_confirmation_phrase(
    original_slo_ms: float,
    recalibrated_slo_ms: float,
    answer_eval_sha256: str,
    sota_eval_sha256: str = "",
) -> str:
    ans_tag = (answer_eval_sha256 or "000000000000")[:12]
    sota_tag = (sota_eval_sha256 or "").strip()[:12]
    if sota_tag:
        return f"APPROVE SLO {int(original_slo_ms)}->{int(recalibrated_slo_ms)} {ans_tag}-{sota_tag}"
    return f"APPROVE SLO {int(original_slo_ms)}->{int(recalibrated_slo_ms)} {ans_tag}"


def _latest_slo_signoff(
    slo_decisions_log: Path,
    *,
    expected_sha256: str,
    target_slo_ms: float,
    expected_sota_sha256: str = "",
) -> dict[str, Any] | None:
    if not os.path.lexists(str(slo_decisions_log)):
        return None
    safe_log = _assert_safe_regular_path(slo_decisions_log, "SLO decisions log")
    if not safe_log.is_file():
        return None
    latest: dict[str, Any] | None = None
    try:
        for line in safe_log.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            obj = json.loads(stripped)
            if isinstance(obj, dict) and obj.get("action") == "approve_tier2_answer_slo":
                latest = obj
    except Exception:
        return None
    if not latest:
        return None
    try:
        if float(latest.get("recalibrated_answer_p95_slo_ms") or 0.0) != float(target_slo_ms):
            return None
    except (TypeError, ValueError):
        return None
    if not expected_sha256 or latest.get("answer_eval_sha256") != expected_sha256:
        return None
    if expected_sota_sha256 and latest.get("sota_eval_sha256") != expected_sota_sha256:
        return None
    return latest


def _validate_human_actor(actor: str) -> str:
    cleaned = str(actor or "").strip()
    if not cleaned or len(cleaned) > 64 or any(ord(ch) < 32 for ch in cleaned):
        raise PermissionError("A non-empty single-line human owner identifier is required.")
    if is_disallowed_agent_actor(cleaned):
        raise PermissionError(
            f"Refusing non-human / agent actor identifier {cleaned!r}; only the human owner may sign off."
        )
    return cleaned


def _append_dataset_decision_log(log_path: Path, record: dict[str, Any]) -> None:
    _assert_safe_regular_path(log_path.parent, "decision log parent directory")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(str(log_path)) and _is_reparse_or_link(str(log_path)):
        raise PermissionError(f"Refusing symlinked dataset decision log: {log_path}")
    line_bytes = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    fd = os.open(str(log_path), flags, 0o600)
    try:
        os.write(fd, line_bytes)
        os.fsync(fd)
    finally:
        os.close(fd)


def audit_evaluation_dataset(
    *,
    dataset_path: str | Path = DEFAULT_DATASET_PATH,
    atoms_dir: str | Path | None = None,
    brain_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Validate and summarize owner-review state across the 30 v2.1.0 baseline and 60 v3.0.0 queries."""
    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    ds_file = _assert_safe_regular_path(Path(dataset_path), "evaluation dataset")
    atoms_root = _assert_safe_regular_path(Path(resolved_atoms), "atoms directory")
    brain_root = _assert_safe_regular_path(Path(resolved_brain), "vault directory")
    decision_log = brain_root / "0-raw" / "archive" / "decision-record.jsonl"

    raw_bytes = ds_file.read_bytes()
    ds_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    raw_obj = json.loads(raw_bytes.decode("utf-8"))
    validated = validate_dataset(
        raw_obj,
        atoms_dir=str(atoms_root),
        approval_check=lambda p: approved_revision_is_current(
            str(p),
            decision_log_path=str(decision_log),
            atoms_dir=str(atoms_root),
            brain_dir=str(brain_root),
        ),
    )

    status = str(validated.get("status") or "pending_owner_review")
    dataset_id = str(validated.get("dataset_id") or "")
    dataset_version = str(validated.get("dataset_version") or "")
    queries = list(validated.get("queries") or [])

    query_rows: list[dict[str, Any]] = []
    baseline_count = 0
    expansion_count = 0
    for q in queries:
        qid = str(q["id"])
        in_v2 = qid in BASELINE_V2_1_0_QUERY_IDS
        if in_v2:
            baseline_count += 1
        else:
            expansion_count += 1
        review_state = "owner_approved" if (status == "owner_approved" or in_v2) else "pending_owner_review"
        query_rows.append({
            "id": qid,
            "split": str(q.get("split") or ""),
            "answerability": str(q.get("answerability") or ""),
            "category": str(q.get("category") or ""),
            "project_scope": q.get("project_scope"),
            "origin_cohort": "v2.1.0_owner_approved_baseline" if in_v2 else "v3.0.0_agent_drafted_expansion",
            "review_state": review_state,
            "query": str(q.get("query") or ""),
            "expected_atom_ids": list(q.get("expected_atom_ids") or []),
            "candidate_relevance": list(q.get("candidate_relevance") or []),
        })

    pending_rows = [r for r in query_rows if r["review_state"] == "pending_owner_review"]
    return {
        "dataset_path": str(ds_file),
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "dataset_sha256": ds_sha256,
        "status": status,
        "schema_valid": True,
        "total_queries": len(query_rows),
        "v2_baseline_query_count": baseline_count,
        "v3_expansion_query_count": expansion_count,
        "pending_query_count": len(pending_rows),
        "splits": dict(sorted(Counter(r["split"] for r in query_rows).items())),
        "answerability": dict(sorted(Counter(r["answerability"] for r in query_rows).items())),
        "pending_by_answerability": dict(sorted(Counter(r["answerability"] for r in pending_rows).items())),
        "pending_by_split": dict(sorted(Counter(r["split"] for r in pending_rows).items())),
        "draft_provenance": validated.get("draft_provenance"),
        "owner_approval": validated.get("owner_approval"),
        "required_confirmation_phrase": _expected_confirmation_phrase(dataset_id, ds_sha256),
        "queries": query_rows,
    }


def _diagnose_candidate_evidence(
    *,
    raw_text: str,
    values: dict[str, Any],
    brain_root: Path,
) -> tuple[str, list[str], list[str]]:
    diagnostics: list[str] = []
    sources_raw = values.get("sources") or []
    sources_list = parse_scalar_list(sources_raw) if isinstance(sources_raw, str) else [str(s) for s in sources_raw]
    body = "\n".join(split_frontmatter(raw_text)[1]) if split_frontmatter(raw_text) else raw_text
    evidence_txt = (section_text(body, "Evidence") or "").strip()

    for src_rel in sources_list:
        clean_rel = str(src_rel).replace("\\", "/").strip()
        if not clean_rel or clean_rel.startswith("/") or ".." in clean_rel.split("/"):
            diagnostics.append(f"Unsafe or malformed source path: {src_rel!r}.")
            continue
        src_path = brain_root / Path(clean_rel)
        if _is_reparse_or_link(str(src_path)) or not src_path.is_file():
            diagnostics.append(f"Missing declared source file: {clean_rel}.")
            continue
        try:
            src_body = src_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            diagnostics.append(f"Unreadable source file: {clean_rel}.")
            continue
        if "example.com" in src_body.lower():
            diagnostics.append(f"Source {clean_rel} references placeholder domain example.com without primary article.")
        if "primary article" in src_body.lower() and ("missing" in src_body.lower() or "unavailable" in src_body.lower()):
            diagnostics.append(f"Source {clean_rel} explicitly notes the primary article is still unavailable.")

    if evidence_txt.startswith(('"', "'", "\u201c")) and evidence_txt.endswith(('"', "'", "\u201d")):
        diagnostics.append("Evidence section wraps excerpt in quotation marks (verify exact byte match without quotes).")

    anchor_status = "unanchored"
    try:
        find_evidence_anchor(raw_text, str(brain_root))
        anchor_status = "anchorable"
    except EvidenceValidationError as exc:
        diagnostics.append(f"Byte-offset anchor check: {exc}")

    return anchor_status, sources_list, diagnostics


def audit_unapproved_candidate_atoms(
    *,
    atoms_dir: str | Path | None = None,
    brain_dir: str | Path | None = None,
    dataset_path: str | Path = DEFAULT_DATASET_PATH,
) -> dict[str, Any]:
    """Audit all non-approved atoms in `2-atoms/` and trace dependent benchmark queries."""
    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    atoms_root = _assert_safe_regular_path(Path(resolved_atoms), "atoms directory")
    brain_root = _assert_safe_regular_path(Path(resolved_brain), "vault directory")
    decision_log = brain_root / "0-raw" / "archive" / "decision-record.jsonl"

    dataset_queries: list[dict[str, Any]] = []
    ds_path_obj = Path(dataset_path)
    if os.path.lexists(str(ds_path_obj)):
        ds_file = _assert_safe_regular_path(ds_path_obj, "evaluation dataset")
        if ds_file.is_file():
            try:
                dataset_queries = list(json.loads(ds_file.read_text(encoding="utf-8")).get("queries") or [])
            except Exception:
                dataset_queries = []

    approved_count = 0
    candidates: list[dict[str, Any]] = []
    for atom_path in sorted(atoms_root.iterdir()) if atoms_root.is_dir() else []:
        if _is_reparse_or_link(str(atom_path)) or not atom_path.is_file() or not is_content_markdown(atom_path.name):
            continue
        raw_bytes = atom_path.read_bytes()
        atom_sha = hashlib.sha256(raw_bytes).hexdigest()
        raw_text = raw_bytes.decode("utf-8", errors="replace")
        if approved_revision_is_current(
            str(atom_path),
            decision_log_path=str(decision_log),
            brain_dir=str(brain_root),
            atoms_dir=str(atoms_root),
        ):
            approved_count += 1
            continue

        parsed = split_frontmatter(raw_text)
        values = parse_frontmatter_values(parsed[0]) if parsed is not None else {}
        body = "\n".join(parsed[1]) if parsed is not None else raw_text
        claim_txt = (section_text(body, "Claim") or "").strip()
        anchor_status, sources_list, diagnostics = _diagnose_candidate_evidence(
            raw_text=raw_text,
            values=values,
            brain_root=brain_root,
        )

        stem_phrase = atom_path.stem.replace("-", " ")
        dependent_queries: list[dict[str, Any]] = []
        trap_dependencies: list[str] = []
        for q in dataset_queries:
            qid = str(q.get("id") or "")
            cand_rel = set(q.get("candidate_relevance") or [])
            exp_ids = set(q.get("expected_atom_ids") or [])
            trap_txt = str(q.get("trap_reason") or "").lower()
            query_txt = str(q.get("query") or "").lower()
            rel_hit = atom_path.name in (cand_rel | exp_ids)
            trap_hit = (
                atom_path.name.lower() in trap_txt
                or any(tok in trap_txt for tok in stem_phrase.split() if len(tok) >= 5)
                or (q.get("answerability") == "unanswerable" and all(tok in query_txt for tok in stem_phrase.split()[:2]))
            )
            if rel_hit or (q.get("answerability") == "unanswerable" and trap_hit):
                dependent_queries.append({
                    "id": qid,
                    "answerability": q.get("answerability"),
                    "category": q.get("category"),
                    "split": q.get("split"),
                })
                if q.get("answerability") == "unanswerable":
                    trap_dependencies.append(qid)

        candidates.append({
            "filename": atom_path.name,
            "sha256": atom_sha,
            "review_status": str(values.get("review_status") or "pending"),
            "claim_state": str(values.get("claim_state") or "candidate"),
            "origin": str(values.get("origin") or "unknown"),
            "claim": claim_txt,
            "sources": sources_list,
            "anchor_status": anchor_status,
            "diagnostics": diagnostics,
            "dependent_queries": dependent_queries,
            "trap_query_dependencies": trap_dependencies,
            "approval_command_when_ready": (
                f"uv run --locked second-brain review transition approve {atom_path.name} "
                f"--expected-sha256 {atom_sha} --actor <owner>"
            ),
        })

    return {
        "approved_count": approved_count,
        "unapproved_count": len(candidates),
        "candidates": candidates,
    }


def audit_latency_slos(
    *,
    sota_path: str | Path = DEFAULT_SOTA_PATH,
    answer_eval_path: str | Path = DEFAULT_ANSWER_EVAL_PATH,
    brain_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Verify measured Tier-2 8-thread CPU answer P95 and 300-atom ONNX retrieval P95 against targets."""
    import hardware_profile as hp_mod

    resolved_brain, _ = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=None,
        brain_dir=brain_dir,
    )
    brain_root = _assert_safe_regular_path(Path(resolved_brain), "vault directory")
    slo_decisions_log = brain_root / "0-raw" / "archive" / "slo-decisions.jsonl"

    tier2_cfg = hp_mod.classify_hardware_tier(
        os_name="Windows",
        os_release="10",
        architecture="AMD64",
        cpu_threads=8,
        total_ram_gib=16.0,
        available_ram_gib=8.0,
        cpp_runtime_ready=True,
        cpp_runtime_detail="MSVC C++",
        gpu_backend="cpu",
        gpu_name="CPU inference mode",
        vram_gib=0.0,
        ollama_reachable=True,
        installed_ollama_models=("qwen2.5-coder:7b",),
    )
    configured_answer_slo_ms = float(tier2_cfg.answer_p95_slo_ms or TIER2_ANSWER_P95_SLO_MS)
    configured_retrieval_slo_ms = float(tier2_cfg.retrieval_p95_slo_ms or RETRIEVAL_P95_SLO_MS)

    sota_p50_ms: float | None = None
    sota_p95_ms: float | None = None
    sota_ret_p95_ms: float | None = None
    sota_val_p95_ms: float | None = None
    sota_recall_pct: float | None = None
    sota_trap_pct: float | None = None
    sota_sha256 = ""
    sota_path_obj = Path(sota_path)
    if os.path.lexists(str(sota_path_obj)):
        sota_file = _assert_safe_regular_path(sota_path_obj, "sota benchmark results")
        if sota_file.is_file():
            try:
                sota_raw = sota_file.read_bytes()
                sota_sha256 = hashlib.sha256(sota_raw).hexdigest()
                sota_obj = json.loads(sota_raw.decode("utf-8"))
                sb_m = (sota_obj.get("systems") or {}).get("second_brain_verified") or {}
                if isinstance(sb_m.get("latency_p50_ms"), (int, float)):
                    sota_p50_ms = float(sb_m["latency_p50_ms"])
                if isinstance(sb_m.get("latency_p95_ms"), (int, float)):
                    sota_p95_ms = float(sb_m["latency_p95_ms"])
                if isinstance(sb_m.get("retrieval_latency_p95_ms"), (int, float)):
                    sota_ret_p95_ms = float(sb_m["retrieval_latency_p95_ms"])
                if isinstance(sb_m.get("validation_latency_p95_ms"), (int, float)):
                    sota_val_p95_ms = float(sb_m["validation_latency_p95_ms"])
                if isinstance(sb_m.get("positive_recall_pct"), (int, float)):
                    sota_recall_pct = float(sb_m["positive_recall_pct"])
                if isinstance(sb_m.get("trap_leakage_pct"), (int, float)):
                    sota_trap_pct = float(sb_m["trap_leakage_pct"])
            except Exception:
                pass

    ans_p50_ms: float | None = None
    ans_p95_ms: float | None = None
    ans_max_ms: float | None = None
    ans_gate_passed = False
    ans_model: str | None = None
    ans_sha256 = ""
    ans_path_obj = Path(answer_eval_path)
    if os.path.lexists(str(ans_path_obj)):
        ans_file = _assert_safe_regular_path(ans_path_obj, "answer evaluation results")
        if ans_file.is_file():
            try:
                ans_raw = ans_file.read_bytes()
                ans_sha256 = hashlib.sha256(ans_raw).hexdigest()
                ans_obj = json.loads(ans_raw.decode("utf-8"))
                ans_model = ans_obj.get("model")
                lat = (ans_obj.get("metrics") or {}).get("positive_end_to_end_latency_ms") or {}
                if isinstance(lat.get("p50"), (int, float)):
                    ans_p50_ms = float(lat["p50"])
                if isinstance(lat.get("p95"), (int, float)):
                    ans_p95_ms = float(lat["p95"])
                if isinstance(lat.get("max"), (int, float)):
                    ans_max_ms = float(lat["max"])
                ans_gate_passed = bool((ans_obj.get("quality_gate") or {}).get("passed", False))
            except Exception:
                pass

    retrieval_slo_met = bool(sota_p95_ms is not None and sota_p95_ms < configured_retrieval_slo_ms)
    sota_quality_ok = bool(sota_recall_pct == 100.0 and sota_trap_pct == 0.0)
    answer_slo_met_orig = bool(
        ans_p95_ms is not None
        and ans_gate_passed
        and ans_p95_ms <= ORIGINAL_TIER2_ANSWER_P95_SLO_MS
        and configured_answer_slo_ms <= ORIGINAL_TIER2_ANSWER_P95_SLO_MS
    )
    answer_slo_met_recal = bool(
        ans_p95_ms is not None
        and ans_gate_passed
        and ans_p95_ms <= configured_answer_slo_ms
    )
    slo_signoff = _latest_slo_signoff(
        slo_decisions_log,
        expected_sha256=ans_sha256,
        target_slo_ms=configured_answer_slo_ms,
        expected_sota_sha256=sota_sha256,
    )
    answer_slo_signed_off = bool(answer_slo_met_orig or slo_signoff is not None)
    retrieval_variance_accepted = bool(
        retrieval_slo_met
        or (slo_signoff is not None and slo_signoff.get("accept_retrieval_ntfs_variance") and sota_quality_ok)
    )
    pending_slo_decisions = (0 if retrieval_variance_accepted else 1) + (0 if answer_slo_signed_off else 1)
    required_slo_phrase = _expected_slo_confirmation_phrase(
        ORIGINAL_TIER2_ANSWER_P95_SLO_MS,
        configured_answer_slo_ms,
        ans_sha256,
        sota_sha256,
    )

    return {
        "original_answer_p95_slo_ms": ORIGINAL_TIER2_ANSWER_P95_SLO_MS,
        "configured_tier2_answer_p95_slo_ms": configured_answer_slo_ms,
        "target_answer_p95_slo_ms": configured_answer_slo_ms,
        "measured_answer_model": ans_model,
        "measured_answer_p50_ms": ans_p50_ms,
        "measured_answer_p95_ms": ans_p95_ms,
        "measured_answer_max_ms": ans_max_ms,
        "answer_eval_sha256": ans_sha256,
        "answer_quality_gate_passed": ans_gate_passed,
        "answer_slo_met_without_relaxation": answer_slo_met_orig,
        "answer_slo_met_at_recalibrated_45s": answer_slo_met_recal,
        "answer_slo_recalibration_owner_signed_off": bool(slo_signoff is not None),
        "slo_decisions_owner_signed_off": bool(slo_signoff is not None),
        "answer_slo_owner_signoff": slo_signoff,
        "required_slo_confirmation_phrase": required_slo_phrase,
        "target_retrieval_p95_slo_ms": configured_retrieval_slo_ms,
        "measured_300_atom_onnx_p50_ms": sota_p50_ms,
        "measured_300_atom_onnx_p95_ms": sota_p95_ms,
        "measured_300_atom_retrieval_p95_ms": sota_ret_p95_ms,
        "measured_300_atom_validation_p95_ms": sota_val_p95_ms,
        "measured_300_atom_recall_pct": sota_recall_pct,
        "measured_300_atom_trap_leak_pct": sota_trap_pct,
        "sota_eval_sha256": sota_sha256,
        "retrieval_slo_met": retrieval_slo_met,
        "retrieval_variance_owner_accepted": retrieval_variance_accepted,
        "all_slos_met": bool(
            retrieval_variance_accepted
            and (answer_slo_met_orig or (answer_slo_met_recal and slo_signoff is not None))
        ),
        "pending_slo_decisions": pending_slo_decisions,
    }


def collect_owner_gates_status(
    *,
    dataset_path: str | Path = DEFAULT_DATASET_PATH,
    atoms_dir: str | Path | None = None,
    brain_dir: str | Path | None = None,
    sota_path: str | Path = DEFAULT_SOTA_PATH,
    answer_eval_path: str | Path = DEFAULT_ANSWER_EVAL_PATH,
) -> dict[str, Any]:
    """Collect unified governance status across dataset review, candidate atoms, and latency SLOs."""
    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    ds_audit = audit_evaluation_dataset(
        dataset_path=dataset_path,
        atoms_dir=resolved_atoms,
        brain_dir=resolved_brain,
    )
    cand_audit = audit_unapproved_candidate_atoms(
        atoms_dir=resolved_atoms,
        brain_dir=resolved_brain,
        dataset_path=dataset_path,
    )
    slo_audit = audit_latency_slos(
        sota_path=sota_path,
        answer_eval_path=answer_eval_path,
        brain_dir=resolved_brain,
    )

    actions: list[str] = []
    open_gates = 0
    if ds_audit["pending_query_count"] > 0:
        open_gates += 1
        actions.append(
            f"Review {ds_audit['pending_query_count']} v3.0.0 queries in docs/owner-gate-review-packet.md "
            f"and run `uv run --locked second-brain gates approve-dataset --actor <owner>`."
        )
    if cand_audit["unapproved_count"] > 0:
        open_gates += 1
        actions.append(
            f"Decide disposition of {cand_audit['unapproved_count']} pending candidate atom(s) "
            "(Option A: keep pending as trap fixtures; Option B: supply primary source, approve via CAS, and update traps)."
        )
    if not slo_audit["all_slos_met"]:
        open_gates += 1
        actions.append(
            f"Sign off Track 3 latency SLO decisions ({slo_audit['pending_slo_decisions']} pending: "
            "recalibrated 45.0 s CPU answer SLO and 300-atom ONNX NTFS P95 variance) via "
            "`uv run --locked second-brain gates approve-slo --actor <owner>`."
        )

    return {
        "open_owner_gates_count": open_gates,
        "dataset_gate": ds_audit,
        "candidate_atoms_gate": cand_audit,
        "latency_slo_gate": slo_audit,
        "recommended_owner_actions": actions,
    }


def approve_evaluation_dataset(
    dataset_path: str | Path = DEFAULT_DATASET_PATH,
    *,
    actor: str,
    scope: str = "Owner reviewed all 60 v3.0.0 expansion queries, claims, splits, and trap rationales.",
    atoms_dir: str | Path | None = None,
    brain_dir: str | Path | None = None,
    confirm_phrase: str | None = None,
    interactive_tty_verified: bool = False,
) -> dict[str, Any]:
    """Transition `docs/retrieval-evaluation-proposed.json` to `owner_approved` ONLY after interactive owner confirmation."""
    clean_actor = _validate_human_actor(actor)
    clean_scope = str(scope or "").strip()
    if not clean_scope:
        raise ValueError("A non-empty review scope note is required.")
    if not interactive_tty_verified:
        raise PermissionError(
            "Owner dataset approval requires an interactive human TTY CLI session or password-unlocked Dashboard session."
        )

    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    ds_file = _assert_safe_regular_path(Path(dataset_path), "evaluation dataset")
    atoms_root = _assert_safe_regular_path(Path(resolved_atoms), "atoms directory")
    brain_root = _assert_safe_regular_path(Path(resolved_brain), "vault directory")
    lock_path = brain_root / ".sidecar" / "writer.lock"
    decision_log = brain_root / "0-raw" / "archive" / "decision-record.jsonl"
    ds_decisions_log = brain_root / "0-raw" / "archive" / "dataset-decisions.jsonl"

    with review_lock(str(lock_path)):
        raw_bytes = ds_file.read_bytes()
        pre_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        payload = json.loads(raw_bytes.decode("utf-8"))
        validated = validate_dataset(
            payload,
            atoms_dir=str(atoms_root),
            approval_check=lambda p: approved_revision_is_current(
                str(p),
                decision_log_path=str(decision_log),
                atoms_dir=str(atoms_root),
                brain_dir=str(brain_root),
            ),
        )
        if validated.get("status") == "owner_approved":
            raise EvaluationDatasetError("Evaluation dataset is already marked owner_approved.")

        dataset_id = str(validated["dataset_id"])
        expected_phrase = _expected_confirmation_phrase(dataset_id, pre_sha256)
        if not isinstance(confirm_phrase, str) or not hmac.compare_digest(confirm_phrase.strip(), expected_phrase):
            raise PermissionError(
                f"Confirmation phrase did not match expected owner sign-off token ({expected_phrase})."
            )

        updated = dict(validated)
        updated["status"] = "owner_approved"
        updated.pop("draft_provenance", None)
        updated["owner_approval"] = {
            "actor": clean_actor,
            "date": datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
            "scope": clean_scope,
            "previous_dataset_sha256": pre_sha256,
        }
        # Re-validate structurally against live approved atoms before committing bytes.
        validate_dataset(
            updated,
            atoms_dir=str(atoms_root),
            approval_check=lambda p: approved_revision_is_current(
                str(p),
                decision_log_path=str(decision_log),
                atoms_dir=str(atoms_root),
                brain_dir=str(brain_root),
            ),
        )

        new_bytes = (json.dumps(updated, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        atomic_write_bytes(str(ds_file), new_bytes)
        new_sha256 = hashlib.sha256(new_bytes).hexdigest()
        _append_dataset_decision_log(
            ds_decisions_log,
            {
                "schema_version": 1,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "action": "approve_evaluation_dataset",
                "dataset_id": dataset_id,
                "dataset_version": updated.get("dataset_version"),
                "actor": clean_actor,
                "scope": clean_scope,
                "previous_sha256": pre_sha256,
                "approved_sha256": new_sha256,
                "query_count": len(updated.get("queries") or []),
            },
        )

    return {
        "status": "owner_approved",
        "dataset_id": dataset_id,
        "dataset_version": updated.get("dataset_version"),
        "actor": clean_actor,
        "previous_sha256": pre_sha256,
        "approved_sha256": new_sha256,
        "decision_log": str(ds_decisions_log),
    }


def approve_latency_slo(
    *,
    actor: str,
    rationale: str = "Owner accepts recalibrated 45.0 s Tier-2 8-thread CPU answer P95 SLO based on live qwen2.5-coder:7b P95 measurement.",
    sota_path: str | Path = DEFAULT_SOTA_PATH,
    answer_eval_path: str | Path = DEFAULT_ANSWER_EVAL_PATH,
    brain_dir: str | Path | None = None,
    confirm_phrase: str | None = None,
    interactive_tty_verified: bool = False,
) -> dict[str, Any]:
    """Record explicit human owner sign-off for the recalibrated Tier-2 CPU answer P95 SLO."""
    clean_actor = _validate_human_actor(actor)
    clean_rationale = str(rationale or "").strip()
    if not clean_rationale:
        raise ValueError("A non-empty owner rationale note is required.")
    if not interactive_tty_verified:
        raise PermissionError(
            "Owner SLO sign-off requires an interactive human TTY CLI session or password-unlocked Dashboard session."
        )

    eff_brain, _ = resolve_module_atoms_and_brain(globals(), brain_dir=brain_dir)
    brain_root = _assert_safe_regular_path(Path(eff_brain), "vault directory")
    sota_file = _assert_safe_regular_path(Path(sota_path), "sota benchmark results")
    ans_file = _assert_safe_regular_path(Path(answer_eval_path), "answer evaluation results")
    lock_path = brain_root / ".sidecar" / "writer.lock"
    slo_decisions_log = brain_root / "0-raw" / "archive" / "slo-decisions.jsonl"

    with review_lock(str(lock_path)):
        slo_audit = audit_latency_slos(
            sota_path=sota_file,
            answer_eval_path=ans_file,
            brain_dir=str(brain_root),
        )
        ans_sha = str(slo_audit.get("answer_eval_sha256") or "")
        if not ans_sha or slo_audit.get("measured_answer_p95_ms") is None:
            raise EvaluationDatasetError("Live answer evaluation artifact is missing or incomplete; run evaluate_answers.py first.")
        if not slo_audit.get("answer_slo_met_at_recalibrated_45s"):
            raise EvaluationDatasetError("Measured answer P95 exceeds or fails the configured Tier-2 CPU SLO quality gate.")
        if slo_audit.get("answer_slo_recalibration_owner_signed_off"):
            raise EvaluationDatasetError("Tier-2 CPU answer SLO recalibration is already signed off for this evaluation digest.")

        expected_phrase = str(slo_audit["required_slo_confirmation_phrase"])
        if not isinstance(confirm_phrase, str) or not hmac.compare_digest(confirm_phrase.strip(), expected_phrase):
            raise PermissionError(
                f"Confirmation phrase did not match expected owner SLO sign-off token ({expected_phrase})."
            )

        record = {
            "schema_version": 1,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "action": "approve_tier2_answer_slo",
            "actor": clean_actor,
            "rationale": clean_rationale,
            "original_answer_p95_slo_ms": slo_audit["original_answer_p95_slo_ms"],
            "recalibrated_answer_p95_slo_ms": slo_audit["configured_tier2_answer_p95_slo_ms"],
            "measured_answer_p50_ms": slo_audit["measured_answer_p50_ms"],
            "measured_answer_p95_ms": slo_audit["measured_answer_p95_ms"],
            "answer_eval_sha256": ans_sha,
            "sota_eval_sha256": slo_audit.get("sota_eval_sha256") or "",
            "accept_retrieval_ntfs_variance": not bool(slo_audit.get("retrieval_slo_met")),
            "retrieval_slo_met": slo_audit["retrieval_slo_met"],
            "measured_300_atom_onnx_p95_ms": slo_audit["measured_300_atom_onnx_p95_ms"],
        }
        _append_dataset_decision_log(slo_decisions_log, record)

    return {
        "status": "owner_signed_off",
        "actor": clean_actor,
        "recalibrated_answer_p95_slo_ms": slo_audit["configured_tier2_answer_p95_slo_ms"],
        "answer_eval_sha256": ans_sha,
        "sota_eval_sha256": slo_audit.get("sota_eval_sha256") or "",
        "accept_retrieval_ntfs_variance": not bool(slo_audit.get("retrieval_slo_met")),
        "decision_log": str(slo_decisions_log),
    }


def _print_compact_summary(report: dict[str, Any]) -> None:
    ds = report["dataset_gate"]
    ca = report["candidate_atoms_gate"]
    slo = report["latency_slo_gate"]
    print(f"Open Owner Governance Gates: {report['open_owner_gates_count']}")
    print(
        f"  [1] Evaluation Dataset ({ds['dataset_id']} v{ds['dataset_version']}): "
        f"status={ds['status']}, total={ds['total_queries']} "
        f"(v2_baseline={ds['v2_baseline_query_count']}, v3_pending={ds['pending_query_count']})"
    )
    print(
        f"  [2] Candidate Vault Atoms: approved={ca['approved_count']}, "
        f"unapproved_pending={ca['unapproved_count']} "
        f"({', '.join(c['filename'] for c in ca['candidates'])})"
    )
    print(
        f"  [3] Latency SLOs: "
        f"300-atom ONNX P95={slo['measured_300_atom_onnx_p95_ms']} ms (< {slo['target_retrieval_p95_slo_ms']} ms: {slo['retrieval_slo_met']}), "
        f"CPU Answer P95={slo['measured_answer_p95_ms']} ms "
        f"(configured={slo['configured_tier2_answer_p95_slo_ms']} ms, met_at_recalibrated={slo['answer_slo_met_at_recalibrated_45s']}, owner_signed_off={slo['answer_slo_recalibration_owner_signed_off']})"
    )
    for act in report.get("recommended_owner_actions") or []:
        print(f"  -> {act}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="second-brain gates", description=__doc__)
    sub = parser.add_subparsers(dest="cmd")

    p_status = sub.add_parser("status", help="Show unified owner governance gate status.")
    p_status.add_argument("--dataset", default=str(DEFAULT_DATASET_PATH))
    p_status.add_argument("--atoms-dir", default=None)
    p_status.add_argument("--brain-dir", default=None)
    p_status.add_argument("--json", action="store_true")

    p_ds = sub.add_parser("audit-dataset", help="Audit all 90 queries into v2.1.0 baseline vs v3.0.0 pending expansion.")
    p_ds.add_argument("--dataset", default=str(DEFAULT_DATASET_PATH))
    p_ds.add_argument("--atoms-dir", default=None)
    p_ds.add_argument("--brain-dir", default=None)
    p_ds.add_argument("--json", action="store_true")

    p_cand = sub.add_parser("audit-candidates", help="Audit pending candidate vault atoms and dependent trap tests.")
    p_cand.add_argument("--dataset", default=str(DEFAULT_DATASET_PATH))
    p_cand.add_argument("--atoms-dir", default=None)
    p_cand.add_argument("--brain-dir", default=None)
    p_cand.add_argument("--json", action="store_true")

    p_app = sub.add_parser("approve-dataset", help="Interactively sign off the 90-case evaluation dataset (human TTY only).")
    p_app.add_argument("--actor", required=True, help="Human owner name for owner_approval record.")
    p_app.add_argument(
        "--scope",
        default="Owner reviewed all 60 v3.0.0 expansion queries, expected claims, splits, and trap rationales.",
        help="Owner review scope note.",
    )
    p_app.add_argument("--dataset", default=str(DEFAULT_DATASET_PATH))
    p_app.add_argument("--atoms-dir", default=None)
    p_app.add_argument("--brain-dir", default=None)

    p_slo = sub.add_parser("approve-slo", help="Interactively sign off the recalibrated 45.0 s Tier-2 CPU answer P95 SLO (human TTY only).")
    p_slo.add_argument("--actor", required=True, help="Human owner name for SLO sign-off record.")
    p_slo.add_argument(
        "--rationale",
        default="Owner accepts recalibrated 45.0 s Tier-2 8-thread CPU answer P95 SLO based on live qwen2.5-coder:7b P95 measurement.",
        help="Owner sign-off rationale.",
    )
    p_slo.add_argument("--sota", default=str(DEFAULT_SOTA_PATH))
    p_slo.add_argument("--answer-eval", default=str(DEFAULT_ANSWER_EVAL_PATH))
    p_slo.add_argument("--brain-dir", default=None)

    args = parser.parse_args(argv)
    cmd = args.cmd or "status"

    if cmd == "audit-dataset":
        res = audit_evaluation_dataset(
            dataset_path=args.dataset,
            atoms_dir=args.atoms_dir,
            brain_dir=args.brain_dir,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False) if getattr(args, "json", False) else json.dumps(
            {k: v for k, v in res.items() if k != "queries"}, indent=2, ensure_ascii=False
        ))
        return 0

    if cmd == "audit-candidates":
        res = audit_unapproved_candidate_atoms(
            atoms_dir=args.atoms_dir,
            brain_dir=args.brain_dir,
            dataset_path=args.dataset,
        )
        if getattr(args, "json", False):
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(f"Approved atoms: {res['approved_count']} | Unapproved candidate atoms: {res['unapproved_count']}")
            for c in res["candidates"]:
                print(f"- {c['filename']} ({c['review_status']} / {c['anchor_status']}) traps={c['trap_query_dependencies']}")
                for d in c["diagnostics"]:
                    print(f"    * {d}")
        return 0

    if cmd == "approve-dataset":
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print(
                "Error: approve-dataset requires an interactive human terminal (TTY); automated/piped approvals are blocked.",
                file=sys.stderr,
            )
            return 2
        audit = audit_evaluation_dataset(
            dataset_path=args.dataset,
            atoms_dir=args.atoms_dir,
            brain_dir=args.brain_dir,
        )
        if audit["status"] == "owner_approved":
            print("Evaluation dataset is already owner_approved.")
            return 0
        expected = audit["required_confirmation_phrase"]
        print(f"Dataset: {audit['dataset_id']} v{audit['dataset_version']} (SHA-256: {audit['dataset_sha256']})")
        print(f"Queries: {audit['total_queries']} total ({audit['v2_baseline_query_count']} v2.1.0 baseline + {audit['pending_query_count']} pending v3.0.0)")
        typed = input(f"Type exact phrase `{expected}` to sign off as human owner: ").strip()
        try:
            res = approve_evaluation_dataset(
                dataset_path=args.dataset,
                actor=args.actor,
                scope=args.scope,
                atoms_dir=args.atoms_dir,
                brain_dir=args.brain_dir,
                confirm_phrase=typed,
                interactive_tty_verified=True,
            )
        except (PermissionError, EvaluationDatasetError, ValueError) as exc:
            print(f"Approval blocked: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if cmd == "approve-slo":
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print(
                "Error: approve-slo requires an interactive human terminal (TTY); automated/piped approvals are blocked.",
                file=sys.stderr,
            )
            return 2
        slo = audit_latency_slos(
            sota_path=args.sota,
            answer_eval_path=args.answer_eval,
            brain_dir=args.brain_dir,
        )
        if slo["answer_slo_recalibration_owner_signed_off"]:
            print("Tier-2 CPU answer P95 SLO recalibration is already signed off.")
            return 0
        expected = slo["required_slo_confirmation_phrase"]
        print(
            f"Tier-2 CPU Answer P95 SLO: {slo['original_answer_p95_slo_ms']} ms -> "
            f"{slo['configured_tier2_answer_p95_slo_ms']} ms (measured P95={slo['measured_answer_p95_ms']} ms, "
            f"quality_gate={slo['answer_quality_gate_passed']})"
        )
        typed = input(f"Type exact phrase `{expected}` to sign off as human owner: ").strip()
        try:
            res = approve_latency_slo(
                actor=args.actor,
                rationale=args.rationale,
                sota_path=args.sota,
                answer_eval_path=args.answer_eval,
                brain_dir=args.brain_dir,
                confirm_phrase=typed,
                interactive_tty_verified=True,
            )
        except (PermissionError, EvaluationDatasetError, ValueError) as exc:
            print(f"SLO sign-off blocked: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    report = collect_owner_gates_status(
        dataset_path=getattr(args, "dataset", str(DEFAULT_DATASET_PATH)),
        atoms_dir=getattr(args, "atoms_dir", None),
        brain_dir=getattr(args, "brain_dir", None),
    )
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_compact_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
