# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = [
#     "faster-whisper",
#     "ollama",
#     "chromadb",
#     "Pillow",
# ]
# ///
"""
Feature 1 — Multimodal Ingestion
Handles audio files (voice memos) and image files dropped into 0-raw/.

  Audio (.mp3, .wav, .m4a, .ogg, .flac):
    → Transcribed locally using faster-whisper (runs 100% offline, no API)
    → Transcript saved as .md in 0-raw/ for the Refinery to process

  Images (.png, .jpg, .jpeg, .gif, .webp):
    → Described by local LLaVA vision model via Ollama
    → Description + key extracted text saved as .md in 0-raw/

Usage (called by watchdog_daemon.py or run manually):
    uv run scripts/ingest_multimodal.py
    uv run scripts/ingest_multimodal.py --file "0-raw/voice-memo.mp3"
"""

import argparse
import base64
import json
import os
import shutil
import sys
import urllib.error
import urllib.request

import ollama

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (
    AUDIO_EXTS, IMAGE_EXTS, RAW_DIR, BRAIN_DIR, SIDECAR_DIR,
    VISION_MODEL, WHISPER_SIZE, archive_dir, today, now_slug, ensure_dirs, file_sha256
)
from review_atom import review_lock


def unique_archive_path(filename: str) -> str:
    destination_dir = archive_dir()
    stem, extension = os.path.splitext(filename)
    candidate = os.path.join(destination_dir, filename)
    suffix = 2
    while os.path.exists(candidate):
        candidate = os.path.join(destination_dir, f"{stem}-{suffix}{extension}")
        suffix += 1
    return candidate


def save_capture_and_archive(filepath: str, output_name: str, content: str, expected_source_sha256: str) -> str:
    """Commit one generated raw capture and its source archive without clobbering files."""
    with review_lock(os.path.join(SIDECAR_DIR, "writer.lock")):
        if not os.path.isfile(filepath) or file_sha256(filepath) != expected_source_sha256:
            raise RuntimeError("The source capture changed or was already processed during multimodal extraction.")
        stem, extension = os.path.splitext(output_name)
        candidate = output_name
        suffix = 2
        while True:
            out_path = os.path.join(RAW_DIR, candidate)
            try:
                with open(out_path, "x", encoding="utf-8") as file:
                    file.write(content)
                break
            except FileExistsError:
                candidate = f"{stem}-{suffix}{extension}"
                suffix += 1
        shutil.move(filepath, unique_archive_path(os.path.basename(filepath)))
        return candidate

# ─── Audio Transcription ──────────────────────────────────────────────────────

def transcribe_audio(filepath: str) -> str:
    """Transcribe audio file using faster-whisper (fully local)."""
    print(f"  [Whisper] Transcribing: {os.path.basename(filepath)}")
    from faster_whisper import WhisperModel

    # Load model (downloads once, then cached)
    model = WhisperModel(WHISPER_SIZE, device="cpu", compute_type="int8")
    segments, info = model.transcribe(filepath, beam_size=5)

    print(f"  [Whisper] Detected language: {info.language} "
          f"(probability: {info.language_probability:.0%})")

    transcript = " ".join(seg.text.strip() for seg in segments)
    return transcript.strip()


def process_audio(filepath: str):
    filename = os.path.basename(filepath)
    stem     = os.path.splitext(filename)[0]
    source_sha256 = file_sha256(filepath)

    transcript = transcribe_audio(filepath)
    if not transcript:
        print(f"  [!] Empty transcript for {filename}. Skipping.")
        return

    # Save transcript as a raw capture
    out_name = f"{now_slug()}-voice-{stem}.md"

    content = f"""<!-- Auto-transcribed from: {filename} — {today()} -->
<!-- Source: voice memo -->

{transcript}
"""
    out_name = save_capture_and_archive(filepath, out_name, content, source_sha256)

    print(f"  [✓] Transcript saved → {out_name}")
    print(f"  [✓] Original archived.")


# ─── Image Understanding ─────────────────────────────────────────────────────

def ollama_base_url() -> str:
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    return host

