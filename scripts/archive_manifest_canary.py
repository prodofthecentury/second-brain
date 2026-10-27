# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = [
#     "ollama",
# ]
# ///
"""
Controlled canary for archive manifest behavior.

This exercises the real refinery `process_single_file` path while patching out
model and embedding calls, so it remains fast and deterministic.
"""

from __future__ import annotations

import json
import os
import tempfile

import ollama_refinery as refinery
from memory_format import normalize_memory_document
from evidence import find_evidence_anchor


class FakeCollection:
    def upsert(self, documents, embeddings, metadatas, ids):
        return None


def check(condition: bool, message: str):
    if not condition:
        raise AssertionError(message)


def draft_canary_atom(raw_content: str, raw_filename: str, rules: str, **kwargs) -> list[str]:
    atom = f"""---
id: archive-manifest-canary
type: atom
memory_type: self
certainty: tentative
created: 2026-07-14
last-reviewed: 2026-07-14
importance: 0.50
confidence: 0.50
sources:
  - {raw_filename}
links: []
tags: []
contexts:
  - offline
supersedes: []
superseded_by: []
contested_by: []
review_status: pending
---

## Claim
The archive manifest canary verifies that processed raw captures remain auditable.

## Evidence
{raw_content.strip()}

## Why It Matters
The local brain must preserve a source trail before broader ingestion automation is trusted.
"""
    return [normalize_memory_document(atom, source_filename=raw_filename, current_date="2026-07-14")]


def run_case(root: str, archive: bool):
    brain_dir = os.path.join(root, "brain")
    raw_dir = os.path.join(brain_dir, "0-raw")
    atoms_dir = os.path.join(brain_dir, "2-atoms")
    archive_dir = os.path.join(raw_dir, "archive", "2026-07")
    manifest_path = os.path.join(raw_dir, "archive", "manifest.jsonl")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(atoms_dir, exist_ok=True)

    source_path = os.path.join(raw_dir, "canary.md")
    with open(source_path, "w", encoding="utf-8") as file:
        file.write("Controlled source for archive manifest canary.")

    original_atoms_dir = refinery.ATOMS_DIR
    original_archive_dir = refinery.archive_dir
    original_append_manifest = refinery.append_archive_manifest_entry
    original_draft = refinery.draft_memory_atoms
    original_embed = refinery.embed_atom_content
    original_verify_embedding = refinery.verify_embedding_available

    try:
        refinery.ATOMS_DIR = atoms_dir
        refinery.archive_dir = lambda: archive_dir
        refinery.append_archive_manifest_entry = (
            lambda entry: original_append_manifest(entry, manifest_path=manifest_path)
        )
        refinery.draft_memory_atoms = draft_canary_atom
        refinery.embed_atom_content = lambda collection, atom_file, content: None
        refinery.verify_embedding_available = lambda: None

        saved = refinery.process_single_file(
            source_path,
            collection=FakeCollection(),
            rules="canary",
            sync_existing=False,
            archive=archive,
            allow_fallback=False,
            run_critic=False,
        )
    finally:
        refinery.ATOMS_DIR = original_atoms_dir
        refinery.archive_dir = original_archive_dir
        refinery.append_archive_manifest_entry = original_append_manifest
        refinery.draft_memory_atoms = original_draft
        refinery.embed_atom_content = original_embed
        refinery.verify_embedding_available = original_verify_embedding

    check(saved == ["archive-manifest-canary.md"], "canary atom filename is stable")
    atom_path = os.path.join(atoms_dir, "archive-manifest-canary.md")
    check(os.path.exists(atom_path), "canary atom was written")
    with open(atom_path, "r", encoding="utf-8") as file:
        atom_content = file.read()

    if archive:
        check(not os.path.exists(source_path), "archived canary source was moved out of raw")
        archived_path = os.path.join(archive_dir, "canary.md")
        check(os.path.exists(archived_path), "archived canary source exists")
        check(os.path.exists(manifest_path), "archive manifest was written")
        with open(manifest_path, "r", encoding="utf-8") as file:
            entries = [json.loads(line) for line in file if line.strip()]
        check(len(entries) == 1, "archive manifest has one canary entry")
        entry = entries[0]
        check(entry["source_filename"] == "canary.md", "manifest records source filename")
        check(entry["generated_atoms"] == saved, "manifest records generated atoms")
        check(entry["archived_path"].endswith("canary.md"), "manifest records archived path")
        anchor = find_evidence_anchor(atom_content, brain_dir)
        check(anchor["evidence_source"].endswith("0-raw/archive/2026-07/canary.md"), "atom source points to the exact archived capture")
    else:
        check(os.path.exists(source_path), "keep-source canary leaves source in raw")
        check(not os.path.exists(manifest_path), "keep-source canary does not write archive manifest")
        anchor = find_evidence_anchor(atom_content, brain_dir)
        check(anchor["evidence_source"] == "0-raw/canary.md", "atom source points to the preserved raw capture")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="second-brain-archive-canary-") as temp_dir:
        run_case(os.path.join(temp_dir, "keep-source"), archive=False)
        run_case(os.path.join(temp_dir, "archive"), archive=True)
    print("ARCHIVE_MANIFEST_CANARY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
