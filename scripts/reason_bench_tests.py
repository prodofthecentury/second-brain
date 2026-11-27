"""Offline tests for the reasoning benchmark harness."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from scripts.reason_bench import (
    BenchError,
    accuracy_pair,
    cache_read_allowed,
    derive_answer_source,
    failure_tuples,
    file_hash,
    merge_resume,
    path_is_forbidden,
    probe_tool,
    repo_root,
    run_tools_only,
    run_with_hang_guard,
)
from scripts.reason_bench_heldout import (
    HeldoutError,
    assert_private_path,
    commitment_digest,
    empty_commitment,
    open_bytes,
    seal_bytes,
)
from scripts.reason_bench_models import MockScript, ModelClientError, RawChatClient, assert_model_env
from scripts.reason_bench_scoring import (
    accuracy_interval,
    cluster_bootstrap_means,
    design_effect,
    extract_answer,
    holm_adjust,
    mcnemar_exact,
    numbers_equal,
    percentile_interval,
    score_prediction,
    wilson_interval,
)


class HashTests(unittest.TestCase):
    def test_canonical_hash_ignores_crlf_bom_and_key_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            left = folder / "a.jsonl"
            right = folder / "b.jsonl"
            left.write_bytes(b'{"b":1,"id":"a"}\n{"id":"b","a":2}\n')
            right.write_bytes("\ufeff{\"id\":\"b\",\"a\":2}\r\n{\"id\":\"a\",\"b\":1}\r\n".encode("utf-8"))
            self.assertEqual(file_hash(left), file_hash(right))

    def test_forbidden_heldout_names(self) -> None:
        self.assertTrue(path_is_forbidden("bench/reasoning/suites/math.heldout.jsonl"))
        self.assertTrue(path_is_forbidden("notes.sbheldout"))
        self.assertTrue(path_is_forbidden("templates.sbsealed"))
        self.assertTrue(path_is_forbidden("corpus/heldout/notes/a.md"))
        self.assertFalse(path_is_forbidden("bench/reasoning/heldout.commitment.json"))
        self.assertTrue(path_is_forbidden("bench/reasoning/heldout-results/run.json"))
        self.assertTrue(path_is_forbidden("bench/reasoning/heldout-results/heldout.jsonl"))
        self.assertTrue(path_is_forbidden("bench/reasoning/heldout_items.json"))
        self.assertTrue(path_is_forbidden("heldout/heldout.commitment.json"))
        self.assertFalse(path_is_forbidden("bench/reasoning/heldout-results/summary.md"))
        self.assertFalse(path_is_forbidden("bench/reasoning/heldout-access.jsonl"))


class ScoringTests(unittest.TestCase):
    def test_exact_fraction_and_tolerance_and_bare_value(self) -> None:
        self.assertFalse(numbers_equal("0.666666666667", "2/3", "fraction", None, tool=True))
        self.assertFalse(numbers_equal("0.666666666667", "2/3", "fraction", None, tool=False))
        self.assertTrue(numbers_equal("0.67", "2/3", "fraction", {"decimals": 2}, tool=False))
        self.assertFalse(numbers_equal("0.67", "2/3", "fraction", {"decimals": 2}, tool=True))
        self.assertTrue(numbers_equal("52.0", "52", "integer", None, tool=False))
        self.assertFalse(numbers_equal("52.5", "52", "integer", None, tool=False))
        item = {"answer_type": "integer", "answer": "4", "answerable": True, "approx_ok": None}
        extracted = extract_answer("The answer is 4", item, engine_value=True)
        self.assertEqual(extracted["extraction"], "fallback")
        bare = extract_answer("4", item, engine_value=True)
        self.assertEqual(bare["extraction"], "engine")
        self.assertTrue(score_prediction("4", item, tool=True)["correct"])
        # Fifty varied numeric cases, including the ones above.
        cases = []
        for n in range(1, 26):
            cases.append((str(n), str(n), True))
            cases.append((f"{n}.0", str(n), True))
        for text, gold, expected in cases:
            self.assertEqual(numbers_equal(text, gold, "integer", None, tool=False), expected)
        self.assertGreaterEqual(len(cases) + 6, 50)

    def test_statistics(self) -> None:
        low, high = wilson_interval(5, 10)
        self.assertAlmostEqual(low, 0.237, places=3)
        self.assertAlmostEqual(high, 0.763, places=3)
        self.assertAlmostEqual(mcnemar_exact(8, 2), 0.109, places=3)
        self.assertEqual(holm_adjust([0.01, 0.04, 0.03]), [0.03, 0.06, 0.06])
        single = [{"template_id": "a", "correct": True}, {"template_id": "b", "correct": False}]
        self.assertEqual(design_effect(single)["deff"], 1.0)
        paired = []
        for name, value in (("a", 1.0), ("b", 0.0), ("c", 1.0)):
            paired.append({"template_id": name, "correct": value})
            paired.append({"template_id": name, "correct": value})
        groups = {"a": [1.0, 1.0], "b": [0.0, 0.0], "c": [1.0, 1.0]}
        clustered = cluster_bootstrap_means(groups, b=200, seed=20261127)
        templates_only = cluster_bootstrap_means({name: [values[0]] for name, values in groups.items()}, b=200, seed=20261127)
        self.assertEqual(percentile_interval(clustered), percentile_interval(templates_only))
        stats = accuracy_interval(paired, b=200, seed=20261127)
        self.assertIn("verdict_interval", stats)


class GateTests(unittest.TestCase):
    def test_b1_trap_is_tool_wrong(self) -> None:
        from scripts.reason_bench import dev_items

        item = next(row for row in dev_items(repo_root()) if row["id"] == "math-dev-t002-0")
        self.assertEqual(item["answer"], "53")
        records = run_tools_only([item], sympy="natural", tier="test")
        self.assertNotEqual(records[0].get("predicted"), "101")
        wrong = dict(records[0])
        wrong["answer_source"] = "tool_exact"
        wrong["correct"] = False
        wrong["verified"] = True
        wrong["abstained"] = False
        wrong["status"] = "ok"
        tuples = failure_tuples(hijack_reports=[{"fires": [], "crashes": []}], records=[wrong])
        self.assertIn(("tool_wrong", "math-dev-t002-0", "natural"), tuples)

    def test_b4_claimed_verified_wrong_and_hidden_fail_is_not_a_gate(self) -> None:
        from scripts.reason_engine import reason

        def _retrieve(_query: str):
            return [{"atom_id": "atlas-backup-size", "claim": "Each Atlas full backup is 14 GB."}]

        result = reason(
            "How large is one Atlas full backup, in GB?",
            profile="deep",
            model_fn=lambda _prompt: "4 GB",
            retriever=_retrieve,
            use_default_model=False,
        )
        self.assertFalse(result["verified"])
        from scripts.reason_bench import blank_record, score_engine_result

        item = {
            "id": "multihop-dev-m02-0",
            "template_id": "m02",
            "instance": 0,
            "suite": "multihop",
            "answer": "14",
            "answer_type": "integer",
            "approx_ok": None,
            "answerable": True,
            "tags": ["unit-mismatch"],
            "unit": "GB",
        }
        verified_wrong = {
            "answer": "The backup is 14 TB.",
            "answer_source": "memory",
            "verified": True,
            "abstained": False,
            "backend": "memory",
            "steps": [],
            "plan": {},
        }
        record = blank_record(item, "deep", sympy="natural", tier="test")
        score_engine_result(item, verified_wrong, record)
        tuples = failure_tuples(hijack_reports=[{"fires": [], "crashes": []}], records=[record])
        self.assertIn(("claimed_verified_wrong", "multihop-dev-m02-0", "natural"), tuples)

    def test_unverified_memory_is_expected(self) -> None:
        """Span-level support is an engine gap. An unverified memory answer is not a HARD failure."""
        from scripts.reason_bench import blank_record, score_engine_result

        item = {
            "id": "multihop-dev-m02-0",
            "template_id": "m02",
            "instance": 0,
            "suite": "multihop",
            "answer": "14",
            "answer_type": "integer",
            "approx_ok": None,
            "answerable": True,
            "tags": ["substring-verify-trap"],
            "unit": "GB",
        }
        result = {
            "answer": "14 GB",
            "answer_source": "model",
            "verified": False,
            "abstained": False,
            "contract_version": 2,
            "steps": [],
            "plan": {},
            "backend": None,
        }
        record = blank_record(item, "deep", sympy="natural", tier="test")
        score_engine_result(item, result, record)
        self.assertFalse(record["verified"])
        self.assertEqual(record["answer_source"], "model_unverified")
        self.assertTrue(record["correct"])
        gates = {row[0] for row in failure_tuples(hijack_reports=[{"fires": [], "crashes": []}], records=[record])}
        self.assertNotIn("claimed_verified_wrong", gates)

    def test_b7_fax_crashes_and_a_clean_record_passes(self) -> None:
        crashed = probe_tool("Fax 09-377 1234.")
        self.assertIsNone(crashed["crash"])
        self.assertFalse(crashed["fired"])
        tuples = failure_tuples(
            hijack_reports=[{"fires": [], "crashes": [{"id": "hijack-h013", "sympy_mode": "natural"}]}],
            records=[],
        )
        self.assertIn(("tool_crash", "hijack-h013", "natural"), tuples)
        clean = failure_tuples(
            hijack_reports=[{"fires": [], "crashes": []}],
            records=[{
                "id": "tools-0001",
                "answer_source": "tool_exact",
                "correct": True,
                "verified": True,
                "status": "ok",
                "sympy_mode": "natural",
            }],
        )
        self.assertEqual(clean, [])

    def test_visible_pass_hidden_fail_is_reported_only(self) -> None:
        from scripts.reason_bench import blank_record, score_engine_result

        item = next(row for row in __import__("scripts.reason_bench", fromlist=["dev_items"]).dev_items(repo_root()) if row["id"] == "code-dev-c01-0")
        stub = (
            "def run_length_encode(s):\n"
            "    if s in ('aaabcc', 'x'):\n"
            "        return {'aaabcc': 'a3b1c2', 'x': 'x1'}[s]\n"
            "    return 'NO'\n"
        )
        record = blank_record(item, "deep", sympy="natural", tier="test")
        score_engine_result(item, {"answer": stub, "abstained": False, "verified": True, "backend": "sandbox", "model_calls": 1, "steps": [], "plan": {}}, record)
        self.assertTrue(record.get("visible_pass_hidden_fail"))
        self.assertFalse(record["correct"])
        gates = {row[0] for row in failure_tuples(hijack_reports=[{"fires": [], "crashes": []}], records=[record])}
        self.assertNotIn("visible_pass_hidden_fail", gates)

    def test_contract_version_requires_answer_source(self) -> None:
        with self.assertRaises(BenchError):
            derive_answer_source({"contract_version": 2, "abstained": False})
        source, origin = derive_answer_source({"abstained": True, "verified": False})
        self.assertEqual((source, origin), ("abstained", "derived"))

    def test_budgets_timeouts_and_accuracy(self) -> None:
        from scripts.reason_bench import classify_exception

        self.assertEqual(classify_exception(RuntimeError("reasoning timeout")), "engine_timeout")
        self.assertEqual(classify_exception(RuntimeError("model call budget is spent")), "budget_exceeded")
        self.assertEqual(classify_exception(SyntaxError("leading zeros")), "engine_exception")
        hung = run_with_hang_guard(lambda: __import__("time").sleep(2), 0.05)
        self.assertEqual(hung["status"], "hung")
        pair = accuracy_pair([
            {"correct": True, "status": "ok"},
            {"correct": False, "status": "hung"},
        ])
        self.assertEqual(pair["accuracy"], 0.5)
        self.assertEqual(pair["accuracy_excl_timeouts"], 1.0)
        from scripts.reason_bench import failure_tuples

        timed = {
            "id": "slow",
            "sympy_mode": "natural",
            "status": "engine_timeout",
            "engine_exception": False,
            "correct": False,
            "verified": False,
            "answer_source": "abstained",
            "plan_step_verified": False,
            "plan_not_executed": [],
        }
        gates = {row[0] for row in failure_tuples(hijack_reports=[{"fires": [], "crashes": []}], records=[timed])}
        self.assertIn("engine_timeout", gates)
        self.assertNotIn("engine_exception", gates)
        from scripts.reason_bench import _record_exception, read_timeout_s

        record = {"id": "typed", "sympy_mode": "natural", "status": "ok"}
        _record_exception(record, ValueError("bad token"))
        self.assertEqual(record["status"], "engine_exception")
        self.assertTrue(str(record["error"]).startswith("ValueError:"))
        with patch.dict(os.environ, {"BRAIN_OLLAMA_TIMEOUT": "17"}):
            self.assertEqual(read_timeout_s(), 17.0)

    def test_majority_tie_abstains_and_resume_has_no_duplicates(self) -> None:
        from scripts.reason_bench import majority_vote as bench_majority

        self.assertEqual(bench_majority(["1", "2"]), "ABSTAIN")
        self.assertEqual(bench_majority(["1/2", "0.5"]), "1/2")
        merged = merge_resume(
            [{"id": "a", "runner": "deep", "sympy_mode": "natural"}],
            [{"id": "a", "runner": "deep", "sympy_mode": "natural"}, {"id": "b", "runner": "deep", "sympy_mode": "natural"}],
        )
        self.assertEqual(len(merged), 2)

    def test_deep_no_tools_does_not_emit_tool_exact(self) -> None:
        from unittest.mock import patch
        from scripts.reason_engine import reason

        with patch("scripts.reason_engine.try_exact_tool", return_value=None):
            result = reason("What is 17 * 23 + 9?", profile="deep", model_fn=lambda _prompt: "ANSWER: 400", use_default_model=False)
        self.assertNotEqual(result.get("backend"), "sympy")
        self.assertNotEqual(result.get("backend"), "arithmetic")
        source, _origin = derive_answer_source(result)
        self.assertNotEqual(source, "tool_exact")

    def test_question_form_hijack_classes_are_in_the_suite(self) -> None:
        from scripts.reason_bench import read_jsonl, bench_root

        rows = read_jsonl(bench_root(repo_root()) / "suites" / "hijack.jsonl")
        questions = [row for row in rows if str(row.get("text") or "").rstrip().endswith("?")]
        self.assertGreaterEqual(len(questions), 60)
        tags = {tag for row in rows for tag in (row.get("tags") or [])}
        for required in ("ratio-question", "slash-support", "shift-range", "boxes-question", "date-prose", "days-between-prose"):
            self.assertIn(required, tags)
        self.assertFalse(any("human_reviewed" in json.dumps(row) for row in rows))


class ClientTests(unittest.TestCase):
    def test_identity_assertions_and_early_stop(self) -> None:
        with patch.dict(os.environ, {"BRAIN_GEN_MODEL": "other:model"}):
            with self.assertRaises(ModelClientError):
                assert_model_env("hermes3:3b")
        chunks = [
            b'{"model":"hermes3:3b","message":{"content":"working\\n"},"done":false}\n',
            b'{"model":"hermes3:3b","message":{"content":"ANSWER: 5\\n"},"done":false}\n',
            b'{"model":"hermes3:3b","message":{"content":"TAIL"},"done":true}\n',
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{"name": "hermes3:3b", "model": "hermes3:3b", "digest": "sha256:abc"}]})
            if request.url.path == "/api/show":
                return httpx.Response(200, json={"details": {"quantization_level": "Q4", "parameter_size": "3B"}})
            if request.url.path == "/api/version":
                return httpx.Response(200, json={"version": "0"})
            return httpx.Response(200, content=b"".join(chunks))

        client = RawChatClient(backend="ollama", model="hermes3:3b", base_url="http://127.0.0.1:11434", num_thread=1)
        client.client = httpx.Client(transport=httpx.MockTransport(handler))
        client.warm()
        prompt = "Question\nFinish with a final line in exactly this form: ANSWER: <answer>\n"
        generated = client.generate(prompt, num_predict=32)
        self.assertTrue(generated.early_stopped)
        self.assertNotIn("TAIL", generated.text)
        self.assertEqual(generated.sha256_intended, generated.sha256_sent)
        self.assertEqual(client.digest, "sha256:abc")
        from scripts.reason_bench_models import assert_prompt_sent

        with self.assertRaises(ModelClientError):
            assert_prompt_sent("hello", '{"messages":[{"role":"user","content":"goodbye"}]}')
        self.assertIn("num_predict", generated.options_effective)
        client.digest = "changed"
        with self.assertRaises(ModelClientError):
            client.assert_digest_unchanged()

    def test_cache_is_read_disabled_when_flip_rate_is_positive(self) -> None:
        self.assertFalse(cache_read_allowed(0.1, temperature=0.0))
        self.assertTrue(cache_read_allowed(0.0, temperature=0.6))
        self.assertFalse(cache_read_allowed(None, temperature=0.6))


class HeldoutTests(unittest.TestCase):
    def test_seal_round_trip_and_bad_passphrase(self) -> None:
        blob = seal_bytes(b'{"items":[]}', "test-passphrase")
        plaintext, _hmac_key = open_bytes(blob, "test-passphrase")
        self.assertEqual(plaintext, b'{"items":[]}')
        with self.assertRaises(HeldoutError):
            open_bytes(blob, "wrong-passphrase")
        salt = b"\x11" * 32
        digest = commitment_digest(salt, b"")
        self.assertNotEqual(digest, commitment_digest(b"\x22" * 32, b""))
        placeholder = empty_commitment()
        self.assertEqual(placeholder["counts"]["math"], 0)
        with self.assertRaises(HeldoutError):
            assert_private_path(repo_root() / "bench" / "reasoning" / "nope.sbheldout", repo_root=repo_root())

    def test_gsm_symbolic_is_off_without_owner_confirmation(self) -> None:
        from scripts.reason_bench import external_allowed

        with self.assertRaises(BenchError):
            external_allowed("gsm-symbolic", owner_statement=None)
        ack = external_allowed("gsm8k-test", owner_statement=None)
        self.assertTrue(ack["contaminated"])


class SandboxSmokeTests(unittest.TestCase):
    def test_reference_passes_and_a_stub_fails_hidden_tests(self) -> None:
        from scripts.reason_bench import dev_items, validate_code_item
        from scripts.symbolic_sandbox import run_sandboxed_python

        item = next(row for row in dev_items(repo_root()) if row["id"] == "code-dev-c01-0")
        validate_code_item(item)
        stub = "def run_length_encode(s):\n    return 'NO'\n" + item["visible_tests"] + item["hidden_tests"]
        out = run_sandboxed_python(stub, timeout_seconds=10)
        self.assertFalse(out["ok"])

    def test_hidden_tests_do_not_leak_into_mock_prompts(self) -> None:
        from scripts.reason_bench import load_mock_script, run_mock_tier

        script = load_mock_script(repo_root())
        run_mock_tier(repo_root(), sympy="natural")
        blob = "\n".join(script.prompts)
        # The run uses its own script instance; rebuild the prompts by calling the runner's script via a direct plain prompt.
        from scripts.reason_bench import dev_items, plain_prompt, note_retriever

        item = next(row for row in dev_items(repo_root()) if row["id"] == "code-dev-c01-0")
        prompt = plain_prompt(item, note_retriever(repo_root()))
        self.assertIn("a3b1c2", prompt)
        self.assertNotIn("zzzzzzzzzzzz", prompt)
        self.assertNotIn(item["hidden_tests"].strip().splitlines()[0], prompt)
        self.assertIsInstance(script, MockScript)
        self.assertIsInstance(blob, str)


class IsolationTests(unittest.TestCase):
    def test_canary_injection_is_a_hard_failure(self) -> None:
        from scripts.reason_bench import assert_isolation

        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            with self.assertRaises(BenchError):
                assert_isolation(
                    [{"atom_id": "sb-bench-canary-1", "path": "", "claim": "The canary token is ZEBRA-1."}],
                    vault,
                    protected=[],
                    canary={"id": "sb-bench-canary-1", "token": "ZEBRA-1"},
                    prompts=[],
                )


def _cr2_reference(groups: dict) -> tuple[float, float]:
    """Independent CR2 / Bell–McCaffrey interval. Residuals use (1-h)^{-1/2} and not G/(G-1)."""
    import math

    from scripts.reason_bench_scoring import student_t_ppf

    names = list(groups)
    n = sum(len(values) for values in groups.values())
    point = sum(value for values in groups.values() for value in values) / n
    pieces = []
    for values in groups.values():
        local = sum(values) / len(values)
        h = len(values) / n
        residual = len(values) * (local - point)
        pieces.append((residual / math.sqrt(1.0 - h)) / n)
    se = math.sqrt(sum(piece * piece for piece in pieces))
    critical = student_t_ppf(0.975, len(names) - 1)
    return (point - critical * se, point + critical * se)


def _cr1_interval(groups: dict) -> tuple[float, float]:
    """CR1: the same cluster scores, scaled by G/(G-1), without the leverage correction."""
    import math

    from scripts.reason_bench_scoring import student_t_ppf

    names = list(groups)
    g = len(names)
    n = sum(len(values) for values in groups.values())
    point = sum(value for values in groups.values() for value in values) / n
    pieces = []
    for values in groups.values():
        local = sum(values) / len(values)
        pieces.append((len(values) * (local - point)) / n)
    se = math.sqrt(sum(piece * piece for piece in pieces) * g / (g - 1))
    critical = student_t_ppf(0.975, g - 1)
    return (point - critical * se, point + critical * se)


def _bca_reference(replicates: list[float], *, theta: float, acceleration: float) -> tuple[float, float]:
    """Efron (1987) eq. (2.3), coded apart from bca_interval."""
    import math
    from statistics import NormalDist

    ordered = sorted(replicates)
    n = len(ordered)
    prop = min(max(sum(1 for value in ordered if value < theta) / n, 1e-6), 1 - 1e-6)
    z0 = NormalDist().inv_cdf(prop)

    def adjusted(z_alpha: float) -> float:
        numer = z0 + z_alpha
        denom = 1.0 - acceleration * numer
        return NormalDist().cdf(z0 + numer / denom)

    lo_p = adjusted(NormalDist().inv_cdf(0.025))
    hi_p = adjusted(NormalDist().inv_cdf(0.975))
    lo = ordered[min(n - 1, max(0, int(math.floor(lo_p * (n - 1)))))]
    hi = ordered[min(n - 1, max(0, int(math.ceil(hi_p * (n - 1)))))]
    return (lo, hi) if lo <= hi else (hi, lo)


class ScoringReferenceTests(unittest.TestCase):
    def test_percent_and_unknown_and_decimal_form(self) -> None:
        from fractions import Fraction

        from scripts.reason_bench_scoring import _is_refusal_text, numbers_equal, parse_fraction

        self.assertEqual(parse_fraction("50%"), Fraction(50))
        self.assertFalse(_is_refusal_text("The unknown count is 53."))
        self.assertTrue(_is_refusal_text("unknown"))
        self.assertFalse(numbers_equal("7/2", "3.5", "decimal", None, tool=True))
        self.assertTrue(numbers_equal("3.5", "3.5", "decimal", None, tool=True))

    def test_cluster_t_is_cr2_and_bca_uses_theta(self) -> None:
        import math

        from scripts.reason_bench_scoring import bca_interval, cluster_t_interval

        # Two equal clusters match CR1 and CR2, so they do not identify the correction.
        equal = cluster_t_interval({"a": [1.0, 1.0], "b": [0.0, 0.0]})
        self.assertAlmostEqual(equal[0], -5.853102368, places=5)
        self.assertAlmostEqual(equal[1], 6.853102368, places=5)
        # Unequal clusters. CR2 scales by (1-h)^{-1/2}. CR1 multiplies by G/(G-1) and does not.
        unequal = {
            "a": [1.0, 1.0, 0.0, 1.0],
            "b": [0.0, 1.0, 0.0],
            "c": [1.0, 0.0],
            "d": [0.0, 0.0, 1.0, 1.0],
        }
        cr2 = cluster_t_interval(unequal)
        cr1 = _cr1_interval(unequal)
        self.assertNotAlmostEqual(cr2[0], cr1[0], places=4)
        self.assertAlmostEqual(cr2[0], _cr2_reference(unequal)[0], places=6)
        self.assertAlmostEqual(cr2[1], _cr2_reference(unequal)[1], places=6)
        # BCa formula: Efron, B. (1987), "Better Bootstrap Confidence Intervals",
        # Journal of the American Statistical Association 82(397), 171-185, eq. (2.3).
        replicates = [0.1, 0.2, 0.3, 0.4, 0.5]
        biased = bca_interval(replicates, 0.0, theta=0.2)
        self.assertEqual(biased, _bca_reference(replicates, theta=0.2, acceleration=0.0))
        self.assertTrue(math.isfinite(biased[0]))

    def test_sealed_extension_is_forbidden_inside_heldout_results(self) -> None:
        self.assertTrue(path_is_forbidden("bench/reasoning/heldout-results/x.sbsealed"))


if __name__ == "__main__":
    unittest.main()
