# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Small local benchmarks for the embedding cache and the job queue.

These numbers are measured on this machine. They do not call Ollama.
The uncached stand-in hashes the text every time. The cached path stores
that vector and reads it back.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:
    import runpy as _sb_runpy
    import sys as _sb_sys
    from pathlib import Path as _sb_Path

    _sb_repo = _sb_Path(__file__).resolve().parents[1]
    _sb_here = _sb_Path(__file__).resolve().parent
    if _sb_Path(_sb_sys.path[0]).resolve() == _sb_here:
        _sb_sys.path[0] = str(_sb_repo)
    elif str(_sb_repo) not in _sb_sys.path:
        _sb_sys.path.insert(0, str(_sb_repo))
    _sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__")
    raise SystemExit(0)

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path


def _stand_in_embed(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [byte / 255.0 for byte in digest]


def run_benchmarks() -> dict[str, float | int]:
    from scripts.job_queue import claim_next, enqueue
    from scripts.ollama_utils import _embed_cache_get, _embed_cache_put

    texts = [f"capture {index} " + ("token " * 40) for index in range(200)]
    started = time.perf_counter()
    uncached = [_stand_in_embed(text) for text in texts for _repeat in range(2)]
    uncached_ms = (time.perf_counter() - started) * 1000

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BRAIN_EMBED_CACHE_PATH"] = str(Path(tmp) / "cache.sqlite3")
        os.environ["BRAIN_JOB_QUEUE"] = str(Path(tmp) / "jobs.sqlite3")
        try:
            started = time.perf_counter()
            for text in texts:
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                cached = _embed_cache_get("stand-in", digest)
                if cached is None:
                    _embed_cache_put("stand-in", digest, _stand_in_embed(text))
                _embed_cache_get("stand-in", digest)
            cold_ms = (time.perf_counter() - started) * 1000

            started = time.perf_counter()
            for text in texts:
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                cached = _embed_cache_get("stand-in", digest)
                if cached is None:
                    raise RuntimeError("embedding cache miss on the second pass")
            hit_ms = (time.perf_counter() - started) * 1000

            started = time.perf_counter()
            for index in range(50):
                enqueue("capture", {"title": "bench", "body": f"note {index}", "brain_dir": tmp})
            queued_ms = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            claimed = 0
            while claim_next() is not None:
                claimed += 1
            claim_ms = (time.perf_counter() - started) * 1000
        finally:
            os.environ.pop("BRAIN_EMBED_CACHE_PATH", None)
            os.environ.pop("BRAIN_JOB_QUEUE", None)

    return {
        "texts": len(texts),
        "uncached_double_embed_ms": round(uncached_ms, 3),
        "uncached_vectors": len(uncached),
        "cache_fill_and_read_ms": round(cold_ms, 3),
        "cache_hit_ms": round(hit_ms, 3),
        "jobs_enqueued": 50,
        "enqueue_ms": round(queued_ms, 3),
        "jobs_claimed": claimed,
        "claim_ms": round(claim_ms, 3),
    }


def main() -> int:
    print(json.dumps(run_benchmarks(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
