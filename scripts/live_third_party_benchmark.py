# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#     "chromadb",
#     "ollama",
#     "rank-bm25",
#     "faiss-cpu",
#     "mem0ai",
#     "onnxruntime",
#     "tokenizers",
#     "numpy",
# ]
# ///
"""Live Head-to-Head Third-Party Memory System Benchmark on the Real Curated Vault.

Evaluates production Second Brain modes directly against live, installed third-party
memory and retrieval engines over the real 78-atom vault (`brain/2-atoms`) and the
90-case human-curated evaluation suite (`docs/retrieval-evaluation-proposed.json`):

  1. second_brain_verified          (FTS5 + ONNX Cross-Encoder + CAS + byte anchors + graph + scope)
  2. second_brain_hybrid_rrf        (FTS5 + ChromaDB 768d RRF + ONNX Cross-Encoder + CAS + byte anchors)
  3. mem0_live_v2                   (Real `mem0ai` v2.x `Memory.from_config` + Ollama 768d + Chroma)
  4. faiss_hnsw_live                (Real `faiss-cpu` C++ `IndexHNSWFlat` cosine over Ollama 768d)
  5. chromadb_hnsw_live             (Real `chromadb` `PersistentClient` HNSW cosine without trust gate)
  6. rank_bm25_okapi_live           (Real `rank-bm25` `BM25Okapi` without trust gate)
  7. sqlite_fts5_untrusted_live     (Real SQLite FTS5 BM25 without CAS/approval/scope trust gate)
  8. basic_memory_live              (Optional live `basic-memory` Markdown+SQLite vault adapter)
  9. graphiti_kuzu_live             (Optional live `graphiti-core` + embedded `kuzu` + local Ollama adapter)
 10. zep_live                       (Optional `zep-cloud`/`zep-python` adapter when configured via env)

All third-party stores are built inside an isolated temporary directory so the live
vault (`brain/`) is read-only throughout the benchmark. Telemetry in Mem0 and Chroma
is explicitly disabled before import.

Run:
    uv run --with rank-bm25 --with faiss-cpu --with mem0ai python scripts/live_third_party_benchmark.py \\
        --output docs/live-third-party-benchmark-2026-11-22.json
"""



from __future__ import annotations

if __name__ == "__main__" and not __package__:
    import runpy as _sb_runpy
    import sys as _sb_sys
    from pathlib import Path as _sb_Path

    _sb_repo = _sb_Path(__file__).resolve().parents[1]
    _sb_here = _sb_Path(__file__).resolve().parent
    if _sb_Path(_sb_sys.path[0]).resolve() == _sb_here:
        _sb_sys.path[0] = str(_sb_repo)
    elif str(_sb_repo) not in _sb_sys.path:
        _sb_sys.path.insert(0, str(_sb_repo))
    raise SystemExit(_sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__"))


import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import gc
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import statistics
import sys
import tempfile
import time
from typing import Any

# Explicitly disable third-party telemetry prior to importing Mem0 or ChromaDB.
os.environ["MEM0_TELEMETRY"] = "false"
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY", "False")


from scripts.atom_mutator import approved_revision_is_current  # noqa: E402
from scripts.config import (  # noqa: E402
    ATOMS_DIR,
    CHROMA_PATH,
    EMBED_MODEL,
    GEN_MODEL,
    LEXICAL_INDEX_PATH,
    is_content_markdown,
    resolve_module_atoms_and_brain,
    resolve_module_vault_path,
    snapshot_module_defaults,
)

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())
from scripts.cross_encoder import score_query_atom_pairs  # noqa: E402
from scripts.evaluation_dataset import load_dataset  # noqa: E402
from scripts.hardware_profile import detect_hardware_profile, resolve_active_local_generation_model  # noqa: E402
from scripts.lexical_store import search_lexical_index  # noqa: E402
from scripts.memory_format import split_frontmatter  # noqa: E402
from scripts.memory_review import parse_atom_record, section_text  # noqa: E402
import scripts.ollama_utils as ollama_utils  # noqa: E402
from scripts.recall import (  # noqa: E402
    QUERY_STOP_WORDS,
    RecallOptions,
    project_tags_match,
    search_hybrid_rrf_memory,
    search_lexical_memory,
    search_memory,
)
from scripts.vector_store import _close_chroma_client, close_vector_collection, get_vector_collection  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET_PATH = ROOT / "docs" / "retrieval-evaluation-proposed.json"
TOP_K = 5
_TOKEN_RE = re.compile(r"[a-z0-9_]{3,}")


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return round(ordered[idx], 3)


def _mrr(ranked: list[str], relevant: set[str]) -> float:
    for pos, name in enumerate(ranked[:TOP_K], start=1):
        if name in relevant:
            return 1.0 / pos
    return 0.0


def _ndcg_at_k(ranked: list[str], relevant: set[str], k: int = TOP_K) -> float:
    if not relevant:
        return 0.0
    dcg = sum(
        1.0 / math.log2(pos + 1.0)
        for pos, name in enumerate(ranked[:k], start=1)
        if name in relevant
    )
    ideal_n = min(k, len(relevant))
    idcg = sum(1.0 / math.log2(pos + 1.0) for pos in range(1, ideal_n + 1))
    return (dcg / idcg) if idcg > 0.0 else 0.0


def _tokenize(text: str) -> list[str]:
    return [tok for tok in _TOKEN_RE.findall((text or "").lower()) if tok not in QUERY_STOP_WORDS]