def available_ollama_models() -> list[dict]:
    """Return local Ollama model metadata, tolerating older client shapes."""
    try:
        with urllib.request.urlopen(f"{ollama_base_url()}/api/tags", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("models"), list):
                return payload["models"]
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        pass

    try:
        response = ollama.list()
    except Exception:
        return []

    models = response.get("models", response) if isinstance(response, dict) else response
    normalized = []
    for model in models or []:
        if isinstance(model, dict):
            normalized.append(model)
        else:
            normalized.append({
                "name": getattr(model, "model", None) or getattr(model, "name", ""),
                "model": getattr(model, "model", None) or getattr(model, "name", ""),
                "capabilities": getattr(model, "capabilities", []),
            })
    return normalized


def select_vision_model() -> str:
    """Use the configured vision model, or fall back to any installed vision model."""
    models = available_ollama_models()
    names = {
        (m.get("name") or m.get("model") or "").split(":")[0]: m
        for m in models
        if m.get("name") or m.get("model")
    }
    exact_names = {m.get("name") or m.get("model") for m in models}
    configured_base = VISION_MODEL.split(":")[0]

    if VISION_MODEL in exact_names or configured_base in names:
        return VISION_MODEL

    for model in models:
        capabilities = model.get("capabilities") or []
        if "vision" in capabilities:
            return model.get("name") or model.get("model")

    raise RuntimeError(
        "No vision-capable Ollama model is installed. "
        f"Pull {VISION_MODEL!r} or set BRAIN_VISION_MODEL to an installed vision model."
    )

def describe_image(filepath: str) -> str:
    """Use LLaVA via Ollama to extract knowledge from an image."""
    model_name = select_vision_model()
    print(f"  [Vision] Analysing image with {model_name}: {os.path.basename(filepath)}")

    with open(filepath, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")

    prompt = """You are extracting knowledge from an image for a personal knowledge base.
Describe what you see in detail. If there is text in the image, transcribe it exactly.
If there is a diagram, chart, or whiteboard, explain what it shows.
Focus on information and ideas — not aesthetics. Be thorough.
Output plain text only."""

    response = ollama.chat(
        model=model_name,
        messages=[{
            "role":    "user",
            "content": prompt,
            "images":  [image_data],
        }]
    )
    return response["message"]["content"].strip()


def process_image(filepath: str):
    filename = os.path.basename(filepath)
    stem     = os.path.splitext(filename)[0]
    source_sha256 = file_sha256(filepath)

    description = describe_image(filepath)
    if not description:
        print(f"  [!] No description returned for {filename}. Skipping.")
        return

    out_name = f"{now_slug()}-image-{stem}.md"

    content = f"""<!-- Auto-described from image: {filename} — {today()} -->
<!-- Source: image capture via LLaVA -->

{description}
"""
    out_name = save_capture_and_archive(filepath, out_name, content, source_sha256)

    print(f"  [✓] Image description saved → {out_name}")
    print(f"  [✓] Original archived.")


# ─── Entry Point ─────────────────────────────────────────────────────────────

def process_file(filepath: str):
    ext = os.path.splitext(filepath)[1].lower()
    if ext in AUDIO_EXTS:
        process_audio(filepath)
    elif ext in IMAGE_EXTS:
        process_image(filepath)
    else:
        print(f"  [skip] Unsupported type: {ext}")


def scan_raw_dir():
    """Find and process all unhandled audio/image files in 0-raw/."""
    ensure_dirs()
    files = [
        f for f in os.listdir(RAW_DIR)
        if os.path.splitext(f)[1].lower() in (AUDIO_EXTS | IMAGE_EXTS)
    ]
    if not files:
        print("[*] No audio or image files found in 0-raw/.")
        return
    for filename in files:
        filepath = os.path.join(RAW_DIR, filename)
        print(f"\n[→] Processing: {filename}")
        process_file(filepath)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multimodal file ingestion.")
    parser.add_argument("--file", help="Process a single file (optional).")
    args = parser.parse_args()

    print("=== MULTIMODAL INGESTION ===")
    if args.file:
        process_file(os.path.abspath(args.file))
    else:
        scan_raw_dir()
    print("=== DONE ===")
