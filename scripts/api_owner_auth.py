# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Owner token, rate limits, and operator jobs for the loopback product API.

Imported by ``scripts.api_product``. The names are re-exported there so callers
and tests keep using ``scripts.api_product``.
"""

from __future__ import annotations

from collections.abc import Callable
import hmac
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import threading
import time

import scripts.config as _config
from scripts.config import _is_reparse_or_link
from scripts.host_adapters import read_bounded_security_file, restrict_security_file_permissions

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


def owner_bearer_attestation(handler, rt, actor: str) -> dict[str, str]:
    """Bind an owner-token proof to an actor. The raw token is checked and then dropped from the audit log."""
    raw = handler.headers.get(OWNER_HEADER)
    supplied = raw.strip() if isinstance(raw, str) else ""
    expected = getattr(rt, "owner_token", None)
    if not expected:
        expected = get_owner_token(getattr(rt, "owner_token_path", None)) or ""
    token_path = getattr(rt, "owner_token_path", None)
    return {
        "method": "owner_bearer_token",
        "actor": str(actor or "").strip(),
        "token": supplied,
        "expected_token": str(expected),
        "token_path": str(token_path) if token_path else "",
    }


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
