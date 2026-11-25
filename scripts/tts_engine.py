# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["numpy", "onnxruntime"]
# ///
"""Offline CPU Text-to-Speech ("Mouth") & Two-Way Voice Loop via Kokoro-82M ONNX.

Synthesizes natural 24 kHz 16-bit mono PCM WAV audio locally using the SHA-256
pinned ``kokoro-tts`` ONNX slot (``onnx-community/Kokoro-82M-v1.0-ONNX`` INT8)
and pairs cleanly with ``faster-whisper`` STT in ``ingest_multimodal.py`` plus
grounded ``streaming_chat.chat_events`` for push-to-talk spoken conversation.
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


import argparse
import base64
from collections.abc import Callable
import gc
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from typing import Any
import wave

if __name__ in ("tts_engine", "scripts.tts_engine"):
    sys.modules.setdefault("tts_engine", sys.modules[__name__])
    sys.modules.setdefault("scripts.tts_engine", sys.modules[__name__])

from scripts.config import _is_reparse_or_link, security_state_dir  # noqa: E402

SAMPLE_RATE_HZ = 24000
DEFAULT_VOICE = "af_heart"
MAX_TTS_TEXT_CHARS = 8000
MAX_TTS_CHUNK_CHARS = 220
MAX_VOICE_AUDIO_BYTES = 16 * 1024 * 1024
ALLOWED_VOICE_EXTS = frozenset({".wav", ".webm", ".mp3", ".m4a", ".ogg"})

_CITATION_BRACKET_RE = re.compile(r"\s*\[(?:[VNPL]\d+|FRAME[^\]]*|\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?[^\]]*)\]")
_CODE_FENCE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_WIKILINK_RE = re.compile(r"\[\[([^|\]]+\|)?([^\]]+)\]\]")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HEADING_PREFIX_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")

_GRAPHEME_MAP = (
    ("tion", "ʃən"),
    ("sion", "ʒən"),
    ("ch", "ʧ"),
    ("sh", "ʃ"),
    ("th", "θ"),
    ("ng", "ŋ"),
    ("ph", "f"),
    ("qu", "kw"),
    ("ee", "i"),
    ("oo", "u"),
    ("ai", "eɪ"),
    ("ay", "eɪ"),
    ("oi", "ɔɪ"),
    ("oy", "ɔɪ"),
    ("ow", "aʊ"),
    ("ou", "aʊ"),
    ("er", "ər"),
    ("or", "ɔr"),
    ("ar", "ɑr"),
)

_KOKORO_CACHE_LOCK = threading.Lock()
_CACHED_KOKORO_KEY: tuple[str, str] | None = None
_CACHED_KOKORO_SESSION: Any = None
_TOKENIZER_CACHE_LOCK = threading.Lock()
_CACHED_TOKENIZER_KEY: tuple[str, int, int] | None = None
_CACHED_TOKENIZER_VOCAB: dict[str, int] | None = None


class TtsError(RuntimeError):
    """Raised on invalid TTS parameters or synthesis errors."""


class TtsUnavailableError(TtsError):
    """Raised when Kokoro ONNX weights or onnxruntime/numpy are unavailable."""


def strip_spoken_citations(text: str) -> str:
    """Clean Markdown, fenced code, and [V#]/[N#]/[P#]/[L#] brackets for natural speech prosody."""
    raw = str(text or "")
    raw = _CODE_FENCE_BLOCK_RE.sub(" Code block omitted. ", raw)
    raw = _CITATION_BRACKET_RE.sub("", raw)
    raw = _MD_LINK_RE.sub(r"\1", raw)
    raw = _WIKILINK_RE.sub(r"\2", raw)
    raw = _INLINE_CODE_RE.sub(r"\1", raw)
    raw = _HTML_TAG_RE.sub(" ", raw)
    raw = _HEADING_PREFIX_RE.sub("", raw)
    raw = raw.replace("**", "").replace("__", "").replace("*", "")
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _chunk_spoken_text(clean_text: str, *, max_chars: int = MAX_TTS_CHUNK_CHARS) -> list[str]:
    if not clean_text:
        return []
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(clean_text) if p and p.strip()]
    chunks: list[str] = []
    cur = ""
    for part in parts:
        if len(part) > max_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            words = part.split()
            wbuf = ""
            for w in words:
                cand = f"{wbuf} {w}".strip() if wbuf else w
                if len(cand) <= max_chars:
                    wbuf = cand
                else:
                    if wbuf:
                        chunks.append(wbuf)
                    wbuf = w[:max_chars]
            if wbuf:
                chunks.append(wbuf)
            continue
        cand = f"{cur} {part}".strip() if cur else part
        if len(cand) <= max_chars:
            cur = cand
        else:
            if cur:
                chunks.append(cur)
            cur = part
    if cur:
        chunks.append(cur)
    return chunks


