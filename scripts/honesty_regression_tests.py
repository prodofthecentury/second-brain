# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Behaviour checks for the POSIX permission bug, owner grants, and source review.

These tests exercise the functions. They do not pass by searching source files
for a keyword.
"""



from __future__ import annotations

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


import ast
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))

from scripts.owner_attestation import (  # noqa: E402
    OwnerAttestationError,
    enroll_owner,
    issue_grant,
    verify_grant,
)
from scripts.refinery_targets import intake_status  # noqa: E402
from scripts.review_atom import main as review_main  # noqa: E402
from scripts.security_fs import restrict_security_file_permissions  # noqa: E402
from scripts.source_review import (  # noqa: E402
    approve_clean_sources,
    approve_sources,
    list_source_queue,
    search_approved_sources,
)


class DirectoryPermissionTests(unittest.TestCase):
    def test_restricting_a_directory_leaves_it_writable(self) -> None:
        from scripts.mobile_vault_client import _save_registry_unlocked

        with tempfile.TemporaryDirectory(prefix="sb-perm-") as temp:
            root = Path(temp) / "security"
            root.mkdir()
            restrict_security_file_permissions(root)
            if os.name != "nt":
                self.assertTrue(root.stat().st_mode & stat.S_IXUSR)
                self.assertFalse(root.stat().st_mode & stat.S_IRWXG)
                self.assertFalse(root.stat().st_mode & stat.S_IRWXO)
            probe = root / "token"
            probe.write_text("still-writable\n", encoding="utf-8")
            restrict_security_file_permissions(probe)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(probe.stat().st_mode), 0o600)
            (root / "another").write_text("second\n", encoding="utf-8")

            staging = root / "models" / ".staging-cross-encoder"
            staging.mkdir(parents=True)
            restrict_security_file_permissions(staging)
            (staging / "model.onnx").write_bytes(b"weights")
            self.assertEqual((staging / "model.onnx").read_bytes(), b"weights")

            saved = _save_registry_unlocked({"schema_version": 1, "clients": []}, state_dir=root)
            self.assertTrue(saved.is_file())
            self.assertIn("clients", saved.read_text(encoding="utf-8"))


class OwnerGrantTests(unittest.TestCase):
    def test_passphrase_grant_round_trip_and_cli_refuses_bare_actor(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb-owner-") as temp:
            enrolled = enroll_owner("Ada Lovelace", "correct horse battery", state_dir=temp)
            self.assertEqual(enrolled["actor"], "Ada Lovelace")
            token = issue_grant("correct horse battery", state_dir=temp)
            verified = verify_grant(token, state_dir=temp)
            self.assertEqual(verified["method"], "owner_ed25519_grant")
            self.assertEqual(verified["credential_fingerprint"], enrolled["fingerprint"])
            with self.assertRaises(OwnerAttestationError):
                issue_grant("not-the-passphrase", state_dir=temp)
            with self.assertRaises(OwnerAttestationError):
                from scripts.owner_attestation import _clean_actor

                _clean_actor("owner-corpus-seed")

        previous = os.environ.pop("BRAIN_OWNER_GRANT", None)
        try:
            code = review_main(
                [
                    "transition",
                    "approve",
                    "missing.md",
                    "--expected-sha256",
                    "a" * 64,
                    "--actor",
                    "Ada Lovelace",
                ]
            )
        finally:
            if previous is not None:
                os.environ["BRAIN_OWNER_GRANT"] = previous
        self.assertEqual(code, 1)

    def test_library_approval_is_labeled_unattested(self) -> None:
        from scripts.atom_mutator import apply_review_transition, content_sha256

        with tempfile.TemporaryDirectory(prefix="sb-unattested-") as temp:
            brain = Path(temp) / "brain"
            atoms = brain / "2-atoms"
            raw = brain / "0-raw"
            atoms.mkdir(parents=True)
            raw.mkdir()
            (raw / "src-note.md").write_text("Primary record: The folder stays writable.\n", encoding="utf-8")
            atom = atoms / "note.md"
            atom.write_text(
                "---\n"
                "id: note\n"
                "type: atom\n"
                "memory_type: semantic\n"
                "epistemic_label: claim\n"
                "sources:\n"
                "  - 0-raw/src-note.md\n"
                "review_status: pending\n"
                "claim_state: candidate\n"
                "citation_integrity: missing\n"
                "---\n\n"
                "## Claim\n"
                "The folder stays writable.\n\n"
                "## Evidence\n"
                "The folder stays writable.\n",
                encoding="utf-8",
            )
            decision = apply_review_transition(
                action="reject",
                atom_path=str(atom),
                expected_sha256=content_sha256(atom.read_bytes()),
                actor="owner",
                brain_dir=str(brain),
                atoms_dir=str(atoms),
            )
            recorded = decision["attestation"]
            self.assertEqual(recorded["method"], "unattested_library_call")
            self.assertEqual(recorded["human_approval"], "false")
            self.assertEqual(recorded["actor"], "owner")
            self.assertIn("review_status: rejected", atom.read_text(encoding="utf-8"))


class SourceReviewTests(unittest.TestCase):
    def test_unapproved_sources_stay_unsearchable_until_owner_grant(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb-src-") as temp:
            brain = Path(temp) / "brain"
            security = Path(temp) / "security"
            (brain / "sources").mkdir(parents=True)
            (brain / "0-raw").mkdir()
            phrase = "zephyrquartzcapture"
            article = brain / "sources" / "article.md"
            article.write_text(
                "---\norigin: web_remote\n---\n\n"
                f"The captured article mentions {phrase} in the body.\n",
                encoding="utf-8",
            )
            pointer = brain / "0-raw" / "article.intake"
            pointer.write_text(
                "source: sources/article.md\norigin: web_remote\nintake_status: quarantined\n",
                encoding="utf-8",
            )
            risky = brain / "sources" / "leaked.md"
            risky.write_text("A key AKIAIOSFODNN7EXAMPLE slipped into the clip.\n", encoding="utf-8")

            self.assertEqual(search_approved_sources(phrase, brain_dir=brain), [])
            queue = {item["filename"]: item for item in list_source_queue(brain)}
            self.assertFalse(queue["article.md"]["approved"])
            self.assertIn("secret_credential:aws_access_key", queue["leaked.md"]["risk_flags"])

            enroll_owner("Ada Lovelace", "correct horse battery", state_dir=security)
            grant = issue_grant("correct horse battery", state_dir=security)
            attestation = verify_grant(grant, state_dir=security)
            with self.assertRaises(Exception):
                approve_sources(
                    [{"filename": "article.md", "expected_sha256": queue["article.md"]["sha256"]}],
                    actor="Ada Lovelace",
                    attestation={"method": "owner_ed25519_grant", "actor": "Ada Lovelace", "grant": "not-a-grant"},
                    brain_dir=brain,
                    state_dir=security,
                )

            clean = approve_clean_sources(
                actor="Ada Lovelace",
                attestation=attestation,
                brain_dir=brain,
                state_dir=security,
            )
            names = [row["filename"] for row in clean["approved"]]
            self.assertEqual(names, ["article.md"])
            self.assertEqual(clean["skipped_risky"], ["leaked.md"])
            self.assertEqual(intake_status(pointer.read_text(encoding="utf-8")), "approved")

            hits = search_approved_sources(phrase, brain_dir=brain)
            self.assertEqual([hit["filename"] for hit in hits], ["article.md"])
            self.assertFalse(hits[0]["citable_claim"])
            self.assertEqual(hits[0]["epistemic_label"], "approved_source_capture")
            self.assertNotIn("leaked.md", [hit["filename"] for hit in hits])

            article.write_text(article.read_text(encoding="utf-8") + "Edited after approval.\n", encoding="utf-8")
            self.assertEqual(search_approved_sources(phrase, brain_dir=brain), [])


class McpDocCountTests(unittest.TestCase):
    def test_readme_and_policy_match_tool_names(self) -> None:
        tree = ast.parse((SCRIPTS / "mcp_server.py").read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(target, ast.Name) and target.id == "TOOL_NAMES" for target in node.targets):
                continue
            self.assertIsInstance(node.value, ast.Set)
            names = {elt.value for elt in node.value.elts if isinstance(elt, ast.Constant)}
        self.assertEqual(len(names), 36)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        policy = (ROOT / "docs" / "mcp-local-policy.md").read_text(encoding="utf-8")
        self.assertIn("36 MCP tools", readme)
        self.assertNotIn("27-Tool", readme)
        self.assertNotIn("31 MCP", readme)
        for name in names:
            self.assertIn(name, policy)


if __name__ == "__main__":
    unittest.main()
