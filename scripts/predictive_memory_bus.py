# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Predictive Memory Bus & L0-L3 Cache Invalidation Engine (Epic 2D.1).

Receives lightweight IDE/Desktop context signals (``active_file``, ``git_branch``,
``project_scope``, ``open_symbols``) and pre-warms an in-memory ``L0`` hot cache
(<0.5ms hits) + ``L1`` neighborhood cache.

SECURITY INVARIANT: Every cache entry is keyed strictly by the live vault
``(ledger_head_sha256, atoms_lstat_signature)`` so canonical edits, revocations,
or supersessions invalidate ``L0``/``L1`` instantaneously with zero stale leaks.
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
    raise SystemExit(_sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__"))


import argparse
from collections import OrderedDict
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any

if __name__ in ("predictive_memory_bus", "scripts.predictive_memory_bus"):
    sys.modules.setdefault("predictive_memory_bus", sys.modules[__name__])
    sys.modules.setdefault("scripts.predictive_memory_bus", sys.modules[__name__])

from scripts.atom_mutator import atomic_write_bytes  # noqa: E402
from scripts.config import (  # noqa: E402
    ATOMS_DIR,
    BRAIN_DIR,
    LEXICAL_INDEX_PATH,
    _is_reparse_or_link,
    is_content_markdown,
    resolve_module_atoms_and_brain,
    snapshot_module_defaults,
)
from scripts.recall import RecallOptions, normalize_project_tag, search_memory  # noqa: E402
from scripts.vault_ledger import ledger_path  # noqa: E402

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())

_BUS_LOCK = threading.Lock()
_MAX_L0_ENTRIES = 128
# Keyed strictly per (resolved_brain_norm, freshness_key, options_fp, query_key)
_L0_HOT_CACHE: OrderedDict[tuple[str, str, str, str], dict[str, Any]] = OrderedDict()
_L1_NEIGHBORHOOD_CACHE: OrderedDict[tuple[str, str, str, str], list[dict[str, Any]]] = OrderedDict()
_RECENT_SIGNALS: OrderedDict[str, dict[str, Any]] = OrderedDict()

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_-]{2,}")
_BUS_STOP_WORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "into", "when", "where",
        "what", "how", "why", "who", "are", "was", "were", "have", "has", "had",
        "not", "but", "can", "will", "use", "using", "our", "your", "their", "about",
        "is", "to", "of", "in", "on", "at", "by", "or", "an", "be", "as", "it",
    }
)


def _normalize_brain_key(
    *,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
) -> tuple[str, str, str]:
    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    brain_norm = os.path.normcase(os.path.normpath(os.path.abspath(resolved_brain)))
    return resolved_brain, resolved_atoms, brain_norm


def _tokenize_query(text: str) -> set[str]:
    return {
        tok.lower()
        for tok in _TOKEN_RE.findall(text or "")
        if len(tok) >= 2 and tok.lower() not in _BUS_STOP_WORDS
    }


