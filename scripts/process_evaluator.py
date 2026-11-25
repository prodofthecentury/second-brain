# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""4D Process Evaluation Rubric, Proof Economy & Solver-Reviewer-Refiner (Epic 2E.3).

Implements:
- Weighted 4D Process Evaluation Rubric:
  * 40% Logical Validity (sound inference steps + sandbox/tool verification)
  * 30% Completeness (base cases, edge cases, domain bounds addressed)
  * 20% Correctness (final result verified via `math_equivalence.are_math_equivalent`)
  * 10% Clarity (explicit variable binding, units, step structure)
- Proof Economy telemetry persisted into disposable ``telemetry.sqlite3``.
- Role-routed ``Solver -> Reviewer -> Refiner`` loop with:
  * Strict ``max_rounds`` cap (`default 3`)
  * Anti-sycophancy guard (verified correct Solver output is NEVER overridden by an unverified critique)
  * Monotonic improvement guard.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from contextlib import closing
import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ in ("process_evaluator", "scripts.process_evaluator"):
    sys.modules.setdefault("process_evaluator", sys.modules[__name__])
    sys.modules.setdefault("scripts.process_evaluator", sys.modules[__name__])

from cognitive_core import init_telemetry_db, save_experience_record  # noqa: E402
from config import _is_reparse_or_link, resolve_module_atoms_and_brain  # noqa: E402
from math_equivalence import are_math_equivalent, extract_final_answer  # noqa: E402
from symbolic_sandbox import SandboxSecurityError, run_sandboxed_python  # noqa: E402

WEIGHT_VALIDITY = 0.40
WEIGHT_COMPLETENESS = 0.30
WEIGHT_CORRECTNESS = 0.20
WEIGHT_CLARITY = 0.10


def score_reasoning_trace(
    steps: list[str] | tuple[str, ...],
    *,
    final_answer: str = "",
    expected_answer: str | None = None,
    unit_warnings: list[str] | tuple[str, ...] = (),
    sandbox_checks: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    edge_cases_covered: bool = False,
    total_tokens_est: int = 0,
    wall_ms: float = 0.0,
) -> dict[str, Any]:
    """Compute weighted 4D Process Rubric + Proof Economy metrics."""
    clean_steps = [str(s or "").strip() for s in (steps or ()) if str(s or "").strip()]
    total_steps = max(1, len(clean_steps))
    passed_checks = sum(1 for c in (sandbox_checks or ()) if bool(c.get("ok")))
    failed_checks = sum(1 for c in (sandbox_checks or ()) if not bool(c.get("ok")))

    # 1. Logical Validity (40%)
    if unit_warnings or failed_checks > 0:
        validity = max(0.2, 0.7 - 0.25 * len(unit_warnings) - 0.2 * failed_checks)
    elif passed_checks > 0:
        validity = 1.0
    elif len(clean_steps) >= 2:
        validity = 0.85
    else:
        validity = 0.55

    # 2. Completeness (30%)
    completeness = 0.65
    if edge_cases_covered or any(k in " ".join(clean_steps).lower() for k in ("case", "bound", "domain", "assuming", "verify")):
        completeness = 0.95
    if len(clean_steps) < 2:
        completeness = min(completeness, 0.5)

    # 3. Correctness (20%)
    if expected_answer is not None and final_answer:
        correctness = 1.0 if are_math_equivalent(final_answer, expected_answer) else 0.0
    elif passed_checks > 0 and final_answer:
        correctness = 0.95
    elif final_answer:
        correctness = 0.7
    else:
        correctness = 0.1

    # 4. Clarity (10%)
    structured_steps = sum(1 for s in clean_steps if len(s) >= 12)
    clarity = min(1.0, round(0.4 + 0.6 * (structured_steps / total_steps), 4))

    weighted_total = round(
        WEIGHT_VALIDITY * validity
        + WEIGHT_COMPLETENESS * completeness
        + WEIGHT_CORRECTNESS * correctness
        + WEIGHT_CLARITY * clarity,
        4,
    )

    useful_steps = max(0, len(clean_steps) - failed_checks)
    proof_economy = {
        "useful_steps": useful_steps,
        "total_steps": len(clean_steps),
        "efficiency_ratio": round(useful_steps / total_steps, 4),
        "tool_calls": len(sandbox_checks or ()),
        "passed_tool_calls": passed_checks,
        "wall_ms": round(float(wall_ms), 2),
        "token_budget_est": int(total_tokens_est or sum(len(s.split()) for s in clean_steps)),
    }

    return {
        "weights": {
            "validity": WEIGHT_VALIDITY,
            "completeness": WEIGHT_COMPLETENESS,
            "correctness": WEIGHT_CORRECTNESS,
            "clarity": WEIGHT_CLARITY,
        },
        "scores": {
            "validity": round(validity, 4),
            "completeness": round(completeness, 4),
            "correctness": round(correctness, 4),
            "clarity": round(clarity, 4),
        },
        "weighted_total": weighted_total,
        "proof_economy": proof_economy,
    }


