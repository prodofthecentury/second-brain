# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#     "faster-whisper",
#     "ollama",
#     "chromadb",
#     "Pillow",
# ]
# ///
"""
Feature 1 — Multimodal & Document Ingestion
Handles audio files (voice memos), image files, and local documents dropped into 0-raw/.

  Audio (.mp3, .wav, .m4a, .ogg, .flac, .webm):
    → Transcribed locally using faster-whisper (runs 100% offline, no API)
    → Timestamped transcript ([MM:SS.mmm -> MM:SS.mmm] lines) saved as .md in 0-raw/

  Images (.png, .jpg, .jpeg, .gif, .bmp, .webp):
    → Validated/normalized via Pillow (fails closed without Pillow) and described by a local vision model
    → Description + key extracted text saved as .md in 0-raw/

  Documents (.pdf, .docx, .epub, .html, .htm, .csv, .tsv):
    → Deterministic, model-free extraction (scripts/doc_extractors.py) with [page N] / section / [row N] anchors

Every capture starts with structured YAML provenance frontmatter (extractor, model, model_digest,
source_binary, source_sha256, limitations). The immutable source binary is preserved in
0-raw/archive/YYYY-MM/ (moved when it lives directly in 0-raw/, copied when external) and recorded
in the ARCHIVE_MANIFEST with SHA-256 lineage.

Usage (called by watchdog_daemon.py or run manually):
    uv run scripts/ingest_multimodal.py
    uv run scripts/ingest_multimodal.py --file "0-raw/voice-memo.mp3"
    uv run scripts/ingest_multimodal.py path/to/paper.pdf other/notes.docx
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
    _sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__")
    raise SystemExit(0)


import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import types

try:
    import ollama
except ImportError:  # Clean minimal wheel without optional [online] extra
    ollama = types.SimpleNamespace(chat=None, embed=None, Client=None)  # type: ignore[assignment]

from scripts.atom_mutator import atomic_write_bytes
from scripts.config import (
    AUDIO_EXTS,
    DOC_EXTS,
    IMAGE_EXTS,
    INBOX_DIR,
    RAW_DIR,
    SIDECAR_DIR,
    VIDEO_EXTS,
    VIDEO_ONLY_EXTS,
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
from scripts.video_extractor import (
    MAX_VIDEO_BYTES,
    build_video_timeline_markdown,
    extract_video_keyframes_and_audio,
    is_video_container,
)
from scripts.doc_extractors import EXTRACTION_BODY_MARKER, MAX_DOC_BINARY_BYTES, extract_document
from scripts.hardware_profile import (
    call_ollama_chat_maybe_no_think,
    fetch_loopback_json,
    strip_reasoning_think_blocks,
)
from scripts.host_adapters import (
    HostConsentError,
    OutboundSecretBlockedError,
    _redact_outbound_with_map,
    inspect_and_redact_outbound,
)
from scripts.inbox_workflow import scan_inbox_payload_risks
from scripts.ollama_utils import _is_monkeypatched_ollama_fn, _resolve_local_ollama_rule_and_url
from scripts.review_atom import review_lock
from scripts.stable_file import wait_for_stable_file

MAX_AUDIO_BYTES = 200 * 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_API_MEDIA_UPLOAD_BYTES = 24 * 1024 * 1024
MAX_IMAGE_SIDE_PX = 2048
MAX_IMAGE_PIXELS = 64_000_000
_THREAD_VAULT_CONTEXT = threading.local()
SOURCE_BINARY_TOKEN = "{{SECOND_BRAIN_SOURCE_BINARY}}"
PILLOW_REQUIRED_MESSAGE = (
    "Pillow (PIL) is required to validate and sanitize images before vision ingestion; refusing to send "
    "unvalidated image bytes. Install it with `setup.ps1 -IncludeMultimodal`, `setup.sh --include-multimodal`, "
    "or `uv sync --frozen --extra multimodal`."
)
AUDIO_LIMITATIONS = (
    "Automated local ASR transcript; verify homophones, speaker turns, numbers, and technical terms "
    "against the archived source audio."
)
IMAGE_LIMITATIONS = (
    "Automated local vision-model description; verify OCR text, numbers, and spatial details against "
    "the archived source image."
)
VIDEO_LIMITATIONS = (
    "Automated local video keyframe + ASR timeline extraction; verify visual frame text, speakers, "
    "and exact quotes against the archived source video."
)

_DEFAULT_OLLAMA_CHAT = ollama.chat
_WHISPER_LOCK = threading.Lock()
_WHISPER_CACHE: dict[str, object] = {}
_WHISPER_MODEL_CACHE = _WHISPER_CACHE


def _yaml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def render_provenance_capture(
    *,
    capture_type: str,
    extractor: str,
    model: str,
    model_digest: str,
    source_filename: str,
    source_sha256: str,
    limitations: str,
    body: str,
    extra: dict[str, object] | None = None,
) -> str:
    """Render a raw capture with structured YAML provenance frontmatter and a sticky origin comment.

    ``source_binary`` is filled with the vault-relative archive path inside ``save_capture_and_archive``.
    """
    fields: list[tuple[str, object]] = [
        ("type", "raw_extraction"),
        ("capture_type", capture_type),
        ("extractor", extractor),
        ("model", model),
        ("model_digest", model_digest or "unavailable"),
        ("source_filename", os.path.basename(source_filename)),
        ("source_binary", SOURCE_BINARY_TOKEN),
        ("source_sha256", source_sha256),
        ("extracted", today()),
        ("limitations", limitations),
    ]
    for key, value in (extra or {}).items():
        if key in {k for k, _ in fields} or not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", key):
            continue
        fields.append((key, value))
    frontmatter = "\n".join(f"{key}: {_yaml_scalar(value)}" for key, value in fields)
    normalized_body = body.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return (
        f"---\n{frontmatter}\n---\n"
        "<!-- second_brain_origin: user_local -->\n"
        f"{EXTRACTION_BODY_MARKER}\n"
        f"{normalized_body}\n"
    )


def _sanitize_stem(raw_stem: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(raw_stem or "")).strip("-.")[:64]
    if not cleaned or cleaned.lower() in _WINDOWS_RESERVED_STEMS:
        return "media"
    return cleaned


def _effective_raw_dir() -> str:
    return str(getattr(_THREAD_VAULT_CONTEXT, "raw_dir", None) or RAW_DIR)


def _effective_inbox_dir() -> str:
    return str(getattr(_THREAD_VAULT_CONTEXT, "inbox_dir", None) or INBOX_DIR)


def _is_direct_raw_child(filepath: str) -> bool:
    try:
        parent = os.path.realpath(os.path.dirname(os.path.abspath(filepath)))
        raw_real = os.path.realpath(os.path.abspath(_effective_raw_dir()))
        return os.path.normcase(parent) == os.path.normcase(raw_real)
    except OSError:
        return False


def _validate_media_source(filepath: str, *, max_bytes: int) -> int:
    eff_raw = _effective_raw_dir()
    if os.path.lexists(filepath) and _is_reparse_or_link(filepath):
        raise PermissionError(f"Refusing to ingest symlink or reparse point media file: {filepath}")
    if os.path.lexists(eff_raw) and _is_reparse_or_link(eff_raw):
        raise PermissionError(f"Refusing to write captures under symlinked 0-raw directory: {eff_raw}")
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"Media file not found: {filepath}")
    size = os.path.getsize(filepath)
    if size <= 0:
        raise ValueError(f"Media file is empty: {filepath}")
    if size > max_bytes:
        raise ValueError(f"Media file exceeds maximum allowed size ({max_bytes} bytes): {filepath}")
    return size


def unique_archive_path(filename: str) -> str:
    # Resolve from effective thread/module RAW_DIR so isolated/concurrent vaults never cross-archive.
    import datetime as _dt

    eff_raw = _effective_raw_dir()
    destination_dir = os.path.join(eff_raw, "archive", _dt.datetime.now().strftime("%Y-%m"))
    os.makedirs(destination_dir, exist_ok=True)
    if _is_reparse_or_link(destination_dir) or _is_reparse_or_link(os.path.dirname(destination_dir)):
        raise PermissionError(f"Refusing to archive into symlinked directory: {destination_dir}")
    stem, extension = os.path.splitext(filename)
    candidate = os.path.join(destination_dir, filename)
    suffix = 2
    while os.path.exists(candidate):
        candidate = os.path.join(destination_dir, f"{stem}-{suffix}{extension}")
        suffix += 1
    return candidate


def _resolve_multimodal_inbox_dir() -> str:
    eff_raw = _effective_raw_dir()
    eff_inbox = _effective_inbox_dir()
    raw_abs = os.path.abspath(eff_raw)
    inbox_abs = os.path.abspath(eff_inbox)
    if os.path.normcase(os.path.dirname(inbox_abs)) == os.path.normcase(raw_abs):
        return eff_inbox
    return os.path.join(eff_raw, "inbox")


def _copy_source_binary(filepath: str, archived_path: str, expected_source_sha256: str) -> None:
    """Non-destructively copy an external source binary into the archive and verify its hash."""
    tmp_path = archived_path + ".partial"
    try:
        with open(filepath, "rb") as src, open(tmp_path, "xb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        if file_sha256(tmp_path) != expected_source_sha256:
            raise RuntimeError("The external source changed while it was being archived.")
        os.replace(tmp_path, archived_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def save_capture_and_archive(
    filepath: str,
    output_name: str,
    content: str,
    expected_source_sha256: str,
    *,
    allow_large: bool = False,
) -> str:
    """Commit one generated raw capture (or quarantine risky output to INBOX_DIR) under writer.lock.

    The immutable source binary is always preserved in ``0-raw/archive/YYYY-MM/`` with an
    ARCHIVE_MANIFEST entry: moved when it lives directly in 0-raw/, copied (verified by SHA-256)
    when it is an external path so the caller's file is never touched.
    """
    eff_raw = _effective_raw_dir()
    if getattr(_THREAD_VAULT_CONTEXT, "raw_dir", None) is None:
        ensure_dirs()
    os.makedirs(eff_raw, exist_ok=True)
    brain_root_dir = os.path.dirname(os.path.abspath(eff_raw))
    sidecar_dir = str(getattr(_THREAD_VAULT_CONTEXT, "sidecar_dir", None) or os.path.join(brain_root_dir, ".sidecar"))
    os.makedirs(sidecar_dir, exist_ok=True)
    lock_path = os.path.join(sidecar_dir, "writer.lock")
    with review_lock(lock_path):
        if _is_reparse_or_link(eff_raw):
            raise PermissionError(f"Refusing to write into symlinked 0-raw directory: {eff_raw}")
        if not os.path.isfile(filepath) or _is_reparse_or_link(filepath) or file_sha256(filepath) != expected_source_sha256:
            raise RuntimeError("The source capture changed or was already processed during multimodal extraction.")

        direct_child = _is_direct_raw_child(filepath)
        archived_path = unique_archive_path(os.path.basename(filepath))
        archived_rel = os.path.relpath(archived_path, brain_root_dir).replace("\\", "/")

        normalized_content = content.replace("\r\n", "\n").replace(SOURCE_BINARY_TOKEN, archived_rel)
        dlp_rep = inspect_and_redact_outbound(normalized_content, redact_pii=False, fail_on_secret_detection=False)
        dlp_flags = tuple(f"dlp_secret:{h}" for h in dlp_rep.secret_hits)
        if dlp_flags:
            normalized_content = dlp_rep.sanitized_text
            setattr(_THREAD_VAULT_CONTEXT, "last_dlp_flags", list(dlp_flags))
        else:
            setattr(_THREAD_VAULT_CONTEXT, "last_dlp_flags", [])

        base_risk_flags = scan_inbox_payload_risks(normalized_content)
        if allow_large:
            base_risk_flags = tuple(flag for flag in base_risk_flags if flag != "oversized_payload")
        risk_flags = tuple(dict.fromkeys([*base_risk_flags, *dlp_flags]))
        if risk_flags:
            target_dir = _resolve_multimodal_inbox_dir()
            os.makedirs(target_dir, exist_ok=True)
            if _is_reparse_or_link(target_dir):
                raise PermissionError(f"Refusing to quarantine capture into symlinked inbox directory: {target_dir}")
            risk_note = f"<!-- multimodal_quarantine_risks: {', '.join(risk_flags)} -->"
            stripped = re.sub(r"(?m)^<!--\s*second_brain_origin:[^>]*-->\n?", "", normalized_content, count=1)
            # Quarantine provenance must be on line 1 (inbox_workflow._extract_origin_tag reads line 1 only).
            normalized_content = f"<!-- second_brain_origin: untrusted_inbox -->\n{risk_note}\n" + stripped.lstrip("\ufeff")
            capture_origin_label = "untrusted_inbox"
        else:
            target_dir = eff_raw
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

        if direct_child:
            shutil.move(filepath, archived_path)
        else:
            _copy_source_binary(filepath, archived_path, expected_source_sha256)
        append_archive_manifest_entry(
            build_archive_manifest_entry(
                original_path=filepath,
                archived_path=archived_path,
                source_filename=os.path.basename(filepath),
                generated_atoms=[],
                origin=capture_origin_label,
                source_sha256=expected_source_sha256,
                archived_sha256=file_sha256(archived_path),
                brain_dir=brain_root_dir,
            ),
            manifest_path=os.path.join(eff_raw, "archive", "manifest.jsonl"),
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


def format_timestamp(seconds: float) -> str:
    """Format seconds as MM:SS.mmm (minutes may exceed 59 for long recordings)."""
    total_ms = int(round(max(0.0, float(seconds)) * 1000.0))
    minutes, rem_ms = divmod(total_ms, 60_000)
    secs, millis = divmod(rem_ms, 1000)
    return f"{minutes:02d}:{secs:02d}.{millis:03d}"


def format_transcript_segments(segments) -> str:
    """Render faster-whisper segments as `[MM:SS.mmm -> MM:SS.mmm] text` lines."""
    lines: list[str] = []
    for seg in segments:
        text = (getattr(seg, "text", "") or "").strip()
        if not text:
            continue
        start = format_timestamp(getattr(seg, "start", 0.0) or 0.0)
        end = format_timestamp(getattr(seg, "end", 0.0) or 0.0)
        lines.append(f"[{start} -> {end}] {text}")
    return "\n".join(lines)


def whisper_model_digest(size: str) -> str:
    """Best-effort content digest of the locally cached faster-whisper model (never downloads)."""
    try:
        from faster_whisper.utils import download_model

        model_dir = download_model(size, local_files_only=True)
    except Exception:
        return "unavailable"
    digest = hashlib.sha256()
    found = False
    for name in ("config.json", "tokenizer.json", "vocabulary.txt", "vocabulary.json"):
        path = os.path.join(model_dir, name)
        if os.path.isfile(path):
            found = True
            with open(path, "rb") as fh:
                digest.update(name.encode() + b"\0" + fh.read())
    model_bin = os.path.join(model_dir, "model.bin")
    if os.path.isfile(model_bin):
        found = True
        digest.update(b"model.bin-size\0" + str(os.path.getsize(model_bin)).encode())
    return f"sha256:{digest.hexdigest()}" if found else "unavailable"


def transcribe_audio(filepath: str) -> str:
    """Transcribe audio file using faster-whisper (fully local) into timestamped segment lines."""
    _validate_media_source(filepath, max_bytes=MAX_AUDIO_BYTES)
    print(f"  [Whisper] Transcribing: {os.path.basename(filepath)}")
    model = _get_whisper_model(WHISPER_SIZE)
    segments, info = model.transcribe(filepath, beam_size=5)

    print(
        f"  [Whisper] Detected language: {info.language} "
        f"(probability: {info.language_probability:.0%})"
    )
    return format_transcript_segments(segments).strip()


def process_audio(filepath: str) -> str | None:
    from scripts.config import configure_utf8_stdio

    configure_utf8_stdio()
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
    content = render_provenance_capture(
        capture_type="audio_transcript",
        extractor="faster-whisper",
        model=f"faster-whisper {WHISPER_SIZE} (cpu int8)",
        model_digest=whisper_model_digest(WHISPER_SIZE),
        source_filename=filename,
        source_sha256=source_sha256,
        limitations=AUDIO_LIMITATIONS,
        body=f"# Voice memo transcript: {filename}\n\n{transcript}",
        extra={"timestamp_format": "[MM:SS.mmm -> MM:SS.mmm]"},
    )
    archive_note = _is_direct_raw_child(filepath)
    out_name = save_capture_and_archive(filepath, out_name, content, source_sha256)

    print(f"  [✓] Transcript saved → {out_name}")
    print("  [✓] Original archived." if archive_note else "  [✓] External original copied into archive.")
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
                    "digest": getattr(model, "digest", "") or "",
                    "capabilities": getattr(model, "capabilities", []),
                }
            )
    return normalized


def resolve_vision_model_metadata() -> tuple[str, str]:
    """Return ``(model_name, model_digest)`` for the active local vision model."""
    models = available_ollama_models()
    by_exact: dict[str, dict] = {}
    by_base: dict[str, dict] = {}
    for item in models:
        mid = str(item.get("name") or item.get("model") or "")
        if not mid:
            continue
        by_exact[mid] = item
        by_base.setdefault(mid.split(":")[0], item)

    def _fmt_digest(entry: dict | None) -> str:
        raw = str((entry or {}).get("digest") or "").strip()
        if not raw:
            return "unavailable"
        return raw if raw.startswith("sha256:") else f"sha256:{raw}"

    configured_base = VISION_MODEL.split(":")[0]
    if VISION_MODEL in by_exact:
        return VISION_MODEL, _fmt_digest(by_exact[VISION_MODEL])
    if configured_base in by_base:
        matched = by_base[configured_base]
        chosen = str(matched.get("name") or matched.get("model") or VISION_MODEL)
        return chosen, _fmt_digest(matched)

    for item in models:
        capabilities = item.get("capabilities") or []
        model_id = str(item.get("name") or item.get("model") or "")
        if "vision" in capabilities or any(v in model_id.lower() for v in ("llava", "vision", "vl", "moondream", "minicpm-v")):
            return model_id, _fmt_digest(item)

    raise RuntimeError(
        "No vision-capable Ollama model is installed. "
        f"Pull {VISION_MODEL!r} or set BRAIN_VISION_MODEL to an installed vision model."
    )


def select_vision_model() -> str:
    """Use the configured vision model, or fall back to any installed vision model."""
    model_name, _ = resolve_vision_model_metadata()
    return model_name


def prepare_image_bytes(filepath: str) -> bytes:
    """Validate, normalize (.bmp/.gif/RGBA -> RGB), and bound image resolution via Pillow.

    Fails closed when Pillow is absent rather than sending unvalidated raw image bytes.
    """
    _validate_media_source(filepath, max_bytes=MAX_IMAGE_BYTES)
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError(PILLOW_REQUIRED_MESSAGE) from exc

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


def describe_image_with_provenance(filepath: str) -> tuple[str, str, str]:
    """Return ``(description, model_name, model_digest)`` using a local Ollama vision model."""
    normalized_bytes = prepare_image_bytes(filepath)
    rule, base_url = _resolve_local_ollama_rule_and_url()
    if select_vision_model is _DEFAULT_SELECT_VISION_MODEL:
        model_name, model_digest = resolve_vision_model_metadata()
    else:
        model_name = select_vision_model()
        model_digest = "unavailable"
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
        if getattr(ollama, "Client", None) is None:
            raise RuntimeError(
                "Python 'ollama' package is not installed; install 'second-brain[online]' for local vision extraction."
            )
        client = ollama.Client(
            host=base_url,
            timeout=480.0,
            follow_redirects=False,
            trust_env=False,
        )
        response = call_ollama_chat_maybe_no_think(
            client.chat,
            model=model_name,
            messages=messages,
            options={"temperature": 0.1, "repeat_penalty": 1.2, "num_predict": 256},
        )
    if isinstance(response, dict) and response.get("model_digest"):
        model_digest = str(response["model_digest"])
    raw_text = str((response.get("message") or {}).get("content") or "").strip()
    cleaned_text = strip_reasoning_think_blocks(raw_text).strip()
    return cleaned_text or raw_text, model_name, model_digest


_DEFAULT_SELECT_VISION_MODEL = select_vision_model
_DEFAULT_DESCRIBE_IMAGE_WITH_PROVENANCE = describe_image_with_provenance


def describe_image(filepath: str) -> str:
    """Use a local vision model via Ollama to extract knowledge from an image."""
    description, _, _ = describe_image_with_provenance(filepath)
    return description


_DEFAULT_DESCRIBE_IMAGE = describe_image


def process_image(filepath: str) -> str | None:
    _validate_media_source(filepath, max_bytes=MAX_IMAGE_BYTES)
    filename = os.path.basename(filepath)
    stem = _sanitize_stem(os.path.splitext(filename)[0])
    source_sha256 = file_sha256(filepath)
    if not source_sha256:
        raise RuntimeError(f"Could not compute SHA-256 for image source: {filename}")

    if describe_image is not _DEFAULT_DESCRIBE_IMAGE:
        description = describe_image(filepath)
        model_name, model_digest = VISION_MODEL, "unavailable"
    else:
        description, model_name, model_digest = describe_image_with_provenance(filepath)
    if not description:
        print(f"  [!] No description returned for {filename}. Skipping.")
        return None

    out_name = f"{now_slug()}-image-{stem}.md"
    content = render_provenance_capture(
        capture_type="image_description",
        extractor="ollama-vision",
        model=model_name,
        model_digest=model_digest,
        source_filename=filename,
        source_sha256=source_sha256,
        limitations=IMAGE_LIMITATIONS,
        body=f"# Image capture description: {filename}\n\n{description}",
    )
    archive_note = _is_direct_raw_child(filepath)
    out_name = save_capture_and_archive(filepath, out_name, content, source_sha256)

    print(f"  [✓] Image description saved → {out_name}")
    print("  [✓] Original archived." if archive_note else "  [✓] External original copied into archive.")
    return out_name


# ─── Deterministic Document Extraction (.pdf, .docx, .epub, .html, .csv, .tsv) ─

def extract_document_markdown(
    filepath: str,
    *,
    max_bytes: int = MAX_DOC_BINARY_BYTES,
) -> tuple[str, dict[str, object]]:
    """Extract deterministic Markdown and provenance metadata from a local document."""
    extraction = extract_document(filepath, max_bytes=max_bytes)
    meta = extraction.provenance()
    if extraction.page_spans:
        meta["page_spans"] = extraction.page_spans
    return extraction.markdown, meta


def process_document(filepath: str) -> str | None:
    from scripts.config import configure_utf8_stdio

    configure_utf8_stdio()
    _validate_media_source(filepath, max_bytes=MAX_DOC_BINARY_BYTES)
    filename = os.path.basename(filepath)
    ext = os.path.splitext(filename)[1].lower()
    stem = _sanitize_stem(os.path.splitext(filename)[0])
    source_sha256 = file_sha256(filepath)
    if not source_sha256:
        raise RuntimeError(f"Could not compute SHA-256 for document source: {filename}")

    extraction = extract_document(filepath, max_bytes=MAX_DOC_BINARY_BYTES)
    prov = extraction.provenance()
    extra = {
        key: value
        for key, value in prov.items()
        if key not in {"extractor", "model", "model_digest", "limitations"}
    }
    out_name = f"{now_slug()}-doc-{stem}.md"
    content = render_provenance_capture(
        capture_type=f"document_{ext.lstrip('.')}",
        extractor=extraction.extractor,
        model=extraction.model,
        model_digest=extraction.model_digest,
        source_filename=filename,
        source_sha256=source_sha256,
        limitations=extraction.limitations,
        body=extraction.markdown,
        extra=extra,
    )
    archive_note = _is_direct_raw_child(filepath)
    out_name = save_capture_and_archive(filepath, out_name, content, source_sha256, allow_large=True)
    print(f"  [✓] Document extracted → {out_name}")
    print("  [✓] Original archived." if archive_note else "  [✓] External original copied into archive.")
    return out_name


# ─── Entry Point ─────────────────────────────────────────────────────────────

SUPPORTED_INGEST_EXTS = AUDIO_EXTS | IMAGE_EXTS | DOC_EXTS | VIDEO_EXTS


def process_video(
    filepath: str,
    *,
    runner: object = subprocess.run,
    frame_describer: object | None = None,
    audio_transcriber: object | None = None,
) -> str | None:
    """Extract deduplicated scene keyframes + 16 kHz ASR audio timeline from a local video."""
    _validate_media_source(filepath, max_bytes=MAX_VIDEO_BYTES)
    filename = os.path.basename(filepath)
    stem = _sanitize_stem(os.path.splitext(filename)[0])
    source_sha256 = file_sha256(filepath)
    if not source_sha256:
        raise RuntimeError(f"Could not compute SHA-256 for video source: {filename}")

    with tempfile.TemporaryDirectory(prefix="sb_video_") as work_dir:
        extracted = extract_video_keyframes_and_audio(filepath, work_dir, runner=runner)
        wav_path = extracted.get("wav_path")
        keyframes = list(extracted.get("keyframes") or [])

        transcript = ""
        asr_model = "unavailable"
        asr_digest = "unavailable"
        if wav_path and os.path.isfile(wav_path):
            trans_fn = audio_transcriber or transcribe_audio
            try:
                transcript = str(trans_fn(wav_path) or "").strip()
                asr_model = f"faster-whisper {WHISPER_SIZE} (cpu int8)"
                asr_digest = whisper_model_digest(WHISPER_SIZE)
            except RuntimeError as exc:
                if not keyframes:
                    raise
                print(f"  [!] Video audio ASR skipped ({exc}); keeping keyframe extraction.")

        frame_notes: list[dict[str, object]] = []
        vis_model = VISION_MODEL
        vis_digest = "unavailable"
        for kf in keyframes:
            kf_path = str(kf["path"])
            desc = ""
            if frame_describer is not None:
                desc = str(frame_describer(kf_path) or "").strip()
            elif describe_image is not _DEFAULT_DESCRIBE_IMAGE:
                desc = str(describe_image(kf_path) or "").strip()
            else:
                try:
                    desc, vis_model, vis_digest = describe_image_with_provenance(kf_path)
                except Exception as exc:
                    desc = f"[Visual frame keyframe extracted; local vision model unavailable ({exc}).]"
            frame_notes.append(
                {
                    "timestamp_seconds": kf["timestamp_seconds"],
                    "timestamp_label": kf["timestamp_label"],
                    "sha256": kf["sha256"],
                    "description": desc,
                }
            )

    if not transcript and not frame_notes:
        print(f"  [!] No transcript or keyframes extracted for {filename}. Skipping.")
        return None

    timeline_body = build_video_timeline_markdown(
        filename,
        transcript=transcript,
        frame_notes=frame_notes,
    )
    out_name = f"{now_slug()}-video-{stem}.md"
    content = render_provenance_capture(
        capture_type="video_timeline",
        extractor="ffmpeg+faster-whisper+vision",
        model=f"{vis_model} | {asr_model}",
        model_digest=vis_digest if vis_digest != "unavailable" else asr_digest,
        source_filename=filename,
        source_sha256=source_sha256,
        limitations=VIDEO_LIMITATIONS,
        body=timeline_body,
        extra={
            "keyframe_count": len(frame_notes),
            "keyframe_sha256s": [fn["sha256"] for fn in frame_notes[:24]],
            "has_audio_track": bool(wav_path),
            "timestamp_format": "[TIMELINE] [FRAME t=HH:MM:SS.mmm sha256=...] & [MM:SS.mmm -> MM:SS.mmm]",
        },
    )
    archive_note = _is_direct_raw_child(filepath)
    out_name = save_capture_and_archive(filepath, out_name, content, source_sha256, allow_large=True)
    print(f"  [✓] Video timeline saved → {out_name}")
    print("  [✓] Original archived." if archive_note else "  [✓] External original copied into archive.")
    return out_name


def ingest_local_video_file(
    filepath: str,
    *,
    brain_dir: str | os.PathLike[str] | None = None,
    runner: object = subprocess.run,
    frame_describer: object | None = None,
    audio_transcriber: object | None = None,
) -> dict[str, object]:
    """Safely ingest an on-disk video file (<=512 MiB) into the active vault without base64 bloat."""
    raw_path = os.path.abspath(str(filepath or "").strip())
    if not raw_path or not os.path.isfile(raw_path):
        raise FileNotFoundError(f"Video file not found: {filepath}")
    if _is_reparse_or_link(raw_path):
        raise PermissionError(f"Refusing symlink/reparse video source: {raw_path}")
    ext = os.path.splitext(raw_path)[1].lower()
    if ext not in VIDEO_EXTS:
        raise ValueError(f"Unsupported video extension {ext!r}; supported: {sorted(VIDEO_EXTS)}")
    _validate_media_source(raw_path, max_bytes=MAX_VIDEO_BYTES)
    source_sha = file_sha256(raw_path)

    resolved_brain = os.path.abspath(str(brain_dir)) if brain_dir is not None else os.path.dirname(os.path.abspath(RAW_DIR))
    target_raw = os.path.join(resolved_brain, "0-raw")
    target_inbox = os.path.join(target_raw, "inbox")
    target_sidecar = os.path.join(resolved_brain, ".sidecar")
    for check_p in (resolved_brain, target_raw, target_inbox, target_sidecar):
        if os.path.lexists(check_p) and _is_reparse_or_link(check_p):
            raise PermissionError(f"Refusing to ingest video under symlinked directory: {check_p}")
        os.makedirs(check_p, exist_ok=True)

    prev_ctx = (
        getattr(_THREAD_VAULT_CONTEXT, "raw_dir", None),
        getattr(_THREAD_VAULT_CONTEXT, "inbox_dir", None),
        getattr(_THREAD_VAULT_CONTEXT, "sidecar_dir", None),
    )
    _THREAD_VAULT_CONTEXT.raw_dir = target_raw
    _THREAD_VAULT_CONTEXT.inbox_dir = target_inbox
    _THREAD_VAULT_CONTEXT.sidecar_dir = target_sidecar
    _THREAD_VAULT_CONTEXT.last_dlp_flags = []
    try:
        if ext == ".webm" and not is_video_container(raw_path):
            saved_rel = process_audio(raw_path)
            category = "audio"
        else:
            saved_rel = process_video(
                raw_path,
                runner=runner,
                frame_describer=frame_describer,
                audio_transcriber=audio_transcriber,
            )
            category = "video"
        dlp_hits = list(getattr(_THREAD_VAULT_CONTEXT, "last_dlp_flags", []) or [])
    finally:
        _THREAD_VAULT_CONTEXT.raw_dir, _THREAD_VAULT_CONTEXT.inbox_dir, _THREAD_VAULT_CONTEXT.sidecar_dir = prev_ctx

    if not saved_rel:
        raise ValueError(f"No transcript or visual frames extracted from {os.path.basename(raw_path)}.")
    quarantined = saved_rel.startswith("inbox/")
    return {
        "status": "quarantined" if quarantined else "ingested",
        "category": category,
        "source_filename": os.path.basename(raw_path),
        "source_sha256": source_sha,
        "output_rel_path": f"0-raw/{saved_rel}",
        "output_filename": os.path.basename(saved_rel),
        "quarantined": quarantined,
        "dlp_flags": dlp_hits,
    }


def ingest_uploaded_media_bytes(
    raw_bytes: bytes,
    filename: str,
    *,
    brain_dir: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    """Safely stage and ingest uploaded audio, image/screenshot, or document bytes into `0-raw/`."""
    if not isinstance(raw_bytes, (bytes, bytearray)) or len(raw_bytes) == 0:
        raise ValueError("Uploaded media payload is empty.")
    if len(raw_bytes) > MAX_API_MEDIA_UPLOAD_BYTES:
        raise ValueError(
            f"Uploaded media payload ({len(raw_bytes)} bytes) exceeds {MAX_API_MEDIA_UPLOAD_BYTES} bytes limit."
        )
    base_name = os.path.basename(str(filename or "").strip().replace("\\", "/"))
    raw_stem, ext = os.path.splitext(base_name)
    ext = ext.lower()
    if ext not in SUPPORTED_INGEST_EXTS:
        raise ValueError(
            f"Unsupported media extension {ext!r}; supported: {sorted(SUPPORTED_INGEST_EXTS)}"
        )
    safe_stem = _sanitize_stem(raw_stem)
    media_category = (
        "video"
        if ext in VIDEO_ONLY_EXTS
        else ("audio" if ext in AUDIO_EXTS else ("image" if ext in IMAGE_EXTS else "document"))
    )

    resolved_brain = os.path.abspath(str(brain_dir)) if brain_dir is not None else os.path.dirname(os.path.abspath(RAW_DIR))
    target_raw = os.path.join(resolved_brain, "0-raw")
    target_inbox = os.path.join(target_raw, "inbox")
    target_archive = os.path.join(target_raw, "archive")
    target_sidecar = os.path.join(resolved_brain, ".sidecar")

    for check_p in (resolved_brain, target_raw, target_inbox, target_archive, target_sidecar):
        if os.path.lexists(check_p) and _is_reparse_or_link(check_p):
            raise PermissionError(f"Refusing to ingest media under symlinked directory: {check_p}")
    for mk_p in (target_raw, target_inbox, target_archive, target_sidecar):
        os.makedirs(mk_p, exist_ok=True)

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    staged_path = ""
    for attempt in range(64):
        cand_name = (
            f"{safe_stem}{ext}"
            if attempt == 0
            else f"{safe_stem}-{attempt + 1}{ext}"
        )
        cand_path = os.path.join(target_raw, cand_name)
        if os.path.lexists(cand_path):
            continue
        try:
            fd = os.open(cand_path, flags, 0o600)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(bytes(raw_bytes))
                    fh.flush()
                    os.fsync(fh.fileno())
            except Exception:
                if os.path.exists(cand_path):
                    try:
                        os.unlink(cand_path)
                    except OSError:
                        pass
                raise
            staged_path = cand_path
            break
        except FileExistsError:
            continue
    if not staged_path:
        raise RuntimeError("Could not allocate an exclusive staging filename in 0-raw/.")

    source_sha = hashlib.sha256(bytes(raw_bytes)).hexdigest()
    prev_ctx = (
        getattr(_THREAD_VAULT_CONTEXT, "raw_dir", None),
        getattr(_THREAD_VAULT_CONTEXT, "inbox_dir", None),
        getattr(_THREAD_VAULT_CONTEXT, "sidecar_dir", None),
    )
    _THREAD_VAULT_CONTEXT.raw_dir = target_raw
    _THREAD_VAULT_CONTEXT.inbox_dir = target_inbox
    _THREAD_VAULT_CONTEXT.sidecar_dir = target_sidecar
    _THREAD_VAULT_CONTEXT.last_dlp_flags = []
    try:
        saved_rel = process_file(staged_path)
        dlp_hits = list(getattr(_THREAD_VAULT_CONTEXT, "last_dlp_flags", []) or [])
    except PermissionError:
        raise
    except Exception as exc:
        raise ValueError(f"Media ingestion failed or deferred: {exc}") from exc
    finally:
        _THREAD_VAULT_CONTEXT.raw_dir, _THREAD_VAULT_CONTEXT.inbox_dir, _THREAD_VAULT_CONTEXT.sidecar_dir = prev_ctx
        if os.path.exists(staged_path):
            try:
                os.unlink(staged_path)
            except OSError:
                pass

    if not saved_rel:
        raise ValueError(f"No text or transcript could be extracted from {os.path.basename(staged_path)}.")

    quarantined = saved_rel.startswith("inbox/")
    if ext == ".webm" and "-video-" in os.path.basename(saved_rel):
        media_category = "video"
    return {
        "status": "quarantined" if quarantined else "ingested",
        "category": media_category,
        "source_filename": os.path.basename(staged_path),
        "source_sha256": source_sha,
        "output_rel_path": f"0-raw/{saved_rel}",
        "output_filename": os.path.basename(saved_rel),
        "quarantined": quarantined,
        "dlp_flags": dlp_hits,
    }


def process_file(filepath: str) -> str | None:
    ext = os.path.splitext(filepath)[1].lower()
    if ext in VIDEO_ONLY_EXTS or (ext == ".webm" and is_video_container(filepath)):
        return process_video(filepath)
    if ext in AUDIO_EXTS:
        return process_audio(filepath)
    if ext in IMAGE_EXTS:
        return process_image(filepath)
    if ext in DOC_EXTS:
        return process_document(filepath)
    print(f"  [skip] Unsupported type: {ext}")
    return None


def scan_raw_dir() -> list[str]:
    """Find and process all unhandled audio, image, and document files in 0-raw/."""
    ensure_dirs()
    if _is_reparse_or_link(RAW_DIR):
        raise PermissionError(f"Refusing to scan symlinked 0-raw directory: {RAW_DIR}")
    files = [
        f
        for f in sorted(os.listdir(RAW_DIR))
        if not f.startswith((".", "~"))
        and os.path.splitext(f)[1].lower() in SUPPORTED_INGEST_EXTS
    ]
    if not files:
        print("[*] No audio, image, or document files found in 0-raw/.")
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


def _apply_env_vault_override() -> None:
    """Honor runtime BRAIN_VAULT_DIR overrides when invoked via CLI in-process."""
    global RAW_DIR, INBOX_DIR, SIDECAR_DIR
    env_vault = (os.environ.get("BRAIN_VAULT_DIR") or "").strip()
    if not env_vault:
        return
    resolved_brain = os.path.abspath(env_vault)
    expected_raw = os.path.join(resolved_brain, "0-raw")
    if os.path.normcase(os.path.abspath(RAW_DIR)) != os.path.normcase(expected_raw):
        RAW_DIR = expected_raw
        INBOX_DIR = os.path.join(expected_raw, "inbox")
        SIDECAR_DIR = os.path.join(resolved_brain, ".sidecar")
        for d in (RAW_DIR, INBOX_DIR, SIDECAR_DIR, os.path.join(RAW_DIR, "archive")):
            os.makedirs(d, exist_ok=True)


def main(argv: list[str] | None = None) -> int:
    from scripts.config import configure_utf8_stdio

    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Multimodal and document file ingestion.")
    parser.add_argument("--file", action="append", default=[], help="Process a single file (repeatable).")
    parser.add_argument("paths", nargs="*", help="Optional file(s) or folder(s) to ingest into 0-raw/.")
    parser.add_argument("--background", action="store_true", help="Enqueue the files and return without ingesting them.")
    args = parser.parse_args(argv)
    _apply_env_vault_override()

    targets = [*args.file, *args.paths]
    if args.background:
        from scripts.job_queue import enqueue

        if not targets:
            print(enqueue("ingest", {"scan": True}))
            return 0
        for raw_target in targets:
            abs_target = os.path.abspath(raw_target)
            if _is_reparse_or_link(abs_target):
                print(f"Error: Refusing to ingest symlink or reparse point: {abs_target}", file=sys.stderr)
                return 1
            files: list[str] = []
            if os.path.isdir(abs_target):
                for entry in sorted(os.listdir(abs_target)):
                    if entry.startswith((".", "~")):
                        continue
                    sub = os.path.join(abs_target, entry)
                    if os.path.isfile(sub) and not _is_reparse_or_link(sub) and os.path.splitext(entry)[1].lower() in SUPPORTED_INGEST_EXTS:
                        files.append(sub)
            else:
                ext = os.path.splitext(abs_target)[1].lower()
                if ext not in SUPPORTED_INGEST_EXTS:
                    print(f"Error: Unsupported ingestion file extension: {ext}", file=sys.stderr)
                    return 1
                files.append(abs_target)
            for path in files:
                print(enqueue("ingest", {"path": path}))
        return 0
    print("=== MULTIMODAL & DOCUMENT INGESTION ===")
    try:
        if targets:
            for raw_target in targets:
                abs_target = os.path.abspath(raw_target)
                if _is_reparse_or_link(abs_target):
                    raise PermissionError(f"Refusing to ingest symlink or reparse point: {abs_target}")
                if os.path.isdir(abs_target):
                    for entry in sorted(os.listdir(abs_target)):
                        if entry.startswith((".", "~")):
                            continue
                        sub = os.path.join(abs_target, entry)
                        if os.path.isfile(sub) and not _is_reparse_or_link(sub) and os.path.splitext(entry)[1].lower() in SUPPORTED_INGEST_EXTS:
                            process_file(sub)
                else:
                    ext = os.path.splitext(abs_target)[1].lower()
                    if ext not in SUPPORTED_INGEST_EXTS:
                        raise ValueError(f"Unsupported ingestion file extension: {ext}")
                    process_file(abs_target)
        else:
            scan_raw_dir()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print("=== DONE ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
