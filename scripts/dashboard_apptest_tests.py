# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["streamlit"]
# ///
"""Streamlit AppTest integration suite for Second Brain local web dashboard (Goal 3 Pillar 3)."""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import streamlit as st
from streamlit.testing.v1 import AppTest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
APP_PATH = PROJECT_ROOT / "dashboard" / "app.py"

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

from scripts import dashboard_auth  # noqa: E402
import scripts.streaming_chat as streaming_chat_mod  # noqa: E402
from scripts.atom_mutator import apply_review_transition  # noqa: E402
from scripts.cognitive_core import atom_correction_log_path, atom_feedback_log_path  # noqa: E402
from scripts.evidence import find_evidence_anchor  # noqa: E402
from scripts.lexical_store import rebuild_lexical_index  # noqa: E402
from scripts.memory_review import parse_atom_record  # noqa: E402
from scripts.notes_store import NotePaths, create_note  # noqa: E402
from scripts.review_atom import approved_revision_is_current  # noqa: E402

TEST_PASSWORD = "correct-horse-battery-staple-99"


def _mock_hardware_profile(*_args, **_kwargs) -> SimpleNamespace:
    return SimpleNamespace(
        tier_id="standard_cpu",
        tier_label="Standard Local CPU",
        total_ram_gib=16.0,
        available_ram_gib=8.0,
        cpu_threads=8,
        gpu_name="Mock Test CPU",
        vram_gib=0.0,
        recommended_generation_model="llama3.2:3b",
        selected_installed_generation_model="llama3.2:3b",
        recommended_retrieval_mode="lexical",
        retrieval_p95_slo_ms=50.0,
        rationale="Deterministic test hardware profile.",
    )


def _seed_atom(
    brain_dir: Path,
    *,
    filename: str,
    claim: str,
    evidence: str,
    memory_type: str = "semantic",
    epistemic_label: str = "claim",
    contexts: list[str] | None = None,
    approve: bool = True,
) -> Path:
    raw_dir = brain_dir / "0-raw"
    atoms_dir = brain_dir / "2-atoms"
    playbooks_dir = brain_dir / "5-playbooks"
    sidecar_dir = brain_dir / ".sidecar"
    archive_dir = raw_dir / "archive"
    for folder in (raw_dir, atoms_dir, playbooks_dir, sidecar_dir, archive_dir):
        folder.mkdir(parents=True, exist_ok=True)

    src_name = f"src-{filename}"
    src_path = raw_dir / src_name
    src_path.write_text(f"Primary engineering source: {evidence}\n", encoding="utf-8")

    ctx_items = contexts if contexts is not None else ["project:second_brain"]
    ctx_yaml = "\n".join(f"  - {item}" for item in ctx_items)
    epistemic_extra = "epistemic_contract: owner_preference\n" if epistemic_label == "preference" else ""
    draft = (
        "---\n"
        f"id: {Path(filename).stem}\n"
        "type: atom\n"
        f"memory_type: {memory_type}\n"
        f"epistemic_label: {epistemic_label}\n"
        f"{epistemic_extra}"
        "created: 2026-11-10\n"
        "last-reviewed: 2026-11-15\n"
        "confidence: 0.90\n"
        "importance: 0.85\n"
        "sources:\n"
        f"  - 0-raw/{src_name}\n"
        "contexts:\n"
        f"{ctx_yaml}\n"
        "review_status: pending\n"
        "claim_state: candidate\n"
        "citation_integrity: missing\n"
        "---\n\n"
        "## Claim\n"
        f"{claim}\n\n"
        "## Evidence\n"
        f"{evidence}\n\n"
        "## Why It Matters\n"
        "Verifiable local second-brain test fixture.\n"
    )
    anchor = find_evidence_anchor(draft, str(brain_dir))
    parts = draft.split("---", 2)
    fm = parts[1]
    for key, val in anchor.items():
        fm += f"{key}: {val}\n"
    fm = fm.replace("citation_integrity: missing", "citation_integrity: anchored")
    atom_path = atoms_dir / filename
    atom_path.write_text(f"---{fm}---{parts[2]}", encoding="utf-8")

    if approve:
        apply_review_transition(
            action="approve",
            atom_path=str(atom_path),
            expected_sha256=hashlib.sha256(atom_path.read_bytes()).hexdigest(),
            actor="apptest-owner",
            decision_log_path=str(archive_dir / "decision-record.jsonl"),
            brain_dir=str(brain_dir),
            atoms_dir=str(atoms_dir),
        )
    rebuild_lexical_index(str(atoms_dir), str(sidecar_dir / "lexical.sqlite3"))
    return atom_path


