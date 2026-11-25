# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Local single-owner dashboard password gate and credential setup command."""

from __future__ import annotations

import argparse
import gc
import getpass
import hashlib
import hmac
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 1024
SCRYPT_MAXMEM = 64 * 1024 * 1024
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SESSION_IDLE_SECONDS = 30 * 60
MAX_SESSION_FAILURES = 5
SESSION_LOCK_SECONDS = 60
ALLOWED_LOOPBACK_ADDRESS = "127.0.0.1"
SESSION_COOKIE_NAME = "sb_session"
SESSION_COOKIE_ENDPOINT = "/_sb_session_cookie"
COOKIE_EXCHANGE_TTL_SECONDS = 60.0
SESSION_COOKIE_POLICY = {
    "HttpOnly": True,
    "SameSite": "Strict",
    "Path": "/",
}
_RATE_LIMIT_TRACKER: dict[str, dict[str, float | int]] = {}
_RATE_LIMIT_LOCK = threading.Lock()
_SESSION_LOCK = threading.Lock()
_PROCESS_SESSION_SECRET: bytes = secrets.token_bytes(32)
_ACTIVE_SESSIONS: dict[str, dict[str, object]] = {}
_PENDING_COOKIE_EXCHANGES: dict[str, dict[str, object]] = {}
_ROUTE_INSTALLED_APP_IDS: set[int] = set()


def assert_loopback_address(address: str | None) -> str:
    """Enforce strict IPv4 loopback binding (127.0.0.1 only; reject None, 0.0.0.0, or external interfaces)."""
    if not isinstance(address, str) or address.strip() != ALLOWED_LOOPBACK_ADDRESS:
        raise PermissionError(
            f"403 Forbidden: Dashboard listener must bind strictly to {ALLOWED_LOOPBACK_ADDRESS} (got {address!r})."
        )
    return ALLOWED_LOOPBACK_ADDRESS


def format_session_cookie(session_token: str, *, max_age: int | None = None) -> str:
    """Format a hardened local session cookie header with HttpOnly and SameSite=Strict."""
    clearing = max_age is not None and int(max_age) <= 0 and session_token == ""
    if not clearing and (not session_token or any(ch in session_token for ch in (";", "\r", "\n", " "))):
        raise ValueError("Invalid session token.")
    if any(ch in str(session_token) for ch in (";", "\r", "\n", " ")):
        raise ValueError("Invalid session token.")
    parts = [
        f"{SESSION_COOKIE_NAME}={session_token}",
        "HttpOnly",
        "SameSite=Strict",
        "Path=/",
    ]
    if max_age is not None:
        parts.append(f"Max-Age={max(0, int(max_age))}")
    return "; ".join(parts)


def _token_digest(session_token: str) -> str:
    return hashlib.sha256(session_token.encode("utf-8")).hexdigest()


def _normalize_auth_key(auth_path: str | Path | None = None) -> str:
    raw = Path(auth_path or default_auth_path()).expanduser()
    return os.path.abspath(str(raw))


def _session_hmac(nonce: str, fingerprint: str, auth_key: str) -> str:
    msg = f"{nonce}:{fingerprint}:{auth_key}".encode("utf-8")
    return hmac.new(_PROCESS_SESSION_SECRET, msg, hashlib.sha256).hexdigest()


def _session_store_path(auth_path: str | Path | None = None) -> Path:
    if auth_path is not None or os.environ.get("BRAIN_DASHBOARD_AUTH_FILE"):
        base_dir = Path(auth_path or default_auth_path()).expanduser().resolve().parent
        return base_dir / "dashboard-sessions.json"
    from config import security_state_dir

    return Path(security_state_dir()) / "dashboard-sessions.json"


