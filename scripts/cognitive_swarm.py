# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Internal Cognitive Swarm & Two-Sided Debate Mode (Epic 2D.5).

Runs structured internal specialist passes (Planner -> Knowledge -> Logic ->
Adversarial Critic) strictly grounded in approved vault atoms, active playbooks,
active principles, and episodic experience records, plus Two-Sided Debate Mode
(Thesis vs Antithesis briefs with owner adjudication options: Keep Existing,
Supersede, or Context-Partition). Never mutates canonical stores autonomously.
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
from collections.abc import Callable
import datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

if __name__ in ("cognitive_swarm", "scripts.cognitive_swarm"):
    sys.modules.setdefault("cognitive_swarm", sys.modules[__name__])
    sys.modules.setdefault("scripts.cognitive_swarm", sys.modules[__name__])

from scripts.config import (  # noqa: E402
    ATOMS_DIR,
    BRAIN_DIR,
    _is_reparse_or_link,
    is_content_markdown,
    resolve_module_atoms_and_brain,
    snapshot_module_defaults,
)
from scripts.knowledge_graph import detect_contradictions  # noqa: E402
from scripts.memory_format import evaluate_temporal_validity, split_frontmatter  # noqa: E402
from scripts.memory_review import parse_atom_record_text, parse_frontmatter_values, parse_scalar_list  # noqa: E402
from scripts.meta_router import classify_query, route_and_retrieve  # noqa: E402
from scripts.recall import normalize_project_tag, project_tags_match  # noqa: E402
from scripts.review_atom import approved_revision_is_current  # noqa: E402

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())

_NEGATION_RE = re.compile(
    r"\b(?:never|not|avoid|forbid|prohibit|disable[sd]?|bypass(?:es|ing)?|skip(?:s|ping)?|omit(?:s|ted)?|without|cannot|no)\b",
    re.IGNORECASE,
)


def _extract_alert_filename(val: Any) -> str:
    if isinstance(val, dict):
        return str(val.get("filename") or "").strip()
    return str(val or "").strip()


def _extract_alert_reason(pair: dict[str, Any], default: str) -> str:
    if pair.get("reason"):
        return str(pair["reason"])
    reasons = pair.get("reasons")
    if isinstance(reasons, (list, tuple)) and reasons:
        return ", ".join(str(r) for r in reasons if r)
    return default


def _projects_strictly_within(contexts: tuple[str, ...] | list[str], project: str | None) -> bool:
    if not project:
        return True
    scope_tag = normalize_project_tag(project)
    if not scope_tag:
        return True
    atom_projs = [
        normalize_project_tag(str(c))
        for c in (contexts or ())
        if str(c).strip().lower().startswith("project:") and normalize_project_tag(str(c))
    ]
    if not atom_projs:
        return False
    return all(bool(project_tags_match((p,), (scope_tag,))) for p in atom_projs)


def _is_preference_or_inactive(rec: dict[str, Any]) -> bool:
    ep_label = str(rec.get("epistemic_label") or "claim").strip().lower()
    ep_contract = str(rec.get("epistemic_contract") or "").strip()
    if ep_label == "preference" or bool(ep_contract):
        return True
    tv = evaluate_temporal_validity(rec.get("valid_from"), rec.get("valid_until"))
    return tv["status"] == "invalid_window" or not tv["active"]


def _result_allowed_for_swarm(item: Any, project: str | None) -> bool:
    fn = str(getattr(item, "filename", None) or (item.get("filename") if isinstance(item, dict) else "atom.md"))
    content = str(getattr(item, "content", None) or (item.get("content") if isinstance(item, dict) else "") or "")
    parsed_rec: dict[str, Any] | None = None
    if content:
        try:
            parsed_rec = parse_atom_record_text(fn, content)
        except ValueError:
            parsed_rec = None
    if parsed_rec is not None:
        if _is_preference_or_inactive(parsed_rec):
            return False
        if project and not _projects_strictly_within(tuple(parsed_rec.get("contexts") or ()), project):
            return False
        return True

    # Fallback attribute check for lightweight dict/namespace results
    ep_label = str(
        getattr(item, "epistemic_label", None)
        or (item.get("epistemic_label") if isinstance(item, dict) else "")
        or "claim"
    ).strip().lower()
    ep_contract = str(
        getattr(item, "epistemic_contract", None)
        or (item.get("epistemic_contract") if isinstance(item, dict) else "")
        or ""
    ).strip()
    if ep_label == "preference" or bool(ep_contract):
        return False
    vf = getattr(item, "valid_from", None) if not isinstance(item, dict) else item.get("valid_from")
    vu = getattr(item, "valid_until", None) if not isinstance(item, dict) else item.get("valid_until")
    if vf or vu:
        tv = evaluate_temporal_validity(vf, vu)
        if tv["status"] == "invalid_window" or not tv["active"]:
            return False
    ctxs = getattr(item, "contexts", None) if not isinstance(item, dict) else item.get("contexts")
    if project and ctxs is not None and not _projects_strictly_within(tuple(ctxs), project):
        return False
    return True


