"""Stage 1 reasoning engine.

Deep is the default. Exact questions go to a tool. Open questions can use a
model, then a check. Chat may hand an ordinary question to the existing
grounded answer path after the engine has classified it. That hand-off is
recorded on the result. It is not a second hidden answer.
"""

from __future__ import annotations

import ast
import json
import operator
import os
import re
import threading
import time
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config" / "reasoning.json"
BENCHMARK_PATH = REPO_ROOT / "docs" / "reasoning-benchmarks.json"
ABSTAIN = "I can't verify an answer for this. No checked result is available."

_PROFILES = {
    "deep": {"max_steps": 6, "max_model_calls": 8, "max_retries": 1, "timeout_s": 180},
    "fast": {"max_steps": 2, "max_model_calls": 1, "max_retries": 0, "timeout_s": 25},
}
_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_LENGTH_TO_M = {
    "mm": 0.001,
    "cm": 0.01,
    "m": 1.0,
    "km": 1000.0,
    "in": 0.0254,
    "ft": 0.3048,
    "mi": 1609.344,
}
_MASS_TO_G = {"mg": 0.001, "g": 1.0, "kg": 1000.0, "oz": 28.349523125, "lb": 453.59237}
_TIME_TO_S = {"s": 1.0, "sec": 1.0, "min": 60.0, "h": 3600.0, "hour": 3600.0, "hours": 3600.0}
_BYTES = {"b": 1.0, "kb": 1000.0, "mb": 1000.0 ** 2, "gb": 1000.0 ** 3, "kib": 1024.0, "mib": 1024.0 ** 2, "gib": 1024.0 ** 3}
_UNIT_ALIASES = {
    "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "kilometer": "km", "kilometers": "km", "kilometre": "km", "kilometres": "km",
    "gram": "g", "grams": "g", "kilogram": "kg", "kilograms": "kg",
    "pound": "lb", "pounds": "lb", "ounce": "oz", "ounces": "oz",
    "second": "s", "seconds": "s", "minute": "min", "minutes": "min",
    "celsius": "c", "fahrenheit": "f",
}


def load_config() -> dict[str, Any]:
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        loaded = {}
    profiles = dict(_PROFILES)
    for name, budget in (loaded.get("profiles") or {}).items():
        if name in profiles and isinstance(budget, dict):
            profiles[name] = {**profiles[name], **{k: budget[k] for k in profiles[name] if k in budget}}
    default = str(loaded.get("default_profile") or "deep").strip().lower()
    if default not in profiles:
        default = "deep"
    return {
        "default_profile": default,
        "profiles": profiles,
        "latency_note": str(loaded.get("latency_note") or ""),
    }


def resolve_profile(profile: str | None) -> str:
    """Explicit argument wins, then BRAIN_REASON_PROFILE, then config. The file default is deep."""
    if profile is not None and str(profile).strip():
        cleaned = str(profile).strip().lower()
        if cleaned not in {"deep", "fast"}:
            raise ValueError("profile must be 'deep' or 'fast'.")
        return cleaned
    env = (os.environ.get("BRAIN_REASON_PROFILE") or "").strip().lower()
    if env in {"deep", "fast"}:
        return env
    return load_config()["default_profile"]


def budget_for(profile: str | None) -> dict[str, float]:
    name = resolve_profile(profile)
    return {"name": name, **load_config()["profiles"][name]}


def server_instructions() -> str:
    note = load_config()["latency_note"]
    return (
        "Use this brain's tools and memory for the owner's notes. "
        "Do not route your own reasoning through the local reason tool before every plan or build. "
        "Deep reasoning is the default for this brain's own chat and for callers that use the reason tool. "
        "Omit profile to use deep. Pass profile fast only when the caller explicitly wants the smaller budget. "
        "The reason tool checks arithmetic, dates, and units. "
        "A result is verified only when an independent check says so; otherwise say it was not verified. "
        + note
    )


def _format_fraction(numer: int, denom: int) -> str:
    if denom < 0:
        numer, denom = -numer, -denom
    if denom == 0:
        raise ValueError("non-finite result")
    if denom == 1:
        return str(numer)
    return f"{numer}/{denom}"


def _format_number(value: Any) -> str:
    """One format for SymPy values and for the restricted evaluator.

    Exact ratios stay as fractions. 2/3 is not a rounded decimal.
    """
    if isinstance(value, bool):
        raise ValueError("not a number")
    if isinstance(value, Fraction):
        return _format_fraction(value.numerator, value.denominator)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError("non-finite result")
        if value.is_integer():
            return str(int(value))
        return format(value, ".12g")
    as_numer_denom = getattr(value, "as_numer_denom", None)
    if getattr(value, "is_rational", None) is True and callable(as_numer_denom):
        numer, denom = as_numer_denom()
        return _format_fraction(int(numer), int(denom))
    if type(value).__name__ == "Float":
        number = float(value)
        if number != number or number in {float("inf"), float("-inf")}:
            raise ValueError("non-finite result")
        if number.is_integer():
            return str(int(number))
        return format(number, ".12g")
    is_integer = getattr(value, "is_integer", None)
    if isinstance(is_integer, bool) or callable(is_integer):
        integer = is_integer() if callable(is_integer) else is_integer
        if integer:
            return str(int(value))
        number = float(value)
        if number != number or number in {float("inf"), float("-inf")}:
            raise ValueError("non-finite result")
        if number.is_integer():
            return str(int(number))
        return format(number, ".12g")
    raise ValueError("not a number")


def _as_fraction(value: Any) -> Fraction | None:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Fraction(value)
    return None