class IsolatedDashboardAppTestCase(unittest.TestCase):
    """Run Streamlit AppTest against an isolated temporary vault & security state directory."""

    def setUp(self) -> None:
        super().setUp()
        self._stack = ExitStack()
        self.addCleanup(self._stack.close)

        self.temp_root = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self.vault_dir = self.temp_root / "brain"
        self.security_dir = self.temp_root / "security"
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.security_dir.mkdir(parents=True, exist_ok=True)
        self.auth_file = self.security_dir / "dashboard-auth.json"
        self.policy_file = self.security_dir / "host-sharing-policy.json"

        self._stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "BRAIN_VAULT_DIR": str(self.vault_dir),
                    "BRAIN_SECURITY_STATE_DIR": str(self.security_dir),
                    "XDG_STATE_HOME": str(self.temp_root / "xdg_state"),
                    "BRAIN_DASHBOARD_AUTH_FILE": str(self.auth_file),
                    "BRAIN_HOST_POLICY_FILE": str(self.policy_file),
                    "BRAIN_MEMORY_API_TOKEN_FILE": str(self.security_dir / "memory-api-token"),
                    "BRAIN_MCP_TOKEN_FILE": str(self.security_dir / "mcp-token"),
                    "BRAIN_OWNER_TOKEN_FILE": str(self.security_dir / "owner-token"),
                },
                clear=False,
            )
        )
        for env_key in (
            "OLLAMA_HOST",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
        ):
            os.environ.pop(env_key, None)

        dashboard_auth._RATE_LIMIT_TRACKER.clear()
        self.addCleanup(dashboard_auth._RATE_LIMIT_TRACKER.clear)

        prev_addr = st.get_option("server.address")
        st.config.set_option("server.address", "127.0.0.1")
        self.addCleanup(lambda: st.config.set_option("server.address", prev_addr))

        # Avoid slow network/GPU probes during UI tests
        self._stack.enter_context(patch("scripts.hardware_profile.detect_hardware_profile", _mock_hardware_profile))
        if "hardware_profile" in sys.modules:
            self._stack.enter_context(patch("hardware_profile.detect_hardware_profile", _mock_hardware_profile))
        self._stack.enter_context(patch("ollama.Client.list", return_value={"models": [{"name": "llama3.2:3b"}]}))

    def _configure_password(self, password: str = TEST_PASSWORD) -> Path:
        set_fn = getattr(dashboard_auth, "set_password_hash", None) or getattr(
            dashboard_auth, "save_password_verifier", None
        )
        if callable(set_fn):
            return Path(set_fn(password, self.auth_file))
        record = dashboard_auth.build_password_record(password)
        return dashboard_auth.save_password_record(record, self.auth_file)

    def _new_unlocked_app(self) -> AppTest:
        if not self.auth_file.exists():
            self._configure_password(TEST_PASSWORD)
        at = AppTest.from_file(str(APP_PATH), default_timeout=20)
        at.run()
        self.assertEqual(len(at.exception), 0, f"Unexpected exception on login screen: {at.exception}")
        at.text_input(key="brain_dashboard_password").set_value(TEST_PASSWORD)
        at.form_submit_button[0].click()
        at.run()
        self.assertEqual(len(at.exception), 0, f"Unexpected exception after unlock: {at.exception}")
        return at