def record_proof_economy_telemetry(
    problem_id: str,
    rubric_report: dict[str, Any],
    *,
    db_path: str | Path,
) -> None:
    """Persist Proof Economy metrics to disposable telemetry.sqlite3."""
    p = Path(db_path)
    if os.path.lexists(str(p.parent)) and _is_reparse_or_link(str(p.parent)):
        raise PermissionError(f"Refusing symlinked sidecar dir: {p.parent}")
    init_telemetry_db(str(p))
    econ = rubric_report["proof_economy"]
    now_iso = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    with closing(sqlite3.connect(str(p), timeout=5.0)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS proof_economy (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                problem_id TEXT NOT NULL,
                weighted_total REAL NOT NULL,
                efficiency_ratio REAL NOT NULL,
                tool_calls INTEGER NOT NULL,
                wall_ms REAL NOT NULL
            )
            """
        )
        with conn:
            conn.execute(
                """
                INSERT INTO proof_economy(recorded_at, problem_id, weighted_total, efficiency_ratio, tool_calls, wall_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    now_iso,
                    str(problem_id)[:120],
                    float(rubric_report["weighted_total"]),
                    float(econ["efficiency_ratio"]),
                    int(econ["tool_calls"]),
                    float(econ["wall_ms"]),
                ),
            )