def _extract_experience_projects(vals: dict[str, Any]) -> list[str]:
    projs: list[str] = []
    for key in ("project", "projects", "context", "contexts", "tags"):
        raw_v = vals.get(key)
        if isinstance(raw_v, str):
            items = parse_scalar_list(raw_v) if raw_v.strip().startswith("[") else [raw_v]
        elif isinstance(raw_v, (list, tuple)):
            items = list(raw_v)
        else:
            items = []
        for item in items:
            s = str(item or "").strip()
            if not s:
                continue
            if key in {"project", "projects"} and not s.lower().startswith("project:"):
                s = f"project:{s}"
            if s.lower().startswith("project:"):
                norm = normalize_project_tag(s)
                if norm and norm not in projs:
                    projs.append(norm)
    return sorted(projs)


def _extract_claim_from_result(item: Any) -> tuple[str, str]:
    fn = str(getattr(item, "filename", None) or (item.get("filename") if isinstance(item, dict) else "atom.md"))
    raw_claim = str(getattr(item, "claim", None) or (item.get("claim") if isinstance(item, dict) else "") or "").strip()
    if raw_claim:
        return fn, raw_claim
    content = str(getattr(item, "content", None) or (item.get("content") if isinstance(item, dict) else "") or "")
    if content:
        try:
            rec = parse_atom_record_text(fn, content)
            if rec.get("claim"):
                return fn, str(rec["claim"]).strip()
        except ValueError:
            pass
    return fn, (content.strip()[:240] or fn)


def run_cognitive_swarm(
    goal: str,
    *,
    project: str | None = None,
    reasoning_profile: str = "auto",
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
    lexical_index_path: str | Path | None = None,
    chroma_path: str | Path | None = None,
    retrieve_fn: Callable[[str, int, str | None], tuple[list[Any], list[Any]]] | None = None,
) -> dict[str, Any]:
    """Run Planner -> Knowledge -> Logic -> Adversarial Critic over approved vault knowledge."""
    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    budget = classify_query(goal, reasoning_profile=reasoning_profile)

    # Role 1: Planner
    clauses = [c.strip() for c in re.split(r"\band\b|;|,", goal or "") if len(c.strip()) >= 4]
    sub_questions = clauses[:4] if clauses else [goal.strip()]
    planner_brief = {
        "role": "planner",
        "complexity": budget.complexity,
        "reasoning_mode": budget.reasoning_mode,
        "sub_questions": sub_questions,
    }

    # Role 2: Knowledge Gatherer (strictly approved/current atoms + active principles)
    routed = route_and_retrieve(
        goal,
        project=project,
        reasoning_profile=reasoning_profile,
        brain_dir=resolved_brain,
        atoms_dir=resolved_atoms,
        lexical_index_path=lexical_index_path,
        chroma_path=chroma_path,
        retrieve_fn=retrieve_fn,
    )
    filtered_results = [r for r in routed["results"] if _result_allowed_for_swarm(r, project)]
    citations: list[dict[str, str]] = []
    for idx, res in enumerate(filtered_results[: budget.n_results], start=1):
        fn, claim = _extract_claim_from_result(res)
        citations.append({"citation_id": f"V{idx}", "filename": fn, "claim": claim})

    active_principles: list[dict[str, Any]] = []
    try:
        from scripts.cognitive_distiller import list_distilled_artifacts

        active_principles = list_distilled_artifacts(
            kind="principle",
            project=project,
            active_only=True,
            brain_dir=resolved_brain,
            atoms_dir=resolved_atoms,
        )[:4]
    except Exception:
        active_principles = []

    knowledge_brief = {
        "role": "knowledge",
        "atom_citations": citations,
        "active_principles": [{"filename": p["filename"], "title": p["title"], "statement": p["statement"]} for p in active_principles],
        "coverage": routed["coverage"],
    }

    # Role 3: Logic Checker
    logic_notes: list[str] = []
    if citations:
        logic_notes.append(f"Grounded in {len(citations)} currently approved vault atom(s) with coverage={routed['coverage']:.2f}.")
    else:
        logic_notes.append("Insufficient approved vault evidence; abstention or owner confirmation required.")
    if active_principles:
        logic_notes.append(f"Cross-checked against {len(active_principles)} owner-promoted principle(s).")
    logic_brief = {
        "role": "logic",
        "notes": logic_notes,
        "can_answer": bool(citations),
    }

    # Role 4: Adversarial Critic (scans contradictions in retrieved scope)
    try:
        contra_report = detect_contradictions(
            project_scope=project,
            include_pending=False,
            atoms_dir=resolved_atoms,
            brain_dir=resolved_brain,
        )
        alerts = list(contra_report.get("alerts") or contra_report.get("contradictions") or ())
    except Exception:
        alerts = []
    cited_names = {c["filename"] for c in citations}
    relevant_alerts = [
        a for a in alerts
        if cited_names
        and (
            _extract_alert_filename(a.get("atom_a")) in cited_names
            or _extract_alert_filename(a.get("atom_b")) in cited_names
        )
    ]
    critic_brief = {
        "role": "critic",
        "contradiction_warnings": relevant_alerts[:5],
        "abstain_recommended": len(citations) == 0,
        "verdict": "caution_contradictions" if relevant_alerts else ("grounded" if citations else "insufficient_evidence"),
    }

    if citations:
        synthesis = " ".join(f"[{c['citation_id']}] {c['claim']}" for c in citations[:4])
    else:
        synthesis = "Abstaining: no verified approved memories support a grounded answer to this query."

    return {
        "goal": goal,
        "project": project,
        "budget": routed["budget"],
        "roles": {
            "planner": planner_brief,
            "knowledge": knowledge_brief,
            "logic": logic_brief,
            "critic": critic_brief,
        },
        "synthesis": synthesis,
        "abstained": len(citations) == 0,
    }


