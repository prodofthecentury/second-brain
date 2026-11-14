# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""Product routes for the loopback Memory API: notes, streaming chat, one-click review, import.

Two credentials, two capability tiers:

* **Agent bearer token** (``Authorization: Bearer …``), held by IDE plugins and agents:
  read notes, recall, stream chat, and ``/v1/remember`` (risk-scanned agent capture).
* **Owner token** (``X-Owner-Token``), held only by the desktop app or CLI on this
  machine: create, edit, and delete notes, one-click verify, approve or reject atoms,
  triage the inbox, and import. An agent holding only the bearer token cannot grant
  trust, which preserves the rule that no model or agent can approve memory.
"""

from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
from urllib.parse import parse_qs, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import (  # noqa: E402
    ReviewConflictError,
    ReviewTransitionError,
    apply_review_transition,
    approved_revision_is_current,
    content_sha256,
)
from config import _is_reparse_or_link, is_content_markdown, safe_file_in_dir  # noqa: E402
from host_adapters import HostConsentError, read_bounded_security_file, restrict_security_file_permissions  # noqa: E402
from memory_review import list_review_queue, parse_atom_record  # noqa: E402
from notes_store import (  # noqa: E402
    NoteError,
    NotePaths,
    backlinks,
    create_note,
    delete_note,
    get_note,
    list_notes,
    update_note,
    verify_note_excerpt,
)

OWNER_HEADER = "X-Owner-Token"
MIN_OWNER_TOKEN_LEN = 32
MAX_SSE_SECONDS = 180


# ─── Owner credential ────────────────────────────────────────────────────────

def default_owner_token_path() -> Path:
    override = (os.environ.get("BRAIN_OWNER_TOKEN_FILE") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    elif os.environ.get("XDG_STATE_HOME"):
        base = Path(os.environ["XDG_STATE_HOME"]).expanduser()
    else:
        base = Path.home() / ".local" / "state"
    return base / "SecondBrain" / "security" / "owner-token"


def ensure_owner_token(path: Path | None = None) -> str:
    """Return the owner token, creating it (owner-only ACL / 0600) on first use."""
    target = (path or default_owner_token_path()).expanduser().resolve()
    existing = get_owner_token(target)
    if existing:
        return existing
    target.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse_or_link(str(target.parent)) or (os.path.lexists(target) and _is_reparse_or_link(str(target))):
        raise PermissionError("Refusing to write owner token through a symlink or junction.")
    token = secrets.token_urlsafe(32)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".owner-token-", suffix=".tmp")
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, target)
    restrict_security_file_permissions(target)
    return token


def get_owner_token(path: Path | None = None) -> str | None:
    target = path or default_owner_token_path()
    if not os.path.lexists(str(target)):
        return None
    try:
        value = read_bounded_security_file(Path(target), max_bytes=4096).strip()
    except OSError:
        return None
    return value if len(value) >= MIN_OWNER_TOKEN_LEN else None


def owner_authorized(header_value: str | None, token_path: Path | None = None) -> bool:
    expected = get_owner_token(token_path)
    supplied = (header_value or "").strip()
    return bool(expected and supplied) and hmac.compare_digest(supplied.encode(), expected.encode())


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _paths(rt) -> NotePaths:
    return NotePaths.for_brain(rt.brain_dir)


def _str(body: dict, key: str, *, max_len: int, required: bool = True, default: str = "") -> str:
    value = body.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ValueError(f"'{key}' must be a string.")
    if required and not value.strip():
        raise ValueError(f"'{key}' is required.")
    if len(value) > max_len:
        raise ValueError(f"'{key}' exceeds {max_len} characters.")
    return value


def _tags(body: dict) -> list[str]:
    tags = body.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 32 or any(not isinstance(t, str) or len(t) > 64 for t in tags):
        raise ValueError("'tags' must be a list of up to 32 short strings.")
    return tags


def _atom_card(path: str, rt) -> dict[str, object]:
    rec = parse_atom_record(path)
    raw = Path(path).read_bytes()
    from memory_format import split_frontmatter
    from memory_review import section_text

    parsed = split_frontmatter(raw.decode("utf-8"))
    evidence = section_text("\n".join(parsed[1]), "Evidence") if parsed else ""
    return {
        "filename": rec["filename"],
        "claim": rec["claim"],
        "evidence": evidence,
        "review_status": rec["review_status"],
        "status": rec["review_status"],
        "memory_type": rec["memory_type"],
        "origin": rec["origin"],
        "sources": rec["sources"],
        "sha256": content_sha256(raw),
        "verified": approved_revision_is_current(path, brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir),
    }


# ─── SSE streaming ───────────────────────────────────────────────────────────

def stream_sse(handler, events) -> None:
    """Write events as text/event-stream; connection closes at end (no chunked encoding needed)."""
    import time

    handler.close_connection = True
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Connection", "close")
    handler.end_headers()
    deadline = time.monotonic() + MAX_SSE_SECONDS
    try:
        for event in events:
            data = json.dumps(event, ensure_ascii=False)
            handler.wfile.write(f"event: {event.get('type', 'message')}\ndata: {data}\n\n".encode("utf-8"))
            handler.wfile.flush()
            if time.monotonic() > deadline:
                handler.wfile.write(b'event: error\ndata: {"type":"error","error":"stream time limit reached"}\n\n')
                break
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
        return


# ─── Dispatcher ──────────────────────────────────────────────────────────────

def dispatch(handler, method: str, path: str, query: str, body: dict | None) -> bool:
    """Handle product routes. Returns False if the path is not a product route.

    The caller has already enforced loopback, Host/Origin/Sec-Fetch-Site, and the bearer token.
    """
    rt = handler.server.runtime
    paths = _paths(rt)
    q = parse_qs(query or "")
    is_owner = owner_authorized(handler.headers.get(OWNER_HEADER), getattr(rt, "owner_token_path", None))
    _api_mod = sys.modules.get("memory_api") or sys.modules.get("scripts.memory_api") or sys.modules.get("__main__")
    _audit_fn = getattr(_api_mod, "_audit_api_event", None)
    if _audit_fn is None:
        from memory_api import _audit_api_event as _audit_fn

    def owner_only() -> bool:
        if not is_owner:
            _audit_fn(rt.brain_dir, path, method, 403)
            handler._send_json(403, {"error": "403 Forbidden: this action requires the owner token (desktop app/CLI)."})
            return False
        return True

    def send(status: int, payload: dict) -> bool:
        _audit_fn(rt.brain_dir, path, method, status)
        handler._send_json(status, payload)
        return True

    try:
        # ── Notes ──
        if path == "/v1/notes" and method == "GET":
            limit = max(1, min(int((q.get("limit") or ["100"])[0]), 500))
            offset = max(0, int((q.get("offset") or ["0"])[0]))
            items, total = list_notes(query=(q.get("query") or [""])[0][:500], limit=limit, offset=offset, paths=paths)
            items_payload = [i.as_dict() for i in items]
            return send(200, {"total": total, "items": items_payload, "notes": items_payload})

        if path == "/v1/notes" and method == "POST":
            if not owner_only():
                return True
            res = create_note(
                _str(body, "title", max_len=200, required=False),
                _str(body, "body", max_len=1_000_000, required=False),
                tags=_tags(body),
                paths=paths,
            )
            return send(201, res)

        if path.startswith("/v1/notes/"):
            rest = unquote(path[len("/v1/notes/"):])
            action = None
            if rest.endswith("/verify"):
                rest, action = rest[: -len("/verify")], "verify"
            elif rest.endswith("/backlinks"):
                rest, action = rest[: -len("/backlinks")], "backlinks"
            filename = rest
            if method == "GET" and action is None:
                return send(200, get_note(filename, paths=paths).as_dict())
            if method == "GET" and action == "backlinks":
                bl_payload = [b.as_dict() for b in backlinks(filename, paths=paths)]
                return send(200, {"items": bl_payload, "backlinks": bl_payload})
            if not owner_only():
                return True
            if method == "PUT" and action is None:
                res = update_note(
                    filename,
                    _str(body, "body", max_len=1_000_000, required=False),
                    expected_sha256=_str(body, "expected_sha256", max_len=64),
                    title=body.get("title") if isinstance(body.get("title"), str) else None,
                    tags=_tags(body) if "tags" in body else None,
                    paths=paths,
                )
                return send(200, res)
            if method == "DELETE" and action is None:
                return send(200, delete_note(filename, expected_sha256=_str(body, "expected_sha256", max_len=64), paths=paths))
            if method == "POST" and action == "verify":
                res = verify_note_excerpt(
                    filename,
                    _str(body, "excerpt", max_len=4000),
                    actor=_str(body, "actor", max_len=120),
                    expected_note_sha256=_str(body, "expected_sha256", max_len=64),
                    claim=body.get("claim") if isinstance(body.get("claim"), str) else None,
                    paths=paths,
                )
                return send(201, res)
            return send(405, {"error": "Method not allowed."})

        # ── Agent capture (Mem0-style remember) ──
        if path == "/v1/remember" and method == "POST":
            res = create_note(
                _str(body, "title", max_len=200, required=False),
                _str(body, "content", max_len=16_000),
                origin="agent_capture",
                tags=_tags(body),
                capture_source=_str(body, "source", max_len=64, required=False, default="agent"),
                paths=paths,
            )
            return send(202 if res["status"] == "quarantined" else 201, res)

        # ── Streaming chat ──
        if path == "/v1/chat/stream" and method == "POST":
            from streaming_chat import CHAT_MODES, ChatPaths, chat_events

            mode = _str(body, "mode", max_len=16, required=False, default="stream")
            if mode not in CHAT_MODES:
                raise ValueError(f"mode must be one of {CHAT_MODES}")
            cp = ChatPaths(
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
                lexical_index_path=rt.lexical_index_path,
                chroma_path=rt.chroma_path,
                policy_path=rt.policy_path,
            )
            scope = body.get("project_scope") if isinstance(body.get("project_scope"), str) else None
            _audit_fn(rt.brain_dir, path, method, 200)
            stream_sse(handler, chat_events(_str(body, "query", max_len=2000), mode=mode, paths=cp, project_scope=scope))
            return True

        if path == "/v1/chat/warmup" and method == "POST":
            from streaming_chat import warmup

            try:
                return send(200, warmup(rt.policy_path))
            except HostConsentError:
                raise
            except Exception as exc:
                return send(503, {"error": f"Local model warmup unavailable: {exc.__class__.__name__}"})

        # ── One-click review (owner) ──
        if path == "/v1/review/queue" and method == "GET":
            if not owner_only():
                return True
            items = []
            for rec in list_review_queue(rt.atoms_dir, {"pending", "needs_edit"}):
                p = safe_file_in_dir(rt.atoms_dir, rec["filename"], allowed_exts={".md"}, must_exist=True)
                if p:
                    items.append(_atom_card(p, rt))
            return send(200, {"items": items})

        if path == "/v1/atoms" and method == "GET":
            cards = []
            for name in sorted(os.listdir(rt.atoms_dir)) if os.path.isdir(rt.atoms_dir) else []:
                if not is_content_markdown(name):
                    continue
                p = safe_file_in_dir(rt.atoms_dir, name, allowed_exts={".md"}, must_exist=True)
                if p:
                    card = _atom_card(p, rt)
                    if card["verified"] or is_owner:
                        cards.append(card)
            return send(200, {"total": len(cards), "items": cards, "atoms": cards})

        if path.startswith("/v1/atoms/") and method == "POST":
            if not owner_only():
                return True
            rest = unquote(path[len("/v1/atoms/"):])
            filename, _, action = rest.rpartition("/")
            if action not in {"approve", "reject", "demote", "quarantine"}:
                return send(404, {"error": "Unknown atom action."})
            atom_path = safe_file_in_dir(rt.atoms_dir, filename, allowed_exts={".md"}, must_exist=True)
            if not atom_path:
                return send(404, {"error": "Atom not found."})
            record = apply_review_transition(
                action=action,
                atom_path=atom_path,
                expected_sha256=_str(body, "expected_sha256", max_len=64),
                actor=_str(body, "actor", max_len=120),
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
            )
            return send(200, {"status": action, "atom": _atom_card(atom_path, rt), "decision": record})

        # ── Inbox triage (owner) ──
        if path == "/v1/inbox/queue" and method == "GET":
            if not owner_only():
                return True
            from dataclasses import asdict
            from inbox_workflow import list_inbox_items

            items = [{**asdict(i), "risk_flags": list(i.risk_flags)} for i in list_inbox_items(rt.inbox_dir)]
            return send(200, {"total": len(items), "items": items})

        if path in {"/v1/inbox/promote", "/v1/inbox/reject"} and method == "POST":
            if not owner_only():
                return True
            from inbox_workflow import promote_inbox_item, reject_inbox_item

            fname = _str(body, "filename", max_len=200)
            sha = _str(body, "expected_sha256", max_len=64)
            actor = _str(body, "actor", max_len=120)
            if path.endswith("promote"):
                res = promote_inbox_item(fname, expected_sha256=sha, actor=actor,
                                         acknowledge_risks=bool(body.get("acknowledge_risks")),
                                         inbox_dir=rt.inbox_dir, raw_dir=os.path.dirname(rt.inbox_dir), brain_dir=rt.brain_dir)
            else:
                res = reject_inbox_item(fname, expected_sha256=sha, actor=actor, inbox_dir=rt.inbox_dir, brain_dir=rt.brain_dir)
            return send(200, res)

        # ── Bulk import (owner) ──
        if path == "/v1/import" and method == "POST":
            if not owner_only():
                return True
            from bulk_import import import_folder

            stats = import_folder(_str(body, "source_dir", max_len=1000), tags=tuple(_tags(body)), paths=paths)
            return send(200, stats.as_dict())

    except (ReviewConflictError,) as exc:
        return send(409, {"error": str(exc), "conflict": True})
    except (NoteError, ReviewTransitionError, ValueError, NotADirectoryError, FileNotFoundError) as exc:
        return send(422, {"error": str(exc)})
    except HostConsentError as exc:
        return send(403, {"error": str(exc)})
    except PermissionError as exc:
        return send(403, {"error": str(exc)})
    return False
