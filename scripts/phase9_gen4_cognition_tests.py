# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Regression suite for Phase 4 — Epic 2D: Gen-4 Cognitive Architecture ("LKE-X").

Tests all 7 Gen-4 slices offline without external services:
  2D.1 Fractal Zoom L0-L5 + Predictive L0-L3 Bus ledger/lstat invalidation
  2D.2 Continuous Distillation (7-principles/, 8-mental-models/) + Owner CAS + Merkle + drift guard
  2D.3 Knowledge Gravity + Ebbinghaus decay + reversible 2-atoms/.cold-archive/
  2D.4 Living Concept Objects + Confidence Timelines + <=3-hop spreading activation
  2D.5 Meta-Reasoning Router + Cognitive Swarm + Two-Sided Debate Mode
  2D.6 Hippocampal Nightly Replay + World-Model AST/KG/Incident Simulator + Self-Model/Curiosity
  2D.7 Owner-ADR-gated LoRA & Notes-Only CRDT sandboxes + MCP/CLI/REST parity
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


import asyncio
from contextlib import redirect_stdout
import datetime
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent

import scripts.adr_gate as adr_gate  # noqa: E402
import scripts.atom_mutator as atom_mutator  # noqa: E402
import scripts.cognitive_core as cognitive_core  # noqa: E402
import scripts.cognitive_distiller as cognitive_distiller  # noqa: E402
import scripts.cognitive_swarm as cognitive_swarm  # noqa: E402
import scripts.concept_graph as concept_graph  # noqa: E402
import scripts.episodic_replay as episodic_replay  # noqa: E402
import scripts.experimental_crdt_notes as experimental_crdt_notes  # noqa: E402
import scripts.experimental_lora_pipeline as experimental_lora_pipeline  # noqa: E402
import scripts.fractal_zoom as fractal_zoom  # noqa: E402
import scripts.meta_router as meta_router  # noqa: E402
import scripts.mcp_server as mcp_server  # noqa: E402
import scripts.predictive_memory_bus as predictive_memory_bus  # noqa: E402
import scripts.recall as recall  # noqa: E402
import scripts.self_model_curiosity as self_model_curiosity  # noqa: E402
import scripts.streaming_chat as streaming_chat  # noqa: E402
import scripts.vault_backup as vault_backup  # noqa: E402
import scripts.vault_ledger as vault_ledger  # noqa: E402
import scripts.world_model_sim as world_model_sim  # noqa: E402
from scripts.evidence import find_evidence_anchor  # noqa: E402
from scripts.executive_planner import ExecutivePlanRequest, assemble_executive_plan  # noqa: E402
from scripts.lexical_store import rebuild_lexical_index  # noqa: E402
from scripts.memory_format import render_memory_document  # noqa: E402


def _seed_vault(root: Path) -> Path:
    brain = root / "brain"
    for sub in (
        "0-raw/inbox",
        "0-raw/archive",
        "0-raw/note-snapshots",
        "1-desk",
        "2-atoms",
        "3-threads",
        "4-experiences",
        "5-playbooks",
        "6-skills",
        "7-principles",
        "8-mental-models",
        "notes",
        "sources",
        "briefings",
        ".sidecar",
    ):
        (brain / sub).mkdir(parents=True, exist_ok=True)
    return brain


def _write_atom(
    brain: Path,
    filename: str,
    *,
    claim: str,
    project: str | None = "project:alpha",
    projects: list[str] | None = None,
    context_domain: str = "architecture",
    tags: list[str] | None = None,
    links: list[str] | None = None,
    created: str = "2026-08-15",
    confidence: float = 0.85,
    epistemic_label: str = "claim",
    epistemic_contract: str | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    extra_fm: dict[str, object] | None = None,
    approve: bool = True,
) -> Path:
    src_rel = f"sources/{filename[:-3]}-src.md"
    src_p = brain / src_rel
    src_p.write_text(f"# Evidence Source\n{claim}\n", encoding="utf-8")

    link_str = " ".join(f"[[{lk}]]" for lk in (links or ()))
    body = f"## Claim\n{claim}\n\n## Evidence\n> {claim}\n\n## Links\n{link_str}\n"
    tag_list = list(tags or [context_domain])
    proj_tags = list(projects) if projects is not None else ([project] if project else [])
    fm: dict[str, object] = {
        "id": filename[:-3],
        "type": "atom",
        "memory_type": "semantic",
        "epistemic_label": epistemic_label,
        "title": filename[:-3],
        "review_status": "pending",
        "claim_state": "unreviewed",
        "citation_integrity": "anchored",
        "certainty": "certain",
        "created": created,
        "last-reviewed": created,
        "importance": 0.8,
        "confidence": confidence,
        "sources": [src_rel],
        "tags": tag_list,
        "contexts": [context_domain, *proj_tags],
    }
    if epistemic_contract:
        fm["epistemic_contract"] = epistemic_contract
    if valid_from:
        fm["valid_from"] = valid_from
    if valid_until:
        fm["valid_until"] = valid_until

    def _render(meta: dict[str, object]) -> str:
        fm_lines = [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items()]
        return render_memory_document(fm_lines, body.splitlines())

    doc = _render(fm)
    fm.update(find_evidence_anchor(doc, str(brain)))
    if extra_fm:
        fm.update(extra_fm)
    raw_bytes = _render(fm).encode("utf-8")
    atom_p = brain / "2-atoms" / filename
    atom_p.write_bytes(raw_bytes)
    if approve:
        atom_mutator.apply_review_transition(
            action="approve",
            atom_path=str(atom_p),
            expected_sha256=atom_mutator.content_sha256(raw_bytes),
            actor="camer",
            reviewed_on=created,
            brain_dir=str(brain),
            atoms_dir=str(brain / "2-atoms"),
            decision_log_path=str(brain / "0-raw" / "archive" / "decision-record.jsonl"),
        )
    return atom_p


