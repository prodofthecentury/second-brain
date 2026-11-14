# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""Corpus-scale benchmark for the model-free lexical engine.

Builds an isolated synthetic vault (never touches the live vault), then measures:
  * full atomic FTS5 rebuild
  * index freshness check (cold = first scan hashes every file; warm = stat cache)
  * single-note incremental upsert (the editor "save" path)
  * lexical query latency P50/P95
  * paginated browse latency

Run:
    uv run --locked python scripts/scale_benchmark.py --notes 5000
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import random
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from index_manifest import invalidate_atom_revision_cache  # noqa: E402
from lexical_store import (  # noqa: E402
    lexical_index_is_current,
    list_indexed_atoms_fast,
    rebuild_lexical_index,
    search_lexical_index,
    upsert_lexical_atoms,
)

VOCAB = (
    "logging observability tracing resilience failure retry backoff circuit breaker cache "
    "database index query latency throughput queue worker scheduler deployment rollback "
    "migration schema contract api client server token auth session encryption backup "
    "restore snapshot replica shard partition consensus leader election heartbeat timeout "
    "battery sodium lithium cathode anode electrolyte voltage capacity cycle thermal "
    "kitchen recipe garden compost soil seed harvest travel itinerary budget invoice"
).split()

# Inline f-string templates for the synthetic atoms.
ATOM_TEMPLATE = """---
id: {stem}
type: atom
memory_type: semantic
review_status: pending
claim_state: candidate
citation_integrity: missing
contexts:
  - project:bench_{project}
---

## Claim
{claim}

## Evidence
{evidence}

## Why It Matters
Synthetic benchmark note {index}.
"""


def _sentence(rng: random.Random, words: int) -> str:
    return " ".join(rng.choice(VOCAB) for _ in range(words)).capitalize() + "."


def build_corpus(atoms_dir: Path, notes: int, seed: int) -> None:
    rng = random.Random(seed)
    atoms_dir.mkdir(parents=True, exist_ok=True)
    for index in range(notes):
        stem = f"note-{index:06d}"
        (atoms_dir / f"{stem}.md").write_text(
            ATOM_TEMPLATE.format(
                stem=stem,
                project=index % 25,
                claim=_sentence(rng, 14),
                evidence=" ".join(_sentence(rng, 18) for _ in range(4)),
                index=index,
            ),
            encoding="utf-8",
        )


def _pct(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


def run(notes: int, queries: int, seed: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="sb-scale-") as tmp:
        root = Path(tmp)
        atoms_dir = root / "2-atoms"
        index_path = str(root / ".sidecar" / "lexical.sqlite3")

        t0 = time.perf_counter()
        build_corpus(atoms_dir, notes, seed)
        corpus_s = time.perf_counter() - t0

        invalidate_atom_revision_cache()
        t0 = time.perf_counter()
        rebuild_lexical_index(str(atoms_dir), index_path)
        rebuild_s = time.perf_counter() - t0

        invalidate_atom_revision_cache()
        t0 = time.perf_counter()
        cold_ok = lexical_index_is_current(str(atoms_dir), index_path)
        fresh_cold_ms = (time.perf_counter() - t0) * 1000

        warm_samples = []
        for _ in range(10):
            t0 = time.perf_counter()
            warm_ok = lexical_index_is_current(str(atoms_dir), index_path)
            warm_samples.append((time.perf_counter() - t0) * 1000)

        # Editor save path: modify one note then incrementally upsert it.
        rng = random.Random(seed + 1)
        save_samples = []
        for i in range(10):
            target = atoms_dir / f"note-{rng.randrange(notes):06d}.md"
            text = target.read_text(encoding="utf-8")
            target.write_text(text + f"\nEdit {i} quasar-{i}.\n", encoding="utf-8")
            t0 = time.perf_counter()
            upsert_lexical_atoms(str(atoms_dir), index_path, [target.name])
            save_samples.append((time.perf_counter() - t0) * 1000)
        still_current = lexical_index_is_current(str(atoms_dir), index_path)
        edit_found = bool(search_lexical_index("quasar-9", index_path, n_results=5))

        query_samples = []
        qrng = random.Random(seed + 2)
        for _ in range(queries):
            q = " ".join(qrng.choice(VOCAB) for _ in range(3))
            t0 = time.perf_counter()
            search_lexical_index(q, index_path, n_results=10, atoms_dir=str(atoms_dir))
            query_samples.append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        page, total = list_indexed_atoms_fast(index_path, limit=200, offset=1000 if notes > 1200 else 0)
        browse_ms = (time.perf_counter() - t0) * 1000

    return {
        "notes": notes,
        "corpus_generation_s": round(corpus_s, 2),
        "full_rebuild_s": round(rebuild_s, 2),
        "freshness_check_cold_ms": round(fresh_cold_ms, 1),
        "freshness_check_warm_p50_ms": round(statistics.median(warm_samples), 1),
        "single_note_save_p50_ms": round(statistics.median(save_samples), 1),
        "single_note_save_p95_ms": round(_pct(save_samples, 95), 1),
        "query_end_to_end_p50_ms": round(statistics.median(query_samples), 1),
        "query_end_to_end_p95_ms": round(_pct(query_samples, 95), 1),
        "browse_page_200_ms": round(browse_ms, 1),
        "browse_total": total,
        "browse_page_len": len(page),
        "index_current_after_rebuild": cold_ok and warm_ok,
        "index_current_after_incremental_saves": still_current,
        "incremental_edit_searchable": edit_found,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notes", type=int, default=5000)
    parser.add_argument("--queries", type=int, default=50)
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--json-output")
    args = parser.parse_args(argv)
    result = run(args.notes, args.queries, args.seed)
    print(json.dumps(result, indent=2))
    if args.json_output:
        Path(args.json_output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_output).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    ok = (
        result["index_current_after_rebuild"]
        and result["index_current_after_incremental_saves"]
        and result["incremental_edit_searchable"]
        and result["browse_total"] == args.notes
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
