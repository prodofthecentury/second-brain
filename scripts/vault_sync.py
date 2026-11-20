# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#     "cryptography>=42,<51",
#     "httpx>=0.28,<1",
# ]
# ///
"""Zero-Knowledge End-to-End Encrypted (E2EE) Multi-Device Vault Sync.

Synchronizes ONLY durable canonical vault state across devices while preserving all
5 Second Brain System Invariants and the Three-Store Boundary:

  * Authenticated E2EE: AES-256-GCM with per-object HKDF-SHA256 subkeys, random
    96-bit nonces, strict AAD binding (vault_id + object kind + object ID), and
    HMAC-SHA256 keyed blind blob IDs (preventing known-plaintext existence probing).
  * Zero plaintext metadata on sync targets: relative paths live strictly inside
    the encrypted manifest; sync targets store opaque ciphertext blobs only.
  * Ed25519 device signatures & peer key pinning: every sync head is signed by a
    device-local Ed25519 private key stored in the OS security state directory
    (never inside brain/), with anti-rollback monotonic epoch enforcement.
  * Three-Store isolation: strictly excludes .sidecar/, .chroma/, locks, temp files,
    symlinks/reparse points, and security secrets. Rebuilds local disposable indexes
    (SQLite FTS5 lexical + notes) automatically after pull.
  * Conflict-safe 3-way merge: deduplicated set-union merge for append-only JSONL
    logs (decision-record.jsonl, manifest.jsonl, atom-corrections.jsonl) and
    quarantined 0-raw/sync-conflicts/ copies on divergent regular-file edits
    (never silently overwriting or auto-approving canonical atoms).
  * Pluggable transports: LocalFolderSyncBackend (OneDrive/iCloud/Syncthing/NAS/USB)
    and HttpObjectSyncBackend (DNS-pinned HTTPS object store / WebDAV / S3-compatible).
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import tempfile
from typing import Any
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import (  # noqa: E402
    atomic_write_bytes,
    content_sha256,
    invalidate_decision_log_cache,
    review_lock,
)
from config import (  # noqa: E402
    BRAIN_DIR,
    _WINDOWS_RESERVED_STEMS,
    _is_reparse_or_link,
    resolve_active_brain_dir,
    safe_file_in_dir,
    security_state_dir,
)
from host_adapters import (  # noqa: E402
    HostSharingRule,
    PinnedHTTPTransport,
    read_bounded_security_file,
    restrict_security_file_permissions,
    validate_endpoint_network_boundary,
)
from lexical_store import rebuild_lexical_index  # noqa: E402
from notes_store import NotePaths, ensure_notes_index  # noqa: E402


SYNC_SCHEMA_VERSION = 1
ENVELOPE_MAGIC = b"SBSY1\x00"
NONCE_BYTES = 12
KEY_BYTES = 32
MAX_SYNC_FILE_BYTES = 50_000_000
MAX_SYNC_MANIFEST_BYTES = 16_000_000
MAX_SYNC_HEADER_BYTES = 65_536
MAX_TRUSTED_PEERS = 32
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024

_HEX_ID_RE = re.compile(r"^(?:[0-9a-f]{2}){8,32}$")
_DEVICE_ID_RE = re.compile(r"^[a-zA-Z0-9._-]{4,64}$")
_ENV_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

_ALLOWED_TOP_FILES = frozenset({"house-rules.md"})
_ALLOWED_ROOT_DIRS = frozenset(
    {
        "0-raw",
        "2-atoms",
        "3-threads",
        "4-experiences",
        "5-playbooks",
        "notes",
        "sources",
        "briefings",
    }
)
_EXCLUDED_DIR_NAMES = frozenset(
    {
        ".sidecar",
        ".chroma",
        ".git",
        "__pycache__",
        "sync-conflicts",
    }
)
_EXCLUDED_SUFFIXES = (".tmp", ".lock", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".db-wal", ".db-shm")
_APPEND_ONLY_JSONL_PATHS = frozenset(
    {
        "0-raw/archive/decision-record.jsonl",
        "0-raw/archive/manifest.jsonl",
        "0-raw/archive/atom-corrections.jsonl",
        "0-raw/archive/mcp-audit.jsonl",
    }
)
_HISTORY_PROTECTED_PREFIXES = ("2-atoms/", "5-playbooks/")


class SyncError(RuntimeError):
    """Base error for vault sync operations."""


class SyncSecurityError(PermissionError):
    """Raised fail-closed on cryptographic, signature, AAD, rollback, or path violations."""


class SyncConflictError(SyncError):
    """Raised when a push is blocked because remote head is ahead of local cursor."""


# ─── Path & Security Helpers ─────────────────────────────────────────────────

def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _vault_key_slug(brain_dir: str | Path) -> str:
    norm = os.path.normcase(os.path.abspath(str(brain_dir)))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def sync_key_dir(state_dir: Path | None = None) -> Path:
    base = Path(state_dir) if state_dir is not None else security_state_dir()
    return base / "sync"


def sync_config_path(brain_dir: str | Path, *, state_dir: Path | None = None) -> Path:
    return sync_key_dir(state_dir) / f"vault-{_vault_key_slug(brain_dir)}.json"


def sync_state_path(brain_dir: str | Path) -> Path:
    return Path(brain_dir).resolve() / ".sidecar" / "sync-state.json"


def _assert_no_symlink_chain(path: Path, *, stop_at: Path | None = None) -> None:
    stop_norm = os.path.normcase(os.path.abspath(str(stop_at))) if stop_at is not None else None
    cur = Path(os.path.abspath(str(path)))
    while True:
        if os.path.lexists(str(cur)) and _is_reparse_or_link(str(cur)):
            raise SyncSecurityError(f"Refusing to traverse symlink or reparse point: {cur}")
        if stop_norm is not None and os.path.normcase(str(cur)) == stop_norm:
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent


def _atomic_write_restricted_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _is_reparse_or_link(str(path.parent)) or (os.path.lexists(str(path)) and _is_reparse_or_link(str(path))):
        raise SyncSecurityError(f"Refusing to write sync secret/state through symlink: {path}")
    raw = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    fd_closed = False
    try:
        if os.name != "nt":
            os.chmod(tmp_name, 0o600)
        if os.write(fd, raw) != len(raw):
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


def validate_sync_rel_path(rel_path: str) -> str:
    """Validate that rel_path is a safe, normalized POSIX durable vault path."""
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise SyncSecurityError("Empty sync path rejected.")
    if "\x00" in rel_path or "\\" in rel_path or rel_path.startswith("/") or ":" in rel_path:
        raise SyncSecurityError(f"Unsafe sync relative path rejected: {rel_path!r}")
    parts = rel_path.split("/")
    if any(seg in {"", ".", ".."} for seg in parts):
        raise SyncSecurityError(f"Path traversal segment rejected: {rel_path!r}")
    for seg in parts:
        stem = os.path.splitext(seg)[0].rstrip(" .").lower()
        if stem in _WINDOWS_RESERVED_STEMS:
            raise SyncSecurityError(f"Windows reserved filename rejected in sync path: {rel_path!r}")
    if len(parts) == 1:
        if parts[0] not in _ALLOWED_TOP_FILES:
            raise SyncSecurityError(f"Top-level file not in durable sync allowlist: {rel_path!r}")
        return rel_path
    root_dir = parts[0]
    if root_dir not in _ALLOWED_ROOT_DIRS:
        raise SyncSecurityError(f"Directory '{root_dir}' is excluded by Three-Store sync boundary: {rel_path!r}")
    if any(seg in _EXCLUDED_DIR_NAMES for seg in parts[:-1]):
        raise SyncSecurityError(f"Excluded internal directory in sync path: {rel_path!r}")
    leaf_low = parts[-1].lower()
    if leaf_low.endswith(_EXCLUDED_SUFFIXES) or leaf_low.startswith("~"):
        raise SyncSecurityError(f"Transient/index file excluded from sync: {rel_path!r}")
    return rel_path


def _is_syncable_file(rel_path: str) -> bool:
    try:
        validate_sync_rel_path(rel_path)
        return True
    except SyncSecurityError:
        return False


# ─── Cryptographic Primitives ────────────────────────────────────────────────

def derive_master_key_from_passphrase(passphrase: str, salt: bytes) -> bytes:
    if not isinstance(passphrase, str) or len(passphrase) < 8:
        raise ValueError("Sync passphrase must be at least 8 characters.")
    if len(salt) < 16:
        raise ValueError("KDF salt must be at least 16 bytes.")
    return hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=KEY_BYTES,
    )


def _derive_subkey(master_key: bytes, vault_id: str, purpose: str, object_id: str = "") -> bytes:
    info = f"second-brain-sync-v1:{purpose}:{object_id}".encode("utf-8")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=bytes.fromhex(vault_id),
        info=info,
    ).derive(master_key)


def compute_blind_blob_id(master_key: bytes, vault_id: str, plaintext_sha256: str) -> str:
    """Compute keyed HMAC-SHA256 blob ID (deduplicates content without exposing plaintext hashes)."""
    blind_key = _derive_subkey(master_key, vault_id, "blind-blob-id")
    return hmac.new(blind_key, bytes.fromhex(plaintext_sha256), hashlib.sha256).hexdigest()


def encrypt_object(
    master_key: bytes,
    *,
    vault_id: str,
    kind: str,
    object_id: str,
    plaintext: bytes,
) -> bytes:
    subkey = _derive_subkey(master_key, vault_id, f"aead-{kind}", object_id)
    nonce = os.urandom(NONCE_BYTES)
    aad = f"sb-sync-v1|{vault_id}|{kind}|{object_id}".encode("utf-8")
    ct = AESGCM(subkey).encrypt(nonce, plaintext, aad)
    return ENVELOPE_MAGIC + nonce + ct


def decrypt_object(
    master_key: bytes,
    *,
    vault_id: str,
    kind: str,
    object_id: str,
    envelope: bytes,
) -> bytes:
    min_len = len(ENVELOPE_MAGIC) + NONCE_BYTES + 16
    if not isinstance(envelope, (bytes, bytearray)) or len(envelope) < min_len:
        raise SyncSecurityError(f"Truncated encrypted {kind} envelope for {object_id}.")
    if bytes(envelope[: len(ENVELOPE_MAGIC)]) != ENVELOPE_MAGIC:
        raise SyncSecurityError(f"Invalid envelope magic header for {kind} {object_id}.")
    nonce = bytes(envelope[len(ENVELOPE_MAGIC) : len(ENVELOPE_MAGIC) + NONCE_BYTES])
    ct = bytes(envelope[len(ENVELOPE_MAGIC) + NONCE_BYTES :])
    subkey = _derive_subkey(master_key, vault_id, f"aead-{kind}", object_id)
    aad = f"sb-sync-v1|{vault_id}|{kind}|{object_id}".encode("utf-8")
    try:
        return AESGCM(subkey).decrypt(nonce, ct, aad)
    except InvalidTag as exc:
        raise SyncSecurityError(
            f"AES-256-GCM authentication failed for {kind} '{object_id}' (tampered ciphertext, wrong key, or AAD mismatch)."
        ) from exc


def _generate_device_keypair() -> tuple[str, str]:
    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(priv_raw).decode("ascii"), base64.b64encode(pub_raw).decode("ascii")


def _head_signing_bytes(head: dict[str, Any]) -> bytes:
    payload = {
        "schema_version": int(head["schema_version"]),
        "vault_id": str(head["vault_id"]),
        "epoch": int(head["epoch"]),
        "parent_manifest_id": head.get("parent_manifest_id"),
        "manifest_id": str(head["manifest_id"]),
        "manifest_ciphertext_sha256": str(head["manifest_ciphertext_sha256"]),
        "device_id": str(head["device_id"]),
        "device_pubkey_b64": str(head["device_pubkey_b64"]),
        "created_at": str(head["created_at"]),
    }
    return b"second-brain-sync-head-v1\n" + json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign_head(head: dict[str, Any], privkey_b64: str) -> str:
    priv = Ed25519PrivateKey.from_private_bytes(base64.b64decode(privkey_b64))
    sig = priv.sign(_head_signing_bytes(head))
    return base64.b64encode(sig).decode("ascii")


def _verify_head_signature(head: dict[str, Any]) -> None:
    try:
        dev_id = str(head["device_id"])
        if not _DEVICE_ID_RE.fullmatch(dev_id):
            raise SyncSecurityError(f"Invalid device_id format in remote sync head: {dev_id!r}")
        pub_raw = base64.b64decode(str(head["device_pubkey_b64"]))
        sig_raw = base64.b64decode(str(head["signature_b64"]))
        pub = Ed25519PublicKey.from_public_bytes(pub_raw)
        pub.verify(sig_raw, _head_signing_bytes(head))
    except SyncSecurityError:
        raise
    except (KeyError, ValueError, TypeError, InvalidSignature) as exc:
        raise SyncSecurityError(f"Invalid Ed25519 signature on remote sync head: {exc}") from exc


def _compute_head_mac(master_key: bytes, vault_id: str, head: dict[str, Any]) -> str:
    mac_key = _derive_subkey(master_key, vault_id, "head-auth")
    return hmac.new(mac_key, _head_signing_bytes(head), hashlib.sha256).hexdigest()


def _verify_head_mac(master_key: bytes, vault_id: str, head: dict[str, Any]) -> None:
    supplied = str(head.get("head_mac") or "").strip().lower()
    if not supplied:
        raise SyncSecurityError("Missing master-key HMAC ('head_mac') on remote sync head.")
    try:
        expected = _compute_head_mac(master_key, vault_id, head)
    except (KeyError, ValueError, TypeError) as exc:
        raise SyncSecurityError(f"Malformed remote sync head: {exc}") from exc
    if not hmac.compare_digest(supplied, expected):
        raise SyncSecurityError("Master-key HMAC verification failed on remote sync head (unauthenticated head.json).")


# ─── Pluggable Transport Backends ────────────────────────────────────────────

class SyncBackend:
    """Abstract zero-knowledge object transport for encrypted vault sync."""

    backend_type: str = "abstract"

    def get_object(self, key: str, *, max_bytes: int = MAX_SYNC_FILE_BYTES + 4096) -> bytes | None:
        raise NotImplementedError

    def put_object(self, key: str, data: bytes) -> None:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        return self.get_object(key, max_bytes=16) is not None


def _validate_backend_key(key: str) -> str:
    if not isinstance(key, str) or not key or "\x00" in key or "\\" in key or key.startswith("/"):
        raise SyncSecurityError(f"Invalid sync object key: {key!r}")
    parts = key.split("/")
    if any(seg in {"", ".", ".."} for seg in parts):
        raise SyncSecurityError(f"Unsafe sync object key traversal: {key!r}")
    if parts[0] not in {"vault-header.json", "head.json", "manifests", "objects"}:
        raise SyncSecurityError(f"Disallowed sync object namespace: {key!r}")
    return key


class LocalFolderSyncBackend(SyncBackend):
    """Zero-server folder backend compatible with OneDrive, iCloud, Dropbox, Syncthing, NAS, and USB."""

    backend_type = "local_folder"

    def __init__(self, target_dir: str | Path) -> None:
        self.root = Path(os.path.abspath(os.path.expanduser(str(target_dir))))
        if os.path.lexists(str(self.root)) and _is_reparse_or_link(str(self.root)):
            raise SyncSecurityError(f"Refusing to use symlinked sync folder: {self.root}")

    def _resolve(self, key: str) -> Path:
        clean = _validate_backend_key(key)
        dest = self.root.joinpath(*clean.split("/"))
        _assert_no_symlink_chain(dest, stop_at=self.root)
        return dest

    def exists(self, key: str) -> bool:
        dest = self._resolve(key)
        return dest.is_file() and not _is_reparse_or_link(str(dest))

    def get_object(self, key: str, *, max_bytes: int = MAX_SYNC_FILE_BYTES + 4096) -> bytes | None:
        dest = self._resolve(key)
        if not dest.exists():
            return None
        if not dest.is_file():
            raise SyncSecurityError(f"Sync object is not a regular file: {key}")
        st = dest.stat()
        if st.st_size > max_bytes:
            raise SyncSecurityError(f"Sync object '{key}' exceeds max allowed size ({st.st_size} > {max_bytes}).")
        return dest.read_bytes()

    def put_object(self, key: str, data: bytes) -> None:
        dest = self._resolve(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _assert_no_symlink_chain(dest, stop_at=self.root)
        atomic_write_bytes(str(dest), data)


class HttpObjectSyncBackend(SyncBackend):
    """DNS-pinned HTTPS object-store backend (S3 presigned/gateway, R2, WebDAV, or custom HTTPS endpoint)."""

    backend_type = "http_object"

    def __init__(
        self,
        endpoint_url: str,
        *,
        bearer_token_env: str | None = None,
        http_transport: httpx.BaseTransport | None = None,
        skip_dns_check_for_mock_transport: bool = False,
        timeout_s: float = 30.0,
    ) -> None:
        raw_ep = (endpoint_url or "").strip().rstrip("/")
        parsed = urlparse(raw_ep)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise SyncSecurityError(f"Invalid HTTP sync endpoint URL: {endpoint_url!r}")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise SyncSecurityError("Sync endpoint URL must not embed credentials, query strings, or fragments.")
        if bearer_token_env is not None and bearer_token_env.strip():
            if not _ENV_VAR_RE.fullmatch(bearer_token_env.strip()):
                raise SyncSecurityError("bearer_token_env must be an uppercase environment variable name.")
            self.bearer_token_env: str | None = bearer_token_env.strip()
        else:
            self.bearer_token_env = None

        transport_kind = "local" if (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}) else "online"
        if parsed.scheme == "http" and transport_kind != "local":
            raise SyncSecurityError("Remote HTTP sync endpoints MUST use https://.")

        rule = HostSharingRule(
            host_id="vault_sync_http",
            provider="openai_compatible",
            transport=transport_kind,
            endpoint_url=raw_ep,
            default_model="sync-object-store",
            api_key_env=self.bearer_token_env,
            enabled=True,
            owner_consent_recorded=True,
        )
        self.pinned_target = validate_endpoint_network_boundary(
            rule,
            skip_dns_resolution=bool(http_transport is not None and skip_dns_check_for_mock_transport),
        )
        self.endpoint_url = raw_ep
        self.http_transport = http_transport
        self.timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        hdrs: dict[str, str] = {"User-Agent": "SecondBrain-Sync/1.0"}
        if self.bearer_token_env:
            tok = (os.environ.get(self.bearer_token_env) or "").strip()
            if not tok:
                raise SyncSecurityError(
                    f"Required sync auth environment variable '{self.bearer_token_env}' is not set."
                )
            hdrs["Authorization"] = f"Bearer {tok}"
        return hdrs

    def _open_client(self) -> tuple[httpx.Client, PinnedHTTPTransport | None]:
        if self.http_transport is not None:
            return (
                httpx.Client(
                    transport=self.http_transport,
                    timeout=self.timeout_s,
                    follow_redirects=False,
                    trust_env=False,
                ),
                None,
            )
        owned_pinned: PinnedHTTPTransport | None = None
        if self.pinned_target is not None and self.pinned_target.addresses and urlparse(self.endpoint_url).scheme == "https":
            owned_pinned = PinnedHTTPTransport(self.pinned_target)
            return (
                httpx.Client(
                    transport=owned_pinned,
                    timeout=self.timeout_s,
                    follow_redirects=False,
                    trust_env=False,
                ),
                owned_pinned,
            )
        return (
            httpx.Client(timeout=self.timeout_s, follow_redirects=False, trust_env=False),
            None,
        )

    def _url_for(self, key: str) -> str:
        clean = _validate_backend_key(key)
        return f"{self.endpoint_url}/{clean}"

    def exists(self, key: str) -> bool:
        url = self._url_for(key)
        client, pinned = self._open_client()
        try:
            with client:
                hdrs = self._headers()
                head_resp = client.request("HEAD", url, headers=hdrs)
                if 300 <= head_resp.status_code < 400:
                    raise SyncSecurityError("HTTP redirects are forbidden on sync object endpoints.")
                if head_resp.status_code == 200:
                    return True
                if head_resp.status_code == 404:
                    return False
                if head_resp.status_code not in {405, 501}:
                    head_resp.raise_for_status()
                with client.stream("GET", url, headers=hdrs) as get_resp:
                    if 300 <= get_resp.status_code < 400:
                        raise SyncSecurityError("HTTP redirects are forbidden on sync object endpoints.")
                    if get_resp.status_code == 404:
                        return False
                    get_resp.raise_for_status()
                    return True
        finally:
            if pinned is not None:
                pinned.close()

    def get_object(self, key: str, *, max_bytes: int = MAX_SYNC_FILE_BYTES + 4096) -> bytes | None:
        url = self._url_for(key)
        client, pinned = self._open_client()
        try:
            with client:
                with client.stream("GET", url, headers=self._headers()) as resp:
                    if 300 <= resp.status_code < 400:
                        raise SyncSecurityError("HTTP redirects are forbidden on sync object endpoints.")
                    if resp.status_code == 404:
                        return None
                    resp.raise_for_status()
                    clen = resp.headers.get("Content-Length")
                    if clen is not None:
                        try:
                            if int(clen) > max_bytes:
                                raise SyncSecurityError(f"Remote sync object '{key}' exceeds max size limit.")
                        except ValueError:
                            pass
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise SyncSecurityError(f"Remote sync object '{key}' exceeds max size limit.")
                        chunks.append(chunk)
                    return b"".join(chunks)
        finally:
            if pinned is not None:
                pinned.close()

    def put_object(self, key: str, data: bytes) -> None:
        url = self._url_for(key)
        client, pinned = self._open_client()
        try:
            with client:
                hdrs = {**self._headers(), "Content-Type": "application/octet-stream"}
                resp = client.put(url, headers=hdrs, content=data)
                if 300 <= resp.status_code < 400:
                    raise SyncSecurityError("HTTP redirects are forbidden on sync object endpoints.")
                resp.raise_for_status()
        finally:
            if pinned is not None:
                pinned.close()


def _backend_from_config(
    cfg: dict[str, Any],
    *,
    http_transport: httpx.BaseTransport | None = None,
    skip_dns_check_for_mock_transport: bool = False,
) -> SyncBackend:
    btype = str(cfg.get("backend_type") or "")
    if btype == "local_folder":
        return LocalFolderSyncBackend(str(cfg["target"]))
    if btype == "http_object":
        return HttpObjectSyncBackend(
            str(cfg["target"]),
            bearer_token_env=cfg.get("bearer_token_env"),
            http_transport=http_transport,
            skip_dns_check_for_mock_transport=skip_dns_check_for_mock_transport,
        )
    raise SyncError(f"Unsupported sync backend type: {btype!r}")


# ─── Vault Scanning & Merge Helpers ──────────────────────────────────────────

def scan_durable_vault_files(brain_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Scan durable Three-Store vault files, returning {rel_posix: {sha256, size}}."""
    root = Path(brain_dir).resolve()
    if os.path.lexists(str(root)) and _is_reparse_or_link(str(root)):
        raise SyncSecurityError(f"Refusing to scan symlinked vault directory: {root}")
    inventory: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return inventory

    # Top-level allowlisted files
    for fname in sorted(_ALLOWED_TOP_FILES):
        fp = root / fname
        if fp.is_file() and not _is_reparse_or_link(str(fp)):
            st = fp.stat()
            if st.st_size <= MAX_SYNC_FILE_BYTES:
                data = fp.read_bytes()
                inventory[fname] = {"sha256": content_sha256(data), "size": len(data)}

    # Allowlisted durable subtrees
    for root_name in sorted(_ALLOWED_ROOT_DIRS):
        sub_root = root / root_name
        if not sub_root.exists():
            continue
        if _is_reparse_or_link(str(sub_root)) or not sub_root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(str(sub_root), followlinks=False):
            # Prune excluded and symlinked directories in-place
            kept_dirs: list[str] = []
            for d in sorted(dirnames):
                if d in _EXCLUDED_DIR_NAMES:
                    continue
                full_d = os.path.join(dirpath, d)
                if _is_reparse_or_link(full_d):
                    continue
                kept_dirs.append(d)
            dirnames[:] = kept_dirs

            for fname in sorted(filenames):
                full_f = os.path.join(dirpath, fname)
                if _is_reparse_or_link(full_f) or not os.path.isfile(full_f):
                    continue
                rel = Path(full_f).relative_to(root).as_posix()
                if not _is_syncable_file(rel):
                    continue
                st = os.stat(full_f)
                if st.st_size > MAX_SYNC_FILE_BYTES:
                    continue
                data = Path(full_f).read_bytes()
                inventory[rel] = {"sha256": content_sha256(data), "size": len(data)}
    return inventory