def _load_vault_atoms(atoms_dir: Path) -> tuple[list[dict[str, Any]], set[str]]:
    """Read all vault Markdown atoms and identify which files lack current owner approval."""
    atoms: list[dict[str, Any]] = []
    unapproved_filenames: set[str] = set()
    for p in sorted(atoms_dir.glob("*.md")):
        if not is_content_markdown(p.name) or p.name.startswith("."):
            continue
        raw_txt = p.read_text(encoding="utf-8")
        rec = parse_atom_record(str(p))
        parsed = split_frontmatter(raw_txt)
        body = "\n".join(parsed[1]) if parsed is not None else raw_txt
        claim = section_text(body, "Claim") or body.strip()
        evidence = section_text(body, "Evidence") or ""
        embed_input = f"{claim}\n{evidence}".strip() if evidence else claim
        is_approved = bool(approved_revision_is_current(str(p), atoms_dir=str(atoms_dir))) and (
            rec.get("review_status") == "accepted"
        )
        if not is_approved:
            unapproved_filenames.add(p.name)
        proj_tags = tuple(
            str(t).strip().lower()
            for t in (rec.get("contexts") or [])
            if str(t).strip().lower().startswith("project:")
        )
        atoms.append(
            {
                "filename": p.name,
                "raw_text": raw_txt,
                "claim": claim,
                "embed_input": embed_input,
                "project_tags": proj_tags,
                "review_status": str(rec.get("review_status") or "pending"),
                "is_owner_approved": is_approved,
            }
        )
    return atoms, unapproved_filenames


def _package_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for pkg in (
        "mem0ai",
        "faiss-cpu",
        "rank-bm25",
        "chromadb",
        "onnxruntime",
        "ollama",
        "basic-memory",
        "graphiti-core",
        "kuzu",
        "zep-cloud",
        "zep-python",
    ):
        try:
            versions[pkg] = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            continue
    return versions


def _map_text_to_filename(text: str, atoms_by_claim: list[tuple[str, str]]) -> str | None:
    """Resolve an external memory adapter hit back to a vault atom filename."""
    s = str(text or "")
    marker = "[ATOM:"
    if marker in s:
        after = s.split(marker, 1)[1]
        cand = after.split("]", 1)[0].strip()
        for fname, _claim in atoms_by_claim:
            if cand == fname or cand == Path(fname).stem:
                return fname
    s_low = s.lower()
    for fname, claim in atoms_by_claim:
        stem = Path(fname).stem.lower()
        if fname.lower() in s_low or (len(stem) >= 4 and stem in s_low):
            return fname
        claim_prefix = claim.strip().lower()[:48]
        if len(claim_prefix) >= 12 and claim_prefix in s_low:
            return fname
    return None


class _ExternalMemoryAdapter:
    """Base protocol for optional isolated live third-party benchmark adapters."""

    name: str = "external"
    packages: tuple[str, ...] = ()

    def check_available(self) -> tuple[bool, str]:
        return False, "not implemented"

    def setup(
        self,
        vault_atoms: list[dict[str, Any]],
        tmp_path: Path,
        *,
        active_llm: str,
        base_url: str,
        embed_dim: int,
    ) -> float:
        raise NotImplementedError

    def search(self, query: str, *, project_scope: str = "", top_k: int = TOP_K) -> list[str]:
        raise NotImplementedError

    def close(self) -> None:
        return None


class BasicMemoryLiveAdapter(_ExternalMemoryAdapter):
    """Isolated adapter for `basic-memory` Markdown + local SQLite FTS/entity store."""

    name = "basic_memory_live"
    packages = ("basic-memory",)

    def __init__(self) -> None:
        self._atoms_by_claim: list[tuple[str, str]] = []
        self._engine: Any = None
        self._project_dir: Path | None = None

    def check_available(self) -> tuple[bool, str]:
        try:
            importlib.import_module("basic_memory")
            return True, "available"
        except ImportError as exc:
            return False, f"basic-memory package not installed ({exc}); invoke via `uv run --with basic-memory`."

    def setup(
        self,
        vault_atoms: list[dict[str, Any]],
        tmp_path: Path,
        *,
        active_llm: str,
        base_url: str,
        embed_dim: int,
    ) -> float:
        t0 = time.perf_counter()
        bm = importlib.import_module("basic_memory")
        proj_dir = tmp_path / "basic_memory_vault"
        proj_dir.mkdir(parents=True, exist_ok=True)
        self._project_dir = proj_dir
        self._atoms_by_claim = [(a["filename"], a["claim"]) for a in vault_atoms]

        for atom in vault_atoms:
            scope = ",".join(atom.get("project_tags") or ())
            note_md = (
                f"---\n"
                f"title: {Path(atom['filename']).stem}\n"
                f"tags: [{scope}]\n"
                f"---\n\n"
                f"[ATOM:{atom['filename']}]\n\n"
                f"{atom['raw_text']}\n"
            )
            (proj_dir / atom["filename"]).write_text(note_md, encoding="utf-8")

        engine_cls = getattr(bm, "BasicMemoryIndex", None) or getattr(bm, "MemoryStore", None)
        if engine_cls is not None:
            self._engine = engine_cls(vault_dir=str(proj_dir), db_path=str(tmp_path / "basic_memory.sqlite3"))
            self._engine.index_directory(str(proj_dir))
            return round((time.perf_counter() - t0) * 1000.0, 2)

        # Native basic-memory library surface
        search_fn = getattr(bm, "search_notes", None)
        if callable(search_fn):
            self._engine = bm
            return round((time.perf_counter() - t0) * 1000.0, 2)

        raise RuntimeError("Installed `basic_memory` module does not expose an index/search entry point.")

    def search(self, query: str, *, project_scope: str = "", top_k: int = TOP_K) -> list[str]:
        if self._engine is None:
            return []
        if hasattr(self._engine, "search"):
            hits = self._engine.search(query, project=project_scope or None, limit=top_k)
        else:
            hits = self._engine.search_notes(
                query, vault_dir=str(self._project_dir), project=project_scope or None, limit=top_k
            )
        ranked: list[str] = []
        for h in hits or []:
            fname = None
            if isinstance(h, dict):
                fname = h.get("filename") or _map_text_to_filename(str(h.get("text") or h.get("title") or ""), self._atoms_by_claim)
            else:
                fname = getattr(h, "filename", None) or _map_text_to_filename(str(h), self._atoms_by_claim)
            if fname and fname not in ranked:
                ranked.append(str(fname))
        return ranked[:top_k]


