# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#     "ollama",
#     "httpx",
#     "chromadb",
# ]
# ///

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
    _sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__")
    raise SystemExit(0)

"""
Lightweight project smoke checks.

This avoids importing runtime-heavy modules so it can run before Ollama,
ChromaDB, Streamlit, or Whisper dependencies are installed.
"""



import pathlib
import json
import tempfile
import hashlib
from contextlib import redirect_stdout
from io import StringIO

from scripts.config import (
    ARCHIVE_MANIFEST,
    ATOMS_DIR,
    BRAIN_DIR,
    build_archive_manifest_entry,
    append_archive_manifest_entry,
    normalize_markdown_filename,
    safe_file_in_dir,
    safe_vault_reference,
    vault_relative_path,
)
from scripts.memory_format import extract_memory_documents, normalize_memory_document
from scripts.memory_review import list_review_queue, parse_atom_record
from scripts.recall import RecallOptions, rerank_candidates
from scripts.answer_policy import NO_APPROVED_MEMORY_RESPONSE, UNVALIDATED_DRAFT_RESPONSE, answer_from_memory
from scripts.refinery_targets import intake_status, is_supported_refinery_target
from scripts.vector_store import JsonVectorCollection, json_collection_path, reset_vector_collection


def check(condition: bool, message: str):
    if not condition:
        raise AssertionError(message)


def test_memory_chat_refuses_unvalidated_model_prose():
    calls = []
    generate = lambda: calls.append("called") or "model answer"

    response = answer_from_memory(" \n", generate)
    check(response == NO_APPROVED_MEMORY_RESPONSE, "empty memory context gets deterministic abstention")
    check(not calls, "model generation is not called without approved context")

    response = answer_from_memory("approved atom", generate)
    check(response == UNVALIDATED_DRAFT_RESPONSE, "approved context alone does not authorize raw model text")
    check(not calls, "legacy callback is never invoked without structured evidence validation")


def test_path_guards():
    check(normalize_markdown_filename("note") == "note.md", "markdown extension added")
    check(normalize_markdown_filename("note.md") == "note.md", "markdown extension preserved")
    check(normalize_markdown_filename("") == "", "empty markdown filename stays empty")

    check(
        safe_file_in_dir(ATOMS_DIR, "safe-note.md", allowed_exts={".md"}, must_exist=False),
        "single markdown filename is allowed",
    )

    bad_filenames = [
        "",
        "../house-rules.md",
        "..\\house-rules.md",
        "nested/file.md",
        "nested\\file.md",
        "C:/Windows/win.ini",
        "C:\\Windows\\win.ini",
        "wrong.txt",
    ]
    for filename in bad_filenames:
        check(
            safe_file_in_dir(ATOMS_DIR, filename, allowed_exts={".md"}, must_exist=False) is None,
            f"unsafe filename rejected: {filename!r}",
        )

    check(
        safe_vault_reference("sources/example.md", allowed_exts={".md"}, must_exist=False),
        "relative vault markdown reference is allowed",
    )

    bad_refs = [
        "../README.md",
        "..\\README.md",
        "C:/Windows/win.ini",
        "sources/example.txt",
    ]
    for ref in bad_refs:
        check(
            safe_vault_reference(ref, allowed_exts={".md"}, must_exist=False) is None,
            f"unsafe vault reference rejected: {ref!r}",
        )

    check(BRAIN_DIR, "brain dir configured")
    check(
        vault_relative_path(ARCHIVE_MANIFEST) == "0-raw/archive/manifest.jsonl",
        "archive manifest path is brain-relative",
    )


def test_archive_manifest_entry_shape_and_append():
    original_path = pathlib.Path(BRAIN_DIR) / "0-raw" / "capture.md"
    archived_path = pathlib.Path(BRAIN_DIR) / "0-raw" / "archive" / "2026-07" / "capture.md"
    entry = build_archive_manifest_entry(
        str(original_path),
        str(archived_path),
        "capture.md",
        ["atom-one.md", "atom-two.md"],
        processed_at="2026-07-12T10:20:30",
        origin="web_remote",
        source_sha256="a" * 64,
        archived_sha256="b" * 64,
    )

    check(entry["schema_version"] == 2, "manifest schema version recorded")
    check(entry["processed_at"] == "2026-07-12T10:20:30", "manifest timestamp recorded")
    check(entry["source_filename"] == "capture.md", "manifest source filename recorded")
    check(entry["original_path"] == "0-raw/capture.md", "manifest original path is vault-relative")
    check(
        entry["archived_path"] == "0-raw/archive/2026-07/capture.md",
        "manifest archive path is vault-relative",
    )
    check(entry["generated_atoms"] == ["atom-one.md", "atom-two.md"], "manifest generated atoms recorded")
    check(entry["origin"] == "web_remote", "manifest records sticky source origin")
    check(entry["source_sha256"] == "a" * 64, "manifest records source bytes before archival")
    check(entry["archived_sha256"] == "b" * 64, "manifest records archived evidence bytes")

    with tempfile.TemporaryDirectory() as temp_dir:
        manifest_path = pathlib.Path(temp_dir) / "manifest.jsonl"
        append_archive_manifest_entry(entry, manifest_path=str(manifest_path))
        payload = json.loads(manifest_path.read_text(encoding="utf-8").strip())
        check(payload == entry, "manifest entry appended as JSONL")

        second_entry = dict(entry)
        second_entry["source_filename"] = "second.md"
        append_archive_manifest_entry(second_entry, manifest_path=str(manifest_path))
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
        check(len(lines) == 2, "manifest append preserves previous entries")


