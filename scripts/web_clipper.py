# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Local-first Browser Web Clipper pairing registry and verifiable web capture engine.

Security invariants:
1. Loopback-only capture over 127.0.0.1 — zero third-party cloud relays.
2. Dedicated least-privilege scoped Clipper tokens (`clipper_*`), stored strictly
   as SHA-256 digests under `SecondBrain/security/clipper-pairings.json` (`0600`/NTFS DACL).
   A Clipper token is write-only capture (`/v1/clipper/{ping,status,clip}`) and can NEVER
   read private atoms/notes or invoke owner governance endpoints.
3. Strict browser extension Origin binding: only explicitly owner-paired
   `chrome-extension://<32-char-id>` or `moz-extension://<uuid>` origins are accepted.
4. Verifiable web provenance: every clip writes an immutable UTF-8 source snapshot under
   `brain/0-raw/note-snapshots/` with exact SHA-256 digest, `source_url`, and `captured_at`.
5. Server-side DLP & prompt-injection screening: clips containing secrets, credentials,
   or forged trust markers are automatically quarantined to `brain/0-raw/inbox/`.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import threading
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import content_sha256, review_lock  # noqa: E402
from config import _is_reparse_or_link, resolve_active_brain_dir, security_state_dir  # noqa: E402
from host_adapters import inspect_and_redact_outbound, restrict_security_file_permissions  # noqa: E402
from inbox_workflow import scan_inbox_payload_risks  # noqa: E402
from notes_store import NotePaths, create_note, slugify  # noqa: E402


CLIPPER_PAIRINGS_FILENAME = "clipper-pairings.json"
CLIPPER_TOKEN_PREFIX = "clipper_"
MAX_CLIP_TITLE_CHARS = 240
MAX_CLIP_URL_CHARS = 2048
MAX_CLIP_QUOTE_CHARS = 32_000
MAX_CLIP_PAGE_TEXT_CHARS = 200_000
MAX_PAIRINGS_FILE_BYTES = 262_144
MAX_ACTIVE_PAIRINGS = 64

