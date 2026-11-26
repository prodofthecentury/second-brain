# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Speed, retrieval measurement, remote Ollama policy, and restore."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class OllamaEndpointTests(unittest.TestCase):
    def test_keep_alive_defaults_to_30m_and_can_be_overridden(self) -> None:
        from scripts.ollama_endpoint import ollama_keep_alive

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BRAIN_OLLAMA_KEEP_ALIVE", None)
            os.environ.pop("OLLAMA_KEEP_ALIVE", None)
            self.assertEqual(ollama_keep_alive(), "30m")
        with patch.dict(os.environ, {"BRAIN_OLLAMA_KEEP_ALIVE": "5m"}):
            self.assertEqual(ollama_keep_alive(), "5m")
        with patch.dict(os.environ, {"OLLAMA_KEEP_ALIVE": "45s"}):
            os.environ.pop("BRAIN_OLLAMA_KEEP_ALIVE", None)
            self.assertEqual(ollama_keep_alive(), "45s")
        with patch.dict(os.environ, {"BRAIN_OLLAMA_KEEP_ALIVE": "0"}):
            self.assertEqual(ollama_keep_alive(), "0")
        with patch.dict(os.environ, {"BRAIN_OLLAMA_KEEP_ALIVE": "forever"}):
            self.assertEqual(ollama_keep_alive(), "30m")

    def test_remote_host_allows_private_lan_and_refuses_public(self) -> None:
        from scripts.hardware_profile import loopback_ollama_base_url
        from scripts.ollama_endpoint import ollama_host_warning, resolved_ollama_base_url

        self.assertIsNone(loopback_ollama_base_url("http://192.168.1.99:11434"))
        self.assertIsNone(loopback_ollama_base_url("http://169.254.169.254:80"))
        with patch.dict(os.environ, {"BRAIN_OLLAMA_HOST": "http://192.168.1.20:11434"}, clear=False):
            base, source = resolved_ollama_base_url()
            self.assertEqual(source, "BRAIN_OLLAMA_HOST")
            self.assertEqual(base, "http://192.168.1.20:11434")
            warning = ollama_host_warning() or ""
            self.assertIn("another machine", warning)
        with patch.dict(os.environ, {"BRAIN_OLLAMA_HOST": "http://1.1.1.1:11434"}):
            base, _source = resolved_ollama_base_url()
            self.assertIsNone(base)
            self.assertIn("Refusing", ollama_host_warning() or "")
        with patch.dict(os.environ, {"BRAIN_OLLAMA_HOST": "http://169.254.169.254:80"}):
            self.assertIsNone(resolved_ollama_base_url()[0])


class EmbeddingCacheTests(unittest.TestCase):
    def test_unchanged_text_is_embedded_once(self) -> None:
        from scripts import ollama_utils

        calls = {"n": 0}

        def _embed(model=None, input=None, **_kwargs):
            calls["n"] += 1
            return {"embeddings": [[0.25, 0.5, float(calls["n"])]]}

        rule = type("Rule", (), {"redact_pii": False, "fail_on_secret_detection": False})()
        with tempfile.TemporaryDirectory() as tmp:
            cache = str(Path(tmp) / "embed.sqlite3")
            with (
                patch.dict(os.environ, {"BRAIN_EMBED_CACHE_PATH": cache}),
                patch.object(ollama_utils, "_resolve_local_ollama_rule_and_url", return_value=(rule, "http://127.0.0.1:11434")),
                patch.object(ollama_utils.ollama, "embed", side_effect=_embed),
            ):
                first = ollama_utils.embed_text("same capture", model="unit-model")
                second = ollama_utils.embed_text("same capture", model="unit-model")
        self.assertEqual(calls["n"], 1)
        self.assertEqual(first, second)
        self.assertEqual(first[:2], [0.25, 0.5])


