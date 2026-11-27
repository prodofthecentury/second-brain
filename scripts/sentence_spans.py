"""Sentence boundaries and fact rows for messy notes.

Abbreviations, decimals, versions, initials, IPs, and paths do not split a sentence.
The capture policy supplies the abbreviation list and the rejection rules.
"""

from __future__ import annotations

import re

from scripts.capture_policy import load_policy, rejection_reasons

_DOT = "\uE000"


def _protect(text: str) -> str:
    policy = load_policy()
    names = "|".join(re.escape(item) for item in policy["abbreviations"])

    def dots(match: re.Match[str]) -> str:
        return match.group(0).replace(".", _DOT)

    protected = text
    protected = re.sub(r"\b(?:e\.g|i\.e)\.", dots, protected, flags=re.IGNORECASE)
    protected = re.sub(rf"\b(?:{names})\.", dots, protected, flags=re.IGNORECASE)
    protected = re.sub(r"\b\d+(?:\.\d+)+", dots, protected)
    protected = re.sub(r"\b[A-Z]\.(?=\s*[A-Z])", dots, protected)
    protected = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", dots, protected)
    protected = re.sub(r"[A-Za-z]:\\(?:[^\\\s]+\\)*[^\s\\]+", dots, protected)
    return protected


def _restore(text: str) -> str:
    return text.replace(_DOT, ".")


def sentence_spans(source: str) -> list[tuple[int, int]]:
    """Half-open spans of sentences. Adjacent subject-less fragments are joined."""
    if not source:
        return []
    protected = _protect(source)
    spans: list[tuple[int, int]] = []
    start = 0
    for match in re.finditer(r"[.!?]+(?=\s)|\n+", protected):
        token = match.group(0)
        if token.startswith("\n"):
            end = match.start()
        else:
            end = match.end() - len(token) + 1
            rest = protected[match.end() :].strip()
            if re.fullmatch(r"(?:\[(?:V|N|P)\d+\]\s*)+", rest):
                continue
        piece = source[start:end]
        if piece.strip():
            spans.append((start, end))
        start = match.end()
    if source[start:].strip():
        spans.append((start, len(source)))
    return _join_subjectless(source, spans)


