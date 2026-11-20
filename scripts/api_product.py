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
    atomic_write_bytes,
    content_sha256,
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
from recall import MemoryRecallUnavailable  # noqa: E402
from skill_playbooks import PlaybookValidationError  # noqa: E402
from vault_sync import (  # noqa: E402
    SyncConflictError,
    SyncError,
    SyncSecurityError,
    init_sync,
    join_sync,
    list_sync_conflicts,
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
    ingest_web_clip,
    list_clipper_pairings,
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


def _atom_summary_from_indexed_row(filename: str, content: str, sha256: str) -> dict[str, object] | None:
    """Parse static atom card metadata from FTS5 row content once per (filename, content_sha256)."""
    cache_key = f"{filename}:{sha256}"
    with _ATOM_SUMMARY_CACHE_LOCK:
        cached = _ATOM_SUMMARY_BY_SHA.get(cache_key)
    if cached is not None:
        return {
            **cached,
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
        "sha256": sha256,
    }
    with _ATOM_SUMMARY_CACHE_LOCK:
        if len(_ATOM_SUMMARY_BY_SHA) >= _ATOM_SUMMARY_CACHE_MAX and cache_key not in _ATOM_SUMMARY_BY_SHA:
            _ATOM_SUMMARY_BY_SHA.pop(next(iter(_ATOM_SUMMARY_BY_SHA)), None)
        _ATOM_SUMMARY_BY_SHA[cache_key] = summary
    return {
        **summary,
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
        else:
            raise FileNotFoundError(f"Unknown vault leaf prefix: {leaf_ref}")
    elif len(parts) == 1:
        raw_name = normalize_markdown_filename(parts[0])
        ap = safe_file_in_dir(atoms_dir, raw_name, allowed_exts={".md"}, must_exist=True)
        pp = safe_file_in_dir(playbooks_dir, raw_name, allowed_exts={".md"}, must_exist=True)
        if ap:
            candidates.append(f"2-atoms/{os.path.basename(ap)}")
        if pp:
            candidates.append(f"5-playbooks/{os.path.basename(pp)}")
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
            from streaming_chat import ChatPaths, ChatStreamRequest, chat_events

            chat_payload = dict(body)
            if "mode" not in chat_payload or chat_payload.get("mode") is None or (
                isinstance(chat_payload.get("mode"), str) and not str(chat_payload.get("mode")).strip()
            ):
                chat_payload["mode"] = "stream"
            req = ChatStreamRequest.model_validate(chat_payload)
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
                )
                if req.answer_view == "preference" and not is_owner:
                    return send(
                        403,
                        {"error": "403 Forbidden: subjective preference view requires owner authorization."},
                    )
                inc_prefs = bool(is_owner and req.include_preferences)
                stream_sse(
                    handler,
                    chat_events(
                        req.query,
                        mode=req.mode,
                        paths=cp,
                        project_scope=req.project_scope,
                        model_override=req.model_override,
                        include_preferences=inc_prefs,
                        host_id=req.host_id,
                        retrieval_mode=req.retrieval_mode,
                        allow_cross_project=req.allow_cross_project,
                        answer_view=req.answer_view,
                        allow_unverified_leads=bool(is_owner and (req.answer_view == "research" or req.mode == "research")),
                    ),
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
                    if proj_filter and not project_tags_match(tuple(base["contexts"]), (proj_filter,)):  # type: ignore[arg-type]
                        continue
                    if not is_owner and (
                        base.get("epistemic_label") == "preference" or bool(base.get("epistemic_contract"))
                    ):
                        continue
                    if not is_owner and base.get("review_status") != "accepted":
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
                    if proj_filter and not project_tags_match(tuple(card.get("contexts") or ()), (proj_filter,)):  # type: ignore[arg-type]
                        continue
                    if not is_owner:
                        if not card["verified"]:
                            continue
                        if card.get("epistemic_label") == "preference" or bool(card.get("epistemic_contract")):
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
            return send(200, proof)

        # ── Contradiction & Drift Radar + Knowledge Graph ──
        if path == "/v1/graph/contradictions" and method == "GET":
            from knowledge_graph import detect_contradictions

            proj = ((q.get("project") or [None])[0] or None)
            if proj and len(proj) > 120:
                raise ValueError("Invalid project query filter.")
            inc_pending = bool(is_owner and ((q.get("include_pending") or ["1"])[0] != "0"))
            enable_nli = bool(is_owner and ((q.get("nli") or ["0"])[0] in {"1", "true", "yes"}))
            dc_params = inspect.signature(detect_contradictions).parameters
            dc_kwargs: dict[str, object] = {
                "brain_dir": rt.brain_dir,
                "atoms_dir": rt.atoms_dir,
                "project_scope": proj,
                "include_pending": inc_pending,
            }
            if "include_preferences" in dc_params:
                dc_kwargs["include_preferences"] = bool(is_owner)
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

        if path in {"/v1/graph/full", "/v1/graph"} and method == "GET":
            from knowledge_graph import build_knowledge_graph

            proj = ((q.get("project") or [None])[0] or None)
            if proj and len(proj) > 120:
                raise ValueError("Invalid project query filter.")
            limit = _parse_bounded_int(q, "limit", default=300, min_val=1, max_val=1000)
            q_inc_pending = (q.get("include_pending") or ["0"])[0] in {"1", "true", "yes"}
            bg_kwargs_full: dict[str, object] = {
                "brain_dir": rt.brain_dir,
                "atoms_dir": rt.atoms_dir,
                "project_scope": proj,
                "include_pending": bool(is_owner and q_inc_pending),
            }
            if "include_preferences" in inspect.signature(build_knowledge_graph).parameters:
                bg_kwargs_full["include_preferences"] = bool(is_owner)
            res = build_knowledge_graph(**bg_kwargs_full)
            nodes_raw = list(res.get("nodes") or [])[:limit]
            kept_ids = {str(n.get("id") or n.get("filename") or "") for n in nodes_raw}
            nodes_norm = [
                {
                    "id": str(n.get("id") or n.get("filename") or ""),
                    "filename": str(n.get("filename") or n.get("id") or ""),
                    "label": str(n.get("claim") or n.get("label") or n.get("filename") or "")[:140],
                    "claim": str(n.get("claim") or ""),
                    "review_status": str(n.get("review_status") or "pending"),
                    "memory_type": str(n.get("memory_type") or "semantic"),
                    "contexts": list(n.get("contexts") or []),
                    "verified": bool(n.get("approved_current", n.get("verified", False))),
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
                    return {**entry, "claim": "", "evidence": "", "redacted_revoked": True}

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
                        "redacted_revoked": True,
                    }
                    if not new_is_live:
                        redacted_m["new_claim"] = ""
                        redacted_m["after_claim"] = ""
                        redacted_m["new_evidence"] = ""
                        redacted_m["after_evidence"] = ""
                    modified.append(redacted_m)

                raw_pb_changes = dict(res.get("playbook_changes") or {})
                pb_added = []
                for item in raw_pb_changes.get("added") or []:
                    pb_rep = _live_active_playbook_report(item.get("filename"), rt)
                    expected_sha = item.get("new_sha256") or item.get("old_sha256")
                    if not pb_rep or pb_rep.sha256 != expected_sha:
                        pb_added.append({**item, "steps": [], "title": "", "redacted_revoked": True})
                    else:
                        pb_added.append(item)

                pb_removed = []
                for item in raw_pb_changes.get("removed") or []:
                    pb_rep = _live_active_playbook_report(item.get("filename"), rt)
                    expected_sha = item.get("new_sha256") or item.get("old_sha256")
                    if not pb_rep or pb_rep.sha256 != expected_sha:
                        pb_removed.append({**item, "steps": [], "title": "", "redacted_revoked": True})
                    else:
                        pb_removed.append(item)

                pb_modified = []
                for item in raw_pb_changes.get("modified") or []:
                    pb_rep = _live_active_playbook_report(item.get("filename"), rt)
                    redacted_pb = {**item, "old_steps": [], "redacted_revoked": True}
                    if not pb_rep or pb_rep.sha256 != item.get("new_sha256"):
                        redacted_pb["new_steps"] = []
                        redacted_pb["title"] = ""
                    pb_modified.append(redacted_pb)

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

            raw_limit = body.get("top_k") if "top_k" in body else body.get("limit", 5)
            if isinstance(raw_limit, bool) or not isinstance(raw_limit, (int, str)):
                raise ValueError("'limit' must be an integer between 1 and 25.")
            top_k = max(1, min(int(str(raw_limit).strip()[:9]), 25))
            proj_scope = _str(body, "project_scope", max_len=120, required=False).strip() or None
            inc_prefs = bool(is_owner and body.get("include_preferences", True))
            ras_params = inspect.signature(recall_at_snapshot).parameters
            ras_kwargs: dict[str, object] = {
                "project_scope": proj_scope,
                "brain_dir": rt.brain_dir,
            }
            if "include_preferences" in ras_params:
                ras_kwargs["include_preferences"] = inc_prefs
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
            bearer_token_env = _str(body, "bearer_token_env", max_len=80, required=False).strip() or None
            res = init_sync(
                target,
                backend_type=backend_type,
                brain_dir=rt.brain_dir,
                device_name=device_name,
                passphrase=passphrase,
                bearer_token_env=bearer_token_env,
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
            bearer_token_env = _str(body, "bearer_token_env", max_len=80, required=False).strip() or None
            res = join_sync(
                target,
                backend_type=backend_type,
                brain_dir=rt.brain_dir,
                device_name=device_name,
                passphrase=passphrase,
                recovery_key_b64=recovery_key_b64,
                bearer_token_env=bearer_token_env,
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

        # ── Local-First Browser Web Clipper (scoped token or owner-only on /ping,/status,/clip; owner-only on pairings) ──
        sec_dir = getattr(rt, "security_dir", None)
        if path in {"/v1/clipper/ping", "/v1/clipper/status", "/v1/clipper/clip"}:
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
                    "version": "0.3.0",
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
            raw_lim = body.get("limit", 5)
            if isinstance(raw_lim, bool) or not isinstance(raw_lim, (int, str)):
                raise VaultWorkspaceError("'limit' must be an integer between 1 and 25.")
            lim = max(1, min(int(str(raw_lim).strip()[:6]), 25))
            proj = _str(body, "project_scope", max_len=120, required=False).strip() or None
            res = federated_recall(
                q_str,
                vault_ids=vids_arg,
                project_scope=proj,
                limit=lim,
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

    except (MemoryRecallUnavailable, LexicalIndexUnavailable) as exc:
        return send(
            503,
            {
                "error": str(exc),
                "remediation": "Run 'second-brain rebuild-lexical' or start local Ollama.",
            },
        )
    except (ReviewConflictError, SyncConflictError) as exc:
        return send(409, {"error": str(exc), "conflict": True})
    except FileNotFoundError as exc:
        return send(404, {"error": str(exc)})
    except NoteError as exc:
        if str(exc).startswith("Note not found:"):
            return send(404, {"error": str(exc)})
        return send(422, {"error": str(exc)})
    except (
        PlaybookValidationError,
        ReviewTransitionError,
        SyncError,
        ClipperError,
        VaultWorkspaceError,
        ValueError,
        NotADirectoryError,
    ) as exc:
        return send(422, {"error": str(exc)})
    except (HostConsentError, SyncSecurityError, ClipperSecurityError, VaultWorkspaceSecurityError) as exc:
        return send(403, {"error": str(exc)})
    except PermissionError as exc:
        return send(403, {"error": str(exc)})
    except RuntimeError as exc:
        if "CAS mismatch" in str(exc):
            return send(409, {"error": str(exc), "conflict": True})
        raise
    return False
