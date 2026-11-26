# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Windows-laptop bugs: house rules, model pin, lexical rerank, eval labels, distill repair."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class HouseRulesInstallTests(unittest.TestCase):
    def test_clean_install_creates_house_rules_and_distill_survives_a_missing_file(self) -> None:
        from scripts.cli import install
        from scripts.config import DEFAULT_HOUSE_RULES
        from scripts.ollama_refinery import get_house_rules

        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            security = Path(tmp) / "security"
            env = {
                "BRAIN_VAULT_DIR": str(vault),
                "BRAIN_SECURITY_STATE_DIR": str(security),
            }
            with patch.dict(os.environ, env, clear=False):
                code = install(type("Args", (), {"no_index": False})())
                rules = vault / "house-rules.md"
                self.assertEqual(code, 0)
                self.assertTrue(rules.is_file())
                self.assertIn("verbatim", rules.read_text(encoding="utf-8"))
                rules.unlink()
                self.assertEqual(get_house_rules(), DEFAULT_HOUSE_RULES)


class GenerationModelTests(unittest.TestCase):
    def test_explicit_env_beats_a_saved_policy_default(self) -> None:
        from scripts.hardware_profile import classify_hardware_tier, model_for_local_rule
        from scripts.ollama_utils import generation_candidates

        tight = classify_hardware_tier(
            os_name="Windows",
            os_release="11",
            architecture="AMD64",
            cpu_threads=8,
            total_ram_gib=16.0,
            available_ram_gib=9.0,
            cpp_runtime_ready=True,
            cpp_runtime_detail="ok",
            gpu_backend="cpu",
            gpu_name="CPU",
            vram_gib=0.0,
            ollama_reachable=True,
            installed_ollama_models=("llama3.2:3b", "qwen2.5-coder:7b"),
        )
        self.assertEqual(tight.selected_installed_generation_model, "llama3.2:3b")
        with patch.dict(os.environ, {"BRAIN_GEN_MODEL": "llama3.2:3b"}):
            self.assertEqual(model_for_local_rule("qwen2.5-coder:7b"), "llama3.2:3b")
            with patch("scripts.ollama_utils.installed_model_names", return_value=["llama3.2:3b", "qwen2.5-coder:7b"]):
                self.assertEqual(generation_candidates("qwen2.5-coder:7b"), ["llama3.2:3b"])


class LexicalRerankTests(unittest.TestCase):
    def test_lexical_mode_reranks_when_enabled(self) -> None:
        from scripts.cross_encoder import rerank_recall_results
        from scripts.recall import RecallOptions, RecallResult, search_memory

        def result(name: str, score: float) -> RecallResult:
            return RecallResult(
                filename=name,
                content=name,
                source="lexical",
                score=score,
                similarity=score,
                review_status="accepted",
                memory_type="semantic",
                confidence=0.5,
                importance=0.5,
            )

        reranked = rerank_recall_results(
            "query",
            [result("first.md", 1.0), result("second.md", 0.5)],
            mode="lexical",
            allow_onnx=False,
        )
        # Default interaction scores may or may not flip these stubs. Force the encoder.
        with patch("scripts.cross_encoder._DEFAULT_INTERACTION_ENCODER.score_pairs", return_value=[0.0, 1.0]):
            reranked = rerank_recall_results(
                "query",
                [result("first.md", 1.0), result("second.md", 0.5)],
                mode="lexical",
                allow_onnx=False,
            )
        self.assertEqual(reranked[0].filename, "second.md")
        self.assertTrue(any(str(reason).startswith("rerank:") for reason in reranked[0].match_reasons))

        with tempfile.TemporaryDirectory() as tmp:
            atoms = Path(tmp) / "2-atoms"
            atoms.mkdir()
            (atoms / "low.md").write_text(
                "---\nid: low\nreview_status: pending\n---\n\n## Claim\nA low overlap note about nothing.\n\n## Evidence\nA low overlap note about nothing.\n",
                encoding="utf-8",
            )
            (atoms / "high.md").write_text(
                "---\nid: high\nreview_status: pending\n---\n\n## Claim\nThe zephyr token is the answer.\n\n## Evidence\nThe zephyr token is the answer.\n",
                encoding="utf-8",
            )
            from scripts.lexical_store import rebuild_lexical_index

            index = Path(tmp) / "lexical.sqlite3"
            rebuild_lexical_index(str(atoms), str(index))
            with patch("scripts.cross_encoder._DEFAULT_INTERACTION_ENCODER.score_pairs", return_value=[0.0, 1.0]):
                direct, _neighbors = search_memory(
                    "zephyr token",
                    RecallOptions(
                        n_results=2,
                        follow_links=False,
                        retrieval_mode="lexical",
                        enable_reranker=True,
                        allow_unreviewed_exploratory=True,
                        include_statuses={"pending"},
                    ),
                    atoms_dir=str(atoms),
                    index_path=str(index),
                    chroma_path=str(Path(tmp) / "chroma"),
                )
        self.assertTrue(direct)
        self.assertTrue(any(str(reason).startswith("rerank:") for reason in direct[0].match_reasons))