def _persist_session_store(auth_path: str | Path | None = None) -> None:
    """Write non-secret session digests & expiry metadata to a 0600 security-state file."""
    from config import _is_reparse_or_link
    from host_adapters import restrict_security_file_permissions

    store_path = _session_store_path(auth_path)
    try:
        if os.path.lexists(str(store_path)) and _is_reparse_or_link(str(store_path)):
            return
        if os.path.lexists(str(store_path.parent)) and _is_reparse_or_link(str(store_path.parent)):
            return
        store_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if _is_reparse_or_link(str(store_path.parent)) or (
            os.path.lexists(str(store_path)) and _is_reparse_or_link(str(store_path))
        ):
            return
        resolved_auth = _normalize_auth_key(auth_path)
        cur = time.time()
        snapshot = {
            digest: {
                "fingerprint": entry["fingerprint"],
                "created_at": entry["created_at"],
                "last_activity": entry["last_activity"],
                "expires_at": entry["expires_at"],
            }
            for digest, entry in _ACTIVE_SESSIONS.items()
            if entry.get("auth_key") == resolved_auth and cur <= float(entry.get("expires_at", 0.0))
        }
        payload = {"schema_version": 1, "sessions": snapshot}
        fd, tmp = tempfile.mkstemp(prefix=".dashboard-sessions-", suffix=".tmp", dir=store_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(payload, fh, sort_keys=True)
                fh.write("\n")
            if os.name != "nt":
                os.chmod(tmp, 0o600)
            os.replace(tmp, store_path)
            restrict_security_file_permissions(store_path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    except Exception:
        # Process memory remains authoritative; never crash auth flow if sidecar disk write is blocked.
        pass


def create_session_token(
    fingerprint: str,
    *,
    auth_path: str | Path | None = None,
    ttl_seconds: int = SESSION_IDLE_SECONDS,
    now: float | None = None,
) -> str:
    """Mint a signed loopback session token bound to this credential fingerprint."""
    cur = float(time.time() if now is None else now)
    auth_key = _normalize_auth_key(auth_path)
    nonce = secrets.token_urlsafe(24)
    sig = _session_hmac(nonce, fingerprint, auth_key)
    token = f"{nonce}.{sig}"
    digest = _token_digest(token)
    with _SESSION_LOCK:
        expired = [k for k, v in _ACTIVE_SESSIONS.items() if cur > float(v.get("expires_at", 0.0))]
        for k in expired:
            _ACTIVE_SESSIONS.pop(k, None)
        _ACTIVE_SESSIONS[digest] = {
            "fingerprint": fingerprint,
            "auth_key": auth_key,
            "created_at": cur,
            "last_activity": cur,
            "expires_at": cur + max(1, int(ttl_seconds)),
        }
        _persist_session_store(auth_path)
    return token


def verify_session_token(
    session_token: str,
    expected_fingerprint: str,
    *,
    auth_path: str | Path | None = None,
    touch: bool = True,
    now: float | None = None,
) -> bool:
    """Validate HMAC signature, credential fingerprint, revocation status, and idle timeout."""
    if not isinstance(session_token, str) or "." not in session_token:
        return False
    if any(ch in session_token for ch in (";", "\r", "\n", " ")):
        return False
    nonce, provided_sig = session_token.split(".", 1)
    if not nonce or not provided_sig:
        return False
    auth_key = _normalize_auth_key(auth_path)
    expected_sig = _session_hmac(nonce, expected_fingerprint, auth_key)
    if not hmac.compare_digest(provided_sig, expected_sig):
        return False
    cur = float(time.time() if now is None else now)
    digest = _token_digest(session_token)
    with _SESSION_LOCK:
        entry = _ACTIVE_SESSIONS.get(digest)
        if not isinstance(entry, dict):
            return False
        if entry.get("fingerprint") != expected_fingerprint or entry.get("auth_key") != auth_key:
            return False
        last_act = float(entry.get("last_activity", 0.0))
        expires_at = float(entry.get("expires_at", 0.0))
        if (cur - last_act) > SESSION_IDLE_SECONDS or cur > expires_at:
            _ACTIVE_SESSIONS.pop(digest, None)
            _persist_session_store(auth_path)
            return False
        if touch:
            entry["last_activity"] = cur
            entry["expires_at"] = cur + SESSION_IDLE_SECONDS
    return True


def revoke_session_token(session_token: str | None, *, auth_path: str | Path | None = None) -> None:
    """Revoke an active dashboard session token and any pending cookie exchange codes."""
    if not session_token or not isinstance(session_token, str):
        return
    digest = _token_digest(session_token)
    with _SESSION_LOCK:
        _ACTIVE_SESSIONS.pop(digest, None)
        stale_codes = [c for c, v in _PENDING_COOKIE_EXCHANGES.items() if v.get("token") == session_token]
        for c in stale_codes:
            _PENDING_COOKIE_EXCHANGES.pop(c, None)
        _persist_session_store(auth_path)


def _mint_cookie_exchange(token: str, *, action: str = "set") -> str:
    cur = time.time()
    code = secrets.token_urlsafe(24)
    with _SESSION_LOCK:
        expired = [k for k, v in _PENDING_COOKIE_EXCHANGES.items() if cur > float(v.get("expires_at", 0.0))]
        for k in expired:
            _PENDING_COOKIE_EXCHANGES.pop(k, None)
        _PENDING_COOKIE_EXCHANGES[code] = {
            "token": token,
            "action": action,
            "expires_at": cur + COOKIE_EXCHANGE_TTL_SECONDS,
        }
    return code


def _install_starlette_cookie_routes() -> bool:
    """Attach the same-origin HttpOnly cookie delivery endpoint to Streamlit's Starlette app when running."""
    try:
        from starlette.applications import Starlette
        from starlette.responses import HTMLResponse, Response
        from starlette.routing import Route
    except Exception:
        return False

    async def _cookie_endpoint(request) -> Response:
        host = (request.headers.get("host") or "").strip().lower()
        if (
            host
            and host not in {"127.0.0.1", "localhost", "[::1]"}
            and not host.startswith(("127.0.0.1:", "localhost:", "[::1]:"))
        ):
            return Response(status_code=403, headers={"Cache-Control": "no-store"})
        sec_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
        if sec_site not in {"", "same-origin", "none"}:
            return Response(status_code=403, headers={"Cache-Control": "no-store"})
        code = (request.query_params.get("code") or "").strip()
        if not code:
            return Response(status_code=403, headers={"Cache-Control": "no-store"})
        cur = time.time()
        with _SESSION_LOCK:
            entry = _PENDING_COOKIE_EXCHANGES.pop(code, None)
        if not isinstance(entry, dict) or cur > float(entry.get("expires_at", 0.0)):
            return Response(status_code=403, headers={"Cache-Control": "no-store"})
        action = str(entry.get("action") or "set")
        if action == "clear":
            cookie_header = format_session_cookie("", max_age=0)
        else:
            cookie_header = format_session_cookie(str(entry.get("token") or ""), max_age=SESSION_IDLE_SECONDS)
        return HTMLResponse(
            "<!doctype html><html><head><meta charset='utf-8'></head><body></body></html>",
            status_code=200,
            headers={"Set-Cookie": cookie_header, "Cache-Control": "no-store"},
        )

    installed = False
    with _SESSION_LOCK:
        for obj in gc.get_objects():
            try:
                if isinstance(obj, Starlette):
                    app_id = id(obj)
                    if app_id not in _ROUTE_INSTALLED_APP_IDS:
                        existing_paths = {getattr(r, "path", None) for r in getattr(obj.router, "routes", ())}
                        if SESSION_COOKIE_ENDPOINT not in existing_paths:
                            obj.router.routes.insert(
                                0,
                                Route(SESSION_COOKIE_ENDPOINT, endpoint=_cookie_endpoint, methods=["GET"]),
                            )
                        _ROUTE_INSTALLED_APP_IDS.add(app_id)
                    installed = True
            except Exception:
                continue
    return installed


def _deliver_cookie_exchange(st, code: str) -> None:
    """Trigger a hidden same-origin GET to set or clear the browser's HttpOnly session cookie."""
    if not code:
        return
    runtime_obj = getattr(st, "runtime", None)
    exists_fn = getattr(runtime_obj, "exists", None)
    if not callable(exists_fn) or not exists_fn():
        return
    if not _install_starlette_cookie_routes():
        return
    iframe_fn = getattr(st, "iframe", None)
    if callable(iframe_fn):
        try:
            iframe_fn(f"{SESSION_COOKIE_ENDPOINT}?code={code}", width=1, height=1)
        except Exception:
            pass


def _extract_request_session_cookie(st) -> str | None:
    """Read the browser's `sb_session` cookie from Streamlit's request context when available."""
    ctx = getattr(st, "context", None)
    if ctx is None:
        return None
    try:
        cookies = getattr(ctx, "cookies", None)
    except Exception:
        cookies = None
    if cookies is not None:
        try:
            val = cookies.get(SESSION_COOKIE_NAME)
            if isinstance(val, str) and val.strip():
                return val.strip()
        except Exception:
            pass
    try:
        headers = getattr(ctx, "headers", None)
    except Exception:
        headers = None
    if headers is not None:
        try:
            raw_cookie = headers.get("cookie") or headers.get("Cookie")
            if isinstance(raw_cookie, str) and raw_cookie.strip():
                jar = SimpleCookie()
                jar.load(raw_cookie)
                morsel = jar.get(SESSION_COOKIE_NAME)
                if morsel is not None and morsel.value:
                    return morsel.value.strip()
        except Exception:
            pass
    return None


def logout_dashboard_session(st, auth_path: str | Path | None = None) -> None:
    """Revoke the active dashboard session, emit an expiring Set-Cookie header, and clear session state."""
    state = st.session_state
    active_token = state.pop("_brain_dashboard_session_token", None) or _extract_request_session_cookie(st)
    revoke_session_token(active_token, auth_path=auth_path)
    state["_brain_dashboard_session_cookie"] = format_session_cookie("", max_age=0)
    state["_brain_dashboard_pending_clear_cookie_code"] = _mint_cookie_exchange("", action="clear")
    state.pop("_brain_dashboard_auth_fingerprint", None)
    state.pop("_brain_dashboard_last_activity", None)
    state.pop("_brain_dashboard_pending_cookie_code", None)


def default_auth_path() -> Path:
    override = os.environ.get("BRAIN_DASHBOARD_AUTH_FILE")
    if override:
        return Path(os.path.abspath(str(Path(override).expanduser())))
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    elif os.environ.get("XDG_STATE_HOME"):
        base = Path(os.environ["XDG_STATE_HOME"]).expanduser()
    else:
        base = Path.home() / ".local" / "state"
    return base / "SecondBrain" / "security" / "dashboard-auth.json"


def build_password_record(password: str) -> dict:
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must contain at least {PASSWORD_MIN_LENGTH} characters.")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise ValueError(f"Password must not exceed {PASSWORD_MAX_LENGTH} characters.")
    salt = secrets.token_bytes(16)
    password_hash = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM, dklen=SCRYPT_DKLEN,
    )
    return {
        "schema_version": 1,
        "algorithm": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": salt.hex(),
        "password_hash": password_hash.hex(),
    }


def _valid_record(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    try:
        return (
            record.get("schema_version") == 1
            and record.get("algorithm") == "scrypt"
            and record.get("n") == SCRYPT_N
            and record.get("r") == SCRYPT_R
            and record.get("p") == SCRYPT_P
            and len(bytes.fromhex(record.get("salt", ""))) == 16
            and len(bytes.fromhex(record.get("password_hash", ""))) == SCRYPT_DKLEN
        )
    except (TypeError, ValueError):
        return False


def verify_password(password: str, record: object) -> bool:
    if not _valid_record(record) or not isinstance(password, str) or len(password) > PASSWORD_MAX_LENGTH:
        return False
    try:
        calculated = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(record["salt"]),
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            maxmem=SCRYPT_MAXMEM,
            dklen=SCRYPT_DKLEN,
        )
        return hmac.compare_digest(calculated, bytes.fromhex(record["password_hash"]))
    except (ValueError, TypeError, MemoryError):
        return False


def save_password_record(record: dict, auth_path: str | Path | None = None) -> Path:
    from config import _is_reparse_or_link
    from host_adapters import restrict_security_file_permissions

    if not _valid_record(record):
        raise ValueError("Refusing to save an invalid dashboard password record.")
    raw_path = Path(auth_path or default_auth_path()).expanduser()
    path = Path(os.path.abspath(str(raw_path)))
    if (os.path.lexists(str(path)) and _is_reparse_or_link(str(path))) or (
        os.path.lexists(str(path.parent)) and _is_reparse_or_link(str(path.parent))
    ):
        raise PermissionError("Refusing to write dashboard auth record through a symlink or reparse point.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _is_reparse_or_link(str(path.parent)) or (os.path.lexists(str(path)) and _is_reparse_or_link(str(path))):
        raise PermissionError("Refusing to write dashboard auth record through a symlink or reparse point.")
    try:
        if os.name != "nt":
            os.chmod(path.parent, 0o700)
    except OSError:
        pass
    descriptor, temporary = tempfile.mkstemp(prefix=".dashboard-auth-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            json.dump(record, file, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        try:
            if os.name != "nt":
                os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        restrict_security_file_permissions(path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return path


def load_password_record(auth_path: str | Path | None = None) -> dict | None:
    from config import _is_reparse_or_link

    path = Path(auth_path or default_auth_path())
    try:
        if not os.path.lexists(str(path)) or _is_reparse_or_link(str(path)) or _is_reparse_or_link(str(path.parent)):
            return None
        if path.stat().st_size > 4096:
            return None
        with path.open("r", encoding="utf-8") as file:
            record = json.load(file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return record if _valid_record(record) else None


def _record_fingerprint(record: dict) -> str:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def require_dashboard_login(st, auth_path: str | Path | None = None) -> bool:
    """Render a fail-closed login screen; return true only for an authenticated session."""
    get_opt = getattr(st, "get_option", None)
    if callable(get_opt):
        try:
            assert_loopback_address(get_opt("server.address"))
        except PermissionError as exc:
            st.error(str(exc))
            return False

    resolved_key = _normalize_auth_key(auth_path)
    with _RATE_LIMIT_LOCK:
        tracker = _RATE_LIMIT_TRACKER.setdefault(resolved_key, {"failures": 0, "locked_until": 0.0})
        tracked_locked_until = float(tracker.get("locked_until", 0.0))

    record = load_password_record(auth_path)
    if record is None:
        st.title("Second Brain is locked")
        st.error("Dashboard password is not configured or the credential file is invalid.")
        st.code("uv run --locked python scripts/dashboard_auth.py set-password", language="powershell")
        return False

    fingerprint = _record_fingerprint(record)
    state = st.session_state
    if state.pop("_brain_dashboard_clear_password", False):
        state.pop("brain_dashboard_password", None)
    if state.get("_brain_dashboard_auth_fingerprint") == fingerprint:
        last_activity = float(state.get("_brain_dashboard_last_activity", 0))
        active_token = state.get("_brain_dashboard_session_token")
        token_ok = (
            True
            if active_token is None
            else verify_session_token(active_token, fingerprint, auth_path=auth_path, touch=True)
        )
        if token_ok and (time.time() - last_activity <= SESSION_IDLE_SECONDS):
            state["_brain_dashboard_last_activity"] = time.time()
            pending_set = state.pop("_brain_dashboard_pending_cookie_code", None)
            if isinstance(pending_set, str) and pending_set:
                _deliver_cookie_exchange(st, pending_set)
            st.sidebar.caption("Owner session unlocked")
            if st.sidebar.button("Lock dashboard", key="brain-dashboard-lock"):
                logout_dashboard_session(st, auth_path=auth_path)
                st.rerun()
            return True
        logout_dashboard_session(st, auth_path=auth_path)

    cookie_token = _extract_request_session_cookie(st)
    if cookie_token and verify_session_token(cookie_token, fingerprint, auth_path=auth_path, touch=True):
        state["_brain_dashboard_auth_fingerprint"] = fingerprint
        state["_brain_dashboard_last_activity"] = time.time()
        state["_brain_dashboard_session_token"] = cookie_token
        state["_brain_dashboard_session_cookie"] = format_session_cookie(
            cookie_token, max_age=SESSION_IDLE_SECONDS
        )
        st.sidebar.caption("Owner session unlocked")
        if st.sidebar.button("Lock dashboard", key="brain-dashboard-lock"):
            logout_dashboard_session(st, auth_path=auth_path)
            st.rerun()
        return True

    pending_clear = state.pop("_brain_dashboard_pending_clear_cookie_code", None)
    if isinstance(pending_clear, str) and pending_clear:
        _deliver_cookie_exchange(st, pending_clear)

    st.title("Unlock Second Brain")
    st.caption("This dashboard is local-only. Unlocking lasts until logout or 30 minutes without activity.")
    error = state.pop("_brain_dashboard_login_error", None)
    if error:
        st.error(error)
    locked_until = max(float(state.get("_brain_dashboard_locked_until", 0)), tracked_locked_until)
    if time.time() < locked_until:
        state["_brain_dashboard_locked_until"] = locked_until
        st.warning("Too many failed attempts. Wait one minute and try again.")
        return False
    with st.form("brain-dashboard-login", clear_on_submit=True):
        st.text_input("Owner password", type="password", key="brain_dashboard_password", max_chars=PASSWORD_MAX_LENGTH)
        submitted = st.form_submit_button("Unlock dashboard")
    if submitted:
        password = state.get("brain_dashboard_password", "")
        state["_brain_dashboard_clear_password"] = True
        if verify_password(password, record):
            session_token = create_session_token(fingerprint, auth_path=auth_path)
            state["_brain_dashboard_auth_fingerprint"] = fingerprint
            state["_brain_dashboard_last_activity"] = time.time()
            state["_brain_dashboard_session_token"] = session_token
            state["_brain_dashboard_session_cookie"] = format_session_cookie(
                session_token, max_age=SESSION_IDLE_SECONDS
            )
            state["_brain_dashboard_pending_cookie_code"] = _mint_cookie_exchange(
                session_token, action="set"
            )
            state["_brain_dashboard_login_failures"] = 0
            with _RATE_LIMIT_LOCK:
                tracker = _RATE_LIMIT_TRACKER.setdefault(resolved_key, {"failures": 0, "locked_until": 0.0})
                tracker["failures"] = 0
                tracker["locked_until"] = 0.0
            state.pop("_brain_dashboard_locked_until", None)
            st.rerun()
        with _RATE_LIMIT_LOCK:
            tracker = _RATE_LIMIT_TRACKER.setdefault(resolved_key, {"failures": 0, "locked_until": 0.0})
            failures = max(int(state.get("_brain_dashboard_login_failures", 0)), int(tracker.get("failures", 0))) + 1
            state["_brain_dashboard_login_failures"] = failures
            tracker["failures"] = failures
            if failures >= MAX_SESSION_FAILURES:
                lock_deadline = time.time() + SESSION_LOCK_SECONDS
                state["_brain_dashboard_locked_until"] = lock_deadline
                tracker["locked_until"] = lock_deadline
                state["_brain_dashboard_login_failures"] = 0
                tracker["failures"] = 0
        state["_brain_dashboard_login_error"] = "Password did not match."
        st.rerun()
    return False


def configure_password_interactively(auth_path: str | Path | None = None) -> bool:
    first = getpass.getpass("Create dashboard password (12+ characters): ")
    second = getpass.getpass("Confirm dashboard password: ")
    if not hmac.compare_digest(first, second):
        print("Passwords do not match.", file=sys.stderr)
        return False
    try:
        record = build_password_record(first)
        path = save_password_record(record, auth_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return False
    print(f"Dashboard password configured. Credential verifier stored at {path}.")
    print("Keep the password private. Reset it with: uv run --locked python scripts/dashboard_auth.py set-password")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices={"ensure-configured", "set-password"})
    parser.add_argument("--auth-file", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    path = Path(args.auth_file).expanduser() if args.auth_file else default_auth_path()
    if args.command == "ensure-configured" and load_password_record(path) is not None:
        print("Dashboard password is already configured.")
        return 0
    return 0 if configure_password_interactively(path) else 1


if __name__ == "__main__":
    raise SystemExit(main())