def _ts_sort_key(raw_ts: str) -> tuple[int, float | str]:
    if not raw_ts:
        return (1, "")
    norm = raw_ts.replace("Z", "+00:00") if raw_ts.endswith("Z") else raw_ts
    try:
        dt = datetime.datetime.fromisoformat(norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return (0, dt.timestamp())
    except ValueError:
        return (0, raw_ts)


def _merge_append_only_jsonl(local_bytes: bytes, remote_bytes: bytes) -> bytes:
    """Deterministic deduplicated set-union merge for append-only JSONL audit/lineage logs."""
    by_hash: dict[str, tuple[tuple[int, float | str], int, str]] = {}
    seq_idx = 0
    for blob in (local_bytes, remote_bytes):
        for raw_line in blob.decode("utf-8", errors="replace").splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            digest = hashlib.sha256(stripped.encode("utf-8")).hexdigest()
            if digest in by_hash:
                continue
            sort_ts = ""
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    for key in ("timestamp", "recorded_at", "created_at", "archived_at", "ts"):
                        val = parsed.get(key)
                        if isinstance(val, str) and val.strip():
                            sort_ts = val.strip()
                            break
            except json.JSONDecodeError:
                pass
            by_hash[digest] = (_ts_sort_key(sort_ts), seq_idx, stripped)
            seq_idx += 1

    ordered = sorted(by_hash.items(), key=lambda kv: (kv[1][0], kv[1][1], kv[0]))
    if not ordered:
        return b""
    return ("\n".join(item[1][2] for item in ordered) + "\n").encode("utf-8")


def _backup_before_sync_overwrite(brain_root: Path, rel_path: str, existing_bytes: bytes) -> None:
    """Preserve prior canonical atom/playbook revision in .history/ before remote fast-forward."""
    for prefix in _HISTORY_PROTECTED_PREFIXES:
        if rel_path.startswith(prefix) and "/.history/" not in rel_path:
            base_dir = brain_root / prefix.rstrip("/")
            hist_dir = base_dir / ".history"
            hist_dir.mkdir(parents=True, exist_ok=True)
            _assert_no_symlink_chain(hist_dir, stop_at=brain_root)
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
            stem = Path(rel_path).stem
            sha_short = content_sha256(existing_bytes)[:12]
            hist_file = hist_dir / f"{stem}.sync-{stamp}-{sha_short}.md"
            if not hist_file.exists():
                atomic_write_bytes(str(hist_file), existing_bytes)
            return


def _quarantine_conflict_copy(
    brain_root: Path,
    rel_path: str,
    remote_bytes: bytes,
    *,
    remote_device_id: str,
    remote_epoch: int,
) -> str:
    """Store conflicting remote copy under 0-raw/sync-conflicts/ (never mutating canonical atoms)."""
    conflicts_dir = brain_root / "0-raw" / "sync-conflicts"
    conflicts_dir.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_chain(conflicts_dir, stop_at=brain_root)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_rel = re.sub(r"[^A-Za-z0-9._-]+", "__", rel_path.replace("/", "__"))[:120]
    safe_dev = re.sub(r"[^A-Za-z0-9._-]+", "", remote_device_id)[:24] or "peer"
    cname = f"{stamp}-e{remote_epoch}-{safe_dev}-{safe_rel}"
    dest = conflicts_dir / cname
    suffix = 2
    while dest.exists():
        dest = conflicts_dir / f"{stamp}-e{remote_epoch}-{safe_dev}-{suffix}-{safe_rel}"
        suffix += 1
    atomic_write_bytes(str(dest), remote_bytes)
    return dest.relative_to(brain_root).as_posix()


def _load_sync_config(brain_dir: str | Path, *, state_dir: Path | None = None) -> dict[str, Any]:
    cfg_path = sync_config_path(brain_dir, state_dir=state_dir)
    if not cfg_path.exists():
        raise SyncError("Vault sync is not configured for this vault. Run `second-brain sync init` or `join` first.")
    if _is_reparse_or_link(str(cfg_path)) or _is_reparse_or_link(str(cfg_path.parent)):
        raise SyncSecurityError(f"Refusing to read symlinked sync config: {cfg_path}")
    raw = read_bounded_security_file(cfg_path, max_bytes=65_536)
    data = json.loads(raw)
    if not isinstance(data, dict) or int(data.get("schema_version", 0)) != SYNC_SCHEMA_VERSION:
        raise SyncSecurityError("Corrupted or unsupported sync configuration schema.")
    return data


def _load_local_sync_state(brain_dir: str | Path) -> dict[str, Any]:
    sp = sync_state_path(brain_dir)
    if not sp.exists():
        return {"schema_version": SYNC_SCHEMA_VERSION, "last_seen_epoch": 0, "last_manifest_id": None, "files": {}, "conflicts": []}
    if _is_reparse_or_link(str(sp)) or _is_reparse_or_link(str(sp.parent)):
        raise SyncSecurityError(f"Refusing to read symlinked sync state: {sp}")
    data = json.loads(sp.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {"schema_version": SYNC_SCHEMA_VERSION, "last_seen_epoch": 0, "last_manifest_id": None, "files": {}, "conflicts": []}
    data.setdefault("last_seen_epoch", 0)
    data.setdefault("last_manifest_id", None)
    data.setdefault("files", {})
    data.setdefault("conflicts", [])
    return data


def _save_local_sync_state(brain_dir: str | Path, state: dict[str, Any]) -> None:
    sp = sync_state_path(brain_dir)
    sp.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse_or_link(str(sp.parent)) or (os.path.lexists(str(sp)) and _is_reparse_or_link(str(sp))):
        raise SyncSecurityError(f"Refusing to write symlinked sync state: {sp}")
    atomic_write_bytes(str(sp), (json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))


def _resolve_master_key(cfg: dict[str, Any], *, passphrase: str | None = None) -> bytes:
    kdf_mode = str(cfg.get("kdf_mode") or "keyfile")
    if kdf_mode == "keyfile":
        key_b64 = str(cfg.get("master_key_b64") or "")
        if not key_b64:
            raise SyncSecurityError("Sync config missing master_key_b64.")
        key = base64.b64decode(key_b64)
        if len(key) != KEY_BYTES:
            raise SyncSecurityError("Invalid master key length.")
        return key
    if kdf_mode == "passphrase":
        if not passphrase:
            raise SyncSecurityError("Passphrase is required to unlock this E2EE sync vault.")
        salt = base64.b64decode(str(cfg["kdf_salt_b64"]))
        return derive_master_key_from_passphrase(passphrase, salt)
    raise SyncSecurityError(f"Unsupported sync kdf_mode: {kdf_mode!r}")


# ─── Public High-Level Sync API ──────────────────────────────────────────────

def init_sync(
    target: str,
    *,
    backend_type: str = "local_folder",
    brain_dir: str | Path | None = None,
    device_name: str | None = None,
    passphrase: str | None = None,
    bearer_token_env: str | None = None,
    state_dir: Path | None = None,
    http_transport: httpx.BaseTransport | None = None,
    skip_dns_check_for_mock_transport: bool = False,
) -> dict[str, Any]:
    """Initialize a new Zero-Knowledge E2EE sync target and local device identity."""
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    vault_id = secrets.token_hex(16)
    host_slug = re.sub(r"[^a-zA-Z0-9_-]+", "", (device_name or socket.gethostname() or "device"))[:20] or "device"
    device_id = f"{host_slug}-{secrets.token_hex(4)}"
    priv_b64, pub_b64 = _generate_device_keypair()

    if passphrase is not None:
        salt = os.urandom(32)
        master_key = derive_master_key_from_passphrase(passphrase, salt)
        kdf_mode = "passphrase"
        salt_b64 = base64.b64encode(salt).decode("ascii")
        stored_key_b64 = None
        recovery_key_b64 = None
    else:
        master_key = os.urandom(KEY_BYTES)
        kdf_mode = "keyfile"
        salt_b64 = None
        stored_key_b64 = base64.b64encode(master_key).decode("ascii")
        recovery_key_b64 = stored_key_b64

    cfg: dict[str, Any] = {
        "schema_version": SYNC_SCHEMA_VERSION,
        "vault_id": vault_id,
        "device_id": device_id,
        "device_privkey_b64": priv_b64,
        "device_pubkey_b64": pub_b64,
        "backend_type": backend_type,
        "target": str(Path(os.path.abspath(os.path.expanduser(target))) if backend_type == "local_folder" else target.strip().rstrip("/")),
        "bearer_token_env": bearer_token_env,
        "kdf_mode": kdf_mode,
        "kdf_salt_b64": salt_b64,
        "master_key_b64": stored_key_b64,
        "trusted_peers": {device_id: pub_b64},
        "created_at": _utc_now_iso(),
    }
    backend = _backend_from_config(
        cfg,
        http_transport=http_transport,
        skip_dns_check_for_mock_transport=skip_dns_check_for_mock_transport,
    )
    if backend.exists("vault-header.json"):
        raise SyncError("Sync target already contains a vault-header.json. Use `join_sync` to pair this device instead.")

    # Write non-secret zero-knowledge vault header (key verifier encrypted under master_key)
    key_check = encrypt_object(
        master_key,
        vault_id=vault_id,
        kind="header",
        object_id="key-check",
        plaintext=f"second-brain-e2ee-v1:{vault_id}".encode("utf-8"),
    )
    header_doc = {
        "schema_version": SYNC_SCHEMA_VERSION,
        "vault_id": vault_id,
        "kdf_mode": kdf_mode,
        "kdf_salt_b64": salt_b64,
        "scrypt_params": {"n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P} if kdf_mode == "passphrase" else None,
        "key_check_b64": base64.b64encode(key_check).decode("ascii"),
        "created_at": cfg["created_at"],
    }
    backend.put_object("vault-header.json", (json.dumps(header_doc, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    _atomic_write_restricted_json(sync_config_path(active_brain, state_dir=state_dir), cfg)
    _save_local_sync_state(
        active_brain,
        {"schema_version": SYNC_SCHEMA_VERSION, "last_seen_epoch": 0, "last_manifest_id": None, "files": {}, "conflicts": []},
    )
    return {
        "status": "initialized",
        "vault_id": vault_id,
        "device_id": device_id,
        "device_pubkey_b64": pub_b64,
        "backend_type": backend_type,
        "target": cfg["target"],
        "kdf_mode": kdf_mode,
        "recovery_key_b64": recovery_key_b64,
    }


def join_sync(
    target: str,
    *,
    backend_type: str = "local_folder",
    brain_dir: str | Path | None = None,
    device_name: str | None = None,
    passphrase: str | None = None,
    recovery_key_b64: str | None = None,
    bearer_token_env: str | None = None,
    state_dir: Path | None = None,
    http_transport: httpx.BaseTransport | None = None,
    skip_dns_check_for_mock_transport: bool = False,
) -> dict[str, Any]:
    """Pair a device to an existing E2EE sync target, verifying master key ownership first."""
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    norm_target = str(Path(os.path.abspath(os.path.expanduser(target))) if backend_type == "local_folder" else target.strip().rstrip("/"))
    probe_cfg = {"backend_type": backend_type, "target": norm_target, "bearer_token_env": bearer_token_env}
    backend = _backend_from_config(
        probe_cfg,
        http_transport=http_transport,
        skip_dns_check_for_mock_transport=skip_dns_check_for_mock_transport,
    )
    raw_hdr = backend.get_object("vault-header.json", max_bytes=MAX_SYNC_HEADER_BYTES)
    if raw_hdr is None:
        raise SyncError("No vault-header.json found at sync target. Run `init_sync` first.")
    hdr = json.loads(raw_hdr.decode("utf-8"))
    vault_id = str(hdr.get("vault_id") or "")
    if not _HEX_ID_RE.fullmatch(vault_id):
        raise SyncSecurityError("Malformed vault_id in remote vault-header.json.")
    kdf_mode = str(hdr.get("kdf_mode") or "")
    salt_b64 = hdr.get("kdf_salt_b64")
    if kdf_mode == "passphrase":
        if not passphrase or not salt_b64:
            raise SyncSecurityError("Passphrase is required to join passphrase-encrypted sync target.")
        master_key = derive_master_key_from_passphrase(passphrase, base64.b64decode(str(salt_b64)))
        stored_key_b64 = None
    elif kdf_mode == "keyfile":
        if not recovery_key_b64:
            raise SyncSecurityError("recovery_key_b64 is required to join keyfile-encrypted sync target.")
        master_key = base64.b64decode(recovery_key_b64.strip())
        if len(master_key) != KEY_BYTES:
            raise SyncSecurityError("Invalid recovery key length (expected 32 bytes).")
        stored_key_b64 = base64.b64encode(master_key).decode("ascii")
    else:
        raise SyncSecurityError(f"Unsupported remote kdf_mode: {kdf_mode!r}")

    # Verify master key decrypts the remote key_check envelope cleanly!
    kc_env = base64.b64decode(str(hdr.get("key_check_b64") or ""))
    kc_plain = decrypt_object(master_key, vault_id=vault_id, kind="header", object_id="key-check", envelope=kc_env)
    if kc_plain != f"second-brain-e2ee-v1:{vault_id}".encode("utf-8"):
        raise SyncSecurityError("Master key verification failed against remote vault-header.json.")

    host_slug = re.sub(r"[^a-zA-Z0-9_-]+", "", (device_name or socket.gethostname() or "device"))[:20] or "device"
    device_id = f"{host_slug}-{secrets.token_hex(4)}"
    priv_b64, pub_b64 = _generate_device_keypair()
    trusted_peers: dict[str, str] = {device_id: pub_b64}

    # If a valid signed + master-key-authenticated remote head already exists, pin its device pubkey
    raw_head = backend.get_object("head.json", max_bytes=MAX_SYNC_HEADER_BYTES)
    if raw_head is not None:
        try:
            head = json.loads(raw_head.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SyncSecurityError(f"Malformed remote head.json: {exc}") from exc
        if not isinstance(head, dict):
            raise SyncSecurityError("Malformed remote head.json.")
        if str(head.get("vault_id")) != vault_id:
            raise SyncSecurityError("Remote head.json vault_id does not match vault-header.json.")
        _verify_head_mac(master_key, vault_id, head)
        _verify_head_signature(head)
        manifest_id = str(head.get("manifest_id") or "")
        if not _HEX_ID_RE.fullmatch(manifest_id):
            raise SyncSecurityError("Malformed manifest_id in remote head.json.")
        manifest_ct = backend.get_object(f"manifests/{manifest_id}.enc", max_bytes=MAX_SYNC_MANIFEST_BYTES)
        if manifest_ct is None or content_sha256(manifest_ct) != str(head.get("manifest_ciphertext_sha256") or ""):
            raise SyncSecurityError("Remote head.json manifest ciphertext verification failed during join.")
        decrypt_object(master_key, vault_id=vault_id, kind="manifest", object_id=manifest_id, envelope=manifest_ct)
        trusted_peers[str(head["device_id"])] = str(head["device_pubkey_b64"])

    cfg = {
        "schema_version": SYNC_SCHEMA_VERSION,
        "vault_id": vault_id,
        "device_id": device_id,
        "device_privkey_b64": priv_b64,
        "device_pubkey_b64": pub_b64,
        "backend_type": backend_type,
        "target": norm_target,
        "bearer_token_env": bearer_token_env,
        "kdf_mode": kdf_mode,
        "kdf_salt_b64": salt_b64,
        "master_key_b64": stored_key_b64,
        "trusted_peers": trusted_peers,
        "created_at": _utc_now_iso(),
    }
    _atomic_write_restricted_json(sync_config_path(active_brain, state_dir=state_dir), cfg)
    _save_local_sync_state(
        active_brain,
        {"schema_version": SYNC_SCHEMA_VERSION, "last_seen_epoch": 0, "last_manifest_id": None, "files": {}, "conflicts": []},
    )
    return {
        "status": "joined",
        "vault_id": vault_id,
        "device_id": device_id,
        "device_pubkey_b64": pub_b64,
        "backend_type": backend_type,
        "target": norm_target,
        "trusted_peer_count": len(trusted_peers),
    }


def sync_status(
    *,
    brain_dir: str | Path | None = None,
    state_dir: Path | None = None,
    http_transport: httpx.BaseTransport | None = None,
    skip_dns_check_for_mock_transport: bool = False,
) -> dict[str, Any]:
    """Return read-only sync configuration, epoch state, pending local changes, and open conflicts."""
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    cfg_path = sync_config_path(active_brain, state_dir=state_dir)
    if not cfg_path.exists():
        return {
            "configured": False,
            "brain_dir": str(active_brain),
            "conflicts": [],
            "conflict_count": 0,
        }
    cfg = _load_sync_config(active_brain, state_dir=state_dir)
    local_state = _load_local_sync_state(active_brain)
    current_files = scan_durable_vault_files(active_brain)
    base_files = dict(local_state.get("files") or {})
    local_changed = sorted(
        p for p, info in current_files.items() if base_files.get(p) != info["sha256"]
    )
    local_deleted = sorted(p for p in base_files if p not in current_files)

    remote_epoch = 0
    remote_device_id = None
    remote_reachable = True
    remote_error = None
    try:
        backend = _backend_from_config(
            cfg,
            http_transport=http_transport,
            skip_dns_check_for_mock_transport=skip_dns_check_for_mock_transport,
        )
        raw_head = backend.get_object("head.json", max_bytes=MAX_SYNC_HEADER_BYTES)
        if raw_head is not None:
            head = json.loads(raw_head.decode("utf-8"))
            remote_epoch = int(head.get("epoch", 0))
            remote_device_id = head.get("device_id")
    except Exception as exc:
        remote_reachable = False
        remote_error = str(exc)

    conflicts = list(local_state.get("conflicts") or [])
    return {
        "configured": True,
        "vault_id": cfg["vault_id"],
        "device_id": cfg["device_id"],
        "backend_type": cfg["backend_type"],
        "target": cfg["target"],
        "kdf_mode": cfg["kdf_mode"],
        "local_epoch": int(local_state.get("last_seen_epoch", 0)),
        "remote_epoch": remote_epoch,
        "remote_device_id": remote_device_id,
        "remote_reachable": remote_reachable,
        "remote_error": remote_error,
        "durable_file_count": len(current_files),
        "local_changed_count": len(local_changed),
        "local_deleted_count": len(local_deleted),
        "trusted_peer_count": len(cfg.get("trusted_peers") or {}),
        "conflicts": conflicts,
        "conflict_count": len(conflicts),
    }


def _verify_and_read_remote_head(
    backend: SyncBackend,
    cfg: dict[str, Any],
    *,
    master_key: bytes,
    local_state: dict[str, Any],
    trust_new_peers: bool = False,
) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
    raw_head = backend.get_object("head.json", max_bytes=MAX_SYNC_HEADER_BYTES)
    if raw_head is None:
        return None, None
    try:
        head = json.loads(raw_head.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncSecurityError(f"Malformed remote head.json: {exc}") from exc
    if not isinstance(head, dict):
        raise SyncSecurityError("Malformed remote head.json.")
    vault_id = str(cfg["vault_id"])
    if str(head.get("vault_id")) != vault_id:
        raise SyncSecurityError("Remote head.json belongs to a different vault_id.")
    _verify_head_mac(master_key, vault_id, head)
    _verify_head_signature(head)

    remote_epoch = int(head.get("epoch", 0))
    local_epoch = int(local_state.get("last_seen_epoch", 0))
    if remote_epoch < local_epoch:
        raise SyncSecurityError(
            f"Anti-rollback check failed: remote epoch {remote_epoch} < local epoch {local_epoch}."
        )

    dev_id = str(head["device_id"])
    pub_b64 = str(head["device_pubkey_b64"])
    if len(pub_b64) > 128:
        raise SyncSecurityError("Oversized device_pubkey_b64 in remote sync head.")
    trusted = dict(cfg.get("trusted_peers") or {})
    new_peer: tuple[str, str] | None = None
    if dev_id in trusted:
        if not hmac.compare_digest(trusted[dev_id], pub_b64):
            raise SyncSecurityError(f"Pinned Ed25519 key mismatch for peer device '{dev_id}'!")
    else:
        if not trust_new_peers:
            raise SyncSecurityError(
                f"Untrusted peer device '{dev_id}' signed remote head. Pass trust_new_peers=True or join/trust explicitly."
            )
        if len(trusted) >= MAX_TRUSTED_PEERS:
            raise SyncSecurityError(f"Maximum trusted sync peers ({MAX_TRUSTED_PEERS}) exceeded.")
        new_peer = (dev_id, pub_b64)
    return head, new_peer


def sync_push(
    *,
    brain_dir: str | Path | None = None,
    passphrase: str | None = None,
    state_dir: Path | None = None,
    http_transport: httpx.BaseTransport | None = None,
    skip_dns_check_for_mock_transport: bool = False,
) -> dict[str, Any]:
    """Encrypt durable vault files (deduplicated via blind blob IDs) and push a signed manifest."""
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    cfg_path = sync_config_path(active_brain, state_dir=state_dir)
    cfg = _load_sync_config(active_brain, state_dir=state_dir)
    master_key = _resolve_master_key(cfg, passphrase=passphrase)
    backend = _backend_from_config(
        cfg,
        http_transport=http_transport,
        skip_dns_check_for_mock_transport=skip_dns_check_for_mock_transport,
    )
    lock_path = active_brain / ".sidecar" / "writer.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with review_lock(str(lock_path)):
        local_state = _load_local_sync_state(active_brain)
        remote_head, _ = _verify_and_read_remote_head(
            backend,
            cfg,
            master_key=master_key,
            local_state=local_state,
            trust_new_peers=True,
        )
        local_epoch = int(local_state.get("last_seen_epoch", 0))
        if remote_head is not None and int(remote_head["epoch"]) > local_epoch:
            raise SyncConflictError(
                f"Remote sync head (epoch {remote_head['epoch']} by {remote_head['device_id']}) is ahead of "
                f"local epoch {local_epoch}. Run `second-brain sync pull` first to merge safely."
            )

        inventory = scan_durable_vault_files(active_brain)
        vault_id = str(cfg["vault_id"])
        manifest_files: dict[str, dict[str, Any]] = {}
        uploaded_blobs = 0
        reused_blobs = 0

        for rel_path, meta in sorted(inventory.items()):
            plain_sha = str(meta["sha256"])
            blob_id = compute_blind_blob_id(master_key, vault_id, plain_sha)
            obj_key = f"objects/{blob_id[:2]}/{blob_id}.enc"
            if backend.exists(obj_key):
                reused_blobs += 1
            else:
                raw_bytes = (active_brain / rel_path).read_bytes()
                if content_sha256(raw_bytes) != plain_sha:
                    plain_sha = content_sha256(raw_bytes)
                    blob_id = compute_blind_blob_id(master_key, vault_id, plain_sha)
                    obj_key = f"objects/{blob_id[:2]}/{blob_id}.enc"
                ct = encrypt_object(
                    master_key,
                    vault_id=vault_id,
                    kind="blob",
                    object_id=blob_id,
                    plaintext=raw_bytes,
                )
                backend.put_object(obj_key, ct)
                uploaded_blobs += 1
            manifest_files[rel_path] = {
                "blob_id": blob_id,
                "sha256": plain_sha,
                "size": int(meta["size"]),
                "append_only_jsonl": rel_path in _APPEND_ONLY_JSONL_PATHS,
            }

        new_epoch = (int(remote_head["epoch"]) if remote_head is not None else local_epoch) + 1
        parent_mid = str(remote_head["manifest_id"]) if remote_head is not None else local_state.get("last_manifest_id")
        created_at = _utc_now_iso()
        manifest_doc = {
            "schema_version": SYNC_SCHEMA_VERSION,
            "vault_id": vault_id,
            "epoch": new_epoch,
            "parent_manifest_id": parent_mid,
            "device_id": cfg["device_id"],
            "created_at": created_at,
            "file_count": len(manifest_files),
            "files": manifest_files,
        }
        manifest_plain = json.dumps(manifest_doc, sort_keys=True, ensure_ascii=False).encode("utf-8")
        manifest_id = hashlib.sha256(
            f"{vault_id}:{new_epoch}:{created_at}:".encode("utf-8") + manifest_plain
        ).hexdigest()
        manifest_ct = encrypt_object(
            master_key,
            vault_id=vault_id,
            kind="manifest",
            object_id=manifest_id,
            plaintext=manifest_plain,
        )
        backend.put_object(f"manifests/{manifest_id}.enc", manifest_ct)

        head_doc: dict[str, Any] = {
            "schema_version": SYNC_SCHEMA_VERSION,
            "vault_id": vault_id,
            "epoch": new_epoch,
            "parent_manifest_id": parent_mid,
            "manifest_id": manifest_id,
            "manifest_ciphertext_sha256": content_sha256(manifest_ct),
            "device_id": cfg["device_id"],
            "device_pubkey_b64": cfg["device_pubkey_b64"],
            "created_at": created_at,
        }
        head_doc["signature_b64"] = _sign_head(head_doc, str(cfg["device_privkey_b64"]))
        head_doc["head_mac"] = _compute_head_mac(master_key, vault_id, head_doc)
        backend.put_object("head.json", (json.dumps(head_doc, indent=2, sort_keys=True) + "\n").encode("utf-8"))

        local_state["last_seen_epoch"] = new_epoch
        local_state["last_manifest_id"] = manifest_id
        local_state["files"] = {rel: entry["sha256"] for rel, entry in manifest_files.items()}
        _save_local_sync_state(active_brain, local_state)

        return {
            "status": "pushed",
            "vault_id": vault_id,
            "device_id": cfg["device_id"],
            "epoch": new_epoch,
            "manifest_id": manifest_id,
            "total_files": len(manifest_files),
            "uploaded_blobs": uploaded_blobs,
            "reused_blobs": reused_blobs,
        }


def sync_pull(
    *,
    brain_dir: str | Path | None = None,
    passphrase: str | None = None,
    trust_new_peers: bool = True,
    state_dir: Path | None = None,
    http_transport: httpx.BaseTransport | None = None,
    skip_dns_check_for_mock_transport: bool = False,
) -> dict[str, Any]:
    """Pull, cryptographically verify, 3-way merge, quarantine conflicts, and rebuild local sidecar indexes."""
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    cfg_path = sync_config_path(active_brain, state_dir=state_dir)
    cfg = _load_sync_config(active_brain, state_dir=state_dir)
    master_key = _resolve_master_key(cfg, passphrase=passphrase)
    backend = _backend_from_config(
        cfg,
        http_transport=http_transport,
        skip_dns_check_for_mock_transport=skip_dns_check_for_mock_transport,
    )
    lock_path = active_brain / ".sidecar" / "writer.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with review_lock(str(lock_path)):
        local_state = _load_local_sync_state(active_brain)
        remote_head, pending_new_peer = _verify_and_read_remote_head(
            backend,
            cfg,
            master_key=master_key,
            local_state=local_state,
            trust_new_peers=trust_new_peers,
        )
        if remote_head is None:
            return {
                "status": "empty_remote",
                "epoch": int(local_state.get("last_seen_epoch", 0)),
                "added": 0,
                "updated": 0,
                "deleted": 0,
                "merged_jsonl": 0,
                "conflicts": [],
            }

        vault_id = str(cfg["vault_id"])
        manifest_id = str(remote_head["manifest_id"])
        if not _HEX_ID_RE.fullmatch(manifest_id):
            raise SyncSecurityError(f"Malformed manifest_id in remote head: {manifest_id!r}")
        manifest_ct = backend.get_object(f"manifests/{manifest_id}.enc", max_bytes=MAX_SYNC_MANIFEST_BYTES)
        if manifest_ct is None:
            raise SyncSecurityError(f"Remote manifest '{manifest_id}' is missing.")
        if content_sha256(manifest_ct) != str(remote_head["manifest_ciphertext_sha256"]):
            raise SyncSecurityError("Remote manifest ciphertext SHA-256 does not match signed head.json.")

        manifest_plain = decrypt_object(
            master_key,
            vault_id=vault_id,
            kind="manifest",
            object_id=manifest_id,
            envelope=manifest_ct,
        )
        manifest = json.loads(manifest_plain.decode("utf-8"))
        if str(manifest.get("vault_id")) != vault_id or int(manifest.get("epoch", -1)) != int(remote_head["epoch"]):
            raise SyncSecurityError("Decrypted manifest vault_id/epoch disagrees with signed head.json.")

        # Persist newly learned peer pubkey ONLY after AEAD manifest decryption succeeded under master_key!
        if pending_new_peer is not None:
            trusted = dict(cfg.get("trusted_peers") or {})
            trusted[pending_new_peer[0]] = pending_new_peer[1]
            cfg["trusted_peers"] = trusted
            _atomic_write_restricted_json(cfg_path, cfg)

        remote_files = manifest.get("files") or {}
        if not isinstance(remote_files, dict):
            raise SyncSecurityError("Malformed files table in decrypted sync manifest.")

        # Phase 1: Validate & decrypt ALL needed blobs into memory before mutating any local file (fail-closed!)
        base_files: dict[str, str] = dict(local_state.get("files") or {})
        local_inventory = scan_durable_vault_files(active_brain)
        decrypted_incoming: dict[str, tuple[bytes, str]] = {}

        for rel_path, entry in sorted(remote_files.items()):
            clean_rel = validate_sync_rel_path(rel_path)
            remote_sha = str(entry.get("sha256") or "")
            blob_id = str(entry.get("blob_id") or "")
            if not _HEX_ID_RE.fullmatch(remote_sha) or not _HEX_ID_RE.fullmatch(blob_id):
                raise SyncSecurityError(f"Malformed hashes in manifest for {clean_rel}.")
            expected_blob_id = compute_blind_blob_id(master_key, vault_id, remote_sha)
            if not hmac.compare_digest(blob_id, expected_blob_id):
                raise SyncSecurityError(f"Blind blob_id mismatch for {clean_rel} (possible manifest tampering).")

            local_meta = local_inventory.get(clean_rel)
            if local_meta is not None and local_meta["sha256"] == remote_sha:
                continue  # Already identical locally

            obj_key = f"objects/{blob_id[:2]}/{blob_id}.enc"
            blob_ct = backend.get_object(obj_key)
            if blob_ct is None:
                raise SyncSecurityError(f"Referenced encrypted blob missing on remote store for {clean_rel}.")
            plain = decrypt_object(
                master_key,
                vault_id=vault_id,
                kind="blob",
                object_id=blob_id,
                envelope=blob_ct,
            )
            if content_sha256(plain) != remote_sha:
                raise SyncSecurityError(f"Decrypted plaintext SHA-256 mismatch for {clean_rel}.")
            decrypted_incoming[clean_rel] = (plain, remote_sha)

        # Phase 2: Apply conflict-safe 3-way merge atomically
        added = 0
        updated = 0
        deleted = 0
        merged_jsonl = 0
        decision_log_touched = False
        new_conflicts: list[dict[str, Any]] = []
        new_base_files = dict(base_files)

        for clean_rel, (remote_bytes, remote_sha) in sorted(decrypted_incoming.items()):
            dest = active_brain.joinpath(*clean_rel.split("/"))
            _assert_no_symlink_chain(dest, stop_at=active_brain)
            local_meta = local_inventory.get(clean_rel)
            base_sha = base_files.get(clean_rel)

            if local_meta is None:
                # New remote file
                dest.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(str(dest), remote_bytes)
                new_base_files[clean_rel] = remote_sha
                added += 1
                if clean_rel == "0-raw/archive/decision-record.jsonl":
                    decision_log_touched = True
                continue

            local_sha = str(local_meta["sha256"])
            if base_sha == local_sha:
                # Clean fast-forward: local unchanged since last sync
                _backup_before_sync_overwrite(active_brain, clean_rel, dest.read_bytes())
                atomic_write_bytes(str(dest), remote_bytes)
                new_base_files[clean_rel] = remote_sha
                updated += 1
                if clean_rel == "0-raw/archive/decision-record.jsonl":
                    decision_log_touched = True
                continue

            if base_sha == remote_sha:
                # Local modified, remote unchanged -> keep local
                continue

            # Both local and remote changed since base!
            if clean_rel in _APPEND_ONLY_JSONL_PATHS:
                merged = _merge_append_only_jsonl(dest.read_bytes(), remote_bytes)
                atomic_write_bytes(str(dest), merged)
                new_base_files[clean_rel] = content_sha256(merged)
                merged_jsonl += 1
                if clean_rel == "0-raw/archive/decision-record.jsonl":
                    decision_log_touched = True
            else:
                conflict_rel = _quarantine_conflict_copy(
                    active_brain,
                    clean_rel,
                    remote_bytes,
                    remote_device_id=str(remote_head["device_id"]),
                    remote_epoch=int(remote_head["epoch"]),
                )
                new_conflicts.append(
                    {
                        "rel_path": clean_rel,
                        "conflict_copy": conflict_rel,
                        "local_sha256": local_sha,
                        "remote_sha256": remote_sha,
                        "remote_device_id": str(remote_head["device_id"]),
                        "remote_epoch": int(remote_head["epoch"]),
                        "recorded_at": _utc_now_iso(),
                    }
                )

        # Propagate remote file deletions (paths in base_files omitted from remote_files)
        for prev_rel, base_sha in sorted(base_files.items()):
            if prev_rel in remote_files or prev_rel in _APPEND_ONLY_JSONL_PATHS:
                continue
            clean_rel = validate_sync_rel_path(prev_rel)
            local_meta = local_inventory.get(clean_rel)
            if local_meta is None:
                new_base_files.pop(clean_rel, None)
                continue
            local_sha = str(local_meta["sha256"])
            dest = active_brain.joinpath(*clean_rel.split("/"))
            _assert_no_symlink_chain(dest, stop_at=active_brain)
            if local_sha == str(base_sha):
                if dest.is_file():
                    _backup_before_sync_overwrite(active_brain, clean_rel, dest.read_bytes())
                    dest.unlink()
                new_base_files.pop(clean_rel, None)
                deleted += 1
            else:
                # Modify/delete divergence: preserve local modified file on disk & record conflict!
                new_base_files.pop(clean_rel, None)
                new_conflicts.append(
                    {
                        "rel_path": clean_rel,
                        "conflict_copy": None,
                        "conflict_kind": "modify_delete",
                        "local_sha256": local_sha,
                        "remote_sha256": None,
                        "remote_device_id": str(remote_head["device_id"]),
                        "remote_epoch": int(remote_head["epoch"]),
                        "recorded_at": _utc_now_iso(),
                    }
                )

        # Record unchanged files that match remote in cursor
        for clean_rel, entry in remote_files.items():
            if clean_rel not in decrypted_incoming and clean_rel in local_inventory:
                new_base_files[clean_rel] = str(entry["sha256"])

        existing_conflicts = list(local_state.get("conflicts") or [])
        existing_conflicts.extend(new_conflicts)
        local_state["last_seen_epoch"] = int(remote_head["epoch"])
        local_state["last_manifest_id"] = manifest_id
        local_state["files"] = new_base_files
        local_state["conflicts"] = existing_conflicts
        _save_local_sync_state(active_brain, local_state)

        if decision_log_touched:
            invalidate_decision_log_cache()

        indexes_rebuilt = False
        if added > 0 or updated > 0 or deleted > 0 or merged_jsonl > 0:
            atoms_dir = active_brain / "2-atoms"
            lex_path = active_brain / ".sidecar" / "lexical.sqlite3"
            atoms_dir.mkdir(parents=True, exist_ok=True)
            rebuild_lexical_index(str(atoms_dir), str(lex_path))
            indexes_rebuilt = True

    if indexes_rebuilt or deleted > 0:
        try:
            ensure_notes_index(NotePaths.for_brain(active_brain))
        except Exception:
            pass

    return {
        "status": "pulled",
        "vault_id": vault_id,
        "epoch": int(remote_head["epoch"]),
        "manifest_id": manifest_id,
        "remote_device_id": str(remote_head["device_id"]),
        "added": added,
        "updated": updated,
        "deleted": deleted,
        "merged_jsonl": merged_jsonl,
        "conflict_count": len(new_conflicts),
        "conflicts": new_conflicts,
        "indexes_rebuilt": indexes_rebuilt,
    }


def sync_verify(
    *,
    brain_dir: str | Path | None = None,
    passphrase: str | None = None,
    state_dir: Path | None = None,
    http_transport: httpx.BaseTransport | None = None,
    skip_dns_check_for_mock_transport: bool = False,
) -> dict[str, Any]:
    """Read-only full cryptographic audit of remote vault-header, head signature, manifest, and every blob."""
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    cfg = _load_sync_config(active_brain, state_dir=state_dir)
    master_key = _resolve_master_key(cfg, passphrase=passphrase)
    backend = _backend_from_config(
        cfg,
        http_transport=http_transport,
        skip_dns_check_for_mock_transport=skip_dns_check_for_mock_transport,
    )
    vault_id = str(cfg["vault_id"])
    issues: list[str] = []

    raw_hdr = backend.get_object("vault-header.json", max_bytes=MAX_SYNC_HEADER_BYTES)
    if raw_hdr is None:
        return {"valid": False, "vault_id": vault_id, "verified_blobs": 0, "issues": ["Missing vault-header.json"]}
    hdr = json.loads(raw_hdr.decode("utf-8"))
    if str(hdr.get("vault_id")) != vault_id:
        issues.append("vault-header.json vault_id mismatch")
    try:
        kc = decrypt_object(
            master_key,
            vault_id=vault_id,
            kind="header",
            object_id="key-check",
            envelope=base64.b64decode(str(hdr.get("key_check_b64") or "")),
        )
        if kc != f"second-brain-e2ee-v1:{vault_id}".encode("utf-8"):
            issues.append("key-check plaintext mismatch")
    except Exception as exc:
        issues.append(f"key-check failed: {exc}")

    local_state = _load_local_sync_state(active_brain)
    try:
        head, _ = _verify_and_read_remote_head(
            backend,
            cfg,
            master_key=master_key,
            local_state=local_state,
            trust_new_peers=True,
        )
    except Exception as exc:
        return {"valid": False, "vault_id": vault_id, "verified_blobs": 0, "issues": [*issues, str(exc)]}

    if head is None:
        return {
            "valid": len(issues) == 0,
            "vault_id": vault_id,
            "epoch": 0,
            "verified_blobs": 0,
            "issues": issues,
        }

    manifest_id = str(head["manifest_id"])
    manifest_ct = backend.get_object(f"manifests/{manifest_id}.enc", max_bytes=MAX_SYNC_MANIFEST_BYTES)
    if manifest_ct is None:
        issues.append(f"Missing manifest {manifest_id}")
        return {"valid": False, "vault_id": vault_id, "epoch": int(head["epoch"]), "verified_blobs": 0, "issues": issues}
    if content_sha256(manifest_ct) != str(head["manifest_ciphertext_sha256"]):
        issues.append("Manifest ciphertext hash does not match signed head.json")

    try:
        manifest_plain = decrypt_object(
            master_key,
            vault_id=vault_id,
            kind="manifest",
            object_id=manifest_id,
            envelope=manifest_ct,
        )
        manifest = json.loads(manifest_plain.decode("utf-8"))
    except Exception as exc:
        issues.append(f"Manifest decrypt failed: {exc}")
        return {"valid": False, "vault_id": vault_id, "epoch": int(head["epoch"]), "verified_blobs": 0, "issues": issues}

    verified_blobs = 0
    for rel_path, entry in sorted((manifest.get("files") or {}).items()):
        try:
            validate_sync_rel_path(rel_path)
            blob_id = str(entry["blob_id"])
            expected_sha = str(entry["sha256"])
            if not hmac.compare_digest(blob_id, compute_blind_blob_id(master_key, vault_id, expected_sha)):
                issues.append(f"Blind blob_id mismatch for {rel_path}")
                continue
            blob_ct = backend.get_object(f"objects/{blob_id[:2]}/{blob_id}.enc")
            if blob_ct is None:
                issues.append(f"Missing encrypted blob for {rel_path}")
                continue
            plain = decrypt_object(master_key, vault_id=vault_id, kind="blob", object_id=blob_id, envelope=blob_ct)
            if content_sha256(plain) != expected_sha:
                issues.append(f"Plaintext SHA-256 mismatch for {rel_path}")
                continue
            verified_blobs += 1
        except Exception as exc:
            issues.append(f"Blob verification failed for {rel_path}: {exc}")

    return {
        "valid": len(issues) == 0,
        "vault_id": vault_id,
        "epoch": int(head["epoch"]),
        "manifest_id": manifest_id,
        "device_id": str(head["device_id"]),
        "verified_blobs": verified_blobs,
        "issues": issues,
    }


def list_sync_conflicts(*, brain_dir: str | Path | None = None) -> list[dict[str, Any]]:
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    state = _load_local_sync_state(active_brain)
    return list(state.get("conflicts") or [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zero-Knowledge E2EE Multi-Device Vault Sync")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Initialize an encrypted sync target and local device identity")
    p_init.add_argument("target", help="Local sync folder (OneDrive/Syncthing/NAS/USB) or https:// object store URL")
    p_init.add_argument("--backend", choices=("local_folder", "http_object"), default="local_folder")
    p_init.add_argument("--device-name", default=None)
    p_init.add_argument("--passphrase-env", default=None, help="Env var holding optional passphrase (defaults to 256-bit keyfile)")
    p_init.add_argument("--bearer-token-env", default=None, help="Env var holding Bearer token for http_object backend")
    p_init.add_argument("--json", action="store_true")

    p_join = sub.add_parser("join", help="Pair this device to an existing encrypted sync target")
    p_join.add_argument("target", help="Local sync folder or https:// object store URL")
    p_join.add_argument("--backend", choices=("local_folder", "http_object"), default="local_folder")
    p_join.add_argument("--device-name", default=None)
    p_join.add_argument("--recovery-key-env", default=None, help="Env var holding base64 256-bit recovery key")
    p_join.add_argument("--passphrase-env", default=None, help="Env var holding vault passphrase")
    p_join.add_argument("--bearer-token-env", default=None)
    p_join.add_argument("--json", action="store_true")

    p_status = sub.add_parser("status", help="Show E2EE sync status, epochs, and pending conflicts")
    p_status.add_argument("--json", action="store_true")

    p_push = sub.add_parser("push", help="Encrypt changed durable vault files and push signed manifest")
    p_push.add_argument("--passphrase-env", default=None)
    p_push.add_argument("--json", action="store_true")

    p_pull = sub.add_parser("pull", help="Pull, verify, 3-way merge, and rebuild local sidecar indexes")
    p_pull.add_argument("--passphrase-env", default=None)
    p_pull.add_argument("--strict-peers", action="store_true", help="Reject previously unseen peer devices")
    p_pull.add_argument("--json", action="store_true")

    p_ver = sub.add_parser("verify", help="Verify remote E2EE signatures, manifest, and every encrypted blob")
    p_ver.add_argument("--passphrase-env", default=None)
    p_ver.add_argument("--json", action="store_true")

    p_conf = sub.add_parser("conflicts", help="List quarantined sync conflicts awaiting owner resolution")
    p_conf.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    brain_dir = resolve_active_brain_dir()
    passphrase = os.environ.get(args.passphrase_env) if getattr(args, "passphrase_env", None) else None

    if args.cmd == "init":
        res = init_sync(
            args.target,
            backend_type=args.backend,
            brain_dir=brain_dir,
            device_name=args.device_name,
            passphrase=passphrase,
            bearer_token_env=args.bearer_token_env,
        )
        print(json.dumps(res, indent=2) if args.json else f"Initialized E2EE sync vault={res['vault_id']} device={res['device_id']} target={res['target']}")
        return 0
    if args.cmd == "join":
        rec_key = os.environ.get(args.recovery_key_env) if args.recovery_key_env else None
        res = join_sync(
            args.target,
            backend_type=args.backend,
            brain_dir=brain_dir,
            device_name=args.device_name,
            passphrase=passphrase,
            recovery_key_b64=rec_key,
            bearer_token_env=args.bearer_token_env,
        )
        print(json.dumps(res, indent=2) if args.json else f"Joined E2EE sync vault={res['vault_id']} device={res['device_id']}")
        return 0
    if args.cmd == "status":
        res = sync_status(brain_dir=brain_dir)
        print(json.dumps(res, indent=2) if args.json else f"configured={res['configured']} local_epoch={res.get('local_epoch', 0)} remote_epoch={res.get('remote_epoch', 0)} conflicts={res['conflict_count']}")
        return 0
    if args.cmd == "push":
        res = sync_push(brain_dir=brain_dir, passphrase=passphrase)
        print(json.dumps(res, indent=2) if args.json else f"Pushed epoch={res['epoch']} uploaded={res['uploaded_blobs']} reused={res['reused_blobs']}")
        return 0
    if args.cmd == "pull":
        res = sync_pull(brain_dir=brain_dir, passphrase=passphrase, trust_new_peers=not args.strict_peers)
        print(json.dumps(res, indent=2) if args.json else f"Pulled epoch={res['epoch']} added={res['added']} updated={res['updated']} merged_jsonl={res['merged_jsonl']} conflicts={res['conflict_count']}")
        return 0
    if args.cmd == "verify":
        res = sync_verify(brain_dir=brain_dir, passphrase=passphrase)
        print(json.dumps(res, indent=2) if args.json else f"valid={res['valid']} verified_blobs={res['verified_blobs']} issues={len(res['issues'])}")
        return 0 if res["valid"] else 1
    if args.cmd == "conflicts":
        items = list_sync_conflicts(brain_dir=brain_dir)
        print(json.dumps({"count": len(items), "conflicts": items}, indent=2) if args.json else f"conflicts={len(items)}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