class JobQueueTests(unittest.TestCase):
    def test_capture_job_survives_and_resumes(self) -> None:
        from scripts.job_queue import complete_job, enqueue, job_status, requeue_running, work_once

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["BRAIN_JOB_QUEUE"] = str(Path(tmp) / "jobs.sqlite3")
            try:
                job_id = enqueue(
                    "capture",
                    {"title": "Queued", "body": "background capture text", "brain_dir": tmp},
                )
                self.assertEqual(job_status()["counts"].get("queued"), 1)
                done = work_once()
                self.assertEqual(done["status"], "done")
                self.assertTrue((Path(tmp) / "notes").is_dir())
                notes = list((Path(tmp) / "notes").glob("*.md"))
                self.assertEqual(len(notes), 1)
                self.assertIn("background capture text", notes[0].read_text(encoding="utf-8"))

                again = enqueue("capture", {"title": "Second", "body": "resume me", "brain_dir": tmp})
                from scripts.job_queue import _connect

                with _connect() as conn:
                    conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (again,))
                self.assertGreaterEqual(requeue_running(), 1)
                resumed = work_once()
                self.assertEqual(resumed["id"], again)
                self.assertEqual(resumed["status"], "done")
                self.assertEqual(job_status()["counts"].get("done"), 2)
                self.assertTrue(job_id)
            finally:
                os.environ.pop("BRAIN_JOB_QUEUE", None)


class IngestQueueTests(unittest.TestCase):
    def test_background_ingest_enqueues_a_file_and_returns(self) -> None:
        from scripts.ingest_multimodal import SUPPORTED_INGEST_EXTS, main as ingest_main
        from scripts.job_queue import job_status

        ext = ".html" if ".html" in SUPPORTED_INGEST_EXTS else sorted(SUPPORTED_INGEST_EXTS)[0]
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["BRAIN_JOB_QUEUE"] = str(Path(tmp) / "jobs.sqlite3")
            folder = Path(tmp) / "incoming"
            folder.mkdir()
            target = folder / f"note{ext}"
            target.write_text("<p>queued</p>", encoding="utf-8")
            try:
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    code = ingest_main(["--background", str(folder)])
                self.assertEqual(code, 0)
                self.assertTrue(out.getvalue().strip())
                self.assertEqual(job_status()["counts"].get("queued"), 1)
            finally:
                os.environ.pop("BRAIN_JOB_QUEUE", None)


class PersonalEvalTests(unittest.TestCase):
    def test_recall_and_mrr(self) -> None:
        from scripts.lexical_store import rebuild_lexical_index
        from scripts.personal_eval import evaluate_file

        with tempfile.TemporaryDirectory() as tmp:
            atoms = Path(tmp) / "2-atoms"
            atoms.mkdir()
            (atoms / "zephyr.md").write_text(
                "---\ntitle: Zephyr\nreview_status: approved\n---\n\n## Claim\n\nThe zephyr token is unique.\n\n## Evidence\n\nThe zephyr token is unique.\n",
                encoding="utf-8",
            )
            (atoms / "other.md").write_text(
                "---\ntitle: Other\nreview_status: approved\n---\n\n## Claim\n\nNothing special here.\n\n## Evidence\n\nNothing special here.\n",
                encoding="utf-8",
            )
            index = Path(tmp) / "lexical.sqlite3"
            rebuild_lexical_index(str(atoms), str(index))
            spec = {
                "name": "unit",
                "k": 5,
                "allow_unreviewed_exploratory": True,
                "queries": [
                    {"id": "hit", "query": "zephyr token", "relevant": ["zephyr.md"]},
                    {"id": "miss", "query": "absentword", "relevant": ["zephyr.md"]},
                ],
            }
            path = Path(tmp) / "eval.json"
            path.write_text(json.dumps(spec), encoding="utf-8")
            report = evaluate_file(str(path), atoms_dir=str(atoms), index_path=str(index))
        self.assertEqual(report["queries"], 2)
        self.assertEqual(report["rows"][0]["recall_at_k"], 1.0)
        self.assertEqual(report["rows"][0]["reciprocal_rank"], 1.0)
        self.assertEqual(report["rows"][1]["recall_at_k"], 0.0)
        self.assertAlmostEqual(report["recall_at_k"], 0.5)
        self.assertAlmostEqual(report["mrr"], 0.5)