class DashboardAuthAndLauncherTests(IsolatedDashboardAppTestCase):
    def test_auth_gate_blocks_non_loopback_missing_verifier_wrong_password_and_unlocks_valid(self) -> None:
        # 1. Non-loopback bind is rejected fail-closed
        st.config.set_option("server.address", "0.0.0.0")
        blocked = AppTest.from_file(str(APP_PATH), default_timeout=20)
        blocked.run()
        self.assertTrue(any("403 Forbidden" in e.value for e in blocked.error))
        self.assertEqual(len(blocked.sidebar.radio), 0)

        # 2. Loopback without configured password verifier stays locked and hides vault tabs
        st.config.set_option("server.address", "127.0.0.1")
        unconfigured = AppTest.from_file(str(APP_PATH), default_timeout=20)
        unconfigured.run()
        self.assertTrue(any("Second Brain is locked" in t.value for t in unconfigured.title))
        self.assertEqual(len(unconfigured.sidebar.radio), 0)
        self.assertFalse((self.vault_dir / "2-atoms").exists())

        # 3. Configured verifier: initial view shows login form; wrong password fails; valid password unlocks
        self._configure_password(TEST_PASSWORD)
        at = AppTest.from_file(str(APP_PATH), default_timeout=20)
        at.run()
        self.assertTrue(any("Unlock Second Brain" in t.value for t in at.title))
        self.assertEqual(len(at.sidebar.radio), 0)

        at.text_input(key="brain_dashboard_password").set_value("wrong-password-attempt-000")
        at.form_submit_button[0].click()
        at.run()
        self.assertTrue(any("Password did not match" in e.value for e in at.error))
        self.assertEqual(len(at.sidebar.radio), 0)

        at.text_input(key="brain_dashboard_password").set_value(TEST_PASSWORD)
        at.form_submit_button[0].click()
        at.run()
        self.assertEqual(len(at.exception), 0)
        self.assertEqual(len(at.sidebar.radio), 1)
        self.assertTrue(any("Owner session unlocked" in c.value for c in at.sidebar.caption))

    def test_posix_launchers_pure_lf_and_wheel_safe_runner(self) -> None:
        launchers = {
            "run_dashboard.sh": ("scripts/dashboard_auth.py", "dashboard/app.py", "--server.address 127.0.0.1"),
            "run_daemon.sh": ("scripts/watchdog_daemon.py",),
            "run_night_shift.sh": ("scripts/ollama_refinery.py", "scripts/ollama_editor.py"),
        }
        for rel_name, expected_tokens in launchers.items():
            path = PROJECT_ROOT / rel_name
            self.assertTrue(path.is_file(), f"Missing POSIX launcher: {rel_name}")
            raw = path.read_bytes()
            self.assertNotIn(b"\r", raw, f"CRLF detected in {rel_name}")
            text = raw.decode("utf-8")
            self.assertTrue(text.startswith("#!/usr/bin/env bash\n"))
            self.assertIn("set -euo pipefail", text)
            for tok in expected_tokens:
                self.assertIn(tok, text)

        # Verify run_script_process in dashboard/app.py has no mutable default and uses sys.executable + writable cwd
        app_src = APP_PATH.read_text(encoding="utf-8")
        self.assertNotIn("_stream_local_ollama_chat", app_src)
        self.assertIn("def run_script_process(script_rel_path: str, extra_args: Sequence[str] = ()):", app_src)


