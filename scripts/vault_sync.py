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
import difflib
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
import threading
import time
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlparse
import xml.etree.ElementTree as ET

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import (  # noqa: E402
    approved_revision_is_current,
    atomic_write_bytes,
    content_sha256,
    invalidate_decision_log_cache,
    review_lock,
)
from index_manifest import invalidate_atom_revision_cache, load_index_manifest  # noqa: E402
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
from keychain_store import (  # noqa: E402
    KeychainError,
    keychain_backend_name,
    load_secret,
    store_secret,
)
from lexical_store import rebuild_lexical_index  # noqa: E402
from memory_format import normalize_memory_document, render_memory_document, set_scalar_field, split_frontmatter  # noqa: E402
from notes_store import NotePaths, ensure_notes_index  # noqa: E402


SYNC_SCHEMA_VERSION = 1
ENVELOPE_MAGIC = b"SBSY1\x00"
NONCE_BYTES = 12
KEY_BYTES = 32
MAX_SYNC_FILE_BYTES = 50_000_000
MAX_SYNC_MANIFEST_BYTES = 16_000_000
MAX_SYNC_HEADER_BYTES = 65_536
MAX_SYNC_BASE_CACHE_BYTES = 524_288
MAX_S3_LIST_XML_BYTES = 4_000_000
MAX_TRUSTED_PEERS = 32
_KEYCHAIN_SERVICE = "second-brain.vault-sync"
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024

_HEX_ID_RE = re.compile(r"^(?:[0-9a-f]{2}){8,32}$")
_DEVICE_ID_RE = re.compile(r"^[a-zA-Z0-9._-]{4,64}$")
_ENV_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_S3_REGION_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_ALLOWED_TOP_FILES = frozenset({"house-rules.md"})
_ALLOWED_ROOT_DIRS = frozenset(
    {
        "0-raw",
        "2-atoms",
        "3-threads",
        "4-experiences",
        "5-playbooks",
        "6-skills",
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
        "0-raw/archive/atom-feedback.jsonl",
        "0-raw/archive/playbook-decisions.jsonl",
        "0-raw/archive/skill-decisions.jsonl",
        "0-raw/archive/creative-media-audit.jsonl",
        "0-raw/archive/mcp-audit.jsonl",
    }
)
_HISTORY_PROTECTED_PREFIXES = ("2-atoms/", "5-playbooks/", "6-skills/")


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

    def delete_object(self, key: str) -> bool:
        raise NotImplementedError

    def list_objects(self, prefix: str = "") -> list[str]:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        return self.get_object(key, max_bytes=16) is not None


def _validate_backend_key(key: str) -> str:
    if not isinstance(key, str) or not key or "\x00" in key or "\\" in key or key.startswith("/"):
        raise SyncSecurityError(f"Invalid sync object key: {key!r}")
    parts = key.split("/")
    if any(seg in {"", ".", ".."} for seg in parts):
        raise SyncSecurityError(f"Unsafe sync object key traversal: {key!r}")
    if parts[0] not in {"vault-header.json", "head.json", "manifests", "objects", "inbox-drops", "mobile-recall"}:
        raise SyncSecurityError(f"Disallowed sync object namespace: {key!r}")
    if parts[0] in {"inbox-drops", "mobile-recall"}:
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{32}\.enc", parts[1]):
            raise SyncSecurityError(f"Invalid {parts[0]} object key format: {key!r}")
    return key


