# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""P2–P5 checks: dense fallback, miss inbox, clause planner, and the Ollama skip."""

from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from recall import RecallResult


def _card(filename: str, claim: str, *, similarity: float = 1.0, status: str = "accepted") -> RecallResult:
    return RecallResult(
        filename=filename,
        content=f"---\nid: {filename}\n---\n\n## Claim\n{claim}\n\n## Evidence\n{claim}\n",
        source="vector",
        score=similarity,
        similarity=similarity,
        review_status=status,
        memory_type="semantic",
        confidence=0.9,
        importance=0.5,
    )


class UpgradeRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.brain = self.root / "brain"
        for relative in ("0-raw/archive", "0-raw/inbox", "2-atoms", "notes", ".sidecar", "4-experiences"):
            (self.brain / relative).mkdir(parents=True)
        self.index = self.brain / ".sidecar" / "lexical.sqlite3"
        self.index.write_bytes(b"")
        self.chroma = self.brain / ".chroma"
        self.chroma.mkdir()

    def test_auto_uses_dense_only_after_lexical_miss_and_keeps_the_gate(self) -> None:
        import recall

        synonym = _card("synonym.md", "Nightly consignments need a verified reversal method.", similarity=0.95)
        weak = _card("weak.md", "Nightly consignments need a verified reversal method.", similarity=0.80)
        rejected = _card("trap.md", "Nightly consignments need a verified reversal method.", similarity=0.99, status="rejected")
        lexical_hit = _card("literal.md", "Production deploys require a tested rollback plan.", similarity=1.0)
        calls = {"dense": 0}

        def lexical(_query, _options, **_kwargs):
            return [], []

        def dense(query, _options, **_kwargs):
            calls["dense"] += 1
            if "under-gate" in query:
                return [weak], []
            if "trap" in query:
                return [rejected], []
            return [synonym], []

        with (
            patch.object(recall, "search_lexical_memory", side_effect=lexical),
            patch.object(recall, "search_semantic_memory", side_effect=dense),
        ):
            from recall import RecallOptions, consume_last_retrieval_fallback, search_memory

            opts = RecallOptions(retrieval_mode="auto")
            direct, _neighbors = search_memory(
                "shipments undo strategy",
                opts,
                atoms_dir=str(self.brain / "2-atoms"),
                index_path=str(self.index),
                chroma_path=str(self.chroma),
            )
            self.assertEqual(direct, [])
            self.assertEqual(consume_last_retrieval_fallback(), "dense")

            anchored = _card(
                "anchored.md",
                "Nightly consignments need a verified reversal method.",
                similarity=0.88,
            )

            def dense_anchored(query, _options, **_kwargs):
                calls["dense"] += 1
                if "anchored" in query:
                    return [anchored], []
                return [], []

            with patch.object(recall, "search_semantic_memory", side_effect=dense_anchored):
                kept, _kept_neighbors = search_memory(
                    "anchored consignments reversal",
                    opts,
                    atoms_dir=str(self.brain / "2-atoms"),
                    index_path=str(self.index),
                    chroma_path=str(self.chroma),
                )
            self.assertEqual([item.filename for item in kept], ["anchored.md"])

            missed, _ = search_memory(
                "under-gate shipments",
                opts,
                atoms_dir=str(self.brain / "2-atoms"),
                index_path=str(self.index),
                chroma_path=str(self.chroma),
            )
            self.assertEqual(missed, [])

            trapped, _ = search_memory(
                "trap shipments",
                opts,
                atoms_dir=str(self.brain / "2-atoms"),
                index_path=str(self.index),
                chroma_path=str(self.chroma),
            )
            self.assertEqual(trapped, [])

            def lexical_found(_query, _options, **_kwargs):
                return [lexical_hit], []

            with patch.object(recall, "search_lexical_memory", side_effect=lexical_found):
                before = calls["dense"]
                found, _ = search_memory(
                    "production deploys rollback",
                    opts,
                    atoms_dir=str(self.brain / "2-atoms"),
                    index_path=str(self.index),
                    chroma_path=str(self.chroma),
                )
                self.assertEqual([item.filename for item in found], ["literal.md"])
                self.assertEqual(calls["dense"], before)
                self.assertIsNone(consume_last_retrieval_fallback())

            lexical_opts = RecallOptions(retrieval_mode="lexical")
            before = calls["dense"]
            search_memory(
                "shipments undo strategy",
                lexical_opts,
                atoms_dir=str(self.brain / "2-atoms"),
                index_path=str(self.index),
                chroma_path=str(self.chroma),
            )
            self.assertEqual(calls["dense"], before)

    def test_trap_queries_do_not_return_an_unapproved_atom(self) -> None:
        from lexical_store import rebuild_lexical_index
        from recall import RecallOptions, search_memory
        from v040_sota_upgrades_tests import _seed_verified_atom

        dataset = json.loads((Path(__file__).resolve().parents[1] / "docs" / "retrieval-evaluation-proposed.json").read_text(encoding="utf-8"))
        traps = [item["query"] for item in dataset["queries"] if item.get("answerability") == "unanswerable"]
        self.assertEqual(len(traps), 25)
        _seed_verified_atom(
            self.brain,
            filename="quorvex-rule.md",
            claim="Quorvex zindlemark stays on the approved shelf.",
            approve=True,
        )
        _seed_verified_atom(
            self.brain,
            filename="trap-note.md",
            claim=traps[0],
            approve=False,
        )
        rebuild_lexical_index(str(self.brain / "2-atoms"), str(self.index))
        opts = RecallOptions(retrieval_mode="auto", include_statuses={"accepted"})
        for query in traps:
            direct, neighbors = search_memory(
                query,
                opts,
                atoms_dir=str(self.brain / "2-atoms"),
                index_path=str(self.index),
                chroma_path=str(self.brain / "missing-chroma"),
            )
            names = {item.filename for item in direct + neighbors}
            self.assertNotIn("trap-note.md", names)
            self.assertNotIn("quorvex-rule.md", names)

    def test_planner_unions_clauses_and_stops_at_four_searches(self) -> None:
        from query_planner import plan_subqueries
        from streaming_chat import ChatPaths, retrieve_context

        self.assertEqual(plan_subqueries("alpha policy"), ("alpha policy",))
        self.assertEqual(plan_subqueries("alpha and beta"), ("alpha and beta",))
        wide = "alpha policy; beta rollback; gamma audit; delta restore; epsilon freeze"
        planned = plan_subqueries(wide)
        self.assertEqual(planned[0], wide)
        self.assertEqual(len(planned), 4)
        self.assertNotIn("delta restore", planned)
        self.assertNotIn("epsilon freeze", planned)

        full = _card("full.md", "Alpha policy covers beta rollback.")
        alpha = _card("alpha.md", "Alpha policy is reviewed on Monday.")
        beta = _card("beta.md", "Beta rollback is rehearsed quarterly.")
        calls: list[str] = []

        def fake_search(query, _options, **_kwargs):
            calls.append(query)
            if query == "alpha policy and beta rollback":
                return [full], []
            if query == "alpha policy":
                return [alpha], []
            if query == "beta rollback":
                return [beta], []
            return [], []

        paths = ChatPaths.for_brain(self.brain)
        with patch("streaming_chat.search_memory", side_effect=fake_search):
            ctx = retrieve_context("alpha policy and beta rollback", paths, retrieval_mode="lexical")
        self.assertEqual(calls, ["alpha policy and beta rollback", "alpha policy", "beta rollback"])
        self.assertEqual([item["atom_id"] for item in ctx["verified"]], ["full.md", "alpha.md", "beta.md"])
        self.assertEqual(ctx["subqueries"][0], "alpha policy and beta rollback")
        self.assertIsNone(ctx["retrieval_fallback"])

        calls.clear()
        with patch("streaming_chat.search_memory", side_effect=fake_search):
            retrieve_context(wide, paths, retrieval_mode="lexical")
        self.assertEqual(len(calls), 4)

    def test_third_abstain_writes_one_inbox_draft_and_a_secret_is_withheld(self) -> None:
        from cognitive_core import record_unanswered_query

        kwargs = {
            "project_scope": "project:alpha",
            "rejected_atom_ids": ["../2-atoms/evil.md", "kept-atom.md"],
            "brain_dir": str(self.brain),
            "db_path": str(self.brain / ".sidecar" / "telemetry.sqlite3"),
        }
        first = record_unanswered_query("How does the lattice backup work?", **kwargs)
        second = record_unanswered_query("How does the lattice backup work?", **kwargs)
        self.assertFalse(first["drafted"])
        self.assertFalse(second["drafted"])
        self.assertEqual(list((self.brain / "0-raw" / "inbox").glob("*.md")), [])
        third = record_unanswered_query("How does the lattice backup work?", **kwargs)
        drafts = list((self.brain / "0-raw" / "inbox").glob("*.md"))
        self.assertTrue(third["drafted"])
        self.assertEqual(len(drafts), 1)
        body = drafts[0].read_text(encoding="utf-8")
        self.assertIn("second_brain_origin: miss_inbox", body)
        self.assertIn("kept-atom.md", body)
        self.assertNotIn("../", body)
        self.assertNotIn("2-atoms", body)
        self.assertNotIn("review_status: accepted", body)
        fourth = record_unanswered_query("How does the lattice backup work?", **kwargs)
        self.assertFalse(fourth["drafted"])
        self.assertEqual(len(list((self.brain / "0-raw" / "inbox").glob("*.md"))), 1)

        secret = "sk_live_" + ("a" * 24)
        hidden = record_unanswered_query(secret, **kwargs)
        hidden2 = record_unanswered_query(secret, **kwargs)
        hidden3 = record_unanswered_query(secret, **kwargs)
        self.assertTrue(hidden3["drafted"])
        self.assertTrue(hidden["withheld"] or hidden3["withheld"])
        for path in (self.brain / "0-raw" / "inbox").glob("*.md"):
            self.assertNotIn(secret, path.read_text(encoding="utf-8"))
        self.assertNotIn(secret, (self.brain / ".sidecar" / "telemetry.sqlite3").read_bytes().decode("utf-8", "ignore"))
        self.assertFalse(hidden2["drafted"])

    def test_direct_answer_ignores_episodes_and_research_lists_them(self) -> None:
        from lexical_store import rebuild_lexical_index
        from streaming_chat import ChatPaths, chat_events
        from v040_sota_upgrades_tests import _seed_verified_atom

        _seed_verified_atom(
            self.brain,
            filename="rollback-rule.md",
            claim="Production deploys require a tested rollback plan.",
            approve=True,
        )
        rebuild_lexical_index(str(self.brain / "2-atoms"), str(self.index))
        experience = self.brain / "4-experiences" / "exp-rollback.md"
        experience.write_text(
            "---\ntype: experience\ngoal: Rehearse the quorvex rollback drill\nresult: The drill finished.\n---\n\n## Goal\nRehearse the quorvex rollback drill\n",
            encoding="utf-8",
        )
        paths = ChatPaths.for_brain(self.brain, policy_path=self.root / "missing-policy.json")
        events = list(chat_events("production deploys rollback plan", mode="direct", paths=paths, retrieval_mode="lexical"))
        answer = next(event for event in events if event["type"] == "verified_answer")
        self.assertTrue(answer["answerable"])
        self.assertNotIn("quorvex", str(answer["text"]).casefold())
        self.assertEqual(answer["claims"][0]["evidence"][0]["atom_id"], "rollback-rule.md")
        context = next(event for event in events if event["type"] == "context")
        self.assertFalse(any(source.get("tier") == "episodic" for source in context["sources"]))

        research = list(
            chat_events(
                "quorvex rollback drill",
                mode="research",
                paths=paths,
                retrieval_mode="lexical",
                json_fn=lambda *_args, **_kwargs: '{"claim_ids": []}',
            )
        )
        leads = next(event for event in research if event["type"] == "research_leads")
        self.assertIn("episodic", str(leads["text"]))
        self.assertTrue(any(item.get("source_kind") == "episodic" for item in leads["leads"]))
        verified = next(event for event in research if event["type"] == "verified_answer")
        self.assertFalse(verified["answerable"])
        self.assertEqual(verified["claims"], [])

    def test_direct_measurement_skips_without_writing_when_ollama_is_absent(self) -> None:
        import evaluate_answers

        destination = self.root / "should-not-exist.json"
        with patch.object(evaluate_answers, "direct_model_available", return_value=(False, "Ollama is not available.")):
            code = evaluate_answers.main(["--chat-mode", "direct", "--output", str(destination), "--model", "llama3.2:3b"])
        self.assertEqual(code, 0)
        self.assertFalse(destination.exists())
        summary = evaluate_answers.summarize_direct_observations(
            [
                {"answerability": "answerable", "answerable": True, "model_ms": 10.0, "json_fn_ran": False},
                {"answerability": "answerable", "answerable": False, "model_ms": 30.0, "json_fn_ran": True},
                {"answerability": "unanswerable", "answerable": False, "model_ms": 0.0, "json_fn_ran": False},
            ]
        )
        self.assertEqual(summary["coverage"], 0.5)
        self.assertEqual(summary["trap_leakage"], 0)
        self.assertEqual(summary["model_ms_p50"], 10.0)
        self.assertEqual(summary["json_fn_runs"], 1)


