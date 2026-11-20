# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#     "ollama",
#     "chromadb",
# ]
# ///
"""
Shared recall contract for Second Brain.

The vector store finds candidate memories. This module applies the durable
Markdown memory metadata before callers inject those memories into chat.
"""

from __future__ import annotations

from contextlib import closing
import contextvars
from dataclasses import dataclass, field, replace
import datetime
import hashlib
import math
import os
import re
import sqlite3
import stat
import sys
import threading

if __name__ in ("recall", "scripts.recall"):
    sys.modules.setdefault("recall", sys.modules[__name__])
    sys.modules.setdefault("scripts.recall", sys.modules[__name__])

from atom_mutator import latest_decisions_by_atom
from config import (
    ATOMS_DIR,
    BRAIN_DIR,
    CHROMA_PATH,
    EMBED_MODEL,
    LEXICAL_INDEX_PATH,
    RAW_DIR,
    TELEMETRY_DB_PATH,
    _WINDOWS_RESERVED_STEMS,
    _is_reparse_or_link,
    is_content_markdown,
    normalize_markdown_filename,
    safe_file_in_dir,
)
from index_manifest import cached_atom_hash_for_stat, index_manifest_is_current, load_index_manifest
from lexical_store import (
    FTS_STOP_WORDS,
    TOKEN_PATTERN,
    _search_lexical_index_verified,
    porter_stem,
    search_lexical_index,
)
from memory_format import split_frontmatter
from memory_review import REVIEW_STATUSES, parse_atom_record, parse_atom_record_text, parse_scalar_list, section_text
from review_atom import approved_revision_is_current
from vector_store import close_vector_collection, get_vector_collection

_ORIG_APPROVED_REVISION_IS_CURRENT = approved_revision_is_current
_ORIG_SEARCH_LEXICAL_INDEX = search_lexical_index


class MemoryRecallUnavailable(RuntimeError):
    """Raised when semantic recall cannot run without compromising retrieval integrity."""


def validated_index_generation(
    manifest: dict | None,
    *,
    atoms_dir: str,
    collection_name: str,
    embedding_model: str,
    embedding_dimensions: int,
) -> str:
    """Return the active generation only if it matches the live atoms and embedder contract."""
    if not isinstance(manifest, dict):
        raise MemoryRecallUnavailable("Semantic recall index has no verified generation manifest; rebuild it first.")
    if manifest.get("collection_name") != collection_name:
        raise MemoryRecallUnavailable("Semantic recall index collection does not match the configured collection.")
    if manifest.get("embedding_model") != embedding_model:
        raise MemoryRecallUnavailable("Semantic recall index was built with a different embedding model; rebuild it first.")
    if manifest.get("embedding_dimensions") != embedding_dimensions:
        raise MemoryRecallUnavailable("Semantic recall index dimensions do not match the current embedding model.")
    generation_id = manifest.get("generation_id")
    if not isinstance(generation_id, str) or not generation_id:
        raise MemoryRecallUnavailable("Semantic recall index generation identifier is missing.")
    if manifest.get("distance_metric") != "cosine":
        raise MemoryRecallUnavailable("Semantic recall index uses an unsupported distance metric; rebuild it with cosine distance.")
    if not index_manifest_is_current(manifest, atoms_dir):
        raise MemoryRecallUnavailable("Semantic recall index is stale because canonical atoms changed; rebuild it first.")
    return generation_id


NORMAL_RECALL_STATUSES = {"accepted"}
EXCLUDED_RECALL_STATUSES = {"rejected", "merged"}
REVIEW_STATUS_WEIGHTS = {
    "accepted": 0.12,
    "pending": -0.02,
    "needs_edit": -0.06,
    "rejected": -1.00,
    "merged": -1.00,
}
QUERY_STOP_WORDS = set(FTS_STOP_WORDS) | {
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "but", "by", "can", "could",
    "did", "do", "does", "for", "from", "had", "has", "have", "having", "how", "i", "in", "into",
    "is", "it", "its", "me", "my", "of", "on", "or", "our", "owner", "owners", "owner's",
    "rather", "should", "so", "some", "such", "than", "that", "the", "their", "them", "then",
    "there", "these", "they", "this", "those", "through", "to", "too", "under", "very", "was",
    "we", "were", "what", "when", "where", "which", "while", "who", "why", "will", "with", "would",
    "you", "your", "please", "output", "according", "approved", "factual", "claim", "claims",
    "confirms", "confirm", "purely",
}

_RECALL_PARSE_CACHE_LOCK = threading.Lock()
# Maps (filename, content_sha256) -> dict of immutable parsed atom metadata
_ATOM_RECORD_BY_SHA: dict[tuple[str, str], dict] = {}
# Maps content_sha256 -> (corroborating_terms_set, corroborating_stems_set)
_CORROBORATING_TERMS_BY_SHA: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
# Maps abs_telemetry_db -> ((db_mtime_ns, db_size, wal_mtime_ns, wal_size), {atom_id: stats_dict})
_TELEMETRY_MAP_CACHE: dict[str, tuple[tuple[int, int, int, int], dict[str, dict[str, int]]]] = {}


@dataclass(frozen=True)
class RecallOptions:
    n_results: int = 5
    follow_links: bool = True
    include_statuses: set[str] | None = None
    exclude_statuses: set[str] = field(default_factory=lambda: set(EXCLUDED_RECALL_STATUSES))
    contexts: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    allow_cross_project: bool = False
    allow_unreviewed_exploratory: bool = False
    memory_types: tuple[str, ...] = ()
    candidate_multiplier: int = 4
    retrieval_mode: str = "auto"  # 'auto', 'rrf', 'lexical', 'dense'
    enable_fitness_boost: bool = False
    active_context_profile: tuple[str, ...] = ()
    telemetry_db_path: str | None = None


@dataclass(frozen=True)
class WorkingContextBundle:
    """Bounded, provenance-tracked working context assembled without truncating atom claims or evidence."""

    included_results: tuple[RecallResult, ...]
    included_atom_ids: tuple[str, ...]
    deferred_atom_ids: tuple[str, ...]
    used_chars: int
    budget_chars: int
    max_atoms: int
    index_generation: str | None
    source_sha256_map: dict[str, str]
    formatted_context: str


_LAST_DEGRADATION_WARNING: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "last_degradation_warning", default=None
)


