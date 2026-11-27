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


def _short_config_quote(text: str) -> bool:
    raw = str(text or "").strip()
    if not (8 <= len(raw) < 12) or any(mark in raw for mark in ".!?"):
        return False
    return len(raw.split()) <= 6


def _loose_span(text: str) -> str:
    """Ignore pipe characters and spacing so an aligned table row still matches."""
    return re.sub(r"\s+", " ", str(text or "").replace("|", " ")).strip().casefold()


def context_spans(text: str) -> list[str]:
    """Verbatim context spans. Table headers stay one span; short labels may use `` | ``."""
    raw = str(text or "").strip()
    if not raw:
        return []
    if "¦" in raw:
        chunks = [part.strip() for part in raw.split("¦") if part.strip()]
    elif raw.startswith("|") or raw.count("|") >= 2:
        chunks = [raw]
    else:
        chunks = [part.strip() for part in re.split(r"\s+\|\s+", raw) if part.strip()]
    # Skill-pack notes use ## Context as commentary, not as a source span.
    if any(span.lstrip().startswith(("- ", "* ", "`")) or "Origin pack" in span for span in chunks):
        return []
    return chunks


def span_in_source(part: str, source: str) -> bool:
    """True when part is verbatim, or the same pipe-row once spacing is ignored."""
    if part and part in source:
        return True
    loose = _loose_span(part)
    if len(loose) < 3:
        return False
    for line in source.splitlines():
        line_loose = _loose_span(line)
        if not line_loose:
            continue
        if loose == line_loose or (len(loose) >= 8 and loose in line_loose):
            return True
    return False


def _context_parts(content: str) -> list[str]:
    parsed = split_frontmatter(content)
    if parsed is None:
        return []
    from scripts.memory_review import section_text

    context = section_text("\n".join(parsed[1]), "Context")
    return context_spans(context)


def quotes_in_source(content: str, source: str) -> bool:
    """True when the evidence quote and every context span occur in this source text."""
    try:
        evidence = _evidence_text(content)
    except EvidenceValidationError:
        return False
    if not span_in_source(evidence, source):
        return False
    return all(span_in_source(part, source) for part in _context_parts(content))


def find_evidence_anchor(content: str, brain_dir: str) -> dict[str, str | int]:
    """Find exactly quoted evidence in exactly one source file and return byte offsets."""
    evidence = _evidence_text(content)
    normalized_quote, _ = _normalize_with_offsets(evidence)
    floor = 8 if _short_config_quote(evidence) else 12
    if len(normalized_quote) < floor:
        raise EvidenceValidationError("Evidence excerpt is too short to anchor safely (minimum 12 characters).")

    sources = _source_values(content)
    if not sources:
        raise EvidenceValidationError("The atom must name at least one source file.")
    context_parts = _context_parts(content)

    matches: list[dict[str, str | int]] = []
    for source in sources:
        for path in _source_candidates(source, brain_dir):
            loaded = _load_normalized_source(path)
            if loaded is None:
                continue
            source_sha256, source_text, normalized_source, offsets = loaded
            if context_parts and any(not span_in_source(part, source_text) for part in context_parts):
                continue
            for start, end in _locate_quote(source_text, normalized_source, normalized_quote):
                if not (0 <= start < end <= len(offsets)):
                    continue
                source_start = offsets[start]
                source_end = offsets[end - 1] + 1
                matches.append({
                    "evidence_source": path.relative_to(Path(brain_dir).resolve()).as_posix(),
                    "evidence_sha256": source_sha256,
                    "evidence_start": len(source_text[:source_start].encode("utf-8")),
                    "evidence_end": len(source_text[:source_end].encode("utf-8")),
                    "evidence_quote_sha256": hashlib.sha256(normalized_quote.encode("utf-8")).hexdigest(),
                })

    if not matches:
        if context_parts:
            raise EvidenceValidationError(
                "The evidence excerpt or its context span does not occur verbatim in any named source revision."
            )
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


_ENTAIL_STOP = frozenset({
    "that", "this", "with", "from", "have", "been", "were", "they", "them", "your", "into", "about",
    "when", "where", "what", "which", "their", "there", "would", "could", "should", "after", "before",
    "also", "only", "just", "than", "then", "such", "some", "more", "most", "very", "each", "both",
})