class SecurityPathTests(unittest.TestCase):
    def test_restricted_directory_keeps_execute_and_accepts_a_lock_file(self) -> None:
        from host_adapters import restrict_security_file_permissions

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sec"
            models = root / "models"
            models.mkdir(parents=True)
            token = root / "mcp-token"
            token.write_text("token", encoding="utf-8")
            restrict_security_file_permissions(root)
            restrict_security_file_permissions(models)
            restrict_security_file_permissions(token)
            lock = models / ".models.lock"
            lock.write_text("ok", encoding="utf-8")
            self.assertTrue(lock.is_file())
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(models.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)

    def test_planted_symlink_stays_blocked_and_os_alias_does_not(self) -> None:
        import mcp_server
        from config import is_untrusted_reparse_or_link

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir()
            planted = root / "planted"
            planted.symlink_to(real, target_is_directory=True)
            with self.assertRaises(PermissionError):
                mcp_server._assert_no_symlink_ancestors(planted / "mcp-token")
            plain = root / "plain"
            plain.mkdir()
            mcp_server._assert_no_symlink_ancestors(plain / "mcp-token")

        def _posix(path: str) -> str:
            return str(path).replace("\\", "/")

        @contextmanager
        def _darwin_alias(realpath_value: str):
            # Windows abspath("/var") becomes "<drive>\\var", so the simulated
            # macOS check keeps the POSIX path it was given.
            with (
                patch("config.sys.platform", "darwin"),
                patch("config._is_reparse_or_link", return_value=True),
                patch("config.os.path.realpath", return_value=realpath_value),
                patch("config.os.path.abspath", side_effect=_posix),
                patch("config.os.path.normpath", side_effect=_posix),
            ):
                yield

        with _darwin_alias("/private/var"):
            self.assertFalse(is_untrusted_reparse_or_link("/var"))
        with _darwin_alias("/tmp/elsewhere"):
            self.assertTrue(is_untrusted_reparse_or_link("/var"))
        with _darwin_alias("/private/var/folders/xx"):
            self.assertTrue(is_untrusted_reparse_or_link("/var/folders/xx"))

    def test_vault_slug_matches_across_a_directory_alias(self) -> None:
        import vault_sync

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real-vault"
            real.mkdir()
            alias = root / "alias-vault"
            alias.symlink_to(real, target_is_directory=True)
            self.assertEqual(vault_sync._vault_key_slug(real), vault_sync._vault_key_slug(alias))
            self.assertEqual(
                vault_sync.sync_config_path(real, state_dir=root / "sec").name,
                vault_sync.sync_config_path(alias, state_dir=root / "sec").name,
            )

    def test_interaction_encoder_name_matches_the_status_label(self) -> None:
        from cross_encoder import InteractionCrossEncoder

        self.assertEqual(InteractionCrossEncoder.backend_name, "interaction_cross_encoder")

    def test_console_print_survives_a_cp1252_stream(self) -> None:
        import ingest_multimodal

        class _Cp1252:
            encoding = "cp1252"

            def __init__(self) -> None:
                self.parts: list[str] = []

            def write(self, text: str) -> int:
                text.encode(self.encoding)
                self.parts.append(text)
                return len(text)

            def flush(self) -> None:
                return None

        stream = _Cp1252()
        with patch("sys.stdout", stream):
            ingest_multimodal._console_print("  [✓] Transcript saved → note.md")
        rendered = "".join(stream.parts)
        self.assertIn("Transcript saved", rendered)
        rendered.encode("cp1252")

    def test_cross_encoder_dir_rejects_a_symlinked_parent(self) -> None:
        from cross_encoder import resolve_cross_encoder_dir

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "models"
            slot = real / "cross-encoder"
            slot.mkdir(parents=True)
            (slot / "model.onnx").write_bytes(b"onnx")
            (slot / "tokenizer.json").write_bytes(b"{}")
            self.assertEqual(resolve_cross_encoder_dir(slot), slot)
            alias = root / "alias-models"
            alias.symlink_to(real, target_is_directory=True)
            self.assertIsNone(resolve_cross_encoder_dir(alias / "cross-encoder"))


