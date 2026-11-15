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

  Audio (.mp3, .wav, .m4a, .ogg, .flac, .webm):
    → Transcribed locally using faster-whisper (runs 100% offline, no API)
    → Transcript saved as .md in 0-raw/ for the Refinery to process

  Images (.png, .jpg, .jpeg, .gif, .bmp, .webp):
    → Validated/normalized via Pillow and described by local vision model via Ollama
    → Description + key extracted text saved as .md in 0-raw/

Usage (called by watchdog_daemon.py or run manually):
    uv run scripts/ingest_multimodal.py
    uv run scripts/ingest_multimodal.py --file "0-raw/voice-memo.mp3"
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import re
import shutil
import sys
import threading

import ollama

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atom_mutator import atomic_write_bytes
from config import (
    AUDIO_EXTS,
    IMAGE_EXTS,
    INBOX_DIR,
    RAW_DIR,
    SIDECAR_DIR,
    VISION_MODEL,
    WHISPER_SIZE,
    _WINDOWS_RESERVED_STEMS,
    _is_reparse_or_link,
    append_archive_manifest_entry,
    archive_dir,
    build_archive_manifest_entry,
    ensure_dirs,
    file_sha256,
    now_slug,
    today,
)
from hardware_profile import fetch_loopback_json
from host_adapters import HostConsentError, OutboundSecretBlockedError, _redact_outbound_with_map
from inbox_workflow import scan_inbox_payload_risks
from ollama_utils import _is_monkeypatched_ollama_fn, _resolve_local_ollama_rule_and_url
from review_atom import review_lock
from stable_file import wait_for_stable_file

MAX_AUDIO_BYTES = 200 * 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_SIDE_PX = 2048
MAX_IMAGE_PIXELS = 64_000_000

_DEFAULT_OLLAMA_CHAT = ollama.chat
_WHISPER_LOCK = threading.Lock()
_WHISPER_CACHE: dict[str, object] = {}
_WHISPER_MODEL_CACHE = _WHISPER_CACHE


def _sanitize_stem(raw_stem: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(raw_stem or "")).strip("-.")[:64]
    if not cleaned or cleaned.lower() in _WINDOWS_RESERVED_STEMS:
        return "media"
    return cleaned


def _is_direct_raw_child(filepath: str) -> bool:
    try:
        parent = os.path.realpath(os.path.dirname(os.path.abspath(filepath)))
        raw_real = os.path.realpath(os.path.abspath(RAW_DIR))
        return os.path.normcase(parent) == os.path.normcase(raw_real)
    except OSError:
        return False


def _validate_media_source(filepath: str, *, max_bytes: int) -> int:
    if os.path.lexists(filepath) and _is_reparse_or_link(filepath):
        raise PermissionError(f"Refusing to ingest symlink or reparse point media file: {filepath}")
    if os.path.lexists(RAW_DIR) and _is_reparse_or_link(RAW_DIR):
        raise PermissionError(f"Refusing to write captures under symlinked 0-raw directory: {RAW_DIR}")
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"Media file not found: {filepath}")
    size = os.path.getsize(filepath)
    if size <= 0:
        raise ValueError(f"Media file is empty: {filepath}")
    if size > max_bytes:
        raise ValueError(f"Media file exceeds maximum allowed size ({max_bytes} bytes): {filepath}")
    return size


def unique_archive_path(filename: str) -> str:
    destination_dir = archive_dir()
    if _is_reparse_or_link(destination_dir):
        raise PermissionError(f"Refusing to archive into symlinked directory: {destination_dir}")
    stem, extension = os.path.splitext(filename)
    candidate = os.path.join(destination_dir, filename)
    suffix = 2
    while os.path.exists(candidate):
        candidate = os.path.join(destination_dir, f"{stem}-{suffix}{extension}")
        suffix += 1
    return candidate


def _resolve_multimodal_inbox_dir() -> str:
    raw_abs = os.path.abspath(RAW_DIR)
    inbox_abs = os.path.abspath(INBOX_DIR)
    if os.path.normcase(os.path.dirname(inbox_abs)) == os.path.normcase(raw_abs):
        return INBOX_DIR
    return os.path.join(RAW_DIR, "inbox")


