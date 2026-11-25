# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Stage 4 Cognitive Core tests: Owner-promoted Executable Skills, closed-action sandbox runtime,
emergency kill-switch (pre-run & mid-flight), deterministic rollback, staleness cascade, and
hash-chained execution audit verification.
"""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _create_dir_link_or_junction(link_path: Path, target_dir: Path) -> bool:
    """Create a directory symlink or (on Windows without symlink privilege) an NTFS junction."""
    try:
        os.symlink(str(target_dir), str(link_path), target_is_directory=True)
        return True
    except OSError:
        if os.name == "nt":
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_path), str(target_dir)],
                capture_output=True,
                text=True,
                check=False,
            )
            return proc.returncode == 0 and os.path.lexists(str(link_path))
        return False


def _seed_atom(
    brain_dir: Path,
    *,
    filename: str,
    claim: str,
    evidence: str,
    memory_type: str = "procedural",
    contexts: list[str] | None = None,
    epistemic_label: str = "claim",
    approve: bool = True,
) -> tuple[Path, str]:
    from atom_mutator import apply_review_transition
    from evidence import find_evidence_anchor
    from lexical_store import rebuild_lexical_index

    raw_dir = brain_dir / "0-raw"
    atoms_dir = brain_dir / "2-atoms"
    playbooks_dir = brain_dir / "5-playbooks"
    skills_dir = brain_dir / "6-skills"
    experiences_dir = brain_dir / "4-experiences"
    sidecar_dir = brain_dir / ".sidecar"
    for d in (raw_dir, atoms_dir, playbooks_dir, skills_dir, experiences_dir, sidecar_dir):
        d.mkdir(parents=True, exist_ok=True)

    src_name = f"src-{filename}"
    src_path = raw_dir / src_name
    src_path.write_text(
        f"Verified runbook (2026-11-20): {evidence}\n",
        encoding="utf-8",
    )
    ctx_list = contexts if contexts is not None else ["project:second_brain", "windows", "offline"]
    ctx_yaml = "\n".join(f"  - {c}" for c in ctx_list)
    draft = (
        "---\n"
        f"id: {Path(filename).stem}\n"
        "type: atom\n"
        f"memory_type: {memory_type}\n"
        f"epistemic_label: {epistemic_label}\n"
        "created: 2026-11-18\n"
        "last-reviewed: 2026-11-20\n"
        "confidence: 0.90\n"
        "importance: 0.85\n"
        "sources:\n"
        f"  - 0-raw/{src_name}\n"
        "contexts:\n"
        f"{ctx_yaml}\n"
        "review_status: pending\n"
        "citation_integrity: missing\n"
        "---\n\n"
        "## Claim\n"
        f"{claim}\n\n"
        "## Evidence\n"
        f"{evidence}\n\n"
        "## Why It Matters\n"
        "Enforces deterministic Stage 4 executable governance.\n"
    )
    anchor = find_evidence_anchor(draft, str(brain_dir))
    parts = draft.split("---", 2)
    fm = parts[1]
    for k, v in anchor.items():
        fm += f"{k}: {v}\n"
    fm = fm.replace("citation_integrity: missing", "citation_integrity: anchored")
    atom_path = atoms_dir / filename
    atom_path.write_text(f"---{fm}---{parts[2]}", encoding="utf-8")

    if approve:
        apply_review_transition(
            action="approve",
            atom_path=str(atom_path),
            expected_sha256=hashlib.sha256(atom_path.read_bytes()).hexdigest(),
            actor="stage4-owner",
            decision_log_path=str(brain_dir / "0-raw" / "archive" / "decision-record.jsonl"),
            brain_dir=str(brain_dir),
            atoms_dir=str(atoms_dir),
        )
    else:
        rebuild_lexical_index(str(atoms_dir), str(sidecar_dir / "lexical.sqlite3"))

    return atom_path, hashlib.sha256(atom_path.read_bytes()).hexdigest()


def _seed_mature_playbook(
    brain_dir: Path,
    *,
    playbook_id: str,
    atom_filename: str,
    steps: list[str],
    citations: int = 3,
    successful_episodes: int = 1,
    contexts: list[str] | None = None,
) -> tuple[Path, str, dict[str, object]]:
    from cognitive_core import record_grounded_answer_telemetry, save_experience_record
    from skill_playbooks import promote_skill_playbook

    claim_body = "\n".join(f"{idx}. {s}" for idx, s in enumerate(steps, start=1))
    evidence_body = " ".join(steps)
    atom_path, atom_sha = _seed_atom(
        brain_dir,
        filename=atom_filename,
        claim=claim_body,
        evidence=evidence_body,
        memory_type="procedural",
        contexts=contexts,
        approve=True,
    )
    telemetry_db = str(brain_dir / ".sidecar" / "telemetry.sqlite3")
    for _ in range(citations):
        record_grounded_answer_telemetry([atom_filename], abstained=False, db_path=telemetry_db)

    pb_res = promote_skill_playbook(
        playbook_id=playbook_id,
        title=f"Playbook {playbook_id}",
        backing_atoms=[(atom_filename, atom_sha)],
        trigger=f"When executing {playbook_id}",
        preconditions=["Vault integrity verified"],
        steps=steps,
        expected_outputs=["Verified sandbox artifacts and SHA-256 digests"],
        failure_recovery=["Deterministically roll back sandbox workspace"],
        actor="stage4-owner",
        allow_early_owner_override=(citations < 3),
        rationale="Test playbook bootstrap" if citations < 3 else "",
        brain_dir=str(brain_dir),
        atoms_dir=str(brain_dir / "2-atoms"),
        playbooks_dir=str(brain_dir / "5-playbooks"),
        telemetry_db_path=telemetry_db,
    )

    for ep in range(successful_episodes):
        save_experience_record(
            goal=f"Prior drill {ep + 1} for {playbook_id}",
            result_summary="Completed procedure drill cleanly.",
            success=True,
            referenced_atoms=[atom_filename],
            tools_used=[f"playbook:{playbook_id}"],
            experiences_dir=str(brain_dir / "4-experiences"),
            inbox_dir=str(brain_dir / "0-raw" / "inbox"),
            brain_dir=str(brain_dir),
        )

    return atom_path, atom_sha, pb_res


class Stage4IsolatedTestCase(unittest.TestCase):
    """Isolate security state, tokens, and environment variables per test."""

    def setUp(self) -> None:
        super().setUp()
        self._stack = ExitStack()
        self.addCleanup(self._stack.close)
        self.root_tmp = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self.sec_dir = self.root_tmp / "security"
        self.sec_dir.mkdir(parents=True, exist_ok=True)
        self._stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "BRAIN_SECURITY_STATE_DIR": str(self.sec_dir),
                    "BRAIN_HOST_POLICY_FILE": str(self.sec_dir / "host-sharing-policy.json"),
                },
                clear=False,
            )
        )


class CandidateReadinessAndPromotionGateTests(Stage4IsolatedTestCase):
    def test_readiness_gates_actor_checks_ast_inspections_dlp_and_cas_updates(self) -> None:
        from cognitive_core import record_grounded_answer_telemetry
        from executable_skills import (
            ExecutableSkillSecretBlockedError,
            ExecutableSkillValidationError,
            inspect_executable_skill_candidates,
            promote_playbook_to_executable_skill,
            skill_visible_in_scope,
            verify_executable_skill_integrity,
        )
        from skill_playbooks import PlaybookValidationError, promote_skill_playbook

        brain = self.root_tmp / "brain"
        telemetry_db = str(brain / ".sidecar" / "telemetry.sqlite3")

        # Unapproved & non-procedural atoms are blocked at the Playbook foundation layer
        _, unapproved_sha = _seed_atom(
            brain,
            filename="unapproved-proc.md",
            claim="Run local audit check.",
            evidence="Run local audit check.",
            memory_type="procedural",
            approve=False,
        )
        _, semantic_sha = _seed_atom(
            brain,
            filename="semantic-fact.md",
            claim="SQLite FTS5 uses BM25 ranking.",
            evidence="SQLite FTS5 uses BM25 ranking.",
            memory_type="semantic",
            approve=True,
        )
        with self.assertRaises(PlaybookValidationError):
            promote_skill_playbook(
                playbook_id="bad-unapproved-pb",
                title="Bad Unapproved",
                backing_atoms=[("unapproved-proc.md", unapproved_sha)],
                trigger="Trigger",
                preconditions=["Pre"],
                steps=["Run local audit check."],
                expected_outputs=["Out"],
                failure_recovery=["Rollback"],
                actor="owner",
                allow_early_owner_override=True,
                rationale="Attempt unapproved",
                brain_dir=str(brain),
            )
        with self.assertRaises(PlaybookValidationError):
            promote_skill_playbook(
                playbook_id="bad-semantic-pb",
                title="Bad Semantic",
                backing_atoms=[("semantic-fact.md", semantic_sha)],
                trigger="Trigger",
                preconditions=["Pre"],
                steps=["SQLite FTS5 uses BM25 ranking."],
                expected_outputs=["Out"],
                failure_recovery=["Rollback"],
                actor="owner",
                allow_early_owner_override=True,
                rationale="Attempt semantic",
                brain_dir=str(brain),
            )

        # Seed Playbook with 3 citations, 0 episodes -> NOT yet eligible_without_override!
        _, _, pb = _seed_mature_playbook(
            brain,
            playbook_id="audit-sources-pb",
            atom_filename="audit-sources-proc.md",
            steps=[
                "Audit canonical atom source anchors cleanly.",
                "Assert source audit JSON report is non-empty.",
            ],
            citations=3,
            successful_episodes=0,
            contexts=["project:second_brain"],
        )

        cands = inspect_executable_skill_candidates(brain_dir=str(brain), security_dir=self.sec_dir)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["playbook_id"], "audit-sources-pb")
        self.assertFalse(cands[0]["eligible_without_override"])
        self.assertEqual(cands[0]["successful_episodes"], 0)
        self.assertEqual(cands[0]["min_grounded_citations"], 3)

        valid_steps = [
            {"action_type": "vault_audit", "audit_kind": "sources", "output_file": "sources.json"},
            {"action_type": "workspace_assert", "rel_path": "sources.json", "op": "non_empty"},
        ]

        # 1. Without override, 3 citations + 0 episodes is rejected!
        with self.assertRaises(ExecutableSkillValidationError):
            promote_playbook_to_executable_skill(
                skill_id="audit-sources-skill",
                playbook_id="audit-sources-pb",
                expected_playbook_sha256=str(pb["sha256"]),
                step_actions=valid_steps,
                actor="owner",
                brain_dir=str(brain),
                security_dir=self.sec_dir,
            )

        # 2. Override without rationale is rejected!
        with self.assertRaises(ExecutableSkillValidationError):
            promote_playbook_to_executable_skill(
                skill_id="audit-sources-skill",
                playbook_id="audit-sources-pb",
                expected_playbook_sha256=str(pb["sha256"]),
                step_actions=valid_steps,
                actor="owner",
                allow_early_owner_override=True,
                rationale="",
                brain_dir=str(brain),
                security_dir=self.sec_dir,
            )

        # 3. Reaching >=5 citations makes candidate eligible_without_override=True!
        for _ in range(2):
            record_grounded_answer_telemetry(["audit-sources-proc.md"], abstained=False, db_path=telemetry_db)
        cands_mature = inspect_executable_skill_candidates(brain_dir=str(brain), security_dir=self.sec_dir)
        self.assertTrue(cands_mature[0]["eligible_without_override"])

        # 4. Disallowed automated/agent actors are strictly rejected
        for bad_actor in ("auto", "system", "llm", "model", "ollama", "assistant", "agent", ""):
            with self.assertRaises(ExecutableSkillValidationError):
                promote_playbook_to_executable_skill(
                    skill_id="audit-sources-skill",
                    playbook_id="audit-sources-pb",
                    expected_playbook_sha256=str(pb["sha256"]),
                    step_actions=valid_steps,
                    actor=bad_actor,
                    brain_dir=str(brain),
                    security_dir=self.sec_dir,
                )

        # 5. Step count mismatch (1 step instead of 2) is rejected
        with self.assertRaises(ExecutableSkillValidationError):
            promote_playbook_to_executable_skill(
                skill_id="audit-sources-skill",
                playbook_id="audit-sources-pb",
                expected_playbook_sha256=str(pb["sha256"]),
                step_actions=[valid_steps[0]],
                actor="owner",
                brain_dir=str(brain),
                security_dir=self.sec_dir,
            )

        # 6. Unknown action_type outside closed catalog is rejected
        with self.assertRaises(ExecutableSkillValidationError):
            promote_playbook_to_executable_skill(
                skill_id="audit-sources-skill",
                playbook_id="audit-sources-pb",
                expected_playbook_sha256=str(pb["sha256"]),
                step_actions=[
                    {"action_type": "shell_command", "cmd": "whoami"},
                    valid_steps[1],
                ],
                actor="owner",
                brain_dir=str(brain),
                security_dir=self.sec_dir,
            )

        # 7. AST static analysis blocks forbidden imports, dynamic execution, and {input.*} interpolation
        forbidden_scripts = [
            "import ctypes\nprint(ctypes)\n",
            "import urllib.request\n",
            "import httpx\n",
            "import requests\n",
            "from http.client import HTTPConnection\n",
            "import winreg\n",
            "import multiprocessing\n",
            "import subprocess\n",
            "eval('1 + 1')\n",
            "__import__('ctypes')\n",
            "import os\nos.system('echo pwned')\n",
            "val = '{input.target}'\n",
        ]
        for bad_code in forbidden_scripts:
            with self.assertRaises(ExecutableSkillValidationError, msg=f"Should reject script: {bad_code!r}"):
                promote_playbook_to_executable_skill(
                    skill_id="audit-sources-skill",
                    playbook_id="audit-sources-pb",
                    expected_playbook_sha256=str(pb["sha256"]),
                    step_actions=[
                        {"action_type": "sandbox_python", "script_body": bad_code},
                        valid_steps[1],
                    ],
                    input_schema={"target": {"type": "string", "required": False}},
                    actor="owner",
                    brain_dir=str(brain),
                    security_dir=self.sec_dir,
                )

        # 8. DLP secret inside script_body is blocked fail-closed!
        fake_oai_key = "sk-proj-" + ("A1b2C3d4E5f6" * 3)
        with self.assertRaises(ExecutableSkillSecretBlockedError):
            promote_playbook_to_executable_skill(
                skill_id="audit-sources-skill",
                playbook_id="audit-sources-pb",
                expected_playbook_sha256=str(pb["sha256"]),
                step_actions=[
                    {"action_type": "sandbox_python", "script_body": f"KEY = {fake_oai_key!r}\n"},
                    valid_steps[1],
                ],
                actor="owner",
                brain_dir=str(brain),
                security_dir=self.sec_dir,
            )

        # 9. Valid promotion + CAS update backup + scope visibility check
        promoted = promote_playbook_to_executable_skill(
            skill_id="audit-sources-skill",
            playbook_id="audit-sources-pb",
            expected_playbook_sha256=str(pb["sha256"]),
            step_actions=valid_steps,
            timeout_seconds=0.1,  # clamped to 1.0
            max_output_bytes=100,  # clamped to 1024
            actor="owner",
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        rep = verify_executable_skill_integrity(
            "audit-sources-skill", brain_dir=str(brain), security_dir=self.sec_dir
        )
        self.assertTrue(rep.valid)
        self.assertEqual(rep.timeout_seconds, 1.0)
        self.assertEqual(rep.max_output_bytes, 1024)
        self.assertTrue(skill_visible_in_scope(rep, ("project:second_brain",)))
        self.assertFalse(skill_visible_in_scope(rep, ("project:other_secret",)))

        # Updating existing skill without expected_skill_sha256 fails CAS; with matching SHA backs up .history/
        with self.assertRaises(ExecutableSkillValidationError):
            promote_playbook_to_executable_skill(
                skill_id="audit-sources-skill",
                playbook_id="audit-sources-pb",
                expected_playbook_sha256=str(pb["sha256"]),
                step_actions=valid_steps,
                timeout_seconds=10.0,
                actor="owner",
                brain_dir=str(brain),
                security_dir=self.sec_dir,
            )

        updated = promote_playbook_to_executable_skill(
            skill_id="audit-sources-skill",
            playbook_id="audit-sources-pb",
            expected_playbook_sha256=str(pb["sha256"]),
            expected_skill_sha256=str(promoted["sha256"]),
            step_actions=valid_steps,
            timeout_seconds=12.0,
            actor="owner",
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        self.assertNotEqual(updated["sha256"], promoted["sha256"])
        hist_backup = brain / "6-skills" / ".history" / f"audit-sources-skill.{str(promoted['sha256'])[:12]}.md"
        self.assertTrue(hist_backup.is_file())


class EndToEndExecutionAndAuditChainTests(Stage4IsolatedTestCase):
    def test_promote_verify_dry_run_live_execution_audit_chain_and_experience_record(self) -> None:
        from executable_skills import (
            ExecutableSkillSecretBlockedError,
            execute_promoted_skill,
            inspect_executable_skill_candidates,
            list_skill_executions,
            promote_playbook_to_executable_skill,
            verify_executable_skill_integrity,
            verify_skill_execution_audit_chain,
        )

        brain = self.root_tmp / "brain"
        _seed_atom(
            brain,
            filename="triage-knowledge.md",
            claim="Worker crash triage collects correlation structured JSON logs from service shards.",
            evidence="Worker crash triage collects correlation structured JSON logs from service shards.",
            memory_type="semantic",
            contexts=["project:second_brain"],
            approve=True,
        )

        steps = [
            "Recall verified incident knowledge for the target service.",
            "Write target service metadata manifest inside sandbox workspace.",
            "Aggregate recalled evidence and runtime inputs via sandboxed Python.",
            "Verify aggregated report status equals verified.",
            "Run read-only vault ledger and contradiction audits.",
        ]
        _, _, pb = _seed_mature_playbook(
            brain,
            playbook_id="triage-pipeline-pb",
            atom_filename="triage-pipeline-proc.md",
            steps=steps,
            citations=3,
            successful_episodes=1,
            contexts=["project:second_brain"],
        )

        cands = inspect_executable_skill_candidates(brain_dir=str(brain), security_dir=self.sec_dir)
        self.assertTrue(cands[0]["eligible_without_override"])
        self.assertEqual(cands[0]["successful_episodes"], 1)

        script_code = (
            "import hashlib\n"
            "import json\n"
            "import os\n"
            "from pathlib import Path\n\n"
            "inputs = json.loads(Path(os.environ['SB_SANDBOX_INPUTS']).read_text(encoding='utf-8'))\n"
            "recall_doc = json.loads(Path('recall.json').read_text(encoding='utf-8'))\n"
            "meta_txt = Path('meta/service.txt').read_text(encoding='utf-8').strip()\n"
            "Path('reports').mkdir(parents=True, exist_ok=True)\n"
            "summary = {\n"
            "    'status': 'verified',\n"
            "    'service': inputs['service'],\n"
            "    'max_items': inputs['max_items'],\n"
            "    'recalled_count': recall_doc['count'],\n"
            "    'meta_sha256': hashlib.sha256(meta_txt.encode('utf-8')).hexdigest(),\n"
            "}\n"
            "Path('reports/summary.json').write_text(json.dumps(summary, sort_keys=True), encoding='utf-8')\n"
            "print('processed service:', inputs['service'])\n"
        )

        promoted = promote_playbook_to_executable_skill(
            skill_id="triage-pipeline-skill",
            playbook_id="triage-pipeline-pb",
            expected_playbook_sha256=str(pb["sha256"]),
            input_schema={
                "service": {
                    "type": "string",
                    "required": True,
                    "pattern": r"^[a-z0-9_-]{2,32}$",
                    "max_length": 32,
                },
                "max_items": {
                    "type": "integer",
                    "required": False,
                    "default": 4,
                },
            },
            step_actions=[
                {
                    "action_type": "vault_recall",
                    "query": "Worker crash triage correlation structured JSON logs {input.service}",
                    "n_results": 5,
                    "output_file": "recall.json",
                },
                {
                    "action_type": "workspace_write",
                    "rel_path": "meta/service.txt",
                    "content": "service={input.service}\nmax_items={input.max_items}\n",
                },
                {
                    "action_type": "sandbox_python",
                    "script_body": script_code,
                    "output_file": "reports/summary.json",
                },
                {
                    "action_type": "workspace_assert",
                    "rel_path": "reports/summary.json",
                    "op": "json_key_equals",
                    "json_key": "status",
                    "expected": "verified",
                },
                {
                    "action_type": "vault_audit",
                    "audit_kind": "contradictions",
                    "output_file": "audits/contradictions.json",
                },
            ],
            actor="stage4-owner",
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )

        rep = verify_executable_skill_integrity(
            "triage-pipeline-skill", brain_dir=str(brain), security_dir=self.sec_dir
        )
        self.assertTrue(rep.valid)
        self.assertEqual(rep.sha256, promoted["sha256"])

        # Runtime DLP secret in input is blocked fail-closed AND recorded in execution audit chain
        bad_secret = "ghp_" + ("x9Y8z7W6v5U4" * 3)
        with self.assertRaises(ExecutableSkillSecretBlockedError):
            execute_promoted_skill(
                "triage-pipeline-skill",
                inputs={"service": bad_secret},
                actor="stage4-owner",
                brain_dir=str(brain),
                security_dir=self.sec_dir,
            )

        # Dry-run verifies plan without spawning subprocesses
        dry = execute_promoted_skill(
            "triage-pipeline-skill",
            inputs={"service": "shards"},
            actor="stage4-owner",
            dry_run=True,
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        self.assertEqual(dry["status"], "dry_run_verified")
        self.assertTrue(dry["dry_run"])
        self.assertEqual(len(dry["step_preview"]), 5)
        self.assertEqual(dry["validated_inputs"]["max_items"], 4)

        # Full live sandboxed execution
        out_copy = self.root_tmp / "sandbox_export"
        exp_before = set(os.listdir(brain / "4-experiences"))
        live = execute_promoted_skill(
            "triage-pipeline-skill",
            inputs={"service": "shards", "max_items": 7},
            actor="stage4-owner",
            dry_run=False,
            keep_sandbox_copy_dir=str(out_copy),
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        self.assertEqual(live["status"], "completed")
        self.assertTrue(live["success"])
        self.assertFalse(live["killed_by_switch"])
        self.assertFalse(live["rollback_executed"])
        self.assertIn("recall.json", live["artifact_hashes"])
        self.assertIn("meta/service.txt", live["artifact_hashes"])
        self.assertIn("reports/summary.json", live["artifact_hashes"])
        self.assertIn("audits/contradictions.json", live["artifact_hashes"])

        summary_doc = json.loads((out_copy / "reports" / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary_doc["status"], "verified")
        self.assertEqual(summary_doc["service"], "shards")
        self.assertEqual(summary_doc["max_items"], 7)
        self.assertGreaterEqual(summary_doc["recalled_count"], 1)

        # Verify ExperienceRecord was persisted in 4-experiences/ linking backing atoms & executable_skill tool
        exp_after = set(os.listdir(brain / "4-experiences"))
        new_exp_files = exp_after - exp_before
        self.assertEqual(len(new_exp_files), 1)
        exp_text = (brain / "4-experiences" / next(iter(new_exp_files))).read_text(encoding="utf-8")
        self.assertIn("type: experience", exp_text)
        self.assertIn("success: true", exp_text)
        self.assertIn("executable_skill:triage-pipeline-skill", exp_text)
        self.assertIn("triage-pipeline-proc.md", exp_text)

        # Verify hash-chained skill-executions.jsonl audit log (3 entries: denied, dry_run, completed)
        chain = verify_skill_execution_audit_chain(str(brain))
        self.assertTrue(chain["valid"])
        self.assertEqual(chain["entry_count"], 3)
        rows = list_skill_executions(str(brain), skill_id="triage-pipeline-skill")
        self.assertEqual([r["execution_index"] for r in rows], [1, 2, 3])
        self.assertEqual(rows[0]["prev_execution_sha256"], "0" * 64)
        self.assertEqual(rows[1]["prev_execution_sha256"], rows[0]["execution_sha256"])
        self.assertEqual(rows[2]["prev_execution_sha256"], rows[1]["execution_sha256"])
        self.assertEqual([r["status"] for r in rows], ["denied", "dry_run_verified", "completed"])


class SandboxSecurityIsolationTests(Stage4IsolatedTestCase):
    def test_env_secret_stripping_network_block_path_confinement_and_timeout_rollback(self) -> None:
        from executable_skills import (
            ExecutableSkillValidationError,
            execute_promoted_skill,
            promote_playbook_to_executable_skill,
        )

        brain = self.root_tmp / "brain"

        # 1. Path traversal `../` in workspace_write / output_file is rejected at promotion time
        _, _, pb_one = _seed_mature_playbook(
            brain,
            playbook_id="single-step-pb",
            atom_filename="single-step-proc.md",
            steps=["Execute isolated sandbox verification step."],
            citations=3,
            successful_episodes=1,
        )
        for unsafe_rel in ("../escape.txt", "/etc/passwd", "C:\\Windows\\win.ini", "sub/../../out.txt", "inputs.json"):
            with self.assertRaises(ExecutableSkillValidationError):
                promote_playbook_to_executable_skill(
                    skill_id="bad-path-skill",
                    playbook_id="single-step-pb",
                    expected_playbook_sha256=str(pb_one["sha256"]),
                    step_actions=[
                        {"action_type": "workspace_write", "rel_path": unsafe_rel, "content": "x"}
                    ],
                    actor="stage4-owner",
                    brain_dir=str(brain),
                    security_dir=self.sec_dir,
                )

        # 2. Child subprocess CANNOT see host secrets in os.environ and runs in Python -I isolated mode
        env_probe_script = (
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "leaked = [k for k in os.environ if any(s in k.upper() for s in ('API_KEY', 'TOKEN', 'SECRET'))]\n"
            "report = {\n"
            "    'leaked_keys': leaked,\n"
            "    'isolated_flag': int(sys.flags.isolated),\n"
            "    'dont_write_bytecode': int(sys.flags.dont_write_bytecode),\n"
            "}\n"
            "Path('env_probe.json').write_text(json.dumps(report), encoding='utf-8')\n"
            "assert not leaked, f'Host secrets leaked: {leaked}'\n"
        )
        promote_playbook_to_executable_skill(
            skill_id="env-probe-skill",
            playbook_id="single-step-pb",
            expected_playbook_sha256=str(pb_one["sha256"]),
            step_actions=[
                {"action_type": "sandbox_python", "script_body": env_probe_script, "output_file": "env_probe.json"}
            ],
            actor="stage4-owner",
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        probe_out = self.root_tmp / "probe_out"
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "sk-proj-SuperSecretHostApiKeyValue123456789",
                "BRAIN_OWNER_TOKEN": "owner-secret-bearer-token-999",
                "AWS_SECRET_ACCESS_KEY": "aws-top-secret-value",
            },
            clear=False,
        ):
            res_env = execute_promoted_skill(
                "env-probe-skill",
                actor="stage4-owner",
                keep_sandbox_copy_dir=str(probe_out),
                brain_dir=str(brain),
                security_dir=self.sec_dir,
            )
        self.assertEqual(res_env["status"], "completed")
        probe_doc = json.loads((probe_out / "env_probe.json").read_text(encoding="utf-8"))
        self.assertEqual(probe_doc["leaked_keys"], [])
        self.assertEqual(probe_doc["isolated_flag"], 1)
        self.assertEqual(probe_doc["dont_write_bytecode"], 1)

        # 3. Network socket & filesystem escape blocked at runtime by `_guard/sitecustomize.py` + rollback!
        _, _, pb_two = _seed_mature_playbook(
            brain,
            playbook_id="two-step-pb",
            atom_filename="two-step-proc.md",
            steps=[
                "Stage preliminary artifact inside sandbox workspace.",
                "Run secondary sandboxed verification script.",
            ],
            citations=3,
            successful_episodes=1,
        )
        net_escape_script = (
            "import socket\n"
            "from pathlib import Path\n"
            "Path('uncommitted_net.txt').write_text('partial', encoding='utf-8')\n"
            "socket.create_connection(('127.0.0.1', 80), timeout=0.5)\n"
        )
        promote_playbook_to_executable_skill(
            skill_id="net-blocked-skill",
            playbook_id="two-step-pb",
            expected_playbook_sha256=str(pb_two["sha256"]),
            step_actions=[
                {"action_type": "workspace_write", "rel_path": "staged.txt", "content": "step1"},
                {"action_type": "sandbox_python", "script_body": net_escape_script},
            ],
            actor="stage4-owner",
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        net_copy = self.root_tmp / "net_copy"
        res_net = execute_promoted_skill(
            "net-blocked-skill",
            actor="stage4-owner",
            keep_sandbox_copy_dir=str(net_copy),
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        self.assertEqual(res_net["status"], "failed_rolled_back")
        self.assertTrue(res_net["rollback_executed"])
        self.assertIn(
            "Network access is disabled inside Second Brain Executable Skill sandbox",
            res_net["step_results"][-1]["stderr"],
        )
        self.assertEqual(os.listdir(net_copy), [])

        # Filesystem write outside SB_SANDBOX_WORKSPACE & os.chdir('..') are blocked by `_guard/sitecustomize.py`
        outside_target = self.root_tmp / "escaped_host_file.txt"
        fs_escape_script = (
            "import os\n"
            "from pathlib import Path\n"
            "try:\n"
            "    os.chdir('..')\n"
            "    raise SystemExit('os.chdir escape succeeded unexpectedly')\n"
            "except PermissionError:\n"
            "    pass\n"
            "Path('../escaped_sibling.txt').write_text('escape', encoding='utf-8')\n"
        )
        promote_playbook_to_executable_skill(
            skill_id="fs-blocked-skill",
            playbook_id="two-step-pb",
            expected_playbook_sha256=str(pb_two["sha256"]),
            step_actions=[
                {"action_type": "workspace_write", "rel_path": "staged.txt", "content": "step1"},
                {"action_type": "sandbox_python", "script_body": fs_escape_script},
            ],
            actor="stage4-owner",
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        fs_copy = self.root_tmp / "fs_copy"
        res_fs = execute_promoted_skill(
            "fs-blocked-skill",
            actor="stage4-owner",
            keep_sandbox_copy_dir=str(fs_copy),
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        self.assertEqual(res_fs["status"], "failed_rolled_back")
        self.assertTrue(res_fs["rollback_executed"])
        self.assertFalse(outside_target.exists())
        self.assertEqual(os.listdir(fs_copy), [])

        # 4. Timeout enforcement terminates hung subprocess and rolls back workspace
        hang_script = (
            "import time\n"
            "from pathlib import Path\n"
            "Path('hung_partial.txt').write_text('hanging', encoding='utf-8')\n"
            "time.sleep(25)\n"
        )
        promote_playbook_to_executable_skill(
            skill_id="timeout-skill",
            playbook_id="two-step-pb",
            expected_playbook_sha256=str(pb_two["sha256"]),
            timeout_seconds=1.0,
            step_actions=[
                {"action_type": "workspace_write", "rel_path": "staged.txt", "content": "step1"},
                {"action_type": "sandbox_python", "script_body": hang_script},
            ],
            actor="stage4-owner",
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        timeout_copy = self.root_tmp / "timeout_copy"
        t_start = time.monotonic()
        res_timeout = execute_promoted_skill(
            "timeout-skill",
            actor="stage4-owner",
            keep_sandbox_copy_dir=str(timeout_copy),
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        elapsed = time.monotonic() - t_start
        self.assertEqual(res_timeout["status"], "timed_out")
        self.assertTrue(res_timeout["rollback_executed"])
        self.assertLess(elapsed, 6.0)
        self.assertEqual(os.listdir(timeout_copy), [])


class KillSwitchGovernanceTests(Stage4IsolatedTestCase):
    def test_pre_execution_and_mid_flight_kill_switch_terminates_and_rolls_back_under_2s(self) -> None:
        from executable_skills import (
            ExecutableSkillExecutionDeniedError,
            execute_promoted_skill,
            get_skill_kill_switch_status,
            promote_playbook_to_executable_skill,
            set_skill_kill_switch,
            skill_kill_switch_path,
            verify_executable_skill_integrity,
        )

        brain = self.root_tmp / "brain"
        _, _, pb = _seed_mature_playbook(
            brain,
            playbook_id="long-job-pb",
            atom_filename="long-job-proc.md",
            steps=[
                "Write initial checkpoint marker into sandbox workspace.",
                "Run long analytical Python worker inside sandbox.",
            ],
            citations=3,
            successful_episodes=1,
        )

        sleeping_worker = (
            "import time\n"
            "from pathlib import Path\n"
            "Path('worker_partial.txt').write_text('in-progress', encoding='utf-8')\n"
            "time.sleep(30)\n"
        )
        promote_playbook_to_executable_skill(
            skill_id="long-job-skill",
            playbook_id="long-job-pb",
            expected_playbook_sha256=str(pb["sha256"]),
            timeout_seconds=30.0,
            step_actions=[
                {"action_type": "workspace_write", "rel_path": "checkpoint.txt", "content": "started"},
                {"action_type": "sandbox_python", "script_body": sleeping_worker},
            ],
            actor="stage4-owner",
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        promote_playbook_to_executable_skill(
            skill_id="healthy-sibling-skill",
            playbook_id="long-job-pb",
            expected_playbook_sha256=str(pb["sha256"]),
            timeout_seconds=10.0,
            step_actions=[
                {"action_type": "workspace_write", "rel_path": "ok.txt", "content": "healthy"},
                {"action_type": "workspace_assert", "rel_path": "ok.txt", "op": "contains", "expected": "healthy"},
            ],
            actor="stage4-owner",
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )

        # 1. Per-skill kill-switch blocks `long-job-skill` pre-execution while `healthy-sibling-skill` runs
        set_skill_kill_switch(
            engaged=True,
            scope="long-job-skill",
            actor="stage4-owner",
            reason="Investigating worker anomaly",
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        rep_killed = verify_executable_skill_integrity(
            "long-job-skill", brain_dir=str(brain), security_dir=self.sec_dir
        )
        self.assertFalse(rep_killed.valid)
        self.assertEqual(rep_killed.status, "killed")

        with self.assertRaises(ExecutableSkillExecutionDeniedError) as denied_ctx:
            execute_promoted_skill(
                "long-job-skill",
                actor="stage4-owner",
                brain_dir=str(brain),
                security_dir=self.sec_dir,
            )
        self.assertIsNotNone(denied_ctx.exception.audit_record)
        self.assertTrue(denied_ctx.exception.audit_record["killed_by_switch"])

        sibling_res = execute_promoted_skill(
            "healthy-sibling-skill",
            actor="stage4-owner",
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        self.assertEqual(sibling_res["status"], "completed")

        # Release per-skill switch
        set_skill_kill_switch(
            engaged=False,
            scope="long-job-skill",
            actor="stage4-owner",
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )

        # 2. Mid-execution global kill-switch terminates in-flight sleeping subprocess within <2s & rolls back!
        def _trigger_switch_mid_run(step_idx: int) -> None:
            if step_idx == 1:
                timer = threading.Timer(
                    0.12,
                    lambda: set_skill_kill_switch(
                        engaged=True,
                        scope="global",
                        actor="stage4-owner",
                        reason="Emergency mid-flight halt",
                        brain_dir=str(brain),
                        security_dir=self.sec_dir,
                    ),
                )
                timer.daemon = True
                timer.start()

        mid_copy = self.root_tmp / "mid_kill_copy"
        t0 = time.monotonic()
        mid_res = execute_promoted_skill(
            "long-job-skill",
            actor="stage4-owner",
            keep_sandbox_copy_dir=str(mid_copy),
            brain_dir=str(brain),
            security_dir=self.sec_dir,
            _mid_run_hook=_trigger_switch_mid_run,
        )
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 2.0, f"Mid-flight kill-switch took {elapsed:.3f}s (expected < 2.0s)")
        self.assertEqual(mid_res["status"], "killed_by_switch")
        self.assertTrue(mid_res["killed_by_switch"])
        self.assertTrue(mid_res["rollback_executed"])
        self.assertEqual(os.listdir(mid_copy), [])

        # Release global kill-switch, then corrupt kill-switch JSON -> verify fail-closed global lock!
        set_skill_kill_switch(
            engaged=False,
            scope="global",
            actor="stage4-owner",
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        ks_file = skill_kill_switch_path(self.sec_dir)
        ks_file.write_text("{corrupt-json", encoding="utf-8")
        corrupt_status = get_skill_kill_switch_status(brain_dir=str(brain), security_dir=self.sec_dir)
        self.assertTrue(corrupt_status["global_engaged"])
        self.assertTrue(corrupt_status["corrupt"])
        with self.assertRaises(ExecutableSkillExecutionDeniedError):
            execute_promoted_skill(
                "healthy-sibling-skill",
                actor="stage4-owner",
                brain_dir=str(brain),
                security_dir=self.sec_dir,
            )


class StalenessCascadeRevocationAndTamperTests(Stage4IsolatedTestCase):
    def test_atom_edit_demote_correction_playbook_deprecate_skill_tamper_revoke_and_audit_tamper(self) -> None:
        from atom_mutator import apply_atom_edit, apply_review_transition
        from cognitive_core import record_atom_feedback
        from executable_skills import (
            ExecutableSkillExecutionDeniedError,
            ExecutableSkillValidationError,
            execute_promoted_skill,
            list_executable_skills,
            list_skill_executions,
            promote_playbook_to_executable_skill,
            revoke_executable_skill,
            skill_execution_log_path,
            verify_executable_skill_integrity,
            verify_skill_execution_audit_chain,
        )
        from skill_playbooks import deprecate_skill_playbook

        brain = self.root_tmp / "brain"
        telemetry_db = str(brain / ".sidecar" / "telemetry.sqlite3")

        def _make_independent_skill(slug: str) -> tuple[Path, str, dict[str, object], dict[str, object]]:
            apath, asha, pb_info = _seed_mature_playbook(
                brain,
                playbook_id=f"pb-{slug}",
                atom_filename=f"atom-{slug}.md",
                steps=[f"Write verified marker for {slug}."],
                citations=3,
                successful_episodes=1,
            )
            sk_info = promote_playbook_to_executable_skill(
                skill_id=f"skill-{slug}",
                playbook_id=f"pb-{slug}",
                expected_playbook_sha256=str(pb_info["sha256"]),
                step_actions=[
                    {"action_type": "workspace_write", "rel_path": "marker.txt", "content": slug}
                ],
                actor="stage4-owner",
                brain_dir=str(brain),
                security_dir=self.sec_dir,
            )
            return apath, asha, pb_info, sk_info

        # Record 1 baseline valid execution so skill-executions.jsonl exists
        a_base, s_base, _, _ = _make_independent_skill("baseline")
        res_base = execute_promoted_skill(
            "skill-baseline", actor="stage4-owner", brain_dir=str(brain), security_dir=self.sec_dir
        )
        self.assertEqual(res_base["status"], "completed")

        # Case A: Editing backing procedural atom cascades staleness immediately
        a_edit, s_edit, _, _ = _make_independent_skill("atom-edit")
        edited_md = a_edit.read_text(encoding="utf-8").replace(
            "Enforces deterministic Stage 4 executable governance.",
            "Edited note invalidating downstream hashes.",
        )
        apply_atom_edit(
            atom_path=str(a_edit),
            expected_sha256=s_edit,
            edited_content=edited_md,
            actor="stage4-owner",
            brain_dir=str(brain),
            atoms_dir=str(brain / "2-atoms"),
        )
        rep_edit = verify_executable_skill_integrity(
            "skill-atom-edit", brain_dir=str(brain), security_dir=self.sec_dir
        )
        self.assertFalse(rep_edit.valid)
        self.assertEqual(rep_edit.status, "stale")
        with self.assertRaises(ExecutableSkillExecutionDeniedError):
            execute_promoted_skill(
                "skill-atom-edit", actor="stage4-owner", brain_dir=str(brain), security_dir=self.sec_dir
            )

        # Case B: Demoting backing procedural atom cascades staleness immediately
        a_dem, s_dem, _, _ = _make_independent_skill("atom-demote")
        apply_review_transition(
            action="demote",
            atom_path=str(a_dem),
            expected_sha256=s_dem,
            actor="stage4-owner",
            brain_dir=str(brain),
            atoms_dir=str(brain / "2-atoms"),
        )
        rep_dem = verify_executable_skill_integrity(
            "skill-atom-demote", brain_dir=str(brain), security_dir=self.sec_dir
        )
        self.assertFalse(rep_dem.valid)
        self.assertEqual(rep_dem.status, "stale")

        # Case C: Unacknowledged correction on backing atom cascades staleness immediately
        _make_independent_skill("atom-corr")
        record_atom_feedback("atom-atom-corr.md", helpful=False, db_path=telemetry_db, brain_dir=str(brain))
        rep_corr = verify_executable_skill_integrity(
            "skill-atom-corr", brain_dir=str(brain), security_dir=self.sec_dir
        )
        self.assertFalse(rep_corr.valid)
        self.assertEqual(rep_corr.status, "stale")

        # Case D: Deprecating backing Playbook cascades staleness immediately
        _, _, pb_dep, _ = _make_independent_skill("pb-dep")
        deprecate_skill_playbook(
            "pb-pb-dep.md",
            expected_sha256=str(pb_dep["sha256"]),
            actor="stage4-owner",
            rationale="Playbook retired",
            brain_dir=str(brain),
            playbooks_dir=str(brain / "5-playbooks"),
        )
        rep_pb_dep = verify_executable_skill_integrity(
            "skill-pb-dep", brain_dir=str(brain), security_dir=self.sec_dir
        )
        self.assertFalse(rep_pb_dep.valid)
        self.assertEqual(rep_pb_dep.status, "stale")

        # Case E: Out-of-band tampering with `6-skills/<id>.md` on disk fails closed
        _make_independent_skill("disk-tamper")
        sk_disk_path = brain / "6-skills" / "skill-disk-tamper.md"
        sk_disk_path.write_text(
            sk_disk_path.read_text(encoding="utf-8").replace('"content": "disk-tamper"', '"content": "pwned"'),
            encoding="utf-8",
        )
        rep_tamper = verify_executable_skill_integrity(
            "skill-disk-tamper", brain_dir=str(brain), security_dir=self.sec_dir
        )
        self.assertFalse(rep_tamper.valid)
        self.assertEqual(rep_tamper.reason, "skill_revision_not_owner_promoted")
        with self.assertRaises(ExecutableSkillExecutionDeniedError):
            execute_promoted_skill(
                "skill-disk-tamper", actor="stage4-owner", brain_dir=str(brain), security_dir=self.sec_dir
            )

        # Case F: CAS Revocation + anti-unrevocation via skill-decisions.jsonl
        _, _, _, sk_rev = _make_independent_skill("revoked")
        rev_res = revoke_executable_skill(
            skill_id="skill-revoked",
            expected_skill_sha256=str(sk_rev["sha256"]),
            actor="stage4-owner",
            reason="Superseded by v2 workflow",
            brain_dir=str(brain),
        )
        self.assertEqual(rev_res["status"], "revoked")
        rep_rev = verify_executable_skill_integrity(
            "skill-revoked", brain_dir=str(brain), security_dir=self.sec_dir
        )
        self.assertFalse(rep_rev.valid)
        self.assertEqual(rep_rev.status, "revoked")

        # Even if an attacker restores the pre-revocation `.history/` bytes onto `6-skills/skill-revoked.md`,
        # `skill-decisions.jsonl` latest action is `revoke_executable_skill`, so it STILL fails closed!
        hist_file = brain / "6-skills" / ".history" / f"skill-revoked.{str(sk_rev['sha256'])[:12]}.md"
        self.assertTrue(hist_file.is_file())
        (brain / "6-skills" / "skill-revoked.md").write_bytes(hist_file.read_bytes())
        rep_unrev = verify_executable_skill_integrity(
            "skill-revoked", brain_dir=str(brain), security_dir=self.sec_dir
        )
        self.assertFalse(rep_unrev.valid)
        self.assertEqual(rep_unrev.status, "revoked")
        self.assertEqual(rep_unrev.reason, "skill_revoked_in_decision_log")

        # Only `skill-baseline` remains active!
        active_list = list_executable_skills(brain_dir=str(brain), security_dir=self.sec_dir, active_only=True)
        self.assertEqual([s.skill_id for s in active_list], ["skill-baseline"])

        # Case G: Symlink / junction defense on `6-skills/`
        elsewhere = self.root_tmp / "elsewhere_skills"
        elsewhere.mkdir(parents=True, exist_ok=True)
        link_skills = self.root_tmp / "linked_skills"
        if _create_dir_link_or_junction(link_skills, elsewhere):
            rep_link = verify_executable_skill_integrity(
                "skill-baseline",
                brain_dir=str(brain),
                skills_dir=str(link_skills),
                security_dir=self.sec_dir,
            )
            self.assertFalse(rep_link.valid)
            self.assertEqual(rep_link.reason, "symlinked_skills_directory")

        # Case H: Tampering with `skill-executions.jsonl` fails closed immediately!
        exec_log = Path(skill_execution_log_path(str(brain)))
        lines = exec_log.read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(lines), 1)
        tampered_obj = json.loads(lines[0])
        tampered_obj["actor"] = "forged-actor"
        lines[0] = json.dumps(tampered_obj, sort_keys=True, separators=(",", ":"))
        exec_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        tampered_chain = verify_skill_execution_audit_chain(str(brain))
        self.assertFalse(tampered_chain["valid"])
        self.assertTrue(any("execution_sha256 digest mismatch" in e for e in tampered_chain["errors"]))

        with self.assertRaises(ExecutableSkillValidationError):
            list_skill_executions(str(brain))

        with self.assertRaises(ExecutableSkillValidationError):
            execute_promoted_skill(
                "skill-baseline",
                actor="stage4-owner",
                brain_dir=str(brain),
                security_dir=self.sec_dir,
            )


class AssertionOpsAuditKindsDlpOutputAndCliTests(Stage4IsolatedTestCase):
    def test_all_assert_ops_ledger_sources_audits_inputs_readonly_output_dlp_and_cli(self) -> None:
        from contextlib import redirect_stdout
        import io
        import executable_skills as es_top
        from scripts import executable_skills as es_pkg
        from vault_ledger import commit_ledger_checkpoint

        self.assertIs(es_top, es_pkg)

        brain = self.root_tmp / "brain"
        known_content = '{"nested": {"count": 42, "ok": true}}\n'
        known_sha = hashlib.sha256(known_content.encode("utf-8")).hexdigest()

        _, _, pb_all = _seed_mature_playbook(
            brain,
            playbook_id="all-asserts-pb",
            atom_filename="all-asserts-proc.md",
            steps=[
                "Write deterministic JSON artifact.",
                "Verify artifact exists.",
                "Verify artifact is non-empty.",
                "Verify artifact contains expected substring.",
                "Verify artifact SHA-256 equals expected digest.",
                "Verify nested JSON key equals 42.",
                "Run sources and ledger audits.",
                "Verify ledger audit report exists.",
            ],
            citations=3,
            successful_episodes=1,
        )
        commit_ledger_checkpoint(brain_dir=str(brain), atoms_dir=str(brain / "2-atoms"), actor="stage4-owner")

        es_top.promote_playbook_to_executable_skill(
            skill_id="all-asserts-skill",
            playbook_id="all-asserts-pb",
            expected_playbook_sha256=str(pb_all["sha256"]),
            input_schema={
                "note": {"type": "string", "required": False, "default": "safe"},
                "other": {"type": "string", "required": False, "default": "NEVER_EXPANDED"},
            },
            step_actions=[
                {"action_type": "workspace_write", "rel_path": "doc.json", "content": known_content},
                {"action_type": "workspace_assert", "rel_path": "doc.json", "op": "exists"},
                {"action_type": "workspace_assert", "rel_path": "doc.json", "op": "non_empty"},
                {"action_type": "workspace_assert", "rel_path": "doc.json", "op": "contains", "expected": '"count": 42'},
                {
                    "action_type": "workspace_assert",
                    "rel_path": "doc.json",
                    "op": "sha256_equals",
                    "expected": known_sha,
                },
                {
                    "action_type": "workspace_assert",
                    "rel_path": "doc.json",
                    "op": "json_key_equals",
                    "json_key": "nested.count",
                    "expected": "42",
                },
                {"action_type": "vault_audit", "audit_kind": "sources", "output_file": "sources.json"},
                {"action_type": "vault_audit", "audit_kind": "ledger", "output_file": "ledger.json"},
            ],
            actor="stage4-owner",
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        res_ok = es_top.execute_promoted_skill(
            "all-asserts-skill",
            actor="stage4-owner",
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        self.assertEqual(res_ok["status"], "completed")
        self.assertIn("sources.json", res_ok["artifact_hashes"])
        self.assertIn("ledger.json", res_ok["artifact_hashes"])

        # Non-recursive template substitution + failing workspace_assert rollback + read-only inputs.json + stdout DLP/truncation
        _, _, pb_edge = _seed_mature_playbook(
            brain,
            playbook_id="edge-guards-pb",
            atom_filename="edge-guards-proc.md",
            steps=[
                "Write user note template without recursive expansion.",
                "Run python script testing inputs.json immutability and noisy stdout.",
            ],
            citations=3,
            successful_episodes=1,
        )
        noisy_and_readonly_check = (
            "import os\n"
            "from pathlib import Path\n"
            "inp_path = Path(os.environ['SB_SANDBOX_INPUTS'])\n"
            "try:\n"
            "    inp_path.write_text('tampered', encoding='utf-8')\n"
            "    raise SystemExit('inputs.json should not be writable!')\n"
            "except PermissionError:\n"
            "    pass\n"
            "print('contact=leak@internal.example ' + ('X' * 2048))\n"
        )
        es_top.promote_playbook_to_executable_skill(
            skill_id="edge-guards-skill",
            playbook_id="edge-guards-pb",
            expected_playbook_sha256=str(pb_edge["sha256"]),
            max_output_bytes=1024,
            input_schema={
                "a": {"type": "string", "required": True},
                "b": {"type": "string", "required": False, "default": "SECRET_SECOND_PASS"},
            },
            step_actions=[
                {"action_type": "workspace_write", "rel_path": "note.txt", "content": "a={input.a}"},
                {"action_type": "sandbox_python", "script_body": noisy_and_readonly_check},
            ],
            actor="stage4-owner",
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        edge_copy = self.root_tmp / "edge_copy"
        res_edge = es_top.execute_promoted_skill(
            "edge-guards-skill",
            inputs={"a": "{input.b}"},
            actor="stage4-owner",
            keep_sandbox_copy_dir=str(edge_copy),
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        self.assertEqual(res_edge["status"], "completed")
        # Literal "{input.b}" preserved verbatim — never recursively expanded!
        self.assertEqual((edge_copy / "note.txt").read_text(encoding="utf-8"), "a={input.b}")
        py_step = res_edge["step_results"][1]
        self.assertTrue(py_step["output_truncated"])
        self.assertNotIn("leak@internal.example", py_step["stdout"])
        self.assertIn("[REDACTED_PII:email_address]", py_step["stdout"])

        # Failing workspace_assert rolls back prior workspace_write
        es_top.promote_playbook_to_executable_skill(
            skill_id="assert-fail-skill",
            playbook_id="edge-guards-pb",
            expected_playbook_sha256=str(pb_edge["sha256"]),
            step_actions=[
                {"action_type": "workspace_write", "rel_path": "dirty.txt", "content": "uncommitted"},
                {
                    "action_type": "workspace_assert",
                    "rel_path": "dirty.txt",
                    "op": "contains",
                    "expected": "NON_EXISTENT_MARKER",
                },
            ],
            actor="stage4-owner",
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        fail_copy = self.root_tmp / "assert_fail_copy"
        res_assert_fail = es_top.execute_promoted_skill(
            "assert-fail-skill",
            actor="stage4-owner",
            keep_sandbox_copy_dir=str(fail_copy),
            brain_dir=str(brain),
            security_dir=self.sec_dir,
        )
        self.assertEqual(res_assert_fail["status"], "failed_rolled_back")
        self.assertTrue(res_assert_fail["rollback_executed"])
        self.assertEqual(os.listdir(fail_copy), [])

        # Symlinked security_dir fails closed on kill-switch read & write
        real_sec = self.root_tmp / "real_sec"
        real_sec.mkdir(parents=True, exist_ok=True)
        link_sec = self.root_tmp / "link_sec"
        if _create_dir_link_or_junction(link_sec, real_sec):
            ks_link = es_top.get_skill_kill_switch_status(brain_dir=str(brain), security_dir=link_sec)
            self.assertTrue(ks_link["global_engaged"])
            self.assertTrue(ks_link["corrupt"])
            with self.assertRaises(PermissionError):
                es_top.set_skill_kill_switch(
                    engaged=True,
                    scope="global",
                    actor="stage4-owner",
                    reason="test",
                    brain_dir=str(brain),
                    security_dir=link_sec,
                )

        # CLI entrypoint smoke checks
        with patch.dict(os.environ, {"BRAIN_VAULT_DIR": str(brain)}, clear=False):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = es_top.main(["list", "--json"])
            self.assertEqual(rc, 0)
            listed = json.loads(buf.getvalue())
            self.assertGreaterEqual(len(listed), 2)

            buf_audit = io.StringIO()
            with redirect_stdout(buf_audit):
                rc_audit = es_top.main(["verify-audit", "--json"])
            self.assertEqual(rc_audit, 0)
            self.assertTrue(json.loads(buf_audit.getvalue())["valid"])

    def test_7_fullstack_api_sdk_cli_merkle_and_ui_parity(self) -> None:
        """Verify end-to-end Loopback HTTP API, MemoryApiClient SDK, Merkle proofs, CLI, and 3-surface UI parity."""
        import threading
        import httpx
        from cognitive_core import compute_memory_fitness_report
        from host_adapters import restrict_security_file_permissions
        from memory_api import MemoryApiClient, MemoryApiRuntimeConfig, create_memory_api_server
        from vault_ledger import prove_leaf_membership, verify_merkle_proof
        from vault_sync import LocalFolderSyncBackend, init_sync

        brain = self.root_tmp / "brain_api"
        _, _, pb_res = _seed_mature_playbook(
            brain,
            playbook_id="api-audit-pb",
            atom_filename="api-proc.md",
            steps=["Run source anchor audit across approved atoms."],
            contexts=("project:alpha",),
            citations=4,
            successful_episodes=1,
        )

        # Initialize local E2EE sync target so Mobile Vault Client pairing works over API
        sync_target = self.root_tmp / "sync_target"
        init_sync(
            str(sync_target),
            brain_dir=brain,
            passphrase="correct-horse-battery-staple",
            state_dir=self.sec_dir,
            use_keychain=False,
        )

        agent_tok = "agent_tok_stage4_0123456789abcdef0123456789abcdef"
        owner_tok = "owner_tok_stage4_fedcba9876543210fedcba9876543210"
        tok_file = self.sec_dir / "api-token"
        own_file = self.sec_dir / "owner-token"
        tok_file.write_text(agent_tok + "\n", encoding="utf-8")
        own_file.write_text(owner_tok + "\n", encoding="utf-8")
        restrict_security_file_permissions(tok_file)
        restrict_security_file_permissions(own_file)

        rt = MemoryApiRuntimeConfig(
            brain_dir=str(brain),
            token_path=tok_file,
            owner_token_path=own_file,
            security_dir=self.sec_dir,
        )
        rt.sync_state_dir = self.sec_dir  # type: ignore[attr-defined]
        server = create_memory_api_server("127.0.0.1", 0, rt)
        port = int(server.server_address[1])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            owner_client = MemoryApiClient("127.0.0.1", port, token=agent_tok, owner_token=owner_tok)
            agent_client = MemoryApiClient("127.0.0.1", port, token=agent_tok)

            # 1. Stage 4 Executable Skills over SDK & HTTP API
            cands = owner_client.list_skill_candidates()
            self.assertGreaterEqual(cands["count"], 1)

            # Agent-only client cannot promote skills (403)
            with self.assertRaises(httpx.HTTPStatusError) as cm_403:
                agent_client.promote_skill(
                    skill_id="api-skill",
                    playbook_id="api-audit-pb",
                    expected_playbook_sha256=str(pb_res["sha256"]),
                    step_actions=[{"action_type": "vault_audit", "audit_kind": "sources", "output_file": "a.json"}],
                    actor="api-owner",
                )
            self.assertEqual(cm_403.exception.response.status_code, 403)

            promoted = owner_client.promote_skill(
                skill_id="api-skill",
                playbook_id="api-audit-pb",
                expected_playbook_sha256=str(pb_res["sha256"]),
                step_actions=[{"action_type": "vault_audit", "audit_kind": "sources", "output_file": "a.json"}],
                actor="api-owner",
                rationale="API promotion test",
                allow_early_owner_override=True,
            )
            self.assertEqual(promoted["status"], "active")

            # Verify Cognitive Core fitness report advances maturity_stage to "executable_skill"
            fit = compute_memory_fitness_report(
                atoms_dir=str(brain / "2-atoms"),
                brain_dir=str(brain),
                db_path=str(brain / ".sidecar" / "telemetry.sqlite3"),
            )
            stages_by_atom = {item["atom_id"]: item["maturity_stage"] for item in fit["procedure_maturity"]}
            self.assertEqual(stages_by_atom.get("api-proc.md"), "executable_skill")

            # Verify Merkle inclusion proof for 6-skills/api-skill.md
            sk_proof = prove_leaf_membership("6-skills/api-skill.md", brain_dir=str(brain), atoms_dir=str(brain / "2-atoms"))
            self.assertEqual(sk_proof["kind"], "skill")
            self.assertTrue(verify_merkle_proof(sk_proof))

            dry_res = owner_client.execute_skill("api-skill", actor="api-owner", dry_run=True)
            self.assertEqual(dry_res["status"], "dry_run_verified")
            live_res = owner_client.execute_skill("api-skill", actor="api-owner", dry_run=False)
            self.assertEqual(live_res["status"], "completed")

            exec_hist = owner_client.list_skill_executions(skill_id="api-skill")
            self.assertGreaterEqual(exec_hist["count"], 2)
            self.assertTrue(exec_hist["audit_chain"]["chain_valid"])

            # Engage and release kill-switch via API
            owner_client.set_skill_kill_switch(engaged=True, actor="api-owner", reason="maintenance")
            with self.assertRaises(httpx.HTTPStatusError) as cm_killed:
                owner_client.execute_skill("api-skill", actor="api-owner")
            self.assertEqual(cm_killed.exception.response.status_code, 403)
            owner_client.set_skill_kill_switch(engaged=False, actor="api-owner", reason="done")

            # 2. Mobile Vault Client over SDK & HTTP API
            mob_pair = owner_client.mobile_pair(label="Test iPhone")
            self.assertIn("#vaultpair=", str(mob_pair.get("pairing_url") or mob_pair.get("pairing_fragment")))
            mob_st = owner_client.mobile_status()
            self.assertEqual(len(mob_st["pairings"]), 1)
            mob_exp = owner_client.mobile_export(str(self.root_tmp / "dist_mob"))
            self.assertTrue(Path(str(mob_exp["zip_path"])).is_file())
            owner_client.mobile_revoke(str(mob_pair["pairing_id"]))

            # 3. Local Creative Media Generation over SDK & HTTP API
            m_st = owner_client.media_status()
            self.assertTrue(m_st["backends"]["builtin_procedural_local"]["ready"])
            img_res = owner_client.generate_image(
                "Deterministic Second Brain architecture diagram",
                backend="builtin_procedural_local",
                style="blueprint",
                width=384,
                height=256,
                ground_in_vault=True,
                project_scope="project:alpha",
            )
            self.assertEqual(img_res["media_type"], "image")
            vid_res = owner_client.generate_video(
                "Animated Merkle proof flow",
                backend="builtin_procedural_local",
                style="concept_card",
                output_format="mp4",
                width=320,
                height=240,
                frames=8,
                fps=8,
            )
            self.assertEqual(vid_res["media_type"], "video")
            gal = owner_client.media_gallery()
            self.assertEqual(gal["count"], 2)
            b64_file = owner_client.media_file_base64(str(vid_res["media_id"]))
            self.assertTrue(str(b64_file["data_uri"]).startswith("data:video/mp4;base64,"))

            # Non-owner (agent-only) token is rejected (403) on /v1/media/gallery and /v1/media/file/<id>
            with self.assertRaises(httpx.HTTPStatusError) as cm_gal_403:
                agent_client.media_gallery()
            self.assertEqual(cm_gal_403.exception.response.status_code, 403)
            with self.assertRaises(httpx.HTTPStatusError) as cm_file_403:
                agent_client.media_file_base64(str(vid_res["media_id"]))
            self.assertEqual(cm_file_403.exception.response.status_code, 403)

            # DLP blocks secret in creative media prompt over API (403)
            with self.assertRaises(httpx.HTTPStatusError) as cm_dlp:
                owner_client.generate_image("Render key sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcdefgh")
            self.assertEqual(cm_dlp.exception.response.status_code, 403)

            # Verify sandbox closure & C audit-hook isolation (sitecustomize not in sys.modules,
            # FileIO and outside-workspace reads blocked even via closure introspection)
            escape_probe_path = self.root_tmp / "outside_secret_probe.txt"
            escape_probe_path.write_text("TOP_SECRET_OUTSIDE_SANDBOX", encoding="utf-8")
            escape_skill = owner_client.promote_skill(
                skill_id="escape-guard-skill",
                playbook_id="api-audit-pb",
                expected_playbook_sha256=str(pb_res["sha256"]),
                step_actions=[
                    {
                        "action_type": "sandbox_python",
                        "output_file": "guard_check.json",
                        "script_body": (
                            "import io, json, os, sys\n"
                            "assert 'sitecustomize' not in sys.modules, 'sitecustomize leaked in sys.modules'\n"
                            f"outside = {str(escape_probe_path)!r}\n"
                            "blocked_read = False\n"
                            "try:\n"
                            "    open(outside, 'r').read()\n"
                            "except PermissionError:\n"
                            "    blocked_read = True\n"
                            "assert blocked_read, 'outside-workspace read was not blocked'\n"
                            "blocked_fileio = False\n"
                            "try:\n"
                            "    io.FileIO(outside, 'w')\n"
                            "except PermissionError:\n"
                            "    blocked_fileio = True\n"
                            "assert blocked_fileio, 'io.FileIO outside-workspace write was not blocked'\n"
                            "with open('guard_check.json', 'w', encoding='utf-8') as f:\n"
                            "    json.dump({'isolated': True}, f)\n"
                        ),
                    }
                ],
                actor="api-owner",
                rationale="Verify sandbox closure & audit-hook hardening",
                allow_early_owner_override=True,
            )
            self.assertEqual(escape_skill["status"], "active")
            guard_exec = owner_client.execute_skill("escape-guard-skill", actor="api-owner", dry_run=False)
            self.assertEqual(guard_exec["status"], "completed")
        finally:
            server.shutdown()
            server.server_close()

        # 4. Verify 3-surface UI parity (Electron Desktop, VS Code Extension, Streamlit Dashboard)
        repo_root = Path(__file__).resolve().parents[1]
        desktop_html = (repo_root / "desktop" / "renderer" / "index.html").read_text(encoding="utf-8")
        for dom_id in ("creativeStudioCard", "systemMobileVaultPanel", "executableSkillsCard"):
            self.assertIn(dom_id, desktop_html)
        vscode_pkg = json.loads((repo_root / "extensions" / "vscode-second-brain" / "package.json").read_text(encoding="utf-8"))
        vscode_cmds = {c["command"] for c in vscode_pkg["contributes"]["commands"]}
        for cmd_name in (
            "secondBrain.manageExecutableSkills",
            "secondBrain.mobileVaultClient",
            "secondBrain.generateCreativeImage",
            "secondBrain.generateCreativeVideo",
        ):
            self.assertIn(cmd_name, vscode_cmds)
        dash_src = (repo_root / "dashboard" / "app.py").read_text(encoding="utf-8")
        for heading in (
            "Stage 4 Executable Skills",
            "Full On-Device Mobile Vault Client",
            "Local Creative Media Generation Studio",
        ):
            self.assertIn(heading, dash_src)


if __name__ == "__main__":
    unittest.main()