class DashboardChatModesAndResearchTests(IsolatedDashboardAppTestCase):
    def test_chat_standard_preference_and_research_views_render_without_live_ollama(self) -> None:
        _seed_atom(
            self.vault_dir,
            filename="sqlite-wal.md",
            claim="SQLite WAL mode enables concurrent readers during local lexical indexing.",
            evidence="SQLite WAL mode enables concurrent readers during local lexical indexing.",
            memory_type="semantic",
            approve=True,
        )
        _seed_atom(
            self.vault_dir,
            filename="owner-style.md",
            claim="Prefer terse engineering bullet summaries with explicit file paths.",
            evidence="Prefer terse engineering bullet summaries with explicit file paths.",
            memory_type="self",
            epistemic_label="preference",
            approve=True,
        )
        _seed_atom(
            self.vault_dir,
            filename="pending-lead-wal.md",
            claim="Unreviewed SQLite WAL checkpoint tuning draft recommends 4096 page size.",
            evidence="Unreviewed SQLite WAL checkpoint tuning draft recommends 4096 page size.",
            memory_type="semantic",
            approve=False,
        )
        inbox_dir = self.vault_dir / "0-raw" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        (inbox_dir / "quarantined-wal-note.md").write_text(
            "<!-- second_brain_origin: mcp_inbox -->\nSQLite WAL lexical indexing lead from untrusted capture.\n",
            encoding="utf-8",
        )
        create_note(
            "WAL Indexing Notes",
            "Working note about SQLite WAL lexical indexing throughput.",
            tags=["project:second_brain"],
            paths=NotePaths.for_brain(str(self.vault_dir)),
        )

        def fake_stream_factory(*_args, **_kwargs):
            def _stream(_prompt: str, _model: str):
                yield "SQLite WAL supports concurrent lexical readers [V1]."

            return _stream

        def fake_json_factory(*_args, **_kwargs):
            def _json(_prompt: str, _model: str, _schema: dict) -> str:
                return json.dumps({"claim_ids": [0]})

            return _json

        with ExitStack() as chat_patches:
            target_mods = {streaming_chat_mod}
            if "streaming_chat" in sys.modules:
                target_mods.add(sys.modules["streaming_chat"])
            for mod in target_mods:
                if hasattr(mod, "default_stream_fn"):
                    chat_patches.enter_context(patch.object(mod, "default_stream_fn", fake_stream_factory))
                if hasattr(mod, "default_host_stream_fn"):
                    chat_patches.enter_context(patch.object(mod, "default_host_stream_fn", fake_stream_factory))
                if hasattr(mod, "default_json_fn"):
                    chat_patches.enter_context(patch.object(mod, "default_json_fn", fake_json_factory))
                if hasattr(mod, "default_host_json_fn"):
                    chat_patches.enter_context(patch.object(mod, "default_host_json_fn", fake_json_factory))

            at = self._new_unlocked_app()

            # 1. Standard stream view: grounded synthesis without unverified leads box
            at.selectbox(key="chat_exec_mode").set_value("stream")
            at.selectbox(key="chat_answer_view").set_value("standard")
            at.selectbox(key="chat_retrieval_mode").set_value("lexical")
            at.text_input(key="chat_model_override").set_value("llama3.2:3b")
            at.chat_input[0].set_value("SQLite WAL lexical indexing")
            at.run()
            self.assertEqual(len(at.exception), 0, f"Chat standard stream exception: {at.exception}")
            self.assertFalse(
                any("UNVERIFIED LEADS — NOT OWNER-APPROVED" in w.value for w in at.warning),
                "Standard view must never surface unverified leads.",
            )
            self.assertTrue(any("Grounding Audit Passed" in s.value for s in at.success))

            # 2. Preference verified view: surfaces subjective owner preference
            at.selectbox(key="chat_exec_mode").set_value("verified")
            at.selectbox(key="chat_answer_view").set_value("preference")
            at.chat_input[0].set_value("engineering bullet summaries style")
            at.run()
            self.assertEqual(len(at.exception), 0, f"Chat preference exception: {at.exception}")
            self.assertTrue(
                any("Prefer terse engineering bullet summaries" in t.value for t in at.text),
                f"Expected preference answer in text elements: {[t.value for t in at.text]}",
            )

            # 3. Research stream view: renders distinct UNVERIFIED LEADS — NOT OWNER-APPROVED warning box
            at.selectbox(key="chat_exec_mode").set_value("stream")
            at.selectbox(key="chat_answer_view").set_value("research")
            at.chat_input[0].set_value("SQLite WAL lexical indexing")
            at.run()
            self.assertEqual(len(at.exception), 0, f"Chat research exception: {at.exception}")
            self.assertTrue(
                any("UNVERIFIED LEADS — NOT OWNER-APPROVED" in w.value for w in at.warning),
                f"Expected UNVERIFIED LEADS warning box, got warnings={[w.value for w in at.warning]}",
            )
            self.assertTrue(
                any("pending-lead-wal.md" in c.value for c in at.code),
                f"Expected pending lead rendered safely inside st.code, got={[c.value for c in at.code]}",
            )

            # 4. Strict audit answer mode with 'research' view also formats & separates unverified leads
            with patch(
                "scripts.host_adapters._invoke_provider_draft",
                return_value=json.dumps(
                    {"claims": ["SQLite WAL mode enables concurrent readers during local lexical indexing."]}
                ),
            ):
                at.selectbox(key="chat_exec_mode").set_value("strict_audit_answer")
                at.selectbox(key="chat_answer_view").set_value("research")
                at.chat_input[0].set_value("SQLite WAL lexical indexing")
                at.run()
                self.assertEqual(len(at.exception), 0, f"Strict audit research exception: {at.exception}")
                self.assertTrue(any("UNVERIFIED LEADS — NOT OWNER-APPROVED" in w.value for w in at.warning))


