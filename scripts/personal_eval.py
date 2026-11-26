# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Personal retrieval eval set.

A file looks like this::

    {
      "name": "week-of-notes",
      "k": 5,
      "allow_unreviewed_exploratory": false,
      "queries": [
        {"id": "q1", "query": "pour-over ratio", "relevant": ["coffee-ratio.md"]}
      ]
    }

``relevant`` lists atom filenames that should appear in the top k.
``second-brain eval`` prints recall@k and MRR. It does not change the vault.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from scripts.recall import RecallOptions, search_lexical_memory


def _label_text(value: str) -> str:
    """Drop a leading date and a trailing content hash so ids still match a stable slug."""
    text = value.casefold().strip()
    text = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", text)
    text = re.sub(r"-[0-9a-f]{8}$", "", text)
    return text


def _result_matches_relevant(result: object, wanted: set[str]) -> bool:
    """Match a filename, its stem, a dated slug, an atom id, a title, or the same content words."""
    filename = str(getattr(result, "filename", "") or "")
    stem = Path(filename).stem
    labels = {filename, Path(filename).name, stem}
    claim = ""
    content = str(getattr(result, "content", "") or "")
    if content:
        from scripts.memory_format import split_frontmatter
        from scripts.memory_review import parse_frontmatter_values, section_text

        parsed = split_frontmatter(content)
        if parsed is not None:
            values = parse_frontmatter_values(parsed[0])
            claim = section_text("\n".join(parsed[1]), "Claim")
            for key in ("id", "title"):
                raw = str(values.get(key) or "").strip().strip("\"'")
                if raw:
                    labels.add(raw)
                    labels.add(Path(raw).stem)
            raw_sources = values.get("sources") or []
            if isinstance(raw_sources, str):
                raw_sources = [raw_sources]
            if isinstance(raw_sources, (list, tuple)):
                for source in raw_sources:
                    source_name = str(source).strip()
                    if source_name:
                        labels.add(source_name)
                        labels.add(Path(source_name).stem)
    folded = {item.casefold() for item in labels if item}
    stripped = {_label_text(item) for item in folded}
    haystack = " ".join(sorted(stripped | {claim.casefold()}))
    for want in wanted:
        clean = want.strip()
        if not clean:
            continue
        if clean in labels or clean.casefold() in folded:
            return True
        want_stem = _label_text(Path(clean).stem)
        if want_stem and any(
            item == want_stem or want_stem in item or item.endswith("-" + want_stem) or want_stem.endswith("-" + item)
            for item in stripped
        ):
            return True
        want_terms = {word for word in re.findall(r"[a-z0-9]+", want_stem) if len(word) >= 4}
        if want_terms and all(term in haystack for term in want_terms):
            return True
    return False


def load_eval_set(path: str | Path) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("queries"), list) or not data["queries"]:
        raise ValueError("Eval set must be an object with a non-empty queries list.")
    for item in data["queries"]:
        if not isinstance(item, dict):
            raise ValueError("Each query must be an object.")
        if not str(item.get("query") or "").strip():
            raise ValueError("Each query needs a query string.")
        relevant = item.get("relevant")
        if not isinstance(relevant, list) or not relevant or not all(isinstance(name, str) and name.strip() for name in relevant):
            raise ValueError("Each query needs a non-empty relevant list of filenames.")
    return data


def evaluate_file(
    path: str | Path,
    *,
    atoms_dir: str | None = None,
    index_path: str | None = None,
    k: int | None = None,
) -> dict[str, Any]:
    data = load_eval_set(path)
    limit = int(k or data.get("k") or 5)
    if limit < 1:
        raise ValueError("k must be at least 1.")
    exploratory = bool(data.get("allow_unreviewed_exploratory"))
    raw_statuses = data.get("include_statuses")
    if isinstance(raw_statuses, list) and raw_statuses:
        include_statuses = {str(item) for item in raw_statuses}
    elif exploratory:
        include_statuses = {"pending", "accepted", "approved"}
    else:
        include_statuses = None
    options = RecallOptions(
        n_results=limit,
        follow_links=False,
        enable_reranker=False,
        allow_unreviewed_exploratory=exploratory,
        include_statuses=include_statuses,
    )
    rows = []
    for item in data["queries"]:
        direct, _neighbors = search_lexical_memory(
            str(item["query"]),
            options,
            atoms_dir=atoms_dir,
            index_path=index_path,
        )
        ranked_results = list(direct[:limit])
        relevant = {name.strip() for name in item["relevant"] if str(name).strip()}
        hit = 1.0 if any(_result_matches_relevant(result, relevant) for result in ranked_results) else 0.0
        reciprocal = 0.0
        ranked = [result.filename for result in ranked_results]
        for position, result in enumerate(ranked_results, start=1):
            if _result_matches_relevant(result, relevant):
                reciprocal = 1.0 / position
                break
        rows.append(
            {
                "id": str(item.get("id") or item["query"]),
                "recall_at_k": hit,
                "reciprocal_rank": reciprocal,
                "ranked": ranked,
            }
        )
    count = len(rows)
    return {
        "name": str(data.get("name") or Path(path).name),
        "k": limit,
        "queries": count,
        "recall_at_k": round(sum(row["recall_at_k"] for row in rows) / count, 4),
        "mrr": round(sum(row["reciprocal_rank"] for row in rows) / count, 4),
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="second-brain eval",
        description="Score a personal retrieval set with recall@k and MRR.",
    )
    parser.add_argument("eval_set", help="Path to a personal eval JSON file.")
    parser.add_argument("--atoms-dir", default=None)
    parser.add_argument("--index-path", default=None)
    parser.add_argument("--k", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = evaluate_file(args.eval_set, atoms_dir=args.atoms_dir, index_path=args.index_path, k=args.k)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"{report['name']}: recall@{report['k']}={report['recall_at_k']:.3f} "
            f"MRR={report['mrr']:.3f} over {report['queries']} queries"
        )
    return 0
