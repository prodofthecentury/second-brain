# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Regression & security test suite for 1A: Live Bugs, Crashes & Security Gaps in HEAD.

Covers all Python-side and full-stack HTTP API defects (Defects #1, #2, #5, #6, #7, #8,
#11, #12-dashboard, #13, #14). JS/UI/Electron/VS Code/Clipper/PWA defects (#3, #4, #7-JS,
#8-JS, #9, #10, #12-desktop, #15) are covered in their respective Node test suites.

Run:
    uv run python -m unittest scripts/bugfix_1a_regression_tests.py
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
    raise SystemExit(_sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__"))


from contextlib import ExitStack, redirect_stdout
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(ROOT))

from scripts.stage4_executable_skills_tests import _seed_atom, _seed_mature_playbook


def _http_json(
    port: int,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    bearer: str = "",
    owner_token: str = "",
) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers: dict[str, str] = {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if owner_token:
        headers["X-Owner-Token"] = owner_token
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except Exception:
            parsed = {"raw": raw}
        return exc.code, parsed


class Bugfix1AIsolatedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._stack = ExitStack()
        self.addCleanup(self._stack.close)
        self.root_tmp = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self.sec_dir = self.root_tmp / "security"
        self.sec_dir.mkdir(parents=True, exist_ok=True)
        self.brain = self.root_tmp / "brain"
        for sub in (
            "0-raw",
            "0-raw/inbox",
            "0-raw/archive",
            "1-notes",
            "2-atoms",
            "3-threads",
            "4-experiences",
            "5-playbooks",
            "6-skills",
            ".sidecar",
            "sources/generated",
        ):
            (self.brain / sub).mkdir(parents=True, exist_ok=True)
        self._stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "BRAIN_SECURITY_STATE_DIR": str(self.sec_dir),
                    "BRAIN_ALLOW_KEYFILE_FALLBACK": "1",
                },
                clear=False,
            )
        )

    def _start_api_server(self):
        from scripts.memory_api import MemoryApiRuntime, create_memory_api_server

        tok_file = self.sec_dir / "api-token"
        own_file = self.sec_dir / "owner-token"
        agent_tok = "agent_bugfix1a_test_token_1234567890abcdef"
        owner_tok = "owner_bugfix1a_test_token_abcdef1234567890"
        tok_file.write_text(agent_tok + "\n", encoding="utf-8")
        own_file.write_text(owner_tok + "\n", encoding="utf-8")

        rt = MemoryApiRuntime(
            brain_dir=str(self.brain),
            token_path=tok_file,
            owner_token_path=own_file,
            security_dir=self.sec_dir,
        )
        rt.sync_state_dir = self.sec_dir  # type: ignore[attr-defined]
        server = create_memory_api_server("127.0.0.1", 0, rt)
        port = int(server.server_address[1])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def _cleanup() -> None:
            server.shutdown()
            server.server_close()

        self.addCleanup(_cleanup)
        return server, port, agent_tok, owner_tok, rt


