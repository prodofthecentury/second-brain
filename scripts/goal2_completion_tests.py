# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Comprehensive unit and end-to-end integration tests for Goal 2 completion.

Covers:
1. Local document ingestion & import (.pdf, .docx, .epub, .html, .csv, .tsv) with
   immutable binary archival, ARCHIVE_MANIFEST SHA-256 lineage, [page N] spans,
   and cryptographic byte-offset source anchoring / owner approval.
2. Context-window-safe Refinery chunking with verbatim byte-anchor preservation,
   incremental vector carry-forward in rebuild_vectors.py, and batched atomic
   JsonVectorCollection writes.
3. Multimodal provenance parity ([MM:SS.mmm -> MM:SS.mmm] timestamps, YAML
   provenance frontmatter, Pillow fail-closed image gate, setup flags, doctor).
4. Hardware-gated watchdog daemon + debounced notes/ & 2-atoms/ FTS5 sync and
   read-only stale-anchor detection.
5. Honest Editor deferral (no counterfeit active stub threads), project:* scope
   partitioning, weekly briefings, full 8-field ExperienceRecord spec, and
   structured action-item extraction + project-scoped note search.
6. "research" Answer View abstention & [UNVERIFIED LEADS] structural isolation,
   multi-host streaming chat (OpenAI/Anthropic/Gemini/custom local via MockTransport)
   with per-host scope containment, outbound secret blocking, indexed PII redaction
   & local rehydration, retrieval_mode + candidate_atom_ids telemetry, sentence-level
   stream buffering, and uncited short declarative sentence enforcement.
7. Upgraded Contradiction Radar (subject/attribute alignment on numeric conflicts,
   mutually exclusive entity/value families with multi-support guards, optional local
   NLI adjudication + offline degradation, scoped graph_neighbors).
8. Full 17-tool MCP surface (audit, scope containment, post-snapshot revocation guard,
   secret blocking, telemetry-only feedback), 6-host IDE connector add/remove symmetry
   + safe Claude Code .mcp.json bootstrap, and real comparative SOTA benchmark invariants.
