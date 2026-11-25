# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Human owner attestation for approvals.

A free-form ``--actor owner`` string is not evidence that a person approved a
memory. This module issues short-lived Ed25519 grants after a passphrase check.
The private key is encrypted with that passphrase. Verifiers use only the
public key, so a later process can check a grant without seeing the passphrase.

Two other credentials are accepted when the caller already proved them:

* ``owner_bearer_token`` — the loopback API re-checks ``X-Owner-Token``.
* ``dashboard_password_session`` — the dashboard re-checks the unlocked
  password-verifier fingerprint stored in the Streamlit session.

In-process library calls that omit an attestation are stored as
``unattested_library_call`` with ``human_approval`` false. They still apply
the file change. The CLI, HTTP API, and dashboard do not use that path.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config import security_state_dir
from security_fs import restrict_security_file_permissions

KEY_FILENAME = "owner-approval-key.json"
GRANT_PREFIX = "sbg1"
GRANT_TTL_SECONDS = 15 * 60
PASSPHRASE_MIN_LENGTH = 12
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
_AAD = b"second-brain-owner-approval-v1"
_UNATTRIBUTABLE_ACTOR = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:owner-corpus-seed|corpus-seed|bulk-script|auto-approve|automation)(?:[^a-z0-9]|$)"
)


