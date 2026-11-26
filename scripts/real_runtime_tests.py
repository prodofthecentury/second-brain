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
        """Live small-model distill of the shared fixture notes.

        The model name comes from the caller. llama3.2:3b is the CI stress
        model. A Hermes run uses the same notes and the same guards.
        """
        self._distill_shared_fixtures(
            model=os.environ.get("BRAIN_DISTILL_FIXTURE_MODEL") or "llama3.2:3b",
            require_flag="BRAIN_REQUIRE_DISTILL",
        )

    def test_hermes_distills_the_same_fixtures(self) -> None:
        self._distill_shared_fixtures(
            model=os.environ.get("BRAIN_HERMES_DISTILL_MODEL") or "hermes3:3b",
            require_flag="BRAIN_REQUIRE_HERMES",
        )

    def _distill_shared_fixtures(self, *, model: str, require_flag: str) -> None:
        import urllib.error
        import urllib.request

        from scripts.evidence import find_evidence_anchor
        from scripts.lexical_store import rebuild_lexical_index
        from scripts.ollama_refinery import draft_memory_atoms
        from scripts.personal_eval import evaluate_file

        fixture_path = Path(__file__).resolve().parents[1] / "docs" / "distill-fixtures.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        notes = fixture["notes"]
        try:
            with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            if _require("BRAIN_REQUIRE_OLLAMA") or _require(require_flag):
                self.fail(f"Ollama is required but not reachable: {exc}")
            self.skipTest(f"Ollama is not reachable: {exc}")
        installed = [str(item.get("name") or "") for item in payload.get("models") or []]
        if not any(name == model or name.startswith(model + ":") for name in installed):
            if _require(require_flag):
                self.fail(f"{model} is required but not installed; tags={installed}")
            self.skipTest(f"{model} is not installed")

        previous = os.environ.get("BRAIN_GEN_MODEL")
        os.environ["BRAIN_GEN_MODEL"] = model
        notes_with_atoms = 0
        atoms_total = 0
        gate_pass = 0
        contaminated = False
        try:
            with tempfile.TemporaryDirectory(prefix="sb-distill-") as tmp:
                vault = Path(tmp) / "brain"
                raw = vault / "0-raw"
                atoms = vault / "2-atoms"
                raw.mkdir(parents=True)
                atoms.mkdir()
                for note in notes:
                    (vault / note["filename"]).parent.mkdir(parents=True, exist_ok=True)
                    (vault / note["filename"]).write_text(note["text"] + "\n", encoding="utf-8")
                    try:
                        drafted = draft_memory_atoms(note["text"], note["filename"], "Copy evidence verbatim.")
                    except Exception as exc:
                        print(f"DISTILL_NOTE {note['id']} atoms=0 path=deferred verbatim=0 gate=0 detail={exc}")
                        continue
                    verbatim = 0
                    anchored = 0
                    for document in drafted:
                        if note["id"] == "06-no-source" and "coffee" in document.casefold():
                            contaminated = True
                        evidence = ""
                        for block in document.split("## Evidence\n", 1)[1:]:
                            evidence = block.split("\n## ", 1)[0].strip()
                            break
                        if evidence and evidence in note["text"]:
                            verbatim += 1
                        atom_path = atoms / f"{note['id']}-{anchored}.md"
                        atom_path.write_text(document, encoding="utf-8")
                        try:
                            find_evidence_anchor(document, str(vault))
                            anchored += 1
                        except Exception:
                            pass
                    if drafted:
                        notes_with_atoms += 1
                        atoms_total += len(drafted)
                    gate_pass += anchored
                    print(
                        f"DISTILL_NOTE {note['id']} atoms={len(drafted)} "
                        f"verbatim={verbatim} gate={anchored}"
                    )
                print(f"DISTILL_SUCCESS {notes_with_atoms}/{len(notes)} model={model} atoms={atoms_total} gate={gate_pass}")
                if contaminated:
                    self.fail(f"{model} kept a coffee atom for the coffee-free note")
                if any(atoms.glob("*.md")):
                    index = vault / "lexical.sqlite3"
                    rebuild_lexical_index(str(atoms), str(index))
                    spec = {
                        "name": fixture["name"],
                        "k": fixture["eval"]["k"],
                        "allow_unreviewed_exploratory": True,
                        "queries": fixture["eval"]["queries"],
                    }
                    spec_path = vault / "eval.json"
                    spec_path.write_text(json.dumps(spec), encoding="utf-8")
                    report = evaluate_file(str(spec_path), atoms_dir=str(atoms), index_path=str(index))
                    print(f"EVAL_RECALL {report['recall_at_k']} EVAL_MRR {report['mrr']} model={model}")
                else:
                    print(f"EVAL_RECALL 0.0 EVAL_MRR 0.0 model={model}")
        finally:
            if previous is None:
                os.environ.pop("BRAIN_GEN_MODEL", None)
            else:
                os.environ["BRAIN_GEN_MODEL"] = previous
        if _require(require_flag) and notes_with_atoms == 0:
            self.fail(f"{model} distilled 0/{len(notes)} notes")

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
