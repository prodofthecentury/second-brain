# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""Tests for OS-Signed Desktop Installers, SOTA Benchmark, Merkle Ledger, Contradiction Radar & Time-Travel."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _seed_atom(
    brain: Path,
    *,
    filename: str,
    claim: str,
    evidence: str,
    project: str = "project:alpha",
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
        "memory_type: semantic\n"
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


class DesktopInstallerAndTrayTests(unittest.TestCase):
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

        main_js = (root / "desktop" / "main.js").read_text(encoding="utf-8")
        for sym in ("refreshTrayMenu", "stopApiSidecar", "getVaultDir", "setLoginItemSettings", "shell.openPath"):
            self.assertIn(sym, main_js)

        self.assertTrue((root / "scripts" / "package_desktop.ps1").is_file())
        self.assertTrue((root / "scripts" / "package_desktop.sh").is_file())


class SotaBenchmarkTests(unittest.TestCase):
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


class VaultMerkleLedgerTests(unittest.TestCase):
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


class KnowledgeGraphAndContradictionRadarTests(unittest.TestCase):
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

            nbrs = graph_neighbors("tls-on.md", brain_dir=str(brain), atoms_dir=str(atoms_dir))
            self.assertGreaterEqual(nbrs["edge_count"], 1)


class TimeTravelSnapshotAndApiTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
