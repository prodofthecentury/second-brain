# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Stdlib-only filesystem permission hardening and bounded security file reading."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import sys

if __name__ in ("security_fs", "scripts.security_fs"):
    sys.modules.setdefault("security_fs", sys.modules[__name__])
    sys.modules.setdefault("scripts.security_fs", sys.modules[__name__])

MAX_POLICY_FILE_BYTES = 65536
_CACHED_WINDOWS_USER: str | None = None
_HARDENED_WIN_PARENT_DIRS = frozenset(
    {
        "security",
        ".sidecar",
        ".second-brain-backups",
        "snapshots",
        "sync",
        "vault-tokens",
        "keychain",
        "models",
        "cross-encoder",
        "nli",
    }
)


def _is_reparse_or_link(path: str | Path) -> bool:
    """Return True if path itself (without following) is a symlink, junction, or reparse point."""
    try:
        st = os.lstat(str(path))
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    if os.name == "nt" and (getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        return True
    return False


def _current_windows_principal() -> str | None:
    global _CACHED_WINDOWS_USER
    if _CACHED_WINDOWS_USER is not None:
        return _CACHED_WINDOWS_USER or None
    if os.name != "nt":
        _CACHED_WINDOWS_USER = ""
        return None
    whoami_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "whoami.exe")
    user = ""
    if os.path.isfile(whoami_exe):
        try:
            proc = subprocess.run(
                [whoami_exe],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                user = proc.stdout.strip()
        except Exception:
            user = ""
    if not user:
        user = (os.environ.get("USERNAME") or "").strip()
    _CACHED_WINDOWS_USER = user
    return _CACHED_WINDOWS_USER or None


def restrict_security_file_permissions(path: Path | str) -> None:
    """Enforce owner-only permissions on POSIX (0o700/0o600) and Windows NTFS DACLs."""
    try:
        path = Path(path)
        if _is_reparse_or_link(str(path)):
            return
        parent = path.parent
        parent_is_link = os.path.lexists(str(parent)) and _is_reparse_or_link(str(parent))
        if os.name != "nt":
            if not parent_is_link and parent.exists():
                os.chmod(parent, 0o700)
            if path.exists():
                os.chmod(path, 0o600)
            return
        icacls_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "icacls.exe")
        if not os.path.exists(icacls_exe):
            return
        principal = _current_windows_principal()
        if not principal:
            return

        def _harden_win_dir(dir_path: Path) -> None:
            if os.path.lexists(str(dir_path)) and _is_reparse_or_link(str(dir_path)):
                return
            if not dir_path.exists():
                return
            subprocess.run(
                [
                    icacls_exe,
                    str(dir_path),
                    "/inheritance:r",
                    "/grant:r",
                    "*S-1-5-18:(OI)(CI)(F)",
                    "*S-1-5-32-544:(OI)(CI)(F)",
                    f"{principal}:(OI)(CI)(F)",
                ],
                capture_output=True,
                text=True,
                timeout=15.0,
                check=False,
            )

        if parent.parent.name in {"security", "models"}:
            _harden_win_dir(parent.parent)
        if not parent_is_link and parent.exists() and parent.name in _HARDENED_WIN_PARENT_DIRS:
            _harden_win_dir(parent)
        if path.exists():
            subprocess.run(
                [
                    icacls_exe,
                    str(path),
                    "/inheritance:r",
                    "/grant:r",
                    "*S-1-5-18:(F)",
                    "*S-1-5-32-544:(F)",
                    f"{principal}:(F)",
                ],
                capture_output=True,
                text=True,
                timeout=15.0,
                check=False,
            )
    except OSError:
        pass


def read_bounded_security_file(path: Path | str, *, max_bytes: int = MAX_POLICY_FILE_BYTES) -> str:
    """Read a security config/token file safely, rejecting symlinks/reparse points and oversized files."""
    path = Path(path)
    if os.path.lexists(str(path)) and _is_reparse_or_link(str(path)):
        raise PermissionError(f"Refusing to read security file via symlink or reparse point: {path}")
    if os.path.lexists(str(path.parent)) and _is_reparse_or_link(str(path.parent)):
        raise PermissionError(f"Refusing to read security file under symlinked directory: {path.parent}")
    stat_info = path.stat()
    if stat_info.st_size > max_bytes:
        raise PermissionError(f"Security file exceeds maximum allowed size ({max_bytes} bytes): {path}")
    return path.read_text(encoding="utf-8")
