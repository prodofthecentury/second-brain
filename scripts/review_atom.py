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
        append_decision_record,
        apply_atom_mutation,
        apply_review_transition,
        approved_revision_is_current,
        atomic_write_bytes,
        content_sha256,
        review_lock,
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Apply an owner review transition to a Second Brain atom.")
    parser.add_argument("action", choices=sorted(TRANSITIONS))
    parser.add_argument("atom", help="Atom filename in brain/2-atoms.")
    parser.add_argument("--expected-sha256", required=True, help="Hash printed when the atom was inspected.")
    parser.add_argument("--actor", required=True, help="Owner identity to record in the decision log.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    atom_path = safe_file_in_dir(ATOMS_DIR, args.atom, allowed_exts={".md"})
    if not atom_path:
        print("Error: atom filename is invalid or does not exist.")
        return 1
    try:
        result = apply_review_transition(
            atom_path=atom_path,
            action=args.action,
            expected_sha256=args.expected_sha256,
            actor=args.actor,
        )
    except (ReviewConflictError, ReviewTransitionError) as exc:
        print(f"Error: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
