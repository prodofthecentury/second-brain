# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Cognitive Core layer: disposable sidecar telemetry, memory fitness reports, and experience records."""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass
import datetime
import json
import math
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import approved_revision_is_current
from config import (
    ATOMS_DIR,
    BRAIN_DIR,
    EXPERIENCES_DIR,
    INBOX_DIR,
    LEXICAL_INDEX_PATH,
    TELEMETRY_DB_PATH,
    _WINDOWS_RESERVED_STEMS,
    _is_reparse_or_link,
    file_sha256,
    is_content_markdown,
    normalize_markdown_filename,
    safe_file_in_dir,
)
from lexical_store import search_lexical_index
from memory_format import split_frontmatter
from memory_review import parse_frontmatter_values
from review_atom import review_lock


@dataclass(frozen=True)
class ExperienceRecord:
    experience_id: str
    created_at: str
    goal: str
    context: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    tools_used: tuple[str, ...] = ()
    referenced_atoms: tuple[str, ...] = ()
    result_summary: str = ""
    success: bool = True
    lessons: tuple[str, ...] = ()
    candidate_procedure_title: str | None = None
    plan: tuple[str, ...] = ()
    reasoning_summary: str = ""
    confidence_before: float | None = None
    confidence_after: float | None = None
    new_memories: tuple[str, ...] = ()
    updated_memories: tuple[str, ...] = ()
    candidate_skills: tuple[str, ...] = ()
    reflection: str = ""


