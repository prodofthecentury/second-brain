# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Secure local video keyframe, audio-track, and chronological timeline extraction (.mp4/.mov/.mkv/.webm).

Extracts scene-change keyframes (deduplicated via 64-bit perceptual dHash) and a 16 kHz mono WAV
audio track via argument-array `ffmpeg`/`ffprobe` subprocess invocations (no shell interpolation,
symlink/reparse-point rejection, strict byte/duration/frame bounds). Optionally falls back to
PyAV (`av`) when installed.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Callable

try:
    from scripts.config import VIDEO_EXTS, _is_reparse_or_link
except ImportError:  # pragma: no cover
    VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
    _is_reparse_or_link = lambda p: os.path.islink(p)  # noqa: E731

MAX_VIDEO_BYTES = 512 * 1024 * 1024
MAX_VIDEO_KEYFRAMES = 24
DHASH_DEDUP_THRESHOLD = 6

_SHOWINFO_PTS_RE = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")
_TIMESTAMP_LINE_RE = re.compile(r"^\[(\d{2}:\d{2}(?::\d{2})?\.\d{3})\s*->\s*([^\]]+)\]\s*(.*)$")


def format_timestamp_hms(seconds: float) -> str:
    """Format seconds into `HH:MM:SS.mmm`."""
    total_ms = max(0, int(round(float(seconds) * 1000.0)))
    hrs, rem = divmod(total_ms, 3_600_000)
    mins, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hrs:02d}:{mins:02d}:{secs:02d}.{ms:03d}"


