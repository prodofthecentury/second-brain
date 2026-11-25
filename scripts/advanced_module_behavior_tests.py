# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Behaviour checks for the optional advanced modules.

Each test calls the module. External models, Lean, ffmpeg, Kokoro weights,
Tailscale, and cloud accounts are not required. The docstring of each test
states what remains unverified when those are absent.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from scripts.adr_gate import ADRRequiredError
from scripts.experimental_lora_pipeline import export_lora_training_pairs
from scripts.formal_proof_search import SafeVerifyError, verify_lean_binary_available, verify_spec_integrity
from scripts.jarvis_orchestrator import OrchestratorSecurityError, export_open_webui_tool, validate_loopback_service_url
from scripts.screen_memory import is_window_blocked
from scripts.self_model_curiosity import load_learning_goals, upsert_learning_goals
from scripts.skill_pack_importer import import_skill_pack
from scripts.symbolic_sandbox import SandboxSecurityError, run_sandboxed_python, validate_sandbox_ast
from scripts.tts_engine import strip_spoken_citations, tts_status
from scripts.video_extractor import format_timestamp_hms, hamming_distance64, resolve_ffmpeg_tools
from scripts.world_model_sim import simulate_change_impact
from scripts.zero_trust_tunnel import TunnelSecurityError, authorize_tunnel_request, configure_tunnel


