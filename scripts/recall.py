# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = [
#     "ollama",
#     "chromadb",
# ]
# ///
"""
Shared recall contract for Second Brain.

The vector store finds candidate memories. This module applies the durable
Markdown memory metadata before callers inject those memories into chat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
import os
import re

from config import ATOMS_DIR, CHROMA_PATH, EMBED_MODEL, LEXICAL_INDEX_PATH, normalize_markdown_filename, safe_file_in_dir
from config import BRAIN_DIR, RAW_DIR
from index_manifest import index_manifest_is_current, load_index_manifest
from lexical_store import LexicalIndexUnavailable, TOKEN_PATTERN, search_lexical_index
from memory_format import split_frontmatter
from memory_review import REVIEW_STATUSES, parse_atom_record, parse_scalar_list, section_text
from review_atom import approved_revision_is_current
from vector_store import get_vector_collection


class MemoryRecallUnavailable(RuntimeError):
    """Raised when semantic recall cannot run without compromising retrieval integrity."""


def validated_index_generation(
    manifest: dict | None,
    *,
    atoms_dir: str,
    collection_name: str,
    embedding_model: str,
    embedding_dimensions: int,
) -> str:
    """Return the active generation only if it matches the live atoms and embedder contract."""
    if not isinstance(manifest, dict):
        raise MemoryRecallUnavailable("Semantic recall index has no verified generation manifest; rebuild it first.")
    if manifest.get("collection_name") != collection_name:
        raise MemoryRecallUnavailable("Semantic recall index collection does not match the configured collection.")
    if manifest.get("embedding_model") != embedding_model:
        raise MemoryRecallUnavailable("Semantic recall index was built with a different embedding model; rebuild it first.")
    if manifest.get("embedding_dimensions") != embedding_dimensions:
        raise MemoryRecallUnavailable("Semantic recall index dimensions do not match the current embedding model.")
    generation_id = manifest.get("generation_id")
    if not isinstance(generation_id, str) or not generation_id:
        raise MemoryRecallUnavailable("Semantic recall index generation identifier is missing.")
    if manifest.get("distance_metric") != "cosine":
        raise MemoryRecallUnavailable("Semantic recall index uses an unsupported distance metric; rebuild it with cosine distance.")
    if not index_manifest_is_current(manifest, atoms_dir):
        raise MemoryRecallUnavailable("Semantic recall index is stale because canonical atoms changed; rebuild it first.")
    return generation_id


NORMAL_RECALL_STATUSES = {"accepted"}
EXCLUDED_RECALL_STATUSES = {"rejected", "merged"}
REVIEW_STATUS_WEIGHTS = {
    "accepted": 0.12,
    "pending": -0.02,
    "needs_edit": -0.06,
    "rejected": -1.00,
    "merged": -1.00,
}
QUERY_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "could",
    "did", "do", "does", "for", "from", "how", "i", "in", "into", "is", "it",
    "me", "my", "of", "on", "or", "our", "owner", "owners", "owner's", "should",
    "that", "the", "their", "these", "this", "those", "to", "we", "what", "when",
    "where", "which", "who", "why", "will", "with", "would", "you", "your",
}


@dataclass(frozen=True)
class RecallOptions:
    n_results: int = 5
    follow_links: bool = True
    include_statuses: set[str] | None = None
    exclude_statuses: set[str] = field(default_factory=lambda: set(EXCLUDED_RECALL_STATUSES))
    contexts: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    memory_types: tuple[str, ...] = ()
    candidate_multiplier: int = 4
    retrieval_mode: str = "auto"  # 'auto', 'rrf', 'lexical', 'dense'


@dataclass(frozen=True)
class RecallResult:
    filename: str
    content: str
    source: str
    score: float
    similarity: float
    review_status: str
    memory_type: str
    confidence: float
    importance: float
    contexts: tuple[str, ...] = ()
    match_reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "content": self.content,
            "source": self.source,
            "score": self.score,
            "similarity": self.similarity,
            "review_status": self.review_status,
            "memory_type": self.memory_type,
            "confidence": self.confidence,
            "importance": self.importance,
            "contexts": list(self.contexts),
            "match_reasons": list(self.match_reasons),
        }


def clamp_number(value: str | float | int | None, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def cosine_distance_to_similarity(distance: float | int | str | None) -> float:
    """Map Chroma cosine distance [0, 2] to similarity [1, 0] without saturation at 1."""
    try:
        value = float(distance)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return 1.0 - max(0.0, min(2.0, value)) / 2.0


def normalize_statuses(statuses: set[str] | None) -> set[str] | None:
    if statuses is None:
        return None
    normalized = {status for status in statuses if status in REVIEW_STATUSES}
    return normalized or None


def metadata_contexts(record: dict) -> tuple[str, ...]:
    contexts = record.get("contexts", [])
    if isinstance(contexts, str):
        contexts = parse_scalar_list(contexts)
    return tuple(str(item).strip() for item in contexts if str(item).strip())


def load_memory_metadata(filename: str, atoms_dir: str = ATOMS_DIR) -> dict:
    path = safe_file_in_dir(atoms_dir, filename, allowed_exts={".md"})
    if not path:
        return {
            "filename": filename,
            "memory_type": "semantic",
            "review_status": "pending",
            "claim_state": "candidate",
            "confidence": "0.50",
            "importance": "0.50",
            "contexts": [],
        }
    try:
        record = parse_atom_record(path)
    except Exception:
        record = {
            "filename": filename,
            "memory_type": "semantic",
            "review_status": "needs_edit",
            "claim_state": "candidate",
            "confidence": "0.20",
            "importance": "0.20",
            "contexts": [],
        }
    effective_brain_dir = os.path.abspath(os.path.join(atoms_dir, os.pardir))
    decision_log = os.path.join(effective_brain_dir, "0-raw", "archive", "decision-record.jsonl")
    if os.path.abspath(atoms_dir) == os.path.abspath(ATOMS_DIR):
        effective_brain_dir = BRAIN_DIR
        decision_log = os.path.join(RAW_DIR, "archive", "decision-record.jsonl")
    record["approved_revision_current"] = approved_revision_is_current(
        path,
        decision_log_path=decision_log,
        brain_dir=effective_brain_dir,
        atoms_dir=atoms_dir,
    )
    return record


def should_include_record(record: dict, options: RecallOptions) -> bool:
    status = str(record.get("review_status", "pending"))
    claim_state = str(record.get("claim_state", "candidate"))
    include_statuses = normalize_statuses(options.include_statuses)
    exclude_statuses = normalize_statuses(options.exclude_statuses) or set()

    if status in exclude_statuses:
        return False
    if include_statuses is not None and status not in include_statuses:
        return False
    if include_statuses is None and status not in NORMAL_RECALL_STATUSES:
        return False
    if include_statuses is None:
        if claim_state != "human_reviewed":
            return False
        if record.get("approved_revision_current") is not True:
            return False

    if options.memory_types:
        memory_type = str(record.get("memory_type", "semantic"))
        if memory_type not in set(options.memory_types):
            return False

    if options.projects:
        record_contexts = metadata_contexts(record)
        proj_tags = {
            c.lower().replace("-", "_")
            for c in record_contexts
            if c.lower().startswith("project:")
        }
        req_tags = {
            p.lower().replace("-", "_") if p.lower().startswith("project:") else f"project:{p.lower().replace('-', '_')}"
            for p in options.projects
        }
        if not proj_tags.intersection(req_tags):
            return False

    return True


def context_match_score(record_contexts: tuple[str, ...], requested_contexts: tuple[str, ...]) -> tuple[float, tuple[str, ...]]:
    if not requested_contexts:
        return 0.0, ()

    record_lookup = {context.lower(): context for context in record_contexts}
    matches = []
    for context in requested_contexts:
        if context.lower() in record_lookup:
            matches.append(record_lookup[context.lower()])

    if not matches:
        return 0.0, ()
    return len(matches) / len(requested_contexts), tuple(matches)


def result_from_candidate(
    content: str,
    filename: str,
    distance: float | None,
    options: RecallOptions,
    source: str,
    atoms_dir: str = ATOMS_DIR,
    similarity_override: float | None = None,
) -> RecallResult | None:
    record = load_memory_metadata(filename, atoms_dir=atoms_dir)
    if not should_include_record(record, options):
        return None

    similarity = (
        clamp_number(similarity_override)
        if similarity_override is not None
        else cosine_distance_to_similarity(distance)
    )
    confidence = clamp_number(record.get("confidence"), default=0.50)
    importance = clamp_number(record.get("importance"), default=0.50)
    status = str(record.get("review_status", "pending"))
    memory_type = str(record.get("memory_type", "semantic"))
    contexts = metadata_contexts(record)
    context_score, matched_contexts = context_match_score(contexts, options.contexts)

    score = (
        similarity * 0.70
        + confidence * 0.10
        + importance * 0.10
        + context_score * 0.08
        + REVIEW_STATUS_WEIGHTS.get(status, -0.04)
    )

    reasons = [f"status:{status}", f"type:{memory_type}"]
    if matched_contexts:
        reasons.append("context:" + ",".join(matched_contexts))

    return RecallResult(
        filename=filename,
        content=content,
        source=source,
        score=round(score, 6),
        similarity=round(similarity, 6),
        review_status=status,
        memory_type=memory_type,
        confidence=confidence,
        importance=importance,
        contexts=contexts,
        match_reasons=tuple(reasons),
    )


def candidate_has_query_overlap(query: str, content: str) -> bool:
    """Require literal claim/evidence corroboration before trusted recall injects a result.

    This is a conservative relevance precondition, not a relevance score or proof of
    entailment. Supporting evidence can be more specific than a concise claim, so both
    sections count. Synonym-only matches remain excluded until evaluation justifies a
    stronger answerability method.
    """
    parsed = split_frontmatter(content)
    if parsed is None:
        return False
    body = "\n".join(parsed[1])
    claim = section_text(body, "Claim")
    evidence = section_text(body, "Evidence")
    query_terms = {
        term.casefold()
        for term in TOKEN_PATTERN.findall(str(query or ""))
        if term.casefold() not in QUERY_STOP_WORDS and len(term) > 1
    }
    corroborating_terms = {
        term.casefold()
        for term in TOKEN_PATTERN.findall(f"{claim}\n{evidence}")
    }
    return len(query_terms.intersection(corroborating_terms)) >= 2


def wikilinks(content: str) -> list[str]:
    return [normalize_markdown_filename(link) for link in re.findall(r"\[\[(.*?)\]\]", content or "")]


def linked_neighbors(
    result: RecallResult,
    options: RecallOptions,
    atoms_dir: str = ATOMS_DIR,
    *,
    query: str,
) -> list[RecallResult]:
    neighbors = []
    for target in wikilinks(result.content):
        target_path = safe_file_in_dir(atoms_dir, target, allowed_exts={".md"})
        if not target_path:
            continue
        with open(target_path, "r", encoding="utf-8") as file:
            content = file.read()
        if not candidate_has_query_overlap(query, content):
            continue
        neighbor = result_from_candidate(
            content=content,
            filename=target,
            distance=0.35,
            options=options,
            source="graph",
            atoms_dir=atoms_dir,
        )
        if neighbor:
            neighbors.append(neighbor)
    return neighbors


def verified_atom_content(filename: str, expected_sha256: str, atoms_dir: str = ATOMS_DIR) -> str | None:
    """Load an atom only when the vector record proves it was derived from this exact revision."""
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        return None
    path = safe_file_in_dir(atoms_dir, filename, allowed_exts={".md"})
    if not path:
        return None
    try:
        with open(path, "rb") as file:
            content_bytes = file.read()
        content = content_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    actual_sha256 = hashlib.sha256(content_bytes).hexdigest()
    return content if actual_sha256 == expected_sha256 else None


def rerank_candidates(
    results: dict,
    options: RecallOptions,
    *,
    query: str,
    atoms_dir: str = ATOMS_DIR,
) -> list[RecallResult]:
    documents = (results.get("documents") or [[]])[0]
    metadatas = (results.get("metadatas") or [[]])[0]
    distances = (results.get("distances") or [[]])[0]

    ranked = []
    for index, content in enumerate(documents):
        metadata = metadatas[index] if index < len(metadatas) and isinstance(metadatas[index], dict) else {}
        distance = distances[index] if index < len(distances) else None
        filename = metadata.get("filename", "unknown.md")
        current_content = verified_atom_content(
            filename,
            metadata.get("content_sha256", ""),
            atoms_dir=atoms_dir,
        )
        if current_content is None:
            continue
        result = result_from_candidate(
            content=current_content,
            filename=filename,
            distance=distance,
            options=options,
            source="vector",
            atoms_dir=atoms_dir,
        )
        if result:
            if candidate_has_query_overlap(query, current_content):
                ranked.append(result)

    return sorted(ranked, key=lambda item: item.score, reverse=True)[: options.n_results]


def search_semantic_memory(query: str, options: RecallOptions | None = None) -> tuple[list[RecallResult], list[RecallResult]]:
    from ollama_utils import EmbeddingUnavailableError, embed_text

    options = options or RecallOptions()
    if not os.path.exists(CHROMA_PATH):
        return [], []

    manifest = load_index_manifest(CHROMA_PATH)

    try:
        collection = get_vector_collection(CHROMA_PATH, name="atoms", create=False)
    except Exception as exc:
        raise MemoryRecallUnavailable(f"Semantic recall index is unavailable: {exc}") from exc

    try:
        embedding = embed_text(query, model=EMBED_MODEL)
    except EmbeddingUnavailableError as exc:
        raise MemoryRecallUnavailable(f"Semantic recall is unavailable: {exc}") from exc
    generation_id = validated_index_generation(
        manifest,
        atoms_dir=ATOMS_DIR,
        collection_name="atoms",
        embedding_model=EMBED_MODEL,
        embedding_dimensions=len(embedding),
    )
    candidate_count = max(options.n_results, options.n_results * max(1, options.candidate_multiplier))
    try:
        results = collection.query(
            query_embeddings=[embedding],
            n_results=candidate_count,
            where={
                "$and": [
                    {"embedding_model": EMBED_MODEL},
                    {"index_generation": generation_id},
                ]
            },
        )
    except Exception as exc:
        raise MemoryRecallUnavailable(f"Semantic recall query failed: {exc}") from exc
    direct = rerank_candidates(results, options, query=query)

    neighbors = []
    seen = {item.filename for item in direct}
    if options.follow_links:
        for result in direct:
            for neighbor in linked_neighbors(result, options, query=query):
                if neighbor.filename not in seen:
                    seen.add(neighbor.filename)
                    neighbors.append(neighbor)

    neighbors = sorted(neighbors, key=lambda item: item.score, reverse=True)
    return direct, neighbors


def search_lexical_memory(
    query: str,
    options: RecallOptions | None = None,
    *,
    atoms_dir: str = ATOMS_DIR,
    index_path: str = LEXICAL_INDEX_PATH,
) -> tuple[list[RecallResult], list[RecallResult]]:
    """Retrieve through the model-free sidecar, then apply the canonical trust filter."""
    options = options or RecallOptions()
    candidate_count = max(options.n_results, options.n_results * max(1, options.candidate_multiplier))
    candidates = search_lexical_index(query, index_path, n_results=candidate_count, atoms_dir=atoms_dir)

    direct = []
    for position, candidate in enumerate(candidates):
        current_content = verified_atom_content(candidate.filename, candidate.content_sha256, atoms_dir=atoms_dir)
        if current_content is None:
            continue
        # FTS5 rank is an ordering-specific BM25 value. Keep it separate from vector
        # distance and only map position to the existing display score contract.
        lexical_similarity = 1.0 / (position + 1)
        result = result_from_candidate(
            content=current_content,
            filename=candidate.filename,
            distance=None,
            options=options,
            source="lexical",
            atoms_dir=atoms_dir,
            similarity_override=lexical_similarity,
        )
        if result and candidate_has_query_overlap(query, current_content):
            direct.append(result)

    direct = sorted(direct, key=lambda item: item.score, reverse=True)[: options.n_results]
    neighbors = []
    seen = {item.filename for item in direct}
    if options.follow_links:
        for result in direct:
            for neighbor in linked_neighbors(result, options, atoms_dir=atoms_dir, query=query):
                if neighbor.filename not in seen:
                    seen.add(neighbor.filename)
                    neighbors.append(neighbor)
    return direct, sorted(neighbors, key=lambda item: item.score, reverse=True)


def search_hybrid_rrf_memory(
    query: str,
    options: RecallOptions | None = None,
    *,
    atoms_dir: str = ATOMS_DIR,
    k_constant: int = 60,
) -> tuple[list[RecallResult], list[RecallResult]]:
    """Reciprocal Rank Fusion (RRF) combining SQLite FTS5 (lexical) and Chroma (dense)."""
    options = options or RecallOptions()
    lex_direct, _ = search_lexical_memory(query, options, atoms_dir=atoms_dir)
    try:
        dense_direct, _ = search_semantic_memory(query, options)
    except Exception:
        # Graceful dynamic fallback to pure lexical when Ollama/Chroma is unavailable
        return lex_direct, []

    rrf_scores: dict[str, float] = {}
    candidate_map: dict[str, RecallResult] = {}

    for rank, res in enumerate(lex_direct):
        rrf_scores[res.filename] = rrf_scores.get(res.filename, 0.0) + (1.0 / (k_constant + rank + 1))
        candidate_map[res.filename] = res

    for rank, res in enumerate(dense_direct):
        rrf_scores[res.filename] = rrf_scores.get(res.filename, 0.0) + (1.0 / (k_constant + rank + 1))
        if res.filename not in candidate_map:
            candidate_map[res.filename] = res

    fused: list[RecallResult] = []
    for filename, rrf_score in sorted(rrf_scores.items(), key=lambda item: item[1], reverse=True)[: options.n_results]:
        base = candidate_map[filename]
        fused.append(
            RecallResult(
                filename=base.filename,
                content=base.content,
                source="rrf_hybrid",
                score=round(rrf_score, 6),
                similarity=base.similarity,
                review_status=base.review_status,
                memory_type=base.memory_type,
                confidence=base.confidence,
                importance=base.importance,
                contexts=base.contexts,
                match_reasons=base.match_reasons + (f"rrf_score:{round(rrf_score, 6)}",),
            )
        )

    neighbors = []
    seen = {item.filename for item in fused}
    if options.follow_links:
        for result in fused:
            for neighbor in linked_neighbors(result, options, atoms_dir=atoms_dir, query=query):
                if neighbor.filename not in seen:
                    seen.add(neighbor.filename)
                    neighbors.append(neighbor)

    return fused, sorted(neighbors, key=lambda item: item.score, reverse=True)


def search_memory(query: str, options: RecallOptions | None = None) -> tuple[list[RecallResult], list[RecallResult]]:
    """Retrieve memories using configured mode ('auto', 'rrf', 'lexical', or 'dense')."""
    options = options or RecallOptions()
    if options.retrieval_mode == "rrf":
        return search_hybrid_rrf_memory(query, options)
    if options.retrieval_mode == "dense":
        try:
            return search_semantic_memory(query, options)
        except MemoryRecallUnavailable:
            # Dynamic fallback to lexical if dense unavailable
            return search_lexical_memory(query, options)
    if options.retrieval_mode == "lexical":
        return search_lexical_memory(query, options)

    # auto mode: use lexical if sidecar exists, else dense
    if os.path.exists(LEXICAL_INDEX_PATH):
        return search_lexical_memory(query, options)
    return search_semantic_memory(query, options)


def format_context(results: list[RecallResult]) -> str:
    parts = []
    for result in results:
        trust = (
            f"status={result.review_status}; type={result.memory_type}; "
            f"confidence={result.confidence:.2f}; importance={result.importance:.2f}; "
            f"score={result.score:.3f}"
        )
        parts.append(f"--- Source Atom: {result.filename} ({trust}) ---\n{result.content}")
    return "\n\n".join(parts)
