# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Behavioral & security regression suite for v0.3.0 Milestones 2 & 3:
- Local-First Browser Web Clipper (MV3 extension, scoped write-only tokens, strict Origin CORS,
  verifiable SHA-256 web snapshots, automatic DLP/injection quarantine, revocation)
- Multi-Vault Workspaces (vaults.json + vault-path.txt sync, live API hot-switching,
  nested-path rejection, per-vault isolated token enforcement, strictly opt-in read-only
  federated cross-vault recall, non-destructive removal, CLI & 3-surface UI parity).
"""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from api_product import ensure_owner_token  # noqa: E402
from atom_mutator import apply_review_transition, content_sha256  # noqa: E402
from evidence import find_evidence_anchor  # noqa: E402
from lexical_store import rebuild_lexical_index  # noqa: E402
from memory_api import (  # noqa: E402
    MemoryApiClient,
    MemoryApiRuntimeConfig,
    MemoryLoopbackHTTPServer,
    generate_memory_api_token,
    reset_api_rate_limiter,
)
from notes_store import NotePaths, get_note, verify_note_excerpt  # noqa: E402
import cli  # noqa: E402
import web_clipper  # noqa: E402


CHROME_ORIGIN_A = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CHROME_ORIGIN_B = "chrome-extension://bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
FIREFOX_ORIGIN = "moz-extension://12345678-1234-4123-8123-123456789abc"


def _seed_approved_atom(
    brain: Path,
    *,
    filename: str,
    claim: str,
    project: str = "project:alpha",
    epistemic_label: str = "claim",
    approve: bool = True,
) -> Path:
    (brain / "0-raw" / "archive").mkdir(parents=True, exist_ok=True)
    (brain / "2-atoms").mkdir(parents=True, exist_ok=True)
    (brain / ".sidecar").mkdir(parents=True, exist_ok=True)
    src_name = f"src-{Path(filename).stem}.md"
    src_path = brain / "0-raw" / src_name
    src_path.write_text(f"Primary evidence: {claim}\n", encoding="utf-8")
    atom_path = brain / "2-atoms" / filename
    draft = (
        f"---\nid: {Path(filename).stem}\ntype: atom\nmemory_type: semantic\n"
        f"epistemic_label: {epistemic_label}\n"
        f"sources:\n  - 0-raw/{src_name}\n"
        f"contexts:\n  - {project}\n"
        f"claim_state: candidate\nreview_status: pending\ncitation_integrity: anchored\n---\n\n"
        f"## Claim\n{claim}\n\n## Evidence\n{claim}\n"
    )
    anc = find_evidence_anchor(draft, str(brain))
    parts = draft.split("---", 2)
    fm_full = parts[1] + "".join(f"{k}: {v}\n" for k, v in anc.items())
    atom_path.write_bytes(f"---{fm_full}---{parts[2]}".encode("utf-8"))
    if approve:
        apply_review_transition(
            action="approve",
            atom_path=str(atom_path),
            expected_sha256=content_sha256(atom_path.read_bytes()),
            actor="owner",
            brain_dir=str(brain),
            atoms_dir=str(brain / "2-atoms"),
        )
    else:
        rebuild_lexical_index(str(brain / "2-atoms"), str(brain / ".sidecar" / "lexical.sqlite3"))
    return atom_path


class BrowserWebClipperTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_api_rate_limiter()

    def test_origin_validation_dlp_quarantine_verifiable_snapshot_and_revocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sec_dir = tmp_path / "security"
            sec_dir.mkdir()
            brain = tmp_path / "brain"
            (brain / "2-atoms").mkdir(parents=True)
            (brain / "0-raw" / "inbox").mkdir(parents=True)
            (brain / ".sidecar").mkdir(parents=True)
            rebuild_lexical_index(str(brain / "2-atoms"), str(brain / ".sidecar" / "lexical.sqlite3"))

            # Reject bad extension origins
            for bad_origin in (
                "https://evil.example",
                "http://127.0.0.1:8765",
                "chrome-extension://short",
                "chrome-extension://zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz",
                "null",
                "*",
            ):
                with self.assertRaises(web_clipper.ClipperError):
                    web_clipper.create_clipper_pairing(bad_origin, security_dir=sec_dir)

            # Pair valid Chrome + Firefox origins and verify plaintext token is NOT stored on disk
            pair_a = web_clipper.create_clipper_pairing(CHROME_ORIGIN_A, label="Chrome Dev", security_dir=sec_dir)
            pair_ff = web_clipper.create_clipper_pairing(FIREFOX_ORIGIN, label="Firefox Dev", security_dir=sec_dir)
            self.assertTrue(str(pair_a["token"]).startswith("clipper_"))
            raw_registry = (sec_dir / "clipper-pairings.json").read_text(encoding="utf-8")
            self.assertNotIn(str(pair_a["token"]), raw_registry)
            self.assertNotIn(str(pair_ff["token"]), raw_registry)

            # Start loopback API server bound strictly to 127.0.0.1
            tok_path = sec_dir / "memory-api-token"
            own_path = sec_dir / "owner-token"
            agent_tok, _ = generate_memory_api_token(tok_path)
            owner_tok = ensure_owner_token(own_path)
            rt = MemoryApiRuntimeConfig(
                brain_dir=str(brain),
                token_path=tok_path,
                owner_token_path=own_path,
                policy_path=sec_dir / "policy.json",
                security_dir=sec_dir,
            )
            server = MemoryLoopbackHTTPServer("127.0.0.1", 0, rt)
            port = server.server_address[1]
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                base = f"http://127.0.0.1:{port}"
                with httpx.Client(trust_env=False, timeout=10.0) as client:
                    # 1. OPTIONS preflight succeeds ONLY for paired extension origin on /v1/clipper/*
                    r_opt_ok = client.options(
                        f"{base}/v1/clipper/clip",
                        headers={"Host": f"127.0.0.1:{port}", "Origin": CHROME_ORIGIN_A},
                    )
                    self.assertEqual(r_opt_ok.status_code, 204)
                    self.assertEqual(r_opt_ok.headers.get("access-control-allow-origin"), CHROME_ORIGIN_A)

                    r_opt_bad_origin = client.options(
                        f"{base}/v1/clipper/clip",
                        headers={"Host": f"127.0.0.1:{port}", "Origin": CHROME_ORIGIN_B},
                    )
                    self.assertEqual(r_opt_bad_origin.status_code, 403)

                    r_opt_bad_path = client.options(
                        f"{base}/v1/atoms",
                        headers={"Host": f"127.0.0.1:{port}", "Origin": CHROME_ORIGIN_A},
                    )
                    self.assertEqual(r_opt_bad_path.status_code, 403)

                    # 2. Extension origin presenting full agent token is rejected (must use scoped clipper_* token)
                    r_agent_from_ext = client.get(
                        f"{base}/v1/clipper/status",
                        headers={
                            "Host": f"127.0.0.1:{port}",
                            "Origin": CHROME_ORIGIN_A,
                            "Authorization": f"Bearer {agent_tok}",
                        },
                    )
                    self.assertEqual(r_agent_from_ext.status_code, 401)

                    # 3. Scoped clipper token CANNOT read private atoms, notes, recall, or vaults (write-only!)
                    clip_headers = {
                        "Host": f"127.0.0.1:{port}",
                        "Origin": CHROME_ORIGIN_A,
                        "Authorization": f"Bearer {pair_a['token']}",
                    }
                    self.assertEqual(client.get(f"{base}/v1/atoms", headers=clip_headers).status_code, 403)
                    self.assertEqual(client.get(f"{base}/v1/notes", headers=clip_headers).status_code, 403)
                    self.assertEqual(client.get(f"{base}/v1/vaults", headers=clip_headers).status_code, 403)
                    self.assertEqual(
                        client.post(f"{base}/v1/recall", headers=clip_headers, json={"query": "secret"}).status_code,
                        403,
                    )

                    # 4. Token minted for Origin A fails if presented from Origin B (cross-extension isolation)
                    r_wrong_origin = client.get(
                        f"{base}/v1/clipper/status",
                        headers={
                            "Host": f"127.0.0.1:{port}",
                            "Origin": FIREFOX_ORIGIN,
                            "Authorization": f"Bearer {pair_a['token']}",
                        },
                    )
                    self.assertEqual(r_wrong_origin.status_code, 401)

                    # 5. Valid clip creates note + immutable SHA-256 snapshot and promotes via verify_note_excerpt!
                    r_status = client.get(f"{base}/v1/clipper/status", headers=clip_headers)
                    self.assertEqual(r_status.status_code, 200)
                    self.assertTrue(r_status.json()["paired"])

                    quote = "PostgreSQL transactional DDL prevents half-applied schema migrations."
                    r_clip = client.post(
                        f"{base}/v1/clipper/clip",
                        headers=clip_headers,
                        json={
                            "url": "https://docs.example.org/pg-migrations",
                            "title": "PG Migration Safety",
                            "selected_quote": quote,
                            "page_text": f"Overview section.\n{quote}\nFooter notes.",
                            "tags": ["project:alpha", "database"],
                            "destination": "note",
                        },
                    )
                    self.assertEqual(r_clip.status_code, 201)
                    clip_doc = r_clip.json()
                    self.assertEqual(clip_doc["destination"], "note")
                    self.assertFalse(clip_doc["quarantined"])
                    snap_file = brain / str(clip_doc["snapshot_path"])
                    self.assertTrue(snap_file.is_file())
                    self.assertEqual(content_sha256(snap_file.read_bytes()), clip_doc["snapshot_sha256"])

                    # One-click verify the clipped quote into a cryptographically anchored approved atom
                    npaths = NotePaths.for_brain(str(brain))
                    note_rec = get_note(str(clip_doc["filename"]), paths=npaths)
                    promoted = verify_note_excerpt(
                        str(clip_doc["filename"]),
                        quote,
                        actor="owner",
                        expected_note_sha256=note_rec.sha256,
                        paths=npaths,
                    )
                    self.assertEqual(promoted["status"], "verified")

                    # 6. DLP secret quarantine: clip containing credential is diverted to 0-raw/inbox/
                    r_secret = client.post(
                        f"{base}/v1/clipper/clip",
                        headers=clip_headers,
                        json={
                            "url": "https://docs.example.org/leaked",
                            "title": "Leaked Credential Page",
                            "selected_quote": "Use OPENAI_API_KEY=sk-1234567890abcdef1234567890abcdef in prod.",
                            "destination": "note",
                        },
                    )
                    self.assertEqual(r_secret.status_code, 201)
                    sec_doc = r_secret.json()
                    self.assertTrue(sec_doc["quarantined"])
                    self.assertEqual(sec_doc["destination"], "inbox")
                    self.assertTrue((brain / "0-raw" / "inbox" / str(sec_doc["filename"])).is_file())

                    # 7. Reject dangerous URL schemes (javascript:, file://, user:pass@)
                    r_bad_url = client.post(
                        f"{base}/v1/clipper/clip",
                        headers=clip_headers,
                        json={"url": "javascript:alert(1)", "title": "XSS", "selected_quote": "bad quote"},
                    )
                    self.assertEqual(r_bad_url.status_code, 422)

                    # 8. Non-owner agent cannot list/pair/revoke pairings; owner can revoke and token immediately dies
                    sdk_agent = MemoryApiClient(port=port, token=agent_tok)
                    sdk_owner = MemoryApiClient(port=port, token=agent_tok, owner_token=owner_tok)
                    with self.assertRaises(httpx.HTTPStatusError) as cm_ag:
                        sdk_agent.clipper_pairings()
                    self.assertEqual(cm_ag.exception.response.status_code, 403)
                    listed = sdk_owner.clipper_pairings()
                    self.assertEqual(listed["count"], 2)
                    sdk_owner.revoke_clipper_pairing(str(pair_a["pairing_id"]))
                    self.assertEqual(client.get(f"{base}/v1/clipper/status", headers=clip_headers).status_code, 403)
            finally:
                server.shutdown()
                server.server_close()

    def test_browser_clipper_node_suite_and_zip_packaging(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "node must be available to run browser-clipper tests")
        proc = subprocess.run(
            [node, "--test", str(ROOT / "extensions" / "browser-clipper" / "test" / "clipper.test.js")],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"Browser clipper node tests failed:\n{proc.stdout}\n{proc.stderr}")

        from build_clipper import build_clipper_zip
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_clipper_zip(tmp)
            self.assertTrue(bundle.is_file())
            with zipfile.ZipFile(bundle, "r") as zf:
                names = set(zf.namelist())
                self.assertIn("manifest.json", names)
                self.assertIn("background.js", names)
                self.assertIn("popup.js", names)
                self.assertIn("lib/clipper-client.js", names)


class MultiVaultWorkspacesTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_api_rate_limiter()

    def test_multi_vault_creation_hot_switch_isolated_tokens_and_opt_in_federated_recall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sec_dir = tmp_path / "security"
            sec_dir.mkdir()
            vault_default = tmp_path / "vault-default"
            vault_work = tmp_path / "vault-work"
            vault_client = tmp_path / "vault-client"

            # Seed approved atoms in default vault
            _seed_approved_atom(
                vault_default,
                filename="default-rule.md",
                claim="Personal vault requires daily encrypted backups to cold storage.",
                project="project:personal",
                approve=True,
            )

            tok_path = sec_dir / "memory-api-token"
            own_path = sec_dir / "owner-token"
            agent_tok, _ = generate_memory_api_token(tok_path)
            owner_tok = ensure_owner_token(own_path)
            rt = MemoryApiRuntimeConfig(
                brain_dir=str(vault_default),
                token_path=tok_path,
                owner_token_path=own_path,
                policy_path=sec_dir / "policy.json",
                security_dir=sec_dir,
            )
            server = MemoryLoopbackHTTPServer("127.0.0.1", 0, rt)
            port = server.server_address[1]
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                sdk_agent = MemoryApiClient(port=port, token=agent_tok)
                sdk_owner = MemoryApiClient(port=port, token=agent_tok, owner_token=owner_tok)

                # Non-owner agent cannot inspect or manage workspaces
                with self.assertRaises(httpx.HTTPStatusError) as cm_lv:
                    sdk_agent.list_vaults()
                self.assertEqual(cm_lv.exception.response.status_code, 403)

                # Create Work vault (with isolated per-vault token + federated_searchable=False initially)
                work_v = sdk_owner.create_vault(
                    "Work",
                    str(vault_work),
                    vault_id="work",
                    isolated_token=True,
                    federated_searchable=False,
                )
                work_tok = str(work_v["vault_token"])
                self.assertTrue(work_tok.startswith("vb_"))

                # Create Client-A vault (strictly isolated, never opted into federation)
                sdk_owner.create_vault(
                    "Client A",
                    str(vault_client),
                    vault_id="client-a",
                    isolated_token=False,
                    federated_searchable=False,
                )

                # Reject nested/overlapping vault paths
                with self.assertRaises(httpx.HTTPStatusError) as cm_nest:
                    sdk_owner.create_vault("Nested", str(vault_work / "sub-vault"), vault_id="nested")
                self.assertEqual(cm_nest.exception.response.status_code, 422)

                # Seed distinct approved atoms in Work & Client-A plus an unapproved pending atom + preference atom in Work
                _seed_approved_atom(
                    vault_work,
                    filename="work-postgres.md",
                    claim="Work production PostgreSQL clusters require point-in-time recovery backups.",
                    project="project:work",
                    approve=True,
                )
                _seed_approved_atom(
                    vault_work,
                    filename="work-pending.md",
                    claim="Unapproved draft backups policy should never appear in federated recall.",
                    project="project:work",
                    approve=False,
                )
                _seed_approved_atom(
                    vault_work,
                    filename="work-pref.md",
                    claim="Owner prefers dark theme backups dashboards.",
                    project="project:work",
                    epistemic_label="preference",
                    approve=True,
                )
                _seed_approved_atom(
                    vault_client,
                    filename="client-secret.md",
                    claim="Client Alpha confidential backups run on dedicated HSM hardware.",
                    project="project:client",
                    approve=True,
                )

                # Per-vault isolated token for 'work' MUST FAIL (403) while 'default' vault is active!
                base = f"http://127.0.0.1:{port}"
                with httpx.Client(trust_env=False, timeout=10.0) as client:
                    r_wrong_vault = client.get(
                        f"{base}/v1/health",
                        headers={"Host": f"127.0.0.1:{port}", "Authorization": f"Bearer {work_tok}"},
                    )
                    self.assertEqual(r_wrong_vault.status_code, 403)

                    # Hot-switch live server to 'work' vault -> vault-path.txt updates AND work_tok now succeeds!
                    sw = sdk_owner.switch_vault("work")
                    self.assertEqual(sw["active_vault_id"], "work")
                    self.assertEqual(
                        Path((sec_dir / "vault-path.txt").read_text(encoding="utf-8").strip()).resolve(),
                        vault_work.resolve(),
                    )
                    r_right_vault = client.get(
                        f"{base}/v1/health",
                        headers={"Host": f"127.0.0.1:{port}", "Authorization": f"Bearer {work_tok}"},
                    )
                    self.assertEqual(r_right_vault.status_code, 200)
                    # Recall on hot-switched server now sees Work vault atom, NOT Default vault atom!
                    rec_work = sdk_agent.recall("PostgreSQL recovery backups", retrieval_mode="lexical")
                    self.assertEqual([r["filename"] for r in rec_work["results"]], ["work-postgres.md"])

                # Opt-in Federated Recall:
                # 1. With all vaults isolated (federated_searchable=False), returns 0 hits & skips all
                fed0 = sdk_owner.federated_recall("backups")
                self.assertEqual(fed0["searched_vaults"], [])
                self.assertEqual(fed0["results"], [])

                # 2. Explicitly requesting non-opted-in 'client-a' fails closed with 403!
                with self.assertRaises(httpx.HTTPStatusError) as cm_fed:
                    sdk_owner.federated_recall("backups", vault_ids=["client-a"])
                self.assertEqual(cm_fed.exception.response.status_code, 403)

                # 3. Opt-in 'default' and 'work' ONLY; 'client-a' stays isolated and NEVER leaks!
                sdk_owner.update_vault_policy("default", federated_searchable=True)
                sdk_owner.update_vault_policy("work", federated_searchable=True)
                fed_hits = sdk_owner.federated_recall("backups", limit=10)
                self.assertEqual(set(fed_hits["searched_vaults"]), {"default", "work"})
                self.assertIn("client-a", fed_hits["skipped_non_opted_in"])
                hit_files = {(h["vault_id"], h["filename"]) for h in fed_hits["results"]}
                self.assertIn(("default", "default-rule.md"), hit_files)
                self.assertIn(("work", "work-postgres.md"), hit_files)
                # Pending, preference, and isolated Client-A atoms MUST NOT appear!
                self.assertNotIn(("work", "work-pending.md"), hit_files)
                self.assertNotIn(("work", "work-pref.md"), hit_files)
                self.assertNotIn(("client-a", "client-secret.md"), hit_files)

                # Non-destructive removal: cannot remove active vault, removing inactive keeps files on disk
                with self.assertRaises(httpx.HTTPStatusError) as cm_rem:
                    sdk_owner.remove_vault("work")
                self.assertEqual(cm_rem.exception.response.status_code, 422)
                rem = sdk_owner.remove_vault("client-a")
                self.assertFalse(rem["files_deleted"])
                self.assertTrue((vault_client / "2-atoms" / "client-secret.md").is_file())
            finally:
                server.shutdown()
                server.server_close()

    def test_cli_clipper_vault_commands_and_3_surface_ui_parity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sec_dir = tmp_path / "sec"
            sec_dir.mkdir()
            v1 = tmp_path / "v1"
            v2 = tmp_path / "v2"
            with patch.dict(os.environ, {"BRAIN_SECURITY_STATE_DIR": str(sec_dir), "BRAIN_VAULT_DIR": str(v1)}, clear=False):
                # CLI clipper pair -> list -> clip -> revoke
                buf = io.StringIO()
                with redirect_stdout(buf):
                    self.assertEqual(cli.main(["clipper", "pair", "--origin", CHROME_ORIGIN_A, "--label", "CLI Chrome", "--json"]), 0)
                paired = json.loads(buf.getvalue())
                self.assertTrue(paired["token"].startswith("clipper_"))

                buf_clip = io.StringIO()
                with redirect_stdout(buf_clip):
                    self.assertEqual(
                        cli.main(
                            [
                                "clipper",
                                "clip",
                                "--url",
                                "https://example.org/spec",
                                "--title",
                                "CLI Spec Clip",
                                "--quote",
                                "All mutations use compare-and-swap under writer.lock.",
                                "--json",
                            ]
                        ),
                        0,
                    )
                self.assertEqual(json.loads(buf_clip.getvalue())["destination"], "note")

                buf_rev = io.StringIO()
                with redirect_stdout(buf_rev):
                    self.assertEqual(cli.main(["clipper", "revoke", paired["pairing_id"], "--json"]), 0)
                self.assertEqual(json.loads(buf_rev.getvalue())["status"], "revoked")

                # CLI vault create -> switch -> list -> policy -> remove
                buf_vc = io.StringIO()
                with redirect_stdout(buf_vc):
                    self.assertEqual(cli.main(["vault", "create", "Secondary", str(v2), "--id", "secondary", "--json"]), 0)
                self.assertEqual(json.loads(buf_vc.getvalue())["vault_id"], "secondary")

                self.assertEqual(cli.main(["vault", "switch", "secondary", "--json"]), 0)
                self.assertEqual(cli.main(["vault", "policy", "secondary", "--federated-searchable", "--json"]), 0)
                self.assertEqual(cli.main(["vault", "switch", "default", "--json"]), 0)
                self.assertEqual(cli.main(["vault", "remove", "secondary", "--json"]), 0)

        # Static 3-surface UI parity & zero-innerHTML verification
        idx_html = (ROOT / "desktop" / "renderer" / "index.html").read_text(encoding="utf-8")
        app_js = (ROOT / "desktop" / "renderer" / "app.js").read_text(encoding="utf-8")
        dash_py = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
        vs_pkg = json.loads((ROOT / "extensions" / "vscode-second-brain" / "package.json").read_text(encoding="utf-8"))
        vs_js = (ROOT / "extensions" / "vscode-second-brain" / "extension.js").read_text(encoding="utf-8")

        for dom_id in ("sidebarVaultSelect", "systemWorkspacesCard", "systemClipperCard", "clipperPairBtn", "wsCreateBtn"):
            self.assertIn(dom_id, idx_html)
        self.assertIn("/v1/vaults/switch", app_js)
        self.assertIn("/v1/clipper/pair", app_js)
        self.assertNotIn("innerHTML", app_js)
        self.assertIn("ws_federated_recall", dash_py)
        self.assertIn("clipper_create_pairing", dash_py)
        vs_cmds = {c["command"] for c in vs_pkg["contributes"]["commands"]}
        self.assertTrue({"secondBrain.switchVault", "secondBrain.federatedSearch", "secondBrain.pairClipper"}.issubset(vs_cmds))
        self.assertNotIn("innerHTML", vs_js)


if __name__ == "__main__":
    unittest.main()
