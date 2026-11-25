# /// script
# requires-python = ">=3.11,<3.14"
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
    EXPERIENCES_DIR,
    GEN_MODEL,
    INBOX_DIR,
    LEXICAL_INDEX_PATH,
    PLAYBOOKS_DIR,
    RAW_DIR,
    READING_LIST,
    SOURCES_DIR,
    THREADS_DIR,
    VISION_MODEL,
    is_content_markdown,
    resolve_module_vault_path,
    snapshot_module_defaults,
)

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())
from hardware_profile import (
    _NoRedirectHandler,
    detect_hardware_profile,
    fetch_loopback_json,
    loopback_ollama_base_url,
    resolve_active_local_generation_model,
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
    return fetch_loopback_json(url, timeout=timeout)


def fetch_text(url: str, timeout: float = 5.0) -> str | None:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())
        with opener.open(url, timeout=timeout) as response:
            return response.read(65536).decode("utf-8", errors="replace")
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


def queued_reading_items(reading_list_path: str | None = None) -> int:
    target = reading_list_path or resolve_module_vault_path(globals(), "READING_LIST")
    if not os.path.exists(target):
        return 0
    with open(target, "r", encoding="utf-8") as file:
        return sum(
            1
            for line in file
            if line.lstrip().startswith(("- [ ] http", "- [feed] http"))
        )


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


