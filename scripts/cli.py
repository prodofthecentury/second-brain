"""Stable bootstrap commands for a Second Brain installation."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import inspect
import platform
import sys
from pathlib import Path
import os
import subprocess
import json
import urllib.error
import urllib.request

# Existing internal modules use direct imports when executed from the scripts
# directory. Preserve that import contract inside both repo checkouts and wheels.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
_RUNTIME_ROOT = str(Path(__file__).resolve().parent.parent)
if _RUNTIME_ROOT not in sys.path:
    sys.path.insert(0, _RUNTIME_ROOT)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


from scripts import config as _config
from scripts.config import _default_installed_vault

if not (os.environ.get("BRAIN_VAULT_DIR") or "").strip():
    installed_vault = _config.read_persisted_vault_dir() or _default_installed_vault()
    if installed_vault:
        os.environ["BRAIN_VAULT_DIR"] = installed_vault

BRAIN_DIR = _config.resolve_active_brain_dir()
_INITIAL_CLI_BRAIN_DIR = BRAIN_DIR
_INITIAL_CONFIG_BRAIN_DIR = _config.BRAIN_DIR
ensure_dirs = _config.ensure_dirs


def _effective_cli_brain_dir() -> str:
    if BRAIN_DIR != _INITIAL_CLI_BRAIN_DIR:
        return os.path.abspath(BRAIN_DIR)
    if _config.BRAIN_DIR != _INITIAL_CONFIG_BRAIN_DIR:
        return os.path.abspath(_config.BRAIN_DIR)
    return _config.resolve_active_brain_dir()


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            # ERROR_ACCESS_DENIED (5) means process exists under another token
            return int(kernel32.GetLastError()) == 5
        try:
            exit_code = ctypes.c_ulong(0)
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return int(exit_code.value) == 259  # STILL_ACTIVE
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


def _discover_api_port_fallback(default_port: int = 8765) -> int:
    port_file = _config.api_port_file()
    try:
        if not os.path.lexists(str(port_file)) or _config._is_reparse_or_link(str(port_file)):
            return default_port
        if os.path.lexists(str(port_file.parent)) and _config._is_reparse_or_link(str(port_file.parent)):
            return default_port
        st = os.lstat(str(port_file))
        if st.st_size <= 0 or st.st_size > 4096:
            return default_port
        raw = port_file.read_text(encoding="utf-8").strip()
        if not raw:
            return default_port
        if raw.isdigit():
            port_val = int(raw)
            return port_val if 1 <= port_val <= 65535 else default_port
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return default_port
        port_val = int(parsed.get("port", 0))
        if not (1 <= port_val <= 65535):
            return default_port
        pid_val = parsed.get("pid")
        if pid_val is not None and not _is_pid_alive(int(pid_val)):
            return default_port
        return port_val
    except Exception:
        return default_port


def _capability(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "HTTP redirects are forbidden on loopback probes", headers, fp)


def _ollama_tags_url(raw_host: str | None = None) -> str | None:
    """Validate that OLLAMA_HOST targets strictly http://127.0.0.1 or http://[::1] and return /api/tags URL."""
    from scripts.hardware_profile import loopback_ollama_base_url

    base = loopback_ollama_base_url(raw_host)
    return f"{base}/api/tags" if base else None