class DashboardGovernancePlaybooksFeedbackAndAuditTests(IsolatedDashboardAppTestCase):
    def test_review_merge_and_playbooks_tab_execute_cleanly(self) -> None:
        proc_claim = "Run uv run python scripts/rebuild_lexical_index.py before verifying search parity."
        _seed_atom(
            self.vault_dir,
            filename="rebuild-lexical-proc.md",
            claim=proc_claim,
            evidence=proc_claim,
            memory_type="procedural",
            approve=True,
        )
        survivor_path = _seed_atom(
            self.vault_dir,
            filename="survivor-atom.md",
            claim="Loopback binding enforces 127.0.0.1 listener safety.",
            evidence="Loopback binding enforces 127.0.0.1 listener safety.",
            memory_type="semantic",
            approve=False,
        )
        retire_path = _seed_atom(
            self.vault_dir,
            filename="duplicate-atom.md",
            claim="Duplicate note on 127.0.0.1 loopback listener binding.",
            evidence="Duplicate note on 127.0.0.1 loopback listener binding.",
            memory_type="semantic",
            approve=False,
        )

        at = self._new_unlocked_app()

        # Navigate to Vault Explorer -> approve survivor-atom.md, then CAS-merge duplicate-atom.md into it
        at.sidebar.radio[0].set_value("🗄️ Vault Explorer")
        at.run()
        self.assertEqual(len(at.exception), 0)

        # Select survivor-atom.md in the file list radio (sidebar has radio[0], main area has file list radio and view mode radio)
        file_radios = [r for r in at.radio if "survivor-atom.md" in r.options]
        self.assertEqual(len(file_radios), 1)
        file_radios[0].set_value("survivor-atom.md")
        at.run()
        self.assertEqual(len(at.exception), 0)

        at.button(key="approve-survivor-atom.md").click()
        at.run()
        self.assertEqual(len(at.exception), 0)
        self.assertTrue(
            approved_revision_is_current(
                str(survivor_path),
                brain_dir=str(self.vault_dir),
                atoms_dir=str(self.vault_dir / "2-atoms"),
            )
        )

        at.selectbox(key="merge-target-survivor-atom.md").set_value("duplicate-atom.md")
        at.text_input(key="merge-rationale-survivor-atom.md").set_value("Consolidate duplicate loopback atoms")
        at.button(key="merge-btn-survivor-atom.md").click()
        at.run()
        self.assertEqual(len(at.exception), 0)
        retired_rec = parse_atom_record(str(retire_path))
        self.assertEqual(retired_rec["review_status"], "merged")
        survivor_rec = parse_atom_record(str(survivor_path))
        self.assertEqual(survivor_rec["review_status"], "pending")

        # Navigate to Host API, Quarantine & Fitness -> promote procedural atom to Skill Playbook
        at.sidebar.radio[0].set_value("🧬 Host API, Quarantine & Fitness")
        at.run()
        self.assertEqual(len(at.exception), 0)

        at.multiselect(key="pb_backing_atoms").select("rebuild-lexical-proc.md")
        at.run()
        at.text_input(key="pb_id_in").set_value("lexical-rebuild-checklist")
        at.text_input(key="pb_title_in").set_value("Lexical Index Rebuild Checklist")
        at.text_input(key="pb_actor_in").set_value("owner_reviewer")
        at.text_input(key="pb_trigger_in").set_value("When FTS5 lexical index needs deterministic rebuild")
        at.text_area(key="pb_pre_in").set_value("Vault directory is unlocked and writable")
        at.text_area(key="pb_steps_in").set_value(proc_claim)
        at.text_area(key="pb_out_in").set_value("lexical.sqlite3 updated cleanly")
        at.text_area(key="pb_rec_in").set_value("Re-run rebuild_lexical_index.py from clean sidecar state")
        at.checkbox(key="pb_early").set_value(True)
        at.text_input(key="pb_rationale_in").set_value("Bootstrap verified procedural playbook")
        at.button(key="btn_promote_playbook").click()
        at.run()
        self.assertEqual(len(at.exception), 0, f"Playbook promote exception: {at.exception}")
        self.assertTrue(
            (self.vault_dir / "5-playbooks" / "lexical-rebuild-checklist.md").is_file(),
            f"Playbook file not created; errors={[e.value for e in at.error]}",
        )

    def test_feedback_alignment_persists_actor_note_and_superseded_writes_no_false_correction(self) -> None:
        _seed_atom(
            self.vault_dir,
            filename="feedback-atom.md",
            claim="Snapshots record Merkle roots over approved atoms and active playbooks.",
            evidence="Snapshots record Merkle roots over approved atoms and active playbooks.",
            memory_type="semantic",
            approve=True,
        )

        at = self._new_unlocked_app()
        at.sidebar.radio[0].set_value("🧬 Host API, Quarantine & Fitness")
        at.run()
        self.assertEqual(len(at.exception), 0)

        fb_log = Path(atom_feedback_log_path(str(self.vault_dir)))
        corr_log = Path(atom_correction_log_path(str(self.vault_dir)))

        # 1. Endorsed feedback
        at.selectbox(key="fb_atom_sel").set_value("feedback-atom.md")
        at.selectbox(key="fb_kind_sel").set_value("endorsed")
        at.text_input(key="fb_actor_in").set_value("cameron_owner")
        at.text_input(key="fb_note_in").set_value("Verified accurate during release audit")
        at.button(key="btn_record_fb").click()
        at.run()
        self.assertEqual(len(at.exception), 0)
        from scripts.cognitive_core import count_canonical_corrections

        self.assertEqual(
            count_canonical_corrections(str(self.vault_dir), "feedback-atom.md"),
            0,
            "Endorsed feedback must not record any atom_correction",
        )

        # 2. Superseded feedback MUST NOT write a false negative correction to atom-corrections.jsonl
        at.selectbox(key="fb_atom_sel").set_value("feedback-atom.md")
        at.selectbox(key="fb_kind_sel").set_value("superseded")
        at.text_input(key="fb_actor_in").set_value("cameron_owner")
        at.text_input(key="fb_note_in").set_value("Superseded by schema v2 snapshot atom")
        at.button(key="btn_record_fb").click()
        at.run()
        self.assertEqual(len(at.exception), 0)
        self.assertEqual(
            count_canonical_corrections(str(self.vault_dir), "feedback-atom.md"),
            0,
            "Submitting 'superseded' feedback must NEVER append a false atom_correction!",
        )

        # 3. Corrected feedback persists actor & note in both feedback log and canonical corrections log
        at.selectbox(key="fb_atom_sel").set_value("feedback-atom.md")
        at.selectbox(key="fb_kind_sel").set_value("corrected")
        at.text_input(key="fb_actor_in").set_value("cameron_owner")
        at.text_input(key="fb_note_in").set_value("Needs updated schema_version detail")
        at.button(key="btn_record_fb").click()
        at.run()
        self.assertEqual(len(at.exception), 0)

        self.assertTrue(fb_log.is_file())
        fb_entries = [json.loads(line) for line in fb_log.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([e["feedback_kind"] for e in fb_entries], ["helpful", "supersession", "correction"])
        for entry in fb_entries:
            self.assertEqual(entry["actor"], "cameron_owner")
        self.assertEqual(fb_entries[0]["note"], "Verified accurate during release audit")
        self.assertEqual(fb_entries[1]["note"], "Superseded by schema v2 snapshot atom")
        self.assertEqual(fb_entries[2]["note"], "Needs updated schema_version detail")

        self.assertTrue(corr_log.is_file())
        corr_entries = [json.loads(line) for line in corr_log.read_text(encoding="utf-8").splitlines() if line.strip()]
        correction_only = [e for e in corr_entries if e.get("action") == "atom_correction"]
        self.assertEqual(len(correction_only), 1)
        self.assertEqual(correction_only[0]["action"], "atom_correction")
        self.assertEqual(correction_only[0]["actor"], "cameron_owner")
        self.assertEqual(correction_only[0]["note"], "Needs updated schema_version detail")

        # 4. Experience Logger parity fields (plan, reasoning_summary, reflection, confidence_before/after)
        at.text_input(key="exp_goal").set_value("Validate Goal 3 dashboard parity")
        at.text_area(key="exp_res").set_value("All Streamlit AppTest checks passed.")
        at.text_area(key="exp_plan").set_value("Inspect contracts\nRun AppTest suite")
        at.text_area(key="exp_actions").set_value("Executed unittest suite")
        at.text_area(key="exp_lessons").set_value("Isolate vault paths per test")
        at.text_area(key="exp_reasoning").set_value("Used signature inspection for compatibility.")
        at.text_area(key="exp_reflection").set_value("Zero regressions across tabs.")
        at.checkbox(key="exp_track_conf").set_value(True)
        at.run()
        at.slider(key="exp_conf_before").set_value(0.6)
        at.slider(key="exp_conf_after").set_value(0.95)
        at.button(key="btn_save_exp").click()
        at.run()
        self.assertEqual(len(at.exception), 0)
        exp_files = list((self.vault_dir / "4-experiences").glob("*.md"))
        self.assertEqual(len(exp_files), 1)
        exp_text = exp_files[0].read_text(encoding="utf-8")
        if "confidence_before" in inspect.signature(__import__("scripts.cognitive_core", fromlist=["save_experience_record"]).save_experience_record).parameters:
            self.assertIn("confidence_before: 0.6", exp_text)
            self.assertIn("confidence_after: 0.95", exp_text)

    def test_time_travel_verify_snapshot_and_source_audit_demote_stale(self) -> None:
        healthy_path = _seed_atom(
            self.vault_dir,
            filename="healthy-anchor.md",
            claim="Merkle leaves sort canonically by relative vault path.",
            evidence="Merkle leaves sort canonically by relative vault path.",
            memory_type="semantic",
            approve=True,
        )
        stale_path = _seed_atom(
            self.vault_dir,
            filename="drifting-anchor.md",
            claim="Snapshot files are stored inside .sidecar/snapshots with 0600 permissions.",
            evidence="Snapshot files are stored inside .sidecar/snapshots with 0600 permissions.",
            memory_type="semantic",
            approve=True,
        )

        at = self._new_unlocked_app()
        at.sidebar.radio[0].set_value("🛡️ Vault Proof & Time-Travel")
        at.run()
        self.assertEqual(len(at.exception), 0)

        # 1. Capture snapshot & run Verify Snapshot Integrity
        at.text_input(key="tt_label").set_value("apptest-baseline")
        at.text_input(key="tt_actor").set_value("cameron_owner")
        at.button(key="btn_take_snap").click()
        at.run()
        self.assertEqual(len(at.exception), 0)

        at.button(key="btn_verify_snap").click()
        at.run()
        self.assertEqual(len(at.exception), 0, f"Verify snapshot exception: {at.exception}")
        self.assertTrue(
            any("Merkle integrity verified" in s.value for s in at.success),
            f"Expected snapshot verification success banner, got {[s.value for s in at.success]}",
        )
        self.assertTrue(any(m.label == "Merkle Integrity" and m.value == "VALID" for m in at.metric))

        # 2. Mutate raw source file of drifting-anchor.md to induce source SHA-256 drift, then demote stale via UI
        drifted_src = self.vault_dir / "0-raw" / "src-drifting-anchor.md"
        drifted_src.write_text("Mutated source bytes invalidating prior byte-offset hash.\n", encoding="utf-8")

        at.text_input(key="radar_demote_actor").set_value("audit_owner")
        at.checkbox(key="radar_demote_confirm").set_value(True)
        at.run()
        at.button(key="btn_demote_stale").click()
        at.run()
        self.assertEqual(len(at.exception), 0, f"Demote stale exception: {at.exception}")
        self.assertTrue(any("demoted 1 stale approval" in s.value for s in at.success))
        self.assertTrue(any(m.label == "Demoted to Pending" and str(m.value) == "1" for m in at.metric))

        self.assertEqual(parse_atom_record(str(stale_path))["review_status"], "pending")
        self.assertEqual(parse_atom_record(str(healthy_path))["review_status"], "accepted")

    def test_host_policy_secret_safety_and_single_doc_import(self) -> None:
        os.environ["CUSTOM_LOCAL_TEST_KEY"] = "sk-super-secret-never-render-value-999999"
        self.addCleanup(lambda: os.environ.pop("CUSTOM_LOCAL_TEST_KEY", None))

        at = self._new_unlocked_app()
        at.sidebar.radio[0].set_value("🧬 Host API, Quarantine & Fitness")
        at.run()

        at.selectbox(key="host_policy_select").set_value("owner_custom_local")
        at.run()
        at.checkbox(key="rp_owner_custom_local").set_value(True)
        at.checkbox(key="fs_owner_custom_local").set_value(True)
        at.text_input(key="ke_owner_custom_local").set_value("CUSTOM_LOCAL_TEST_KEY")
        at.run()
        at.button(key="save_owner_custom_local").click()
        at.run()
        self.assertEqual(len(at.exception), 0)
        self.assertTrue(any("Updated policy for `owner_custom_local`" in s.value for s in at.success))

        # Ensure the raw secret value is NEVER rendered anywhere in captions/markdown/text/code
        rendered_blobs = [
            *(c.value for c in at.caption),
            *(m.value for m in at.markdown),
            *(t.value for t in at.text),
            *(c.value for c in at.code),
        ]
        for blob in rendered_blobs:
            self.assertNotIn("sk-super-secret-never-render-value-999999", blob)
        self.assertTrue(any("SET ✅" in c.value for c in at.caption))

        # Test single-document CSV import in Working Notes & Import
        sample_csv = self.temp_root / "benchmarks.csv"
        sample_csv.write_text("metric,score\nrecall_at_5,0.98\n", encoding="utf-8")

        at.sidebar.radio[0].set_value("📝 Working Notes & Import")
        at.run()
        at.text_input(key="imp_single_file_path").set_value(str(sample_csv))
        at.button(key="btn_single_doc_import").click()
        at.run()
        self.assertEqual(len(at.exception), 0, f"Single doc import exception: {at.exception}")
        self.assertTrue(any("Imported `benchmarks.csv`" in s.value for s in at.success))


if __name__ == "__main__":
    unittest.main()
