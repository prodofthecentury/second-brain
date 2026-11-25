# /// script
# requires-python = ">=3.11,<3.14"
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

import base64
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
import hmac
import inspect
import json
import math
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
    archive_atom_to_cold,
    atomic_write_bytes,
    content_sha256,
    restore_cold_archived_atom,
    set_atom_temporal_bounds,
)
import config as _config  # noqa: E402
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
from git_distiller import (  # noqa: E402
    GitDistillerError,
    GitDistillerSecurityError,
    distill_git_commits,
    get_git_hook_status,
    install_git_post_commit_hook,
    uninstall_git_post_commit_hook,
)
from inbox_drop import (  # noqa: E402
    InboxDropError,
    InboxDropSecurityError,
    create_mobile_drop_pairing,
    list_mobile_drop_pairings,
    resolve_scoped_inbox_drop_s3_credentials,
    revoke_mobile_drop_pairing,
)
from lexical_store import LexicalIndexUnavailable  # noqa: E402
from memory_format import (  # noqa: E402
    VALID_EPISTEMIC_LABELS,
    VALID_MEMORY_TYPES,
    evaluate_temporal_validity,
    frontmatter_has_key,
    frontmatter_value,
    normalize_temporal_bound_str,
    remove_frontmatter_fields,
    render_memory_document,
    set_scalar_field,
    set_string_list_field,
    split_frontmatter,
)
from memory_review import (  # noqa: E402
    list_review_queue,
    normalize_status,
    parse_atom_record,
    parse_frontmatter_values,
    parse_scalar_list,
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
from onnx_models import (  # noqa: E402
    OnnxModelError,
    OnnxModelSecurityError,
    get_onnx_models_status,
    pull_onnx_models,
    remove_onnx_models,
)
from recall import MemoryRecallUnavailable, normalize_project_tag, project_tags_match  # noqa: E402
from skill_playbooks import PlaybookValidationError  # noqa: E402
from vault_backup import (  # noqa: E402
    VaultBackupError,
    VaultBackupSecurityError,
    create_vault_backup,
    restore_vault_backup,
    verify_vault_backup,
)
from vault_sync import (  # noqa: E402
    SyncConflictError,
    SyncError,
    SyncSecurityError,
    configure_auto_sync,
    get_auto_sync_status,
    get_sync_conflict_detail,
    init_sync,
    join_sync,
    list_sync_conflicts,
    migrate_sync_key_to_keychain,
    pull_mobile_inbox_drops,
    resolve_sync_conflict,
    run_auto_sync_cycle,
    sync_gc,
    sync_pull,
    sync_push,
    sync_status,
    sync_verify,
)
from vault_workspaces import (  # noqa: E402
    VaultWorkspaceError,
    VaultWorkspaceSecurityError,
    create_vault,
    federated_recall,
    list_vaults,
    remove_vault,
    switch_vault,
    update_vault_policy,
)
from web_clipper import (  # noqa: E402
    ClipperError,
    ClipperSecurityError,
    create_clipper_pairing,
    create_clipper_pairing_code,
    ingest_clipper_media_bytes,
    ingest_web_clip,
    list_clipper_pairings,
    redeem_clipper_pairing_code,
    revoke_clipper_pairing,
)

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
_ATOM_SUMMARY_CACHE_MAX = 8192
_ATOM_SUMMARY_CACHE_LOCK = threading.Lock()
_ATOM_SUMMARY_BY_SHA: dict[str, dict[str, object]] = {}


# ─── Owner credential & rate guards ──────────────────────────────────────────

def default_owner_token_path() -> Path:
    override = (os.environ.get("BRAIN_OWNER_TOKEN_FILE") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    sec_override = (
        os.environ.get("BRAIN_SECURITY_STATE_DIR")
        or os.environ.get("SECOND_BRAIN_SECURITY_DIR")
        or ""
    ).strip()
    if sec_override:
        return Path(sec_override).expanduser().resolve() / "owner-token"
    cfg_sec = getattr(_config, "security_state_dir", None)
    if callable(cfg_sec):
        return Path(cfg_sec()).expanduser().resolve() / "owner-token"
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    elif os.environ.get("XDG_STATE_HOME"):
        base = Path(os.environ["XDG_STATE_HOME"]).expanduser()
    else:
        base = Path.home() / ".local" / "state"
    return base / "SecondBrain" / "security" / "owner-token"


_JOBS_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, object]] = {}


def _start_operator_job(kind: str, script_name: str, extra_args: list[str], *, brain_dir: str) -> dict[str, object]:
    import subprocess
    import tempfile

    scripts_dir = Path(__file__).resolve().parent
    script_path = scripts_dir / script_name
    if not script_path.is_file():
        raise FileNotFoundError(f"Operator script not found: {script_name}")
    job_id = f"job-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    rec: dict[str, object] = {
        "job_id": job_id,
        "kind": kind,
        "status": "running",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "returncode": None,
        "output_tail": "",
    }
    with _JOBS_LOCK:
        if len(_JOBS) > 64:
            oldest = next(iter(_JOBS))
            _JOBS.pop(oldest, None)
        _JOBS[job_id] = rec

    writable_cwd = str(Path(brain_dir).resolve().parent) if os.access(str(Path(brain_dir).resolve().parent), os.W_OK) else tempfile.gettempdir()
    env = dict(os.environ)
    env["BRAIN_VAULT_DIR"] = str(brain_dir)

    def _runner() -> None:
        try:
            proc = subprocess.run(
                [sys.executable, str(script_path), *extra_args],
                cwd=writable_cwd,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
                check=False,
            )
            combined = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
            with _JOBS_LOCK:
                rec["returncode"] = int(proc.returncode)
                rec["status"] = "completed" if proc.returncode == 0 else "failed"
                rec["output_tail"] = combined[-8000:]
        except subprocess.TimeoutExpired:
            with _JOBS_LOCK:
                rec["returncode"] = -1
                rec["status"] = "failed"
                rec["output_tail"] = "Job timed out after 900 seconds."
        except Exception as exc:
            with _JOBS_LOCK:
                rec["returncode"] = -1
                rec["status"] = "failed"
                rec["output_tail"] = f"Job error: {exc}"

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=1.5)
    with _JOBS_LOCK:
        return dict(rec)


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


def _tags_strictly_in_project_scope(tags: list[str] | tuple[str, ...], project: str | None) -> bool:
    """Enforce strict normalized multi-project containment (excluding unscoped & mixed-project items)."""
    if not project or not str(project).strip():
        return True
    scope_tag = normalize_project_tag(str(project))
    if not scope_tag:
        return True
    proj_tags = [str(t).strip() for t in (tags or ()) if str(t).strip().lower().startswith("project:")]
    return bool(proj_tags) and all(bool(project_tags_match((t,), (scope_tag,))) for t in proj_tags)


class _BadRequestError(ValueError):
    """Validation error mapped to HTTP 400 Bad Request inside api_product.dispatch()."""


_PROJECT_SCOPE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _optional_project_scope(val: object) -> str | None:
    """Validate and normalize an optional project:<slug> scope parameter.

    Returns None for None or blank input; raises _BadRequestError (a ValueError subclass
    mapped to HTTP 400) for malformed or path-traversal project scopes.
    """
    if val is None:
        return None
    if not isinstance(val, str):
        raise _BadRequestError("Invalid 'project' scope: must be a string.")
    raw = val.strip()
    if not raw:
        return None
    if len(raw) > 120 or "\x00" in raw or "\\" in raw or ".." in raw:
        raise _BadRequestError(f"Invalid 'project' scope: {val!r}")
    body = raw[len("project:") :] if raw.lower().startswith("project:") else raw
    if not body or body.startswith("/") or body.endswith("/") or "//" in body:
        raise _BadRequestError(f"Invalid 'project' scope: {val!r}")
    segments = body.split("/")
    if any(seg in {".", ".."} or not _PROJECT_SCOPE_SEGMENT_RE.fullmatch(seg) for seg in segments):
        raise _BadRequestError(f"Invalid 'project' scope: {val!r}")
    normalized = normalize_project_tag(raw)
    if not normalized or normalized == "project:":
        raise _BadRequestError(f"Invalid 'project' scope: {val!r}")
    return normalized


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
    raw = str(vals[0]).strip()
    if len(raw) > 12:
        raise ValueError(f"Invalid integer query parameter '{key}'.")
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid integer query parameter '{key}'.") from exc
    return max(min_val, min(parsed, max_val))


def _parse_json_int(
    body: dict,
    key: str,
    *,
    default: int | None = None,
    min_val: int | None = None,
    max_val: int | None = None,
    clamp: bool = False,
    allow_none: bool = False,
    error_cls: type[Exception] = ValueError,
) -> int | None:
    if key not in body:
        return default
    raw = body.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if allow_none:
            return None
        raise error_cls(f"'{key}' must be an integer.")
    if isinstance(raw, bool):
        raise error_cls(f"'{key}' must be an integer, not a boolean.")
    if isinstance(raw, int):
        parsed = int(raw)
    elif isinstance(raw, str):
        cleaned = raw.strip()
        if len(cleaned) > 18:
            raise error_cls(f"'{key}' must be a valid integer.")
        try:
            parsed = int(cleaned)
        except ValueError as exc:
            raise error_cls(f"'{key}' must be a valid integer.") from exc
    else:
        raise error_cls(f"'{key}' must be an integer.")
    if clamp:
        if min_val is not None:
            parsed = max(min_val, parsed)
        if max_val is not None:
            parsed = min(max_val, parsed)
    else:
        if min_val is not None and parsed < min_val:
            raise error_cls(f"'{key}' must be >= {min_val}.")
        if max_val is not None and parsed > max_val:
            raise error_cls(f"'{key}' must be <= {max_val}.")
    return parsed


def _parse_json_float(
    body: dict,
    key: str,
    *,
    default: float,
    error_cls: type[Exception] = ValueError,
) -> float:
    if key not in body:
        return default
    raw = body.get(key)
    if raw is None or isinstance(raw, bool):
        raise error_cls(f"'{key}' must be a finite number.")
    if isinstance(raw, (int, float)):
        val = float(raw)
    elif isinstance(raw, str):
        cleaned = raw.strip()
        if not cleaned or len(cleaned) > 32:
            raise error_cls(f"'{key}' must be a finite number.")
        try:
            val = float(cleaned)
        except ValueError as exc:
            raise error_cls(f"'{key}' must be a finite number.") from exc
    else:
        raise error_cls(f"'{key}' must be a finite number.")
    if not math.isfinite(val):
        raise error_cls(f"'{key}' must be a finite number.")
    return val


def _default_mobile_export_dir(rt: object) -> str:
    """Return a user-writable export directory outside read-only application bundles."""
    sec_dir = getattr(rt, "security_dir", None) or getattr(rt, "sync_state_dir", None)
    if sec_dir is not None and str(sec_dir).strip():
        base_state = Path(str(sec_dir)).expanduser().resolve()
    else:
        base_state = _config.security_state_dir().resolve()
    return str(base_state.parent / "exports" / "mobile-vault-client")


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


def _atom_card(path: str, rt, *, as_of: str | None = None) -> dict[str, object]:
    rec = parse_atom_record(path)
    raw = Path(path).read_bytes()
    raw_text = raw.decode("utf-8")
    parsed = split_frontmatter(raw_text)
    fm_vals = parse_frontmatter_values(parsed[0]) if parsed else {}
    evidence = section_text("\n".join(parsed[1]), "Evidence") if parsed else ""
    epistemic_label = str(fm_vals.get("epistemic_label") or rec.get("epistemic_label") or "claim").strip() or "claim"
    epistemic_contract = str(fm_vals.get("epistemic_contract") or rec.get("epistemic_contract") or "").strip()
    valid_from = str(rec.get("valid_from") or fm_vals.get("valid_from") or "").strip()
    valid_until = str(rec.get("valid_until") or fm_vals.get("valid_until") or "").strip()
    tv = evaluate_temporal_validity(valid_from, valid_until, as_of=as_of)
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
        "valid_from": valid_from,
        "valid_until": valid_until,
        "temporal_status": str(tv["status"]),
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


def _apply_temporal_overrides_to_frontmatter(fm: list[str], body: dict) -> list[str]:
    if "valid_from" in body:
        raw_vf = _str(body, "valid_from", max_len=64, required=False).strip()
        norm_vf = normalize_temporal_bound_str(raw_vf) if raw_vf else ""
        if norm_vf:
            fm = set_scalar_field(fm, "valid_from", norm_vf, "last-reviewed")
        else:
            fm = remove_frontmatter_fields(fm, {"valid_from"})

    if "valid_until" in body:
        raw_vu = _str(body, "valid_until", max_len=64, required=False).strip()
        norm_vu = normalize_temporal_bound_str(raw_vu) if raw_vu else ""
        if norm_vu:
            after_key = "valid_from" if frontmatter_has_key(fm, "valid_from") else "last-reviewed"
            fm = set_scalar_field(fm, "valid_until", norm_vu, after_key)
        else:
            fm = remove_frontmatter_fields(fm, {"valid_until"})

    eff_vf = frontmatter_value(fm, "valid_from")
    eff_vu = frontmatter_value(fm, "valid_until")
    if eff_vf or eff_vu:
        tv = evaluate_temporal_validity(eff_vf, eff_vu)
        if tv["status"] == "invalid_window":
            raise ValueError("Invalid bi-temporal validity window: valid_from must be <= valid_until.")
    return fm


def _apply_temporal_overrides_to_markdown(raw_md: str, body: dict) -> str:
    parsed = split_frontmatter(raw_md)
    if parsed is None:
        if "valid_from" in body or "valid_until" in body:
            raise ValueError("Target atom is missing valid YAML frontmatter.")
        return raw_md
    fm, body_lines = list(parsed[0]), list(parsed[1])
    fm = _apply_temporal_overrides_to_frontmatter(fm, body)
    if "valid_from" in body or "valid_until" in body:
        return render_memory_document(fm, body_lines)
    return raw_md


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

    fm = _apply_temporal_overrides_to_frontmatter(fm, body)

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


def _atom_summary_from_indexed_row(filename: str, content: str, sha256: str) -> dict[str, object] | None:
    """Parse static atom card metadata from FTS5 row content once per (filename, content_sha256)."""
    cache_key = f"{filename}:{sha256}"
    with _ATOM_SUMMARY_CACHE_LOCK:
        cached = _ATOM_SUMMARY_BY_SHA.get(cache_key)
    if cached is not None:
        tv_cached = evaluate_temporal_validity(
            str(cached.get("valid_from") or ""),
            str(cached.get("valid_until") or ""),
        )
        return {
            **cached,
            "temporal_status": str(tv_cached["status"]),
            "contexts": list(cached["contexts"]),  # type: ignore[arg-type]
            "sources": list(cached["sources"]),  # type: ignore[arg-type]
        }

    parsed = split_frontmatter(content)
    if parsed is None:
        return None
    fm_lines, body_lines = parsed
    fm_vals = parse_frontmatter_values(fm_lines)
    body_text = "\n".join(body_lines)

    raw_sources = fm_vals.get("sources", [])
    sources = parse_scalar_list(raw_sources) if isinstance(raw_sources, str) else list(raw_sources)
    raw_contexts = fm_vals.get("contexts", [])
    contexts = parse_scalar_list(raw_contexts) if isinstance(raw_contexts, str) else list(raw_contexts)

    review_status = normalize_status(str(fm_vals.get("review_status", "pending")))
    memory_type = str(fm_vals.get("memory_type", "semantic") or "semantic")
    origin = str(fm_vals.get("origin", "imported_unknown") or "imported_unknown")
    epistemic_label_raw = str(fm_vals.get("epistemic_label") or "claim").strip() or "claim"
    epistemic_contract = str(fm_vals.get("epistemic_contract") or "").strip()
    effective_label = (
        "preference" if (epistemic_label_raw == "preference" or epistemic_contract) else epistemic_label_raw
    )
    valid_from = str(fm_vals.get("valid_from") or "").strip()
    valid_until = str(fm_vals.get("valid_until") or "").strip()
    summary: dict[str, object] = {
        "filename": filename,
        "claim": section_text(body_text, "Claim"),
        "evidence": section_text(body_text, "Evidence"),
        "review_status": review_status,
        "status": review_status,
        "memory_type": memory_type,
        "epistemic_label": effective_label,
        "epistemic_contract": epistemic_contract or None,
        "contexts": list(contexts),
        "origin": origin,
        "sources": list(sources),
        "valid_from": valid_from,
        "valid_until": valid_until,
        "sha256": sha256,
    }
    with _ATOM_SUMMARY_CACHE_LOCK:
        if len(_ATOM_SUMMARY_BY_SHA) >= _ATOM_SUMMARY_CACHE_MAX and cache_key not in _ATOM_SUMMARY_BY_SHA:
            _ATOM_SUMMARY_BY_SHA.pop(next(iter(_ATOM_SUMMARY_BY_SHA)), None)
        _ATOM_SUMMARY_BY_SHA[cache_key] = summary
    tv_live = evaluate_temporal_validity(valid_from, valid_until)
    return {
        **summary,
        "temporal_status": str(tv_live["status"]),
        "contexts": list(contexts),
        "sources": list(sources),
    }


def _live_approved_atom_sha(fname: object, rt) -> str | None:
    """Return live content_sha256 of rt.atoms_dir/<fname> iff current, non-symlink, non-preference, and approved."""
    raw_name = str(fname or "").strip()
    if not raw_name:
        return None
    ap = safe_file_in_dir(rt.atoms_dir, raw_name, allowed_exts={".md"}, must_exist=True)
    if not ap or _is_reparse_or_link(ap) or not os.path.isfile(ap):
        return None
    try:
        raw_bytes = Path(ap).read_bytes()
        raw_text = raw_bytes.decode("utf-8")
        rec = parse_atom_record(ap)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if is_preference_atom_content(raw_text):
        return None
    if rec.get("review_status") != "accepted" or bool(rec.get("superseded_by")):
        return None
    tv_live_atom = evaluate_temporal_validity(
        str(rec.get("valid_from") or "").strip(),
        str(rec.get("valid_until") or "").strip(),
    )
    if not tv_live_atom["active"] or tv_live_atom["status"] != "active":
        return None
    if not approved_revision_is_current(ap, brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir):
        return None
    return content_sha256(raw_bytes)


def _is_live_preference_atom(fname: object, rt) -> bool:
    raw_name = str(fname or "").strip()
    if not raw_name:
        return False
    ap = safe_file_in_dir(rt.atoms_dir, raw_name, allowed_exts={".md"}, must_exist=True)
    if not ap or _is_reparse_or_link(ap) or not os.path.isfile(ap):
        return False
    return _is_preference_atom_file(ap)


def _live_active_playbook_report(fname: object, rt):
    """Verify live Playbook integrity and return PlaybookIntegrityReport iff valid and active."""
    from skill_playbooks import verify_playbook_integrity

    raw_name = str(fname or "").strip()
    if not raw_name:
        return None
    pb_path = safe_file_in_dir(rt.playbooks_dir, raw_name, allowed_exts={".md"}, must_exist=True)
    if not pb_path or _is_reparse_or_link(pb_path) or not os.path.isfile(pb_path):
        return None
    try:
        rep = verify_playbook_integrity(
            pb_path,
            brain_dir=rt.brain_dir,
            atoms_dir=rt.atoms_dir,
            playbooks_dir=rt.playbooks_dir,
            telemetry_db_path=rt.telemetry_db_path,
        )
    except Exception:
        return None
    if not rep.valid or rep.status != "active":
        return None
    try:
        disk_sha = content_sha256(Path(pb_path).read_bytes())
    except OSError:
        return None
    if not rep.sha256 or disk_sha != rep.sha256:
        return None
    return rep


