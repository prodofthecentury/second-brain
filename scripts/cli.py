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


def _default_installed_vault() -> str | None:
    """Keep wheel-installed user data outside the immutable Python package."""
    package_root = Path(__file__).resolve().parent.parent
    if (package_root / "pyproject.toml").exists():
        return None  # editable checkout: preserve the repository's brain/ path
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return str(Path(base) / "SecondBrain" / "brain")
    if sys.platform == "darwin":
        return str(Path.home() / "Library" / "Application Support" / "SecondBrain" / "brain")
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return str(Path(base) / "second-brain" / "brain")


if not os.environ.get("BRAIN_VAULT_DIR"):
    installed_vault = _default_installed_vault()
    if installed_vault:
        os.environ["BRAIN_VAULT_DIR"] = installed_vault

from scripts import config as _config

BRAIN_DIR = _config.BRAIN_DIR
ensure_dirs = _config.ensure_dirs


def _capability(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


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
        raw_host = (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").strip().rstrip("/")
        ollama_base = raw_host if raw_host.startswith(("http://", "https://")) else f"http://{raw_host}"
        with urllib.request.urlopen(f"{ollama_base}/api/tags", timeout=5.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
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
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return doctor()
    if args.command == "dashboard":
        return launch_dashboard()
    if args.command == "install":
        if args.no_index:
            ensure_dirs()
            print(f"Created/verified vault at {BRAIN_DIR}")
            return 0
        return install(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