def doctor() -> int:
    """Report runtime capabilities and locally reachable model services."""
    from scripts.doctor import gpu_driver_status, native_cpp_runtime_status, system_memory_summary
    from scripts.vector_store import JsonVectorCollection, close_vector_collection, get_vector_collection, json_collection_path

    vp = _config.vault_paths_for(_effective_cli_brain_dir())
    active_brain_dir = vp["brain_dir"]
    print(f"Second Brain {platform.python_version()} on {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"Vault: {active_brain_dir}")
    print(f"CPU threads: {os.cpu_count() or 'unknown'}")
    print(f"System memory: {system_memory_summary()}")
    cpp_ok, cpp_detail = native_cpp_runtime_status()
    print(f"C++ runtime: {'ready' if cpp_ok else 'missing'} ({cpp_detail})")
    print(f"Compute device: {gpu_driver_status()}")
    try:
        from scripts.hardware_profile import detect_hardware_profile

        hw = detect_hardware_profile(ollama_timeout=2.0)
        print(
            f"Capability tier: {hw.tier_label} "
            f"(recommended={hw.recommended_generation_model}, selected={hw.selected_installed_generation_model or 'none'})"
        )
    except Exception:
        pass
    capabilities = {
        "lexical_search_engine": True,
        "online_model_adapter": _capability("ollama"),
        "dashboard": _capability("streamlit"),
        "mcp": _capability("mcp"),
        "dense_index": _capability("chromadb"),
        "onnx_reranker_runtime": _capability("onnxruntime") and _capability("tokenizers"),
        "scout_http": _capability("httpx"),
        "extractor": _capability("trafilatura"),
        "document_pdf": _capability("pypdf"),
        "vault_sync_crypto": _capability("cryptography"),
        "multimodal_audio": _capability("faster_whisper"),
        "image_processing_library": _capability("PIL"),
    }
    for name, ready in capabilities.items():
        print(f"{'ready' if ready else 'not installed':16} {name}")
    if not capabilities["multimodal_audio"] or not capabilities["image_processing_library"]:
        print(
            "Multimodal optional extras: install via `./setup.ps1 -IncludeMultimodal`, "
            "`./setup.sh --include-multimodal`, or `uv sync --frozen --extra multimodal` "
            "(image ingestion fails closed without Pillow)."
        )
    atoms_dir = Path(vp["atoms_dir"])
    atom_files = sorted(path for path in atoms_dir.glob("*.md") if path.name != "README.md") if atoms_dir.is_dir() else []
    print(f"Atoms: {len(atom_files)}")
    playbooks_dir = Path(vp["playbooks_dir"])
    playbook_files = (
        sorted(path for path in playbooks_dir.glob("*.md") if path.name != "README.md")
        if playbooks_dir.is_dir()
        else []
    )
    print(f"Playbooks: {len(playbook_files)}")
    skills_dir = Path(vp.get("skills_dir") or os.path.join(active_brain_dir, "6-skills"))
    skill_files = (
        sorted(path for path in skills_dir.glob("*.md") if path.name != "README.md")
        if skills_dir.is_dir()
        else []
    )
    print(f"Executable skills: {len(skill_files)}")
    notes_dir = Path(vp["notes_dir"])
    note_files = (
        sorted(path for path in notes_dir.glob("*.md") if path.name != "README.md")
        if notes_dir.is_dir()
        else []
    )
    print(f"Working notes: {len(note_files)}")
    required_dirs = (
        vp["raw_dir"],
        vp["inbox_dir"],
        vp["atoms_dir"],
        vp["threads_dir"],
        vp["experiences_dir"],
        vp["playbooks_dir"],
        vp["sources_dir"],
        vp["briefings_dir"],
    )
    missing_dirs = [d for d in required_dirs if not os.path.isdir(d)]
    if missing_dirs:
        print(f"Vault directories: missing ({', '.join(missing_dirs)})")
    try:
        from scripts.review_atom import approved_revision_is_current

        approved = sum(
            1
            for path in atom_files
            if approved_revision_is_current(str(path), brain_dir=active_brain_dir, atoms_dir=vp["atoms_dir"])
        )
        print(f"Current owner-approved atoms: {approved}")
    except OSError:
        print("Current owner-approved atoms: could not verify")
    from scripts.lexical_store import lexical_index_is_current

    lexical_ready = lexical_index_is_current(vp["atoms_dir"], vp["lexical_index_path"])
    print(f"Lexical index: {'current' if lexical_ready else 'missing or stale'}")
    ledger_ok = True
    try:
        from scripts.vault_ledger import verify_vault_ledger

        ledger_res = verify_vault_ledger(
            brain_dir=active_brain_dir,
            atoms_dir=vp["atoms_dir"],
            playbooks_dir=vp["playbooks_dir"],
        )
        ledger_ok = bool(ledger_res.get("chain_valid", False))
        print(f"Vault ledger: {'valid' if ledger_ok else 'INVALID'} ({ledger_res.get('entry_count', 0)} checkpoint(s))")
    except Exception as exc:
        print(f"Vault ledger: could not verify ({exc})")
    try:
        tags_url = _ollama_tags_url()
        if not tags_url:
            raise urllib.error.URLError("OLLAMA_HOST must target http://127.0.0.1 or http://[::1]")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())
        with opener.open(tags_url, timeout=5.0) as response:
            payload = json.loads(response.read(65536).decode("utf-8"))
        if not isinstance(payload, dict):
            raise urllib.error.URLError("Ollama /api/tags unavailable")
        models = sorted(item.get("name", item.get("model", "unknown")) for item in payload.get("models", []))
        print(f"Ollama service: ready ({len(models)} model(s))")
        print(f"Ollama models: {', '.join(models) if models else 'none installed'}")
        from scripts.hardware_profile import resolve_active_local_generation_model

        active_gen_model = resolve_active_local_generation_model(fallback=_config.GEN_MODEL)
        model_roots = {name.split(":", 1)[0] for name in models}
        for label, configured in (("Generation", active_gen_model), ("Embedding", _config.EMBED_MODEL), ("Vision", _config.VISION_MODEL)):
            present = configured in models or configured.split(":", 1)[0] in model_roots
            print(f"{label} model {configured}: {'available' if present else 'not installed'}")
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        print("Ollama service: unavailable; configured generation and embedding models cannot be used")
        print("Offline lexical recall remains usable when its index is current")
    try:
        from scripts.index_manifest import index_manifest_is_current, load_index_manifest

        chroma_path = vp["chroma_path"]
        manifest = load_index_manifest(chroma_path)
        backend_note = ""
        json_fallback_file = json_collection_path(chroma_path, "atoms")
        if os.path.isdir(chroma_path) and (manifest is not None or os.path.exists(json_fallback_file)):
            collection = get_vector_collection(chroma_path, name="atoms", create=False)
            try:
                if isinstance(collection, JsonVectorCollection):
                    reason = collection.fallback_reason or "chromadb unavailable"
                    backend_note = f" [degraded JSON backend: {reason}]"
            finally:
                close_vector_collection(collection)
        if manifest and index_manifest_is_current(manifest, vp["atoms_dir"]):
            print(
                "Dense index: current; model={} dimensions={} generation={}{}".format(
                    manifest.get("embedding_model"),
                    manifest.get("embedding_dimensions"),
                    manifest.get("generation_id"),
                    backend_note,
                )
            )
        elif manifest is not None:
            print(f"Dense index: stale manifest (generation={manifest.get('generation_id')}); run scripts/rebuild_vectors.py")
        else:
            print("Dense index: unavailable (no active manifest); lexical FTS5 handles recall cleanly")
    except (OSError, ValueError, TypeError) as exc:
        print(f"Dense index: unavailable or invalid ({exc}); no dense results will be served")
    try:
        from scripts.onnx_models import get_onnx_models_status

        onnx_st = get_onnx_models_status(run_inference=False)
        ce_info = onnx_st["slots"]["cross-encoder"]
        nli_info = onnx_st["slots"]["nli"]
        print(
            f"ONNX neural models: cross-encoder={ce_info['active_backend']} "
            f"({'verified' if ce_info['verified'] else 'fallback'}), "
            f"nli={nli_info['active_backend']} ({'verified' if nli_info['verified'] else 'fallback'})"
        )
    except Exception:
        pass
    return 1 if (missing_dirs or not lexical_ready or not ledger_ok) else 0


