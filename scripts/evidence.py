"""Exact quote anchors for owner-reviewed memory atoms."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re

from memory_format import split_frontmatter
from memory_review import parse_frontmatter_values, parse_scalar_list


class EvidenceValidationError(ValueError):
    """Raised when an atom's evidence cannot be tied to a durable source revision."""


ANCHOR_FIELDS = (
    "evidence_source",
    "evidence_sha256",
    "evidence_start",
    "evidence_end",
    "evidence_quote_sha256",
)


def _normalize_with_offsets(value: str) -> tuple[str, list[int]]:
    normalized: list[str] = []
    offsets: list[int] = []
    pending_space = False
    for index, character in enumerate(value):
        if character.isspace():
            pending_space = bool(normalized)
            continue
        if pending_space:
            normalized.append(" ")
            offsets.append(index)
            pending_space = False
        normalized.append(character)
        offsets.append(index)
    return "".join(normalized), offsets


def _evidence_text(content: str) -> str:
    parsed = split_frontmatter(content)
    if parsed is None:
        raise EvidenceValidationError("Atom has no valid frontmatter.")
    _, body = parsed
    lines = []
    active = False
    for line in body:
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            if active:
                break
            active = heading.group(1).strip().casefold() == "evidence"
            continue
        if active:
            lines.append(re.sub(r"^\s*>\s?", "", line))
    evidence = "\n".join(lines).strip()
    if len(evidence) >= 2 and evidence[0] == evidence[-1] and evidence[0] in {'"', "'", "“", "”", "‘", "’"}:
        evidence = evidence[1:-1].strip()
    if not evidence:
        raise EvidenceValidationError("The atom has no evidence excerpt to verify.")
    return evidence


def _source_values(content: str) -> list[str]:
    parsed = split_frontmatter(content)
    if parsed is None:
        raise EvidenceValidationError("Atom has no valid frontmatter.")
    values = parse_frontmatter_values(parsed[0]).get("sources", [])
    if isinstance(values, str):
        return parse_scalar_list(values)
    return [str(value).strip() for value in values if str(value).strip()]


def _inside_brain(path: Path, brain_dir: str) -> Path | None:
    root = Path(brain_dir).resolve()
    try:
        resolved = path.resolve(strict=True)
        if os.path.commonpath([str(root), str(resolved)]) != str(root) or not resolved.is_file():
            return None
        return resolved
    except (OSError, ValueError):
        return None


def _source_candidates(source: str, brain_dir: str) -> list[Path]:
    root = Path(brain_dir).resolve()
    relative = Path(source.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() != ".md":
        raise EvidenceValidationError(f"Unsafe or unsupported source reference: {source}")

    explicit = _inside_brain(root / relative, brain_dir)
    if explicit:
        allowed_roots = (root / "0-raw", root / "sources")
        if not any(os.path.commonpath([str(directory.resolve()), str(explicit)]) == str(directory.resolve()) for directory in allowed_roots):
            raise EvidenceValidationError(f"Source reference is outside durable source directories: {source}")
        return [explicit]

    if len(relative.parts) > 1:
        raise EvidenceValidationError(f"Named source path does not resolve inside the vault: {source}")

    basename = relative.name
    candidates: list[Path] = []
    for directory in (root / "0-raw", root / "sources"):
        if directory.is_dir():
            for path in directory.rglob(basename):
                resolved = _inside_brain(path, brain_dir)
                if resolved and resolved not in candidates:
                    candidates.append(resolved)
    if len(candidates) > 1:
        raise EvidenceValidationError(
            f"Bare source name {source!r} is ambiguous; use a vault-relative path."
        )
    return candidates


def find_evidence_anchor(content: str, brain_dir: str) -> dict[str, str | int]:
    """Find exactly quoted evidence in exactly one source file and return byte offsets."""
    evidence = _evidence_text(content)
    normalized_quote, _ = _normalize_with_offsets(evidence)
    if len(normalized_quote) < 12:
        raise EvidenceValidationError("Evidence excerpt is too short to anchor safely (minimum 12 characters).")

    sources = _source_values(content)
    if not sources:
        raise EvidenceValidationError("The atom must name at least one source file.")

    matches: list[dict[str, str | int]] = []
    for source in sources:
        for path in _source_candidates(source, brain_dir):
            raw = path.read_bytes()
            try:
                source_text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            normalized_source, offsets = _normalize_with_offsets(source_text)
            start = 0
            while True:
                start = normalized_source.find(normalized_quote, start)
                if start < 0:
                    break
                end = start + len(normalized_quote)
                source_start = offsets[start]
                source_end = offsets[end - 1] + 1
                matches.append({
                    "evidence_source": path.relative_to(Path(brain_dir).resolve()).as_posix(),
                    "evidence_sha256": hashlib.sha256(raw).hexdigest(),
                    "evidence_start": len(source_text[:source_start].encode("utf-8")),
                    "evidence_end": len(source_text[:source_end].encode("utf-8")),
                    "evidence_quote_sha256": hashlib.sha256(normalized_quote.encode("utf-8")).hexdigest(),
                })
                start += 1

    if not matches:
        raise EvidenceValidationError("The evidence excerpt does not occur verbatim in any named source revision.")
    unique = {(item["evidence_source"], item["evidence_start"], item["evidence_end"]) for item in matches}
    if len(unique) != 1:
        raise EvidenceValidationError("The evidence excerpt matches multiple source locations; make the citation unambiguous.")
    return matches[0]


def verify_evidence_anchor(content: str, anchor: dict, brain_dir: str) -> bool:
    try:
        current = find_evidence_anchor(content, brain_dir)
    except (EvidenceValidationError, OSError, ValueError):
        return False
    return all(str(current.get(field)) == str(anchor.get(field)) for field in ANCHOR_FIELDS)