def make_options_fingerprint(
    *,
    n_results: int = 5,
    follow_links: bool = True,
    retrieval_mode: str = "auto",
    include_expired: bool = False,
    as_of: str | None = None,
    memory_types: tuple[str, ...] = (),
    contexts: tuple[str, ...] = (),
    active_context_profile: tuple[str, ...] = (),
    allow_cross_project: bool = False,
    enable_fitness_boost: bool = False,
    enable_reranker: bool = True,
    enable_gravity_boost: bool = False,
    enable_spreading_activation: bool = False,
) -> str:
    payload = {
        "n_results": int(n_results),
        "follow_links": bool(follow_links),
        "retrieval_mode": str(retrieval_mode or "auto").strip().lower(),
        "include_expired": bool(include_expired),
        "as_of": str(as_of or "").strip(),
        "memory_types": sorted(str(m).strip().lower() for m in (memory_types or ()) if str(m).strip()),
        "contexts": sorted(str(c).strip().lower() for c in (contexts or ()) if str(c).strip()),
        "active_context_profile": sorted(str(c).strip().lower() for c in (active_context_profile or ()) if str(c).strip()),
        "allow_cross_project": bool(allow_cross_project),
        "enable_fitness_boost": bool(enable_fitness_boost),
        "enable_reranker": bool(enable_reranker),
        "enable_gravity_boost": bool(enable_gravity_boost),
        "enable_spreading_activation": bool(enable_spreading_activation),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def compute_vault_freshness_key(
    *,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
) -> str:
    """Compute a fast cryptographic freshness signature from ledger tail + atoms lstat + calendar date."""
    resolved_brain, resolved_atoms, _ = _normalize_brain_key(
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    h = hashlib.sha256()
    bpath = Path(resolved_brain)
    apath = Path(resolved_atoms)

    # 0. Calendar date so midnight temporal expiry invalidates cached hits automatically
    h.update(f"today:{datetime.date.today().isoformat()}\n".encode("utf-8"))

    # 1. Ledger head hash via O(1) tail-seek of last 4 KB (never reads entire ledger into RAM)
    lp = ledger_path(resolved_brain)
    head_hash = "genesis"
    if lp.is_file() and not _is_reparse_or_link(str(lp)):
        try:
            with open(lp, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                file_size = fh.tell()
                if file_size > 0:
                    read_size = min(file_size, 4096)
                    fh.seek(file_size - read_size)
                    tail_bytes = fh.read(read_size)
                    lines = [ln for ln in tail_bytes.decode("utf-8", errors="replace").splitlines() if ln.strip()]
                    if lines:
                        last_obj = json.loads(lines[-1])
                        head_hash = str(last_obj.get("entry_sha256") or "genesis")
        except Exception:
            head_hash = "unreadable"
    h.update(f"ledger:{head_hash}\n".encode("utf-8"))

    # 2. Sub-millisecond lstat sweep over 2-atoms/*.md
    if apath.is_dir() and not _is_reparse_or_link(str(apath)):
        for entry in sorted(os.scandir(apath), key=lambda e: e.name):
            if not is_content_markdown(entry.name):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
                h.update(f"{entry.name}:{st.st_mtime_ns}:{st.st_size}\n".encode("utf-8"))
            except OSError:
                h.update(f"{entry.name}:err\n".encode("utf-8"))

    # 3. Also factor in decision-record.jsonl mtime/size so instantaneous CAS transitions bust cache
    dec_p = bpath / "0-raw" / "archive" / "decision-record.jsonl"
    if os.path.lexists(str(dec_p)):
        if _is_reparse_or_link(str(dec_p)):
            h.update(b"decisions:symlink\n")
        else:
            try:
                dst = os.lstat(str(dec_p))
                h.update(f"decisions:{dst.st_mtime_ns}:{dst.st_size}\n".encode("utf-8"))
            except OSError:
                h.update(b"decisions:err\n")
    else:
        h.update(b"decisions:missing\n")

    return h.hexdigest()[:32]


def invalidate_bus_cache() -> None:
    """Explicitly clear L0/L1 predictive memory caches."""
    with _BUS_LOCK:
        _L0_HOT_CACHE.clear()
        _L1_NEIGHBORHOOD_CACHE.clear()


def _bus_signal_log_path(brain_dir: str | Path | None = None) -> Path:
    resolved_brain, _ = resolve_module_atoms_and_brain(globals(), brain_dir=brain_dir)
    return Path(resolved_brain) / ".sidecar" / "predictive_bus_state.json"


def _normalize_scope(project_scope: str | None) -> str:
    cleaned = str(project_scope or "").strip()
    if not cleaned:
        return ""
    return normalize_project_tag(cleaned)


def _normalize_query_key(text: str, project_scope: str | None) -> str:
    tokens = sorted({t.lower() for t in (text or "").split() if len(t.strip()) >= 2})
    raw = f"{_normalize_scope(project_scope)}|{' '.join(tokens[:12])}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _serialize_recall_item(rec: Any) -> dict[str, Any]:
    return {
        "filename": str(getattr(rec, "filename", "")),
        "content": str(getattr(rec, "content", "")),
        "source": str(getattr(rec, "source", "")),
        "score": float(getattr(rec, "score", 0.0) or 0.0),
        "similarity": float(getattr(rec, "similarity", 0.0) or 0.0),
        "review_status": str(getattr(rec, "review_status", "accepted") or "accepted"),
        "memory_type": str(getattr(rec, "memory_type", "semantic") or "semantic"),
        "confidence": float(getattr(rec, "confidence", 0.5) if getattr(rec, "confidence", None) is not None else 0.5),
        "importance": float(getattr(rec, "importance", 0.5) if getattr(rec, "importance", None) is not None else 0.5),
        "contexts": list(getattr(rec, "contexts", ()) or ()),
        "match_reasons": list(getattr(rec, "match_reasons", ()) or ()),
        "valid_from": str(getattr(rec, "valid_from", "") or ""),
        "valid_until": str(getattr(rec, "valid_until", "") or ""),
        "temporal_status": str(getattr(rec, "temporal_status", "active") or "active"),
    }


def record_context_signal(
    *,
    active_file: str = "",
    git_branch: str = "",
    project_scope: str | None = None,
    open_symbols: list[str] | tuple[str, ...] = (),
    cursor_topic: str = "",
    prewarm: bool = True,
    n_results: int = 5,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
    lexical_index_path: str | Path | None = None,
) -> dict[str, Any]:
    """Ingest a UI/IDE context signal and pre-warm L0 + L1 memory caches cleanly."""
    resolved_brain, resolved_atoms, _ = _normalize_brain_key(
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    lex_path = (
        str(lexical_index_path)
        if lexical_index_path is not None
        else str(Path(resolved_brain) / ".sidecar" / "lexical.sqlite3")
    )
    freshness = compute_vault_freshness_key(brain_dir=resolved_brain, atoms_dir=resolved_atoms)
    clean_file = str(active_file or "").strip()[:260]
    clean_branch = str(git_branch or "").strip()[:120]
    clean_proj = str(project_scope or "").strip() or None
    symbols = [str(s).strip()[:80] for s in (open_symbols or []) if str(s).strip()][:12]
    clean_topic = str(cursor_topic or "").strip()[:260]

    signal = {
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "active_file": clean_file,
        "git_branch": clean_branch,
        "project_scope": clean_proj,
        "open_symbols": symbols,
        "cursor_topic": clean_topic,
        "freshness_key": freshness,
    }

    # Build a synthetic prediction query from signal fields
    file_stem = Path(clean_file).stem.replace("_", " ").replace("-", " ") if clean_file else ""
    branch_words = clean_branch.replace("/", " ").replace("-", " ").replace("_", " ")
    pred_query = " ".join(part for part in (clean_topic, file_stem, branch_words, " ".join(symbols)) if part).strip()

    prewarmed_count = 0
    neighbor_count = 0
    if prewarm and pred_query:
        projects = (clean_proj,) if clean_proj else ()
        direct, neighbors = search_memory(
            pred_query,
            RecallOptions(
                n_results=max(1, min(10, int(n_results))),
                follow_links=True,
                include_statuses={"accepted"},
                projects=projects,
                retrieval_mode="lexical",
                use_l0_bus=False,
            ),
            atoms_dir=resolved_atoms,
            index_path=lex_path,
            chroma_path=str(Path(resolved_brain) / ".chroma"),
        )
        d_ser = [_serialize_recall_item(r) for r in direct]
        n_ser = [_serialize_recall_item(r) for r in neighbors]
        prewarmed_count = len(d_ser)
        neighbor_count = len(n_ser)
        store_l0_entry(
            query=pred_query,
            project_scope=clean_proj,
            direct_items=d_ser,
            neighbor_items=n_ser,
            freshness_key=freshness,
            options_fp="prewarm",
            brain_dir=resolved_brain,
            atoms_dir=resolved_atoms,
            entry_kind="prewarm",
        )

    with _BUS_LOCK:
        sig_key = f"{clean_proj or '*'}|{clean_file}"
        _RECENT_SIGNALS[sig_key] = signal
        _RECENT_SIGNALS.move_to_end(sig_key)
        while len(_RECENT_SIGNALS) > 32:
            _RECENT_SIGNALS.popitem(last=False)

    # Persist lightweight summary snapshot to disposable sidecar
    state_p = _bus_signal_log_path(resolved_brain)
    try:
        if not _is_reparse_or_link(str(state_p.parent)) and not (os.path.lexists(str(state_p)) and _is_reparse_or_link(str(state_p))):
            state_p.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(str(state_p), (json.dumps(signal, indent=2) + "\n").encode("utf-8"))
    except OSError:
        pass

    return {
        "status": "warmed" if prewarm and pred_query else "recorded",
        "signal": signal,
        "predicted_query": pred_query,
        "prewarmed_direct_count": prewarmed_count,
        "prewarmed_neighbor_count": neighbor_count,
        "freshness_key": freshness,
    }


def store_l0_entry(
    *,
    query: str,
    project_scope: str | None,
    direct_items: list[dict[str, Any]],
    neighbor_items: list[dict[str, Any]],
    freshness_key: str,
    options_fp: str = "default",
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
    entry_kind: str = "exact",
    cache_key_override: tuple[str, str, str, str] | None = None,
) -> tuple[str, str, str, str]:
    _, _, brain_norm = _normalize_brain_key(brain_dir=brain_dir, atoms_dir=atoms_dir)
    proj_norm = _normalize_scope(project_scope)
    eff_fp = str(options_fp or "default")
    qhash = _normalize_query_key(query, proj_norm)
    qkey: tuple[str, str, str, str] = (
        cache_key_override
        if isinstance(cache_key_override, tuple) and len(cache_key_override) == 4
        else (brain_norm, freshness_key, eff_fp, qhash)
    )
    q_tokens = sorted(_tokenize_query(query))
    with _BUS_LOCK:
        # Evict stale freshness keys ONLY for the same vault (never wipe another vault's entries)
        stale_keys = [k for k in _L0_HOT_CACHE if k[0] == brain_norm and k[1] != freshness_key]
        for sk in stale_keys:
            _L0_HOT_CACHE.pop(sk, None)
            _L1_NEIGHBORHOOD_CACHE.pop(sk, None)
        _L0_HOT_CACHE[qkey] = {
            "brain_norm": brain_norm,
            "freshness_key": freshness_key,
            "options_fp": eff_fp,
            "entry_kind": str(entry_kind or "exact"),
            "query": query,
            "query_tokens": q_tokens,
            "project_scope": project_scope,
            "project_scope_norm": proj_norm,
            "direct": list(direct_items),
            "cached_at": time.monotonic(),
        }
        _L0_HOT_CACHE.move_to_end(qkey)
        _L1_NEIGHBORHOOD_CACHE[qkey] = list(neighbor_items)
        _L1_NEIGHBORHOOD_CACHE.move_to_end(qkey)
        while len(_L0_HOT_CACHE) > _MAX_L0_ENTRIES:
            old_k, _ = _L0_HOT_CACHE.popitem(last=False)
            _L1_NEIGHBORHOOD_CACHE.pop(old_k, None)
    return qkey


def lookup_l0_hot(
    query: str,
    *,
    project_scope: str | None = None,
    options_fp: str = "default",
    allow_prewarm_match: bool = True,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """Check L0 RAM hot cache after verifying current vault freshness key (<0.5ms).

    Returns ``None`` immediately with zero disk I/O when the L0 cache is empty.
    """
    t0 = time.perf_counter()
    with _BUS_LOCK:
        if not _L0_HOT_CACHE:
            return None

    resolved_brain, resolved_atoms, brain_norm = _normalize_brain_key(
        brain_dir=brain_dir,
        atoms_dir=atoms_dir,
    )
    with _BUS_LOCK:
        if not any(k[0] == brain_norm for k in _L0_HOT_CACHE):
            return {
                "hit": False,
                "tier": "miss",
                "freshness_key": "",
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 4),
                "direct": [],
                "neighbors": [],
            }

    freshness = compute_vault_freshness_key(brain_dir=resolved_brain, atoms_dir=resolved_atoms)
    proj_norm = _normalize_scope(project_scope)
    eff_fp = str(options_fp or "default")
    qhash = _normalize_query_key(query, proj_norm)
    exact_key = (brain_norm, freshness, eff_fp, qhash)

    with _BUS_LOCK:
        matched_key: tuple[str, str, str, str] | None = None
        match_kind = "exact"
        entry = _L0_HOT_CACHE.get(exact_key)
        if entry is not None:
            matched_key = exact_key
        elif eff_fp == "default":
            prewarm_exact_key = (brain_norm, freshness, "prewarm", qhash)
            entry = _L0_HOT_CACHE.get(prewarm_exact_key)
            if entry is not None:
                matched_key = prewarm_exact_key
                match_kind = "prewarm_exact"

        if entry is None and allow_prewarm_match:
            q_toks = _tokenize_query(query)
            if len(q_toks) >= 2:
                best_overlap = 1
                best_key: tuple[str, str, str, str] | None = None
                for k, cand in _L0_HOT_CACHE.items():
                    if k[0] != brain_norm or k[1] != freshness:
                        continue
                    if cand.get("entry_kind") != "prewarm":
                        continue
                    if cand.get("project_scope_norm") != proj_norm:
                        continue
                    if not cand.get("direct"):
                        continue
                    cand_toks = set(cand.get("query_tokens") or ())
                    overlap = len(q_toks & cand_toks)
                    if overlap >= 2 and overlap > best_overlap:
                        best_overlap = overlap
                        best_key = k
                if best_key is not None:
                    matched_key = best_key
                    entry = _L0_HOT_CACHE[best_key]
                    match_kind = "prewarm_overlap"

        if entry is None or matched_key is None:
            return {
                "hit": False,
                "tier": "miss",
                "freshness_key": freshness,
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 4),
                "direct": [],
                "neighbors": [],
            }
        _L0_HOT_CACHE.move_to_end(matched_key)
        neighbors = list(_L1_NEIGHBORHOOD_CACHE.get(matched_key, []))
        return {
            "hit": True,
            "tier": "L0",
            "match_kind": match_kind,
            "freshness_key": freshness,
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 4),
            "direct": list(entry["direct"]),
            "neighbors": neighbors,
        }


def bus_status(*, brain_dir: str | Path | None = None, atoms_dir: str | Path | None = None) -> dict[str, Any]:
    resolved_brain, resolved_atoms, brain_norm = _normalize_brain_key(
        brain_dir=brain_dir,
        atoms_dir=atoms_dir,
    )
    freshness = compute_vault_freshness_key(brain_dir=resolved_brain, atoms_dir=resolved_atoms)
    with _BUS_LOCK:
        valid_l0 = sum(1 for k in _L0_HOT_CACHE if k[0] == brain_norm and k[1] == freshness)
        vault_l0 = sum(1 for k in _L0_HOT_CACHE if k[0] == brain_norm)
        signals = list(_RECENT_SIGNALS.values())[-5:]
    return {
        "freshness_key": freshness,
        "l0_valid_entries": valid_l0,
        "l0_vault_entries": vault_l0,
        "l0_total_entries": len(_L0_HOT_CACHE),
        "recent_signals": signals,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Second Brain Predictive Memory Bus (L0-L3)")
    sub = parser.add_subparsers(dest="cmd")

    p_st = sub.add_parser("status", help="Inspect Predictive Memory Bus status")
    p_st.add_argument("--json", action="store_true")

    p_sig = sub.add_parser("signal", help="Record an IDE/Desktop context signal and pre-warm L0")
    p_sig.add_argument("--active-file", default="")
    p_sig.add_argument("--git-branch", default="")
    p_sig.add_argument("--project", default=None)
    p_sig.add_argument("--topic", default="")
    p_sig.add_argument("--symbol", action="append", default=[])
    p_sig.add_argument("--no-prewarm", action="store_true")
    p_sig.add_argument("--json", action="store_true")

    p_lk = sub.add_parser("lookup", help="Look up a query in L0 hot cache")
    p_lk.add_argument("query")
    p_lk.add_argument("--project", default=None)
    p_lk.add_argument("--json", action="store_true")

    p_inv = sub.add_parser("invalidate", help="Invalidate in-memory L0/L1 caches")
    p_inv.add_argument("--json", action="store_true")

    raw_args = list(sys.argv[1:] if argv is None else argv)
    if not raw_args or raw_args[0].startswith("-"):
        raw_args = ["status", *raw_args]
    args = parser.parse_args(raw_args)

    if args.cmd == "status":
        res = bus_status()
        print(json.dumps(res, indent=2) if args.json else f"freshness={res['freshness_key']} l0_valid={res['l0_valid_entries']}")
        return 0
    if args.cmd == "signal":
        res = record_context_signal(
            active_file=args.active_file,
            git_branch=args.git_branch,
            project_scope=args.project,
            open_symbols=args.symbol,
            cursor_topic=args.topic,
            prewarm=not args.no_prewarm,
        )
        print(json.dumps(res, indent=2) if args.json else f"status={res['status']} prewarmed={res['prewarmed_direct_count']}")
        return 0
    if args.cmd == "lookup":
        res = lookup_l0_hot(args.query, project_scope=args.project) or {"hit": False, "tier": "miss", "direct": [], "neighbors": []}
        print(json.dumps(res, indent=2) if args.json else f"hit={res['hit']} tier={res['tier']}")
        return 0
    if args.cmd == "invalidate":
        invalidate_bus_cache()
        out = {"status": "invalidated"}
        print(json.dumps(out, indent=2) if args.json else "invalidated")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
