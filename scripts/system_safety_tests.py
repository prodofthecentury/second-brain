"""Focused regression checks for the Second Brain safety boundaries."""

from __future__ import annotations

import unittest
from pathlib import Path
import hashlib
import re
import tempfile
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class EvaluationDatasetTests(unittest.TestCase):
    def _dataset(self, atoms_dir: Path) -> dict:
        atom = atoms_dir / "approved.md"
        atom.write_text("approved revision", encoding="utf-8")
        revision = hashlib.sha256(atom.read_bytes()).hexdigest()
        return {
            "schema_version": 2,
            "dataset_id": "unit-test",
            "dataset_version": "1.0.0",
            "status": "owner_approved",
            "purpose": "Test the dataset contract.",
            "owner_approval": {"actor": "owner", "date": "2026-10-27", "scope": "test labels"},
            "queries": [
                {
                    "id": "positive",
                    "query": "What is known?",
                    "category": "paraphrase",
                    "split": "regression",
                    "project_scope": None,
                    "answerability": "answerable",
                    "answer_mode": "standard",
                    "expected_atom_ids": ["approved.md"],
                    "acceptable_atom_ids": [],
                    "candidate_relevance": ["approved.md"],
                    "expected_claims": [{"text": "A supported claim.", "supported_by": ["approved.md"]}],
                    "expected_atom_revisions": [{"atom_id": "approved.md", "revision_sha256": revision}],
                    "expected_behavior": "answer_with_evidence",
                    "trap_reason": None,
                }
            ],
        }

    def test_versioned_answerable_case_requires_current_approved_atom_revision(self):
        from evaluation_dataset import validate_dataset

        with tempfile.TemporaryDirectory() as directory:
            atoms_dir = Path(directory)
            dataset = self._dataset(atoms_dir)
            validated = validate_dataset(dataset, atoms_dir=atoms_dir, approval_check=lambda _path: True)
        self.assertEqual(validated["dataset_version"], "1.0.0")

    def test_dataset_rejects_an_atom_revision_hash_that_does_not_match_current_file(self):
        from evaluation_dataset import EvaluationDatasetError, validate_dataset

        with tempfile.TemporaryDirectory() as directory:
            atoms_dir = Path(directory)
            dataset = self._dataset(atoms_dir)
            dataset["queries"][0]["expected_atom_revisions"][0]["revision_sha256"] = "0" * 64
            with self.assertRaises(EvaluationDatasetError):
                validate_dataset(dataset, atoms_dir=atoms_dir, approval_check=lambda _path: True)

    def test_dataset_rejects_an_unapproved_answer_target(self):
        from evaluation_dataset import EvaluationDatasetError, validate_dataset

        with tempfile.TemporaryDirectory() as directory:
            atoms_dir = Path(directory)
            dataset = self._dataset(atoms_dir)
            with self.assertRaises(EvaluationDatasetError):
                validate_dataset(dataset, atoms_dir=atoms_dir, approval_check=lambda _path: False)

    def test_dataset_rejects_atom_path_traversal_outside_atom_directory(self):
        from evaluation_dataset import EvaluationDatasetError, validate_dataset

        with tempfile.TemporaryDirectory() as directory:
            atoms_dir = Path(directory)
            dataset = self._dataset(atoms_dir)
            case = dataset["queries"][0]
            case.update({
                "answerability": "exploratory",
                "answer_mode": "candidate_retrieval",
                "expected_atom_ids": [],
                "candidate_relevance": ["../external.md"],
                "expected_atom_revisions": [],
                "expected_claims": [],
                "expected_behavior": "not_scored",
            })
            with self.assertRaises(EvaluationDatasetError):
                validate_dataset(dataset, atoms_dir=atoms_dir, approval_check=lambda _path: False)

    def test_exploratory_candidate_may_reference_pending_atom_without_approval(self):
        from evaluation_dataset import validate_dataset

        with tempfile.TemporaryDirectory() as directory:
            atoms_dir = Path(directory)
            dataset = self._dataset(atoms_dir)
            pending = atoms_dir / "pending.md"
            pending.write_text("pending candidate", encoding="utf-8")
            case = dataset["queries"][0]
            case.update({
                "answerability": "exploratory",
                "answer_mode": "candidate_retrieval",
                "expected_atom_ids": [],
                "candidate_relevance": ["pending.md"],
                "expected_atom_revisions": [],
                "expected_claims": [],
                "expected_behavior": "not_scored",
            })
            validated = validate_dataset(dataset, atoms_dir=atoms_dir, approval_check=lambda _path: False)
        self.assertEqual(validated["queries"][0]["answerability"], "exploratory")

    def test_dataset_rejects_an_atom_path_outside_atom_directory(self):
        from evaluation_dataset import EvaluationDatasetError, validate_dataset

        with tempfile.TemporaryDirectory() as directory:
            atoms_dir = Path(directory)
            dataset = self._dataset(atoms_dir)
            dataset["queries"][0]["expected_atom_ids"] = ["../outside.md"]
            with self.assertRaises(EvaluationDatasetError):
                validate_dataset(dataset, atoms_dir=atoms_dir, approval_check=lambda _path: True)

    def test_no_answer_case_requires_a_reason_and_cannot_claim_expected_atoms(self):
        from evaluation_dataset import EvaluationDatasetError, validate_dataset

        with tempfile.TemporaryDirectory() as directory:
            atoms_dir = Path(directory)
            dataset = self._dataset(atoms_dir)
            trap = dict(dataset["queries"][0])
            trap.update({
                "id": "trap",
                "answerability": "unanswerable",
                "expected_atom_ids": [],
                "candidate_relevance": [],
                "expected_claims": [],
                "expected_atom_revisions": [],
                "expected_behavior": "abstain",
                "trap_reason": None,
            })
            dataset["queries"].append(trap)
            with self.assertRaises(EvaluationDatasetError):
                validate_dataset(dataset, atoms_dir=atoms_dir, approval_check=lambda _path: True)

    def test_preference_case_with_expected_atoms_enforces_sha256_revision_and_approval(self):
        from evaluation_dataset import EvaluationDatasetError, validate_dataset

        with tempfile.TemporaryDirectory() as directory:
            atoms_dir = Path(directory)
            dataset = self._dataset(atoms_dir)
            pref_atom = atoms_dir / "indent-pref.md"
            pref_atom.write_text("owner prefers 4 spaces", encoding="utf-8")
            pref_sha = hashlib.sha256(pref_atom.read_bytes()).hexdigest()

            pref_case = {
                "id": "pref-scored",
                "query": "What indentation style does the owner prefer?",
                "category": "preference",
                "split": "regression",
                "project_scope": None,
                "answerability": "preference",
                "answer_mode": "preference",
                "expected_atom_ids": ["indent-pref.md"],
                "acceptable_atom_ids": [],
                "candidate_relevance": ["indent-pref.md"],
                "expected_claims": [{"text": "Use 4-space indentation.", "supported_by": ["indent-pref.md"]}],
                "expected_atom_revisions": [{"atom_id": "indent-pref.md", "revision_sha256": pref_sha}],
                "expected_behavior": "preference_recall",
                "trap_reason": None,
            }
            dataset["queries"] = [pref_case]

            validated = validate_dataset(dataset, atoms_dir=atoms_dir, approval_check=lambda _path: True)
            self.assertEqual(validated["queries"][0]["answerability"], "preference")

            # Reject unapproved preference target atom
            with self.assertRaisesRegex(EvaluationDatasetError, "not currently owner-approved"):
                validate_dataset(dataset, atoms_dir=atoms_dir, approval_check=lambda _path: False)

            # Reject mismatched SHA-256 revision on preference target atom
            bad_sha_dataset = {
                **dataset,
                "queries": [{
                    **pref_case,
                    "expected_atom_revisions": [{"atom_id": "indent-pref.md", "revision_sha256": "0" * 64}],
                }],
            }
            with self.assertRaisesRegex(EvaluationDatasetError, "revision hash is stale"):
                validate_dataset(bad_sha_dataset, atoms_dir=atoms_dir, approval_check=lambda _path: True)

            # Reject missing revision metadata when expected_atom_ids is non-empty
            missing_rev_dataset = {
                **dataset,
                "queries": [{**pref_case, "expected_atom_revisions": []}],
            }
            with self.assertRaisesRegex(EvaluationDatasetError, "revision metadata must cover"):
                validate_dataset(missing_rev_dataset, atoms_dir=atoms_dir, approval_check=lambda _path: True)


class AnswerGroundingTests(unittest.TestCase):
    def _atom(self, atoms_dir: Path, *, approve: bool = True) -> tuple[Path, str]:
        from evidence import find_evidence_anchor

        brain_dir = atoms_dir.parent
        source_path = brain_dir / "0-raw" / "source.md"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text("Structured logs should use JSON and request IDs.\n", encoding="utf-8")
        content = """---
id: approved
sources:
  - 0-raw/source.md
review_status: accepted
claim_state: human_reviewed
citation_integrity: missing
---

## Claim
Logs should be structured as JSON.

## Evidence
Structured logs should use JSON and request IDs.
"""
        path = atoms_dir / "approved.md"
        atoms_dir.mkdir(parents=True, exist_ok=True)
        parsed = content.split("---", 2)
        anchor = find_evidence_anchor(content, str(brain_dir))
        frontmatter = parsed[1]
        for key, value in anchor.items():
            if isinstance(value, str):
                frontmatter += f"{key}: {value}\n"
            else:
                frontmatter += f"{key}: {value}\n"
        frontmatter = frontmatter.replace("citation_integrity: missing", "citation_integrity: anchored")
        content = f"---{frontmatter}---{parsed[2]}"
        path.write_text(content, encoding="utf-8")
        if approve:
            from atom_mutator import apply_review_transition

            apply_review_transition(
                action="approve",
                atom_path=str(path),
                expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                actor="system-safety-test",
                decision_log_path=str(brain_dir / "0-raw" / "archive" / "decision-record.jsonl"),
                brain_dir=str(brain_dir),
                atoms_dir=str(atoms_dir),
            )
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def _draft(self, revision: str) -> str:
        import json

        return json.dumps({
            "claims": ["Logs should be structured as JSON."],
        })

    def test_grounded_answer_renders_only_exact_approved_claim_and_program_built_citation(self):
        from answer_policy import validate_standard_answer

        with tempfile.TemporaryDirectory() as directory:
            atoms_dir = Path(directory) / "brain" / "2-atoms"
            atoms_dir.mkdir(parents=True)
            path, revision = self._atom(atoms_dir)
            atoms_dir = path.parent
            revision = hashlib.sha256(path.read_bytes()).hexdigest()
            result = validate_standard_answer(
                self._draft(revision),
                [{"filename": path.name, "content": path.read_text(encoding="utf-8")}],
                atoms_dir=atoms_dir,
            )
        self.assertTrue(result.answerable)
        self.assertIn("Logs should be structured as JSON.", result.text)
        self.assertIn("[approved.md]", result.text)
        self.assertIn("Structured logs should use JSON and request IDs.", result.text)

    def test_grounding_rejects_a_plausible_but_unsupported_paraphrase(self):
        from answer_policy import validate_standard_answer

        with tempfile.TemporaryDirectory() as directory:
            atoms_dir = Path(directory) / "brain" / "2-atoms"
            atoms_dir.mkdir(parents=True)
            path, revision = self._atom(atoms_dir)
            draft = self._draft(revision).replace(
                "Logs should be structured as JSON.",
                "JSON logging always prevents security incidents.",
            )
            result = validate_standard_answer(
                draft,
                [{"filename": path.name, "content": path.read_text(encoding="utf-8")}],
                atoms_dir=atoms_dir,
            )
        self.assertFalse(result.answerable)
        self.assertIn("does not support a claim", result.text.casefold())

    def test_grounding_rejects_a_claim_that_is_not_in_retrieved_memory(self):
        from answer_policy import validate_standard_answer

        with tempfile.TemporaryDirectory() as directory:
            atoms_dir = Path(directory) / "brain" / "2-atoms"
            atoms_dir.mkdir(parents=True)
            path, revision = self._atom(atoms_dir)
            draft = self._draft(revision).replace(
                "Logs should be structured as JSON.",
                "Other files contain sensitive facts.",
            )
            result = validate_standard_answer(
                draft,
                [{"filename": path.name, "content": path.read_text(encoding="utf-8")}],
                atoms_dir=atoms_dir,
            )
        self.assertFalse(result.answerable)

    def test_grounding_rejects_stale_atom_revision_and_revoked_approval(self):
        from answer_policy import validate_standard_answer

        with tempfile.TemporaryDirectory() as directory:
            atoms_dir = Path(directory) / "brain" / "2-atoms"
            atoms_dir.mkdir(parents=True)
            path, revision = self._atom(atoms_dir)
            retrieved_content = path.read_text(encoding="utf-8")
            stale_retrieval = retrieved_content + "\nChanged after retrieval.\n"
            stale_result = validate_standard_answer(
                self._draft(revision),
                [{"filename": path.name, "content": stale_retrieval}],
                atoms_dir=atoms_dir,
            )
            from atom_mutator import apply_review_transition
            apply_review_transition(
                action="demote", atom_path=str(path), expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                actor="system-safety-test", decision_log_path=str(atoms_dir.parent / "0-raw" / "archive" / "decision-record.jsonl"),
                brain_dir=str(atoms_dir.parent), atoms_dir=str(atoms_dir),
            )
            revoked_result = validate_standard_answer(
                self._draft(revision),
                [{"filename": path.name, "content": retrieved_content}],
                atoms_dir=atoms_dir,
            )
        self.assertFalse(stale_result.answerable)
        self.assertFalse(revoked_result.answerable)

    def test_grounding_rejects_model_supplied_citation_fields(self):
        from answer_policy import validate_standard_answer

        with tempfile.TemporaryDirectory() as directory:
            atoms_dir = Path(directory) / "brain" / "2-atoms"
            atoms_dir.mkdir(parents=True)
            path, revision = self._atom(atoms_dir)
            draft = self._draft(revision).replace(
                '"Logs should be structured as JSON."',
                '{"text": "Logs should be structured as JSON.", "atom_id": "approved.md", "quote": "forged"}',
            )
            result = validate_standard_answer(
                draft,
                [{"filename": path.name, "content": path.read_text(encoding="utf-8")}],
                atoms_dir=atoms_dir,
            )
        self.assertFalse(result.answerable)

    def test_grounding_rejects_source_changed_after_atom_approval(self):
        from answer_policy import validate_standard_answer

        with tempfile.TemporaryDirectory() as directory:
            atoms_dir = Path(directory) / "brain" / "2-atoms"
            atoms_dir.mkdir(parents=True)
            path, revision = self._atom(atoms_dir)
            retrieved_content = path.read_text(encoding="utf-8")
            (atoms_dir.parent / "0-raw" / "source.md").write_text(
                "Changed source content after approval.\n", encoding="utf-8"
            )
            result = validate_standard_answer(
                self._draft(revision),
                [{"filename": path.name, "content": retrieved_content}],
                atoms_dir=atoms_dir,
            )
        self.assertFalse(result.answerable)

    def test_standard_prompt_rejects_unimplemented_research_mode(self):
        from answer_policy import AnswerRequest, build_grounding_prompt

        with self.assertRaises(ValueError):
            build_grounding_prompt(AnswerRequest(query="question", mode="research"), [])

    def test_grounding_prompt_lists_only_exact_claim_sections_as_allowed_output(self):
        from answer_policy import ANSWER_DRAFT_SCHEMA, AnswerRequest, build_grounding_prompt

        atom = {
            "filename": "approved.md",
            "content": "---\nid: approved\n---\n\n## Claim\nExact claim.\n\n## Evidence\nDifferent source wording.\n",
        }
        prompt = build_grounding_prompt(AnswerRequest(query="question"), [atom])
        self.assertIn('["Exact claim."]', prompt)
        self.assertEqual(ANSWER_DRAFT_SCHEMA["properties"]["claims"]["items"]["type"], "string")


