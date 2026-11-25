# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["cryptography", "filelock", "httpx"]
# ///
"""Full On-Device Mobile Vault Client Pairing & Deterministic Bundle Exporter.

Pairs iOS / Android / Offline PWA mobile vault clients (`mobile/vault-client/`) to a
configured Zero-Knowledge E2EE vault sync target (`scripts/vault_sync.py`):

  * Zero private signing key exposure: exports ONLY `vault_id`, sync target metadata,
    KDF salt/parameters, and pinned peer `Ed25519` public keys — NEVER host private
    `Ed25519` signing keys or full-bucket write credentials.
  * Restricted `0600` / NTFS DACL pairing registry in
    `SecondBrain/security/mobile-vault-clients.json` with per-device revocation.
  * Optional bundled `SBID1` forward-secret ECDH P-256 Inbox Drop pairing so mobile
    captures route strictly into quarantined `0-raw/inbox/`.
  * Deterministic `second-brain-mobile-vault-client.zip` bundle exporter rejecting
    symlinks/reparse points fail-closed.
"""



from __future__ import annotations

if __name__ == "__main__" and not __package__:
    import runpy as _sb_runpy
    import sys as _sb_sys
    from pathlib import Path as _sb_Path

    _sb_repo = _sb_Path(__file__).resolve().parents[1]
    _sb_here = _sb_Path(__file__).resolve().parent
    if _sb_Path(_sb_sys.path[0]).resolve() == _sb_here:
        _sb_sys.path[0] = str(_sb_repo)
    elif str(_sb_repo) not in _sb_sys.path:
        _sb_sys.path.insert(0, str(_sb_repo))
    raise SystemExit(_sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__"))


import argparse
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import threading
from typing import Any
from urllib.parse import urlsplit
import zipfile

from filelock import FileLock


from scripts.atom_mutator import atomic_write_bytes  # noqa: E402
from scripts.config import _is_immutable_darwin_root_symlink, _is_reparse_or_link, resolve_active_brain_dir, security_state_dir  # noqa: E402
from scripts.host_adapters import read_bounded_security_file, restrict_security_file_permissions  # noqa: E402
from scripts.inbox_drop import create_mobile_drop_pairing, revoke_mobile_drop_pairing  # noqa: E402
from scripts.vault_sync import _resolve_master_key, sync_config_path  # noqa: E402


MOBILE_VAULT_PAIRING_VERSION = 1
REGISTRY_FILENAME = "mobile-vault-clients.json"
MAX_REGISTRY_BYTES = 524_288
BUNDLE_ZIP_FILENAME = "second-brain-mobile-vault-client.zip"
BUNDLE_ALLOWED_ROOT_ITEMS: tuple[str, ...] = (
    "README.md",
    "android",
    "capacitor.config.json",
    "ios",
    "package.json",
    "www",
)
BUNDLE_EXCLUDED_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".gradle",
        ".DS_Store",
        "__pycache__",
        "node_modules",
        "Pods",
        "build",
        "DerivedData",
        "gradle-wrapper.jar",
    }
)
BUNDLE_EXCLUDED_REL_PREFIXES: tuple[str, ...] = (
    "android/app/src/main/assets/public/",
    "ios/App/App/public/",
)

_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")
_HEX_VAULT_ID_RE = re.compile(r"^(?:[0-9a-f]{2}){8,32}$")
_REGISTRY_LOCK = threading.RLock()


class MobileVaultError(RuntimeError):
    """Raised on invalid mobile vault pairing arguments or missing sync config."""


class MobileVaultSecurityError(PermissionError):
    """Raised fail-closed on symlink traversal, unsafe URLs, or key boundary violations."""


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _resolve_security_base(state_dir: str | Path | None = None) -> Path:
    if state_dir is not None:
        return Path(os.path.abspath(os.path.expanduser(str(state_dir))))
    return security_state_dir()


def mobile_vault_registry_path(*, state_dir: str | Path | None = None) -> Path:
    return _resolve_security_base(state_dir) / REGISTRY_FILENAME


def _mobile_vault_registry_lock_path(*, state_dir: str | Path | None = None) -> Path:
    return _resolve_security_base(state_dir) / f"{REGISTRY_FILENAME}.lock"


