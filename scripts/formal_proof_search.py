# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""AlphaProof Nexus Discovery Engine: SafeVerify, Ralph Loop & Agent D (Epic 2E.4 & 2E.5).

Synthesizes the AlphaProof Nexus discovery protocol (Science 394, 234 (2026);
arXiv:2605.22763v2 + SM) into Second Brain's local-first Three-Store architecture:
- Explicit EVOLVE-BLOCK / EVOLVE-VALUE mutation markers (Python/Z3 and Lean 4)
- Immutable specification diff guard (`SafeVerify`) rejecting edits outside evolve
  markers, signature tampering, `sorry`/`sorryAx`/`admit`, or sandbox escapes
- Small-n concrete Test-Lemma misformalization pre-checks
- Mode A (Default Local): Ralph Episode Loop with APOLLO-style subgoal isolation
- Mode B (Opt-In Evolutionary): Agent D with P=7 Batch Rater (Good vs Miracle Gaps),
  pure-Python Plackett-Luce Gamma-Gamma Gibbs sampler (I=1000, B=200),
  Thompson sampling matchmaking, and P-UCB (c=0.2, top-64 frontier)
- Content-addressed Global Subgoal Cache (`brain/.sidecar/subgoal_cache.sqlite3`)
- Clean degrade (`lean_toolchain_unavailable`) when external `lean` CLI is absent.
"""



from __future__ import annotations

if __name__ == "__main__" and not __package__:
    import runpy as _sb_runpy
    import sys as _sb_sys
    from pathlib import Path as _sb_Path

    _sb_repo = _sb_Path(__file__).resolve().parents[1]
    _sb_here = _sb_Path(__file__).resolve().parent
    if _sb_Path(_sb_sys.path[0]).resolve() == _sb_here:
        _sb_sys.path[0] = str(_sb_repo)
    elif str(_sb_repo) not in _sb_sys.path:
        _sb_sys.path.insert(0, str(_sb_repo))
    _sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__")
    raise SystemExit(0)


import argparse
from collections.abc import Callable
from contextlib import closing
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any

if __name__ in ("formal_proof_search", "scripts.formal_proof_search"):
    sys.modules.setdefault("formal_proof_search", sys.modules[__name__])
    sys.modules.setdefault("scripts.formal_proof_search", sys.modules[__name__])

from scripts.atom_mutator import atomic_write_bytes  # noqa: E402
from scripts.cognitive_core import save_experience_record  # noqa: E402
from scripts.config import _is_reparse_or_link, resolve_module_atoms_and_brain  # noqa: E402
from scripts.symbolic_sandbox import SandboxSecurityError, run_sandboxed_python, validate_sandbox_ast  # noqa: E402

_PY_EVOLVE_RE = re.compile(
    r"(?P<start>^[ \t]*#[ \t]*EVOLVE-(?:BLOCK|VALUE)-START[^\n]*\n)(?P<body>[\s\S]*?)(?P<end>^[ \t]*#[ \t]*EVOLVE-(?:BLOCK|VALUE)-END[^\n]*$)",
    re.MULTILINE,
)
_LEAN_EVOLVE_RE = re.compile(
    r"(?P<start>^[ \t]*--[ \t]*EVOLVE-(?:BLOCK|VALUE)-START[^\n]*\n)(?P<body>[\s\S]*?)(?P<end>^[ \t]*--[ \t]*EVOLVE-(?:BLOCK|VALUE)-END[^\n]*$)",
    re.MULTILINE,
)
_FORBIDDEN_LEAN_TOKENS = (
    re.compile(r"\bsorry\b"),
    re.compile(r"\bsorryAx\b"),
    re.compile(r"\badmit\b"),
    re.compile(r"\baxiom\b"),
    re.compile(r"\bunsafe\b"),
    re.compile(r"#eval\b"),
    re.compile(r"#check_failure\b"),
    re.compile(r"\bIO\."),
    re.compile(r"\bSystem\."),
    re.compile(r"@\[\s*extern\b"),
    re.compile(r"@\[\s*implemented_by\b"),
    re.compile(r"\bimplemented_by\b"),
    re.compile(r"\bnative_decide\b"),
    re.compile(r"\bopaque\b"),
    re.compile(r"\bpartial\b"),
    re.compile(r"\belab(?:_rules)?\b"),
    re.compile(r"\bmacro(?:_rules)?\b"),
    re.compile(r"\bsyntax\b"),
    re.compile(r"\bimport\s+Lean\b"),
    re.compile(r"\bimport\s+Init\.System\b"),
    re.compile(r"\bset_option\b"),
)


class SafeVerifyError(ValueError):
    """Raised when a mutated candidate violates SafeVerify spec-integrity or anti-cheat locks."""


def _assert_safe_lean_source(source: str, *, context: str = "Lean source") -> None:
    """Reject forbidden Lean cheat, IO, FFI, and metaprogramming constructs before running `lean`."""
    text = str(source or "")
    for rx in _FORBIDDEN_LEAN_TOKENS:
        if rx.search(text):
            raise SafeVerifyError(
                f"SafeVerify rejected {context} containing forbidden construct: {rx.pattern}"
            )


def _build_clean_lean_env(tmp_dir: str) -> dict[str, str]:
    """Construct a secret-free subprocess environment for invoking `lean` (no API keys or BRAIN_* vars)."""
    env: dict[str, str] = {
        "TEMP": tmp_dir,
        "TMP": tmp_dir,
        "TMPDIR": tmp_dir,
    }
    for key in ("PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "ELAN_HOME", "LEAN_PATH"):
        val = os.environ.get(key)
        if val:
            env[key] = val
    return env


def _detect_language(source: str, language: str = "auto") -> str:
    clean = (language or "auto").strip().lower()
    if clean in {"python", "lean"}:
        return clean
    if "-- EVOLVE-" in source:
        return "lean"
    if "# EVOLVE-" in source:
        return "python"
    if re.search(r"^\s*(?:theorem|lemma|import\s+Mathlib)\b", source, re.MULTILINE):
        return "lean"
    return "python"


def _extract_skeleton_and_blocks(source: str, language: str) -> tuple[str, list[str]]:
    pattern = _LEAN_EVOLVE_RE if language == "lean" else _PY_EVOLVE_RE
    blocks: list[str] = []
    parts: list[str] = []
    last = 0
    for idx, m in enumerate(pattern.finditer(source)):
        parts.append(source[last : m.end("start")])
        blocks.append(m.group("body"))
        parts.append(f"__EVOLVE_SLOT_{idx}__\n")
        parts.append(m.group("end"))
        last = m.end()
    parts.append(source[last:])
    return "".join(parts).replace("\r\n", "\n"), blocks


def replace_evolve_blocks(template_source: str, replacements: list[str], *, language: str = "auto") -> str:
    """Replace EVOLVE-BLOCK/EVOLVE-VALUE interiors deterministically inside a verified template."""
    lang = _detect_language(template_source, language)
    pattern = _LEAN_EVOLVE_RE if lang == "lean" else _PY_EVOLVE_RE
    matches = list(pattern.finditer(template_source))
    if len(matches) != len(replacements):
        raise SafeVerifyError(
            f"Expected {len(matches)} evolve replacement(s), got {len(replacements)}."
        )
    parts: list[str] = []
    last = 0
    for m, repl in zip(matches, replacements):
        parts.append(template_source[last : m.end("start")])
        body_clean = str(repl).replace("\r\n", "\n").rstrip("\n") + "\n"
        parts.append(body_clean)
        parts.append(m.group("end"))
        last = m.end()
    parts.append(template_source[last:])
    return "".join(parts)


def verify_spec_integrity(
    baseline_source: str,
    candidate_source: str,
    *,
    language: str = "auto",
) -> dict[str, Any]:
    """SafeVerify audit: reject edits outside evolve blocks, cheat tokens, or AST escapes."""
    lang = _detect_language(baseline_source, language)
    base_norm = baseline_source.replace("\r\n", "\n")
    cand_norm = candidate_source.replace("\r\n", "\n")

    base_skel, base_blocks = _extract_skeleton_and_blocks(base_norm, lang)
    cand_skel, cand_blocks = _extract_skeleton_and_blocks(cand_norm, lang)

    if not base_blocks:
        raise SafeVerifyError("Baseline specification has no EVOLVE-BLOCK or EVOLVE-VALUE markers.")
    if len(base_blocks) != len(cand_blocks):
        raise SafeVerifyError(
            f"Candidate altered evolve marker count (expected {len(base_blocks)}, got {len(cand_blocks)})."
        )
    if base_skel != cand_skel:
        raise SafeVerifyError(
            "SafeVerify rejected candidate: immutable code/signature outside EVOLVE markers was modified."
        )

    if lang == "lean":
        _assert_safe_lean_source(cand_norm, context="Lean candidate")

    if lang == "python":
        try:
            validate_sandbox_ast(cand_norm)
        except (SandboxSecurityError, ValueError) as exc:
            raise SafeVerifyError(f"SafeVerify AST security check failed: {exc}") from exc

    return {
        "valid": True,
        "language": lang,
        "evolve_block_count": len(base_blocks),
        "spec_sha256": hashlib.sha256(base_skel.encode("utf-8")).hexdigest(),
        "candidate_sha256": hashlib.sha256(cand_norm.encode("utf-8")).hexdigest(),
    }


def verify_lean_binary_available() -> str | None:
    """Return path to `lean` binary if available on PATH, else None."""
    return shutil.which("lean")


def verify_lean_proof(
    candidate_source: str,
    *,
    timeout_seconds: float = 5.0,
    spec_sha256: str = "",
) -> dict[str, Any]:
    """Verify a Lean 4 proof candidate with forbidden-construct checks, scrubbed env, and timeout handling."""
    _assert_safe_lean_source(candidate_source, context="Lean candidate")
    timeout = max(0.5, min(60.0, float(timeout_seconds)))
    lean_bin = verify_lean_binary_available()
    if not lean_bin:
        return {
            "verified": False,
            "status": "lean_toolchain_unavailable",
            "error": "External 'lean' toolchain binary not found on PATH.",
            "spec_sha256": spec_sha256,
        }
    with tempfile.TemporaryDirectory(prefix="sb_lean_") as td:
        f = Path(td) / "Proof.lean"
        f.write_text(candidate_source, encoding="utf-8")
        try:
            proc = subprocess.run(
                [lean_bin, str(f)],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=_build_clean_lean_env(td),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "verified": False,
                "status": "lean_timeout",
                "error": f"Lean verification timed out after {timeout:.1f}s.",
                "spec_sha256": spec_sha256,
            }
        ok = proc.returncode == 0
        return {
            "verified": ok,
            "status": "verified" if ok else "lean_check_failed",
            "error": "" if ok else (proc.stderr or proc.stdout).strip()[:1500],
            "spec_sha256": spec_sha256,
        }


def run_test_lemmas(
    candidate_source: str,
    test_lemmas: list[str] | tuple[str, ...],
    *,
    language: str = "auto",
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Run small-n boundary sanity checks ('Test Lemmas') to fail fast on misformalized specs."""
    lang = _detect_language(candidate_source, language)
    lemmas = [str(t or "").strip() for t in (test_lemmas or ()) if str(t or "").strip()]

    if lang == "lean":
        _assert_safe_lean_source(candidate_source, context="Lean candidate")
        for idx, lemma in enumerate(lemmas, start=1):
            _assert_safe_lean_source(lemma, context=f"Lean test_lemma #{idx}")

    if not lemmas:
        return {"passed": True, "checked": 0, "failures": []}

    if lang == "lean":
        timeout = max(0.5, min(60.0, float(timeout_seconds)))
        lean_bin = verify_lean_binary_available()
        if not lean_bin:
            return {
                "passed": False,
                "status": "lean_toolchain_unavailable",
                "checked": len(lemmas),
                "failures": ["External 'lean' toolchain binary not installed; cannot execute Lean test lemmas."],
            }
        # Evaluate concatenated Lean spec + test lemmas inside temp file with scrubbed env
        probe_src = candidate_source.rstrip() + "\n\n" + "\n".join(lemmas) + "\n"
        with tempfile.TemporaryDirectory(prefix="sb_lean_test_") as td:
            f = Path(td) / "TestLemma.lean"
            f.write_text(probe_src, encoding="utf-8")
            try:
                proc = subprocess.run(
                    [lean_bin, str(f)],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=_build_clean_lean_env(td),
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return {
                    "passed": False,
                    "status": "lean_timeout",
                    "checked": len(lemmas),
                    "failures": [f"Lean test lemmas timed out after {timeout:.1f}s."],
                }
            if proc.returncode != 0:
                return {
                    "passed": False,
                    "status": "misformalized_spec",
                    "checked": len(lemmas),
                    "failures": [(proc.stderr or proc.stdout).strip()[:1500]],
                }
        return {"passed": True, "status": "ok", "checked": len(lemmas), "failures": []}

    failures: list[str] = []
    for idx, assertion_code in enumerate(lemmas, start=1):
        combined = candidate_source.rstrip() + "\n" + assertion_code + "\n"
        res = run_sandboxed_python(combined, timeout_seconds=timeout_seconds)
        if not res["ok"]:
            failures.append(f"TestLemma #{idx}: {res['error']}")
    return {
        "passed": len(failures) == 0,
        "status": "ok" if not failures else "misformalized_spec",
        "checked": len(lemmas),
        "failures": failures,
    }


def _subgoal_cache_path(brain_dir: str | Path | None = None) -> Path:
    resolved_brain, _ = resolve_module_atoms_and_brain(globals(), brain_dir=str(brain_dir) if brain_dir is not None else None)
    return Path(resolved_brain) / ".sidecar" / "subgoal_cache.sqlite3"


def _connect_subgoal_cache(dbp: Path) -> sqlite3.Connection:
    if os.path.lexists(str(dbp.parent)) and _is_reparse_or_link(str(dbp.parent)):
        raise PermissionError(f"Refusing symlinked sidecar dir: {dbp.parent}")
    dbp.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(dbp))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subgoal_cache (
            spec_sha256 TEXT NOT NULL,
            subgoal_sha256 TEXT NOT NULL,
            proof_fragment TEXT NOT NULL,
            verified_at TEXT NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (spec_sha256, subgoal_sha256)
        )
        """
    )
    return conn


def lookup_subgoal_cache(spec_sha256: str, subgoal_text: str, *, brain_dir: str | Path | None = None) -> str | None:
    norm_sg = re.sub(r"\s+", " ", (subgoal_text or "").strip())
    sg_hash = hashlib.sha256(norm_sg.encode("utf-8")).hexdigest()
    dbp = _subgoal_cache_path(brain_dir)
    with closing(_connect_subgoal_cache(dbp)) as conn:
        row = conn.execute(
            "SELECT proof_fragment FROM subgoal_cache WHERE spec_sha256 = ? AND subgoal_sha256 = ?",
            (spec_sha256, sg_hash),
        ).fetchone()
        if row is not None:
            with conn:
                conn.execute(
                    "UPDATE subgoal_cache SET hits = hits + 1 WHERE spec_sha256 = ? AND subgoal_sha256 = ?",
                    (spec_sha256, sg_hash),
                )
            return str(row["proof_fragment"])
    return None


def store_subgoal_cache(spec_sha256: str, subgoal_text: str, proof_fragment: str, *, brain_dir: str | Path | None = None) -> str:
    norm_sg = re.sub(r"\s+", " ", (subgoal_text or "").strip())
    sg_hash = hashlib.sha256(norm_sg.encode("utf-8")).hexdigest()
    now_iso = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    dbp = _subgoal_cache_path(brain_dir)
    with closing(_connect_subgoal_cache(dbp)) as conn, conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO subgoal_cache(spec_sha256, subgoal_sha256, proof_fragment, verified_at, hits)
            VALUES (?, ?, ?, ?, COALESCE((SELECT hits FROM subgoal_cache WHERE spec_sha256=? AND subgoal_sha256=?), 0))
            """,
            (spec_sha256, sg_hash, proof_fragment, now_iso, spec_sha256, sg_hash),
        )
    return sg_hash