def test_json_vector_collection_reset_removes_stale_items():
    with tempfile.TemporaryDirectory() as temp_dir:
        collection = JsonVectorCollection(json_collection_path(temp_dir, "atoms"), "atoms")
        collection.upsert(
            documents=["stale document"],
            embeddings=[[1.0, 0.0]],
            metadatas=[{
                "filename": "stale.md",
                "embedding_model": "test-model",
                "embedding_dimensions": 2,
            }],
            ids=["stale.md"],
        )

        reset = reset_vector_collection(temp_dir, name="atoms", prefer_json=True)
        check(isinstance(reset, JsonVectorCollection), "JSON reset returns JSON collection")
        results = reset.query(query_embeddings=[[1.0, 0.0]], n_results=5)
        check(results["ids"] == [[]], "JSON reset removes stale vector entries")

        reset.upsert(
            documents=["fresh document"],
            embeddings=[[1.0, 0.0]],
            metadatas=[{
                "filename": "fresh.md",
                "embedding_model": "test-model",
                "embedding_dimensions": 2,
            }],
            ids=["fresh.md"],
        )
        results = reset.query(query_embeddings=[[1.0, 0.0]], n_results=5)
        check(results["ids"] == [["fresh.md"]], "JSON reset collection can be reused")


def test_json_vector_collection_rejects_unproven_or_incompatible_embeddings():
    with tempfile.TemporaryDirectory() as temp_dir:
        collection = JsonVectorCollection(json_collection_path(temp_dir, "atoms"), "atoms")
        collection.upsert(
            documents=["current document", "foreign model document", "unproven document", "wrong dimensions document"],
            embeddings=[[1.0, 0.0], [1.0, 0.0], [1.0, 0.0], [1.0, 0.0, 0.0]],
            metadatas=[
                {
                    "filename": "current.md",
                    "embedding_model": "nomic-embed-text:latest",
                    "embedding_dimensions": 2,
                },
                {
                    "filename": "foreign.md",
                    "embedding_model": "other-model:latest",
                    "embedding_dimensions": 2,
                },
                {"filename": "unproven.md"},
                {
                    "filename": "wrong-dimensions.md",
                    "embedding_model": "nomic-embed-text:latest",
                    "embedding_dimensions": 3,
                },
            ],
            ids=["current.md", "foreign.md", "unproven.md", "wrong-dimensions.md"],
        )

        results = collection.query(
            query_embeddings=[[1.0, 0.0]],
            n_results=5,
            where={"embedding_model": "nomic-embed-text:latest"},
        )

        check(
            results["ids"] == [["current.md"]],
            "recall excludes vectors from another model, missing provenance, or another dimension",
        )

        malformed_filter_results = collection.query(
            query_embeddings=[[1.0, 0.0]],
            n_results=5,
            where={"$and": [{"embedding_model": "nomic-embed-text:latest"}, "not-a-filter"]},
        )
        check(
            malformed_filter_results["ids"] == [[]],
            "malformed metadata filters fail closed instead of broadening recall",
        )


def test_dashboard_launcher_uses_the_supported_python_version():
    root = pathlib.Path(__file__).resolve().parents[1]
    launcher = (root / "run_dashboard.ps1").read_text(encoding="utf-8")
    check("uv sync --locked" in launcher, "dashboard launcher syncs the locked project environment")
    check("--extra dashboard" not in launcher, "dashboard launcher does not pass removed dashboard extra")
    check(
        "uv run --locked python scripts/dashboard_auth.py ensure-configured" in launcher,
        "dashboard launcher enforces password configuration gate",
    )
    check("uv run --locked streamlit run" in launcher, "dashboard launch uses the locked project environment")
    check("--server.address 127.0.0.1" in launcher, "dashboard launcher binds only to loopback")
    check("--browser.serverAddress 127.0.0.1" in launcher, "dashboard launcher binds browser address to loopback")
    check("--browser.gatherUsageStats false" in launcher, "dashboard launcher disables Streamlit telemetry")
    for script_name in ("run_daemon.ps1", "run_night_shift.ps1"):
        script_text = (root / script_name).read_text(encoding="utf-8")
        check("--extra " not in script_text, f"{script_name} does not pass removed uv extras")


def test_dashboard_has_noninteractive_local_streamlit_configuration():
    root = pathlib.Path(__file__).resolve().parents[1]
    config = root / ".streamlit" / "config.toml"
    check(config.exists(), "dashboard has a project-local Streamlit configuration")
    content = config.read_text(encoding="utf-8")
    check("[server]" in content and "headless = true" in content, "dashboard startup never waits for the Streamlit email prompt")
    check('address = "127.0.0.1"' in content, "Streamlit configuration binds the dashboard to loopback")
    check("[browser]" in content and "gatherUsageStats = false" in content, "dashboard disables Streamlit usage telemetry by default")
    app = (root / "dashboard" / "app.py").read_text(encoding="utf-8")
    check("apply_atom_edit(" in app and "apply_review_transition(" in app, "dashboard atom edits and approvals use canonical mutators")
    check("open(file_path, \"w\"" not in app, "dashboard cannot overwrite raw evidence or source files")


def test_embedding_failure_is_explicit_and_never_returns_a_fabricated_vector():
    import scripts.ollama_utils as ollama_utils
    from scripts.ollama_utils import EmbeddingUnavailableError, embed_text

    original_embed = ollama_utils.ollama.embed

    def unavailable_embed(**kwargs):
        raise RuntimeError("embedding daemon unavailable")

    ollama_utils.ollama.embed = unavailable_embed
    try:
        try:
            embed_text("do not fabricate this vector", model="nomic-embed-text:latest")
        except EmbeddingUnavailableError as exc:
            check("nomic-embed-text:latest" in str(exc), "embedding failure reports the affected model")
        else:
            raise AssertionError("embedding failure must not return a hash or placeholder vector")
    finally:
        ollama_utils.ollama.embed = original_embed


def test_rebuild_refuses_to_reset_an_index_before_embedding_preflight_succeeds():
    import scripts.rebuild_vectors as rebuild_vectors
    from scripts.ollama_utils import EmbeddingUnavailableError

    original_embed_text = rebuild_vectors.embed_text
    original_reset = rebuild_vectors.reset_vector_collection
    reset_called = False

    def unavailable_embed(*args, **kwargs):
        raise EmbeddingUnavailableError("embedding service unavailable")

    def reset_must_not_run(*args, **kwargs):
        nonlocal reset_called
        reset_called = True
        raise AssertionError("index reset ran before the embedding capability was verified")

    rebuild_vectors.embed_text = unavailable_embed
    rebuild_vectors.reset_vector_collection = reset_must_not_run
    try:
        try:
            rebuild_vectors.rebuild_vectors(store_path=tempfile.mkdtemp())
        except EmbeddingUnavailableError:
            pass
        else:
            raise AssertionError("rebuild must stop when the embedding capability is unavailable")
        check(not reset_called, "rebuild leaves the existing index intact when preflight fails")
    finally:
        rebuild_vectors.embed_text = original_embed_text
        rebuild_vectors.reset_vector_collection = original_reset