def _hmac_sha256_bytes(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sign_s3_sigv4_headers(
    method: str,
    url: str,
    payload: bytes = b"",
    *,
    access_key: str,
    secret_key: str,
    region: str = "auto",
    service: str = "s3",
    session_token: str | None = None,
    amz_date: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """Compute RFC-compliant AWS SigV4 (`AWS4-HMAC-SHA256`) headers for S3 / R2 / B2 / MinIO."""
    if not access_key or not secret_key:
        raise SyncSecurityError("S3 SigV4 access_key and secret_key must be non-empty.")
    if not _S3_REGION_RE.fullmatch(region):
        raise SyncSecurityError(f"Invalid S3 region identifier: {region!r}")
    parsed = urlparse(url)
    if not parsed.hostname or parsed.scheme not in {"https", "http"}:
        raise SyncSecurityError(f"Invalid S3 request URL: {url!r}")

    now_amz = amz_date or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if len(now_amz) < 16 or not now_amz.endswith("Z"):
        raise SyncSecurityError(f"Invalid x-amz-date format: {now_amz!r}")
    date_stamp = now_amz[:8]

    if (parsed.scheme == "https" and parsed.port == 443) or (parsed.scheme == "http" and parsed.port == 80):
        host_header = parsed.hostname
    else:
        host_header = parsed.netloc

    raw_path = parsed.path or "/"
    encoded_segments = [quote(unquote(seg), safe="-_.~") for seg in raw_path.split("/")]
    canonical_uri = "/".join(encoded_segments) or "/"
    if not canonical_uri.startswith("/"):
        canonical_uri = "/" + canonical_uri

    query_pairs: list[tuple[str, str]] = []
    if parsed.query:
        for raw_part in parsed.query.split("&"):
            if not raw_part:
                continue
            if "=" in raw_part:
                qk, qv = raw_part.split("=", 1)
                query_pairs.append((unquote(qk), unquote(qv)))
            else:
                query_pairs.append((unquote(raw_part), ""))
    encoded_query = sorted((quote(k, safe="-_.~"), quote(v, safe="-_.~")) for k, v in query_pairs)
    canonical_querystring = "&".join(f"{k}={v}" for k, v in encoded_query)

    payload_hash = hashlib.sha256(payload).hexdigest()
    signable: dict[str, str] = {
        "host": host_header,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": now_amz,
    }
    if session_token:
        signable["x-amz-security-token"] = session_token.strip()

    out_headers: dict[str, str] = {
        "User-Agent": "SecondBrain-Sync/1.0",
        "Host": host_header,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": now_amz,
    }
    if session_token:
        out_headers["x-amz-security-token"] = session_token.strip()
    if extra_headers:
        for hk, hv in extra_headers.items():
            out_headers[hk] = hv
            hk_low = hk.strip().lower()
            if hk_low.startswith("x-amz-") or hk_low == "content-md5":
                signable[hk_low] = str(hv)

    sorted_keys = sorted(signable.keys())
    canonical_headers = "".join(f"{k}:{' '.join(signable[k].strip().split())}\n" for k in sorted_keys)
    signed_headers = ";".join(sorted_keys)

    canonical_request = "\n".join(
        [
            method.upper(),
            canonical_uri,
            canonical_querystring,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            now_amz,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    k_date = _hmac_sha256_bytes(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _hmac_sha256_bytes(k_date, region)
    k_service = _hmac_sha256_bytes(k_region, service)
    k_signing = _hmac_sha256_bytes(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    out_headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return out_headers


def _parse_s3_list_objects_xml(
    xml_bytes: bytes,
    *,
    key_strip_prefix: str = "",
) -> tuple[list[str], str | None]:
    """Safely parse S3 ListObjectsV2 XML, rejecting DTD/XXE declarations fail-closed."""
    if len(xml_bytes) > MAX_S3_LIST_XML_BYTES:
        raise SyncSecurityError("S3 ListObjectsV2 XML response exceeds maximum size.")
    upper_all = xml_bytes.upper()
    if b"<!DOCTYPE" in upper_all or b"<!ENTITY" in upper_all:
        raise SyncSecurityError("Rejected S3 XML response containing DTD/ENTITY declarations.")
    try:
        root = ET.fromstring(xml_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ET.ParseError) as exc:
        raise SyncSecurityError(f"Malformed S3 ListObjectsV2 XML response: {exc}") from exc

    def _local_tag(tag: str) -> str:
        return tag.rsplit("}", 1)[-1] if "}" in tag else tag

    keys: list[str] = []
    is_truncated = False
    next_token: str | None = None
    norm_strip = key_strip_prefix.strip("/")
    prefix_with_slash = f"{norm_strip}/" if norm_strip else ""

    for child in root:
        ctag = _local_tag(child.tag)
        if ctag == "IsTruncated" and (child.text or "").strip().lower() == "true":
            is_truncated = True
        elif ctag == "NextContinuationToken" and (child.text or "").strip():
            next_token = (child.text or "").strip()
        elif ctag == "Contents":
            for sub in child:
                if _local_tag(sub.tag) == "Key" and (sub.text or "").strip():
                    raw_k = (sub.text or "").strip().lstrip("/")
                    if prefix_with_slash:
                        if not raw_k.startswith(prefix_with_slash):
                            continue
                        raw_k = raw_k[len(prefix_with_slash) :]
                    try:
                        keys.append(_validate_backend_key(raw_k))
                    except SyncSecurityError:
                        continue
    return keys, (next_token if is_truncated else None)


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

    def delete_object(self, key: str) -> bool:
        dest = self._resolve(key)
        if not dest.exists():
            return False
        if not dest.is_file() or _is_reparse_or_link(str(dest)):
            raise SyncSecurityError(f"Refusing to delete non-regular sync object: {key}")
        dest.unlink()
        return True

    def list_objects(self, prefix: str = "") -> list[str]:
        clean_pref = prefix.strip().strip("/")
        if clean_pref and clean_pref.split("/")[0] not in {"vault-header.json", "head.json", "manifests", "objects", "inbox-drops", "mobile-recall"}:
            raise SyncSecurityError(f"Disallowed sync list prefix: {prefix!r}")
        if not self.root.is_dir():
            return []
        _assert_no_symlink_chain(self.root)
        namespaces = [clean_pref.split("/")[0]] if clean_pref else ["manifests", "objects"]
        found: list[str] = []
        for ns in namespaces:
            target = self.root / ns
            if not target.exists() or _is_reparse_or_link(str(target)):
                continue
            if target.is_file():
                rel = target.relative_to(self.root).as_posix()
                if not clean_pref or rel == clean_pref or rel.startswith(clean_pref + "/"):
                    found.append(_validate_backend_key(rel))
                continue
            for dirpath, dirnames, filenames in os.walk(str(target), followlinks=False):
                dirnames[:] = [d for d in sorted(dirnames) if not _is_reparse_or_link(os.path.join(dirpath, d))]
                for fn in sorted(filenames):
                    full = os.path.join(dirpath, fn)
                    if _is_reparse_or_link(full) or not os.path.isfile(full):
                        continue
                    rel = Path(full).relative_to(self.root).as_posix()
                    if not clean_pref or rel == clean_pref or rel.startswith(clean_pref + "/"):
                        try:
                            found.append(_validate_backend_key(rel))
                        except SyncSecurityError:
                            continue
        return sorted(set(found))


class HttpObjectSyncBackend(SyncBackend):
    """DNS-pinned HTTPS object-store backend (AWS S3 / Cloudflare R2 / Backblaze B2 SigV4, Bearer, or HTTPS)."""

    backend_type = "http_object"

    def __init__(
        self,
        endpoint_url: str,
        *,
        bearer_token_env: str | None = None,
        auth_mode: str = "bearer",
        s3_region: str = "auto",
        s3_access_key_env: str | None = None,
        s3_secret_key_env: str | None = None,
        s3_session_token_env: str | None = None,
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

        norm_auth = (auth_mode or "bearer").strip().lower()
        if norm_auth not in {"bearer", "s3_sigv4", "none"}:
            raise SyncSecurityError(f"Unsupported HTTP sync auth_mode: {auth_mode!r}")
        self.auth_mode = norm_auth

        if bearer_token_env is not None and bearer_token_env.strip():
            if not _ENV_VAR_RE.fullmatch(bearer_token_env.strip()):
                raise SyncSecurityError("bearer_token_env must be an uppercase environment variable name.")
            self.bearer_token_env: str | None = bearer_token_env.strip()
        else:
            self.bearer_token_env = None

        norm_region = (s3_region or "auto").strip()
        if not _S3_REGION_RE.fullmatch(norm_region):
            raise SyncSecurityError(f"Invalid s3_region: {s3_region!r}")
        self.s3_region = norm_region

        def _clean_env_name(raw_name: str | None, default_name: str | None) -> str | None:
            cand = (raw_name or "").strip() or default_name
            if cand is None:
                return None
            if not _ENV_VAR_RE.fullmatch(cand):
                raise SyncSecurityError(f"Invalid credential environment variable name: {cand!r}")
            return cand

        if self.auth_mode == "s3_sigv4":
            self.s3_access_key_env = _clean_env_name(s3_access_key_env, "AWS_ACCESS_KEY_ID")
            self.s3_secret_key_env = _clean_env_name(s3_secret_key_env, "AWS_SECRET_ACCESS_KEY")
            self.s3_session_token_env = _clean_env_name(s3_session_token_env, None)
        else:
            self.s3_access_key_env = _clean_env_name(s3_access_key_env, None)
            self.s3_secret_key_env = _clean_env_name(s3_secret_key_env, None)
            self.s3_session_token_env = _clean_env_name(s3_session_token_env, None)

        transport_kind = "local" if (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}) else "online"
        if parsed.scheme == "http" and transport_kind != "local":
            raise SyncSecurityError("Remote HTTP sync endpoints MUST use https://.")

        rule = HostSharingRule(
            host_id="vault_sync_http",
            provider="openai_compatible",
            transport=transport_kind,
            endpoint_url=raw_ep,
            default_model="sync-object-store",
            api_key_env=self.bearer_token_env or self.s3_access_key_env,
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

    def _request_headers(
        self,
        method: str,
        url: str,
        payload: bytes = b"",
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        if self.auth_mode == "s3_sigv4":
            ak_env = self.s3_access_key_env or "AWS_ACCESS_KEY_ID"
            sk_env = self.s3_secret_key_env or "AWS_SECRET_ACCESS_KEY"
            access_key = (os.environ.get(ak_env) or "").strip()
            secret_key = (os.environ.get(sk_env) or "").strip()
            if not access_key or not secret_key:
                raise SyncSecurityError(
                    f"Required S3 SigV4 environment variables ('{ak_env}', '{sk_env}') are not set."
                )
            session_tok = (
                (os.environ.get(self.s3_session_token_env) or "").strip() or None
                if self.s3_session_token_env
                else None
            )
            return sign_s3_sigv4_headers(
                method,
                url,
                payload,
                access_key=access_key,
                secret_key=secret_key,
                region=self.s3_region,
                session_token=session_tok,
                extra_headers=extra_headers,
            )

        hdrs: dict[str, str] = {"User-Agent": "SecondBrain-Sync/1.0"}
        if self.auth_mode == "bearer" and self.bearer_token_env:
            tok = (os.environ.get(self.bearer_token_env) or "").strip()
            if not tok:
                raise SyncSecurityError(
                    f"Required sync auth environment variable '{self.bearer_token_env}' is not set."
                )
            hdrs["Authorization"] = f"Bearer {tok}"
        if extra_headers:
            hdrs.update(extra_headers)
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
                head_hdrs = self._request_headers("HEAD", url)
                head_resp = client.request("HEAD", url, headers=head_hdrs)
                if 300 <= head_resp.status_code < 400:
                    raise SyncSecurityError("HTTP redirects are forbidden on sync object endpoints.")
                if head_resp.status_code == 200:
                    return True
                if head_resp.status_code == 404:
                    return False
                if head_resp.status_code not in {405, 501}:
                    head_resp.raise_for_status()
                get_hdrs = self._request_headers("GET", url)
                with client.stream("GET", url, headers=get_hdrs) as get_resp:
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
                with client.stream("GET", url, headers=self._request_headers("GET", url)) as resp:
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
                hdrs = self._request_headers(
                    "PUT",
                    url,
                    data,
                    extra_headers={"Content-Type": "application/octet-stream"},
                )
                resp = client.put(url, headers=hdrs, content=data)
                if 300 <= resp.status_code < 400:
                    raise SyncSecurityError("HTTP redirects are forbidden on sync object endpoints.")
                resp.raise_for_status()
        finally:
            if pinned is not None:
                pinned.close()

    def delete_object(self, key: str) -> bool:
        url = self._url_for(key)
        client, pinned = self._open_client()
        try:
            with client:
                hdrs = self._request_headers("DELETE", url)
                resp = client.delete(url, headers=hdrs)
                if 300 <= resp.status_code < 400:
                    raise SyncSecurityError("HTTP redirects are forbidden on sync object endpoints.")
                if resp.status_code == 404:
                    return False
                resp.raise_for_status()
                return True
        finally:
            if pinned is not None:
                pinned.close()

    @staticmethod
    def _is_virtual_hosted_s3_hostname(hostname: str) -> bool:
        host = (hostname or "").strip().lower().rstrip(".")
        if not host:
            return False
        # Virtual-hosted S3: <bucket>.s3.<region>.amazonaws.com, <bucket>.s3-<region>.amazonaws.com, <bucket>.s3.amazonaws.com
        if ".s3." in host or ".s3-" in host or ".s3-accelerate" in host:
            return True
        # Virtual-hosted Backblaze B2: <bucket>.s3.<region>.backblazeb2.com (covered by .s3.)
        # Virtual-hosted DigitalOcean Spaces: <bucket>.<region>.digitaloceanspaces.com (4 labels)
        if host.endswith(".digitaloceanspaces.com") and host.count(".") >= 3:
            return True
        # Virtual-hosted Cloudflare R2 custom/r2.dev domain: <pub>.r2.dev
        if host.endswith(".r2.dev"):
            return True
        return False

    def list_objects(self, prefix: str = "") -> list[str]:
        clean_pref = prefix.strip().strip("/")
        if clean_pref and clean_pref.split("/")[0] not in {"vault-header.json", "head.json", "manifests", "objects", "inbox-drops", "mobile-recall"}:
            raise SyncSecurityError(f"Disallowed sync list prefix: {prefix!r}")
        parsed_ep = urlparse(self.endpoint_url)
        path_segments = [s for s in (parsed_ep.path or "").split("/") if s]
        is_vhost = self._is_virtual_hosted_s3_hostname(parsed_ep.hostname or "")

        if is_vhost:
            bucket_url = f"{parsed_ep.scheme}://{parsed_ep.netloc}"
            sub_prefix = "/".join(path_segments)
            if sub_prefix:
                full_prefix = f"{sub_prefix}/{clean_pref}/" if clean_pref in {"manifests", "objects", "inbox-drops"} else (f"{sub_prefix}/{clean_pref}" if clean_pref else f"{sub_prefix}/")
            else:
                full_prefix = f"{clean_pref}/" if clean_pref in {"manifests", "objects", "inbox-drops"} else clean_pref
        elif len(path_segments) >= 2:
            # Path-style S3 URLs like https://acct.r2.cloudflarestorage.com/my-bucket/subprefix
            bucket_url = f"{parsed_ep.scheme}://{parsed_ep.netloc}/{path_segments[0]}"
            sub_prefix = "/".join(path_segments[1:])
            full_prefix = f"{sub_prefix}/{clean_pref}/" if clean_pref in {"manifests", "objects", "inbox-drops"} else (f"{sub_prefix}/{clean_pref}" if clean_pref else f"{sub_prefix}/")
        else:
            bucket_url = self.endpoint_url
            sub_prefix = ""
            full_prefix = f"{clean_pref}/" if clean_pref in {"manifests", "objects", "inbox-drops"} else clean_pref

        collected: list[str] = []
        continuation_token: str | None = None
        client, pinned = self._open_client()
        try:
            with client:
                for _page in range(200):
                    q_items = [("list-type", "2")]
                    if full_prefix:
                        q_items.append(("prefix", full_prefix))
                    if continuation_token:
                        q_items.append(("continuation-token", continuation_token))
                    qs = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(q_items))
                    list_url = f"{bucket_url}?{qs}"
                    hdrs = self._request_headers("GET", list_url)
                    chunks: list[bytes] = []
                    total_bytes = 0
                    with client.stream("GET", list_url, headers=hdrs) as resp:
                        if 300 <= resp.status_code < 400:
                            raise SyncSecurityError("HTTP redirects are forbidden on sync object endpoints.")
                        resp.raise_for_status()
                        ctype = (resp.headers.get("Content-Type") or "").lower()
                        for chunk in resp.iter_bytes():
                            total_bytes += len(chunk)
                            if total_bytes > MAX_S3_LIST_XML_BYTES:
                                raise SyncSecurityError("Sync list_objects response exceeded maximum byte limit.")
                            chunks.append(chunk)
                    raw_body = b"".join(chunks)
                    if "json" in ctype:
                        doc = json.loads(raw_body.decode("utf-8"))
                        raw_items = doc.get("keys") or doc.get("objects") or []
                        for item in raw_items:
                            rk = str(item.get("key") if isinstance(item, dict) else item).lstrip("/")
                            if sub_prefix:
                                if not rk.startswith(sub_prefix + "/"):
                                    continue
                                rk = rk[len(sub_prefix) + 1 :]
                            try:
                                collected.append(_validate_backend_key(rk))
                            except SyncSecurityError:
                                continue
                        continuation_token = None
                    else:
                        page_keys, continuation_token = _parse_s3_list_objects_xml(
                            raw_body,
                            key_strip_prefix=sub_prefix,
                        )
                        collected.extend(page_keys)
                    if not continuation_token:
                        break
        finally:
            if pinned is not None:
                pinned.close()
        return sorted(set(collected))


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
            auth_mode=str(cfg.get("auth_mode") or "bearer"),
            s3_region=str(cfg.get("s3_region") or "auto"),
            s3_access_key_env=cfg.get("s3_access_key_env"),
            s3_secret_key_env=cfg.get("s3_secret_key_env"),
            s3_session_token_env=cfg.get("s3_session_token_env"),
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


def _seal_key_into_keychain(
    vault_id: str,
    device_id: str,
    master_key: bytes,
    *,
    state_dir: Path | None = None,
) -> dict[str, str]:
    account = f"vault-{vault_id}-{device_id}"
    try:
        return store_secret(_KEYCHAIN_SERVICE, account, master_key, state_dir=state_dir)
    except KeychainError as exc:
        raise SyncSecurityError(f"Failed to seal master key into OS Keychain / DPAPI: {exc}") from exc


def _resolve_master_key(
    cfg: dict[str, Any],
    *,
    passphrase: str | None = None,
    state_dir: Path | None = None,
) -> bytes:
    kdf_mode = str(cfg.get("kdf_mode") or "keyfile")
    kc_account = str(cfg.get("keychain_account") or "").strip()
    kc_service = str(cfg.get("keychain_service") or _KEYCHAIN_SERVICE).strip()
    kc_backend = str(cfg.get("keychain_backend") or "").strip() or None

    if kdf_mode == "os_keychain":
        if not kc_account:
            raise SyncSecurityError("Sync config missing keychain_account for os_keychain mode.")
        try:
            key = load_secret(kc_service, kc_account, state_dir=state_dir, backend=kc_backend)
        except KeychainError as exc:
            raise SyncSecurityError(f"OS Keychain / DPAPI unseal failed: {exc}") from exc
        if key is None or len(key) != KEY_BYTES:
            raise SyncSecurityError("Master key missing or corrupted in OS Keychain / DPAPI store.")
        return key

    if kdf_mode == "passphrase":
        if passphrase:
            salt = base64.b64decode(str(cfg["kdf_salt_b64"]))
            return derive_master_key_from_passphrase(passphrase, salt)
        if kc_account:
            try:
                key = load_secret(kc_service, kc_account, state_dir=state_dir, backend=kc_backend)
            except KeychainError as exc:
                raise SyncSecurityError(f"OS Keychain / DPAPI unseal failed: {exc}") from exc
            if key is not None and len(key) == KEY_BYTES:
                return key
        raise SyncSecurityError("Passphrase is required to unlock this E2EE sync vault.")

    if kdf_mode == "keyfile":
        if kc_account and not cfg.get("master_key_b64"):
            try:
                key = load_secret(kc_service, kc_account, state_dir=state_dir, backend=kc_backend)
            except KeychainError as exc:
                raise SyncSecurityError(f"OS Keychain / DPAPI unseal failed: {exc}") from exc
            if key is None or len(key) != KEY_BYTES:
                raise SyncSecurityError("Master key missing or corrupted in OS Keychain / DPAPI store.")
            return key
        key_b64 = str(cfg.get("master_key_b64") or "")
        if not key_b64:
            raise SyncSecurityError("Sync config missing master_key_b64.")
        key = base64.b64decode(key_b64)
        if len(key) != KEY_BYTES:
            raise SyncSecurityError("Invalid master key length.")
        return key

    raise SyncSecurityError(f"Unsupported sync kdf_mode: {kdf_mode!r}")


def _sync_base_cache_dir(active_brain: Path) -> Path:
    return active_brain / ".sidecar" / "sync-base"


def _cache_sync_base_blob(active_brain: Path, sha256_hex: str, raw_bytes: bytes) -> None:
    if not _HEX_ID_RE.fullmatch(sha256_hex) or len(raw_bytes) > MAX_SYNC_BASE_CACHE_BYTES:
        return
    base_dir = _sync_base_cache_dir(active_brain)
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
        if _is_reparse_or_link(str(base_dir)):
            return
        dest = base_dir / f"{sha256_hex}.blob"
        if not dest.exists():
            atomic_write_bytes(str(dest), raw_bytes)
    except OSError:
        pass


def _load_sync_base_text(active_brain: Path, sha256_hex: str | None) -> str | None:
    if not sha256_hex or not _HEX_ID_RE.fullmatch(str(sha256_hex)):
        return None
    dest = _sync_base_cache_dir(active_brain) / f"{sha256_hex}.blob"
    if not dest.is_file() or _is_reparse_or_link(str(dest)):
        return None
    try:
        raw = dest.read_bytes()
        if content_sha256(raw) != sha256_hex:
            return None
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return None


def _prune_sync_base_cache(active_brain: Path, keep_shas: set[str]) -> None:
    base_dir = _sync_base_cache_dir(active_brain)
    if not base_dir.is_dir() or _is_reparse_or_link(str(base_dir)):
        return
    try:
        for fn in os.listdir(base_dir):
            if not fn.endswith(".blob"):
                continue
            stem = fn[:-5]
            if stem not in keep_shas:
                fp = base_dir / fn
                if fp.is_file() and not _is_reparse_or_link(str(fp)):
                    fp.unlink()
    except OSError:
        pass


def _sync_dense_vectors_incremental(
    active_brain: Path,
    changed_atoms: list[str],
    deleted_atoms: list[str],
) -> bool:
    atoms_dir = active_brain / "2-atoms"
    for fname in (*changed_atoms, *deleted_atoms):
        invalidate_atom_revision_cache(str(atoms_dir / fname))
    if not changed_atoms and not deleted_atoms:
        return False
    chroma_dir = active_brain / ".chroma"
    try:
        manifest = load_index_manifest(str(chroma_dir))
        if not manifest or manifest.get("distance_metric") != "cosine":
            return False
        import rebuild_vectors as _rv_mod

        inc_fn = getattr(_rv_mod, "upsert_changed_vectors", None)
        if callable(inc_fn):
            res = inc_fn(
                atoms_dir=str(atoms_dir),
                store_path=str(chroma_dir),
                model=str(manifest.get("embedding_model") or "").strip() or None,
                changed_filenames=changed_atoms,
                deleted_filenames=deleted_atoms,
                _writer_lock_held=True,
            )
            return bool(res.get("committed", True))
    except Exception:
        return False
    return False


def _conflict_entry_id(entry: dict[str, Any]) -> str:
    existing = str(entry.get("conflict_id") or "").strip()
    if existing:
        return existing
    seed = f"{entry.get('rel_path', '')}:{entry.get('remote_epoch', 0)}:{entry.get('remote_sha256', '')}:{entry.get('local_sha256', '')}"
    return "conf_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


# ─── Public High-Level Sync API ──────────────────────────────────────────────

def init_sync(
    target: str,
    *,
    backend_type: str = "local_folder",
    brain_dir: str | Path | None = None,
    device_name: str | None = None,
    passphrase: str | None = None,
    kdf_mode: str | None = None,
    use_keychain: bool = False,
    bearer_token_env: str | None = None,
    auth_mode: str = "bearer",
    s3_region: str = "auto",
    s3_access_key_env: str | None = None,
    s3_secret_key_env: str | None = None,
    s3_session_token_env: str | None = None,
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

    req_kdf = (kdf_mode or "").strip().lower() or None
    if req_kdf not in {None, "keyfile", "passphrase", "os_keychain"}:
        raise ValueError(f"Unsupported kdf_mode: {kdf_mode!r}")
    seal_in_kc = bool(use_keychain or req_kdf == "os_keychain")

    kc_meta: dict[str, str] | None = None
    if passphrase is not None:
        salt = os.urandom(32)
        master_key = derive_master_key_from_passphrase(passphrase, salt)
        local_kdf_mode = "passphrase"
        header_kdf_mode = "passphrase"
        salt_b64 = base64.b64encode(salt).decode("ascii")
        stored_key_b64 = None
        recovery_key_b64 = None
        if seal_in_kc:
            kc_meta = _seal_key_into_keychain(vault_id, device_id, master_key, state_dir=state_dir)
    else:
        if req_kdf == "passphrase":
            raise ValueError("Passphrase must be supplied when kdf_mode='passphrase'.")
        master_key = os.urandom(KEY_BYTES)
        header_kdf_mode = "keyfile"
        salt_b64 = None
        recovery_key_b64 = base64.b64encode(master_key).decode("ascii")
        if seal_in_kc:
            local_kdf_mode = "os_keychain"
            stored_key_b64 = None
            kc_meta = _seal_key_into_keychain(vault_id, device_id, master_key, state_dir=state_dir)
        else:
            local_kdf_mode = "keyfile"
            stored_key_b64 = recovery_key_b64

    norm_auth = (auth_mode or "bearer").strip().lower()
    cfg: dict[str, Any] = {
        "schema_version": SYNC_SCHEMA_VERSION,
        "vault_id": vault_id,
        "device_id": device_id,
        "device_privkey_b64": priv_b64,
        "device_pubkey_b64": pub_b64,
        "backend_type": backend_type,
        "target": str(Path(os.path.abspath(os.path.expanduser(target))) if backend_type == "local_folder" else target.strip().rstrip("/")),
        "auth_mode": norm_auth,
        "bearer_token_env": bearer_token_env,
        "s3_region": (s3_region or "auto").strip(),
        "s3_access_key_env": s3_access_key_env,
        "s3_secret_key_env": s3_secret_key_env,
        "s3_session_token_env": s3_session_token_env,
        "kdf_mode": local_kdf_mode,
        "kdf_salt_b64": salt_b64,
        "master_key_b64": stored_key_b64,
        "keychain_service": kc_meta["service"] if kc_meta else None,
        "keychain_account": kc_meta["account"] if kc_meta else None,
        "keychain_backend": kc_meta["backend"] if kc_meta else None,
        "trusted_peers": {device_id: pub_b64},
        "auto_sync_enabled": False,
        "auto_sync_interval_seconds": 300,
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
        "kdf_mode": header_kdf_mode,
        "kdf_salt_b64": salt_b64,
        "scrypt_params": {"n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P} if header_kdf_mode == "passphrase" else None,
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
        "auth_mode": norm_auth,
        "target": cfg["target"],
        "kdf_mode": local_kdf_mode,
        "keychain_sealed": bool(kc_meta),
        "keychain_backend": kc_meta["backend"] if kc_meta else None,
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
    kdf_mode: str | None = None,
    use_keychain: bool = False,
    bearer_token_env: str | None = None,
    auth_mode: str = "bearer",
    s3_region: str = "auto",
    s3_access_key_env: str | None = None,
    s3_secret_key_env: str | None = None,
    s3_session_token_env: str | None = None,
    state_dir: Path | None = None,
    http_transport: httpx.BaseTransport | None = None,
    skip_dns_check_for_mock_transport: bool = False,
) -> dict[str, Any]:
    """Pair a device to an existing E2EE sync target, verifying master key ownership first."""
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    norm_target = str(Path(os.path.abspath(os.path.expanduser(target))) if backend_type == "local_folder" else target.strip().rstrip("/"))
    norm_auth = (auth_mode or "bearer").strip().lower()
    probe_cfg = {
        "backend_type": backend_type,
        "target": norm_target,
        "auth_mode": norm_auth,
        "bearer_token_env": bearer_token_env,
        "s3_region": (s3_region or "auto").strip(),
        "s3_access_key_env": s3_access_key_env,
        "s3_secret_key_env": s3_secret_key_env,
        "s3_session_token_env": s3_session_token_env,
    }
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
    remote_kdf_mode = str(hdr.get("kdf_mode") or "")
    salt_b64 = hdr.get("kdf_salt_b64")
    if remote_kdf_mode == "passphrase":
        if not passphrase or not salt_b64:
            raise SyncSecurityError("Passphrase is required to join passphrase-encrypted sync target.")
        master_key = derive_master_key_from_passphrase(passphrase, base64.b64decode(str(salt_b64)))
    elif remote_kdf_mode in {"keyfile", "os_keychain"}:
        if not recovery_key_b64:
            raise SyncSecurityError("recovery_key_b64 is required to join keyfile-encrypted sync target.")
        master_key = base64.b64decode(recovery_key_b64.strip())
        if len(master_key) != KEY_BYTES:
            raise SyncSecurityError("Invalid recovery key length (expected 32 bytes).")
    else:
        raise SyncSecurityError(f"Unsupported remote kdf_mode: {remote_kdf_mode!r}")

    # Verify master key decrypts the remote key_check envelope cleanly!
    kc_env = base64.b64decode(str(hdr.get("key_check_b64") or ""))
    kc_plain = decrypt_object(master_key, vault_id=vault_id, kind="header", object_id="key-check", envelope=kc_env)
    if kc_plain != f"second-brain-e2ee-v1:{vault_id}".encode("utf-8"):
        raise SyncSecurityError("Master key verification failed against remote vault-header.json.")

    host_slug = re.sub(r"[^a-zA-Z0-9_-]+", "", (device_name or socket.gethostname() or "device"))[:20] or "device"
    device_id = f"{host_slug}-{secrets.token_hex(4)}"
    priv_b64, pub_b64 = _generate_device_keypair()
    trusted_peers: dict[str, str] = {device_id: pub_b64}

    req_kdf = (kdf_mode or "").strip().lower() or None
    seal_in_kc = bool(use_keychain or req_kdf == "os_keychain")
    kc_meta: dict[str, str] | None = None
    if remote_kdf_mode == "passphrase":
        local_kdf_mode = "passphrase"
        stored_key_b64 = None
        if seal_in_kc:
            kc_meta = _seal_key_into_keychain(vault_id, device_id, master_key, state_dir=state_dir)
    else:
        if seal_in_kc:
            local_kdf_mode = "os_keychain"
            stored_key_b64 = None
            kc_meta = _seal_key_into_keychain(vault_id, device_id, master_key, state_dir=state_dir)
        else:
            local_kdf_mode = "keyfile"
            stored_key_b64 = base64.b64encode(master_key).decode("ascii")

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
        "auth_mode": norm_auth,
        "bearer_token_env": bearer_token_env,
        "s3_region": (s3_region or "auto").strip(),
        "s3_access_key_env": s3_access_key_env,
        "s3_secret_key_env": s3_secret_key_env,
        "s3_session_token_env": s3_session_token_env,
        "kdf_mode": local_kdf_mode,
        "kdf_salt_b64": salt_b64,
        "master_key_b64": stored_key_b64,
        "keychain_service": kc_meta["service"] if kc_meta else None,
        "keychain_account": kc_meta["account"] if kc_meta else None,
        "keychain_backend": kc_meta["backend"] if kc_meta else None,
        "trusted_peers": trusted_peers,
        "auto_sync_enabled": False,
        "auto_sync_interval_seconds": 300,
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
        "auth_mode": norm_auth,
        "target": norm_target,
        "kdf_mode": local_kdf_mode,
        "keychain_sealed": bool(kc_meta),
        "keychain_backend": kc_meta["backend"] if kc_meta else None,
        "trusted_peer_count": len(trusted_peers),
    }


def migrate_sync_key_to_keychain(
    *,
    brain_dir: str | Path | None = None,
    passphrase: str | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Seal an existing vault's master key into the OS Keychain / DPAPI and scrub plaintext JSON key material."""
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    cfg_path = sync_config_path(active_brain, state_dir=state_dir)
    cfg = _load_sync_config(active_brain, state_dir=state_dir)
    master_key = _resolve_master_key(cfg, passphrase=passphrase, state_dir=state_dir)
    kc_meta = _seal_key_into_keychain(
        str(cfg["vault_id"]),
        str(cfg["device_id"]),
        master_key,
        state_dir=state_dir,
    )
    if str(cfg.get("kdf_mode")) == "keyfile":
        cfg["kdf_mode"] = "os_keychain"
    cfg["master_key_b64"] = None
    cfg["keychain_service"] = kc_meta["service"]
    cfg["keychain_account"] = kc_meta["account"]
    cfg["keychain_backend"] = kc_meta["backend"]
    _atomic_write_restricted_json(cfg_path, cfg)
    return {
        "status": "migrated",
        "vault_id": cfg["vault_id"],
        "kdf_mode": cfg["kdf_mode"],
        "keychain_sealed": kc_meta["backend"] != "posix_restricted_keyfile",
        "keychain_backend": kc_meta["backend"],
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
            "keychain_available_backend": keychain_backend_name(state_dir=state_dir),
            "auto_sync": get_auto_sync_status(brain_dir=active_brain, state_dir=state_dir),
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

    conflicts = [{**c, "conflict_id": _conflict_entry_id(c)} for c in (local_state.get("conflicts") or [])]
    return {
        "configured": True,
        "vault_id": cfg["vault_id"],
        "device_id": cfg["device_id"],
        "backend_type": cfg["backend_type"],
        "auth_mode": str(cfg.get("auth_mode") or "bearer"),
        "s3_region": cfg.get("s3_region"),
        "target": cfg["target"],
        "kdf_mode": cfg["kdf_mode"],
        "keychain_sealed": bool(cfg.get("keychain_account")),
        "keychain_backend": cfg.get("keychain_backend") or keychain_backend_name(state_dir=state_dir),
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
        "auto_sync": get_auto_sync_status(brain_dir=active_brain, state_dir=state_dir),
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
    cfg = _load_sync_config(active_brain, state_dir=state_dir)
    master_key = _resolve_master_key(cfg, passphrase=passphrase, state_dir=state_dir)
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
            raw_bytes: bytes | None = None
            blob_id = compute_blind_blob_id(master_key, vault_id, plain_sha)
            obj_key = f"objects/{blob_id[:2]}/{blob_id}.enc"
            if backend.exists(obj_key):
                reused_blobs += 1
                if int(meta["size"]) <= MAX_SYNC_BASE_CACHE_BYTES:
                    try:
                        raw_bytes = (active_brain / rel_path).read_bytes()
                    except OSError:
                        raw_bytes = None
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
            if raw_bytes is not None:
                _cache_sync_base_blob(active_brain, plain_sha, raw_bytes)
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
        keep_shas = set(local_state["files"].values()) | {
            str(c.get("base_sha256")) for c in (local_state.get("conflicts") or []) if c.get("base_sha256")
        }
        _prune_sync_base_cache(active_brain, keep_shas)

        mobile_recall_exported = False
        try:
            from inbox_drop import build_mobile_recall_catalog, encrypt_mobile_recall_envelope, list_mobile_drop_pairings, _load_drop_state

            cand_drop_dirs = [state_dir, sync_key_dir(state_dir)] if state_dir is not None else [None]
            for d_dir in cand_drop_dirs:
                drop_info = list_mobile_drop_pairings(vault_id=vault_id, brain_dir=active_brain, state_dir=d_dir)
                if int(drop_info.get("recall_enabled_count") or 0) > 0:
                    d_state = _load_drop_state(active_brain, state_dir=d_dir)
                    if isinstance(d_state, dict) and d_state.get("recall_key_b64") and d_state.get("recall_key_id"):
                        cat = build_mobile_recall_catalog(brain_dir=active_brain, include_pending_review=True)
                        env = encrypt_mobile_recall_envelope(
                            cat,
                            vault_id=vault_id,
                            recall_key_b64=str(d_state["recall_key_b64"]),
                            recall_key_id=str(d_state["recall_key_id"]),
                        )
                        backend.put_object(f"mobile-recall/{vault_id}.enc", env)
                        mobile_recall_exported = True
                        break
        except Exception:
            mobile_recall_exported = False

        return {
            "status": "pushed",
            "vault_id": vault_id,
            "device_id": cfg["device_id"],
            "epoch": new_epoch,
            "manifest_id": manifest_id,
            "total_files": len(manifest_files),
            "uploaded_blobs": uploaded_blobs,
            "reused_blobs": reused_blobs,
            "mobile_recall_exported": mobile_recall_exported,
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
    master_key = _resolve_master_key(cfg, passphrase=passphrase, state_dir=state_dir)
    backend = _backend_from_config(
        cfg,
        http_transport=http_transport,
        skip_dns_check_for_mock_transport=skip_dns_check_for_mock_transport,
    )
    lock_path = active_brain / ".sidecar" / "writer.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    changed_atoms: list[str] = []
    deleted_atoms: list[str] = []

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
            from inbox_drop import pull_inbox_drops_from_backend

            drops_res = pull_inbox_drops_from_backend(
                backend,
                brain_dir=active_brain,
                state_dir=state_dir,
                _writer_lock_held=True,
            )
            return {
                "status": "empty_remote",
                "epoch": int(local_state.get("last_seen_epoch", 0)),
                "added": 0,
                "updated": 0,
                "deleted": 0,
                "merged_jsonl": 0,
                "conflicts": [],
                "inbox_drops_ingested": int(drops_res.get("ingested_count", 0)),
                "inbox_drops": drops_res,
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
                if int(local_meta.get("size", 0)) <= MAX_SYNC_BASE_CACHE_BYTES:
                    try:
                        _cache_sync_base_blob(active_brain, remote_sha, (active_brain / clean_rel).read_bytes())
                    except OSError:
                        pass
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

        def _is_top_atom_rel(rp: str) -> str | None:
            if rp.startswith("2-atoms/") and rp.count("/") == 1 and rp.endswith(".md"):
                return rp.split("/", 1)[1]
            return None

        for clean_rel, (remote_bytes, remote_sha) in sorted(decrypted_incoming.items()):
            dest = active_brain.joinpath(*clean_rel.split("/"))
            _assert_no_symlink_chain(dest, stop_at=active_brain)
            local_meta = local_inventory.get(clean_rel)
            base_sha = base_files.get(clean_rel)
            atom_fname = _is_top_atom_rel(clean_rel)

            if local_meta is None:
                # New remote file
                dest.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(str(dest), remote_bytes)
                _cache_sync_base_blob(active_brain, remote_sha, remote_bytes)
                new_base_files[clean_rel] = remote_sha
                added += 1
                if atom_fname:
                    changed_atoms.append(atom_fname)
                if clean_rel == "0-raw/archive/decision-record.jsonl":
                    decision_log_touched = True
                continue

            local_sha = str(local_meta["sha256"])
            if base_sha == local_sha:
                # Clean fast-forward: local unchanged since last sync
                _backup_before_sync_overwrite(active_brain, clean_rel, dest.read_bytes())
                atomic_write_bytes(str(dest), remote_bytes)
                _cache_sync_base_blob(active_brain, remote_sha, remote_bytes)
                new_base_files[clean_rel] = remote_sha
                updated += 1
                if atom_fname:
                    changed_atoms.append(atom_fname)
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
                merged_sha = content_sha256(merged)
                _cache_sync_base_blob(active_brain, merged_sha, merged)
                new_base_files[clean_rel] = merged_sha
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
                c_entry: dict[str, Any] = {
                    "rel_path": clean_rel,
                    "conflict_copy": conflict_rel,
                    "conflict_kind": "divergent_edit",
                    "base_sha256": base_sha,
                    "local_sha256": local_sha,
                    "remote_sha256": remote_sha,
                    "remote_device_id": str(remote_head["device_id"]),
                    "remote_epoch": int(remote_head["epoch"]),
                    "recorded_at": _utc_now_iso(),
                }
                c_entry["conflict_id"] = _conflict_entry_id(c_entry)
                new_conflicts.append(c_entry)

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
                atom_fname = _is_top_atom_rel(clean_rel)
                if atom_fname:
                    deleted_atoms.append(atom_fname)
            else:
                # Modify/delete divergence: preserve local modified file on disk & record conflict!
                new_base_files.pop(clean_rel, None)
                md_entry: dict[str, Any] = {
                    "rel_path": clean_rel,
                    "conflict_copy": None,
                    "conflict_kind": "modify_delete",
                    "base_sha256": base_sha,
                    "local_sha256": local_sha,
                    "remote_sha256": None,
                    "remote_device_id": str(remote_head["device_id"]),
                    "remote_epoch": int(remote_head["epoch"]),
                    "recorded_at": _utc_now_iso(),
                }
                md_entry["conflict_id"] = _conflict_entry_id(md_entry)
                new_conflicts.append(md_entry)

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

        keep_shas = set(new_base_files.values()) | {
            str(c.get("base_sha256")) for c in existing_conflicts if c.get("base_sha256")
        }
        _prune_sync_base_cache(active_brain, keep_shas)

        if decision_log_touched:
            invalidate_decision_log_cache()

        from inbox_drop import pull_inbox_drops_from_backend

        drops_res = pull_inbox_drops_from_backend(
            backend,
            brain_dir=active_brain,
            state_dir=state_dir,
            _writer_lock_held=True,
        )

        indexes_rebuilt = False
        vectors_updated = False
        if added > 0 or updated > 0 or deleted > 0 or merged_jsonl > 0:
            atoms_dir = active_brain / "2-atoms"
            lex_path = active_brain / ".sidecar" / "lexical.sqlite3"
            atoms_dir.mkdir(parents=True, exist_ok=True)
            rebuild_lexical_index(str(atoms_dir), str(lex_path))
            indexes_rebuilt = True
            if changed_atoms or deleted_atoms:
                vectors_updated = _sync_dense_vectors_incremental(active_brain, changed_atoms, deleted_atoms)

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
        "vectors_updated": vectors_updated,
        "inbox_drops_ingested": int(drops_res.get("ingested_count", 0)),
        "inbox_drops": drops_res,
    }


def pull_mobile_inbox_drops(
    *,
    brain_dir: str | Path | None = None,
    state_dir: Path | None = None,
    http_transport: httpx.BaseTransport | None = None,
    skip_dns_check_for_mock_transport: bool = False,
) -> dict[str, Any]:
    """Standalone pull of E2EE mobile `inbox-drops/*.enc` into quarantined `0-raw/inbox/`."""
    from inbox_drop import pull_inbox_drops_from_backend

    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    cfg = _load_sync_config(active_brain, state_dir=state_dir)
    backend = _backend_from_config(
        cfg,
        http_transport=http_transport,
        skip_dns_check_for_mock_transport=skip_dns_check_for_mock_transport,
    )
    return pull_inbox_drops_from_backend(
        backend,
        brain_dir=active_brain,
        state_dir=state_dir,
        _writer_lock_held=False,
    )


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
    master_key = _resolve_master_key(cfg, passphrase=passphrase, state_dir=state_dir)
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


def sync_gc(
    *,
    brain_dir: str | Path | None = None,
    passphrase: str | None = None,
    dry_run: bool = False,
    keep_parent_manifest: bool = True,
    state_dir: Path | None = None,
    http_transport: httpx.BaseTransport | None = None,
    skip_dns_check_for_mock_transport: bool = False,
) -> dict[str, Any]:
    """Garbage-collect unreferenced encrypted blobs and superseded manifests after verifying signed head."""
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    cfg = _load_sync_config(active_brain, state_dir=state_dir)
    master_key = _resolve_master_key(cfg, passphrase=passphrase, state_dir=state_dir)
    backend = _backend_from_config(
        cfg,
        http_transport=http_transport,
        skip_dns_check_for_mock_transport=skip_dns_check_for_mock_transport,
    )
    vault_id = str(cfg["vault_id"])
    lock_path = active_brain / ".sidecar" / "writer.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with review_lock(str(lock_path)):
        local_state = _load_local_sync_state(active_brain)
        head, _ = _verify_and_read_remote_head(
            backend,
            cfg,
            master_key=master_key,
            local_state=local_state,
            trust_new_peers=True,
        )
        if head is None:
            return {
                "status": "empty_remote",
                "dry_run": bool(dry_run),
                "kept_manifests": 0,
                "kept_blobs": 0,
                "deleted_manifests": 0,
                "deleted_blobs": 0,
                "deleted_keys": [],
            }

        head_mid = str(head["manifest_id"])
        if not _HEX_ID_RE.fullmatch(head_mid):
            raise SyncSecurityError(f"Malformed manifest_id in remote head: {head_mid!r}")
        head_mct = backend.get_object(f"manifests/{head_mid}.enc", max_bytes=MAX_SYNC_MANIFEST_BYTES)
        if head_mct is None or content_sha256(head_mct) != str(head["manifest_ciphertext_sha256"]):
            raise SyncSecurityError("Active remote manifest missing or failed hash check; aborting GC fail-closed.")
        head_mplain = decrypt_object(master_key, vault_id=vault_id, kind="manifest", object_id=head_mid, envelope=head_mct)
        head_mdoc = json.loads(head_mplain.decode("utf-8"))
        if (
            not isinstance(head_mdoc, dict)
            or str(head_mdoc.get("vault_id") or "") != vault_id
            or int(head_mdoc.get("epoch", -1)) != int(head["epoch"])
            or not isinstance(head_mdoc.get("files"), dict)
        ):
            raise SyncSecurityError("Decrypted active manifest failed vault_id/epoch/files validation during GC.")

        kept_manifest_keys: set[str] = {f"manifests/{head_mid}.enc"}
        kept_blob_keys: set[str] = set()
        for entry in head_mdoc["files"].values():
            bid = str(entry.get("blob_id") or "") if isinstance(entry, dict) else ""
            if _HEX_ID_RE.fullmatch(bid):
                kept_blob_keys.add(f"objects/{bid[:2]}/{bid}.enc")

        parent_mid = str(head.get("parent_manifest_id") or "").strip()
        if keep_parent_manifest and parent_mid and _HEX_ID_RE.fullmatch(parent_mid):
            pm_key = f"manifests/{parent_mid}.enc"
            pm_ct = backend.get_object(pm_key, max_bytes=MAX_SYNC_MANIFEST_BYTES)
            if pm_ct is not None:
                try:
                    pm_plain = decrypt_object(master_key, vault_id=vault_id, kind="manifest", object_id=parent_mid, envelope=pm_ct)
                    pm_doc = json.loads(pm_plain.decode("utf-8"))
                    if (
                        isinstance(pm_doc, dict)
                        and str(pm_doc.get("vault_id") or "") == vault_id
                        and isinstance(pm_doc.get("files"), dict)
                    ):
                        kept_manifest_keys.add(pm_key)
                        for entry in pm_doc["files"].values():
                            bid = str(entry.get("blob_id") or "") if isinstance(entry, dict) else ""
                            if _HEX_ID_RE.fullmatch(bid):
                                kept_blob_keys.add(f"objects/{bid[:2]}/{bid}.enc")
                except Exception:
                    pass

        remote_manifests = backend.list_objects("manifests")
        remote_objects = backend.list_objects("objects")
        orphan_manifests = sorted(k for k in remote_manifests if k not in kept_manifest_keys)
        orphan_blobs = sorted(k for k in remote_objects if k not in kept_blob_keys)
        deleted_keys: list[str] = []

        if not dry_run:
            for k in (*orphan_blobs, *orphan_manifests):
                if backend.delete_object(k):
                    deleted_keys.append(k)
        else:
            deleted_keys = [*orphan_blobs, *orphan_manifests]

        return {
            "status": "dry_run" if dry_run else "pruned",
            "dry_run": bool(dry_run),
            "epoch": int(head["epoch"]),
            "kept_manifests": len(kept_manifest_keys),
            "kept_blobs": len(kept_blob_keys),
            "deleted_manifests": len(orphan_manifests),
            "deleted_blobs": len(orphan_blobs),
            "deleted_keys": deleted_keys,
        }


# ─── Interactive 3-Way Conflict Inspection & Resolution ──────────────────────

def list_sync_conflicts(*, brain_dir: str | Path | None = None) -> list[dict[str, Any]]:
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    state = _load_local_sync_state(active_brain)
    return [{**c, "conflict_id": _conflict_entry_id(c)} for c in (state.get("conflicts") or [])]


def _resolve_conflict_quarantine_path(active_brain: Path, conflict_copy: str | None) -> Path | None:
    if not conflict_copy:
        return None
    clean = conflict_copy.strip().replace("\\", "/")
    if not clean.startswith("0-raw/sync-conflicts/") or ".." in clean.split("/"):
        raise SyncSecurityError(f"Invalid conflict quarantine path: {conflict_copy!r}")
    dest = active_brain.joinpath(*clean.split("/"))
    _assert_no_symlink_chain(dest, stop_at=active_brain)
    return dest


def get_sync_conflict_detail(
    conflict_id_or_rel_path: str,
    *,
    brain_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return local, remote, and 3-way base text plus unified diff for an unresolved sync conflict."""
    needle = (conflict_id_or_rel_path or "").strip()
    if not needle:
        raise ValueError("conflict_id or rel_path is required.")
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    conflicts = list_sync_conflicts(brain_dir=active_brain)
    matched = next(
        (c for c in conflicts if c["conflict_id"] == needle or c.get("rel_path") == needle),
        None,
    )
    if matched is None:
        raise FileNotFoundError(f"Sync conflict not found: {needle}")

    clean_rel = validate_sync_rel_path(str(matched["rel_path"]))
    local_file = active_brain.joinpath(*clean_rel.split("/"))
    _assert_no_symlink_chain(local_file, stop_at=active_brain)

    local_content = ""
    current_local_sha256: str | None = None
    if local_file.is_file():
        lbytes = local_file.read_bytes()
        current_local_sha256 = content_sha256(lbytes)
        local_content = lbytes.decode("utf-8", errors="replace")

    remote_content: str | None = None
    q_path = _resolve_conflict_quarantine_path(active_brain, matched.get("conflict_copy"))
    if q_path is not None and q_path.is_file():
        rbytes = q_path.read_bytes()
        remote_content = rbytes.decode("utf-8", errors="replace")

    base_content = _load_sync_base_text(active_brain, matched.get("base_sha256"))
    diff_lines = list(
        difflib.unified_diff(
            local_content.splitlines(),
            (remote_content or "").splitlines(),
            fromfile=f"local/{clean_rel}",
            tofile=f"remote/{clean_rel}" if remote_content is not None else "(deleted on remote)",
            lineterm="",
        )
    )
    return {
        **matched,
        "rel_path": clean_rel,
        "current_local_sha256": current_local_sha256,
        "base_content": base_content,
        "local_content": local_content,
        "remote_content": remote_content,
        "unified_diff": "\n".join(diff_lines),
        "is_canonical_atom": clean_rel.startswith("2-atoms/") and clean_rel.count("/") == 1 and clean_rel.endswith(".md"),
    }


def _demote_unapproved_canonical_merge(
    active_brain: Path,
    clean_rel: str,
    candidate_bytes: bytes,
    *,
    force_demote: bool = False,
) -> tuple[bytes, bool]:
    """Ensure manual_merge or peer-duplicate canonical files never claim accepted status without owner approval."""
    if clean_rel.startswith("2-atoms/") and clean_rel.count("/") == 1 and clean_rel.endswith(".md"):
        atoms_root = str(active_brain / "2-atoms")
        dest_path = str(active_brain / clean_rel)
        if not force_demote and approved_revision_is_current(
            dest_path,
            brain_dir=str(active_brain),
            atoms_dir=atoms_root,
            known_atom_bytes=candidate_bytes,
            known_atom_sha256=content_sha256(candidate_bytes),
            canonical_atoms_root=atoms_root,
        ):
            return candidate_bytes, False
        text = candidate_bytes.decode("utf-8", errors="replace")
        parsed = split_frontmatter(text)
        if parsed is not None:
            fm_lines, body_lines = parsed
            updated_fm = set_scalar_field(fm_lines, "claim_state", "candidate", "citation_integrity")
            updated_fm = set_scalar_field(updated_fm, "review_status", "pending", "epistemic_label")
            return render_memory_document(updated_fm, body_lines).encode("utf-8"), True
        normalized = normalize_memory_document(text, source_filename=Path(clean_rel).name, origin="user_local")
        return normalized.encode("utf-8"), True
    if clean_rel.startswith("5-playbooks/") and clean_rel.count("/") == 1 and clean_rel.endswith(".md"):
        if not force_demote:
            try:
                from skill_playbooks import _latest_playbook_decision

                pb_fname = Path(clean_rel).name
                log_ok, latest_pb_dec = _latest_playbook_decision(str(active_brain), pb_fname)
                if (
                    log_ok
                    and isinstance(latest_pb_dec, dict)
                    and latest_pb_dec.get("action") == "promote_playbook"
                    and str(latest_pb_dec.get("new_sha256") or "") == content_sha256(candidate_bytes)
                ):
                    return candidate_bytes, False
            except Exception:
                pass
        text = candidate_bytes.decode("utf-8", errors="replace")
        parsed = split_frontmatter(text)
        if parsed is not None:
            fm_lines, body_lines = parsed
            updated_fm = set_scalar_field(fm_lines, "playbook_status", "draft", "type")
            updated_fm = set_scalar_field(updated_fm, "status", "draft", "playbook_status")
            return render_memory_document(updated_fm, body_lines).encode("utf-8"), True
        rebuilt_pb = f"---\ntype: playbook\nplaybook_status: draft\nstatus: draft\n---\n\n{text.strip()}\n"
        return rebuilt_pb.encode("utf-8"), True
    if clean_rel.startswith("6-skills/") and clean_rel.count("/") == 1 and clean_rel.endswith(".md"):
        text = candidate_bytes.decode("utf-8", errors="replace")
        parsed = split_frontmatter(text)
        if parsed is not None:
            fm_lines, body_lines = parsed
            updated_fm = set_scalar_field(fm_lines, "skill_status", "revoked", "type")
            return render_memory_document(updated_fm, body_lines).encode("utf-8"), True
        rebuilt_sk = f"---\ntype: executable_skill\nskill_status: revoked\n---\n\n{text.strip()}\n"
        return rebuilt_sk.encode("utf-8"), True
    return candidate_bytes, False


def resolve_sync_conflict(
    conflict_id_or_rel_path: str,
    *,
    resolution: str,
    merged_content: str | None = None,
    expected_local_sha256: str | None = None,
    actor: str = "owner",
    brain_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Resolve a quarantined sync conflict atomically with CAS, .history/ backup, and canonical trust guards."""
    needle = (conflict_id_or_rel_path or "").strip()
    if not needle:
        raise ValueError("conflict_id or rel_path is required.")
    strat = (resolution or "").strip().lower()
    if strat not in {"keep_local", "accept_remote", "keep_both", "manual_merge"}:
        raise ValueError("resolution must be one of: keep_local, accept_remote, keep_both, manual_merge.")
    if strat == "manual_merge":
        if not isinstance(merged_content, str) or not merged_content.strip():
            raise ValueError("'merged_content' is required when resolution='manual_merge'.")
        if len(merged_content.encode("utf-8")) > MAX_SYNC_FILE_BYTES:
            raise ValueError("merged_content exceeds maximum sync file size.")

    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    lock_path = active_brain / ".sidecar" / "writer.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    changed_atoms: list[str] = []
    deleted_atoms: list[str] = []
    sibling_rel: str | None = None
    demoted_to_pending = False

    with review_lock(str(lock_path)):
        local_state = _load_local_sync_state(active_brain)
        raw_conflicts = list(local_state.get("conflicts") or [])
        idx = next(
            (
                i
                for i, c in enumerate(raw_conflicts)
                if _conflict_entry_id(c) == needle or str(c.get("rel_path") or "") == needle
            ),
            None,
        )
        if idx is None:
            raise FileNotFoundError(f"Sync conflict not found: {needle}")

        entry = raw_conflicts[idx]
        cid = _conflict_entry_id(entry)
        clean_rel = validate_sync_rel_path(str(entry["rel_path"]))
        dest = active_brain.joinpath(*clean_rel.split("/"))
        _assert_no_symlink_chain(dest, stop_at=active_brain)

        existing_local_bytes = dest.read_bytes() if dest.is_file() else None
        if expected_local_sha256:
            actual_sha = content_sha256(existing_local_bytes) if existing_local_bytes is not None else ""
            if actual_sha != expected_local_sha256.strip().lower():
                raise SyncConflictError(
                    f"CAS mismatch on '{clean_rel}': local file changed since conflict was inspected."
                )

        q_path = _resolve_conflict_quarantine_path(active_brain, entry.get("conflict_copy"))
        remote_sha = entry.get("remote_sha256")
        remote_bytes: bytes | None = None
        if q_path is not None and q_path.is_file():
            remote_bytes = q_path.read_bytes()
            if remote_sha and content_sha256(remote_bytes) != str(remote_sha):
                raise SyncSecurityError(f"Quarantined conflict copy hash mismatch for '{clean_rel}'.")

        is_top_atom = clean_rel.startswith("2-atoms/") and clean_rel.count("/") == 1 and clean_rel.endswith(".md")
        atom_fname = clean_rel.split("/", 1)[1] if is_top_atom else None

        if strat == "keep_local":
            if existing_local_bytes is None:
                raise SyncError(f"Local file '{clean_rel}' no longer exists; cannot keep_local.")
            if atom_fname:
                changed_atoms.append(atom_fname)

        elif strat == "accept_remote":
            if entry.get("conflict_kind") == "modify_delete" or remote_sha is None:
                if existing_local_bytes is not None:
                    _backup_before_sync_overwrite(active_brain, clean_rel, existing_local_bytes)
                    dest.unlink()
                if atom_fname:
                    deleted_atoms.append(atom_fname)
            else:
                if remote_bytes is None:
                    raise SyncError(f"Quarantined remote copy missing for '{clean_rel}'.")
                safe_remote_bytes, rem_demoted = _demote_unapproved_canonical_merge(
                    active_brain,
                    clean_rel,
                    remote_bytes,
                    force_demote=False,
                )
                demoted_to_pending = demoted_to_pending or rem_demoted
                if existing_local_bytes is not None:
                    _backup_before_sync_overwrite(active_brain, clean_rel, existing_local_bytes)
                dest.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(str(dest), safe_remote_bytes)
                if atom_fname:
                    changed_atoms.append(atom_fname)

        elif strat == "keep_both":
            if existing_local_bytes is None:
                raise SyncError(f"Local file '{clean_rel}' no longer exists; cannot keep_both.")
            if remote_bytes is None or entry.get("conflict_kind") == "modify_delete":
                raise SyncError(f"Remote copy does not exist for '{clean_rel}'; use keep_local or accept_remote.")
            dev_slug = re.sub(r"[^a-zA-Z0-9_-]+", "", str(entry.get("remote_device_id") or "peer"))[:16] or "peer"
            p_obj = Path(clean_rel)
            cand_stem = f"{p_obj.stem}-peer-{dev_slug}"
            sib_path = dest.parent / f"{cand_stem}{p_obj.suffix}"
            suffix_n = 2
            while sib_path.exists():
                sib_path = dest.parent / f"{cand_stem}-{suffix_n}{p_obj.suffix}"
                suffix_n += 1
            sibling_rel = validate_sync_rel_path(sib_path.relative_to(active_brain).as_posix())
            safe_peer_bytes, peer_demoted = _demote_unapproved_canonical_merge(
                active_brain,
                sibling_rel,
                remote_bytes,
                force_demote=True,
            )
            demoted_to_pending = demoted_to_pending or peer_demoted
            atomic_write_bytes(str(sib_path), safe_peer_bytes)
            if sibling_rel.startswith("2-atoms/") and sibling_rel.count("/") == 1 and sibling_rel.endswith(".md"):
                changed_atoms.append(sibling_rel.split("/", 1)[1])

        elif strat == "manual_merge":
            assert merged_content is not None
            norm_text = merged_content.replace("\r\n", "\n")
            if not norm_text.endswith("\n"):
                norm_text += "\n"
            merged_bytes, was_demoted = _demote_unapproved_canonical_merge(
                active_brain,
                clean_rel,
                norm_text.encode("utf-8"),
                force_demote=False,
            )
            demoted_to_pending = was_demoted
            if existing_local_bytes is not None:
                _backup_before_sync_overwrite(active_brain, clean_rel, existing_local_bytes)
            dest.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(str(dest), merged_bytes)
            if atom_fname:
                changed_atoms.append(atom_fname)

        # Advance base cursor for clean_rel to remote_sha (so remote epoch is acknowledged without re-conflicting)
        files_map = dict(local_state.get("files") or {})
        if remote_sha and isinstance(remote_sha, str):
            files_map[clean_rel] = remote_sha
            if remote_bytes is not None:
                _cache_sync_base_blob(active_brain, remote_sha, remote_bytes)
        else:
            files_map.pop(clean_rel, None)

        raw_conflicts.pop(idx)
        local_state["files"] = files_map
        local_state["conflicts"] = raw_conflicts
        _save_local_sync_state(active_brain, local_state)

        if q_path is not None and q_path.is_file():
            still_used = any(c.get("conflict_copy") == entry.get("conflict_copy") for c in raw_conflicts)
            if not still_used:
                try:
                    q_path.unlink()
                except OSError:
                    pass

        if clean_rel == "0-raw/archive/decision-record.jsonl":
            invalidate_decision_log_cache()
        atoms_dir = active_brain / "2-atoms"
        lex_path = active_brain / ".sidecar" / "lexical.sqlite3"
        atoms_dir.mkdir(parents=True, exist_ok=True)
        rebuild_lexical_index(str(atoms_dir), str(lex_path))
        if changed_atoms or deleted_atoms:
            _sync_dense_vectors_incremental(active_brain, changed_atoms, deleted_atoms)

    try:
        ensure_notes_index(NotePaths.for_brain(active_brain))
    except Exception:
        pass

    final_sha = content_sha256(dest.read_bytes()) if dest.is_file() else None
    return {
        "status": "resolved",
        "conflict_id": cid,
        "rel_path": clean_rel,
        "resolution": strat,
        "actor": (actor or "owner").strip()[:120] or "owner",
        "resolved_sha256": final_sha,
        "sibling_rel_path": sibling_rel,
        "demoted_unapproved_canonical": demoted_to_pending,
        "remaining_conflicts": len(raw_conflicts),
    }


# ─── Background Auto-Sync Scheduler ──────────────────────────────────────────

class AutoSyncScheduler:
    """Thread-safe background pull+push sync loop with conflict pause and exponential backoff."""

    def __init__(
        self,
        brain_dir: str | Path,
        *,
        interval_seconds: int = 300,
        state_dir: Path | None = None,
        http_transport: httpx.BaseTransport | None = None,
        skip_dns_check_for_mock_transport: bool = False,
    ) -> None:
        self.brain_dir = Path(brain_dir).resolve()
        self.interval_seconds = max(5, min(int(interval_seconds), 86400))
        self.state_dir = state_dir
        self.http_transport = http_transport
        self.skip_dns_check_for_mock_transport = skip_dns_check_for_mock_transport
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.last_run_at: str | None = None
        self.last_status: str = "idle"
        self.last_error: str | None = None
        self.consecutive_failures: int = 0
        self.cycles_completed: int = 0

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._wake_event.clear()
            t = threading.Thread(
                target=self._loop,
                name=f"sb-autosync-{_vault_key_slug(self.brain_dir)[:8]}",
                daemon=True,
            )
            self._thread = t
            t.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        with self._lock:
            t = self._thread
            self._thread = None
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=timeout)

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set()

    def run_once(self) -> dict[str, Any]:
        res = run_auto_sync_cycle(
            brain_dir=self.brain_dir,
            state_dir=self.state_dir,
            http_transport=self.http_transport,
            skip_dns_check_for_mock_transport=self.skip_dns_check_for_mock_transport,
        )
        with self._lock:
            self.last_run_at = _utc_now_iso()
            self.last_status = str(res.get("status") or "unknown")
            self.last_error = str(res["error"]) if res.get("error") else None
            if res.get("status") == "error":
                self.consecutive_failures += 1
            else:
                self.consecutive_failures = 0
            self.cycles_completed += 1
        return res

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self.run_once()
            with self._lock:
                fails = self.consecutive_failures
                base_iv = self.interval_seconds
            wait_s = min(3600.0, float(base_iv) * (2 ** min(fails, 5)))
            self._wake_event.wait(timeout=wait_s)
            self._wake_event.clear()


_AUTO_SYNC_REGISTRY_LOCK = threading.Lock()
_AUTO_SYNC_SCHEDULERS: dict[str, AutoSyncScheduler] = {}


def run_auto_sync_cycle(
    *,
    brain_dir: str | Path | None = None,
    state_dir: Path | None = None,
    http_transport: httpx.BaseTransport | None = None,
    skip_dns_check_for_mock_transport: bool = False,
) -> dict[str, Any]:
    """Execute one non-interactive pull -> conditional push cycle, pausing cleanly on conflicts or locked keys."""
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    cfg_path = sync_config_path(active_brain, state_dir=state_dir)
    if not cfg_path.exists():
        return {"status": "not_configured", "pushed": False, "pulled": False}

    open_conflicts = list_sync_conflicts(brain_dir=active_brain)
    if open_conflicts:
        return {
            "status": "paused_conflicts",
            "conflict_count": len(open_conflicts),
            "pushed": False,
            "pulled": False,
        }

    try:
        cfg = _load_sync_config(active_brain, state_dir=state_dir)
        _resolve_master_key(cfg, passphrase=None, state_dir=state_dir)
    except SyncSecurityError as exc:
        return {
            "status": "paused_locked",
            "error": str(exc),
            "pushed": False,
            "pulled": False,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc), "pushed": False, "pulled": False}

    try:
        pull_res = sync_pull(
            brain_dir=active_brain,
            passphrase=None,
            trust_new_peers=True,
            state_dir=state_dir,
            http_transport=http_transport,
            skip_dns_check_for_mock_transport=skip_dns_check_for_mock_transport,
        )
        if int(pull_res.get("conflict_count") or 0) > 0:
            return {
                "status": "paused_conflicts",
                "conflict_count": int(pull_res["conflict_count"]),
                "pulled": True,
                "pushed": False,
                "pull": pull_res,
            }

        local_state = _load_local_sync_state(active_brain)
        current_files = scan_durable_vault_files(active_brain)
        base_files = dict(local_state.get("files") or {})
        has_local_changes = (
            any(base_files.get(p) != info["sha256"] for p, info in current_files.items())
            or any(p not in current_files for p in base_files)
            or (int(local_state.get("last_seen_epoch", 0)) == 0 and len(current_files) > 0)
        )
        push_res: dict[str, Any] | None = None
        if has_local_changes:
            push_res = sync_push(
                brain_dir=active_brain,
                passphrase=None,
                state_dir=state_dir,
                http_transport=http_transport,
                skip_dns_check_for_mock_transport=skip_dns_check_for_mock_transport,
            )
        local_state = _load_local_sync_state(active_brain)
        local_state["last_auto_sync_at"] = _utc_now_iso()
        local_state["last_auto_sync_status"] = "synced"
        _save_local_sync_state(active_brain, local_state)
        return {
            "status": "synced",
            "pulled": True,
            "pushed": push_res is not None,
            "pull": pull_res,
            "push": push_res,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc), "pulled": False, "pushed": False}


def configure_auto_sync(
    *,
    enabled: bool,
    interval_seconds: int = 300,
    start_background: bool = True,
    brain_dir: str | Path | None = None,
    state_dir: Path | None = None,
    http_transport: httpx.BaseTransport | None = None,
    skip_dns_check_for_mock_transport: bool = False,
) -> dict[str, Any]:
    """Enable or disable background auto-sync for the active vault."""
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    cfg_path = sync_config_path(active_brain, state_dir=state_dir)
    cfg = _load_sync_config(active_brain, state_dir=state_dir)
    iv = max(5, min(int(interval_seconds), 86400))
    cfg["auto_sync_enabled"] = bool(enabled)
    cfg["auto_sync_interval_seconds"] = iv
    _atomic_write_restricted_json(cfg_path, cfg)

    slug = _vault_key_slug(active_brain)
    with _AUTO_SYNC_REGISTRY_LOCK:
        existing = _AUTO_SYNC_SCHEDULERS.get(slug)
        if not enabled:
            if existing is not None:
                existing.stop()
                _AUTO_SYNC_SCHEDULERS.pop(slug, None)
        else:
            if existing is not None:
                existing.interval_seconds = iv
                existing.state_dir = state_dir
                existing.http_transport = http_transport
                existing.skip_dns_check_for_mock_transport = skip_dns_check_for_mock_transport
                sched = existing
            else:
                sched = AutoSyncScheduler(
                    active_brain,
                    interval_seconds=iv,
                    state_dir=state_dir,
                    http_transport=http_transport,
                    skip_dns_check_for_mock_transport=skip_dns_check_for_mock_transport,
                )
                _AUTO_SYNC_SCHEDULERS[slug] = sched
            if start_background:
                sched.start()
    return get_auto_sync_status(brain_dir=active_brain, state_dir=state_dir)


def get_auto_sync_status(
    *,
    brain_dir: str | Path | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    cfg_path = sync_config_path(active_brain, state_dir=state_dir)
    enabled = False
    interval_seconds = 300
    if cfg_path.exists():
        try:
            cfg = _load_sync_config(active_brain, state_dir=state_dir)
            enabled = bool(cfg.get("auto_sync_enabled", False))
            interval_seconds = int(cfg.get("auto_sync_interval_seconds") or 300)
        except Exception:
            pass
    local_state = _load_local_sync_state(active_brain)
    slug = _vault_key_slug(active_brain)
    with _AUTO_SYNC_REGISTRY_LOCK:
        sched = _AUTO_SYNC_SCHEDULERS.get(slug)
    return {
        "enabled": enabled,
        "running": sched.is_running() if sched is not None else False,
        "interval_seconds": interval_seconds,
        "last_run_at": (sched.last_run_at if sched and sched.last_run_at else local_state.get("last_auto_sync_at")),
        "last_status": (sched.last_status if sched and sched.cycles_completed else local_state.get("last_auto_sync_status") or "idle"),
        "last_error": sched.last_error if sched else None,
        "consecutive_failures": sched.consecutive_failures if sched else 0,
        "cycles_completed": sched.cycles_completed if sched else 0,
    }


def stop_auto_sync_scheduler(brain_dir: str | Path) -> bool:
    """Stop and unregister the background AutoSyncScheduler for one specific vault, if running."""
    active_brain = Path(brain_dir).resolve()
    slug = _vault_key_slug(active_brain)
    with _AUTO_SYNC_REGISTRY_LOCK:
        sched = _AUTO_SYNC_SCHEDULERS.pop(slug, None)
    if sched is not None:
        sched.stop()
        return True
    return False


def stop_all_auto_sync_schedulers() -> None:
    with _AUTO_SYNC_REGISTRY_LOCK:
        scheds = list(_AUTO_SYNC_SCHEDULERS.values())
        _AUTO_SYNC_SCHEDULERS.clear()
    for s in scheds:
        s.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zero-Knowledge E2EE Multi-Device Vault Sync")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Initialize an encrypted sync target and local device identity")
    p_init.add_argument("target", help="Local sync folder (OneDrive/Syncthing/NAS/USB) or https:// S3/R2/object store URL")
    p_init.add_argument("--backend", choices=("local_folder", "http_object"), default="local_folder")
    p_init.add_argument("--device-name", default=None)
    p_init.add_argument("--kdf-mode", choices=("keyfile", "passphrase", "os_keychain"), default=None)
    p_init.add_argument("--use-keychain", action="store_true", help="Seal master key in OS Keychain / Windows DPAPI")
    p_init.add_argument("--passphrase-env", default=None, help="Env var holding optional passphrase")
    p_init.add_argument("--auth-mode", choices=("bearer", "s3_sigv4", "none"), default="bearer")
    p_init.add_argument("--bearer-token-env", default=None, help="Env var holding Bearer token for http_object backend")
    p_init.add_argument("--s3-region", default="auto", help="S3/R2 region (e.g. 'auto', 'us-east-1')")
    p_init.add_argument("--s3-access-key-env", default=None, help="Env var holding S3 Access Key ID")
    p_init.add_argument("--s3-secret-key-env", default=None, help="Env var holding S3 Secret Access Key")
    p_init.add_argument("--s3-session-token-env", default=None, help="Optional env var holding STS Session Token")
    p_init.add_argument("--json", action="store_true")

    p_join = sub.add_parser("join", help="Pair this device to an existing encrypted sync target")
    p_join.add_argument("target", help="Local sync folder or https:// S3/R2/object store URL")
    p_join.add_argument("--backend", choices=("local_folder", "http_object"), default="local_folder")
    p_join.add_argument("--device-name", default=None)
    p_join.add_argument("--kdf-mode", choices=("keyfile", "passphrase", "os_keychain"), default=None)
    p_join.add_argument("--use-keychain", action="store_true", help="Seal master key in OS Keychain / Windows DPAPI")
    p_join.add_argument("--recovery-key-env", default=None, help="Env var holding base64 256-bit recovery key")
    p_join.add_argument("--passphrase-env", default=None, help="Env var holding vault passphrase")
    p_join.add_argument("--auth-mode", choices=("bearer", "s3_sigv4", "none"), default="bearer")
    p_join.add_argument("--bearer-token-env", default=None)
    p_join.add_argument("--s3-region", default="auto")
    p_join.add_argument("--s3-access-key-env", default=None)
    p_join.add_argument("--s3-secret-key-env", default=None)
    p_join.add_argument("--s3-session-token-env", default=None)
    p_join.add_argument("--json", action="store_true")

    p_status = sub.add_parser("status", help="Show E2EE sync status, epochs, keychain, auto-sync, and pending conflicts")
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

    p_gc = sub.add_parser("gc", help="Prune unreferenced encrypted blobs and superseded manifests")
    p_gc.add_argument("--passphrase-env", default=None)
    p_gc.add_argument("--dry-run", action="store_true")
    p_gc.add_argument("--json", action="store_true")

    p_kc = sub.add_parser("seal-keychain", help="Migrate existing master key into OS Keychain / Windows DPAPI")
    p_kc.add_argument("--passphrase-env", default=None)
    p_kc.add_argument("--json", action="store_true")

    p_auto = sub.add_parser("auto", help="Configure or inspect background auto-sync")
    p_auto.add_argument("--enable", action="store_true", help="Enable background auto-sync")
    p_auto.add_argument("--disable", action="store_true", help="Disable background auto-sync")
    p_auto.add_argument("--run-once", action="store_true", help="Run a single auto-sync cycle immediately")
    p_auto.add_argument("--interval", type=int, default=300, help="Sync interval in seconds (default 300)")
    p_auto.add_argument("--json", action="store_true")

    p_conf = sub.add_parser("conflicts", help="List quarantined sync conflicts awaiting owner resolution")
    p_conf.add_argument("--json", action="store_true")

    p_cdet = sub.add_parser("conflict-detail", help="Inspect 3-way diff for a specific sync conflict")
    p_cdet.add_argument("conflict", help="conflict_id or relative vault path")
    p_cdet.add_argument("--json", action="store_true")

    p_res = sub.add_parser("resolve", help="Resolve a sync conflict (keep_local, accept_remote, keep_both, manual_merge)")
    p_res.add_argument("conflict", help="conflict_id or relative vault path")
    p_res.add_argument(
        "--strategy",
        choices=("keep_local", "accept_remote", "keep_both", "manual_merge"),
        required=True,
    )
    p_res.add_argument("--merged-file", default=None, help="Path to merged file when --strategy=manual_merge")
    p_res.add_argument("--expected-sha256", default=None, help="Optional CAS local file SHA-256")
    p_res.add_argument("--actor", default="owner")
    p_res.add_argument("--json", action="store_true")

    p_dpair = sub.add_parser("drop-pair", help="Create a forward-secret E2EE Mobile Inbox Drop pairing (PWA + iOS Shortcut)")
    p_dpair.add_argument("--label", default="Mobile Quick-Capture")
    p_dpair.add_argument("--s3-access-key-env", default=None, help="Optional env var with write-scoped S3 Access Key ID")
    p_dpair.add_argument("--s3-secret-key-env", default=None, help="Optional env var with write-scoped S3 Secret Access Key")
    p_dpair.add_argument("--pwa-url", default="https://localhost/inbox-drop/")
    p_dpair.add_argument("--include-recall", action="store_true", help="Include E2EE mobile recall/search/review key (`SBMR1`)")
    p_dpair.add_argument("--json", action="store_true")

    p_mrec = sub.add_parser("export-mobile-recall", help="Encrypt and export E2EE mobile recall/search/review catalog (`mobile-recall/<vault_id>.enc`)")
    p_mrec.add_argument("--output", default=None, help="Optional local output path for encrypted .enc catalog")
    p_mrec.add_argument("--no-pending", action="store_true", help="Exclude pending review queue from mobile catalog")
    p_mrec.add_argument("--json", action="store_true")

    p_dlist = sub.add_parser("drop-list", help="List configured Mobile Inbox Drop pairings")
    p_dlist.add_argument("--json", action="store_true")

    p_drev = sub.add_parser("drop-revoke", help="Revoke a Mobile Inbox Drop pairing token_id")
    p_drev.add_argument("token_id", help="16-char hex token_id")
    p_drev.add_argument("--json", action="store_true")

    p_dpull = sub.add_parser("pull-drops", help="Pull and decrypt E2EE mobile inbox-drops/*.enc into 0-raw/inbox/")
    p_dpull.add_argument("--json", action="store_true")

    p_dpwa = sub.add_parser("export-pwa", help="Export the static zero-dependency Mobile Inbox Drop PWA bundle")
    p_dpwa.add_argument("dest_dir", help="Destination directory for static PWA files")
    p_dpwa.add_argument("--json", action="store_true")

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
            kdf_mode=args.kdf_mode,
            use_keychain=bool(args.use_keychain),
            bearer_token_env=args.bearer_token_env,
            auth_mode=args.auth_mode,
            s3_region=args.s3_region,
            s3_access_key_env=args.s3_access_key_env,
            s3_secret_key_env=args.s3_secret_key_env,
            s3_session_token_env=args.s3_session_token_env,
        )
        print(json.dumps(res, indent=2) if args.json else f"Initialized E2EE sync vault={res['vault_id']} device={res['device_id']} target={res['target']} kdf={res['kdf_mode']}")
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
            kdf_mode=args.kdf_mode,
            use_keychain=bool(args.use_keychain),
            bearer_token_env=args.bearer_token_env,
            auth_mode=args.auth_mode,
            s3_region=args.s3_region,
            s3_access_key_env=args.s3_access_key_env,
            s3_secret_key_env=args.s3_secret_key_env,
            s3_session_token_env=args.s3_session_token_env,
        )
        print(json.dumps(res, indent=2) if args.json else f"Joined E2EE sync vault={res['vault_id']} device={res['device_id']} kdf={res['kdf_mode']}")
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
    if args.cmd == "gc":
        res = sync_gc(brain_dir=brain_dir, passphrase=passphrase, dry_run=bool(args.dry_run))
        print(json.dumps(res, indent=2) if args.json else f"status={res['status']} deleted_blobs={res['deleted_blobs']} deleted_manifests={res['deleted_manifests']}")
        return 0
    if args.cmd == "seal-keychain":
        res = migrate_sync_key_to_keychain(brain_dir=brain_dir, passphrase=passphrase)
        print(json.dumps(res, indent=2) if args.json else f"Sealed sync master key in {res['keychain_backend']} (kdf_mode={res['kdf_mode']})")
        return 0
    if args.cmd == "auto":
        if args.run_once:
            res = run_auto_sync_cycle(brain_dir=brain_dir)
        elif args.enable:
            res = configure_auto_sync(enabled=True, interval_seconds=args.interval, start_background=False, brain_dir=brain_dir)
        elif args.disable:
            res = configure_auto_sync(enabled=False, interval_seconds=args.interval, start_background=False, brain_dir=brain_dir)
        else:
            res = get_auto_sync_status(brain_dir=brain_dir)
        print(json.dumps(res, indent=2) if args.json else f"auto_sync={res}")
        return 0
    if args.cmd == "conflicts":
        items = list_sync_conflicts(brain_dir=brain_dir)
        print(json.dumps({"count": len(items), "conflicts": items}, indent=2) if args.json else f"conflicts={len(items)}")
        return 0
    if args.cmd == "conflict-detail":
        det = get_sync_conflict_detail(args.conflict, brain_dir=brain_dir)
        print(json.dumps(det, indent=2) if args.json else (det["unified_diff"] or "(no text diff)"))
        return 0
    if args.cmd == "resolve":
        merged_txt = Path(args.merged_file).read_text(encoding="utf-8") if args.merged_file else None
        res = resolve_sync_conflict(
            args.conflict,
            resolution=args.strategy,
            merged_content=merged_txt,
            expected_local_sha256=args.expected_sha256,
            actor=args.actor,
            brain_dir=brain_dir,
        )
        print(json.dumps(res, indent=2) if args.json else f"Resolved {res['rel_path']} ({res['resolution']}) remaining={res['remaining_conflicts']}")
        return 0
    if args.cmd == "drop-pair":
        from inbox_drop import create_mobile_drop_pairing

        cfg = _load_sync_config(Path(brain_dir).resolve())
        ak = os.environ.get(args.s3_access_key_env, "") if args.s3_access_key_env else ""
        sk = os.environ.get(args.s3_secret_key_env, "") if args.s3_secret_key_env else ""
        res = create_mobile_drop_pairing(
            label=args.label,
            vault_id=str(cfg["vault_id"]),
            sync_target_url=str(cfg.get("target") or "") if cfg.get("backend_type") == "http_object" else "",
            s3_region=str(cfg.get("s3_region") or "auto"),
            s3_access_key_id=ak,
            s3_secret_access_key=sk,
            pwa_base_url=args.pwa_url,
            include_recall=bool(args.include_recall),
            brain_dir=brain_dir,
        )
        print(json.dumps(res, indent=2) if args.json else f"Paired mobile drop token_id={res['token_id']} include_recall={res['include_recall']} pwa_url={res['pwa_pairing_url']}")
        return 0
    if args.cmd == "export-mobile-recall":
        from inbox_drop import export_mobile_recall_index

        res = export_mobile_recall_index(
            brain_dir=brain_dir,
            output_file=args.output,
            include_pending_review=not bool(args.no_pending),
        )
        print(json.dumps(res, indent=2) if args.json else f"Exported mobile recall index {res['object_key']} (atoms={res['atom_count']}, review_queue={res['review_count']}, bytes={res['envelope_bytes']})")
        return 0
    if args.cmd == "drop-list":
        from inbox_drop import list_mobile_drop_pairings

        vid = None
        cfg_p = sync_config_path(Path(brain_dir).resolve())
        if cfg_p.exists():
            vid = str(_load_sync_config(Path(brain_dir).resolve()).get("vault_id") or "") or None
        res = list_mobile_drop_pairings(vault_id=vid, brain_dir=brain_dir)
        print(json.dumps(res, indent=2) if args.json else f"active_pairings={res['count']} key_id={res.get('key_id')}")
        return 0
    if args.cmd == "drop-revoke":
        from inbox_drop import revoke_mobile_drop_pairing

        res = revoke_mobile_drop_pairing(args.token_id, brain_dir=brain_dir)
        print(json.dumps(res, indent=2) if args.json else f"Revoked mobile drop pairing {res['token_id']}")
        return 0
    if args.cmd == "pull-drops":
        res = pull_mobile_inbox_drops(brain_dir=brain_dir)
        print(json.dumps(res, indent=2) if args.json else f"Ingested {res['ingested_count']} mobile inbox drop(s)")
        return 0
    if args.cmd == "export-pwa":
        from inbox_drop import export_static_pwa_bundle

        res = export_static_pwa_bundle(args.dest_dir)
        print(json.dumps(res, indent=2) if args.json else f"Exported static PWA ({len(res['files'])} files) to {res['destination']}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
