# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Reasoning benchmark harness.

Model-free gates, dev suites, and the raw model runners. The engine is called,
never modified. Held-out plaintext is never written.
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
import hashlib
import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from scripts.reason_bench_scoring import (
    accuracy_interval,
    design_effect,
    extract_answer,
    holm_adjust,
    mcnemar_exact,
    paired_cluster_delta,
    score_prediction,
    type_normalized_equal,
    wilson_interval,
)


HARNESS_VERSION = "2.2.0"
SUITE_VERSION = "2.2.0"
BOOTSTRAP_SEED = 20261127
FORMAT_SUFFIX = (
    "Finish with a final line in exactly this form: ANSWER: <answer>\n"
    "If the information is not enough to answer, finish with: ANSWER: UNKNOWN\n"
)
NUM_PREDICT = {"math": 384, "logic": 384, "multihop": 256, "code": 512, "tools": 384, "traps": 384}
ENGINE_TOOL_BACKENDS = {"sympy", "arithmetic", "datetime", "unit-table", "word-template"}
ANSWER_SOURCES = {
    "tool_exact",
    "model_verified_tests",
    "model_claimed_verified",
    "model_unverified",
    "delegated",
    "abstained",
}
SMOKE_IDS = (
    "math-dev-t002-0",
    "math-dev-t001-0",
    "multihop-dev-m03-0",
    "multihop-dev-m01-0",
    "code-dev-c01-0",
    "code-dev-c02-0",
    "logic-dev-l01-0",
    "logic-dev-l03-0",
)
B3_FIRED = {
    "hijack-b3-04",
    "hijack-b3-05",
    "hijack-b3-06",
    "hijack-b3-07",
    "hijack-b3-08",
    "hijack-b3-09",
}
FAIRNESS_NOTES = (
    "Deep's memory-path draft prompt asks for one or two sentences, while plain uses chain of thought. "
    "The harness appends the same format suffix to every runner, but deep's internal prompts are not chain of thought. "
    "Any deep − plain gap includes that prompt difference.",
    "The sha256 of each runner's prompt templates is recorded on the run.",
    "Per runner, the share of correct answers that came from fallback extraction is reported. "
    "A runner that wins mainly through fallback extraction is flagged.",
)
EXTERNAL_SETS = {
    "gsm8k-test": {"repo": "openai/gsm8k", "licence": "mit", "contaminated": True},
    "gsm-plus": {"repo": "qintongli/GSM-Plus", "licence": "cc-by-sa-4.0", "contaminated": True},
    "gsm-symbolic": {"repo": "apple/GSM-Symbolic", "licence": "cc-by-nc-nd-4.0", "contaminated": True},
}


class BenchError(RuntimeError):
    """Config, data, hash, isolation, identity, or contract error (exit 2)."""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def bench_root(root: Path | None = None) -> Path:
    return (root or repo_root()) / "bench" / "reasoning"


def require_source_checkout(root: Path | None = None) -> Path:
    root = root or repo_root()
    if not (root / "bench" / "reasoning" / "MANIFEST.json").is_file() and not (root / "pyproject.toml").is_file():
        raise BenchError("reason-bench needs a source checkout")
    if not (root / "pyproject.toml").is_file() or not (root / "bench" / "reasoning").is_dir():
        raise BenchError("reason-bench needs a source checkout")
    return root


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def canonical_bytes(path: Path) -> bytes:
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        rows = []
        text = raw.decode("utf-8-sig")
        for line in text.splitlines():
            if line.strip():
                rows.append(json.loads(line))
        rows.sort(key=lambda row: str(row.get("id", "")))
        body = "\n".join(canonical_json(row) for row in rows)
        return (body + ("\n" if body else "")).encode("utf-8")
    if suffix == ".json":
        return canonical_json(json.loads(raw.decode("utf-8-sig"))).encode("utf-8")
    if suffix in {".md", ".py", ".txt", ".ps1", ".sh"} or path.name in {"MANIFEST.json"}:
        text = raw.decode("utf-8-sig")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.rstrip() for line in text.split("\n")]
        return ("\n".join(lines)).encode("utf-8")
    text = raw.decode("utf-8-sig")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    return ("\n".join(lines)).encode("utf-8")


def file_hash(path: Path) -> str:
    return hashlib.sha256(canonical_bytes(path)).hexdigest()


def directory_hash(root: Path) -> str:
    rows = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        rows.append(f"{rel}\0{file_hash(path)}\n")
    return hashlib.sha256("".join(rows).encode("utf-8")).hexdigest()


def iter_public_files(root: Path) -> Iterator[Path]:
    base = bench_root(root)
    skip_dirs = {"results", "cache", "__pycache__"}
    for path in sorted(p for p in base.rglob("*") if p.is_file()):
        rel_parts = path.relative_to(base).parts
        if any(part in skip_dirs or part.endswith(".pyc") for part in rel_parts):
            continue
        if path.name == "MANIFEST.json":
            continue
        if path.name == ".gitkeep":
            continue
        yield path


def public_rel(root: Path, path: Path) -> str:
    return path.relative_to(bench_root(root)).as_posix()


def compute_manifest(root: Path, *, suite_version: str = SUITE_VERSION) -> dict[str, Any]:
    files = {public_rel(root, path): file_hash(path) for path in iter_public_files(root)}
    counts, template_counts = suite_counts(root)
    corpus = bench_root(root) / "corpus" / "dev"
    return {
        "suite_version": suite_version,
        "harness_version": HARNESS_VERSION,
        "files": files,
        "counts": counts,
        "template_counts": template_counts,
        "corpus_hash": directory_hash(corpus) if corpus.is_dir() else None,
        "generator_versions": {"harness": HARNESS_VERSION, "template_api": 1},
    }


def load_manifest(root: Path) -> dict[str, Any]:
    path = bench_root(root) / "MANIFEST.json"
    if not path.is_file():
        raise BenchError("MANIFEST.json is missing")
    return json.loads(path.read_text(encoding="utf-8"))


def check_manifest(root: Path) -> None:
    recorded = load_manifest(root)
    current = compute_manifest(root, suite_version=str(recorded.get("suite_version") or SUITE_VERSION))
    if recorded.get("files") != current["files"]:
        missing = sorted(set(current["files"]) ^ set(recorded.get("files") or {}))
        changed = [key for key in current["files"] if recorded.get("files", {}).get(key) != current["files"][key]]
        raise BenchError(f"manifest hash mismatch (changed={changed[:8]} symmetric={missing[:8]})")
    if recorded.get("counts") != current["counts"]:
        raise BenchError(f"manifest counts mismatch: {current['counts']}")
    if recorded.get("corpus_hash") != current["corpus_hash"]:
        raise BenchError("manifest corpus_hash mismatch")


def write_manifest(root: Path, *, suite_version: str, reason: str) -> dict[str, Any]:
    payload = compute_manifest(root, suite_version=suite_version)
    payload["lock_reason"] = reason
    path = bench_root(root) / "MANIFEST.json"
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8", newline="\n")
    return payload


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise BenchError(f"{path}:{line_no} is not JSON") from exc
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    path.write_text(body, encoding="utf-8", newline="\n")


def suite_counts(root: Path) -> tuple[dict[str, int], dict[str, int]]:
    counts: dict[str, int] = {}
    templates: dict[str, set[str]] = defaultdict(set)
    suites = bench_root(root) / "suites"
    if suites.is_dir():
        for path in sorted(suites.glob("*.jsonl")):
            rows = read_jsonl(path)
            counts[path.name] = len(rows)
            for row in rows:
                suite = str(row.get("suite") or path.stem.split(".")[0])
                if row.get("template_id"):
                    templates[suite].add(str(row["template_id"]))
    return counts, {key: len(value) for key, value in sorted(templates.items())}


def load_suite_items(root: Path) -> dict[str, list[dict]]:
    suites = bench_root(root) / "suites"
    loaded = {}
    for path in sorted(suites.glob("*.jsonl")):
        loaded[path.name] = read_jsonl(path)
    return loaded


def dev_items(root: Path) -> list[dict]:
    items = []
    for name in ("math.dev.jsonl", "multihop.dev.jsonl", "code.dev.jsonl", "logic.dev.jsonl"):
        items.extend(read_jsonl(bench_root(root) / "suites" / name))
    return items


def core_items(items: list[dict]) -> list[dict]:
    return [item for item in items if int(item.get("instance") or 0) == 0]


