# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#     "httpx>=0.28,<1",
# ]
# ///
"""Loopback llama.cpp speculative decoding server supervisor (draft + target GGUF).

Manages a strictly loopback-bound ``llama-server`` process (``127.0.0.1:<port>``)
pairing a small draft GGUF (e.g. ``Qwen2.5-Coder-0.5B/1.5B`` or ``Llama-3.2-1B``)
with an ``IQ3_S`` / ``Q4_K_M`` target GGUF, scaling threads and GPU layers
automatically via ``hardware_profile.py`` without heavy dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ in ("llama_cpp_runner", "scripts.llama_cpp_runner"):
    sys.modules.setdefault("llama_cpp_runner", sys.modules[__name__])
    sys.modules.setdefault("scripts.llama_cpp_runner", sys.modules[__name__])

from atom_mutator import atomic_write_bytes  # noqa: E402
from config import _is_reparse_or_link, security_state_dir  # noqa: E402
from hardware_profile import (  # noqa: E402
    detect_hardware_profile,
    detect_physical_cpu_cores,
    parse_gguf_quant_tag,
    recommended_ollama_runtime_options,
)
from host_adapters import (  # noqa: E402
    LLAMA_CPP_SPECULATIVE_HOST_ID,
    register_speculative_host_policy,
    restrict_security_file_permissions,
)

DEFAULT_SPECULATIVE_PORT = 8081
STATE_FILENAME = "llama-speculative.json"


class SpeculativeRunnerError(ValueError):
    """Raised on invalid GGUF paths, port bounds, or unavailable llama-server."""


def speculative_state_path(state_dir: str | Path | None = None) -> Path:
    raw_base = Path(os.path.expanduser(str(state_dir))) if state_dir is not None else Path(security_state_dir())
    if os.path.lexists(str(raw_base)) and _is_reparse_or_link(str(raw_base)):
        raise PermissionError(f"Refusing symlinked security state dir: {raw_base}")
    base = raw_base.resolve()
    if os.path.lexists(str(base)) and _is_reparse_or_link(str(base)):
        raise PermissionError(f"Refusing symlinked security state dir: {base}")
    base.mkdir(parents=True, exist_ok=True)
    target = base / STATE_FILENAME
    if os.path.lexists(str(target)) and _is_reparse_or_link(str(target)):
        raise PermissionError(f"Refusing symlinked speculative state file: {target}")
    return target


def resolve_llama_server_binary() -> str | None:
    explicit = (os.environ.get("SECOND_BRAIN_LLAMA_SERVER_BIN") or "").strip()
    if explicit:
        p = Path(explicit)
        if p.is_file() and not _is_reparse_or_link(str(p)):
            return str(p.resolve())
        return None
    for cand in ("llama-server", "llama-server.exe"):
        found = shutil.which(cand)
        if found and not _is_reparse_or_link(found):
            return str(Path(found).resolve())
    return None


def _validate_gguf_path(raw_path: str, label: str) -> Path:
    clean = str(raw_path or "").strip()
    if not clean:
        raise SpeculativeRunnerError(f"{label} GGUF path is required.")
    p = Path(os.path.expanduser(clean))
    if os.path.lexists(str(p)) and _is_reparse_or_link(str(p)):
        raise PermissionError(f"Refusing symlinked {label} GGUF path: {p}")
    if os.path.lexists(str(p.parent)) and _is_reparse_or_link(str(p.parent)):
        raise PermissionError(f"Refusing symlinked parent on {label} GGUF: {p.parent}")
    resolved = p.resolve()
    if _is_reparse_or_link(str(resolved)) or _is_reparse_or_link(str(resolved.parent)):
        raise PermissionError(f"Refusing symlinked parent on {label} GGUF: {resolved}")
    if resolved.suffix.lower() != ".gguf":
        raise SpeculativeRunnerError(f"{label} model must be a .gguf file: {resolved.name}")
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise FileNotFoundError(f"{label} GGUF file not found or empty: {resolved}")
    return resolved


def _get_process_image_name(pid: int) -> str | None:
    """Return lowercased executable basename for ``pid``, or None if unavailable."""
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return None
        try:
            buf_len = ctypes.c_ulong(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len)):
                return os.path.basename(buf.value).lower()
            return None
        finally:
            kernel32.CloseHandle(handle)
    for proc_path in (f"/proc/{int(pid)}/comm", f"/proc/{int(pid)}/cmdline"):
        try:
            raw = Path(proc_path).read_bytes()
            if raw:
                first = raw.split(b"\x00", 1)[0].decode("utf-8", errors="ignore").strip()
                if first:
                    return os.path.basename(first).lower()
        except OSError:
            pass
    try:
        proc = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return os.path.basename(proc.stdout.strip()).lower()
    except Exception:
        pass
    return None


def _pid_matches_llama_server(pid: int) -> bool:
    """Verify ``pid`` still belongs to a `llama-server` executable before sending kill signals."""
    image = _get_process_image_name(pid)
    return image in {"llama-server", "llama-server.exe"}


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return int(kernel32.GetLastError()) == 5
        try:
            code = ctypes.c_ulong(0)
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return int(code.value) == 259
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def build_speculative_server_command(
    *,
    server_bin: str,
    target_model: str | Path,
    draft_model: str | Path,
    port: int = DEFAULT_SPECULATIVE_PORT,
    draft_max: int = 8,
    draft_min: int = 2,
    threads: int | None = None,
    ctx_size: int | None = None,
    n_gpu_layers: int | None = None,
) -> list[str]:
    if not (1024 <= int(port) <= 65535):
        raise SpeculativeRunnerError("Speculative server port must be in 1024..65535.")
    if int(draft_min) < 1 or int(draft_max) < int(draft_min) or int(draft_max) > 32:
        raise SpeculativeRunnerError("Invalid draft window bounds (--draft-min/--draft-max).")

    hw = None
    if threads is None or ctx_size is None or n_gpu_layers is None:
        try:
            hw = detect_hardware_profile(use_cache=True)
        except Exception:
            hw = None
    opts = recommended_ollama_runtime_options(hw, model_name=str(target_model))
    eff_threads = max(1, min(128, int(threads if threads is not None else opts["num_thread"])))
    eff_ctx = max(1024, min(131072, int(ctx_size if ctx_size is not None else opts["num_ctx"])))
    if n_gpu_layers is not None:
        eff_ngl = max(0, min(999, int(n_gpu_layers)))
    elif hw is not None and str(getattr(hw, "tier_id", "")) == "tier_3_gpu_accelerated":
        eff_ngl = 99
    else:
        eff_ngl = 0

    return [
        str(server_bin),
        "--host",
        "127.0.0.1",
        "--port",
        str(int(port)),
        "-m",
        str(target_model),
        "-md",
        str(draft_model),
        "--draft-max",
        str(int(draft_max)),
        "--draft-min",
        str(int(draft_min)),
        "-t",
        str(eff_threads),
        "-c",
        str(eff_ctx),
        "--n-gpu-layers",
        str(eff_ngl),
    ]


def _load_state(state_file: Path) -> dict[str, Any]:
    if not state_file.is_file():
        return {}
    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def speculative_status(
    *,
    state_dir: str | Path | None = None,
    port: int | None = None,
    http_client: Any = None,
) -> dict[str, Any]:
    state_file = speculative_state_path(state_dir)
    st = _load_state(state_file)
    bin_path = resolve_llama_server_binary()
    eff_port = int(port or st.get("port") or DEFAULT_SPECULATIVE_PORT)
    pid = int(st.get("pid") or 0)
    pid_alive = _is_pid_running(pid) if pid > 0 else False

    health_ok = False
    endpoint_base = f"http://127.0.0.1:{eff_port}"
    try:
        if http_client is not None:
            resp = http_client.get(f"{endpoint_base}/health", timeout=1.5)
            health_ok = resp.status_code == 200
        elif pid_alive:
            with httpx.Client(trust_env=False, timeout=1.5) as client:
                resp = client.get(f"{endpoint_base}/health")
                health_ok = resp.status_code == 200
    except Exception:
        health_ok = False

    if pid > 0 and not pid_alive and st.get("running"):
        st["running"] = False
        atomic_write_bytes(str(state_file), (json.dumps(st, indent=2) + "\n").encode("utf-8"))
        restrict_security_file_permissions(state_file)

    return {
        "available": bool(bin_path),
        "llama_server_bin": bin_path,
        "status": "running" if (pid_alive or health_ok) else ("ready" if bin_path else "llama_server_unavailable"),
        "running": bool(pid_alive or health_ok),
        "health_ok": health_ok,
        "pid": pid if pid_alive else None,
        "port": eff_port,
        "endpoint_url": f"{endpoint_base}/v1",
        "host_id": LLAMA_CPP_SPECULATIVE_HOST_ID,
        "target_model": st.get("target_model"),
        "target_quant": parse_gguf_quant_tag(st.get("target_model")),
        "draft_model": st.get("draft_model"),
        "physical_cores": detect_physical_cpu_cores(),
        "draft_max": st.get("draft_max", 8),
        "draft_min": st.get("draft_min", 2),
        "command": st.get("command") or [],
    }


def start_speculative_server(
    target_model: str,
    draft_model: str,
    *,
    port: int = DEFAULT_SPECULATIVE_PORT,
    draft_max: int = 8,
    draft_min: int = 2,
    threads: int | None = None,
    ctx_size: int | None = None,
    n_gpu_layers: int | None = None,
    state_dir: str | Path | None = None,
    policy_path: Path | None = None,
    register_host: bool = True,
    popen_fn: Any = subprocess.Popen,
) -> dict[str, Any]:
    target_p = _validate_gguf_path(target_model, "Target")
    draft_p = _validate_gguf_path(draft_model, "Draft")
    if target_p == draft_p:
        raise SpeculativeRunnerError("Target and draft GGUF paths must be distinct files.")

    server_bin = resolve_llama_server_binary()
    if not server_bin:
        raise FileNotFoundError(
            "llama-server binary not found on PATH (or set SECOND_BRAIN_LLAMA_SERVER_BIN)."
        )

    state_file = speculative_state_path(state_dir)
    existing_state = _load_state(state_file)
    existing_pid = int(existing_state.get("pid") or 0)
    if existing_pid > 0 and _is_pid_running(existing_pid):
        stop_speculative_server(state_dir=state_dir)

    cmd = build_speculative_server_command(
        server_bin=server_bin,
        target_model=target_p,
        draft_model=draft_p,
        port=port,
        draft_max=draft_max,
        draft_min=draft_min,
        threads=threads,
        ctx_size=ctx_size,
        n_gpu_layers=n_gpu_layers,
    )

    proc = popen_fn(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )
    endpoint_url = f"http://127.0.0.1:{int(port)}/v1"
    target_alias = target_p.stem[:96]
    if register_host:
        register_speculative_host_policy(
            endpoint_url=endpoint_url,
            target_model=target_alias,
            enabled=True,
            policy_path=policy_path,
        )

    state_doc = {
        "running": True,
        "pid": int(getattr(proc, "pid", 0) or 0),
        "port": int(port),
        "endpoint_url": endpoint_url,
        "host_id": LLAMA_CPP_SPECULATIVE_HOST_ID,
        "target_model": str(target_p),
        "target_quant": parse_gguf_quant_tag(target_p.name),
        "draft_model": str(draft_p),
        "draft_max": int(draft_max),
        "draft_min": int(draft_min),
        "command": cmd,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    atomic_write_bytes(str(state_file), (json.dumps(state_doc, indent=2) + "\n").encode("utf-8"))
    restrict_security_file_permissions(state_file)
    return state_doc


def stop_speculative_server(*, state_dir: str | Path | None = None) -> dict[str, Any]:
    state_file = speculative_state_path(state_dir)
    st = _load_state(state_file)
    pid = int(st.get("pid") or 0)
    stopped = False
    pid_verified = False
    if pid > 0 and _is_pid_running(pid):
        if _pid_matches_llama_server(pid):
            pid_verified = True
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=5, check=False)
                else:
                    os.kill(pid, 15)
                stopped = True
            except Exception:
                stopped = False
    st["running"] = False
    st["pid"] = None
    atomic_write_bytes(str(state_file), (json.dumps(st, indent=2) + "\n").encode("utf-8"))
    restrict_security_file_permissions(state_file)
    return {
        "status": "stopped" if stopped else "not_running",
        "previous_pid": pid or None,
        "pid_verified": pid_verified,
    }


def benchmark_speculative_server(
    *,
    prompt: str = "Summarize why local-first knowledge bases require immutable provenance anchors in 3 bullet points.",
    max_tokens: int = 96,
    port: int | None = None,
    state_dir: str | Path | None = None,
    http_client: Any = None,
) -> dict[str, Any]:
    st = speculative_status(state_dir=state_dir, port=port, http_client=http_client)
    endpoint_url = str(st["endpoint_url"])
    model_name = Path(str(st.get("target_model") or "speculative-local")).stem
    req_body = {
        "model": model_name,
        "messages": [{"role": "user", "content": str(prompt)[:2000]}],
        "max_tokens": max(16, min(512, int(max_tokens))),
        "temperature": 0.0,
        "stream": False,
    }
    t0 = time.perf_counter()
    if http_client is not None:
        resp = http_client.post(f"{endpoint_url}/chat/completions", json=req_body, timeout=60.0)
    else:
        if not st.get("running"):
            raise RuntimeError("Speculative llama-server is not running. Start it with target + draft GGUF paths first.")
        with httpx.Client(trust_env=False, timeout=60.0) as client:
            resp = client.post(f"{endpoint_url}/chat/completions", json=req_body)
    if resp.status_code != 200:
        raise RuntimeError(f"Speculative benchmark request returned HTTP {resp.status_code}")
    elapsed_s = max(0.001, time.perf_counter() - t0)
    payload = resp.json() if hasattr(resp, "json") else {}
    usage = payload.get("usage") or {}
    comp_tokens = int(usage.get("completion_tokens") or 0)
    timings = payload.get("timings") or {}
    predicted_tps = float(timings.get("predicted_per_second") or (comp_tokens / elapsed_s if comp_tokens else 0.0))
    return {
        "status": "ok",
        "endpoint_url": endpoint_url,
        "completion_tokens": comp_tokens,
        "elapsed_seconds": round(elapsed_s, 3),
        "tokens_per_second": round(predicted_tps, 2),
        "draft_n": timings.get("draft_n"),
        "draft_n_accepted": timings.get("draft_n_accepted"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_stat = sub.add_parser("status", help="Inspect llama-server binary, physical cores & speculative status")
    p_stat.add_argument("--port", type=int, default=None)
    p_stat.add_argument("--json", action="store_true")

    p_start = sub.add_parser("start", help="Start loopback llama-server with target + draft GGUF")
    p_start.add_argument("--target", required=True, help="Path to target .gguf model (e.g. IQ3_S or Q4_K_M)")
    p_start.add_argument("--draft", required=True, help="Path to small draft .gguf model")
    p_start.add_argument("--port", type=int, default=DEFAULT_SPECULATIVE_PORT)
    p_start.add_argument("--draft-max", type=int, default=8)
    p_start.add_argument("--draft-min", type=int, default=2)
    p_start.add_argument("--threads", type=int, default=None)
    p_start.add_argument("--ctx-size", type=int, default=None)
    p_start.add_argument("--ngl", type=int, default=None)
    p_start.add_argument("--no-register-host", action="store_true")
    p_start.add_argument("--json", action="store_true")

    p_stop = sub.add_parser("stop", help="Stop tracked loopback speculative llama-server")
    p_stop.add_argument("--json", action="store_true")

    p_bench = sub.add_parser("benchmark", help="Measure tokens/sec on the running speculative llama-server")
    p_bench.add_argument("--port", type=int, default=None)
    p_bench.add_argument("--max-tokens", type=int, default=96)
    p_bench.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "status":
        res = speculative_status(port=args.port)
    elif args.cmd == "start":
        res = start_speculative_server(
            args.target,
            args.draft,
            port=args.port,
            draft_max=args.draft_max,
            draft_min=args.draft_min,
            threads=args.threads,
            ctx_size=args.ctx_size,
            n_gpu_layers=args.ngl,
            register_host=not args.no_register_host,
        )
    elif args.cmd == "stop":
        res = stop_speculative_server()
    else:
        res = benchmark_speculative_server(port=args.port, max_tokens=args.max_tokens)

    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