def test_rebuild_preserves_the_active_index_when_a_staged_embedding_fails():
    import scripts.rebuild_vectors as rebuild_vectors

    original_embed_text = rebuild_vectors.embed_text

    def staged_embed(text, **kwargs):
        if "preflight" in text:
            return [1.0, 0.0]
        if "second atom" in text:
            raise RuntimeError("simulated embedding failure")
        return [1.0, 0.0]

    with tempfile.TemporaryDirectory() as temp_dir:
        atoms_dir = pathlib.Path(temp_dir) / "atoms"
        store_dir = pathlib.Path(temp_dir) / "store"
        atoms_dir.mkdir()
        atoms_dir.joinpath("first.md").write_text("first atom", encoding="utf-8")
        atoms_dir.joinpath("second.md").write_text("second atom", encoding="utf-8")

        active = JsonVectorCollection(json_collection_path(str(store_dir), "atoms"), "atoms")
        active.upsert(
            documents=["known-good active index"],
            embeddings=[[1.0, 0.0]],
            metadatas=[{
                "filename": "old.md",
                "embedding_model": "test-model",
                "embedding_dimensions": 2,
            }],
            ids=["old.md"],
        )

        rebuild_vectors.embed_text = staged_embed
        try:
            try:
                with redirect_stdout(StringIO()):
                    rebuild_vectors.rebuild_vectors(
                        atoms_dir=str(atoms_dir),
                        store_path=str(store_dir),
                        model="test-model",
                        require_valid_memory=False,
                        prefer_json=True,
                    )
            except RuntimeError as exc:
                check("simulated embedding failure" in str(exc), "rebuild reports the staged failure")
            else:
                raise AssertionError("a partial staged rebuild must not be committed")
        finally:
            rebuild_vectors.embed_text = original_embed_text

        preserved = JsonVectorCollection(json_collection_path(str(store_dir), "atoms"), "atoms").query(
            query_embeddings=[[1.0, 0.0]],
            n_results=5,
            where={"embedding_model": "test-model"},
        )
        check(preserved["ids"] == [["old.md"]], "failed rebuild leaves the active index unchanged")


def test_rebuild_activates_only_a_complete_generation_and_keeps_a_rollback_backup():
    import scripts.rebuild_vectors as rebuild_vectors
    from scripts.index_manifest import index_manifest_is_current, load_index_manifest

    original_embed_text = rebuild_vectors.embed_text

    with tempfile.TemporaryDirectory() as temp_dir:
        atoms_dir = pathlib.Path(temp_dir) / "atoms"
        store_dir = pathlib.Path(temp_dir) / "store"
        atoms_dir.mkdir()
        atoms_dir.joinpath("replacement.md").write_text("replacement atom", encoding="utf-8")

        active = JsonVectorCollection(json_collection_path(str(store_dir), "atoms"), "atoms")
        active.upsert(
            documents=["previous generation"],
            embeddings=[[1.0, 0.0]],
            metadatas=[{
                "filename": "old.md",
                "embedding_model": "test-model",
                "embedding_dimensions": 2,
            }],
            ids=["old.md"],
        )

        rebuild_vectors.embed_text = lambda *args, **kwargs: [1.0, 0.0]
        try:
            with redirect_stdout(StringIO()):
                stats = rebuild_vectors.rebuild_vectors(
                    atoms_dir=str(atoms_dir),
                    store_path=str(store_dir),
                    model="test-model",
                    require_valid_memory=False,
                    prefer_json=True,
                )
        finally:
            rebuild_vectors.embed_text = original_embed_text

        active_results = JsonVectorCollection(json_collection_path(str(store_dir), "atoms"), "atoms").query(
            query_embeddings=[[1.0, 0.0]],
            n_results=5,
            where={"embedding_model": "test-model"},
        )
        backup_results = JsonVectorCollection(
            json_collection_path(stats["backup_path"], "atoms"),
            "atoms",
        ).query(
            query_embeddings=[[1.0, 0.0]],
            n_results=5,
            where={"embedding_model": "test-model"},
        )
        check(stats["committed"], "complete rebuild reports a successful activation")
        check(active_results["ids"] == [["replacement.md"]], "complete rebuild replaces the active generation")
        check(backup_results["ids"] == [["old.md"]], "complete rebuild retains the previous generation as a backup")
        manifest = load_index_manifest(str(store_dir))
        check(manifest is not None, "complete rebuild writes an active index manifest")
        check(manifest["embedding_model"] == "test-model", "manifest records the embedding model")
        check(manifest["embedding_dimensions"] == 2, "manifest records the embedding dimensions")
        check(index_manifest_is_current(manifest, str(atoms_dir)), "manifest matches the active atom revisions")
        check(
            active_results["metadatas"][0][0]["index_generation"] == manifest["generation_id"],
            "each active vector is bound to the manifest generation",
        )


def test_index_manifest_detects_changed_atom_revisions():
    from scripts.index_manifest import build_index_manifest, index_manifest_is_current

    with tempfile.TemporaryDirectory() as temp_dir:
        atoms_dir = pathlib.Path(temp_dir) / "atoms"
        atoms_dir.mkdir()
        atom_path = atoms_dir / "note.md"
        atom_path.write_text("first revision", encoding="utf-8")

        manifest = build_index_manifest(
            atoms_dir=str(atoms_dir),
            collection_name="atoms",
            embedding_model="test-model",
            embedding_dimensions=2,
            indexed_count=1,
        )
        check(index_manifest_is_current(manifest, str(atoms_dir)), "manifest accepts the indexed atom revision")

        atom_path.write_text("human-edited revision", encoding="utf-8")
        check(
            not index_manifest_is_current(manifest, str(atoms_dir)),
            "manifest rejects dense recall after an atom revision changes",
        )