class RrfWeightTests(unittest.TestCase):
    def test_weights_change_which_candidate_wins(self) -> None:
        from scripts.recall import RecallOptions, RecallResult, search_hybrid_rrf_memory

        def result(name: str) -> RecallResult:
            return RecallResult(
                filename=name,
                content=name,
                source="test",
                score=1.0,
                similarity=1.0,
                review_status="approved",
                memory_type="fact",
                confidence=1.0,
                importance=1.0,
            )

        lexical = ([result("lexical.md")], [])
        dense = ([result("dense.md")], [])
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("scripts.recall.search_lexical_memory", return_value=lexical),
                patch("scripts.recall.search_semantic_memory", return_value=dense),
            ):
                lexical_first, _ = search_hybrid_rrf_memory(
                    "query",
                    RecallOptions(
                        n_results=2,
                        follow_links=False,
                        enable_reranker=False,
                        lexical_rrf_weight=5,
                        dense_rrf_weight=0.1,
                    ),
                    atoms_dir=tmp,
                    index_path=str(Path(tmp) / "lex.sqlite3"),
                    chroma_path=str(Path(tmp) / "chroma"),
                )
                dense_first, _ = search_hybrid_rrf_memory(
                    "query",
                    RecallOptions(
                        n_results=2,
                        follow_links=False,
                        enable_reranker=False,
                        lexical_rrf_weight=0.1,
                        dense_rrf_weight=5,
                    ),
                    atoms_dir=tmp,
                    index_path=str(Path(tmp) / "lex.sqlite3"),
                    chroma_path=str(Path(tmp) / "chroma"),
                )
        self.assertEqual(lexical_first[0].filename, "lexical.md")
        self.assertEqual(dense_first[0].filename, "dense.md")


class RestoreCommandTests(unittest.TestCase):
    def test_one_command_restore_puts_the_file_back(self) -> None:
        from scripts.cli import main as cli_main
        from scripts.vault_backup import auto_backup_now, create_vault_backup

        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            (vault / "notes").mkdir(parents=True)
            (vault / "0-raw").mkdir()
            note = vault / "notes" / "kept.md"
            note.write_text("keep this sentence\n", encoding="utf-8")
            archive = Path(tmp) / "vault.sbvault"
            created = create_vault_backup(archive, brain_dir=vault, unencrypted=True)
            self.assertGreaterEqual(int(created["file_count"]), 1)
            note.unlink()
            self.assertFalse(note.exists())
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli_main(
                    [
                        "restore",
                        str(archive),
                        "--target",
                        str(vault),
                        "--unencrypted",
                        "--force",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(note.read_text(encoding="utf-8"), "keep this sentence\n")

            os.environ["BRAIN_AUTO_BACKUP_DIR"] = str(Path(tmp) / "auto")
            os.environ.pop("BRAIN_BACKUP_PASSPHRASE", None)
            try:
                first = auto_backup_now(brain_dir=vault)
                second = auto_backup_now(brain_dir=vault)
            finally:
                os.environ.pop("BRAIN_AUTO_BACKUP_DIR", None)
            self.assertFalse(first.get("skipped"))
            self.assertTrue(second.get("skipped"))
            self.assertTrue(Path(first["backup_path"]).is_file())


class SplitLayoutTests(unittest.TestCase):
    def test_dispatcher_graph_and_chat_sender_moved_without_dropping_them(self) -> None:
        import scripts.api_dispatch as api_dispatch
        import scripts.api_product as api_product

        self.assertIs(api_product.dispatch.__code__, api_dispatch.dispatch.__code__)
        self.assertTrue(callable(api_product.stream_sse))
        graph = (ROOT / "dashboard" / "graph_view.py").read_text(encoding="utf-8")
        app = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
        self.assertIn("def build_network_graph", graph)
        self.assertIn("def draw_network_plotly", graph)
        self.assertNotIn("def build_network_graph", app)
        self.assertIn("bind_graph_view", app)
        html = (ROOT / "desktop" / "renderer" / "index.html").read_text(encoding="utf-8")
        self.assertLess(html.index('src="app.js"'), html.index('src="chat_send.js"'))
        sender = (ROOT / "desktop" / "renderer" / "chat_send.js").read_text(encoding="utf-8")
        app_js = (ROOT / "desktop" / "renderer" / "app.js").read_text(encoding="utf-8")
        self.assertIn("async function sendChat()", sender)
        self.assertNotIn("async function sendChat()", app_js)
        self.assertIn("window.secondBrain.streamChat", sender)


if __name__ == "__main__":
    unittest.main()
