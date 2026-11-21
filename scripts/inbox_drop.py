# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["cryptography", "filelock", "httpx"]
# ///
"""Zero-Knowledge Forward-Secret Mobile Inbox Drop (`SBID1\\x00`).

Allows a static mobile PWA or iOS Shortcut Share Target to client-side encrypt URLs and
quick notes into `inbox-drops/<drop_id>.enc` on the user's S3/R2 bucket (or local sync
folder) with ZERO relay server and ZERO vault read/forge capability:

  * Asymmetric forward secrecy: Ephemeral `ECDH P-256` (`SECP256R1`) + `HKDF-SHA256`
    + `AES-256-GCM` sealed box (`SBID1\\x00`). Mobile devices hold ONLY the vault's
    public `P-256` encryption key — never the vault sync `master_key` or `Ed25519` head
    signing key.
  * Per-device revocable authorization: Encrypt-then-MAC `HMAC-SHA256` (`auth_hmac`)
    bound to a per-device `token_id` + `drop_secret` so compromised mobile pairings can
    be revoked individually without re-keying the vault.
  * Strict quarantine boundary: Decrypted drops are replay-deduplicated, DLP/injection
    risk-scanned, stamped with `<!-- second_brain_origin: web_remote -->`, and written
    exclusively (`O_CREAT | O_EXCL`) into `brain/0-raw/inbox/mobile-drop-*.md`.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import struct
import sys
import threading
from typing import Any
from urllib.parse import quote, urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from filelock import FileLock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import content_sha256, review_lock  # noqa: E402
from config import _is_reparse_or_link, resolve_active_brain_dir, safe_file_in_dir, security_state_dir  # noqa: E402
from host_adapters import inspect_and_redact_outbound, restrict_security_file_permissions  # noqa: E402
from inbox_workflow import scan_inbox_payload_risks  # noqa: E402
from memory_format import normalize_temporal_bound_str  # noqa: E402


SBID_MAGIC = b"SBID1\x00"
SBID_VERSION = 1
NONCE_BYTES = 12
GCM_TAG_BYTES = 16
MAX_DROP_ENVELOPE_BYTES = 262_144  # 256 KiB hard ceiling per mobile quick-capture
MAX_DROP_HEADER_BYTES = 4096
MAX_DROPS_PER_CYCLE = 50
MAX_SKIPPED_CLEANUP_PER_CYCLE = 100
MAX_PROCESSED_DROP_IDS = 4096
INBOX_DROP_S3_ACCESS_KEY_ENV = "BRAIN_INBOX_DROP_S3_ACCESS_KEY_ID"
INBOX_DROP_S3_SECRET_KEY_ENV = "BRAIN_INBOX_DROP_S3_SECRET_ACCESS_KEY"

_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_DROP_KEY_RE = re.compile(r"^inbox-drops/([0-9a-f]{32})\.enc$")
_INBOX_DROP_STATE_LOCK = threading.RLock()


class InboxDropError(RuntimeError):
    """Raised on invalid inbox drop configuration or usage."""


class InboxDropSecurityError(InboxDropError):
    """Raised when an inbox drop envelope fails authentication, decryption, or path safety."""


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    clean = (raw or "").strip()
    pad = "=" * ((4 - (len(clean) % 4)) % 4)
    return base64.urlsafe_b64decode(clean + pad)


def _assert_no_symlinks(path: Path, *, stop_at: Path | None = None) -> None:
    stop_resolved = stop_at.resolve() if stop_at is not None and stop_at.exists() else None
    for cur in (path, *path.parents):
        if os.path.lexists(str(cur)) and _is_reparse_or_link(str(cur)):
            raise InboxDropSecurityError(f"Refusing to access inbox-drop state via symlink/reparse point: {cur}")
        if stop_resolved is not None and cur == stop_resolved:
            break


def _vault_slug(brain_dir: Path) -> str:
    norm = os.path.normcase(str(brain_dir.resolve()))
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", brain_dir.name).strip("-")[:24] or "vault"
    return f"{stem}-{digest}"


def inbox_drop_state_path(brain_dir: str | Path, *, state_dir: str | Path | None = None) -> Path:
    base = Path(state_dir).expanduser().resolve() if state_dir is not None else (security_state_dir() / "sync")
    return base / f"inbox-drop-{_vault_slug(Path(brain_dir).resolve())}.json"


def _inbox_drop_lock_path(brain_dir: str | Path, *, state_dir: str | Path | None = None) -> Path:
    base = Path(state_dir).expanduser().resolve() if state_dir is not None else (security_state_dir() / "sync")
    return base / f"inbox-drop-{_vault_slug(Path(brain_dir).resolve())}.lock"


def resolve_scoped_inbox_drop_s3_credentials(
    *,
    s3_access_key_id: str = "",
    s3_secret_access_key: str = "",
    include_from_env: bool = False,
) -> tuple[str, str]:
    """Resolve optional write-only S3/R2 `inbox-drops/*` credentials without exporting full-bucket sync keys."""
    ak = str(s3_access_key_id or "").strip()
    sk = str(s3_secret_access_key or "").strip()
    if ak and sk:
        return ak, sk
    if bool(ak) != bool(sk):
        raise InboxDropError("Provide both s3_access_key_id and s3_secret_access_key, or omit both.")
    if not include_from_env:
        return "", ""
    env_ak = (os.environ.get(INBOX_DROP_S3_ACCESS_KEY_ENV) or "").strip()
    env_sk = (os.environ.get(INBOX_DROP_S3_SECRET_KEY_ENV) or "").strip()
    if env_ak and env_sk:
        return env_ak, env_sk
    raise InboxDropError(
        "Refusing to export full-bucket sync credentials into mobile pairing. "
        f"Provide scoped write-only `inbox-drops/*` PutObject credentials explicitly or set "
        f"{INBOX_DROP_S3_ACCESS_KEY_ENV} and {INBOX_DROP_S3_SECRET_KEY_ENV}."
    )


def _processed_drops_ledger_path(brain_dir: Path) -> Path:
    return brain_dir / ".sidecar" / "processed-inbox-drops.json"


def _atomic_write_restricted_json(path: Path, payload: dict[str, Any]) -> None:
    _assert_no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    restrict_security_file_permissions(path.parent)
    tmp = path.parent / f".tmp-{path.name}-{secrets.token_hex(6)}"
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(str(tmp), flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
        restrict_security_file_permissions(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _load_drop_state(brain_dir: Path, *, state_dir: str | Path | None = None) -> dict[str, Any] | None:
    p = inbox_drop_state_path(brain_dir, state_dir=state_dir)
    if not os.path.lexists(str(p)):
        return None
    _assert_no_symlinks(p)
    st = os.lstat(str(p))
    if not stat.S_ISREG(st.st_mode) or st.st_size > 262_144:
        raise InboxDropSecurityError(f"Invalid inbox-drop state file: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _compute_auth_hmac(
    drop_secret: bytes,
    *,
    vault_id: str,
    drop_id: str,
    key_id: str,
    token_id: str,
    epk_b64: str,
    created_at: str,
    body_sha256_hex: str,
) -> str:
    msg = (
        f"SBID1-AUTH\n{vault_id}\n{drop_id}\n{key_id}\n{token_id}\n"
        f"{epk_b64}\n{created_at}\n{body_sha256_hex}"
    ).encode("utf-8")
    return hmac.new(drop_secret, msg, hashlib.sha256).hexdigest()


def _derive_drop_aes_key(
    shared_secret: bytes,
    *,
    vault_id: str,
    drop_id: str,
    key_id: str,
    token_id: str,
) -> bytes:
    info = f"SecondBrain-InboxDrop-v1|{vault_id}|{key_id}|{token_id}".encode("utf-8")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=bytes.fromhex(drop_id),
        info=info,
    ).derive(shared_secret)


def _ensure_inbox_drop_keypair_unlocked(
    *,
    vault_id: str,
    active_brain: Path,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    existing = _load_drop_state(active_brain, state_dir=state_dir)
    if isinstance(existing, dict) and existing.get("vault_id") == vault_id and existing.get("privkey_pkcs8_b64"):
        return existing

    priv = ec.generate_private_key(ec.SECP256R1())
    priv_der = priv.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    pub_b64 = base64.b64encode(pub_raw).decode("ascii")
    key_id = hashlib.sha256(pub_raw).hexdigest()[:16]

    doc: dict[str, Any] = {
        "schema_version": 1,
        "vault_id": vault_id,
        "key_id": key_id,
        "curve": "P-256",
        "pubkey_uncompressed_b64": pub_b64,
        "privkey_pkcs8_b64": base64.b64encode(priv_der).decode("ascii"),
        "pairings": list(existing.get("pairings") or []) if isinstance(existing, dict) else [],
        "created_at": _utc_now_iso(),
    }
    _atomic_write_restricted_json(inbox_drop_state_path(active_brain, state_dir=state_dir), doc)
    return doc


def ensure_inbox_drop_keypair(
    *,
    vault_id: str,
    brain_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Ensure this vault has a P-256 ECDH inbox-drop keypair stored in restricted security state."""
    if not _HEX32_RE.fullmatch(str(vault_id or "")):
        raise InboxDropError(f"Invalid 32-char hex vault_id: {vault_id!r}")
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    lock_file = _inbox_drop_lock_path(active_brain, state_dir=state_dir)
    _assert_no_symlinks(lock_file)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    restrict_security_file_permissions(lock_file.parent)
    with _INBOX_DROP_STATE_LOCK, FileLock(str(lock_file), timeout=30):
        return _ensure_inbox_drop_keypair_unlocked(
            vault_id=vault_id,
            active_brain=active_brain,
            state_dir=state_dir,
        )


