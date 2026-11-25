# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#     "ollama",
# ]
# ///

import hashlib
import inspect
import math
import os
import sys
import types

try:
    import ollama
except ImportError:  # Clean minimal wheel without optional [online]/[dense] extra
    ollama = types.SimpleNamespace(chat=None, embed=None, Client=None)  # type: ignore[assignment]

from scripts.config import EMBED_MODEL, GEN_MODEL
from scripts.hardware_profile import (
    call_ollama_chat_maybe_no_think,
    detect_hardware_profile,
    fetch_loopback_json,
    is_reasoning_model_name,
    loopback_ollama_base_url,
    recommended_ollama_runtime_options,
    resolve_active_local_generation_model,
    strip_reasoning_think_blocks,
)
import scripts.host_adapters as _host_adapters
from scripts.host_adapters import (
    HostConsentError,
    HostSharingRule,
    OutboundSecretBlockedError,
    _redact_outbound_with_map,
    builtin_default_policies,
    default_host_policy_path,
    validate_endpoint_network_boundary,
)

_DEFAULT_OLLAMA_CHAT = ollama.chat
_DEFAULT_OLLAMA_EMBED = ollama.embed


class EmbeddingUnavailableError(RuntimeError):
    """Raised when Ollama cannot provide a valid embedding for a request."""


def load_host_sharing_policy(policy_path=None) -> dict[str, HostSharingRule]:
    """Load configured host sharing policies (delegating dynamically to host_adapters.load_host_policies)."""
    return _host_adapters.load_host_policies(policy_path)


def _is_monkeypatched_ollama_fn(fn, default_fn) -> bool:
    if fn is None:
        return False
    return fn is not default_fn or not getattr(fn, "__module__", "").startswith("ollama")


def _resolve_local_ollama_rule_and_url() -> tuple[HostSharingRule, str]:
    policy_path = default_host_policy_path()
    if os.path.lexists(str(policy_path)):
        policies = load_host_sharing_policy(policy_path)
    else:
        policies = builtin_default_policies()

    rule = policies.get("ollama_local")
    if rule is None or not rule.enabled:
        raise HostConsentError("Local Ollama host 'ollama_local' is disabled by host sharing policy.")

    env_host = (os.environ.get("OLLAMA_HOST") or "").strip()
    raw_endpoint = (env_host or rule.endpoint_url or "http://127.0.0.1:11434").strip().rstrip("/")
    candidate_url = raw_endpoint if "://" in raw_endpoint else f"http://{raw_endpoint}"

    effective_rule = HostSharingRule(
        host_id=rule.host_id,
        provider="ollama",
        transport="local",
        endpoint_url=candidate_url,
        default_model=rule.default_model,
        api_key_env=rule.api_key_env,
        enabled=rule.enabled,
        allowed_projects=rule.allowed_projects,
        allow_unscoped_atoms=rule.allow_unscoped_atoms,
        allow_preference_atoms=rule.allow_preference_atoms,
        redact_pii=rule.redact_pii,
        fail_on_secret_detection=rule.fail_on_secret_detection,
    )
    validate_endpoint_network_boundary(effective_rule)
    canonical_loopback = loopback_ollama_base_url(candidate_url)
    if not canonical_loopback:
        raise HostConsentError(
            f"OLLAMA_HOST must point strictly to a local HTTP loopback address (127.0.0.1, localhost, or [::1]); got {raw_endpoint!r}."
        )
    return effective_rule, canonical_loopback


def ollama_base_url() -> str:
    _, validated = _resolve_local_ollama_rule_and_url()
    return validated


def installed_model_names() -> list[str]:
    try:
        _, base = _resolve_local_ollama_rule_and_url()
    except (HostConsentError, PermissionError, ValueError):
        return []
    payload = fetch_loopback_json(f"{base}/api/tags", timeout=5.0, max_bytes=65536) if base else None
    if not isinstance(payload, dict):
        payload = {"models": []}

    names = []
    for model in payload.get("models", []):
        if not isinstance(model, dict):
            continue
        name = model.get("name") or model.get("model")
        if name:
            names.append(str(name))
    return names


def _allow_heavy_qwen36_fallback(
    preferred: str | None,
    configured: str,
    fallback_items: list[str],
) -> bool:
    pref_norm = (preferred or "").strip().lower()
    if pref_norm == "qwen3.6:latest" or pref_norm.startswith("qwen3.6"):
        return True
    if "qwen3.6" in (configured or "").lower() or "qwen3.6" in (GEN_MODEL or "").lower():
        return True
    if any("qwen3.6" in item.lower() for item in fallback_items):
        return True
    try:
        sig = inspect.signature(detect_hardware_profile)
        if "probe_ollama" in sig.parameters:
            hw = detect_hardware_profile(probe_ollama=False)
        else:
            hw = detect_hardware_profile(use_cache=True)
    except TypeError:
        try:
            hw = detect_hardware_profile()
        except Exception:
            hw = None
    except Exception:
        hw = None
    if hw is not None:
        ram_avail = float(
            getattr(hw, "ram_available_gb", None)
            or getattr(hw, "available_ram_gb", None)
            or getattr(hw, "available_ram_gib", 0.0)
            or 0.0
        )
        vram = float(getattr(hw, "vram_gb", None) or getattr(hw, "vram_gib", 0.0) or 0.0)
        if ram_avail >= 28.0 or vram >= 20.0:
            return True
    return False


