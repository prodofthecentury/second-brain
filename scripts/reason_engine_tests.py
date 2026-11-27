"""Stage 1 reasoning engine tests. Live model rows run only when BRAIN_REQUIRE_OLLAMA=1."""

from __future__ import annotations

import builtins
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch


def _exact_decimal(value: Fraction) -> str | None:
    """The full decimal of a fraction, computed here and not by the engine."""
    denom = abs(value.denominator)
    while denom % 2 == 0:
        denom //= 2
    while denom % 5 == 0:
        denom //= 5
    if denom != 1:
        return None
    text = format(Decimal(value.numerator) / Decimal(value.denominator), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _assert_verified_decimal(test: unittest.TestCase, printed: str, exact: Fraction) -> None:
    """A verified decimal must be the exact expansion, not a rounded stand-in."""
    test.assertEqual(Fraction(printed), exact)
    test.assertEqual(printed, _exact_decimal(exact))


_INDEPENDENT_EXACT = {
    "What is 2/3 + 0.5?": Fraction(2, 3) + Fraction(1, 2),
    "What is 1/3 + 0.0?": Fraction(1, 3) + Fraction(0),
    "What is 2.5 * 2?": Fraction(5, 2) * 2,
    "What is 0.1 + 0.2?": Fraction(1, 10) + Fraction(2, 10),
    "What is 40 - 15?": Fraction(40) - 15,
    "10 - 4": Fraction(10) - 4,
    "What is 10 - 2 - 3?": Fraction(10) - 2 - 3,
    "Calculate 2026 - 1990": Fraction(2026) - 1990,
    "What is 40 -15?": Fraction(40) - 15,
    "40 − 15": Fraction(40) - 15,
    ".5 + .25": Fraction(1, 2) + Fraction(1, 4),
    "What is 17 * 23 + 9?": Fraction(17) * 23 + 9,
    "What is 1,500 + 20?": Fraction(1500) + 20,
    "What is -5 + 3?": Fraction(-5) + 3,
    "What is 100/4?": Fraction(100, 4),
    "What is 50/50?": Fraction(50, 50),
    "What is 4/20?": Fraction(4, 20),
    "100/4": Fraction(100, 4),
    "4/20": Fraction(4, 20),
    "What is 1/2 + 1/4?": Fraction(1, 2) + Fraction(1, 4),
    "What is 11 / 2 as a decimal?": Fraction(11, 2),
    "What is (10) -4?": Fraction(10) - 4,
}

@contextmanager
def _sympy_blocked():
    """Run a block as if SymPy were not installed."""
    real_import = builtins.__import__

    def _blocked(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "sympy" or str(name).startswith("sympy."):
            raise ImportError("sympy blocked for this test")
        return real_import(name, globals, locals, fromlist, level)

    hidden = {
        key: sys.modules.pop(key)
        for key in list(sys.modules)
        if key == "sympy" or key.startswith("sympy.")
    }
    try:
        with patch("builtins.__import__", _blocked):
            yield
    finally:
        sys.modules.update(hidden)


def _assert_sells_half_declines(test: unittest.TestCase) -> None:
    """Pens prose is not a calculator question. The model may answer; that answer is unverified."""
    question = "12 boxes of 8 pens, sells half, then 5 more"
    tool = try_exact_tool(question)
    test.assertTrue(tool is None or tool.get("refused"))
    declined = reason(question, use_default_model=False)
    test.assertNotEqual(declined["answer"], "101")
    test.assertFalse(declined["verified"])
    test.assertFalse(str(declined["answer_source"]).startswith("tool:"))
    handed = reason(question, model_fn=lambda _prompt: "53")
    test.assertEqual(handed["answer"], "53")
    test.assertFalse(handed["verified"])
    test.assertEqual(handed["answer_source"], "model")
    test.assertEqual(handed["contract_version"], 2)
    sold = "A shop sold 12 boxes of 8 pens and then 5 more pens. How many pens were sold?"
    test.assertTrue(try_exact_tool(sold) is None or try_exact_tool(sold).get("refused"))
    sold_result = reason(sold, use_default_model=False)
    test.assertNotEqual(sold_result["answer"], "101")
    test.assertFalse(sold_result["verified"])


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
        "What is on pages 10-20 of the manual?",
        "What is the 2024-2025 budget for the garden?",
        "What's the score, 3-1 to us?",
        "What is my 9-5 shift pattern next week?",
        "What is the 80/20 rule?",
        "What is 24/7 support on my plan?",
        "What is a 3-2-1 backup?",
        "What is the 1920x1080 setting called?",
        "What's a 2x4 used for in framing?",
        "What is the 50/50 split with Sam?",
        "How much is a 2-3 day courier?",
        "What is in boxes 3-5 in the garage?",
        "Calculate my hours for the 9-5 days this week.",
        "Solve the 3-4 puzzle from the book club.",
        "What is the ratio for 2/3 of the class going on camp?",
        "Evaluate the 2025-2026 plan please.",
        "The parcel arrived 3 days after 2026-11-20.",
        "I was born 5 days after 1990-03-01, apparently.",
        "There are 26 days between 2026-11-01 and 2026-11-27 on the old calendar.",
        "The 3 boxes of 10 apples had 2 more rotten than last week.",
        "We stacked 6 packs of 4 drinks and 2 fewer chairs.",
        "Kids: 3 groups of 5 and 2 more parents.",
        "12 boxes of 8 pens, sells half, then 5 more",
        "9 groups of 7 pens, lost 3 more than usual",
        "3 boxes of 4 pens, 2 fewer than needed",
        "1+1 deal on pizzas this week.",
        "Buy 2+1 free at the shop.",
        "Remember 5*3 stars on the review form.",
        "10x faster than before, apparently.",
        "Rated 4.5/5 on the app store.",
        "Lens 18-55 mm is in the bag.",
        "Rooms 101-110 are on level one.",
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
            "What is 1,500 + 20?": "1520",
            "What is -5 + 3?": "-2",
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
        _assert_sells_half_declines(self)

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
        self.assertEqual(result["answer"], "5")
        self.assertFalse(result["verified"])
        self.assertEqual(result["answer_source"], "model")
        self.assertTrue(any(step.get("status") == "not_adopted" for step in result["steps"]))

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
        self.assertFalse(score_answer("4", "Not 4; it is 5."))
        self.assertTrue(score_answer("5", "Not 4; it is 5."))

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
        echoed = reason("What is 2/3?", use_default_model=False)
        self.assertFalse(str(echoed["answer_source"]).startswith("tool:"))
        self.assertFalse(echoed["verified"])
        self.assertIsNone(try_exact_tool("What is 5/2?"))
        combined = reason("What is 1/2 + 1/3?")
        self.assertEqual(combined["answer"], "5/6")
        self.assertNotIn(".", combined["answer"])
        self.assertEqual(reason("What is 4/2?")["answer"], "2")
        self.assertEqual(reason("What is 100/4?")["answer"], "25")
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
        with _sympy_blocked():
            eats = try_exact_tool("3 boxes of 4, eats 2")
            self.assertTrue(eats is None or eats.get("answer") != "12")
            result = reason("3 boxes of 4, eats 2", use_default_model=False)
            self.assertNotEqual(result["answer"], "12")
            self.assertFalse(result["verified"])
            _assert_sells_half_declines(self)
            third, source, verified = exact_arithmetic("2/3")
            self.assertEqual(third, "2/3")
            self.assertEqual(source, "tool:calculator")
            self.assertFalse(verified)


class MechanismProbeTests(unittest.TestCase):
    """General route, verification, and exception rules. Both SymPy modes."""

    def _both(self, check) -> None:
        check()
        with _sympy_blocked():
            check()

    def test_prose_and_qualified_patterns_do_not_fire(self) -> None:
        questions = [
            "What is the 80/20 rule?",
            "What's a 2x4 used for in framing?",
            "What is on pages 10-20 of the manual?",
            "What is 24/7 support on my plan?",
            "Calculate my hours for the 9-5 days this week.",
            "12 boxes of 8 pens, sells half, then 5 more",
            "12 boxes of 8 pens, sold all but 5 more than half, how many left?",
            "9 groups of 7 pens, lost 3 more than usual",
            "3 boxes of 4 pens, 2 fewer than needed",
            "12 boxes of 8 pens with 5 more removed",
            "12 boxes of 8 pens minus 5 more",
            "then 5 fewer were sold after 12 boxes of 8 pens",
            "days between 2026-11-01 and 2026-11-27 excluding weekends",
            "14 days after 2026-11-27 not counting public holidays",
            "The parcel arrived 3 days after 2026-11-20.",
            "How many seconds in 1 hour and 30 min?",
            "Convert 5 ft 6 in to cm.",
            "What is 1 hour and 30 min in seconds?",
            "Fax 09-377 1234.",
            "What is 6/2(1+2)?",
            "3/4",
            "9/11",
            "2x4",
            "50%",
            "1+1 deal on pizzas this week.",
        ]

        def check() -> None:
            for question in questions:
                tool = try_exact_tool(question)
                self.assertTrue(tool is None or tool.get("refused"), question)
                result = reason(question, use_default_model=False)
                self.assertIsInstance(result, dict, question)
                self.assertFalse(result["verified"], question)
                self.assertFalse(str(result["answer_source"]).startswith("tool:"), question)
                self.assertEqual(result["contract_version"], 2, question)

        self._both(check)

    def test_explicit_whole_expressions_still_compute(self) -> None:
        def check() -> None:
            self.assertEqual(reason("What is 1,500 + 20?")["answer"], "1520")
            self.assertEqual(reason("What is -5 + 3?")["answer"], "-2")
            self.assertEqual(reason("What is 1/3 + 1/3?")["answer"], "2/3")
            self.assertEqual(reason("What date is 14 days after 2026-11-27?")["answer"], "2026-12-11")
            self.assertEqual(reason("How many days between 2026-11-01 and 2026-11-27?")["answer"], "26")
            qualified = reason(
                "How many days between 2026-11-01 and 2026-11-27 excluding weekends?",
                use_default_model=False,
            )
            self.assertNotEqual(qualified["answer"], "26")
            self.assertFalse(qualified["verified"])

        self._both(check)

    def test_memory_word_overlap_is_not_verification(self) -> None:
        samples = [
            ("Does a 70B model need a large GPU?", "A 70B model needs a large GPU.", "A 70B model does not need a large GPU."),
            ("Who is my dentist?", "My dentist is Dr. Hale.", "Your dentist is Dr. Smith."),
            ("What is the invoice total?", "Invoice 1044 total is $1,250.", "The invoice total is $250."),
            ("What was the low temperature?", "The low temperature was -5 C.", "The low temperature was 5 C."),
            ("When is the dentist?", "Dentist on 2026-11-27.", "The dentist appointment is in 27 days."),
            ("Who paid whom?", "Alice paid Bob 40 dollars.", "Bob paid Alice 40 dollars."),
            ("How large is the backup?", "The backup is 14 GB.", "The backup is 14 TB."),
        ]

        def check() -> None:
            self.assertEqual(
                unsupported_numbers("250", [{"claim": "Invoice 1044 total is $1,250."}]),
                ["250"],
            )
            self.assertEqual(
                unsupported_numbers("5 C", [{"claim": "The low temperature was -5 C."}]),
                ["5"],
            )
            for question, memory, answer in samples:
                result = reason(
                    question,
                    model_fn=lambda _prompt, text=answer: text,
                    retriever=lambda _question, claim=memory: [{"atom_id": "m.md", "claim": claim, "evidence": claim}],
                )
                self.assertFalse(result["verified"], question)
                self.assertNotEqual(result["answer_source"], "memory", question)
                self.assertNotIn(result["answer_source"], {"model+tool_verified"}, question)

        self._both(check)

    def test_a_model_chosen_plan_step_is_not_verified(self) -> None:
        def _plan(prompt: str) -> str:
            if "Return one JSON object" in prompt:
                return '{"problem_type":"math","steps":[{"tool":"calculator","input":"12*8+5"}]}'
            return "I cannot verify it."

        def _dentist(prompt: str) -> str:
            if "Return one JSON object" in prompt:
                return '{"problem_type":"fact","steps":[{"tool":"calculator","input":"2+2"}]}'
            return "I have not written that down."

        def check() -> None:
            pens = reason(
                "A shop has 12 boxes of 8 pens. It sold all but 5 more than half of them. How many are left?",
                model_fn=_plan,
            )
            self.assertNotEqual(pens["answer"], "101")
            self.assertFalse(pens["verified"])
            self.assertNotEqual(pens["answer_source"], "model+tool_verified")
            dentist = reason("Who is my dentist?", model_fn=_dentist)
            self.assertNotEqual(dentist["answer"], "4")
            self.assertFalse(dentist["verified"])

        self._both(check)

    def test_no_question_raises_and_huge_values_decline(self) -> None:
        samples = [
            "99999999999 days after 2026-11-27",
            "9" * 400 + " km to m",
            "What is ((10**20)**20)**20?",
            "What is 1e400+1?",
            "\x00",
            "(" * 60 + "1" + ")" * 60,
        ]

        def check() -> None:
            for question in samples:
                result = reason(question, use_default_model=False)
                self.assertIsInstance(result, dict, question)
                self.assertTrue(result.get("answer_source"), question)
                self.assertFalse(result["verified"], question)
            small = reason("What is 2**-20?", use_default_model=False)
            self.assertIsInstance(small, dict)
            self.assertTrue(small.get("answer_source"))
            for question in (None, 123, b"bytes", ["list"]):
                result = reason(question, use_default_model=False)
                self.assertIsInstance(result, dict)
                self.assertTrue(result.get("answer_source"))
            raised = reason(
                "Where is the spare key?",
                model_fn=lambda _prompt: (_ for _ in ()).throw(RuntimeError("ollama down")),
            )
            self.assertIsInstance(raised, dict)
            self.assertFalse(raised["verified"])

        self._both(check)

    def test_held_out_prose_does_not_fire_a_tool(self) -> None:
        """Sentences kept out of the routing rules. A fire means the mechanism is still too wide."""
        held_out = [
            "Please calculate the tip after you read page 12-14.",
            "What is inside the 4 boxes of nails in the shed?",
            "The invoice says net 30 days after 2026-01-15, and weekends still count on that form.",
            "How many people fit when we make groups of 8 at tables 3 and 4?",
            "Solve which shelf holds box 12 before lunch.",
            "What is 2,500 people doing at the fair?",
            "Evaluate whether 3 days after 2026-11-27 is a Monday in our notes.",
            "Convert the story of 5 km to a diary entry, not to metres.",
            "I bought 3 boxes of 6 eggs and the receipt is in the blue folder.",
            "What's left of the 12 boxes of 8 pens after the sale talk?",
        ]

        def check() -> None:
            for question in held_out:
                tool = try_exact_tool(question)
                self.assertTrue(tool is None or tool.get("refused"), question)

        self._both(check)

    def test_review_probes_in_both_sympy_modes(self) -> None:
        """Every probe from the d04b7fa review, plus the B3 word-problem sentences."""
        blocked = [
            "What is 2026-11-27?",
            "Calculate 2026-11-27",
            "What is 555-1234?",
            "What is 1/1/2027?",
            "2026-11-27",
            "555-1234",
            "(555)-1234",
            "(09) 377-1234",
            "2026 -11",
            "10 -4",
            "(10) -4",
            "((10)) -4",
            "What is 24/7?",
            "Compute 12/25.",
            "Evaluate 9/11.",
            "Calculate 24/7",
            "What is 2/3?",
            "What is 28/11?",
            "What is 16/9?",
            "What is 22/7?",
            "10-4",
            "2026-11",
            "What is 2026-11?",
            "11-27",
            "4-20",
            "9-5",
            "3-2",
            "24-7",
            "50-50",
            "1-0",
            "2-1",
            "112-0",
            "1-800",
            "10.5-12",
            "7.5-9",
            "3.5-4",
            "0.5-1",
            "2.0-1",
            "3.11-3",
            "What is 2 + 3 4?",
            "What is 3 4 + 1?",
            "A shop packs 12 boxes of 8 pens. It sells half of the pens and then gets 5 more pens delivered. How many pens does it have now?",
            "10 boxes of 5 pens",
            "8 groups of 6 and 2 more",
            "12 boxes of 8 pens, sells half, then 5 more",
            "12 boxes of 8 pens, sold all but 5 more",
            "9 groups of 7 pens, lost 3 more than usual",
            "3 boxes of 4 pens, 2 fewer than needed",
            "What is 12 boxes of 8 pens plus 5 more?",
            "A shop sold 12 boxes of 8 pens and then 5 more pens. How many pens were sold?",
        ]
        exact = {
            "What is 2/3 + 0.5?": "7/6",
            "What is 1/3 + 0.0?": "1/3",
            "What is 2.5 * 2?": "5",
            "What is 0.1 + 0.2?": "0.3",
            "What is 40 - 15?": "25",
            "10 - 4": "6",
            "What is 10 - 2 - 3?": "5",
            "Calculate 2026 - 1990": "36",
            "What is 40 -15?": "25",
            "40 − 15": "25",
            ".5 + .25": "0.75",
            "What is 17 * 23 + 9?": "400",
            "What is 1,500 + 20?": "1520",
            "What is -5 + 3?": "-2",
            "What is 100/4?": "25",
            "What is 50/50?": "1",
            "What is 4/20?": "1/5",
            "100/4": "25",
            "4/20": "1/5",
            "What is 1/2 + 1/4?": "3/4",
            "What is 11 / 2 as a decimal?": "5.5",
            "What is (10) -4?": "6",
        }

        def check() -> None:
            seen: dict[str, str] = {}
            for question in blocked:
                tool = try_exact_tool(question)
                self.assertTrue(tool is None or tool.get("refused"), question)
                result = reason(question, use_default_model=False)
                self.assertFalse(result["verified"], question)
                self.assertFalse(result.get("internal_error"), question)
            for question, answer in exact.items():
                result = reason(question, use_default_model=False)
                self.assertEqual(result["answer"], answer, question)
                if "." in result["answer"] and result["verified"]:
                    _assert_verified_decimal(self, result["answer"], _INDEPENDENT_EXACT[question])
                seen[question] = result["answer"]
            self.assertEqual(seen["What is 2.5 * 2?"], "5")
            self.assertEqual(seen["What is 2/3 + 0.5?"], "7/6")

        natural = {}
        for question, answer in exact.items():
            natural[question] = reason(question, use_default_model=False)["answer"]
        with _sympy_blocked():
            blocked_answers = {
                question: reason(question, use_default_model=False)["answer"] for question in exact
            }
            check()
        self.assertEqual(natural, blocked_answers)
        check()

    def test_internal_error_keeps_the_exception_type(self) -> None:
        with patch("scripts.reason_engine.try_exact_tool", side_effect=OverflowError("too big")):
            result = reason("What is 1 + 1?")
        self.assertTrue(result["internal_error"])
        self.assertTrue(any("OverflowError" in error for error in result["errors"]))
        self.assertFalse(result["verified"])

    def test_a_plain_question_does_not_spend_a_planner_call(self) -> None:
        calls: list[str] = []

        def _model(prompt: str) -> str:
            calls.append(prompt)
            return "The kiln is warm."

        result = reason("How hot is the kiln?", model_fn=_model)
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("JSON", calls[0])
        self.assertFalse(result["verified"])
        self.assertFalse(result["internal_error"])

    def test_budget_wins_over_the_ollama_env_timeout(self) -> None:
        import socket
        import threading
        import time

        from scripts.reason_engine import load_config

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        stop = threading.Event()

        def _hang() -> None:
            while not stop.is_set():
                server.settimeout(0.2)
                try:
                    conn, _addr = server.accept()
                except (TimeoutError, socket.timeout):
                    continue
                except OSError:
                    return
                try:
                    conn.settimeout(0.2)
                    while not stop.is_set():
                        try:
                            if not conn.recv(65536):
                                break
                        except (TimeoutError, socket.timeout):
                            continue
                finally:
                    conn.close()

        worker = threading.Thread(target=_hang, daemon=True)
        worker.start()
        original = load_config

        def _short() -> dict:
            config = original()
            config["profiles"]["deep"] = {**config["profiles"]["deep"], "timeout_s": 0.8, "max_retries": 0}
            return config

        env = {
            "OLLAMA_HOST": f"http://127.0.0.1:{port}",
            "BRAIN_OLLAMA_TIMEOUT": "30",
        }
        def _model_workers() -> list[threading.Thread]:
            return [
                thread
                for thread in threading.enumerate()
                if thread.name == "reason-model-call" and thread.is_alive()
            ]

        try:
            with patch.dict(os.environ, env, clear=False):
                os.environ.pop("BRAIN_OLLAMA_HOST", None)
                with patch("scripts.reason_engine.load_config", _short):
                    started = time.perf_counter()
                    result = reason("Why is the sky blue?", use_default_model=True)
                    elapsed = time.perf_counter() - started
            # The call also probes hardware before the budgeted request. That probe is a few seconds on Windows.
            deadline = time.perf_counter() + 12.0
            while time.perf_counter() < deadline and _model_workers():
                time.sleep(0.05)
            self.assertEqual(_model_workers(), [])
        finally:
            stop.set()
            server.close()
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertLess(elapsed, 4.0)
        self.assertIsInstance(result, dict)
        self.assertFalse(result["verified"])
        self.assertTrue(any("timeout" in error for error in result["errors"]))

    def test_unit_prefix_case_and_bit_byte(self) -> None:
        """Prefix and case stay distinct. The assertion is the printed string."""
        from fractions import Fraction

        rows = {
            "100 Mb to kB": "12500",
            "1 Gb to MB": "125",
            "1 Mm to m": "1000000",
            "1 Mg to kg": "1000",
            "1 mm to m": "0.001",
            "1 km to m": "1000",
            "100 Mb to MB": "12.5",
            "8 b to B": "1",
            "1 GiB to MB": "1073.741824",
            "1 Gib to MB": "134.217728",
            "3 mi to km": "4.828032",
            "1 lb to kg": "0.45359237",
            "1 kB to KiB": "0.9765625",
            "1 cm to in": "50/127",
            "2 kg to g": "2000",
            "30 min to s": "1800",
            "3 km to m": "3000",
            "5 KM to m": "5000",
            "1 LB to oz": "16",
            "100 C to F": "212",
            "72 F to C": "200/9",
            "98 F to C": "110/3",
            "61 F to C": "145/9",
            "100 F to C": "340/9",
            "0 F to C": "-160/9",
            "451 F to C": "2095/9",
            "32 F to c": "0",
            "1 Mm to m": "1000000",
            "1 Mg to kg": "1000",
            "1 Ms to s": "1000000",
            "1 Pm to m": "1000000000000000",
            "1 Gm to m": "1000000000",
            "1 Pg to g": "1000000000000000",
            "1 Ps to s": "1000000000000000",
            "1 Km to m": "1000",
            "1 CM to m": "0.01",
            "1 km to m": "1000",
            "1 cm to m": "0.01",
            "1 dm to m": "0.1",
        }
        declined = (
            "1 MB to mb",
            "1 mb to MB",
            "1 MB to mB",
            "1000 mB to B",
            "1 Mb to mb",
            "1 dB to B",
            "10 dB to B",
            "1 cB to B",
            "1 hB to B",
            "1 daB to B",
            "1 nb to b",
            "1 MM to m",
            "1 MG to kg",
            "1 MG to mg",
            "1 PM to m",
            "1 MS to s",
            "1 M to m",
            "1 G to g",
            "1 S to s",
            "1 H to min",
            "1 KM to MM",
            "1 km to M",
            "1 mm to MM",
            "1 Ms to MS",
            "5 Km to M",
            "2 MM to m",
            "3 mm to M",
            "1 mg to MG",
            "1 mi to M",
            "1 GM to m",
            "1 PG to g",
            "1 PS to s",
            "1 kM to m",
            "1 cM to m",
            "1 dM to m",
        )
        independent = {
            "100 Mb to kB": Fraction(100) * 10**6 / 8 / 10**3,
            "1 Gb to MB": Fraction(10**9) / 8 / 10**6,
            "1 Mm to m": Fraction(10**6),
            "1 Mg to kg": Fraction(10**6) / 10**3,
            "1 mm to m": Fraction(1, 1000),
            "1 km to m": Fraction(1000),
            "100 Mb to MB": Fraction(100) / 8,
            "8 b to B": Fraction(1),
            "1 GiB to MB": Fraction(1024**3, 10**6),
            "1 Gib to MB": Fraction(1024**3, 8 * 10**6),
            "3 mi to km": Fraction(3) * Fraction("1609.344") / 1000,
            "1 lb to kg": Fraction("453.59237") / 1000,
            "1 kB to KiB": Fraction(1000, 1024),
            "1 cm to in": Fraction(1, 100) / Fraction(254, 10000),
            "2 kg to g": Fraction(2000),
            "30 min to s": Fraction(1800),
            "3 km to m": Fraction(3000),
            "5 KM to m": Fraction(5000),
            "1 LB to oz": Fraction("453.59237") / Fraction("28.349523125"),
            "100 C to F": Fraction(100) * 9 / 5 + 32,
            "72 F to C": (Fraction(72) - 32) * 5 / 9,
            "98 F to C": (Fraction(98) - 32) * 5 / 9,
            "61 F to C": (Fraction(61) - 32) * 5 / 9,
            "100 F to C": (Fraction(100) - 32) * 5 / 9,
            "0 F to C": (Fraction(0) - 32) * 5 / 9,
            "451 F to C": (Fraction(451) - 32) * 5 / 9,
            "32 F to c": Fraction(0),
            "1 Mm to m": Fraction(10**6),
            "1 Mg to kg": Fraction(10**6) / 10**3,
            "1 Ms to s": Fraction(10**6),
            "1 Pm to m": Fraction(10**15),
            "1 Gm to m": Fraction(10**9),
            "1 Pg to g": Fraction(10**15),
            "1 Ps to s": Fraction(10**15),
            "1 Km to m": Fraction(1000),
            "1 CM to m": Fraction(1, 100),
            "1 km to m": Fraction(1000),
            "1 cm to m": Fraction(1, 100),
            "1 dm to m": Fraction(1, 10),
        }

        def check() -> None:
            for question, expected in rows.items():
                result = reason(f"Convert {question}.", use_default_model=False)
                self.assertEqual(result["answer"], expected, question)
                self.assertTrue(result["verified"], question)
                exact = independent[question]
                self.assertEqual(Fraction(result["answer"]), exact, question)
                if "." in result["answer"]:
                    _assert_verified_decimal(self, result["answer"], exact)
            for question in declined:
                tool = try_exact_tool(f"Convert {question}.")
                self.assertTrue(tool is None or tool.get("refused"), question)
                result = reason(f"Convert {question}.", use_default_model=False)
                self.assertFalse(result["verified"], question)

        self._both(check)

    def test_verified_decimals_match_an_independent_fraction(self) -> None:
        samples = (
            ("What is 0.2 + 0.3?", Fraction(1, 5) + Fraction(3, 10)),
            ("What is 0.5 * 0.5?", Fraction(1, 2) * Fraction(1, 2)),
            ("Convert 2 cm to m.", Fraction(2, 100)),
            ("What is 11 / 2 as a decimal?", Fraction(11, 2)),
            ("Convert 72 F to C.", (Fraction(72) - 32) * 5 / 9),
        )

        def check() -> None:
            for question, exact in samples:
                result = reason(question, use_default_model=False)
                self.assertEqual(Fraction(result["answer"]), exact, question)
                if "." in result["answer"] and result["verified"]:
                    _assert_verified_decimal(self, result["answer"], exact)

        self._both(check)


class ReasonBenchmarkTests(unittest.TestCase):
    def test_tool_precision_on_the_migrated_controls(self) -> None:
        """Five migrated tool controls stay exact. gsm-pens (tools-0007) correctly declines."""
        from scripts.reason_bench import read_jsonl, bench_root, run_tools_only, repo_root

        must_fire = {
            "tools-0001",
            "tools-0002",
            "tools-0003",
            "tools-0004",
            "tools-0005",
        }
        items = [row for row in read_jsonl(bench_root(repo_root()) / "suites" / "tools.jsonl") if row["id"] in must_fire or row["id"] == "tools-0007"]
        self.assertEqual(len(items), 6)
        records = run_tools_only(items, sympy="natural", tier="tool-precision")
        by_id = {row["id"]: row for row in records}
        answered = [by_id[item_id] for item_id in sorted(must_fire)]
        self.assertTrue(all(row.get("answer_source") == "tool_exact" and row["correct"] for row in answered), answered)
        pens = by_id["tools-0007"]
        if pens.get("answer_source") == "tool_exact":
            self.assertTrue(pens["correct"])
            self.assertEqual(str(pens.get("predicted")), "101")
        else:
            self.assertNotEqual(pens.get("answer_source"), "tool_exact")
        self.assertEqual(sum(row["model_calls"] for row in records), 0)

    def test_live_models_are_recorded_when_ollama_is_required(self) -> None:
        if os.environ.get("BRAIN_REQUIRE_OLLAMA") != "1":
            self.skipTest("live comparison runs in the advanced workflow")
        from scripts.ollama_utils import chat_with_fallback

        questions = [
            {"id": "arith-17-23-9", "kind": "tool", "question": "What is 17 * 23 + 9?", "answer": "400"},
            {
                "id": "date-14-days",
                "kind": "tool",
                "question": "What date is 14 days after 2026-11-27?",
                "answer": "2026-12-11",
            },
            {
                "id": "pens-half",
                "kind": "model",
                "question": "12 boxes of 8 pens, sells half, then 5 more",
                "answer": "53",
            },
            {
                "id": "rule-80-20",
                "kind": "model",
                "question": "What is the 80/20 rule?",
                "answer": "",
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
                    f"BENCH {row['id']} kind={row.get('kind') or ''} {row['runner']} {row['model']} "
                    f"correct={int(bool(row['correct']))} elapsed_ms={row['elapsed_ms']} "
                    f"source={row.get('answer_source')} verified={row.get('verified')} "
                    f"model_calls={row.get('model_calls')} "
                    f"answer={str(row.get('answer') or '')[:160]!r} "
                    f"error={str(row.get('error') or '')[:200]!r}"
                )
        self.assertEqual(len(models), 16)
        failed_calls = [row for row in models if row.get("error")]
        self.assertEqual(failed_calls, [], "a required live model call did not reach Ollama")
        engine_tools = [
            row for row in models
            if row["runner"] == "engine" and row.get("kind") == "tool"
        ]
        self.assertTrue(engine_tools)
        self.assertTrue(all(row["correct"] and row.get("verified") for row in engine_tools))
        engine_models = [
            row for row in models
            if row["runner"] == "engine" and row.get("kind") == "model"
        ]
        self.assertTrue(engine_models)
        self.assertTrue(all(row.get("verified") is False for row in engine_models))


def _empty_paths():
    from scripts.streaming_chat import ChatPaths

    root = Path(tempfile.mkdtemp(prefix="sb-reason-"))
    (root / "2-atoms").mkdir()
    (root / ".sidecar").mkdir()
    return ChatPaths.for_brain(root)


if __name__ == "__main__":
    unittest.main()
