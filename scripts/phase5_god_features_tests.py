# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""Tests for OS-Signed Desktop Installers, SOTA Benchmark, Merkle Ledger, Contradiction Radar & Time-Travel."""

from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class Phase5IsolatedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        stack = ExitStack()
        self.addCleanup(stack.close)
        sec_root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "BRAIN_MEMORY_API_TOKEN_FILE": str(sec_root / "memory-api-token"),
                    "BRAIN_HOST_POLICY_FILE": str(sec_root / "host-sharing-policy.json"),
                    "BRAIN_MCP_TOKEN_FILE": str(sec_root / "mcp-token"),
                    "BRAIN_OWNER_TOKEN_FILE": str(sec_root / "owner-token"),
                },
                clear=False,
            )
        )
        for var in (
            "BRAIN_VAULT_DIR",
            "BRAIN_MEMORY_API_TOKEN",
            "BRAIN_MCP_BEARER_TOKEN",
            "BRAIN_MCP_CLIENT_TOKEN",
            "BRAIN_MCP_TRUSTED_STDIO",
            "OLLAMA_HOST",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            os.environ.pop(var, None)


def _seed_atom(
    brain: Path,
    *,
    filename: str,
    claim: str,
    evidence: str,
    project: str = "project:alpha",
    memory_type: str = "semantic",
    approve: bool = True,
    links: list[str] | None = None,
) -> Path:
    from atom_mutator import apply_review_transition, content_sha256

    raw_dir = brain / "0-raw"
    atoms_dir = brain / "2-atoms"
    sidecar = brain / ".sidecar"
    raw_dir.mkdir(parents=True, exist_ok=True)
    atoms_dir.mkdir(parents=True, exist_ok=True)
    sidecar.mkdir(parents=True, exist_ok=True)

    src_name = f"src-{filename}"
    (raw_dir / src_name).write_text(f"Primary record: {evidence}\n", encoding="utf-8")
    link_block = ""
    if links:
        link_block = "links:\n" + "".join(f"  - {l}\n" for l in links)
    atom_path = atoms_dir / filename
    atom_path.write_text(
        "---\n"
        f"id: {Path(filename).stem}\n"
        "type: atom\n"
        f"memory_type: {memory_type}\n"
        "epistemic_label: claim\n"
        "sources:\n"
        f"  - 0-raw/{src_name}\n"
        "contexts:\n"
        f"  - {project}\n"
        f"{link_block}"
        "review_status: pending\n"
        "claim_state: candidate\n"
        "citation_integrity: missing\n"
        "---\n\n"
        "## Claim\n"
        f"{claim}\n\n"
        "## Evidence\n"
        f"{evidence}\n",
        encoding="utf-8",
    )
    if approve:
        apply_review_transition(
            action="approve",
            atom_path=str(atom_path),
            expected_sha256=content_sha256(atom_path.read_bytes()),
            actor="test-owner",
            brain_dir=str(brain),
            atoms_dir=str(atoms_dir),
        )
    return atom_path


class DesktopInstallerAndTrayTests(Phase5IsolatedTestCase):
    def test_installer_targets_and_tray_lifecycle_present(self) -> None:
        root = Path(__file__).resolve().parents[1]
        pkg = json.loads((root / "desktop" / "package.json").read_text(encoding="utf-8"))
        scripts = pkg["scripts"]
        for key in ("pack", "dist", "dist:win", "dist:msi", "dist:mac", "dist:linux", "dist:all"):
            self.assertIn(key, scripts)
        win_targets = {t["target"] for t in pkg["build"]["win"]["target"]}
        self.assertEqual(win_targets, {"nsis", "msi"})
        mac_targets = {t["target"] for t in pkg["build"]["mac"]["target"]}
        self.assertEqual(mac_targets, {"dmg", "zip"})
        linux_targets = {t["target"] for t in pkg["build"]["linux"]["target"]}
        self.assertEqual(linux_targets, {"AppImage", "deb"})
        extra = pkg.get("build", {}).get("extraResources", [])
        self.assertTrue(any(isinstance(x, dict) and x.get("from") == "../dashboard" for x in extra))

        main_js = (root / "desktop" / "main.js").read_text(encoding="utf-8")
        for sym in ("refreshTrayMenu", "stopApiSidecar", "getVaultDir", "setLoginItemSettings", "shell.openPath", "ensureBackendBootstrapped"):
            self.assertIn(sym, main_js)

        index_html = (root / "desktop" / "renderer" / "index.html").read_text(encoding="utf-8")
        for view_id in ("view-planner", "view-proof", "commitLedgerBtn", "scanRadarBtn", "createSnapshotBtn"):
            self.assertIn(view_id, index_html)

        app_js = (root / "desktop" / "renderer" / "app.js").read_text(encoding="utf-8")
        for fn_name in ("loadPlannerView", "runExecutivePlan", "loadProofView", "scanContradictions"):
            self.assertIn(fn_name, app_js)

        extra_to = {item["to"] for item in pkg["build"]["extraResources"]}
        self.assertIn("backend/setup.ps1", extra_to)
        self.assertIn("backend/setup.sh", extra_to)

        self.assertTrue((root / "scripts" / "package_desktop.ps1").is_file())
        self.assertTrue((root / "scripts" / "package_desktop.sh").is_file())


class SotaBenchmarkTests(Phase5IsolatedTestCase):
    def test_sota_benchmark_outperforms_baselines_on_safety_and_grounding(self) -> None:
        from sota_benchmark import run_sota_benchmark

        res = run_sota_benchmark(atoms=100)
        self.assertIn("hardware_profile", res)
        sb = res["systems"]["second_brain_verified"]
        naive = res["systems"]["naive_bm25_rag_no_trust_gate"]
        unscoped = res["systems"]["unscoped_flat_memory"]
        ungrounded = res["systems"]["ungrounded_synthesis"]

        self.assertEqual(sb["positive_recall_pct"], 100.0)
        self.assertEqual(sb["claim_precision_pct"], 100.0)
        self.assertEqual(sb["citation_grounding_pct"], 100.0)
        self.assertEqual(sb["trap_leakage_pct"], 0.0)
        self.assertEqual(sb["cross_project_leak_pct"], 0.0)
        self.assertEqual(sb["abstention_accuracy_pct"], 100.0)
        self.assertGreater(naive["trap_leakage_pct"], 0.0)
        self.assertGreater(unscoped["cross_project_leak_pct"], 0.0)
        self.assertEqual(ungrounded["citation_grounding_pct"], 0.0)


class VaultMerkleLedgerTests(Phase5IsolatedTestCase):
    def test_merkle_proof_chain_and_tamper_detection(self) -> None:
        from vault_ledger import (
            commit_ledger_checkpoint,
            prove_atom_membership,
            verify_merkle_proof,
            verify_vault_ledger,
        )

        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            atoms_dir = brain / "2-atoms"
            _seed_atom(brain, filename="a1.md", claim="Rotate keys every 90 days.", evidence="Rotate keys every 90 days.", approve=True)
            _seed_atom(brain, filename="a2.md", claim="Enable structured JSON logs.", evidence="Enable structured JSON logs.", approve=True)
            _seed_atom(brain, filename="draft.md", claim="Pending unapproved rule.", evidence="Pending unapproved rule.", approve=False)

            c1 = commit_ledger_checkpoint(brain_dir=str(brain), atoms_dir=str(atoms_dir), actor="owner", note="init")
            self.assertEqual(c1["seq"], 1)
            self.assertEqual(c1["approved_atoms"], 2)

            ver = verify_vault_ledger(brain_dir=str(brain), atoms_dir=str(atoms_dir))
            self.assertTrue(ver["chain_valid"])
            self.assertTrue(ver["synced_with_head"])

            proof = prove_atom_membership("a1.md", brain_dir=str(brain), atoms_dir=str(atoms_dir))
            self.assertTrue(verify_merkle_proof(proof))

            tampered = dict(proof)
            tampered["file_sha256"] = "0" * 64
            self.assertFalse(verify_merkle_proof(tampered))

            with self.assertRaises(PermissionError):
                prove_atom_membership("draft.md", brain_dir=str(brain), atoms_dir=str(atoms_dir))

            ledger_file = brain / ".sidecar" / "vault-ledger.jsonl"
            ledger_file.write_text(ledger_file.read_text(encoding="utf-8").replace('"owner"', '"mallory"'), encoding="utf-8")
            bad_ver = verify_vault_ledger(brain_dir=str(brain), atoms_dir=str(atoms_dir))
            self.assertFalse(bad_ver["chain_valid"])

    def test_forged_atom_excluded_and_promoted_playbook_included(self) -> None:
        from atom_mutator import content_sha256
        from skill_playbooks import promote_skill_playbook
        from vault_ledger import compute_vault_merkle, prove_atom_membership

        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            atoms_dir = brain / "2-atoms"
            pb_dir = brain / "5-playbooks"
            pb_dir.mkdir(parents=True, exist_ok=True)
            proc_path = _seed_atom(
                brain,
                filename="proc1.md",
                claim="Run smoke checks after build.",
                evidence="Run smoke checks after build.",
                memory_type="procedural",
                approve=True,
            )
            promote_skill_playbook(
                playbook_id="build-smoke",
                title="Build Smoke",
                backing_atoms=[("proc1.md", content_sha256(proc_path.read_bytes()))],
                trigger="After build",
                preconditions=["Clean workspace"],
                steps=["Run smoke checks after build."],
                expected_outputs=["Tests pass"],
                failure_recovery=["Inspect logs"],
                actor="owner",
                rationale="phase5 verification test",
                allow_early_owner_override=True,
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
                playbooks_dir=str(pb_dir),
                telemetry_db_path=str(brain / ".sidecar" / "telemetry.sqlite3"),
            )
            (atoms_dir / "forged.md").write_text(
                "---\nid: forged\ntype: atom\nmemory_type: semantic\nreview_status: accepted\nclaim_state: human_reviewed\n---\n\n## Claim\nForged claim.\n",
                encoding="utf-8",
            )
            (pb_dir / "forged-pb.md").write_text(
                "---\nid: forged-pb\ntype: atom\nreview_status: accepted\nplaybook_status: invalid\n---\n",
                encoding="utf-8",
            )

            merkle = compute_vault_merkle(brain_dir=str(brain), atoms_dir=str(atoms_dir), playbooks_dir=str(pb_dir))
            self.assertEqual(merkle["approved_atoms"], 1)
            self.assertEqual(merkle["accepted_playbooks"], 1)
            self.assertEqual(merkle["leaf_count"], 2)
            self.assertEqual(
                {leaf["rel_path"] for leaf in merkle["leaves"]},
                {"2-atoms/proc1.md", "5-playbooks/build-smoke.md"},
            )
            with self.assertRaises(PermissionError):
                prove_atom_membership("forged.md", brain_dir=str(brain), atoms_dir=str(atoms_dir), playbooks_dir=str(pb_dir))

    def test_three_leaf_merkle_proofs_and_odd_domain_separation(self) -> None:
        from vault_ledger import _build_merkle_levels, compute_vault_merkle, prove_atom_membership, verify_merkle_proof

        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            atoms_dir = brain / "2-atoms"
            for name in ("a1.md", "a2.md", "a3.md"):
                _seed_atom(
                    brain,
                    filename=name,
                    claim=f"Verified canonical claim for {name}.",
                    evidence=f"Verified canonical claim for {name}.",
                    approve=True,
                )

            merkle = compute_vault_merkle(brain_dir=str(brain), atoms_dir=str(atoms_dir))
            leaves = [leaf["leaf_sha256"] for leaf in merkle["leaves"]]
            self.assertEqual(len(leaves), 3)
            root_3 = _build_merkle_levels(leaves)[-1][0]
            root_4_dup = _build_merkle_levels([leaves[0], leaves[1], leaves[2], leaves[2]])[-1][0]
            self.assertNotEqual(root_3, root_4_dup)

            for name in ("a1.md", "a2.md", "a3.md"):
                proof = prove_atom_membership(name, brain_dir=str(brain), atoms_dir=str(atoms_dir))
                self.assertTrue(verify_merkle_proof(proof))
                if name == "a3.md":
                    self.assertTrue(any(step["position"] == "odd" for step in proof["siblings"]))


class KnowledgeGraphAndContradictionRadarTests(Phase5IsolatedTestCase):
    def test_detects_polarity_and_numeric_contradictions_with_scope_gate(self) -> None:
        from knowledge_graph import detect_contradictions, graph_neighbors

        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            atoms_dir = brain / "2-atoms"
            _seed_atom(
                brain,
                filename="tls-on.md",
                claim="Production ingress must enforce TLS transport encryption.",
                evidence="Production ingress must enforce TLS transport encryption.",
                project="project:alpha",
                approve=True,
                links=["keys-90.md"],
            )
            _seed_atom(
                brain,
                filename="tls-off.md",
                claim="Production ingress must never enforce TLS transport encryption.",
                evidence="Production ingress must never enforce TLS transport encryption.",
                project="project:alpha",
                approve=False,
            )
            _seed_atom(
                brain,
                filename="keys-90.md",
                claim="Rotate API signing keys every 90 days.",
                evidence="Rotate API signing keys every 90 days.",
                project="project:alpha",
                approve=True,
            )
            _seed_atom(
                brain,
                filename="keys-30.md",
                claim="Rotate API signing keys every 30 days.",
                evidence="Rotate API signing keys every 30 days.",
                project="project:alpha",
                approve=True,
            )
            _seed_atom(
                brain,
                filename="other-proj.md",
                claim="Never rotate API signing keys.",
                evidence="Never rotate API signing keys.",
                project="project:secret",
                approve=True,
            )

            owner_radar = detect_contradictions(
                brain_dir=str(brain), atoms_dir=str(atoms_dir), project_scope="project:alpha", include_pending=True
            )
            self.assertEqual(owner_radar["contradiction_count"], 2)
            reasons = {r for al in owner_radar["alerts"] for r in al["reasons"]}
            self.assertIn("polarity_conflict", reasons)
            self.assertIn("numeric_value_conflict", reasons)

            agent_radar = detect_contradictions(
                brain_dir=str(brain), atoms_dir=str(atoms_dir), project_scope="project:alpha", include_pending=False
            )
            # Only accepted contradiction (keys-90 vs keys-30) visible when include_pending=False
            self.assertEqual(agent_radar["contradiction_count"], 1)

            nbrs = graph_neighbors("tls-on.md", brain_dir=str(brain), atoms_dir=str(atoms_dir), include_pending=True)
            self.assertGreaterEqual(nbrs["edge_count"], 1)
            verified_nbrs = graph_neighbors("tls-on.md", brain_dir=str(brain), atoms_dir=str(atoms_dir), include_pending=False)
            for al in verified_nbrs["contradictions"]:
                self.assertEqual(al["atom_a"]["review_status"], "accepted")
                self.assertEqual(al["atom_b"]["review_status"], "accepted")
            self.assertLess(len(verified_nbrs["contradictions"]), len(nbrs["contradictions"]))


class TimeTravelSnapshotAndApiTests(Phase5IsolatedTestCase):
    def test_snapshots_semantic_diff_point_in_time_recall_and_api_routes(self) -> None:
        from api_product import ensure_owner_token
        from memory_api import MemoryApiRuntimeConfig, MemoryLoopbackHTTPServer, generate_memory_api_token, reset_api_rate_limiter
        from time_travel import create_snapshot, diff_snapshots, recall_at_snapshot
        import httpx

        reset_api_rate_limiter()
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            atoms_dir = brain / "2-atoms"
            _seed_atom(
                brain,
                filename="v1-rule.md",
                claim="Backup retention window is 14 days.",
                evidence="Backup retention window is 14 days.",
                project="project:alpha",
                approve=True,
            )
            snap1 = create_snapshot(label="baseline", actor="owner", brain_dir=str(brain), atoms_dir=str(atoms_dir))

            _seed_atom(
                brain,
                filename="v2-rule.md",
                claim="Quantum key rotation runs weekly.",
                evidence="Quantum key rotation runs weekly.",
                project="project:alpha",
                approve=True,
            )
            snap2 = create_snapshot(label="after-v2", actor="owner", brain_dir=str(brain), atoms_dir=str(atoms_dir))

            diff = diff_snapshots(snap1["snapshot_id"], snap2["snapshot_id"], brain_dir=str(brain), atoms_dir=str(atoms_dir))
            self.assertEqual([a["filename"] for a in diff["added_atoms"]], ["v2-rule.md"])

            past_recall = recall_at_snapshot("quantum key rotation", snap1["snapshot_id"], project_scope="project:alpha", brain_dir=str(brain))
            self.assertEqual(past_recall["results"], [])

            now_recall = recall_at_snapshot("quantum key rotation", snap2["snapshot_id"], project_scope="project:alpha", brain_dir=str(brain))
            self.assertEqual([r["filename"] for r in now_recall["results"]], ["v2-rule.md"])

            with self.assertRaises(ValueError):
                recall_at_snapshot("test", "../outside", brain_dir=str(brain))

            # Live Loopback API tests for God features
            tok_path = Path(tmp) / "api-token"
            own_path = Path(tmp) / "owner-token"
            agent_tok, _ = generate_memory_api_token(tok_path)
            owner_tok = ensure_owner_token(own_path)
            rt = MemoryApiRuntimeConfig(
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
                inbox_dir=str(brain / "0-raw" / "inbox"),
                experiences_dir=str(brain / "4-experiences"),
                lexical_index_path=str(brain / ".sidecar" / "lexical.sqlite3"),
                chroma_path=str(brain / ".chroma"),
                telemetry_db_path=str(brain / ".sidecar" / "telemetry.sqlite3"),
                token_path=tok_path,
                owner_token_path=own_path,
                policy_path=Path(tmp) / "policy.json",
            )
            server = MemoryLoopbackHTTPServer("127.0.0.1", 0, rt)
            port = server.server_address[1]
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                base = f"http://127.0.0.1:{port}"
                agent_h = {"Authorization": f"Bearer {agent_tok}", "Host": f"127.0.0.1:{port}"}
                owner_h = {**agent_h, "X-Owner-Token": owner_tok}
                with httpx.Client(trust_env=False, timeout=15) as c:
                    self.assertEqual(c.post(f"{base}/v1/ledger/commit", headers=agent_h, json={"actor": "bot"}).status_code, 403)
                    self.assertEqual(c.post(f"{base}/v1/snapshots", headers=agent_h, json={"actor": "bot"}).status_code, 403)

                    r_commit = c.post(f"{base}/v1/ledger/commit", headers=owner_h, json={"actor": "owner", "note": "api"})
                    self.assertEqual(r_commit.status_code, 201)

                    r_verify = c.get(f"{base}/v1/ledger/verify", headers=agent_h)
                    self.assertEqual(r_verify.status_code, 200)
                    self.assertTrue(r_verify.json()["chain_valid"])
                    self.assertTrue(r_verify.json()["synced_with_head"])

                    r_prove = c.get(f"{base}/v1/ledger/prove/v1-rule.md", headers=agent_h)
                    self.assertEqual(r_prove.status_code, 200)
                    self.assertEqual(r_prove.json()["merkle_root"], r_commit.json()["merkle_root"])

                    r_snap = c.post(f"{base}/v1/snapshots", headers=owner_h, json={"actor": "owner", "label": "api-snap"})
                    self.assertEqual(r_snap.status_code, 201)

                    r_diff = c.post(
                        f"{base}/v1/snapshots/diff",
                        headers=agent_h,
                        json={"from_snapshot": snap1["snapshot_id"], "to_snapshot": "live"},
                    )
                    self.assertEqual(r_diff.status_code, 200)
                    self.assertEqual([a["filename"] for a in r_diff.json()["added_atoms"]], ["v2-rule.md"])
            finally:
                server.shutdown()
                server.server_close()

    def test_snapshot_excludes_forged_atom_and_includes_promoted_playbook(self) -> None:
        from atom_mutator import content_sha256
        from skill_playbooks import promote_skill_playbook
        from time_travel import create_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            atoms_dir = brain / "2-atoms"
            pb_dir = brain / "5-playbooks"
            pb_dir.mkdir(parents=True, exist_ok=True)
            proc_path = _seed_atom(
                brain,
                filename="proc1.md",
                claim="Run smoke checks after build.",
                evidence="Run smoke checks after build.",
                memory_type="procedural",
                approve=True,
            )
            promote_skill_playbook(
                playbook_id="build-smoke",
                title="Build Smoke",
                backing_atoms=[("proc1.md", content_sha256(proc_path.read_bytes()))],
                trigger="After build",
                preconditions=["Clean workspace"],
                steps=["Run smoke checks after build."],
                expected_outputs=["Tests pass"],
                failure_recovery=["Inspect logs"],
                actor="owner",
                rationale="phase5 snapshot verification test",
                allow_early_owner_override=True,
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
                playbooks_dir=str(pb_dir),
                telemetry_db_path=str(brain / ".sidecar" / "telemetry.sqlite3"),
            )
            (atoms_dir / "forged.md").write_text(
                "---\nid: forged\ntype: atom\nmemory_type: semantic\nreview_status: accepted\nclaim_state: human_reviewed\n---\n\n## Claim\nForged claim.\n",
                encoding="utf-8",
            )
            (pb_dir / "forged-pb.md").write_text(
                "---\nid: forged-pb\ntype: atom\nreview_status: accepted\nplaybook_status: invalid\n---\n",
                encoding="utf-8",
            )

            snap = create_snapshot(actor="owner", brain_dir=str(brain), atoms_dir=str(atoms_dir), playbooks_dir=str(pb_dir))
            self.assertEqual(snap["atom_count"], 1)
            self.assertEqual(snap["playbook_count"], 1)
            self.assertIn("proc1.md", snap["atoms"])
            self.assertNotIn("forged.md", snap["atoms"])
            self.assertIn("build-smoke.md", snap["playbooks"])
            self.assertNotIn("forged-pb.md", snap["playbooks"])

    def test_p2_p3_snapshot_collision_single_pass_radar_and_ledger_seq(self) -> None:
        from datetime import datetime, timezone
        from knowledge_graph import build_knowledge_graph, detect_contradictions
        from time_travel import create_snapshot, list_snapshots, recall_at_snapshot
        from vault_ledger import GENESIS_HASH, _canonical_json, _sha256_text, compute_vault_merkle, verify_vault_ledger

        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            atoms_dir = brain / "2-atoms"
            _seed_atom(
                brain,
                filename="sb-rule.md",
                claim="Second Brain enforces single writer lock semantics for ledger and snapshots.",
                evidence="Second Brain enforces single writer lock semantics for ledger and snapshots.",
                project="project:second_brain",
                approve=True,
            )

            # 1. Freeze UTC timestamp to force same-second identical-label snapshot calls -> must never collide/overwrite.
            fixed_now = datetime(2026, 11, 15, 19, 15, 0, tzinfo=timezone.utc)
            with patch("time_travel.datetime") as mock_dt:
                mock_dt.now.return_value = fixed_now
                mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
                s1 = create_snapshot(label="same-sec", actor="owner", brain_dir=str(brain), atoms_dir=str(atoms_dir))
                s2 = create_snapshot(label="same-sec", actor="owner", brain_dir=str(brain), atoms_dir=str(atoms_dir))
            self.assertNotEqual(s1["snapshot_id"], s2["snapshot_id"])
            self.assertEqual(len(list_snapshots(brain_dir=str(brain))), 2)
            self.assertEqual(
                s1["merkle_root"],
                compute_vault_merkle(brain_dir=str(brain), atoms_dir=str(atoms_dir))["merkle_root"],
            )

            # 2. Hyphen/underscore normalization in project_scope across recall_at_snapshot and knowledge_graph.
            recalled = recall_at_snapshot(
                "writer lock semantics",
                s1["snapshot_id"],
                project_scope="second-brain",
                brain_dir=str(brain),
            )
            self.assertEqual([r["filename"] for r in recalled["results"]], ["sb-rule.md"])
            g = build_knowledge_graph(brain_dir=str(brain), atoms_dir=str(atoms_dir), project_scope="second-brain")
            self.assertEqual([n["filename"] for n in g["nodes"]], ["sb-rule.md"])

            # 3. Forged "accepted" atom with no current decision record cannot escalate to high severity or orphan_approved_atoms.
            (atoms_dir / "forged-accepted.md").write_text(
                "---\nid: forged-acc\ntype: atom\nmemory_type: semantic\nreview_status: accepted\n"
                "contexts:\n  - project:second_brain\n---\n\n"
                "## Claim\nSecond Brain must never enforce writer lock semantics for ledger and snapshots.\n",
                encoding="utf-8",
            )
            radar = detect_contradictions(brain_dir=str(brain), atoms_dir=str(atoms_dir), include_pending=True)
            self.assertEqual(radar["contradiction_count"], 1)
            self.assertEqual(radar["high_severity_count"], 0)
            self.assertEqual(radar["alerts"][0]["severity"], "medium")
            self.assertNotIn("forged-accepted.md", radar["orphan_approved_atoms"])
            self.assertIn("sb-rule.md", radar["orphan_approved_atoms"])

            # 4. Non-int/bool ledger seq must fail verification cleanly even with recomputed hash.
            ledger_file = brain / ".sidecar" / "vault-ledger.jsonl"
            ledger_file.parent.mkdir(parents=True, exist_ok=True)
            bad_entry = {
                "schema_version": 1,
                "seq": True,
                "timestamp": "2026-11-15T19:15:00+00:00",
                "actor": "owner",
                "note": "bool seq attack",
                "prev_entry_sha256": GENESIS_HASH,
                "merkle_root": s1["merkle_root"],
                "leaf_count": 1,
                "approved_atoms": 1,
                "accepted_playbooks": 0,
            }
            bad_entry["entry_sha256"] = _sha256_text(_canonical_json(bad_entry))
            ledger_file.write_text(_canonical_json(bad_entry) + "\n", encoding="utf-8")
            ver = verify_vault_ledger(brain_dir=str(brain), atoms_dir=str(atoms_dir))
            self.assertFalse(ver["chain_valid"])
            self.assertTrue(any("non-sequential seq=" in err for err in ver["errors"]))

    def test_audit_remediations_quarantined_graph_wikilinks_ledger_corrupt_stopword_and_sdk(self) -> None:
        from api_product import ensure_owner_token
        from atom_mutator import apply_atom_merge, apply_review_transition, content_sha256
        from knowledge_graph import build_knowledge_graph, detect_contradictions
        from memory_api import (
            MemoryApiClient,
            MemoryApiRuntimeConfig,
            MemoryLoopbackHTTPServer,
            generate_memory_api_token,
            reset_api_rate_limiter,
        )
        from time_travel import create_snapshot, recall_at_snapshot
        from vault_ledger import prove_atom_membership, verify_merkle_proof, verify_vault_ledger
        import httpx

        root = Path(__file__).resolve().parents[1]
        main_js = (root / "desktop" / "main.js").read_text(encoding="utf-8")
        self.assertIn("UV_PROJECT_ENVIRONMENT", main_js)
        self.assertIn("killProcessTree", main_js)
        icon_bytes = (root / "desktop" / "assets" / "icon.png").read_bytes()
        self.assertEqual(icon_bytes[:8], b"\x89PNG\r\n\x1a\n")
        width = int.from_bytes(icon_bytes[16:20], "big")
        height = int.from_bytes(icon_bytes[20:24], "big")
        self.assertGreaterEqual(width, 512)
        self.assertGreaterEqual(height, 512)

        reset_api_rate_limiter()
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            atoms_dir = brain / "2-atoms"

            # 1. Wikilinks + merge lineage prevent false orphans; quarantined atoms are excluded from graph/radar.
            a1 = _seed_atom(
                brain,
                filename="anchor-rule.md",
                claim="Cryptographic source anchors verify exact byte offsets.",
                evidence="Cryptographic source anchors verify exact byte offsets.",
                project="project:alpha",
                approve=True,
            )
            # Add a Markdown [[wikilink]] to peer-rule inside a2 and re-approve
            a2 = _seed_atom(
                brain,
                filename="peer-rule.md",
                claim="Retention window for audit archives is 365 days. Related: [[anchor-rule]]",
                evidence="Retention window for audit archives is 365 days. Related: [[anchor-rule]]",
                project="project:alpha",
                approve=True,
            )
            q_atom = _seed_atom(
                brain,
                filename="quarantined-trap.md",
                claim="Never verify cryptographic source anchors or byte offsets.",
                evidence="Never verify cryptographic source anchors or byte offsets.",
                project="project:alpha",
                approve=False,
            )
            apply_review_transition(
                action="quarantine",
                atom_path=str(q_atom),
                expected_sha256=content_sha256(q_atom.read_bytes()),
                actor="owner",
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
            )
            draft_atom = _seed_atom(
                brain,
                filename="unapproved-secret.md",
                claim="Pending unapproved draft claim.",
                evidence="Pending unapproved draft claim.",
                project="project:alpha",
                approve=False,
            )

            g = build_knowledge_graph(brain_dir=str(brain), atoms_dir=str(atoms_dir), include_pending=True)
            node_files = {n["filename"] for n in g["nodes"]}
            self.assertNotIn("quarantined-trap.md", node_files)
            self.assertEqual(g["orphan_approved_atoms"], [])
            radar = detect_contradictions(brain_dir=str(brain), atoms_dir=str(atoms_dir), include_pending=True)
            self.assertEqual(radar["contradiction_count"], 0)

            # Merge lineage edge emission when survivor supersedes old atom
            old_dup = _seed_atom(
                brain,
                filename="old-dup.md",
                claim="Legacy retention rule.",
                evidence="Legacy retention rule.",
                project="project:alpha",
                approve=False,
            )
            surv = _seed_atom(
                brain,
                filename="survivor-dup.md",
                claim="Unified retention rule.",
                evidence="Unified retention rule.",
                project="project:alpha",
                approve=False,
            )
            apply_atom_merge(
                survivor_path=str(surv),
                superseded_path=str(old_dup),
                survivor_expected_sha256=content_sha256(surv.read_bytes()),
                superseded_expected_sha256=content_sha256(old_dup.read_bytes()),
                actor="owner",
                rationale="dedup test",
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
            )
            g_merged = build_knowledge_graph(brain_dir=str(brain), atoms_dir=str(atoms_dir), include_pending=True)
            self.assertTrue(
                any(
                    e["from"] == "old-dup.md" and e["to"] == "survivor-dup.md" and e["kind"] == "superseded_by"
                    for e in g_merged["edges"]
                )
            )

            # 2. Stem normalization in prove_atom_membership, malformed proof hardening, and corrupt UTF-8 ledger handling.
            stem_proof = prove_atom_membership("anchor-rule", brain_dir=str(brain), atoms_dir=str(atoms_dir))
            self.assertTrue(verify_merkle_proof(stem_proof))
            self.assertFalse(verify_merkle_proof("not-a-dict"))  # type: ignore[arg-type]
            self.assertFalse(verify_merkle_proof({**stem_proof, "merkle_root": "not-hex"}))
            self.assertFalse(verify_merkle_proof({**stem_proof, "siblings": [{"position": "middle", "sha256": "0" * 64}]}))

            ledger_file = brain / ".sidecar" / "vault-ledger.jsonl"
            ledger_file.write_bytes(b"\xff\xfe\x00corrupt-utf8")
            corrupt_ver = verify_vault_ledger(brain_dir=str(brain), atoms_dir=str(atoms_dir))
            self.assertFalse(corrupt_ver["chain_valid"])
            ledger_file.unlink()

            # 3. Stopword filtering in recall_at_snapshot prevents false matches on "is"/"for".
            snap = create_snapshot(label="stopword-check", actor="owner", brain_dir=str(brain), atoms_dir=str(atoms_dir))
            no_false_hit = recall_at_snapshot("what is for the", snap["snapshot_id"], brain_dir=str(brain))
            self.assertEqual(no_false_hit["results"], [])
            real_hit = recall_at_snapshot(
                "what is the retention window for audit archives?",
                snap["snapshot_id"],
                brain_dir=str(brain),
            )
            self.assertEqual([r["filename"] for r in real_hit["results"]], ["peer-rule.md"])
            self.assertEqual(a1.name, "anchor-rule.md")
            self.assertEqual(a2.name, "peer-rule.md")
            self.assertEqual(draft_atom.name, "unapproved-secret.md")

            # 4. Uniform 404 for non-owner prove requests on unapproved/missing atoms + MemoryApiClient Phase 5 SDK parity.
            tok_path = Path(tmp) / "api-token"
            own_path = Path(tmp) / "owner-token"
            agent_tok, _ = generate_memory_api_token(tok_path)
            owner_tok = ensure_owner_token(own_path)
            rt = MemoryApiRuntimeConfig(
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
                inbox_dir=str(brain / "0-raw" / "inbox"),
                experiences_dir=str(brain / "4-experiences"),
                lexical_index_path=str(brain / ".sidecar" / "lexical.sqlite3"),
                chroma_path=str(brain / ".chroma"),
                telemetry_db_path=str(brain / ".sidecar" / "telemetry.sqlite3"),
                token_path=tok_path,
                owner_token_path=own_path,
                policy_path=Path(tmp) / "policy.json",
            )
            server = MemoryLoopbackHTTPServer("127.0.0.1", 0, rt)
            port = server.server_address[1]
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                base = f"http://127.0.0.1:{port}"
                agent_h = {"Authorization": f"Bearer {agent_tok}", "Host": f"127.0.0.1:{port}"}
                owner_h = {**agent_h, "X-Owner-Token": owner_tok}
                with httpx.Client(trust_env=False, timeout=15) as c:
                    r_unapproved_agent = c.get(f"{base}/v1/ledger/prove/unapproved-secret.md", headers=agent_h)
                    r_missing_agent = c.get(f"{base}/v1/ledger/prove/nonexistent-atom.md", headers=agent_h)
                    self.assertEqual(r_unapproved_agent.status_code, 404)
                    self.assertEqual(r_missing_agent.status_code, 404)
                    self.assertEqual(r_unapproved_agent.json(), r_missing_agent.json())
                    r_unapproved_owner = c.get(f"{base}/v1/ledger/prove/unapproved-secret.md", headers=owner_h)
                    self.assertEqual(r_unapproved_owner.status_code, 403)

                sdk_owner = MemoryApiClient(port=port, token=agent_tok, owner_token=owner_tok)
                committed = sdk_owner.commit_ledger(actor="sdk-owner", note="sdk-checkpoint")
                self.assertEqual(committed["seq"], 1)
                verified = sdk_owner.verify_ledger()
                self.assertTrue(verified["chain_valid"])
                proof = sdk_owner.prove_atom("anchor-rule")
                self.assertTrue(verify_merkle_proof(proof))
                summary = sdk_owner.graph_summary(project_scope="project:alpha")
                self.assertEqual(summary["node_count"], 2)
                contradictions = sdk_owner.graph_contradictions(project_scope="project:alpha")
                self.assertEqual(contradictions["contradiction_count"], 0)
                snap2 = sdk_owner.create_snapshot(actor="sdk-owner", label="sdk-snap")
                snaps = sdk_owner.list_snapshots()
                self.assertGreaterEqual(len(snaps["snapshots"]), 2)
                diff = sdk_owner.diff_snapshots(snap["snapshot_id"], str(snap2["snapshot_id"]))
                self.assertEqual(diff["added_atoms"], [])
                recalled = sdk_owner.recall_snapshot("retention window audit archives", str(snap2["snapshot_id"]))
                self.assertEqual([r["filename"] for r in recalled["results"]], ["peer-rule.md"])
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
