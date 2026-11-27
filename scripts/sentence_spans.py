"""Sentence boundaries and fact rows for messy notes.

Abbreviations, decimals, versions, initials, IPs, and paths do not split a sentence.
The capture policy supplies the abbreviation list and the rejection rules.
"""

from __future__ import annotations

import re

from scripts.capture_policy import load_policy, rejection_reasons

_DOT = "\uE000"


_ABBREV_STOP = {
    "the", "and", "but", "for", "nor", "yet", "so", "he", "she", "it", "we", "they",
    "this", "that", "then", "yes", "end", "see", "now", "all", "one", "two", "his",
    "her", "our", "was", "are", "not", "can", "may", "did", "had", "has", "who",
    "how", "why", "let", "put", "got", "its", "too", "a", "an",
}


def _protect(text: str) -> str:
    policy = load_policy()
    names = "|".join(re.escape(item) for item in policy["abbreviations"])

    def dots(match: re.Match[str]) -> str:
        return match.group(0).replace(".", _DOT)

    def general_abbrev(match: re.Match[str]) -> str:
        word = match.group(1)
        nxt = match.group(2)
        title_case = word[0].isupper() and word[1:].islower()
        proper = len(nxt) >= 2 and nxt[0].isupper() and any(char.islower() for char in nxt[1:])
        if not title_case or not proper or word.casefold() in _ABBREV_STOP or nxt.casefold() in _ABBREV_STOP:
            return match.group(0)
        return word + _DOT

    protected = text
    protected = re.sub(r"\b(?:e\.g|i\.e)\.", dots, protected, flags=re.IGNORECASE)
    protected = re.sub(rf"\b(?:{names})\.", dots, protected, flags=re.IGNORECASE)
    protected = re.sub(r"(?<![A-Za-z])([A-Z][A-Za-z]{0,3})\.(?=\s+([A-Z][A-Za-z'-]*))", general_abbrev, protected)
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
            opener = ""
            for _body_index, body_line in body:
                stripped = body_line.strip()
                if re.match(r"^(?:.*\s)?[A-Za-z0-9_.:-]+\s*\{\s*$", stripped):
                    opener = stripped
                    continue
                if not stripped or stripped in {"{", "}", "};"} or stripped.endswith("{"):
                    continue
                evidence = stripped
                if evidence not in source:
                    continue
                claim = evidence
                row = _row(claim, evidence, evidence, "Extracted from a config block.")
                rows.append(_remember_context(row, opener))
            index += 1
            continue
        index += 1

    index = 0
    while index < len(lines):
        if index in consumed or not _is_table_line(lines[index][2]):
            index += 1
            continue
        header_line = lines[index][2].strip()
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
            if evidence in source and claim and "|" not in claim:
                row = _row(claim, evidence, evidence, "Extracted from a table row.")
                if header_line:
                    row["context"] = header_line
                rows.append(row)
            index += 1
    return rows


def _brace_blocks(source: str, consumed: set[int]) -> list[dict]:
    rows: list[dict] = []
    lines = _line_spans(source)
    index = 0
    while index < len(lines):
        if index in consumed:
            index += 1
            continue
        stripped = lines[index][2].strip()
        if not re.match(r"^(?:.*\s)?[A-Za-z0-9_.:-]+\s*\{\s*$", stripped):
            index += 1
            continue
        opener = stripped
        consumed.add(index)
        index += 1
        while index < len(lines):
            body = lines[index][2].strip()
            consumed.add(index)
            index += 1
            if not body:
                continue
            if body.startswith("}"):
                break
            if body.endswith("{") or body in {"{", "}", "};"}:
                continue
            if body not in source:
                continue
            row = _row(body, body, body, "Extracted from a config block.")
            rows.append(_remember_context(row, opener))
    return rows


