# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Persistent multi-turn chat thread store and deterministic coreference query resolver.

Chat threads live strictly inside ``brain/.sidecar/chat_threads.sqlite3`` (disposable/sidecar
state, never polluting canonical Markdown truth in ``brain/2-atoms/`` or ``brain/5-playbooks/``),
unless the owner explicitly pins a turn to a working note via ``pin_turn_to_note()``.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import closing
import datetime
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
from typing import Any

if __name__ in ("chat_threads", "scripts.chat_threads"):
    sys.modules.setdefault("chat_threads", sys.modules[__name__])
    sys.modules.setdefault("scripts.chat_threads", sys.modules[__name__])

from scripts.config import (
    BRAIN_DIR,
    _is_reparse_or_link,
    resolve_module_vault_path,
    snapshot_module_defaults,
)
from scripts.host_adapters import inspect_and_redact_outbound, restrict_security_file_permissions
from scripts.lexical_store import FTS_STOP_WORDS, TOKEN_PATTERN

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())

MAX_THREAD_TITLE_LEN = 160
MAX_TURN_CONTENT_LEN = 24_000
MAX_HISTORY_TURNS_IN_PROMPT = 4
MAX_HISTORY_PROMPT_CHARS = 1400
MAX_CUSTOM_RULES_LEN = 4000
MAX_DONE_CRITERIA_LEN = 2000
THREAD_ID_RE = re.compile(r"^thr-[a-z0-9]{8,32}$")

WORK_MODES: tuple[str, ...] = ("chat", "build")
BEHAVIOR_MODES: tuple[str, ...] = ("autopilot", "interview")
PERSONA_PROFILES: dict[str, dict[str, str]] = {
    "grounded_default": {
        "id": "grounded_default",
        "label": "Grounded Default",
        "default_work_mode": "chat",
        "description": "Strict source-grounded recall and synthesis over verified atoms and owner notes.",
        "instruction": "Cite every factual sentence with [V#], [N#], or [P#] and keep answers concise.",
    },
    "coding": {
        "id": "coding",
        "label": "Coding",
        "default_work_mode": "build",
        "description": "Modular production engineering, clean diffs, edge-case guards, and unit tests.",
        "instruction": "Output modular, well-typed code in labeled fenced blocks with edge-case checks and unit tests.",
    },
    "website_builder": {
        "id": "website_builder",
        "label": "Website Builder",
        "default_work_mode": "build",
        "description": "Responsive editorial/product landing pages and UI sections renderable in live Build Canvas.",
        "instruction": "Provide self-contained responsive HTML, CSS, and vanilla JS in fenced ```html, ```css, and ```js blocks suitable for live browser preview.",
    },
    "app_builder": {
        "id": "app_builder",
        "label": "App Builder",
        "default_work_mode": "build",
        "description": "Interactive client-side productivity utilities and data visualizers with keyboard ergonomics.",
        "instruction": "Build an interactive offline client-side web app in fenced ```html/```css/```js blocks with accessible state management.",
    },
    "game_builder": {
        "id": "game_builder",
        "label": "Game Builder",
        "default_work_mode": "build",
        "description": "Self-contained HTML5 Canvas/SVG 60fps mini-games with crisp controls, HUD, and instant restart.",
        "instruction": "Build a self-contained playable HTML5 Canvas/SVG game in fenced ```html/```css/```js blocks with keyboard/mouse controls, score HUD, and reset.",
    },
}

_PRONOUN_FOLLOWUP_RE = re.compile(
    r"(?i)\b(?:it|its|itself|that|this|those|these|they|them|their|theirs|he|him|his|she|her|hers|same|former|latter)\b"
)
_ELLIPSIS_PREFIX_RE = re.compile(
    r"(?i)^\s*(?:and\b|also\b|what\s+about\b|how\s+about\b|why\b|when\b|where\b|how\s+does\b|can\s+it\b|does\s+it\b|is\s+it\b|what\s+else\b|tell\s+me\s+more\b|explain\s+more\b|elaborate\b)"
)
_COREF_STOP_WORDS = set(FTS_STOP_WORDS) | {
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "but", "by", "can", "could",
    "did", "do", "does", "for", "from", "had", "has", "have", "having", "how", "i", "in", "into",
    "is", "it", "its", "itself", "me", "more", "my", "no", "not", "of", "on", "or", "our", "owner",
    "please", "same", "should", "so", "some", "such", "tell", "than", "that", "the", "their",
    "them", "then", "there", "these", "they", "this", "those", "through", "to", "too", "under",
    "very", "was", "we", "were", "what", "when", "where", "which", "while", "who", "why", "will",
    "with", "would", "you", "your", "about", "also", "else", "explain", "elaborate", "verified",
    "claim", "claims", "source", "sources", "memory", "notes", "answer", "question",
}
_TECH_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{2,48}")


