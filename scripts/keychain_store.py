# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Cross-Platform Native Secret Sealing (Windows DPAPI / macOS Keychain / Linux Secret Service).

Seals 256-bit vault sync master keys outside `brain/` so plaintext key material
is never persisted in JSON configuration files:
  * Windows (`win32`): Native Win32 Data Protection API (`CryptProtectData` /
    `CryptUnprotectData` in `crypt32.dll` with `CRYPTPROTECT_UI_FORBIDDEN` and
    application-specific entropy), writing only opaque DPAPI-encrypted blobs to
    `<security_state_dir>/keychain/*.dpapi` protected by NTFS user-only DACLs.
  * macOS (`darwin`): `/usr/bin/security -i` (stdin mode, zero `argv` exposure)
    when running interactively against the login keychain, with restricted `0600`
    local fallback when isolated `state_dir` or headless CI is active.
  * Linux (`linux`): `secret-tool` (`libsecret` D-Bus Secret Service via stdin,
    zero `argv` exposure) when available, with restricted `0600` local fallback
    in headless environments.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import _is_reparse_or_link, security_state_dir  # noqa: E402
from host_adapters import restrict_security_file_permissions  # noqa: E402


class KeychainError(RuntimeError):
    """Raised on secret sealing, unsealing, or path integrity failures."""


class KeychainUnavailableError(KeychainError):
    """Raised when a requested native OS keychain backend is unavailable."""


_NAME_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_DPAPI_ENTROP = b"SecondBrain-VaultSync-MasterKey-v1"
_DPAPI_ENTROPY = _DPAPI_ENTROP
_DPAPI_FILE_MAGIC = b"SBDP1\x00"
_FALLBACK_FILE_MAGIC = b"SBKF1\x00"
_SEALED_FALLBACK_MAGIC = b"SBKF2\x00"
_CRYPTPROTECT_UI_FORBIDDEN = 0x00000001
MAX_SECRET_BYTES = 4096
NATIVE_KEYCHAIN_BACKENDS = frozenset({"windows_dpapi", "macos_keychain", "linux_secret_service"})


def _validate_identifier(value: str, label: str) -> str:
    cleaned = (value or "").strip()
    if not _NAME_RE.fullmatch(cleaned):
        raise KeychainError(f"Invalid keychain {label}: {value!r}")
    return cleaned


def _keychain_dir(state_dir: Path | None = None) -> Path:
    base = Path(state_dir) if state_dir is not None else security_state_dir()
    return base / "keychain"


def _entry_slug(service: str, account: str) -> str:
    digest = hashlib.sha256(f"{service}\x00{account}".encode("utf-8")).hexdigest()
    return digest[:32]


def _assert_safe_keychain_file(path: Path) -> None:
    for candidate in (path, *path.parents):
        if os.path.lexists(str(candidate)) and _is_reparse_or_link(str(candidate)):
            raise KeychainError(f"Refusing to access keychain secret through symlink or reparse point: {candidate}")


def _assert_regular_secret_file(path: Path) -> os.stat_result:
    _assert_safe_keychain_file(path)
    st = os.lstat(str(path))
    if not stat.S_ISREG(st.st_mode):
        raise KeychainError(f"Refusing to read non-regular keychain file: {path}")
    return st


