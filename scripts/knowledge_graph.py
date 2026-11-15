# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""Contradiction & Drift Radar + Temporal Knowledge Graph.

Constructs a deterministic, explainable graph across atoms (explicit links,
superseded_by lineage, shared-topic edges) and runs a zero-model Contradiction &
Drift Radar to surface:
  * Polarity conflicts (affirmative vs prohibitive claims on the same topic)
  * Numeric parameter conflicts (e.g. differing thresholds/days/ports)
  * Source anchor drift & orphan approved atoms

Read-only advisory radar: never mutates or auto-approves vault memory.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import approved_revision_is_current, latest_decisions_by_atom  # noqa: E402
from config import ATOMS_DIR, BRAIN_DIR, _is_reparse_or_link, is_content_markdown  # noqa: E402
from memory_format import split_frontmatter  # noqa: E402
from memory_review import parse_atom_record, parse_frontmatter_values, parse_scalar_list  # noqa: E402
from recall import normalize_project_tag, project_tags_match, wikilinks  # noqa: E402

_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have",
        "in", "is", "it", "its", "of", "on", "or", "that", "the", "to", "was", "were",
        "will", "with", "should", "must", "can", "could", "would", "all", "any",
        "not", "no", "never", "do", "dont", "don't", "avoid", "forbid", "deprecated",
        "use", "used", "using", "only", "always", "every",
    }
)
_NEGATION_RE = re.compile(
    r"\b(?:never|must\s+not|do\s+not|don't|cannot|can't|avoid|forbid|forbidden|disable|disabled|deprecated|no\s+longer|prohibited)\b",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\b(\d+(?:\.\d+)?)\b")
_TOKEN_RE = re.compile(r"[a-z0-9_]{3,}")


def _topic_tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS and not t.isdigit()}


def _is_negative_claim(text: str) -> bool:
    return bool(_NEGATION_RE.search(text))


def _extract_numbers(text: str) -> set[str]:
    return set(_NUMBER_RE.findall(text))


def _matches_scope(contexts: list[str], project_scope: str | None) -> bool:
    if not project_scope or not project_scope.strip():
        return True
    target = normalize_project_tag(project_scope)
    if not target:
        return True
    proj_tags = [str(c).strip() for c in contexts if str(c).strip().lower().startswith("project:")]
    return bool(proj_tags) and all(bool(project_tags_match((c,), (target,))) for c in proj_tags)