class DistillRepairTests(unittest.TestCase):
    def test_json_repair_uses_exact_source_text_and_a_title_slug(self) -> None:
        from scripts.ollama_refinery import draft_memory_atoms

        source = "The pour-over ratio is one gram of coffee to sixteen grams of water."
        calls = {"n": 0}

        def _chat(*, messages, model=None, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"message": {"content": "I think the ratio is 1:16, more or less."}}
            return {
                "message": {
                    "content": (
                        '{"memories":[{"title":"Pour-over ratio","claim":"The pour-over ratio is one gram of coffee to sixteen grams of water.",'
                        '"evidence":"pour over ratio is one gram of coffee to sixteen grams of water",'
                        '"why":"It is the brewing ratio.","memory_type":"semantic"}]}'
                    )
                }
            }

        with patch("scripts.ollama_refinery.chat_with_fallback", side_effect=_chat):
            drafted = draft_memory_atoms(source, "0-raw/coffee.md", "House rules")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(drafted), 1)
        text = drafted[0]
        self.assertIn(source, text)
        self.assertIn("id: ", text)
        self.assertIn("pour-over-ratio", text)
        self.assertNotIn("ratioo", text)

    def test_empty_claim_json_derives_a_claim_from_the_evidence(self) -> None:
        from scripts.ollama_refinery import LAST_DISTILL_PATHS, draft_memory_atoms

        source = "The pour-over ratio is one gram of coffee to sixteen grams of water."
        recorded = (
            '{"atoms":[{"title":"","claim":"","evidence":'
            '"The pour-over ratio is one gram of coffee to sixteen grams of water.",'
            '"why":"","memory_type":"semantic"}]}'
        )
        seen: dict[str, object] = {}

        def _chat(*, messages, model=None, **kwargs):
            seen["format"] = kwargs.get("format")
            seen["options"] = kwargs.get("options")
            return {"message": {"content": recorded}}

        with patch("scripts.ollama_refinery.chat_with_fallback", side_effect=_chat):
            drafted = draft_memory_atoms(source, "0-raw/coffee.md", "House rules")
        self.assertEqual(len(drafted), 1)
        self.assertIn(source, drafted[0])
        self.assertIn("derived_claim", LAST_DISTILL_PATHS[-1])
        self.assertIsInstance(seen["format"], dict)
        self.assertIn("atoms", seen["format"]["properties"])
        self.assertGreaterEqual(seen["format"]["properties"]["atoms"]["items"]["properties"]["claim"]["minLength"], 5)
        self.assertEqual(seen["options"]["temperature"], 0.1)

    def test_title_copied_into_the_claim_is_replaced_by_the_evidence_sentence(self) -> None:
        from scripts.ollama_refinery import draft_memory_atoms

        source = "The pour-over ratio is one gram of coffee to sixteen grams of water."
        recorded = (
            '{"atoms":[{"title":"Pour-over ratio","claim":"Pour-over ratio","evidence":'
            '"The pour-over ratio is one gram of coffee to sixteen grams of water.",'
            '"why":"Brewing.","memory_type":"semantic"}]}'
        )
        with patch("scripts.ollama_refinery.chat_with_fallback", return_value={"message": {"content": recorded}}):
            drafted = draft_memory_atoms(source, "0-raw/coffee.md", "House rules")
        self.assertIn("## Claim\n" + source, drafted[0])
        self.assertNotIn("## Claim\nPour-over ratio\n", drafted[0])

    def test_prose_falls_through_to_the_legacy_markdown_parser(self) -> None:
        from scripts.ollama_refinery import LAST_DISTILL_PATHS, draft_memory_atoms

        source = "The pour-over ratio is one gram of coffee to sixteen grams of water."
        legacy = (
            "```markdown\n---\nid: pour-over-ratio\ntype: atom\nmemory_type: semantic\n"
            "sources:\n  - 0-raw/coffee.md\n---\n\n"
            f"## Claim\n{source}\n\n## Evidence\n{source}\n\n"
            "## Why It Matters\nIt is the brewing ratio.\n```"
        )
        calls = {"n": 0}

        def _chat(*, messages, model=None, **_kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                return {"message": {"content": "I think the ratio is about one to sixteen."}}
            return {"message": {"content": legacy}}

        with patch("scripts.ollama_refinery.chat_with_fallback", side_effect=_chat):
            drafted = draft_memory_atoms(source, "0-raw/coffee.md", "House rules")
        self.assertEqual(len(drafted), 1)
        self.assertIn("legacy_markdown", LAST_DISTILL_PATHS[-1])
        self.assertIn(source, drafted[0])

    def test_evidence_that_does_not_support_the_claim_is_dropped(self) -> None:
        from scripts.ollama_refinery import draft_memory_atoms

        source = "The pour-over ratio is one gram of coffee to sixteen grams of water."
        recorded = (
            '{"atoms":[{"title":"Bananas","claim":"Bananas are blue today.",'
            '"evidence":"The pour-over ratio is one gram of coffee to sixteen grams of water.",'
            '"why":"No.","memory_type":"semantic"}]}'
        )
        with patch("scripts.ollama_refinery.chat_with_fallback", return_value={"message": {"content": recorded}}):
            with self.assertRaises(RuntimeError) as caught:
                draft_memory_atoms(source, "0-raw/coffee.md", "House rules")
        self.assertIn("Could not parse model memories", str(caught.exception))

    def test_optional_model_support_check_can_drop_a_lexical_match(self) -> None:
        from scripts.ollama_refinery import draft_memory_atoms

        source = "The pour-over ratio is one gram of coffee to sixteen grams of water."
        recorded = (
            '{"atoms":[{"title":"Pour-over ratio","claim":"The pour-over ratio is one gram of coffee to sixteen grams of water.",'
            '"evidence":"The pour-over ratio is one gram of coffee to sixteen grams of water.",'
            '"why":"Brewing.","memory_type":"semantic"}]}'
        )

        def _chat(*, messages, model=None, **_kwargs):
            if "Does the evidence support" in messages[0]["content"]:
                return {"message": {"content": "no"}}
            return {"message": {"content": recorded}}

        with patch.dict(os.environ, {"BRAIN_DISTILL_MODEL_SUPPORT_CHECK": "1"}):
            with patch("scripts.ollama_refinery.chat_with_fallback", side_effect=_chat):
                with self.assertRaises(RuntimeError) as caught:
                    draft_memory_atoms(source, "0-raw/coffee.md", "House rules")
        self.assertIn("Could not parse model memories", str(caught.exception))

    def test_token_repeat_abort_stays_on_the_pinned_model(self) -> None:
        from scripts.ollama_refinery import draft_memory_atoms

        seen: list[str] = []

        def boom(*_args, **kwargs):
            seen.append(str(kwargs.get("model")))
            raise RuntimeError("token repeat limit")

        with patch.dict(os.environ, {"BRAIN_GEN_MODEL": "llama3.2:3b"}):
            with patch("scripts.ollama_utils.ollama.chat", boom):
                with self.assertRaises(RuntimeError) as caught:
                    draft_memory_atoms(
                        "The pour-over ratio is one gram of coffee to sixteen grams of water.",
                        "0-raw/coffee.md",
                        "House rules",
                        model="qwen2.5-coder:7b",
                    )
        self.assertEqual(seen, ["llama3.2:3b"])
        self.assertIn("token repeat", str(caught.exception).lower())
        self.assertNotIn("qwen2.5-coder:7b", seen)


class DistillGroundingTests(unittest.TestCase):
    def test_prompt_example_cannot_be_mistaken_for_note_content(self) -> None:
        from scripts.ollama_refinery import ATOM_JSON_SCHEMA, _schema_prompt

        prompt = _schema_prompt("No source, no claim. A memory needs an exact quote from this note.")
        folded = prompt.casefold()
        self.assertNotIn("coffee", folded)
        self.assertNotIn("llama", folded)
        self.assertIn("XQV7PLACEHOLDER", prompt)
        self.assertEqual(ATOM_JSON_SCHEMA["properties"]["atoms"]["maxItems"], 5)

    def test_coffee_example_is_dropped_from_a_coffee_free_note(self) -> None:
        from scripts.ollama_refinery import draft_memory_atoms

        source = "No source, no claim. A memory needs an exact quote from this note."
        leaked = (
            '{"atoms":[{"title":"Pour-over ratio","claim":"Pour-over coffee uses one gram of coffee per sixteen grams of water.",'
            '"evidence":"Pour-over coffee uses one gram of coffee per sixteen grams of water.",'
            '"why":"Leaked example.","memory_type":"semantic"}]}'
        )
        with patch("scripts.ollama_refinery.chat_with_fallback", return_value={"message": {"content": leaked}}):
            with self.assertRaises(RuntimeError):
                draft_memory_atoms(source, "0-raw/06-no-source.md", "House rules")

    def test_paraphrased_evidence_is_replaced_by_the_source_span(self) -> None:
        from scripts.ollama_refinery import draft_memory_atoms

        source = "The pour-over ratio is one gram of coffee to sixteen grams of water."
        recorded = (
            '{"atoms":[{"title":"Pour-over ratio","claim":"The pour-over ratio is one gram of coffee to sixteen grams of water.",'
            '"evidence":"pour over RATIO is one gram of coffee to sixteen grams of water",'
            '"why":"Brewing.","memory_type":"semantic"}]}'
        )
        with patch("scripts.ollama_refinery.chat_with_fallback", return_value={"message": {"content": recorded}}):
            drafted = draft_memory_atoms(source, "0-raw/01-coffee.md", "House rules")
        self.assertIn("## Evidence\n" + source, drafted[0])
        self.assertNotIn("RATIO", drafted[0])

    def test_one_note_keeps_multiple_facts_with_unique_ids(self) -> None:
        from scripts.ollama_refinery import draft_memory_atoms

        source = "Daily backups run at 02:00. A 70B Hermes model needs a large GPU."
        recorded = (
            '{"atoms":['
            '{"title":"Daily backups","claim":"Daily backups run at 02:00.","evidence":"Daily backups run at 02:00.",'
            '"why":"Operations.","memory_type":"semantic"},'
            '{"title":"Daily backups","claim":"A 70B Hermes model needs a large GPU.","evidence":"A 70B Hermes model needs a large GPU.",'
            '"why":"Hardware.","memory_type":"semantic"}'
            ']}'
        )
        with patch("scripts.ollama_refinery.chat_with_fallback", return_value={"message": {"content": recorded}}):
            drafted = draft_memory_atoms(source, "0-raw/02-backups-hermes.md", "House rules")
        self.assertEqual(len(drafted), 2)
        ids = []
        for document in drafted:
            for line in document.splitlines():
                if line.startswith("id: "):
                    ids.append(line.split(": ", 1)[1])
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)
        self.assertIn("02-backups-hermes", ids[0])
        self.assertTrue(all(len(item.split("-")[-1]) == 8 for item in ids))
        joined = "\n".join(drafted)
        self.assertIn("Daily backups run at 02:00.", joined)
        self.assertIn("A 70B Hermes model needs a large GPU.", joined)


