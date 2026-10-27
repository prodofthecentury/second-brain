# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = [
#     "ollama",
# ]
# ///

import json
import hashlib
import math
import os
import urllib.error
import urllib.request

import ollama

from config import EMBED_MODEL, GEN_MODEL


class EmbeddingUnavailableError(RuntimeError):
    """Raised when Ollama cannot provide a valid embedding for a request."""


def ollama_base_url() -> str:
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    return host


def installed_model_names() -> list[str]:
    try:
        with urllib.request.urlopen(f"{ollama_base_url()}/api/tags", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        payload = {"models": []}

    names = []
    for model in payload.get("models", []):
        name = model.get("name") or model.get("model")
        if name:
            names.append(name)
    return names


def generation_candidates(preferred: str | None = None) -> list[str]:
    installed = installed_model_names()
    installed_bases = {name.split(":", 1)[0]: name for name in installed}
    configured = preferred or GEN_MODEL
    fallback_env = os.environ.get("BRAIN_FALLBACK_MODELS", "")
    requested = [
        configured,
        *[item.strip() for item in fallback_env.split(",") if item.strip()],
        "qwen2.5-coder:7b",
        "qwen3.6:latest",
        "llama3.2:3b",
        "mistral",
        "phi3",
    ]

    candidates = []
    for name in requested:
        if not name or name in candidates:
            continue
        if name in installed:
            candidates.append(name)
            continue
        base = name.split(":", 1)[0]
        if base in installed_bases and installed_bases[base] not in candidates:
            candidates.append(installed_bases[base])
    return candidates or [configured]


def chat_with_fallback(messages: list[dict], model: str | None = None, **kwargs):
    errors = []
    for candidate in generation_candidates(model):
        try:
            if candidate != (model or GEN_MODEL):
                print(f"[*] Trying fallback generation model: {candidate}")
            return ollama.chat(model=candidate, messages=messages, **kwargs)
        except Exception as exc:
            errors.append(f"{candidate}: {exc}")
            print(f"[!] Generation model failed: {candidate} ({exc})")

    raise RuntimeError("All generation models failed: " + " | ".join(errors))


def embed_text(text: str, model: str | None = None) -> list[float]:
    """Return one validated embedding or fail without substituting a fake vector."""
    model_name = model or EMBED_MODEL
    try:
        response = ollama.embed(model=model_name, input=text)
        embeddings = response["embeddings"]
        embedding = embeddings[0]
    except Exception as exc:
        raise EmbeddingUnavailableError(
            f"Embedding model {model_name!r} could not provide an embedding: {exc}"
        ) from exc

    if (
        not isinstance(embedding, list)
        or not embedding
        or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in embedding)
    ):
        raise EmbeddingUnavailableError(
            f"Embedding model {model_name!r} returned an invalid embedding response."
        )
    return [float(value) for value in embedding]


def embedding_metadata(
    filename: str,
    content: str,
    model: str,
    embedding: list[float],
    content_sha256: str | None = None,
) -> dict[str, str | int]:
    """Describe the vector provenance needed to safely decide whether it is queryable."""
    return {
        "filename": filename,
        "embedding_model": model,
        "embedding_dimensions": len(embedding),
        "content_sha256": content_sha256 or hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
