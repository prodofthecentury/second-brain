# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""
List reviewable memory atoms.

This is the first lightweight review queue surface. It returns structured
records for reuse by CLI, dashboard, or later MCP tools.
"""



from __future__ import annotations

if __name__ == "__main__" and not __package__:
    import runpy as _sb_runpy
    import sys as _sb_sys
    from pathlib import Path as _sb_Path

    _sb_repo = _sb_Path(__file__).resolve().parents[1]
    _sb_here = _sb_Path(__file__).resolve().parent
    if _sb_Path(_sb_sys.path[0]).resolve() == _sb_here:
        _sb_sys.path[0] = str(_sb_repo)
    elif str(_sb_repo) not in _sb_sys.path:
        _sb_sys.path.insert(0, str(_sb_repo))
    raise SystemExit(_sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__"))


import argparse
import functools
import json
import os
import re
import sys
from pathlib import Path

from scripts.config import (
    ATOMS_DIR,
    is_content_markdown,
    resolve_module_vault_path,
    snapshot_module_defaults,
)
from scripts.memory_format import split_frontmatter

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())


DEFAULT_STATUS = "pending"
REVIEW_STATUSES = {"pending", "accepted", "needs_edit", "rejected", "merged"}


def _strip_yaml_comment(raw: str) -> str:
    """Strip a YAML `# ...` comment only when `#` is outside quotes and preceded by whitespace (or at start)."""
    if "#" not in raw:
        return raw
    in_single = False
    in_double = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if in_single:
            if ch == "'" and i + 1 < n and raw[i + 1] == "'":
                i += 2
                continue
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_double = False
        else:
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch == "#" and (i == 0 or raw[i - 1] in (" ", "\t", "\r", "\n")):
                return raw[:i]
        i += 1
    return raw


def _unescape_double_quoted(inner: str) -> str:
    out: list[str] = []
    i = 0
    n = len(inner)
    escapes = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "0": "\0",
    }
    while i < n:
        ch = inner[i]
        if ch == "\\" and i + 1 < n:
            nxt = inner[i + 1]
            if nxt in escapes:
                out.append(escapes[nxt])
                i += 2
                continue
            if nxt == "u" and i + 5 < n:
                hex_part = inner[i + 2 : i + 6]
                if all(c in "0123456789abcdefABCDEF" for c in hex_part):
                    out.append(chr(int(hex_part, 16)))
                    i += 6
                    continue
            if nxt == "U" and i + 9 < n:
                hex_part = inner[i + 2 : i + 10]
                if all(c in "0123456789abcdefABCDEF" for c in hex_part):
                    out.append(chr(int(hex_part, 16)))
                    i += 10
                    continue
            out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_yaml_scalar(raw: str) -> str:
    """Parse a single YAML scalar (double-quoted, single-quoted with '' escape, or bare with # comments)."""
    s = (raw or "").strip()
    if not s:
        return ""
    if s.startswith('"'):
        i = 1
        n = len(s)
        while i < n:
            if s[i] == "\\" and i + 1 < n:
                i += 2
                continue
            if s[i] == '"':
                rest = _strip_yaml_comment(s[i + 1 :]).strip()
                if not rest:
                    return _unescape_double_quoted(s[1:i])
                break
            i += 1
    elif s.startswith("'"):
        i = 1
        n = len(s)
        buf: list[str] = []
        while i < n:
            if s[i] == "'" and i + 1 < n and s[i + 1] == "'":
                buf.append("'")
                i += 2
                continue
            if s[i] == "'":
                rest = _strip_yaml_comment(s[i + 1 :]).strip()
                if not rest:
                    return "".join(buf)
                break
            buf.append(s[i])
            i += 1
    return _strip_yaml_comment(s).strip()


def _flow_bracket_delta(line: str) -> int:
    """Return net `[` minus `]` count outside quotes and YAML comments."""
    cleaned = _strip_yaml_comment(line)
    in_single = False
    in_double = False
    delta = 0
    i = 0
    n = len(cleaned)
    while i < n:
        ch = cleaned[i]
        if in_single:
            if ch == "'" and i + 1 < n and cleaned[i + 1] == "'":
                i += 2
                continue
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_double = False
        else:
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch == "[":
                delta += 1
            elif ch == "]":
                delta -= 1
        i += 1
    return delta


def parse_scalar_list(value: str) -> list[str]:
    raw = (value or "").strip()
    if not raw:
        return []
    # Strip line-level YAML comments outside quotes while preserving multiline flow items.
    cleaned_lines = [_strip_yaml_comment(line) for line in raw.splitlines()]
    cleaned = "\n".join(cleaned_lines).strip()
    if not cleaned or cleaned == "[]":
        return []
    if cleaned.startswith("[") and cleaned.endswith("]"):
        inner = cleaned[1:-1].strip()
        if not inner:
            return []
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list) and all(
                x is None or isinstance(x, (str, int, float, bool)) for x in parsed
            ):
                out_json: list[str] = []
                for x in parsed:
                    if x is None:
                        continue
                    s_val = "true" if x is True else "false" if x is False else str(x).strip()
                    if s_val:
                        out_json.append(s_val)
                return out_json
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        items: list[str] = []
        buf: list[str] = []
        quote_kind: str | None = None
        in_single = False
        in_double = False
        depth = 0
        i = 0
        n = len(inner)

        def _flush_token() -> None:
            nonlocal quote_kind
            token_raw = "".join(buf).strip()
            buf.clear()
            q = quote_kind
            quote_kind = None
            if not token_raw and q is None:
                return
            val = _parse_yaml_scalar(token_raw)
            if val:
                items.append(val)

        while i < n:
            ch = inner[i]
            if in_single:
                buf.append(ch)
                if ch == "'" and i + 1 < n and inner[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                if ch == "'":
                    in_single = False
                i += 1
                continue
            if in_double:
                buf.append(ch)
                if ch == "\\" and i + 1 < n:
                    buf.append(inner[i + 1])
                    i += 2
                    continue
                if ch == '"':
                    in_double = False
                i += 1
                continue

            if ch == "#" and (i == 0 or inner[i - 1] in (" ", "\t", "\r", "\n", ",")):
                while i < n and inner[i] != "\n":
                    i += 1
                continue
            if ch == "'":
                if not "".join(buf).strip():
                    quote_kind = "'"
                in_single = True
                buf.append(ch)
            elif ch == '"':
                if not "".join(buf).strip():
                    quote_kind = '"'
                in_double = True
                buf.append(ch)
            elif ch in ("[", "{"):
                depth += 1
                buf.append(ch)
            elif ch in ("]", "}"):
                depth = max(0, depth - 1)
                buf.append(ch)
            elif ch == "," and depth == 0:
                _flush_token()
            else:
                buf.append(ch)
            i += 1

        _flush_token()
        return items

    single = _parse_yaml_scalar(cleaned)
    return [single] if single else []


def _fold_block_scalar(block_lines: list[str], style: str, chomp: str) -> str:
    non_empty_indents = [len(line) - len(line.lstrip(" ")) for line in block_lines if line.strip()]
    min_indent = min(non_empty_indents) if non_empty_indents else 0
    dedented = [line[min_indent:] if len(line) >= min_indent else "" for line in block_lines]
    if style == "|":
        joined = "\n".join(dedented)
    else:
        paragraphs: list[str] = []
        cur_para: list[str] = []
        for dline in dedented:
            if not dline.strip():
                if cur_para:
                    paragraphs.append(" ".join(cur_para))
                    cur_para = []
                paragraphs.append("")
            elif dline.startswith((" ", "\t")):
                if cur_para:
                    paragraphs.append(" ".join(cur_para))
                    cur_para = []
                paragraphs.append(dline)
            else:
                cur_para.append(dline.strip())
        if cur_para:
            paragraphs.append(" ".join(cur_para))
        # Collapse single blank lines between folded paragraphs into standard newline breaks
        out_parts: list[str] = []
        idx = 0
        while idx < len(paragraphs):
            if paragraphs[idx] == "":
                blank_run = 0
                while idx < len(paragraphs) and paragraphs[idx] == "":
                    blank_run += 1
                    idx += 1
                out_parts.append("\n" * blank_run)
            else:
                if out_parts and not out_parts[-1].endswith("\n"):
                    out_parts.append("\n")
                out_parts.append(paragraphs[idx])
                idx += 1
        joined = "".join(out_parts)

    if chomp == "+":
        return joined
    return joined.rstrip("\r\n")


_FM_LIST_RE = re.compile(r"^\s*-\s*(.*)$")
_FM_KEY_GUARD_RE = re.compile(r"^[A-Za-z0-9_-]+:(?:\s|$)")
_FM_KV_RE = re.compile(r"^([A-Za-z0-9_-]+):(?:\s+(.*)|\s*)$")
_FM_BLOCK_RE = re.compile(r"^([|>])([+-]?)$")
_WHITESPACE_RE = re.compile(r"\s+")


@functools.lru_cache(maxsize=64)
def _section_pattern(heading: str) -> re.Pattern[str]:
    return re.compile(
        rf"^##\s+{re.escape(heading)}\s*\n(.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )


def parse_frontmatter_values(lines: list[str]) -> dict:
    values: dict[str, str | list[str]] = {}
    current_key: str | None = None
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped_no_comment = _strip_yaml_comment(line).strip()
        if not stripped_no_comment:
            i += 1
            continue

        list_match = _FM_LIST_RE.match(line)
        if list_match and current_key and not _FM_KEY_GUARD_RE.match(line):
            values.setdefault(current_key, [])
            if isinstance(values[current_key], list):
                raw_item = list_match.group(1)
                if raw_item.strip():
                    parsed_item = _parse_yaml_scalar(raw_item)
                    if parsed_item or raw_item.strip() in ('""', "''"):
                        values[current_key].append(parsed_item)
            i += 1
            continue

        match = _FM_KV_RE.match(line)
        if not match:
            current_key = None
            i += 1
            continue

        key = match.group(1)
        raw_val = match.group(2) if match.group(2) is not None else ""
        clean_val = _strip_yaml_comment(raw_val).strip()

        if not clean_val:
            values[key] = []
            current_key = key
            i += 1
            continue

        block_match = _FM_BLOCK_RE.match(clean_val)
        if block_match:
            style, chomp = block_match.group(1), block_match.group(2)
            i += 1
            block_lines: list[str] = []
            while i < n:
                nxt = lines[i]
                if nxt.strip() and not nxt.startswith((" ", "\t")):
                    break
                block_lines.append(nxt)
                i += 1
            values[key] = _fold_block_scalar(block_lines, style, chomp)
            current_key = None
            continue

        if clean_val.startswith("["):
            flow_lines = [raw_val]
            depth = _flow_bracket_delta(raw_val)
            i += 1
            while depth > 0 and i < n:
                flow_lines.append(lines[i])
                depth += _flow_bracket_delta(lines[i])
                i += 1
            values[key] = parse_scalar_list("\n".join(flow_lines))
            current_key = None
            continue

        values[key] = _parse_yaml_scalar(raw_val)
        current_key = key
        i += 1

    return values


def section_text(body: str, heading: str) -> str:
    match = _section_pattern(heading).search(body or "")
    if not match:
        return ""
    return _WHITESPACE_RE.sub(" ", match.group(1)).strip()


def normalize_status(status: str | None) -> str:
    value = (status or DEFAULT_STATUS).strip()
    return value if value in REVIEW_STATUSES else DEFAULT_STATUS


def parse_atom_record_text(filename: str, content: str) -> dict:
    parsed = split_frontmatter(content)
    if parsed is None:
        raise ValueError(f"missing frontmatter: {filename}")

    frontmatter, body_lines = parsed
    values = parse_frontmatter_values(frontmatter)
    body = "\n".join(body_lines)

    sources = values.get("sources", [])
    if isinstance(sources, str):
        sources = parse_scalar_list(sources)

    contexts = values.get("contexts", [])
    if isinstance(contexts, str):
        contexts = parse_scalar_list(contexts)

    supersedes = values.get("supersedes", [])
    if isinstance(supersedes, str):
        supersedes = parse_scalar_list(supersedes)

    superseded_by = values.get("superseded_by", [])
    if isinstance(superseded_by, str):
        superseded_by = parse_scalar_list(superseded_by)

    contested_by = values.get("contested_by", [])
    if isinstance(contested_by, str):
        contested_by = parse_scalar_list(contested_by)

    return {
        "filename": filename,
        "id": str(values.get("id", "")),
        "memory_type": str(values.get("memory_type", "semantic") or "semantic"),
        "review_status": normalize_status(str(values.get("review_status", DEFAULT_STATUS))),
        "origin": str(values.get("origin", "imported_unknown") or "imported_unknown"),
        "citation_integrity": str(values.get("citation_integrity", "missing") or "missing"),
        "claim_state": str(values.get("claim_state", "candidate") or "candidate"),
        "epistemic_label": str(values.get("epistemic_label", "claim") or "claim"),
        "epistemic_contract": str(values.get("epistemic_contract", "") or ""),
        "claim": section_text(body, "Claim"),
        "evidence": section_text(body, "Evidence"),
        "confidence": str(values.get("confidence", "")),
        "importance": str(values.get("importance", "")),
        "valid_from": str(values.get("valid_from", "") or "").strip(),
        "valid_until": str(values.get("valid_until", "") or "").strip(),
        "sources": list(sources),
        "contexts": list(contexts),
        "supersedes": list(supersedes),
        "superseded_by": list(superseded_by),
        "contested_by": list(contested_by),
    }


def parse_atom_record(path: str | Path) -> dict:
    atom_path = Path(path)
    content = atom_path.read_text(encoding="utf-8")
    return parse_atom_record_text(atom_path.name, content)


def list_review_queue(atoms_dir: str | None = None, statuses: set[str] | None = None) -> list[dict]:
    eff_atoms_dir = resolve_module_vault_path(globals(), "ATOMS_DIR", atoms_dir)
    wanted = {normalize_status(status) for status in (statuses or {DEFAULT_STATUS})}
    records = []
    if not os.path.isdir(eff_atoms_dir):
        return records

    for filename in sorted(os.listdir(eff_atoms_dir)):
        if not is_content_markdown(filename):
            continue
        path = os.path.join(eff_atoms_dir, filename)
        try:
            record = parse_atom_record(path)
        except Exception as exc:
            record = {
                "filename": filename,
                "id": "",
                "memory_type": "",
                "review_status": "needs_edit",
                "claim": f"Could not parse atom: {exc}",
                "confidence": "",
                "importance": "",
                "sources": [],
            }
        if record["review_status"] in wanted:
            records.append(record)
    return records


def format_table(records: list[dict]) -> str:
    headers = ["status", "type", "confidence", "importance", "filename", "claim", "sources"]
    rows = [
        [
            record["review_status"],
            record["memory_type"],
            record["confidence"],
            record["importance"],
            record["filename"],
            record["claim"],
            ", ".join(record["sources"]),
        ]
        for record in records
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows)) if rows else len(headers[index])
        for index in range(len(headers))
    ]
    lines = [
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)),
        "  ".join("-" * width for width in widths),
    ]
    for row in rows:
        lines.append("  ".join(row[index].ljust(widths[index]) for index in range(len(headers))))
    return "\n".join(lines)


def parse_statuses(value: str) -> set[str]:
    if value.strip().lower() == "all":
        return set(REVIEW_STATUSES)
    return {normalize_status(status.strip()) for status in value.split(",") if status.strip()}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="List Second Brain memory atoms by review status.")
    parser.add_argument("--atoms-dir", default=None, help="Directory containing atom Markdown files.")
    parser.add_argument(
        "--status",
        default=DEFAULT_STATUS,
        help="Comma-separated review statuses to list, or 'all'. Defaults to pending.",
    )
    parser.add_argument("--json", action="store_true", help="Emit structured JSON for reuse by other tools.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    records = list_review_queue(args.atoms_dir, statuses=parse_statuses(args.status))
    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
    else:
        print(format_table(records))
        print(f"\nREVIEW_QUEUE_COUNT {len(records)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
