# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""
List reviewable memory atoms.

This is the first lightweight review queue surface. It returns structured
records for reuse by CLI, dashboard, or later MCP tools.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from config import ATOMS_DIR, is_content_markdown
from memory_format import split_frontmatter


DEFAULT_STATUS = "pending"
REVIEW_STATUSES = {"pending", "accepted", "needs_edit", "rejected", "merged"}


def parse_scalar_list(value: str) -> list[str]:
    value = (value or "").strip()
    if not value:
        return []
    if value == "[]":
        return []
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("\"'") for item in inner.split(",") if item.strip()]
    return [value.strip().strip("\"'")]


def parse_frontmatter_values(lines: list[str]) -> dict:
    values: dict[str, str | list[str]] = {}
    current_key = None

    for line in lines:
        if not line.strip():
            continue

        list_match = re.match(r"^\s*-\s+(.*)$", line)
        if list_match and current_key:
            values.setdefault(current_key, [])
            if isinstance(values[current_key], list):
                values[current_key].append(list_match.group(1).strip().strip("\"'"))
            continue

        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not match:
            current_key = None
            continue

        key, value = match.group(1), match.group(2).strip()
        current_key = key
        if value:
            values[key] = parse_scalar_list(value) if value.startswith("[") else value.strip("\"'")
        else:
            values[key] = []

    return values


def section_text(body: str, heading: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*\n(.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(body or "")
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def normalize_status(status: str | None) -> str:
    value = (status or DEFAULT_STATUS).strip()
    return value if value in REVIEW_STATUSES else DEFAULT_STATUS


def parse_atom_record(path: str | Path) -> dict:
    atom_path = Path(path)
    content = atom_path.read_text(encoding="utf-8")
    parsed = split_frontmatter(content)
    if parsed is None:
        raise ValueError(f"missing frontmatter: {atom_path.name}")

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

    return {
        "filename": atom_path.name,
        "id": str(values.get("id", "")),
        "memory_type": str(values.get("memory_type", "semantic") or "semantic"),
        "review_status": normalize_status(str(values.get("review_status", DEFAULT_STATUS))),
        "origin": str(values.get("origin", "imported_unknown") or "imported_unknown"),
        "citation_integrity": str(values.get("citation_integrity", "missing") or "missing"),
        "claim_state": str(values.get("claim_state", "candidate") or "candidate"),
        "epistemic_label": str(values.get("epistemic_label", "claim") or "claim"),
        "claim": section_text(body, "Claim"),
        "confidence": str(values.get("confidence", "")),
        "importance": str(values.get("importance", "")),
        "sources": list(sources),
        "contexts": list(contexts),
        "supersedes": list(supersedes),
        "superseded_by": list(superseded_by),
    }


def list_review_queue(atoms_dir: str = ATOMS_DIR, statuses: set[str] | None = None) -> list[dict]:
    wanted = {normalize_status(status) for status in (statuses or {DEFAULT_STATUS})}
    records = []
    if not os.path.isdir(atoms_dir):
        return records

    for filename in sorted(os.listdir(atoms_dir)):
        if not is_content_markdown(filename):
            continue
        path = os.path.join(atoms_dir, filename)
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
    parser.add_argument("--atoms-dir", default=ATOMS_DIR, help="Directory containing atom Markdown files.")
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
