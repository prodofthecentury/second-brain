# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Capability lines for ``second-brain doctor``.

Missing optional tools are warnings. They are not failures of the core vault.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys


def _line(status: str, area: str, detail: str) -> tuple[str, str, str]:
    return status, area, detail


def capability_checks() -> list[tuple[str, str, str]]:
    checks: list[tuple[str, str, str]] = []
    ffmpeg = shutil.which("ffmpeg")
    checks.append(
        _line("OK", "ffmpeg", ffmpeg) if ffmpeg else _line("WARN", "ffmpeg", "ffmpeg is not on PATH; keyframe extraction cannot run")
    )
    espeak = shutil.which("espeak-ng") or shutil.which("espeak")
    checks.append(
        _line("OK", "speech", espeak or "kokoro slot may still be present")
        if espeak
        else _line("WARN", "speech", "espeak-ng is not on PATH; Kokoro weights are a separate install")
    )
    lean = shutil.which("lean")
    checks.append(_line("OK", "lean", lean) if lean else _line("WARN", "lean", "lean is not on PATH"))
    for name in ("cloudflared", "tailscale"):
        path = shutil.which(name)
        checks.append(
            _line("OK", name, path)
            if path
            else _line("WARN", name, f"{name} is not on PATH; no tunnel will be opened")
        )
    missing = [pkg for pkg in ("torch", "transformers", "peft") if importlib.util.find_spec(pkg) is None]
    checks.append(
        _line("OK", "lora", "torch, transformers, and peft are importable")
        if not missing
        else _line("WARN", "lora", "LoRA training loop is unavailable. Missing: " + ", ".join(missing))
    )
    if sys.platform == "win32":
        checks.append(_line("OK", "windows ocr", "Windows OCR can be called through PowerShell"))
    else:
        tess = shutil.which("tesseract")
        checks.append(
            _line("OK", "ocr", tess)
            if tess
            else _line("WARN", "ocr", "tesseract is not on PATH; Windows OCR runs only on win32")
        )
    display = bool(__import__("os").environ.get("DISPLAY")) or bool(shutil.which("Xvfb")) or sys.platform == "win32"
    checks.append(
        _line("OK", "screen capture", "a framebuffer capturer is available")
        if display and (ffmpeg or sys.platform in {"win32", "darwin"})
        else _line("WARN", "screen capture", "no DISPLAY, Xvfb, or platform capturer was found")
    )
    return checks