def _verify_candidate(
    baseline_source: str,
    candidate_source: str,
    verification_tests: list[str] | tuple[str, ...],
    *,
    language: str = "auto",
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    lang = _detect_language(baseline_source, language)
    safe_meta = verify_spec_integrity(baseline_source, candidate_source, language=lang)
    if lang == "lean":
        return verify_lean_proof(
            candidate_source,
            timeout_seconds=timeout_seconds,
            spec_sha256=safe_meta["spec_sha256"],
        )

    full_prog = candidate_source.rstrip() + "\n" + "\n".join(verification_tests) + "\n"
    sb = run_sandboxed_python(full_prog, timeout_seconds=timeout_seconds)
    return {
        "verified": bool(sb["ok"]),
        "status": "verified" if sb["ok"] else "execution_failed",
        "error": sb.get("error") or "",
        "stdout": sb.get("stdout") or "",
        "result": sb.get("result"),
        "spec_sha256": safe_meta["spec_sha256"],
    }


def isolate_and_retry_subgoals(
    subgoals: list[dict[str, str]],
    *,
    spec_sha256: str,
    repair_subgoal_fn: Callable[[str, str, str], str],
    verify_subgoal_fn: Callable[[str, str], tuple[bool, str]],
    max_retries_per_subgoal: int = 3,
    brain_dir: str | Path | None = None,
) -> dict[str, Any]:
    """APOLLO-style independent subgoal isolation, cache reuse, and recursive repair."""
    resolved: list[dict[str, Any]] = []
    cache_hits = 0
    for sg in subgoals:
        sg_id = str(sg.get("id") or "subgoal")
        sg_stmt = str(sg.get("statement") or "")
        cached = lookup_subgoal_cache(spec_sha256, sg_stmt, brain_dir=brain_dir)
        if cached is not None:
            ok_c, err_c = verify_subgoal_fn(sg_stmt, cached)
            if ok_c:
                cache_hits += 1
                resolved.append({"id": sg_id, "verified": True, "source": "subgoal_cache", "fragment": cached})
                continue

        cur_frag = str(sg.get("initial_fragment") or "")
        feedback = ""
        passed = False
        for attempt in range(1, max(1, int(max_retries_per_subgoal)) + 1):
            cur_frag = str(repair_subgoal_fn(sg_id, sg_stmt, feedback) or cur_frag)
            ok, feedback = verify_subgoal_fn(sg_stmt, cur_frag)
            if ok:
                passed = True
                store_subgoal_cache(spec_sha256, sg_stmt, cur_frag, brain_dir=brain_dir)
                resolved.append({"id": sg_id, "verified": True, "source": f"repaired_attempt_{attempt}", "fragment": cur_frag})
                break
        if not passed:
            resolved.append({"id": sg_id, "verified": False, "source": "failed", "fragment": cur_frag, "error": feedback})

    all_ok = all(r["verified"] for r in resolved)
    return {"all_verified": all_ok, "cache_hits": cache_hits, "subgoals": resolved}


def run_ralph_proof_loop(
    baseline_source: str,
    *,
    mutate_fn: Callable[[str, int, str], list[str]],
    test_lemmas: list[str] | tuple[str, ...] = (),
    verification_tests: list[str] | tuple[str, ...] = (),
    language: str = "auto",
    max_iterations: int = 6,
    brain_dir: str | Path | None = None,
    record_candidate: bool = True,
) -> dict[str, Any]:
    """Mode A (Default Local): Single-lineage Ralph Episode Loop with SafeVerify + Test-Lemma precheck."""
    lang = _detect_language(baseline_source, language)
    skel, base_blocks = _extract_skeleton_and_blocks(baseline_source.replace("\r\n", "\n"), lang)
    if not base_blocks:
        raise SafeVerifyError("Baseline spec lacks EVOLVE-BLOCK/EVOLVE-VALUE markers.")
    spec_sha = hashlib.sha256(skel.encode("utf-8")).hexdigest()

    if test_lemmas:
        tl = run_test_lemmas(baseline_source, test_lemmas, language=lang)
        if tl.get("status") == "lean_toolchain_unavailable":
            return {"status": "lean_toolchain_unavailable", "verified": False, "spec_sha256": spec_sha, "details": tl}

    feedback = ""
    attempts: list[dict[str, Any]] = []
    verified_src = None

    for it in range(1, max(1, int(max_iterations)) + 1):
        repls = list(mutate_fn(baseline_source, it, feedback))
        cand_src = replace_evolve_blocks(baseline_source, repls, language=lang)
        # Validate SafeVerify + small-n Test Lemmas before expensive full verification
        safe_meta = verify_spec_integrity(baseline_source, cand_src, language=lang)
        if test_lemmas:
            tl = run_test_lemmas(cand_src, test_lemmas, language=lang)
            if not tl["passed"]:
                feedback = "; ".join(tl.get("failures") or [])
                attempts.append({"iteration": it, "verified": False, "stage": "test_lemma", "error": feedback})
                continue
        ver = _verify_candidate(baseline_source, cand_src, verification_tests, language=lang)
        attempts.append({"iteration": it, **{k: v for k, v in ver.items() if k != "stdout"}})
        if ver.get("status") == "lean_toolchain_unavailable":
            return {"status": "lean_toolchain_unavailable", "verified": False, "spec_sha256": spec_sha, "attempts": attempts}
        if ver["verified"]:
            verified_src = cand_src
            break
        feedback = str(ver.get("error") or "")

    staged_path = None
    if verified_src is not None and record_candidate and brain_dir is not None:
        resolved_brain, _ = resolve_module_atoms_and_brain(globals(), brain_dir=str(brain_dir))
        exp_res = save_experience_record(
            goal=f"Verified formal/algorithmic discovery ({lang}, spec={spec_sha[:12]})",
            result_summary=f"Ralph loop converged in {len(attempts)} iteration(s) passing SafeVerify + Test Lemmas.",
            success=True,
            lessons=[f"Verified discovery inside spec {spec_sha[:12]} stored as candidate for owner review."],
            experiences_dir=str(Path(resolved_brain) / "4-experiences"),
            brain_dir=resolved_brain,
        )
        raw_out = Path(resolved_brain) / "0-raw" / f"verified-discovery-{spec_sha[:12]}.{ 'lean' if lang == 'lean' else 'py' }"
        atomic_write_bytes(str(raw_out), verified_src.encode("utf-8"))
        staged_path = str(raw_out)

    return {
        "status": "verified" if verified_src is not None else "exhausted",
        "verified": verified_src is not None,
        "spec_sha256": spec_sha,
        "language": lang,
        "iterations": len(attempts),
        "attempts": attempts,
        "verified_source": verified_src,
        "staged_artifact_path": staged_path,
    }


def score_batch_rater_decomposition(sketch: str) -> dict[str, Any]:
    """Batch Rater evaluator scoring Generalizability & Good Gaps vs Miracle Gaps."""
    txt = str(sketch or "")
    good_hits = len(re.findall(r"\b(?:lemma|have|def |assert |invariant|base_case|helper)\b", txt))
    miracle_hits = len(re.findall(r"\b(?:sorry|admit|magic|skip|TODO|assume_false|hack)\b", txt, re.I))
    generalizability = min(1.0, round(0.4 + 0.15 * len(re.findall(r"\b(?:for |while |forall|∀|def )\b", txt)), 4))
    decomp_score = max(0.0, min(1.0, round(0.5 + 0.15 * good_hits - 0.35 * miracle_hits, 4)))
    overall = round(0.55 * decomp_score + 0.45 * generalizability, 4)
    return {
        "good_gaps": good_hits,
        "miracle_gaps": miracle_hits,
        "generalizability": generalizability,
        "decomposition_quality": decomp_score,
        "sketch_score": overall,
    }


def fit_plackett_luce_gibbs_elo(
    rankings: list[list[str]],
    candidate_ids: list[str],
    *,
    iterations: int = 1000,
    burn_in: int = 200,
    a0: float = 1.0,
    b0: float = 1.0,
    seed: int = 42,
) -> dict[str, dict[str, float]]:
    """Pure-Python Plackett-Luce Gamma-Gamma Gibbs sampler (Caron & Doucet; I=1000, B=200).

    Each ranking in ``rankings`` orders candidates best-to-worst (``m <= P=7`` items).
    Returns posterior mean ``lambda_mean``, ``lambda_var`` (for Thompson sampling), and ``elo``.
    """
    ids = list(dict.fromkeys(candidate_ids))
    if not ids:
        return {}
    rng = random.Random(seed)
    lambdas: dict[str, float] = {cid: 1.0 for cid in ids}
    wins: dict[str, int] = {cid: 0 for cid in ids}
    for r in rankings:
        for cid in r[:-1]:
            if cid in wins:
                wins[cid] += 1

    eff_iters = max(20, int(iterations))
    eff_burn = max(0, min(eff_iters - 1, int(burn_in)))
    keep_count = eff_iters - eff_burn

    sum_lam: dict[str, float] = {cid: 0.0 for cid in ids}
    sum_lam_sq: dict[str, float] = {cid: 0.0 for cid in ids}

    for step in range(eff_iters):
        denom_contrib: dict[str, float] = {cid: 0.0 for cid in ids}
        for r in rankings:
            valid = [c for c in r if c in lambdas]
            if len(valid) < 2:
                continue
            running_sum = sum(lambdas[c] for c in valid)
            for idx in range(len(valid) - 1):
                rate = max(1e-9, running_sum)
                # Exponential(rate) auxiliary variable
                z = -math.log(max(1e-12, 1.0 - rng.random())) / rate
                for rem in valid[idx:]:
                    denom_contrib[rem] += z
                running_sum -= lambdas[valid[idx]]

        for cid in ids:
            shape = a0 + wins[cid]
            rate_param = b0 + denom_contrib[cid]
            lam = max(1e-6, rng.gammavariate(shape, 1.0 / max(1e-9, rate_param)))
            lambdas[cid] = lam
            if step >= eff_burn:
                sum_lam[cid] += lam
                sum_lam_sq[cid] += lam * lam

    out: dict[str, dict[str, float]] = {}
    for cid in ids:
        mean_l = sum_lam[cid] / keep_count
        var_l = max(1e-6, (sum_lam_sq[cid] / keep_count) - (mean_l * mean_l))
        elo = 1200.0 + 400.0 * math.log10(max(1e-9, mean_l))
        out[cid] = {
            "lambda_mean": round(mean_l, 6),
            "lambda_std": round(math.sqrt(var_l), 6),
            "elo": round(elo, 2),
        }
    return out


def select_thompson_batch(
    elo_posterior: dict[str, dict[str, float]],
    *,
    batch_size: int = 7,
    seed: int = 7,
) -> list[str]:
    """Thompson-sampling batch matchmaking pairing informative P=7 candidates."""
    rng = random.Random(seed)
    draws = []
    for cid, stats in elo_posterior.items():
        mu = float(stats.get("lambda_mean", 1.0))
        sd = max(0.05, float(stats.get("lambda_std", 0.25)))
        sample = rng.gauss(mu, sd)
        draws.append((sample, cid))
    draws.sort(key=lambda pair: (-pair[0], pair[1]))
    return [cid for _, cid in draws[: max(2, min(16, int(batch_size)))]]


def select_p_ucb_parent(
    candidates: list[dict[str, Any]],
    *,
    c: float = 0.2,
    frontier_k: int = 64,
) -> dict[str, Any]:
    """P-UCB selection (c=0.2, top-64 frontier) picking next evolutionary parent."""
    if not candidates:
        raise ValueError("Candidate list is empty.")
    frontier = sorted(
        candidates,
        key=lambda x: (-float(x.get("elo", 1200.0)), str(x.get("id", ""))),
    )[: max(1, int(frontier_k))]
    total_visits = sum(int(x.get("visits", 0)) for x in frontier) + 1
    sqrt_n = math.sqrt(total_visits)
    best_item = frontier[0]
    best_ucb = -1e18
    for item in frontier:
        q_val = (float(item.get("elo", 1200.0)) - 1200.0) / 400.0 + float(item.get("value_score", 0.5))
        prior = max(0.01, float(item.get("prior", 1.0 / len(frontier))))
        visits = int(item.get("visits", 0))
        ucb = q_val + float(c) * prior * (sqrt_n / (1.0 + visits))
        if ucb > best_ucb:
            best_ucb = ucb
            best_item = item
    return best_item


def run_agent_d_search(
    baseline_source: str,
    candidate_pool: list[dict[str, Any]],
    *,
    verification_tests: list[str] | tuple[str, ...] = (),
    language: str = "auto",
    batch_size: int = 7,
    gibbs_iterations: int = 1000,
    gibbs_burn_in: int = 200,
    seed: int = 42,
) -> dict[str, Any]:
    """Mode B (Opt-In Evolutionary): Batch Rater (P=7) + Gibbs Elo + Thompson + P-UCB."""
    scored: list[dict[str, Any]] = []
    for idx, cand in enumerate(candidate_pool):
        cid = str(cand.get("id") or f"cand_{idx+1}")
        repls = list(cand.get("replacements") or ())
        src = replace_evolve_blocks(baseline_source, repls, language=language) if repls else str(cand.get("source") or "")
        safe_ok = True
        safe_err = ""
        try:
            verify_spec_integrity(baseline_source, src, language=language)
        except SafeVerifyError as exc:
            safe_ok = False
            safe_err = str(exc)
        ver = _verify_candidate(baseline_source, src, verification_tests, language=language) if safe_ok else {"verified": False, "error": safe_err}
        rater = score_batch_rater_decomposition("\n".join(repls) or src)
        scored.append(
            {
                "id": cid,
                "safe_ok": safe_ok,
                "verified": bool(ver.get("verified")),
                "error": ver.get("error") or safe_err,
                "rater": rater,
                "value_score": (1.0 if ver.get("verified") else rater["sketch_score"] * 0.5),
                "visits": int(cand.get("visits", 0)),
                "prior": float(cand.get("prior", 1.0 / max(1, len(candidate_pool)))),
                "source": src,
            }
        )

    # Create informative P-sized comparison batches weighted by verified status & rater score
    ids = [c["id"] for c in scored]
    by_id = {c["id"]: c for c in scored}
    rankings: list[list[str]] = []
    sorted_all = sorted(
        scored,
        key=lambda x: (-int(x["verified"]), -x["rater"]["sketch_score"], x["id"]),
    )
    ranked_ids = [c["id"] for c in sorted_all]
    for start in range(0, len(ranked_ids), max(2, min(int(batch_size), len(ranked_ids)))):
        chunk = ranked_ids[start : start + int(batch_size)]
        if len(chunk) >= 2:
            rankings.append(chunk)
    if ranked_ids and len(rankings) == 0 and len(ranked_ids) >= 2:
        rankings.append(ranked_ids)

    elo_post = fit_plackett_luce_gibbs_elo(
        rankings,
        ids,
        iterations=gibbs_iterations,
        burn_in=gibbs_burn_in,
        seed=seed,
    )
    for item in scored:
        item["elo"] = elo_post.get(item["id"], {}).get("elo", 1200.0)

    next_batch = select_thompson_batch(elo_post, batch_size=batch_size, seed=seed)
    best_parent = select_p_ucb_parent(scored, c=0.2, frontier_k=64) if scored else None

    return {
        "status": "completed",
        "total_candidates": len(scored),
        "verified_count": sum(1 for c in scored if c["verified"]),
        "elo_posterior": elo_post,
        "thompson_next_batch": next_batch,
        "p_ucb_selected_parent": best_parent["id"] if best_parent else None,
        "leaderboard": sorted(scored, key=lambda x: -float(x["elo"])),
    }


def generate_perturbed_variant(
    problem_text: str,
    *,
    constant_scale: int = 3,
    var_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Generate a parameter-perturbed / variable-renamed test variant for invariance testing (2E.5)."""
    text = str(problem_text or "")
    vmap = {"x": "u", "y": "v", "n": "k"} if var_map is None else dict(var_map)

    def _scale_num(match: re.Match[str]) -> str:
        val = int(match.group(0))
        return str(val * int(constant_scale))

    out = re.sub(r"\b\d+\b", _scale_num, text)
    if vmap:
        ordered_keys = sorted((str(k) for k in vmap if str(k)), key=len, reverse=True)
        if ordered_keys:
            var_rx = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in ordered_keys) + r")\b")
            out = var_rx.sub(lambda m: str(vmap[m.group(0)]), out)
    return {
        "original": problem_text,
        "perturbed": out,
        "constant_scale": int(constant_scale),
        "var_map": vmap,
    }


def check_benchmark_decontamination(
    candidate_problem: str,
    corpus_texts: list[str],
    *,
    ngram_size: int = 8,
) -> dict[str, Any]:
    """Detect n-gram contamination overlap against an evaluation benchmark suite (2E.5)."""
    tokens = re.findall(r"[a-zA-Z0-9_]+", (candidate_problem or "").lower())
    n = max(3, int(ngram_size))
    if len(tokens) < n:
        return {"contaminated": False, "max_overlap_ratio": 0.0, "matched_ngrams": 0}
    cand_ngrams = {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}
    max_ratio = 0.0
    max_hits = 0
    for ref in corpus_texts or ():
        rtokens = re.findall(r"[a-zA-Z0-9_]+", (ref or "").lower())
        if len(rtokens) < n:
            continue
        ref_ngrams = {tuple(rtokens[i : i + n]) for i in range(len(rtokens) - n + 1)}
        hits = len(cand_ngrams & ref_ngrams)
        ratio = hits / max(1, len(cand_ngrams))
        if hits > max_hits:
            max_hits = hits
            max_ratio = ratio
    return {
        "contaminated": max_hits > 0 and max_ratio >= 0.25,
        "max_overlap_ratio": round(max_ratio, 4),
        "matched_ngrams": max_hits,
        "ngram_size": n,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Second Brain AlphaProof Nexus Discovery Engine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_v = sub.add_parser("verify", help="Verify SafeVerify spec integrity between baseline and candidate files")
    p_v.add_argument("baseline")
    p_v.add_argument("candidate")
    p_v.add_argument("--json", action="store_true")

    p_st = sub.add_parser("elo-status", help="Inspect Lean toolchain & subgoal cache readiness")
    p_st.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "verify":
        rep = verify_spec_integrity(
            Path(args.baseline).read_text(encoding="utf-8"),
            Path(args.candidate).read_text(encoding="utf-8"),
        )
        print(json.dumps(rep, indent=2) if args.json else f"valid={rep['valid']} blocks={rep['evolve_block_count']}")
        return 0
    lean_bin = verify_lean_binary_available()
    st = {
        "lean_available": bool(lean_bin),
        "lean_path": lean_bin,
        "lean_status": "ready" if lean_bin else "lean_toolchain_unavailable",
        "subgoal_cache_path": str(_subgoal_cache_path()),
    }
    print(json.dumps(st, indent=2) if args.json else f"lean={st['lean_status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