def test_recall_requires_a_current_matching_index_generation():
    from scripts.index_manifest import build_index_manifest
    from scripts.recall import MemoryRecallUnavailable, validated_index_generation

    with tempfile.TemporaryDirectory() as temp_dir:
        atoms_dir = pathlib.Path(temp_dir) / "atoms"
        atoms_dir.mkdir()
        atoms_dir.joinpath("note.md").write_text("indexed note", encoding="utf-8")
        manifest = build_index_manifest(
            atoms_dir=str(atoms_dir),
            collection_name="atoms",
            embedding_model="test-model",
            embedding_dimensions=2,
            indexed_count=1,
            generation_id="generation-one",
        )

        check(
            validated_index_generation(
                manifest,
                atoms_dir=str(atoms_dir),
                collection_name="atoms",
                embedding_model="test-model",
                embedding_dimensions=2,
            ) == "generation-one",
            "recall accepts a current matching index generation",
        )
        try:
            validated_index_generation(
                manifest,
                atoms_dir=str(atoms_dir),
                collection_name="atoms",
                embedding_model="different-model",
                embedding_dimensions=2,
            )
        except MemoryRecallUnavailable:
            pass
        else:
            raise AssertionError("recall must reject an index generated by a different embedding model")


def test_owner_review_uses_compare_and_swap_and_records_a_decision():
    from scripts.review_atom import ReviewConflictError, apply_review_transition

    atom = """---
id: reviewable-memory
type: atom
memory_type: semantic
certainty: tentative
created: 2026-07-14
last-reviewed: 2026-07-14
importance: 0.50
confidence: 0.50
sources:
  - source.md
links: []
tags: []
contexts: []
supersedes: []
superseded_by: []
contested_by: []
origin: user_local
citation_integrity: anchored
claim_state: candidate
epistemic_label: claim
review_status: pending
---

## Claim
Reviewable claim.

## Evidence
Anchored source evidence.
"""

    with tempfile.TemporaryDirectory() as temp_dir:
        brain_dir = pathlib.Path(temp_dir) / "brain"
        atoms_dir = brain_dir / "2-atoms"
        source_path = brain_dir / "0-raw" / "archive" / "2026-07" / "source.md"
        atoms_dir.mkdir(parents=True)
        source_path.parent.mkdir(parents=True)
        source_path.write_text("The source supports this with: Anchored source evidence.", encoding="utf-8")
        atom_path = atoms_dir / "reviewable.md"
        decision_log_path = brain_dir / "0-raw" / "archive" / "decision-record.jsonl"
        history_dir = atoms_dir / ".history"
        atom_path.write_text(atom, encoding="utf-8")
        expected_sha256 = hashlib.sha256(atom_path.read_bytes()).hexdigest()

        result = apply_review_transition(
            atom_path=str(atom_path),
            action="approve",
            expected_sha256=expected_sha256,
            actor="owner-test",
            decision_log_path=str(decision_log_path),
            history_dir=str(history_dir),
            brain_dir=str(brain_dir),
            atoms_dir=str(atoms_dir),
            reviewed_on="2026-07-27",
        )
        updated = atom_path.read_text(encoding="utf-8")
        decision = json.loads(decision_log_path.read_text(encoding="utf-8").strip())

        check("claim_state: human_reviewed" in updated, "approval promotes only through the review mutator")
        check("review_status: accepted" in updated, "approval records the accepted review status")
        check("last-reviewed: 2026-07-27" in updated, "approval records the review date")
        check(pathlib.Path(result["backup_path"]).exists(), "approval retains a backup of the canonical atom")
        check(decision["actor"] == "owner-test", "approval records the owner actor")
        check(decision["content_sha256_before"] == expected_sha256, "approval records the compared revision")

        try:
            apply_review_transition(
                atom_path=str(atom_path),
                action="reject",
                expected_sha256=expected_sha256,
                actor="owner-test",
                decision_log_path=str(decision_log_path),
                history_dir=str(history_dir),
                brain_dir=str(brain_dir),
                atoms_dir=str(atoms_dir),
            )
        except ReviewConflictError:
            pass
        else:
            raise AssertionError("review refuses a stale expected hash instead of overwriting a newer atom")


def test_editor_reads_only_currently_approved_atoms_and_never_overwrites_threads():
    from unittest.mock import patch
    from scripts.review_atom import apply_review_transition
    import scripts.ollama_editor as ollama_editor

    atom = """---
id: reviewed-source
type: atom
memory_type: semantic
sources:
  - source.md
origin: user_local
citation_integrity: missing
claim_state: candidate
epistemic_label: claim
review_status: pending
---

## Claim
This reviewed claim is backed by a source.

## Evidence
This source sentence is long enough to anchor the evidence.
"""
    with tempfile.TemporaryDirectory() as temp_dir:
        brain_dir = pathlib.Path(temp_dir) / "brain"
        atoms_dir = brain_dir / "2-atoms"
        raw_dir = brain_dir / "0-raw"
        threads_dir = brain_dir / "3-threads"
        atoms_dir.mkdir(parents=True)
        raw_dir.mkdir()
        threads_dir.mkdir()
        (raw_dir / "source.md").write_text(
            "This source sentence is long enough to anchor the evidence.", encoding="utf-8"
        )
        atom_path = atoms_dir / "reviewed-source.md"
        atom_path.write_text(atom, encoding="utf-8")
        decision_log = raw_dir / "archive" / "decision-record.jsonl"
        apply_review_transition(
            atom_path=str(atom_path),
            action="approve",
            expected_sha256=hashlib.sha256(atom_path.read_bytes()).hexdigest(),
            actor="owner-test",
            decision_log_path=str(decision_log),
            brain_dir=str(brain_dir),
            atoms_dir=str(atoms_dir),
        )
        (atoms_dir / "pending.md").write_text(atom.replace("reviewed-source", "pending"), encoding="utf-8")

        with patch.object(ollama_editor, "ATOMS_DIR", str(atoms_dir)), \
             patch.object(ollama_editor, "BRAIN_DIR", str(brain_dir)), \
             patch.object(ollama_editor, "RAW_DIR", str(raw_dir)), \
             patch.object(ollama_editor, "THREADS_DIR", str(threads_dir)), \
             patch.object(ollama_editor, "ensure_dirs", lambda: None):
            atoms = ollama_editor.load_atoms()
            check([item["filename"] for item in atoms] == ["reviewed-source.md"], "editor excludes pending atom revisions")

            existing = threads_dir / "safe-topic.md"
            existing.write_text("human-authored thread", encoding="utf-8")
            written = ollama_editor.write_thread("Safe Topic", "generated thread")
            check(written == "safe-topic-2.md", "generated thread gets a new name when the slug already exists")
            check(existing.read_text(encoding="utf-8") == "human-authored thread", "editor never overwrites an existing thread")