class ChatThreadError(ValueError):
    """Raised on invalid chat thread arguments or missing thread IDs."""


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def chat_threads_db_path(brain_dir: str | Path | None = None) -> str:
    b = resolve_module_vault_path(globals(), "BRAIN_DIR", str(brain_dir) if brain_dir is not None else None)
    if os.path.lexists(b) and _is_reparse_or_link(b):
        raise PermissionError(f"Refusing to open chat threads DB under symlinked vault: {b}")
    sidecar = os.path.join(b, ".sidecar")
    if os.path.lexists(sidecar) and _is_reparse_or_link(sidecar):
        raise PermissionError(f"Refusing to open chat threads DB under symlinked .sidecar: {sidecar}")
    os.makedirs(sidecar, exist_ok=True)
    db_file = os.path.join(sidecar, "chat_threads.sqlite3")
    if os.path.lexists(db_file) and _is_reparse_or_link(db_file):
        raise PermissionError(f"Refusing to open symlinked chat threads DB: {db_file}")
    return db_file


def _connect_db(brain_dir: str | Path | None = None) -> sqlite3.Connection:
    db_file = chat_threads_db_path(brain_dir)
    existed = os.path.exists(db_file)
    conn = sqlite3.connect(db_file, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chat_threads (
            thread_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            project_scope TEXT,
            host_id TEXT NOT NULL DEFAULT 'ollama_local',
            mode TEXT NOT NULL DEFAULT 'verified',
            work_mode TEXT NOT NULL DEFAULT 'chat',
            persona_id TEXT NOT NULL DEFAULT 'grounded_default',
            behavior_mode TEXT NOT NULL DEFAULT 'autopilot',
            custom_rules TEXT NOT NULL DEFAULT '',
            done_criteria TEXT NOT NULL DEFAULT '',
            turn_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS chat_turns (
            turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL REFERENCES chat_threads(thread_id) ON DELETE CASCADE,
            turn_index INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            resolved_query TEXT,
            answer_mode TEXT,
            host_id TEXT,
            cited_sources_json TEXT NOT NULL DEFAULT '[]',
            grounding_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chat_turns_thread_idx
            ON chat_turns(thread_id, turn_index);
        """
    )
    existing_cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(chat_threads)").fetchall()}
    migrations = (
        ("work_mode", "TEXT NOT NULL DEFAULT 'chat'"),
        ("persona_id", "TEXT NOT NULL DEFAULT 'grounded_default'"),
        ("behavior_mode", "TEXT NOT NULL DEFAULT 'autopilot'"),
        ("custom_rules", "TEXT NOT NULL DEFAULT ''"),
        ("done_criteria", "TEXT NOT NULL DEFAULT ''"),
    )
    for col_name, col_def in migrations:
        if col_name not in existing_cols:
            conn.execute(f"ALTER TABLE chat_threads ADD COLUMN {col_name} {col_def};")
    if not existed:
        try:
            restrict_security_file_permissions(Path(db_file))
        except Exception:
            pass
    return conn


def normalize_work_mode(raw: object) -> str:
    val = str(raw or "chat").strip().lower() or "chat"
    if val not in WORK_MODES:
        raise ChatThreadError(f"work_mode must be one of {WORK_MODES}")
    return val


def normalize_persona_id(raw: object) -> str:
    val = str(raw or "grounded_default").strip().lower() or "grounded_default"
    if val not in PERSONA_PROFILES:
        raise ChatThreadError(f"persona_id must be one of {tuple(PERSONA_PROFILES.keys())}")
    return val


def normalize_behavior_mode(raw: object) -> str:
    val = str(raw or "autopilot").strip().lower() or "autopilot"
    if val not in BEHAVIOR_MODES:
        raise ChatThreadError(f"behavior_mode must be one of {BEHAVIOR_MODES}")
    return val


def normalize_custom_rules(raw: object) -> str:
    if raw is None:
        return ""
    return _scrub_stored_text(str(raw).strip(), max_len=MAX_CUSTOM_RULES_LEN)[:MAX_CUSTOM_RULES_LEN]


def normalize_done_criteria(raw: object) -> str:
    if raw is None:
        return ""
    return _scrub_stored_text(str(raw).strip(), max_len=MAX_DONE_CRITERIA_LEN)[:MAX_DONE_CRITERIA_LEN]


def list_persona_profiles() -> list[dict[str, str]]:
    return [dict(v) for v in PERSONA_PROFILES.values()]


def build_workspace_guidance(
    *,
    work_mode: str = "chat",
    persona_id: str = "grounded_default",
    behavior_mode: str = "autopilot",
    custom_rules: str = "",
    done_criteria: str = "",
) -> str:
    """Format bounded Build/Persona/Interview guidance (never relaxes factual citation rules)."""
    wm = normalize_work_mode(work_mode)
    pid = normalize_persona_id(persona_id)
    bm = normalize_behavior_mode(behavior_mode)
    rules_clean = str(custom_rules or "").strip()[:MAX_CUSTOM_RULES_LEN]
    done_clean = str(done_criteria or "").strip()[:MAX_DONE_CRITERIA_LEN]
    if wm == "chat" and pid == "grounded_default" and bm == "autopilot" and not rules_clean and not done_clean:
        return ""
    persona = PERSONA_PROFILES[pid]
    parts = [
        f"Work mode: {wm} | Persona: {persona['label']} ({pid}) | Execution posture: {bm}.",
        f"Persona guidance: {persona['instruction']}",
    ]
    if wm == "build":
        parts.append(
            "Build mode formatting: emit complete runnable code in explicitly tagged fenced blocks "
            "(```html, ```css, ```js) so the sandboxed Build Canvas can live-preview it, while still citing "
            "vault facts [V#]/[N#] in any explanatory prose."
        )
    if bm == "interview":
        parts.append(
            "Interview posture: if requirements, scope, or trade-offs are ambiguous, ask 2-4 crisp targeted clarifying "
            "questions before generating full artifacts."
        )
    if rules_clean:
        parts.append(f"Owner project rules (must not override safety/citations): {rules_clean}")
    if done_clean:
        parts.append(f"Definition of done: {done_clean}")
    return "\n".join(parts)


def validate_thread_id(thread_id: str) -> str:
    cleaned = str(thread_id or "").strip().lower()
    if not THREAD_ID_RE.fullmatch(cleaned):
        raise ChatThreadError(f"Invalid chat thread_id: {thread_id!r}")
    return cleaned


def _scrub_stored_text(text: str, *, max_len: int = MAX_TURN_CONTENT_LEN) -> str:
    """Never persist high-risk API keys/tokens in chat_threads.sqlite3."""
    trimmed = str(text or "")[:max_len]
    report = inspect_and_redact_outbound(
        trimmed,
        redact_pii=False,
        fail_on_secret_detection=False,
    )
    return report.sanitized_text


def _derive_thread_title(raw_title: str | None, fallback_query: str = "") -> str:
    candidate = re.sub(r"\s+", " ", str(raw_title or "").strip())
    if not candidate:
        candidate = re.sub(r"\s+", " ", str(fallback_query or "").strip())
    if not candidate:
        candidate = "New Chat Thread"
    return _scrub_stored_text(candidate, max_len=MAX_THREAD_TITLE_LEN)[:MAX_THREAD_TITLE_LEN]


def create_chat_thread(
    *,
    title: str = "",
    project_scope: str | None = None,
    host_id: str = "ollama_local",
    mode: str = "verified",
    work_mode: str = "chat",
    persona_id: str = "grounded_default",
    behavior_mode: str = "autopilot",
    custom_rules: str = "",
    done_criteria: str = "",
    brain_dir: str | Path | None = None,
) -> dict[str, Any]:
    now = _now_iso()
    tid = f"thr-{secrets.token_hex(8)}"
    clean_title = _derive_thread_title(title)
    clean_proj = (project_scope or "").strip()[:120] or None
    clean_host = (host_id or "ollama_local").strip()[:64] or "ollama_local"
    clean_mode = (mode or "verified").strip()[:32] or "verified"
    clean_work_mode = normalize_work_mode(work_mode)
    clean_persona = normalize_persona_id(persona_id)
    clean_behavior = normalize_behavior_mode(behavior_mode)
    clean_rules = normalize_custom_rules(custom_rules)
    clean_done = normalize_done_criteria(done_criteria)
    with closing(_connect_db(brain_dir)) as conn, conn:
        conn.execute(
            """
            INSERT INTO chat_threads (
                thread_id, title, created_at, updated_at, project_scope, host_id, mode,
                work_mode, persona_id, behavior_mode, custom_rules, done_criteria, turn_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                tid,
                clean_title,
                now,
                now,
                clean_proj,
                clean_host,
                clean_mode,
                clean_work_mode,
                clean_persona,
                clean_behavior,
                clean_rules,
                clean_done,
            ),
        )
    return {
        "thread_id": tid,
        "title": clean_title,
        "created_at": now,
        "updated_at": now,
        "project_scope": clean_proj,
        "host_id": clean_host,
        "mode": clean_mode,
        "work_mode": clean_work_mode,
        "persona_id": clean_persona,
        "behavior_mode": clean_behavior,
        "custom_rules": clean_rules,
        "done_criteria": clean_done,
        "turn_count": 0,
    }


def list_chat_threads(
    *,
    limit: int = 50,
    offset: int = 0,
    project_scope: str | None = None,
    brain_dir: str | Path | None = None,
) -> dict[str, Any]:
    lim = max(1, min(int(limit), 200))
    off = max(0, int(offset))
    clean_proj = (project_scope or "").strip() or None
    with closing(_connect_db(brain_dir)) as conn:
        if clean_proj:
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM chat_threads WHERE project_scope = ?",
                    (clean_proj,),
                ).fetchone()[0]
            )
            rows = conn.execute(
                """
                SELECT thread_id, title, created_at, updated_at, project_scope, host_id, mode,
                       work_mode, persona_id, behavior_mode, custom_rules, done_criteria, turn_count
                FROM chat_threads
                WHERE project_scope = ?
                ORDER BY updated_at DESC, thread_id DESC
                LIMIT ? OFFSET ?
                """,
                (clean_proj, lim, off),
            ).fetchall()
        else:
            total = int(conn.execute("SELECT COUNT(*) FROM chat_threads").fetchone()[0])
            rows = conn.execute(
                """
                SELECT thread_id, title, created_at, updated_at, project_scope, host_id, mode,
                       work_mode, persona_id, behavior_mode, custom_rules, done_criteria, turn_count
                FROM chat_threads
                ORDER BY updated_at DESC, thread_id DESC
                LIMIT ? OFFSET ?
                """,
                (lim, off),
            ).fetchall()
    items = [dict(r) for r in rows]
    return {
        "total": total,
        "count": len(items),
        "offset": off,
        "limit": lim,
        "threads": items,
        "items": items,
    }