def emit_degradation_warning(reason: str) -> str:
    """Emit an explicit warning banner when dense retrieval degrades to FTS5 lexical search."""
    banner = (
        f"[WARNING] Dense embedding service unavailable ({reason}); "
        "dynamically degraded to FTS5 lexical retrieval."
    )
    _LAST_DEGRADATION_WARNING.set(banner)
    print(banner, file=sys.stderr)
    return banner


def consume_last_degradation_warning() -> str | None:
    """Return and clear the most recent degradation warning banner for the current execution context."""
    warning = _LAST_DEGRADATION_WARNING.get()
    _LAST_DEGRADATION_WARNING.set(None)
    return warning


def normalize_project_tag(tag: str) -> str:
    cleaned = str(tag or "").strip().lower().replace("-", "_")
    if not cleaned:
        return ""
    return cleaned if cleaned.startswith("project:") else f"project:{cleaned}"


def project_tags_match(record_contexts: tuple[str, ...], requested_projects: tuple[str, ...]) -> tuple[str, ...]:
    """Match normalized project:<id> tags, supporting hierarchical sub-scopes (project:parent/child)."""
    req_tags = [normalize_project_tag(p) for p in requested_projects if str(p or "").strip()]
    if not req_tags:
        return ()
    matched = []
    for raw_ctx in record_contexts:
        if not raw_ctx.lower().startswith("project:"):
            continue
        norm_ctx = normalize_project_tag(raw_ctx)
        for req in req_tags:
            if norm_ctx == req or norm_ctx.startswith(req + "/"):
                matched.append(raw_ctx)
                break
    return tuple(matched)


@dataclass(frozen=True)
class RecallResult:
    filename: str
    content: str
    source: str
    score: float
    similarity: float
    review_status: str
    memory_type: str
    confidence: float
    importance: float
    contexts: tuple[str, ...] = ()
    match_reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "content": self.content,
            "source": self.source,
            "score": self.score,
            "similarity": self.similarity,
            "review_status": self.review_status,
            "memory_type": self.memory_type,
            "confidence": self.confidence,
            "importance": self.importance,
            "contexts": list(self.contexts),
            "match_reasons": list(self.match_reasons),
        }