def test_stable_file_wait_rejects_empty_and_missing_files():
    from scripts.stable_file import wait_for_stable_file

    with tempfile.TemporaryDirectory() as temp_dir:
        stable = pathlib.Path(temp_dir) / "stable.md"
        stable.write_text("complete capture", encoding="utf-8")
        empty = pathlib.Path(temp_dir) / "empty.md"
        empty.write_text("", encoding="utf-8")
        check(wait_for_stable_file(str(stable), interval_seconds=0.01, stable_checks=2, timeout_seconds=0.2), "stable capture passes repeated size/mtime checks")
        check(not wait_for_stable_file(str(empty), interval_seconds=0.01, stable_checks=1, timeout_seconds=0.05), "empty capture is not processed")
        check(not wait_for_stable_file(str(pathlib.Path(temp_dir) / "missing.md"), interval_seconds=0.01, stable_checks=1, timeout_seconds=0.05), "missing capture returns immediately")


def test_web_scout_rejects_private_targets_and_quarantines_remote_intake():
    from scripts.ollama_scout import UnsafeRemoteUrlError, create_intake_pointer, validate_remote_url

    for unsafe_url in ("http://127.0.0.1", "http://localhost:11434", "file:///etc/passwd"):
        try:
            validate_remote_url(unsafe_url)
        except UnsafeRemoteUrlError:
            pass
        else:
            raise AssertionError(f"Scout must reject unsafe remote target: {unsafe_url}")

    with tempfile.TemporaryDirectory() as temp_dir:
        with redirect_stdout(StringIO()):
            pointer_path = create_intake_pointer("remote-source.md", raw_dir=temp_dir)
        pointer = pathlib.Path(pointer_path).read_text(encoding="utf-8")
        check("origin: web_remote" in pointer, "remote intake records sticky web provenance")
        check("intake_status: quarantined" in pointer, "remote intake is quarantined by default")
        check("queued_for_refinery" not in pointer, "remote intake is not automatically sent to the refinery")


def test_memory_document_extraction():
    output = """```markdown
---
id: first-memory
type: atom
memory_type: semantic
certainty: tentative
created: 2026-07-07
sources:
  - source.md
---

## Claim
First claim.

## Evidence
Evidence one.

## Why It Matters
Reason one.
```

<!-- MEMORY_BOUNDARY -->

```markdown
---
id: second-memory
type: atom
memory_type: procedural
certainty: tentative
created: 2026-07-07
sources:
  - source.md
---

## Claim
Second claim.

## Evidence
Evidence two.

## Why It Matters
Reason two.
```"""

    memories = extract_memory_documents(output, source_filename="source.md", current_date="2026-07-07")

    check(len(memories) == 2, "two fenced memory documents extracted")
    check("id: first-memory" in memories[0], "first memory preserved")
    check("memory_type: procedural" in memories[1], "second memory type preserved")


def test_memory_document_extraction_rejects_wrapped_unfenced_output():
    output = """Here are the extracted atomic memories:

### Memory 1

---
id: wrapped-memory
type: atom
memory_type: semantic
certainty: tentative
created: 2026-07-07
sources:
  - source.md
---

## Claim
Wrapped claim.

## Evidence
Wrapped evidence.

## Why It Matters
Wrapped reason.
"""

    memories = extract_memory_documents(output, source_filename="source.md", current_date="2026-07-07")
    check(len(memories) == 0, "wrapped unfenced output is rejected instead of normalized")


def test_memory_document_normalization_adds_cognitive_fields():
    raw_memory = """---
id: legacy-memory
type: atom
certainty: tentative
created: 2026-07-07
sources:
  - source.md
---

## Claim
Legacy claim.

## Evidence
Legacy evidence.

## Why It Matters
Legacy reason.
"""

    normalized = normalize_memory_document(
        raw_memory,
        source_filename="source.md",
        current_date="2026-07-07",
    )

    check("memory_type: semantic" in normalized, "default memory type added")
    check("importance: 0.50" in normalized, "default importance added")
    check("confidence: 0.50" in normalized, "default confidence added")
    check("last-reviewed: 2026-07-07" in normalized, "last-reviewed added")
    check("contexts: []" in normalized, "default context profile added")
    check("review_status: pending" in normalized, "default review status added")
    check("origin: user_local" in normalized, "default local origin added")
    check("citation_integrity: missing" in normalized, "default citation integrity added")
    check("claim_state: candidate" in normalized, "default claim lifecycle added")
    check("epistemic_label: claim" in normalized, "default epistemic label added")
    check("sources:\n  - source.md\nlinks: []" in normalized, "missing links added after source list")


def test_memory_document_normalization_repairs_invalid_review_status():
    raw_memory = """---
id: invalid-review-status
type: atom
memory_type: semantic
certainty: tentative
created: 2026-07-07
last-reviewed: 2026-07-07
importance: 0.50
confidence: 0.50
sources:
  - source.md
links: []
tags: []
contexts: []
supersedes: []
superseded_by: []
contested_by: []
review_status: trusted
---

## Claim
Invalid status claim.

## Evidence
Invalid status evidence.

## Why It Matters
Invalid status reason.
"""

    normalized = normalize_memory_document(
        raw_memory,
        source_filename="source.md",
        current_date="2026-07-07",
    )

    check("review_status: pending" in normalized, "invalid review status repaired")
    check("review_status: trusted" not in normalized, "invalid review status removed")


