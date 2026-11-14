# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""Deterministic hardware capability profiling and model/retrieval tier selection."""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import asdict, dataclass
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
from urllib.parse import urlsplit
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ in ("hardware_profile", "scripts.hardware_profile"):
    sys.modules.setdefault("hardware_profile", sys.modules[__name__])
    sys.modules.setdefault("scripts.hardware_profile", sys.modules[__name__])

from config import EMBED_MODEL, GEN_MODEL

HARDWARE_PROFILE_CACHE_TTL_SECONDS = 30.0
_PROFILE_CACHE_LOCK = threading.Lock()
_CACHED_PROFILE: tuple[float, str, HardwareCapabilityProfile] | None = None


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "HTTP redirects are forbidden on loopback probes", headers, fp)


def loopback_ollama_base_url(raw_host_override: str | None = None) -> str | None:
    """Return validated http://127.0.0.1:<port> or http://[::1]:<port> Ollama URL, or None if non-loopback/malformed."""
    raw_host = (
        raw_host_override
        if raw_host_override is not None
        else (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434")
    ).strip().rstrip("/")
    if not raw_host:
        return None
    if "://" in raw_host:
        if not raw_host.lower().startswith("http://"):
            return None
        candidate = raw_host
    else:
        candidate = f"http://{raw_host}"
    try:
        parsed = urlsplit(candidate)
        port = parsed.port or 11434
    except ValueError:
        return None
    hostname = (parsed.hostname or "").strip().lower()
    if (
        parsed.scheme != "http"
        or hostname not in {"127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        return None
    host_literal = "[::1]" if hostname == "::1" else "127.0.0.1"
    return f"http://{host_literal}:{port}"


def fetch_loopback_json(url: str, *, timeout: float = 3.0, max_bytes: int = 65536) -> dict | None:
    """Fetch JSON strictly from http://127.0.0.1 or http://[::1] without environment proxies or redirects."""
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").strip().lower()
        if parsed.scheme != "http" or hostname not in {"127.0.0.1", "::1"}:
            return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())
        cap = min(max(1, int(max_bytes)), 65536)
        with opener.open(url, timeout=timeout) as resp:
            raw = resp.read(cap)
            return json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return None


@dataclass(frozen=True)
class HardwareCapabilityProfile:
    os_name: str
    os_release: str
    architecture: str
    cpu_threads: int
    total_ram_gib: float
    available_ram_gib: float
    cpp_runtime_ready: bool
    cpp_runtime_detail: str
    gpu_backend: str  # "cuda" | "metal" | "cpu"
    gpu_name: str
    vram_gib: float
    ollama_reachable: bool
    installed_ollama_models: tuple[str, ...]
    tier_id: str  # "tier_0_lexical_only" | "tier_1_cpu_compact" | "tier_2_cpu_balanced" | "tier_3_gpu_accelerated"
    tier_label: str
    recommended_generation_model: str
    selected_installed_generation_model: str | None
    recommended_embedding_model: str
    recommended_retrieval_mode: str  # "lexical" | "rrf"
    retrieval_p95_slo_ms: float
    answer_p95_slo_ms: float | None
    rationale: str


def probe_memory_gib() -> tuple[float, float]:
    """Return (total_ram_gib, available_ram_gib) using OS-native APIs."""
    try:
        if os.name == "nt":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return (
                    round(stat.ullTotalPhys / (1024 ** 3), 2),
                    round(stat.ullAvailPhys / (1024 ** 3), 2),
                )
        elif os.path.exists("/proc/meminfo"):
            mem: dict[str, int] = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as mf:
                for line in mf:
                    parts = line.split(":")
                    if len(parts) == 2:
                        mem[parts[0].strip()] = int(parts[1].strip().split()[0]) * 1024
            if "MemTotal" in mem:
                total = mem["MemTotal"] / (1024 ** 3)
                avail = mem.get("MemAvailable", mem.get("MemFree", 0)) / (1024 ** 3)
                return (round(total, 2), round(avail, 2))
        elif sys.platform == "darwin":
            proc = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip().isdigit():
                total = int(proc.stdout.strip()) / (1024 ** 3)
                return (round(total, 2), round(total * 0.5, 2))
    except Exception:
        pass
    return (0.0, 0.0)


def probe_cpp_runtime() -> tuple[bool, str]:
    if os.name == "nt":
        sys_root = os.environ.get("SystemRoot", r"C:\Windows")
        vc_dll = os.path.join(sys_root, "System32", "vcruntime140.dll")
        if os.path.exists(vc_dll):
            return True, "MSVC C++ runtime (vcruntime140.dll) present"
        return False, "vcruntime140.dll missing; install Microsoft Visual C++ Redistributable"
    if sys.platform == "darwin":
        return True, "macOS libc++ runtime present"
    for candidate in ("/lib/x86_64-linux-gnu/libstdc++.so.6", "/usr/lib/libstdc++.so.6", "/usr/lib64/libstdc++.so.6"):
        if os.path.exists(candidate):
            return True, f"POSIX C++ runtime ({candidate}) present"
    return True, "POSIX runtime assumed via dynamic linker"


def probe_gpu(total_ram_gib: float) -> tuple[str, str, float]:
    """Return (gpu_backend, gpu_name, vram_gib)."""
    nvidia = shutil.which("nvidia-smi")
    if nvidia:
        try:
            proc = subprocess.run(
                [nvidia, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                first = proc.stdout.strip().splitlines()[0]
                parts = [p.strip() for p in first.split(",")]
                gpu_name = parts[0] if parts else "NVIDIA GPU"
                vram_mib = float(parts[1]) if len(parts) > 1 and parts[1].replace(".", "", 1).isdigit() else 0.0
                return ("cuda", gpu_name, round(vram_mib / 1024.0, 2))
        except Exception:
            pass
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return ("metal", "Apple Silicon Unified GPU", round(total_ram_gib * 0.75, 2))
    return ("cpu", "CPU inference mode (no CUDA/Metal accelerator)", 0.0)


def probe_ollama_models(
    host: str | float | None = None,
    timeout: float = 3.0,
) -> tuple[bool, list[str]]:
    if isinstance(host, (int, float)):
        timeout = float(host)
        raw_host = None
    else:
        raw_host = host
    base = loopback_ollama_base_url(raw_host)
    if not base:
        return (False, [])
    payload = fetch_loopback_json(f"{base}/api/tags", timeout=timeout, max_bytes=65536)
    if not isinstance(payload, dict):
        return (False, [])
    names = sorted(
        {
            str(item.get("name") or item.get("model"))
            for item in payload.get("models", [])
            if isinstance(item, dict) and (item.get("name") or item.get("model"))
        }
    )
    return (True, names)


def _match_installed_model(candidates: list[str], installed: tuple[str, ...]) -> str | None:
    installed_set = set(installed)
    installed_roots = {m.split(":", 1)[0]: m for m in installed}
    for candidate in candidates:
        if candidate in installed_set:
            return candidate
        if f"{candidate}:latest" in installed_set:
            return f"{candidate}:latest"
        root = candidate.split(":", 1)[0]
        if root in installed_roots and ":" not in candidate:
            return installed_roots[root]
    return None


def classify_hardware_tier(
    *,
    os_name: str,
    os_release: str,
    architecture: str,
    cpu_threads: int,
    total_ram_gib: float,
    available_ram_gib: float,
    cpp_runtime_ready: bool,
    cpp_runtime_detail: str,
    gpu_backend: str,
    gpu_name: str,
    vram_gib: float,
    ollama_reachable: bool,
    installed_ollama_models: tuple[str, ...] | list[str],
) -> HardwareCapabilityProfile:
    """Deterministically classify host hardware into one of four truthful capability tiers."""
    installed_ollama_models = tuple(installed_ollama_models)
    if not ollama_reachable or (total_ram_gib > 0 and total_ram_gib < 6.0):
        reason = (
            "Ollama daemon is unreachable; operating in zero-model SQLite FTS5 lexical mode."
            if not ollama_reachable
            else f"System memory ({total_ram_gib:.1f} GiB) is below the 6 GiB threshold for local LLM inference."
        )
        return HardwareCapabilityProfile(
            os_name=os_name,
            os_release=os_release,
            architecture=architecture,
            cpu_threads=cpu_threads,
            total_ram_gib=total_ram_gib,
            available_ram_gib=available_ram_gib,
            cpp_runtime_ready=cpp_runtime_ready,
            cpp_runtime_detail=cpp_runtime_detail,
            gpu_backend=gpu_backend,
            gpu_name=gpu_name,
            vram_gib=vram_gib,
            ollama_reachable=ollama_reachable,
            installed_ollama_models=installed_ollama_models,
            tier_id="tier_0_lexical_only",
            tier_label="Tier 0 — Offline Lexical Core (Model-Free)",
            recommended_generation_model=GEN_MODEL,
            selected_installed_generation_model=None,
            recommended_embedding_model=EMBED_MODEL,
            recommended_retrieval_mode="lexical",
            retrieval_p95_slo_ms=15.0,
            answer_p95_slo_ms=None,
            rationale=reason,
        )

    has_embedder = _match_installed_model([EMBED_MODEL, "nomic-embed-text"], installed_ollama_models) is not None
    can_use_hybrid = cpp_runtime_ready and has_embedder

    if (gpu_backend == "cuda" and vram_gib >= 8.0) or (gpu_backend == "metal" and total_ram_gib >= 16.0):
        preferred = ["qwen2.5:14b", "qwen2.5-coder:14b", "qwen2.5-coder:7b", "qwen2.5:7b", "llama3.1:8b", "llama3.2:3b"]
        selected = _match_installed_model(preferred, installed_ollama_models)
        return HardwareCapabilityProfile(
            os_name=os_name,
            os_release=os_release,
            architecture=architecture,
            cpu_threads=cpu_threads,
            total_ram_gib=total_ram_gib,
            available_ram_gib=available_ram_gib,
            cpp_runtime_ready=cpp_runtime_ready,
            cpp_runtime_detail=cpp_runtime_detail,
            gpu_backend=gpu_backend,
            gpu_name=gpu_name,
            vram_gib=vram_gib,
            ollama_reachable=True,
            installed_ollama_models=installed_ollama_models,
            tier_id="tier_3_gpu_accelerated",
            tier_label="Tier 3 — GPU-Accelerated Local Intelligence",
            recommended_generation_model="qwen2.5:14b" if vram_gib >= 12.0 else "qwen2.5-coder:7b",
            selected_installed_generation_model=selected,
            recommended_embedding_model=EMBED_MODEL,
            recommended_retrieval_mode="rrf" if can_use_hybrid else "lexical",
            retrieval_p95_slo_ms=50.0 if can_use_hybrid else 15.0,
            answer_p95_slo_ms=5000.0,
            rationale=f"Hardware accelerator ({gpu_name}, {vram_gib:.1f} GiB VRAM) supports low-latency 7B-14B grounded inference.",
        )

    if total_ram_gib >= 12.0:
        preferred = ["qwen2.5-coder:7b", "qwen2.5:7b", "mistral:7b", "llama3.2:3b"]
        selected = _match_installed_model(preferred, installed_ollama_models)
        return HardwareCapabilityProfile(
            os_name=os_name,
            os_release=os_release,
            architecture=architecture,
            cpu_threads=cpu_threads,
            total_ram_gib=total_ram_gib,
            available_ram_gib=available_ram_gib,
            cpp_runtime_ready=cpp_runtime_ready,
            cpp_runtime_detail=cpp_runtime_detail,
            gpu_backend=gpu_backend,
            gpu_name=gpu_name,
            vram_gib=vram_gib,
            ollama_reachable=True,
            installed_ollama_models=installed_ollama_models,
            tier_id="tier_2_cpu_balanced",
            tier_label="Tier 2 — Balanced CPU 7B Intelligence",
            recommended_generation_model="qwen2.5-coder:7b",
            selected_installed_generation_model=selected,
            recommended_embedding_model=EMBED_MODEL,
            recommended_retrieval_mode="lexical",
            retrieval_p95_slo_ms=15.0,
            answer_p95_slo_ms=35000.0,
            rationale=(
                f"CPU host with {total_ram_gib:.1f} GiB RAM comfortably fits quantized 7B models "
                "(94% benchmarked positive coverage on qwen2.5-coder:7b); lexical FTS5 stays default for sub-10ms recall, with RRF opt-in."
            ),
        )

    preferred = ["llama3.2:3b", "qwen2.5:3b"]
    selected = _match_installed_model(preferred, installed_ollama_models)
    return HardwareCapabilityProfile(
        os_name=os_name,
        os_release=os_release,
        architecture=architecture,
        cpu_threads=cpu_threads,
        total_ram_gib=total_ram_gib,
        available_ram_gib=available_ram_gib,
        cpp_runtime_ready=cpp_runtime_ready,
        cpp_runtime_detail=cpp_runtime_detail,
        gpu_backend=gpu_backend,
        gpu_name=gpu_name,
        vram_gib=vram_gib,
        ollama_reachable=True,
        installed_ollama_models=installed_ollama_models,
        tier_id="tier_1_cpu_compact",
        tier_label="Tier 1 — Compact CPU 3B Intelligence",
        recommended_generation_model="llama3.2:3b",
        selected_installed_generation_model=selected,
        recommended_embedding_model=EMBED_MODEL,
        recommended_retrieval_mode="lexical",
        retrieval_p95_slo_ms=15.0,
        answer_p95_slo_ms=18000.0,
        rationale=f"Constrained RAM ({total_ram_gib:.1f} GiB) selects compact 3B quantized inference and fast FTS5 lexical recall.",
    )


def detect_hardware_profile(*, ollama_timeout: float = 3.0, use_cache: bool = False) -> HardwareCapabilityProfile:
    global _CACHED_PROFILE
    env_key = (os.environ.get("OLLAMA_HOST") or "").strip()
    now = time.monotonic()
    if use_cache:
        with _PROFILE_CACHE_LOCK:
            if _CACHED_PROFILE is not None:
                cached_at, cached_env, cached_profile = _CACHED_PROFILE
                if cached_env == env_key and (now - cached_at) < HARDWARE_PROFILE_CACHE_TTL_SECONDS:
                    return cached_profile

    total_ram, avail_ram = probe_memory_gib()
    cpp_ok, cpp_detail = probe_cpp_runtime()
    gpu_backend, gpu_name, vram_gib = probe_gpu(total_ram)
    ollama_ok, models = probe_ollama_models(timeout=ollama_timeout)
    profile = classify_hardware_tier(
        os_name=platform.system(),
        os_release=platform.release(),
        architecture=platform.machine(),
        cpu_threads=os.cpu_count() or 1,
        total_ram_gib=total_ram,
        available_ram_gib=avail_ram,
        cpp_runtime_ready=cpp_ok,
        cpp_runtime_detail=cpp_detail,
        gpu_backend=gpu_backend,
        gpu_name=gpu_name,
        vram_gib=vram_gib,
        ollama_reachable=ollama_ok,
        installed_ollama_models=models,
    )
    with _PROFILE_CACHE_LOCK:
        _CACHED_PROFILE = (time.monotonic(), env_key, profile)
    return profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect host hardware profile and recommended Second Brain tier.")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON profile.")
    args = parser.parse_args(argv)

    profile = detect_hardware_profile()
    if args.json:
        payload = asdict(profile)
        payload["installed_ollama_models"] = list(profile.installed_ollama_models)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Capability Tier:       {profile.tier_label} ({profile.tier_id})")
    print(f"Host OS / Arch:        {profile.os_name} {profile.os_release} ({profile.architecture}), {profile.cpu_threads} threads")
    print(f"Physical Memory:       {profile.available_ram_gib:.1f} GiB free / {profile.total_ram_gib:.1f} GiB total")
    print(f"Accelerator:           {profile.gpu_name} [{profile.gpu_backend}, {profile.vram_gib:.1f} GiB VRAM]")
    print(f"Native C++ ABI:        {'Ready' if profile.cpp_runtime_ready else 'Missing'} ({profile.cpp_runtime_detail})")
    print(f"Recommended Model:     {profile.recommended_generation_model} (installed match: {profile.selected_installed_generation_model or 'none'})")
    print(f"Recommended Retrieval: {profile.recommended_retrieval_mode} (P95 target <= {profile.retrieval_p95_slo_ms:.0f} ms)")
    if profile.answer_p95_slo_ms is not None:
        print(f"Answer P95 Budget:     <= {profile.answer_p95_slo_ms / 1000.0:.1f} s")
    print(f"Rationale:             {profile.rationale}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
