# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Audit atom source anchors and optionally demote stale approvals."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import (
    ReviewConflictError,
    ReviewTransitionError,
    apply_review_transition,
    approved_revision_is_current,
    review_lock,
)
from config import (
    ATOMS_DIR,
    BRAIN_DIR,
    _is_reparse_or_link,
    is_content_markdown,
    resolve_module_vault_path,
    snapshot_module_defaults,
)
from evidence import ANCHOR_FIELDS, EvidenceValidationError, find_evidence_anchor
from memory_format import split_frontmatter
from memory_review import parse_frontmatter_values

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())


def _source_snapshot(brain_root: Path, anchor: dict[str, Any]) -> tuple[str, str | None, str | None]:
    source = anchor.get("evidence_source")
    if not isinstance(source, str) or not source.strip():
        return "missing_path", None, "No evidence_source anchor is stored."
    relative = Path(source.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        return "unsafe_path", None, "Stored evidence path is absolute or traverses parent directories."
    try:
        candidate = brain_root / relative
        if _is_reparse_or_link(str(candidate)):
            return "unsafe_path", None, "Stored evidence source is a symlink or reparse point."
        path = candidate.resolve(strict=True)
        allowed_roots = [(brain_root / "0-raw").resolve(), (brain_root / "sources").resolve()]
        if not path.is_file() or _is_reparse_or_link(str(path)) or not any(path.is_relative_to(root) for root in allowed_roots):
            return "outside_allowed_sources", None, "Stored evidence source is outside durable source directories."
        return "present", hashlib.sha256(path.read_bytes()).hexdigest(), None
    except OSError as exc:
        return "missing_or_unreadable", None, str(exc)


def audit_sources(
    *,
    atoms_dir: str | Path | None = None,
    brain_dir: str | Path | None = None,
    decision_log_path: str | Path | None = None,
    demote_stale: bool = False,
    actor: str | None = None,
) -> list[dict[str, Any]]:
    """Report source/approval drift; only demote on explicit opt-in.

    Demotion uses the canonical compare-and-swap mutator and appends its normal
    owner decision record. The audit never edits evidence or silently repairs
    an anchor; the owner must inspect the current source before re-approval.
    """
    explicit_atoms = str(atoms_dir) if atoms_dir is not None else None
    explicit_brain = str(brain_dir) if brain_dir is not None else None
    if explicit_atoms is not None and explicit_brain is None:
        atoms_root = Path(explicit_atoms).resolve()
        brain_root = atoms_root.parent
    else:
        brain_str = resolve_module_vault_path(globals(), "BRAIN_DIR", explicit_brain)
        brain_root = Path(brain_str).resolve()
        atoms_str = resolve_module_vault_path(
            globals(),
            "ATOMS_DIR",
            explicit_atoms,
            brain_dir=explicit_brain,
        )
        atoms_root = Path(atoms_str).resolve()
    decision_path = (
        Path(decision_log_path).resolve()
        if decision_log_path is not None
        else (brain_root / "0-raw" / "archive" / "decision-record.jsonl").resolve()
    )
    if demote_stale and (not isinstance(actor, str) or not actor.strip()):
        raise ValueError("An explicit actor is required with --demote-stale.")

    reports: list[dict[str, Any]] = []
    lock_path = brain_root / ".sidecar" / "writer.lock"
    if _is_reparse_or_link(str(atoms_root)):
        raise ValueError("Refusing to audit a symlinked atoms directory.")
    for atom_path in sorted(atoms_root.iterdir()) if atoms_root.is_dir() else []:
        if _is_reparse_or_link(str(atom_path)) or not atom_path.is_file() or not is_content_markdown(atom_path.name):
            continue
        entry: dict[str, Any] = {
            "atom_id": atom_path.name,
            "anchor_status": "unreadable",
            "anchor_error": None,
            "review_status": "unknown",
            "claim_state": "unknown",
            "atom_sha256": None,
            "stored_anchor": None,
            "current_anchor": None,
            "source_status": "unknown",
            "current_source_sha256": None,
            "approval_current": False,
            "action": "none",
            "mutation_error": None,
        }
        atom_hash = None
        should_demote = False
        try:
            with review_lock(str(lock_path)):
                raw = atom_path.read_bytes()
                content = raw.decode("utf-8")
                atom_hash = hashlib.sha256(raw).hexdigest()
                parsed = split_frontmatter(content)
                if parsed is None:
                    raise ValueError("Atom has invalid or missing frontmatter.")
                values = parse_frontmatter_values(parsed[0])
                entry["review_status"] = str(values.get("review_status", "pending"))
                entry["claim_state"] = str(values.get("claim_state", "candidate"))
                entry["atom_sha256"] = atom_hash

                anchors = {key: values.get(key) for key in ANCHOR_FIELDS}
                if not all(value is not None and str(value).strip() for value in anchors.values()):
                    entry["anchor_status"] = "missing"
                else:
                    entry["stored_anchor"] = anchors
                    entry["source_status"], entry["current_source_sha256"], source_error = _source_snapshot(
                        brain_root, anchors,
                    )
                    if source_error:
                        entry["anchor_error"] = source_error
                    try:
                        current_anchor = find_evidence_anchor(content, str(brain_root))
                        entry["current_anchor"] = current_anchor
                        entry["anchor_status"] = (
                            "current"
                            if all(str(current_anchor.get(key)) == str(anchors.get(key)) for key in ANCHOR_FIELDS)
                            else "stale_or_unverifiable"
                        )
                    except (EvidenceValidationError, OSError, ValueError) as exc:
                        entry["anchor_status"] = "stale_or_unverifiable"
                        entry["anchor_error"] = str(exc)

                entry["approval_current"] = approved_revision_is_current(
                    str(atom_path),
                    decision_log_path=str(decision_path),
                    brain_dir=str(brain_root),
                    atoms_dir=str(atoms_root),
                )
                approval_metadata_present = (
                    entry["review_status"] == "accepted"
                    or entry["claim_state"] == "human_reviewed"
                )
                should_demote = approval_metadata_present and not entry["approval_current"]
                if should_demote:
                    entry["action"] = "would_demote" if demote_stale else "review_required"
                elif entry["approval_current"]:
                    entry["action"] = "none"
                else:
                    entry["action"] = "not_approved"
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            entry["anchor_error"] = str(exc)
            if entry["review_status"] == "accepted" or entry["claim_state"] == "human_reviewed":
                should_demote = True
                entry["action"] = "would_demote" if demote_stale else "review_required"

        if demote_stale and should_demote and atom_hash:
            try:
                apply_review_transition(
                    action="demote",
                    atom_path=str(atom_path),
                    expected_sha256=atom_hash,
                    actor=str(actor).strip(),
                    decision_log_path=str(decision_path),
                    brain_dir=str(brain_root),
                    atoms_dir=str(atoms_root),
                )
                entry["action"] = "demoted"
            except (ReviewConflictError, ReviewTransitionError, OSError, ValueError) as exc:
                entry["action"] = "conflict_or_failure"
                entry["mutation_error"] = str(exc)
        entry["stale_approval"] = bool(
            should_demote or entry["action"] in {"review_required", "demoted", "would_demote"}
        )
        entry["demoted"] = bool(entry["action"] == "demoted")
        entry["ok"] = bool(
            entry["action"] not in {"review_required", "would_demote", "conflict_or_failure"}
            and entry["anchor_status"] != "unreadable"
        )
        reports.append(entry)
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--atoms-dir", default=None)
    parser.add_argument("--brain-dir", default=None)
    parser.add_argument("--decision-log", default=None)
    parser.add_argument(
        "--check", action="store_true",
        help="Exit 1 when any atom requires review, failed mutation, or has an unreadable anchor.",
    )
    parser.add_argument(
        "--demote-stale", action="store_true",
        help="Demote accepted atoms whose approval no longer matches current atom/source bytes.",
    )
    parser.add_argument("--actor", help="Required audit actor for --demote-stale.")
    args = parser.parse_args(argv)
    try:
        report = audit_sources(
            atoms_dir=args.atoms_dir,
            brain_dir=args.brain_dir,
            decision_log_path=args.decision_log,
            demote_stale=args.demote_stale,
            actor=args.actor,
        )
    except (OSError, ValueError) as exc:
        print(f"Source audit failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.check:
        has_issues = any(
            row["action"] in {"conflict_or_failure", "review_required"}
            or row["anchor_status"] == "unreadable"
            for row in report
        )
        return 1 if has_issues else 0
    return 1 if any(row["action"] == "conflict_or_failure" for row in report) else 0


if __name__ == "__main__":
    raise SystemExit(main())