def save_capture_and_archive(filepath: str, output_name: str, content: str, expected_source_sha256: str) -> str:
    """Commit one generated raw capture (or quarantine risky transcripts/descriptions to INBOX_DIR) under writer.lock."""
    ensure_dirs()
    lock_path = os.path.join(SIDECAR_DIR, "writer.lock")
    with review_lock(lock_path):
        if _is_reparse_or_link(RAW_DIR):
            raise PermissionError(f"Refusing to write into symlinked 0-raw directory: {RAW_DIR}")
        if not os.path.isfile(filepath) or _is_reparse_or_link(filepath) or file_sha256(filepath) != expected_source_sha256:
            raise RuntimeError("The source capture changed or was already processed during multimodal extraction.")

        normalized_content = content.replace("\r\n", "\n")
        risk_flags = scan_inbox_payload_risks(normalized_content)
        if risk_flags:
            target_dir = _resolve_multimodal_inbox_dir()
            os.makedirs(target_dir, exist_ok=True)
            if _is_reparse_or_link(target_dir):
                raise PermissionError(f"Refusing to quarantine capture into symlinked inbox directory: {target_dir}")
            risk_note = f"<!-- multimodal_quarantine_risks: {', '.join(risk_flags)} -->"
            if "<!-- second_brain_origin: user_local -->" in normalized_content:
                normalized_content = normalized_content.replace(
                    "<!-- second_brain_origin: user_local -->",
                    f"<!-- second_brain_origin: untrusted_inbox -->\n{risk_note}",
                    1,
                )
            else:
                normalized_content = (
                    f"<!-- second_brain_origin: untrusted_inbox -->\n{risk_note}\n\n"
                    + normalized_content.lstrip()
                )
            capture_origin_label = "untrusted_inbox"
        else:
            target_dir = RAW_DIR
            capture_origin_label = "user_local"

        stem, extension = os.path.splitext(output_name)
        candidate = output_name
        suffix = 2
        while True:
            out_path = os.path.join(target_dir, candidate)
            if not os.path.lexists(out_path):
                atomic_write_bytes(out_path, normalized_content.encode("utf-8"))
                break
            candidate = f"{stem}-{suffix}{extension}"
            suffix += 1

        if _is_direct_raw_child(filepath):
            archived_path = unique_archive_path(os.path.basename(filepath))
            shutil.move(filepath, archived_path)
            append_archive_manifest_entry(
                build_archive_manifest_entry(
                    original_path=filepath,
                    archived_path=archived_path,
                    source_filename=os.path.basename(filepath),
                    generated_atoms=[],
                    origin=capture_origin_label,
                    source_sha256=expected_source_sha256,
                    archived_sha256=file_sha256(archived_path),
                )
            )
        return f"inbox/{candidate}" if risk_flags else candidate


# ─── Audio Transcription ──────────────────────────────────────────────────────

def _get_whisper_model(size: str):
    from faster_whisper import WhisperModel

    with _WHISPER_LOCK:
        cached = _WHISPER_CACHE.get(size)
        if cached is None:
            cached = WhisperModel(size, device="cpu", compute_type="int8")
            _WHISPER_CACHE[size] = cached
        return cached


def transcribe_audio(filepath: str) -> str:
    """Transcribe audio file using faster-whisper (fully local)."""
    _validate_media_source(filepath, max_bytes=MAX_AUDIO_BYTES)
    print(f"  [Whisper] Transcribing: {os.path.basename(filepath)}")
    model = _get_whisper_model(WHISPER_SIZE)
    segments, info = model.transcribe(filepath, beam_size=5)

    print(
        f"  [Whisper] Detected language: {info.language} "
        f"(probability: {info.language_probability:.0%})"
    )

    transcript = " ".join(seg.text.strip() for seg in segments if seg.text and seg.text.strip())
    return transcript.strip()