def _assert_no_symlinks(path: Path, *, stop_at: Path | None = None) -> None:
    stop_norm = os.path.normcase(os.path.abspath(str(stop_at))) if stop_at is not None else None
    cur = Path(os.path.abspath(str(path)))
    while True:
        if os.path.lexists(str(cur)) and _is_reparse_or_link(str(cur)):
            if not _is_immutable_darwin_root_symlink(cur):
                raise MobileVaultSecurityError(f"Refusing to traverse symlink or reparse point: {cur}")
        if stop_norm is not None and os.path.normcase(str(cur)) == stop_norm:
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent


def _validate_https_or_loopback_url(raw_url: str, *, field_name: str, allow_path_only_base: bool = False) -> str:
    cleaned = str(raw_url or "").strip()
    if allow_path_only_base:
        cleaned = cleaned.split("#", 1)[0]
    cleaned = cleaned.rstrip("/") if not allow_path_only_base else cleaned
    parsed = urlsplit(cleaned)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if (
        not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or scheme not in {"https", "http"}
        or (scheme == "http" and host not in {"127.0.0.1", "localhost", "::1"})
    ):
        raise MobileVaultSecurityError(
            f"{field_name} must use https:// (or loopback http://127.0.0.1 / http://localhost) "
            f"without embedded credentials, query strings, or fragments: {raw_url!r}"
        )
    return cleaned


def _load_registry_unlocked(*, state_dir: str | Path | None = None) -> dict[str, Any]:
    reg_path = mobile_vault_registry_path(state_dir=state_dir)
    if not os.path.lexists(str(reg_path)):
        return {"schema_version": 1, "clients": []}
    _assert_no_symlinks(reg_path)
    st = os.lstat(str(reg_path))
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_REGISTRY_BYTES:
        raise MobileVaultSecurityError(f"Invalid or oversized mobile vault pairing registry: {reg_path}")
    raw_text = read_bounded_security_file(reg_path, max_bytes=MAX_REGISTRY_BYTES)
    data = json.loads(raw_text)
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("clients"), list):
        raise MobileVaultSecurityError(f"Malformed mobile vault pairing registry schema: {reg_path}")
    return data


