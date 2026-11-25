# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Quarantined inbox inspection, adversarial risk scanning, and CAS-locked owner promotion."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import datetime
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import ReviewConflictError, atomic_write_bytes
from config import (
    BRAIN_DIR,
    INBOX_DIR,
    RAW_DIR,
    SIDECAR_DIR,
    _is_reparse_or_link,
    file_sha256,
    resolve_under,
    safe_file_in_dir,
    safe_vault_reference,
)
from host_adapters import restrict_security_file_permissions
from refinery_targets import intake_status
from review_atom import review_lock

INBOX_AUDIT_FILENAME = "inbox-decisions.jsonl"
MAX_PROMOTED_BYTES = 64 * 1024
ALLOWED_INBOX_ORIGINS = frozenset({
    "mcp_local",
    "api_local",
    "cognitive_experience",
    "web_clipper",
    "web_remote",
    "imported_unknown",
    "untrusted_inbox",
    "miss_inbox",
})

_FORGED_TRUST_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("forged_accepted_status", re.compile(r"(?im)^\s*review_status\s*:\s*(?:accepted|approved)\b")),
    ("forged_epistemic_contract", re.compile(r"(?im)^\s*epistemic_contract\s*:")),
    ("forged_established_certainty", re.compile(r"(?im)^\s*certainty\s*:\s*established\b")),
    ("prompt_override_directive", re.compile(r"(?i)\b(?:ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions|disregard\s+system\s+prompt|you\s+are\s+now\s+in\s+developer\s+mode)\b")),
    (
        "outbound_markdown_beacon",
        re.compile(
            r"(?:!\[[^\]]*\]\(\s*<?https?://|!\[[^\]]*\]\s*\[[^\]]*\]|<img\b[^>]*https?://|^\s{0,3}\[[^\]]+\]:\s*<?https?://)",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
)


@dataclass(frozen=True)
class QuarantinedInboxItem:
    filename: str
    sha256: str
    size_bytes: int
    modified_at: str
    origin: str
    risk_flags: tuple[str, ...]
    preview: str


def _inbox_audit_path(brain_dir: str = BRAIN_DIR) -> str:
    return os.path.join(brain_dir, ".sidecar", INBOX_AUDIT_FILENAME)


def _extract_origin_tag(text: str) -> str:
    """Extract channel provenance strictly from the very first non-BOM line; forbid user_local in inbox."""
    unbom = (text or "").lstrip("\ufeff")
    if not unbom:
        return "untrusted_inbox"
    first_line = unbom.splitlines()[0].strip()
    match = re.fullmatch(r"<!--\s*second_brain_origin:\s*([a-zA-Z0-9_.-]+)\s*-->", first_line)
    if match:
        candidate = match.group(1).strip()
        if candidate in ALLOWED_INBOX_ORIGINS:
            return candidate
    return "untrusted_inbox"


_SECRET_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai_api_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35,}\b")),
    ("github_pat", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_sts_key", re.compile(r"\bASIA[0-9A-Z]{16}\b")),
    ("jwt_token", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("stripe_live_key", re.compile(r"\b(?:sk|rk)_live_[0-9a-zA-Z]{24,}\b")),
    ("url_basic_auth", re.compile(r"https?://[^/\s:@\"']+:[^/\s@\"']+@")),
    ("private_key_pem", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("bearer_header_secret", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b")),
)


def scan_inbox_payload_risks(content: str) -> tuple[str, ...]:
    flags: list[str] = []
    for code, pattern in _FORGED_TRUST_PATTERNS:
        if pattern.search(content):
            flags.append(code)
    for label, pattern in _SECRET_CREDENTIAL_PATTERNS:
        if pattern.search(content):
            flags.append(f"secret_credential:{label}")
    if any(ord(ch) < 32 and ch not in ("\n", "\r", "\t") for ch in content):
        flags.append("control_characters_present")
    if len(content.encode("utf-8")) > MAX_PROMOTED_BYTES:
        flags.append("oversized_payload")
    return tuple(flags)


def _unique_archive_destination(inbox_dir: str, subdir: str, prefix_sha: str, basename: str) -> str:
    if subdir not in {"promoted", "rejected", ".rejected"}:
        raise ValueError(f"Unsupported inbox archive subdirectory: {subdir}")
    os.makedirs(inbox_dir, exist_ok=True)
    resolved_subdir = resolve_under(inbox_dir, subdir, must_exist=False)
    if not resolved_subdir:
        raise PermissionError(f"Refusing to archive into unsafe or symlinked inbox directory: {subdir}")
    os.makedirs(resolved_subdir, exist_ok=True)
    if _is_reparse_or_link(resolved_subdir):
        raise PermissionError(f"Refusing to archive through symlink or junction: {subdir}")

    stem, ext = os.path.splitext(basename)
    if ext.lower() not in {".md", ".txt", ".intake"}:
        raise ValueError(f"Unsupported inbox archive extension: {ext}")
    candidate_name = f"{prefix_sha}-{basename}"
    suffix = 2
    while True:
        rel_target = os.path.join(subdir, candidate_name)
        dest = resolve_under(inbox_dir, rel_target, must_exist=False)
        if not dest:
            raise PermissionError(f"Refusing unsafe archive destination path: {rel_target}")
        if not os.path.lexists(dest):
            return dest
        candidate_name = f"{prefix_sha}-{stem}-{suffix}{ext}"
        suffix += 1


def _resolve_raw_and_brain(
    inbox_dir: str,
    raw_dir: str | None = None,
    brain_dir: str | None = None,
) -> tuple[str, str]:
    abs_inbox = os.path.abspath(inbox_dir)
    if raw_dir is not None:
        resolved_raw = os.path.abspath(raw_dir)
    elif os.path.basename(abs_inbox) == "inbox":
        resolved_raw = os.path.dirname(abs_inbox)
    else:
        resolved_raw = os.path.abspath(RAW_DIR)

    if brain_dir is not None:
        resolved_brain = os.path.abspath(brain_dir)
    else:
        resolved_brain = os.path.dirname(resolved_raw)
    return resolved_raw, resolved_brain


def _extract_intake_origin(content: str) -> str:
    tagged = _extract_origin_tag(content)
    if tagged != "untrusted_inbox":
        return tagged
    origin_match = re.search(r"^origin:\s*([a-zA-Z0-9_.-]+)\s*$", content or "", re.MULTILINE)
    if origin_match:
        cand = origin_match.group(1).strip()
        if cand in ALLOWED_INBOX_ORIGINS:
            return cand
    return "web_remote"


def _resolve_intake_source(pointer_text: str, brain_dir: str) -> tuple[str | None, str | None, str]:
    """Return (source_ref, resolved_source_path, source_text) for a .intake pointer."""
    source_match = re.search(r"^source:\s*(.+)$", pointer_text or "", re.MULTILINE)
    if not source_match:
        return None, None, ""
    source_ref = source_match.group(1).strip()
    if os.path.splitext(source_ref)[1].lower() != ".md":
        return source_ref, None, ""
    if os.path.abspath(brain_dir) == os.path.abspath(BRAIN_DIR):
        source_path = safe_vault_reference(source_ref, allowed_exts={".md"})
    else:
        source_path = resolve_under(brain_dir, source_ref, must_exist=True)
    if not source_path or not os.path.isfile(source_path) or _is_reparse_or_link(source_path):
        return source_ref, None, ""
    try:
        with open(source_path, "r", encoding="utf-8", errors="replace") as handle:
            return source_ref, source_path, handle.read()
    except OSError:
        return source_ref, None, ""


def _scan_intake_risks(pointer_text: str, brain_dir: str) -> tuple[tuple[str, ...], str | None, str]:
    source_ref, source_path, article_text = _resolve_intake_source(pointer_text, brain_dir)
    combined = pointer_text if not article_text else f"{pointer_text}\n{article_text}"
    flags = list(scan_inbox_payload_risks(combined))
    if source_ref and not source_path and "invalid_or_missing_intake_source" not in flags:
        flags.append("invalid_or_missing_intake_source")
    return tuple(flags), source_ref, article_text


def _sanitize_actor_header_value(actor: str) -> str:
    safe_actor = (
        str(actor)
        .replace("--", "—")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()[:120]
        or "owner"
    )
    while "--" in safe_actor:
        safe_actor = safe_actor.replace("--", "—")
    return safe_actor


def _rewrite_intake_pointer(
    pointer_text: str,
    *,
    new_status: str,
    actor: str,
    timestamp: str,
) -> str:
    safe_actor = _sanitize_actor_header_value(actor)
    lines: list[str] = []
    saw_status = False
    actor_key = "promoted_by" if new_status == "approved" else "rejected_by"
    time_key = "promoted_at" if new_status == "approved" else "rejected_at"
    skip_keys = {"promoted_by", "promoted_at", "rejected_by", "rejected_at"}
    for raw_line in (pointer_text or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        key_part = stripped.split(":", 1)[0].strip() if ":" in stripped else ""
        if key_part == "intake_status":
            lines.append(f"intake_status: {new_status}")
            saw_status = True
        elif key_part == "requires_owner_promotion" and new_status == "approved":
            lines.append("requires_owner_promotion: false")
        elif key_part in skip_keys:
            continue
        else:
            lines.append(raw_line)
    if not saw_status:
        lines.append(f"intake_status: {new_status}")
    lines.append(f"{actor_key}: {safe_actor}")
    lines.append(f"{time_key}: {timestamp}")
    return "\n".join(lines) + "\n"


def sanitize_promoted_content(content: str, *, origin: str, promoted_by: str, original_sha256: str) -> str:
    """Sanitize untrusted inbox text and neutralize forged YAML frontmatter before Refinery intake."""
    safe_origin = origin if origin in ALLOWED_INBOX_ORIGINS else "untrusted_inbox"
    safe_actor = (
        str(promoted_by)
        .replace("--", "—")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()[:120]
        or "owner"
    )
    while "--" in safe_actor:
        safe_actor = safe_actor.replace("--", "—")

    cleaned = "".join(ch for ch in (content or "").lstrip("\ufeff") if ch in ("\n", "\r", "\t") or ord(ch) >= 32)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")

    # 1. Strip all leading HTML metadata comments and any embedded second_brain_origin comments FIRST.
    while True:
        updated = re.sub(
            r"<!--[^>]*?\b(?:second_brain_origin|inbox_promoted_by)\b[\s\S]*?-->",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        updated = re.sub(r"^\s*<!--[\s\S]*?-->\s*", "", updated, count=1)
        if updated == cleaned:
            break
        cleaned = updated
    cleaned = cleaned.strip()

    # 2. Neutralize leading or embedded YAML frontmatter delimiters so untrusted text cannot pre-seed metadata.
    if (
        cleaned.startswith("---\n")
        or cleaned.startswith("---\r\n")
        or cleaned == "---"
        or bool(re.match(r"^---[ \t]*(?:\r?\n|$)", cleaned))
    ):
        first_nl = cleaned.find("\n")
        rest = cleaned[first_nl + 1 :].lstrip("\n") if first_nl != -1 else ""
        cleaned = "<!-- neutralized_untrusted_frontmatter -->" + (f"\n{rest}" if rest else "")
    cleaned = re.sub(r"(?m)^[ \t]*---[ \t]*$", "<!-- neutralized_yaml_delimiter -->", cleaned)

    # 3. Defang inline, angle-bracketed, reference-style, and raw HTML external image beacons.
    cleaned = re.sub(
        r"!\[([^\]]*)\]\(\s*<?(https?://[^)\s>]+)>?(?:\s+[^)]*)?\)",
        r"[external-image-blocked: \1](\2)",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"!\[([^\]]*)\]\s*\[([^\]]*)\]",
        r"[external-image-ref-blocked: \1][\2]",
        cleaned,
    )
    cleaned = re.sub(
        r"<img\b[^>]*>",
        "[external-html-img-blocked]",
        cleaned,
        flags=re.IGNORECASE,
    )

    ts = datetime.datetime.now().isoformat(timespec="seconds")
    header = (
        f"<!-- second_brain_origin: {safe_origin} -->\n"
        f"<!-- inbox_promoted_by: {safe_actor} at {ts} | inbox_sha256: {original_sha256} -->\n\n"
    )
    return header + cleaned.lstrip() + "\n"


def list_inbox_items(
    inbox_dir: str = INBOX_DIR,
    *,
    raw_dir: str | None = None,
    brain_dir: str | None = None,
) -> list[QuarantinedInboxItem]:
    resolved_raw, resolved_brain = _resolve_raw_and_brain(inbox_dir, raw_dir=raw_dir, brain_dir=brain_dir)
    items: list[QuarantinedInboxItem] = []
    seen_names: set[str] = set()

    if os.path.isdir(inbox_dir):
        for entry in sorted(os.listdir(inbox_dir)):
            safe_path = safe_file_in_dir(inbox_dir, entry, allowed_exts={".md", ".txt", ".intake"}, must_exist=True)
            if not safe_path or not os.path.isfile(safe_path) or _is_reparse_or_link(safe_path):
                continue
            sha = file_sha256(safe_path)
            if not sha:
                continue
            stat = os.stat(safe_path)
            with open(safe_path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
            if entry.lower().endswith(".intake"):
                risk_flags, _src_ref, article_text = _scan_intake_risks(content, resolved_brain)
                origin = _extract_intake_origin(content)
                preview_text = f"{content.strip()}\n\n{article_text[:400]}".strip()[:600] if article_text else content[:600]
            else:
                risk_flags = scan_inbox_payload_risks(content)
                origin = _extract_origin_tag(content)
                preview_text = content[:600]
            seen_names.add(entry)
            items.append(
                QuarantinedInboxItem(
                    filename=entry,
                    sha256=sha,
                    size_bytes=stat.st_size,
                    modified_at=datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                    origin=origin,
                    risk_flags=risk_flags,
                    preview=preview_text,
                )
            )

    if os.path.isdir(resolved_raw) and os.path.abspath(resolved_raw) != os.path.abspath(inbox_dir):
        for entry in sorted(os.listdir(resolved_raw)):
            if not entry.lower().endswith(".intake") or entry in seen_names:
                continue
            safe_path = safe_file_in_dir(resolved_raw, entry, allowed_exts={".intake"}, must_exist=True)
            if not safe_path or not os.path.isfile(safe_path) or _is_reparse_or_link(safe_path):
                continue
            with open(safe_path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
            if intake_status(content) != "quarantined":
                continue
            sha = file_sha256(safe_path)
            if not sha:
                continue
            stat = os.stat(safe_path)
            risk_flags, _src_ref, article_text = _scan_intake_risks(content, resolved_brain)
            preview_text = f"{content.strip()}\n\n{article_text[:400]}".strip()[:600] if article_text else content[:600]
            items.append(
                QuarantinedInboxItem(
                    filename=entry,
                    sha256=sha,
                    size_bytes=stat.st_size,
                    modified_at=datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                    origin=_extract_intake_origin(content),
                    risk_flags=risk_flags,
                    preview=preview_text,
                )
            )

    items.sort(key=lambda item: item.filename)
    return items


def _locate_quarantined_file(filename: str, inbox_dir: str, raw_dir: str) -> tuple[str, bool]:
    """Locate a quarantined file in inbox_dir or (for quarantined .intake) raw_dir. Returns (safe_path, from_raw_dir)."""
    safe_path = safe_file_in_dir(inbox_dir, filename, allowed_exts={".md", ".txt", ".intake"}, must_exist=False)
    if safe_path and os.path.isfile(safe_path) and not _is_reparse_or_link(safe_path):
        return safe_path, False
    if str(filename or "").lower().endswith(".intake") and os.path.isdir(raw_dir):
        raw_path = safe_file_in_dir(raw_dir, filename, allowed_exts={".intake"}, must_exist=False)
        if raw_path and os.path.isfile(raw_path) and not _is_reparse_or_link(raw_path):
            with open(raw_path, "r", encoding="utf-8", errors="replace") as handle:
                if intake_status(handle.read()) == "quarantined":
                    return raw_path, True
    raise FileNotFoundError(f"Quarantined inbox file not found or invalid: {filename}")


def inspect_inbox_item(
    filename: str,
    *,
    inbox_dir: str = INBOX_DIR,
    raw_dir: str | None = None,
    brain_dir: str | None = None,
) -> dict[str, object]:
    resolved_raw, resolved_brain = _resolve_raw_and_brain(inbox_dir, raw_dir=raw_dir, brain_dir=brain_dir)
    safe_path, _from_raw = _locate_quarantined_file(filename, inbox_dir, resolved_raw)
    sha = file_sha256(safe_path)
    if not sha:
        raise OSError(f"Unable to hash quarantined inbox file: {filename}")
    with open(safe_path, "r", encoding="utf-8", errors="replace") as handle:
        content = handle.read()
    basename = os.path.basename(safe_path)
    if basename.lower().endswith(".intake"):
        risks, source_ref, article_text = _scan_intake_risks(content, resolved_brain)
        combined_display = (
            f"{content.strip()}\n\n--- Referenced Source ({source_ref}) ---\n{article_text}"
            if article_text
            else content
        )
        return {
            "filename": basename,
            "sha256": sha,
            "size_bytes": len(content.encode("utf-8")),
            "origin": _extract_intake_origin(content),
            "risk_flags": list(risks),
            "requires_acknowledge_risks": bool(risks),
            "pointer_metadata": content,
            "source_reference": source_ref,
            "article_preview": article_text[:1000] if article_text else "",
            "content": combined_display,
        }
    risks = scan_inbox_payload_risks(content)
    return {
        "filename": basename,
        "sha256": sha,
        "size_bytes": len(content.encode("utf-8")),
        "origin": _extract_origin_tag(content),
        "risk_flags": list(risks),
        "requires_acknowledge_risks": bool(risks),
        "content": content,
    }


def _append_inbox_decision(record: dict[str, object], *, brain_dir: str) -> None:
    audit_path = _inbox_audit_path(brain_dir)
    audit_parent = os.path.dirname(os.path.abspath(audit_path))
    os.makedirs(audit_parent, exist_ok=True)
    if _is_reparse_or_link(audit_parent) or (os.path.lexists(audit_path) and _is_reparse_or_link(audit_path)):
        raise PermissionError("Refusing to write inbox audit log through a symlink or reparse point.")
    existed_before = os.path.exists(audit_path)
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.write(fd, line) != len(line):
            raise OSError("Incomplete write to inbox decision log.")
        os.fsync(fd)
    finally:
        os.close(fd)
    if not existed_before:
        restrict_security_file_permissions(audit_path)


def promote_inbox_item(
    filename: str,
    *,
    expected_sha256: str,
    actor: str,
    acknowledge_risks: bool = False,
    inbox_dir: str = INBOX_DIR,
    raw_dir: str | None = None,
    brain_dir: str | None = None,
) -> dict[str, object]:
    """CAS-verified promotion from quarantined 0-raw/inbox/ (or 0-raw/*.intake) into 0-raw/ for Refinery processing."""
    actor_clean = str(actor or "").strip()
    if not actor_clean:
        raise ValueError("Actor identity is required to promote quarantined inbox captures.")
    expected = str(expected_sha256 or "").strip().lower()
    if len(expected) != 64:
        raise ValueError("A 64-character expected_sha256 is required for CAS inbox promotion.")

    resolved_raw, resolved_brain = _resolve_raw_and_brain(inbox_dir, raw_dir=raw_dir, brain_dir=brain_dir)
    lock_path = os.path.join(resolved_brain, ".sidecar", "writer.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with review_lock(lock_path):
        safe_src, from_raw = _locate_quarantined_file(filename, inbox_dir, resolved_raw)
        current_sha = file_sha256(safe_src)
        if current_sha != expected:
            raise ReviewConflictError(
                f"CAS mismatch on quarantined file '{filename}': expected {expected}, found {current_sha}."
            )
        with open(safe_src, "r", encoding="utf-8", errors="strict") as handle:
            raw_text = handle.read()
        if len(raw_text.encode("utf-8")) > MAX_PROMOTED_BYTES:
            raise ValueError(f"Quarantined capture exceeds {MAX_PROMOTED_BYTES} byte promotion ceiling.")

        basename = os.path.basename(safe_src)
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

        if basename.lower().endswith(".intake"):
            risks, source_ref, _article_text = _scan_intake_risks(raw_text, resolved_brain)
            if risks and not acknowledge_risks:
                raise PermissionError(
                    f"Quarantined intake '{filename}' triggered security risk flags {list(risks)}; "
                    "inspect the payload and pass acknowledge_risks=True (--acknowledge-risks) to promote."
                )
            origin = _extract_intake_origin(raw_text)
            approved_pointer = _rewrite_intake_pointer(
                raw_text,
                new_status="approved",
                actor=actor_clean,
                timestamp=now_iso,
            )
            os.makedirs(resolved_raw, exist_ok=True)
            if from_raw:
                dest_path = safe_src
                dest_name = basename
            else:
                dest_name = basename
                candidate = safe_file_in_dir(resolved_raw, dest_name, allowed_exts={".intake"}, must_exist=False)
                if not candidate:
                    raise ValueError("Invalid destination .intake filename in 0-raw.")
                stem, ext = os.path.splitext(basename)
                suffix = 2
                while os.path.lexists(candidate):
                    dest_name = f"{stem}-{suffix}{ext}"
                    candidate = safe_file_in_dir(resolved_raw, dest_name, allowed_exts={".intake"}, must_exist=False)
                    if not candidate:
                        raise ValueError("Invalid destination .intake filename in 0-raw.")
                    suffix += 1
                dest_path = candidate

            atomic_write_bytes(dest_path, approved_pointer.encode("utf-8"))
            if not from_raw:
                archived_inbox_path = _unique_archive_destination(
                    inbox_dir,
                    "promoted",
                    current_sha[:12],
                    basename,
                )
                shutil.move(safe_src, archived_inbox_path)

            promoted_sha = file_sha256(dest_path)
            decision = {
                "schema_version": 1,
                "timestamp": now_iso,
                "action": "promote_inbox",
                "actor": actor_clean,
                "filename": basename,
                "promoted_to": os.path.basename(dest_path),
                "sha256": promoted_sha,
                "inbox_filename": basename,
                "inbox_sha256": current_sha,
                "promoted_filename": os.path.basename(dest_path),
                "promoted_sha256": promoted_sha,
                "source_reference": source_ref,
                "origin": origin,
                "risk_flags": list(risks),
                "acknowledged_risks": bool(acknowledge_risks),
            }
            _append_inbox_decision(decision, brain_dir=resolved_brain)
            return decision

        risks = scan_inbox_payload_risks(raw_text)
        if risks and not acknowledge_risks:
            raise PermissionError(
                f"Quarantined capture '{filename}' triggered security risk flags {list(risks)}; "
                "inspect the payload and pass acknowledge_risks=True (--acknowledge-risks) to promote."
            )

        origin = _extract_origin_tag(raw_text)
        sanitized = sanitize_promoted_content(
            raw_text,
            origin=origin,
            promoted_by=actor_clean,
            original_sha256=current_sha,
        )

        os.makedirs(resolved_raw, exist_ok=True)
        stem, ext = os.path.splitext(basename)
        target_ext = ".md" if ext.lower() == ".md" else ".txt"
        dest_name = f"promoted-{stem}{target_ext}"
        suffix = 2
        while True:
            dest_path = safe_file_in_dir(resolved_raw, dest_name, allowed_exts={".md", ".txt"}, must_exist=False)
            if not dest_path:
                raise ValueError("Invalid destination filename in 0-raw.")
            try:
                fd = os.open(dest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    encoded = sanitized.encode("utf-8")
                    if os.write(fd, encoded) != len(encoded):
                        raise OSError("Incomplete write while promoting inbox capture.")
                    os.fsync(fd)
                finally:
                    os.close(fd)
                break
            except FileExistsError:
                dest_name = f"promoted-{stem}-{suffix}{target_ext}"
                suffix += 1

        archived_inbox_path = _unique_archive_destination(
            inbox_dir,
            "promoted",
            current_sha[:12],
            basename,
        )
        shutil.move(safe_src, archived_inbox_path)

        promoted_sha = file_sha256(dest_path)
        decision = {
            "schema_version": 1,
            "timestamp": now_iso,
            "action": "promote_inbox",
            "actor": actor_clean,
            "filename": basename,
            "promoted_to": dest_name,
            "sha256": promoted_sha,
            "inbox_filename": basename,
            "inbox_sha256": current_sha,
            "promoted_filename": dest_name,
            "promoted_sha256": promoted_sha,
            "origin": origin,
            "risk_flags": list(risks),
            "acknowledged_risks": bool(acknowledge_risks),
        }
        _append_inbox_decision(decision, brain_dir=resolved_brain)
        return decision


def reject_inbox_item(
    filename: str,
    *,
    expected_sha256: str,
    actor: str,
    rationale: str = "Rejected during inbox quarantine review",
    inbox_dir: str = INBOX_DIR,
    raw_dir: str | None = None,
    brain_dir: str | None = None,
) -> dict[str, object]:
    actor_clean = str(actor or "").strip()
    if not actor_clean:
        raise ValueError("Actor identity is required to reject quarantined inbox captures.")
    expected = str(expected_sha256 or "").strip().lower()
    if len(expected) != 64:
        raise ValueError("A 64-character expected_sha256 is required for CAS inbox rejection.")

    resolved_raw, resolved_brain = _resolve_raw_and_brain(inbox_dir, raw_dir=raw_dir, brain_dir=brain_dir)
    lock_path = os.path.join(resolved_brain, ".sidecar", "writer.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with review_lock(lock_path):
        safe_src, _from_raw = _locate_quarantined_file(filename, inbox_dir, resolved_raw)
        current_sha = file_sha256(safe_src)
        if current_sha != expected:
            raise ReviewConflictError(
                f"CAS mismatch on quarantined file '{filename}': expected {expected}, found {current_sha}."
            )
        basename = os.path.basename(safe_src)
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

        if basename.lower().endswith(".intake"):
            with open(safe_src, "r", encoding="utf-8", errors="replace") as handle:
                pointer_text = handle.read()
            rejected_pointer = _rewrite_intake_pointer(
                pointer_text,
                new_status="rejected",
                actor=actor_clean,
                timestamp=now_iso,
            )
            archived_rejected = _unique_archive_destination(
                inbox_dir,
                ".rejected",
                current_sha[:12],
                basename,
            )
            atomic_write_bytes(archived_rejected, rejected_pointer.encode("utf-8"))
            os.remove(safe_src)
        else:
            archived_rejected = _unique_archive_destination(
                inbox_dir,
                "rejected",
                current_sha[:12],
                basename,
            )
            shutil.move(safe_src, archived_rejected)

        decision = {
            "schema_version": 1,
            "timestamp": now_iso,
            "action": "reject_inbox",
            "actor": actor_clean,
            "filename": basename,
            "inbox_filename": basename,
            "inbox_sha256": current_sha,
            "archived_to": os.path.relpath(archived_rejected, inbox_dir).replace("\\", "/"),
            "rationale": str(rationale or "").strip()[:500],
        }
        _append_inbox_decision(decision, brain_dir=resolved_brain)
        return decision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect, promote, or reject quarantined 0-raw/inbox captures.")
    sub = parser.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list", help="List quarantined inbox captures and risk flags.")
    list_p.add_argument("--json", action="store_true")

    insp_p = sub.add_parser("inspect", help="Inspect full contents and risk flags of one inbox capture.")
    insp_p.add_argument("filename")
    insp_p.add_argument("--json", action="store_true")

    prom_p = sub.add_parser("promote", help="Promote a quarantined capture into 0-raw/ via CAS.")
    prom_p.add_argument("filename")
    prom_p.add_argument("--expected-sha256", required=True)
    prom_p.add_argument("--actor", required=True)
    prom_p.add_argument("--acknowledge-risks", action="store_true")

    rej_p = sub.add_parser("reject", help="Reject and archive a quarantined capture.")
    rej_p.add_argument("filename")
    rej_p.add_argument("--expected-sha256", required=True)
    rej_p.add_argument("--actor", required=True)
    rej_p.add_argument("--rationale", default="Rejected during owner quarantine review")

    args = parser.parse_args(argv)
    if args.command == "list":
        items = list_inbox_items()
        if args.json:
            print(json.dumps([asdict(i) for i in items], indent=2, sort_keys=True))
        elif not items:
            print("Quarantined inbox is empty.")
        else:
            for item in items:
                risk_str = f" [RISKS: {', '.join(item.risk_flags)}]" if item.risk_flags else " [CLEAN]"
                print(f"{item.filename} | sha256={item.sha256[:12]}... | {item.size_bytes}B | origin={item.origin}{risk_str}")
        return 0

    if args.command == "inspect":
        detail = inspect_inbox_item(args.filename)
        if args.json:
            print(json.dumps(detail, indent=2, sort_keys=True))
        else:
            print(f"Filename: {detail['filename']}")
            print(f"SHA-256:  {detail['sha256']}")
            print(f"Origin:   {detail['origin']}")
            print(f"Risks:    {', '.join(detail['risk_flags']) if detail['risk_flags'] else 'none'}")
            print("-" * 60)
            print(detail["content"])
        return 0

    if args.command == "promote":
        decision = promote_inbox_item(
            args.filename,
            expected_sha256=args.expected_sha256,
            actor=args.actor,
            acknowledge_risks=args.acknowledge_risks,
        )
        print(json.dumps(decision, indent=2, sort_keys=True))
        return 0

    if args.command == "reject":
        decision = reject_inbox_item(
            args.filename,
            expected_sha256=args.expected_sha256,
            actor=args.actor,
            rationale=args.rationale,
        )
        print(json.dumps(decision, indent=2, sort_keys=True))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
