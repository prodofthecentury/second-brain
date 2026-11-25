# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Comprehension-Generation-Verification (CGV) Quantitative Reasoner (Epic 2E.2).

Implements:
1. **DUP 3-Stage Comprehension:**
   - Stage 1: Target goal/variable & domain constraint extraction + visual OCR interleave check
   - Stage 2: Distractor sentence separation with citation byte anchors
   - Stage 3: Lightweight Unit & Quantity Dependency Graph (catching dimensional mismatches upfront)
2. **PAL / PoT / ToRA Generation-Verification Loop:**
   - Interleaves reasoning turns with `symbolic_sandbox.run_sandboxed_python`
   - Feeds exact numeric/symbolic results or tracebacks back into subsequent turns
   - Aggregates `majority@k` via `math_equivalence.aggregate_majority_at_k`.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import os
import re
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ in ("cgv_reasoner", "scripts.cgv_reasoner"):
    sys.modules.setdefault("cgv_reasoner", sys.modules[__name__])
    sys.modules.setdefault("scripts.cgv_reasoner", sys.modules[__name__])

from math_equivalence import (  # noqa: E402
    _UNIT_TABLE,
    aggregate_majority_at_k,
    are_math_equivalent,
    extract_final_answer,
)
from process_evaluator import score_reasoning_trace  # noqa: E402
from symbolic_sandbox import SandboxSecurityError, run_sandboxed_python  # noqa: E402

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_QUANTITY_RE = re.compile(
    r"(?:(?P<var>[a-zA-Z][a-zA-Z0-9_]*)\s*=\s*)?(?P<num>-?\d+(?:\.\d+)?(?:/\d+)?)\s*(?P<unit>[a-zA-Z]{1,8}|%)?\b"
)
_CODE_BLOCK_RE = re.compile(r"```python\s*\n([\s\S]*?)```", re.IGNORECASE)
_CONSTRAINT_RE = re.compile(
    r"\b(?:positive|negative|integer|integers|nonnegative|real|prime|greater than|less than|at least|at most|distinct|[<>]=?)\b",
    re.IGNORECASE,
)