def _save_registry_unlocked(payload: dict[str, Any], *, state_dir: str | Path | None = None) -> Path:
    reg_path = mobile_vault_registry_path(state_dir=state_dir)
    _assert_no_symlinks(reg_path)
    reg_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    restrict_security_file_permissions(reg_path.parent)

    tmp_path = reg_path.parent / f".tmp-{reg_path.name}-{secrets.token_hex(6)}"
    raw_bytes = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(str(tmp_path), flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name != "nt":
            os.chmod(str(tmp_path), 0o600)
        os.replace(str(tmp_path), str(reg_path))
        restrict_security_file_permissions(reg_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return reg_path


def _load_vault_sync_config_safe(brain_dir: Path, *, state_dir: str | Path | None = None) -> dict[str, Any]:
    sec_dir = _resolve_security_base(state_dir) if state_dir is not None else None
    cfg_path = sync_config_path(brain_dir, state_dir=sec_dir)
    if not os.path.lexists(str(cfg_path)):
        raise MobileVaultError(
            "Vault E2EE sync is not initialized for this vault. Run `second-brain sync init` first."
        )
    _assert_no_symlinks(cfg_path)
    raw = read_bounded_security_file(cfg_path, max_bytes=65_536)
    cfg = json.loads(raw)
    if not isinstance(cfg, dict) or int(cfg.get("schema_version", 0)) != 1:
        raise MobileVaultSecurityError("Invalid or unsupported vault sync configuration schema.")
    return cfg


def create_mobile_vault_pairing(
    *,
    brain_dir: str | Path | None = None,
    label: str = "mobile-client",
    include_passphrase_hint: str = "",
    sync_endpoint_override: str | None = None,
    s3_read_access_key_id: str = "",
    s3_read_secret_access_key: str = "",
    include_recovery_key: bool = False,
    allow_bidirectional: bool = False,
    peer_device_id: str | None = None,
    peer_pubkey_b64: str | None = None,
    include_inbox_drop_pairing: bool = True,
    pwa_base_url: str = "https://localhost/vault-client/",
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Create a zero-knowledge `#vaultpair=` mobile pairing bundle (optionally bidirectional with peer pubkey pinning)."""
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    _assert_no_symlinks(active_brain)
    cfg = _load_vault_sync_config_safe(active_brain, state_dir=state_dir)

    vault_id = str(cfg.get("vault_id") or "").strip().lower()
    if not _HEX_VAULT_ID_RE.fullmatch(vault_id):
        raise MobileVaultSecurityError("Malformed vault_id in vault sync configuration.")

    clean_label = re.sub(r"\s+", " ", str(label or "mobile-client")).strip()[:80] or "mobile-client"
    clean_label = clean_label.replace("<!--", "").replace("-->", "").strip() or "mobile-client"
    clean_hint = re.sub(r"[\r\n\t]+", " ", str(include_passphrase_hint or "")).strip()[:120]

    ak = str(s3_read_access_key_id or "").strip()
    sk = str(s3_read_secret_access_key or "").strip()
    if bool(ak) != bool(sk):
        raise MobileVaultError("Provide both s3_read_access_key_id and s3_read_secret_access_key, or omit both.")

    backend_type = str(cfg.get("backend_type") or "http_object")
    if sync_endpoint_override is not None and str(sync_endpoint_override).strip():
        endpoint_url = _validate_https_or_loopback_url(
            str(sync_endpoint_override),
            field_name="sync_endpoint_override",
        )
    elif backend_type == "http_object":
        endpoint_url = _validate_https_or_loopback_url(
            str(cfg.get("target") or ""),
            field_name="sync target endpoint_url",
        )
    else:
        endpoint_url = ""

    clean_pwa_base = _validate_https_or_loopback_url(
        pwa_base_url or "https://localhost/vault-client/",
        field_name="pwa_base_url",
        allow_path_only_base=True,
    )
    if not clean_pwa_base.endswith("/"):
        clean_pwa_base += "/"

    # Extract ONLY public Ed25519 peer verification keys — NEVER export device_privkey_b64!
    raw_peers = cfg.get("trusted_peers") or {}
    trusted_peers: dict[str, str] = {}
    if isinstance(raw_peers, dict):
        for dev_id, pub_b64 in sorted(raw_peers.items()):
            if isinstance(dev_id, str) and isinstance(pub_b64, str) and dev_id and pub_b64:
                trusted_peers[dev_id] = pub_b64
    host_dev_id = str(cfg.get("device_id") or "").strip()
    host_pub_b64 = str(cfg.get("device_pubkey_b64") or "").strip()
    if host_dev_id and host_pub_b64:
        trusted_peers.setdefault(host_dev_id, host_pub_b64)

    kdf_mode = str(cfg.get("kdf_mode") or "keyfile")
    header_kdf_mode = "passphrase" if kdf_mode == "passphrase" else "keyfile"
    kdf_salt_b64 = cfg.get("kdf_salt_b64") if header_kdf_mode == "passphrase" else None

    recovery_key_b64: str | None = None
    if include_recovery_key:
        if header_kdf_mode == "passphrase":
            raise MobileVaultError(
                "Passphrase-encrypted vaults derive keys via scrypt on device; omit include_recovery_key."
            )
        sec_dir = _resolve_security_base(state_dir) if state_dir is not None else None
        mk = _resolve_master_key(cfg, state_dir=sec_dir)
        recovery_key_b64 = base64.b64encode(mk).decode("ascii")

    drop_bundle: dict[str, Any] | None = None
    drop_token_id: str | None = None
    if include_inbox_drop_pairing and len(vault_id) == 32:
        drop_state_dir = _resolve_security_base(state_dir) if state_dir is not None else None
        drop_res = create_mobile_drop_pairing(
            label=f"{clean_label} (Drop)",
            vault_id=vault_id,
            sync_target_url=endpoint_url,
            s3_region=str(cfg.get("s3_region") or "auto"),
            brain_dir=active_brain,
            state_dir=drop_state_dir,
        )
        drop_bundle = dict(drop_res["pairing_bundle"])
        drop_token_id = str(drop_res["token_id"])

    pairing_id = secrets.token_hex(8)
    created_at = _utc_now_iso()
    auth_mode = "s3_sigv4" if (ak and sk) else str(cfg.get("auth_mode") or "none")

    client_bundle: dict[str, Any] = {
        "v": MOBILE_VAULT_PAIRING_VERSION,
        "pairing_id": pairing_id,
        "label": clean_label,
        "vault_id": vault_id,
        "backend_type": "http_object" if endpoint_url else backend_type,
        "endpoint_url": endpoint_url,
        "auth_mode": auth_mode,
        "s3_region": str(cfg.get("s3_region") or "auto"),
        "kdf_mode": header_kdf_mode,
        "kdf_salt_b64": kdf_salt_b64,
        "trusted_peers": trusted_peers,
        "created_at": created_at,
    }
    if clean_hint:
        client_bundle["passphrase_hint"] = clean_hint
    if ak and sk:
        client_bundle["s3_access_key_id"] = ak
        client_bundle["s3_secret_access_key"] = sk
    if recovery_key_b64:
        client_bundle["recovery_key_b64"] = recovery_key_b64
    client_bundle["allow_bidirectional"] = bool(allow_bidirectional)
    if drop_bundle is not None:
        client_bundle["inbox_drop_pairing"] = drop_bundle

    if peer_device_id and peer_pubkey_b64:
        register_mobile_peer_pubkey(
            brain_dir=active_brain,
            device_id=peer_device_id,
            device_pubkey_b64=peer_pubkey_b64,
            state_dir=state_dir,
        )
        trusted_peers[peer_device_id] = peer_pubkey_b64
        client_bundle["trusted_peers"] = trusted_peers

    # Defense-in-depth assertion: private signing key material MUST NEVER appear in client_bundle
    if "device_privkey_b64" in client_bundle or "privkey_pkcs8_b64" in json.dumps(client_bundle):
        raise MobileVaultSecurityError("CRITICAL: Refusing to export private key material in mobile pairing.")

    registry_record = {
        "pairing_id": pairing_id,
        "vault_id": vault_id,
        "brain_dir": str(active_brain),
        "label": clean_label,
        "backend_type": client_bundle["backend_type"],
        "endpoint_url": endpoint_url,
        "kdf_mode": header_kdf_mode,
        "included_read_only_s3_creds": bool(ak and sk),
        "included_recovery_key": bool(recovery_key_b64),
        "drop_token_id": drop_token_id,
        "trusted_peer_count": len(trusted_peers),
        "created_at": created_at,
        "revoked": False,
        "revoked_at": None,
    }

    lock_file = _mobile_vault_registry_lock_path(state_dir=state_dir)
    _assert_no_symlinks(lock_file)
    lock_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    restrict_security_file_permissions(lock_file.parent)

    with _REGISTRY_LOCK, FileLock(str(lock_file), timeout=30):
        reg = _load_registry_unlocked(state_dir=state_dir)
        reg["clients"].append(registry_record)
        reg["updated_at"] = created_at
        reg_path = _save_registry_unlocked(reg, state_dir=state_dir)

    fragment_tok = _b64url_encode(
        json.dumps(client_bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    pairing_fragment = f"#vaultpair={fragment_tok}"
    pwa_url = f"{clean_pwa_base}{pairing_fragment}"

    return {
        "status": "paired",
        "pairing_id": pairing_id,
        "vault_id": vault_id,
        "label": clean_label,
        "backend_type": client_bundle["backend_type"],
        "endpoint_url": endpoint_url,
        "kdf_mode": header_kdf_mode,
        "trusted_peers": trusted_peers,
        "drop_token_id": drop_token_id,
        "created_at": created_at,
        "registry_path": str(reg_path),
        "pairing_bundle": client_bundle,
        "pairing_fragment": pairing_fragment,
        "pwa_pairing_url": pwa_url,
    }


def list_mobile_vault_pairings(
    *,
    brain_dir: str | Path | None = None,
    vault_id: str | None = None,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """List registered mobile vault client pairings without exposing secrets."""
    filter_vault_id = (vault_id or "").strip().lower() or None
    if filter_vault_id is None and brain_dir is not None:
        active_brain = Path(brain_dir).resolve()
        try:
            cfg = _load_vault_sync_config_safe(active_brain, state_dir=state_dir)
            filter_vault_id = str(cfg.get("vault_id") or "").strip().lower() or None
        except MobileVaultError:
            filter_vault_id = None

    with _REGISTRY_LOCK:
        reg = _load_registry_unlocked(state_dir=state_dir)

    items: list[dict[str, Any]] = []
    for raw in reg.get("clients") or []:
        if not isinstance(raw, dict):
            continue
        if filter_vault_id and str(raw.get("vault_id") or "").lower() != filter_vault_id:
            continue
        items.append(
            {
                "pairing_id": str(raw.get("pairing_id") or ""),
                "vault_id": str(raw.get("vault_id") or ""),
                "label": str(raw.get("label") or ""),
                "backend_type": str(raw.get("backend_type") or ""),
                "endpoint_url": str(raw.get("endpoint_url") or ""),
                "kdf_mode": str(raw.get("kdf_mode") or ""),
                "drop_token_id": raw.get("drop_token_id"),
                "trusted_peer_count": int(raw.get("trusted_peer_count", 0)),
                "created_at": str(raw.get("created_at") or ""),
                "revoked": bool(raw.get("revoked", False)),
                "revoked_at": raw.get("revoked_at"),
            }
        )

    return {
        "registry_path": str(mobile_vault_registry_path(state_dir=state_dir)),
        "active_count": sum(1 for x in items if not x["revoked"]),
        "total_count": len(items),
        "pairings": items,
    }


def revoke_mobile_vault_pairing(
    pairing_id: str,
    *,
    brain_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Revoke a mobile vault pairing in `mobile-vault-clients.json` (and its bundled SBID1 drop token)."""
    clean_pid = str(pairing_id or "").strip().lower()
    if not _HEX16_RE.fullmatch(clean_pid):
        raise MobileVaultError(f"Invalid 16-char hex pairing_id: {pairing_id!r}")

    lock_file = _mobile_vault_registry_lock_path(state_dir=state_dir)
    _assert_no_symlinks(lock_file)
    lock_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    restrict_security_file_permissions(lock_file.parent)

    revoked_at = _utc_now_iso()
    matched_entry: dict[str, Any] | None = None

    with _REGISTRY_LOCK, FileLock(str(lock_file), timeout=30):
        reg = _load_registry_unlocked(state_dir=state_dir)
        for item in reg.get("clients") or []:
            if isinstance(item, dict) and str(item.get("pairing_id") or "").lower() == clean_pid:
                item["revoked"] = True
                item["revoked_at"] = revoked_at
                matched_entry = dict(item)
                break
        if matched_entry is None:
            raise FileNotFoundError(f"Mobile vault pairing not found: {clean_pid}")
        reg["updated_at"] = revoked_at
        _save_registry_unlocked(reg, state_dir=state_dir)

    drop_revoked = False
    drop_tid = matched_entry.get("drop_token_id")
    if isinstance(drop_tid, str) and _HEX16_RE.fullmatch(drop_tid):
        target_brain = Path(brain_dir or matched_entry.get("brain_dir") or resolve_active_brain_dir()).resolve()
        drop_state_dir = _resolve_security_base(state_dir) if state_dir is not None else None
        try:
            revoke_mobile_drop_pairing(drop_tid, brain_dir=target_brain, state_dir=drop_state_dir)
            drop_revoked = True
        except FileNotFoundError:
            drop_revoked = False

    return {
        "status": "revoked",
        "pairing_id": clean_pid,
        "revoked_at": revoked_at,
        "drop_token_revoked": drop_revoked,
    }


def _locate_mobile_vault_source_dir(source_dir: str | Path | None = None) -> Path:
    if source_dir is not None:
        cand = Path(os.path.abspath(os.path.expanduser(str(source_dir))))
        if not cand.is_dir():
            raise MobileVaultError(f"Mobile vault client source directory not found: {cand}")
        return cand
    candidates = [
        Path(__file__).resolve().parent.parent / "mobile" / "vault-client",
        Path(__file__).resolve().parent / "mobile" / "vault-client",
    ]
    found = next((p for p in candidates if p.is_dir()), candidates[0])
    if not found.is_dir():
        raise MobileVaultError(f"Mobile vault client directory not found: {found}")
    return found


def export_mobile_vault_client_bundle(
    out_dir: str | Path,
    *,
    source_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Build a deterministic `second-brain-mobile-vault-client.zip` rejecting symlinks fail-closed."""
    src_root = _locate_mobile_vault_source_dir(source_dir)
    _assert_no_symlinks(src_root)

    dest_root = Path(os.path.abspath(os.path.expanduser(str(out_dir))))
    _assert_no_symlinks(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    zip_path = dest_root / BUNDLE_ZIP_FILENAME
    _assert_no_symlinks(zip_path)

    collected_files: list[tuple[str, Path]] = []
    for root_item_name in BUNDLE_ALLOWED_ROOT_ITEMS:
        item_path = src_root / root_item_name
        if not os.path.lexists(str(item_path)):
            raise MobileVaultError(f"Required mobile vault client component missing: {item_path}")
        if _is_reparse_or_link(str(item_path)):
            raise MobileVaultSecurityError(f"Refusing to bundle symlinked path: {item_path}")
        if item_path.is_file():
            collected_files.append((root_item_name, item_path))
            continue
        if item_path.is_dir():
            for dirpath, dirnames, filenames in os.walk(str(item_path), followlinks=False):
                cur_dir = Path(dirpath)
                if _is_reparse_or_link(str(cur_dir)):
                    raise MobileVaultSecurityError(f"Refusing to traverse symlinked directory: {cur_dir}")
                kept_dirs: list[str] = []
                for dname in sorted(dirnames):
                    d_full = cur_dir / dname
                    if _is_reparse_or_link(str(d_full)):
                        raise MobileVaultSecurityError(f"Refusing to bundle symlinked subdirectory: {d_full}")
                    if dname in BUNDLE_EXCLUDED_NAMES:
                        continue
                    kept_dirs.append(dname)
                dirnames[:] = kept_dirs

                for fname in sorted(filenames):
                    f_full = cur_dir / fname
                    if _is_reparse_or_link(str(f_full)):
                        raise MobileVaultSecurityError(f"Refusing to bundle symlinked file: {f_full}")
                    if fname in BUNDLE_EXCLUDED_NAMES or fname.endswith((".tmp", ".pyc")):
                        continue
                    if not f_full.is_file():
                        raise MobileVaultSecurityError(f"Refusing to bundle non-regular file: {f_full}")
                    rel_posix = f_full.relative_to(src_root).as_posix()
                    if any(rel_posix.startswith(pref) for pref in BUNDLE_EXCLUDED_REL_PREFIXES):
                        continue
                    collected_files.append((rel_posix, f_full))

    collected_files.sort(key=lambda pair: pair[0])
    if not collected_files:
        raise MobileVaultError("No mobile vault client files found to export.")

    tmp_zip = dest_root / f".tmp-mobile-vault-{os.getpid()}-{secrets.token_hex(4)}.zip"
    try:
        with zipfile.ZipFile(tmp_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for rel_posix, fpath in collected_files:
                arcname = f"mobile-vault-client/{rel_posix}"
                zinfo = zipfile.ZipInfo(filename=arcname, date_time=(2026, 1, 1, 0, 0, 0))
                zinfo.compress_type = zipfile.ZIP_DEFLATED
                zinfo.external_attr = 0o644 << 16
                zf.writestr(zinfo, fpath.read_bytes())
        raw_zip = tmp_zip.read_bytes()
        atomic_write_bytes(str(zip_path), raw_zip)
    finally:
        if tmp_zip.exists():
            try:
                tmp_zip.unlink()
            except OSError:
                pass

    return {
        "status": "exported",
        "zip_path": str(zip_path),
        "size_bytes": len(raw_zip),
        "sha256": hashlib.sha256(raw_zip).hexdigest(),
        "file_count": len(collected_files),
        "files": [rel for rel, _ in collected_files],
    }


def register_mobile_peer_pubkey(
    *,
    brain_dir: str | Path | None = None,
    device_id: str,
    device_pubkey_b64: str,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Pin an authenticated mobile device's own Ed25519 PUBLIC key (never private keys) in `trusted_peers`."""
    clean_dev = str(device_id or "").strip()
    if not re.fullmatch(r"^[A-Za-z0-9._:-]{3,64}$", clean_dev):
        raise MobileVaultSecurityError(f"Invalid mobile device_id: {device_id!r}")
    clean_pub = str(device_pubkey_b64 or "").strip()
    try:
        raw_pub = base64.b64decode(clean_pub, validate=True)
    except Exception as exc:
        raise MobileVaultSecurityError(f"Malformed base64 Ed25519 public key: {exc}") from exc
    if len(raw_pub) != 32:
        raise MobileVaultSecurityError("Mobile Ed25519 public key must be exactly 32 raw bytes.")

    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    _assert_no_symlinks(active_brain)
    cfg = _load_vault_sync_config_safe(active_brain, state_dir=state_dir)
    peers = dict(cfg.get("trusted_peers") or {})
    if len(peers) >= 32 and clean_dev not in peers:
        raise MobileVaultError("Maximum trusted peers (32) reached.")
    peers[clean_dev] = clean_pub
    cfg["trusted_peers"] = peers

    sec_base = _resolve_security_base(state_dir) if state_dir is not None else None
    cfg_path = sync_config_path(active_brain, state_dir=sec_base)
    raw_cfg = (json.dumps(cfg, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    atomic_write_bytes(str(cfg_path), raw_cfg)
    restrict_security_file_permissions(cfg_path)
    return {
        "status": "peer_registered",
        "device_id": clean_dev,
        "trusted_peer_count": len(peers),
        "config_path": str(cfg_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Second Brain Full Mobile Vault Client Pairing & Exporter")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pair = sub.add_parser("pair", help="Create a zero-knowledge #vaultpair= mobile pairing")
    p_pair.add_argument("--brain-dir", default=None)
    p_pair.add_argument("--label", default="mobile-client")
    p_pair.add_argument("--hint", default="")
    p_pair.add_argument("--endpoint", default=None)
    p_pair.add_argument("--pwa-url", default="https://localhost/vault-client/")
    p_pair.add_argument("--include-recovery-key", action="store_true")
    p_pair.add_argument("--allow-bidirectional", action="store_true")
    p_pair.add_argument("--peer-device-id", default=None)
    p_pair.add_argument("--peer-pubkey-b64", default=None)
    p_pair.add_argument("--json", action="store_true")

    p_peer = sub.add_parser("register-peer", help="Register a mobile device's Ed25519 public key into trusted_peers")
    p_peer.add_argument("--brain-dir", default=None)
    p_peer.add_argument("--device-id", required=True)
    p_peer.add_argument("--pubkey-b64", required=True)
    p_peer.add_argument("--json", action="store_true")

    p_list = sub.add_parser("list", help="List mobile vault pairings")
    p_list.add_argument("--brain-dir", default=None)
    p_list.add_argument("--json", action="store_true")

    p_rev = sub.add_parser("revoke", help="Revoke a mobile vault pairing ID")
    p_rev.add_argument("pairing_id")
    p_rev.add_argument("--brain-dir", default=None)
    p_rev.add_argument("--json", action="store_true")

    p_exp = sub.add_parser("export-bundle", help="Export deterministic second-brain-mobile-vault-client.zip")
    p_exp.add_argument("--out-dir", required=True)
    p_exp.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "pair":
        res = create_mobile_vault_pairing(
            brain_dir=args.brain_dir,
            label=args.label,
            include_passphrase_hint=args.hint,
            sync_endpoint_override=args.endpoint,
            include_recovery_key=args.include_recovery_key,
            allow_bidirectional=bool(args.allow_bidirectional),
            peer_device_id=args.peer_device_id,
            peer_pubkey_b64=args.peer_pubkey_b64,
            pwa_base_url=args.pwa_url,
        )
        print(json.dumps(res, indent=2) if args.json else res["pwa_pairing_url"])
        return 0
    if args.cmd == "register-peer":
        res = register_mobile_peer_pubkey(
            brain_dir=args.brain_dir,
            device_id=args.device_id,
            device_pubkey_b64=args.pubkey_b64,
        )
        print(json.dumps(res, indent=2) if args.json else f"Registered {res['device_id']}")
        return 0
    if args.cmd == "list":
        res = list_mobile_vault_pairings(brain_dir=args.brain_dir)
        print(json.dumps(res, indent=2) if args.json else f"Active mobile pairings: {res['active_count']}")
        return 0
    if args.cmd == "revoke":
        res = revoke_mobile_vault_pairing(args.pairing_id, brain_dir=args.brain_dir)
        print(json.dumps(res, indent=2) if args.json else f"Revoked {res['pairing_id']}")
        return 0
    if args.cmd == "export-bundle":
        res = export_mobile_vault_client_bundle(args.out_dir)
        print(json.dumps(res, indent=2) if args.json else f"Exported {res['zip_path']} ({res['sha256']})")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