class GraphitiKuzuLiveAdapter(_ExternalMemoryAdapter):
    """Isolated adapter for `graphiti-core` + embedded `kuzu` graph + local Ollama."""

    name = "graphiti_kuzu_live"
    packages = ("graphiti-core", "kuzu")

    def __init__(self) -> None:
        self._client: Any = None
        self._atoms_by_claim: list[tuple[str, str]] = []

    def check_available(self) -> tuple[bool, str]:
        missing: list[str] = []
        for pkg in ("graphiti_core", "kuzu"):
            try:
                importlib.import_module(pkg)
            except ImportError:
                missing.append(pkg)
        if missing:
            return False, f"Missing optional packages ({', '.join(missing)}); invoke via `uv run --with graphiti-core --with kuzu`."
        return True, "available"

    def setup(
        self,
        vault_atoms: list[dict[str, Any]],
        tmp_path: Path,
        *,
        active_llm: str,
        base_url: str,
        embed_dim: int,
    ) -> float:
        t0 = time.perf_counter()
        g_mod = importlib.import_module("graphiti_core")
        self._atoms_by_claim = [(a["filename"], a["claim"]) for a in vault_atoms]
        graph_cls = getattr(g_mod, "Graphiti")
        db_dir = str(tmp_path / "graphiti_kuzu_db")

        # Try lightweight injected or KuzuDriver-backed construction without cloud calls
        try:
            driver_mod = importlib.import_module("graphiti_core.driver.kuzu_driver")
            kuzu_driver = driver_mod.KuzuDriver(db=db_dir)
            llm_mod = importlib.import_module("graphiti_core.llm_client.openai_client")
            emb_mod = importlib.import_module("graphiti_core.embedder.openai")
            openai_base = base_url.rstrip("/") + "/v1"
            llm_client = llm_mod.OpenAIClient(model=active_llm, base_url=openai_base, api_key="ollama-local")
            embedder = emb_mod.OpenAIEmbedder(
                embedding_model=EMBED_MODEL,
                embedding_dim=embed_dim,
                base_url=openai_base,
                api_key="ollama-local",
            )
            self._client = graph_cls(graph_driver=kuzu_driver, llm_client=llm_client, embedder=embedder)
        except (ImportError, AttributeError, TypeError):
            self._client = graph_cls(db_path=db_dir, model=active_llm, embed_model=EMBED_MODEL, base_url=base_url)

        async def _ingest() -> None:
            if hasattr(self._client, "build_indices_and_constraints"):
                res = self._client.build_indices_and_constraints()
                if asyncio.iscoroutine(res):
                    await res
            for atom in vault_atoms:
                grp = atom["project_tags"][0] if atom.get("project_tags") else "global"
                ep_body = f"[ATOM:{atom['filename']}] {atom['claim']}"
                res = self._client.add_episode(
                    name=atom["filename"],
                    episode_body=ep_body,
                    source_description="vault-atom",
                    reference_time=datetime.now(timezone.utc),
                    group_id=grp,
                )
                if asyncio.iscoroutine(res):
                    await res

        asyncio.run(_ingest())
        return round((time.perf_counter() - t0) * 1000.0, 2)

    def search(self, query: str, *, project_scope: str = "", top_k: int = TOP_K) -> list[str]:
        if self._client is None:
            return []

        async def _query() -> Any:
            kwargs: dict[str, Any] = {"num_results": top_k}
            if project_scope:
                kwargs["group_ids"] = [project_scope]
            try:
                res = self._client.search(query, **kwargs)
            except TypeError:
                res = self._client.search(query)
            if asyncio.iscoroutine(res):
                return await res
            return res

        hits = asyncio.run(_query())
        ranked: list[str] = []
        for h in hits or []:
            text = (
                h.get("fact") or h.get("name") or h.get("content") or ""
                if isinstance(h, dict)
                else f"{getattr(h, 'name', '')} {getattr(h, 'fact', '')} {getattr(h, 'content', '')}"
            )
            fname = _map_text_to_filename(text, self._atoms_by_claim)
            if fname and fname not in ranked:
                ranked.append(fname)
        return ranked[:top_k]

    def close(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            try:
                res = self._client.close()
                if asyncio.iscoroutine(res):
                    asyncio.run(res)
            except Exception:
                pass


class ZepLiveAdapter(_ExternalMemoryAdapter):
    """Isolated opt-in adapter for `zep-cloud` / `zep-python` when env credentials exist."""

    name = "zep_live"
    packages = ("zep-cloud", "zep-python")

    def __init__(self) -> None:
        self._client: Any = None
        self._session_id: str = ""
        self._atoms_by_claim: list[tuple[str, str]] = []

    def check_available(self) -> tuple[bool, str]:
        api_key = (os.environ.get("ZEP_API_KEY") or "").strip()
        api_url = (os.environ.get("ZEP_API_URL") or "").strip()
        mod = None
        for pkg in ("zep_cloud", "zep_python"):
            try:
                mod = importlib.import_module(pkg)
                break
            except ImportError:
                continue
        if mod is None:
            return False, "Missing optional `zep-cloud`/`zep-python` package."
        if not api_key and not api_url:
            return False, "Zep server/key not configured (`ZEP_API_KEY` or `ZEP_API_URL` required)."
        return True, "available"

    def setup(
        self,
        vault_atoms: list[dict[str, Any]],
        tmp_path: Path,
        *,
        active_llm: str,
        base_url: str,
        embed_dim: int,
    ) -> float:
        t0 = time.perf_counter()
        self._atoms_by_claim = [(a["filename"], a["claim"]) for a in vault_atoms]
        mod = None
        for pkg in ("zep_cloud", "zep_python"):
            try:
                mod = importlib.import_module(pkg)
                break
            except ImportError:
                continue
        if mod is None:
            raise RuntimeError("Zep client module unavailable.")
        client_cls = getattr(mod, "Zep", None) or getattr(mod, "ZepClient", None)
        if client_cls is None:
            raise RuntimeError("Zep module missing Zep/ZepClient class.")
        api_key = (os.environ.get("ZEP_API_KEY") or "").strip() or None
        api_url = (os.environ.get("ZEP_API_URL") or "").strip() or None
        self._client = client_cls(api_key=api_key, base_url=api_url) if api_url else client_cls(api_key=api_key)
        self._session_id = f"sb_bench_{int(time.time())}"
        if hasattr(self._client, "index_vault"):
            self._client.index_vault(vault_atoms, session_id=self._session_id)
        elif hasattr(self._client, "memory") and hasattr(self._client.memory, "add"):
            msg_cls = getattr(mod, "Message", dict)
            msgs = [
                msg_cls(role="user", role_type="user", content=f"[ATOM:{a['filename']}] {a['claim']}")
                for a in vault_atoms
            ]
            self._client.memory.add(self._session_id, messages=msgs)
        return round((time.perf_counter() - t0) * 1000.0, 2)

    def search(self, query: str, *, project_scope: str = "", top_k: int = TOP_K) -> list[str]:
        if self._client is None:
            return []
        if hasattr(self._client, "search_session"):
            hits = self._client.search_session(self._session_id, query=query, limit=top_k)
        elif hasattr(self._client, "memory") and hasattr(self._client.memory, "search_sessions"):
            hits = self._client.memory.search_sessions(session_ids=[self._session_id], text=query, limit=top_k)
            hits = getattr(hits, "results", hits)
        else:
            return []
        ranked: list[str] = []
        for h in hits or []:
            content = (
                h.get("content") or h.get("fact") or ""
                if isinstance(h, dict)
                else f"{getattr(h, 'fact', '')} {getattr(getattr(h, 'message', None), 'content', '')}"
            )
            fname = _map_text_to_filename(content, self._atoms_by_claim)
            if fname and fname not in ranked:
                ranked.append(fname)
        return ranked[:top_k]


def default_optional_adapters() -> list[_ExternalMemoryAdapter]:
    return [BasicMemoryLiveAdapter(), GraphitiKuzuLiveAdapter(), ZepLiveAdapter()]


def build_ephemeral_uv_benchmark_command(
    *,
    dataset_path: Path = DEFAULT_DATASET_PATH,
    output_path: Path | None = None,
    include_mem0: bool = True,
    include_basic_memory: bool = True,
    include_graphiti_kuzu: bool = True,
    include_zep: bool = False,
) -> list[str]:
    """Build an isolated `uv run --with ...` invocation without mutating pyproject.toml or uv.lock."""
    cmd = ["uv", "run"]
    for pkg in ("rank-bm25", "faiss-cpu"):
        cmd.extend(["--with", pkg])
    if include_mem0:
        cmd.extend(["--with", "mem0ai"])
    if include_basic_memory:
        cmd.extend(["--with", "basic-memory"])
    if include_graphiti_kuzu:
        cmd.extend(["--with", "graphiti-core", "--with", "kuzu"])
    if include_zep:
        cmd.extend(["--with", "zep-cloud"])
    cmd.extend(["python", str(Path(__file__).resolve()), "--dataset", str(dataset_path)])
    if output_path is not None:
        cmd.extend(["--output", str(output_path)])
    return cmd


def run_live_third_party_benchmark(
    *,
    dataset_path: Path = DEFAULT_DATASET_PATH,
    atoms_dir: Path | None = None,
    mem0_infer: bool = True,
    optional_adapters: list[_ExternalMemoryAdapter] | None = None,
    include_optional_adapters: bool = True,
) -> dict[str, Any]:
    import chromadb
    import faiss
    from mem0 import Memory
    import numpy as np
    from rank_bm25 import BM25Okapi

    resolved_brain_str, resolved_atoms_str = resolve_module_atoms_and_brain(globals(), atoms_dir=atoms_dir)
    resolved_atoms_dir = Path(resolved_atoms_str)
    resolved_brain_for_live = resolved_brain_str if atoms_dir is not None else None
    resolved_chroma_path = resolve_module_vault_path(globals(), "CHROMA_PATH", brain_dir=resolved_brain_for_live)
    resolved_lexical_path = resolve_module_vault_path(globals(), "LEXICAL_INDEX_PATH", brain_dir=resolved_brain_for_live)

    hw = detect_hardware_profile()
    active_llm = resolve_active_local_generation_model(fallback=GEN_MODEL)
    base_url = ollama_utils.ollama_base_url()

    corpus = load_dataset(
        dataset_path,
        atoms_dir=str(resolved_atoms_dir),
        approval_check=lambda path: approved_revision_is_current(str(path), atoms_dir=str(resolved_atoms_dir)),
    )
    dataset_sha256 = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    vault_atoms, unapproved_set = _load_vault_atoms(resolved_atoms_dir)

    # Embed all 78 vault files once via live Ollama nomic-embed-text (768d) for fair dense index setup
    t_embed0 = time.perf_counter()
    atom_vectors: list[list[float]] = [
        ollama_utils.embed_text(item["embed_input"], model=EMBED_MODEL) for item in vault_atoms
    ]
    embed_dim = len(atom_vectors[0])
    vault_embed_setup_ms = round((time.perf_counter() - t_embed0) * 1000.0, 2)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)

        # 1. Build live Rank-BM25 (BM25Okapi)
        bm25_tokens = [_tokenize(item["raw_text"]) or ["_empty_"] for item in vault_atoms]
        bm25_index = BM25Okapi(bm25_tokens)

        # 2. Build live SQLite FTS5 untrusted baseline
        fts_conn = sqlite3.connect(str(tmp_path / "untrusted_fts5.sqlite3"))
        fts_conn.execute("CREATE VIRTUAL TABLE docs USING fts5(filename, claim, body)")
        for item in vault_atoms:
            fts_conn.execute(
                "INSERT INTO docs VALUES (?, ?, ?)",
                (item["filename"], item["claim"], item["raw_text"]),
            )
        fts_conn.commit()

        # 3. Build live FAISS IndexHNSWFlat (768d cosine / inner-product on L2-normalized vectors)
        faiss_mat = np.asarray(atom_vectors, dtype=np.float32)
        faiss_norms = np.linalg.norm(faiss_mat, axis=1, keepdims=True)
        faiss_norms[faiss_norms == 0.0] = 1.0
        faiss_mat = faiss_mat / faiss_norms
        faiss_index = faiss.IndexHNSWFlat(embed_dim, 16, faiss.METRIC_INNER_PRODUCT)
        faiss_index.hnsw.efConstruction = 64
        faiss_index.hnsw.efSearch = 32
        faiss_index.add(faiss_mat)

        # 4. Build live ChromaDB PersistentClient HNSW cosine baseline (untrusted)
        chroma_client = chromadb.PersistentClient(path=str(tmp_path / "chroma_live"))
        chroma_col = chroma_client.get_or_create_collection(
            name="vault_untrusted_hnsw",
            configuration={"hnsw": {"space": "cosine"}},
        )
        chroma_col.upsert(
            ids=[item["filename"] for item in vault_atoms],
            documents=[item["embed_input"] for item in vault_atoms],
            metadatas=[
                {
                    "filename": item["filename"],
                    "contexts": ",".join(item["project_tags"]),
                    "review_status": item["review_status"],
                }
                for item in vault_atoms
            ],
            embeddings=atom_vectors,
        )

        # 5. Build live Mem0 v2.x (Memory.from_config backed by local Ollama + Chroma)
        t_mem0_setup = time.perf_counter()
        mem0_cfg = {
            "vector_store": {
                "provider": "chroma",
                "config": {
                    "collection_name": "mem0_vault_curated",
                    "path": str(tmp_path / "mem0_live_store"),
                },
            },
            "embedder": {
                "provider": "ollama",
                "config": {
                    "model": EMBED_MODEL,
                    "ollama_base_url": base_url,
                    "embedding_dims": embed_dim,
                },
            },
            "llm": {
                "provider": "ollama",
                "config": {
                    "model": active_llm,
                    "ollama_base_url": base_url,
                },
            },
            "version": "v1.1",
        }
        mem0_engine = Memory.from_config(mem0_cfg)
        for item in vault_atoms:
            primary_project = item["project_tags"][0] if item["project_tags"] else ""
            mem0_meta = {
                "filename": item["filename"],
                "contexts": ",".join(item["project_tags"]),
                "project": primary_project,
                "review_status": item["review_status"],
            }
            try:
                mem0_engine.add(
                    item["claim"],
                    user_id="owner",
                    metadata=mem0_meta,
                    infer=bool(mem0_infer),
                )
            except (ValueError, KeyError, TypeError):
                # Some minimal/short facts yield empty structured JSON under LLM infer=True
                if mem0_infer:
                    mem0_engine.add(
                        item["claim"],
                        user_id="owner",
                        metadata=mem0_meta,
                        infer=False,
                    )
                else:
                    raise
        mem0_setup_ms = round((time.perf_counter() - t_mem0_setup) * 1000.0, 2)

        # Keep Second Brain's live Chroma collection open during the run
        sb_live_col = get_vector_collection(resolved_chroma_path, name="atoms", create=False)
        import scripts.recall as _recall_mod
        from unittest.mock import patch

        active_optional_adapters: list[_ExternalMemoryAdapter] = []
        skipped_systems: dict[str, dict[str, Any]] = {}
        optional_setup_timings: dict[str, float] = {}
        candidate_adapters = (
            list(optional_adapters)
            if optional_adapters is not None
            else (default_optional_adapters() if include_optional_adapters else [])
        )
        for adapter in candidate_adapters:
            avail_ok, avail_reason = adapter.check_available()
            if not avail_ok:
                skipped_systems[adapter.name] = {
                    "status": "skipped_unavailable",
                    "packages": list(adapter.packages),
                    "reason": avail_reason,
                }
                continue
            try:
                setup_ms = adapter.setup(
                    vault_atoms,
                    tmp_path,
                    active_llm=active_llm,
                    base_url=base_url,
                    embed_dim=embed_dim,
                )
                active_optional_adapters.append(adapter)
                optional_setup_timings[f"{adapter.name}_setup_ms"] = setup_ms
            except Exception as exc:
                skipped_systems[adapter.name] = {
                    "status": "skipped_unavailable",
                    "packages": list(adapter.packages),
                    "reason": f"Adapter initialization failed: {exc}",
                }

        system_names = (
            "second_brain_verified",
            "second_brain_hybrid_rrf",
            "mem0_live_v2",
            "faiss_hnsw_live",
            "chromadb_hnsw_live",
            "rank_bm25_okapi_live",
            "sqlite_fts5_untrusted_live",
            *[a.name for a in active_optional_adapters],
        )
        metrics_acc: dict[str, dict[str, Any]] = {
            s: {
                "answerable_recalls": [],
                "answerable_mrrs": [],
                "answerable_ndcgs": [],
                "candidate_recalls": [],
                "candidate_mrrs": [],
                "candidate_ndcgs": [],
                "trap_total": 0,
                "trap_false_positive_queries": 0,
                "unapproved_leak_queries": 0,
                "cross_project_total": 0,
                "cross_project_leak_queries": 0,
                "latencies_ms": [],
            }
            for s in system_names
        }

        def _score_query_run(
            sys_name: str,
            *,
            trusted_ranked: list[str],
            candidate_ranked: list[str],
            case: dict[str, Any],
            latency_ms: float,
        ) -> None:
            acc = metrics_acc[sys_name]
            acc["latencies_ms"].append(latency_ms)
            ans_type = case["answerability"]
            cand_rel = set(case.get("candidate_relevance") or [])
            if cand_rel:
                acc["candidate_recalls"].append(
                    len(cand_rel.intersection(candidate_ranked[:TOP_K])) / len(cand_rel)
                )
                acc["candidate_mrrs"].append(_mrr(candidate_ranked, cand_rel))
                acc["candidate_ndcgs"].append(_ndcg_at_k(candidate_ranked, cand_rel, TOP_K))

            if ans_type == "answerable":
                exp = set(case["expected_atom_ids"])
                acc["answerable_recalls"].append(
                    len(exp.intersection(trusted_ranked[:TOP_K])) / max(1, len(exp))
                )
                acc["answerable_mrrs"].append(_mrr(trusted_ranked, exp))
                acc["answerable_ndcgs"].append(_ndcg_at_k(trusted_ranked, exp, TOP_K))
                if any(f in unapproved_set for f in trusted_ranked[:TOP_K]):
                    acc["unapproved_leak_queries"] += 1
            elif ans_type == "unanswerable":
                acc["trap_total"] += 1
                if trusted_ranked:
                    acc["trap_false_positive_queries"] += 1
                if any(f in unapproved_set for f in trusted_ranked[:TOP_K]):
                    acc["unapproved_leak_queries"] += 1
                if case.get("category") == "cross_project_trap":
                    acc["cross_project_total"] += 1
                    req_proj = str(case.get("project_scope") or "").strip()
                    for fname in trusted_ranked[:TOP_K]:
                        atom_meta = next((a for a in vault_atoms if a["filename"] == fname), None)
                        tags = atom_meta["project_tags"] if atom_meta else ()
                        if not tags or not bool(project_tags_match(tags, (req_proj,))):
                            acc["cross_project_leak_queries"] += 1
                            break

        try:
            with (
                patch.object(_recall_mod, "get_vector_collection", return_value=sb_live_col),
                patch.object(_recall_mod, "close_vector_collection", lambda _c: None),
            ):
                # Warm ONNX Cross-Encoder once prior to timed query loop
                score_query_atom_pairs("warmup", ["warmup"], allow_onnx=True)

                for case in corpus["queries"]:
                    q_text = case["query"]
                    proj_tuple = (str(case["project_scope"]).strip(),) if case.get("project_scope") else ()
                    ans_type = case["answerability"]

                    # 1. Second Brain Verified (Auto / Lexical + ONNX CE + CAS + Byte Anchors + Scope)
                    t0 = time.perf_counter()
                    sb_lex_direct, sb_lex_neighbors = search_memory(
                        q_text,
                        RecallOptions(
                            n_results=TOP_K,
                            follow_links=True,
                            retrieval_mode="auto",
                            projects=proj_tuple,
                        ),
                        atoms_dir=str(resolved_atoms_dir),
                        index_path=resolved_lexical_path,
                        chroma_path=resolved_chroma_path,
                    )
                    sb_lex_ms = (time.perf_counter() - t0) * 1000.0
                    sb_lex_trusted = [h.filename for h in (list(sb_lex_direct) + list(sb_lex_neighbors))[:TOP_K]]
                    if ans_type in ("preference", "exploratory"):
                        raw_lex = search_lexical_index(
                            q_text, resolved_lexical_path, n_results=TOP_K, atoms_dir=str(resolved_atoms_dir)
                        )
                        sb_lex_cand = [h.filename for h in raw_lex[:TOP_K]]
                    else:
                        sb_lex_cand = sb_lex_trusted
                    _score_query_run(
                        "second_brain_verified",
                        trusted_ranked=sb_lex_trusted,
                        candidate_ranked=sb_lex_cand,
                        case=case,
                        latency_ms=sb_lex_ms,
                    )

                    # 2. Second Brain Hybrid RRF (FTS5 + Chroma 768d RRF + ONNX CE + CAS + Byte Anchors + Scope)
                    t0 = time.perf_counter()
                    sb_rrf_direct, sb_rrf_neighbors = search_hybrid_rrf_memory(
                        q_text,
                        RecallOptions(
                            n_results=TOP_K,
                            follow_links=True,
                            retrieval_mode="rrf",
                            projects=proj_tuple,
                        ),
                        atoms_dir=str(resolved_atoms_dir),
                        index_path=resolved_lexical_path,
                        chroma_path=resolved_chroma_path,
                    )
                    sb_rrf_ms = (time.perf_counter() - t0) * 1000.0
                    sb_rrf_trusted = [h.filename for h in (list(sb_rrf_direct) + list(sb_rrf_neighbors))[:TOP_K]]
                    if ans_type in ("preference", "exploratory"):
                        exp_rrf, _ = search_hybrid_rrf_memory(
                            q_text,
                            RecallOptions(
                                n_results=TOP_K,
                                follow_links=False,
                                allow_unreviewed_exploratory=True,
                                projects=proj_tuple,
                            ),
                            atoms_dir=str(resolved_atoms_dir),
                            index_path=resolved_lexical_path,
                            chroma_path=resolved_chroma_path,
                        )
                        sb_rrf_cand = [h.filename for h in exp_rrf[:TOP_K]]
                    else:
                        sb_rrf_cand = sb_rrf_trusted
                    _score_query_run(
                        "second_brain_hybrid_rrf",
                        trusted_ranked=sb_rrf_trusted,
                        candidate_ranked=sb_rrf_cand,
                        case=case,
                        latency_ms=sb_rrf_ms,
                    )

                    # 3. Live Mem0 v2.x (mem0ai Memory.search with Ollama 768d + Chroma + project scope filter)
                    t0 = time.perf_counter()
                    m0_filters: dict[str, Any] = {"user_id": "owner"}
                    if proj_tuple:
                        m0_filters["project"] = proj_tuple[0]
                    m0_res = mem0_engine.search(q_text, filters=m0_filters, limit=TOP_K)
                    m0_ms = (time.perf_counter() - t0) * 1000.0
                    m0_items = m0_res.get("results", []) if isinstance(m0_res, dict) else []
                    m0_ranked = [
                        str((it.get("metadata") or {}).get("filename") or "")
                        for it in m0_items[:TOP_K]
                        if isinstance(it, dict) and (it.get("metadata") or {}).get("filename")
                    ]
                    _score_query_run(
                        "mem0_live_v2",
                        trusted_ranked=m0_ranked,
                        candidate_ranked=m0_ranked,
                        case=case,
                        latency_ms=m0_ms,
                    )

                    # 4. Live FAISS HNSW (faiss-cpu IndexHNSWFlat 768d)
                    t0 = time.perf_counter()
                    q_vec = ollama_utils.embed_text(q_text, model=EMBED_MODEL)
                    q_arr = np.asarray([q_vec], dtype=np.float32)
                    q_norm = float(np.linalg.norm(q_arr))
                    if q_norm > 0.0:
                        q_arr = q_arr / q_norm
                    f_scores, f_idxs = faiss_index.search(q_arr, TOP_K)
                    fa_ms = (time.perf_counter() - t0) * 1000.0
                    fa_ranked = [
                        vault_atoms[int(i)]["filename"]
                        for sc, i in zip(f_scores[0], f_idxs[0])
                        if int(i) >= 0 and float(sc) > 0.05
                    ]
                    _score_query_run(
                        "faiss_hnsw_live",
                        trusted_ranked=fa_ranked,
                        candidate_ranked=fa_ranked,
                        case=case,
                        latency_ms=fa_ms,
                    )

                    # 5. Live ChromaDB PersistentClient HNSW cosine (untrusted)
                    t0 = time.perf_counter()
                    c_vec = ollama_utils.embed_text(q_text, model=EMBED_MODEL)
                    c_res = chroma_col.query(query_embeddings=[c_vec], n_results=TOP_K)
                    ch_ms = (time.perf_counter() - t0) * 1000.0
                    ch_metas = (c_res.get("metadatas") or [[]])[0]
                    ch_ranked = [
                        str(m.get("filename") or "")
                        for m in ch_metas[:TOP_K]
                        if isinstance(m, dict) and m.get("filename")
                    ]
                    _score_query_run(
                        "chromadb_hnsw_live",
                        trusted_ranked=ch_ranked,
                        candidate_ranked=ch_ranked,
                        case=case,
                        latency_ms=ch_ms,
                    )

                    # 6. Live Rank-BM25 (BM25Okapi)
                    t0 = time.perf_counter()
                    q_toks = _tokenize(q_text)
                    if q_toks:
                        b_scores = bm25_index.get_scores(q_toks)
                        b_pairs = sorted(
                            (
                                (float(sc), vault_atoms[i]["filename"])
                                for i, sc in enumerate(b_scores)
                                if float(sc) > 0.0
                            ),
                            key=lambda kv: (-kv[0], kv[1]),
                        )
                        rb_ranked = [fname for _, fname in b_pairs[:TOP_K]]
                    else:
                        rb_ranked = []
                    rb_ms = (time.perf_counter() - t0) * 1000.0
                    _score_query_run(
                        "rank_bm25_okapi_live",
                        trusted_ranked=rb_ranked,
                        candidate_ranked=rb_ranked,
                        case=case,
                        latency_ms=rb_ms,
                    )

                    # 7. Live SQLite FTS5 Untrusted BM25
                    t0 = time.perf_counter()
                    fts_words = [
                        w
                        for w in "".join(c if c.isalnum() or c == "_" else " " for c in q_text).split()
                        if len(w) >= 3 and w.lower() not in QUERY_STOP_WORDS
                    ]
                    if fts_words:
                        match_expr = " OR ".join(f'"{w}"' for w in fts_words[:12])
                        rows = fts_conn.execute(
                            "SELECT filename FROM docs WHERE docs MATCH ? ORDER BY bm25(docs) LIMIT ?",
                            (match_expr, TOP_K),
                        ).fetchall()
                        fts_ranked = [str(r[0]) for r in rows]
                    else:
                        fts_ranked = []
                    fts_ms = (time.perf_counter() - t0) * 1000.0
                    _score_query_run(
                        "sqlite_fts5_untrusted_live",
                        trusted_ranked=fts_ranked,
                        candidate_ranked=fts_ranked,
                        case=case,
                        latency_ms=fts_ms,
                    )

                    # 8..10. Optional isolated adapters (basic_memory_live, graphiti_kuzu_live, zep_live)
                    scope_str = proj_tuple[0] if proj_tuple else ""
                    for adapter in active_optional_adapters:
                        t0 = time.perf_counter()
                        opt_ranked = adapter.search(q_text, project_scope=scope_str, top_k=TOP_K)
                        opt_ms = (time.perf_counter() - t0) * 1000.0
                        _score_query_run(
                            adapter.name,
                            trusted_ranked=opt_ranked,
                            candidate_ranked=opt_ranked,
                            case=case,
                            latency_ms=opt_ms,
                        )
        finally:
            for adapter in active_optional_adapters:
                adapter.close()
            fts_conn.close()
            _close_chroma_client(chroma_client)
            m0_vs = getattr(mem0_engine, "vector_store", None)
            m0_client = getattr(m0_vs, "client", None)
            if m0_client is not None:
                _close_chroma_client(m0_client)
            close_vector_collection(sb_live_col)
            gc.collect()

    _, rerank_backend = score_query_atom_pairs("probe", ["probe"], allow_onnx=True)
    systems_summary: dict[str, dict[str, Any]] = {}
    for name, acc in metrics_acc.items():
        trap_n = max(1, acc["trap_total"])
        cross_n = max(1, acc["cross_project_total"])
        fp_rate = round(100.0 * acc["trap_false_positive_queries"] / trap_n, 1)
        systems_summary[name] = {
            "answerable_expected_recall_at_5_pct": round(100.0 * statistics.mean(acc["answerable_recalls"]), 1),
            "answerable_mrr_at_5": round(statistics.mean(acc["answerable_mrrs"]), 4),
            "answerable_ndcg_at_5": round(statistics.mean(acc["answerable_ndcgs"]), 4),
            "candidate_mean_recall_at_5_pct": round(100.0 * statistics.mean(acc["candidate_recalls"]), 1),
            "candidate_mean_mrr_at_5": round(statistics.mean(acc["candidate_mrrs"]), 4),
            "candidate_mean_ndcg_at_5": round(statistics.mean(acc["candidate_ndcgs"]), 4),
            "trap_false_positive_rate_pct": fp_rate,
            "abstention_accuracy_pct": round(100.0 - fp_rate, 1),
            "unapproved_candidate_leak_queries": acc["unapproved_leak_queries"],
            "cross_project_leak_pct": round(100.0 * acc["cross_project_leak_queries"] / cross_n, 1),
            "latency_p50_ms": _pct(acc["latencies_ms"], 50),
            "latency_p95_ms": _pct(acc["latencies_ms"], 95),
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "benchmark_type": "live_curated_vault_third_party_comparison",
        "dataset_id": corpus["dataset_id"],
        "dataset_version": corpus["dataset_version"],
        "dataset_sha256": dataset_sha256,
        "vault_total_atoms": len(vault_atoms),
        "vault_owner_approved_atoms": len(vault_atoms) - len(unapproved_set),
        "vault_unapproved_candidate_atoms": sorted(unapproved_set),
        "query_counts": {
            "total": len(corpus["queries"]),
            "answerable": sum(1 for q in corpus["queries"] if q["answerability"] == "answerable"),
            "preference": sum(1 for q in corpus["queries"] if q["answerability"] == "preference"),
            "exploratory": sum(1 for q in corpus["queries"] if q["answerability"] == "exploratory"),
            "unanswerable": sum(1 for q in corpus["queries"] if q["answerability"] == "unanswerable"),
        },
        "embedding_model": EMBED_MODEL,
        "embedding_dimensions": embed_dim,
        "rerank_backend": rerank_backend,
        "package_versions": _package_versions(),
        "setup_timing_ms": {
            "vault_78_atom_ollama_embed_ms": vault_embed_setup_ms,
            "mem0_live_index_setup_ms": mem0_setup_ms,
            **optional_setup_timings,
        },
        "hardware_profile": asdict(hw),
        "systems": systems_summary,
        "skipped_systems": skipped_systems,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        default=str(DEFAULT_DATASET_PATH),
        help="Path to human-curated evaluation dataset JSON.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional path to write full JSON benchmark results.",
    )
    parser.add_argument(
        "--mem0-no-infer",
        action="store_true",
        help="Bypass Mem0 LLM extraction (`infer=False`) instead of fair default `infer=True`.",
    )
    parser.add_argument(
        "--skip-optional",
        action="store_true",
        help="Skip probing optional Basic Memory / Graphiti+Kuzu / Zep adapters.",
    )
    parser.add_argument(
        "--print-ephemeral-command",
        action="store_true",
        help="Print the isolated `uv run --with ...` invocation covering all optional adapters and exit.",
    )
    parser.add_argument("--json", action="store_true", help="Print full JSON payload to stdout.")
    args = parser.parse_args(argv)

    if args.print_ephemeral_command:
        cmd = build_ephemeral_uv_benchmark_command(
            dataset_path=Path(args.dataset),
            output_path=Path(args.output) if args.output else None,
        )
        print(" ".join(cmd))
        return 0

    result = run_live_third_party_benchmark(
        dataset_path=Path(args.dataset),
        mem0_infer=not bool(args.mem0_no_infer),
        include_optional_adapters=not bool(args.skip_optional),
    )
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(
        f"Live Third-Party Benchmark on Curated Vault — {result['vault_total_atoms']} atoms "
        f"({result['vault_owner_approved_atoms']} approved, {len(result['vault_unapproved_candidate_atoms'])} pending) | "
        f"{result['query_counts']['total']} curated queries | Packages: {result['package_versions']}"
    )
    for sys_name, m in result["systems"].items():
        print(
            f"  {sys_name:28s} ans_rec@5={m['answerable_expected_recall_at_5_pct']:5.1f}% "
            f"cand_rec@5={m['candidate_mean_recall_at_5_pct']:5.1f}% "
            f"abstain={m['abstention_accuracy_pct']:5.1f}% "
            f"unapproved_leaks={m['unapproved_candidate_leak_queries']:2d} "
            f"cross_leak={m['cross_project_leak_pct']:5.1f}% "
            f"p50={m['latency_p50_ms']:6.2f}ms p95={m['latency_p95_ms']:6.2f}ms"
        )
    for skipped_name, info in (result.get("skipped_systems") or {}).items():
        print(f"  {skipped_name:28s} [SKIPPED: {info.get('reason')}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