def test_refinery_defers_when_generation_is_unavailable_instead_of_writing_a_fallback_atom():
    import scripts.ollama_refinery as ollama_refinery

    original_chat = ollama_refinery.chat_with_fallback

    def unavailable_generation(*args, **kwargs):
        raise RuntimeError("generation model unavailable")

    ollama_refinery.chat_with_fallback = unavailable_generation
    try:
        try:
            with redirect_stdout(StringIO()):
                ollama_refinery.draft_memory_atoms("raw capture", "capture.md", "rules")
        except RuntimeError as exc:
            check("LLM drafting unavailable" in str(exc), "refinery reports unavailable generation explicitly")
        else:
            raise AssertionError("refinery must defer instead of manufacturing a fallback memory atom")
    finally:
        ollama_refinery.chat_with_fallback = original_chat


def test_refinery_writes_archive_manifest():
    root = pathlib.Path(__file__).resolve().parents[1]
    refinery = (root / "scripts" / "ollama_refinery.py").read_text(encoding="utf-8")
    check("append_archive_manifest_entry" in refinery, "refinery appends archive manifest entries")
    check("build_archive_manifest_entry" in refinery, "refinery builds archive manifest entries")
    check("generated_atoms=saved_files" in refinery, "refinery records generated atoms in manifest")


def test_rebuild_vectors_command_exists():
    root = pathlib.Path(__file__).resolve().parents[1]
    rebuild = (root / "scripts" / "rebuild_vectors.py").read_text(encoding="utf-8")
    check("def rebuild_vectors(" in rebuild, "vector rebuild function exists")
    check("reset_vector_collection(" in rebuild, "vector rebuild resets collection first")
    check("VECTOR_REBUILD_COMPLETE" in rebuild, "vector rebuild reports completion counts")


def test_memory_review_queue_parses_and_filters_atoms():
    pending_atom = """---
id: pending-memory
type: atom
memory_type: semantic
certainty: tentative
created: 2026-07-14
last-reviewed: 2026-07-14
importance: 0.60
confidence: 0.70
sources:
  - pending-source.md
links: []
tags: []
contexts: []
supersedes: []
superseded_by: []
contested_by: []
review_status: pending
---

## Claim
Pending claim for review.

## Evidence
Pending evidence.
"""
    accepted_atom = pending_atom.replace("pending-memory", "accepted-memory").replace(
        "review_status: pending",
        "review_status: accepted",
    ).replace("Pending claim", "Accepted claim")
    legacy_atom = pending_atom.replace("pending-memory", "legacy-memory").replace(
        "review_status: pending\n",
        "",
    ).replace("Pending claim", "Legacy claim")

    with tempfile.TemporaryDirectory() as temp_dir:
        atoms_dir = pathlib.Path(temp_dir)
        (atoms_dir / "pending.md").write_text(pending_atom, encoding="utf-8")
        (atoms_dir / "accepted.md").write_text(accepted_atom, encoding="utf-8")
        (atoms_dir / "legacy.md").write_text(legacy_atom, encoding="utf-8")
        (atoms_dir / "README.md").write_text("# docs", encoding="utf-8")

        record = parse_atom_record(atoms_dir / "pending.md")
        check(record["filename"] == "pending.md", "review record includes filename")
        check(record["memory_type"] == "semantic", "review record includes memory type")
        check(record["claim"] == "Pending claim for review.", "review record extracts claim")
        check(record["confidence"] == "0.70", "review record includes confidence")
        check(record["importance"] == "0.60", "review record includes importance")
        check(record["sources"] == ["pending-source.md"], "review record includes sources")
        check(record["contexts"] == [], "review record includes contexts")

        pending_records = list_review_queue(str(atoms_dir), statuses={"pending"})
        pending_filenames = [item["filename"] for item in pending_records]
        check(pending_filenames == ["legacy.md", "pending.md"], "pending queue includes legacy missing status")

        accepted_records = list_review_queue(str(atoms_dir), statuses={"accepted"})
        check([item["filename"] for item in accepted_records] == ["accepted.md"], "accepted queue can be filtered")


