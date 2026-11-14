# /// script
# requires-python = ">=3.11,<3.12"
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

from config import BRAIN_DIR, INBOX_DIR, RAW_DIR, SIDECAR_DIR, _is_reparse_or_link, file_sha256, resolve_under, safe_file_in_dir
from review_atom import review_lock

INBOX_AUDIT_FILENAME = "inbox-decisions.jsonl"
MAX_PROMOTED_BYTES = 64 * 1024
ALLOWED_INBOX_ORIGINS = frozenset({
    "mcp_local",
    "api_local",
    "cognitive_experience",
    "web_remote",
    "imported_unknown",
    "untrusted_inbox",
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


def scan_inbox_payload_risks(content: str) -> tuple[str, ...]:
    flags: list[str] = []
    for code, pattern in _FORGED_TRUST_PATTERNS:
        if pattern.search(content):
            flags.append(code)
    if any(ord(ch) < 32 and ch not in ("\n", "\r", "\t") for ch in content):
        flags.append("control_characters_present")
    if len(content.encode("utf-8")) > MAX_PROMOTED_BYTES:
        flags.append("oversized_payload")
    return tuple(flags)


def _unique_archive_destination(inbox_dir: str, subdir: str, prefix_sha: str, basename: str) -> str:
    if subdir not in {"promoted", "rejected"}:
        raise ValueError(f"Unsupported inbox archive subdirectory: {subdir}")
    resolved_subdir = resolve_under(inbox_dir, subdir, must_exist=False)
    if not resolved_subdir:
        raise PermissionError(f"Refusing to archive into unsafe or symlinked inbox directory: {subdir}")
    os.makedirs(resolved_subdir, exist_ok=True)
    if _is_reparse_or_link(resolved_subdir):
        raise PermissionError(f"Refusing to archive through symlink or junction: {subdir}")

    stem, ext = os.path.splitext(basename)
    if ext.lower() not in {".md", ".txt"}:
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


def list_inbox_items(inbox_dir: str = INBOX_DIR) -> list[QuarantinedInboxItem]:
    if not os.path.isdir(inbox_dir):
        return []
    items: list[QuarantinedInboxItem] = []
    for entry in sorted(os.listdir(inbox_dir)):
        safe_path = safe_file_in_dir(inbox_dir, entry, allowed_exts={".md", ".txt"}, must_exist=True)
        if not safe_path or not os.path.isfile(safe_path):
            continue
        sha = file_sha256(safe_path)
        if not sha:
            continue
        stat = os.stat(safe_path)
        with open(safe_path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
        items.append(
            QuarantinedInboxItem(
                filename=entry,
                sha256=sha,
                size_bytes=stat.st_size,
                modified_at=datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                origin=_extract_origin_tag(content),
                risk_flags=scan_inbox_payload_risks(content),
                preview=content[:600],
            )
        )
    return items


def inspect_inbox_item(filename: str, *, inbox_dir: str = INBOX_DIR) -> dict[str, object]:
    safe_path = safe_file_in_dir(inbox_dir, filename, allowed_exts={".md", ".txt"}, must_exist=True)
    if not safe_path or not os.path.isfile(safe_path):
        raise FileNotFoundError(f"Quarantined inbox file not found or invalid: {filename}")
    sha = file_sha256(safe_path)
    if not sha:
        raise OSError(f"Unable to hash quarantined inbox file: {filename}")
    with open(safe_path, "r", encoding="utf-8", errors="replace") as handle:
        content = handle.read()
    risks = scan_inbox_payload_risks(content)
    return {
        "filename": os.path.basename(safe_path),
        "sha256": sha,
        "size_bytes": len(content.encode("utf-8")),
        "origin": _extract_origin_tag(content),
        "risk_flags": list(risks),
        "requires_acknowledge_risks": bool(risks),
        "content": content,
    }


def _append_inbox_decision(record: dict[str, object], *, brain_dir: str) -> None:
    audit_path = _inbox_audit_path(brain_dir)
    os.makedirs(os.path.dirname(audit_path), exist_ok=True)
    if os.path.lexists(audit_path) and _is_reparse_or_link(audit_path):
        raise PermissionError("Refusing to write inbox audit log through a symlink or reparse point.")
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.write(fd, line) != len(line):
            raise OSError("Incomplete write to inbox decision log.")
        os.fsync(fd)
    finally:
        os.close(fd)


def promote_inbox_item(
    filename: str,
    *,
    expected_sha256: str,
    actor: str,
    acknowledge_risks: bool = False,
    inbox_dir: str = INBOX_DIR,
    raw_dir: str = RAW_DIR,
    brain_dir: str = BRAIN_DIR,
) -> dict[str, object]:
    """CAS-verified promotion from quarantined 0-raw/inbox/ into 0-raw/ for Refinery processing."""
    actor_clean = str(actor or "").strip()
    if not actor_clean:
        raise ValueError("Actor identity is required to promote quarantined inbox captures.")
    expected = str(expected_sha256 or "").strip().lower()
    if len(expected) != 64:
        raise ValueError("A 64-character expected_sha256 is required for CAS inbox promotion.")

    lock_path = os.path.join(brain_dir, ".sidecar", "writer.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with review_lock(lock_path):
        safe_src = safe_file_in_dir(inbox_dir, filename, allowed_exts={".md", ".txt"}, must_exist=True)
        if not safe_src or not os.path.isfile(safe_src):
            raise FileNotFoundError(f"Quarantined inbox file not found or invalid: {filename}")
        current_sha = file_sha256(safe_src)
        if current_sha != expected:
            raise RuntimeError(
                f"CAS mismatch on quarantined file '{filename}': expected {expected}, found {current_sha}."
            )
        with open(safe_src, "r", encoding="utf-8", errors="strict") as handle:
            raw_text = handle.read()
        if len(raw_text.encode("utf-8")) > MAX_PROMOTED_BYTES:
            raise ValueError(f"Quarantined capture exceeds {MAX_PROMOTED_BYTES} byte promotion ceiling.")

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

        os.makedirs(raw_dir, exist_ok=True)
        stem, ext = os.path.splitext(os.path.basename(safe_src))
        target_ext = ".md" if ext.lower() == ".md" else ".txt"
        dest_name = f"promoted-{stem}{target_ext}"
        suffix = 2
        while True:
            dest_path = safe_file_in_dir(raw_dir, dest_name, allowed_exts={".md", ".txt"}, must_exist=False)
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
            os.path.basename(safe_src),
        )
        shutil.move(safe_src, archived_inbox_path)

        promoted_sha = file_sha256(dest_path)
        decision = {
            "schema_version": 1,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "action": "promote_inbox",
            "actor": actor_clean,
            "inbox_filename": os.path.basename(safe_src),
            "inbox_sha256": current_sha,
            "promoted_filename": dest_name,
            "promoted_sha256": promoted_sha,
            "origin": origin,
            "risk_flags": list(risks),
            "acknowledged_risks": bool(acknowledge_risks),
        }
        _append_inbox_decision(decision, brain_dir=brain_dir)
        return decision


def reject_inbox_item(
    filename: str,
    *,
    expected_sha256: str,
    actor: str,
    rationale: str = "Rejected during inbox quarantine review",
    inbox_dir: str = INBOX_DIR,
    brain_dir: str = BRAIN_DIR,
) -> dict[str, object]:
    actor_clean = str(actor or "").strip()
    if not actor_clean:
        raise ValueError("Actor identity is required to reject quarantined inbox captures.")
    expected = str(expected_sha256 or "").strip().lower()
    if len(expected) != 64:
        raise ValueError("A 64-character expected_sha256 is required for CAS inbox rejection.")

    lock_path = os.path.join(brain_dir, ".sidecar", "writer.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with review_lock(lock_path):
        safe_src = safe_file_in_dir(inbox_dir, filename, allowed_exts={".md", ".txt"}, must_exist=True)
        if not safe_src or not os.path.isfile(safe_src):
            raise FileNotFoundError(f"Quarantined inbox file not found or invalid: {filename}")
        current_sha = file_sha256(safe_src)
        if current_sha != expected:
            raise RuntimeError(
                f"CAS mismatch on quarantined file '{filename}': expected {expected}, found {current_sha}."
            )
        archived_rejected = _unique_archive_destination(
            inbox_dir,
            "rejected",
            current_sha[:12],
            os.path.basename(safe_src),
        )
        shutil.move(safe_src, archived_rejected)

        decision = {
            "schema_version": 1,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "action": "reject_inbox",
            "actor": actor_clean,
            "inbox_filename": os.path.basename(safe_src),
            "inbox_sha256": current_sha,
            "rationale": str(rationale or "").strip()[:500],
        }
        _append_inbox_decision(decision, brain_dir=brain_dir)
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