# --- templates and generation ---------------------------------------------------


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"bench_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise BenchError(f"cannot load template {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def iter_templates(root: Path, kind: str) -> list[Any]:
    folder = bench_root(root) / "templates" / kind
    if not folder.is_dir():
        return []
    modules = []
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        modules.append(_load_module(path))
    return modules


def materialize_module(module: Any) -> list[dict]:
    if not hasattr(module, "build_item"):
        raise BenchError(f"template {getattr(module, 'TEMPLATE_ID', module)} has no build_item")
    count = int(getattr(module, "INSTANCES", 2))
    return [module.build_item(instance) for instance in range(count)]


def generate_suite(root: Path, kind: str) -> list[dict]:
    rows: list[dict] = []
    for module in iter_templates(root, kind):
        rows.extend(materialize_module(module))
    rows.sort(key=lambda row: str(row["id"]))
    return rows


def _provenance(source: str = "original", *, derived_from: str | None = None, seed: int | None = None) -> dict[str, Any]:
    return {
        "source": source,
        "derived_from": derived_from,
        "license": "CC0-1.0",
        "author": "grok-bot",
        "created": "2026-11-27",
        "llm_assisted": True,
        "seed": seed,
    }


def build_hijack_items() -> list[dict]:
    """220 ordinary sentences. The 12 B3 sentences are verbatim."""
    hand = [
        ("hijack-b3-01", "phone", "Call me on 021 555 0199 after lunch."),
        ("hijack-b3-02", "dates-times", "The release moved from 2026-11-27 to 2026-12-04."),
        ("hijack-b3-03", "versions", "We need Python 3.11 or 3.12 for this build."),
        ("hijack-b3-04", "numeric-ranges", "Read pages 10-20 before the meeting."),
        ("hijack-b3-05", "recipe-fractions", "Add 1/2 cup of sugar and stir."),
        ("hijack-b3-06", "n-times", "She rang 3 times 2 days in a row."),
        ("hijack-b3-07", "boxes-narrative", "We stacked 4 boxes of 12 books in the hall and then 2 more friends arrived."),
        ("hijack-b3-08", "dates-times", "Kickoff is at 9:30 on 3/4 according to the invite."),
        ("hijack-b3-09", "scores-ratios", "My score went from 7-3 to 8-3."),
        ("hijack-b3-10", "ids", "Server 10.0.0.12 is down."),
        ("hijack-b3-11", "dates-times", "Convert the doc to PDF before 5 pm."),
        ("hijack-b3-12", "ids", "Ticket SB-1042 is blocked by SB-1040."),
        ("hijack-h013", "phone", "Fax 09-377 1234."),
        ("hijack-h014", "phone", "Ring the depot on 04-499 0188 before you leave."),
        ("hijack-h015", "numeric-ranges", "Ages 5–12 welcome."),
        ("hijack-h016", "versions", "Bump to v2.10.3-rc1."),
        ("hijack-h017", "scores-ratios", "Mix 2:1 with water."),
        ("hijack-h018", "recipe-fractions", "Use 3/4 tsp salt."),
        ("hijack-h019", "n-times", "I told him 5 times today."),
        ("hijack-h020", "ids", "Ship to 1010 Auckland."),
        ("hijack-h021", "money", "Lunch was $12.50 and the tip was 10%."),
        ("hijack-h022", "units-prose", "The trail is 3 km and took 2 h."),
        ("hijack-h023", "phone", "Text 027 555 0142 when the ferry docks."),
        ("hijack-h024", "phone", "The after-hours line is 0800 555 019."),
        ("hijack-h025", "dates-times", "Doors open at 7 pm on Friday."),
        ("hijack-h026", "dates-times", "The workshop runs from Monday to Wednesday."),
        ("hijack-h027", "versions", "Stay on Ubuntu 24.04 for this image."),
        ("hijack-h028", "versions", "The schema is still v1."),
        ("hijack-h029", "numeric-ranges", "See rows 3-8 in the sheet."),
        ("hijack-h030", "numeric-ranges", "Chapters 2-4 cover the setup."),
        ("hijack-h031", "scores-ratios", "The vote finished 12-9."),
        ("hijack-h032", "scores-ratios", "Dilute the syrup 1:4."),
        ("hijack-h033", "recipe-fractions", "Stir in 1/4 cup of milk."),
        ("hijack-h034", "recipe-fractions", "Add 2/3 cup of oats."),
        ("hijack-h035", "n-times", "We met 4 times 3 weeks apart."),
        ("hijack-h036", "n-times", "The bell rang 6 times 2 minutes apart."),
        ("hijack-h037", "boxes-narrative", "They left 3 boxes of 8 mugs by the door and 1 more chair."),
        ("hijack-h038", "boxes-narrative", "I bought 2 packs of 6 rolls for the picnic."),
        ("hijack-h039", "ids", "Ticket SB-1100 blocks SB-1099."),
        ("hijack-h040", "ids", "Post the parcel to 6011 Wellington."),
        ("hijack-h041", "money", "The fare was $4.20 each way."),
        ("hijack-h042", "money", "Coffee was $5 and the muffin was 15% off."),
        ("hijack-h043", "units-prose", "The desk is 120 cm wide and 70 cm deep."),
        ("hijack-h044", "units-prose", "Each bottle holds 750 ml."),
        ("hijack-h045", "phone", "Ask reception for extension 214."),
        ("hijack-h046", "dates-times", "The backup window is 01:00 to 03:00."),
        ("hijack-h047", "versions", "Node 20 is what the container expects."),
        ("hijack-h048", "numeric-ranges", "Questions 15-18 are optional."),
        ("hijack-h049", "scores-ratios", "The set ended 6-4."),
        ("hijack-h050", "recipe-fractions", "Whisk 1/8 tsp of yeast into the water."),
        ("hijack-h051", "n-times", "She checked 2 times 4 hours apart."),
        ("hijack-h052", "boxes-narrative", "We carried 5 groups of 4 chairs into the hall."),
        ("hijack-h053", "ids", "Host app-03.internal failed its health check."),
        ("hijack-h054", "money", "The refund was $18.00."),
        ("hijack-h055", "units-prose", "The drive took 45 min."),
        ("hijack-h056", "phone", "Leave a message on 09 555 0177."),
        ("hijack-h057", "dates-times", "Submit the form by 2026-11-30."),
        ("hijack-h058", "versions", "The lockfile pins ruff 0.8."),
        ("hijack-h059", "numeric-ranges", "Use sizes 8-14 from the sample rack."),
        ("hijack-h060", "ids", "Reference INV-2044 matches PO-2040."),
    ]
    if len(hand) != 60:
        raise BenchError(f"hand-written hijack sentences must be 60, got {len(hand)}")
    quotas = {
        "phone": 20,
        "dates-times": 30,
        "versions": 20,
        "numeric-ranges": 20,
        "scores-ratios": 15,
        "recipe-fractions": 20,
        "n-times": 20,
        "boxes-narrative": 20,
        "ids": 25,
        "money": 15,
        "units-prose": 15,
    }
    rows = []
    seen = set()
    counts: dict[str, int] = Counter()
    for item_id, category, text in hand:
        if text in seen:
            raise BenchError(f"duplicate hijack text {text!r}")
        seen.add(text)
        counts[category] += 1
        rows.append(_hijack_row(item_id, category, text, source="original"))
    rng = random.Random(20261127)
    serial = 1
    names = ["Ana", "Mere", "Sam", "Jo", "Kai", "Noa"]
    while sum(counts.values()) < 220:
        progress = False
        for category, quota in quotas.items():
            if counts[category] >= quota:
                continue
            text = _generated_hijack(category, rng, names, serial)
            serial += 1
            if text in seen:
                continue
            seen.add(text)
            counts[category] += 1
            rows.append(_hijack_row(f"hijack-{len(rows)+1:04d}", category, text, source="template", seed=20261127))
            progress = True
            if sum(counts.values()) >= 220:
                break
        if not progress:
            raise BenchError(f"hijack generator stalled counts={dict(counts)} serial={serial}")
    if counts != Counter(quotas) or len(rows) != 220:
        raise BenchError(f"hijack composition drifted: {dict(counts)} n={len(rows)}")
    return rows


def _hijack_row(item_id: str, category: str, text: str, *, source: str, seed: int | None = None) -> dict[str, Any]:
    return {
        "id": item_id,
        "template_id": item_id,
        "instance": 0,
        "suite": "hijack",
        "split": "dev",
        "category": category,
        "text": text,
        "question": text,
        "expect": "no_tool",
        "answer": None,
        "answer_type": "unanswerable",
        "approx_ok": None,
        "answerable": False,
        "difficulty": 1,
        "tags": ["hijack", category],
        "expected_tool": None,
        "provenance": _provenance(source, seed=seed),
        "reference": "ANSWER = None",
    }


def _generated_hijack(category: str, rng: random.Random, names: list[str], serial: int) -> str:
    name = names[serial % len(names)]
    a = 2 + (serial % 17)
    b = 3 + (serial % 11)
    # serial is part of each sentence so a filled category cannot stall on a repeated draw.
    if category == "phone":
        return f"Call {name} on 021 {540 + serial % 400:03d} {1000 + (serial * 17) % 8000:04d} after stand-up {serial}."
    if category == "dates-times":
        month = 1 + (serial % 12)
        day = 1 + ((serial // 12) % 27)
        return f"The {name} review {serial} moved from 2026-{month:02d}-{day:02d} to the following Tuesday."
    if category == "versions":
        return f"This {name} service {serial} stays on release {1 + serial % 4}.{serial % 9}.{serial % 12}."
    if category == "numeric-ranges":
        return f"Read pages {a}-{a + b} before the {name} briefing {serial}."
    if category == "scores-ratios":
        return f"The {name} match {serial} finished {a}-{b} after extra time."
    if category == "recipe-fractions":
        return f"Add {a}/{a + b} cup of sugar and stir, {name} said in note {serial}."
    if category == "n-times":
        return f"{name} rang {a} times {b} days in a row during week {serial}."
    if category == "boxes-narrative":
        return f"We stacked {a} boxes of {b} books in the {name} hall {serial} and then 2 more friends arrived."
    if category == "ids":
        return f"Ticket SB-{1000 + serial} is blocked by SB-{1000 + serial - 1}."
    if category == "money":
        return f"{name} paid ${a}.{b:02d} for lunch {serial} and left a 10% tip."
    if category == "units-prose":
        return f"The {name} trail {serial} is {a} km and took {b} h."
    raise BenchError(f"unknown hijack category {category}")


def build_tools_items() -> list[dict]:
    specs = [
        ("tools-0001", "What is 17 * 23 + 9?", "400", "integer", "any", "migrated", "docs/reasoning-benchmarks.json#arith-17-23-9", "ANSWER = 17 * 23 + 9"),
        ("tools-0002", "What date is 14 days after 2026-11-27?", "2026-12-11", "date", "any", "migrated", "docs/reasoning-benchmarks.json#date-14-days", "ANSWER = '2026-12-11'"),
        ("tools-0003", "How many days between 2026-11-01 and 2026-11-27?", "26", "integer", "any", "migrated", "docs/reasoning-benchmarks.json#days-between", "ANSWER = 26"),
        ("tools-0004", "Convert 3 km to m.", "3000", "integer", "any", "migrated", "docs/reasoning-benchmarks.json#unit-km", "ANSWER = 3000"),
        ("tools-0005", "Convert 100 C to F.", "212", "integer", "any", "migrated", "docs/reasoning-benchmarks.json#unit-celsius", "ANSWER = 212"),
        ("tools-0007", "A shop sold 12 boxes of 8 pens and then 5 more pens. How many pens were sold?", "101", "integer", "any", "migrated", "docs/reasoning-benchmarks.json#gsm-pens", "ANSWER = 12 * 8 + 5"),
        ("tools-0012", "What is 1/3 + 1/6?", "1/2", "fraction", "any", "original", None, "from fractions import Fraction\nANSWER = Fraction(1, 3) + Fraction(1, 6)"),
        ("tools-0013", "What is 2/3?", "2/3", "fraction", "any", "original", None, "from fractions import Fraction\nANSWER = Fraction(2, 3)"),
        ("tools-0014", "What is 5/6?", "5/6", "fraction", "any", "original", None, "from fractions import Fraction\nANSWER = Fraction(5, 6)"),
        ("tools-0015", "What is 1/7?", "1/7", "fraction", "any", "original", None, "from fractions import Fraction\nANSWER = Fraction(1, 7)"),
        ("tools-0016", "What is 1/2?", "1/2", "fraction", "any", "original", None, "from fractions import Fraction\nANSWER = Fraction(1, 2)"),
        ("tools-0017", "What is 3/4?", "3/4", "fraction", "any", "original", None, "from fractions import Fraction\nANSWER = Fraction(3, 4)"),
        ("tools-0018", "What is 1/4 + 1/4?", "1/2", "fraction", "any", "original", None, "from fractions import Fraction\nANSWER = Fraction(1, 2)"),
        ("tools-0019", "Convert 2 kg to g.", "2000", "integer", "any", "original", None, "ANSWER = 2000"),
        ("tools-0020", "Convert 30 min to s.", "1800", "integer", "any", "original", None, "ANSWER = 1800"),
        ("tools-0021", "What date is 7 days after 2026-01-28?", "2026-02-04", "date", "any", "original", None, "ANSWER = '2026-02-04'"),
        ("tools-0022", "How many days between 2024-02-01 and 2024-03-01?", "29", "integer", "any", "original", None, "ANSWER = 29"),
        ("tools-0023", "What does the owner prefer for coffee?", None, "unanswerable", None, "original", None, "ANSWER = None"),
    ]
    rows = []
    for item_id, question, answer, answer_type, expected, source, derived, reference in specs:
        rows.append(_tool_row(item_id, question, answer, answer_type, expected, source, derived, reference, tags=["should-fire"] if expected else ["decline-ok"]))
    # Twenty further should-fire arithmetic controls the current tool can answer exactly.
    extras = [
        (2, 2, "+"), (8, 7, "*"), (40, 15, "-"), (9, 3, "/"), (12, 5, "+"),
        (6, 6, "*"), (100, 37, "-"), (15, 4, "*"), (21, 7, "/"), (13, 8, "+"),
        (50, 25, "-"), (11, 11, "*"), (18, 6, "/"), (7, 9, "+"), (30, 12, "-"),
        (4, 16, "*"), (81, 9, "/"),         (22, 19, "+"), (64, 8, "-"), (3, 14, "*"),
        (16, 4, "+"), (45, 9, "/"), (8, 7, "+"), (19, 6, "-"),
    ]
    for index, (left, right, op) in enumerate(extras, start=24):
        if op == "+":
            value = left + right
        elif op == "-":
            value = left - right
        elif op == "*":
            value = left * right
        else:
            value = left // right
        question = f"What is {left} {op} {right}?"
        rows.append(_tool_row(
            f"tools-{index:04d}",
            question,
            str(value),
            "integer",
            "any",
            "original",
            None,
            f"ANSWER = {value}",
            tags=["should-fire", "arithmetic"],
        ))
    return rows


def _tool_row(item_id, question, answer, answer_type, expected, source, derived, reference, tags) -> dict[str, Any]:
    return {
        "id": item_id,
        "template_id": item_id,
        "instance": 0,
        "suite": "tools",
        "split": "dev",
        "question": question,
        "answer": answer,
        "answer_type": answer_type,
        "approx_ok": None,
        "answerable": answer_type != "unanswerable",
        "difficulty": 1,
        "tags": tags,
        "expected_tool": expected,
        "provenance": _provenance(source, derived_from=derived),
        "reference": reference,
    }


def write_corpus_notes(root: Path, items: list[dict]) -> None:
    notes_dir = bench_root(root) / "corpus" / "dev" / "notes"
    if notes_dir.exists():
        shutil.rmtree(notes_dir)
    notes_dir.mkdir(parents=True, exist_ok=True)
    written = set()
    for item in items:
        for note in item.get("notes") or []:
            note_id = str(note["id"])
            if note_id in written:
                continue
            written.add(note_id)
            claim = str(note["claim"]).strip()
            front = [
                "---",
                f"id: {note_id}",
                "type: atom",
                "memory_type: semantic",
                "review_status: accepted",
                "status: active",
                "contexts: []",
                "claim_state: human_reviewed",
            ]
            if note.get("valid_from"):
                front.append(f"valid_from: {note['valid_from']}")
            if note.get("valid_until"):
                front.append(f"valid_until: {note['valid_until']}")
            front.append("---")
            body = "\n".join(front) + f"\n\n## Claim\n\n{claim}\n"
            (notes_dir / f"{note_id}.md").write_text(body, encoding="utf-8", newline="\n")


def generate(root: Path, kind: str) -> int:
    suites = bench_root(root) / "suites"
    suites.mkdir(parents=True, exist_ok=True)
    if kind in {"math", "multihop", "logic", "code"}:
        rows = generate_suite(root, kind)
        write_jsonl(suites / f"{kind}.dev.jsonl", rows)
        if kind == "multihop":
            write_corpus_notes(root, rows)
        return len(rows)
    if kind == "hijack":
        rows = build_hijack_items()
        write_jsonl(suites / "hijack.jsonl", rows)
        return len(rows)
    if kind == "tools":
        rows = build_tools_items()
        write_jsonl(suites / "tools.jsonl", rows)
        return len(rows)
    if kind == "traps":
        rows = build_trap_index(root)
        write_jsonl(suites / "traps.dev.jsonl", rows)
        return len(rows)
    if kind == "all":
        total = 0
        for name in ("math", "multihop", "logic", "code", "hijack", "tools", "traps"):
            total += generate(root, name)
        return total
    raise BenchError(f"unknown suite {kind}")


def build_trap_index(root: Path) -> list[dict]:
    wanted = {
        "math-dev-t001-0",
        "math-dev-t002-0",
        "math-dev-t003-0",
        "math-dev-t004-1",
        "math-dev-t005-0",
        "math-dev-t006-0",
        "math-dev-t007-0",
        "math-dev-t008-0",
        "math-dev-t009-0",
        "math-dev-t010-0",
        "multihop-dev-m02-0",
        "multihop-dev-m03-0",
        "multihop-dev-m04-0",
        "multihop-dev-m11-0",
        "multihop-dev-m12-0",
        "code-dev-c01-0",
    }
    rows = []
    for item in dev_items(root):
        tags = set(item.get("tags") or [])
        if item["id"] in wanted or "template-trap" in tags and int(item.get("instance") or 0) == 0 and item["suite"] == "math" and item["id"].endswith("-0") and item["template_id"] in {f"t{i:03d}" for i in range(2, 11)}:
            if item["id"] in wanted or item["template_id"] in {"t002", "t007", "t008", "t009", "t010"}:
                rows.append(dict(item))
    # Stable unique set: the named traps only.
    named = []
    seen = set()
    for item in dev_items(root):
        if item["id"] in wanted and item["id"] not in seen:
            named.append(dict(item))
            seen.add(item["id"])
    named.sort(key=lambda row: row["id"])
    return named


# --- validation -----------------------------------------------------------------


REQUIRED_FIELDS = (
    "id", "template_id", "instance", "suite", "split", "question", "answer",
    "answer_type", "approx_ok", "answerable", "difficulty", "tags", "provenance",
)


def validate_items(items: list[dict], *, seen: set[str] | None = None, allow_existing: bool = False) -> set[str]:
    seen = set() if seen is None else seen
    for item in items:
        for field in REQUIRED_FIELDS:
            if field not in item:
                raise BenchError(f"{item.get('id')} missing {field}")
        if item["id"] in seen and not allow_existing:
            raise BenchError(f"duplicate id {item['id']}")
        if allow_existing and item["id"] not in seen:
            raise BenchError(f"trap {item['id']} is not in a parent suite")
        seen.add(item["id"])
        if item["split"] != "dev":
            raise BenchError(f"{item['id']} split must be dev in the repo")
        if item.get("expected_tool") not in {None, "any"}:
            raise BenchError(f"{item['id']} expected_tool must be null or 'any'")
        difficulty = int(item["difficulty"])
        if difficulty not in {1, 2, 3}:
            raise BenchError(f"{item['id']} difficulty out of range")
        if item["suite"] != "code" and "reference" not in item and "solver" not in item:
            raise BenchError(f"{item['id']} needs a reference or solver")


def _numbers_in_text(text: str) -> set[str]:
    import re
    return set(re.findall(r"\d+", text or ""))


def _answer_numbers(value: Any) -> set[str]:
    import re
    if value is None or isinstance(value, bool):
        return set()
    return set(re.findall(r"\d+", str(value)))


def validate_template_module(module: Any) -> None:
    if not hasattr(module, "sample"):
        return
    rng = random.Random(f"validate:{module.TEMPLATE_ID}")
    for _ in range(200):
        params = module.sample(rng)
        first = module.solve(params)
        second = module.solve(params)
        if not type_normalized_equal(first, second):
            raise BenchError(f"{module.TEMPLATE_ID} solve is not deterministic")
        answer_type = str(getattr(module, "ANSWER_TYPE", "integer"))
        if answer_type == "integer" and not isinstance(first, int):
            raise BenchError(f"{module.TEMPLATE_ID} integer solve returned {type(first).__name__}")
        rendered = module.render(params)
        if not getattr(module, "SKIP_NUMBER_CHECK", False):
            missing = []
            for number in _answer_numbers(params):
                if number not in _numbers_in_text(rendered) and number.lstrip("0") not in _numbers_in_text(rendered):
                    missing.append(number)
            if missing:
                raise BenchError(f"{module.TEMPLATE_ID} render omitted numbers {missing}")
        if hasattr(module, "render_missing_info"):
            module.render_missing_info(params)


def validate_reference(item: dict) -> None:
    if item.get("suite") == "code":
        return
    if item.get("solver") and item.get("suite") == "logic":
        _validate_solver(item)
        return
    reference = item.get("reference")
    if not reference:
        raise BenchError(f"{item['id']} missing reference")
    result = _sandbox_answer(reference)
    gold = item.get("answer")
    if gold is None:
        if result not in {None, "None"}:
            raise BenchError(f"{item['id']} reference produced {result!r} for an empty answer")
        return
    if not _reference_matches(result, gold, item):
        raise BenchError(f"{item['id']} reference {result!r} != {gold!r}")


def _sandbox_answer(reference: str) -> Any:
    from scripts.symbolic_sandbox import run_sandboxed_python

    program = reference.rstrip() + "\nresult = ANSWER\n"
    out = run_sandboxed_python(program, timeout_seconds=10)
    if not out.get("ok"):
        raise BenchError(f"reference failed in the sandbox: {out.get('error')}")
    return out.get("result")


def _reference_matches(result: Any, gold: Any, item: dict) -> bool:
    if result is None:
        return gold is None
    predicted = str(result)
    if item.get("answer_type") in {"sequence", "assignment"}:
        try:
            parsed = json.loads(predicted.replace("'", '"'))
        except json.JSONDecodeError:
            parsed = predicted
        return type_normalized_equal(parsed, gold)
    scored = score_prediction(predicted, item, tool=True)
    return bool(scored["correct"])


def _validate_solver(item: dict) -> None:
    from scripts.symbolic_sandbox import run_sandboxed_python

    program = str(item["solver"]).rstrip() + "\nimport json\nresult = json.dumps(SOLUTIONS)\n"
    out = run_sandboxed_python(program, timeout_seconds=10)
    if not out.get("ok"):
        raise BenchError(f"{item['id']} solver failed: {out.get('error')}")
    solutions = json.loads(str(out.get("result") or "[]"))
    if len(solutions) != 1:
        raise BenchError(f"{item['id']} solver produced {len(solutions)} solutions")
    if not type_normalized_equal(solutions[0], item.get("answer")):
        raise BenchError(f"{item['id']} solver solution {solutions[0]!r} != {item.get('answer')!r}")


def validate_code_item(item: dict) -> None:
    from scripts.symbolic_sandbox import run_sandboxed_python

    program = str(item["reference_solution"]).rstrip() + "\n" + str(item.get("visible_tests") or "") + "\n" + str(item.get("hidden_tests") or "") + "\n"
    out = run_sandboxed_python(program, timeout_seconds=float(item.get("timeout_s") or 10))
    if not out.get("ok"):
        raise BenchError(f"{item['id']} reference solution failed hidden tests: {out.get('error')}")


def validate_composition(items_by_file: dict[str, list[dict]]) -> None:
    math = items_by_file.get("math.dev.jsonl") or []
    multi = items_by_file.get("multihop.dev.jsonl") or []
    code = items_by_file.get("code.dev.jsonl") or []
    logic = items_by_file.get("logic.dev.jsonl") or []
    hijack = items_by_file.get("hijack.jsonl") or []
    tools = items_by_file.get("tools.jsonl") or []
    if len({row["template_id"] for row in math}) < 60 or len(math) != 120:
        raise BenchError(f"math composition {len(math)} items")
    if len({row["template_id"] for row in multi}) < 20 or len(multi) != 40:
        raise BenchError("multihop composition")
    if len(code) != 20 or len({row["template_id"] for row in code}) != 20:
        raise BenchError("code composition")
    if len(logic) != 24 or len({row["template_id"] for row in logic}) < 12:
        raise BenchError("logic composition")
    if len(hijack) < 220:
        raise BenchError("hijack suite is below 220")
    if len(tools) < 40:
        raise BenchError("tools suite is below 40")
    trap_templates = {row["template_id"] for row in math if "template-trap" in (row.get("tags") or [])}
    if len(trap_templates) < 10:
        raise BenchError("math needs at least 10 template-trap templates")
    fraction_templates = {row["template_id"] for row in math if "fraction-exact" in (row.get("tags") or [])}
    if len(fraction_templates) < 6:
        raise BenchError("math needs at least 6 fraction-exact templates")
    all_but = {row["template_id"] for row in math if "all-but" in (row.get("tags") or []) or "more-than-half" in (row.get("tags") or [])}
    if len(all_but) < 4:
        raise BenchError("math needs at least 4 all-but or more-than-half templates")
    missing_templates = {row["template_id"] for row in math if "missing-info" in (row.get("tags") or [])}
    missing_items = [row for row in math if "missing-info" in (row.get("tags") or [])]
    if len(missing_templates) < 6 or not (0.08 <= len(missing_items) / len(math) <= 0.12):
        raise BenchError("math missing-information mix is outside the spec")
    distractors = {row["template_id"] for row in math if "distractor-numbers" in (row.get("tags") or [])}
    if len(distractors) < 6:
        raise BenchError("math needs at least 6 distractor-number templates")
    gsm = [row for row in math if str((row.get("provenance") or {}).get("derived_from") or "").startswith("gsm8k")]
    if len({row["template_id"] for row in gsm}) > 10:
        raise BenchError("more than 10 GSM8K-derived math templates")
    unanswerable = [row for row in multi if row.get("answer_type") == "unanswerable" or not row.get("answerable", True)]
    if len(unanswerable) / len(multi) < 0.15 or len(unanswerable) < 6 or len({row["template_id"] for row in unanswerable}) < 3:
        raise BenchError("multihop unanswerable mix is below 15%")
    if len({row["template_id"] for row in multi if "temporal-supersession" in (row.get("tags") or [])}) < 4 and len([row for row in multi if "temporal-supersession" in (row.get("tags") or [])]) < 4:
        raise BenchError("multihop needs at least 4 temporal items")
    if len([row for row in multi if "answer-not-in-notes" in (row.get("tags") or [])]) < 4:
        raise BenchError("multihop needs at least 4 numeric aggregations")
    if len([row for row in multi if "substring-verify-trap" in (row.get("tags") or [])]) < 2:
        raise BenchError("multihop needs at least 2 substring traps")
    if len([row for row in multi if "date-arithmetic" in (row.get("tags") or [])]) < 2:
        raise BenchError("multihop needs at least 2 date-arithmetic items")
    def _tagged(tag: str) -> int:
        return sum(1 for row in code if tag in (row.get("tags") or []))

    if _tagged("string") < 5:
        raise BenchError("code mix needs 5 string items")
    if sum(1 for row in code if "list" in (row.get("tags") or []) or "dict" in (row.get("tags") or [])) < 5:
        raise BenchError("code mix needs 5 list/dict items")
    if _tagged("parsing") < 5:
        raise BenchError("code mix needs 5 parsing items")
    if _tagged("algorithm") < 5:
        raise BenchError("code mix needs 5 algorithmic items")
    should_fire = [row for row in tools if row.get("expected_tool") == "any"]
    if len(should_fire) < 20:
        raise BenchError("tools suite needs at least 20 should-fire controls")


def path_is_forbidden(name: str) -> bool:
    """True when a tracked path would be held-out plaintext."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    lowered = name.replace("\\", "/").casefold()
    if lowered.startswith("bench/reasoning/heldout-results/"):
        return False
    if base in {"heldout.commitment.json", "heldout-access.jsonl"}:
        return False
    if lowered.endswith(".sbheldout") or lowered.endswith(".sbsealed"):
        return True
    if "templates-private/" in lowered or "/corpus/heldout/" in lowered or lowered.startswith("corpus/heldout/"):
        return True
    if "/heldout/" in f"/{lowered}" or lowered.startswith("heldout/"):
        return True
    if "heldout" in base.casefold() and base.casefold().endswith(".jsonl"):
        return True
    return False


def forbidden_tracked_paths(root: Path) -> list[str]:
    try:
        listed = subprocess.check_output(["git", "ls-files"], cwd=root, text=True).splitlines()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise BenchError(f"git ls-files failed: {exc}") from exc
    return [rel for rel in listed if path_is_forbidden(rel)]


def validate(root: Path, *, check_engine: bool = False) -> dict[str, Any]:
    require_source_checkout(root)
    check_manifest(root)
    forbidden = forbidden_tracked_paths(root)
    if forbidden:
        raise BenchError(f"tracked held-out plaintext: {forbidden}")
    loaded = load_suite_items(root)
    seen: set[str] = set()
    for name, rows in loaded.items():
        if name == "traps.dev.jsonl":
            continue
        validate_items(rows, seen=seen)
    validate_items(loaded.get("traps.dev.jsonl") or [], seen=seen, allow_existing=True)
    everything = [row for name, rows in loaded.items() if name != "traps.dev.jsonl" for row in rows]
    validate_composition(loaded)
    for kind in ("math", "multihop", "logic"):
        for module in iter_templates(root, kind):
            validate_template_module(module)
    for item in everything:
        if item.get("suite") == "hijack":
            if item.get("answer") is not None or item.get("expect") != "no_tool":
                raise BenchError(f"{item['id']} hijack item must expect no tool and have no gold answer")
            continue
        if item.get("suite") == "code":
            validate_code_item(item)
        elif item.get("solver"):
            _validate_solver(item)
        else:
            validate_reference(item)
    report = {"items": len(everything), "manifest": "ok"}
    if check_engine:
        report["model_free"] = model_free_report(root)
    return report


# --- engine adapters ------------------------------------------------------------


@contextmanager
def sympy_mode(mode: str):
    if mode == "natural":
        yield
        return
    if mode != "off":
        raise BenchError(f"unknown sympy_mode {mode}")
    from unittest.mock import patch

    with patch("scripts.reason_engine._sympy_value", side_effect=ImportError("sympy forced off")):
        yield


def sympy_status() -> dict[str, Any]:
    try:
        import sympy
    except ImportError:
        return {"sympy_available": False, "sympy_version": None}
    return {"sympy_available": True, "sympy_version": sympy.__version__}


def derive_answer_source(result: dict) -> tuple[str, str]:
    version = int(result.get("contract_version") or 0)
    if version >= 2:
        source = result.get("answer_source")
        if source not in ANSWER_SOURCES:
            raise BenchError("contract_version >= 2 but answer_source is missing")
        return str(source), "engine"
    if result.get("delegated"):
        return "delegated", "derived"
    if result.get("abstained"):
        return "abstained", "derived"
    backend = str(result.get("backend") or "")
    if backend in ENGINE_TOOL_BACKENDS:
        return "tool_exact", "derived"
    if backend == "sandbox" and result.get("verified"):
        return "model_verified_tests", "derived"
    if result.get("verified"):
        return "model_claimed_verified", "derived"
    return "model_unverified", "derived"


def engine_missing_answer_source(result: dict | None) -> bool:
    if not isinstance(result, dict):
        return False
    version = int(result.get("contract_version") or 0)
    if version >= 2:
        return False
    return "answer_source" not in result


def question_for_runner(item: dict) -> str:
    question = str(item.get("question") or item.get("text") or "")
    suffix = FORMAT_SUFFIX
    hint = item.get("answer_format_hint")
    extra = f"{hint}\n" if hint else ""
    if item.get("suite") == "code":
        extra += "Return only the Python function in one ```python block.\n"
    return question.rstrip() + "\n\n" + extra + suffix


def classify_exception(exc: BaseException) -> str:
    message = str(exc)
    if isinstance(exc, RuntimeError) and "reasoning timeout" in message:
        return "engine_timeout"
    if isinstance(exc, RuntimeError) and "model call budget is spent" in message:
        return "budget_exceeded"
    return "engine_exception"


def tool_name_from_result(result: dict) -> str | None:
    backend = result.get("backend")
    if backend in ENGINE_TOOL_BACKENDS:
        return str(backend)
    for step in result.get("steps") or []:
        if isinstance(step, dict) and step.get("backend") in ENGINE_TOOL_BACKENDS:
            return str(step["backend"])
    return None


def plan_fields(result: dict) -> dict[str, Any]:
    plan_steps = []
    for step in (result.get("plan") or {}).get("steps") or []:
        if isinstance(step, dict) and step.get("tool"):
            plan_steps.append(str(step["tool"]))
    executed = []
    for step in result.get("steps") or []:
        if isinstance(step, dict) and step.get("tool"):
            executed.append(str(step["tool"]))
    missing = [tool for tool in plan_steps if tool not in executed]
    return {"plan_steps": plan_steps, "executed_steps": executed, "plan_not_executed": missing}


# --- model-free probes and gates ------------------------------------------------


def probe_tool(text: str) -> dict[str, Any]:
    from scripts.reason_engine import classify, try_exact_tool

    record: dict[str, Any] = {"classify": None, "fired": False, "answer": None, "backend": None, "crash": None}
    try:
        record["classify"] = classify(text)
    except BaseException as exc:
        record["crash"] = f"classify {type(exc).__name__}: {exc}"
        record["classify"] = "error"
    try:
        result = try_exact_tool(text)
    except BaseException as exc:
        record["crash"] = f"try_exact_tool {type(exc).__name__}: {exc}"
        return record
    if result is not None:
        record["fired"] = True
        record["answer"] = result.get("answer")
        record["backend"] = result.get("backend")
    return record


def run_hijack(items: list[dict], *, sympy: str) -> dict[str, Any]:
    fires = []
    crashes = []
    misroutes = []
    with sympy_mode(sympy):
        for item in items:
            probed = probe_tool(str(item.get("text") or item.get("question") or ""))
            if probed["crash"]:
                crashes.append({"id": item["id"], "sympy_mode": sympy, "error": probed["crash"]})
            if probed["fired"]:
                fires.append({
                    "id": item["id"],
                    "sympy_mode": sympy,
                    "answer": probed["answer"],
                    "backend": probed["backend"],
                    "classify": probed["classify"],
                })
            if probed["classify"] in {"math", "date", "unit"}:
                misroutes.append({"id": item["id"], "sympy_mode": sympy, "classify": probed["classify"]})
    return {"fires": fires, "crashes": crashes, "misroutes": misroutes}


def blank_record(item: dict, runner: str, *, sympy: str, tier: str) -> dict[str, Any]:
    return {
        "id": item["id"],
        "template_id": item.get("template_id") or item["id"],
        "instance": int(item.get("instance") or 0),
        "suite": item.get("suite"),
        "runner": runner,
        "tier": tier,
        "sympy_mode": sympy,
        "correct": False,
        "abstained": False,
        "verified": False,
        "answer_source": None,
        "answer_source_origin": None,
        "tool_name": None,
        "status": "ok",
        "extraction": None,
        "model_calls": 0,
        "completion_tokens": 0,
        "prompts": [],
        "cache_hit": False,
        "truncated": False,
        "early_stopped": False,
        "plan_steps": [],
        "executed_steps": [],
        "plan_not_executed": [],
        "ablation_impl": None,
        "error": None,
    }


def score_engine_result(item: dict, result: dict, record: dict) -> None:
    source, origin = derive_answer_source(result)
    record["answer_source"] = source
    record["answer_source_origin"] = origin
    record["tool_name"] = tool_name_from_result(result) if source == "tool_exact" else None
    record["verified"] = bool(result.get("verified"))
    record["model_calls"] = int(result.get("model_calls") or 0)
    record.update(plan_fields(result))
    if source == "tool_exact":
        record["verified_claim_basis"] = "tool"
    elif source == "model_verified_tests":
        record["verified_claim_basis"] = "tests"
    elif source == "model_claimed_verified":
        record["verified_claim_basis"] = "sources"
    else:
        record["verified_claim_basis"] = "none"
    if item.get("suite") == "code":
        _score_code(item, result, record)
        return
    extracted = extract_answer(
        str(result.get("answer") or ""),
        item,
        engine_abstained=bool(result.get("abstained")),
        engine_value=True,
    )
    record["extraction"] = extracted["extraction"]
    if source == "abstained" or result.get("abstained"):
        extracted = {"value": "ABSTAIN", "extraction": "engine", "format_ok": True}
        record["extraction"] = "engine"
    scored = score_prediction(
        extracted["value"],
        item,
        tool=source == "tool_exact",
        extraction=extracted["extraction"],
    )
    record["correct"] = bool(scored["correct"])
    record["abstained"] = bool(scored["abstained"])
    record["predicted"] = extracted["value"]


def _score_code(item: dict, result: dict, record: dict) -> None:
    from scripts.symbolic_sandbox import SandboxSecurityError, run_sandboxed_python

    if result.get("abstained") or not str(result.get("answer") or "").strip():
        record["correct"] = False
        record["abstained"] = bool(result.get("abstained"))
        record["extraction"] = "engine"
        return
    code = str(result.get("answer") or "")
    visible = str(item.get("visible_tests") or "")
    hidden = str(item.get("hidden_tests") or "")
    try:
        visible_out = run_sandboxed_python(code + "\n" + visible, timeout_seconds=float(item.get("timeout_s") or 10))
        both = run_sandboxed_python(code + "\n" + visible + "\n" + hidden, timeout_seconds=float(item.get("timeout_s") or 10))
    except SandboxSecurityError as exc:
        record["correct"] = False
        record["status"] = "sandbox_rejected"
        record["error"] = str(exc)
        return
    record["correct"] = bool(both.get("ok"))
    if visible_out.get("ok") and not both.get("ok"):
        record["visible_pass_hidden_fail"] = True
        record["answer_source"] = "model_verified_tests"
        record["answer_source_origin"] = record.get("answer_source_origin") or "derived"
    record["extraction"] = "engine"


def run_tools_only(items: list[dict], *, sympy: str, tier: str) -> list[dict]:
    from scripts.reason_engine import reason

    records = []
    with sympy_mode(sympy):
        for item in items:
            record = blank_record(item, "tools_only", sympy=sympy, tier=tier)
            try:
                result = reason(
                    question_for_runner(item) if item.get("suite") != "hijack" else str(item.get("question") or ""),
                    profile="deep",
                    model_fn=None,
                    use_default_model=False,
                    tests=item.get("visible_tests") if item.get("suite") == "code" else None,
                )
            except BaseException as exc:
                status = classify_exception(exc)
                record["status"] = status
                record["error"] = f"{type(exc).__name__}: {exc}"
                record["engine_exception"] = True
                records.append(record)
                continue
            if engine_missing_answer_source(result):
                record["engine_answer_source_missing"] = True
            try:
                score_engine_result(item, result, record)
            except BenchError:
                raise
            except Exception as exc:
                record["status"] = "error"
                record["error"] = f"harness {type(exc).__name__}: {exc}"
            records.append(record)
    return records


def failure_tuples(*, hijack_reports: list[dict], records: list[dict]) -> list[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for report in hijack_reports:
        for fire in report["fires"]:
            found.add(("hijack_fires", fire["id"], fire["sympy_mode"]))
        for crash in report["crashes"]:
            found.add(("tool_crash", crash["id"], crash["sympy_mode"]))
    for record in records:
        mode = str(record.get("sympy_mode") or "natural")
        item_id = str(record["id"])
        if record.get("engine_exception") or record.get("status") in {"engine_exception", "engine_timeout", "budget_exceeded"}:
            if record.get("status") != "ok":
                found.add(("engine_exception", item_id, mode))
        if record.get("engine_answer_source_missing"):
            found.add(("engine_answer_source_present", item_id, mode))
        if record.get("status") == "error":
            found.add(("harness_errors", item_id, mode))
        source = record.get("answer_source")
        if source == "tool_exact" and not record.get("correct"):
            found.add(("tool_wrong", item_id, mode))
            found.add(("tool_precision", item_id, mode))
        if (
            not record.get("correct")
            and record.get("verified")
            and source in {"model_claimed_verified", "tool_exact"}
        ):
            found.add(("claimed_verified_wrong", item_id, mode))
        if record.get("isolation"):
            found.add(("isolation", item_id, mode))
    return sorted(found)


def collect_hard_failures(root: Path, *, include_mock: bool = True) -> dict[str, Any]:
    status = sympy_status()
    hijack = read_jsonl(bench_root(root) / "suites" / "hijack.jsonl")
    tools = read_jsonl(bench_root(root) / "suites" / "tools.jsonl")
    dev = dev_items(root)
    reports = []
    records: list[dict] = []
    for mode in ("natural", "off"):
        reports.append(run_hijack(hijack, sympy=mode))
        records.extend(run_tools_only(tools, sympy=mode, tier="model-free"))
        records.extend(run_tools_only(dev, sympy=mode, tier="model-free"))
    if include_mock:
        records.extend(run_mock_tier(root, sympy="natural"))
        records.extend(run_mock_tier(root, sympy="off"))
    tuples = failure_tuples(hijack_reports=reports, records=records)
    b3 = {
        mode: sorted(fire["id"] for fire in report["fires"] if str(fire["id"]).startswith("hijack-b3-"))
        for mode, report in zip(("natural", "off"), reports)
    }
    return {
        **status,
        "tuples": tuples,
        "b3_fired": b3,
        "hijack_fire_counts": {mode: len(report["fires"]) for mode, report in zip(("natural", "off"), reports)},
        "records": len(records),
    }


def model_free_report(root: Path) -> dict[str, Any]:
    payload = collect_hard_failures(root, include_mock=False)
    payload.pop("records", None)
    return payload


# --- mock tier, runners, isolation ---------------------------------------------


def load_mock_script(root: Path):
    from scripts.reason_bench_models import MockScript

    responses = {}
    path = bench_root(root) / "mock" / "responses.jsonl"
    for row in read_jsonl(path):
        responses[str(row.get("match") or row.get("id"))] = str(row.get("output") or "")
    return MockScript(responses=responses, default="ANSWER: UNKNOWN")


def _mock_reply(script, prompt: str, item: dict) -> str:
    item_id = str(item.get("id") or "")
    if "Return one JSON object" in prompt and "plan-not-executed" in (item.get("tags") or []):
        return '{"problem_type":"math","steps":[{"tool":"calculator","input":"compute the value"}]}'
    if "Return one JSON object" in prompt:
        return '{"problem_type":"other","steps":[{"tool":"model","input":"answer from memory"}]}'
    if item_id in script.responses:
        return script.responses[item_id]
    for key, value in script.responses.items():
        if key and key not in {"id"} and key in prompt:
            return value
    return script.default


def run_mock_tier(root: Path, *, sympy: str) -> list[dict]:
    """ci-mock: smoke + tools + traps through the scripted backend and the engine."""
    from scripts.reason_engine import reason

    script = load_mock_script(root)
    by_id = {item["id"]: item for item in dev_items(root)}
    by_id.update({item["id"]: item for item in read_jsonl(bench_root(root) / "suites" / "tools.jsonl")})
    selected = []
    for item_id in list(SMOKE_IDS) + [row["id"] for row in read_jsonl(bench_root(root) / "suites" / "traps.dev.jsonl")]:
        if item_id in by_id and all(existing["id"] != item_id for existing in selected):
            selected.append(by_id[item_id])
    retriever = note_retriever(root)
    records = []
    runners = ("plain", "fast", "deep", "deep_no_tools", "tools_only", "plain_sc@k", "oracle")
    with sympy_mode(sympy):
        for item in selected:
            order = list(runners)
            random.Random(f"{item['id']}:{sympy}").shuffle(order)
            for runner in order:
                if runner == "oracle" and item.get("suite") != "multihop":
                    continue
                record = blank_record(item, runner, sympy=sympy, tier="ci-mock")
                try:
                    if runner == "tools_only":
                        result = reason(question_for_runner(item), profile="deep", model_fn=None, use_default_model=False, retriever=retriever, tests=item.get("visible_tests") if item.get("suite") == "code" else None)
                        record["model_calls"] = 0
                        if engine_missing_answer_source(result):
                            record["engine_answer_source_missing"] = True
                        score_engine_result(item, result, record)
                    elif runner in {"fast", "deep", "deep_no_tools"}:
                        calls = {"n": 0}

                        def _model(prompt: str, _item=item, _calls=calls) -> str:
                            _calls["n"] += 1
                            script.prompts.append(prompt)
                            return _mock_reply(script, prompt, _item)

                        profile = "fast" if runner == "fast" else "deep"
                        tests = item.get("visible_tests") if item.get("suite") == "code" and runner != "deep_no_tools" else None
                        if runner == "deep_no_tools":
                            record["ablation_impl"] = "patched"
                            from unittest.mock import patch

                            with patch("scripts.reason_engine.try_exact_tool", return_value=None):
                                result = reason(question_for_runner(item), profile=profile, model_fn=_model, use_default_model=False, retriever=retriever, tests=None)
                        else:
                            result = reason(question_for_runner(item), profile=profile, model_fn=_model, use_default_model=False, retriever=retriever, tests=tests)
                        record["model_calls"] = calls["n"]
                        if engine_missing_answer_source(result):
                            record["engine_answer_source_missing"] = True
                        score_engine_result(item, result, record)
                        if runner == "deep_no_tools" and record.get("answer_source") == "tool_exact":
                            record["status"] = "error"
                            record["error"] = "deep_no_tools yielded tool_exact"
                    else:
                        _run_harness_runner(item, runner, record, script, retriever)
                except BenchError:
                    raise
                except BaseException as exc:
                    status = classify_exception(exc)
                    record["status"] = status
                    record["error"] = f"{type(exc).__name__}: {exc}"
                    record["engine_exception"] = True
                records.append(record)
    return records


def _run_harness_runner(item: dict, runner: str, record: dict, script, retriever) -> None:
    if runner == "plain_sc@k":
        samples = []
        for offset in range(3):
            prompt = plain_prompt(item, retriever)
            script.prompts.append(prompt)
            text = _mock_reply(script, prompt, item)
            extracted = extract_answer(text, item)
            samples.append(extracted["value"])
            record["prompts"].append({"sha256_intended": hashlib.sha256(prompt.encode()).hexdigest(), "sample": offset})
        record["model_calls"] = 3
        winner = majority_vote(samples)
        record["answer_source"] = "abstained" if winner == "ABSTAIN" else "model_unverified"
        record["answer_source_origin"] = "harness"
        scored = score_prediction(winner, item, tool=False)
        record["correct"] = bool(scored["correct"])
        record["abstained"] = winner == "ABSTAIN" or bool(scored["abstained"])
        record["extraction"] = "strict"
        record["predicted"] = winner
        return
    prompt = plain_prompt(item, retriever, oracle=runner == "oracle")
    script.prompts.append(prompt)
    text = _mock_reply(script, prompt, item)
    record["model_calls"] = 1
    record["prompts"].append({"sha256_intended": hashlib.sha256(prompt.encode()).hexdigest(), "sha256_sent": hashlib.sha256(prompt.encode()).hexdigest()})
    extracted = extract_answer(text, item)
    record["extraction"] = extracted["extraction"]
    record["answer_source"] = "abstained" if extracted["value"] == "ABSTAIN" else "model_unverified"
    record["answer_source_origin"] = "harness"
    scored = score_prediction(extracted["value"], item, tool=False, extraction=extracted["extraction"])
    record["correct"] = bool(scored["correct"])
    record["abstained"] = bool(scored["abstained"])
    record["predicted"] = extracted["value"]


def majority_vote(samples: list[str]) -> str:
    usable = []
    for sample in samples:
        if sample in {"ABSTAIN", "NONE"}:
            continue
        from scripts.reason_bench_scoring import parse_fraction

        number = parse_fraction(sample)
        usable.append(str(number) if number is not None else sample.strip().casefold())
    if not usable:
        return "ABSTAIN"
    counts = Counter(usable)
    best = counts.most_common()
    if len(best) > 1 and best[0][1] == best[1][1]:
        return "ABSTAIN"
    return best[0][0]


def plain_prompt(item: dict, retriever, *, oracle: bool = False) -> str:
    root = repo_root()
    suite = str(item.get("suite") or "math")
    template = bench_root(root) / "prompts" / f"plain_{suite}.txt"
    if not template.is_file():
        template = bench_root(root) / "prompts" / "plain_math.txt"
    prefix = template.read_text(encoding="utf-8") if template.is_file() else "Think step by step.\n"
    context = ""
    if item.get("suite") == "multihop":
        notes = oracle_notes(item) if oracle else retriever(str(item.get("question") or ""))
        lines = []
        for note in notes[:5]:
            lines.append(f"[{note.get('atom_id') or note.get('id')}] {note.get('claim') or ''}")
        context = "Memory:\n" + "\n".join(lines) + "\n"
    if item.get("suite") == "code":
        context = f"Signature:\n{item.get('signature')}\nVisible tests:\n{item.get('visible_tests')}\n"
    return prefix + "\n" + context + question_for_runner(item)


def oracle_notes(item: dict) -> list[dict]:
    wanted = set(item.get("gold_support") or [])
    return [{"atom_id": note["id"], "claim": note["claim"], "id": note["id"]} for note in item.get("notes") or [] if note.get("id") in wanted]


def note_retriever(root: Path):
    """Lexical overlap over the dev corpus, used when a full vault index is not required."""
    notes = []
    folder = bench_root(root) / "corpus" / "dev" / "notes"
    if folder.is_dir():
        for path in sorted(folder.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            claim = text.split("## Claim", 1)[-1].strip()
            notes.append({"atom_id": path.stem, "filename": path.name, "claim": claim, "path": str(path.resolve())})

    def _retrieve(query: str) -> list[dict]:
        tokens = {token.casefold() for token in __import__("re").findall(r"[A-Za-z0-9]+", query) if len(token) > 2}
        scored = []
        for note in notes:
            hay = {token.casefold() for token in __import__("re").findall(r"[A-Za-z0-9]+", note["claim"])}
            scored.append((len(tokens & hay), note))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["atom_id"]))
        return [note for score, note in scored[:5] if score > 0]

    return _retrieve


def prepare_temp_vault(root: Path, *, canary_into: Path | None = None) -> dict[str, Any]:
    """Create a disposable vault, copy dev notes, and build a lexical index."""
    import importlib

    vault = Path(tempfile.mkdtemp(prefix="sb-reason-bench-"))
    os.environ["BRAIN_VAULT_DIR"] = str(vault)
    atoms = vault / "2-atoms"
    atoms.mkdir(parents=True)
    (vault / "0-raw" / "bench").mkdir(parents=True)
    (vault / ".sidecar").mkdir(parents=True)
    notes = bench_root(root) / "corpus" / "dev" / "notes"
    copied = 0
    if notes.is_dir():
        for path in notes.glob("*.md"):
            claim = path.read_text(encoding="utf-8").split("## Claim", 1)[-1].strip()
            if len(claim) < 12:
                claim = claim + " (benchmark note)"
            source_rel = f"0-raw/bench/{path.name}"
            (vault / "0-raw" / "bench" / path.name).write_text(claim + "\n", encoding="utf-8")
            atom = (
                "---\n"
                f"id: {path.stem}\n"
                "type: atom\n"
                "memory_type: semantic\n"
                "created: 2026-11-27\n"
                "last-reviewed: 2026-11-27\n"
                "importance: 0.50\n"
                "confidence: 0.50\n"
                "sources:\n"
                f"  - {source_rel}\n"
                "links: []\n"
                "tags: []\n"
                "contexts: []\n"
                "supersedes: []\n"
                "superseded_by: []\n"
                "contested_by: []\n"
                "origin: user_local\n"
                "citation_integrity: missing\n"
                "claim_state: candidate\n"
                "epistemic_label: claim\n"
                "review_status: pending\n"
                "status: active\n"
                "---\n\n"
                f"## Claim\n\n{claim}\n\n## Evidence\n\n{claim}\n"
            )
            (atoms / path.name).write_text(atom, encoding="utf-8")
            copied += 1
    import scripts.config as config

    importlib.reload(config)
    from scripts.lexical_store import rebuild_lexical_index

    index_path = vault / ".sidecar" / "lexical.sqlite3"
    rebuild_lexical_index(str(atoms), str(index_path))
    _approve_atoms(vault, atoms)
    if canary_into is not None:
        _write_canary(canary_into)
    return {
        "vault": vault,
        "atoms": atoms,
        "index_path": index_path,
        "chroma_path": vault / ".chroma",
        "copied": copied,
    }


def _approve_atoms(vault: Path, atoms: Path) -> None:
    from scripts.atom_mutator import apply_review_transition

    decision = vault / "0-raw" / "archive" / "decision-record.jsonl"
    decision.parent.mkdir(parents=True, exist_ok=True)
    for path in sorted(atoms.glob("*.md")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        try:
            apply_review_transition(
                atom_path=str(path),
                action="approve",
                expected_sha256=digest,
                actor="benchmark-owner",
                decision_log_path=str(decision),
                history_dir=str(atoms / ".history"),
                brain_dir=str(vault),
                atoms_dir=str(atoms),
                reviewed_on="2026-11-27",
            )
        except Exception:
            # Retrieval still has the corpus retriever. Approval is best-effort so a
            # vault-format change cannot hide a tool-safety failure.
            continue


def _write_canary(vault: Path) -> dict[str, str]:
    token = f"ZEBRA-{uuid.uuid4().hex}"
    atom_id = f"sb-bench-canary-{uuid.uuid4().hex}"
    atoms = vault / "2-atoms"
    atoms.mkdir(parents=True, exist_ok=True)
    claim = f"The canary token is {token}."
    (atoms / f"{atom_id}.md").write_text(
        "---\n"
        f"id: {atom_id}\n"
        "memory_type: semantic\n"
        "review_status: accepted\n"
        "status: active\n"
        "claim_state: human_reviewed\n"
        "contexts: []\n"
        "---\n\n## Claim\n\n"
        f"{claim}\n",
        encoding="utf-8",
    )
    try:
        from scripts.lexical_store import rebuild_lexical_index
        from scripts.predictive_memory_bus import store_l0_entry

        rebuild_lexical_index(str(atoms), str(vault / ".sidecar" / "lexical.sqlite3"))
        store_l0_entry(
            query=claim,
            project_scope=None,
            direct_items=[{
                "filename": f"{atom_id}.md",
                "content": claim,
                "source": "lexical",
                "score": 1.0,
                "similarity": 1.0,
                "review_status": "accepted",
                "memory_type": "semantic",
            }],
            neighbor_items=[],
            freshness_key="bench-canary",
            brain_dir=str(vault),
            atoms_dir=str(atoms),
        )
    except Exception:
        pass
    return {"id": atom_id, "token": token}


def isolated_retriever(vault_info: dict[str, Any], fallback):
    from scripts.recall import RecallOptions, search_memory

    atoms = str(vault_info["atoms"])
    index_path = str(vault_info["index_path"])
    chroma = str(vault_info["chroma_path"])

    def _retrieve(query: str) -> list[dict]:
        direct, neighbors = search_memory(
            query,
            RecallOptions(
                n_results=5,
                include_statuses={"accepted"},
                retrieval_mode="lexical",
                use_l0_bus=False,
                enable_reranker=False,
            ),
            atoms_dir=atoms,
            index_path=index_path,
            chroma_path=chroma,
        )
        rows = []
        for result in list(direct) + list(neighbors):
            filename = str(getattr(result, "filename", "") or "")
            path = str(Path(atoms, filename).resolve()) if filename else ""
            rows.append({
                "atom_id": filename,
                "filename": filename,
                "claim": str(getattr(result, "content", "") or ""),
                "path": path,
            })
        if not rows:
            rows = list(fallback(query) or [])
        return rows

    return _retrieve


def assert_isolation(rows: list[dict], vault: Path, *, protected: list[Path], canary: dict[str, str] | None, prompts: list[str]) -> None:
    vault_real = Path(os.path.realpath(vault))
    protected_real = [Path(os.path.realpath(path)) for path in protected if path and Path(path).exists()]
    blob = "\n".join(prompts)
    if canary:
        if canary["token"] in blob or canary["id"] in blob:
            raise BenchError("isolation leak: canary appeared in a prompt")
    for row in rows:
        path = str(row.get("path") or "")
        atom_id = str(row.get("atom_id") or row.get("filename") or "")
        if canary and (canary["id"] in atom_id or canary["token"] in str(row.get("claim") or "")):
            raise BenchError("isolation leak: canary was retrieved")
        if not path:
            raise BenchError(f"isolation failure: retrieved row {atom_id} has no resolvable path")
        resolved = Path(os.path.realpath(path))
        try:
            resolved.relative_to(vault_real)
        except ValueError as exc:
            raise BenchError(f"isolation failure: {resolved} is outside the temporary vault") from exc
        for root in protected_real:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            if root != vault_real:
                raise BenchError(f"isolation failure: retrieved path is under the real vault {root}")


# --- cache, summary, fingerprint, CLI ------------------------------------------


def cache_key(parts: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(parts).encode("utf-8")).hexdigest()


def cache_read_allowed(flip_rate: float | None, *, temperature: float) -> bool:
    if flip_rate is None:
        return temperature == 0
    return flip_rate == 0


def render_summary(run: dict[str, Any]) -> str:
    lines = [
        f"# Reasoning benchmark summary ({run.get('tier')} {run.get('model')})",
        "",
        "## Verified answers: P(correct | verified)",
        "",
        "| runner | p | wilson | cluster |",
        "|---|---:|---|---|",
    ]
    for row in run.get("verified_table") or []:
        lines.append(f"| {row['runner']} | {row['point']:.3f} | {row['wilson']} | {row['cluster']} |")
    lines.extend(["", "## Tool safety: precision, hijack fires, tool_fire_rate", ""])
    safety = run.get("safety") or {}
    lines.append(f"- tool_precision: {safety.get('tool_precision')}")
    lines.append(f"- hijack_fires: {safety.get('hijack_fires')}")
    lines.append(f"- tool_fire_rate: {safety.get('tool_fire_rate')}")
    lines.append(f"- tool_crash: {safety.get('tool_crash')}")
    lines.append(f"- engine_exception: {safety.get('engine_exception')}")
    lines.extend(["", "## Ablations", ""])
    for name, value in (run.get("ablations") or {}).items():
        lines.append(f"- {name}: {value}")
    lines.extend(["", "## Plan vs execution", ""])
    lines.append(f"- plan_not_executed_rate: {run.get('plan_not_executed_rate')}")
    lines.extend(["", "## Fairness notes", ""])
    for note in FAIRNESS_NOTES:
        lines.append(f"- {note}")
    lines.append("")
    lines.append(f"Cache-hit share: {run.get('cache_hit_share')}")
    if run.get("not_comparable"):
        lines.append("Accuracy and latency comparisons are not comparable.")
    lines.append(f"sympy_available: {run.get('sympy_available')} sympy_version: {run.get('sympy_version')} sympy_mode: {run.get('sympy_mode')}")
    return "\n".join(lines) + "\n"


def write_fingerprint(path: Path, tuples: list[tuple[str, str, str]], meta: dict[str, Any]) -> None:
    payload = {
        "engine_sha": meta.get("engine_sha"),
        "sympy_available": meta.get("sympy_available"),
        "sympy_version": meta.get("sympy_version"),
        "tuples": [list(item) for item in tuples],
    }
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8", newline="\n")


def load_fingerprint(path: Path) -> list[tuple[str, str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [tuple(item) for item in payload["tuples"]]


def fingerprint_added(current: list[tuple[str, str, str]], base: list[tuple[str, str, str]] | None) -> list[tuple[str, str, str]]:
    if base is None:
        return []
    return sorted(set(current) - set(base))


def gates_main(root: Path) -> int:
    payload = collect_hard_failures(root, include_mock=True)
    print(f"sympy_available={payload['sympy_available']} sympy_version={payload['sympy_version']}")
    print(f"b3_fired={json.dumps(payload['b3_fired'], sort_keys=True)}")
    for gate, item_id, mode in payload["tuples"]:
        print(f"HARD {gate} {item_id} {mode}")
    print(f"hard_failure_count={len(payload['tuples'])}")
    return 1 if payload["tuples"] else 0


def compare_fingerprint(root: Path, expect_path: Path, base_tuples: list[tuple[str, str, str]] | None) -> int:
    expected = load_fingerprint(expect_path)
    added = fingerprint_added(expected, base_tuples)
    if added:
        print(f"fingerprint added tuples relative to the base: {added[:12]}", file=sys.stderr)
        return 1
    observed_payload = collect_hard_failures(root, include_mock=True)
    observed = observed_payload["tuples"]
    missing = sorted(set(expected) - set(observed))
    extra = sorted(set(observed) - set(expected))
    print(f"sympy_available={observed_payload['sympy_available']} sympy_version={observed_payload['sympy_version']}")
    print(f"b3_fired={json.dumps(observed_payload['b3_fired'], sort_keys=True)}")
    if missing or extra:
        print(f"fingerprint mismatch missing={len(missing)} extra={len(extra)}", file=sys.stderr)
        for row in extra[:30]:
            print(f"EXTRA {' '.join(row)}", file=sys.stderr)
        for row in missing[:30]:
            print(f"MISSING {' '.join(row)}", file=sys.stderr)
        return 1
    if observed_payload["b3_fired"].get("natural") != sorted(B3_FIRED) or observed_payload["b3_fired"].get("off") != sorted(B3_FIRED):
        print(f"B3 fired set drifted: {observed_payload['b3_fired']}", file=sys.stderr)
        return 1
    print(f"fingerprint_ok tuples={len(observed)}")
    return 0


def external_allowed(name: str, *, owner_statement: str | None) -> dict[str, Any]:
    if name not in EXTERNAL_SETS:
        raise BenchError(f"unknown external set {name}")
    spec = EXTERNAL_SETS[name]
    if name == "gsm-symbolic":
        if not owner_statement:
            raise BenchError(
                "GSM-Symbolic stays disabled until the owner passes --owner-noncommercial-confirm. "
                "A confirmation relayed by an agent does not count."
            )
        licence = fetch_dataset_licence(spec["repo"])
        if licence != "cc-by-nc-nd-4.0":
            raise BenchError(f"GSM-Symbolic card licence is {licence!r}, expected cc-by-nc-nd-4.0")
        return {
            "set": name,
            "licence": "cc-by-nc-nd-4.0",
            "confirmed_by": "owner",
            "confirmed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "statement": owner_statement,
        }
    return {"set": name, "licence": spec["licence"], "contaminated": True}


def fetch_dataset_licence(repo_id: str) -> str:
    import httpx

    response = httpx.get(f"https://huggingface.co/api/datasets/{repo_id}", timeout=30.0)
    response.raise_for_status()
    body = response.json()
    card = body.get("cardData") or {}
    licence = card.get("license") or card.get("licence") or ""
    if isinstance(licence, list):
        licence = licence[0] if licence else ""
    return str(licence).strip().casefold()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reason-bench", description="Second Brain reasoning benchmark harness.")
    sub = parser.add_subparsers(dest="command", required=True)
    validate_p = sub.add_parser("validate")
    validate_p.add_argument("--root", default=None)
    generate_p = sub.add_parser("generate")
    generate_p.add_argument("--suite", required=True)
    generate_p.add_argument("--bump-version", default=None)
    run_p = sub.add_parser("run")
    run_p.add_argument("--tier", default="ci-mock")
    run_p.add_argument("--models", default="mock")
    run_p.add_argument("--backend", default=None)
    run_p.add_argument("--runners", default=None)
    run_p.add_argument("--aa", action="store_true")
    run_p.add_argument("--cache", default=None, choices=["off", "read", "write", "readwrite"])
    run_p.add_argument("--seed", type=int, default=1234)
    run_p.add_argument("--temperature", type=float, default=0.0)
    run_p.add_argument("--repeats", type=int, default=1)
    run_p.add_argument("--num-thread", type=int, default=None)
    run_p.add_argument("--k", type=int, default=3)
    run_p.add_argument("--max-items", type=int, default=None)
    run_p.add_argument("--ids", default=None)
    run_p.add_argument("--resume", default=None)
    run_p.add_argument("--base-url", default=None)
    run_p.add_argument("--api-key-env", default=None)
    run_p.add_argument("--allow-remote-endpoint", action="store_true")
    run_p.add_argument("--canary-real-vault", action="store_true")
    run_p.add_argument("--external", default=None)
    run_p.add_argument("--owner-noncommercial-confirm", default=None)
    run_p.add_argument("--sealed", default=None)
    run_p.add_argument("--prereg", default=None)
    run_p.add_argument("--out", default=None)
    compare_p = sub.add_parser("compare")
    compare_p.add_argument("run_dir")
    compare_p.add_argument("--baseline", required=True)
    compare_p.add_argument("--strict", action="store_true")
    report_p = sub.add_parser("report")
    report_p.add_argument("run_dirs", nargs="+")
    baseline_p = sub.add_parser("baseline")
    baseline_p.add_argument("run_dir")
    baseline_p.add_argument("--runner", required=True)
    baseline_p.add_argument("--write", action="store_true")
    lock_p = sub.add_parser("lock")
    lock_p.add_argument("--bump-version", required=True)
    lock_p.add_argument("--reason", required=True)
    sub.add_parser("gates")
    finger_p = sub.add_parser("fingerprint")
    finger_p.add_argument("--write", default=None)
    finger_p.add_argument("--check", default=None)
    finger_p.add_argument("--base-file", default=None)
    seal_p = sub.add_parser("heldout-seal-templates")
    seal_p.add_argument("--in", dest="src", required=True)
    seal_p.add_argument("--out", required=True)
    gen_p = sub.add_parser("heldout-generate")
    gen_p.add_argument("--templates-sealed", required=True)
    gen_p.add_argument("--out", required=True)
    dis_p = sub.add_parser("heldout-check-disjoint")
    dis_p.add_argument("--templates-sealed", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(_dispatch(args))
    except BenchError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except SystemExit as exc:
        code = exc.code
        if code in {0, 1, 2}:
            return int(code)
        raise


def _dispatch(args: argparse.Namespace) -> int:
    root = require_source_checkout()
    if args.command == "validate":
        validate(root)
        print("validate_ok")
        return 0
    if args.command == "generate":
        count = generate(root, args.suite)
        print(f"generated {args.suite} items={count}")
        return 0
    if args.command == "lock":
        payload = write_manifest(root, suite_version=args.bump_version, reason=args.reason)
        print(f"locked {payload['suite_version']} files={len(payload['files'])}")
        return 0
    if args.command == "gates":
        return gates_main(root)
    if args.command == "fingerprint":
        if args.write:
            payload = collect_hard_failures(root, include_mock=True)
            meta = {
                "engine_sha": _git_sha(root),
                "sympy_available": payload["sympy_available"],
                "sympy_version": payload["sympy_version"],
            }
            write_fingerprint(Path(args.write), payload["tuples"], meta)
            print(f"wrote {len(payload['tuples'])} tuples")
            return 0
        if args.check:
            base = load_fingerprint(Path(args.base_file)) if args.base_file and Path(args.base_file).is_file() else None
            return compare_fingerprint(root, Path(args.check), base)
        raise BenchError("fingerprint needs --write or --check")
    if args.command == "run":
        return run_command(root, args)
    if args.command == "compare":
        return compare_command(Path(args.run_dir), Path(args.baseline), strict=args.strict)
    if args.command == "report":
        for run_dir in args.run_dirs:
            summary = Path(run_dir) / "summary.md"
            print(summary.read_text(encoding="utf-8") if summary.is_file() else "")
        return 0
    if args.command == "baseline":
        return baseline_command(Path(args.run_dir), args.runner, write=args.write)
    if args.command == "heldout-seal-templates":
        return heldout_seal_templates(Path(args.src), Path(args.out), root)
    if args.command == "heldout-generate":
        return heldout_generate(Path(args.templates_sealed), Path(args.out), root)
    if args.command == "heldout-check-disjoint":
        return heldout_disjoint(Path(args.templates_sealed), root)
    raise BenchError(f"unknown command {args.command}")


def _git_sha(root: Path) -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _select_tier_items(root: Path, tier: str, args: argparse.Namespace) -> list[dict]:
    dev = dev_items(root)
    if tier == "ci-live-smoke":
        by_id = {item["id"]: item for item in dev}
        chosen = [by_id[item_id] for item_id in SMOKE_IDS if item_id in by_id]
    elif tier == "pr":
        chosen = core_items(dev)
    elif tier == "full":
        chosen = dev
    elif tier == "ablation":
        chosen = core_items(dev)
    else:
        chosen = core_items(dev)
    if args.ids:
        wanted = {part.strip() for part in args.ids.split(",") if part.strip()}
        chosen = [item for item in chosen if item["id"] in wanted]
    if args.max_items is not None:
        chosen = chosen[: args.max_items]
    return chosen


def _tier_runners(tier: str, explicit: str | None) -> tuple[str, ...]:
    if explicit:
        return tuple(part.strip() for part in explicit.split(",") if part.strip())
    return {
        "ci-live-smoke": ("fast", "deep", "tools_only"),
        "pr": ("plain", "deep", "tools_only"),
        "ablation": ("plain_sc@k", "deep_no_tools", "fast", "oracle"),
        "full": ("plain", "deep", "tools_only"),
    }.get(tier, ("plain", "deep", "tools_only"))


def _run_live(root: Path, args: argparse.Namespace, *, cache_mode: str, ack: dict | None) -> int:
    """Live Ollama or OpenAI run. Hard gates still fail the process. Accuracy is not gated."""
    from scripts.reason_bench_models import ModelClientError, RawChatClient, ollama_base_from_utils
    from scripts.reason_engine import reason

    backend = args.backend or "ollama"
    model_name = (args.models or "").split(",")[0].strip()
    if not model_name:
        raise BenchError("a live run needs --models")
    if backend == "ollama":
        base = args.base_url or ollama_base_from_utils()
    else:
        base = args.base_url or "http://127.0.0.1:8080"
    os.environ["OLLAMA_NUM_PARALLEL"] = "1"
    os.environ["OLLAMA_MAX_LOADED_MODELS"] = "1"
    try:
        client = RawChatClient(
            backend=backend,
            model=model_name,
            base_url=base,
            allow_remote=bool(args.allow_remote_endpoint),
            api_key_env=args.api_key_env,
            num_thread=args.num_thread or (os.cpu_count() or 1),
            temperature=args.temperature,
            seed=args.seed,
        )
        identity = client.warm()
    except ModelClientError as exc:
        raise BenchError(str(exc)) from exc
    except Exception as exc:
        raise BenchError(f"model warm-up failed: {exc}") from exc
    items = _select_tier_items(root, args.tier, args)
    runners = _tier_runners(args.tier, args.runners)
    partial = args.max_items is not None
    records: list[dict] = []
    mode = "natural"
    with sympy_mode(mode):
        for item in items:
            for runner in runners:
                if runner == "oracle" and item.get("suite") != "multihop":
                    continue
                record = blank_record(item, runner, sympy=mode, tier=args.tier)
                num_predict = NUM_PREDICT.get(str(item.get("suite") or "math"), 384)

                def _model(prompt: str, _record=record, _num=num_predict, **options) -> str:
                    try:
                        generated = client.generate(
                            prompt,
                            num_predict=_num,
                            temperature=options.get("temperature", args.temperature),
                            seed=options.get("seed", args.seed),
                        )
                    except ModelClientError:
                        raise
                    _record["prompts"].append({
                        "sha256_intended": generated.sha256_intended,
                        "sha256_sent": generated.sha256_sent,
                        "options_effective": generated.options_effective,
                    })
                    _record["completion_tokens"] = int(_record.get("completion_tokens") or 0) + int(generated.completion_tokens or 0)
                    _record["early_stopped"] = bool(generated.early_stopped)
                    _record["truncated"] = bool(generated.truncated)
                    return generated.text

                try:
                    if runner == "tools_only":
                        result = reason(question_for_runner(item), profile="deep", model_fn=None, use_default_model=False, tests=item.get("visible_tests") if item.get("suite") == "code" else None)
                        if engine_missing_answer_source(result):
                            record["engine_answer_source_missing"] = True
                        score_engine_result(item, result, record)
                    elif runner in {"fast", "deep", "deep_no_tools"}:
                        profile = "fast" if runner == "fast" else "deep"
                        tests = item.get("visible_tests") if item.get("suite") == "code" and runner != "deep_no_tools" else None
                        if runner == "deep_no_tools":
                            from unittest.mock import patch

                            record["ablation_impl"] = "patched"
                            with patch("scripts.reason_engine.try_exact_tool", return_value=None):
                                result = reason(question_for_runner(item), profile=profile, model_fn=_model, use_default_model=False, tests=None)
                        else:
                            result = reason(question_for_runner(item), profile=profile, model_fn=_model, use_default_model=False, tests=tests)
                        if engine_missing_answer_source(result):
                            record["engine_answer_source_missing"] = True
                        score_engine_result(item, result, record)
                    else:
                        prompt = plain_prompt(item, note_retriever(root), oracle=runner == "oracle")
                        record["model_calls"] = 1
                        text = _model(prompt)
                        extracted = extract_answer(text, item)
                        record["extraction"] = extracted["extraction"]
                        record["answer_source"] = "abstained" if extracted["value"] == "ABSTAIN" else "model_unverified"
                        record["answer_source_origin"] = "harness"
                        scored = score_prediction(extracted["value"], item, tool=False, extraction=extracted["extraction"])
                        record["correct"] = bool(scored["correct"])
                        record["abstained"] = bool(scored["abstained"])
                except ModelClientError as exc:
                    client.close()
                    raise BenchError(str(exc)) from exc
                except BaseException as exc:
                    status = classify_exception(exc)
                    record["status"] = status
                    record["error"] = f"{type(exc).__name__}: {exc}"
                    record["engine_exception"] = True
                records.append(record)
    hijack = run_hijack(read_jsonl(bench_root(root) / "suites" / "hijack.jsonl"), sympy=mode) if args.tier == "ci-live-smoke" else {"fires": [], "crashes": [], "misroutes": []}
    if args.tier == "ci-live-smoke":
        records.extend(run_tools_only(read_jsonl(bench_root(root) / "suites" / "tools.jsonl"), sympy=mode, tier=args.tier))
    try:
        client.assert_digest_unchanged()
    except ModelClientError as exc:
        client.close()
        raise BenchError(str(exc)) from exc
    client.close()
    tuples = failure_tuples(hijack_reports=[hijack], records=records)
    out = Path(args.out) if args.out else bench_root(root) / "results" / time.strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=True)
    write_jsonl(out / "items.jsonl", records)
    run = {
        "tier": args.tier,
        "partial": partial,
        "model": model_name,
        "backend": backend,
        "cache": cache_mode,
        "harness_version": HARNESS_VERSION,
        "suite_version": SUITE_VERSION,
        "git_sha": _git_sha(root),
        "sympy_mode": mode,
        **sympy_status(),
        "identity": identity,
        "external_licence_ack": ack,
        "num_thread": client.num_thread,
        "hard_failures": [list(item) for item in tuples],
        "fairness_notes": list(FAIRNESS_NOTES),
        "safety": _safety_from(records, hijack),
        "verified_table": [],
        "ablations": {},
        "plan_not_executed_rate": _rate(records, lambda row: bool(row.get("plan_not_executed"))),
        "cache_hit_share": 0.0,
    }
    (out / "run.json").write_text(canonical_json(run) + "\n", encoding="utf-8")
    (out / "summary.md").write_text(render_summary(run), encoding="utf-8")
    print(f"run_dir={out}")
    print(f"hard_failure_count={len(tuples)}")
    for gate, item_id, sympy_name in tuples:
        print(f"HARD {gate} {item_id} {sympy_name}")
    return 1 if tuples else 0


def run_command(root: Path, args: argparse.Namespace) -> int:
    if args.aa and args.cache not in {None, "off"}:
        raise BenchError("--aa forces --cache off")
    cache_mode = "off" if args.aa else (args.cache or "readwrite")
    if args.external:
        ack = external_allowed(args.external, owner_statement=args.owner_noncommercial_confirm)
    else:
        ack = None
        if args.owner_noncommercial_confirm:
            raise BenchError("--owner-noncommercial-confirm is only used with --external gsm-symbolic")
    if args.tier == "ci-mock" or (args.backend == "mock" and args.tier == "ci-mock"):
        return _run_ci_mock(root, args, cache_mode=cache_mode, ack=ack)
    if args.tier == "heldout":
        if not args.sealed:
            raise BenchError("held-out runs require the owner at the keyboard; pass --sealed")
        return _run_heldout(root, args)
    if args.backend == "mock" or args.models == "mock" or args.tier == "ci-mock":
        return _run_ci_mock(root, args, cache_mode=cache_mode, ack=ack)
    return _run_live(root, args, cache_mode=cache_mode, ack=ack)


def _run_ci_mock(root: Path, args: argparse.Namespace, *, cache_mode: str, ack: dict | None) -> int:
    del cache_mode
    records = run_mock_tier(root, sympy="natural")
    hijack = run_hijack(read_jsonl(bench_root(root) / "suites" / "hijack.jsonl"), sympy="natural")
    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="sb-reason-mock-"))
    out.mkdir(parents=True, exist_ok=True)
    write_jsonl(out / "items.jsonl", records)
    tuples = failure_tuples(hijack_reports=[hijack], records=records)
    safety = _safety_from(records, hijack)
    verified_rows = [row for row in records if row.get("verified")]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in verified_rows:
        grouped[row["runner"]].append(row)
    table = []
    for runner, rows in sorted(grouped.items()):
        stats = accuracy_interval(rows, b=200, seed=BOOTSTRAP_SEED)
        table.append({
            "runner": runner,
            "point": stats["point"],
            "wilson": [round(value, 3) for value in stats["wilson_item"]],
            "cluster": [round(value, 3) for value in stats["verdict_interval"]],
        })
    run = {
        "tier": "ci-mock",
        "model": "mock",
        "harness_version": HARNESS_VERSION,
        "suite_version": SUITE_VERSION,
        "git_sha": _git_sha(root),
        "sympy_mode": "natural",
        **sympy_status(),
        "external_licence_ack": ack,
        "verified_table": table,
        "safety": safety,
        "ablations": {},
        "plan_not_executed_rate": _rate(records, lambda row: bool(row.get("plan_not_executed"))),
        "cache_hit_share": 0.0,
        "hard_failures": [list(item) for item in tuples],
        "fairness_notes": list(FAIRNESS_NOTES),
    }
    (out / "run.json").write_text(canonical_json(run) + "\n", encoding="utf-8")
    (out / "summary.md").write_text(render_summary(run), encoding="utf-8")
    print(f"run_dir={out}")
    print(f"hard_failure_count={len(tuples)}")
    return 1 if tuples else 0


def _run_heldout(root: Path, args: argparse.Namespace) -> int:
    from scripts.reason_bench_heldout import HeldoutError, load_sealed, verify_commitment

    if not sys.stdin.isatty():
        raise BenchError("a held-out run needs the owner at an interactive TTY")
    try:
        from scripts.reason_bench_heldout import prompt_passphrase

        passphrase = prompt_passphrase(confirm=False)
        payload, _hmac_key = load_sealed(Path(args.sealed), passphrase, repo_root=root)
        commitment = json.loads((bench_root(root) / "heldout.commitment.json").read_text(encoding="utf-8"))
        verify_commitment(payload, commitment)
    except HeldoutError as exc:
        raise BenchError(str(exc)) from exc
    if not args.prereg:
        raise BenchError("a held-out run needs --prereg")
    print("held-out commitment ok; item text is not written")
    return 0


def _safety_from(records: list[dict], hijack: dict) -> dict[str, Any]:
    tool_rows = [row for row in records if row.get("answer_source") == "tool_exact"]
    correct = sum(1 for row in tool_rows if row.get("correct"))
    return {
        "tool_precision": (correct / len(tool_rows)) if tool_rows else 1.0,
        "hijack_fires": len(hijack.get("fires") or []),
        "tool_crash": len(hijack.get("crashes") or []),
        "engine_exception": sum(1 for row in records if row.get("engine_exception")),
        "tool_fire_rate": None,
        "tool_wrong": sum(1 for row in tool_rows if not row.get("correct")),
        "claimed_verified_wrong": sum(
            1 for row in records
            if not row.get("correct") and row.get("verified") and row.get("answer_source") in {"model_claimed_verified", "tool_exact"}
        ),
    }


def run_with_hang_guard(fn, hang_timeout_s: float):
    """Run ``fn`` on a worker thread. A timeout yields status hung."""
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["error"] = exc

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join(hang_timeout_s)
    if worker.is_alive():
        return {"status": "hung"}
    if "error" in box:
        raise box["error"]
    return {"status": "ok", "value": box.get("value")}


def accuracy_pair(records: list[dict]) -> dict[str, float]:
    """Headline accuracy counts timeouts as wrong. The excluded figure drops them from n."""
    if not records:
        return {"accuracy": 0.0, "accuracy_excl_timeouts": 0.0, "n": 0, "n_excl_timeouts": 0}
    dropped = {"engine_timeout", "hung", "budget_exceeded"}
    kept = [row for row in records if row.get("status") not in dropped]
    return {
        "accuracy": sum(1 for row in records if row.get("correct")) / len(records),
        "accuracy_excl_timeouts": (sum(1 for row in kept if row.get("correct")) / len(kept)) if kept else 0.0,
        "n": len(records),
        "n_excl_timeouts": len(kept),
    }


def merge_resume(existing: list[dict], fresh: list[dict]) -> list[dict]:
    seen = {(row.get("id"), row.get("runner"), row.get("sympy_mode")) for row in existing}
    merged = list(existing)
    for row in fresh:
        key = (row.get("id"), row.get("runner"), row.get("sympy_mode"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged


def _rate(records: list[dict], predicate) -> float:
    if not records:
        return 0.0
    return sum(1 for row in records if predicate(row)) / len(records)


def compare_command(run_dir: Path, baseline: Path, *, strict: bool) -> int:
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    base = json.loads(baseline.read_text(encoding="utf-8"))
    comparable = run.get("comparability") == base.get("comparability")
    print("comparable" if comparable else "not comparable")
    if not comparable:
        return 1 if strict else 0
    return 0


def baseline_command(run_dir: Path, runner: str, *, write: bool) -> int:
    if not write:
        print("pass --write to store a baseline")
        return 0
    destination = bench_root() / "baselines" / f"manual.{runner}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(run_dir / "run.json", destination)
    print(f"wrote {destination}")
    return 0


def heldout_seal_templates(src: Path, dest: Path, root: Path) -> int:
    from scripts.reason_bench_heldout import HeldoutError, prompt_passphrase, seal_to_path

    try:
        passphrase = prompt_passphrase(confirm=True)
        files = {}
        for path in sorted(src.rglob("*.py")):
            files[path.relative_to(src).as_posix()] = path.read_text(encoding="utf-8")
        if not files:
            raise BenchError("no templates to seal")
        seal_to_path(json.dumps({"templates": files}).encode("utf-8"), dest, passphrase, repo_root=root)
    except HeldoutError as exc:
        raise BenchError(str(exc)) from exc
    shutil.rmtree(src, ignore_errors=True)
    print(f"sealed {len(files)} templates")
    return 0


def heldout_generate(sealed_templates: Path, dest: Path, root: Path) -> int:
    from scripts.reason_bench_heldout import HeldoutError, load_sealed, load_template_module, prompt_passphrase, seal_to_path

    try:
        passphrase = prompt_passphrase(confirm=True)
        payload, _hmac_key = load_sealed(sealed_templates, passphrase, repo_root=root)
        templates = payload.get("templates") or {}
        items = []
        for name, source in sorted(templates.items()):
            module = load_template_module(source, "private_" + Path(name).stem)
            if hasattr(module, "build_item"):
                items.append(module.build_item(0))
        seed = os.urandom(32)
        salt = os.urandom(32)
        body = {
            "items": items,
            "seed_hex": seed.hex(),
            "salt_hex": salt.hex(),
            "notes": [],
        }
        seal_to_path(json.dumps(body).encode("utf-8"), dest, passphrase, repo_root=root)
    except HeldoutError as exc:
        raise BenchError(str(exc)) from exc
    print(f"sealed {len(items)} held-out items; plaintext was not written")
    return 0


def heldout_disjoint(sealed_templates: Path, root: Path) -> int:
    from scripts.reason_bench_heldout import HeldoutError, disjointness_report, load_sealed, load_template_module, prompt_passphrase, public_template_texts

    try:
        passphrase = prompt_passphrase(confirm=False)
        payload, _hmac_key = load_sealed(sealed_templates, passphrase, repo_root=root)
        texts = []
        for name, source in sorted((payload.get("templates") or {}).items()):
            module = load_template_module(source, "private_" + Path(name).stem)
            if hasattr(module, "render") and hasattr(module, "sample"):
                texts.append(module.render(module.sample(random.Random(0))))
            else:
                texts.append(source)
        report = disjointness_report(texts, public_template_texts(root))
    except HeldoutError as exc:
        raise BenchError(str(exc)) from exc
    print(canonical_json(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
