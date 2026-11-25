# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Split a user question into a bounded list of lexical searches.

The first item is always the original query. Extra items are clauses long enough
to search on their own. There is no model call and no recursive split.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lexical_store import FTS_STOP_WORDS, tokenize

_SPLIT_RE = re.compile(r"\s+(?:and|vs|versus)\s+|;\s*", re.IGNORECASE)
_MAX_EXTRA = 3


def _content_terms(text: str) -> list[str]:
    return [tok for tok in tokenize(text) if tok not in FTS_STOP_WORDS and len(tok) > 1]


def plan_subqueries(query: str) -> tuple[str, ...]:
    """Return the original query, then up to three clauses worth searching.

    A clause with fewer than two non-stopword terms is omitted. When no clause
    qualifies, the result is the original query alone. Length is at most 4.
    """
    original = " ".join(str(query or "").split())
    if not original:
        return ("",)
    pieces = [part.strip() for part in _SPLIT_RE.split(original) if part.strip()]
    if len(pieces) <= 1:
        return (original,)
    kept = [part for part in pieces if len(_content_terms(part)) >= 2]
    if not kept:
        return (original,)
    return (original, *kept[:_MAX_EXTRA])
