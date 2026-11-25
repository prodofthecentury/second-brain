"""Exact quote anchors for owner-reviewed memory atoms."""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import os
from pathlib import Path
import re
import threading

from scripts.memory_format import split_frontmatter
from scripts.memory_review import parse_frontmatter_values, parse_scalar_list


class EvidenceValidationError(ValueError):
    """Raised when an atom's evidence cannot be tied to a durable source revision."""


ANCHOR_FIELDS = (
    "evidence_source",
    "evidence_sha256",
    "evidence_start",
    "evidence_end",
    "evidence_quote_sha256",
)

_SOURCE_NORM_LOCK = threading.Lock()
# Keyed by raw-bytes SHA-256 so a cache hit is cryptographically exact, never stat-trusted.
_SOURCE_NORM_CACHE: dict[str, tuple[str, str, list[int]]] = {}
_MAX_SOURCE_CACHE = 4096


def _load_normalized_source(path: Path) -> tuple[str, str, str, list[int]] | None:
    """Return (sha256, source_text, normalized_source, offsets); raw bytes are always re-hashed."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(raw).hexdigest()
    with _SOURCE_NORM_LOCK:
        cached = _SOURCE_NORM_CACHE.get(digest)
    if cached is not None:
        return digest, cached[0], cached[1], cached[2]
    try:
        source_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    normalized_source, offsets = _normalize_with_offsets(source_text)
    with _SOURCE_NORM_LOCK:
        if len(_SOURCE_NORM_CACHE) >= _MAX_SOURCE_CACHE:
            _SOURCE_NORM_CACHE.clear()
        _SOURCE_NORM_CACHE[digest] = (source_text, normalized_source, offsets)
    return digest, source_text, normalized_source, offsets


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


_EVIDENCE_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_BLOCKQUOTE_PREFIX_RE = re.compile(r"^\s*>\s?")
_QUOTE_PAIRS = {('"', '"'), ("'", "'"), ("\u201c", "\u201d"), ("\u2018", "\u2019")}


def extract_evidence_from_body_lines(body: Sequence[str]) -> str:
    """Extract and unquote the ## Evidence section text from split body lines."""
    lines: list[str] = []
    active = False
    for line in body:
        heading = _EVIDENCE_HEADING_RE.match(line)
        if heading:
            if active:
                break
            active = heading.group(1).strip().casefold() == "evidence"
            continue
        if active:
            lines.append(_BLOCKQUOTE_PREFIX_RE.sub("", line))
    evidence = "\n".join(lines).strip()
    if len(evidence) >= 2 and (evidence[0], evidence[-1]) in _QUOTE_PAIRS:
        evidence = evidence[1:-1].strip()
    return evidence


def has_cached_normalized_source(source_sha256: str) -> bool:
    """Return True if the normalized form of this verified source SHA-256 is already cached in memory."""
    if not source_sha256:
        return False
    with _SOURCE_NORM_LOCK:
        return source_sha256 in _SOURCE_NORM_CACHE


def _evidence_text(content: str) -> str:
    parsed = split_frontmatter(content)
    if parsed is None:
        raise EvidenceValidationError("Atom has no valid frontmatter.")
    _, body = parsed
    evidence = extract_evidence_from_body_lines(body)
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
            loaded = _load_normalized_source(path)
            if loaded is None:
                continue
            source_sha256, source_text, normalized_source, offsets = loaded
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
                    "evidence_sha256": source_sha256,
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


def _normalized_source_from_bytes(source_sha256: str, raw: bytes | None = None) -> tuple[str, str, list[int]] | None:
    with _SOURCE_NORM_LOCK:
        cached = _SOURCE_NORM_CACHE.get(source_sha256)
    if cached is not None:
        return cached
    if raw is None:
        return None
    try:
        source_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    normalized_source, offsets = _normalize_with_offsets(source_text)
    with _SOURCE_NORM_LOCK:
        if len(_SOURCE_NORM_CACHE) >= _MAX_SOURCE_CACHE:
            _SOURCE_NORM_CACHE.clear()
        _SOURCE_NORM_CACHE[source_sha256] = (source_text, normalized_source, offsets)
    return source_text, normalized_source, offsets


def verify_evidence_anchor_known_source(
    content: str,
    anchor: dict,
    *,
    source_rel: str,
    source_sha256: str,
    source_bytes: bytes | None = None,
    preparsed_evidence: str | None = None,
    preparsed_sources: Sequence[str] | None = None,
) -> bool:
    """Verify anchor fields against already-validated, hash-verified source bytes without extra disk I/O."""
    clean_rel = source_rel.replace("\\", "/").strip()
    if not (clean_rel.startswith("0-raw/") or clean_rel.startswith("sources/")) or not clean_rel.lower().endswith(".md"):
        return False
    if str(anchor.get("evidence_source") or "") != clean_rel:
        return False
    if str(anchor.get("evidence_sha256") or "") != source_sha256:
        return False
    try:
        if preparsed_evidence is not None:
            if not preparsed_evidence:
                return False
            evidence = preparsed_evidence
        else:
            evidence = _evidence_text(content)
        sources = list(preparsed_sources) if preparsed_sources is not None else _source_values(content)
    except EvidenceValidationError:
        return False
    if len(sources) != 1:
        return False
    src_ref = sources[0].replace("\\", "/").strip()
    if src_ref != clean_rel:
        return False
    normalized_quote, _ = _normalize_with_offsets(evidence)
    if len(normalized_quote) < 12:
        return False
    quote_sha = hashlib.sha256(normalized_quote.encode("utf-8")).hexdigest()
    if str(anchor.get("evidence_quote_sha256") or "") != quote_sha:
        return False
    loaded = _normalized_source_from_bytes(source_sha256, source_bytes)
    if loaded is None:
        return False
    source_text, normalized_source, offsets = loaded
    first = normalized_source.find(normalized_quote, 0)
    if first < 0:
        return False
    if normalized_source.find(normalized_quote, first + 1) >= 0:
        return False
    end = first + len(normalized_quote)
    source_start = offsets[first]
    source_end = offsets[end - 1] + 1
    byte_start = len(source_text[:source_start].encode("utf-8"))
    byte_end = len(source_text[:source_end].encode("utf-8"))
    return str(anchor.get("evidence_start")) == str(byte_start) and str(anchor.get("evidence_end")) == str(byte_end)


def verify_evidence_anchor(content: str, anchor: dict, brain_dir: str) -> bool:
    try:
        current = find_evidence_anchor(content, brain_dir)
    except (EvidenceValidationError, OSError, ValueError):
        return False
    return all(str(current.get(field)) == str(anchor.get(field)) for field in ANCHOR_FIELDS)
