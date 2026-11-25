# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Self-Model, Long-Term Goal Hierarchy & Directed Curiosity Engine (Epic 2D.6).

Tracks owner-authored learning goals in ``brain/self-model.md``, computes per-domain
competence metrics in disposable ``brain/.sidecar/self_model.sqlite3`` (atom count,
active playbooks, 4-experiences correction/failure rate), detects vault coverage
imbalances relative to ambitions, and queues non-mutating Study / Scout targets.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ in ("self_model_curiosity", "scripts.self_model_curiosity"):
    sys.modules.setdefault("self_model_curiosity", sys.modules[__name__])
    sys.modules.setdefault("scripts.self_model_curiosity", sys.modules[__name__])

from atom_mutator import atomic_write_bytes  # noqa: E402
from config import (  # noqa: E402
    ATOMS_DIR,
    BRAIN_DIR,
    _is_reparse_or_link,
    is_content_markdown,
    resolve_module_atoms_and_brain,
    snapshot_module_defaults,
)
from memory_format import evaluate_temporal_validity, split_frontmatter  # noqa: E402
from memory_review import parse_atom_record_text, parse_frontmatter_values, parse_scalar_list  # noqa: E402
from recall import normalize_project_tag, project_tags_match  # noqa: E402
from review_atom import approved_revision_is_current  # noqa: E402

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())
_GOAL_LINE_RE = re.compile(
    r"^\s*-\s+\[(?P<id>[a-zA-Z0-9_-]+)\]\s+(?P<title>.+?)\s+\(domain:\s*(?P<domain>[^,)]+)(?:,\s*target_atoms:\s*(?P<target>\d+))?(?:,\s*parent:\s*(?P<parent>[^,)]+))?(?:,\s*project:\s*(?P<project>[^)]+))?\)\s*$"
)


def self_model_md_path(brain_dir: str | Path | None = None) -> Path:
    resolved_brain, _ = resolve_module_atoms_and_brain(globals(), brain_dir=brain_dir)
    return Path(resolved_brain) / "self-model.md"


def self_model_db_path(brain_dir: str | Path | None = None) -> Path:
    resolved_brain, _ = resolve_module_atoms_and_brain(globals(), brain_dir=brain_dir)
    return Path(resolved_brain) / ".sidecar" / "self_model.sqlite3"


