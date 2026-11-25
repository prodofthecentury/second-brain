# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#     "httpx>=0.28,<1",
#     "pillow>=10",
#     "cryptography",
#     "chromadb",
# ]
# ///
"""Comprehensive unit, integration & doc-parity test suite for release tooling, benchmarks, diffusers/PyAV media & owner gates."""

from __future__ import annotations

import base64
from contextlib import ExitStack, redirect_stdout
import hashlib
import hmac
import importlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import httpx
from PIL import Image, PngImagePlugin

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

from atom_mutator import apply_review_transition  # noqa: E402
import cli as sb_cli  # noqa: E402
import creative_media  # noqa: E402
from evaluation_dataset import EvaluationDatasetError, load_dataset, validate_dataset  # noqa: E402
from evidence import find_evidence_anchor  # noqa: E402
from hardware_profile import HardwareCapabilityProfile  # noqa: E402
from lexical_store import rebuild_lexical_index  # noqa: E402
import live_third_party_benchmark as ltb  # noqa: E402
import mcp_server  # noqa: E402
import owner_gates  # noqa: E402
import publish_registries as pub_reg  # noqa: E402
from review_atom import approved_revision_is_current  # noqa: E402
import verify_release_signatures as ver_sig  # noqa: E402


def _make_profile(*, tier_id: str = "tier_3_gpu_accelerated", gpu_backend: str = "cuda", vram_gib: float = 12.0, total_ram_gib: float = 32.0) -> HardwareCapabilityProfile:
    return HardwareCapabilityProfile(
        os_name="Windows",
        os_release="10",
        architecture="AMD64",
        cpu_threads=8,
        total_ram_gib=total_ram_gib,
        available_ram_gib=max(2.0, total_ram_gib / 2.0),
        cpp_runtime_ready=True,
        cpp_runtime_detail="MSVC C++ runtime present",
        gpu_backend=gpu_backend,
        gpu_name="Test Accelerator",
        vram_gib=vram_gib,
        ollama_reachable=False,
        installed_ollama_models=(),
        tier_id=tier_id,
        tier_label=tier_id,
        recommended_generation_model="qwen2.5-coder:7b",
        selected_installed_generation_model=None,
        recommended_embedding_model="nomic-embed-text",
        recommended_retrieval_mode="lexical",
        retrieval_p95_slo_ms=15.0,
        answer_p95_slo_ms=35000.0,
        rationale="Unit test profile",
    )


def _seed_source_and_atom(
    brain_dir: Path,
    *,
    filename: str,
    claim: str,
    evidence: str,
    project: str | None = None,
    approve: bool = True,
) -> Path:
    raw_dir = brain_dir / "0-raw"
    atoms_dir = brain_dir / "2-atoms"
    archive_dir = raw_dir / "archive"
    sidecar_dir = brain_dir / ".sidecar"
    for d in (raw_dir, atoms_dir, archive_dir, sidecar_dir):
        d.mkdir(parents=True, exist_ok=True)
    src_name = f"src-{filename}"
    (raw_dir / src_name).write_text(f"Primary engineering reference: {evidence}\n", encoding="utf-8")
    ctx_yaml = f"contexts:\n  - {project}\n" if project else "contexts: []\n"
    draft = (
        "---\n"
        f"id: {Path(filename).stem}\n"
        "type: atom\n"
        "memory_type: semantic\n"
        "epistemic_label: claim\n"
        "created: 2026-11-22\n"
        "last-reviewed: 2026-11-22\n"
        "confidence: 0.90\n"
        "importance: 0.85\n"
        "sources:\n"
        f"  - 0-raw/{src_name}\n"
        f"{ctx_yaml}"
        "review_status: pending\n"
        "claim_state: candidate\n"
        "citation_integrity: missing\n"
        "---\n\n"
        f"## Claim\n{claim}\n\n"
        f"## Evidence\n{evidence}\n\n"
        "## Why It Matters\nVerification test fixture.\n"
    )
    anchor = find_evidence_anchor(draft, str(brain_dir))
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
            actor="unit-test-owner",
            decision_log_path=str(archive_dir / "decision-record.jsonl"),
            brain_dir=str(brain_dir),
            atoms_dir=str(atoms_dir),
        )
    return atom_path


