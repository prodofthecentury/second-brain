"""Stage 1 reasoning engine tests. Live model rows run only when BRAIN_REQUIRE_OLLAMA=1."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.reason_engine import (
    ABSTAIN,
    exact_arithmetic,
    load_config,
    reason,
    resolve_profile,
    run_deterministic_benchmark,
    run_model_comparison,
    try_exact_tool,
    unsupported_numbers,
)


class ReasonEngineTests(unittest.TestCase):
    def test_config_default_is_deep(self) -> None:
        config = load_config()
        self.assertEqual(config["default_profile"], "deep")
        self.assertIn("several times", config["latency_note"].lower())
        self.assertEqual(resolve_profile(None), "deep")
        self.assertEqual(resolve_profile("fast"), "fast")
        with patch.dict(os.environ, {"BRAIN_REASON_PROFILE": "fast"}):
            self.assertEqual(resolve_profile(None), "fast")
            self.assertEqual(resolve_profile("deep"), "deep")

    def test_arithmetic_date_and_units_do_not_call_a_model(self) -> None:
        calls: list[str] = []

        def _model(prompt: str) -> str:
            calls.append(prompt)
            return "999"

        cases = {
            "What is 17 * 23 + 9?": "400",
            "A shop sold 12 boxes of 8 pens and then 5 more pens. How many pens were sold?": "101",
            "What date is 14 days after 2026-11-27?": "2026-12-11",
            "How many days between 2026-11-01 and 2026-11-27?": "26",
            "Convert 3 km to m.": "3000",
            "Convert 100 C to F.": "212",
        }
        for question, expected in cases.items():
            result = reason(question, model_fn=_model)
            self.assertEqual(result["profile"], "deep", question)
            self.assertFalse(result["abstained"], question)
            self.assertIn(expected, result["answer"], question)
            self.assertEqual(result["model_calls"], 0, question)
            self.assertTrue(result["engine"])
        self.assertEqual(calls, [])
        self.assertIsNone(try_exact_tool("What does the owner prefer for coffee?"))

    def test_an_iso_date_is_not_subtracted(self) -> None:
        self.assertIsNone(try_exact_tool("The note mentions 2026-11-27 in passing."))

    def test_sympy_is_used_when_it_imports(self) -> None:
        with patch("scripts.reason_engine._sympy_value", return_value=400):
            text, backend = exact_arithmetic("17*23+9")
        self.assertEqual((text, backend), ("400", "sympy"))

    def test_arithmetic_fallback_when_sympy_is_absent(self) -> None:
        with patch("scripts.reason_engine._sympy_value", side_effect=ImportError):
            text, backend = exact_arithmetic("17*23+9")
        self.assertEqual((text, backend), ("400", "arithmetic"))

    def test_fast_budget_is_smaller_and_a_bad_function_abstains(self) -> None:
        result = reason(
            "Write add(a, b).",
            profile="fast",
            tests="assert add(2, 3) == 5",
            model_fn=lambda _prompt: "def add(a, b):\n    return a - b\n",
        )
        self.assertTrue(result["abstained"])
        self.assertEqual(result["answer"], ABSTAIN)
        self.assertEqual(result["budgets"]["max_retries"], 0)
        self.assertLess(result["budgets"]["max_model_calls"], load_config()["profiles"]["deep"]["max_model_calls"])

    def test_deep_retries_code_until_the_tests_pass(self) -> None:
        seen: list[str] = []

        def _model(prompt: str) -> str:
            seen.append(prompt)
            if "failed" in prompt.lower():
                return "def add(a, b):\n    return a + b\n"
            return "def add(a, b):\n    return a - b\n"

        result = reason(
            "Write add(a, b).",
            profile="deep",
            tests="assert add(2, 3) == 5",
            model_fn=_model,
        )
        self.assertFalse(result["abstained"])
        self.assertGreaterEqual(result["model_calls"], 2)
        self.assertGreaterEqual(len(seen), 2)
        self.assertIn("return a + b", result["answer"])

    def test_a_number_that_is_not_in_memory_is_rejected(self) -> None:
        sources = [{"atom_id": "port.md", "claim": "The dashboard listens on port 8501.", "evidence": "port 8501"}]

        def _retriever(_question: str) -> list[dict[str, str]]:
            return sources

        bad = reason(
            "What port does the dashboard listen on?",
            model_fn=lambda _prompt: "It listens on port 9999.",
            retriever=_retriever,
        )
        self.assertTrue(bad["abstained"])
        self.assertEqual(bad["answer"], ABSTAIN)
        good = reason(
            "What port does the dashboard listen on?",
            model_fn=lambda _prompt: "The dashboard listens on port 8501.",
            retriever=_retriever,
        )
        self.assertFalse(good["abstained"])
        self.assertIn("8501", good["answer"])
        self.assertEqual(unsupported_numbers("port 9999", sources), ["9999"])

    def test_sandbox_rejects_a_network_import(self) -> None:
        from scripts.reason_engine import run_python_checks

        ran = run_python_checks("import socket\n", "assert True")
        self.assertFalse(ran["ok"])


class ChatDefaultTests(unittest.TestCase):
    def test_default_chat_uses_the_engine_for_arithmetic(self) -> None:
        from scripts.streaming_chat import chat_events

        def _explode(_prompt: str, _model: str):
            raise AssertionError("the model must not be called")
            yield ""

        events = list(chat_events("What is 17 * 23 + 9?", mode="stream", stream_fn=_explode))
        text = "".join(str(event.get("text") or "") for event in events if event.get("type") == "token")
        done = next(event for event in events if event.get("type") == "done")
        self.assertIn("400", text)
        self.assertEqual(events[0]["type"], "context")
        self.assertTrue(done["reasoning"]["engine"])
        self.assertEqual(done["reasoning"]["profile"], "deep")
        self.assertEqual(done["reasoning"]["model_calls"], 0)

    def test_fast_chat_opts_out_of_the_engine(self) -> None:
        from scripts.streaming_chat import chat_events

        events = list(
            chat_events(
                "What is 17 * 23 + 9?",
                mode="stream",
                profile="fast",
                stream_fn=lambda _prompt, _model: iter(["plain reply"]),
                paths=_empty_paths(),
            )
        )
        blob = " ".join(str(event.get("text") or "") for event in events)
        self.assertNotIn("400", blob)
        done = next(event for event in events if event.get("type") == "done")
        self.assertNotIn("reasoning", done)
        self.assertTrue(any(event.get("type") in {"abstain", "token", "error"} for event in events))

    def test_a_normal_question_is_marked_as_routed_through_the_engine(self) -> None:
        from scripts.streaming_chat import chat_events

        events = list(
            chat_events(
                "Where is the rollback plan?",
                mode="instant",
                paths=_empty_paths(),
                stream_fn=lambda *_args: iter([]),
            )
        )
        done = next(event for event in events if event.get("type") == "done")
        self.assertEqual(events[0]["type"], "context")
        self.assertTrue(done["reasoning"]["engine"])
        self.assertEqual(done["reasoning"]["profile"], "deep")
        self.assertEqual(done["reasoning"]["steps"][0]["tool"], "grounded_chat")


class ReasonToolContractTests(unittest.TestCase):
    def test_server_instructions_and_tool_schema_default_to_deep(self) -> None:
        from scripts.reason_engine import server_instructions
        import scripts.mcp_server as mcp_server

        text = server_instructions()
        self.assertIn("reason", text)
        self.assertIn("deep", text.lower())
        self.assertIn("reason", mcp_server.TOOL_NAMES)
        tool = next(item for item in mcp_server.ALL_TOOLS if item.name == "reason")
        schema = tool.input_schema
        self.assertEqual(schema["required"], ["question"])
        self.assertIn("profile", schema["properties"])
        self.assertIn("deep", (mcp_server.app.instructions or "").lower())


class ReasonBenchmarkTests(unittest.TestCase):
    def test_deterministic_benchmark_is_exact(self) -> None:
        rows = run_deterministic_benchmark()
        self.assertGreaterEqual(len(rows), 6)
        for row in rows:
            self.assertTrue(row["correct"], row)
            self.assertEqual(row["model_calls"], 0, row)
            self.assertLess(row["elapsed_ms"], 2000, row)

    def test_live_models_are_recorded_when_ollama_is_required(self) -> None:
        if os.environ.get("BRAIN_REQUIRE_OLLAMA") != "1":
            self.skipTest("live comparison runs in the advanced workflow")
        from scripts.ollama_utils import chat_with_fallback

        questions = [
            {"id": "arith-17-23-9", "question": "What is 17 * 23 + 9?", "answer": "400"},
            {
                "id": "gsm-pens",
                "question": "A shop sold 12 boxes of 8 pens and then 5 more pens. How many pens were sold?",
                "answer": "101",
            },
        ]
        models = []
        for name in ("llama3.2:3b", "hermes3:3b"):
            def _call(prompt: str, model_name: str = name) -> str:
                response = chat_with_fallback(
                    [{"role": "user", "content": prompt}],
                    model=model_name,
                    allow_model_fallback=False,
                    options={"temperature": 0.1, "num_predict": 128},
                )
                if isinstance(response, dict):
                    return str((response.get("message") or {}).get("content") or "")
                return str(response or "")

            rows = run_model_comparison(name, _call, questions)
            models.extend(rows)
            for row in rows:
                print(
                    f"BENCH {row['id']} {row['runner']} {row['model']} correct={int(bool(row['correct']))} "
                    f"elapsed_ms={row['elapsed_ms']} answer={str(row.get('answer') or '')[:160]!r} "
                    f"error={str(row.get('error') or '')[:200]!r}"
                )
        self.assertEqual(len(models), 8)
        failed_calls = [row for row in models if row.get("error")]
        self.assertEqual(failed_calls, [], "a required live model call did not reach Ollama")
        engine_arith = [row for row in models if row["runner"] == "engine" and row["id"] == "arith-17-23-9"]
        self.assertTrue(all(row["correct"] for row in engine_arith))


def _empty_paths():
    from scripts.streaming_chat import ChatPaths

    root = Path(tempfile.mkdtemp(prefix="sb-reason-"))
    (root / "2-atoms").mkdir()
    (root / ".sidecar").mkdir()
    return ChatPaths.for_brain(root)


if __name__ == "__main__":
    unittest.main()