def clamp_number(value: str | float | int | None, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def cosine_distance_to_similarity(distance: float | int | str | None) -> float:
    """Map Chroma cosine distance [0, 2] to similarity [1, 0] without saturation at 1."""
    try:
        value = float(distance)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return 1.0 - max(0.0, min(2.0, value)) / 2.0


def normalize_statuses(statuses: set[str] | None) -> set[str] | None:
    if statuses is None:
        return None
    normalized = {status for status in statuses if status in REVIEW_STATUSES}
    return normalized or None


def metadata_contexts(record: dict) -> tuple[str, ...]:
    contexts = record.get("contexts", [])
    if isinstance(contexts, str):
        contexts = parse_scalar_list(contexts)
    return tuple(str(item).strip() for item in contexts if str(item).strip())


def _resolve_canonical_atoms_root(atoms_dir: str) -> str | None:
    if not atoms_dir or not os.path.isdir(atoms_dir) or _is_reparse_or_link(atoms_dir):
        return None
    try:
        real_root = os.path.realpath(atoms_dir)
    except OSError:
        return None
    if not os.path.isdir(real_root) or _is_reparse_or_link(real_root):
        return None
    return real_root


def _read_verified_atom_bytes_fast(
    filename: str,
    expected_sha256: str,
    *,
    canonical_atoms_root: str | None,
    atoms_dir: str,
    candidate_content: str | None = None,
) -> tuple[str, bytes, str] | None:
    """Verify atom filename, symlink safety, and exact SHA-256 without redundant disk opens."""
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        return None
    st: os.stat_result | None = None
    if canonical_atoms_root is not None:
        name = str(filename or "").strip().replace("\\", "/")
        if (
            not name
            or name in {".", ".."}
            or "/" in name
            or ":" in name
            or "\x00" in name
            or os.path.isabs(name)
            or not name.lower().endswith(".md")
        ):
            return None
        stem = os.path.splitext(name)[0].rstrip(" .").lower()
        first_stem = name.split(".", 1)[0].rstrip(" .").lower()
        if stem in _WINDOWS_RESERVED_STEMS or first_stem in _WINDOWS_RESERVED_STEMS:
            return None
        path = os.path.join(canonical_atoms_root, name)
        try:
            st = os.lstat(path)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
            return None
        if os.name == "nt" and (getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            return None
    else:
        resolved = safe_file_in_dir(atoms_dir, filename, allowed_exts={".md"})
        if not resolved:
            return None
        path = resolved

    if st is not None and candidate_content is not None:
        cached_sha = cached_atom_hash_for_stat(path, st)
        if cached_sha == expected_sha256:
            try:
                cand_bytes = candidate_content.encode("utf-8")
            except UnicodeEncodeError:
                cand_bytes = b""
            if cand_bytes and hashlib.sha256(cand_bytes).hexdigest() == expected_sha256:
                return path, cand_bytes, candidate_content

    try:
        with open(path, "rb") as fh:
            raw_bytes = fh.read()
        content = raw_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if hashlib.sha256(raw_bytes).hexdigest() != expected_sha256:
        return None
    return path, raw_bytes, content


def load_memory_metadata(
    filename: str,
    atoms_dir: str = ATOMS_DIR,
    *,
    _preverified: tuple[str, bytes, str] | None = None,
    _known_sha256: str | None = None,
    _canonical_atoms_root: str | None = None,
    _decision_map: dict[str, dict] | None = None,
    _source_sha_memo: dict[str, str | None] | None = None,
) -> dict:
    pre_content: str | None = None
    if _preverified is not None:
        path, raw_bytes, pre_content = _preverified
    else:
        path = safe_file_in_dir(atoms_dir, filename, allowed_exts={".md"})
        if not path:
            return {
                "filename": filename,
                "memory_type": "semantic",
                "review_status": "pending",
                "claim_state": "candidate",
                "confidence": "0.50",
                "importance": "0.50",
                "contexts": [],
            }
        raw_bytes = b""
    try:
        if not raw_bytes:
            with open(path, "rb") as fh:
                raw_bytes = fh.read()
        sha = _known_sha256 or hashlib.sha256(raw_bytes).hexdigest()
        cache_key = (filename, sha)
        with _RECALL_PARSE_CACHE_LOCK:
            cached_rec = _ATOM_RECORD_BY_SHA.get(cache_key)
        if cached_rec is not None:
            record = dict(cached_rec)
        else:
            if pre_content is not None:
                record = parse_atom_record_text(filename, pre_content)
            else:
                record = parse_atom_record(path)
            with _RECALL_PARSE_CACHE_LOCK:
                if len(_ATOM_RECORD_BY_SHA) > 8192:
                    _ATOM_RECORD_BY_SHA.clear()
                _ATOM_RECORD_BY_SHA[cache_key] = dict(record)
    except Exception:
        sha = ""
        record = {
            "filename": filename,
            "memory_type": "semantic",
            "review_status": "needs_edit",
            "claim_state": "candidate",
            "confidence": "0.20",
            "importance": "0.20",
            "contexts": [],
        }
    effective_brain_dir = os.path.abspath(os.path.join(atoms_dir, os.pardir))
    decision_log = os.path.join(effective_brain_dir, "0-raw", "archive", "decision-record.jsonl")
    if os.path.abspath(atoms_dir) == os.path.abspath(ATOMS_DIR):
        effective_brain_dir = BRAIN_DIR
        decision_log = os.path.join(RAW_DIR, "archive", "decision-record.jsonl")
    if (
        approved_revision_is_current is _ORIG_APPROVED_REVISION_IS_CURRENT
        and raw_bytes
        and sha
        and _canonical_atoms_root is not None
    ):
        record["approved_revision_current"] = approved_revision_is_current(
            path,
            decision_log_path=decision_log,
            brain_dir=effective_brain_dir,
            atoms_dir=atoms_dir,
            known_atom_bytes=raw_bytes,
            known_atom_sha256=sha,
            known_decision_map=_decision_map,
            source_sha_memo=_source_sha_memo,
            canonical_atoms_root=_canonical_atoms_root,
        )
    else:
        record["approved_revision_current"] = approved_revision_is_current(
            path,
            decision_log_path=decision_log,
            brain_dir=effective_brain_dir,
            atoms_dir=atoms_dir,
        )
    return record


def should_include_record(record: dict, options: RecallOptions) -> bool:
    status = str(record.get("review_status", "pending"))
    claim_state = str(record.get("claim_state", "candidate"))
    include_statuses = normalize_statuses(options.include_statuses)
    exclude_statuses = normalize_statuses(options.exclude_statuses) or set()

    superseded_by = record.get("superseded_by", [])
    if isinstance(superseded_by, str):
        superseded_by = parse_scalar_list(superseded_by)
    if superseded_by:
        return False

    if status in exclude_statuses:
        return False
    if include_statuses is not None and status not in include_statuses:
        return False
    if include_statuses is None and status not in NORMAL_RECALL_STATUSES:
        return False

    # Unconditional trust invariant: standard recall and ANY 'accepted' atom must
    # always verify human_reviewed claim_state and current approved revision.
    if not options.allow_unreviewed_exploratory or status == "accepted":
        if claim_state != "human_reviewed":
            return False
        if record.get("approved_revision_current") is not True:
            return False

    if options.memory_types:
        memory_type = str(record.get("memory_type", "semantic"))
        if memory_type not in set(options.memory_types):
            return False

    if options.projects:
        record_contexts = metadata_contexts(record)
        if not project_tags_match(record_contexts, options.projects):
            return False

    return True


def context_match_score(record_contexts: tuple[str, ...], requested_contexts: tuple[str, ...]) -> tuple[float, tuple[str, ...]]:
    if not requested_contexts:
        return 0.0, ()

    record_lookup = {context.lower(): context for context in record_contexts}
    matches = []
    for context in requested_contexts:
        if context.lower() in record_lookup:
            matches.append(record_lookup[context.lower()])

    if not matches:
        return 0.0, ()
    return len(matches) / len(requested_contexts), tuple(matches)


def _load_single_atom_telemetry(filename: str, telemetry_db_path: str | None = None) -> dict[str, int]:
    """Read-only lookup of disposable sidecar telemetry; stat-cached in memory so recall avoids N SQLite connects."""
    db_path = telemetry_db_path or TELEMETRY_DB_PATH
    if not db_path:
        return {}
    abs_db = os.path.abspath(db_path)
    try:
        st_db = os.stat(abs_db)
    except OSError:
        return {}
    wal_mtime = 0
    wal_size = 0
    try:
        st_wal = os.stat(abs_db + "-wal")
        wal_mtime = st_wal.st_mtime_ns
        wal_size = st_wal.st_size
    except OSError:
        pass
    sig = (st_db.st_mtime_ns, st_db.st_size, wal_mtime, wal_size)
    with _RECALL_PARSE_CACHE_LOCK:
        cached = _TELEMETRY_MAP_CACHE.get(abs_db)
    if cached is not None and cached[0] == sig:
        hit = cached[1].get(filename)
        return dict(hit) if hit else {}

    try:
        table_map: dict[str, dict[str, int]] = {}
        with closing(sqlite3.connect(abs_db, timeout=2.0)) as conn:
            rows = conn.execute(
                """
                SELECT atom_id, retrieval_hits, grounded_citations, positive_feedback, corrections
                FROM atom_telemetry
                """
            ).fetchall()
            for atom_id, ret_h, cit_h, pos_h, corr_h in rows:
                if atom_id:
                    table_map[str(atom_id)] = {
                        "retrieval_hits": int(ret_h or 0),
                        "grounded_citations": int(cit_h or 0),
                        "positive_feedback": int(pos_h or 0),
                        "corrections": int(corr_h or 0),
                    }
        with _RECALL_PARSE_CACHE_LOCK:
            if len(_TELEMETRY_MAP_CACHE) > 64:
                _TELEMETRY_MAP_CACHE.clear()
            _TELEMETRY_MAP_CACHE[abs_db] = (sig, table_map)
        hit = table_map.get(filename)
        return dict(hit) if hit else {}
    except sqlite3.Error:
        return {}


def _load_fitness_signals(
    filename: str,
    telemetry_db_path: str | None = None,
    brain_dir: str | None = None,
) -> dict[str, int]:
    """Merge disposable SQLite telemetry with durable 0-raw/archive/atom-corrections.jsonl."""
    sidecar = _load_single_atom_telemetry(filename, telemetry_db_path)
    resolved_brain = brain_dir
    if resolved_brain is None:
        db_candidate = telemetry_db_path or TELEMETRY_DB_PATH
        if db_candidate:
            abs_db = os.path.abspath(db_candidate)
            parent = os.path.dirname(abs_db)
            if os.path.basename(parent) == ".sidecar":
                resolved_brain = os.path.dirname(parent)
    durable_pos = 0
    durable_corr = 0
    if resolved_brain:
        try:
            from cognitive_core import load_durable_feedback_counts

            dmap = load_durable_feedback_counts(resolved_brain)
            dentry = dmap.get(filename) or {}
            durable_pos = int(dentry.get("positive_feedback", 0) or 0)
            durable_corr = int(dentry.get("corrections", 0) or 0)
        except Exception:
            pass
    if not sidecar and durable_pos == 0 and durable_corr == 0:
        return {}
    return {
        "retrieval_hits": int(sidecar.get("retrieval_hits", 0) or 0),
        "grounded_citations": int(sidecar.get("grounded_citations", 0) or 0),
        "positive_feedback": max(int(sidecar.get("positive_feedback", 0) or 0), durable_pos),
        "corrections": max(int(sidecar.get("corrections", 0) or 0), durable_corr),
    }


def _compute_freshness_bonus(record: dict) -> float:
    """Gentle 180-day half-life recency bonus in [0.0, 0.03] from last-reviewed or created ISO date."""
    raw_date = str(record.get("last-reviewed") or record.get("created") or "").strip()
    if not raw_date:
        return 0.0
    try:
        parsed_day = datetime.date.fromisoformat(raw_date[:10])
    except ValueError:
        return 0.0
    today = datetime.datetime.now(datetime.timezone.utc).date()
    age_days = max(0.0, float((today - parsed_day).days))
    return round(0.03 * (2.0 ** (-age_days / 180.0)), 6)


def result_from_candidate(
    content: str,
    filename: str,
    distance: float | None,
    options: RecallOptions,
    source: str,
    atoms_dir: str = ATOMS_DIR,
    similarity_override: float | None = None,
    *,
    _preloaded_record: dict | None = None,
) -> RecallResult | None:
    record = _preloaded_record if _preloaded_record is not None else load_memory_metadata(filename, atoms_dir=atoms_dir)
    if not should_include_record(record, options):
        return None

    similarity = (
        clamp_number(similarity_override)
        if similarity_override is not None
        else cosine_distance_to_similarity(distance)
    )
    confidence = clamp_number(record.get("confidence"), default=0.50)
    importance = clamp_number(record.get("importance"), default=0.50)
    status = str(record.get("review_status", "pending"))
    memory_type = str(record.get("memory_type", "semantic"))
    contexts = metadata_contexts(record)
    context_score, matched_contexts = context_match_score(contexts, options.contexts)
    profile_score, matched_profile = context_match_score(contexts, options.active_context_profile)
    matched_projects = project_tags_match(contexts, options.projects)

    score = (
        similarity * 0.70
        + confidence * 0.10
        + importance * 0.10
        + context_score * 0.08
        + REVIEW_STATUS_WEIGHTS.get(status, -0.04)
    )

    reasons = [f"status:{status}", f"type:{memory_type}", f"retrieval:{source}"]
    if status != "accepted" and options.allow_unreviewed_exploratory:
        reasons.append("exploratory:unreviewed")
    if matched_contexts:
        reasons.append("context:" + ",".join(matched_contexts))
    if matched_profile:
        score += profile_score * 0.08
        reasons.append("profile_context:" + ",".join(matched_profile))
    if matched_projects:
        reasons.append("project:" + ",".join(matched_projects))

    if options.enable_fitness_boost:
        effective_brain = (
            BRAIN_DIR
            if os.path.abspath(atoms_dir) == os.path.abspath(ATOMS_DIR)
            else os.path.abspath(os.path.join(atoms_dir, os.pardir))
        )
        effective_db = options.telemetry_db_path
        if effective_db is None and os.path.abspath(atoms_dir) != os.path.abspath(ATOMS_DIR):
            effective_db = os.path.join(effective_brain, ".sidecar", "telemetry.sqlite3")
        t_stats = _load_fitness_signals(filename, effective_db, brain_dir=effective_brain)
        if t_stats:
            # Retrieval hits are intentionally excluded: boosted results would otherwise accrue
            # hits and self-reinforce their own rank (feedback loop). Only grounded citations,
            # explicit owner feedback, and corrections influence the bounded fitness delta.
            utility = (
                (t_stats["grounded_citations"] * 2.0)
                + (t_stats["positive_feedback"] * 1.5)
                - (t_stats["corrections"] * 3.0)
            )
            fitness_delta = round(0.06 * math.tanh(utility / 10.0), 6)
            score += fitness_delta
            if fitness_delta > 0:
                reasons.append(f"fitness:boost(+{fitness_delta:.4f})")
            elif fitness_delta < 0:
                reasons.append(f"fitness:demote({fitness_delta:.4f};corrections={t_stats['corrections']})")
        freshness = _compute_freshness_bonus(record)
        if freshness > 0:
            score += freshness
            reasons.append(f"freshness:+{freshness:.4f}")

    return RecallResult(
        filename=filename,
        content=content,
        source=source,
        score=round(score, 6),
        similarity=round(similarity, 6),
        review_status=status,
        memory_type=memory_type,
        confidence=confidence,
        importance=importance,
        contexts=contexts,
        match_reasons=tuple(reasons),
    )


def _corroborating_vocab(
    content: str,
    content_sha256: str | None = None,
) -> tuple[frozenset[str], frozenset[str]] | None:
    content_hash = (
        content_sha256
        if (isinstance(content_sha256, str) and len(content_sha256) == 64)
        else hashlib.sha256(content.encode("utf-8")).hexdigest()
    )
    with _RECALL_PARSE_CACHE_LOCK:
        cached = _CORROBORATING_TERMS_BY_SHA.get(content_hash)
    if cached is not None:
        return cached
    parsed = split_frontmatter(content)
    if parsed is None:
        return None
    body = "\n".join(parsed[1])
    claim = section_text(body, "Claim")
    evidence = section_text(body, "Evidence")
    terms = frozenset(term.casefold() for term in TOKEN_PATTERN.findall(f"{claim}\n{evidence}"))
    stems = frozenset(porter_stem(t) for t in terms if len(t) >= 4)
    with _RECALL_PARSE_CACHE_LOCK:
        if len(_CORROBORATING_TERMS_BY_SHA) > 8192:
            _CORROBORATING_TERMS_BY_SHA.clear()
        _CORROBORATING_TERMS_BY_SHA[content_hash] = (terms, stems)
    return terms, stems


def _extract_query_terms(query: str) -> set[str]:
    return {
        term.casefold()
        for term in TOKEN_PATTERN.findall(str(query or ""))
        if term.casefold() not in QUERY_STOP_WORDS and len(term) > 1
    }


def candidate_has_query_overlap(
    query: str,
    content: str,
    *,
    content_sha256: str | None = None,
) -> bool:
    """Require literal or single-term stem claim/evidence corroboration before trusted recall injects a result."""
    vocab = _corroborating_vocab(content, content_sha256=content_sha256)
    if vocab is None:
        return False
    corroborating_terms, corroborating_stems = vocab
    query_terms = _extract_query_terms(query)
    if not query_terms:
        return False
    required = min(2, len(query_terms))
    literal_overlap = len(query_terms.intersection(corroborating_terms))
    if literal_overlap >= required:
        return True
    if required == 1:
        only_term = next(iter(query_terms))
        stem = porter_stem(only_term)
        if len(stem) >= 4 and stem in corroborating_stems:
            return True
    return False


def candidate_has_semantic_corroboration(
    query: str,
    content: str,
    *,
    distance: float | int | str | None = None,
    content_sha256: str | None = None,
) -> bool:
    """Allow calibrated high-confidence dense/RRF synonym matches while blocking adversarial traps."""
    if candidate_has_query_overlap(query, content, content_sha256=content_sha256):
        return True
    vocab = _corroborating_vocab(content, content_sha256=content_sha256)
    if vocab is None:
        return False
    corroborating_terms, corroborating_stems = vocab
    query_terms = _extract_query_terms(query)
    if not query_terms or distance is None:
        return False
    sim = cosine_distance_to_similarity(distance)
    literal_hits = len(query_terms.intersection(corroborating_terms))
    stem_hits = sum(1 for qt in query_terms if len(porter_stem(qt)) >= 4 and porter_stem(qt) in corroborating_stems)
    if (literal_hits >= 1 or stem_hits >= 1) and sim >= 0.86:
        return True
    return sim >= 0.90


def wikilinks(content: str) -> list[str]:
    return [normalize_markdown_filename(link) for link in re.findall(r"\[\[(.*?)\]\]", content or "")]


def linked_neighbors(
    result: RecallResult,
    options: RecallOptions,
    atoms_dir: str = ATOMS_DIR,
    *,
    query: str,
) -> list[RecallResult]:
    neighbors = []
    neighbor_options = (
        replace(options, projects=())
        if options.projects and options.allow_cross_project
        else options
    )
    for target in wikilinks(result.content):
        target_path = safe_file_in_dir(atoms_dir, target, allowed_exts={".md"})
        if not target_path:
            continue
        with open(target_path, "r", encoding="utf-8") as file:
            content = file.read()
        if not candidate_has_query_overlap(query, content):
            continue
        neighbor = result_from_candidate(
            content=content,
            filename=target,
            distance=0.35,
            options=neighbor_options,
            source="graph",
            atoms_dir=atoms_dir,
        )
        if neighbor:
            extra_reasons = [f"linked_from:{result.filename}"]
            if options.projects and not project_tags_match(neighbor.contexts, options.projects):
                extra_reasons.append("cross_project_reference")
            neighbors.append(
                replace(neighbor, match_reasons=neighbor.match_reasons + tuple(extra_reasons))
            )
    return neighbors


def verified_atom_content(filename: str, expected_sha256: str, atoms_dir: str = ATOMS_DIR) -> str | None:
    """Load an atom only when the vector record proves it was derived from this exact revision."""
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        return None
    path = safe_file_in_dir(atoms_dir, filename, allowed_exts={".md"})
    if not path:
        return None
    try:
        with open(path, "rb") as file:
            content_bytes = file.read()
        content = content_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    actual_sha256 = hashlib.sha256(content_bytes).hexdigest()
    return content if actual_sha256 == expected_sha256 else None


_ORIG_LOAD_MEMORY_METADATA = load_memory_metadata
_ORIG_VERIFIED_ATOM_CONTENT = verified_atom_content


def _preload_batch_decision_map(atoms_dir: str) -> dict[str, dict] | None:
    effective_brain_dir = (
        BRAIN_DIR
        if os.path.abspath(atoms_dir) == os.path.abspath(ATOMS_DIR)
        else os.path.abspath(os.path.join(atoms_dir, os.pardir))
    )
    decision_log = (
        os.path.join(RAW_DIR, "archive", "decision-record.jsonl")
        if os.path.abspath(atoms_dir) == os.path.abspath(ATOMS_DIR)
        else os.path.join(effective_brain_dir, "0-raw", "archive", "decision-record.jsonl")
    )
    try:
        return latest_decisions_by_atom(decision_log)
    except Exception:
        return None


def rerank_candidates(
    results: dict,
    options: RecallOptions,
    *,
    query: str,
    atoms_dir: str = ATOMS_DIR,
) -> list[RecallResult]:
    documents = (results.get("documents") or [[]])[0]
    metadatas = (results.get("metadatas") or [[]])[0]
    distances = (results.get("distances") or [[]])[0]

    use_fast_verify = (
        verified_atom_content is _ORIG_VERIFIED_ATOM_CONTENT
        and load_memory_metadata is _ORIG_LOAD_MEMORY_METADATA
    )
    canon_root = _resolve_canonical_atoms_root(atoms_dir) if use_fast_verify else None
    decision_map = _preload_batch_decision_map(atoms_dir) if use_fast_verify else None
    source_sha_memo: dict[str, str | None] = {}

    ranked = []
    for index, content in enumerate(documents):
        metadata = metadatas[index] if index < len(metadatas) and isinstance(metadatas[index], dict) else {}
        distance = distances[index] if index < len(distances) else None
        filename = metadata.get("filename", "unknown.md")
        expected_sha = metadata.get("content_sha256", "")
        if use_fast_verify and canon_root is not None:
            if isinstance(content, str) and not candidate_has_semantic_corroboration(
                query, content, distance=distance, content_sha256=expected_sha
            ):
                continue
            verified_bundle = _read_verified_atom_bytes_fast(
                filename,
                expected_sha,
                canonical_atoms_root=canon_root,
                atoms_dir=atoms_dir,
                candidate_content=content if isinstance(content, str) else None,
            )
            if verified_bundle is None:
                continue
            current_content = verified_bundle[2]
            if not candidate_has_semantic_corroboration(
                query, current_content, distance=distance, content_sha256=expected_sha
            ):
                continue
            rec = load_memory_metadata(
                filename,
                atoms_dir=atoms_dir,
                _preverified=verified_bundle,
                _known_sha256=expected_sha,
                _canonical_atoms_root=canon_root,
                _decision_map=decision_map,
                _source_sha_memo=source_sha_memo,
            )
            result = result_from_candidate(
                content=current_content,
                filename=filename,
                distance=distance,
                options=options,
                source="vector",
                atoms_dir=atoms_dir,
                _preloaded_record=rec,
            )
            if result:
                ranked.append(result)
            continue

        current_content = verified_atom_content(
            filename,
            expected_sha,
            atoms_dir=atoms_dir,
        )
        if current_content is None:
            continue
        result = result_from_candidate(
            content=current_content,
            filename=filename,
            distance=distance,
            options=options,
            source="vector",
            atoms_dir=atoms_dir,
        )
        if result:
            if candidate_has_semantic_corroboration(query, current_content, distance=distance):
                ranked.append(result)

    return sorted(ranked, key=lambda item: item.score, reverse=True)[: options.n_results]


def search_semantic_memory(
    query: str,
    options: RecallOptions | None = None,
    *,
    atoms_dir: str = ATOMS_DIR,
    chroma_path: str = CHROMA_PATH,
) -> tuple[list[RecallResult], list[RecallResult]]:
    from ollama_utils import EmbeddingUnavailableError, embed_text

    options = options or RecallOptions()
    if not os.path.exists(chroma_path):
        raise MemoryRecallUnavailable("Dense vector store directory is missing; run scripts/rebuild_vectors.py.")

    manifest = load_index_manifest(chroma_path)

    try:
        collection = get_vector_collection(chroma_path, name="atoms", create=False)
    except Exception as exc:
        raise MemoryRecallUnavailable(f"Semantic recall index is unavailable: {exc}") from exc

    try:
        try:
            embedding = embed_text(query, model=EMBED_MODEL)
        except EmbeddingUnavailableError as exc:
            raise MemoryRecallUnavailable(f"Semantic recall is unavailable: {exc}") from exc
        generation_id = validated_index_generation(
            manifest,
            atoms_dir=atoms_dir,
            collection_name="atoms",
            embedding_model=EMBED_MODEL,
            embedding_dimensions=len(embedding),
        )
        candidate_count = max(options.n_results, options.n_results * max(1, options.candidate_multiplier))
        try:
            results = collection.query(
                query_embeddings=[embedding],
                n_results=candidate_count,
                where={
                    "$and": [
                        {"embedding_model": EMBED_MODEL},
                        {"index_generation": generation_id},
                    ]
                },
            )
        except Exception as exc:
            raise MemoryRecallUnavailable(f"Semantic recall query failed: {exc}") from exc
    finally:
        close_vector_collection(collection)
    direct = rerank_candidates(results, options, query=query, atoms_dir=atoms_dir)

    neighbors = []
    seen = {item.filename for item in direct}
    if options.follow_links:
        for result in direct:
            for neighbor in linked_neighbors(result, options, atoms_dir=atoms_dir, query=query):
                if neighbor.filename not in seen:
                    seen.add(neighbor.filename)
                    neighbors.append(neighbor)

    neighbors = sorted(neighbors, key=lambda item: item.score, reverse=True)
    return direct, neighbors


def search_lexical_memory(
    query: str,
    options: RecallOptions | None = None,
    *,
    atoms_dir: str = ATOMS_DIR,
    index_path: str = LEXICAL_INDEX_PATH,
) -> tuple[list[RecallResult], list[RecallResult]]:
    """Retrieve through the model-free sidecar, then apply the canonical trust filter."""
    options = options or RecallOptions()
    candidate_count = max(options.n_results, options.n_results * max(1, options.candidate_multiplier))
    use_fast_verify = (
        verified_atom_content is _ORIG_VERIFIED_ATOM_CONTENT
        and load_memory_metadata is _ORIG_LOAD_MEMORY_METADATA
    )
    verified_rev_map: dict[str, str] | None = None
    if use_fast_verify and search_lexical_index is _ORIG_SEARCH_LEXICAL_INDEX:
        candidates, verified_rev_map = _search_lexical_index_verified(
            query, index_path, n_results=candidate_count, atoms_dir=atoms_dir
        )
    else:
        candidates = search_lexical_index(query, index_path, n_results=candidate_count, atoms_dir=atoms_dir)
    if not candidates:
        return [], []

    fitness_or_profile = bool(options.enable_fitness_boost or options.active_context_profile)
    if verified_rev_map is not None:
        canon_root: str | None = os.path.abspath(atoms_dir)
    else:
        canon_root = _resolve_canonical_atoms_root(atoms_dir) if use_fast_verify else None
    decision_map = _preload_batch_decision_map(atoms_dir) if use_fast_verify else None
    source_sha_memo: dict[str, str | None] = {}

    # Apply the canonical content/overlap/trust gates once per candidate so untrusted
    # (pending, out-of-scope, tampered) candidates never participate in BM25 normalization.
    trusted_gated: list[tuple[object, str, dict]] = []
    for candidate in candidates:
        if use_fast_verify and canon_root is not None:
            if not candidate_has_query_overlap(
                query, candidate.content, content_sha256=candidate.content_sha256
            ):
                continue
            verified_bundle: tuple[str, bytes, str] | None = None
            if (
                verified_rev_map is not None
                and verified_rev_map.get(candidate.filename) == candidate.content_sha256
            ):
                try:
                    cand_bytes = candidate.content.encode("utf-8")
                except UnicodeEncodeError:
                    cand_bytes = b""
                if cand_bytes and hashlib.sha256(cand_bytes).hexdigest() == candidate.content_sha256:
                    verified_bundle = (
                        os.path.join(canon_root, candidate.filename),
                        cand_bytes,
                        candidate.content,
                    )
            if verified_bundle is None:
                verified_bundle = _read_verified_atom_bytes_fast(
                    candidate.filename,
                    candidate.content_sha256,
                    canonical_atoms_root=canon_root,
                    atoms_dir=atoms_dir,
                    candidate_content=candidate.content,
                )
            if verified_bundle is None:
                continue
            current_content = verified_bundle[2]
            if current_content is not candidate.content and not candidate_has_query_overlap(
                query, current_content, content_sha256=candidate.content_sha256
            ):
                continue
            rec = load_memory_metadata(
                candidate.filename,
                atoms_dir=atoms_dir,
                _preverified=verified_bundle,
                _known_sha256=candidate.content_sha256,
                _canonical_atoms_root=canon_root,
                _decision_map=decision_map,
                _source_sha_memo=source_sha_memo,
            )
        else:
            current_content = verified_atom_content(
                candidate.filename, candidate.content_sha256, atoms_dir=atoms_dir
            )
            if current_content is None or not candidate_has_query_overlap(query, current_content):
                continue
            rec = load_memory_metadata(candidate.filename, atoms_dir=atoms_dir)
        if not should_include_record(rec, options):
            continue
        trusted_gated.append((candidate, current_content, rec))

    best_bm25 = (
        max((abs(float(cand.rank)) for cand, _, _ in trusted_gated), default=0.0)
        if fitness_or_profile
        else 0.0
    )
    use_bm25_ratio = fitness_or_profile and best_bm25 > 0.0

    direct = []
    for trusted_position, (candidate, current_content, rec) in enumerate(trusted_gated):
        if use_bm25_ratio:
            lexical_similarity = clamp_number(abs(float(candidate.rank)) / best_bm25)
        else:
            lexical_similarity = 1.0 / (trusted_position + 1)
        result = result_from_candidate(
            content=current_content,
            filename=candidate.filename,
            distance=None,
            options=options,
            source="lexical",
            atoms_dir=atoms_dir,
            similarity_override=lexical_similarity,
            _preloaded_record=rec,
        )
        if result:
            direct.append(result)

    direct = sorted(direct, key=lambda item: item.score, reverse=True)[: options.n_results]
    neighbors = []
    seen = {item.filename for item in direct}
    if options.follow_links:
        for result in direct:
            for neighbor in linked_neighbors(result, options, atoms_dir=atoms_dir, query=query):
                if neighbor.filename not in seen:
                    seen.add(neighbor.filename)
                    neighbors.append(neighbor)
    return direct, sorted(neighbors, key=lambda item: item.score, reverse=True)


def _tag_degraded_results(
    direct: list[RecallResult],
    neighbors: list[RecallResult],
) -> tuple[list[RecallResult], list[RecallResult]]:
    tagged_direct = [
        replace(item, match_reasons=item.match_reasons + ("degraded:lexical_fallback",))
        for item in direct
    ]
    tagged_neighbors = [
        replace(item, match_reasons=item.match_reasons + ("degraded:lexical_fallback",))
        for item in neighbors
    ]
    return tagged_direct, tagged_neighbors


def search_hybrid_rrf_memory(
    query: str,
    options: RecallOptions | None = None,
    *,
    atoms_dir: str = ATOMS_DIR,
    index_path: str = LEXICAL_INDEX_PATH,
    chroma_path: str = CHROMA_PATH,
    k_constant: int = 60,
) -> tuple[list[RecallResult], list[RecallResult]]:
    """Reciprocal Rank Fusion (RRF) combining SQLite FTS5 (lexical) and Chroma (dense)."""
    options = options or RecallOptions()
    pool_size = max(options.n_results * 4, 20)
    pool_options = replace(options, n_results=pool_size, follow_links=False)

    lex_pool, _ = search_lexical_memory(
        query, pool_options, atoms_dir=atoms_dir, index_path=index_path
    )
    try:
        dense_pool, _ = search_semantic_memory(
            query, pool_options, atoms_dir=atoms_dir, chroma_path=chroma_path
        )
    except MemoryRecallUnavailable as exc:
        emit_degradation_warning(str(exc))
        fallback_direct = lex_pool[: options.n_results]
        fallback_neighbors = []
        seen_fb = {item.filename for item in fallback_direct}
        if options.follow_links:
            for result in fallback_direct:
                for neighbor in linked_neighbors(result, options, atoms_dir=atoms_dir, query=query):
                    if neighbor.filename not in seen_fb:
                        seen_fb.add(neighbor.filename)
                        fallback_neighbors.append(neighbor)
        fallback_neighbors = sorted(fallback_neighbors, key=lambda item: item.score, reverse=True)
        return _tag_degraded_results(fallback_direct, fallback_neighbors)

    rrf_scores: dict[str, float] = {}
    candidate_map: dict[str, RecallResult] = {}
    lex_ranks: dict[str, int] = {}
    dense_ranks: dict[str, int] = {}

    for rank, res in enumerate(lex_pool):
        rrf_scores[res.filename] = rrf_scores.get(res.filename, 0.0) + (1.0 / (k_constant + rank + 1))
        candidate_map[res.filename] = res
        lex_ranks[res.filename] = rank + 1

    for rank, res in enumerate(dense_pool):
        rrf_scores[res.filename] = rrf_scores.get(res.filename, 0.0) + (1.0 / (k_constant + rank + 1))
        dense_ranks[res.filename] = rank + 1
        if res.filename not in candidate_map:
            candidate_map[res.filename] = res

    fused: list[RecallResult] = []
    for filename, rrf_score in sorted(rrf_scores.items(), key=lambda item: item[1], reverse=True)[: options.n_results]:
        base = candidate_map[filename]
        extra = ["retrieval:rrf_hybrid", f"rrf_score:{round(rrf_score, 6)}"]
        if filename in lex_ranks:
            extra.append(f"lexical_rank:{lex_ranks[filename]}")
        if filename in dense_ranks:
            extra.append(f"dense_rank:{dense_ranks[filename]}")
        base_reasons = tuple(r for r in base.match_reasons if not r.startswith("retrieval:"))
        fused.append(
            RecallResult(
                filename=base.filename,
                content=base.content,
                source="rrf_hybrid",
                score=round(rrf_score, 6),
                similarity=base.similarity,
                review_status=base.review_status,
                memory_type=base.memory_type,
                confidence=base.confidence,
                importance=base.importance,
                contexts=base.contexts,
                match_reasons=base_reasons + tuple(extra),
            )
        )

    neighbors = []
    seen = {item.filename for item in fused}
    if options.follow_links:
        for result in fused:
            for neighbor in linked_neighbors(result, options, atoms_dir=atoms_dir, query=query):
                if neighbor.filename not in seen:
                    seen.add(neighbor.filename)
                    neighbors.append(neighbor)

    return fused, sorted(neighbors, key=lambda item: item.score, reverse=True)


def search_memory(
    query: str,
    options: RecallOptions | None = None,
    *,
    atoms_dir: str = ATOMS_DIR,
    index_path: str = LEXICAL_INDEX_PATH,
    chroma_path: str = CHROMA_PATH,
) -> tuple[list[RecallResult], list[RecallResult]]:
    """Retrieve memories using configured mode ('auto', 'rrf', 'lexical', or 'dense')."""
    options = options or RecallOptions()
    if options.retrieval_mode == "rrf":
        return search_hybrid_rrf_memory(
            query, options, atoms_dir=atoms_dir, index_path=index_path, chroma_path=chroma_path
        )
    if options.retrieval_mode == "dense":
        try:
            return search_semantic_memory(query, options, atoms_dir=atoms_dir, chroma_path=chroma_path)
        except MemoryRecallUnavailable as exc:
            if os.path.exists(index_path):
                emit_degradation_warning(str(exc))
                lex_direct, lex_neighbors = search_lexical_memory(
                    query, options, atoms_dir=atoms_dir, index_path=index_path
                )
                return _tag_degraded_results(lex_direct, lex_neighbors)
            raise
    if options.retrieval_mode == "lexical":
        return search_lexical_memory(query, options, atoms_dir=atoms_dir, index_path=index_path)

    # auto mode: use lexical if sidecar exists, else dense
    if os.path.exists(index_path):
        return search_lexical_memory(query, options, atoms_dir=atoms_dir, index_path=index_path)
    return search_semantic_memory(query, options, atoms_dir=atoms_dir, chroma_path=chroma_path)


def suggest_duplicate_atoms(
    atoms_dir: str = ATOMS_DIR,
    min_jaccard: float = 0.35,
    *,
    require_current_approval: bool = False,
    projects: tuple[str, ...] = (),
    brain_dir: str | None = None,
) -> list[dict]:
    """Read-only review aid: identify candidate duplicate atoms without mutating files or trust."""
    if not os.path.isdir(atoms_dir):
        return []
    if brain_dir is not None:
        resolved_brain_dir = os.path.abspath(brain_dir)
    elif os.path.abspath(atoms_dir) == os.path.abspath(ATOMS_DIR):
        resolved_brain_dir = os.path.abspath(BRAIN_DIR)
    else:
        resolved_brain_dir = os.path.abspath(os.path.join(atoms_dir, os.pardir))
    requested_projects = tuple(str(p).strip() for p in (projects or ()) if str(p or "").strip())

    entries = []
    for filename in sorted(os.listdir(atoms_dir)):
        if not is_content_markdown(filename):
            continue
        path = safe_file_in_dir(atoms_dir, filename, allowed_exts={".md"})
        if not path:
            continue
        try:
            with open(path, "rb") as file_obj:
                raw_bytes = file_obj.read()
            content = raw_bytes.decode("utf-8")
            record = parse_atom_record(path)
        except Exception:
            continue
        if record.get("review_status") in EXCLUDED_RECALL_STATUSES or record.get("superseded_by"):
            continue
        if require_current_approval:
            if record.get("review_status") != "accepted" or record.get("claim_state") != "human_reviewed":
                continue
            if not approved_revision_is_current(path, brain_dir=resolved_brain_dir, atoms_dir=atoms_dir):
                continue
        if requested_projects:
            if not project_tags_match(metadata_contexts(record), requested_projects):
                continue
        parsed = split_frontmatter(content)
        body = "\n".join(parsed[1]) if parsed else content
        claim = record.get("claim", "")
        evidence = section_text(body, "Evidence")
        tokens = {
            t.casefold()
            for t in TOKEN_PATTERN.findall(f"{claim}\n{evidence}")
            if t.casefold() not in QUERY_STOP_WORDS and len(t) > 2
        }
        if not tokens:
            continue
        entries.append({
            "filename": filename,
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "claim": claim,
            "review_status": record.get("review_status", "pending"),
            "origin": record.get("origin", "imported_unknown"),
            "tokens": tokens,
        })

    suggestions = []
    for idx, left in enumerate(entries):
        for right in entries[idx + 1 :]:
            union = left["tokens"] | right["tokens"]
            if not union:
                continue
            jaccard = len(left["tokens"] & right["tokens"]) / len(union)
            if jaccard >= min_jaccard:
                suggestions.append({
                    "atom_a": left["filename"],
                    "atom_b": right["filename"],
                    "jaccard_similarity": round(jaccard, 4),
                    "atom_a_sha256": left["sha256"],
                    "atom_b_sha256": right["sha256"],
                    "atom_a_status": left["review_status"],
                    "atom_b_status": right["review_status"],
                    "atom_a_origin": left["origin"],
                    "atom_b_origin": right["origin"],
                    "atom_a_claim": left["claim"],
                    "atom_b_claim": right["claim"],
                })
    return sorted(suggestions, key=lambda item: item["jaccard_similarity"], reverse=True)


def format_context(results: list[RecallResult]) -> str:
    parts = []
    for result in results:
        trust = (
            f"status={result.review_status}; type={result.memory_type}; "
            f"confidence={result.confidence:.2f}; importance={result.importance:.2f}; "
            f"score={result.score:.3f}"
        )
        parts.append(f"--- Source Atom: {result.filename} ({trust}) ---\n{result.content}")
    return "\n\n".join(parts)


def assemble_bounded_working_context(
    results: list[RecallResult],
    *,
    max_chars: int = 6000,
    max_atoms: int = 6,
    atoms_dir: str = ATOMS_DIR,
    chroma_path: str = CHROMA_PATH,
) -> WorkingContextBundle:
    """Assemble a bounded, whole-atom working context bundle without truncating Claim or Evidence text."""
    effective_budget = max(256, int(max_chars))
    effective_cap = max(1, int(max_atoms))

    manifest = load_index_manifest(chroma_path) if os.path.isdir(chroma_path) else None
    gen_id = str(manifest["generation_id"]) if isinstance(manifest, dict) and manifest.get("generation_id") else None

    included: list[RecallResult] = []
    included_ids: list[str] = []
    deferred_ids: list[str] = []
    sha_map: dict[str, str] = {}
    used_chars = 0

    for item in results:
        if item.filename in sha_map or item.filename in deferred_ids:
            continue
        atom_len = len(item.content)
        separator_len = 2 if included else 0
        if len(included) >= effective_cap or (used_chars + separator_len + atom_len) > effective_budget:
            deferred_ids.append(item.filename)
            continue

        # Hash the exact verified content being included (not a fresh re-read of the file),
        # so the reported SHA always describes the bytes actually placed in working context.
        sha_map[item.filename] = hashlib.sha256(item.content.encode("utf-8")).hexdigest()

        included.append(item)
        included_ids.append(item.filename)
        used_chars += separator_len + atom_len

    return WorkingContextBundle(
        included_results=tuple(included),
        included_atom_ids=tuple(included_ids),
        deferred_atom_ids=tuple(deferred_ids),
        used_chars=used_chars,
        budget_chars=effective_budget,
        max_atoms=effective_cap,
        index_generation=gen_id,
        source_sha256_map=sha_map,
        formatted_context=format_context(included),
    )
