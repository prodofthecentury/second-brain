# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Regression suite verifying all 1C Cross-Layer & Client UI Parity Gaps."""



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
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from scripts.api_product import ensure_owner_token  # noqa: E402
from scripts.atom_mutator import apply_review_transition  # noqa: E402
from scripts.build_clipper import build_store_clipper_bundles  # noqa: E402
from scripts.chat_threads import append_chat_turn, create_chat_thread  # noqa: E402
from scripts.evidence import find_evidence_anchor  # noqa: E402
from scripts.executable_skills import set_skill_kill_switch  # noqa: E402
from scripts.inbox_drop import export_static_pwa_bundle  # noqa: E402
from scripts.lexical_store import rebuild_lexical_index  # noqa: E402
from scripts.memory_api import (  # noqa: E402
    MemoryApiClient,
    MemoryApiRuntimeConfig,
    MemoryLoopbackHTTPServer,
    generate_memory_api_token,
    render_host_adapter_configs,
    reset_api_rate_limiter,
)
from scripts.mobile_vault_client import export_mobile_vault_client_bundle  # noqa: E402
from scripts.notes_store import NotePaths, create_note  # noqa: E402
from scripts.skill_playbooks import promote_skill_playbook  # noqa: E402
from scripts.vault_sync import init_sync  # noqa: E402


def _seed_atom(
    vault: Path,
    *,
    filename: str,
    claim: str,
    project: str = "project:alpha",
    memory_type: str = "semantic",
    valid_from: str = "",
    valid_until: str = "",
    approve: bool = True,
) -> Path:
    raw_dir = vault / "0-raw"
    atoms_dir = vault / "2-atoms"
    archive_dir = raw_dir / "archive"
    sidecar_dir = vault / ".sidecar"
    for d in (raw_dir, atoms_dir, archive_dir, sidecar_dir, vault / "5-playbooks", vault / "6-skills"):
        d.mkdir(parents=True, exist_ok=True)

    src_name = f"src-{filename}"
    (raw_dir / src_name).write_text(f"Primary engineering source: {claim}\n", encoding="utf-8")
    temporal_lines = ""
    if valid_from:
        temporal_lines += f"valid_from: {valid_from}\n"
    if valid_until:
        temporal_lines += f"valid_until: {valid_until}\n"

    draft = (
        "---\n"
        f"id: {Path(filename).stem}\n"
        "type: atom\n"
        f"memory_type: {memory_type}\n"
        "epistemic_label: claim\n"
        "created: 2026-11-10\n"
        "last-reviewed: 2026-11-15\n"
        "confidence: 0.90\n"
        "importance: 0.85\n"
        f"{temporal_lines}"
        "sources:\n"
        f"  - 0-raw/{src_name}\n"
        "contexts:\n"
        f"  - {project}\n"
        "review_status: pending\n"
        "claim_state: candidate\n"
        "citation_integrity: missing\n"
        "---\n\n"
        "## Claim\n"
        f"{claim}\n\n"
        "## Evidence\n"
        f"{claim}\n\n"
        "## Why It Matters\n"
        "Parity 1C test atom.\n"
    )
    anchor = find_evidence_anchor(draft, str(vault))
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
            actor="parity_1c_owner",
            decision_log_path=str(archive_dir / "decision-record.jsonl"),
            brain_dir=str(vault),
            atoms_dir=str(atoms_dir),
        )
    rebuild_lexical_index(str(atoms_dir), str(sidecar_dir / "lexical.sqlite3"))
    return atom_path


class GraphTemporalApiAndSdkTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_api_rate_limiter()

    def test_temporal_graph_endpoints_sdk_parity_methods_and_adapter_configs(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            vault = root / "brain"
            sec = root / "security"
            sec.mkdir(parents=True, exist_ok=True)
            _seed_atom(
                vault,
                filename="rule-2024.md",
                claim="API v1 allowed legacy TLS 1.1 during 2024 migration.",
                valid_from="2024-01-01",
                valid_until="2024-12-31",
            )
            _seed_atom(
                vault,
                filename="rule-2026.md",
                claim="API v2 requires strict TLS 1.3 mTLS across all nodes.",
                valid_from="2026-01-01",
                valid_until="2027-12-31",
            )

            with patch.dict(os.environ, {"BRAIN_SECURITY_STATE_DIR": str(sec)}):
                sync_dest = root / "sync_remote"
                sync_dest.mkdir(parents=True, exist_ok=True)
                init_sync(
                    str(sync_dest),
                    backend_type="local_folder",
                    brain_dir=str(vault),
                    passphrase="correct horse battery parity 1c",
                    state_dir=sec,
                )

                tok_path = sec / "memory-api-token"
                own_path = sec / "owner-token"
                agent_tok, _ = generate_memory_api_token(tok_path)
                owner_tok = ensure_owner_token(own_path)
                rt = MemoryApiRuntimeConfig(
                    brain_dir=str(vault),
                    token_path=tok_path,
                    owner_token_path=own_path,
                    policy_path=sec / "policy.json",
                    security_dir=sec,
                )
                server = MemoryLoopbackHTTPServer("127.0.0.1", 0, rt)
                port = server.server_address[1]
                t = threading.Thread(target=server.serve_forever, daemon=True)
                t.start()
                try:
                    sdk_owner = MemoryApiClient(port=port, token=agent_tok, owner_token=owner_tok)

                    # 1. Bi-Temporal /v1/graph/summary and /v1/graph/full
                    sum_2024 = sdk_owner.graph_summary(as_of="2024-06-15", include_expired=False)
                    self.assertEqual(sum_2024["as_of"], "2024-06-15")
                    self.assertEqual(sum_2024["include_expired"], False)
                    self.assertIn("2024-01-01", sum_2024["timeline_dates"])
                    self.assertIn("2027-12-31", sum_2024["timeline_dates"])

                    full_2024 = sdk_owner.graph_full(as_of="2024-06-15", include_expired=False)
                    self.assertEqual([n["id"] for n in full_2024["nodes"]], ["rule-2024.md"])

                    full_2026_all = sdk_owner.graph_full(as_of="2026-06-15", include_expired=True)
                    by_id = {n["id"]: n for n in full_2026_all["nodes"]}
                    self.assertEqual(by_id["rule-2024.md"]["temporal_status"], "expired")
                    self.assertEqual(by_id["rule-2026.md"]["temporal_status"], "active")

                    # 2. All newly added MemoryApiClient parity endpoints
                    imp_src = root / "single-import.md"
                    imp_src.write_text("# Imported Note\n\nSingle file import content.\n", encoding="utf-8")
                    imp_res = sdk_owner.import_file(str(imp_src), tags=["project:alpha"])
                    self.assertIn(imp_res.get("status"), {"imported", "queued", "saved", "ok"})

                    exp_dir = vault / "4-experiences"
                    exp_dir.mkdir(parents=True, exist_ok=True)
                    (exp_dir / "exp-parity.md").write_text("# Parity Experience\n\nVerified outcome.\n", encoding="utf-8")
                    sec_list = sdk_owner.list_vault_section("experiences")
                    self.assertGreaterEqual(len(sec_list.get("items") or []), 1)
                    one_item = sdk_owner.get_vault_item("experiences", "exp-parity.md")
                    self.assertIn("Verified outcome.", one_item.get("content", ""))

                    led_hist = sdk_owner.ledger_history(limit=10, offset=0)
                    self.assertIn("entries", led_hist)
                    led_leaves = sdk_owner.ledger_leaves(limit=20, offset=0)
                    self.assertIn("leaves", led_leaves)

                    sdk_owner.enqueue_scout_url("https://example.com/feed.xml", is_feed=True)
                    sq = sdk_owner.scout_queue()
                    self.assertEqual(len(sq["items"]), 1)

                    job_start = sdk_owner.start_job("doctor")
                    self.assertIn("job_id", job_start)
                    job_poll = sdk_owner.get_job(str(job_start["job_id"]))
                    self.assertEqual(job_poll["job_id"], job_start["job_id"])

                    import scripts.streaming_chat as bare_streaming_chat

                    with patch.object(
                        bare_streaming_chat,
                        "warmup",
                        return_value={"model": "qwen2.5-coder:7b", "warmup_ms": 1.0},
                    ):
                        warm = sdk_owner.chat_warmup()
                    self.assertEqual(warm["model"], "qwen2.5-coder:7b")

                    # 3. Mobile Inbox Drop include_recall + export_inbox_drop_recall + rotate_inbox_drop_recall
                    pair_res = sdk_owner.pair_inbox_drop(label="ParityPhone", include_recall=True)
                    self.assertTrue(pair_res["include_recall"])
                    self.assertTrue(pair_res.get("recall_key_id"))
                    exp_res = sdk_owner.export_inbox_drop_recall()
                    # rule-2024.md is expired in 2026 so 1 active live atom (rule-2026.md) is exported
                    self.assertGreaterEqual(exp_res["atom_count"], 1)
                    rot_res = sdk_owner.rotate_inbox_drop_recall(reexport=True)
                    self.assertIn("recall_key_id", rot_res)

                    # 4. Adapter configs expose all 13 added HTTP endpoints
                    cfg = render_host_adapter_configs(port=port)
                    eps = set(cfg["loopback_http_api"]["endpoints"])
                    for expected_ep in (
                        "POST /v1/import/file",
                        "GET /v1/ledger/history",
                        "GET /v1/ledger/leaves",
                        "GET /v1/graph/full",
                        "GET /v1/vault/<section>",
                        "GET /v1/vault/<section>/<filename>",
                        "GET /v1/scout/queue",
                        "POST /v1/scout/queue",
                        "POST /v1/scout/run",
                        "POST /v1/jobs/<kind>",
                        "GET /v1/jobs/<id>",
                        "POST /v1/sync/inbox-drop/export-recall",
                        "POST /v1/sync/inbox-drop/rotate-recall",
                        "POST /v1/chat/warmup",
                    ):
                        self.assertIn(expected_ep, eps)
                finally:
                    server.shutdown()
                    server.server_close()


class McpExpanded27ToolsAndNliTests(unittest.TestCase):
    def test_27_mcp_tools_nli_auto_enable_and_scope_isolation(self) -> None:
        from mcp import types
        from scripts import mcp_server
        from scripts.creative_media import generate_local_image

        expected_new = {
            "extract_action_items",
            "list_skills",
            "get_skill",
            "execute_skill",
            "list_vaults",
            "federated_recall",
            "media_status",
            "list_generated_media",
            "git_hook_status",
            "distill_git_commits",
        }
        self.assertEqual(len(mcp_server.ALL_TOOLS), 36)
        self.assertTrue(expected_new.issubset(mcp_server.TOOL_NAMES))

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            vault = root / "brain"
            sec = root / "security"
            sec.mkdir(parents=True, exist_ok=True)
            atom_path = _seed_atom(
                vault,
                filename="alpha-deploy.md",
                claim="Verify checksums before shipping release bundles.",
                project="project:alpha",
                memory_type="procedural",
            )
            create_note(
                "Alpha Release Tasks",
                "- [ ] Audit cryptographic checksums (#project:alpha)\n- [x] Done task\n",
                tags=["project:alpha"],
                paths=NotePaths.for_brain(str(vault)),
            )
            pb = promote_skill_playbook(
                playbook_id="alpha-release-pb",
                title="Alpha Release Check",
                trigger="Release cut",
                preconditions=["Vault unlocked"],
                steps=["Verify checksums before shipping release bundles."],
                expected_outputs=["step_01.json"],
                failure_recovery=["Abort release"],
                backing_atoms=[(atom_path.name, hashlib.sha256(atom_path.read_bytes()).hexdigest())],
                actor="mcp_test_owner",
                rationale="MCP 1C test early owner override",
                allow_early_owner_override=True,
                brain_dir=str(vault),
                atoms_dir=str(vault / "2-atoms"),
                playbooks_dir=str(vault / "5-playbooks"),
                telemetry_db_path=str(vault / ".sidecar" / "telemetry.db"),
            )
            from scripts.executable_skills import promote_playbook_to_executable_skill

            promote_playbook_to_executable_skill(
                skill_id="alpha-skill",
                playbook_id="alpha-release-pb",
                expected_playbook_sha256=str(pb["sha256"]),
                step_actions=[{"action_type": "vault_audit", "audit_kind": "sources", "output_file": "step_01.json"}],
                input_schema={"env": {"type": "string", "required": False, "default": "prod"}},
                actor="mcp_test_owner",
                rationale="MCP 1C test skill promotion",
                allow_early_owner_override=True,
                brain_dir=str(vault),
                atoms_dir=str(vault / "2-atoms"),
                playbooks_dir=str(vault / "5-playbooks"),
                skills_dir=str(vault / "6-skills"),
                telemetry_db_path=str(vault / ".sidecar" / "telemetry.db"),
                security_dir=str(sec),
            )
            set_skill_kill_switch(engaged=False, scope="global", actor="mcp_test_owner", brain_dir=str(vault), security_dir=str(sec))
            generate_local_image(
                "Alpha deployment diagram",
                brain_dir=str(vault),
                backend="builtin_procedural_local",
                output_format="png",
                project_scope="project:alpha",
            )

            token = "parity1c-mcp-secret-token-0123456789"
            ctx = SimpleNamespace(meta={"authorization": f"Bearer {token}"})

            async def _call(name: str, arguments: dict) -> types.CallToolResult:
                return await mcp_server.handle_call_tool(
                    ctx,
                    types.CallToolRequestParams(
                        name=name,
                        arguments=arguments,
                        _meta={"authorization": f"Bearer {token}"},
                    ),
                )

            async def _run() -> None:
                listed = await mcp_server.handle_list_tools(ctx, None)
                self.assertEqual(len(listed.tools), 36)

                # list_contradictions auto-enables ONNX NLI when DeBERTa slot verified
                seen_calls: list[dict] = []

                def _spy_detect(**kwargs):
                    seen_calls.append(dict(kwargs))
                    return {"contradiction_count": 0, "nli_enabled": bool(kwargs.get("enable_nli")), "alerts": []}

                import scripts.onnx_models as bare_onnx_mod
                import scripts.knowledge_graph as bare_kg_mod

                with patch.object(bare_onnx_mod, "get_onnx_models_status", return_value={"slots": {"nli": {"verified": True}}}), \
                     patch("scripts.onnx_models.get_onnx_models_status", return_value={"slots": {"nli": {"verified": True}}}), \
                     patch.object(bare_kg_mod, "detect_contradictions", side_effect=_spy_detect), \
                     patch("scripts.knowledge_graph.detect_contradictions", side_effect=_spy_detect):
                    auto_r = json.loads((await _call("list_contradictions", {"project": "project:alpha"})).content[0].text)
                    self.assertTrue(auto_r["nli_enabled"])
                    self.assertTrue(auto_r["onnx_nli_verified"])
                    self.assertEqual(seen_calls[-1]["nli_mode"], "fast")

                    off_r = json.loads((await _call("list_contradictions", {"project": "project:alpha", "enable_nli": False})).content[0].text)
                    self.assertFalse(off_r["nli_enabled"])

                # Note tasks
                tasks_r = json.loads((await _call("extract_action_items", {"project": "project:alpha"})).content[0].text)
                self.assertEqual(len(tasks_r.get("tasks") or tasks_r.get("items") or []), 1)

                # Stage 4 skills list / get / scope isolation / dry-run execute
                ls_r = json.loads((await _call("list_skills", {"project": "project:alpha"})).content[0].text)
                self.assertEqual(ls_r["count"], 1)
                gs_r = json.loads((await _call("get_skill", {"skill_id": "alpha-skill", "project": "project:alpha"})).content[0].text)
                self.assertEqual(gs_r["skill_id"], "alpha-skill")

                denied_scope = await _call("execute_skill", {"skill_id": "alpha-skill", "dry_run": True})
                self.assertTrue(denied_scope.is_error)

                exec_ok = json.loads(
                    (await _call("execute_skill", {"skill_id": "alpha-skill", "project": "project:alpha", "dry_run": True, "inputs": {"env": "staging"}})).content[0].text
                )
                self.assertEqual(exec_ok["status"], "dry_run_verified")

                # Vaults + Media + Git Hook Status
                lv_r = json.loads((await _call("list_vaults", {})).content[0].text)
                self.assertIn("vaults", lv_r)
                fr_r = json.loads((await _call("federated_recall", {"query": "checksums", "project": "project:alpha"})).content[0].text)
                self.assertIn("results", fr_r)

                ms_r = json.loads((await _call("media_status", {})).content[0].text)
                self.assertIn("creative_tier", ms_r)
                mg_r = json.loads((await _call("list_generated_media", {"project": "project:alpha"})).content[0].text)
                self.assertGreaterEqual(len(mg_r.get("items") or []), 1)

                # Init temp git repo with 1 commit -> test distill_git_commits writes only 4-experiences/ + 0-raw/inbox/
                repo_dir = root / "sample_repo"
                repo_dir.mkdir(parents=True, exist_ok=True)
                subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
                gh_r = json.loads((await _call("git_hook_status", {"repo": str(repo_dir)})).content[0].text)
                self.assertIn("installed", gh_r)
                self.assertFalse(gh_r["installed"])
                subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True, capture_output=True)
                subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_dir), check=True, capture_output=True)
                (repo_dir / "README.md").write_text("Parity commit.\n", encoding="utf-8")
                subprocess.run(["git", "add", "README.md"], cwd=str(repo_dir), check=True, capture_output=True)
                subprocess.run(["git", "commit", "-m", "feat: verify checksum pipeline"], cwd=str(repo_dir), check=True, capture_output=True)
                dist_r = json.loads(
                    (await _call("distill_git_commits", {"repo": str(repo_dir), "project": "project:alpha", "max_commits": 1})).content[0].text
                )
                self.assertIn("distilled_count", dist_r)
                # Ensure no unauthorized 2-atoms files were created
                self.assertEqual([p.name for p in (vault / "2-atoms").glob("*.md")], ["alpha-deploy.md"])

            with patch.dict(
                os.environ,
                {
                    "BRAIN_ENABLE_LOCAL_MCP": "1",
                    "BRAIN_MCP_TRUSTED_STDIO": "1",
                    "BRAIN_MCP_BEARER_TOKEN": token,
                    "BRAIN_VAULT_DIR": str(vault),
                    "BRAIN_SECURITY_STATE_DIR": str(sec),
                },
            ):
                asyncio.run(_run())