def process_audio(filepath: str) -> str | None:
    _validate_media_source(filepath, max_bytes=MAX_AUDIO_BYTES)
    filename = os.path.basename(filepath)
    stem = _sanitize_stem(os.path.splitext(filename)[0])
    source_sha256 = file_sha256(filepath)
    if not source_sha256:
        raise RuntimeError(f"Could not compute SHA-256 for audio source: {filename}")

    transcript = transcribe_audio(filepath)
    if not transcript:
        print(f"  [!] Empty transcript for {filename}. Skipping.")
        return None

    out_name = f"{now_slug()}-voice-{stem}.md"
    content = (
        "<!-- second_brain_origin: user_local -->\n"
        f"<!-- Auto-transcribed from: {filename} — {today()} -->\n"
        "<!-- Source: voice memo -->\n\n"
        f"{transcript}\n"
    )
    archive_note = _is_direct_raw_child(filepath)
    out_name = save_capture_and_archive(filepath, out_name, content, source_sha256)

    print(f"  [✓] Transcript saved → {out_name}")
    if archive_note:
        print("  [✓] Original archived.")
    return out_name


# ─── Image Understanding ─────────────────────────────────────────────────────

def ollama_base_url() -> str:
    _, validated = _resolve_local_ollama_rule_and_url()
    return validated


def available_ollama_models() -> list[dict]:
    """Return local Ollama model metadata strictly over loopback without proxy/redirect leakage."""
    try:
        _, base = _resolve_local_ollama_rule_and_url()
    except (HostConsentError, PermissionError, ValueError):
        return []
    payload = fetch_loopback_json(f"{base}/api/tags", timeout=5.0, max_bytes=65536) if base else None
    if isinstance(payload, dict) and isinstance(payload.get("models"), list):
        return [m for m in payload["models"] if isinstance(m, dict)]

    if not base:
        return []
    try:
        client = ollama.Client(host=base, timeout=5.0, follow_redirects=False, trust_env=False)
        response = client.list()
    except Exception:
        return []

    models = response.get("models", response) if isinstance(response, dict) else getattr(response, "models", response)
    normalized = []
    for model in models or []:
        if isinstance(model, dict):
            normalized.append(model)
        else:
            normalized.append(
                {
                    "name": getattr(model, "model", None) or getattr(model, "name", ""),
                    "model": getattr(model, "model", None) or getattr(model, "name", ""),
                    "capabilities": getattr(model, "capabilities", []),
                }
            )
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

    if VISION_MODEL in exact_names:
        return VISION_MODEL
    if configured_base in names:
        matched = names[configured_base]
        return matched.get("name") or matched.get("model") or VISION_MODEL

    for model in models:
        capabilities = model.get("capabilities") or []
        model_id = str(model.get("name") or model.get("model") or "")
        if "vision" in capabilities or any(v in model_id.lower() for v in ("llava", "vision", "vl", "moondream", "minicpm-v")):
            return model_id

    raise RuntimeError(
        "No vision-capable Ollama model is installed. "
        f"Pull {VISION_MODEL!r} or set BRAIN_VISION_MODEL to an installed vision model."
    )


