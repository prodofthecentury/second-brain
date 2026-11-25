# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""End-to-End HTTP, MemoryApiClient SDK, Deliberative Chat & CLI Verification Suite (Phases 8-10)."""

from __future__ import annotations

import base64
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _seed_vault(root: Path) -> Path:
    brain = root / "brain"
    for sub in (
        "0-raw",
        "0-raw/inbox",
        "0-raw/archive",
        "0-raw/note-snapshots",
        "sources",
        "2-atoms",
        "3-threads",
        "4-experiences",
        "5-playbooks",
        "6-skills",
        "7-principles",
        "8-mental-models",
        "notes",
        "briefings",
        ".sidecar",
    ):
        (brain / sub).mkdir(parents=True, exist_ok=True)
    return brain


def _write_approved_atom(
    brain: Path,
    filename: str,
    *,
    claim: str,
    project: str = "project:alpha",
    extra_tags: tuple[str, ...] = ("reliability",),
    links: tuple[str, ...] = (),
) -> str:
    from atom_mutator import apply_review_transition, content_sha256
    from evidence import find_evidence_anchor
    from memory_format import render_memory_document

    src_rel = f"0-raw/src-{filename}"
    (brain / "0-raw" / f"src-{filename}").write_text(f"# Source\n{claim}\n", encoding="utf-8")
    link_lines = "\n".join(f"- [[{lk}]]" for lk in links)
    body = f"## Claim\n{claim}\n\n## Evidence\n> {claim}\n\n## Links\n{link_lines}\n"
    tags = [project, *extra_tags]
    fm: dict[str, object] = {
        "id": filename[:-3],
        "type": "atom",
        "memory_type": "semantic",
        "title": filename[:-3],
        "review_status": "pending",
        "claim_state": "unreviewed",
        "citation_integrity": "anchored",
        "certainty": "certain",
        "created": "2026-11-23",
        "last-reviewed": "2026-11-23",
        "importance": 0.85,
        "confidence": 0.92,
        "sources": [src_rel],
        "tags": tags,
        "contexts": ["architecture", project],
    }

    def _render_md(meta: dict[str, object], text_body: str) -> str:
        fm_lines = [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items()]
        return render_memory_document(fm_lines, text_body.splitlines())

    doc = _render_md(fm, body)
    fm.update(find_evidence_anchor(doc, str(brain)))
    final_doc = _render_md(fm, body)
    raw_bytes = final_doc.encode("utf-8")
    atom_file = brain / "2-atoms" / filename
    atom_file.write_bytes(raw_bytes)
    apply_review_transition(
        action="approve",
        atom_path=str(atom_file),
        expected_sha256=content_sha256(raw_bytes),
        actor="camer",
        brain_dir=str(brain),
        atoms_dir=str(brain / "2-atoms"),
    )
    return content_sha256(atom_file.read_bytes())


def _sample_python_spec() -> tuple[str, str, str]:
    baseline = (
        "def compute_square(n: int) -> int:\n"
        "    # EVOLVE-BLOCK-START\n"
        "    return n + 1\n"
        "    # EVOLVE-BLOCK-END\n"
    )
    good_candidate = (
        "def compute_square(n: int) -> int:\n"
        "    # EVOLVE-BLOCK-START\n"
        "    return n * n\n"
        "    # EVOLVE-BLOCK-END\n"
    )
    tampered_candidate = (
        "def compute_square(n: int, extra=0) -> int:\n"
        "    # EVOLVE-BLOCK-START\n"
        "    return n * n\n"
        "    # EVOLVE-BLOCK-END\n"
    )
    return baseline, good_candidate, tampered_candidate


class QuantumLeapApiSdkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sb_qleap_api_")
        self.tpath = Path(self._tmp.name)
        self.brain = _seed_vault(self.tpath)
        self.sec_dir = self.tpath / "sec"
        self.sec_dir.mkdir(parents=True, exist_ok=True)

        # Seed 3+ approved atoms in project:alpha with causal & reliability tags
        self.sha_auth = _write_approved_atom(
            self.brain,
            "auth-service.md",
            claim="The auth_service signs JWT tokens using RS256 and rotates signing keys every 30 days.",
            project="project:alpha",
            extra_tags=("reliability", "auth_service"),
        )
        self.sha_gw = _write_approved_atom(
            self.brain,
            "api-gateway.md",
            claim="The api_gateway depends on auth_service token verification before forwarding requests.",
            project="project:alpha",
            extra_tags=("reliability", "api_gateway"),
            links=("auth-service.md",),
        )
        self.sha_retry = _write_approved_atom(
            self.brain,
            "retry-budget.md",
            claim="Retry policies enforce bounded exponential backoff with jitter to preserve reliability.",
            project="project:alpha",
            extra_tags=("reliability", "resilience"),
        )
        self.sha_cold = _write_approved_atom(
            self.brain,
            "legacy-cron.md",
            claim="Legacy nightly cron jobs were retired after migrating to event-driven workers.",
            project="project:alpha",
            extra_tags=("ops",),
        )

        # Seed an episodic failure record referencing auth_service for replay & impact simulation
        from cognitive_core import save_experience_record

        save_experience_record(
            goal="Rotate auth_service signing key in staging",
            result_summary="Key rotation failed because api_gateway cached old JWKS for 24 hours.",
            success=False,
            context=["architecture", "project:alpha"],
            lessons=["Invalidate api_gateway JWKS cache before rotating auth_service keys."],
            experiences_dir=str(self.brain / "4-experiences"),
            brain_dir=str(self.brain),
        )

        # Seed a draft mental model for mental-model gating & promotion tests
        from cognitive_distiller import propose_mental_model

        self.mm_draft = propose_mental_model(
            title="Auth Gateway Coupling",
            summary="Changes to auth_service signing semantics directly impact api_gateway verification.",
            edges=[
                {
                    "from": "auth_service",
                    "relation": "enables",
                    "to": "api_gateway",
                    "evidence_atom": "api-gateway.md",
                }
            ],
            backing_atoms=["auth-service.md", "api-gateway.md"],
            brain_dir=self.brain,
            atoms_dir=self.brain / "2-atoms",
        )

        # Rebuild lexical index so search_memory / swarm / zoom work offline
        from lexical_store import rebuild_lexical_index

        rebuild_lexical_index(
            atoms_dir=str(self.brain / "2-atoms"),
            index_path=str(self.brain / ".sidecar" / "lexical.sqlite3"),
        )

        # Start real MemoryLoopbackHTTPServer on an ephemeral loopback port
        from api_product import ensure_owner_token, reset_owner_rate_limiter
        from memory_api import (
            MemoryApiClient,
            MemoryApiRuntimeConfig,
            MemoryLoopbackHTTPServer,
            generate_memory_api_token,
        )

        reset_owner_rate_limiter()
        tok_p = self.sec_dir / "api-token"
        own_p = self.sec_dir / "owner-token"
        self.agent_tok, _ = generate_memory_api_token(tok_p)
        self.owner_tok = ensure_owner_token(own_p)

        self.rt = MemoryApiRuntimeConfig(
            brain_dir=str(self.brain),
            token_path=tok_p,
            owner_token_path=own_p,
            policy_path=self.sec_dir / "policy.json",
            security_dir=self.sec_dir,
        )
        self.srv = MemoryLoopbackHTTPServer("127.0.0.1", 0, self.rt)
        self.port = int(self.srv.server_address[1])
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self._thread.start()

        self.agent_headers = {"Authorization": f"Bearer {self.agent_tok}"}
        self.owner_headers = {
            "Authorization": f"Bearer {self.agent_tok}",
            "X-Owner-Token": self.owner_tok,
        }
        self.owner_client = MemoryApiClient(
            host="127.0.0.1",
            port=self.port,
            token=self.agent_tok,
            owner_token=self.owner_tok,
            timeout=20.0,
        )
        self.agent_client = MemoryApiClient(
            host="127.0.0.1",
            port=self.port,
            token=self.agent_tok,
            timeout=20.0,
        )

    def tearDown(self) -> None:
        try:
            self.srv.shutdown()
            self.srv.server_close()
        finally:
            self._tmp.cleanup()

    def test_01_all_phase9_10_routes_end_to_end_and_security_gates(self) -> None:
        baseline, good_cand, tampered_cand = _sample_python_spec()
        mini_repo = self.tpath / "mini_repo"
        mini_repo.mkdir()
        (mini_repo / "gateway.py").write_text("import auth_service\n", encoding="utf-8")
        (mini_repo / "auth_service.py").write_text("def verify(): return True\n", encoding="utf-8")

        with httpx.Client(trust_env=False, timeout=20.0) as client:
            # 1. Context signal & Bus status (owner-only)
            r = client.post(
                f"{self.base}/v1/cognitive/context-signal",
                headers=self.owner_headers,
                json={
                    "active_file": "scripts/auth_service.py",
                    "git_branch": "main",
                    "project_scope": "project:alpha",
                    "open_symbols": ["auth_service", "verify"],
                    "cursor_topic": "RS256 token verification",
                    "prewarm": True,
                },
            )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("freshness_key", r.json())

            r_bus = client.get(f"{self.base}/v1/cognitive/bus-status", headers=self.owner_headers)
            self.assertEqual(r_bus.status_code, 200, r_bus.text)
            self.assertIn("l0_vault_entries", r_bus.json())

            # 2. Fractal zoom (agent read) + traversal project tag -> HTTP 400
            r_zoom = client.post(
                f"{self.base}/v1/recall/zoom",
                headers=self.agent_headers,
                json={"query": "auth_service RS256", "target_level": "L4", "project": "project:alpha", "limit": 5},
            )
            self.assertEqual(r_zoom.status_code, 200, r_zoom.text)
            self.assertEqual(r_zoom.json()["target_level"], "L4")

            r_bad_proj = client.post(
                f"{self.base}/v1/recall/zoom",
                headers=self.agent_headers,
                json={"query": "auth", "project": "project:../etc/passwd"},
            )
            self.assertEqual(r_bad_proj.status_code, 400, r_bad_proj.text)

            # 3. Review-due gravity report (owner-only)
            r_rev = client.get(f"{self.base}/v1/cognitive/review-due", headers=self.owner_headers)
            self.assertEqual(r_rev.status_code, 200, r_rev.text)
            self.assertIn("items", r_rev.json())

            # 4. Cold archive & restore (verifying route is NOT shadowed by /v1/atoms/<fn>/<action>!)
            r_arch = client.post(
                f"{self.base}/v1/atoms/legacy-cron.md/cold-archive",
                headers=self.owner_headers,
                json={"expected_sha256": self.sha_cold, "actor": "camer", "reason": "low gravity"},
            )
            self.assertEqual(r_arch.status_code, 200, r_arch.text)
            self.assertEqual(r_arch.json()["status"], "cold_archived")
            cold_sha = r_arch.json()["sha256"]

            r_rest = client.post(
                f"{self.base}/v1/atoms/cold-archive/legacy-cron.md/restore",
                headers=self.owner_headers,
                json={"expected_sha256": cold_sha, "actor": "camer", "reason": "needed again"},
            )
            self.assertEqual(r_rest.status_code, 200, r_rest.text)
            self.assertEqual(r_rest.json()["status"], "restored")

            # 5. Principles distill -> non-owner active_only=false forced to active_only=True & strips path!
            r_dist = client.post(f"{self.base}/v1/principles/distill", headers=self.owner_headers, json={})
            self.assertEqual(r_dist.status_code, 201, r_dist.text)
            created_p = r_dist.json()["principles_created"]
            self.assertGreaterEqual(len(created_p), 1)
            p_fname = created_p[0]["filename"]
            p_sha = created_p[0]["sha256"]

            # Non-owner requesting active_only=false sees NO drafts
            r_p_agent_before = client.get(
                f"{self.base}/v1/principles?active_only=false",
                headers=self.agent_headers,
            )
            self.assertEqual(r_p_agent_before.status_code, 200, r_p_agent_before.text)
            self.assertEqual(r_p_agent_before.json()["principles"], [])

            # Owner requesting active_only=false sees the draft WITH path
            r_p_owner_before = client.get(
                f"{self.base}/v1/principles?active_only=false",
                headers=self.owner_headers,
            )
            self.assertEqual(r_p_owner_before.status_code, 200, r_p_owner_before.text)
            self.assertEqual(len(r_p_owner_before.json()["principles"]), 1)
            self.assertIn("path", r_p_owner_before.json()["principles"][0])

            # Promote principle via Owner CAS
            r_p_prom = client.post(
                f"{self.base}/v1/principles/{p_fname}/promote",
                headers=self.owner_headers,
                json={"expected_sha256": p_sha, "actor": "camer", "note": "verified reliability principle"},
            )
            self.assertEqual(r_p_prom.status_code, 200, r_p_prom.text)
            self.assertEqual(r_p_prom.json()["status"], "active")

            # Now non-owner sees active principle WITHOUT raw local filesystem 'path'
            r_p_agent_after = client.get(
                f"{self.base}/v1/principles?active_only=false",
                headers=self.agent_headers,
            )
            self.assertEqual(r_p_agent_after.status_code, 200, r_p_agent_after.text)
            self.assertEqual(len(r_p_agent_after.json()["principles"]), 1)
            self.assertNotIn("path", r_p_agent_after.json()["principles"][0])

            # 6. Mental models: non-owner hides drafts and strips path after promotion
            r_mm_agent_before = client.get(
                f"{self.base}/v1/mental-models?active_only=false",
                headers=self.agent_headers,
            )
            self.assertEqual(r_mm_agent_before.status_code, 200, r_mm_agent_before.text)
            self.assertEqual(r_mm_agent_before.json()["mental_models"], [])

            r_mm_prom = client.post(
                f"{self.base}/v1/mental-models/{self.mm_draft['filename']}/promote",
                headers=self.owner_headers,
                json={"expected_sha256": self.mm_draft["sha256"], "actor": "camer", "note": "verified model"},
            )
            self.assertEqual(r_mm_prom.status_code, 200, r_mm_prom.text)
            self.assertEqual(r_mm_prom.json()["status"], "active")

            r_mm_agent_after = client.get(
                f"{self.base}/v1/mental-models?active_only=false",
                headers=self.agent_headers,
            )
            self.assertEqual(r_mm_agent_after.status_code, 200, r_mm_agent_after.text)
            self.assertEqual(len(r_mm_agent_after.json()["mental_models"]), 1)
            self.assertNotIn("path", r_mm_agent_after.json()["mental_models"][0])

            # 7. Living concepts list & get (agent read)
            r_c_list = client.get(f"{self.base}/v1/concepts?project=project:alpha", headers=self.agent_headers)
            self.assertEqual(r_c_list.status_code, 200, r_c_list.text)
            concepts = r_c_list.json()["concepts"]
            self.assertGreaterEqual(len(concepts), 1)
            cid = concepts[0]["concept_id"]
            r_c_get = client.get(f"{self.base}/v1/concepts/{cid}?project=project:alpha", headers=self.agent_headers)
            self.assertEqual(r_c_get.status_code, 200, r_c_get.text)
            self.assertEqual(r_c_get.json()["concept_id"], cid)

            # 8. Debate & Swarm (agent read)
            r_deb = client.post(
                f"{self.base}/v1/cognitive/debate",
                headers=self.agent_headers,
                json={"topic": "auth_service JWT rotation", "project": "project:alpha"},
            )
            self.assertEqual(r_deb.status_code, 200, r_deb.text)
            self.assertIn("thesis", r_deb.json())

            r_sw = client.post(
                f"{self.base}/v1/cognitive/swarm",
                headers=self.agent_headers,
                json={"goal": "How does api_gateway depend on auth_service?", "project": "project:alpha"},
            )
            self.assertEqual(r_sw.status_code, 200, r_sw.text)
            self.assertIn("roles", r_sw.json())

            # 9. World-Model Simulate: non-owner repo_dir -> 403; non-owner without repo_dir -> 200; owner with repo_dir -> 200
            r_sim_denied = client.post(
                f"{self.base}/v1/cognitive/simulate",
                headers=self.agent_headers,
                json={"target": "auth_service", "repo_dir": str(mini_repo)},
            )
            self.assertEqual(r_sim_denied.status_code, 403, r_sim_denied.text)

            r_sim_agent = client.post(
                f"{self.base}/v1/cognitive/simulate",
                headers=self.agent_headers,
                json={"target": "auth_service", "project": "project:alpha"},
            )
            self.assertEqual(r_sim_agent.status_code, 200, r_sim_agent.text)
            self.assertEqual(r_sim_agent.json()["provenance_summary"]["code_ast_dependency"], 0)

            r_sim_owner = client.post(
                f"{self.base}/v1/cognitive/simulate",
                headers=self.owner_headers,
                json={"target": "auth_service", "repo_dir": str(mini_repo), "project": "project:alpha"},
            )
            self.assertEqual(r_sim_owner.status_code, 200, r_sim_owner.text)
            self.assertGreaterEqual(r_sim_owner.json()["provenance_summary"]["code_ast_dependency"], 1)

            # 10. Self-Model, Curiosity, ADR status/enable
            r_sm_post = client.post(
                f"{self.base}/v1/cognitive/self-model",
                headers=self.owner_headers,
                json={"goals": [{"id": "g-sec", "title": "Master Security", "domain": "architecture", "target_atoms": 5}]},
            )
            self.assertEqual(r_sm_post.status_code, 200, r_sm_post.text)
            r_sm_get = client.get(f"{self.base}/v1/cognitive/self-model", headers=self.owner_headers)
            self.assertEqual(r_sm_get.status_code, 200, r_sm_get.text)
            r_cur = client.get(f"{self.base}/v1/cognitive/curiosity", headers=self.owner_headers)
            self.assertEqual(r_cur.status_code, 200, r_cur.text)
            self.assertIn("curiosity_targets", r_cur.json())

            r_adr_s = client.get(f"{self.base}/v1/cognitive/adr-status", headers=self.owner_headers)
            self.assertEqual(r_adr_s.status_code, 200, r_adr_s.text)
            r_adr_e = client.post(
                f"{self.base}/v1/cognitive/adr-enable",
                headers=self.owner_headers,
                json={"adr_id": "ADR-NOTES-CRDT", "actor": "camer", "rationale": "testing opt-in ADR"},
            )
            self.assertEqual(r_adr_e.status_code, 200, r_adr_e.text)
            self.assertEqual(r_adr_e.json()["status"], "enabled")

            # 11. NEW Gen-4 Routes: /v1/cognitive/replay (owner-only) & /v1/cognitive/route (agent read)
            r_rep_denied = client.post(f"{self.base}/v1/cognitive/replay", headers=self.agent_headers, json={})
            self.assertEqual(r_rep_denied.status_code, 403, r_rep_denied.text)

            r_rep_ok = client.post(
                f"{self.base}/v1/cognitive/replay",
                headers=self.owner_headers,
                json={"date_str": "2026-11-24", "write_inbox": False},
            )
            self.assertEqual(r_rep_ok.status_code, 200, r_rep_ok.text)
            self.assertEqual(r_rep_ok.json()["status"], "completed")
            self.assertGreaterEqual(r_rep_ok.json()["failures_analyzed"], 1)

            r_route = client.post(
                f"{self.base}/v1/cognitive/route",
                headers=self.agent_headers,
                json={"query": "If I change auth_service what breaks downstream?"},
            )
            self.assertEqual(r_route.status_code, 200, r_route.text)
            self.assertEqual(r_route.json()["mode"], "causal_simulation")
            self.assertIn(r_route.json()["profile"], {"local_compact", "local_gpu", "cloud_hybrid"})

            # 12. Math solve & rubric (agent read)
            r_math = client.post(
                f"{self.base}/v1/math/solve",
                headers=self.agent_headers,
                json={"problem": "Compute 17 * 23 + 9", "samples": 2, "use_local_llm": False},
            )
            self.assertEqual(r_math.status_code, 200, r_math.text)
            self.assertEqual(r_math.json()["majority_answer"], "400")

            r_rub = client.post(
                f"{self.base}/v1/math/rubric",
                headers=self.agent_headers,
                json={
                    "steps": ["Multiply 17 by 23 to get 391.", "Add 9 to get 400 and verify base case."],
                    "final_answer": "400",
                    "expected_answer": "400",
                },
            )
            self.assertEqual(r_rub.status_code, 200, r_rub.text)
            self.assertGreater(r_rub.json()["weighted_total"], 0.8)

            # 13. Proof status (redacts paths for non-owner), verify (owner-only), search (owner-only, Ralph + Agent D + single)
            r_ps_agent = client.get(f"{self.base}/v1/proof/status", headers=self.agent_headers)
            self.assertEqual(r_ps_agent.status_code, 200, r_ps_agent.text)
            ps_agent_data = r_ps_agent.json()
            self.assertNotIn("lean_path", ps_agent_data)
            self.assertNotIn("subgoal_cache_path", ps_agent_data)
            self.assertIn("lean_installed", ps_agent_data)
            self.assertIn("subgoal_cache_ready", ps_agent_data)

            r_ps_owner = client.get(f"{self.base}/v1/proof/status", headers=self.owner_headers)
            self.assertEqual(r_ps_owner.status_code, 200, r_ps_owner.text)
            self.assertIn("lean_path", r_ps_owner.json())
            self.assertIn("subgoal_cache_path", r_ps_owner.json())

            r_pv_denied = client.post(
                f"{self.base}/v1/proof/verify",
                headers=self.agent_headers,
                json={"baseline_source": baseline, "candidate_source": good_cand},
            )
            self.assertEqual(r_pv_denied.status_code, 403, r_pv_denied.text)

            r_pv_ok = client.post(
                f"{self.base}/v1/proof/verify",
                headers=self.owner_headers,
                json={
                    "baseline_source": baseline,
                    "candidate_source": good_cand,
                    "test_lemmas": ["assert compute_square(5) == 25"],
                },
            )
            self.assertEqual(r_pv_ok.status_code, 200, r_pv_ok.text)
            self.assertTrue(r_pv_ok.json()["valid"])
            self.assertTrue(r_pv_ok.json()["test_lemmas"]["passed"])

            r_pv_tamper = client.post(
                f"{self.base}/v1/proof/verify",
                headers=self.owner_headers,
                json={"baseline_source": baseline, "candidate_source": tampered_cand},
            )
            self.assertEqual(r_pv_tamper.status_code, 422, r_pv_tamper.text)

            r_psrc_denied = client.post(
                f"{self.base}/v1/proof/search",
                headers=self.agent_headers,
                json={"baseline_source": baseline, "candidate_source": good_cand},
            )
            self.assertEqual(r_psrc_denied.status_code, 403, r_psrc_denied.text)

            # Ralph loop search over multiple candidates
            r_ralph = client.post(
                f"{self.base}/v1/proof/search",
                headers=self.owner_headers,
                json={
                    "problem_id": "square-ralph",
                    "baseline_source": baseline,
                    "candidates": ["    return n + 2", "    return n * n"],
                    "max_iterations": 3,
                    "verification_tests": ["assert compute_square(6) == 36"],
                },
            )
            self.assertEqual(r_ralph.status_code, 200, r_ralph.text)
            self.assertTrue(r_ralph.json()["verified"])
            self.assertEqual(r_ralph.json()["search_mode"], "ralph")
            self.assertEqual(r_ralph.json()["iterations"], 2)

            # Agent D search over structured candidate pool
            r_agentd = client.post(
                f"{self.base}/v1/proof/search",
                headers=self.owner_headers,
                json={
                    "problem_id": "square-agentd",
                    "baseline_source": baseline,
                    "search_mode": "agent_d",
                    "candidates": [
                        {"id": "bad", "replacements": ["    return n + 1"]},
                        {"id": "good", "replacements": ["    return n * n"]},
                    ],
                    "verification_tests": ["assert compute_square(7) == 49"],
                },
            )
            self.assertEqual(r_agentd.status_code, 200, r_agentd.text)
            self.assertTrue(r_agentd.json()["verified"])
            self.assertEqual(r_agentd.json()["search_mode"], "agent_d")
            self.assertEqual(r_agentd.json()["verified_count"], 1)

            # Backwards-compatible single-candidate search
            r_single = client.post(
                f"{self.base}/v1/proof/search",
                headers=self.owner_headers,
                json={
                    "baseline_source": baseline,
                    "candidate_source": good_cand,
                    "verification_tests": ["assert compute_square(4) == 16"],
                },
            )
            self.assertEqual(r_single.status_code, 200, r_single.text)
            self.assertTrue(r_single.json()["verified"])

            # 14. Opt-in gates: OpenAI proxy disabled -> 403; Screen ingest & External scan ignore require_enabled=false -> 403!
            r_oai_models = client.get(f"{self.base}/v1/openai/v1/models", headers=self.agent_headers)
            self.assertEqual(r_oai_models.status_code, 403, r_oai_models.text)

            r_oai_chat = client.post(
                f"{self.base}/v1/openai/v1/chat/completions",
                headers=self.agent_headers,
                json={"model": "second-brain/qwen2.5:3b", "messages": [{"role": "user", "content": "hello"}]},
            )
            self.assertEqual(r_oai_chat.status_code, 403, r_oai_chat.text)

            # Once explicitly enabled in security_dir, /v1/openai/v1/models returns 200
            from local_ai_proxy import configure_proxy

            configure_proxy(enabled=True, state_dir=self.sec_dir)
            r_oai_ok = client.get(f"{self.base}/v1/openai/v1/models", headers=self.agent_headers)
            self.assertEqual(r_oai_ok.status_code, 200, r_oai_ok.text)
            self.assertEqual(r_oai_ok.json()["object"], "list")

            # Screen ingest/capture cannot bypass disabled state via require_enabled=False
            tiny_b64 = base64.b64encode(b"not-a-real-png-but-valid-b64").decode("ascii")
            for screen_ep in ("/v1/screen/ingest", "/v1/screen/capture"):
                r_scr = client.post(
                    f"{self.base}{screen_ep}",
                    headers=self.owner_headers,
                    json={"image_b64": tiny_b64, "window_title": "Test", "require_enabled": False},
                )
                self.assertEqual(r_scr.status_code, 403, f"{screen_ep}: {r_scr.text}")

            # External folders scan cannot bypass disabled state via require_enabled=False
            r_ext_scan = client.post(
                f"{self.base}/v1/external-folders/scan",
                headers=self.owner_headers,
                json={"require_enabled": False},
            )
            self.assertEqual(r_ext_scan.status_code, 403, r_ext_scan.text)

            # External folders config forwards brain_dir on remove_folder, tunnel forwards tunnel_id/allow_quick_tunnel,
            # and Open WebUI tool export aligns with active server port + /v1/openai/v1
            ext_dir = self.tpath / "ext_docs"
            ext_dir.mkdir()
            (ext_dir / "note.md").write_text("external_prune_probe_xyz\n", encoding="utf-8")
            r_ext_add = client.post(
                f"{self.base}/v1/external-folders/config",
                headers=self.owner_headers,
                json={"enabled": True, "add_folder": str(ext_dir)},
            )
            self.assertEqual(r_ext_add.status_code, 200, r_ext_add.text)
            r_ext_scan_ok = client.post(f"{self.base}/v1/external-folders/scan", headers=self.owner_headers, json={})
            self.assertEqual(r_ext_scan_ok.status_code, 200, r_ext_scan_ok.text)
            r_ext_rem = client.post(
                f"{self.base}/v1/external-folders/config",
                headers=self.owner_headers,
                json={"remove_folder": str(ext_dir)},
            )
            self.assertEqual(r_ext_rem.status_code, 200, r_ext_rem.text)
            r_ext_search = client.get(
                f"{self.base}/v1/external-folders/search?query=external_prune_probe_xyz",
                headers=self.owner_headers,
            )
            self.assertEqual(r_ext_search.status_code, 200, r_ext_search.text)
            self.assertEqual(r_ext_search.json()["results"], [])

            r_tun_bad = client.post(
                f"{self.base}/v1/tunnel/config",
                headers=self.owner_headers,
                json={"enabled": True, "provider": "cloudflared"},
            )
            self.assertEqual(r_tun_bad.status_code, 403, r_tun_bad.text)

            r_tun_ok = client.post(
                f"{self.base}/v1/tunnel/config",
                headers=self.owner_headers,
                json={"enabled": True, "provider": "cloudflared", "tunnel_id": "sb-named-01", "allow_quick_tunnel": False},
            )
            self.assertEqual(r_tun_ok.status_code, 200, r_tun_ok.text)
            self.assertEqual(r_tun_ok.json()["tunnel_id"], "sb-named-01")

            r_owui = client.get(f"{self.base}/v1/orchestrator/open-webui-tool", headers=self.owner_headers)
            self.assertEqual(r_owui.status_code, 200, r_owui.text)
            self.assertEqual(r_owui.json()["openai_proxy_base_url"], f"http://127.0.0.1:{self.port}/v1/openai/v1")

    def test_02_memory_api_client_sdk_full_phase8_10_parity(self) -> None:
        baseline, good_cand, _ = _sample_python_spec()
        c = self.owner_client
        self.assertEqual(c.owner_token, self.owner_tok)

        # Gen-4 Cognitive SDK methods
        sig = c.send_context_signal(
            active_file="scripts/auth_service.py",
            git_branch="main",
            project_scope="project:alpha",
            open_symbols=("verify_token",),
            cursor_topic="JWT RS256",
            prewarm=True,
        )
        self.assertIn("freshness_key", sig)
        self.assertIn("l0_vault_entries", c.bus_status())

        zoom = c.zoom_recall("auth_service", target_level="L4", project="project:alpha", limit=5)
        self.assertEqual(zoom["target_level"], "L4")

        concepts = c.list_concepts(project="project:alpha", limit=20)
        self.assertGreaterEqual(len(concepts["concepts"]), 1)
        first_cid = concepts["concepts"][0]["concept_id"]
        c_detail = c.get_concept(first_cid, project="project:alpha")
        self.assertEqual(c_detail["concept_id"], first_cid)

        dist = c.distill_principles(project="project:alpha")
        self.assertGreaterEqual(len(dist["principles_created"]), 1)
        p_info = dist["principles_created"][0]
        p_all = c.list_principles(project="project:alpha", active_only=False)
        self.assertGreaterEqual(len(p_all["principles"]), 1)
        p_prom = c.promote_principle(p_info["filename"], p_info["sha256"], actor="camer", note="SDK promote")
        self.assertEqual(p_prom["status"], "active")

        mm_all = c.list_mental_models(project="project:alpha", active_only=False)
        self.assertEqual(len(mm_all["mental_models"]), 1)
        mm_prom = c.promote_mental_model(
            self.mm_draft["filename"],
            self.mm_draft["sha256"],
            actor="camer",
            note="SDK mm promote",
        )
        self.assertEqual(mm_prom["status"], "active")

        sw = c.run_swarm("How does api_gateway verify auth_service tokens?", project="project:alpha", max_atoms=4)
        self.assertIn("roles", sw)
        deb = c.run_debate("auth_service rotation policy", project="project:alpha")
        self.assertIn("thesis", deb)
        sim = c.simulate_impact("auth_service", max_hops=3, project="project:alpha")
        self.assertEqual(sim["target"], "auth_service")

        self.assertIn("curiosity_targets", c.get_curiosity(project="project:alpha"))
        self.assertIn("domains", c.get_self_model())

        self.assertIn("adrs", c.adr_status())
        adr_res = c.adr_enable("ADR-NOTES-CRDT", actor="camer", rationale="SDK ADR test")
        self.assertEqual(adr_res["status"], "enabled")

        arch = c.cold_archive_atom("legacy-cron.md", self.sha_cold, actor="camer")
        self.assertEqual(arch["status"], "cold_archived")
        rest = c.restore_cold_archive("legacy-cron.md", actor="camer", expected_sha256=arch["sha256"])
        self.assertEqual(rest["status"], "restored")

        rep = c.run_episodic_replay(date_str="2026-11-24", write_inbox=False)
        self.assertEqual(rep["status"], "completed")

        routed = c.route_query("Compute 19 * 21")
        self.assertEqual(routed["mode"], "quantitative_derivation")

        # Math & Proof SDK methods
        math_res = c.solve_math("Calculate 19 * 21", samples=2, use_local_llm=False)
        self.assertEqual(math_res["majority_answer"], "399")
        rub_res = c.score_math_rubric(
            "Calculate 19 * 21",
            "Step 1: 19 * 20 = 380.\nStep 2: 380 + 19 = 399.\nFinal Answer: \\boxed{399}",
            expected_answer="399",
        )
        self.assertGreater(rub_res["weighted_total"], 0.7)

        pst = c.proof_status()
        self.assertIn("lean_available", pst)
        self.assertIn("lean_path", pst)

        pv = c.proof_verify(baseline, good_cand, test_lemmas=("assert compute_square(3) == 9",))
        self.assertTrue(pv["valid"])
        self.assertTrue(pv["test_lemmas"]["passed"])

        ps = c.proof_search(
            "sdk-square",
            baseline,
            candidates=("    return n * n",),
            max_iterations=2,
            verification_tests=("assert compute_square(8) == 64",),
        )
        self.assertTrue(ps["verified"])

        # Ecosystem SDK methods
        presets = c.design_presets()
        self.assertGreaterEqual(len(presets["presets"]), 1)
        crit = c.design_critique("<main><button aria-label='Go'>Go</button></main>", preset="editorial_luxury")
        self.assertEqual(len(crit["panels"]), 5)
        self.assertIn("tts", c.voice_status())
        self.assertIn("enabled", c.screen_status())
        self.assertIn("enabled", c.external_folders_status())
        self.assertIn("imported_packs", c.packs_list())
        self.assertIn("services", c.orchestrator_status())
        self.assertIn("provider", c.tunnel_status())
        self.assertIn("running", c.speculative_status())

        # Non-owner agent client fails closed on owner-gated SDK methods (403)
        with self.assertRaises(httpx.HTTPStatusError) as cm:
            self.agent_client.proof_verify(baseline, good_cand)
        self.assertEqual(cm.exception.response.status_code, 403)

    def test_03_deliberative_streaming_chat_meta_router_and_l0_bus(self) -> None:
        import streaming_chat as sc

        cp = sc.ChatPaths(
            brain_dir=str(self.brain),
            atoms_dir=str(self.brain / "2-atoms"),
            lexical_index_path=str(self.brain / ".sidecar" / "lexical.sqlite3"),
            chroma_path=str(self.brain / ".chroma"),
            policy_path=self.sec_dir / "policy.json",
            security_dir=self.sec_dir,
            persist_threads_allowed=True,
        )

        # 1. Causal simulation route (allow_repo_scan=False enforced)
        ev_causal = list(
            sc.chat_events(
                "If I change auth_service what breaks downstream?",
                mode="deliberative",
                paths=cp,
                project_scope="project:alpha",
            )
        )
        route_c = next(e for e in ev_causal if e.get("type") == "cognitive_route")
        self.assertEqual(route_c["mode"], "causal_simulation")
        self.assertIn("causal_simulation", route_c["signals"])
        self.assertEqual(route_c["signals"]["causal_simulation"]["target"], "auth_service")
        self.assertEqual(route_c["signals"]["causal_simulation"]["provenance_summary"]["code_ast_dependency"], 0)
        self.assertTrue(any(e.get("type") == "token" for e in ev_causal))
        self.assertTrue(any(e.get("type") == "done" for e in ev_causal))

        # 2. Quantitative derivation route (sandbox solved offline, use_local_llm=False)
        ev_quant = list(
            sc.chat_events(
                "Calculate 17 * 23 + 9",
                mode="deliberative",
                paths=cp,
            )
        )
        route_q = next(e for e in ev_quant if e.get("type") == "cognitive_route")
        self.assertEqual(route_q["mode"], "quantitative_derivation")
        self.assertEqual(route_q["signals"]["quantitative_derivation"]["majority_answer"], "400")
        g_q = next(e for e in ev_quant if e.get("type") == "grounding")
        self.assertTrue(g_q.get("sandbox_verified"))

        # 3. Complex multi-hop / contested route via 'cognitive' alias
        ev_complex = list(
            sc.chat_events(
                "Compare and reconcile auth_service signing policy versus api_gateway token verification step-by-step",
                mode="cognitive",
                paths=cp,
                project_scope="project:alpha",
            )
        )
        route_x = next(e for e in ev_complex if e.get("type") == "cognitive_route")
        self.assertIn("cognitive_swarm", route_x["signals"])
        self.assertTrue(any(e.get("type") == "token" for e in ev_complex))

        # 4. Abstention invariant preserved when zero verified evidence supports query
        ev_abstain = list(
            sc.chat_events(
                "What is the cafeteria lunch menu on Neptune?",
                mode="deliberative",
                paths=cp,
                project_scope="project:alpha",
            )
        )
        self.assertTrue(any(e.get("type") == "cognitive_route" for e in ev_abstain))
        self.assertTrue(any(e.get("type") == "abstain" for e in ev_abstain))

        # 5. Live HTTP SSE /v1/chat/stream with mode='deliberative'
        sse_events = self.agent_client.chat_stream(
            "Calculate 12 * 11",
            mode="deliberative",
            project_scope="project:alpha",
        )
        self.assertTrue(any(e.get("type") == "cognitive_route" and e.get("mode") == "quantitative_derivation" for e in sse_events))
        self.assertTrue(any(e.get("type") == "done" for e in sse_events))

        # 6. Verify retrieve_context enables use_l0_bus=True for standard lexical/auto recall and False for expired/as_of
        seen_l0_flags: list[bool] = []
        orig_search = sc.search_memory

        def _spy_search(query, opts, **kwargs):
            seen_l0_flags.append(bool(opts.use_l0_bus))
            return orig_search(query, opts, **kwargs)

        with patch.object(sc, "search_memory", side_effect=_spy_search):
            sc.retrieve_context("auth_service", cp, project_scope="project:alpha", retrieval_mode="lexical")
            sc.retrieve_context("auth_service", cp, project_scope="project:alpha", retrieval_mode="lexical", include_expired=True)
            sc.retrieve_context("auth_service", cp, project_scope="project:alpha", retrieval_mode="lexical", as_of="2026-11-24")
        self.assertEqual(seen_l0_flags, [True, False, False])

    def test_04_cli_help_parity_and_recall_zoom_dispatch(self) -> None:
        from scripts import cli

        # Top-level --help includes 'math' and 'proof'
        buf_top = io.StringIO()
        with redirect_stdout(buf_top):
            with self.assertRaises(SystemExit) as cm_top:
                cli.main(["--help"])
        self.assertEqual(cm_top.exception.code, 0)
        top_help = buf_top.getvalue()
        self.assertIn("math", top_help)
        self.assertIn("proof", top_help)

        # math --help, proof --help, recall zoom --help all exit 0
        for argv, expected_sub in (
            (["math", "--help"], "problem"),
            (["proof", "--help"], "verify"),
            (["recall", "zoom", "--help"], "sync"),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    cli.main(argv)
            self.assertEqual(cm.exception.code, 0, f"CLI {argv} exited {cm.exception.code}")
            self.assertIn(expected_sub, buf.getvalue())

        # recall zoom sync actually runs sync (not query 'sync'!)
        buf_sync = io.StringIO()
        with patch.dict(os.environ, {"BRAIN_VAULT_DIR": str(self.brain)}):
            with redirect_stdout(buf_sync):
                rc = cli.main(["recall", "zoom", "sync", "--json"])
        self.assertEqual(rc, 0)
        sync_payload = json.loads(buf_sync.getvalue())
        self.assertIn("node_count", sync_payload)
        self.assertGreater(sync_payload["node_count"], 0)


if __name__ == "__main__":
    unittest.main()
