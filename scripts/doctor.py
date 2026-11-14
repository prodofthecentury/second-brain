# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""
Second Brain health check.

Run:
    uv run scripts/doctor.py
"""

import ctypes
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

from config import (
    ATOMS_DIR,
    BRAIN_DIR,
    BRIEFINGS_DIR,
    CHROMA_PATH,
    EMBED_MODEL,
    GEN_MODEL,
    INBOX_DIR,
    LEXICAL_INDEX_PATH,
    RAW_DIR,
    READING_LIST,
    SOURCES_DIR,
    THREADS_DIR,
    VISION_MODEL,
    is_content_markdown,
)
from index_manifest import index_manifest_is_current, load_index_manifest
from lexical_store import lexical_index_is_current
from review_atom import approved_revision_is_current
from vector_store import JsonVectorCollection, close_vector_collection, get_vector_collection, json_collection_path


def ok(area: str, detail: str) -> tuple[str, str, str]:
    return ("OK", area, detail)


def warn(area: str, detail: str) -> tuple[str, str, str]:
    return ("WARN", area, detail)


def fail(area: str, detail: str) -> tuple[str, str, str]:
    return ("FAIL", area, detail)


def fetch_json(url: str, timeout: float = 5.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def fetch_text(url: str, timeout: float = 5.0) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError):
        return None


def model_names(models_payload: dict | None) -> set[str]:
    names = set()
    for model in (models_payload or {}).get("models", []):
        name = model.get("name") or model.get("model")
        if name:
            names.add(name)
            names.add(name.split(":", 1)[0])
    return names


def vision_model_names(models_payload: dict | None) -> set[str]:
    names = set()
    for model in (models_payload or {}).get("models", []):
        capabilities = model.get("capabilities") or []
        if "vision" in capabilities:
            name = model.get("name") or model.get("model")
            if name:
                names.add(name)
    return names


def count_files(directory: str, suffixes: tuple[str, ...]) -> int:
    if not os.path.isdir(directory):
        return 0
    count = 0
    for filename in os.listdir(directory):
        if filename.endswith(".md") and not is_content_markdown(filename):
            continue
        if filename.endswith(suffixes):
            count += 1
    return count


def queued_reading_items() -> int:
    if not os.path.exists(READING_LIST):
        return 0
    with open(READING_LIST, "r", encoding="utf-8") as file:
        return sum(1 for line in file if line.startswith("- [ ] http"))


def system_memory_summary() -> str:
    """Report physical memory without external psutil requirement."""
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
                total_gib = stat.ullTotalPhys / (1024 ** 3)
                avail_gib = stat.ullAvailPhys / (1024 ** 3)
                return f"{avail_gib:.1f} GiB available / {total_gib:.1f} GiB total"
        elif os.path.exists("/proc/meminfo"):
            mem = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as mf:
                for line in mf:
                    parts = line.split(":")
                    if len(parts) == 2:
                        mem[parts[0].strip()] = int(parts[1].strip().split()[0]) * 1024
            if "MemTotal" in mem:
                total_gib = mem["MemTotal"] / (1024 ** 3)
                avail_gib = mem.get("MemAvailable", mem.get("MemFree", 0)) / (1024 ** 3)
                return f"{avail_gib:.1f} GiB available / {total_gib:.1f} GiB total"
    except Exception:
        pass
    return "memory telemetry unavailable"


def native_cpp_runtime_status() -> tuple[bool, str]:
    """Verify C++ runtime libraries needed by native vector/ONNX extensions."""
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


def gpu_driver_status() -> str:
    nvidia = shutil.which("nvidia-smi")
    if nvidia:
        try:
            proc = subprocess.run(
                [nvidia, "--query-gpu=name,driver_version", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return f"NVIDIA GPU ({proc.stdout.strip().splitlines()[0]})"
        except Exception:
            pass
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "Apple Silicon Unified GPU (Metal)"
    return "No discrete CUDA GPU detected (CPU inference mode)"


def run_checks() -> list[tuple[str, str, str]]:
    checks = []

    checks.append(ok(
        "host runtime",
        f"Python {platform.python_version()} on {platform.system()} {platform.release()} ({platform.machine()}), "
        f"{os.cpu_count() or 'unknown'} CPU threads, RAM: {system_memory_summary()}",
    ))
    cpp_ok, cpp_detail = native_cpp_runtime_status()
    checks.append(ok("c++ runtime", cpp_detail) if cpp_ok else warn("c++ runtime", cpp_detail))
    checks.append(ok("compute device", gpu_driver_status()))

    opt_caps = []
    for mod_name, label in (
        ("chromadb", "chromadb"),
        ("streamlit", "dashboard"),
        ("mcp", "mcp"),
        ("httpx", "scout"),
        ("trafilatura", "extractor"),
    ):
        opt_caps.append(f"{label}={'yes' if importlib.util.find_spec(mod_name) else 'no'}")
    checks.append(ok("capabilities", ", ".join(opt_caps)))

    if shutil.which("uv"):
        checks.append(ok("uv", "uv is available on PATH"))
    else:
        checks.append(fail("uv", "uv is not available on PATH"))

    required_dirs = [RAW_DIR, INBOX_DIR, ATOMS_DIR, THREADS_DIR, SOURCES_DIR, BRIEFINGS_DIR]
    missing = [path for path in required_dirs if not os.path.isdir(path)]
    if missing:
        checks.append(fail("vault dirs", f"Missing directories: {', '.join(missing)}"))
    else:
        checks.append(ok("vault dirs", "All vault directories exist (including quarantined inbox)"))

    index_manifest = load_index_manifest(CHROMA_PATH)
    json_fallback_file = json_collection_path(CHROMA_PATH, "atoms")
    if not os.path.isdir(CHROMA_PATH) or (index_manifest is None and not os.path.exists(json_fallback_file)):
        checks.append(warn("vector store", "No dense index is installed; model-free lexical recall remains available"))
    else:
        vector_collection = get_vector_collection(CHROMA_PATH, name="atoms", create=False)
        try:
            if isinstance(vector_collection, JsonVectorCollection):
                reason = f" ({vector_collection.fallback_reason})" if vector_collection.fallback_reason else ""
                if not index_manifest or not index_manifest_is_current(index_manifest, ATOMS_DIR):
                    checks.append(warn("vector store", f"JSON fallback files are present but no current generation is served{reason}"))
                else:
                    checks.append(warn("vector store", f"Validated dense generation uses the JSON fallback{reason}"))
            else:
                checks.append(ok("vector store", "ChromaDB persistent client initialized"))
        finally:
            close_vector_collection(vector_collection)

    if index_manifest is None:
        checks.append(warn("index generation", "No verified index manifest; dense recall is intentionally unavailable"))
    elif not index_manifest_is_current(index_manifest, ATOMS_DIR):
        checks.append(warn("index generation", "Index manifest is stale; run the staged vector rebuild"))
    elif index_manifest.get("embedding_model") != EMBED_MODEL:
        checks.append(warn("index generation", "Index model differs from configured embedder; run the staged vector rebuild"))
    else:
        checks.append(ok(
            "index generation",
            f"{index_manifest.get('generation_id')} ({index_manifest.get('indexed_count')} indexed atom(s))",
        ))

    if lexical_index_is_current(ATOMS_DIR, LEXICAL_INDEX_PATH):
        checks.append(ok("lexical recall", "Local FTS index is current and does not require an embedding service"))
    else:
        checks.append(fail("lexical recall", "Local FTS index is missing or stale; run scripts/rebuild_lexical_index.py"))

    current_approvals = 0
    if os.path.isdir(ATOMS_DIR):
        for filename in os.listdir(ATOMS_DIR):
            if not is_content_markdown(filename):
                continue
            atom_path = os.path.join(ATOMS_DIR, filename)
            if approved_revision_is_current(atom_path, brain_dir=BRAIN_DIR):
                current_approvals += 1
    if current_approvals:
        checks.append(ok("approved memories", f"{current_approvals} atom revision(s) have current, verifiable owner approval"))
    else:
        checks.append(warn("approved memories", "No atom has current verifiable owner approval; recall will return no memories"))

    raw_ollama_host = (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").strip().rstrip("/")
    ollama_base = raw_ollama_host if raw_ollama_host.startswith(("http://", "https://")) else f"http://{raw_ollama_host}"
    models_payload = fetch_json(f"{ollama_base}/api/tags")
    if models_payload is None:
        checks.append(warn("ollama", "Ollama is not reachable; local lexical recall remains available, generation and dense indexing are unavailable"))
        names = set()
        vision_names = set()
    else:
        names = model_names(models_payload)
        vision_names = vision_model_names(models_payload)
        checks.append(ok("ollama", f"Ollama returned {len(models_payload.get('models', []))} model(s)"))

    for label, model in [("generation model", GEN_MODEL), ("embedding model", EMBED_MODEL)]:
        if model in names or model.split(":", 1)[0] in names:
            checks.append(ok(label, model))
        else:
            checks.append(warn(label, f"{model} is not installed; this model-backed feature is unavailable"))

    if VISION_MODEL in names or VISION_MODEL.split(":", 1)[0] in names:
        checks.append(ok("vision model", VISION_MODEL))
    elif vision_names:
        checks.append(warn(
            "vision model",
            f"{VISION_MODEL} is not installed; {sorted(vision_names)[0]} is present but vision remains unverified and disabled",
        ))
    else:
        checks.append(warn(
            "vision model",
            f"{VISION_MODEL} is not installed and no vision-capable fallback model was found",
        ))

    checks.append(ok("atoms", f"{count_files(ATOMS_DIR, ('.md',))} atom file(s)"))
    checks.append(ok("threads", f"{count_files(THREADS_DIR, ('.md',))} thread file(s)"))
    checks.append(ok("sources", f"{count_files(SOURCES_DIR, ('.md',))} source file(s)"))

    raw_count = count_files(RAW_DIR, (".md", ".txt", ".intake"))
    if os.path.exists(READING_LIST):
        raw_count = max(0, raw_count - 1)
    inbox_count = count_files(INBOX_DIR, (".md", ".txt", ".intake"))
    checks.append(ok("raw queue", f"{raw_count} queued capture(s), {inbox_count} quarantined in 0-raw/inbox"))
    checks.append(ok("reading list", f"{queued_reading_items()} queued URL(s)"))

    dashboard = fetch_text("http://127.0.0.1:8501/_stcore/health", timeout=2.0)
    if dashboard is None:
        checks.append(warn("dashboard", "Streamlit dashboard is not reachable on 127.0.0.1:8501"))
    else:
        checks.append(ok("dashboard", "Streamlit dashboard health endpoint responded"))

    return checks


def print_checks(checks: list[tuple[str, str, str]]):
    width = max(len(area) for _, area, _ in checks)
    for status, area, detail in checks:
        print(f"[{status:<4}] {area:<{width}}  {detail}")


def main() -> int:
    checks = run_checks()
    print_checks(checks)
    return 1 if any(status == "FAIL" for status, _, _ in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