class PinnedModelFallbackTests(unittest.TestCase):
    def test_chat_does_not_try_a_second_model_after_token_repeat(self) -> None:
        from scripts.ollama_utils import chat_with_fallback

        seen: list[str] = []

        def boom(*_args, **kwargs):
            seen.append(str(kwargs.get("model")))
            raise RuntimeError("token repeat limit")

        with patch.dict(os.environ, {"BRAIN_GEN_MODEL": "llama3.2:3b"}):
            with patch("scripts.ollama_utils.ollama.chat", boom):
                with self.assertRaises(RuntimeError):
                    chat_with_fallback(
                        [{"role": "user", "content": "hi"}],
                        model="qwen2.5-coder:7b",
                    )
        self.assertEqual(seen, ["llama3.2:3b"])

    def test_allow_model_fallback_false_stops_after_one_model(self) -> None:
        from scripts.ollama_utils import chat_with_fallback

        seen: list[str] = []

        def boom(*_args, **kwargs):
            seen.append(str(kwargs.get("model")))
            raise RuntimeError("token repeat limit")

        with patch.dict(os.environ, {"BRAIN_GEN_MODEL": ""}, clear=False):
            with patch("scripts.ollama_utils.ollama.chat", boom):
                with patch(
                    "scripts.ollama_utils.installed_model_names",
                    return_value=["llama3.2:3b", "qwen2.5-coder:7b"],
                ):
                    with self.assertRaises(RuntimeError):
                        chat_with_fallback(
                            [{"role": "user", "content": "hi"}],
                            model="llama3.2:3b",
                            allow_model_fallback=False,
                        )
        self.assertEqual(seen, ["llama3.2:3b"])


