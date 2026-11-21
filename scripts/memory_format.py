# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""
Pure formatting helpers for cognitive memory documents.

These functions intentionally avoid Ollama, ChromaDB, and filesystem imports so
they can be tested by the lightweight smoke suite.
"""

from __future__ import annotations

import datetime
import json
import re


MEMORY_BOUNDARY = "<!-- MEMORY_BOUNDARY -->"
VALID_MEMORY_TYPES = {"semantic", "episodic", "procedural", "self"}
VALID_REVIEW_STATUSES = {"pending", "accepted", "needs_edit", "rejected", "merged"}
VALID_ORIGINS = {
    "user_local",
    "mcp_local",
    "api_local",
    "web_clipper",
    "web_remote",
    "cognitive_experience",
    "untrusted_inbox",
    "imported_unknown",
}
_NON_USER_ORIGINS = {
    "mcp_local",
    "api_local",
    "web_clipper",
    "web_remote",
    "cognitive_experience",
    "untrusted_inbox",
    "imported_unknown",
}
VALID_CITATION_INTEGRITY = {"anchored", "missing", "hash_mismatch", "invalid_offset"}
VALID_CLAIM_STATES = {
    "candidate",
    "source_anchored",
    "human_reviewed",
    "superseded",
    "rejected",
    "quarantined",
}
VALID_EPISTEMIC_LABELS = {"observation", "preference", "hypothesis", "claim", "procedure"}

CODE_BLOCK_RE = re.compile(r"```(?:markdown|md)?\s*\n(.*?)\n```", re.IGNORECASE | re.DOTALL)
BOUNDARY_RE = re.compile(r"^\s*<!--\s*MEMORY_BOUNDARY\s*-->\s*$", re.IGNORECASE | re.MULTILINE)


def today() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")


_ISO_DATE_ONLY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def parse_temporal_bound(raw: str | None, *, is_end: bool = False) -> datetime.datetime | None:
    """Parse an ISO-8601 date (YYYY-MM-DD) or timestamp into a UTC datetime.

    Date-only bounds are inclusive: 00:00:00 UTC for ``valid_from`` (``is_end=False``)
    and 23:59:59.999999 UTC for ``valid_until`` (``is_end=True``).
    """
    text = str(raw or "").strip().strip("\"'")
    if not text:
        return None
    m = _ISO_DATE_ONLY_RE.match(text)
    if m:
        d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if is_end:
            return datetime.datetime(d.year, d.month, d.day, 23, 59, 59, 999999, tzinfo=datetime.timezone.utc)
        return datetime.datetime(d.year, d.month, d.day, 0, 0, 0, 0, tzinfo=datetime.timezone.utc)
    iso_norm = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    dt = datetime.datetime.fromisoformat(iso_norm)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def normalize_temporal_bound_str(raw: str | None) -> str:
    """Validate and canonicalize an optional temporal bound string ('' when unset)."""
    text = str(raw or "").strip().strip("\"'")
    if not text:
        return ""
    m = _ISO_DATE_ONLY_RE.match(text)
    if m:
        d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return d.isoformat()
    dt = parse_temporal_bound(text, is_end=False)
    assert dt is not None
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve_as_of_utc(as_of: str | datetime.date | datetime.datetime | None = None) -> datetime.datetime:
    if as_of is None or (isinstance(as_of, str) and not as_of.strip()):
        return datetime.datetime.now(datetime.timezone.utc)
    if isinstance(as_of, datetime.datetime):
        return as_of.replace(tzinfo=datetime.timezone.utc) if as_of.tzinfo is None else as_of.astimezone(datetime.timezone.utc)
    if isinstance(as_of, datetime.date):
        return datetime.datetime(as_of.year, as_of.month, as_of.day, 12, 0, 0, tzinfo=datetime.timezone.utc)
    raw = str(as_of).strip().strip("\"'")
    m = _ISO_DATE_ONLY_RE.match(raw)
    if m:
        d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return datetime.datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=datetime.timezone.utc)
    dt = parse_temporal_bound(raw, is_end=False)
    assert dt is not None
    return dt


def evaluate_temporal_validity(
    valid_from: str | None,
    valid_until: str | None,
    *,
    as_of: str | datetime.date | datetime.datetime | None = None,
) -> dict[str, object]:
    """Evaluate bi-temporal fact validity at ``as_of`` (default: current UTC instant).

    Returns ``{"status": "active"|"expired"|"not_yet_valid"|"invalid_window", "active": bool, ...}``.
    """
    ref_dt = _resolve_as_of_utc(as_of)
    vf_raw = str(valid_from or "").strip()
    vu_raw = str(valid_until or "").strip()
    try:
        start_dt = parse_temporal_bound(vf_raw, is_end=False) if vf_raw else None
        end_dt = parse_temporal_bound(vu_raw, is_end=True) if vu_raw else None
    except ValueError:
        return {
            "status": "invalid_window",
            "active": False,
            "valid_from": vf_raw,
            "valid_until": vu_raw,
        }
    if start_dt is not None and end_dt is not None and start_dt > end_dt:
        return {
            "status": "invalid_window",
            "active": False,
            "valid_from": vf_raw,
            "valid_until": vu_raw,
        }
    if start_dt is not None and ref_dt < start_dt:
        return {
            "status": "not_yet_valid",
            "active": False,
            "valid_from": vf_raw,
            "valid_until": vu_raw,
        }
    if end_dt is not None and ref_dt > end_dt:
        return {
            "status": "expired",
            "active": False,
            "valid_from": vf_raw,
            "valid_until": vu_raw,
        }
    return {
        "status": "active",
        "active": True,
        "valid_from": vf_raw,
        "valid_until": vu_raw,
    }


def temporal_windows_overlap(
    valid_from_a: str | None,
    valid_until_a: str | None,
    valid_from_b: str | None,
    valid_until_b: str | None,
) -> bool:
    """Return True if two [valid_from, valid_until] windows overlap in time."""
    try:
        sa = parse_temporal_bound(valid_from_a, is_end=False)
        ea = parse_temporal_bound(valid_until_a, is_end=True)
        sb = parse_temporal_bound(valid_from_b, is_end=False)
        eb = parse_temporal_bound(valid_until_b, is_end=True)
    except ValueError:
        return False
    if (sa is not None and ea is not None and sa > ea) or (sb is not None and eb is not None and sb > eb):
        return False
    if ea is not None and sb is not None and ea < sb:
        return False
    if eb is not None and sa is not None and eb < sa:
        return False
    return True


def markdown_blocks(text: str) -> list[str]:
    """Return fenced markdown blocks, or the raw text if no fences exist."""
    blocks = [match.group(1).strip() for match in CODE_BLOCK_RE.finditer(text or "")]
    if blocks:
        return [block for block in blocks if block]

    stripped = (text or "").strip()
    return [stripped] if stripped else []


def split_memory_candidates(text: str) -> list[str]:
    """Split one model output into candidate memory documents."""
    candidates = []
    for block in markdown_blocks(text):
        for part in BOUNDARY_RE.split(block):
            candidate = part.strip()
            if candidate:
                candidates.append(candidate)
    return candidates


def has_frontmatter(content: str) -> bool:
    lines = (content or "").splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    return any(line.strip() == "---" for line in lines[1:])


def split_frontmatter(content: str) -> tuple[list[str], list[str]] | None:
    """Return frontmatter lines and body lines, excluding boundary markers."""
    lines = (content or "").strip().splitlines()
    if not lines or lines[0].strip() != "---":
        return None

    end_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break

    if end_index is None:
        return None

    return lines[1:end_index], lines[end_index + 1 :]


def frontmatter_has_key(frontmatter: list[str], key: str) -> bool:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:")
    return any(pattern.match(line) for line in frontmatter)


def frontmatter_value(frontmatter: list[str], key: str) -> str:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:\s*(.*)$")
    for line in frontmatter:
        match = pattern.match(line)
        if match:
            return match.group(1).strip().strip("\"'")
    return ""


def insert_after_key(frontmatter: list[str], after_key: str, new_lines: list[str]) -> list[str]:
    pattern = re.compile(rf"^\s*{re.escape(after_key)}\s*:")
    for index, line in enumerate(frontmatter):
        if pattern.match(line):
            insert_index = index + 1
            while insert_index < len(frontmatter):
                next_line = frontmatter[insert_index]
                if next_line.strip() and not next_line.startswith((" ", "\t")):
                    break
                insert_index += 1
            return frontmatter[:insert_index] + new_lines + frontmatter[insert_index:]
    return frontmatter + new_lines


def add_missing_field(frontmatter: list[str], key: str, value: str, after_key: str) -> list[str]:
    if frontmatter_has_key(frontmatter, key):
        return frontmatter
    return insert_after_key(frontmatter, after_key, [f"{key}: {value}"])


def set_scalar_field(frontmatter: list[str], key: str, value: str, after_key: str) -> list[str]:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:")
    updated = []
    found = False
    skipping_continuation = False
    for line in frontmatter:
        if pattern.match(line):
            if not found:
                updated.append(f"{key}: {value}")
                found = True
            skipping_continuation = True
            continue
        if re.match(r"^\s*[A-Za-z0-9_-]+\s*:", line):
            skipping_continuation = False
        elif skipping_continuation and line.startswith((" ", "\t")):
            continue
        updated.append(line)
    return updated if found else insert_after_key(updated, after_key, [f"{key}: {value}"])


def remove_frontmatter_fields(frontmatter: list[str], keys: set[str]) -> list[str]:
    """Remove selected top-level fields, including any indented YAML continuation."""
    kept = []
    skipping = False
    wanted = {key.casefold() for key in keys}
    for line in frontmatter:
        match = re.match(r"^([A-Za-z0-9_-]+):", line)
        if match:
            skipping = match.group(1).casefold() in wanted
        elif not line.startswith((" ", "\t")):
            skipping = False
        if not skipping:
            kept.append(line)
    return kept


def set_string_list_field(frontmatter: list[str], key: str, values: list[str], after_key: str) -> list[str]:
    """Replace a YAML string-list field and remove any prior scalar/list form."""
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:")
    kept = []
    skipping = False
    for line in frontmatter:
        if pattern.match(line):
            skipping = True
            continue
        if re.match(r"^\s*[A-Za-z0-9_-]+\s*:", line):
            skipping = False
        elif skipping and line.startswith((" ", "\t")):
            continue
        kept.append(line)
    serialized = [f"{key}:"] + [f"  - {json.dumps(str(value), ensure_ascii=False)}" for value in values]
    if not values:
        serialized = [f"{key}: []"]
    return insert_after_key(kept, after_key, serialized)


def normalize_enum_field(frontmatter: list[str], key: str, valid_values: set[str], default: str) -> list[str]:
    value = frontmatter_value(frontmatter, key)
    if value and value in valid_values:
        return frontmatter
    return set_scalar_field(frontmatter, key, default, key)


def normalize_memory_document(
    content: str,
    source_filename: str,
    current_date: str | None = None,
    origin: str = "user_local",
) -> str:
    """Ensure a memory document has the durable Cognitive Memory fields."""
    current_date = current_date or today()
    stamped_origin = origin if origin in VALID_ORIGINS else "imported_unknown"
    parsed = split_frontmatter(content)

    if parsed is None:
        frontmatter = [
            f"id: {current_date}-memory",
            "type: atom",
            "memory_type: semantic",
            "certainty: tentative",
            f"created: {current_date}",
            f"last-reviewed: {current_date}",
            "importance: 0.50",
            "confidence: 0.50",
            "sources:",
            f"  - {source_filename}",
            "links: []",
            "tags: []",
            "contexts: []",
            "supersedes: []",
            "superseded_by: []",
            "contested_by: []",
            f"origin: {stamped_origin}",
            "citation_integrity: missing",
            "claim_state: candidate",
            "epistemic_label: claim",
            "review_status: pending",
        ]
        body = (content or "").strip().splitlines()
        return render_memory_document(frontmatter, body)

    frontmatter, body = parsed

    frontmatter = add_missing_field(frontmatter, "type", "atom", "id")
    frontmatter = add_missing_field(frontmatter, "memory_type", "semantic", "type")
    memory_type = frontmatter_value(frontmatter, "memory_type")
    if memory_type and memory_type not in VALID_MEMORY_TYPES:
        frontmatter = [
            "memory_type: semantic" if re.match(r"^\s*memory_type\s*:", line) else line
            for line in frontmatter
        ]

    frontmatter = add_missing_field(frontmatter, "certainty", "tentative", "memory_type")
    frontmatter = add_missing_field(frontmatter, "created", current_date, "certainty")
    frontmatter = add_missing_field(frontmatter, "last-reviewed", current_date, "created")
    frontmatter = add_missing_field(frontmatter, "importance", "0.50", "last-reviewed")
    frontmatter = add_missing_field(frontmatter, "confidence", "0.50", "importance")

    if not frontmatter_has_key(frontmatter, "sources"):
        frontmatter = insert_after_key(frontmatter, "confidence", ["sources:", f"  - {source_filename}"])

    frontmatter = add_missing_field(frontmatter, "links", "[]", "sources")
    frontmatter = add_missing_field(frontmatter, "tags", "[]", "links")
    frontmatter = add_missing_field(frontmatter, "contexts", "[]", "tags")
    frontmatter = add_missing_field(frontmatter, "supersedes", "[]", "contexts")
    frontmatter = add_missing_field(frontmatter, "superseded_by", "[]", "supersedes")
    frontmatter = add_missing_field(frontmatter, "contested_by", "[]", "superseded_by")
    frontmatter = set_scalar_field(frontmatter, "origin", stamped_origin, "contested_by")
    # Model/import output is untrusted. Lifecycle and evidence anchors are
    # assigned only by the local review code after verifying source bytes.
    frontmatter = remove_frontmatter_fields(frontmatter, {
        "evidence_source", "evidence_sha256", "evidence_start", "evidence_end",
        "evidence_quote_sha256", "epistemic_contract",
    })
    frontmatter = set_scalar_field(frontmatter, "citation_integrity", "missing", "origin")
    frontmatter = set_scalar_field(frontmatter, "claim_state", "candidate", "citation_integrity")
    frontmatter = add_missing_field(frontmatter, "epistemic_label", "claim", "claim_state")
    frontmatter = set_scalar_field(frontmatter, "review_status", "pending", "epistemic_label")
    frontmatter = normalize_enum_field(frontmatter, "epistemic_label", VALID_EPISTEMIC_LABELS, "claim")
    frontmatter = normalize_enum_field(frontmatter, "review_status", VALID_REVIEW_STATUSES, "pending")

    raw_vf = frontmatter_value(frontmatter, "valid_from")
    raw_vu = frontmatter_value(frontmatter, "valid_until")
    if raw_vf or raw_vu:
        try:
            norm_vf = normalize_temporal_bound_str(raw_vf)
            norm_vu = normalize_temporal_bound_str(raw_vu)
            tv_check = evaluate_temporal_validity(norm_vf, norm_vu)
            if tv_check["status"] == "invalid_window":
                frontmatter = remove_frontmatter_fields(frontmatter, {"valid_from", "valid_until"})
            else:
                if norm_vf:
                    frontmatter = set_scalar_field(frontmatter, "valid_from", norm_vf, "last-reviewed")
                else:
                    frontmatter = remove_frontmatter_fields(frontmatter, {"valid_from"})
                if norm_vu:
                    frontmatter = set_scalar_field(
                        frontmatter,
                        "valid_until",
                        norm_vu,
                        "valid_from" if norm_vf else "last-reviewed",
                    )
                else:
                    frontmatter = remove_frontmatter_fields(frontmatter, {"valid_until"})
        except ValueError:
            frontmatter = remove_frontmatter_fields(frontmatter, {"valid_from", "valid_until"})

    return render_memory_document(frontmatter, body)


def capture_origin(raw_content: str, source_filename: str = "") -> str:
    """Derive provenance from the intake channel, never from generated atom YAML."""
    if str(source_filename).lower().endswith(".intake"):
        return "web_remote"
    text = (raw_content or "").lstrip("\ufeff")
    comments = re.findall(
        r"<!--\s*second_brain_origin\s*:(.*?)-->",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for raw_tag in comments:
        tag = raw_tag.strip()
        if tag in _NON_USER_ORIGINS:
            return tag
        if tag != "user_local":
            return "imported_unknown"
    if not comments and re.search(r"<!--[^>]*\bsecond_brain_origin\b[^>]*-->", text, flags=re.IGNORECASE):
        return "imported_unknown"
    return "user_local"


def render_memory_document(frontmatter: list[str], body: list[str]) -> str:
    body_text = "\n".join(body).strip()
    document = "---\n" + "\n".join(frontmatter).strip() + "\n---"
    if body_text:
        document += "\n\n" + body_text
    return document.rstrip() + "\n"


def is_memory_document(content: str) -> bool:
    parsed = split_frontmatter(content)
    if parsed is None:
        return False

    _, body = parsed
    body_text = "\n".join(body).lstrip()
    return body_text.startswith("## Claim") and "## Evidence" in body_text


def extract_memory_documents(
    model_output: str,
    source_filename: str,
    current_date: str | None = None,
) -> list[str]:
    """Extract and normalize memory documents from a model response."""
    documents = []
    for candidate in split_memory_candidates(model_output):
        normalized = normalize_memory_document(
            candidate,
            source_filename=source_filename,
            current_date=current_date,
        )
        if is_memory_document(normalized):
            documents.append(normalized)
    return documents