def build_knowledge_graph(
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    project_scope: str | None = None,
    include_pending: bool = False,
) -> dict[str, Any]:
    """Build deterministic nodes and edges across vault atoms."""
    nodes: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    latest_decisions: dict[str, dict[str, Any]] = {}
    dlog = os.path.join(brain_dir, "0-raw", "archive", "decision-record.jsonl")
    if os.path.isfile(dlog) and not _is_reparse_or_link(dlog):
        try:
            latest_decisions = latest_decisions_by_atom(dlog)
        except (OSError, ValueError, TypeError):
            latest_decisions = {}

    if os.path.isdir(atoms_dir) and not _is_reparse_or_link(atoms_dir):
        for entry in sorted(os.scandir(atoms_dir), key=lambda e: e.name):
            if not is_content_markdown(entry.name):
                continue
            path = os.path.join(atoms_dir, entry.name)
            if _is_reparse_or_link(path) or not os.path.isfile(path):
                continue
            try:
                raw = Path(path).read_text(encoding="utf-8")
                rec = parse_atom_record(path)
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            fm_parsed = split_frontmatter(raw)
            fm_vals = parse_frontmatter_values(fm_parsed[0]) if fm_parsed else {}
            status = rec.get("review_status")
            claim_state = str(fm_vals.get("claim_state") or rec.get("claim_state") or "").strip().lower()
            latest_action = str((latest_decisions.get(entry.name) or {}).get("action") or "").strip().lower()
            if (
                status in ("rejected", "quarantined")
                or claim_state in ("rejected", "quarantined")
                or latest_action in ("reject", "quarantine")
            ):
                continue
            sup_list = list(rec.get("superseded_by") or [])
            is_approved_current = bool(
                status == "accepted"
                and not sup_list
                and approved_revision_is_current(path, brain_dir=brain_dir, atoms_dir=atoms_dir)
            )
            if not include_pending and not is_approved_current:
                continue
            contexts = list(rec.get("contexts") or [])
            if not _matches_scope(contexts, project_scope):
                continue
            raw_links = fm_vals.get("links", "")
            fm_links = (
                [str(x).strip() for x in raw_links if str(x).strip()]
                if isinstance(raw_links, list)
                else parse_scalar_list(str(raw_links or ""))
            )
            links_dedup: list[str] = []
            seen_links: set[str] = set()
            for raw_target in [*fm_links, *wikilinks(raw)]:
                cleaned = str(raw_target or "").strip()
                if cleaned.startswith("[[") and cleaned.endswith("]]"):
                    cleaned = cleaned[2:-2].split("|", 1)[0].strip()
                if cleaned and cleaned not in seen_links:
                    seen_links.add(cleaned)
                    links_dedup.append(cleaned)
            links = links_dedup
            raw_supersedes = fm_vals.get("supersedes", "")
            supersedes_list = (
                [str(x).strip() for x in raw_supersedes if str(x).strip()]
                if isinstance(raw_supersedes, list)
                else parse_scalar_list(str(raw_supersedes or ""))
            )
            superseded_by = sup_list[0] if sup_list else ""
            claim_text = rec.get("claim") or ""
            atom_id = rec.get("id") or Path(entry.name).stem
            effective_status = (
                "unverified_accepted"
                if (status == "accepted" and not sup_list and not is_approved_current)
                else str(status or "")
            )
            node = {
                "atom_id": atom_id,
                "filename": rec.get("filename", entry.name),
                "review_status": rec.get("review_status", ""),
                "effective_status": effective_status,
                "approved_current": is_approved_current,
                "memory_type": rec.get("memory_type", ""),
                "contexts": contexts,
                "links": links,
                "supersedes": supersedes_list,
                "superseded_by": superseded_by,
                "claim": claim_text,
                "negative_polarity": _is_negative_claim(claim_text),
                "numbers": sorted(_extract_numbers(claim_text)),
                "topic_tokens": sorted(_topic_tokens(claim_text)),
            }
            nodes.append(node)
            by_id[atom_id] = node
            by_id[node["filename"]] = node

    def _resolve_target(target_ref: str) -> dict[str, Any] | None:
        if target_ref in by_id:
            return by_id[target_ref]
        if not target_ref.lower().endswith(".md") and f"{target_ref}.md" in by_id:
            return by_id[f"{target_ref}.md"]
        if target_ref.lower().endswith(".md") and Path(target_ref).stem in by_id:
            return by_id[Path(target_ref).stem]
        return None

    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for node in nodes:
        for target in node["links"]:
            resolved = _resolve_target(target)
            if resolved and resolved["filename"] != node["filename"]:
                ek = (node["filename"], resolved["filename"], "link")
                if ek not in seen_edges:
                    seen_edges.add(ek)
                    edges.append({"from": node["filename"], "to": resolved["filename"], "kind": "link"})
        if node["superseded_by"]:
            resolved = _resolve_target(node["superseded_by"])
            if resolved and resolved["filename"] != node["filename"]:
                ek = (node["filename"], resolved["filename"], "superseded_by")
                if ek not in seen_edges:
                    seen_edges.add(ek)
                    edges.append({"from": node["filename"], "to": resolved["filename"], "kind": "superseded_by"})
        for old_ref in node.get("supersedes") or ():
            resolved = _resolve_target(old_ref)
            if resolved and resolved["filename"] != node["filename"]:
                ek = (resolved["filename"], node["filename"], "superseded_by")
                if ek not in seen_edges:
                    seen_edges.add(ek)
                    edges.append({"from": resolved["filename"], "to": node["filename"], "kind": "superseded_by"})

    # Compute topic-similarity edges
    for i in range(len(nodes)):
        toks_a = set(nodes[i]["topic_tokens"])
        if not toks_a:
            continue
        for j in range(i + 1, len(nodes)):
            toks_b = set(nodes[j]["topic_tokens"])
            if not toks_b:
                continue
            shared = toks_a & toks_b
            union = toks_a | toks_b
            jaccard = len(shared) / len(union) if union else 0.0
            if jaccard >= 0.35 or len(shared) >= 3:
                edges.append(
                    {
                        "from": nodes[i]["filename"],
                        "to": nodes[j]["filename"],
                        "kind": "topic_overlap",
                        "jaccard": round(jaccard, 3),
                        "shared_terms": sorted(shared)[:8],
                    }
                )

    linked_files = {e["from"] for e in edges if e["kind"] == "link"} | {e["to"] for e in edges if e["kind"] == "link"}
    orphans = [n["filename"] for n in nodes if n.get("approved_current") and n["filename"] not in linked_files]
    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "orphan_approved_atoms": orphans,
        "nodes": nodes,
        "edges": edges,
    }


