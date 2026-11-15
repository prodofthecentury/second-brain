# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""Product routes for the loopback Memory API: notes, streaming chat, one-click review, import, governance.

Two credentials, two capability tiers:

* **Agent bearer token** (``Authorization: Bearer …``), held by IDE plugins and agents:
  read notes, recall, stream chat, and ``/v1/remember`` (risk-scanned agent capture).
* **Owner token** (``X-Owner-Token``), held only by the desktop app or CLI on this
  machine: create, edit, and delete notes, one-click verify, approve/reject/edit/merge atoms,
  triage the inbox, manage playbooks/hosts/snapshots, and import. An agent holding only the
  bearer token cannot grant trust, which preserves the rule that no model or agent can approve memory.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
import hmac
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import (  # noqa: E402
    ReviewConflictError,
    ReviewTransitionError,
    apply_atom_edit,
    apply_atom_merge,
    apply_review_transition,
    approved_revision_is_current,
    content_sha256,
)
from config import (  # noqa: E402
    _is_reparse_or_link,
    is_content_markdown,
    normalize_markdown_filename,
    safe_file_in_dir,
)
from host_adapters import (  # noqa: E402
    HostConsentError,
    read_bounded_security_file,
    restrict_security_file_permissions,
)
from lexical_store import LexicalIndexUnavailable  # noqa: E402
from memory_format import (  # noqa: E402
    VALID_EPISTEMIC_LABELS,
    VALID_MEMORY_TYPES,
    render_memory_document,
    set_scalar_field,
    set_string_list_field,
    split_frontmatter,
)
from memory_review import (  # noqa: E402
    list_review_queue,
    parse_atom_record,
    parse_frontmatter_values,
    section_text,
)
from notes_store import (  # noqa: E402
    MAX_NOTE_BYTES,
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
from recall import MemoryRecallUnavailable  # noqa: E402
from skill_playbooks import PlaybookValidationError  # noqa: E402

OWNER_HEADER = "X-Owner-Token"
MIN_OWNER_TOKEN_LEN = 32
MAX_SSE_SECONDS = 180

OWNER_MAX_AUTH_FAILURES = 10
OWNER_LOCKOUT_SECONDS = 60.0
CAPTURE_MAX_WRITES_PER_MIN = 180
CAPTURE_WINDOW_SECONDS = 60.0

_OWNER_RATE_LOCK = threading.Lock()
_OWNER_AUTH_FAILURES: dict[str, list[float]] = {}
_CAPTURE_WRITE_TIMESTAMPS: dict[str, list[float]] = {}
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")


# ─── Owner credential & rate guards ──────────────────────────────────────────

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
    if path is None:
        env_tok = (os.environ.get("BRAIN_OWNER_TOKEN") or "").strip()
        if env_tok:
            return env_tok if len(env_tok) >= MIN_OWNER_TOKEN_LEN else None
    target = path or default_owner_token_path()
    if not os.path.lexists(str(target)):
        return None
    if _is_reparse_or_link(str(target)) or (
        os.path.lexists(str(Path(target).parent)) and _is_reparse_or_link(str(Path(target).parent))
    ):
        return None
    try:
        value = read_bounded_security_file(Path(target), max_bytes=4096).strip()
    except OSError:
        return None
    return value if len(value) >= MIN_OWNER_TOKEN_LEN else None


def reset_owner_rate_limiter() -> None:
    with _OWNER_RATE_LOCK:
        _OWNER_AUTH_FAILURES.clear()
        _CAPTURE_WRITE_TIMESTAMPS.clear()


def _record_owner_failure(client_key: str) -> bool:
    """Record a failed non-empty X-Owner-Token attempt; return True when >10 failures occur within 60s."""
    now = time.monotonic()
    cutoff = now - OWNER_LOCKOUT_SECONDS
    with _OWNER_RATE_LOCK:
        recent = [ts for ts in _OWNER_AUTH_FAILURES.get(client_key, []) if ts > cutoff]
        if len(recent) >= OWNER_MAX_AUTH_FAILURES:
            _OWNER_AUTH_FAILURES[client_key] = recent
            return True
        recent.append(now)
        _OWNER_AUTH_FAILURES[client_key] = recent
        return False


def _clear_owner_failures(client_key: str) -> None:
    with _OWNER_RATE_LOCK:
        _OWNER_AUTH_FAILURES.pop(client_key, None)


def check_capture_rate_limit(client_key: str) -> bool:
    """Allow up to CAPTURE_MAX_WRITES_PER_MIN writes per 60s window for agent-callable capture routes."""
    now = time.monotonic()
    cutoff = now - CAPTURE_WINDOW_SECONDS
    with _OWNER_RATE_LOCK:
        recent = [ts for ts in _CAPTURE_WRITE_TIMESTAMPS.get(client_key, []) if ts > cutoff]
        if len(recent) >= CAPTURE_MAX_WRITES_PER_MIN:
            _CAPTURE_WRITE_TIMESTAMPS[client_key] = recent
            return False
        recent.append(now)
        _CAPTURE_WRITE_TIMESTAMPS[client_key] = recent
        return True


def verify_owner_token(
    header_value: str | None,
    expected_token: str | None,
    *,
    client_ip: str | None = None,
    brain_dir: str | None = None,
    endpoint: str = "/v1/owner-auth",
    method: str = "AUTH",
    record_failure: bool = True,
    audit_fn: Callable[[str, str, str, int], None] | None = None,
) -> bool:
    """Constant-time X-Owner-Token verification with optional brute-force tracking & auditing."""
    supplied = (header_value or "").strip() if isinstance(header_value, str) else ""
    if not supplied:
        return False
    expected = (expected_token or "").strip() if isinstance(expected_token, str) else ""
    matched = (
        len(expected) >= MIN_OWNER_TOKEN_LEN
        and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    )
    if matched:
        if client_ip and record_failure:
            _clear_owner_failures(client_ip)
        return True
    if record_failure:
        throttled = _record_owner_failure(client_ip or "127.0.0.1")
        if brain_dir and audit_fn is not None:
            audit_fn(brain_dir, endpoint, method, 429 if throttled else 403)
    return False


def owner_authorized(header_value: str | None, token_path: Path | None = None) -> bool:
    return verify_owner_token(header_value, get_owner_token(token_path), record_failure=False)


def evaluate_request_owner_auth(
    handler,
    rt,
    *,
    path: str,
    method: str,
    audit_fn: Callable[[str, str, str, int], None],
) -> tuple[bool, bool, bool]:
    """Evaluate X-Owner-Token once per HTTP request.

    Returns (is_owner, throttled_429, had_failed_header).
    Caches per request headers object so memory_api.py and api_product.py never double-count.
    """
    cache_key = id(handler.headers)
    cached = getattr(handler, "_owner_auth_eval_cache", None)
    if isinstance(cached, tuple) and len(cached) == 4 and cached[0] == cache_key:
        return cached[1], cached[2], cached[3]

    raw_hdr = handler.headers.get(OWNER_HEADER)
    supplied = raw_hdr.strip() if isinstance(raw_hdr, str) else ""
    expected = getattr(rt, "owner_token", None)
    if expected is None:
        expected = get_owner_token(getattr(rt, "owner_token_path", None))
    client_ip = handler.client_address[0] if getattr(handler, "client_address", None) else "127.0.0.1"

    if not supplied:
        res = (False, False, False)
    elif verify_owner_token(supplied, expected, record_failure=False):
        _clear_owner_failures(client_ip)
        res = (True, False, False)
    else:
        throttled = _record_owner_failure(client_ip)
        res = (False, throttled, True)

    handler._owner_auth_eval_cache = (cache_key, res[0], res[1], res[2])
    if res[1]:
        audit_fn(rt.brain_dir, path, method, 429)
    return res


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _paths(rt) -> NotePaths:
    return NotePaths.for_brain(rt.brain_dir)


def _parse_bounded_int(
    q: dict[str, list[str]],
    key: str,
    *,
    default: int,
    min_val: int,
    max_val: int,
) -> int:
    vals = q.get(key)
    if not vals or not str(vals[0]).strip():
        return default
    raw = str(vals[0]).strip()[:9]
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid integer query parameter '{key}'.") from exc
    return max(min_val, min(parsed, max_val))


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


def _str_list(body: dict, key: str, *, max_items: int = 32, max_item_len: int = 1000, required: bool = False) -> list[str]:
    val = body.get(key, [])
    if val is None:
        val = []
    if not isinstance(val, list) or len(val) > max_items:
        raise ValueError(f"'{key}' must be a list of up to {max_items} strings.")
    out: list[str] = []
    for item in val:
        if not isinstance(item, str) or len(item) > max_item_len:
            raise ValueError(f"Each entry in '{key}' must be a string up to {max_item_len} characters.")
        if item.strip():
            out.append(item.strip())
    if required and not out:
        raise ValueError(f"'{key}' requires at least one non-empty item.")
    return out


def is_preference_atom_content(content: str) -> bool:
    parsed = split_frontmatter(content)
    if not parsed:
        return False
    fm_vals = parse_frontmatter_values(parsed[0])
    label = str(fm_vals.get("epistemic_label") or "").strip().lower()
    contract = str(fm_vals.get("epistemic_contract") or "").strip()
    return label == "preference" or bool(contract)


def _is_preference_atom_file(path: str) -> bool:
    try:
        raw_text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return is_preference_atom_content(raw_text)


def _atom_card(path: str, rt) -> dict[str, object]:
    rec = parse_atom_record(path)
    raw = Path(path).read_bytes()
    raw_text = raw.decode("utf-8")
    parsed = split_frontmatter(raw_text)
    fm_vals = parse_frontmatter_values(parsed[0]) if parsed else {}
    evidence = section_text("\n".join(parsed[1]), "Evidence") if parsed else ""
    epistemic_label = str(fm_vals.get("epistemic_label") or rec.get("epistemic_label") or "claim").strip() or "claim"
    epistemic_contract = str(fm_vals.get("epistemic_contract") or rec.get("epistemic_contract") or "").strip()
    return {
        "filename": rec["filename"],
        "claim": rec["claim"],
        "evidence": evidence,
        "review_status": rec["review_status"],
        "status": rec["review_status"],
        "memory_type": rec["memory_type"],
        "epistemic_label": "preference" if (epistemic_label == "preference" or epistemic_contract) else epistemic_label,
        "epistemic_contract": epistemic_contract or None,
        "contexts": list(rec.get("contexts") or []),
        "origin": rec["origin"],
        "sources": rec["sources"],
        "sha256": content_sha256(raw),
        "verified": approved_revision_is_current(path, brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir),
    }


def _safe_atom_card(path: str, rt, fallback: dict | None = None) -> dict[str, object] | None:
    try:
        return _atom_card(path, rt)
    except (OSError, UnicodeDecodeError, ValueError):
        if fallback is None:
            return None
        status = str(fallback.get("review_status") or "needs_edit")
        return {
            "filename": str(fallback.get("filename") or os.path.basename(path)),
            "claim": str(fallback.get("claim") or ""),
            "evidence": "",
            "review_status": status,
            "status": status,
            "memory_type": str(fallback.get("memory_type") or ""),
            "epistemic_label": str(fallback.get("epistemic_label") or "claim"),
            "epistemic_contract": fallback.get("epistemic_contract") or None,
            "contexts": list(fallback.get("contexts") or []),
            "origin": str(fallback.get("origin") or ""),
            "sources": list(fallback.get("sources") or []),
            "sha256": "",
            "verified": False,
        }


def _sync_lexical_after_atom_mutation(rt, filenames: list[str]) -> None:
    """Keep the FTS5 lexical sidecar's SHA-256 manifest synchronized after API atom mutations."""
    idx_path = getattr(rt, "lexical_index_path", None)
    if not idx_path or not os.path.isfile(idx_path):
        return
    try:
        from atom_mutator import review_lock
        from lexical_store import upsert_lexical_atoms

        lock_path = os.path.join(os.path.dirname(os.path.abspath(idx_path)), "writer.lock")
        with review_lock(lock_path):
            upsert_lexical_atoms(rt.atoms_dir, idx_path, [f for f in filenames if f])
    except Exception:
        pass


def _replace_markdown_section(body_lines: list[str], section_name: str, new_text: str, *, before_section: str | None = None) -> list[str]:
    clean_text = new_text.strip()
    if re.search(r"(?m)^##\s+", clean_text):
        raise ValueError(f"Section '{section_name}' text cannot inject secondary '##' headings.")
    header = f"## {section_name}"
    target_lower = header.lower()
    before_lower = f"## {before_section}".lower() if before_section else None

    sections: list[tuple[str | None, list[str]]] = []
    cur_header: str | None = None
    cur_lines: list[str] = []
    for line in body_lines:
        if line.strip().startswith("## "):
            sections.append((cur_header, cur_lines))
            cur_header = line.strip()
            cur_lines = []
        else:
            cur_lines.append(line)
    sections.append((cur_header, cur_lines))

    replaced = False
    rebuilt: list[str] = []
    replacement_lines = [clean_text, ""] if clean_text else [""]

    for hdr, lines in sections:
        if hdr is not None and hdr.lower() == target_lower:
            if not replaced:
                rebuilt.append(header)
                rebuilt.extend(replacement_lines)
                replaced = True
            continue
        if not replaced and before_lower and hdr is not None and hdr.lower() == before_lower:
            rebuilt.append(header)
            rebuilt.extend(replacement_lines)
            replaced = True
        if hdr is not None:
            rebuilt.append(hdr)
        rebuilt.extend(lines)

    if not replaced:
        if rebuilt and rebuilt[-1].strip():
            rebuilt.append("")
        rebuilt.append(header)
        rebuilt.extend(replacement_lines)
    return rebuilt


def _synthesize_edited_atom_markdown(atom_path: str, body: dict) -> str:
    raw_text = Path(atom_path).read_text(encoding="utf-8")
    parsed = split_frontmatter(raw_text)
    if parsed is None:
        raise ValueError("Target atom is missing valid YAML frontmatter.")
    fm, body_lines = list(parsed[0]), list(parsed[1])

    if body.get("memory_type") is not None:
        mtype = _str(body, "memory_type", max_len=32)
        if mtype not in VALID_MEMORY_TYPES:
            raise ValueError(f"Invalid memory_type {mtype!r}; expected one of {sorted(VALID_MEMORY_TYPES)}.")
        fm = set_scalar_field(fm, "memory_type", mtype, "type")

    if body.get("epistemic_label") is not None:
        elabel = _str(body, "epistemic_label", max_len=32)
        if elabel not in VALID_EPISTEMIC_LABELS:
            raise ValueError(f"Invalid epistemic_label {elabel!r}; expected one of {sorted(VALID_EPISTEMIC_LABELS)}.")
        fm = set_scalar_field(fm, "epistemic_label", elabel, "claim_state")

    if "contexts" in body and body.get("contexts") is not None:
        ctxs = _str_list(body, "contexts", max_items=32, max_item_len=120)
        fm = set_string_list_field(fm, "contexts", ctxs, "tags")

    if body.get("claim") is not None:
        claim_val = _str(body, "claim", max_len=8000)
        body_lines = _replace_markdown_section(body_lines, "Claim", claim_val, before_section="Evidence")

    if body.get("why_it_matters") is not None:
        why_val = _str(body, "why_it_matters", max_len=8000, required=False)
        if why_val.strip():
            body_lines = _replace_markdown_section(body_lines, "Why It Matters", why_val, before_section="Evidence")

    return render_memory_document(fm, body_lines)


def _parse_backing_atoms_payload(raw_val: object) -> list[tuple[str, str]]:
    if not isinstance(raw_val, list) or not raw_val or len(raw_val) > 32:
        raise ValueError("'backing_atoms' must be a non-empty list of [atom_filename, expected_sha256] pairs.")
    pairs: list[tuple[str, str]] = []
    for entry in raw_val:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            fn, sha = entry[0], entry[1]
        elif isinstance(entry, dict):
            fn = entry.get("filename") or entry.get("atom_id") or entry.get("atom")
            sha = entry.get("sha256") or entry.get("expected_sha256")
        elif isinstance(entry, str) and ":" in entry:
            fn, sha = entry.split(":", 1)
        else:
            raise ValueError("Invalid 'backing_atoms' entry; expected [filename, sha256] or {filename, sha256}.")
        if not isinstance(fn, str) or not isinstance(sha, str) or not fn.strip() or not sha.strip():
            raise ValueError("Each backing atom requires non-empty filename and sha256 strings.")
        pairs.append((normalize_markdown_filename(fn.strip()), sha.strip()))
    return pairs


def _verify_snapshot_fallback(snapshot_id: str, *, brain_dir: str) -> dict[str, object]:
    from time_travel import load_snapshot
    from vault_ledger import _build_merkle_levels, _leaf_digest

    snap = load_snapshot(snapshot_id, brain_dir=brain_dir)
    atoms = snap.get("atoms") if isinstance(snap.get("atoms"), dict) else {}
    playbooks = snap.get("playbooks") if isinstance(snap.get("playbooks"), dict) else {}
    leaves: list[tuple[str, str]] = []
    issues: list[str] = []

    for fname, rec in atoms.items():
        fhash = str((rec or {}).get("file_sha256") or "")
        if len(fhash) != 64:
            issues.append(f"Invalid atom hash in snapshot: {fname}")
            continue
        rel = f"2-atoms/{fname}"
        leaves.append((rel, _leaf_digest(rel, fhash)))

    for fname, rec in playbooks.items():
        fhash = str((rec or {}).get("file_sha256") or "")
        if len(fhash) != 64:
            issues.append(f"Invalid playbook hash in snapshot: {fname}")
            continue
        rel = f"5-playbooks/{fname}"
        leaves.append((rel, _leaf_digest(rel, fhash)))

    leaves.sort(key=lambda item: item[0])
    computed_root = _build_merkle_levels([h for _, h in leaves])[-1][0]
    stored_root = str(snap.get("merkle_root") or "")
    if computed_root != stored_root:
        issues.append("Snapshot Merkle root mismatch.")
    if int(snap.get("atom_count", len(atoms))) != len(atoms):
        issues.append("Snapshot atom_count metadata mismatch.")
    if int(snap.get("playbook_count", len(playbooks))) != len(playbooks):
        issues.append("Snapshot playbook_count metadata mismatch.")

    return {
        "snapshot_id": snap.get("snapshot_id", snapshot_id),
        "valid": len(issues) == 0,
        "stored_merkle_root": stored_root,
        "computed_merkle_root": computed_root,
        "merkle_root": stored_root,
        "atom_count": len(atoms),
        "playbook_count": len(playbooks),
        "issues": issues,
    }


# ─── SSE streaming ───────────────────────────────────────────────────────────

def stream_sse(handler, events, *, on_open: Callable[[], None] | None = None) -> None:
    """Write events as text/event-stream; connection closes at end (no chunked encoding needed)."""
    it = iter(events)
    sentinel = object()
    first = next(it, sentinel)
    if on_open is not None:
        on_open()

    handler.close_connection = True
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Connection", "close")
    handler.end_headers()
    deadline = time.monotonic() + MAX_SSE_SECONDS

    def _emit(event: dict) -> None:
        data = json.dumps(event, ensure_ascii=False)
        handler.wfile.write(f"event: {event.get('type', 'message')}\ndata: {data}\n\n".encode("utf-8"))
        handler.wfile.flush()

    try:
        if first is not sentinel:
            _emit(first)
        for event in it:
            _emit(event)
            if time.monotonic() > deadline:
                handler.wfile.write(b'event: error\ndata: {"type":"error","error":"stream time limit reached"}\n\n')
                handler.wfile.flush()
                break
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
        return
    except Exception as exc:
        try:
            _emit({"type": "error", "error": f"Stream aborted: {exc.__class__.__name__}"})
            _emit({"type": "done", "total_ms": 0.0, "elapsed_ms": 0.0})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
            return


# ─── Dispatcher ──────────────────────────────────────────────────────────────

def dispatch(handler, method: str, path: str, query: str, body: dict | None) -> bool:
    """Handle product and governance routes. Returns False if the path is not handled here.

    The caller has already enforced loopback, Host/Origin/Sec-Fetch-Site, and the bearer token.
    """
    rt = handler.server.runtime
    paths = _paths(rt)
    q = parse_qs(query or "")
    body = body if isinstance(body, dict) else {}

    _api_mod = sys.modules.get("memory_api") or sys.modules.get("scripts.memory_api") or sys.modules.get("__main__")
    _audit_fn = getattr(_api_mod, "_audit_api_event", None)
    if _audit_fn is None:
        from memory_api import _audit_api_event as _audit_fn

    is_owner, throttled_429, had_failed_owner_hdr = evaluate_request_owner_auth(
        handler, rt, path=path, method=method, audit_fn=_audit_fn
    )
    if throttled_429:
        handler._send_json(429, {"error": "Too Many Requests: owner token authentication rate limit exceeded."})
        return True

    def owner_only() -> bool:
        if not is_owner:
            _audit_fn(rt.brain_dir, path, method, 403)
            handler._send_json(403, {"error": "403 Forbidden: this action requires the owner token (desktop app/CLI)."})
            return False
        return True

    def send(status: int, payload: dict) -> bool:
        if had_failed_owner_hdr and status < 400:
            # Record failed X-Owner-Token probe in audit log even on mixed-tier read routes.
            _audit_fn(rt.brain_dir, path, method, 403)
        _audit_fn(rt.brain_dir, path, method, status)
        handler._send_json(status, payload)
        return True

    try:
        # ── Notes ──
        if path == "/v1/notes" and method == "GET":
            if (os.path.lexists(paths.brain_dir) and _is_reparse_or_link(paths.brain_dir)) or (
                os.path.lexists(paths.notes_dir) and _is_reparse_or_link(paths.notes_dir)
            ):
                raise PermissionError("Refusing to list notes through a symlink or reparse point.")
            limit = _parse_bounded_int(q, "limit", default=100, min_val=1, max_val=500)
            offset = _parse_bounded_int(q, "offset", default=0, min_val=0, max_val=1_000_000)
            note_query = (q.get("q") or q.get("query") or [""])[0][:500]
            note_tag = (q.get("tag") or [""])[0].strip().casefold()

            def _note_is_safe(item) -> bool:
                npath = os.path.join(paths.notes_dir, item.filename)
                return not (os.path.lexists(npath) and _is_reparse_or_link(npath))

            if note_tag:
                filtered = []
                scan_offset = 0
                while True:
                    batch, raw_total = list_notes(query=note_query, limit=1000, offset=scan_offset, paths=paths)
                    if not batch:
                        break
                    for item in batch:
                        if _note_is_safe(item) and any(str(t).casefold() == note_tag for t in (item.tags or ())):
                            filtered.append(item)
                    scan_offset += len(batch)
                    if scan_offset >= raw_total:
                        break
                total = len(filtered)
                items = filtered[offset : offset + limit]
            else:
                raw_items, total = list_notes(query=note_query, limit=limit, offset=offset, paths=paths)
                items = [item for item in raw_items if _note_is_safe(item)]
            page = [i.as_dict() for i in items]
            return send(
                200,
                {
                    "notes": page,
                    "items": page,
                    "count": len(page),
                    "total": total,
                    "offset": offset,
                    "limit": limit,
                },
            )

        if path == "/v1/notes" and method == "POST":
            if not owner_only():
                return True
            res = create_note(
                _str(body, "title", max_len=200, required=False),
                _str(body, "body", max_len=MAX_NOTE_BYTES, required=False),
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
                return send(200, {"items": bl_payload, "backlinks": bl_payload, "count": len(bl_payload)})
            if not owner_only():
                return True
            if method == "PUT" and action is None:
                res = update_note(
                    filename,
                    _str(body, "body", max_len=MAX_NOTE_BYTES, required=False),
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
                    memory_type=body.get("memory_type") if isinstance(body.get("memory_type"), str) else "semantic",
                    tags=_tags(body) if "tags" in body else None,
                    paths=paths,
                )
                return send(201, res)
            return send(405, {"error": "Method not allowed."})

        # ── Agent capture (Mem0-style remember) ──
        if path == "/v1/remember" and method == "POST":
            client_ip = handler.client_address[0] if getattr(handler, "client_address", None) else "127.0.0.1"
            if not check_capture_rate_limit(client_ip):
                return send(429, {"error": "Too Many Requests: capture write rate limit exceeded (max 180 per minute)."})
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
            query_str = _str(body, "query", max_len=2000)
            cp = ChatPaths(
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
                lexical_index_path=rt.lexical_index_path,
                chroma_path=rt.chroma_path,
                policy_path=rt.policy_path,
                telemetry_db_path=rt.telemetry_db_path,
            )
            scope = body.get("project_scope") if isinstance(body.get("project_scope"), str) else None
            model_ov = _str(body, "model_override", max_len=128, required=False) or None
            inc_prefs = bool(is_owner and body.get("include_preferences", True))
            stream_sse(
                handler,
                chat_events(
                    query_str,
                    mode=mode,
                    paths=cp,
                    project_scope=scope,
                    model_override=model_ov,
                    include_preferences=inc_prefs,
                ),
                on_open=lambda: _audit_fn(rt.brain_dir, path, method, 200),
            )
            return True

        if path == "/v1/chat/warmup" and method == "POST":
            from streaming_chat import warmup

            try:
                return send(200, warmup(rt.policy_path))
            except HostConsentError:
                raise
            except Exception as exc:
                return send(503, {"error": f"Local model warmup unavailable: {exc.__class__.__name__}"})

        # ── Cognitive feedback (owner) ──
        if path == "/v1/cognitive/feedback" and method == "POST":
            if not owner_only():
                return True
            from cognitive_core import record_atom_feedback

            raw_atom = body.get("atom_filename") if "atom_filename" in body else body.get("atom_id")
            if not isinstance(raw_atom, str) or not raw_atom.strip() or len(raw_atom) > 200:
                raise ValueError("'atom_filename' is required.")
            atom_fn = normalize_markdown_filename(raw_atom.strip())
            atom_path = safe_file_in_dir(rt.atoms_dir, atom_fn, allowed_exts={".md"}, must_exist=True)
            if not atom_path or _is_reparse_or_link(atom_path):
                return send(404, {"error": "Atom not found."})

            raw_fb = body.get("feedback") if "feedback" in body else body.get("signal")
            if isinstance(raw_fb, str) and raw_fb.strip():
                fb_norm = raw_fb.strip().lower()
                if fb_norm not in {"endorsed", "helpful", "positive", "corrected", "unhelpful", "negative"}:
                    raise ValueError("Invalid 'feedback'; expected 'endorsed' or 'corrected'.")
                helpful = fb_norm in {"endorsed", "helpful", "positive"}
            elif isinstance(body.get("helpful"), bool):
                helpful = bool(body.get("helpful"))
            else:
                raise ValueError("Provide 'feedback' ('endorsed' | 'corrected') or boolean 'helpful'.")
            record_atom_feedback(atom_fn, helpful=helpful, db_path=rt.telemetry_db_path, brain_dir=rt.brain_dir)
            return send(200, {"status": "recorded", "atom_filename": atom_fn, "helpful": helpful})

        # ── One-click review & Atom Governance ──
        if path == "/v1/review/queue" and method == "GET":
            if not owner_only():
                return True
            items = []
            for rec in list_review_queue(rt.atoms_dir, {"pending", "needs_edit"}):
                p = safe_file_in_dir(rt.atoms_dir, rec["filename"], allowed_exts={".md"}, must_exist=True)
                if p and not _is_reparse_or_link(p):
                    card = _safe_atom_card(p, rt, fallback=rec)
                    if card is not None:
                        items.append(card)
            return send(200, {"items": items, "count": len(items), "total": len(items)})

        if path == "/v1/atoms" and method == "GET":
            limit = _parse_bounded_int(q, "limit", default=200, min_val=1, max_val=500)
            offset = _parse_bounded_int(q, "offset", default=0, min_val=0, max_val=1_000_000)
            cards = []
            for name in sorted(os.listdir(rt.atoms_dir)) if (os.path.isdir(rt.atoms_dir) and not _is_reparse_or_link(rt.atoms_dir)) else []:
                if not is_content_markdown(name):
                    continue
                p = safe_file_in_dir(rt.atoms_dir, name, allowed_exts={".md"}, must_exist=True)
                if p and not _is_reparse_or_link(p):
                    card = _safe_atom_card(p, rt)
                    if card is None:
                        continue
                    if not is_owner:
                        if not card["verified"]:
                            continue
                        if card.get("epistemic_label") == "preference" or bool(card.get("epistemic_contract")):
                            continue
                    cards.append(card)
            page = cards[offset : offset + limit]
            return send(
                200,
                {
                    "total": len(cards),
                    "count": len(page),
                    "offset": offset,
                    "limit": limit,
                    "items": page,
                    "atoms": page,
                },
            )

        if path == "/v1/atoms/duplicates" and method == "GET":
            if not owner_only():
                return True
            from recall import suggest_duplicate_atoms

            raw_dups = suggest_duplicate_atoms(
                rt.atoms_dir,
                require_current_approval=True,
                brain_dir=rt.brain_dir,
            )
            dups = [
                {**d, "similarity": d.get("similarity", d.get("jaccard_similarity", 0.0))}
                for d in raw_dups
            ]
            return send(200, {"duplicates": dups, "items": dups, "count": len(dups)})

        if path == "/v1/atoms/merge" and method == "POST":
            if not owner_only():
                return True
            surv_raw = body.get("survivor_filename") if "survivor_filename" in body else body.get("survivor")
            sup_raw = body.get("superseded_filename") if "superseded_filename" in body else body.get("superseded")
            if not isinstance(surv_raw, str) or not surv_raw.strip():
                raise ValueError("'survivor_filename' is required.")
            if not isinstance(sup_raw, str) or not sup_raw.strip():
                raise ValueError("'superseded_filename' is required.")
            surv_name = normalize_markdown_filename(surv_raw.strip()[:200])
            sup_name = normalize_markdown_filename(sup_raw.strip()[:200])
            survivor_path = safe_file_in_dir(rt.atoms_dir, surv_name, allowed_exts={".md"}, must_exist=True)
            superseded_path = safe_file_in_dir(rt.atoms_dir, sup_name, allowed_exts={".md"}, must_exist=True)
            if not survivor_path or not superseded_path or _is_reparse_or_link(survivor_path) or _is_reparse_or_link(superseded_path):
                return send(404, {"error": "One or both atoms not found."})
            records = apply_atom_merge(
                survivor_path=survivor_path,
                survivor_expected_sha256=_str(body, "survivor_expected_sha256", max_len=64),
                superseded_path=superseded_path,
                superseded_expected_sha256=_str(body, "superseded_expected_sha256", max_len=64),
                actor=_str(body, "actor", max_len=120),
                rationale=_str(body, "rationale", max_len=1000),
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
            )
            _sync_lexical_after_atom_mutation(rt, [surv_name, sup_name])
            return send(
                200,
                {
                    "status": "merged",
                    "survivor": _atom_card(survivor_path, rt),
                    "superseded": _atom_card(superseded_path, rt),
                    "decisions": records,
                },
            )

        if path.startswith("/v1/atoms/") and method == "GET":
            raw_fname = unquote(path[len("/v1/atoms/"):]).strip()
            if not raw_fname or "/" in raw_fname or "\\" in raw_fname:
                return send(404, {"error": "Atom not found."})
            norm_fname = normalize_markdown_filename(raw_fname)
            atom_path = safe_file_in_dir(rt.atoms_dir, norm_fname, allowed_exts={".md"}, must_exist=True)
            if not atom_path or _is_reparse_or_link(atom_path):
                return send(404, {"error": "Atom not found."})
            raw_bytes = Path(atom_path).read_bytes()
            raw_text = raw_bytes.decode("utf-8")
            rec = parse_atom_record(atom_path)
            parsed = split_frontmatter(raw_text)
            metadata = parse_frontmatter_values(parsed[0]) if parsed else {}
            body_text = "\n".join(parsed[1]).strip() if parsed else raw_text
            sha = content_sha256(raw_bytes)
            approved_cur = approved_revision_is_current(atom_path, brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir)
            is_pref = is_preference_atom_content(raw_text)

            if not is_owner:
                if (
                    not approved_cur
                    or rec.get("review_status") != "accepted"
                    or bool(rec.get("superseded_by"))
                    or is_pref
                ):
                    return send(404, {"error": "Atom not found."})
                card = _atom_card(atom_path, rt)
                return send(
                    200,
                    {
                        **card,
                        "body": body_text,
                        "content": raw_text,
                        "sha256": sha,
                        "approved_current": True,
                    },
                )

            from review_details import atom_review_details

            details = atom_review_details(
                raw_text,
                atom_path=atom_path,
                atoms_dir=rt.atoms_dir,
                brain_dir=rt.brain_dir,
            )
            card = _atom_card(atom_path, rt)
            return send(
                200,
                {
                    **card,
                    "metadata": metadata,
                    "claim": rec["claim"],
                    "body": body_text,
                    "content": raw_text,
                    "sha256": sha,
                    "approved_current": approved_cur,
                    "verified": approved_cur,
                    "review_details": details,
                },
            )

        if path.startswith("/v1/atoms/") and method == "PUT":
            if not owner_only():
                return True
            raw_fname = unquote(path[len("/v1/atoms/"):]).strip()
            if not raw_fname or "/" in raw_fname or "\\" in raw_fname:
                return send(404, {"error": "Atom not found."})
            norm_fname = normalize_markdown_filename(raw_fname)
            atom_path = safe_file_in_dir(rt.atoms_dir, norm_fname, allowed_exts={".md"}, must_exist=True)
            if not atom_path or _is_reparse_or_link(atom_path):
                return send(404, {"error": "Atom not found."})
            expected_sha = _str(body, "expected_sha256", max_len=64)
            actor = _str(body, "actor", max_len=120)

            edit_params = inspect.signature(apply_atom_edit).parameters
            has_raw_md = isinstance(body.get("edited_content"), str) or isinstance(body.get("content"), str)
            if has_raw_md:
                md_key = "edited_content" if isinstance(body.get("edited_content"), str) else "content"
                edited_md = _str(body, md_key, max_len=MAX_NOTE_BYTES)
                record = apply_atom_edit(
                    atom_path=atom_path,
                    expected_sha256=expected_sha,
                    actor=actor,
                    edited_content=edited_md,
                    brain_dir=rt.brain_dir,
                    atoms_dir=rt.atoms_dir,
                )
            elif "claim" in edit_params:
                record = apply_atom_edit(
                    atom_path=atom_path,
                    expected_sha256=expected_sha,
                    actor=actor,
                    claim=_str(body, "claim", max_len=8000, required=False) or None,
                    why_it_matters=_str(body, "why_it_matters", max_len=8000, required=False) or None,
                    contexts=_str_list(body, "contexts", max_items=32, max_item_len=120) if "contexts" in body else None,
                    memory_type=_str(body, "memory_type", max_len=32, required=False) or None,
                    epistemic_label=_str(body, "epistemic_label", max_len=32, required=False) or None,
                    brain_dir=rt.brain_dir,
                    atoms_dir=rt.atoms_dir,
                )
            else:
                if not any(k in body for k in ("claim", "why_it_matters", "contexts", "memory_type", "epistemic_label")):
                    raise ValueError("Atom edit requires 'edited_content'/'content' or at least one structured field ('claim', 'why_it_matters', 'contexts', 'memory_type', 'epistemic_label').")
                edited_md = _synthesize_edited_atom_markdown(atom_path, body)
                record = apply_atom_edit(
                    atom_path=atom_path,
                    expected_sha256=expected_sha,
                    actor=actor,
                    edited_content=edited_md,
                    brain_dir=rt.brain_dir,
                    atoms_dir=rt.atoms_dir,
                )
            _sync_lexical_after_atom_mutation(rt, [norm_fname])
            return send(200, {"status": "edited", "atom": _atom_card(atom_path, rt), "decision": record})

        if path.startswith("/v1/atoms/") and method == "POST":
            if not owner_only():
                return True
            rest = unquote(path[len("/v1/atoms/"):])
            filename, _, action = rest.rpartition("/")
            if action not in {"approve", "reject", "demote", "quarantine"}:
                return send(404, {"error": "Unknown atom action."})
            norm_fname = normalize_markdown_filename(filename)
            atom_path = safe_file_in_dir(rt.atoms_dir, norm_fname, allowed_exts={".md"}, must_exist=True)
            if not atom_path or _is_reparse_or_link(atom_path):
                return send(404, {"error": "Atom not found."})
            record = apply_review_transition(
                action=action,
                atom_path=atom_path,
                expected_sha256=_str(body, "expected_sha256", max_len=64),
                actor=_str(body, "actor", max_len=120),
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
            )
            _sync_lexical_after_atom_mutation(rt, [norm_fname])
            return send(200, {"status": action, "atom": _atom_card(atom_path, rt), "decision": record})

        # ── Inbox triage (owner) ──
        if path == "/v1/inbox/queue" and method == "GET":
            if not owner_only():
                return True
            from inbox_workflow import list_inbox_items

            items = [
                {**asdict(i), "risk_flags": list(i.risk_flags)}
                for i in list_inbox_items(
                    rt.inbox_dir,
                    raw_dir=os.path.dirname(rt.inbox_dir),
                    brain_dir=rt.brain_dir,
                )
            ]
            return send(200, {"total": len(items), "count": len(items), "items": items})

        if path in {"/v1/inbox/promote", "/v1/inbox/reject"} and method == "POST":
            if not owner_only():
                return True
            from inbox_workflow import promote_inbox_item, reject_inbox_item

            fname = _str(body, "filename", max_len=200)
            sha = _str(body, "expected_sha256", max_len=64)
            actor = _str(body, "actor", max_len=120)
            if path.endswith("promote"):
                res = promote_inbox_item(
                    fname,
                    expected_sha256=sha,
                    actor=actor,
                    acknowledge_risks=bool(body.get("acknowledge_risks")),
                    inbox_dir=rt.inbox_dir,
                    raw_dir=os.path.dirname(rt.inbox_dir),
                    brain_dir=rt.brain_dir,
                )
                if isinstance(res.get("atom_filename"), str):
                    _sync_lexical_after_atom_mutation(rt, [res["atom_filename"]])
            else:
                res = reject_inbox_item(
                    fname,
                    expected_sha256=sha,
                    actor=actor,
                    inbox_dir=rt.inbox_dir,
                    raw_dir=os.path.dirname(rt.inbox_dir),
                    brain_dir=rt.brain_dir,
                )
            return send(200, res)

        if path.startswith("/v1/inbox/") and method == "GET":
            if not owner_only():
                return True
            fname = unquote(path[len("/v1/inbox/"):]).strip()
            if not fname or "/" in fname or "\\" in fname:
                return send(404, {"error": "Inbox item not found."})
            from inbox_workflow import inspect_inbox_item

            item_doc = inspect_inbox_item(
                fname,
                inbox_dir=rt.inbox_dir,
                raw_dir=os.path.dirname(rt.inbox_dir),
                brain_dir=rt.brain_dir,
            )
            preview = str(item_doc.get("content") or "")[:400]
            return send(200, {**item_doc, "preview": preview})

        # ── Playbooks governance (owner) ──
        if path == "/v1/playbooks/candidates" and method == "GET":
            if not owner_only():
                return True
            from skill_playbooks import inspect_skill_candidates

            raw_cands = inspect_skill_candidates(
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
                playbooks_dir=rt.playbooks_dir,
                telemetry_db_path=rt.telemetry_db_path,
            )
            cands = []
            for cand in raw_cands:
                claim_txt = str(cand.get("claim") or "")
                aid = str(cand.get("atom_id") or "")
                if not claim_txt and aid:
                    ap = safe_file_in_dir(rt.atoms_dir, aid, allowed_exts={".md"}, must_exist=True)
                    if ap and not _is_reparse_or_link(ap):
                        try:
                            claim_txt = str(parse_atom_record(ap).get("claim") or "")
                        except Exception:
                            claim_txt = ""
                cands.append({**cand, "claim": claim_txt})
            return send(200, {"candidates": cands, "items": cands, "count": len(cands)})

        if path == "/v1/playbooks/promote" and method == "POST":
            if not owner_only():
                return True
            from skill_playbooks import promote_skill_playbook

            exp_pb_sha = body.get("expected_playbook_sha256")
            if exp_pb_sha is not None:
                exp_pb_sha = _str(body, "expected_playbook_sha256", max_len=64, required=False) or None
            res = promote_skill_playbook(
                playbook_id=_str(body, "playbook_id", max_len=120),
                title=_str(body, "title", max_len=240),
                backing_atoms=_parse_backing_atoms_payload(body.get("backing_atoms")),
                trigger=_str(body, "trigger", max_len=1000),
                preconditions=_str_list(body, "preconditions", max_items=30, max_item_len=1000),
                steps=_str_list(body, "steps", max_items=40, max_item_len=1000, required=True),
                expected_outputs=_str_list(body, "expected_outputs", max_items=30, max_item_len=1000),
                failure_recovery=_str_list(body, "failure_recovery", max_items=30, max_item_len=1000),
                actor=_str(body, "actor", max_len=120),
                rationale=_str(body, "rationale", max_len=1000, required=False),
                allow_early_owner_override=bool(body.get("allow_early_owner_override")),
                acknowledge_corrections=bool(body.get("acknowledge_corrections")),
                expected_playbook_sha256=exp_pb_sha,
                allow_multi_project=bool(body.get("allow_multi_project")),
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
                playbooks_dir=rt.playbooks_dir,
                telemetry_db_path=rt.telemetry_db_path,
            )
            return send(200, res)

        if path.startswith("/v1/playbooks/") and method == "POST":
            if not owner_only():
                return True
            rest = unquote(path[len("/v1/playbooks/"):])
            pb_filename, _, pb_action = rest.rpartition("/")
            if pb_action != "deprecate" or not pb_filename:
                return send(404, {"error": "Unknown playbook action."})
            from skill_playbooks import deprecate_skill_playbook

            res = deprecate_skill_playbook(
                normalize_markdown_filename(pb_filename),
                expected_sha256=_str(body, "expected_sha256", max_len=64),
                actor=_str(body, "actor", max_len=120),
                rationale=_str(body, "rationale", max_len=1000),
                brain_dir=rt.brain_dir,
                playbooks_dir=rt.playbooks_dir,
            )
            return send(200, res)

        # ── Host sharing policy governance (owner) ──
        if path == "/v1/hosts" and method == "GET":
            if not owner_only():
                return True
            import host_adapters

            load_fn = getattr(host_adapters, "load_host_sharing_policy", None) or host_adapters.load_host_policies
            policies = load_fn(rt.policy_path)
            serialized = {
                hid: {**asdict(rule), "allowed_projects": list(rule.allowed_projects)}
                for hid, rule in sorted(policies.items())
            }
            return send(200, serialized)

        if path.startswith("/v1/hosts/") and method == "PUT":
            if not owner_only():
                return True
            host_id = unquote(path[len("/v1/hosts/"):]).strip()
            if not host_id or "/" in host_id or "\\" in host_id or len(host_id) > 64:
                return send(404, {"error": "Invalid host_id."})
            import host_adapters
            from recall import normalize_project_tag

            set_fn = getattr(host_adapters, "set_host_sharing_profile", None)
            if callable(set_fn):
                sig = inspect.signature(set_fn)
                kwargs: dict[str, object] = {}
                for k in (
                    "enabled",
                    "endpoint_url",
                    "default_model",
                    "allowed_projects",
                    "allow_unscoped_atoms",
                    "allow_preference_atoms",
                    "redact_pii",
                    "fail_on_secret_detection",
                    "api_key_env",
                ):
                    if k in body and k in sig.parameters:
                        kwargs[k] = body[k]
                if "policy_path" in sig.parameters:
                    kwargs["policy_path"] = rt.policy_path
                updated_rule = set_fn(host_id, **kwargs)
                rule_dict = (
                    {**asdict(updated_rule), "allowed_projects": list(updated_rule.allowed_projects)}
                    if hasattr(updated_rule, "__dataclass_fields__")
                    else dict(updated_rule)
                )
                return send(200, rule_dict)

            policies = host_adapters.load_host_policies(rt.policy_path)
            if host_id not in policies:
                return send(404, {"error": f"Unknown host_id '{host_id}'."})
            cur = policies[host_id]
            enabled = cur.enabled if "enabled" not in body else bool(body.get("enabled"))
            if "allowed_projects" in body and body.get("allowed_projects") is not None:
                raw_projs = _str_list(body, "allowed_projects", max_items=32, max_item_len=120)
                projects = tuple("*" if p == "*" else normalize_project_tag(p) for p in raw_projs if p)
            else:
                projects = cur.allowed_projects

            raw_model = (
                _str(body, "default_model", max_len=128)
                if body.get("default_model") is not None
                else cur.default_model
            )
            new_model = host_adapters.validate_model_identifier(raw_model, provider=cur.provider)
            new_ep = (
                _str(body, "endpoint_url", max_len=500).rstrip("/")
                if body.get("endpoint_url") is not None
                else cur.endpoint_url
            )
            new_key_env = cur.api_key_env
            if "api_key_env" in body and body.get("api_key_env") is not None:
                cand_env = _str(body, "api_key_env", max_len=64, required=False).strip()
                if cand_env and not _ENV_VAR_NAME_RE.fullmatch(cand_env):
                    raise ValueError("api_key_env must be an uppercase environment variable identifier (never a raw key).")
                new_key_env = cand_env or None

            updated = host_adapters.HostSharingRule(
                host_id=cur.host_id,
                provider=cur.provider,
                transport=cur.transport,
                endpoint_url=new_ep,
                default_model=new_model,
                api_key_env=new_key_env,
                enabled=enabled,
                allowed_projects=projects,
                allow_unscoped_atoms=(
                    cur.allow_unscoped_atoms
                    if "allow_unscoped_atoms" not in body
                    else bool(body.get("allow_unscoped_atoms"))
                ),
                allow_preference_atoms=(
                    cur.allow_preference_atoms
                    if "allow_preference_atoms" not in body
                    else bool(body.get("allow_preference_atoms"))
                ),
                redact_pii=True if cur.transport == "online" else (
                    cur.redact_pii if "redact_pii" not in body else bool(body.get("redact_pii"))
                ),
                fail_on_secret_detection=True if cur.transport == "online" else (
                    cur.fail_on_secret_detection
                    if "fail_on_secret_detection" not in body
                    else bool(body.get("fail_on_secret_detection"))
                ),
            )
            if updated.enabled or body.get("endpoint_url") is not None:
                host_adapters.validate_endpoint_network_boundary(updated)
            policies[host_id] = updated
            host_adapters.save_host_policies(policies, rt.policy_path)
            return send(200, {**asdict(updated), "allowed_projects": list(updated.allowed_projects)})

        # ── Bulk import (owner) ──
        if path == "/v1/import" and method == "POST":
            if not owner_only():
                return True
            from bulk_import import import_folder

            stats = import_folder(
                _str(body, "source_dir", max_len=1000),
                tags=tuple(_tags(body)),
                dry_run=bool(body.get("dry_run")),
                overwrite_modified=bool(body.get("overwrite_modified")),
                paths=paths,
            )
            return send(200, stats.as_dict())

        # ── Cryptographic Vault Merkle Ledger ──
        if path == "/v1/ledger/verify" and method == "GET":
            from vault_ledger import verify_vault_ledger

            return send(200, verify_vault_ledger(brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir, playbooks_dir=rt.playbooks_dir))

        if path == "/v1/ledger/commit" and method == "POST":
            if not owner_only():
                return True
            from vault_ledger import commit_ledger_checkpoint

            entry = commit_ledger_checkpoint(
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
                playbooks_dir=rt.playbooks_dir,
                actor=_str(body, "actor", max_len=120, required=False) or "owner",
                note=_str(body, "note", max_len=300, required=False),
            )
            return send(201, entry)

        if path == "/v1/ledger/verify-proof" and method == "POST":
            from vault_ledger import verify_merkle_proof

            proof_obj = body.get("proof") if isinstance(body.get("proof"), dict) else body
            if not isinstance(proof_obj, dict):
                raise ValueError("'proof' must be a JSON object.")
            return send(200, {"valid": bool(verify_merkle_proof(proof_obj))})

        if path.startswith("/v1/ledger/prove/") and method == "GET":
            from vault_ledger import prove_atom_membership

            atom_name = unquote(path[len("/v1/ledger/prove/"):])
            try:
                proof = prove_atom_membership(
                    atom_name, brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir, playbooks_dir=rt.playbooks_dir
                )
            except (FileNotFoundError, PermissionError):
                if not is_owner:
                    return send(404, {"error": "Verified atom proof not found."})
                raise
            return send(200, proof)

        # ── Contradiction & Drift Radar + Knowledge Graph ──
        if path == "/v1/graph/contradictions" and method == "GET":
            from knowledge_graph import detect_contradictions

            proj = ((q.get("project") or [None])[0] or None)
            if proj and len(proj) > 120:
                raise ValueError("Invalid project query filter.")
            inc_pending = bool(is_owner and ((q.get("include_pending") or ["1"])[0] != "0"))
            dc_kwargs: dict[str, object] = {
                "brain_dir": rt.brain_dir,
                "atoms_dir": rt.atoms_dir,
                "project_scope": proj,
                "include_pending": inc_pending,
            }
            if "include_preferences" in inspect.signature(detect_contradictions).parameters:
                dc_kwargs["include_preferences"] = bool(is_owner)
            radar = detect_contradictions(**dc_kwargs)
            if not is_owner:
                pref_files = {
                    name
                    for name in (os.listdir(rt.atoms_dir) if os.path.isdir(rt.atoms_dir) else [])
                    if is_content_markdown(name) and _is_preference_atom_file(os.path.join(rt.atoms_dir, name))
                }
                if pref_files:
                    alerts = [
                        al
                        for al in (radar.get("alerts") or [])
                        if al.get("atom_a", {}).get("filename") not in pref_files
                        and al.get("atom_b", {}).get("filename") not in pref_files
                    ]
                    orphans = [fn for fn in (radar.get("orphan_approved_atoms") or []) if fn not in pref_files]
                    radar = {
                        **radar,
                        "alerts": alerts,
                        "contradiction_count": len(alerts),
                        "high_severity_count": sum(1 for al in alerts if al.get("severity") == "high"),
                        "orphan_approved_atoms": orphans,
                        "orphan_approved_count": len(orphans),
                    }
            return send(200, radar)

        if path == "/v1/graph/summary" and method == "GET":
            from knowledge_graph import build_knowledge_graph

            proj = ((q.get("project") or [None])[0] or None)
            if proj and len(proj) > 120:
                raise ValueError("Invalid project query filter.")
            bg_kwargs: dict[str, object] = {
                "brain_dir": rt.brain_dir,
                "atoms_dir": rt.atoms_dir,
                "project_scope": proj,
                "include_pending": False,
            }
            if "include_preferences" in inspect.signature(build_knowledge_graph).parameters:
                bg_kwargs["include_preferences"] = bool(is_owner)
            res = build_knowledge_graph(**bg_kwargs)
            return send(200, {k: v for k, v in res.items() if k not in {"nodes", "edges"}})

        if path.startswith("/v1/graph/neighbors/") and method == "GET":
            from knowledge_graph import graph_neighbors

            raw_target = unquote(path[len("/v1/graph/neighbors/"):]).strip()
            if not raw_target or "/" in raw_target or "\\" in raw_target or len(raw_target) > 200:
                return send(404, {"error": "Atom not found in graph."})
            q_inc_pending = (q.get("include_pending") or ["0"])[0] in {"1", "true", "yes"}
            gn_kwargs: dict[str, object] = {
                "brain_dir": rt.brain_dir,
                "atoms_dir": rt.atoms_dir,
                "include_pending": bool(is_owner and q_inc_pending),
            }
            if "include_preferences" in inspect.signature(graph_neighbors).parameters:
                gn_kwargs["include_preferences"] = bool(is_owner)
            res = graph_neighbors(raw_target, **gn_kwargs)
            if not is_owner:
                target_fname = str(res.get("atom") or normalize_markdown_filename(raw_target))
                target_path = safe_file_in_dir(rt.atoms_dir, target_fname, allowed_exts={".md"}, must_exist=True)
                if not target_path or _is_preference_atom_file(target_path):
                    return send(404, {"error": "Atom not found in graph."})
                pref_files = {
                    name
                    for name in (os.listdir(rt.atoms_dir) if os.path.isdir(rt.atoms_dir) else [])
                    if is_content_markdown(name) and _is_preference_atom_file(os.path.join(rt.atoms_dir, name))
                }
                if pref_files:
                    edges = [
                        e
                        for e in (res.get("edges") or [])
                        if e.get("from") not in pref_files and e.get("to") not in pref_files
                    ]
                    contras = [
                        al
                        for al in (res.get("contradictions") or [])
                        if al.get("atom_a", {}).get("filename") not in pref_files
                        and al.get("atom_b", {}).get("filename") not in pref_files
                    ]
                    res = {
                        **res,
                        "edge_count": len(edges),
                        "edges": edges,
                        "contradictions": contras,
                    }
            return send(200, res)

        # ── Source Anchor Audit (owner) ──
        if path == "/v1/audit/sources" and method == "GET":
            if not owner_only():
                return True
            from audit_sources import audit_sources

            reports = audit_sources(atoms_dir=rt.atoms_dir, brain_dir=rt.brain_dir, demote_stale=False)
            stale = [r for r in reports if r.get("stale_approval")]
            return send(
                200,
                {
                    "count": len(reports),
                    "stale_count": len(stale),
                    "reports": reports,
                    "items": reports,
                },
            )

        if path == "/v1/audit/sources/demote-stale" and method == "POST":
            if not owner_only():
                return True
            from audit_sources import audit_sources

            actor = _str(body, "actor", max_len=120, required=False) or "owner"
            reports = audit_sources(
                atoms_dir=rt.atoms_dir,
                brain_dir=rt.brain_dir,
                demote_stale=True,
                actor=actor,
            )
            demoted = [r for r in reports if r.get("demoted")]
            return send(
                200,
                {
                    "count": len(reports),
                    "demoted_count": len(demoted),
                    "demoted": demoted,
                    "reports": reports,
                },
            )

        # ── Time-Travel Snapshots, Diff, Recall & Verify ──
        if path == "/v1/snapshots" and method == "GET":
            from time_travel import list_snapshots

            snaps = list_snapshots(brain_dir=rt.brain_dir)
            return send(200, {"total": len(snaps), "count": len(snaps), "snapshots": snaps, "items": snaps})

        if path == "/v1/snapshots" and method == "POST":
            if not owner_only():
                return True
            from time_travel import create_snapshot

            doc = create_snapshot(
                label=_str(body, "label", max_len=160, required=False),
                actor=_str(body, "actor", max_len=120, required=False) or "owner",
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
                playbooks_dir=rt.playbooks_dir,
            )
            return send(201, doc)

        if path == "/v1/snapshots/diff" and method == "POST":
            from time_travel import diff_snapshots

            res = diff_snapshots(
                _str(body, "from_snapshot", max_len=120),
                _str(body, "to_snapshot", max_len=120, required=False) or "live",
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
                playbooks_dir=rt.playbooks_dir,
            )
            if not is_owner:
                def _live_atom_approved(fname: object) -> bool:
                    p = safe_file_in_dir(rt.atoms_dir, str(fname or ""), allowed_exts={".md"}, must_exist=True)
                    return bool(p and approved_revision_is_current(p, brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir))

                added = [
                    a
                    for a in (res.get("added_atoms") or [])
                    if a.get("epistemic_label") != "preference" and _live_atom_approved(a.get("filename"))
                ]
                removed = [
                    (
                        a
                        if _live_atom_approved(a.get("filename"))
                        else {**a, "claim": "", "redacted_revoked": True}
                    )
                    for a in (res.get("removed_atoms") or [])
                    if a.get("epistemic_label") != "preference"
                ]
                modified = [
                    {
                        **m,
                        "before_claim": "",
                        "old_claim": "",
                        "after_claim": m.get("after_claim", "") if _live_atom_approved(m.get("filename")) else "",
                        "new_claim": m.get("new_claim", "") if _live_atom_approved(m.get("filename")) else "",
                        "redacted_revoked": True,
                    }
                    for m in (res.get("modified_atoms") or [])
                    if m.get("before_epistemic_label") != "preference"
                    and m.get("after_epistemic_label") != "preference"
                ]
                prev_summary = dict(res.get("summary") or {})
                res = {
                    **res,
                    "added_atoms": added,
                    "removed_atoms": removed,
                    "modified_atoms": modified,
                    "summary": {
                        **prev_summary,
                        "added": len(added),
                        "removed": len(removed),
                        "modified": len(modified),
                    },
                }
            return send(200, res)

        if path == "/v1/snapshots/recall" and method == "POST":
            from time_travel import recall_at_snapshot

            raw_limit = body.get("top_k") if "top_k" in body else body.get("limit", 5)
            if isinstance(raw_limit, bool) or not isinstance(raw_limit, (int, str)):
                raise ValueError("'limit' must be an integer between 1 and 25.")
            top_k = max(1, min(int(str(raw_limit).strip()[:9]), 25))
            inc_prefs = bool(is_owner and body.get("include_preferences", True))
            ras_params = inspect.signature(recall_at_snapshot).parameters
            ras_kwargs: dict[str, object] = {
                "project_scope": _str(body, "project_scope", max_len=120, required=False) or None,
                "brain_dir": rt.brain_dir,
            }
            if "include_preferences" in ras_params:
                ras_kwargs["include_preferences"] = inc_prefs
            if "top_k" in ras_params:
                ras_kwargs["top_k"] = top_k
            res = recall_at_snapshot(
                _str(body, "query", max_len=1000),
                _str(body, "snapshot_id", max_len=120),
                **ras_kwargs,
            )
            if not is_owner:
                filtered_results = []
                for hit in res.get("results") or []:
                    if hit.get("kind", "atom") == "playbook":
                        filtered_results.append(hit)
                        continue
                    ap = safe_file_in_dir(rt.atoms_dir, str(hit.get("filename") or ""), allowed_exts={".md"}, must_exist=True)
                    if ap and approved_revision_is_current(ap, brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir) and not _is_preference_atom_file(ap):
                        filtered_results.append(hit)
                res = {**res, "results": filtered_results}
            return send(200, res)

        if path == "/v1/snapshots/verify" and method == "POST":
            import time_travel

            sid = _str(body, "snapshot_id", max_len=120)
            verify_fn = getattr(time_travel, "verify_snapshot", None)
            if callable(verify_fn):
                res = verify_fn(sid, brain_dir=rt.brain_dir)
            else:
                res = _verify_snapshot_fallback(sid, brain_dir=rt.brain_dir)
            return send(200, res)

    except (MemoryRecallUnavailable, LexicalIndexUnavailable) as exc:
        return send(
            503,
            {
                "error": str(exc),
                "remediation": "Run 'second-brain rebuild-lexical' or start local Ollama.",
            },
        )
    except ReviewConflictError as exc:
        return send(409, {"error": str(exc), "conflict": True})
    except FileNotFoundError as exc:
        return send(404, {"error": str(exc)})
    except NoteError as exc:
        if str(exc).startswith("Note not found:"):
            return send(404, {"error": str(exc)})
        return send(422, {"error": str(exc)})
    except (PlaybookValidationError, ReviewTransitionError, ValueError, NotADirectoryError) as exc:
        return send(422, {"error": str(exc)})
    except HostConsentError as exc:
        return send(403, {"error": str(exc)})
    except PermissionError as exc:
        return send(403, {"error": str(exc)})
    except RuntimeError as exc:
        if "CAS mismatch" in str(exc):
            return send(409, {"error": str(exc), "conflict": True})
        raise
    return False
