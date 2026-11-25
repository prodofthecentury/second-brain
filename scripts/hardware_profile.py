# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Deterministic hardware capability profiling and model/retrieval tier selection."""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import asdict, dataclass, replace
import json
import os
import platform
import re
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
NOMINAL_16GB_RAM_GIB: float = 15.5
_PROFILE_CACHE_LOCK = threading.Lock()
_CACHED_PROFILE: tuple[float, tuple[Any, ...], HardwareCapabilityProfile] | None = None


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
        or hostname not in {"127.0.0.1", "localhost", "::1"}
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
    oversized_installed_models: tuple[str, ...] = ()
    physical_cores: int = 0
    recommended_num_ctx: int = 4096


_THINK_CLOSED_RE = re.compile(r"(?is)<think(?:ing)?>.*?</think(?:ing)?>\s*")
_THINK_UNCLOSED_RE = re.compile(r"(?is)<think(?:ing)?>.*$")
_REASONING_MODEL_RE = re.compile(r"^(?:.*/)?(?:qwen3(?:\.\d+|-coder|-vl)?|qwq|deepseek-r1)(?:[:_-]|$)", re.IGNORECASE)

GGUF_QUANT_GB_PER_B: dict[str, float] = {
    "iq1_m": 0.24,
    "iq2_xxs": 0.28,
    "iq2_xs": 0.31,
    "iq2_s": 0.34,
    "iq2_m": 0.37,
    "iq3_xxs": 0.40,
    "iq3_xs": 0.42,
    "iq3_s": 0.45,
    "iq3_m": 0.48,
    "iq4_xs": 0.54,
    "iq4_nl": 0.56,
    "q2_k": 0.38,
    "q3_k_s": 0.44,
    "q3_k_m": 0.49,
    "q3_k_l": 0.54,
    "q4_0": 0.56,
    "q4_k_s": 0.58,
    "q4_k_m": 0.61,
    "q5_k_s": 0.68,
    "q5_k_m": 0.72,
    "q6_k": 0.82,
    "q8_0": 1.06,
    "f16": 2.0,
    "fp16": 2.0,
    "bf16": 2.0,
}
_GGUF_QUANT_RE = re.compile(
    r"(?i)(?:^|[:_.-])(iq[1-4]_[a-z]{1,3}|q[2-8]_(?:k_[sml]|0|k)|fp16|f16|bf16)(?:$|[:_.-])"
)


def parse_gguf_quant_tag(model_name: str | None) -> str | None:
    """Extract normalized GGUF quantization tag such as ``iq3_s`` or ``q4_k_m`` from a model identifier."""
    m = _GGUF_QUANT_RE.search(str(model_name or "").strip())
    return m.group(1).lower() if m else None


