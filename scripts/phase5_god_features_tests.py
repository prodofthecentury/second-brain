# /// script
# requires-python = ">=3.11,<3.14"
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

            ledger_file = brain / "0-raw" / "archive" / "vault-ledger.jsonl"
            self.assertTrue(ledger_file.is_file())
            self.assertFalse((brain / ".sidecar" / "vault-ledger.jsonl").exists())
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

                rem1 = sdk_owner.remember("Use uv sync --frozen for reproducible Python builds.", title="UV Rule", tags=["python", "ci"])
                self.assertEqual(rem1["status"], "saved")
                sdk_owner.remember("Frontend styles live in styles.css.", title="CSS Rule", tags=["frontend"])
                by_q_and_tag = sdk_owner.list_notes(query="reproducible", tag="python")
                self.assertEqual(by_q_and_tag["total"], 1)
                self.assertEqual(by_q_and_tag["items"][0]["title"], "UV Rule")
                by_wrong_tag = sdk_owner.list_notes(query="reproducible", tag="frontend")
                self.assertEqual(by_wrong_tag["total"], 0)

                dash_py = (root / "dashboard" / "app.py").read_text(encoding="utf-8")
                self.assertIn("📝 Working Notes & Import", dash_py)
                self.assertIn("🛡️ Vault Proof & Time-Travel", dash_py)
                ext_js = (root / "extensions" / "vscode-second-brain" / "extension.js").read_text(encoding="utf-8")
                self.assertIn("secondBrain.verifyLedger", ext_js)
                self.assertIn("secondBrain.scanContradictions", ext_js)
                self.assertIn("secondBrain.assemblePlan", ext_js)
            finally:
                server.shutdown()
                server.server_close()

    def test_sweep_radar_antonyms_units_drift_bm25_snapshots_and_sdk_parity(self) -> None:
        import threading
        import httpx
        from api_product import ensure_owner_token
        from atom_mutator import content_sha256
        from executive_planner import ExecutivePlanRequest, assemble_executive_plan
        from knowledge_graph import build_knowledge_graph, detect_contradictions
        from lexical_store import rebuild_lexical_index
        from memory_api import (
            MemoryApiClient,
            MemoryApiRuntimeConfig,
            MemoryLoopbackHTTPServer,
            generate_memory_api_token,
        )
        from skill_playbooks import promote_skill_playbook
        from time_travel import create_snapshot, diff_snapshots, recall_at_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            atoms_dir = brain / "2-atoms"
            pb_dir = brain / "5-playbooks"
            pb_dir.mkdir(parents=True, exist_ok=True)

            # 1. Double-negation ("is not invalid" vs "is valid") & equivalent units ("5000ms" vs "5s") produce 0 false alerts.
            _seed_atom(
                brain,
                filename="timeout-ms.md",
                claim="Gateway request timeout is 5000ms for internal RPC calls.",
                evidence="Gateway request timeout is 5000ms for internal RPC calls.",
                project="project:core",
                approve=True,
            )
            _seed_atom(
                brain,
                filename="timeout-sec-equal.md",
                claim="Gateway request timeout is 5s for internal RPC calls.",
                evidence="Gateway request timeout is 5s for internal RPC calls.",
                project="project:core",
                approve=True,
            )
            _seed_atom(
                brain,
                filename="cert-valid-a.md",
                claim="Mutual TLS client certificate chain is valid during rotation.",
                evidence="Mutual TLS client certificate chain is valid during rotation.",
                project="project:core",
                approve=True,
            )
            _seed_atom(
                brain,
                filename="cert-valid-b.md",
                claim="Mutual TLS client certificate chain is not invalid during rotation.",
                evidence="Mutual TLS client certificate chain is not invalid during rotation.",
                project="project:core",
                approve=True,
            )
            radar_clean = detect_contradictions(brain_dir=str(brain), atoms_dir=str(atoms_dir), project_scope="project:core")
            self.assertEqual(radar_clean["contradiction_count"], 0, radar_clean["alerts"])
            self.assertEqual(radar_clean["drift_count"], 0)

            # 2. Curated antonym pair ("synchronous" vs "asynchronous") & conflicting unit-normalized duration ("15s" vs "5000ms").
            _seed_atom(
                brain,
                filename="timeout-conflict.md",
                claim="Gateway request timeout is 15s for internal RPC calls.",
                evidence="Gateway request timeout is 15s for internal RPC calls.",
                project="project:core",
                approve=True,
            )
            _seed_atom(
                brain,
                filename="dispatch-sync.md",
                claim="Audit event dispatcher uses synchronous writes on every request.",
                evidence="Audit event dispatcher uses synchronous writes on every request.",
                project="project:core",
                approve=True,
            )
            _seed_atom(
                brain,
                filename="dispatch-async.md",
                claim="Audit event dispatcher uses asynchronous writes on every request.",
                evidence="Audit event dispatcher uses asynchronous writes on every request.",
                project="project:core",
                approve=True,
            )
            radar_conflicts = detect_contradictions(brain_dir=str(brain), atoms_dir=str(atoms_dir), project_scope="project:core")
            reasons = {r for a in radar_conflicts["alerts"] for r in a["reasons"]}
            self.assertIn("numeric_value_conflict", reasons)
            self.assertIn("antonym_conflict", reasons)

            # 3. Source tampering triggers drift_alerts in Contradiction & Drift Radar; contested_by emits graph edges.
            tamper_src = brain / "0-raw" / "src-dispatch-async.md"
            orig_src_bytes = tamper_src.read_bytes()
            tamper_src.write_bytes(b"TAMPERED SOURCE BYTES ON DISK")
            radar_drift = detect_contradictions(brain_dir=str(brain), atoms_dir=str(atoms_dir), project_scope="project:core")
            self.assertGreaterEqual(radar_drift["drift_count"], 1)
            self.assertTrue(any(d["filename"] == "dispatch-async.md" for d in radar_drift["drift_alerts"]))
            tamper_src.write_bytes(orig_src_bytes)

            contested_path = _seed_atom(
                brain,
                filename="contested-claim.md",
                claim="Legacy cache layer is stateless.",
                evidence="Legacy cache layer is stateless.",
                project="project:core",
                approve=False,
            )
            contested_txt = contested_path.read_text(encoding="utf-8").replace(
                "review_status: pending",
                "contested_by:\n  - dispatch-sync.md\nreview_status: pending",
            )
            contested_path.write_text(contested_txt, encoding="utf-8")
            kg = build_knowledge_graph(brain_dir=str(brain), atoms_dir=str(atoms_dir), include_pending=True)
            self.assertTrue(any(e["kind"] == "contested_by" and e["to"] == "dispatch-sync.md" for e in kg["edges"]))

            # 4. BM25 snapshot recall + playbook_changes diff + preference privacy + full SDK parity.
            pref_atom = _seed_atom(
                brain,
                filename="owner-pref.md",
                claim="Prefer dark solarized terminal themes for local debugging.",
                evidence="Prefer dark solarized terminal themes for local debugging.",
                project="project:core",
                approve=True,
            )
            # Re-seed a real preference atom so non-owner snapshot diff/recall filters it out.
            pref_src = brain / "0-raw" / "pref-real.src.md"
            pref_src.write_text("Prefer Concise Mermaid Sequence Diagrams.", encoding="utf-8")
            from evidence import find_evidence_anchor
            from atom_mutator import apply_review_transition

            pref_draft = (
                "---\nid: pref-mermaid\ntype: atom\nmemory_type: semantic\nepistemic_label: preference\n"
                "sources:\n  - 0-raw/pref-real.src.md\ncontexts:\n  - project:core\nreview_status: pending\ncitation_integrity: anchored\n---\n\n"
                "## Claim\nPrefer Concise Mermaid Sequence Diagrams.\n\n## Evidence\nPrefer Concise Mermaid Sequence Diagrams.\n"
            )
            pref_anc = find_evidence_anchor(pref_draft, str(brain))
            p_parts = pref_draft.split("---", 2)
            p_fm = p_parts[1] + "".join(f"{k}: {v}\n" for k, v in pref_anc.items())
            pref_real_path = atoms_dir / "pref-mermaid.md"
            pref_real_path.write_text(f"---{p_fm}---{p_parts[2]}", encoding="utf-8")
            apply_review_transition(
                action="approve",
                atom_path=str(pref_real_path),
                expected_sha256=content_sha256(pref_real_path.read_bytes()),
                actor="owner",
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
            )

            snap_before_pb = create_snapshot(label="before-pb", actor="owner", brain_dir=str(brain), atoms_dir=str(atoms_dir), playbooks_dir=str(pb_dir))
            self.assertEqual(snap_before_pb["schema_version"], 3)

            proc_atom = _seed_atom(
                brain,
                filename="deploy-canary.md",
                claim="Verify Merkle ledger root before cutting release artifacts.",
                evidence="Verify Merkle ledger root before cutting release artifacts.",
                memory_type="procedural",
                project="project:core",
                approve=True,
            )
            promote_skill_playbook(
                playbook_id="merkle-release-gate",
                title="Merkle Release Gate",
                backing_atoms=[("deploy-canary.md", content_sha256(proc_atom.read_bytes()))],
                trigger="Cutting a production release",
                preconditions=["Clean git status"],
                steps=["Verify Merkle ledger root before cutting release artifacts."],
                expected_outputs=["Signed SHA256SUMS.txt"],
                failure_recovery=["Abort release"],
                actor="owner",
                rationale="sweep test",
                allow_early_owner_override=True,
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
                playbooks_dir=str(pb_dir),
                telemetry_db_path=str(brain / ".sidecar" / "telemetry.sqlite3"),
            )
            snap_after_pb = create_snapshot(label="after-pb", actor="owner", brain_dir=str(brain), atoms_dir=str(atoms_dir), playbooks_dir=str(pb_dir))
            pb_diff = diff_snapshots(snap_before_pb["snapshot_id"], snap_after_pb["snapshot_id"], brain_dir=str(brain), atoms_dir=str(atoms_dir), playbooks_dir=str(pb_dir))
            self.assertEqual([p["filename"] for p in pb_diff["playbook_changes"]["added"]], ["merkle-release-gate.md"])

            bm25_res = recall_at_snapshot("Merkle ledger root release artifacts", snap_after_pb["snapshot_id"], brain_dir=str(brain))
            self.assertEqual(bm25_res["results"][0]["filename"], "deploy-canary.md")

            lex_db = str(brain / ".sidecar" / "lexical.sqlite3")
            rebuild_lexical_index(str(atoms_dir), lex_db)
            plan = assemble_executive_plan(
                ExecutivePlanRequest(
                    goal="Cutting a production release with Merkle ledger verification",
                    project_scope="project:core",
                    active_context_profile=("project:core",),
                ),
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
                playbooks_dir=str(pb_dir),
                lexical_index_path=lex_db,
                chroma_path=str(brain / ".chroma"),
                telemetry_db_path=str(brain / ".sidecar" / "telemetry.sqlite3"),
                save_briefing=False,
            )
            self.assertEqual(plan.matched_playbook_ids[0], "merkle-release-gate")

            # 5. Live HTTP API & SDK verification for newly added routes and non-owner guards.
            tok_path = Path(tmp) / "api-tok"
            own_path = Path(tmp) / "own-tok"
            agent_tok, _ = generate_memory_api_token(tok_path)
            owner_tok = ensure_owner_token(own_path)
            rt = MemoryApiRuntimeConfig(
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
                inbox_dir=str(brain / "0-raw" / "inbox"),
                experiences_dir=str(brain / "4-experiences"),
                playbooks_dir=str(pb_dir),
                lexical_index_path=lex_db,
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
                sdk_owner = MemoryApiClient(port=port, token=agent_tok, owner_token=owner_tok)
                sdk_agent = MemoryApiClient(port=port, token=agent_tok)

                sdk_owner.commit_ledger(actor="owner", note="sweep")
                proof = sdk_owner.prove_atom("deploy-canary.md")
                self.assertTrue(sdk_owner.verify_proof(proof)["valid"])

                audit_rep = sdk_owner.audit_sources()
                self.assertEqual(audit_rep["stale_count"], 0)

                fb_ok = sdk_owner.record_feedback("deploy-canary.md", helpful=True)
                self.assertEqual(fb_ok["status"], "recorded")
                with self.assertRaises(httpx.HTTPStatusError) as missing_fb:
                    sdk_owner.record_feedback("does-not-exist.md", helpful=False)
                self.assertEqual(missing_fb.exception.response.status_code, 404)

                dups = sdk_owner.atom_duplicates()
                self.assertIn("duplicates", dups)
                for d in dups["duplicates"]:
                    self.assertIn("similarity", d)

                cands = sdk_owner.playbook_candidates()
                self.assertTrue(any(c["atom_id"] == "deploy-canary.md" and c["claim"] for c in cands["candidates"]))

                # Agent snapshot recall must never leak preference atoms.
                agent_snap_recall = sdk_agent.recall_snapshot("Mermaid Sequence Diagrams", snap_after_pb["snapshot_id"])
                self.assertEqual(agent_snap_recall["results"], [])
                owner_snap_recall = sdk_owner.recall_snapshot("Mermaid Sequence Diagrams", snap_after_pb["snapshot_id"])
                self.assertEqual([r["filename"] for r in owner_snap_recall["results"]], ["pref-mermaid.md"])

                # Non-owner oversized POST to /v1/notes is rejected at 64 KiB (413) before body allocation.
                with httpx.Client(trust_env=False, timeout=10) as raw_c:
                    r_big_agent = raw_c.post(
                        f"http://127.0.0.1:{port}/v1/notes",
                        headers={"Authorization": f"Bearer {agent_tok}", "Host": f"127.0.0.1:{port}"},
                        json={"title": "Big", "body": "x" * 70_000},
                    )
                    self.assertEqual(r_big_agent.status_code, 413)
                big_owner_note = sdk_owner.create_note("Big Owner Note", "x" * 70_000)
                self.assertEqual(big_owner_note["status"], "saved")
            finally:
                server.shutdown()
                server.server_close()
                _ = pref_atom

    def test_system_sweep_security_intake_revocation_offset_and_client_guards(self) -> None:
        import httpx
        from api_product import ensure_owner_token
        from atom_mutator import ReviewConflictError, apply_review_transition, content_sha256
        from inbox_workflow import inspect_inbox_item, list_inbox_items, promote_inbox_item
        from memory_api import (
            MemoryApiClient,
            MemoryApiRuntimeConfig,
            MemoryLoopbackHTTPServer,
            generate_memory_api_token,
            reset_api_rate_limiter,
        )
        from ollama_scout import create_intake_pointer
        from refinery_targets import intake_status
        from sota_benchmark import run_sota_benchmark
        from time_travel import create_snapshot, verify_snapshot

        root = Path(__file__).resolve().parents[1]

        # 1. VS Code Workspace-Trust apiPort exfiltration guard & Electron sidecar readiness guard
        vscode_pkg = json.loads((root / "extensions" / "vscode-second-brain" / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(
            vscode_pkg["contributes"]["configuration"]["properties"]["secondBrain.apiPort"].get("scope"),
            "application",
        )
        restricted = vscode_pkg.get("capabilities", {}).get("untrustedWorkspaces", {}).get("restrictedConfigurations", [])
        self.assertIn("secondBrain.apiPort", restricted)
        vscode_ext = (root / "extensions" / "vscode-second-brain" / "extension.js").read_text(encoding="utf-8")
        self.assertIn('inspect("apiPort")', vscode_ext)
        self.assertNotIn("innerHTML", vscode_ext)

        main_js = (root / "desktop" / "main.js").read_text(encoding="utf-8")
        self.assertIn("SECOND_BRAIN_API_READY", main_js)
        self.assertIn("apiProcess.exitCode !== null", main_js)
        app_js = (root / "desktop" / "renderer" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("acknowledge_risks: true", app_js)
        self.assertNotIn("innerHTML", app_js)

        # 2. SOTA benchmark upgraded baselines & statistical metrics
        bench = run_sota_benchmark(atoms=80)
        self.assertIn("tfidf_cosine_rag", bench["systems"])
        sb_sys = bench["systems"]["second_brain_verified"]
        for metric_key in ("mrr_at_5", "ndcg_at_5", "p99_latency_ms", "recall_ci95"):
            self.assertIn(metric_key, sb_sys)

        # 3. Web Scout .intake quarantine -> inspect -> CAS conflict -> promote + revoked snapshot redaction + offset bound
        reset_api_rate_limiter()
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            raw_dir = brain / "0-raw"
            inbox_dir = raw_dir / "inbox"
            sources_dir = brain / "sources"
            atoms_dir = brain / "2-atoms"
            for d in (raw_dir, inbox_dir, sources_dir, atoms_dir, brain / ".sidecar"):
                d.mkdir(parents=True, exist_ok=True)

            (sources_dir / "2026-11-15-scout-doc.md").write_text(
                "---\nurl: https://example.com/a\norigin: web_remote\n---\n\nClean technical article content on Merkle trees.\n",
                encoding="utf-8",
            )
            ptr_path = Path(create_intake_pointer("2026-11-15-scout-doc.md", raw_dir=str(raw_dir)))
            self.assertEqual(intake_status(ptr_path.read_text(encoding="utf-8")), "quarantined")

            queued = list_inbox_items(inbox_dir=str(inbox_dir))
            self.assertTrue(any(item.filename == ptr_path.name for item in queued))
            inspected = inspect_inbox_item(ptr_path.name, inbox_dir=str(inbox_dir))
            self.assertIn("Merkle trees", str(inspected["content"]))

            with self.assertRaises(ReviewConflictError):
                promote_inbox_item(
                    ptr_path.name,
                    expected_sha256="0" * 64,
                    actor="owner",
                    inbox_dir=str(inbox_dir),
                    raw_dir=str(raw_dir),
                    brain_dir=str(brain),
                )

            promoted = promote_inbox_item(
                ptr_path.name,
                expected_sha256=str(inspected["sha256"]),
                actor="owner",
                inbox_dir=str(inbox_dir),
                raw_dir=str(raw_dir),
                brain_dir=str(brain),
            )
            self.assertEqual(promoted["filename"], ptr_path.name)
            self.assertEqual(intake_status(ptr_path.read_text(encoding="utf-8")), "approved")

            # Snapshot revocation isolation + tamper check + SQLite offset overflow clamping
            rev_atom = _seed_atom(
                brain,
                filename="revoked-later.md",
                claim="Temporary staging credential rotation token window is 7 hours.",
                evidence="Temporary staging credential rotation token window is 7 hours.",
                project="project:sec",
                approve=True,
            )
            snap1 = create_snapshot(label="pre-revoke", actor="owner", brain_dir=str(brain), atoms_dir=str(atoms_dir))
            self.assertTrue(verify_snapshot(snap1["snapshot_id"], brain_dir=str(brain))["valid"])

            # Quarantine the atom AFTER snap1 was recorded
            apply_review_transition(
                action="quarantine",
                atom_path=str(rev_atom),
                expected_sha256=content_sha256(rev_atom.read_bytes()),
                actor="owner",
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
            )

            tok_path = Path(tmp) / "api-token"
            own_path = Path(tmp) / "owner-token"
            agent_tok, _ = generate_memory_api_token(tok_path)
            owner_tok = ensure_owner_token(own_path)
            rt = MemoryApiRuntimeConfig(
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
                inbox_dir=str(inbox_dir),
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
                sdk_agent = MemoryApiClient(port=port, token=agent_tok)
                sdk_owner = MemoryApiClient(port=port, token=agent_tok, owner_token=owner_tok)

                # Agent must NOT recall or see claim text of a subsequently quarantined atom!
                self.assertEqual(sdk_agent.recall_snapshot("staging credential rotation", snap1["snapshot_id"])["results"], [])
                agent_diff = sdk_agent.diff_snapshots(snap1["snapshot_id"], "live")
                self.assertEqual(agent_diff["removed_atoms"][0]["claim"], "")
                self.assertTrue(agent_diff["removed_atoms"][0].get("redacted_revoked"))

                # Owner retains full point-in-time historical recall & snapshot verification
                owner_recall = sdk_owner.recall_snapshot("staging credential rotation", snap1["snapshot_id"])
                self.assertEqual([r["filename"] for r in owner_recall["results"]], ["revoked-later.md"])
                self.assertTrue(sdk_owner.verify_snapshot(snap1["snapshot_id"])["valid"])

                # Huge 128-bit offset query param must never 500 with SQLite OverflowError
                with httpx.Client(trust_env=False, timeout=10) as raw_c:
                    r_huge_offset = raw_c.get(
                        f"http://127.0.0.1:{port}/v1/atoms?offset=999999999999999999999999999",
                        headers={"Authorization": f"Bearer {agent_tok}", "Host": f"127.0.0.1:{port}"},
                    )
                    self.assertIn(r_huge_offset.status_code, (200, 422))
            finally:
                server.shutdown()
                server.server_close()


class Goal1CoreRuntimeDurabilityAndScaleTests(Phase5IsolatedTestCase):
    def test_cli_mcp_token_vault_path_port_discovery_and_readme_argparse_parity(self) -> None:
        import io
        import re
        import shlex
        from contextlib import redirect_stdout
        from scripts import cli, config, ide_connector, mcp_server
        from memory_api import (
            MemoryApiClient,
            discover_active_api_port,
            remove_active_api_port_file,
            write_active_api_port_file,
        )

        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sec_dir = tmp_path / "sec"
            sec_dir.mkdir(parents=True, exist_ok=True)
            switched_vault = tmp_path / "custom-vault"
            switched_vault.mkdir(parents=True, exist_ok=True)
            (sec_dir / "vault-path.txt").write_text(str(switched_vault) + "\n", encoding="utf-8")
            mcp_tok_file = sec_dir / "mcp-token"
            api_port_path = sec_dir / "api-port"

            with patch.dict(
                os.environ,
                {
                    "BRAIN_SECURITY_STATE_DIR": str(sec_dir),
                    "BRAIN_MCP_TOKEN_FILE": str(mcp_tok_file),
                    "BRAIN_API_PORT_FILE": str(api_port_path),
                },
                clear=False,
            ):
                os.environ.pop("BRAIN_VAULT_DIR", None)
                os.environ.pop("SECOND_BRAIN_PORT", None)

                # 1. Unified vault-path.txt resolution across config, cli, mcp_server, ide_connector
                # macOS /var and Windows 8.3 names are the same directory with a different spelling.
                expected_vault = switched_vault.resolve()
                self.assertEqual(Path(config.resolve_active_brain_dir()).resolve(), expected_vault)
                vdir, _, _ = cli._resolve_active_vault_paths()
                self.assertEqual(Path(vdir).resolve(), expected_vault)
                mcp_brain, _, _, _ = mcp_server._effective_mcp_paths()
                self.assertEqual(Path(mcp_brain).resolve(), expected_vault)
                entry = ide_connector.server_entry("test-token-1234567890")
                self.assertNotIn("BRAIN_VAULT_DIR", entry["env"])

                # 2. Fix mcp --generate-token & --print-token (no AttributeError on write_token_file)
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc_gen = cli.main(["mcp", "--generate-token"])
                self.assertEqual(rc_gen, 0)
                self.assertTrue(mcp_tok_file.is_file())
                saved_tok = mcp_tok_file.read_text(encoding="utf-8").strip()
                self.assertGreaterEqual(len(saved_tok), 32)
                buf2 = io.StringIO()
                with redirect_stdout(buf2):
                    rc_print = cli.main(["mcp", "--print-token"])
                self.assertEqual(rc_print, 0)
                self.assertEqual(buf2.getvalue().strip(), saved_tok)

                # 3. Port file persistence, PID check, and SDK / CLI adapter-config discovery
                self.assertEqual(discover_active_api_port(8765), 8765)
                write_active_api_port_file(51923, port_file=api_port_path)
                self.assertEqual(discover_active_api_port(8765, port_file=api_port_path), 51923)
                auto_client = MemoryApiClient(token="x" * 32)
                self.assertIn(":51923", auto_client.base_url)
                buf_cfg = io.StringIO()
                with redirect_stdout(buf_cfg):
                    self.assertEqual(cli.main(["adapter-config"]), 0)
                self.assertIn("51923", buf_cfg.getvalue())
                remove_active_api_port_file(expected_pid=os.getpid(), port_file=api_port_path)
                self.assertEqual(discover_active_api_port(8765, port_file=api_port_path), 8765)

                # Dead PID in api-port falls back cleanly to 8765
                api_port_path.write_text(json.dumps({"port": 59999, "pid": 999_999_991}), encoding="utf-8")
                self.assertEqual(discover_active_api_port(8765, port_file=api_port_path), 8765)

                # 4. Behavioral CLI parity: capture --tags, notes add --title/--body, notes verify --excerpt, import --overwrite-modified
                with patch.dict(os.environ, {"BRAIN_VAULT_DIR": str(switched_vault)}, clear=False):
                    self.assertEqual(cli.main(["install"]), 0)
                    cap_buf = io.StringIO()
                    with redirect_stdout(cap_buf):
                        self.assertEqual(
                            cli.main(
                                [
                                    "capture",
                                    "Idempotent PostgreSQL migration procedures require transactional DDL locks.",
                                    "--title",
                                    "PG Migrations",
                                    "--tags",
                                    "project:alpha,project:shared,db",
                                    "--json",
                                ]
                            ),
                            0,
                        )
                    cap_doc = json.loads(cap_buf.getvalue())
                    note_fname = cap_doc["filename"]

                    ver_buf = io.StringIO()
                    with redirect_stdout(ver_buf):
                        self.assertEqual(
                            cli.main(
                                [
                                    "notes",
                                    "verify",
                                    note_fname,
                                    "--excerpt",
                                    "Idempotent PostgreSQL migration procedures require transactional DDL locks.",
                                    "--actor",
                                    "cli-owner",
                                    "--json",
                                ]
                            ),
                            0,
                        )
                    ver_doc = json.loads(ver_buf.getvalue())
                    atom_fname = ver_doc.get("atom_filename") or ver_doc.get("filename")
                    from memory_review import parse_atom_record

                    atom_rec = parse_atom_record(switched_vault / "2-atoms" / str(atom_fname))
                    self.assertIn("project:alpha", atom_rec["contexts"])
                    self.assertIn("project:shared", atom_rec["contexts"])

                    # Add note via --title / --body flags (README syntax)
                    add_buf = io.StringIO()
                    with redirect_stdout(add_buf):
                        self.assertEqual(
                            cli.main(["notes", "add", "--title", "Flag Note", "--body", "Body via flags.", "--json"]),
                            0,
                        )
                    self.assertEqual(json.loads(add_buf.getvalue())["status"], "saved")

                    # Bulk import --overwrite-modified flag
                    ext_dir = tmp_path / "obsidian-src"
                    ext_dir.mkdir()
                    (ext_dir / "ext.md").write_text("# Ext\n\nVersion one.\n", encoding="utf-8")
                    self.assertEqual(cli.main(["import", str(ext_dir), "--tags", "project:alpha,imported"]), 0)
                    imported_note = next((switched_vault / "notes").glob("*ext*.md"))
                    imported_note.write_text(imported_note.read_text(encoding="utf-8") + "\nLocal edit.\n", encoding="utf-8")
                    (ext_dir / "ext.md").write_text("# Ext\n\nVersion two.\n", encoding="utf-8")
                    self.assertEqual(cli.main(["import", str(ext_dir), "--overwrite-modified"]), 0)
                    self.assertIn("Version two.", imported_note.read_text(encoding="utf-8"))

                # 5. CLI argparse --help smoke check across every `second-brain <subcmd>` documented in README.md
                readme_text = (root / "README.md").read_text(encoding="utf-8")
                subcmds_in_readme: set[str] = set()
                for line in readme_text.splitlines():
                    m = re.search(r"\bsecond-brain\s+([a-z0-9-]+)", line)
                    if m:
                        subcmds_in_readme.add(m.group(1))
                self.assertGreaterEqual(len(subcmds_in_readme), 15)
                for subcmd in sorted(subcmds_in_readme):
                    out_h = io.StringIO()
                    with self.assertRaises(SystemExit) as cm, redirect_stdout(out_h):
                        cli.main([subcmd, "--help"])
                    self.assertEqual(cm.exception.code, 0, f"second-brain {subcmd} --help exited {cm.exception.code}")
                    _ = shlex

        # Static checks on setup.ps1 / setup.sh / runbooks relocation
        setup_ps1 = (root / "setup.ps1").read_text(encoding="utf-8")
        self.assertIn("UseProxy", setup_ps1)
        self.assertIn("AllowAutoRedirect", setup_ps1)
        setup_sh = (root / "setup.sh").read_text(encoding="utf-8")
        self.assertIn("--noproxy", setup_sh)
        self.assertIn("--max-redirs 0", setup_sh)
        for rb in ("audit-run.md", "editor-run.md", "refinery-run.md", "scout-run.md", "templates/atom.md", "templates/briefing.md", "templates/thread.md"):
            self.assertTrue((root / "docs" / "runbooks" / rb).is_file(), f"Missing relocated runbook: {rb}")

    def test_three_store_durability_yaml_frontmatter_feedback_redaction_and_sub15ms_scale(self) -> None:
        import time
        import httpx
        from api_product import ensure_owner_token
        from atom_mutator import apply_atom_edit, apply_review_transition, content_sha256
        from cognitive_core import compute_memory_fitness_report, record_atom_feedback, record_retrieval_telemetry
        from knowledge_graph import build_knowledge_graph
        from lexical_store import _fts_query, porter_stem, rebuild_lexical_index
        from memory_api import (
            MemoryApiClient,
            MemoryApiRuntimeConfig,
            MemoryLoopbackHTTPServer,
            generate_memory_api_token,
            reset_api_rate_limiter,
        )
        from memory_review import parse_atom_record, parse_frontmatter_values, parse_scalar_list
        from ollama_utils import generation_candidates
        from recall import RecallOptions, _load_fitness_signals, search_lexical_memory
        from skill_playbooks import deprecate_skill_playbook, promote_skill_playbook
        from time_travel import create_snapshot, export_snapshot, recall_at_snapshot, verify_snapshot
        from vault_ledger import (
            GENESIS_HASH,
            _canonical_json,
            _sha256_text,
            commit_ledger_checkpoint,
            prove_leaf_membership,
            verify_merkle_proof,
            verify_vault_ledger,
        )

        # 1. Spec-compliant YAML/JSON frontmatter & Porter stemmer + FTS stopword slicing
        self.assertEqual(
            parse_scalar_list('["alpha, beta", \'it\'\'s: ok\', bare_tag]'),
            ["alpha, beta", "it's: ok", "bare_tag"],
        )
        fm_parsed = parse_frontmatter_values(
            [
                'confidence: 0.90 # keep exact trailing zero',
                'created: 2026-11-16',
                'tags: ["one, two", "three: four"]',
                'contexts:',
                '  - "project:alpha"',
                "  - 'project:beta'",
            ]
        )
        self.assertEqual(fm_parsed["confidence"], "0.90")
        self.assertEqual(fm_parsed["created"], "2026-11-16")
        self.assertEqual(fm_parsed["tags"], ["one, two", "three: four"])
        self.assertEqual(fm_parsed["contexts"], ["project:alpha", "project:beta"])

        self.assertEqual(porter_stem("nodes"), "node")
        self.assertEqual(porter_stem("node"), "node")
        self.assertEqual(porter_stem("idempotency"), porter_stem("idempotent"))
        long_stop_q = " ".join(["what", "is", "the", "for", "with", "that", "from", "should"] * 5 + ["postgresql"])
        self.assertEqual(_fts_query(long_stop_q), '"postgresql"')

        # 2. qwen3.6:latest excluded on constrained RAM unless explicitly requested
        with patch("ollama_utils.installed_model_names", return_value=["qwen2.5-coder:7b", "qwen3.6:latest", "llama3.2:3b"]):
            with patch("hardware_profile.detect_hardware_profile") as mock_hw:
                mock_hw.return_value.ram_available_gb = 10.0
                mock_hw.return_value.vram_gb = 0.0
                cands_default = generation_candidates("llama3.2:3b")
                self.assertNotIn("qwen3.6:latest", cands_default)
                cands_explicit = generation_candidates("qwen3.6:latest")
                self.assertEqual(cands_explicit[0], "qwen3.6:latest")

        reset_api_rate_limiter()
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            atoms_dir = brain / "2-atoms"
            pb_dir = brain / "5-playbooks"
            pb_dir.mkdir(parents=True, exist_ok=True)

            # Seed multi-project procedural atom + preference atom
            proc_atom = _seed_atom(
                brain,
                filename="pg-idempotent.md",
                claim="PostgreSQL schema migrations must be idempotent across multi-region nodes.",
                evidence="PostgreSQL schema migrations must be idempotent across multi-region nodes.",
                project="project:alpha",
                memory_type="procedural",
                approve=True,
            )
            # Add second project tag ("project:shared") and re-approve
            edited_txt = proc_atom.read_text(encoding="utf-8").replace(
                "  - project:alpha\n", "  - project:alpha\n  - project:shared\n"
            )
            apply_atom_edit(
                atom_path=str(proc_atom),
                expected_sha256=content_sha256(proc_atom.read_bytes()),
                edited_content=edited_txt,
                actor="owner",
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
            )
            apply_review_transition(
                action="approve",
                atom_path=str(proc_atom),
                expected_sha256=content_sha256(proc_atom.read_bytes()),
                actor="owner",
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
            )

            rec_parsed = parse_atom_record(proc_atom)
            self.assertIn("PostgreSQL", rec_parsed["evidence"])

            # Multi-project scope works across recall, knowledge_graph, and time_travel
            lex_db = str(brain / ".sidecar" / "lexical.sqlite3")
            rebuild_lexical_index(str(atoms_dir), lex_db)
            # Single-term & stem recall ("PostgreSQL", "idempotency")
            one_term_hits, _ = search_lexical_memory(
                "PostgreSQL",
                RecallOptions(n_results=5, projects=("project:alpha",)),
                atoms_dir=str(atoms_dir),
                index_path=lex_db,
            )
            self.assertEqual([h.filename for h in one_term_hits], ["pg-idempotent.md"])
            kg_alpha = build_knowledge_graph(brain_dir=str(brain), atoms_dir=str(atoms_dir), project_scope="project:alpha")
            self.assertEqual([n["filename"] for n in kg_alpha["nodes"]], ["pg-idempotent.md"])

            # Promote playbook & prove BOTH atom and playbook leaves; migrate legacy .sidecar ledger
            promote_skill_playbook(
                playbook_id="pg-migration-pb",
                title="PG Migration Playbook",
                backing_atoms=[("pg-idempotent.md", content_sha256(proc_atom.read_bytes()))],
                trigger="Running PostgreSQL DDL",
                preconditions=["Backup verified"],
                steps=["PostgreSQL schema migrations must be idempotent across multi-region nodes."],
                expected_outputs=["Zero lock timeouts"],
                failure_recovery=["Rollback tx"],
                actor="owner",
                rationale="goal1 test",
                allow_early_owner_override=True,
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
                playbooks_dir=str(pb_dir),
                telemetry_db_path=str(brain / ".sidecar" / "telemetry.sqlite3"),
            )
            pb_proof = prove_leaf_membership("5-playbooks/pg-migration-pb.md", brain_dir=str(brain), atoms_dir=str(atoms_dir), playbooks_dir=str(pb_dir))
            self.assertTrue(verify_merkle_proof(pb_proof))

            legacy_ledger = brain / ".sidecar" / "vault-ledger.jsonl"
            legacy_entry = {
                "schema_version": 1,
                "seq": 1,
                "timestamp": "2026-11-16T00:00:00+00:00",
                "actor": "owner",
                "note": "legacy",
                "prev_entry_sha256": GENESIS_HASH,
                "merkle_root": pb_proof["merkle_root"],
                "leaf_count": 2,
                "approved_atoms": 1,
                "accepted_playbooks": 1,
            }
            legacy_entry["entry_sha256"] = _sha256_text(_canonical_json(legacy_entry))
            legacy_ledger.write_text(_canonical_json(legacy_entry) + "\n", encoding="utf-8")
            self.assertTrue(verify_vault_ledger(brain_dir=str(brain), atoms_dir=str(atoms_dir), playbooks_dir=str(pb_dir))["chain_valid"])
            cp2 = commit_ledger_checkpoint(brain_dir=str(brain), atoms_dir=str(atoms_dir), playbooks_dir=str(pb_dir), actor="owner", note="migrated")
            self.assertEqual(cp2["seq"], 2)
            self.assertTrue((brain / "0-raw" / "archive" / "vault-ledger.jsonl").is_file())
            self.assertFalse(legacy_ledger.exists())

            # Content-addressed O(changed) snapshots + blob tamper check + Porter BM25 ("node" matches "nodes")
            s1 = create_snapshot(label="s1", actor="owner", brain_dir=str(brain), atoms_dir=str(atoms_dir), playbooks_dir=str(pb_dir))
            s2 = create_snapshot(label="s2", actor="owner", brain_dir=str(brain), atoms_dir=str(atoms_dir), playbooks_dir=str(pb_dir))
            blobs_dir = brain / "0-raw" / "archive" / "snapshots" / "blobs"
            blob_files = list(blobs_dir.rglob("*.json"))
            self.assertEqual(len(blob_files), 2)  # 1 atom + 1 playbook deduplicated across 2 snapshots!
            on_disk_s1 = json.loads((brain / "0-raw" / "archive" / "snapshots" / f"{s1['snapshot_id']}.json").read_text(encoding="utf-8"))
            self.assertNotIn("atoms", on_disk_s1)
            self.assertIn("atom_refs", on_disk_s1)
            porter_hit = recall_at_snapshot("node", s1["snapshot_id"], project_scope="project:alpha", brain_dir=str(brain))
            self.assertEqual(porter_hit["results"][0]["filename"], "pg-idempotent.md")
            exp_dir = Path(tmp) / "exported-snap"
            exp_res = export_snapshot(s1["snapshot_id"], str(exp_dir), brain_dir=str(brain))
            self.assertEqual(exp_res["atom_count"], 1)

            # Feedback durability (actor, note, supersession vs correction, no ret_hits*0.5, survives telemetry DB deletion)
            tel_db = str(brain / ".sidecar" / "telemetry.sqlite3")
            record_retrieval_telemetry(["pg-idempotent.md"] * 10, db_path=tel_db)
            record_atom_feedback("pg-idempotent.md", helpful=True, actor="alice", note="solid rule", db_path=tel_db, brain_dir=str(brain))
            record_atom_feedback("pg-idempotent.md", helpful=False, actor="alice", note="replaced", feedback_kind="supersession", db_path=tel_db, brain_dir=str(brain))
            fit1 = compute_memory_fitness_report(atoms_dir=str(atoms_dir), brain_dir=str(brain), db_path=tel_db)
            row1 = next(x for x in fit1["high_utility_verified_atoms"] if x["atom_id"] == "pg-idempotent.md")
            self.assertEqual(row1["corrections"], 0)
            self.assertEqual(row1["utility_score"], 1.5)  # pos_fb * 1.5 only (zero ret_hits inflation!)
            os.remove(tel_db)
            fit2 = compute_memory_fitness_report(atoms_dir=str(atoms_dir), brain_dir=str(brain), db_path=tel_db)
            row2 = next(x for x in fit2["high_utility_verified_atoms"] if x["atom_id"] == "pg-idempotent.md")
            self.assertEqual(row2["positive_feedback"], 1)
            sig = _load_fitness_signals("pg-idempotent.md", tel_db, brain_dir=str(brain))
            self.assertEqual(sig["positive_feedback"], 1)

            # Edit v1 -> v2 (new SHA-256) & deprecate playbook, then test non-owner v1 redaction + audit_sources API + 250-atom <15ms SLO
            v1_atom = _seed_atom(
                brain,
                filename="rotating-secret.md",
                claim="Legacy v1 token lifetime is 99 hours.",
                evidence="Legacy v1 token lifetime is 99 hours.",
                project="project:alpha",
                approve=True,
            )
            snap_v1 = create_snapshot(label="v1", actor="owner", brain_dir=str(brain), atoms_dir=str(atoms_dir), playbooks_dir=str(pb_dir))
            # Update source + atom to v2 and re-approve
            (brain / "0-raw" / "src-rotating-secret.md").write_text("Primary record: Updated v2 token lifetime is 12 hours.\n", encoding="utf-8")
            v2_doc = v1_atom.read_text(encoding="utf-8").replace("Legacy v1 token lifetime is 99 hours.", "Updated v2 token lifetime is 12 hours.")
            apply_atom_edit(atom_path=str(v1_atom), expected_sha256=content_sha256(v1_atom.read_bytes()), edited_content=v2_doc, actor="owner", brain_dir=str(brain), atoms_dir=str(atoms_dir))
            apply_review_transition(action="approve", atom_path=str(v1_atom), expected_sha256=content_sha256(v1_atom.read_bytes()), actor="owner", brain_dir=str(brain), atoms_dir=str(atoms_dir))
            deprecate_skill_playbook(
                "pg-migration-pb.md",
                expected_sha256=content_sha256((pb_dir / "pg-migration-pb.md").read_bytes()),
                actor="owner",
                rationale="deprecated for v2",
                brain_dir=str(brain),
                playbooks_dir=str(pb_dir),
            )
            snap_v2 = create_snapshot(label="v2", actor="owner", brain_dir=str(brain), atoms_dir=str(atoms_dir), playbooks_dir=str(pb_dir))

            # Seed 220 synthetic approved atoms for sub-15ms recall & fast paginated GET /v1/atoms check
            bulk_src = brain / "0-raw" / "bulk-src.md"
            bulk_lines = [f"Synthetic rule {i:03d}: partition shard_{i:03d} uses deterministic quorum writes." for i in range(220)]
            bulk_src.write_text("\n".join(bulk_lines) + "\n", encoding="utf-8")
            from evidence import find_evidence_anchor

            dlog_path = brain / "0-raw" / "archive" / "decision-record.jsonl"
            dlog_records = []
            for i, claim_line in enumerate(bulk_lines):
                fn = f"synth-{i:03d}.md"
                draft = (
                    f"---\nid: synth-{i:03d}\ntype: atom\nmemory_type: semantic\nepistemic_label: claim\n"
                    f"sources:\n  - 0-raw/bulk-src.md\ncontexts:\n  - project:alpha\n"
                    f"claim_state: human_reviewed\nreview_status: accepted\ncitation_integrity: anchored\n---\n\n"
                    f"## Claim\n{claim_line}\n\n## Evidence\n{claim_line}\n"
                )
                anc = find_evidence_anchor(draft, str(brain))
                parts = draft.split("---", 2)
                fm_full = parts[1] + "".join(f"{k}: {v}\n" for k, v in anc.items())
                full_bytes = f"---{fm_full}---{parts[2]}".encode("utf-8")
                (atoms_dir / fn).write_bytes(full_bytes)
                dlog_records.append(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "action": "approve",
                            "actor": "owner",
                            "atom_filename": fn,
                            "content_sha256_after": content_sha256(full_bytes),
                        }
                    )
                )
            with open(dlog_path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(dlog_records) + "\n")
            from atom_mutator import invalidate_decision_log_cache

            invalidate_decision_log_cache()
            rebuild_lexical_index(str(atoms_dir), lex_db)

            # Warm once, then measure 20 lexical recall queries over 220+ approved atoms (P95 <= 15 ms)
            search_lexical_memory("shard_010 quorum writes", RecallOptions(n_results=5), atoms_dir=str(atoms_dir), index_path=lex_db)
            latencies_ms = []
            for i in range(20):
                t0 = time.perf_counter()
                hits, _ = search_lexical_memory(
                    f"shard_{i * 10:03d} quorum writes",
                    RecallOptions(n_results=5),
                    atoms_dir=str(atoms_dir),
                    index_path=lex_db,
                )
                latencies_ms.append((time.perf_counter() - t0) * 1000.0)
                self.assertTrue(any(h.filename == f"synth-{i * 10:03d}.md" for h in hits))
            p95_ms = sorted(latencies_ms)[int(0.95 * (len(latencies_ms) - 1))]
            self.assertLessEqual(p95_ms, 15.0, f"220-atom lexical P95 {p95_ms:.2f}ms exceeded 15ms SLO")

            tok_path = Path(tmp) / "api-token"
            own_path = Path(tmp) / "owner-token"
            agent_tok, _ = generate_memory_api_token(tok_path)
            owner_tok = ensure_owner_token(own_path)
            rt = MemoryApiRuntimeConfig(
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
                inbox_dir=str(brain / "0-raw" / "inbox"),
                experiences_dir=str(brain / "4-experiences"),
                playbooks_dir=str(pb_dir),
                lexical_index_path=lex_db,
                chroma_path=str(brain / ".chroma"),
                telemetry_db_path=tel_db,
                token_path=tok_path,
                owner_token_path=own_path,
                policy_path=Path(tmp) / "policy.json",
            )
            server = MemoryLoopbackHTTPServer("127.0.0.1", 0, rt)
            self.assertTrue(hasattr(server, "_worker_pool"))
            self.assertTrue(hasattr(server, "_stream_slots"))
            port = server.server_address[1]
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                sdk_agent = MemoryApiClient(port=port, token=agent_tok)
                sdk_owner = MemoryApiClient(port=port, token=agent_tok, owner_token=owner_tok)

                # Non-owner snapshot recall & diff MUST NOT leak historical v1 claim/evidence or deprecated playbook steps!
                self.assertEqual(sdk_agent.recall_snapshot("Legacy v1 token lifetime", snap_v1["snapshot_id"])["results"], [])
                self.assertEqual(sdk_agent.recall_snapshot("PG Migration Playbook", snap_v1["snapshot_id"])["results"], [])
                ag_diff = sdk_agent.diff_snapshots(snap_v1["snapshot_id"], snap_v2["snapshot_id"])
                mod_entry = next(m for m in ag_diff["modified_atoms"] if m["filename"] == "rotating-secret.md")
                self.assertEqual(mod_entry["old_claim"], "")
                self.assertEqual(mod_entry.get("old_evidence", ""), "")
                pb_rem = ag_diff["playbook_changes"]["removed"][0]
                self.assertEqual(pb_rem["steps"], [])
                self.assertTrue(pb_rem.get("redacted_revoked"))

                # Owner sees full historical v1 -> v2 diff
                own_diff = sdk_owner.diff_snapshots(snap_v1["snapshot_id"], snap_v2["snapshot_id"])
                own_mod = next(m for m in own_diff["modified_atoms"] if m["filename"] == "rotating-secret.md")
                self.assertIn("99 hours", own_mod["old_claim"])

                # Fast paginated GET /v1/atoms
                page = sdk_agent.list_atoms(limit=20, offset=10)
                self.assertEqual(len(page["items"]), 20)
                self.assertGreaterEqual(page["total"], 220)

                # Corrupt source file -> /v1/audit/sources stale_count == 1 -> demote-stale demoted_count == 1
                (brain / "0-raw" / "src-rotating-secret.md").write_text("CORRUPTED ON DISK\n", encoding="utf-8")
                audit_before = sdk_owner.audit_sources()
                self.assertEqual(audit_before["stale_count"], 1)
                with httpx.Client(trust_env=False, timeout=10) as raw_c:
                    r_demote = raw_c.post(
                        f"http://127.0.0.1:{port}/v1/audit/sources/demote-stale",
                        headers={"Authorization": f"Bearer {agent_tok}", "X-Owner-Token": owner_tok, "Host": f"127.0.0.1:{port}"},
                        json={"actor": "owner"},
                    )
                    self.assertEqual(r_demote.status_code, 200)
                    self.assertEqual(r_demote.json()["demoted_count"], 1)

                # Blob tamper fails verify_snapshot cleanly
                blob_files[0].write_text('{"tampered": true}', encoding="utf-8")
                self.assertFalse(verify_snapshot(s1["snapshot_id"], brain_dir=str(brain))["valid"])
            finally:
                server.shutdown()
                server.server_close()

    def test_migrate_legacy_sidecar_snapshots_and_hourly_sweep(self):
        from time_travel import create_snapshot, list_snapshots, migrate_legacy_snapshots, verify_snapshot
        from vault_backup import create_vault_backup, verify_vault_backup
        import hourly_sweep

        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            atoms_dir = brain / "2-atoms"
            raw_arch = brain / "0-raw" / "archive"
            legacy_snaps = brain / ".sidecar" / "snapshots"
            atoms_dir.mkdir(parents=True)
            raw_arch.mkdir(parents=True)
            legacy_snaps.mkdir(parents=True)

            # Create a real snapshot, then convert a second legacy v1 snapshot into .sidecar/snapshots/
            snap_live = create_snapshot(label="modern-v3", actor="owner", brain_dir=str(brain), atoms_dir=str(atoms_dir))
            legacy_id = "snap-20261115T071858Z-deadbeef"
            legacy_doc = {
                "schema_version": 1,
                "snapshot_id": legacy_id,
                "created_at": "2026-11-15T07:18:58+00:00",
                "actor": "owner",
                "label": "legacy-sidecar-v1",
                "merkle_root": snap_live["merkle_root"],
                "atom_count": 0,
                "playbook_count": 0,
                "atoms": {},
                "playbooks": {},
            }
            (legacy_snaps / f"{legacy_id}.json").write_text(json.dumps(legacy_doc, indent=2), encoding="utf-8")

            mig = migrate_legacy_snapshots(brain_dir=str(brain))
            self.assertEqual(mig["migrated_count"], 1)
            self.assertIn(legacy_id, mig["migrated_ids"])
            self.assertFalse((legacy_snaps / f"{legacy_id}.json").exists())
            self.assertTrue((raw_arch / "snapshots" / f"{legacy_id}.json").is_file())
            self.assertTrue(verify_snapshot(legacy_id, brain_dir=str(brain))["valid"])
            self.assertEqual(len(list_snapshots(brain_dir=str(brain))), 2)

            # Verify .sbvault backup now captures both snapshots
            bundle_path = Path(tmp) / "migrated.sbvault"
            create_vault_backup(str(bundle_path), passphrase="StrongTestBackupPassword123!", brain_dir=str(brain))
            ver = verify_vault_backup(str(bundle_path), passphrase="StrongTestBackupPassword123!")
            self.assertTrue(ver["valid"])

            # Verify hourly_sweep writes latest.json and appends history.jsonl
            sweep = hourly_sweep.run_hourly_sweep(brain_dir=str(brain), repo_root=str(hourly_sweep.REPO_ROOT))
            self.assertIn("scores", sweep)
            self.assertGreaterEqual(sweep["scores"]["overall_completion"], 70)
            self.assertTrue((brain / ".sidecar" / "hourly-sweeps" / "latest.json").is_file())
            self.assertTrue((brain / ".sidecar" / "hourly-sweeps" / "history.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