def init_telemetry_db(db_path: str = TELEMETRY_DB_PATH) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS atom_telemetry (
                atom_id TEXT PRIMARY KEY,
                retrieval_hits INTEGER NOT NULL DEFAULT 0,
                candidate_hits INTEGER NOT NULL DEFAULT 0,
                grounded_citations INTEGER NOT NULL DEFAULT 0,
                positive_feedback INTEGER NOT NULL DEFAULT 0,
                corrections INTEGER NOT NULL DEFAULT 0,
                last_retrieved_at TEXT,
                last_cited_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS query_telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                mode TEXT NOT NULL,
                result_count INTEGER NOT NULL,
                abstained INTEGER NOT NULL,
                latency_ms REAL NOT NULL
            )
            """
        )
        conn.commit()


def _valid_atom_id(atom_id: str) -> bool:
    return (
        isinstance(atom_id, str)
        and atom_id.endswith(".md")
        and os.path.basename(atom_id) == atom_id
        and atom_id not in {".", ".."}
    )


def peek_lexical_candidate_atom_ids(
    query: str,
    *,
    index_path: str = LEXICAL_INDEX_PATH,
    atoms_dir: str = ATOMS_DIR,
    limit: int = 20,
) -> list[str]:
    """Return raw FTS5 candidate atom filenames (without content) for bottleneck telemetry."""
    try:
        candidates = search_lexical_index(query, index_path, n_results=limit, atoms_dir=atoms_dir)
        return [c.filename for c in candidates if _valid_atom_id(c.filename)]
    except Exception:
        return []


def record_retrieval_telemetry(
    retrieved_atom_ids: list[str],
    *,
    candidate_atom_ids: list[str] | None = None,
    mode: str = "lexical",
    latency_ms: float = 0.0,
    db_path: str = TELEMETRY_DB_PATH,
) -> None:
    """Record content-free retrieval hit counts in the disposable sidecar SQLite DB."""
    init_telemetry_db(db_path)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    clean_retrieved = [aid for aid in retrieved_atom_ids if _valid_atom_id(aid)]
    clean_candidates = [aid for aid in (candidate_atom_ids or []) if _valid_atom_id(aid)]

    with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
        for aid in clean_retrieved:
            conn.execute(
                """
                INSERT INTO atom_telemetry (atom_id, retrieval_hits, last_retrieved_at)
                VALUES (?, 1, ?)
                ON CONFLICT(atom_id) DO UPDATE SET
                    retrieval_hits = retrieval_hits + 1,
                    last_retrieved_at = excluded.last_retrieved_at
                """,
                (aid, now),
            )
        for aid in clean_candidates:
            conn.execute(
                """
                INSERT INTO atom_telemetry (atom_id, candidate_hits)
                VALUES (?, 1)
                ON CONFLICT(atom_id) DO UPDATE SET
                    candidate_hits = candidate_hits + 1
                """,
                (aid,),
            )
        conn.execute(
            """
            INSERT INTO query_telemetry (recorded_at, event_type, mode, result_count, abstained, latency_ms)
            VALUES (?, 'recall', ?, ?, ?, ?)
            """,
            (now, str(mode)[:32], len(clean_retrieved), 1 if not clean_retrieved else 0, float(latency_ms)),
        )
        conn.commit()


def record_grounded_answer_telemetry(
    cited_atom_ids: list[str],
    *,
    abstained: bool,
    mode: str = "standard",
    latency_ms: float = 0.0,
    db_path: str = TELEMETRY_DB_PATH,
) -> None:
    init_telemetry_db(db_path)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    clean_cited = [aid for aid in cited_atom_ids if _valid_atom_id(aid)]
    with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
        for aid in clean_cited:
            conn.execute(
                """
                INSERT INTO atom_telemetry (atom_id, grounded_citations, last_cited_at)
                VALUES (?, 1, ?)
                ON CONFLICT(atom_id) DO UPDATE SET
                    grounded_citations = grounded_citations + 1,
                    last_cited_at = excluded.last_cited_at
                """,
                (aid, now),
            )
        conn.execute(
            """
            INSERT INTO query_telemetry (recorded_at, event_type, mode, result_count, abstained, latency_ms)
            VALUES (?, 'answer', ?, ?, ?, ?)
            """,
            (now, str(mode)[:32], len(clean_cited), 1 if abstained else 0, float(latency_ms)),
        )
        conn.commit()


def _brain_dir_for_telemetry_db(db_path: str) -> str | None:
    """Telemetry sidecars live at <brain>/.sidecar/telemetry.sqlite3; derive the owning vault."""
    abs_db = os.path.abspath(db_path)
    if abs_db == os.path.abspath(TELEMETRY_DB_PATH):
        return BRAIN_DIR
    parent = os.path.dirname(abs_db)
    if os.path.basename(parent) == ".sidecar":
        return os.path.dirname(parent)
    return None


from pathlib import Path
import threading


_DURABLE_FB_LOCK = threading.Lock()
# Maps abs_log_path -> (mtime_ns, size_bytes, per_atom_counts)
_DURABLE_FB_CACHE: dict[str, tuple[int, int, dict[str, dict[str, object]]]] = {}


def atom_correction_log_path(brain_dir: str) -> str:
    return os.path.join(brain_dir, "0-raw", "archive", "atom-corrections.jsonl")


def atom_feedback_log_path(brain_dir: str) -> str:
    return os.path.join(brain_dir, "0-raw", "archive", "atom-feedback.jsonl")


def _restrict_durable_log_permissions(log_path: str) -> None:
    try:
        from host_adapters import restrict_security_file_permissions

        restrict_security_file_permissions(Path(log_path))
    except Exception:
        if os.name != "nt":
            try:
                os.chmod(log_path, 0o600)
            except OSError:
                pass


def _append_canonical_feedback(
    brain_dir: str,
    atom_id: str,
    *,
    feedback_kind: str,
    helpful: bool,
    actor: str = "owner",
    note: str = "",
) -> None:
    log_path = atom_feedback_log_path(brain_dir)
    archive_dir = os.path.dirname(log_path)
    os.makedirs(archive_dir, exist_ok=True)
    if _is_reparse_or_link(archive_dir) or (os.path.lexists(log_path) and _is_reparse_or_link(log_path)):
        raise PermissionError("Refusing to write atom feedback log through a symlink or reparse point.")
    record = {
        "schema_version": 1,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "action": f"atom_{feedback_kind}",
        "feedback_kind": feedback_kind,
        "helpful": bool(helpful),
        "atom_id": atom_id,
        "actor": _sanitize_single_line(actor or "owner", max_len=120) or "owner",
        "note": _sanitize_single_line(note or "", max_len=500),
    }
    encoded = (json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if os.write(fd, encoded) != len(encoded):
            raise OSError("Incomplete write to atom-feedback.jsonl.")
        os.fsync(fd)
    finally:
        os.close(fd)
    _restrict_durable_log_permissions(log_path)


def _append_durable_feedback(
    brain_dir: str,
    atom_id: str,
    *,
    action: str,
    actor: str = "owner",
    note: str = "",
) -> None:
    """Persist durable feedback/correction/supersession records in 0-raw/archive/atom-corrections.jsonl."""
    log_path = atom_correction_log_path(brain_dir)
    archive_dir = os.path.dirname(log_path)
    os.makedirs(archive_dir, exist_ok=True)
    if _is_reparse_or_link(archive_dir) or (os.path.lexists(log_path) and _is_reparse_or_link(log_path)):
        raise PermissionError("Refusing to write atom correction log through a symlink or reparse point.")
    clean_actor = _sanitize_single_line(actor or "owner", max_len=120) or "owner"
    clean_note = _sanitize_single_line(note or "", max_len=500)
    record: dict[str, object] = {
        "schema_version": 1,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "action": action,
        "atom_id": atom_id,
        "actor": clean_actor,
        "note": clean_note,
    }
    encoded = (json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if os.write(fd, encoded) != len(encoded):
            raise OSError("Incomplete write to atom-corrections.jsonl.")
        os.fsync(fd)
    finally:
        os.close(fd)
    _restrict_durable_log_permissions(log_path)
    with _DURABLE_FB_LOCK:
        _DURABLE_FB_CACHE.pop(os.path.abspath(log_path), None)


def _append_canonical_correction(
    brain_dir: str,
    atom_id: str,
    *,
    actor: str = "owner",
    note: str = "",
) -> None:
    """Durable append-only correction record so Playbook staleness never depends on a disposable sidecar."""
    _append_durable_feedback(brain_dir, atom_id, action="atom_correction", actor=actor, note=note)


def load_durable_feedback_counts(brain_dir: str) -> dict[str, dict[str, object]]:
    """Load per-atom positive_feedback, corrections, supersessions, last_actor, and last_note from atom-corrections.jsonl."""
    if not brain_dir:
        return {}
    log_path = atom_correction_log_path(brain_dir)
    abs_log = os.path.abspath(log_path)
    if not os.path.lexists(abs_log):
        return {}
    if _is_reparse_or_link(abs_log) or _is_reparse_or_link(os.path.dirname(abs_log)):
        return {}
    try:
        st = os.stat(abs_log)
    except OSError:
        return {}
    with _DURABLE_FB_LOCK:
        cached = _DURABLE_FB_CACHE.get(abs_log)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return {k: dict(v) for k, v in cached[2].items()}

    counts: dict[str, dict[str, object]] = {}
    try:
        with open(abs_log, "r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                aid = rec.get("atom_id")
                if not isinstance(aid, str) or not aid:
                    continue
                act = str(rec.get("action") or "")
                if act not in {"atom_helpful", "atom_endorsed", "atom_correction", "atom_supersession"}:
                    continue
                bucket = counts.setdefault(
                    aid,
                    {
                        "positive_feedback": 0,
                        "corrections": 0,
                        "supersessions": 0,
                        "last_actor": "",
                        "last_note": "",
                    },
                )
                if act in {"atom_helpful", "atom_endorsed"}:
                    bucket["positive_feedback"] = int(bucket["positive_feedback"]) + 1
                elif act == "atom_correction":
                    bucket["corrections"] = int(bucket["corrections"]) + 1
                elif act == "atom_supersession":
                    bucket["supersessions"] = int(bucket["supersessions"]) + 1
                bucket["last_actor"] = str(rec.get("actor") or "")
                bucket["last_note"] = str(rec.get("note") or "")
    except OSError:
        return {}

    with _DURABLE_FB_LOCK:
        if len(_DURABLE_FB_CACHE) > 256:
            _DURABLE_FB_CACHE.clear()
        _DURABLE_FB_CACHE[abs_log] = (st.st_mtime_ns, st.st_size, counts)
    return {k: dict(v) for k, v in counts.items()}


def count_canonical_corrections(brain_dir: str, atom_id: str) -> int | None:
    """Count durable correction records for an atom; None when the log is unsafe or unreadable (fail closed)."""
    log_path = atom_correction_log_path(brain_dir)
    if not os.path.lexists(log_path):
        return 0
    if _is_reparse_or_link(log_path) or _is_reparse_or_link(os.path.dirname(log_path)):
        return None
    count = 0
    try:
        with open(log_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("action") == "atom_correction" and rec.get("atom_id") == atom_id:
                    count += 1
    except OSError:
        return None
    return count


def record_atom_feedback(
    atom_id: str,
    *,
    helpful: bool = True,
    actor: str = "owner",
    note: str = "",
    feedback_kind: str | None = None,
    db_path: str = TELEMETRY_DB_PATH,
    brain_dir: str | None = None,
) -> dict[str, object]:
    if not _valid_atom_id(atom_id):
        raise ValueError(f"Invalid atom_id for telemetry feedback: {atom_id!r}")

    if feedback_kind is not None:
        fk = str(feedback_kind).strip().lower()
        if fk not in {"helpful", "correction", "supersession"}:
            raise ValueError(
                f"Invalid feedback_kind {feedback_kind!r}; expected one of 'helpful', 'correction', 'supersession'."
            )
        resolved_kind = fk
    else:
        resolved_kind = "helpful" if helpful else "correction"

    clean_actor = _sanitize_single_line(actor or "owner", max_len=120) or "owner"
    clean_note = _sanitize_single_line(note or "", max_len=500)
    action_map = {
        "helpful": "atom_helpful",
        "correction": "atom_correction",
        "supersession": "atom_supersession",
    }
    action = action_map[resolved_kind]

    owning_brain = brain_dir or _brain_dir_for_telemetry_db(db_path)
    if owning_brain:
        _append_canonical_feedback(
            owning_brain,
            atom_id,
            feedback_kind=resolved_kind,
            helpful=(resolved_kind == "helpful"),
            actor=clean_actor,
            note=clean_note,
        )
        _append_durable_feedback(
            owning_brain,
            atom_id,
            action=action,
            actor=clean_actor,
            note=clean_note,
        )

    init_telemetry_db(db_path)
    if resolved_kind in {"helpful", "correction"}:
        col = "positive_feedback" if resolved_kind == "helpful" else "corrections"
        with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
            conn.execute(
                f"""
                INSERT INTO atom_telemetry (atom_id, {col})
                VALUES (?, 1)
                ON CONFLICT(atom_id) DO UPDATE SET
                    {col} = {col} + 1
                """,
                (atom_id,),
            )
            conn.commit()

    return {
        "atom_id": atom_id,
        "feedback_kind": resolved_kind,
        "action": action,
        "helpful": resolved_kind == "helpful",
        "actor": clean_actor,
        "note": clean_note,
    }


def _load_telemetry_map(db_path: str) -> dict[str, dict[str, object]]:
    if not os.path.exists(db_path):
        return {}
    init_telemetry_db(db_path)
    with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM atom_telemetry").fetchall()
        return {str(row["atom_id"]): dict(row) for row in rows}


def compute_memory_fitness_report(
    *,
    atoms_dir: str = ATOMS_DIR,
    brain_dir: str = BRAIN_DIR,
    db_path: str = TELEMETRY_DB_PATH,
) -> dict[str, object]:
    """Compute read-only memory utility, review bottlenecks, and procedural skill maturity."""
    telemetry = _load_telemetry_map(db_path)
    durable_feedback = load_durable_feedback_counts(brain_dir)
    corr_log_path = atom_correction_log_path(brain_dir) if brain_dir else ""
    corr_log_unsafe = bool(
        corr_log_path
        and os.path.lexists(corr_log_path)
        and (_is_reparse_or_link(corr_log_path) or _is_reparse_or_link(os.path.dirname(corr_log_path)))
    )
    high_utility: list[dict[str, object]] = []
    cold_verified: list[dict[str, object]] = []
    pending_bottlenecks: list[dict[str, object]] = []
    procedure_maturity: list[dict[str, object]] = []

    if os.path.isdir(atoms_dir):
        for fname in sorted(os.listdir(atoms_dir)):
            if not is_content_markdown(fname):
                continue
            fpath = safe_file_in_dir(atoms_dir, fname, allowed_exts={".md"}, must_exist=True)
            if not fpath or not os.path.isfile(fpath):
                continue
            with open(fpath, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
            parsed = split_frontmatter(content)
            if parsed is None:
                continue
            fm = parse_frontmatter_values(parsed[0])
            mem_type = str(fm.get("memory_type", "semantic"))
            review_status = str(fm.get("review_status", "pending"))
            is_approved = approved_revision_is_current(fpath, brain_dir=brain_dir, atoms_dir=atoms_dir)

            stats = telemetry.get(fname, {})
            d_stats = durable_feedback.get(fname, {})
            ret_hits = int(stats.get("retrieval_hits", 0) or 0)
            cand_hits = int(stats.get("candidate_hits", 0) or 0)
            citations = int(stats.get("grounded_citations", 0) or 0)
            pos_fb = max(
                int(stats.get("positive_feedback", 0) or 0),
                int(d_stats.get("positive_feedback", 0) or 0),
            )
            db_corrections = int(stats.get("corrections", 0) or 0)
            durable_corrections = 1 if corr_log_unsafe else int(d_stats.get("corrections", 0) or 0)
            corrections = max(db_corrections, durable_corrections)
            supersessions = int(d_stats.get("supersessions", 0) or 0)
            utility_score = round((citations * 2.0) + (pos_fb * 1.5) - (corrections * 3.0), 2)

            entry = {
                "atom_id": fname,
                "memory_type": mem_type,
                "review_status": review_status,
                "approved_current": is_approved,
                "retrieval_hits": ret_hits,
                "candidate_hits": cand_hits,
                "grounded_citations": citations,
                "positive_feedback": pos_fb,
                "corrections": corrections,
                "supersessions": supersessions,
                "utility_score": utility_score,
            }

            if is_approved:
                if citations > 0 or pos_fb > 0:
                    high_utility.append(entry)
                else:
                    cold_verified.append(entry)
            elif review_status == "pending":
                pending_bottlenecks.append(entry)

            if mem_type == "procedural":
                if not is_approved:
                    stage = "procedural_draft"
                elif citations >= 3 and corrections == 0:
                    stage = "skill_candidate"
                else:
                    stage = "verified_procedure"
                procedure_maturity.append({**entry, "maturity_stage": stage})

    high_utility.sort(key=lambda x: (-float(x["utility_score"]), str(x["atom_id"])))
    pending_bottlenecks.sort(key=lambda x: (-int(x["candidate_hits"]), str(x["atom_id"])))

    query_summary = {"total_recall_events": 0, "total_answer_events": 0, "answer_abstention_count": 0}
    if os.path.exists(db_path):
        with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
            row_r = conn.execute("SELECT COUNT(*) FROM query_telemetry WHERE event_type = 'recall'").fetchone()
            row_a = conn.execute("SELECT COUNT(*), COALESCE(SUM(abstained), 0) FROM query_telemetry WHERE event_type = 'answer'").fetchone()
            query_summary["total_recall_events"] = int(row_r[0]) if row_r else 0
            query_summary["total_answer_events"] = int(row_a[0]) if row_a else 0
            query_summary["answer_abstention_count"] = int(row_a[1]) if row_a else 0

    return {
        "schema_version": 1,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "query_summary": query_summary,
        "high_utility_verified_atoms": high_utility,
        "cold_verified_atoms": cold_verified,
        "pending_review_bottlenecks": pending_bottlenecks,
        "procedure_maturity": procedure_maturity,
    }


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:48] or "experience"


def _sanitize_single_line(value: str, *, max_len: int = 500) -> str:
    cleaned = "".join(ch for ch in str(value or "") if ch in ("\r", "\n", "\t") or ord(ch) >= 32)
    cleaned = re.sub(r"[\r\n\t]+", " ", cleaned).replace("-->", "—>").strip()
    if cleaned == "---":
        cleaned = "—--"
    return cleaned[:max_len]


def _sanitize_multiline_body(value: str, *, max_len: int = 4000) -> str:
    cleaned = "".join(ch for ch in str(value or "") if ch in ("\r", "\n", "\t") or ord(ch) >= 32)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n").replace("-->", "—>").strip()
    cleaned = re.sub(r"(?m)^---\s*$", "—--", cleaned)
    return cleaned[:max_len]


def _yaml_quote(value: str) -> str:
    cleaned = _sanitize_single_line(value, max_len=500)
    return json.dumps(cleaned, ensure_ascii=False)


def _validate_confidence_score(value: float | int | None, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite float between 0.0 and 1.0, not bool.")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite float between 0.0 and 1.0.") from exc
    if not math.isfinite(numeric) or numeric < 0.0 or numeric > 1.0:
        raise ValueError(f"{field_name} must be between 0.0 and 1.0; got {value!r}.")
    return round(numeric, 6)


def _validate_memory_basenames(items: list[str] | tuple[str, ...] | None, field_name: str) -> tuple[str, ...]:
    cleaned: list[str] = []
    for raw_item in items or ():
        raw = str(raw_item or "").strip()
        if not raw:
            continue
        if (
            "/" in raw
            or "\\" in raw
            or ":" in raw
            or "\x00" in raw
            or raw in {".", ".."}
            or os.path.isabs(raw)
        ):
            raise ValueError(f"Unsafe memory basename in {field_name}: {raw_item!r}")
        normalized = normalize_markdown_filename(raw)
        stem = os.path.splitext(normalized)[0].rstrip(" .").lower()
        first_stem = normalized.split(".", 1)[0].rstrip(" .").lower()
        if (
            not _valid_atom_id(normalized)
            or normalized.startswith(".")
            or normalized == "README.md"
            or stem in _WINDOWS_RESERVED_STEMS
            or first_stem in _WINDOWS_RESERVED_STEMS
            or any(ord(ch) < 32 for ch in normalized)
        ):
            raise ValueError(f"Invalid memory basename in {field_name}: {raw_item!r}")
        if normalized not in cleaned:
            cleaned.append(normalized)
    return tuple(cleaned)


def save_experience_record(
    *,
    goal: str,
    result_summary: str,
    success: bool,
    context: list[str] | None = None,
    plan: list[str] | None = None,
    actions: list[str] | None = None,
    tools_used: list[str] | None = None,
    referenced_atoms: list[str] | None = None,
    reasoning_summary: str | None = None,
    confidence_before: float | None = None,
    confidence_after: float | None = None,
    new_memories: list[str] | None = None,
    updated_memories: list[str] | None = None,
    candidate_skills: list[str] | None = None,
    reflection: str | None = None,
    lessons: list[str] | None = None,
    candidate_procedure_title: str | None = None,
    propose_to_inbox: bool = False,
    experiences_dir: str = EXPERIENCES_DIR,
    inbox_dir: str = INBOX_DIR,
    brain_dir: str = BRAIN_DIR,
) -> dict[str, object]:
    """Persist an immutable Experience Record (O_CREAT|O_EXCL) without mutating any canonical atom."""
    goal_clean = _sanitize_single_line(goal, max_len=500)
    summary_clean = _sanitize_multiline_body(result_summary, max_len=4000)
    if not goal_clean or not summary_clean:
        raise ValueError("Experience record requires non-empty goal and result_summary.")

    conf_before = _validate_confidence_score(confidence_before, "confidence_before")
    conf_after = _validate_confidence_score(confidence_after, "confidence_after")
    new_mem_tuple = _validate_memory_basenames(new_memories, "new_memories")
    upd_mem_tuple = _validate_memory_basenames(updated_memories, "updated_memories")

    now = datetime.datetime.now(datetime.timezone.utc)
    ts_iso = now.isoformat(timespec="seconds")
    exp_slug = f"{now.strftime('%Y%m%d-%H%M%S')}-{_slugify(goal_clean)}"

    ctx_tuple = tuple(s for s in (_sanitize_single_line(x) for x in (context or [])) if s)
    plan_tuple = tuple(s for s in (_sanitize_single_line(x) for x in (plan or [])) if s)
    act_tuple = tuple(s for s in (_sanitize_single_line(x) for x in (actions or [])) if s)
    tools_tuple = tuple(s for s in (_sanitize_single_line(x) for x in (tools_used or [])) if s)
    atoms_tuple = tuple(str(x).strip() for x in (referenced_atoms or []) if _valid_atom_id(str(x).strip()))
    reasoning_clean = _sanitize_multiline_body(reasoning_summary, max_len=4000) if reasoning_summary else ""
    reflection_clean = _sanitize_multiline_body(reflection, max_len=4000) if reflection else ""
    lessons_tuple = tuple(s for s in (_sanitize_single_line(x) for x in (lessons or [])) if s)
    cand_title = _sanitize_single_line(candidate_procedure_title, max_len=200) if candidate_procedure_title else None
    if cand_title == "":
        cand_title = None

    skills_list = list(
        dict.fromkeys(
            s for s in (_sanitize_single_line(x, max_len=200) for x in (candidate_skills or [])) if s
        )
    )
    if cand_title and cand_title not in skills_list:
        skills_list.append(cand_title)
    skills_tuple = tuple(skills_list)
    effective_proc_title = cand_title or (skills_tuple[0] if skills_tuple else None)

    from host_adapters import inspect_and_redact_outbound
    from inbox_workflow import scan_inbox_payload_risks

    all_text_parts = [
        goal_clean,
        summary_clean,
        reasoning_clean,
        reflection_clean,
        cand_title or "",
        *ctx_tuple,
        *plan_tuple,
        *act_tuple,
        *tools_tuple,
        *atoms_tuple,
        *new_mem_tuple,
        *upd_mem_tuple,
        *skills_tuple,
        *lessons_tuple,
    ]
    combined_payload = "\n".join(p for p in all_text_parts if p)
    risk_flags = list(scan_inbox_payload_risks(combined_payload))
    dlp_scan = inspect_and_redact_outbound(combined_payload, redact_pii=False, fail_on_secret_detection=False)
    for sec_hit in dlp_scan.secret_hits:
        flag = f"dlp_secret:{sec_hit}"
        if flag not in risk_flags:
            risk_flags.append(flag)
    if risk_flags:
        raise ValueError(f"Rejected experience record due to security/DLP risk flags: {', '.join(risk_flags)}.")

    lock_path = os.path.join(brain_dir, ".sidecar", "writer.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    os.makedirs(experiences_dir, exist_ok=True)

    with review_lock(lock_path):
        filename = f"{exp_slug}.md"
        suffix = 2
        while True:
            exp_path = safe_file_in_dir(experiences_dir, filename, allowed_exts={".md"}, must_exist=False)
            if not exp_path:
                raise ValueError("Invalid experience filename.")
            exp_id = os.path.splitext(filename)[0]
            record = ExperienceRecord(
                experience_id=exp_id,
                created_at=ts_iso,
                goal=goal_clean,
                context=ctx_tuple,
                actions=act_tuple,
                tools_used=tools_tuple,
                referenced_atoms=atoms_tuple,
                result_summary=summary_clean,
                success=bool(success),
                lessons=lessons_tuple,
                candidate_procedure_title=effective_proc_title,
                plan=plan_tuple,
                reasoning_summary=reasoning_clean,
                confidence_before=conf_before,
                confidence_after=conf_after,
                new_memories=new_mem_tuple,
                updated_memories=upd_mem_tuple,
                candidate_skills=skills_tuple,
                reflection=reflection_clean,
            )
            md_lines = [
                "---",
                f"experience_id: {_yaml_quote(record.experience_id)}",
                "type: experience",
                f"created_at: {_yaml_quote(record.created_at)}",
                f"goal: {_yaml_quote(record.goal)}",
                f"success: {'true' if record.success else 'false'}",
                f"context: {json.dumps(list(record.context), ensure_ascii=False)}",
                f"plan: {json.dumps(list(record.plan), ensure_ascii=False)}",
                f"actions: {json.dumps(list(record.actions), ensure_ascii=False)}",
                f"tools_used: {json.dumps(list(record.tools_used), ensure_ascii=False)}",
                f"referenced_atoms: {json.dumps(list(record.referenced_atoms), ensure_ascii=False)}",
                f"reasoning_summary: {_yaml_quote(record.reasoning_summary)}",
                f"result: {_yaml_quote(record.result_summary)}",
                f"confidence_before: {json.dumps(record.confidence_before)}",
                f"confidence_after: {json.dumps(record.confidence_after)}",
                f"new_memories: {json.dumps(list(record.new_memories), ensure_ascii=False)}",
                f"updated_memories: {json.dumps(list(record.updated_memories), ensure_ascii=False)}",
                f"candidate_skills: {json.dumps(list(record.candidate_skills), ensure_ascii=False)}",
                f"candidate_procedure: {_yaml_quote(record.candidate_procedure_title) if record.candidate_procedure_title else 'null'}",
                f"reflection: {_yaml_quote(record.reflection)}",
                f"lessons: {json.dumps(list(record.lessons), ensure_ascii=False)}",
                "---",
                "",
                "## Goal",
                record.goal,
                "",
                "## Plan",
                *(f"- {item}" for item in (record.plan or ("No explicit plan steps recorded.",))),
                "",
                "## Actions",
                *(f"- {item}" for item in (record.actions or ("No explicit action steps recorded.",))),
                "",
                "## Reasoning Summary",
                record.reasoning_summary or "No explicit reasoning summary recorded.",
                "",
                "## Result",
                record.result_summary,
                "",
                "## Reflection",
                record.reflection or "No explicit reflection recorded.",
                "",
                "## Lessons",
                *(f"- {item}" for item in (record.lessons or ("No explicit lessons recorded.",))),
                "",
            ]
            payload = "\n".join(md_lines).encode("utf-8")
            try:
                fd = os.open(exp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    if os.write(fd, payload) != len(payload):
                        raise OSError("Incomplete write to experience record.")
                    os.fsync(fd)
                finally:
                    os.close(fd)
                break
            except FileExistsError:
                filename = f"{exp_slug}-{suffix}.md"
                suffix += 1

        inbox_draft_filename = None
        if propose_to_inbox and effective_proc_title and record.success:
            os.makedirs(inbox_dir, exist_ok=True)
            draft_name = f"proc-candidate-{exp_id}.md"
            draft_suffix = 2
            while True:
                draft_path = safe_file_in_dir(inbox_dir, draft_name, allowed_exts={".md"}, must_exist=False)
                if not draft_path:
                    raise ValueError("Invalid inbox procedural draft filename.")
                draft_text = (
                    "<!-- second_brain_origin: cognitive_experience -->\n"
                    f"<!-- derived_from_experience: 4-experiences/{filename} -->\n\n"
                    f"# Candidate Procedure: {effective_proc_title}\n\n"
                    f"Derived from completed task goal: {record.goal}\n\n"
                    "Steps executed:\n"
                    + "\n".join(f"1. {step}" for step in (record.actions or (record.result_summary,)))
                    + "\n\nLessons observed:\n"
                    + "\n".join(f"- {lesson}" for lesson in (record.lessons or (record.result_summary,)))
                    + "\n"
                ).encode("utf-8")
                try:
                    dfd = os.open(draft_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    try:
                        if os.write(dfd, draft_text) != len(draft_text):
                            raise OSError("Incomplete write to inbox procedural draft.")
                        os.fsync(dfd)
                    finally:
                        os.close(dfd)
                    inbox_draft_filename = draft_name
                    break
                except FileExistsError:
                    draft_name = f"proc-candidate-{exp_id}-{draft_suffix}.md"
                    draft_suffix += 1

    return {
        "experience_id": record.experience_id,
        "filename": filename,
        "sha256": file_sha256(exp_path),
        "created_at": record.created_at,
        "goal": record.goal,
        "success": record.success,
        "context": list(record.context),
        "plan": list(record.plan),
        "actions": list(record.actions),
        "tools_used": list(record.tools_used),
        "referenced_atoms": list(record.referenced_atoms),
        "reasoning_summary": record.reasoning_summary,
        "result_summary": record.result_summary,
        "confidence_before": record.confidence_before,
        "confidence_after": record.confidence_after,
        "new_memories": list(record.new_memories),
        "updated_memories": list(record.updated_memories),
        "candidate_skills": list(record.candidate_skills),
        "candidate_procedure_title": record.candidate_procedure_title,
        "reflection": record.reflection,
        "lessons": list(record.lessons),
        "inbox_procedural_draft": inbox_draft_filename,
    }


def _cli_cognitive_dirs() -> tuple[str, str, str, str, str]:
    env_vault = (os.environ.get("BRAIN_VAULT_DIR") or "").strip()
    if env_vault and os.path.abspath(env_vault) != os.path.abspath(BRAIN_DIR):
        b = os.path.abspath(env_vault)
        return (
            b,
            os.path.join(b, "2-atoms"),
            os.path.join(b, "4-experiences"),
            os.path.join(b, "0-raw", "inbox"),
            os.path.join(b, ".sidecar", "telemetry.sqlite3"),
        )
    return BRAIN_DIR, ATOMS_DIR, EXPERIENCES_DIR, INBOX_DIR, TELEMETRY_DB_PATH


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Second Brain Cognitive Core reports and experience logger.")
    sub = parser.add_subparsers(dest="command", required=True)

    fit_p = sub.add_parser("fitness", help="Generate a Memory Fitness & Skill Candidate report.")
    fit_p.add_argument("--json", action="store_true")

    fb_p = sub.add_parser("feedback", help="Record helpful/correction feedback for an atom in sidecar telemetry.")
    fb_p.add_argument("atom_id")
    fb_p.add_argument("--helpful", action="store_true")
    fb_p.add_argument("--correction", action="store_true")

    exp_p = sub.add_parser("record-experience", help="Save a structured Experience Record.")
    exp_p.add_argument("--goal", required=True)
    exp_p.add_argument("--result", required=True)
    exp_p.add_argument("--success", action="store_true")
    exp_p.add_argument("--context", action="append", default=[])
    exp_p.add_argument("--plan", action="append", default=[])
    exp_p.add_argument("--action", action="append", default=[])
    exp_p.add_argument("--tool", action="append", default=[])
    exp_p.add_argument("--referenced-atom", action="append", default=[])
    exp_p.add_argument("--reasoning-summary", default=None)
    exp_p.add_argument("--confidence-before", type=float, default=None)
    exp_p.add_argument("--confidence-after", type=float, default=None)
    exp_p.add_argument("--new-memory", action="append", default=[])
    exp_p.add_argument("--updated-memory", action="append", default=[])
    exp_p.add_argument("--candidate-skill", action="append", default=[])
    exp_p.add_argument("--candidate-procedure", default=None)
    exp_p.add_argument("--reflection", default=None)
    exp_p.add_argument("--lesson", action="append", default=[])
    exp_p.add_argument("--propose-inbox", action="store_true")

    args = parser.parse_args(argv)
    eff_brain, eff_atoms, eff_exp, eff_inbox, eff_db = _cli_cognitive_dirs()

    if args.command == "fitness":
        report = compute_memory_fitness_report(atoms_dir=eff_atoms, brain_dir=eff_brain, db_path=eff_db)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            qs = report["query_summary"]
            print(f"Recall events: {qs['total_recall_events']} | Answer events: {qs['total_answer_events']} (abstained: {qs['answer_abstention_count']})")
            print(f"High-utility verified atoms: {len(report['high_utility_verified_atoms'])}")
            print(f"Cold verified atoms:         {len(report['cold_verified_atoms'])}")
            print(f"Pending review bottlenecks:  {len(report['pending_review_bottlenecks'])}")
            print(f"Procedural memories tracked: {len(report['procedure_maturity'])}")
        return 0

    if args.command == "feedback":
        if args.helpful == args.correction:
            print("Specify exactly one of --helpful or --correction.", file=sys.stderr)
            return 1
        record_atom_feedback(args.atom_id, helpful=args.helpful, db_path=eff_db, brain_dir=eff_brain)
        print(f"Recorded {'helpful' if args.helpful else 'correction'} feedback for {args.atom_id}.")
        return 0

    if args.command == "record-experience":
        saved = save_experience_record(
            goal=args.goal,
            result_summary=args.result,
            success=args.success,
            context=args.context,
            plan=args.plan,
            actions=args.action,
            tools_used=args.tool,
            referenced_atoms=args.referenced_atom,
            reasoning_summary=args.reasoning_summary,
            confidence_before=args.confidence_before,
            confidence_after=args.confidence_after,
            new_memories=args.new_memory,
            updated_memories=args.updated_memory,
            candidate_skills=args.candidate_skill,
            candidate_procedure_title=args.candidate_procedure,
            reflection=args.reflection,
            lessons=args.lesson,
            propose_to_inbox=args.propose_inbox,
            experiences_dir=eff_exp,
            inbox_dir=eff_inbox,
            brain_dir=eff_brain,
        )
        print(json.dumps(saved, indent=2, sort_keys=True))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
