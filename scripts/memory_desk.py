# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Capture receipts and trust-lane memory search.

Daily capture writes a working note or a quarantined inbox file. It does not
approve anything. Search then looks across those lanes without pretending they
share a trust level:

* ``working_note`` — searchable immediately, never a verified fact
* ``verified_atom`` — current owner-approved atoms only
* ``quarantine`` — owner-only; hidden from default search

Indexes stay disposable. Quarantine hits are read from inbox Markdown, and
secret-shaped text is redacted in previews.
"""

from __future__ import annotations

import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import content_sha256  # noqa: E402
from config import ATOMS_DIR, LEXICAL_INDEX_PATH, _is_reparse_or_link  # noqa: E402
from inbox_workflow import redact_detected_secrets, scan_inbox_payload_risks  # noqa: E402
from lexical_store import (  # noqa: E402
    FTS_STOP_WORDS,
    MAX_QUERY_TERMS,
    TOKEN_PATTERN,
    LexicalIndexUnavailable,
)
from memory_format import split_frontmatter  # noqa: E402
from memory_review import parse_frontmatter_values, section_text  # noqa: E402
from notes_store import (  # noqa: E402
    AGENT_ORIGINS,
    MAX_AGENT_CAPTURE_CHARS,
    MAX_NOTE_BYTES,
    NoteError,
    NotePaths,
    _create_exclusive,
    create_note,
    search_notes,
    slugify,
)
from recall import (  # noqa: E402
    MemoryRecallUnavailable,
    RecallOptions,
    normalize_project_tag,
    project_tags_match,
    search_memory,
)

WORKING_NOTE = "working_note"
VERIFIED_ATOM = "verified_atom"
QUARANTINE = "quarantine"
DEFAULT_LANES = (WORKING_NOTE, VERIFIED_ATOM)
ALL_LANES = (WORKING_NOTE, VERIFIED_ATOM, QUARANTINE)
_LANE_ORDER = {VERIFIED_ATOM: 0, WORKING_NOTE: 1, QUARANTINE: 2}
_MAX_QUERY_CHARS = 500
_MAX_HITS = 25
_SNIPPET_CHARS = 280
_INBOX_PREVIEW_BYTES = 256 * 1024
_PROJECT_TAG_RE = re.compile(r"project:[A-Za-z0-9_:/.-]+", re.IGNORECASE)

_TRUST_LABELS = {
    WORKING_NOTE: "Working note — searchable now, not a verified fact",
    VERIFIED_ATOM: "Verified atom — owner-approved memory",
    QUARANTINE: "Quarantine — hidden from default search until you review it",
}
_PREFERENCE_LABEL = "Verified preference — owner preference, not a factual claim"
_SECRET_PREVIEW = "Quarantined capture withheld from preview because it matched a secret pattern."
_SAVED_RISK_WARNING = (
    "This capture matches a secret or injection pattern. It remains a working note on this machine "
    "and was not turned into a verified fact."
)


class MemoryDeskError(ValueError):
    """Raised when a capture or search request violates the memory-desk contract."""


def _query_terms(query: str, *, limit: int = MAX_QUERY_TERMS) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    fallback: list[str] = []
    for token in TOKEN_PATTERN.findall(str(query or "")):
        normalized = token.casefold()
        if not normalized or normalized in seen or normalized == "redacted":
            continue
        seen.add(normalized)
        if normalized in FTS_STOP_WORDS:
            if len(fallback) < limit:
                fallback.append(normalized)
            continue
        terms.append(normalized)
        if len(terms) >= limit:
            break
    return terms or fallback


def _window_snippet(text: str, query: str, *, width: int = _SNIPPET_CHARS) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    if not flat:
        return ""
    haystack = flat.casefold()
    for term in _query_terms(query, limit=8):
        pos = haystack.find(term)
        if pos >= 0:
            start = max(0, pos - width // 3)
            prefix = "…" if start else ""
            chunk = flat[start : start + width]
            suffix = "…" if start + width < len(flat) else ""
            return redact_detected_secrets(prefix + chunk + suffix)
    chunk = flat[:width]
    suffix = "…" if len(flat) > width else ""
    return redact_detected_secrets(chunk + suffix)


def _clean_title(title: str, body: str) -> str:
    raw = str(title or "").strip()
    if not raw and str(body or "").strip():
        raw = str(body).strip().splitlines()[0]
    cleaned = "".join(ch for ch in raw if ord(ch) >= 32 or ch == "\t")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()[:200]
    return cleaned or "Untitled"


def _safe_source(source: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._:-]", "", str(source or ""))
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")[:64]
    return cleaned or "owner"


def _find_again_query(title: str, body: str) -> str:
    redacted = redact_detected_secrets(f"{title}\n{body}")
    terms = _query_terms(redacted, limit=8)
    return " ".join(terms)


def _is_preference(content: str) -> bool:
    parsed = split_frontmatter(content)
    if not parsed:
        return False
    values = parse_frontmatter_values(parsed[0])
    label = str(values.get("epistemic_label") or "").strip().lower()
    contract = str(values.get("epistemic_contract") or "").strip()
    return label == "preference" or bool(contract)


def _atom_title(content: str, filename: str) -> str:
    parsed = split_frontmatter(content)
    if parsed:
        values = parse_frontmatter_values(parsed[0])
        title = str(values.get("title") or values.get("id") or "").strip()
        if title:
            return title[:200]
    return os.path.splitext(filename)[0]


def _atom_snippet(content: str, query: str) -> str:
    parsed = split_frontmatter(content)
    body = "\n".join(parsed[1]) if parsed else content
    claim = section_text(body, "Claim")
    return _window_snippet(claim or body, query)


def _project_tags(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(match.group(0) for match in _PROJECT_TAG_RE.finditer(text or "")))


def _matches_project(tags: tuple[str, ...] | list[str], project: str) -> bool:
    if not project:
        return True
    project_tags = [tag for tag in tags if str(tag).lower().startswith("project:")]
    if not project_tags:
        return False
    return all(bool(project_tags_match((tag,), (project,))) for tag in project_tags)


def _sha_file(directory: str, filename: str) -> str:
    path = os.path.join(directory, filename)
    if not os.path.isfile(path) or _is_reparse_or_link(path):
        return ""
    with open(path, "rb") as handle:
        return content_sha256(handle.read())


def _quarantine_capture(paths: NotePaths, title: str, body: str, risks: tuple[str, ...], source: str) -> dict[str, object]:
    os.makedirs(paths.inbox_dir, exist_ok=True)
    if _is_reparse_or_link(paths.inbox_dir):
        raise PermissionError(f"Refusing to quarantine capture into a symlinked inbox: {paths.inbox_dir}")
    payload = (
        "<!-- second_brain_origin: api_local -->\n"
        f"<!-- capture_source: {_safe_source(source)} -->\n\n"
        f"# {title}\n\n{body}\n"
    ).encode("utf-8")
    if len(payload) > MAX_NOTE_BYTES:
        raise NoteError("Capture exceeds the maximum supported size (2 MB).")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = _create_exclusive(paths.inbox_dir, f"{stamp}-{slugify(title)}-capture", payload)
    return {
        "status": "quarantined",
        "filename": name,
        "sha256": content_sha256(payload),
        "location": "0-raw/inbox/",
        "origin": "quarantine",
        "risk_flags": list(risks),
        "reason": "Capture was sent to the quarantined inbox and is excluded from default search.",
    }


def _normalize_lanes(lanes: tuple[str, ...] | list[str] | None, *, include_quarantine: bool) -> tuple[str, ...]:
    selected = list(DEFAULT_LANES if lanes is None else lanes)
    if any(not isinstance(lane, str) for lane in selected):
        raise MemoryDeskError("Search lanes must be strings.")
    unknown = [lane for lane in selected if lane not in ALL_LANES]
    if unknown:
        raise MemoryDeskError(f"Unknown search lane: {unknown[0]}.")
    if include_quarantine and QUARANTINE not in selected:
        selected.append(QUARANTINE)
    if QUARANTINE in selected and not include_quarantine:
        raise MemoryDeskError("Quarantine search is owner-only and must be requested explicitly.")
    ordered = tuple(dict.fromkeys(selected))
    if not ordered:
        raise MemoryDeskError("Select at least one search lane.")
    return ordered


def _hit(
    *,
    lane: str,
    filename: str,
    title: str,
    snippet: str,
    score: float,
    why: list[str],
    verified_fact: bool,
    review_status: str,
    tags: list[str],
    sha256: str,
    trust_label: str | None = None,
) -> dict[str, object]:
    return {
        "lane": lane,
        "filename": filename,
        "title": title[:200],
        "snippet": snippet[: _SNIPPET_CHARS + 2],
        "score": round(float(score), 6),
        "why": why[:6],
        "trust_label": trust_label or _TRUST_LABELS[lane],
        "verified_fact": bool(verified_fact),
        "review_status": review_status,
        "tags": tags[:32],
        "sha256": sha256,
    }


def search_memory_desk(
    query: str,
    *,
    lanes: tuple[str, ...] | list[str] | None = None,
    limit: int = 8,
    project: str | None = None,
    include_quarantine: bool = False,
    hide_preference: bool = False,
    exclude_filenames: tuple[str, ...] | set[str] = (),
    paths: NotePaths | None = None,
    atoms_dir: str | None = None,
    lexical_index_path: str | None = None,
) -> dict[str, object]:
    """Search working notes and verified atoms, optionally including owner quarantine."""
    text = str(query or "").strip()
    if not text:
        raise MemoryDeskError("Search query is required.")
    if len(text) > _MAX_QUERY_CHARS:
        raise MemoryDeskError(f"Search query exceeds {_MAX_QUERY_CHARS} characters.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_HITS:
        raise MemoryDeskError(f"Search limit must be an integer from 1 to {_MAX_HITS}.")
    scope = str(project or "").strip()
    if len(scope) > 120:
        raise MemoryDeskError("Project filter exceeds 120 characters.")
    selected = _normalize_lanes(lanes, include_quarantine=bool(include_quarantine))
    note_paths = paths or NotePaths()
    atoms = atoms_dir or ATOMS_DIR
    atom_index = lexical_index_path or LEXICAL_INDEX_PATH
    excluded = {str(name) for name in exclude_filenames if str(name).strip()}
    warnings: list[str] = []
    grouped: dict[str, list[dict[str, object]]] = {lane: [] for lane in selected}

    if WORKING_NOTE in selected:
        try:
            notes = search_notes(text, limit=limit + len(excluded), project=scope or None, paths=note_paths)
        except (LexicalIndexUnavailable, NoteError, OSError) as exc:
            notes = []
            warnings.append(f"working_note lane unavailable: {exc}")
        kept = [note for note in notes if note.filename not in excluded]
        for index, note in enumerate(kept[:limit]):
            grouped[WORKING_NOTE].append(
                _hit(
                    lane=WORKING_NOTE,
                    filename=note.filename,
                    title=note.title,
                    snippet=_window_snippet(note.snippet, text),
                    score=1.0 / (1 + index),
                    why=["working-note lexical match", "not a verified fact"],
                    verified_fact=False,
                    review_status="unreviewed_note",
                    tags=list(note.tags),
                    sha256=note.sha256,
                )
            )

    if VERIFIED_ATOM in selected:
        try:
            direct, _neighbors = search_memory(
                text,
                RecallOptions(
                    n_results=limit + len(excluded),
                    follow_links=False,
                    include_statuses={"accepted"},
                    projects=(scope,) if scope else (),
                    retrieval_mode="lexical",
                    enable_reranker=False,
                ),
                atoms_dir=atoms,
                index_path=atom_index,
            )
        except (MemoryRecallUnavailable, LexicalIndexUnavailable, OSError) as exc:
            direct = []
            warnings.append(f"verified_atom lane unavailable: {exc}")
        kept_atoms = []
        for item in direct:
            if item.filename in excluded or item.review_status != "accepted":
                continue
            if hide_preference and _is_preference(item.content):
                continue
            if scope and not _matches_project(item.contexts, scope):
                continue
            kept_atoms.append(item)
        for index, item in enumerate(kept_atoms[:limit]):
            preference = _is_preference(item.content)
            why = list(item.match_reasons[:4]) or ["lexical match on an owner-approved atom"]
            why.append("owner preference contract" if preference else "owner-approved factual claim")
            grouped[VERIFIED_ATOM].append(
                _hit(
                    lane=VERIFIED_ATOM,
                    filename=item.filename,
                    title=_atom_title(item.content, item.filename),
                    snippet=_atom_snippet(item.content, text),
                    score=1.0 / (1 + index),
                    why=why,
                    verified_fact=not preference,
                    review_status=item.review_status,
                    tags=list(item.contexts),
                    sha256=content_sha256(item.content.encode("utf-8")),
                    trust_label=_PREFERENCE_LABEL if preference else None,
                )
            )

    if QUARANTINE in selected:
        grouped[QUARANTINE].extend(
            _search_quarantine(text, note_paths.inbox_dir, limit=limit, project=scope, exclude=excluded)
        )

    hits: list[dict[str, object]] = []
    for lane in selected:
        hits.extend(grouped[lane])
    hits.sort(key=lambda item: (-float(item["score"]), _LANE_ORDER.get(str(item["lane"]), 9), str(item["filename"])))
    hits = hits[:limit]
    return {
        "query": text,
        "lanes": list(selected),
        "project": normalize_project_tag(scope) if scope else "",
        "include_quarantine": QUARANTINE in selected,
        "count": len(hits),
        "hits": hits,
        "lane_warnings": warnings,
    }


def _search_quarantine(
    query: str,
    inbox_dir: str,
    *,
    limit: int,
    project: str,
    exclude: set[str],
) -> list[dict[str, object]]:
    if not inbox_dir or not os.path.isdir(inbox_dir) or _is_reparse_or_link(inbox_dir):
        return []
    terms = _query_terms(query)
    if not terms:
        return []
    found: list[tuple[float, str, dict[str, object]]] = []
    try:
        iterator = os.scandir(inbox_dir)
    except OSError:
        return []
    with iterator:
        for entry in iterator:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            name = entry.name
            if name.startswith(".") or not name.lower().endswith(".md") or name in exclude:
                continue
            if _is_reparse_or_link(entry.path):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if info.st_size > _INBOX_PREVIEW_BYTES:
                continue
            try:
                with open(entry.path, "rb") as handle:
                    raw = handle.read()
            except OSError:
                continue
            text = raw.decode("utf-8", errors="replace")
            risks = scan_inbox_payload_risks(text)
            tags = _project_tags(text)
            if project and not _matches_project(tags, project):
                continue
            haystack = redact_detected_secrets(text).casefold()
            matched = [term for term in terms if term in haystack]
            if not matched:
                continue
            secret = any(str(flag).startswith("secret_credential:") for flag in risks)
            title = name
            for line in text.splitlines():
                if line.startswith("# "):
                    title = redact_detected_secrets(line[2:].strip())[:200] or name
                    break
            snippet = _SECRET_PREVIEW if secret else _window_snippet(text, query)
            score = len(matched) / len(terms)
            found.append(
                (
                    score,
                    name,
                    _hit(
                        lane=QUARANTINE,
                        filename=name,
                        title=title,
                        snippet=snippet,
                        score=score,
                        why=[f"matched {len(matched)} of {len(terms)} query terms in quarantined inbox"],
                        verified_fact=False,
                        review_status="quarantined",
                        tags=list(tags),
                        sha256=content_sha256(raw),
                    ),
                )
            )
    found.sort(key=lambda item: (-item[0], item[1]))
    ranked: list[dict[str, object]] = []
    for index, (_score, _name, hit) in enumerate(found[:limit]):
        hit["score"] = round(1.0 / (1 + index), 6)
        ranked.append(hit)
    return ranked


def _receipt_from_created(
    created: dict[str, object],
    *,
    title: str,
    body: str,
    paths: NotePaths,
    atoms_dir: str,
    lexical_index_path: str,
    hide_preference: bool,
    similar_limit: int,
    risks: tuple[str, ...],
) -> dict[str, object]:
    status = str(created.get("status") or "")
    quarantined = status == "quarantined"
    filename = str(created.get("filename") or "")
    directory = paths.inbox_dir if quarantined else paths.notes_dir
    sha = str(created.get("sha256") or "") or _sha_file(directory, filename)
    risk_flags = [str(flag) for flag in (created.get("risk_flags") or risks)]
    find_query = _find_again_query(title, body)
    similar: list[dict[str, object]] = []
    if find_query:
        similar_result = search_memory_desk(
            find_query,
            limit=max(1, min(int(similar_limit), 5)),
            include_quarantine=False,
            hide_preference=hide_preference,
            exclude_filenames=(filename,),
            paths=paths,
            atoms_dir=atoms_dir,
            lexical_index_path=lexical_index_path,
        )
        similar = list(similar_result["hits"])
    lane = QUARANTINE if quarantined else WORKING_NOTE
    receipt: dict[str, object] = {
        "kind": "memory_receipt",
        "status": status,
        "filename": filename,
        "sha256": sha,
        "origin": str(created.get("origin") or ("quarantine" if quarantined else "owner_note")),
        "location": str(created.get("location") or ("0-raw/inbox/" if quarantined else "notes/")),
        "trust_lane": lane,
        "trust_label": _TRUST_LABELS[lane],
        "searchable_by_default": not quarantined and status == "saved",
        "verified_fact": False,
        "risk_flags": risk_flags,
        "find_again_query": find_query,
        "similar": similar,
    }
    if created.get("reason"):
        receipt["reason"] = str(created["reason"])
    if not quarantined and risk_flags:
        receipt["secret_warning"] = _SAVED_RISK_WARNING
    return receipt


def capture_memory(
    title: str,
    body: str,
    *,
    origin: str = "owner_note",
    tags=(),
    capture_source: str = "owner",
    inbox: bool = False,
    hide_preference: bool = False,
    similar_limit: int = 3,
    paths: NotePaths | None = None,
    atoms_dir: str | None = None,
    lexical_index_path: str | None = None,
) -> dict[str, object]:
    """Save a capture and return a receipt. Never approves or promotes memory."""
    note_paths = paths or NotePaths()
    atoms = atoms_dir or ATOMS_DIR
    atom_index = lexical_index_path or LEXICAL_INDEX_PATH
    text_body = str(body or "")
    clean_title = _clean_title(title, text_body)
    if not str(title or "").strip() and not text_body.strip():
        raise MemoryDeskError("Capture text is required.")
    if origin in AGENT_ORIGINS and len(text_body) > MAX_AGENT_CAPTURE_CHARS:
        raise NoteError(f"Agent capture exceeds {MAX_AGENT_CAPTURE_CHARS} characters.")
    risks = scan_inbox_payload_risks(f"{clean_title}\n{text_body}")
    if inbox:
        created = _quarantine_capture(note_paths, clean_title, text_body, risks, capture_source)
    else:
        created = create_note(
            clean_title,
            text_body,
            origin=origin,
            tags=tags,
            capture_source=capture_source,
            paths=note_paths,
        )
    return _receipt_from_created(
        created,
        title=clean_title,
        body=text_body,
        paths=note_paths,
        atoms_dir=atoms,
        lexical_index_path=atom_index,
        hide_preference=hide_preference,
        similar_limit=similar_limit,
        risks=risks,
    )


def attach_receipt(
    created: dict[str, object],
    *,
    title: str,
    body: str,
    paths: NotePaths,
    atoms_dir: str,
    lexical_index_path: str,
    hide_preference: bool = False,
) -> dict[str, object]:
    """Add a memory receipt to an existing capture response without changing its status."""
    risks = scan_inbox_payload_risks(f"{title}\n{body}")
    receipt = _receipt_from_created(
        created,
        title=title or _clean_title("", body),
        body=body,
        paths=paths,
        atoms_dir=atoms_dir,
        lexical_index_path=lexical_index_path,
        hide_preference=hide_preference,
        similar_limit=3,
        risks=risks,
    )
    merged = dict(created)
    merged["receipt"] = receipt
    return merged