def _eval_node(node: ast.AST) -> Fraction | float | int:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        if isinstance(node.value, int):
            return Fraction(node.value)
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_node(node.operand)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Pow):
            exponent = _as_fraction(right)
            if exponent is None or exponent.denominator != 1 or abs(exponent.numerator) > _MAX_EXPONENT:
                raise ValueError("exponent is too large")
            try:
                powered = left ** exponent.numerator
            except OverflowError as exc:
                raise ValueError("result is too large") from exc
            if isinstance(powered, int) and abs(powered) > _MAX_ABS_RESULT:
                raise ValueError("result is too large")
            return powered
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise ZeroDivisionError("division by zero")
            left_exact = _as_fraction(left)
            right_exact = _as_fraction(right)
            if left_exact is not None and right_exact is not None:
                return left_exact / right_exact
        return _OPS[type(node.op)](left, right)
    raise ValueError("expression is not allowed")


def _ast_eval(expr: str) -> Fraction | float | int:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError("expression is not plain arithmetic") from exc
    return _eval_node(tree.body)


def _sympy_value(expr: str) -> Any:
    import sympy

    value = sympy.sympify(expr, evaluate=True)
    if not getattr(value, "is_number", False):
        raise ValueError("not a number")
    return value


_COMPUTE_PREFIXES = (
    "what date is",
    "how much is",
    "how many",
    "what is",
    "what's",
    "calculate",
    "compute",
    "evaluate",
    "convert",
    "solve",
)
_DATE_AFTER_RE = re.compile(r"^(\d{1,6})\s+days?\s+after\s+(\d{4}-\d{2}-\d{2})$", re.IGNORECASE)
_DATE_BETWEEN_RE = re.compile(
    r"^days?\s+between\s+(\d{4}-\d{2}-\d{2})\s+and\s+(\d{4}-\d{2}-\d{2})$",
    re.IGNORECASE,
)
_UNIT_RE = re.compile(
    r"^(\d{1,18}(?:\.\d{1,12})?)\s*([A-Za-z°]+)\s+(?:to|in|into)\s+([A-Za-z°]+)$",
    re.IGNORECASE,
)
_MAX_EXPONENT = 20
_MAX_ABS_RESULT = 10**18


def _normalize_operators(text: str) -> str:
    cleaned = text.replace("×", "*").replace("·", "*").replace("∙", "*")
    cleaned = re.sub(r"(?<=\d)\s*[xX]\s*(?=\d)", "*", cleaned)
    cleaned = cleaned.replace("^", "**")
    return cleaned


def _balanced(expr: str) -> bool:
    depth = 0
    for char in expr:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _decimals_to_ratios(expr: str) -> str:
    """Turn 2.5 into 25/10 so both evaluators see an exact ratio, not a rounded float."""
    def _replace(match: re.Match[str]) -> str:
        whole, frac = match.group(0).split(".", 1)
        digits = f"{whole or '0'}{frac}"
        return f"({int(digits)}/{10 ** len(frac)})"

    return re.sub(r"\d*\.\d+(?![eE])", _replace, expr)


def _prepare_expression(expr: str) -> str:
    cleaned = re.sub(r"\s+", "", _normalize_operators(expr))
    if re.fullmatch(r"\d+(?:\.\d+)?%", cleaned):
        cleaned = f"({cleaned[:-1]})/100"
    if len(cleaned) > 80 or not re.fullmatch(r"[0-9+\-*/%.()**]+", cleaned) or not _balanced(cleaned):
        raise ValueError("expression is not plain arithmetic")
    if re.search(r"(?<![\d.])0\d", cleaned):
        raise ValueError("expression is not plain arithmetic")
    if not re.search(r"[+\-*/%]|(\*\*)", cleaned):
        raise ValueError("expression has no operator")
    for power in re.finditer(r"\*\*(\d+)", cleaned):
        if int(power.group(1)) > _MAX_EXPONENT:
            raise ValueError("exponent is too large")
    cleaned = _decimals_to_ratios(cleaned)
    try:
        ast.parse(cleaned, mode="eval")
    except SyntaxError as exc:
        raise ValueError("expression is not plain arithmetic") from exc
    return cleaned


def _ordinary_number_text(expr: str) -> bool:
    """Dates, phones, and bare ranges stay non-arithmetic even after a compute prefix."""
    return _date_phone_or_range(expr)


def _is_whole_expression(text: str) -> bool:
    try:
        _prepare_expression(text)
    except ValueError:
        return False
    return True


def exact_arithmetic(expr: str) -> tuple[str, str, bool]:
    """Return (text, answer_source, verified). Verified only when SymPy and the AST agree.

    A rounded float is never verified. Decimal literals are exact ratios first.
    """
    cleaned = _prepare_expression(expr)
    try:
        restricted_value = _ast_eval(cleaned)
        restricted = _format_number(restricted_value)
    except SyntaxError as exc:
        raise ValueError("expression is not plain arithmetic") from exc
    rounded = isinstance(restricted_value, float) and not float(restricted_value).is_integer()
    try:
        symbolic = _format_number(_sympy_value(cleaned))
    except ImportError:
        return restricted, "tool:calculator", False
    except (ValueError, ZeroDivisionError, OverflowError, SyntaxError):
        raise
    except Exception as exc:
        raise ValueError(str(exc)) from exc
    if symbolic != restricted:
        raise ValueError("calculator mismatch")
    if rounded:
        return restricted, "tool:calculator", False
    return symbolic, "tool:sympy", True


def _question_text(question: Any) -> str:
    """Coerce a caller value to text. Bad types become an empty question, not an exception."""
    try:
        if question is None:
            return ""
        if isinstance(question, (bytes, bytearray)):
            return question.decode("utf-8", "replace")
        return str(question)
    except Exception:
        return ""


def _normalize_question(question: str) -> str:
    text = question.replace("\u2019", "'").replace("\u2018", "'")
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip("?.!").strip()