def _join_subjectless(source: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    starters = {item.casefold() for item in load_policy()["subjectless_starts"]}
    joined: list[tuple[int, int]] = []
    for start, end in spans:
        text = source[start:end].strip()
        first = re.findall(r"[A-Za-z0-9']+", text)
        gap = source[joined[-1][1] : start] if joined else "\n\n"
        if (
            joined
            and first
            and first[0].casefold() in starters
            and len(text.split()) <= 12
            and "\n\n" not in gap
        ):
            prev_start, _prev_end = joined[-1]
            joined[-1] = (prev_start, end)
        else:
            joined.append((start, end))
    return joined


def split_sentences(text: str) -> list[str]:
    return [text[start:end].strip() for start, end in sentence_spans(text) if text[start:end].strip()]


def _line_spans(source: str) -> list[tuple[int, int, str]]:
    rows: list[tuple[int, int, str]] = []
    offset = 0
    for line in source.splitlines(keepends=True):
        rows.append((offset, offset + len(line), line))
        offset += len(line)
    return rows


def _is_separator(line: str) -> bool:
    stripped = line.strip()
    return bool(re.match(r"^\|?\s*:?-{3,}:?(?:\s*\|\s*:?-{3,}:?)*\s*\|?\s*$", stripped))


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _code_and_table_rows(source: str, consumed: set[int]) -> list[dict]:
    rows: list[dict] = []
    lines = _line_spans(source)
    index = 0
    while index < len(lines):
        _start, _end, line = lines[index]
        if line.strip().startswith("```"):
            consumed.add(index)
            index += 1
            body: list[tuple[int, str]] = []
            while index < len(lines) and not lines[index][2].strip().startswith("```"):
                consumed.add(index)
                body.append((index, lines[index][2]))
                index += 1
            if index < len(lines):
                consumed.add(index)
            label = ""
            for _body_index, body_line in body:
                stripped = body_line.strip()
                named = re.match(r"^([A-Za-z0-9_.:-]+)\s*\{\s*$", stripped)
                if named:
                    label = named.group(1)
                    continue
                if not stripped or stripped in {"{", "}", "};"}:
                    continue
                evidence = stripped
                if evidence not in source:
                    continue
                claim = f"{label}: {evidence}" if label else evidence
                support = f"{label} {evidence}" if label else evidence
                rows.append(_row(claim, evidence, support, "Extracted from a config block."))
            index += 1
            continue
        index += 1

    index = 0
    while index < len(lines):
        if index in consumed or not _is_table_line(lines[index][2]):
            index += 1
            continue
        header = _cells(lines[index][2])
        consumed.add(index)
        index += 1
        if index < len(lines) and _is_separator(lines[index][2]):
            consumed.add(index)
            index += 1
        while index < len(lines) and _is_table_line(lines[index][2]) and not _is_separator(lines[index][2]):
            consumed.add(index)
            cells = _cells(lines[index][2])
            evidence = lines[index][2].strip()
            parts = []
            for col, cell in enumerate(cells):
                name = header[col] if col < len(header) else f"column {col + 1}"
                if cell:
                    parts.append(f"{name} {cell}")
            claim = ", ".join(parts)
            if header and cells and cells[0]:
                claim = f"{header[0]} {cells[0]}: " + ", ".join(parts[1:])
            support = " ".join(header) + " " + evidence
            if evidence in source and claim:
                rows.append(_row(claim, evidence, support, "Extracted from a table row."))
            index += 1
    return rows


def _row(claim: str, evidence: str, support: str, why: str) -> dict:
    return {
        "title": "",
        "claim": claim.strip(),
        "evidence": evidence.strip(),
        "support": support.strip(),
        "why": why,
        "memory_type": "semantic",
    }


def _heading_context(source: str) -> str:
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        title = stripped.lstrip("#").strip()
        if re.search(r"\b\d{1,2}\s+[A-Z][a-z]{2,8}\s+\d{4}\b", title):
            return title
    return ""


def _bullet(line: str) -> tuple[str, str] | None:
    match = re.match(r"^\s*[-*]\s+(?:\[(?:[ xX])\]\s+)?(.*)$", line.strip())
    if not match:
        return None
    body = match.group(1).strip()
    speaker = re.match(r"^([A-Z][\w.'-]*):\s+(.*)$", body)
    if speaker:
        claim = f"{speaker.group(1)} {speaker.group(2).strip()}"
    else:
        claim = body
    return claim, line.strip()


def _with_date(claim: str, evidence: str, context: str) -> tuple[str, str]:
    if not context or context.casefold() in claim.casefold():
        return claim, evidence
    return f"{context}: {claim}", f"{context} {evidence}"


def extract_fact_rows(source: str) -> list[dict]:
    """Fact rows the note itself supports. Markup and non-facts are left out."""
    consumed: set[int] = set()
    rows = [row for row in _code_and_table_rows(source, consumed) if not rejection_reasons(row["claim"])]
    context = _heading_context(source)
    lines = _line_spans(source)
    for index, (_start, _end, line) in enumerate(lines):
        if index in consumed:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("```"):
            continue
        bullet = _bullet(line)
        if bullet:
            claim, evidence = bullet
            claim, support = _with_date(claim, evidence, context)
            if evidence in source and not rejection_reasons(claim):
                rows.append(_row(claim, evidence, support, "Extracted from a list item."))
            consumed.add(index)
    consumed_spans = []
    for index in consumed:
        start, end, _line = lines[index]
        consumed_spans.append((start, end))
    for start, end in sentence_spans(source):
        if any(not (end <= span_start or start >= span_end) for span_start, span_end in consumed_spans):
            continue
        evidence = source[start:end].strip()
        if not evidence or evidence.startswith("#"):
            continue
        claim, support = _with_date(evidence, evidence, context)
        if rejection_reasons(claim) or rejection_reasons(evidence):
            continue
        if evidence not in source:
            continue
        rows.append(_row(claim, evidence, support, "Extracted from the source sentence."))
    return rows
