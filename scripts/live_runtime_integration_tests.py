# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Unmocked Live-Runtime Integration Suite (Suite 30).

Exercises real local dependencies in isolated ``tempfile.TemporaryDirectory()`` vaults
without mocking their outputs:
1. Real local ONNX int8 weights (Cross-Encoder + bidirectional NLI inference).
2. Real ``faster-whisper`` ``tiny.en`` speech transcription from synthesized WAV audio.
3. Real Ollama ``llava:latest`` Vision OCR on generated PNG and image-only PDFs.
4. Real ``PyAV libx264`` H.264 MP4 container encoding and ``av.open()`` frame decode.
5. Real loopback TCP HTTP servers (``127.0.0.1:0``) for SD WebUI & ComfyUI generation.
6. Real Ollama ``nomic-embed-text`` (768-dim Chroma) dense vectors + ``qwen2.5-coder:7b``
   grounded answer validation and out-of-scope trap abstention.

Tests requiring a running Ollama daemon respect ``BRAIN_CI_CODE_ONLY=1`` for
offline/headless CI runs while keeping all non-Ollama runtime checks active.
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


import base64
from contextlib import ExitStack
from dataclasses import replace
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from urllib.parse import parse_qs, urlsplit

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent

from scripts.config import security_state_dir  # noqa: E402

# Capture the host's verified read-only ONNX model location before per-test env isolation.
REAL_SECURITY_DIR = Path(security_state_dir())


def _ci_code_only() -> bool:
    return (os.environ.get("BRAIN_CI_CODE_ONLY") or "").strip() == "1"


def _skip_if_unavailable(test: unittest.TestCase, *, flag: str, available: bool, reason: str) -> None:
    """Skip optional hardware/model tests unless the caller sets the matching BRAIN_REQUIRE_* flag."""
    if available:
        return
    if (os.environ.get(flag) or "").strip() == "1":
        test.fail(f"{reason} ({flag}=1 requires it.)")
    test.skipTest(reason)


