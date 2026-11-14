"""Stable bootstrap commands for a Second Brain installation."""

from __future__ import annotations

import argparse
import importlib.util
import platform
import sys
from pathlib import Path
import os
import subprocess
import json
import urllib.error
import urllib.request

# Existing internal modules use direct imports when executed from the scripts
# directory. Preserve that import contract inside the installed wheel.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


from scripts import config as _config
from scripts.config import _default_installed_vault

if not os.environ.get("BRAIN_VAULT_DIR"):
    installed_vault = _default_installed_vault()
    if installed_vault:
        os.environ["BRAIN_VAULT_DIR"] = installed_vault

BRAIN_DIR = _config.BRAIN_DIR
ensure_dirs = _config.ensure_dirs


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

    print(f"Second Brain {platform.python_version()} on {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"Vault: {BRAIN_DIR}")
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
        "scout_http": _capability("httpx"),
        "extractor": _capability("trafilatura"),
        "multimodal_audio": _capability("faster_whisper"),
        "image_processing_library": _capability("PIL"),
    }
    for name, ready in capabilities.items():
        print(f"{'ready' if ready else 'not installed':16} {name}")
    atoms_dir = Path(_config.ATOMS_DIR)
    atom_files = sorted(path for path in atoms_dir.glob("*.md") if path.name != "README.md") if atoms_dir.is_dir() else []
    print(f"Atoms: {len(atom_files)}")
    playbooks_dir = Path(_config.PLAYBOOKS_DIR)
    playbook_files = (
        sorted(path for path in playbooks_dir.glob("*.md") if path.name != "README.md")
        if playbooks_dir.is_dir()
        else []
    )
    print(f"Playbooks: {len(playbook_files)}")
    try:
        from scripts.review_atom import approved_revision_is_current

        approved = sum(1 for path in atom_files if approved_revision_is_current(str(path), brain_dir=_config.BRAIN_DIR))
        print(f"Current owner-approved atoms: {approved}")
    except OSError:
        print("Current owner-approved atoms: could not verify")
    from scripts.lexical_store import lexical_index_is_current

    lexical_ready = lexical_index_is_current(_config.ATOMS_DIR, _config.LEXICAL_INDEX_PATH)
    print(f"Lexical index: {'current' if lexical_ready else 'missing or stale'}")
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
        model_roots = {name.split(":", 1)[0] for name in models}
        for label, configured in (("Generation", _config.GEN_MODEL), ("Embedding", _config.EMBED_MODEL), ("Vision", _config.VISION_MODEL)):
            present = configured in models or configured.split(":", 1)[0] in model_roots
            print(f"{label} model {configured}: {'available' if present else 'not installed'}")
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        print("Ollama service: unavailable; configured generation and embedding models cannot be used")
        print("Offline lexical recall remains usable when its index is current")
    try:
        from scripts.index_manifest import index_manifest_is_current, load_index_manifest

        manifest = load_index_manifest(_config.CHROMA_PATH)
        backend_note = ""
        json_fallback_file = json_collection_path(_config.CHROMA_PATH, "atoms")
        if os.path.isdir(_config.CHROMA_PATH) and (manifest is not None or os.path.exists(json_fallback_file)):
            collection = get_vector_collection(_config.CHROMA_PATH, name="atoms", create=False)
            try:
                if isinstance(collection, JsonVectorCollection):
                    reason = collection.fallback_reason or "chromadb unavailable"
                    backend_note = f" [degraded JSON backend: {reason}]"
            finally:
                close_vector_collection(collection)
        if manifest and index_manifest_is_current(manifest, _config.ATOMS_DIR):
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
    return 0


def install(args: argparse.Namespace) -> int:
    ensure_dirs()
    print(f"Created/verified vault at {BRAIN_DIR}")
    from scripts.lexical_store import rebuild_lexical_index
    from scripts.review_atom import review_lock

    lock_path = _config.os.path.join(_config.os.path.dirname(_config.LEXICAL_INDEX_PATH), "writer.lock")
    try:
        with review_lock(lock_path):
            stats = rebuild_lexical_index(_config.ATOMS_DIR, _config.LEXICAL_INDEX_PATH)
    except Exception as exc:
        print(f"Lexical index rebuild failed: {exc}", file=sys.stderr)
        return 1
    print(f"Lexical recall initialized with {stats['indexed_count']} atom(s).")
    print("Model-free local setup is ready. Inspect installed capabilities with `second-brain doctor`.")
    if _capability("streamlit"):
        print("Start the local dashboard with `second-brain dashboard`.")
    else:
        print("Install runtime dependencies with `uv sync --frozen`.")
    if not _capability("ollama"):
        print("Ollama Python client is not installed. Run `uv sync --frozen`; an Ollama daemon and model are separate.")
    return 0