class UpgradeRecallGateTests(unittest.TestCase):
    def test_high_cosine_without_shared_words_is_not_corroboration(self) -> None:
        from hardware_profile import classify_hardware_tier
        from recall import candidate_has_semantic_corroboration

        logs = (
            "---\nid: logs\n---\n\n## Claim\nLexical retrieval indexes structured logs.\n\n"
            "## Evidence\nStructured logs stay traceable.\n"
        )
        self.assertFalse(
            candidate_has_semantic_corroboration(
                "What is the owner's favorite battery chemistry?",
                logs,
                distance=0.05,
            )
        )
        self.assertTrue(
            candidate_has_semantic_corroboration(
                "How should logs be formatted so requests are traceable?",
                logs,
                distance=0.20,
            )
        )
        profile = classify_hardware_tier(
            os_name="linux",
            os_release="test",
            architecture="x86_64",
            cpu_threads=8,
            total_ram_gib=16.0,
            available_ram_gib=8.0,
            cpp_runtime_ready=True,
            cpp_runtime_detail="ok",
            gpu_backend="cpu",
            gpu_name="",
            vram_gib=0.0,
            ollama_reachable=True,
            installed_ollama_models=("qwen2.5-coder:7b", "nomic-embed-text"),
            model_sizes_bytes={"qwen2.5-coder:7b": 4_000_000_000},
        )
        self.assertEqual(profile.tier_id, "tier_2_cpu_balanced")
        self.assertEqual(profile.answer_p95_slo_ms, 35000.0)

    def test_new_thread_and_pending_dataset_stay_honest(self) -> None:
        from chat_threads import create_chat_thread
        from evaluation_dataset import release_gate_for_status

        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            brain.mkdir()
            created = create_chat_thread(title="cables", brain_dir=str(brain))
        self.assertEqual(created["mode"], "direct")
        payload = json.loads(
            (Path(__file__).resolve().parents[1] / "docs" / "retrieval-evaluation-proposed.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["status"], "pending_owner_review")
        self.assertEqual(release_gate_for_status(payload["status"]), "blocked_pending_owner_review")
        self.assertNotIn("owner_approval", payload)


if __name__ == "__main__":
    unittest.main()
