# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["cryptography", "filelock"]
# ///
"""Portable, Three-Store-Compliant Verifiable Vault Backup & Disaster-Recovery Restore.

Archive format (`.sbvault`):
  `SBVBKP1\\x00` (8B magic) || uint32_be(header_len) || canonical_utf8_json(header) || payload

Modes:
  * Encrypted (default): `scrypt(N=2^15, r=8, p=1)` -> `HKDF-SHA256` -> `AES-256-GCM` with 96-bit random
    nonce and strict AAD binding over the canonical JSON outer header.
  * Unencrypted (`--unencrypted` explicit opt-in): gzipped tar stream + outer `payload_sha256` integrity check.

Three-Store invariants:
  * Included: durable Markdown & source/audit state (`0-raw/`, `1-desk/`, `2-atoms/`, `3-threads/`,
    `4-experiences/`, `5-playbooks/`, `notes/`, `sources/`, `briefings/`) + `backup-manifest.json`.
  * Strictly Excluded: disposable `.sidecar/`, `.chroma/`, `.git/`, runtime locks, SQLite files,
    symlinks/reparse points, and credential/key files (`*.pfx`, `*.p12`, `*.pem`, `*.key`, `.env*`).
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
    _sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__")
    raise SystemExit(0)


import argparse
import base64
from datetime import datetime, timezone
import getpass
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import struct
import sys
import tarfile
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


from scripts.atom_mutator import atomic_write_bytes, invalidate_decision_log_cache, review_lock  # noqa: E402
from scripts.config import _is_immutable_darwin_root_symlink, _is_reparse_or_link, resolve_active_brain_dir  # noqa: E402
from scripts.host_adapters import restrict_security_file_permissions  # noqa: E402
from scripts.index_manifest import invalidate_atom_revision_cache  # noqa: E402
from scripts.lexical_store import rebuild_lexical_index  # noqa: E402
from scripts.notes_store import NotePaths  # noqa: E402
from scripts.vault_ledger import (  # noqa: E402
    _build_merkle_levels,
    _leaf_digest,
    compute_vault_merkle,
    verify_vault_ledger,
)


BACKUP_MAGIC = b"SBVBKP1\x00"
BACKUP_SCHEMA_VERSION = 1
MIN_PASSPHRASE_LEN = 8
SCRYPT_N = 32768
SCRYPT_R = 8
SCRYPT_P = 1
MAX_BACKUP_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXTRACTED_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_SINGLE_MEMBER_BYTES = 256 * 1024 * 1024
MANIFEST_MEMBER_NAME = "backup-manifest.json"

DURABLE_TOP_DIRS: tuple[str, ...] = (
    "0-raw",
    "1-desk",
    "2-atoms",
    "3-threads",
    "4-experiences",
    "5-playbooks",
    "6-skills",
    "7-principles",
    "8-mental-models",
    "notes",
    "sources",
    "briefings",
)

ALLOWED_TOP_FILES: frozenset[str] = frozenset({
    "house-rules.md",
})

FORBIDDEN_DIR_NAMES: frozenset[str] = frozenset({
    ".sidecar",
    ".chroma",
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "sync-conflicts",
})

FORBIDDEN_FILE_SUFFIXES: tuple[str, ...] = (
    ".lock",
    ".sqlite3",
    ".sqlite3-wal",
    ".sqlite3-shm",
    ".pfx",
    ".p12",
    ".pem",
    ".key",
    ".sbvault",
)


class VaultBackupError(RuntimeError):
    """Raised on invalid backup arguments, passphrase mismatch, or corrupt archive."""


class VaultBackupSecurityError(PermissionError):
    """Raised when a symlink/reparse boundary, path traversal, or forbidden file rule is violated."""


def _assert_no_symlink_ancestors(path: Path, *, stop_at: Path | None = None) -> None:
    stop_res = stop_at.resolve() if (stop_at is not None and stop_at.exists()) else None
    for cur in (path, *path.parents):
        if os.path.lexists(str(cur)) and _is_reparse_or_link(str(cur)):
            if not _is_immutable_darwin_root_symlink(cur):
                raise VaultBackupSecurityError(f"Refusing to access backup/vault path via symlink or reparse point: {cur}")
        if stop_res is not None and cur == stop_res:
            break


def _is_forbidden_filename(name: str) -> bool:
    low = name.lower()
    if low == ".env" or low.startswith(".env."):
        return True
    if low.startswith(".tmp-"):
        return True
    return low.endswith(FORBIDDEN_FILE_SUFFIXES)


def _derive_backup_aead_key(passphrase: str, salt: bytes, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> bytes:
    scrypt_kdf = Scrypt(salt=salt, length=32, n=n, r=r, p=p)
    ikm = scrypt_kdf.derive(passphrase.encode("utf-8"))
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"sb-vault-backup-v1|aes-256-gcm",
    )
    return hkdf.derive(ikm)


def _validate_archive_rel_path(rel_path: str) -> str:
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise VaultBackupSecurityError("Empty archive member path.")
    if "\\" in rel_path or "\x00" in rel_path or rel_path.startswith("/") or ":" in rel_path.split("/")[0]:
        raise VaultBackupSecurityError(f"Unsafe archive member path rejected: {rel_path!r}")
    pure = PurePosixPath(rel_path)
    parts = pure.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise VaultBackupSecurityError(f"Path traversal in archive member rejected: {rel_path!r}")
    if rel_path == MANIFEST_MEMBER_NAME:
        return MANIFEST_MEMBER_NAME
    if len(parts) == 1 and rel_path in ALLOWED_TOP_FILES:
        return rel_path
    if parts[0] not in DURABLE_TOP_DIRS:
        raise VaultBackupSecurityError(f"Non-durable or forbidden archive top-level path rejected: {rel_path!r}")
    if any(part in FORBIDDEN_DIR_NAMES for part in parts[:-1]):
        raise VaultBackupSecurityError(f"Forbidden disposable directory in archive rejected: {rel_path!r}")
    if _is_forbidden_filename(parts[-1]):
        raise VaultBackupSecurityError(f"Forbidden secret/sidecar file in archive rejected: {rel_path!r}")
    return "/".join(parts)


def _collect_durable_vault_files(brain_root: Path) -> dict[str, bytes]:
    collected: dict[str, bytes] = {}
    total_bytes = 0
    for top_file in sorted(ALLOWED_TOP_FILES):
        tf_full = brain_root / top_file
        if not os.path.lexists(str(tf_full)):
            continue
        if _is_reparse_or_link(str(tf_full)):
            raise VaultBackupSecurityError(f"Refusing to back up symlinked top-level vault file: {tf_full}")
        if not tf_full.is_file():
            continue
        _validate_archive_rel_path(top_file)
        data = tf_full.read_bytes()
        if len(data) > MAX_SINGLE_MEMBER_BYTES:
            raise VaultBackupError(f"Vault file {top_file} exceeds {MAX_SINGLE_MEMBER_BYTES} bytes limit.")
        total_bytes += len(data)
        if total_bytes > MAX_EXTRACTED_TOTAL_BYTES:
            raise VaultBackupError(f"Total vault size exceeds {MAX_EXTRACTED_TOTAL_BYTES} bytes archive limit.")
        collected[top_file] = data
    for top_name in DURABLE_TOP_DIRS:
        top_dir = brain_root / top_name
        if not os.path.lexists(str(top_dir)):
            continue
        if _is_reparse_or_link(str(top_dir)):
            raise VaultBackupSecurityError(f"Refusing to back up symlinked vault directory: {top_dir}")
        if not top_dir.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(str(top_dir), followlinks=False):
            cur_dir = Path(dirpath)
            if _is_reparse_or_link(str(cur_dir)):
                raise VaultBackupSecurityError(f"Refusing to traverse symlinked subdirectory: {cur_dir}")
            kept_dirs: list[str] = []
            for dname in sorted(dirnames):
                d_full = cur_dir / dname
                if _is_reparse_or_link(str(d_full)):
                    raise VaultBackupSecurityError(f"Refusing to back up symlinked directory inside vault: {d_full}")
                if dname in FORBIDDEN_DIR_NAMES:
                    continue
                kept_dirs.append(dname)
            dirnames[:] = kept_dirs

            for fname in sorted(filenames):
                f_full = cur_dir / fname
                if _is_reparse_or_link(str(f_full)):
                    raise VaultBackupSecurityError(f"Refusing to back up symlinked file inside vault: {f_full}")
                if _is_forbidden_filename(fname):
                    continue
                if not f_full.is_file():
                    continue
                rel_posix = f_full.relative_to(brain_root).as_posix()
                _validate_archive_rel_path(rel_posix)
                data = f_full.read_bytes()
                if len(data) > MAX_SINGLE_MEMBER_BYTES:
                    raise VaultBackupError(f"Vault file {rel_posix} exceeds {MAX_SINGLE_MEMBER_BYTES} bytes limit.")
                total_bytes += len(data)
                if total_bytes > MAX_EXTRACTED_TOTAL_BYTES:
                    raise VaultBackupError(f"Total vault size exceeds {MAX_EXTRACTED_TOTAL_BYTES} bytes archive limit.")
                collected[rel_posix] = data
    return dict(sorted(collected.items()))


def _build_tar_gz_bytes(manifest: dict[str, Any], files_map: dict[str, bytes]) -> bytes:
    raw_buf = io.BytesIO()
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw_buf, compresslevel=6, mtime=0) as gz:
        with tarfile.open(mode="w", fileobj=gz, format=tarfile.PAX_FORMAT) as tf:
            m_info = tarfile.TarInfo(name=MANIFEST_MEMBER_NAME)
            m_info.size = len(manifest_bytes)
            m_info.mtime = 0
            m_info.mode = 0o600
            m_info.uid = 0
            m_info.gid = 0
            m_info.uname = ""
            m_info.gname = ""
            m_info.type = tarfile.REGTYPE
            tf.addfile(m_info, io.BytesIO(manifest_bytes))

            for rel_path, content in sorted(files_map.items()):
                info = tarfile.TarInfo(name=rel_path)
                info.size = len(content)
                info.mtime = 0
                info.mode = 0o600
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.type = tarfile.REGTYPE
                tf.addfile(info, io.BytesIO(content))
    return raw_buf.getvalue()


def create_vault_backup(
    output_path: str | Path,
    *,
    brain_dir: str | Path | None = None,
    passphrase: str | None = None,
    unencrypted: bool = False,
    _skip_ledger_gate_for_pre_restore: bool = False,
) -> dict[str, Any]:
    """Create a verifiable Three-Store `.sbvault` archive from `brain_dir`."""
    active_brain = Path(brain_dir or resolve_active_brain_dir()).resolve()
    if not active_brain.is_dir():
        raise VaultBackupError(f"Vault directory does not exist: {active_brain}")
    _assert_no_symlink_ancestors(active_brain)

    out_raw = str(output_path or "").strip()
    if not out_raw:
        raise VaultBackupError("Output backup path is required.")
    out_file = Path(os.path.abspath(os.path.expanduser(out_raw)))
    if out_file.suffix.lower() != ".sbvault":
        out_file = out_file.with_suffix(out_file.suffix + ".sbvault" if out_file.suffix else ".sbvault")
    _assert_no_symlink_ancestors(out_file)

    try:
        out_file.resolve().relative_to(active_brain)
        inside_vault = True
    except ValueError:
        inside_vault = False
    if inside_vault:
        raise VaultBackupError("Output backup path must be outside the live vault directory.")

    if not unencrypted:
        if not isinstance(passphrase, str) or len(passphrase) < MIN_PASSPHRASE_LEN:
            raise VaultBackupError(
                f"Passphrase of at least {MIN_PASSPHRASE_LEN} characters is required (or pass unencrypted=True explicitly)."
            )
    elif passphrase:
        raise VaultBackupError("Specify either a passphrase or unencrypted=True, not both.")

    sidecar_dir = active_brain / ".sidecar"
    if os.path.lexists(str(sidecar_dir)) and _is_reparse_or_link(str(sidecar_dir)):
        raise VaultBackupSecurityError(f"Refusing to lock symlinked .sidecar directory: {sidecar_dir}")
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    lock_path = sidecar_dir / "writer.lock"

    with review_lock(str(lock_path)):
        files_map = _collect_durable_vault_files(active_brain)
        ledger_rep = verify_vault_ledger(
            brain_dir=str(active_brain),
            atoms_dir=str(active_brain / "2-atoms"),
            playbooks_dir=str(active_brain / "5-playbooks"),
        )
        if not ledger_rep["chain_valid"] and not _skip_ledger_gate_for_pre_restore:
            raise VaultBackupError(
                f"Refusing to create backup with corrupted vault ledger: {'; '.join(ledger_rep['errors'])}"
            )
        merkle = compute_vault_merkle(
            brain_dir=str(active_brain),
            atoms_dir=str(active_brain / "2-atoms"),
            playbooks_dir=str(active_brain / "5-playbooks"),
        )

    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    file_index = {
        rel_p: {
            "sha256": hashlib.sha256(raw_b).hexdigest(),
            "size": len(raw_b),
        }
        for rel_p, raw_b in sorted(files_map.items())
    }
    total_plain_bytes = sum(len(b) for b in files_map.values())
    manifest: dict[str, Any] = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "created_at": created_at,
        "vault_name": active_brain.name,
        "file_count": len(files_map),
        "total_bytes": total_plain_bytes,
        "merkle_root": merkle["merkle_root"],
        "approved_atoms": merkle["approved_atoms"],
        "accepted_playbooks": merkle["accepted_playbooks"],
        "active_skills": merkle.get("active_skills", 0),
        "merkle_leaves": [
            {
                "kind": str(leaf["kind"]),
                "rel_path": str(leaf["rel_path"]),
                "file_sha256": str(leaf["file_sha256"]),
                "leaf_sha256": str(leaf["leaf_sha256"]),
            }
            for leaf in merkle["leaves"]
        ],
        "ledger_chain_valid": bool(ledger_rep["chain_valid"]),
        "ledger_entry_count": int(ledger_rep["entry_count"]),
        "ledger_head_entry_sha256": ledger_rep["head_entry_sha256"],
        "files": file_index,
    }
    tar_gz_bytes = _build_tar_gz_bytes(manifest, files_map)

    if not unencrypted:
        assert isinstance(passphrase, str)
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(12)
        outer_header: dict[str, Any] = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "encrypted": True,
            "cipher": "AES-256-GCM",
            "kdf": "scrypt+hkdf-sha256",
            "scrypt_n": SCRYPT_N,
            "scrypt_r": SCRYPT_R,
            "scrypt_p": SCRYPT_P,
            "salt_b64": base64.b64encode(salt).decode("ascii"),
            "nonce_b64": base64.b64encode(nonce).decode("ascii"),
            "created_at": created_at,
        }
        header_bytes = json.dumps(outer_header, sort_keys=True, separators=(",", ":")).encode("utf-8")
        key = _derive_backup_aead_key(passphrase, salt)
        payload_bytes = AESGCM(key).encrypt(nonce, tar_gz_bytes, header_bytes)
    else:
        outer_header = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "encrypted": False,
            "cipher": "none",
            "payload_sha256": hashlib.sha256(tar_gz_bytes).hexdigest(),
            "created_at": created_at,
        }
        header_bytes = json.dumps(outer_header, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload_bytes = tar_gz_bytes

    archive_bytes = BACKUP_MAGIC + struct.pack(">I", len(header_bytes)) + header_bytes + payload_bytes
    out_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(str(out_file), archive_bytes)
    try:
        restrict_security_file_permissions(out_file)
    except Exception:
        pass

    archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    return {
        "status": "created",
        "backup_path": str(out_file),
        "encrypted": not unencrypted,
        "archive_bytes": len(archive_bytes),
        "archive_sha256": archive_sha256,
        "file_count": len(files_map),
        "total_plain_bytes": total_plain_bytes,
        "merkle_root": merkle["merkle_root"],
        "approved_atoms": merkle["approved_atoms"],
        "accepted_playbooks": merkle["accepted_playbooks"],
        "active_skills": merkle.get("active_skills", 0),
        "ledger_entry_count": int(ledger_rep["entry_count"]),
        "created_at": created_at,
    }


def _unpack_and_verify_archive(
    backup_path: str | Path,
    *,
    passphrase: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes], str]:
    raw_in = str(backup_path or "").strip()
    if not raw_in:
        raise VaultBackupError("Backup archive path is required.")
    bpath = Path(os.path.abspath(os.path.expanduser(raw_in)))
    if not os.path.lexists(str(bpath)):
        raise FileNotFoundError(f"Backup archive not found: {bpath}")
    _assert_no_symlink_ancestors(bpath)
    if not bpath.is_file():
        raise VaultBackupError(f"Backup path is not a regular file: {bpath}")

    st_size = bpath.stat().st_size
    if st_size < len(BACKUP_MAGIC) + 4 or st_size > MAX_BACKUP_ARCHIVE_BYTES:
        raise VaultBackupError(f"Invalid or oversized backup archive ({st_size} bytes).")

    archive_bytes = bpath.read_bytes()
    archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    if archive_bytes[: len(BACKUP_MAGIC)] != BACKUP_MAGIC:
        raise VaultBackupError("Unrecognized backup file header (expected SBVBKP1 magic).")

    hdr_len = struct.unpack(">I", archive_bytes[8:12])[0]
    if hdr_len <= 2 or hdr_len > 65536 or (12 + hdr_len) > len(archive_bytes):
        raise VaultBackupError("Malformed backup outer header length.")
    header_bytes = archive_bytes[12 : 12 + hdr_len]
    payload_bytes = archive_bytes[12 + hdr_len :]

    try:
        outer = json.loads(header_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VaultBackupError(f"Malformed backup outer JSON header: {exc}") from exc
    if not isinstance(outer, dict) or outer.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise VaultBackupError("Unsupported backup outer header schema_version.")

    encrypted = bool(outer.get("encrypted", True))
    if encrypted:
        if not isinstance(passphrase, str) or not passphrase:
            raise VaultBackupError("Passphrase is required to decrypt this encrypted .sbvault archive.")
        try:
            salt = base64.b64decode(str(outer.get("salt_b64") or ""), validate=True)
            nonce = base64.b64decode(str(outer.get("nonce_b64") or ""), validate=True)
            n_param = int(outer.get("scrypt_n", SCRYPT_N))
            r_param = int(outer.get("scrypt_r", SCRYPT_R))
            p_param = int(outer.get("scrypt_p", SCRYPT_P))
        except Exception as exc:
            raise VaultBackupError(f"Invalid KDF parameters in backup header: {exc}") from exc
        if (
            len(salt) != 16
            or len(nonce) != 12
            or n_param < 16384
            or n_param > (1 << 20)
            or (n_param & (n_param - 1)) != 0
            or r_param < 1
            or r_param > 16
            or p_param < 1
            or p_param > 16
            or (128 * n_param * r_param * p_param) > (256 * 1024 * 1024)
        ):
            raise VaultBackupError("Invalid salt/nonce/scrypt work factor in backup header.")
        key = _derive_backup_aead_key(passphrase, salt, n=n_param, r=r_param, p=p_param)
        try:
            tar_gz_bytes = AESGCM(key).decrypt(nonce, payload_bytes, header_bytes)
        except InvalidTag as exc:
            raise VaultBackupError("Decryption failed: wrong passphrase or tampered .sbvault archive.") from exc
    else:
        expected_sha = str(outer.get("payload_sha256") or "")
        actual_sha = hashlib.sha256(payload_bytes).hexdigest()
        if not expected_sha or expected_sha != actual_sha:
            raise VaultBackupError("Unencrypted backup payload SHA-256 mismatch (corrupted or tampered archive).")
        tar_gz_bytes = payload_bytes

    extracted_files: dict[str, bytes] = {}
    manifest: dict[str, Any] | None = None
    total_extracted = 0
    max_tar_stream = MAX_EXTRACTED_TOTAL_BYTES + 16 * 1024 * 1024

    try:
        with gzip.GzipFile(fileobj=io.BytesIO(tar_gz_bytes), mode="rb") as gz:
            tar_bytes = gz.read(max_tar_stream + 1)
        if len(tar_bytes) > max_tar_stream:
            raise VaultBackupError("Archive uncompressed stream exceeds safety limit.")
        with tarfile.open(mode="r:", fileobj=io.BytesIO(tar_bytes)) as tf:
            for member in tf:
                if not member.isfile() or member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise VaultBackupSecurityError(
                        f"Archive contains non-regular/symlink tar member: {member.name!r}"
                    )
                clean_name = _validate_archive_rel_path(member.name)
                if member.size < 0 or member.size > MAX_SINGLE_MEMBER_BYTES:
                    raise VaultBackupError(f"Archive member {clean_name} exceeds size bounds.")
                total_extracted += member.size
                if total_extracted > MAX_EXTRACTED_TOTAL_BYTES:
                    raise VaultBackupError("Archive uncompressed payload exceeds safety limit.")
                fobj = tf.extractfile(member)
                if fobj is None:
                    raise VaultBackupError(f"Could not read archive member {clean_name}.")
                data = fobj.read(MAX_SINGLE_MEMBER_BYTES + 1)
                if len(data) != member.size:
                    raise VaultBackupError(f"Truncated or oversized tar stream for {clean_name}.")

                if clean_name == MANIFEST_MEMBER_NAME:
                    if manifest is not None:
                        raise VaultBackupSecurityError("Duplicate backup-manifest.json in archive.")
                    manifest = json.loads(data.decode("utf-8"))
                else:
                    if clean_name in extracted_files:
                        raise VaultBackupSecurityError(f"Duplicate archive member path rejected: {clean_name}")
                    extracted_files[clean_name] = data
    except (VaultBackupError, VaultBackupSecurityError):
        raise
    except (gzip.BadGzipFile, tarfile.TarError, UnicodeDecodeError, json.JSONDecodeError, EOFError, OSError) as exc:
        raise VaultBackupError(f"Corrupt inner tar.gz backup stream: {exc}") from exc

    if not isinstance(manifest, dict) or manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise VaultBackupError("Missing or invalid backup-manifest.json inside archive.")

    expected_files = manifest.get("files")
    if not isinstance(expected_files, dict):
        raise VaultBackupError("Malformed 'files' map in backup-manifest.json.")

    if set(extracted_files.keys()) != set(expected_files.keys()):
        missing = sorted(set(expected_files.keys()) - set(extracted_files.keys()))
        extra = sorted(set(extracted_files.keys()) - set(expected_files.keys()))
        raise VaultBackupError(
            f"Archive member mismatch against manifest (missing={missing[:5]}, unexpected={extra[:5]})."
        )

    for rel_p, raw_b in extracted_files.items():
        spec = expected_files[rel_p]
        if not isinstance(spec, dict):
            raise VaultBackupError(f"Invalid manifest file entry for {rel_p}.")
        actual_sha = hashlib.sha256(raw_b).hexdigest()
        if actual_sha != spec.get("sha256") or len(raw_b) != int(spec.get("size", -1)):
            raise VaultBackupError(f"SHA-256 or byte-length integrity mismatch for {rel_p}.")

    # Verify Merkle root over manifest leaves & verify every leaf matches actual archived file bytes
    raw_leaves = manifest.get("merkle_leaves")
    if not isinstance(raw_leaves, list):
        raise VaultBackupError("Missing 'merkle_leaves' in backup-manifest.json.")
    leaf_hashes: list[str] = []
    for leaf in raw_leaves:
        if not isinstance(leaf, dict):
            raise VaultBackupError("Malformed Merkle leaf entry in manifest.")
        l_rel = str(leaf.get("rel_path") or "")
        l_fsha = str(leaf.get("file_sha256") or "")
        l_digest = str(leaf.get("leaf_sha256") or "")
        if l_rel not in extracted_files:
            raise VaultBackupError(f"Merkle leaf {l_rel} missing from archived files.")
        if hashlib.sha256(extracted_files[l_rel]).hexdigest() != l_fsha:
            raise VaultBackupError(f"Merkle leaf {l_rel} SHA-256 disagrees with archived file bytes.")
        if _leaf_digest(l_rel, l_fsha) != l_digest:
            raise VaultBackupError(f"Merkle leaf digest mismatch for {l_rel}.")
        leaf_hashes.append(l_digest)

    recomputed_root = _build_merkle_levels(leaf_hashes)[-1][0]
    if recomputed_root != str(manifest.get("merkle_root") or ""):
        raise VaultBackupError(
            f"Merkle root verification failed (expected {manifest.get('merkle_root')}, got {recomputed_root})."
        )

    return outer, manifest, extracted_files, archive_sha256


def verify_vault_backup(
    backup_path: str | Path,
    *,
    passphrase: str | None = None,
) -> dict[str, Any]:
    """Verify `.sbvault` AEAD/checksum, member safety, per-file SHA-256s, and Merkle root in memory."""
    outer, manifest, extracted_files, archive_sha256 = _unpack_and_verify_archive(
        backup_path,
        passphrase=passphrase,
    )
    return {
        "valid": True,
        "backup_path": str(Path(os.path.abspath(os.path.expanduser(str(backup_path))))),
        "encrypted": bool(outer.get("encrypted", True)),
        "cipher": str(outer.get("cipher") or "none"),
        "archive_sha256": archive_sha256,
        "created_at": str(manifest.get("created_at") or ""),
        "vault_name": str(manifest.get("vault_name") or ""),
        "file_count": len(extracted_files),
        "total_bytes": int(manifest.get("total_bytes") or 0),
        "merkle_root": str(manifest.get("merkle_root") or ""),
        "approved_atoms": int(manifest.get("approved_atoms") or 0),
        "accepted_playbooks": int(manifest.get("accepted_playbooks") or 0),
        "ledger_entry_count": int(manifest.get("ledger_entry_count") or 0),
    }


def _vault_has_durable_files(target_dir: Path) -> bool:
    if not target_dir.exists():
        return False
    for top_file in ALLOWED_TOP_FILES:
        if (target_dir / top_file).is_file():
            return True
    for top_name in DURABLE_TOP_DIRS:
        sub = target_dir / top_name
        if not sub.exists():
            continue
        for _dp, _dns, fns in os.walk(str(sub), followlinks=False):
            if any(not _is_forbidden_filename(f) for f in fns):
                return True
    return False


def restore_vault_backup(
    backup_path: str | Path,
    target_brain_dir: str | Path,
    *,
    passphrase: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Verify a `.sbvault` archive, restore durable Three-Store files, and rebuild local FTS5 sidecars."""
    outer, manifest, extracted_files, archive_sha256 = _unpack_and_verify_archive(
        backup_path,
        passphrase=passphrase,
    )
    raw_target = str(target_brain_dir or "").strip()
    if not raw_target:
        raise VaultBackupError("Target vault directory is required for restore.")
    target = Path(os.path.abspath(os.path.expanduser(raw_target)))
    _assert_no_symlink_ancestors(target)

    pre_restore_backup: str | None = None
    had_existing = _vault_has_durable_files(target)
    if had_existing and not force:
        raise VaultBackupError(
            f"Target vault directory is not empty ({target}); pass --force to create a pre-restore safety backup and overwrite."
        )

    if had_existing and force:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safety_path = target.parent / f".pre-restore-{target.name}-{ts}.sbvault"
        safety_res = create_vault_backup(
            safety_path,
            brain_dir=target,
            passphrase=passphrase if bool(outer.get("encrypted", True)) else None,
            unencrypted=not bool(outer.get("encrypted", True)),
            _skip_ledger_gate_for_pre_restore=True,
        )
        pre_restore_backup = str(safety_res["backup_path"])

    target.mkdir(parents=True, exist_ok=True)
    sidecar_dir = target / ".sidecar"
    if os.path.lexists(str(sidecar_dir)) and _is_reparse_or_link(str(sidecar_dir)):
        raise VaultBackupSecurityError(f"Refusing to access symlinked .sidecar in restore target: {sidecar_dir}")
    sidecar_dir.mkdir(parents=True, exist_ok=True)

    with review_lock(str(sidecar_dir / "writer.lock")):
        if had_existing and force:
            for cleanup_name in (*DURABLE_TOP_DIRS, *sorted(ALLOWED_TOP_FILES), ".chroma"):
                sub = target / cleanup_name
                if os.path.lexists(str(sub)):
                    if _is_reparse_or_link(str(sub)):
                        raise VaultBackupSecurityError(f"Refusing to remove symlinked path: {sub}")
                    if sub.is_dir():
                        shutil.rmtree(str(sub))
                    else:
                        sub.unlink()

        for std_sub in (
            "0-raw/inbox",
            "0-raw/archive",
            "0-raw/note-snapshots",
            "1-desk",
            "2-atoms",
            "3-threads",
            "4-experiences",
            "5-playbooks",
            "6-skills",
            "7-principles",
            "8-mental-models",
            "notes",
            "sources",
            "briefings",
            ".sidecar",
        ):
            d_path = target / std_sub
            _assert_no_symlink_ancestors(d_path, stop_at=target)
            d_path.mkdir(parents=True, exist_ok=True)

        for rel_posix, raw_bytes in sorted(extracted_files.items()):
            clean_rel = _validate_archive_rel_path(rel_posix)
            dest = target / Path(*clean_rel.split("/"))
            _assert_no_symlink_ancestors(dest, stop_at=target)
            dest.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(str(dest), raw_bytes)

        # Rebuild disposable SQLite FTS5 projections immediately
        lexical_db = sidecar_dir / "lexical.sqlite3"
        for stale_db in (
            lexical_db,
            Path(f"{lexical_db}-wal"),
            Path(f"{lexical_db}-shm"),
            sidecar_dir / "notes.sqlite3",
            sidecar_dir / "notes.sqlite3-wal",
            sidecar_dir / "notes.sqlite3-shm",
        ):
            if stale_db.exists() and not _is_reparse_or_link(str(stale_db)):
                try:
                    stale_db.unlink()
                except OSError:
                    pass

        invalidate_decision_log_cache()
        invalidate_atom_revision_cache()

        note_paths = NotePaths.for_brain(str(target))
        lex_res = rebuild_lexical_index(str(target / "2-atoms"), str(lexical_db))
        notes_res = rebuild_lexical_index(note_paths.notes_dir, note_paths.index_path)

        ledger_check = verify_vault_ledger(
            brain_dir=str(target),
            atoms_dir=str(target / "2-atoms"),
            playbooks_dir=str(target / "5-playbooks"),
        )
        if not ledger_check["chain_valid"]:
            raise VaultBackupError(
                f"Post-restore ledger verification failed: {'; '.join(ledger_check['errors'])}"
            )
        if ledger_check["live_merkle_root"] != manifest["merkle_root"]:
            raise VaultBackupError(
                f"Post-restore Merkle root mismatch (expected {manifest['merkle_root']}, got {ledger_check['live_merkle_root']})."
            )

    return {
        "status": "restored",
        "backup_path": str(Path(os.path.abspath(os.path.expanduser(str(backup_path))))),
        "target_brain_dir": str(target),
        "encrypted": bool(outer.get("encrypted", True)),
        "archive_sha256": archive_sha256,
        "restored_files": len(extracted_files),
        "merkle_root": str(manifest["merkle_root"]),
        "lexical_indexed_atoms": int(lex_res.get("indexed_count", 0)),
        "indexed_notes": int(notes_res.get("indexed_count", 0)),
        "pre_restore_backup": pre_restore_backup,
    }