class PublishRegistriesTests(unittest.TestCase):
    """Complete unit & transport tests for `scripts/publish_registries.py`."""

    _ALL_CRED_KEYS = (
        "PYPI_API_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "VSCE_PAT",
        "OVSX_PAT",
        "CWS_CLIENT_ID",
        "CWS_CLIENT_SECRET",
        "CWS_REFRESH_TOKEN",
        "CWS_EXTENSION_ID",
        "EDGE_PRODUCT_ID",
        "EDGE_CLIENT_ID",
        "EDGE_API_KEY",
        "AMO_JWT_ISSUER",
        "AMO_JWT_SECRET",
        "HOMEBREW_TAP_TOKEN",
        "GITHUB_TOKEN",
        "WINGET_GITHUB_TOKEN",
    )

    def test_check_credentials_oidc_and_secret_non_leakage(self) -> None:
        clean_env = {k: v for k, v in os.environ.items() if k not in self._ALL_CRED_KEYS}
        with patch.dict(os.environ, clean_env, clear=True):
            none_rep = pub_reg.check_registry_credentials()
            self.assertEqual(len(none_rep), 9)
            self.assertFalse(any(v["ready"] for v in none_rep.values()))

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = pub_reg.main(["check-credentials", "--json"])
            self.assertEqual(rc, 0)
            parsed = json.loads(buf.getvalue())
            self.assertFalse(parsed["pypi"]["ready"])

        # OIDC trusted publisher enables PyPI without PYPI_API_TOKEN
        with patch.dict(
            os.environ,
            {
                **clean_env,
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.actions.example/oidc",
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc-secret-value-never-print",
            },
            clear=True,
        ):
            oidc_rep = pub_reg.check_registry_credentials()
            self.assertTrue(oidc_rep["pypi"]["ready"])
            self.assertNotIn("oidc-secret-value-never-print", json.dumps(oidc_rep))

        # Full credential set marks all 8 registries ready while never echoing secret strings
        full_env = {**clean_env, **{k: f"secret-val-{k.lower()}" for k in self._ALL_CRED_KEYS}}
        with patch.dict(os.environ, full_env, clear=True):
            full_rep = pub_reg.check_registry_credentials()
            self.assertTrue(all(v["ready"] for v in full_rep.values()))
            serialized = json.dumps(full_rep)
            for k in self._ALL_CRED_KEYS:
                self.assertNotIn(f"secret-val-{k.lower()}", serialized)

    def test_build_amo_jwt_hs256_verification(self) -> None:
        token = pub_reg.build_amo_jwt("user:998877:11", "super-secret-amo-key", now_ts=1_700_000_000)
        parts = token.split(".")
        self.assertEqual(len(parts), 3)

        def _b64dec(seg: str) -> bytes:
            pad = "=" * ((4 - len(seg) % 4) % 4)
            return base64.urlsafe_b64decode(seg + pad)

        header = json.loads(_b64dec(parts[0]).decode("utf-8"))
        payload = json.loads(_b64dec(parts[1]).decode("utf-8"))
        self.assertEqual(header, {"alg": "HS256", "typ": "JWT"})
        self.assertEqual(payload["iss"], "user:998877:11")
        self.assertEqual(payload["iat"], 1_700_000_000)
        self.assertEqual(payload["exp"], 1_700_000_240)
        self.assertEqual(len(payload["jti"]), 32)

        expected_sig = hmac.new(
            b"super-secret-amo-key",
            f"{parts[0]}.{parts[1]}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        self.assertTrue(hmac.compare_digest(_b64dec(parts[2]), expected_sig))

    def test_chrome_edge_firefox_and_homebrew_publish_transports_and_failures(self) -> None:
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp)
            zip_file = tpath / "clipper.zip"
            with zipfile.ZipFile(zip_file, "w") as zf:
                zf.writestr("manifest.json", json.dumps({"manifest_version": 3, "name": "Test"}))
            xpi_file = tpath / "clipper.xpi"
            with zipfile.ZipFile(xpi_file, "w") as zf:
                zf.writestr("manifest.json", json.dumps({"manifest_version": 3, "name": "Test"}))
            cask_file = tpath / "second-brain.rb"
            cask_file.write_text("cask 'second-brain' do\nend\n", encoding="utf-8")
            formula_file = tpath / "formula-second-brain.rb"
            formula_file.write_text("class SecondBrain < Formula\nend\n", encoding="utf-8")
            readme_file = tpath / "TAP_README.md"
            readme_file.write_text("# Tap\n", encoding="utf-8")

            # Missing zip/creds fail closed
            with self.assertRaises(pub_reg.RegistryPublishError):
                pub_reg.publish_chrome_web_store(tpath / "missing.zip")
            with self.assertRaises(pub_reg.RegistryPublishError):
                pub_reg.publish_edge_addons(tpath / "missing.zip")
            with self.assertRaises(pub_reg.RegistryPublishError):
                pub_reg.publish_firefox_amo(tpath / "missing.xpi")

            cws_calls: list[tuple[str, str]] = []

            def _cws_handler(req: httpx.Request) -> httpx.Response:
                cws_calls.append((req.method, str(req.url)))
                if str(req.url).endswith("/token"):
                    return httpx.Response(200, json={"access_token": "cws-access-123"})
                if req.method == "PUT":
                    self.assertEqual(req.headers.get("Authorization"), "Bearer cws-access-123")
                    return httpx.Response(200, json={"uploadState": "SUCCESS"})
                if req.method == "POST" and "/publish" in str(req.url):
                    return httpx.Response(200, json={"status": ["OK"]})
                return httpx.Response(404)

            cws_env = {
                "CWS_CLIENT_ID": "cid",
                "CWS_CLIENT_SECRET": "csec",
                "CWS_REFRESH_TOKEN": "cref",
                "CWS_EXTENSION_ID": "abcdefghijklmnopabcdefghijklmnop",
            }
            with patch.dict(os.environ, cws_env):
                res_cws = pub_reg.publish_chrome_web_store(
                    zip_file, transport=httpx.MockTransport(_cws_handler)
                )
                self.assertEqual(res_cws["registry"], "chrome_web_store")
                self.assertEqual(res_cws["status"], ["OK"])
                self.assertEqual(len(cws_calls), 3)

                # Upload FAILURE rejected
                fail_up = httpx.MockTransport(
                    lambda r: httpx.Response(200, json={"access_token": "tok"})
                    if "/token" in str(r.url)
                    else httpx.Response(200, json={"uploadState": "FAILURE", "itemError": ["bad_manifest"]})
                )
                with self.assertRaises(pub_reg.RegistryPublishError):
                    pub_reg.publish_chrome_web_store(zip_file, transport=fail_up)

            # Edge Add-ons happy & error paths
            edge_env = {
                "EDGE_PRODUCT_ID": "prod-1",
                "EDGE_CLIENT_ID": "ecid",
                "EDGE_API_KEY": "eapi",
            }
            edge_ok = httpx.MockTransport(
                lambda r: httpx.Response(202, headers={"Location": "https://api.addons.microsoftedge.microsoft.com/ops/99"})
                if r.url.path.endswith("/submissions")
                else httpx.Response(200, json={"status": "Succeeded"})
            )
            with patch.dict(os.environ, edge_env):
                res_edge = pub_reg.publish_edge_addons(zip_file, transport=edge_ok)
                self.assertEqual(res_edge["registry"], "edge_addons")
                self.assertIn("/ops/99", res_edge["operation_location"])

                with self.assertRaises(pub_reg.RegistryPublishError):
                    pub_reg.publish_edge_addons(zip_file, transport=httpx.MockTransport(lambda _r: httpx.Response(500)))

            # Firefox AMO happy & error paths
            amo_env_missing_slug = {"AMO_JWT_ISSUER": "issuer-1", "AMO_JWT_SECRET": "secret-1"}
            amo_env = {**amo_env_missing_slug, "AMO_ADDON_SLUG": "second-brain-clipper"}

            def _amo_handler(req: httpx.Request) -> httpx.Response:
                if not req.headers.get("Authorization", "").startswith("JWT "):
                    return httpx.Response(401)
                if req.method == "POST" and "/versions/" in str(req.url):
                    return httpx.Response(201, json={"id": 99})
                if req.method == "POST":
                    return httpx.Response(201, json={"uuid": "amo-uuid-42"})
                return httpx.Response(200, json={"uuid": "amo-uuid-42", "processed": True, "valid": True})

            amo_ok = httpx.MockTransport(_amo_handler)
            with patch.dict(os.environ, amo_env_missing_slug, clear=False):
                os.environ.pop("AMO_ADDON_SLUG", None)
                with self.assertRaises(pub_reg.RegistryPublishError):
                    pub_reg.publish_firefox_amo(xpi_file, transport=amo_ok)
            with patch.dict(os.environ, amo_env):
                res_amo = pub_reg.publish_firefox_amo(xpi_file, transport=amo_ok)
                self.assertEqual(res_amo["upload_uuid"], "amo-uuid-42")
                self.assertEqual(res_amo["version_id"], 99)
                with self.assertRaises(pub_reg.RegistryPublishError):
                    pub_reg.publish_firefox_amo(xpi_file, transport=httpx.MockTransport(lambda _r: httpx.Response(400)))

            # Homebrew Tap slug traversal security check + existing sha vs new file PUT
            for bad_slug in ("../evil/repo", "noslash", "a/b/c", "owner/..hidden"):
                with self.assertRaises(pub_reg.RegistryPublishError):
                    pub_reg.publish_homebrew_tap_via_github_api(
                        tap_repo=bad_slug,
                        cask_path=cask_file,
                        token="gh-tok",
                    )

            gh_puts: list[dict] = []

            def _gh_handler(req: httpx.Request) -> httpx.Response:
                if req.method == "GET":
                    if req.url.path.endswith("Casks/second-brain.rb"):
                        return httpx.Response(200, json={"sha": "existing-blob-sha-1"})
                    return httpx.Response(404, json={"message": "Not Found"})
                if req.method == "PUT":
                    gh_puts.append(json.loads(req.content.decode("utf-8")))
                    return httpx.Response(201, json={"content": {"sha": "new-sha"}})
                return httpx.Response(405)

            res_tap = pub_reg.publish_homebrew_tap_via_github_api(
                tap_repo="owner/homebrew-second-brain",
                cask_path=cask_file,
                formula_path=formula_file,
                readme_path=readme_file,
                token="gh-tok",
                version="1.0.0",
                transport=httpx.MockTransport(_gh_handler),
            )
            self.assertEqual(res_tap["updated_files"], ["Casks/second-brain.rb", "Formula/second-brain.rb", "README.md"])
            self.assertEqual(gh_puts[0].get("sha"), "existing-blob-sha-1")
            self.assertNotIn("sha", gh_puts[1])

            # WinGet validation missing dir, missing winget CLI, and skip-missing-creds CLI
            with self.assertRaises(pub_reg.RegistryPublishError):
                pub_reg.validate_winget_manifests_locally(tpath / "no-winget-dir")
            wg_dir = tpath / "wg"
            wg_dir.mkdir()
            with patch.object(pub_reg.shutil, "which", return_value=None):
                wg_res = pub_reg.validate_winget_manifests_locally(wg_dir)
                self.assertEqual(wg_res["reason"], "winget_cli_not_on_host")

            clean_creds = {k: v for k, v in os.environ.items() if k not in self._ALL_CRED_KEYS}
            out_buf = io.StringIO()
            with patch.dict(os.environ, clean_creds, clear=True), redirect_stdout(out_buf):
                rc_skip = pub_reg.main([
                    "publish-browser-stores",
                    "--chromium-zip", str(zip_file),
                    "--firefox-xpi", str(xpi_file),
                    "--skip-missing-creds",
                ])
            self.assertEqual(rc_skip, 0)
            skip_json = json.loads(out_buf.getvalue())
            self.assertTrue(skip_json["chrome_web_store"]["skipped"])


