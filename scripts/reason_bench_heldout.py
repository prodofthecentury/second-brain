"""Owner-only held-out sealing, commitment, disjointness, and aggregate writer.

Plaintext held-out items, templates, salt, and seed are never written by these
functions. The passphrase is taken from ``getpass`` on the CLI path only.
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


MAGIC = "SBSEALED"
FORMAT_VERSION = 1
HELD_OUT_VERSION = "h1"
SCRYPT_N = 131072  # 2^17
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 256 * 1024 * 1024
SCHEMA: dict[str, Any] = {
    "item_fields": [
        "id", "template_id", "instance", "suite", "split", "question", "answer",
        "answer_type", "approx_ok", "answerable", "difficulty", "tags", "provenance",
    ],
    "split": "heldout",
    "format_version": FORMAT_VERSION,
}
CLOUD_PATH_COMPONENTS = {
    "dropbox",
    "google drive",
    "my drive",
    "iclouddrive",
    "icloud drive",
}
CLOUD_ENV_VARS = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")


class HeldoutError(RuntimeError):
    """Config or integrity failure. The CLI turns this into exit 2."""


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def schema_sha256() -> str:
    return hashlib.sha256(canonical_json(SCHEMA).encode("utf-8")).hexdigest()


def canonical_jsonl(items: Iterable[dict]) -> bytes:
    rows = sorted((dict(item) for item in items), key=lambda row: str(row.get("id", "")))
    lines = [canonical_json(row) for row in rows]
    text = ("\n".join(lines) + ("\n" if lines else ""))
    return text.encode("utf-8")


def commitment_digest(salt: bytes, payload: bytes) -> str:
    if len(salt) != 32:
        raise HeldoutError("commitment salt must be 32 bytes")
    return hashlib.sha256(salt + payload).hexdigest()


def _scrypt(password: bytes, salt: bytes, n: int) -> bytes:
    if n < SCRYPT_N:
        raise HeldoutError(f"refusing scrypt n < {SCRYPT_N}")
    try:
        return hashlib.scrypt(password, salt=salt, n=n, r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=SCRYPT_MAXMEM)
    except TypeError:
        return hashlib.scrypt(password, salt=salt, n=n, r=SCRYPT_R, p=SCRYPT_P, dklen=32)


def _hkdf(master: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=SHA256(), length=32, salt=None, info=info).derive(master)


def derive_keys(passphrase: str, kdf_salt: bytes, n: int) -> tuple[bytes, bytes]:
    master = _scrypt(passphrase.encode("utf-8"), kdf_salt, n)
    aead_key = _hkdf(master, b"sb-sealed-aead-v1")
    hmac_key = _hkdf(master, b"sb-heldout-hmac-v1")
    return aead_key, hmac_key


def _header(kdf_salt: bytes, nonce: bytes, n: int = SCRYPT_N) -> bytes:
    payload = {
        "magic": MAGIC,
        "format_version": FORMAT_VERSION,
        "heldout_version": HELD_OUT_VERSION,
        "kdf": {
            "name": "scrypt",
            "n": n,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
            "salt_b64": _b64(kdf_salt),
        },
        "aead": "AES-256-GCM",
        "nonce_b64": _b64(nonce),
    }
    return canonical_json(payload).encode("utf-8")


def _b64(raw: bytes) -> str:
    import base64
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    import base64
    return base64.b64decode(text.encode("ascii"))


def seal_bytes(plaintext: bytes, passphrase: str) -> bytes:
    if not passphrase:
        raise HeldoutError("passphrase is empty")
    kdf_salt = os.urandom(32)
    nonce = os.urandom(12)
    header = _header(kdf_salt, nonce)
    aead_key, _hmac_key = derive_keys(passphrase, kdf_salt, SCRYPT_N)
    ciphertext = AESGCM(aead_key).encrypt(nonce, plaintext, header)
    return header + b"\n" + ciphertext


def open_bytes(blob: bytes, passphrase: str) -> tuple[bytes, bytes]:
    """Return ``(plaintext, hmac_key)``. The HMAC key is independent of the AEAD key."""
    if b"\n" not in blob:
        raise HeldoutError("sealed file is missing its header")
    header, ciphertext = blob.split(b"\n", 1)
    try:
        meta = json.loads(header.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HeldoutError("sealed header is not JSON") from exc
    if meta.get("magic") != MAGIC or int(meta.get("format_version") or 0) != FORMAT_VERSION:
        raise HeldoutError("sealed header magic or format_version is not supported")
    if meta.get("aead") != "AES-256-GCM":
        raise HeldoutError("sealed file is not AES-256-GCM")
    kdf = meta.get("kdf") or {}
    n = int(kdf.get("n") or 0)
    if n < SCRYPT_N or int(kdf.get("r") or 0) != SCRYPT_R or int(kdf.get("p") or 0) != SCRYPT_P:
        raise HeldoutError("refusing KDF parameters below the scrypt floor")
    kdf_salt = _unb64(str(kdf.get("salt_b64") or ""))
    nonce = _unb64(str(meta.get("nonce_b64") or ""))
    if len(kdf_salt) != 32 or len(nonce) != 12:
        raise HeldoutError("sealed salt or nonce has the wrong length")
    # Re-encode the header canonically and require it to match the bytes on disk,
    # so a reordered header cannot be treated as authentic AAD.
    expected = _header(kdf_salt, nonce, n)
    if header != expected:
        raise HeldoutError("sealed header is not canonical")
    aead_key, hmac_key = derive_keys(passphrase, kdf_salt, n)
    try:
        plaintext = AESGCM(aead_key).decrypt(nonce, ciphertext, header)
    except Exception as exc:
        raise HeldoutError("decryption failed") from exc
    return plaintext, hmac_key


def prompt_passphrase(*, confirm: bool) -> str:
    if not sys.stdin.isatty():
        raise HeldoutError("held-out sealing needs an interactive TTY")
    first = getpass.getpass("Held-out passphrase: ")
    if not first:
        raise HeldoutError("passphrase is empty")
    if confirm:
        second = getpass.getpass("Repeat held-out passphrase: ")
        if first != second:
            raise HeldoutError("passphrases did not match")
    return first


def repo_root_from(start: Path | None = None) -> Path:
    here = (start or Path(__file__)).resolve()
    for parent in [here, *here.parents]:
        if (parent / "pyproject.toml").is_file() and (parent / "scripts").is_dir():
            return parent
    return here.parents[1]


def _cloud_roots() -> list[Path]:
    roots = []
    for name in CLOUD_ENV_VARS:
        raw = (os.environ.get(name) or "").strip()
        if raw:
            roots.append(Path(raw))
    return roots


def assert_private_path(path: Path, *, repo_root: Path | None = None) -> Path:
    """Abort when a sealed, raw, or temp path sits in a shared or synced tree."""
    resolved = Path(os.path.realpath(path))
    repo = (repo_root or repo_root_from()).resolve()
    banned_roots = [Path("/workspace").resolve(), repo, *_cloud_roots()]
    for root in banned_roots:
        try:
            resolved.relative_to(Path(os.path.realpath(root)))
        except ValueError:
            continue
        raise HeldoutError(f"refusing a held-out path inside {root}")
    parts = {part.casefold() for part in resolved.parts}
    for component in parts:
        if component in CLOUD_PATH_COMPONENTS or component.startswith("onedrive"):
            raise HeldoutError(f"refusing a held-out path inside a cloud-sync folder ({component})")
    return resolved


def seal_to_path(plaintext: bytes, destination: Path, passphrase: str, *, repo_root: Path | None = None) -> None:
    target = assert_private_path(destination, repo_root=repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    blob = seal_bytes(plaintext, passphrase)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(target, flags, 0o600)
    try:
        os.write(fd, blob)
    finally:
        os.close(fd)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


def load_sealed(path: Path, passphrase: str, *, repo_root: Path | None = None) -> tuple[dict, bytes]:
    source = assert_private_path(path, repo_root=repo_root)
    plaintext, hmac_key = open_bytes(source.read_bytes(), passphrase)
    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HeldoutError("sealed payload is not JSON") from exc
    if not isinstance(payload, dict):
        raise HeldoutError("sealed payload must be an object")
    return payload, hmac_key


def item_hmac(hmac_key: bytes, item_id: str) -> str:
    return hmac.new(hmac_key, item_id.encode("utf-8"), hashlib.sha256).hexdigest()


def empty_commitment(*, suite_version: str = "2.2.0") -> dict[str, Any]:
    salt = b"\x00" * 32
    payload = canonical_jsonl([])
    return {
        "heldout_version": HELD_OUT_VERSION,
        "suite_version": suite_version,
        "created": None,
        "counts": {"math": 0, "multihop": 0, "code": 0, "logic": 0},
        "template_counts": {"math": 0, "multihop": 0, "code": 0, "logic": 0},
        "schema_sha256": schema_sha256(),
        "commitment": commitment_digest(salt, payload),
        "empty_placeholder": True,
        "disjointness": {"n_private_templates": 0, "max_jaccard": 0.0, "passed": True},
        "runs_used": 0,
        "retired": False,
    }


def verify_commitment(payload: dict, commitment: dict) -> None:
    salt_hex = str(payload.get("salt_hex") or "")
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError as exc:
        raise HeldoutError("sealed payload salt is not hex") from exc
    items = list(payload.get("items") or [])
    hidden = list(payload.get("hidden_tests") or [])
    corpus = list(payload.get("corpus") or payload.get("notes") or [])
    if commitment.get("empty_placeholder") and not items:
        digest = commitment_digest(salt, canonical_jsonl([]))
    else:
        from scripts.reason_bench import canonical_json

        digest = commitment_digest(salt, canonical_json({"items": items, "hidden_tests": hidden, "corpus": corpus}).encode("utf-8"))
    if digest != str(commitment.get("commitment") or ""):
        raise HeldoutError("held-out commitment does not match the decrypted set")
    if str(commitment.get("schema_sha256") or "") != schema_sha256():
        raise HeldoutError("held-out schema hash does not match this harness")


def _mask_text(text: str) -> list[str]:
    masked = re.sub(r"\d+", "0", text or "")
    masked = re.sub(r"\b[A-Z][a-z]+\b", "Name", masked)
    return re.findall(r"[a-z0-9]+", masked.casefold())


def _fourgrams(words: list[str]) -> set[str]:
    if len(words) < 4:
        return set()
    return {" ".join(words[i:i + 4]) for i in range(len(words) - 3)}


def jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def rendered_fourgrams(text: str) -> set[str]:
    return _fourgrams(_mask_text(text))


def max_jaccard_against(private_texts: Iterable[str], public_texts: Iterable[str]) -> float:
    public = [rendered_fourgrams(text) for text in public_texts]
    worst = 0.0
    for private in private_texts:
        grams = rendered_fourgrams(private)
        for other in public:
            worst = max(worst, jaccard(grams, other))
    return worst


def disjointness_report(private_texts: Iterable[str], public_texts: Iterable[str]) -> dict[str, Any]:
    texts = list(private_texts)
    score = max_jaccard_against(texts, public_texts)
    return {
        "n_private_templates": len(texts),
        "max_jaccard": round(score, 6),
        "passed": score < 0.5,
    }


def load_template_module(source: str, name: str):
    import types
    module = types.ModuleType(name)
    exec(compile(source, f"<sealed:{name}>", "exec"), module.__dict__)
    return module


def write_heldout_aggregate(
    destination: Path,
    payload: dict[str, Any],
    *,
    hmac_key: bytes,
    decrypted_items: list[dict],
) -> None:
    """Write a repo aggregate. Refuse any plaintext question, answer, output, or id."""
    forbidden: set[str] = set()
    for item in decrypted_items:
        for key in ("question", "id", "answer"):
            value = item.get(key)
            if isinstance(value, str) and value:
                forbidden.add(value)
        for note in item.get("notes") or []:
            if isinstance(note, dict):
                claim = str(note.get("claim") or "")
                if claim:
                    forbidden.add(claim)
        hidden = item.get("hidden_tests")
        if isinstance(hidden, str) and hidden:
            forbidden.add(hidden)
    per_item = payload.get("per_item") or {}
    if not isinstance(per_item, dict):
        raise HeldoutError("held-out aggregate per_item must be an object")
    for key, value in per_item.items():
        if not re.fullmatch(r"[0-9a-f]{64}", str(key)):
            raise HeldoutError("held-out aggregate keys must be HMAC-SHA256 hex digests")
        if isinstance(value, str) and value in forbidden:
            raise HeldoutError("held-out aggregate contains plaintext")
    blob = canonical_json(payload)
    for secret in forbidden:
        if secret and secret in blob:
            raise HeldoutError("held-out aggregate contains plaintext")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(blob + "\n", encoding="utf-8")


def public_template_texts(root: Path) -> list[str]:
    texts = []
    templates = root / "bench" / "reasoning" / "templates"
    if not templates.is_dir():
        return texts
    for path in sorted(templates.rglob("*.py")):
        texts.append(path.read_text(encoding="utf-8"))
    suites = root / "bench" / "reasoning" / "suites"
    if suites.is_dir():
        for path in sorted(suites.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                question = row.get("question") or row.get("text")
                if isinstance(question, str):
                    texts.append(question)
    return texts


PassphraseFn = Callable[[], str]
