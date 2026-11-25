# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["httpx>=0.28,<1", "PyYAML", "cryptography>=42,<49"]
# ///
"""Phase 6 (Epic 2A): Multi-registry store publishers, trusted CA signing, outer DMG notarization,
detached Ed25519 manifest signatures, and mobile Fastlane/CI pipeline tests.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import httpx
import yaml

_DMG_NOTARIZE_BEHAVIOR_SCRIPT = r"""
const hookMod = require("./desktop/build/afterAllArtifactBuild.js");
const calls = [];
const fakeExec = (cmd, args) => { calls.push([cmd, args]); };
const env = {
  SIMULATED_PLATFORM: "darwin",
  CSC_NAME: "Developer ID Application: Example",
  APPLE_ID: "dev@example.com",
  APPLE_APP_SPECIFIC_PASSWORD: "app-specific",
  APPLE_TEAM_ID: "TEAMID1234",
};
(async () => {
  const skipped = await hookMod.default(
    { artifactPaths: ["dist/Second-Brain-1.0.0-universal.dmg"] },
    {},
    fakeExec
  );
  if (!skipped || calls.length !== 0) process.exit(2);
  calls.length = 0;
  const res = await hookMod.default(
    { artifactPaths: ["dist/Second-Brain-1.0.0-universal.dmg"] },
    env,
    fakeExec
  );
  if (!res || res.length !== 1 || calls.length !== 4) process.exit(3);
  const submit = calls[0];
  if (submit[0] !== "xcrun" || submit[1][0] !== "notarytool" || submit[1][1] !== "submit") process.exit(4);
  if (submit[1][2] !== "dist/Second-Brain-1.0.0-universal.dmg" || !submit[1].includes("--apple-id")) process.exit(5);
  if (calls[1][1][0] !== "stapler" || calls[1][1][1] !== "staple") process.exit(6);
  if (calls[3][0] !== "spctl") process.exit(7);
  console.log(calls.map((c) => [c[0], ...c[1]].join(" ")).join("\n"));
})().catch((err) => { console.error(err); process.exit(1); });
"""

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


class Phase6ReleaseAndStoresTests(unittest.TestCase):
    def test_chrome_web_store_v2_and_v1_fallback_and_dry_run(self) -> None:
        import publish_registries as pr

        with tempfile.TemporaryDirectory(prefix="sb_cws_test_") as td:
            zpath = Path(td) / "clipper.zip"
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("manifest.json", '{"manifest_version": 3, "name": "Second Brain", "version": "1.0.0"}')

            v2_calls: list[tuple[str, str]] = []

            def v2_handler(req: httpx.Request) -> httpx.Response:
                v2_calls.append((req.method, str(req.url)))
                url_s = str(req.url)
                if url_s == "https://oauth2.googleapis.com/token":
                    return httpx.Response(200, json={"access_token": "tok-123"})
                if ":upload" in url_s:
                    self.assertIn("/upload/v2/publishers/pub-99/items/ext-11:upload", url_s)
                    return httpx.Response(200, json={"uploadState": "SUCCEEDED", "itemId": "ext-11"})
                if ":publish" in url_s:
                    self.assertIn("/v2/publishers/pub-99/items/ext-11:publish", url_s)
                    return httpx.Response(200, json={"state": "PENDING_REVIEW"})
                return httpx.Response(404)

            env_v2 = {
                "CWS_CLIENT_ID": "cid",
                "CWS_CLIENT_SECRET": "csec",
                "CWS_REFRESH_TOKEN": "rtok",
                "CWS_EXTENSION_ID": "ext-11",
                "CWS_PUBLISHER_ID": "pub-99",
            }
            with patch.dict(os.environ, env_v2, clear=False):
                dry = pr.publish_chrome_web_store(zpath, dry_run=True)
                self.assertTrue(dry["dry_run"])
                self.assertEqual(dry["api_version"], "v2")

                res = pr.publish_chrome_web_store(zpath, transport=httpx.MockTransport(v2_handler))
                self.assertEqual(res["api_version"], "v2")
                self.assertEqual(res["publisher_id"], "pub-99")
                self.assertEqual(res["status"], "PENDING_REVIEW")
                self.assertTrue(any(":upload" in u for _, u in v2_calls))
                self.assertTrue(any(":publish" in u for _, u in v2_calls))

            # Fallback to v1.1 when CWS_PUBLISHER_ID is unset
            v1_calls: list[str] = []

            def v1_handler(req: httpx.Request) -> httpx.Response:
                v1_calls.append(str(req.url))
                if str(req.url) == "https://oauth2.googleapis.com/token":
                    return httpx.Response(200, json={"access_token": "tok-v1"})
                if "/upload/chromewebstore/v1.1/" in str(req.url):
                    return httpx.Response(200, json={"uploadState": "SUCCESS"})
                if "/chromewebstore/v1.1/items/ext-11/publish" in str(req.url):
                    return httpx.Response(200, json={"status": ["OK"]})
                return httpx.Response(404)

            env_v1 = {k: v for k, v in env_v2.items() if k != "CWS_PUBLISHER_ID"}
            with patch.dict(os.environ, {**env_v1, "CWS_PUBLISHER_ID": ""}, clear=False):
                res_v1 = pr.publish_chrome_web_store(zpath, transport=httpx.MockTransport(v1_handler))
                self.assertEqual(res_v1["api_version"], "v1.1")
                self.assertTrue(any("/v1.1/items/ext-11/publish" in u for u in v1_calls))

    def test_firefox_amo_polling_and_version_create_and_edge_polling(self) -> None:
        import publish_registries as pr

        with tempfile.TemporaryDirectory(prefix="sb_amo_edge_") as td:
            xpi = Path(td) / "clipper.xpi"
            zpath = Path(td) / "edge.zip"
            for p in (xpi, zpath):
                with zipfile.ZipFile(p, "w") as zf:
                    zf.writestr("manifest.json", '{"manifest_version": 3, "version": "1.0.0"}')

            amo_polls = {"n": 0}
            amo_created = {"called": False}

            def amo_handler(req: httpx.Request) -> httpx.Response:
                p = req.url.path
                if req.method == "POST" and p == "/api/v5/addons/upload/":
                    return httpx.Response(201, json={"uuid": "uuid-abc-123", "processed": False, "valid": False})
                if req.method == "GET" and p == "/api/v5/addons/upload/uuid-abc-123/":
                    amo_polls["n"] += 1
                    if amo_polls["n"] < 2:
                        return httpx.Response(200, json={"uuid": "uuid-abc-123", "processed": False, "valid": False})
                    return httpx.Response(200, json={"uuid": "uuid-abc-123", "processed": True, "valid": True})
                if req.method == "POST" and p == "/api/v5/addons/addon/second-brain-clipper/versions/":
                    amo_created["called"] = True
                    body = json.loads(req.content.decode("utf-8"))
                    self.assertEqual(body["upload"], "uuid-abc-123")
                    return httpx.Response(201, json={"id": 555, "version": "1.0.0"})
                return httpx.Response(404, json={"path": p})

            with patch.dict(
                os.environ,
                {
                    "AMO_JWT_ISSUER": "user:12345:999",
                    "AMO_JWT_SECRET": "secret-amo-test-key-0123456789abcdef",
                    "AMO_ADDON_SLUG": "second-brain-clipper",
                },
                clear=False,
            ):
                res = pr.publish_firefox_amo(
                    xpi,
                    sleep_fn=lambda _s: None,
                    transport=httpx.MockTransport(amo_handler),
                )
                self.assertEqual(res["upload_uuid"], "uuid-abc-123")
                self.assertTrue(res["validated"])
                self.assertEqual(res["version_id"], 555)
                self.assertTrue(amo_created["called"])
                self.assertEqual(amo_polls["n"], 2)

            edge_polls = {"n": 0}

            def edge_handler(req: httpx.Request) -> httpx.Response:
                p = req.url.path
                if req.method == "POST" and p.endswith("/draft/package"):
                    return httpx.Response(202, headers={"Location": "https://api.addons.microsoftedge.microsoft.com/v1/products/pid-1/submissions/draft/package/operations/op-1"})
                if req.method == "POST" and p.endswith("/submissions"):
                    return httpx.Response(202, headers={"Location": "https://api.addons.microsoftedge.microsoft.com/v1/products/pid-1/submissions/operations/op-pub"})
                if req.method == "GET" and "/operations/" in p:
                    edge_polls["n"] += 1
                    return httpx.Response(200, json={"status": "Succeeded"})
                return httpx.Response(404)

            with patch.dict(
                os.environ,
                {
                    "EDGE_PRODUCT_ID": "pid-1",
                    "EDGE_CLIENT_ID": "cid-1",
                    "EDGE_API_KEY": "key-1",
                },
                clear=False,
            ):
                eres = pr.publish_edge_addons(
                    zpath,
                    sleep_fn=lambda _s: None,
                    transport=httpx.MockTransport(edge_handler),
                )
                self.assertEqual(eres["publish_status"], "Succeeded")
                self.assertEqual(edge_polls["n"], 2)

    def test_pypi_vsix_ovsx_publishers_and_setup_wizard_never_leak_secrets(self) -> None:
        import publish_registries as pr

        with tempfile.TemporaryDirectory(prefix="sb_pypi_vsix_") as td:
            dist = Path(td)
            whl = dist / "second_brain-1.0.0-py3-none-any.whl"
            with zipfile.ZipFile(whl, "w") as zf:
                zf.writestr("second_brain/__init__.py", "__version__ = '1.0.0'\n")
                zf.writestr("second_brain-1.0.0.dist-info/METADATA", "Name: second-brain\nVersion: 1.0.0\n")

            vsix = dist / "second-brain-1.0.0.vsix"
            with zipfile.ZipFile(vsix, "w") as zf:
                zf.writestr("extension/package.json", '{"name": "second-brain", "version": "1.0.0"}')
                zf.writestr("extension/wheelhouse/second_brain-1.0.0-py3-none-any.whl", whl.read_bytes())

            recorded_cmds: list[tuple[list[str], dict[str, str]]] = []

            def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                recorded_cmds.append((list(cmd), dict(kwargs.get("env") or {})))
                return subprocess.CompletedProcess(cmd, 0, stdout="published ok", stderr="")

            with patch.dict(
                os.environ,
                {
                    "PYPI_API_TOKEN": "pypi-secret-token-value-should-not-appear-in-argv",
                    "VSCE_PAT": "vsce-secret-pat-value-should-not-appear-in-argv",
                    "OVSX_PAT": "ovsx-secret-pat-value-should-not-appear-in-argv",
                },
                clear=False,
            ):
                dry_pypi = pr.publish_pypi(dist, dry_run=True, runner=fake_run)
                self.assertTrue(dry_pypi["dry_run"])
                live_pypi = pr.publish_pypi(dist, dry_run=False, runner=fake_run)
                self.assertEqual(live_pypi["registry"], "pypi")

                dry_vsce = pr.publish_vscode_marketplace(vsix, dry_run=True, runner=fake_run)
                self.assertTrue(dry_vsce["dry_run"])
                live_vsce = pr.publish_vscode_marketplace(vsix, dry_run=False, runner=fake_run)
                self.assertEqual(live_vsce["registry"], "vscode_marketplace")

                dry_ovsx = pr.publish_open_vsx(vsix, dry_run=True, runner=fake_run)
                self.assertTrue(dry_ovsx["dry_run"])
                live_ovsx = pr.publish_open_vsx(vsix, dry_run=False, runner=fake_run)
                self.assertEqual(live_ovsx["registry"], "open_vsx")

            self.assertEqual(len(recorded_cmds), 3)
            for argv_list, _env_map in recorded_cmds:
                joined = " ".join(argv_list)
                self.assertNotIn("secret-token-value", joined)
                self.assertNotIn("secret-pat-value", joined)

        wiz = pr.format_setup_wizard()
        self.assertIn("registries", wiz)
        self.assertGreaterEqual(len(wiz["registries"]), 9)
        self.assertTrue(any("PYPI_API_TOKEN" in r["secrets"] for r in wiz["registries"]))

    def test_ed25519_manifest_signature_roundtrip_and_tamper_rejection(self) -> None:
        import verify_release_signatures as vrs

        priv = Ed25519PrivateKey.generate()
        priv_pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub_pem = priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        with tempfile.TemporaryDirectory(prefix="sb_sig_test_") as td:
            dist = Path(td)
            sums = dist / "SHA256SUMS.txt"
            sums.write_text(("a" * 64) + "  second_brain-1.0.0-py3-none-any.whl\n", encoding="utf-8")

            sig_path = vrs.sign_manifest_ed25519(sums, priv_pem)
            self.assertTrue(sig_path.is_file())
            self.assertEqual(sig_path.name, "SHA256SUMS.txt.sig")

            ok_res = vrs.verify_manifest_ed25519(sums, pub_pem, sig_path=sig_path)
            self.assertTrue(ok_res["ok"])

            # Tamper SHA256SUMS.txt -> must fail closed
            sums.write_text(("b" * 64) + "  second_brain-1.0.0-py3-none-any.whl\n", encoding="utf-8")
            bad_res = vrs.verify_manifest_ed25519(sums, pub_pem, sig_path=sig_path)
            self.assertFalse(bad_res["ok"])
            self.assertIn("invalid", bad_res["reason"])

        readiness = vrs.inspect_signing_readiness_env()
        self.assertIn("manifest_ed25519_configured", readiness)

    def test_sign_release_ps1_and_outer_dmg_after_all_artifact_build_hook(self) -> None:
        ps_src = (REPO_ROOT / "scripts" / "sign_release.ps1").read_text(encoding="utf-8")
        self.assertIn("https://timestamp.acs.microsoft.com", ps_src)
        self.assertIn("https://timestamp.digicert.com", ps_src)
        self.assertNotIn('"http://timestamp.acs.microsoft.com"', ps_src)
        self.assertIn("Azure.CodeSigning.Dlib.dll", ps_src)
        self.assertIn("/dlib", ps_src)
        self.assertIn("/dmdf", ps_src)

        desktop_pkg = json.loads((REPO_ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(desktop_pkg["build"].get("afterAllArtifactBuild"), "./build/afterAllArtifactBuild.js")

        hook_path = REPO_ROOT / "desktop" / "build" / "afterAllArtifactBuild.js"
        self.assertTrue(hook_path.is_file())

        node_bin = shutil.which("node")
        if node_bin:
            proc = subprocess.run(
                [node_bin, "-e", _DMG_NOTARIZE_BEHAVIOR_SCRIPT],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
            self.assertIn("notarytool", proc.stdout)
            self.assertIn("stapler", proc.stdout)
            self.assertIn("spctl", proc.stdout)

    def test_homebrew_cask_bundle_id_and_macos_only_manifest_decoupling(self) -> None:
        from build_distribution_manifests import build_package_manager_manifests

        with tempfile.TemporaryDirectory(prefix="sb_mac_mf_") as td:
            dist = Path(td)
            dmg = dist / "Second-Brain-1.0.0-universal.dmg"
            dmg.write_bytes(b"dmg-payload")
            dmg_sha = hashlib.sha256(b"dmg-payload").hexdigest()
            (dist / "SHA256SUMS.txt").write_text(f"{dmg_sha}  {dmg.name}\n", encoding="utf-8")

            mf = build_package_manager_manifests(
                out_dir=dist,
                dist_dir=dist,
                version="1.0.0",
                repo_slug="prodofthecentury/second-brain",
                require_checksums=True,
                allow_missing_exe=True,
            )
            cask_txt = Path(mf["homebrew_cask"]).read_text(encoding="utf-8")
            self.assertIn("com.secondbrain.desktop", cask_txt)
            self.assertNotIn("ai.secondbrain.desktop", cask_txt)

    def test_fastlane_and_mobile_and_desktop_workflows(self) -> None:
        for rel in (
            ".github/workflows/release.yml",
            ".github/workflows/mobile-release.yml",
        ):
            raw = (REPO_ROOT / rel).read_bytes()
            self.assertNotIn(b"\r\n", raw, f"Workflow {rel} must use LF line endings")
            doc = yaml.safe_load(raw.decode("utf-8"))
            self.assertIsInstance(doc, dict)

        rel_txt = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("verify_release_signatures.py", rel_txt)
        self.assertIn("REQUIRE_TRUSTED_SIGNING", rel_txt)
        self.assertIn("attest-build-provenance", rel_txt)
        self.assertIn("SHA256SUMS.txt.sig", rel_txt)

        mob_txt = (REPO_ROOT / ".github" / "workflows" / "mobile-release.yml").read_text(encoding="utf-8")
        self.assertIn("assembleRelease", mob_txt)
        self.assertIn("bundleRelease", mob_txt)
        self.assertIn("fastlane", mob_txt)

        fast_dir = REPO_ROOT / "mobile" / "vault-client" / "fastlane"
        for fn in ("Appfile", "Fastfile", "Matchfile"):
            self.assertTrue((fast_dir / fn).is_file(), f"Missing fastlane/{fn}")
        fastfile_txt = (fast_dir / "Fastfile").read_text(encoding="utf-8")
        for lane in ("build_aab", "deploy_internal", "build_ipa", "deploy_testflight"):
            self.assertIn(lane, fastfile_txt)


if __name__ == "__main__":
    unittest.main()
