# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Regression suite for Phase 5 — Epic 2E: Quantitative Reasoning & Formal Discovery.

Covers offline without external servers or optional binaries:
  2E.1 LaTeX-preserving HTML/PDF/chunking + symbolic equivalence & Minerva majority@k
  2E.2 AST-jailed zero-network symbolic sandbox + CGV DUP 3-stage + ToRA/PAL solver loop
  2E.3 4D Process Rubric (40/30/20/10), Proof Economy telemetry, round caps & anti-sycophancy guard
  2E.4 AlphaProof Nexus: SafeVerify spec locks, Test-Lemma precheck, Ralph APOLLO subgoal repair,
       Agent D Batch Rater (Good vs Miracle Gaps) + Plackett-Luce Gamma-Gamma Gibbs Elo + P-UCB + Subgoal Cache
  2E.5 Parameter perturbation invariance, benchmark n-gram decontamination, Three-Store safety & 31-tool MCP parity.
"""

from __future__ import annotations

import ast
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import cgv_reasoner  # noqa: E402
import doc_extractors  # noqa: E402
import formal_proof_search  # noqa: E402
import jarvis_orchestrator  # noqa: E402
import math_equivalence  # noqa: E402
import mcp_server  # noqa: E402
import process_evaluator  # noqa: E402
import recall  # noqa: E402
import security_fs  # noqa: E402
import symbolic_sandbox  # noqa: E402
import web_clipper  # noqa: E402


class Phase10MathAndFormalDiscoveryTests(unittest.TestCase):
    def test_2e1_latex_preservation_equivalence_and_minerva_majority_k(self) -> None:
        html_math = (
            "<html><body>"
            "<p>By Euler, <script type='math/tex'>e^{i\\pi} + 1 = 0</script>.</p>"
            "<span class='katex-display'><span class='katex'><math><semantics>"
            "<annotation encoding='application/x-tex'>\\int_0^1 x^2 \\, dx = \\frac{1}{3}</annotation>"
            "</semantics></math></span></span>"
            "</body></html>"
        )
        md = web_clipper.extract_clean_markdown_from_html(html_math)
        self.assertIn("e^{i\\pi} + 1 = 0", md)
        self.assertIn("\\int_0^1 x^2", md)

        with tempfile.TemporaryDirectory(prefix="sb_2e1_") as td:
            hp = Path(td) / "math_doc.html"
            hp.write_text(html_math, encoding="utf-8")
            doc_res = doc_extractors.extract_html(str(hp))
            self.assertIn("e^{i\\pi} + 1 = 0", doc_res.markdown)
            self.assertIn("\\frac{1}{3}", doc_res.markdown)

        # Atomic chunking guard never bisects $$...$$ or \begin{align} blocks
        long_text = (
            "Intro paragraph explaining properties cleanly.\n\n"
            "$$\\begin{align} a &= \\frac{1}{2} + \\frac{1}{3} \\\\ b &= 2(x + 1)^2 \\end{align}$$\n\n"
            + ("Trailing commentary sentence. " * 20)
        )
        chunks = recall.chunk_text_preserving_latex(long_text, max_chars=140)
        self.assertTrue(any("\\begin{align}" in c and "\\end{align}" in c for c in chunks))

        # Symbolic & algebraic equivalence checks
        self.assertTrue(math_equivalence.are_math_equivalent(r"\frac{12}{36}", r"\frac{1}{3}"))
        self.assertTrue(math_equivalence.are_math_equivalent(r"(x+1)^2", r"x^2 + 2x + 1"))
        self.assertTrue(math_equivalence.are_math_equivalent("100 cm", "1 m"))
        self.assertFalse(math_equivalence.are_math_equivalent("100 cm", "1 s"))
        self.assertTrue(math_equivalence.are_math_equivalent("2x = 6", "x - 3 = 0"))
        self.assertTrue(math_equivalence.are_math_equivalent("{1, 2}", "{4/2, 1}"))

        # Minerva majority@k merges equivalent forms to beat single-pass Pass@1
        agg = math_equivalence.aggregate_majority_at_k(
            [
                "Step 1... Final Answer: \\boxed{0.5}",
                "Step 1... Final Answer: \\boxed{\\frac{12}{36}}",
                "Step 1... Final Answer: \\boxed{\\frac{1}{3}}",
            ]
        )
        self.assertEqual(agg["majority_votes"], 2)
        self.assertTrue(math_equivalence.are_math_equivalent(agg["majority_answer"], r"\frac{1}{3}"))
        metrics = math_equivalence.score_benchmark_metrics(
            [
                {
                    "ground_truth": r"\frac{1}{3}",
                    "samples": [r"\boxed{0.5}", r"\boxed{\frac{12}{36}}", r"\boxed{\frac{1}{3}}"],
                    "verified_candidate": r"\frac{1}{3}",
                }
            ]
        )
        self.assertEqual(metrics["pass_at_1"], 0.0)
        self.assertEqual(metrics["majority_at_k"], 1.0)
        self.assertEqual(metrics["verifier_pass_at_k"], 1.0)

    def test_2e2_symbolic_sandbox_escapes_and_cgv_dup_tora_loop(self) -> None:
        # Security boundary: block filesystem/network/eval/dunder reflection escapes
        for evil in (
            "import os\nos.listdir('.')",
            "import socket\nsocket.socket()",
            "open('secret.txt', 'w').write('x')",
            "eval('1+1')",
            "x = ().__class__.__mro__",
        ):
            with self.assertRaises(symbolic_sandbox.SandboxSecurityError):
                symbolic_sandbox.run_sandboxed_python(evil)

        good = symbolic_sandbox.run_sandboxed_python(
            "from fractions import Fraction\nresult = Fraction(1, 3) + Fraction(1, 6)\nprint('half:', result)"
        )
        self.assertTrue(good["ok"])
        self.assertEqual(good["result"], "1/2")

        # DUP 3-stage comprehension + unit graph dimensional mismatch detection
        dup = cgv_reasoner.decompose_dup(
            "Alice likes blue notebooks. A beam has length 10 m + 5 s in a strange formula. Find the positive velocity v = 20 m / 4 s.",
            visual_ocr_text="height = 12 m",
        )
        self.assertEqual(len(dup["stage1_target"]["visual_ocr_quantities"]), 1)
        self.assertGreaterEqual(len(dup["stage2_sentences"]["distractors_filtered"]), 1)
        self.assertTrue(any("Dimensional mismatch" in w for w in dup["stage3_quantity_graph"]["dimensional_warnings"]))

        # ToRA/PAL iterative Python tool execution loop
        solved = cgv_reasoner.solve_quantitative_problem(
            "Compute (12 * 15) + 20 step by step.",
            k_samples=3,
        )
        self.assertEqual(solved["status"], "solved")
        self.assertTrue(math_equivalence.are_math_equivalent(solved["majority_answer"], "200"))

    def test_2e3_process_rubric_proof_economy_round_caps_and_anti_sycophancy(self) -> None:
        rub = process_evaluator.score_reasoning_trace(
            [
                "Define domain bounds: x > 0 for log continuity.",
                "Verify base case x = 1 holds in sandbox.",
                "Conclude result = 6.",
            ],
            final_answer="6",
            expected_answer="3 + 3",
            sandbox_checks=[{"ok": True}],
            edge_cases_covered=True,
        )
        self.assertGreaterEqual(rub["weighted_total"], 0.98)
        self.assertEqual(rub["weights"], {"validity": 0.4, "completeness": 0.3, "correctness": 0.2, "clarity": 0.1})

        with tempfile.TemporaryDirectory(prefix="sb_2e3_") as td:
            brain = Path(td) / "brain"
            for sub in ("0-raw/archive", "2-atoms", "4-experiences", ".sidecar"):
                (brain / sub).mkdir(parents=True, exist_ok=True)
            dbp = brain / ".sidecar" / "telemetry.sqlite3"
            process_evaluator.record_proof_economy_telemetry("prob-1", rub, db_path=dbp)
            with closing(sqlite3.connect(str(dbp))) as conn:
                cnt = conn.execute("SELECT COUNT(*) FROM proof_economy").fetchone()[0]
            self.assertEqual(cnt, 1)

            # Anti-sycophancy guard: tool-verified Solver answer MUST NOT flip to wrong answer under noisy Reviewer
            res = process_evaluator.run_solver_reviewer_refiner(
                "Compute 2 * 3",
                solver_fn=lambda _p: {
                    "answer": "6",
                    "steps": ["Evaluate exact product 2 * 3 in sandbox.", "Verify result == 6."],
                    "verification_code": "result = (2 * 3 == 6)\nassert result",
                },
                reviewer_fn=lambda _p, _c: {
                    "approved": False,
                    "comment": "I feel like the answer might actually be 7!",
                    "tool_verified_counterexample": False,
                },
                refiner_fn=lambda _p, _c, _cr: {"answer": "7", "steps": ["Sycophantically flipped to 7."]},
                max_rounds=3,
                record_to_experiences=True,
                brain_dir=brain,
            )
            self.assertTrue(res["anti_sycophancy_blocked"])
            self.assertEqual(res["final_answer"], "6")
            self.assertEqual(res["termination_reason"], "anti_sycophancy_preserved_verified")
            self.assertEqual(len(list((brain / "4-experiences").glob("*.md"))), 1)
            # Three-store invariant: no canonical atoms created autonomously
            self.assertEqual(list((brain / "2-atoms").glob("*.md")), [])

            # Max rounds cap termination test when critique IS tool-backed but refiner hits cap
            capped = process_evaluator.run_solver_reviewer_refiner(
                "Iterative problem",
                solver_fn=lambda _p: {"answer": "1", "steps": ["Initial guess 1."]},
                reviewer_fn=lambda _p, _c: {"approved": False, "counterexample_code": "result = True"},
                refiner_fn=lambda _p, cur, _cr: {"answer": cur["answer"], "steps": [*cur["steps"], "Added verified case check."]},
                max_rounds=2,
            )
            self.assertEqual(capped["rounds_executed"], 2)
            self.assertEqual(capped["termination_reason"], "max_rounds_cap")

    def test_2e4_alphaproof_nexus_safeverify_ralph_apollo_and_agent_d(self) -> None:
        baseline = (
            "def sum_first_n(n: int) -> int:\n"
            "    # EVOLVE-BLOCK-START\n"
            "    return 0\n"
            "    # EVOLVE-BLOCK-END\n"
        )
        # 1. SafeVerify blocks tampering outside EVOLVE-BLOCK & signature rewrites & sandbox escapes & sorryAx
        tampered_outside = baseline.replace("sum_first_n", "cheat_fn")
        with self.assertRaises(formal_proof_search.SafeVerifyError):
            formal_proof_search.verify_spec_integrity(baseline, tampered_outside)

        tampered_io = formal_proof_search.replace_evolve_blocks(baseline, ["    import os\n    return n"])
        with self.assertRaises(formal_proof_search.SafeVerifyError):
            formal_proof_search.verify_spec_integrity(baseline, tampered_io)

        lean_base = (
            "theorem add_zero_right (n : Nat) : n + 0 = n := by\n"
            "  -- EVOLVE-BLOCK-START\n"
            "  rfl\n"
            "  -- EVOLVE-BLOCK-END\n"
        )
        lean_cheat = formal_proof_search.replace_evolve_blocks(lean_base, ["  sorryAx"])
        with self.assertRaises(formal_proof_search.SafeVerifyError):
            formal_proof_search.verify_spec_integrity(lean_base, lean_cheat)

        # Clean degrade when lean binary is absent (or verified if installed)
        with patch("shutil.which", return_value=None):
            lean_res = formal_proof_search.run_ralph_proof_loop(
                lean_base,
                mutate_fn=lambda _b, _i, _f: ["  rfl"],
                test_lemmas=["#check add_zero_right"],
            )
            self.assertEqual(lean_res["status"], "lean_toolchain_unavailable")
            self.assertFalse(lean_res["verified"])

        # 2. Test-Lemma misformalization gate + Ralph APOLLO subgoal isolation + Subgoal Cache
        with tempfile.TemporaryDirectory(prefix="sb_2e4_") as td:
            brain = Path(td) / "brain"
            for sub in ("0-raw/archive", "2-atoms", "4-experiences", ".sidecar"):
                (brain / sub).mkdir(parents=True, exist_ok=True)

            bad_tl = formal_proof_search.run_test_lemmas(baseline, ["assert sum_first_n(3) == 6"])
            self.assertFalse(bad_tl["passed"])
            self.assertEqual(bad_tl["status"], "misformalized_spec")

            def _ralph_mut(_src: str, iteration: int, _fb: str) -> list[str]:
                if iteration == 1:
                    return ["    return n * n"]
                return ["    return (n * (n + 1)) // 2"]

            ralph_res = formal_proof_search.run_ralph_proof_loop(
                baseline,
                mutate_fn=_ralph_mut,
                test_lemmas=["assert sum_first_n(0) == 0", "assert sum_first_n(1) == 1"],
                verification_tests=["assert [sum_first_n(k) for k in range(10)] == [sum(range(k+1)) for k in range(10)]"],
                brain_dir=brain,
            )
            self.assertTrue(ralph_res["verified"])
            self.assertEqual(ralph_res["iterations"], 2)
            self.assertIsNotNone(ralph_res["staged_artifact_path"])
            self.assertTrue(Path(ralph_res["staged_artifact_path"]).is_file())
            self.assertEqual(list((brain / "2-atoms").glob("*.md")), [])

            # APOLLO subgoal isolation + Global Subgoal Cache hit on 2nd run
            subgoals = [{"id": "sg1", "statement": "sum(1..n) invariant", "initial_fragment": "wrong"}]
            sg_rep1 = formal_proof_search.isolate_and_retry_subgoals(
                subgoals,
                spec_sha256="spec123",
                repair_subgoal_fn=lambda _id, _st, fb: "wrong" if not fb else "good_proof",
                verify_subgoal_fn=lambda _st, frag: (frag == "good_proof", "err" if frag != "good_proof" else ""),
                brain_dir=brain,
            )
            self.assertTrue(sg_rep1["all_verified"])
            self.assertEqual(sg_rep1["cache_hits"], 0)
            sg_rep2 = formal_proof_search.isolate_and_retry_subgoals(
                subgoals,
                spec_sha256="spec123",
                repair_subgoal_fn=lambda _id, _st, _fb: "unused",
                verify_subgoal_fn=lambda _st, frag: (frag == "good_proof", ""),
                brain_dir=brain,
            )
            self.assertEqual(sg_rep2["cache_hits"], 1)

            # 3. Mode B Agent D: P=7 Batch Rater (Good vs Miracle Gaps) + Plackett-Luce Gamma-Gamma Gibbs Elo + P-UCB
            good_gap = formal_proof_search.score_batch_rater_decomposition("def helper(x):\n    assert invariant\n    return x")
            miracle_gap = formal_proof_search.score_batch_rater_decomposition("# TODO magic hack sorry assume_false")
            self.assertGreater(good_gap["sketch_score"], miracle_gap["sketch_score"])

            cands = [
                {"id": "exact_closed_form", "replacements": ["    # helper invariant base_case\n    return (n * (n + 1)) // 2"]},
                {"id": "loop_impl", "replacements": ["    s = 0\n    for i in range(n + 1):\n        s += i\n    return s"]},
                {"id": "wrong_const", "replacements": ["    return 42"]},
                {"id": "miracle_bad", "replacements": ["    # TODO magic sorry\n    return 0"]},
                {"id": "linear_bad", "replacements": ["    return n"]},
                {"id": "double_bad", "replacements": ["    return 2 * n"]},
                {"id": "neg_bad", "replacements": ["    return -n"]},
            ]
            agent_d = formal_proof_search.run_agent_d_search(
                baseline,
                cands,
                verification_tests=["assert sum_first_n(5) == 15 and sum_first_n(0) == 0"],
                batch_size=7,
                gibbs_iterations=1000,
                gibbs_burn_in=200,
            )
            self.assertEqual(agent_d["verified_count"], 2)
            elo_top = agent_d["leaderboard"][0]["id"]
            self.assertIn(elo_top, {"exact_closed_form", "loop_impl"})
            self.assertGreater(agent_d["elo_posterior"]["exact_closed_form"]["elo"], agent_d["elo_posterior"]["miracle_bad"]["elo"])
            self.assertEqual(len(agent_d["thompson_next_batch"]), 7)
            self.assertIn(agent_d["p_ucb_selected_parent"], {c["id"] for c in cands})

    def test_2e5_perturbation_decontamination_and_mcp_api_parity(self) -> None:
        base_prob = "Solve 2 * x + 6 = 10 for x."
        pert = formal_proof_search.generate_perturbed_variant(base_prob, constant_scale=3, var_map={"x": "u"})
        self.assertIn("6 * u + 18 = 30", pert["perturbed"])

        # Benchmark decontamination n-gram detector
        clean_decont = formal_proof_search.check_benchmark_decontamination(
            "Find the sum of primes below twenty using constructive sieve.",
            ["Compute the area of a circle with radius five using standard pi formulas."],
        )
        self.assertFalse(clean_decont["contaminated"])

        leaky = "A very distinctive olympiad geometry problem with nine points on an inscribed circle chord."
        flagged = formal_proof_search.check_benchmark_decontamination(
            leaky + " Prove collinearity.",
            [leaky],
            ngram_size=6,
        )
        self.assertTrue(flagged["contaminated"])

        # MCP verify_symbolic tool & 31-tool verification
        from mcp import types

        token = "math-mcp-secret-token-0123456789"
        with tempfile.TemporaryDirectory(prefix="sb_2e5_mcp_") as td:
            brain = Path(td) / "brain"
            for sub in ("0-raw/archive", "0-raw/inbox", "2-atoms", "3-threads", "5-playbooks", "6-skills", "briefings", ".sidecar"):
                (brain / sub).mkdir(parents=True, exist_ok=True)
            audit_p = brain / "0-raw" / "archive" / "mcp-audit.jsonl"
            with (
                patch.object(mcp_server, "BRAIN_DIR", str(brain)),
                patch.object(mcp_server, "ATOMS_DIR", str(brain / "2-atoms")),
                patch.object(mcp_server, "RAW_DIR", str(brain / "0-raw")),
                patch.object(mcp_server, "INBOX_DIR", str(brain / "0-raw" / "inbox")),
                patch.object(mcp_server, "BRIEFINGS_DIR", str(brain / "briefings")),
                patch.object(mcp_server, "THREADS_DIR", str(brain / "3-threads")),
                patch.object(mcp_server, "MCP_AUDIT_PATH", str(audit_p)),
                patch.dict(os.environ, {"BRAIN_MCP_BEARER_TOKEN": token}, clear=False),
            ):
                ctx = SimpleNamespace(meta={"authorization": f"Bearer {token}"})
                listed = asyncio.run(mcp_server.handle_list_tools(ctx, None))
                self.assertEqual(len(listed.tools), 35)
                eq_res = asyncio.run(
                    mcp_server.handle_call_tool(
                        ctx,
                        types.CallToolRequestParams(
                            name="verify_symbolic",
                            arguments={"mode": "equiv", "expr_a": r"\frac{12}{36}", "expr_b": r"\frac{1}{3}"},
                            _meta={"authorization": f"Bearer {token}"},
                        ),
                    )
                )
                self.assertFalse(eq_res.is_error)
                self.assertTrue(json.loads(eq_res.content[0].text)["equivalent"])

                bad_sb = asyncio.run(
                    mcp_server.handle_call_tool(
                        ctx,
                        types.CallToolRequestParams(
                            name="verify_symbolic",
                            arguments={"mode": "sandbox", "code": "import os"},
                            _meta={"authorization": f"Bearer {token}"},
                        ),
                    )
                )
                self.assertTrue(bad_sb.is_error)

    def test_2e6_adversarial_sandbox_escape_and_runtime_isolation(self) -> None:
        # AST-level sandbox escape vectors MUST raise SandboxSecurityError
        escape_payloads = (
            "from fractions import sys\nresult = sys.modules",
            "import fractions\nresult = fractions.sys.modules['os']",
            "import fractions.sys",
            "from fractions import *",
            "from fractions import _gcd",
            "import fractions\nresult = fractions.operator.attrgetter('__traceback__')",
            "from fractions import operator",
            "import re\nresult = re.enum.bltns",
            "from re import enum",
            "import json\nresult = json.decoder",
            "g = (x for x in [1])\nresult = g.gi_frame.f_back.f_globals",
            "async def f():\n    pass\nc = f()\nresult = c.cr_frame.f_builtins",
            'result = "{0.__init__.__globals__}".format(Fraction)',
            'result = "{a.__init__.__globals__}".format_map({"a": Fraction})',
            "try:\n    1 / 0\nexcept Exception as e:\n    x = e.tb_frame",
            "import math as __builtins__",
            "from math import sin as eval",
        )
        for payload in escape_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(symbolic_sandbox.SandboxSecurityError):
                    symbolic_sandbox.run_sandboxed_python(payload)

        # Defense-in-depth runtime verification (even if AST validation were bypassed)
        harness_sample = symbolic_sandbox._build_runner_harness("result = 1", [])
        self.assertIn("assert _audit_hook.__closure__ is None", harness_sample)
        with tempfile.TemporaryDirectory(prefix="sb_2e6_rt_") as td:
            # 1. __main__ globals MUST NOT expose _orig_import, _raw_exec, _raw_compile, io, builtins, or sys
            probe_main = (
                "import fractions\n"
                "m = fractions.sys.modules['__main__'].__dict__\n"
                "result = sorted(m.keys())\n"
            )
            p_main = Path(td) / "probe_main.py"
            p_main.write_text(symbolic_sandbox._build_runner_harness(probe_main, []), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-I", "-B", str(p_main)],
                capture_output=True,
                text=True,
                env=symbolic_sandbox._build_clean_subprocess_env(td),
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out_payload = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertEqual(out_payload["result"], "['__name__']")

            # 2. Traceback frame traversal MUST NOT recover _orig_import, _audit_hook, _b, _sys,
            #    _allowed_read_roots, setattr, getattr, or open, AND re.enum.bltns is scrubbed!
            probe_frames = (
                "import fractions, re\n"
                "forbidden_keys = {'_orig_import', '_audit_hook', '_b', '_sys', '_allowed_read_roots',\n"
                "                  'setattr', 'getattr', 'delattr', 'open', 'eval', 'exec', 'compile',\n"
                "                  'vars', 'globals', 'locals', 'breakpoint', 'input'}\n"
                "leaked = []\n"
                "try:\n"
                "    1 / 0\n"
                "except Exception as e:\n"
                "    f = e.__traceback__.tb_frame\n"
                "    while f is not None:\n"
                "        for scope in (f.f_locals, f.f_globals, f.f_builtins):\n"
                "            for k in list(scope):\n"
                "                if k in forbidden_keys:\n"
                "                    leaked.append(k)\n"
                "        f = f.f_back\n"
                "bltns_leaked = [k for k in forbidden_keys if k in re.enum.bltns.__dict__]\n"
                "result = repr((sorted(set(leaked)), sorted(set(bltns_leaked))))\n"
            )
            p_frames = Path(td) / "probe_frames.py"
            p_frames.write_text(symbolic_sandbox._build_runner_harness(probe_frames, []), encoding="utf-8")
            proc_f = subprocess.run(
                [sys.executable, "-I", "-B", str(p_frames)],
                capture_output=True,
                text=True,
                env=symbolic_sandbox._build_clean_subprocess_env(td),
                check=False,
            )
            self.assertEqual(proc_f.returncode, 0, proc_f.stderr)
            out_frames = json.loads(proc_f.stdout.strip().splitlines()[-1])
            self.assertEqual(out_frames["result"], "([], [])")

            # 3. Audit hook & meta_path block OS/IO/import/frame/mmap escapes at runtime
            for rt_evil in (
                "import os",
                "import mmap",
                "from fractions import sys",
                "import fractions\nfractions.sys.modules['os'].system('echo pwned')",
                "import fractions\nfractions.sys.modules['_io'].open('pwned.txt', 'w')",
                "import fractions\nfractions.sys._current_frames()",
                "import fractions\nfractions.sys._getframe()",
                "import re\nre.enum.bltns.setattr(re, 'x', 1)",
                "import re\nre.enum.bltns.open('pwned.txt', 'w')",
            ):
                p_ev = Path(td) / "probe_ev.py"
                p_ev.write_text(symbolic_sandbox._build_runner_harness(rt_evil, []), encoding="utf-8")
                pr = subprocess.run(
                    [sys.executable, "-I", "-B", str(p_ev)],
                    capture_output=True,
                    text=True,
                    env=symbolic_sandbox._build_clean_subprocess_env(td),
                    check=False,
                )
                self.assertNotEqual(pr.returncode, 0, f"Runtime defense failed to block: {rt_evil}")

        # Env scrubbing strips API keys, AWS credentials, and BRAIN_* secrets
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "sk-test-openai",
                "ANTHROPIC_API_KEY": "sk-ant-test",
                "BRAIN_MCP_BEARER_TOKEN": "secret-brain-token",
                "AWS_SECRET_ACCESS_KEY": "aws-secret",
            },
            clear=False,
        ):
            clean_env = symbolic_sandbox._build_clean_subprocess_env(tempfile.gettempdir())
            for secret_key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "BRAIN_MCP_BEARER_TOKEN", "AWS_SECRET_ACCESS_KEY"):
                self.assertNotIn(secret_key, clean_env)

        # Bounded stdout capture & wall-clock timeout
        flood = symbolic_sandbox.run_sandboxed_python("for _ in range(3000):\n    print('X' * 50)\nresult = 99\n")
        self.assertTrue(flood["ok"])
        self.assertEqual(flood["result"], "99")
        self.assertLessEqual(len(flood["stdout"]), symbolic_sandbox.MAX_STDOUT_CHARS)

        t_out = symbolic_sandbox.run_sandboxed_python("while True:\n    pass\n", timeout_seconds=0.6)
        self.assertFalse(t_out["ok"])
        self.assertIn("timed out", str(t_out["error"]).lower())

        # Legitimate stdlib math/symbolic modules & user classes remain fully functional
        legit = symbolic_sandbox.run_sandboxed_python(
            "import math, cmath, fractions, decimal, itertools, statistics, json, re, collections, functools\n"
            "from fractions import Fraction\n"
            "from decimal import Decimal\n"
            "class Poly:\n"
            "    def eval_at(self, x):\n"
            "        return x * x + 1\n"
            "s = sum(Fraction(1, k) for k in range(1, 5))\n"
            "m = statistics.mean([2, 4, 6])\n"
            "d = Decimal('0.1') + Decimal('0.2')\n"
            "c = len(list(itertools.combinations([1, 2, 3, 4], 2)))\n"
            "nums = re.findall(r'\\d+', 'a12b34')\n"
            "top = collections.Counter('abracadabra').most_common(1)[0][0]\n"
            "p2 = functools.partial(pow, 2)(4)\n"
            "pv = Poly().eval_at(3)\n"
            "result = json.dumps({'s': str(s), 'm': m, 'd': str(d), 'c': c, 'nums': nums, 'top': top, 'p2': p2, 'pv': pv}, sort_keys=True)\n"
        )
        self.assertTrue(legit["ok"], legit)
        self.assertEqual(
            json.loads(legit["result"]),
            {"c": 6, "d": "0.3", "m": 4, "nums": ["12", "34"], "p2": 16, "pv": 10, "s": "25/12", "top": "a"},
        )

    def test_2e7_math_equivalence_security_and_edge_cases(self) -> None:
        # Confirm sp.sympify() is completely absent from math_equivalence.py
        me_src = (ROOT / "scripts" / "math_equivalence.py").read_text(encoding="utf-8")
        self.assertNotIn("sympify(", me_src)

        # Adversarial RCE / attribute / subscript / keyword payloads MUST be rejected safely without executing
        executed: list[str] = []
        for evil in (
            "__import__('os').system('echo pwned')",
            "__import__('os')",
            "os.system('echo pwned')",
            "x.foo()",
            "x[0]()",
            "x[0]",
            "sin(x, kw=1)",
            "(lambda z: z)(1)",
            "[x for x in [1]]",
            "_private_var",
            "True",
            "'string_const'",
        ):
            with self.subTest(evil=evil):
                self.assertFalse(math_equivalence.are_math_equivalent(evil, evil))
                self.assertFalse(math_equivalence.are_math_equivalent(evil, "0"))
                self.assertFalse(math_equivalence.are_math_equivalent("0", evil))
        self.assertEqual(executed, [])

        # Scientific notation edge cases
        self.assertEqual(math_equivalence.normalize_latex_expr("1e-5"), "1e-5")
        self.assertEqual(math_equivalence.normalize_latex_expr("2.5E+3"), "2.5E+3")
        self.assertEqual(math_equivalence.normalize_latex_expr("1e5"), "1e5")
        self.assertTrue(math_equivalence.are_math_equivalent("1e-5", "0.00001"))
        self.assertFalse(math_equivalence.are_math_equivalent("1e-5", "e - 5"))
        self.assertTrue(math_equivalence.are_math_equivalent("2.5E+3", "2500"))
        self.assertTrue(math_equivalence.are_math_equivalent("1e5", "100000"))

        # Variable cancellation & logarithms (even without SymPy)
        self.assertTrue(math_equivalence.are_math_equivalent("x - x", "0"))
        self.assertTrue(math_equivalence.are_math_equivalent("x + y - y", "x"))
        self.assertFalse(math_equivalence.are_math_equivalent("x", "0"))
        self.assertTrue(math_equivalence.are_math_equivalent("log(100)", "2"))
        self.assertTrue(math_equivalence.are_math_equivalent("ln(e^2)", "2"))
        self.assertTrue(math_equivalence.are_math_equivalent("factorial(5)", "120"))

        # Verify AST-to-SymPy walker works directly without sympify
        calls: list[str] = []

        class _MockExpr:
            def __init__(self, text: str) -> None:
                self.text = text

            def __add__(self, other: Any) -> "_MockExpr":
                return _MockExpr(f"({self.text}+{other})")

            def __sub__(self, other: Any) -> "_MockExpr":
                return _MockExpr(f"({self.text}-{other})")

            def __mul__(self, other: Any) -> "_MockExpr":
                return _MockExpr(f"({self.text}*{other})")

            def __truediv__(self, other: Any) -> "_MockExpr":
                return _MockExpr(f"({self.text}/{other})")

            def __pow__(self, other: Any) -> "_MockExpr":
                return _MockExpr(f"({self.text}**{other})")

            def __str__(self) -> str:
                return self.text

            def __eq__(self, other: object) -> bool:
                return self.text == str(other)

        class _MockSymPy:
            pi = _MockExpr("PI")
            E = _MockExpr("E")
            I = _MockExpr("I")

            @staticmethod
            def Integer(v: int) -> _MockExpr:
                return _MockExpr(f"Int({v})")

            @staticmethod
            def Rational(s: str) -> _MockExpr:
                return _MockExpr(f"Rat({s})")

            @staticmethod
            def Symbol(name: str) -> _MockExpr:
                return _MockExpr(f"Sym({name})")

            @staticmethod
            def log(x: Any, base: Any = None) -> str:
                calls.append(f"log({x},{base})")
                return f"log({x},{base})"

            @staticmethod
            def sqrt(x: Any) -> str:
                return f"sqrt({x})"

            @staticmethod
            def factorial(x: Any) -> str:
                return f"fact({x})"

        tree_log = math_equivalence._validate_math_ast(ast.parse("log(100)", mode="eval"))
        self.assertEqual(math_equivalence._ast_to_sympy(tree_log, _MockSymPy), "log(Int(100),10)")
        tree_ln = math_equivalence._validate_math_ast(ast.parse("ln(x)", mode="eval"))
        self.assertEqual(math_equivalence._ast_to_sympy(tree_ln, _MockSymPy), "log(Sym(x),None)")
        tree_fact_ok = math_equivalence._validate_math_ast(ast.parse("factorial(5)", mode="eval"))
        self.assertEqual(math_equivalence._ast_to_sympy(tree_fact_ok, _MockSymPy), "fact(Int(5))")
        tree_pow_ok = math_equivalence._validate_math_ast(ast.parse("(x + 1)**2", mode="eval"))
        self.assertEqual(math_equivalence._ast_to_sympy(tree_pow_ok, _MockSymPy), "((Sym(x)+Int(1))**Int(2))")

        # SymPy walker DoS bounds: reject huge/negative/symbolic factorials, huge exponents, & deep nested powers
        for dos_expr in (
            "factorial(10000)",
            "factorial(171)",
            "factorial(-1)",
            "factorial(x)",
            "2**(2**(2**100))",
            "x**1001",
            "x**(-1001)",
            "x**(x**(x**(x**2)))",
        ):
            with self.subTest(dos_expr=dos_expr):
                dos_tree = math_equivalence._validate_math_ast(ast.parse(dos_expr, mode="eval"))
                with self.assertRaises(ValueError):
                    math_equivalence._ast_to_sympy(dos_tree, _MockSymPy)
        self.assertFalse(math_equivalence.are_math_equivalent("factorial(10000)", "1"))
        self.assertFalse(math_equivalence.are_math_equivalent("2**(2**(2**100))", "1"))

    def test_2e8_formal_proof_lean_hardening_timeout_perturbation_and_cgv_polish(self) -> None:
        lean_base = (
            "theorem add_zero_right (n : Nat) : n + 0 = n := by\n"
            "  -- EVOLVE-BLOCK-START\n"
            "  rfl\n"
            "  -- EVOLVE-BLOCK-END\n"
        )
        forbidden_lean_snippets = (
            "#eval IO.println 1",
            "#check_failure Nat",
            "#reduce 1 + 1",
            "#print Nat",
            "run_cmd do pure ()",
            "initialize x : Nat := pure 0",
            "builtin_initialize y : Nat := pure 0",
            "@[init] def f : IO Unit := pure ()",
            "open IO\nrfl",
            "open System\nrfl",
            "open Lean\nrfl",
            "import Lake\nrfl",
            "IO.Process.spawn {}",
            "System.FilePath.mk \"/tmp\"",
            "@[extern \"c_fn\"] def f : Nat := 0",
            "@[implemented_by f] def g : Nat := 0",
            "native_decide",
            "opaque secret : Nat := 0",
            "partial def loop : Nat := loop",
            "def f := by\n  have : True := axiom_foo; axiom bad : False",
            "elab \"foo\" : term => pure default",
            "macro \"bar\" : term => `(0)",
            "syntax \"baz\" : term",
            "import Lean\nrfl",
            "import Init.System\nrfl",
            "set_option maxRecDepth 999999",
        )
        # Reject forbidden Lean constructs in candidate_source
        for bad in forbidden_lean_snippets:
            with self.subTest(where="candidate_source", bad=bad):
                cand = formal_proof_search.replace_evolve_blocks(lean_base, [f"  {bad}"])
                with self.assertRaises(formal_proof_search.SafeVerifyError):
                    formal_proof_search.verify_spec_integrity(lean_base, cand)
                with self.assertRaises(formal_proof_search.SafeVerifyError):
                    formal_proof_search.verify_lean_proof(cand)

        # Reject forbidden Lean constructs in test_lemmas BEFORE running lean
        for bad_lemma in ("#eval 1 + 1", "def x := IO.Process.run", "@[extern] def f : Nat := 0", "theorem t : 1 = 1 := by native_decide"):
            with self.subTest(where="test_lemmas", bad_lemma=bad_lemma):
                with self.assertRaises(formal_proof_search.SafeVerifyError):
                    formal_proof_search.run_test_lemmas(lean_base, [bad_lemma], language="lean")

        # Handle subprocess.TimeoutExpired cleanly AND scrub env in both run_test_lemmas and verify_lean_proof
        captured_envs: list[dict[str, str]] = []

        def _fake_timeout_run(*_args: Any, **kwargs: Any) -> Any:
            captured_envs.append(dict(kwargs.get("env") or {}))
            raise subprocess.TimeoutExpired(cmd=["lean"], timeout=1.0)

        with (
            patch("shutil.which", return_value="/usr/bin/lean"),
            patch.object(formal_proof_search.subprocess, "run", side_effect=_fake_timeout_run),
            patch.dict(os.environ, {"OPENAI_API_KEY": "sk-leak", "BRAIN_MCP_BEARER_TOKEN": "tok-leak"}, clear=False),
        ):
            tl_to = formal_proof_search.run_test_lemmas(lean_base, ["#check add_zero_right"], language="lean", timeout_seconds=1.0)
            self.assertFalse(tl_to["passed"])
            self.assertEqual(tl_to["status"], "lean_timeout")

            vp_to = formal_proof_search.verify_lean_proof(lean_base, timeout_seconds=1.0, spec_sha256="abc")
            self.assertFalse(vp_to["verified"])
            self.assertEqual(vp_to["status"], "lean_timeout")

        self.assertEqual(len(captured_envs), 2)
        for env_used in captured_envs:
            self.assertNotIn("OPENAI_API_KEY", env_used)
            self.assertNotIn("BRAIN_MCP_BEARER_TOKEN", env_used)

        # Simultaneous variable swap {"x": "y", "y": "x"} does NOT corrupt into all "x"
        swapped = formal_proof_search.generate_perturbed_variant(
            "Find x + 2 * y given x - y = 3.",
            constant_scale=2,
            var_map={"x": "y", "y": "x"},
        )
        self.assertEqual(swapped["perturbed"], "Find y + 4 * x given y - x = 6.")

        # CGV Reasoner: rubric_score attached + loopback localhost case normalization + redirect rejection + fail-closed fallback
        with patch.dict(os.environ, {"OLLAMA_HOST": "http://LOCALHOST:11434"}, clear=False):
            self.assertEqual(cgv_reasoner._loopback_ollama_chat_url(), "http://127.0.0.1:11434/api/chat")
        self.assertEqual(
            jarvis_orchestrator.validate_loopback_service_url("http://LOCALHOST:5678/webhook"),
            "http://127.0.0.1:5678/webhook",
        )
        self.assertEqual(
            jarvis_orchestrator.validate_loopback_service_url("http://LocalHost"),
            "http://127.0.0.1",
        )
        no_redir = cgv_reasoner._NoRedirectHandler()
        with self.assertRaises(cgv_reasoner.urllib.error.HTTPError):
            no_redir.redirect_request(
                cgv_reasoner.urllib.request.Request("http://127.0.0.1:11434/api/chat"),
                None,
                302,
                "Found",
                {},
                "http://169.254.169.254/latest/meta-data/",
            )

        with tempfile.TemporaryDirectory(prefix="sb_secfs_") as sec_td:
            ok_f = Path(sec_td) / "token.txt"
            ok_f.write_text("safe-token", encoding="utf-8")
            self.assertEqual(security_fs.read_bounded_security_file(ok_f, max_bytes=64), "safe-token")
            big_f = Path(sec_td) / "big.txt"
            big_f.write_bytes(b"A" * 128)
            with self.assertRaises(PermissionError):
                security_fs.read_bounded_security_file(big_f, max_bytes=64)

        with patch.dict(os.environ, {"OLLAMA_HOST": "http://192.168.1.99:11434"}, clear=False):
            solved_llm_fallback = cgv_reasoner.solve_quantitative_problem(
                "Compute (10 * 5) + 7.",
                k_samples=1,
                use_local_llm=True,
                llm_timeout_seconds=1.0,
            )
        self.assertEqual(solved_llm_fallback["status"], "solved")
        self.assertTrue(math_equivalence.are_math_equivalent(solved_llm_fallback["majority_answer"], "57"))
        self.assertIsInstance(solved_llm_fallback.get("rubric_score"), dict)
        self.assertGreater(solved_llm_fallback["rubric_score"]["weighted_total"], 0.7)


if __name__ == "__main__":
    unittest.main()
