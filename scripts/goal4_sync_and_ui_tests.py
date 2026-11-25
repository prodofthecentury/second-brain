# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Goal 4 verification suite: Zero-Knowledge E2EE Vault Sync, UI parity & CI/release hygiene."""



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


import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(REPO_ROOT))

import scripts.api_product as api_product  # noqa: E402
import scripts.memory_api as memory_api  # noqa: E402
import scripts.vault_sync as vault_sync  # noqa: E402


def _seed_sample_vault(brain_dir: Path, *, note_body: str = "- [ ] Ship E2EE sync #project:alpha\n") -> None:
    for rel in (
        "0-raw/archive",
        "0-raw/inbox",
        "2-atoms",
        "3-threads",
        "4-experiences",
        "5-playbooks",
        "notes",
        "sources",
        "briefings",
        ".sidecar",
        ".chroma",
    ):
        (brain_dir / rel).mkdir(parents=True, exist_ok=True)
    (brain_dir / "house-rules.md").write_text("# House Rules\n\nAlways verify claims.\n", encoding="utf-8")
    (brain_dir / "2-atoms" / "alpha-fact.md").write_text(
        "---\n"
        "id: alpha-fact\n"
        "memory_type: semantic\n"
        "review_status: accepted\n"
        "status: active\n"
        "contexts: [project:alpha]\n"
        "---\n\n"
        "## Claim\n"
        "Second Brain sync encrypts blobs with AES-256-GCM before leaving disk.\n",
        encoding="utf-8",
    )
    (brain_dir / "5-playbooks" / "sync-check.md").write_text(
        "---\n"
        "playbook_id: sync-check\n"
        "status: accepted\n"
        "---\n\n"
        "# Sync Check\n\n1. Run verify.\n",
        encoding="utf-8",
    )
    (brain_dir / "notes" / "shared-note.md").write_text(
        f"---\ntitle: Shared Note\ntags: [project:alpha]\norigin: owner_note\n---\n\n{note_body}",
        encoding="utf-8",
    )
    (brain_dir / "0-raw" / "archive" / "decision-record.jsonl").write_text(
        json.dumps({"timestamp": "2026-11-19T00:00:01Z", "actor": "owner", "atom": "alpha-fact.md", "action": "approve"})
        + "\n",
        encoding="utf-8",
    )
    # Disposable store files that MUST NEVER be synced
    (brain_dir / ".sidecar" / "secret-sidecar-marker.txt").write_text("DO_NOT_SYNC_SIDECAR", encoding="utf-8")
    (brain_dir / ".chroma" / "chroma-marker.bin").write_bytes(b"DO_NOT_SYNC_CHROMA")