class AnswerEvaluationTests(unittest.TestCase):
    def test_scores_expected_claim_coverage_and_trap_leakage_separately(self):
        from evaluate_answers import score_evaluation

        corpus = {"queries": [
            {
                "id": "positive", "answerability": "answerable",
                "expected_claims": [{"text": "Supported claim."}],
            },
            {
                "id": "trap", "answerability": "unanswerable",
                "expected_claims": [],
            },
        ]}
        observations = [
            {"case_id": "positive", "claims": ["Supported claim."], "abstained": False},
            {"case_id": "positive", "claims": ["Unexpected claim."], "abstained": False},
            {"case_id": "trap", "claims": ["Leaked claim."], "abstained": False},
            {"case_id": "trap", "claims": [], "abstained": True},
        ]
        metrics = score_evaluation(corpus, observations)
        self.assertEqual(metrics["answerable_run_count"], 2)
        self.assertEqual(metrics["positive_expected_claim_coverage"], 0.5)
        self.assertEqual(metrics["unexpected_displayed_claim_count"], 1)
        self.assertEqual(metrics["trap_claim_leakage_rate"], 0.5)

    def test_quality_gate_fails_on_missed_positive_claims_or_any_trap_leak(self):
        from evaluate_answers import evaluate_quality_gate

        passing = {
            "answerable_run_count": 2,
            "positive_complete_answer_rate": 1.0,
            "unexpected_displayed_claim_count": 0,
            "trap_run_count": 2,
            "trap_claim_leak_count": 0,
            "validated_claim_citation_rate": 1.0,
        }
        self.assertTrue(evaluate_quality_gate(passing)["passed"])
        failing = {**passing, "positive_complete_answer_rate": 0.5, "trap_claim_leak_count": 1}
        result = evaluate_quality_gate(failing)
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["failures"]), 2)

    def test_rejects_observations_for_exploratory_or_unknown_cases(self):
        from evaluate_answers import EvaluationRunError, score_evaluation

        corpus = {"queries": [{
            "id": "explore", "answerability": "exploratory", "expected_claims": [],
        }]}
        with self.assertRaises(EvaluationRunError):
            score_evaluation(corpus, [{"case_id": "explore", "claims": [], "abstained": True}])


class SourceAuditTests(unittest.TestCase):
    def _approved_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        atoms_dir = root / "brain" / "2-atoms"
        atoms_dir.mkdir(parents=True)
        path, _ = AnswerGroundingTests()._atom(atoms_dir)
        brain_dir = atoms_dir.parent
        return path, atoms_dir, brain_dir / "0-raw" / "archive" / "decision-record.jsonl"

    def test_source_audit_reports_changed_evidence_without_mutating(self):
        from audit_sources import audit_sources

        with tempfile.TemporaryDirectory() as directory:
            path, atoms_dir, decision_log = self._approved_fixture(Path(directory))
            source_path = atoms_dir.parent / "0-raw" / "source.md"
            source_path.write_text("The reviewed evidence source has changed.\n", encoding="utf-8")
            report = audit_sources(
                atoms_dir=atoms_dir, brain_dir=atoms_dir.parent, decision_log_path=decision_log,
            )
            content = path.read_text(encoding="utf-8")
        self.assertEqual(report[0]["anchor_status"], "stale_or_unverifiable")
        self.assertFalse(report[0]["approval_current"])
        self.assertEqual(report[0]["action"], "review_required")
        self.assertEqual(report[0]["source_status"], "present")
        self.assertNotEqual(
            report[0]["stored_anchor"]["evidence_sha256"],
            report[0]["current_source_sha256"],
        )
        self.assertIn("review_status: accepted", content)

    def test_source_audit_opt_in_demotes_through_canonical_mutator(self):
        from audit_sources import audit_sources
        from atom_mutator import approved_revision_is_current

        with tempfile.TemporaryDirectory() as directory:
            path, atoms_dir, decision_log = self._approved_fixture(Path(directory))
            (atoms_dir.parent / "0-raw" / "source.md").write_text(
                "The reviewed evidence source has changed.\n", encoding="utf-8",
            )
            report = audit_sources(
                atoms_dir=atoms_dir, brain_dir=atoms_dir.parent, decision_log_path=decision_log,
                demote_stale=True, actor="source-audit-test",
            )
            content = path.read_text(encoding="utf-8")
            current = approved_revision_is_current(
                str(path), decision_log_path=str(decision_log),
                brain_dir=str(atoms_dir.parent), atoms_dir=str(atoms_dir),
            )
        self.assertEqual(report[0]["action"], "demoted")
        self.assertIn("review_status: pending", content)
        self.assertFalse(current)

    def test_source_audit_cli_check_returns_exit_code_one_on_tampered_source(self):
        from contextlib import redirect_stdout
        from io import StringIO
        import json
        import audit_sources

        with tempfile.TemporaryDirectory() as directory:
            path, atoms_dir, decision_log = self._approved_fixture(Path(directory))
            brain_dir = atoms_dir.parent

            clean_out = StringIO()
            with redirect_stdout(clean_out):
                clean_code = audit_sources.main([
                    "--brain-dir", str(brain_dir),
                    "--atoms-dir", str(atoms_dir),
                    "--decision-log", str(decision_log),
                    "--check",
                ])
            self.assertEqual(clean_code, 0)

            (brain_dir / "0-raw" / "source.md").write_text(
                "Tampered source bytes after owner approval.\n", encoding="utf-8",
            )
            tampered_out = StringIO()
            with redirect_stdout(tampered_out):
                tampered_code = audit_sources.main([
                    "--brain-dir", str(brain_dir),
                    "--atoms-dir", str(atoms_dir),
                    "--decision-log", str(decision_log),
                    "--check",
                ])
            payload = json.loads(tampered_out.getvalue())
            atom_after = path.read_text(encoding="utf-8")

        self.assertEqual(tampered_code, 1)
        self.assertEqual(payload[0]["action"], "review_required")
        self.assertFalse(payload[0]["approval_current"])
        self.assertIn("review_status: accepted", atom_after)


