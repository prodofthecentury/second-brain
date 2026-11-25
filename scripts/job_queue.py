# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""SQLite-backed job queue for ingestion and distillation.

Jobs survive process restarts. ``second-brain jobs work`` runs the next job.
Capture can enqueue with ``--background`` so the CLI returns immediately.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from scripts.config import security_state_dir


def queue_path() -> Path:
    override = (os.environ.get("BRAIN_JOB_QUEUE") or "").strip()
    if override:
        return Path(override).expanduser()
    return security_state_dir() / "jobs.sqlite3"


def _connect() -> sqlite3.Connection:
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.commit()
    return conn


def enqueue(kind: str, payload: dict[str, Any]) -> str:
    if kind not in {"ingest", "distill", "backup", "capture"}:
        raise ValueError(f"Unsupported job kind: {kind}")
    job_id = uuid.uuid4().hex
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO jobs (id, kind, payload, status, created_at, updated_at) VALUES (?, ?, ?, 'queued', ?, ?)",
            (job_id, kind, json.dumps(payload, sort_keys=True), now, now),
        )
    return job_id


def job_status() -> dict[str, Any]:
    with _connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        recent = conn.execute(
            "SELECT id, kind, status, error, updated_at FROM jobs ORDER BY updated_at DESC LIMIT 8"
        ).fetchall()
    counts = {str(row["status"]): int(row["n"]) for row in rows}
    return {
        "queue": str(queue_path()),
        "counts": counts,
        "recent": [dict(row) for row in recent],
    }


def claim_next() -> dict[str, Any] | None:
    now = time.time()
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, kind, payload, attempts FROM jobs WHERE status IN ('queued', 'retry') ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE jobs SET status = 'running', attempts = ?, updated_at = ? WHERE id = ?",
            (int(row["attempts"]) + 1, now, row["id"]),
        )
    return {"id": row["id"], "kind": row["kind"], "payload": json.loads(row["payload"]), "attempts": int(row["attempts"]) + 1}


def complete_job(job_id: str, *, error: str = "") -> None:
    status = "failed" if error else "done"
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, error[:1000], time.time(), job_id),
        )


def requeue_running() -> int:
    """Mark jobs left in ``running`` as retry so a crashed worker can resume."""
    with _connect() as conn:
        cur = conn.execute("UPDATE jobs SET status = 'retry', updated_at = ? WHERE status = 'running'", (time.time(),))
        return int(cur.rowcount)


def work_once() -> dict[str, Any] | None:
    """Run one queued job. Returns None when the queue is empty."""
    requeue_running()
    job = claim_next()
    if job is None:
        return None
    try:
        result = _run_job(job["kind"], job["payload"])
    except Exception as exc:
        complete_job(job["id"], error=f"{exc.__class__.__name__}: {exc}")
        return {"id": job["id"], "kind": job["kind"], "status": "failed", "error": str(exc)}
    complete_job(job["id"])
    return {"id": job["id"], "kind": job["kind"], "status": "done", "result": result}


def _run_job(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    if kind == "capture":
        from scripts.notes_store import NotePaths, create_note

        brain = payload.get("brain_dir")
        created = create_note(
            str(payload.get("title") or ""),
            str(payload.get("body") or ""),
            tags=tuple(payload.get("tags") or ()),
            paths=NotePaths.for_brain(brain) if brain else None,
        )
        return {"filename": created.get("filename"), "status": created.get("status")}
    if kind == "ingest":
        from scripts.ingest_multimodal import process_file, scan_raw_dir

        if payload.get("scan"):
            return {"notes": scan_raw_dir()}
        path = str(payload.get("path") or "")
        if not path:
            raise ValueError("ingest job requires path")
        return {"processed": path, "note": process_file(path)}
    if kind == "distill":
        from scripts.cognitive_distiller import propose_principle

        return propose_principle(
            title=str(payload["title"]),
            statement=str(payload["statement"]),
            backing_atoms=list(payload["backing_atoms"]),
            brain_dir=payload.get("brain_dir"),
            atoms_dir=payload.get("atoms_dir"),
        )
    if kind == "backup":
        from scripts.vault_backup import create_vault_backup

        output = str(payload.get("output") or "")
        brain = payload.get("brain_dir")
        return create_vault_backup(
            output,
            brain_dir=brain,
            passphrase=payload.get("passphrase"),
            unencrypted=bool(payload.get("unencrypted")),
        )
    raise ValueError(f"Unsupported job kind: {kind}")


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json as _json
    import sys

    parser = argparse.ArgumentParser(prog="second-brain jobs", description="Background ingest, distill, and backup jobs.")
    sub = parser.add_subparsers(dest="subcmd", required=True)
    sub.add_parser("status", help="Show queued, running, and finished jobs.")
    work = sub.add_parser("work", help="Run queued jobs.")
    work.add_argument("--once", action="store_true", help="Run at most one job.")
    enq = sub.add_parser("enqueue", help="Add a job without running it.")
    enq.add_argument("kind", choices=("ingest", "distill", "backup", "capture"))
    enq.add_argument("--payload", default="{}", help="JSON object payload.")
    args = parser.parse_args(argv)
    if args.subcmd == "status":
        print(_json.dumps(job_status(), indent=2))
        return 0
    if args.subcmd == "enqueue":
        payload = _json.loads(args.payload)
        if not isinstance(payload, dict):
            raise SystemExit("payload must be a JSON object")
        print(enqueue(args.kind, payload))
        return 0
    if os.environ.get("BRAIN_AUTO_BACKUP", "1").strip().lower() not in {"0", "false", "off", "no"}:
        from scripts.vault_backup import auto_backup_now

        try:
            backup = auto_backup_now()
            print(_json.dumps({"auto_backup": backup}, default=str))
        except Exception as exc:
            print(f"auto backup skipped: {exc.__class__.__name__}: {exc}", file=sys.stderr)
    if args.once:
        outcome = work_once()
        print(_json.dumps(outcome, indent=2, default=str))
        return 0
    ran = 0
    while True:
        outcome = work_once()
        if outcome is None:
            break
        ran += 1
        print(_json.dumps(outcome, default=str))
    print(f"jobs finished: {ran}")
    return 0