class VerifyReleaseSignaturesTests(unittest.TestCase):
    """Real Windows PowerShell Authenticode + macOS/env signature verification tests."""

    def test_windows_authenticode_unsigned_self_signed_and_tampered_real_powershell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp)
            missing = ver_sig.verify_windows_authenticode(tpath / "does-not-exist.exe")
            self.assertFalse(missing["ok"])
            self.assertEqual(missing["reason"], "file_not_found")

            if os.name != "nt":
                self.skipTest("Real Windows Authenticode PowerShell test requires Windows host.")
            pwsh = shutil.which("pwsh") or shutil.which("powershell")
            if not pwsh:
                self.skipTest("PowerShell unavailable on Windows host.")

            target_ps1 = tpath / "sample_unsigned.ps1"
            target_ps1.write_text("Write-Output 'Second Brain Release Test'\n", encoding="utf-8")

            unsigned_res = ver_sig.verify_windows_authenticode(target_ps1, require_trusted_ca=True)
            self.assertFalse(unsigned_res["ok"])
            self.assertEqual(unsigned_res["reason"], "unsigned_binary")

            # Mint an ephemeral CurrentUser code-signing cert, sign target_ps1, test strict/permissive, then tamper!
            thumbprint = ""
            create_cmd = (
                "$c = New-SelfSignedCertificate -Type CodeSigningCert "
                "-Subject 'CN=SecondBrainTestSigner' -CertStoreLocation 'Cert:\\CurrentUser\\My'; "
                "Set-AuthenticodeSignature -FilePath $env:SB_TARGET -Certificate $c | Out-Null; "
                "Write-Output $c.Thumbprint"
            )
            env = {**os.environ, "SB_TARGET": str(target_ps1)}
            try:
                cproc = subprocess.run(
                    [pwsh, "-NoProfile", "-NonInteractive", "-Command", create_cmd],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=30,
                    check=True,
                )
                thumbprint = cproc.stdout.strip().splitlines()[-1].strip()
                self.assertTrue(len(thumbprint) >= 16)

                strict_res = ver_sig.verify_windows_authenticode(target_ps1, require_trusted_ca=True)
                self.assertFalse(strict_res["ok"])
                self.assertTrue(strict_res["self_signed"])
                self.assertEqual(strict_res["reason"], "self_signed_certificate_not_trusted_ca")

                permissive_res = ver_sig.verify_windows_authenticode(target_ps1, require_trusted_ca=False)
                self.assertTrue(permissive_res["ok"])
                self.assertTrue(permissive_res["self_signed"])

                # Flip one byte before the signature block -> PowerShell Get-AuthenticodeSignature reports HashMismatch
                signed_txt = target_ps1.read_text(encoding="utf-8")
                target_ps1.write_text("# tampered\n" + signed_txt, encoding="utf-8")
                tampered_res = ver_sig.verify_windows_authenticode(target_ps1, require_trusted_ca=False)
                self.assertFalse(tampered_res["ok"])
                self.assertEqual(tampered_res["reason"], "hash_mismatch")
            finally:
                if thumbprint:
                    rm_cmd = "Remove-Item -Path ('Cert:\\CurrentUser\\My\\' + $env:SB_THUMB) -ErrorAction SilentlyContinue"
                    subprocess.run(
                        [pwsh, "-NoProfile", "-NonInteractive", "-Command", rm_cmd],
                        capture_output=True,
                        env={**os.environ, "SB_THUMB": thumbprint},
                        timeout=15,
                        check=False,
                    )

    def test_trusted_ca_mock_edges_macos_paths_and_env_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp)
            exe_file = tpath / "SecondBrainSetup.exe"
            exe_file.write_bytes(b"MZ-stub")

            def _mock_ps(payload: dict) -> subprocess.CompletedProcess:
                return subprocess.CompletedProcess(["powershell"], 0, json.dumps(payload), "")

            with (
                patch.object(ver_sig.shutil, "which", return_value="pwsh"),
                patch.object(
                    ver_sig.subprocess,
                    "run",
                    return_value=_mock_ps({
                        "status": "Valid",
                        "status_message": "Signature verified.",
                        "subject": "CN=Second Brain Publisher",
                        "issuer": "CN=DigiCert Trusted G4 Code Signing",
                        "timestamper": "CN=DigiCert Timestamp Responder",
                    }),
                ),
            ):
                trusted = ver_sig.verify_windows_authenticode(exe_file, require_trusted_ca=True)
                self.assertTrue(trusted["ok"])
                self.assertEqual(trusted["reason"], "trusted_authenticode_valid")

            with (
                patch.object(ver_sig.shutil, "which", return_value="pwsh"),
                patch.object(
                    ver_sig.subprocess,
                    "run",
                    return_value=_mock_ps({
                        "status": "Valid",
                        "status_message": "Signature verified.",
                        "subject": "CN=Second Brain Publisher",
                        "issuer": "CN=DigiCert Trusted G4 Code Signing",
                        "timestamper": "",
                    }),
                ),
            ):
                no_ts = ver_sig.verify_windows_authenticode(exe_file, require_trusted_ca=True)
                self.assertFalse(no_ts["ok"])
                self.assertEqual(no_ts["reason"], "missing_rfc3161_timestamp")

            # macOS notarization paths
            dmg_file = tpath / "SecondBrain.dmg"
            dmg_file.write_bytes(b"dmg")
            with patch.object(ver_sig.sys, "platform", "linux"):
                non_mac = ver_sig.verify_macos_notarization(dmg_file)
                self.assertFalse(non_mac["ok"])
                self.assertEqual(non_mac["reason"], "macos_host_required_for_codesign_stapler_spctl")

            with (
                patch.object(ver_sig.sys, "platform", "darwin"),
                patch.object(
                    ver_sig.subprocess,
                    "run",
                    side_effect=[
                        subprocess.CompletedProcess(["codesign"], 0, "ok", ""),
                        subprocess.CompletedProcess(["xcrun"], 0, "ok", ""),
                        subprocess.CompletedProcess(["spctl"], 0, "accepted", ""),
                    ],
                ),
            ):
                mac_ok = ver_sig.verify_macos_notarization(dmg_file, require_notarized=True)
                self.assertTrue(mac_ok["ok"])
                self.assertEqual(mac_ok["reason"], "notarized_and_gatekeeper_accepted")

            # Credential readiness modes & main CLI
            clean_env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("SECOND_BRAIN_", "WINDOWS_PFX_", "CSC_", "MAC_CSC_", "APPLE_"))
            }
            with patch.dict(
                os.environ,
                {
                    **clean_env,
                    "SECOND_BRAIN_CERT_THUMBPRINT": "ABCDEF012345",
                    "CSC_LINK": "/tmp/devid.p12",
                    "APPLE_API_KEY": "key",
                    "APPLE_API_KEY_ID": "KID",
                    "APPLE_API_ISSUER": "ISS",
                },
                clear=True,
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc_env = ver_sig.main(["--check-env-only"])
                self.assertEqual(rc_env, 0)
                rep = json.loads(buf.getvalue())
                self.assertTrue(rep["windows_modes"]["cert_store_thumbprint"])
                self.assertTrue(rep["macos_modes"]["app_store_connect_api_key"])

            empty_dist = tpath / "dist_empty"
            empty_dist.mkdir()
            self.assertEqual(ver_sig.main(["--dist-dir", str(empty_dist), "--json"]), 1)


class LiveThirdPartyBenchmarkTests(unittest.TestCase):
    """Unit & mini-vault integration tests for `scripts/live_third_party_benchmark.py`."""

    def test_metrics_and_mini_vault_benchmark_execution(self) -> None:
        self.assertEqual(ltb._pct([10.0, 20.0, 30.0], 50), 20.0)
        self.assertEqual(ltb._pct([], 95), 0.0)
        self.assertEqual(ltb._mrr(["a.md", "b.md"], {"b.md"}), 0.5)
        self.assertEqual(ltb._mrr(["a.md"], {"z.md"}), 0.0)
        self.assertEqual(ltb._ndcg_at_k(["a.md"], {"a.md"}, 5), 1.0)
        self.assertIn("sodium", ltb._tokenize("The sodium battery record"))

        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp)
            brain = tpath / "brain"
            atoms_dir = brain / "2-atoms"
            sidecar_dir = brain / ".sidecar"
            chroma_dir = brain / ".chroma"
            chroma_dir.mkdir(parents=True, exist_ok=True)

            log_atom = _seed_source_and_atom(
                brain,
                filename="structured-logging.md",
                claim="Logs should be structured JSON to facilitate observability.",
                evidence="Logs should be structured JSON to facilitate observability.",
                project="project:alpha",
                approve=True,
            )
            _seed_source_and_atom(
                brain,
                filename="fail-graceful.md",
                claim="Services must degrade gracefully on database timeout.",
                evidence="Services must degrade gracefully on database timeout.",
                project="project:beta",
                approve=True,
            )
            _seed_source_and_atom(
                brain,
                filename="pending-sodium.md",
                claim="Sodium ion cells hit $56/kWh in unreviewed benchmark draft.",
                evidence="Sodium ion cells hit $56/kWh in unreviewed benchmark draft.",
                project="project:alpha",
                approve=False,
            )

            vault_atoms, unapproved = ltb._load_vault_atoms(atoms_dir)
            self.assertEqual(len(vault_atoms), 3)
            self.assertEqual(unapproved, {"pending-sodium.md"})

            rebuild_lexical_index(str(atoms_dir), str(sidecar_dir / "lexical.sqlite3"))

            log_sha = hashlib.sha256(log_atom.read_bytes()).hexdigest()
            mini_ds = {
                "schema_version": 2,
                "dataset_id": "mini-benchmark",
                "dataset_version": "1.0.0",
                "status": "pending_owner_review",
                "draft_provenance": {"drafted_by": "test", "date": "2026-11-23", "scope": "unit test"},
                "purpose": "Mini isolated benchmark test",
                "queries": [
                    {
                        "id": "q_pos_1",
                        "query": "structured JSON logs observability",
                        "category": "observability",
                        "split": "regression",
                        "project_scope": "project:alpha",
                        "answerability": "answerable",
                        "answer_mode": "standard",
                        "expected_atom_ids": ["structured-logging.md"],
                        "acceptable_atom_ids": [],
                        "candidate_relevance": ["structured-logging.md"],
                        "expected_atom_revisions": [{"atom_id": "structured-logging.md", "revision_sha256": log_sha}],
                        "expected_claims": [{"text": "Logs should be structured JSON to facilitate observability.", "supported_by": ["structured-logging.md"]}],
                        "expected_behavior": "answer_with_evidence",
                        "trap_reason": None,
                    },
                    {
                        "id": "q_trap_pending",
                        "query": "Sodium ion $56/kWh cell cost",
                        "category": "unapproved_draft",
                        "split": "development",
                        "project_scope": "project:alpha",
                        "answerability": "unanswerable",
                        "answer_mode": "standard",
                        "expected_atom_ids": [],
                        "acceptable_atom_ids": [],
                        "candidate_relevance": [],
                        "expected_atom_revisions": [],
                        "expected_claims": [],
                        "expected_behavior": "abstain",
                        "trap_reason": "Pending atom must never leak",
                    },
                    {
                        "id": "q_trap_cross",
                        "query": "database timeout graceful degrade",
                        "category": "cross_project_trap",
                        "split": "holdout",
                        "project_scope": "project:alpha",
                        "answerability": "unanswerable",
                        "answer_mode": "standard",
                        "expected_atom_ids": [],
                        "acceptable_atom_ids": [],
                        "candidate_relevance": [],
                        "expected_atom_revisions": [],
                        "expected_claims": [],
                        "expected_behavior": "abstain",
                        "trap_reason": "project:beta atom must not leak into project:alpha",
                    },
                ],
            }
            ds_path = tpath / "mini_ds.json"
            ds_path.write_text(json.dumps(mini_ds), encoding="utf-8")
            out_path = tpath / "out" / "mini_bench.json"

            import recall as recall_mod
            import ollama_utils as ou_mod

            def _det_embed(text: str, model: str | None = None) -> list[float]:
                digest = hashlib.sha256(str(text).encode("utf-8")).digest()
                return [round((b - 128) / 128.0, 4) for b in digest[:8]]

            # Build real Chroma collection so rebuild_vectors/search_hybrid_rrf execute cleanly
            import vector_store as vs_mod
            col = vs_mod.get_vector_collection(str(chroma_dir), name="atoms", create=True)
            vs_mod.close_vector_collection(col)

            # Minimal in-process stubs for optional external packages only if absent
            extra_mods: dict[str, types.ModuleType] = {}
            for opt_mod in ("faiss", "mem0", "rank_bm25"):
                if opt_mod not in sys.modules:
                    try:
                        importlib.import_module(opt_mod)
                    except ImportError:
                        stub = types.ModuleType(opt_mod)
                        if opt_mod == "faiss":
                            class _FakeFaissIdx:
                                def __init__(self, _d, _m, _metric):
                                    self.hnsw = types.SimpleNamespace(efConstruction=64, efSearch=32)
                                    self._n = 0
                                def add(self, mat):
                                    self._n = len(mat)
                                def search(self, _q, k):
                                    return ([[0.9] * min(k, self._n)], [list(range(min(k, self._n)))])
                            stub.METRIC_INNER_PRODUCT = 0
                            stub.IndexHNSWFlat = _FakeFaissIdx
                        elif opt_mod == "mem0":
                            class _FakeMem0:
                                def __init__(self):
                                    self._rows = []
                                @classmethod
                                def from_config(cls, _cfg):
                                    return cls()
                                def add(self, text, user_id="", metadata=None, infer=False):
                                    self._rows.append({"memory": text, "metadata": metadata or {}})
                                def search(self, query, filters=None, limit=5):
                                    q_toks = set(query.lower().split())
                                    scored = sorted(
                                        self._rows,
                                        key=lambda r: len(q_toks & set(r["memory"].lower().split())),
                                        reverse=True,
                                    )
                                    return {"results": scored[:limit]}
                            stub.Memory = _FakeMem0
                        else:
                            class _FakeBM25:
                                def __init__(self, corpus):
                                    self._c = [set(c) for c in corpus]
                                def get_scores(self, qtoks):
                                    qs = set(qtoks)
                                    return [float(len(qs & doc)) for doc in self._c]
                            stub.BM25Okapi = _FakeBM25
                        extra_mods[opt_mod] = stub

            with ExitStack() as stack:
                for mname, mobj in extra_mods.items():
                    stack.enter_context(patch.dict(sys.modules, {mname: mobj}))
                stack.enter_context(patch.object(ltb, "ATOMS_DIR", str(atoms_dir)))
                stack.enter_context(patch.object(ltb, "CHROMA_PATH", str(chroma_dir)))
                stack.enter_context(patch.object(ltb, "LEXICAL_INDEX_PATH", str(sidecar_dir / "lexical.sqlite3")))
                stack.enter_context(patch.object(recall_mod, "ATOMS_DIR", str(atoms_dir)))
                stack.enter_context(patch.object(recall_mod, "CHROMA_PATH", str(chroma_dir)))
                stack.enter_context(patch.object(recall_mod, "LEXICAL_INDEX_PATH", str(sidecar_dir / "lexical.sqlite3")))
                stack.enter_context(patch.object(ou_mod, "embed_text", side_effect=_det_embed))
                stack.enter_context(patch.object(ltb.ollama_utils, "embed_text", side_effect=_det_embed))
                stack.enter_context(patch.object(ltb, "approved_revision_is_current", lambda p, atoms_dir=str(atoms_dir): approved_revision_is_current(str(p), atoms_dir=atoms_dir, brain_dir=str(brain))))
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = ltb.main(["--dataset", str(ds_path), "--output", str(out_path), "--json"])

            self.assertEqual(rc, 0)
            self.assertTrue(out_path.is_file())
            report = json.loads(out_path.read_text(encoding="utf-8"))
            sb_ver = report["systems"]["second_brain_verified"]
            self.assertEqual(sb_ver["answerable_expected_recall_at_5_pct"], 100.0)
            self.assertEqual(sb_ver["unapproved_candidate_leak_queries"], 0)
            self.assertEqual(sb_ver["cross_project_leak_pct"], 0.0)
            self.assertEqual(sb_ver["abstention_accuracy_pct"], 100.0)