def _text_to_pseudo_phonemes(text: str) -> str:
    """Deterministic zero-binary English grapheme approximation into Kokoro IPA vocabulary."""
    out_words: list[str] = []
    for tok in re.findall(r"[A-Za-z0-9']+|[,.;:!?—-]", text.lower()):
        if not tok[0].isalnum():
            out_words.append(tok)
            continue
        w = tok
        for src, dst in _GRAPHEME_MAP:
            w = w.replace(src, dst)
        out_words.append(w)
    return " ".join(out_words)


def _load_kokoro_vocab(tokenizer_json_path: str | Path) -> dict[str, int]:
    """Load and cache Kokoro tokenizer vocabulary keyed by ``(resolved_path, mtime_ns, size)``."""
    global _CACHED_TOKENIZER_KEY, _CACHED_TOKENIZER_VOCAB
    tok_path = Path(tokenizer_json_path)
    if os.path.lexists(str(tok_path)) and _is_reparse_or_link(str(tok_path)):
        raise PermissionError(f"Refusing symlinked Kokoro tokenizer file: {tok_path}")
    st = tok_path.stat()
    key = (str(tok_path.resolve()), int(st.st_mtime_ns), int(st.st_size))
    with _TOKENIZER_CACHE_LOCK:
        if _CACHED_TOKENIZER_KEY == key and _CACHED_TOKENIZER_VOCAB is not None:
            return _CACHED_TOKENIZER_VOCAB
    tok_doc = json.loads(tok_path.read_text(encoding="utf-8"))
    vocab: dict[str, int] = {
        str(k): int(v)
        for k, v in ((tok_doc.get("model") or {}).get("vocab") or {}).items()
        if v is not None
    }
    if not vocab:
        raise TtsError(f"Missing model.vocab inside Kokoro tokenizer: {tok_path}")
    with _TOKENIZER_CACHE_LOCK:
        _CACHED_TOKENIZER_KEY = key
        _CACHED_TOKENIZER_VOCAB = vocab
    return vocab


def text_to_kokoro_token_ids(text: str, tokenizer_json_path: str | Path) -> list[int]:
    """Encode cleaned text to bounded Kokoro phoneme token IDs ([0, ..., 0], max 510 interior)."""
    vocab = _load_kokoro_vocab(tokenizer_json_path)

    phoneme_str = _text_to_pseudo_phonemes(text)
    ids: list[int] = [int(vocab[ch]) for ch in phoneme_str if ch in vocab]
    if not ids:
        fallback_tok = vocab.get(" ") or vocab.get("a") or next(iter(vocab.values()))
        ids = [int(fallback_tok)]
    ids = ids[:508]
    return [0, *ids, 0]


def _load_voice_style_vector(voice_bin_path: str | Path, token_count: int):
    import numpy as np  # type: ignore

    raw = np.fromfile(str(voice_bin_path), dtype=np.float32)
    if raw.size < 256:
        raise TtsError(f"Voice style vector binary is too small: {voice_bin_path}")
    packs = raw.reshape(-1, 1, 256)
    idx = max(0, min(int(token_count), packs.shape[0] - 1))
    return packs[idx]


