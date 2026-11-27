# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Run the pull-request engine in its own interpreter.

The parent scores the JSON this process prints. Nothing here is the gate.
Off mode installs an import guard before the engine module loads.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import traceback
from pathlib import Path


class _SympyBlock:
    """Reject sympy imports. Installed before the engine module is executed."""

    _blocks_sympy = True

    def find_spec(self, fullname, path, target=None):
        if fullname == "sympy" or fullname.startswith("sympy."):
            raise ModuleNotFoundError("sympy is blocked in off mode")
        return None


def _install_sympy_block() -> None:
    for name in list(sys.modules):
        if name == "sympy" or name.startswith("sympy."):
            del sys.modules[name]
    sys.meta_path.insert(0, _SympyBlock())


def _sympy_blocked() -> bool:
    try:
        importlib.import_module("sympy")
    except ModuleNotFoundError:
        return True
    return False


def _load_engine(engine_root: Path):
    root = str(engine_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    path = engine_root / "scripts" / "reason_engine.py"
    spec = importlib.util.spec_from_file_location("scripts.reason_engine", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["scripts.reason_engine"] = module
    spec.loader.exec_module(module)
    return module


def _mock_reply(responses: dict, item_id: str, tags: list, prompt: str) -> str:
    if "Return one JSON object" in prompt and ("plan-step-verified" in tags or "plan-not-executed" in tags):
        return '{"problem_type":"fact","steps":[{"tool":"calculator","input":"12*8+5"}]}'
    if "Return one JSON object" in prompt:
        return '{"problem_type":"other","steps":[{"tool":"model","input":"answer from memory"}]}'
    if item_id in responses:
        return str(responses[item_id])
    for key, value in responses.items():
        if key and key not in {"id"} and key in prompt:
            return str(value)
    return "ANSWER: UNKNOWN"


def _copy_result(row: dict, result: dict) -> None:
    if not isinstance(result, dict):
        row["ok"] = False
        row["exception"] = "engine returned a non-dict"
        return
    for key in (
        "answer",
        "answer_source",
        "verified",
        "abstained",
        "backend",
        "model_calls",
        "contract_version",
        "delegated",
        "steps",
        "plan",
    ):
        if key in result:
            row[key] = result.get(key)


def _one(engine, item: dict) -> dict:
    item_id = str(item["id"])
    mode = str(item.get("sympy") or "natural")
    kind = str(item.get("kind") or "tool")
    runner = str(item.get("runner") or "")
    question = str(item.get("question") or "")
    row = {"id": item_id, "sympy": mode, "kind": kind, "runner": runner, "ok": True}
    if kind == "hijack":
        try:
            row["classify"] = engine.classify(question)
        except BaseException as exc:
            row["crash"] = f"classify {type(exc).__name__}: {exc}"
            row["classify"] = "error"
        try:
            result = engine.try_exact_tool(question)
        except BaseException as exc:
            row["crash"] = f"try_exact_tool {type(exc).__name__}: {exc}"
            return row
        if isinstance(result, dict) and result.get("refused"):
            row["refused"] = True
            return row
        if result is not None:
            row["fired"] = True
            row["answer"] = result.get("answer")
            row["backend"] = result.get("backend")
        return row
    calls = {"n": 0}

    def _model(prompt: str) -> str:
        calls["n"] += 1
        return _mock_reply(item.get("responses") or {}, item_id, list(item.get("tags") or []), prompt)

    try:
        if kind == "mock" and item.get("disable_tools"):
            engine.try_exact_tool = lambda *_args, **_kwargs: None
        result = engine.reason(
            question,
            profile="fast" if runner == "fast" else "deep",
            model_fn=_model if kind == "mock" and runner != "tools_only" else None,
            use_default_model=False,
            retriever=None,
            tests=item.get("tests"),
        )
    except BaseException as exc:
        row["ok"] = False
        row["exception"] = f"{type(exc).__name__}: {exc}"
        return row
    _copy_result(row, result)
    if kind == "mock" and runner != "tools_only":
        row["model_calls"] = calls["n"]
    return row


def main() -> int:
    request = json.loads(sys.stdin.read())
    if request.get("block_sympy"):
        _install_sympy_block()
    engine_root = Path(request["engine_root"])
    try:
        engine = _load_engine(engine_root)
    except BaseException:
        blocked = _sympy_blocked() if request.get("block_sympy") else False
        sys.stdout.write(json.dumps({
            "ok": False,
            "sympy_blocked": blocked,
            "error": traceback.format_exc(),
        }))
        return 0
    results = []
    for item in request.get("items") or []:
        try:
            results.append(_one(engine, item))
        except BaseException:
            results.append({
                "id": str(item.get("id")),
                "sympy": str(item.get("sympy") or "natural"),
                "kind": str(item.get("kind") or "tool"),
                "runner": str(item.get("runner") or ""),
                "ok": False,
                "exception": traceback.format_exc().splitlines()[-1],
            })
    payload = {"ok": True, "results": results}
    if request.get("block_sympy"):
        payload["sympy_blocked"] = _sympy_blocked()
    sys.stdout.write(json.dumps(payload, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