class Goal4VaultSyncAndUiParityTests(unittest.TestCase):
    def test_e2ee_two_device_local_folder_zero_knowledge_and_sidecar_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vault_a = root / "device_a_brain"
            vault_b = root / "device_b_brain"
            state_a = root / "state_a"
            state_b = root / "state_b"
            sync_target = root / "cloud_sync_folder"
            _seed_sample_vault(vault_a)
            vault_b.mkdir(parents=True)

            init_res = vault_sync.init_sync(
                str(sync_target),
                backend_type="local_folder",
                brain_dir=vault_a,
                device_name="laptop-a",
                state_dir=state_a,
            )
            self.assertEqual(init_res["status"], "initialized")
            self.assertTrue(init_res["recovery_key_b64"])

            push_res = vault_sync.sync_push(brain_dir=vault_a, state_dir=state_a)
            self.assertEqual(push_res["status"], "pushed")
            self.assertEqual(push_res["epoch"], 1)
            self.assertGreaterEqual(push_res["uploaded_blobs"], 5)

            # Second push with no edits should reuse 100% of blobs
            push2 = vault_sync.sync_push(brain_dir=vault_a, state_dir=state_a)
            self.assertEqual(push2["epoch"], 2)
            self.assertEqual(push2["uploaded_blobs"], 0)
            self.assertEqual(push2["reused_blobs"], push_res["total_files"])

            # Zero-knowledge invariant: NO plaintext filenames or body tokens anywhere on sync_target!
            for fpath in sync_target.rglob("*"):
                if fpath.is_file():
                    raw = fpath.read_bytes()
                    for secret_token in (
                        b"alpha-fact.md",
                        b"shared-note.md",
                        b"AES-256-GCM",
                        b"DO_NOT_SYNC_SIDECAR",
                        b"DO_NOT_SYNC_CHROMA",
                    ):
                        self.assertNotIn(
                            secret_token,
                            raw,
                            f"Plaintext leaked to sync target file {fpath.name}: {secret_token!r}",
                        )

            # Device B joins with recovery key and pulls cleanly
            join_res = vault_sync.join_sync(
                str(sync_target),
                backend_type="local_folder",
                brain_dir=vault_b,
                device_name="desktop-b",
                recovery_key_b64=init_res["recovery_key_b64"],
                state_dir=state_b,
            )
            self.assertEqual(join_res["status"], "joined")

            pull_res = vault_sync.sync_pull(brain_dir=vault_b, state_dir=state_b)
            self.assertEqual(pull_res["status"], "pulled")
            self.assertEqual(pull_res["epoch"], 2)
            self.assertGreaterEqual(pull_res["added"], 5)
            self.assertEqual(pull_res["conflict_count"], 0)
            self.assertTrue(pull_res["indexes_rebuilt"])

            # Canonical files arrived identically; disposable stores NEVER crossed devices
            self.assertEqual(
                (vault_a / "2-atoms" / "alpha-fact.md").read_bytes(),
                (vault_b / "2-atoms" / "alpha-fact.md").read_bytes(),
            )
            self.assertEqual(
                (vault_a / "notes" / "shared-note.md").read_bytes(),
                (vault_b / "notes" / "shared-note.md").read_bytes(),
            )
            self.assertFalse((vault_b / ".sidecar" / "secret-sidecar-marker.txt").exists())
            self.assertFalse((vault_b / ".chroma" / "chroma-marker.bin").exists())
            # Disposable lexical index was rebuilt locally on Device B
            self.assertTrue((vault_b / ".sidecar" / "lexical.sqlite3").is_file())

            ver_res = vault_sync.sync_verify(brain_dir=vault_b, state_dir=state_b)
            self.assertTrue(ver_res["valid"], f"Unexpected verify issues: {ver_res}")
            self.assertEqual(ver_res["issues"], [])
            self.assertGreaterEqual(ver_res["verified_blobs"], 5)

    def test_passphrase_scrypt_kdf_and_wrong_passphrase_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vault_a = root / "va"
            vault_b = root / "vb"
            state_a = root / "sa"
            state_b = root / "sb"
            target = root / "sync"
            _seed_sample_vault(vault_a)
            vault_b.mkdir(parents=True)

            init_res = vault_sync.init_sync(
                str(target),
                brain_dir=vault_a,
                passphrase="correct-horse-battery-staple-99",
                state_dir=state_a,
            )
            self.assertEqual(init_res["kdf_mode"], "passphrase")
            self.assertIsNone(init_res["recovery_key_b64"])

            # Config on disk must NOT store raw master_key_b64 in passphrase mode
            cfg_json = json.loads(vault_sync.sync_config_path(vault_a, state_dir=state_a).read_text(encoding="utf-8"))
            self.assertIsNone(cfg_json.get("master_key_b64"))

            vault_sync.sync_push(
                brain_dir=vault_a,
                passphrase="correct-horse-battery-staple-99",
                state_dir=state_a,
            )

            with self.assertRaises(vault_sync.SyncSecurityError):
                vault_sync.join_sync(
                    str(target),
                    brain_dir=vault_b,
                    passphrase="wrong-passphrase-attempt",
                    state_dir=state_b,
                )

            vault_sync.join_sync(
                str(target),
                brain_dir=vault_b,
                passphrase="correct-horse-battery-staple-99",
                state_dir=state_b,
            )
            pulled = vault_sync.sync_pull(
                brain_dir=vault_b,
                passphrase="correct-horse-battery-staple-99",
                state_dir=state_b,
            )
            self.assertGreaterEqual(pulled["added"], 5)

    def test_append_only_jsonl_set_union_merge_fast_forward_backup_and_conflict_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            va, vb = root / "va", root / "vb"
            sa, sb = root / "sa", root / "sb"
            target = root / "sync"
            _seed_sample_vault(va)
            vb.mkdir(parents=True)

            init_res = vault_sync.init_sync(str(target), brain_dir=va, device_name="dev-a", state_dir=sa)
            vault_sync.sync_push(brain_dir=va, state_dir=sa)
            vault_sync.join_sync(
                str(target),
                brain_dir=vb,
                device_name="dev-b",
                recovery_key_b64=init_res["recovery_key_b64"],
                state_dir=sb,
            )
            vault_sync.sync_pull(brain_dir=vb, state_dir=sb)

            # 1) Clean fast-forward update on 2-atoms/alpha-fact.md -> Device B should backup old copy into .history/
            atom_a = va / "2-atoms" / "alpha-fact.md"
            atom_a.write_text(atom_a.read_text(encoding="utf-8") + "\nUpdated cleanly on Device A.\n", encoding="utf-8")
            vault_sync.sync_push(brain_dir=va, state_dir=sa)
            ff_pull = vault_sync.sync_pull(brain_dir=vb, state_dir=sb)
            self.assertEqual(ff_pull["updated"], 1)
            self.assertEqual(ff_pull["conflict_count"], 0)
            history_files = list((vb / "2-atoms" / ".history").glob("alpha-fact.sync-*.md"))
            self.assertTrue(history_files, "Expected .history/ backup before fast-forward overwrite of 2-atoms/")

            # 2) Divergent edits on BOTH devices:
            #    - Both append to decision-record.jsonl (must set-union merge!)
            #    - Both edit notes/shared-note.md (must preserve local & quarantine remote!)
            rec_a = va / "0-raw" / "archive" / "decision-record.jsonl"
            rec_b = vb / "0-raw" / "archive" / "decision-record.jsonl"
            rec_a.write_text(
                rec_a.read_text(encoding="utf-8")
                + json.dumps({"timestamp": "2026-11-19T01:00:00Z", "actor": "dev-a", "action": "approve"})
                + "\n",
                encoding="utf-8",
            )
            rec_b.write_text(
                rec_b.read_text(encoding="utf-8")
                + json.dumps({"timestamp": "2026-11-19T01:05:00Z", "actor": "dev-b", "action": "demote"})
                + "\n",
                encoding="utf-8",
            )
            (va / "notes" / "shared-note.md").write_text("# Shared Note\nEdited on Device A\n", encoding="utf-8")
            (vb / "notes" / "shared-note.md").write_text("# Shared Note\nEdited on Device B\n", encoding="utf-8")

            vault_sync.sync_push(brain_dir=va, state_dir=sa)

            # Device B pushing without pulling first MUST fail with SyncConflictError!
            with self.assertRaises(vault_sync.SyncConflictError):
                vault_sync.sync_push(brain_dir=vb, state_dir=sb)

            # Device B pulls: lossless JSONL merge + quarantined conflict copy + local note preserved!
            merge_res = vault_sync.sync_pull(brain_dir=vb, state_dir=sb)
            self.assertEqual(merge_res["merged_jsonl"], 1)
            self.assertEqual(merge_res["conflict_count"], 1)
            self.assertIn("Edited on Device B", (vb / "notes" / "shared-note.md").read_text(encoding="utf-8"))

            merged_lines = [ln for ln in rec_b.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertEqual(len(merged_lines), 3)
            self.assertTrue(any('"dev-a"' in ln for ln in merged_lines))
            self.assertTrue(any('"dev-b"' in ln for ln in merged_lines))

            conflicts = vault_sync.list_sync_conflicts(brain_dir=vb)
            self.assertEqual(len(conflicts), 1)
            conflict_copy = vb / conflicts[0]["conflict_copy"]
            self.assertTrue(conflict_copy.is_file())
            self.assertIn("Edited on Device A", conflict_copy.read_text(encoding="utf-8"))

    def test_cryptographic_tamper_aad_signature_rollback_and_path_traversal_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            va, vb = root / "va", root / "vb"
            sa, sb = root / "sa", root / "sb"
            target = root / "sync"
            _seed_sample_vault(va)
            vb.mkdir(parents=True)

            init_res = vault_sync.init_sync(str(target), brain_dir=va, state_dir=sa)
            vault_sync.sync_push(brain_dir=va, state_dir=sa)
            head_e1_bytes = (target / "head.json").read_bytes()

            vault_sync.join_sync(
                str(target),
                brain_dir=vb,
                recovery_key_b64=init_res["recovery_key_b64"],
                state_dir=sb,
            )
            vault_sync.sync_pull(brain_dir=vb, state_dir=sb)

            # Push epoch 2
            (va / "notes" / "second-note.md").write_text("# Second\nBody.\n", encoding="utf-8")
            vault_sync.sync_push(brain_dir=va, state_dir=sa)
            vault_sync.sync_pull(brain_dir=vb, state_dir=sb)
            head_e2_bytes = (target / "head.json").read_bytes()

            # 1) Rollback attack: replace head.json with old valid epoch 1 head
            (target / "head.json").write_bytes(head_e1_bytes)
            with self.assertRaises(vault_sync.SyncSecurityError):
                vault_sync.sync_pull(brain_dir=vb, state_dir=sb)
            (target / "head.json").write_bytes(head_e2_bytes)

            # 2) Signature forgery: tamper epoch in head.json without valid Ed25519 signature
            forged = json.loads(head_e2_bytes.decode("utf-8"))
            forged["epoch"] = 999
            (target / "head.json").write_text(json.dumps(forged), encoding="utf-8")
            with self.assertRaises(vault_sync.SyncSecurityError):
                vault_sync.sync_pull(brain_dir=vb, state_dir=sb)
            (target / "head.json").write_bytes(head_e2_bytes)

            # 3) Ciphertext bit-flip & AAD swap attack
            enc_blobs = sorted((target / "objects").rglob("*.enc"))
            self.assertGreaterEqual(len(enc_blobs), 2)
            orig_b0 = enc_blobs[0].read_bytes()
            orig_b1 = enc_blobs[1].read_bytes()

            # Flip last byte (GCM auth tag)
            tampered = bytearray(orig_b0)
            tampered[-1] ^= 0xFF
            enc_blobs[0].write_bytes(bytes(tampered))
            ver_tampered = vault_sync.sync_verify(brain_dir=vb, state_dir=sb)
            self.assertFalse(ver_tampered["valid"])
            self.assertTrue(ver_tampered["issues"])

            # Swap valid blob1 ciphertext into blob0 path -> AAD mismatch MUST fail!
            enc_blobs[0].write_bytes(orig_b1)
            ver_swapped = vault_sync.sync_verify(brain_dir=vb, state_dir=sb)
            self.assertFalse(ver_swapped["valid"])
            self.assertTrue(ver_swapped["issues"])
            enc_blobs[0].write_bytes(orig_b0)

            # 4) Path validation blocks traversal, sidecar, chroma, sync-conflicts, and reserved names
            for bad_rel in (
                "../escape.md",
                "/etc/passwd",
                ".sidecar/lexical.sqlite3",
                ".chroma/chroma.sqlite3",
                "0-raw/sync-conflicts/evil.md",
                "2-atoms/CON.md",
                "unknown-dir/file.md",
            ):
                with self.assertRaises(vault_sync.SyncSecurityError, msg=f"Should reject {bad_rel!r}"):
                    vault_sync.validate_sync_rel_path(bad_rel)

    def test_http_object_backend_mock_https_and_security_boundaries(self) -> None:
        store: dict[str, bytes] = {}
        seen_auth: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auth.append(request.headers.get("Authorization", ""))
            if request.url.path.endswith("/redirect-test"):
                return httpx.Response(302, headers={"Location": "https://evil.example/pwn"})
            key = request.url.path.lstrip("/")
            if request.method == "PUT":
                store[key] = request.content
                return httpx.Response(200)
            if request.method == "GET":
                if key not in store:
                    return httpx.Response(404)
                return httpx.Response(200, content=store[key])
            return httpx.Response(405)

        mock_transport = httpx.MockTransport(handler)
        prev_tok = os.environ.get("TEST_SB_SYNC_BEARER")
        os.environ["TEST_SB_SYNC_BEARER"] = "mock-object-store-token-xyz"
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                va, vb = root / "va", root / "vb"
                sa, sb = root / "sa", root / "sb"
                _seed_sample_vault(va)
                vb.mkdir(parents=True)

                # Plain HTTP must be rejected fail-closed!
                with self.assertRaises(Exception):
                    vault_sync.HttpObjectSyncBackend(
                        "http://sync.example.com/bucket",
                        http_transport=mock_transport,
                        skip_dns_check_for_mock_transport=True,
                    )

                init_res = vault_sync.init_sync(
                    "https://sync.example.com/vault1",
                    backend_type="http_object",
                    brain_dir=va,
                    bearer_token_env="TEST_SB_SYNC_BEARER",
                    state_dir=sa,
                    http_transport=mock_transport,
                    skip_dns_check_for_mock_transport=True,
                )
                vault_sync.sync_push(
                    brain_dir=va,
                    state_dir=sa,
                    http_transport=mock_transport,
                    skip_dns_check_for_mock_transport=True,
                )
                vault_sync.join_sync(
                    "https://sync.example.com/vault1",
                    backend_type="http_object",
                    brain_dir=vb,
                    recovery_key_b64=init_res["recovery_key_b64"],
                    bearer_token_env="TEST_SB_SYNC_BEARER",
                    state_dir=sb,
                    http_transport=mock_transport,
                    skip_dns_check_for_mock_transport=True,
                )
                pulled = vault_sync.sync_pull(
                    brain_dir=vb,
                    state_dir=sb,
                    http_transport=mock_transport,
                    skip_dns_check_for_mock_transport=True,
                )
                self.assertGreaterEqual(pulled["added"], 5)
                self.assertTrue(all(h == "Bearer mock-object-store-token-xyz" for h in seen_auth if h))

                # 302 redirects must fail closed with SyncSecurityError!
                backend = vault_sync.HttpObjectSyncBackend(
                    "https://sync.example.com",
                    bearer_token_env="TEST_SB_SYNC_BEARER",
                    http_transport=mock_transport,
                    skip_dns_check_for_mock_transport=True,
                )
                with self.assertRaises(vault_sync.SyncSecurityError):
                    backend.get_object("redirect-test")
        finally:
            if prev_tok is None:
                os.environ.pop("TEST_SB_SYNC_BEARER", None)
            else:
                os.environ["TEST_SB_SYNC_BEARER"] = prev_tok

    def test_loopback_api_sync_routes_sdk_owner_gating_and_cli(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            brain_dir = root / "brain"
            state_dir = root / "sync_state"
            sync_target = root / "sync_target"
            _seed_sample_vault(brain_dir)

            agent_token = "a" * 32
            owner_token = "o" * 32
            sec_dir = root / "sec"
            sec_dir.mkdir(parents=True, exist_ok=True)
            tok_path = sec_dir / "memory-api-token"
            own_path = sec_dir / "owner-token"
            tok_path.write_text(agent_token + "\n", encoding="utf-8")
            own_path.write_text(owner_token + "\n", encoding="utf-8")
            rt = memory_api.MemoryApiRuntimeConfig(
                brain_dir=str(brain_dir),
                atoms_dir=str(brain_dir / "2-atoms"),
                playbooks_dir=str(brain_dir / "5-playbooks"),
                experiences_dir=str(brain_dir / "4-experiences"),
                lexical_index_path=str(brain_dir / ".sidecar" / "lexical.sqlite3"),
                chroma_path=str(brain_dir / ".chroma"),
                telemetry_db_path=str(brain_dir / ".sidecar" / "telemetry.sqlite3"),
                token_path=tok_path,
                owner_token_path=own_path,
            )
            rt.sync_state_dir = state_dir
            server = memory_api.create_memory_api_server("127.0.0.1", 0, runtime_config=rt)
            port = server.server_address[1]
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                agent_client = memory_api.MemoryApiClient(host="127.0.0.1", port=port, token=agent_token)
                owner_client = memory_api.MemoryApiClient(
                    host="127.0.0.1", port=port, token=agent_token, owner_token=owner_token
                )

                # Agent without X-Owner-Token MUST receive 403 Forbidden on all /v1/sync/* routes
                for fn in (
                    lambda: agent_client.sync_status(),
                    lambda: agent_client.sync_init(str(sync_target)),
                    lambda: agent_client.sync_push(),
                    lambda: agent_client.sync_pull(),
                    lambda: agent_client.sync_verify(),
                ):
                    with self.assertRaises(httpx.HTTPStatusError) as ctx:
                        fn()
                    self.assertEqual(ctx.exception.response.status_code, 403)

                # Owner client succeeds end-to-end
                st0 = owner_client.sync_status()
                self.assertFalse(st0["configured"])

                init_r = owner_client.sync_init(str(sync_target), device_name="api-owner")
                self.assertEqual(init_r["status"], "initialized")

                push_r = owner_client.sync_push()
                self.assertEqual(push_r["status"], "pushed")
                self.assertEqual(push_r["epoch"], 1)

                ver_r = owner_client.sync_verify()
                self.assertTrue(ver_r["valid"])

                conf_r = owner_client.sync_conflicts()
                self.assertEqual(conf_r["count"], 0)

                # CLI passthrough check
                env = {**os.environ, "BRAIN_VAULT_DIR": str(brain_dir), "BRAIN_SECURITY_STATE_DIR": str(state_dir)}
                proc = subprocess.run(
                    [sys.executable, "-m", "scripts.cli", "sync", "status", "--json"],
                    cwd=str(REPO_ROOT),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                parsed_cli = json.loads(proc.stdout)
                self.assertIn("configured", parsed_cli)
            finally:
                server.shutdown()
                server.server_close()

    def test_ui_parity_and_ci_release_pipeline_hygiene(self) -> None:
        # 1. Electron Desktop UI parity
        index_html = (REPO_ROOT / "desktop" / "renderer" / "index.html").read_text(encoding="utf-8")
        app_js = (REPO_ROOT / "desktop" / "renderer" / "app.js").read_text(encoding="utf-8")
        for marker in ("notesTasksCard", "radarEnableNliCheck", "systemSyncCard", "syncPushBtn"):
            self.assertIn(marker, index_html)
        for marker in ("/v1/notes/tasks", "nli=1", "/v1/sync/status", "/v1/sync/push", "/v1/sync/pull", "/v1/sync/verify"):
            self.assertIn(marker, app_js)

        # 2. VS Code Extension parity
        vs_pkg = json.loads((REPO_ROOT / "extensions" / "vscode-second-brain" / "package.json").read_text(encoding="utf-8"))
        vs_cmds = {c["command"] for c in vs_pkg["contributes"]["commands"]}
        for cmd in ("secondBrain.showTasks", "secondBrain.scanContradictionsNli", "secondBrain.syncVault"):
            self.assertIn(cmd, vs_cmds)
        vs_ext = (REPO_ROOT / "extensions" / "vscode-second-brain" / "extension.js").read_text(encoding="utf-8")
        for marker in ("/v1/notes/tasks", "?nli=1", "/v1/sync/status", "/v1/sync/${action}"):
            self.assertIn(marker, vs_ext)

        # 3. Streamlit Dashboard parity
        dash_py = (REPO_ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
        for marker in ("extract_action_items", "enable_nli=radar_enable_nli", "vault_sync_push", "vault_sync_pull"):
            self.assertIn(marker, dash_py)

        # 4. CI / Release workflows & packaging scripts hygiene
        for wf_rel in (".github/workflows/ci.yml", ".github/workflows/release.yml"):
            wf_path = REPO_ROOT / wf_rel
            self.assertTrue(wf_path.is_file(), f"Missing workflow: {wf_rel}")
            wf_text = wf_path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(wf_text)
            self.assertIsInstance(parsed, dict)
            self.assertIn("jobs", parsed)
            self.assertIn("goal4_sync_and_ui_tests", wf_text)

        pkg_ps1 = (REPO_ROOT / "scripts" / "package_desktop.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$SkipSign", pkg_ps1)

        for sh_rel in ("setup.sh", "run_daemon.sh", "run_dashboard.sh", "run_night_shift.sh", "scripts/package_desktop.sh"):
            sh_path = REPO_ROOT / sh_rel
            self.assertTrue(sh_path.is_file())
            self.assertNotIn(b"\r", sh_path.read_bytes(), f"{sh_rel} must be strictly LF-only!")

    def test_unauthenticated_head_rejection_remote_deletions_and_streamed_size_cap(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            va, vb = root / "va", root / "vb"
            sa, sb = root / "sa", root / "sb"
            target = root / "sync"
            _seed_sample_vault(va)
            (va / "notes" / "ephemeral.md").write_text("---\ntitle: Ephemeral\n---\n\nTemp note.\n", encoding="utf-8")
            (va / "notes" / "mod-del.md").write_text("---\ntitle: ModDel\n---\n\nInitial.\n", encoding="utf-8")
            vb.mkdir(parents=True)

            init_res = vault_sync.init_sync(str(target), brain_dir=va, device_name="dev-a", state_dir=sa)
            vault_sync.sync_push(brain_dir=va, state_dir=sa)
            vault_sync.join_sync(
                str(target),
                brain_dir=vb,
                device_name="dev-b",
                recovery_key_b64=init_res["recovery_key_b64"],
                state_dir=sb,
            )
            vault_sync.sync_pull(brain_dir=vb, state_dir=sb)
            self.assertTrue((vb / "notes" / "ephemeral.md").is_file())
            self.assertTrue((vb / "notes" / "mod-del.md").is_file())

            # 1) Unauthenticated remote head.json (valid Ed25519 signature by attacker WITHOUT master_key)
            good_head = (target / "head.json").read_bytes()
            cfg_b_path = vault_sync.sync_config_path(vb, state_dir=sb)
            peers_before = json.loads(cfg_b_path.read_text(encoding="utf-8"))["trusted_peers"]

            att_priv, att_pub = vault_sync._generate_device_keypair()
            fake_head = json.loads(good_head.decode("utf-8"))
            fake_head["device_id"] = "attacker-dev99"
            fake_head["device_pubkey_b64"] = att_pub
            fake_head["epoch"] = 50
            fake_head["signature_b64"] = vault_sync._sign_head(fake_head, att_priv)
            fake_head["head_mac"] = "0" * 64
            (target / "head.json").write_text(json.dumps(fake_head), encoding="utf-8")

            with self.assertRaises(vault_sync.SyncSecurityError):
                vault_sync.sync_pull(brain_dir=vb, state_dir=sb)
            peers_after = json.loads(cfg_b_path.read_text(encoding="utf-8"))["trusted_peers"]
            self.assertEqual(peers_before, peers_after, "Attacker peer MUST NOT be persisted to trusted_peers!")

            # Truncated / malformed head.json missing required fields must also raise SyncSecurityError (never KeyError)
            (target / "head.json").write_text(json.dumps({"vault_id": init_res["vault_id"], "head_mac": "ab" * 32}), encoding="utf-8")
            with self.assertRaises(vault_sync.SyncSecurityError):
                vault_sync.sync_pull(brain_dir=vb, state_dir=sb)
            (target / "head.json").write_bytes(good_head)

            # 2) Remote deletion propagation + modify/delete conflict preservation
            (va / "notes" / "ephemeral.md").unlink()
            (va / "notes" / "mod-del.md").unlink()
            (vb / "notes" / "mod-del.md").write_text("---\ntitle: ModDel\n---\n\nEdited locally on B.\n", encoding="utf-8")

            vault_sync.sync_push(brain_dir=va, state_dir=sa)
            pull_del = vault_sync.sync_pull(brain_dir=vb, state_dir=sb)
            self.assertEqual(pull_del["deleted"], 1)
            self.assertFalse((vb / "notes" / "ephemeral.md").exists(), "Cleanly deleted remote note must be removed on pull!")
            self.assertTrue((vb / "notes" / "mod-del.md").is_file(), "Locally modified file must survive remote deletion!")
            self.assertTrue(any(c.get("conflict_kind") == "modify_delete" for c in pull_del["conflicts"]))

            # Next push from Device B must NOT resurrect ephemeral.md!
            vault_sync.sync_push(brain_dir=vb, state_dir=sb)
            vault_sync.sync_pull(brain_dir=va, state_dir=sa)
            self.assertFalse((va / "notes" / "ephemeral.md").exists())

            # 3) Streamed HttpObjectSyncBackend aborts immediately when streamed chunks exceed max_bytes
            chunks_yielded = 0

            class _OversizedStream(httpx.SyncByteStream):
                def __iter__(self):
                    nonlocal chunks_yielded
                    for _ in range(10):
                        chunks_yielded += 1
                        yield b"X" * 1024

            def stream_handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, stream=_OversizedStream())

            http_backend = vault_sync.HttpObjectSyncBackend(
                "https://sync.example.com/bucket",
                http_transport=httpx.MockTransport(stream_handler),
                skip_dns_check_for_mock_transport=True,
            )
            with self.assertRaises(vault_sync.SyncSecurityError):
                http_backend.get_object("head.json", max_bytes=2048)
            self.assertLessEqual(chunks_yielded, 3, "Stream must abort as soon as max_bytes is exceeded!")


if __name__ == "__main__":
    unittest.main(verbosity=2)