class Cli1CSubcommandsTests(unittest.TestCase):
    def test_cli_skills_promote_ledger_prove_skill_tasks_benchmark_and_chat_pin(self) -> None:
        from scripts import cli

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            vault = root / "brain"
            sec = root / "security"
            sec.mkdir(parents=True, exist_ok=True)
            atom_path = _seed_atom(
                vault,
                filename="cli-proc.md",
                claim="Check vault integrity before deployment.",
                memory_type="procedural",
            )
            pb = promote_skill_playbook(
                playbook_id="cli-proc-pb",
                title="CLI Proc Checklist",
                trigger="Before deploy",
                preconditions=["Unlocked vault"],
                steps=["Check vault integrity before deployment."],
                expected_outputs=["audit.json"],
                failure_recovery=["Halt deploy"],
                backing_atoms=[(atom_path.name, hashlib.sha256(atom_path.read_bytes()).hexdigest())],
                actor="cli_owner",
                rationale="CLI 1C test early owner override",
                allow_early_owner_override=True,
                brain_dir=str(vault),
                atoms_dir=str(vault / "2-atoms"),
                playbooks_dir=str(vault / "5-playbooks"),
                telemetry_db_path=str(vault / ".sidecar" / "telemetry.db"),
            )
            create_note(
                "CLI Todo Note",
                "- [ ] Ship release tag v1.0.1 (#project:alpha)\n",
                tags=["project:alpha"],
                paths=NotePaths.for_brain(str(vault)),
            )
            t = create_chat_thread(title="CLI Thread", brain_dir=str(vault))
            append_chat_turn(
                str(t["thread_id"]),
                "user",
                "What does deployment require?",
                brain_dir=str(vault),
            )
            append_chat_turn(
                str(t["thread_id"]),
                "assistant",
                "It requires checking vault integrity [V1].",
                cited_sources=[{"citation_id": "V1", "filename": "cli-proc.md", "tier": "approved_atom"}],
                grounding={"fully_cited": True},
                brain_dir=str(vault),
            )

            env_patch = {
                "BRAIN_VAULT_DIR": str(vault),
                "BRAIN_SECURITY_STATE_DIR": str(sec),
            }
            with patch.dict(os.environ, env_patch):
                # 1. skills promote via CLI with --input-schema-json
                out_prom = io.StringIO()
                with patch("sys.stdout", out_prom):
                    rc_prom = cli.main([
                        "skills",
                        "promote",
                        "cli-check-skill",
                        "--playbook-id",
                        "cli-proc-pb",
                        "--expected-playbook-sha256",
                        str(pb["sha256"]),
                        "--step-actions-json",
                        json.dumps([{"action_type": "vault_audit", "audit_kind": "sources", "output_file": "s1.json"}]),
                        "--input-schema-json",
                        json.dumps({"target": {"type": "string", "required": False}}),
                        "--actor",
                        "cli_owner",
                        "--rationale",
                        "CLI 1C skill promotion rationale",
                        "--allow-early-owner-override",
                        "--json",
                    ])
                self.assertEqual(rc_prom, 0)
                prom_json = json.loads(out_prom.getvalue())
                self.assertEqual(prom_json["skill_id"], "cli-check-skill")

                # 2. ledger prove --kind skill
                out_prove = io.StringIO()
                with patch("sys.stdout", out_prove):
                    rc_prove = cli.main(["ledger", "prove", "cli-check-skill.md", "--kind", "skill", "--json"])
                self.assertEqual(rc_prove, 0)
                self.assertEqual(json.loads(out_prove.getvalue())["rel_path"], "6-skills/cli-check-skill.md")

                # 3. second-brain tasks --json
                out_tasks = io.StringIO()
                with patch("sys.stdout", out_tasks):
                    rc_tasks = cli.main(["tasks", "--project", "project:alpha", "--json"])
                self.assertEqual(rc_tasks, 0)
                tasks_parsed = json.loads(out_tasks.getvalue())
                tasks_items = tasks_parsed["tasks"] if isinstance(tasks_parsed, dict) else tasks_parsed
                self.assertEqual(len(tasks_items), 1)

                # 4. second-brain chat --pin <thread_id>
                out_pin = io.StringIO()
                with patch("sys.stdout", out_pin):
                    rc_pin = cli.main(["chat", "--pin", str(t["thread_id"]), "--pin-title", "CLI Pinned Decision"])
                self.assertEqual(rc_pin, 0)
                self.assertIn("Pinned turn #", out_pin.getvalue())

                # 5. live-benchmark / third-party-benchmark dispatch works (--help exits 0 cleanly)
                for subcmd in ("live-benchmark", "third-party-benchmark"):
                    help_buf = io.StringIO()
                    with patch("sys.stdout", help_buf):
                        with self.assertRaises(SystemExit) as cm:
                            cli.main([subcmd, "--help"])
                    self.assertEqual(cm.exception.code, 0)
                    self.assertIn("--dataset", help_buf.getvalue())