def create_mobile_drop_pairing(
    *,
    label: str = "Mobile Quick-Capture",
    vault_id: str,
    sync_target_url: str = "",
    s3_region: str = "auto",
    s3_access_key_id: str = "",
    s3_secret_access_key: str = "",
    pwa_base_url: str = "https://localhost/inbox-drop/",
    brain_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Create a per-device zero-knowledge mobile inbox drop pairing bundle + `#pair=` URI."""
    if not _HEX32_RE.fullmatch(str(vault_id or "")):
        raise InboxDropError(f"Invalid 32-char hex vault_id: {vault_id!r}")
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    clean_label = re.sub(r"\s+", " ", str(label or "Mobile Quick-Capture")).strip()[:80] or "Mobile Quick-Capture"
    clean_label = clean_label.replace("<!--", "").replace("-->", "").strip() or "Mobile Quick-Capture"
    lock_file = _inbox_drop_lock_path(active_brain, state_dir=state_dir)
    _assert_no_symlinks(lock_file)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    restrict_security_file_permissions(lock_file.parent)

    with _INBOX_DROP_STATE_LOCK, FileLock(str(lock_file), timeout=30):
        state = _ensure_inbox_drop_keypair_unlocked(
            vault_id=vault_id,
            active_brain=active_brain,
            state_dir=state_dir,
        )
        token_id = secrets.token_hex(8)
        drop_secret = secrets.token_bytes(32)
        drop_secret_b64 = base64.b64encode(drop_secret).decode("ascii")
        created_at = _utc_now_iso()

        pairing_entry = {
            "token_id": token_id,
            "label": clean_label,
            "drop_secret_b64": drop_secret_b64,
            "created_at": created_at,
            "revoked": False,
        }
        pairings = [p for p in (state.get("pairings") or []) if isinstance(p, dict)]
        pairings.append(pairing_entry)
        state["pairings"] = pairings
        _atomic_write_restricted_json(inbox_drop_state_path(active_brain, state_dir=state_dir), state)

    client_bundle: dict[str, Any] = {
        "v": SBID_VERSION,
        "vault_id": vault_id,
        "key_id": state["key_id"],
        "recipient_pubkey_b64": state["pubkey_uncompressed_b64"],
        "token_id": token_id,
        "drop_secret_b64": drop_secret_b64,
        "label": clean_label,
        "target_url": str(sync_target_url or "").strip().rstrip("/"),
        "s3_region": str(s3_region or "auto").strip() or "auto",
    }
    if s3_access_key_id and s3_secret_access_key:
        client_bundle["s3_access_key_id"] = s3_access_key_id.strip()
        client_bundle["s3_secret_access_key"] = s3_secret_access_key.strip()

    fragment = _b64url_encode(json.dumps(client_bundle, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    raw_base_pwa = (pwa_base_url or "https://localhost/inbox-drop/").strip().split("#", 1)[0]
    parsed_pwa = urlsplit(raw_base_pwa)
    pwa_scheme = parsed_pwa.scheme.lower()
    pwa_host = (parsed_pwa.hostname or "").lower()
    if (
        not pwa_host
        or parsed_pwa.username is not None
        or parsed_pwa.password is not None
        or pwa_scheme not in {"https", "http"}
        or (pwa_scheme == "http" and pwa_host not in {"127.0.0.1", "localhost", "::1"})
    ):
        raise InboxDropSecurityError(
            "pwa_base_url must use https:// (or loopback http://localhost / http://127.0.0.1) without embedded credentials."
        )
    pwa_launch_url = f"{raw_base_pwa}#pair={fragment}"

    return {
        "status": "paired",
        "token_id": token_id,
        "label": clean_label,
        "vault_id": vault_id,
        "key_id": state["key_id"],
        "recipient_pubkey_b64": state["pubkey_uncompressed_b64"],
        "created_at": created_at,
        "pairing_bundle": client_bundle,
        "pairing_fragment": f"#pair={fragment}",
        "pwa_pairing_url": pwa_launch_url,
        "ios_shortcut": generate_ios_shortcut_recipe(client_bundle, pwa_pairing_url=pwa_launch_url),
    }


def list_mobile_drop_pairings(
    *,
    vault_id: str | None = None,
    brain_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """List configured mobile inbox drop pairings without exposing secret HMAC keys."""
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    with _INBOX_DROP_STATE_LOCK:
        state = _load_drop_state(active_brain, state_dir=state_dir)
    if not isinstance(state, dict) or (vault_id and state.get("vault_id") != vault_id):
        return {
            "initialized": False,
            "vault_id": vault_id,
            "key_id": None,
            "recipient_pubkey_b64": None,
            "count": 0,
            "pairings": [],
        }
    items = [
        {
            "token_id": str(p.get("token_id") or ""),
            "label": str(p.get("label") or ""),
            "created_at": str(p.get("created_at") or ""),
            "revoked": bool(p.get("revoked", False)),
            "revoked_at": p.get("revoked_at"),
        }
        for p in (state.get("pairings") or [])
        if isinstance(p, dict)
    ]
    return {
        "initialized": True,
        "vault_id": state.get("vault_id"),
        "key_id": state.get("key_id"),
        "recipient_pubkey_b64": state.get("pubkey_uncompressed_b64"),
        "count": sum(1 for x in items if not x["revoked"]),
        "pairings": items,
    }


def revoke_mobile_drop_pairing(
    token_id: str,
    *,
    brain_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Revoke a specific mobile drop pairing token_id immediately."""
    clean_tid = (token_id or "").strip().lower()
    if not _HEX16_RE.fullmatch(clean_tid):
        raise InboxDropError(f"Invalid 16-char hex token_id: {token_id!r}")
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    lock_file = _inbox_drop_lock_path(active_brain, state_dir=state_dir)
    _assert_no_symlinks(lock_file)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    restrict_security_file_permissions(lock_file.parent)

    with _INBOX_DROP_STATE_LOCK, FileLock(str(lock_file), timeout=30):
        state = _load_drop_state(active_brain, state_dir=state_dir)
        if not isinstance(state, dict):
            raise FileNotFoundError(f"Mobile drop pairing not found: {clean_tid}")
        found = False
        for p in state.get("pairings") or []:
            if isinstance(p, dict) and str(p.get("token_id") or "") == clean_tid:
                p["revoked"] = True
                p["revoked_at"] = _utc_now_iso()
                p.pop("drop_secret_b64", None)
                found = True
        if not found:
            raise FileNotFoundError(f"Mobile drop pairing not found: {clean_tid}")
        _atomic_write_restricted_json(inbox_drop_state_path(active_brain, state_dir=state_dir), state)
    return {"status": "revoked", "token_id": clean_tid}


def generate_ios_shortcut_recipe(
    pairing_bundle: dict[str, Any],
    *,
    pwa_pairing_url: str = "",
) -> dict[str, Any]:
    """Generate a zero-server iOS Shortcut Share Sheet specification for E2EE S3/R2 quick-capture."""
    target = str(pairing_bundle.get("target_url") or "https://<bucket>.<account>.r2.cloudflarestorage.com")
    return {
        "shortcut_name": "Drop to Second Brain (E2EE)",
        "accepts_share_sheet": ["URLs", "Text", "Safari Web Pages", "Articles"],
        "zero_relay_server": True,
        "encryption_spec": "SBID1 (ECDH P-256 + HKDF-SHA256 + AES-256-GCM + AWS SigV4 PUT)",
        "object_key_template": f"{target.rstrip('/')}/inbox-drops/<32-hex-drop-id>.enc",
        "steps": [
            "1. Open the static `mobile/inbox-drop/index.html` PWA on iOS Safari using the `#pair=...` link once (auto-saves key to local storage and scrubs the URL hash) and tap Share -> Add to Home Screen.",
            "2. Or in Apple Shortcuts, create 'Drop to Second Brain' accepting Safari Web Pages / URLs / Text from Share Sheet.",
            "3. Add action 'URL' -> `<PWA_ORIGIN>/?url=[Shortcut Input]` and 'Open URLs' (pre-fills the PWA for 1-tap client-side WebCrypto P-256 sealed-box encryption + direct S3/R2 SigV4 PUT with zero third-party relay).",
        ],
        "pwa_share_url_template": (
            (pwa_pairing_url.split("#", 1)[0] if pwa_pairing_url else "https://localhost/inbox-drop/")
            + "?url={url}&title={title}&text={text}"
        ),
    }


def export_static_pwa_bundle(dest_dir: str | Path) -> dict[str, Any]:
    """Copy the self-contained static zero-dependency `mobile/inbox-drop/` PWA to `dest_dir`."""
    candidates = [
        Path(__file__).resolve().parent.parent / "mobile" / "inbox-drop",
        Path(__file__).resolve().parent / "mobile" / "inbox-drop",
    ]
    repo_mobile = next((p for p in candidates if p.is_dir()), candidates[0])
    if not repo_mobile.is_dir():
        raise InboxDropError(f"Static PWA source directory not found: {repo_mobile}")
    out = Path(dest_dir).expanduser().resolve()
    _assert_no_symlinks(out)
    out.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for item in sorted(repo_mobile.iterdir()):
        if item.is_file() and not _is_reparse_or_link(str(item)):
            dst = out / item.name
            _assert_no_symlinks(dst)
            shutil.copyfile(str(item), str(dst))
            copied.append(item.name)
    return {"status": "exported", "destination": str(out), "files": copied}


def encrypt_inbox_drop_envelope(
    payload: dict[str, Any],
    pairing_bundle: dict[str, Any],
    *,
    drop_id: str | None = None,
    created_at: str | None = None,
) -> tuple[str, bytes]:
    """Client-side encrypt a quick-capture payload into `(drop_id, SBID1_envelope_bytes)`."""
    vault_id = str(pairing_bundle.get("vault_id") or "")
    key_id = str(pairing_bundle.get("key_id") or "")
    token_id = str(pairing_bundle.get("token_id") or "")
    pub_b64 = str(pairing_bundle.get("recipient_pubkey_b64") or "")
    secret_b64 = str(pairing_bundle.get("drop_secret_b64") or "")

    if not _HEX32_RE.fullmatch(vault_id):
        raise InboxDropSecurityError("Invalid vault_id in pairing bundle.")
    if not _HEX16_RE.fullmatch(key_id) or not _HEX16_RE.fullmatch(token_id):
        raise InboxDropSecurityError("Invalid key_id or token_id in pairing bundle.")

    actual_drop_id = (drop_id or secrets.token_hex(16)).lower()
    if not _HEX32_RE.fullmatch(actual_drop_id):
        raise InboxDropSecurityError("drop_id must be 32 lowercase hex characters.")
    ts = created_at or _utc_now_iso()

    recipient_pub_bytes = base64.b64decode(pub_b64)
    if hashlib.sha256(recipient_pub_bytes).hexdigest()[:16] != key_id:
        raise InboxDropSecurityError("Recipient public key fingerprint does not match key_id.")
    recipient_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), recipient_pub_bytes)
    drop_secret = base64.b64decode(secret_b64)
    if len(drop_secret) != 32:
        raise InboxDropSecurityError("Invalid drop_secret length.")

    eph_priv = ec.generate_private_key(ec.SECP256R1())
    eph_pub_bytes = eph_priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    epk_b64 = base64.b64encode(eph_pub_bytes).decode("ascii")
    shared_z = eph_priv.exchange(ec.ECDH(), recipient_pub)
    aes_key = _derive_drop_aes_key(
        shared_z,
        vault_id=vault_id,
        drop_id=actual_drop_id,
        key_id=key_id,
        token_id=token_id,
    )

    clean_payload = {
        "kind": "quick_capture",
        "title": str(payload.get("title") or "")[:300],
        "url": str(payload.get("url") or "")[:2048],
        "text": str(payload.get("text") or "")[:64_000],
        "tags": [str(t)[:64] for t in (payload.get("tags") or [])[:16]] if isinstance(payload.get("tags"), list) else [],
        "captured_at": str(payload.get("captured_at") or ts)[:64],
        "client": str(payload.get("client") or "pwa")[:32],
    }
    plaintext_bytes = json.dumps(clean_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(plaintext_bytes) > MAX_DROP_ENVELOPE_BYTES - 1024:
        raise InboxDropError("Mobile quick-capture exceeds maximum size.")

    iv = os.urandom(NONCE_BYTES)
    aad_bind = f"SBID1|{vault_id}|{actual_drop_id}|{key_id}|{token_id}|{epk_b64}|{ts}".encode("utf-8")
    ct_with_tag = AESGCM(aes_key).encrypt(iv, plaintext_bytes, aad_bind)
    body_bytes = iv + ct_with_tag
    body_sha_hex = hashlib.sha256(body_bytes).hexdigest()

    auth_hmac = _compute_auth_hmac(
        drop_secret,
        vault_id=vault_id,
        drop_id=actual_drop_id,
        key_id=key_id,
        token_id=token_id,
        epk_b64=epk_b64,
        created_at=ts,
        body_sha256_hex=body_sha_hex,
    )
    header_obj = {
        "v": SBID_VERSION,
        "vault_id": vault_id,
        "drop_id": actual_drop_id,
        "key_id": key_id,
        "token_id": token_id,
        "epk_b64": epk_b64,
        "created_at": ts,
        "auth_hmac": auth_hmac,
    }
    header_bytes = json.dumps(header_obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    envelope = SBID_MAGIC + struct.pack(">H", len(header_bytes)) + header_bytes + body_bytes
    return actual_drop_id, envelope


def decrypt_inbox_drop_envelope(
    envelope: bytes,
    *,
    expected_drop_id: str,
    brain_dir: str | Path,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Authenticate and decrypt an `SBID1\\x00` mobile drop envelope (Encrypt-then-MAC + ECDH P-256)."""
    if not isinstance(envelope, (bytes, bytearray)):
        raise InboxDropSecurityError("Envelope must be bytes.")
    min_size = len(SBID_MAGIC) + 2 + 32 + NONCE_BYTES + GCM_TAG_BYTES
    if len(envelope) < min_size or len(envelope) > MAX_DROP_ENVELOPE_BYTES:
        raise InboxDropSecurityError(f"Out-of-bounds mobile drop envelope size: {len(envelope)} bytes.")
    if bytes(envelope[: len(SBID_MAGIC)]) != SBID_MAGIC:
        raise InboxDropSecurityError("Invalid SBID1 magic header on mobile inbox drop.")

    hdr_len = struct.unpack(">H", bytes(envelope[len(SBID_MAGIC) : len(SBID_MAGIC) + 2]))[0]
    if hdr_len <= 0 or hdr_len > MAX_DROP_HEADER_BYTES:
        raise InboxDropSecurityError(f"Invalid SBID1 header length: {hdr_len}")
    hdr_start = len(SBID_MAGIC) + 2
    hdr_end = hdr_start + hdr_len
    if len(envelope) < hdr_end + NONCE_BYTES + GCM_TAG_BYTES:
        raise InboxDropSecurityError("Truncated SBID1 ciphertext body.")

    try:
        header = json.loads(bytes(envelope[hdr_start:hdr_end]).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InboxDropSecurityError(f"Malformed SBID1 JSON header: {exc}") from exc
    if not isinstance(header, dict) or header.get("v") != SBID_VERSION:
        raise InboxDropSecurityError("Unsupported SBID1 envelope version.")

    active_brain = Path(brain_dir).resolve()
    with _INBOX_DROP_STATE_LOCK:
        state = _load_drop_state(active_brain, state_dir=state_dir)
    if not isinstance(state, dict) or not state.get("privkey_pkcs8_b64"):
        raise InboxDropSecurityError("Mobile inbox drop keypair is not initialized for this vault.")

    vault_id = str(header.get("vault_id") or "")
    drop_id = str(header.get("drop_id") or "")
    key_id = str(header.get("key_id") or "")
    token_id = str(header.get("token_id") or "")
    epk_b64 = str(header.get("epk_b64") or "")
    created_at = str(header.get("created_at") or "").strip()
    supplied_hmac = str(header.get("auth_hmac") or "").lower()

    if vault_id != str(state.get("vault_id") or ""):
        raise InboxDropSecurityError("Mobile drop vault_id does not match active vault.")
    if not _HEX32_RE.fullmatch(drop_id) or drop_id != expected_drop_id.lower():
        raise InboxDropSecurityError("Mobile drop_id does not match object key (rename/replay attempt blocked).")
    if key_id != str(state.get("key_id") or ""):
        raise InboxDropSecurityError("Mobile drop key_id does not match active vault P-256 public key.")
    if not _HEX64_RE.fullmatch(supplied_hmac):
        raise InboxDropSecurityError("Malformed auth_hmac in mobile drop header.")
    if not created_at or len(created_at) > 64 or any(ch in created_at for ch in ("\r", "\n", "<", ">")):
        raise InboxDropSecurityError("Malformed or unsafe created_at timestamp in mobile drop header.")
    try:
        normalize_temporal_bound_str(created_at)
    except ValueError as exc:
        raise InboxDropSecurityError(f"Invalid ISO-8601 created_at in mobile drop header: {exc}") from exc

    pairing = next(
        (
            p
            for p in (state.get("pairings") or [])
            if isinstance(p, dict) and str(p.get("token_id") or "") == token_id
        ),
        None,
    )
    if pairing is None:
        raise InboxDropSecurityError(f"Unrecognized mobile drop pairing token_id: {token_id!r}")
    if bool(pairing.get("revoked", False)) or not pairing.get("drop_secret_b64"):
        raise InboxDropSecurityError(f"Revoked mobile drop pairing token_id: {token_id!r}")

    drop_secret = base64.b64decode(str(pairing["drop_secret_b64"]))
    body_bytes = bytes(envelope[hdr_end:])
    body_sha_hex = hashlib.sha256(body_bytes).hexdigest()
    expected_hmac = _compute_auth_hmac(
        drop_secret,
        vault_id=vault_id,
        drop_id=drop_id,
        key_id=key_id,
        token_id=token_id,
        epk_b64=epk_b64,
        created_at=created_at,
        body_sha256_hex=body_sha_hex,
    )
    if not hmac.compare_digest(supplied_hmac, expected_hmac):
        raise InboxDropSecurityError("Mobile drop Encrypt-then-MAC HMAC verification failed.")

    try:
        epk_bytes = base64.b64decode(epk_b64)
        eph_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), epk_bytes)
        priv_der = base64.b64decode(str(state["privkey_pkcs8_b64"]))
        priv_key = serialization.load_der_private_key(priv_der, password=None)
        if not isinstance(priv_key, ec.EllipticCurvePrivateKey):
            raise InboxDropSecurityError("Stored inbox-drop private key is not P-256.")
        shared_z = priv_key.exchange(ec.ECDH(), eph_pub)
        aes_key = _derive_drop_aes_key(
            shared_z,
            vault_id=vault_id,
            drop_id=drop_id,
            key_id=key_id,
            token_id=token_id,
        )
        iv = body_bytes[:NONCE_BYTES]
        ct = body_bytes[NONCE_BYTES:]
        aad_bind = f"SBID1|{vault_id}|{drop_id}|{key_id}|{token_id}|{epk_b64}|{created_at}".encode("utf-8")
        plain_bytes = AESGCM(aes_key).decrypt(iv, ct, aad_bind)
    except InboxDropSecurityError:
        raise
    except (ValueError, TypeError, InvalidTag) as exc:
        raise InboxDropSecurityError(f"SBID1 ECDH/AES-256-GCM decryption failed: {exc}") from exc

    try:
        payload = json.loads(plain_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InboxDropSecurityError(f"Malformed decrypted mobile drop JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise InboxDropSecurityError("Decrypted mobile drop payload must be a JSON object.")

    return {
        "drop_id": drop_id,
        "token_id": token_id,
        "pairing_label": str(pairing.get("label") or "mobile"),
        "created_at": created_at,
        "payload": payload,
    }


def _load_processed_drop_ledger(brain_dir: Path) -> tuple[list[str], list[str]]:
    p = _processed_drops_ledger_path(brain_dir)
    if not os.path.lexists(str(p)):
        return [], []
    _assert_no_symlinks(p, stop_at=brain_dir)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(doc, dict):
            proc_raw = doc.get("processed_ids") if isinstance(doc.get("processed_ids"), list) else []
            rej_raw = doc.get("rejected_ids") if isinstance(doc.get("rejected_ids"), list) else []
            proc = [str(x) for x in proc_raw if _HEX32_RE.fullmatch(str(x))]
            rej = [str(x) for x in rej_raw if _HEX32_RE.fullmatch(str(x))]
            return proc, rej
    except Exception:
        pass
    return [], []


def _load_processed_drop_ids(brain_dir: Path) -> list[str]:
    proc, _ = _load_processed_drop_ledger(brain_dir)
    return proc


def _save_processed_drop_ledger(
    brain_dir: Path,
    processed_ids: list[str],
    rejected_ids: list[str] | None = None,
) -> None:
    p = _processed_drops_ledger_path(brain_dir)
    _assert_no_symlinks(p, stop_at=brain_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    if rejected_ids is None:
        _, rejected_ids = _load_processed_drop_ledger(brain_dir)
    bounded_proc = processed_ids[-MAX_PROCESSED_DROP_IDS:]
    bounded_rej = (rejected_ids or [])[-MAX_PROCESSED_DROP_IDS:]
    tmp = p.parent / f".tmp-drops-{secrets.token_hex(6)}"
    raw = (
        json.dumps(
            {
                "schema_version": 1,
                "processed_ids": bounded_proc,
                "rejected_ids": bounded_rej,
            },
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(p))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _save_processed_drop_ids(brain_dir: Path, ids: list[str]) -> None:
    _save_processed_drop_ledger(brain_dir, ids)


def _scrub_comment_delimiters(text: str) -> str:
    return text.replace("<!--", "").replace("-->", "")


def _render_quarantined_inbox_markdown(decrypted: dict[str, Any]) -> tuple[str, list[str]]:
    payload = decrypted["payload"]
    raw_title = re.sub(r"[\r\n]+", " ", str(payload.get("title") or "")).strip()[:240]
    raw_url = re.sub(r"[\r\n\s]+", "", str(payload.get("url") or "")).strip()[:2048]
    raw_body = str(payload.get("text") or "").replace("\x00", "").strip()[:64_000]
    raw_tags = payload.get("tags") if isinstance(payload.get("tags"), list) else []
    clean_tags = [
        re.sub(r"[^a-zA-Z0-9_:/.-]+", "", str(t)).strip()[:48]
        for t in raw_tags[:16]
        if str(t).strip()
    ]
    clean_tags = [t for t in clean_tags if t]

    risks: list[str] = []
    combined_for_scan = "\n".join(x for x in (raw_title, raw_url, raw_body, " ".join(clean_tags)) if x)
    for flag in scan_inbox_payload_risks(combined_for_scan):
        if str(flag) not in risks:
            risks.append(str(flag))

    url = raw_url
    if url:
        if any(ord(c) < 32 for c in url):
            url = ""
        else:
            parsed_u = urlsplit(url)
            if (parsed_u.scheme or "").lower() not in {"http", "https"} or not parsed_u.netloc or not parsed_u.hostname:
                url = ""
            elif parsed_u.username or parsed_u.password:
                url = ""
                if "dlp_secret:url_embedded_credentials" not in risks:
                    risks.append("dlp_secret:url_embedded_credentials")

    title_dlp = inspect_and_redact_outbound(raw_title, redact_pii=False, fail_on_secret_detection=False)
    url_dlp = inspect_and_redact_outbound(url, redact_pii=False, fail_on_secret_detection=False) if url else None
    body_dlp = inspect_and_redact_outbound(raw_body, redact_pii=False, fail_on_secret_detection=False)
    for rep in (title_dlp, url_dlp, body_dlp):
        if rep is None:
            continue
        for hit in rep.secret_hits:
            lbl = f"dlp_secret:{hit}"
            if lbl not in risks:
                risks.append(lbl)

    title = _scrub_comment_delimiters(title_dlp.sanitized_text).strip()
    safe_url = _scrub_comment_delimiters(url_dlp.sanitized_text).strip() if url_dlp is not None else ""
    body_text = body_dlp.sanitized_text

    # Strip any forged HTML origin comments (including multiline or unclosed markers) inside untrusted mobile text
    safe_body = re.sub(
        r"<!--\s*second_brain_origin.*?-->",
        "[stripped-origin-marker]",
        body_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    safe_body = re.sub(
        r"<!--\s*second_brain_origin\b",
        "[stripped-origin-marker]",
        safe_body,
        flags=re.IGNORECASE,
    )
    safe_label = re.sub(r"[\r\n`]+", " ", _scrub_comment_delimiters(str(decrypted.get("pairing_label") or "mobile"))).strip()[:80] or "mobile"
    safe_created_at = _scrub_comment_delimiters(re.sub(r"[\r\n]+", "", str(decrypted["created_at"])))[:64]
    safe_title = title or (safe_url[:120] if safe_url else f"Mobile Quick-Capture ({decrypted['drop_id'][:8]})")

    lines = [
        "<!-- second_brain_origin: web_remote -->",
        f"<!-- mobile_inbox_drop_id: {decrypted['drop_id']} device_token: {decrypted['token_id']} captured_at: {safe_created_at} -->",
    ]
    if risks:
        lines.append(f"<!-- quarantine_risk_flags: {', '.join(risks)} -->")
    lines.append("")
    lines.append(f"# {safe_title}")
    lines.append("")
    if safe_url:
        lines.append(f"- **Source URL:** {safe_url}")
    lines.append(f"- **Captured From:** Mobile E2EE Drop (`{safe_label}`)")
    if clean_tags:
        lines.append(f"- **Tags:** {', '.join(clean_tags)}")
    lines.append("")
    if safe_body:
        lines.append(safe_body)
        lines.append("")
    return "\n".join(lines), risks


def stage_decrypted_drop_to_inbox(
    decrypted: dict[str, Any],
    *,
    brain_dir: str | Path,
) -> dict[str, Any]:
    """Atomically stage one authenticated mobile drop strictly into `brain/0-raw/inbox/`."""
    active_brain = Path(brain_dir).resolve()
    inbox_dir = active_brain / "0-raw" / "inbox"
    _assert_no_symlinks(inbox_dir, stop_at=active_brain)
    inbox_dir.mkdir(parents=True, exist_ok=True)

    rendered_md, risks = _render_quarantined_inbox_markdown(decrypted)
    drop_id = str(decrypted["drop_id"])
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    base_name = f"mobile-drop-{stamp}-{drop_id[:8]}.md"
    dest_str = safe_file_in_dir(str(inbox_dir), base_name, allowed_exts={".md"}, must_exist=False)
    if not dest_str:
        raise InboxDropSecurityError("Invalid inbox destination filename.")

    payload_bytes = rendered_md.encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    attempt = 0
    while True:
        cand_name = base_name if attempt == 0 else f"mobile-drop-{stamp}-{drop_id[:8]}-{attempt}.md"
        dest_str = safe_file_in_dir(str(inbox_dir), cand_name, allowed_exts={".md"}, must_exist=False)
        if not dest_str:
            raise InboxDropSecurityError("Invalid inbox destination filename.")
        try:
            fd = os.open(dest_str, flags, 0o600)
            break
        except FileExistsError:
            attempt += 1
            if attempt > 50:
                raise
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload_bytes)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        if os.path.exists(dest_str):
            try:
                os.unlink(dest_str)
            except OSError:
                pass
        raise

    return {
        "drop_id": drop_id,
        "inbox_filename": os.path.basename(dest_str),
        "rel_path": f"0-raw/inbox/{os.path.basename(dest_str)}",
        "sha256": content_sha256(payload_bytes),
        "risk_flags": risks,
        "origin": "web_remote",
    }


def pull_inbox_drops_from_backend(
    backend: Any,
    *,
    brain_dir: str | Path,
    state_dir: str | Path | None = None,
    delete_consumed: bool = True,
    _writer_lock_held: bool = False,
) -> dict[str, Any]:
    """List, authenticate, decrypt, deduplicate, and stage `inbox-drops/*.enc` into `0-raw/inbox/`."""
    active_brain = Path(brain_dir).resolve()
    state = _load_drop_state(active_brain, state_dir=state_dir)
    if not isinstance(state, dict) or not state.get("privkey_pkcs8_b64"):
        return {
            "enabled": False,
            "ingested_count": 0,
            "skipped_duplicate_count": 0,
            "rejected_count": 0,
            "ingested": [],
            "rejected": [],
        }

    try:
        raw_keys = backend.list_objects("inbox-drops")
    except Exception as exc:
        return {
            "enabled": True,
            "ingested_count": 0,
            "skipped_duplicate_count": 0,
            "rejected_count": 0,
            "ingested": [],
            "rejected": [],
            "list_warning": str(exc)[:300],
        }

    matched_candidates: list[tuple[str, str]] = []
    for k in sorted(raw_keys):
        m = _DROP_KEY_RE.fullmatch(str(k or "").strip())
        if m:
            matched_candidates.append((str(k).strip(), m.group(1)))

    if not matched_candidates:
        return {
            "enabled": True,
            "ingested_count": 0,
            "skipped_duplicate_count": 0,
            "rejected_count": 0,
            "ingested": [],
            "rejected": [],
        }

    def _run_under_lock() -> dict[str, Any]:
        processed_list, rejected_list = _load_processed_drop_ledger(active_brain)
        processed_set = set(processed_list)
        rejected_set = set(rejected_list)
        ingested: list[dict[str, Any]] = []
        rejected: list[dict[str, str]] = []
        skipped_dups = 0
        cleanup_budget = MAX_SKIPPED_CLEANUP_PER_CYCLE

        worklist: list[tuple[str, str]] = []
        for obj_key, drop_id in matched_candidates:
            if drop_id in processed_set or drop_id in rejected_set:
                if drop_id in processed_set:
                    skipped_dups += 1
                if delete_consumed and cleanup_budget > 0:
                    cleanup_budget -= 1
                    try:
                        backend.delete_object(obj_key)
                    except Exception:
                        pass
                continue
            if len(worklist) < MAX_DROPS_PER_CYCLE:
                worklist.append((obj_key, drop_id))

        from vault_sync import SyncSecurityError

        for obj_key, drop_id in worklist:
            try:
                ct_bytes = backend.get_object(obj_key, max_bytes=MAX_DROP_ENVELOPE_BYTES)
            except SyncSecurityError as exc:
                rejected.append({"key": obj_key, "drop_id": drop_id, "error": str(exc)[:300]})
                if drop_id not in rejected_set:
                    rejected_list.append(drop_id)
                    rejected_set.add(drop_id)
                    _save_processed_drop_ledger(active_brain, processed_list, rejected_list)
                if delete_consumed:
                    try:
                        backend.delete_object(obj_key)
                    except Exception:
                        pass
                continue
            except Exception as exc:
                rejected.append({"key": obj_key, "drop_id": drop_id, "error": str(exc)[:300]})
                continue
            if ct_bytes is None:
                continue

            try:
                dec = decrypt_inbox_drop_envelope(
                    ct_bytes,
                    expected_drop_id=drop_id,
                    brain_dir=active_brain,
                    state_dir=state_dir,
                )
                staged = stage_decrypted_drop_to_inbox(dec, brain_dir=active_brain)
                processed_list.append(drop_id)
                processed_set.add(drop_id)
                _save_processed_drop_ledger(active_brain, processed_list, rejected_list)
                ingested.append(staged)
                if delete_consumed:
                    try:
                        backend.delete_object(obj_key)
                    except Exception:
                        pass
            except InboxDropSecurityError as exc:
                rejected.append({"key": obj_key, "drop_id": drop_id, "error": str(exc)[:300]})
                if drop_id not in rejected_set:
                    rejected_list.append(drop_id)
                    rejected_set.add(drop_id)
                    _save_processed_drop_ledger(active_brain, processed_list, rejected_list)
                if delete_consumed:
                    try:
                        backend.delete_object(obj_key)
                    except Exception:
                        pass
            except Exception as exc:
                rejected.append({"key": obj_key, "drop_id": drop_id, "error": str(exc)[:300]})

        return {
            "enabled": True,
            "ingested_count": len(ingested),
            "skipped_duplicate_count": skipped_dups,
            "rejected_count": len(rejected),
            "ingested": ingested,
            "rejected": rejected,
        }

    if _writer_lock_held:
        return _run_under_lock()
    lock_path = active_brain / ".sidecar" / "writer.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with review_lock(str(lock_path)):
        return _run_under_lock()