class Defect01MobileRecallRotateTests(Bugfix1AIsolatedTestCase):
    """Defect #1: rotate_mobile_recall_key() crashes with TypeError on CLI and HTTP API."""

    def test_rotate_recall_key_direct_cli_and_http_without_vault_id(self) -> None:
        from scripts.inbox_drop import create_mobile_drop_pairing, rotate_mobile_recall_key
        import scripts.vault_sync as vault_sync

        remote_dir = self.root_tmp / "sync_remote"
        remote_dir.mkdir(parents=True, exist_ok=True)
        sync_info = vault_sync.init_sync(
            str(remote_dir),
            brain_dir=str(self.brain),
            state_dir=self.sec_dir,
        )
        pairing = create_mobile_drop_pairing(
            vault_id=str(sync_info["vault_id"]),
            brain_dir=str(self.brain),
            state_dir=self.sec_dir,
            label="Phone Rotate Test",
            include_recall=True,
        )
        first_rk_id = pairing["recall_key_id"]
        self.assertTrue(first_rk_id)

        # 1. Direct call with omitted vault_id must auto-resolve and rotate without TypeError
        rot1 = rotate_mobile_recall_key(
            brain_dir=str(self.brain),
            state_dir=self.sec_dir,
        )
        self.assertEqual(rot1["status"], "rotated")
        self.assertEqual(rot1["vault_id"], str(sync_info["vault_id"]))
        self.assertNotEqual(rot1["recall_key_id"], first_rk_id)

        # 2. CLI `second-brain sync rotate-recall` must succeed without TypeError
        buf = io.StringIO()
        with (
            patch.object(vault_sync, "resolve_active_brain_dir", return_value=str(self.brain)),
            patch.dict(os.environ, {"BRAIN_SECURITY_STATE_DIR": str(self.sec_dir)}),
            redirect_stdout(buf),
        ):
            rc = vault_sync.main(["rotate-recall", "--no-export", "--json"])
        self.assertEqual(rc, 0)
        cli_out = json.loads(buf.getvalue())
        self.assertEqual(cli_out["status"], "rotated")
        self.assertNotEqual(cli_out["recall_key_id"], rot1["recall_key_id"])

        # 3. HTTP POST /v1/sync/inbox-drop/rotate-recall with empty body must succeed (200)
        _, port, agent_tok, owner_tok, _ = self._start_api_server()
        status, data = _http_json(
            port,
            "POST",
            "/v1/sync/inbox-drop/rotate-recall",
            {"export_now": False},
            bearer=agent_tok,
            owner_token=owner_tok,
        )
        self.assertEqual(status, 200, f"Expected 200 on rotate-recall, got {status}: {data}")
        self.assertEqual(data["status"], "rotated")
        self.assertNotEqual(data["recall_key_id"], cli_out["recall_key_id"])


class Defect02SandboxHardeningTests(Bugfix1AIsolatedTestCase):
    """Defect #2: Stage 4 sandbox_python escapes (sqlite3/_winapi/getattr/__builtins__/quota) & OS containment."""

    def test_ast_blocks_sqlite3_winapi_getattr_builtins_and_type_metaclass(self) -> None:
        from scripts.executable_skills import ExecutableSkillValidationError, _validate_sandbox_python_ast

        forbidden_scripts = [
            "import sqlite3\nsqlite3.connect('host.db')\n",
            "import _sqlite3\n",
            "import _winapi\n",
            "import _ctypes\n",
            "x = getattr(__builtins__, 'open')\n",
            "x = __builtins__['eval']\n",
            "x = ().__class__.__base__.__subclasses__()\n",
            "x = type('Evil', (), {})\n",
            "import sys\nf = sys._getframe(0)\n",
        ]
        for script in forbidden_scripts:
            with self.subTest(script=script.strip()):
                with self.assertRaises(ExecutableSkillValidationError):
                    _validate_sandbox_python_ast(script)

    def test_live_disk_quota_kills_runaway_writer_and_reports_os_containment(self) -> None:
        from scripts.executable_skills import (
            ExecutableSkillExecutionDeniedError,
            execute_promoted_skill,
            promote_playbook_to_executable_skill,
        )

        # (a) Valid sandbox_python reports OS containment metadata
        _, _, pb_ok = _seed_mature_playbook(
            self.brain,
            playbook_id="pb-sandbox-ok",
            atom_filename="atom-sandbox-ok.md",
            steps=["Compute bounded summary JSON inside hardened sandbox."],
            citations=3,
        )
        promote_playbook_to_executable_skill(
            skill_id="skill-sandbox-ok",
            playbook_id="pb-sandbox-ok",
            expected_playbook_sha256=str(pb_ok["sha256"]),
            step_actions=[
                {
                    "action_type": "sandbox_python",
                    "script_body": (
                        "import json\n"
                        "from pathlib import Path\n"
                        "Path('ok.json').write_text(json.dumps({'sanitized': True, 'sum': 2 + 2}), encoding='utf-8')\n"
                    ),
                    "output_file": "ok.json",
                }
            ],
            actor="owner",
            brain_dir=str(self.brain),
            security_dir=self.sec_dir,
        )
        ok_res = execute_promoted_skill(
            "skill-sandbox-ok",
            actor="owner",
            brain_dir=str(self.brain),
            security_dir=self.sec_dir,
        )
        self.assertEqual(ok_res["status"], "completed")
        self.assertTrue(ok_res["step_results"][0].get("containment_backend"))

        # (b) Mid-flight disk quota bomb is stopped & rolled back (never writes multi-MB bomb)
        _, _, pb_bomb = _seed_mature_playbook(
            self.brain,
            playbook_id="pb-quota-bomb",
            atom_filename="atom-quota-bomb.md",
            steps=["Attempt oversized write inside sandbox workspace."],
            citations=3,
        )
        promote_playbook_to_executable_skill(
            skill_id="skill-quota-bomb",
            playbook_id="pb-quota-bomb",
            expected_playbook_sha256=str(pb_bomb["sha256"]),
            step_actions=[
                {
                    "action_type": "sandbox_python",
                    "script_body": (
                        "chunk = b'X' * 16384\n"
                        "with open('bomb.bin', 'wb') as fh:\n"
                        "    for _ in range(256):\n"
                        "        fh.write(chunk)\n"
                        "        fh.flush()\n"
                    ),
                    "output_file": "bomb.bin",
                }
            ],
            max_output_bytes=8192,
            timeout_seconds=10.0,
            actor="owner",
            brain_dir=str(self.brain),
            security_dir=self.sec_dir,
        )
        bomb_res = execute_promoted_skill(
            "skill-quota-bomb",
            actor="owner",
            brain_dir=str(self.brain),
            security_dir=self.sec_dir,
        )
        self.assertEqual(bomb_res["status"], "failed_rolled_back")
        self.assertTrue(bomb_res["rollback_executed"])
        bomb_file = self.brain / ".sidecar" / "skill-workspaces" / "skill-quota-bomb" / "bomb.bin"
        self.assertFalse(bomb_file.exists(), "Rolled-back quota bomb artifact must not remain on disk")


