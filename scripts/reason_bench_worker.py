# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Run the pull-request engine in its own interpreter.

The parent scores the JSON this process prints. Nothing here is the gate.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import traceback
from pathlib import Path


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


def _without_sympy(engine, fn):
    def _boom(*_args, **_kwargs):
        raise ImportError("sympy forced off")

    original = engine._sympy_value
    engine._sympy_value = _boom
    try:
        return fn()
    finally:
        engine._sympy_value = original


def _one(engine, item: dict) -> dict:
    item_id = str(item["id"])
    mode = str(item.get("sympy") or "natural")
    kind = str(item.get("kind") or "tool")
    question = str(item.get("question") or "")
    row = {"id": item_id, "sympy": mode, "kind": kind, "ok": True}

    def run() -> None:
        if kind == "hijack":
            try:
                classify = engine.classify(question)
            except BaseException as exc:
                row["crash"] = f"classify {type(exc).__name__}: {exc}"
                classify = "error"
            row["classify"] = classify
            try:
                result = engine.try_exact_tool(question)
            except BaseException as exc:
                row["crash"] = f"try_exact_tool {type(exc).__name__}: {exc}"
                return
            if isinstance(result, dict) and result.get("refused"):
                row["refused"] = True
                return
            if result is not None:
                row["fired"] = True
                row["answer"] = result.get("answer")
                row["backend"] = result.get("backend")
            return
        try:
            result = engine.reason(
                question,
                profile="deep",
                model_fn=None,
                use_default_model=False,
                retriever=None,
                tests=item.get("tests"),
            )
        except BaseException as exc:
            row["ok"] = False
            row["exception"] = f"{type(exc).__name__}: {exc}"
            return
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

    if mode == "off":
        _without_sympy(engine, run)
    else:
        run()
    return row


def main() -> int:
    raw = sys.stdin.read()
    request = json.loads(raw)
    engine_root = Path(request["engine_root"])
    try:
        engine = _load_engine(engine_root)
    except BaseException:
        sys.stdout.write(json.dumps({"ok": False, "error": traceback.format_exc()}))
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
                "ok": False,
                "exception": traceback.format_exc().splitlines()[-1],
            })
    sys.stdout.write(json.dumps({"ok": True, "results": results}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