def launch_dashboard() -> int:
    if not _capability("streamlit"):
        print("Dashboard dependencies are not installed. Run `uv sync --frozen` first.", file=sys.stderr)
        return 2
    from scripts.dashboard_auth import configure_password_interactively, load_password_record

    if load_password_record() is None and not configure_password_interactively():
        return 1
    app_path = Path(__file__).resolve().parent.parent / "dashboard" / "app.py"
    if not app_path.is_file():
        print("Dashboard files are missing from this installation.", file=sys.stderr)
        return 2
    return subprocess.call([
        sys.executable, "-m", "streamlit", "run", str(app_path),
        "--server.address=127.0.0.1",
        "--server.port=8501",
        "--server.enableCORS=true",
        "--server.enableXsrfProtection=true",
        "--server.xsrfCookieSameSite=strict",
        "--server.allowedHosts=127.0.0.1",
        "--server.corsAllowedOrigins=http://127.0.0.1:8501",
        "--browser.serverAddress=127.0.0.1",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="second-brain", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="Report local runtime capabilities.")
    subparsers.add_parser("dashboard", help="Start the local password-protected dashboard.")
    install_parser = subparsers.add_parser("install", help="Initialize the vault and model-free lexical index.")
    install_parser.add_argument("--no-index", action="store_true", help="Only create the vault folders.")

    profile_parser = subparsers.add_parser("profile", help="Inspect host hardware tier and model recommendations.")
    profile_parser.add_argument("--json", action="store_true", help="Output structured JSON hardware profile.")

    serve_parser = subparsers.add_parser("serve", help="Start the loopback 127.0.0.1 HTTP JSON Memory API.")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Loopback bind host (must be 127.0.0.1).")
    serve_parser.add_argument("--port", type=int, default=8765, help="Loopback TCP port (default 8765).")
    serve_parser.add_argument("--generate-token", action="store_true", help="Generate a 256-bit API bearer token and exit.")

    adapter_parser = subparsers.add_parser("adapter-config", help="Print IDE and custom host adapter configuration JSON.")
    adapter_parser.add_argument("--port", type=int, default=8765, help="Loopback API TCP port (default 8765).")

    inbox_parser = subparsers.add_parser(
        "inbox",
        help="Inspect, promote, or reject quarantined 0-raw/inbox captures.",
        add_help=False,
    )
    inbox_parser.add_argument("sub_argv", nargs=argparse.REMAINDER)

    hosts_parser = subparsers.add_parser(
        "hosts",
        help="Inspect and configure per-host AI sharing consent policies.",
        add_help=False,
    )
    hosts_parser.add_argument("sub_argv", nargs=argparse.REMAINDER)

    cognitive_parser = subparsers.add_parser(
        "cognitive",
        help="Generate memory fitness reports, record feedback, and log experiences.",
        add_help=False,
    )
    cognitive_parser.add_argument("sub_argv", nargs=argparse.REMAINDER)

    playbooks_parser = subparsers.add_parser(
        "playbooks",
        help="Inspect skill candidates and promote, verify, list, or deprecate owner-gated Playbooks.",
        add_help=False,
    )
    playbooks_parser.add_argument("sub_argv", nargs=argparse.REMAINDER)

    plan_parser = subparsers.add_parser(
        "plan",
        help="Assemble a grounded, citation-verified Executive Plan from Playbooks and approved Atoms.",
        add_help=False,
    )
    plan_parser.add_argument("sub_argv", nargs=argparse.REMAINDER)

    for name, help_text in (
        ("notes", "Zero-friction notes: add, search, list, reindex, verify an excerpt."),
        ("import", "Bulk-import an Obsidian vault or Markdown folder into notes."),
        ("chat", "Chat with streaming answers over verified memory and notes."),
        ("connect", "One-click connect Claude Code/Desktop, Cursor, Windsurf, VS Code, or Antigravity."),
        ("benchmark", "Run the corpus-scale benchmark on a synthetic isolated vault."),
        ("sota-benchmark", "Run comparative SOTA benchmarks against external RAG/memory baselines."),
        ("ledger", "Cryptographic Vault Merkle proof and tamper-evident audit ledger."),
        ("graph", "Contradiction & Drift Radar + temporal knowledge graph."),
        ("snapshot", "Time-travel vault snapshots, semantic diff, and point-in-time recall."),
    ):
        p = subparsers.add_parser(name, help=help_text, add_help=False)
        p.add_argument("sub_argv", nargs=argparse.REMAINDER)
    serve_parser.add_argument("--ensure-tokens", action="store_true", help="Create agent and owner tokens if missing.")

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
        return launch_dashboard()
    if args.command == "install":
        if remainder:
            parser.error(f"unrecognized arguments: {' '.join(remainder)}")
        if args.no_index:
            ensure_dirs()
            print(f"Created/verified vault at {BRAIN_DIR}")
            return 0
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
        from scripts.memory_api import main as api_main

        return api_main(["--port", str(args.port), "--print-adapter-config"])
    if args.command in _PASSTHROUGH_COMMANDS:
        forwarded = list(getattr(args, "sub_argv", []) or []) + list(remainder)
        if forwarded[:1] == ["--"]:
            forwarded = forwarded[1:]
        return _dispatch_passthrough(args.command, forwarded)
    return 2


_PASSTHROUGH_COMMANDS = frozenset({
    "inbox", "hosts", "cognitive", "playbooks", "plan",
    "notes", "import", "chat", "connect", "benchmark",
    "sota-benchmark", "ledger", "graph", "snapshot",
})


def _dispatch_passthrough(command: str, forwarded: list[str]) -> int:
    if command == "inbox":
        from scripts.inbox_workflow import main as inbox_main

        return inbox_main(forwarded)
    if command == "hosts":
        from scripts.host_adapters import main as hosts_main

        return hosts_main(forwarded)
    if command == "cognitive":
        from scripts.cognitive_core import main as cognitive_main

        return cognitive_main(forwarded)
    if command == "playbooks":
        from scripts.skill_playbooks import main as playbooks_main

        return playbooks_main(forwarded)
    if command == "plan":
        from scripts.executive_planner import main as plan_main

        return plan_main(forwarded)
    if command == "notes":
        from scripts.notes_store import main as notes_main

        return notes_main(forwarded)
    if command == "import":
        from scripts.bulk_import import main as import_main

        return import_main(forwarded)
    if command == "chat":
        from scripts.streaming_chat import main as chat_main

        return chat_main(forwarded)
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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