def generation_candidates(preferred: str | None = None) -> list[str]:
    from scripts.hardware_profile import _model_fits_host_memory, detect_hardware_profile, probe_ollama_models_with_sizes

    installed = installed_model_names()
    try:
        hw = detect_hardware_profile(use_cache=True)
        _, _, model_sizes = probe_ollama_models_with_sizes(timeout=0.8)
        total_ram = float(getattr(hw, "total_ram_gib", 0.0) or 0.0)
        gpu_backend = str(getattr(hw, "gpu_backend", "cpu") or "cpu")
        vram_gib = float(getattr(hw, "vram_gib", 0.0) or 0.0)
    except Exception:
        total_ram, gpu_backend, vram_gib, model_sizes = 0.0, "cpu", 0.0, {}

    def _fits(m_name: str) -> bool:
        return _model_fits_host_memory(
            m_name,
            total_ram_gib=total_ram,
            gpu_backend=gpu_backend,
            vram_gib=vram_gib,
            model_sizes_bytes=model_sizes,
        )

    installed_by_base: dict[str, list[str]] = {}
    for m_name in installed:
        installed_by_base.setdefault(m_name.split(":", 1)[0], []).append(m_name)

    explicit_env = (os.environ.get("BRAIN_GEN_MODEL") or "").strip()
    pref_clean = (preferred or "").strip()
    has_explicit_pin = bool(
        (pref_clean and pref_clean.lower() != "auto")
        or (explicit_env and explicit_env.lower() != "auto")
    )
    if pref_clean and pref_clean.lower() != "auto":
        configured = pref_clean
    else:
        configured = resolve_active_local_generation_model(fallback=GEN_MODEL)
    fallback_env = os.environ.get("BRAIN_FALLBACK_MODELS", "")
    fallback_items = [item.strip() for item in fallback_env.split(",") if item.strip()]
    explicit_allowed = {configured, *fallback_items} if has_explicit_pin else set(fallback_items)

    auto_fallbacks = ["qwen3-coder:8b", "qwen3:8b", "qwen2.5-coder:7b"]
    if _allow_heavy_qwen36_fallback(preferred, configured, fallback_items):
        auto_fallbacks.append("qwen3.6:latest")
    auto_fallbacks.extend(["qwen3:4b", "llama3.2:3b", "mistral", "phi3"])
    requested = [
        configured,
        *fallback_items,
        *auto_fallbacks,
    ]

    candidates: list[str] = []
    for name in requested:
        if not name or name in candidates:
            continue
        is_pinned = name in explicit_allowed
        if name in installed:
            if is_pinned or _fits(name):
                candidates.append(name)
            continue
        if f"{name}:latest" in installed and ":" not in name:
            cand_latest = f"{name}:latest"
            if cand_latest not in candidates and (is_pinned or _fits(cand_latest)):
                candidates.append(cand_latest)
            continue
        # Only bare family names (no explicit ':tag') may match an installed variant, and only if RAM-fit passes
        if ":" not in name and name in installed_by_base:
            for matched in installed_by_base[name]:
                if matched not in candidates and (is_pinned or _fits(matched)):
                    candidates.append(matched)
                    break
    return candidates or [configured]


def _sanitize_messages_for_rule(messages: list[dict], rule: HostSharingRule) -> list[dict]:
    if not (rule.redact_pii or rule.fail_on_secret_detection):
        return messages
    sanitized: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), str):
            sanitized.append(msg)
            continue
        report, _ = _redact_outbound_with_map(
            msg["content"],
            redact_pii=rule.redact_pii,
            fail_on_secret_detection=rule.fail_on_secret_detection,
        )
        sanitized.append({**msg, "content": report.sanitized_text})
    return sanitized