def bind_table_row(source: str, claim: str, evidence: str) -> tuple[str, str] | None:
    """Pair a table claim with the raw data line and the raw header line."""
    lines = source.splitlines()
    header = ""
    best: tuple[int, str, str] | None = None
    blob = f"{claim}\n{evidence}".casefold()
    for index, line in enumerate(lines):
        if _is_table_line(line) and index + 1 < len(lines) and _is_separator(lines[index + 1]):
            header = line.strip()
            continue
        if not header or not _is_table_line(line) or _is_separator(line):
            continue
        cells = [cell for cell in _cells(line) if cell]
        if len(cells) < 2:
            continue
        if evidence.strip() == line.strip():
            return line.strip(), header
        hits = sum(1 for cell in cells if cell.casefold() in blob)
        if hits == len(cells) and (best is None or hits * 100 + sum(len(cell) for cell in cells) > best[0]):
            best = (hits * 100 + sum(len(cell) for cell in cells), line.strip(), header)
    if best and ("|" in evidence or evidence.strip() not in source):
        return best[1], best[2]
    return None


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _colon_blocks(source: str, consumed: set[int]) -> list[dict]:
    """Indented lines under a `name:` label, such as an Unbound `server:` block."""
    rows: list[dict] = []
    lines = _line_spans(source)
    section = ""
    index = 0
    while index < len(lines):
        raw = lines[index][2]
        stripped = raw.strip()
        if stripped.startswith("#"):
            section = stripped
            index += 1
            continue
        if index in consumed or not re.fullmatch(r"[A-Za-z0-9_.:-]+\s*:", stripped):
            index += 1
            continue
        opener_index = index
        opener = stripped
        label_indent = _line_indent(raw)
        index += 1
        body_rows: list[dict] = []
        while index < len(lines):
            body_raw = lines[index][2]
            body = body_raw.strip()
            if not body:
                index += 1
                continue
            if _line_indent(body_raw) <= label_indent:
                break
            consumed.add(index)
            index += 1
            if body.endswith("{") or body in {"{", "}", "};"} or body.startswith("}"):
                continue
            if body not in source:
                continue
            row = _row(body, body, body, "Extracted from a config block.")
            body_rows.append(_remember_context(row, opener, section))
        if body_rows:
            consumed.add(opener_index)
            rows.extend(body_rows)
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
    lines = [line.strip() for line in source.splitlines() if line.strip()]
    for line in lines[:3]:
        title = line.lstrip("#").strip()
        short_title = line.startswith("#") or len(title.split()) <= 8
        if short_title and re.search(r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b", title):
            return title
    return ""


def _bullet(line: str) -> tuple[str, str, str] | None:
    match = re.match(r"^\s*[-*]\s+(?:\[(?:[ xX])\]\s+)?(.*)$", line.strip())
    if not match:
        return None
    body = match.group(1).strip()
    speaker = re.match(r"^([A-Z][\w.'-]*):\s+(.*)$", body)
    if speaker and speaker.group(1) in line:
        return f"{speaker.group(1)} {speaker.group(2).strip()}", line.strip(), speaker.group(1)
    return body, line.strip(), ""


def _trim_intention(text: str) -> str:
    trimmed = re.sub(
        r"(?i)(?:\s*[,;]\s*|\s+)(?:should|will|might|gonna)\s+\w+\b\s*$",
        "",
        str(text or ""),
    ).strip()
    if len(trimmed) >= 8 and trimmed != str(text or "").strip() and trimmed in str(text or ""):
        return trimmed
    return str(text or "").strip()


def _keep_joined_followup(sentence: str) -> bool:
    """A subject-less tail such as 'Too slow, ~2 tok/s' stays with the sentence before it."""
    starters = {item.casefold() for item in load_policy()["subjectless_starts"]}
    match = re.search(r"\.\s+([A-Za-z]+)\b", sentence)
    return bool(match and match.group(1).casefold() in starters)


def _fact_clauses(text: str) -> list[str]:
    clauses: list[str] = []
    for sentence in split_sentences(text) or [text.strip()]:
        sentence = sentence.strip()
        if not sentence:
            continue
        if _keep_joined_followup(sentence):
            clauses.append(sentence)
            continue
        bits = [bit.strip(" ,") for bit in re.split(r";\s+|\s+\band\b\s+|,\s+", sentence) if bit.strip(" ,")]
        rich = [bit for bit in bits if re.search(r"\d|\$", bit) and len(bit.split()) >= 2]
        if len(rich) >= 2 and (len(rich) == len(bits) or all(len(bit.split()) >= 3 or re.search(r"\d|\$", bit) for bit in bits)):
            clauses.extend(rich)
        else:
            clauses.append(sentence)
    return [clause for clause in clauses if clause]


def _stored_spans(text: str) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    if "¦" in raw:
        return [part.strip() for part in raw.split("¦") if part.strip()]
    if raw.startswith("|") or raw.count("|") >= 2:
        return [raw]
    return [part.strip() for part in re.split(r"\s+\|\s+", raw) if part.strip()]


def _label_for_claim(part: str) -> str:
    from scripts.evidence import _context_label

    return _context_label(part)


def _remember_context(row: dict, *spans: str) -> dict:
    parts = _stored_spans(str(row.get("context") or ""))
    for part in spans:
        cleaned = str(part or "").strip()
        if cleaned and cleaned not in parts:
            parts.append(cleaned)
    if not parts:
        return row
    claim = row["claim"]
    for part in parts:
        label = _label_for_claim(part)
        if not label or label.casefold() in claim.casefold():
            continue
        claim = f"{label}: {claim}"
    row["claim"] = claim
    row["context"] = " ¦ ".join(parts)
    row["support"] = " ".join(parts + [row.get("evidence") or ""])
    return row


def extract_fact_rows(source: str) -> list[dict]:
    """Fact rows the note itself supports. Markup and non-facts are left out."""
    consumed: set[int] = set()
    rows = [row for row in _code_and_table_rows(source, consumed) if not rejection_reasons(row["claim"], config_line=True)]
    rows.extend(row for row in _brace_blocks(source, consumed) if not rejection_reasons(row["claim"], config_line=True))
    rows.extend(row for row in _colon_blocks(source, consumed) if not rejection_reasons(row["claim"], config_line=True))
    context = _heading_context(source)
    lines = _line_spans(source)
    for index, (_start, _end, line) in enumerate(lines):
        if index in consumed:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("```"):
            continue
        if re.match(r"(?i)^(?:attendees|present|participants|next meeting|rate limit)\b", stripped):
            if stripped in source and not rejection_reasons(stripped):
                rows.append(_remember_context(_row(stripped, stripped, stripped, "Extracted from a meeting line."), context))
            consumed.add(index)
            continue
        bullet = _bullet(line)
        if bullet:
            claim, evidence, speaker = bullet
            trimmed = _trim_intention(claim)
            evidence_span = trimmed if trimmed in source else evidence
            if evidence_span in source and not rejection_reasons(trimmed):
                rows.append(_remember_context(_row(trimmed, evidence_span, evidence_span, "Extracted from a list item."), context, speaker))
            consumed.add(index)
    consumed_spans = []
    for index in consumed:
        start, end, _line = lines[index]
        consumed_spans.append((start, end))
    for start, end in sentence_spans(source):
        if any(not (end <= span_start or start >= span_end) for span_start, span_end in consumed_spans):
            continue
        sentence = source[start:end].strip()
        if not sentence or sentence.startswith("#"):
            continue
        for clause in _fact_clauses(sentence):
            clause = _trim_intention(clause)
            if context and clause.casefold().strip(" .") == context.casefold().strip(" ."):
                continue
            if clause not in source or rejection_reasons(clause):
                continue
            rows.append(_remember_context(_row(clause, clause, clause, "Extracted from the source sentence."), context))
    if context:
        rows = [_remember_context(row, context) for row in rows]
    return rows
