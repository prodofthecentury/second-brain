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

from config import ATOMS_DIR, LEXICAL_INDEX_PATH
from lexical_store import rebuild_lexical_index
from review_atom import review_lock


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--atoms-dir", default=ATOMS_DIR, help="Canonical Markdown atom directory.")
    parser.add_argument("--index-path", default=LEXICAL_INDEX_PATH, help="Derived SQLite FTS5 sidecar path.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        lock_path = os.path.join(os.path.dirname(os.path.abspath(args.index_path)), "writer.lock")
        with review_lock(lock_path):
            stats = rebuild_lexical_index(args.atoms_dir, args.index_path)
    except Exception as exc:
        print(f"Lexical index rebuild failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