def _atomic_write_secret_file(path: Path, raw_bytes: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_safe_keychain_file(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    fd_closed = False
    try:
        if os.name != "nt":
            os.chmod(tmp_name, 0o600)
        if os.write(fd, raw_bytes) != len(raw_bytes):
            raise OSError(f"Incomplete write to {tmp_name}")
        os.fsync(fd)
        os.close(fd)
        fd_closed = True
        os.replace(tmp_name, str(path))
        restrict_security_file_permissions(path)
    finally:
        if not fd_closed:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass


# ─── Windows DPAPI (`crypt32.dll`) ───────────────────────────────────────────

if sys.platform == "win32":

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL

    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL

    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p


def _make_blob(data: bytes) -> tuple[ Any, ctypes.Array[ctypes.c_ubyte] ]:
    buf = (ctypes.c_ubyte * max(1, len(data)))()
    if data:
        ctypes.memmove(buf, data, len(data))
    blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buf


def dpapi_protect_bytes(plaintext: bytes, *, entropy: bytes = _DPAPI_ENTROPY) -> bytes:
    """Encrypt bytes with Windows CurrentUser DPAPI + application entropy (headless-safe)."""
    if sys.platform != "win32":
        raise KeychainUnavailableError("Windows DPAPI is only available on win32.")
    if not isinstance(plaintext, (bytes, bytearray)) or not plaintext:
        raise KeychainError("Cannot seal empty secret.")
    in_blob, in_buf = _make_blob(bytes(plaintext))
    ent_blob, _ent_buf = _make_blob(entropy)
    out_blob = _DATA_BLOB()
    try:
        ok = _crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "SecondBrainVaultMasterKey",
            ctypes.byref(ent_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            err = ctypes.get_last_error()
            raise KeychainError(f"Windows DPAPI CryptProtectData failed (winerr={err}).")
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.memset(in_buf, 0, len(in_buf))
        if out_blob.pbData:
            _kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


def dpapi_unprotect_bytes(ciphertext: bytes, *, entropy: bytes = _DPAPI_ENTROPY) -> bytes:
    """Decrypt bytes sealed with Windows CurrentUser DPAPI + application entropy."""
    if sys.platform != "win32":
        raise KeychainUnavailableError("Windows DPAPI is only available on win32.")
    if not isinstance(ciphertext, (bytes, bytearray)) or not ciphertext:
        raise KeychainError("Cannot unseal empty DPAPI blob.")
    in_blob, _in_buf = _make_blob(bytes(ciphertext))
    ent_blob, _ent_buf = _make_blob(entropy)
    out_blob = _DATA_BLOB()
    try:
        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            ctypes.byref(ent_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            err = ctypes.get_last_error()
            raise KeychainError(
                f"Windows DPAPI CryptUnprotectData failed (winerr={err}; wrong Windows user, machine, or tampered blob)."
            )
        result = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return result
    finally:
        if out_blob.pbData:
            if out_blob.cbData:
                ctypes.memset(out_blob.pbData, 0, out_blob.cbData)
            _kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


# ─── macOS Keychain & Linux Secret Service Helpers ───────────────────────────

def _scoped_os_service(service: str, state_dir: Path | None) -> str:
    """Namespace OS keychain service by `state_dir` when a non-default state directory is used."""
    if state_dir is None:
        return service
    try:
        resolved_custom = Path(state_dir).resolve()
        resolved_default = security_state_dir().resolve()
    except OSError:
        resolved_custom = Path(os.path.abspath(str(state_dir)))
        resolved_default = Path(os.path.abspath(str(security_state_dir())))
    if os.path.normcase(str(resolved_custom)) == os.path.normcase(str(resolved_default)):
        return service
    state_hash = hashlib.sha256(os.path.normcase(str(resolved_custom)).encode("utf-8")).hexdigest()[:12]
    suffix = f".sd-{state_hash}"
    base = service[: max(1, 128 - len(suffix))]
    return f"{base}{suffix}"


def _macos_security_available(state_dir: Path | None = None) -> bool:
    _ = state_dir
    return sys.platform == "darwin" and os.path.isfile("/usr/bin/security")


def _linux_secret_tool_available(state_dir: Path | None = None) -> bool:
    _ = state_dir
    return (
        sys.platform.startswith("linux")
        and bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))
        and shutil.which("secret-tool") is not None
    )


def _derive_local_wrap_subkey(kdir: Path, service: str, account: str, *, create: bool) -> bytes | None:
    wrap_path = kdir / ".wrap.key"
    if os.path.lexists(str(wrap_path)):
        st = _assert_regular_secret_file(wrap_path)
        if st.st_size != 32:
            raise KeychainError(f"Corrupted local wrap key length ({st.st_size} bytes): {wrap_path}")
        root_key = wrap_path.read_bytes()
    else:
        if not create:
            return None
        root_key = os.urandom(32)
        _atomic_write_secret_file(wrap_path, root_key)
    uid_tag = str(os.getuid()) if hasattr(os, "getuid") else (os.environ.get("USERNAME") or "local")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"SecondBrain-LocalWrap-Salt-v2",
        info=f"sb-keyfile-v2:{uid_tag}:{service}:{account}".encode("utf-8"),
    ).derive(root_key)


def _seal_fallback_blob(kdir: Path, service: str, account: str, plaintext: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    subkey = _derive_local_wrap_subkey(kdir, service, account, create=True)
    if subkey is None:
        raise KeychainError("Failed to initialize local wrap key.")
    nonce = os.urandom(12)
    aad = f"SBKF2:{service}:{account}".encode("utf-8")
    ct = AESGCM(subkey).encrypt(nonce, plaintext, aad)
    return _SEALED_FALLBACK_MAGIC + nonce + ct


def _unseal_fallback_blob(kdir: Path, service: str, account: str, blob: bytes) -> bytes:
    if blob.startswith(_SEALED_FALLBACK_MAGIC):
        payload = blob[len(_SEALED_FALLBACK_MAGIC) :]
        if len(payload) < 12 + 16 + 1:
            raise KeychainError("Truncated SBKF2 keychain file.")
        subkey = _derive_local_wrap_subkey(kdir, service, account, create=False)
        if subkey is None:
            raise KeychainError("Missing local wrap key (.wrap.key) required to unseal SBKF2 secret.")
        nonce = payload[:12]
        ct = payload[12:]
        aad = f"SBKF2:{service}:{account}".encode("utf-8")
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        try:
            return AESGCM(subkey).decrypt(nonce, ct, aad)
        except InvalidTag as exc:
            raise KeychainError("Local SBKF2 keychain file authentication failed (tampered or wrong wrap key).") from exc
    if blob.startswith(_FALLBACK_FILE_MAGIC):
        return blob[len(_FALLBACK_FILE_MAGIC) :]
    raise KeychainError("Malformed restricted keychain file header.")


def is_native_keychain_backend(backend: str | None) -> bool:
    """Return True only when sealed by an OS-managed keychain/DPAPI provider (never file fallback)."""
    return (backend or "").strip().lower() in NATIVE_KEYCHAIN_BACKENDS


def keychain_backend_name(*, state_dir: Path | None = None) -> str:
    """Return the active secret-sealing backend identifier for diagnostics and status reporting."""
    if sys.platform == "win32":
        return "windows_dpapi"
    if _macos_security_available(state_dir):
        return "macos_keychain"
    if _linux_secret_tool_available(state_dir):
        return "linux_secret_service"
    return "posix_restricted_keyfile"


def store_secret(
    service: str,
    account: str,
    secret_bytes: bytes,
    *,
    state_dir: Path | None = None,
    allow_file_fallback: bool | None = None,
) -> dict[str, str]:
    """Seal `secret_bytes` into the platform keychain / DPAPI store."""
    svc = _validate_identifier(service, "service")
    acc = _validate_identifier(account, "account")
    if not isinstance(secret_bytes, (bytes, bytearray)) or not (1 <= len(secret_bytes) <= MAX_SECRET_BYTES):
        raise KeychainError(f"Secret length must be between 1 and {MAX_SECRET_BYTES} bytes.")
    raw = bytes(secret_bytes)
    kdir = _keychain_dir(state_dir)
    slug = _entry_slug(svc, acc)

    if sys.platform == "win32":
        sealed = dpapi_protect_bytes(raw)
        dest = kdir / f"{slug}.dpapi"
        _atomic_write_secret_file(dest, _DPAPI_FILE_MAGIC + sealed)
        return {"backend": "windows_dpapi", "service": svc, "account": acc}

    os_svc = _scoped_os_service(svc, state_dir)
    native_attempted_and_failed = False

    if _macos_security_available(state_dir):
        b64_val = base64.b64encode(raw).decode("ascii")
        cmd_line = f'add-generic-password -U -s "{os_svc}" -a "{acc}" -w "{b64_val}"\n'
        proc = subprocess.run(
            ["/usr/bin/security", "-i"],
            input=cmd_line,
            text=True,
            capture_output=True,
            timeout=10.0,
            check=False,
        )
        if proc.returncode == 0:
            return {"backend": "macos_keychain", "service": svc, "account": acc}
        native_attempted_and_failed = True

    if _linux_secret_tool_available(state_dir):
        st_bin = shutil.which("secret-tool")
        if st_bin:
            b64_val = base64.b64encode(raw).decode("ascii")
            proc = subprocess.run(
                [st_bin, "store", "--label=Second Brain Vault Key", "service", os_svc, "account", acc],
                input=b64_val,
                text=True,
                capture_output=True,
                timeout=10.0,
                check=False,
            )
            if proc.returncode == 0:
                return {"backend": "linux_secret_service", "service": svc, "account": acc}
            native_attempted_and_failed = True

    env_fallback = (os.environ.get("BRAIN_ALLOW_KEYFILE_FALLBACK") or "").strip() == "1"
    if allow_file_fallback is not None:
        fallback_permitted = bool(allow_file_fallback)
    elif native_attempted_and_failed:
        # Native OS keychain exists on this machine but refused/failed (e.g. locked keychain): fail closed unless explicitly overridden!
        fallback_permitted = env_fallback
    else:
        # Headless POSIX container without Secret Service / security binary: allow AEAD-wrapped local fallback when isolated state_dir or env opt-in is used
        fallback_permitted = state_dir is not None or env_fallback

    if not fallback_permitted:
        raise KeychainUnavailableError(
            "Native OS Keychain / Secret Service is unavailable or locked; refusing fallback keyfile."
        )
    dest = kdir / f"{slug}.key"
    _atomic_write_secret_file(dest, _seal_fallback_blob(kdir, svc, acc, raw))
    return {"backend": "posix_restricted_keyfile", "service": svc, "account": acc}


def load_secret(
    service: str,
    account: str,
    *,
    state_dir: Path | None = None,
    backend: str | None = None,
) -> bytes | None:
    """Unseal and return `secret_bytes` from the platform keychain / DPAPI store, or `None` if absent."""
    svc = _validate_identifier(service, "service")
    acc = _validate_identifier(account, "account")
    kdir = _keychain_dir(state_dir)
    slug = _entry_slug(svc, acc)
    preferred = (backend or "").strip().lower()
    if preferred not in {"", "windows_dpapi", "macos_keychain", "linux_secret_service", "posix_restricted_keyfile"}:
        raise KeychainError(f"Unsupported keychain backend: {backend!r}")

    if preferred == "windows_dpapi" and sys.platform != "win32":
        return None

    if sys.platform == "win32":
        if preferred in {"macos_keychain", "linux_secret_service"}:
            return None
        if preferred != "posix_restricted_keyfile":
            dest = kdir / f"{slug}.dpapi"
            if not os.path.lexists(str(dest)):
                return None
            st = _assert_regular_secret_file(dest)
            if st.st_size > MAX_SECRET_BYTES + 4096:
                raise KeychainError(f"Oversized DPAPI keychain blob rejected: {dest}")
            blob = dest.read_bytes()
            if not blob.startswith(_DPAPI_FILE_MAGIC):
                raise KeychainError("Malformed DPAPI keychain file header.")
            return dpapi_unprotect_bytes(blob[len(_DPAPI_FILE_MAGIC) :])

    os_svc = _scoped_os_service(svc, state_dir)

    if preferred == "macos_keychain":
        if not _macos_security_available(state_dir):
            return None
        for candidate_svc in ([os_svc, svc] if os_svc != svc else [svc]):
            proc = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", candidate_svc, "-a", acc, "-w"],
                text=True,
                capture_output=True,
                timeout=10.0,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return base64.b64decode(proc.stdout.strip())
        return None

    if preferred == "linux_secret_service":
        if not _linux_secret_tool_available(state_dir):
            return None
        st_bin = shutil.which("secret-tool")
        if not st_bin:
            return None
        for candidate_svc in ([os_svc, svc] if os_svc != svc else [svc]):
            proc = subprocess.run(
                [st_bin, "lookup", "service", candidate_svc, "account", acc],
                text=True,
                capture_output=True,
                timeout=10.0,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return base64.b64decode(proc.stdout.strip())
        return None

    if preferred == "":
        if _macos_security_available(state_dir):
            for candidate_svc in ([os_svc, svc] if os_svc != svc else [svc]):
                proc = subprocess.run(
                    ["/usr/bin/security", "find-generic-password", "-s", candidate_svc, "-a", acc, "-w"],
                    text=True,
                    capture_output=True,
                    timeout=10.0,
                    check=False,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    return base64.b64decode(proc.stdout.strip())
        if _linux_secret_tool_available(state_dir):
            st_bin = shutil.which("secret-tool")
            if st_bin:
                for candidate_svc in ([os_svc, svc] if os_svc != svc else [svc]):
                    proc = subprocess.run(
                        [st_bin, "lookup", "service", candidate_svc, "account", acc],
                        text=True,
                        capture_output=True,
                        timeout=10.0,
                        check=False,
                    )
                    if proc.returncode == 0 and proc.stdout.strip():
                        return base64.b64decode(proc.stdout.strip())

    fallback = kdir / f"{slug}.key"
    if not os.path.lexists(str(fallback)):
        return None
    st = _assert_regular_secret_file(fallback)
    if st.st_size > MAX_SECRET_BYTES + 128:
        raise KeychainError(f"Oversized keychain file rejected: {fallback}")
    blob = fallback.read_bytes()
    return _unseal_fallback_blob(kdir, svc, acc, blob)


def delete_secret(
    service: str,
    account: str,
    *,
    state_dir: Path | None = None,
) -> bool:
    """Remove a sealed secret from the platform keychain / DPAPI store."""
    svc = _validate_identifier(service, "service")
    acc = _validate_identifier(account, "account")
    kdir = _keychain_dir(state_dir)
    slug = _entry_slug(svc, acc)
    removed = False

    if sys.platform == "win32":
        dest = kdir / f"{slug}.dpapi"
        if os.path.lexists(str(dest)):
            _assert_safe_keychain_file(dest)
            dest.unlink()
            removed = True
        return removed

    os_svc = _scoped_os_service(svc, state_dir)

    if _macos_security_available(state_dir):
        for candidate_svc in ([os_svc, svc] if os_svc != svc else [svc]):
            proc = subprocess.run(
                ["/usr/bin/security", "delete-generic-password", "-s", candidate_svc, "-a", acc],
                text=True,
                capture_output=True,
                timeout=10.0,
                check=False,
            )
            if proc.returncode == 0:
                removed = True

    if _linux_secret_tool_available(state_dir):
        st_bin = shutil.which("secret-tool")
        if st_bin:
            for candidate_svc in ([os_svc, svc] if os_svc != svc else [svc]):
                proc = subprocess.run(
                    [st_bin, "clear", "service", candidate_svc, "account", acc],
                    text=True,
                    capture_output=True,
                    timeout=10.0,
                    check=False,
                )
                if proc.returncode == 0:
                    removed = True

    fallback = kdir / f"{slug}.key"
    if os.path.lexists(str(fallback)):
        _assert_safe_keychain_file(fallback)
        fallback.unlink()
        removed = True
    return removed