class Defect05KeychainStoreTests(Bugfix1AIsolatedTestCase):
    """Defect #5: keychain_store skips macOS/Linux native keychains when state_dir is passed, and misreports keychain_sealed."""

    def test_macos_and_linux_native_keychain_invoked_even_with_custom_state_dir(self) -> None:
        import base64
        import scripts.keychain_store as ks

        custom_state = self.root_tmp / "custom_state_dir"
        custom_state.mkdir(parents=True, exist_ok=True)
        secret_bytes = b"K" * 32
        secret_b64 = base64.b64encode(secret_bytes).decode("ascii")

        # 1. macOS: `security` MUST be invoked with state_dir-scoped service name
        macos_calls: list[tuple[list[str], str]] = []
        orig_isfile = os.path.isfile

        def fake_isfile(p):
            if str(p) == "/usr/bin/security":
                return True
            return orig_isfile(p)

        def fake_macos_run(cmd, **kwargs):
            macos_calls.append((list(cmd), str(kwargs.get("input") or "")))
            if "find-generic-password" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=secret_b64 + "\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with (
            patch.object(ks.sys, "platform", "darwin"),
            patch.object(ks.os.path, "isfile", side_effect=fake_isfile),
            patch.object(ks.subprocess, "run", side_effect=fake_macos_run),
        ):
            res = ks.store_secret(
                "sync_master_key",
                "vault123",
                secret_bytes,
                state_dir=custom_state,
            )
            self.assertEqual(res["backend"], "macos_keychain")
            self.assertTrue(ks.is_native_keychain_backend(res["backend"]))
            loaded = ks.load_secret("sync_master_key", "vault123", state_dir=custom_state)
            self.assertEqual(loaded, secret_bytes)
            self.assertGreaterEqual(len(macos_calls), 2)
            # Service name passed to `/usr/bin/security -i` via stdin must be scoped by custom_state hash
            self.assertIn(".sd-", macos_calls[0][1])

        # 2. Linux: `secret-tool` MUST be invoked even with custom state_dir when D-Bus session is set
        linux_calls: list[list[str]] = []

        def fake_linux_run(cmd, **kwargs):
            linux_calls.append(list(cmd))
            if "lookup" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=secret_b64 + "\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with (
            patch.object(ks.sys, "platform", "linux"),
            patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}),
            patch.object(ks.shutil, "which", return_value="/usr/bin/secret-tool"),
            patch.object(ks.subprocess, "run", side_effect=fake_linux_run),
        ):
            res = ks.store_secret(
                "sync_master_key",
                "vault123",
                secret_bytes,
                state_dir=custom_state,
            )
            self.assertEqual(res["backend"], "linux_secret_service")
            self.assertTrue(ks.is_native_keychain_backend(res["backend"]))
            loaded = ks.load_secret("sync_master_key", "vault123", state_dir=custom_state)
            self.assertEqual(loaded, secret_bytes)
            self.assertTrue(any(".sd-" in arg for arg in linux_calls[0]))

        # 3. Failing native keychain fails closed (KeychainUnavailableError) unless fallback explicitly allowed
        def failing_macos_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 36, stdout="", stderr="Keychain is locked")

        with (
            patch.object(ks.sys, "platform", "darwin"),
            patch.dict(os.environ, {"BRAIN_ALLOW_KEYFILE_FALLBACK": "0"}),
            patch.object(ks.os.path, "isfile", side_effect=fake_isfile),
            patch.object(ks.subprocess, "run", side_effect=failing_macos_run),
        ):
            with self.assertRaises(ks.KeychainUnavailableError):
                ks.store_secret(
                    "sync_master_key",
                    "vault_locked",
                    secret_bytes,
                    state_dir=custom_state,
                )

    def test_fallback_keyfile_is_aead_encrypted_sbkf2_and_vault_sync_reports_keychain_sealed_false(self) -> None:
        import scripts.keychain_store as ks
        import scripts.vault_sync as vault_sync

        fallback_state = self.root_tmp / "headless_linux_state"
        fallback_state.mkdir(parents=True, exist_ok=True)
        marker_secret = b"SUPER_RAW_SYNC_SECRET_DO_NOT_LEAK"

        # Simulate headless Linux (no D-Bus / no secret-tool) -> encrypted SBKF2 fallback
        with (
            patch.object(ks.sys, "platform", "linux"),
            patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "", "BRAIN_ALLOW_KEYFILE_FALLBACK": "1"}),
            patch.object(ks.shutil, "which", return_value=None),
        ):
            res = ks.store_secret("sync_master_key", "v_headless", marker_secret, state_dir=fallback_state)
            self.assertEqual(res["backend"], "posix_restricted_keyfile")
            self.assertFalse(ks.is_native_keychain_backend(res["backend"]))

            # Raw secret bytes MUST NOT appear plaintext on disk!
            key_files = [
                p for p in (fallback_state / "keychain").glob("*.key") if p.name != ".wrap.key"
            ]
            self.assertEqual(len(key_files), 1)
            raw_on_disk = key_files[0].read_bytes()
            self.assertTrue(raw_on_disk.startswith(b"SBKF2\x00"))
            self.assertNotIn(marker_secret, raw_on_disk)

            # Round-trip decrypt works cleanly
            self.assertEqual(
                ks.load_secret("sync_master_key", "v_headless", state_dir=fallback_state),
                marker_secret,
            )

            # vault_sync init_sync + sync_status MUST report keychain_sealed=False for keyfile fallback!
            remote_dir = self.root_tmp / "remote_headless"
            remote_dir.mkdir(parents=True, exist_ok=True)
            init_res = vault_sync.init_sync(
                str(remote_dir),
                brain_dir=str(self.brain),
                state_dir=fallback_state,
                use_keychain=True,
            )
            self.assertEqual(init_res["keychain_backend"], "posix_restricted_keyfile")
            self.assertFalse(init_res["keychain_sealed"])

            st = vault_sync.sync_status(brain_dir=str(self.brain), state_dir=fallback_state)
            self.assertEqual(st["keychain_backend"], "posix_restricted_keyfile")
            self.assertFalse(st["keychain_sealed"])