class OwnerAttestationError(ValueError):
    """The caller did not present a usable human owner credential."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _kdf(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    )


def approval_key_path(state_dir: str | Path | None = None) -> Path:
    base = Path(state_dir).expanduser() if state_dir is not None else security_state_dir()
    return Path(os.path.abspath(str(base))) / KEY_FILENAME


def _read_key_file(path: Path) -> dict:
    if not path.is_file():
        raise OwnerAttestationError(
            "No owner approval key is enrolled. Run `second-brain owner enroll` and keep the passphrase."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OwnerAttestationError(f"Owner approval key is unreadable: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise OwnerAttestationError("Owner approval key has an unsupported schema.")
    return data


def _write_key_file(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    restrict_security_file_permissions(path.parent)
    tmp = path.parent / f".tmp-{path.name}-{secrets.token_hex(4)}"
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    restrict_security_file_permissions(path)


def _public_key(data: dict) -> Ed25519PublicKey:
    try:
        raw = _b64d(str(data["public_key"]))
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception as exc:
        raise OwnerAttestationError("Owner approval public key is invalid.") from exc


def _fingerprint(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:32]


def _unwrap_private(data: dict, passphrase: str) -> Ed25519PrivateKey:
    try:
        salt = _b64d(str(data["kdf_salt"]))
        nonce = _b64d(str(data["private_key_nonce"]))
        wrapped = _b64d(str(data["private_key_wrapped"]))
        key = _kdf(passphrase, salt)
        raw = AESGCM(key).decrypt(nonce, wrapped, _AAD)
        return Ed25519PrivateKey.from_private_bytes(raw)
    except OwnerAttestationError:
        raise
    except Exception as exc:
        raise OwnerAttestationError("Owner passphrase was not accepted.") from exc


def enroll_owner(
    actor: str,
    passphrase: str,
    *,
    state_dir: str | Path | None = None,
    rotate: bool = False,
) -> dict[str, str]:
    """Create an encrypted owner signing key. Refuses to overwrite unless rotate=True."""
    clean_actor = _clean_actor(actor)
    secret = str(passphrase or "")
    if len(secret) < PASSPHRASE_MIN_LENGTH:
        raise OwnerAttestationError(
            f"Owner passphrase must be at least {PASSPHRASE_MIN_LENGTH} characters."
        )
    path = approval_key_path(state_dir)
    if path.exists() and not rotate:
        raise OwnerAttestationError(
            f"An owner approval key already exists at {path}. Pass --rotate to replace it."
        )
    private = Ed25519PrivateKey.generate()
    raw_private = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    raw_public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    wrapped = AESGCM(_kdf(secret, salt)).encrypt(nonce, raw_private, _AAD)
    payload = {
        "schema_version": 1,
        "actor": clean_actor,
        "kdf": "scrypt",
        "kdf_salt": _b64e(salt),
        "public_key": _b64e(raw_public),
        "fingerprint": _fingerprint(raw_public),
        "private_key_nonce": _b64e(nonce),
        "private_key_wrapped": _b64e(wrapped),
        "enrolled_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    _write_key_file(path, payload)
    return {"actor": clean_actor, "fingerprint": payload["fingerprint"], "path": str(path)}


def issue_grant(
    passphrase: str,
    *,
    state_dir: str | Path | None = None,
    ttl_seconds: int = GRANT_TTL_SECONDS,
) -> str:
    """Return a short-lived grant token after the passphrase unwraps the private key."""
    data = _read_key_file(approval_key_path(state_dir))
    private = _unwrap_private(data, passphrase)
    now = int(time.time())
    ttl = max(30, min(int(ttl_seconds), 24 * 60 * 60))
    body = {
        "v": 1,
        "actor": str(data["actor"]),
        "exp": now + ttl,
        "jti": secrets.token_hex(8),
        "fp": str(data["fingerprint"]),
    }
    payload = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = private.sign(payload)
    return f"{GRANT_PREFIX}.{_b64e(payload)}.{_b64e(signature)}"


def verify_grant(token: str, *, state_dir: str | Path | None = None) -> dict[str, str]:
    """Verify a grant signature and expiry. Does not require the passphrase."""
    text = str(token or "").strip()
    parts = text.split(".")
    if len(parts) != 3 or parts[0] != GRANT_PREFIX:
        raise OwnerAttestationError("Owner grant is missing or malformed.")
    try:
        payload = _b64d(parts[1])
        signature = _b64d(parts[2])
        body = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise OwnerAttestationError("Owner grant is malformed.") from exc
    if not isinstance(body, dict) or body.get("v") != 1:
        raise OwnerAttestationError("Owner grant has an unsupported version.")
    data = _read_key_file(approval_key_path(state_dir))
    if str(body.get("fp") or "") != str(data.get("fingerprint") or ""):
        raise OwnerAttestationError("Owner grant was not signed by the enrolled key.")
    try:
        _public_key(data).verify(signature, payload)
    except Exception as exc:
        raise OwnerAttestationError("Owner grant signature was rejected.") from exc
    try:
        exp = int(body["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OwnerAttestationError("Owner grant expiry is invalid.") from exc
    if exp < int(time.time()):
        raise OwnerAttestationError("Owner grant has expired. Run `second-brain owner grant` again.")
    actor = _clean_actor(str(body.get("actor") or ""))
    return {
        "method": "owner_ed25519_grant",
        "actor": actor,
        "credential_fingerprint": str(data["fingerprint"]),
        "grant_id": str(body.get("jti") or "")[:64],
        "grant": text,
    }


def _clean_actor(actor: str) -> str:
    clean = str(actor or "").strip()[:120]
    if not clean or _UNATTRIBUTABLE_ACTOR.search(clean):
        raise OwnerAttestationError(
            "This actor name cannot be recorded as a human approval. "
            "Enroll a person with `second-brain owner enroll`."
        )
    return clean


def unattested_library_attestation(actor: str) -> tuple[str, dict[str, str]]:
    """Record a library call that did not present an owner credential.

    The actor string is kept for the test or tool that made the change. The
    method says it was not a human approval.
    """
    clean = _clean_actor(actor)
    return clean, {
        "method": "unattested_library_call",
        "actor": clean,
        "human_approval": "false",
    }


def public_attestation(attestation: dict | None) -> dict[str, str] | None:
    """Return the fields safe to store in an audit log. Drops secrets."""
    if not attestation:
        return None
    method = str(attestation.get("method") or "")[:64]
    if not method:
        return None
    out = {"method": method}
    actor = str(attestation.get("actor") or "").strip()[:120]
    if actor:
        out["actor"] = actor
    fp = str(attestation.get("credential_fingerprint") or "")[:64]
    if fp:
        out["credential_fingerprint"] = fp
    grant_id = str(attestation.get("grant_id") or "")[:64]
    if grant_id:
        out["grant_id"] = grant_id
    return out


def require_attestation(
    attestation: dict | None,
    *,
    actor: str,
    state_dir: str | Path | None = None,
    auth_path: str | Path | None = None,
) -> dict[str, str]:
    """Verify an attestation and bind it to ``actor``.

    ``owner_ed25519_grant`` is checked against the enrolled public key.
    ``owner_bearer_token`` is checked against the owner token file.
    ``dashboard_password_session`` is checked against the dashboard verifier fingerprint.
    """
    if not isinstance(attestation, dict):
        raise OwnerAttestationError(
            "A verified owner credential is required. A --actor string alone is not an approval."
        )
    method = str(attestation.get("method") or "")
    clean_actor = _clean_actor(actor)
    if method == "owner_ed25519_grant":
        verified = verify_grant(str(attestation.get("grant") or ""), state_dir=state_dir)
    elif method == "owner_bearer_token":
        verified = _verify_owner_bearer(attestation)
    elif method == "dashboard_password_session":
        verified = _verify_dashboard_session(attestation, auth_path=auth_path)
    else:
        raise OwnerAttestationError(f"Unsupported owner attestation method: {method or 'missing'}.")
    if verified["actor"] and verified["actor"].casefold() != clean_actor.casefold():
        raise OwnerAttestationError(
            "Actor does not match the owner credential. Approvals record the enrolled identity."
        )
    verified["actor"] = clean_actor
    public = public_attestation(verified)
    if public is None:
        raise OwnerAttestationError("Owner attestation could not be recorded.")
    return public


def _verify_owner_bearer(attestation: dict) -> dict[str, str]:
    token = str(attestation.get("token") or "")
    expected = str(attestation.get("expected_token") or "")
    if not expected:
        token_path = str(attestation.get("token_path") or "").strip()
        from api_product import get_owner_token

        loaded = get_owner_token(Path(token_path) if token_path else None)
        expected = loaded or ""
    from api_product import verify_owner_token

    if not verify_owner_token(token, expected, record_failure=False):
        raise OwnerAttestationError("Owner token was not accepted.")
    actor = _clean_actor(str(attestation.get("actor") or "owner"))
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
    return {
        "method": "owner_bearer_token",
        "actor": actor,
        "credential_fingerprint": fingerprint,
        "grant_id": "",
    }


def _verify_dashboard_session(attestation: dict, *, auth_path: str | Path | None) -> dict[str, str]:
    from dashboard_auth import _record_fingerprint, load_password_record

    record = load_password_record(auth_path)
    if not isinstance(record, dict):
        raise OwnerAttestationError("Dashboard password is not configured.")
    expected = _record_fingerprint(record)
    presented = str(attestation.get("credential_fingerprint") or "")
    if not presented or not hmac.compare_digest(presented, expected):
        raise OwnerAttestationError("Dashboard approval requires the unlocked owner session.")
    actor = _clean_actor(str(attestation.get("actor") or "owner"))
    return {
        "method": "dashboard_password_session",
        "actor": actor,
        "credential_fingerprint": expected[:32],
        "grant_id": "",
    }


def attestation_from_grant(token: str, *, state_dir: str | Path | None = None) -> dict[str, str]:
    verified = verify_grant(token, state_dir=state_dir)
    return verified


def split_grant_args(argv: list[str] | None) -> tuple[list[str], str]:
    """Pull ``--grant`` out of a CLI argv list. ``BRAIN_OWNER_GRANT`` is the fallback."""
    source = list(sys.argv[1:] if argv is None else argv)
    grant = (os.environ.get("BRAIN_OWNER_GRANT") or "").strip()
    cleaned: list[str] = []
    index = 0
    while index < len(source):
        item = source[index]
        if item == "--grant" and index + 1 < len(source):
            grant = source[index + 1].strip()
            index += 2
            continue
        if item.startswith("--grant="):
            grant = item.split("=", 1)[1].strip()
            index += 1
            continue
        cleaned.append(item)
        index += 1
    return cleaned, grant


def _passphrase_from_args(value: str) -> str:
    if value:
        return value
    env = (os.environ.get("BRAIN_OWNER_PASSPHRASE") or "")
    if env:
        return env
    if sys.stdin.isatty():
        return getpass.getpass("Owner passphrase: ")
    raise OwnerAttestationError(
        "Pass --passphrase or set BRAIN_OWNER_PASSPHRASE. Refusing to block on a non-interactive prompt."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="second-brain owner",
        description="Enroll an owner approval key and issue short-lived approval grants.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    enroll = sub.add_parser("enroll", help="Create the encrypted owner signing key.")
    enroll.add_argument("--actor", required=True, help="Name recorded on later approvals.")
    enroll.add_argument("--passphrase", default="", help="Prefer BRAIN_OWNER_PASSPHRASE over a shell history entry.")
    enroll.add_argument("--rotate", action="store_true", help="Replace an existing key.")
    grant = sub.add_parser("grant", help="Print a 15-minute approval grant after the passphrase check.")
    grant.add_argument("--passphrase", default="", help="Prefer BRAIN_OWNER_PASSPHRASE over a shell history entry.")
    grant.add_argument("--ttl-seconds", type=int, default=GRANT_TTL_SECONDS)
    args = parser.parse_args(argv)
    try:
        if args.command == "enroll":
            result = enroll_owner(
                args.actor,
                _passphrase_from_args(args.passphrase),
                rotate=bool(args.rotate),
            )
            print(json.dumps({"status": "enrolled", **result}, indent=2))
            return 0
        token = issue_grant(_passphrase_from_args(args.passphrase), ttl_seconds=args.ttl_seconds)
        print(token)
        return 0
    except OwnerAttestationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