class CreativeMediaDiffusersAndPyAVTests(unittest.TestCase):
    """Tests for `_generate_via_diffusers_local()` and bundled PyAV `libx264` MP4 encoding."""

    def test_diffusers_local_security_device_routing_and_persistence(self) -> None:
        # 1. Missing model identifier
        with self.assertRaises(creative_media.CreativeMediaError):
            creative_media._generate_via_diffusers_local(
                prompt="test", negative_prompt="", model_name=None,
                width=256, height=256, seed=1, steps=4, cfg_scale=5.0,
                output_format="png", gpu_backend="cpu",
            )

        # 2. Remote URL / UNC / traversal / shell metacharacters blocked
        for bad_url in ("https://huggingface.co/model", "http://127.0.0.1/model", "file:///c:/model"):
            with self.assertRaises(creative_media.CreativeMediaSecurityError):
                creative_media._generate_via_diffusers_local(
                    prompt="test", negative_prompt="", model_name=bad_url,
                    width=256, height=256, seed=1, steps=4, cfg_scale=5.0,
                    output_format="png", gpu_backend="cpu",
                )
        for bad_id in (r"\\server\share\model", "../escape-model", "bad;model|name"):
            with self.assertRaises(creative_media.CreativeMediaSecurityError):
                creative_media._generate_via_diffusers_local(
                    prompt="test", negative_prompt="", model_name=bad_id,
                    width=256, height=256, seed=1, steps=4, cfg_scale=5.0,
                    output_format="png", gpu_backend="cpu",
                )

        # 3. Missing diffusers/torch fails closed gracefully
        real_import = __import__
        def _missing_torch(name, *a, **kw):
            if name in ("torch", "diffusers"):
                raise ImportError("missing")
            return real_import(name, *a, **kw)

        with patch("builtins.__import__", side_effect=_missing_torch):
            with self.assertRaises(creative_media.CreativeMediaError):
                creative_media._generate_via_diffusers_local(
                    prompt="test", negative_prompt="", model_name="local/sdxl-turbo",
                    width=256, height=256, seed=1, steps=4, cfg_scale=5.0,
                    output_format="png", gpu_backend="cpu",
                )

        # 4. CUDA, MPS, and CPU device/dtype routing + local_files_only=True + Pillow EXIF/tEXt stripping
        recorded_calls: list[dict] = []

        class _FakeGen:
            def __init__(self, device: str = "cpu") -> None:
                self.device = device
                self.seed = None
            def manual_seed(self, seed: int) -> _FakeGen:
                self.seed = seed
                return self

        class _FakePipe:
            def to(self, dev: str) -> _FakePipe:
                recorded_calls[-1]["to_device"] = dev
                return self
            def __call__(self, **kwargs):
                recorded_calls[-1]["pipe_call"] = kwargs
                img = Image.new("RGB", (kwargs["width"], kwargs["height"]), (30, 90, 180))
                info = PngImagePlugin.PngInfo()
                info.add_text("parameters", "LEAKED_PROMPT_TEXT")
                buf = io.BytesIO()
                img.save(buf, format="PNG", pnginfo=info)
                buf.seek(0)
                loaded = Image.open(buf)
                return types.SimpleNamespace(images=[loaded])

        class _FakeAutoPipe:
            @classmethod
            def from_pretrained(cls, model_id: str, **kwargs):
                recorded_calls.append({"model_id": model_id, "pretrained_kwargs": kwargs})
                return _FakePipe()

        def _fake_torch_mod(*, cuda_ok: bool, mps_ok: bool):
            return types.SimpleNamespace(
                float16="float16_token",
                float32="float32_token",
                cuda=types.SimpleNamespace(is_available=lambda: cuda_ok),
                backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: mps_ok)),
                Generator=_FakeGen,
            )

        fake_diffusers = types.SimpleNamespace(AutoPipelineForText2Image=_FakeAutoPipe)
        for gpu_bk, cuda_ok, mps_ok, exp_dev, exp_dtype, fmt in (
            ("cuda", True, False, "cuda", "float16_token", "png"),
            ("metal", False, True, "mps", "float16_token", "webp"),
            ("cpu", False, False, "cpu", "float32_token", "png"),
        ):
            recorded_calls.clear()
            with patch.dict(sys.modules, {"torch": _fake_torch_mod(cuda_ok=cuda_ok, mps_ok=mps_ok), "diffusers": fake_diffusers}):
                out_bytes = creative_media._generate_via_diffusers_local(
                    prompt="Diagram", negative_prompt="blurry", model_name="local/flux-mini",
                    width=256, height=256, seed=42, steps=120, cfg_scale=7.0,
                    output_format=fmt, gpu_backend=gpu_bk,
                )
            self.assertEqual(recorded_calls[0]["pretrained_kwargs"]["local_files_only"], True)
            self.assertEqual(recorded_calls[0]["pretrained_kwargs"]["torch_dtype"], exp_dtype)
            self.assertEqual(recorded_calls[0]["to_device"], exp_dev)
            self.assertEqual(recorded_calls[0]["pipe_call"]["num_inference_steps"], 80)  # clamped to 80
            self.assertEqual(recorded_calls[0]["pipe_call"]["generator"].seed, 42)
            self.assertNotIn(b"LEAKED_PROMPT_TEXT", out_bytes)

        # 5. Full generate_local_image(backend="diffusers_local") persistence
        with tempfile.TemporaryDirectory() as tmp:
            brain_dir = os.path.join(tmp, "brain")
            with patch.dict(sys.modules, {"torch": _fake_torch_mod(cuda_ok=True, mps_ok=False), "diffusers": fake_diffusers}):
                art = creative_media.generate_local_image(
                    "Local neural architecture blueprint",
                    brain_dir=brain_dir,
                    backend="diffusers_local",
                    model_name="local/flux-mini",
                    width=256,
                    height=256,
                    seed=77,
                    hardware_profile=_make_profile(tier_id="tier_3_gpu_accelerated", gpu_backend="cuda"),
                )
            self.assertEqual(art.backend_used, "diffusers_local")
            self.assertEqual(art.encoder_used, "diffusers_sanitized_png")
            self.assertTrue(os.path.isfile(art.file_path))

    def test_pyav_libx264_mp4_encoding_and_decode(self) -> None:
        self.assertTrue(creative_media._is_pyav_libx264_available())
        with tempfile.TemporaryDirectory() as tmp:
            brain_dir = os.path.join(tmp, "brain")
            vid = creative_media.generate_local_video(
                "H264 libx264 local video encode test",
                brain_dir=brain_dir,
                backend="ffmpeg_slideshow_local",
                output_format="mp4",
                width=256,
                height=192,
                frames=4,
                fps=8,
                seed=123,
                hardware_profile=_make_profile(tier_id="tier_2_cpu_balanced", gpu_backend="cpu", total_ram_gib=16.0),
            )
            self.assertIn(vid.encoder_used, {"pyav_libx264", "ffmpeg_libx264"})
            info = creative_media.inspect_mp4_container(Path(vid.file_path).read_bytes())
            self.assertEqual(info["codec_tag"], "avc1")
            self.assertEqual(info["width"], 256)
            self.assertEqual(info["height"], 192)


