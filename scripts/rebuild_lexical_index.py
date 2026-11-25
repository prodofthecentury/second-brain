# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Explicitly rebuild the derived SQLite FTS5 sidecar from canonical atoms."""

from __future__ import annotations

import argparse
import json
import os
import sys

from config import ATOMS_DIR, LEXICAL_INDEX_PATH, resolve_module_vault_path, snapshot_module_defaults

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())
from lexical_store import rebuild_lexical_index
from review_atom import review_lock


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--atoms-dir", default=None, help="Canonical Markdown atom directory.")
    parser.add_argument("--index-path", default=None, help="Derived SQLite FTS5 sidecar path.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        eff_atoms = resolve_module_vault_path(globals(), "ATOMS_DIR", args.atoms_dir)
        derived_brain = os.path.dirname(os.path.abspath(eff_atoms)) if args.atoms_dir else None
        eff_index = resolve_module_vault_path(globals(), "LEXICAL_INDEX_PATH", args.index_path, brain_dir=derived_brain)
        lock_path = os.path.join(os.path.dirname(os.path.abspath(eff_index)), "writer.lock")
        with review_lock(lock_path):
            stats = rebuild_lexical_index(eff_atoms, eff_index)
    except Exception as exc:
        print(f"Lexical index rebuild failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