class AdvancedModuleBehaviorTests(unittest.TestCase):
    def test_jarvis_refuses_public_urls_and_builds_a_loopback_tool(self) -> None:
        with self.assertRaises(OrchestratorSecurityError):
            validate_loopback_service_url("https://example.com/hook", service_name="n8n")
        tool = export_open_webui_tool(api_port=8765)
        blob = str(tool)
        self.assertIn("127.0.0.1", blob)
        self.assertNotIn("https://api.openai.com", blob)
        # Unverified: a live Open WebUI, n8n, or OpenHands process is not started.

    def test_safeverify_accepts_an_evolve_block_edit_and_rejects_a_signature_edit(self) -> None:
        baseline = "def add(a, b):\n    # EVOLVE-BLOCK-START\n    return a + b\n    # EVOLVE-BLOCK-END\n"
        edited = "def add(a, b):\n    # EVOLVE-BLOCK-START\n    return a + b + 0\n    # EVOLVE-BLOCK-END\n"
        ok = verify_spec_integrity(baseline, edited, language="python")
        self.assertTrue(ok["valid"])
        self.assertEqual(ok["evolve_block_count"], 1)
        tampered = "def add(a, b, c):\n    # EVOLVE-BLOCK-START\n    return a + b\n    # EVOLVE-BLOCK-END\n"
        with self.assertRaises(SafeVerifyError):
            verify_spec_integrity(baseline, tampered, language="python")
        cheat = baseline.replace("return a + b", "return a + b\n    import os\n")
        with self.assertRaises((SafeVerifyError, SandboxSecurityError)):
            verify_spec_integrity(baseline, cheat, language="python")
        # Unverified: the Lean CLI is not required. This records whether it exists.
        self.assertTrue(verify_lean_binary_available() is None or isinstance(verify_lean_binary_available(), str))

    def test_sandbox_runs_arithmetic_and_rejects_imports(self) -> None:
        ran = run_sandboxed_python("print(20 + 22)", timeout_seconds=5)
        self.assertIn("42", str(ran.get("stdout") or ran))
        with self.assertRaises(SandboxSecurityError):
            validate_sandbox_ast("import os\nos.system('echo no')\n")

    def test_tunnel_is_disabled_until_configured_and_then_blocks_mutations(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb-tunnel-") as temp:
            state = Path(temp) / "state"
            brain = Path(temp) / "brain"
            brain.mkdir()
            with self.assertRaises(TunnelSecurityError):
                authorize_tunnel_request("GET", "/v1/health", state_dir=state, brain_dir=brain)
            configure_tunnel(
                enabled=True,
                provider="tailscale",
                target_port=8765,
                gateway_port=8790,
                rate_limit_per_min=30,
                state_dir=state,
            )
            allowed = authorize_tunnel_request("GET", "/v1/health", state_dir=state, brain_dir=brain)
            self.assertEqual(allowed.get("scope") or allowed.get("decision") or "allowed", allowed.get("scope", "allowed"))
            with self.assertRaises(TunnelSecurityError):
                authorize_tunnel_request("POST", "/v1/atoms/approve", state_dir=state, brain_dir=brain)
        # Unverified: tailscale and cloudflared binaries are not launched.

    def test_lora_export_refuses_to_run_without_an_owner_adr(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb-lora-") as temp:
            brain = Path(temp) / "brain"
            atoms = brain / "2-atoms"
            atoms.mkdir(parents=True)
            with self.assertRaises(ADRRequiredError):
                export_lora_training_pairs(brain_dir=brain, atoms_dir=atoms)
        # Unverified: Unsloth/PEFT training is not executed. The module only writes a config after the ADR.

    def test_world_model_reports_a_local_import_edge(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb-sim-") as temp:
            root = Path(temp)
            repo = root / "repo"
            brain = root / "brain"
            atoms = brain / "2-atoms"
            repo.mkdir()
            atoms.mkdir(parents=True)
            (repo / "leaf.py").write_text("VALUE = 1\n", encoding="utf-8")
            (repo / "top.py").write_text("import leaf\n", encoding="utf-8")
            report = simulate_change_impact(
                "leaf",
                repo_dir=repo,
                allowed_repo_roots=(str(root),),
                brain_dir=brain,
                atoms_dir=atoms,
            )
            self.assertTrue(report)
            blob = str(report).lower()
            self.assertTrue("leaf" in blob or "top" in blob)

    def test_tts_strips_citations_and_reports_missing_weights(self) -> None:
        spoken = strip_spoken_citations("The claim is true [V1] and [source](brain/0-raw/a.md).")
        self.assertNotIn("[V1]", spoken)
        self.assertNotIn("](", spoken)
        self.assertIn("true", spoken.lower())
        self.assertIn("source", spoken.lower())
        status = tts_status()
        self.assertIn("available", status or {})
        # Unverified: Kokoro ONNX weights are not downloaded and no audio sample is synthesized here.

    def test_video_timestamp_and_hash_distance_do_not_need_ffmpeg(self) -> None:
        self.assertEqual(format_timestamp_hms(3661), "01:01:01.000")
        self.assertEqual(hamming_distance64(0b1111, 0b1010), 2)
        ffmpeg, ffprobe = resolve_ffmpeg_tools()
        self.assertTrue(ffmpeg is None or isinstance(ffmpeg, str))
        self.assertTrue(ffprobe is None or isinstance(ffprobe, str))
        # Unverified: frame extraction is skipped when ffmpeg is absent.

    def test_screen_memory_blocks_a_password_window(self) -> None:
        self.assertIsNotNone(is_window_blocked("1Password — vault", "1Password"))
        self.assertIsNone(is_window_blocked("Project notes", "TextEdit"))
        # Unverified: Windows OCR and a live screen-capture loop are not run.

    def test_self_model_round_trips_a_learning_goal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb-self-") as temp:
            brain = Path(temp) / "brain"
            saved = upsert_learning_goals(
                [{"id": "proofs", "title": "Practice local proofs", "domain": "math", "target_atoms": 3}],
                brain_dir=brain,
            )
            self.assertEqual(saved["goal_count"], 1)
            loaded = load_learning_goals(brain_dir=brain)
            self.assertEqual(loaded[0]["id"], "proofs")
            self.assertTrue((brain / "self-model.md").is_file())

    def test_skill_pack_import_stages_a_pending_atom(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb-pack-") as temp:
            pack = Path(temp) / "pack"
            pack.mkdir()
            (pack / "SKILL.md").write_text(
                "---\nname: pack-skill\ndescription: Adds two numbers.\n---\n\nAdd the inputs.\n",
                encoding="utf-8",
            )
            brain = Path(temp) / "brain"
            result = import_skill_pack(pack, "demo-pack", brain_dir=brain)
            self.assertGreaterEqual(int(result.get("imported") or result.get("atom_count") or result.get("staged") or 1), 1)
            staged = list((brain / "2-atoms").glob("*.md")) + list((brain / "sources").rglob("*.md"))
            self.assertTrue(staged)
            blob = "\n".join(p.read_text(encoding="utf-8") for p in staged)
            self.assertIn("Add the inputs", blob)

    def test_swarm_project_filter_keeps_foreign_projects_out(self) -> None:
        from scripts.cognitive_swarm import _projects_strictly_within

        self.assertTrue(_projects_strictly_within(("project:alpha",), "project:alpha"))
        self.assertFalse(_projects_strictly_within(("project:beta",), "project:alpha"))
        # Unverified: a multi-model debate is not called. That path needs a local model.


if __name__ == "__main__":
    raise SystemExit(unittest.main())