def prepare_image_bytes(filepath: str) -> bytes:
    """Validate, normalize (.bmp/.gif/RGBA -> RGB), and bound image resolution via Pillow."""
    _validate_media_source(filepath, max_bytes=MAX_IMAGE_BYTES)
    try:
        from PIL import Image, ImageOps
    except ImportError:
        with open(filepath, "rb") as fh:
            return fh.read()

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    with Image.open(filepath) as img:
        if getattr(img, "is_animated", False):
            img.seek(0)
        img = ImageOps.exif_transpose(img)
        orig_format = (img.format or "").upper()
        if img.mode not in ("RGB", "L"):
            if "A" in img.mode or img.mode == "P":
                rgba = img.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[3])
                img = background
            else:
                img = img.convert("RGB")
        elif img.mode == "L":
            img = img.convert("RGB")

        if max(img.size) > MAX_IMAGE_SIDE_PX:
            img.thumbnail((MAX_IMAGE_SIDE_PX, MAX_IMAGE_SIDE_PX), Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        target_format = "JPEG" if orig_format in {"JPEG", "JPG"} else "PNG"
        save_kwargs = {"quality": 90, "optimize": True} if target_format == "JPEG" else {"optimize": True}
        img.save(buf, format=target_format, **save_kwargs)
        return buf.getvalue()


def describe_image(filepath: str) -> str:
    """Use a local vision model via Ollama to extract knowledge from an image."""
    normalized_bytes = prepare_image_bytes(filepath)
    rule, base_url = _resolve_local_ollama_rule_and_url()
    model_name = select_vision_model()
    print(f"  [Vision] Analysing image with {model_name}: {os.path.basename(filepath)}")

    image_data = base64.b64encode(normalized_bytes).decode("utf-8")

    prompt = """You are extracting knowledge from an image for a personal knowledge base.
Describe what you see in detail. If there is text in the image, transcribe it exactly.
If there is a diagram, chart, or whiteboard, explain what it shows.
Focus on information and ideas — not aesthetics. Be thorough.
Output plain text only."""

    outbound_prompt = prompt
    if rule.redact_pii or rule.fail_on_secret_detection:
        report, _ = _redact_outbound_with_map(
            prompt,
            redact_pii=rule.redact_pii,
            fail_on_secret_detection=rule.fail_on_secret_detection,
        )
        outbound_prompt = report.sanitized_text

    messages = [
        {
            "role": "user",
            "content": outbound_prompt,
            "images": [image_data],
        }
    ]
    if _is_monkeypatched_ollama_fn(ollama.chat, _DEFAULT_OLLAMA_CHAT):
        response = ollama.chat(model=model_name, messages=messages)
    else:
        client = ollama.Client(
            host=base_url,
            timeout=120.0,
            follow_redirects=False,
            trust_env=False,
        )
        response = client.chat(model=model_name, messages=messages)
    return response["message"]["content"].strip()


def process_image(filepath: str) -> str | None:
    _validate_media_source(filepath, max_bytes=MAX_IMAGE_BYTES)
    filename = os.path.basename(filepath)
    stem = _sanitize_stem(os.path.splitext(filename)[0])
    source_sha256 = file_sha256(filepath)
    if not source_sha256:
        raise RuntimeError(f"Could not compute SHA-256 for image source: {filename}")

    description = describe_image(filepath)
    if not description:
        print(f"  [!] No description returned for {filename}. Skipping.")
        return None

    out_name = f"{now_slug()}-image-{stem}.md"
    content = (
        "<!-- second_brain_origin: user_local -->\n"
        f"<!-- Auto-described from image: {filename} — {today()} -->\n"
        "<!-- Source: image capture via local vision model -->\n\n"
        f"{description}\n"
    )
    archive_note = _is_direct_raw_child(filepath)
    out_name = save_capture_and_archive(filepath, out_name, content, source_sha256)

    print(f"  [✓] Image description saved → {out_name}")
    if archive_note:
        print("  [✓] Original archived.")
    return out_name


# ─── Entry Point ─────────────────────────────────────────────────────────────

def process_file(filepath: str) -> str | None:
    ext = os.path.splitext(filepath)[1].lower()
    if ext in AUDIO_EXTS:
        return process_audio(filepath)
    if ext in IMAGE_EXTS:
        return process_image(filepath)
    print(f"  [skip] Unsupported type: {ext}")
    return None


def scan_raw_dir() -> list[str]:
    """Find and process all unhandled audio/image files in 0-raw/."""
    ensure_dirs()
    if _is_reparse_or_link(RAW_DIR):
        raise PermissionError(f"Refusing to scan symlinked 0-raw directory: {RAW_DIR}")
    files = [
        f
        for f in sorted(os.listdir(RAW_DIR))
        if not f.startswith((".", "~"))
        and os.path.splitext(f)[1].lower() in (AUDIO_EXTS | IMAGE_EXTS)
    ]
    if not files:
        print("[*] No audio or image files found in 0-raw/.")
        return []
    saved: list[str] = []
    for filename in files:
        filepath = os.path.join(RAW_DIR, filename)
        if _is_reparse_or_link(filepath) or not os.path.isfile(filepath):
            continue
        if not wait_for_stable_file(filepath):
            print(f"  [!] Skipping unstable or empty media file: {filename}")
            continue
        print(f"\n[→] Processing: {filename}")
        try:
            out = process_file(filepath)
            if out:
                saved.append(out)
        except Exception as exc:
            print(f"  [!] Failed to process {filename}: {exc}")
    return saved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Multimodal file ingestion.")
    parser.add_argument("--file", help="Process a single file (optional).")
    args = parser.parse_args(argv)

    print("=== MULTIMODAL INGESTION ===")
    try:
        if args.file:
            process_file(os.path.abspath(args.file))
        else:
            scan_raw_dir()
    except Exception as exc:
        print(f"Error: {exc}")
        return 1
    print("=== DONE ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