def _resolve_active_vault_paths() -> tuple[str, str, str]:
    vault_dir = _effective_cli_brain_dir()
    if vault_dir == os.path.abspath(_config.BRAIN_DIR):
        ensure_dirs()
        return vault_dir, _config.ATOMS_DIR, _config.LEXICAL_INDEX_PATH
    ensure_dirs(vault_dir)
    vp = _config.vault_paths_for(vault_dir)
    return vp["brain_dir"], vp["atoms_dir"], vp["lexical_index_path"]


def install(args: argparse.Namespace | None = None) -> int:
    vault_dir, atoms_dir, lexical_index_path = _resolve_active_vault_paths()
    print(f"Created/verified vault at {vault_dir}")
    if getattr(args, "no_index", False) and os.path.isfile(lexical_index_path):
        return 0
    from scripts.lexical_store import rebuild_lexical_index
    from scripts.review_atom import review_lock

    lock_path = os.path.join(os.path.dirname(lexical_index_path), "writer.lock")
    try:
        with review_lock(lock_path):
            stats = rebuild_lexical_index(atoms_dir, lexical_index_path)
    except Exception as exc:
        print(f"Lexical index rebuild failed: {exc}", file=sys.stderr)
        return 1
    print(f"Lexical recall initialized with {stats['indexed_count']} atom(s).")
    try:
        from scripts.notes_store import NotePaths, ensure_notes_index

        ensure_notes_index(NotePaths.for_brain(vault_dir))
    except Exception as exc:
        print(f"Notes index initialization warning: {exc}", file=sys.stderr)
    print("Model-free local setup is ready. Inspect installed capabilities with `second-brain doctor`.")
    if _capability("streamlit"):
        print("Start the local dashboard with `second-brain dashboard`.")
    else:
        print("Install runtime dependencies with `uv sync --frozen`.")
    if not _capability("ollama"):
        print("Ollama Python client is not installed. Run `uv sync --frozen`; an Ollama daemon and model are separate.")
    return 0


cmd_install = install


def _resolve_writable_dashboard_cwd(app_path: Path) -> str:
    root_dir = app_path.parent.parent
    if (root_dir / "pyproject.toml").is_file() and os.access(str(root_dir), os.W_OK):
        return str(root_dir)
    vault_dir = (os.environ.get("BRAIN_VAULT_DIR") or "").strip() or _config.BRAIN_DIR
    try:
        os.makedirs(vault_dir, exist_ok=True)
        if os.access(vault_dir, os.W_OK):
            return vault_dir
    except OSError:
        pass
    import tempfile

    return tempfile.gettempdir()


def launch_dashboard(port: int = 8501) -> int:
    if not (1 <= int(port) <= 65535):
        print("--port must be between 1 and 65535.", file=sys.stderr)
        return 2
    if not _capability("streamlit"):
        print("Dashboard dependencies are not installed. Run `uv sync --frozen` or install `second-brain[dashboard]`.", file=sys.stderr)
        return 2
    from scripts.dashboard_auth import configure_password_interactively, load_password_record

    if load_password_record() is None and not configure_password_interactively():
        return 1
    app_path = Path(__file__).resolve().parent.parent / "dashboard" / "app.py"
    if not app_path.is_file():
        print("Dashboard files are missing from this installation.", file=sys.stderr)
        return 2
    bound_port = int(port)
    run_cwd = _resolve_writable_dashboard_cwd(app_path)
    dash_env = dict(os.environ)
    dash_env.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    dash_env.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    if not (dash_env.get("BRAIN_VAULT_DIR") or "").strip():
        if not _config.read_persisted_vault_dir():
            installed_vault = _default_installed_vault()
            if installed_vault:
                dash_env["BRAIN_VAULT_DIR"] = installed_vault
    return subprocess.call(
        [
            sys.executable, "-m", "streamlit", "run", str(app_path),
            "--server.headless=true",
            "--server.address=127.0.0.1",
            f"--server.port={bound_port}",
            "--server.enableCORS=true",
            "--server.enableXsrfProtection=true",
            "--server.xsrfCookieSameSite=strict",
            "--server.allowedHosts=127.0.0.1",
            f"--server.corsAllowedOrigins=http://127.0.0.1:{bound_port}",
            "--client.toolbarMode=minimal",
            "--browser.serverAddress=127.0.0.1",
            "--browser.gatherUsageStats=false",
        ],
        cwd=run_cwd,
        env=dash_env,
    )


