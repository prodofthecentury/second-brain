# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""Cognitive Core layer: disposable sidecar telemetry, memory fitness reports, and experience records."""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass
import datetime
import json
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
    file_sha256,
    is_content_markdown,
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
    context: tuple[str, ...]
    actions: tuple[str, ...]
    tools_used: tuple[str, ...]
    referenced_atoms: tuple[str, ...]
    result_summary: str
    success: bool
    lessons: tuple[str, ...]
    candidate_procedure_title: str | None = None


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


def record_atom_feedback(
    atom_id: str,
    *,
    helpful: bool,
    db_path: str = TELEMETRY_DB_PATH,
) -> None:
    if not _valid_atom_id(atom_id):
        raise ValueError(f"Invalid atom_id for telemetry feedback: {atom_id!r}")
    init_telemetry_db(db_path)
    col = "positive_feedback" if helpful else "corrections"
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
            ret_hits = int(stats.get("retrieval_hits", 0) or 0)
            cand_hits = int(stats.get("candidate_hits", 0) or 0)
            citations = int(stats.get("grounded_citations", 0) or 0)
            pos_fb = int(stats.get("positive_feedback", 0) or 0)
            corrections = int(stats.get("corrections", 0) or 0)
            utility_score = round((citations * 2.0) + (ret_hits * 0.5) + (pos_fb * 1.5) - (corrections * 3.0), 2)

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
                "utility_score": utility_score,
            }

            if is_approved:
                if citations > 0 or ret_hits > 0:
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


def save_experience_record(
    *,
    goal: str,
    result_summary: str,
    success: bool,
    context: list[str] | None = None,
    actions: list[str] | None = None,
    tools_used: list[str] | None = None,
    referenced_atoms: list[str] | None = None,
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

    now = datetime.datetime.now(datetime.timezone.utc)
    ts_iso = now.isoformat(timespec="seconds")
    exp_slug = f"{now.strftime('%Y%m%d-%H%M%S')}-{_slugify(goal_clean)}"

    ctx_tuple = tuple(s for s in (_sanitize_single_line(x) for x in (context or [])) if s)
    act_tuple = tuple(s for s in (_sanitize_single_line(x) for x in (actions or [])) if s)
    tools_tuple = tuple(s for s in (_sanitize_single_line(x) for x in (tools_used or [])) if s)
    atoms_tuple = tuple(str(x).strip() for x in (referenced_atoms or []) if _valid_atom_id(str(x).strip()))
    lessons_tuple = tuple(s for s in (_sanitize_single_line(x) for x in (lessons or [])) if s)
    cand_title = _sanitize_single_line(candidate_procedure_title, max_len=200) if candidate_procedure_title else None
    if cand_title == "":
        cand_title = None

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
            md_lines = [
                "---",
                f"experience_id: {_yaml_quote(exp_id)}",
                "type: experience",
                f"created_at: {_yaml_quote(ts_iso)}",
                f"goal: {_yaml_quote(goal_clean)}",
                f"success: {'true' if success else 'false'}",
                f"context: {json.dumps(list(ctx_tuple), ensure_ascii=False)}",
                f"tools_used: {json.dumps(list(tools_tuple), ensure_ascii=False)}",
                f"referenced_atoms: {json.dumps(list(atoms_tuple), ensure_ascii=False)}",
                f"candidate_procedure: {_yaml_quote(cand_title) if cand_title else 'null'}",
                "---",
                "",
                "## Goal",
                goal_clean,
                "",
                "## Actions",
                *(f"- {item}" for item in (act_tuple or ("No explicit action steps recorded.",))),
                "",
                "## Result",
                summary_clean,
                "",
                "## Lessons & Reflection",
                *(f"- {item}" for item in (lessons_tuple or ("No explicit lessons recorded.",))),
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
        if propose_to_inbox and cand_title and success:
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
                    f"# Candidate Procedure: {cand_title}\n\n"
                    f"Derived from completed task goal: {goal_clean}\n\n"
                    "Steps executed:\n"
                    + "\n".join(f"1. {step}" for step in (act_tuple or (summary_clean,)))
                    + "\n\nLessons observed:\n"
                    + "\n".join(f"- {lesson}" for lesson in (lessons_tuple or (summary_clean,)))
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
        "experience_id": exp_id,
        "filename": filename,
        "sha256": file_sha256(exp_path),
        "inbox_procedural_draft": inbox_draft_filename,
    }


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
    exp_p.add_argument("--action", action="append", default=[])
    exp_p.add_argument("--lesson", action="append", default=[])
    exp_p.add_argument("--candidate-procedure")
    exp_p.add_argument("--propose-inbox", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "fitness":
        report = compute_memory_fitness_report()
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
        record_atom_feedback(args.atom_id, helpful=args.helpful)
        print(f"Recorded {'helpful' if args.helpful else 'correction'} feedback for {args.atom_id}.")
        return 0

    if args.command == "record-experience":
        saved = save_experience_record(
            goal=args.goal,
            result_summary=args.result,
            success=args.success,
            actions=args.action,
            lessons=args.lesson,
            candidate_procedure_title=args.candidate_procedure,
            propose_to_inbox=args.propose_inbox,
        )
        print(json.dumps(saved, indent=2, sort_keys=True))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
