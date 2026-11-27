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
import time
from datetime import date, timedelta
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
_EXPR_RE = re.compile(
    r"(?<![A-Za-z0-9])([+\-]?\d+(?:\.\d+)?(?:\s*[+\-*/%^]\s*[+\-]?\d+(?:\.\d+)?)+)"
)


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
        "Deep reasoning is the default. For a question, a plan, or a build, call reason before you answer. "
        "Omit profile to use deep. Pass profile fast only when the caller explicitly wants the smaller budget. "
        "Use the tool result for arithmetic, dates, and units instead of guessing. "
        "If reason abstains, say the answer was not verified. "
        + note
    )


def _format_number(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("not a number")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not value == value or value in {float("inf"), float("-inf")}:  # NaN / inf
            raise ValueError("non-finite result")
        if value.is_integer():
            return str(int(value))
        return format(value, ".12g")
    if getattr(value, "is_integer", False) and bool(value.is_integer):
        return str(int(value))
    return str(value)


def _eval_node(node: ast.AST) -> float | int:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_node(node.operand)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Pow):
            if isinstance(right, float) or abs(float(right)) > 8:
                raise ValueError("exponent is too large")
        return _OPS[type(node.op)](left, right)
    raise ValueError("expression is not allowed")


def _ast_eval(expr: str) -> float | int:
    tree = ast.parse(expr, mode="eval")
    return _eval_node(tree.body)


def _sympy_value(expr: str) -> Any:
    import sympy

    value = sympy.sympify(expr, evaluate=True)
    if not getattr(value, "is_number", False):
        raise ValueError("not a number")
    return value


def exact_arithmetic(expr: str) -> tuple[str, str]:
    cleaned = expr.replace("^", "**").replace(" ", "")
    if len(cleaned) > 80 or not re.fullmatch(r"[0-9+\-*/%.()]+", cleaned):
        raise ValueError("expression is not plain arithmetic")
    for power in re.finditer(r"\*\*(\d+)", cleaned):
        if int(power.group(1)) > 8:
            raise ValueError("exponent is too large")
    try:
        value = _sympy_value(cleaned)
        return _format_number(value), "sympy"
    except ImportError:
        return _format_number(_ast_eval(cleaned)), "arithmetic"
    except Exception:
        return _format_number(_ast_eval(cleaned)), "arithmetic"


def extract_expression(question: str) -> str | None:
    cleaned = re.sub(r"\d{4}-\d{2}-\d{2}", " ", question)
    match = _EXPR_RE.search(cleaned)
    if not match:
        return None
    expr = re.sub(r"\s+", "", match.group(1))
    if not re.search(r"[+\-*/%^]", expr):
        return None
    return expr


def word_problem_expression(question: str) -> str | None:
    grouped = re.search(r"(\d+)\s+(?:boxes|groups|packs)\s+of\s+(\d+)", question, re.I)
    times = re.search(r"(\d+)\s+times\s+(\d+)", question, re.I)
    if grouped:
        total = int(grouped.group(1)) * int(grouped.group(2))
    elif times:
        total = int(times.group(1)) * int(times.group(2))
    else:
        return None
    more = re.search(r"(\d+)\s+more", question, re.I)
    fewer = re.search(r"(\d+)\s+(?:fewer|less)", question, re.I)
    if more:
        total += int(more.group(1))
    if fewer:
        total -= int(fewer.group(1))
    return str(total)


def _date_answer(question: str) -> str | None:
    try:
        after = re.search(r"(\d+)\s+days?\s+after\s+(\d{4}-\d{2}-\d{2})", question, re.I)
        if after:
            start = date.fromisoformat(after.group(2))
            return (start + timedelta(days=int(after.group(1)))).isoformat()
        between = re.search(
            r"days?\s+between\s+(\d{4}-\d{2}-\d{2})\s+and\s+(\d{4}-\d{2}-\d{2})",
            question,
            re.I,
        )
        if between:
            left = date.fromisoformat(between.group(1))
            right = date.fromisoformat(between.group(2))
            return str(abs((right - left).days))
    except ValueError:
        return None
    return None


def _canon_unit(raw: str) -> str:
    token = raw.strip().lower().replace("°", "")
    return _UNIT_ALIASES.get(token, token)