def main(argv: list[str] | None = None) -> int:
    had_env_vault = bool((os.environ.get("BRAIN_VAULT_DIR") or "").strip())
    injected_vault = False
    if not had_env_vault:
        persisted_or_installed = _config.read_persisted_vault_dir() or _default_installed_vault()
        if persisted_or_installed:
            os.environ["BRAIN_VAULT_DIR"] = persisted_or_installed
            injected_vault = True
    try:
        return _main_impl(argv)
    finally:
        if injected_vault and argv is not None:
            os.environ.pop("BRAIN_VAULT_DIR", None)


def _main_impl(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="second-brain", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="Report local runtime capabilities.")
    dashboard_parser = subparsers.add_parser("dashboard", help="Start the local password-protected dashboard.")
    dashboard_parser.add_argument("--port", type=int, default=8501, help="Loopback TCP port (default 8501).")
    dashboard_parser.add_argument("--headless", action="store_true", default=True, help="Run headless without opening a browser.")
    install_parser = subparsers.add_parser("install", help="Initialize the vault and model-free lexical index.")
    install_parser.add_argument("--no-index", action="store_true", help="Skip reindexing if lexical index already exists.")

    profile_parser = subparsers.add_parser("profile", help="Inspect host hardware tier and model recommendations.")
    profile_parser.add_argument("--json", action="store_true", help="Output structured JSON hardware profile.")

    serve_parser = subparsers.add_parser("serve", help="Start the loopback 127.0.0.1 HTTP JSON Memory API.")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Loopback bind host (must be 127.0.0.1).")
    serve_parser.add_argument("--port", type=int, default=8765, help="Loopback TCP port (default 8765; 0 for ephemeral).")
    serve_parser.add_argument("--generate-token", action="store_true", help="Generate a 256-bit API bearer token and exit.")
    serve_parser.add_argument("--ensure-tokens", action="store_true", help="Create agent and owner tokens if missing.")

    adapter_parser = subparsers.add_parser("adapter-config", help="Print IDE and custom host adapter configuration JSON.")
    adapter_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Loopback API TCP port (auto-discovers live port when omitted, falling back to 8765).",
    )

    for name, help_text in (
        ("api", "Run or configure the loopback 127.0.0.1 HTTP JSON Memory API."),
        ("inbox", "Inspect, promote, or reject quarantined 0-raw/inbox captures."),
        ("hosts", "Inspect and configure per-host AI sharing consent policies."),
        ("cognitive", "Generate memory fitness reports, record feedback, and log experiences."),
        ("playbooks", "Inspect skill candidates and promote, verify, list, or deprecate owner-gated Playbooks."),
        ("playbook", "Inspect skill candidates and promote, verify, list, or deprecate owner-gated Playbooks."),
        ("skills", "Stage 4 Executable Skills: candidates, promote, list, verify, execute in sandbox, kill-switch & audit."),
        ("plan", "Assemble a grounded, citation-verified Executive Plan from Playbooks and approved Atoms."),
        ("notes", "Zero-friction notes: add, search, list, tasks, reindex, verify an excerpt."),
        ("capture", "Quickly capture a working note or quarantined inbox item from the CLI."),
        ("import", "Bulk-import Markdown, Obsidian, PDF, DOCX, EPUB, HTML, or CSV files into notes."),
        ("chat", "Chat over memory and notes. Default mode is direct; also instant, verified, stream, and research."),
        ("agent", "Run, start, stop, or inspect the autonomous agent runtime."),
        ("connect", "One-click connect Claude Code/Desktop, Cursor, Windsurf, VS Code, or Antigravity."),
        ("benchmark", "Run the corpus-scale benchmark on a synthetic isolated vault."),
        ("sota-benchmark", "Run comparative SOTA benchmarks against external RAG/memory baselines."),
        ("ledger", "Cryptographic Vault Merkle proof and tamper-evident audit ledger."),
        ("graph", "Contradiction & Drift Radar + temporal knowledge graph."),
        ("snapshot", "Time-travel vault snapshots, semantic diff, and point-in-time recall."),
        ("sync", "Zero-Knowledge E2EE multi-device vault sync (init/join/status/push/pull/verify/conflicts)."),
        ("mobile", "Full On-Device Mobile Vault Client (iOS / Android): pair, list, revoke, or export offline app bundle."),
        ("media", "Local Creative Media Studio: hardware-gated offline image (.png/.webp) & video (.mp4/.gif) generation."),
        ("release", "Release signature verification, store credential checks, WinGet validation & registry publishing."),
        ("models", "Download, SHA-256 verify, inspect, or remove local ONNX Cross-Encoder & NLI weights."),
        ("clipper", "Pair, manage, and capture via the local-first Browser Web Clipper."),
        ("vault", "Manage isolated Multi-Vault Workspaces and opt-in cross-vault federated recall."),
        ("backup", "Create, cryptographically verify, or restore portable Three-Store .sbvault backups."),
        ("hook", "Install/manage git post-commit hooks and distill commits into Second Brain."),
        ("recall", "Search owner-approved memory atoms via lexical, dense, or hybrid RRF recall."),
        ("answer", "Generate a citation-validated grounded answer (standard, preference, or research)."),
        ("review", "Inspect, approve, reject, quarantine, demote, edit, or merge memory atoms."),
        ("audit", "Audit canonical memory atoms against anchored source files."),
        ("rebuild-lexical", "Rebuild the SQLite FTS5 lexical recall sidecar index."),
        ("rebuild-vectors", "Rebuild the Chroma dense vector index and active generation manifest."),
        ("refinery", "Extract candidate atomic notes from raw captures via local Ollama."),
        ("editor", "Synthesize thematic threads and weekly briefings via local Ollama."),
        ("scout", "Fetch allowlisted reading-list sources into 0-raw/inbox/."),
        ("ingest", "Ingest local PDF, DOCX, EPUB, HTML, CSV, audio, or image files into 0-raw/."),
        ("daemon", "Run the continuous local vault watchdog daemon."),
        ("mcp", "Run or configure the loopback stdio MCP server."),
        ("eval-retrieval", "Run retrieval quality evaluation over labeled suites."),
        ("eval-answers", "Run grounded answer policy evaluation suites."),
    ):
        p = subparsers.add_parser(name, help=help_text, add_help=False)
        p.add_argument("sub_argv", nargs=argparse.REMAINDER)

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] in _PASSTHROUGH_COMMANDS:
        # Forward sub-command arguments verbatim: argparse.REMAINDER reorders leading
        # optionals (e.g. `plan --goal X`), so passthrough commands bypass the top-level parser.
        forwarded = raw_argv[1:]
        if forwarded[:1] == ["--"]:
            forwarded = forwarded[1:]
        return _dispatch_passthrough(raw_argv[0], forwarded)

    args, remainder = parser.parse_known_args(raw_argv)
    if args.command == "doctor":
        if remainder:
            parser.error(f"unrecognized arguments: {' '.join(remainder)}")
        return doctor()
    if args.command == "dashboard":
        if remainder:
            parser.error(f"unrecognized arguments: {' '.join(remainder)}")
        if not (1 <= args.port <= 65535):
            parser.error("--port must be between 1 and 65535.")
        return launch_dashboard(port=args.port)
    if args.command == "install":
        if remainder:
            parser.error(f"unrecognized arguments: {' '.join(remainder)}")
        return install(args)
    if args.command == "profile":
        if remainder:
            parser.error(f"unrecognized arguments: {' '.join(remainder)}")
        from scripts.hardware_profile import main as profile_main

        return profile_main(["--json"] if args.json else [])
    if args.command == "serve":
        if remainder:
            parser.error(f"unrecognized arguments: {' '.join(remainder)}")
        from scripts.memory_api import main as api_main

        api_argv = ["--host", args.host, "--port", str(args.port)]
        if args.generate_token:
            api_argv.append("--generate-token")
        if getattr(args, "ensure_tokens", False):
            api_argv.append("--ensure-tokens")
        return api_main(api_argv)
    if args.command == "adapter-config":
        if remainder:
            parser.error(f"unrecognized arguments: {' '.join(remainder)}")
        from scripts import memory_api as api_mod

        if args.port is not None:
            resolved_port = int(args.port)
        else:
            discover_fn = getattr(api_mod, "discover_active_api_port", None)
            if callable(discover_fn):
                resolved_port = int(discover_fn(8765))
            else:
                resolved_port = _discover_api_port_fallback(8765)
        return api_mod.main(["--port", str(resolved_port), "--print-adapter-config"])
    if args.command in _PASSTHROUGH_COMMANDS:
        forwarded = list(getattr(args, "sub_argv", []) or []) + list(remainder)
        if forwarded[:1] == ["--"]:
            forwarded = forwarded[1:]
        return _dispatch_passthrough(args.command, forwarded)
    return 2