_CHROME_EXT_ORIGIN_RE = re.compile(r"^chrome-extension://[a-p]{32}$")
_MOZ_EXT_ORIGIN_RE = re.compile(
    r"^moz-extension://[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_PAIRING_ID_RE = re.compile(r"^clip_[0-9a-f]{12,32}$")
_CLIPPER_LOCK = threading.Lock()
_O_BINARY = getattr(os, "O_BINARY", 0)


class ClipperError(ValueError):
    """Raised when a clipper pairing or capture request is invalid."""


class ClipperSecurityError(PermissionError):
    """Raised when a clipper security boundary (symlink, origin, token) is violated."""


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _clipper_pairings_path(security_dir: str | Path | None = None) -> Path:
    base = Path(security_dir).expanduser().resolve() if security_dir is not None else security_state_dir()
    return base / CLIPPER_PAIRINGS_FILENAME


def validate_extension_origin(origin: str) -> str:
    """Validate and normalize a Chrome/Edge or Firefox browser extension origin."""
    cleaned = str(origin or "").strip().lower().rstrip("/")
    if not cleaned or any(ch.isspace() for ch in cleaned):
        raise ClipperError(
            "Extension origin must be a valid 'chrome-extension://<32-char-id>' or 'moz-extension://<uuid>' origin."
        )
    if _CHROME_EXT_ORIGIN_RE.fullmatch(cleaned) or _MOZ_EXT_ORIGIN_RE.fullmatch(cleaned):
        return cleaned
    raise ClipperError(
        f"Unsupported extension origin {origin!r}; expected 'chrome-extension://<32-char-id>' or 'moz-extension://<uuid>'."
    )


def validate_web_clip_url(url: str) -> tuple[str, str]:
    """Validate an http(s) URL and return (cleaned_url, safe_hostname)."""
    cleaned = str(url or "").strip()
    if not cleaned or len(cleaned) > MAX_CLIP_URL_CHARS or any(ord(c) < 32 or c.isspace() for c in cleaned):
        raise ClipperError("Web clip URL must be a single-line http:// or https:// URL without whitespace.")
    parsed = urlparse(cleaned)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ClipperError("Web clip URL must use http:// or https:// with a valid hostname.")
    if parsed.username or parsed.password:
        raise ClipperError("Refusing to clip URL containing embedded userinfo credentials.")
    host = re.sub(r"[^a-z0-9.-]", "", parsed.hostname.lower())[:64] or "web"
    return cleaned, host


def _sha256_str(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_pairings_unlocked(path: Path) -> dict[str, object]:
    if os.path.lexists(str(path.parent)) and _is_reparse_or_link(str(path.parent)):
        raise ClipperSecurityError(f"Refusing to read clipper pairings under symlinked directory: {path.parent}")
    if not os.path.lexists(str(path)):
        return {"schema_version": 1, "pairings": []}
    if _is_reparse_or_link(str(path)):
        raise ClipperSecurityError(f"Refusing to read symlinked clipper pairings file: {path}")
    st = os.lstat(str(path))
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_PAIRINGS_FILE_BYTES:
        raise ClipperSecurityError("Clipper pairings file is irregular or exceeds maximum allowed size.")
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("pairings"), list):
        raise ClipperSecurityError("Malformed clipper pairings registry.")
    return data


def _save_pairings_unlocked(path: Path, data: dict[str, object]) -> None:
    if os.path.lexists(str(path.parent)) and _is_reparse_or_link(str(path.parent)):
        raise ClipperSecurityError(f"Refusing to write clipper pairings under symlinked directory: {path.parent}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _is_reparse_or_link(str(path.parent)) or (os.path.lexists(str(path)) and _is_reparse_or_link(str(path))):
        raise ClipperSecurityError(f"Refusing to write clipper pairings through a symlink or reparse point: {path}")
    encoded = (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".clipper-pairings-", suffix=".tmp")
    fd_closed = False
    try:
        if os.name != "nt":
            os.chmod(tmp_name, 0o600)
        if os.write(fd, encoded) != len(encoded):
            raise OSError("Incomplete clipper pairings write.")
        os.fsync(fd)
        os.close(fd)
        fd_closed = True
        os.replace(tmp_name, path)
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


def create_clipper_pairing(
    origin: str,
    *,
    label: str = "Browser Clipper",
    security_dir: str | Path | None = None,
) -> dict[str, object]:
    """Register a browser extension origin and mint a scoped write-only Clipper bearer token."""
    norm_origin = validate_extension_origin(origin)
    clean_label = re.sub(r"\s+", " ", str(label or "Browser Clipper")).strip()[:80] or "Browser Clipper"
    path = _clipper_pairings_path(security_dir)
    raw_token = CLIPPER_TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_sha = _sha256_str(raw_token)
    pairing_id = "clip_" + secrets.token_hex(8)
    created_at = _now_iso()

    with _CLIPPER_LOCK:
        data = _load_pairings_unlocked(path)
        pairings: list[dict[str, object]] = [
            p for p in data.get("pairings", []) if isinstance(p, dict)
        ]
        # Re-pairing the same extension origin rotates its scoped token cleanly.
        pairings = [p for p in pairings if str(p.get("origin") or "").lower() != norm_origin]
        if len(pairings) >= MAX_ACTIVE_PAIRINGS:
            raise ClipperError(f"Maximum active browser clipper pairings ({MAX_ACTIVE_PAIRINGS}) reached; revoke unused pairings first.")
        record = {
            "pairing_id": pairing_id,
            "origin": norm_origin,
            "label": clean_label,
            "token_sha256": token_sha,
            "token_preview": f"{raw_token[:14]}…{raw_token[-4:]}",
            "created_at": created_at,
            "scopes": ["clipper:write", "clipper:status"],
        }
        pairings.append(record)
        _save_pairings_unlocked(path, {"schema_version": 1, "pairings": pairings})

    return {
        "pairing_id": pairing_id,
        "origin": norm_origin,
        "label": clean_label,
        "token": raw_token,
        "token_preview": record["token_preview"],
        "created_at": created_at,
        "scopes": list(record["scopes"]),
        "allowed_endpoints": ["/v1/clipper/ping", "/v1/clipper/status", "/v1/clipper/clip"],
    }


def list_clipper_pairings(*, security_dir: str | Path | None = None) -> list[dict[str, object]]:
    """List paired browser extensions without exposing secrets or hashes."""
    path = _clipper_pairings_path(security_dir)
    with _CLIPPER_LOCK:
        data = _load_pairings_unlocked(path)
        out: list[dict[str, object]] = []
        for item in data.get("pairings", []):
            if not isinstance(item, dict):
                continue
            out.append(
                {
                    "pairing_id": str(item.get("pairing_id") or ""),
                    "origin": str(item.get("origin") or ""),
                    "label": str(item.get("label") or "Browser Clipper"),
                    "token_preview": str(item.get("token_preview") or ""),
                    "created_at": str(item.get("created_at") or ""),
                    "scopes": list(item.get("scopes") or ["clipper:write", "clipper:status"]),
                }
            )
        return out


def revoke_clipper_pairing(pairing_id: str, *, security_dir: str | Path | None = None) -> dict[str, object]:
    """Immediately revoke a paired browser extension by pairing_id."""
    pid = str(pairing_id or "").strip()
    if not _PAIRING_ID_RE.fullmatch(pid):
        raise ClipperError(f"Invalid pairing_id: {pairing_id!r}")
    path = _clipper_pairings_path(security_dir)
    with _CLIPPER_LOCK:
        data = _load_pairings_unlocked(path)
        pairings = [p for p in data.get("pairings", []) if isinstance(p, dict)]
        remaining = [p for p in pairings if str(p.get("pairing_id") or "") != pid]
        if len(remaining) == len(pairings):
            raise FileNotFoundError(f"Clipper pairing not found: {pid}")
        _save_pairings_unlocked(path, {"schema_version": 1, "pairings": remaining})
    return {"status": "revoked", "pairing_id": pid}


def is_paired_clipper_origin(origin: str, *, security_dir: str | Path | None = None) -> bool:
    """Return True iff origin is a valid, owner-paired browser extension origin."""
    cleaned = str(origin or "").strip().lower().rstrip("/")
    if not (_CHROME_EXT_ORIGIN_RE.fullmatch(cleaned) or _MOZ_EXT_ORIGIN_RE.fullmatch(cleaned)):
        return False
    path = _clipper_pairings_path(security_dir)
    try:
        with _CLIPPER_LOCK:
            data = _load_pairings_unlocked(path)
            for item in data.get("pairings", []):
                if isinstance(item, dict) and hmac.compare_digest(str(item.get("origin") or "").lower(), cleaned):
                    return True
    except (OSError, ValueError, ClipperSecurityError):
        return False
    return False


def verify_clipper_token(
    auth_header: str | None,
    *,
    origin: str | None = None,
    security_dir: str | Path | None = None,
) -> dict[str, object] | None:
    """Verify a scoped `clipper_*` Bearer token (and optional extension Origin) in constant time."""
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    supplied = auth_header[7:].strip()
    if not supplied.startswith(CLIPPER_TOKEN_PREFIX) or len(supplied) < 32:
        return None
    supplied_sha = _sha256_str(supplied)
    clean_origin = str(origin or "").strip().lower().rstrip("/")
    path = _clipper_pairings_path(security_dir)
    try:
        with _CLIPPER_LOCK:
            data = _load_pairings_unlocked(path)
            pairings = [p for p in data.get("pairings", []) if isinstance(p, dict)]
    except (OSError, ValueError, ClipperSecurityError):
        return None

    matched: dict[str, object] | None = None
    for item in pairings:
        expected_sha = str(item.get("token_sha256") or "")
        if len(expected_sha) == 64 and hmac.compare_digest(supplied_sha, expected_sha):
            matched = item
    if matched is None:
        return None
    registered_origin = str(matched.get("origin") or "").lower()
    if clean_origin and not hmac.compare_digest(clean_origin, registered_origin):
        return None
    return {
        "pairing_id": str(matched.get("pairing_id") or ""),
        "origin": registered_origin,
        "label": str(matched.get("label") or "Browser Clipper"),
        "scopes": list(matched.get("scopes") or ["clipper:write", "clipper:status"]),
    }


def _screen_clip_risks(url: str, title: str, quote: str, page_text: str) -> list[str]:
    """Run both secret-pattern DLP and inbox injection risk screening on clipped web text."""
    combined = f"{url}\n{title}\n{quote}\n{page_text}"
    risks: list[str] = []
    for flag in scan_inbox_payload_risks(combined):
        if flag not in risks:
            risks.append(str(flag))
    redaction = inspect_and_redact_outbound(combined, redact_pii=False, fail_on_secret_detection=False)
    for secret_hit in redaction.secret_hits:
        label = f"dlp_secret:{secret_hit}"
        if label not in risks:
            risks.append(label)
    return risks


def _write_exclusive_snapshot(directory: Path, stem: str, payload: bytes) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    if _is_reparse_or_link(str(directory)):
        raise ClipperSecurityError(f"Refusing to write clip snapshot into symlinked directory: {directory}")
    suffix = 1
    while True:
        name = f"{stem}.md" if suffix == 1 else f"{stem}-{suffix}.md"
        target = directory / name
        if os.path.lexists(str(target)) and _is_reparse_or_link(str(target)):
            raise ClipperSecurityError(f"Refusing to overwrite symlinked snapshot: {name}")
        try:
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
        except FileExistsError:
            suffix += 1
            continue
        try:
            if os.write(fd, payload) != len(payload):
                raise OSError("Incomplete clip snapshot write.")
            os.fsync(fd)
        finally:
            os.close(fd)
        return name


def ingest_web_clip(
    *,
    url: str,
    title: str,
    selected_quote: str = "",
    page_text: str = "",
    tags: list[str] | tuple[str, ...] | str = (),
    destination: str = "note",
    capture_source: str = "browser-clipper",
    brain_dir: str | None = None,
) -> dict[str, object]:
    """Capture a web quote/page locally with an immutable SHA-256 snapshot and DLP quarantine."""
    clean_url, host = validate_web_clip_url(url)
    clean_title = re.sub(r"\s+", " ", str(title or "")).strip()[:MAX_CLIP_TITLE_CHARS] or host
    clean_quote = str(selected_quote or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    clean_page = str(page_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(clean_quote) > MAX_CLIP_QUOTE_CHARS:
        raise ClipperError(f"Selected quote exceeds maximum allowed length ({MAX_CLIP_QUOTE_CHARS} chars).")
    if len(clean_page) > MAX_CLIP_PAGE_TEXT_CHARS:
        raise ClipperError(f"Page text exceeds maximum allowed length ({MAX_CLIP_PAGE_TEXT_CHARS} chars).")
    if not clean_quote and not clean_page:
        raise ClipperError("Provide either 'selected_quote' or 'page_text' to clip.")

    dest_norm = str(destination or "note").strip().lower()
    if dest_norm not in {"note", "inbox"}:
        raise ClipperError("Destination must be 'note' or 'inbox'.")

    raw_tags = tags.split(",") if isinstance(tags, str) else list(tags or ())
    merged_tags = ["web-clip", f"site:{host}"]
    for t in raw_tags:
        s = re.sub(r"[^A-Za-z0-9_:/.-]", "", str(t or "").strip())[:64]
        if s and s not in merged_tags:
            merged_tags.append(s)

    resolved_brain = os.path.abspath(brain_dir) if brain_dir else resolve_active_brain_dir()
    if os.path.lexists(resolved_brain) and _is_reparse_or_link(resolved_brain):
        raise ClipperSecurityError("Refusing to clip into a symlinked vault directory.")
    npaths = NotePaths.for_brain(resolved_brain)
    captured_at = _now_iso()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = slugify(clean_title, max_len=48)

    risks = _screen_clip_risks(clean_url, clean_title, clean_quote, clean_page)
    quarantined = bool(risks) or dest_norm == "inbox"

    snapshot_rel: str | None = None
    snapshot_sha256: str | None = None
    if not risks:
        # Build immutable UTF-8 source snapshot so clean quotes can later be cryptographically anchored.
        # Risk-flagged clips NEVER write unredacted snapshots to 0-raw/note-snapshots/.
        snap_sections = [
            "---",
            f"title: {json.dumps(clean_title, ensure_ascii=False)}",
            "type: web_clip_snapshot",
            f"source_url: {json.dumps(clean_url, ensure_ascii=False)}",
            f"captured_at: {captured_at}",
            f"capture_source: {json.dumps(str(capture_source or 'browser-clipper')[:64], ensure_ascii=False)}",
            "---",
            "",
            f"# {clean_title}",
            "",
            f"Source URL: {clean_url}",
            f"Captured At: {captured_at}",
        ]
        if clean_quote and (not clean_page or clean_quote not in clean_page):
            snap_sections.extend(["", "## Selected Quote", "", clean_quote])
        if clean_page:
            snap_sections.extend(["", "## Captured Page Text", "", clean_page])
        snapshot_bytes = ("\n".join(snap_sections).rstrip() + "\n").encode("utf-8")
        snapshot_sha256 = content_sha256(snapshot_bytes)

        snap_dir = Path(resolved_brain) / "0-raw" / "note-snapshots"
        os.makedirs(os.path.dirname(npaths.lock_path), exist_ok=True)
        with review_lock(npaths.lock_path):
            snap_name = _write_exclusive_snapshot(snap_dir, f"clip-{stamp}-{slug}", snapshot_bytes)
        snapshot_rel = f"0-raw/note-snapshots/{snap_name}"

    if quarantined:
        inbox_dir = Path(npaths.inbox_dir)
        inbox_dir.mkdir(parents=True, exist_ok=True)
        if _is_reparse_or_link(str(inbox_dir)):
            raise ClipperSecurityError("Refusing to write quarantined clip to symlinked inbox directory.")
        os.makedirs(os.path.dirname(npaths.lock_path), exist_ok=True)
        safe_url = (
            inspect_and_redact_outbound(clean_url, redact_pii=False, fail_on_secret_detection=False).sanitized_text
            if risks and clean_url
            else clean_url
        )
        safe_quote = (
            inspect_and_redact_outbound(clean_quote, redact_pii=False, fail_on_secret_detection=False).sanitized_text
            if risks and clean_quote
            else clean_quote
        )
        safe_page = (
            inspect_and_redact_outbound(clean_page, redact_pii=False, fail_on_secret_detection=False).sanitized_text
            if risks and clean_page
            else clean_page
        )
        safe_title = (
            inspect_and_redact_outbound(clean_title, redact_pii=False, fail_on_secret_detection=False).sanitized_text
            if risks and clean_title
            else clean_title
        )
        inbox_slug = slugify(safe_title, max_len=48) if risks else slug
        safe_src = re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9._:-]", "", str(capture_source or "browser-clipper")))[:64]
        inbox_lines = [
            "<!-- second_brain_origin: web_clipper -->",
            f"<!-- capture_source: {safe_src} -->",
            f"<!-- source_url: {safe_url.replace('--', '—')} -->",
        ]
        if snapshot_rel and snapshot_sha256:
            inbox_lines.append(f"<!-- clip_snapshot: {snapshot_rel} -->")
            inbox_lines.append(f"<!-- clip_snapshot_sha256: {snapshot_sha256} -->")
        if risks:
            inbox_lines.append(f"<!-- quarantine_risks: {', '.join(risks)} -->")
        meta_lines = [
            "",
            f"# {safe_title}",
            "",
            f"- Source: {safe_url}",
        ]
        if snapshot_rel and snapshot_sha256:
            meta_lines.append(f"- Snapshot: `{snapshot_rel}` (`{snapshot_sha256}`)")
        meta_lines.append(f"- Tags: {', '.join(merged_tags)}")
        inbox_lines.extend(meta_lines)
        if safe_quote:
            inbox_lines.extend(["", "## Highlighted Quote", "", f"> {safe_quote}"])
        if safe_page:
            inbox_lines.extend(["", "## Page Text", "", safe_page])
        inbox_bytes = ("\n".join(inbox_lines).rstrip() + "\n").encode("utf-8")
        with review_lock(npaths.lock_path):
            inbox_name = _write_exclusive_snapshot(inbox_dir, f"{stamp}-{inbox_slug}-clip", inbox_bytes)
        return {
            "status": "quarantined" if risks else "queued_inbox",
            "destination": "inbox",
            "filename": inbox_name,
            "location": "0-raw/inbox/",
            "snapshot_path": snapshot_rel,
            "snapshot_sha256": snapshot_sha256,
            "quarantined": bool(risks),
            "risk_flags": risks,
            "quarantine_reason": (
                "Web clip matched DLP secret or prompt-injection patterns and was quarantined to 0-raw/inbox/."
                if risks
                else "Clipped directly to quarantined 0-raw/inbox/ as requested."
            ),
            "url": safe_url,
            "title": safe_title,
            "tags": merged_tags,
            "captured_at": captured_at,
        }

    note_lines = [
        f"Source: {clean_url}",
        f"Captured: {captured_at}",
        f"Snapshot: `{snapshot_rel}` (SHA-256 `{snapshot_sha256}`)",
    ]
    if clean_quote:
        note_lines.extend(["", "## Highlighted Quote", "", clean_quote])
    if clean_page and (not clean_quote or clean_quote not in clean_page):
        note_lines.extend(["", "## Excerpt / Page Context", "", clean_page])
    note_body = "\n".join(note_lines).strip()
    created = create_note(
        title=clean_title,
        body=note_body,
        origin="owner_import",
        tags=tuple(merged_tags),
        source_path=snapshot_rel,
        capture_source=capture_source,
        paths=npaths,
    )
    return {
        "status": str(created.get("status") or "saved"),
        "destination": "note",
        "filename": str(created.get("filename") or ""),
        "note_sha256": str(created.get("sha256") or ""),
        "location": "notes/",
        "snapshot_path": snapshot_rel,
        "snapshot_sha256": snapshot_sha256,
        "quarantined": False,
        "risk_flags": [],
        "url": clean_url,
        "title": clean_title,
        "tags": merged_tags,
        "captured_at": captured_at,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="second-brain clipper",
        description="Pair and manage the local-first Browser Web Clipper extension.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pair = sub.add_parser("pair", help="Register a browser extension origin and mint a scoped Clipper token.")
    p_pair.add_argument("--origin", required=True, help="Extension origin (chrome-extension://<32-char-id> or moz-extension://<uuid>).")
    p_pair.add_argument("--label", default="Browser Clipper", help="Human-friendly browser label.")
    p_pair.add_argument("--json", action="store_true", help="Output JSON.")

    p_list = sub.add_parser("list", help="List paired browser clipper extensions.")
    p_list.add_argument("--json", action="store_true", help="Output JSON.")

    p_rev = sub.add_parser("revoke", help="Revoke a paired browser extension by pairing_id.")
    p_rev.add_argument("pairing_id", help="Pairing ID (e.g. clip_...).")
    p_rev.add_argument("--json", action="store_true", help="Output JSON.")

    p_clip = sub.add_parser("clip", help="Ingest a web clip locally with an immutable SHA-256 snapshot.")
    p_clip.add_argument("--url", required=True, help="Source http(s) URL.")
    p_clip.add_argument("--title", default="", help="Page title.")
    p_clip.add_argument("--quote", default="", help="Highlighted verbatim quote.")
    p_clip.add_argument("--body", default="", help="Cleaned page text.")
    p_clip.add_argument("--tags", default="", help="Comma-separated tags.")
    p_clip.add_argument("--destination", choices=["note", "inbox"], default="note")
    p_clip.add_argument("--json", action="store_true", help="Output JSON.")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "pair":
            res = create_clipper_pairing(args.origin, label=args.label)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"Paired browser clipper '{res['label']}' ({res['pairing_id']})")
                print(f"  Origin: {res['origin']}")
                print(f"  Scoped Token (save in extension popup once): {res['token']}")
            return 0
        if args.cmd == "list":
            items = list_clipper_pairings()
            if args.json:
                print(json.dumps({"count": len(items), "pairings": items}, indent=2))
            elif not items:
                print("No browser clipper extensions paired yet.")
            else:
                for it in items:
                    print(f"- {it['pairing_id']}  {it['label']}  {it['origin']}  ({it['token_preview']})")
            return 0
        if args.cmd == "revoke":
            res = revoke_clipper_pairing(args.pairing_id)
            print(json.dumps(res, indent=2) if args.json else f"Revoked pairing {res['pairing_id']}")
            return 0
        if args.cmd == "clip":
            res = ingest_web_clip(
                url=args.url,
                title=args.title,
                selected_quote=args.quote,
                page_text=args.body,
                tags=args.tags,
                destination=args.destination,
            )
            print(json.dumps(res, indent=2) if args.json else f"[{res['status']}] {res['location']}{res['filename']} (snapshot {res['snapshot_path']})")
            return 0
    except (ClipperError, ClipperSecurityError, FileNotFoundError, PermissionError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