def _strip_compute_prefix(text: str) -> tuple[str, bool]:
    """Remove one leading compute phrase. A phrase later in the sentence is not a route."""
    for prefix in _COMPUTE_PREFIXES:
        if text.casefold().startswith(prefix) and (len(text) == len(prefix) or text[len(prefix)].isspace()):
            return text[len(prefix):].strip(), True
    return text, False


def _strip_thousands(text: str) -> str:
    return re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)


def _glued_dimension(text: str) -> bool:
    """2x4 and 1920x1080 are names. Spaced '2 x 3' is multiplication."""
    compact = re.sub(r"\s+", "", text)
    return bool(re.fullmatch(r"\d+[xX]\d+", compact)) and not re.search(r"\d\s+[xX]\s+\d", text)


def _date_phone_or_range(text: str) -> bool:
    """ISO dates, d/m/y dates, phones, and bare a-b ranges are not calculations.

    A leading 'what is' or 'calculate' does not turn them into arithmetic.
    """
    compact = re.sub(r"\s+", "", text)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", compact):
        return True
    if re.fullmatch(r"\d{1,4}/\d{1,4}/\d{2,4}", compact):
        return True
    if re.fullmatch(r"\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)+", compact) and not re.search(r"[+*/%^xX×]", text):
        return True
    return False


def _ordinary_literal(text: str) -> bool:
    """A bare phone, date, range, percent, or dimension is not a calculation."""
    compact = re.sub(r"\s+", "", text)
    if _date_phone_or_range(text):
        return True
    if re.fullmatch(r"\d+/\d+(?:/\d+)?", compact):
        return True
    if re.fullmatch(r"\d+(?:\.\d+)?%", compact):
        return True
    if _glued_dimension(text):
        return True
    if re.fullmatch(r"\d+\.\d+-\d+\.\d+", compact):
        return True
    return False


def _expression_text(text: str, *, prefixed: bool) -> str | None:
    """The whole remainder is one expression, or it is not a tool question."""
    if not text or _glued_dimension(text) or _date_phone_or_range(text):
        return None
    if _ordinary_literal(text) and not prefixed:
        return None
    if re.search(r"\d\s+\d", text):
        return None
    if not re.fullmatch(r"[0-9+\-*/%.()\s*^×xX·∙,eE]+", text):
        return None
    candidate = re.sub(r"\s+", "", _normalize_operators(_strip_thousands(text)))
    if not _is_whole_expression(candidate):
        return None
    return candidate


def extract_expression(question: str) -> str | None:
    """Fire only when the whole question, minus one leading compute phrase, is the expression."""
    text = _normalize_question(_question_text(question))
    if not text:
        return None
    body, prefixed = _strip_compute_prefix(text)
    return _expression_text(body, prefixed=prefixed)


def _date_pair(question: str) -> tuple[str, str] | None:
    """Two independent date calculations. The whole question must be the pattern."""
    text = _normalize_question(_question_text(question))
    body, _prefixed = _strip_compute_prefix(text)
    try:
        after = _DATE_AFTER_RE.fullmatch(body)
        if after:
            start = date.fromisoformat(after.group(2))
            days = int(after.group(1))
            by_delta = (start + timedelta(days=days)).isoformat()
            by_ordinal = date.fromordinal(start.toordinal() + days).isoformat()
            return by_delta, by_ordinal
        between = _DATE_BETWEEN_RE.fullmatch(body)
        if between:
            left = date.fromisoformat(between.group(1))
            right = date.fromisoformat(between.group(2))
            by_delta = str(abs((right - left).days))
            by_ordinal = str(abs(right.toordinal() - left.toordinal()))
            return by_delta, by_ordinal
    except (ValueError, OverflowError):
        return None
    return None


def _date_answer(question: str) -> str | None:
    pair = _date_pair(question)
    if pair is None or pair[0] != pair[1]:
        return None
    return pair[0]


def _canon_unit(raw: str) -> str:
    token = raw.strip().lower().replace("°", "")
    return _UNIT_ALIASES.get(token, token)


def _unit_answer(question: str) -> str | None:
    text = _normalize_question(_question_text(question))
    body, _prefixed = _strip_compute_prefix(text)
    match = _UNIT_RE.fullmatch(body)
    if not match:
        return None
    try:
        amount = float(match.group(1))
    except (ValueError, OverflowError):
        return None
    raw_src = match.group(2)
    raw_dst = match.group(3)
    if raw_src.casefold() == raw_dst.casefold() and raw_src != raw_dst:
        return None
    src = _canon_unit(raw_src)
    dst = _canon_unit(raw_dst)
    if src in {"c", "f"} or dst in {"c", "f"}:
        if src == "c" and dst == "f":
            value = amount * 9.0 / 5.0 + 32.0
        elif src == "f" and dst == "c":
            value = (amount - 32.0) * 5.0 / 9.0
        elif src == dst:
            value = amount
        else:
            return None
        forward = _format_number(value)
        if src == dst:
            back = float(forward)
        elif src == "c" and dst == "f":
            back = (float(forward) - 32.0) * 5.0 / 9.0
        elif src == "f" and dst == "c":
            back = float(forward) * 9.0 / 5.0 + 32.0
        else:
            return None
        if abs(back - amount) > 1e-6:
            return None
        return forward
    for table in (_LENGTH_TO_M, _MASS_TO_G, _TIME_TO_S, _BYTES):
        if src in table and dst in table:
            value = amount * table[src] / table[dst]
            forward = _format_number(value)
            back = float(forward) * table[dst] / table[src]
            if abs(back - amount) > max(1e-6, abs(amount) * 1e-9):
                return None
            return forward
    return None


def classify(question: str) -> str:
    if _date_answer(question) is not None:
        return "date"
    if _unit_answer(question) is not None:
        return "unit"
    if re.search(r"\b(write|implement)\b.{0,40}\bfunction\b|\bdef\s+[A-Za-z_]", question, re.I):
        return "code"
    if extract_expression(question):
        return "math"
    if re.search(r"\b(plan|design|architecture)\b", question, re.I):
        return "planning"
    return "other"


