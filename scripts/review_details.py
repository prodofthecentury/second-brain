# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Read-only evidence and relationship details for the owner review UI."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath

from scripts.config import safe_file_in_dir
from scripts.evidence import ANCHOR_FIELDS, EvidenceValidationError, find_evidence_anchor
from scripts.memory_format import split_frontmatter
from scripts.memory_review import parse_atom_record, parse_frontmatter_values


def atom_review_details(content: str, *, atom_path: str, atoms_dir: str, brain_dir: str) -> dict:
    parsed = split_frontmatter(content)
    frontmatter = parsed[0] if parsed else []
    metadata = parse_frontmatter_values(frontmatter)
    anchor = {key: metadata.get(key) for key in ANCHOR_FIELDS}
    result = {
        "atom_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "anchor": anchor,
        "stored_quote": "",
        "current_source_sha256": None,
        "current_quote": None,
        "anchor_current": False,
        "source_error": None,
        "related_atoms": [],
    }

    if parsed:
        body = "\n".join(parsed[1])
        evidence_section = body.split("## Evidence", 1)
        if len(evidence_section) == 2:
            result["stored_quote"] = evidence_section[1].split("\n## ", 1)[0].strip()

    if all(anchor.values()):
        source_ref = str(anchor["evidence_source"]).replace("\\", "/")
        relative = PurePosixPath(source_ref)
        if relative.is_absolute() or ".." in relative.parts:
            result["source_error"] = "Source anchor path is absolute or traverses parent directories."
        else:
            source_path = Path(brain_dir).joinpath(*relative.parts)
            try:
                resolved = source_path.resolve(strict=True)
                roots = [(Path(brain_dir) / "0-raw").resolve(), (Path(brain_dir) / "sources").resolve()]
                if not resolved.is_file() or not any(resolved.is_relative_to(root) for root in roots):
                    raise ValueError("Source anchor points outside durable source directories.")
                source_bytes = resolved.read_bytes()
                result["current_source_sha256"] = hashlib.sha256(source_bytes).hexdigest()
                start, end = int(anchor["evidence_start"]), int(anchor["evidence_end"])
                if start < 0 or end < start or end > len(source_bytes):
                    raise ValueError("Stored source byte offsets are invalid for the current source file.")
                result["current_quote"] = source_bytes[start:end].decode("utf-8")
                current_anchor = find_evidence_anchor(content, str(brain_dir))
                result["anchor_current"] = all(
                    str(current_anchor.get(key)) == str(anchor.get(key)) for key in ANCHOR_FIELDS
                )
            except (OSError, UnicodeDecodeError, ValueError, EvidenceValidationError) as exc:
                result["source_error"] = str(exc)
    else:
        result["source_error"] = "No complete verified source anchor is stored on this atom."

    relation_names = []
    for key in ("supersedes", "superseded_by", "contested_by", "links"):
        values = metadata.get(key, [])
        if isinstance(values, str):
            values = [values]
        for name in values or []:
            if str(name) not in relation_names:
                relation_names.append(str(name))
    for name in relation_names:
        path = safe_file_in_dir(atoms_dir, name, allowed_exts={".md"})
        if not path:
            result["related_atoms"].append({"filename": name, "error": "Missing or unsafe atom path."})
            continue
        try:
            record = parse_atom_record(path)
            result["related_atoms"].append({
                "filename": name,
                "review_status": record["review_status"],
                "claim": record["claim"],
            })
        except Exception as exc:
            result["related_atoms"].append({"filename": name, "error": f"Unreadable atom: {exc}"})
    return result
