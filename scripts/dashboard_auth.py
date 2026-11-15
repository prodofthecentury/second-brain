# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""Local single-owner dashboard password gate and credential setup command."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
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
SESSION_COOKIE_POLICY = {
    "HttpOnly": True,
    "SameSite": "Strict",
    "Path": "/",
}
_RATE_LIMIT_TRACKER: dict[str, dict[str, float | int]] = {}
_RATE_LIMIT_LOCK = threading.Lock()


def assert_loopback_address(address: str | None) -> str:
    """Enforce strict IPv4 loopback binding (127.0.0.1 only; reject None, 0.0.0.0, or external interfaces)."""
    if not isinstance(address, str) or address.strip() != ALLOWED_LOOPBACK_ADDRESS:
        raise PermissionError(
            f"403 Forbidden: Dashboard listener must bind strictly to {ALLOWED_LOOPBACK_ADDRESS} (got {address!r})."
        )
    return ALLOWED_LOOPBACK_ADDRESS


def format_session_cookie(session_token: str) -> str:
    """Format a hardened local session cookie header with HttpOnly and SameSite=Strict."""
    if not session_token or any(ch in session_token for ch in (";", "\r", "\n", " ")):
        raise ValueError("Invalid session token.")
    return f"sb_session={session_token}; HttpOnly; SameSite=Strict; Path=/"


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

    resolved_key = str(Path(auth_path or default_auth_path()))
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
        if time.time() - last_activity <= SESSION_IDLE_SECONDS:
            state["_brain_dashboard_last_activity"] = time.time()
            st.sidebar.caption("Owner session unlocked")
            if st.sidebar.button("Lock dashboard", key="brain-dashboard-lock"):
                state.pop("_brain_dashboard_auth_fingerprint", None)
                state.pop("_brain_dashboard_last_activity", None)
                st.rerun()
            return True
        state.pop("_brain_dashboard_auth_fingerprint", None)
        state.pop("_brain_dashboard_last_activity", None)

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
            state["_brain_dashboard_auth_fingerprint"] = fingerprint
            state["_brain_dashboard_last_activity"] = time.time()
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