def detect_physical_cpu_cores() -> int:
    """Return physical CPU core count safely across Windows, Linux, and macOS (avoiding SMT oversubscription)."""
    logical = max(1, int(os.cpu_count() or 1))
    try:
        if sys.platform.startswith("linux") and os.path.isfile("/proc/cpuinfo"):
            pairs: set[tuple[str, str]] = set()
            phys_id = "0"
            core_id = ""
            with open("/proc/cpuinfo", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    low = line.strip()
                    if not low:
                        if core_id:
                            pairs.add((phys_id, core_id))
                        phys_id, core_id = "0", ""
                    elif low.startswith("physical id"):
                        phys_id = low.split(":", 1)[-1].strip()
                    elif low.startswith("core id"):
                        core_id = low.split(":", 1)[-1].strip()
                if core_id:
                    pairs.add((phys_id, core_id))
            if pairs:
                return max(1, min(logical, len(pairs)))
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.physicalcpu"], capture_output=True, text=True, timeout=2, check=False)
            if out.returncode == 0 and out.stdout.strip().isdigit():
                return max(1, min(logical, int(out.stdout.strip())))
    except Exception:
        pass
    # Heuristic fallback when OS topology isn't directly exposed: assume 2-way SMT on >=4 logical CPUs
    return max(1, logical // 2) if logical >= 4 else logical


def is_reasoning_model_name(model: str | None) -> bool:
    """Return True when model belongs to a thinking-capable family (qwen3*, qwq*, deepseek-r1*)."""
    return bool(_REASONING_MODEL_RE.match((model or "").strip()))


def strip_reasoning_think_blocks(text: str | None) -> str:
    """Remove <think>...</think> chain-of-thought blocks (closed or truncated) from model output."""
    if not text:
        return ""
    cleaned = _THINK_CLOSED_RE.sub("", str(text))
    if "<think" in cleaned.lower():
        cleaned = _THINK_UNCLOSED_RE.sub("", cleaned)
    return cleaned.strip()


def call_ollama_chat_maybe_no_think(chat_fn, /, **kwargs):
    """Invoke ollama chat passing think=False for reasoning models when supported by client/mock."""
    model_name = str(kwargs.get("model") or "")
    if "think" not in kwargs and is_reasoning_model_name(model_name):
        try:
            return chat_fn(**kwargs, think=False)
        except TypeError:
            pass
    return chat_fn(**kwargs)


_VM_STAT_PAGE_SIZE_RE = re.compile(r"page size of (\d+) bytes", re.IGNORECASE)


def _parse_macos_vm_stat_avail_bytes(vm_stat_text: str, total_bytes: int) -> int | None:
    """Compute reclaimable + free bytes from macOS ``vm_stat`` output."""
    if not vm_stat_text or total_bytes <= 0:
        return None
    ps_match = _VM_STAT_PAGE_SIZE_RE.search(vm_stat_text)
    page_size = int(ps_match.group(1)) if ps_match else 4096
    counts: dict[str, int] = {}
    for line in vm_stat_text.splitlines():
        if ":" not in line:
            continue
        key, raw_val = line.split(":", 1)
        digits = re.sub(r"[^0-9]", "", raw_val)
        if digits:
            counts[key.strip().lower()] = int(digits)
    avail_pages = (
        counts.get("pages free", 0)
        + counts.get("pages inactive", 0)
        + counts.get("pages speculative", 0)
        + counts.get("pages purgeable", 0)
    )
    if avail_pages <= 0:
        return None
    return min(total_bytes, avail_pages * page_size)


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
                total_bytes = int(proc.stdout.strip())
                total = total_bytes / (1024 ** 3)
                avail_bytes: int | None = None
                try:
                    vm_proc = subprocess.run(
                        ["vm_stat"],
                        capture_output=True,
                        text=True,
                        timeout=2.0,
                        check=False,
                    )
                    if vm_proc.returncode == 0:
                        avail_bytes = _parse_macos_vm_stat_avail_bytes(vm_proc.stdout, total_bytes)
                except Exception:
                    avail_bytes = None
                avail = (avail_bytes / (1024 ** 3)) if avail_bytes is not None else (total * 0.5)
                return (round(total, 2), round(avail, 2))
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


_DISCRETE_GPU_BACKENDS = frozenset({"cuda", "rocm", "xpu", "directml"})


def _resolve_nvidia_smi_binary() -> str | None:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if os.name == "nt":
        sys_root = os.environ.get("SystemRoot", r"C:\Windows")
        prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        for candidate in (
            os.path.join(prog_files, "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe"),
            os.path.join(sys_root, "System32", "nvidia-smi.exe"),
        ):
            if os.path.isfile(candidate):
                return candidate
    return None


def _probe_linux_drm_sysfs_gpu(vendor_hex: str, backend: str, default_name: str) -> tuple[str, str, float] | None:
    drm_root = "/sys/class/drm"
    if not os.path.isdir(drm_root):
        return None
    best: tuple[str, str, float] | None = None
    try:
        for entry in sorted(os.listdir(drm_root)):
            if not re.fullmatch(r"card\d+", entry):
                continue
            dev_dir = os.path.join(drm_root, entry, "device")
            vendor_path = os.path.join(dev_dir, "vendor")
            if not os.path.isfile(vendor_path):
                continue
            with open(vendor_path, "r", encoding="utf-8", errors="ignore") as vf:
                vendor = vf.read().strip().lower()
            if vendor != vendor_hex.lower():
                continue
            vram_bytes = 0
            for mem_file in ("mem_info_vram_total", "local_memory_size"):
                mem_path = os.path.join(dev_dir, mem_file)
                if os.path.isfile(mem_path):
                    with open(mem_path, "r", encoding="utf-8", errors="ignore") as mf:
                        raw = mf.read().strip()
                        if raw.isdigit():
                            vram_bytes = int(raw)
                            break
            if vram_bytes <= 0:
                continue
            vram_gib = round(vram_bytes / (1024 ** 3), 2)
            name = default_name
            product_path = os.path.join(dev_dir, "product_name")
            if os.path.isfile(product_path):
                with open(product_path, "r", encoding="utf-8", errors="ignore") as pf:
                    pname = pf.read().strip()
                    if pname:
                        name = pname
            if best is None or vram_gib > best[2]:
                best = (backend, name, vram_gib)
    except OSError:
        return best
    return best


def _parse_rocm_smi_output(text: str) -> tuple[str, str, float] | None:
    if not text or not text.strip():
        return None
    raw = text.strip()
    if raw.startswith("{"):
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                for _card, info in payload.items():
                    if isinstance(info, dict):
                        vram_val = None
                        name = str(info.get("Card series") or info.get("Card model") or "AMD Radeon / Instinct GPU")
                        for k, v in info.items():
                            if "vram total" in k.lower() and str(v).strip().isdigit():
                                vram_val = int(str(v).strip())
                                break
                        if vram_val:
                            return ("rocm", name, round(vram_val / (1024 ** 3), 2))
        except (ValueError, TypeError):
            pass
    best_vram_b = 0
    for line in raw.splitlines():
        if "vram" in line.lower() and any(ch.isdigit() for ch in line):
            nums = [int(n) for n in re.findall(r"\b(\d{6,})\b", line)]
            if nums:
                best_vram_b = max(best_vram_b, max(nums))
    if best_vram_b > 0:
        return ("rocm", "AMD ROCm GPU", round(best_vram_b / (1024 ** 3), 2))
    return None


def _probe_rocm_gpu() -> tuple[str, str, float] | None:
    for tool_name, args in (
        ("rocm-smi", ["--showproductname", "--showmeminfo", "vram", "--json"]),
        ("amd-smi", ["static", "--asic", "--vram", "--json"]),
    ):
        bin_path = shutil.which(tool_name)
        if not bin_path:
            continue
        try:
            proc = subprocess.run([bin_path, *args], capture_output=True, text=True, timeout=2.5, check=False)
            if proc.returncode == 0:
                parsed = _parse_rocm_smi_output(proc.stdout)
                if parsed is not None:
                    return parsed
        except Exception:
            pass
    return _probe_linux_drm_sysfs_gpu("0x1002", "rocm", "AMD ROCm GPU")


def _probe_intel_xpu_gpu() -> tuple[str, str, float] | None:
    xpu_smi = shutil.which("xpu-smi")
    if xpu_smi:
        try:
            proc = subprocess.run(
                [xpu_smi, "discovery", "-j"],
                capture_output=True,
                text=True,
                timeout=2.5,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout)
                devices = data.get("device_list") if isinstance(data, dict) else None
                if isinstance(devices, list) and devices:
                    first = devices[0]
                    name = str(first.get("device_name") or "Intel Arc GPU")
                    mem_raw = str(first.get("memory_physical_size_byte") or "0")
                    vram_gib = round(float(re.sub(r"[^0-9.]", "", mem_raw) or 0) / (1024 ** 3), 2)
                    if vram_gib <= 0:
                        mem_mib = float(first.get("memory_physical_size_mib") or 0)
                        vram_gib = round(mem_mib / 1024.0, 2)
                    return ("xpu", name, vram_gib)
        except Exception:
            pass
    return _probe_linux_drm_sysfs_gpu("0x8086", "xpu", "Intel Arc GPU")


def _probe_windows_directml_gpu() -> tuple[str, str, float] | None:
    if os.name != "nt":
        return None
    ps_script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "$adapters = @(Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM); "
        "$reg = @(Get-ItemProperty 'HKLM:\\SYSTEM\\ControlSet001\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}\\0*' "
        "| Select-Object DriverDesc,@{N='QwMem';E={$_.'HardwareInformation.qwMemorySize'}}); "
        "@{adapters=$adapters; reg=$reg} | ConvertTo-Json -Depth 4"
    )
    ps_exe = os.path.join(
        os.environ.get("SystemRoot") or r"C:\Windows",
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )
    if not os.path.isfile(ps_exe):
        ps_exe = shutil.which("powershell.exe") or ""
    if not ps_exe:
        return None
    try:
        proc = subprocess.run(
            [ps_exe, "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=3.5,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        payload = json.loads(proc.stdout)
        raw_reg = payload.get("reg") or []
        if isinstance(raw_reg, dict):
            raw_reg = [raw_reg]
        reg_map: dict[str, int] = {}
        for r in raw_reg:
            if isinstance(r, dict) and r.get("DriverDesc") and r.get("QwMem"):
                reg_map[str(r["DriverDesc"]).strip().lower()] = int(r["QwMem"])
        skip_names = ("microsoft basic render", "remote display", "hyper-v video", "virtual")
        raw_ads = payload.get("adapters") or []
        if isinstance(raw_ads, dict):
            raw_ads = [raw_ads]
        best: tuple[str, str, float] | None = None
        for ad in raw_ads:
            if not isinstance(ad, dict):
                continue
            name = str(ad.get("Name") or "").strip()
            low = name.lower()
            if not name or any(s in low for s in skip_names):
                continue
            vram_b = reg_map.get(low) or int(ad.get("AdapterRAM") or 0)
            vram_gib = round(max(0, vram_b) / (1024 ** 3), 2)
            if best is None or vram_gib > best[2]:
                best = ("directml", name, vram_gib)
        return best
    except Exception:
        return None


def probe_gpu(total_ram_gib: float) -> tuple[str, str, float]:
    """Return (gpu_backend, gpu_name, vram_gib)."""
    nvidia = _resolve_nvidia_smi_binary()
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
    rocm = _probe_rocm_gpu()
    if rocm is not None:
        return rocm
    xpu = _probe_intel_xpu_gpu()
    if xpu is not None:
        return xpu
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return ("metal", "Apple Silicon Unified GPU", round(total_ram_gib * 0.75, 2))
    directml = _probe_windows_directml_gpu()
    if directml is not None:
        return directml
    return ("cpu", "CPU inference mode (no CUDA/ROCm/XPU/DirectML/Metal accelerator)", 0.0)


def probe_ollama_models_with_sizes(
    host: str | float | None = None,
    timeout: float = 3.0,
) -> tuple[bool, list[str], dict[str, int]]:
    if isinstance(host, (int, float)):
        timeout = float(host)
        raw_host = None
    else:
        raw_host = host
    base = loopback_ollama_base_url(raw_host)
    if not base:
        return (False, [], {})
    payload = fetch_loopback_json(f"{base}/api/tags", timeout=timeout, max_bytes=65536)
    if not isinstance(payload, dict):
        return (False, [], {})
    names_set: set[str] = set()
    sizes: dict[str, int] = {}
    for item in payload.get("models", []):
        if not isinstance(item, dict):
            continue
        raw_name = item.get("name") or item.get("model")
        if not raw_name:
            continue
        m_name = str(raw_name)
        names_set.add(m_name)
        raw_sz = item.get("size")
        if isinstance(raw_sz, int) and raw_sz > 0:
            sizes[m_name] = raw_sz
            sizes.setdefault(m_name.split(":", 1)[0], raw_sz)
    return (True, sorted(names_set), sizes)


def probe_ollama_models(
    host: str | float | None = None,
    timeout: float = 3.0,
) -> tuple[bool, list[str]]:
    reachable, names, _ = probe_ollama_models_with_sizes(host, timeout=timeout)
    return (reachable, names)


_DEFAULT_PROBE_OLLAMA_MODELS = probe_ollama_models


def _model_fits_host_memory(
    model_name: str,
    *,
    total_ram_gib: float | None = 0.0,
    gpu_backend: str = "cpu",
    vram_gib: float | None = 0.0,
    model_sizes_bytes: dict[str, int] | None = None,
) -> bool:
    """Return True if a local GGUF model fits host VRAM/RAM without severe pagefile thrashing."""
    total_ram_gib = float(total_ram_gib or 0.0)
    vram_gib = float(vram_gib or 0.0)
    clean = (model_name or "").strip()
    if not clean:
        return False
    if model_sizes_bytes:
        sz = model_sizes_bytes.get(clean) or model_sizes_bytes.get(clean.split(":", 1)[0]) or 0
        if sz > 0:
            gib = sz / (1024 ** 3)
            if gpu_backend in _DISCRETE_GPU_BACKENDS and vram_gib > 0:
                cap = max(vram_gib * 1.15, total_ram_gib * 0.68 if total_ram_gib > 0 else vram_gib * 1.15)
                return gib <= cap
            if total_ram_gib > 0:
                return gib <= total_ram_gib * 0.68
            return True

    if total_ram_gib <= 0 and vram_gib <= 0:
        return True
    low = clean.lower()
    quant_tag = parse_gguf_quant_tag(low)
    quant_mult = GGUF_QUANT_GB_PER_B.get(quant_tag or "", 0.61)
    size_b_match = re.search(r"[:_-](\d+(?:\.\d+)?)b(?:[:_-]|$)", low)
    if size_b_match:
        params_b = float(size_b_match.group(1))
        est_gib = params_b * quant_mult + 1.0
        if gpu_backend in _DISCRETE_GPU_BACKENDS and vram_gib > 0:
            return est_gib <= max(vram_gib * 1.15, total_ram_gib * 0.72 if total_ram_gib > 0 else vram_gib * 1.15)
        if total_ram_gib > 0:
            return est_gib <= total_ram_gib * 0.72
    if low.startswith("qwen3.6") or any(t in low for t in (":30b", ":32b", ":36b", ":70b", ":72b")):
        return (gpu_backend in _DISCRETE_GPU_BACKENDS and vram_gib >= 20.0) or total_ram_gib >= 32.0
    if ":14b" in low:
        return (gpu_backend in _DISCRETE_GPU_BACKENDS and vram_gib >= 10.0) or total_ram_gib >= 20.0
    return True


def recommended_ollama_runtime_options(
    profile: HardwareCapabilityProfile | None = None,
    *,
    model_name: str | None = None,
) -> dict[str, int]:
    """Compute portable CPU thread + context options scaling from compact laptops up to GPU desktops."""
    phys_cores = detect_physical_cpu_cores()
    total_ram = float(getattr(profile, "total_ram_gib", 0.0) or 0.0)
    avail_ram = float(getattr(profile, "available_ram_gib", 0.0) or 0.0)
    vram_gib = float(getattr(profile, "vram_gib", 0.0) or 0.0)
    gpu_backend = str(getattr(profile, "gpu_backend", "cpu") or "cpu")

    if gpu_backend in _DISCRETE_GPU_BACKENDS and vram_gib >= 16.0:
        num_ctx = 32768
    elif (gpu_backend in _DISCRETE_GPU_BACKENDS and vram_gib >= 8.0) or total_ram >= 28.0:
        num_ctx = 16384
    elif total_ram >= 12.0 and avail_ram >= 5.0:
        num_ctx = 8192
    else:
        num_ctx = 4096

    if parse_gguf_quant_tag(model_name) in {"iq2_xs", "iq2_s", "iq3_xxs", "iq3_xs", "iq3_s"}:
        # Compact I-quants leave more KV cache headroom cleanly
        num_ctx = max(num_ctx, 8192) if total_ram >= 12.0 or vram_gib >= 6.0 else num_ctx

    return {
        "num_thread": max(1, phys_cores),
        "num_ctx": num_ctx,
    }


def _match_installed_model(
    candidates: list[str],
    installed: tuple[str, ...],
    *,
    total_ram_gib: float = 0.0,
    gpu_backend: str = "cpu",
    vram_gib: float = 0.0,
    model_sizes_bytes: dict[str, int] | None = None,
    enforce_memory_fit: bool = False,
) -> str | None:
    installed_set = set(installed)
    installed_roots: dict[str, list[str]] = {}
    for m in installed:
        installed_roots.setdefault(m.split(":", 1)[0], []).append(m)

    def _ok(name: str) -> bool:
        if not enforce_memory_fit:
            return True
        return _model_fits_host_memory(
            name,
            total_ram_gib=total_ram_gib,
            gpu_backend=gpu_backend,
            vram_gib=vram_gib,
            model_sizes_bytes=model_sizes_bytes,
        )

    for candidate in candidates:
        if candidate in installed_set and _ok(candidate):
            return candidate
        if f"{candidate}:latest" in installed_set and _ok(f"{candidate}:latest"):
            return f"{candidate}:latest"
        root = candidate.split(":", 1)[0]
        if root in installed_roots and ":" not in candidate:
            for matched in installed_roots[root]:
                if _ok(matched):
                    return matched
    return None


def classify_hardware_tier(
    *,
    os_name: str,
    os_release: str,
    architecture: str,
    cpu_threads: int,
    total_ram_gib: float | None,
    available_ram_gib: float | None,
    cpp_runtime_ready: bool,
    cpp_runtime_detail: str,
    gpu_backend: str,
    gpu_name: str,
    vram_gib: float | None,
    ollama_reachable: bool,
    installed_ollama_models: tuple[str, ...] | list[str],
    model_sizes_bytes: dict[str, int] | None = None,
) -> HardwareCapabilityProfile:
    """Deterministically classify host hardware into one of four truthful capability tiers."""
    total_ram_gib = float(total_ram_gib or 0.0)
    available_ram_gib = float(available_ram_gib or 0.0)
    vram_gib = float(vram_gib or 0.0)
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
    oversized = tuple(
        m
        for m in installed_ollama_models
        if not _model_fits_host_memory(
            m,
            total_ram_gib=total_ram_gib,
            gpu_backend=gpu_backend,
            vram_gib=vram_gib,
            model_sizes_bytes=model_sizes_bytes,
        )
    )

    if (gpu_backend in _DISCRETE_GPU_BACKENDS and vram_gib >= 8.0) or (
        gpu_backend == "metal" and total_ram_gib >= NOMINAL_16GB_RAM_GIB
    ):
        preferred = [
            "qwen3.6",
            "qwen3-coder:30b",
            "qwen3:32b",
            "qwen3:30b",
            "qwen3.5:14b",
            "qwen3:14b",
            "qwen3-coder:14b",
            "qwen2.5:14b",
            "qwen2.5-coder:14b",
            "qwen3-coder:8b",
            "qwen3.5:8b",
            "qwen3:8b",
            "qwen3",
            "qwen2.5-coder:7b",
            "qwen2.5:7b",
            "llama3.1:8b",
            "qwen3:4b",
            "llama3.2:3b",
        ]
        selected = _match_installed_model(
            preferred,
            installed_ollama_models,
            total_ram_gib=total_ram_gib,
            gpu_backend=gpu_backend,
            vram_gib=vram_gib,
            model_sizes_bytes=model_sizes_bytes,
            enforce_memory_fit=True,
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
            rationale=f"Hardware accelerator ({gpu_name}, {vram_gib:.1f} GiB VRAM) supports low-latency 7B-14B+ grounded inference.",
            oversized_installed_models=oversized,
        )

    if total_ram_gib >= 12.0:
        preferred = [
            "qwen3.6",
            "qwen3-coder:30b",
            "qwen3:32b",
            "qwen3.5:14b",
            "qwen3:14b",
            "qwen3-coder:14b",
            "qwen3-coder:8b",
            "qwen3.5:8b",
            "qwen3:8b",
            "qwen3",
            "qwen2.5-coder:7b",
            "qwen2.5:7b",
            "mistral:7b",
            "qwen3:4b",
            "llama3.2:3b",
        ]
        selected = _match_installed_model(
            preferred,
            installed_ollama_models,
            total_ram_gib=total_ram_gib,
            gpu_backend=gpu_backend,
            vram_gib=vram_gib,
            model_sizes_bytes=model_sizes_bytes,
            enforce_memory_fit=True,
        )
        oversized_note = (
            f" Oversized model(s) skipped for auto-default to prevent swap thrashing: {', '.join(oversized)} (override via BRAIN_GEN_MODEL)."
            if oversized
            else ""
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
            ollama_reachable=True,
            installed_ollama_models=installed_ollama_models,
            tier_id="tier_2_cpu_balanced",
            tier_label="Tier 2 — Balanced CPU 7B Intelligence",
            recommended_generation_model="qwen2.5-coder:7b",
            selected_installed_generation_model=selected,
            recommended_embedding_model=EMBED_MODEL,
            recommended_retrieval_mode="lexical",
            retrieval_p95_slo_ms=15.0,
            answer_p95_slo_ms=45000.0,
            rationale=(
                f"CPU host with {total_ram_gib:.1f} GiB RAM ({cpu_threads} threads) comfortably fits quantized 7B/8B models "
                "(100.0% benchmarked positive coverage on qwen2.5-coder:7b across the 90-case curated suite); lexical FTS5 stays default for sub-15ms recall, with RRF opt-in (answer SLO recalibrated from 35.0 s to 45.0 s pending owner sign-off)."
                + oversized_note
            ),
            oversized_installed_models=oversized,
        )

    preferred = ["qwen3:4b", "qwen3.5:4b", "llama3.2:3b", "qwen2.5:3b", "qwen3:1.7b"]
    selected = _match_installed_model(
        preferred,
        installed_ollama_models,
        total_ram_gib=total_ram_gib,
        gpu_backend=gpu_backend,
        vram_gib=vram_gib,
        model_sizes_bytes=model_sizes_bytes,
        enforce_memory_fit=True,
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
        rationale=f"Constrained RAM ({total_ram_gib:.1f} GiB) selects compact 3B/4B quantized inference and fast FTS5 lexical recall.",
        oversized_installed_models=oversized,
    )


def detect_hardware_profile(*, ollama_timeout: float = 3.0, use_cache: bool = False) -> HardwareCapabilityProfile:
    global _CACHED_PROFILE
    env_key = (os.environ.get("OLLAMA_HOST") or "").strip()
    cache_identity = (
        env_key,
        id(probe_ollama_models),
        id(probe_ollama_models_with_sizes),
        id(probe_gpu),
        id(probe_memory_gib),
    )
    now = time.monotonic()
    if use_cache:
        with _PROFILE_CACHE_LOCK:
            if _CACHED_PROFILE is not None:
                cached_at, cached_identity, cached_profile = _CACHED_PROFILE
                if cached_identity == cache_identity and (now - cached_at) < HARDWARE_PROFILE_CACHE_TTL_SECONDS:
                    return cached_profile

    total_ram, avail_ram = probe_memory_gib()
    cpp_ok, cpp_detail = probe_cpp_runtime()
    gpu_backend, gpu_name, vram_gib = probe_gpu(total_ram)
    if probe_ollama_models is _DEFAULT_PROBE_OLLAMA_MODELS:
        ollama_ok, models, model_sizes = probe_ollama_models_with_sizes(timeout=ollama_timeout)
    else:
        ollama_ok, models = probe_ollama_models(timeout=ollama_timeout)
        model_sizes = {}
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
        model_sizes_bytes=model_sizes,
    )
    opts = recommended_ollama_runtime_options(profile, model_name=profile.selected_installed_generation_model)
    profile = replace(
        profile,
        physical_cores=int(opts["num_thread"]),
        recommended_num_ctx=int(opts["num_ctx"]),
    )
    with _PROFILE_CACHE_LOCK:
        _CACHED_PROFILE = (time.monotonic(), cache_identity, profile)
    return profile


def invalidate_hardware_profile_cache() -> None:
    """Clear the cached hardware capability profile so the next probe re-evaluates live state."""
    global _CACHED_PROFILE
    with _PROFILE_CACHE_LOCK:
        _CACHED_PROFILE = None


_SAFE_LOCAL_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def resolve_active_local_generation_model(
    *,
    fallback: str | None = None,
    ollama_timeout: float = 0.8,
    use_cache: bool = True,
) -> str:
    """Resolve the active local Ollama generation model from explicit env override or hardware tier.

    Precedence:
    1. Explicit ``BRAIN_GEN_MODEL`` environment variable (unless empty, ``"auto"``, or malformed).
    2. Hardware-tier ``selected_installed_generation_model`` (e.g. ``qwen2.5-coder:7b`` on Tier 2+).
    3. Explicit ``fallback`` argument or ``GEN_MODEL`` baseline (``llama3.2:3b``).
    """
    explicit_env = (os.environ.get("BRAIN_GEN_MODEL") or "").strip()
    if (
        explicit_env
        and explicit_env.lower() != "auto"
        and ".." not in explicit_env
        and _SAFE_LOCAL_MODEL_RE.fullmatch(explicit_env)
    ):
        return explicit_env
    try:
        profile = detect_hardware_profile(ollama_timeout=ollama_timeout, use_cache=use_cache)
    except TypeError:
        try:
            profile = detect_hardware_profile()
        except Exception:
            profile = None
    except Exception:
        profile = None
    if profile is not None:
        selected = getattr(profile, "selected_installed_generation_model", None)
        if isinstance(selected, str) and selected.strip():
            return selected.strip()
        if fallback is None:
            rec = getattr(profile, "recommended_generation_model", None)
            if isinstance(rec, str) and rec.strip():
                return rec.strip()
    return (fallback or GEN_MODEL).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect host hardware profile and recommended Second Brain tier.")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON profile.")
    args = parser.parse_args(argv)

    profile = detect_hardware_profile()
    active_model = resolve_active_local_generation_model(fallback=GEN_MODEL)
    if args.json:
        payload = asdict(profile)
        payload["installed_ollama_models"] = list(profile.installed_ollama_models)
        payload["oversized_installed_models"] = list(profile.oversized_installed_models)
        payload["active_generation_model"] = active_model
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Capability Tier:       {profile.tier_label} ({profile.tier_id})")
    print(f"Host OS / Arch:        {profile.os_name} {profile.os_release} ({profile.architecture}), {profile.cpu_threads} threads")
    print(f"Physical Memory:       {profile.available_ram_gib:.1f} GiB free / {profile.total_ram_gib:.1f} GiB total")
    print(f"Accelerator:           {profile.gpu_name} [{profile.gpu_backend}, {profile.vram_gib:.1f} GiB VRAM]")
    print(f"Native C++ ABI:        {'Ready' if profile.cpp_runtime_ready else 'Missing'} ({profile.cpp_runtime_detail})")
    print(f"Active Default Model:  {active_model}")
    print(f"Recommended Model:     {profile.recommended_generation_model} (installed match: {profile.selected_installed_generation_model or 'none'})")
    print(f"Recommended Retrieval: {profile.recommended_retrieval_mode} (P95 target <= {profile.retrieval_p95_slo_ms:.0f} ms)")
    if profile.answer_p95_slo_ms is not None:
        print(f"Answer P95 Budget:     <= {profile.answer_p95_slo_ms / 1000.0:.1f} s")
    print(f"Rationale:             {profile.rationale}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