def _connect(dbp: Path) -> sqlite3.Connection:
    if os.path.lexists(str(dbp.parent)) and _is_reparse_or_link(str(dbp.parent)):
        raise PermissionError(f"Refusing symlinked sidecar dir: {dbp.parent}")
    dbp.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(str(dbp)) and _is_reparse_or_link(str(dbp)):
        raise PermissionError(f"Refusing symlinked self_model db: {dbp}")
    conn = sqlite3.connect(str(dbp), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS domain_competence (
            domain TEXT PRIMARY KEY,
            atom_count INTEGER NOT NULL,
            playbook_count INTEGER NOT NULL,
            failure_count INTEGER NOT NULL,
            competence_score REAL NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    return conn


def _domain_matches_text(dom: str, text_low: str) -> bool:
    if not dom or not text_low:
        return False
    return bool(re.search(rf"\b{re.escape(dom)}\b", text_low))


def _strictly_matches_project(items: Any, scope_tag: str) -> bool:
    if not scope_tag:
        return True
    proj_tags = [str(c).strip() for c in (items or ()) if str(c).strip().lower().startswith("project:")]
    if not proj_tags:
        return False
    return all(bool(project_tags_match((t,), (scope_tag,))) for t in proj_tags)


def _extract_experience_projects(efm: dict[str, Any]) -> list[str]:
    projs: list[str] = []
    for key in ("project", "projects", "context", "contexts", "tags"):
        raw_v = efm.get(key)
        if isinstance(raw_v, str):
            raw_s = raw_v.strip()
            vals = parse_scalar_list(raw_s) if raw_s.startswith("[") else [raw_s]
        elif isinstance(raw_v, (list, tuple)):
            vals = list(raw_v)
        else:
            vals = []
        for item in vals:
            s = str(item or "").strip()
            if not s:
                continue
            if key in ("project", "projects") or s.lower().startswith("project:"):
                norm = normalize_project_tag(s)
                if norm and norm not in projs:
                    projs.append(norm)
    return projs


def _goal_project_tag(goal: dict[str, Any]) -> str:
    raw_p = str(goal.get("project") or "").strip()
    if raw_p:
        return normalize_project_tag(raw_p)
    for field in ("domain", "parent"):
        val = str(goal.get(field) or "").strip()
        if val.lower().startswith("project:"):
            return normalize_project_tag(val)
    return ""


def upsert_learning_goals(
    goals: list[dict[str, Any]],
    *,
    brain_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Write or replace owner-authored Goal Hierarchy DAG lines in brain/self-model.md."""
    p = self_model_md_path(brain_dir)
    if os.path.lexists(str(p)) and _is_reparse_or_link(str(p)):
        raise PermissionError(f"Refusing symlinked self-model.md: {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Owner Self-Model & Goal Hierarchy",
        "",
        "## Long-Term Learning & Engineering Goals",
    ]
    clean_goals = []
    for g in goals or ():
        gid = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(g.get("id") or g.get("title") or "goal").strip().lower()).strip("-")[:48]
        title = str(g.get("title") or gid).strip().replace("\n", " ")[:180]
        domain = str(g.get("domain") or "general").strip().lower()[:64]
        target_atoms = max(1, int(g.get("target_atoms") or 10))
        parent = str(g.get("parent") or "root").strip()[:48]
        raw_proj = str(g.get("project") or "").strip()[:120]
        proj_tag = normalize_project_tag(raw_proj) if raw_proj else ""
        proj_suffix = f", project: {proj_tag}" if proj_tag else ""
        lines.append(
            f"- [{gid}] {title} (domain: {domain}, target_atoms: {target_atoms}, parent: {parent}{proj_suffix})"
        )
        goal_obj: dict[str, Any] = {
            "id": gid,
            "title": title,
            "domain": domain,
            "target_atoms": target_atoms,
            "parent": parent,
        }
        if proj_tag:
            goal_obj["project"] = proj_tag
        clean_goals.append(goal_obj)
    atomic_write_bytes(str(p), ("\n".join(lines).rstrip() + "\n").encode("utf-8"))
    return {"status": "saved", "path": str(p), "goal_count": len(clean_goals), "goals": clean_goals}


def load_learning_goals(*, brain_dir: str | Path | None = None) -> list[dict[str, Any]]:
    p = self_model_md_path(brain_dir)
    if not p.is_file() or _is_reparse_or_link(str(p)):
        return []
    goals = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _GOAL_LINE_RE.match(line)
        if m:
            item: dict[str, Any] = {
                "id": m.group("id").strip(),
                "title": m.group("title").strip(),
                "domain": m.group("domain").strip().lower(),
                "target_atoms": int(m.group("target") or 10),
                "parent": (m.group("parent") or "root").strip(),
            }
            raw_proj = (m.group("project") or "").strip()
            if raw_proj:
                norm_p = normalize_project_tag(raw_proj)
                if norm_p:
                    item["project"] = norm_p
            goals.append(item)
    return goals


def evaluate_self_model_and_curiosity(
    *,
    project: str | None = None,
    include_private_goals: bool = True,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Compute per-domain competence scores & prioritize directed curiosity study targets."""
    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    bpath = Path(resolved_brain)
    apath = Path(resolved_atoms)
    scope_tag = normalize_project_tag(project) if (project and str(project).strip()) else ""
    proj_slug = scope_tag.removeprefix("project:").lower() if scope_tag else ""
    raw_goals = load_learning_goals(brain_dir=resolved_brain)

    domain_atoms: dict[str, int] = {}
    if apath.is_dir() and not _is_reparse_or_link(str(apath)):
        for entry in sorted(os.scandir(apath), key=lambda e: e.name):
            if not is_content_markdown(entry.name):
                continue
            p = apath / entry.name
            if not p.is_file() or _is_reparse_or_link(str(p)):
                continue
            try:
                raw = p.read_text(encoding="utf-8")
                rec = parse_atom_record_text(entry.name, raw)
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            if rec.get("review_status") != "accepted" or rec.get("superseded_by") or not approved_revision_is_current(
                str(p), brain_dir=resolved_brain, atoms_dir=resolved_atoms
            ):
                continue
            ep_label = str(rec.get("epistemic_label") or "claim").strip().lower()
            ep_contract = str(rec.get("epistemic_contract") or "").strip()
            if ep_label == "preference" or bool(ep_contract):
                continue
            tv = evaluate_temporal_validity(rec.get("valid_from"), rec.get("valid_until"))
            if tv["status"] == "invalid_window" or not tv["active"]:
                continue
            parsed = split_frontmatter(raw)
            fm_vals = parse_frontmatter_values(parsed[0]) if parsed else {}
            tags = parse_scalar_list(fm_vals.get("tags", [])) if isinstance(fm_vals.get("tags"), str) else list(fm_vals.get("tags") or [])
            if scope_tag and not _strictly_matches_project([*(rec.get("contexts") or ()), *tags], scope_tag):
                continue
            domains = {
                str(c).strip().lower()
                for c in [*(rec.get("contexts") or ()), *tags]
                if str(c).strip() and not str(c).lower().startswith("project:")
            }
            for dom in domains or {"general"}:
                domain_atoms[dom] = domain_atoms.get(dom, 0) + 1

    # Filter goals strictly BEFORE counting playbooks, failures, domains, and curiosity targets.
    goals: list[dict[str, Any]] = []
    for g in raw_goals:
        g_proj = _goal_project_tag(g)
        if not include_private_goals and not g_proj:
            continue
        if scope_tag:
            if g_proj:
                if not project_tags_match((g_proj,), (scope_tag,)):
                    continue
            else:
                if g["domain"] not in domain_atoms and g["domain"] != proj_slug:
                    continue
        goals.append(g)

    candidate_domains = set(domain_atoms) | {g["domain"] for g in goals}

    domain_pbs: dict[str, int] = {}
    try:
        from skill_playbooks import list_playbooks, playbook_visible_in_scope

        for pb in list_playbooks(brain_dir=resolved_brain, atoms_dir=resolved_atoms, active_only=True):
            if scope_tag:
                if not playbook_visible_in_scope(pb, (scope_tag,)):
                    continue
                if not all(_strictly_matches_project(ctx, scope_tag) for ctx in (pb.backing_atom_contexts or ())):
                    continue
            tok = (pb.title or pb.filename).lower()
            for dom in candidate_domains:
                if _domain_matches_text(dom, tok):
                    domain_pbs[dom] = domain_pbs.get(dom, 0) + 1
    except Exception:
        pass

    domain_failures: dict[str, int] = {}
    exp_dir = bpath / "4-experiences"
    if exp_dir.is_dir() and not _is_reparse_or_link(str(exp_dir)):
        for entry in sorted(os.scandir(exp_dir), key=lambda e: e.name):
            if not is_content_markdown(entry.name):
                continue
            ep = exp_dir / entry.name
            if not ep.is_file() or _is_reparse_or_link(str(ep)):
                continue
            try:
                raw_exp = ep.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            eparsed = split_frontmatter(raw_exp)
            efm = parse_frontmatter_values(eparsed[0]) if eparsed else {}
            if scope_tag:
                exp_projs = _extract_experience_projects(efm)
                if not exp_projs or not all(bool(project_tags_match((p,), (scope_tag,))) for p in exp_projs):
                    continue
            succ_raw = str(efm.get("success") or "").strip().lower()
            is_failure = succ_raw in {"false", "0", "no"}
            if is_failure:
                txt = raw_exp.lower()
                for dom in candidate_domains:
                    if _domain_matches_text(dom, txt):
                        domain_failures[dom] = domain_failures.get(dom, 0) + 1

    all_domains = sorted(candidate_domains)
    domain_profiles: list[dict[str, Any]] = []
    now_iso = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    for dom in all_domains:
        ac = domain_atoms.get(dom, 0)
        pc = domain_pbs.get(dom, 0)
        fc = domain_failures.get(dom, 0)
        comp = max(0.0, min(1.0, round((min(ac, 12) / 12.0) * 0.7 + (min(pc, 3) / 3.0) * 0.3 - (0.08 * fc), 3)))
        domain_profiles.append(
            {
                "domain": dom,
                "atom_count": ac,
                "playbook_count": pc,
                "failure_count": fc,
                "competence_score": comp,
            }
        )

    # Only persist global vault competence snapshots; project-scoped or redacted views must not overwrite global DB.
    if not scope_tag and include_private_goals:
        dbp = self_model_db_path(resolved_brain)
        with closing(_connect(dbp)) as conn, conn:
            conn.execute("DELETE FROM domain_competence")
            for prof in domain_profiles:
                conn.execute(
                    """
                    INSERT INTO domain_competence(domain, atom_count, playbook_count, failure_count, competence_score, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        prof["domain"],
                        prof["atom_count"],
                        prof["playbook_count"],
                        prof["failure_count"],
                        prof["competence_score"],
                        now_iso,
                    ),
                )

    # Directed Curiosity: rank goal domains with largest coverage deficit
    curiosity_targets: list[dict[str, Any]] = []
    max_atom_count = max([1, *(domain_atoms.values())])
    for g in goals:
        ac = domain_atoms.get(g["domain"], 0)
        fc = domain_failures.get(g["domain"], 0)
        target_n = max(1, int(g["target_atoms"]))
        deficit = max(0, target_n - ac)
        priority = round(min(1.0, (deficit / target_n) * 0.75 + (min(fc, 3) / 3.0) * 0.25), 3)
        if deficit > 0 or fc > 0:
            curiosity_targets.append(
                {
                    "goal_id": g["id"],
                    "goal_title": g["title"],
                    "domain": g["domain"],
                    "current_atoms": ac,
                    "target_atoms": target_n,
                    "deficit": deficit,
                    "failure_count": fc,
                    "priority_score": priority,
                    "suggested_scout_query": f"{g['domain']} {g['title']} best practices failure modes",
                }
            )
    # Also surface stark domain imbalance even if no explicit goal matches
    for dom, ac in domain_atoms.items():
        if max_atom_count >= 5 and ac <= 1 and not any(c["domain"] == dom for c in curiosity_targets):
            curiosity_targets.append(
                {
                    "goal_id": f"imbalance-{dom}",
                    "goal_title": f"Strengthen sparse domain '{dom}'",
                    "domain": dom,
                    "current_atoms": ac,
                    "target_atoms": 5,
                    "deficit": 5 - ac,
                    "failure_count": domain_failures.get(dom, 0),
                    "priority_score": round(min(0.85, 1.0 - (ac / max_atom_count)), 3),
                    "suggested_scout_query": f"{dom} architecture fundamentals verified patterns",
                }
            )
    curiosity_targets.sort(key=lambda x: (-float(x["priority_score"]), x["domain"]))

    return {
        "project": scope_tag or None,
        "self_model_path": str(self_model_md_path(resolved_brain)),
        "goals": goals,
        "domains": domain_profiles,
        "curiosity_targets": curiosity_targets,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Second Brain Self-Model & Directed Curiosity")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    out = evaluate_self_model_and_curiosity()
    print(json.dumps(out, indent=2) if args.json else f"domains={len(out['domains'])} curiosity={len(out['curiosity_targets'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