class Defect06MerkleSnapshotParityTests(Bugfix1AIsolatedTestCase):
    """Defect #6: Merkle root split-brain between vault_ledger and time_travel on 6-skills/."""

    def test_snapshot_and_ledger_merkle_roots_match_with_promoted_skill_and_legacy_compat(self) -> None:
        from scripts.executable_skills import promote_playbook_to_executable_skill
        from scripts.time_travel import create_snapshot, diff_snapshots, verify_snapshot
        from scripts.vault_ledger import compute_vault_merkle

        _, _, pb_res = _seed_mature_playbook(
            self.brain,
            playbook_id="pb-merkle-parity",
            atom_filename="atom-merkle-parity.md",
            steps=["Audit source anchors cleanly."],
            citations=3,
        )

        # Snapshot 1 BEFORE Stage 4 skill promotion
        snap1 = create_snapshot(
            label="before-skill",
            brain_dir=str(self.brain),
            atoms_dir=str(self.brain / "2-atoms"),
        )
        ledger1 = compute_vault_merkle(brain_dir=str(self.brain), atoms_dir=str(self.brain / "2-atoms"))
        self.assertEqual(snap1["merkle_root"], ledger1["merkle_root"])
        self.assertEqual(snap1["skill_count"], 0)

        # Promote Stage 4 skill into 6-skills/
        promote_playbook_to_executable_skill(
            skill_id="skill-merkle-parity",
            playbook_id="pb-merkle-parity",
            expected_playbook_sha256=str(pb_res["sha256"]),
            step_actions=[{"action_type": "vault_audit", "audit_kind": "sources", "output_file": "a.json"}],
            actor="owner",
            brain_dir=str(self.brain),
            security_dir=self.sec_dir,
        )

        # Snapshot 2 AFTER Stage 4 skill promotion -> MUST match vault_ledger Merkle root identically!
        snap2 = create_snapshot(
            label="after-skill",
            brain_dir=str(self.brain),
            atoms_dir=str(self.brain / "2-atoms"),
        )
        ledger2 = compute_vault_merkle(brain_dir=str(self.brain), atoms_dir=str(self.brain / "2-atoms"))
        self.assertEqual(snap2["skill_count"], 1)
        self.assertEqual(
            snap2["merkle_root"],
            ledger2["merkle_root"],
            "time_travel snapshot Merkle root diverged from vault_ledger on 6-skills/!",
        )
        self.assertNotEqual(snap1["merkle_root"], snap2["merkle_root"])

        ver2 = verify_snapshot(snap2["snapshot_id"], brain_dir=str(self.brain))
        self.assertTrue(ver2["valid"], f"verify_snapshot failed: {ver2}")
        self.assertEqual(ver2["skill_count"], 1)

        diff = diff_snapshots(snap1["snapshot_id"], snap2["snapshot_id"], brain_dir=str(self.brain))
        added_skill_files = [item["filename"] for item in diff["skill_changes"]["added"]]
        self.assertEqual(added_skill_files, ["skill-merkle-parity.md"])

        # Legacy snapshot without `skill_refs` field still verifies cleanly
        legacy_path = self.brain / "0-raw" / "archive" / "snapshots" / f"{snap1['snapshot_id']}.json"
        legacy_doc = json.loads(legacy_path.read_text(encoding="utf-8"))
        legacy_doc.pop("skill_refs", None)
        legacy_doc.pop("skill_count", None)
        legacy_path.write_text(json.dumps(legacy_doc, indent=2), encoding="utf-8")
        ver_legacy = verify_snapshot(snap1["snapshot_id"], brain_dir=str(self.brain))
        self.assertTrue(ver_legacy["valid"], f"Legacy snapshot backward-compat failed: {ver_legacy}")


