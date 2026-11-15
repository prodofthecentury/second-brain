# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""CLI and compatibility exports for the canonical atom mutator."""

from __future__ import annotations

import argparse
import json
import os
import sys

from config import ATOMS_DIR, safe_file_in_dir

try:  # Supports both `python scripts/review_atom.py` and `import scripts.review_atom`.
    from .atom_mutator import (
        ATOM_HISTORY_DIR,
        DECISION_LOG,
        TRANSITIONS,
        ReviewConflictError,
        ReviewTransitionError,
        apply_atom_edit,
        apply_atom_merge,
        append_decision_record,
        apply_atom_mutation,
        apply_review_transition,
        approved_revision_is_current,
        atomic_write_bytes,
        content_sha256,
        review_lock,
    )
except ImportError:
    from atom_mutator import (
        ATOM_HISTORY_DIR,
        DECISION_LOG,
        TRANSITIONS,
        ReviewConflictError,
        ReviewTransitionError,
        apply_atom_edit,
        apply_atom_merge,
        append_decision_record,
        apply_atom_mutation,
        apply_review_transition,
        approved_revision_is_current,
        atomic_write_bytes,
        content_sha256,
        review_lock,
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Apply owner review decisions to Second Brain atoms.")
    subparsers = parser.add_subparsers(dest="command")
    transition = subparsers.add_parser("transition", help="Approve, reject, quarantine, or demote one atom.")
    transition.add_argument("action", choices=sorted(TRANSITIONS))
    transition.add_argument("atom", help="Atom filename in brain/2-atoms.")
    transition.add_argument("--expected-sha256", required=True, help="Hash printed when the atom was inspected.")
    transition.add_argument("--actor", required=True, help="Owner identity to record in the decision log.")
    edit_p = subparsers.add_parser("edit", help="Update an atom's canonical Markdown using CAS verification.")
    edit_p.add_argument("atom", help="Atom filename in brain/2-atoms.")
    edit_p.add_argument("--expected-sha256", required=True, help="Hash printed when the atom was inspected.")
    edit_p.add_argument("--actor", required=True, help="Owner identity to record in the decision log.")
    src_grp = edit_p.add_mutually_exclusive_group(required=True)
    src_grp.add_argument("--file", help="Path to file containing updated Markdown atom content.")
    src_grp.add_argument("--stdin", action="store_true", help="Read updated Markdown atom content from stdin.")
    merge = subparsers.add_parser("merge", help="Supersede one atom with an explicitly chosen survivor.")
    merge.add_argument("survivor", help="Surviving atom filename in brain/2-atoms.")
    merge.add_argument("superseded", help="Atom filename to mark superseded.")
    merge.add_argument("--survivor-sha256", required=True, help="Exact survivor revision hash reviewed by owner.")
    merge.add_argument("--superseded-sha256", required=True, help="Exact superseded revision hash reviewed by owner.")
    merge.add_argument("--actor", required=True, help="Owner identity to record in the decision log.")
    merge.add_argument("--rationale", required=True, help="Why the two atoms should be linked and one superseded.")
    parsed = parser.parse_args(argv)
    if parsed.command is None:
        # Keep the established `review_atom.py approve <atom> ...` interface.
        parsed = transition.parse_args(argv)
        parsed.command = "transition"
    return parsed


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "merge":
            survivor_path = safe_file_in_dir(ATOMS_DIR, args.survivor, allowed_exts={".md"})
            superseded_path = safe_file_in_dir(ATOMS_DIR, args.superseded, allowed_exts={".md"})
            if not survivor_path or not superseded_path:
                print("Error: both atom filenames must identify existing canonical atoms.")
                return 1
            result = apply_atom_merge(
                survivor_path=survivor_path,
                survivor_expected_sha256=args.survivor_sha256,
                superseded_path=superseded_path,
                superseded_expected_sha256=args.superseded_sha256,
                actor=args.actor,
                rationale=args.rationale,
            )
        elif args.command == "edit":
            atom_path = safe_file_in_dir(ATOMS_DIR, args.atom, allowed_exts={".md"})
            if not atom_path:
                print("Error: atom filename is invalid or does not exist.")
                return 1
            if args.stdin:
                edited_content = sys.stdin.read()
            else:
                with open(args.file, "r", encoding="utf-8") as fh:
                    edited_content = fh.read()
            result = apply_atom_edit(
                atom_path=atom_path,
                edited_content=edited_content,
                expected_sha256=args.expected_sha256,
                actor=args.actor,
            )
        else:
            atom_path = safe_file_in_dir(ATOMS_DIR, args.atom, allowed_exts={".md"})
            if not atom_path:
                print("Error: atom filename is invalid or does not exist.")
                return 1
            result = apply_review_transition(
                atom_path=atom_path,
                action=args.action,
                expected_sha256=args.expected_sha256,
                actor=args.actor,
            )
    except (ReviewConflictError, ReviewTransitionError, OSError) as exc:
        print(f"Error: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