def get_chat_thread(
    thread_id: str,
    *,
    max_turns: int = 100,
    brain_dir: str | Path | None = None,
) -> dict[str, Any]:
    tid = validate_thread_id(thread_id)
    cap = max(1, min(int(max_turns), 500))
    with closing(_connect_db(brain_dir)) as conn:
        t_row = conn.execute(
            """
            SELECT thread_id, title, created_at, updated_at, project_scope, host_id, mode,
                   work_mode, persona_id, behavior_mode, custom_rules, done_criteria, turn_count
            FROM chat_threads WHERE thread_id = ?
            """,
            (tid,),
        ).fetchone()
        if t_row is None:
            raise FileNotFoundError(f"Chat thread not found: {tid}")
        turn_rows = conn.execute(
            """
            SELECT turn_id, turn_index, role, content, resolved_query, answer_mode, host_id,
                   cited_sources_json, grounding_json, created_at
            FROM (
                SELECT * FROM chat_turns
                WHERE thread_id = ?
                ORDER BY turn_index DESC
                LIMIT ?
            )
            ORDER BY turn_index ASC
            """,
            (tid, cap),
        ).fetchall()
    turns: list[dict[str, Any]] = []
    for r in turn_rows:
        try:
            cited = json.loads(r["cited_sources_json"] or "[]")
        except json.JSONDecodeError:
            cited = []
        try:
            grounding = json.loads(r["grounding_json"] or "{}")
        except json.JSONDecodeError:
            grounding = {}
        cited_list = cited if isinstance(cited, list) else []
        turns.append(
            {
                "turn_id": int(r["turn_id"]),
                "turn_index": int(r["turn_index"]),
                "role": str(r["role"]),
                "content": str(r["content"]),
                "resolved_query": r["resolved_query"],
                "answer_mode": r["answer_mode"],
                "host_id": r["host_id"],
                "cited_sources": cited_list,
                "citations": list(cited_list),
                "grounding": grounding if isinstance(grounding, dict) else {},
                "created_at": str(r["created_at"]),
            }
        )
    out = dict(t_row)
    out["turns"] = turns
    return out