def run_two_sided_debate(
    topic_or_query: str,
    *,
    project: str | None = None,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Assemble a cited Thesis vs Antithesis Debate Brief for owner adjudication."""
    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    bpath = Path(resolved_brain)
    apath = Path(resolved_atoms)
    q_tokens = {
        tok.lower()
        for tok in re.findall(r"[a-zA-Z0-9_-]{3,}", topic_or_query or "")
        if len(tok) >= 3
    }

    # Load all approved matching atoms + contradiction pairs
    matching_atoms: list[dict[str, Any]] = []
    if apath.is_dir() and not _is_reparse_or_link(str(apath)):
        for entry in sorted(os.scandir(apath), key=lambda e: e.name):
            if not is_content_markdown(entry.name):
                continue
            fpath = apath / entry.name
            if not fpath.is_file() or _is_reparse_or_link(str(fpath)):
                continue
            try:
                raw = fpath.read_text(encoding="utf-8")
                rec = parse_atom_record_text(entry.name, raw)
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            if rec.get("review_status") != "accepted" or rec.get("superseded_by"):
                continue
            if not approved_revision_is_current(str(fpath), brain_dir=resolved_brain, atoms_dir=resolved_atoms):
                continue
            if _is_preference_or_inactive(rec):
                continue
            ctxs = tuple(str(c) for c in (rec.get("contexts") or ()))
            if project and not _projects_strictly_within(ctxs, project):
                continue
            claim = str(rec.get("claim") or "").strip()
            overlap = sum(1 for t in q_tokens if t in claim.lower() or t in entry.name.lower())
            if not q_tokens or overlap > 0:
                matching_atoms.append(
                    {
                        "filename": entry.name,
                        "claim": claim,
                        "confidence": str(rec.get("confidence") or ""),
                        "contexts": list(ctxs),
                        "negated": bool(_NEGATION_RE.search(claim)),
                        "overlap": overlap,
                    }
                )

    contra_rep = detect_contradictions(
        project_scope=project,
        include_pending=False,
        atoms_dir=resolved_atoms,
        brain_dir=resolved_brain,
    )
    raw_contras = list(contra_rep.get("alerts") or contra_rep.get("contradictions") or ())
    matching_names = {a["filename"] for a in matching_atoms}
    relevant_contras = [
        c for c in raw_contras
        if matching_names
        and (
            _extract_alert_filename(c.get("atom_a")) in matching_names
            or _extract_alert_filename(c.get("atom_b")) in matching_names
        )
    ]

    thesis_claims: list[dict[str, Any]] = []
    antithesis_claims: list[dict[str, Any]] = []
    seen_t: set[str] = set()
    seen_a: set[str] = set()
    atom_by_name = {a["filename"]: a for a in matching_atoms}

    for pair in relevant_contras:
        a_fn = _extract_alert_filename(pair.get("atom_a"))
        b_fn = _extract_alert_filename(pair.get("atom_b"))
        if a_fn in atom_by_name and a_fn not in seen_t:
            thesis_claims.append(
                {**atom_by_name[a_fn], "role_reason": _extract_alert_reason(pair, "contested claim A")}
            )
            seen_t.add(a_fn)
        if b_fn in atom_by_name and b_fn not in seen_a:
            antithesis_claims.append(
                {**atom_by_name[b_fn], "role_reason": _extract_alert_reason(pair, "contested claim B")}
            )
            seen_a.add(b_fn)

    for atom in sorted(matching_atoms, key=lambda x: (-x["overlap"], x["filename"])):
        fn = atom["filename"]
        if fn in seen_t or fn in seen_a:
            continue
        if atom["negated"] and len(antithesis_claims) <= len(thesis_claims):
            antithesis_claims.append({**atom, "role_reason": "cautionary/negative constraint"})
            seen_a.add(fn)
        else:
            thesis_claims.append({**atom, "role_reason": "affirmative/primary claim"})
            seen_t.add(fn)

    # Pull historical failure warnings from 4-experiences/
    failure_evidence: list[dict[str, str]] = []
    exp_dir = bpath / "4-experiences"
    if exp_dir.is_dir() and not _is_reparse_or_link(str(exp_dir)):
        for entry in sorted(os.scandir(exp_dir), key=lambda e: e.name):
            if not is_content_markdown(entry.name):
                continue
            ep = exp_dir / entry.name
            if _is_reparse_or_link(str(ep)) or not ep.is_file():
                continue
            try:
                txt = ep.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            parsed = split_frontmatter(txt)
            vals = parse_frontmatter_values(parsed[0]) if parsed else {}
            if project:
                exp_projs = _extract_experience_projects(vals)
                if not _projects_strictly_within(exp_projs, project):
                    continue
            succ_raw = str(vals.get("success") or "").strip().lower()
            is_failure = succ_raw in {"false", "0", "no"}
            if is_failure and (not q_tokens or any(t in txt.lower() for t in q_tokens)):
                goal_line = str(vals.get("goal") or (txt.splitlines()[0] if txt.splitlines() else entry.name))[:120]
                failure_evidence.append({"experience": entry.name, "excerpt": goal_line})

    return {
        "topic": topic_or_query,
        "project": project,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "thesis": {
            "label": "Thesis (Primary / Supporting Evidence)",
            "claims": thesis_claims,
        },
        "antithesis": {
            "label": "Antithesis (Conflicting / Cautionary Evidence)",
            "claims": antithesis_claims,
            "related_failures": failure_evidence[:5],
        },
        "contradictions_detected": relevant_contras,
        "owner_adjudication_actions": [
            {
                "action": "keep_existing",
                "label": "Keep Existing",
                "description": "Retain current approved claims as valid within distinct scopes.",
            },
            {
                "action": "supersede",
                "label": "Supersede",
                "description": "Merge/supersede outdated atom into the stronger survivor via Owner CAS.",
            },
            {
                "action": "context_partition",
                "label": "Context-Partition",
                "description": "Set bi-temporal bounds or project/environment context tags on each atom.",
            },
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Second Brain Cognitive Swarm & Two-Sided Debate")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_s = sub.add_parser("swarm", help="Run internal cognitive swarm")
    p_s.add_argument("goal")
    p_s.add_argument("--project", default=None)
    p_s.add_argument("--json", action="store_true")

    p_d = sub.add_parser("debate", help="Run two-sided debate brief")
    p_d.add_argument("topic")
    p_d.add_argument("--project", default=None)
    p_d.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "swarm":
        res = run_cognitive_swarm(args.goal, project=args.project)
        print(json.dumps(res, indent=2) if args.json else res["synthesis"])
        return 0
    res = run_two_sided_debate(args.topic, project=args.project)
    print(json.dumps(res, indent=2) if args.json else f"thesis={len(res['thesis']['claims'])} antithesis={len(res['antithesis']['claims'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