def clear_tts_cache() -> None:
    global _CACHED_KOKORO_KEY, _CACHED_KOKORO_SESSION, _CACHED_TOKENIZER_KEY, _CACHED_TOKENIZER_VOCAB
    with _KOKORO_CACHE_LOCK:
        _CACHED_KOKORO_KEY = None
        _CACHED_KOKORO_SESSION = None
    with _TOKENIZER_CACHE_LOCK:
        _CACHED_TOKENIZER_KEY = None
        _CACHED_TOKENIZER_VOCAB = None
    gc.collect()


def smoke_test_kokoro_slot(slot_dir: Path) -> dict[str, Any]:
    """Verify Kokoro ONNX model + voice vector execute on CPU ORT returning valid audio samples."""
    import numpy as np  # type: ignore
    import onnxruntime as ort  # type: ignore

    model_fp = slot_dir / "model.onnx"
    tok_fp = slot_dir / "tokenizer.json"
    voice_fp = slot_dir / f"{DEFAULT_VOICE}.bin"
    if not voice_fp.is_file():
        bins = sorted(slot_dir.glob("*.bin"))
        if not bins:
            raise TtsUnavailableError("Kokoro slot is missing *.bin voice embedding pack.")
        voice_fp = bins[0]

    sess_opts = ort.SessionOptions()
    sess_opts.inter_op_num_threads = 1
    sess_opts.intra_op_num_threads = max(1, min(4, int(os.cpu_count() or 2)))
    sess = ort.InferenceSession(str(model_fp), sess_options=sess_opts, providers=["CPUExecutionProvider"])
    try:
        tokens = text_to_kokoro_token_ids("Ready.", tok_fp)
        input_ids = np.array([tokens], dtype=np.int64)
        style = _load_voice_style_vector(voice_fp, len(tokens) - 2).astype(np.float32)
        speed_arr = np.array([1.0], dtype=np.float32)
        in_names = {inp.name for inp in sess.get_inputs()}
        feed: dict[str, Any] = {}
        feed["input_ids" if "input_ids" in in_names else "tokens"] = input_ids
        feed["style"] = style
        feed["speed"] = speed_arr
        outputs = sess.run(None, feed)
        wave_arr = np.asarray(outputs[0], dtype=np.float32).reshape(-1)
        if wave_arr.size < 100 or not np.all(np.isfinite(wave_arr)):
            raise TtsError("Kokoro ONNX smoke test produced empty or non-finite audio.")
        return {
            "sample_rate": SAMPLE_RATE_HZ,
            "smoke_samples": int(wave_arr.size),
            "voice": voice_fp.stem,
        }
    finally:
        del sess
        gc.collect()