def _resolve_cli_passphrase(
    *,
    passphrase_arg: str | None,
    passphrase_env: str | None,
    unencrypted: bool = False,
    confirm: bool = False,
) -> str | None:
    if unencrypted:
        return None
    if passphrase_arg:
        return passphrase_arg
    if passphrase_env:
        val = os.environ.get(passphrase_env, "")
        if not val:
            raise VaultBackupError(f"Environment variable {passphrase_env!r} is empty or unset.")
        return val
    env_default = os.environ.get("BRAIN_BACKUP_PASSPHRASE", "")
    if env_default:
        return env_default
    if not sys.stdin.isatty():
        raise VaultBackupError(
            "Passphrase required in non-interactive mode (pass --passphrase-env VAR, BRAIN_BACKUP_PASSPHRASE, or --unencrypted)."
        )
    pw = getpass.getpass("Backup passphrase: ")
    if confirm:
        pw2 = getpass.getpass("Confirm backup passphrase: ")
        if pw != pw2:
            raise VaultBackupError("Passphrases did not match.")
    return pw


def _peek_backup_is_encrypted(raw_backup_path: str) -> bool:
    bpath = Path(os.path.abspath(os.path.expanduser(str(raw_backup_path or "").strip())))
    _assert_no_symlink_ancestors(bpath)
    if not bpath.is_file():
        return True
    with open(bpath, "rb") as fh:
        raw_head = fh.read(12 + 65536)
    if raw_head[:8] != BACKUP_MAGIC or len(raw_head) < 12:
        return True
    hlen = struct.unpack(">I", raw_head[8:12])[0]
    if hlen <= 2 or hlen > 65536 or len(raw_head) < 12 + hlen:
        return True
    outer_peek = json.loads(raw_head[12 : 12 + hlen].decode("utf-8"))
    if not isinstance(outer_peek, dict):
        return True
    return bool(outer_peek.get("encrypted", True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="second-brain backup",
        description="Create, verify, or restore portable Three-Store .sbvault backups.",
    )
    sub = parser.add_subparsers(dest="subcmd", required=True)

    p_cr = sub.add_parser("create", help="Create an encrypted (or --unencrypted) .sbvault backup archive.")
    p_cr.add_argument("output", help="Destination .sbvault file path (must be outside the vault).")
    p_cr.add_argument("--brain-dir", default=None, help="Source vault path (defaults to active vault).")
    p_cr.add_argument("--passphrase", default=None, help="Passphrase (>= 8 chars; prefer --passphrase-env).")
    p_cr.add_argument("--passphrase-env", default=None, help="Environment variable containing the backup passphrase.")
    p_cr.add_argument("--unencrypted", action="store_true", help="Write checksummed unencrypted archive explicitly.")
    p_cr.add_argument("--json", action="store_true")

    p_vf = sub.add_parser("verify", help="Cryptographically verify a .sbvault backup in memory without extracting.")
    p_vf.add_argument("backup", help="Path to .sbvault archive.")
    p_vf.add_argument("--passphrase", default=None)
    p_vf.add_argument("--passphrase-env", default=None)
    p_vf.add_argument("--unencrypted", action="store_true", help="Verify an unencrypted archive without prompting.")
    p_vf.add_argument("--json", action="store_true")

    p_rs = sub.add_parser("restore", help="Verify and restore a .sbvault archive and rebuild FTS5 indexes.")
    p_rs.add_argument("backup", help="Path to .sbvault archive.")
    p_rs.add_argument("target_dir", help="Destination vault directory to restore into.")
    p_rs.add_argument("--passphrase", default=None)
    p_rs.add_argument("--passphrase-env", default=None)
    p_rs.add_argument("--unencrypted", action="store_true")
    p_rs.add_argument("--force", action="store_true", help="Create a pre-restore safety backup and overwrite non-empty target.")
    p_rs.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.subcmd == "create":
            pw = _resolve_cli_passphrase(
                passphrase_arg=args.passphrase,
                passphrase_env=args.passphrase_env,
                unencrypted=bool(args.unencrypted),
                confirm=True,
            )
            res = create_vault_backup(
                args.output,
                brain_dir=args.brain_dir,
                passphrase=pw,
                unencrypted=bool(args.unencrypted),
            )
            print(
                json.dumps(res, indent=2)
                if args.json
                else f"Created backup {res['backup_path']} ({res['file_count']} files, merkle={res['merkle_root'][:16]}...)"
            )
            return 0

        if args.subcmd == "verify":
            pw = None
            if not args.unencrypted:
                if args.passphrase or args.passphrase_env or os.environ.get("BRAIN_BACKUP_PASSPHRASE"):
                    pw = _resolve_cli_passphrase(
                        passphrase_arg=args.passphrase,
                        passphrase_env=args.passphrase_env,
                        unencrypted=False,
                    )
                elif sys.stdin.isatty():
                    if _peek_backup_is_encrypted(args.backup):
                        pw = getpass.getpass("Backup passphrase: ")
            res = verify_vault_backup(args.backup, passphrase=pw)
            print(
                json.dumps(res, indent=2)
                if args.json
                else f"VALID backup: {res['file_count']} files, encrypted={res['encrypted']}, merkle={res['merkle_root']}"
            )
            return 0

        if args.subcmd == "restore":
            pw = None
            if not args.unencrypted:
                if args.passphrase or args.passphrase_env or os.environ.get("BRAIN_BACKUP_PASSPHRASE"):
                    pw = _resolve_cli_passphrase(
                        passphrase_arg=args.passphrase,
                        passphrase_env=args.passphrase_env,
                        unencrypted=False,
                    )
                elif sys.stdin.isatty():
                    if _peek_backup_is_encrypted(args.backup):
                        pw = getpass.getpass("Backup passphrase: ")
            res = restore_vault_backup(
                args.backup,
                args.target_dir,
                passphrase=pw,
                force=bool(args.force),
            )
            print(
                json.dumps(res, indent=2)
                if args.json
                else f"Restored {res['restored_files']} files to {res['target_brain_dir']} (merkle={res['merkle_root'][:16]}...)"
            )
            return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