def run_checks(brain_dir: str | None = None) -> list[tuple[str, str, str]]:
    checks = []
    eff_brain = resolve_module_vault_path(globals(), "BRAIN_DIR", brain_dir)
    eff_raw = resolve_module_vault_path(globals(), "RAW_DIR", brain_dir=brain_dir)
    eff_inbox = resolve_module_vault_path(globals(), "INBOX_DIR", brain_dir=brain_dir)
    eff_atoms = resolve_module_vault_path(globals(), "ATOMS_DIR", brain_dir=brain_dir)
    eff_threads = resolve_module_vault_path(globals(), "THREADS_DIR", brain_dir=brain_dir)
    eff_experiences = resolve_module_vault_path(globals(), "EXPERIENCES_DIR", brain_dir=brain_dir)
    eff_playbooks = resolve_module_vault_path(globals(), "PLAYBOOKS_DIR", brain_dir=brain_dir)
    eff_sources = resolve_module_vault_path(globals(), "SOURCES_DIR", brain_dir=brain_dir)
    eff_briefings = resolve_module_vault_path(globals(), "BRIEFINGS_DIR", brain_dir=brain_dir)
    eff_chroma = resolve_module_vault_path(globals(), "CHROMA_PATH", brain_dir=brain_dir)
    eff_lexical = resolve_module_vault_path(globals(), "LEXICAL_INDEX_PATH", brain_dir=brain_dir)
    eff_reading_list = resolve_module_vault_path(globals(), "READING_LIST", brain_dir=brain_dir)

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
        ("onnxruntime", "onnx"),
        ("tokenizers", "tokenizers"),
        ("streamlit", "dashboard"),
        ("mcp", "mcp"),
        ("httpx", "scout"),
        ("trafilatura", "extractor"),
        ("pypdf", "pdf"),
        ("faster_whisper", "whisper"),
        ("PIL", "pillow"),
    ):
        opt_caps.append(f"{label}={'yes' if importlib.util.find_spec(mod_name) else 'no'}")
    checks.append(ok("capabilities", ", ".join(opt_caps)))

    has_whisper = importlib.util.find_spec("faster_whisper") is not None
    has_pillow = importlib.util.find_spec("PIL") is not None
    has_pypdf = importlib.util.find_spec("pypdf") is not None
    if has_whisper and has_pillow and has_pypdf:
        checks.append(ok(
            "multimodal",
            "Audio (faster-whisper), image safety gate (Pillow), and local PDF/DOCX/EPUB/HTML/CSV extractors ready",
        ))
    else:
        missing_parts = []
        if not has_whisper:
            missing_parts.append("faster-whisper (audio ASR)")
        if not has_pillow:
            missing_parts.append("Pillow (image sanitization — image ingestion fails closed without it)")
        if not has_pypdf:
            missing_parts.append("pypdf (PDF extraction)")
        checks.append(warn(
            "multimodal",
            "Missing optional multimodal package(s): "
            + ", ".join(missing_parts)
            + "; install via `setup.ps1 -IncludeMultimodal`, `./setup.sh --include-multimodal`, or `uv sync --frozen --extra multimodal`",
        ))

    if shutil.which("uv"):
        checks.append(ok("uv", "uv is available on PATH"))
    else:
        checks.append(fail("uv", "uv is not available on PATH"))

    required_dirs = [
        eff_raw,
        eff_inbox,
        eff_atoms,
        eff_threads,
        eff_experiences,
        eff_playbooks,
        eff_sources,
        eff_briefings,
    ]
    missing = [path for path in required_dirs if not os.path.isdir(path)]
    if missing:
        checks.append(fail("vault dirs", f"Missing directories: {', '.join(missing)}"))
    else:
        checks.append(ok("vault dirs", "All vault directories exist (including quarantined inbox, experiences, playbooks)"))

    index_manifest = load_index_manifest(eff_chroma)
    json_fallback_file = json_collection_path(eff_chroma, "atoms")
    if not os.path.isdir(eff_chroma) or (index_manifest is None and not os.path.exists(json_fallback_file)):
        checks.append(warn("vector store", "No dense index is installed; model-free lexical recall remains available"))
    else:
        vector_collection = get_vector_collection(eff_chroma, name="atoms", create=False)
        try:
            if isinstance(vector_collection, JsonVectorCollection):
                reason = f" ({vector_collection.fallback_reason})" if vector_collection.fallback_reason else ""
                if not index_manifest or not index_manifest_is_current(index_manifest, eff_atoms):
                    checks.append(warn("vector store", f"JSON fallback files are present but no current generation is served{reason}"))
                else:
                    checks.append(warn("vector store", f"Validated dense generation uses the JSON fallback{reason}"))
            else:
                checks.append(ok("vector store", "ChromaDB persistent client initialized"))
        finally:
            close_vector_collection(vector_collection)

    if index_manifest is None:
        checks.append(warn("index generation", "No verified index manifest; dense recall is intentionally unavailable"))
    elif not index_manifest_is_current(index_manifest, eff_atoms):
        checks.append(warn("index generation", "Index manifest is stale; run the staged vector rebuild"))
    elif index_manifest.get("embedding_model") != EMBED_MODEL:
        checks.append(warn("index generation", "Index model differs from configured embedder; run the staged vector rebuild"))
    else:
        checks.append(ok(
            "index generation",
            f"{index_manifest.get('generation_id')} ({index_manifest.get('indexed_count')} indexed atom(s))",
        ))

    if lexical_index_is_current(eff_atoms, eff_lexical):
        checks.append(ok("lexical recall", "Local FTS index is current and does not require an embedding service"))
    else:
        checks.append(fail("lexical recall", "Local FTS index is missing or stale; run scripts/rebuild_lexical_index.py"))

    current_approvals = 0
    if os.path.isdir(eff_atoms):
        for filename in os.listdir(eff_atoms):
            if not is_content_markdown(filename):
                continue
            atom_path = os.path.join(eff_atoms, filename)
            if approved_revision_is_current(atom_path, brain_dir=eff_brain, atoms_dir=eff_atoms):
                current_approvals += 1
    if current_approvals:
        checks.append(ok("approved memories", f"{current_approvals} atom revision(s) have current, verifiable owner approval"))
    else:
        checks.append(warn("approved memories", "No atom has current verifiable owner approval; recall will return no memories"))

    ollama_base = loopback_ollama_base_url()
    models_payload = fetch_json(f"{ollama_base}/api/tags") if ollama_base else None
    if ollama_base is None:
        checks.append(warn("ollama", "OLLAMA_HOST is not bound to http://127.0.0.1; remote Ollama probe refused for safety"))
        names = set()
        vision_names = set()
    elif models_payload is None:
        checks.append(warn("ollama", "Ollama is not reachable; local lexical recall remains available, generation and dense indexing are unavailable"))
        names = set()
        vision_names = set()
    else:
        names = model_names(models_payload)
        vision_names = vision_model_names(models_payload)
        checks.append(ok("ollama", f"Ollama returned {len(models_payload.get('models', []))} model(s)"))

    active_gen_model = resolve_active_local_generation_model(fallback=GEN_MODEL)
    for label, model in [("generation model", active_gen_model), ("embedding model", EMBED_MODEL)]:
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

    checks.append(ok("atoms", f"{count_files(eff_atoms, ('.md',))} atom file(s)"))
    checks.append(ok("threads", f"{count_files(eff_threads, ('.md',))} thread file(s)"))
    checks.append(ok("experiences", f"{count_files(eff_experiences, ('.md',))} experience record(s)"))
    playbook_total = count_files(eff_playbooks, (".md",))
    active_playbooks = 0
    if playbook_total:
        try:
            from skill_playbooks import list_playbooks

            reports = list_playbooks(
                playbooks_dir=eff_playbooks,
                atoms_dir=eff_atoms,
                brain_dir=eff_brain,
                active_only=False,
            )
            active_playbooks = sum(1 for item in reports if item.valid)
        except Exception:
            active_playbooks = 0
    checks.append(ok("playbooks", f"{playbook_total} playbook file(s) ({active_playbooks} active)"))
    skills_dir = os.path.join(eff_brain, "6-skills")
    skill_total = count_files(skills_dir, (".md",)) if os.path.isdir(skills_dir) else 0
    active_skills = 0
    if skill_total:
        try:
            from executable_skills import list_executable_skills

            sk_reports = list_executable_skills(
                skills_dir=skills_dir,
                playbooks_dir=eff_playbooks,
                atoms_dir=eff_atoms,
                brain_dir=eff_brain,
                active_only=False,
            )
            active_skills = sum(1 for item in sk_reports if item.valid and item.status == "active")
        except Exception:
            active_skills = 0
    checks.append(ok("executable skills", f"{skill_total} executable skill file(s) ({active_skills} active)"))
    checks.append(ok("sources", f"{count_files(eff_sources, ('.md',))} source file(s)"))

    notes_dir = os.path.join(eff_brain, "notes")
    notes_index = os.path.join(eff_brain, ".sidecar", "notes.sqlite3")
    if os.path.isdir(notes_dir):
        idx_state = "indexed" if os.path.isfile(notes_index) else "index created on demand"
        checks.append(ok("notes", f"{count_files(notes_dir, ('.md',))} working note(s) ({idx_state})"))
    else:
        checks.append(warn("notes", "Working notes directory not yet initialized; run `second-brain install`"))

    try:
        from vault_ledger import verify_vault_ledger

        ledger_status = verify_vault_ledger(
            brain_dir=eff_brain,
            atoms_dir=eff_atoms,
            playbooks_dir=eff_playbooks,
        )
        if ledger_status.get("chain_valid", False):
            cp_count = int(ledger_status.get("entry_count", 0))
            synced = "synced" if ledger_status.get("synced_with_head") else "live ahead of last checkpoint"
            checks.append(
                ok(
                    "vault ledger",
                    f"chain valid ({cp_count} checkpoint(s), {synced})" if cp_count else "chain valid (no checkpoints committed yet)",
                )
            )
        else:
            checks.append(fail("vault ledger", f"Tamper or hash-chain error: {ledger_status.get('errors')}"))
    except Exception as exc:
        checks.append(warn("vault ledger", f"Could not verify vault ledger: {exc}"))

    hw_profile = detect_hardware_profile(ollama_timeout=2.0)
    checks.append(
        ok(
            "capability tier",
            f"{hw_profile.tier_label} (recommended={hw_profile.recommended_generation_model}, "
            f"installed={hw_profile.selected_installed_generation_model or 'none'})",
        )
    )

    try:
        from onnx_models import get_onnx_models_status

        onnx_st = get_onnx_models_status(run_inference=False)
        ce_info = onnx_st["slots"]["cross-encoder"]
        nli_info = onnx_st["slots"]["nli"]
        corrupt_slots = [
            s for s, info in onnx_st["slots"].items() if info.get("installed") and not info.get("verified")
        ]
        if corrupt_slots:
            checks.append(
                warn(
                    "onnx models",
                    f"Unverified local ONNX files in {', '.join(corrupt_slots)}; run `second-brain models pull --force`",
                )
            )
        elif onnx_st["all_verified"]:
            checks.append(
                ok(
                    "onnx models",
                    f"SHA-256 verified ({ce_info.get('model_id')}, {nli_info.get('model_id')})",
                )
            )
        else:
            checks.append(
                ok(
                    "onnx models",
                    f"cross-encoder={ce_info['active_backend']}, nli={nli_info['active_backend']} "
                    "(run `second-brain models pull` for neural ONNX weights)",
                )
            )
    except Exception as exc:
        checks.append(warn("onnx models", f"Could not inspect ONNX models: {exc}"))

    raw_count = count_files(eff_raw, (".md", ".txt", ".intake"))
    if os.path.exists(eff_reading_list):
        raw_count = max(0, raw_count - 1)
    inbox_count = count_files(eff_inbox, (".md", ".txt", ".intake"))
    checks.append(ok("raw queue", f"{raw_count} queued capture(s), {inbox_count} quarantined in 0-raw/inbox"))
    checks.append(ok("reading list", f"{queued_reading_items(eff_reading_list)} queued URL(s)"))

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


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Second Brain health check.")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON health report.")
    args = parser.parse_args(argv)
    checks = run_checks()
    failed = any(status == "FAIL" for status, _, _ in checks)
    if args.json:
        print(
            json.dumps(
                {
                    "ok": not failed,
                    "checks": [
                        {"status": status, "area": area, "detail": detail}
                        for status, area, detail in checks
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print_checks(checks)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