_PASSTHROUGH_COMMANDS = frozenset({
    "api",
    "inbox", "hosts", "cognitive", "playbooks", "playbook", "skills", "plan",
    "notes", "capture", "import", "chat", "agent", "connect", "benchmark",
    "sota-benchmark", "ledger", "graph", "snapshot", "sync", "mobile", "media", "release",
    "models", "clipper", "vault",
    "backup", "hook",
    "recall", "answer", "review", "audit",
    "rebuild-lexical", "rebuild-vectors",
    "refinery", "editor", "scout", "ingest", "daemon", "mcp",
    "eval-retrieval", "eval-answers",
})


def _invoke_entrypoint(fn, forwarded: list[str], *, prog: str, description: str) -> int:
    sig = inspect.signature(fn)
    accepts_args = any(
        p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
        for p in sig.parameters.values()
    )
    if accepts_args:
        res = fn(forwarded)
        return int(res) if isinstance(res, int) else 0
    sub_parser = argparse.ArgumentParser(prog=prog, description=description)
    sub_parser.parse_args(forwarded)
    res = fn()
    return int(res) if isinstance(res, int) else 0


def _run_recall_cli(forwarded: list[str]) -> int:
    from scripts import recall as recall_mod

    mod_main = getattr(recall_mod, "main", None)
    if callable(mod_main):
        return _invoke_entrypoint(mod_main, forwarded, prog="second-brain recall", description="Search approved vault memory.")

    p = argparse.ArgumentParser(prog="second-brain recall", description="Search owner-approved memory atoms.")
    p.add_argument("query", help="Natural-language or keyword recall query.")
    p.add_argument("--retrieval-mode", "--mode", dest="retrieval_mode", choices=("auto", "lexical", "dense", "rrf"), default="auto")
    p.add_argument("-n", "--limit", type=int, default=5, help="Max direct results (1..25).")
    p.add_argument("--project", default=None, help="Optional project:<slug> scope filter.")
    p.add_argument("--allow-cross-project", action="store_true")
    p.add_argument("--no-follow-links", action="store_true")
    p.add_argument("--include-duplicates", action="store_true")
    p.add_argument("--include-expired", action="store_true", help="Include expired or not-yet-valid temporal atoms.")
    p.add_argument("--as-of", default=None, help="Point-in-time ISO-8601 date/timestamp validity filter (YYYY-MM-DD).")
    p.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    args = p.parse_args(forwarded)

    vault_dir, atoms_dir, lexical_index_path = _resolve_active_vault_paths()
    chroma_path = os.path.join(vault_dir, ".chroma")
    projects = (args.project.strip(),) if args.project and args.project.strip() else ()
    opts = recall_mod.RecallOptions(
        n_results=max(1, min(int(args.limit), 25)),
        follow_links=not args.no_follow_links,
        include_statuses={"accepted"},
        projects=projects,
        allow_cross_project=bool(args.allow_cross_project),
        retrieval_mode=args.retrieval_mode,
        include_expired=bool(args.include_expired),
        as_of=args.as_of.strip() if args.as_of and args.as_of.strip() else None,
    )
    direct, neighbors = recall_mod.search_memory(
        args.query,
        opts,
        atoms_dir=atoms_dir,
        index_path=lexical_index_path,
        chroma_path=chroma_path,
    )
    duplicates = (
        recall_mod.suggest_duplicate_atoms(
            atoms_dir,
            require_current_approval=True,
            projects=projects,
            brain_dir=vault_dir,
        )
        if args.include_duplicates
        else []
    )
    payload = {
        "results": [r.as_dict() for r in direct],
        "linked_neighbors": [r.as_dict() for r in neighbors],
        "duplicate_suggestions": duplicates,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for item in direct:
            print(f"[{item.score:.3f}] {item.filename} ({item.memory_type})")
        if not direct:
            print("No approved memories matched query.")
    return 0


def _run_answer_cli(forwarded: list[str]) -> int:
    from scripts import answer_policy as ans_mod

    mod_main = getattr(ans_mod, "main", None)
    if callable(mod_main):
        return _invoke_entrypoint(mod_main, forwarded, prog="second-brain answer", description="Generate a grounded answer.")

    from scripts.host_adapters import generate_grounded_answer_via_host
    from scripts.recall import RecallOptions, search_memory

    p = argparse.ArgumentParser(prog="second-brain answer", description="Generate a citation-validated grounded answer.")
    p.add_argument("query", help="Question to answer from approved memory.")
    p.add_argument("--mode", choices=("standard", "preference", "research"), default="standard")
    p.add_argument("--retrieval-mode", choices=("auto", "lexical", "dense", "rrf"), default="auto")
    p.add_argument("-n", "--limit", type=int, default=5)
    p.add_argument("--project", default=None)
    p.add_argument("--allow-cross-project", action="store_true")
    p.add_argument("--include-expired", action="store_true", help="Include temporally expired atoms during recall.")
    p.add_argument("--as-of", default=None, help="Optional ISO-8601 date/timestamp point-in-time validity filter.")
    p.add_argument("--host-id", default="ollama_local")
    p.add_argument("--model-override", default=None)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(forwarded)

    vault_dir, atoms_dir, lexical_index_path = _resolve_active_vault_paths()
    chroma_path = os.path.join(vault_dir, ".chroma")
    n_res = max(1, min(int(args.limit), 15))
    projects = (args.project.strip(),) if args.project and args.project.strip() else ()
    as_of_val = str(args.as_of).strip() if args.as_of and str(args.as_of).strip() else None
    direct, neighbors = search_memory(
        args.query,
        RecallOptions(
            n_results=n_res,
            follow_links=True,
            include_statuses={"accepted"},
            projects=projects,
            allow_cross_project=bool(args.allow_cross_project),
            retrieval_mode=args.retrieval_mode,
            include_expired=bool(args.include_expired),
            as_of=as_of_val,
        ),
        atoms_dir=atoms_dir,
        index_path=lexical_index_path,
        chroma_path=chroma_path,
    )
    ans = generate_grounded_answer_via_host(
        ans_mod.AnswerRequest(query=args.query, mode=args.mode, project_scope=args.project, result_limit=n_res),
        list(direct) + list(neighbors),
        host_id=args.host_id,
        model_override=args.model_override,
        atoms_dir=atoms_dir,
        brain_dir=vault_dir,
    )
    if args.json:
        from dataclasses import asdict

        payload: dict[str, object] = {
            "answerable": ans.answerable,
            "mode": args.mode,
            "text": ans.text,
            "reason": ans.reason,
            "claims": [
                {"text": c.text, "is_preference": c.is_preference, "evidence": [asdict(ev) for ev in c.evidence]}
                for c in ans.claims
            ],
        }
        if args.mode == "research":
            payload["unverified_leads"] = [lead.as_dict() for lead in ans.unverified_leads]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(ans.text)
    return 0


def _dispatch_passthrough(command: str, forwarded: list[str]) -> int:
    if command == "api":
        from scripts.memory_api import main as api_main

        return api_main(forwarded)
    if command == "inbox":
        from scripts.inbox_workflow import main as inbox_main

        return inbox_main(forwarded)
    if command == "hosts":
        from scripts.host_adapters import main as hosts_main

        return hosts_main(forwarded)
    if command == "cognitive":
        from scripts.cognitive_core import main as cognitive_main

        return cognitive_main(forwarded)
    if command in ("playbooks", "playbook"):
        from scripts.skill_playbooks import main as playbooks_main

        return playbooks_main(forwarded)
    if command == "plan":
        from scripts.executive_planner import main as plan_main

        return plan_main(forwarded)
    if command == "notes":
        from scripts.notes_store import main as notes_main

        return notes_main(forwarded)
    if command == "capture":
        p = argparse.ArgumentParser(prog="second-brain capture", description="Quickly capture a working note.")
        p.add_argument("text", nargs="*", help="Note body text (or pass --stdin).")
        p.add_argument("--title", default="", help="Optional note title.")
        p.add_argument("--tag", "--tags", dest="tags", action="append", default=[], help="Optional tag or comma-separated tags (repeatable).")
        p.add_argument("--stdin", action="store_true", help="Read note body from stdin.")
        p.add_argument("--json", action="store_true", help="Emit JSON output.")
        args = p.parse_args(forwarded)
        body_text = sys.stdin.read() if args.stdin else " ".join(args.text)
        if not body_text.strip():
            p.error("Provide capture text or --stdin.")
        parsed_tags = [
            part.strip()
            for raw in (args.tags or [])
            for part in str(raw).split(",")
            if part.strip()
        ]
        vault_dir, _, _ = _resolve_active_vault_paths()
        from scripts.notes_store import NotePaths, create_note

        created = create_note(
            args.title,
            body_text,
            tags=parsed_tags,
            paths=NotePaths.for_brain(vault_dir),
        )
        if args.json:
            print(json.dumps(created, indent=2, ensure_ascii=False))
        else:
            print(f"Captured note: {created.get('filename')} ({created.get('status')})")
        return 0
    if command == "import":
        from scripts.bulk_import import main as import_main

        return import_main(forwarded)
    if command == "chat":
        from scripts.streaming_chat import main as chat_main

        return chat_main(forwarded)
    if command == "agent":
        from scripts.agent_runtime import main as agent_main

        return agent_main(forwarded)
    if command == "connect":
        from scripts.ide_connector import main as connect_main

        return connect_main(forwarded)
    if command == "benchmark":
        from scripts.scale_benchmark import main as bench_main

        return bench_main(forwarded)
    if command == "sota-benchmark":
        from scripts.sota_benchmark import main as sota_main

        return sota_main(forwarded)
    if command == "ledger":
        from scripts.vault_ledger import main as ledger_main

        return ledger_main(forwarded)
    if command == "graph":
        from scripts.knowledge_graph import main as graph_main

        return graph_main(forwarded)
    if command == "snapshot":
        from scripts.time_travel import main as snap_main

        return snap_main(forwarded)
    if command == "sync":
        from scripts.vault_sync import main as sync_main

        return sync_main(forwarded)
    if command == "models":
        from scripts.onnx_models import main as models_main

        return models_main(forwarded)
    if command == "clipper":
        from scripts.web_clipper import main as clipper_main

        return clipper_main(forwarded)
    if command == "vault":
        from scripts.vault_workspaces import main as vault_main

        return vault_main(forwarded)
    if command == "backup":
        from scripts.vault_backup import main as backup_main

        return backup_main(forwarded)
    if command == "hook":
        from scripts.git_distiller import main as hook_main

        return hook_main(forwarded)
    if command == "skills":
        from scripts.executable_skills import main as skills_main

        return skills_main(forwarded)
    if command == "mobile":
        from scripts.mobile_vault_client import main as mobile_main

        return mobile_main(forwarded)
    if command == "media":
        from scripts.creative_media import main as media_main

        return media_main(forwarded)
    if command == "release":
        if forwarded and forwarded[0] == "verify-signatures":
            from scripts.verify_release_signatures import main as sig_main

            return sig_main(forwarded[1:])
        from scripts.publish_registries import main as pub_main

        return pub_main(forwarded)
    if command == "recall":
        return _run_recall_cli(forwarded)
    if command == "answer":
        return _run_answer_cli(forwarded)
    if command == "review":
        from scripts.review_atom import main as review_main

        return _invoke_entrypoint(review_main, forwarded, prog="second-brain review", description="Review and mutate canonical atoms.")
    if command == "audit":
        from scripts.audit_sources import main as audit_main

        return _invoke_entrypoint(audit_main, forwarded, prog="second-brain audit", description="Audit source anchors across atoms.")
    if command == "rebuild-lexical":
        from scripts.rebuild_lexical_index import main as rebuild_lex_main

        return _invoke_entrypoint(rebuild_lex_main, forwarded, prog="second-brain rebuild-lexical", description="Rebuild SQLite FTS5 lexical index.")
    if command == "rebuild-vectors":
        from scripts.rebuild_vectors import main as rebuild_vec_main

        return _invoke_entrypoint(rebuild_vec_main, forwarded, prog="second-brain rebuild-vectors", description="Rebuild Chroma dense vector index.")
    if command == "refinery":
        from scripts import ollama_refinery as ref_mod

        fn = getattr(ref_mod, "main", None) or ref_mod.process_raw_files
        return _invoke_entrypoint(fn, forwarded, prog="second-brain refinery", description="Extract candidate atoms from 0-raw/.")
    if command == "editor":
        from scripts import ollama_editor as ed_mod

        fn = getattr(ed_mod, "main", None) or ed_mod.run_editor
        return _invoke_entrypoint(fn, forwarded, prog="second-brain editor", description="Synthesize threads and weekly briefings.")
    if command == "scout":
        from scripts import ollama_scout as scout_mod

        fn = getattr(scout_mod, "main", None) or scout_mod.run_scout
        return _invoke_entrypoint(fn, forwarded, prog="second-brain scout", description="Fetch allowlisted reading-list sources.")
    if command == "ingest":
        from scripts import ingest_multimodal as ingest_mod

        mod_main = getattr(ingest_mod, "main", None)
        if callable(mod_main):
            return _invoke_entrypoint(mod_main, forwarded, prog="second-brain ingest", description="Ingest multimodal files into 0-raw/.")
        p = argparse.ArgumentParser(prog="second-brain ingest", description="Ingest multimodal audio/image files into 0-raw/.")
        p.add_argument("--file", help="Path to a single audio or image file to ingest.")
        args = p.parse_args(forwarded)
        ensure_dirs()
        if args.file:
            ingest_mod.process_file(args.file)
        else:
            ingest_mod.scan_raw_dir()
        return 0
    if command == "daemon":
        from scripts import watchdog_daemon as daemon_mod

        fn = getattr(daemon_mod, "main", None) or daemon_mod.run_daemon
        return _invoke_entrypoint(fn, forwarded, prog="second-brain daemon", description="Run continuous vault watchdog daemon.")
    if command == "mcp":
        from scripts import mcp_server as mcp_mod

        mod_main = getattr(mcp_mod, "main", None)
        if callable(mod_main) and not inspect.iscoroutinefunction(mod_main):
            return _invoke_entrypoint(mod_main, forwarded, prog="second-brain mcp", description="Run loopback stdio MCP server.")
        p = argparse.ArgumentParser(prog="second-brain mcp", description="Run or configure the Second Brain local MCP server.")
        p.add_argument("--generate-token", action="store_true", help="Generate and store a random 256-bit MCP bearer token.")
        p.add_argument("--print-token", action="store_true", help="Print the configured file-stored MCP bearer token.")
        args = p.parse_args(forwarded)
        if args.generate_token:
            token = mcp_mod.generate_bearer_token()
            saved_path = mcp_mod.save_bearer_token(token)
            print(f"Wrote MCP bearer token to {saved_path}")
            if args.print_token:
                print(f"BRAIN_MCP_BEARER_TOKEN={token}")
            return 0
        if args.print_token:
            try:
                token = mcp_mod.get_configured_bearer_token()
            except PermissionError as exc:
                print(f"No valid MCP bearer token is configured ({exc}). Run with --generate-token.", file=sys.stderr)
                return 1
            if not token:
                print("No valid MCP bearer token is configured. Run with --generate-token.", file=sys.stderr)
                return 1
            print(token)
            return 0
        asyncio.run(mod_main())
        return 0
    if command == "eval-retrieval":
        from scripts.evaluate_retrieval import main as eval_ret_main

        return _invoke_entrypoint(eval_ret_main, forwarded, prog="second-brain eval-retrieval", description="Evaluate retrieval quality.")
    if command == "eval-answers":
        from scripts.evaluate_answers import main as eval_ans_main

        return _invoke_entrypoint(eval_ans_main, forwarded, prog="second-brain eval-answers", description="Evaluate grounded answer policy.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