"""

from __future__ import annotations

import asyncio
import builtins
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _init_temp_vault(root: Path) -> Path:
    vault = root / "brain"
    for sub in (
        "0-raw",
        "0-raw/inbox",
        "0-raw/archive",
        "0-raw/note-snapshots",
        "1-desk",
        "2-atoms",
        "3-threads",
        "4-experiences",
        "5-playbooks",
        "sources",
        "briefings",
        "notes",
        ".sidecar",
        ".chroma",
    ):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    (vault / "house-rules.md").write_text(
        "# House Rules\nExtract atomic claims with exact verbatim ## Evidence quotes.\n",
        encoding="utf-8",
    )
    return vault


class DocumentIngestionAndChunkedVectorTests(unittest.TestCase):
    def test_pdf_docx_epub_html_csv_ingest_and_byte_offset_approval(self) -> None:
        import bulk_import as bi
        from doc_fixtures import write_docx, write_epub, write_text_pdf
        from evidence import find_evidence_anchor
        import ingest_multimodal as im
        from memory_format import capture_origin, split_frontmatter
        from notes_store import NotePaths, get_note, search_notes, verify_note_excerpt
        from review_atom import apply_review_transition, approved_revision_is_current, content_sha256

        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            vault = _init_temp_vault(t)
            ext_dir = t / "external_docs"
            ext_dir.mkdir()

            pdf_file = write_text_pdf(
                ext_dir / "raft-paper.pdf",
                [
                    [
                        "Raft Consensus Architecture",
                        "Abstract",
                        "Leader completeness guarantees committed log entries appear in all future leaders.",
                        "RunningHeader Page 1",
                    ],
                    [
                        "1. Election Safety",
                        "At most one leader can be elected in a given term across any cluster configuration.",
                        "RunningHeader Page 2",
                    ],
                    [
                        "2. Log Matching",
                        "Two logs with identical index and term store identical commands up through that index.",
                        "RunningHeader Page 3",
                    ],
                ],
                title="Raft Consensus Specification",
            )
            pdf_sha = hashlib.sha256(pdf_file.read_bytes()).hexdigest()

            with (
                patch.object(im, "RAW_DIR", str(vault / "0-raw")),
                patch.object(im, "INBOX_DIR", str(vault / "0-raw" / "inbox")),
                patch.object(im, "SIDECAR_DIR", str(vault / ".sidecar")),
                patch.dict(os.environ, {"BRAIN_VAULT_DIR": str(vault)}, clear=False),
            ):
                rc = im.main(["--file", str(pdf_file)])
                self.assertEqual(rc, 0)

            # External binary must stay untouched while an immutable archived copy + manifest entry exist.
            self.assertTrue(pdf_file.is_file())
            raw_captures = sorted(p for p in (vault / "0-raw").glob("*-doc-raft-paper.md"))
            self.assertEqual(len(raw_captures), 1)
            cap_path = raw_captures[0]
            cap_text = cap_path.read_text(encoding="utf-8")
            self.assertEqual(capture_origin(cap_text, cap_path.name), "user_local")

            parsed = split_frontmatter(cap_text)
            self.assertIsNotNone(parsed)
            fm_text = "\n".join(parsed[0])
            for field_name in ("extractor:", "model:", "model_digest:", "source_binary:", "source_sha256:", "limitations:"):
                self.assertIn(field_name, fm_text)
            self.assertIn(pdf_sha, fm_text)
            self.assertIn("## [page 1]", cap_text)
            self.assertIn("## [page 2]", cap_text)
            self.assertIn("- [page 1] span ", cap_text)

            manifest_entries = [
                json.loads(line)
                for line in (vault / "0-raw" / "archive" / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(manifest_entries), 1)
            self.assertEqual(manifest_entries[0]["source_sha256"], pdf_sha)
            self.assertEqual(manifest_entries[0]["archived_sha256"], pdf_sha)
            self.assertTrue((vault / manifest_entries[0]["archived_path"]).is_file())

            # Create a pending atom anchored to the ingested PDF Markdown and approve via CAS!
            atom_path = vault / "2-atoms" / "raft-leader-completeness.md"
            atom_body = (
                "---\n"
                "id: raft-leader-completeness\n"
                "type: atom\n"
                "memory_type: semantic\n"
                "origin: user_local\n"
                "citation_integrity: missing\n"
                "claim_state: candidate\n"
                "epistemic_label: claim\n"
                "review_status: pending\n"
                "certainty: tentative\n"
                "created: 2026-11-18\n"
                "last-reviewed: 2026-11-18\n"
                "importance: 0.85\n"
                "confidence: 0.90\n"
                f"sources:\n  - 0-raw/{cap_path.name}\n"
                "links: []\n"
                "tags: [consensus, raft]\n"
                "contexts: [project:distributed]\n"
                "supersedes: []\n"
                "superseded_by: []\n"
                "contested_by: []\n"
                "---\n\n"
                "## Claim\n"
                "Raft leader completeness guarantees committed log entries appear in all future leaders.\n\n"
                "## Evidence\n"
                '"Leader completeness guarantees committed log entries appear in all future leaders."\n\n'
                "## Why It Matters\n"
                "Prevents committed write loss across leader transitions.\n"
            )
            atom_path.write_text(atom_body, encoding="utf-8")
            anchor = find_evidence_anchor(atom_body, str(vault))
            self.assertEqual(anchor["evidence_source"], f"0-raw/{cap_path.name}")
            app_res = apply_review_transition(
                atom_path=str(atom_path),
                action="approve",
                expected_sha256=content_sha256(atom_path.read_bytes()),
                actor="owner",
                brain_dir=str(vault),
                atoms_dir=str(vault / "2-atoms"),
            )
            self.assertEqual(app_res["action"], "approve")
            self.assertTrue(
                approved_revision_is_current(
                    str(atom_path),
                    brain_dir=str(vault),
                    atoms_dir=str(vault / "2-atoms"),
                )
            )

            # Test bulk_import across DOCX, EPUB, HTML, CSV, TSV + verify_note_excerpt approval!
            write_docx(
                ext_dir / "security.docx",
                [
                    ("Heading1", "Transport Encryption"),
                    ("", "All replication channels enforce mutual TLS 1.3 with pinned root certificates."),
                ],
                table=[["setting", "value"], ["min_tls", "1.3"]],
            )
            write_epub(
                ext_dir / "handbook.epub",
                "Storage Handbook",
                [("Write-Ahead Logging", ["Every durability checkpoint flushes fsync before acknowledging commit."])],
            )
            (ext_dir / "guide.html").write_text(
                "<html><head><title>API Guide</title><script>evil()</script></head>"
                "<body><h1>Rate Limiting</h1><p>Loopback endpoints enforce per-session token bucket limits.</p></body></html>",
                encoding="utf-8",
            )
            (ext_dir / "metrics.csv").write_text("metric,threshold_ms\np95_recall,15\n", encoding="utf-8")
            (ext_dir / "routes.tsv").write_text("route\tauth_tier\n/v1/ledger/commit\towner_token\n", encoding="utf-8")

            npaths = NotePaths.for_brain(vault)
            imp_stats = bi.import_folder(ext_dir, paths=npaths, tags=("project:distributed",))
            self.assertEqual(imp_stats.created, 6)
            imp_again = bi.import_folder(ext_dir, paths=npaths, tags=("project:distributed",))
            self.assertEqual(imp_again.unchanged, 6)
            self.assertEqual(imp_again.created, 0)

            docx_hits = search_notes("mutual TLS", project="distributed", paths=npaths)
            self.assertEqual(len(docx_hits), 1)
            self.assertEqual(search_notes("mutual TLS", project="other_proj", paths=npaths), [])
            docx_note = get_note(docx_hits[0].filename, paths=npaths)
            ver = verify_note_excerpt(
                docx_note.filename,
                "All replication channels enforce mutual TLS 1.3 with pinned root certificates.",
                expected_note_sha256=docx_note.sha256,
                claim="Replication channels enforce mutual TLS 1.3 with pinned root certificates.",
                actor="owner",
                paths=npaths,
            )
            self.assertTrue(str(ver["atom_filename"]).endswith(".md"))
            self.assertTrue(
                approved_revision_is_current(
                    str(vault / "2-atoms" / ver["atom_filename"]),
                    brain_dir=str(vault),
                    atoms_dir=str(vault / "2-atoms"),
                )
            )

    def test_refinery_chunking_incremental_vectors_and_batched_json_store(self) -> None:
        from evidence import find_evidence_anchor
        from index_manifest import index_manifest_is_current, load_index_manifest
        import ollama_refinery as ref
        import rebuild_vectors as rv
        from vector_store import JsonVectorCollection

        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            vault = _init_temp_vault(t)

            # Build a multi-section >12,000 char document and verify unmodified slices + drafting
            sections = []
            for idx in range(1, 7):
                para = " ".join(
                    f"Section {idx} unique invariant statement {idx}-{j}: deterministic verification gate {idx}.{j} "
                    f"prevents silent provenance drift across distributed checkpoints."
                    for j in range(1, 19)
                )
                sections.append(f"## [page {idx}]\n\n### Section {idx}\n\n{para}")
            long_doc = "\n\n".join(sections) + "\n"
            raw_file = vault / "0-raw" / "long-paper.md"
            raw_file.write_text(long_doc, encoding="utf-8")

            chunks = ref.chunk_raw_document(long_doc, max_chunk_chars=3200, overlap_chars=300)
            self.assertGreater(len(chunks), 3)
            for ch in chunks:
                self.assertEqual(ch["text"], long_doc[ch["char_start"] : ch["char_end"]])

            call_idx = {"n": 0}

            def _mock_llm(*, messages, model=None, **kwargs):
                call_idx["n"] += 1
                idx = call_idx["n"]
                prompt_text = messages[0]["content"]
                raw_slice = prompt_text.split("RAW CAPTURE:\n", 1)[1]
                if idx == 2:
                    return {"message": {"content": "NONE"}}
                m = None
                for line in raw_slice.splitlines():
                    if "unique invariant statement" in line:
                        m = line.split(".")[0] + "."
                        break
                if not m:
                    return {"message": {"content": "NONE"}}
                atom_md = (
                    f"```markdown\n---\nid: chunk-claim-{idx}\ntype: atom\nmemory_type: semantic\n"
                    f"sources:\n  - 0-raw/long-paper.md\n---\n\n"
                    f"## Claim\nChunk claim {idx}: {m[:80]}\n\n"
                    f"## Evidence\n\"{m}\"\n\n"
                    f"## Why It Matters\nVerifies multi-chunk extraction.\n```"
                )
                return {"message": {"content": atom_md}}

            with patch.object(ref, "chat_with_fallback", side_effect=_mock_llm):
                drafted = ref.draft_memory_atoms(
                    long_doc,
                    "0-raw/long-paper.md",
                    "House rules",
                    max_chunk_chars=3200,
                    overlap_chars=300,
                )
            self.assertGreaterEqual(len(drafted), 2)
            for atom_doc in drafted:
                anc = find_evidence_anchor(atom_doc, str(vault))
                self.assertEqual(anc["evidence_source"], "0-raw/long-paper.md")

            from vector_store import JsonVectorCollection, json_collection_path

            # Verify batched JsonVectorCollection writes once per batch instead of O(N^2)
            json_store_dir = t / "json_vec"
            json_store_dir.mkdir()
            col = JsonVectorCollection(json_collection_path(str(json_store_dir), "atoms"), "atoms")
            save_calls = {"count": 0}
            orig_save = col._save

            def _counted_save(*a, **kw):
                save_calls["count"] += 1
                return orig_save(*a, **kw)

            col._save = _counted_save  # type: ignore[method-assign]
            with col.batch():
                for i in range(25):
                    col.upsert(
                        documents=[f"doc {i}"],
                        embeddings=[[1.0, float(i + 1), 0.5]],
                        metadatas=[{"filename": f"a-{i}.md", "content_sha256": f"{i:064x}", "embedding_model": "m"}],
                        ids=[f"a-{i}.md"],
                    )
            self.assertEqual(save_calls["count"], 1)
            got = col.get(include=["embeddings", "metadatas"])
            self.assertEqual(len(got["ids"]), 25)

            # Verify incremental carry-forward in rebuild_vectors.py
            atoms_dir = vault / "2-atoms"
            for i in range(4):
                (atoms_dir / f"vec-atom-{i}.md").write_text(
                    f"---\nid: vec-atom-{i}\ntype: atom\nmemory_type: semantic\n---\n\n"
                    f"## Claim\nVector claim {i}.\n\n## Evidence\nQuote {i}.\n",
                    encoding="utf-8",
                )
            embed_calls: list[str] = []

            def _fake_embed(text: str, model: str = "nomic-embed-text") -> list[float]:
                embed_calls.append(text[:30])
                h = hashlib.sha256(text.encode("utf-8")).digest()
                return [round((b + 1) / 256.0, 6) for b in h[:8]]

            chroma_dir = vault / ".chroma"
            with patch.object(rv, "embed_text", side_effect=_fake_embed):
                first_stats = rv.rebuild_vectors(
                    atoms_dir=str(atoms_dir),
                    store_path=str(chroma_dir),
                    prefer_json=True,
                )
            self.assertEqual(first_stats["indexed"], 4)
            self.assertEqual(first_stats["embedded"], 4)
            self.assertEqual(first_stats["reused"], 0)

            # Modify only 1 of the 4 atoms; rebuild must reuse 3 and embed only 1 (+ 1 preflight)!
            (atoms_dir / "vec-atom-2.md").write_text(
                "---\nid: vec-atom-2\ntype: atom\nmemory_type: semantic\n---\n\n"
                "## Claim\nUpdated vector claim 2.\n\n## Evidence\nUpdated quote 2.\n",
                encoding="utf-8",
            )
            embed_calls.clear()
            with patch.object(rv, "embed_text", side_effect=_fake_embed):
                second_stats = rv.rebuild_vectors(
                    atoms_dir=str(atoms_dir),
                    store_path=str(chroma_dir),
                    prefer_json=True,
                )
            self.assertEqual(second_stats["indexed"], 4)
            self.assertEqual(second_stats["reused"], 3)
            self.assertEqual(second_stats["embedded"], 1)
            self.assertEqual(len(embed_calls), 2)  # 1 preflight + 1 changed atom
            manifest = load_index_manifest(str(chroma_dir))
            self.assertTrue(index_manifest_is_current(manifest, str(atoms_dir)))


class MultimodalDaemonEditorAndCognitiveTests(unittest.TestCase):
    def test_whisper_timestamps_pillow_fail_closed_and_provenance(self) -> None:
        import ingest_multimodal as im
        from memory_format import split_frontmatter

        segs = [
            SimpleNamespace(start=0.5, end=3.125, text=" First spoken observation. "),
            SimpleNamespace(start=62.0, end=66.04, text="Second spoken observation."),
        ]
        rendered = im.format_transcript_segments(segs)
        self.assertIn("[00:00.500 -> 00:03.125] First spoken observation.", rendered)
        self.assertIn("[01:02.000 -> 01:06.040] Second spoken observation.", rendered)

        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            vault = _init_temp_vault(t)
            audio_file = vault / "0-raw" / "memo.wav"
            audio_file.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
            with (
                patch.object(im, "RAW_DIR", str(vault / "0-raw")),
                patch.object(im, "INBOX_DIR", str(vault / "0-raw" / "inbox")),
                patch.object(im, "SIDECAR_DIR", str(vault / ".sidecar")),
                patch.object(im, "transcribe_audio", return_value=rendered),
            ):
                out_md = im.process_audio(str(audio_file))
            self.assertIsNotNone(out_md)
            self.assertFalse(audio_file.exists())  # moved into 0-raw/archive/
            cap_text = (vault / "0-raw" / out_md).read_text(encoding="utf-8")
            fm, _ = split_frontmatter(cap_text)
            fm_joined = "\n".join(fm)
            self.assertIn('extractor: "faster-whisper"', fm_joined)
            self.assertIn("source_binary:", fm_joined)
            self.assertIn("source_sha256:", fm_joined)
            self.assertIn("limitations:", fm_joined)
            self.assertIn("[00:00.500 -> 00:03.125]", cap_text)

            # Fail closed when Pillow is missing
            img_path = t / "diagram.png"
            img_path.write_bytes(b"\x89PNG\r\n\x1a\nraw")
            orig_import = builtins.__import__

            def _deny_pil(name, *args, **kwargs):
                if name == "PIL" or name.startswith("PIL."):
                    raise ImportError("No module named 'PIL'")
                return orig_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=_deny_pil):
                with self.assertRaises(RuntimeError) as ctx:
                    im.prepare_image_bytes(str(img_path))
                self.assertIn("Pillow (PIL) is required", str(ctx.exception))

    def test_watchdog_hardware_gate_and_debounced_watchers(self) -> None:
        from notes_store import NotePaths, create_note, search_notes
        import watchdog_daemon as wd

        with tempfile.TemporaryDirectory() as tmp:
            vault = _init_temp_vault(Path(tmp))
            base = wd.get_daemon_hardware_profile()
            tier0 = replace(
                base,
                total_ram_gib=4.0,
                ollama_reachable=False,
                tier_id="tier_0_lexical_only",
                selected_installed_generation_model=None,
            )
            raw_note = vault / "0-raw" / "pending-capture.md"
            raw_note.write_text("# Raw idea\nSome unrefined text.\n", encoding="utf-8")
            with (
                patch.object(wd, "RAW_DIR", str(vault / "0-raw")),
                patch.object(wd, "get_daemon_hardware_profile", return_value=tier0),
            ):
                res = wd.handle_new_file(str(raw_note))
            self.assertIsNone(res)
            self.assertTrue(raw_note.is_file())  # preserved safely in 0-raw/
            deferred_json = json.loads((vault / ".sidecar" / "deferred-captures.json").read_text(encoding="utf-8"))
            self.assertIn("pending-capture.md", deferred_json)

            # Debounced handler coalesces bursts and syncs notes FTS5 model-free
            npaths = NotePaths.for_brain(vault)
            note_file = Path(npaths.notes_dir) / "manual-edit.md"
            note_file.write_text("---\ntitle: Manual\ntags: [project:alpha]\n---\n\nUnindexed body keyword xylophone.\n", encoding="utf-8")
            calls = {"n": 0}

            def _cb():
                calls["n"] += 1
                wd.sync_notes_index_for_vault(brain_dir=str(vault), notes_dir=str(npaths.notes_dir))

            debouncer = wd.DebouncedMarkdownSyncHandler(str(npaths.notes_dir), _cb, debounce_seconds=0.05)
            for _ in range(5):
                debouncer.on_modified(SimpleNamespace(is_directory=False, src_path=str(note_file)))
            debouncer.flush()
            debouncer.stop()
            self.assertEqual(calls["n"], 1)
            self.assertEqual(len(search_notes("xylophone", project="alpha", paths=npaths)), 1)

    def test_editor_honest_deferral_project_partitioning_experience_spec_and_tasks(self) -> None:
        from cognitive_core import save_experience_record
        from notes_store import NotePaths, create_note, extract_action_items
        import ollama_editor as ed
        from review_atom import apply_review_transition, content_sha256

        with tempfile.TemporaryDirectory() as tmp:
            vault = _init_temp_vault(Path(tmp))
            src_md = vault / "0-raw" / "eng-source.md"
            src_md.write_text(
                "Alpha service isolates tenant encryption keys per region.\n"
                "Beta service enforces deterministic replay logs.\n"
                "Owner prefers dark solarized terminal themes.\n",
                encoding="utf-8",
            )

            def _make_approved_atom(fname: str, claim: str, quote: str, ctx: str, ep_label: str = "claim") -> Path:
                p = vault / "2-atoms" / fname
                p.write_text(
                    f"---\nid: {fname[:-3]}\ntype: atom\nmemory_type: semantic\norigin: user_local\n"
                    f"epistemic_label: {ep_label}\nreview_status: pending\nclaim_state: candidate\n"
                    f"sources:\n  - 0-raw/eng-source.md\ncontexts: [{ctx}]\nsupersedes: []\nsuperseded_by: []\n---\n\n"
                    f"## Claim\n{claim}\n\n## Evidence\n\"{quote}\"\n\n## Why It Matters\nTest.\n",
                    encoding="utf-8",
                )
                apply_review_transition(
                    atom_path=str(p),
                    action="approve",
                    expected_sha256=content_sha256(p.read_bytes()),
                    actor="owner",
                    brain_dir=str(vault),
                    atoms_dir=str(vault / "2-atoms"),
                )
                return p

            _make_approved_atom(
                "alpha-keys.md",
                "Alpha service isolates tenant encryption keys per region.",
                "Alpha service isolates tenant encryption keys per region.",
                "project:alpha",
            )
            _make_approved_atom(
                "beta-replay.md",
                "Beta service enforces deterministic replay logs.",
                "Beta service enforces deterministic replay logs.",
                "project:beta",
            )
            _make_approved_atom(
                "owner-theme-pref.md",
                "Owner prefers dark solarized terminal themes.",
                "Owner prefers dark solarized terminal themes.",
                "project:alpha",
                ep_label="preference",
            )

            with (
                patch.object(ed, "BRAIN_DIR", str(vault)),
                patch.object(ed, "ATOMS_DIR", str(vault / "2-atoms")),
                patch.object(ed, "THREADS_DIR", str(vault / "3-threads")),
                patch.object(ed, "BRIEFINGS_DIR", str(vault / "briefings")),
                patch.object(ed, "RAW_DIR", str(vault / "0-raw")),
            ):
                loaded = ed.load_atoms()
                loaded_names = {a["filename"] for a in loaded}
                self.assertEqual(loaded_names, {"alpha-keys.md", "beta-replay.md"})  # preference excluded!

                # Offline Ollama -> visibly defer, writing 0 counterfeit active stub threads!
                with patch.object(ed, "chat_with_fallback", side_effect=RuntimeError("Ollama offline")):
                    offline_res = ed.run_editor(include_briefing=True)
                self.assertEqual(list((vault / "3-threads").glob("*.md")), [])
                self.assertEqual(list((vault / "briefings").glob("*.md")), [])
                self.assertGreaterEqual(offline_res["deferred_clusters"], 1)

            # Full 8-field ExperienceRecord spec + action-item extraction
            exp = save_experience_record(
                goal="Validate multi-section PDF ingestion",
                result_summary="All byte-offset anchors resolved cleanly.",
                success=True,
                context="project:alpha",
                plan=["Generate PDF fixture", "Ingest to 0-raw", "Approve atom"],
                actions=["Ran ingest_multimodal.py", "Verified anchor"],
                tools_used=["ingest_multimodal", "review_atom"],
                reasoning_summary="UTF-8 byte offsets require stable LF normalization.",
                confidence_before=0.55,
                confidence_after=0.95,
                new_memories=["raft-leader-completeness.md"],
                updated_memories=["alpha-keys.md"],
                candidate_skills=["Verify PDF Byte-Offset Anchors"],
                reflection="Deterministic span coordinates eliminate manual quote edits.",
                lessons=["Keep source binaries archived separately from UTF-8 Markdown artifacts."],
                propose_to_inbox=True,
                experiences_dir=str(vault / "4-experiences"),
                inbox_dir=str(vault / "0-raw" / "inbox"),
                brain_dir=str(vault),
            )
            self.assertEqual(exp["confidence_before"], 0.55)
            self.assertEqual(exp["confidence_after"], 0.95)
            self.assertIsNotNone(exp["inbox_procedural_draft"])
            exp_text = (vault / "4-experiences" / exp["filename"]).read_text(encoding="utf-8")
            self.assertIn("## Plan", exp_text)
            self.assertIn("## Reasoning Summary", exp_text)
            self.assertIn("## Reflection", exp_text)

            npaths = NotePaths.for_brain(vault)
            create_note(
                "Sprint Backlog",
                "- [ ] Audit TLS rotation playbooks @due(2026-12-01) !high\n"
                "- [x] Ship PDF extractor\n"
                "```markdown\n- [ ] Ignored fenced code task\n```\n"
                "TODO: Benchmark HNSW recall latency [P2]\n",
                tags=("project:alpha", "sprint"),
                paths=npaths,
            )
            open_tasks = extract_action_items(paths=npaths, project="alpha", include_completed=False)
            self.assertEqual(len(open_tasks), 2)
            self.assertEqual(open_tasks[0]["due_date"], "2026-12-01")
            self.assertEqual(open_tasks[0]["priority"], "high")


def _write_and_approve_atom(
    vault: Path,
    *,
    filename: str,
    claim: str,
    quote: str,
    source_rel: str,
    contexts: list[str],
    memory_type: str = "semantic",
    epistemic_label: str = "claim",
    links: list[str] | None = None,
    contested_by: list[str] | None = None,
    approve: bool = True,
) -> Path:
    from review_atom import apply_review_transition, content_sha256

    atom_path = vault / "2-atoms" / filename
    atom_id = filename[:-3] if filename.endswith(".md") else filename
    ctx_str = ", ".join(contexts)
    links_str = ", ".join(links or [])
    contested_str = ", ".join(contested_by or [])
    atom_path.write_text(
        f"---\n"
        f"id: {atom_id}\n"
        f"type: atom\n"
        f"memory_type: {memory_type}\n"
        f"origin: user_local\n"
        f"citation_integrity: missing\n"
        f"claim_state: candidate\n"
        f"epistemic_label: {epistemic_label}\n"
        f"review_status: pending\n"
        f"certainty: tentative\n"
        f"created: 2026-11-18\n"
        f"last-reviewed: 2026-11-18\n"
        f"importance: 0.85\n"
        f"confidence: 0.90\n"
        f"sources:\n  - {source_rel}\n"
        f"links: [{links_str}]\n"
        f"tags: [test]\n"
        f"contexts: [{ctx_str}]\n"
        f"supersedes: []\n"
        f"superseded_by: []\n"
        f"contested_by: [{contested_str}]\n"
        f"---\n\n"
        f"## Claim\n{claim}\n\n"
        f"## Evidence\n\"{quote}\"\n\n"
        f"## Why It Matters\nGoal 2 verification fixture.\n",
        encoding="utf-8",
    )
    if approve:
        apply_review_transition(
            atom_path=str(atom_path),
            action="approve",
            expected_sha256=content_sha256(atom_path.read_bytes()),
            actor="owner",
            brain_dir=str(vault),
            atoms_dir=str(vault / "2-atoms"),
        )
    return atom_path


class ResearchViewAndMultiHostStreamingTests(unittest.TestCase):
    def test_research_mode_abstention_lead_isolation_and_api_parity(self) -> None:
        from answer_policy import AnswerRequest, NO_APPROVED_MEMORY_RESPONSE
        import api_product
        from host_adapters import generate_grounded_answer_via_host
        import httpx
        from lexical_store import rebuild_lexical_index
        import memory_api
        from recall import RecallOptions, search_memory
        from streaming_chat import ChatPaths, chat_events

        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            vault = _init_temp_vault(t)
            (vault / "0-raw" / "arch.md").write_text(
                "Mercury broker pins mutual TLS certificates on every edge node.\n"
                "Unverified mercury draft claims canary rollout uses 12 percent traffic.\n",
                encoding="utf-8",
            )
            _write_and_approve_atom(
                vault,
                filename="mercury-mtls.md",
                claim="Mercury broker pins mutual TLS certificates on every edge node.",
                quote="Mercury broker pins mutual TLS certificates on every edge node.",
                source_rel="0-raw/arch.md",
                contexts=["project:mercury"],
                approve=True,
            )
            _write_and_approve_atom(
                vault,
                filename="mercury-canary-pending.md",
                claim="Mercury canary rollout uses 12 percent traffic.",
                quote="Unverified mercury draft claims canary rollout uses 12 percent traffic.",
                source_rel="0-raw/arch.md",
                contexts=["project:mercury"],
                approve=False,
            )
            (vault / "0-raw" / "inbox" / "2026-11-18-mercury-inbox.md").write_text(
                "<!-- second_brain_origin: mcp_local -->\n\n"
                "Mercury unverified inbox lead about region failover (project:mercury).\n",
                encoding="utf-8",
            )
            (vault / "0-raw" / "inbox" / "2026-11-18-other-inbox.md").write_text(
                "<!-- second_brain_origin: mcp_local -->\n\n"
                "Other project mercury failover note (project:other).\n",
                encoding="utf-8",
            )
            lex_path = str(vault / ".sidecar" / "lexical.sqlite3")
            rebuild_lexical_index(str(vault / "2-atoms"), lex_path)

            captured_prompts: list[str] = []

            def _mock_ollama_handler(request: httpx.Request) -> httpx.Response:
                payload = json.loads(request.content.decode("utf-8"))
                msg = payload["messages"][0]["content"]
                captured_prompts.append(msg)
                if "claim_ids" in json.dumps(payload.get("format", {})):
                    content = json.dumps({"claim_ids": [0]})
                else:
                    content = json.dumps(
                        {"claims": ["Mercury broker pins mutual TLS certificates on every edge node."]}
                    )
                return httpx.Response(200, json={"message": {"content": content}})

            transport = httpx.MockTransport(_mock_ollama_handler)

            # 1) Query with 0 approved matches + pending/inbox leads -> abstains (answerable=False), appends [UNVERIFIED LEADS]
            ans_abstain = generate_grounded_answer_via_host(
                AnswerRequest(query="mercury canary rollout region failover", mode="research", project_scope="project:mercury"),
                [],
                host_id="ollama_local",
                atoms_dir=str(vault / "2-atoms"),
                brain_dir=str(vault),
                http_transport=transport,
                skip_dns_check_for_mock_transport=True,
            )
            self.assertFalse(ans_abstain.answerable)
            self.assertEqual(ans_abstain.claims, ())
            self.assertIn(NO_APPROVED_MEMORY_RESPONSE, ans_abstain.text)
            self.assertIn("[UNVERIFIED LEADS]", ans_abstain.text)
            self.assertGreaterEqual(len(ans_abstain.unverified_leads), 1)
            self.assertEqual(len(captured_prompts), 0)  # never calls model when 0 approved atoms!

            # 2) Query with approved claim + pending lead -> grounded claim first, [UNVERIFIED LEADS] appended structurally,
            #    and unverified lead text NEVER enters the model prompt!
            direct, neighbors = search_memory(
                "mercury mutual TLS canary",
                RecallOptions(n_results=5, projects=("project:mercury",), retrieval_mode="lexical"),
                atoms_dir=str(vault / "2-atoms"),
                index_path=lex_path,
                chroma_path=str(vault / ".chroma"),
            )
            ans_hit = generate_grounded_answer_via_host(
                AnswerRequest(query="mercury mutual TLS canary", mode="research", project_scope="project:mercury"),
                list(direct) + list(neighbors),
                host_id="ollama_local",
                atoms_dir=str(vault / "2-atoms"),
                brain_dir=str(vault),
                http_transport=transport,
                skip_dns_check_for_mock_transport=True,
            )
            self.assertTrue(ans_hit.answerable)
            self.assertEqual(len(ans_hit.claims), 1)
            self.assertIn("[UNVERIFIED LEADS]", ans_hit.text)
            self.assertEqual(len(captured_prompts), 1)
            self.assertNotIn("12 percent traffic", captured_prompts[0])
            self.assertNotIn("region failover", captured_prompts[0])

            # 3) streaming_chat mode="research" + telemetry candidate_atom_ids check
            cp = ChatPaths.for_brain(
                vault,
                http_transport=transport,
                skip_dns_check_for_mock_transport=True,
            )
            events = list(
                chat_events(
                    "mercury mutual TLS canary",
                    mode="research",
                    paths=cp,
                    project_scope="project:mercury",
                )
            )
            ver_ev = next(e for e in events if e["type"] == "verified_answer")
            self.assertTrue(ver_ev["answerable"])
            self.assertIn("[UNVERIFIED LEADS]", str(ver_ev["text"]))
            self.assertTrue(any(e["type"] == "research_leads" for e in events))

            # Pending atom should have candidate_hits > 0 via record_retrieval_telemetry(candidate_atom_ids=...)
            with closing(sqlite3.connect(cp.telemetry_db_path)) as conn:
                row = conn.execute(
                    "SELECT candidate_hits FROM atom_telemetry WHERE atom_id = ?",
                    ("mercury-canary-pending.md",),
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertGreaterEqual(int(row[0]), 1)

            # 4) Live loopback API /v1/answer & /v1/chat/stream & /v1/notes/tasks & /v1/cognitive/experiences
            api_tok_path = t / "api-token"
            owner_tok_path = t / "owner-token"
            api_tok, _ = memory_api.generate_memory_api_token(api_tok_path)
            owner_tok = api_product.ensure_owner_token(owner_tok_path)
            rt = memory_api.MemoryApiRuntimeConfig(
                brain_dir=str(vault),
                atoms_dir=str(vault / "2-atoms"),
                inbox_dir=str(vault / "0-raw" / "inbox"),
                experiences_dir=str(vault / "4-experiences"),
                playbooks_dir=str(vault / "5-playbooks"),
                lexical_index_path=lex_path,
                chroma_path=str(vault / ".chroma"),
                telemetry_db_path=str(vault / ".sidecar" / "telemetry.sqlite3"),
                token_path=api_tok_path,
                owner_token_path=owner_tok_path,
                http_transport=transport,
                skip_dns_check_for_mock_transport=True,
            )
            memory_api.reset_api_rate_limiter()
            srv = memory_api.create_memory_api_server("127.0.0.1", 0, rt)
            port = srv.server_address[1]
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            try:
                owner_cli = memory_api.MemoryApiClient(port=port, token=api_tok, owner_token=owner_tok)
                agent_cli = memory_api.MemoryApiClient(port=port, token=api_tok)

                owner_res = owner_cli.answer(
                    "mercury canary region failover",
                    mode="research",
                    project_scope="project:mercury",
                )
                agent_res = agent_cli.answer(
                    "mercury canary region failover",
                    mode="research",
                    project_scope="project:mercury",
                )
                self.assertFalse(owner_res["answerable"])
                self.assertTrue(any(l["source_kind"] == "quarantined_inbox" for l in owner_res["unverified_leads"]))
                self.assertFalse(any("other-inbox" in str(l["lead_id"]) for l in owner_res["unverified_leads"]))
                # Non-owner API clients must not see quarantined raw inbox captures
                self.assertFalse(any(l["source_kind"] == "quarantined_inbox" for l in agent_res["unverified_leads"]))

                # Also test /v1/notes/tasks and 8-field /v1/cognitive/experiences via SDK
                owner_cli.create_note(
                    "Release Tasks",
                    "- [ ] Rotate mercury staging certs @due(2026-12-10) !high\n",
                    tags=["project:mercury"],
                )
                tasks_res = owner_cli.note_tasks(project="project:mercury")
                self.assertEqual(tasks_res["count"], 1)
                self.assertEqual(tasks_res["tasks"][0]["due_date"], "2026-12-10")

                exp_res = owner_cli.record_experience(
                    goal="Mercury research verification",
                    result_summary="Verified research view isolation.",
                    plan=["Run API answer", "Check leads"],
                    reasoning_summary="Leads stay out of model context.",
                    confidence_before=0.6,
                    confidence_after=0.95,
                    new_memories=["mercury-mtls.md"],
                    candidate_skills=["Audit Research View"],
                    reflection="No unverified promotion occurred.",
                )
                self.assertEqual(exp_res["confidence_after"], 0.95)
                self.assertEqual(exp_res["plan"], ["Run API answer", "Check leads"])
            finally:
                srv.shutdown()
                srv.server_close()
                thread.join(timeout=3.0)

    def test_multi_host_streaming_dlp_pii_rehydration_and_short_sentence_citation_enforcement(self) -> None:
        from host_adapters import HostSharingRule, builtin_default_policies, save_host_policies
        import httpx
        from lexical_store import rebuild_lexical_index
        from notes_store import NotePaths, create_note
        from streaming_chat import ChatPaths, ChatStreamRequest, chat_events, check_citations

        # 1) Sentence-level citation enforcement with NO len(s) >= 25 bypass!
        ver = [{"id": "V1", "claim": "TLS 1.3 is enabled."}]
        g_short_bad = check_citations("TLS is off.", ver, [])
        self.assertFalse(g_short_bad["fully_cited"])
        self.assertEqual(g_short_bad["uncited_sentences"], ["TLS is off."])

        g_short_ok = check_citations("TLS 1.3 is on [V1].", ver, [])
        self.assertTrue(g_short_ok["fully_cited"])
        self.assertEqual(g_short_ok["uncited_sentences"], [])

        g_abstain = check_citations("The provided sources do not answer this question.", ver, [])
        self.assertTrue(g_abstain["fully_cited"])

        # Validate ChatStreamRequest fields & strict mode/retrieval_mode validation
        req = ChatStreamRequest.model_validate(
            {
                "query": "How does auth work?",
                "mode": "stream",
                "retrieval_mode": "rrf",
                "host_id": "openai_online",
                "project_scope": "project:alpha",
            }
        )
        self.assertEqual(req.retrieval_mode, "rrf")
        self.assertEqual(req.host_id, "openai_online")
        with self.assertRaises(ValueError):
            ChatStreamRequest.model_validate({"query": "x", "retrieval_mode": "bogus"})

        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            vault = _init_temp_vault(t)
            (vault / "0-raw" / "alpha-src.md").write_text(
                "Alpha incident contact is alice@example.com for production outage pages.\n"
                "Secret beta pipeline token placeholder text for scoping test.\n",
                encoding="utf-8",
            )
            _write_and_approve_atom(
                vault,
                filename="alpha-contact.md",
                claim="Alpha incident contact is alice@example.com for production outage pages.",
                quote="Alpha incident contact is alice@example.com for production outage pages.",
                source_rel="0-raw/alpha-src.md",
                contexts=["project:alpha"],
                approve=True,
            )
            _write_and_approve_atom(
                vault,
                filename="beta-secret-scope.md",
                claim="Secret beta pipeline token placeholder text for scoping test.",
                quote="Secret beta pipeline token placeholder text for scoping test.",
                source_rel="0-raw/alpha-src.md",
                contexts=["project:beta"],
                approve=True,
            )
            lex_path = str(vault / ".sidecar" / "lexical.sqlite3")
            rebuild_lexical_index(str(vault / "2-atoms"), lex_path)
            create_note(
                "Beta Working Note",
                "Beta private contact is bob@beta.internal for alpha outage cross-check.",
                tags=("project:beta",),
                paths=NotePaths.for_brain(vault),
            )

            # Configure opted-in online hosts + custom local host scoped strictly to project:alpha
            policy_path = t / "host-sharing-policy.json"
            policies = builtin_default_policies()
            policies["openai_online"] = HostSharingRule(
                host_id="openai_online",
                provider="openai_compatible",
                transport="online",
                endpoint_url="https://api.openai.com/v1",
                default_model="gpt-4o-mini",
                api_key_env="TEST_OPENAI_KEY",
                enabled=True,
                allowed_projects=("project:alpha",),
                allow_unscoped_atoms=False,
                allow_preference_atoms=False,
                redact_pii=True,
                fail_on_secret_detection=True,
                owner_consent_recorded=True,
            )
            policies["anthropic_online"] = HostSharingRule(
                host_id="anthropic_online",
                provider="anthropic",
                transport="online",
                endpoint_url="https://api.anthropic.com/v1",
                default_model="claude-3-5-haiku-latest",
                api_key_env="TEST_ANTHROPIC_KEY",
                enabled=True,
                allowed_projects=("project:alpha",),
                allow_unscoped_atoms=False,
                allow_preference_atoms=False,
                redact_pii=True,
                fail_on_secret_detection=True,
                owner_consent_recorded=True,
            )
            policies["gemini_online"] = HostSharingRule(
                host_id="gemini_online",
                provider="gemini",
                transport="online",
                endpoint_url="https://generativelanguage.googleapis.com/v1beta",
                default_model="gemini-2.0-flash",
                api_key_env="TEST_GEMINI_KEY",
                enabled=True,
                allowed_projects=("project:alpha",),
                allow_unscoped_atoms=False,
                allow_preference_atoms=False,
                redact_pii=True,
                fail_on_secret_detection=True,
                owner_consent_recorded=True,
            )
            policies["owner_custom_local"] = HostSharingRule(
                host_id="owner_custom_local",
                provider="openai_compatible",
                transport="local",
                endpoint_url="http://127.0.0.1:8080/v1",
                default_model="local-qwen",
                api_key_env=None,
                enabled=True,
                allowed_projects=("project:alpha",),
                allow_unscoped_atoms=False,
                allow_preference_atoms=False,
                redact_pii=False,
                fail_on_secret_detection=True,
                owner_consent_recorded=True,
            )
            save_host_policies(policies, policy_path)

            wire_bodies: dict[str, str] = {}

            def _multi_host_mock(request: httpx.Request) -> httpx.Response:
                body_str = request.content.decode("utf-8")
                url_str = str(request.url)
                if "api.openai.com" in url_str:
                    wire_bodies["openai_online"] = body_str
                    sse = (
                        'data: {"choices":[{"delta":{"content":"Contact [REDACTED_PII:email_address:1] "}}]}\n\n'
                        'data: {"choices":[{"delta":{"content":"for outages [V1]. "}}]}\n\n'
                        'data: {"choices":[{"delta":{"content":"Escalation is fast."}}]}\n\n'
                        "data: [DONE]\n\n"
                    )
                    return httpx.Response(200, content=sse.encode("utf-8"), headers={"content-type": "text/event-stream"})
                if "api.anthropic.com" in url_str:
                    wire_bodies["anthropic_online"] = body_str
                    sse = (
                        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Contact [REDACTED_PII:email_address:1] [V1]."}}\n\n'
                        'data: {"type":"message_stop"}\n\n'
                    )
                    return httpx.Response(200, content=sse.encode("utf-8"), headers={"content-type": "text/event-stream"})
                if "generativelanguage.googleapis.com" in url_str:
                    wire_bodies["gemini_online"] = body_str
                    sse = (
                        'data: {"candidates":[{"content":{"parts":[{"text":"Contact [REDACTED_PII:email_address:1] [V1]."}]}}]}\n\n'
                    )
                    return httpx.Response(200, content=sse.encode("utf-8"), headers={"content-type": "text/event-stream"})
                wire_bodies["owner_custom_local"] = body_str
                sse = (
                    'data: {"choices":[{"delta":{"content":"Contact alice@example.com [V1]."}}]}\n\n'
                    "data: [DONE]\n\n"
                )
                return httpx.Response(200, content=sse.encode("utf-8"), headers={"content-type": "text/event-stream"})

            transport = httpx.MockTransport(_multi_host_mock)
            cp = ChatPaths.for_brain(
                vault,
                policy_path=policy_path,
                http_transport=transport,
                skip_dns_check_for_mock_transport=True,
            )

            fake_env = {
                "TEST_OPENAI_KEY": "test-dummy-openai-key",
                "TEST_ANTHROPIC_KEY": "test-dummy-anthropic-key",
                "TEST_GEMINI_KEY": "test-dummy-gemini-key",
            }
            with patch.dict(os.environ, fake_env, clear=False):
                # Test OpenAI streaming: PII redacted on wire, rehydrated locally, project:beta excluded,
                # sentence-buffered, short uncited sentence ("Escalation is fast.") caught!
                oa_events = list(chat_events("alpha incident contact outage", mode="stream", paths=cp, host_id="openai_online"))
                self.assertIn("[REDACTED_PII:email_address:1]", wire_bodies["openai_online"])
                self.assertNotIn("alice@example.com", wire_bodies["openai_online"])
                self.assertNotIn("bob@beta.internal", wire_bodies["openai_online"])
                self.assertNotIn("beta-secret-scope", wire_bodies["openai_online"])
                token_chunks = [str(e["text"]) for e in oa_events if e["type"] == "token"]
                self.assertGreaterEqual(len(token_chunks), 2)
                full_oa = "".join(token_chunks)
                self.assertIn("alice@example.com", full_oa)
                grounding_oa = next(e for e in oa_events if e["type"] == "grounding")
                self.assertFalse(grounding_oa["fully_cited"])
                self.assertEqual(grounding_oa["uncited_sentences"], ["Escalation is fast."])

                # Test Anthropic, Gemini, and owner_custom_local hosts
                for hid in ("anthropic_online", "gemini_online", "owner_custom_local"):
                    evs = list(chat_events("alpha incident contact outage", mode="stream", paths=cp, host_id=hid))
                    text_out = "".join(str(e["text"]) for e in evs if e["type"] == "token")
                    self.assertIn("alice@example.com", text_out)
                    g = next(e for e in evs if e["type"] == "grounding")
                    self.assertTrue(g["fully_cited"])
                    if hid != "owner_custom_local":
                        self.assertNotIn("alice@example.com", wire_bodies[hid])

                # Outbound secret detection fails closed & logs blocked_secret audit event!
                sec_events = list(
                    chat_events(
                        "alpha outage using token sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
                        mode="stream",
                        paths=cp,
                        host_id="openai_online",
                    )
                )
                self.assertTrue(any(e["type"] == "error" and "blocked" in str(e["error"]).lower() for e in sec_events))
                audit_lines = [
                    json.loads(line)
                    for line in (vault / ".sidecar" / "outbound-host-audit.jsonl").read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                self.assertTrue(any(rec["outcome"] == "blocked_secret" and rec["host_id"] == "openai_online" for rec in audit_lines))
                self.assertTrue(any(rec["outcome"] == "streamed_synthesis" and rec["pii_redaction_count"] >= 1 for rec in audit_lines))


class UpgradedContradictionRadarAndNliTests(unittest.TestCase):
    def test_numeric_subject_attribute_alignment_exclusive_entities_nli_and_scoped_neighbors(self) -> None:
        from host_adapters import HostSharingRule, builtin_default_policies, save_host_policies
        import knowledge_graph as kg

        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            vault = _init_temp_vault(t)
            (vault / "0-raw" / "radar.md").write_text(
                "HTTP proxy connect timeout is 5s.\n"
                "HTTP proxy keepalive is 60s.\n"
                "HTTP proxy read timeout is 60s.\n"
                "HTTP proxy connect timeout is 30s.\n"
                "Memory API binds strictly to 127.0.0.1.\n"
                "Memory API binds to 0.0.0.0.\n"
                "Vault primary metadata store uses SQLite.\n"
                "Vault primary metadata store uses PostgreSQL.\n"
                "Gateway supports both SQLite and PostgreSQL backends.\n"
                "Auth policy operates fail-closed.\n"
                "Auth policy operates fail-open.\n"
                "Production API traffic must route exclusively through Cloudflare proxy.\n"
                "Production API accepts direct origin connections bypassing Cloudflare proxy.\n"
                "Other project storage uses SQLite.\n",
                encoding="utf-8",
            )

            # 1) Non-conflicting numeric attributes (connect timeout vs keepalive vs read timeout) -> 0 alerts!
            sub_v1 = _init_temp_vault(t / "v_nonconflict")
            (sub_v1 / "0-raw" / "radar.md").write_text((vault / "0-raw" / "radar.md").read_text(encoding="utf-8"), encoding="utf-8")
            _write_and_approve_atom(
                sub_v1,
                filename="proxy-connect.md",
                claim="HTTP proxy connect timeout is 5s.",
                quote="HTTP proxy connect timeout is 5s.",
                source_rel="0-raw/radar.md",
                contexts=["project:net"],
            )
            _write_and_approve_atom(
                sub_v1,
                filename="proxy-keepalive.md",
                claim="HTTP proxy keepalive is 60s.",
                quote="HTTP proxy keepalive is 60s.",
                source_rel="0-raw/radar.md",
                contexts=["project:net"],
            )
            _write_and_approve_atom(
                sub_v1,
                filename="proxy-read.md",
                claim="HTTP proxy read timeout is 60s.",
                quote="HTTP proxy read timeout is 60s.",
                source_rel="0-raw/radar.md",
                contexts=["project:net"],
            )
            r_clean = kg.detect_contradictions(brain_dir=str(sub_v1), atoms_dir=str(sub_v1 / "2-atoms"), include_pending=False)
            self.assertEqual(r_clean["contradiction_count"], 0)

            # Add genuinely conflicting connect timeout 30s -> numeric_value_conflict fires ONLY for connect vs connect!
            _write_and_approve_atom(
                sub_v1,
                filename="proxy-connect-30.md",
                claim="HTTP proxy connect timeout is 30s.",
                quote="HTTP proxy connect timeout is 30s.",
                source_rel="0-raw/radar.md",
                contexts=["project:net"],
            )
            r_num = kg.detect_contradictions(brain_dir=str(sub_v1), atoms_dir=str(sub_v1 / "2-atoms"), include_pending=False)
            self.assertEqual(r_num["contradiction_count"], 1)
            self.assertIn("numeric_value_conflict", r_num["alerts"][0]["reasons"])

            # 2) Exclusive entity conflicts (bind address, storage engine, failure policy) + multi-support guard
            _write_and_approve_atom(
                vault,
                filename="bind-loopback.md",
                claim="Memory API binds strictly to 127.0.0.1.",
                quote="Memory API binds strictly to 127.0.0.1.",
                source_rel="0-raw/radar.md",
                contexts=["project:core"],
            )
            _write_and_approve_atom(
                vault,
                filename="bind-wildcard.md",
                claim="Memory API binds to 0.0.0.0.",
                quote="Memory API binds to 0.0.0.0.",
                source_rel="0-raw/radar.md",
                contexts=["project:core"],
            )
            _write_and_approve_atom(
                vault,
                filename="store-sqlite.md",
                claim="Vault primary metadata store uses SQLite.",
                quote="Vault primary metadata store uses SQLite.",
                source_rel="0-raw/radar.md",
                contexts=["project:core"],
            )
            _write_and_approve_atom(
                vault,
                filename="store-postgres.md",
                claim="Vault primary metadata store uses PostgreSQL.",
                quote="Vault primary metadata store uses PostgreSQL.",
                source_rel="0-raw/radar.md",
                contexts=["project:core"],
            )
            _write_and_approve_atom(
                vault,
                filename="gateway-dual.md",
                claim="Gateway supports both SQLite and PostgreSQL backends.",
                quote="Gateway supports both SQLite and PostgreSQL backends.",
                source_rel="0-raw/radar.md",
                contexts=["project:core"],
            )
            _write_and_approve_atom(
                vault,
                filename="policy-closed.md",
                claim="Auth policy operates fail-closed.",
                quote="Auth policy operates fail-closed.",
                source_rel="0-raw/radar.md",
                contexts=["project:core"],
            )
            _write_and_approve_atom(
                vault,
                filename="policy-open.md",
                claim="Auth policy operates fail-open.",
                quote="Auth policy operates fail-open.",
                source_rel="0-raw/radar.md",
                contexts=["project:core"],
            )
            _write_and_approve_atom(
                vault,
                filename="cf-only.md",
                claim="Production API traffic must route exclusively through Cloudflare proxy.",
                quote="Production API traffic must route exclusively through Cloudflare proxy.",
                source_rel="0-raw/radar.md",
                contexts=["project:core"],
                links=["other-proj-atom.md"],
            )
            _write_and_approve_atom(
                vault,
                filename="cf-bypass.md",
                claim="Production API accepts direct origin connections bypassing Cloudflare proxy.",
                quote="Production API accepts direct origin connections bypassing Cloudflare proxy.",
                source_rel="0-raw/radar.md",
                contexts=["project:core"],
            )
            _write_and_approve_atom(
                vault,
                filename="other-proj-atom.md",
                claim="Other project storage uses SQLite.",
                quote="Other project storage uses SQLite.",
                source_rel="0-raw/radar.md",
                contexts=["project:other"],
            )

            base_radar = kg.detect_contradictions(
                brain_dir=str(vault),
                atoms_dir=str(vault / "2-atoms"),
                project_scope="project:core",
                include_pending=False,
            )
            cats = {c for al in base_radar["alerts"] for c in al.get("exclusive_categories", ())}
            self.assertIn("bind_address", cats)
            self.assertIn("storage_engine", cats)
            self.assertIn("failure_policy", cats)
            # Multi-support gateway-dual.md must NOT be flagged in any conflict!
            self.assertFalse(
                any(
                    "gateway-dual.md" in (al["atom_a"]["filename"], al["atom_b"]["filename"])
                    for al in base_radar["alerts"]
                )
            )
            self.assertFalse(base_radar["nli_enabled"])

            # 3) Optional NLI adjudication promotes semantic Cloudflare contradiction and suppresses compatible heuristic false positive
            def _mock_nli(claim_a: str, claim_b: str) -> dict:
                joined = f"{claim_a} || {claim_b}".lower()
                if "cloudflare" in joined:
                    return {"verdict": "contradiction", "confidence": 0.94, "rationale": "Direct origin bypass contradicts Cloudflare-only routing."}
                if "fail-closed" in joined and "fail-open" in joined:
                    return {"verdict": "compatible", "confidence": 0.92, "rationale": "Different layers."}
                return {"verdict": "contradiction", "confidence": 0.9, "rationale": "Conflicting values."}

            nli_radar = kg.detect_contradictions(
                brain_dir=str(vault),
                atoms_dir=str(vault / "2-atoms"),
                project_scope="project:core",
                include_pending=False,
                enable_nli=True,
                nli_fn=_mock_nli,
            )
            self.assertEqual(nli_radar["nli_status"], "completed")
            self.assertGreaterEqual(nli_radar["nli_promoted_count"], 1)
            self.assertGreaterEqual(nli_radar["nli_suppressed_count"], 1)
            self.assertTrue(
                any(
                    {"cf-only.md", "cf-bypass.md"} == {al["atom_a"]["filename"], al["atom_b"]["filename"]}
                    and "nli_contradiction" in al["reasons"]
                    for al in nli_radar["alerts"]
                )
            )

            # Disabled local Ollama degrades cleanly without crashing or dropping heuristic alerts
            disabled_pol_path = t / "disabled-ollama.json"
            pols = builtin_default_policies()
            pols["ollama_local"] = replace(pols["ollama_local"], enabled=False)
            save_host_policies(pols, disabled_pol_path)
            degraded = kg.detect_contradictions(
                brain_dir=str(vault),
                atoms_dir=str(vault / "2-atoms"),
                project_scope="project:core",
                include_pending=False,
                enable_nli=True,
                policy_path=disabled_pol_path,
            )
            self.assertEqual(degraded["nli_status"], "degraded_disabled")
            self.assertEqual(degraded["contradiction_count"], base_radar["contradiction_count"])

            # 4) graph_neighbors(project_scope=...) excludes cross-project link targets
            nbrs = kg.graph_neighbors(
                "cf-only.md",
                brain_dir=str(vault),
                atoms_dir=str(vault / "2-atoms"),
                project_scope="project:core",
                include_pending=False,
            )
            self.assertFalse(any("other-proj-atom.md" in (e["from"], e["to"]) for e in nbrs["edges"]))


class ExpandedMcpAndConnectorParityTests(unittest.TestCase):
    def test_all_17_mcp_tools_scope_revocation_and_telemetry_safety(self) -> None:
        from lexical_store import rebuild_lexical_index
        from mcp import types
        import mcp_server
        from notes_store import NotePaths, create_note
        from review_atom import apply_review_transition, content_sha256
        from skill_playbooks import promote_skill_playbook
        import time_travel
        import vault_ledger

        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            vault = _init_temp_vault(t)
            (vault / "0-raw" / "mcp-src.md").write_text(
                "Apollo gateway enforces 127.0.0.1 loopback binding.\n"
                "Apollo gateway binds to 0.0.0.0 on staging.\n"
                "Apollo deployment checklist validates Merkle proofs first.\n"
                "Zeus secret key rotation runs weekly.\n"
                "Owner prefers concise bullet responses.\n",
                encoding="utf-8",
            )
            a1 = _write_and_approve_atom(
                vault,
                filename="apollo-loopback.md",
                claim="Apollo gateway enforces 127.0.0.1 loopback binding.",
                quote="Apollo gateway enforces 127.0.0.1 loopback binding.",
                source_rel="0-raw/mcp-src.md",
                contexts=["project:apollo"],
                links=["apollo-deploy.md"],
            )
            _write_and_approve_atom(
                vault,
                filename="apollo-wildcard.md",
                claim="Apollo gateway binds to 0.0.0.0 on staging.",
                quote="Apollo gateway binds to 0.0.0.0 on staging.",
                source_rel="0-raw/mcp-src.md",
                contexts=["project:apollo"],
            )
            a_proc = _write_and_approve_atom(
                vault,
                filename="apollo-deploy.md",
                claim="Apollo deployment checklist validates Merkle proofs first.",
                quote="Apollo deployment checklist validates Merkle proofs first.",
                source_rel="0-raw/mcp-src.md",
                contexts=["project:apollo"],
                memory_type="procedural",
            )
            _write_and_approve_atom(
                vault,
                filename="zeus-rotation.md",
                claim="Zeus secret key rotation runs weekly.",
                quote="Zeus secret key rotation runs weekly.",
                source_rel="0-raw/mcp-src.md",
                contexts=["project:zeus"],
            )
            _write_and_approve_atom(
                vault,
                filename="owner-pref.md",
                claim="Owner prefers concise bullet responses.",
                quote="Owner prefers concise bullet responses.",
                source_rel="0-raw/mcp-src.md",
                contexts=["project:apollo"],
                epistemic_label="preference",
            )
            rebuild_lexical_index(str(vault / "2-atoms"), str(vault / ".sidecar" / "lexical.sqlite3"))

            # Working notes in two scopes
            npaths = NotePaths.for_brain(vault)
            create_note("Apollo Runbook Note", "Apollo failover procedure uses standby replicas.", tags=("project:apollo",), paths=npaths)
            create_note("Zeus Runbook Note", "Zeus failover procedure uses separate cluster.", tags=("project:zeus",), paths=npaths)

            # Promote an active Playbook backed by apollo-deploy.md
            pb = promote_skill_playbook(
                playbook_id="apollo-release-check",
                title="Apollo Release Checklist",
                backing_atoms=[("apollo-deploy.md", content_sha256(a_proc.read_bytes()))],
                trigger="Before shipping an Apollo gateway build",
                preconditions=["Local vault ledger is current"],
                steps=["Apollo deployment checklist validates Merkle proofs first."],
                expected_outputs=["Verified ledger chain and loopback binding"],
                failure_recovery=["Halt release and inspect audit log"],
                actor="owner",
                rationale="Codified release gate",
                allow_early_owner_override=True,
                brain_dir=str(vault),
                atoms_dir=str(vault / "2-atoms"),
                playbooks_dir=str(vault / "5-playbooks"),
                telemetry_db_path=str(vault / ".sidecar" / "telemetry.sqlite3"),
            )
            self.assertEqual(pb["playbook_id"], "apollo-release-check")

            # Commit ledger checkpoint + create time-travel snapshot BEFORE demoting apollo-wildcard.md
            vault_ledger.commit_ledger_checkpoint(
                brain_dir=str(vault),
                atoms_dir=str(vault / "2-atoms"),
                playbooks_dir=str(vault / "5-playbooks"),
                actor="owner",
                note="baseline",
            )
            snap = time_travel.create_snapshot(
                label="pre-demote",
                actor="owner",
                brain_dir=str(vault),
                atoms_dir=str(vault / "2-atoms"),
                playbooks_dir=str(vault / "5-playbooks"),
            )

            token = "goal2-mcp-secret-token-1234567890"
            ctx = SimpleNamespace(meta={"authorization": f"Bearer {token}"})

            async def _call(name: str, arguments: dict) -> types.CallToolResult:
                return await mcp_server.handle_call_tool(
                    ctx,
                    types.CallToolRequestParams(
                        name=name,
                        arguments=arguments,
                        _meta={"authorization": f"Bearer {token}"},
                    ),
                )

            async def _run_all() -> None:
                listed = await mcp_server.handle_list_tools(ctx, None)
                self.assertEqual({tool.name for tool in listed.tools}, mcp_server.TOOL_NAMES)
                self.assertEqual(len(listed.tools), 35)

                # search_notes(project=...) strict scope isolation
                sn_apollo = await _call("search_notes", {"query": "failover procedure", "project": "apollo"})
                self.assertIn("Apollo Runbook Note", sn_apollo.content[0].text)
                self.assertNotIn("Zeus Runbook Note", sn_apollo.content[0].text)

                # list_playbooks & get_playbook
                lp = json.loads((await _call("list_playbooks", {"project": "apollo"})).content[0].text)
                self.assertEqual(lp["count"], 1)
                self.assertEqual(lp["playbooks"][0]["playbook_id"], "apollo-release-check")
                gp_ok = json.loads((await _call("get_playbook", {"playbook_id": "apollo-release-check", "project": "apollo"})).content[0].text)
                self.assertEqual(gp_ok["playbook_id"], "apollo-release-check")
                gp_denied = await _call("get_playbook", {"playbook_id": "apollo-release-check", "project": "zeus"})
                self.assertTrue(gp_denied.is_error)
                self.assertEqual(gp_denied.content[0].text, mcp_server.UNAVAILABLE_PLAYBOOK_MESSAGE)

                # assemble_plan
                plan = json.loads((await _call("assemble_plan", {"goal": "Validate Apollo gateway release", "project": "apollo"})).content[0].text)
                self.assertIn("steps", plan)

                # list_contradictions & graph_neighbors
                contras = json.loads((await _call("list_contradictions", {"project": "apollo"})).content[0].text)
                self.assertGreaterEqual(contras["contradiction_count"], 1)
                gn = json.loads((await _call("graph_neighbors", {"atom_id": "apollo-loopback.md", "project": "apollo"})).content[0].text)
                self.assertEqual(gn["atom"], "apollo-loopback.md")
                self.assertGreaterEqual(gn["edge_count"], 1)
                gn_pref = await _call("graph_neighbors", {"atom_id": "owner-pref.md", "project": "apollo"})
                self.assertTrue(gn_pref.is_error)
                self.assertEqual(gn_pref.content[0].text, mcp_server.UNAVAILABLE_GRAPH_ATOM_MESSAGE)

                # verify_ledger & prove_leaf (atom + playbook + non-oracle error)
                vled = json.loads((await _call("verify_ledger", {})).content[0].text)
                self.assertTrue(vled["chain_valid"])
                p_atom = json.loads((await _call("prove_leaf", {"leaf_id": "apollo-loopback.md", "project": "apollo"})).content[0].text)
                self.assertTrue(vault_ledger.verify_merkle_proof(p_atom))
                p_pb = json.loads((await _call("prove_leaf", {"leaf_id": "apollo-release-check.md", "kind": "playbook", "project": "apollo"})).content[0].text)
                self.assertTrue(vault_ledger.verify_merkle_proof(p_pb))
                p_bad = await _call("prove_leaf", {"leaf_id": "../secret.md"})
                self.assertTrue(p_bad.is_error)
                self.assertEqual(p_bad.content[0].text, mcp_server.UNAVAILABLE_LEAF_PROOF_MESSAGE)

                # list_snapshots & recall_at_snapshot with post-snapshot live revocation guard!
                snaps = json.loads((await _call("list_snapshots", {})).content[0].text)
                self.assertEqual(snaps["count"], 1)
                # Demote apollo-wildcard.md in live vault AFTER snapshot -> recall_at_snapshot must hide it!
                w_path = vault / "2-atoms" / "apollo-wildcard.md"
                apply_review_transition(
                    atom_path=str(w_path),
                    action="demote",
                    expected_sha256=content_sha256(w_path.read_bytes()),
                    actor="owner",
                    brain_dir=str(vault),
                    atoms_dir=str(vault / "2-atoms"),
                )
                rsnap = json.loads(
                    (
                        await _call(
                            "recall_at_snapshot",
                            {"query": "Apollo gateway loopback staging", "snapshot_id": snap["snapshot_id"], "project": "apollo"},
                        )
                    ).content[0].text
                )
                returned_files = {r["filename"] for r in rsnap.get("results", [])}
                self.assertIn("apollo-loopback.md", returned_files)
                self.assertNotIn("apollo-wildcard.md", returned_files)

                # record_experience (8 fields + secret block)
                exp_ok = json.loads(
                    (
                        await _call(
                            "record_experience",
                            {
                                "goal": "Verify MCP experience logging",
                                "result_summary": "Logged cleanly.",
                                "success": True,
                                "plan": ["Step A"],
                                "reasoning_summary": "Deterministic MCP call.",
                                "confidence_before": 0.5,
                                "confidence_after": 0.9,
                                "new_memories": ["apollo-loopback.md"],
                                "candidate_skills": ["MCP Experience Check"],
                                "reflection": "Zero canonical mutation.",
                            },
                        )
                    ).content[0].text
                )
                self.assertTrue((vault / "4-experiences" / exp_ok["filename"]).is_file())
                exp_secret = await _call(
                    "record_experience",
                    {
                        "goal": "Bad secret test",
                        "result_summary": "Leaked key sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
                        "success": False,
                    },
                )
                self.assertTrue(exp_secret.is_error)

                # record_feedback (approved-only, telemetry-only on both True/False, canonical atom untouched!)
                a1_before = a1.read_bytes()
                fb_res = json.loads(
                    (await _call("record_feedback", {"atom_id": "apollo-loopback.md", "helpful": False, "project": "apollo"})).content[0].text
                )
                self.assertEqual(fb_res["target"], "telemetry_sidecar")
                self.assertEqual(a1.read_bytes(), a1_before)
                # External MCP feedback must NOT create owner entries in canonical 0-raw/archive/atom-corrections.jsonl!
                self.assertFalse((vault / "0-raw" / "archive" / "atom-corrections.jsonl").exists())

            with (
                patch.object(mcp_server, "BRAIN_DIR", str(vault)),
                patch.object(mcp_server, "ATOMS_DIR", str(vault / "2-atoms")),
                patch.object(mcp_server, "RAW_DIR", str(vault / "0-raw")),
                patch.object(mcp_server, "INBOX_DIR", str(vault / "0-raw" / "inbox")),
                patch.object(mcp_server, "BRIEFINGS_DIR", str(vault / "briefings")),
                patch.object(mcp_server, "MCP_AUDIT_PATH", str(vault / "0-raw" / "archive" / "mcp-audit.jsonl")),
                patch.dict(
                    os.environ,
                    {
                        "BRAIN_ENABLE_LOCAL_MCP": "1",
                        "BRAIN_MCP_BEARER_TOKEN": token,
                        "BRAIN_VAULT_DIR": str(vault),
                    },
                    clear=False,
                ),
            ):
                asyncio.run(_run_all())

    def test_ide_connector_six_host_add_remove_symmetry_and_claude_plugin_mcp_json(self) -> None:
        import ide_connector as ic

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            tok_file = Path(tmp) / "mcp-token"

            # Pre-seed user rules & pre-existing MCP server across hosts to verify zero collateral damage
            claude_agents = fake_home / ".claude" / "AGENTS.md"
            claude_agents.parent.mkdir(parents=True, exist_ok=True)
            claude_agents.write_text("# My Personal Claude Rules\nKeep edits small.\n", encoding="utf-8")

            ws_global = fake_home / ".codeium" / "windsurf" / "memories" / "global_rules.md"
            ws_global.parent.mkdir(parents=True, exist_ok=True)
            ws_global.write_text("# My Windsurf Rules\nPrefer strict types.\n", encoding="utf-8")

            cursor_cfg = fake_home / ".cursor" / "mcp.json"
            cursor_cfg.parent.mkdir(parents=True, exist_ok=True)
            cursor_cfg.write_text(json.dumps({"mcpServers": {"other-tool": {"command": "echo"}}}), encoding="utf-8")

            with patch.dict(os.environ, {"BRAIN_MCP_TOKEN_FILE": str(tok_file)}, clear=False):
                for host_id in ("antigravity", "claude-code", "claude-desktop", "cursor", "windsurf", "vscode"):
                    res_add = ic.connect(host_id, home=fake_home)
                    self.assertEqual(res_add["status"], "connected")

                # Verify rules/skills installed on all 6 hosts
                targets = ic.host_targets(fake_home)
                self.assertTrue((targets["antigravity"].config_path.parent / "rules" / "AGENTS.md").is_file())
                self.assertTrue((targets["antigravity"].config_path.parent / "skills" / "second-brain-memory" / "SKILL.md").is_file())
                self.assertTrue((fake_home / ".claude" / "skills" / "second-brain-memory" / "SKILL.md").is_file())
                self.assertTrue((fake_home / ".claude" / "rules" / "second-brain.md").is_file())
                self.assertIn(ic.FENCE_BEGIN, claude_agents.read_text(encoding="utf-8"))
                self.assertIn("# My Personal Claude Rules", claude_agents.read_text(encoding="utf-8"))
                self.assertTrue((targets["claude-desktop"].config_path.parent / "rules" / "second-brain.md").is_file())
                self.assertTrue((targets["claude-desktop"].config_path.parent / "AGENTS.md").is_file())
                self.assertTrue((fake_home / ".cursor" / "rules" / "second-brain.mdc").is_file())
                self.assertTrue((fake_home / ".cursor" / "AGENTS.md").is_file())
                self.assertIn(ic.FENCE_BEGIN, ws_global.read_text(encoding="utf-8"))
                self.assertIn("# My Windsurf Rules", ws_global.read_text(encoding="utf-8"))
                self.assertTrue((targets["vscode"].config_path.parent / "prompts" / "second-brain.instructions.md").is_file())
                self.assertTrue((targets["vscode"].config_path.parent / "AGENTS.md").is_file())

                # Symmetrically disconnect all 6 hosts
                for host_id in ("antigravity", "claude-code", "claude-desktop", "cursor", "windsurf", "vscode"):
                    res_rm = ic.disconnect(host_id, home=fake_home)
                    self.assertEqual(res_rm["status"], "disconnected")

                # Second Brain owned files gone; user content and unrelated MCP servers preserved!
                self.assertFalse((targets["antigravity"].config_path.parent / "rules" / "AGENTS.md").exists())
                self.assertFalse((fake_home / ".claude" / "skills" / "second-brain-memory" / "SKILL.md").exists())
                self.assertFalse((fake_home / ".cursor" / "rules" / "second-brain.mdc").exists())
                self.assertFalse((targets["vscode"].config_path.parent / "prompts" / "second-brain.instructions.md").exists())
                self.assertEqual(claude_agents.read_text(encoding="utf-8").strip(), "# My Personal Claude Rules\nKeep edits small.")
                self.assertEqual(ws_global.read_text(encoding="utf-8").strip(), "# My Windsurf Rules\nPrefer strict types.")
                cursor_after = json.loads(cursor_cfg.read_text(encoding="utf-8"))
                self.assertIn("other-tool", cursor_after["mcpServers"])
                self.assertNotIn("second-brain", cursor_after["mcpServers"])

            # Verify plugins/claude-code/second-brain/.mcp.json safety & token-file resolution
            mcp_cfg_path = ROOT / "plugins" / "claude-code" / "second-brain" / ".mcp.json"
            raw_mcp = mcp_cfg_path.read_text(encoding="utf-8")
            self.assertNotIn("../../..", raw_mcp)
            parsed_mcp = json.loads(raw_mcp)
            entry = parsed_mcp["mcpServers"]["second-brain"]
            self.assertIn("ensure_local_mcp_token", entry["args"][-1])
            # Verify it resolves plugin root cleanly from CLAUDE_PLUGIN_ROOT without manual token env export
            probe_py = (
                "import os, pathlib, sys; "
                f"os.environ['CLAUDE_PLUGIN_ROOT'] = {str(mcp_cfg_path.parent)!r}; "
                f"os.environ['BRAIN_MCP_TOKEN_FILE'] = {str(tok_file)!r}; "
                "sys.path.insert(0, str(pathlib.Path(os.environ['CLAUDE_PLUGIN_ROOT']).parent.parent.parent / 'scripts')); "
                "import mcp_server as m; "
                "tok = m.ensure_local_mcp_token(); "
                "assert len(tok) >= 16 and pathlib.Path(os.environ['BRAIN_MCP_TOKEN_FILE']).is_file(); "
                "print('OK')"
            )
            proc = subprocess.run([sys.executable, "-c", probe_py], capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("OK", proc.stdout)


class SotaBenchmarkInvariantTests(unittest.TestCase):
    def test_sota_benchmark_no_tautology_pdf_multihop_and_chroma_mem0_baselines(self) -> None:
        import sota_benchmark as sb

        src_text = (SCRIPTS_DIR / "sota_benchmark.py").read_text(encoding="utf-8")
        self.assertNotIn("or True", src_text)

        report = sb.run_sota_benchmark(atoms=100)
        self.assertGreaterEqual(report["pdf_papers_ingested"], 1)
        self.assertGreaterEqual(report["multihop_pairs_seeded"], 1)
        systems = report["systems"]
        for expected_sys in (
            "second_brain_verified",
            "second_brain_hybrid_rrf",
            "naive_bm25_rag_no_trust_gate",
            "tfidf_cosine_rag",
            "chroma_hnsw_dense_rag_no_trust_gate",
            "hybrid_bm25_chroma_rrf_no_trust_gate",
            "mem0_style_flat_overwrite_memory",
            "unscoped_flat_memory",
            "ungrounded_synthesis",
        ):
            self.assertIn(expected_sys, systems)
            self.assertIn("retrieval_latency_p50_ms", systems[expected_sys])
            self.assertIn("validation_latency_p50_ms", systems[expected_sys])

        for sb_name in ("second_brain_verified", "second_brain_hybrid_rrf"):
            m = systems[sb_name]
            self.assertEqual(m["trap_leakage_pct"], 0.0)
            self.assertEqual(m["cross_project_leak_pct"], 0.0)
            self.assertEqual(m["abstention_accuracy_pct"], 100.0)
            self.assertEqual(m["citation_grounding_pct"], 100.0)
            self.assertGreaterEqual(m["pdf_paper_recall_pct"], 80.0)
            self.assertGreaterEqual(m["multihop_recall_pct"], 80.0)


if __name__ == "__main__":
    unittest.main()