def _entail_stem(word: str) -> str:
    for suffix in ("ments", "ment", "ings", "ing", "ied", "ies", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def number_tokens(text: str) -> set[str]:
    """Digit tokens, so 02:00 and 70B cannot be treated as the same fact.

    A leading list marker such as ``1.`` is not a fact number.
    """
    cleaned = re.sub(r"(?<!\d)(?<!\.)\b\d{1,2}[.)]\s+", "", str(text or ""))
    return set(re.findall(r"\d+(?:\.\d+)?", cleaned.casefold()))


def config_tokens(text: str) -> set[str]:
    return {
        match.group(0).casefold()
        for match in re.finditer(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b", str(text or ""))
    }


def entail_stems(text: str) -> set[str]:
    return {
        _entail_stem(word)
        for word in re.findall(r"[a-z0-9]+", str(text or "").casefold())
        if len(word) >= 4 and word not in _ENTAIL_STOP
    }


def _context_label(part: str) -> str:
    """Claim prefix for a context span. A raw table header is not copied into the claim."""
    text = str(part or "").strip()
    if not text or text.startswith("|") or text.count("|") >= 2:
        return ""
    named = re.match(r"^(?:.*\s)?([A-Za-z0-9_.:-]+)\s*\{\s*$", text)
    if named:
        return named.group(1)
    if re.fullmatch(r"[A-Za-z0-9_.:-]+\s*:", text):
        return text[:-1].strip()
    return text.lstrip("#").strip()


def _locate_quote(source_text: str, normalized_source: str, normalized_quote: str) -> list[tuple[int, int]]:
    """Spans of the quote in the normalized source, including a pipe-row whose spacing differs."""
    found: list[tuple[int, int]] = []
    start = 0
    while True:
        start = normalized_source.find(normalized_quote, start)
        if start < 0:
            break
        found.append((start, start + len(normalized_quote)))
        start += 1
    if found:
        return found
    loose_quote = _loose_span(normalized_quote)
    if len(loose_quote) < 8:
        return []
    seen: set[tuple[int, int]] = set()
    for line in source_text.splitlines():
        line_loose = _loose_span(line)
        if not line_loose:
            continue
        if loose_quote != line_loose and not (len(loose_quote) >= 12 and loose_quote in line_loose and "|" in line):
            continue
        norm_line, _offsets = _normalize_with_offsets(line.strip())
        start = 0
        while True:
            start = normalized_source.find(norm_line, start)
            if start < 0:
                break
            span = (start, start + len(norm_line))
            if span not in seen:
                seen.add(span)
                found.append(span)
            start += 1
    return found


def fact_portion(claim: str, context: str = "") -> str:
    """Drop heading, block-label, or speaker prefixes that are recorded as context."""
    text = str(claim or "").strip()
    for part in context_spans(context):
        labels = [label for label in (_context_label(part), part) if label]
        for label in labels:
            for prefix in (f"{label}:", label):
                if text.casefold().startswith(prefix.casefold()):
                    rest = text[len(prefix) :].lstrip(" :-")
                    if rest:
                        text = rest
                        break
            else:
                continue
            break
    return text


def claim_entailed_by_evidence(claim: str, evidence: str, context: str = "") -> bool:
    """True when the fact portion's numbers, settings, and most content words are in its evidence.

    A heading, block label, or speaker recorded as context does not have to be repeated in the quote.
    """
    claim_text = fact_portion(claim, context)
    evidence_text = str(evidence or "").strip()
    context_text = str(context or "")
    if not claim_text or not evidence_text:
        return False
    evidence_numbers = number_tokens(evidence_text)
    claim_numbers = number_tokens(claim_text) - number_tokens(context_text)
    if not claim_numbers <= evidence_numbers:
        return False
    evidence_fold = evidence_text.casefold()
    claim_config = config_tokens(claim_text) - config_tokens(context_text)
    if any(token not in evidence_fold for token in claim_config):
        return False
    claim_stems = entail_stems(claim_text) - entail_stems(context_text)
    if not claim_stems:
        return bool(claim_numbers or claim_config or len(claim_text.split()) <= 8)
    evidence_stems = entail_stems(evidence_text)
    evidence_blob = " ".join(_entail_stem(word) for word in re.findall(r"[a-z0-9]+", evidence_fold))
    hits = sum(1 for stem in claim_stems if stem in evidence_stems or stem in evidence_blob)
    needed = max(1, int(round(len(claim_stems) * 0.7)))
    return hits >= needed


def verify_evidence_anchor(content: str, anchor: dict, brain_dir: str) -> bool:
    try:
        current = find_evidence_anchor(content, brain_dir)
    except (EvidenceValidationError, OSError, ValueError):
        return False
    return all(str(current.get(field)) == str(anchor.get(field)) for field in ANCHOR_FIELDS)
