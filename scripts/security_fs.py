# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Stdlib-only filesystem permission hardening and bounded security file reading."""

from __future__ import annotations

import errno
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


def _is_directory_nofollow(path: Path) -> bool:
    """True when path is a directory and not a symlink (symlinks are never chmod'd here)."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _posix_owner_mode(path: Path) -> int:
    """Directories need the execute bit or the owner cannot create or open files inside them."""
    return 0o700 if _is_directory_nofollow(path) else 0o600


def restrict_security_file_permissions(path: Path | str) -> None:
    """Enforce owner-only permissions on POSIX (0o700 for directories, 0o600 for files) and Windows NTFS DACLs.

    Callers pass both secret files and the directories that hold them (mobile pairing,
    ONNX downloads, inbox ledgers). A directory must keep the owner execute bit; 0o600
    on a directory removes it and the next create/open fails with PermissionError on POSIX.
    """
    try:
        path = Path(path)
        if _is_reparse_or_link(str(path)):
            return
        parent = path.parent
        parent_is_link = os.path.lexists(str(parent)) and _is_reparse_or_link(str(parent))
        if os.name != "nt":
            if not parent_is_link and parent.exists():
                os.chmod(parent, _posix_owner_mode(parent))
            if path.exists():
                os.chmod(path, _posix_owner_mode(path))
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
            if _is_directory_nofollow(path):
                _harden_win_dir(path)
            else:
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
    """Read a security config/token file safely via O_NOFOLLOW + fstat(fd), rejecting symlinks/reparse points and oversized files."""
    p = Path(path)
    eff_max = max(0, int(max_bytes))
    if os.path.lexists(str(p)) and _is_reparse_or_link(str(p)):
        raise PermissionError(f"Refusing to read security file via symlink or reparse point: {p}")
    seen_ancestors: set[Path] = {p}
    for anc in (p.parent, p.parent.parent):
        if anc in seen_ancestors:
            continue
        seen_ancestors.add(anc)
        if os.path.lexists(str(anc)) and _is_reparse_or_link(str(anc)):
            raise PermissionError(f"Refusing to read security file under symlinked directory: {anc}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(p), flags)
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ELOOP:
            raise PermissionError(f"Refusing to read security file via symlink or reparse point: {p}") from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PermissionError(f"Refusing to read non-regular security file: {p}")
        if st.st_size > eff_max:
            raise PermissionError(f"Security file exceeds maximum allowed size ({eff_max} bytes): {p}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(8192, (eff_max + 1) - total))
            if not chunk:
                break
            total += len(chunk)
            if total > eff_max:
                raise PermissionError(f"Security file exceeds maximum allowed size ({eff_max} bytes): {p}")
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8")