def try_exact_tool(question: str) -> dict[str, Any] | None:
    """Solve a whole-question tool, refuse a real divide-by-zero, or decline."""
    try:
        return _try_exact_tool(question)
    except Exception:
        return None


def _try_exact_tool(question: str) -> dict[str, Any] | None:
    text = _question_text(question)
    if _date_pair(text) is not None:
        pair = _date_pair(text)
        if pair is None or pair[0] != pair[1]:
            return None
        return _tool_result(text, "date", pair[0], "tool:date", f"date math -> {pair[0]}", verified=True)
    answer = _unit_answer(text)
    if answer is not None:
        return _tool_result(text, "unit", answer, "tool:unit", f"unit conversion -> {answer}", verified=True)
    expr = extract_expression(text)
    if not expr:
        return None
    try:
        value, source, verified = exact_arithmetic(expr)
    except ZeroDivisionError:
        return {"refused": True, "problem_type": "math"}
    except (SyntaxError, ValueError, OverflowError, ArithmeticError):
        return None
    return _tool_result(text, "math", value, source, f"{expr} = {value}", verified=verified)


def _tool_result(
    question: str,
    kind: str,
    answer: str,
    answer_source: str,
    detail: str,
    *,
    verified: bool,
) -> dict[str, Any]:
    step = {
        "tool": kind,
        "status": "verified" if verified else "unchecked",
        "input": question[:500],
        "output": answer,
        "answer_source": answer_source,
    }
    return {
        "answer": answer,
        "answer_source": answer_source,
        "abstained": False,
        "problem_type": kind,
        "steps": [step],
        "tool_calls": [step],
        "citations": [{"source": answer_source, "detail": detail}],
        "confidence": 1.0 if verified else 0.0,
        "backend": answer_source,
        "model_calls": 0,
        "verified": verified,
    }


def _strip_fence(text: str) -> str:
    match = re.search(r"```(?:python)?\s*(.*?)```", text, re.S | re.I)
    return (match.group(1) if match else text).strip()


def run_python_checks(code: str, tests: str, *, timeout_s: float = 5.0) -> dict[str, Any]:
    from scripts.symbolic_sandbox import SandboxSecurityError, run_sandboxed_python

    program = _strip_fence(code).rstrip() + "\n" + tests.strip() + "\n"
    try:
        return run_sandboxed_python(program, timeout_seconds=min(5.0, max(0.5, timeout_s)))
    except (SandboxSecurityError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "result": None, "stdout": ""}


_NUMBER_TOKEN_RE = re.compile(
    r"(?<![\d.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?!\d)"
)
_REFUSAL_RE = re.compile(
    r"\b(?:i don't know|i do not know|i dont know|i'm not sure|i am not sure|"
    r"cannot verify|can't verify|i can't|i cannot|no information|not enough information|"
    r"i have no|unsure)\b",
    re.IGNORECASE,
)


def _canon_number(token: str) -> str:
    """'1,250' and '1250' are the same token. '-5' is not '5'. '4' is not '14'."""
    body = str(token or "").replace(",", "").strip()
    sign = ""
    if body[:1] in "+-":
        sign = "-" if body[0] == "-" else ""
        body = body[1:]
    if "." in body:
        whole, frac = body.split(".", 1)
        frac = frac.rstrip("0")
        body = whole if not frac else f"{whole}.{frac}"
    return (sign + (body or "0")) if body else ""


def _numbers_in(text: str) -> list[str]:
    """Whole number tokens, including a sign and thousands separators."""
    return _NUMBER_TOKEN_RE.findall(str(text or ""))


def _is_refusal(text: str) -> bool:
    return bool(_REFUSAL_RE.search(str(text or "")))


def _memory_blob(item: dict[str, Any]) -> str:
    return "\n".join(str(item.get(key) or "") for key in ("claim", "evidence", "title"))


def unsupported_numbers(answer: str, sources: list[dict[str, Any]]) -> list[str]:
    """Numbers in the answer that are not the same whole token in retrieved memory.

    An empty source list does not reject the answer. Sharing a word is not support.
    """
    if not sources:
        return []
    known = {_canon_number(number) for item in sources for number in _numbers_in(_memory_blob(item))}
    known.discard("")
    return [number for number in _numbers_in(answer) if _canon_number(number) not in known]


def _default_model(prompt: str, *, timeout_s: float | None = None) -> str:
    from scripts.ollama_utils import chat_with_fallback

    call: dict[str, Any] = {
        "allow_model_fallback": False,
        "options": {"temperature": 0.1, "num_predict": 256},
    }
    if timeout_s is not None:
        call["timeout"] = max(0.05, float(timeout_s))
    response = chat_with_fallback(
        [{"role": "user", "content": prompt}],
        **call,
    )
    if isinstance(response, dict):
        return str((response.get("message") or {}).get("content") or "")
    return str(response or "")


def _budget_expired(state: dict[str, Any]) -> bool:
    """True once the profile budget, including the 180s deep budget, is spent."""
    limit = float(state["budget"]["timeout_s"])
    return (time.perf_counter() - state["started"]) > limit


