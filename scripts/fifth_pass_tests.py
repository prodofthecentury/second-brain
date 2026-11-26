# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Windows-laptop bugs: house rules, model pin, lexical rerank, eval labels, distill repair."""

from __future__ import annotations

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
                self.assertEqual(generation_candidates("qwen2.5-coder:7b")[0], "llama3.2:3b")


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


if __name__ == "__main__":
    unittest.main()
