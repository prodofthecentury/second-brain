# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#     "pillow>=10",
#     "httpx>=0.28,<1",
# ]
# ///
"""Local-first creative image and video generation modules with strict loopback, hardware, DLP, and provenance gates.

Architectural invariants:
1. **Zero Cloud Dependencies**: External endpoints are strictly forbidden. Loopback HTTP
   adapters (`sd_webui_local`, `comfyui_local`) may bind only to `127.0.0.1` or `::1`
   with `trust_env=False` and `follow_redirects=False`.
2. **Hardware Capability Gating**: Heavy neural backends (`sd_webui_local`, `comfyui_local`,
   `diffusers_local`) and high-resolution / high-frame-count budgets are gated by
   `HardwareCapabilityProfile` unless `allow_hardware_override=True` is explicitly passed
   (still hard-capped at `2048x2048` and `120` frames for host memory safety).
3. **Always-Ready Offline Synthesis**: `builtin_procedural_local` renders deterministic
   2x-supersampled visual cards/diagrams/storyboards via Pillow and encodes standards-compliant
   ISO/IEC 14496-12 (`.mp4`) and animated `.gif` video 100% offline without requiring `ffmpeg`.
4. **DLP & Non-Fabrication Invariant**: Prompts are scanned for credentials and adversarial
   risks prior to generation. Generated media lives under `brain/sources/generated/` with JSON
   provenance sidecars and append-only audit logs under `brain/0-raw/archive/creative-media-audit.jsonl`,
   and NEVER auto-creates `accepted` canonical atoms in `brain/2-atoms/`.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, replace
import datetime
import hashlib
import importlib.util
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import random
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import warnings

import httpx

try:
    from PIL import Image, ImageDraw, ImageFont, ImageSequence

    _PILLOW_AVAILABLE = True
except ImportError:  # pragma: no cover - minimal non-[multimodal] wheel install
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]
    ImageSequence = None  # type: ignore[assignment]
    _PILLOW_AVAILABLE = False

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ in ("creative_media", "scripts.creative_media"):
    sys.modules.setdefault("creative_media", sys.modules[__name__])
    sys.modules.setdefault("scripts.creative_media", sys.modules[__name__])

from atom_mutator import atomic_write_bytes, review_lock  # noqa: E402
from config import (  # noqa: E402
    _WINDOWS_RESERVED_STEMS,
    _is_reparse_or_link,
    file_sha256,
    resolve_active_brain_dir,
    safe_file_in_dir,
    vault_paths_for,
    vault_relative_path,
)
from hardware_profile import (  # noqa: E402
    NOMINAL_16GB_RAM_GIB,
    HardwareCapabilityProfile,
    detect_hardware_profile,
)
from host_adapters import (  # noqa: E402
    HostSharingRule,
    OutboundSecretBlockedError,
    filter_atoms_for_host,
    inspect_and_redact_outbound,
    load_host_policies,
    restrict_security_file_permissions,
)
from inbox_workflow import scan_inbox_payload_risks  # noqa: E402
from lexical_store import (  # noqa: E402
    LexicalIndexUnavailable,
    TOKEN_PATTERN,
    lexical_index_is_current,
    sync_lexical_index,
)
from memory_format import split_frontmatter  # noqa: E402
from memory_review import parse_frontmatter_values, section_text  # noqa: E402
from notes_store import NotePaths, create_note  # noqa: E402
from recall import (  # noqa: E402
    MemoryRecallUnavailable,
    QUERY_STOP_WORDS,
    RecallOptions,
    normalize_project_tag,
    project_tags_match,
    search_memory,
)

# Guard Pillow against decompression bombs from untrusted local daemon responses.
if _PILLOW_AVAILABLE and Image is not None:
    Image.MAX_IMAGE_PIXELS = 4096 * 4096

CREATIVE_AUDIT_FILENAME = "creative-media-audit.jsonl"
GENERATED_REL_DIR = "sources/generated"

IMAGE_BACKENDS: tuple[str, ...] = (
    "auto",
    "builtin_procedural_local",
    "sd_webui_local",
    "comfyui_local",
    "diffusers_local",
)
VIDEO_BACKENDS: tuple[str, ...] = (
    "auto",
    "builtin_procedural_local",
    "comfyui_local",
    "diffusers_local",
    "ffmpeg_slideshow_local",
)
NEURAL_BACKENDS: frozenset[str] = frozenset({
    "sd_webui_local",
    "comfyui_local",
    "diffusers_local",
})
PROCEDURAL_STYLES: tuple[str, ...] = (
    "concept_card",
    "blueprint",
    "knowledge_poster",
    "geometric_art",
    "storyboard",
)
IMAGE_FORMATS: tuple[str, ...] = ("png", "webp")
VIDEO_FORMATS: tuple[str, ...] = ("mp4", "gif")

MIN_DIMENSION = 128
DIMENSION_MULTIPLE = 8
HARD_MAX_WIDTH = 2048
HARD_MAX_HEIGHT = 2048
HARD_MAX_FRAMES = 120
HARD_MAX_FPS = 60

MAX_PROMPT_CHARS = 8000
MAX_TITLE_CHARS = 240
MAX_META_FILE_BYTES = 262_144
MAX_DAEMON_RESPONSE_BYTES = 64 * 1024 * 1024

DEFAULT_SD_WEBUI_URL = "http://127.0.0.1:7860"
DEFAULT_COMFYUI_URL = "http://127.0.0.1:8188"

_O_BINARY = getattr(os, "O_BINARY", 0)
_CREATIVE_AUDIT_LOCK = threading.Lock()
_SAFE_MEDIA_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{3,120}$")
_SAFE_PROMPT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SAFE_CHECKPOINT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\\ -]{0,180}$")

_MIME_TYPES: dict[str, str] = {
    "png": "image/png",
    "webp": "image/webp",
    "mp4": "video/mp4",
    "gif": "image/gif",
}


# ─── Exceptions ──────────────────────────────────────────────────────────────


class CreativeMediaError(Exception):
    """Base exception for local creative media generation errors."""


class CreativeMediaSecurityError(CreativeMediaError, PermissionError):
    """Raised when an endpoint, redirect, symlink, or container violates security boundaries."""


class CreativeMediaPolicyError(CreativeMediaError, PermissionError):
    """Raised when DLP, prompt risk scanning, or host sharing policy blocks generation."""


class CreativeMediaSecretBlockedError(OutboundSecretBlockedError, CreativeMediaPolicyError):
    """Raised when credential/secret material is detected in creative prompt inputs."""


class CreativeMediaHardwareGateError(CreativeMediaError, ValueError):
    """Raised when requested backend, resolution, or frame budget exceeds hardware gates."""


class CreativeMediaUnavailableError(CreativeMediaError):
    """Raised in 'auto' mode when no local neural backend is ready and procedural fallback is disabled."""


# ─── Structured Return Records ───────────────────────────────────────────────


class _DictCompatibleMixin:
    """Allow seamless attribute access AND dict/mapping indexing across CLI, API, and UI callers."""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)  # type: ignore[arg-type]

    def __getitem__(self, key: str) -> object:
        return self.as_dict()[key]

    def get(self, key: str, default: object = None) -> object:
        return self.as_dict().get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self.as_dict()

    def keys(self):
        return self.as_dict().keys()

    def items(self):
        return self.as_dict().items()

    def values(self):
        return self.as_dict().values()

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())


@dataclass(frozen=True)
class CreativeMediaGateDecision(_DictCompatibleMixin):
    allowed: bool
    creative_tier: str
    tier_id: str
    backend: str
    media_type: str
    width: int
    height: int
    frames: int
    fps: int
    max_width: int
    max_height: int
    max_frames: int
    max_fps: int
    hardware_override_used: bool
    rationale: str


@dataclass(frozen=True)
class CreativeMediaStatus(_DictCompatibleMixin):
    tier_id: str
    tier_label: str
    creative_tier: str
    gpu_backend: str
    gpu_name: str
    vram_gib: float
    total_ram_gib: float
    available_ram_gib: float
    max_width: int
    max_height: int
    max_frames: int
    max_fps: int
    hard_cap_width: int
    hard_cap_height: int
    hard_cap_frames: int
    neural_backends_unlocked: bool
    neural_gate_reason: str
    recommended_image_backend: str
    recommended_video_backend: str
    ffmpeg_available: bool
    ffmpeg_path: str | None
    diffusers_available: bool
    offline_mp4_muxer_ready: bool
    backends: dict[str, dict[str, object]]


@dataclass(frozen=True)
class GeneratedMediaArtifact(_DictCompatibleMixin):
    schema_version: int
    media_id: str
    media_type: str
    output_format: str
    mime_type: str
    filename: str
    relative_path: str
    metadata_relative_path: str
    file_path: str
    metadata_path: str
    sha256: str
    size_bytes: int
    width: int
    height: int
    frames: int
    fps: int | None
    duration_seconds: float | None
    requested_backend: str
    backend_used: str
    encoder_used: str
    style: str
    seed: int
    title: str
    prompt_sanitized: str
    negative_prompt_sanitized: str
    pii_redaction_count: int
    ground_in_vault: bool
    project_scope: str | None
    grounded_atom_ids: tuple[str, ...]
    grounded_claims: tuple[dict[str, str], ...]
    epistemic_status: str
    canonical_atoms_created: int
    attached_note_filename: str | None
    hardware_tier: str
    hardware_override_used: bool
    created_at: str
    degraded_fallback: bool = False
    warning: str | None = None

    def as_dict(self) -> dict[str, object]:
        raw = asdict(self)
        raw["grounded_atom_ids"] = list(self.grounded_atom_ids)
        raw["grounded_claims"] = [dict(c) for c in self.grounded_claims]
        return raw


# ─── Strict Loopback Network Boundary ────────────────────────────────────────


def validate_local_media_endpoint(
    endpoint_url: str,
    *,
    default_port: int | None = None,
) -> str:
    """Validate that an adapter URL strictly targets HTTP loopback (`127.0.0.1` or `[::1]`).

    Raises `CreativeMediaSecurityError` on cloud/remote hostnames, LAN/public IPs, HTTPS URLs,
    missing/invalid ports, credentials, query strings, fragments, or non-root paths.
    """
    raw = str(endpoint_url or "").strip()
    if not raw or any(ord(ch) < 32 or ch in ("\x7f", " ", "\t") for ch in raw):
        raise CreativeMediaSecurityError(f"Invalid or empty local media endpoint URL: {endpoint_url!r}")

    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise CreativeMediaSecurityError(f"Malformed local media endpoint URL {endpoint_url!r}: {exc}") from exc

    if parsed.scheme != "http":
        raise CreativeMediaSecurityError(
            f"Local media endpoints must use plain loopback 'http://' (rejected scheme {parsed.scheme!r} in {endpoint_url!r})."
        )
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise CreativeMediaSecurityError(
            f"Embedded credentials are forbidden in local media endpoint URL: {endpoint_url!r}"
        )
    if parsed.query or parsed.fragment:
        raise CreativeMediaSecurityError(
            f"Query parameters and fragments are forbidden in local media endpoint URL: {endpoint_url!r}"
        )
    if parsed.path not in ("", "/"):
        raise CreativeMediaSecurityError(
            f"Base local media endpoint URL must not include path segments (got {parsed.path!r} in {endpoint_url!r})."
        )

    try:
        port = parsed.port if parsed.port is not None else default_port
    except ValueError as exc:
        raise CreativeMediaSecurityError(f"Invalid TCP port in local media endpoint {endpoint_url!r}: {exc}") from exc
    if port is None or not (1 <= int(port) <= 65535):
        raise CreativeMediaSecurityError(
            f"Local media endpoint requires an explicit valid TCP port 1-65535 (got {endpoint_url!r})."
        )

    hostname = (parsed.hostname or "").strip().lower()
    if hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise CreativeMediaSecurityError(
            f"External, LAN, or non-loopback media endpoint blocked: {endpoint_url!r} (only 127.0.0.1, ::1, or localhost allowed)."
        )

    if hostname == "localhost":
        try:
            infos = socket.getaddrinfo("localhost", int(port), type=socket.SOCK_STREAM)
        except OSError as exc:
            raise CreativeMediaSecurityError(f"Loopback resolution failed for 'localhost': {exc}") from exc
        if not infos:
            raise CreativeMediaSecurityError("Loopback resolution for 'localhost' returned no addresses.")
        resolved_ips: set[str] = set()
        for info in infos:
            ip_raw = str(info[4][0]).split("%", 1)[0]
            try:
                ip_obj = ipaddress.ip_address(ip_raw)
            except ValueError as exc:
                raise CreativeMediaSecurityError(f"Invalid IP resolved for 'localhost': {ip_raw!r}") from exc
            canonical_ip = str(ip_obj)
            if canonical_ip not in {"127.0.0.1", "::1"}:
                raise CreativeMediaSecurityError(
                    f"DNS rebinding / hosts tamper blocked: 'localhost' resolved to non-loopback address {canonical_ip!r}."
                )
            resolved_ips.add(canonical_ip)
        host_literal = "127.0.0.1" if "127.0.0.1" in resolved_ips else "[::1]"
    elif hostname == "::1":
        host_literal = "[::1]"
    else:
        host_literal = "127.0.0.1"

    return f"http://{host_literal}:{int(port)}"


def _open_loopback_client(
    *,
    timeout: float,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """Create an HTTP client that ignores HTTP_PROXY/HTTPS_PROXY and refuses redirects."""
    return httpx.Client(
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(timeout),
        transport=transport,
    )


def _check_loopback_response(resp: httpx.Response, *, max_bytes: int = MAX_DAEMON_RESPONSE_BYTES) -> bytes:
    """Reject HTTP redirects (3xx), non-200 errors, and oversized loopback responses."""
    if 300 <= resp.status_code < 400 or "location" in {k.lower() for k in resp.headers.keys()}:
        raise CreativeMediaSecurityError(
            f"Loopback media daemon returned forbidden HTTP redirect (status {resp.status_code})."
        )
    if resp.status_code < 200 or resp.status_code >= 300:
        raise CreativeMediaError(
            f"Local media daemon returned HTTP {resp.status_code}: {resp.text[:240]}"
        )
    body = resp.content
    if len(body) > max_bytes:
        raise CreativeMediaSecurityError(
            f"Local media daemon payload ({len(body)} bytes) exceeds safety cap ({max_bytes} bytes)."
        )
    return body


# ─── Hardware Profile Classification & Gates ─────────────────────────────────


def _tier_budget_for_profile(profile: HardwareCapabilityProfile) -> tuple[str, int, int, int, int, bool, str]:
    """Return `(creative_tier, max_w, max_h, max_frames, max_fps, neural_unlocked_by_tier, gate_reason)`."""
    gpu_backend = str(profile.gpu_backend or "cpu").strip().lower()
    vram_gib = float(profile.vram_gib or 0.0)
    total_ram_gib = float(profile.total_ram_gib or 0.0)
    tier_id = str(profile.tier_id or "tier_0_lexical_only").strip()

    is_gpu_tier3 = (
        tier_id == "tier_3_gpu_accelerated"
        or (
            tier_id != "tier_0_lexical_only"
            and gpu_backend in {"cuda", "rocm", "xpu", "directml", "metal"}
            and (vram_gib >= 6.0 or (gpu_backend == "metal" and total_ram_gib >= NOMINAL_16GB_RAM_GIB))
        )
    )
    if is_gpu_tier3:
        return (
            "tier_3_gpu_accelerated",
            1536,
            1536,
            60,
            24,
            True,
            f"GPU accelerator ({profile.gpu_name}, {vram_gib:.1f} GiB VRAM / {total_ram_gib:.1f} GiB RAM) unlocks local neural image & video adapters.",
        )

    if tier_id == "tier_2_cpu_balanced":
        if total_ram_gib >= NOMINAL_16GB_RAM_GIB:
            reason = (
                f"Tier 2 host ({total_ram_gib:.1f} GiB RAM >= {NOMINAL_16GB_RAM_GIB:.1f} GiB nominal 16 GB): "
                "neural adapters unlocked when a loopback daemon (SD WebUI/ComfyUI) or local diffusers model is configured."
            )
        else:
            reason = (
                f"Tier 2 CPU host ({total_ram_gib:.1f} GiB RAM < {NOMINAL_16GB_RAM_GIB:.1f} GiB nominal 16 GB): "
                "heavy neural backends gated off to prevent swap thrashing unless allow_hardware_override=True."
            )
        return ("tier_2_cpu_balanced", 1024, 1024, 36, 16, False, reason)

    effective_low = "tier_1_cpu_compact" if tier_id == "tier_1_cpu_compact" else "tier_0_lexical_only"
    return (
        effective_low,
        768,
        768,
        24,
        12,
        False,
        f"{profile.tier_label}: builtin_procedural_local is ready when explicitly requested; heavy neural GPU backends are gated off unless allow_hardware_override=True.",
    )


def _probe_sd_webui_daemon(
    endpoint_url: str,
    *,
    timeout: float = 0.75,
    transport: httpx.BaseTransport | None = None,
) -> tuple[bool, list[str], str]:
    """Probe Automatic1111 / Forge on loopback without env proxies or redirects."""
    try:
        with _open_loopback_client(timeout=timeout, transport=transport) as client:
            resp = client.get(f"{endpoint_url}/sdapi/v1/sd-models")
            if 300 <= resp.status_code < 400 or "location" in {k.lower() for k in resp.headers.keys()}:
                return (False, [], "Loopback probe rejected HTTP redirect")
            if resp.status_code == 200:
                raw = resp.content[:262_144]
                parsed = json.loads(raw.decode("utf-8"))
                models: list[str] = []
                if isinstance(parsed, list):
                    for item in parsed[:32]:
                        if isinstance(item, dict):
                            name = str(item.get("model_name") or item.get("title") or "").strip()
                            if name:
                                models.append(name)
                return (True, models, f"Automatic1111/Forge online ({len(models)} checkpoint(s))")
            ping = client.get(f"{endpoint_url}/internal/ping")
            if 200 <= ping.status_code < 300 and "location" not in {k.lower() for k in ping.headers.keys()}:
                return (True, [], "Automatic1111/Forge ping online")
    except Exception as exc:
        return (False, [], f"Offline ({type(exc).__name__})")
    return (False, [], "Offline")


def _probe_comfyui_daemon(
    endpoint_url: str,
    *,
    timeout: float = 0.75,
    transport: httpx.BaseTransport | None = None,
) -> tuple[bool, list[str], str]:
    """Probe ComfyUI `/system_stats` on loopback without env proxies or redirects."""
    try:
        with _open_loopback_client(timeout=timeout, transport=transport) as client:
            resp = client.get(f"{endpoint_url}/system_stats")
            if 300 <= resp.status_code < 400 or "location" in {k.lower() for k in resp.headers.keys()}:
                return (False, [], "Loopback probe rejected HTTP redirect")
            if resp.status_code == 200:
                raw = resp.content[:262_144]
                parsed = json.loads(raw.decode("utf-8"))
                devices: list[str] = []
                if isinstance(parsed, dict):
                    for dev in parsed.get("devices") or []:
                        if isinstance(dev, dict) and dev.get("name"):
                            devices.append(str(dev["name"]))
                return (True, devices, "ComfyUI loopback daemon online")
    except Exception as exc:
        return (False, [], f"Offline ({type(exc).__name__})")
    return (False, [], "Offline")


def _is_diffusers_installed() -> bool:
    try:
        return (
            importlib.util.find_spec("diffusers") is not None
            and importlib.util.find_spec("torch") is not None
        )
    except Exception:
        return False


def _is_pyav_libx264_available() -> bool:
    """Return True when PyAV (`av`) is installed with compiled `libx264` H.264 encoder support."""
    try:
        import av  # type: ignore[import-not-found]

        return "libx264" in getattr(av, "codecs_available", set())
    except Exception:
        return False


def _discover_local_diffusers_models(*, explicit_model: str | None = None, media_type: str = "image") -> list[str]:
    """Return configured or locally cached HuggingFace diffusers model specs without network access."""
    models: list[str] = []
    candidates = [
        explicit_model,
        os.environ.get("BRAIN_DIFFUSERS_VIDEO_MODEL") if media_type == "video" else None,
        os.environ.get("BRAIN_DIFFUSERS_MODEL"),
    ]
    for raw in candidates:
        clean = str(raw or "").strip()
        if clean and clean not in models:
            models.append(clean)

    hub_roots: list[Path] = []
    for env_key in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        val = (os.environ.get(env_key) or "").strip()
        if val:
            hub_roots.append(Path(val))
    hf_home = (os.environ.get("HF_HOME") or "").strip()
    if hf_home:
        hub_roots.append(Path(hf_home) / "hub")
    hub_roots.append(Path.home() / ".cache" / "huggingface" / "hub")

    for root in hub_roots:
        if not root.is_dir() or _is_reparse_or_link(str(root)):
            continue
        try:
            for entry in sorted(root.iterdir()):
                if not entry.is_dir() or not entry.name.startswith("models--"):
                    continue
                snaps = entry / "snapshots"
                if not snaps.is_dir():
                    continue
                for snap in snaps.iterdir():
                    if snap.is_dir() and (snap / "model_index.json").is_file():
                        repo_id = entry.name[len("models--") :].replace("--", "/")
                        if repo_id not in models:
                            models.append(repo_id)
                        break
        except OSError:
            continue
    return models[:32]


def get_creative_media_status(
    *,
    hardware_profile: HardwareCapabilityProfile | None = None,
    sd_webui_url: str | None = None,
    comfyui_url: str | None = None,
    probe_timeout: float = 0.75,
    http_transport: httpx.BaseTransport | None = None,
    ffmpeg_path_override: str | None = None,
    diffusers_available_override: bool | None = None,
    diffusers_model_name: str | None = None,
) -> CreativeMediaStatus:
    """Inspect host hardware capability profile and probe local loopback media backends."""
    profile = hardware_profile or detect_hardware_profile(use_cache=True)
    creative_tier, max_w, max_h, max_frames, max_fps, tier3_unlocked, gate_reason = _tier_budget_for_profile(profile)

    if sd_webui_url is not None:
        validated_sd_url = validate_local_media_endpoint(sd_webui_url, default_port=7860)
        sd_url_valid = True
        sd_err = ""
    else:
        env_sd = (os.environ.get("BRAIN_SD_WEBUI_URL") or DEFAULT_SD_WEBUI_URL).strip()
        try:
            validated_sd_url = validate_local_media_endpoint(env_sd, default_port=7860)
            sd_url_valid = True
            sd_err = ""
        except CreativeMediaSecurityError as exc:
            validated_sd_url = DEFAULT_SD_WEBUI_URL
            sd_url_valid = False
            sd_err = str(exc)

    if comfyui_url is not None:
        validated_comfy_url = validate_local_media_endpoint(comfyui_url, default_port=8188)
        comfy_url_valid = True
        comfy_err = ""
    else:
        env_comfy = (os.environ.get("BRAIN_COMFYUI_URL") or DEFAULT_COMFYUI_URL).strip()
        try:
            validated_comfy_url = validate_local_media_endpoint(env_comfy, default_port=8188)
            comfy_url_valid = True
            comfy_err = ""
        except CreativeMediaSecurityError as exc:
            validated_comfy_url = DEFAULT_COMFYUI_URL
            comfy_url_valid = False
            comfy_err = str(exc)

    sd_online, sd_models, sd_detail = (
        _probe_sd_webui_daemon(validated_sd_url, timeout=probe_timeout, transport=http_transport)
        if sd_url_valid
        else (False, [], f"Blocked invalid URL: {sd_err}")
    )
    comfy_online, comfy_devices, comfy_detail = (
        _probe_comfyui_daemon(validated_comfy_url, timeout=probe_timeout, transport=http_transport)
        if comfy_url_valid
        else (False, [], f"Blocked invalid URL: {comfy_err}")
    )

    diffusers_ok = (
        bool(diffusers_available_override)
        if diffusers_available_override is not None
        else _is_diffusers_installed()
    )
    diffusers_models_img = (
        _discover_local_diffusers_models(explicit_model=diffusers_model_name, media_type="image")
        if diffusers_ok
        else []
    )
    diffusers_models_vid = (
        _discover_local_diffusers_models(explicit_model=diffusers_model_name, media_type="video")
        if diffusers_ok
        else []
    )
    explicit_no_ffmpeg = ffmpeg_path_override is not None and not ffmpeg_path_override.strip()
    ffmpeg_resolved = (
        (ffmpeg_path_override.strip() or None)
        if ffmpeg_path_override is not None
        else shutil.which("ffmpeg")
    )
    ffmpeg_ok = bool(ffmpeg_resolved and os.path.isfile(ffmpeg_resolved))
    pyav_h264_ok = (not explicit_no_ffmpeg) and _is_pyav_libx264_available()
    h264_ok = ffmpeg_ok or pyav_h264_ok

    tier2_ram_ok = (
        creative_tier == "tier_2_cpu_balanced"
        and float(profile.total_ram_gib or 0.0) >= NOMINAL_16GB_RAM_GIB
    )
    neural_unlocked = bool(tier3_unlocked or tier2_ram_ok)
    sd_hw_eligible = neural_unlocked
    comfy_hw_eligible = neural_unlocked
    diffusers_hw_eligible = neural_unlocked
    diffusers_img_ready = bool(diffusers_ok and diffusers_hw_eligible and diffusers_models_img)
    diffusers_vid_ready = bool(diffusers_ok and diffusers_hw_eligible and diffusers_models_vid)

    if sd_online and sd_hw_eligible:
        rec_img = "sd_webui_local"
    elif comfy_online and comfy_hw_eligible:
        rec_img = "comfyui_local"
    elif diffusers_img_ready:
        rec_img = "diffusers_local"
    else:
        rec_img = "none"

    if comfy_online and comfy_hw_eligible:
        rec_vid = "comfyui_local"
    elif diffusers_vid_ready:
        rec_vid = "diffusers_local"
    else:
        rec_vid = "none"

    backends: dict[str, dict[str, object]] = {
        "builtin_procedural_local": {
            "available": True,
            "reachable": True,
            "hardware_eligible": True,
            "ready": True,
            "opt_in_only_in_auto": True,
            "endpoint_url": None,
            "supported_media_types": ["image", "video"],
            "models": list(PROCEDURAL_STYLES),
            "detail": (
                "Deterministic 2x-supersampled Pillow synthesizer + "
                + ("PyAV libx264 (avc1)" if pyav_h264_ok else "Pure-Python H.264 Baseline (avc1)")
                + " & GIF muxer (explicit backend or --allow-procedural-fallback)"
            ),
        },
        "sd_webui_local": {
            "available": sd_online,
            "reachable": sd_online,
            "hardware_eligible": sd_hw_eligible,
            "ready": bool(sd_online and sd_hw_eligible),
            "endpoint_url": validated_sd_url if sd_url_valid else None,
            "supported_media_types": ["image"],
            "models": sd_models,
            "detail": sd_detail,
        },
        "comfyui_local": {
            "available": comfy_online,
            "reachable": comfy_online,
            "hardware_eligible": comfy_hw_eligible,
            "ready": bool(comfy_online and comfy_hw_eligible),
            "endpoint_url": validated_comfy_url if comfy_url_valid else None,
            "supported_media_types": ["image", "video"],
            "models": comfy_devices,
            "detail": comfy_detail,
        },
        "diffusers_local": {
            "available": diffusers_ok,
            "reachable": diffusers_ok,
            "hardware_eligible": diffusers_hw_eligible,
            "ready": bool(diffusers_img_ready or diffusers_vid_ready),
            "endpoint_url": None,
            "supported_media_types": ["image", "video"],
            "models": diffusers_models_img,
            "detail": (
                f"Local HuggingFace diffusers (local_files_only=True, {len(diffusers_models_img)} model(s))"
                if diffusers_ok and diffusers_models_img
                else (
                    "diffusers/torch installed (configure BRAIN_DIFFUSERS_MODEL or cache local model)"
                    if diffusers_ok
                    else "diffusers/torch not installed (install extra: .[creative])"
                )
            ),
        },
        "ffmpeg_slideshow_local": {
            "available": True,
            "reachable": True,
            "hardware_eligible": True,
            "ready": True,
            "endpoint_url": None,
            "supported_media_types": ["video"],
            "models": ["libx264"] if h264_ok else ["h264_baseline_ipcm"],
            "detail": (
                f"Local ffmpeg CLI ({ffmpeg_resolved})"
                if ffmpeg_ok
                else (
                    "Bundled PyAV FFmpeg libx264 (H.264 avc1 / yuv420p)"
                    if pyav_h264_ok
                    else "Pure-Python ISO-BMFF H.264 Baseline (avc1 I_PCM)"
                )
            ),
        },
    }

    return CreativeMediaStatus(
        tier_id=profile.tier_id,
        tier_label=profile.tier_label,
        creative_tier=creative_tier,
        gpu_backend=profile.gpu_backend,
        gpu_name=profile.gpu_name,
        vram_gib=float(profile.vram_gib or 0.0),
        total_ram_gib=float(profile.total_ram_gib or 0.0),
        available_ram_gib=float(profile.available_ram_gib or 0.0),
        max_width=max_w,
        max_height=max_h,
        max_frames=max_frames,
        max_fps=max_fps,
        hard_cap_width=HARD_MAX_WIDTH,
        hard_cap_height=HARD_MAX_HEIGHT,
        hard_cap_frames=HARD_MAX_FRAMES,
        neural_backends_unlocked=neural_unlocked,
        neural_gate_reason=gate_reason,
        recommended_image_backend=rec_img,
        recommended_video_backend=rec_vid,
        ffmpeg_available=h264_ok,
        ffmpeg_path=ffmpeg_resolved if ffmpeg_ok else ("pyav:libx264" if pyav_h264_ok else None),
        diffusers_available=diffusers_ok,
        offline_mp4_muxer_ready=True,
        backends=backends,
    )


def enforce_creative_hardware_gate(
    *,
    backend: str,
    media_type: str = "image",
    width: int = 512,
    height: int = 512,
    frames: int = 16,
    fps: int = 12,
    allow_hardware_override: bool = False,
    hardware_profile: HardwareCapabilityProfile | None = None,
    daemon_online: bool = False,
) -> CreativeMediaGateDecision:
    """Validate backend eligibility, resolution multiples, tier limits, and host safety caps."""
    clean_media = str(media_type or "").strip().lower()
    if clean_media not in {"image", "video"}:
        raise CreativeMediaHardwareGateError(f"Unsupported creative media_type: {media_type!r}")

    clean_backend = str(backend or "").strip().lower()
    allowed_backends = IMAGE_BACKENDS if clean_media == "image" else VIDEO_BACKENDS
    if clean_backend not in allowed_backends:
        raise CreativeMediaHardwareGateError(
            f"Unsupported {clean_media} backend {backend!r}; expected one of {list(allowed_backends)}."
        )

    if isinstance(width, bool) or isinstance(height, bool) or not isinstance(width, int) or not isinstance(height, int):
        raise CreativeMediaHardwareGateError("Width and height must be integer pixel dimensions.")
    if width < MIN_DIMENSION or height < MIN_DIMENSION:
        raise CreativeMediaHardwareGateError(
            f"Dimensions {width}x{height} are below minimum {MIN_DIMENSION}x{MIN_DIMENSION}."
        )
    if (width % DIMENSION_MULTIPLE) != 0 or (height % DIMENSION_MULTIPLE) != 0:
        raise CreativeMediaHardwareGateError(
            f"Dimensions {width}x{height} must be exact multiples of {DIMENSION_MULTIPLE}."
        )
    if width > HARD_MAX_WIDTH or height > HARD_MAX_HEIGHT:
        raise CreativeMediaHardwareGateError(
            f"Dimensions {width}x{height} exceed absolute host memory safety ceiling ({HARD_MAX_WIDTH}x{HARD_MAX_HEIGHT})."
        )

    eff_frames = 1 if clean_media == "image" else frames
    eff_fps = 1 if clean_media == "image" else fps
    if clean_media == "video":
        if isinstance(frames, bool) or isinstance(fps, bool) or not isinstance(frames, int) or not isinstance(fps, int):
            raise CreativeMediaHardwareGateError("Video frames and fps must be integers.")
        if frames < 2:
            raise CreativeMediaHardwareGateError("Video generation requires at least 2 frames.")
        if fps < 1:
            raise CreativeMediaHardwareGateError("Video generation requires fps >= 1.")
        if frames > HARD_MAX_FRAMES:
            raise CreativeMediaHardwareGateError(
                f"Requested {frames} frames exceeds absolute host memory safety ceiling ({HARD_MAX_FRAMES} frames)."
            )
        if fps > HARD_MAX_FPS:
            raise CreativeMediaHardwareGateError(
                f"Requested {fps} fps exceeds maximum frame rate ({HARD_MAX_FPS} fps)."
            )

    profile = hardware_profile or detect_hardware_profile(use_cache=True)
    creative_tier, max_w, max_h, max_frames, max_fps, tier3_unlocked, gate_reason = _tier_budget_for_profile(profile)

    override_used = False
    if width > max_w or height > max_h:
        if not allow_hardware_override:
            raise CreativeMediaHardwareGateError(
                f"Requested resolution {width}x{height} exceeds {creative_tier} limit ({max_w}x{max_h}); "
                "reduce dimensions or pass allow_hardware_override=True."
            )
        override_used = True

    if clean_media == "video" and (eff_frames > max_frames or eff_fps > max_fps):
        if not allow_hardware_override:
            raise CreativeMediaHardwareGateError(
                f"Requested video budget ({eff_frames} frames @ {eff_fps} fps) exceeds {creative_tier} limit "
                f"({max_frames} frames @ {max_fps} fps); reduce frames/fps or pass allow_hardware_override=True."
            )
        override_used = True

    if clean_backend in NEURAL_BACKENDS:
        if tier3_unlocked:
            pass
        elif creative_tier == "tier_2_cpu_balanced":
            ram_ok = float(profile.total_ram_gib or 0.0) >= NOMINAL_16GB_RAM_GIB
            if allow_hardware_override:
                override_used = True
            elif ram_ok and daemon_online:
                pass
            else:
                raise CreativeMediaHardwareGateError(
                    f"Neural backend '{clean_backend}' is gated off on {creative_tier} "
                    f"(RAM={profile.total_ram_gib:.1f} GiB, daemon_online={daemon_online}). "
                    "Use 'builtin_procedural_local'/'auto' or pass allow_hardware_override=True."
                )
        else:
            if not allow_hardware_override:
                raise CreativeMediaHardwareGateError(
                    f"Neural backend '{clean_backend}' is gated off on {creative_tier} ({profile.tier_label}). "
                    "Use 'builtin_procedural_local'/'auto' or pass allow_hardware_override=True."
                )
            override_used = True

    return CreativeMediaGateDecision(
        allowed=True,
        creative_tier=creative_tier,
        tier_id=profile.tier_id,
        backend=clean_backend,
        media_type=clean_media,
        width=width,
        height=height,
        frames=eff_frames,
        fps=eff_fps,
        max_width=max_w,
        max_height=max_h,
        max_frames=max_frames,
        max_fps=max_fps,
        hardware_override_used=override_used,
        rationale=gate_reason,
    )


# ─── Vault Paths, Audit Logging & Symlink Guards ─────────────────────────────


def _resolve_creative_paths(brain_dir: str | Path | None = None) -> dict[str, str]:
    resolved_brain = (
        os.path.abspath(os.path.expanduser(str(brain_dir)))
        if brain_dir is not None and str(brain_dir).strip()
        else resolve_active_brain_dir()
    )
    vp = vault_paths_for(resolved_brain)
    sources_dir = vp["sources_dir"]
    generated_dir = os.path.join(sources_dir, "generated")
    archive_dir = os.path.join(vp["raw_dir"], "archive")
    audit_path = os.path.join(archive_dir, CREATIVE_AUDIT_FILENAME)
    lock_path = os.path.join(vp["sidecar_dir"], "writer.lock")

    for check_dir in (resolved_brain, vp["raw_dir"], archive_dir, sources_dir, generated_dir, vp["sidecar_dir"]):
        if os.path.lexists(check_dir) and _is_reparse_or_link(check_dir):
            raise CreativeMediaSecurityError(
                f"Refusing creative media operation through symlink or reparse point: {check_dir}"
            )
    return {
        **vp,
        "generated_dir": generated_dir,
        "archive_dir": archive_dir,
        "audit_path": audit_path,
        "lock_path": lock_path,
    }


def _append_creative_audit(
    audit_path: str,
    *,
    media_type: str,
    requested_backend: str,
    backend_used: str,
    outcome: str,
    project_scope: str | None = None,
    media_id: str | None = None,
    sha256: str | None = None,
    grounded_atom_ids: Sequence[str] = (),
    pii_redaction_count: int = 0,
    risk_flags: Sequence[str] = (),
    hardware_tier: str | None = None,
    hardware_override_used: bool = False,
) -> None:
    """Append an auditable JSONL event without ever persisting raw prompt secrets."""
    record: dict[str, object] = {
        "schema_version": 1,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "media_type": media_type,
        "requested_backend": requested_backend,
        "backend_used": backend_used,
        "outcome": outcome,
        "project_scope": project_scope,
        "media_id": media_id,
        "sha256": sha256,
        "grounded_atom_count": len(grounded_atom_ids),
        "grounded_atom_ids": list(grounded_atom_ids),
        "pii_redaction_count": int(pii_redaction_count),
        "risk_flags": list(risk_flags),
        "hardware_tier": hardware_tier,
        "hardware_override_used": bool(hardware_override_used),
    }
    line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    with _CREATIVE_AUDIT_LOCK:
        parent = os.path.dirname(os.path.abspath(audit_path))
        if os.path.lexists(parent) and _is_reparse_or_link(parent):
            raise CreativeMediaSecurityError(f"Refusing to write creative audit log under symlinked dir: {parent}")
        os.makedirs(parent, exist_ok=True)
        if _is_reparse_or_link(parent) or (os.path.lexists(audit_path) and _is_reparse_or_link(audit_path)):
            raise CreativeMediaSecurityError(f"Refusing to write creative audit log via symlink: {audit_path}")
        existed_before = os.path.exists(audit_path)
        fd = os.open(audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | _O_BINARY, 0o600)
        try:
            if os.write(fd, line) != len(line):
                raise OSError("Incomplete write to creative-media-audit.jsonl.")
            os.fsync(fd)
        finally:
            os.close(fd)
        if not existed_before:
            restrict_security_file_permissions(audit_path)


# ─── DLP, Prompt Risk Scanning & Scoped Vault Grounding ──────────────────────


def _inspect_and_sanitize_creative_inputs(
    *,
    prompt: str,
    negative_prompt: str,
    title: str,
    redact_pii: bool,
    strict_dlp: bool,
    audit_path: str,
    media_type: str,
    requested_backend: str,
    project_scope: str | None,
) -> tuple[str, str, str, int]:
    """Validate and sanitize prompt, negative_prompt, and title fail-closed before generation."""
    raw_prompt = str(prompt or "").strip()
    if not raw_prompt:
        raise ValueError("Creative media generation requires a non-empty prompt.")
    if len(raw_prompt) > MAX_PROMPT_CHARS:
        raise CreativeMediaPolicyError(f"Prompt exceeds maximum length ({MAX_PROMPT_CHARS} chars).")

    raw_neg = str(negative_prompt or "").strip()
    if len(raw_neg) > MAX_PROMPT_CHARS:
        raise CreativeMediaPolicyError(f"Negative prompt exceeds maximum length ({MAX_PROMPT_CHARS} chars).")

    raw_title = str(title or "").strip()
    if len(raw_title) > MAX_TITLE_CHARS:
        raw_title = raw_title[:MAX_TITLE_CHARS].strip()

    # Pre-scan without throwing first so we can record exact pattern labels (never raw secrets!) in audit log.
    combined_secret_hits: list[str] = []
    for field_val in (raw_prompt, raw_neg, raw_title):
        if not field_val:
            continue
        probe = inspect_and_redact_outbound(field_val, redact_pii=False, fail_on_secret_detection=False)
        for hit in probe.secret_hits:
            if hit not in combined_secret_hits:
                combined_secret_hits.append(hit)

    risk_flags_all: list[str] = []
    for field_val in (raw_prompt, raw_neg, raw_title):
        if not field_val:
            continue
        for flag in scan_inbox_payload_risks(field_val):
            if flag.startswith("secret_credential:"):
                sec_label = flag.split(":", 1)[1]
                if sec_label not in combined_secret_hits:
                    combined_secret_hits.append(sec_label)
            elif flag not in risk_flags_all:
                risk_flags_all.append(flag)

    if combined_secret_hits:
        _append_creative_audit(
            audit_path,
            media_type=media_type,
            requested_backend=requested_backend,
            backend_used="none",
            outcome="blocked_secret",
            project_scope=project_scope,
            risk_flags=[f"secret:{h}" for h in combined_secret_hits],
        )
        raise CreativeMediaSecretBlockedError(
            f"Creative media generation blocked: detected credential pattern(s) {combined_secret_hits} in prompt inputs."
        )

    # Execute formal fail-closed outbound redaction pass on each text field.
    prompt_rep = inspect_and_redact_outbound(raw_prompt, redact_pii=redact_pii, fail_on_secret_detection=True)
    neg_rep = inspect_and_redact_outbound(raw_neg, redact_pii=redact_pii, fail_on_secret_detection=True) if raw_neg else None
    title_rep = inspect_and_redact_outbound(raw_title, redact_pii=redact_pii, fail_on_secret_detection=True) if raw_title else None

    if strict_dlp and risk_flags_all:
        _append_creative_audit(
            audit_path,
            media_type=media_type,
            requested_backend=requested_backend,
            backend_used="none",
            outcome="blocked_dlp",
            project_scope=project_scope,
            risk_flags=risk_flags_all,
        )
        raise CreativeMediaPolicyError(
            f"Creative media prompt blocked by DLP / adversarial risk scanner: {risk_flags_all}"
        )

    total_pii = (
        prompt_rep.pii_redaction_count
        + (neg_rep.pii_redaction_count if neg_rep else 0)
        + (title_rep.pii_redaction_count if title_rep else 0)
    )
    return (
        prompt_rep.sanitized_text,
        neg_rep.sanitized_text if neg_rep else "",
        title_rep.sanitized_text if title_rep else "",
        total_pii,
    )


def _retrieve_grounded_vault_claims(
    query: str,
    *,
    paths: dict[str, str],
    project_scope: str | None,
    include_preferences: bool,
    redact_pii: bool,
    max_atoms: int,
    policy_path: Path | None,
    audit_path: str,
    media_type: str,
    requested_backend: str,
) -> tuple[list[str], list[dict[str, str]], int]:
    """Retrieve owner-approved, source-anchored vault claims strictly within `project_scope`."""
    try:
        policies = load_host_policies(policy_path)
    except Exception as exc:
        _append_creative_audit(
            audit_path,
            media_type=media_type,
            requested_backend=requested_backend,
            backend_used="none",
            outcome="blocked_policy",
            project_scope=project_scope,
            risk_flags=["invalid_host_policy"],
        )
        raise CreativeMediaPolicyError(f"Failed to load host sharing policy: {exc}") from exc

    base_rule: HostSharingRule | None = policies.get("creative_local") or policies.get("ollama_local")
    if base_rule is None or not base_rule.enabled or not base_rule.owner_consent_recorded:
        _append_creative_audit(
            audit_path,
            media_type=media_type,
            requested_backend=requested_backend,
            backend_used="none",
            outcome="blocked_policy",
            project_scope=project_scope,
            risk_flags=["host_consent_disabled"],
        )
        raise CreativeMediaPolicyError("Local host sharing policy has disabled vault grounding for creative media.")

    if base_rule.transport != "local":
        _append_creative_audit(
            audit_path,
            media_type=media_type,
            requested_backend=requested_backend,
            backend_used="none",
            outcome="blocked_security",
            project_scope=project_scope,
            risk_flags=["non_local_policy_transport"],
        )
        raise CreativeMediaSecurityError("Creative media vault grounding strictly forbids online host transport rules.")

    norm_scope = normalize_project_tag(project_scope) if (project_scope and str(project_scope).strip()) else None
    if norm_scope:
        allow_wildcard = any(str(p).strip() == "*" for p in base_rule.allowed_projects)
        if not allow_wildcard and not project_tags_match((norm_scope,), base_rule.allowed_projects):
            _append_creative_audit(
                audit_path,
                media_type=media_type,
                requested_backend=requested_backend,
                backend_used="none",
                outcome="blocked_policy",
                project_scope=norm_scope,
                risk_flags=["project_scope_not_allowed"],
            )
            raise CreativeMediaPolicyError(
                f"Project scope '{norm_scope}' is not permitted by host sharing policy."
            )
        effective_rule = replace(
            base_rule,
            allowed_projects=(norm_scope,),
            allow_unscoped_atoms=False,
            allow_preference_atoms=bool(include_preferences and base_rule.allow_preference_atoms),
        )
    else:
        effective_rule = replace(
            base_rule,
            allow_preference_atoms=bool(include_preferences and base_rule.allow_preference_atoms),
        )

    atoms_dir = paths["atoms_dir"]
    index_path = paths["lexical_index_path"]
    chroma_path = paths["chroma_path"]
    if not os.path.isdir(atoms_dir):
        return ([], [], 0)

    if not os.path.isfile(index_path) or not lexical_index_is_current(atoms_dir, index_path):
        with review_lock(paths["lock_path"]):
            sync_lexical_index(atoms_dir, index_path)

    cap = max(1, min(int(max_atoms), 12))
    recall_opts = RecallOptions(
        n_results=max(cap * 4, 16),
        follow_links=False,
        projects=(norm_scope,) if norm_scope else (),
        allow_cross_project=False,
        allow_unreviewed_exploratory=False,
        retrieval_mode="lexical" if os.path.isfile(index_path) else "auto",
        enable_reranker=False,
    )
    try:
        direct_hits, _ = search_memory(
            query,
            recall_opts,
            atoms_dir=atoms_dir,
            index_path=index_path,
            chroma_path=chroma_path,
        )
    except (MemoryRecallUnavailable, LexicalIndexUnavailable):
        return ([], [], 0)

    gated_records = filter_atoms_for_host(
        [hit.as_dict() for hit in direct_hits],
        effective_rule,
        mode=None,
        atoms_dir=atoms_dir,
        brain_dir=paths["brain_dir"],
    )

    atom_ids: list[str] = []
    claims: list[dict[str, str]] = []
    pii_extra = 0
    eff_redact_pii = bool(redact_pii or effective_rule.redact_pii)

    for rec in gated_records:
        if len(claims) >= cap:
            break
        filename = str(rec.get("filename") or "").strip()
        content = str(rec.get("content") or "")
        if not filename or not content:
            continue
        parsed = split_frontmatter(content)
        if parsed is None:
            continue
        fm = parse_frontmatter_values(parsed[0])
        body = "\n".join(parsed[1])
        claim_raw = re.sub(r"\s+", " ", section_text(body, "Claim")).strip()
        if not claim_raw:
            continue
        try:
            red = inspect_and_redact_outbound(
                claim_raw,
                redact_pii=eff_redact_pii,
                fail_on_secret_detection=True,
            )
        except OutboundSecretBlockedError:
            # Skip any atom that inadvertently contains raw credential patterns rather than leaking it.
            continue
        pii_extra += red.pii_redaction_count
        citation_tag = f"[verified: {filename}]"
        atom_ids.append(filename)
        claims.append({
            "atom_id": filename,
            "frontmatter_id": str(fm.get("id") or Path(filename).stem),
            "claim": red.sanitized_text,
            "citation": citation_tag,
        })

    return (atom_ids, claims, pii_extra)


def _compose_neural_prompt(
    sanitized_prompt: str,
    *,
    style: str,
    title: str,
    grounded_claims: Sequence[dict[str, str]],
) -> str:
    """Enrich neural image/video prompt with style descriptors and verified vault claims."""
    parts: list[str] = []
    if title:
        parts.append(f"{title}: {sanitized_prompt}")
    else:
        parts.append(sanitized_prompt)

    style_hints = {
        "concept_card": "clean architectural concept illustration, crisp lighting, structured composition",
        "blueprint": "detailed engineering schematic blueprint, cyan-on-prussian technical linework",
        "knowledge_poster": "editorial infographic poster, high-contrast geometric hierarchy",
        "geometric_art": "fine generative geometric abstraction, harmonic forms, museum print",
        "storyboard": "cinematic multi-beat storyboard composition, dramatic framing",
    }
    if style in style_hints:
        parts.append(style_hints[style])

    if grounded_claims:
        verified_snippets = "; ".join(
            f"{item['claim'][:140]} {item['citation']}" for item in grounded_claims[:3]
        )
        parts.append(f"Grounded context: {verified_snippets}")
    return ". ".join(parts)[:MAX_PROMPT_CHARS]


# ─── Pure-Python ISO/IEC 14496-12 (`.mp4`) Container Muxer & Inspector ──────


def _mp4_box(box_type: bytes, payload: bytes) -> bytes:
    if len(box_type) != 4:
        raise ValueError(f"ISO-BMFF box type must be 4 ASCII bytes (got {box_type!r}).")
    total = 8 + len(payload)
    if total <= 0xFFFFFFFF:
        return struct.pack(">I4s", total, box_type) + payload
    return struct.pack(">I4sQ", 1, box_type, 16 + len(payload)) + payload


def _mp4_full_box(box_type: bytes, version: int, flags: int, payload: bytes) -> bytes:
    ver_flags = ((version & 0xFF) << 24) | (flags & 0xFFFFFF)
    return _mp4_box(box_type, struct.pack(">I", ver_flags) + payload)


def _exp_golomb_ue_bits(val: int) -> list[int]:
    code = val + 1
    leading_zeros = code.bit_length() - 1
    out = [0] * leading_zeros + [1]
    for shift in range(leading_zeros - 1, -1, -1):
        out.append((code >> shift) & 1)
    return out


def _bits_to_rbsp_bytes(bits: list[int]) -> bytes:
    padded = list(bits) + [1]
    while len(padded) % 8 != 0:
        padded.append(0)
    raw = bytearray()
    for pos in range(0, len(padded), 8):
        byte_val = 0
        for bit_idx in range(8):
            byte_val = (byte_val << 1) | padded[pos + bit_idx]
        raw.append(byte_val)
    return bytes(raw)


def _rbsp_to_nal_payload(rbsp: bytes) -> bytes:
    """Insert H.264 start-code emulation prevention bytes (0x03)."""
    out = bytearray()
    zeros = 0
    for b in rbsp:
        if zeros >= 2 and b <= 3:
            out.append(3)
            zeros = 0
        out.append(b)
        zeros = (zeros + 1) if b == 0 else 0
    return bytes(out)


def _h264_sps_nalu(width: int, height: int) -> bytes:
    mb_w = (width + 15) // 16
    mb_h = (height + 15) // 16
    crop_r = (mb_w * 16 - width) // 2
    crop_b = (mb_h * 16 - height) // 2
    bits: list[int] = []
    for byte in (66, 0xC0, 40):  # Baseline Profile (66), constraints 0xC0, Level 4.0
        bits.extend((byte >> shift) & 1 for shift in range(7, -1, -1))
    bits.extend(_exp_golomb_ue_bits(0))  # seq_parameter_set_id
    bits.extend(_exp_golomb_ue_bits(0))  # log2_max_frame_num_minus4
    bits.extend(_exp_golomb_ue_bits(2))  # pic_order_cnt_type = 2
    bits.extend(_exp_golomb_ue_bits(0))  # max_num_ref_frames = 0
    bits.append(0)                       # gaps_in_frame_num_value_allowed_flag
    bits.extend(_exp_golomb_ue_bits(mb_w - 1))
    bits.extend(_exp_golomb_ue_bits(mb_h - 1))
    bits.append(1)                       # frame_mbs_only_flag = 1
    bits.append(0)                       # direct_8x8_inference_flag
    if crop_r or crop_b:
        bits.append(1)
        bits.extend(_exp_golomb_ue_bits(0))
        bits.extend(_exp_golomb_ue_bits(crop_r))
        bits.extend(_exp_golomb_ue_bits(0))
        bits.extend(_exp_golomb_ue_bits(crop_b))
    else:
        bits.append(0)
    bits.append(0)                       # vui_parameters_present_flag = 0
    return b"\x67" + _rbsp_to_nal_payload(_bits_to_rbsp_bytes(bits))


def _h264_pps_nalu() -> bytes:
    bits: list[int] = []
    bits.extend(_exp_golomb_ue_bits(0))  # pic_parameter_set_id
    bits.extend(_exp_golomb_ue_bits(0))  # seq_parameter_set_id
    bits.extend([0, 0])                  # entropy_coding_mode_flag, bottom_field_pic_order
    bits.extend(_exp_golomb_ue_bits(0))  # num_slice_groups_minus1
    bits.extend(_exp_golomb_ue_bits(0))  # num_ref_idx_l0_default_active_minus1
    bits.extend(_exp_golomb_ue_bits(0))  # num_ref_idx_l1_default_active_minus1
    bits.extend([0, 0, 0])               # weighted_pred_flag, weighted_bipred_idc (2 bits)
    bits.extend(_exp_golomb_ue_bits(0))  # pic_init_qp_minus26
    bits.extend(_exp_golomb_ue_bits(0))  # pic_init_qs_minus26
    bits.extend(_exp_golomb_ue_bits(0))  # chroma_qp_index_offset
    bits.extend([0, 0, 0])               # deblocking, constrained_intra, redundant_pic_cnt
    return b"\x68" + _rbsp_to_nal_payload(_bits_to_rbsp_bytes(bits))


def _encode_pil_frame_to_h264_idr_nalu(frame: Image.Image, width: int, height: int) -> bytes:
    """Encode a PIL Image into a valid H.264 Baseline `I_PCM` IDR slice NALU (`nal_unit_type=5`)."""
    rgb = Image.frombytes("RGB", frame.size, frame.convert("RGB").tobytes())
    if rgb.size != (width, height):
        rgb = rgb.resize((width, height), Image.Resampling.LANCZOS)
    mb_w = (width + 15) // 16
    mb_h = (height + 15) // 16
    pw, ph = mb_w * 16, mb_h * 16
    cw = mb_w * 8
    if (width, height) != (pw, ph):
        padded = Image.new("RGB", (pw, ph))
        padded.paste(rgb, (0, 0))
    else:
        padded = rgb
    y_im, cb_im, cr_im = padded.convert("YCbCr").split()
    y_bytes = y_im.tobytes()
    cb_bytes = cb_im.resize((pw // 2, ph // 2), Image.Resampling.BILINEAR).tobytes()
    cr_bytes = cr_im.resize((pw // 2, ph // 2), Image.Resampling.BILINEAR).tobytes()

    bit_buf: list[int] = []
    bit_buf.extend(_exp_golomb_ue_bits(0))  # first_mb_in_slice
    bit_buf.extend(_exp_golomb_ue_bits(7))  # slice_type = 7 (I slice)
    bit_buf.extend(_exp_golomb_ue_bits(0))  # pic_parameter_set_id
    bit_buf.extend([0, 0, 0, 0])            # frame_num = 0 (4 bits)
    bit_buf.extend(_exp_golomb_ue_bits(0))  # idr_pic_id = 0
    bit_buf.extend([0, 0])                  # no_output_of_prior_pics, long_term_reference
    bit_buf.extend(_exp_golomb_ue_bits(0))  # slice_qp_delta

    rbsp = bytearray()
    ipcm_bits = _exp_golomb_ue_bits(25)

    def _flush_aligned(pending: list[int]) -> None:
        rem = len(pending) % 8
        if rem:
            pending.extend([0] * (8 - rem))
        for p in range(0, len(pending), 8):
            val = 0
            for j in range(8):
                val = (val << 1) | pending[p + j]
            rbsp.append(val)
        pending.clear()

    for mby in range(mb_h):
        for mbx in range(mb_w):
            bit_buf.extend(ipcm_bits)
            _flush_aligned(bit_buf)
            y0, x0 = mby * 16, mbx * 16
            for row in range(16):
                off = (y0 + row) * pw + x0
                rbsp.extend(y_bytes[off : off + 16])
            cy0, cx0 = mby * 8, mbx * 8
            for row in range(8):
                off = (cy0 + row) * cw + cx0
                rbsp.extend(cb_bytes[off : off + 8])
            for row in range(8):
                off = (cy0 + row) * cw + cx0
                rbsp.extend(cr_bytes[off : off + 8])
    rbsp.append(0x80)  # rbsp_trailing_bits
    return b"\x65" + _rbsp_to_nal_payload(bytes(rbsp))


def _encode_frames_to_h264_avc1_mp4(
    frames: Sequence[Image.Image],
    *,
    width: int,
    height: int,
    fps: int,
) -> bytes:
    """Mux RGB frames into a browser-playable pure-Python H.264 Baseline (`avc1` + `avcC` + `stss`) `.mp4`."""
    if not frames:
        raise ValueError("Cannot mux an empty frame sequence into MP4.")
    if width < 16 or height < 16 or width > 65535 or height > 65535 or (width % 2) or (height % 2):
        raise ValueError(f"Invalid H.264 YUV420p dimensions: {width}x{height}")
    if fps < 1 or fps > 240:
        raise ValueError(f"Invalid MP4 frame rate: {fps}")

    sps_nalu = _h264_sps_nalu(width, height)
    pps_nalu = _h264_pps_nalu()
    avcc_payload = (
        struct.pack(
            ">BBBBBBH",
            1,            # configurationVersion
            sps_nalu[1],  # AVCProfileIndication
            sps_nalu[2],  # profile_compatibility
            sps_nalu[3],  # AVCLevelIndication
            0xFF,         # lengthSizeMinusOne = 3 (4-byte NAL length prefix)
            0xE1,         # numOfSequenceParameterSets = 1
            len(sps_nalu),
        )
        + sps_nalu
        + struct.pack(">BH", 1, len(pps_nalu))
        + pps_nalu
    )
    avcc_box = _mp4_box(b"avcC", avcc_payload)

    sample_payloads: list[bytes] = []
    frame_sizes: list[int] = []
    for frame in frames:
        idr_nalu = _encode_pil_frame_to_h264_idr_nalu(frame, width, height)
        avcc_sample = struct.pack(">I", len(idr_nalu)) + idr_nalu
        sample_payloads.append(avcc_sample)
        frame_sizes.append(len(avcc_sample))

    ftyp_payload = struct.pack(">4sI", b"isom", 0x00000200) + b"isomiso2avc1mp41"
    ftyp_box = _mp4_box(b"ftyp", ftyp_payload)

    mdat_payload = b"".join(sample_payloads)
    mdat_header_len = 8 if (8 + len(mdat_payload)) <= 0xFFFFFFFF else 16
    mdat_box = _mp4_box(b"mdat", mdat_payload)

    first_sample_offset = len(ftyp_box) + mdat_header_len
    chunk_offsets: list[int] = []
    running = first_sample_offset
    for sz in frame_sizes:
        chunk_offsets.append(running)
        running += sz

    sample_count = len(frame_sizes)
    sample_delta = 1000
    timescale = int(fps) * sample_delta
    total_duration = sample_count * sample_delta
    unity_matrix = struct.pack(
        ">9i",
        0x00010000, 0, 0,
        0, 0x00010000, 0,
        0, 0, 0x40000000,
    )
    mvhd_payload = (
        struct.pack(">IIIIihH2I", 0, 0, timescale, total_duration, 0x00010000, 0x0100, 0, 0, 0)
        + unity_matrix
        + struct.pack(">6II", 0, 0, 0, 0, 0, 0, 2)
    )
    mvhd_box = _mp4_full_box(b"mvhd", 0, 0, mvhd_payload)
    tkhd_payload = (
        struct.pack(">IIIII2IhhhH", 0, 0, 1, 0, total_duration, 0, 0, 0, 0, 0, 0)
        + unity_matrix
        + struct.pack(">II", width << 16, height << 16)
    )
    tkhd_box = _mp4_full_box(b"tkhd", 0, 0x000007, tkhd_payload)
    mdhd_box = _mp4_full_box(b"mdhd", 0, 0, struct.pack(">IIIIHH", 0, 0, timescale, total_duration, 0x55C4, 0))
    hdlr_box = _mp4_full_box(
        b"hdlr",
        0,
        0,
        struct.pack(">I4s3I", 0, b"vide", 0, 0, 0) + b"SecondBrain Local Video\x00",
    )
    vmhd_box = _mp4_full_box(b"vmhd", 0, 0x000001, struct.pack(">H3H", 0, 0, 0, 0))
    url_box = _mp4_full_box(b"url ", 0, 0x000001, b"")
    dref_box = _mp4_full_box(b"dref", 0, 0, struct.pack(">I", 1) + url_box)
    dinf_box = _mp4_box(b"dinf", dref_box)

    compressor_label = b"H.264 Baseline"
    compressor_field = bytes([len(compressor_label)]) + compressor_label + b"\x00" * (31 - len(compressor_label))
    visual_sample_payload = (
        b"\x00" * 6
        + struct.pack(
            ">HHH3IHHIIIH",
            1, 0, 0, 0, 0, 0,
            width, height,
            0x00480000, 0x00480000, 0, 1,
        )
        + compressor_field
        + struct.pack(">Hh", 0x0018, -1)
        + avcc_box
    )
    sample_entry_box = _mp4_box(b"avc1", visual_sample_payload)
    stsd_box = _mp4_full_box(b"stsd", 0, 0, struct.pack(">I", 1) + sample_entry_box)
    stts_box = _mp4_full_box(b"stts", 0, 0, struct.pack(">III", 1, sample_count, sample_delta))
    stsc_box = _mp4_full_box(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, 1, 1))
    stsz_box = _mp4_full_box(
        b"stsz",
        0,
        0,
        struct.pack(">II", 0, sample_count) + struct.pack(f">{sample_count}I", *frame_sizes),
    )
    sync_indices = list(range(1, sample_count + 1))
    stss_box = _mp4_full_box(
        b"stss",
        0,
        0,
        struct.pack(">I", sample_count) + struct.pack(f">{sample_count}I", *sync_indices),
    )
    if chunk_offsets[-1] <= 0xFFFFFFFF:
        stco_box = _mp4_full_box(
            b"stco", 0, 0, struct.pack(">I", sample_count) + struct.pack(f">{sample_count}I", *chunk_offsets)
        )
    else:
        stco_box = _mp4_full_box(
            b"co64", 0, 0, struct.pack(">I", sample_count) + struct.pack(f">{sample_count}Q", *chunk_offsets)
        )

    stbl_box = _mp4_box(b"stbl", stsd_box + stts_box + stsc_box + stsz_box + stss_box + stco_box)
    minf_box = _mp4_box(b"minf", vmhd_box + dinf_box + stbl_box)
    mdia_box = _mp4_box(b"mdia", mdhd_box + hdlr_box + minf_box)
    trak_box = _mp4_box(b"trak", tkhd_box + mdia_box)
    moov_box = _mp4_box(b"moov", mvhd_box + trak_box)

    mp4_bytes = ftyp_box + mdat_box + moov_box
    inspect_mp4_container(mp4_bytes)
    return mp4_bytes


def _mux_mjpeg_mp4(jpeg_frames: Sequence[bytes], width: int, height: int, fps: int) -> bytes:
    """Legacy helper signature retained for compatibility; decodes JPEG frames to browser-playable H.264 `avc1`."""
    if not jpeg_frames:
        raise ValueError("Cannot mux an empty frame sequence into MP4.")
    pil_frames: list[Image.Image] = []
    for raw_jpg in jpeg_frames:
        with Image.open(io.BytesIO(raw_jpg)) as im:
            pil_frames.append(im.convert("RGB"))
    return _encode_frames_to_h264_avc1_mp4(pil_frames, width=width, height=height, fps=fps)


def _iter_bmff_boxes(buf: bytes, start: int = 0, end: int | None = None) -> list[tuple[str, int, int, int]]:
    """Parse contiguous ISO-BMFF boxes inside `buf[start:end]`.

    Returns list of `(box_type, box_start, payload_start, box_end)`.
    """
    limit = len(buf) if end is None else end
    pos = start
    boxes: list[tuple[str, int, int, int]] = []
    while pos < limit:
        if pos + 8 > limit:
            raise CreativeMediaSecurityError("Truncated ISO-BMFF box header in MP4 stream.")
        size32, raw_type = struct.unpack(">I4s", buf[pos : pos + 8])
        box_type = raw_type.decode("latin-1")
        if size32 == 1:
            if pos + 16 > limit:
                raise CreativeMediaSecurityError("Truncated 64-bit ISO-BMFF largesize header.")
            size64 = struct.unpack(">Q", buf[pos + 8 : pos + 16])[0]
            if size64 < 16:
                raise CreativeMediaSecurityError("Invalid 64-bit ISO-BMFF box size.")
            payload_start = pos + 16
            box_end = pos + size64
        elif size32 == 0:
            payload_start = pos + 8
            box_end = limit
        else:
            if size32 < 8:
                raise CreativeMediaSecurityError(f"Invalid ISO-BMFF box size {size32} for {box_type!r}.")
            payload_start = pos + 8
            box_end = pos + size32
        if box_end > limit:
            raise CreativeMediaSecurityError(
                f"ISO-BMFF box {box_type!r} extends past container boundary ({box_end} > {limit})."
            )
        boxes.append((box_type, pos, payload_start, box_end))
        pos = box_end
    return boxes


def inspect_mp4_container(mp4_bytes: bytes) -> dict[str, object]:
    """Validate and inspect an ISO/IEC 14496-12 `.mp4` binary container."""
    if not isinstance(mp4_bytes, (bytes, bytearray)) or len(mp4_bytes) < 64:
        raise CreativeMediaSecurityError("MP4 payload is empty or truncated.")
    raw = bytes(mp4_bytes)
    top = _iter_bmff_boxes(raw, 0, len(raw))
    top_types = [b[0] for b in top]
    if "ftyp" not in top_types or "moov" not in top_types or "mdat" not in top_types:
        raise CreativeMediaSecurityError(
            f"MP4 container missing required top-level boxes (found {top_types})."
        )

    ftyp_box = next(b for b in top if b[0] == "ftyp")
    major_brand = raw[ftyp_box[2] : ftyp_box[2] + 4].decode("latin-1")
    mdat_boxes = [b for b in top if b[0] == "mdat"]
    moov_box = next(b for b in top if b[0] == "moov")

    moov_children = _iter_bmff_boxes(raw, moov_box[2], moov_box[3])
    moov_types = [b[0] for b in moov_children]
    if "mvhd" not in moov_types or "trak" not in moov_types:
        raise CreativeMediaSecurityError("MP4 moov box missing mvhd or trak.")

    trak_box = next(b for b in moov_children if b[0] == "trak")
    trak_children = _iter_bmff_boxes(raw, trak_box[2], trak_box[3])
    trak_types = [b[0] for b in trak_children]
    if "tkhd" not in trak_types or "mdia" not in trak_types:
        raise CreativeMediaSecurityError("MP4 trak box missing tkhd or mdia.")

    mdia_box = next(b for b in trak_children if b[0] == "mdia")
    mdia_children = _iter_bmff_boxes(raw, mdia_box[2], mdia_box[3])
    mdia_types = [b[0] for b in mdia_children]
    if "mdhd" not in mdia_types or "hdlr" not in mdia_types or "minf" not in mdia_types:
        raise CreativeMediaSecurityError("MP4 mdia box missing mdhd, hdlr, or minf.")

    minf_box = next(b for b in mdia_children if b[0] == "minf")
    minf_children = _iter_bmff_boxes(raw, minf_box[2], minf_box[3])
    minf_types = [b[0] for b in minf_children]
    if "stbl" not in minf_types:
        raise CreativeMediaSecurityError("MP4 minf box missing stbl.")

    stbl_box = next(b for b in minf_children if b[0] == "stbl")
    stbl_children = _iter_bmff_boxes(raw, stbl_box[2], stbl_box[3])
    stbl_map = {b[0]: b for b in stbl_children}
    for req in ("stsd", "stts", "stsc", "stsz"):
        if req not in stbl_map:
            raise CreativeMediaSecurityError(f"MP4 stbl box missing {req}.")
    if "stco" not in stbl_map and "co64" not in stbl_map:
        raise CreativeMediaSecurityError("MP4 stbl box missing stco/co64.")

    # Parse stsd VisualSampleEntry
    stsd_p = stbl_map["stsd"][2]
    stsd_e = stbl_map["stsd"][3]
    entry_count = struct.unpack(">I", raw[stsd_p + 4 : stsd_p + 8])[0]
    if entry_count < 1:
        raise CreativeMediaSecurityError("MP4 stsd has zero sample descriptions.")
    sample_entries = _iter_bmff_boxes(raw, stsd_p + 8, stsd_e)
    codec_tag, _, se_p, se_e = sample_entries[0]
    width, height = struct.unpack(">HH", raw[se_p + 24 : se_p + 28])
    horiz_dpi = struct.unpack(">I", raw[se_p + 28 : se_p + 32])[0] >> 16
    depth = struct.unpack(">H", raw[se_p + 74 : se_p + 76])[0]
    sample_child_boxes: list[str] = []
    if se_e > se_p + 78:
        try:
            sample_child_boxes = [b[0] for b in _iter_bmff_boxes(raw, se_p + 78, se_e)]
        except CreativeMediaSecurityError:
            sample_child_boxes = []
    has_avcc = "avcC" in sample_child_boxes
    has_stss = "stss" in stbl_map
    browser_playable = bool(codec_tag in {"avc1", "avc3", "hev1", "hvc1"} and has_avcc)

    # Parse stsz sample sizes
    stsz_p = stbl_map["stsz"][2]
    fixed_sz, sample_count = struct.unpack(">II", raw[stsz_p + 4 : stsz_p + 12])
    if fixed_sz > 0:
        sample_sizes = [fixed_sz] * sample_count
    else:
        sample_sizes = list(
            struct.unpack(f">{sample_count}I", raw[stsz_p + 12 : stsz_p + 12 + 4 * sample_count])
        )

    # Parse stco / co64 chunk offsets
    if "stco" in stbl_map:
        stco_p = stbl_map["stco"][2]
        chunk_count = struct.unpack(">I", raw[stco_p + 4 : stco_p + 8])[0]
        chunk_offsets = list(
            struct.unpack(f">{chunk_count}I", raw[stco_p + 8 : stco_p + 8 + 4 * chunk_count])
        )
    else:
        co64_p = stbl_map["co64"][2]
        chunk_count = struct.unpack(">I", raw[co64_p + 4 : co64_p + 8])[0]
        chunk_offsets = list(
            struct.unpack(f">{chunk_count}Q", raw[co64_p + 8 : co64_p + 8 + 8 * chunk_count])
        )

    mdat_ranges = [(b[2], b[3]) for b in mdat_boxes]
    for off in chunk_offsets:
        if not any(m_start <= off < m_end for m_start, m_end in mdat_ranges):
            raise CreativeMediaSecurityError(f"MP4 chunk offset {off} points outside mdat payload.")

    return {
        "major_brand": major_brand,
        "top_boxes": top_types,
        "moov_boxes": moov_types,
        "trak_boxes": trak_types,
        "mdia_boxes": mdia_types,
        "minf_boxes": minf_types,
        "stbl_boxes": list(stbl_map.keys()),
        "codec_tag": codec_tag,
        "has_avcC": has_avcc,
        "has_stss": has_stss,
        "browser_playable": browser_playable,
        "width": width,
        "height": height,
        "horiz_dpi": horiz_dpi,
        "depth": depth,
        "sample_count": sample_count,
        "sample_sizes": sample_sizes,
        "chunk_offsets": chunk_offsets,
        "mdat_ranges": mdat_ranges,
    }


# ─── Deterministic 2x-Supersampled Pillow Procedural Visual Synthesizer ──────


_STYLE_PALETTES: dict[str, dict[str, tuple[int, int, int]]] = {
    "concept_card": {
        "bg_top": (15, 23, 42),
        "bg_bottom": (30, 27, 75),
        "panel": (24, 34, 62),
        "panel_border": (79, 112, 229),
        "accent": (56, 189, 248),
        "accent_alt": (167, 139, 250),
        "verified_bg": (16, 68, 56),
        "verified_fg": (110, 231, 183),
        "text_primary": (241, 245, 249),
        "text_muted": (148, 163, 184),
        "grid": (38, 52, 88),
    },
    "blueprint": {
        "bg_top": (8, 32, 62),
        "bg_bottom": (12, 49, 92),
        "panel": (11, 41, 78),
        "panel_border": (103, 212, 255),
        "accent": (125, 228, 255),
        "accent_alt": (250, 204, 21),
        "verified_bg": (14, 74, 96),
        "verified_fg": (165, 243, 252),
        "text_primary": (236, 254, 255),
        "text_muted": (147, 197, 253),
        "grid": (24, 78, 136),
    },
    "knowledge_poster": {
        "bg_top": (24, 24, 27),
        "bg_bottom": (45, 28, 52),
        "panel": (39, 35, 48),
        "panel_border": (245, 158, 11),
        "accent": (251, 191, 36),
        "accent_alt": (244, 114, 182),
        "verified_bg": (20, 72, 52),
        "verified_fg": (134, 239, 172),
        "text_primary": (250, 250, 249),
        "text_muted": (214, 211, 209),
        "grid": (63, 56, 74),
    },
    "geometric_art": {
        "bg_top": (17, 17, 27),
        "bg_bottom": (30, 30, 46),
        "panel": (28, 28, 44),
        "panel_border": (243, 139, 168),
        "accent": (137, 180, 250),
        "accent_alt": (250, 179, 135),
        "verified_bg": (24, 64, 58),
        "verified_fg": (148, 226, 213),
        "text_primary": (205, 214, 244),
        "text_muted": (166, 173, 200),
        "grid": (49, 50, 68),
    },
    "storyboard": {
        "bg_top": (18, 20, 28),
        "bg_bottom": (31, 36, 48),
        "panel": (26, 30, 42),
        "panel_border": (96, 165, 250),
        "accent": (96, 165, 250),
        "accent_alt": (251, 146, 60),
        "verified_bg": (19, 68, 54),
        "verified_fg": (110, 231, 183),
        "text_primary": (243, 244, 246),
        "text_muted": (156, 163, 175),
        "grid": (45, 53, 72),
    },
}


def _derive_seed(sanitized_prompt: str, seed: int | None) -> int:
    if seed is not None:
        return int(seed) & 0x7FFFFFFF
    digest = hashlib.sha256(sanitized_prompt.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _prompt_tokens(text: str, *, limit: int = 8) -> list[str]:
    seen: list[str] = []
    for raw_tok in TOKEN_PATTERN.findall(text):
        low = raw_tok.casefold()
        if len(low) < 3 or low in QUERY_STOP_WORDS or low.startswith("redacted"):
            continue
        label = raw_tok[:18]
        if label.casefold() not in {s.casefold() for s in seen}:
            seen.append(label)
        if len(seen) >= limit:
            break
    return seen or ["local", "memory", "synthesis"]


def _load_font(px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    safe_px = max(10, int(px))
    try:
        return ImageFont.load_default(size=safe_px)
    except TypeError:  # pragma: no cover - older Pillow fallback
        return ImageFont.load_default()


def _wrap_text_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    *,
    max_lines: int = 3,
) -> list[str]:
    words = re.sub(r"\s+", " ", str(text or "")).strip().split(" ")
    if not words or words == [""]:
        return []
    lines: list[str] = []
    current = ""
    for idx, word in enumerate(words):
        candidate = word if not current else f"{current} {word}"
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if (bbox[2] - bbox[0]) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            if len(lines) >= max_lines - 1:
                rem = " ".join(words[idx:])
                while len(rem) > 4:
                    tb = draw.textbbox((0, 0), rem + "…", font=font)
                    if (tb[2] - tb[0]) <= max_width:
                        break
                    rem = rem[:-2].rstrip()
                lines.append((rem + "…") if rem else "…")
                return lines
            current = word
    if current and len(lines) < max_lines:
        lines.append(current)
    return lines[:max_lines]


def _fast_gradient_canvas(
    width: int,
    height: int,
    top_rgb: tuple[int, int, int],
    bottom_rgb: tuple[int, int, int],
    accent_rgb: tuple[int, int, int],
    rng: random.Random,
    phase: float = 0.0,
) -> Image.Image:
    """Build a smooth 2D gradient backdrop in <2ms via C-accelerated bicubic upsampling."""
    gw, gh = 32, 32
    glow_x = 0.5 + 0.28 * math.cos(2.0 * math.pi * phase + rng.uniform(0.0, 3.14))
    glow_y = 0.42 + 0.22 * math.sin(2.0 * math.pi * phase + rng.uniform(0.0, 3.14))
    buf = bytearray(gw * gh * 3)
    pos = 0
    for y in range(gh):
        ty = y / max(1, gh - 1)
        br = top_rgb[0] + (bottom_rgb[0] - top_rgb[0]) * ty
        bg = top_rgb[1] + (bottom_rgb[1] - top_rgb[1]) * ty
        bb = top_rgb[2] + (bottom_rgb[2] - top_rgb[2]) * ty
        for x in range(gw):
            tx = x / max(1, gw - 1)
            dist2 = (tx - glow_x) ** 2 + (ty - glow_y) ** 2
            glow = 0.20 * math.exp(-dist2 * 5.5)
            buf[pos] = min(255, max(0, int(br * (1.0 - glow) + accent_rgb[0] * glow)))
            buf[pos + 1] = min(255, max(0, int(bg * (1.0 - glow) + accent_rgb[1] * glow)))
            buf[pos + 2] = min(255, max(0, int(bb * (1.0 - glow) + accent_rgb[2] * glow)))
            pos += 3
    small = Image.frombytes("RGB", (gw, gh), bytes(buf))
    return small.resize((width, height), Image.Resampling.BICUBIC)


def _render_procedural_frame(
    *,
    width: int,
    height: int,
    style: str,
    sanitized_prompt: str,
    title: str,
    seed_int: int,
    grounded_claims: Sequence[dict[str, str]],
    project_scope: str | None,
    frame_index: int = 0,
    total_frames: int = 1,
    fps: int | None = None,
    supersample: int = 2,
) -> Image.Image:
    """Synthesize a high-craft anti-aliased visual frame deterministically from prompt + vault claims."""
    eff_style = style if style in _STYLE_PALETTES else "concept_card"
    pal = _STYLE_PALETTES[eff_style]
    scale = max(1, int(supersample))
    cw = width * scale
    ch = height * scale

    frame_seed_bytes = hashlib.sha256(
        f"{seed_int}:{eff_style}:{sanitized_prompt}".encode("utf-8")
    ).digest()
    rng = random.Random(int.from_bytes(frame_seed_bytes[:8], "big"))
    phase = 0.0 if total_frames <= 1 else (frame_index / float(total_frames))
    progress = 1.0 if total_frames <= 1 else (frame_index / float(max(1, total_frames - 1)))

    canvas = _fast_gradient_canvas(
        cw,
        ch,
        pal["bg_top"],
        pal["bg_bottom"],
        pal["accent"],
        rng,
        phase=phase,
    )
    draw = ImageDraw.Draw(canvas)

    margin = max(20 * scale, int(min(cw, ch) * 0.045))
    grid_step = max(24 * scale, int(min(cw, ch) * 0.08))

    # 1. Style-specific technical grid / backdrop
    if eff_style == "blueprint":
        minor = max(12 * scale, grid_step // 4)
        for gx in range(0, cw, minor):
            col = pal["panel_border"] if (gx % grid_step == 0) else pal["grid"]
            draw.line([(gx, 0), (gx, ch)], fill=col, width=max(1, scale if gx % grid_step == 0 else 1))
        for gy in range(0, ch, minor):
            col = pal["panel_border"] if (gy % grid_step == 0) else pal["grid"]
            draw.line([(0, gy), (cw, gy)], fill=col, width=max(1, scale if gy % grid_step == 0 else 1))
        draw.rectangle(
            [margin // 2, margin // 2, cw - margin // 2, ch - margin // 2],
            outline=pal["panel_border"],
            width=2 * scale,
        )
    else:
        for gx in range(margin, cw - margin, grid_step):
            draw.line([(gx, margin), (gx, ch - margin)], fill=pal["grid"], width=1)
        for gy in range(margin, ch - margin, grid_step):
            draw.line([(margin, gy), (cw - margin, gy)], fill=pal["grid"], width=1)

    tokens = _prompt_tokens(f"{title} {sanitized_prompt}", limit=7)
    font_title = _load_font(max(16, int(min(width, height) * 0.044)) * scale)
    font_body = _load_font(max(11, int(min(width, height) * 0.026)) * scale)
    font_small = _load_font(max(10, int(min(width, height) * 0.021)) * scale)

    header_h = int(ch * 0.22)
    footer_h = int(ch * (0.28 if grounded_claims else 0.14))
    stage_top = margin + header_h
    stage_bottom = ch - margin - footer_h
    if stage_bottom <= stage_top + 40 * scale:
        stage_top = int(ch * 0.25)
        stage_bottom = int(ch * 0.75)

    # 2. Header Card (Title + Prompt Summary + Style/Scope Pill)
    draw.rounded_rectangle(
        [margin, margin, cw - margin, margin + header_h - 8 * scale],
        radius=12 * scale,
        fill=pal["panel"],
        outline=pal["panel_border"],
        width=2 * scale,
    )
    badge_label = eff_style.upper().replace("_", " ")
    if project_scope:
        badge_label = f"{badge_label} · {project_scope}"
    draw.text(
        (margin + 16 * scale, margin + 10 * scale),
        badge_label,
        fill=pal["accent"],
        font=font_small,
    )

    display_title = title if title else " ".join(tokens[:4]).title()
    title_lines = _wrap_text_lines(draw, display_title, font_title, cw - 2 * margin - 32 * scale, max_lines=1)
    title_y = margin + 12 * scale + int(font_small.size * 1.35) if hasattr(font_small, "size") else margin + 28 * scale
    for tl in title_lines:
        draw.text((margin + 16 * scale, title_y), tl, fill=pal["text_primary"], font=font_title)
        tb = draw.textbbox((0, 0), tl, font=font_title)
        title_y += (tb[3] - tb[1]) + 6 * scale

    prompt_lines = _wrap_text_lines(draw, sanitized_prompt, font_body, cw - 2 * margin - 32 * scale, max_lines=2)
    for pl in prompt_lines:
        if title_y + 14 * scale < margin + header_h - 10 * scale:
            draw.text((margin + 16 * scale, title_y), pl, fill=pal["text_muted"], font=font_body)
            pb = draw.textbbox((0, 0), pl, font=font_body)
            title_y += (pb[3] - pb[1]) + 4 * scale

    # 3. Central Generative Motif Stage
    cx = cw // 2
    cy = (stage_top + stage_bottom) // 2
    rx = max(40 * scale, (cw - 2 * margin) // 3)
    ry = max(30 * scale, (stage_bottom - stage_top) // 3)

    if eff_style == "storyboard":
        panel_count = 3 if width >= height else 2
        gap = 12 * scale
        avail_w = cw - 2 * margin - gap * (panel_count - 1)
        pw = max(40 * scale, avail_w // panel_count)
        for p_idx in range(panel_count):
            px0 = margin + p_idx * (pw + gap)
            py0 = stage_top + 4 * scale
            px1 = px0 + pw
            py1 = stage_bottom - 4 * scale
            is_active_panel = total_frames > 1 and (int(progress * panel_count) % panel_count == p_idx)
            border_col = pal["accent_alt"] if is_active_panel else pal["panel_border"]
            draw.rounded_rectangle(
                [px0, py0, px1, py1],
                radius=10 * scale,
                fill=pal["panel"],
                outline=border_col,
                width=(3 if is_active_panel else 2) * scale,
            )
            horizon_y = py0 + int((py1 - py0) * 0.62)
            draw.line([(px0 + 8 * scale, horizon_y), (px1 - 8 * scale, horizon_y)], fill=pal["grid"], width=2 * scale)
            orb_x = (px0 + px1) // 2 + int(math.sin(2.0 * math.pi * phase + p_idx) * (pw * 0.18))
            orb_r = max(10 * scale, min(pw, py1 - py0) // 6)
            draw.ellipse(
                [orb_x - orb_r, horizon_y - orb_r * 2, orb_x + orb_r, horizon_y],
                outline=pal["accent"],
                fill=pal["bg_bottom"],
                width=2 * scale,
            )
            tok_label = tokens[p_idx % len(tokens)].upper()
            draw.text((px0 + 10 * scale, py0 + 8 * scale), f"SCENE 0{p_idx + 1}", fill=pal["accent"], font=font_small)
            draw.text((px0 + 10 * scale, horizon_y + 8 * scale), tok_label, fill=pal["text_primary"], font=font_body)
    elif eff_style == "geometric_art":
        ring_count = 5
        for r_idx in range(ring_count, 0, -1):
            rad = int(min(rx, ry) * (r_idx / ring_count) * 1.15)
            sides = 3 + ((r_idx + len(tokens)) % 5)
            rot = 2.0 * math.pi * phase * (1 if r_idx % 2 == 0 else -1) + rng.uniform(0, math.pi)
            pts = [
                (
                    cx + int(math.cos(rot + 2.0 * math.pi * k / sides) * rad * 1.25),
                    cy + int(math.sin(rot + 2.0 * math.pi * k / sides) * rad),
                )
                for k in range(sides)
            ]
            col = pal["accent"] if r_idx % 2 == 0 else pal["accent_alt"]
            draw.polygon(pts, outline=col, width=2 * scale)
        # Harmonic wave overlay
        wave_pts: list[tuple[int, int]] = []
        steps_w = 64
        for s_i in range(steps_w + 1):
            wx = margin + int((cw - 2 * margin) * (s_i / steps_w))
            wy = cy + int(math.sin(s_i * 0.28 + 2.0 * math.pi * phase) * (ry * 0.45))
            wave_pts.append((wx, wy))
        draw.line(wave_pts, fill=pal["panel_border"], width=3 * scale)
    else:
        # Node-link constellation / schematic graph (concept_card, blueprint, knowledge_poster)
        node_coords: list[tuple[int, int, str]] = []
        n_nodes = max(3, min(len(tokens), 7))
        for idx in range(n_nodes):
            base_ang = (2.0 * math.pi * idx / n_nodes) + rng.uniform(-0.18, 0.18)
            ang = base_ang + (2.0 * math.pi * phase * 0.35)
            dist_scale = 0.68 + 0.25 * ((idx % 2) + 0.2)
            nx = cx + int(math.cos(ang) * rx * dist_scale)
            ny = cy + int(math.sin(ang) * ry * dist_scale)
            node_coords.append((nx, ny, tokens[idx]))

        # Connect hub & ring edges
        for idx, (nx, ny, _) in enumerate(node_coords):
            nx2, ny2, _ = node_coords[(idx + 1) % len(node_coords)]
            if eff_style == "blueprint":
                draw.line([(nx, ny), (nx2, ny), (nx2, ny2)], fill=pal["accent"], width=2 * scale)
            else:
                draw.line([(cx, cy), (nx, ny)], fill=pal["grid"], width=2 * scale)
                draw.line([(nx, ny), (nx2, ny2)], fill=pal["panel_border"], width=2 * scale)

        # Central synthesis core
        hub_r = max(14 * scale, int(min(rx, ry) * 0.22))
        draw.ellipse(
            [cx - hub_r, cy - hub_r, cx + hub_r, cy + hub_r],
            fill=pal["panel"],
            outline=pal["accent_alt"],
            width=3 * scale,
        )
        for nx, ny, label in node_coords:
            nr = max(8 * scale, int(hub_r * 0.52))
            draw.ellipse(
                [nx - nr, ny - nr, nx + nr, ny + nr],
                fill=pal["bg_top"],
                outline=pal["accent"],
                width=2 * scale,
            )
            draw.text((nx + nr + 6 * scale, ny - nr), label, fill=pal["text_primary"], font=font_small)

    # 4. Grounded Vault Claims / Provenance Footer
    footer_top = ch - margin - footer_h + 6 * scale
    draw.rounded_rectangle(
        [margin, footer_top, cw - margin, ch - margin],
        radius=10 * scale,
        fill=pal["panel"],
        outline=pal["verified_fg"] if grounded_claims else pal["grid"],
        width=2 * scale,
    )
    fy = footer_top + 10 * scale
    if grounded_claims:
        for claim_item in grounded_claims[:2]:
            badge = claim_item["citation"]
            snippet = f"{badge} {claim_item['claim']}"
            wrapped = _wrap_text_lines(draw, snippet, font_small, cw - 2 * margin - 28 * scale, max_lines=1)
            if wrapped and fy + 14 * scale < ch - margin - 18 * scale:
                draw.text((margin + 14 * scale, fy), wrapped[0], fill=pal["verified_fg"], font=font_small)
                bb = draw.textbbox((0, 0), wrapped[0], font=font_small)
                fy += (bb[3] - bb[1]) + 6 * scale
    else:
        prov_line = f"LOCAL OFFLINE CREATIVE ARTIFACT · SEED {seed_int} · UNVERIFIED OUTPUT"
        draw.text((margin + 14 * scale, fy), prov_line, fill=pal["text_muted"], font=font_small)

    # Animated progress bar for multi-frame video
    if total_frames > 1:
        bar_y0 = ch - margin - 12 * scale
        bar_y1 = ch - margin - 5 * scale
        bar_x0 = margin + 14 * scale
        bar_x1 = cw - margin - 14 * scale
        draw.rounded_rectangle([bar_x0, bar_y0, bar_x1, bar_y1], radius=3 * scale, fill=pal["bg_top"])
        fill_x = bar_x0 + max(6 * scale, int((bar_x1 - bar_x0) * progress))
        draw.rounded_rectangle([bar_x0, bar_y0, fill_x, bar_y1], radius=3 * scale, fill=pal["accent"])
        fps_str = f" · {fps} FPS" if fps else ""
        frame_tag = f"FRAME {frame_index + 1:02d}/{total_frames:02d}{fps_str}"
        tb = draw.textbbox((0, 0), frame_tag, font=font_small)
        draw.text((bar_x1 - (tb[2] - tb[0]), footer_top + 8 * scale), frame_tag, fill=pal["accent_alt"], font=font_small)

    if scale > 1:
        return canvas.resize((width, height), Image.Resampling.LANCZOS)
    return canvas


# ─── Pillow Image / Video Sanitization & Encoding Helpers ────────────────────


def _sanitize_and_encode_image(
    raw_image: Image.Image | bytes,
    *,
    target_width: int,
    target_height: int,
    output_format: str,
) -> bytes:
    """Decode/verify an image, strip ALL untrusted metadata chunks via `Image.frombytes`, and re-encode."""
    if isinstance(raw_image, (bytes, bytearray)):
        if len(raw_image) > MAX_DAEMON_RESPONSE_BYTES:
            raise CreativeMediaSecurityError("Daemon image payload exceeds safety limit.")
        with Image.open(io.BytesIO(raw_image)) as verify_im:
            verify_im.verify()
        with Image.open(io.BytesIO(raw_image)) as src_im:
            src_w, src_h = src_im.size
            if src_w < 1 or src_h < 1 or src_w > HARD_MAX_WIDTH * 2 or src_h > HARD_MAX_HEIGHT * 2:
                raise CreativeMediaSecurityError(f"Daemon returned unsafe image dimensions: {src_w}x{src_h}")
            rgb_conv = src_im.convert("RGB")
            clean_im = Image.frombytes("RGB", rgb_conv.size, rgb_conv.tobytes())
    else:
        rgb_conv = raw_image.convert("RGB")
        clean_im = Image.frombytes("RGB", rgb_conv.size, rgb_conv.tobytes())

    if clean_im.size != (target_width, target_height):
        clean_im = clean_im.resize((target_width, target_height), Image.Resampling.LANCZOS)

    out_buf = io.BytesIO()
    fmt = output_format.lower()
    if fmt == "png":
        clean_im.save(out_buf, format="PNG", optimize=False)
    elif fmt == "webp":
        clean_im.save(out_buf, format="WEBP", quality=90, method=4)
    else:
        raise ValueError(f"Unsupported image output_format: {output_format!r}")

    encoded = out_buf.getvalue()
    with Image.open(io.BytesIO(encoded)) as check_im:
        check_im.verify()
    with Image.open(io.BytesIO(encoded)) as check_dim:
        if check_dim.size != (target_width, target_height):
            raise CreativeMediaError("Encoded image dimensions did not match target dimensions.")
    return encoded


def _encode_frames_to_gif(frames: Sequence[Image.Image], *, fps: int) -> bytes:
    if len(frames) < 2:
        raise ValueError("Animated GIF requires at least 2 frames.")
    duration_ms = max(20, int(round(1000.0 / max(1, fps))))
    pal_frames: list[Image.Image] = []
    for frame in frames:
        rgb = Image.frombytes("RGB", frame.size, frame.convert("RGB").tobytes())
        pal_frames.append(
            rgb.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        )
    buf = io.BytesIO()
    pal_frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=pal_frames[1:],
        duration=duration_ms,
        loop=0,
        optimize=False,
        disposal=2,
    )
    gif_bytes = buf.getvalue()
    with Image.open(io.BytesIO(gif_bytes)) as check_gif:
        count = sum(1 for _ in ImageSequence.Iterator(check_gif))
        if count != len(frames):
            raise CreativeMediaError(f"Animated GIF verification expected {len(frames)} frames, got {count}.")
    return gif_bytes


def _encode_frames_to_mp4(
    frames: Sequence[Image.Image],
    *,
    width: int,
    height: int,
    fps: int,
    ffmpeg_exe: str | None = None,
    prefer_ffmpeg: bool = False,
    allow_pyav: bool = True,
) -> tuple[bytes, str]:
    """Encode RGB frames into a browser-playable H.264 (`avc1` + `avcC` + `stss`) `.mp4`."""
    if prefer_ffmpeg and ffmpeg_exe and os.path.isfile(ffmpeg_exe):
        try:
            return (
                _encode_frames_with_ffmpeg(
                    frames,
                    ffmpeg_exe=ffmpeg_exe,
                    width=width,
                    height=height,
                    fps=fps,
                ),
                "ffmpeg_libx264",
            )
        except CreativeMediaError:
            pass

    if allow_pyav and _is_pyav_libx264_available():
        try:
            return (
                _encode_frames_with_pyav_libx264(
                    frames,
                    width=width,
                    height=height,
                    fps=fps,
                ),
                "pyav_libx264",
            )
        except CreativeMediaError:
            pass

    if ffmpeg_exe and os.path.isfile(ffmpeg_exe):
        try:
            return (
                _encode_frames_with_ffmpeg(
                    frames,
                    ffmpeg_exe=ffmpeg_exe,
                    width=width,
                    height=height,
                    fps=fps,
                ),
                "ffmpeg_libx264",
            )
        except CreativeMediaError:
            pass

    return (
        _encode_frames_to_h264_avc1_mp4(
            frames,
            width=width,
            height=height,
            fps=fps,
        ),
        "iso_bmff_h264_avc1_pure_python",
    )


def _encode_frames_to_mjpeg_mp4(
    frames: Sequence[Image.Image],
    *,
    width: int,
    height: int,
    fps: int,
    jpeg_quality: int = 88,
) -> bytes:
    """Compatibility alias returning browser-playable pure-Python H.264 Baseline `avc1` `.mp4`."""
    del jpeg_quality
    return _encode_frames_to_h264_avc1_mp4(frames, width=width, height=height, fps=fps)


def _encode_frames_with_ffmpeg(
    frames: Sequence[Image.Image],
    *,
    ffmpeg_exe: str,
    width: int,
    height: int,
    fps: int,
) -> bytes:
    """Encode RGB frames into H.264 `.mp4` via local `ffmpeg` (`shell=False`, isolated temp dir)."""
    if not ffmpeg_exe or not os.path.isfile(ffmpeg_exe):
        raise CreativeMediaError("Local ffmpeg executable is not available.")
    with tempfile.TemporaryDirectory(prefix="sb-creative-ffmpeg-") as tmp_dir:
        for idx, frame in enumerate(frames):
            rgb = Image.frombytes("RGB", frame.size, frame.convert("RGB").tobytes())
            if rgb.size != (width, height):
                rgb = rgb.resize((width, height), Image.Resampling.LANCZOS)
            frame_path = os.path.join(tmp_dir, f"frame-{idx:04d}.png")
            rgb.save(frame_path, format="PNG")
        out_mp4 = os.path.join(tmp_dir, "output.mp4")
        cmd = [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-framerate",
            str(int(fps)),
            "-i",
            os.path.join(tmp_dir, "frame-%04d.png"),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            out_mp4,
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60.0,
            check=False,
            shell=False,
        )
        if proc.returncode != 0 or not os.path.isfile(out_mp4):
            raise CreativeMediaError(f"ffmpeg local encoding failed: {proc.stderr[:240]}")
        mp4_bytes = Path(out_mp4).read_bytes()
    inspect_mp4_container(mp4_bytes)
    return mp4_bytes


def _encode_frames_with_pyav_libx264(
    frames: Sequence[Image.Image],
    *,
    width: int,
    height: int,
    fps: int,
) -> bytes:
    """Encode RGB frames into H.264 (`avc1`, `yuv420p`) `.mp4` via bundled PyAV/libx264."""
    try:
        import av  # type: ignore[import-not-found]
    except ImportError as exc:
        raise CreativeMediaError("PyAV ('av') package is not installed.") from exc
    if "libx264" not in getattr(av, "codecs_available", set()):
        raise CreativeMediaError("Bundled PyAV installation lacks libx264 H.264 encoder support.")

    with tempfile.TemporaryDirectory(prefix="sb-creative-pyav-") as tmp_dir:
        out_mp4 = os.path.join(tmp_dir, "output.mp4")
        container = av.open(out_mp4, mode="w", format="mp4", options={"movflags": "+faststart"})
        try:
            stream = container.add_stream("libx264", rate=max(1, int(fps)))
            stream.width = int(width)
            stream.height = int(height)
            stream.pix_fmt = "yuv420p"
            stream.options = {"crf": "23", "preset": "veryfast"}
            for frame in frames:
                rgb = Image.frombytes("RGB", frame.size, frame.convert("RGB").tobytes())
                if rgb.size != (width, height):
                    rgb = rgb.resize((width, height), Image.Resampling.LANCZOS)
                vframe = av.VideoFrame.from_image(rgb)
                for packet in stream.encode(vframe):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        finally:
            container.close()
        mp4_bytes = Path(out_mp4).read_bytes()
    info = inspect_mp4_container(mp4_bytes)
    if info.get("codec_tag") != "avc1":
        raise CreativeMediaError(f"Unexpected PyAV H.264 codec tag: {info.get('codec_tag')!r}")
    return mp4_bytes


# ─── Local Neural Loopback Adapters ──────────────────────────────────────────


def _generate_via_sd_webui_local(
    *,
    endpoint_url: str,
    prompt: str,
    negative_prompt: str,
    width: int,
    height: int,
    seed: int,
    steps: int,
    cfg_scale: float,
    sampler_name: str,
    output_format: str,
    timeout: float,
    transport: httpx.BaseTransport | None,
) -> bytes:
    payload = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "seed": int(seed),
        "steps": max(1, min(100, int(steps))),
        "cfg_scale": max(1.0, min(30.0, float(cfg_scale))),
        "width": int(width),
        "height": int(height),
        "sampler_name": str(sampler_name or "Euler a")[:64],
        "batch_size": 1,
        "n_iter": 1,
        "do_not_save_samples": True,
        "do_not_save_grid": True,
    }
    with _open_loopback_client(timeout=timeout, transport=transport) as client:
        resp = client.post(f"{endpoint_url}/sdapi/v1/txt2img", json=payload)
        raw_body = _check_loopback_response(resp)
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CreativeMediaError(f"Invalid JSON from sd_webui_local: {exc}") from exc
    images = parsed.get("images") if isinstance(parsed, dict) else None
    if not isinstance(images, list) or not images or not isinstance(images[0], str):
        raise CreativeMediaError("sd_webui_local response did not include base64 image data.")
    b64_str = images[0].split(",", 1)[-1].strip()
    if len(b64_str) > MAX_DAEMON_RESPONSE_BYTES:
        raise CreativeMediaSecurityError("sd_webui_local base64 image exceeds safety ceiling.")
    try:
        decoded_bytes = base64.b64decode(b64_str, validate=True)
    except Exception as exc:
        raise CreativeMediaError(f"sd_webui_local returned malformed base64 payload: {exc}") from exc
    return _sanitize_and_encode_image(
        decoded_bytes,
        target_width=width,
        target_height=height,
        output_format=output_format,
    )


def _select_comfy_checkpoint(
    client: httpx.Client,
    endpoint_url: str,
    requested_model: str | None,
) -> str:
    if requested_model:
        clean = requested_model.strip()
        if ".." in clean or not _SAFE_CHECKPOINT_RE.fullmatch(clean):
            raise CreativeMediaSecurityError(f"Unsafe ComfyUI checkpoint identifier: {requested_model!r}")
        return clean
    try:
        resp = client.get(f"{endpoint_url}/object_info/CheckpointLoaderSimple")
        if resp.status_code == 200 and "location" not in {k.lower() for k in resp.headers.keys()}:
            info = json.loads(resp.content[:524_288].decode("utf-8"))
            ckpts = (
                info.get("CheckpointLoaderSimple", {})
                .get("input", {})
                .get("required", {})
                .get("ckpt_name", [[]])[0]
            )
            if isinstance(ckpts, list) and ckpts and isinstance(ckpts[0], str):
                cand = ckpts[0].strip()
                if ".." not in cand and _SAFE_CHECKPOINT_RE.fullmatch(cand):
                    return cand
    except Exception:
        pass
    return "v1-5-pruned-emaonly.safetensors"


def _build_comfy_workflow(
    *,
    ckpt_name: str,
    prompt: str,
    negative_prompt: str,
    width: int,
    height: int,
    batch_size: int,
    seed: int,
    steps: int,
    cfg_scale: float,
) -> dict[str, object]:
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": ckpt_name},
        },
        "2": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": int(width), "height": int(height), "batch_size": int(batch_size)},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["1", 1]},
        },
        "4": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt, "clip": ["1", 1]},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "seed": int(seed),
                "steps": max(1, min(100, int(steps))),
                "cfg": max(1.0, min(30.0, float(cfg_scale))),
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["3", 0],
                "negative": ["4", 0],
                "latent_image": ["2", 0],
            },
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
        },
        "7": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "SecondBrainLocal", "images": ["6", 0]},
        },
    }


def _submit_and_collect_comfy_outputs(
    *,
    endpoint_url: str,
    workflow: dict[str, object],
    timeout: float,
    poll_interval: float,
    max_polls: int,
    transport: httpx.BaseTransport | None,
) -> list[tuple[str, bytes]]:
    """Submit ComfyUI workflow over loopback, poll `/history/<prompt_id>`, and fetch `/view` assets."""
    with _open_loopback_client(timeout=timeout, transport=transport) as client:
        post_resp = client.post(
            f"{endpoint_url}/prompt",
            json={"prompt": workflow, "client_id": f"secondbrain-{secrets.token_hex(6)}"},
        )
        post_body = _check_loopback_response(post_resp)
        try:
            prompt_ack = json.loads(post_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CreativeMediaError(f"Invalid JSON from ComfyUI /prompt: {exc}") from exc

        prompt_id = str(prompt_ack.get("prompt_id") or "").strip() if isinstance(prompt_ack, dict) else ""
        if not prompt_id or not _SAFE_PROMPT_ID_RE.fullmatch(prompt_id):
            raise CreativeMediaSecurityError(f"ComfyUI returned invalid or unsafe prompt_id: {prompt_id!r}")

        outputs_map: dict[str, object] | None = None
        polls = max(1, int(max_polls))
        wait_s = max(0.0, float(poll_interval))
        for attempt in range(polls):
            hist_resp = client.get(f"{endpoint_url}/history/{prompt_id}")
            hist_raw = _check_loopback_response(hist_resp)
            hist_json = json.loads(hist_raw.decode("utf-8"))
            if isinstance(hist_json, dict) and prompt_id in hist_json:
                entry = hist_json[prompt_id]
                if isinstance(entry, dict) and isinstance(entry.get("outputs"), dict) and entry["outputs"]:
                    outputs_map = entry["outputs"]
                    break
            if attempt + 1 < polls and wait_s > 0:
                time.sleep(wait_s)

        if not outputs_map:
            raise CreativeMediaError(f"ComfyUI workflow {prompt_id} timed out before producing outputs.")

        asset_descriptors: list[dict[str, str]] = []
        for node_out in outputs_map.values():
            if not isinstance(node_out, dict):
                continue
            for key in ("videos", "gifs", "images"):
                items = node_out.get(key)
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict) and item.get("filename"):
                            asset_descriptors.append({
                                "filename": str(item["filename"]),
                                "subfolder": str(item.get("subfolder") or ""),
                                "type": str(item.get("type") or "output"),
                            })

        if not asset_descriptors:
            raise CreativeMediaError(f"ComfyUI workflow {prompt_id} completed without image/video outputs.")

        fetched: list[tuple[str, bytes]] = []
        for desc in asset_descriptors[: HARD_MAX_FRAMES + 2]:
            fname = desc["filename"]
            subf = desc["subfolder"]
            ftype = desc["type"]
            if ".." in fname or "/" in fname or "\\" in fname or ":" in fname:
                raise CreativeMediaSecurityError(f"ComfyUI returned unsafe output filename: {fname!r}")
            if ".." in subf or ":" in subf or subf.startswith(("/", "\\")):
                raise CreativeMediaSecurityError(f"ComfyUI returned unsafe output subfolder: {subf!r}")
            if ftype not in {"output", "temp"}:
                raise CreativeMediaSecurityError(f"ComfyUI returned unsafe output type: {ftype!r}")
            view_resp = client.get(
                f"{endpoint_url}/view",
                params={
                    "filename": fname,
                    "subfolder": subf,
                    "type": ftype,
                },
            )
            fetched.append((fname.lower(), _check_loopback_response(view_resp)))
        return fetched


def _generate_via_comfyui_image(
    *,
    endpoint_url: str,
    prompt: str,
    negative_prompt: str,
    model_name: str | None,
    width: int,
    height: int,
    seed: int,
    steps: int,
    cfg_scale: float,
    output_format: str,
    timeout: float,
    poll_interval: float,
    max_polls: int,
    transport: httpx.BaseTransport | None,
) -> bytes:
    with _open_loopback_client(timeout=timeout, transport=transport) as client:
        ckpt = _select_comfy_checkpoint(client, endpoint_url, model_name)
    workflow = _build_comfy_workflow(
        ckpt_name=ckpt,
        prompt=prompt,
        negative_prompt=negative_prompt,
        width=width,
        height=height,
        batch_size=1,
        seed=seed,
        steps=steps,
        cfg_scale=cfg_scale,
    )
    assets = _submit_and_collect_comfy_outputs(
        endpoint_url=endpoint_url,
        workflow=workflow,
        timeout=timeout,
        poll_interval=poll_interval,
        max_polls=max_polls,
        transport=transport,
    )
    return _sanitize_and_encode_image(
        assets[0][1],
        target_width=width,
        target_height=height,
        output_format=output_format,
    )


def _probe_comfy_object_info(client: httpx.Client, endpoint_url: str) -> dict[str, object]:
    """Fetch ComfyUI `/object_info` summary safely to discover installed temporal video nodes."""
    try:
        resp = client.get(f"{endpoint_url}/object_info")
        if resp.status_code == 200 and "location" not in {k.lower() for k in resp.headers.keys()}:
            parsed = json.loads(resp.content[: 2 * 1024 * 1024].decode("utf-8"))
            if isinstance(parsed, dict):
                return parsed
    except Exception:
        pass
    return {}


def _build_comfy_video_workflow(
    *,
    ckpt_name: str,
    prompt: str,
    negative_prompt: str,
    width: int,
    height: int,
    frames: int,
    fps: int,
    seed: int,
    steps: int,
    cfg_scale: float,
    object_info: dict[str, object],
) -> tuple[dict[str, object], str]:
    """Construct a real temporal ComfyUI video graph (AnimateDiff / SVD / LTXV / LatentMotionChain)."""
    clamped_steps = max(1, min(100, int(steps)))
    clamped_cfg = max(1.0, min(30.0, float(cfg_scale)))

    # Path A: AnimateDiff temporal motion module installed
    for ad_node in ("ADE_AnimateDiffLoaderGen1", "ADE_AnimateDiffLoaderWithContext"):
        if ad_node in object_info:
            motion_models = (
                object_info.get(ad_node, {})
                .get("input", {})
                .get("required", {})
                .get("model_name", [[]])
            )
            mm_name = (
                str(motion_models[0][0])
                if isinstance(motion_models, list) and motion_models and isinstance(motion_models[0], list) and motion_models[0]
                else "mm_sd_v15_v2.ckpt"
            )
            wf = {
                "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt_name}},
                "2": {"class_type": "EmptyLatentImage", "inputs": {"width": int(width), "height": int(height), "batch_size": int(frames)}},
                "3": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
                "4": {"class_type": "CLIPTextEncode", "inputs": {"text": negative_prompt, "clip": ["1", 1]}},
                "10": {
                    "class_type": ad_node,
                    "inputs": {"model": ["1", 0], "model_name": mm_name, "beta_schedule": "autoselect"},
                },
                "5": {
                    "class_type": "KSampler",
                    "inputs": {
                        "seed": int(seed),
                        "steps": clamped_steps,
                        "cfg": clamped_cfg,
                        "sampler_name": "euler",
                        "scheduler": "normal",
                        "denoise": 1.0,
                        "model": ["10", 0],
                        "positive": ["3", 0],
                        "negative": ["4", 0],
                        "latent_image": ["2", 0],
                    },
                },
                "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
                "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "SecondBrainAnimateDiff", "images": ["6", 0]}},
            }
            return wf, "animatediff"

    # Path B: Stable Video Diffusion image-to-video conditioning
    if "SVD_img2vid_conditioning" in object_info and "ImageOnlyCheckpointLoader" in object_info:
        wf = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt_name}},
            "2": {"class_type": "EmptyLatentImage", "inputs": {"width": int(width), "height": int(height), "batch_size": 1}},
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
            "4": {"class_type": "CLIPTextEncode", "inputs": {"text": negative_prompt, "clip": ["1", 1]}},
            "5": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": int(seed),
                    "steps": clamped_steps,
                    "cfg": clamped_cfg,
                    "sampler_name": "euler",
                    "scheduler": "normal",
                    "denoise": 1.0,
                    "model": ["1", 0],
                    "positive": ["3", 0],
                    "negative": ["4", 0],
                    "latent_image": ["2", 0],
                },
            },
            "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
            "11": {"class_type": "ImageOnlyCheckpointLoader", "inputs": {"ckpt_name": "svd_xt.safetensors"}},
            "12": {
                "class_type": "SVD_img2vid_conditioning",
                "inputs": {
                    "clip_vision": ["11", 1],
                    "init_image": ["6", 0],
                    "vae": ["11", 2],
                    "width": int(width),
                    "height": int(height),
                    "video_frames": int(frames),
                    "motion_bucket_id": 127,
                    "fps": max(1, int(fps)),
                    "augmentation_level": 0.0,
                },
            },
            "13": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": int(seed) + 1,
                    "steps": clamped_steps,
                    "cfg": min(clamped_cfg, 3.0),
                    "sampler_name": "euler",
                    "scheduler": "karras",
                    "denoise": 1.0,
                    "model": ["11", 0],
                    "positive": ["12", 0],
                    "negative": ["12", 1],
                    "latent_image": ["12", 2],
                },
            },
            "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["11", 2]}},
            "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "SecondBrainSVD", "images": ["14", 0]}},
        }
        return wf, "svd_img2vid"

    # Path C: LTXVideo native temporal latent generator
    if "EmptyLTXVLatentVideo" in object_info:
        wf = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt_name}},
            "2": {
                "class_type": "EmptyLTXVLatentVideo",
                "inputs": {"width": int(width), "height": int(height), "length": int(frames), "batch_size": 1},
            },
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
            "4": {"class_type": "CLIPTextEncode", "inputs": {"text": negative_prompt, "clip": ["1", 1]}},
            "5": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": int(seed),
                    "steps": clamped_steps,
                    "cfg": clamped_cfg,
                    "sampler_name": "euler",
                    "scheduler": "normal",
                    "denoise": 1.0,
                    "model": ["1", 0],
                    "positive": ["3", 0],
                    "negative": ["4", 0],
                    "latent_image": ["2", 0],
                },
            },
            "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
            "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "SecondBrainLTXV", "images": ["6", 0]}},
        }
        return wf, "ltxv"

    # Path D: Stock ComfyUI progressive latent-chained camera-motion graph (`batch_size=1` per keyframe step,
    # NEVER unchained `EmptyLatentImage(batch_size=frames)` which would produce N unrelated random stills!)
    wf_chain: dict[str, object] = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt_name}},
        "2": {"class_type": "EmptyLatentImage", "inputs": {"width": int(width), "height": int(height), "batch_size": 1}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": negative_prompt, "clip": ["1", 1]}},
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "seed": int(seed),
                "steps": clamped_steps,
                "cfg": clamped_cfg,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["3", 0],
                "negative": ["4", 0],
                "latent_image": ["2", 0],
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "SecondBrainMotion_000", "images": ["6", 0]}},
    }
    prev_latent_id = "5"
    step_crop_w = max(64, (int(width * 0.96) // 8) * 8)
    step_crop_h = max(64, (int(height * 0.96) // 8) * 8)
    next_id = 20
    for idx in range(1, min(int(frames), HARD_MAX_FRAMES)):
        crop_id = str(next_id)
        upscale_id = str(next_id + 1)
        samp_id = str(next_id + 2)
        dec_id = str(next_id + 3)
        save_id = str(next_id + 4)
        next_id += 5

        pan_x = (idx * 8) % max(8, width - step_crop_w + 8)
        pan_y = (idx * 8) % max(8, height - step_crop_h + 8)
        wf_chain[crop_id] = {
            "class_type": "LatentCrop",
            "inputs": {"samples": [prev_latent_id, 0], "width": step_crop_w, "height": step_crop_h, "x": pan_x, "y": pan_y},
        }
        wf_chain[upscale_id] = {
            "class_type": "LatentUpscale",
            "inputs": {"samples": [crop_id, 0], "upscale_method": "bilinear", "width": int(width), "height": int(height), "crop": "disabled"},
        }
        wf_chain[samp_id] = {
            "class_type": "KSampler",
            "inputs": {
                "seed": int(seed) + idx,
                "steps": max(4, min(clamped_steps, 12)),
                "cfg": clamped_cfg,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 0.22,
                "model": ["1", 0],
                "positive": ["3", 0],
                "negative": ["4", 0],
                "latent_image": [upscale_id, 0],
            },
        }
        wf_chain[dec_id] = {"class_type": "VAEDecode", "inputs": {"samples": [samp_id, 0], "vae": ["1", 2]}}
        wf_chain[save_id] = {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": f"SecondBrainMotion_{idx:03d}", "images": [dec_id, 0]},
        }
        prev_latent_id = samp_id
    return wf_chain, "latent_motion_chain"


def _generate_via_comfyui_video(
    *,
    endpoint_url: str,
    prompt: str,
    negative_prompt: str,
    model_name: str | None,
    width: int,
    height: int,
    frames: int,
    fps: int,
    seed: int,
    steps: int,
    cfg_scale: float,
    output_format: str,
    jpeg_quality: int,
    timeout: float,
    poll_interval: float,
    max_polls: int,
    transport: httpx.BaseTransport | None,
    ffmpeg_exe: str | None = None,
    prefer_ffmpeg: bool = False,
    allow_pyav: bool = True,
) -> tuple[bytes, int, str]:
    """Execute temporal ComfyUI video workflow and sanitize outputs into H.264 `.mp4` or `.gif`."""
    del jpeg_quality
    with _open_loopback_client(timeout=timeout, transport=transport) as client:
        ckpt = _select_comfy_checkpoint(client, endpoint_url, model_name)
        object_info = _probe_comfy_object_info(client, endpoint_url)
    workflow, temporal_mode = _build_comfy_video_workflow(
        ckpt_name=ckpt,
        prompt=prompt,
        negative_prompt=negative_prompt,
        width=width,
        height=height,
        frames=frames,
        fps=fps,
        seed=seed,
        steps=steps,
        cfg_scale=cfg_scale,
        object_info=object_info,
    )
    assets = _submit_and_collect_comfy_outputs(
        endpoint_url=endpoint_url,
        workflow=workflow,
        timeout=timeout,
        poll_interval=poll_interval,
        max_polls=max_polls,
        transport=transport,
    )

    first_name, first_bytes = assets[0]
    if first_name.endswith(".mp4") and output_format == "mp4":
        info = inspect_mp4_container(first_bytes)
        if info.get("browser_playable"):
            return (first_bytes, max(2, int(info.get("sample_count") or frames)), f"comfyui_{temporal_mode}_mp4")

    decoded_frames: list[Image.Image] = []
    if first_name.endswith(".gif"):
        with Image.open(io.BytesIO(first_bytes)) as gif_im:
            for seq_frame in ImageSequence.Iterator(gif_im):
                rgb = seq_frame.convert("RGB")
                clean = Image.frombytes("RGB", rgb.size, rgb.tobytes()).resize(
                    (width, height), Image.Resampling.LANCZOS
                )
                decoded_frames.append(clean)
                if len(decoded_frames) >= HARD_MAX_FRAMES:
                    break
    else:
        for _fname, raw_img in assets:
            with Image.open(io.BytesIO(raw_img)) as verify_im:
                verify_im.verify()
            with Image.open(io.BytesIO(raw_img)) as src_im:
                rgb = src_im.convert("RGB")
                clean = Image.frombytes("RGB", rgb.size, rgb.tobytes()).resize(
                    (width, height), Image.Resampling.LANCZOS
                )
                decoded_frames.append(clean)

    if len(decoded_frames) < 2:
        raise CreativeMediaError("ComfyUI video workflow returned fewer than 2 frames.")

    if output_format == "gif":
        return (_encode_frames_to_gif(decoded_frames, fps=fps), len(decoded_frames), f"comfyui_{temporal_mode}_pillow_gif")
    mp4_bytes, mp4_enc = _encode_frames_to_mp4(
        decoded_frames,
        width=width,
        height=height,
        fps=fps,
        ffmpeg_exe=ffmpeg_exe,
        prefer_ffmpeg=prefer_ffmpeg,
        allow_pyav=allow_pyav,
    )
    return (mp4_bytes, len(decoded_frames), f"comfyui_{temporal_mode}_{mp4_enc}")


def _resolve_validated_diffusers_model(model_name: str | None, *, media_type: str = "image") -> str:
    env_key = "BRAIN_DIFFUSERS_VIDEO_MODEL" if media_type == "video" else "BRAIN_DIFFUSERS_MODEL"
    candidates = [
        model_name,
        os.environ.get(env_key) if media_type == "video" else None,
        os.environ.get("BRAIN_DIFFUSERS_MODEL"),
    ]
    model_id = next((str(c).strip() for c in candidates if c and str(c).strip()), "")
    if not model_id:
        cached = _discover_local_diffusers_models(media_type=media_type)
        model_id = cached[0] if cached else ""
    if not model_id:
        raise CreativeMediaError(
            "diffusers_local requires a locally cached model path or repo ID via model_name or BRAIN_DIFFUSERS_MODEL."
        )
    if model_id.lower().startswith(("http://", "https://", "ftp://", "file://")):
        raise CreativeMediaSecurityError("diffusers_local forbids remote HTTP/HTTPS model URLs.")
    if model_id.startswith(("\\\\", "//")) or ".." in model_id or not _SAFE_CHECKPOINT_RE.fullmatch(model_id):
        raise CreativeMediaSecurityError(f"diffusers_local rejected unsafe or UNC model identifier: {model_id!r}")
    return model_id


def _generate_via_diffusers_local(
    *,
    prompt: str,
    negative_prompt: str,
    model_name: str | None,
    width: int,
    height: int,
    seed: int,
    steps: int,
    cfg_scale: float,
    output_format: str,
    gpu_backend: str,
) -> bytes:
    """Strictly offline local HuggingFace `diffusers` image pipeline (`local_files_only=True`)."""
    model_id = _resolve_validated_diffusers_model(model_name, media_type="image")

    try:
        import torch  # type: ignore[import-not-found]
        from diffusers import AutoPipelineForText2Image  # type: ignore[import-not-found]
    except ImportError as exc:
        raise CreativeMediaError("Local 'diffusers' and 'torch' packages are not installed.") from exc

    device = "cuda" if gpu_backend == "cuda" and torch.cuda.is_available() else (
        "mps" if gpu_backend == "metal" and hasattr(torch.backends, "mps") and torch.backends.mps.is_available() else "cpu"
    )
    dtype = torch.float16 if device in {"cuda", "mps"} else torch.float32
    pipe = AutoPipelineForText2Image.from_pretrained(
        model_id,
        local_files_only=True,
        torch_dtype=dtype,
    )
    pipe = pipe.to(device)
    gen = torch.Generator(device="cpu").manual_seed(int(seed))
    out = pipe(
        prompt=prompt,
        negative_prompt=negative_prompt or None,
        width=int(width),
        height=int(height),
        num_inference_steps=max(1, min(80, int(steps))),
        guidance_scale=float(cfg_scale),
        generator=gen,
    )
    pil_img = out.images[0]
    return _sanitize_and_encode_image(
        pil_img,
        target_width=width,
        target_height=height,
        output_format=output_format,
    )


def _generate_via_diffusers_video(
    *,
    prompt: str,
    negative_prompt: str,
    model_name: str | None,
    width: int,
    height: int,
    frames: int,
    fps: int,
    seed: int,
    steps: int,
    cfg_scale: float,
    output_format: str,
    gpu_backend: str,
    ffmpeg_exe: str | None = None,
    prefer_ffmpeg: bool = False,
    allow_pyav: bool = True,
) -> tuple[bytes, int, str]:
    """Strictly offline local HuggingFace `diffusers` video synthesis (`local_files_only=True`)."""
    model_id = _resolve_validated_diffusers_model(model_name, media_type="video")

    try:
        import torch  # type: ignore[import-not-found]
        import diffusers  # type: ignore[import-not-found]
    except ImportError as exc:
        raise CreativeMediaError("Local 'diffusers' and 'torch' packages are not installed.") from exc

    device = "cuda" if gpu_backend == "cuda" and torch.cuda.is_available() else (
        "mps" if gpu_backend == "metal" and hasattr(torch.backends, "mps") and torch.backends.mps.is_available() else "cpu"
    )
    dtype = torch.float16 if device in {"cuda", "mps"} else torch.float32
    gen = torch.Generator(device="cpu").manual_seed(int(seed))
    clamped_steps = max(1, min(80, int(steps)))

    raw_frames: list[Image.Image] = []
    mode_tag = "diffusers_video"

    # Prefer native temporal video pipelines if installed & matching the checkpoint
    text_to_video_cls = getattr(diffusers, "TextToVideoSDPipeline", None)
    if text_to_video_cls is not None:
        try:
            vpipe = text_to_video_cls.from_pretrained(model_id, local_files_only=True, torch_dtype=dtype)
            vpipe = vpipe.to(device)
            vout = vpipe(
                prompt=prompt,
                negative_prompt=negative_prompt or None,
                width=int(width),
                height=int(height),
                num_frames=int(frames),
                num_inference_steps=clamped_steps,
                guidance_scale=float(cfg_scale),
                generator=gen,
            )
            seq = getattr(vout, "frames", None) or getattr(vout, "images", None) or []
            if seq and isinstance(seq[0], list):
                seq = seq[0]
            for item in list(seq)[:HARD_MAX_FRAMES]:
                if isinstance(item, Image.Image):
                    raw_frames.append(item.convert("RGB"))
            if raw_frames:
                mode_tag = "diffusers_t2v"
        except Exception:
            raw_frames = []

    if not raw_frames:
        auto_img_cls = getattr(diffusers, "AutoPipelineForText2Image", None)
        if auto_img_cls is None:
            raise CreativeMediaError("Diffusers installation lacks TextToVideoSDPipeline and AutoPipelineForText2Image.")
        ipipe = auto_img_cls.from_pretrained(model_id, local_files_only=True, torch_dtype=dtype)
        ipipe = ipipe.to(device)
        out = ipipe(
            prompt=prompt,
            negative_prompt=negative_prompt or None,
            width=int(width),
            height=int(height),
            num_inference_steps=clamped_steps,
            guidance_scale=float(cfg_scale),
            generator=gen,
        )
        keyframe = out.images[0].convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
        # Try image-to-image sequential camera-motion conditioning when AutoPipelineForImage2Image exists
        img2img_cls = getattr(diffusers, "AutoPipelineForImage2Image", None)
        img2img_pipe = None
        if img2img_cls is not None:
            try:
                img2img_pipe = img2img_cls.from_pipe(ipipe)
            except Exception:
                img2img_pipe = None
        raw_frames.append(keyframe)
        prev_im = keyframe
        crop_w = max(64, int(width * 0.94))
        crop_h = max(64, int(height * 0.94))
        for idx in range(1, int(frames)):
            t = idx / float(max(1, frames - 1))
            dx = int((width - crop_w) * 0.5 * (1.0 + math.sin(math.pi * t - 0.5 * math.pi)))
            dy = int((height - crop_h) * 0.5 * (1.0 + math.cos(math.pi * t)))
            warped = prev_im.crop((dx, dy, dx + crop_w, dy + crop_h)).resize((width, height), Image.Resampling.BICUBIC)
            if img2img_pipe is not None:
                step_gen = torch.Generator(device="cpu").manual_seed(int(seed) + idx)
                step_out = img2img_pipe(
                    prompt=prompt,
                    negative_prompt=negative_prompt or None,
                    image=warped,
                    strength=0.22,
                    num_inference_steps=max(4, min(clamped_steps, 12)),
                    guidance_scale=float(cfg_scale),
                    generator=step_gen,
                )
                prev_im = step_out.images[0].convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
            else:
                prev_im = warped
            raw_frames.append(prev_im)
        mode_tag = "diffusers_i2i_motion" if img2img_pipe is not None else "diffusers_motion"

    if len(raw_frames) < 2:
        raise CreativeMediaError("diffusers_local video synthesis produced fewer than 2 frames.")

    clean_frames = [
        Image.frombytes("RGB", f.size, f.convert("RGB").tobytes()).resize((width, height), Image.Resampling.LANCZOS)
        for f in raw_frames
    ]
    if output_format == "gif":
        return (_encode_frames_to_gif(clean_frames, fps=fps), len(clean_frames), f"{mode_tag}_pillow_gif")
    mp4_bytes, mp4_enc = _encode_frames_to_mp4(
        clean_frames,
        width=width,
        height=height,
        fps=fps,
        ffmpeg_exe=ffmpeg_exe,
        prefer_ffmpeg=prefer_ffmpeg,
        allow_pyav=allow_pyav,
    )
    return (mp4_bytes, len(clean_frames), f"{mode_tag}_{mp4_enc}")


# ─── Atomic Vault Persistence & Optional Working Note Attachment ─────────────


def _persist_generated_artifact(
    *,
    paths: dict[str, str],
    media_prefix: str,
    media_type: str,
    output_format: str,
    media_bytes: bytes,
    width: int,
    height: int,
    frames: int,
    fps: int | None,
    requested_backend: str,
    backend_used: str,
    encoder_used: str,
    style: str,
    seed: int,
    title: str,
    prompt_sanitized: str,
    negative_prompt_sanitized: str,
    pii_redaction_count: int,
    ground_in_vault: bool,
    project_scope: str | None,
    grounded_atom_ids: Sequence[str],
    grounded_claims: Sequence[dict[str, str]],
    attach_to_note: bool,
    hardware_tier: str,
    hardware_override_used: bool,
    degraded_fallback: bool = False,
    warning: str | None = None,
) -> GeneratedMediaArtifact:
    generated_dir = paths["generated_dir"]
    brain_dir = paths["brain_dir"]
    os.makedirs(generated_dir, exist_ok=True)
    if _is_reparse_or_link(generated_dir):
        raise CreativeMediaSecurityError(f"Refusing to write media into symlinked directory: {generated_dir}")

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    stamp = now_utc.strftime("%Y%m%d-%H%M%S")
    created_iso = now_utc.isoformat(timespec="seconds")
    ext = output_format.lower()
    media_sha = hashlib.sha256(media_bytes).hexdigest()
    duration_s = round(frames / float(fps), 3) if (media_type == "video" and fps) else None

    with review_lock(paths["lock_path"]):
        while True:
            media_id = f"{media_prefix}-{stamp}-{secrets.token_hex(4)}"
            media_filename = f"{media_id}.{ext}"
            meta_filename = f"{media_id}.meta.json"
            media_path = safe_file_in_dir(
                generated_dir,
                media_filename,
                allowed_exts={f".{ext}"},
                must_exist=False,
            )
            meta_path = safe_file_in_dir(
                generated_dir,
                meta_filename,
                allowed_exts={".json"},
                must_exist=False,
            )
            if not media_path or not meta_path:
                raise CreativeMediaSecurityError("Generated unsafe media destination path.")
            if not os.path.lexists(media_path) and not os.path.lexists(meta_path):
                break

        rel_media = vault_relative_path(media_path, brain_dir=brain_dir)
        rel_meta = vault_relative_path(meta_path, brain_dir=brain_dir)

        meta_dict: dict[str, object] = {
            "schema_version": 1,
            "media_id": media_id,
            "media_type": media_type,
            "output_format": ext,
            "mime_type": _MIME_TYPES[ext],
            "filename": media_filename,
            "relative_path": rel_media,
            "metadata_relative_path": rel_meta,
            "sha256": media_sha,
            "size_bytes": len(media_bytes),
            "width": width,
            "height": height,
            "frames": frames,
            "fps": fps,
            "duration_seconds": duration_s,
            "requested_backend": requested_backend,
            "backend_used": backend_used,
            "encoder_used": encoder_used,
            "style": style,
            "seed": seed,
            "title": title,
            "prompt_sanitized": prompt_sanitized,
            "negative_prompt_sanitized": negative_prompt_sanitized,
            "pii_redaction_count": pii_redaction_count,
            "ground_in_vault": ground_in_vault,
            "project_scope": project_scope,
            "grounded_atom_ids": list(grounded_atom_ids),
            "grounded_claims": [dict(c) for c in grounded_claims],
            "epistemic_status": "unverified_creative_artifact",
            "canonical_atoms_created": 0,
            "attached_note_filename": None,
            "hardware_tier": hardware_tier,
            "hardware_override_used": hardware_override_used,
            "degraded_fallback": bool(degraded_fallback),
            "warning": warning,
            "created_at": created_iso,
        }
        atomic_write_bytes(media_path, media_bytes)
        atomic_write_bytes(meta_path, (json.dumps(meta_dict, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))

    attached_note: str | None = None
    if attach_to_note:
        note_tags: list[str] = ["creative-media", f"media:{media_type}"]
        if project_scope:
            note_tags.append(project_scope)
        note_title = title or f"Creative {media_type.title()}: {prompt_sanitized[:64]}"
        citation_lines = (
            "\n".join(f"- {c['claim']} {c['citation']}" for c in grounded_claims)
            if grounded_claims
            else "- None (standalone creative synthesis)"
        )
        note_body = (
            f"Generated local {media_type} artifact (`{rel_media}`).\n\n"
            f"- **Media ID**: `{media_id}`\n"
            f"- **Backend**: `{backend_used}` (`{encoder_used}`)\n"
            f"- **Dimensions**: `{width}x{height}`"
            + (f" (`{frames}` frames @ `{fps}` fps)" if media_type == "video" else "")
            + f"\n- **SHA-256**: `{media_sha}`\n"
            f"- **Epistemic Status**: Unverified creative working note (never auto-promoted to `2-atoms/`).\n\n"
            f"## Prompt\n{prompt_sanitized}\n\n"
            f"## Grounded Vault Citations\n{citation_lines}\n"
        )
        note_res = create_note(
            note_title,
            note_body,
            origin="owner_note",
            tags=tuple(note_tags),
            source_path=rel_media,
            paths=NotePaths.for_brain(brain_dir),
        )
        if note_res.get("status") == "saved" and isinstance(note_res.get("filename"), str):
            attached_note = str(note_res["filename"])
            meta_dict["attached_note_filename"] = attached_note
            with review_lock(paths["lock_path"]):
                atomic_write_bytes(
                    meta_path,
                    (json.dumps(meta_dict, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                )

    _append_creative_audit(
        paths["audit_path"],
        media_type=media_type,
        requested_backend=requested_backend,
        backend_used=backend_used,
        outcome="generated",
        project_scope=project_scope,
        media_id=media_id,
        sha256=media_sha,
        grounded_atom_ids=grounded_atom_ids,
        pii_redaction_count=pii_redaction_count,
        hardware_tier=hardware_tier,
        hardware_override_used=hardware_override_used,
    )

    return GeneratedMediaArtifact(
        schema_version=1,
        media_id=media_id,
        media_type=media_type,
        output_format=ext,
        mime_type=_MIME_TYPES[ext],
        filename=media_filename,
        relative_path=rel_media,
        metadata_relative_path=rel_meta,
        file_path=media_path,
        metadata_path=meta_path,
        sha256=media_sha,
        size_bytes=len(media_bytes),
        width=width,
        height=height,
        frames=frames,
        fps=fps,
        duration_seconds=duration_s,
        requested_backend=requested_backend,
        backend_used=backend_used,
        encoder_used=encoder_used,
        style=style,
        seed=seed,
        title=title,
        prompt_sanitized=prompt_sanitized,
        negative_prompt_sanitized=negative_prompt_sanitized,
        pii_redaction_count=pii_redaction_count,
        ground_in_vault=ground_in_vault,
        project_scope=project_scope,
        grounded_atom_ids=tuple(grounded_atom_ids),
        grounded_claims=tuple(dict(c) for c in grounded_claims),
        epistemic_status="unverified_creative_artifact",
        canonical_atoms_created=0,
        attached_note_filename=attached_note,
        hardware_tier=hardware_tier,
        hardware_override_used=hardware_override_used,
        created_at=created_iso,
        degraded_fallback=bool(degraded_fallback),
        warning=warning,
    )


# ─── Public Generation & Vault Inspection Entrypoints ────────────────────────


def generate_local_image(
    prompt: str,
    *,
    brain_dir: str | Path | None = None,
    backend: str = "auto",
    style: str = "concept_card",
    output_format: str = "png",
    width: int = 768,
    height: int = 512,
    seed: int | None = None,
    negative_prompt: str = "",
    title: str = "",
    steps: int = 20,
    cfg_scale: float = 7.0,
    sampler_name: str = "Euler a",
    model_name: str | None = None,
    ground_in_vault: bool = False,
    project_scope: str | None = None,
    include_preferences: bool = False,
    max_grounding_atoms: int = 4,
    attach_to_note: bool = False,
    redact_pii: bool = True,
    strict_dlp: bool = True,
    allow_hardware_override: bool = False,
    allow_procedural_fallback: bool = False,
    policy_path: Path | None = None,
    hardware_profile: HardwareCapabilityProfile | None = None,
    sd_webui_url: str | None = None,
    comfyui_url: str | None = None,
    http_transport: httpx.BaseTransport | None = None,
    request_timeout: float = 60.0,
    comfy_poll_interval: float = 0.25,
    comfy_max_polls: int = 120,
) -> GeneratedMediaArtifact:
    """Generate a local image (`png` or `webp`) with DLP, hardware gating, and optional vault grounding."""
    paths = _resolve_creative_paths(brain_dir)
    norm_scope = normalize_project_tag(project_scope) if (project_scope and str(project_scope).strip()) else None

    clean_fmt = str(output_format or "").strip().lower()
    if clean_fmt not in IMAGE_FORMATS:
        raise ValueError(f"Unsupported image output_format {output_format!r}; expected one of {list(IMAGE_FORMATS)}.")

    clean_style = str(style or "").strip().lower()
    if clean_style not in PROCEDURAL_STYLES:
        raise ValueError(f"Unsupported creative style {style!r}; expected one of {list(PROCEDURAL_STYLES)}.")

    req_backend = str(backend or "auto").strip().lower()

    # 1. Validate any supplied or env-configured loopback URLs immediately (fail-closed before probing).
    raw_sd_url = sd_webui_url if sd_webui_url is not None else (os.environ.get("BRAIN_SD_WEBUI_URL") or DEFAULT_SD_WEBUI_URL)
    raw_comfy_url = comfyui_url if comfyui_url is not None else (os.environ.get("BRAIN_COMFYUI_URL") or DEFAULT_COMFYUI_URL)
    try:
        if sd_webui_url is not None or req_backend == "sd_webui_local" or os.environ.get("BRAIN_SD_WEBUI_URL"):
            validated_sd_url = validate_local_media_endpoint(raw_sd_url, default_port=7860)
        else:
            validated_sd_url = DEFAULT_SD_WEBUI_URL
        if comfyui_url is not None or req_backend == "comfyui_local" or os.environ.get("BRAIN_COMFYUI_URL"):
            validated_comfy_url = validate_local_media_endpoint(raw_comfy_url, default_port=8188)
        else:
            validated_comfy_url = DEFAULT_COMFYUI_URL
    except CreativeMediaSecurityError:
        _append_creative_audit(
            paths["audit_path"],
            media_type="image",
            requested_backend=req_backend,
            backend_used="none",
            outcome="blocked_security",
            project_scope=norm_scope,
            risk_flags=["non_loopback_or_unsafe_endpoint"],
        )
        raise

    # 2. DLP & Adversarial Prompt Risk Gate
    sanitized_prompt, sanitized_neg, sanitized_title, pii_count = _inspect_and_sanitize_creative_inputs(
        prompt=prompt,
        negative_prompt=negative_prompt,
        title=title,
        redact_pii=redact_pii,
        strict_dlp=strict_dlp,
        audit_path=paths["audit_path"],
        media_type="image",
        requested_backend=req_backend,
        project_scope=norm_scope,
    )

    # 3. Hardware Capability Gate & Backend Resolution
    profile = hardware_profile or detect_hardware_profile(use_cache=True)
    eff_allow_fallback = bool(
        allow_procedural_fallback
        or (os.environ.get("BRAIN_MEDIA_ALLOW_PROCEDURAL_FALLBACK") or "").strip() == "1"
    )
    degraded_fallback = False
    fallback_warning: str | None = None
    try:
        if req_backend == "auto":
            base_gate = enforce_creative_hardware_gate(
                backend="auto",
                media_type="image",
                width=width,
                height=height,
                allow_hardware_override=allow_hardware_override,
                hardware_profile=profile,
            )
            status = get_creative_media_status(
                hardware_profile=profile,
                sd_webui_url=validated_sd_url,
                comfyui_url=validated_comfy_url,
                http_transport=http_transport,
                diffusers_model_name=model_name,
            )
            selected_backend = status.recommended_image_backend
            if selected_backend in {"none", ""}:
                if eff_allow_fallback:
                    selected_backend = "builtin_procedural_local"
                    degraded_fallback = True
                    fallback_warning = (
                        "DEGRADED FALLBACK: No local neural image backend was ready "
                        "(SD WebUI :7860, ComfyUI :8188, and diffusers_local were offline/unconfigured); "
                        "rendered 2D Pillow procedural card instead."
                    )
                    warnings.warn(fallback_warning, RuntimeWarning, stacklevel=2)
                else:
                    _append_creative_audit(
                        paths["audit_path"],
                        media_type="image",
                        requested_backend=req_backend,
                        backend_used="none",
                        outcome="unavailable_no_neural_backend",
                        project_scope=norm_scope,
                        hardware_tier=profile.tier_id,
                    )
                    raise CreativeMediaUnavailableError(
                        "No local neural image backend is ready (checked SD WebUI :7860, ComfyUI :8188, and diffusers_local). "
                        "Start a local diffusion daemon, set BRAIN_DIFFUSERS_MODEL, or pass backend='builtin_procedural_local' / allow_procedural_fallback=True."
                    )
            gate_decision = replace(base_gate, backend=selected_backend)
        else:
            daemon_online = False
            if req_backend == "sd_webui_local" and profile.tier_id == "tier_2_cpu_balanced":
                daemon_online, _, _ = _probe_sd_webui_daemon(validated_sd_url, transport=http_transport)
            elif req_backend == "comfyui_local" and profile.tier_id == "tier_2_cpu_balanced":
                daemon_online, _, _ = _probe_comfyui_daemon(validated_comfy_url, transport=http_transport)
            elif req_backend == "diffusers_local" and profile.tier_id == "tier_2_cpu_balanced":
                daemon_online = bool(
                    _is_diffusers_installed()
                    and _discover_local_diffusers_models(explicit_model=model_name, media_type="image")
                )
            gate_decision = enforce_creative_hardware_gate(
                backend=req_backend,
                media_type="image",
                width=width,
                height=height,
                allow_hardware_override=allow_hardware_override,
                hardware_profile=profile,
                daemon_online=daemon_online,
            )
            selected_backend = req_backend
    except CreativeMediaHardwareGateError:
        _append_creative_audit(
            paths["audit_path"],
            media_type="image",
            requested_backend=req_backend,
            backend_used="none",
            outcome="blocked_hardware_gate",
            project_scope=norm_scope,
            hardware_tier=profile.tier_id,
            hardware_override_used=allow_hardware_override,
        )
        raise

    # 4. Scoped Vault Grounding
    grounded_ids: list[str] = []
    grounded_claims: list[dict[str, str]] = []
    if ground_in_vault:
        grounded_ids, grounded_claims, extra_pii = _retrieve_grounded_vault_claims(
            f"{sanitized_title} {sanitized_prompt}".strip(),
            paths=paths,
            project_scope=norm_scope,
            include_preferences=include_preferences,
            redact_pii=redact_pii,
            max_atoms=max_grounding_atoms,
            policy_path=policy_path,
            audit_path=paths["audit_path"],
            media_type="image",
            requested_backend=req_backend,
        )
        pii_count += extra_pii

    eff_seed = _derive_seed(sanitized_prompt, seed)

    # 5. Execute Local Backend
    if selected_backend == "builtin_procedural_local":
        pil_frame = _render_procedural_frame(
            width=width,
            height=height,
            style=clean_style,
            sanitized_prompt=sanitized_prompt,
            title=sanitized_title,
            seed_int=eff_seed,
            grounded_claims=grounded_claims,
            project_scope=norm_scope,
            supersample=2,
        )
        image_bytes = _sanitize_and_encode_image(
            pil_frame,
            target_width=width,
            target_height=height,
            output_format=clean_fmt,
        )
        encoder_used = f"pillow_{clean_fmt}"
    elif selected_backend == "sd_webui_local":
        neural_prompt = _compose_neural_prompt(
            sanitized_prompt,
            style=clean_style,
            title=sanitized_title,
            grounded_claims=grounded_claims,
        )
        image_bytes = _generate_via_sd_webui_local(
            endpoint_url=validated_sd_url,
            prompt=neural_prompt,
            negative_prompt=sanitized_neg,
            width=width,
            height=height,
            seed=eff_seed,
            steps=steps,
            cfg_scale=cfg_scale,
            sampler_name=sampler_name,
            output_format=clean_fmt,
            timeout=request_timeout,
            transport=http_transport,
        )
        encoder_used = f"sd_webui_sanitized_{clean_fmt}"
    elif selected_backend == "comfyui_local":
        neural_prompt = _compose_neural_prompt(
            sanitized_prompt,
            style=clean_style,
            title=sanitized_title,
            grounded_claims=grounded_claims,
        )
        image_bytes = _generate_via_comfyui_image(
            endpoint_url=validated_comfy_url,
            prompt=neural_prompt,
            negative_prompt=sanitized_neg,
            model_name=model_name,
            width=width,
            height=height,
            seed=eff_seed,
            steps=steps,
            cfg_scale=cfg_scale,
            output_format=clean_fmt,
            timeout=request_timeout,
            poll_interval=comfy_poll_interval,
            max_polls=comfy_max_polls,
            transport=http_transport,
        )
        encoder_used = f"comfyui_sanitized_{clean_fmt}"
    elif selected_backend == "diffusers_local":
        neural_prompt = _compose_neural_prompt(
            sanitized_prompt,
            style=clean_style,
            title=sanitized_title,
            grounded_claims=grounded_claims,
        )
        image_bytes = _generate_via_diffusers_local(
            prompt=neural_prompt,
            negative_prompt=sanitized_neg,
            model_name=model_name,
            width=width,
            height=height,
            seed=eff_seed,
            steps=steps,
            cfg_scale=cfg_scale,
            output_format=clean_fmt,
            gpu_backend=profile.gpu_backend,
        )
        encoder_used = f"diffusers_sanitized_{clean_fmt}"
    else:  # pragma: no cover
        raise CreativeMediaHardwareGateError(f"Unsupported image backend: {selected_backend!r}")

    return _persist_generated_artifact(
        paths=paths,
        media_prefix="img",
        media_type="image",
        output_format=clean_fmt,
        media_bytes=image_bytes,
        width=width,
        height=height,
        frames=1,
        fps=None,
        requested_backend=req_backend,
        backend_used=selected_backend,
        encoder_used=encoder_used,
        style=clean_style,
        seed=eff_seed,
        title=sanitized_title,
        prompt_sanitized=sanitized_prompt,
        negative_prompt_sanitized=sanitized_neg,
        pii_redaction_count=pii_count,
        ground_in_vault=ground_in_vault,
        project_scope=norm_scope,
        grounded_atom_ids=grounded_ids,
        grounded_claims=grounded_claims,
        attach_to_note=attach_to_note,
        hardware_tier=gate_decision.creative_tier,
        hardware_override_used=gate_decision.hardware_override_used,
        degraded_fallback=degraded_fallback,
        warning=fallback_warning,
    )


def generate_local_video(
    prompt: str,
    *,
    brain_dir: str | Path | None = None,
    backend: str = "auto",
    style: str = "concept_card",
    output_format: str = "mp4",
    width: int = 640,
    height: int = 384,
    frames: int = 16,
    fps: int = 12,
    seed: int | None = None,
    negative_prompt: str = "",
    title: str = "",
    steps: int = 16,
    cfg_scale: float = 6.5,
    model_name: str | None = None,
    prefer_ffmpeg: bool = False,
    jpeg_quality: int = 88,
    ground_in_vault: bool = False,
    project_scope: str | None = None,
    include_preferences: bool = False,
    max_grounding_atoms: int = 4,
    attach_to_note: bool = False,
    redact_pii: bool = True,
    strict_dlp: bool = True,
    allow_hardware_override: bool = False,
    allow_procedural_fallback: bool = False,
    policy_path: Path | None = None,
    hardware_profile: HardwareCapabilityProfile | None = None,
    comfyui_url: str | None = None,
    http_transport: httpx.BaseTransport | None = None,
    ffmpeg_path_override: str | None = None,
    request_timeout: float = 90.0,
    comfy_poll_interval: float = 0.25,
    comfy_max_polls: int = 160,
) -> GeneratedMediaArtifact:
    """Generate a local video (`mp4` or `gif`) 100% offline with pure-Python ISO-BMFF muxing or loopback ComfyUI."""
    paths = _resolve_creative_paths(brain_dir)
    norm_scope = normalize_project_tag(project_scope) if (project_scope and str(project_scope).strip()) else None

    clean_fmt = str(output_format or "").strip().lower()
    if clean_fmt not in VIDEO_FORMATS:
        raise ValueError(f"Unsupported video output_format {output_format!r}; expected one of {list(VIDEO_FORMATS)}.")

    clean_style = str(style or "").strip().lower()
    if clean_style not in PROCEDURAL_STYLES:
        raise ValueError(f"Unsupported creative style {style!r}; expected one of {list(PROCEDURAL_STYLES)}.")

    req_backend = str(backend or "auto").strip().lower()

    # 1. Loopback Endpoint Security Check
    raw_comfy_url = comfyui_url if comfyui_url is not None else (os.environ.get("BRAIN_COMFYUI_URL") or DEFAULT_COMFYUI_URL)
    try:
        if comfyui_url is not None or req_backend == "comfyui_local" or os.environ.get("BRAIN_COMFYUI_URL"):
            validated_comfy_url = validate_local_media_endpoint(raw_comfy_url, default_port=8188)
        else:
            validated_comfy_url = DEFAULT_COMFYUI_URL
    except CreativeMediaSecurityError:
        _append_creative_audit(
            paths["audit_path"],
            media_type="video",
            requested_backend=req_backend,
            backend_used="none",
            outcome="blocked_security",
            project_scope=norm_scope,
            risk_flags=["non_loopback_or_unsafe_endpoint"],
        )
        raise

    # 2. DLP & Adversarial Prompt Risk Gate
    sanitized_prompt, sanitized_neg, sanitized_title, pii_count = _inspect_and_sanitize_creative_inputs(
        prompt=prompt,
        negative_prompt=negative_prompt,
        title=title,
        redact_pii=redact_pii,
        strict_dlp=strict_dlp,
        audit_path=paths["audit_path"],
        media_type="video",
        requested_backend=req_backend,
        project_scope=norm_scope,
    )

    # 3. Hardware Capability Gate & Backend Resolution (video)
    profile = hardware_profile or detect_hardware_profile(use_cache=True)
    eff_allow_fallback = bool(
        allow_procedural_fallback
        or (os.environ.get("BRAIN_MEDIA_ALLOW_PROCEDURAL_FALLBACK") or "").strip() == "1"
    )
    degraded_fallback = False
    fallback_warning: str | None = None
    try:
        if req_backend == "auto":
            base_gate = enforce_creative_hardware_gate(
                backend="auto",
                media_type="video",
                width=width,
                height=height,
                frames=frames,
                fps=fps,
                allow_hardware_override=allow_hardware_override,
                hardware_profile=profile,
            )
            status = get_creative_media_status(
                hardware_profile=profile,
                comfyui_url=validated_comfy_url,
                http_transport=http_transport,
                ffmpeg_path_override=ffmpeg_path_override,
                diffusers_model_name=model_name,
            )
            selected_backend = status.recommended_video_backend
            if selected_backend in {"none", ""}:
                if eff_allow_fallback:
                    selected_backend = "builtin_procedural_local"
                    degraded_fallback = True
                    fallback_warning = (
                        "DEGRADED FALLBACK: No local neural video backend was ready "
                        "(ComfyUI :8188 and diffusers_local were offline/unconfigured); "
                        "rendered 2D Pillow procedural animation instead."
                    )
                    warnings.warn(fallback_warning, RuntimeWarning, stacklevel=2)
                else:
                    _append_creative_audit(
                        paths["audit_path"],
                        media_type="video",
                        requested_backend=req_backend,
                        backend_used="none",
                        outcome="unavailable_no_neural_backend",
                        project_scope=norm_scope,
                        hardware_tier=profile.tier_id,
                    )
                    raise CreativeMediaUnavailableError(
                        "No local neural video backend is ready (checked ComfyUI :8188 and diffusers_local). "
                        "Start ComfyUI, set BRAIN_DIFFUSERS_MODEL / BRAIN_DIFFUSERS_VIDEO_MODEL, or pass backend='builtin_procedural_local' / allow_procedural_fallback=True."
                    )
            gate_decision = replace(base_gate, backend=selected_backend)
        else:
            daemon_online = False
            if req_backend == "comfyui_local" and profile.tier_id == "tier_2_cpu_balanced":
                daemon_online, _, _ = _probe_comfyui_daemon(validated_comfy_url, transport=http_transport)
            elif req_backend == "diffusers_local" and profile.tier_id == "tier_2_cpu_balanced":
                daemon_online = bool(
                    _is_diffusers_installed()
                    and _discover_local_diffusers_models(explicit_model=model_name, media_type="video")
                )
            gate_decision = enforce_creative_hardware_gate(
                backend=req_backend,
                media_type="video",
                width=width,
                height=height,
                frames=frames,
                fps=fps,
                allow_hardware_override=allow_hardware_override,
                hardware_profile=profile,
                daemon_online=daemon_online,
            )
            selected_backend = req_backend
    except CreativeMediaHardwareGateError:
        _append_creative_audit(
            paths["audit_path"],
            media_type="video",
            requested_backend=req_backend,
            backend_used="none",
            outcome="blocked_hardware_gate",
            project_scope=norm_scope,
            hardware_tier=profile.tier_id,
            hardware_override_used=allow_hardware_override,
        )
        raise

    # 4. Scoped Vault Grounding
    grounded_ids: list[str] = []
    grounded_claims: list[dict[str, str]] = []
    if ground_in_vault:
        grounded_ids, grounded_claims, extra_pii = _retrieve_grounded_vault_claims(
            f"{sanitized_title} {sanitized_prompt}".strip(),
            paths=paths,
            project_scope=norm_scope,
            include_preferences=include_preferences,
            redact_pii=redact_pii,
            max_atoms=max_grounding_atoms,
            policy_path=policy_path,
            audit_path=paths["audit_path"],
            media_type="video",
            requested_backend=req_backend,
        )
        pii_count += extra_pii

    eff_seed = _derive_seed(sanitized_prompt, seed)
    explicit_no_ffmpeg = ffmpeg_path_override is not None and not ffmpeg_path_override.strip()
    ffmpeg_exe = (
        (ffmpeg_path_override.strip() or None)
        if ffmpeg_path_override is not None
        else shutil.which("ffmpeg")
    )

    # 5. Execute Local Video Backend
    if selected_backend == "comfyui_local":
        neural_prompt = _compose_neural_prompt(
            sanitized_prompt,
            style=clean_style,
            title=sanitized_title,
            grounded_claims=grounded_claims,
        )
        video_bytes, actual_frames, encoder_used = _generate_via_comfyui_video(
            endpoint_url=validated_comfy_url,
            prompt=neural_prompt,
            negative_prompt=sanitized_neg,
            model_name=model_name,
            width=width,
            height=height,
            frames=frames,
            fps=fps,
            seed=eff_seed,
            steps=steps,
            cfg_scale=cfg_scale,
            output_format=clean_fmt,
            jpeg_quality=jpeg_quality,
            timeout=request_timeout,
            poll_interval=comfy_poll_interval,
            max_polls=comfy_max_polls,
            transport=http_transport,
            ffmpeg_exe=ffmpeg_exe,
            prefer_ffmpeg=prefer_ffmpeg,
            allow_pyav=(not explicit_no_ffmpeg),
        )
    elif selected_backend == "diffusers_local":
        neural_prompt = _compose_neural_prompt(
            sanitized_prompt,
            style=clean_style,
            title=sanitized_title,
            grounded_claims=grounded_claims,
        )
        video_bytes, actual_frames, encoder_used = _generate_via_diffusers_video(
            prompt=neural_prompt,
            negative_prompt=sanitized_neg,
            model_name=model_name,
            width=width,
            height=height,
            frames=frames,
            fps=fps,
            seed=eff_seed,
            steps=steps,
            cfg_scale=cfg_scale,
            output_format=clean_fmt,
            gpu_backend=profile.gpu_backend,
            ffmpeg_exe=ffmpeg_exe,
            prefer_ffmpeg=prefer_ffmpeg,
            allow_pyav=(not explicit_no_ffmpeg),
        )
    elif selected_backend in {"builtin_procedural_local", "ffmpeg_slideshow_local"}:
        supersample = 2 if max(width, height) <= 768 else 1
        rendered_frames = [
            _render_procedural_frame(
                width=width,
                height=height,
                style=clean_style,
                sanitized_prompt=sanitized_prompt,
                title=sanitized_title,
                seed_int=eff_seed,
                grounded_claims=grounded_claims,
                project_scope=norm_scope,
                frame_index=idx,
                total_frames=frames,
                fps=fps,
                supersample=supersample,
            )
            for idx in range(frames)
        ]
        actual_frames = len(rendered_frames)
        if clean_fmt == "gif":
            video_bytes = _encode_frames_to_gif(rendered_frames, fps=fps)
            encoder_used = "pillow_gif"
        else:
            video_bytes, encoder_used = _encode_frames_to_mp4(
                rendered_frames,
                width=width,
                height=height,
                fps=fps,
                ffmpeg_exe=ffmpeg_exe if not explicit_no_ffmpeg else None,
                prefer_ffmpeg=bool(prefer_ffmpeg or selected_backend == "ffmpeg_slideshow_local"),
                allow_pyav=(not explicit_no_ffmpeg),
            )
    else:  # pragma: no cover
        raise CreativeMediaHardwareGateError(f"Unsupported video backend: {selected_backend!r}")

    return _persist_generated_artifact(
        paths=paths,
        media_prefix="vid",
        media_type="video",
        output_format=clean_fmt,
        media_bytes=video_bytes,
        width=width,
        height=height,
        frames=actual_frames,
        fps=fps,
        requested_backend=req_backend,
        backend_used=selected_backend,
        encoder_used=encoder_used,
        style=clean_style,
        seed=eff_seed,
        title=sanitized_title,
        prompt_sanitized=sanitized_prompt,
        negative_prompt_sanitized=sanitized_neg,
        pii_redaction_count=pii_count,
        ground_in_vault=ground_in_vault,
        project_scope=norm_scope,
        grounded_atom_ids=grounded_ids,
        grounded_claims=grounded_claims,
        attach_to_note=attach_to_note,
        hardware_tier=gate_decision.creative_tier,
        hardware_override_used=gate_decision.hardware_override_used,
        degraded_fallback=degraded_fallback,
        warning=fallback_warning,
    )


def list_generated_media(
    brain_dir: str | Path | None = None,
    media_type: str | None = None,
    project_scope: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """List generated media metadata sidecars under `brain/sources/generated/` newest-first."""
    paths = _resolve_creative_paths(brain_dir)
    generated_dir = paths["generated_dir"]
    if not os.path.isdir(generated_dir):
        return []

    clean_type = str(media_type).strip().lower() if (media_type and str(media_type).strip()) else None
    if clean_type and clean_type not in {"image", "video"}:
        raise ValueError(f"Unsupported media_type filter: {media_type!r}")

    norm_scope = normalize_project_tag(project_scope) if (project_scope and str(project_scope).strip()) else None
    eff_limit = max(1, min(int(limit), 500))

    items: list[dict[str, object]] = []
    for entry in sorted(os.listdir(generated_dir), reverse=True):
        if not entry.endswith(".meta.json"):
            continue
        meta_path = safe_file_in_dir(generated_dir, entry, allowed_exts={".json"}, must_exist=True)
        if not meta_path or _is_reparse_or_link(meta_path):
            continue
        try:
            st = os.stat(meta_path)
            if st.st_size <= 0 or st.st_size > MAX_META_FILE_BYTES:
                continue
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue

        m_id = str(meta.get("media_id") or "").strip()
        m_file = str(meta.get("filename") or "").strip()
        m_type = str(meta.get("media_type") or "").strip().lower()
        if not m_id or not _SAFE_MEDIA_ID_RE.fullmatch(m_id):
            continue
        if clean_type and m_type != clean_type:
            continue

        if norm_scope:
            rec_scope = str(meta.get("project_scope") or "").strip()
            if not rec_scope or not project_tags_match((rec_scope,), (norm_scope,)):
                continue

        asset_path = safe_file_in_dir(
            generated_dir,
            m_file,
            allowed_exts={".png", ".webp", ".mp4", ".gif"},
            must_exist=True,
        )
        if not asset_path or _is_reparse_or_link(asset_path):
            continue

        enriched = dict(meta)
        enriched["file_path"] = asset_path
        enriched["metadata_path"] = meta_path
        items.append(enriched)

    items.sort(key=lambda r: (str(r.get("created_at") or ""), str(r.get("media_id") or "")), reverse=True)
    return items[:eff_limit]


def get_generated_media_file(
    brain_dir: str | Path | None = None,
    media_id: str = "",
    *,
    verify_sha256: bool = True,
) -> dict[str, object]:
    """Resolve and verify a generated media artifact and its sidecar by `media_id`."""
    clean_id = str(media_id or "").strip()
    if (
        not clean_id
        or ".." in clean_id
        or "/" in clean_id
        or "\\" in clean_id
        or ":" in clean_id
        or clean_id.lower() in _WINDOWS_RESERVED_STEMS
        or not _SAFE_MEDIA_ID_RE.fullmatch(clean_id)
    ):
        raise CreativeMediaSecurityError(f"Invalid or unsafe media_id: {media_id!r}")

    paths = _resolve_creative_paths(brain_dir)
    generated_dir = paths["generated_dir"]
    if not os.path.isdir(generated_dir):
        raise FileNotFoundError(f"Generated media artifact not found: {clean_id}")

    meta_filename = f"{clean_id}.meta.json"
    meta_path = safe_file_in_dir(generated_dir, meta_filename, allowed_exts={".json"}, must_exist=False)
    if not meta_path:
        raise CreativeMediaSecurityError(f"Unsafe metadata path for media_id: {clean_id!r}")
    if os.path.lexists(meta_path) and _is_reparse_or_link(meta_path):
        raise CreativeMediaSecurityError(f"Refusing to read symlinked media sidecar: {meta_path}")
    if not os.path.isfile(meta_path):
        raise FileNotFoundError(f"Generated media artifact not found: {clean_id}")

    st = os.stat(meta_path)
    if st.st_size <= 0 or st.st_size > MAX_META_FILE_BYTES:
        raise CreativeMediaSecurityError(f"Media metadata sidecar exceeds size bounds: {meta_path}")
    with open(meta_path, "r", encoding="utf-8") as fh:
        meta = json.load(fh)
    if not isinstance(meta, dict) or str(meta.get("media_id") or "") != clean_id:
        raise CreativeMediaSecurityError(f"Corrupted or mismatched media metadata sidecar: {clean_id}")

    asset_filename = str(meta.get("filename") or "").strip()
    asset_path = safe_file_in_dir(
        generated_dir,
        asset_filename,
        allowed_exts={".png", ".webp", ".mp4", ".gif"},
        must_exist=False,
    )
    if not asset_path:
        raise CreativeMediaSecurityError(f"Unsafe asset filename in metadata sidecar: {asset_filename!r}")
    if os.path.lexists(asset_path) and _is_reparse_or_link(asset_path):
        raise CreativeMediaSecurityError(f"Refusing to read symlinked media asset: {asset_path}")
    if not os.path.isfile(asset_path):
        raise FileNotFoundError(f"Generated media binary missing on disk: {asset_filename}")

    expected_sha = str(meta.get("sha256") or "").strip().lower()
    if verify_sha256:
        actual_sha = file_sha256(asset_path)
        if not actual_sha or actual_sha.lower() != expected_sha:
            raise CreativeMediaSecurityError(
                f"SHA-256 integrity mismatch for {asset_filename}: expected {expected_sha}, got {actual_sha}."
            )

    ext = os.path.splitext(asset_filename)[1].lstrip(".").lower()
    result = dict(meta)
    result["file_path"] = asset_path
    result["metadata_path"] = meta_path
    result["mime_type"] = _MIME_TYPES.get(ext, "application/octet-stream")
    result["verified_sha256"] = bool(verify_sha256)
    return result


# ─── CLI Interface ───────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local-first creative image and video synthesis for Second Brain."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status_p = sub.add_parser("status", help="Inspect hardware gating and local creative backend readiness.")
    status_p.add_argument("--json", action="store_true")

    img_p = sub.add_parser("image", help="Generate a local image under brain/sources/generated/.")
    img_p.add_argument("--prompt", required=True)
    img_p.add_argument("--backend", default="auto", choices=list(IMAGE_BACKENDS))
    img_p.add_argument("--style", default="concept_card", choices=list(PROCEDURAL_STYLES))
    img_p.add_argument("--format", dest="output_format", default="png", choices=list(IMAGE_FORMATS))
    img_p.add_argument("--width", type=int, default=768)
    img_p.add_argument("--height", type=int, default=512)
    img_p.add_argument("--seed", type=int, default=None)
    img_p.add_argument("--title", default="")
    img_p.add_argument("--ground-in-vault", action="store_true")
    img_p.add_argument("--project", default=None)
    img_p.add_argument("--attach-to-note", action="store_true")
    img_p.add_argument("--allow-hardware-override", action="store_true")
    img_p.add_argument("--allow-procedural-fallback", action="store_true")
    img_p.add_argument("--model", dest="model_name", default=None)
    img_p.add_argument("--brain-dir", default=None)

    vid_p = sub.add_parser("video", help="Generate a local MP4 or GIF video under brain/sources/generated/.")
    vid_p.add_argument("--prompt", required=True)
    vid_p.add_argument("--backend", default="auto", choices=list(VIDEO_BACKENDS))
    vid_p.add_argument("--style", default="concept_card", choices=list(PROCEDURAL_STYLES))
    vid_p.add_argument("--format", dest="output_format", default="mp4", choices=list(VIDEO_FORMATS))
    vid_p.add_argument("--width", type=int, default=640)
    vid_p.add_argument("--height", type=int, default=384)
    vid_p.add_argument("--frames", type=int, default=16)
    vid_p.add_argument("--fps", type=int, default=12)
    vid_p.add_argument("--seed", type=int, default=None)
    vid_p.add_argument("--title", default="")
    vid_p.add_argument("--ground-in-vault", action="store_true")
    vid_p.add_argument("--project", default=None)
    vid_p.add_argument("--attach-to-note", action="store_true")
    vid_p.add_argument("--allow-hardware-override", action="store_true")
    vid_p.add_argument("--allow-procedural-fallback", action="store_true")
    vid_p.add_argument("--model", dest="model_name", default=None)
    vid_p.add_argument("--brain-dir", default=None)

    list_p = sub.add_parser("list", help="List generated creative media artifacts.")
    list_p.add_argument("--type", dest="media_type", default=None, choices=["image", "video"])
    list_p.add_argument("--project", default=None)
    list_p.add_argument("--limit", type=int, default=50)
    list_p.add_argument("--brain-dir", default=None)
    list_p.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "status":
        st = get_creative_media_status()
        if args.json:
            print(json.dumps(st.as_dict(), indent=2, sort_keys=True))
        else:
            print(f"Creative Tier:      {st.creative_tier} ({st.tier_label})")
            print(f"Max Budget:         {st.max_width}x{st.max_height}, {st.max_frames} frames @ {st.max_fps} fps")
            print(f"Neural Unlocked:    {st.neural_backends_unlocked} ({st.neural_gate_reason})")
            print(f"Recommended Image:  {st.recommended_image_backend}")
            print(f"Recommended Video:  {st.recommended_video_backend}")
            print(f"Offline MP4 Muxer:  Ready (ffmpeg={'present' if st.ffmpeg_available else 'not required'})")
        return 0

    if args.command == "image":
        art = generate_local_image(
            args.prompt,
            brain_dir=args.brain_dir,
            backend=args.backend,
            style=args.style,
            output_format=args.output_format,
            width=args.width,
            height=args.height,
            seed=args.seed,
            title=args.title,
            model_name=args.model_name,
            ground_in_vault=args.ground_in_vault,
            project_scope=args.project,
            attach_to_note=args.attach_to_note,
            allow_hardware_override=args.allow_hardware_override,
            allow_procedural_fallback=args.allow_procedural_fallback,
        )
        print(json.dumps(art.as_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "video":
        art = generate_local_video(
            args.prompt,
            brain_dir=args.brain_dir,
            backend=args.backend,
            style=args.style,
            output_format=args.output_format,
            width=args.width,
            height=args.height,
            frames=args.frames,
            fps=args.fps,
            seed=args.seed,
            title=args.title,
            model_name=args.model_name,
            ground_in_vault=args.ground_in_vault,
            project_scope=args.project,
            attach_to_note=args.attach_to_note,
            allow_hardware_override=args.allow_hardware_override,
            allow_procedural_fallback=args.allow_procedural_fallback,
        )
        print(json.dumps(art.as_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "list":
        items = list_generated_media(
            brain_dir=args.brain_dir,
            media_type=args.media_type,
            project_scope=args.project,
            limit=args.limit,
        )
        if args.json:
            print(json.dumps(items, indent=2, sort_keys=True))
        else:
            for item in items:
                print(
                    f"{item['media_id']} | {item['media_type']} ({item['output_format']}) | "
                    f"{item['width']}x{item['height']} | backend={item['backend_used']} | {item['relative_path']}"
                )
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