def chat_with_fallback(messages: list[dict], model: str | None = None, **kwargs):
    rule, base_url = _resolve_local_ollama_rule_and_url()
    outbound_messages = _sanitize_messages_for_rule(messages, rule)
    errors = []
    effective_target = (model or "").strip()
    if not effective_target or effective_target.lower() == "auto":
        effective_target = resolve_active_local_generation_model(fallback=rule.default_model or GEN_MODEL)
    for candidate in generation_candidates(model):
        try:
            if candidate != effective_target:
                print(f"[*] Trying fallback generation model: {candidate}")
            call_kwargs = {k: v for k, v in kwargs.items() if k != "timeout"}
            if "options" not in call_kwargs:
                try:
                    hw_cached = detect_hardware_profile(use_cache=True)
                except Exception:
                    hw_cached = None
                runtime_opts = recommended_ollama_runtime_options(hw_cached, model_name=candidate)
                if not _is_monkeypatched_ollama_fn(ollama.chat, _DEFAULT_OLLAMA_CHAT):
                    call_kwargs["options"] = runtime_opts
                else:
                    try:
                        sig = inspect.signature(ollama.chat)
                        if "options" in sig.parameters or any(
                            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                        ):
                            call_kwargs["options"] = runtime_opts
                    except (TypeError, ValueError):
                        pass
            if _is_monkeypatched_ollama_fn(ollama.chat, _DEFAULT_OLLAMA_CHAT):
                resp = call_ollama_chat_maybe_no_think(
                    ollama.chat, model=candidate, messages=outbound_messages, **call_kwargs
                )
            else:
                if getattr(ollama, "Client", None) is None:
                    raise RuntimeError(
                        "Python 'ollama' package is not installed; install 'second-brain[online]' for local model generation."
                    )
                timeout_arg = kwargs.get("timeout")
                env_timeout = os.environ.get("BRAIN_OLLAMA_TIMEOUT")
                if env_timeout:
                    try:
                        timeout_val = float(env_timeout)
                    except ValueError:
                        timeout_val = 360.0 if is_reasoning_model_name(candidate) else 360.0
                elif timeout_arg is not None:
                    timeout_val = float(timeout_arg)
                else:
                    timeout_val = 360.0 if is_reasoning_model_name(candidate) else 360.0
                client = ollama.Client(
                    host=base_url,
                    timeout=timeout_val,
                    follow_redirects=False,
                    trust_env=False,
                )
                call_kwargs.setdefault("stream", True)
                resp = call_ollama_chat_maybe_no_think(
                    client.chat, model=candidate, messages=outbound_messages, **call_kwargs
                )
            if hasattr(resp, "__iter__") and not isinstance(resp, (dict, list, str, bytes)):
                chunks = []
                last_chunk = {}
                for chunk in resp:
                    last_chunk = chunk if isinstance(chunk, dict) else getattr(chunk, "__dict__", {})
                    if isinstance(chunk, dict):
                        text_piece = chunk.get("message", {}).get("content", "")
                    else:
                        msg = getattr(chunk, "message", None)
                        text_piece = getattr(msg, "content", "") if msg else ""
                    if text_piece:
                        chunks.append(text_piece)
                full_content = strip_reasoning_think_blocks("".join(chunks))
                return {
                    **(last_chunk if isinstance(last_chunk, dict) else {}),
                    "message": {
                        **(last_chunk.get("message") if isinstance(last_chunk, dict) and isinstance(last_chunk.get("message"), dict) else {}),
                        "content": full_content,
                    },
                }
            if isinstance(resp, dict) and isinstance(resp.get("message"), dict) and isinstance(resp["message"].get("content"), str):
                return {
                    **resp,
                    "message": {
                        **resp["message"],
                        "content": strip_reasoning_think_blocks(resp["message"]["content"]),
                    },
                }
            msg_obj = getattr(resp, "message", None)
            if msg_obj is not None and isinstance(getattr(msg_obj, "content", None), str):
                try:
                    msg_obj.content = strip_reasoning_think_blocks(msg_obj.content)
                except Exception:
                    pass
            return resp
        except (HostConsentError, OutboundSecretBlockedError, PermissionError):
            raise
        except Exception as exc:
            errors.append(f"{candidate}: {exc}")
            print(f"[!] Generation model failed: {candidate} ({exc})")

    raise RuntimeError("All generation models failed: " + " | ".join(errors))


def embed_text(text: str, model: str | None = None) -> list[float]:
    """Return one validated embedding or fail without substituting a fake vector."""
    model_name = model or EMBED_MODEL
    rule, base_url = _resolve_local_ollama_rule_and_url()
    outbound_text = text
    if rule.redact_pii or rule.fail_on_secret_detection:
        report, _ = _redact_outbound_with_map(
            text,
            redact_pii=rule.redact_pii,
            fail_on_secret_detection=rule.fail_on_secret_detection,
        )
        outbound_text = report.sanitized_text

    try:
        if _is_monkeypatched_ollama_fn(ollama.embed, _DEFAULT_OLLAMA_EMBED):
            response = ollama.embed(model=model_name, input=outbound_text)
        else:
            if getattr(ollama, "Client", None) is None:
                raise EmbeddingUnavailableError(
                    "Python 'ollama' package is not installed; install 'second-brain[dense]' for dense embeddings."
                )
            client = ollama.Client(
                host=base_url,
                timeout=60.0,
                follow_redirects=False,
                trust_env=False,
            )
            response = client.embed(model=model_name, input=outbound_text)
        embeddings = response["embeddings"]
        embedding = embeddings[0]
    except (HostConsentError, OutboundSecretBlockedError, PermissionError):
        raise
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