def decompose_dup(
    problem_text: str,
    *,
    visual_ocr_text: str = "",
) -> dict[str, Any]:
    """Execute 3-Stage DUP Comprehension + Unit & Quantity Dependency Graph."""
    raw = str(problem_text or "").strip()
    if not raw:
        raise ValueError("Problem text is required for DUP decomposition.")

    # Stage 1: Core target question/variable & constraints + visual diagram interleaving
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(raw) if s.strip()]
    target_question = next((s for s in reversed(sentences) if "?" in s or re.search(r"\b(?:find|solve|compute|calculate|what\s+is|determine)\b", s, re.I)), sentences[-1])
    constraints = [m.group(0) for m in _CONSTRAINT_RE.finditer(raw)]

    ocr_quantities: list[dict[str, Any]] = []
    if visual_ocr_text.strip():
        for m in _QUANTITY_RE.finditer(visual_ocr_text):
            ocr_quantities.append(
                {
                    "var": m.group("var") or "",
                    "value": m.group("num"),
                    "unit": (m.group("unit") or "").lower(),
                    "source": "visual_ocr",
                }
            )

    # Stage 2: Load-bearing quantitative facts vs distractor prose with character offsets
    load_bearing: list[dict[str, Any]] = []
    distractors: list[dict[str, Any]] = []
    cursor = 0
    for idx, sent in enumerate(sentences, start=1):
        pos = raw.find(sent, cursor)
        if pos == -1:
            pos = cursor
        cursor = pos + len(sent)
        has_math = bool(
            re.search(r"\d", sent)
            or "$" in sent
            or "=" in sent
            or sent == target_question
            or _CONSTRAINT_RE.search(sent)
        )
        entry = {"sentence_id": f"S{idx}", "text": sent, "char_start": pos, "char_end": pos + len(sent)}
        if has_math:
            load_bearing.append(entry)
        else:
            distractors.append(entry)

    # Stage 3: Build Unit & Quantity Dependency Graph & check dimensional conflicts
    nodes: list[dict[str, Any]] = []
    dimensions_seen: dict[str, set[str]] = {}
    for lb in load_bearing:
        for m in _QUANTITY_RE.finditer(lb["text"]):
            unit_tok = (m.group("unit") or "").lower()
            dim = _UNIT_TABLE[unit_tok][0] if unit_tok in _UNIT_TABLE else ("percent" if unit_tok == "%" else "dimensionless")
            nodes.append(
                {
                    "var": m.group("var") or f"q_{len(nodes)+1}",
                    "value": m.group("num"),
                    "unit": unit_tok,
                    "dimension": dim,
                    "anchor": lb["sentence_id"],
                }
            )
            if dim != "dimensionless":
                dimensions_seen.setdefault(dim, set()).add(unit_tok)

    dimensional_warnings: list[str] = []
    for dim, units in dimensions_seen.items():
        if len(units) > 1:
            dimensional_warnings.append(
                f"Mixed units detected for dimension '{dim}' ({sorted(units)}); convert to common base units before combining."
            )
    if ("+" in raw or "-" in raw) and len([d for d in dimensions_seen if d not in {"percent"}]) > 1:
        # Check if an addition/subtraction directly bridges incompatible physical dimensions like m + s
        if re.search(r"\b\d+(?:\.\d+)?\s*(?:m|km|cm)\s*[\+\-]\s*\d+(?:\.\d+)?\s*(?:s|min|h|hr|kg|g)\b", raw, re.I):
            dimensional_warnings.append(
                "Dimensional mismatch: incompatible physical dimensions combined via addition/subtraction."
            )

    return {
        "stage1_target": {
            "question": target_question,
            "constraints": constraints,
            "visual_ocr_quantities": ocr_quantities,
        },
        "stage2_sentences": {
            "load_bearing": load_bearing,
            "distractors_filtered": distractors,
        },
        "stage3_quantity_graph": {
            "nodes": nodes,
            "dimensions": {k: sorted(v) for k, v in dimensions_seen.items()},
            "dimensional_warnings": dimensional_warnings,
        },
    }


def _default_tora_solver(
    problem_text: str,
    dup_info: dict[str, Any],
    turn: int,
    prior_feedback: str,
) -> str:
    """Deterministic fallback ToRA/PAL generator for pure arithmetic/algebra when no LLM callback is passed."""
    if turn > 1 and "SANDBOX_RESULT=" in prior_feedback:
        val = prior_feedback.split("SANDBOX_RESULT=", 1)[1].splitlines()[0].strip()
        return f"From the verified sandbox computation (`{val}`), `Final Answer: \\boxed{{{val}}}`."

    # Attempt clean arithmetic expression extraction for offline standalone test/CLI usage
    cleaned = re.sub(r"[^0-9a-zA-Z\+\-\*/\.\(\)\s=^]", " ", problem_text)
    arith_m = re.search(r"([\(\s]*[0-9][0-9\+\-\*/\.\(\)\s]*[\+\-\*/][0-9\+\-\*/\.\(\)\s]*[0-9\)])", cleaned)
    if arith_m:
        expr = arith_m.group(1)
        return (
            "Let us evaluate using exact rational arithmetic in the sandbox.\n"
            f"```python\nfrom fractions import Fraction\nresult = {expr}\nprint(result)\n```\n"
        )
    return "No exact closed-form tool call needed; `Final Answer: \\boxed{unknown}`."