def _ollama_reachable() -> bool:
    import socket

    host = (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").strip()
    port = 11434
    if host.startswith("http://") or host.startswith("https://"):
        from urllib.parse import urlsplit

        parsed = urlsplit(host)
        port = parsed.port or (443 if parsed.scheme == "https" else 11434)
        host = parsed.hostname or "127.0.0.1"
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def _seed_approved_atom(
    brain_dir: Path,
    *,
    filename: str,
    claim: str,
    evidence: str,
    contexts: list[str] | None = None,
) -> Path:
    """Create a source-anchored, owner-approved atom inside an isolated temp vault."""
    from scripts.atom_mutator import apply_review_transition
    from scripts.evidence import find_evidence_anchor
    from scripts.lexical_store import rebuild_lexical_index

    raw_dir = brain_dir / "0-raw"
    atoms_dir = brain_dir / "2-atoms"
    sidecar_dir = brain_dir / ".sidecar"
    archive_dir = raw_dir / "archive"
    for d in (raw_dir, atoms_dir, sidecar_dir, archive_dir):
        d.mkdir(parents=True, exist_ok=True)

    src_name = f"src-{filename}"
    (raw_dir / src_name).write_text(f"Primary engineering source: {evidence}\n", encoding="utf-8")

    ctx_items = contexts if contexts is not None else ["project:second_brain"]
    ctx_yaml = "\n".join(f"  - {item}" for item in ctx_items)
    draft = (
        "---\n"
        f"id: {Path(filename).stem}\n"
        "type: atom\n"
        "memory_type: semantic\n"
        "epistemic_label: claim\n"
        "created: 2026-11-23\n"
        "last-reviewed: 2026-11-23\n"
        "confidence: 0.95\n"
        "importance: 0.90\n"
        "sources:\n"
        f"  - 0-raw/{src_name}\n"
        "contexts:\n"
        f"{ctx_yaml}\n"
        "review_status: pending\n"
        "claim_state: candidate\n"
        "citation_integrity: missing\n"
        "---\n\n"
        "## Claim\n"
        f"{claim}\n\n"
        "## Evidence\n"
        f"{evidence}\n\n"
        "## Why It Matters\n"
        "Unmocked live runtime integration fixture.\n"
    )
    anchor = find_evidence_anchor(draft, str(brain_dir))
    parts = draft.split("---", 2)
    fm = parts[1]
    for k, v in anchor.items():
        fm += f"{k}: {v}\n"
    fm = fm.replace("citation_integrity: missing", "citation_integrity: anchored")
    atom_path = atoms_dir / filename
    atom_path.write_text(f"---{fm}---{parts[2]}", encoding="utf-8")

    apply_review_transition(
        action="approve",
        atom_path=str(atom_path),
        expected_sha256=hashlib.sha256(atom_path.read_bytes()).hexdigest(),
        actor="live-runtime-test-owner",
        decision_log_path=str(archive_dir / "decision-record.jsonl"),
        brain_dir=str(brain_dir),
        atoms_dir=str(atoms_dir),
    )
    rebuild_lexical_index(str(atoms_dir), str(sidecar_dir / "lexical.sqlite3"))
    return atom_path


def _synthesize_spoken_wav(out_wav: Path, phrase: str) -> bool:
    """Synthesize a real spoken English PCM WAV file using local OS TTS without network access."""
    if os.name == "nt":
        ps_exe = shutil.which("powershell.exe") or shutil.which("powershell")
        if ps_exe:
            esc_path = str(out_wav).replace("'", "''")
            esc_phrase = phrase.replace("'", "''")
            ps_script = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "$s.Rate = -1; "
                f"$s.SetOutputToWaveFile('{esc_path}'); "
                f"$s.Speak('{esc_phrase}'); "
                "$s.Dispose()"
            )
            proc = subprocess.run(
                [ps_exe, "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=30.0,
                check=False,
            )
            return proc.returncode == 0 and out_wav.is_file() and out_wav.stat().st_size > 4096
    espeak_bin = shutil.which("espeak-ng") or shutil.which("espeak")
    if espeak_bin:
        proc = subprocess.run(
            [espeak_bin, "-w", str(out_wav), phrase],
            capture_output=True,
            text=True,
            timeout=20.0,
            check=False,
        )
        return proc.returncode == 0 and out_wav.is_file() and out_wav.stat().st_size > 4096
    return False


class LiveRuntimeIntegrationTests(unittest.TestCase):
    """Unmocked runtime verification inside isolated temporary vaults."""

    def setUp(self) -> None:
        super().setUp()
        self._stack = ExitStack()
        self.addCleanup(self._stack.close)

        self.temp_root = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self.vault_dir = self.temp_root / "brain"
        self.security_dir = self.temp_root / "security"
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.security_dir.mkdir(parents=True, exist_ok=True)
        self.policy_file = self.security_dir / "host-sharing-policy.json"

        self._stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "BRAIN_VAULT_DIR": str(self.vault_dir),
                    "BRAIN_SECURITY_STATE_DIR": str(self.security_dir),
                    "XDG_STATE_HOME": str(self.temp_root / "xdg_state"),
                    "BRAIN_HOST_POLICY_FILE": str(self.policy_file),
                    "BRAIN_DASHBOARD_AUTH_FILE": str(self.security_dir / "dashboard-auth.json"),
                    "BRAIN_MEMORY_API_TOKEN_FILE": str(self.security_dir / "memory-api-token"),
                    "BRAIN_ALLOW_ENV_POLICY_PATH": "1",
                },
            )
        )

    def _enable_local_ollama_policy(self, *, project_scope: str = "project:second_brain", model: str = "qwen2.5-coder:7b") -> None:
        import scripts.host_adapters as host_adapters

        policies = host_adapters.load_host_policies(self.policy_file)
        cur = policies["ollama_local"]
        policies["ollama_local"] = replace(
            cur,
            enabled=True,
            allowed_projects=(project_scope,),
            allow_unscoped_atoms=True,
            allow_preference_atoms=True,
            default_model=model,
        )
        host_adapters.save_host_policies(policies, policy_path=self.policy_file)

    def test_01_real_onnx_cross_encoder_and_nli_weights(self) -> None:
        import importlib.util

        _skip_if_unavailable(
            self,
            flag="BRAIN_REQUIRE_ONNX",
            available=importlib.util.find_spec("onnxruntime") is not None and importlib.util.find_spec("tokenizers") is not None,
            reason="onnxruntime and tokenizers are not installed. They ship in the optional reranker extra.",
        )
        import scripts.cross_encoder as cross_encoder
        import scripts.knowledge_graph as knowledge_graph
        import scripts.onnx_models as onnx_models

        status = onnx_models.get_onnx_models_status(state_dir=REAL_SECURITY_DIR, run_inference=False)
        _skip_if_unavailable(
            self,
            flag="BRAIN_REQUIRE_ONNX",
            available=bool(status.get("all_verified")),
            reason="Pinned ONNX cross-encoder and NLI weights are not installed. Run `second-brain models pull`.",
        )
        status = onnx_models.get_onnx_models_status(state_dir=REAL_SECURITY_DIR, run_inference=True)
        self.assertTrue(status["onnx_runtime_ready"])
        self.assertTrue(status["all_verified"], f"Local ONNX weights incomplete: {status}")
        self.assertTrue(status["slots"]["cross-encoder"]["verified"])
        self.assertTrue(status["slots"]["nli"]["verified"])

        ce_dir = REAL_SECURITY_DIR / "models" / "cross-encoder"
        nli_dir = REAL_SECURITY_DIR / "models" / "nli"
        cross_encoder.clear_cross_encoder_cache()
        try:
            scores, backend = cross_encoder.score_query_atom_pairs(
                "How does SQLite WAL mode handle concurrent readers?",
                [
                    "SQLite WAL mode lets multiple readers run concurrently without blocking the single writer.",
                    "Bananas ripen quickly when stored next to apples inside a warm kitchen pantry.",
                ],
                onnx_dir=ce_dir,
                allow_onnx=True,
            )
            self.assertEqual(backend, "onnx_cross_encoder")
            self.assertEqual(len(scores), 2)
            self.assertGreater(scores[0], scores[1])

            nli_enc = cross_encoder.get_onnx_cross_encoder(nli_dir, max_length=384)
            self.assertIsNotNone(nli_enc)
            assert nli_enc is not None
            probs = nli_enc.predict_nli_probs(
                "PostgreSQL uses MVCC transactions for isolation.",
                "PostgreSQL never supports any transactions.",
            )
            self.assertIsNotNone(probs)
            assert probs is not None
            self.assertGreater(probs[0], probs[1])
            self.assertGreater(probs[0], probs[2])

            with patch.dict(os.environ, {"BRAIN_NLI_ONNX_DIR": str(nli_dir)}):
                cross_encoder.clear_cross_encoder_cache()
                verdict = knowledge_graph._local_fast_nli_verdict(
                    "SQLite WAL mode allows concurrent readers alongside writers.",
                    "SQLite WAL mode forbids all concurrent readers entirely.",
                )
            self.assertEqual(verdict["verdict"], "contradiction")
        finally:
            cross_encoder.clear_cross_encoder_cache()

    def test_02_real_faster_whisper_tiny_en_transcription(self) -> None:
        import importlib.util

        _skip_if_unavailable(
            self,
            flag="BRAIN_REQUIRE_WHISPER",
            available=importlib.util.find_spec("faster_whisper") is not None and not _ci_code_only(),
            reason="faster-whisper tiny.en is an optional multimodal extra and is skipped in code-only CI.",
        )
        import scripts.ingest_multimodal as ingest_multimodal

        wav_path = self.temp_root / "voice_probe.wav"
        if not _synthesize_spoken_wav(wav_path, "Second brain verification passes."):
            self.skipTest("No local OS offline TTS synthesizer available to generate spoken test WAV.")

        prev_ctx = (
            getattr(ingest_multimodal._THREAD_VAULT_CONTEXT, "raw_dir", None),
            getattr(ingest_multimodal._THREAD_VAULT_CONTEXT, "inbox_dir", None),
            getattr(ingest_multimodal._THREAD_VAULT_CONTEXT, "sidecar_dir", None),
        )
        prev_whisper_size = ingest_multimodal.WHISPER_SIZE
        ingest_multimodal._THREAD_VAULT_CONTEXT.raw_dir = str(self.vault_dir / "0-raw")
        ingest_multimodal._THREAD_VAULT_CONTEXT.inbox_dir = str(self.vault_dir / "0-raw" / "inbox")
        ingest_multimodal._THREAD_VAULT_CONTEXT.sidecar_dir = str(self.vault_dir / ".sidecar")
        ingest_multimodal.WHISPER_SIZE = "tiny.en"
        try:
            capture_name = ingest_multimodal.process_audio(str(wav_path))
        finally:
            ingest_multimodal.WHISPER_SIZE = prev_whisper_size
            (
                ingest_multimodal._THREAD_VAULT_CONTEXT.raw_dir,
                ingest_multimodal._THREAD_VAULT_CONTEXT.inbox_dir,
                ingest_multimodal._THREAD_VAULT_CONTEXT.sidecar_dir,
            ) = prev_ctx

        self.assertIsNotNone(capture_name)
        assert capture_name is not None
        capture_file = self.vault_dir / "0-raw" / capture_name
        self.assertTrue(capture_file.is_file())
        capture_text = capture_file.read_text(encoding="utf-8")
        self.assertIn("faster-whisper tiny.en", capture_text)
        self.assertIn("<!-- second_brain_origin: user_local -->", capture_text)
        lowered = capture_text.lower()
        self.assertTrue(
            any(tok in lowered for tok in ("second", "brain", "verification", "pass")),
            f"Unexpected Whisper tiny.en transcript output:\n{capture_text}",
        )
        archived_files = list((self.vault_dir / "0-raw" / "archive").iterdir())
        self.assertGreaterEqual(len(archived_files), 1)

    def test_03_real_ollama_llava_vision_ocr_image_and_pdf(self) -> None:
        _skip_if_unavailable(
            self,
            flag="BRAIN_REQUIRE_OLLAMA",
            available=_ollama_reachable() and not _ci_code_only(),
            reason="Ollama is not reachable on the loopback host. Start Ollama to run live vision OCR.",
        )

        from PIL import Image, ImageDraw, ImageFont
        import ollama
        import scripts.doc_extractors as doc_extractors
        import scripts.ingest_multimodal as ingest_multimodal

        self._enable_local_ollama_policy()

        img = Image.new("RGB", (384, 144), color=(255, 255, 255))
        draw = ImageDraw.Draw(img)
        font = ImageFont.load_default(size=36)
        draw.text((20, 20), "SECOND BRAIN", fill=(0, 0, 0), font=font)
        draw.text((20, 75), "OCR 42", fill=(0, 0, 0), font=font)

        png_path = self.temp_root / "vision_ocr_card.png"
        pdf_path = self.temp_root / "vision_ocr_scan.pdf"
        img.save(png_path, format="PNG")
        img.save(pdf_path, "PDF", resolution=150.0)

        try:
            desc, model_name, model_digest = ingest_multimodal.describe_image_with_provenance(str(png_path))
            self.assertTrue(model_name)
            self.assertTrue(model_digest)
            self.assertTrue(desc.strip())
            self.assertTrue(
                any(tok in desc.lower() for tok in ("second", "brain", "ocr", "42")),
                f"Vision description missed banner tokens: {desc!r}",
            )

            # Force Vision OCR fallback on the embedded-image PDF (bypass external tesseract if absent/present)
            with patch.dict(os.environ, {"BRAIN_TESSERACT_CMD": str(self.temp_root / "nonexistent-tesseract")}):
                doc_res = doc_extractors.extract_pdf(str(pdf_path), enable_ocr=True, allow_vision_ocr=True)
            self.assertEqual(doc_res.extractor, "pypdf-layout+ocr")
            self.assertIn("ollama-vision:", doc_res.model)
            self.assertTrue(
                any(tok in doc_res.markdown.lower() for tok in ("second", "brain", "ocr", "42")),
                f"PDF Vision OCR output missed banner tokens: {doc_res.markdown!r}",
            )
        finally:
            try:
                ollama.Client(host="http://127.0.0.1:11434", timeout=10.0).chat(
                    model="llava:latest",
                    messages=[],
                    keep_alive=0,
                )
            except Exception:
                pass

    def test_04_real_pyav_libx264_mp4_encode_and_decode(self) -> None:
        import importlib.util

        _skip_if_unavailable(
            self,
            flag="BRAIN_REQUIRE_PYAV",
            available=importlib.util.find_spec("av") is not None,
            reason="PyAV is not installed. It ships in the optional multimodal extra.",
        )
        import av
        import scripts.creative_media as creative_media

        art = creative_media.generate_local_video(
            "Local encrypted knowledge graph pipeline telemetry",
            brain_dir=self.vault_dir,
            backend="builtin_procedural_local",
            output_format="mp4",
            width=320,
            height=192,
            frames=6,
            fps=6,
            allow_procedural_fallback=True,
            allow_hardware_override=True,
        )
        self.assertEqual(art.encoder_used, "pyav_libx264")
        mp4_path = Path(art.file_path)
        self.assertTrue(mp4_path.is_file())
        info = creative_media.inspect_mp4_container(mp4_path.read_bytes())
        self.assertEqual(info["codec_tag"], "avc1")
        self.assertTrue(info["has_avcC"])
        self.assertTrue(info["browser_playable"])

        with av.open(str(mp4_path)) as container:
            self.assertGreaterEqual(len(container.streams.video), 1)
            self.assertEqual(container.streams.video[0].codec_context.name, "h264")
            decoded_frames = sum(1 for _ in container.decode(video=0))
        self.assertGreaterEqual(decoded_frames, 6)

    def test_05_real_loopback_tcp_sd_webui_and_comfyui_daemons(self) -> None:
        import importlib.util

        _skip_if_unavailable(
            self,
            flag="BRAIN_REQUIRE_PILLOW",
            available=importlib.util.find_spec("PIL") is not None,
            reason="Pillow is not installed. It ships in the optional multimodal extra.",
        )
        from PIL import Image
        import scripts.creative_media as creative_media

        png_buf = io.BytesIO()
        Image.new("RGB", (64, 64), color=(32, 128, 192)).save(png_buf, format="PNG")
        sample_png = png_buf.getvalue()
        sample_b64 = base64.b64encode(sample_png).decode("ascii")
        requests_seen: list[tuple[str, str]] = []
        seen_lock = threading.Lock()

        class _LoopbackDaemonHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def _send_json(self, code: int, payload: object) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                with seen_lock:
                    requests_seen.append(("GET", parsed.path))
                if parsed.path == "/sdapi/v1/sd-models":
                    self._send_json(200, [{"model_name": "sdxl-local.safetensors", "title": "sdxl-local"}])
                elif parsed.path == "/system_stats":
                    self._send_json(200, {"devices": [{"name": "cpu"}]})
                elif parsed.path == "/object_info/CheckpointLoaderSimple":
                    self._send_json(
                        200,
                        {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["v1-5-pruned-emaonly.safetensors"]]}}}},
                    )
                elif parsed.path == "/history/live-tcp-job-01":
                    self._send_json(
                        200,
                        {
                            "live-tcp-job-01": {
                                "outputs": {
                                    "7": {
                                        "images": [
                                            {"filename": "local_tcp_out.png", "subfolder": "", "type": "output"}
                                        ]
                                    }
                                }
                            }
                        },
                    )
                elif parsed.path == "/view":
                    qs = parse_qs(parsed.query)
                    if qs.get("filename") == ["local_tcp_out.png"]:
                        self.send_response(200)
                        self.send_header("Content-Type", "image/png")
                        self.send_header("Content-Length", str(len(sample_png)))
                        self.end_headers()
                        self.wfile.write(sample_png)
                    else:
                        self._send_json(404, {"error": "not found"})
                else:
                    self._send_json(404, {"error": f"unhandled GET {parsed.path}"})

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                _ = self.rfile.read(length) if length > 0 else b""
                with seen_lock:
                    requests_seen.append(("POST", parsed.path))
                if parsed.path == "/sdapi/v1/txt2img":
                    self._send_json(200, {"images": [sample_b64]})
                elif parsed.path == "/prompt":
                    self._send_json(200, {"prompt_id": "live-tcp-job-01"})
                else:
                    self._send_json(404, {"error": f"unhandled POST {parsed.path}"})

        server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackDaemonHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            base_url = f"http://127.0.0.1:{port}"

            sd_art = creative_media.generate_local_image(
                "Deterministic local vault diagram",
                brain_dir=self.vault_dir,
                backend="sd_webui_local",
                sd_webui_url=base_url,
                http_transport=None,
                allow_hardware_override=True,
                width=256,
                height=256,
            )
            self.assertEqual(sd_art.backend_used, "sd_webui_local")
            self.assertTrue(Path(sd_art.file_path).is_file())
            self.assertTrue(Path(sd_art.file_path).read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

            comfy_art = creative_media.generate_local_image(
                "Deterministic ComfyUI local workflow card",
                brain_dir=self.vault_dir,
                backend="comfyui_local",
                comfyui_url=base_url,
                http_transport=None,
                allow_hardware_override=True,
                width=256,
                height=256,
                comfy_poll_interval=0.05,
                comfy_max_polls=20,
            )
            self.assertEqual(comfy_art.backend_used, "comfyui_local")
            self.assertTrue(Path(comfy_art.file_path).is_file())
            self.assertTrue(Path(comfy_art.file_path).read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5.0)

        self.assertIn(("POST", "/sdapi/v1/txt2img"), requests_seen)
        self.assertIn(("POST", "/prompt"), requests_seen)
        self.assertIn(("GET", "/history/live-tcp-job-01"), requests_seen)
        self.assertIn(("GET", "/view"), requests_seen)

    def test_06_real_ollama_dense_vectors_and_qwen_grounded_answer(self) -> None:
        _skip_if_unavailable(
            self,
            flag="BRAIN_REQUIRE_OLLAMA",
            available=_ollama_reachable() and not _ci_code_only(),
            reason="Ollama is not reachable on the loopback host. Start Ollama to run live embeddings and grounded answers.",
        )

        import scripts.config as config
        import scripts.host_adapters as host_adapters
        import scripts.index_manifest as index_manifest
        import scripts.recall as recall
        import scripts.rebuild_vectors as rebuild_vectors

        _seed_approved_atom(
            self.vault_dir,
            filename="vault-portability.md",
            claim="Second Brain stores durable canonical knowledge as plain Markdown files inside the local vault so users keep full ownership without proprietary lock-in.",
            evidence="Second Brain stores durable canonical knowledge as plain Markdown files inside the local vault so users keep full ownership without proprietary lock-in.",
            contexts=["project:second_brain"],
        )
        self._enable_local_ollama_policy(project_scope="project:second_brain", model="qwen2.5-coder:7b")

        atoms_dir = str(self.vault_dir / "2-atoms")
        index_path = str(self.vault_dir / ".sidecar" / "lexical.sqlite3")
        chroma_path = str(self.vault_dir / ".chroma")

        vec_res = rebuild_vectors.rebuild_vectors(
            atoms_dir=atoms_dir,
            store_path=chroma_path,
            collection_name="atoms",
            model=config.EMBED_MODEL,
            require_valid_memory=True,
        )
        self.assertTrue(vec_res.get("committed"), f"Vector rebuild failed: {vec_res}")
        self.assertEqual(vec_res.get("indexed"), 1)
        manifest = index_manifest.load_index_manifest(chroma_path)
        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertEqual(manifest.get("embedding_dimensions"), 768)

        dense_hits, _ = recall.search_memory(
            "How does Second Brain store durable canonical knowledge?",
            options=recall.RecallOptions(
                n_results=5,
                projects=("project:second_brain",),
                retrieval_mode="dense",
            ),
            atoms_dir=atoms_dir,
            index_path=index_path,
            chroma_path=chroma_path,
        )
        self.assertTrue(
            any(
                h.filename == "vault-portability.md"
                and h.source == "vector"
                and "retrieval:vector" in (h.match_reasons or ())
                for h in dense_hits
            ),
            f"Dense search did not return vector-matched vault-portability.md: {dense_hits}",
        )

        ans_res = host_adapters.generate_grounded_answer_via_host(
            host_adapters.AnswerRequest(
                query="How does Second Brain store durable canonical knowledge?",
                mode="standard",
                project_scope="project:second_brain",
                result_limit=5,
            ),
            dense_hits,
            host_id="ollama_local",
            model_override="qwen2.5-coder:7b",
            atoms_dir=atoms_dir,
            brain_dir=str(self.vault_dir),
            policy_path=self.policy_file,
            security_dir=self.security_dir,
            http_transport=None,
        )
        self.assertTrue(ans_res.answerable, f"Grounded Qwen answer unexpectedly abstained: {ans_res}")
        self.assertGreaterEqual(len(ans_res.claims), 1)
        self.assertTrue(
            all(any(ev.atom_id == "vault-portability.md" for ev in c.evidence) for c in ans_res.claims),
            f"Claims did not cite vault-portability.md: {ans_res.claims}",
        )

        trap_res = host_adapters.generate_grounded_answer_via_host(
            host_adapters.AnswerRequest(
                query="What is the exact quantum annealing qubit count of the Mars colony reactor?",
                mode="standard",
                project_scope="project:second_brain",
                result_limit=5,
            ),
            dense_hits,
            host_id="ollama_local",
            model_override="qwen2.5-coder:7b",
            atoms_dir=atoms_dir,
            brain_dir=str(self.vault_dir),
            policy_path=self.policy_file,
            security_dir=self.security_dir,
            http_transport=None,
        )
        self.assertFalse(trap_res.answerable, f"Out-of-scope trap query must abstain: {trap_res}")
        self.assertEqual(trap_res.claims, ())


if __name__ == "__main__":
    unittest.main()
