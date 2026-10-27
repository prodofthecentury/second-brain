# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""
Second Brain health check.

Run:
    uv run scripts/doctor.py
"""

import json
import os
import shutil
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
from vector_store import JsonVectorCollection, get_vector_collection


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
    if not os.path.exists(directory):
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


def run_checks() -> list[tuple[str, str, str]]:
    checks = []

    if shutil.which("uv"):
        checks.append(ok("uv", "uv is available on PATH"))
    else:
        checks.append(fail("uv", "uv is not available on PATH"))

    required_dirs = [RAW_DIR, ATOMS_DIR, THREADS_DIR, SOURCES_DIR, BRIEFINGS_DIR]
    missing = [path for path in required_dirs if not os.path.isdir(path)]
    if missing:
        checks.append(fail("vault dirs", f"Missing directories: {', '.join(missing)}"))
    else:
        checks.append(ok("vault dirs", "All vault directories exist"))

    index_manifest = load_index_manifest(CHROMA_PATH)
    if not os.path.isdir(CHROMA_PATH):
        checks.append(warn("vector store", "No dense index is installed; model-free lexical recall remains available"))
    else:
        vector_collection = get_vector_collection(CHROMA_PATH, name="atoms", create=False)
        if isinstance(vector_collection, JsonVectorCollection):
            reason = f" ({vector_collection.fallback_reason})" if vector_collection.fallback_reason else ""
            if not index_manifest or not index_manifest_is_current(index_manifest, ATOMS_DIR):
                checks.append(warn("vector store", f"JSON fallback files are present but no current generation is served{reason}"))
            else:
                checks.append(warn("vector store", f"Validated dense generation uses the JSON fallback{reason}"))
        else:
            checks.append(ok("vector store", "ChromaDB persistent client initialized"))

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

    models_payload = fetch_json("http://localhost:11434/api/tags")
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
    checks.append(ok("raw queue", f"{raw_count} queued capture(s)"))
    checks.append(ok("reading list", f"{queued_reading_items()} queued URL(s)"))

    dashboard = fetch_text("http://localhost:8501/_stcore/health", timeout=2.0)
    if dashboard is None:
        checks.append(warn("dashboard", "Streamlit dashboard is not reachable on localhost:8501"))
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