def _loopback_ollama_chat_url() -> str | None:
    """Validate OLLAMA_HOST points strictly to local HTTP loopback and return /api/chat URL."""
    raw = (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").strip().rstrip("/")
    if not raw:
        raw = "http://127.0.0.1:11434"
    cand = raw if "://" in raw else f"http://{raw}"
    try:
        parsed = urllib.parse.urlparse(cand)
    except ValueError:
        return None
    if parsed.scheme != "http" or parsed.username or parsed.password:
        return None
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return None
    try:
        port = parsed.port or 11434
    except ValueError:
        return None
    if not (1 <= port <= 65535):
        return None
    if host == "localhost":
        host = "127.0.0.1"
    netloc_host = "[::1]" if host == "::1" else host
    return f"http://{netloc_host}:{port}/api/chat"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Fail closed on any HTTP redirect from the loopback Ollama endpoint."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            f"Redirects are disallowed for local Ollama requests (HTTP {code})",
            headers,
            fp,
        )

    def http_error_302(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            f"Redirects are disallowed for local Ollama requests (HTTP {code})",
            headers,
            fp,
        )

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _local_ollama_tora_solver(
    problem_text: str,
    dup_info: dict[str, Any],
    turn: int,
    prior_feedback: str,
    *,
    model: str | None = None,
    timeout_seconds: float = 10.0,
) -> str:
    """Attempt a bounded local loopback Ollama /api/chat ToRA/PAL draft with fail-closed fallback."""
    url = _loopback_ollama_chat_url()
    if not url:
        return _default_tora_solver(problem_text, dup_info, turn, prior_feedback)

    chosen_model = (model or os.environ.get("BRAIN_GEN_MODEL") or "llama3.2:3b").strip()
    warnings = list(dup_info.get("stage3_quantity_graph", {}).get("dimensional_warnings") or ())
    target_q = str(dup_info.get("stage1_target", {}).get("question") or problem_text)

    user_parts = [f"Problem:\n{problem_text.strip()}", f"Target Question: {target_q}"]
    if warnings:
        user_parts.append("Unit Warnings:\n" + "\n".join(f"- {w}" for w in warnings))
    if prior_feedback:
        user_parts.append(f"Turn {turn - 1} Sandbox Feedback:\n{prior_feedback}")

    payload = {
        "model": chosen_model,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a quantitative ToRA/PAL reasoner. Solve step by step. "
                    "When exact calculation helps, emit a single ```python ... ``` block assigning `result`. "
                    "When finished, end with `Final Answer: \\boxed{...}`."
                ),
            },
            {"role": "user", "content": "\n\n".join(user_parts)},
        ],
        "options": {"temperature": 0.2, "num_predict": 512},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())
    eff_timeout = max(0.5, min(20.0, float(timeout_seconds)))
    try:
        with opener.open(req, timeout=eff_timeout) as resp:
            raw_bytes = resp.read(65_537)
        if not raw_bytes or len(raw_bytes) > 65_536:
            return _default_tora_solver(problem_text, dup_info, turn, prior_feedback)
        parsed = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        msg = parsed.get("message") if isinstance(parsed, dict) else None
        content = str(msg.get("content") or "") if isinstance(msg, dict) else ""
        content = re.sub(r"<think>[\s\S]*?</think>", "", content, flags=re.IGNORECASE).strip()
        if content:
            return content
    except Exception:
        pass
    return _default_tora_solver(problem_text, dup_info, turn, prior_feedback)


