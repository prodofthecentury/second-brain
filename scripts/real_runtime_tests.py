# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Real executions of the advanced runtimes.

A test skips only when the tool is absent and ``BRAIN_REQUIRE_<NAME>`` is not
``1``. The advanced CI workflow sets those flags after it installs the tool.
Missing tools raise a clear error from the module. They do not return a fake
artifact.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts.cognitive_swarm import SwarmRuntimeError, enforce_cited_sentences, run_ollama_cited_debate
from scripts.experimental_lora_pipeline import LoraDependencyError, lora_training_available, train_lora_adapter
from scripts.formal_proof_search import verify_lean_binary_available, verify_lean_proof
from scripts.jarvis_orchestrator import export_open_webui_tool, trigger_n8n_workflow
from scripts.screen_memory import ScreenCaptureError, capture_screen_png, recognize_image_text
from scripts.tts_engine import TtsError, synthesize_wav_bytes
from scripts.video_extractor import extract_video_keyframes_and_audio, render_sample_clip, resolve_ffmpeg_tools
from scripts.zero_trust_tunnel import probe_tunnel_binary


def _require(flag: str) -> bool:
    return os.environ.get(flag) == "1"


class RealRuntimeTests(unittest.TestCase):
    def test_citation_enforcement_drops_uncited_sentences(self) -> None:
        kept = enforce_cited_sentences("Supported [V1]. Invented without a source.", {"V1"})
        self.assertEqual(kept, ["Supported [V1]."])

    def test_n8n_webhook_posts_real_http_to_loopback(self) -> None:
        seen: dict[str, object] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length)
                seen["body"] = json.loads(raw.decode("utf-8"))
                payload = b'{"executionId":"local-1"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt: str, *args: object) -> None:
                return None

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = trigger_n8n_workflow(
                f"http://127.0.0.1:{port}/webhook/second-brain",
                {"event": "atom.approved", "atom": "note.md"},
            )
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(seen["body"]["event"], "atom.approved")
        tool = export_open_webui_tool(api_port=8765)
        compile(tool["python_tool_source"], "<open-webui-tool>", "exec")
        self.assertIn("127.0.0.1:8765", tool["python_tool_source"])

    def test_ffmpeg_renders_a_clip_and_extracts_a_png_frame(self) -> None:
        if not resolve_ffmpeg_tools()[0]:
            if _require("BRAIN_REQUIRE_FFMPEG"):
                self.fail("ffmpeg is required but not on PATH")
            self.skipTest("ffmpeg is not on PATH")
        with tempfile.TemporaryDirectory(prefix="sb-vid-") as temp:
            clip = render_sample_clip(Path(temp) / "sample.mp4", duration_seconds=0.6)
            extracted = extract_video_keyframes_and_audio(str(clip), str(Path(temp) / "out"), max_keyframes=2)
            frames = extracted["keyframes"]
            self.assertTrue(frames, extracted)
            raw = Path(str(frames[0]["path"])).read_bytes()
            self.assertTrue(raw.startswith(b"\x89PNG"))

    def test_espeak_writes_a_riff_wav(self) -> None:
        import shutil

        if not (shutil.which("espeak-ng") or shutil.which("espeak")):
            if _require("BRAIN_REQUIRE_TTS"):
                self.fail("espeak-ng is required but not on PATH, and Kokoro weights are not assumed")
            self.skipTest("espeak-ng is not on PATH")
        spoken = synthesize_wav_bytes("Hello from second brain.")
        self.assertTrue(spoken["wav_bytes"].startswith(b"RIFF"), spoken["model_id"])
        self.assertGreater(spoken["duration_seconds"], 0.0)
        self.assertNotIn("injected", spoken["model_id"])

    def test_screen_capture_writes_a_png(self) -> None:
        try:
            shot = capture_screen_png()
        except ScreenCaptureError as exc:
            if _require("BRAIN_REQUIRE_SCREEN"):
                self.fail(str(exc))
            self.skipTest(str(exc))
        self.assertTrue(shot["png_bytes"].startswith(b"\x89PNG"))
        self.assertGreater(shot["byte_count"], 32)

    def test_windows_ocr_reads_rendered_text(self) -> None:
        if sys.platform != "win32":
            if _require("BRAIN_REQUIRE_WINOCR"):
                self.fail("Windows OCR was required, but this runner is not Windows")
            self.skipTest("Windows OCR runs on win32")
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (640, 180), "white")
        draw = ImageDraw.Draw(image)
        draw.text((40, 60), "HELLO", fill="black")
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        result = recognize_image_text(buf.getvalue(), timeout_seconds=30)
        self.assertFalse(result["error"], result)
        self.assertIn("hello", result["text"].lower())
        self.assertEqual(result["engine"], "windows-winrt-ocr")

    def test_lora_training_loop_or_a_clear_missing_dependency_error(self) -> None:
        info = lora_training_available()
        with tempfile.TemporaryDirectory(prefix="sb-lora-") as temp:
            if not info["available"]:
                if _require("BRAIN_REQUIRE_LORA"):
                    self.fail(info["detail"])
                with self.assertRaises(LoraDependencyError) as caught:
                    train_lora_adapter(out_dir=Path(temp) / "adapter", steps=1)
                self.assertIn("peft", str(caught.exception))
                return
            trained = train_lora_adapter(
                out_dir=Path(temp) / "adapter",
                texts=["cite the approved atom"],
                steps=2,
                timeout_seconds=90,
            )
            self.assertEqual(trained["status"], "trained")
            self.assertEqual(trained["steps"], 2)
            self.assertGreater(trained["trainable_parameters"], 0)
            self.assertEqual(trained["loaded_logits_vocab"], 128)
            self.assertTrue(Path(trained["adapter_path"]).is_file())

    def test_lean_checks_a_real_file_when_the_toolchain_is_installed(self) -> None:
        if not verify_lean_binary_available():
            if _require("BRAIN_REQUIRE_LEAN"):
                self.fail("lean is required but not on PATH")
            self.skipTest("lean is not on PATH")
        result = verify_lean_proof("example : 2 = 2 := rfl\n", timeout_seconds=40)
        self.assertTrue(result["verified"], result)

    def test_ollama_debate_uses_a_live_model_and_drops_uncited_claims(self) -> None:
        citations = [{"citation_id": "V1", "claim": "Encrypted backups cover durable Markdown."}]
        try:
            result = run_ollama_cited_debate(
                "Do encrypted backups cover durable Markdown?",
                citations,
                timeout_seconds=90,
            )
        except SwarmRuntimeError as exc:
            if _require("BRAIN_REQUIRE_OLLAMA"):
                self.fail(str(exc))
            self.skipTest(str(exc))
        self.assertEqual(result["uncited_claims_emitted"], 0)
        self.assertIn(result["status"], {"completed", "abstained"})
        self.assertTrue(result["rounds"])

    def test_llama32_distills_eight_short_notes(self) -> None:
        """Live llama3.2:3b distill. Skips unless Ollama has that model.

        The advanced workflow sets BRAIN_REQUIRE_DISTILL=1 after pulling the model.
        A total miss (0/8) fails that job. Partial success is printed and kept.
        """
        import urllib.error
        import urllib.request

        from scripts.ollama_refinery import draft_memory_atoms

        model = "llama3.2:3b"
        notes = (
            "Pour-over coffee uses 1 g of coffee per 16 g of water.",
            "The vault stores approved claims as Markdown atoms.",
            "House rules require evidence copied from the source note.",
            "A pinned BRAIN_GEN_MODEL must not switch after an abort.",
            "Lexical search can rerank with the local cross-encoder.",
            "SQLite is the job queue for background capture.",
            "The owner token header is X-Owner-Token.",
            "Daily notes land in the 0-raw inbox before distillation.",
        )
        try:
            with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            if _require("BRAIN_REQUIRE_OLLAMA") or _require("BRAIN_REQUIRE_DISTILL"):
                self.fail(f"Ollama is required but not reachable: {exc}")
            self.skipTest(f"Ollama is not reachable: {exc}")
        installed = [str(item.get("name") or "") for item in payload.get("models") or []]
        if not any(name == model or name.startswith(model + ":") for name in installed):
            if _require("BRAIN_REQUIRE_DISTILL"):
                self.fail(f"{model} is required but not installed; tags={installed}")
            self.skipTest(f"{model} is not installed")
        successes = 0
        previous = os.environ.get("BRAIN_GEN_MODEL")
        os.environ["BRAIN_GEN_MODEL"] = model
        try:
            for index, note in enumerate(notes, start=1):
                try:
                    drafted = draft_memory_atoms(note, f"0-raw/note-{index}.md", "Copy evidence verbatim.")
                except Exception as exc:
                    print(f"DISTILL_NOTE {index} FAIL {exc}")
                    continue
                if drafted and note in drafted[0]:
                    successes += 1
                    print(f"DISTILL_NOTE {index} OK")
                else:
                    print(f"DISTILL_NOTE {index} EMPTY")
        finally:
            if previous is None:
                os.environ.pop("BRAIN_GEN_MODEL", None)
            else:
                os.environ["BRAIN_GEN_MODEL"] = previous
        print(f"DISTILL_SUCCESS {successes}/{len(notes)}")
        if _require("BRAIN_REQUIRE_DISTILL") and successes == 0:
            self.fail(f"llama3.2:3b distilled 0/{len(notes)} notes")

    def test_tunnel_binaries_report_their_real_version_when_installed(self) -> None:
        for name, flag in (("cloudflared", "BRAIN_REQUIRE_CLOUDFLARED"), ("tailscale", "BRAIN_REQUIRE_TAILSCALE")):
            probed = probe_tunnel_binary(name)
            if not probed["available"]:
                if _require(flag):
                    self.fail(probed["error"])
                continue
            self.assertTrue(probed["version"], probed)
            self.assertFalse(probed["error"])


if __name__ == "__main__":
    raise SystemExit(unittest.main())