class Phase9Gen4CognitionTests(unittest.TestCase):
    def test_2d1_fractal_zoom_and_predictive_bus_invalidation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb_2d1_") as td:
            brain = _seed_vault(Path(td))
            _write_atom(
                brain,
                "alpha-mtls.md",
                claim="Alpha gateway enforces mTLS rotation every 24 hours.",
                project="project:alpha",
                context_domain="security",
                tags=["security", "mtls"],
            )
            _write_atom(
                brain,
                "beta-secret.md",
                claim="Beta billing secret key rotates quarterly.",
                project="project:beta",
                context_domain="security",
                tags=["security"],
            )
            _write_atom(
                brain,
                "alpha-pending.md",
                claim="Pending unverified leak candidate rotation.",
                project="project:alpha",
                approve=False,
            )
            rebuild_lexical_index(atoms_dir=str(brain / "2-atoms"), index_path=str(brain / ".sidecar" / "lexical.sqlite3"))

            z_l5 = fractal_zoom.zoom_retrieve(
                "Alpha mTLS rotation",
                target_level="L5",
                project="project:alpha",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )
            self.assertGreaterEqual(len(z_l5["nodes"]), 1)
            filenames = {n["atom_filename"] for n in z_l5["nodes"]}
            self.assertIn("alpha-mtls.md", filenames)
            self.assertNotIn("beta-secret.md", filenames)
            self.assertNotIn("alpha-pending.md", filenames)
            self.assertIn("mTLS rotation", z_l5["nodes"][0]["summary"])
            self.assertEqual(len(z_l5["path"]), 6)  # L0..L5 progressive path

            # Predictive L0-L3 Bus warm-up and instant cache invalidation on canonical edit
            predictive_memory_bus.invalidate_bus_cache()
            warmed = predictive_memory_bus.record_context_signal(
                active_file="src/alpha_mtls.py",
                git_branch="feat/mtls-rotation",
                project_scope="project:alpha",
                open_symbols=["Alpha", "gateway", "mTLS", "rotation"],
                cursor_topic="Alpha gateway mTLS rotation",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
                lexical_index_path=brain / ".sidecar" / "lexical.sqlite3",
            )
            self.assertEqual(warmed["status"], "warmed")
            self.assertGreaterEqual(warmed["prewarmed_direct_count"], 1)
            q_exact = warmed["predicted_query"]
            hit = predictive_memory_bus.lookup_l0_hot(
                q_exact,
                project_scope="project:alpha",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )
            self.assertTrue(hit["hit"])
            self.assertEqual(hit["tier"], "L0")

            # Add/approve a new atom -> freshness key flips -> L0 cache MUST NOT serve stale hit
            _write_atom(
                brain,
                "alpha-extra.md",
                claim="Alpha audit logs go to WORM ledger.",
                project="project:alpha",
            )
            miss_after_edit = predictive_memory_bus.lookup_l0_hot(
                q_exact,
                project_scope="project:alpha",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )
            self.assertFalse(miss_after_edit["hit"])
            self.assertEqual(miss_after_edit["tier"], "miss")

    def test_2d2_continuous_distillation_cas_merkle_and_drift_invalidation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb_2d2_") as td:
            tpath = Path(td)
            brain = _seed_vault(tpath)
            a1 = _write_atom(brain, "p-one.md", claim="Always pin dependency lockfiles in CI.", project="project:alpha", tags=["ci-hygiene"])
            a2 = _write_atom(brain, "p-two.md", claim="Verify SHA-256 checksums on downloaded wheels.", project="project:beta", tags=["ci-hygiene"])
            a3 = _write_atom(brain, "p-three.md", claim="Disallow unpinned curl pipe bash installers.", project="project:alpha", tags=["ci-hygiene"])
            rebuild_lexical_index(atoms_dir=str(brain / "2-atoms"), index_path=str(brain / ".sidecar" / "lexical.sqlite3"))

            with self.assertRaises(cognitive_distiller.DistillationError):
                cognitive_distiller.propose_principle(
                    title="Too Few Backing Atoms",
                    statement="Needs 3 atoms.",
                    backing_atoms=["p-one.md", "p-two.md"],
                    brain_dir=brain,
                    atoms_dir=brain / "2-atoms",
                )

            draft_pr = cognitive_distiller.propose_principle(
                title="Hermetic Supply Chain Pinning",
                statement="All builds and agent toolchains must pin cryptographic hashes and lockfiles.",
                backing_atoms=["p-one.md", "p-two.md", "p-three.md"],
                filename="hermetic-pinning.md",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )
            self.assertEqual(draft_pr["status"], "draft")
            self.assertEqual(
                cognitive_distiller.list_distilled_artifacts(kind="principle", active_only=True, brain_dir=brain, atoms_dir=brain / "2-atoms"),
                [],
            )

            draft_mm = cognitive_distiller.propose_mental_model(
                title="Supply Chain Blast Model",
                summary="Unpinned installers cause compromised CI artifacts unless lockfile verification prevents drift.",
                edges=[
                    {"from": "unpinned-installer", "relation": "causes", "to": "ci-compromise", "evidence_atom": "p-three.md"},
                    {"from": "lockfile-pinning", "relation": "prevents", "to": "ci-compromise", "evidence_atom": "p-one.md"},
                ],
                backing_atoms=["p-one.md", "p-three.md"],
                filename="supply-chain-model.md",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )

            # Promote both via Owner CAS
            prom_pr = cognitive_distiller.promote_distilled_artifact(
                draft_pr["path"],
                expected_sha256=draft_pr["sha256"],
                actor="camer",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )
            self.assertEqual(prom_pr["status"], "active")
            prom_mm = cognitive_distiller.promote_distilled_artifact(
                draft_mm["path"],
                expected_sha256=draft_mm["sha256"],
                actor="camer",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )
            self.assertEqual(prom_mm["status"], "active")

            # Verify Merkle tree counts + proof + Executive Planner inclusion
            merkle = vault_ledger.compute_vault_merkle(brain_dir=str(brain), atoms_dir=str(brain / "2-atoms"))
            self.assertEqual(merkle["active_principles"], 1)
            self.assertEqual(merkle["active_mental_models"], 1)
            proof = vault_ledger.prove_leaf_membership(
                "principle:hermetic-pinning.md",
                kind="principle",
                brain_dir=str(brain),
                atoms_dir=str(brain / "2-atoms"),
            )
            self.assertTrue(vault_ledger.verify_merkle_proof(proof))

            plan = assemble_executive_plan(
                ExecutivePlanRequest(goal="Pin dependency lockfiles for project:alpha", project_scope="project:alpha", allow_cross_project=True),
                brain_dir=str(brain),
                atoms_dir=str(brain / "2-atoms"),
                lexical_index_path=str(brain / ".sidecar" / "lexical.sqlite3"),
            )
            self.assertTrue(plan.plan_ready)
            self.assertTrue(any("Hermetic" in p or "Hermetic" in s or "pin" in s.lower() for p in plan.preconditions for s in [p]))

            # Backup + restore roundtrip preserves 7-principles & 8-mental-models cleanly
            bk_file = tpath / "backup.sbvault"
            vault_backup.create_vault_backup(str(bk_file), unencrypted=True, brain_dir=str(brain))
            restore_dir = tpath / "restored_vault"
            vault_backup.restore_vault_backup(str(bk_file), str(restore_dir))
            self.assertTrue((restore_dir / "7-principles" / "hermetic-pinning.md").is_file())
            self.assertTrue((restore_dir / "8-mental-models" / "supply-chain-model.md").is_file())

            # Mutating/demoting a backing atom immediately invalidates the active Principle
            atom_mutator.apply_review_transition(
                action="reject",
                atom_path=str(a1),
                expected_sha256=atom_mutator.content_sha256(a1.read_bytes()),
                actor="camer",
                brain_dir=str(brain),
                atoms_dir=str(brain / "2-atoms"),
                decision_log_path=str(brain / "0-raw" / "archive" / "decision-record.jsonl"),
            )
            rep_after = cognitive_distiller.verify_distilled_artifact_integrity(
                draft_pr["path"],
                kind="principle",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )
            self.assertFalse(rep_after.valid)
            self.assertEqual(rep_after.reason, "backing_atom_drifted")

    def test_2d3_knowledge_gravity_decay_and_reversible_cold_archive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb_2d3_") as td:
            brain = _seed_vault(Path(td))
            hub_p = _write_atom(
                brain,
                "hub-core.md",
                claim="SQLite WAL mode improves concurrent read latency.",
                links=["cold-old.md"],
                created="2026-10-01",
            )
            cold_p = _write_atom(
                brain,
                "cold-old.md",
                claim="Legacy XML RPC timeout constant is 45 seconds.",
                created="2024-01-01",
            )
            rebuild_lexical_index(atoms_dir=str(brain / "2-atoms"), index_path=str(brain / ".sidecar" / "lexical.sqlite3"))
            dbp = str(brain / ".sidecar" / "telemetry.sqlite3")
            cognitive_core.init_telemetry_db(dbp)
            cognitive_core.record_atom_feedback("hub-core.md", helpful=True, db_path=dbp, brain_dir=str(brain))
            cognitive_core.record_atom_feedback("hub-core.md", helpful=True, db_path=dbp, brain_dir=str(brain))

            grav_now = cognitive_core.compute_knowledge_gravity_report(
                atoms_dir=str(brain / "2-atoms"),
                brain_dir=str(brain),
                db_path=dbp,
                as_of="2026-11-23",
            )
            by_id = {x["atom_id"]: x for x in grav_now["items"]}
            self.assertGreater(by_id["hub-core.md"]["gravity"], by_id["cold-old.md"]["gravity"])
            cold_ids = {x["atom_id"] for x in grav_now["cold_archive_candidates"]}
            self.assertIn("cold-old.md", cold_ids)
            self.assertNotIn("hub-core.md", cold_ids)

            # Long future horizon flags high-utility hub as review_due
            grav_future = cognitive_core.compute_knowledge_gravity_report(
                atoms_dir=str(brain / "2-atoms"),
                brain_dir=str(brain),
                db_path=dbp,
                as_of="2029-01-01",
            )
            due_ids = {x["atom_id"] for x in grav_future["review_due_candidates"]}
            self.assertIn("hub-core.md", due_ids)

            # Reversible cold-archive + restore under Owner CAS
            cold_sha = atom_mutator.content_sha256(cold_p.read_bytes())
            arch = atom_mutator.archive_atom_to_cold(
                str(cold_p),
                expected_sha256=cold_sha,
                actor="camer",
                reason="Low gravity >180d",
                brain_dir=str(brain),
                atoms_dir=str(brain / "2-atoms"),
            )
            self.assertEqual(arch["status"], "cold_archived")
            self.assertFalse(cold_p.exists())
            self.assertTrue((brain / "2-atoms" / ".cold-archive" / "cold-old.md").is_file())
            hits_archived, _ = recall.search_memory(
                "Legacy XML RPC timeout",
                recall.RecallOptions(n_results=5, retrieval_mode="lexical"),
                atoms_dir=str(brain / "2-atoms"),
                index_path=str(brain / ".sidecar" / "lexical.sqlite3"),
            )
            self.assertEqual(hits_archived, [])

            rest = atom_mutator.restore_cold_archived_atom(
                "cold-old.md",
                expected_sha256=arch["sha256"],
                actor="camer",
                reason="Re-needed for legacy migration",
                brain_dir=str(brain),
                atoms_dir=str(brain / "2-atoms"),
            )
            self.assertEqual(rest["status"], "restored")
            self.assertTrue(rest["approved_current"])
            self.assertTrue(cold_p.is_file())
            hits_restored, _ = recall.search_memory(
                "Legacy XML RPC timeout",
                recall.RecallOptions(n_results=5, retrieval_mode="lexical"),
                atoms_dir=str(brain / "2-atoms"),
                index_path=str(brain / ".sidecar" / "lexical.sqlite3"),
            )
            self.assertEqual(len(hits_restored), 1)

    def test_2d4_living_concept_timelines_and_spreading_activation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb_2d4_") as td:
            brain = _seed_vault(Path(td))
            _write_atom(
                brain,
                "docker-v1.md",
                claim="Docker images should use slim Alpine bases.",
                project="project:infra",
                tags=["Docker"],
                created="2025-07-10",
                confidence=0.62,
            )
            _write_atom(
                brain,
                "docker-v2.md",
                claim="Docker builds use BuildKit mount caches.",
                project="project:infra",
                tags=["Docker", "BuildKit"],
                links=["buildkit-cache.md"],
                created="2026-10-12",
                confidence=0.92,
            )
            _write_atom(
                brain,
                "buildkit-cache.md",
                claim="BuildKit cache mounts speed up pip wheel installs.",
                project="project:infra",
                tags=["BuildKit", "Python"],
                created="2026-10-15",
                confidence=0.88,
            )
            cognitive_core.save_experience_record(
                goal="Optimize Docker build step",
                result_summary="Forgot cache mount id causing miss",
                success=False,
                context=["project:infra"],
                lessons=["Always pass explicit id= on BuildKit RUN --mount=type=cache with Docker."],
                experiences_dir=str(brain / "4-experiences"),
                brain_dir=str(brain),
            )

            docker_obj = concept_graph.get_concept(
                "Docker",
                project="project:infra",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )
            self.assertEqual(docker_obj["atom_count"], 2)
            periods = [pt["period"] for pt in docker_obj["confidence_timeline"]]
            self.assertEqual(periods, ["2025-Q3", "2026-Q4"])
            self.assertGreater(docker_obj["confidence_timeline"][1]["confidence"], docker_obj["confidence_timeline"][0]["confidence"])
            self.assertGreaterEqual(len(docker_obj["mistakes"]), 1)
            self.assertTrue(any(e["relation"] in {"part_of", "depends_on"} for e in docker_obj["edges"]))

            act = concept_graph.spreading_activation(
                ["Docker"],
                max_hops=3,
                project="project:infra",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )
            activated_ids = {c["concept_id"] for c in act["activated_concepts"]}
            self.assertIn("docker", activated_ids)
            self.assertIn("buildkit", activated_ids)
            self.assertIn("python", activated_ids)

    def test_2d5_meta_router_cognitive_swarm_and_debate_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb_2d5_") as td:
            brain = _seed_vault(Path(td))
            _write_atom(
                brain,
                "auth-tls.md",
                claim="Service-to-service traffic strictly enforces mutual TLS on port 8443.",
                project="project:alpha",
                tags=["auth", "tls"],
            )
            _write_atom(
                brain,
                "auth-bypass.md",
                claim="Legacy healthcheck endpoint bypasses mutual TLS on port 8443 for local probes.",
                project="project:alpha",
                tags=["auth", "tls"],
            )
            rebuild_lexical_index(atoms_dir=str(brain / "2-atoms"), index_path=str(brain / ".sidecar" / "lexical.sqlite3"))

            b_fast = meta_router.classify_query("SQLite WAL port", reasoning_profile="local_compact")
            b_quant = meta_router.classify_query("Solve $\\frac{12}{36} + x = 2$", reasoning_profile="cloud_hybrid")
            b_proof = meta_router.classify_query("Prove loop invariant termination theorem", reasoning_profile="local_gpu")
            self.assertEqual(b_fast.path, "fast_path")
            self.assertEqual(b_quant.reasoning_mode, "quantitative_derivation")
            self.assertEqual(b_quant.candidate_samples_k, 8)
            self.assertEqual(b_proof.reasoning_mode, "formal_proof")
            self.assertEqual(b_proof.path, "deliberative_path")

            routed = meta_router.route_and_retrieve(
                "Explain how service traffic handles mutual TLS telemetry encryption and also audit policy across clusters",
                project="project:alpha",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
                lexical_index_path=brain / ".sidecar" / "lexical.sqlite3",
                chroma_path=brain / ".chroma",
            )
            self.assertTrue(routed["mid_flight_requeried"])

            swarm_res = cognitive_swarm.run_cognitive_swarm(
                "Explain mutual TLS enforcement on port 8443",
                project="project:alpha",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
                lexical_index_path=brain / ".sidecar" / "lexical.sqlite3",
                chroma_path=brain / ".chroma",
            )
            self.assertFalse(swarm_res["abstained"])
            self.assertEqual(set(swarm_res["roles"].keys()), {"planner", "knowledge", "logic", "critic"})

            debate = cognitive_swarm.run_two_sided_debate(
                "mutual TLS port 8443",
                project="project:alpha",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )
            self.assertGreaterEqual(len(debate["thesis"]["claims"]), 1)
            self.assertGreaterEqual(len(debate["antithesis"]["claims"]), 1)
            actions = {a["action"] for a in debate["owner_adjudication_actions"]}
            self.assertEqual(actions, {"keep_existing", "supersede", "context_partition"})

            # Streaming chat mode="debate" yields cited Thesis/Antithesis event without mutating atoms
            sec_dir = Path(td) / "sec"
            sec_dir.mkdir()
            cp = streaming_chat.ChatPaths(
                brain_dir=str(brain),
                atoms_dir=str(brain / "2-atoms"),
                lexical_index_path=str(brain / ".sidecar" / "lexical.sqlite3"),
                chroma_path=str(brain / ".chroma"),
                telemetry_db_path=str(brain / ".sidecar" / "telemetry.sqlite3"),
                policy_path=sec_dir / "policy.json",
                security_dir=sec_dir,
            )
            evts = list(streaming_chat.chat_events("mutual TLS port 8443", mode="debate", paths=cp, project_scope="project:alpha"))
            self.assertTrue(any(e.get("type") == "debate_brief" for e in evts))

    def test_2d6_nightly_replay_world_model_simulator_and_self_model(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb_2d6_") as td:
            tpath = Path(td)
            brain = _seed_vault(tpath)
            _write_atom(
                brain,
                "sqlite-lock.md",
                claim="Use writer.lock around lexical rebuild operations.",
                project="project:core",
                context_domain="concurrency",
                tags=["writer-lock", "sqlite"],
            )
            _write_atom(
                brain,
                "recall-depend.md",
                claim="Recall Engine depends on lexical rebuild consistency.",
                project="project:core",
                context_domain="retrieval",
                tags=["Recall", "sqlite"],
                links=["sqlite-lock.md"],
            )
            cognitive_core.save_experience_record(
                goal="Rebuild sqlite index concurrently",
                result_summary="Database locked failure in Recall Engine",
                success=False,
                context="concurrency",
                lessons=["Always acquire writer.lock before rebuilding sqlite lexical index."],
                experiences_dir=str(brain / "4-experiences"),
                brain_dir=str(brain),
            )

            rep = episodic_replay.run_nightly_replay(
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
                date_str="2026-11-23",
            )
            self.assertEqual(rep["failures_analyzed"], 1)
            self.assertTrue(Path(rep["briefing_path"]).is_file())
            self.assertEqual(len(rep["staged_inbox_files"]), 1)
            self.assertTrue(Path(rep["staged_inbox_files"][0]).read_text(encoding="utf-8").startswith("<!-- second_brain_origin: cognitive_experience -->"))

            # Mini Python repo to verify AST reverse-dependency edge extraction
            mini_repo = tpath / "repo"
            mini_repo.mkdir()
            (mini_repo / "writer_lock.py").write_text("LOCK = 1\n", encoding="utf-8")
            (mini_repo / "indexer_job.py").write_text("import writer_lock\n", encoding="utf-8")

            sim = world_model_sim.simulate_change_impact(
                "writer-lock",
                repo_dir=mini_repo,
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )
            provs = {n["provenance"] for n in sim["impacted_nodes"]}
            self.assertIn("verified_vault_edge", provs)
            self.assertIn("code_ast_dependency", provs)
            self.assertTrue(
                sim["provenance_summary"]["verified_vault_edge"] >= 1
                and sim["provenance_summary"]["code_ast_dependency"] >= 1
            )

            # Self-Model & Directed Curiosity Engine
            self_model_curiosity.upsert_learning_goals(
                [{"id": "cuda-goal", "title": "Master CUDA Kernels", "domain": "cuda", "target_atoms": 8}],
                brain_dir=brain,
            )
            cur = self_model_curiosity.evaluate_self_model_and_curiosity(brain_dir=brain, atoms_dir=brain / "2-atoms")
            self.assertEqual(cur["curiosity_targets"][0]["domain"], "cuda")
            self.assertGreater(cur["curiosity_targets"][0]["priority_score"], 0.5)

    def test_2d7_owner_adr_gated_experimental_sandboxes_and_mcp_parity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb_2d7_") as td:
            tpath = Path(td)
            brain = _seed_vault(tpath)
            _write_atom(
                brain,
                "clean-fact.md",
                claim="Contact owner@example.com for key rotation escalation procedures.",
                project="project:sec",
                tags=["SecurityOps"],
            )
            rebuild_lexical_index(atoms_dir=str(brain / "2-atoms"), index_path=str(brain / ".sidecar" / "lexical.sqlite3"))

            # Fail closed by default when no owner ADR is enabled
            with self.assertRaises(adr_gate.ADRRequiredError):
                experimental_lora_pipeline.export_lora_training_pairs(brain_dir=brain, atoms_dir=brain / "2-atoms")
            with self.assertRaises(adr_gate.ADRRequiredError):
                experimental_crdt_notes.sync_crdt_note("scratch.md", local_state=[], remote_state=[], brain_dir=brain)

            # Enable LoRA experiment ADR -> PII scrubbed export + strict holdout gate
            adr_gate.set_adr_status(
                "ADR-LORA-EXPERIMENT",
                enabled=True,
                actor="camer",
                rationale="Benchmark small-N adapter safely in isolated scratch.",
                brain_dir=brain,
            )
            exp = experimental_lora_pipeline.export_lora_training_pairs(brain_dir=brain, atoms_dir=brain / "2-atoms")
            self.assertEqual(exp["pair_count"], 1)
            self.assertNotIn("owner@example.com", Path(exp["dataset_path"]).read_text(encoding="utf-8"))
            with self.assertRaises(experimental_lora_pipeline.LoraExperimentGateError):
                experimental_lora_pipeline.register_experimental_modelfile(
                    holdout_eval_report={"trap_leakage_rate": 0.1, "grounding_score": 0.9, "baseline_grounding_score": 0.8},
                    brain_dir=brain,
                )
            ok_mf = experimental_lora_pipeline.register_experimental_modelfile(
                holdout_eval_report={"trap_leakage_rate": 0.0, "grounding_score": 0.91, "baseline_grounding_score": 0.88},
                brain_dir=brain,
            )
            self.assertEqual(ok_mf["model_alias"], "second-brain-lora-exp")

            # Enable Notes-Only CRDT ADR -> canonical stores STILL rejected with PermissionError
            adr_gate.set_adr_status(
                "ADR-NOTES-CRDT",
                enabled=True,
                actor="camer",
                rationale="Allow scratchpad CRDT test under brain/notes/ only.",
                brain_dir=brain,
            )
            for forbidden in ("2-atoms/clean-fact.md", "../2-atoms/clean-fact.md", "7-principles/p.md", "5-playbooks/pb.md"):
                with self.assertRaises(PermissionError):
                    experimental_crdt_notes.sync_crdt_note(forbidden, local_state=[], remote_state=[], brain_dir=brain)

            st_a = [{"elem_id": "1", "hlc": 2, "replica": "a", "text": "Line A"}, {"elem_id": "2", "hlc": 1, "replica": "a", "text": "Line B"}]
            st_b = [{"elem_id": "1", "hlc": 5, "replica": "b", "text": "Line A updated"}, {"elem_id": "3", "hlc": 4, "replica": "b", "text": "Line C"}]
            res_ab = experimental_crdt_notes.sync_crdt_note("scratch.md", local_state=st_a, remote_state=st_b, brain_dir=brain)
            res_ba = experimental_crdt_notes.sync_crdt_note("scratch.md", local_state=st_b, remote_state=st_a, brain_dir=brain)
            self.assertEqual(res_ab["sha256"], res_ba["sha256"])

            # Verify MCP Gen-4 tools over authorized loopback handler
            from mcp import types

            token = "gen4-mcp-secret-token-0123456789"
            sec_dir = tpath / "sec"
            sec_dir.mkdir()
            audit_p = brain / "0-raw" / "archive" / "mcp-audit.jsonl"
            with (
                patch.object(mcp_server, "BRAIN_DIR", str(brain)),
                patch.object(mcp_server, "ATOMS_DIR", str(brain / "2-atoms")),
                patch.object(mcp_server, "RAW_DIR", str(brain / "0-raw")),
                patch.object(mcp_server, "INBOX_DIR", str(brain / "0-raw" / "inbox")),
                patch.object(mcp_server, "BRIEFINGS_DIR", str(brain / "briefings")),
                patch.object(mcp_server, "THREADS_DIR", str(brain / "3-threads")),
                patch.object(mcp_server, "MCP_AUDIT_PATH", str(audit_p)),
                patch.dict(os.environ, {"BRAIN_MCP_BEARER_TOKEN": token}, clear=False),
            ):
                ctx = SimpleNamespace(meta={"authorization": f"Bearer {token}"})
                listed = asyncio.run(mcp_server.handle_list_tools(ctx, None))
                self.assertEqual(len(listed.tools), 35)
                z_res = asyncio.run(
                    mcp_server.handle_call_tool(
                        ctx,
                        types.CallToolRequestParams(
                            name="zoom_memory",
                            arguments={"query": "rotation escalation", "project": "project:sec"},
                            _meta={"authorization": f"Bearer {token}"},
                        ),
                    )
                )
                self.assertFalse(z_res.is_error)
                c_res = asyncio.run(
                    mcp_server.handle_call_tool(
                        ctx,
                        types.CallToolRequestParams(
                            name="get_concept",
                            arguments={"concept": "SecurityOps", "project": "project:sec"},
                            _meta={"authorization": f"Bearer {token}"},
                        ),
                    )
                )
                self.assertFalse(c_res.is_error)
                s_res = asyncio.run(
                    mcp_server.handle_call_tool(
                        ctx,
                        types.CallToolRequestParams(
                            name="simulate_impact",
                            arguments={"target": "SecurityOps", "project": "project:sec"},
                            _meta={"authorization": f"Bearer {token}"},
                        ),
                    )
                )
                self.assertFalse(s_res.is_error)

    def test_reg1_swarm_and_debate_detect_real_contradictions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb_reg_contra_") as td:
            brain = _seed_vault(Path(td))
            _write_atom(
                brain,
                "cache-ttl-short.md",
                claim="Session token cache timeout is strictly 60 seconds in production.",
                project="project:alpha",
                tags=["cache", "session"],
            )
            _write_atom(
                brain,
                "cache-ttl-long.md",
                claim="Session token cache timeout is strictly 600 seconds in production.",
                project="project:alpha",
                tags=["cache", "session"],
            )
            rebuild_lexical_index(
                atoms_dir=str(brain / "2-atoms"),
                index_path=str(brain / ".sidecar" / "lexical.sqlite3"),
            )

            swarm = cognitive_swarm.run_cognitive_swarm(
                "What is the session token cache timeout in production?",
                project="project:alpha",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
                lexical_index_path=brain / ".sidecar" / "lexical.sqlite3",
                chroma_path=brain / ".chroma",
            )
            self.assertFalse(swarm["abstained"])
            self.assertEqual(swarm["roles"]["critic"]["verdict"], "caution_contradictions")
            self.assertGreaterEqual(len(swarm["roles"]["critic"]["contradiction_warnings"]), 1)

            debate = cognitive_swarm.run_two_sided_debate(
                "session token cache timeout production",
                project="project:alpha",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )
            self.assertGreaterEqual(len(debate["contradictions_detected"]), 1)
            thesis_fns = {c["filename"] for c in debate["thesis"]["claims"]}
            anti_fns = {c["filename"] for c in debate["antithesis"]["claims"]}
            self.assertEqual(thesis_fns | anti_fns, {"cache-ttl-short.md", "cache-ttl-long.md"})
            self.assertGreaterEqual(len(thesis_fns), 1)
            self.assertGreaterEqual(len(anti_fns), 1)

    def test_reg2_preference_expired_and_cross_project_isolation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb_reg_iso_") as td:
            tpath = Path(td)
            brain = _seed_vault(tpath)
            # Sibling prefix directory outside brain/ to test sibling-prefix traversal defense
            evil_dir = tpath / "brain_evil"
            evil_dir.mkdir(parents=True, exist_ok=True)
            (evil_dir / "secret.md").write_text("TOP_SECRET_SIBLING_LEAK\n", encoding="utf-8")

            # Clean alpha atom + second linked alpha atom
            alpha_clean = _write_atom(
                brain,
                "alpha-gateway.md",
                claim="Gateway service enforces TLS 1.3 cipher suite pinning.",
                project="project:alpha",
                context_domain="security",
                tags=["Gateway", "TLS"],
                created="2026-09-01",
            )
            # Preference atom in alpha (must be excluded everywhere!)
            _write_atom(
                brain,
                "alpha-pref.md",
                claim="Gateway service prefers concise YAML formatting for TLS configs.",
                project="project:alpha",
                context_domain="security",
                tags=["Gateway", "TLS"],
                epistemic_label="preference",
                epistemic_contract="owner_subjective_preference",
            )
            # Expired atom in alpha (must be excluded everywhere!)
            _write_atom(
                brain,
                "alpha-expired.md",
                claim="Gateway service uses legacy TLS 1.0 fallback.",
                project="project:alpha",
                context_domain="security",
                tags=["Gateway", "TLS"],
                valid_from="2019-01-01",
                valid_until="2020-12-31",
            )
            # Multi-project atom belonging to BOTH alpha and beta (must be excluded when scoped strictly to alpha!)
            _write_atom(
                brain,
                "shared-ab.md",
                claim="Gateway service shares billing secret across alpha and beta.",
                projects=["project:alpha", "project:beta"],
                context_domain="security",
                tags=["Gateway", "BillingSecret"],
            )
            # Pure beta atom touching the same concept "Gateway" with a beta-only edge & timeline point
            _write_atom(
                brain,
                "beta-only.md",
                claim="Gateway service routes BetaVault payments through beta-queue.",
                project="project:beta",
                context_domain="security",
                tags=["Gateway", "BetaVault"],
                created="2024-02-01",
                confidence=0.40,
            )
            # Beta-scoped experience and unscoped experience mentioning Gateway
            cognitive_core.save_experience_record(
                goal="Gateway beta outage",
                result_summary="Beta secret rotation broke Gateway",
                success=False,
                context=["project:beta"],
                lessons=["Rotate beta Gateway secrets safely."],
                experiences_dir=str(brain / "4-experiences"),
                brain_dir=str(brain),
            )
            cognitive_core.save_experience_record(
                goal="Gateway private personal note",
                result_summary="Unscoped failure on Gateway",
                success=False,
                context=[],
                lessons=["Unscoped private Gateway failure."],
                experiences_dir=str(brain / "4-experiences"),
                brain_dir=str(brain),
            )
            # Beta-scoped concept note with open question
            concepts_dir = brain / "concepts"
            concepts_dir.mkdir(parents=True, exist_ok=True)
            (concepts_dir / "gateway.md").write_text(
                "---\nproject: project:beta\n---\n\n## Open Questions\n- Should BetaVault replace Gateway?\n",
                encoding="utf-8",
            )
            rebuild_lexical_index(
                atoms_dir=str(brain / "2-atoms"),
                index_path=str(brain / ".sidecar" / "lexical.sqlite3"),
            )

            # 1) Fractal Zoom: strict alpha isolation + malformed offset safety + CAS revocation invalidation
            z_res = fractal_zoom.zoom_retrieve(
                "Gateway TLS cipher formatting legacy billing BetaVault",
                target_level="L5",
                project="project:alpha",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )
            z_fns = {n["atom_filename"] for n in z_res["nodes"]}
            self.assertEqual(z_fns, {"alpha-gateway.md"})
            self.assertIsNone(fractal_zoom._safe_quote_source_path(brain, "../brain_evil/secret.md"))
            self.assertIsNone(fractal_zoom._safe_quote_source_path(brain, "2-atoms/alpha-gateway.md"))

            # 2) Concept Graph: zero cross-project, preference, expired, or multi-project leakage + traversal_count preserved
            cg = concept_graph.get_concept("Gateway", project="project:alpha", brain_dir=brain, atoms_dir=brain / "2-atoms")
            self.assertEqual([a["filename"] for a in cg["atoms"]], ["alpha-gateway.md"])
            self.assertEqual(cg["projects"], ["project:alpha"])
            self.assertEqual(cg["mistakes"], [])
            self.assertEqual(cg["open_questions"], [])
            self.assertEqual([pt["period"] for pt in cg["confidence_timeline"]], ["2026-Q3"])
            for ed in cg["edges"]:
                self.assertNotIn("betavault", {ed["src_concept"], ed["dst_concept"]})
                self.assertNotIn("billingsecret", {ed["src_concept"], ed["dst_concept"]})
                self.assertNotIn("beta-only.md", ed["evidence_atoms"])
                self.assertNotIn("shared-ab.md", ed["evidence_atoms"])

            # Verify spreading activation increments traversal_count and forced rebuild preserves it
            concept_graph.spreading_activation(["Gateway"], max_hops=2, project="project:alpha", brain_dir=brain, atoms_dir=brain / "2-atoms")
            cg_before_rb = concept_graph.get_concept("Gateway", project="project:alpha", brain_dir=brain, atoms_dir=brain / "2-atoms")
            self.assertTrue(any(e["traversal_count"] >= 1 for e in cg_before_rb["edges"]))
            concept_graph.rebuild_concepts(brain_dir=brain, atoms_dir=brain / "2-atoms", force=True)
            cg_after_rb = concept_graph.get_concept("Gateway", project="project:alpha", brain_dir=brain, atoms_dir=brain / "2-atoms")
            self.assertTrue(any(e["traversal_count"] >= 1 for e in cg_after_rb["edges"]))

            # 3) World-Model Sim: excludes beta/unscoped experiences and beta/multi-project concepts
            sim = world_model_sim.simulate_change_impact(
                "Gateway",
                project="project:alpha",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )
            impacted_keys = {n["node_key"] for n in sim["impacted_nodes"]}
            self.assertNotIn("betavault", impacted_keys)
            self.assertNotIn("billingsecret", impacted_keys)
            self.assertFalse(any(n["provenance"] == "inferred_risk" for n in sim["impacted_nodes"]))

            # 4) Cognitive Swarm & Debate: excludes preference, expired, multi-project, and beta atoms/experiences
            sw = cognitive_swarm.run_cognitive_swarm(
                "Gateway TLS cipher formatting legacy billing BetaVault",
                project="project:alpha",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
                lexical_index_path=brain / ".sidecar" / "lexical.sqlite3",
                chroma_path=brain / ".chroma",
            )
            sw_fns = {c["filename"] for c in sw["roles"]["knowledge"]["atom_citations"]}
            self.assertEqual(sw_fns, {"alpha-gateway.md"})

            deb = cognitive_swarm.run_two_sided_debate(
                "Gateway TLS",
                project="project:alpha",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )
            deb_fns = {c["filename"] for c in deb["thesis"]["claims"]} | {c["filename"] for c in deb["antithesis"]["claims"]}
            self.assertEqual(deb_fns, {"alpha-gateway.md"})
            self.assertEqual(deb["antithesis"]["related_failures"], [])

            # 5) Cognitive Distiller rejects preference backing atoms & refuses active overwrite
            a2 = _write_atom(brain, "alpha-dist2.md", claim="Pin TLS cipher suites.", project="project:alpha")
            a3 = _write_atom(brain, "alpha-dist3.md", claim="Audit TLS certs weekly.", project="project:alpha")
            with self.assertRaises(cognitive_distiller.DistillationError):
                cognitive_distiller.propose_principle(
                    title="Pref Backed",
                    statement="Should fail due to preference backing atom.",
                    backing_atoms=["alpha-gateway.md", "alpha-dist2.md", "alpha-pref.md"],
                    brain_dir=brain,
                    atoms_dir=brain / "2-atoms",
                )
            pr = cognitive_distiller.propose_principle(
                title="Active Protected Principle",
                statement="All TLS endpoints enforce strict cipher pinning.",
                backing_atoms=["alpha-gateway.md", "alpha-dist2.md", "alpha-dist3.md"],
                filename="active-protected.md",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )
            cognitive_distiller.promote_distilled_artifact(
                pr["path"],
                expected_sha256=pr["sha256"],
                actor="camer",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )
            with self.assertRaises(cognitive_distiller.DistillationError):
                cognitive_distiller.propose_principle(
                    title="Active Protected Principle",
                    statement="Attempted silent downgrade overwrite of active principle.",
                    backing_atoms=["alpha-gateway.md", "alpha-dist2.md", "alpha-dist3.md"],
                    filename="active-protected.md",
                    brain_dir=brain,
                    atoms_dir=brain / "2-atoms",
                )

            # 6) CAS rejection of alpha-gateway.md invalidates fractal_zoom immediately via decision-record.jsonl
            atom_mutator.apply_review_transition(
                action="reject",
                atom_path=str(alpha_clean),
                expected_sha256=atom_mutator.content_sha256(alpha_clean.read_bytes()),
                actor="camer",
                brain_dir=str(brain),
                atoms_dir=str(brain / "2-atoms"),
                decision_log_path=str(brain / "0-raw" / "archive" / "decision-record.jsonl"),
            )
            z_after_revoke = fractal_zoom.zoom_retrieve(
                "Gateway TLS cipher",
                target_level="L5",
                project="project:alpha",
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )
            self.assertNotIn("alpha-gateway.md", {n["atom_filename"] for n in z_after_revoke["nodes"]})

            # Also verify malformed byte offsets in frontmatter do not crash sync_cognitive_hierarchy
            _write_atom(
                brain,
                "alpha-bad-offset.md",
                claim="Malformed offset atom survives fractal sync safely.",
                project="project:alpha",
                extra_fm={"source_byte_start": "not-an-int", "source_byte_end": "bad"},
            )
            sync_out = fractal_zoom.sync_cognitive_hierarchy(brain_dir=brain, atoms_dir=brain / "2-atoms")
            self.assertEqual(sync_out["status"], "rebuilt")

    def test_reg3_world_model_sim_single_rebuild_and_repo_confinement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb_reg_sim_") as td:
            tpath = Path(td)
            brain = _seed_vault(tpath)
            for idx in range(5):
                _write_atom(
                    brain,
                    f"svc-{idx}.md",
                    claim=f"Service-{idx} depends on CoreBus messaging protocol.",
                    project="project:core",
                    tags=[f"Service{idx}", "CoreBus"],
                )
            allowed_root = tpath / "allowed_repos"
            allowed_repo = allowed_root / "myrepo"
            allowed_repo.mkdir(parents=True, exist_ok=True)
            (allowed_repo / "core_bus.py").write_text("BUS = 1\n", encoding="utf-8")
            (allowed_repo / "worker.py").write_text("import core_bus\n", encoding="utf-8")

            sibling_evil = tpath / "allowed_repos_evil"
            sibling_evil.mkdir(parents=True, exist_ok=True)

            with patch.object(concept_graph, "rebuild_concepts", wraps=concept_graph.rebuild_concepts) as spy_rb:
                sim1 = world_model_sim.simulate_change_impact(
                    "CoreBus",
                    project="project:core",
                    repo_dir=allowed_repo,
                    allowed_repo_roots=(str(allowed_root),),
                    brain_dir=brain,
                    atoms_dir=brain / "2-atoms",
                )
                sim2 = world_model_sim.simulate_change_impact(
                    "CoreBus",
                    project="project:core",
                    repo_dir=allowed_repo,
                    allowed_repo_roots=(str(allowed_root),),
                    brain_dir=brain,
                    atoms_dir=brain / "2-atoms",
                )
                self.assertEqual(spy_rb.call_count, 1)
                self.assertGreaterEqual(sim1["impacted_count"], 1)
                self.assertEqual(sim1["impacted_count"], sim2["impacted_count"])

            with self.assertRaises(PermissionError):
                world_model_sim.simulate_change_impact(
                    "CoreBus",
                    repo_dir=allowed_repo,
                    allow_repo_scan=False,
                    brain_dir=brain,
                    atoms_dir=brain / "2-atoms",
                )
            with self.assertRaises(PermissionError):
                world_model_sim.simulate_change_impact(
                    "CoreBus",
                    repo_dir=sibling_evil,
                    allowed_repo_roots=(str(allowed_root),),
                    brain_dir=brain,
                    atoms_dir=brain / "2-atoms",
                )
            with self.assertRaises(PermissionError):
                world_model_sim.simulate_change_impact(
                    "CoreBus",
                    repo_dir=str(allowed_repo / ".." / ".." / "escape"),
                    allowed_repo_roots=(str(allowed_root),),
                    brain_dir=brain,
                    atoms_dir=brain / "2-atoms",
                )

    def test_reg4_episodic_replay_singular_context_and_date_traversal_block(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb_reg_replay_") as td:
            brain = _seed_vault(Path(td))
            cognitive_core.save_experience_record(
                goal="Run concurrent SQLite rebuild",
                result_summary="Database locked without writer lock",
                success=False,
                context=["concurrency", "project:core"],
                lessons=["Acquire writer.lock before SQLite rebuild."],
                experiences_dir=str(brain / "4-experiences"),
                brain_dir=str(brain),
            )
            rep = episodic_replay.run_nightly_replay(
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
                date_str="2026-11-24",
            )
            self.assertEqual(rep["failure_clusters"].get("concurrency"), 1)
            self.assertEqual(rep["failure_clusters"].get("project:core"), 1)
            staged = Path(rep["staged_inbox_files"][0]).read_text(encoding="utf-8")
            self.assertTrue(staged.startswith("<!-- second_brain_origin: cognitive_experience -->"))

            with self.assertRaises(ValueError):
                episodic_replay.run_nightly_replay(
                    brain_dir=brain,
                    atoms_dir=brain / "2-atoms",
                    date_str="../../pwn",
                )

    def test_reg5_feedback_default_db_cli_delegation_and_curiosity_word_boundary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb_reg_cli_") as td:
            brain = _seed_vault(Path(td))
            _write_atom(
                brain,
                "tls-core.md",
                claim="Mutual TLS is required on port 8443.",
                project="project:alpha",
                tags=["tls"],
            )
            rebuild_lexical_index(
                atoms_dir=str(brain / "2-atoms"),
                index_path=str(brain / ".sidecar" / "lexical.sqlite3"),
            )
            # Word-boundary curiosity check: "ai" must NOT match "failed" or "daily"
            self_model_curiosity.upsert_learning_goals(
                [{"id": "ai-goal", "title": "Study AI Systems", "domain": "ai", "target_atoms": 5}],
                brain_dir=brain,
            )
            cognitive_core.save_experience_record(
                goal="Daily backup failed on disk quota",
                result_summary="Daily cron job failed unexpectedly",
                success=False,
                context=["storage"],
                lessons=["Check daily disk quota before backup."],
                experiences_dir=str(brain / "4-experiences"),
                brain_dir=str(brain),
            )
            cur = self_model_curiosity.evaluate_self_model_and_curiosity(
                brain_dir=brain,
                atoms_dir=brain / "2-atoms",
            )
            ai_prof = next(d for d in cur["domains"] if d["domain"] == "ai")
            self.assertEqual(ai_prof["failure_count"], 0)

            # record_atom_feedback with db_path=None + CLI delegation subcommands
            with patch.dict(os.environ, {"BRAIN_VAULT_DIR": str(brain)}, clear=False):
                fb = cognitive_core.record_atom_feedback("tls-core.md", helpful=True, db_path=None, brain_dir=str(brain))
                self.assertEqual(fb["feedback_kind"], "helpful")
                self.assertTrue((brain / ".sidecar" / "telemetry.sqlite3").is_file())

                buf = io.StringIO()
                with redirect_stdout(buf):
                    self.assertEqual(cognitive_core.main(["curiosity", "--json"]), 0)
                    self.assertEqual(cognitive_core.main(["replay", "--no-inbox", "--json"]), 0)
                    self.assertEqual(cognitive_core.main(["swarm", "mutual TLS", "--json"]), 0)
                    self.assertEqual(cognitive_core.main(["bus", "status", "--json"]), 0)
                    self.assertEqual(cognitive_core.main(["route", "Solve 2 + 2", "--json"]), 0)

    def test_reg6_predictive_bus_writeback_multivault_and_recall_cognitive_boosts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sb_reg_bus_") as td:
            tpath = Path(td)
            brain_a = _seed_vault(tpath / "vault_a")
            brain_b = _seed_vault(tpath / "vault_b")

            _write_atom(
                brain_a,
                "alpha-hub.md",
                claim="Quantum ledger checkpointing verifies shard digests deterministically.",
                project="project:alpha",
                tags=["QuantumLedger", "Checkpointing"],
                links=["alpha-dep.md"],
                created="2026-11-20",
            )
            _write_atom(
                brain_a,
                "alpha-dep.md",
                claim="Quantum ledger checkpointing runs offline without remote calls.",
                project="project:alpha",
                tags=["Checkpointing", "Offline"],
                created="2024-01-01",
            )
            _write_atom(
                brain_b,
                "beta-item.md",
                claim="Vault B stores independent robotics calibration tables.",
                project="project:beta",
                tags=["Robotics"],
            )
            rebuild_lexical_index(
                atoms_dir=str(brain_a / "2-atoms"),
                index_path=str(brain_a / ".sidecar" / "lexical.sqlite3"),
            )
            rebuild_lexical_index(
                atoms_dir=str(brain_b / "2-atoms"),
                index_path=str(brain_b / ".sidecar" / "lexical.sqlite3"),
            )

            # 1) Empty L0 cache fast-path returns None with ZERO disk I/O
            predictive_memory_bus.invalidate_bus_cache()
            with patch.object(predictive_memory_bus, "compute_vault_freshness_key") as spy_fk:
                self.assertIsNone(
                    predictive_memory_bus.lookup_l0_hot(
                        "Quantum ledger checkpointing",
                        project_scope="project:alpha",
                        brain_dir=brain_a,
                        atoms_dir=brain_a / "2-atoms",
                    )
                )
                self.assertEqual(spy_fk.call_count, 0)

            # 2) recall.search_memory write-back on miss -> L0 hit on repeat -> Vault B does NOT evict Vault A
            opts_bus = recall.RecallOptions(
                n_results=5,
                retrieval_mode="lexical",
                projects=("project:alpha",),
                use_l0_bus=True,
            )
            first_d, _ = recall.search_memory(
                "Quantum ledger checkpointing",
                opts_bus,
                atoms_dir=str(brain_a / "2-atoms"),
                index_path=str(brain_a / ".sidecar" / "lexical.sqlite3"),
            )
            self.assertGreaterEqual(len(first_d), 1)
            self.assertNotIn("l0_bus_hit", first_d[0].match_reasons)

            # Store/query Vault B into L0 bus
            recall.search_memory(
                "robotics calibration tables",
                recall.RecallOptions(
                    n_results=5,
                    retrieval_mode="lexical",
                    projects=("project:beta",),
                    use_l0_bus=True,
                ),
                atoms_dir=str(brain_b / "2-atoms"),
                index_path=str(brain_b / ".sidecar" / "lexical.sqlite3"),
            )

            # Vault A's L0 entry MUST still hit cleanly!
            second_d, _ = recall.search_memory(
                "Quantum ledger checkpointing",
                opts_bus,
                atoms_dir=str(brain_a / "2-atoms"),
                index_path=str(brain_a / ".sidecar" / "lexical.sqlite3"),
            )
            self.assertGreaterEqual(len(second_d), 1)
            self.assertIn("l0_bus_hit", second_d[0].match_reasons)
            self.assertIn("+l0_bus", second_d[0].source)

            # Signal prewarm overlap (>=2 tokens) hits L0 even when query differs from synthetic predicted_query
            predictive_memory_bus.invalidate_bus_cache()
            predictive_memory_bus.record_context_signal(
                active_file="src/quantum_ledger.py",
                git_branch="feat/checkpointing-verify",
                project_scope="project:alpha",
                cursor_topic="Quantum ledger checkpointing",
                brain_dir=brain_a,
                atoms_dir=brain_a / "2-atoms",
                lexical_index_path=brain_a / ".sidecar" / "lexical.sqlite3",
            )
            overlap_hit = predictive_memory_bus.lookup_l0_hot(
                "How does quantum checkpointing behave?",
                project_scope="project:alpha",
                brain_dir=brain_a,
                atoms_dir=brain_a / "2-atoms",
            )
            self.assertIsNotNone(overlap_hit)
            assert overlap_hit is not None
            self.assertTrue(overlap_hit["hit"])
            self.assertEqual(overlap_hit["match_kind"], "prewarm_overlap")

            # 3) enable_gravity_boost & enable_spreading_activation in recall.py
            db_a = str(brain_a / ".sidecar" / "telemetry.sqlite3")
            cognitive_core.init_telemetry_db(db_a)
            for _ in range(4):
                cognitive_core.record_grounded_answer_telemetry(
                    ["alpha-hub.md"], abstained=False, db_path=db_a
                )
            cognitive_core.record_atom_feedback("alpha-hub.md", helpful=True, db_path=db_a, brain_dir=str(brain_a))

            grav_d, _ = recall.search_memory(
                "Quantum ledger checkpointing",
                recall.RecallOptions(
                    n_results=5,
                    retrieval_mode="lexical",
                    projects=("project:alpha",),
                    enable_gravity_boost=True,
                    telemetry_db_path=db_a,
                ),
                atoms_dir=str(brain_a / "2-atoms"),
                index_path=str(brain_a / ".sidecar" / "lexical.sqlite3"),
            )
            self.assertEqual(grav_d[0].filename, "alpha-hub.md")
            self.assertTrue(any(r.startswith("gravity:boost(") for r in grav_d[0].match_reasons))

            spread_d, _ = recall.search_memory(
                "Quantum ledger checkpointing",
                recall.RecallOptions(
                    n_results=5,
                    retrieval_mode="lexical",
                    projects=("project:alpha",),
                    enable_spreading_activation=True,
                ),
                atoms_dir=str(brain_a / "2-atoms"),
                index_path=str(brain_a / ".sidecar" / "lexical.sqlite3"),
            )
            self.assertGreaterEqual(len(spread_d), 1)
            self.assertTrue(any(r.startswith("spreading_activation:") for item in spread_d for r in item.match_reasons))


if __name__ == "__main__":
    unittest.main()
