# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["cryptography", "filelock", "httpx"]
# ///
"""End-to-End Cross-Runtime Tests for the Full On-Device Mobile Vault Client (Item 4).

Exercises real Python (`vault_sync`, `vault_backup`, `atom_mutator`, `skill_playbooks`,
`vault_ledger`, `inbox_drop`, `mobile_vault_client`) <-> Node.js (`vault-crypto.js`,
`vault-epistemic-engine.js`, `mobile_vault.test.js`) interoperability and security gates.
"""

from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlsplit
import zipfile

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
MOBILE_CLIENT_DIR = ROOT_DIR / "mobile" / "vault-client"

sys.path.insert(0, str(SCRIPTS_DIR))

from atom_mutator import (  # noqa: E402
    apply_atom_merge,
    apply_atom_mutation,
    atomic_write_bytes,
    content_sha256,
    invalidate_decision_log_cache,
    set_atom_temporal_bounds,
)
from inbox_drop import list_mobile_drop_pairings  # noqa: E402
from mobile_vault_client import (  # noqa: E402
    MobileVaultSecurityError,
    create_mobile_vault_pairing,
    export_mobile_vault_client_bundle,
    list_mobile_vault_pairings,
    revoke_mobile_vault_pairing,
)
from skill_playbooks import promote_skill_playbook  # noqa: E402
from vault_backup import create_vault_backup  # noqa: E402
from vault_ledger import commit_ledger_checkpoint, verify_merkle_proof, verify_vault_ledger  # noqa: E402
from vault_sync import (  # noqa: E402
    _compute_head_mac,
    _sign_head,
    decrypt_object,
    derive_master_key_from_passphrase,
    encrypt_object,
    init_sync,
    pull_mobile_inbox_drops,
    sign_s3_sigv4_headers,
    sync_config_path,
    sync_push,
)


class _SigV4MemoryObjectServer:
    """Thread-safe local HTTP object store verifying AWS4-HMAC-SHA256 SigV4 signatures."""

    def __init__(self, *, access_key: str, secret_key: str, region: str = "auto") -> None:
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.objects: dict[str, bytes] = {}
        self.get_counts: dict[str, int] = {}
        self.lock = threading.Lock()
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def _verify_sigv4(self, body: bytes) -> bool:
                auth = self.headers.get("Authorization") or ""
                amz_date = self.headers.get("x-amz-date") or ""
                if not auth.startswith("AWS4-HMAC-SHA256 ") or not amz_date:
                    return False
                full_url = f"http://{self.headers.get('Host')}{self.path}"
                expected = sign_s3_sigv4_headers(
                    self.command,
                    full_url,
                    body,
                    access_key=parent.access_key,
                    secret_key=parent.secret_key,
                    region=parent.region,
                    amz_date=amz_date,
                )
                return auth == expected["Authorization"]

            def do_HEAD(self) -> None:  # noqa: N802
                if not self._verify_sigv4(b""):
                    self.send_response(403)
                    self.end_headers()
                    return
                key = urlsplit(self.path).path.lstrip("/")
                with parent.lock:
                    data = parent.objects.get(key)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                if not self._verify_sigv4(b""):
                    self.send_response(403)
                    self.end_headers()
                    return
                parsed = urlsplit(self.path)
                if "list-type=2" in parsed.query:
                    prefix = ""
                    for part in parsed.query.split("&"):
                        if part.startswith("prefix="):
                            from urllib.parse import unquote

                            prefix = unquote(part.split("=", 1)[1])
                    with parent.lock:
                        keys = sorted(k for k in parent.objects if k.startswith(prefix))
                    items_xml = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
                    xml_bytes = (
                        '<?xml version="1.0" encoding="UTF-8"?>'
                        f"<ListBucketResult><IsTruncated>false</IsTruncated>{items_xml}</ListBucketResult>"
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/xml")
                    self.send_header("Content-Length", str(len(xml_bytes)))
                    self.end_headers()
                    self.wfile.write(xml_bytes)
                    return

                key = parsed.path.lstrip("/")
                with parent.lock:
                    data = parent.objects.get(key)
                    if data is not None:
                        parent.get_counts[key] = parent.get_counts.get(key, 0) + 1
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_PUT(self) -> None:  # noqa: N802
                clen = int(self.headers.get("Content-Length") or "0")
                body = self.rfile.read(clen) if clen > 0 else b""
                if not self._verify_sigv4(body):
                    self.send_response(403)
                    self.end_headers()
                    return
                key = urlsplit(self.path).path.lstrip("/")
                with parent.lock:
                    parent.objects[key] = body
                self.send_response(200)
                self.end_headers()

            def do_DELETE(self) -> None:  # noqa: N802
                if not self._verify_sigv4(b""):
                    self.send_response(403)
                    self.end_headers()
                    return
                key = urlsplit(self.path).path.lstrip("/")
                with parent.lock:
                    existed = parent.objects.pop(key, None) is not None
                self.send_response(204 if existed else 404)
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.endpoint_url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> str:
        self.thread.start()
        return self.endpoint_url

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5.0)


def _write_atom_draft(
    atoms_dir: Path,
    filename: str,
    *,
    claim: str,
    evidence_quote: str,
    source_rel: str,
    contexts: list[str],
    memory_type: str = "semantic",
    epistemic_label: str = "claim",
) -> tuple[Path, str]:
    doc = "\n".join(
        [
            "---",
            f"id: 2026-06-01-{Path(filename).stem}",
            "type: atom",
            f"memory_type: {memory_type}",
            "certainty: established",
            "created: 2026-06-01",
            "last-reviewed: 2026-06-01",
            "importance: 0.85",
            "confidence: 0.92",
            "sources:",
            f'  - "{source_rel}"',
            "links: []",
            "tags: []",
            "contexts: " + json.dumps(contexts),
            "supersedes: []",
            "superseded_by: []",
            "contested_by: []",
            "origin: user_local",
            "citation_integrity: missing",
            "claim_state: candidate",
            f"epistemic_label: {epistemic_label}",
            "review_status: pending",
            "---",
            "",
            "## Claim",
            claim,
            "",
            "## Evidence",
            f"> {evidence_quote}",
            "",
        ]
    ) + "\n"
    target = atoms_dir / filename
    raw = doc.encode("utf-8")
    atomic_write_bytes(str(target), raw)
    return target, content_sha256(raw)


