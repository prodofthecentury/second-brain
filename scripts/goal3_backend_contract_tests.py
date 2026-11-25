# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Behavioral verification suite for Goal 3 backend/API parity contracts."""



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
    raise SystemExit(_sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__"))


import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import zipfile

import httpx


import scripts.api_product as api_product  # noqa: E402
from scripts.atom_mutator import apply_review_transition  # noqa: E402
from scripts.cognitive_core import count_canonical_corrections, record_grounded_answer_telemetry  # noqa: E402
from scripts.evidence import find_evidence_anchor  # noqa: E402
from scripts.lexical_store import rebuild_lexical_index  # noqa: E402
from scripts.memory_api import (  # noqa: E402
    MemoryApiClient,
    MemoryApiRuntimeConfig,
    MemoryLoopbackHTTPServer,
    discover_active_api_port,
    generate_memory_api_token,
    remove_active_api_port_file,
    write_active_api_port_file,
)
from scripts.memory_format import render_memory_document, set_scalar_field  # noqa: E402
from scripts.skill_playbooks import promote_skill_playbook  # noqa: E402
from scripts.vault_ledger import verify_merkle_proof  # noqa: E402


def _make_minimal_docx(paragraphs: list[str]) -> bytes:
    buf = io.BytesIO()
    body_xml = "".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs
    )
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body_xml}</w:body></w:document>"
    )
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", doc_xml)
    return buf.getvalue()