def _unit_answer(question: str) -> str | None:
    pattern = re.compile(
        r"(\d+(?:\.\d+)?)\s*([A-Za-z°]+)\s*(?:to|in|into)\s*([A-Za-z°]+)",
        re.I,
    )
    match = pattern.search(question)
    if not match:
        how_many = re.search(
            r"how many\s+([A-Za-z°]+)\s+(?:are\s+)?in\s+(\d+(?:\.\d+)?)\s*([A-Za-z°]+)",
            question,
            re.I,
        )
        if not how_many:
            return None
        amount = float(how_many.group(2))
        src = _canon_unit(how_many.group(3))
        dst = _canon_unit(how_many.group(1))
    else:
        amount = float(match.group(1))
        src = _canon_unit(match.group(2))
        dst = _canon_unit(match.group(3))
    if src in {"c", "f"} or dst in {"c", "f"}:
        if src == "c" and dst == "f":
            value = amount * 9.0 / 5.0 + 32.0
        elif src == "f" and dst == "c":
            value = (amount - 32.0) * 5.0 / 9.0
        elif src == dst:
            value = amount
        else:
            return None
        return _format_number(value)
    for table in (_LENGTH_TO_M, _MASS_TO_G, _TIME_TO_S, _BYTES):
        if src in table and dst in table:
            value = amount * table[src] / table[dst]
            return _format_number(value)
    return None


def classify(question: str) -> str:
    if _date_answer(question) is not None:
        return "date"
    if _unit_answer(question) is not None:
        return "unit"
    if re.search(r"\b(write|implement)\b.{0,40}\bfunction\b|\bdef\s+[A-Za-z_]", question, re.I):
        return "code"
    if extract_expression(question) or word_problem_expression(question):
        return "math"
    if re.search(r"\b(plan|design|architecture)\b", question, re.I):
        return "planning"
    return "other"


def try_exact_tool(question: str) -> dict[str, Any] | None:
    """Solve a question with a tool, or return None when no exact tool applies."""
    kind = classify(question)
    if kind == "date":
        answer = _date_answer(question)
        if answer is None or _date_answer(question) != answer:
            return None
        return _tool_result(question, "date", answer, "datetime", f"date math -> {answer}")
    if kind == "unit":
        answer = _unit_answer(question)
        if answer is None or _unit_answer(question) != answer:
            return None
        return _tool_result(question, "unit", answer, "unit-table", f"unit conversion -> {answer}")
    if kind == "math":
        expr = extract_expression(question)
        if expr:
            try:
                value, backend = exact_arithmetic(expr)
                again, _backend = exact_arithmetic(expr)
            except (ValueError, ZeroDivisionError, OverflowError):
                return None
            if value != again:
                return None
            return _tool_result(question, "math", value, backend, f"{expr} = {value}")
        word = word_problem_expression(question)
        if word and re.fullmatch(r"-?\d+", word):
            return _tool_result(question, "math", word, "word-template", f"template -> {word}")
    return None