class OwnerGatesAndDocParityTests(unittest.TestCase):
    """Tests for `scripts/owner_gates.py`, `second-brain gates`, and live doc-to-code parity."""

    def test_owner_gates_audit_and_fail_closed_dataset_signoff(self) -> None:
        report = owner_gates.collect_owner_gates_status()
        self.assertEqual(report["dataset_gate"]["total_queries"], 90)
        self.assertEqual(report["dataset_gate"]["v2_baseline_query_count"], 30)
        self.assertEqual(report["dataset_gate"]["v3_expansion_query_count"], 60)
        expected_pending = (
            "sodium-ion-battery-cost-record.md",
            "code-reviews-as-mentorship.md",
            "continuous-integration-deployment.md",
        )
        if report["dataset_gate"].get("validation_mode") == "code_only_schema":
            self.assertEqual(report["candidate_atoms_gate"]["unapproved_count"], 0)
        else:
            self.assertGreaterEqual(report["candidate_atoms_gate"]["unapproved_count"], 3)
            cand_names = {c["filename"] for c in report["candidate_atoms_gate"]["candidates"]}
            for name in expected_pending:
                self.assertIn(name, cand_names)

        # CLI `second-brain gates status --json`
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(sb_cli.main(["gates", "status", "--json"]), 0)
        cli_rep = json.loads(buf.getvalue())
        self.assertEqual(cli_rep["dataset_gate"]["total_queries"], 90)

        # Isolated dataset approval tests (never modifies real docs/retrieval-evaluation-proposed.json)
        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp)
            vault = tpath / "brain"
            _seed_source_and_atom(
                vault,
                filename="approved-fact.md",
                claim="Systems emit structured JSON logs.",
                evidence="Systems emit structured JSON logs.",
                approve=True,
            )
            atom_sha = hashlib.sha256((vault / "2-atoms" / "approved-fact.md").read_bytes()).hexdigest()
            ds_obj = {
                "schema_version": 2,
                "dataset_id": "temp-gate-test",
                "dataset_version": "1.0.0",
                "status": "pending_owner_review",
                "draft_provenance": {"drafted_by": "agent", "date": "2026-11-23", "scope": "test"},
                "purpose": "Gate test",
                "queries": [
                    {
                        "id": "q_temp_1",
                        "query": "JSON logs",
                        "category": "obs",
                        "split": "regression",
                        "project_scope": None,
                        "answerability": "answerable",
                        "answer_mode": "standard",
                        "expected_atom_ids": ["approved-fact.md"],
                        "acceptable_atom_ids": [],
                        "candidate_relevance": ["approved-fact.md"],
                        "expected_atom_revisions": [{"atom_id": "approved-fact.md", "revision_sha256": atom_sha}],
                        "expected_claims": [{"text": "Systems emit structured JSON logs.", "supported_by": ["approved-fact.md"]}],
                        "expected_behavior": "answer_with_evidence",
                        "trap_reason": None,
                    }
                ],
            }
            ds_copy = tpath / "dataset.json"
            raw_b = (json.dumps(ds_obj, indent=2) + "\n").encode("utf-8")
            ds_copy.write_bytes(raw_b)
            ds_sha = hashlib.sha256(raw_b).hexdigest()
            good_phrase = owner_gates._expected_confirmation_phrase("temp-gate-test", ds_sha)

            # Fail closed on non-interactive call, agent actor names, and mismatched confirmation phrase
            with self.assertRaises(PermissionError):
                owner_gates.approve_evaluation_dataset(
                    ds_copy, actor="camer", atoms_dir=vault / "2-atoms", brain_dir=vault,
                    confirm_phrase=good_phrase, interactive_tty_verified=False,
                )
            for bad_actor in ("agent", "antigravity", "codex", "assistant-bot"):
                with self.assertRaises(PermissionError):
                    owner_gates.approve_evaluation_dataset(
                        ds_copy, actor=bad_actor, atoms_dir=vault / "2-atoms", brain_dir=vault,
                        confirm_phrase=good_phrase, interactive_tty_verified=True,
                    )
            with self.assertRaises(PermissionError):
                owner_gates.approve_evaluation_dataset(
                    ds_copy, actor="camer", atoms_dir=vault / "2-atoms", brain_dir=vault,
                    confirm_phrase="WRONG PHRASE", interactive_tty_verified=True,
                )

            # Interactive owner confirmation succeeds and writes dataset-decisions.jsonl
            app_res = owner_gates.approve_evaluation_dataset(
                ds_copy, actor="camer", scope="Reviewed all cases",
                atoms_dir=vault / "2-atoms", brain_dir=vault,
                confirm_phrase=good_phrase, interactive_tty_verified=True,
            )
            self.assertEqual(app_res["status"], "owner_approved")
            validated = load_dataset(
                ds_copy,
                atoms_dir=vault / "2-atoms",
                approval_check=lambda p: approved_revision_is_current(str(p), atoms_dir=str(vault / "2-atoms"), brain_dir=str(vault)),
            )
            self.assertEqual(validated["owner_approval"]["actor"], "camer")
            self.assertNotIn("draft_provenance", validated)
            self.assertTrue((vault / "0-raw" / "archive" / "dataset-decisions.jsonl").is_file())

            # Fail-closed Track 3 SLO owner sign-off (requires interactive TTY, human actor, exact phrase)
            sota_copy = tpath / "sota.json"
            sota_bytes = json.dumps({
                "systems": {"second_brain_verified": {
                    "latency_p50_ms": 4.003,
                    "latency_p95_ms": 24.224,
                    "retrieval_latency_p95_ms": 18.711,
                    "validation_latency_p95_ms": 6.527,
                    "positive_recall_pct": 100.0,
                    "trap_leakage_pct": 0.0,
                }}
            }).encode("utf-8")
            sota_copy.write_bytes(sota_bytes)
            sota_sha = hashlib.sha256(sota_bytes).hexdigest()
            ans_copy = tpath / "answer_eval.json"
            ans_bytes = json.dumps({
                "model": "qwen2.5-coder:7b",
                "metrics": {"positive_end_to_end_latency_ms": {"p50": 23627.237, "p95": 36453.912, "max": 36885.958}},
                "quality_gate": {"passed": True},
            }).encode("utf-8")
            ans_copy.write_bytes(ans_bytes)
            ans_sha = hashlib.sha256(ans_bytes).hexdigest()
            slo_audit = owner_gates.audit_latency_slos(
                sota_path=sota_copy,
                answer_eval_path=ans_copy,
                brain_dir=vault,
            )
            self.assertEqual(slo_audit["configured_tier2_answer_p95_slo_ms"], 45000.0)
            self.assertFalse(slo_audit["all_slos_met"])
            self.assertEqual(slo_audit["pending_slo_decisions"], 2)
            good_slo_phrase = owner_gates._expected_slo_confirmation_phrase(35000.0, 45000.0, ans_sha, sota_sha)
            self.assertEqual(slo_audit["required_slo_confirmation_phrase"], good_slo_phrase)

            # Compound agent identifiers are blocked across both owner_gates and dataset validation
            for bad_actor in ("claude-code", "antigravity-v1", "gpt4o", "mcp-server"):
                with self.assertRaises(PermissionError):
                    owner_gates._validate_human_actor(bad_actor)
                bad_ds_payload = json.loads(ds_copy.read_text(encoding="utf-8"))
                bad_ds_payload["owner_approval"] = {"actor": bad_actor, "date": "2026-11-23", "scope": "test"}
                with self.assertRaises(EvaluationDatasetError):
                    validate_dataset(
                        bad_ds_payload,
                        atoms_dir=vault / "2-atoms",
                        approval_check=lambda p: approved_revision_is_current(str(p), atoms_dir=str(vault / "2-atoms"), brain_dir=str(vault)),
                    )
            with self.assertRaises(PermissionError):
                owner_gates.approve_latency_slo(
                    actor="camer", sota_path=sota_copy, answer_eval_path=ans_copy,
                    brain_dir=vault, confirm_phrase=good_slo_phrase, interactive_tty_verified=False,
                )
            with self.assertRaises(PermissionError):
                owner_gates.approve_latency_slo(
                    actor="codex", sota_path=sota_copy, answer_eval_path=ans_copy,
                    brain_dir=vault, confirm_phrase=good_slo_phrase, interactive_tty_verified=True,
                )
            with self.assertRaises(PermissionError):
                owner_gates.approve_latency_slo(
                    actor="camer", sota_path=sota_copy, answer_eval_path=ans_copy,
                    brain_dir=vault, confirm_phrase="WRONG", interactive_tty_verified=True,
                )
            slo_res = owner_gates.approve_latency_slo(
                actor="camer",
                rationale="Owner accepts 45s CPU answer SLO and 300-atom NTFS P95 variance.",
                sota_path=sota_copy,
                answer_eval_path=ans_copy,
                brain_dir=vault,
                confirm_phrase=good_slo_phrase,
                interactive_tty_verified=True,
            )
            self.assertEqual(slo_res["status"], "owner_signed_off")
            slo_after = owner_gates.audit_latency_slos(sota_path=sota_copy, answer_eval_path=ans_copy, brain_dir=vault)
            self.assertTrue(slo_after["all_slos_met"])
            self.assertEqual(slo_after["pending_slo_decisions"], 0)
            self.assertTrue((vault / "0-raw" / "archive" / "slo-decisions.jsonl").is_file())

    def test_documentation_and_cli_artifact_parity(self) -> None:
        # 1. Every `second-brain <cmd> [subcmd]` in README.md must succeed on `--help`
        readme_text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("second-brain mobile status", readme_text)
        self.assertNotIn("second-brain mobile export-app", readme_text)
        self.assertIn("second-brain mobile list", readme_text)
        self.assertIn("second-brain mobile export-bundle", readme_text)

        seen_invocations: set[tuple[str, ...]] = set()
        for line in readme_text.splitlines():
            for match in re.finditer(r"`?(?:uv run --[^\s`]+ )?second-brain ([^`\n#]+)`?", line):
                raw_cmd = match.group(1).strip()
                if not raw_cmd or raw_cmd.startswith("<"):
                    continue
                try:
                    parts = shlex.split(raw_cmd)
                except ValueError:
                    parts = raw_cmd.split()
                if not parts or not re.fullmatch(r"[a-z0-9-]+", parts[0]):
                    continue
                cmd = parts[0]
                if len(parts) >= 2 and re.fullmatch(r"[a-z0-9][a-z0-9-]*", parts[1]) and parts[1] not in {"work"}:
                    if not (len(parts) >= 3 and parts[1] in {"add", "create", "init", "install", "distill", "pair", "switch"}):
                        seen_invocations.add((cmd, parts[1]))
                    else:
                        seen_invocations.add((cmd,))
                else:
                    seen_invocations.add((cmd,))

        self.assertGreaterEqual(len(seen_invocations), 18)
        for tokens in sorted(seen_invocations):
            out = io.StringIO()
            with self.assertRaises(SystemExit) as cm, redirect_stdout(out):
                sb_cli.main([*tokens, "--help"])
            self.assertEqual(cm.exception.code, 0, f"`second-brain {' '.join(tokens)} --help` exited {cm.exception.code}")

        # 2. Benchmark report SHA-256 & retrieval metrics match JSON artifacts verbatim
        dataset_bytes = (ROOT / "docs" / "retrieval-evaluation-proposed.json").read_bytes().replace(b"\r\n", b"\n")
        ds_sha = hashlib.sha256(dataset_bytes).hexdigest()
        bench_md = (ROOT / "docs" / "benchmark-report-v1.0.0.md").read_text(encoding="utf-8")
        self.assertIn(ds_sha, bench_md)
        ret_res = json.loads((ROOT / "docs" / "retrieval-evaluation-results-2026-11-22.json").read_text(encoding="utf-8"))
        self.assertIn(f"{ret_res['candidate_lexical_mean_mrr']:.4f}", bench_md)
        self.assertIn(f"{ret_res['candidate_rrf_hybrid_mean_mrr']:.4f}", bench_md)

        # 3. MCP policy docs cover every tool in mcp_server.TOOL_NAMES (35)
        tracked_mcp = (ROOT / "docs" / "mcp-local-policy.md").read_text(encoding="utf-8")
        for tool_name in sorted(mcp_server.TOOL_NAMES):
            self.assertIn(tool_name, tracked_mcp, f"docs/mcp-local-policy.md missing MCP tool {tool_name}")
        vault_mcp = ROOT / "brain" / "mcp-config.md"
        if vault_mcp.is_file():
            vault_mcp_txt = vault_mcp.read_text(encoding="utf-8")
            for tool_name in sorted(mcp_server.TOOL_NAMES):
                self.assertIn(tool_name, vault_mcp_txt, f"brain/mcp-config.md missing MCP tool {tool_name}")

        # 4. Unified spec has zero unresolved Open Questions; runbooks & playbook use CAS + Three-Store
        unified = (ROOT / "docs" / "unified-local-ai-brain.md").read_text(encoding="utf-8")
        self.assertNotIn("## Open Questions", unified)
        self.assertIn("## Resolved Architecture Decisions", unified)

        refinery_rb = (ROOT / "docs" / "runbooks" / "refinery-run.md").read_text(encoding="utf-8")
        self.assertIn("review_atom.py transition approve", refinery_rb)
        scout_rb = (ROOT / "docs" / "runbooks" / "scout-run.md").read_text(encoding="utf-8")
        self.assertIn("ollama_scout.py", scout_rb)
        self.assertIn("0-raw/inbox", scout_rb)
        editor_rb = (ROOT / "docs" / "runbooks" / "editor-run.md").read_text(encoding="utf-8")
        self.assertIn("create-only", editor_rb.lower())
        playbook = (ROOT / "docs" / "recovery-and-operations-playbook.md").read_text(encoding="utf-8")
        self.assertNotIn(".sidecar/vault-ledger.jsonl", playbook)
        self.assertIn("0-raw/archive/", playbook)

        # 5. Owner gate review packet exists and lists dataset SHA-256 + 60 pending queries + 3 core atoms
        packet = (ROOT / "docs" / "owner-gate-review-packet.md").read_text(encoding="utf-8")
        self.assertIn(ds_sha, packet)
        for cand_atom in (
            "sodium-ion-battery-cost-record.md",
            "code-reviews-as-mentorship.md",
            "continuous-integration-deployment.md",
        ):
            self.assertIn(cand_atom, packet)
        self.assertNotIn("15.18 ms", bench_md)
        self.assertNotIn("15.18 ms", readme_text)
        self.assertIn("second-brain media image --prompt", readme_text)
        self.assertIn("second-brain media video --prompt", readme_text)

        gov_rb = (ROOT / "docs" / "runbooks" / "owner-governance-queue.md").read_text(encoding="utf-8")
        self.assertNotIn("review_status: approved", gov_rb)
        self.assertNotIn("second-brain inbox show", gov_rb)
        self.assertNotIn("second-brain review --status", gov_rb)
        self.assertIn("--expected-sha256", gov_rb)
        self.assertIn("memory_review.py --status pending", gov_rb)
        self.assertIn("review_status: accepted", gov_rb)

    def test_owner_scope_migration_preserves_multiline_yaml_approvals_and_ledger(self) -> None:
        from scripts import owner_scope_migration
        from scripts.atom_mutator import approved_revision_is_current

        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "brain"
            atoms_dir = vault / "2-atoms"
            raw_dir = vault / "0-raw"
            archive_dir = raw_dir / "archive"
            sidecar_dir = vault / ".sidecar"
            for folder in (atoms_dir, raw_dir, archive_dir, sidecar_dir):
                folder.mkdir(parents=True, exist_ok=True)
            (raw_dir / "src-scope.md").write_text(
                "Primary evidence: Canonical atoms keep byte-verified anchors.\n",
                encoding="utf-8",
            )
            draft_text = (
                "---\n"
                "id: scope-mig-atom\n"
                "type: atom\n"
                "memory_type: semantic\n"
                "epistemic_label: claim\n"
                "created: 2026-11-23\n"
                "last-reviewed: 2026-11-23\n"
                "sources:\n"
                "  - 0-raw/src-scope.md\n"
                "contexts: []\n"
                "review_status: pending\n"
                "claim_state: candidate\n"
                "citation_integrity: missing\n"
                "---\n\n"
                "## Claim\n"
                "Canonical atoms keep byte-verified anchors.\n\n"
                "## Evidence\n"
                "Canonical atoms keep byte-verified anchors.\n"
            )
            anchor = find_evidence_anchor(draft_text, str(vault))
            parts = draft_text.split("---", 2)
            fm = parts[1] + "".join(f"{k}: {v}\n" for k, v in anchor.items())
            fm = fm.replace("citation_integrity: missing", "citation_integrity: anchored")
            atom_path = atoms_dir / "scope-mig-atom.md"
            atom_path.write_text(f"---{fm}---{parts[2]}", encoding="utf-8")
            apply_review_transition(
                action="approve",
                atom_path=str(atom_path),
                expected_sha256=hashlib.sha256(atom_path.read_bytes()).hexdigest(),
                actor="camer",
                decision_log_path=str(archive_dir / "decision-record.jsonl"),
                brain_dir=str(vault),
                atoms_dir=str(atoms_dir),
            )

            # Agent actors and non-confirmed runs are blocked fail-closed
            with self.assertRaises(PermissionError):
                owner_scope_migration.apply_scope_migration(
                    scope="project:second_brain",
                    actor="claude-code",
                    brain_dir=vault,
                    atoms_dir=atoms_dir,
                    interactive_tty_verified=True,
                )
            with self.assertRaises(PermissionError):
                owner_scope_migration.apply_scope_migration(
                    scope="project:second_brain",
                    actor="camer",
                    brain_dir=vault,
                    atoms_dir=atoms_dir,
                    interactive_tty_verified=False,
                )

            res = owner_scope_migration.apply_scope_migration(
                scope="project:second_brain",
                actor="camer",
                brain_dir=vault,
                atoms_dir=atoms_dir,
                interactive_tty_verified=True,
            )
            self.assertEqual(res["modified_count"], 1)
            self.assertTrue(
                approved_revision_is_current(
                    str(atom_path),
                    decision_log_path=str(archive_dir / "decision-record.jsonl"),
                    brain_dir=str(vault),
                    atoms_dir=str(atoms_dir),
                )
            )
            self.assertTrue((archive_dir / "vault-ledger.jsonl").is_file())
            self.assertIn("project:second_brain", atom_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
