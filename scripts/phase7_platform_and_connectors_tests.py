# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["httpx>=0.28,<1", "Pillow>=10", "pypdf>=4", "PyYAML"]
# ///
"""Phase 7 (Epic 2B): Multimodal video ingestion, desktop auto-updater, bidirectional mobile E2EE,
14 IDE/AI host connectors + custom profiles, Safari/media browser clipper, and live third-party benchmarks.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave
import zipfile

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _make_solid_png_bytes(color: tuple[int, int, int], *, width: int = 16, height: int = 16, add_text_chunk: bool = False) -> bytes:
    from PIL import Image, PngImagePlugin

    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    if add_text_chunk:
        meta = PngImagePlugin.PngInfo()
        meta.add_text("Author", "Tracking-Exif-Secret")
        meta.add_text("Comment", "Should be stripped by Pillow sanitizer")
        img.save(buf, format="PNG", pnginfo=meta)
    else:
        img.save(buf, format="PNG")
    return buf.getvalue()


def _make_pattern_png_bytes(invert: bool = False) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (18, 16))
    for y in range(16):
        for x in range(18):
            dark = (x < 9) ^ invert
            val = 15 if dark else 245
            img.putpixel((x, y), (val, val, val))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_wav_bytes(duration_ms: int = 120) -> bytes:
    buf = io.BytesIO()
    nframes = int(16000 * duration_ms / 1000)
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(struct.pack("<" + "h" * nframes, *([0] * nframes)))
    return buf.getvalue()


def _make_simple_pdf_bytes(lines: list[str]) -> bytes:
    """Create a valid minimal single-page uncompressed PDF containing extractable Helvetica text."""
    escaped = []
    for i, ln in enumerate(lines):
        safe = str(ln).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        dy = "0 -16 Td " if i > 0 else ""
        escaped.append(f"{dy}({safe}) Tj")
    stream_txt = "BT\n/F1 12 Tf\n72 720 Td\n" + "\n".join(escaped) + "\nET\n"
    stream_bytes = stream_txt.encode("latin-1", errors="replace")

    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n"
        ),
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
        (
            f"5 0 obj\n<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii")
            + stream_bytes
            + b"\nendstream\nendobj\n"
        ),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objs:
        offsets.append(len(out))
        out.extend(obj)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii"))
    return bytes(out)


def _seed_clean_vault(root: Path) -> Path:
    brain = root / "brain"
    for sub in ("0-raw", "0-raw/inbox", "0-raw/archive", "0-raw/note-snapshots", "2-atoms", "3-threads", "notes", ".sidecar"):
        (brain / sub).mkdir(parents=True, exist_ok=True)
    return brain


class Phase7PlatformAndConnectorsTests(unittest.TestCase):
    def test_2b1_video_extraction_dhash_dedup_webm_routing_and_api(self) -> None:
        from api_product import ensure_owner_token
        import config
        import ingest_multimodal as im
        from memory_api import MemoryApiRuntimeConfig, MemoryLoopbackHTTPServer, generate_memory_api_token
        import video_extractor as ve

        self.assertTrue({".mp4", ".mov", ".mkv", ".webm"}.issubset(config.VIDEO_EXTS))
        self.assertIn(".mp4", im.SUPPORTED_INGEST_EXTS)
        self.assertEqual(ve.format_timestamp_hms(3723.456), "01:02:03.456")

        with tempfile.TemporaryDirectory(prefix="sb_2b1_vid_") as td:
            tpath = Path(td)
            brain = _seed_clean_vault(tpath)
            sec_dir = tpath / "security"
            sec_dir.mkdir(parents=True)

            # 1. Verify 64-bit dHash dedup: identical frames distance == 0, inverted gradient > threshold
            p1 = tpath / "f1.png"
            p2 = tpath / "f2.png"
            p3 = tpath / "f3.png"
            p1.write_bytes(_make_pattern_png_bytes(invert=False))
            p2.write_bytes(_make_pattern_png_bytes(invert=False))
            p3.write_bytes(_make_pattern_png_bytes(invert=True))
            h1, h2, h3 = ve.compute_dhash64(str(p1)), ve.compute_dhash64(str(p2)), ve.compute_dhash64(str(p3))
            self.assertEqual(ve.hamming_distance64(h1, h2), 0)
            self.assertGreater(ve.hamming_distance64(h1, h3), ve.DHASH_DEDUP_THRESHOLD)

            # 2. Audio-only .webm vs video .webm routing
            audio_webm = tpath / "voice.webm"
            audio_webm.write_bytes(b"\x1aE\xdf\xa3audio-only")
            video_webm = tpath / "screen.webm"
            video_webm.write_bytes(b"\x1aE\xdf\xa3video-stream")

            def _probe_runner(cmd, **_kw):
                fp = cmd[-1]
                streams = [{"codec_type": "audio", "codec_name": "opus"}]
                if fp == str(video_webm):
                    streams.append({"codec_type": "video", "codec_name": "vp9", "width": 640, "height": 480})
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    json.dumps({"streams": streams, "format": {"duration": "12.0"}}),
                    "",
                )

            with patch.object(ve, "resolve_ffmpeg_tools", return_value=("ffmpeg", "ffprobe")):
                self.assertFalse(ve.is_video_container(str(audio_webm), runner=_probe_runner))
                self.assertTrue(ve.is_video_container(str(video_webm), runner=_probe_runner))

            # 3. End-to-end local video ingest + timeline merge + DLP quarantine
            demo_mp4 = tpath / "architecture-demo.mp4"
            demo_mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
            wav_bytes = _make_wav_bytes()
            pat_a = _make_pattern_png_bytes(invert=False)
            pat_b = _make_pattern_png_bytes(invert=True)

            def _mock_video_runner(cmd, **_kw):
                exe = Path(cmd[0]).name.lower()
                if "ffprobe" in exe:
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        json.dumps({
                            "streams": [
                                {"codec_type": "video", "codec_name": "h264", "width": 640, "height": 480},
                                {"codec_type": "audio", "codec_name": "aac"},
                            ],
                            "format": {"duration": "15.0"},
                        }),
                        "",
                    )
                out_arg = cmd[-1]
                if out_arg.endswith(".wav"):
                    Path(out_arg).write_bytes(wav_bytes)
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                if "keyframe_" in out_arg:
                    stage_dir = Path(out_arg).parent
                    (stage_dir / "keyframe_0001.png").write_bytes(pat_a)
                    (stage_dir / "keyframe_0002.png").write_bytes(pat_a)  # duplicate -> deduped
                    (stage_dir / "keyframe_0003.png").write_bytes(pat_b)  # distinct scene -> kept
                    stderr = "pts_time:0.500\npts_time:2.500\npts_time:8.500\n"
                    return subprocess.CompletedProcess(cmd, 0, "", stderr)
                return subprocess.CompletedProcess(cmd, 1, "", "unexpected command")

            with patch.object(ve, "resolve_ffmpeg_tools", return_value=("ffmpeg", "ffprobe")):
                res = im.ingest_local_video_file(
                    str(demo_mp4),
                    brain_dir=brain,
                    runner=_mock_video_runner,
                    frame_describer=lambda fp: f"Slide diagram for {Path(fp).name}",
                    audio_transcriber=lambda _wav: ("[00:01.000 -> 00:05.000] Reviewing Three-Store boundaries.", "tiny.en"),
                )
            self.assertEqual(res["status"], "ingested")
            out_md = brain / str(res["output_rel_path"])
            self.assertTrue(out_md.is_file())
            txt = out_md.read_text(encoding="utf-8")
            self.assertIn("[TIMELINE]", txt)
            self.assertIn("Three-Store boundaries", txt)
            self.assertIn("keyframe_count: 2", txt)
            body_only = txt.split("<!-- extraction_body -->", 1)[-1]
            self.assertEqual(body_only.count("[FRAME t="), 2)  # duplicate keyframe collapsed!

            # 4. Secret leak in spoken transcript routes video extraction to 0-raw/inbox/ quarantine
            secret_mp4 = tpath / "secret-meeting.mp4"
            secret_mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
            with patch.object(ve, "resolve_ffmpeg_tools", return_value=("ffmpeg", "ffprobe")):
                res_q = im.ingest_local_video_file(
                    str(secret_mp4),
                    brain_dir=brain,
                    runner=_mock_video_runner,
                    frame_describer=lambda _fp: "Clean UI screen",
                    audio_transcriber=lambda _wav: (
                        "[00:01.000 -> 00:04.000] Never share sk-proj-abcdefghijklmnopqrstuvwxyz1234567890ABCD",
                        "tiny.en",
                    ),
                )
            self.assertTrue(res_q["quarantined"])
            self.assertTrue(str(res_q["output_rel_path"]).startswith("0-raw/inbox/"))
            q_text = (brain / str(res_q["output_rel_path"])).read_text(encoding="utf-8")
            self.assertNotIn("sk-proj-abcdefghijklmnopqrstuvwxyz1234567890ABCD", q_text)

            # 5. Loopback HTTP POST /v1/ingest/video-file owner-gated
            tok_p = sec_dir / "memory-api-token"
            own_p = sec_dir / "owner-token"
            agent_tok, _ = generate_memory_api_token(tok_p)
            owner_tok = ensure_owner_token(own_p)
            rt = MemoryApiRuntimeConfig(
                brain_dir=str(brain),
                token_path=tok_p,
                owner_token_path=own_p,
                policy_path=sec_dir / "policy.json",
                security_dir=sec_dir,
            )
            srv = MemoryLoopbackHTTPServer("127.0.0.1", 0, rt)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                base = f"http://127.0.0.1:{srv.server_address[1]}"
                with httpx.Client(trust_env=False, timeout=10.0) as client:
                    # Non-owner rejected 403
                    r_denied = client.post(
                        f"{base}/v1/ingest/video-file",
                        headers={"Authorization": f"Bearer {agent_tok}"},
                        json={"filepath": str(demo_mp4)},
                    )
                    self.assertEqual(r_denied.status_code, 403)

                    api_mp4 = tpath / "api-clip.mp4"
                    api_mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
                    with patch(
                        "ingest_multimodal.ingest_local_video_file",
                        return_value={"status": "ingested", "category": "video", "output_rel_path": "0-raw/api-clip-video.md"},
                    ) as mock_ing:
                        r_ok = client.post(
                            f"{base}/v1/ingest/video-file",
                            headers={"Authorization": f"Bearer {agent_tok}", "X-Owner-Token": owner_tok},
                            json={"filepath": str(api_mp4)},
                        )
                        self.assertEqual(r_ok.status_code, 201)
                        self.assertEqual(r_ok.json()["category"], "video")
                        mock_ing.assert_called_once()
            finally:
                srv.shutdown()
                srv.server_close()

    def test_2b2_desktop_auto_updater_contract_and_js_tests(self) -> None:
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "Node.js is required to test Desktop auto-updater module.")
        pkg = json.loads((REPO_ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
        self.assertIn("electron-updater", pkg.get("dependencies", {}))
        pub_cfg = (pkg.get("build") or {}).get("publish")
        pub_items = pub_cfg if isinstance(pub_cfg, list) else ([pub_cfg] if isinstance(pub_cfg, dict) else [])
        self.assertTrue(any(isinstance(it, dict) and it.get("provider") == "github" for it in pub_items))

        updater_test = REPO_ROOT / "desktop" / "tests" / "updater.test.js"
        self.assertTrue(updater_test.is_file())
        proc = subprocess.run([node_bin, "--test", str(updater_test)], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + "\n" + proc.stderr)

    def test_2b3_mobile_e2ee_bidirectional_cross_runtime_notes_atoms_and_ios_shortcut(self) -> None:
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "Node.js is required for cross-runtime mobile E2EE verification.")
        import build_ios_shortcut as bis
        import mobile_vault_client as mvc
        import vault_sync as vs
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        with tempfile.TemporaryDirectory(prefix="sb_2b3_mob_") as td:
            tpath = Path(td)
            desktop_brain = _seed_clean_vault(tpath / "desktop")
            desktop_state = tpath / "desktop_state"
            pull_brain = _seed_clean_vault(tpath / "restored")
            pull_state = tpath / "pull_state"
            sync_target = tpath / "sync_store"
            sync_target.mkdir(parents=True)

            # Seed an anchored canonical atom on Desktop BEFORE initial push
            from evidence import find_evidence_anchor

            src_rel = "0-raw/source-spec.md"
            src_txt = "# Spec\nExact verifiable quote: Zero-knowledge sync uses AES-256-GCM.\n"
            (desktop_brain / src_rel).write_text(src_txt, encoding="utf-8")
            q = "Zero-knowledge sync uses AES-256-GCM."

            draft_atom = (
                "---\n"
                "id: atom-sync-enc\n"
                "type: atom\n"
                "memory_type: semantic\n"
                "certainty: high\n"
                "sources:\n"
                f"  - {src_rel}\n"
                "review_status: pending\n"
                "claim_state: candidate\n"
                "citation_integrity: missing\n"
                "---\n\n"
                "## Claim\nZero-knowledge sync encrypts all vault objects with AES-256-GCM.\n\n"
                f"## Evidence\n{q}\n"
            )
            anchor = find_evidence_anchor(draft_atom, str(desktop_brain))
            p_head, p_fm, p_body = draft_atom.split("---", 2)
            for k, v in anchor.items():
                p_fm += f"{k}: {v}\n"
            p_fm = p_fm.replace("citation_integrity: missing", "citation_integrity: anchored")
            good_atom = f"---{p_fm}---{p_body}"
            (desktop_brain / "2-atoms" / "sync-enc.md").write_text(good_atom, encoding="utf-8")

            bad_atom = (
                "---\n"
                "id: atom-bad-anchor\n"
                "type: atom\n"
                "memory_type: semantic\n"
                "sources:\n"
                "  - 0-raw/missing-src.md\n"
                "review_status: pending\n"
                "claim_state: candidate\n"
                "---\n\n"
                "## Claim\nFabricated unanchored claim.\n\n## Evidence\nMissing text.\n"
            )
            (desktop_brain / "2-atoms" / "bad-anchor.md").write_text(bad_atom, encoding="utf-8")

            vs.init_sync(
                str(sync_target),
                backend_type="local_folder",
                brain_dir=desktop_brain,
                passphrase="mobile-passphrase-2026",
                state_dir=desktop_state,
            )
            vs.sync_push(brain_dir=desktop_brain, passphrase="mobile-passphrase-2026", state_dir=desktop_state)

            # Second desktop/verifier device joins BEFORE mobile pushes, so it only trusts desktop_brain
            vs.join_sync(
                str(sync_target),
                backend_type="local_folder",
                brain_dir=pull_brain,
                passphrase="mobile-passphrase-2026",
                state_dir=pull_state,
            )

            # Execute mobile JS pull -> offline note write -> anchor-gated atom review -> JS Ed25519 push
            js_script = f"""