def _call_model(model_fn: Callable[[str], str] | None, prompt: str, state: dict[str, Any]) -> str:
    """One model call on a worker thread, bounded by the time still left in the budget."""
    budget = state["budget"]
    if state["model_calls"] >= int(budget["max_model_calls"]):
        state["errors"].append("model call budget is spent")
        return ""
    if _budget_expired(state):
        state["errors"].append("reasoning timeout")
        return ""
    remaining = float(budget["timeout_s"]) - (time.perf_counter() - state["started"])
    if remaining <= 0:
        state["errors"].append("reasoning timeout")
        return ""
    fn = model_fn
    if fn is None:
        if not state.get("allow_default_model"):
            state["errors"].append("no model configured")
            return ""
        budget_left = remaining

        def fn(prompt: str, _left: float = budget_left) -> str:
            return _default_model(prompt, timeout_s=_left)

    state["model_calls"] += 1
    box: dict[str, str] = {}

    def _run() -> None:
        try:
            box["text"] = str(fn(prompt) or "")
        except Exception as exc:
            box["error"] = str(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(remaining)
    if worker.is_alive():
        state["errors"].append("reasoning timeout")
        return ""
    if "error" in box:
        state["errors"].append(box["error"])
        return ""
    return box.get("text", "")


def _parse_plan(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        loaded = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, dict) or not isinstance(loaded.get("steps"), list):
        return None
    return loaded


def _heuristic_plan(kind: str) -> dict[str, Any]:
    if kind == "code":
        steps = [{"tool": "python", "input": "write the function, then run the tests"}]
    elif kind == "planning":
        steps = [
            {"tool": "retrieve", "input": "related notes"},
            {"tool": "model", "input": "short plan from the notes"},
        ]
    else:
        steps = [
            {"tool": "retrieve", "input": "related memory"},
            {"tool": "model", "input": "answer only from the retrieved text"},
        ]
    return {"problem_type": kind, "steps": steps, "planner": "template"}


def _model_plan(question: str, model_fn: Callable[[str], str] | None, state: dict[str, Any]) -> dict[str, Any]:
    prompt = (
        "Return one JSON object and no other text. "
        '{"problem_type":"math|date|unit|code|fact|planning|other",'
        '"steps":[{"tool":"calculator|date|unit|python|retrieve|model","input":"short"}]}'
        f"\nQuestion: {question[:1500]}"
    )
    text = _call_model(model_fn, prompt, state)
    plan = _parse_plan(text)
    if plan is None and int(state["budget"]["max_retries"]) > 0 and state["model_calls"] < int(state["budget"]["max_model_calls"]):
        text = _call_model(model_fn, prompt + "\nThe previous reply was not a JSON object. Return only the object.", state)
        plan = _parse_plan(text)
    if plan is None:
        return _heuristic_plan(classify(question))
    plan["planner"] = "model"
    plan["steps"] = [step for step in plan.get("steps") or [] if isinstance(step, dict)][: int(state["budget"]["max_steps"])]
    return plan


def _execute_plan_step(step: dict[str, Any], question: str) -> dict[str, Any] | None:
    """Run one planned tool step. Calculator input must itself be a safe expression."""
    tool = str(step.get("tool") or "").strip().lower()
    raw = str(step.get("input") or "").strip() or question
    if tool in {"calculator", "math"}:
        expr = raw if _is_whole_expression(re.sub(r"\s+", "", _normalize_operators(raw))) else extract_expression(raw)
        if not expr:
            return None
        try:
            value, source, verified = exact_arithmetic(expr)
        except (SyntaxError, ValueError, ZeroDivisionError, OverflowError):
            return None
        solved = _tool_result(raw, "math", value, source, f"{expr} = {value}", verified=False)
        solved["answer_source"] = source
        solved["verified"] = False
        return solved
    if tool == "date":
        pair = _date_pair(raw)
        if pair and pair[0] == pair[1]:
            solved = _tool_result(raw, "date", pair[0], "tool:date", f"date math -> {pair[0]}", verified=False)
            solved["verified"] = False
            return solved
    if tool == "unit":
        answer = _unit_answer(raw)
        if answer is not None:
            solved = _tool_result(raw, "unit", answer, "tool:unit", f"unit conversion -> {answer}", verified=False)
            solved["verified"] = False
            return solved
    return None


def _abstain(kind: str, steps: list[dict[str, Any]], sources: list[dict[str, Any]], plan: dict[str, Any] | None = None) -> dict[str, Any]:
    body = {
        "answer": ABSTAIN,
        "answer_source": "abstain",
        "abstained": True,
        "problem_type": kind,
        "steps": steps,
        "tool_calls": [],
        "citations": [_citation(item) for item in sources],
        "confidence": 0.0,
        "backend": None,
        "verified": False,
    }
    if plan is not None:
        body["plan"] = plan
    return body


def _model_answer(
    draft: str,
    *,
    kind: str,
    sources: list[dict[str, Any]],
    steps: list[dict[str, Any]],
    plan: dict[str, Any],
) -> dict[str, Any]:
    text = draft.strip()
    missing = unsupported_numbers(text, sources)
    refusal = _is_refusal(text)
    if not text or missing or refusal:
        return _abstain(kind, steps + [{"tool": "model", "status": "rejected"}], sources, plan)
    return {
        "answer": text,
        "answer_source": "model",
        "abstained": False,
        "problem_type": kind,
        "steps": steps + [{"tool": "model", "status": "unchecked"}],
        "tool_calls": [],
        "citations": [_citation(item) for item in sources],
        "confidence": 0.0,
        "backend": None,
        "verified": False,
        "plan": plan,
    }


def _fallback_result(exc: BaseException | None = None) -> dict[str, Any]:
    return {
        "answer": ABSTAIN,
        "answer_source": "abstain",
        "verified": False,
        "abstained": True,
        "problem_type": "other",
        "contract_version": 2,
        "steps": [],
        "tool_calls": [],
        "citations": [],
        "confidence": 0.0,
        "backend": None,
        "model_calls": 0,
        "elapsed_ms": 0.0,
        "errors": ["reasoning failed" if exc is None else f"reasoning failed: {type(exc).__name__}"],
        "internal_error": True,
        "engine": True,
        "profile": "deep",
    }


def _failure_note(exc: BaseException) -> str:
    return f"reasoning failed: {type(exc).__name__}"


def _safe_abstain(question: Any, profile: str | None, exc: BaseException) -> dict[str, Any]:
    """A caller always gets a result dict. This helper does not classify or raise."""
    try:
        try:
            budget = budget_for(profile)
        except Exception:
            budget = {"name": "deep", **_PROFILES["deep"]}
        started = time.perf_counter()
        state: dict[str, Any] = {
            "budget": budget,
            "model_calls": 0,
            "started": started,
            "errors": [_failure_note(exc)],
            "allow_default_model": False,
        }
        return _finish(_abstain("other", [], [], None), budget, state, started, internal_error=True)
    except Exception:
        return _fallback_result(exc)


def reason(
    question: str,
    *,
    profile: str | None = None,
    model_fn: Callable[[str], str] | None = None,
    retriever: Callable[[str], list[dict[str, Any]]] | None = None,
    tests: str | None = None,
    atoms_dir: str | None = None,
    index_path: str | None = None,
    chroma_path: str | None = None,
    delegate_fn: Callable[[], list[dict[str, Any]]] | None = None,
    use_default_model: bool = False,
) -> dict[str, Any]:
    """Run one question. Profile defaults to deep. A delegate is the existing chat path.

    Question text never raises. Unparseable input stays on the non-tool path.
    """
    try:
        return _reason(
            question,
            profile=profile,
            model_fn=model_fn,
            retriever=retriever,
            tests=tests,
            atoms_dir=atoms_dir,
            index_path=index_path,
            chroma_path=chroma_path,
            delegate_fn=delegate_fn,
            use_default_model=use_default_model,
        )
    except Exception as exc:
        return _safe_abstain(question, profile, exc)


def _reason(
    question: str,
    *,
    profile: str | None = None,
    model_fn: Callable[[str], str] | None = None,
    retriever: Callable[[str], list[dict[str, Any]]] | None = None,
    tests: str | None = None,
    atoms_dir: str | None = None,
    index_path: str | None = None,
    chroma_path: str | None = None,
    delegate_fn: Callable[[], list[dict[str, Any]]] | None = None,
    use_default_model: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    budget = budget_for(profile)
    state: dict[str, Any] = {
        "budget": budget,
        "model_calls": 0,
        "started": started,
        "errors": [],
        "allow_default_model": bool(use_default_model),
        "index_path": index_path,
        "chroma_path": chroma_path,
    }
    text = _question_text(question).strip()
    if not text:
        return _finish(_abstain("other", [], [], None), budget, state, started)
    if _budget_expired(state):
        state["errors"].append("reasoning timeout")
        return _finish(_abstain("other", [], [], None), budget, state, started)
    solved = None if tests else try_exact_tool(text)
    tool_refused = bool(solved and solved.get("refused"))
    if solved is not None and not tool_refused:
        return _finish(solved, budget, state, started)
    kind = "code" if tests else classify(text)
    if tests:
        return _finish(_solve_code(text, tests, model_fn if model_fn or use_default_model else model_fn, state), budget, state, started)
    if delegate_fn is not None and kind in {"other", "planning", "code"} and not tool_refused:
        events = list(delegate_fn())
        trace = {
            "answer": _answer_from_events(events),
            "answer_source": "model",
            "abstained": any(event.get("type") == "abstain" for event in events),
            "problem_type": kind,
            "steps": [{"tool": "grounded_chat", "status": "delegated", "input": text[:500]}],
            "tool_calls": [],
            "citations": _citations_from_events(events),
            "confidence": 0.0,
            "backend": None,
            "verified": False,
            "events": events,
            "delegated": True,
        }
        return _finish(trace, budget, state, started)
    sources = _retrieve(text, retriever, atoms_dir, state)
    can_model = bool(model_fn or use_default_model)
    if tool_refused and can_model:
        draft = _call_model(model_fn, f"Answer the question in one or two sentences.\nQuestion: {text[:1500]}", state)
        if draft.strip() and not _is_refusal(draft):
            return _finish(_model_answer(draft, kind=kind, sources=[], steps=[], plan={"problem_type": kind, "steps": [], "planner": "tool-refused"}), budget, state, started)
        return _finish(_abstain(kind, [], sources, None), budget, state, started)
    plan = _heuristic_plan(kind)
    executed: list[dict[str, Any]] = []
    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            continue
        executed.append({
            "tool": step.get("tool"),
            "status": "not_adopted",
            "input": str(step.get("input") or "")[:300],
            "output": None,
        })
    if _budget_expired(state):
        state["errors"].append("reasoning timeout")
        return _finish(_abstain(kind, executed, sources, plan), budget, state, started)
    if not can_model:
        return _finish(_abstain(kind, executed or list(plan.get("steps") or []), sources, plan), budget, state, started)
    source_block = "\n".join(f"[{item.get('atom_id', 'S')}] {item.get('claim', '')}" for item in sources) or "(no retrieved memory)"
    if sources:
        prompt = (
            "Answer in one or two sentences using only the memory below. "
            "Do not add a number that is not in the memory. If the memory is not enough, say you cannot verify it.\n"
            f"Memory:\n{source_block[:4000]}\nQuestion: {text[:1500]}"
        )
    else:
        prompt = f"Answer the question in one or two sentences.\nQuestion: {text[:1500]}"
    draft = _call_model(model_fn, prompt, state)
    missing = unsupported_numbers(draft, sources)
    retries = 0
    while missing and sources and retries < int(budget["max_retries"]) and state["model_calls"] < int(budget["max_model_calls"]):
        retries += 1
        draft = _call_model(
            model_fn,
            prompt + f"\nThe previous answer used numbers that are not in memory: {', '.join(missing)}. Fix it or say you cannot verify it.",
            state,
        )
        missing = unsupported_numbers(draft, sources)
    return _finish(
        _model_answer(draft, kind=plan.get("problem_type") or kind, sources=sources, steps=executed, plan=plan),
        budget,
        state,
        started,
    )


def _solve_code(question: str, tests: str, model_fn: Callable[[str], str] | None, state: dict[str, Any]) -> dict[str, Any]:
    prompt = (
        "Write only the Python function. No explanation. The tests will be appended and executed.\n"
        f"Task: {question[:1500]}\nTests:\n{tests[:1500]}"
    )
    code = _call_model(model_fn, prompt, state)
    ran = run_python_checks(code, tests)
    retries = 0
    while not ran.get("ok") and retries < int(state["budget"]["max_retries"]):
        retries += 1
        code = _call_model(
            model_fn,
            prompt + f"\nThe tests failed: {ran.get('error') or 'failed'}. Return a corrected function only.",
            state,
        )
        ran = run_python_checks(code, tests)
    if not ran.get("ok"):
        return {
            "answer": ABSTAIN,
            "abstained": True,
            "problem_type": "code",
            "steps": [{"tool": "python", "status": "failed", "output": str(ran.get("error") or "")[:500]}],
            "tool_calls": [{"tool": "python", "status": "failed"}],
            "citations": [{"source": "tool:python", "detail": "tests failed"}],
            "confidence": 0.0,
            "backend": "sandbox",
            "answer_source": "abstain",
            "verified": False,
        }
    return {
        "answer": _strip_fence(code),
        "abstained": False,
        "problem_type": "code",
        "steps": [{"tool": "python", "status": "verified", "output": "tests passed"}],
        "tool_calls": [{"tool": "python", "status": "verified"}],
        "citations": [{"source": "tool:python", "detail": "sandbox tests passed"}],
        "confidence": 1.0,
        "backend": "sandbox",
        "answer_source": "tool:code",
        "verified": True,
    }


def _retrieve(
    question: str,
    retriever: Callable[[str], list[dict[str, Any]]] | None,
    atoms_dir: str | None,
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    try:
        if retriever is not None:
            rows = list(retriever(question) or [])
        elif atoms_dir:
            rows = retrieve_atoms(
                question,
                atoms_dir=atoms_dir,
                index_path=state.get("index_path"),
                chroma_path=state.get("chroma_path"),
            )
        else:
            rows = []
    except Exception as exc:
        state["errors"].append(str(exc))
        rows = []
    clean = []
    for row in rows[:5]:
        if isinstance(row, dict):
            clean.append(row)
    return clean


def vault_search_paths(atoms_dir: str) -> tuple[str, str]:
    """Index paths that belong to this vault, never the process-wide default vault."""
    brain = os.path.dirname(os.path.abspath(atoms_dir))
    return (
        os.path.join(brain, ".sidecar", "lexical.sqlite3"),
        os.path.join(brain, ".chroma"),
    )


def retrieve_atoms(
    question: str,
    *,
    atoms_dir: str,
    index_path: str | None = None,
    chroma_path: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    from scripts.config import CHROMA_PATH, LEXICAL_INDEX_PATH
    from scripts.recall import RecallOptions, search_memory

    derived_index, derived_chroma = vault_search_paths(atoms_dir)
    index_path = index_path or derived_index
    chroma_path = chroma_path or derived_chroma
    if os.path.abspath(index_path) == os.path.abspath(LEXICAL_INDEX_PATH) and os.path.abspath(index_path) != os.path.abspath(derived_index):
        index_path = derived_index
    if os.path.abspath(chroma_path) == os.path.abspath(CHROMA_PATH) and os.path.abspath(chroma_path) != os.path.abspath(derived_chroma):
        chroma_path = derived_chroma
    direct, neighbors = search_memory(
        question,
        RecallOptions(n_results=limit, include_statuses={"accepted"}, retrieval_mode="lexical"),
        atoms_dir=atoms_dir,
        index_path=index_path,
        chroma_path=chroma_path,
    )
    rows = []
    for result in list(direct) + list(neighbors):
        content = str(getattr(result, "content", "") or "")
        rows.append({
            "atom_id": str(getattr(result, "filename", "") or ""),
            "claim": content[:800],
            "evidence": content[:800],
        })
        if len(rows) >= limit:
            break
    return rows


def _citation(item: dict[str, Any]) -> dict[str, str]:
    return {"source": str(item.get("atom_id") or "memory"), "detail": str(item.get("claim") or "")[:300]}


def _answer_from_events(events: list[dict[str, Any]]) -> str:
    parts = [str(event.get("text") or "") for event in events if event.get("type") in {"token", "verified_answer", "abstain"}]
    if not parts:
        for event in events:
            if event.get("type") == "verified_answer":
                return str(event.get("text") or event.get("answer") or "")
    return "".join(parts).strip()


def _citations_from_events(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    citations = []
    for event in events:
        if event.get("type") != "context":
            continue
        for source in event.get("sources") or []:
            if isinstance(source, dict):
                citations.append({
                    "source": str(source.get("atom_id") or source.get("filename") or source.get("citation_id") or ""),
                    "detail": str(source.get("claim") or source.get("excerpt") or "")[:300],
                })
    return citations


def _finish(
    body: dict[str, Any],
    budget: dict[str, Any],
    state: dict[str, Any],
    started: float,
    *,
    internal_error: bool = False,
) -> dict[str, Any]:
    elapsed = round((time.perf_counter() - started) * 1000.0, 2)
    if not body.get("answer_source"):
        body = {**body, "answer_source": "abstain" if body.get("abstained") else "model"}
    body = {**body, "verified": bool(body.get("verified")), "contract_version": 2}
    flag = bool(internal_error or body.get("internal_error"))
    return {
        **body,
        "internal_error": flag,
        "profile": budget["name"],
        "model_calls": state["model_calls"],
        "elapsed_ms": elapsed,
        "budgets": {
            "max_steps": budget["max_steps"],
            "max_model_calls": budget["max_model_calls"],
            "max_retries": budget["max_retries"],
            "timeout_s": budget["timeout_s"],
        },
        "latency_note": load_config()["latency_note"],
        "errors": list(state["errors"]),
        "engine": True,
    }


def tool_chat_events(result: dict[str, Any]) -> list[dict[str, Any]]:
    elapsed = float(result.get("elapsed_ms") or 0)
    return [
        {
            "type": "context",
            "sources": [],
            "verified": [],
            "preferences": [],
            "notes": [],
            "retrieval_ms": 0,
            "warning": None,
        },
        {
            "type": "reasoning",
            "profile": result.get("profile"),
            "problem_type": result.get("problem_type"),
            "steps": result.get("steps") or [],
            "engine": True,
        },
        {"type": "token", "text": str(result.get("answer") or "")},
        {
            "type": "grounding",
            "label": "tool",
            "fully_cited": bool(result.get("verified")),
            "invalid_citations": [],
            "uncited_sentences": [],
            "sandbox_verified": bool(result.get("verified")),
        },
        {
            "type": "done",
            "total_ms": elapsed,
            "elapsed_ms": elapsed,
            "answer_source": result.get("answer_source"),
            "verified": bool(result.get("verified")),
            "contract_version": result.get("contract_version", 2),
            "reasoning": {
                "engine": True,
                "profile": result.get("profile"),
                "problem_type": result.get("problem_type"),
                "steps": result.get("steps") or [],
                "tool_calls": result.get("tool_calls") or [],
                "citations": result.get("citations") or [],
                "confidence": result.get("confidence"),
                "model_calls": result.get("model_calls"),
                "latency_note": result.get("latency_note"),
                "answer_source": result.get("answer_source"),
                "verified": bool(result.get("verified")),
            },
        },
    ]


def delegation_trace(question: str, *, profile: str | None) -> dict[str, Any]:
    resolved = resolve_profile(profile)
    return {
        "engine": True,
        "profile": resolved,
        "problem_type": classify(question),
        "steps": [{"tool": "grounded_chat", "status": "delegated"}],
        "tool_calls": [],
        "model_calls": 0,
        "answer_source": "model",
        "verified": False,
        "latency_note": load_config()["latency_note"],
    }


def load_benchmark() -> dict[str, Any]:
    return json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))


def score_answer(expected: str, answer: str) -> bool:
    """Exact normalized match, or the final number token when the expected answer is numeric.

    An earlier number does not count. 'Not 4; it is 5.' does not score as 4.
    """
    def _norm(text: str) -> str:
        token = str(text or "").strip().casefold().replace(",", "")
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", token):
            return _canon_number(token)
        return re.sub(r"\s+", " ", token)

    expected_norm = _norm(expected)
    answer_text = str(answer or "")
    if _norm(answer_text) == expected_norm:
        return True
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", expected_norm):
        numbers = _numbers_in(answer_text)
        return bool(numbers) and _canon_number(numbers[-1]) == _canon_number(expected_norm)
    return False


def run_deterministic_benchmark() -> list[dict[str, Any]]:
    rows = []
    for item in load_benchmark()["items"]:
        if item.get("requires_model"):
            continue
        started = time.perf_counter()
        result = reason(item["question"], profile="deep", use_default_model=False)
        elapsed = round((time.perf_counter() - started) * 1000.0, 2)
        rows.append({
            "id": item["id"],
            "runner": "engine",
            "model": "none",
            "correct": (not result["abstained"]) and score_answer(item["answer"], result["answer"]),
            "abstained": result["abstained"],
            "answer": result["answer"],
            "elapsed_ms": elapsed,
            "model_calls": result["model_calls"],
        })
    return rows


def run_model_comparison(model_name: str, model_fn: Callable[[str], str], questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compare one plain model call with the engine. The caller supplies the model."""
    rows = []
    for item in questions:
        started = time.perf_counter()
        try:
            plain = model_fn(item["question"])
            plain_error = ""
        except Exception as exc:
            plain = ""
            plain_error = str(exc)
        plain_ms = round((time.perf_counter() - started) * 1000.0, 2)
        rows.append({
            "id": item["id"],
            "kind": item.get("kind", ""),
            "runner": "plain",
            "model": model_name,
            "correct": score_answer(item["answer"], plain),
            "answer": plain[:300],
            "elapsed_ms": plain_ms,
            "error": plain_error,
        })
        started = time.perf_counter()
        result = reason(item["question"], profile="deep", model_fn=model_fn, use_default_model=False)
        rows.append({
            "id": item["id"],
            "kind": item.get("kind", ""),
            "runner": "engine",
            "model": model_name,
            "correct": (not result["abstained"]) and score_answer(item["answer"], result["answer"]) if item.get("answer") else False,
            "abstained": result["abstained"],
            "answer": str(result["answer"])[:300],
            "answer_source": result.get("answer_source"),
            "verified": result.get("verified"),
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
            "model_calls": result["model_calls"],
        })
    return rows


def format_benchmark(rows: list[dict[str, Any]]) -> str:
    lines = ["id runner model correct abstained elapsed_ms model_calls answer"]
    for row in rows:
        lines.append(
            "BENCH {id} {runner} {model} correct={correct} abstained={abstained} elapsed_ms={elapsed_ms} model_calls={model_calls} answer={answer}".format(
                id=row.get("id"),
                runner=row.get("runner"),
                model=row.get("model"),
                correct=int(bool(row.get("correct"))),
                abstained=int(bool(row.get("abstained"))),
                elapsed_ms=row.get("elapsed_ms"),
                model_calls=row.get("model_calls", ""),
                answer=str(row.get("answer") or "").replace("\n", " ")[:180],
            )
        )
    return "\n".join(lines)