class Defect07SkillExecutionsSyncMergeTests(Bugfix1AIsolatedTestCase):
    """Defect #7: 0-raw/archive/skill-executions.jsonl omitted from E2EE sync append-only merge."""

    def test_skill_executions_jsonl_merges_valid_hash_chain_and_rejects_tampering(self) -> None:
        from scripts.executable_skills import skill_execution_log_path, verify_skill_execution_audit_chain
        from scripts.vault_sync import _APPEND_ONLY_JSONL_PATHS, SyncSecurityError, _merge_append_only_jsonl

        self.assertIn("0-raw/archive/skill-executions.jsonl", _APPEND_ONLY_JSONL_PATHS)

        def _make_chain(entries: list[dict]) -> bytes:
            prev = "0" * 64
            out_lines: list[str] = []
            for idx, entry in enumerate(entries, start=1):
                rec = dict(entry)
                rec["execution_index"] = idx
                rec["prev_execution_sha256"] = prev
                rec.pop("execution_sha256", None)
                canonical = json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                digest = hashlib.sha256(canonical).hexdigest()
                rec["execution_sha256"] = digest
                prev = digest
                out_lines.append(json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
            return ("\n".join(out_lines) + "\n").encode("utf-8")

        shared = {
            "schema_version": 1,
            "execution_id": "exec-shared-001",
            "skill_id": "sk-a",
            "started_at": "2026-11-20T09:00:00Z",
            "status": "completed",
        }
        dev_a = {
            "schema_version": 1,
            "execution_id": "exec-dev-a-002",
            "skill_id": "sk-a",
            "started_at": "2026-11-20T09:05:00Z",
            "status": "completed",
        }
        dev_b = {
            "schema_version": 1,
            "execution_id": "exec-dev-b-003",
            "skill_id": "sk-a",
            "started_at": "2026-11-20T09:02:00Z",
            "status": "completed",
        }

        local_bytes = _make_chain([shared, dev_a])
        remote_bytes = _make_chain([shared, dev_b])
        merged = _merge_append_only_jsonl(
            local_bytes,
            remote_bytes,
            rel_path="0-raw/archive/skill-executions.jsonl",
        )
        log_path = Path(skill_execution_log_path(str(self.brain)))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_bytes(merged)

        chain_check = verify_skill_execution_audit_chain(str(self.brain))
        self.assertTrue(chain_check["valid"], f"Merged skill-executions chain invalid: {chain_check}")
        self.assertEqual(chain_check["entry_count"], 3)

        # Tampered remote chain must fail closed with SyncSecurityError
        tampered = remote_bytes.replace(b'"completed"', b'"forged"')
        with self.assertRaises(SyncSecurityError):
            _merge_append_only_jsonl(
                local_bytes,
                tampered,
                rel_path="0-raw/archive/skill-executions.jsonl",
            )


class Defect08ClipperEphemeralPortTests(Bugfix1AIsolatedTestCase):
    """Defect #8: Browser Web Clipper 1-click pairing codes encode Desktop ephemeral port."""

    def test_clipper_pair_code_embeds_bound_port_and_redeems_cleanly(self) -> None:
        _, port, agent_tok, owner_tok, _ = self._start_api_server()
        self.assertNotEqual(port, 8765)

        st, created = _http_json(
            port,
            "POST",
            "/v1/clipper/pair-code",
            {"label": "Desktop Ephemeral Clipper", "ttl_seconds": 300},
            bearer=agent_tok,
            owner_token=owner_tok,
        )
        self.assertEqual(st, 201, f"Expected 201 from pair-code, got {st}: {created}")
        code = str(created.get("code") or "")
        self.assertTrue(code.endswith(f"-{port}"), f"Pairing code must embed bound port {port}: {code}")
        self.assertEqual(created.get("port"), port)

        # Redeem the port-suffixed code over loopback
        st2, redeemed = _http_json(
            port,
            "POST",
            "/v1/clipper/redeem",
            {
                "code": code,
                "origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
                "label": "Chrome Test",
            },
        )
        self.assertEqual(st2, 201, f"Redeem failed ({st2}): {redeemed}")
        self.assertTrue(str(redeemed.get("token") or "").startswith("clipper_"))


class Defect11MobileExportDirAndDashboardSchemaTests(Bugfix1AIsolatedTestCase):
    """Defects #11 & #12 (dashboard): Mobile export writes outside app bundles & dashboard schema checks."""

    def test_mobile_export_empty_out_dir_writes_to_user_state_outside_repo_dist(self) -> None:
        _, port, agent_tok, owner_tok, _ = self._start_api_server()

        st, exported = _http_json(
            port,
            "POST",
            "/v1/mobile/export",
            {"out_dir": ""},
            bearer=agent_tok,
            owner_token=owner_tok,
        )
        self.assertIn(st, (200, 201), f"Expected 200/201 from /v1/mobile/export, got {st}: {exported}")
        zip_path = Path(str(exported["zip_path"])).resolve()
        self.assertTrue(zip_path.is_file())
        repo_dist = (ROOT / "dist").resolve()
        self.assertFalse(
            zip_path.is_relative_to(repo_dist),
            f"Export must NOT write into repo/app bundle dist/: {zip_path}",
        )
        expected_dir = (self.sec_dir.parent / "exports" / "mobile-vault-client").resolve()
        self.assertTrue(
            zip_path.is_relative_to(expected_dir),
            f"Expected export inside {expected_dir}, got {zip_path}",
        )

    def test_dashboard_uses_pwa_pairing_url_user_export_dir_and_guards_video_backends(self) -> None:
        dash_src = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
        self.assertIn('mres.get("pwa_pairing_url")', dash_src)
        self.assertNotIn('export_mobile_vault_client_bundle(os.path.join(PROJECT_ROOT, "dist"))', dash_src)
        self.assertIn('{"sd_webui_local", "diffusers_local"}', dash_src)


class Defect13InvalidNumericFieldValidationTests(Bugfix1AIsolatedTestCase):
    """Defect #13: Invalid numeric JSON fields return clean HTTP 422 (never HTTP 500)."""

    def test_invalid_numeric_fields_return_422_never_500(self) -> None:
        _, port, agent_tok, owner_tok, _ = self._start_api_server()
        _, _, pb_res = _seed_mature_playbook(
            self.brain,
            playbook_id="pb-num-test",
            atom_filename="atom-num-test.md",
            steps=["Validate numeric input boundaries."],
            citations=3,
        )

        cases: list[tuple[str, dict]] = [
            (
                "/v1/skills/promote",
                {
                    "skill_id": "num-bad-1",
                    "playbook_id": "pb-num-test",
                    "expected_playbook_sha256": str(pb_res["sha256"]),
                    "step_actions": [{"action_type": "vault_audit", "audit_kind": "sources", "output_file": "a.json"}],
                    "timeout_seconds": None,
                    "actor": "owner",
                },
            ),
            (
                "/v1/skills/promote",
                {
                    "skill_id": "num-bad-2",
                    "playbook_id": "pb-num-test",
                    "expected_playbook_sha256": str(pb_res["sha256"]),
                    "step_actions": [{"action_type": "vault_audit", "audit_kind": "sources", "output_file": "a.json"}],
                    "timeout_seconds": "NaN",
                    "max_output_bytes": True,
                    "actor": "owner",
                },
            ),
            (
                "/v1/media/image",
                {"prompt": "test", "width": None, "height": 512},
            ),
            (
                "/v1/media/image",
                {"prompt": "test", "seed": "not-an-int", "width": 512, "height": 512},
            ),
            (
                "/v1/media/video",
                {"prompt": "test", "frames": True, "fps": "abc"},
            ),
            (
                "/v1/clipper/pair-code",
                {"label": "test", "ttl_seconds": "abc"},
            ),
            (
                "/v1/graph/auto-link",
                {"max_links_per_atom": "not-a-num"},
            ),
            (
                "/v1/snapshots/recall",
                {"snapshot_id": "snap-missing", "query": "test", "limit": "xyz"},
            ),
            (
                "/v1/sync/auto",
                {"enabled": False, "interval_seconds": "bad-int"},
            ),
            (
                "/v1/vaults/federated-recall",
                {"query": "test", "limit": "bad-int"},
            ),
            (
                "/v1/git/distill",
                {"max_commits": "bad-int"},
            ),
        ]

        for endpoint, payload in cases:
            with self.subTest(endpoint=endpoint, payload=payload):
                status, data = _http_json(
                    port,
                    "POST",
                    endpoint,
                    payload,
                    bearer=agent_tok,
                    owner_token=owner_tok,
                )
                self.assertEqual(
                    status,
                    422,
                    f"Expected HTTP 422 for invalid numeric input on {endpoint}, got {status}: {data}",
                )


class Defect14OptionalImportsMinimalWheelTests(unittest.TestCase):
    """Defect #14: publish_registries, ollama_refinery, ollama_scout import cleanly without optional deps."""

    def test_modules_import_without_httpx_httpcore_or_ollama_and_stdlib_client_works(self) -> None:
        # 1. publish_registries & ollama_scout must import cleanly with httpx, httpcore & ollama all absent
        probe_no_httpx = r"""
import sys

BLOCKED = {"httpx", "httpcore", "ollama"}

class _Blocker:
    def find_spec(self, fullname, path=None, target=None):
        top = fullname.split(".", 1)[0]
        if top in BLOCKED:
            raise ModuleNotFoundError(f"Blocked optional dependency: {fullname}")
        return None

sys.meta_path.insert(0, _Blocker())
sys.path.insert(0, r"__ROOT__")
sys.path.insert(0, r"__SCRIPTS__")

import publish_registries
import ollama_scout

assert publish_registries.httpx is None
assert ollama_scout.HAS_HTTPX is False

# Instantiating scout SSRF transport without httpx raises clean RuntimeError
try:
    ollama_scout.PinnedHTTPTransport(None)
    raise AssertionError("Expected RuntimeError when httpx is missing")
except RuntimeError as exc:
    assert "httpx" in str(exc).lower()

print("NO_HTTPX_PROBE_OK")
""".replace("__ROOT__", str(ROOT)).replace("__SCRIPTS__", str(SCRIPTS_DIR))

        proc = subprocess.run(
            [sys.executable, "-I", "-c", probe_no_httpx],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"No-httpx import probe failed (rc={proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        self.assertIn("NO_HTTPX_PROBE_OK", proc.stdout)

        # 2. ollama_refinery must import cleanly in minimal wheel installs where optional `ollama` is absent
        probe_no_ollama = r"""
import sys

class _OllamaBlocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] == "ollama":
            raise ModuleNotFoundError(f"Blocked optional dependency: {fullname}")
        return None

sys.meta_path.insert(0, _OllamaBlocker())
sys.path.insert(0, r"__ROOT__")
sys.path.insert(0, r"__SCRIPTS__")

import ollama_refinery

assert ollama_refinery.ollama is None
print("NO_OLLAMA_PROBE_OK")
""".replace("__ROOT__", str(ROOT)).replace("__SCRIPTS__", str(SCRIPTS_DIR))

        proc2 = subprocess.run(
            [sys.executable, "-I", "-c", probe_no_ollama],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(
            proc2.returncode,
            0,
            f"Minimal-wheel (no ollama) import probe failed (rc={proc2.returncode}):\nSTDOUT:\n{proc2.stdout}\nSTDERR:\n{proc2.stderr}",
        )
        self.assertIn("NO_OLLAMA_PROBE_OK", proc2.stdout)

        # Verify publish_registries._StdlibHttpClient works against a real loopback HTTP server
        import scripts.publish_registries as publish_registries

        class _EchoHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                payload = json.dumps({"path": self.path, "len": len(raw)}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):
                return

        srv = http.server.HTTPServer(("127.0.0.1", 0), _EchoHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with publish_registries._StdlibHttpClient(timeout=5.0) as client:
                resp = client.post(
                    f"http://127.0.0.1:{srv.server_address[1]}/echo",
                    json={"ok": True},
                )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["path"], "/echo")
        finally:
            srv.shutdown()
            srv.server_close()

        # Verify .github/workflows/release.yml syncs dependencies in publish-release
        rel_yml = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("publish-release:", rel_yml)
        pub_section = rel_yml.split("publish-release:", 1)[1]
        self.assertIn("uv sync --locked --no-dev", pub_section)


if __name__ == "__main__":
    unittest.main(verbosity=2)