def _run_node_json_script(js_code: str, payload: dict[str, object]) -> dict[str, object]:
    proc = subprocess.run(
        ["node", "-e", js_code],
        input=json.dumps(payload).encode("utf-8"),
        cwd=str(MOBILE_CLIENT_DIR),
        capture_output=True,
        timeout=60.0,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"Node script failed (exit {proc.returncode}):\nSTDOUT:\n{proc.stdout.decode('utf-8', 'replace')}\n"
            f"STDERR:\n{proc.stderr.decode('utf-8', 'replace')}"
        )
    return json.loads(proc.stdout.decode("utf-8"))


class MobileVaultClientEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        invalidate_decision_log_cache()
        self.tmp_obj = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self.tmp_obj.name)
        self.s3_ak_env = "TEST_MOBILE_S3_AK"
        self.s3_sk_env = "TEST_MOBILE_S3_SK"
        self.s3_ak = "AKIATESTMOBILEVAULT01"
        self.s3_sk = "secretTestMobileVaultKey0123456789abcdef"
        os.environ[self.s3_ak_env] = self.s3_ak
        os.environ[self.s3_sk_env] = self.s3_sk

    def tearDown(self) -> None:
        os.environ.pop(self.s3_ak_env, None)
        os.environ.pop(self.s3_sk_env, None)
        invalidate_decision_log_cache()
        self.tmp_obj.cleanup()

    def _populate_canonical_test_vault(self, brain_dir: Path) -> dict[str, object]:
        raw_dir = brain_dir / "0-raw"
        inbox_dir = raw_dir / "inbox"
        archive_dir = raw_dir / "archive"
        atoms_dir = brain_dir / "2-atoms"
        playbooks_dir = brain_dir / "5-playbooks"
        notes_dir = brain_dir / "notes"
        for d in (raw_dir, inbox_dir, archive_dir, atoms_dir, playbooks_dir, notes_dir, brain_dir / ".sidecar"):
            d.mkdir(parents=True, exist_ok=True)

        atomic_write_bytes(str(brain_dir / "house-rules.md"), b"# House Rules\nVerify every claim.\n")

        # Raw sources (include UTF-8 multibyte chars BEFORE the quote to test exact UTF-8 byte-offset slicing!)
        src_alpha_quote = (
            "Deploy Alpha release artifacts only after verifying Ed25519 signatures and mTLS certificates."
        )
        atomic_write_bytes(
            str(raw_dir / "alpha-runbook.md"),
            (f"# Runbook — Café 🚀\n\n{src_alpha_quote}\n\nLegacy Alpha deployment used SSH keys in 2021.\n").encode(
                "utf-8"
            ),
        )

        src_beta_quote = "Beta analytics warehouse stores partitioned Parquet tables inside encrypted NVMe volumes."
        atomic_write_bytes(
            str(raw_dir / "beta-spec.md"),
            (f"# Beta Storage — Résumé\n\n{src_beta_quote}\n").encode("utf-8"),
        )

        src_pref_quote = "Prefer concise terminal tables with ISO-8601 UTC timestamps over verbose prose summaries."
        atomic_write_bytes(
            str(raw_dir / "owner-prefs.md"),
            (f"# Preferences\n\n{src_pref_quote}\n").encode("utf-8"),
        )

        src_hist_quote = "Alpha staging cluster ran PostgreSQL 14 on primary node db-2023-east during 2023."
        atomic_write_bytes(
            str(raw_dir / "alpha-2023-ops.md"),
            (f"# 2023 Ops Log\n\n{src_hist_quote}\n").encode("utf-8"),
        )

        src_tampered_quote = "Alpha cache layer uses Redis 7.2 with TLS replication enabled across zones."
        tampered_src_path = raw_dir / "alpha-tampered.md"
        atomic_write_bytes(
            str(tampered_src_path),
            (f"# Cache Spec\n\n{src_tampered_quote}\n").encode("utf-8"),
        )

        # 1. Superseded trap atom + 2. Approved procedural atom (project:alpha) that supersedes it
        sup_path, sup_sha = _write_atom_draft(
            atoms_dir,
            "alpha-superseded-ssh.md",
            claim="Alpha deployments authenticate with legacy static SSH keys.",
            evidence_quote="Legacy Alpha deployment used SSH keys in 2021.",
            source_rel="0-raw/alpha-runbook.md",
            contexts=["project:alpha"],
            memory_type="procedural",
        )
        sup_approved = apply_atom_mutation(
            action="approve",
            atom_path=str(sup_path),
            expected_sha256=sup_sha,
            actor="cameron",
            brain_dir=str(brain_dir),
            atoms_dir=str(atoms_dir),
        )

        alpha_path, alpha_sha = _write_atom_draft(
            atoms_dir,
            "alpha-deploy-mtls.md",
            claim="Verify Ed25519 release signatures and mTLS client certificates before deploying Alpha.",
            evidence_quote=src_alpha_quote,
            source_rel="0-raw/alpha-runbook.md",
            contexts=["project:alpha"],
            memory_type="procedural",
        )
        merge_records = apply_atom_merge(
            survivor_path=str(alpha_path),
            survivor_expected_sha256=alpha_sha,
            superseded_path=str(sup_path),
            superseded_expected_sha256=sup_approved["content_sha256_after"],
            actor="cameron",
            rationale="SSH keys replaced by Ed25519 + mTLS.",
            brain_dir=str(brain_dir),
            atoms_dir=str(atoms_dir),
        )
        alpha_approved = apply_atom_mutation(
            action="approve",
            atom_path=str(alpha_path),
            expected_sha256=merge_records[0]["content_sha256_after"],
            actor="cameron",
            brain_dir=str(brain_dir),
            atoms_dir=str(atoms_dir),
        )

        # 3. Approved semantic atom in project:beta
        beta_path, beta_sha = _write_atom_draft(
            atoms_dir,
            "beta-parquet-storage.md",
            claim="Beta analytics warehouse stores partitioned Parquet tables on encrypted NVMe volumes.",
            evidence_quote=src_beta_quote,
            source_rel="0-raw/beta-spec.md",
            contexts=["project:beta"],
            memory_type="semantic",
        )
        apply_atom_mutation(
            action="approve",
            atom_path=str(beta_path),
            expected_sha256=beta_sha,
            actor="cameron",
            brain_dir=str(brain_dir),
            atoms_dir=str(atoms_dir),
        )

        # 4. Approved owner_subjective_preference atom
        pref_path, pref_sha = _write_atom_draft(
            atoms_dir,
            "owner-cli-style.md",
            claim="Format CLI reports as concise tables with ISO-8601 UTC timestamps.",
            evidence_quote=src_pref_quote,
            source_rel="0-raw/owner-prefs.md",
            contexts=["project:alpha"],
            memory_type="self",
            epistemic_label="preference",
        )
        apply_atom_mutation(
            action="approve",
            atom_path=str(pref_path),
            expected_sha256=pref_sha,
            actor="cameron",
            brain_dir=str(brain_dir),
            atoms_dir=str(atoms_dir),
        )

        # 5. Unapproved pending trap atom
        _write_atom_draft(
            atoms_dir,
            "alpha-pending-trap.md",
            claim="Alpha deployments might skip Ed25519 signature verification on Fridays.",
            evidence_quote=src_alpha_quote,
            source_rel="0-raw/alpha-runbook.md",
            contexts=["project:alpha"],
            memory_type="semantic",
        )

        # 6. Expired bi-temporal atom (valid_from: 2023-01-01, valid_until: 2024-01-01)
        hist_path, hist_sha = _write_atom_draft(
            atoms_dir,
            "alpha-postgres-2023.md",
            claim="Alpha staging database ran PostgreSQL 14 on db-2023-east.",
            evidence_quote=src_hist_quote,
            source_rel="0-raw/alpha-2023-ops.md",
            contexts=["project:alpha"],
            memory_type="semantic",
        )
        hist_app = apply_atom_mutation(
            action="approve",
            atom_path=str(hist_path),
            expected_sha256=hist_sha,
            actor="cameron",
            brain_dir=str(brain_dir),
            atoms_dir=str(atoms_dir),
        )
        set_atom_temporal_bounds(
            str(hist_path),
            valid_from="2023-01-01",
            valid_until="2024-01-01",
            expected_sha256=hist_app["content_sha256_after"],
            actor="cameron",
            rationale="Cluster migrated in January 2024.",
            brain_dir=str(brain_dir),
            atoms_dir=str(atoms_dir),
        )

        # 7. Tampered-source atom: approve validly first, THEN mutate its 0-raw/ source file!
        tamp_path, tamp_sha = _write_atom_draft(
            atoms_dir,
            "alpha-tampered-redis.md",
            claim="Alpha cache layer runs Redis 7.2 with TLS replication.",
            evidence_quote=src_tampered_quote,
            source_rel="0-raw/alpha-tampered.md",
            contexts=["project:alpha"],
            memory_type="semantic",
        )
        apply_atom_mutation(
            action="approve",
            atom_path=str(tamp_path),
            expected_sha256=tamp_sha,
            actor="cameron",
            brain_dir=str(brain_dir),
            atoms_dir=str(atoms_dir),
        )
        atomic_write_bytes(
            str(tampered_src_path),
            b"# Cache Spec (TAMPERED AFTER APPROVAL!)\n\nAlpha cache layer runs unencrypted Memcached.\n",
        )

        # 8. Working note in notes/
        atomic_write_bytes(
            str(notes_dir / "alpha-ideas.md"),
            b"# Alpha Scratchpad\n\nInvestigate hardware YubiKey signing for Alpha release tags #project:alpha\n",
        )

        # 9. Active promoted Playbook backed by alpha-deploy-mtls.md
        pb_promoted = promote_skill_playbook(
            playbook_id="alpha-release-gate",
            title="Alpha Signed Release Gate",
            backing_atoms=[("alpha-deploy-mtls.md", alpha_approved["content_sha256_after"])],
            trigger="Before deploying Alpha to production.",
            preconditions=["Release tarball built reproducibly."],
            steps=["Verify Ed25519 release signatures and mTLS client certificates before deploying Alpha."],
            expected_outputs=["Signed Alpha artifact active on port 8443."],
            failure_recovery=["Abort rollout and alert security on signature mismatch."],
            actor="cameron",
            rationale="Core mobile audit test playbook.",
            allow_early_owner_override=True,
            brain_dir=str(brain_dir),
            atoms_dir=str(atoms_dir),
            playbooks_dir=str(playbooks_dir),
            telemetry_db_path=str(brain_dir / ".sidecar" / "telemetry.sqlite3"),
        )

        # 9b. Active promoted Stage 4 Executable Skill in 6-skills/ backed by alpha-release-gate
        from executable_skills import promote_playbook_to_executable_skill

        promote_playbook_to_executable_skill(
            skill_id="alpha-release-skill",
            playbook_id="alpha-release-gate",
            expected_playbook_sha256=str(pb_promoted["sha256"]),
            step_actions=[{"action_type": "vault_audit", "audit_kind": "sources", "output_file": "sources.json"}],
            actor="cameron",
            rationale="Core mobile audit test executable skill.",
            allow_early_owner_override=True,
            brain_dir=str(brain_dir),
            atoms_dir=str(atoms_dir),
            playbooks_dir=str(playbooks_dir),
            skills_dir=str(brain_dir / "6-skills"),
            telemetry_db_path=str(brain_dir / ".sidecar" / "telemetry.sqlite3"),
            security_dir=self.tmp / "sec_skills",
        )

        # 10. Commit tamper-evident Merkle checkpoint in 0-raw/archive/vault-ledger.jsonl
        checkpoint = commit_ledger_checkpoint(
            brain_dir=str(brain_dir),
            atoms_dir=str(atoms_dir),
            playbooks_dir=str(playbooks_dir),
            actor="cameron",
            note="Mobile sync verification checkpoint",
        )
        py_ledger = verify_vault_ledger(
            brain_dir=str(brain_dir),
            atoms_dir=str(atoms_dir),
            playbooks_dir=str(playbooks_dir),
        )
        self.assertTrue(py_ledger["chain_valid"])
        self.assertTrue(py_ledger["synced_with_head"])
        self.assertEqual(py_ledger["approved_atoms"], 4)
        self.assertEqual(py_ledger["accepted_playbooks"], 1)
        return {"checkpoint": checkpoint, "py_ledger": py_ledger}

    def test_node_unit_suite_passes(self) -> None:
        proc = subprocess.run(
            ["node", "--test", "test/mobile_vault.test.js"],
            cwd=str(MOBILE_CLIENT_DIR),
            capture_output=True,
            text=True,
            timeout=60.0,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"node --test failed:\n{proc.stdout}\n{proc.stderr}")

    def test_scrypt_passphrase_and_sbvault_full_offline_roundtrip(self) -> None:
        brain_dir = self.tmp / "vault_scrypt"
        state_dir = self.tmp / "sec_scrypt"
        passphrase = " Vault-Passphrase-2026-Scrypt! "
        backup_passphrase = "Backup-Sbvault-Passphrase-99!"

        vault_meta = self._populate_canonical_test_vault(brain_dir)
        py_ledger = vault_meta["py_ledger"]

        server = _SigV4MemoryObjectServer(access_key=self.s3_ak, secret_key=self.s3_sk, region="auto")
        endpoint = server.start()
        try:
            init_res = init_sync(
                endpoint,
                backend_type="http_object",
                auth_mode="s3_sigv4",
                s3_region="auto",
                s3_access_key_env=self.s3_ak_env,
                s3_secret_key_env=self.s3_sk_env,
                brain_dir=brain_dir,
                device_name="desktop-owner",
                passphrase=passphrase,
                state_dir=state_dir,
            )
            push_res = sync_push(brain_dir=brain_dir, passphrase=passphrase, state_dir=state_dir)
            self.assertEqual(push_res["status"], "pushed")
            self.assertEqual(push_res["epoch"], 1)

            # Verify bit-for-bit scrypt parity between Python hashlib.scrypt and JS RFC 7914 scrypt
            hdr_doc = json.loads(server.objects["vault-header.json"].decode("utf-8"))
            py_mk = derive_master_key_from_passphrase(passphrase, base64.b64decode(hdr_doc["kdf_salt_b64"]))

            pairing = create_mobile_vault_pairing(
                brain_dir=brain_dir,
                label="Cameron iPhone 16",
                include_passphrase_hint="2026 scrypt phrase",
                s3_read_access_key_id=self.s3_ak,
                s3_read_secret_access_key=self.s3_sk,
                state_dir=state_dir,
            )
            self.assertNotIn("device_privkey_b64", json.dumps(pairing))

            # Also create an encrypted .sbvault backup archive in Python
            sbvault_path = self.tmp / "export.sbvault"
            backup_res = create_vault_backup(
                sbvault_path,
                brain_dir=brain_dir,
                passphrase=backup_passphrase,
            )
            self.assertEqual(backup_res["merkle_root"], py_ledger["live_merkle_root"])

            # Step 1 in Node: Pull over live HTTP/SigV4 AND upload an outbound SBID1 Inbox Drop!
            pull_js = r"""
            const fs = require("node:fs");
            const VaultCrypto = require("./www/lib/vault-crypto.js");
            (async () => {
              const input = JSON.parse(fs.readFileSync(0, "utf8"));
              const paired = VaultCrypto.parseVaultPairingFragment(input.pwa_pairing_url);
              const derivedHex = VaultCrypto.bytesToHex(
                VaultCrypto.deriveMasterKeyFromPassphrase(
                  input.passphrase,
                  VaultCrypto.base64ToBytes(paired.kdf_salt_b64)
                )
              );
              const transport = new VaultCrypto.HttpObjectSyncTransport({
                endpointUrl: paired.endpoint_url,
                authMode: paired.auth_mode,
                region: paired.s3_region,
                accessKeyId: paired.s3_access_key_id,
                secretAccessKey: paired.s3_secret_access_key,
              });
              const pulled1 = await VaultCrypto.pullVaultFromBackend(
                transport,
                {
                  vaultId: paired.vault_id,
                  passphrase: input.passphrase,
                  trustedPeers: paired.trusted_peers,
                  pinMode: "strict",
                  forcePureJsEd25519: true,
                },
                { files: {}, syncCursor: { epoch: 0 } }
              );
              // Second pull must reuse 100% of blobs incrementally without downloading any objects/*
              const pulled2 = await VaultCrypto.pullVaultFromBackend(
                transport,
                {
                  vaultId: paired.vault_id,
                  passphrase: input.passphrase,
                  trustedPeers: pulled1.syncCursor.trusted_peers,
                  pinMode: "strict",
                },
                { files: pulled1.files, syncCursor: pulled1.syncCursor }
              );

              // Also send an encrypted SBID1 mobile quick-capture drop to the S3 server!
              const dropEnv = await VaultCrypto.encryptInboxDropEnvelope(
                {
                  title: "Mobile Field Observation",
                  url: "https://example.org/mtls-spec",
                  text: "Captured from mobile vault client.",
                  tags: ["project:alpha", "mobile"],
                },
                paired.inbox_drop_pairing
              );
              await transport.putObject(dropEnv.objectKey, dropEnv.envelopeBytes);

              const serializedFiles = {};
              for (const [k, v] of Object.entries(pulled2.files)) {
                serializedFiles[k] = VaultCrypto.bytesToBase64(v);
              }
              process.stdout.write(JSON.stringify({
                derivedHex,
                epoch: pulled2.epoch,
                pulled1Downloaded: pulled1.downloadedBlobs,
                pulled2Downloaded: pulled2.downloadedBlobs,
                pulled2Reused: pulled2.reusedBlobs,
                dropObjectKey: dropEnv.objectKey,
                filesB64: serializedFiles,
              }));
            })();
            """
            online_out = _run_node_json_script(
                pull_js,
                {
                    "pwa_pairing_url": pairing["pwa_pairing_url"],
                    "passphrase": passphrase,
                },
            )
            self.assertEqual(online_out["derivedHex"], py_mk.hex())
            self.assertEqual(online_out["epoch"], 1)
            self.assertGreater(online_out["pulled1Downloaded"], 0)
            self.assertEqual(online_out["pulled2Downloaded"], 0)
            self.assertEqual(online_out["pulled2Reused"], online_out["pulled1Downloaded"])

            # Verify Python ingests the mobile SBID1 drop strictly into quarantined 0-raw/inbox/
            drop_ingest = pull_mobile_inbox_drops(brain_dir=brain_dir, state_dir=state_dir)
            self.assertEqual(drop_ingest["ingested_count"], 1)
            inbox_files = list((brain_dir / "0-raw" / "inbox").glob("mobile-drop-*.md"))
            self.assertEqual(len(inbox_files), 1)
        finally:
            # SHUT DOWN SERVER — 100% OFFLINE FROM HERE ON!
            server.stop()

        # Step 2 in Node (100% OFFLINE — server stopped!):
        # Store in EncryptedDeviceStore, lock/unlock with PIN, import .sbvault, run Search, Chat & Merkle Proofs!
        offline_js = r"""
        const fs = require("node:fs");
        const VaultCrypto = require("./www/lib/vault-crypto.js");
        const Epistemic = require("./www/lib/vault-epistemic-engine.js");
        (async () => {
          const input = JSON.parse(fs.readFileSync(0, "utf8"));
          const rawFiles = {};
          for (const [k, b64] of Object.entries(input.filesB64)) {
            rawFiles[k] = VaultCrypto.base64ToBytes(b64);
          }

          // 1. EncryptedDeviceStore roundtrip (PBKDF2 210,000 + AES-256-GCM)
          const store = new VaultCrypto.EncryptedDeviceStore({ recordId: "offline_test" });
          await store.saveState("948271", {
            vaultId: input.vaultId,
            syncCursor: { epoch: 1 },
            files: rawFiles,
          });
          const rawCt = await store.inspectRawCiphertext();
          const ctText = Buffer.from(rawCt).toString("utf8");
          if (ctText.includes("Ed25519 release signatures") || ctText.includes("PostgreSQL 14")) {
            throw new Error("Plaintext leaked into EncryptedDeviceStore!");
          }
          store.lock();
          let wrongPinRejected = false;
          try {
            await store.unlock("000000");
          } catch (e) {
            wrongPinRejected = e instanceof VaultCrypto.SyncSecurityError;
          }
          const unlocked = await store.unlock("948271");
          const files = unlocked.files;

          // 2. Also test offline .sbvault archive decryption + USTAR/PAX tar + Merkle verification
          const sbvaultBytes = new Uint8Array(fs.readFileSync(input.sbvaultPath));
          const unpackedBackup = await VaultCrypto.importSbVaultArchive(sbvaultBytes, input.backupPassphrase);

          // 3. Offline Search & Epistemic Isolation checks
          const alphaVerified = Epistemic.searchVerifiedVault(
            files,
            "Alpha deployments Ed25519 signatures mTLSSSH Redis Fridays PostgreSQL",
            { project: "project:alpha" }
          );
          const betaInAlpha = Epistemic.searchVerifiedVault(
            files,
            "Beta analytics Parquet NVMe volumes",
            { project: "project:alpha" }
          );
          const betaInBeta = Epistemic.searchVerifiedVault(
            files,
            "Beta analytics Parquet NVMe volumes",
            { project: "project:beta" }
          );
          const tamperedCheck = Epistemic.searchVerifiedVault(
            files,
            "Alpha cache Redis TLS replication",
            { project: "project:alpha" }
          );
          const expiredNow = Epistemic.searchVerifiedVault(
            files,
            "Alpha staging PostgreSQL 14 db-2023-east",
            { project: "project:alpha" }
          );
          const historicalAsOf = Epistemic.searchVerifiedVault(
            files,
            "Alpha staging PostgreSQL 14 db-2023-east",
            { project: "project:alpha", asOf: "2023-06-01" }
          );
          const notesHits = Epistemic.searchWorkingNotes(
            files,
            "YubiKey signing Alpha release tags",
            { project: "project:alpha" }
          );

          // 4. Offline Grounded Chat modes
          const chatVerified = await Epistemic.answerGroundedQuestion(
            files,
            "How do we verify Alpha release deployments?",
            { mode: "verified", project: "project:alpha" }
          );
          const chatInstant = await Epistemic.answerGroundedQuestion(
            files,
            "How do we verify Alpha release deployments?",
            { mode: "instant", project: "project:alpha" }
          );
          const chatPref = await Epistemic.answerGroundedQuestion(
            files,
            "How should CLI reports and timestamps be formatted?",
            { mode: "preference", project: "project:alpha" }
          );
          const prefInStandard = await Epistemic.answerGroundedQuestion(
            files,
            "How should CLI reports and timestamps be formatted?",
            { mode: "verified", project: "project:alpha" }
          );
          const chatResearch = await Epistemic.answerGroundedQuestion(
            files,
            "Alpha deployments Ed25519 signature verification Fridays",
            { mode: "research", project: "project:alpha" }
          );
          const chatHistorical = await Epistemic.answerGroundedQuestion(
            files,
            "Which PostgreSQL database node did Alpha staging run?",
            { mode: "verified", project: "project:alpha", asOf: "2023-06-01" }
          );
          const chatAbstain = await Epistemic.answerGroundedQuestion(
            files,
            "What is the Wi-Fi password in Tokyo?",
            { mode: "verified", project: "project:alpha" }
          );

          // 5. Offline Merkle & Ledger Audit ( run on both synced files and .sbvault imported files )
          const ledgerAudit = Epistemic.verifyVaultLedgerOffline(files);
          const atomProof = Epistemic.proveLeafOffline(files, "2-atoms/alpha-deploy-mtls.md");
          const pbProof = Epistemic.proveLeafOffline(files, "5-playbooks/alpha-release-gate.md");
          const skProof = Epistemic.proveLeafOffline(files, "6-skills/alpha-release-skill.md");
          const atomProofValidJs = Epistemic.verifyMerkleProofOffline(atomProof);
          const pbProofValidJs = Epistemic.verifyMerkleProofOffline(pbProof);
          const skProofValidJs = Epistemic.verifyMerkleProofOffline(skProof);

          process.stdout.write(JSON.stringify({
            wrongPinRejected,
            backupMerkleRoot: unpackedBackup.merkleRoot,
            backupApprovedAtoms: unpackedBackup.approvedAtoms,
            backupAcceptedPlaybooks: unpackedBackup.acceptedPlaybooks,
            alphaVerifiedIds: alphaVerified.map((x) => x.filename),
            betaInAlphaIds: betaInAlpha.map((x) => x.filename),
            betaInBetaIds: betaInBeta.map((x) => x.filename),
            tamperedIds: tamperedCheck.map((x) => x.filename),
            expiredNowIds: expiredNow.map((x) => x.filename),
            historicalAsOfIds: historicalAsOf.map((x) => x.filename),
            notesHits,
            chatVerified,
            chatInstant,
            chatPref,
            prefInStandard,
            chatResearch,
            chatHistorical,
            chatAbstain,
            ledgerAudit,
            atomProof,
            pbProof,
            skProof,
            atomProofValidJs,
            pbProofValidJs,
            skProofValidJs,
          }));
        })();
        """
        off = _run_node_json_script(
            offline_js,
            {
                "vaultId": init_res["vault_id"],
                "filesB64": online_out["filesB64"],
                "sbvaultPath": str(sbvault_path),
                "backupPassphrase": backup_passphrase,
            },
        )

        # Assertions: EncryptedDeviceStore & .sbvault
        self.assertTrue(off["wrongPinRejected"])
        self.assertEqual(off["backupMerkleRoot"], py_ledger["live_merkle_root"])
        self.assertEqual(off["backupApprovedAtoms"], 4)
        self.assertEqual(off["backupAcceptedPlaybooks"], 1)

        # Assertions: 0% leakage of pending, superseded, tampered, expired, preference, or cross-project atoms!
        self.assertEqual(off["alphaVerifiedIds"], ["alpha-deploy-mtls.md"])
        self.assertEqual(off["betaInAlphaIds"], [])
        self.assertEqual(off["betaInBetaIds"], ["beta-parquet-storage.md"])
        self.assertEqual(off["tamperedIds"], [])
        self.assertEqual(off["expiredNowIds"], [])
        self.assertEqual(off["historicalAsOfIds"], ["alpha-postgres-2023.md"])

        # Working notes strictly isolated
        self.assertEqual(len(off["notesHits"]), 1)
        self.assertEqual(off["notesHits"][0]["trust_tier"], "unverified_note")
        self.assertEqual(off["notesHits"][0]["attribution"], "[note: alpha-ideas.md]")

        # Grounded chat modes
        self.assertFalse(off["chatVerified"]["abstained"])
        self.assertEqual(len(off["chatVerified"]["claims"]), 1)
        self.assertIn(
            "[alpha-deploy-mtls.md -> 0-raw/alpha-runbook.md[",
            off["chatVerified"]["text"],
        )
        self.assertFalse(off["chatInstant"]["abstained"])

        # Preference separation: [Preference] in preference mode, 0% bleed into verified mode!
        self.assertFalse(off["chatPref"]["abstained"])
        self.assertTrue(off["chatPref"]["text"].startswith("- [Preference] "))
        self.assertTrue(off["prefInStandard"]["abstained"])
        self.assertEqual(off["prefInStandard"]["claims"], [])

        # Research mode separates verified claims from [UNVERIFIED LEADS]
        self.assertFalse(off["chatResearch"]["abstained"])
        self.assertEqual(
            [c["evidence"][0]["atom_id"] for c in off["chatResearch"]["claims"]],
            ["alpha-deploy-mtls.md"],
        )
        lead_ids = [l["lead_id"] for l in off["chatResearch"]["unverified_leads"]]
        self.assertIn("alpha-pending-trap.md", lead_ids)
        self.assertIn("[UNVERIFIED LEADS]", off["chatResearch"]["text"])

        # Bi-temporal as_of recovery in grounded chat
        self.assertFalse(off["chatHistorical"]["abstained"])
        self.assertEqual(
            off["chatHistorical"]["claims"][0]["evidence"][0]["atom_id"],
            "alpha-postgres-2023.md",
        )

        # Structural abstention on unanswerable trap query
        self.assertTrue(off["chatAbstain"]["abstained"])
        self.assertEqual(off["chatAbstain"]["claims"], [])

        # Offline Merkle & Ledger bit-for-bit parity with Python!
        self.assertTrue(off["ledgerAudit"]["chain_valid"])
        self.assertTrue(off["ledgerAudit"]["synced_with_head"])
        self.assertEqual(off["ledgerAudit"]["live_merkle_root"], py_ledger["live_merkle_root"])
        self.assertEqual(off["ledgerAudit"]["head_entry_sha256"], py_ledger["head_entry_sha256"])
        self.assertTrue(off["atomProofValidJs"])
        self.assertTrue(off["pbProofValidJs"])
        self.assertTrue(off["skProofValidJs"])
        self.assertEqual(off["ledgerAudit"]["active_skills"], 1)
        self.assertTrue(verify_merkle_proof(off["atomProof"]))
        self.assertTrue(verify_merkle_proof(off["pbProof"]))
        self.assertTrue(verify_merkle_proof(off["skProof"]))

    def test_keyfile_mode_and_adversarial_sync_attacks_fail_closed(self) -> None:
        brain_dir = self.tmp / "vault_keyfile"
        state_dir = self.tmp / "sec_keyfile"
        self._populate_canonical_test_vault(brain_dir)

        server = _SigV4MemoryObjectServer(access_key=self.s3_ak, secret_key=self.s3_sk, region="us-east-1")
        endpoint = server.start()
        try:
            init_res = init_sync(
                endpoint,
                backend_type="http_object",
                auth_mode="s3_sigv4",
                s3_region="us-east-1",
                s3_access_key_env=self.s3_ak_env,
                s3_secret_key_env=self.s3_sk_env,
                brain_dir=brain_dir,
                device_name="linux-host",
                kdf_mode="keyfile",
                state_dir=state_dir,
            )
            self.assertEqual(init_res["kdf_mode"], "keyfile")
            self.assertTrue(init_res["recovery_key_b64"])
            sync_push(brain_dir=brain_dir, state_dir=state_dir)

            pairing = create_mobile_vault_pairing(
                brain_dir=brain_dir,
                label="Pixel 9 Pro",
                s3_read_access_key_id=self.s3_ak,
                s3_read_secret_access_key=self.s3_sk,
                include_recovery_key=True,
                state_dir=state_dir,
            )
            self.assertEqual(pairing["kdf_mode"], "keyfile")
            self.assertEqual(
                pairing["pairing_bundle"]["recovery_key_b64"],
                init_res["recovery_key_b64"],
            )

            # Snapshot clean server objects + host sync config so we can craft adversarial attacks!
            clean_objects = dict(server.objects)
        finally:
            server.stop()

        cfg = json.loads(sync_config_path(brain_dir, state_dir=state_dir).read_text(encoding="utf-8"))
        master_key = base64.b64decode(init_res["recovery_key_b64"])
        vault_id = init_res["vault_id"]

        # Build a validly signed & encrypted malicious manifest containing `../escape.md` path traversal!
        orig_head = json.loads(clean_objects["head.json"].decode("utf-8"))
        orig_manifest_id = orig_head["manifest_id"]
        orig_manifest_plain = decrypt_object(
            master_key,
            vault_id=vault_id,
            kind="manifest",
            object_id=orig_manifest_id,
            envelope=clean_objects[f"manifests/{orig_manifest_id}.enc"],
        )
        evil_manifest = json.loads(orig_manifest_plain.decode("utf-8"))
        first_entry = next(iter(evil_manifest["files"].values()))
        evil_manifest["epoch"] = 2
        evil_manifest["files"]["../escape.md"] = first_entry
        evil_manifest_plain = json.dumps(evil_manifest, sort_keys=True).encode("utf-8")
        evil_mid = "ff" * 32
        evil_mct = encrypt_object(
            master_key,
            vault_id=vault_id,
            kind="manifest",
            object_id=evil_mid,
            plaintext=evil_manifest_plain,
        )
        evil_head = dict(orig_head)
        evil_head["epoch"] = 2
        evil_head["manifest_id"] = evil_mid
        evil_head["manifest_ciphertext_sha256"] = content_sha256(evil_mct)
        evil_head["signature_b64"] = _sign_head(evil_head, cfg["device_privkey_b64"])
        evil_head["head_mac"] = _compute_head_mac(master_key, vault_id, evil_head)

        adv_js = r"""
        const fs = require("node:fs");
        const VaultCrypto = require("./www/lib/vault-crypto.js");
        (async () => {
          const input = JSON.parse(fs.readFileSync(0, "utf8"));
          const paired = VaultCrypto.parseVaultPairingFragment(input.pwa_pairing_url);

          function makeBackend(overrides = {}) {
            const objs = {};
            for (const [k, b64] of Object.entries(input.cleanObjectsB64)) {
              objs[k] = VaultCrypto.base64ToBytes(b64);
            }
            for (const [k, v] of Object.entries(overrides)) {
              if (v === null) delete objs[k];
              else objs[k] = v;
            }
            return new VaultCrypto.InMemoryOrBundleBackend(objs);
          }

          const syncCfg = {
            vaultId: paired.vault_id,
            recoveryKeyB64: paired.recovery_key_b64,
            trustedPeers: paired.trusted_peers,
            pinMode: "strict",
          };

          // 0. Baseline keyfile pull succeeds!
          const baseline = await VaultCrypto.pullVaultFromBackend(makeBackend(), syncCfg, {
            files: {},
            syncCursor: { epoch: 0 },
          });

          async function expectSecurityFailure(name, backend, localCursorEpoch = 0) {
            try {
              await VaultCrypto.pullVaultFromBackend(backend, syncCfg, {
                files: {},
                syncCursor: { epoch: localCursorEpoch, trusted_peers: paired.trusted_peers },
              });
              return { name, rejected: false };
            } catch (err) {
              return { name, rejected: err instanceof VaultCrypto.SyncSecurityError, error: err.message };
            }
          }

          const blobKeys = Object.keys(input.cleanObjectsB64).filter((k) => k.startsWith("objects/"));
          const targetBlobKey = blobKeys[0];
          const secondBlobKey = blobKeys[1];

          // 1. Tampered blob ciphertext (flip 1 byte)
          const tamperedBlob = VaultCrypto.base64ToBytes(input.cleanObjectsB64[targetBlobKey]);
          tamperedBlob[tamperedBlob.length - 5] ^= 0xff;
          const r1 = await expectSecurityFailure("tampered_blob", makeBackend({ [targetBlobKey]: tamperedBlob }));

          // 2. Swapped AAD (place valid blob B ciphertext under blob A key)
          const blobB = VaultCrypto.base64ToBytes(input.cleanObjectsB64[secondBlobKey]);
          const r2 = await expectSecurityFailure("swapped_aad", makeBackend({ [targetBlobKey]: blobB }));

          // 3. Forged Ed25519 signature on head.json (valid head_mac, corrupted signature_b64)
          const forgedHead = JSON.parse(
            VaultCrypto.utf8DecodeStrict(VaultCrypto.base64ToBytes(input.cleanObjectsB64["head.json"]))
          );
          const sigBytes = VaultCrypto.base64ToBytes(forgedHead.signature_b64);
          sigBytes[0] ^= 0x55;
          forgedHead.signature_b64 = VaultCrypto.bytesToBase64(sigBytes);
          const r3 = await expectSecurityFailure(
            "forged_ed25519",
            makeBackend({ "head.json": VaultCrypto.utf8Bytes(JSON.stringify(forgedHead)) })
          );

          // 4. Missing head_mac on head.json
          const noMacHead = JSON.parse(
            VaultCrypto.utf8DecodeStrict(VaultCrypto.base64ToBytes(input.cleanObjectsB64["head.json"]))
          );
          delete noMacHead.head_mac;
          const r4 = await expectSecurityFailure(
            "missing_head_mac",
            makeBackend({ "head.json": VaultCrypto.utf8Bytes(JSON.stringify(noMacHead)) })
          );

          // 5. Rollback epoch attack (remote epoch 1 < local cursor epoch 2)
          const r5 = await expectSecurityFailure("rollback_epoch", makeBackend(), 2);

          // 6. ../ path traversal inside encrypted & signed manifest
          const r6 = await expectSecurityFailure(
            "manifest_path_traversal",
            makeBackend({
              ["manifests/" + input.evilMid + ".enc"]: VaultCrypto.base64ToBytes(input.evilMctB64),
              "head.json": VaultCrypto.utf8Bytes(JSON.stringify(input.evilHead)),
            })
          );

          process.stdout.write(JSON.stringify({
            baselineFiles: Object.keys(baseline.files).length,
            attacks: [r1, r2, r3, r4, r5, r6],
          }));
        })();
        """
        adv_out = _run_node_json_script(
            adv_js,
            {
                "pwa_pairing_url": pairing["pwa_pairing_url"],
                "cleanObjectsB64": {k: base64.b64encode(v).decode("ascii") for k, v in clean_objects.items()},
                "evilMid": evil_mid,
                "evilMctB64": base64.b64encode(evil_mct).decode("ascii"),
                "evilHead": evil_head,
            },
        )
        self.assertGreater(adv_out["baselineFiles"], 5)
        for attack in adv_out["attacks"]:
            self.assertTrue(attack["rejected"], f"Attack {attack['name']} was NOT rejected: {attack}")

    def test_pairing_registry_revocation_and_deterministic_bundle_export(self) -> None:
        brain_dir = self.tmp / "vault_reg"
        state_dir = self.tmp / "sec_reg"
        sync_folder = self.tmp / "local_sync_target"
        brain_dir.mkdir(parents=True, exist_ok=True)

        init_sync(
            str(sync_folder),
            backend_type="local_folder",
            brain_dir=brain_dir,
            passphrase="Registry-Test-Passphrase-123!",
            state_dir=state_dir,
        )

        paired = create_mobile_vault_pairing(
            brain_dir=brain_dir,
            label="iPad Air",
            sync_endpoint_override="https://sync.example.com/vault",
            state_dir=state_dir,
        )
        listing1 = list_mobile_vault_pairings(brain_dir=brain_dir, state_dir=state_dir)
        self.assertEqual(listing1["active_count"], 1)
        self.assertEqual(listing1["pairings"][0]["pairing_id"], paired["pairing_id"])

        rev_res = revoke_mobile_vault_pairing(paired["pairing_id"], brain_dir=brain_dir, state_dir=state_dir)
        self.assertEqual(rev_res["status"], "revoked")
        self.assertTrue(rev_res["drop_token_revoked"])

        listing2 = list_mobile_vault_pairings(brain_dir=brain_dir, state_dir=state_dir)
        self.assertEqual(listing2["active_count"], 0)
        self.assertTrue(listing2["pairings"][0]["revoked"])

        drop_listing = list_mobile_drop_pairings(brain_dir=brain_dir, state_dir=state_dir)
        self.assertEqual(drop_listing["count"], 0)

        # Deterministic bundle export verification
        out1 = self.tmp / "dist1"
        out2 = self.tmp / "dist2"
        exp1 = export_mobile_vault_client_bundle(out1)
        exp2 = export_mobile_vault_client_bundle(out2)
        self.assertEqual(exp1["sha256"], exp2["sha256"])
        self.assertGreaterEqual(exp1["file_count"], 13)

        with zipfile.ZipFile(exp1["zip_path"], "r") as zf:
            names = set(zf.namelist())
        for required_member in (
            "mobile-vault-client/README.md",
            "mobile-vault-client/package.json",
            "mobile-vault-client/capacitor.config.json",
            "mobile-vault-client/www/index.html",
            "mobile-vault-client/www/app.js",
            "mobile-vault-client/www/sw.js",
            "mobile-vault-client/www/lib/vault-crypto.js",
            "mobile-vault-client/www/lib/vault-epistemic-engine.js",
            "mobile-vault-client/ios/App/App/Info.plist",
            "mobile-vault-client/android/app/src/main/AndroidManifest.xml",
        ):
            self.assertIn(required_member, names)

        # Symlink / junction inside mobile client staging tree must fail closed!
        staged_src = self.tmp / "staged_mobile_src"
        shutil.copytree(MOBILE_CLIENT_DIR, staged_src)
        outside_target = self.tmp / "outside_dir"
        outside_target.mkdir(parents=True, exist_ok=True)
        (outside_target / "secret.txt").write_text("do-not-bundle", encoding="utf-8")
        planted_link = staged_src / "www" / "planted_link"
        created_link = False
        try:
            os.symlink(str(outside_target), str(planted_link), target_is_directory=True)
            created_link = True
        except OSError:
            if os.name == "nt":
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(planted_link), str(outside_target)],
                    capture_output=True,
                    check=True,
                )
                created_link = True
        if created_link:
            with self.assertRaises(MobileVaultSecurityError):
                export_mobile_vault_client_bundle(self.tmp / "dist_evil", source_dir=staged_src)


if __name__ == "__main__":
    unittest.main()