def solve_quantitative_problem(
    problem_text: str,
    *,
    visual_ocr_text: str = "",
    solver_fn: Callable[[str, dict[str, Any], int, str], str] | None = None,
    max_tool_turns: int = 4,
    k_samples: int = 1,
    timeout_seconds: float = 5.0,
    use_local_llm: bool = False,
    llm_model: str | None = None,
    llm_timeout_seconds: float = 10.0,
    include_rubric: bool = True,
    expected_answer: str | None = None,
) -> dict[str, Any]:
    """Run CGV + DUP + iterative ToRA/PAL sandboxed solver across k_samples and majority@k."""
    t0 = time.perf_counter()
    dup_report = decompose_dup(problem_text, visual_ocr_text=visual_ocr_text)
    if solver_fn is not None:
        runner = solver_fn
    elif use_local_llm:
        runner = lambda p, d, t, fb: _local_ollama_tora_solver(  # noqa: E731
            p, d, t, fb, model=llm_model, timeout_seconds=llm_timeout_seconds
        )
    else:
        runner = _default_tora_solver

    eff_turns = max(1, min(8, int(max_tool_turns)))
    eff_k = max(1, min(16, int(k_samples)))

    sample_outputs: list[str] = []
    traces: list[dict[str, Any]] = []

    for s_idx in range(eff_k):
        feedback = ""
        turn_logs: list[dict[str, Any]] = []
        final_text = ""
        for turn in range(1, eff_turns + 1):
            draft = str(runner(problem_text, dup_report, turn, feedback) or "")
            code_blocks = _CODE_BLOCK_RE.findall(draft)
            sandbox_runs = []
            if code_blocks:
                for blk in code_blocks[:2]:
                    try:
                        sb = run_sandboxed_python(blk, timeout_seconds=timeout_seconds)
                    except (SandboxSecurityError, ValueError) as exc:
                        sb = {"ok": False, "stdout": "", "result": None, "error": str(exc), "elapsed_ms": 0.0}
                    sandbox_runs.append(sb)
                last_sb = sandbox_runs[-1]
                if last_sb["ok"]:
                    res_val = last_sb["result"] if last_sb["result"] is not None else last_sb["stdout"].strip()
                    feedback = f"SANDBOX_RESULT={res_val}\nSTDOUT={last_sb['stdout'].strip()}"
                else:
                    feedback = f"SANDBOX_ERROR={last_sb['error']}"
            turn_logs.append({"turn": turn, "draft": draft, "sandbox_runs": sandbox_runs})
            final_text = draft
            ans_probe = extract_final_answer(draft)
            if ans_probe and (not code_blocks or (turn == eff_turns) or "\\boxed" in draft or "Final Answer:" in draft):
                break
        sample_outputs.append(final_text)
        traces.append({"sample_index": s_idx, "turns": turn_logs, "extracted_answer": extract_final_answer(final_text)})

    majority = aggregate_majority_at_k(sample_outputs)
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    rubric_score: dict[str, Any] | None = None
    if include_rubric:
        best_trace = traces[0] if traces else {"turns": []}
        maj_ans = str(majority.get("majority_answer") or "")
        if maj_ans:
            for tr in traces:
                tr_ans = str(tr.get("extracted_answer") or "")
                if tr_ans and (tr_ans == maj_ans or are_math_equivalent(tr_ans, maj_ans)):
                    best_trace = tr
                    break
        trace_steps: list[str] = []
        trace_checks: list[dict[str, Any]] = []
        for tlog in best_trace.get("turns") or ():
            draft_txt = str(tlog.get("draft") or "").strip()
            if draft_txt:
                trace_steps.append(draft_txt)
            trace_checks.extend(list(tlog.get("sandbox_runs") or ()))
        rubric_score = score_reasoning_trace(
            trace_steps,
            final_answer=maj_ans,
            expected_answer=expected_answer,
            unit_warnings=dup_report["stage3_quantity_graph"]["dimensional_warnings"],
            sandbox_checks=trace_checks,
            wall_ms=elapsed_ms,
        )

    return {
        "status": "solved" if majority["majority_answer"] else "unresolved",
        "dup": dup_report,
        "majority_answer": majority["majority_answer"],
        "confidence": majority["confidence"],
        "majority_summary": majority,
        "rubric_score": rubric_score,
        "traces": traces,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Second Brain CGV Quantitative Solver (DUP + ToRA Sandbox)")
    parser.add_argument("problem")
    parser.add_argument("--k", type=int, default=1)
    parser.add_argument("--use-local-llm", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    out = solve_quantitative_problem(args.problem, k_samples=args.k, use_local_llm=args.use_local_llm)
    print(json.dumps(out, indent=2) if args.json else f"answer={out['majority_answer']} warnings={len(out['dup']['stage3_quantity_graph']['dimensional_warnings'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