class DoctorVaultTests(unittest.TestCase):
    def test_capability_line_reports_the_pinned_model_as_selected(self) -> None:
        from types import SimpleNamespace

        from scripts.doctor import capability_tier_detail

        profile = SimpleNamespace(
            tier_label="cpu-small",
            selected_installed_generation_model="qwen2.5-coder:7b",
        )
        with patch.dict(os.environ, {"BRAIN_GEN_MODEL": "llama3.2:3b"}):
            detail = capability_tier_detail(profile)
        self.assertIn("selected=llama3.2:3b", detail)
        self.assertIn("auto=qwen2.5-coder:7b", detail)

    def test_dated_hash_id_still_matches_a_stable_eval_label(self) -> None:
        from scripts.lexical_store import rebuild_lexical_index
        from scripts.personal_eval import evaluate_file

        with tempfile.TemporaryDirectory() as tmp:
            atoms = Path(tmp) / "2-atoms"
            atoms.mkdir()
            (atoms / "2026-11-26-coffee-pour-over-coffee-ratio-ab12cd34.md").write_text(
                "---\nid: 2026-11-26-coffee-pour-over-coffee-ratio-ab12cd34\n"
                "title: Pour-over coffee ratio\nreview_status: pending\n"
                "sources:\n  - 0-raw/01-coffee.md\n---\n\n"
                "## Claim\nThe pour-over coffee ratio is one gram of coffee to sixteen grams of water.\n\n"
                "## Evidence\nThe pour-over coffee ratio is one gram of coffee to sixteen grams of water.\n",
                encoding="utf-8",
            )
            index = Path(tmp) / "lexical.sqlite3"
            rebuild_lexical_index(str(atoms), str(index))
            spec = {
                "name": "personal-example",
                "k": 5,
                "allow_unreviewed_exploratory": True,
                "queries": [
                    {
                        "id": "coffee",
                        "query": "pour-over coffee ratio",
                        "relevant": ["pour-over-coffee-ratio", "Pour-over coffee ratio"],
                    }
                ],
            }
            path = Path(tmp) / "eval.json"
            path.write_text(json.dumps(spec), encoding="utf-8")
            report = evaluate_file(str(path), atoms_dir=str(atoms), index_path=str(index))
        self.assertEqual(report["recall_at_k"], 1.0)
        self.assertEqual(report["mrr"], 1.0)

    def test_doctor_uses_a_temporary_vault_when_the_configured_one_is_missing(self) -> None:
        import io

        from scripts.doctor import main

        missing = Path(tempfile.mkdtemp()) / "no-such-brain"
        buf = io.StringIO()
        with patch.dict(os.environ, {"BRAIN_VAULT_DIR": str(missing)}, clear=False):
            with patch("sys.stdout", buf):
                code = main([])
        text = buf.getvalue()
        self.assertIn("temporary vault", text)
        self.assertNotIn("Missing directories", text)
        self.assertEqual(code, 0)

    def test_empty_directory_is_treated_as_an_unconfigured_vault(self) -> None:
        import io

        from scripts.doctor import main

        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with patch.dict(os.environ, {"BRAIN_VAULT_DIR": tmp}, clear=False):
                with patch("sys.stdout", buf):
                    code = main([])
        self.assertIn("not initialized", buf.getvalue())
        self.assertIn("temporary vault", buf.getvalue())
        self.assertEqual(code, 0)

    def test_cli_doctor_reports_the_pinned_model_as_selected(self) -> None:
        import io
        from types import SimpleNamespace

        from scripts.cli import doctor

        profile = SimpleNamespace(
            tier_label="Tier 2 — Balanced CPU 7B Intelligence",
            recommended_generation_model="qwen2.5-coder:7b",
            selected_installed_generation_model="qwen2.5-coder:7b",
        )
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"BRAIN_VAULT_DIR": tmp, "BRAIN_GEN_MODEL": "llama3.2:3b"}):
                with patch("scripts.hardware_profile.detect_hardware_profile", return_value=profile):
                    with patch("sys.stdout", buf):
                        doctor()
        text = buf.getvalue()
        self.assertIn("selected=llama3.2:3b", text)
        self.assertNotIn("selected=qwen2.5-coder:7b", text)
        self.assertIn("auto=qwen2.5-coder:7b", text)


if __name__ == "__main__":
    unittest.main()