def update_chat_thread_settings(
    thread_id: str,
    *,
    title: str | None = None,
    work_mode: str | None = None,
    persona_id: str | None = None,
    behavior_mode: str | None = None,
    custom_rules: str | None = None,
    done_criteria: str | None = None,
    brain_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Update persisted Build/Persona/Rules settings on an existing thread."""
    tid = validate_thread_id(thread_id)
    now = _now_iso()
    with closing(_connect_db(brain_dir)) as conn, conn:
        row = conn.execute(
            """
            SELECT thread_id, title, created_at, updated_at, project_scope, host_id, mode,
                   work_mode, persona_id, behavior_mode, custom_rules, done_criteria, turn_count
            FROM chat_threads WHERE thread_id = ?
            """,
            (tid,),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"Chat thread not found: {tid}")
        new_title = _derive_thread_title(title) if title is not None else str(row["title"])
        new_wm = normalize_work_mode(work_mode if work_mode is not None else row["work_mode"])
        new_pid = normalize_persona_id(persona_id if persona_id is not None else row["persona_id"])
        new_bm = normalize_behavior_mode(behavior_mode if behavior_mode is not None else row["behavior_mode"])
        new_rules = normalize_custom_rules(custom_rules if custom_rules is not None else row["custom_rules"])
        new_done = normalize_done_criteria(done_criteria if done_criteria is not None else row["done_criteria"])
        conn.execute(
            """
            UPDATE chat_threads
            SET title = ?, updated_at = ?, work_mode = ?, persona_id = ?,
                behavior_mode = ?, custom_rules = ?, done_criteria = ?
            WHERE thread_id = ?
            """,
            (new_title, now, new_wm, new_pid, new_bm, new_rules, new_done, tid),
        )
        updated = conn.execute(
            """
            SELECT thread_id, title, created_at, updated_at, project_scope, host_id, mode,
                   work_mode, persona_id, behavior_mode, custom_rules, done_criteria, turn_count
            FROM chat_threads WHERE thread_id = ?
            """,
            (tid,),
        ).fetchone()
    return dict(updated)


def delete_chat_thread(thread_id: str, *, brain_dir: str | Path | None = None) -> dict[str, Any]:
    tid = validate_thread_id(thread_id)
    with closing(_connect_db(brain_dir)) as conn, conn:
        cur = conn.execute("DELETE FROM chat_threads WHERE thread_id = ?", (tid,))
        if cur.rowcount == 0:
            raise FileNotFoundError(f"Chat thread not found: {tid}")
    return {"status": "deleted", "thread_id": tid}


def _scrub_json_obj(val: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return None
    if isinstance(val, str):
        return _scrub_stored_text(val, max_len=2000)
    if isinstance(val, (int, float, bool)) or val is None:
        return val
    if isinstance(val, (list, tuple)):
        return [_scrub_json_obj(item, depth=depth + 1) for item in list(val)[:24]]
    if isinstance(val, dict):
        out_d: dict[str, Any] = {}
        for k, v in list(val.items())[:32]:
            out_d[str(k)[:64]] = _scrub_json_obj(v, depth=depth + 1)
        return out_d
    return _scrub_stored_text(str(val), max_len=500)


def append_chat_turn(
    thread_id: str,
    role: str,
    content: str,
    *,
    resolved_query: str | None = None,
    answer_mode: str | None = None,
    host_id: str | None = None,
    cited_sources: Sequence[dict[str, Any]] | None = None,
    citations: Sequence[dict[str, Any]] | None = None,
    grounding: dict[str, Any] | None = None,
    brain_dir: str | Path | None = None,
) -> dict[str, Any]:
    tid = validate_thread_id(thread_id)
    clean_role = str(role or "").strip().lower()
    if clean_role not in {"user", "assistant"}:
        raise ChatThreadError(f"Invalid chat turn role: {role!r}")
    clean_content = _scrub_stored_text(content)
    clean_resolved = _scrub_stored_text(resolved_query, max_len=2500) if resolved_query else None
    effective_cited = cited_sources if cited_sources is not None else citations
    scrubbed_cited = _scrub_json_obj(list(effective_cited or [])[:24])
    scrubbed_grounding = _scrub_json_obj(dict(grounding or {}))
    cited_json = json.dumps(scrubbed_cited, ensure_ascii=False)
    if len(cited_json.encode("utf-8")) > 65_536:
        cited_json = "[]"
    grounding_json = json.dumps(scrubbed_grounding, ensure_ascii=False)
    if len(grounding_json.encode("utf-8")) > 65_536:
        grounding_json = "{}"
    now = _now_iso()

    with closing(_connect_db(brain_dir)) as conn, conn:
        row = conn.execute(
            "SELECT title, turn_count FROM chat_threads WHERE thread_id = ?",
            (tid,),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"Chat thread not found: {tid}")
        next_idx = int(row["turn_count"]) + 1
        cur_title = str(row["title"])
        new_title = (
            _derive_thread_title("", clean_content)
            if (clean_role == "user" and (cur_title == "New Chat Thread" or not cur_title.strip()))
            else cur_title
        )
        cur = conn.execute(
            """
            INSERT INTO chat_turns (
                thread_id, turn_index, role, content, resolved_query,
                answer_mode, host_id, cited_sources_json, grounding_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tid,
                next_idx,
                clean_role,
                clean_content,
                clean_resolved,
                (answer_mode or "")[:32] or None,
                (host_id or "")[:64] or None,
                cited_json,
                grounding_json,
                now,
            ),
        )
        conn.execute(
            """
            UPDATE chat_threads
            SET title = ?, updated_at = ?, turn_count = ?,
                host_id = COALESCE(?, host_id), mode = COALESCE(?, mode)
            WHERE thread_id = ?
            """,
            (
                new_title,
                now,
                next_idx,
                (host_id or "")[:64] or None,
                (answer_mode or "")[:32] or None,
                tid,
            ),
        )
        turn_id = int(cur.lastrowid or 0)
    return {
        "thread_id": tid,
        "turn_id": turn_id,
        "turn_index": next_idx,
        "role": clean_role,
        "title": new_title,
        "created_at": now,
    }