def _tool_result(question: str, kind: str, answer: str, backend: str, detail: str) -> dict[str, Any]:
    step = {
        "tool": kind,
        "status": "verified",
        "input": question[:500],
        "output": answer,
        "backend": backend,
    }
    return {
        "answer": answer,
        "abstained": False,
        "problem_type": kind,
        "steps": [step],
        "tool_calls": [step],
        "citations": [{"source": f"tool:{kind}", "detail": detail}],
        "confidence": 1.0,
        "backend": backend,
        "model_calls": 0,
        "verified": True,
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


def _numbers_in(text: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?", text)


def unsupported_numbers(answer: str, sources: list[dict[str, Any]]) -> list[str]:
    blob = "\n".join(
        f"{item.get('claim', '')}\n{item.get('evidence', '')}\n{item.get('title', '')}" for item in sources
    )
    missing = []
    for number in _numbers_in(answer):
        if number not in blob and number.lstrip("0") not in blob:
            missing.append(number)
    return missing


def _default_model(prompt: str) -> str:
    from scripts.ollama_utils import chat_with_fallback

    response = chat_with_fallback(
        [{"role": "user", "content": prompt}],
        temperature=0.1,
        options={"num_predict": 256},
    )
    if isinstance(response, dict):
        return str((response.get("message") or {}).get("content") or "")
    return str(response or "")


def _call_model(model_fn: Callable[[str], str] | None, prompt: str, state: dict[str, Any]) -> str:
    budget = state["budget"]
    if state["model_calls"] >= int(budget["max_model_calls"]):
        raise RuntimeError("model call budget is spent")
    if time.perf_counter() - state["started"] > float(budget["timeout_s"]):
        raise RuntimeError("reasoning timeout")
    fn = model_fn
    if fn is None:
        if not state.get("allow_default_model"):
            state["errors"].append("no model configured")
            return ""
        fn = _default_model
    state["model_calls"] += 1
    try:
        return str(fn(prompt) or "")
    except Exception as exc:
        state["errors"].append(str(exc))
        return ""


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


def reason(
    question: str,
    *,
    profile: str | None = None,
    model_fn: Callable[[str], str] | None = None,
    retriever: Callable[[str], list[dict[str, Any]]] | None = None,
    tests: str | None = None,
    atoms_dir: str | None = None,
    delegate_fn: Callable[[], list[dict[str, Any]]] | None = None,
    use_default_model: bool = False,
) -> dict[str, Any]:
    """Run one question. Profile defaults to deep. A delegate is the existing chat path."""
    started = time.perf_counter()
    budget = budget_for(profile)
    state: dict[str, Any] = {
        "budget": budget,
        "model_calls": 0,
        "started": started,
        "errors": [],
        "allow_default_model": bool(use_default_model),
    }
    text = str(question or "").strip()
    if not text:
        raise ValueError("question is empty")
    solved = try_exact_tool(text)
    if solved is not None and not tests:
        return _finish(solved, budget, state, started)
    kind = "code" if tests else classify(text)
    if tests:
        return _finish(_solve_code(text, tests, model_fn if model_fn or use_default_model else model_fn, state), budget, state, started)
    if delegate_fn is not None and kind in {"other", "planning", "code"}:
        events = list(delegate_fn())
        trace = {
            "answer": _answer_from_events(events),
            "abstained": any(event.get("type") == "abstain" for event in events),
            "problem_type": kind,
            "steps": [{"tool": "grounded_chat", "status": "delegated", "input": text[:500]}],
            "tool_calls": [],
            "citations": _citations_from_events(events),
            "confidence": 0.5,
            "backend": None,
            "verified": False,
            "events": events,
            "delegated": True,
        }
        return _finish(trace, budget, state, started)
    sources = _retrieve(text, retriever, atoms_dir, state)
    plan = _model_plan(text, model_fn if (model_fn or use_default_model) else None, state) if (model_fn or use_default_model) else _heuristic_plan(kind)
    if not (model_fn or use_default_model):
        abstained = {
            "answer": ABSTAIN,
            "abstained": True,
            "problem_type": kind,
            "steps": plan.get("steps") or [],
            "tool_calls": [],
            "citations": [_citation(item) for item in sources],
            "confidence": 0.0,
            "backend": None,
            "verified": False,
            "plan": plan,
        }
        return _finish(abstained, budget, state, started)
    source_block = "\n".join(f"[{item.get('atom_id', 'S')}] {item.get('claim', '')}" for item in sources) or "(no retrieved memory)"
    prompt = (
        "Answer in one or two sentences using only the memory below. "
        "Do not add a number that is not in the memory. If the memory is not enough, say you cannot verify it.\n"
        f"Memory:\n{source_block[:4000]}\nQuestion: {text[:1500]}"
    )
    draft = _call_model(model_fn, prompt, state)
    missing = unsupported_numbers(draft, sources) if sources else _numbers_in(draft)
    retries = 0
    while missing and retries < int(budget["max_retries"]) and state["model_calls"] < int(budget["max_model_calls"]):
        retries += 1
        draft = _call_model(
            model_fn,
            prompt + f"\nThe previous answer used numbers that are not in memory: {', '.join(missing)}. Fix it or say you cannot verify it.",
            state,
        )
        missing = unsupported_numbers(draft, sources) if sources else _numbers_in(draft)
    if not draft.strip() or missing or "cannot verify" in draft.lower():
        body = {
            "answer": ABSTAIN,
            "abstained": True,
            "problem_type": plan.get("problem_type") or kind,
            "steps": [{"tool": "retrieve", "status": "done", "output": str(len(sources))}, {"tool": "model", "status": "rejected"}],
            "tool_calls": [],
            "citations": [_citation(item) for item in sources],
            "confidence": 0.0,
            "backend": None,
            "verified": False,
            "plan": plan,
        }
        return _finish(body, budget, state, started)
    body = {
        "answer": draft.strip(),
        "abstained": False,
        "problem_type": plan.get("problem_type") or kind,
        "steps": [{"tool": "retrieve", "status": "done", "output": str(len(sources))}, {"tool": "model", "status": "verified"}],
        "tool_calls": [],
        "citations": [_citation(item) for item in sources],
        "confidence": 0.6 if sources else 0.3,
        "backend": None,
        "verified": bool(sources),
        "plan": plan,
    }
    return _finish(body, budget, state, started)


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
            rows = retrieve_atoms(question, atoms_dir=atoms_dir)
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


def retrieve_atoms(question: str, *, atoms_dir: str, limit: int = 5) -> list[dict[str, Any]]:
    from scripts.recall import RecallOptions, search_memory

    direct, neighbors = search_memory(
        question,
        RecallOptions(n_results=limit, include_statuses={"accepted"}, retrieval_mode="lexical"),
        atoms_dir=atoms_dir,
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


def _finish(body: dict[str, Any], budget: dict[str, Any], state: dict[str, Any], started: float) -> dict[str, Any]:
    elapsed = round((time.perf_counter() - started) * 1000.0, 2)
    return {
        **body,
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
        "latency_note": load_config()["latency_note"],
    }


def load_benchmark() -> dict[str, Any]:
    return json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))


def score_answer(expected: str, answer: str) -> bool:
    return expected.casefold() in str(answer or "").casefold()


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
            "runner": "engine",
            "model": model_name,
            "correct": (not result["abstained"]) and score_answer(item["answer"], result["answer"]),
            "abstained": result["abstained"],
            "answer": str(result["answer"])[:300],
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
