# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Hippocampal Nightly Episodic Replay & Counterfactual Engine (Epic 2D.6).

Replays ``brain/4-experiences/*.md`` during Night Shift, performs counterfactual
matching against active Playbooks and approved Atoms ("What verified
Playbook/Atom would have prevented this failure or reduced steps?"), writes a
summary briefing into ``brain/briefings/YYYY-MM-DD-nightly-replay.md``, and
stages sanitized procedural/principle proposals into quarantined ``brain/0-raw/inbox/``.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ in ("episodic_replay", "scripts.episodic_replay"):
    sys.modules.setdefault("episodic_replay", sys.modules[__name__])
    sys.modules.setdefault("scripts.episodic_replay", sys.modules[__name__])

from atom_mutator import atomic_write_bytes  # noqa: E402
from config import (  # noqa: E402
    ATOMS_DIR,
    BRAIN_DIR,
    _is_reparse_or_link,
    is_content_markdown,
    resolve_module_atoms_and_brain,
    snapshot_module_defaults,
)
from memory_format import split_frontmatter  # noqa: E402
from memory_review import parse_atom_record_text, parse_frontmatter_values, parse_scalar_list, section_text  # noqa: E402
from review_atom import approved_revision_is_current  # noqa: E402

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())
_WORD_RE = re.compile(r"[a-zA-Z0-9_-]{3,}")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STOP_WORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "into", "when",
        "where", "what", "should", "could", "would", "have", "been", "true", "false",
    }
)


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _WORD_RE.findall(text or "") if t.lower() not in _STOP_WORDS}


def run_nightly_replay(
    *,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
    date_str: str | None = None,
    stage_inbox_proposals: bool = True,
) -> dict[str, Any]:
    """Replay 4-experiences/*.md and produce counterfactual findings & inbox drafts."""
    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    bpath = Path(resolved_brain)
    apath = Path(resolved_atoms)
    exp_dir = bpath / "4-experiences"
    brief_dir = bpath / "briefings"
    raw_dir = bpath / "0-raw"
    inbox_dir = raw_dir / "inbox"
    if date_str is not None:
        run_date = str(date_str).strip()
        if not _ISO_DATE_RE.fullmatch(run_date):
            raise ValueError(f"Invalid date_str {date_str!r}; expected strict YYYY-MM-DD.")
        try:
            datetime.date.fromisoformat(run_date)
        except ValueError as exc:
            raise ValueError(f"Invalid calendar date_str {date_str!r}.") from exc
    else:
        run_date = datetime.date.today().isoformat()

    # Gather approved atoms & active playbooks for counterfactual retrieval
    approved_atoms: list[dict[str, Any]] = []
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
            if rec.get("review_status") == "accepted" and not rec.get("superseded_by") and approved_revision_is_current(
                str(p), brain_dir=resolved_brain, atoms_dir=resolved_atoms
            ):
                claim = str(rec.get("claim") or "").strip()
                approved_atoms.append(
                    {
                        "filename": entry.name,
                        "claim": claim,
                        "tokens": _tokens(f"{entry.name} {claim}"),
                    }
                )

    active_pbs: list[dict[str, Any]] = []
    try:
        from skill_playbooks import list_playbooks

        for pb in list_playbooks(brain_dir=resolved_brain, atoms_dir=resolved_atoms, active_only=True):
            active_pbs.append(
                {
                    "filename": pb.filename,
                    "title": pb.title,
                    "tokens": _tokens(f"{pb.filename} {pb.title} {' '.join(pb.steps or ())}"),
                }
            )
    except Exception:
        pass

    experiences_scanned = 0
    failures: list[dict[str, Any]] = []
    lesson_clusters: dict[str, int] = {}

    if exp_dir.is_dir() and not _is_reparse_or_link(str(exp_dir)):
        for entry in sorted(os.scandir(exp_dir), key=lambda e: e.name):
            if not is_content_markdown(entry.name):
                continue
            ep = exp_dir / entry.name
            if not ep.is_file() or _is_reparse_or_link(str(ep)):
                continue
            experiences_scanned += 1
            text = ep.read_text(encoding="utf-8", errors="replace")
            parsed = split_frontmatter(text)
            fm_vals = parse_frontmatter_values(parsed[0]) if parsed else {}
            body = "\n".join(parsed[1]) if parsed else text
            goal = str(fm_vals.get("goal") or entry.name[:-3]).strip()
            success_raw = str(fm_vals.get("success") or "").strip().lower()
            is_failure = success_raw in {"false", "0", "no"}
            lesson_text = section_text(body, "Lessons") or section_text(body, "Reflection") or section_text(body, "Result Summary")
            ctxs: list[str] = []
            for ctx_key in ("context", "contexts"):
                ctx_raw = fm_vals.get(ctx_key)
                if isinstance(ctx_raw, str):
                    parsed_ctx = parse_scalar_list(ctx_raw)
                elif isinstance(ctx_raw, (list, tuple)):
                    parsed_ctx = [str(x).strip() for x in ctx_raw if str(x).strip()]
                else:
                    parsed_ctx = []
                for c_val in parsed_ctx:
                    if c_val and c_val not in ctxs:
                        ctxs.append(c_val)
            for c in ctxs:
                if is_failure:
                    lesson_clusters[str(c)] = lesson_clusters.get(str(c), 0) + 1

            if is_failure:
                ep_tokens = _tokens(f"{goal} {lesson_text} {body[:500]}")
                matched_pb = sorted(
                    ((len(ep_tokens & pb["tokens"]), pb) for pb in active_pbs),
                    key=lambda pair: (-pair[0], pair[1]["filename"]),
                )
                matched_atoms = sorted(
                    ((len(ep_tokens & at["tokens"]), at) for at in approved_atoms),
                    key=lambda pair: (-pair[0], pair[1]["filename"]),
                )
                counterfactual_pb = matched_pb[0][1]["filename"] if matched_pb and matched_pb[0][0] > 0 else None
                counterfactual_atoms = [pair[1]["filename"] for pair in matched_atoms[:3] if pair[0] > 0]
                failures.append(
                    {
                        "experience": entry.name,
                        "goal": goal,
                        "lesson": (lesson_text or goal).strip()[:300],
                        "counterfactual_playbook": counterfactual_pb,
                        "counterfactual_atoms": counterfactual_atoms,
                        "recommendation": (
                            f"Apply playbook {counterfactual_pb} before retrying."
                            if counterfactual_pb
                            else (
                                f"Ground next attempt with {', '.join(counterfactual_atoms)}."
                                if counterfactual_atoms
                                else "Promote preventive checklist from this failure into 2-atoms/ via Owner CAS."
                            )
                        ),
                    }
                )

    if os.path.lexists(str(brief_dir)) and _is_reparse_or_link(str(brief_dir)):
        raise PermissionError(f"Refusing symlinked briefings directory: {brief_dir}")
    brief_dir.mkdir(parents=True, exist_ok=True)
    briefing_path = brief_dir / f"{run_date}-nightly-replay.md"
    if os.path.lexists(str(briefing_path)) and _is_reparse_or_link(str(briefing_path)):
        raise PermissionError(f"Refusing symlinked briefing file: {briefing_path}")
    lines = [
        f"# Nightly Hippocampal Episodic Replay ({run_date})",
        "",
        f"- **Experiences Replayed:** {experiences_scanned}",
        f"- **Failures / Counterfactuals Analyzed:** {len(failures)}",
        "",
        "## Counterfactual Analysis",
    ]
    if failures:
        for f in failures:
            lines.append(f"### `{f['experience']}` — {f['goal']}")
            lines.append(f"- **Lesson:** {f['lesson']}")
            lines.append(f"- **Counterfactual Guidance:** {f['recommendation']}")
            if f["counterfactual_atoms"]:
                lines.append(f"- **Related Approved Atoms:** {', '.join(f['counterfactual_atoms'])}")
            lines.append("")
    else:
        lines.append("No unresolved failure episodes required counterfactual remediation.")
        lines.append("")

    atomic_write_bytes(str(briefing_path), ("\n".join(lines).rstrip() + "\n").encode("utf-8"))

    staged_inbox_files: list[str] = []
    if stage_inbox_proposals and failures:
        if os.path.lexists(str(raw_dir)) and _is_reparse_or_link(str(raw_dir)):
            raise PermissionError(f"Refusing symlinked raw directory: {raw_dir}")
        if os.path.lexists(str(inbox_dir)) and _is_reparse_or_link(str(inbox_dir)):
            raise PermissionError(f"Refusing symlinked inbox directory: {inbox_dir}")
        inbox_dir.mkdir(parents=True, exist_ok=True)
        proposal_path = inbox_dir / f"{run_date}-replay-counterfactuals.md"
        if os.path.lexists(str(proposal_path)) and _is_reparse_or_link(str(proposal_path)):
            raise PermissionError(f"Refusing symlinked inbox proposal file: {proposal_path}")
        p_lines = [
            "<!-- second_brain_origin: cognitive_experience -->",
            f"# Counterfactual Remediation Proposals ({run_date})",
            "",
            "Untrusted automated Nightly Replay candidates staged in quarantine for owner review:",
            "",
        ]
        for f in failures[:8]:
            p_lines.append(f"- **{f['goal']}** (`{f['experience']}`): {f['recommendation']} — Lesson: {f['lesson']}")
        atomic_write_bytes(str(proposal_path), ("\n".join(p_lines).rstrip() + "\n").encode("utf-8"))
        staged_inbox_files.append(str(proposal_path))

    return {
        "status": "completed",
        "date": run_date,
        "experiences_scanned": experiences_scanned,
        "failures_analyzed": len(failures),
        "failure_clusters": lesson_clusters,
        "counterfactuals": failures,
        "briefing_path": str(briefing_path),
        "staged_inbox_files": staged_inbox_files,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Second Brain Hippocampal Nightly Episodic Replay")
    parser.add_argument("--date", default=None)
    parser.add_argument("--no-inbox", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    out = run_nightly_replay(date_str=args.date, stage_inbox_proposals=not args.no_inbox)
    print(json.dumps(out, indent=2) if args.json else f"replayed={out['experiences_scanned']} failures={out['failures_analyzed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