def test_recall_contract_filters_and_reranks_with_memory_metadata():
    accepted_atom = """---
id: accepted-memory
type: atom
memory_type: procedural
certainty: established
created: 2026-07-14
last-reviewed: 2026-07-14
importance: 0.90
confidence: 0.90
sources:
  - accepted-source.md
links: []
tags: []
contexts: [coding, offline]
supersedes: []
superseded_by: []
contested_by: []
origin: user_local
citation_integrity: anchored
claim_state: human_reviewed
epistemic_label: procedure
review_status: accepted
---

## Claim
Accepted coding procedure.

## Evidence
Accepted evidence.
"""
    pending_atom = accepted_atom.replace("accepted-memory", "pending-memory").replace(
        "review_status: accepted",
        "review_status: pending",
    ).replace("importance: 0.90", "importance: 0.50").replace("confidence: 0.90", "confidence: 0.50")
    source_anchored_atom = accepted_atom.replace("accepted-memory", "source-anchored-memory").replace(
        "claim_state: human_reviewed",
        "claim_state: source_anchored",
    )
    rejected_atom = accepted_atom.replace("accepted-memory", "rejected-memory").replace(
        "review_status: accepted",
        "review_status: rejected",
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        atoms_dir = pathlib.Path(temp_dir)
        (atoms_dir / "accepted.md").write_text(accepted_atom, encoding="utf-8")
        (atoms_dir / "pending.md").write_text(pending_atom, encoding="utf-8")
        (atoms_dir / "source-anchored.md").write_text(source_anchored_atom, encoding="utf-8")
        (atoms_dir / "rejected.md").write_text(rejected_atom, encoding="utf-8")

        def stored_revision(filename):
            return hashlib.sha256((atoms_dir / filename).read_bytes()).hexdigest()

        vector_results = {
            "documents": [[source_anchored_atom, pending_atom, accepted_atom, rejected_atom]],
            "metadatas": [[
                {
                    "filename": "source-anchored.md",
                    "content_sha256": stored_revision("source-anchored.md"),
                },
                {
                    "filename": "pending.md",
                    "content_sha256": stored_revision("pending.md"),
                },
                {
                    "filename": "accepted.md",
                    "content_sha256": stored_revision("accepted.md"),
                },
                {
                    "filename": "rejected.md",
                    "content_sha256": stored_revision("rejected.md"),
                },
            ]],
            "distances": [[0.10, 0.20, 0.35, 0.01]],
        }

        from unittest.mock import patch
        with patch("recall.approved_revision_is_current", return_value=True):
            ranked = rerank_candidates(
                vector_results,
                RecallOptions(n_results=3, contexts=("coding",), memory_types=("procedural",)),
                query="coding procedure",
                atoms_dir=str(atoms_dir),
            )

        check(
            [item.filename for item in ranked] == ["accepted.md"],
            "standard recall serves only human-reviewed atoms, not candidates or source-anchored material",
        )
        check(ranked[0].review_status == "accepted", "recall result carries review status")
        check(ranked[0].contexts == ("coding", "offline"), "recall result carries context profile")
        check("context:coding" in ranked[0].match_reasons, "recall result explains context match")

        stale_results = {
            "documents": [[accepted_atom]],
            "metadatas": [[{"filename": "accepted.md", "content_sha256": "stale"}]],
            "distances": [[0.10]],
        }
        check(
            rerank_candidates(
                stale_results,
                RecallOptions(n_results=1),
                query="coding procedure",
                atoms_dir=str(atoms_dir),
            ) == [],
            "recall rejects vectors whose stored source hash no longer matches the atom",
        )


def test_lexical_index_is_rebuildable_safe_and_trust_filtered():
    from scripts.lexical_store import LexicalIndexUnavailable, lexical_index_is_current, rebuild_lexical_index, search_lexical_index
    from scripts.recall import search_lexical_memory

    accepted_atom = """---
id: lexical-accepted
type: atom
memory_type: semantic
certainty: established
created: 2026-07-27
last-reviewed: 2026-07-27
importance: 0.90
confidence: 0.90
sources: []
links: []
tags: []
contexts: [offline]
supersedes: []
superseded_by: []
contested_by: []
origin: user_local
citation_integrity: anchored
claim_state: human_reviewed
epistemic_label: claim
review_status: accepted
---

## Claim
Lexical retrieval indexes structured logs without an embedding model.

## Evidence
Structured JSON logs contain request IDs for tracing and avoid logging PII.
"""
    candidate_atom = accepted_atom.replace("lexical-accepted", "lexical-candidate").replace(
        "claim_state: human_reviewed", "claim_state: candidate"
    ).replace("review_status: accepted", "review_status: pending")

    with tempfile.TemporaryDirectory() as temp_dir:
        root = pathlib.Path(temp_dir)
        atoms_dir = root / "atoms"
        index_path = root / "sidecar" / "lexical.sqlite3"
        atoms_dir.mkdir()
        (atoms_dir / "accepted.md").write_text(accepted_atom, encoding="utf-8")
        (atoms_dir / "candidate.md").write_text(candidate_atom, encoding="utf-8")

        stats = rebuild_lexical_index(str(atoms_dir), str(index_path))
        check(stats["indexed_count"] == 2, "lexical rebuild indexes canonical atoms into a derived sidecar")
        check(lexical_index_is_current(str(atoms_dir), str(index_path)), "fresh lexical index binds to current atom revisions")

        candidates = search_lexical_index('lexical" OR * ; DROP TABLE atoms_fts; --', str(index_path), n_results=5)
        check(
            {item.filename for item in candidates} == {"accepted.md", "candidate.md"},
            "lexical query syntax is neutralized and cannot alter the sidecar",
        )

        from unittest.mock import patch
        with patch("recall.approved_revision_is_current", return_value=True):
            direct, neighbors = search_lexical_memory(
                "lexical retrieval",
                RecallOptions(n_results=5, follow_links=False),
                atoms_dir=str(atoms_dir),
                index_path=str(index_path),
            )
        check([item.filename for item in direct] == ["accepted.md"], "standard lexical recall filters unreviewed atoms after retrieval")
        check(neighbors == [], "lexical recall does not invent graph neighbors when link following is disabled")
        check(direct[0].source == "lexical", "lexical recall exposes its retrieval source")

        with patch("recall.approved_revision_is_current", return_value=True):
            paraphrase, _ = search_lexical_memory(
                "How should logs be formatted so requests are traceable and PII is protected?",
                RecallOptions(n_results=5, follow_links=False),
                atoms_dir=str(atoms_dir),
                index_path=str(index_path),
            )
            check(
                [item.filename for item in paraphrase] == ["accepted.md"],
                "approved claims remain retrievable when two query terms are supported by evidence",
            )

            trap_direct, _ = search_lexical_memory(
                "What is the owner's favorite battery chemistry?",
                RecallOptions(n_results=5, follow_links=False),
                atoms_dir=str(atoms_dir),
                index_path=str(index_path),
            )
            check(
                trap_direct == [],
                "lexical recall does not inject an approved but off-topic atom for a trap query",
            )
            overlap_trap, _ = search_lexical_memory(
                "What does the owner prefer for logs?",
                RecallOptions(n_results=5, follow_links=False),
                atoms_dir=str(atoms_dir),
                index_path=str(index_path),
            )
            check(
                overlap_trap == [],
                "one shared topic word is not enough to treat an owner-preference question as answered",
            )

            dense_trap = {
                "documents": [[accepted_atom]],
                "metadatas": [[{
                    "filename": "accepted.md",
                    "content_sha256": hashlib.sha256(accepted_atom.encode("utf-8")).hexdigest(),
                }]],
                "distances": [[0.05]],
            }
            check(
                rerank_candidates(
                    dense_trap,
                    RecallOptions(n_results=1),
                    query="What is the owner's favorite battery chemistry?",
                    atoms_dir=str(atoms_dir),
                ) == [],
                "dense recall does not inject an approved but off-topic atom for a trap query",
            )
            check(
                rerank_candidates(
                    dense_trap,
                    RecallOptions(n_results=1),
                    query="What does the owner prefer for logs?",
                    atoms_dir=str(atoms_dir),
                ) == [],
                "one shared topic word is not enough to inject an atom into a personal-preference query",
            )
            dense_evidence_match = {
                "documents": [[accepted_atom]],
                "metadatas": [[{
                    "filename": "accepted.md",
                    "content_sha256": hashlib.sha256((atoms_dir / "accepted.md").read_bytes()).hexdigest(),
                }]],
                "distances": [[0.05]],
            }
            dense_evidence_results = rerank_candidates(
                    dense_evidence_match,
                    RecallOptions(n_results=1),
                    query="How should logs be formatted so requests are traceable and PII is protected?",
                    atoms_dir=str(atoms_dir),
                )
            check(
                [item.filename for item in dense_evidence_results] == ["accepted.md"],
                "dense recall retains an approved paraphrase corroborated by evidence terms",
            )

        linked_seed = accepted_atom.replace("links: []", "links:\n  - unrelated.md", 1)
        unrelated_atom = accepted_atom.replace("accepted-memory", "unrelated-memory").replace(
            "Lexical retrieval indexes structured logs without an embedding model.",
            "Private contact information is missing.",
        )
        (atoms_dir / "accepted.md").write_text(linked_seed, encoding="utf-8")
        (atoms_dir / "unrelated.md").write_text(unrelated_atom, encoding="utf-8")
        rebuild_lexical_index(str(atoms_dir), str(index_path))
        with patch("recall.approved_revision_is_current", return_value=True):
            linked_direct, linked_neighbors = search_lexical_memory(
                "lexical retrieval",
                RecallOptions(n_results=5, follow_links=True),
                atoms_dir=str(atoms_dir),
                index_path=str(index_path),
            )
        check(
            [item.filename for item in linked_direct] == ["accepted.md"],
            f"relevant lexical seed remains retrievable with link following: {[item.filename for item in linked_direct]!r}",
        )
        check(linked_neighbors == [], "graph expansion rejects an approved neighbor whose claim does not overlap the query")

        (atoms_dir / "accepted.md").write_text(accepted_atom + "\nChanged after indexing.\n", encoding="utf-8")
        check(not lexical_index_is_current(str(atoms_dir), str(index_path)), "canonical edits mark the lexical sidecar stale")
        try:
            search_lexical_index("lexical", str(index_path), n_results=5, atoms_dir=str(atoms_dir))
        except LexicalIndexUnavailable:
            pass
        else:
            raise AssertionError("stale lexical indexes must fail closed instead of serving changed atom text")


def test_rebuild_lexical_index_command_exists():
    root = pathlib.Path(__file__).resolve().parents[1]
    command = root / "scripts" / "rebuild_lexical_index.py"
    check(command.exists(), "lexical sidecar has an explicit rebuild command")
    content = command.read_text(encoding="utf-8")
    check("rebuild_lexical_index" in content, "lexical rebuild command delegates to the canonical rebuild function")


def test_refinery_target_validation():
    check(is_supported_refinery_target("capture.md"), "markdown capture is supported")
    check(is_supported_refinery_target("source.intake"), "intake pointer is supported")
    check(not is_supported_refinery_target("reading-list.md"), "reading list is not processed directly")
    check(not is_supported_refinery_target("image.png"), "unsupported extension rejected")
    check(not is_supported_refinery_target(""), "empty target rejected")
    check(intake_status("source: sources/remote.md\n") == "quarantined", "legacy intake is fail-closed")
    check(
        intake_status("source: sources/remote.md\nintake_status: approved\n") == "approved",
        "only explicit owner approval releases an intake pointer",
    )


def test_python_syntax():
    root = pathlib.Path(__file__).resolve().parents[1]
    for path in root.rglob("*.py"):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def main():
    test_memory_chat_refuses_unvalidated_model_prose()
    test_path_guards()
    test_archive_manifest_entry_shape_and_append()
    test_json_vector_collection_reset_removes_stale_items()
    test_json_vector_collection_rejects_unproven_or_incompatible_embeddings()
    test_dashboard_launcher_uses_the_supported_python_version()
    test_dashboard_has_noninteractive_local_streamlit_configuration()
    test_embedding_failure_is_explicit_and_never_returns_a_fabricated_vector()
    test_rebuild_refuses_to_reset_an_index_before_embedding_preflight_succeeds()
    test_rebuild_preserves_the_active_index_when_a_staged_embedding_fails()
    test_rebuild_activates_only_a_complete_generation_and_keeps_a_rollback_backup()
    test_index_manifest_detects_changed_atom_revisions()
    test_recall_requires_a_current_matching_index_generation()
    test_owner_review_uses_compare_and_swap_and_records_a_decision()
    test_editor_reads_only_currently_approved_atoms_and_never_overwrites_threads()
    test_stable_file_wait_rejects_empty_and_missing_files()
    test_web_scout_rejects_private_targets_and_quarantines_remote_intake()
    test_memory_document_extraction()
    test_memory_document_extraction_rejects_wrapped_unfenced_output()
    test_memory_document_normalization_adds_cognitive_fields()
    test_memory_document_normalization_repairs_invalid_review_status()
    test_refinery_defers_when_generation_is_unavailable_instead_of_writing_a_fallback_atom()
    test_refinery_writes_archive_manifest()
    test_rebuild_vectors_command_exists()
    test_memory_review_queue_parses_and_filters_atoms()
    test_recall_contract_filters_and_reranks_with_memory_metadata()
    test_lexical_index_is_rebuildable_safe_and_trust_filtered()
    test_rebuild_lexical_index_command_exists()
    test_refinery_target_validation()
    test_python_syntax()
    print("SMOKE_TEST_OK")


if __name__ == "__main__":
    main()
