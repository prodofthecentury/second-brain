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
    vf_raw = str(valid_from or "").strip() if valid_from else ""
    vu_raw = str(valid_until or "").strip() if valid_until else ""
    if not vf_raw and not vu_raw and as_of is None:
        return {
            "status": "active",
            "active": True,
            "valid_from": "",
            "valid_until": "",
        }
    ref_dt = _resolve_as_of_utc(as_of)
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
    """Return fenced markdown blocks, supporting nested code fences and inline prefixing."""
    lines = (text or "").splitlines(keepends=True)
    blocks: list[str] = []
    in_block = False
    fence_char = ""
    fence_len = 0
    inner_depth = 0
    current_lines: list[str] = []

    for line in lines:
        if not in_block:
            m = re.search(r"([`~]{3,})(.*)$", line)
            if m:
                chars, info = m.group(1), m.group(2).strip().lower()
                clean_info = re.sub(r"[^a-z0-9_-].*$", "", info)
                if clean_info in {"markdown", "md", ""}:
                    in_block = True
                    fence_char = chars[0]
                    fence_len = len(chars)
                    inner_depth = 0
                    current_lines = []
                    continue
        else:
            m_close = re.match(r"^\s*([`~]{3,})\s*$", line)
            if m_close and m_close.group(1)[0] == fence_char and len(m_close.group(1)) >= fence_len:
                if inner_depth > 0:
                    inner_depth -= 1
                    current_lines.append(line)
                else:
                    b = "".join(current_lines).strip()
                    if b:
                        blocks.append(b)
                    in_block = False
                    current_lines = []
                continue

            m_inner = re.match(r"^\s*([`~]{3,})\s*([a-zA-Z0-9_-]+)", line)
            if m_inner:
                inner_depth += 1
                current_lines.append(line)
                continue

            current_lines.append(line)

    if in_block and current_lines:
        b = "".join(current_lines).strip()
        if b:
            blocks.append(b)

    if blocks:
        return blocks

    stripped = (text or "").strip()
    return [stripped] if stripped else []


def parse_memory_blocks(text: str) -> list[str]:
    """Extract fenced memory documents, gracefully handling unlabelled inner code blocks."""
    lines = (text or "").splitlines(keepends=True)
    blocks: list[str] = []
    in_block = False
    inner_code_fence = False
    current_lines: list[str] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not in_block:
            if re.search(r"```(?:markdown|md)?\s*$", stripped, re.IGNORECASE):
                has_fm = any(lines[j].strip() == "---" or "## Claim" in lines[j] for j in range(i + 1, min(i + 8, len(lines))))
                if has_fm:
                    in_block = True
                    inner_code_fence = False
                    current_lines = []
                    continue
        else:
            if re.match(r"^```\s*$", stripped):
                cur_text = "".join(current_lines)
                has_all_sections = "## Claim" in cur_text and "## Evidence" in cur_text and "## Why It Matters" in cur_text

                next_non_empty = ""
                for j in range(i + 1, len(lines)):
                    if lines[j].strip():
                        next_non_empty = lines[j].strip()
                        break

                is_boundary = (
                    not next_non_empty
                    or "<!-- MEMORY_BOUNDARY -->" in next_non_empty
                    or re.search(r"```(?:markdown|md)?", next_non_empty, re.IGNORECASE)
                    or next_non_empty.startswith("---")
                    or next_non_empty.startswith("#")
                )

                if has_all_sections and (is_boundary or inner_code_fence):
                    b = "".join(current_lines).strip()
                    if b:
                        blocks.append(b)
                    in_block = False
                    inner_code_fence = False
                    current_lines = []
                    continue
                else:
                    inner_code_fence = not inner_code_fence
                    current_lines.append(line)
                    continue

            elif re.match(r"^```[a-zA-Z0-9_-]+\s*$", stripped):
                inner_code_fence = True
                current_lines.append(line)
                continue

            current_lines.append(line)

    if in_block and current_lines:
        b = "".join(current_lines).strip()
        if b:
            blocks.append(b)

    return blocks


def split_memory_candidates(text: str) -> list[str]:
    """Split one model output into candidate memory documents."""
    mem_blocks = parse_memory_blocks(text)
    if mem_blocks:
        return mem_blocks

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

    frontmatter, body = parsed
    id_val = frontmatter_value(frontmatter, "id")
    if not id_val or id_val.strip().upper() in {"NONE", "NULL"}:
        return False

    body_text = "\n".join(body).lstrip()
    if not (body_text.startswith("## Claim") and "## Evidence" in body_text):
        return False

    # Check evidence section is not empty
    ev_idx = body_text.find("## Evidence")
    after_ev = body_text[ev_idx + len("## Evidence"):].lstrip()
    next_sec = re.search(r"^##\s+", after_ev, re.MULTILINE)
    ev_content = (after_ev[:next_sec.start()] if next_sec else after_ev).strip()
    if not ev_content or len(ev_content) < 5:
        return False

    # Check claim section is not empty
    claim_idx = body_text.find("## Claim")
    after_claim = body_text[claim_idx + len("## Claim"):ev_idx].strip()
    if not after_claim or len(after_claim) < 5:
        return False

    # Reject blatant dummy placeholders
    dummy_patterns = {
        "a single clear asserting claim sentence",
        "this chunk contains only boilerplate",
        "exact quote copied verbatim from raw capture",
        "no standalone durable claim exists in this chunk",
    }
    if after_claim.lower().rstrip(".;:") in dummy_patterns or ev_content.lower().rstrip(".;:") in dummy_patterns:
        return False

    return True


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