const fs = require("fs");
const path = require("path");
const assert = require("assert/strict");
const VaultCrypto = require({json.dumps(str(REPO_ROOT / 'mobile/vault-client/www/lib/vault-crypto.js'))});
const Engine = require({json.dumps(str(REPO_ROOT / 'mobile/vault-client/www/lib/vault-epistemic-engine.js'))});

const storeDir = {json.dumps(str(sync_target))};
const backend = {{
  async getObject(relPath) {{
    const fp = path.join(storeDir, ...relPath.split("/"));
    if (!fs.existsSync(fp)) return null;
    return new Uint8Array(fs.readFileSync(fp));
  }},
  async putObject(relPath, bytes) {{
    const fp = path.join(storeDir, ...relPath.split("/"));
    fs.mkdirSync(path.dirname(fp), {{ recursive: true }});
    fs.writeFileSync(fp, Buffer.from(bytes));
  }},
}};

(async () => {{
  const pulled = await VaultCrypto.pullVaultFromBackend(
    backend,
    {{ passphrase: "mobile-passphrase-2026", forcePureJsEd25519: true }},
    {{ files: {{}}, syncCursor: {{ epoch: 0 }} }}
  );
  const pending = Engine.listPendingAtomsOffline(pulled.files);
  assert.equal(pending.length, 2);

  // Anchor gate MUST refuse unanchored atom approval
  assert.throws(
    () => Engine.reviewAtomOffline(pulled.files, "bad-anchor.md", "accepted", {{ actor: "mobile-owner" }}),
    /invalid source anchor/i
  );

  // Anchored atom approves cleanly and appends decision-record.jsonl
  const reviewed = Engine.reviewAtomOffline(pulled.files, "sync-enc.md", "accepted", {{ actor: "mobile-owner" }});
  const noteRes = Engine.createOrUpdateNoteOffline(reviewed.files, {{
    title: "Mobile Field Note",
    body: "Written offline on iOS with Ed25519 write-back.",
    tags: ["ios", "e2ee"],
  }});

  // Conflict resolution test (local vs remote)
  const conflictResFiles = Engine.resolveSyncConflictOffline(
    noteRes.files,
    {{ relPath: "notes/conflict.md", localBytes: VaultCrypto.utf8Bytes("local"), remoteBytes: VaultCrypto.utf8Bytes("remote") }},
    "accept_remote"
  );
  assert.equal(VaultCrypto.utf8DecodeStrict(conflictResFiles["notes/conflict.md"]), "remote");
  assert.equal(Engine.approvedRevisionIsCurrent("sync-enc.md", reviewed.files), true);

  const kp = await VaultCrypto.generateEd25519Keypair();
  const sig = await VaultCrypto.signEd25519Message(VaultCrypto.base64ToBytes(kp.seedB64), VaultCrypto.utf8Bytes("verify-me"), {{ forcePureJs: true }});
  const devId = "ios-iphone-15";
  const pushed = await VaultCrypto.pushVaultToBackend(
    backend,
    {{ passphrase: "mobile-passphrase-2026", forcePureJsEd25519: true }},
    {{ files: noteRes.files, syncCursor: pulled.syncCursor }},
    {{ deviceId: devId, seedB64: kp.seedB64, pubkeyB64: kp.pubkeyB64 }}
  );
  process.stdout.write(JSON.stringify({{
    deviceId: devId,
    pubkeyB64: kp.pubkeyB64,
    sigB64: VaultCrypto.bytesToBase64(sig),
    epoch: pushed.epoch,
    noteRelPath: noteRes.relPath,
  }}));
}})().catch((e) => {{ console.error(e); process.exit(1); }});
"""
            jproc = subprocess.run([node_bin, "-e", js_script], capture_output=True, text=True, check=False)
            self.assertEqual(jproc.returncode, 0, jproc.stdout + "\n" + jproc.stderr)
            jres = json.loads(jproc.stdout)
            Ed25519PublicKey.from_public_bytes(base64.b64decode(jres["pubkeyB64"])).verify(
                base64.b64decode(jres["sigB64"]),
                b"verify-me",
            )

            # Untrusted mobile peer fails closed when trust_new_peers=False until register_mobile_peer_pubkey pins it!
            with self.assertRaises(vs.SyncSecurityError):
                vs.sync_pull(
                    brain_dir=pull_brain,
                    passphrase="mobile-passphrase-2026",
                    trust_new_peers=False,
                    state_dir=pull_state,
                )

            # Register mobile peer Ed25519 public key -> Python sync_pull succeeds & verifies E2EE write-back!
            mvc.register_mobile_peer_pubkey(
                brain_dir=pull_brain,
                device_id=jres["deviceId"],
                device_pubkey_b64=jres["pubkeyB64"],
                state_dir=pull_state,
            )
            pulled_py = vs.sync_pull(
                brain_dir=pull_brain,
                passphrase="mobile-passphrase-2026",
                trust_new_peers=False,
                state_dir=pull_state,
            )
            self.assertEqual(pulled_py["status"], "pulled")
            self.assertIn("Written offline on iOS with Ed25519 write-back.", (pull_brain / jres["noteRelPath"]).read_text(encoding="utf-8"))
            self.assertIn("sync-enc.md", (pull_brain / "0-raw" / "archive" / "decision-record.jsonl").read_text(encoding="utf-8"))

            # Native iOS Shortcut binary plist builder
            sc_res = bis.build_ios_shortcut(out_dir=tpath / "sc", sign_if_available=False)
            self.assertEqual(sc_res["status"], "unsigned_generated")
            raw_plist = plistlib.loads(Path(sc_res["unsigned_path"]).read_bytes())
            self.assertTrue(len(raw_plist["WFWorkflowActions"]) >= 3)
            self.assertTrue(any(q.get("ParameterKey") == "WFURLActionURL" for q in raw_plist["WFWorkflowImportQuestions"]))

    def test_2b4_all_14_ide_hosts_custom_profiles_and_plugin_bundles(self) -> None:
        from api_product import ensure_owner_token
        import build_obsidian_plugin as bop
        import host_adapters as ha
        import ide_connector as ic
        from memory_api import MemoryApiRuntimeConfig, MemoryLoopbackHTTPServer, generate_memory_api_token

        with tempfile.TemporaryDirectory(prefix="sb_2b4_hosts_") as td:
            tpath = Path(td)
            fake_home = tpath / "home"
            fake_home.mkdir()
            state_dir = tpath / "state"
            brain = _seed_clean_vault(tpath / "vault")

            with patch.object(ic, "custom_mcp_hosts_path", return_value=state_dir / "custom-mcp-hosts.json"), patch.dict(
                os.environ, {"BRAIN_VAULT_DIR": str(brain)}, clear=False
            ):
                targets = ic.host_targets(home=fake_home, state_dir=state_dir)
                expected_14 = {
                    "claude-code", "claude-desktop", "cursor", "windsurf", "vscode", "antigravity",
                    "codex-cli", "gemini-cli", "cline", "roo-code", "continue", "zed", "jetbrains", "neovim",
                }
                self.assertEqual(set(targets.keys()), expected_14)

                # Codex CLI TOML preservation + fenced block + AGENTS.md
                codex_cfg = fake_home / ".codex" / "config.toml"
                codex_cfg.parent.mkdir(parents=True, exist_ok=True)
                codex_cfg.write_text('model = "gpt-5.2-codex"\n', encoding="utf-8")
                c_res = ic.connect("codex-cli", home=fake_home, vault_dir=str(brain))
                self.assertEqual(c_res["host"], "codex-cli")
                toml_txt = codex_cfg.read_text(encoding="utf-8")
                self.assertIn('model = "gpt-5.2-codex"', toml_txt)
                self.assertIn("mcp_servers.second-brain", toml_txt)
                self.assertTrue((fake_home / ".codex" / "AGENTS.md").is_file())
                ic.disconnect("codex-cli", home=fake_home)
                toml_after = codex_cfg.read_text(encoding="utf-8")
                self.assertIn('model = "gpt-5.2-codex"', toml_after)
                self.assertNotIn("mcp_servers.second-brain", toml_after)

                # Continue YAML + Zed JSONC + Gemini CLI
                cont_res = ic.connect("continue", home=fake_home, vault_dir=str(brain))
                self.assertTrue(Path(cont_res["config_path"]).is_file())
                zed_res = ic.connect("zed", home=fake_home, vault_dir=str(brain))
                self.assertIn("context_servers", Path(zed_res["config_path"]).read_text(encoding="utf-8"))
                gem_res = ic.connect("gemini-cli", home=fake_home, vault_dir=str(brain))
                self.assertIn("mcpServers", Path(gem_res["config_path"]).read_text(encoding="utf-8"))

                # Custom MCP host registration + built-in guard
                with self.assertRaises(ValueError):
                    ic.register_custom_mcp_host("codex-cli", label="Overwrite", config_path=fake_home / "x.json", state_dir=state_dir)
                ic.register_custom_mcp_host(
                    "acme-ide",
                    label="Acme IDE",
                    config_path=fake_home / ".acme" / "mcp.json",
                    servers_key="mcpServers",
                    config_format="json",
                    cloud_model=False,
                    state_dir=state_dir,
                )
                self.assertIn("acme-ide", ic.host_targets(home=fake_home, state_dir=state_dir))
                self.assertTrue(ic.remove_custom_mcp_host("acme-ide", state_dir=state_dir))
                self.assertNotIn("acme-ide", ic.host_targets(home=fake_home, state_dir=state_dir))

            # Custom AI Model Host Profile CRUD + loopback /v1/hosts API
            policy_path = tpath / "host-policy.json"
            with self.assertRaises(ValueError):
                ha.create_custom_host_policy(
                    "ollama_local",
                    provider="ollama",
                    transport="local",
                    default_model="qwen2.5:7b",
                    endpoint_url="http://127.0.0.1:11434",
                    policy_path=policy_path,
                )
            ha.create_custom_host_policy(
                "local_vllm",
                provider="openai_compatible",
                transport="local",
                default_model="qwen2.5-7b-awq",
                endpoint_url="http://127.0.0.1:8000",
                enabled=False,
                policy_path=policy_path,
            )
            loaded = ha.load_host_policies(policy_path)
            self.assertIn("local_vllm", loaded)
            self.assertTrue(ha.delete_custom_host_policy("local_vllm", policy_path=policy_path))
            self.assertNotIn("local_vllm", ha.load_host_policies(policy_path))

            sec_dir = tpath / "sec"
            sec_dir.mkdir()
            tok_p = sec_dir / "memory-api-token"
            own_p = sec_dir / "owner-token"
            agent_tok, _ = generate_memory_api_token(tok_p)
            owner_tok = ensure_owner_token(own_p)
            rt = MemoryApiRuntimeConfig(
                brain_dir=str(brain),
                token_path=tok_p,
                owner_token_path=own_p,
                policy_path=policy_path,
                security_dir=sec_dir,
            )
            srv = MemoryLoopbackHTTPServer("127.0.0.1", 0, rt)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                base = f"http://127.0.0.1:{srv.server_address[1]}"
                with httpx.Client(trust_env=False, timeout=10.0) as client:
                    r_add = client.post(
                        f"{base}/v1/hosts",
                        headers={"Authorization": f"Bearer {agent_tok}", "X-Owner-Token": owner_tok},
                        json={
                            "host_id": "custom_mlx",
                            "provider": "openai_compatible",
                            "transport": "local",
                            "default_model": "qwen2.5-7b-4bit",
                            "endpoint_url": "http://127.0.0.1:8080",
                        },
                    )
                    self.assertEqual(r_add.status_code, 201)
                    r_del = client.delete(
                        f"{base}/v1/hosts/custom_mlx",
                        headers={"Authorization": f"Bearer {agent_tok}", "X-Owner-Token": owner_tok},
                    )
                    self.assertEqual(r_del.status_code, 200)
            finally:
                srv.shutdown()
                srv.server_close()

            # Neovim plugin, Raycast extension, Obsidian plugin zip build
            self.assertTrue((REPO_ROOT / "plugins" / "neovim" / "second-brain.nvim" / "lua" / "second_brain" / "init.lua").is_file())
            ray_pkg = json.loads((REPO_ROOT / "extensions" / "raycast-second-brain" / "package.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(ray_pkg.get("commands", [])), 3)
            obs_res = bop.build_obsidian_plugin(tpath / "dist-obs")
            with zipfile.ZipFile(obs_res["zip_path"], "r") as zf:
                names = set(zf.namelist())
                self.assertIn("second-brain-obsidian/manifest.json", names)
                self.assertIn("second-brain-obsidian/main.js", names)

    def test_2b5_safari_clipper_media_capture_commands_and_bundle(self) -> None:
        from api_product import ensure_owner_token
        import build_clipper as bc
        from memory_api import MemoryApiRuntimeConfig, MemoryLoopbackHTTPServer, generate_memory_api_token
        import web_clipper as wc
        from PIL import Image

        SAFARI_ORIGIN = "safari-web-extension://12345678-1234-4123-8123-123456789abc"
        self.assertEqual(wc.validate_extension_origin(SAFARI_ORIGIN.upper()), SAFARI_ORIGIN)
        with self.assertRaises(wc.ClipperError):
            wc.validate_extension_origin("safari-web-extension://not-a-uuid")

        with tempfile.TemporaryDirectory(prefix="sb_2b5_clip_") as td:
            tpath = Path(td)
            brain = _seed_clean_vault(tpath)
            sec_dir = tpath / "security"
            sec_dir.mkdir(parents=True)

            paired = wc.create_clipper_pairing(SAFARI_ORIGIN, label="Safari macOS", security_dir=sec_dir)
            tok_p = sec_dir / "memory-api-token"
            own_p = sec_dir / "owner-token"
            generate_memory_api_token(tok_p)
            ensure_owner_token(own_p)
            rt = MemoryApiRuntimeConfig(
                brain_dir=str(brain),
                token_path=tok_p,
                owner_token_path=own_p,
                policy_path=sec_dir / "policy.json",
                security_dir=sec_dir,
            )
            srv = MemoryLoopbackHTTPServer("127.0.0.1", 0, rt)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                port = srv.server_address[1]
                base = f"http://127.0.0.1:{port}"
                safari_headers = {
                    "Host": f"127.0.0.1:{port}",
                    "Origin": SAFARI_ORIGIN,
                    "Sec-Fetch-Site": "cross-site",
                    "Authorization": f"Bearer {paired['token']}",
                }
                with httpx.Client(trust_env=False, timeout=10.0) as client:
                    opt = client.options(
                        f"{base}/v1/clipper/capture-media",
                        headers={"Host": f"127.0.0.1:{port}", "Origin": SAFARI_ORIGIN},
                    )
                    self.assertEqual(opt.status_code, 204)
                    self.assertEqual(opt.headers.get("access-control-allow-origin"), SAFARI_ORIGIN)

                    # Clean PNG screenshot with injected tEXt metadata stripped by Pillow
                    dirty_png = _make_solid_png_bytes((20, 100, 180), add_text_chunk=True)
                    r_png = client.post(
                        f"{base}/v1/clipper/capture-media",
                        headers=safari_headers,
                        json={
                            "url": "https://example.org/dashboard",
                            "title": "Dashboard View",
                            "kind": "screenshot_png",
                            "content_base64": "data:image/png;base64," + base64.b64encode(dirty_png).decode("ascii"),
                            "tags": ["visual"],
                            "destination": "note",
                        },
                    )
                    self.assertEqual(r_png.status_code, 201)
                    j_png = r_png.json()
                    self.assertFalse(j_png["quarantined"])
                    arch_png = brain / j_png["media_archive_path"]
                    self.assertTrue(arch_png.is_file())
                    with Image.open(arch_png) as sanitized:
                        self.assertNotIn("Author", sanitized.info)
                        self.assertNotIn("Comment", sanitized.info)

                    # Clean PDF extraction + archival
                    clean_pdf = _make_simple_pdf_bytes(["Second Brain PDF Capture Architecture Verified."])
                    r_pdf = client.post(
                        f"{base}/v1/clipper/capture-media",
                        headers=safari_headers,
                        json={
                            "url": "https://example.org/whitepaper.pdf",
                            "title": "Architecture PDF",
                            "kind": "pdf_document",
                            "content_base64": base64.b64encode(clean_pdf).decode("ascii"),
                            "destination": "note",
                        },
                    )
                    self.assertEqual(r_pdf.status_code, 201)
                    j_pdf = r_pdf.json()
                    self.assertFalse(j_pdf["quarantined"])
                    self.assertTrue((brain / j_pdf["media_archive_path"]).is_file())

                    # Secret in PDF text routes to quarantined 0-raw/inbox/ & withholds snapshot/archive
                    secret_pdf = _make_simple_pdf_bytes(["Leak sk-proj-abcdefghijklmnopqrstuvwxyz1234567890ABCD"])
                    r_sec_pdf = client.post(
                        f"{base}/v1/clipper/capture-media",
                        headers=safari_headers,
                        json={
                            "url": "https://example.org/secret.pdf",
                            "title": "Secret PDF",
                            "kind": "pdf_document",
                            "content_base64": base64.b64encode(secret_pdf).decode("ascii"),
                            "destination": "note",
                        },
                    )
                    self.assertEqual(r_sec_pdf.status_code, 201)
                    j_sec = r_sec_pdf.json()
                    self.assertTrue(j_sec["quarantined"])
                    self.assertIsNone(j_sec["snapshot_path"])
                    self.assertIsNone(j_sec["media_archive_path"])
                    self.assertEqual(j_sec["location"], "0-raw/inbox/")
            finally:
                srv.shutdown()
                srv.server_close()

            # Safari bundle generation & xcrun wrapper
            bundles = bc.build_store_clipper_bundles(tpath / "dist-clip")
            self.assertTrue(Path(bundles["safari_zip"]).is_file())
            with zipfile.ZipFile(bundles["safari_zip"], "r") as zf:
                s_man = json.loads(zf.read("manifest.json").decode("utf-8"))
                self.assertNotIn("browser_specific_settings", s_man)
                self.assertIn("capture-screenshot", s_man["commands"])
                self.assertIn("capture-pdf", s_man["commands"])

            with patch.object(bc.sys, "platform", "win32"):
                skipped = bc.convert_safari_web_extension(project_out_dir=tpath / "xcode")
                self.assertEqual(skipped["status"], "skipped_non_macos")

            conv_calls: list[list[str]] = []
            conv = bc.convert_safari_web_extension(
                project_out_dir=tpath / "xcode2",
                runner=lambda cmd, **_kw: conv_calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0, "ok", ""),
            )
            self.assertEqual(conv["status"], "converted")
            self.assertIn("safari-web-extension-converter", conv_calls[0])

    def test_2b6_live_third_party_adapters_and_graceful_skip(self) -> None:
        import live_third_party_benchmark as ltb

        # 1. Availability checks fail gracefully without env keys or uninstalled optional libs
        zep = ltb.ZepLiveAdapter()
        with patch.dict(os.environ, {}, clear=True):
            ok_z, _reason_z = zep.check_available()
            self.assertFalse(ok_z)

        cmd = ltb.build_ephemeral_uv_benchmark_command(include_basic_memory=True, include_graphiti_kuzu=True, include_zep=True)
        self.assertIn("basic-memory", cmd)
        self.assertIn("graphiti-core", cmd)
        self.assertIn("kuzu", cmd)
        self.assertIn("zep-cloud", cmd)

        # 2. Inject mock BasicMemory + GraphitiKuzu + Zep adapters into mini-vault benchmark
        with tempfile.TemporaryDirectory(prefix="sb_2b6_bench_") as td:
            tpath = Path(td)
            brain = _seed_clean_vault(tpath)
            (brain / ".chroma").mkdir(parents=True, exist_ok=True)
            src = brain / "0-raw" / "s.md"
            src.write_text("Structured JSON logs aid debugging.", encoding="utf-8")
            sha_s = hashlib.sha256(src.read_bytes()).hexdigest()
            from atom_mutator import apply_review_transition, approved_revision_is_current
            from evidence import find_evidence_anchor
            import lexical_store as ls
            import ollama_utils as ou
            import recall as recall_mod
            import vector_store as vs

            draft_atom = (
                "---\n"
                "id: json-logs\n"
                "type: atom\n"
                "memory_type: semantic\n"
                "review_status: pending\n"
                "claim_state: candidate\n"
                "citation_integrity: missing\n"
                "contexts:\n"
                "  - project:alpha\n"
                "sources:\n"
                "  - 0-raw/s.md\n"
                "---\n\n"
                "## Claim\nStructured JSON logs aid debugging.\n\n"
                "## Evidence\nStructured JSON logs aid debugging.\n"
            )
            anchor = find_evidence_anchor(draft_atom, str(brain))
            _, fm_raw, body_raw = draft_atom.split("---", 2)
            for k, v in anchor.items():
                fm_raw += f"{k}: {v}\n"
            fm_raw = fm_raw.replace("citation_integrity: missing", "citation_integrity: anchored")
            atom = brain / "2-atoms" / "json-logs.md"
            atom.write_text(f"---{fm_raw}---{body_raw}", encoding="utf-8")

            apply_review_transition(
                action="approve",
                atom_path=str(atom),
                expected_sha256=hashlib.sha256(atom.read_bytes()).hexdigest(),
                actor="tester",
                decision_log_path=str(brain / "0-raw" / "archive" / "decision-record.jsonl"),
                brain_dir=str(brain),
                atoms_dir=str(brain / "2-atoms"),
            )
            ls.rebuild_lexical_index(str(brain / "2-atoms"), str(brain / ".sidecar" / "lexical.sqlite3"))
            col = vs.get_vector_collection(str(brain / ".chroma"), name="atoms", create=True)
            vs.close_vector_collection(col)

            ds = {
                "schema_version": 2,
                "dataset_id": "mini-opt",
                "dataset_version": "1.0.0",
                "status": "pending_owner_review",
                "draft_provenance": {"drafted_by": "test", "date": "2026-11-23", "scope": "opt adapters"},
                "purpose": "test",
                "queries": [
                    {
                        "id": "q1",
                        "query": "structured JSON logs",
                        "category": "obs",
                        "split": "regression",
                        "project_scope": "project:alpha",
                        "answerability": "answerable",
                        "answer_mode": "standard",
                        "expected_atom_ids": ["json-logs.md"],
                        "acceptable_atom_ids": [],
                        "candidate_relevance": ["json-logs.md"],
                        "expected_atom_revisions": [
                            {"atom_id": "json-logs.md", "revision_sha256": hashlib.sha256(atom.read_bytes()).hexdigest()}
                        ],
                        "expected_claims": [{"text": "Structured JSON logs aid debugging.", "supported_by": ["json-logs.md"]}],
                        "expected_behavior": "answer_with_evidence",
                        "trap_reason": None,
                    }
                ],
            }
            ds_path = tpath / "ds.json"
            ds_path.write_text(json.dumps(ds), encoding="utf-8")

            class _StubAdapter(ltb._ExternalMemoryAdapter):
                def __init__(self, name: str, available: bool, hits: list[str] | None = None):
                    self.name = name
                    self.packages = (name,)
                    self._avail = available
                    self._hits = hits or []

                def check_available(self) -> tuple[bool, str]:
                    return (True, "ok") if self._avail else (False, "not installed in test")

                def setup(self, vault_atoms, tmp_path, *, active_llm, base_url, embed_dim) -> float:
                    return 1.5

                def search(self, query: str, *, project_scope: str = "", top_k: int = 5) -> list[str]:
                    return self._hits[:top_k]

            class _FakeFaiss:
                METRIC_INNER_PRODUCT = 0
                class IndexHNSWFlat:
                    def __init__(self, *_a):
                        self.hnsw = type("H", (), {"efConstruction": 64, "efSearch": 32})()
                        self._n = 0
                    def add(self, m):
                        self._n = len(m)
                    def search(self, _q, k):
                        return ([[0.9] * min(k, self._n)], [list(range(min(k, self._n)))])

            class _FakeMem0:
                class Memory:
                    @classmethod
                    def from_config(cls, _c):
                        return cls()
                    def add(self, *_a, **_kw):
                        return None
                    def search(self, *_a, **_kw):
                        return {"results": [{"metadata": {"filename": "json-logs.md"}}]}

            class _FakeBm25:
                class BM25Okapi:
                    def __init__(self, corpus):
                        self._c = corpus
                    def get_scores(self, _q):
                        return [1.0 for _ in self._c]

            adapters = [
                _StubAdapter("basic_memory_live", True, ["json-logs.md"]),
                _StubAdapter("graphiti_kuzu_live", True, ["json-logs.md"]),
                _StubAdapter("zep_live", False),
            ]
            with (
                patch.dict(sys.modules, {"faiss": _FakeFaiss, "mem0": _FakeMem0, "rank_bm25": _FakeBm25}),
                patch.object(ltb, "ATOMS_DIR", str(brain / "2-atoms")),
                patch.object(ltb, "CHROMA_PATH", str(brain / ".chroma")),
                patch.object(ltb, "LEXICAL_INDEX_PATH", str(brain / ".sidecar" / "lexical.sqlite3")),
                patch.object(recall_mod, "ATOMS_DIR", str(brain / "2-atoms")),
                patch.object(recall_mod, "CHROMA_PATH", str(brain / ".chroma")),
                patch.object(recall_mod, "LEXICAL_INDEX_PATH", str(brain / ".sidecar" / "lexical.sqlite3")),
                patch.object(ou, "embed_text", side_effect=lambda *_a, **_kw: [0.1, 0.2, 0.3, 0.4]),
                patch.object(ltb.ollama_utils, "embed_text", side_effect=lambda *_a, **_kw: [0.1, 0.2, 0.3, 0.4]),
                patch.object(
                    ltb,
                    "approved_revision_is_current",
                    lambda p, atoms_dir=str(brain / "2-atoms"): approved_revision_is_current(
                        str(p), atoms_dir=atoms_dir, brain_dir=str(brain)
                    ),
                ),
            ):
                rep = ltb.run_live_third_party_benchmark(
                    dataset_path=ds_path,
                    atoms_dir=brain / "2-atoms",
                    mem0_infer=False,
                    optional_adapters=adapters,
                )
            self.assertIn("basic_memory_live", rep["systems"])
            self.assertIn("graphiti_kuzu_live", rep["systems"])
            self.assertEqual(rep["systems"]["basic_memory_live"]["answerable_expected_recall_at_5_pct"], 100.0)
            self.assertEqual(rep["skipped_systems"]["zep_live"]["status"], "skipped_unavailable")


if __name__ == "__main__":
    unittest.main()
