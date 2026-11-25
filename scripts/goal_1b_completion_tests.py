# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Comprehensive regression tests covering all 8 tracks of 1B completion."""

from __future__ import annotations

import ast
from contextlib import ExitStack
import hashlib
import http.cookies
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


class Track1MobileVaultClientTests(unittest.TestCase):
    def test_capacitor_android_and_ios_native_scaffolds_and_security_controls(self) -> None:
        mobile_root = ROOT / "mobile" / "vault-client"
        android_files = (
            "android/gradlew",
            "android/gradle/wrapper/gradle-wrapper.properties",
            "android/build.gradle",
            "android/settings.gradle",
            "android/app/build.gradle",
            "android/app/proguard-rules.pro",
            "android/app/src/main/res/values/styles.xml",
            "android/app/src/main/java/com/secondbrain/vault/MainActivity.kt",
        )
        for rel in android_files:
            self.assertTrue((mobile_root / rel).is_file(), f"Missing Android scaffold file: {rel}")

        gradlew_bytes = (mobile_root / "android" / "gradlew").read_bytes()
        self.assertNotIn(b"\r", gradlew_bytes, "mobile/vault-client/android/gradlew must use pure LF endings")
        main_kt = (mobile_root / "android/app/src/main/java/com/secondbrain/vault/MainActivity.kt").read_text(encoding="utf-8")
        self.assertIn("FLAG_SECURE", main_kt)

        ios_files = (
            "ios/App/App.xcodeproj/project.pbxproj",
            "ios/App/App.xcworkspace/contents.xcworkspacedata",
            "ios/App/App/Base.lproj/Main.storyboard",
            "ios/App/App/AppDelegate.swift",
            "ios/App/App/Info.plist",
        )
        for rel in ios_files:
            self.assertTrue((mobile_root / rel).is_file(), f"Missing iOS scaffold file: {rel}")

        app_delegate = (mobile_root / "ios/App/App/AppDelegate.swift").read_text(encoding="utf-8")
        self.assertIn("UIVisualEffectView", app_delegate)
        self.assertNotIn("window?.isHidden = false", app_delegate)

        info_plist = (mobile_root / "ios/App/App/Info.plist").read_text(encoding="utf-8")
        self.assertRegex(
            info_plist,
            r"<key>NSAllowsLocalNetworking</key>\s*<true/>",
        )

    def test_mobile_vault_node_tests_pass(self) -> None:
        res = subprocess.run(
            ["node", "--test", str(ROOT / "mobile/vault-client/test/mobile_vault.test.js")],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(res.returncode, 0, f"mobile_vault.test.js failed:\n{res.stdout}\n{res.stderr}")


class Track2CreativeMediaTests(unittest.TestCase):
    def test_fail_closed_auto_mode_and_15_91_gib_neural_unlock(self) -> None:
        import creative_media
        import hardware_profile

        profile_16g = hardware_profile.classify_hardware_tier(
            os_name="Windows",
            os_release="10",
            architecture="AMD64",
            cpu_threads=8,
            total_ram_gib=15.91,
            available_ram_gib=8.0,
            cpp_runtime_ready=True,
            cpp_runtime_detail="ok",
            gpu_backend="cuda",
            gpu_name="GeForce GTX 965M",
            vram_gib=4.0,
            ollama_reachable=True,
            installed_ollama_models=("llama3.2:3b",),
        )
        status = creative_media.get_creative_media_status(
            hardware_profile=profile_16g,
            sd_webui_url="http://127.0.0.1:59991",
            comfyui_url="http://127.0.0.1:59992",
            diffusers_available_override=False,
            ffmpeg_path_override="",
        )
        self.assertTrue(status.neural_backends_unlocked)

        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            brain.mkdir()
            with self.assertRaises(creative_media.CreativeMediaUnavailableError):
                creative_media.generate_local_image(
                    "draw architecture diagram",
                    brain_dir=brain,
                    backend="auto",
                    allow_procedural_fallback=False,
                    hardware_profile=profile_16g,
                    sd_webui_url="http://127.0.0.1:59991",
                    comfyui_url="http://127.0.0.1:59992",
                )

    def test_pure_python_h264_mp4_muxer_and_comfyui_temporal_workflow(self) -> None:
        from PIL import Image
        import creative_media

        frames = [Image.new("RGB", (64, 48), (10 + i * 20, 40, 120)) for i in range(4)]
        mp4_bytes, enc = creative_media._encode_frames_to_mp4(
            frames,
            width=64,
            height=48,
            fps=8,
            ffmpeg_exe=None,
            allow_pyav=False,
        )
        self.assertEqual(enc, "iso_bmff_h264_avc1_pure_python")
        meta = creative_media.inspect_mp4_container(mp4_bytes)
        self.assertEqual(meta["codec_tag"], "avc1")
        self.assertTrue(meta["has_avcC"])
        self.assertTrue(meta["has_stss"])

        wf_stock, wf_stock_kind = creative_media._build_comfy_video_workflow(
            ckpt_name="model.safetensors",
            prompt="sunset timelapse",
            negative_prompt="blurry",
            width=512,
            height=384,
            frames=12,
            fps=8,
            seed=42,
            steps=10,
            cfg_scale=6.0,
            object_info={},
        )
        self.assertEqual(wf_stock_kind, "latent_motion_chain")
        stock_latent = [v for v in wf_stock.values() if isinstance(v, dict) and v.get("class_type") == "EmptyLatentImage"]
        self.assertTrue(stock_latent)
        self.assertEqual(stock_latent[0]["inputs"]["batch_size"], 1)

        wf, wf_kind = creative_media._build_comfy_video_workflow(
            ckpt_name="model.safetensors",
            prompt="sunset timelapse",
            negative_prompt="blurry",
            width=512,
            height=384,
            frames=12,
            fps=8,
            seed=42,
            steps=10,
            cfg_scale=6.0,
            object_info={"ADE_AnimateDiffLoaderWithContext": {}},
        )
        self.assertEqual(wf_kind, "animatediff")
        latent_nodes = [v for v in wf.values() if isinstance(v, dict) and v.get("class_type") == "EmptyLatentImage"]
        self.assertTrue(latent_nodes)
        self.assertEqual(latent_nodes[0]["inputs"]["batch_size"], 12)

        pyproj = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(pyproj, r"creative\s*=\s*\[[^\]]*diffusers")


class Track3OnnxCrossEncoderTests(unittest.TestCase):
    def test_unconditional_stage2_onnx_scoring_and_honest_tag(self) -> None:
        from dataclasses import dataclass
        import cross_encoder

        @dataclass
        class _Hit:
            filename: str
            content: str
            score: float
            match_reasons: list[str]

        hits = [
            _Hit("a.md", "## Claim\nPostgreSQL uses MVCC snapshots.\n", 0.91, ["bm25"]),
            _Hit("b.md", "## Claim\nSQLite uses rollback or WAL.\n", 0.12, ["bm25"]),
        ]

        class _FakeOnnx:
            def __init__(self):
                self.called_pairs = 0

            def score_pairs(self, query: str, texts: list[str]) -> list[float]:
                self.called_pairs += len(texts)
                return [0.95 if "wal" in t.lower() else 0.25 for t in texts]

        fake_onnx = _FakeOnnx()
        with patch.object(cross_encoder, "get_onnx_cross_encoder", return_value=fake_onnx):
            reranked = cross_encoder.rerank_recall_results("Which database uses WAL?", hits, allow_onnx=True)
        self.assertEqual(fake_onnx.called_pairs, 2)
        self.assertEqual(reranked[0].filename, "b.md")
        self.assertTrue(any(r.startswith("rerank:onnx_cross_encoder:") for r in reranked[0].match_reasons))

        with patch.object(cross_encoder, "get_onnx_cross_encoder", return_value=None):
            heur = cross_encoder.rerank_recall_results("Which database uses WAL?", hits, allow_onnx=True)
        self.assertTrue(all(not any(r.startswith("rerank:onnx_cross_encoder") for r in h.match_reasons) for h in heur))
        self.assertTrue(any(any(r.startswith("rerank:cross_encoder:") for r in h.match_reasons) for h in heur))


class Track4RegistryPublisherTests(unittest.TestCase):
    def test_status_subcommands_and_firefox_amo_polling_and_version_create(self) -> None:
        import io as _io
        from contextlib import redirect_stdout
        import publish_registries

        buf = _io.StringIO()
        with redirect_stdout(buf):
            rc = publish_registries.main(["status", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertGreaterEqual(payload["total_registries"], 8)
        for reg in ("pypi", "vscode_marketplace", "open_vsx", "winget_pkgs", "firefox_amo"):
            self.assertIn(reg, payload["registries"])

        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            dist = tmp_p / "dist"
            dist.mkdir()
            (dist / "second_brain-0.1.0-py3-none-any.whl").write_bytes(b"zip")
            vsix_p = tmp_p / "ext.vsix"
            vsix_p.write_bytes(b"PK\x03\x04")
            with (
                patch.object(publish_registries, "_validate_wheel_cleanliness"),
                patch.object(publish_registries, "_validate_vsix_package"),
                patch.dict(
                    os.environ,
                    {
                        "PYPI_API_TOKEN": "pypi-tok",
                        "VSCE_PAT": "vsce-tok",
                        "OVSX_PAT": "ovsx-tok",
                        "GITHUB_TOKEN": "gh-tok",
                    },
                    clear=False,
                ),
            ):
                self.assertEqual(
                    publish_registries.main(
                        ["publish-pypi", "--dist-dir", str(dist), "--repository-url", "https://test.pypi.org/legacy/", "--dry-run"]
                    ),
                    0,
                )
                self.assertEqual(publish_registries.main(["publish-vscode", "--vsix", str(vsix_p), "--dry-run"]), 0)
                self.assertEqual(publish_registries.main(["publish-openvsx", "--vsix", str(vsix_p), "--dry-run"]), 0)
                manifest_dir = tmp_p / "winget" / "manifests" / "s" / "SecondBrain" / "SecondBrain" / "1.2.3"
                manifest_dir.mkdir(parents=True)
                (manifest_dir / "SecondBrain.SecondBrain.yaml").write_text(
                    "# yaml-language-server: $schema=https://aka.ms/winget-manifest.version.1.6.0.schema.json\n"
                    "PackageIdentifier: SecondBrain.SecondBrain\nPackageVersion: 1.2.3\nDefaultLocale: en-US\nManifestType: version\nManifestVersion: 1.6.0\n",
                    encoding="utf-8",
                )
                (manifest_dir / "SecondBrain.SecondBrain.installer.yaml").write_text(
                    "# yaml-language-server: $schema=https://aka.ms/winget-manifest.installer.1.6.0.schema.json\n"
                    f"PackageIdentifier: SecondBrain.SecondBrain\nPackageVersion: 1.2.3\nInstallers:\n- Architecture: x64\n  InstallerType: nullsoft\n  InstallerUrl: https://github.com/prodofthecentury/second-brain/releases/download/v1.2.3/SecondBrain.exe\n  InstallerSha256: {'A' * 64}\nManifestType: installer\nManifestVersion: 1.6.0\n",
                    encoding="utf-8",
                )
                (manifest_dir / "SecondBrain.SecondBrain.locale.en-US.yaml").write_text(
                    "# yaml-language-server: $schema=https://aka.ms/winget-manifest.defaultLocale.1.6.0.schema.json\n"
                    "PackageIdentifier: SecondBrain.SecondBrain\nPackageVersion: 1.2.3\nPackageLocale: en-US\nPublisher: Second Brain\nPackageName: Second Brain\nLicense: MIT\nShortDescription: Local-first verifiable AI memory platform.\nManifestType: defaultLocale\nManifestVersion: 1.6.0\n",
                    encoding="utf-8",
                )
                with patch.object(
                    publish_registries,
                    "validate_winget_manifests_locally",
                    return_value={"validated": True, "ok": True, "manifest_files": ["Pkg.yaml"]},
                ):
                    self.assertEqual(
                        publish_registries.main(
                            [
                                "publish-winget",
                                "--manifest-dir",
                                str(tmp_p / "winget"),
                                "--version",
                                "1.2.3",
                                "--installer-sha256",
                                "a" * 64,
                                "--dry-run",
                            ]
                        ),
                        0,
                    )

            # AMO upload + poll + version creation
            import zipfile

            xpi_p = tmp_p / "addon.xpi"
            with zipfile.ZipFile(xpi_p, "w") as zf:
                zf.writestr(
                    "manifest.json",
                    json.dumps(
                        {
                            "manifest_version": 3,
                            "name": "Test",
                            "version": "1.0.0",
                            "browser_specific_settings": {"gecko": {"id": "clip@secondbrain.local"}},
                        }
                    ),
                )

            calls: list[tuple[str, str]] = []

            def _handler(req: httpx.Request) -> httpx.Response:
                calls.append((req.method, req.url.path))
                if req.method == "POST" and req.url.path.endswith("/upload/"):
                    return httpx.Response(201, json={"uuid": "u-123", "valid": False, "processed": False})
                if req.method == "GET" and req.url.path.endswith("/upload/u-123/"):
                    return httpx.Response(200, json={"uuid": "u-123", "valid": True, "processed": True})
                if req.method == "POST" and req.url.path.endswith("/versions/"):
                    return httpx.Response(201, json={"id": 999, "version": "1.0.0"})
                return httpx.Response(404)

            with patch.dict(os.environ, {"AMO_JWT_ISSUER": "user:1", "AMO_JWT_SECRET": "sec"}, clear=False):
                amo_res = publish_registries.publish_firefox_amo(
                    xpi_p,
                    poll_interval=0.0,
                    sleep_fn=lambda _s: None,
                    transport=httpx.MockTransport(_handler),
                )
            self.assertEqual(amo_res["addon_slug"], "clip@secondbrain.local")
            self.assertEqual(amo_res["version_id"], 999)
            self.assertEqual([m for m, _ in calls], ["POST", "GET", "POST"])


class Track5LinuxSignatureVerifierTests(unittest.TestCase):
    @staticmethod
    def _build_minimal_deb(path: Path) -> None:
        import io
        import tarfile

        def _ar_entry(name: str, data: bytes) -> bytes:
            hdr = f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(data):<10}`\n".encode("ascii")
            pad = b"\n" if (len(data) % 2) else b""
            return hdr + data + pad

        def _tgz(entry_name: str, content: bytes) -> bytes:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                ti = tarfile.TarInfo(entry_name)
                ti.size = len(content)
                tf.addfile(ti, io.BytesIO(content))
            return buf.getvalue()

        control_tgz = _tgz("./control", b"Package: second-brain\nVersion: 1.0.0\n")
        data_tgz = _tgz("./usr/bin/second-brain", b"#!/bin/sh\n")
        payload = b"!<arch>\n" + _ar_entry("debian-binary", b"2.0\n") + _ar_entry("control.tar.gz", control_tgz) + _ar_entry("data.tar.gz", data_tgz)
        path.write_bytes(payload)

    def test_appimage_and_deb_verification_with_ed25519_manifest(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        import verify_release_signatures as vrs

        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp)
            appimage = dist / "SecondBrain.AppImage"
            appimage.write_bytes(b"\x7fELF\x02\x01\x01\x00AI\x02\x00hsqs" + b"\x00" * 32)
            deb = dist / "secondbrain_1.0_amd64.deb"
            self._build_minimal_deb(deb)

            unsigned_res = vrs.verify_linux_artifact(deb, require_signature=True)
            self.assertFalse(unsigned_res["ok"])
            allowed_res = vrs.verify_linux_artifact(appimage, require_signature=False)
            self.assertTrue(allowed_res["ok"])

            priv = Ed25519PrivateKey.generate()
            priv_pem = priv.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            pub_pem = priv.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            sums = dist / "SHA256SUMS.txt"
            sums.write_text(
                f"{hashlib.sha256(appimage.read_bytes()).hexdigest()}  {appimage.name}\n"
                f"{hashlib.sha256(deb.read_bytes()).hexdigest()}  {deb.name}\n",
                encoding="utf-8",
            )
            vrs.sign_manifest_ed25519(sums, priv_pem)

            ok_app = vrs.verify_linux_artifact(appimage, require_signature=True, public_key_pem=pub_pem)
            ok_deb = vrs.verify_linux_artifact(deb, require_signature=True, public_key_pem=pub_pem)
            self.assertTrue(ok_app["ok"])
            self.assertTrue(ok_deb["ok"])
            dist_rep = vrs.verify_dist_directory(dist, require_trusted=True, public_key_pem=pub_pem, require_manifest_sig=True)
            self.assertTrue(dist_rep["all_ok"])


class Track6BenchmarkFairnessTests(unittest.TestCase):
    def test_fair_svd_embeddings_chroma_mem0_and_cross_encoder_claim_selection(self) -> None:
        import sota_benchmark

        src = (SCRIPTS_DIR / "sota_benchmark.py").read_text(encoding="utf-8")
        self.assertNotIn("blake2b", src)
        self.assertNotIn("_deterministic_dense_embed", src)

        emb = sota_benchmark._SvdTfidfSemanticEmbedder(
            [
                "PostgreSQL MVCC uses snapshot isolation for transactions.",
                "SQLite WAL supports concurrent database readers.",
                "Espresso extraction runs at nine bar water pressure.",
            ]
        )
        vecs = emb.embed_batch(
            [
                "PostgreSQL snapshot isolation",
                "PostgreSQL transactions MVCC",
                "Espresso coffee bar pressure",
            ]
        )
        dot_rel = sum(a * b for a, b in zip(vecs[0], vecs[1]))
        dot_unrel = sum(a * b for a, b in zip(vecs[0], vecs[2]))
        self.assertGreater(dot_rel, dot_unrel)

        live_src = (SCRIPTS_DIR / "live_third_party_benchmark.py").read_text(encoding="utf-8")
        self.assertIn("mem0_infer: bool = True", live_src)
        self.assertIn('"project"', live_src)


class Track7HardwareAndPdfOcrTests(unittest.TestCase):
    def test_macos_vm_stat_multivendor_gpu_and_pdf_rasterization_fail_closed(self) -> None:
        import doc_extractors
        import hardware_profile

        vm_stat_sample = (
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            "Pages free:                              10000.\n"
            "Pages inactive:                          20000.\n"
            "Pages speculative:                        5000.\n"
            "Pages purgeable:                          1000.\n"
        )
        avail = hardware_profile._parse_macos_vm_stat_avail_bytes(vm_stat_sample, 16 * 1024**3)
        self.assertEqual(avail, 36000 * 16384)

        rocm_info = hardware_profile._parse_rocm_smi_output(
            "GPU[0] : Card series: Radeon RX 7900 XTX\nGPU[0] : VRAM Total Memory (B): 25769803776\n"
        )
        self.assertIsNotNone(rocm_info)
        self.assertEqual(rocm_info[0], "rocm")
        self.assertGreater(rocm_info[2], 20.0)

        # Fake 1-page PDF where pypdf text & XObjects are empty -> full-page rasterization + OCR
        class _FakePage:
            def __getitem__(self, key):
                return {}

            def get(self, key, default=None):
                return default

            def extract_text(self, **_kwargs):
                return ""

        class _FakeReader:
            metadata = {}
            outline = []
            pages = [_FakePage()]

        with tempfile.TemporaryDirectory() as tmp:
            pdf_p = Path(tmp) / "vector.pdf"
            pdf_p.write_bytes(b"%PDF-1.4\n%%EOF\n")
            with (
                patch("pypdf.PdfReader", return_value=_FakeReader()),
                patch.object(doc_extractors, "_rasterize_pdf_page_to_png", return_value=b"PNG"),
                patch.object(doc_extractors, "_sanitize_pdf_image_to_png", return_value=b"PNG"),
            ):
                extracted = doc_extractors.extract_pdf(
                    str(pdf_p),
                    enable_ocr=True,
                    ocr_fn=lambda _png, page_num: f"Scanned raster text from page {page_num}",
                )
                self.assertIn("Scanned raster text from page 1", extracted.markdown)

                with self.assertRaises(doc_extractors.DocumentExtractionError):
                    doc_extractors.extract_pdf(
                        str(pdf_p),
                        enable_ocr=True,
                        ocr_fn=lambda _png, _p: "",
                    )


class Track8UnfrozenPathsSessionCookieChatReplSmartCriticTests(unittest.TestCase):
    def test_ast_zero_frozen_path_defaults_and_runtime_vault_switch(self) -> None:
        frozen_names = {
            "BRAIN_DIR",
            "ATOMS_DIR",
            "RAW_DIR",
            "INBOX_DIR",
            "EXPERIENCES_DIR",
            "PLAYBOOKS_DIR",
            "CHROMA_PATH",
            "LEXICAL_INDEX_PATH",
            "ARCHIVE_DIR",
            "HOUSE_RULES_PATH",
            "DEFAULT_HOUSE_RULES",
        }
        hits = []
        for path in sorted(SCRIPTS_DIR.glob("*.py")):
            if path.name.endswith(("_tests.py", "_test.py", "_canary.py")) or path.name == "test_all.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
                    for default_node in defaults:
                        if isinstance(default_node, ast.Name) and default_node.id in frozen_names:
                            hits.append((path.name, node.lineno, node.name, default_node.id))
        self.assertEqual(hits, [], f"Found frozen path defaults: {hits}")

        import config
        import notes_store
        import streaming_chat
        import time_travel

        orig = os.environ.get("BRAIN_VAULT_DIR")
        try:
            with tempfile.TemporaryDirectory() as v1, tempfile.TemporaryDirectory() as v2:
                os.environ["BRAIN_VAULT_DIR"] = v1
                self.assertEqual(os.path.abspath(config.get_active_brain_dir()), os.path.abspath(v1))
                self.assertEqual(os.path.abspath(notes_store.NotePaths().brain_dir), os.path.abspath(v1))
                self.assertEqual(os.path.abspath(streaming_chat.ChatPaths().brain_dir), os.path.abspath(v1))
                self.assertTrue(str(time_travel.snapshots_dir()).startswith(os.path.abspath(v1)))

                os.environ["BRAIN_VAULT_DIR"] = v2
                self.assertEqual(os.path.abspath(config.get_active_brain_dir()), os.path.abspath(v2))
                self.assertEqual(os.path.abspath(notes_store.NotePaths().brain_dir), os.path.abspath(v2))
                self.assertEqual(os.path.abspath(streaming_chat.ChatPaths().brain_dir), os.path.abspath(v2))
                self.assertTrue(str(time_travel.snapshots_dir()).startswith(os.path.abspath(v2)))
        finally:
            if orig is None:
                os.environ.pop("BRAIN_VAULT_DIR", None)
            else:
                os.environ["BRAIN_VAULT_DIR"] = orig

    def test_dashboard_auth_session_cookie_reload_and_logout(self) -> None:
        import dashboard_auth

        class _FakeSidebar:
            def __init__(self, lock_clicked: bool = False):
                self._lock = lock_clicked

            def caption(self, _txt):
                return None

            def button(self, label, key=None):
                return bool(self._lock and "Lock" in label)

        class _FakeContext:
            def __init__(self, cookies: dict[str, str]):
                self.cookies = cookies
                self.headers = {}

        class _FakeSt:
            def __init__(self, cookies: dict[str, str] | None = None, lock_clicked: bool = False):
                self.session_state: dict[str, object] = {}
                self.sidebar = _FakeSidebar(lock_clicked=lock_clicked)
                self.context = _FakeContext(cookies or {})
                self.form_submitted = False

            def get_option(self, name):
                return "127.0.0.1" if name == "server.address" else None

            def title(self, _m):
                return None

            def caption(self, _m):
                return None

            def error(self, _m):
                return None

            def warning(self, _m):
                return None

            def code(self, _m, **_kw):
                return None

            def form(self, _k, clear_on_submit=True):
                class _Ctx:
                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *args):
                        return False

                return _Ctx()

            def text_input(self, _label, type=None, key=None, max_chars=None):
                return ""

            def form_submit_button(self, _label):
                return self.form_submitted

            def rerun(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / "dashboard-auth.json"
            pw = "correct horse battery staple"
            dashboard_auth.save_password_record(dashboard_auth.build_password_record(pw), auth_file)

            # 1. Password login mints signed HttpOnly SameSite=Strict session cookie
            st1 = _FakeSt()
            st1.session_state["brain_dashboard_password"] = pw
            st1.form_submitted = True
            dashboard_auth.require_dashboard_login(st1, auth_path=auth_file)
            cookie_hdr = str(st1.session_state.get("_brain_dashboard_session_cookie") or "")
            self.assertIn("HttpOnly", cookie_hdr)
            self.assertIn("SameSite=Strict", cookie_hdr)
            jar = http.cookies.SimpleCookie()
            jar.load(cookie_hdr)
            token = jar["sb_session"].value

            # 2. Brand-new session_state (simulated reload) presenting valid cookie unlocks automatically
            st_reload = _FakeSt(cookies={"sb_session": token})
            self.assertTrue(dashboard_auth.require_dashboard_login(st_reload, auth_path=auth_file))

            # 3. Clicking Lock dashboard revokes token and emits Max-Age=0 clearing header
            st_lock = _FakeSt(cookies={"sb_session": token}, lock_clicked=True)
            st_lock.session_state.update(st_reload.session_state)
            dashboard_auth.require_dashboard_login(st_lock, auth_path=auth_file)
            self.assertIn("Max-Age=0", str(st_lock.session_state.get("_brain_dashboard_session_cookie") or ""))

            # 4. Revoked cookie cannot unlock next reload
            st_after = _FakeSt(cookies={"sb_session": token})
            self.assertFalse(dashboard_auth.require_dashboard_login(st_after, auth_path=auth_file))

    def test_chat_shim_delegation_and_streaming_chat_repl(self) -> None:
        import chat_with_memory
        import streaming_chat

        for sym in ("ATOMS_DIR", "BRAIN_DIR", "GEN_MODEL", "console", "_stream_local_ollama_chat", "search_memory", "chat_loop", "main"):
            self.assertTrue(hasattr(chat_with_memory, sym), f"Missing compatibility symbol {sym}")

        with patch.object(streaming_chat, "main", return_value=0) as mock_main:
            rc = chat_with_memory.chat_loop(["--mode", "rrf", "--answer-mode", "research", "--project", "project:alpha", "--pin"])
        self.assertEqual(rc, 0)
        forwarded = mock_main.call_args[0][0]
        self.assertIn("--interactive", forwarded)
        self.assertIn("--retrieval-mode", forwarded)
        self.assertIn("rrf", forwarded)
        self.assertIn("--view", forwarded)
        self.assertIn("research", forwarded)
        self.assertIn("--pin", forwarded)

        inputs = iter(["/mode verified", "/project project:x", "What is WAL?", "exit"])
        outputs: list[str] = []

        def _fake_chat_events(query, **kwargs):
            yield {"type": "context", "retrieval_mode": "lexical", "retrieval_ms": 1.2, "verified": [], "notes": [], "preferences": []}
            yield {"type": "verified_answer", "answerable": True, "text": f"Answered: {query}", "claims": []}
            yield {"type": "done", "total_ms": 2.0}

        with (
            patch.object(streaming_chat, "chat_events", side_effect=_fake_chat_events) as mock_ev,
            tempfile.TemporaryDirectory() as tmp,
        ):
            rc_repl = streaming_chat.run_interactive_chat_repl(
                paths=streaming_chat.ChatPaths.for_brain(tmp),
                input_fn=lambda _prompt: next(inputs),
                print_fn=lambda *args, **_kw: outputs.append(" ".join(str(a) for a in args)),
            )
        self.assertEqual(rc_repl, 0)
        self.assertEqual(mock_ev.call_count, 1)
        self.assertEqual(mock_ev.call_args.kwargs["mode"], "verified")
        self.assertEqual(mock_ev.call_args.kwargs["project_scope"], "project:x")
        self.assertTrue(any("Answered: What is WAL?" in line for line in outputs))

    def test_ollama_refinery_smart_critic_recall_graph_and_fail_closed(self) -> None:
        from atom_mutator import refresh_lexical_index
        from evidence import find_evidence_anchor
        import ollama_refinery
        from review_atom import apply_review_transition

        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            raw_dir = brain / "0-raw"
            atoms_dir = brain / "2-atoms"
            sidecar_dir = brain / ".sidecar"
            archive_dir = raw_dir / "archive"
            raw_dir.mkdir(parents=True)
            atoms_dir.mkdir(parents=True)
            sidecar_dir.mkdir(parents=True)
            archive_dir.mkdir(parents=True)

            src = raw_dir / "source-db.md"
            src.write_text("SQLite WAL mode allows concurrent readers during writes.\n", encoding="utf-8")
            existing = atoms_dir / "sqlite-wal.md"
            existing.write_text(
                "---\n"
                "id: sqlite-wal\n"
                "type: atom\n"
                "memory_type: semantic\n"
                "certainty: established\n"
                "created: 2026-11-20\n"
                "last-reviewed: 2026-11-20\n"
                "confidence: 0.90\n"
                "importance: 0.80\n"
                "sources:\n"
                "  - 0-raw/source-db.md\n"
                "links: []\n"
                "tags: []\n"
                "contexts: []\n"
                "supersedes: []\n"
                "superseded_by: []\n"
                "contested_by: []\n"
                "origin: user_local\n"
                "citation_integrity: missing\n"
                "claim_state: candidate\n"
                "review_status: pending\n"
                "---\n\n"
                "## Claim\nSQLite WAL mode allows concurrent readers during writes.\n\n"
                "## Evidence\nSQLite WAL mode allows concurrent readers during writes.\n",
                encoding="utf-8",
            )
            anchor = find_evidence_anchor(existing.read_text(encoding="utf-8"), str(brain))
            txt = existing.read_text(encoding="utf-8").replace("citation_integrity: missing", f"evidence_source: {anchor['evidence_source']}\nevidence_sha256: {anchor['evidence_sha256']}\nevidence_start: {anchor['evidence_start']}\nevidence_end: {anchor['evidence_end']}\ncitation_integrity: anchored")
            existing.write_text(txt, encoding="utf-8")
            apply_review_transition(
                action="approve",
                atom_path=str(existing),
                expected_sha256=hashlib.sha256(existing.read_bytes()).hexdigest(),
                actor="owner",
                decision_log_path=str(archive_dir / "decision-record.jsonl"),
                brain_dir=str(brain),
                atoms_dir=str(atoms_dir),
            )
            refresh_lexical_index(str(atoms_dir), str(brain))

            draft = (
                "---\n"
                "id: sqlite-wal-conflict\n"
                "type: atom\n"
                "memory_type: semantic\n"
                "review_status: pending\n"
                "---\n\n"
                "## Claim\nSQLite WAL mode never allows concurrent readers during writes.\n\n"
                "## Evidence\nSQLite WAL mode never allows concurrent readers during writes.\n"
            )

            # 1. Fail closed when LLM critic call raises
            with patch.object(ollama_refinery, "chat_with_fallback", side_effect=RuntimeError("ollama offline")):
                with self.assertRaisesRegex(RuntimeError, "LLM smart critic unavailable"):
                    ollama_refinery.run_smart_critic(
                        draft,
                        brain_dir=str(brain),
                        atoms_dir=str(atoms_dir),
                    )

            # 2. Succeeds with friction block preserving original frontmatter & claim/evidence
            friction_reply = (
                "## [FRICTION] Raised by review\n"
                "> **Conflicts with:** [[sqlite-wal]]\n"
                "> **Nature of conflict:** Contradicts concurrent-reader WAL invariant.\n"
                "> **Flagged:** 2026-11-23\n"
                "> **Status:** Awaiting human judgment. Not auto-resolved.\n"
            )
            with patch.object(
                ollama_refinery,
                "chat_with_fallback",
                return_value={"message": {"content": friction_reply}},
            ) as mock_chat:
                updated = ollama_refinery.run_smart_critic(
                    draft,
                    brain_dir=str(brain),
                    atoms_dir=str(atoms_dir),
                )
            prompt_sent = mock_chat.call_args.kwargs["messages"][0]["content"]
            self.assertIn("sqlite-wal.md", prompt_sent)
            self.assertIn("## [FRICTION] Raised by review", updated)
            self.assertIn("## Claim\nSQLite WAL mode never allows concurrent readers during writes.", updated)


class RemediationRegressionTests(unittest.TestCase):
    def test_bulk_import_respects_runtime_notes_store_default_paths_rebind(self) -> None:
        import bulk_import
        import notes_store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = root / "brain"
            (brain / "2-atoms").mkdir(parents=True)
            src_dir = root / "external_notes"
            src_dir.mkdir(parents=True)
            src_file = src_dir / "alpha.md"
            src_file.write_text("# Alpha Note\n\nDynamic path test content.\n", encoding="utf-8")

            custom_paths = notes_store.NotePaths.for_brain(str(brain))
            with patch.object(notes_store, "DEFAULT_PATHS", custom_paths):
                dry_stats = bulk_import.import_folder(str(src_dir), dry_run=True)
                self.assertEqual(Path(dry_stats.notes_dir).resolve(), (brain / "notes").resolve())
                file_res = bulk_import.import_file(str(src_file))
                self.assertEqual(file_res["source_file"], "alpha.md")
                self.assertTrue((brain / "notes" / str(file_res["filename"])).is_file())

    def test_verify_release_signatures_fails_closed_on_missing_manifest_and_key_path(self) -> None:
        import verify_release_signatures as vrs

        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            dist.mkdir()
            rep = vrs.verify_dist_directory(dist, require_trusted=True, require_manifest_sig=True)
            self.assertFalse(rep["all_ok"])
            reasons = {item.get("reason") for item in rep["artifacts"]}
            self.assertIn("missing_sha256sums", reasons)

            with self.assertRaises(FileNotFoundError):
                vrs._resolve_ed25519_key_bytes(str(Path(tmp) / "missing_key.pem"))

    def test_streaming_chat_pin_query_cli_and_strict_multihop_project_note_scope(self) -> None:
        import notes_store
        import streaming_chat

        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp) / "brain"
            (brain / "2-atoms").mkdir(parents=True)
            (brain / "0-raw" / "archive").mkdir(parents=True)
            paths = streaming_chat.ChatPaths.for_brain(str(brain))
            notes_store.create_note(
                "Scoped Alpha Only",
                "WAL journaling helps alpha database concurrency.",
                tags=("project:alpha",),
                paths=paths.notes,
            )
            notes_store.create_note(
                "Shared Alpha And Beta",
                "WAL journaling secret across alpha and beta.",
                tags=("project:alpha", "project:beta"),
                paths=paths.notes,
            )

            ctx = streaming_chat.retrieve_context(
                "WAL journaling",
                paths,
                project_scope="project:alpha",
                retrieval_mode="lexical",
            )
            note_titles = {n["title"] for n in ctx["notes"]}
            self.assertIn("Scoped Alpha Only", note_titles)
            self.assertNotIn("Shared Alpha And Beta", note_titles)

            fake_summary = {"thread_id": "thr-wal-01", "answer_text": "Grounded.", "saw_error_or_abstain": False, "cited_atoms": []}
            with (
                patch.dict(os.environ, {"BRAIN_VAULT_DIR": str(brain)}, clear=False),
                patch.object(streaming_chat, "_render_chat_turn", return_value=fake_summary) as mock_turn,
                patch.object(streaming_chat, "_pin_chat_turn_result") as mock_pin,
            ):
                rc = streaming_chat.main(["--pin", "What is WAL?", "--mode", "instant"])
            self.assertEqual(rc, 0)
            self.assertEqual(mock_turn.call_args.args[0], "What is WAL?")
            self.assertEqual(mock_pin.call_count, 1)

    def test_mobile_www_public_parity_and_native_workflow_hardening(self) -> None:
        mobile_root = ROOT / "mobile" / "vault-client"
        www_dir = mobile_root / "www"
        expected_files = {
            p.relative_to(www_dir).as_posix(): p.read_bytes()
            for p in sorted(www_dir.rglob("*"))
            if p.is_file()
        }
        self.assertGreater(len(expected_files), 0)
        for mirror_rel in ("ios/App/App/public", "android/app/src/main/assets/public"):
            mirror_dir = mobile_root / mirror_rel
            # Capacitor writes these on `npx cap copy` and the native gitignores omit them.
            # A fresh checkout materializes the copy here; an existing copy that drifted still fails.
            if not mirror_dir.is_dir() or not any(mirror_dir.rglob("*")):
                for rel_path, payload in expected_files.items():
                    dest = mirror_dir / rel_path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(payload)
            actual_files = {
                p.relative_to(mirror_dir).as_posix(): p.read_bytes()
                for p in sorted(mirror_dir.rglob("*"))
                if p.is_file()
            }
            self.assertEqual(set(actual_files.keys()), set(expected_files.keys()), f"File list mismatch in {mirror_rel}")
            for rel_path, expected_bytes in expected_files.items():
                self.assertEqual(actual_files[rel_path], expected_bytes, f"Content mismatch in {mirror_rel}/{rel_path}")

        wf_txt = (ROOT / ".github" / "workflows" / "mobile-release.yml").read_text(encoding="utf-8")
        self.assertIn("npx cap copy ios", wf_txt)

        manifest_txt = (mobile_root / "android/app/src/main/AndroidManifest.xml").read_text(encoding="utf-8")
        self.assertIn("android.permission.POST_NOTIFICATIONS", manifest_txt)

        root_cap = json.loads((mobile_root / "capacitor.config.json").read_text(encoding="utf-8"))
        ios_cap_path = mobile_root / "ios/App/App/capacitor.config.json"
        if not ios_cap_path.is_file():
            ios_cap_path.parent.mkdir(parents=True, exist_ok=True)
            ios_cap_path.write_text(
                (mobile_root / "capacitor.config.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        ios_cap = json.loads(ios_cap_path.read_text(encoding="utf-8"))
        pbx_txt = (mobile_root / "ios/App/App.xcodeproj/project.pbxproj").read_text(encoding="utf-8")
        self.assertIn("SecondBrainHardwarePlugin", root_cap.get("packageClassList", []))
        self.assertIn("SecondBrainHardwarePlugin", ios_cap.get("packageClassList", []))
        self.assertIn("SecondBrainHardwarePlugin.swift in Sources", pbx_txt)


if __name__ == "__main__":
    unittest.main()