class DashboardBoundaryTests(unittest.TestCase):
    def test_dashboard_binds_only_to_loopback(self):
        config = (ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")
        launcher = (ROOT / "run_dashboard.ps1").read_text(encoding="utf-8")

        self.assertIn('address = "127.0.0.1"', config)
        self.assertIn("--server.address 127.0.0.1", launcher)

    def test_setup_never_initializes_or_commits_the_repository_automatically(self):
        setup = (ROOT / "setup.ps1").read_text(encoding="utf-8").lower()

        self.assertIsNone(re.search(r"(?im)^\s*git\s+(?:init|add|commit)\b", setup))
        self.assertIn("will not initialize, stage, or commit files", setup)

    def test_installed_dashboard_launcher_preserves_loopback_and_origin_controls(self):
        from unittest.mock import patch

        from scripts import cli

        with (
            patch("scripts.cli._capability", return_value=True),
            patch("scripts.dashboard_auth.load_password_record", return_value={}),
            patch("scripts.cli.subprocess.call", return_value=0) as launch,
        ):
            self.assertEqual(cli.launch_dashboard(), 0)

        command = launch.call_args.args[0]
        self.assertIn("--server.address=127.0.0.1", command)
        self.assertIn("--server.enableCORS=true", command)
        self.assertIn("--server.enableXsrfProtection=true", command)
        self.assertIn("--server.allowedHosts=127.0.0.1", command)
        self.assertIn("--server.corsAllowedOrigins=http://127.0.0.1:8501", command)

    def test_dashboard_streamlit_cors_xsrf_and_allowed_hosts_are_explicit(self):
        config = (ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")
        self.assertIn("enableCORS = true", config)
        self.assertIn("enableXsrfProtection = true", config)
        self.assertIn('allowedHosts = ["127.0.0.1"]', config)
        self.assertIn('corsAllowedOrigins = ["http://127.0.0.1:8501"]', config)

    def test_dashboard_requires_password_gate_before_vault_content(self):
        app = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
        gate = app.index("dashboard_authenticated = require_dashboard_login(st)")
        vault_count = app.index("counts = get_counts()")
        self.assertLess(gate, vault_count)
        self.assertIn("if not dashboard_authenticated:\n    st.stop()", app)

    def test_non_loopback_binding_and_cross_origin_penetration_rejected(self):
        import http.client
        import os
        import socket
        import subprocess
        import time
        from dashboard_auth import SESSION_COOKIE_POLICY, assert_loopback_address, format_session_cookie
        from mcp_server import assert_mcp_loopback_transport

        # 1. Explicit loopback assertions reject None, empty, 0.0.0.0, IPv6 wildcard, and LAN/public IPs
        self.assertEqual(assert_loopback_address("127.0.0.1"), "127.0.0.1")
        for missing_or_empty in (None, "", "   "):
            with self.assertRaisesRegex(PermissionError, "403 Forbidden"):
                assert_loopback_address(missing_or_empty)
        with self.assertRaisesRegex(PermissionError, "403 Forbidden"):
            assert_mcp_loopback_transport("")
        for forbidden in ("0.0.0.0", "::", "192.168.1.50", "10.0.0.5", "8.8.8.8"):
            with self.assertRaisesRegex(PermissionError, "403 Forbidden"):
                assert_loopback_address(forbidden)
            with self.assertRaisesRegex(PermissionError, "403 Forbidden"):
                assert_mcp_loopback_transport(forbidden)

        # 2. Session cookie flags strictly enforce HttpOnly and SameSite=Strict
        self.assertTrue(SESSION_COOKIE_POLICY["HttpOnly"])
        self.assertEqual(SESSION_COOKIE_POLICY["SameSite"], "Strict")
        cookie = format_session_cookie("deadbeef" * 4)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

        # 3. Live Tornado/Streamlit penetration probe on loopback ephemeral port (isolated vault/auth)
        with tempfile.TemporaryDirectory() as st_tmp:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_sock:
                probe_sock.bind(("127.0.0.1", 0))
                port = probe_sock.getsockname()[1]

            child_env = dict(os.environ)
            child_env["BRAIN_VAULT_DIR"] = str(Path(st_tmp) / "brain")
            child_env["BRAIN_DASHBOARD_AUTH_FILE"] = str(Path(st_tmp) / "auth.json")

            proc = subprocess.Popen(
                [
                    sys.executable, "-m", "streamlit", "run",
                    str(ROOT / "dashboard" / "app.py"),
                    "--server.headless=true",
                    "--server.address=127.0.0.1",
                    f"--server.port={port}",
                    "--server.enableCORS=true",
                    "--server.enableXsrfProtection=true",
                    "--server.xsrfCookieSameSite=strict",
                    "--server.allowedHosts=127.0.0.1",
                    f"--server.corsAllowedOrigins=http://127.0.0.1:{port}",
                    "--browser.gatherUsageStats=false",
                ],
                cwd=str(ROOT),
                env=child_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                ready = False
                deadline = time.time() + 20.0
                while time.time() < deadline:
                    try:
                        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1.0)
                        conn.request("GET", "/_stcore/health", headers={"Host": f"127.0.0.1:{port}"})
                        resp = conn.getresponse()
                        resp.read()
                        conn.close()
                        if resp.status == 200:
                            ready = True
                            break
                    except OSError:
                        time.sleep(0.25)
                self.assertTrue(ready, "Streamlit loopback listener failed to become healthy within 20s")

                # 3a. Cross-origin WebSocket upgrade to /_stcore/stream must fail with 403 Forbidden
                ws_conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3.0)
                ws_conn.request(
                    "GET",
                    "/_stcore/stream",
                    headers={
                        "Host": f"127.0.0.1:{port}",
                        "Origin": "https://evil-attacker.example",
                        "Connection": "Upgrade",
                        "Upgrade": "websocket",
                        "Sec-WebSocket-Version": "13",
                        "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                    },
                )
                ws_resp = ws_conn.getresponse()
                ws_resp.read()
                ws_conn.close()
                self.assertEqual(ws_resp.status, 403)

                # 3b. Spoofed external Host header WebSocket upgrade must fail with 403 Forbidden
                spoof_conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3.0)
                spoof_conn.request(
                    "GET",
                    "/_stcore/stream",
                    headers={
                        "Host": f"evil-host.example:{port}",
                        "Origin": f"http://evil-host.example:{port}",
                        "Connection": "Upgrade",
                        "Upgrade": "websocket",
                        "Sec-WebSocket-Version": "13",
                        "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                    },
                )
                spoof_resp = spoof_conn.getresponse()
                spoof_resp.read()
                spoof_conn.close()
                self.assertEqual(spoof_resp.status, 403)

                # 3c. Non-loopback LAN IP connection attempt must be refused immediately
                try:
                    lan_ip = socket.gethostbyname(socket.gethostname())
                except OSError:
                    lan_ip = "127.0.0.1"
                if lan_ip and not lan_ip.startswith("127."):
                    with self.assertRaises(OSError):
                        with socket.create_connection((lan_ip, port), timeout=1.5):
                            pass
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


class DashboardAuthTests(unittest.TestCase):
    @staticmethod
    def _make_fake_streamlit(*, submitted: bool = False, password: str = "", server_address: str | None = "127.0.0.1"):
        class Form:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False

        class FakeStreamlit:
            def __init__(self):
                self.session_state = {}
                self.submitted = submitted
                self.password = password
                self.sidebar = self
                self.errors = []
                self.warnings = []
                self.titles = []
            def get_option(self, name):
                return server_address if name == "server.address" else None
            def title(self, message): self.titles.append(message)
            def caption(self, *_args): pass
            def error(self, message): self.errors.append(message)
            def warning(self, message): self.warnings.append(message)
            def code(self, *_args, **_kwargs): pass
            def form(self, *_args, **_kwargs): return Form()
            def text_input(self, *_args, **_kwargs):
                self.session_state["brain_dashboard_password"] = self.password
                return self.password
            def form_submit_button(self, *_args, **_kwargs): return self.submitted
            def button(self, *_args, **_kwargs): return False
            def rerun(self): raise RuntimeError("rerun")

        return FakeStreamlit()

    def test_scrypt_verifier_accepts_only_matching_password_and_has_unique_salt(self):
        from dashboard_auth import build_password_record, verify_password
        first = build_password_record("a-long-owner-password")
        second = build_password_record("a-long-owner-password")
        self.assertTrue(verify_password("a-long-owner-password", first))
        self.assertFalse(verify_password("wrong-password", first))
        self.assertNotEqual(first["salt"], second["salt"])
        self.assertNotEqual(first["password_hash"], second["password_hash"])

    def test_auth_store_fails_closed_on_missing_or_malformed_data_and_can_be_reset(self):
        from dashboard_auth import load_password_record, save_password_record, build_password_record, verify_password
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private" / "dashboard-auth.json"
            self.assertIsNone(load_password_record(path))
            save_password_record(build_password_record("another-long-owner-password"), path)
            loaded = load_password_record(path)
            self.assertTrue(verify_password("another-long-owner-password", loaded))
            path.write_text("not json", encoding="utf-8")
            self.assertIsNone(load_password_record(path))

    def test_login_gate_stops_unauthenticated_and_unlocks_matching_session(self):
        import copy
        import time
        import dashboard_auth
        from dashboard_auth import (
            MAX_SESSION_FAILURES,
            SESSION_IDLE_SECONDS,
            build_password_record,
            require_dashboard_login,
            save_password_record,
        )

        saved_tracker = copy.deepcopy(dashboard_auth._RATE_LIMIT_TRACKER)
        try:
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "auth.json"
                save_password_record(build_password_record("test-password-long-enough"), path)
                unauthenticated = self._make_fake_streamlit()
                self.assertFalse(require_dashboard_login(unauthenticated, path))
                self.assertIn("Unlock Second Brain", unauthenticated.titles)

                wrong = self._make_fake_streamlit(submitted=True, password="wrong-password")
                with self.assertRaisesRegex(RuntimeError, "rerun"):
                    require_dashboard_login(wrong, path)
                self.assertNotIn("_brain_dashboard_auth_fingerprint", wrong.session_state)

                valid = self._make_fake_streamlit(submitted=True, password="test-password-long-enough")
                with self.assertRaisesRegex(RuntimeError, "rerun"):
                    require_dashboard_login(valid, path)
                self.assertIn("_brain_dashboard_auth_fingerprint", valid.session_state)
                valid.submitted = False
                self.assertTrue(require_dashboard_login(valid, path))
                valid.session_state["_brain_dashboard_last_activity"] = time.time() - SESSION_IDLE_SECONDS - 1
                self.assertFalse(require_dashboard_login(valid, path))
                self.assertNotIn("_brain_dashboard_auth_fingerprint", valid.session_state)

                attacker = self._make_fake_streamlit(submitted=True, password="wrong-password")
                for _ in range(MAX_SESSION_FAILURES):
                    with self.assertRaisesRegex(RuntimeError, "rerun"):
                        require_dashboard_login(attacker, path)
                self.assertIn("_brain_dashboard_locked_until", attacker.session_state)
                self.assertFalse(require_dashboard_login(attacker, path))
        finally:
            with dashboard_auth._RATE_LIMIT_LOCK:
                dashboard_auth._RATE_LIMIT_TRACKER.clear()
                dashboard_auth._RATE_LIMIT_TRACKER.update(saved_tracker)

    def test_login_gate_rejects_non_loopback_or_missing_server_address_and_rate_limits_across_sessions(self):
        import copy
        import dashboard_auth
        from dashboard_auth import (
            MAX_SESSION_FAILURES,
            assert_loopback_address,
            build_password_record,
            require_dashboard_login,
            save_password_record,
        )

        with self.assertRaisesRegex(PermissionError, "403 Forbidden"):
            assert_loopback_address(None)
        with self.assertRaisesRegex(PermissionError, "403 Forbidden"):
            assert_loopback_address("")

        saved_tracker = copy.deepcopy(dashboard_auth._RATE_LIMIT_TRACKER)
        try:
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "multi-session-auth.json"
                save_password_record(build_password_record("test-password-long-enough"), path)

                # Non-loopback or missing server.address fails closed with 403 Forbidden
                for bad_address in ("0.0.0.0", None):
                    bad_st = self._make_fake_streamlit(
                        submitted=True,
                        password="test-password-long-enough",
                        server_address=bad_address,
                    )
                    self.assertFalse(require_dashboard_login(bad_st, path))
                    self.assertTrue(any("403 Forbidden" in msg for msg in bad_st.errors))
                    self.assertNotIn("_brain_dashboard_auth_fingerprint", bad_st.session_state)

                # 5 distinct FakeStreamlit instances (each fresh session_state={}) lock out the 6th fresh session
                for attempt in range(MAX_SESSION_FAILURES):
                    fresh_attacker = self._make_fake_streamlit(
                        submitted=True,
                        password=f"wrong-password-{attempt}",
                    )
                    self.assertEqual(fresh_attacker.session_state, {})
                    with self.assertRaisesRegex(RuntimeError, "rerun"):
                        require_dashboard_login(fresh_attacker, path)

                sixth_fresh_session = self._make_fake_streamlit(
                    submitted=True,
                    password="test-password-long-enough",
                )
                self.assertEqual(sixth_fresh_session.session_state, {})
                self.assertFalse(require_dashboard_login(sixth_fresh_session, path))
                self.assertTrue(any("Too many failed attempts" in msg for msg in sixth_fresh_session.warnings))
                self.assertNotIn("_brain_dashboard_auth_fingerprint", sixth_fresh_session.session_state)
        finally:
            with dashboard_auth._RATE_LIMIT_LOCK:
                dashboard_auth._RATE_LIMIT_TRACKER.clear()
                dashboard_auth._RATE_LIMIT_TRACKER.update(saved_tracker)


class MCPBoundaryTests(unittest.TestCase):
    @staticmethod
    def _write_candidate_atom(path: Path, atom_id: str, source_rel: str, claim: str, evidence: str) -> None:
        path.write_text(
            f"---\nid: {atom_id}\ntype: atom\nmemory_type: semantic\nsources:\n  - {source_rel}\n"
            "links: []\ncontexts: []\nsupersedes: []\nsuperseded_by: []\ncontested_by: []\n"
            "origin: user_local\ncitation_integrity: missing\nclaim_state: candidate\n"
            "epistemic_label: claim\nreview_status: pending\n---\n\n"
            f"## Claim\n{claim}\n\n## Evidence\n{evidence}\n",
            encoding="utf-8",
        )

    def _isolated_mcp_env(self, *, configure_token: str | None = "secret-mcp-bearer-token-123456"):
        from contextlib import ExitStack, contextmanager
        import os
        from unittest.mock import patch
        import config
        import mcp_server
        import recall
        from atom_mutator import apply_atom_merge, apply_review_transition
        from lexical_store import rebuild_lexical_index

        @contextmanager
        def _ctx():
            with tempfile.TemporaryDirectory() as tmp_root, ExitStack() as stack:
                root = Path(tmp_root)
                brain_dir = root / "brain"
                raw_dir = brain_dir / "0-raw"
                inbox_dir = raw_dir / "inbox"
                desk_dir = brain_dir / "1-desk"
                atoms_dir = brain_dir / "2-atoms"
                threads_dir = brain_dir / "3-threads"
                experiences_dir = brain_dir / "4-experiences"
                sources_dir = brain_dir / "sources"
                briefings_dir = brain_dir / "briefings"
                chroma_dir = brain_dir / ".chroma"
                sidecar_dir = brain_dir / ".sidecar"
                for d in (
                    raw_dir, inbox_dir, desk_dir, atoms_dir, threads_dir, experiences_dir,
                    sources_dir, briefings_dir, chroma_dir, sidecar_dir,
                ):
                    d.mkdir(parents=True, exist_ok=True)

                decision_log = raw_dir / "archive" / "decision-record.jsonl"
                audit_path = raw_dir / "archive" / "mcp-audit.jsonl"
                lexical_index_path = sidecar_dir / "lexical.sqlite3"
                token_file = root / "security" / "mcp-token"

                # 1. Seed owner-approved current atom (structured-logging.md)
                log_src = raw_dir / "logging-source.md"
                log_quote = "Logs should be structured (JSON) with correlation request IDs."
                log_src.write_text(log_quote + "\n", encoding="utf-8")
                approved_atom = atoms_dir / "structured-logging.md"
                self._write_candidate_atom(
                    approved_atom, "structured-logging", "0-raw/logging-source.md", log_quote, log_quote
                )
                apply_review_transition(
                    action="approve",
                    atom_path=str(approved_atom),
                    expected_sha256=hashlib.sha256(approved_atom.read_bytes()).hexdigest(),
                    actor="mcp-test-owner",
                    decision_log_path=str(decision_log),
                    history_dir=str(atoms_dir / ".history"),
                    brain_dir=str(brain_dir),
                    atoms_dir=str(atoms_dir),
                )

                # 2. Seed pending candidate atom (observability-o11y.md)
                pending_atom = atoms_dir / "observability-o11y.md"
                self._write_candidate_atom(
                    pending_atom,
                    "observability-o11y",
                    "0-raw/logging-source.md",
                    "Pending unapproved observability note.",
                    log_quote,
                )

                # 3. Seed superseded atom via canonical merge (superseded-logging.md -> survivor-logging.md)
                sup_src = raw_dir / "superseded-source.md"
                sup_quote = "Legacy plaintext logs were superseded by structured JSON logs."
                sup_src.write_text(sup_quote + "\n", encoding="utf-8")
                superseded_atom = atoms_dir / "superseded-logging.md"
                survivor_atom = atoms_dir / "survivor-logging.md"
                self._write_candidate_atom(
                    superseded_atom, "superseded-logging", "0-raw/superseded-source.md", sup_quote, sup_quote
                )
                self._write_candidate_atom(
                    survivor_atom, "survivor-logging", "0-raw/logging-source.md", log_quote, log_quote
                )
                apply_review_transition(
                    action="approve",
                    atom_path=str(superseded_atom),
                    expected_sha256=hashlib.sha256(superseded_atom.read_bytes()).hexdigest(),
                    actor="mcp-test-owner",
                    decision_log_path=str(decision_log),
                    history_dir=str(atoms_dir / ".history"),
                    brain_dir=str(brain_dir),
                    atoms_dir=str(atoms_dir),
                )
                apply_atom_merge(
                    survivor_path=str(survivor_atom),
                    survivor_expected_sha256=hashlib.sha256(survivor_atom.read_bytes()).hexdigest(),
                    superseded_path=str(superseded_atom),
                    superseded_expected_sha256=hashlib.sha256(superseded_atom.read_bytes()).hexdigest(),
                    actor="mcp-test-owner",
                    rationale="Superseded in isolated MCP fixture.",
                    decision_log_path=str(decision_log),
                    history_dir=str(atoms_dir / ".history"),
                    brain_dir=str(brain_dir),
                    atoms_dir=str(atoms_dir),
                )

                # 4. Seed tampered-source atom (approved, then source modified on disk)
                tamp_src = raw_dir / "tampered-source.md"
                tamp_quote = "Original verified evidence sentence before tampering."
                tamp_src.write_text(tamp_quote + "\n", encoding="utf-8")
                tampered_atom = atoms_dir / "tampered-logging.md"
                self._write_candidate_atom(
                    tampered_atom, "tampered-logging", "0-raw/tampered-source.md", tamp_quote, tamp_quote
                )
                apply_review_transition(
                    action="approve",
                    atom_path=str(tampered_atom),
                    expected_sha256=hashlib.sha256(tampered_atom.read_bytes()).hexdigest(),
                    actor="mcp-test-owner",
                    decision_log_path=str(decision_log),
                    history_dir=str(atoms_dir / ".history"),
                    brain_dir=str(brain_dir),
                    atoms_dir=str(atoms_dir),
                )
                tamp_src.write_text("Tampered source content after owner approval.\n", encoding="utf-8")

                rebuild_lexical_index(str(atoms_dir), str(lexical_index_path))

                env_updates = {
                    "BRAIN_VAULT_DIR": str(brain_dir),
                    "BRAIN_MCP_TOKEN_FILE": str(token_file),
                }
                if configure_token is not None:
                    env_updates["BRAIN_MCP_BEARER_TOKEN"] = configure_token
                stack.enter_context(patch.dict(os.environ, env_updates, clear=False))
                if configure_token is None:
                    os.environ.pop("BRAIN_MCP_BEARER_TOKEN", None)
                os.environ.pop("BRAIN_MCP_ALLOWED_TOOLS", None)
                os.environ.pop("BRAIN_MCP_BIND_ADDRESS", None)
                os.environ.pop("BRAIN_MCP_TRUSTED_STDIO", None)
                os.environ.pop("BRAIN_MCP_CLIENT_TOKEN", None)

                import notes_store

                playbooks_dir = brain_dir / "5-playbooks"
                notes_dir = brain_dir / "notes"
                playbooks_dir.mkdir(parents=True, exist_ok=True)
                notes_dir.mkdir(parents=True, exist_ok=True)
                isolated_note_paths = notes_store.NotePaths.for_brain(str(brain_dir))

                for mod in (config, mcp_server):
                    stack.enter_context(patch.object(mod, "BRAIN_DIR", str(brain_dir), create=True))
                    stack.enter_context(patch.object(mod, "RAW_DIR", str(raw_dir), create=True))
                    stack.enter_context(patch.object(mod, "INBOX_DIR", str(inbox_dir), create=True))
                    stack.enter_context(patch.object(mod, "ATOMS_DIR", str(atoms_dir), create=True))
                    stack.enter_context(patch.object(mod, "THREADS_DIR", str(threads_dir), create=True))
                    stack.enter_context(patch.object(mod, "BRIEFINGS_DIR", str(briefings_dir), create=True))
                stack.enter_context(patch.object(config, "DESK_DIR", str(desk_dir)))
                stack.enter_context(patch.object(config, "EXPERIENCES_DIR", str(experiences_dir)))
                stack.enter_context(patch.object(config, "PLAYBOOKS_DIR", str(playbooks_dir), create=True))
                stack.enter_context(patch.object(config, "NOTES_DIR", str(notes_dir), create=True))
                stack.enter_context(
                    patch.object(config, "NOTE_SNAPSHOTS_DIR", isolated_note_paths.snapshots_dir, create=True)
                )
                stack.enter_context(
                    patch.object(config, "NOTES_INDEX_PATH", isolated_note_paths.index_path, create=True)
                )
                stack.enter_context(patch.object(notes_store, "DEFAULT_PATHS", isolated_note_paths))
                stack.enter_context(patch.object(config, "SOURCES_DIR", str(sources_dir)))
                stack.enter_context(patch.object(config, "CHROMA_PATH", str(chroma_dir)))
                stack.enter_context(patch.object(config, "SIDECAR_DIR", str(sidecar_dir)))
                stack.enter_context(patch.object(config, "LEXICAL_INDEX_PATH", str(lexical_index_path)))
                stack.enter_context(
                    patch.object(config, "TELEMETRY_DB_PATH", str(sidecar_dir / "telemetry.sqlite3"))
                )
                stack.enter_context(
                    patch.object(config, "ARCHIVE_MANIFEST", str(raw_dir / "archive" / "manifest.jsonl"))
                )
                stack.enter_context(patch.object(mcp_server, "MCP_AUDIT_PATH", str(audit_path)))

                stack.enter_context(patch.object(recall, "BRAIN_DIR", str(brain_dir)))
                stack.enter_context(patch.object(recall, "RAW_DIR", str(raw_dir)))
                stack.enter_context(patch.object(recall, "ATOMS_DIR", str(atoms_dir)))
                stack.enter_context(patch.object(recall, "CHROMA_PATH", str(chroma_dir)))
                stack.enter_context(patch.object(recall, "LEXICAL_INDEX_PATH", str(lexical_index_path)))

                real_search_memory = recall.search_memory

                def isolated_search_memory(query, options=None, **kwargs):
                    kwargs.setdefault("atoms_dir", str(atoms_dir))
                    kwargs.setdefault("index_path", str(lexical_index_path))
                    kwargs.setdefault("chroma_path", str(chroma_dir))
                    return real_search_memory(query, options, **kwargs)

                stack.enter_context(patch.object(mcp_server, "search_memory", side_effect=isolated_search_memory))

                yield {
                    "brain_dir": brain_dir,
                    "raw_dir": raw_dir,
                    "inbox_dir": inbox_dir,
                    "atoms_dir": atoms_dir,
                    "audit_path": audit_path,
                    "token_file": token_file,
                    "token": configure_token,
                }

        return _ctx()

    def test_mcp_is_opt_in_bounded_and_does_not_enumerate_vault(self):
        import mcp_server

        implementation = (ROOT / "scripts" / "mcp_server.py").read_text(encoding="utf-8")
        readme_policy = (ROOT / "README.md").read_text(encoding="utf-8").lower()

        self.assertIn('os.environ.get("BRAIN_ENABLE_LOCAL_MCP") != "1"', implementation)
        self.assertIn("MAX_INBOX_CHARS = 100_000", implementation)
        self.assertNotIn('name="list_atoms"', implementation)
        self.assertNotIn('name="list_threads"', implementation)
        self.assertIn("return []", implementation)
        self.assertIn("do not register it with cloud-connected clients", readme_policy)

        vault_policy_path = ROOT / "brain" / "mcp-config.md"
        if vault_policy_path.is_file():
            vault_policy = vault_policy_path.read_text(encoding="utf-8").lower()
            self.assertIn("do not register it with", vault_policy)
            self.assertIn("cloud-connected clients", vault_policy)

        for tool in mcp_server.ALL_TOOLS:
            schema = getattr(tool, "input_schema", None) or tool.model_dump(by_alias=True).get("inputSchema", {})
            props = schema.get("properties", {})
            self.assertNotIn("bearer_token", props)
            self.assertNotIn("auth_token", props)
            self.assertNotIn("token", props)

    def test_mcp_audit_is_content_free_and_happens_before_tool_execution(self):
        from mcp_server import audit_mcp_use
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mcp-audit.jsonl"
            record = audit_mcp_use("tool:search_memory", str(path))
            stored = path.read_text(encoding="utf-8")
        self.assertEqual(record["event"], "tool:search_memory")
        self.assertEqual(record["transport"], "stdio")
        self.assertNotIn("query", record)
        self.assertNotIn("content", record)
        self.assertNotIn("private search phrase", stored)
        implementation = (ROOT / "scripts" / "mcp_server.py").read_text(encoding="utf-8")
        call_body = implementation.split("async def handle_call_tool", 1)[1].split("async def handle_list_resources", 1)[0]
        self.assertLess(call_body.index("audit_mcp_use("), call_body.index("ensure_dirs()"))

    def test_mcp_audit_rejects_unrecognized_event_names(self):
        from mcp_server import audit_mcp_use
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                audit_mcp_use("query:private search phrase", str(Path(temporary) / "audit.jsonl"))

    def test_mcp_sdk_v2_handlers_return_protocol_results_and_reject_audit_failure(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import patch

        import mcp_server
        from mcp import types

        test_token = "secret-mcp-bearer-token-123456"
        auth_ctx = SimpleNamespace(meta={"authorization": f"Bearer {test_token}"})

        async def scenario():
            with self._isolated_mcp_env(configure_token=test_token) as env:
                listed = await mcp_server.handle_list_tools(auth_ctx, None)
                self.assertIsInstance(listed, types.ListToolsResult)
                self.assertEqual(
                    {tool.name for tool in listed.tools},
                    {"search_memory", "get_atom", "add_to_inbox", "search_notes", "get_note", "remember"},
                )
                resources = await mcp_server.handle_list_resources(auth_ctx, None)
                self.assertEqual(resources.resources, [])
                with patch("mcp_server.audit_mcp_use", side_effect=OSError("audit unavailable")):
                    with self.assertRaises(OSError):
                        await mcp_server.handle_call_tool(
                            auth_ctx,
                            types.CallToolRequestParams(
                                name="add_to_inbox",
                                arguments={"content": "must not write"},
                                _meta={"authorization": f"Bearer {test_token}"},
                            ),
                        )
                self.assertEqual(list(env["inbox_dir"].glob("*.md")), [])

        asyncio.run(scenario())

    def test_mcp_bearer_token_authorization(self):
        import asyncio
        import mcp_server
        from mcp import types

        test_token = "secret-mcp-bearer-token-123456"

        async def run_auth_checks():
            # 1. Fail-closed by default when NO token is configured anywhere
            with self._isolated_mcp_env(configure_token=None) as unconf_env:
                unconf_call = await mcp_server.handle_call_tool(
                    None,
                    types.CallToolRequestParams(name="search_memory", arguments={"query": "structured JSON"}),
                )
                self.assertTrue(unconf_call.is_error)
                self.assertIn("Unauthorized", unconf_call.content[0].text)
                self.assertFalse(unconf_env["audit_path"].exists())

                # Truncated token file must also fail closed
                unconf_env["token_file"].parent.mkdir(parents=True, exist_ok=True)
                unconf_env["token_file"].write_text("short\n", encoding="utf-8")
                self.assertFalse(mcp_server.is_authorized())

            # 2. Configured token enforcement + pre-auth audit protection + _meta bearer header
            with self._isolated_mcp_env(configure_token=test_token) as auth_env:
                audit_path = auth_env["audit_path"]

                # Request without token fails BEFORE writing audit log
                no_auth_call = await mcp_server.handle_call_tool(
                    None,
                    types.CallToolRequestParams(name="search_memory", arguments={"query": "structured JSON"}),
                )
                self.assertTrue(no_auth_call.is_error)
                self.assertIn("Unauthorized", no_auth_call.content[0].text)
                self.assertFalse(audit_path.exists())

                # Request with wrong token fails BEFORE writing audit log
                wrong_auth_call = await mcp_server.handle_call_tool(
                    None,
                    types.CallToolRequestParams(
                        name="search_memory",
                        arguments={"query": "structured JSON"},
                        _meta={"authorization": "Bearer wrong-token-value"},
                    ),
                )
                self.assertTrue(wrong_auth_call.is_error)
                self.assertIn("Unauthorized", wrong_auth_call.content[0].text)
                self.assertFalse(audit_path.exists())

                # Request passing token in tool arguments without _meta MUST be rejected
                args_only_call = await mcp_server.handle_call_tool(
                    None,
                    types.CallToolRequestParams(
                        name="search_memory",
                        arguments={"query": "structured JSON", "bearer_token": test_token},
                    ),
                )
                self.assertTrue(args_only_call.is_error)
                self.assertIn("Unauthorized", args_only_call.content[0].text)
                self.assertFalse(audit_path.exists())

                # Request with valid token in _meta succeeds against isolated lexical index and records audit
                valid_auth_call = await mcp_server.handle_call_tool(
                    None,
                    types.CallToolRequestParams(
                        name="search_memory",
                        arguments={"query": "structured JSON correlation request"},
                        _meta={"authorization": f"Bearer {test_token}"},
                    ),
                )
                self.assertFalse(valid_auth_call.is_error)
                self.assertIn("structured-logging.md", valid_auth_call.content[0].text)
                self.assertTrue(audit_path.exists())

        asyncio.run(run_auth_checks())

    def test_mcp_granular_tool_permissions(self):
        import asyncio
        import os
        from types import SimpleNamespace
        from unittest.mock import patch
        import mcp_server
        from mcp import types

        test_token = "secret-mcp-bearer-token-123456"
        auth_ctx = SimpleNamespace(meta={"authorization": f"Bearer {test_token}"})

        async def run_permission_checks():
            with self._isolated_mcp_env(configure_token=test_token):
                # Restrict tools to only search_memory
                with patch.dict(os.environ, {"BRAIN_MCP_ALLOWED_TOOLS": "search_memory"}):
                    listed = await mcp_server.handle_list_tools(auth_ctx, None)
                    tool_names = {t.name for t in listed.tools}
                    self.assertEqual(tool_names, {"search_memory"})
                    self.assertNotIn("add_to_inbox", tool_names)

                    # Calling a forbidden tool returns permission error
                    denied_call = await mcp_server.handle_call_tool(
                        auth_ctx,
                        types.CallToolRequestParams(
                            name="add_to_inbox",
                            arguments={"content": "hello"},
                            _meta={"authorization": f"Bearer {test_token}"},
                        ),
                    )
                    self.assertTrue(denied_call.is_error)
                    self.assertIn("Permission denied", denied_call.content[0].text)

        asyncio.run(run_permission_checks())

    def test_mcp_inbox_path_traversal_and_size_rejection(self):
        import asyncio
        import mcp_server
        from mcp import types

        test_token = "secret-mcp-bearer-token-123456"

        async def run_ingestion_checks():
            with self._isolated_mcp_env(configure_token=test_token):
                # Path traversal attempt in arguments raises descriptive ValueError
                with self.assertRaisesRegex(ValueError, "Path traversal attempt detected"):
                    await mcp_server.handle_call_tool(
                        None,
                        types.CallToolRequestParams(
                            name="add_to_inbox",
                            arguments={"content": "malicious content", "source": "../../evil"},
                            _meta={"authorization": f"Bearer {test_token}"},
                        ),
                    )

                with self.assertRaisesRegex(ValueError, "Path traversal attempt detected"):
                    await mcp_server.handle_call_tool(
                        None,
                        types.CallToolRequestParams(
                            name="add_to_inbox",
                            arguments={"content": "malicious content", "filename": "../../../boot.ini"},
                            _meta={"authorization": f"Bearer {test_token}"},
                        ),
                    )

                # Oversized payload raises descriptive ValueError
                oversized_content = "x" * (mcp_server.MAX_INBOX_CHARS + 10)
                with self.assertRaisesRegex(ValueError, "Inbox content must contain"):
                    await mcp_server.handle_call_tool(
                        None,
                        types.CallToolRequestParams(
                            name="add_to_inbox",
                            arguments={"content": oversized_content},
                            _meta={"authorization": f"Bearer {test_token}"},
                        ),
                    )

                # Null byte injection raises descriptive ValueError
                with self.assertRaisesRegex(ValueError, "null bytes detected"):
                    await mcp_server.handle_call_tool(
                        None,
                        types.CallToolRequestParams(
                            name="add_to_inbox",
                            arguments={"content": "data with \x00 null byte"},
                            _meta={"authorization": f"Bearer {test_token}"},
                        ),
                    )

        asyncio.run(run_ingestion_checks())

    def test_mcp_get_atom_restricts_to_accepted_and_quarantines_inbox(self):
        import asyncio
        import mcp_server
        from mcp import types

        test_token = "secret-mcp-bearer-token-123456"

        async def run_atom_and_quarantine_checks():
            with self._isolated_mcp_env(configure_token=test_token) as env:
                meta = {"authorization": f"Bearer {test_token}"}

                # 1. Approved atom can be read
                approved_res = await mcp_server.handle_call_tool(
                    None,
                    types.CallToolRequestParams(
                        name="get_atom",
                        arguments={"atom_id": "structured-logging.md"},
                        _meta=meta,
                    ),
                )
                self.assertIn("Logs should be structured (JSON)", approved_res.content[0].text)

                # 2. Pending, superseded, and tampered-source atoms are blocked from external reading
                for blocked_id in ("observability-o11y.md", "superseded-logging.md", "tampered-logging.md"):
                    blocked_res = await mcp_server.handle_call_tool(
                        None,
                        types.CallToolRequestParams(
                            name="get_atom",
                            arguments={"atom_id": blocked_id},
                            _meta=meta,
                        ),
                    )
                    self.assertIn("not currently owner-approved", blocked_res.content[0].text)

                # 3. Path traversal in get_atom is rejected
                trav_res = await mcp_server.handle_call_tool(
                    None,
                    types.CallToolRequestParams(
                        name="get_atom",
                        arguments={"atom_id": "../../pyproject.toml"},
                        _meta=meta,
                    ),
                )
                self.assertIn("not found or invalid", trav_res.content[0].text)

                # 4. Valid add_to_inbox writes strictly to quarantined inbox (0-raw/inbox/) and never top-level 0-raw/
                existing_raw_md = set(env["raw_dir"].glob("*.md"))
                inbox_res = await mcp_server.handle_call_tool(
                    None,
                    types.CallToolRequestParams(
                        name="add_to_inbox",
                        arguments={
                            "content": "Quarantined capture test payload.",
                            "source": "unit_test",
                        },
                        _meta=meta,
                    ),
                )
                self.assertIn("quarantined inbox", inbox_res.content[0].text)
                self.assertIn("remains isolated in 0-raw/inbox/", inbox_res.content[0].text)
                inbox_files = list(env["inbox_dir"].glob("*.md"))
                new_raw_root_files = set(env["raw_dir"].glob("*.md")) - existing_raw_md
                self.assertEqual(len(inbox_files), 1)
                self.assertEqual(len(new_raw_root_files), 0)
                stored_text = inbox_files[0].read_text(encoding="utf-8")
                self.assertIn("<!-- second_brain_origin: mcp_local -->", stored_text)
                self.assertIn("Quarantined capture test payload.", stored_text)

        asyncio.run(run_atom_and_quarantine_checks())

    def test_mcp_read_resource_enforces_auth_approval_tamper_and_traversal_guards(self):
        import asyncio
        import mcp_server
        from mcp import types

        test_token = "secret-mcp-bearer-token-123456"

        async def run_resource_checks():
            with self._isolated_mcp_env(configure_token=test_token):
                # Verify bearer_token is absent from every ALL_TOOLS inputSchema["properties"]
                for tool in mcp_server.ALL_TOOLS:
                    schema = getattr(tool, "input_schema", None) or tool.model_dump(by_alias=True).get("inputSchema", {})
                    self.assertNotIn("bearer_token", schema.get("properties", {}))

                meta = {"authorization": f"Bearer {test_token}"}

                # (a) Unauthorized call (missing or wrong token) returns contents=[]
                unauth_none = await mcp_server.handle_read_resource(
                    None,
                    types.ReadResourceRequestParams(uri="brain://atoms/structured-logging.md"),
                )
                self.assertEqual(unauth_none.contents, [])

                unauth_wrong = await mcp_server.handle_read_resource(
                    None,
                    types.ReadResourceRequestParams(
                        uri="brain://atoms/structured-logging.md",
                        _meta={"authorization": "Bearer wrong-token-123456"},
                    ),
                )
                self.assertEqual(unauth_wrong.contents, [])

                # (b) Authorized call via _meta on accepted current atom returns markdown content
                ok_res = await mcp_server.handle_read_resource(
                    None,
                    types.ReadResourceRequestParams(
                        uri="brain://atoms/structured-logging.md",
                        _meta=meta,
                    ),
                )
                self.assertEqual(len(ok_res.contents), 1)
                self.assertEqual(ok_res.contents[0].mime_type, "text/markdown")
                self.assertIn("Logs should be structured (JSON)", ok_res.contents[0].text)

                # (c) pending, superseded_by, or tampered-source atom returns contents=[]
                for blocked_uri in (
                    "brain://atoms/observability-o11y.md",
                    "brain://atoms/superseded-logging.md",
                    "brain://atoms/tampered-logging.md",
                ):
                    blocked = await mcp_server.handle_read_resource(
                        None,
                        types.ReadResourceRequestParams(uri=blocked_uri, _meta=meta),
                    )
                    self.assertEqual(blocked.contents, [], f"Expected empty contents for {blocked_uri}")

                # (d) Traversal URI brain://atoms/../../pyproject.toml returns contents=[]
                for trav_uri in (
                    "brain://atoms/../../pyproject.toml",
                    "brain://atoms/..\\..\\pyproject.toml",
                    "brain://other/structured-logging.md",
                ):
                    trav = await mcp_server.handle_read_resource(
                        None,
                        types.ReadResourceRequestParams(uri=trav_uri, _meta=meta),
                    )
                    self.assertEqual(trav.contents, [], f"Expected empty contents for {trav_uri}")

        asyncio.run(run_resource_checks())


class ScoutPinningTests(unittest.TestCase):
    def test_remote_validation_pins_all_public_answers_and_rejects_mixed_dns_answers(self):
        from unittest.mock import patch

        from ollama_scout import UnsafeRemoteUrlError, validate_remote_url

        public = (None, None, None, None, ("8.8.8.8", 443))
        private = (None, None, None, None, ("10.0.0.7", 443))
        with patch("ollama_scout.socket.getaddrinfo", return_value=[public]):
            target = validate_remote_url("https://public.example/article")
        self.assertEqual(target.addresses, ("8.8.8.8",))
        self.assertEqual(target.hostname, "public.example")
        self.assertEqual(target.port, 443)

        with patch("ollama_scout.socket.getaddrinfo", return_value=[public, private]):
            with self.assertRaises(UnsafeRemoteUrlError):
                validate_remote_url("https://mixed.example/article")

    def test_scout_transport_connects_to_pinned_address_without_resolving_url_host(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from threading import Thread
        from unittest.mock import patch

        from ollama_scout import PinnedHTTPTransport, ValidatedRemoteTarget

        seen_hosts = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen_hosts.append(self.headers.get("Host"))
                body = b"pinned response"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        port = server.server_address[1]
        target = ValidatedRemoteTarget(
            url=f"http://public.example:{port}/article",
            hostname="public.example",
            port=port,
            addresses=("127.0.0.1",),
        )
        original_getaddrinfo = __import__("socket").getaddrinfo
        resolved_hosts = []

        def guarded_getaddrinfo(host, *args, **kwargs):
            resolved_hosts.append(str(host))
            if str(host) != "127.0.0.1":
                raise AssertionError(f"unvalidated hostname was resolved during connect: {host}")
            return original_getaddrinfo(host, *args, **kwargs)

        try:
            import httpx

            with patch("socket.getaddrinfo", side_effect=guarded_getaddrinfo):
                with httpx.Client(
                    transport=PinnedHTTPTransport(target),
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    response = client.get(target.url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, "pinned response")
            self.assertEqual(seen_hosts, [f"public.example:{port}"])
            self.assertTrue(resolved_hosts)
            self.assertTrue(all(host == "127.0.0.1" for host in resolved_hosts))
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)


class TrustBoundaryTests(unittest.TestCase):
    def test_mcp_capture_provenance_is_sticky(self):
        from memory_format import capture_origin, normalize_memory_document

        capture = "<!-- second_brain_origin: mcp_local -->\nA note from an MCP caller."
        self.assertEqual(capture_origin(capture, "capture.md"), "mcp_local")
        self.assertEqual(capture_origin("external text", "web-note.intake"), "web_remote")
        forged = normalize_memory_document(
            """---\nid: x\norigin: web_remote\nreview_status: accepted\n---\n\n## Claim\nA claim.\n\n## Evidence\nAn excerpt.\n""",
            "capture.md",
            current_date="2026-10-24",
            origin=capture_origin(capture, "capture.md"),
        )
        self.assertIn("origin: mcp_local", forged)
        self.assertIn("review_status: pending", forged)

    def test_model_output_cannot_set_trusted_lifecycle_or_citation_fields(self):
        from memory_format import normalize_memory_document
        from recall import RecallOptions, should_include_record

        forged = """---
id: forged
type: atom
memory_type: semantic
sources: []
citation_integrity: anchored
claim_state: human_reviewed
review_status: accepted
review_status: accepted
epistemic_label: claim
---

## Claim
Forged claim.

## Evidence
Forged evidence.
"""
        normalized = normalize_memory_document(
            forged,
            source_filename="remote.md",
            current_date="2026-10-24",
            origin="web_remote",
        )

        self.assertIn("origin: web_remote", normalized)
        self.assertIn("citation_integrity: missing", normalized)
        self.assertIn("claim_state: candidate", normalized)
        self.assertIn("review_status: pending", normalized)
        self.assertEqual(normalized.count("review_status:"), 1)
        self.assertNotIn("evidence_source:", normalized)
        self.assertFalse(should_include_record(
            {"review_status": "accepted", "claim_state": "human_reviewed", "memory_type": "semantic"},
            RecallOptions(),
        ))

    def test_approval_requires_verifiable_evidence_and_revision_attestation(self):
        from review_atom import ReviewTransitionError, apply_review_transition, approved_revision_is_current

        atom = """---
id: evidence-test
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
The source makes a claim.

## Evidence
\"The exact source sentence is here.\"
"""
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "brain"
            atoms = vault / "2-atoms"
            source = vault / "0-raw" / "archive" / "2026-10" / "source.md"
            atoms.mkdir(parents=True)
            source.parent.mkdir(parents=True)
            source.write_text("Capture:\nThe exact source sentence is here.\n", encoding="utf-8")
            atom_path = atoms / "evidence-test.md"
            atom_path.write_text(atom, encoding="utf-8")
            decision_log = vault / "0-raw" / "archive" / "decision-record.jsonl"
            expected = hashlib.sha256(atom_path.read_bytes()).hexdigest()

            decision = apply_review_transition(
                atom_path=str(atom_path),
                action="approve",
                expected_sha256=expected,
                actor="owner-test",
                decision_log_path=str(decision_log),
                history_dir=str(atoms / ".history"),
                brain_dir=str(vault),
                atoms_dir=str(atoms),
                reviewed_on="2026-10-24",
            )

            self.assertEqual(decision["action"], "approve")
            self.assertTrue(decision.get("evidence_source"))
            self.assertTrue(approved_revision_is_current(str(atom_path), str(decision_log), str(vault), str(atoms)))
            source.write_text(
                "Capture:\nThe exact source sentence is here.\nA later source revision.\n",
                encoding="utf-8",
            )
            self.assertFalse(approved_revision_is_current(str(atom_path), str(decision_log), str(vault), str(atoms)))
            duplicate_source = vault / "0-raw" / "archive" / "2026-10" / "duplicate.md"
            duplicate_source.write_text(
                "The exact source sentence is here. Then again: The exact source sentence is here.",
                encoding="utf-8",
            )
            duplicate_atom_path = atoms / "duplicate-evidence.md"
            duplicate_atom = atom.replace("evidence-test", "duplicate-evidence").replace("source.md", "duplicate.md")
            duplicate_atom_path.write_text(duplicate_atom, encoding="utf-8")
            with self.assertRaises(ReviewTransitionError):
                apply_review_transition(
                    atom_path=str(duplicate_atom_path),
                    action="approve",
                    expected_sha256=hashlib.sha256(duplicate_atom_path.read_bytes()).hexdigest(),
                    actor="owner-test",
                    decision_log_path=str(decision_log),
                    brain_dir=str(vault),
                    atoms_dir=str(atoms),
                )
            self.assertIn("review_status: pending", duplicate_atom_path.read_text(encoding="utf-8"))

            ambiguous_source = vault / "0-raw" / "alternate" / "source.md"
            ambiguous_source.parent.mkdir(parents=True)
            ambiguous_source.write_text("An unrelated source with a reused basename.", encoding="utf-8")
            ambiguous_atom_path = atoms / "ambiguous-source.md"
            ambiguous_atom = atom.replace("evidence-test", "ambiguous-source")
            ambiguous_atom_path.write_text(ambiguous_atom, encoding="utf-8")
            with self.assertRaises(ReviewTransitionError):
                apply_review_transition(
                    atom_path=str(ambiguous_atom_path),
                    action="approve",
                    expected_sha256=hashlib.sha256(ambiguous_atom_path.read_bytes()).hexdigest(),
                    actor="owner-test",
                    decision_log_path=str(decision_log),
                    brain_dir=str(vault),
                    atoms_dir=str(atoms),
                )

            from atom_mutator import apply_atom_edit
            from lexical_store import lexical_index_is_current

            approved_text = atom_path.read_text(encoding="utf-8")
            edit_hash = hashlib.sha256(atom_path.read_bytes()).hexdigest()
            apply_atom_edit(
                atom_path=str(atom_path),
                edited_content=approved_text.replace("source makes", "source once made"),
                expected_sha256=edit_hash,
                actor="owner-test",
                decision_log_path=str(decision_log),
                brain_dir=str(vault),
                atoms_dir=str(atoms),
            )
            current_text = atom_path.read_text(encoding="utf-8")
            self.assertIn("review_status: pending", current_text)
            self.assertIn("claim_state: candidate", current_text)
            self.assertFalse(approved_revision_is_current(str(atom_path), str(decision_log), str(vault), str(atoms)))
            self.assertTrue(lexical_index_is_current(str(atoms), str(vault / ".sidecar" / "lexical.sqlite3")))



    def test_failed_derived_index_refresh_rolls_back_atom_and_decision(self):
        from unittest.mock import patch
        from atom_mutator import apply_atom_edit

        with tempfile.TemporaryDirectory() as temporary:
            brain = Path(temporary) / "brain"
            atoms = brain / "2-atoms"
            atoms.mkdir(parents=True)
            atom_path = atoms / "rollback.md"
            original = "---\nid: rollback\ntype: atom\nsources: []\norigin: user_local\nreview_status: pending\n---\n\n## Claim\nThe original claim.\n\n## Evidence\nA sufficiently long excerpt for structural parsing.\n"
            atom_path.write_text(original, encoding="utf-8")
            decision_log = brain / "0-raw" / "archive" / "decision-record.jsonl"
            with patch("atom_mutator.refresh_lexical_index", side_effect=RuntimeError("index unavailable")):
                with self.assertRaisesRegex(RuntimeError, "index unavailable"):
                    apply_atom_edit(
                        atom_path=str(atom_path),
                        edited_content=original.replace("original claim", "edited claim"),
                        expected_sha256=hashlib.sha256(atom_path.read_bytes()).hexdigest(),
                        actor="owner-test",
                        decision_log_path=str(decision_log),
                        brain_dir=str(brain),
                        atoms_dir=str(atoms),
                    )
            self.assertEqual(atom_path.read_text(encoding="utf-8"), original)
            self.assertFalse(decision_log.exists())

    def test_resolve_under_and_safe_file_reject_junction_escape_ntfs_ads_and_reserved_devices(self):
        import os
        import subprocess
        from config import resolve_under, safe_file_in_dir

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_dir = root / "vault"
            outside_dir = root / "outside"
            base_dir.mkdir()
            outside_dir.mkdir()
            (outside_dir / "secret.md").write_text("outside secret", encoding="utf-8")
            (base_dir / "note.md").write_text("legit note", encoding="utf-8")

            escape_junc = base_dir / "escape_junc"
            direct_junc_md = base_dir / "junction-note.md"
            created_junctions: list[Path] = []
            try:
                for link_path in (escape_junc, direct_junc_md):
                    if os.name == "nt":
                        subprocess.run(
                            ["cmd", "/c", "mklink", "/J", str(link_path), str(outside_dir)],
                            check=True,
                            capture_output=True,
                            text=True,
                        )
                    else:
                        os.symlink(outside_dir, link_path, target_is_directory=True)
                    created_junctions.append(link_path)

                # 1. NTFS directory junction / symlink escaping base_dir returns None
                self.assertIsNone(resolve_under(str(base_dir), "escape_junc/secret.md", must_exist=True))
                self.assertIsNone(resolve_under(str(base_dir), "escape_junc/new.md", must_exist=False))
                self.assertIsNone(resolve_under(str(base_dir), "escape_junc", must_exist=True))
                self.assertIsNone(resolve_under(str(base_dir), "escape_junc", must_exist=False))
                self.assertIsNone(
                    safe_file_in_dir(str(base_dir), "junction-note.md", allowed_exts={".md"}, must_exist=True)
                )
                self.assertIsNone(
                    safe_file_in_dir(str(base_dir), "junction-note.md", allowed_exts={".md"}, must_exist=False)
                )

                # 2. NTFS Alternate Data Streams (ADS) return None
                for ads_name in ("note.md:stream", "note.md:stream:$DATA", "note.md:hidden.md"):
                    self.assertIsNone(resolve_under(str(base_dir), ads_name, must_exist=True))
                    self.assertIsNone(resolve_under(str(base_dir), ads_name, must_exist=False))
                    self.assertIsNone(
                        safe_file_in_dir(str(base_dir), ads_name, allowed_exts={".md"}, must_exist=True)
                    )
                    self.assertIsNone(
                        safe_file_in_dir(str(base_dir), ads_name, allowed_exts={".md"}, must_exist=False)
                    )

                # 3. Reserved Windows device names return None across both helpers
                for reserved in ("CON.md", "NUL.md", "COM1.md", "con.md", "NUL.tar.md"):
                    self.assertIsNone(resolve_under(str(base_dir), reserved, must_exist=True))
                    self.assertIsNone(resolve_under(str(base_dir), reserved, must_exist=False))
                    self.assertIsNone(resolve_under(str(base_dir), f"sub/{reserved}", must_exist=False))
                    self.assertIsNone(
                        safe_file_in_dir(str(base_dir), reserved, allowed_exts={".md"}, must_exist=True)
                    )
                    self.assertIsNone(
                        safe_file_in_dir(str(base_dir), reserved, allowed_exts={".md"}, must_exist=False)
                    )
            finally:
                for link_path in reversed(created_junctions):
                    try:
                        if os.name == "nt":
                            os.rmdir(link_path)
                        else:
                            os.unlink(link_path)
                    except OSError:
                        pass


class ReviewDetailsTests(unittest.TestCase):
    def test_review_details_report_current_quote_then_detect_source_drift(self):
        from evidence import find_evidence_anchor, ANCHOR_FIELDS
        from memory_format import set_scalar_field, split_frontmatter, render_memory_document
        from review_details import atom_review_details

        with tempfile.TemporaryDirectory() as temporary:
            brain = Path(temporary) / "brain"
            atoms = brain / "2-atoms"
            source = brain / "0-raw" / "source.md"
            atoms.mkdir(parents=True)
            source.parent.mkdir(parents=True)
            source.write_text("The durable evidence sentence for review details.", encoding="utf-8")
            atom = atoms / "review.md"
            content = (
                "---\nid: review\ntype: atom\nmemory_type: semantic\nsources:\n  - 0-raw/source.md\n"
                "links: []\ncontexts: []\nsupersedes: []\nsuperseded_by: []\ncontested_by: []\n"
                "origin: user_local\ncitation_integrity: missing\nclaim_state: candidate\n"
                "epistemic_label: claim\nreview_status: pending\n---\n\n## Claim\nA review claim.\n\n"
                "## Evidence\nThe durable evidence sentence for review details.\n"
            )
            anchor = find_evidence_anchor(content, str(brain))
            frontmatter, body = split_frontmatter(content)
            for key in ANCHOR_FIELDS:
                frontmatter = set_scalar_field(frontmatter, key, str(anchor[key]), "origin")
            atom.write_text(render_memory_document(frontmatter, body), encoding="utf-8")
            initial = atom_review_details(atom.read_text(encoding="utf-8"), atom_path=str(atom), atoms_dir=str(atoms), brain_dir=str(brain))
            self.assertTrue(initial["anchor_current"])
            self.assertEqual(initial["current_quote"], "The durable evidence sentence for review details.")
            source.write_text("The durable evidence sentence for review details. Changed.", encoding="utf-8")
            changed = atom_review_details(atom.read_text(encoding="utf-8"), atom_path=str(atom), atoms_dir=str(atoms), brain_dir=str(brain))
            self.assertFalse(changed["anchor_current"])
            self.assertNotEqual(changed["current_source_sha256"], anchor["evidence_sha256"])

    def test_review_details_never_reads_traversal_source_paths(self):
        from review_details import atom_review_details
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brain = root / "brain"
            atoms = brain / "2-atoms"
            atoms.mkdir(parents=True)
            outside = root / "secret.md"
            outside.write_text("private", encoding="utf-8")
            content = (
                "---\nid: unsafe\nevidence_source: ../secret.md\nevidence_sha256: " + "0" * 64 +
                "\nevidence_start: 0\nevidence_end: 7\nevidence_quote_sha256: " + "0" * 64 +
                "\nlinks: []\nsupersedes: []\nsuperseded_by: []\ncontested_by: []\n---\n\n"
                "## Claim\nUnsafe path.\n\n## Evidence\nPrivate.\n"
            )
            details = atom_review_details(content, atom_path=str(atoms / "unsafe.md"), atoms_dir=str(atoms), brain_dir=str(brain))
            self.assertIn("traverses parent", details["source_error"])
            self.assertIsNone(details["current_quote"])

class AtomMergeTests(unittest.TestCase):
    def _fixture(self, root: Path):
        brain = root / "brain"
        atoms = brain / "2-atoms"
        atoms.mkdir(parents=True)
        survivor = atoms / "survivor.md"
        superseded = atoms / "superseded.md"
        def document(atom_id: str, claim: str) -> str:
            return (
                f"---\nid: {atom_id}\ntype: atom\nmemory_type: semantic\n"
                "sources: []\nlinks: []\ncontexts: []\nsupersedes: []\n"
                "superseded_by: []\ncontested_by: []\norigin: user_local\n"
                "citation_integrity: missing\nclaim_state: candidate\n"
                "epistemic_label: claim\nreview_status: pending\n---\n\n"
                f"## Claim\n{claim}\n\n## Evidence\nOwner-provided evidence excerpt.\n"
            )
        survivor.write_text(document("survivor", "The surviving claim."), encoding="utf-8")
        superseded.write_text(document("superseded", "The old claim."), encoding="utf-8")
        return brain, atoms, survivor, superseded

    def _merge(self, brain, atoms, survivor, superseded, **overrides):
        from atom_mutator import apply_atom_merge
        args = {
            "survivor_path": str(survivor),
            "survivor_expected_sha256": hashlib.sha256(survivor.read_bytes()).hexdigest(),
            "superseded_path": str(superseded),
            "superseded_expected_sha256": hashlib.sha256(superseded.read_bytes()).hexdigest(),
            "actor": "owner-test",
            "rationale": "Owner confirmed these records describe the same fact.",
            "decision_log_path": str(brain / "0-raw" / "decision.jsonl"),
            "history_dir": str(atoms / ".history"),
            "brain_dir": str(brain),
            "atoms_dir": str(atoms),
        }
        args.update(overrides)
        return apply_atom_merge(**args)

    def test_merge_links_both_preserved_atoms_and_records_both_revisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            brain, atoms, survivor, superseded = self._fixture(Path(temporary))
            old_superseded = superseded.read_bytes()
            records = self._merge(brain, atoms, survivor, superseded)
            survivor_text = survivor.read_text(encoding="utf-8")
            superseded_text = superseded.read_text(encoding="utf-8")
            self.assertIn('  - "superseded.md"', survivor_text)
            self.assertIn('  - "survivor.md"', superseded_text)
            self.assertIn("review_status: pending", survivor_text)
            self.assertIn("review_status: merged", superseded_text)
            self.assertIn("claim_state: superseded", superseded_text)
            self.assertEqual(len(records), 2)
            self.assertEqual(superseded.with_suffix(".md").read_bytes() != old_superseded, True)
            self.assertTrue((atoms / ".history" / f"superseded.md.{hashlib.sha256(old_superseded).hexdigest()}.bak").is_file())
            audit = [__import__("json").loads(line) for line in (brain / "0-raw" / "decision.jsonl").read_text().splitlines()]
            self.assertEqual(len(audit), 2)
            self.assertTrue(all(item["action"] == "merge" for item in audit))

    def test_merge_rejects_stale_second_hash_without_mutating_either_atom(self):
        from atom_mutator import ReviewConflictError
        with tempfile.TemporaryDirectory() as temporary:
            brain, atoms, survivor, superseded = self._fixture(Path(temporary))
            before = (survivor.read_bytes(), superseded.read_bytes())
            with self.assertRaises(ReviewConflictError):
                self._merge(brain, atoms, survivor, superseded, superseded_expected_sha256="0" * 64)
            self.assertEqual((survivor.read_bytes(), superseded.read_bytes()), before)
            self.assertFalse((brain / "0-raw" / "decision.jsonl").exists())

    def test_merged_atom_cannot_be_reapproved_through_single_atom_transition(self):
        from atom_mutator import ReviewTransitionError, apply_review_transition
        with tempfile.TemporaryDirectory() as temporary:
            brain, atoms, survivor, superseded = self._fixture(Path(temporary))
            self._merge(brain, atoms, survivor, superseded)
            with self.assertRaisesRegex(ReviewTransitionError, "cannot be approved"):
                apply_review_transition(
                    atom_path=str(superseded),
                    action="approve",
                    expected_sha256=hashlib.sha256(superseded.read_bytes()).hexdigest(),
                    actor="owner-test",
                    decision_log_path=str(brain / "0-raw" / "decision.jsonl"),
                    brain_dir=str(brain),
                    atoms_dir=str(atoms),
                )
            self.assertIn("review_status: merged", superseded.read_text(encoding="utf-8"))

    def test_merge_rejects_missing_rationale_and_rolls_back_index_failure(self):
        from unittest.mock import patch
        from atom_mutator import ReviewTransitionError
        with tempfile.TemporaryDirectory() as temporary:
            brain, atoms, survivor, superseded = self._fixture(Path(temporary))
            before = (survivor.read_bytes(), superseded.read_bytes())
            with self.assertRaises(ReviewTransitionError):
                self._merge(brain, atoms, survivor, superseded, rationale=" ")
            self.assertEqual((survivor.read_bytes(), superseded.read_bytes()), before)
            with patch("atom_mutator.refresh_lexical_index", side_effect=RuntimeError("index unavailable")):
                with self.assertRaisesRegex(RuntimeError, "index unavailable"):
                    self._merge(brain, atoms, survivor, superseded)
            self.assertEqual((survivor.read_bytes(), superseded.read_bytes()), before)
            self.assertFalse((brain / "0-raw" / "decision.jsonl").exists())

    def test_merge_rolls_back_both_files_when_second_write_or_audit_fails(self):
        from unittest.mock import patch
        import atom_mutator
        with tempfile.TemporaryDirectory() as temporary:
            brain, atoms, survivor, superseded = self._fixture(Path(temporary))
            before = (survivor.read_bytes(), superseded.read_bytes())
            real_write = atom_mutator.atomic_write_bytes
            calls = 0
            def fail_second(path, content):
                nonlocal calls
                calls += 1
                if calls == 4:  # two backups, survivor write, then superseded write
                    raise OSError("second atom write failed")
                return real_write(path, content)
            with patch("atom_mutator.atomic_write_bytes", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "second atom write failed"):
                    self._merge(brain, atoms, survivor, superseded)
            self.assertEqual((survivor.read_bytes(), superseded.read_bytes()), before)

            with patch("atom_mutator.append_decision_records", side_effect=OSError("audit unavailable")):
                with self.assertRaisesRegex(OSError, "audit unavailable"):
                    self._merge(brain, atoms, survivor, superseded)
            self.assertEqual((survivor.read_bytes(), superseded.read_bytes()), before)
            self.assertFalse((brain / "0-raw" / "decision.jsonl").exists())


class DenseScoreTests(unittest.TestCase):
    def test_system_source_path_replaces_model_supplied_sources(self):
        from memory_format import render_memory_document, set_string_list_field, split_frontmatter

        document = "---\nid: source-check\nsources:\n  - hallucinated.md\n  - another.md\ntype: atom\n---\n\n## Claim\nA source-backed claim.\n\n## Evidence\nExact evidence excerpt.\n"
        frontmatter, body = split_frontmatter(document)
        updated = render_memory_document(
            set_string_list_field(frontmatter, "sources", ["0-raw/archive/2026-10/source-file.md"], "id"),
            body,
        )
        self.assertIn('  - "0-raw/archive/2026-10/source-file.md"', updated)
        self.assertNotIn("hallucinated.md", updated)
        self.assertNotIn("another.md", updated)

    def test_cosine_distances_above_one_retain_distinctions(self):
        from recall import cosine_distance_to_similarity

        self.assertEqual(cosine_distance_to_similarity(1.25), 0.375)
        self.assertEqual(cosine_distance_to_similarity(1.75), 0.125)
        self.assertNotEqual(cosine_distance_to_similarity(1.25), cosine_distance_to_similarity(1.75))

    def test_new_vector_manifest_declares_cosine_metric(self):
        from index_manifest import build_index_manifest

        with tempfile.TemporaryDirectory() as temporary:
            atoms = Path(temporary) / "atoms"
            atoms.mkdir()
            manifest = build_index_manifest(
                atoms_dir=str(atoms),
                collection_name="atoms",
                embedding_model="test-embedder",
                embedding_dimensions=3,
                indexed_count=0,
            )

        self.assertEqual(manifest["distance_metric"], "cosine")

    def test_atom_mutator_rebuilds_an_active_dense_generation_under_its_lock(self):
        from unittest.mock import patch
        from atom_mutator import apply_atom_edit, apply_review_transition
        from index_manifest import build_index_manifest, write_index_manifest

        with tempfile.TemporaryDirectory() as temporary:
            brain = Path(temporary) / "brain"
            atoms = brain / "2-atoms"
            source = brain / "0-raw" / "source.md"
            chroma = brain / ".chroma"
            atoms.mkdir(parents=True)
            source.parent.mkdir(parents=True)
            chroma.mkdir()
            source.write_text("An exact, durable source sentence for this memory.", encoding="utf-8")
            atom_path = atoms / "current.md"
            atom_path.write_text(
                "---\nid: current\ntype: atom\nmemory_type: semantic\nsources:\n  - 0-raw/source.md\norigin: user_local\ncitation_integrity: missing\nclaim_state: candidate\nepistemic_label: claim\nreview_status: pending\n---\n\n## Claim\nThe source contains a sentence.\n\n## Evidence\nAn exact, durable source sentence for this memory.\n",
                encoding="utf-8",
            )
            manifest = build_index_manifest(
                atoms_dir=str(atoms),
                collection_name="atoms",
                embedding_model="test-embedder",
                embedding_dimensions=3,
                indexed_count=1,
                generation_id="initial",
            )
            write_index_manifest(str(chroma), manifest)
            rebuild_calls = []

            def fake_rebuild(**kwargs):
                rebuild_calls.append(kwargs)
                next_manifest = build_index_manifest(
                    atoms_dir=kwargs["atoms_dir"],
                    collection_name=kwargs["collection_name"],
                    embedding_model=kwargs["model"],
                    embedding_dimensions=3,
                    indexed_count=1,
                    generation_id=f"rebuilt-{len(rebuild_calls)}",
                )
                write_index_manifest(kwargs["store_path"], next_manifest)
                return {"committed": True, "generation_id": next_manifest["generation_id"]}

            fake_module = types.ModuleType("rebuild_vectors")
            fake_module.rebuild_vectors = fake_rebuild
            decision_log = brain / "0-raw" / "archive" / "decision-record.jsonl"
            with patch.dict(sys.modules, {"rebuild_vectors": fake_module}):
                approval = apply_review_transition(
                    atom_path=str(atom_path),
                    action="approve",
                    expected_sha256=hashlib.sha256(atom_path.read_bytes()).hexdigest(),
                    actor="owner-test",
                    decision_log_path=str(decision_log),
                    brain_dir=str(brain),
                    atoms_dir=str(atoms),
                )
                edit = apply_atom_edit(
                    atom_path=str(atom_path),
                    edited_content=atom_path.read_text(encoding="utf-8").replace("contains a sentence", "once contained a sentence"),
                    expected_sha256=hashlib.sha256(atom_path.read_bytes()).hexdigest(),
                    actor="owner-test",
                    decision_log_path=str(decision_log),
                    brain_dir=str(brain),
                    atoms_dir=str(atoms),
                )

            self.assertEqual(approval["dense_index_state"], "current")
            self.assertEqual(edit["dense_index_state"], "current")
            self.assertEqual(len(rebuild_calls), 2)
            self.assertTrue(all(call["_writer_lock_held"] is True for call in rebuild_calls))

    def test_semantic_recall_refuses_legacy_or_non_cosine_manifests(self):
        from index_manifest import build_index_manifest
        from recall import MemoryRecallUnavailable, validated_index_generation

        with tempfile.TemporaryDirectory() as temporary:
            atoms = Path(temporary) / "atoms"
            atoms.mkdir()
            manifest = build_index_manifest(
                atoms_dir=str(atoms),
                collection_name="atoms",
                embedding_model="test-embedder",
                embedding_dimensions=3,
                indexed_count=0,
            )
            manifest["distance_metric"] = "l2"
            with self.assertRaises(MemoryRecallUnavailable):
                validated_index_generation(
                    manifest,
                    atoms_dir=str(atoms),
                    collection_name="atoms",
                    embedding_model="test-embedder",
                    embedding_dimensions=3,
                )


class HybridAndScopedRecallTests(unittest.TestCase):
    def test_include_statuses_never_bypasses_approval_or_superseded_invariants(self):
        from recall import RecallOptions, should_include_record

        unverified_accepted = {
            "review_status": "accepted",
            "claim_state": "human_reviewed",
            "approved_revision_current": False,
            "memory_type": "semantic",
        }
        self.assertFalse(should_include_record(unverified_accepted, RecallOptions()))
        self.assertFalse(should_include_record(unverified_accepted, RecallOptions(include_statuses={"accepted"})))
        self.assertFalse(
            should_include_record(
                unverified_accepted,
                RecallOptions(include_statuses={"accepted"}, allow_unreviewed_exploratory=True),
            )
        )

        pending_candidate = {
            "review_status": "pending",
            "claim_state": "candidate",
            "approved_revision_current": False,
            "memory_type": "semantic",
        }
        self.assertFalse(should_include_record(pending_candidate, RecallOptions(include_statuses={"pending"})))
        self.assertTrue(
            should_include_record(
                pending_candidate,
                RecallOptions(include_statuses={"pending"}, allow_unreviewed_exploratory=True),
            )
        )

        superseded_approved = {
            "review_status": "accepted",
            "claim_state": "human_reviewed",
            "approved_revision_current": True,
            "superseded_by": ["survivor.md"],
            "memory_type": "semantic",
        }
        self.assertFalse(should_include_record(superseded_approved, RecallOptions()))

    def test_project_scoping_hierarchical_matching_and_cross_project_link_gate(self):
        from unittest.mock import patch
        from lexical_store import rebuild_lexical_index
        from recall import RecallOptions, search_lexical_memory

        atom_sb = """---
id: sb-auth
type: atom
memory_type: semantic
contexts: [project:second-brain/auth]
claim_state: human_reviewed
review_status: accepted
---

## Claim
Structured JSON logs trace authentication events safely.

## Evidence
"Structured JSON logs trace authentication events safely across services."
"""
        atom_client = """---
id: client-alpha
type: atom
memory_type: semantic
contexts: [project:client_alpha]
claim_state: human_reviewed
review_status: accepted
---

## Claim
Client alpha services emit structured JSON authentication logs.

## Evidence
"Client alpha services emit structured JSON authentication logs."
"""
        atom_sb_linked = atom_sb + "\n## Links\n[[client-alpha.md]]\n"

        with tempfile.TemporaryDirectory() as tmp:
            atoms_dir = Path(tmp) / "2-atoms"
            atoms_dir.mkdir(parents=True)
            index_path = Path(tmp) / "lexical.sqlite3"
            (atoms_dir / "sb-auth.md").write_text(atom_sb_linked, encoding="utf-8")
            (atoms_dir / "client-alpha.md").write_text(atom_client, encoding="utf-8")
            rebuild_lexical_index(str(atoms_dir), str(index_path))

            with patch("recall.approved_revision_is_current", return_value=True):
                # Scoped to project:second_brain without cross-project permission
                direct, neighbors = search_lexical_memory(
                    "structured JSON authentication logs",
                    RecallOptions(projects=("project:second_brain",), allow_cross_project=False),
                    atoms_dir=str(atoms_dir),
                    index_path=str(index_path),
                )
                self.assertEqual([item.filename for item in direct], ["sb-auth.md"])
                self.assertEqual(neighbors, [])
                self.assertIn("project:project:second-brain/auth", direct[0].match_reasons)

                # Scoped to project:second_brain WITH explicit cross-project link permission
                direct_cp, neighbors_cp = search_lexical_memory(
                    "structured JSON authentication logs",
                    RecallOptions(projects=("project:second_brain",), allow_cross_project=True),
                    atoms_dir=str(atoms_dir),
                    index_path=str(index_path),
                )
                self.assertEqual([item.filename for item in direct_cp], ["sb-auth.md"])
                self.assertEqual([item.filename for item in neighbors_cp], ["client-alpha.md"])
                self.assertIn("cross_project_reference", neighbors_cp[0].match_reasons)
                self.assertIn("linked_from:sb-auth.md", neighbors_cp[0].match_reasons)

    def test_rrf_hybrid_fusion_and_graceful_fts5_degradation_banner(self):
        from unittest.mock import patch
        from lexical_store import rebuild_lexical_index
        from recall import (
            MemoryRecallUnavailable,
            RecallOptions,
            consume_last_degradation_warning,
            search_hybrid_rrf_memory,
        )

        atom = """---
id: rrf-test
type: atom
memory_type: semantic
contexts: [project:second_brain]
claim_state: human_reviewed
review_status: accepted
---

## Claim
Hybrid retrieval fuses lexical and dense rankings deterministically.

## Evidence
"Hybrid retrieval fuses lexical and dense rankings deterministically."
"""
        with tempfile.TemporaryDirectory() as tmp:
            atoms_dir = Path(tmp) / "2-atoms"
            atoms_dir.mkdir(parents=True)
            index_path = Path(tmp) / "lexical.sqlite3"
            (atoms_dir / "rrf-test.md").write_text(atom, encoding="utf-8")
            rebuild_lexical_index(str(atoms_dir), str(index_path))

            with patch("recall.approved_revision_is_current", return_value=True):
                consume_last_degradation_warning()
                with patch("recall.search_semantic_memory", side_effect=MemoryRecallUnavailable("Ollama daemon offline")):
                    degraded_direct, _ = search_hybrid_rrf_memory(
                        "hybrid retrieval lexical dense rankings",
                        RecallOptions(retrieval_mode="rrf"),
                        atoms_dir=str(atoms_dir),
                        index_path=str(index_path),
                    )
                banner = consume_last_degradation_warning()
                self.assertEqual([item.filename for item in degraded_direct], ["rrf-test.md"])
                self.assertIsNotNone(banner)
                self.assertIn("dynamically degraded to FTS5 lexical retrieval", banner)
                self.assertIn("degraded:lexical_fallback", degraded_direct[0].match_reasons)

    def test_read_only_duplicate_suggestions_never_mutate_files(self):
        from recall import suggest_duplicate_atoms

        atom_one = """---
id: dup-one
type: atom
memory_type: semantic
origin: user_local
review_status: pending
---

## Claim
Structured JSON logging enables distributed request correlation and redacts PII.

## Evidence
"Structured JSON logging enables distributed request correlation and redacts sensitive PII fields."
"""
        atom_two = """---
id: dup-two
type: atom
memory_type: semantic
origin: mcp_local
review_status: pending
---

## Claim
Distributed request correlation relies on structured JSON logging with PII redaction.

## Evidence
"Services use structured JSON logging for distributed request correlation while redacting PII fields."
"""
        with tempfile.TemporaryDirectory() as tmp:
            atoms_dir = Path(tmp) / "2-atoms"
            atoms_dir.mkdir(parents=True)
            p1 = atoms_dir / "dup-one.md"
            p2 = atoms_dir / "dup-two.md"
            p1.write_text(atom_one, encoding="utf-8")
            p2.write_text(atom_two, encoding="utf-8")
            before_1 = p1.read_bytes()
            before_2 = p2.read_bytes()

            suggestions = suggest_duplicate_atoms(str(atoms_dir), min_jaccard=0.35)
            self.assertEqual(len(suggestions), 1)
            self.assertEqual(suggestions[0]["atom_a"], "dup-one.md")
            self.assertEqual(suggestions[0]["atom_b"], "dup-two.md")
            self.assertEqual(p1.read_bytes(), before_1)
            self.assertEqual(p2.read_bytes(), before_2)


class ProductionPackagingAndGateTests(unittest.TestCase):
    def test_posix_and_windows_setup_scripts_inspect_capabilities_without_git_mutations(self):
        ps1 = (ROOT / "setup.ps1").read_text(encoding="utf-8")
        sh = (ROOT / "setup.sh").read_text(encoding="utf-8")

        for script_text in (ps1, sh):
            lowered = script_text.lower()
            self.assertIsNone(re.search(r"(?im)^\s*git\s+(?:init|add|commit)\b", lowered))
            self.assertIn("will not initialize, stage, or commit files", lowered)
            self.assertIn("sync --frozen", lowered)

        self.assertIn("vcruntime140.dll", ps1)
        self.assertIn("nvidia-smi", ps1)
        self.assertIn("libstdc++.so.6", sh)
        self.assertIn("nvidia-smi", sh)
        self.assertTrue(sh.startswith("#!/usr/bin/env bash"))

    def test_doctor_and_test_all_orchestrator_contracts(self):
        from contextlib import ExitStack
        from unittest.mock import patch
        import doctor
        import test_all
        from lexical_store import rebuild_lexical_index

        def _patch_doctor_vault(stack: ExitStack, vault_root: Path) -> None:
            raw_dir = vault_root / "0-raw"
            stack.enter_context(patch.object(doctor, "BRAIN_DIR", str(vault_root)))
            stack.enter_context(patch.object(doctor, "RAW_DIR", str(raw_dir)))
            stack.enter_context(patch.object(doctor, "INBOX_DIR", str(raw_dir / "inbox")))
            stack.enter_context(patch.object(doctor, "ATOMS_DIR", str(vault_root / "2-atoms")))
            stack.enter_context(patch.object(doctor, "THREADS_DIR", str(vault_root / "3-threads")))
            stack.enter_context(patch.object(doctor, "EXPERIENCES_DIR", str(vault_root / "4-experiences")))
            stack.enter_context(patch.object(doctor, "PLAYBOOKS_DIR", str(vault_root / "5-playbooks")))
            stack.enter_context(patch.object(doctor, "SOURCES_DIR", str(vault_root / "sources")))
            stack.enter_context(patch.object(doctor, "BRIEFINGS_DIR", str(vault_root / "briefings")))
            stack.enter_context(patch.object(doctor, "CHROMA_PATH", str(vault_root / ".chroma")))
            stack.enter_context(
                patch.object(doctor, "LEXICAL_INDEX_PATH", str(vault_root / ".sidecar" / "lexical.sqlite3"))
            )
            stack.enter_context(patch.object(doctor, "READING_LIST", str(raw_dir / "reading-list.md")))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)

            # 1. Uninitialized empty BRAIN_VAULT_DIR returns structured FAILs without raising FileNotFoundError
            uninit_vault = tmp_root / "empty-vault"
            uninit_vault.mkdir()
            with ExitStack() as stack:
                _patch_doctor_vault(stack, uninit_vault)
                uninit_checks = doctor.run_checks()
            uninit_failed_areas = {area for status, area, _ in uninit_checks if status == "FAIL"}
            self.assertIn("vault dirs", uninit_failed_areas)
            self.assertIn("lexical recall", uninit_failed_areas)

            # 2. Seeded isolated temporary vault passes all required doctor checks cleanly
            healthy_vault = tmp_root / "healthy-vault"
            for subdir in (
                "0-raw",
                "0-raw/inbox",
                "1-desk",
                "2-atoms",
                "3-threads",
                "4-experiences",
                "5-playbooks",
                "sources",
                "briefings",
                ".chroma",
                ".sidecar",
            ):
                (healthy_vault / subdir).mkdir(parents=True, exist_ok=True)
            rebuild_lexical_index(
                str(healthy_vault / "2-atoms"),
                str(healthy_vault / ".sidecar" / "lexical.sqlite3"),
            )
            with ExitStack() as stack:
                _patch_doctor_vault(stack, healthy_vault)
                checks = doctor.run_checks()

        areas = {area for _, area, _ in checks}
        self.assertIn("host runtime", areas)
        self.assertIn("c++ runtime", areas)
        self.assertIn("compute device", areas)
        self.assertIn("capabilities", areas)
        self.assertIn("vault dirs", areas)
        self.assertIn("lexical recall", areas)
        self.assertIn("capability tier", areas)
        self.assertIn("playbooks", areas)
        failures = [item for item in checks if item[0] == "FAIL"]
        self.assertEqual(failures, [])

        suite_ids = {suite_id for suite_id, _, _ in test_all.SUITES}
        self.assertEqual(
            suite_ids,
            {
                "doctor",
                "evaluation_dataset",
                "audit_sources",
                "system_safety_tests",
                "phase2_safety_tests",
                "phase3_cognitive_tests",
                "chroma_rebuild_canary",
                "smoke_test",
                "phase4_product_tests",
                "phase5_god_features_tests",
                "scale_benchmark_1500",
            },
        )

    def test_launcher_and_bootstrap_script_paths_exist_and_setup_sh_is_lf_only(self):
        launcher_files = (
            "run_dashboard.ps1",
            "run_daemon.ps1",
            "run_night_shift.ps1",
            "setup.ps1",
            "setup.sh",
        )
        scripts_dir = ROOT / "scripts"
        pattern = re.compile(r"scripts[\\/]([A-Za-z0-9_.-]+\.py)")

        for launcher_name in launcher_files:
            launcher_path = ROOT / launcher_name
            self.assertTrue(launcher_path.is_file(), f"Missing launcher file: {launcher_name}")
            text = launcher_path.read_text(encoding="utf-8")
            referenced_scripts = pattern.findall(text)
            self.assertTrue(
                referenced_scripts,
                f"Expected at least one scripts/*.py reference in {launcher_name}",
            )
            for script_basename in referenced_scripts:
                target = scripts_dir / script_basename
                self.assertTrue(
                    target.is_file(),
                    f"{launcher_name} references missing script: scripts/{script_basename}",
                )

        setup_sh_bytes = (ROOT / "setup.sh").read_bytes()
        self.assertEqual(setup_sh_bytes.count(b"\r"), 0, "setup.sh must use LF line endings (zero \\r bytes)")


class IndependentAuditReconciliationTests(unittest.TestCase):
    def test_no_legacy_powershell_or_shell_bypass_of_cas_or_grounding(self):
        refinery_ps1 = (ROOT / "ollama_refinery.ps1").read_text(encoding="utf-8")
        chat_ps1 = (ROOT / "chat_with_memory.ps1").read_text(encoding="utf-8")

        self.assertNotIn("Set-Content", refinery_ps1)
        self.assertNotIn("api/generate", refinery_ps1)
        self.assertIn("scripts/ollama_refinery.py", refinery_ps1)

        self.assertNotIn("api/generate", chat_ps1)
        self.assertNotIn("Search-Vault", chat_ps1)
        self.assertIn("chat_with_memory.py", chat_ps1)

    def test_merged_atoms_are_permanently_terminal_against_demote_edit_and_cycles(self):
        from atom_mutator import (
            ReviewTransitionError,
            apply_atom_edit,
            apply_atom_merge,
            apply_review_transition,
            approved_revision_is_current,
        )

        helper = AtomMergeTests()
        with tempfile.TemporaryDirectory() as tmp:
            brain, atoms, survivor, superseded = helper._fixture(Path(tmp))
            helper._merge(brain, atoms, survivor, superseded)

            # Merged atom rejects demote, quarantine, reject, approve, AND edit
            for action in ("demote", "quarantine", "reject", "approve"):
                with self.assertRaisesRegex(ReviewTransitionError, "Merged or superseded"):
                    apply_review_transition(
                        atom_path=str(superseded),
                        action=action,
                        expected_sha256=hashlib.sha256(superseded.read_bytes()).hexdigest(),
                        actor="owner-test",
                        decision_log_path=str(brain / "0-raw" / "decision.jsonl"),
                        brain_dir=str(brain),
                        atoms_dir=str(atoms),
                    )
            with self.assertRaisesRegex(ReviewTransitionError, "Merged or superseded"):
                apply_atom_edit(
                    atom_path=str(superseded),
                    edited_content=superseded.read_text(encoding="utf-8").replace("superseded_by:", "tags:"),
                    expected_sha256=hashlib.sha256(superseded.read_bytes()).hexdigest(),
                    actor="owner-test",
                    decision_log_path=str(brain / "0-raw" / "decision.jsonl"),
                    brain_dir=str(brain),
                    atoms_dir=str(atoms),
                )

            # Transitive cycle rejection: A supersedes B, B supersedes C => C cannot supersede A
            atom_c = atoms / "atom-c.md"
            atom_c.write_text(
                survivor.read_text(encoding="utf-8").replace("id: survivor", "id: atom-c").replace('  - "superseded.md"\n', ""),
                encoding="utf-8",
            )
            # Merge survivor (A) into atom_c (so atom_c supersedes survivor, which supersedes superseded)
            apply_atom_merge(
                survivor_path=str(atom_c),
                survivor_expected_sha256=hashlib.sha256(atom_c.read_bytes()).hexdigest(),
                superseded_path=str(survivor),
                superseded_expected_sha256=hashlib.sha256(survivor.read_bytes()).hexdigest(),
                actor="owner-test",
                rationale="Chained merge test.",
                decision_log_path=str(brain / "0-raw" / "decision.jsonl"),
                history_dir=str(atoms / ".history"),
                brain_dir=str(brain),
                atoms_dir=str(atoms),
            )
            self.assertFalse(
                approved_revision_is_current(
                    str(survivor),
                    decision_log_path=str(brain / "0-raw" / "decision.jsonl"),
                    brain_dir=str(brain),
                    atoms_dir=str(atoms),
                )
            )

    def test_epistemic_contract_sanitization_and_strict_mode_isolation(self):
        import json
        from answer_policy import AnswerRequest, build_grounding_prompt, validate_standard_answer
        from atom_mutator import apply_review_transition
        from memory_format import normalize_memory_document

        # 1. Forged epistemic_contract is stripped during normalization
        forged = (
            "---\nid: forged-pref\nepistemic_label: claim\nepistemic_contract: injected_contract\n---\n\n"
            "## Claim\nEmpirical statement.\n\n## Evidence\nEmpirical statement quote.\n"
        )
        normalized = normalize_memory_document(forged, "source.md", current_date="2026-11-15")
        self.assertNotIn("epistemic_contract", normalized)

        # 2. Strict standard vs preference mode isolation
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            atoms = brain / "2-atoms"
            source = brain / "0-raw" / "pref-source.md"
            atoms.mkdir(parents=True)
            source.parent.mkdir(parents=True)
            source.write_text("Owner prefers 4-space indentation in Python modules.\n", encoding="utf-8")

            pref_atom = atoms / "indent-pref.md"
            pref_atom.write_text(
                "---\nid: indent-pref\ntype: atom\nmemory_type: semantic\nsources:\n  - 0-raw/pref-source.md\n"
                "origin: user_local\ncitation_integrity: missing\nclaim_state: candidate\n"
                "epistemic_label: preference\nepistemic_contract: forged_value\nreview_status: pending\n---\n\n"
                "## Claim\nUse 4-space indentation in Python modules.\n\n"
                "## Evidence\nOwner prefers 4-space indentation in Python modules.\n",
                encoding="utf-8",
            )
            decision_log = brain / "0-raw" / "archive" / "decision-record.jsonl"
            apply_review_transition(
                action="approve",
                atom_path=str(pref_atom),
                expected_sha256=hashlib.sha256(pref_atom.read_bytes()).hexdigest(),
                actor="owner-test",
                decision_log_path=str(decision_log),
                brain_dir=str(brain),
                atoms_dir=str(atoms),
            )
            approved_text = pref_atom.read_text(encoding="utf-8")
            self.assertIn("epistemic_contract: owner_subjective_preference", approved_text)
            self.assertNotIn("forged_value", approved_text)

            retrieved = [{"filename": pref_atom.name, "content": approved_text}]
            std_prompt = build_grounding_prompt(AnswerRequest(query="indentation?", mode="standard"), retrieved)
            pref_prompt = build_grounding_prompt(AnswerRequest(query="indentation?", mode="preference"), retrieved)
            self.assertIn("[]", std_prompt)
            self.assertIn("Use 4-space indentation in Python modules.", pref_prompt)

            draft = json.dumps({"claims": ["Use 4-space indentation in Python modules."]})
            blocked_in_std = validate_standard_answer(draft, retrieved, atoms_dir=atoms, mode="standard")
            self.assertFalse(blocked_in_std.answerable)

            allowed_in_pref = validate_standard_answer(draft, retrieved, atoms_dir=atoms, mode="preference")
            self.assertTrue(allowed_in_pref.answerable)
            self.assertIn("[Preference]", allowed_in_pref.text)
            self.assertIn("[Owner Preference / Non-Factual (owner_subjective_preference)]", allowed_in_pref.text)

    def test_rrf_pool_depth_missing_chroma_degradation_and_manifest_completeness(self):
        from index_manifest import build_index_manifest, index_manifest_is_current
        from lexical_store import rebuild_lexical_index
        from recall import (
            RecallOptions,
            consume_last_degradation_warning,
            search_hybrid_rrf_memory,
        )
        from vector_store import get_vector_collection

        with tempfile.TemporaryDirectory() as tmp:
            atoms = Path(tmp) / "2-atoms"
            atoms.mkdir()
            (atoms / "a.md").write_text(
                "---\nid: a\ntype: atom\nmemory_type: semantic\nclaim_state: human_reviewed\nreview_status: accepted\n---\n\n"
                "## Claim\nConsensus retrieval target alpha beta.\n\n## Evidence\nConsensus retrieval target alpha beta.\n",
                encoding="utf-8",
            )
            # Manifest with indexed_count < atom_count must be rejected as non-current
            incomplete = build_index_manifest(
                atoms_dir=str(atoms),
                collection_name="atoms",
                embedding_model="nomic-embed-text",
                embedding_dimensions=768,
                indexed_count=0,
            )
            self.assertFalse(index_manifest_is_current(incomplete, str(atoms)))

            # Read-only vector probe on empty store must NOT create chroma.sqlite3
            empty_store = Path(tmp) / "empty-chroma"
            empty_store.mkdir()
            col = get_vector_collection(str(empty_store), create=False)
            self.assertFalse((empty_store / "chroma.sqlite3").exists())
            col.close() if hasattr(col, "close") else None

            # Missing .chroma directory triggers explicit FTS5 degradation banner in RRF mode
            lex_path = Path(tmp) / "lexical.sqlite3"
            rebuild_lexical_index(str(atoms), str(lex_path))
            from unittest.mock import patch
            with patch("recall.approved_revision_is_current", return_value=True):
                consume_last_degradation_warning()
                direct, _ = search_hybrid_rrf_memory(
                    "consensus retrieval target alpha",
                    RecallOptions(n_results=3, retrieval_mode="rrf"),
                    atoms_dir=str(atoms),
                    index_path=str(lex_path),
                    chroma_path=str(Path(tmp) / "nonexistent-chroma"),
                )
                banner = consume_last_degradation_warning()
                self.assertEqual([r.filename for r in direct], ["a.md"])
                self.assertIsNotNone(banner)
                self.assertIn("Dense vector store directory is missing", banner)

    def test_scout_rejects_nat64_and_ipv4_mapped_loopback_and_packaging_extras_exist(self):
        import tomllib
        from ollama_scout import is_public_ip_address

        self.assertTrue(is_public_ip_address("8.8.8.8"))
        self.assertFalse(is_public_ip_address("64:ff9b::7f00:1"))
        self.assertFalse(is_public_ip_address("64:ff9b:1::1"))
        self.assertFalse(is_public_ip_address("::ffff:127.0.0.1"))
        self.assertFalse(is_public_ip_address("127.0.0.1"))

        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        extras = set(pyproject.get("project", {}).get("optional-dependencies", {}).keys())
        for required_extra in ("dashboard", "online", "mcp", "scout", "watchdog", "evaluation", "dev"):
            self.assertIn(required_extra, extras)

        playbook = (ROOT / "docs" / "recovery-and-operations-playbook.md").read_text(encoding="utf-8")
        self.assertNotIn("rebuild_vector_index.py", playbook)
        self.assertIn("scripts/rebuild_vectors.py", playbook)
        self.assertIn("scripts/review_atom.py", playbook)
        self.assertIn("scripts/dashboard_auth.py set-password", playbook)
        self.assertIn("--runs 5", playbook)


if __name__ == "__main__":
    unittest.main()