def resolve_ffmpeg_tools() -> tuple[str | None, str | None]:
    """Locate `ffmpeg` and `ffprobe` binaries safely on PATH or explicit env overrides."""
    ffmpeg_env = (os.environ.get("FFMPEG_BIN") or os.environ.get("IMAGEIO_FFMPEG_EXE") or "").strip()
    ffprobe_env = (os.environ.get("FFPROBE_BIN") or "").strip()

    ffmpeg_bin = ffmpeg_env if (ffmpeg_env and os.path.isfile(ffmpeg_env)) else shutil.which("ffmpeg")
    ffprobe_bin = ffprobe_env if (ffprobe_env and os.path.isfile(ffprobe_env)) else shutil.which("ffprobe")
    if not ffprobe_bin and ffmpeg_bin:
        cand = str(Path(ffmpeg_bin).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe"))
        if os.path.isfile(cand):
            ffprobe_bin = cand
    return ffmpeg_bin, ffprobe_bin


_PLAYLIST_OR_TEXT_PREFIXES: tuple[bytes, ...] = (
    b"#extm3u",
    b"ffconcat",
    b"<mpd",
    b"<?xml",
    b"<html",
    b"<!doctype",
    b"http://",
    b"https://",
    b"file:",
    b"ftp://",
    b"rtsp://",
    b"concat:",
    b"subfile:",
    b"crypto:",
    b"data:",
)
_ISOBMFF_MOV_ATOMS = frozenset({b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip"})


def _validate_video_container_magic(filepath: str, head: bytes) -> None:
    """Reject text/playlist SSRF/LFI payloads and verify a genuine binary video container signature."""
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    for prefix in _PLAYLIST_OR_TEXT_PREFIXES:
        if stripped.startswith(prefix):
            raise ValueError(
                f"Refusing playlist or text-based file disguised as video ({prefix.decode('ascii', errors='ignore')}): {filepath}"
            )
    head_low = head[:64].lower()
    if b"#extm3u" in head_low or b"ffconcat" in head_low or b"<mpd" in head_low:
        raise ValueError(f"Refusing playlist/manifest header inside video file: {filepath}")

    ext = os.path.splitext(filepath)[1].lower()
    is_mp4_ftyp = len(head) >= 8 and head[4:8] == b"ftyp"
    is_mov_atom = len(head) >= 8 and head[4:8] in _ISOBMFF_MOV_ATOMS
    is_ebml = head.startswith(b"\x1a\x45\xdf\xa3")
    is_avi = len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"AVI "

    if ext in {".mp4", ".m4v"}:
        valid = is_mp4_ftyp
    elif ext == ".mov":
        valid = is_mov_atom
    elif ext in {".mkv", ".webm"}:
        valid = is_ebml
    elif ext == ".avi":
        valid = is_avi
    else:
        valid = is_mp4_ftyp or is_ebml or is_avi

    if not valid:
        raise ValueError(
            f"Video file lacks a valid binary container signature for '{ext or 'unknown'}': {filepath}"
        )


def _assert_safe_video_file(filepath: str, *, max_bytes: int = MAX_VIDEO_BYTES) -> int:
    raw = str(filepath or "")
    if not raw or "\x00" in raw:
        raise ValueError(f"Invalid video file path: {filepath!r}")
    base_name = os.path.basename(raw)
    if not base_name or base_name.startswith("-"):
        raise ValueError(f"Refusing unsafe video filename starting with '-': {filepath!r}")
    parent_dir = os.path.dirname(raw)
    if parent_dir and os.path.lexists(parent_dir) and _is_reparse_or_link(parent_dir):
        raise PermissionError(f"Refusing to ingest video from symlinked parent directory: {parent_dir}")
    if os.path.lexists(raw) and _is_reparse_or_link(raw):
        raise PermissionError(f"Refusing to ingest symlink or reparse-point video: {raw}")
    if not os.path.isfile(raw):
        raise FileNotFoundError(f"Video file not found: {raw}")
    size = os.path.getsize(raw)
    if size <= 0:
        raise ValueError(f"Video file is empty: {raw}")
    if size > max_bytes:
        raise ValueError(f"Video file ({size} bytes) exceeds maximum limit ({max_bytes} bytes).")
    with open(raw, "rb") as fh:
        head = fh.read(512)
    _validate_video_container_magic(raw, head)
    return size


def probe_video_streams(
    filepath: str,
    *,
    max_bytes: int = MAX_VIDEO_BYTES,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Inspect container video/audio streams using `ffprobe` (or optional PyAV fallback)."""
    _assert_safe_video_file(filepath, max_bytes=max_bytes)
    ext = os.path.splitext(filepath)[1].lower()
    ffmpeg_bin, ffprobe_bin = resolve_ffmpeg_tools()

    if ffprobe_bin:
        cmd = [
            ffprobe_bin,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-show_entries",
            "stream=codec_type,codec_name,width,height:format=duration",
            "-of",
            "json",
            filepath,
        ]
        proc = runner(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            payload = json.loads(proc.stdout)
            streams = payload.get("streams") or []
            video_streams = [s for s in streams if s.get("codec_type") == "video"]
            audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
            fmt = payload.get("format") or {}
            try:
                duration_s = float(fmt.get("duration") or 0.0)
            except (TypeError, ValueError):
                duration_s = 0.0
            return {
                "has_video": bool(video_streams),
                "has_audio": bool(audio_streams),
                "duration_seconds": max(0.0, duration_s),
                "width": int((video_streams[0].get("width") or 0)) if video_streams else 0,
                "height": int((video_streams[0].get("height") or 0)) if video_streams else 0,
                "backend": "ffprobe",
                "tools_available": True,
            }

    try:
        import av  # type: ignore

        with av.open(filepath) as container:
            v_streams = list(container.streams.video)
            a_streams = list(container.streams.audio)
            duration_s = float(container.duration / 1_000_000.0) if container.duration else 0.0
            return {
                "has_video": bool(v_streams),
                "has_audio": bool(a_streams),
                "duration_seconds": max(0.0, duration_s),
                "width": int(v_streams[0].width or 0) if v_streams else 0,
                "height": int(v_streams[0].height or 0) if v_streams else 0,
                "backend": "pyav",
                "tools_available": True,
            }
    except ImportError:
        pass

    return {
        "has_video": ext in {".mp4", ".mov", ".mkv"},
        "has_audio": ext == ".webm",
        "duration_seconds": 0.0,
        "width": 0,
        "height": 0,
        "backend": "extension_heuristic",
        "tools_available": bool(ffmpeg_bin),
    }


def is_video_container(
    filepath: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    """Return True if `filepath` is `.mp4/.mov/.mkv` or a `.webm` container with a video stream."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in {".mp4", ".mov", ".mkv"}:
        return True
    if ext == ".webm":
        try:
            info = probe_video_streams(filepath, runner=runner)
            return bool(info.get("has_video"))
        except Exception:
            return False
    return False


def compute_dhash64(image_path: str) -> int:
    """Compute a 64-bit gradient perceptual difference hash (`9x8` grayscale)."""
    from PIL import Image, ImageOps

    with Image.open(image_path) as raw:
        img = ImageOps.exif_transpose(raw).convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        get_data = getattr(img, "get_flattened_data", None) or img.getdata
        pixels = list(get_data())
    bits = 0
    for row in range(8):
        offset = row * 9
        for col in range(8):
            if pixels[offset + col] > pixels[offset + col + 1]:
                bits |= 1 << (row * 8 + col)
    return bits


def hamming_distance64(a: int, b: int) -> int:
    return (int(a) ^ int(b)).bit_count()


def extract_video_keyframes_and_audio(
    filepath: str,
    work_dir: str,
    *,
    max_keyframes: int = MAX_VIDEO_KEYFRAMES,
    dhash_threshold: int = DHASH_DEDUP_THRESHOLD,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Demux 16 kHz mono WAV audio and extract deduplicated scene-change PNG keyframes."""
    _assert_safe_video_file(filepath)
    if os.path.lexists(work_dir) and _is_reparse_or_link(work_dir):
        raise PermissionError(f"Refusing to write video staging output into symlink: {work_dir}")
    os.makedirs(work_dir, exist_ok=True)

    probe = probe_video_streams(filepath, runner=runner)
    ffmpeg_bin, _ = resolve_ffmpeg_tools()

    wav_path: str | None = None
    extracted_frames: list[dict[str, Any]] = []

    if ffmpeg_bin:
        if probe.get("has_audio", True):
            cand_wav = os.path.join(work_dir, "audio_track.wav")
            audio_proc = runner(
                [
                    ffmpeg_bin,
                    "-y",
                    "-protocol_whitelist",
                    "file",
                    "-i",
                    filepath,
                    "-vn",
                    "-acodec",
                    "pcm_s16le",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    cand_wav,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                check=False,
            )
            if audio_proc.returncode == 0 and os.path.isfile(cand_wav) and os.path.getsize(cand_wav) > 44:
                wav_path = cand_wav

        if probe.get("has_video", True):
            frame_pattern = os.path.join(work_dir, "keyframe_%04d.png")
            vf_filter = "select='gt(scene\\,0.28)+isnan(prev_selected_t)+gte(t-prev_selected_t\\,30)',showinfo"
            vid_proc = runner(
                [
                    ffmpeg_bin,
                    "-y",
                    "-protocol_whitelist",
                    "file",
                    "-i",
                    filepath,
                    "-vf",
                    vf_filter,
                    "-vsync",
                    "vfr",
                    "-frames:v",
                    str(max(1, max_keyframes * 2)),
                    frame_pattern,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                check=False,
            )
            pts_matches = [float(m) for m in _SHOWINFO_PTS_RE.findall(vid_proc.stderr or "")]
            raw_frames = sorted(
                os.path.join(work_dir, fn)
                for fn in os.listdir(work_dir)
                if fn.startswith("keyframe_") and fn.endswith(".png")
            )
            extracted_frames = _deduplicate_frame_files(
                raw_frames,
                pts_times=pts_matches,
                duration_seconds=float(probe.get("duration_seconds") or 0.0),
                max_keyframes=max_keyframes,
                dhash_threshold=dhash_threshold,
            )
    else:
        extracted_frames = _extract_with_pyav_fallback(
            filepath,
            work_dir,
            max_keyframes=max_keyframes,
            dhash_threshold=dhash_threshold,
        )

    if not ffmpeg_bin and not probe.get("tools_available") and not extracted_frames and wav_path is None:
        raise RuntimeError(
            "Video extraction requires `ffmpeg`/`ffprobe` on PATH (or optional PyAV `av` package)."
        )

    return {
        "probe": probe,
        "wav_path": wav_path,
        "keyframes": extracted_frames,
    }


def _deduplicate_frame_files(
    frame_paths: list[str],
    *,
    pts_times: list[float],
    duration_seconds: float,
    max_keyframes: int,
    dhash_threshold: int,
) -> list[dict[str, Any]]:
    has_pil = True
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        has_pil = False

    kept: list[dict[str, Any]] = []
    prev_hashes: list[int] = []
    total = max(1, len(frame_paths))
    for idx, fpath in enumerate(frame_paths):
        if len(kept) >= max_keyframes:
            break
        if not os.path.isfile(fpath) or os.path.getsize(fpath) == 0:
            continue
        if has_pil:
            try:
                cur_hash = compute_dhash64(fpath)
            except Exception:
                continue
            if any(hamming_distance64(cur_hash, old_h) <= dhash_threshold for old_h in prev_hashes):
                continue
            prev_hashes.append(cur_hash)
        ts = pts_times[idx] if idx < len(pts_times) else (duration_seconds * idx / total)
        with open(fpath, "rb") as fh:
            fsha = hashlib.sha256(fh.read()).hexdigest()
        kept.append(
            {
                "index": len(kept) + 1,
                "path": fpath,
                "timestamp_seconds": float(ts),
                "timestamp_label": format_timestamp_hms(ts),
                "sha256": fsha,
            }
        )
    return kept


def _extract_with_pyav_fallback(
    filepath: str,
    work_dir: str,
    *,
    max_keyframes: int,
    dhash_threshold: int,
) -> list[dict[str, Any]]:
    try:
        import av  # type: ignore
    except ImportError:
        return []

    raw_paths: list[str] = []
    pts_times: list[float] = []
    with av.open(filepath) as container:
        if not container.streams.video:
            return []
        stream = container.streams.video[0]
        stream.codec_context.skip_frame = "NONKEY"
        for i, frame in enumerate(container.decode(stream)):
            if len(raw_paths) >= max_keyframes * 2:
                break
            out_path = os.path.join(work_dir, f"pyav_keyframe_{i:04d}.png")
            frame.to_image().save(out_path)
            raw_paths.append(out_path)
            pts_times.append(float(frame.time) if frame.time is not None else float(i))
    return _deduplicate_frame_files(
        raw_paths,
        pts_times=pts_times,
        duration_seconds=float(pts_times[-1]) if pts_times else 0.0,
        max_keyframes=max_keyframes,
        dhash_threshold=dhash_threshold,
    )


def _parse_transcript_start_seconds(ts_label: str) -> float:
    parts = [p for p in ts_label.strip().split(":") if p]
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60.0 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600.0 + int(parts[1]) * 60.0 + float(parts[2])
    except ValueError:
        pass
    return 0.0


def build_video_timeline_markdown(
    filename: str,
    *,
    transcript: str,
    frame_notes: list[dict[str, Any]],
) -> str:
    """Merge chronological visual keyframes and ASR audio transcript lines into `[TIMELINE]` Markdown."""
    events: list[tuple[float, int, str]] = []

    for note in frame_notes:
        ts_sec = float(note.get("timestamp_seconds") or 0.0)
        ts_lbl = str(note.get("timestamp_label") or format_timestamp_hms(ts_sec))
        sha = str(note.get("sha256") or "unavailable")
        desc = str(note.get("description") or "").strip() or "(No visual text/objects described.)"
        block = f"[FRAME t={ts_lbl} sha256={sha}]\n{desc}"
        events.append((ts_sec, 0, block))

    for raw_line in (transcript or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _TIMESTAMP_LINE_RE.match(line)
        ts_sec = _parse_transcript_start_seconds(m.group(1)) if m else 0.0
        events.append((ts_sec, 1, line))

    events.sort(key=lambda item: (item[0], item[1]))
    timeline_text = "\n\n".join(entry[2] for entry in events) if events else "(No video transcript or keyframe notes extracted.)"
    return (
        f"# Video capture timeline: {filename}\n\n"
        f"[TIMELINE]\n\n"
        f"{timeline_text}\n"
    )