class PwaAndClipperManifestPolishTests(unittest.TestCase):
    def test_pwa_icons_and_browser_clipper_manifests(self) -> None:
        from PIL import Image

        # 1. Both PWAs declare valid PNG icons, separate purposes, PNG apple-touch-icon, and sw precache
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            drop_dir = ROOT / "mobile" / "inbox-drop"
            vc_dir = ROOT / "mobile" / "vault-client" / "www"

            for base_dir in (drop_dir, vc_dir):
                manifest = json.loads((base_dir / "manifest.webmanifest").read_text(encoding="utf-8"))
                icons = manifest.get("icons") or []
                self.assertTrue(icons, f"Missing manifest icons in {base_dir}")
                purposes = {entry.get("purpose") for entry in icons}
                self.assertNotIn("any maskable", purposes, f"Do not combine 'any maskable' in {base_dir}")
                self.assertIn("any", purposes)
                self.assertIn("maskable", purposes)
                for entry in icons:
                    icon_file = base_dir / entry["src"]
                    self.assertTrue(icon_file.is_file(), f"Missing PWA icon {icon_file}")
                html = (base_dir / "index.html").read_text(encoding="utf-8")
                self.assertIn('rel="apple-touch-icon"', html)
                self.assertIn("icons/apple-touch-icon.png", html)
                self.assertNotIn('rel="apple-touch-icon" href="icon.svg"', html)
                with Image.open(base_dir / "icons" / "apple-touch-icon.png") as apple_im:
                    self.assertEqual(apple_im.size, (180, 180))
                sw_src = (base_dir / "sw.js").read_text(encoding="utf-8")
                for expected_ic in ("icons/icon-192.png", "icons/icon-512.png", "icons/apple-touch-icon.png"):
                    self.assertIn(expected_ic, sw_src)

            # Export bundles retain nested icons/ PNGs cleanly
            pwa_bundle = export_static_pwa_bundle(tmp_path / "pwa-export")
            self.assertTrue((tmp_path / "pwa-export" / "icons" / "icon-192.png").is_file())
            self.assertGreaterEqual(len(pwa_bundle["files"]), 8)

            vc_zip = export_mobile_vault_client_bundle(tmp_path / "vc-export")
            with zipfile.ZipFile(vc_zip["zip_path"], "r") as zf:
                names = set(zf.namelist())
                self.assertIn("mobile-vault-client/www/icons/icon-192.png", names)
                self.assertIn("mobile-vault-client/www/icons/apple-touch-icon.png", names)

            # 2. Browser clipper unpacked manifest avoids Chrome dual background warning & store builds are valid
            clip_manifest = json.loads((ROOT / "extensions" / "browser-clipper" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(clip_manifest["background"].get("service_worker"), "background.js")
            self.assertNotIn("scripts", clip_manifest["background"])
            for sz in ("16", "32", "48", "128"):
                self.assertEqual(clip_manifest["icons"][sz], f"icons/icon-{sz}.png")
                self.assertEqual(clip_manifest["action"]["default_icon"][sz], f"icons/icon-{sz}.png")
                self.assertTrue((ROOT / "extensions" / "browser-clipper" / f"icons/icon-{sz}.png").is_file())

            bundles = build_store_clipper_bundles(tmp_path / "clipper-dist")
            with zipfile.ZipFile(bundles["chromium_zip"], "r") as czf:
                c_names = czf.namelist()
                self.assertEqual(len(c_names), len(set(c_names)), "Duplicate zip entry in Chromium bundle")
                c_mf = json.loads(czf.read("manifest.json").decode("utf-8"))
                self.assertEqual(c_mf["background"].get("service_worker"), "background.js")
                self.assertNotIn("scripts", c_mf["background"])
                self.assertIn("icons/icon-32.png", c_names)
            with zipfile.ZipFile(bundles["firefox_xpi"], "r") as fzf:
                f_names = fzf.namelist()
                self.assertEqual(len(f_names), len(set(f_names)), "Duplicate zip entry in Firefox XPI bundle")
                f_mf = json.loads(fzf.read("manifest.json").decode("utf-8"))
                self.assertNotIn("service_worker", f_mf["background"])
                self.assertEqual(f_mf["background"]["scripts"], ["lib/clipper-client.js", "lib/readability-markdown.js", "background.js"])
                self.assertIn("icons/icon-32.png", f_names)


if __name__ == "__main__":
    unittest.main()
