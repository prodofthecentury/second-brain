# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = [
#     "httpx>=0.28,<1",
# ]
# ///
"""Layer 5 Cognitive Core: Read-only Executive Planner grounded strictly in verified Playbooks and approved Atoms."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import datetime
import json
import math
import os
from pathlib import Path
import re
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ in ("executive_planner", "scripts.executive_planner"):
    sys.modules.setdefault("executive_planner", sys.modules[__name__])
    sys.modules.setdefault("scripts.executive_planner", sys.modules[__name__])

from atom_mutator import review_lock
from cognitive_core import peek_lexical_candidate_atom_ids
from config import (
    ATOMS_DIR,
    BRAIN_DIR,
    BRIEFINGS_DIR,
    CHROMA_PATH,
    LEXICAL_INDEX_PATH,
    PLAYBOOKS_DIR,
    TELEMETRY_DB_PATH,
    _is_reparse_or_link,
    is_content_markdown,
    safe_file_in_dir,
)
from evidence import verify_evidence_anchor
from host_adapters import (
    HostConsentError,
    HostSharingRule,
    OutboundSecretBlockedError,
    _audit_outbound_call,
    _invoke_provider_draft,
    _redact_outbound_with_map,
    filter_atoms_for_host,
    load_host_policies,
    validate_endpoint_network_boundary,
    validate_model_identifier,
)
from lexical_store import LexicalIndexUnavailable, TOKEN_PATTERN
from memory_format import split_frontmatter
from memory_review import parse_frontmatter_values, parse_scalar_list, section_text
from recall import (
    EXCLUDED_RECALL_STATUSES,
    QUERY_STOP_WORDS,
    MemoryRecallUnavailable,
    RecallOptions,
    RecallResult,
    assemble_bounded_working_context,
    normalize_project_tag,
    project_tags_match,
    search_memory,
)
from review_atom import approved_revision_is_current
from skill_playbooks import PlaybookIntegrityReport, list_playbooks, playbook_visible_in_scope

INSUFFICIENT_VERIFIED_MEMORY_FOR_PLAN = (
    "Cannot assemble a grounded executive plan: no verified active Playbooks or owner-approved "
    "memories match this goal. Capture and approve relevant procedural/factual sources first."
)

EXECUTIVE_PLAN_DRAFT_SCHEMA: dict[str, object] = {
    # Intentionally avoids maxLength/minLength (not portable across strict structured-output
    # providers); every returned step_id is whitelisted out-of-model, and missing_prerequisites
    # text is discarded.
    "type": "object",
    "properties": {
        "selected_step_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
        "missing_prerequisites": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["selected_step_ids", "missing_prerequisites"],
    "additionalProperties": False,
}

GEMINI_PLAN_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "OBJECT",
    "properties": {
        "selected_step_ids": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
        },
        "missing_prerequisites": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
        },
    },
    "required": ["selected_step_ids", "missing_prerequisites"],
}


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    kind: str  # "playbook_step" | "procedural_atom" | "semantic_constraint"
    instruction: str
    source_playbook_id: str | None = None
    backing_atom_ids: tuple[str, ...] = ()
    citations: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "kind": self.kind,
            "instruction": self.instruction,
            "source_playbook_id": self.source_playbook_id,
            "backing_atom_ids": list(self.backing_atom_ids),
            "citations": list(self.citations),
        }


@dataclass(frozen=True)
class ExecutivePlanRequest:
    goal: str
    project_scope: str | None = None
    allow_cross_project: bool = False
    active_context_profile: tuple[str, ...] = ()
    retrieval_mode: str = "lexical"
    include_preferences: bool = False
    mode: str = "deterministic"  # "deterministic" | "model_assisted"
    host_id: str = "ollama_local"
    model_override: str | None = None
    max_context_chars: int = 6000
    max_atoms: int = 6


@dataclass(frozen=True)
class ExecutivePlanResult:
    plan_ready: bool
    goal: str
    mode_used: str
    summary: str
    steps: tuple[PlanStep, ...] = ()
    matched_playbook_ids: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    rollback_checkpoints: tuple[str, ...] = ()
    preference_constraints: tuple[str, ...] = ()
    pending_candidate_count: int = 0
    epistemic_gaps: tuple[str, ...] = ()
    working_context_atom_ids: tuple[str, ...] = ()
    deferred_budget_atom_ids: tuple[str, ...] = ()
    saved_briefing_path: str | None = None

    def render_markdown(self) -> str:
        lines = [
            f"# Executive Plan: {self.goal}",
            "",
            f"- **Status:** {'READY (Grounded)' if self.plan_ready else 'ABSTAINED (Insufficient Verified Memory)'}",
            f"- **Mode:** `{self.mode_used}`",
            f"- **Summary:** {self.summary}",
            "",
        ]
        if not self.plan_ready:
            if self.epistemic_gaps:
                lines.append("## Epistemic Gaps")
                lines.extend(f"- {gap}" for gap in self.epistemic_gaps)
                lines.append("")
            return "\n".join(lines)

        if self.preconditions:
            lines.append("## Owner-Authored Preconditions")
            lines.extend(f"- {item}" for item in self.preconditions)
            lines.append("")

        lines.append("## Grounded Execution Steps")
        for idx, step in enumerate(self.steps, start=1):
            prov = (
                f"playbook:{step.source_playbook_id}; atoms:{','.join(step.backing_atom_ids)}"
                if step.source_playbook_id
                else f"atom:{','.join(step.backing_atom_ids)}"
            )
            cites = f" [{'; '.join(step.citations)}]" if step.citations else ""
            lines.append(f"{idx}. **{step.instruction}** _({prov})_{cites}")
        lines.append("")

        if self.expected_outputs:
            lines.append("## Expected Outputs & Verification")
            lines.extend(f"- {item}" for item in self.expected_outputs)
            lines.append("")

        if self.rollback_checkpoints:
            lines.append("## Failure Modes & Rollback Checkpoints")
            lines.extend(f"- {item}" for item in self.rollback_checkpoints)
            lines.append("")

        if self.preference_constraints:
            lines.append("## Owner Preference Guardrails (Non-Factual)")
            lines.extend(f"- [Preference] {pref}" for pref in self.preference_constraints)
            lines.append("")

        if self.epistemic_gaps:
            lines.append("## Epistemic Gaps & Review Notices")
            lines.extend(f"- {gap}" for gap in self.epistemic_gaps)
            lines.append("")

        return "\n".join(lines)

    def as_dict(self) -> dict[str, object]:
        return {
            "plan_ready": self.plan_ready,
            "goal": self.goal,
            "mode_used": self.mode_used,
            "summary": self.summary,
            "steps": [s.as_dict() for s in self.steps],
            "matched_playbook_ids": list(self.matched_playbook_ids),
            "preconditions": list(self.preconditions),
            "expected_outputs": list(self.expected_outputs),
            "rollback_checkpoints": list(self.rollback_checkpoints),
            "preference_constraints": list(self.preference_constraints),
            "pending_candidate_count": self.pending_candidate_count,
            "epistemic_gaps": list(self.epistemic_gaps),
            "working_context_atom_ids": list(self.working_context_atom_ids),
            "deferred_budget_atom_ids": list(self.deferred_budget_atom_ids),
            "saved_briefing_path": self.saved_briefing_path,
        }


def _goal_terms(text: str) -> set[str]:
    return {
        tok.casefold()
        for tok in TOKEN_PATTERN.findall(str(text or ""))
        if tok.casefold() not in QUERY_STOP_WORDS and len(tok) > 1
    }


def _playbook_weighted_tf(pb: PlaybookIntegrityReport) -> tuple[Counter[str], float, set[str]]:
    weighted: Counter[str] = Counter()
    for tok in TOKEN_PATTERN.findall(pb.title or ""):
        t = tok.casefold()
        if len(t) > 1 and t not in QUERY_STOP_WORDS:
            weighted[t] += 2.5
    for tok in TOKEN_PATTERN.findall(pb.trigger or ""):
        t = tok.casefold()
        if len(t) > 1 and t not in QUERY_STOP_WORDS:
            weighted[t] += 2.5
    for step_txt in pb.steps:
        for tok in TOKEN_PATTERN.findall(step_txt or ""):
            t = tok.casefold()
            if len(t) > 1 and t not in QUERY_STOP_WORDS:
                weighted[t] += 1.5
    for pre_txt in pb.preconditions:
        for tok in TOKEN_PATTERN.findall(pre_txt or ""):
            t = tok.casefold()
            if len(t) > 1 and t not in QUERY_STOP_WORDS:
                weighted[t] += 1.0
    doc_len = float(sum(weighted.values())) or 1.0
    return weighted, doc_len, set(weighted.keys())


def _playbook_matches_goal(pb: PlaybookIntegrityReport, goal: str, projects: tuple[str, ...]) -> bool:
    # Every backing atom must individually be visible in the requested scope (no union laundering).
    if projects and not playbook_visible_in_scope(pb, projects):
        return False
    q_terms = _goal_terms(goal)
    pb_corpus = f"{pb.title}\n{pb.trigger}\n" + "\n".join(pb.steps) + "\n" + "\n".join(pb.preconditions)
    pb_terms = {tok.casefold() for tok in TOKEN_PATTERN.findall(pb_corpus)}
    return bool(q_terms) and len(q_terms.intersection(pb_terms)) >= min(2, len(q_terms))


def _rank_matching_playbooks(
    candidates: list[PlaybookIntegrityReport],
    goal: str,
    projects: tuple[str, ...],
    active_context_profile: tuple[str, ...] = (),
) -> list[PlaybookIntegrityReport]:
    """Filter and rank Playbooks by field-weighted Okapi BM25 + active context profile overlap."""
    eligible = [pb for pb in candidates if _playbook_matches_goal(pb, goal, projects)]
    if len(eligible) <= 1:
        return eligible

    q_terms = _goal_terms(goal)
    profile_tags = {str(tag).strip().casefold() for tag in active_context_profile if str(tag).strip()}
    indexed: list[tuple[PlaybookIntegrityReport, Counter[str], float, set[str]]] = []
    df_counts: Counter[str] = Counter()
    for pb in eligible:
        wtf, dlen, vocab = _playbook_weighted_tf(pb)
        for t in vocab:
            df_counts[t] += 1
        indexed.append((pb, wtf, dlen, vocab))

    n_docs = len(indexed)
    avgdl = sum(item[2] for item in indexed) / max(1, n_docs)
    idf = {
        term: math.log(1.0 + (n_docs - df_counts[term] + 0.5) / (df_counts[term] + 0.5))
        for term in q_terms
    }
    k1, b = 1.5, 0.75
    scored: list[tuple[float, str, PlaybookIntegrityReport]] = []
    for pb, wtf, dlen, vocab in indexed:
        bm25 = 0.0
        norm = k1 * (1.0 - b + b * (dlen / max(1.0, avgdl)))
        for term in q_terms & vocab:
            tf_val = wtf[term]
            bm25 += idf[term] * ((tf_val * (k1 + 1.0)) / (tf_val + norm))
        pb_ctx_lower = {str(c).strip().casefold() for c in pb.contexts if str(c).strip()}
        ctx_boost = 0.15 * len(profile_tags & pb_ctx_lower)
        scored.append((bm25 + ctx_boost, pb.playbook_id, pb))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored]


def _contexts_allowed_for_host(contexts: tuple[str, ...], rule: HostSharingRule) -> bool:
    allow_all = any(str(p).strip() == "*" for p in rule.allowed_projects)
    norm_allowed = tuple(
        norm
        for p in rule.allowed_projects
        if str(p).strip() and str(p).strip() != "*"
        for norm in (normalize_project_tag(p),)
        if norm
    )
    proj_tags = [str(c).strip() for c in contexts if str(c).strip().lower().startswith("project:")]
    if not proj_tags:
        return bool(rule.allow_unscoped_atoms)
    if allow_all:
        return True
    if not norm_allowed:
        return False
    return all(bool(project_tags_match((tag,), norm_allowed)) for tag in proj_tags)


def _playbook_allowed_for_host(
    pb: PlaybookIntegrityReport,
    rule: HostSharingRule,
    *,
    atoms_dir: str | None = None,
) -> bool:
    """Require merged contexts, every backing atom's contexts, and host atom policy to allow sharing.

    Per-atom checks stop an unscoped or policy-disallowed backing atom from riding along inside a
    playbook to a host whose policy forbids it.
    """
    if not pb.backing_atom_contexts or len(pb.backing_atom_contexts) != len(pb.backing_atoms):
        return False
    if not _contexts_allowed_for_host(pb.contexts, rule):
        return False
    if not all(_contexts_allowed_for_host(tuple(ctx), rule) for ctx in pb.backing_atom_contexts):
        return False
    if atoms_dir is not None:
        records: list[dict[str, str]] = []
        for aid in pb.backing_atoms:
            apath = safe_file_in_dir(atoms_dir, aid, allowed_exts={".md"}, must_exist=True)
            if not apath or not os.path.isfile(apath) or _is_reparse_or_link(apath):
                return False
            try:
                records.append({"filename": aid, "content": Path(apath).read_text(encoding="utf-8")})
            except OSError:
                return False
        if len(filter_atoms_for_host(records, rule, mode="standard")) != len(pb.backing_atoms):
            return False
    return True


def _verified_atom_citation(atom_id: str, brain_dir: str, atoms_dir: str) -> str | None:
    atom_path = safe_file_in_dir(atoms_dir, atom_id, allowed_exts={".md"}, must_exist=True)
    if not atom_path or not os.path.isfile(atom_path) or _is_reparse_or_link(atom_path):
        return None
    if not approved_revision_is_current(atom_path, brain_dir=brain_dir, atoms_dir=atoms_dir):
        return None
    text = Path(atom_path).read_text(encoding="utf-8")
    parsed = split_frontmatter(text)
    if parsed is None:
        return None
    fm = parse_frontmatter_values(parsed[0])
    if not verify_evidence_anchor(text, fm, brain_dir):
        return None
    src = fm.get("evidence_source", "")
    start = fm.get("evidence_start", 0)
    end = fm.get("evidence_end", 0)
    return f"{atom_id} -> {src}[{start}:{end}]"


def _count_unapproved_candidates(
    goal: str,
    *,
    lexical_index_path: str,
    atoms_dir: str,
    brain_dir: str,
    projects: tuple[str, ...] = (),
) -> int:
    cand_ids = peek_lexical_candidate_atom_ids(goal, index_path=lexical_index_path, atoms_dir=atoms_dir, limit=12)
    pending = 0
    for aid in cand_ids:
        apath = safe_file_in_dir(atoms_dir, aid, allowed_exts={".md"}, must_exist=True)
        if not apath or not os.path.isfile(apath):
            continue
        if approved_revision_is_current(apath, brain_dir=brain_dir, atoms_dir=atoms_dir):
            continue
        try:
            raw = Path(apath).read_text(encoding="utf-8")
            parsed = split_frontmatter(raw)
            fm = parse_frontmatter_values(parsed[0]) if parsed else {}
            if projects:
                ctx_raw = fm.get("contexts") or []
                if isinstance(ctx_raw, str):
                    ctx_raw = parse_scalar_list(ctx_raw)
                atom_ctx = tuple(str(c).strip() for c in ctx_raw if str(c).strip())
                # Never act as a cross-project oracle: only count pending atoms in the requested scope.
                if not project_tags_match(atom_ctx, projects):
                    continue
            sup = fm.get("superseded_by") or []
            if isinstance(sup, str):
                sup = parse_scalar_list(sup)
            if sup:
                continue
            if str(fm.get("review_status", "pending")) not in EXCLUDED_RECALL_STATUSES:
                pending += 1
        except OSError:
            continue
    return pending


def _save_plan_briefing(plan: ExecutivePlanResult, *, brain_dir: str, briefings_dir: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", plan.goal.lower()).strip("-")[:40] or "plan"
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    lock_path = os.path.join(brain_dir, ".sidecar", "writer.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    os.makedirs(briefings_dir, exist_ok=True)
    if _is_reparse_or_link(briefings_dir):
        raise PermissionError("Refusing to save briefing under symlinked directory.")

    encoded = plan.render_markdown().encode("utf-8")
    with review_lock(lock_path):
        fname = f"plan-{ts}-{slug}.md"
        suffix = 2
        while True:
            bpath = safe_file_in_dir(briefings_dir, fname, allowed_exts={".md"}, must_exist=False)
            if not bpath:
                raise ValueError("Unsafe briefing filename.")
            try:
                fd = os.open(bpath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    if os.write(fd, encoded) != len(encoded):
                        raise OSError("Incomplete briefing write.")
                    os.fsync(fd)
                finally:
                    os.close(fd)
                return bpath
            except FileExistsError:
                fname = f"plan-{ts}-{slug}-{suffix}.md"
                suffix += 1


def assemble_executive_plan(
    request: ExecutivePlanRequest,
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    playbooks_dir: str = PLAYBOOKS_DIR,
    briefings_dir: str = BRIEFINGS_DIR,
    lexical_index_path: str = LEXICAL_INDEX_PATH,
    chroma_path: str = CHROMA_PATH,
    telemetry_db_path: str = TELEMETRY_DB_PATH,
    policy_path: Path | None = None,
    http_transport: httpx.BaseTransport | None = None,
    skip_dns_check_for_mock_transport: bool = False,
    save_briefing: bool = False,
) -> ExecutivePlanResult:
    """Assemble a read-only Executive Plan grounded exclusively in verified Playbooks and approved Atoms."""
    clean_goal = " ".join(str(request.goal or "").split()).strip()
    if not clean_goal:
        raise ValueError("ExecutivePlanRequest requires a non-empty goal.")
    if request.mode not in {"deterministic", "model_assisted"}:
        raise ValueError(f"Unsupported planning mode: {request.mode!r}")

    projects = (request.project_scope.strip(),) if request.project_scope and request.project_scope.strip() else ()

    # Validate host consent & network boundary upfront if model_assisted
    host_rule: HostSharingRule | None = None
    chosen_model = ""
    pinned_target = None
    if request.mode == "model_assisted":
        policies = load_host_policies(policy_path)
        if request.host_id not in policies:
            raise HostConsentError(f"Unknown AI host '{request.host_id}'.")
        host_rule = policies[request.host_id]
        if not host_rule.enabled:
            raise HostConsentError(f"Host '{request.host_id}' is disabled by default; explicit owner consent required.")
        chosen_model = validate_model_identifier(
            (request.model_override or "").strip() or host_rule.default_model,
            provider=host_rule.provider,
        )
        if not (http_transport is not None and skip_dns_check_for_mock_transport):
            pinned_target = validate_endpoint_network_boundary(host_rule)

    # 1. Retrieve verified active Playbooks matching goal & project scope
    all_active_pbs = list_playbooks(
        playbooks_dir=playbooks_dir,
        brain_dir=brain_dir,
        atoms_dir=atoms_dir,
        telemetry_db_path=telemetry_db_path,
        active_only=True,
    )
    matched_pbs = _rank_matching_playbooks(
        all_active_pbs,
        clean_goal,
        projects,
        active_context_profile=request.active_context_profile,
    )
    if host_rule is not None:
        matched_pbs = [pb for pb in matched_pbs if _playbook_allowed_for_host(pb, host_rule, atoms_dir=atoms_dir)]

    # 2. Retrieve approved Atoms with fitness & context-profile ranking + bounded working context
    recall_opts = RecallOptions(
        n_results=max(request.max_atoms, 6),
        follow_links=True,
        include_statuses={"accepted"},
        projects=projects,
        allow_cross_project=request.allow_cross_project,
        retrieval_mode=request.retrieval_mode,
        enable_fitness_boost=True,
        active_context_profile=request.active_context_profile,
        telemetry_db_path=telemetry_db_path,
    )
    try:
        direct, neighbors = search_memory(
            clean_goal,
            recall_opts,
            atoms_dir=atoms_dir,
            index_path=lexical_index_path,
            chroma_path=chroma_path,
        )
    except (MemoryRecallUnavailable, LexicalIndexUnavailable):
        has_atom_files = os.path.isdir(atoms_dir) and any(is_content_markdown(n) for n in os.listdir(atoms_dir))
        if not os.path.exists(lexical_index_path) and not has_atom_files:
            direct, neighbors = [], []
        else:
            raise
    bundle = assemble_bounded_working_context(
        list(direct) + list(neighbors),
        max_chars=request.max_context_chars,
        max_atoms=request.max_atoms,
        atoms_dir=atoms_dir,
        chroma_path=chroma_path,
    )

    factual_procedural_atoms: list[RecallResult] = []
    preference_claims: list[str] = []

    for res in bundle.included_results:
        parsed = split_frontmatter(res.content)
        if parsed is None:
            continue
        fm = parse_frontmatter_values(parsed[0])
        is_pref = (
            str(fm.get("epistemic_label", "")) == "preference"
            or bool(fm.get("epistemic_contract"))
        )
        if is_pref:
            if request.include_preferences and (host_rule is None or host_rule.allow_preference_atoms):
                claim_txt = " ".join(section_text("\n".join(parsed[1]), "Claim").split()).strip()
                if claim_txt and claim_txt not in preference_claims:
                    preference_claims.append(claim_txt)
        else:
            factual_procedural_atoms.append(res)

    if host_rule is not None:
        factual_procedural_atoms = filter_atoms_for_host(factual_procedural_atoms, host_rule, mode="standard")

    # 3. Build verified step catalog (Playbook steps -> Procedural atoms -> Semantic constraints)
    catalog_list: list[PlanStep] = []
    seen_instructions: set[str] = set()
    preconditions: list[str] = []
    expected_outputs: list[str] = []
    rollback_checkpoints: list[str] = []
    matched_pb_ids: list[str] = []

    pb_counter = 1
    for pb in matched_pbs:
        pb_cites = tuple(
            c
            for aid in pb.backing_atoms
            for c in (_verified_atom_citation(aid, brain_dir, atoms_dir),)
            if c is not None
        )
        if len(pb_cites) != len(pb.backing_atoms):
            continue
        matched_pb_ids.append(pb.playbook_id)
        for p in pb.preconditions:
            if p not in preconditions:
                preconditions.append(p)
        for o in pb.expected_outputs:
            if o not in expected_outputs:
                expected_outputs.append(o)
        for r in pb.failure_recovery:
            if r not in rollback_checkpoints:
                rollback_checkpoints.append(r)

        for step_str in pb.steps:
            norm_s = " ".join(step_str.split()).strip()
            if not norm_s or norm_s in seen_instructions:
                continue
            seen_instructions.add(norm_s)
            catalog_list.append(
                PlanStep(
                    step_id=f"PB_{pb_counter:02d}",
                    kind="playbook_step",
                    instruction=norm_s,
                    source_playbook_id=pb.playbook_id,
                    backing_atom_ids=pb.backing_atoms,
                    citations=pb_cites,
                )
            )
            pb_counter += 1

    # Order atoms: procedural first, then semantic/episodic/self
    ordered_atoms = sorted(
        factual_procedural_atoms,
        key=lambda a: (0 if a.memory_type == "procedural" else 1, -a.score, a.filename),
    )
    at_counter = 1
    for atom_res in ordered_atoms:
        cite = _verified_atom_citation(atom_res.filename, brain_dir, atoms_dir)
        if cite is None:
            continue
        parsed = split_frontmatter(atom_res.content)
        if parsed is None:
            continue
        claim_txt = " ".join(section_text("\n".join(parsed[1]), "Claim").split()).strip()
        if not claim_txt or claim_txt in seen_instructions:
            continue
        seen_instructions.add(claim_txt)
        kind = "procedural_atom" if atom_res.memory_type == "procedural" else "semantic_constraint"
        catalog_list.append(
            PlanStep(
                step_id=f"AT_{at_counter:02d}",
                kind=kind,
                instruction=claim_txt,
                source_playbook_id=None,
                backing_atom_ids=(atom_res.filename,),
                citations=(cite,),
            )
        )
        at_counter += 1

    # 4. Inspect pending review bottlenecks (count only — never expose unapproved claim text!)
    pending_count = _count_unapproved_candidates(
        clean_goal,
        lexical_index_path=lexical_index_path,
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
        projects=projects,
    )
    gaps: list[str] = []
    if pending_count > 0:
        gaps.append(
            f"{pending_count} unreviewed candidate atom(s) in 2-atoms/ match this goal; "
            "owner review and approval are required before relying on them."
        )
    if bundle.deferred_atom_ids:
        gaps.append(
            f"{len(bundle.deferred_atom_ids)} additional approved atom(s) were deferred by the working-context budget."
        )

    # Structural abstention when zero verified steps exist
    if not catalog_list:
        return ExecutivePlanResult(
            plan_ready=False,
            goal=clean_goal,
            mode_used=request.mode,
            summary=INSUFFICIENT_VERIFIED_MEMORY_FOR_PLAN,
            steps=(),
            pending_candidate_count=pending_count,
            epistemic_gaps=tuple(gaps),
            working_context_atom_ids=bundle.included_atom_ids,
            deferred_budget_atom_ids=bundle.deferred_atom_ids,
        )

    selected_steps: list[PlanStep] = list(catalog_list)
    effective_mode = "deterministic"

    # 5. Model-assisted ordering & pruning with strict out-of-model step_id whitelist excision
    if request.mode == "model_assisted" and host_rule is not None:
        catalog_by_id = {step.step_id: step for step in catalog_list}
        catalog_json = json.dumps(
            [
                {
                    "step_id": s.step_id,
                    "kind": s.kind,
                    "instruction": s.instruction,
                }
                for s in catalog_list
            ],
            ensure_ascii=False,
        )
        raw_prompt = (
            "You are a bounded planning selector. Select and order ONLY the relevant step_id values from "
            "ALLOWED_STEP_CATALOG_JSON needed to accomplish the Goal. Never invent step_ids outside the catalog. "
            "Selecting any PB_ step includes its whole owner-verified Playbook in authored order.\n\n"
            f"ALLOWED_STEP_CATALOG_JSON:\n{catalog_json}\n\n"
            f"Goal: {clean_goal}\n"
        )
        redaction, _ = _redact_outbound_with_map(
            raw_prompt,
            redact_pii=host_rule.redact_pii,
            fail_on_secret_detection=host_rule.fail_on_secret_detection,
        )
        shared_atom_ids = sorted({aid for s in catalog_list for aid in s.backing_atom_ids})
        draft_raw: str | None = None
        try:
            draft_raw = _invoke_provider_draft(
                host_rule,
                model=chosen_model,
                prompt=redaction.sanitized_text,
                http_transport=http_transport,
                pinned_target=pinned_target,
                json_schema=EXECUTIVE_PLAN_DRAFT_SCHEMA,
                schema_name="select_executive_plan_steps",
                gemini_response_schema=GEMINI_PLAN_RESPONSE_SCHEMA,
            )
        except (HostConsentError, OutboundSecretBlockedError, ValueError):
            _audit_outbound_call(
                brain_dir=brain_dir,
                rule=host_rule,
                model=chosen_model,
                atom_ids=shared_atom_ids,
                pii_redactions=redaction.pii_redaction_count,
                outcome="planner_transport_error",
            )
            raise
        except Exception as exc:
            _audit_outbound_call(
                brain_dir=brain_dir,
                rule=host_rule,
                model=chosen_model,
                atom_ids=shared_atom_ids,
                pii_redactions=redaction.pii_redaction_count,
                outcome="planner_transport_fallback",
            )
            gaps.append(
                f"Model-assisted ordering unavailable ({exc.__class__.__name__}); used deterministic verified catalog order."
            )
            selected_steps = list(catalog_list)
            effective_mode = "deterministic_fallback"

        if draft_raw is not None:
            validated_steps: list[PlanStep] = []
            seen_ids: set[str] = set()
            if isinstance(draft_raw, str) and len(draft_raw) <= 65_536:
                try:
                    parsed_draft = json.loads(draft_raw)
                    raw_ids = parsed_draft.get("selected_step_ids", []) if isinstance(parsed_draft, dict) else []
                    if isinstance(raw_ids, list):
                        for raw_id in raw_ids:
                            sid = str(raw_id).strip()
                            if sid in catalog_by_id and sid not in seen_ids:
                                seen_ids.add(sid)
                                validated_steps.append(catalog_by_id[sid])
                except json.JSONDecodeError:
                    validated_steps = []

            # Playbooks are atomic owner-authored procedures: the model may choose WHICH Playbooks
            # (and which standalone atoms) apply and their relative order, but can never drop or
            # reorder steps inside a Playbook (e.g. skipping "take a backup" before "run migration").
            if validated_steps:
                pb_steps_in_order: dict[str, list[PlanStep]] = {}
                for s in catalog_list:
                    if s.source_playbook_id:
                        pb_steps_in_order.setdefault(s.source_playbook_id, []).append(s)
                expanded: list[PlanStep] = []
                emitted_pbs: set[str] = set()
                for s in validated_steps:
                    if s.source_playbook_id:
                        if s.source_playbook_id in emitted_pbs:
                            continue
                        emitted_pbs.add(s.source_playbook_id)
                        expanded.extend(pb_steps_in_order.get(s.source_playbook_id, []))
                    else:
                        expanded.append(s)
                validated_steps = expanded

            if validated_steps:
                selected_steps = validated_steps
                effective_mode = f"model_assisted:{host_rule.host_id}"
                outcome = "grounded_plan"
            else:
                # Safe deterministic fallback if model emitted zero valid whitelist IDs
                selected_steps = list(catalog_list)
                effective_mode = "deterministic_fallback"
                outcome = "deterministic_fallback"

            _audit_outbound_call(
                brain_dir=brain_dir,
                rule=host_rule,
                model=chosen_model,
                atom_ids=shared_atom_ids,
                pii_redactions=redaction.pii_redaction_count,
                outcome=outcome,
            )

    selected_pb_ids_set = {s.source_playbook_id for s in selected_steps if s.source_playbook_id}
    selected_pbs = [pb for pb in matched_pbs if pb.playbook_id in selected_pb_ids_set and pb.playbook_id in matched_pb_ids]
    final_pb_ids: list[str] = []
    final_preconditions: list[str] = []
    final_expected_outputs: list[str] = []
    final_rollback_checkpoints: list[str] = []
    for pb in selected_pbs:
        if pb.playbook_id not in final_pb_ids:
            final_pb_ids.append(pb.playbook_id)
        for p in pb.preconditions:
            if p not in final_preconditions:
                final_preconditions.append(p)
        for o in pb.expected_outputs:
            if o not in final_expected_outputs:
                final_expected_outputs.append(o)
        for r in pb.failure_recovery:
            if r not in final_rollback_checkpoints:
                final_rollback_checkpoints.append(r)

    selected_atom_count = sum(1 for s in selected_steps if s.kind in {"procedural_atom", "semantic_constraint"})
    plan = ExecutivePlanResult(
        plan_ready=True,
        goal=clean_goal,
        mode_used=effective_mode,
        summary=(
            f"Assembled {len(selected_steps)} grounded step(s) from "
            f"{len(final_pb_ids)} verified playbook(s) and {selected_atom_count} approved atom(s)."
        ),
        steps=tuple(selected_steps),
        matched_playbook_ids=tuple(final_pb_ids),
        preconditions=tuple(final_preconditions),
        expected_outputs=tuple(final_expected_outputs),
        rollback_checkpoints=tuple(final_rollback_checkpoints),
        preference_constraints=tuple(preference_claims),
        pending_candidate_count=pending_count,
        epistemic_gaps=tuple(gaps),
        working_context_atom_ids=bundle.included_atom_ids,
        deferred_budget_atom_ids=bundle.deferred_atom_ids,
    )

    if save_briefing:
        saved_path = _save_plan_briefing(plan, brain_dir=brain_dir, briefings_dir=briefings_dir)
        plan = ExecutivePlanResult(
            **{**{k: getattr(plan, k) for k in plan.__dataclass_fields__}, "saved_briefing_path": saved_path}
        )
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assemble a grounded Second Brain Executive Plan.")
    parser.add_argument("--goal", required=True, help="Task goal to plan from verified memory.")
    parser.add_argument("--mode", choices=["deterministic", "model_assisted"], default="deterministic")
    parser.add_argument("--host", default="ollama_local", help="Host ID for model_assisted planning.")
    parser.add_argument("--model", default=None, help="Optional model override.")
    parser.add_argument("--project", default=None, help="Optional project:<id> scope filter.")
    parser.add_argument("--context", action="append", default=[], help="Active context profile tag (repeatable).")
    parser.add_argument("--include-preferences", action="store_true")
    parser.add_argument("--save-briefing", action="store_true", help="Save read-only plan Markdown to brain/briefings/.")
    parser.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    req = ExecutivePlanRequest(
        goal=args.goal,
        project_scope=args.project,
        active_context_profile=tuple(args.context),
        include_preferences=args.include_preferences,
        mode=args.mode,
        host_id=args.host,
        model_override=args.model,
    )
    res = assemble_executive_plan(req, save_briefing=args.save_briefing)
    if args.json:
        print(json.dumps(res.as_dict(), indent=2, sort_keys=True))
    else:
        print(res.render_markdown())
    return 0 if res.plan_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