def pin_turn_to_note(
    thread_id: str,
    *,
    turn_id: int | None = None,
    title: str | None = None,
    tags: Sequence[str] = (),
    brain_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Save an assistant chat turn (plus its preceding user question & citations) as a Working Note."""
    from scripts.notes_store import NotePaths, create_note

    resolved_brain = resolve_module_vault_path(
        globals(), "BRAIN_DIR", str(brain_dir) if brain_dir is not None else None
    )
    thread = get_chat_thread(thread_id, max_turns=200, brain_dir=resolved_brain)
    turns = list(thread.get("turns") or [])
    if not turns:
        raise ChatThreadError("Cannot pin an empty chat thread.")

    chosen_idx = -1
    if turn_id is not None:
        for idx, t in enumerate(turns):
            if int(t["turn_id"]) == int(turn_id):
                chosen_idx = idx
                break
        if chosen_idx < 0:
            raise FileNotFoundError(f"Turn {turn_id} not found in thread {thread['thread_id']}.")
    else:
        for idx in range(len(turns) - 1, -1, -1):
            if turns[idx]["role"] == "assistant":
                chosen_idx = idx
                break
        if chosen_idx < 0:
            chosen_idx = len(turns) - 1

    target_turn = turns[chosen_idx]
    prev_user = None
    for idx in range(chosen_idx - 1, -1, -1):
        if turns[idx]["role"] == "user":
            prev_user = turns[idx]
            break

    note_title = (title or "").strip() or f"Pinned Chat: {thread['title']}"[:140]
    lines = [
        f"> Pinned from chat thread `{thread['thread_id']}` on {_now_iso()}",
        "",
    ]
    if prev_user:
        lines.extend(["## Question", prev_user["content"].strip(), ""])
    lines.extend(["## Answer", target_turn["content"].strip(), ""])
    cited = list(target_turn.get("cited_sources") or [])
    if cited:
        lines.append("## Cited Vault Sources")
        for src in cited:
            if isinstance(src, dict):
                cid = src.get("citation_id") or "?"
                fn = src.get("filename") or ""
                tier = src.get("tier") or "source"
                stem = fn[:-3] if str(fn).endswith(".md") else str(fn)
                link_str = f"[[{stem}]]" if stem else str(fn)
                lines.append(f"- `[{cid}]` ({tier}) {link_str}: {str(src.get('claim') or src.get('title') or '')[:200]}")
        lines.append("")

    merged_tags = list(dict.fromkeys([*(tags or ()), "chat-pinned", *(([thread["project_scope"]]) if thread.get("project_scope") else [])]))
    created = create_note(
        note_title,
        "\n".join(lines).strip() + "\n",
        tags=merged_tags,
        paths=NotePaths.for_brain(brain_dir),
    )
    return {
        "status": "pinned",
        "thread_id": thread["thread_id"],
        "turn_id": target_turn["turn_id"],
        "filename": created.get("filename"),
        "note_filename": created.get("filename"),
        "note": created,
    }


def _extract_salient_terms(text: str, *, limit: int = 8) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    # Prioritize technical/compound tokens first, then general content words
    candidates = _TECH_TERM_RE.findall(text or "") + TOKEN_PATTERN.findall(text or "")
    for raw_tok in candidates:
        cleaned = raw_tok.strip("._:/-")
        low = cleaned.casefold()
        if len(low) < 3 or low in _COREF_STOP_WORDS or low.isdigit():
            continue
        if re.fullmatch(r"[vpnl]\d+", low):
            continue
        if low not in seen:
            seen.add(low)
            out.append(low)
            if len(out) >= limit:
                break
    return out


def resolve_followup_query(
    query: str,
    history: Sequence[dict[str, Any]] | None,
) -> tuple[str, bool]:
    """Deterministically expand coreferential/elliptical follow-up queries using prior turns.

    Returns ``(resolved_query, was_expanded)``. When ``query`` is already self-contained or
    ``history`` is empty, returns ``(query, False)`` unchanged.
    """
    raw_q = str(query or "").strip()
    if not raw_q or not history:
        return raw_q, False

    q_content_terms = _extract_salient_terms(raw_q, limit=12)
    has_pronoun = bool(_PRONOUN_FOLLOWUP_RE.search(raw_q))
    has_ellipsis = bool(_ELLIPSIS_PREFIX_RE.search(raw_q))
    short_followup = len(q_content_terms) <= 2

    if not (has_pronoun or has_ellipsis or short_followup):
        return raw_q, False

    prior_user_texts: list[str] = []
    prior_assistant_texts: list[str] = []
    for item in reversed(list(history)[-6:]):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        txt = str(item.get("resolved_query") or item.get("content") or "").strip()
        if not txt:
            continue
        if role == "user" and len(prior_user_texts) < 2:
            prior_user_texts.append(txt)
        elif role == "assistant" and len(prior_assistant_texts) < 1:
            prior_assistant_texts.append(txt[:400])

    if not prior_user_texts and not prior_assistant_texts:
        return raw_q, False

    existing_lower = {t.casefold() for t in TOKEN_PATTERN.findall(raw_q)}
    anchors: list[str] = []
    for src_text in (*prior_user_texts, *prior_assistant_texts):
        for term in _extract_salient_terms(src_text, limit=6):
            base_parts = {p.casefold() for p in TOKEN_PATTERN.findall(term)} | {term.casefold()}
            if base_parts & existing_lower:
                continue
            if term not in anchors:
                anchors.append(term)
            if len(anchors) >= 6:
                break
        if len(anchors) >= 6:
            break

    if not anchors:
        return raw_q, False

    expanded = f"{raw_q} ({' '.join(anchors)})"
    return expanded[:2000], True


def format_history_for_prompt(
    history: Sequence[dict[str, Any]] | None,
    *,
    max_turns: int = MAX_HISTORY_TURNS_IN_PROMPT,
    max_chars: int = MAX_HISTORY_PROMPT_CHARS,
) -> str:
    """Render bounded, untrusted prior dialogue context for synthesis/selection prompts."""
    if not history:
        return ""
    selected: list[str] = []
    used = 0
    for item in reversed(list(history)[-(max_turns * 2) :]):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = re.sub(r"\s+", " ", str(item.get("content") or "").strip())
        if not content:
            continue
        snippet = content[:360]
        line = f"{'User' if role == 'user' else 'Assistant'}: {snippet}"
        if used + len(line) + 1 > max_chars:
            break
        selected.append(line)
        used += len(line) + 1
        if len(selected) >= max_turns:
            break
    if not selected:
        return ""
    selected.reverse()
    return "\n".join(selected)