def _prove_leaf_fallback(
    leaf_ref: str,
    *,
    brain_dir: str,
    atoms_dir: str,
    playbooks_dir: str,
) -> dict[str, object]:
    import vault_ledger

    parts = [p for p in leaf_ref.split("/") if p]
    candidates: list[str] = []
    if len(parts) == 2:
        prefix, raw_name = parts[0].lower(), normalize_markdown_filename(parts[1])
        if prefix in {"2-atoms", "atoms"}:
            ap = safe_file_in_dir(atoms_dir, raw_name, allowed_exts={".md"}, must_exist=True)
            if not ap:
                raise FileNotFoundError(f"Atom not found: {leaf_ref}")
            candidates.append(f"2-atoms/{os.path.basename(ap)}")
        elif prefix in {"5-playbooks", "playbooks"}:
            pp = safe_file_in_dir(playbooks_dir, raw_name, allowed_exts={".md"}, must_exist=True)
            if not pp:
                raise FileNotFoundError(f"Playbook not found: {leaf_ref}")
            candidates.append(f"5-playbooks/{os.path.basename(pp)}")
        elif prefix in {"6-skills", "skills"}:
            skills_dir = os.path.join(brain_dir, "6-skills")
            sp = safe_file_in_dir(skills_dir, raw_name, allowed_exts={".md"}, must_exist=True)
            if not sp:
                raise FileNotFoundError(f"Executable skill not found: {leaf_ref}")
            candidates.append(f"6-skills/{os.path.basename(sp)}")
        else:
            raise FileNotFoundError(f"Unknown vault leaf prefix: {leaf_ref}")
    elif len(parts) == 1:
        raw_name = normalize_markdown_filename(parts[0])
        skills_dir = os.path.join(brain_dir, "6-skills")
        ap = safe_file_in_dir(atoms_dir, raw_name, allowed_exts={".md"}, must_exist=True)
        pp = safe_file_in_dir(playbooks_dir, raw_name, allowed_exts={".md"}, must_exist=True)
        sp = safe_file_in_dir(skills_dir, raw_name, allowed_exts={".md"}, must_exist=True) if os.path.isdir(skills_dir) else None
        if ap:
            candidates.append(f"2-atoms/{os.path.basename(ap)}")
        if pp:
            candidates.append(f"5-playbooks/{os.path.basename(pp)}")
        if sp:
            candidates.append(f"6-skills/{os.path.basename(sp)}")
        if not candidates:
            raise FileNotFoundError(f"Leaf not found: {leaf_ref}")
    else:
        raise FileNotFoundError(f"Leaf not found: {leaf_ref}")

    merkle = vault_ledger.compute_vault_merkle(
        brain_dir=brain_dir, atoms_dir=atoms_dir, playbooks_dir=playbooks_dir
    )
    leaves = merkle["leaves"]
    idx = None
    target_rel = candidates[0]
    for cand in candidates:
        found = next((i for i, leaf in enumerate(leaves) if leaf["rel_path"] == cand), None)
        if found is not None:
            idx = found
            target_rel = cand
            break
    if idx is None:
        raise PermissionError(f"Target '{leaf_ref}' is not an accepted member of the verified vault Merkle tree.")

    levels = vault_ledger._build_merkle_levels([leaf["leaf_sha256"] for leaf in leaves])
    siblings: list[dict[str, str]] = []
    cur_idx = idx
    for level in levels[:-1]:
        if cur_idx % 2 == 0:
            if cur_idx + 1 < len(level):
                siblings.append({"position": "right", "sha256": level[cur_idx + 1]})
            else:
                siblings.append({"position": "odd", "sha256": level[cur_idx]})
        else:
            siblings.append({"position": "left", "sha256": level[cur_idx - 1]})
        cur_idx //= 2

    ledger = vault_ledger.verify_vault_ledger(
        brain_dir=brain_dir, atoms_dir=atoms_dir, playbooks_dir=playbooks_dir
    )
    return {
        "schema_version": 1,
        "rel_path": target_rel,
        "file_sha256": leaves[idx]["file_sha256"],
        "leaf_sha256": leaves[idx]["leaf_sha256"],
        "siblings": siblings,
        "merkle_root": merkle["merkle_root"],
        "ledger_head_entry_sha256": ledger["head_entry_sha256"] if ledger["synced_with_head"] else None,
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
    rt = getattr(handler, "_request_runtime", None) or handler.server.runtime
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

        if path == "/v1/notes/tasks" and method == "GET":
            from notes_store import extract_action_items

            proj = ((q.get("project") or [None])[0] or None)
            if proj and len(proj) > 120:
                raise ValueError("Invalid project query filter.")
            tag_f = ((q.get("tag") or [None])[0] or None)
            if tag_f and len(tag_f) > 80:
                raise ValueError("Invalid tag query filter.")
            inc_completed = (q.get("include_completed") or ["0"])[0] in {"1", "true", "yes"}
            req_inc_raw = (q.get("include_raw") or ["0"])[0] in {"1", "true", "yes"}
            inc_raw = bool(is_owner and req_inc_raw)
            tasks = extract_action_items(
                paths=paths,
                include_completed=inc_completed,
                include_raw=inc_raw,
                project=proj,
                tag=tag_f,
            )
            return send(200, {"count": len(tasks), "tasks": tasks, "items": tasks})

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
                exp_note_sha = (
                    _str(body, "expected_sha256", max_len=64, required=False)
                    or _str(body, "expected_note_sha256", max_len=64)
                )
                res = verify_note_excerpt(
                    filename,
                    _str(body, "excerpt", max_len=4000),
                    actor=_str(body, "actor", max_len=120),
                    expected_note_sha256=exp_note_sha,
                    claim=body.get("claim") if isinstance(body.get("claim"), str) else None,
                    memory_type=body.get("memory_type") if isinstance(body.get("memory_type"), str) else "semantic",
                    tags=_tags(body) if "tags" in body else None,
                    valid_from=_str(body, "valid_from", max_len=64, required=False).strip() or None,
                    valid_until=_str(body, "valid_until", max_len=64, required=False).strip() or None,
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

        # ── Multi-turn Chat Threads (owner-only) & Streaming Chat ──
        if path == "/v1/chat/personas" and method == "GET":
            from chat_threads import BEHAVIOR_MODES, WORK_MODES, list_persona_profiles

            return send(
                200,
                {
                    "personas": list_persona_profiles(),
                    "work_modes": list(WORK_MODES),
                    "behavior_modes": list(BEHAVIOR_MODES),
                },
            )

        if path == "/v1/chat/threads" and method == "GET":
            if not owner_only():
                return True
            from chat_threads import list_chat_threads

            limit = _parse_bounded_int(q, "limit", default=50, min_val=1, max_val=200)
            offset = _parse_bounded_int(q, "offset", default=0, min_val=0, max_val=1_000_000)
            proj = ((q.get("project") or [None])[0] or None)
            if proj and len(proj) > 120:
                raise ValueError("Invalid project query filter.")
            return send(200, list_chat_threads(limit=limit, offset=offset, project_scope=proj, brain_dir=rt.brain_dir))

        if path == "/v1/chat/threads" and method == "POST":
            if not owner_only():
                return True
            from chat_threads import create_chat_thread

            created = create_chat_thread(
                title=_str(body, "title", max_len=160, required=False),
                project_scope=_str(body, "project_scope", max_len=120, required=False).strip() or None,
                host_id=_str(body, "host_id", default="ollama_local", max_len=64, required=False) or "ollama_local",
                mode=_str(body, "mode", default="verified", max_len=32, required=False) or "verified",
                work_mode=_str(body, "work_mode", default="chat", max_len=16, required=False) or "chat",
                persona_id=_str(body, "persona_id", default="grounded_default", max_len=48, required=False)
                or "grounded_default",
                behavior_mode=_str(body, "behavior_mode", default="autopilot", max_len=16, required=False)
                or "autopilot",
                custom_rules=_str(body, "custom_rules", default="", max_len=4000, required=False),
                done_criteria=_str(body, "done_criteria", default="", max_len=2000, required=False),
                brain_dir=rt.brain_dir,
            )
            return send(201, created)

        if path.startswith("/v1/chat/threads/"):
            if not owner_only():
                return True
            from chat_threads import (
                delete_chat_thread,
                get_chat_thread,
                pin_turn_to_note,
                update_chat_thread_settings,
            )

            rest = unquote(path[len("/v1/chat/threads/") :]).strip("/")
            if rest.endswith("/pin") and method == "POST":
                tid = rest[: -len("/pin")].strip()
                raw_turn_id = body.get("turn_id")
                if raw_turn_id is not None and (isinstance(raw_turn_id, bool) or not isinstance(raw_turn_id, int)):
                    raise ValueError("'turn_id' must be an integer or null.")
                pinned = pin_turn_to_note(
                    tid,
                    turn_id=raw_turn_id,
                    title=_str(body, "title", max_len=160, required=False).strip() or None,
                    tags=_tags(body) if "tags" in body else (),
                    brain_dir=rt.brain_dir,
                )
                return send(201, pinned)
            if method == "GET":
                max_turns = _parse_bounded_int(q, "max_turns", default=100, min_val=1, max_val=500)
                return send(200, get_chat_thread(rest, max_turns=max_turns, brain_dir=rt.brain_dir))
            if method == "PATCH":
                updated = update_chat_thread_settings(
                    rest,
                    title=body.get("title") if "title" in body else None,
                    work_mode=body.get("work_mode") if "work_mode" in body else None,
                    persona_id=body.get("persona_id") if "persona_id" in body else None,
                    behavior_mode=body.get("behavior_mode") if "behavior_mode" in body else None,
                    custom_rules=body.get("custom_rules") if "custom_rules" in body else None,
                    done_criteria=body.get("done_criteria") if "done_criteria" in body else None,
                    brain_dir=rt.brain_dir,
                )
                return send(200, updated)
            if method == "DELETE":
                return send(200, delete_chat_thread(rest, brain_dir=rt.brain_dir))
            return send(405, {"error": "Method not allowed."})

        if path == "/v1/chat/stream" and method == "POST":
            from chat_threads import get_chat_thread
            from streaming_chat import ChatPaths, ChatStreamRequest, chat_events

            chat_payload = dict(body)
            if "mode" not in chat_payload or chat_payload.get("mode") is None or (
                isinstance(chat_payload.get("mode"), str) and not str(chat_payload.get("mode")).strip()
            ):
                chat_payload["mode"] = "stream"
            req = ChatStreamRequest.model_validate(chat_payload)
            if req.thread_id and not is_owner:
                return send(
                    403,
                    {"error": "403 Forbidden: persistent chat threads require owner authorization."},
                )
            if req.thread_id and req.thread_id != "new":
                # Validate thread existence synchronously so missing threads return HTTP 404 before opening SSE.
                get_chat_thread(req.thread_id, max_turns=1, brain_dir=rt.brain_dir)
            stream_sem = getattr(handler.server, "_stream_slots", None)
            if stream_sem is not None and not stream_sem.acquire(blocking=False):
                return send(429, {"error": "Too many concurrent chat streams."})
            try:
                cp = ChatPaths(
                    brain_dir=rt.brain_dir,
                    atoms_dir=rt.atoms_dir,
                    lexical_index_path=rt.lexical_index_path,
                    chroma_path=rt.chroma_path,
                    policy_path=rt.policy_path,
                    security_dir=getattr(rt, "security_dir", None),
                    telemetry_db_path=rt.telemetry_db_path,
                    http_transport=rt.http_transport,
                    skip_dns_check_for_mock_transport=rt.skip_dns_check_for_mock_transport,
                    include_quarantined_inbox=bool(is_owner),
                    persist_threads_allowed=bool(is_owner),
                )
                if req.answer_view == "preference" and not is_owner:
                    return send(
                        403,
                        {"error": "403 Forbidden: subjective preference view requires owner authorization."},
                    )
                inc_prefs = bool(is_owner and req.include_preferences)
                ce_kwargs: dict[str, object] = {
                    "mode": req.mode,
                    "paths": cp,
                    "project_scope": req.project_scope,
                    "model_override": req.model_override,
                    "include_preferences": inc_prefs,
                    "host_id": req.host_id,
                    "retrieval_mode": req.retrieval_mode,
                    "allow_cross_project": req.allow_cross_project,
                    "answer_view": req.answer_view,
                    "allow_unverified_leads": bool(is_owner and (req.answer_view == "research" or req.mode == "research")),
                    "include_expired": bool(req.include_expired),
                    "as_of": req.as_of.strip() if req.as_of and req.as_of.strip() else None,
                    "work_mode": req.work_mode,
                    "persona_id": req.persona_id,
                    "behavior_mode": req.behavior_mode,
                    "custom_rules": req.custom_rules,
                    "done_criteria": req.done_criteria,
                }
                if req.thread_id:
                    ce_kwargs["thread_id"] = req.thread_id
                if req.history:
                    ce_kwargs["history"] = [h.model_dump() for h in req.history]
                stream_sse(
                    handler,
                    chat_events(req.query, **ce_kwargs),
                    on_open=lambda: _audit_fn(rt.brain_dir, path, method, 200),
                )
                return True
            finally:
                if stream_sem is not None:
                    stream_sem.release()

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

            actor = _str(body, "actor", max_len=120, required=False).strip() or "owner"
            note = _str(body, "note", max_len=1000, required=False).strip()
            raw_kind = body.get("feedback_kind")
            if raw_kind is not None:
                kind_norm = _str(body, "feedback_kind", max_len=32, required=False).strip().lower()
                if kind_norm and kind_norm not in {"helpful", "correction", "supersession"}:
                    raise ValueError("Invalid 'feedback_kind'; expected 'helpful', 'correction', or 'supersession'.")
            else:
                kind_norm = ""

            allowed_fb = {
                "endorsed",
                "helpful",
                "positive",
                "corrected",
                "correction",
                "unhelpful",
                "negative",
                "superseded",
                "supersession",
            }
            raw_fb = body.get("feedback") if "feedback" in body else body.get("signal")
            if kind_norm:
                if isinstance(raw_fb, str) and raw_fb.strip() and raw_fb.strip().lower() not in allowed_fb:
                    raise ValueError("Invalid 'feedback'; expected 'endorsed', 'corrected', or 'superseded'.")
                helpful = kind_norm == "helpful"
                resolved_kind = kind_norm
            elif isinstance(raw_fb, str) and raw_fb.strip():
                fb_norm = raw_fb.strip().lower()
                if fb_norm not in allowed_fb:
                    raise ValueError("Invalid 'feedback'; expected 'endorsed', 'corrected', or 'superseded'.")
                helpful = fb_norm in {"endorsed", "helpful", "positive"}
                resolved_kind = (
                    "helpful"
                    if helpful
                    else ("supersession" if fb_norm in {"superseded", "supersession"} else "correction")
                )
            elif isinstance(body.get("helpful"), bool):
                helpful = bool(body.get("helpful"))
                resolved_kind = "helpful" if helpful else "correction"
            else:
                raise ValueError("Provide 'feedback' ('endorsed' | 'corrected' | 'superseded'), 'feedback_kind', or boolean 'helpful'.")

            fb_params = inspect.signature(record_atom_feedback).parameters
            fb_kwargs: dict[str, object] = {
                "helpful": helpful,
                "db_path": rt.telemetry_db_path,
                "brain_dir": rt.brain_dir,
            }
            if "actor" in fb_params:
                fb_kwargs["actor"] = actor
            if "note" in fb_params:
                fb_kwargs["note"] = note
            if "feedback_kind" in fb_params:
                fb_kwargs["feedback_kind"] = resolved_kind
            fb_res = record_atom_feedback(atom_fn, **fb_kwargs)
            resp_payload: dict[str, object] = {
                "status": "recorded",
                "atom_filename": atom_fn,
                "helpful": helpful,
                "feedback_kind": resolved_kind,
                "actor": actor,
                "note": note,
            }
            if isinstance(fb_res, dict):
                for k, v in fb_res.items():
                    resp_payload.setdefault(k, v)
            return send(200, resp_payload)

        # ── One-click review & Atom Governance ──
        if path == "/v1/review/queue" and method == "GET":
            if not owner_only():
                return True
            limit = _parse_bounded_int(q, "limit", default=200, min_val=1, max_val=500)
            offset = _parse_bounded_int(q, "offset", default=0, min_val=0, max_val=1_000_000)
            all_items = []
            for rec in list_review_queue(rt.atoms_dir, {"pending", "needs_edit"}):
                p = safe_file_in_dir(rt.atoms_dir, rec["filename"], allowed_exts={".md"}, must_exist=True)
                if p and not _is_reparse_or_link(p):
                    card = _safe_atom_card(p, rt, fallback=rec)
                    if card is not None:
                        all_items.append(card)
            page = all_items[offset : offset + limit]
            return send(
                200,
                {
                    "items": page,
                    "count": len(page),
                    "total": len(all_items),
                    "offset": offset,
                    "limit": limit,
                    "has_more": offset + len(page) < len(all_items),
                },
            )

        if path == "/v1/atoms" and method == "GET":
            from atom_mutator import review_lock
            from lexical_store import (
                lexical_index_is_current,
                list_indexed_atoms_fast,
                sync_lexical_index,
                tokenize,
            )
            from recall import project_tags_match

            limit = _parse_bounded_int(q, "limit", default=200, min_val=1, max_val=500)
            offset = _parse_bounded_int(q, "offset", default=0, min_val=0, max_val=1_000_000)
            q_search = (q.get("q") or q.get("query") or [""])[0].strip()[:500]
            proj_filter = (q.get("project") or [""])[0].strip()
            if len(proj_filter) > 120:
                raise ValueError("Invalid project query filter.")
            inc_exp_list = (q.get("include_expired") or ["0"])[0] in {"1", "true", "yes"}

            if (os.path.lexists(rt.brain_dir) and _is_reparse_or_link(rt.brain_dir)) or (
                os.path.lexists(rt.atoms_dir) and _is_reparse_or_link(rt.atoms_dir)
            ):
                raise PermissionError("Refusing to list atoms through a symlink or reparse point.")
            if not os.path.isdir(rt.atoms_dir):
                return send(
                    200,
                    {"atoms": [], "items": [], "count": 0, "total": 0, "offset": offset, "limit": limit},
                )

            use_fast_index = False
            if rt.lexical_index_path:
                if lexical_index_is_current(rt.atoms_dir, rt.lexical_index_path):
                    use_fast_index = True
                else:
                    try:
                        lock_path = os.path.join(os.path.dirname(os.path.abspath(rt.lexical_index_path)), "writer.lock")
                        with review_lock(lock_path):
                            sync_lexical_index(rt.atoms_dir, rt.lexical_index_path)
                        use_fast_index = True
                    except (OSError, UnicodeDecodeError, RuntimeError, ValueError, LexicalIndexUnavailable):
                        use_fast_index = False

            matching: list[dict[str, object]] = []
            if use_fast_index:
                from atom_mutator import decision_log_path, latest_decisions_by_atom

                canon_atoms_dir = os.path.realpath(rt.atoms_dir)
                try:
                    batch_decision_map: dict[str, dict] | None = latest_decisions_by_atom(
                        decision_log_path(rt.brain_dir)
                    )
                except Exception:
                    batch_decision_map = None
                batch_source_memo: dict[str, str | None] = {}
                rows, _ = list_indexed_atoms_fast(rt.lexical_index_path, query=q_search, limit=100_000, offset=0)
                for row in rows:
                    fname = row.filename
                    if (
                        not is_content_markdown(fname)
                        or "/" in fname
                        or "\\" in fname
                        or fname in {".", ".."}
                    ):
                        continue
                    atom_path = os.path.join(canon_atoms_dir, fname)
                    if not os.path.lexists(atom_path) or _is_reparse_or_link(atom_path):
                        continue
                    base = _atom_summary_from_indexed_row(fname, row.content, row.content_sha256)
                    if base is None:
                        continue
                    if proj_filter:
                        if is_owner:
                            if not project_tags_match(tuple(base["contexts"]), (proj_filter,)):  # type: ignore[arg-type]
                                continue
                        elif not _tags_strictly_in_project_scope(tuple(base["contexts"]), proj_filter):  # type: ignore[arg-type]
                            continue
                    if not is_owner and (
                        base.get("epistemic_label") == "preference" or bool(base.get("epistemic_contract"))
                    ):
                        continue
                    if not is_owner and base.get("review_status") != "accepted":
                        continue
                    if not is_owner and (
                        base.get("temporal_status") == "invalid_window"
                        or (base.get("temporal_status") != "active" and not inc_exp_list)
                    ):
                        continue
                    verified = (
                        base.get("review_status") == "accepted"
                        and approved_revision_is_current(
                            atom_path,
                            brain_dir=rt.brain_dir,
                            atoms_dir=rt.atoms_dir,
                            known_atom_bytes=row.content.encode("utf-8"),
                            known_atom_sha256=row.content_sha256,
                            known_decision_map=batch_decision_map,
                            source_sha_memo=batch_source_memo,
                            canonical_atoms_root=canon_atoms_dir,
                        )
                    )
                    if not is_owner and not verified:
                        continue
                    matching.append({**base, "verified": verified})
            else:
                q_terms = set(tokenize(q_search)) if q_search else set()
                for name in sorted(os.listdir(rt.atoms_dir)):
                    if not is_content_markdown(name):
                        continue
                    p = safe_file_in_dir(rt.atoms_dir, name, allowed_exts={".md"}, must_exist=True)
                    if not p or _is_reparse_or_link(p):
                        continue
                    card = _safe_atom_card(p, rt)
                    if card is None:
                        continue
                    if q_terms:
                        haystack = set(
                            tokenize(
                                f"{card.get('filename', '')} {card.get('claim', '')} {card.get('evidence', '')} {' '.join(card.get('contexts') or [])}"  # type: ignore[arg-type]
                            )
                        )
                        if not (q_terms & haystack):
                            continue
                    if proj_filter:
                        if is_owner:
                            if not project_tags_match(tuple(card.get("contexts") or ()), (proj_filter,)):  # type: ignore[arg-type]
                                continue
                        elif not _tags_strictly_in_project_scope(tuple(card.get("contexts") or ()), proj_filter):  # type: ignore[arg-type]
                            continue
                    if not is_owner:
                        if not card["verified"]:
                            continue
                        if card.get("epistemic_label") == "preference" or bool(card.get("epistemic_contract")):
                            continue
                        if card.get("temporal_status") == "invalid_window" or (
                            card.get("temporal_status") != "active" and not inc_exp_list
                        ):
                            continue
                    matching.append(card)

            page = matching[offset : offset + limit]
            return send(
                200,
                {
                    "atoms": page,
                    "items": page,
                    "count": len(page),
                    "total": len(matching),
                    "offset": offset,
                    "limit": limit,
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
            inc_exp_q = (q.get("include_expired") or ["0"])[0] in {"1", "true", "yes"}
            clean_as_of_q: str | None = None
            as_of_q = ((q.get("as_of") or [None])[0] or None)
            if as_of_q is not None and str(as_of_q).strip():
                if len(str(as_of_q)) > 64:
                    raise ValueError("Invalid as_of query filter.")
                clean_as_of_q = str(as_of_q).strip()
                evaluate_temporal_validity("", "", as_of=clean_as_of_q)

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
                tv_get = evaluate_temporal_validity(
                    rec.get("valid_from"),
                    rec.get("valid_until"),
                    as_of=clean_as_of_q,
                )
                if tv_get["status"] == "invalid_window" or (not tv_get["active"] and not inc_exp_q):
                    return send(404, {"error": "Atom not found."})
                card = _atom_card(atom_path, rt, as_of=clean_as_of_q)
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
            card = _atom_card(atom_path, rt, as_of=clean_as_of_q)
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
                edited_md = _apply_temporal_overrides_to_markdown(
                    _str(body, md_key, max_len=MAX_NOTE_BYTES),
                    body,
                )
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
                if not any(k in body for k in ("claim", "why_it_matters", "contexts", "memory_type", "epistemic_label", "valid_from", "valid_until")):
                    raise ValueError("Atom edit requires 'edited_content'/'content' or at least one structured field ('claim', 'why_it_matters', 'contexts', 'memory_type', 'epistemic_label', 'valid_from', 'valid_until').")
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

        if (
            path.startswith("/v1/atoms/")
            and method == "POST"
            and not path.endswith("/cold-archive")
            and not (path.startswith("/v1/atoms/cold-archive/") and path.endswith("/restore"))
        ):
            if not owner_only():
                return True
            rest = unquote(path[len("/v1/atoms/"):])
            filename, _, action = rest.rpartition("/")
            if action == "validity":
                norm_fname = normalize_markdown_filename(filename)
                atom_path = safe_file_in_dir(rt.atoms_dir, norm_fname, allowed_exts={".md"}, must_exist=True)
                if not atom_path or _is_reparse_or_link(atom_path):
                    return send(404, {"error": "Atom not found."})
                record = set_atom_temporal_bounds(
                    atom_path,
                    valid_from=_str(body, "valid_from", max_len=64, required=False).strip() or None,
                    valid_until=_str(body, "valid_until", max_len=64, required=False).strip() or None,
                    expected_sha256=_str(body, "expected_sha256", max_len=64),
                    actor=_str(body, "actor", max_len=120),
                    rationale=_str(body, "rationale", max_len=500, required=False),
                    brain_dir=rt.brain_dir,
                    atoms_dir=rt.atoms_dir,
                )
                _sync_lexical_after_atom_mutation(rt, [norm_fname])
                card = _atom_card(atom_path, rt)
                return send(
                    200,
                    {
                        "status": "validity_updated",
                        "valid_from": record.get("valid_from", card.get("valid_from", "")),
                        "valid_until": record.get("valid_until", card.get("valid_until", "")),
                        "temporal_status": record.get("temporal_status", card.get("temporal_status", "active")),
                        "approved_current": bool(record.get("approved_current", card.get("approved_current"))),
                        "atom": card,
                        "decision": record,
                    },
                )
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
            from skill_playbooks import _claim_grounding_units, inspect_skill_candidates

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
                units = list(_claim_grounding_units(claim_txt)) if claim_txt else []
                cands.append({**cand, "claim": claim_txt, "grounding_units": units})
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
                hid: {
                    **asdict(rule),
                    "allowed_projects": list(rule.allowed_projects),
                    "is_builtin": hid in getattr(host_adapters, "BUILTIN_HOST_IDS", ()),
                }
                for hid, rule in sorted(policies.items())
            }
            return send(200, serialized)

        if path == "/v1/hosts" and method == "POST":
            if not owner_only():
                return True
            import host_adapters
            from recall import normalize_project_tag

            hid = _str(body, "host_id", max_len=48, required=True).strip().lower()
            prov = _str(body, "provider", max_len=32, required=True).strip()
            transp = _str(body, "transport", max_len=16, required=True).strip()
            ep = _str(body, "endpoint_url", max_len=500, required=True).strip()
            mod = _str(body, "default_model", max_len=128, required=True).strip()
            key_env = _str(body, "api_key_env", max_len=64, required=False).strip() or None
            raw_projs = (
                _str_list(body, "allowed_projects", max_items=32, max_item_len=120)
                if body.get("allowed_projects") is not None
                else ["*"]
            )
            projs = tuple("*" if p == "*" else normalize_project_tag(p) for p in raw_projs if p)
            created = host_adapters.create_custom_host_policy(
                hid,
                provider=prov,
                transport=transp,
                endpoint_url=ep,
                default_model=mod,
                api_key_env=key_env,
                allowed_projects=projs or ("*",),
                allow_unscoped_atoms=bool(body.get("allow_unscoped_atoms", False)),
                allow_preference_atoms=bool(body.get("allow_preference_atoms", False)),
                redact_pii=body.get("redact_pii") if "redact_pii" in body else None,
                fail_on_secret_detection=(
                    body.get("fail_on_secret_detection") if "fail_on_secret_detection" in body else None
                ),
                enabled=bool(body.get("enabled", False)),
                policy_path=rt.policy_path,
            )
            return send(
                201,
                {
                    **asdict(created),
                    "allowed_projects": list(created.allowed_projects),
                    "is_builtin": False,
                },
            )

        if path.startswith("/v1/hosts/") and method == "DELETE":
            if not owner_only():
                return True
            host_id = unquote(path[len("/v1/hosts/"):]).strip().lower()
            import host_adapters

            deleted = host_adapters.delete_custom_host_policy(host_id, policy_path=rt.policy_path)
            if not deleted:
                return send(404, {"error": f"Custom host '{host_id}' not found."})
            return send(200, {"deleted": host_id})

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
            if cur.provider == "ollama" and cur.transport == "local" and raw_model.strip().lower() == "auto":
                from hardware_profile import resolve_active_local_generation_model

                raw_model = resolve_active_local_generation_model(fallback=_config.GEN_MODEL)
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

        if path == "/v1/import/file" and method == "POST":
            if not owner_only():
                return True
            from bulk_import import import_file

            file_path = _str(body, "path", max_len=1000)
            res = import_file(file_path, tags=tuple(_tags(body)), paths=paths)
            return send(201, res)

        # ── Cryptographic Vault Merkle Ledger ──
        if path == "/v1/ledger/verify" and method == "GET":
            from vault_ledger import verify_vault_ledger

            return send(200, verify_vault_ledger(brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir, playbooks_dir=rt.playbooks_dir))

        if path == "/v1/ledger/history" and method == "GET":
            from vault_ledger import verify_vault_ledger

            limit = _parse_bounded_int(q, "limit", default=100, min_val=1, max_val=500)
            offset = _parse_bounded_int(q, "offset", default=0, min_val=0, max_val=1_000_000)
            led = verify_vault_ledger(brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir, playbooks_dir=rt.playbooks_dir)
            entries = list(reversed(list(led.get("entries") or [])))
            page = entries[offset : offset + limit]
            return send(
                200,
                {
                    "total": len(entries),
                    "count": len(page),
                    "offset": offset,
                    "limit": limit,
                    "has_more": offset + len(page) < len(entries),
                    "entries": page,
                    "items": page,
                },
            )

        if path == "/v1/ledger/leaves" and method == "GET":
            from vault_ledger import compute_vault_merkle

            limit = _parse_bounded_int(q, "limit", default=200, min_val=1, max_val=500)
            offset = _parse_bounded_int(q, "offset", default=0, min_val=0, max_val=1_000_000)
            merkle = compute_vault_merkle(
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
                playbooks_dir=rt.playbooks_dir,
            )
            leaves = list(merkle.get("leaves") or [])
            if not is_owner:
                pref_rels = {
                    f"2-atoms/{name}"
                    for name in (os.listdir(rt.atoms_dir) if os.path.isdir(rt.atoms_dir) else [])
                    if is_content_markdown(name) and _is_preference_atom_file(os.path.join(rt.atoms_dir, name))
                }
                leaves = [lf for lf in leaves if lf.get("rel_path") not in pref_rels]
            page = leaves[offset : offset + limit]
            return send(
                200,
                {
                    "total": len(leaves),
                    "count": len(page),
                    "offset": offset,
                    "limit": limit,
                    "has_more": offset + len(page) < len(leaves),
                    "items": page,
                    "leaves": page,
                },
            )

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
            import vault_ledger

            raw_leaf = unquote(path[len("/v1/ledger/prove/"):]).strip().replace("\\", "/").lstrip("/")
            parts = [p for p in raw_leaf.split("/") if p]
            if not parts or len(parts) > 2 or any(p in {".", ".."} or ":" in p or "\x00" in p for p in parts):
                if not is_owner:
                    return send(404, {"error": "Verified atom proof not found."})
                return send(404, {"error": f"Leaf not found: {raw_leaf}"})

            prove_leaf_fn = getattr(vault_ledger, "prove_leaf_membership", None)
            try:
                if callable(prove_leaf_fn):
                    proof = prove_leaf_fn(
                        raw_leaf,
                        brain_dir=rt.brain_dir,
                        atoms_dir=rt.atoms_dir,
                        playbooks_dir=rt.playbooks_dir,
                    )
                else:
                    proof = _prove_leaf_fallback(
                        raw_leaf,
                        brain_dir=rt.brain_dir,
                        atoms_dir=rt.atoms_dir,
                        playbooks_dir=rt.playbooks_dir,
                    )
            except (FileNotFoundError, PermissionError, ValueError):
                if not is_owner:
                    return send(404, {"error": "Verified atom proof not found."})
                raise

            if not is_owner:
                rel_p = str(proof.get("rel_path") or "")
                if rel_p.startswith("2-atoms/"):
                    atom_fname = os.path.basename(rel_p)
                    live_sha = _live_approved_atom_sha(atom_fname, rt)
                    if live_sha is None or live_sha != proof.get("file_sha256"):
                        return send(404, {"error": "Verified atom proof not found."})
                elif rel_p.startswith("5-playbooks/"):
                    pb_fname = os.path.basename(rel_p)
                    pb_rep = _live_active_playbook_report(pb_fname, rt)
                    if pb_rep is None or pb_rep.sha256 != proof.get("file_sha256"):
                        return send(404, {"error": "Verified atom proof not found."})
                elif rel_p.startswith("6-skills/"):
                    from executable_skills import verify_executable_skill_integrity

                    sk_fname = os.path.basename(rel_p)
                    sk_dir = getattr(rt, "skills_dir", None) or os.path.join(rt.brain_dir, "6-skills")
                    sk_rep = verify_executable_skill_integrity(
                        sk_fname,
                        brain_dir=rt.brain_dir,
                        atoms_dir=rt.atoms_dir,
                        playbooks_dir=rt.playbooks_dir,
                        skills_dir=sk_dir,
                        telemetry_db_path=rt.telemetry_db_path,
                        security_dir=getattr(rt, "security_dir", None),
                    )
                    if not (sk_rep.valid and sk_rep.status == "active" and sk_rep.sha256 == proof.get("file_sha256")):
                        return send(404, {"error": "Verified atom proof not found."})
            return send(200, proof)

        # ── Contradiction & Drift Radar + Knowledge Graph ──
        if path == "/v1/graph/contradictions" and method == "GET":
            from knowledge_graph import detect_contradictions

            proj = ((q.get("project") or [None])[0] or None)
            if proj and len(proj) > 120:
                raise ValueError("Invalid project query filter.")
            inc_pending = bool(is_owner and ((q.get("include_pending") or ["1"])[0] != "0"))
            raw_nli_mode = str((q.get("nli_mode") or ["llm"])[0] or "llm").strip().lower()
            if raw_nli_mode not in {"llm", "fast", "hybrid"}:
                raise ValueError("nli_mode must be one of ('llm', 'fast', 'hybrid').")
            enable_nli = bool(is_owner and (((q.get("nli") or ["0"])[0] in {"1", "true", "yes"}) or raw_nli_mode != "llm"))
            dc_params = inspect.signature(detect_contradictions).parameters
            dc_kwargs: dict[str, object] = {
                "brain_dir": rt.brain_dir,
                "atoms_dir": rt.atoms_dir,
                "project_scope": proj,
                "include_pending": inc_pending,
            }
            if "include_preferences" in dc_params:
                dc_kwargs["include_preferences"] = bool(is_owner)
            if "nli_mode" in dc_params and is_owner:
                dc_kwargs["nli_mode"] = raw_nli_mode
            if "enable_nli" in dc_params:
                dc_kwargs["enable_nli"] = enable_nli
                dc_kwargs["policy_path"] = rt.policy_path
                dc_kwargs["http_transport"] = rt.http_transport
                dc_kwargs["skip_dns_check_for_mock_transport"] = rt.skip_dns_check_for_mock_transport
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
                    drifts = [
                        dr
                        for dr in (radar.get("drift_alerts") or [])
                        if isinstance(dr, dict) and dr.get("filename") not in pref_files
                    ]
                    expired_list = [
                        ex
                        for ex in (radar.get("expired_atoms") or [])
                        if isinstance(ex, dict) and ex.get("filename") not in pref_files
                    ]
                    radar = {
                        **radar,
                        "alerts": alerts,
                        "contradiction_count": len(alerts),
                        "high_severity_count": sum(1 for al in alerts if al.get("severity") == "high"),
                        "orphan_approved_atoms": orphans,
                        "orphan_approved_count": len(orphans),
                        "drift_alerts": drifts,
                        "drift_count": len(drifts),
                        "expired_atoms": expired_list,
                        "expired_count": len(expired_list),
                    }
            return send(200, radar)

        if path == "/v1/graph/summary" and method == "GET":
            from knowledge_graph import build_knowledge_graph
            from memory_format import normalize_temporal_bound_str

            proj = ((q.get("project") or [None])[0] or None)
            if proj and len(proj) > 120:
                raise ValueError("Invalid project query filter.")
            raw_as_of = str((q.get("as_of") or [""])[0] or "").strip()
            as_of_val = normalize_temporal_bound_str(raw_as_of) if raw_as_of else None
            if "include_expired" in q:
                inc_expired = str((q.get("include_expired") or ["1"])[0]).strip().lower() in {"1", "true", "yes"}
            else:
                inc_expired = False if as_of_val is not None else True
            bg_sig = inspect.signature(build_knowledge_graph).parameters
            bg_kwargs: dict[str, object] = {
                "brain_dir": rt.brain_dir,
                "atoms_dir": rt.atoms_dir,
                "project_scope": proj,
                "include_pending": False,
            }
            if "include_preferences" in bg_sig:
                bg_kwargs["include_preferences"] = bool(is_owner)
            if "as_of" in bg_sig:
                bg_kwargs["as_of"] = as_of_val
            if "include_expired" in bg_sig:
                bg_kwargs["include_expired"] = inc_expired
            res = build_knowledge_graph(**bg_kwargs)
            timeline_source_nodes = list(res.get("nodes") or [])
            if is_owner and not inc_expired and "include_expired" in bg_sig:
                all_res = build_knowledge_graph(**{**bg_kwargs, "include_expired": True})
                timeline_source_nodes = list(all_res.get("nodes") or [])
            timeline_dates_set: set[str] = set()
            for n_item in timeline_source_nodes:
                for b_field in ("valid_from", "valid_until"):
                    b_raw = str(n_item.get(b_field) or "").strip()
                    if len(b_raw) >= 10:
                        timeline_dates_set.add(b_raw[:10])
            if timeline_dates_set:
                timeline_dates_set.add(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
            summary_payload = {k: v for k, v in res.items() if k not in {"nodes", "edges"}}
            summary_payload["include_expired"] = inc_expired
            summary_payload["timeline_dates"] = sorted(timeline_dates_set)
            return send(200, summary_payload)

        if path in {"/v1/graph/full", "/v1/graph"} and method == "GET":
            from knowledge_graph import build_knowledge_graph
            from memory_format import normalize_temporal_bound_str

            proj = ((q.get("project") or [None])[0] or None)
            if proj and len(proj) > 120:
                raise ValueError("Invalid project query filter.")
            limit = _parse_bounded_int(q, "limit", default=300, min_val=1, max_val=1000)
            q_inc_pending = (q.get("include_pending") or ["0"])[0] in {"1", "true", "yes"}
            raw_as_of = str((q.get("as_of") or [""])[0] or "").strip()
            as_of_val = normalize_temporal_bound_str(raw_as_of) if raw_as_of else None
            if "include_expired" in q:
                inc_expired = str((q.get("include_expired") or ["1"])[0]).strip().lower() in {"1", "true", "yes"}
            else:
                inc_expired = False if as_of_val is not None else True
            bg_sig_full = inspect.signature(build_knowledge_graph).parameters
            bg_kwargs_full: dict[str, object] = {
                "brain_dir": rt.brain_dir,
                "atoms_dir": rt.atoms_dir,
                "project_scope": proj,
                "include_pending": bool(is_owner and q_inc_pending),
            }
            if "include_preferences" in bg_sig_full:
                bg_kwargs_full["include_preferences"] = bool(is_owner)
            if "as_of" in bg_sig_full:
                bg_kwargs_full["as_of"] = as_of_val
            if "include_expired" in bg_sig_full:
                bg_kwargs_full["include_expired"] = inc_expired
            res = build_knowledge_graph(**bg_kwargs_full)
            nodes_raw = list(res.get("nodes") or [])[:limit]
            timeline_source_nodes = list(res.get("nodes") or []) if is_owner else nodes_raw
            if is_owner and not inc_expired and "include_expired" in bg_sig_full:
                all_res = build_knowledge_graph(**{**bg_kwargs_full, "include_expired": True})
                timeline_source_nodes = list(all_res.get("nodes") or [])
            timeline_dates_set_full: set[str] = set()
            for n_item in timeline_source_nodes:
                for b_field in ("valid_from", "valid_until"):
                    b_raw = str(n_item.get(b_field) or "").strip()
                    if len(b_raw) >= 10:
                        timeline_dates_set_full.add(b_raw[:10])
            if timeline_dates_set_full:
                timeline_dates_set_full.add(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
            kept_ids = {str(n.get("id") or n.get("filename") or "") for n in nodes_raw}
            nodes_norm = [
                {
                    "id": str(n.get("id") or n.get("filename") or ""),
                    "filename": str(n.get("filename") or n.get("id") or ""),
                    "label": str(n.get("claim") or n.get("label") or n.get("filename") or "")[:140],
                    "claim": str(n.get("claim") or ""),
                    "review_status": str(n.get("review_status") or "pending"),
                    "effective_status": str(n.get("effective_status") or n.get("review_status") or "pending"),
                    "memory_type": str(n.get("memory_type") or "semantic"),
                    "epistemic_label": str(n.get("epistemic_label") or "claim"),
                    "contexts": list(n.get("contexts") or []),
                    "verified": bool(n.get("approved_current", n.get("verified", False))),
                    "valid_from": str(n.get("valid_from") or ""),
                    "valid_until": str(n.get("valid_until") or ""),
                    "temporal_status": str(n.get("temporal_status") or "unbounded"),
                    "superseded_by": str(n.get("superseded_by") or ""),
                }
                for n in nodes_raw
            ]
            edges_norm = [
                {
                    "from": str(e.get("from") or e.get("source") or ""),
                    "to": str(e.get("to") or e.get("target") or ""),
                    "kind": str(e.get("kind") or e.get("relation") or "link"),
                }
                for e in (res.get("edges") or [])
                if str(e.get("from") or e.get("source") or "") in kept_ids
                and str(e.get("to") or e.get("target") or "") in kept_ids
            ]
            return send(
                200,
                {
                    **res,
                    "nodes": nodes_norm,
                    "edges": edges_norm,
                    "node_count": len(nodes_norm),
                    "edge_count": len(edges_norm),
                    "include_expired": inc_expired,
                    "timeline_dates": sorted(timeline_dates_set_full),
                },
            )

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

        if path.startswith("/v1/graph/suggestions/") and method == "GET":
            from knowledge_graph import suggest_atom_graph_links
            from recall import project_tags_match

            raw_target = unquote(path[len("/v1/graph/suggestions/") :]).strip()
            if not raw_target or "/" in raw_target or "\\" in raw_target or len(raw_target) > 200:
                return send(404, {"error": "Atom not found in graph."})
            proj = ((q.get("project") or [None])[0] or None)
            if proj and len(proj) > 120:
                raise ValueError("Invalid project query filter.")
            norm_t = normalize_markdown_filename(raw_target)
            if not is_owner:
                if _live_approved_atom_sha(norm_t, rt) is None:
                    return send(404, {"error": "Atom not found in graph."})
                t_path = safe_file_in_dir(rt.atoms_dir, norm_t, allowed_exts={".md"}, must_exist=True)
                if not t_path or _is_preference_atom_file(t_path):
                    return send(404, {"error": "Atom not found in graph."})
                t_rec = parse_atom_record(t_path)
                t_projs = [
                    str(c).strip()
                    for c in (t_rec.get("contexts") or [])
                    if str(c).strip().lower().startswith("project:")
                ]
                if proj:
                    if not t_projs or not all(project_tags_match((tp,), (proj,)) for tp in t_projs):
                        return send(404, {"error": "Atom not found in graph."})
                elif t_projs:
                    return send(404, {"error": "Atom not found in graph."})
            q_inc_pending = bool(is_owner and (q.get("include_pending") or ["0"])[0] in {"1", "true", "yes"})
            sug = suggest_atom_graph_links(
                raw_target,
                project_scope=proj,
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
                include_pending=q_inc_pending,
            )
            return send(200, sug)

        if path == "/v1/graph/auto-link" and method == "POST":
            if not owner_only():
                return True
            from knowledge_graph import auto_link_vault_atoms

            actor = _str(body, "actor", default="owner", max_len=120, required=False).strip() or "owner"
            proj = _str(body, "project_scope", max_len=120, required=False).strip() or None
            dry_run = bool(body.get("dry_run", False))
            inc_pending = bool(body.get("include_pending", False))
            max_links = _parse_json_int(
                body,
                "max_links_per_atom",
                default=4,
                min_val=1,
                max_val=10,
                clamp=True,
            )
            assert max_links is not None
            res = auto_link_vault_atoms(
                actor=actor,
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
                project_scope=proj,
                include_pending=inc_pending,
                dry_run=dry_run,
                max_links_per_atom=max_links,
            )
            return send(200, res)

        # ── Source Anchor Audit (owner) ──
        if path == "/v1/audit/sources" and method == "GET":
            if not owner_only():
                return True
            from audit_sources import audit_sources

            reports = audit_sources(atoms_dir=rt.atoms_dir, brain_dir=rt.brain_dir, demote_stale=False)
            stale = [
                r
                for r in reports
                if r.get("stale_approval")
                or r.get("action") in {"review_required", "would_demote", "demoted", "conflict_or_failure"}
            ]
            return send(
                200,
                {
                    "count": len(reports),
                    "stale_count": len(stale),
                    "stale": stale,
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
            demoted = [r for r in reports if r.get("demoted") or r.get("action") == "demoted"]
            if demoted:
                _sync_lexical_after_atom_mutation(
                    rt,
                    [str(r.get("atom_id")) for r in demoted if r.get("atom_id")],
                )
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
                def _diff_entry_is_pref(entry: dict) -> bool:
                    return (
                        entry.get("epistemic_label") == "preference"
                        or bool(entry.get("epistemic_contract"))
                        or _is_live_preference_atom(entry.get("filename"), rt)
                    )

                def _redact_atom_diff_entry(entry: dict) -> dict:
                    live_sha = _live_approved_atom_sha(entry.get("filename"), rt)
                    if live_sha is not None and live_sha == entry.get("file_sha256"):
                        return entry
                    return {
                        **entry,
                        "claim": "",
                        "evidence": "",
                        "contexts": [],
                        "valid_from": "",
                        "valid_until": "",
                        "redacted_revoked": True,
                    }

                added = [
                    _redact_atom_diff_entry(a)
                    for a in (res.get("added_atoms") or [])
                    if not _diff_entry_is_pref(a)
                ]
                removed = [
                    _redact_atom_diff_entry(a)
                    for a in (res.get("removed_atoms") or [])
                    if not _diff_entry_is_pref(a)
                ]
                modified = []
                for m in res.get("modified_atoms") or []:
                    if (
                        m.get("before_epistemic_label") == "preference"
                        or m.get("after_epistemic_label") == "preference"
                        or bool(m.get("before_epistemic_contract"))
                        or bool(m.get("after_epistemic_contract"))
                        or _is_live_preference_atom(m.get("filename"), rt)
                    ):
                        continue
                    live_sha = _live_approved_atom_sha(m.get("filename"), rt)
                    new_is_live = live_sha is not None and live_sha == m.get("new_sha256")
                    redacted_m = {
                        **m,
                        "old_claim": "",
                        "before_claim": "",
                        "old_evidence": "",
                        "before_evidence": "",
                        "old_contexts": [],
                        "old_valid_from": "",
                        "old_valid_until": "",
                        "redacted_revoked": True,
                    }
                    if not new_is_live:
                        redacted_m["new_claim"] = ""
                        redacted_m["after_claim"] = ""
                        redacted_m["new_evidence"] = ""
                        redacted_m["after_evidence"] = ""
                        redacted_m["new_contexts"] = []
                        redacted_m["new_valid_from"] = ""
                        redacted_m["new_valid_until"] = ""
                    modified.append(redacted_m)

                raw_pb_changes = dict(res.get("playbook_changes") or {})
                pb_added = []
                for item in raw_pb_changes.get("added") or []:
                    pb_rep = _live_active_playbook_report(item.get("filename"), rt)
                    expected_sha = item.get("new_sha256") or item.get("old_sha256")
                    if not pb_rep or pb_rep.sha256 != expected_sha:
                        pb_added.append({**item, "steps": [], "backing_atoms": [], "title": "", "redacted_revoked": True})
                    else:
                        pb_added.append(item)

                pb_removed = []
                for item in raw_pb_changes.get("removed") or []:
                    pb_rep = _live_active_playbook_report(item.get("filename"), rt)
                    expected_sha = item.get("new_sha256") or item.get("old_sha256")
                    if not pb_rep or pb_rep.sha256 != expected_sha:
                        pb_removed.append({**item, "steps": [], "backing_atoms": [], "title": "", "redacted_revoked": True})
                    else:
                        pb_removed.append(item)

                pb_modified = []
                for item in raw_pb_changes.get("modified") or []:
                    pb_rep = _live_active_playbook_report(item.get("filename"), rt)
                    pb_is_live = bool(pb_rep and pb_rep.sha256 == item.get("new_sha256"))
                    redacted_pb = {
                        **item,
                        "old_steps": [],
                        "old_backing_atoms": [],
                        "title": pb_rep.title if pb_is_live else "",
                        "redacted_revoked": True,
                    }
                    if not pb_is_live:
                        redacted_pb["new_steps"] = []
                        redacted_pb["new_backing_atoms"] = []
                    pb_modified.append(redacted_pb)

                raw_sk_changes = dict(res.get("skill_changes") or {})
                redacted_sk_changes: dict[str, list[dict[str, object]]] = {}
                for sk_bucket in ("added", "removed", "modified"):
                    redacted_sk_changes[sk_bucket] = [
                        {**item, "title": "", "redacted_revoked": True}
                        for item in (raw_sk_changes.get(sk_bucket) or [])
                    ]

                prev_summary = dict(res.get("summary") or {})
                res = {
                    **res,
                    "added_atoms": added,
                    "removed_atoms": removed,
                    "modified_atoms": modified,
                    "playbook_changes": {
                        "added": pb_added,
                        "removed": pb_removed,
                        "modified": pb_modified,
                    },
                    "skill_changes": redacted_sk_changes,
                    "summary": {
                        **prev_summary,
                        "added": len(added),
                        "removed": len(removed),
                        "modified": len(modified),
                    },
                }
            return send(200, res)

        if path == "/v1/snapshots/recall" and method == "POST":
            from skill_playbooks import playbook_visible_in_scope
            from time_travel import recall_at_snapshot

            limit_key = "top_k" if "top_k" in body else "limit"
            top_k = _parse_json_int(
                body,
                limit_key,
                default=5,
                min_val=1,
                max_val=25,
                clamp=True,
            )
            assert top_k is not None
            proj_scope = _str(body, "project_scope", max_len=120, required=False).strip() or None
            as_of_str = _str(body, "as_of", max_len=64, required=False).strip() or None
            inc_expired = bool(body.get("include_expired", False))
            enable_rerank = bool(body.get("enable_reranker", True))
            inc_prefs = bool(is_owner and body.get("include_preferences", True))
            ras_params = inspect.signature(recall_at_snapshot).parameters
            ras_kwargs: dict[str, object] = {
                "project_scope": proj_scope,
                "brain_dir": rt.brain_dir,
            }
            if "include_preferences" in ras_params:
                ras_kwargs["include_preferences"] = inc_prefs
            if "as_of" in ras_params:
                ras_kwargs["as_of"] = as_of_str
            if "include_expired" in ras_params:
                ras_kwargs["include_expired"] = inc_expired
            if "enable_reranker" in ras_params:
                ras_kwargs["enable_reranker"] = enable_rerank
            if "top_k" in ras_params:
                ras_kwargs["top_k"] = top_k if is_owner else min(25, max(top_k * 3, 15))
            res = recall_at_snapshot(
                _str(body, "query", max_len=1000),
                _str(body, "snapshot_id", max_len=120),
                **ras_kwargs,
            )
            if not is_owner:
                filtered_results = []
                for hit in res.get("results") or []:
                    if hit.get("kind", "atom") == "playbook":
                        pb_rep = _live_active_playbook_report(hit.get("filename"), rt)
                        if pb_rep is None or pb_rep.sha256 != hit.get("file_sha256"):
                            continue
                        if proj_scope and not playbook_visible_in_scope(pb_rep, (proj_scope,)):
                            continue
                        filtered_results.append(hit)
                        continue
                    if not _tags_strictly_in_project_scope(list(hit.get("contexts") or []), proj_scope):
                        continue
                    live_sha = _live_approved_atom_sha(hit.get("filename"), rt)
                    if live_sha is not None and live_sha == hit.get("file_sha256"):
                        filtered_results.append(hit)
                res = {**res, "results": filtered_results[:top_k]}
            return send(200, res)

        if path == "/v1/snapshots/verify" and method == "POST":
            from time_travel import verify_snapshot

            sid = _str(body, "snapshot_id", max_len=120)
            return send(200, verify_snapshot(sid, brain_dir=rt.brain_dir))

        # ── Vault Section Browsers (threads, experiences, briefings — owner) ──
        if path.startswith("/v1/vault/") and method == "GET":
            if not owner_only():
                return True
            section_map = {
                "threads": os.path.join(rt.brain_dir, "3-threads"),
                "experiences": rt.experiences_dir,
                "briefings": os.path.join(rt.brain_dir, "briefings"),
            }
            sub = path[len("/v1/vault/") :].strip("/")
            parts = [unquote(p) for p in sub.split("/") if p]
            if not parts or parts[0] not in section_map:
                return send(404, {"error": "Unknown vault section."})
            sec_dir = section_map[parts[0]]
            if len(parts) == 1:
                limit = _parse_bounded_int(q, "limit", default=100, min_val=1, max_val=500)
                offset = _parse_bounded_int(q, "offset", default=0, min_val=0, max_val=1_000_000)
                rows: list[dict[str, object]] = []
                if os.path.isdir(sec_dir) and not _is_reparse_or_link(sec_dir):
                    for name in sorted(os.listdir(sec_dir), reverse=True):
                        if not is_content_markdown(name):
                            continue
                        fp = safe_file_in_dir(sec_dir, name, allowed_exts={".md"}, must_exist=True)
                        if not fp or _is_reparse_or_link(fp):
                            continue
                        try:
                            st = os.stat(fp)
                            head = Path(fp).read_text(encoding="utf-8", errors="replace")[:600]
                        except OSError:
                            continue
                        title_m = re.search(r"(?m)^#\s+(.+)$", head)
                        title = title_m.group(1).strip() if title_m else os.path.splitext(name)[0]
                        rows.append(
                            {
                                "filename": name,
                                "title": title[:160],
                                "size_bytes": int(st.st_size),
                                "modified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
                            }
                        )
                page = rows[offset : offset + limit]
                return send(
                    200,
                    {
                        "section": parts[0],
                        "total": len(rows),
                        "count": len(page),
                        "offset": offset,
                        "limit": limit,
                        "has_more": offset + len(page) < len(rows),
                        "items": page,
                    },
                )
            if len(parts) == 2:
                norm_name = normalize_markdown_filename(parts[1])
                fp = safe_file_in_dir(sec_dir, norm_name, allowed_exts={".md"}, must_exist=True)
                if not fp or _is_reparse_or_link(fp):
                    return send(404, {"error": "Vault item not found."})
                raw_txt = Path(fp).read_text(encoding="utf-8", errors="replace")
                return send(
                    200,
                    {
                        "section": parts[0],
                        "filename": norm_name,
                        "content": raw_txt,
                        "sha256": content_sha256(raw_txt.encode("utf-8")),
                    },
                )
            return send(404, {"error": "Vault item not found."})

        # ── Web Scout Reading Queue & Operator Jobs (owner) ──
        if path == "/v1/scout/queue" and method == "GET":
            if not owner_only():
                return True
            rlist = Path(rt.brain_dir) / "0-raw" / "reading-list.md"
            items: list[dict[str, str]] = []
            if rlist.is_file() and not _is_reparse_or_link(str(rlist)):
                for raw_line in rlist.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = raw_line.strip()
                    m = re.match(r"^-\s*\[( |x|X|feed|failed|blocked)\]\s+(https?://\S+)", line)
                    if m:
                        mark, u = m.group(1).lower(), m.group(2)
                        status_lbl = "queued" if mark == " " else ("completed" if mark == "x" else mark)
                        items.append({"url": u, "status": status_lbl, "kind": "feed" if mark == "feed" else "url"})
            return send(200, {"total": len(items), "count": len(items), "items": items})

        if path == "/v1/scout/queue" and method == "POST":
            if not owner_only():
                return True
            raw_url = _str(body, "url", max_len=1000).strip()
            is_feed = bool(body.get("is_feed"))
            if not re.match(r"^https?://[^\s<>\"']+$", raw_url):
                raise ValueError("Scout URL must start with http:// or https:// and contain no whitespace.")
            raw_dir = Path(rt.brain_dir) / "0-raw"
            if _is_reparse_or_link(str(raw_dir)):
                raise PermissionError("Refusing to write reading-list.md under symlinked 0-raw directory.")
            raw_dir.mkdir(parents=True, exist_ok=True)
            rlist = raw_dir / "reading-list.md"
            if rlist.exists() and _is_reparse_or_link(str(rlist)):
                raise PermissionError("Refusing to write symlinked reading-list.md.")
            existing_txt = rlist.read_text(encoding="utf-8", errors="replace") if rlist.is_file() else "# Reading List\n\n"
            marker = "[feed]" if is_feed else "[ ]"
            new_line = f"- {marker} {raw_url}\n"
            if not existing_txt.endswith("\n"):
                existing_txt += "\n"
            atomic_write_bytes(str(rlist), (existing_txt + new_line).encode("utf-8"))
            return send(201, {"status": "queued", "url": raw_url, "kind": "feed" if is_feed else "url"})

        if path == "/v1/scout/run" and method == "POST":
            if not owner_only():
                return True
            return send(202, _start_operator_job("scout", "ollama_scout.py", [], brain_dir=rt.brain_dir))

        if path in {"/v1/jobs/refinery", "/v1/jobs/editor", "/v1/jobs/doctor"} and method == "POST":
            if not owner_only():
                return True
            kind = path.rsplit("/", 1)[-1]
            script_map = {
                "refinery": "ollama_refinery.py",
                "editor": "ollama_editor.py",
                "doctor": "doctor.py",
            }
            return send(202, _start_operator_job(kind, script_map[kind], [], brain_dir=rt.brain_dir))

        if path.startswith("/v1/jobs/") and method == "GET":
            if not owner_only():
                return True
            jid = unquote(path[len("/v1/jobs/") :]).strip()
            with _JOBS_LOCK:
                rec = dict(_JOBS[jid]) if jid in _JOBS else None
            if rec is None:
                return send(404, {"error": "Job not found."})
            return send(200, rec)

        # ── Zero-Knowledge E2EE Multi-Device Vault Sync (owner) ──
        if path == "/v1/sync/status" and method == "GET":
            if not owner_only():
                return True
            return send(
                200,
                sync_status(
                    brain_dir=rt.brain_dir,
                    state_dir=getattr(rt, "sync_state_dir", None),
                    http_transport=getattr(rt, "http_transport", None),
                    skip_dns_check_for_mock_transport=bool(getattr(rt, "skip_dns_check_for_mock_transport", False)),
                ),
            )

        if path == "/v1/sync/conflicts" and method == "GET":
            if not owner_only():
                return True
            items = list_sync_conflicts(brain_dir=rt.brain_dir)
            return send(200, {"count": len(items), "conflicts": items})

        if path == "/v1/sync/conflicts/resolve" and method == "POST":
            if not owner_only():
                return True
            cid = _str(body, "conflict_id", max_len=300, required=False).strip() or _str(body, "rel_path", max_len=300).strip()
            resolution = _str(body, "resolution", max_len=32).strip()
            merged_content = (
                _str(body, "merged_content", max_len=MAX_NOTE_BYTES, required=False)
                if "merged_content" in body and body.get("merged_content") is not None
                else None
            )
            exp_sha = _str(body, "expected_local_sha256", max_len=64, required=False).strip() or None
            actor = _str(body, "actor", default="owner", max_len=120, required=False) or "owner"
            res = resolve_sync_conflict(
                cid,
                resolution=resolution,
                merged_content=merged_content,
                expected_local_sha256=exp_sha,
                actor=actor,
                brain_dir=rt.brain_dir,
            )
            return send(200, res)

        if path.startswith("/v1/sync/conflicts/") and method == "GET":
            if not owner_only():
                return True
            cid = unquote(path[len("/v1/sync/conflicts/") :]).strip()
            return send(200, get_sync_conflict_detail(cid, brain_dir=rt.brain_dir))

        if path == "/v1/sync/auto" and method == "GET":
            if not owner_only():
                return True
            return send(
                200,
                get_auto_sync_status(
                    brain_dir=rt.brain_dir,
                    state_dir=getattr(rt, "sync_state_dir", None),
                ),
            )

        if path == "/v1/sync/auto" and method == "POST":
            if not owner_only():
                return True
            if bool(body.get("run_once", False)):
                return send(
                    200,
                    run_auto_sync_cycle(
                        brain_dir=rt.brain_dir,
                        state_dir=getattr(rt, "sync_state_dir", None),
                        http_transport=getattr(rt, "http_transport", None),
                        skip_dns_check_for_mock_transport=bool(getattr(rt, "skip_dns_check_for_mock_transport", False)),
                    ),
                )
            enabled = bool(body.get("enabled", False))
            iv = _parse_json_int(
                body,
                "interval_seconds",
                default=300,
                min_val=5,
                max_val=86400,
                clamp=True,
            )
            assert iv is not None
            start_bg = bool(body.get("start_background", True))
            return send(
                200,
                configure_auto_sync(
                    enabled=enabled,
                    interval_seconds=iv,
                    start_background=start_bg,
                    brain_dir=rt.brain_dir,
                    state_dir=getattr(rt, "sync_state_dir", None),
                    http_transport=getattr(rt, "http_transport", None),
                    skip_dns_check_for_mock_transport=bool(getattr(rt, "skip_dns_check_for_mock_transport", False)),
                ),
            )

        if path == "/v1/sync/gc" and method == "POST":
            if not owner_only():
                return True
            passphrase = body.get("passphrase")
            if passphrase is not None and not isinstance(passphrase, str):
                raise ValueError("'passphrase' must be a string.")
            if isinstance(passphrase, str) and not passphrase:
                passphrase = None
            dry_run = bool(body.get("dry_run", False))
            res = sync_gc(
                brain_dir=rt.brain_dir,
                passphrase=passphrase,
                dry_run=dry_run,
                state_dir=getattr(rt, "sync_state_dir", None),
                http_transport=getattr(rt, "http_transport", None),
                skip_dns_check_for_mock_transport=bool(getattr(rt, "skip_dns_check_for_mock_transport", False)),
            )
            return send(200, res)

        if path == "/v1/sync/seal-keychain" and method == "POST":
            if not owner_only():
                return True
            passphrase = body.get("passphrase")
            if passphrase is not None and not isinstance(passphrase, str):
                raise ValueError("'passphrase' must be a string.")
            if isinstance(passphrase, str) and not passphrase:
                passphrase = None
            res = migrate_sync_key_to_keychain(
                brain_dir=rt.brain_dir,
                passphrase=passphrase,
                state_dir=getattr(rt, "sync_state_dir", None),
            )
            return send(200, res)

        if path == "/v1/sync/init" and method == "POST":
            if not owner_only():
                return True
            target = _str(body, "target", max_len=1000).strip()
            backend_type = _str(body, "backend_type", default="local_folder", max_len=32, required=False).strip() or "local_folder"
            device_name = _str(body, "device_name", max_len=80, required=False).strip() or None
            passphrase = body.get("passphrase")
            if passphrase is not None and not isinstance(passphrase, str):
                raise ValueError("'passphrase' must be a string.")
            if isinstance(passphrase, str) and not passphrase:
                passphrase = None
            kdf_mode = _str(body, "kdf_mode", max_len=32, required=False).strip() or None
            use_keychain = bool(body.get("use_keychain", False))
            auth_mode = _str(body, "auth_mode", default="bearer", max_len=32, required=False).strip() or "bearer"
            bearer_token_env = _str(body, "bearer_token_env", max_len=80, required=False).strip() or None
            s3_region = _str(body, "s3_region", default="auto", max_len=64, required=False).strip() or "auto"
            s3_access_key_env = _str(body, "s3_access_key_env", max_len=80, required=False).strip() or None
            s3_secret_key_env = _str(body, "s3_secret_key_env", max_len=80, required=False).strip() or None
            s3_session_token_env = _str(body, "s3_session_token_env", max_len=80, required=False).strip() or None
            res = init_sync(
                target,
                backend_type=backend_type,
                brain_dir=rt.brain_dir,
                device_name=device_name,
                passphrase=passphrase,
                kdf_mode=kdf_mode,
                use_keychain=use_keychain,
                bearer_token_env=bearer_token_env,
                auth_mode=auth_mode,
                s3_region=s3_region,
                s3_access_key_env=s3_access_key_env,
                s3_secret_key_env=s3_secret_key_env,
                s3_session_token_env=s3_session_token_env,
                state_dir=getattr(rt, "sync_state_dir", None),
                http_transport=getattr(rt, "http_transport", None),
                skip_dns_check_for_mock_transport=bool(getattr(rt, "skip_dns_check_for_mock_transport", False)),
            )
            return send(201, res)

        if path == "/v1/sync/join" and method == "POST":
            if not owner_only():
                return True
            target = _str(body, "target", max_len=1000).strip()
            backend_type = _str(body, "backend_type", default="local_folder", max_len=32, required=False).strip() or "local_folder"
            device_name = _str(body, "device_name", max_len=80, required=False).strip() or None
            passphrase = body.get("passphrase")
            if passphrase is not None and not isinstance(passphrase, str):
                raise ValueError("'passphrase' must be a string.")
            if isinstance(passphrase, str) and not passphrase:
                passphrase = None
            recovery_key_b64 = _str(body, "recovery_key_b64", max_len=256, required=False).strip() or None
            kdf_mode = _str(body, "kdf_mode", max_len=32, required=False).strip() or None
            use_keychain = bool(body.get("use_keychain", False))
            auth_mode = _str(body, "auth_mode", default="bearer", max_len=32, required=False).strip() or "bearer"
            bearer_token_env = _str(body, "bearer_token_env", max_len=80, required=False).strip() or None
            s3_region = _str(body, "s3_region", default="auto", max_len=64, required=False).strip() or "auto"
            s3_access_key_env = _str(body, "s3_access_key_env", max_len=80, required=False).strip() or None
            s3_secret_key_env = _str(body, "s3_secret_key_env", max_len=80, required=False).strip() or None
            s3_session_token_env = _str(body, "s3_session_token_env", max_len=80, required=False).strip() or None
            res = join_sync(
                target,
                backend_type=backend_type,
                brain_dir=rt.brain_dir,
                device_name=device_name,
                passphrase=passphrase,
                recovery_key_b64=recovery_key_b64,
                kdf_mode=kdf_mode,
                use_keychain=use_keychain,
                bearer_token_env=bearer_token_env,
                auth_mode=auth_mode,
                s3_region=s3_region,
                s3_access_key_env=s3_access_key_env,
                s3_secret_key_env=s3_secret_key_env,
                s3_session_token_env=s3_session_token_env,
                state_dir=getattr(rt, "sync_state_dir", None),
                http_transport=getattr(rt, "http_transport", None),
                skip_dns_check_for_mock_transport=bool(getattr(rt, "skip_dns_check_for_mock_transport", False)),
            )
            return send(200, res)

        if path == "/v1/sync/push" and method == "POST":
            if not owner_only():
                return True
            passphrase = body.get("passphrase")
            if passphrase is not None and not isinstance(passphrase, str):
                raise ValueError("'passphrase' must be a string.")
            if isinstance(passphrase, str) and not passphrase:
                passphrase = None
            res = sync_push(
                brain_dir=rt.brain_dir,
                passphrase=passphrase,
                state_dir=getattr(rt, "sync_state_dir", None),
                http_transport=getattr(rt, "http_transport", None),
                skip_dns_check_for_mock_transport=bool(getattr(rt, "skip_dns_check_for_mock_transport", False)),
            )
            return send(200, res)

        if path == "/v1/sync/pull" and method == "POST":
            if not owner_only():
                return True
            passphrase = body.get("passphrase")
            if passphrase is not None and not isinstance(passphrase, str):
                raise ValueError("'passphrase' must be a string.")
            if isinstance(passphrase, str) and not passphrase:
                passphrase = None
            trust_new_peers = bool(body.get("trust_new_peers", True))
            res = sync_pull(
                brain_dir=rt.brain_dir,
                passphrase=passphrase,
                trust_new_peers=trust_new_peers,
                state_dir=getattr(rt, "sync_state_dir", None),
                http_transport=getattr(rt, "http_transport", None),
                skip_dns_check_for_mock_transport=bool(getattr(rt, "skip_dns_check_for_mock_transport", False)),
            )
            return send(200, res)

        if path == "/v1/sync/verify" and method == "POST":
            if not owner_only():
                return True
            passphrase = body.get("passphrase")
            if passphrase is not None and not isinstance(passphrase, str):
                raise ValueError("'passphrase' must be a string.")
            if isinstance(passphrase, str) and not passphrase:
                passphrase = None
            res = sync_verify(
                brain_dir=rt.brain_dir,
                passphrase=passphrase,
                state_dir=getattr(rt, "sync_state_dir", None),
                http_transport=getattr(rt, "http_transport", None),
                skip_dns_check_for_mock_transport=bool(getattr(rt, "skip_dns_check_for_mock_transport", False)),
            )
            return send(200, res)

        # ── Zero-Knowledge Mobile Inbox Drop (owner-only) ──
        _drop_state_dir = getattr(rt, "sync_state_dir", None) or getattr(rt, "security_dir", None)
        if path == "/v1/sync/inbox-drop/pairings" and method == "GET":
            if not owner_only():
                return True
            sync_st = sync_status(
                brain_dir=rt.brain_dir,
                state_dir=_drop_state_dir,
                http_transport=getattr(rt, "http_transport", None),
                skip_dns_check_for_mock_transport=bool(getattr(rt, "skip_dns_check_for_mock_transport", False)),
            )
            vid = str(sync_st.get("vault_id") or "") or None
            return send(
                200,
                list_mobile_drop_pairings(
                    vault_id=vid,
                    brain_dir=rt.brain_dir,
                    state_dir=_drop_state_dir,
                ),
            )

        if path == "/v1/sync/inbox-drop/pair" and method == "POST":
            if not owner_only():
                return True
            sync_st = sync_status(
                brain_dir=rt.brain_dir,
                state_dir=_drop_state_dir,
                http_transport=getattr(rt, "http_transport", None),
                skip_dns_check_for_mock_transport=bool(getattr(rt, "skip_dns_check_for_mock_transport", False)),
            )
            if not sync_st.get("configured") or not sync_st.get("vault_id"):
                raise InboxDropError("Initialize E2EE vault sync first before creating a Mobile Inbox Drop pairing.")
            label = (
                _str(body, "label", max_len=80, required=False).strip()
                or _str(body, "device_label", max_len=80, required=False).strip()
                or "Mobile Quick-Capture"
            )
            pwa_url = _str(body, "pwa_base_url", default="https://localhost/inbox-drop/", max_len=300, required=False) or "https://localhost/inbox-drop/"
            ak_id, sk_id = resolve_scoped_inbox_drop_s3_credentials(
                s3_access_key_id=_str(body, "s3_access_key_id", max_len=160, required=False).strip(),
                s3_secret_access_key=_str(body, "s3_secret_access_key", max_len=160, required=False).strip(),
                include_from_env=bool(body.get("include_s3_credentials")),
            )
            res = create_mobile_drop_pairing(
                label=label,
                vault_id=str(sync_st["vault_id"]),
                sync_target_url=str(sync_st.get("target") or "") if sync_st.get("backend_type") == "http_object" else "",
                s3_region=str(sync_st.get("s3_region") or "auto"),
                s3_access_key_id=ak_id,
                s3_secret_access_key=sk_id,
                pwa_base_url=pwa_url,
                include_recall=bool(body.get("include_recall", False)),
                brain_dir=rt.brain_dir,
                state_dir=_drop_state_dir,
            )
            res["device_label"] = res.get("label", label)
            return send(201, res)

        if path == "/v1/sync/inbox-drop/export-recall" and method == "POST":
            if not owner_only():
                return True
            from inbox_drop import export_mobile_recall_index

            res = export_mobile_recall_index(
                brain_dir=rt.brain_dir,
                state_dir=_drop_state_dir,
                include_pending_review=bool(body.get("include_pending_review", True)),
                http_transport=getattr(rt, "http_transport", None),
                skip_dns_check_for_mock_transport=bool(getattr(rt, "skip_dns_check_for_mock_transport", False)),
            )
            return send(200, res)

        if path == "/v1/sync/inbox-drop/rotate-recall" and method == "POST":
            if not owner_only():
                return True
            from inbox_drop import export_mobile_recall_index, rotate_mobile_recall_key

            opt_vid = _str(body, "vault_id", max_len=64, required=False).strip() or None
            rot = rotate_mobile_recall_key(
                vault_id=opt_vid,
                brain_dir=rt.brain_dir,
                state_dir=_drop_state_dir,
            )
            if bool(body.get("reexport", True)):
                exp = export_mobile_recall_index(
                    brain_dir=rt.brain_dir,
                    state_dir=_drop_state_dir,
                    http_transport=getattr(rt, "http_transport", None),
                    skip_dns_check_for_mock_transport=bool(getattr(rt, "skip_dns_check_for_mock_transport", False)),
                )
                rot = {**rot, "reexported_object_key": exp["object_key"], "atom_count": exp["atom_count"]}
            return send(200, rot)

        if path == "/v1/sync/inbox-drop/pull" and method == "POST":
            if not owner_only():
                return True
            res = pull_mobile_inbox_drops(
                brain_dir=rt.brain_dir,
                state_dir=_drop_state_dir,
                http_transport=getattr(rt, "http_transport", None),
                skip_dns_check_for_mock_transport=bool(getattr(rt, "skip_dns_check_for_mock_transport", False)),
            )
            return send(200, res)

        if path.startswith("/v1/sync/inbox-drop/") and method == "DELETE":
            if not owner_only():
                return True
            tid = unquote(path[len("/v1/sync/inbox-drop/") :]).strip()
            res = revoke_mobile_drop_pairing(
                tid,
                brain_dir=rt.brain_dir,
                state_dir=_drop_state_dir,
            )
            return send(200, res)

        # ── Local-First Browser Web Clipper (scoped token or owner-only on /ping,/status,/clip; owner-only on pairings) ──
        sec_dir = getattr(rt, "security_dir", None)
        if path in {"/v1/clipper/ping", "/v1/clipper/status", "/v1/clipper/clip", "/v1/clipper/capture-media"}:
            pairing = getattr(handler, "_clipper_pairing", None)
            if pairing is None and not is_owner:
                return send(
                    403,
                    {
                        "error": "403 Forbidden: browser clipper endpoints require a paired scoped clipper_* token or owner authorization."
                    },
                )
        if path == "/v1/clipper/ping" and method == "GET":
            return send(
                200,
                {
                    "ok": True,
                    "service": "second-brain-clipper",
                    "version": "1.0.0",
                    "active_vault": os.path.basename(os.path.abspath(rt.brain_dir)),
                },
            )

        if path == "/v1/clipper/status" and method == "GET":
            pairing = getattr(handler, "_clipper_pairing", None)
            return send(
                200,
                {
                    "ok": True,
                    "paired": bool(pairing),
                    "pairing_id": pairing.get("pairing_id") if isinstance(pairing, dict) else None,
                    "label": pairing.get("label") if isinstance(pairing, dict) else None,
                    "origin": pairing.get("origin") if isinstance(pairing, dict) else None,
                    "active_vault": os.path.basename(os.path.abspath(rt.brain_dir)),
                },
            )

        if path == "/v1/clipper/clip" and method == "POST":
            client_ip = handler.client_address[0] if getattr(handler, "client_address", None) else "127.0.0.1"
            if not check_capture_rate_limit(client_ip):
                return send(
                    429,
                    {"error": "Too Many Requests: capture write rate limit exceeded (max 180 per minute)."},
                )
            pairing = getattr(handler, "_clipper_pairing", None)
            src_label = (
                f"clipper:{pairing.get('pairing_id')}"
                if isinstance(pairing, dict) and pairing.get("pairing_id")
                else "browser-clipper"
            )
            raw_tags = body.get("tags", [])
            if isinstance(raw_tags, str):
                tags_val: list[str] | str = raw_tags[:1000]
            elif isinstance(raw_tags, list):
                tags_val = [str(t)[:64] for t in raw_tags[:32]]
            else:
                raise ClipperError("'tags' must be a list of strings or comma-separated string.")
            res = ingest_web_clip(
                url=_str(body, "url", max_len=2048),
                title=_str(body, "title", max_len=240, required=False),
                selected_quote=_str(body, "selected_quote", max_len=32_000, required=False),
                page_text=_str(body, "page_text", max_len=200_000, required=False),
                raw_html=_str(body, "raw_html", max_len=500_000, required=False),
                tags=tags_val,
                destination=_str(body, "destination", default="note", max_len=16, required=False) or "note",
                capture_source=src_label,
                brain_dir=rt.brain_dir,
            )
            return send(201, res)

        if path == "/v1/clipper/capture-media" and method == "POST":
            client_ip = handler.client_address[0] if getattr(handler, "client_address", None) else "127.0.0.1"
            if not check_capture_rate_limit(client_ip):
                return send(
                    429,
                    {"error": "Too Many Requests: capture write rate limit exceeded (max 180 per minute)."},
                )
            pairing = getattr(handler, "_clipper_pairing", None)
            src_label = (
                f"clipper:{pairing.get('pairing_id')}"
                if isinstance(pairing, dict) and pairing.get("pairing_id")
                else "browser-clipper"
            )
            raw_tags = body.get("tags", [])
            if isinstance(raw_tags, str):
                tags_val = raw_tags[:1000]
            elif isinstance(raw_tags, list):
                tags_val = [str(t)[:64] for t in raw_tags[:32]]
            else:
                raise ClipperError("'tags' must be a list of strings or comma-separated string.")
            b64_data = body.get("content_base64")
            if not isinstance(b64_data, str) or not b64_data.strip():
                raise ClipperError("'content_base64' is required for /v1/clipper/capture-media.")
            res = ingest_clipper_media_bytes(
                url=_str(body, "url", max_len=2048),
                title=_str(body, "title", max_len=240, required=False),
                kind=_str(body, "kind", max_len=32),
                content_base64=b64_data,
                tags=tags_val,
                destination=_str(body, "destination", default="note", max_len=16, required=False) or "note",
                capture_source=src_label,
                brain_dir=rt.brain_dir,
            )
            return send(201, res)

        if path == "/v1/clipper/pairings" and method == "GET":
            if not owner_only():
                return True
            items = list_clipper_pairings(security_dir=sec_dir)
            return send(200, {"count": len(items), "pairings": items, "items": items})

        if path == "/v1/clipper/pair-code" and method == "POST":
            if not owner_only():
                return True
            label = _str(body, "label", default="Browser Clipper", max_len=80, required=False) or "Browser Clipper"
            ttl = _parse_json_int(
                body,
                "ttl_seconds",
                default=300,
                min_val=30,
                max_val=900,
                clamp=True,
                error_cls=ClipperError,
            )
            assert ttl is not None
            srv_addr = getattr(getattr(handler, "server", None), "server_address", None)
            bound_port = int(srv_addr[1]) if isinstance(srv_addr, (tuple, list)) and len(srv_addr) >= 2 else 0
            res = create_clipper_pairing_code(
                label=label,
                ttl_seconds=ttl,
                port=(bound_port if 1 <= bound_port <= 65535 else None),
                security_dir=sec_dir,
            )
            return send(201, res)

        if path == "/v1/clipper/redeem" and method == "POST":
            client_ip = handler.client_address[0] if getattr(handler, "client_address", None) else "127.0.0.1"
            code = _str(body, "code", max_len=32)
            origin = _str(body, "origin", max_len=200)
            hdr_origin = (handler.headers.get("Origin") or "").strip().lower().rstrip("/")
            bound_port = handler.server.server_address[1]
            label = _str(body, "label", default="", max_len=80, required=False).strip() or None
            try:
                if hdr_origin and hdr_origin != f"http://127.0.0.1:{bound_port}":
                    if hdr_origin != origin.strip().lower().rstrip("/"):
                        raise ClipperSecurityError("Extension Origin header does not match requested pairing origin.")
                res = redeem_clipper_pairing_code(code, origin, label=label, security_dir=sec_dir)
            except ClipperSecurityError as exc:
                rec_redeem_fn = getattr(_api_mod, "_record_clipper_redeem_failure", None)
                if callable(rec_redeem_fn):
                    fail_code = rec_redeem_fn(client_ip, hdr_origin)
                    if fail_code == 429:
                        return send(429, {"error": "Too Many Requests: pairing code attempt limit exceeded."})
                return send(403, {"error": str(exc)})
            return send(201, res)

        if path == "/v1/clipper/pair" and method == "POST":
            if not owner_only():
                return True
            origin = _str(body, "origin", max_len=200)
            label = _str(body, "label", default="Browser Clipper", max_len=80, required=False) or "Browser Clipper"
            res = create_clipper_pairing(origin, label=label, security_dir=sec_dir)
            return send(201, res)

        if path.startswith("/v1/clipper/pair/") and method == "DELETE":
            if not owner_only():
                return True
            pid = unquote(path[len("/v1/clipper/pair/") :]).strip()
            res = revoke_clipper_pairing(pid, security_dir=sec_dir)
            return send(200, res)

        # ── Multi-Vault Workspaces & Opt-In Federated Cross-Vault Recall (owner-only) ──
        if path == "/v1/vaults" and method == "GET":
            if not owner_only():
                return True
            return send(200, list_vaults(security_dir=sec_dir, current_brain_dir=rt.brain_dir))

        if path == "/v1/vaults" and method == "POST":
            if not owner_only():
                return True
            label = _str(body, "label", max_len=80)
            vpath = _str(body, "path", max_len=1000)
            vid = _str(body, "vault_id", max_len=64, required=False).strip() or None
            host_policy = _str(body, "default_host_policy", default="local_only", max_len=32, required=False) or "local_only"
            activate = bool(body.get("activate", False))
            res = create_vault(
                label,
                vpath,
                vault_id=vid,
                isolated_token=bool(body.get("isolated_token", False)),
                default_host_policy=host_policy,
                federated_searchable=bool(body.get("federated_searchable", False)),
                activate=activate,
                security_dir=sec_dir,
                current_brain_dir=rt.brain_dir,
            )
            if activate and hasattr(rt, "switch_brain_dir"):
                rt.switch_brain_dir(str(res["path"]))
            return send(201, res)

        if path == "/v1/vaults/switch" and method == "POST":
            if not owner_only():
                return True
            vid = _str(body, "vault_id", max_len=64)
            res = switch_vault(vid, security_dir=sec_dir, current_brain_dir=rt.brain_dir)
            if hasattr(rt, "switch_brain_dir"):
                rt.switch_brain_dir(str(res["active_vault_path"]))
            return send(200, res)

        if path == "/v1/vaults/federated-recall" and method == "POST":
            if not owner_only():
                return True
            q_str = _str(body, "query", max_len=2000)
            raw_vids = body.get("vault_ids")
            if raw_vids is not None:
                if not isinstance(raw_vids, list):
                    raise VaultWorkspaceError("'vault_ids' must be a list of vault IDs.")
                vids_arg: list[str] | None = [str(x).strip() for x in raw_vids if str(x).strip()]
            else:
                vids_arg = None
            lim = _parse_json_int(
                body,
                "limit",
                default=5,
                min_val=1,
                max_val=25,
                clamp=True,
                error_cls=VaultWorkspaceError,
            )
            assert lim is not None
            proj = _str(body, "project_scope", max_len=120, required=False).strip() or None
            ret_mode = _str(body, "retrieval_mode", default="lexical", max_len=16, required=False).strip() or "lexical"
            res = federated_recall(
                q_str,
                vault_ids=vids_arg,
                project_scope=proj,
                retrieval_mode=ret_mode,
                limit=lim,
                include_expired=bool(body.get("include_expired", False)),
                as_of=_str(body, "as_of", max_len=64, required=False).strip() or None,
                security_dir=sec_dir,
                current_brain_dir=rt.brain_dir,
            )
            return send(200, res)

        if path.startswith("/v1/vaults/") and method == "PATCH":
            if not owner_only():
                return True
            vid = unquote(path[len("/v1/vaults/") :]).strip()
            lbl = _str(body, "label", max_len=80, required=False) if "label" in body else None
            pol = _str(body, "default_host_policy", max_len=32, required=False) if "default_host_policy" in body else None
            fed = bool(body["federated_searchable"]) if "federated_searchable" in body else None
            res = update_vault_policy(
                vid,
                label=lbl,
                default_host_policy=pol,
                federated_searchable=fed,
                security_dir=sec_dir,
                current_brain_dir=rt.brain_dir,
            )
            return send(200, res)

        if path.startswith("/v1/vaults/") and method == "DELETE":
            if not owner_only():
                return True
            vid = unquote(path[len("/v1/vaults/") :]).strip()
            res = remove_vault(vid, security_dir=sec_dir, current_brain_dir=rt.brain_dir)
            return send(200, res)

        # ── SHA-256-Pinned Verified Local ONNX Weights Manager (owner-only) ──
        if path == "/v1/models/onnx" and method == "GET":
            if not owner_only():
                return True
            return send(200, get_onnx_models_status(state_dir=sec_dir, run_inference=False))

        if path == "/v1/models/onnx/verify" and method == "POST":
            if not owner_only():
                return True
            return send(200, get_onnx_models_status(state_dir=sec_dir, run_inference=True))

        if path == "/v1/models/onnx/pull" and method == "POST":
            if not owner_only():
                return True
            target = (
                _str(body, "target", max_len=32, required=False).strip()
                or _str(body, "slot", max_len=32, required=False).strip()
                or "all"
            )
            preset = _str(body, "preset", default="default", max_len=32, required=False) or "default"
            force = bool(body.get("force", False))
            res = pull_onnx_models(
                target,
                preset=preset,
                force=force,
                state_dir=sec_dir,
                http_transport=getattr(rt, "http_transport", None),
                skip_dns_check_for_mock_transport=bool(getattr(rt, "skip_dns_check_for_mock_transport", False)),
            )
            return send(200, res)

        if path.startswith("/v1/models/onnx/") and method == "DELETE":
            if not owner_only():
                return True
            slot_target = unquote(path[len("/v1/models/onnx/") :]).strip()
            res = remove_onnx_models(slot_target, state_dir=sec_dir)
            return send(200, res)

        # ── Multimodal Media Upload (Audio, Screenshots/Images, Documents — owner-only) ──
        if path == "/v1/ingest/media" and method == "POST":
            if not owner_only():
                return True
            import base64 as _b64
            from ingest_multimodal import MAX_API_MEDIA_UPLOAD_BYTES, ingest_uploaded_media_bytes

            fname = _str(body, "filename", max_len=255, required=True).strip()
            raw_media = body.get("_raw_media_bytes")
            if isinstance(raw_media, (bytes, bytearray)):
                media_bytes = bytes(raw_media)
            else:
                b64_raw = body.get("content_base64")
                if not isinstance(b64_raw, str) or not b64_raw.strip():
                    raise ValueError("'content_base64' (or binary body) is required for /v1/ingest/media.")
                # Strip optional data URL prefix (e.g. data:image/png;base64,...)
                clean_b64 = b64_raw.strip()
                if clean_b64.startswith("data:") and "," in clean_b64[:128]:
                    clean_b64 = clean_b64.split(",", 1)[1].strip()
                try:
                    media_bytes = _b64.b64decode(clean_b64, validate=True)
                except Exception as exc:
                    raise ValueError(f"Invalid base64 media payload: {exc}") from exc
            if len(media_bytes) > MAX_API_MEDIA_UPLOAD_BYTES:
                return send(
                    413,
                    {"error": f"Payload Too Large: uploaded media ({len(media_bytes)} bytes) exceeds {MAX_API_MEDIA_UPLOAD_BYTES} bytes."},
                )
            res = ingest_uploaded_media_bytes(media_bytes, fname, brain_dir=rt.brain_dir)
            return send(201, res)

        if path == "/v1/ingest/video-file" and method == "POST":
            if not owner_only():
                return True
            from ingest_multimodal import ingest_local_video_file

            video_fp = _str(body, "filepath", max_len=1000, required=True).strip()
            res = ingest_local_video_file(video_fp, brain_dir=rt.brain_dir)
            return send(201, res)

        # ── Git Post-Commit Hook & Coding Session Distiller (owner-only) ──
        if path == "/v1/git/hook" and method == "GET":
            if not owner_only():
                return True
            default_repo = str(Path(__file__).resolve().parent.parent)
            repo_q = ((q.get("repo") or [""])[0] or "").strip()[:1000] or default_repo
            return send(200, get_git_hook_status(repo_q))

        if path == "/v1/git/hook/install" and method == "POST":
            if not owner_only():
                return True
            default_repo = str(Path(__file__).resolve().parent.parent)
            repo_p = _str(body, "repo", default=default_repo, max_len=1000, required=False).strip() or default_repo
            proj_p = _str(body, "project_scope", max_len=120, required=False).strip() or None
            return send(
                200,
                install_git_post_commit_hook(repo_dir=repo_p, brain_dir=rt.brain_dir, project_scope=proj_p),
            )

        if path == "/v1/git/hook/uninstall" and method == "POST":
            if not owner_only():
                return True
            default_repo = str(Path(__file__).resolve().parent.parent)
            repo_p = _str(body, "repo", default=default_repo, max_len=1000, required=False).strip() or default_repo
            return send(200, uninstall_git_post_commit_hook(repo_dir=repo_p))

        if path == "/v1/git/distill" and method == "POST":
            if not owner_only():
                return True
            default_repo = str(Path(__file__).resolve().parent.parent)
            repo_p = _str(body, "repo", default=default_repo, max_len=1000, required=False).strip() or default_repo
            proj_p = _str(body, "project_scope", max_len=120, required=False).strip() or None
            commit_p = _str(body, "commit_sha", max_len=64, required=False).strip() or None
            mc_val = _parse_json_int(
                body,
                "max_commits",
                default=5,
                min_val=1,
                max_val=25,
                clamp=True,
            )
            assert mc_val is not None
            stage_ib = bool(body.get("stage_inbox", True))
            force_d = bool(body.get("force", False))
            return send(
                200,
                distill_git_commits(
                    repo_dir=repo_p,
                    brain_dir=rt.brain_dir,
                    project_scope=proj_p,
                    max_commits=mc_val,
                    commit_sha=commit_p,
                    stage_inbox=stage_ib,
                    force=force_d,
                ),
            )

        # ── Watchdog Daemon Status & One-Shot Sync/Index Pass (owner-only) ──
        if path in {"/v1/daemon/status", "/v1/system"} and method == "GET":
            if not owner_only():
                return True
            from index_manifest import index_manifest_is_current, load_index_manifest
            from lexical_store import lexical_index_is_current

            lex_ok = lexical_index_is_current(rt.atoms_dir, rt.lexical_index_path)
            manifest = load_index_manifest(rt.chroma_path) if os.path.isdir(rt.chroma_path) else None
            dense_ok = bool(manifest and index_manifest_is_current(manifest, rt.atoms_dir))
            raw_dir = Path(rt.brain_dir) / "0-raw"
            inbox_dir = raw_dir / "inbox"
            raw_q_cnt = (
                sum(1 for f in os.listdir(raw_dir) if is_content_markdown(f))
                if (raw_dir.is_dir() and not _is_reparse_or_link(str(raw_dir)))
                else 0
            )
            inbox_q_cnt = (
                sum(1 for f in os.listdir(inbox_dir) if is_content_markdown(f))
                if (inbox_dir.is_dir() and not _is_reparse_or_link(str(inbox_dir)))
                else 0
            )
            stale_summary: dict[str, object] | None = None
            for stale_name in ("stale-anchors.json", "stale-source-anchors.json"):
                stale_file = Path(rt.brain_dir) / ".sidecar" / stale_name
                if stale_file.is_file() and not _is_reparse_or_link(str(stale_file)):
                    try:
                        stale_summary = json.loads(stale_file.read_text(encoding="utf-8"))
                        break
                    except Exception:
                        stale_summary = None
            auto_st = get_auto_sync_status(
                brain_dir=rt.brain_dir,
                state_dir=getattr(rt, "sync_state_dir", None),
            )
            return send(
                200,
                {
                    "status": "ok",
                    "brain_dir": rt.brain_dir,
                    "lexical_index_current": lex_ok,
                    "dense_index_current": dense_ok,
                    "dense_generation_id": manifest.get("generation_id") if isinstance(manifest, dict) else None,
                    "raw_queue_count": raw_q_cnt,
                    "inbox_quarantine_count": inbox_q_cnt,
                    "auto_sync": auto_st,
                    "stale_anchor_report": stale_summary,
                },
            )

        if path == "/v1/daemon/sync-once" and method == "POST":
            if not owner_only():
                return True
            from watchdog_daemon import sync_notes_and_atoms_once

            notes_d = os.path.join(rt.brain_dir, "notes")
            sync_res = sync_notes_and_atoms_once(
                notes_dir=notes_d,
                atoms_dir=rt.atoms_dir,
                lexical_index_path=rt.lexical_index_path,
                brain_dir=rt.brain_dir,
            )
            auto_cycle = None
            if bool(body.get("auto_sync", False)):
                auto_cycle = run_auto_sync_cycle(
                    brain_dir=rt.brain_dir,
                    state_dir=getattr(rt, "sync_state_dir", None),
                    http_transport=getattr(rt, "http_transport", None),
                    skip_dns_check_for_mock_transport=bool(getattr(rt, "skip_dns_check_for_mock_transport", False)),
                )
            return send(200, {"status": "synced", "daemon_sync": sync_res, "auto_sync_cycle": auto_cycle})

        # ── Portable Three-Store .sbvault Backup & Disaster-Recovery Restore (owner-only) ──
        if path == "/v1/vault/backup/create" and method == "POST":
            if not owner_only():
                return True
            out_p = _str(body, "output_path", max_len=1000, required=True).strip()
            pw = body.get("passphrase")
            if pw is not None and not isinstance(pw, str):
                raise ValueError("'passphrase' must be a string.")
            if isinstance(pw, str) and not pw:
                pw = None
            unenc = bool(body.get("unencrypted", False))
            return send(
                201,
                create_vault_backup(out_p, brain_dir=rt.brain_dir, passphrase=pw, unencrypted=unenc),
            )

        if path == "/v1/vault/backup/verify" and method == "POST":
            if not owner_only():
                return True
            b_p = _str(body, "backup_path", max_len=1000, required=True).strip()
            pw = body.get("passphrase")
            if pw is not None and not isinstance(pw, str):
                raise ValueError("'passphrase' must be a string.")
            if isinstance(pw, str) and not pw:
                pw = None
            return send(200, verify_vault_backup(b_p, passphrase=pw))

        if path == "/v1/vault/backup/restore" and method == "POST":
            if not owner_only():
                return True
            b_p = _str(body, "backup_path", max_len=1000, required=True).strip()
            t_p = _str(body, "target_brain_dir", max_len=1000, required=True).strip()
            pw = body.get("passphrase")
            if pw is not None and not isinstance(pw, str):
                raise ValueError("'passphrase' must be a string.")
            if isinstance(pw, str) and not pw:
                pw = None
            force_r = bool(body.get("force", False))
            return send(200, restore_vault_backup(b_p, t_p, passphrase=pw, force=force_r))

        # ── Stage 4 Executable Skills (Sandboxed Runtime, Kill-Switch & Audit Trail) ──
        if path == "/v1/skills" and method == "GET":
            from executable_skills import (
                get_skill_kill_switch_status,
                list_executable_skills,
                skill_visible_in_scope,
            )

            proj = ((q.get("project") or [None])[0] or None)
            if proj and len(proj) > 120:
                raise ValueError("Invalid project query filter.")
            inc_inactive = bool(is_owner and ((q.get("include_inactive") or ["0"])[0] in {"1", "true", "yes"}))
            sk_dir = getattr(rt, "skills_dir", None) or os.path.join(rt.brain_dir, "6-skills")
            sec_dir = getattr(rt, "security_dir", None)
            skills = list_executable_skills(
                skills_dir=sk_dir,
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
                playbooks_dir=rt.playbooks_dir,
                telemetry_db_path=rt.telemetry_db_path,
                security_dir=sec_dir,
                active_only=not inc_inactive,
            )
            if proj:
                skills = [s for s in skills if skill_visible_in_scope(s, (proj,))]
            ks_status = get_skill_kill_switch_status(brain_dir=rt.brain_dir, security_dir=sec_dir)
            return send(
                200,
                {
                    "count": len(skills),
                    "kill_switch": ks_status if is_owner else {"global_engaged": bool(ks_status.get("global_engaged"))},
                    "skills": [s.as_dict() for s in skills],
                },
            )

        if path == "/v1/skills/candidates" and method == "GET":
            if not owner_only():
                return True
            from executable_skills import inspect_executable_skill_candidates

            sk_dir = getattr(rt, "skills_dir", None) or os.path.join(rt.brain_dir, "6-skills")
            sec_dir = getattr(rt, "security_dir", None)
            cands = inspect_executable_skill_candidates(
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
                playbooks_dir=rt.playbooks_dir,
                skills_dir=sk_dir,
                experiences_dir=rt.experiences_dir,
                telemetry_db_path=rt.telemetry_db_path,
                security_dir=sec_dir,
            )
            return send(200, {"count": len(cands), "candidates": cands})

        if path == "/v1/skills/executions" and method == "GET":
            if not owner_only():
                return True
            from executable_skills import list_skill_executions, verify_skill_execution_audit_chain

            skill_filter = ((q.get("skill_id") or [None])[0] or None)
            limit_val = _parse_bounded_int(q, "limit", default=50, min_val=1, max_val=500)
            execs = list_skill_executions(brain_dir=rt.brain_dir, skill_id=skill_filter, limit=limit_val)
            chain = verify_skill_execution_audit_chain(brain_dir=rt.brain_dir)
            return send(200, {"count": len(execs), "audit_chain": chain, "executions": execs})

        if path == "/v1/skills/promote" and method == "POST":
            if not owner_only():
                return True
            from executable_skills import promote_playbook_to_executable_skill

            sk_dir = getattr(rt, "skills_dir", None) or os.path.join(rt.brain_dir, "6-skills")
            sec_dir = getattr(rt, "security_dir", None)
            raw_actions = body.get("step_actions")
            if not isinstance(raw_actions, list) or not raw_actions:
                raise ValueError("'step_actions' must be a non-empty list of sandboxed action objects.")
            raw_schema = body.get("input_schema")
            if raw_schema is not None and not isinstance(raw_schema, dict):
                raise ValueError("'input_schema' must be an object mapping parameter names to definitions.")
            exp_sk = _str(body, "expected_skill_sha256", max_len=64, required=False).strip() or None
            timeout_s = _parse_json_float(body, "timeout_seconds", default=15.0)
            max_out_b = _parse_json_int(body, "max_output_bytes", default=65536)
            assert max_out_b is not None
            res = promote_playbook_to_executable_skill(
                skill_id=_str(body, "skill_id", max_len=64),
                playbook_id=_str(body, "playbook_id", max_len=64),
                expected_playbook_sha256=_str(body, "expected_playbook_sha256", max_len=64),
                step_actions=raw_actions,
                input_schema=raw_schema,
                timeout_seconds=timeout_s,
                max_output_bytes=max_out_b,
                actor=_str(body, "actor", max_len=120),
                rationale=_str(body, "rationale", max_len=500, required=False),
                allow_early_owner_override=bool(body.get("allow_early_owner_override", False)),
                expected_skill_sha256=exp_sk,
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
                playbooks_dir=rt.playbooks_dir,
                skills_dir=sk_dir,
                experiences_dir=rt.experiences_dir,
                telemetry_db_path=rt.telemetry_db_path,
                security_dir=sec_dir,
            )
            return send(201, res)

        if path == "/v1/skills/kill-switch" and method == "POST":
            if not owner_only():
                return True
            from executable_skills import set_skill_kill_switch

            sec_dir = getattr(rt, "security_dir", None)
            res = set_skill_kill_switch(
                engaged=bool(body.get("engaged", True)),
                scope=_str(body, "scope", max_len=64, required=False, default="global") or "global",
                actor=_str(body, "actor", max_len=120),
                reason=_str(body, "reason", max_len=500, required=False),
                brain_dir=rt.brain_dir,
                security_dir=sec_dir,
            )
            return send(200, res)

        if path.startswith("/v1/skills/") and method == "POST":
            sk_tail = path[len("/v1/skills/") :]
            sk_parts = [p for p in sk_tail.split("/") if p]
            if len(sk_parts) == 2 and sk_parts[1] in {"execute", "revoke"}:
                if not owner_only():
                    return True
                sk_id = unquote(sk_parts[0]).strip()
                sk_dir = getattr(rt, "skills_dir", None) or os.path.join(rt.brain_dir, "6-skills")
                sec_dir = getattr(rt, "security_dir", None)
                if sk_parts[1] == "execute":
                    from executable_skills import execute_promoted_skill

                    raw_inputs = body.get("inputs")
                    if raw_inputs is not None and not isinstance(raw_inputs, dict):
                        raise ValueError("'inputs' must be a JSON object.")
                    exec_res = execute_promoted_skill(
                        sk_id,
                        inputs=raw_inputs,
                        actor=_str(body, "actor", max_len=120),
                        dry_run=bool(body.get("dry_run", False)),
                        brain_dir=rt.brain_dir,
                        atoms_dir=rt.atoms_dir,
                        playbooks_dir=rt.playbooks_dir,
                        skills_dir=sk_dir,
                        experiences_dir=rt.experiences_dir,
                        lexical_index_path=rt.lexical_index_path,
                        chroma_path=rt.chroma_path,
                        telemetry_db_path=rt.telemetry_db_path,
                        security_dir=sec_dir,
                    )
                    return send(200, exec_res)
                if sk_parts[1] == "revoke":
                    from executable_skills import revoke_executable_skill

                    rev_res = revoke_executable_skill(
                        sk_id,
                        expected_skill_sha256=_str(body, "expected_skill_sha256", max_len=64),
                        actor=_str(body, "actor", max_len=120),
                        reason=_str(body, "reason", max_len=500),
                        brain_dir=rt.brain_dir,
                        skills_dir=sk_dir,
                    )
                    return send(200, rev_res)

        # ── Full On-Device Mobile Vault Client (iOS / Android) Pairing & Bundle Export ──
        if path == "/v1/mobile/status" and method == "GET":
            if not owner_only():
                return True
            from mobile_vault_client import list_mobile_vault_pairings

            sec_dir = getattr(rt, "security_dir", None) or getattr(rt, "sync_state_dir", None)
            return send(200, list_mobile_vault_pairings(brain_dir=rt.brain_dir, state_dir=sec_dir))

        if path == "/v1/mobile/pair" and method == "POST":
            if not owner_only():
                return True
            from mobile_vault_client import create_mobile_vault_pairing

            sec_dir = getattr(rt, "security_dir", None) or getattr(rt, "sync_state_dir", None)
            res = create_mobile_vault_pairing(
                brain_dir=rt.brain_dir,
                label=_str(body, "label", max_len=120, required=False, default="Mobile Vault Client") or "Mobile Vault Client",
                include_passphrase_hint=_str(body, "include_passphrase_hint", max_len=200, required=False),
                sync_endpoint_override=_str(body, "sync_endpoint_override", max_len=1024, required=False).strip() or None,
                s3_read_access_key_id=_str(body, "s3_read_access_key_id", max_len=256, required=False),
                s3_read_secret_access_key=_str(body, "s3_read_secret_access_key", max_len=256, required=False),
                include_recovery_key=bool(body.get("include_recovery_key", False)),
                include_inbox_drop_pairing=bool(body.get("include_inbox_drop_pairing", True)),
                pwa_base_url=_str(body, "pwa_base_url", max_len=512, required=False, default="https://localhost/vault-client/") or "https://localhost/vault-client/",
                state_dir=sec_dir,
            )
            return send(201, res)

        if path == "/v1/mobile/revoke" and method == "POST":
            if not owner_only():
                return True
            from mobile_vault_client import revoke_mobile_vault_pairing

            sec_dir = getattr(rt, "security_dir", None) or getattr(rt, "sync_state_dir", None)
            pid = _str(body, "pairing_id", max_len=64)
            return send(200, revoke_mobile_vault_pairing(pid, brain_dir=rt.brain_dir, state_dir=sec_dir))

        if path == "/v1/mobile/export" and method == "POST":
            if not owner_only():
                return True
            from mobile_vault_client import export_mobile_vault_client_bundle

            default_out = _default_mobile_export_dir(rt)
            out_d = _str(body, "out_dir", max_len=1000, required=False, default=default_out).strip() or default_out
            return send(201, export_mobile_vault_client_bundle(out_d))

        # ── Local Creative Media Generation Studio (Offline Image & Video) ──
        if path == "/v1/media/status" and method == "GET":
            from creative_media import get_creative_media_status

            st = get_creative_media_status()
            return send(200, st.as_dict())

        if path == "/v1/media/gallery" and method == "GET":
            if not owner_only():
                return True
            from creative_media import list_generated_media

            mtype = ((q.get("media_type") or [None])[0] or None)
            proj = ((q.get("project") or [None])[0] or None)
            if proj and len(proj) > 120:
                raise ValueError("Invalid project query filter.")
            limit_val = _parse_bounded_int(q, "limit", default=30, min_val=1, max_val=200)
            items = list_generated_media(brain_dir=rt.brain_dir, media_type=mtype, project_scope=proj, limit=limit_val)
            return send(200, {"count": len(items), "items": items})

        if path.startswith("/v1/media/file/") and method == "GET":
            if not owner_only():
                return True
            from creative_media import get_generated_media_file

            media_id = unquote(path[len("/v1/media/file/") :]).strip()
            rec = get_generated_media_file(brain_dir=rt.brain_dir, media_id=media_id, verify_sha256=True)
            raw_bytes = Path(str(rec["file_path"])).read_bytes()
            mime = str(rec.get("mime_type") or "application/octet-stream")
            enc_mode = str((q.get("encoding") or ["binary"])[0] or "binary").strip().lower()
            if enc_mode in {"base64", "json", "data_uri"}:
                b64 = base64.b64encode(raw_bytes).decode("ascii")
                return send(
                    200,
                    {
                        "media_id": rec["media_id"],
                        "filename": rec.get("filename"),
                        "mime_type": mime,
                        "size_bytes": len(raw_bytes),
                        "sha256": rec.get("sha256"),
                        "data_uri": f"data:{mime};base64,{b64}",
                        "metadata": {k: v for k, v in rec.items() if k not in {"file_path", "metadata_path"}},
                    },
                )
            handler.send_response(200)
            handler.send_header("Content-Type", mime)
            handler.send_header("Content-Length", str(len(raw_bytes)))
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("X-Content-Type-Options", "nosniff")
            handler.end_headers()
            handler.wfile.write(raw_bytes)
            return True

        if path == "/v1/media/image" and method == "POST":
            if not owner_only():
                return True
            from creative_media import generate_local_image

            seed_val = _parse_json_int(body, "seed", default=None, allow_none=True)
            width_val = _parse_json_int(body, "width", default=768)
            height_val = _parse_json_int(body, "height", default=512)
            assert width_val is not None and height_val is not None
            art = generate_local_image(
                _str(body, "prompt", max_len=4000),
                brain_dir=rt.brain_dir,
                backend=_str(body, "backend", max_len=64, required=False, default="auto") or "auto",
                style=_str(body, "style", max_len=64, required=False, default="concept_card") or "concept_card",
                output_format=_str(body, "output_format", max_len=16, required=False, default="png") or "png",
                width=width_val,
                height=height_val,
                seed=seed_val,
                negative_prompt=_str(body, "negative_prompt", max_len=2000, required=False),
                title=_str(body, "title", max_len=200, required=False),
                ground_in_vault=bool(body.get("ground_in_vault", False)),
                project_scope=_str(body, "project_scope", max_len=120, required=False).strip() or None,
                include_preferences=bool(body.get("include_preferences", False)),
                attach_to_note=bool(body.get("attach_to_note", False)),
                redact_pii=bool(body.get("redact_pii", True)),
                strict_dlp=bool(body.get("strict_dlp", True)),
                allow_hardware_override=bool(body.get("allow_hardware_override", False)),
                allow_procedural_fallback=bool(body.get("allow_procedural_fallback", False)),
                policy_path=getattr(rt, "policy_path", None),
                sd_webui_url=_str(body, "sd_webui_url", max_len=256, required=False).strip() or None,
                comfyui_url=_str(body, "comfyui_url", max_len=256, required=False).strip() or None,
                http_transport=getattr(rt, "http_transport", None),
            )
            return send(201, art.as_dict())

        if path == "/v1/media/video" and method == "POST":
            if not owner_only():
                return True
            from creative_media import generate_local_video

            seed_val = _parse_json_int(body, "seed", default=None, allow_none=True)
            width_val = _parse_json_int(body, "width", default=640)
            height_val = _parse_json_int(body, "height", default=384)
            frames_val = _parse_json_int(body, "frames", default=16)
            fps_val = _parse_json_int(body, "fps", default=12)
            assert width_val is not None and height_val is not None and frames_val is not None and fps_val is not None
            art = generate_local_video(
                _str(body, "prompt", max_len=4000),
                brain_dir=rt.brain_dir,
                backend=_str(body, "backend", max_len=64, required=False, default="auto") or "auto",
                style=_str(body, "style", max_len=64, required=False, default="concept_card") or "concept_card",
                output_format=_str(body, "output_format", max_len=16, required=False, default="mp4") or "mp4",
                width=width_val,
                height=height_val,
                frames=frames_val,
                fps=fps_val,
                seed=seed_val,
                negative_prompt=_str(body, "negative_prompt", max_len=2000, required=False),
                title=_str(body, "title", max_len=200, required=False),
                ground_in_vault=bool(body.get("ground_in_vault", False)),
                project_scope=_str(body, "project_scope", max_len=120, required=False).strip() or None,
                include_preferences=bool(body.get("include_preferences", False)),
                attach_to_note=bool(body.get("attach_to_note", False)),
                redact_pii=bool(body.get("redact_pii", True)),
                strict_dlp=bool(body.get("strict_dlp", True)),
                allow_hardware_override=bool(body.get("allow_hardware_override", False)),
                allow_procedural_fallback=bool(body.get("allow_procedural_fallback", False)),
                policy_path=getattr(rt, "policy_path", None),
                comfyui_url=_str(body, "comfyui_url", max_len=256, required=False).strip() or None,
                http_transport=getattr(rt, "http_transport", None),
            )
            return send(201, art.as_dict())

        # ── Jarvis Orchestrator, 5-Panelist Design Critique & Skill Pack Importer (owner) ──
        if path == "/v1/design/presets" and method == "GET":
            from design_critique import get_design_presets

            return send(200, {"presets": get_design_presets()})

        if path == "/v1/design/critique" and method == "POST":
            if not owner_only():
                return True
            from design_critique import run_five_panel_critique

            files_raw = body.get("files") if isinstance(body.get("files"), list) else []
            artifact_txt = _str(body, "artifact_text", default="", max_len=250_000, required=False)
            preset_val = _str(body, "preset", default="", max_len=64, required=False).strip()
            if not files_raw and artifact_txt.strip():
                art_name = _str(body, "filename", default="artifact.html", max_len=120, required=False).strip() or "artifact.html"
                files_raw = [{"filename": art_name, "lang": "html", "content": artifact_txt}]
            brief_val = _str(body, "brief", max_len=2000, required=False)
            if not brief_val.strip() and preset_val:
                brief_val = f"Evaluate against preset '{preset_val}'."
            return send(
                200,
                run_five_panel_critique(
                    files_raw,
                    persona_id=_str(body, "persona_id", max_len=64, required=False),
                    done_criteria=_str(body, "done_criteria", max_len=2000, required=False),
                    brief=brief_val,
                ),
            )

        if path == "/v1/orchestrator/status" and method == "GET":
            if not owner_only():
                return True
            from jarvis_orchestrator import orchestrator_status

            return send(200, orchestrator_status())

        if path == "/v1/orchestrator/open-webui-tool" and method == "GET":
            if not owner_only():
                return True
            from jarvis_orchestrator import export_open_webui_tool

            srv_addr = getattr(getattr(handler, "server", None), "server_address", None)
            live_port = (
                int(srv_addr[1])
                if isinstance(srv_addr, tuple) and len(srv_addr) >= 2 and isinstance(srv_addr[1], int) and srv_addr[1] >= 1024
                else 0
            )
            rt_port = int(getattr(rt, "port", 0) or 0)
            api_p = live_port if live_port >= 1024 else (rt_port if rt_port >= 1024 else 8765)
            raw_proxy_q = (q.get("proxy_port") or [""])[0].strip()
            p_val = (
                _parse_bounded_int(q, "proxy_port", default=api_p, min_val=1024, max_val=65535)
                if raw_proxy_q
                else api_p
            )
            return send(200, export_open_webui_tool(api_port=int(api_p), proxy_port=int(p_val)))

        if path == "/v1/orchestrator/n8n/trigger" and method == "POST":
            if not owner_only():
                return True
            from jarvis_orchestrator import trigger_n8n_workflow

            wh_url = _str(body, "webhook_url", max_len=500, required=True)
            pl_val = body.get("payload") if isinstance(body.get("payload"), dict) else {}
            return send(200, trigger_n8n_workflow(wh_url, pl_val, http_transport=getattr(rt, "http_transport", None)))

        if path == "/v1/orchestrator/openhands/dispatch" and method == "POST":
            if not owner_only():
                return True
            from jarvis_orchestrator import dispatch_openhands_task

            return send(
                200,
                dispatch_openhands_task(
                    _str(body, "goal", max_len=4000, required=True),
                    repo_path=_str(body, "repo_path", max_len=500, required=False),
                    openhands_url=_str(body, "openhands_url", default="http://127.0.0.1:3002", max_len=300, required=False)
                    or "http://127.0.0.1:3002",
                    project_scope=_str(body, "project_scope", max_len=120, required=False) or None,
                    brain_dir=rt.brain_dir,
                    http_transport=getattr(rt, "http_transport", None),
                ),
            )

        if path == "/v1/orchestrator/appflowy/sync" and method == "POST":
            if not owner_only():
                return True
            from jarvis_orchestrator import sync_appflowy_board_tasks

            bt_raw = body.get("tasks") if isinstance(body.get("tasks"), list) else None
            return send(
                200,
                sync_appflowy_board_tasks(
                    external_board_tasks=bt_raw,
                    project=_str(body, "project", max_len=120, required=False) or None,
                    brain_dir=rt.brain_dir,
                ),
            )

        if path == "/v1/packs" and method == "GET":
            if not owner_only():
                return True
            from skill_pack_importer import list_imported_packs

            return send(200, list_imported_packs(brain_dir=rt.brain_dir))

        if path == "/v1/packs/import" and method == "POST":
            if not owner_only():
                return True
            from skill_pack_importer import discover_default_skill_packs, import_skill_pack

            pack_name = _str(body, "pack_name", max_len=64, required=True)
            src_dir = _str(body, "source_dir", max_len=1000, required=False).strip() or (
                discover_default_skill_packs().get(pack_name) or ""
            )
            if not src_dir:
                raise ValueError(f"Pack directory for {pack_name!r} not provided and not auto-discovered.")
            lim_v = _parse_json_int(body, "limit", default=None, allow_none=True)
            return send(201, import_skill_pack(src_dir, pack_name, brain_dir=rt.brain_dir, limit=lim_v))

        # ── Opt-In Screen OCR ('Eyes'), External Folder Indexer, OpenAI Proxy & Zero-Trust Tunnel ──
        if path in {"/v1/screen/config", "/v1/screen/status"} and method == "GET":
            if not owner_only():
                return True
            from screen_memory import load_screen_config

            return send(200, load_screen_config(state_dir=getattr(rt, "security_dir", None)))

        if path == "/v1/screen/config" and method == "POST":
            if not owner_only():
                return True
            from screen_memory import configure_screen_memory

            terms_raw = body.get("blocked_window_terms")
            terms_list = [str(t) for t in terms_raw] if isinstance(terms_raw, list) else None
            return send(
                200,
                configure_screen_memory(
                    enabled=bool(body["enabled"]) if "enabled" in body else None,
                    interval_seconds=_parse_json_int(body, "interval_seconds", default=None, allow_none=True),
                    retention_days=_parse_json_int(body, "retention_days", default=None, allow_none=True),
                    blocked_window_terms=terms_list,
                    state_dir=getattr(rt, "security_dir", None),
                ),
            )

        if path == "/v1/screen/search" and method == "GET":
            if not owner_only():
                return True
            from screen_memory import search_screen_memory

            query_s = (q.get("query") or [""])[0]
            lim_s = _parse_bounded_int(q, "limit", default=12, min_val=1, max_val=50)
            return send(200, {"results": search_screen_memory(query_s, limit=lim_s, brain_dir=rt.brain_dir)})

        if path in {"/v1/screen/capture", "/v1/screen/ingest"} and method == "POST":
            if not owner_only():
                return True
            from screen_memory import ingest_screen_frame

            img_b64 = _str(body, "image_b64", max_len=16_000_000, required=True)
            try:
                img_raw = base64.b64decode(img_b64, validate=True)
            except (ValueError, TypeError) as exc:
                raise ValueError("Invalid base64 'image_b64' payload.") from exc
            return send(
                200,
                ingest_screen_frame(
                    img_raw,
                    window_title=_str(body, "window_title", max_len=300, required=False),
                    app_name=_str(body, "app_name", max_len=120, required=False),
                    brain_dir=rt.brain_dir,
                    state_dir=getattr(rt, "security_dir", None),
                    require_enabled=True,
                ),
            )

        if path == "/v1/screen/promote" and method == "POST":
            if not owner_only():
                return True
            from screen_memory import promote_capture_to_note

            cid = _parse_json_int(body, "capture_id", default=None, allow_none=False)
            assert cid is not None
            return send(201, promote_capture_to_note(cid, brain_dir=rt.brain_dir, tags=_tags(body) if "tags" in body else ()))

        if path in {"/v1/external-folders/config", "/v1/external-folders/status"} and method == "GET":
            if not owner_only():
                return True
            from external_indexer import load_external_config

            return send(200, load_external_config(state_dir=getattr(rt, "security_dir", None)))

        if path == "/v1/external-folders/config" and method == "POST":
            if not owner_only():
                return True
            from external_indexer import configure_external_indexer

            return send(
                200,
                configure_external_indexer(
                    enabled=bool(body["enabled"]) if "enabled" in body else None,
                    add_folder=_str(body, "add_folder", max_len=1000, required=False) or None,
                    remove_folder=_str(body, "remove_folder", max_len=1000, required=False) or None,
                    state_dir=getattr(rt, "security_dir", None),
                    brain_dir=str(rt.brain_dir),
                ),
            )

        if path == "/v1/external-folders/scan" and method == "POST":
            if not owner_only():
                return True
            from external_indexer import scan_external_folders

            return send(
                200,
                scan_external_folders(
                    brain_dir=rt.brain_dir,
                    state_dir=getattr(rt, "security_dir", None),
                    require_enabled=True,
                ),
            )

        if path == "/v1/external-folders/search" and method == "GET":
            if not owner_only():
                return True
            from external_indexer import search_external_files

            query_e = (q.get("query") or [""])[0]
            lim_e = _parse_bounded_int(q, "limit", default=12, min_val=1, max_val=50)
            return send(
                200,
                {
                    "results": search_external_files(
                        query_e,
                        limit=lim_e,
                        brain_dir=rt.brain_dir,
                        state_dir=getattr(rt, "security_dir", None),
                    )
                },
            )

        if path == "/v1/openai/v1/models" and method == "GET":
            from local_ai_proxy import handle_openai_models, load_proxy_config

            proxy_cfg = load_proxy_config(state_dir=getattr(rt, "security_dir", None))
            if not proxy_cfg.get("enabled"):
                return send(
                    403,
                    {
                        "error": "403 Forbidden: Local OpenAI-compatible AI proxy is disabled by default. Enable it first via 'second-brain proxy enable'."
                    },
                )
            return send(200, handle_openai_models(policy_path=getattr(rt, "policy_path", None)))

        if path == "/v1/openai/v1/chat/completions" and method == "POST":
            from local_ai_proxy import handle_openai_chat_completions, load_proxy_config

            proxy_cfg = load_proxy_config(state_dir=getattr(rt, "security_dir", None))
            if not proxy_cfg.get("enabled"):
                return send(
                    403,
                    {
                        "error": "403 Forbidden: Local OpenAI-compatible AI proxy is disabled by default. Enable it first via 'second-brain proxy enable'."
                    },
                )
            hdr_proj = None
            if hasattr(handler, "headers") and handler.headers is not None:
                hdr_proj = handler.headers.get("X-Second-Brain-Project")
            res_proxy = handle_openai_chat_completions(
                body,
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
                lexical_index_path=rt.lexical_index_path,
                chroma_path=rt.chroma_path,
                policy_path=getattr(rt, "policy_path", None),
                project_scope=hdr_proj,
                http_transport=getattr(rt, "http_transport", None),
                skip_dns_check_for_mock_transport=bool(getattr(rt, "skip_dns_check_for_mock_transport", False)),
            )
            return send(200, res_proxy)

        if path == "/v1/tunnel/status" and method == "GET":
            if not owner_only():
                return True
            from zero_trust_tunnel import tunnel_status

            return send(200, tunnel_status(state_dir=getattr(rt, "security_dir", None)))

        if path == "/v1/tunnel/config" and method == "POST":
            if not owner_only():
                return True
            from zero_trust_tunnel import configure_tunnel

            return send(
                200,
                configure_tunnel(
                    enabled=bool(body["enabled"]) if "enabled" in body else None,
                    provider=_str(body, "provider", max_len=32, required=False) or None,
                    target_port=_parse_json_int(body, "target_port", default=None, allow_none=True),
                    gateway_port=_parse_json_int(body, "gateway_port", default=None, allow_none=True),
                    rate_limit_per_min=_parse_json_int(body, "rate_limit_per_min", default=None, allow_none=True),
                    tunnel_id=_str(body, "tunnel_id", max_len=128, required=False) if "tunnel_id" in body else None,
                    allow_quick_tunnel=bool(body["allow_quick_tunnel"]) if "allow_quick_tunnel" in body else None,
                    state_dir=getattr(rt, "security_dir", None),
                ),
            )

        # ── Local Text-to-Speech (Kokoro-82M ONNX) & Two-Way Voice Loop (owner-only) ──
        if path == "/v1/voice/status" and method == "GET":
            if not owner_only():
                return True
            import importlib.util as _ilu
            import ingest_multimodal as _im
            from tts_engine import tts_status

            probe_fn = getattr(_im, "probe_audio_transcription_backend", None)
            if callable(probe_fn):
                stt_ok, stt_detail = probe_fn()
            elif _ilu.find_spec("faster_whisper") is not None:
                stt_ok, stt_detail = True, "faster-whisper available"
            else:
                stt_ok, stt_detail = False, "faster-whisper not installed"
            tts_info = tts_status(state_dir=getattr(rt, "security_dir", None))
            return send(
                200,
                {
                    "tts": tts_info,
                    "stt": {"available": bool(stt_ok), "detail": str(stt_detail)},
                },
            )

        if path == "/v1/voice/speak" and method == "POST":
            if not owner_only():
                return True
            from tts_engine import TtsUnavailableError, synthesize_wav_bytes

            raw_text = _str(body, "text", max_len=8000, required=True)
            voice_name = _str(body, "voice", default="af_heart", max_len=64, required=False) or "af_heart"
            raw_spd = body.get("speed", 1.0)
            try:
                spd_val = float(raw_spd)
            except (TypeError, ValueError) as exc:
                raise ValueError("'speed' must be a numeric float.") from exc
            try:
                out_tts = synthesize_wav_bytes(
                    raw_text,
                    voice=voice_name,
                    speed=spd_val,
                    state_dir=getattr(rt, "security_dir", None),
                )
            except TtsUnavailableError as exc:
                return send(503, {"error": str(exc), "remediation": "second-brain models pull kokoro-tts"})
            return send(
                200,
                {
                    "status": "ok",
                    "mime_type": out_tts["mime_type"],
                    "sample_rate": out_tts["sample_rate"],
                    "duration_seconds": out_tts["duration_seconds"],
                    "rtf": out_tts["rtf"],
                    "clean_text": out_tts["clean_text"],
                    "voice": out_tts["voice"],
                    "model_id": out_tts["model_id"],
                    "audio_b64": base64.b64encode(out_tts["wav_bytes"]).decode("ascii"),
                },
            )

        if path == "/v1/voice/conversation-turn" and method == "POST":
            if not owner_only():
                return True
            from tts_engine import voice_conversation_turn

            audio_b64 = _str(body, "audio_b64", max_len=24_000_000, required=True)
            try:
                audio_raw = base64.b64decode(audio_b64, validate=True)
            except Exception as exc:
                raise ValueError("Invalid base64 'audio_b64' payload.") from exc
            fname = _str(body, "filename", default="voice-turn.webm", max_len=128, required=False) or "voice-turn.webm"
            mode_val = _str(body, "mode", default="stream", max_len=32, required=False) or "stream"
            host_val = _str(body, "host_id", default="ollama_local", max_len=64, required=False) or "ollama_local"
            scope_val = _str(body, "project_scope", max_len=120, required=False).strip() or None
            voice_name = _str(body, "voice", default="af_heart", max_len=64, required=False) or "af_heart"
            try:
                spd_val = float(body.get("speed", 1.0))
            except (TypeError, ValueError) as exc:
                raise ValueError("'speed' must be a numeric float.") from exc
            turn_res = voice_conversation_turn(
                audio_raw,
                fname,
                brain_dir=rt.brain_dir,
                policy_path=getattr(rt, "policy_path", None),
                state_dir=getattr(rt, "security_dir", None),
                mode=mode_val,
                host_id=host_val,
                project_scope=scope_val,
                voice=voice_name,
                speed=spd_val,
            )
            return send(200, turn_res)

        # ── Speculative Decoding Runtime (llama.cpp Draft+Target, owner-controlled) ──
        if path == "/v1/runtime/speculative/status" and method == "GET":
            if not owner_only():
                return True
            from llama_cpp_runner import speculative_status

            port_q = _parse_bounded_int(q, "port", default=0, min_val=0, max_val=65535)
            return send(
                200,
                speculative_status(
                    state_dir=getattr(rt, "security_dir", None),
                    port=port_q if port_q >= 1024 else None,
                ),
            )

        if path == "/v1/runtime/speculative/start" and method == "POST":
            if not owner_only():
                return True
            from llama_cpp_runner import start_speculative_server

            port_v = _parse_json_int(body, "port", default=8081) or 8081
            dmax = _parse_json_int(body, "draft_max", default=8) or 8
            dmin = _parse_json_int(body, "draft_min", default=2) or 2
            res = start_speculative_server(
                _str(body, "target_model", max_len=1000, required=True),
                _str(body, "draft_model", max_len=1000, required=True),
                port=port_v,
                draft_max=dmax,
                draft_min=dmin,
                threads=_parse_json_int(body, "threads", default=None, allow_none=True),
                ctx_size=_parse_json_int(body, "ctx_size", default=None, allow_none=True),
                n_gpu_layers=_parse_json_int(body, "n_gpu_layers", default=None, allow_none=True),
                state_dir=getattr(rt, "security_dir", None),
                policy_path=getattr(rt, "policy_path", None),
                register_host=bool(body.get("register_host", True)),
            )
            return send(200, res)

        if path == "/v1/runtime/speculative/stop" and method == "POST":
            if not owner_only():
                return True
            from llama_cpp_runner import stop_speculative_server

            return send(200, stop_speculative_server(state_dir=getattr(rt, "security_dir", None)))

        if path == "/v1/runtime/speculative/benchmark" and method == "POST":
            if not owner_only():
                return True
            from llama_cpp_runner import benchmark_speculative_server

            port_v = _parse_json_int(body, "port", default=None, allow_none=True)
            max_tok = _parse_json_int(body, "max_tokens", default=96) or 96
            prompt_v = _str(
                body,
                "prompt",
                default="Summarize why local-first knowledge bases require immutable provenance anchors in 3 bullet points.",
                max_len=2000,
                required=False,
            )
            return send(
                200,
                benchmark_speculative_server(
                    prompt=prompt_v,
                    max_tokens=max_tok,
                    port=port_v,
                    state_dir=getattr(rt, "security_dir", None),
                ),
            )

        # ── Phase 4 / Epic 2D: Gen-4 Cognitive Architecture ("LKE-X") ──
        if path == "/v1/cognitive/context-signal" and method == "POST":
            if not owner_only():
                return True
            from predictive_memory_bus import record_context_signal

            syms_raw = body.get("open_symbols")
            syms = [str(s) for s in syms_raw] if isinstance(syms_raw, list) else []
            return send(
                200,
                record_context_signal(
                    active_file=_str(body, "active_file", default="", max_len=260, required=False),
                    git_branch=_str(body, "git_branch", default="", max_len=120, required=False),
                    project_scope=_optional_project_scope(body.get("project") or body.get("project_scope")),
                    open_symbols=syms,
                    cursor_topic=_str(body, "cursor_topic", default="", max_len=260, required=False),
                    prewarm=bool(body.get("prewarm", True)),
                    brain_dir=rt.brain_dir,
                    atoms_dir=rt.atoms_dir,
                    lexical_index_path=rt.lexical_index_path,
                ),
            )

        if path == "/v1/cognitive/bus-status" and method == "GET":
            if not owner_only():
                return True
            from predictive_memory_bus import bus_status

            return send(200, bus_status(brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir))

        if path == "/v1/recall/zoom" and method == "POST":
            from fractal_zoom import zoom_retrieve

            query_z = _str(body, "query", max_len=1000, default="", required=False)
            lvl = _str(body, "target_level", max_len=8, default="L5", required=False).upper()
            parent_id = _str(body, "parent_id", max_len=200, default="", required=False).strip() or None
            proj_z = _optional_project_scope(body.get("project"))
            lim_z = _parse_json_int(body, "limit", default=8) or 8
            return send(
                200,
                zoom_retrieve(
                    query_z,
                    target_level=lvl,
                    parent_id=parent_id,
                    project=proj_z,
                    limit=lim_z,
                    brain_dir=rt.brain_dir,
                    atoms_dir=rt.atoms_dir,
                ),
            )

        if path == "/v1/cognitive/review-due" and method == "GET":
            if not owner_only():
                return True
            from cognitive_core import compute_knowledge_gravity_report

            as_of_v = (q.get("as_of") or [None])[0]
            return send(
                200,
                compute_knowledge_gravity_report(
                    atoms_dir=rt.atoms_dir,
                    brain_dir=rt.brain_dir,
                    db_path=rt.telemetry_db_path,
                    as_of=as_of_v,
                ),
            )

        if path.startswith("/v1/atoms/cold-archive/") and path.endswith("/restore") and method == "POST":
            if not owner_only():
                return True
            raw_slug = unquote(path[len("/v1/atoms/cold-archive/") : -len("/restore")]).strip()
            norm_slug = normalize_markdown_filename(raw_slug)
            if not norm_slug or "/" in norm_slug or "\\" in norm_slug or ".." in norm_slug:
                raise _BadRequestError(f"Invalid cold-archive atom identifier: {raw_slug!r}")
            exp_sha = _str(body, "expected_sha256", max_len=64, required=True)
            actor_v = _str(body, "actor", default="owner", max_len=120, required=False)
            reason_v = _str(body, "reason", default="", max_len=500, required=False)
            return send(
                200,
                restore_cold_archived_atom(
                    norm_slug,
                    expected_sha256=exp_sha,
                    actor=actor_v,
                    reason=reason_v,
                    brain_dir=rt.brain_dir,
                    atoms_dir=rt.atoms_dir,
                ),
            )

        if path.startswith("/v1/atoms/") and path.endswith("/cold-archive") and method == "POST":
            if not owner_only():
                return True
            raw_fn = unquote(path[len("/v1/atoms/") : -len("/cold-archive")]).strip()
            safe_p = safe_file_in_dir(rt.atoms_dir, raw_fn, allowed_exts={".md"}, must_exist=True)
            if not safe_p:
                raise FileNotFoundError(f"Atom not found: {raw_fn}")
            exp_sha = _str(body, "expected_sha256", max_len=64, required=True)
            actor_v = _str(body, "actor", default="owner", max_len=120, required=False)
            reason_v = _str(body, "reason", default="", max_len=500, required=False)
            return send(
                200,
                archive_atom_to_cold(
                    safe_p,
                    expected_sha256=exp_sha,
                    actor=actor_v,
                    reason=reason_v,
                    brain_dir=rt.brain_dir,
                    atoms_dir=rt.atoms_dir,
                ),
            )

        if path == "/v1/principles" and method == "GET":
            from cognitive_distiller import list_distilled_artifacts

            proj_p = _optional_project_scope((q.get("project") or [None])[0])
            req_active_only_p = ((q.get("active_only") or ["true"])[0]).strip().lower() not in {"false", "0", "no"}
            active_only_p = True if not is_owner else req_active_only_p
            rows_p = list_distilled_artifacts(
                kind="principle",
                project=proj_p,
                active_only=active_only_p,
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
            )
            if not is_owner:
                rows_p = [{k: v for k, v in item.items() if k != "path"} for item in rows_p]
            return send(200, {"principles": rows_p})

        if path == "/v1/mental-models" and method == "GET":
            from cognitive_distiller import list_distilled_artifacts

            proj_m = _optional_project_scope((q.get("project") or [None])[0])
            req_active_only_m = ((q.get("active_only") or ["true"])[0]).strip().lower() not in {"false", "0", "no"}
            active_only_m = True if not is_owner else req_active_only_m
            rows_m = list_distilled_artifacts(
                kind="mental_model",
                project=proj_m,
                active_only=active_only_m,
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
            )
            if not is_owner:
                rows_m = [{k: v for k, v in item.items() if k != "path"} for item in rows_m]
            return send(200, {"mental_models": rows_m})

        if path == "/v1/principles/distill" and method == "POST":
            if not owner_only():
                return True
            from cognitive_distiller import distill_cross_project_candidates

            _optional_project_scope(body.get("project"))
            return send(
                201,
                distill_cross_project_candidates(brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir),
            )

        if (
            (path.startswith("/v1/principles/") or path.startswith("/v1/mental-models/"))
            and path.endswith("/promote")
            and method == "POST"
        ):
            if not owner_only():
                return True
            from cognitive_distiller import promote_distilled_artifact

            is_pr = path.startswith("/v1/principles/")
            prefix = "/v1/principles/" if is_pr else "/v1/mental-models/"
            subdir = "7-principles" if is_pr else "8-mental-models"
            fname = unquote(path[len(prefix) : -len("/promote")]).strip()
            safe_d = safe_file_in_dir(os.path.join(rt.brain_dir, subdir), fname, allowed_exts={".md"}, must_exist=True)
            if not safe_d:
                raise FileNotFoundError(f"Distilled artifact not found: {fname}")
            reason_val = (
                _str(body, "reason", default="", max_len=500, required=False)
                or _str(body, "note", default="", max_len=500, required=False)
            )
            return send(
                200,
                promote_distilled_artifact(
                    safe_d,
                    expected_sha256=_str(body, "expected_sha256", max_len=64, required=True),
                    actor=_str(body, "actor", default="owner", max_len=120, required=False),
                    action=_str(body, "action", default="promote", max_len=16, required=False),
                    reason=reason_val,
                    brain_dir=rt.brain_dir,
                    atoms_dir=rt.atoms_dir,
                ),
            )

        if path == "/v1/concepts" and method == "GET":
            from concept_graph import list_concepts

            proj_c = _optional_project_scope((q.get("project") or [None])[0])
            lim_c = _parse_bounded_int(q, "limit", default=50, min_val=1, max_val=200)
            return send(
                200,
                {"concepts": list_concepts(project=proj_c, limit=lim_c, brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir)},
            )

        if path.startswith("/v1/concepts/") and method == "GET":
            from concept_graph import get_concept

            cid_raw = unquote(path[len("/v1/concepts/") :]).strip()
            proj_c = _optional_project_scope((q.get("project") or [None])[0])
            return send(200, get_concept(cid_raw, project=proj_c, brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir))

        if path == "/v1/cognitive/debate" and method == "POST":
            from cognitive_swarm import run_two_sided_debate

            topic_d = _str(body, "topic", max_len=1000, required=True)
            proj_d = _optional_project_scope(body.get("project"))
            return send(
                200,
                run_two_sided_debate(topic_d, project=proj_d, brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir),
            )

        if path == "/v1/cognitive/swarm" and method == "POST":
            from cognitive_swarm import run_cognitive_swarm

            goal_s = (
                _str(body, "goal", max_len=1500, required=False).strip()
                or _str(body, "query", max_len=1500, required=False).strip()
            )
            if not goal_s:
                raise ValueError("'goal' is required.")
            proj_s = _optional_project_scope(body.get("project"))
            prof_s = _str(body, "reasoning_profile", default="auto", max_len=32, required=False)
            max_atoms_s = _parse_json_int(body, "max_atoms", default=None, min_val=1, max_val=25, allow_none=True)
            swarm_res = run_cognitive_swarm(
                goal_s,
                project=proj_s,
                reasoning_profile=prof_s,
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
                lexical_index_path=rt.lexical_index_path,
                chroma_path=rt.chroma_path,
            )
            if max_atoms_s is not None and isinstance(swarm_res.get("roles"), dict):
                know = swarm_res["roles"].get("knowledge")
                if isinstance(know, dict) and isinstance(know.get("atom_citations"), list):
                    know["atom_citations"] = know["atom_citations"][:max_atoms_s]
            return send(200, swarm_res)

        if path == "/v1/cognitive/simulate" and method == "POST":
            from world_model_sim import simulate_change_impact

            target_sim = _str(body, "target", max_len=260, required=True)
            hops_sim = _parse_json_int(body, "max_hops", default=3) or 3
            proj_sim = _optional_project_scope(body.get("project"))
            repo_sim = _str(body, "repo_dir", default="", max_len=1000, required=False).strip() or None
            if repo_sim is not None and not owner_only():
                return True
            return send(
                200,
                simulate_change_impact(
                    target_sim,
                    max_hops=hops_sim,
                    project=proj_sim,
                    repo_dir=repo_sim,
                    allow_repo_scan=bool(is_owner),
                    brain_dir=rt.brain_dir,
                    atoms_dir=rt.atoms_dir,
                ),
            )

        if path == "/v1/cognitive/self-model" and method == "GET":
            if not owner_only():
                return True
            from self_model_curiosity import evaluate_self_model_and_curiosity

            return send(200, evaluate_self_model_and_curiosity(brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir))

        if path == "/v1/cognitive/self-model" and method == "POST":
            if not owner_only():
                return True
            from self_model_curiosity import upsert_learning_goals

            goals_raw = body.get("goals")
            if not isinstance(goals_raw, list):
                raise ValueError("'goals' list is required.")
            return send(200, upsert_learning_goals(goals_raw, brain_dir=rt.brain_dir))

        if path == "/v1/cognitive/curiosity" and method == "GET":
            if not owner_only():
                return True
            from self_model_curiosity import evaluate_self_model_and_curiosity

            _optional_project_scope((q.get("project") or [None])[0])
            res_sm = evaluate_self_model_and_curiosity(brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir)
            return send(200, {"curiosity_targets": res_sm["curiosity_targets"], "domains": res_sm["domains"]})

        if path == "/v1/cognitive/adr-status" and method == "GET":
            if not owner_only():
                return True
            from adr_gate import list_adr_states

            return send(200, {"adrs": list_adr_states(brain_dir=rt.brain_dir)})

        if path == "/v1/cognitive/adr-enable" and method == "POST":
            if not owner_only():
                return True
            from adr_gate import set_adr_status

            return send(
                200,
                set_adr_status(
                    _str(body, "adr_id", max_len=64, required=True),
                    enabled=bool(body.get("enabled", True)),
                    actor=_str(body, "actor", default="owner", max_len=120, required=False),
                    rationale=_str(body, "rationale", max_len=400, required=True),
                    expires_on=_str(body, "expires_on", default="", max_len=16, required=False).strip() or None,
                    brain_dir=rt.brain_dir,
                ),
            )

        if path == "/v1/cognitive/replay" and method == "POST":
            if not owner_only():
                return True
            from episodic_replay import run_nightly_replay

            date_val = _str(body, "date_str", default="", max_len=32, required=False).strip() or None
            if "write_inbox" in body:
                stage_inbox = bool(body.get("write_inbox"))
            elif "stage_inbox_proposals" in body:
                stage_inbox = bool(body.get("stage_inbox_proposals"))
            else:
                stage_inbox = False
            return send(
                200,
                run_nightly_replay(
                    brain_dir=rt.brain_dir,
                    atoms_dir=rt.atoms_dir,
                    date_str=date_val,
                    stage_inbox_proposals=stage_inbox,
                ),
            )

        if path == "/v1/cognitive/route" and method == "POST":
            from meta_router import classify_query, resolve_reasoning_profile

            query_r = _str(body, "query", max_len=2000, required=True)
            req_prof = _str(body, "reasoning_profile", default="auto", max_len=32, required=False).strip() or "auto"
            has_cloud = bool(body.get("has_cloud_consent", False))
            resolved_prof = resolve_reasoning_profile(req_prof, has_cloud_consent=has_cloud)
            budget = classify_query(query_r, reasoning_profile=resolved_prof, has_cloud_consent=has_cloud)
            return send(
                200,
                {
                    "query": query_r,
                    "profile": resolved_prof,
                    "mode": budget.reasoning_mode,
                    "complexity": budget.complexity,
                    "path": budget.path,
                    "budget": asdict(budget),
                },
            )

        # ── Phase 5 / Epic 2E: Quantitative Reasoning, 4D Rubric & AlphaProof Nexus ──
        if path == "/v1/math/solve" and method == "POST":
            from cgv_reasoner import solve_quantitative_problem

            problem_m = _str(body, "problem", max_len=4000, required=True)
            vis_ocr = _str(body, "visual_ocr_text", default="", max_len=4000, required=False)
            if "k_samples" in body:
                k_m = _parse_json_int(body, "k_samples", default=1, min_val=1, max_val=16) or 1
            else:
                k_m = _parse_json_int(body, "samples", default=1, min_val=1, max_val=16) or 1
            turns_m = _parse_json_int(body, "max_tool_turns", default=4, min_val=1, max_val=8) or 4
            exp_ans = _str(body, "expected_answer", default="", max_len=1000, required=False).strip() or None
            return send(
                200,
                solve_quantitative_problem(
                    problem_m,
                    visual_ocr_text=vis_ocr,
                    k_samples=k_m,
                    max_tool_turns=turns_m,
                    use_local_llm=bool(body.get("use_local_llm", False)),
                    expected_answer=exp_ans,
                ),
            )

        if path == "/v1/math/rubric" and method == "POST":
            from math_equivalence import extract_final_answer
            from process_evaluator import score_reasoning_trace

            steps_r = body.get("steps")
            if steps_r is None and "trace" in body:
                raw_trace = body.get("trace")
                if isinstance(raw_trace, list):
                    steps_r = [str(s) for s in raw_trace]
                elif isinstance(raw_trace, str):
                    steps_r = [ln.strip() for ln in raw_trace.splitlines() if ln.strip()]
            if not isinstance(steps_r, list):
                raise ValueError("'steps' list is required.")
            clean_steps = [str(s) for s in steps_r]
            final_ans = _str(body, "final_answer", default="", max_len=1000, required=False).strip()
            if not final_ans and clean_steps:
                final_ans = extract_final_answer("\n".join(clean_steps))
            return send(
                200,
                score_reasoning_trace(
                    clean_steps,
                    final_answer=final_ans,
                    expected_answer=_str(body, "expected_answer", default="", max_len=1000, required=False) or None,
                ),
            )

        if path == "/v1/proof/status" and method == "GET":
            from formal_proof_search import _subgoal_cache_path, verify_lean_binary_available

            lean_bin = verify_lean_binary_available()
            cache_p = _subgoal_cache_path(rt.brain_dir)
            status_payload: dict[str, object] = {
                "lean_available": bool(lean_bin),
                "lean_installed": bool(lean_bin),
                "lean_status": "ready" if lean_bin else "lean_toolchain_unavailable",
                "subgoal_cache_ready": bool(cache_p.is_file() or cache_p.parent.is_dir()),
            }
            if is_owner:
                status_payload["lean_path"] = lean_bin
                status_payload["subgoal_cache_path"] = str(cache_p)
            return send(200, status_payload)

        if path == "/v1/proof/verify" and method == "POST":
            if not owner_only():
                return True
            from formal_proof_search import SafeVerifyError, run_test_lemmas, verify_spec_integrity

            base_src = _str(body, "baseline_source", max_len=64000, required=True)
            cand_src = _str(body, "candidate_source", max_len=64000, required=True)
            lang_p = _str(body, "language", default="auto", max_len=16, required=False)
            lemmas_raw = body.get("test_lemmas")
            lemmas = [str(x) for x in lemmas_raw] if isinstance(lemmas_raw, list) else []
            try:
                sv = verify_spec_integrity(base_src, cand_src, language=lang_p)
            except SafeVerifyError as exc:
                return send(422, {"valid": False, "error": str(exc)})
            tl = run_test_lemmas(cand_src, lemmas, language=lang_p) if lemmas else {"passed": True, "checked": 0, "failures": []}
            return send(200, {**sv, "test_lemmas": tl})

        if path == "/v1/proof/search" and method == "POST":
            if not owner_only():
                return True
            import hashlib as _hl
            from formal_proof_search import (
                SafeVerifyError,
                _detect_language,
                _extract_skeleton_and_blocks,
                _verify_candidate,
                run_agent_d_search,
                run_ralph_proof_loop,
                verify_spec_integrity,
            )

            base_src = _str(body, "baseline_source", max_len=64000, required=True)
            problem_id = _str(body, "problem_id", default="", max_len=160, required=False).strip()
            cand_src = _str(body, "candidate_source", default="", max_len=64000, required=False)
            lang_p = _str(body, "language", default="auto", max_len=16, required=False)
            search_mode = _str(body, "search_mode", default="auto", max_len=16, required=False).strip().lower() or "auto"
            if search_mode not in {"auto", "ralph", "agent_d", "single"}:
                raise ValueError("'search_mode' must be one of auto, ralph, agent_d, single.")
            tests_raw = body.get("verification_tests")
            vtests = [str(x) for x in tests_raw] if isinstance(tests_raw, list) else []
            lemmas_raw = body.get("test_lemmas")
            tlemmas = [str(x) for x in lemmas_raw] if isinstance(lemmas_raw, list) else []
            max_iters = _parse_json_int(body, "max_iterations", default=None, min_val=1, max_val=16, allow_none=True)
            candidates_raw = body.get("candidates")
            if candidates_raw is not None:
                if not isinstance(candidates_raw, list) or len(candidates_raw) > 32:
                    raise ValueError("'candidates' must be a list of up to 32 entries.")

            try:
                eff_lang = _detect_language(base_src, lang_p)
                base_skel, base_blocks = _extract_skeleton_and_blocks(base_src.replace("\r\n", "\n"), eff_lang)
                if not base_blocks:
                    raise SafeVerifyError("Baseline specification has no EVOLVE-BLOCK or EVOLVE-VALUE markers.")
                spec_sha = _hl.sha256(base_skel.encode("utf-8")).hexdigest()
                marker_token = "-- EVOLVE-" if eff_lang == "lean" else "# EVOLVE-"

                # Backwards-compatible single-candidate path when only candidate_source is passed.
                if (
                    candidates_raw is None
                    and max_iters is None
                    and search_mode in {"auto", "single"}
                    and cand_src.strip()
                ):
                    verify_spec_integrity(base_src, cand_src, language=eff_lang)
                    single_res = _verify_candidate(base_src, cand_src, vtests, language=eff_lang)
                    return send(200, single_res)

                use_agent_d = search_mode == "agent_d" or (
                    search_mode == "auto"
                    and isinstance(candidates_raw, list)
                    and len(candidates_raw) >= 2
                    and any(isinstance(c, dict) for c in candidates_raw)
                )

                if use_agent_d:
                    pool: list[dict[str, object]] = []
                    for idx, item in enumerate(candidates_raw or ()):
                        cid = f"cand_{idx + 1}"
                        if isinstance(item, dict):
                            entry = dict(item)
                            entry.setdefault("id", cid)
                            pool.append(entry)
                        elif isinstance(item, list):
                            pool.append({"id": cid, "replacements": [str(r) for r in item]})
                        elif isinstance(item, str):
                            if marker_token in item:
                                pool.append({"id": cid, "source": item})
                            else:
                                pool.append({"id": cid, "replacements": [item]})
                        else:
                            raise ValueError("Each candidate must be a string, list of replacements, or dict.")
                    if not pool:
                        pool.append({"id": "cand_1", "source": cand_src if cand_src.strip() else base_src})
                    ad_res = run_agent_d_search(
                        base_src,
                        pool,
                        verification_tests=vtests,
                        language=eff_lang,
                    )
                    lb = list(ad_res.get("leaderboard") or [])
                    first_ok = next((row for row in lb if row.get("verified")), None)
                    first_err = "" if first_ok else next((str(row.get("error") or "") for row in lb if row.get("error")), "")
                    return send(
                        200,
                        {
                            **ad_res,
                            "problem_id": problem_id,
                            "search_mode": "agent_d",
                            "spec_sha256": spec_sha,
                            "verified": bool(ad_res.get("verified_count", 0) > 0),
                            "verified_source": first_ok.get("source") if isinstance(first_ok, dict) else None,
                            "error": first_err,
                        },
                    )

                # Ralph proof loop path (real run_ralph_proof_loop over candidates / baseline).
                repl_seq: list[list[str]] = []
                for item in candidates_raw or ():
                    if isinstance(item, dict):
                        if isinstance(item.get("replacements"), list):
                            repl_seq.append([str(r) for r in item["replacements"]])
                        elif isinstance(item.get("source"), str):
                            src_i = item["source"]
                            verify_spec_integrity(base_src, src_i, language=eff_lang)
                            _, blks_i = _extract_skeleton_and_blocks(src_i.replace("\r\n", "\n"), eff_lang)
                            repl_seq.append(blks_i)
                        else:
                            raise ValueError("Candidate dict requires 'replacements' or 'source'.")
                    elif isinstance(item, list):
                        repl_seq.append([str(r) for r in item])
                    elif isinstance(item, str):
                        if marker_token in item:
                            verify_spec_integrity(base_src, item, language=eff_lang)
                            _, blks_i = _extract_skeleton_and_blocks(item.replace("\r\n", "\n"), eff_lang)
                            repl_seq.append(blks_i)
                        else:
                            repl_seq.append([item])
                    else:
                        raise ValueError("Each candidate must be a string, list of replacements, or dict.")

                if not repl_seq:
                    if cand_src.strip():
                        verify_spec_integrity(base_src, cand_src, language=eff_lang)
                        _, cand_blks = _extract_skeleton_and_blocks(cand_src.replace("\r\n", "\n"), eff_lang)
                        repl_seq.append(cand_blks)
                    else:
                        repl_seq.append(base_blocks)

                eff_iters = min(max_iters, len(repl_seq)) if max_iters is not None else len(repl_seq)
                eff_iters = max(1, eff_iters)

                def _mutate_from_candidates(_baseline: str, iteration: int, _feedback: str) -> list[str]:
                    return repl_seq[min(iteration - 1, len(repl_seq) - 1)]

                ralph_res = run_ralph_proof_loop(
                    base_src,
                    mutate_fn=_mutate_from_candidates,
                    test_lemmas=tlemmas,
                    verification_tests=vtests,
                    language=eff_lang,
                    max_iterations=eff_iters,
                    brain_dir=rt.brain_dir,
                    record_candidate=bool(body.get("record_candidate", False)),
                )
                attempts_list = list(ralph_res.get("attempts") or [])
                last_err = "" if ralph_res.get("verified") else (str(attempts_list[-1].get("error") or "") if attempts_list else "")
                return send(
                    200,
                    {
                        **ralph_res,
                        "problem_id": problem_id,
                        "search_mode": "ralph",
                        "error": last_err,
                    },
                )
            except SafeVerifyError as exc:
                return send(422, {"valid": False, "verified": False, "error": str(exc)})

    except (MemoryRecallUnavailable, LexicalIndexUnavailable) as exc:
        return send(
            503,
            {
                "error": str(exc),
                "remediation": "Run 'second-brain rebuild-lexical' or start local Ollama.",
            },
        )
    except _BadRequestError as exc:
        return send(400, {"error": str(exc)})
    except (ReviewConflictError, SyncConflictError) as exc:
        return send(409, {"error": str(exc), "conflict": True})
    except FileNotFoundError as exc:
        return send(404, {"error": str(exc)})
    except NoteError as exc:
        if str(exc).startswith("Note not found:"):
            return send(404, {"error": str(exc)})
        return send(422, {"error": str(exc)})
    except PermissionError as exc:
        err_body: dict[str, object] = {"error": str(exc)}
        audit_rec = getattr(exc, "audit_record", None)
        if isinstance(audit_rec, dict):
            err_body["audit_record"] = audit_rec
        return send(403, err_body)
    except (
        PlaybookValidationError,
        ReviewTransitionError,
        SyncError,
        InboxDropError,
        ClipperError,
        VaultWorkspaceError,
        OnnxModelError,
        GitDistillerError,
        VaultBackupError,
        ValueError,
        NotADirectoryError,
    ) as exc:
        from host_adapters import OutboundSecretBlockedError

        if isinstance(
            exc,
            (
                PermissionError,
                OutboundSecretBlockedError,
                OnnxModelSecurityError,
                InboxDropSecurityError,
                GitDistillerSecurityError,
                VaultBackupSecurityError,
            ),
        ):
            err_body = {"error": str(exc)}
            audit_rec = getattr(exc, "audit_record", None)
            if isinstance(audit_rec, dict):
                err_body["audit_record"] = audit_rec
            return send(403, err_body)
        err_body = {"error": str(exc)}
        audit_rec = getattr(exc, "audit_record", None)
        if isinstance(audit_rec, dict):
            err_body["audit_record"] = audit_rec
        return send(422, err_body)
    except (
        HostConsentError,
        SyncSecurityError,
        InboxDropSecurityError,
        ClipperSecurityError,
        VaultWorkspaceSecurityError,
        OnnxModelSecurityError,
        GitDistillerSecurityError,
        VaultBackupSecurityError,
    ) as exc:
        return send(403, {"error": str(exc)})
    except RuntimeError as exc:
        if "CAS mismatch" in str(exc):
            return send(409, {"error": str(exc), "conflict": True})
        if exc.__class__.__name__ == "TtsUnavailableError":
            return send(503, {"error": str(exc)})
        if exc.__class__.__name__ in {"MobileVaultError", "CreativeMediaError", "TtsError"}:
            return send(422, {"error": str(exc)})
        raise
    except Exception as exc:
        if exc.__class__.__name__ == "CreativeMediaUnavailableError":
            return send(503, {"error": str(exc)})
        if exc.__class__.__name__ == "CreativeMediaError":
            return send(422, {"error": str(exc)})
        raise
    return False
