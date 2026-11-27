"""Stage 1 reasoning engine tests. Live model rows run only when BRAIN_REQUIRE_OLLAMA=1."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

def _sympy_present() -> bool:
    try:
        import sympy  # noqa: F401
    except ImportError:
        return False
    return True


def ordinary_non_math_sentences() -> list[str]:
    """Prose that mentions numbers and must not be treated as a calculation."""
    names = ["Avery", "Blair", "Casey", "Drew", "Ellis", "Finley", "Gray", "Harper", "Indigo", "Jules"]
    streets = ["Main", "Oak", "Pine", "Cedar", "Lake", "Hill", "Park", "River", "Elm", "King"]
    sentences = [
        "Fax 09-377 1234.",
        "Call 555-1234 tomorrow morning.",
        "The office line is (09) 377-1234.",
        "Dial 0800 123 456 for the desk.",
        "Extension 214 is the front desk.",
        "Meet on 11/27 at the library.",
        "The note mentions 2026-11-27 in passing.",
        "Born in the range 1990-1995.",
        "The project ran across 2020-2025.",
        "Pages 12-18 cover the appendix.",
        "Ages 3-5 use the small chairs.",
        "Score finished 3-2 after extra time.",
        "Room 09-377 is down the hall.",
        "SKU 09-377-1234 is back in stock.",
        "Ticket ABC-1234 is still open.",
        "Order #A100 ships Friday.",
        "Build id 8f3c-19a2 is the one we tagged.",
        "Release 1.2.3 shipped on Tuesday.",
        "We are on v2.0.1-beta in staging.",
        "The client speaks protocol 1.0.",
        "12 Main Street, apartment 4B.",
        "PO Box 18, River City.",
        "The cafe is at 400 Lake Road.",
        "Gate code for dock 7 is 2468.",
        "Arrive at 9:30 and leave by 11:15.",
        "The window is 09:30-10:15.",
        "Lunch is at 3 pm in the small room.",
        "Doors open from 2 pm to 4 pm.",
        "The fare was $12.50 each way.",
        "Listed at 19.99 before the sale.",
        "Budget line shows 40 dollars for tea.",
        "Invoice 1044 totals $1,250.00.",
        "Version string 10.0.0.1 is internal.",
        "IPv4 address 192.168.0.1 is the printer.",
        "Listen on port 8080 only.",
        "Ratio on the slide is 16:9.",
        "Chapter 4, section 2, paragraph 3.",
        "See figure 12 on page 40.",
        "The serial is SN-44021.",
        "Lot 77B expires next March.",
        "Case file CF-2024-018 is closed.",
        "Use form W-9 from 2018.",
        "The pin is 09 377 and then 1234.",
        "Flight UA 417 boards at gate B12.",
        "Seat 14A is by the window.",
        "Track 03 on the second disc.",
        "ISBN 978-1-4028-9462-6 is the manual.",
        "Zip code 94107 is the office.",
        "Postcode SW1A 1AA is the hall.",
        "Apartment 2, 14 GB Road. The name is just a street.",
        "The binder tab is 4.1 and the copy is 4.2.",
        "Refer to appendix C-3, not the whole chapter.",
        "Her employee number is E-4408.",
        "The locker combination is 12-24-36.",
        "Platform 9 is beside clock 3.",
        "Use coupon code SAVE10 at checkout.",
        "The map grid is B4, not a measurement.",
        "Row 18, seat 4, aisle left.",
        "Catalogue item No. 77 is the blue mug.",
        "The warranty card says model XR-200.",
    ]
    for index, name in enumerate(names):
        street = streets[index]
        sentences.append(f"{name} lives at {index + 11} {street} Street, apartment {index + 2}B.")
        sentences.append(f"Call {name} on 555-01{index:02d} after 4 pm.")
        sentences.append(f"{name} booked room {200 + index} for 11/{index + 1}.")
        sentences.append(f"Order {name[:3].upper()}-{1000 + index} costs ${index + 8}.50.")
        sentences.append(f"{name} arrives at {8 + (index % 3)}:{10 + index:02d} from {street} Station.")
        sentences.append(f"File {name.lower()}-{index + 3}.2.1 replaces the {2010 + index} draft.")
        sentences.append(f"The range for {name} is {1990 + index}-{1995 + index}, not a subtraction.")
        sentences.append(f"{street} warehouse bay {index + 1} holds pallet {400 + index}.")
        sentences.append(f"Badge {name[:2].upper()}{index:03d} opens door {index + 9}.")
        sentences.append(f"Meet {name} at {street} Cafe, table {index + 4}, around 12:{index:02d}.")
    for hour in range(1, 13):
        sentences.append(f"The reading group starts at {hour}:05 and ends at {hour}:50.")
        sentences.append(f"Shift {hour} covers desks {hour}01 through {hour}08.")
    for price in (5, 8, 12, 15, 19, 24, 30, 42, 60, 75):
        sentences.append(f"The paperback is ${price}.00 and the card costs extra.")
        sentences.append(f"A ticket priced at {price} dollars is still on hold.")
    return sentences


from scripts.reason_engine import (
    ABSTAIN,
    exact_arithmetic,
    load_config,
    reason,
    resolve_profile,
    run_deterministic_benchmark,
    run_model_comparison,
    score_answer,
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
            text, source, verified = exact_arithmetic("17*23+9")
        self.assertEqual((text, source, verified), ("400", "tool:sympy", True))

    def test_arithmetic_fallback_when_sympy_is_absent(self) -> None:
        with patch("scripts.reason_engine._sympy_value", side_effect=ImportError):
            text, source, verified = exact_arithmetic("17*23+9")
        self.assertEqual((text, source, verified), ("400", "tool:calculator", False))

    def test_real_sympy_matches_the_restricted_evaluator(self) -> None:
        try:
            import sympy
        except ImportError:
            self.skipTest("sympy is not installed")

        text, source, verified = exact_arithmetic("(2+3)*4")
        self.assertEqual(text, "20")
        self.assertEqual(source, "tool:sympy")
        self.assertTrue(verified)
        self.assertEqual(str(sympy.Integer(20)), "20")
        powered, powered_source, powered_verified = exact_arithmetic("2**10")
        self.assertEqual((powered, powered_source, powered_verified), ("1024", "tool:sympy", True))
        fraction, fraction_source, fraction_verified = exact_arithmetic("2/3")
        self.assertEqual((fraction, fraction_source, fraction_verified), ("2/3", "tool:sympy", True))
        self.assertEqual(exact_arithmetic("1/2+1/3")[0], "5/6")
        self.assertEqual(exact_arithmetic("4/2")[0], "2")

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

    def test_deep_stream_calls_the_model_for_a_non_tool_question(self) -> None:
        from scripts.streaming_chat import chat_events

        def _model(prompt: str, _name: str):
            self.assertIn("kiln", prompt.lower())
            yield "The kiln is hot."

        events = list(chat_events("How hot is the kiln?", mode="stream", paths=_empty_paths(), stream_fn=_model))
        text = "".join(str(event.get("text") or "") for event in events if event.get("type") == "token")
        done = next(event for event in events if event.get("type") == "done")
        self.assertIn("kiln", text.lower())
        self.assertGreaterEqual(done["reasoning"]["model_calls"], 1)
        self.assertFalse(done["verified"])
        self.assertEqual(done["answer_source"], "model")

    def test_tool_answer_is_saved_and_followups_are_resolved_first(self) -> None:
        from scripts.chat_threads import append_chat_turn, create_chat_thread, get_chat_thread
        from scripts.streaming_chat import chat_events

        paths = _empty_paths()
        created = create_chat_thread(title="math", brain_dir=paths.brain_dir)
        thread_id = created["thread_id"]
        append_chat_turn(thread_id, "user", "How many pens were sold?", brain_dir=paths.brain_dir)
        append_chat_turn(thread_id, "assistant", "101 pens", brain_dir=paths.brain_dir)
        seen: list[str] = []

        def _spy(question: str, **_kwargs):
            seen.append(question)
            return {
                "answer": "405",
                "answer_source": "model",
                "verified": False,
                "abstained": False,
                "problem_type": "other",
                "steps": [],
                "tool_calls": [],
                "citations": [],
                "confidence": 0.0,
                "model_calls": 0,
                "profile": "deep",
                "elapsed_ms": 1,
                "latency_note": "",
                "errors": [],
            }

        with patch("scripts.reason_engine.reason", _spy):
            events = list(
                chat_events(
                    "what about it?",
                    mode="stream",
                    paths=paths,
                    thread_id=thread_id,
                    stream_fn=lambda *_args: iter(["unused"]),
                )
            )
        self.assertTrue(seen)
        self.assertNotEqual(seen[0], "what about it?")
        self.assertIn("pens", seen[0].casefold())
        done = next(event for event in events if event.get("type") == "done")
        turns = get_chat_thread(done["thread_id"], brain_dir=paths.brain_dir)["turns"]
        self.assertTrue(any(turn.get("role") == "assistant" and "405" in str(turn.get("content")) for turn in turns))


class ReasonToolContractTests(unittest.TestCase):
    def test_server_instructions_and_tool_schema_default_to_deep(self) -> None:
        from scripts.reason_engine import server_instructions
        import scripts.mcp_server as mcp_server

        text = server_instructions()
        self.assertIn("reason", text)
        self.assertIn("deep", text.lower())
        self.assertIn("do not route your own reasoning", text.lower())
        self.assertNotIn("call reason before you answer", text.lower())
        self.assertIn("reason", mcp_server.TOOL_NAMES)
        tool = next(item for item in mcp_server.ALL_TOOLS if item.name == "reason")
        schema = tool.input_schema
        self.assertIn("tests", schema["properties"])
        self.assertEqual(schema["required"], ["question"])
        self.assertIn("profile", schema["properties"])
        self.assertIn("deep", (mcp_server.app.instructions or "").lower())


class ReviewRegressionTests(unittest.TestCase):
    def test_ordinary_text_is_not_arithmetic(self) -> None:
        for question in ("555-1234", "11/27", "2020-2025"):
            routed = try_exact_tool(question)
            self.assertTrue(routed is None or routed.get("refused"), question)
            result = reason(question, model_fn=lambda _prompt: "left as text")
            self.assertNotEqual(result["answer"], "-679", question)
            self.assertNotEqual(result["answer"], "0.407407407407", question)
            self.assertNotEqual(result["answer"], "-5", question)
            self.assertFalse(result["verified"], question)
            self.assertIn("answer_source", result)

    def test_parentheses_and_multiply_signs(self) -> None:
        self.assertEqual(reason("(2+3)*4")["answer"], "20")
        self.assertEqual(reason("2 × 3")["answer"], "6")
        self.assertEqual(reason("2 x 3")["answer"], "6")
        self.assertEqual(reason("20 % 6")["answer"], "2")
        self.assertEqual(reason("2^10")["answer"], "1024")
        powered = reason("2^10")
        if _sympy_present():
            self.assertTrue(powered["verified"])
            self.assertEqual(powered["answer_source"], "tool:sympy")
        else:
            self.assertFalse(powered["verified"])
            self.assertEqual(powered["answer_source"], "tool:calculator")

    def test_loose_word_template_does_not_invent_a_total(self) -> None:
        eats = try_exact_tool("3 boxes of 4, eats 2")
        self.assertTrue(eats is None or eats.get("refused") or eats.get("answer") != "12")
        times = try_exact_tool("2 times 3 plus 4 times 5")
        self.assertTrue(times is None or times.get("refused") or times.get("answer") != "6")
        half = reason("12 boxes of 8 pens, sells half, then 5 more", model_fn=lambda _prompt: "53")
        self.assertNotEqual(half["answer"], "101")
        self.assertFalse(half["verified"] and half["answer"] == "101")

    def test_division_by_zero_uses_the_model_reply(self) -> None:
        result = reason("10/0", model_fn=lambda _prompt: "Division by zero is undefined.")
        self.assertIn("undefined", result["answer"].lower())
        self.assertFalse(result["verified"])
        self.assertNotEqual(result["answer"], ABSTAIN)
        self.assertEqual(result["answer_source"], "model")

    def test_memory_numbers_are_whole_tokens_and_refusals_are_not_verified(self) -> None:
        sources = [{"atom_id": "year.md", "claim": "The note is dated 2026.", "evidence": "dated 2026"}]
        self.assertEqual(unsupported_numbers("Use code 20.", sources), ["20"])
        self.assertEqual(unsupported_numbers("The year is 2026.", sources), [])
        refused = reason(
            "What year is the note?",
            model_fn=lambda _prompt: "I don't know.",
            retriever=lambda _question: sources,
        )
        self.assertFalse(refused["verified"])
        self.assertEqual(refused["answer_source"], "abstain")
        no_memory = reason(
            "Where is the spare key?",
            model_fn=lambda _prompt: "The spare key is in drawer 4.",
            retriever=lambda _question: [],
        )
        self.assertFalse(no_memory["abstained"])
        self.assertFalse(no_memory["verified"])
        self.assertIn("4", no_memory["answer"])
        self.assertEqual(no_memory["answer_source"], "model")

    def test_a_parsed_plan_step_is_executed(self) -> None:
        def _plan(prompt: str) -> str:
            if "JSON" in prompt or "json" in prompt:
                return '{"problem_type":"math","steps":[{"tool":"calculator","input":"(2+3)*4"}]}'
            return "5"
        result = reason("Use the calculator on (2+3)*4", model_fn=_plan)
        self.assertEqual(result["answer"], "20")
        if _sympy_present():
            self.assertTrue(result["verified"])
            self.assertEqual(result["answer_source"], "model+tool_verified")
        else:
            self.assertFalse(result["verified"])
            self.assertEqual(result["answer_source"], "tool:calculator")

    def test_timeout_bounds_an_in_flight_call_and_counts_it(self) -> None:
        import time

        def _slow(_prompt: str) -> str:
            time.sleep(2)
            return "too late"

        with patch("scripts.reason_engine.budget_for", return_value={
            "name": "fast", "max_steps": 2, "max_model_calls": 1, "max_retries": 0, "timeout_s": 0.05,
        }):
            started = time.perf_counter()
            result = reason("Tell a story about a kiln.", profile="fast", model_fn=_slow)
            elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.0)
        self.assertGreaterEqual(result["model_calls"], 1)
        self.assertTrue(any("timeout" in error for error in result["errors"]))
        self.assertFalse(result["verified"])

    def test_model_calls_match_attempts(self) -> None:
        calls: list[str] = []

        def _model(prompt: str) -> str:
            calls.append(prompt)
            return "not json" if "JSON" in prompt or "json" in prompt else "The kiln is hot."

        result = reason("How hot is the kiln?", model_fn=_model)
        self.assertEqual(result["model_calls"], len(calls))
        self.assertGreaterEqual(len(calls), 1)

    def test_default_model_does_not_fall_back_to_another_model(self) -> None:
        from scripts.reason_engine import _default_model

        with patch("scripts.ollama_utils.chat_with_fallback", return_value={"message": {"content": "ok"}}) as chat:
            self.assertEqual(_default_model("hello"), "ok")
        self.assertFalse(chat.call_args.kwargs["allow_model_fallback"])

    def test_retrieve_uses_the_vault_index_not_the_real_default(self) -> None:
        from scripts.config import LEXICAL_INDEX_PATH
        from scripts.reason_engine import retrieve_atoms

        real = os.path.abspath(LEXICAL_INDEX_PATH)
        seen: list[str] = []

        def _spy(_query, _options, **kwargs):
            index = os.path.abspath(str(kwargs.get("index_path") or ""))
            seen.append(index)
            if not kwargs.get("index_path") or index == real:
                raise AssertionError(f"real index path was touched: {index}")
            return [], []

        with tempfile.TemporaryDirectory() as tmp:
            atoms = Path(tmp) / "2-atoms"
            atoms.mkdir()
            with patch("scripts.recall.search_memory", _spy):
                retrieve_atoms("kiln temperature", atoms_dir=str(atoms))
                reason("Where is the kiln?", atoms_dir=str(atoms), retriever=None, model_fn=lambda _prompt: "no")
        self.assertTrue(seen)
        self.assertTrue(all(path != real and path.startswith(os.path.abspath(tmp)) for path in seen))

    def test_benchmark_score_is_exact(self) -> None:
        self.assertFalse(score_answer("40", "400"))
        self.assertTrue(score_answer("400", "400"))
        self.assertTrue(score_answer("400", "The result is 400."))
        self.assertFalse(score_answer("40", "The result is 400."))

    def test_engine_result_exposes_answer_source_and_verified(self) -> None:
        from scripts.reason_engine import tool_chat_events

        result = reason("(2+3)*4")
        self.assertIn(result["answer_source"], {"tool:sympy", "tool:calculator"})
        self.assertIn("verified", result)
        self.assertEqual(result["verified"], result["answer_source"] == "tool:sympy")
        done = next(event for event in tool_chat_events(result) if event["type"] == "done")
        self.assertEqual(done["answer_source"], result["answer_source"])
        self.assertEqual(done["verified"], result["verified"])
        self.assertEqual(done["reasoning"]["answer_source"], result["answer_source"])
        self.assertEqual(done["reasoning"]["verified"], result["verified"])

    def test_fax_number_never_raises_and_stays_off_the_tool_path(self) -> None:
        seen: list[str] = []

        def _model(prompt: str) -> str:
            seen.append(prompt)
            return "That is a fax number."

        for question in ("Fax 09-377 1234.", "What is 09-377?", "calculate 09-377", "What is 2***3?"):
            seen.clear()
            result = reason(question, model_fn=_model)
            self.assertIsInstance(result, dict, question)
            self.assertEqual(result["answer"], "That is a fax number.", question)
            self.assertFalse(str(result["answer_source"]).startswith("tool:"), question)
            self.assertFalse(result["verified"], question)
            self.assertGreaterEqual(result["model_calls"], 1, question)
            self.assertIsNone(try_exact_tool(question), question)
        blank = reason("   ")
        self.assertEqual(blank["answer_source"], "abstain")
        self.assertFalse(blank["verified"])

    def test_ordinary_sentences_do_not_fire_a_tool(self) -> None:
        sentences = ordinary_non_math_sentences()
        self.assertGreaterEqual(len(sentences), 200)
        self.assertGreaterEqual(len(set(sentences)), 200)
        for question in sentences:
            routed = try_exact_tool(question)
            self.assertIsNone(routed, question)
            result = reason(question, use_default_model=False)
            self.assertIsInstance(result, dict, question)
            self.assertFalse(str(result.get("answer_source") or "").startswith("tool:"), question)
            self.assertFalse(result["verified"], question)

    def test_exact_fractions_stay_exact(self) -> None:
        third = reason("What is 2/3?")
        self.assertEqual(third["answer"], "2/3")
        self.assertNotIn(".", third["answer"])
        self.assertEqual(reason("What is 1/2 + 1/3?")["answer"], "5/6")
        self.assertEqual(reason("What is 5/2?")["answer"], "5/2")
        self.assertEqual(reason("What is 4/2?")["answer"], "2")
        self.assertEqual(reason("What is 17 * 23 + 9?")["answer"], "400")

    def test_supported_numbers_are_whole_tokens(self) -> None:
        sources = [{"atom_id": "disk.md", "claim": "14 GB", "evidence": "14 GB", "title": "disk"}]
        self.assertEqual(unsupported_numbers("4", sources), ["4"])
        self.assertEqual(unsupported_numbers("Use 4 GB.", sources), ["4"])
        self.assertEqual(unsupported_numbers("The disk is 14 GB.", sources), [])
        self.assertEqual(unsupported_numbers("140", sources), ["140"])
        answered = reason(
            "How large is the disk?",
            model_fn=lambda _prompt: "4",
            retriever=lambda _question: sources,
        )
        self.assertFalse(answered["verified"])
        self.assertNotEqual(answered["answer_source"], "memory")

    def test_budget_timeout_returns_abstain_instead_of_raising(self) -> None:
        import time

        real = time.perf_counter
        start = {"value": None}

        def _clock() -> float:
            now = real()
            if start["value"] is None:
                start["value"] = now
                return now
            return start["value"] + 181.0

        def _explode(_prompt: str) -> str:
            raise RuntimeError("reasoning timeout")

        with patch("scripts.reason_engine.time.perf_counter", _clock):
            result = reason("Where is the kiln?", model_fn=_explode)
        self.assertIsInstance(result, dict)
        self.assertTrue(result["abstained"])
        self.assertEqual(result["answer"], ABSTAIN)
        self.assertEqual(result["answer_source"], "abstain")
        self.assertFalse(result["verified"])
        self.assertTrue(any("timeout" in error for error in result["errors"]))

    def test_boxes_template_does_not_depend_on_sympy(self) -> None:
        import builtins
        import sys

        real_import = builtins.__import__

        def _blocked(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "sympy" or name.startswith("sympy."):
                raise ImportError("sympy blocked for this test")
            return real_import(name, globals, locals, fromlist, level)

        hidden = {
            key: sys.modules.pop(key)
            for key in list(sys.modules)
            if key == "sympy" or key.startswith("sympy.")
        }
        try:
            with patch("builtins.__import__", _blocked):
                eats = try_exact_tool("3 boxes of 4, eats 2")
                self.assertTrue(eats is None or eats.get("answer") != "12")
                result = reason("3 boxes of 4, eats 2", use_default_model=False)
                self.assertNotEqual(result["answer"], "12")
                self.assertFalse(result["verified"])
                sold = reason(
                    "A shop sold 12 boxes of 8 pens and then 5 more pens. How many pens were sold?",
                    use_default_model=False,
                )
                self.assertEqual(sold["answer"], "101")
                third, source, verified = exact_arithmetic("2/3")
                self.assertEqual(third, "2/3")
                self.assertEqual(source, "tool:calculator")
                self.assertFalse(verified)
        finally:
            sys.modules.update(hidden)


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