def detect_contradictions(
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    project_scope: str | None = None,
    include_pending: bool = True,
) -> dict[str, Any]:
    """Detect polarity and numeric contradictions among active (non-superseded) atoms."""
    graph = build_knowledge_graph(
        brain_dir=brain_dir,
        atoms_dir=atoms_dir,
        project_scope=project_scope,
        include_pending=include_pending,
    )
    active = [n for n in graph["nodes"] if not n["superseded_by"]]
    alerts: list[dict[str, Any]] = []

    for i in range(len(active)):
        a = active[i]
        toks_a = set(a["topic_tokens"])
        if len(toks_a) < 2:
            continue
        for j in range(i + 1, len(active)):
            b = active[j]
            toks_b = set(b["topic_tokens"])
            shared = sorted(toks_a & toks_b)
            union = toks_a | toks_b
            jaccard = len(shared) / len(union) if union else 0.0
            if jaccard < 0.30 and len(shared) < 3:
                continue

            reasons: list[str] = []
            if a["negative_polarity"] != b["negative_polarity"]:
                reasons.append("polarity_conflict")

            nums_a, nums_b = set(a["numbers"]), set(b["numbers"])
            if nums_a and nums_b and nums_a != nums_b:
                reasons.append("numeric_value_conflict")

            if not reasons:
                continue

            both_accepted = bool(a.get("approved_current") and b.get("approved_current"))
            alerts.append(
                {
                    "severity": "high" if both_accepted else "medium",
                    "reasons": reasons,
                    "jaccard": round(jaccard, 3),
                    "shared_terms": shared[:10],
                    "atom_a": {
                        "filename": a["filename"],
                        "review_status": a["review_status"],
                        "approved_current": bool(a.get("approved_current")),
                        "claim": a["claim"],
                    },
                    "atom_b": {
                        "filename": b["filename"],
                        "review_status": b["review_status"],
                        "approved_current": bool(b.get("approved_current")),
                        "claim": b["claim"],
                    },
                    "recommended_action": "owner_review_merge_or_demote",
                }
            )

    alerts.sort(key=lambda item: (0 if item["severity"] == "high" else 1, -item["jaccard"]))
    return {
        "contradiction_count": len(alerts),
        "high_severity_count": sum(1 for al in alerts if al["severity"] == "high"),
        "orphan_approved_count": len(graph["orphan_approved_atoms"]),
        "alerts": alerts,
        "orphan_approved_atoms": graph["orphan_approved_atoms"],
    }


def graph_neighbors(
    atom_filename: str,
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    include_pending: bool = False,
) -> dict[str, Any]:
    """Return 1-hop neighbors and contradictions involving a specific atom."""
    graph = build_knowledge_graph(brain_dir=brain_dir, atoms_dir=atoms_dir, include_pending=include_pending)
    radar = detect_contradictions(brain_dir=brain_dir, atoms_dir=atoms_dir, include_pending=include_pending)
    node = next((n for n in graph["nodes"] if n["filename"] == atom_filename or n["atom_id"] == atom_filename), None)
    if node is None:
        raise FileNotFoundError(f"Atom not found in graph: {atom_filename}")
    fname = node["filename"]
    neighbors = [e for e in graph["edges"] if e["from"] == fname or e["to"] == fname]
    related_alerts = [
        al for al in radar["alerts"] if al["atom_a"]["filename"] == fname or al["atom_b"]["filename"] == fname
    ]
    return {
        "atom": fname,
        "edge_count": len(neighbors),
        "edges": neighbors,
        "contradictions": related_alerts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Contradiction & Drift Radar + Knowledge Graph")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_c = sub.add_parser("contradictions", help="Scan for claim polarity & numeric contradictions")
    p_c.add_argument("--project", default=None)
    p_c.add_argument("--accepted-only", action="store_true")
    p_c.add_argument("--json", action="store_true")

    p_s = sub.add_parser("summary", help="Show knowledge graph node/edge/orphan summary")
    p_s.add_argument("--project", default=None)
    p_s.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "contradictions":
        res = detect_contradictions(project_scope=args.project, include_pending=not args.accepted_only)
        print(json.dumps(res, indent=2) if args.json else f"contradictions={res['contradiction_count']} high={res['high_severity_count']}")
        return 0
    if args.cmd == "summary":
        res = build_knowledge_graph(project_scope=args.project, include_pending=False)
        out = {k: v for k, v in res.items() if k not in {"nodes", "edges"}}
        print(json.dumps(out, indent=2) if args.json else f"nodes={out['node_count']} edges={out['edge_count']} orphans={len(out['orphan_approved_atoms'])}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