class Goal3BackendContractTests(unittest.TestCase):
    def test_port_discovery_research_view_playbook_proof_feedback_audit_and_doc_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            for sub in ("0-raw/inbox", "0-raw/archive", "2-atoms", "3-threads", "4-experiences", "5-playbooks", "notes", "briefings", ".sidecar"):
                (brain / sub).mkdir(parents=True, exist_ok=True)

            src_file = brain / "0-raw" / "canary-guide.md"
            quote = "Always verify canary health metrics for 15 minutes before promoting production traffic."
            src_file.write_text(f"# Canary Guide\n\n{quote}\n", encoding="utf-8")

            atom_path = brain / "2-atoms" / "canary-proc.md"
            atom_path.write_text(
                "---\n"
                "id: canary-proc\n"
                "type: atom\n"
                "memory_type: procedural\n"
                "epistemic_label: claim\n"
                "sources:\n"
                "  - 0-raw/canary-guide.md\n"
                "contexts:\n"
                "  - project:alpha\n"
                "review_status: pending\n"
                "claim_state: candidate\n"
                "citation_integrity: missing\n"
                "---\n\n"
                f"## Claim\n{quote}\n\n"
                f"## Evidence\n{quote}\n",
                encoding="utf-8",
            )
            sha = __import__("hashlib").sha256(atom_path.read_bytes()).hexdigest()
            apply_review_transition(
                action="approve",
                atom_path=str(atom_path),
                expected_sha256=sha,
                actor="owner",
                brain_dir=str(brain),
                atoms_dir=str(brain / "2-atoms"),
            )

            # Create one pending atom to verify Research View [UNVERIFIED LEADS]
            pend_path = brain / "2-atoms" / "canary-pending-lead.md"
            pend_path.write_text(
                "---\n"
                "id: canary-pending-lead\n"
                "type: atom\n"
                "memory_type: semantic\n"
                "epistemic_label: claim\n"
                "sources:\n"
                "  - 0-raw/canary-guide.md\n"
                "contexts:\n"
                "  - project:alpha\n"
                "review_status: pending\n"
                "claim_state: candidate\n"
                "citation_integrity: missing\n"
                "---\n\n"
                "## Claim\nExperimental canary rollback threshold is 2% error budget burn.\n\n"
                f"## Evidence\n{quote}\n",
                encoding="utf-8",
            )

            lex_path = brain / ".sidecar" / "lexical.sqlite3"
            rebuild_lexical_index(str(brain / "2-atoms"), str(lex_path))
            tel_path = brain / ".sidecar" / "telemetry.sqlite3"
            for _ in range(3):
                record_grounded_answer_telemetry(
                    ["canary-proc.md"],
                    abstained=False,
                    mode="standard",
                    latency_ms=5.0,
                    db_path=str(tel_path),
                )

            approved_sha = __import__("hashlib").sha256(atom_path.read_bytes()).hexdigest()
            promote_skill_playbook(
                playbook_id="canary-rollout",
                title="Canary Rollout Playbook",
                backing_atoms=(("canary-proc.md", approved_sha),),
                trigger="Production service deployment",
                preconditions=("CI green",),
                steps=(quote,),
                expected_outputs=("Healthy canary",),
                failure_recovery=("Immediate rollback",),
                actor="owner",
                rationale="Promote verified canary SOP",
                brain_dir=str(brain),
                atoms_dir=str(brain / "2-atoms"),
                playbooks_dir=str(brain / "5-playbooks"),
                telemetry_db_path=str(tel_path),
            )

            sec_dir = root / "sec"
            sec_dir.mkdir()
            tok, tok_file = generate_memory_api_token(sec_dir / "memory-api-token")
            owner_tok = api_product.ensure_owner_token(sec_dir / "owner-token")

            rt = MemoryApiRuntimeConfig(
                brain_dir=str(brain),
                atoms_dir=str(brain / "2-atoms"),
                inbox_dir=str(brain / "0-raw" / "inbox"),
                experiences_dir=str(brain / "4-experiences"),
                playbooks_dir=str(brain / "5-playbooks"),
                lexical_index_path=str(lex_path),
                chroma_path=str(brain / ".chroma"),
                telemetry_db_path=str(tel_path),
                token_path=tok_file,
                owner_token_path=sec_dir / "owner-token",
                policy_path=sec_dir / "host-policy.json",
            )
            server = MemoryLoopbackHTTPServer("127.0.0.1", 0, rt)
            port = server.server_address[1]
            port_file = sec_dir / "api-port"
            write_active_api_port_file(port, port_file=port_file)
            self.assertEqual(discover_active_api_port(8765, port_file=port_file), port)

            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                old_port_env = os.environ.get("BRAIN_API_PORT_FILE")
                os.environ["BRAIN_API_PORT_FILE"] = str(port_file)
                try:
                    # Auto-port discovery when port=None!
                    sdk = MemoryApiClient(port=None, token=tok, owner_token=owner_tok)
                    self.assertEqual(sdk.health()["status"], "ok")
                finally:
                    if old_port_env is None:
                        os.environ.pop("BRAIN_API_PORT_FILE", None)
                    else:
                        os.environ["BRAIN_API_PORT_FILE"] = old_port_env

                headers = {
                    "Authorization": f"Bearer {tok}",
                    "X-Owner-Token": owner_tok,
                    "Host": f"127.0.0.1:{port}",
                }
                base = f"http://127.0.0.1:{port}"
                with httpx.Client(trust_env=False, timeout=20.0) as client:
                    # 1. Playbook candidates return grounding_units
                    r_cands = client.get(f"{base}/v1/playbooks/candidates", headers=headers)
                    self.assertEqual(r_cands.status_code, 200)
                    cand0 = r_cands.json()["candidates"][0]
                    self.assertIn(quote, cand0["grounding_units"])

                    # 2. Ledger commit, history, leaves & Playbook leaf Merkle proof!
                    r_com = client.post(f"{base}/v1/ledger/commit", headers=headers, json={"actor": "owner", "note": "cp1"})
                    self.assertEqual(r_com.status_code, 201)
                    r_ver = client.get(f"{base}/v1/ledger/verify", headers=headers)
                    self.assertEqual(len(r_ver.json()["entries"]), 1)
                    r_hist = client.get(f"{base}/v1/ledger/history", headers=headers)
                    self.assertEqual(r_hist.json()["total"], 1)
                    r_leaves = client.get(f"{base}/v1/ledger/leaves", headers=headers)
                    rels = {item["rel_path"] for item in r_leaves.json()["items"]}
                    self.assertIn("5-playbooks/canary-rollout.md", rels)

                    r_pb_proof = client.get(f"{base}/v1/ledger/prove/5-playbooks/canary-rollout.md", headers=headers)
                    self.assertEqual(r_pb_proof.status_code, 200)
                    pb_proof = r_pb_proof.json()
                    self.assertEqual(pb_proof["rel_path"], "5-playbooks/canary-rollout.md")
                    self.assertTrue(verify_merkle_proof(pb_proof))

                    # 3. Aligned feedback: 'superseded' does NOT log a false correction; actor & note preserved!
                    r_fb = client.post(
                        f"{base}/v1/cognitive/feedback",
                        headers=headers,
                        json={"atom_filename": "canary-proc.md", "feedback": "superseded", "actor": "alice", "note": "Replaced by v2"},
                    )
                    self.assertEqual(r_fb.status_code, 200)
                    self.assertEqual(r_fb.json()["feedback_kind"], "supersession")
                    self.assertEqual(count_canonical_corrections(str(brain), "canary-proc.md"), 0)

                    # 4. Research answer_view in /v1/chat/stream emits research_leads without promoting pending atoms!
                    r_sse = client.post(
                        f"{base}/v1/chat/stream",
                        headers=headers,
                        json={
                            "query": "canary health metrics rollback threshold",
                            "mode": "instant",
                            "answer_view": "research",
                            "retrieval_mode": "lexical",
                        },
                    )
                    self.assertEqual(r_sse.status_code, 200)
                    events = [
                        json.loads(line[6:])
                        for line in r_sse.text.splitlines()
                        if line.startswith("data: ")
                    ]
                    lead_events = [ev for ev in events if ev.get("type") == "research_leads"]
                    self.assertEqual(len(lead_events), 1)
                    self.assertIn("UNVERIFIED LEADS", lead_events[0]["text"])
                    self.assertEqual(lead_events[0]["leads"][0]["filename"], "canary-pending-lead.md")

                    # 5. Full graph, vault browsers, scout queue, and DOCX/HTML import!
                    r_graph = client.get(f"{base}/v1/graph/full?include_pending=1", headers=headers)
                    self.assertEqual(r_graph.status_code, 200)
                    self.assertGreaterEqual(r_graph.json()["node_count"], 2)

                    r_exp = client.post(
                        f"{base}/v1/cognitive/experiences",
                        headers=headers,
                        json={
                            "goal": "Test Goal 3 Experience",
                            "result_summary": "Verified extended experience fields.",
                            "success": True,
                            "tools_used": ["pytest"],
                            "referenced_atoms": ["canary-proc.md"],
                            "plan": ["Step 1"],
                            "reasoning_summary": "Clean deterministic test.",
                            "confidence_before": 0.6,
                            "confidence_after": 0.95,
                            "reflection": "Full parity achieved.",
                        },
                    )
                    self.assertEqual(r_exp.status_code, 201)
                    exp_fname = r_exp.json()["filename"]
                    r_vault_exp = client.get(f"{base}/v1/vault/experiences/{exp_fname}", headers=headers)
                    self.assertEqual(r_vault_exp.status_code, 200)
                    self.assertIn("confidence_after: 0.95", r_vault_exp.json()["content"])

                    r_sq = client.post(f"{base}/v1/scout/queue", headers=headers, json={"url": "https://example.com/feed.xml", "is_feed": True})
                    self.assertEqual(r_sq.status_code, 201)
                    r_sq_list = client.get(f"{base}/v1/scout/queue", headers=headers)
                    self.assertEqual(r_sq_list.json()["items"][0]["kind"], "feed")

                    docs_src = root / "ext_docs"
                    docs_src.mkdir()
                    docx_file = docs_src / "architecture-spec.docx"
                    docx_file.write_bytes(_make_minimal_docx(["Zero-Trust Architecture", "Enforce mTLS across internal RPCs."]))
                    (docs_src / "overview.html").write_text("<html><head><title>System Overview</title></head><body><h1>Overview</h1><p>Local-first vault.</p></body></html>", encoding="utf-8")

                    r_imp_file = client.post(f"{base}/v1/import/file", headers=headers, json={"path": str(docx_file), "tags": ["project:alpha"]})
                    self.assertEqual(r_imp_file.status_code, 201)
                    self.assertEqual(r_imp_file.json()["format"], "docx")

                    r_imp_dir = client.post(f"{base}/v1/import", headers=headers, json={"source_dir": str(docs_src), "overwrite_modified": True})
                    self.assertEqual(r_imp_dir.status_code, 200)
                    self.assertEqual(r_imp_dir.json()["created"], 2)

                    # 6. Tamper source to verify /v1/audit/sources stale_count & demote-stale demoted_count!
                    src_file.write_text("# Tampered\n\nDifferent content.\n", encoding="utf-8")
                    r_aud = client.get(f"{base}/v1/audit/sources", headers=headers)
                    self.assertEqual(r_aud.json()["stale_count"], 1)
                    r_dem = client.post(f"{base}/v1/audit/sources/demote-stale", headers=headers, json={"actor": "owner"})
                    self.assertEqual(r_dem.json()["demoted_count"], 1)
            finally:
                server.shutdown()
                server.server_close()
                remove_active_api_port_file(expected_pid=os.getpid(), port_file=port_file)
                self.assertFalse(port_file.exists())


if __name__ == "__main__":
    unittest.main()