def run_solver_reviewer_refiner(
    problem: str,
    *,
    solver_fn: Callable[[str], dict[str, Any]],
    reviewer_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    refiner_fn: Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]],
    verify_answer_fn: Callable[[dict[str, Any]], bool] | None = None,
    max_rounds: int = 3,
    record_to_experiences: bool = False,
    brain_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run Solver -> Reviewer -> Refiner loop with hard max_rounds cap & anti-sycophancy guard."""
    t0 = time.perf_counter()
    rounds_cap = max(1, min(6, int(max_rounds)))

    current = dict(solver_fn(problem) or {})
    cur_answer = str(current.get("answer") or extract_final_answer(str(current.get("text") or ""))).strip()
    cur_steps = list(current.get("steps") or [str(current.get("text") or cur_answer)])
    cur_checks = list(current.get("sandbox_checks") or ())

    def _is_verified(cand: dict[str, Any]) -> bool:
        if callable(verify_answer_fn):
            return bool(verify_answer_fn(cand))
        checks = list(cand.get("sandbox_checks") or ())
        if checks and all(bool(c.get("ok")) for c in checks):
            return True
        code = str(cand.get("verification_code") or "").strip()
        if code:
            try:
                res = run_sandboxed_python(code)
            except (SandboxSecurityError, ValueError):
                return False
            return bool(res.get("ok"))
        return False

    cur_verified = _is_verified(current)
    cur_rubric = score_reasoning_trace(
        cur_steps,
        final_answer=cur_answer,
        sandbox_checks=cur_checks if cur_checks else ([{"ok": True}] if cur_verified else []),
    )

    history: list[dict[str, Any]] = []
    anti_sycophancy_blocked = False
    termination_reason = "converged"

    for rnd in range(1, rounds_cap + 1):
        critique = dict(reviewer_fn(problem, current) or {})
        approved = bool(critique.get("approved", False))
        counter_code = str(critique.get("counterexample_code") or "").strip()
        tool_backed_critique = False
        if counter_code:
            try:
                sb_crit = run_sandboxed_python(counter_code)
                tool_backed_critique = bool(sb_crit.get("ok") and sb_crit.get("result"))
            except (SandboxSecurityError, ValueError):
                tool_backed_critique = False
        elif bool(critique.get("tool_verified_counterexample", False)):
            tool_backed_critique = True

        if approved:
            history.append({"round": rnd, "status": "approved", "rubric": cur_rubric})
            termination_reason = "reviewer_approved"
            break

        # Anti-sycophancy guard: never let an unverified critique override a tool-verified candidate
        if cur_verified and not tool_backed_critique:
            anti_sycophancy_blocked = True
            history.append(
                {
                    "round": rnd,
                    "status": "critique_rejected_anti_sycophancy",
                    "reason": "Current candidate is tool-verified; critique lacked tool-backed counterexample.",
                }
            )
            termination_reason = "anti_sycophancy_preserved_verified"
            break

        refined = dict(refiner_fn(problem, current, critique) or {})
        ref_ans = str(refined.get("answer") or extract_final_answer(str(refined.get("text") or ""))).strip()
        ref_steps = list(refined.get("steps") or [str(refined.get("text") or ref_ans)])
        ref_verified = _is_verified(refined)
        ref_rubric = score_reasoning_trace(
            ref_steps,
            final_answer=ref_ans,
            sandbox_checks=list(refined.get("sandbox_checks") or ()) or ([{"ok": True}] if ref_verified else []),
        )

        # Anti-sycophancy: even after refinement, refuse flipping a verified answer to an unverified one
        if cur_verified and not ref_verified and not are_math_equivalent(cur_answer, ref_ans):
            anti_sycophancy_blocked = True
            history.append({"round": rnd, "status": "refinement_rejected_regression"})
            termination_reason = "anti_sycophancy_preserved_verified"
            break

        if ref_rubric["weighted_total"] >= cur_rubric["weighted_total"]:
            current = refined
            cur_answer = ref_ans
            cur_steps = ref_steps
            cur_verified = ref_verified
            cur_rubric = ref_rubric
            history.append({"round": rnd, "status": "refined", "rubric": ref_rubric})
        else:
            history.append({"round": rnd, "status": "kept_prior_monotonic_guard", "rubric": cur_rubric})
            termination_reason = "monotonic_guard"
            break

        if rnd == rounds_cap:
            termination_reason = "max_rounds_cap"

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    cur_rubric["proof_economy"]["wall_ms"] = elapsed_ms

    if record_to_experiences and brain_dir is not None:
        resolved_brain, _ = resolve_module_atoms_and_brain(globals(), brain_dir=str(brain_dir))
        save_experience_record(
            goal=f"Solve/Verify: {problem[:160]}",
            result_summary=f"Answer={cur_answer} score={cur_rubric['weighted_total']} term={termination_reason}",
            success=bool(cur_verified or cur_rubric["weighted_total"] >= 0.75),
            lessons=[f"4D score={cur_rubric['weighted_total']}, anti_sycophancy={anti_sycophancy_blocked}"],
            experiences_dir=str(Path(resolved_brain) / "4-experiences"),
            brain_dir=resolved_brain,
        )

    return {
        "final_candidate": current,
        "final_answer": cur_answer,
        "verified": cur_verified,
        "rubric": cur_rubric,
        "rounds_executed": len(history),
        "termination_reason": termination_reason,
        "anti_sycophancy_blocked": anti_sycophancy_blocked,
        "history": history,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Second Brain 4D Process Rubric Evaluator")
    parser.add_argument("--steps-json", required=True)
    parser.add_argument("--answer", default="")
    parser.add_argument("--expected", default=None)
    args = parser.parse_args(argv)
    steps = json.loads(args.steps_json)
    out = score_reasoning_trace(steps, final_answer=args.answer, expected_answer=args.expected)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