def encode_pcm16_wav(samples_float32, *, sample_rate: int = SAMPLE_RATE_HZ) -> bytes:
    import numpy as np  # type: ignore

    arr = np.asarray(samples_float32, dtype=np.float32).reshape(-1)
    clipped = np.clip(arr, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def tts_status(*, state_dir: str | Path | None = None, slot_dir: str | Path | None = None) -> dict[str, Any]:
    from scripts.onnx_models import verify_slot_installation

    info = verify_slot_installation("kokoro-tts", state_dir=state_dir, slot_dir=slot_dir, run_inference=False)
    ort_ok = False
    np_ok = False
    try:
        import onnxruntime  # noqa: F401

        ort_ok = True
    except ImportError:
        ort_ok = False
    try:
        import numpy  # noqa: F401

        np_ok = True
    except ImportError:
        np_ok = False

    voices: list[str] = []
    if info.get("directory") and Path(str(info["directory"])).is_dir():
        voices = sorted(p.stem for p in Path(str(info["directory"])).glob("*.bin"))
    return {
        "slot": "kokoro-tts",
        "available": bool(info.get("verified") and ort_ok and np_ok),
        "weights_verified": bool(info.get("verified")),
        "installed": bool(info.get("installed")),
        "onnxruntime_ready": ort_ok,
        "numpy_ready": np_ok,
        "model_id": info.get("model_id") or "onnx-community/Kokoro-82M-v1.0-ONNX@int8",
        "voices": voices or [DEFAULT_VOICE],
        "sample_rate": SAMPLE_RATE_HZ,
        "issues": info.get("issues") or [],
        "remediation": (
            ""
            if info.get("verified")
            else "Run 'second-brain models pull kokoro-tts' to install SHA-256 verified Kokoro-82M INT8 ONNX weights."
        ),
    }


def _get_or_open_session(slot_dir: Path):
    import onnxruntime as ort  # type: ignore

    global _CACHED_KOKORO_KEY, _CACHED_KOKORO_SESSION
    model_fp = slot_dir / "model.onnx"
    key = (str(model_fp.resolve()), str(model_fp.stat().st_mtime_ns))
    with _KOKORO_CACHE_LOCK:
        if _CACHED_KOKORO_KEY == key and _CACHED_KOKORO_SESSION is not None:
            return _CACHED_KOKORO_SESSION
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = max(1, min(8, int(os.cpu_count() or 4)))
        sess = ort.InferenceSession(str(model_fp), sess_options=opts, providers=["CPUExecutionProvider"])
        _CACHED_KOKORO_KEY = key
        _CACHED_KOKORO_SESSION = sess
        return sess


def synthesize_wav_bytes(
    text: str,
    *,
    voice: str = DEFAULT_VOICE,
    speed: float = 1.0,
    state_dir: str | Path | None = None,
    slot_dir: str | Path | None = None,
    synthesizer_fn: Callable[[str, str, float], bytes] | None = None,
) -> dict[str, Any]:
    """Synthesize cleaned answer text to 24 kHz mono WAV bytes via verified Kokoro-82M ONNX."""
    clean_text = strip_spoken_citations(text)[:MAX_TTS_TEXT_CHARS]
    if not clean_text:
        raise TtsError("No speakable text remains after cleaning citations and formatting.")
    clean_voice = re.sub(r"[^a-zA-Z0-9_-]", "", str(voice or DEFAULT_VOICE)) or DEFAULT_VOICE
    eff_speed = max(0.5, min(2.0, float(speed)))

    t0 = time.perf_counter()
    if synthesizer_fn is not None:
        wav_bytes = bytes(synthesizer_fn(clean_text, clean_voice, eff_speed))
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            n_frames = wf.getnframes()
            sr = wf.getframerate() or SAMPLE_RATE_HZ
            dur_s = round(n_frames / float(sr), 3)
        elapsed_s = max(0.001, time.perf_counter() - t0)
        return {
            "status": "ok",
            "wav_bytes": wav_bytes,
            "mime_type": "audio/wav",
            "sample_rate": sr,
            "duration_seconds": dur_s,
            "rtf": round(elapsed_s / max(0.05, dur_s), 3),
            "clean_text": clean_text,
            "voice": clean_voice,
            "model_id": "injected_tts_synthesizer",
        }

    from scripts.onnx_models import verify_slot_installation

    info = verify_slot_installation("kokoro-tts", state_dir=state_dir, slot_dir=slot_dir, run_inference=False)
    if not info.get("verified"):
        raise TtsUnavailableError(
            "Kokoro-82M ONNX weights are missing or unverified. Run 'second-brain models pull kokoro-tts'."
        )
    try:
        import numpy as np  # type: ignore
        import onnxruntime  # noqa: F401
    except ImportError as exc:
        raise TtsUnavailableError("Kokoro TTS requires numpy and onnxruntime ('second-brain[reranker]').") from exc

    resolved_dir = Path(str(info["directory"]))
    if _is_reparse_or_link(str(resolved_dir)):
        raise PermissionError(f"Refusing symlinked Kokoro model directory: {resolved_dir}")
    tok_fp = resolved_dir / "tokenizer.json"
    voice_fp = resolved_dir / f"{clean_voice}.bin"
    if not voice_fp.is_file():
        voice_fp = resolved_dir / f"{DEFAULT_VOICE}.bin"
        if not voice_fp.is_file():
            bins = sorted(resolved_dir.glob("*.bin"))
            if not bins:
                raise TtsUnavailableError("No voice style pack (*.bin) installed in Kokoro slot.")
            voice_fp = bins[0]

    sess = _get_or_open_session(resolved_dir)
    in_names = {inp.name for inp in sess.get_inputs()}
    id_key = "input_ids" if "input_ids" in in_names else "tokens"
    speed_arr = np.array([eff_speed], dtype=np.float32)

    chunks = _chunk_spoken_text(clean_text)
    segments = []
    pause = np.zeros(int(SAMPLE_RATE_HZ * 0.14), dtype=np.float32)
    for idx, ch in enumerate(chunks):
        tokens = text_to_kokoro_token_ids(ch, tok_fp)
        input_ids = np.array([tokens], dtype=np.int64)
        style = _load_voice_style_vector(voice_fp, len(tokens) - 2).astype(np.float32)
        outputs = sess.run(None, {id_key: input_ids, "style": style, "speed": speed_arr})
        wave_arr = np.asarray(outputs[0], dtype=np.float32).reshape(-1)
        if wave_arr.size:
            segments.append(wave_arr)
            if idx + 1 < len(chunks):
                segments.append(pause)

    if not segments:
        raise TtsError("Kokoro inference produced empty waveform output.")
    full_wave = np.concatenate(segments)
    wav_bytes = encode_pcm16_wav(full_wave, sample_rate=SAMPLE_RATE_HZ)
    dur_s = round(len(full_wave) / float(SAMPLE_RATE_HZ), 3)
    elapsed_s = max(0.001, time.perf_counter() - t0)
    return {
        "status": "ok",
        "wav_bytes": wav_bytes,
        "mime_type": "audio/wav",
        "sample_rate": SAMPLE_RATE_HZ,
        "duration_seconds": dur_s,
        "rtf": round(elapsed_s / max(0.05, dur_s), 3),
        "clean_text": clean_text,
        "voice": voice_fp.stem,
        "model_id": info.get("model_id") or "onnx-community/Kokoro-82M-v1.0-ONNX@int8",
    }


def voice_conversation_turn(
    audio_bytes: bytes,
    filename: str = "voice-turn.wav",
    *,
    brain_dir: str | Path | None = None,
    policy_path: Path | None = None,
    state_dir: str | Path | None = None,
    mode: str = "stream",
    host_id: str = "ollama_local",
    project_scope: str | None = None,
    voice: str = DEFAULT_VOICE,
    speed: float = 1.0,
    transcriber: Callable[[str], str] | None = None,
    stream_fn: Any = None,
    synthesizer_fn: Callable[[str, str, float], bytes] | None = None,
) -> dict[str, Any]:
    """End-to-end offline two-way spoken turn: STT -> grounded vault chat -> Kokoro WAV audio."""
    if not isinstance(audio_bytes, (bytes, bytearray)) or len(audio_bytes) == 0:
        raise TtsError("Voice turn audio payload is empty.")
    if len(audio_bytes) > MAX_VOICE_AUDIO_BYTES:
        raise TtsError(f"Voice turn audio exceeds {MAX_VOICE_AUDIO_BYTES} byte limit.")
    ext = Path(str(filename or "voice.wav")).suffix.lower() or ".wav"
    if ext not in ALLOWED_VOICE_EXTS:
        raise TtsError(f"Unsupported voice recording extension {ext!r}; allowed: {sorted(ALLOWED_VOICE_EXTS)}")

    with tempfile.TemporaryDirectory(prefix="sb_voice_") as td:
        audio_path = Path(td) / f"input{ext}"
        audio_path.write_bytes(bytes(audio_bytes))
        if transcriber is not None:
            transcript = str(transcriber(str(audio_path)) or "").strip()
        else:
            from scripts.ingest_multimodal import transcribe_audio

            transcript = str(transcribe_audio(str(audio_path)) or "").strip()
    if not transcript:
        raise TtsError("No speech detected in uploaded voice recording.")

    from scripts.streaming_chat import ChatPaths, chat_events

    resolved_brain = str(Path(brain_dir).resolve()) if brain_dir is not None else None
    cp_kw: dict[str, Any] = {
        "include_quarantined_inbox": False,
        "persist_threads_allowed": True,
    }
    if resolved_brain:
        cp_kw.update(
            {
                "brain_dir": resolved_brain,
                "atoms_dir": os.path.join(resolved_brain, "2-atoms"),
                "lexical_index_path": os.path.join(resolved_brain, ".sidecar", "lexical.sqlite3"),
                "chroma_path": os.path.join(resolved_brain, ".chroma"),
                "telemetry_db_path": os.path.join(resolved_brain, ".sidecar", "telemetry.sqlite3"),
            }
        )
    if policy_path is not None:
        cp_kw["policy_path"] = Path(policy_path)
    if state_dir is not None:
        cp_kw["security_dir"] = Path(state_dir)
    cp = ChatPaths(**cp_kw)

    sources: list[dict[str, Any]] = []
    grounding: dict[str, Any] = {}
    answer_chunks: list[str] = []
    verified_text = ""
    err_msg = ""
    for evt in chat_events(
        transcript,
        mode=mode,
        paths=cp,
        project_scope=project_scope,
        host_id=host_id,
        stream_fn=stream_fn,
    ):
        etype = evt.get("type")
        if etype == "context":
            sources = list(evt.get("sources") or [])
        elif etype == "token":
            answer_chunks.append(str(evt.get("text") or evt.get("token") or ""))
        elif etype == "verified_answer":
            verified_text = str(evt.get("text") or evt.get("answer") or "")
        elif etype == "abstain":
            verified_text = str(evt.get("text") or evt.get("reason") or "")
        elif etype == "grounding":
            grounding = dict(evt)
        elif etype == "error":
            err_msg = str(evt.get("error") or "Chat error")

    answer_text = ("".join(answer_chunks) or verified_text or err_msg or "No matching memory found.").strip()
    tts_result: dict[str, Any] | None = None
    tts_warning = ""
    try:
        tts_result = synthesize_wav_bytes(
            answer_text,
            voice=voice,
            speed=speed,
            state_dir=state_dir,
            synthesizer_fn=synthesizer_fn,
        )
    except TtsUnavailableError as exc:
        tts_warning = str(exc)

    return {
        "status": "ok",
        "transcript": transcript,
        "answer_text": answer_text,
        "sources": sources,
        "grounding": grounding,
        "tts_available": tts_result is not None,
        "tts_warning": tts_warning,
        "audio_b64": base64.b64encode(tts_result["wav_bytes"]).decode("ascii") if tts_result else None,
        "mime_type": "audio/wav" if tts_result else None,
        "sample_rate": tts_result["sample_rate"] if tts_result else None,
        "duration_seconds": tts_result["duration_seconds"] if tts_result else 0.0,
        "clean_spoken_text": strip_spoken_citations(answer_text),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_stat = sub.add_parser("status", help="Inspect Kokoro-82M ONNX TTS availability")
    p_stat.add_argument("--json", action="store_true")

    p_spk = sub.add_parser("speak", help="Synthesize WAV audio from text using Kokoro-82M ONNX")
    p_spk.add_argument("text", help="Text to speak (citations are automatically stripped for prosody)")
    p_spk.add_argument("--out", default="", help="Destination .wav path")
    p_spk.add_argument("--voice", default=DEFAULT_VOICE)
    p_spk.add_argument("--speed", type=float, default=1.0)
    p_spk.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps(tts_status(), indent=2, ensure_ascii=False))
        return 0

    res = synthesize_wav_bytes(args.text, voice=args.voice, speed=args.speed)
    if args.out:
        out_p = Path(args.out).resolve()
        out_p.write_bytes(res["wav_bytes"])
    payload = {k: v for k, v in res.items() if k != "wav_bytes"}
    payload["out_path"] = str(Path(args.out).resolve()) if args.out else None
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
