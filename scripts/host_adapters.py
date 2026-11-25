# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#     "httpx>=0.28,<1",
#     "ollama>=0.6,<1",
# ]
# ///
"""Per-host local, online, and owner-built AI model adapters with explicit consent and outbound redaction."""



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
    _sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__")
    raise SystemExit(0)


import argparse
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
from typing import TYPE_CHECKING, Any
from urllib.parse import quote as url_quote, urlsplit

if TYPE_CHECKING:
    import httpx as _httpx_mod
    from scripts.ollama_scout import PinnedHTTPTransport as _PinnedHTTPTransportType
    from scripts.ollama_scout import ValidatedRemoteTarget

if __name__ in ("host_adapters", "scripts.host_adapters"):
    sys.modules.setdefault("host_adapters", sys.modules[__name__])
    sys.modules.setdefault("scripts.host_adapters", sys.modules[__name__])

from scripts.answer_policy import (
    ANSWER_DRAFT_SCHEMA,
    AnswerRequest,
    AnswerResult,
    NO_APPROVED_MEMORY_RESPONSE,
    UnverifiedLead,
    build_grounding_prompt,
    collect_unverified_leads,
    format_unverified_leads_section,
    validate_standard_answer,
)
from scripts.config import (
    ATOMS_DIR,
    BRAIN_DIR,
    GEN_MODEL,
    _is_reparse_or_link,
    resolve_module_vault_path,
    snapshot_module_defaults,
)

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())
from scripts.evidence import ANCHOR_FIELDS
from scripts.hardware_profile import (
    call_ollama_chat_maybe_no_think,
    is_reasoning_model_name,
    loopback_ollama_base_url,
    resolve_active_local_generation_model,
    strip_reasoning_think_blocks,
)
from scripts.memory_format import split_frontmatter
from scripts.memory_review import parse_frontmatter_values, parse_scalar_list, section_text
from scripts.recall import normalize_project_tag, project_tags_match
from scripts.security_fs import (
    MAX_POLICY_FILE_BYTES,
    _current_windows_principal,
    read_bounded_security_file,
    restrict_security_file_permissions,
)

# Lazily loaded on first HTTP/online call so importing host_adapters stays lightweight;
# kept as module attributes for backwards-compatible unittest.mock.patch targets.
httpx: Any = None
PinnedHTTPTransport: Any = None


def _get_httpx():
    global httpx
    if httpx is None:
        import httpx as _httpx

        httpx = _httpx
    return httpx


def _get_pinned_http_transport():
    global PinnedHTTPTransport
    if PinnedHTTPTransport is None:
        from scripts.ollama_scout import PinnedHTTPTransport as _PinnedHTTPTransport

        PinnedHTTPTransport = _PinnedHTTPTransport
    return PinnedHTTPTransport


OUTBOUND_AUDIT_FILENAME = "outbound-host-audit.jsonl"
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_PII_PLACEHOLDER_TOKEN_RE = re.compile(r"\[REDACTED_PII:[a-z0-9_]+:\d+\]")
_AUDIT_LOCK = threading.Lock()

# Deterministic secret & PII patterns for outbound context gating
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai_api_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("github_pat", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_sts_key", re.compile(r"\bASIA[0-9A-Z]{16}\b")),
    ("jwt_token", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("stripe_live_key", re.compile(r"\b(?:sk|rk)_live_[0-9a-zA-Z]{24,}\b")),
    ("url_basic_auth", re.compile(r"https?://[^/\s:@\"']+:[^/\s@\"']+@")),
    ("private_key_pem", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("bearer_header_secret", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b")),
)

_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email_address", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("ipv4_private", re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")),
    ("ipv4_cgnat", re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b")),
    ("ipv4_link_local", re.compile(r"\b169\.254\.\d{1,3}\.\d{1,3}\b")),
    ("ipv6_ula", re.compile(r"\bfd[0-9a-fA-F]{2}:[0-9a-fA-F:]+(?:\b|(?<=:)(?![0-9a-fA-F:]))")),
    ("windows_user_path", re.compile(r"(?i)\b[A-Z]:(?:\\\\|\\)Users(?:\\\\|\\)[^\\\s\"']+")),
    ("posix_home_path", re.compile(r"(?<![A-Za-z0-9._-])/(?:home|Users)/[^/\s\"']+")),
)


class HostConsentError(PermissionError):
    """Raised when a target host lacks explicit owner sharing consent or violates scope rules."""


class OutboundSecretBlockedError(PermissionError):
    """Raised when outbound redaction detects high-risk credential material in payload."""


@dataclass(frozen=True)
class HostSharingRule:
    host_id: str
    provider: str  # "ollama" | "openai_compatible" | "anthropic" | "gemini"
    transport: str  # "local" | "online"
    endpoint_url: str
    default_model: str
    api_key_env: str | None = None
    enabled: bool = False
    allowed_projects: tuple[str, ...] = ()  # e.g. ("project:second_brain",) or ("*",)
    allow_unscoped_atoms: bool = False
    allow_preference_atoms: bool = False
    redact_pii: bool = True
    fail_on_secret_detection: bool = True
    owner_consent_recorded: bool | None = None

    def __post_init__(self) -> None:
        if self.owner_consent_recorded is None:
            object.__setattr__(self, "owner_consent_recorded", bool(self.enabled))


@dataclass(frozen=True)
class RedactionReport:
    sanitized_text: str
    secret_hits: tuple[str, ...]
    pii_redaction_count: int


def default_host_policy_path() -> Path:
    override = (os.environ.get("BRAIN_HOST_POLICY_FILE") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    elif os.environ.get("XDG_STATE_HOME"):
        base = Path(os.environ["XDG_STATE_HOME"]).expanduser()
    else:
        base = Path.home() / ".local" / "state"
    return base / "SecondBrain" / "security" / "host-sharing-policy.json"


def builtin_default_policies() -> dict[str, HostSharingRule]:
    """Fail-closed defaults: only local loopback Ollama is enabled out of the box."""
    raw_ollama = (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").strip().rstrip("/")
    normalized_loopback = loopback_ollama_base_url(raw_ollama)
    if normalized_loopback is not None:
        ollama_url = normalized_loopback
    else:
        ollama_url = raw_ollama if raw_ollama.startswith(("http://", "https://")) else f"http://{raw_ollama}"
    active_local_model = resolve_active_local_generation_model(fallback=GEN_MODEL)
    return {
        "ollama_local": HostSharingRule(
            host_id="ollama_local",
            provider="ollama",
            transport="local",
            endpoint_url=ollama_url,
            default_model=active_local_model,
            api_key_env=None,
            enabled=True,
            allowed_projects=("*",),
            allow_unscoped_atoms=True,
            allow_preference_atoms=True,
            redact_pii=False,
            fail_on_secret_detection=False,
        ),
        "openai_online": HostSharingRule(
            host_id="openai_online",
            provider="openai_compatible",
            transport="online",
            endpoint_url="https://api.openai.com/v1",
            default_model="gpt-4o-mini",
            api_key_env="OPENAI_API_KEY",
            enabled=False,
            allowed_projects=(),
            allow_unscoped_atoms=False,
            allow_preference_atoms=False,
            redact_pii=True,
            fail_on_secret_detection=True,
        ),
        "anthropic_online": HostSharingRule(
            host_id="anthropic_online",
            provider="anthropic",
            transport="online",
            endpoint_url="https://api.anthropic.com/v1",
            default_model="claude-3-5-haiku-latest",
            api_key_env="ANTHROPIC_API_KEY",
            enabled=False,
            allowed_projects=(),
            allow_unscoped_atoms=False,
            allow_preference_atoms=False,
            redact_pii=True,
            fail_on_secret_detection=True,
        ),
        "gemini_online": HostSharingRule(
            host_id="gemini_online",
            provider="gemini",
            transport="online",
            endpoint_url="https://generativelanguage.googleapis.com/v1beta",
            default_model="gemini-2.5-flash",
            api_key_env="GEMINI_API_KEY",
            enabled=False,
            allowed_projects=(),
            allow_unscoped_atoms=False,
            allow_preference_atoms=False,
            redact_pii=True,
            fail_on_secret_detection=True,
        ),
        "owner_custom_local": HostSharingRule(
            host_id="owner_custom_local",
            provider="openai_compatible",
            transport="local",
            endpoint_url="http://127.0.0.1:8080/v1",
            default_model="local-model",
            api_key_env="CUSTOM_HOST_API_KEY",
            enabled=False,
            allowed_projects=("*",),
            allow_unscoped_atoms=True,
            allow_preference_atoms=False,
            redact_pii=False,
            fail_on_secret_detection=True,
        ),
    }


def validate_model_identifier(model: str, *, provider: str | None = None) -> str:
    """Validate model identifier against URL/path injection and control characters."""
    cleaned = str(model or "").strip()
    if (
        not cleaned
        or ".." in cleaned
        or cleaned.startswith("/")
        or any(ch in cleaned for ch in ("?", "#", "%", "\\"))
        or not MODEL_ID_PATTERN.fullmatch(cleaned)
    ):
        raise ValueError(f"Invalid or unsafe model identifier: {model!r}")
    if provider == "gemini" and ("/" in cleaned or ":" in cleaned):
        raise ValueError(f"Invalid Gemini model identifier (path/action separators forbidden): {model!r}")
    return cleaned


def load_host_policies(policy_path: Path | None = None) -> dict[str, HostSharingRule]:
    policies = builtin_default_policies()
    target = policy_path or default_host_policy_path()
    if not os.path.lexists(str(target)):
        return policies
    raw = json.loads(read_bounded_security_file(target, max_bytes=MAX_POLICY_FILE_BYTES))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1 or not isinstance(raw.get("hosts"), dict):
        raise ValueError(f"Invalid host sharing policy schema in {target}")
    for host_id, entry in raw["hosts"].items():
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider", "")).strip()
        transport = str(entry.get("transport", "")).strip()
        if provider not in {"ollama", "openai_compatible", "anthropic", "gemini"}:
            raise ValueError(f"Unsupported provider '{provider}' for host '{host_id}'")
        if transport not in {"local", "online"}:
            raise ValueError(f"Unsupported transport '{transport}' for host '{host_id}'")
        raw_default_model = str(entry.get("default_model", "")).strip()
        if provider == "ollama" and transport == "local" and (not raw_default_model or raw_default_model.lower() == "auto"):
            raw_default_model = resolve_active_local_generation_model(fallback=GEN_MODEL)
        default_model = validate_model_identifier(raw_default_model, provider=provider)
        enabled_val = bool(entry.get("enabled", False))
        consent_val = (
            bool(entry["owner_consent_recorded"])
            if ("owner_consent_recorded" in entry and entry["owner_consent_recorded"] is not None)
            else enabled_val
        )
        policies[host_id] = HostSharingRule(
            host_id=host_id,
            provider=provider,
            transport=transport,
            endpoint_url=str(entry.get("endpoint_url", "")).strip().rstrip("/"),
            default_model=default_model,
            api_key_env=str(entry["api_key_env"]).strip() if entry.get("api_key_env") else None,
            enabled=enabled_val,
            allowed_projects=tuple(str(p).strip() for p in entry.get("allowed_projects", []) if str(p).strip()),
            allow_unscoped_atoms=bool(entry.get("allow_unscoped_atoms", False)),
            allow_preference_atoms=bool(entry.get("allow_preference_atoms", False)),
            redact_pii=True if transport == "online" else bool(entry.get("redact_pii", False)),
            fail_on_secret_detection=True if transport == "online" else bool(entry.get("fail_on_secret_detection", True)),
            owner_consent_recorded=consent_val,
        )
    return policies


def save_host_policies(policies: dict[str, HostSharingRule], policy_path: Path | None = None) -> Path:
    raw_target = (policy_path or default_host_policy_path()).expanduser()
    if os.path.lexists(str(raw_target)) and _is_reparse_or_link(str(raw_target)):
        raise PermissionError(f"Refusing to overwrite symlinked policy file: {raw_target}")
    if os.path.lexists(str(raw_target.parent)) and _is_reparse_or_link(str(raw_target.parent)):
        raise PermissionError(f"Refusing to write policy under symlinked directory: {raw_target.parent}")
    target = raw_target.resolve()
    if os.path.lexists(str(target)) and _is_reparse_or_link(str(target)):
        raise PermissionError(f"Refusing to overwrite symlinked policy file: {target}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _is_reparse_or_link(str(target.parent)):
        raise PermissionError(f"Refusing to write policy under symlinked directory: {target.parent}")
    payload = {
        "schema_version": 1,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "hosts": {
            hid: {
                **asdict(rule),
                "allowed_projects": list(rule.allowed_projects),
            }
            for hid, rule in sorted(policies.items())
        },
    }
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".host-policy-", suffix=".tmp")
    fd_closed = False
    try:
        if os.name != "nt":
            os.chmod(tmp_name, 0o600)
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if os.write(fd, encoded) != len(encoded):
            raise OSError("Incomplete host policy write.")
        os.fsync(fd)
        os.close(fd)
        fd_closed = True
        os.replace(tmp_name, target)
        restrict_security_file_permissions(target)
    finally:
        if not fd_closed:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass
    return target


BUILTIN_HOST_IDS = frozenset(
    {"ollama_local", "openai_online", "anthropic_online", "gemini_online", "owner_custom_local"}
)
_CUSTOM_HOST_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{2,47}$")
_API_KEY_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def create_custom_host_policy(
    host_id: str,
    *,
    provider: str,
    transport: str,
    endpoint_url: str,
    default_model: str,
    api_key_env: str | None = None,
    allowed_projects: tuple[str, ...] = ("*",),
    allow_unscoped_atoms: bool = False,
    allow_preference_atoms: bool = False,
    redact_pii: bool | None = None,
    fail_on_secret_detection: bool | None = None,
    enabled: bool = False,
    policy_path: Path | None = None,
) -> HostSharingRule:
    """Create and persist a validated user-defined AI model host profile in `host-sharing-policy.json`."""
    clean_id = str(host_id or "").strip().lower()
    if not _CUSTOM_HOST_ID_RE.fullmatch(clean_id):
        raise ValueError("Custom host_id must match ^[a-z][a-z0-9_-]{2,47}$.")
    if clean_id in BUILTIN_HOST_IDS:
        raise ValueError(f"Host ID '{clean_id}' is a built-in profile and cannot be re-created.")

    clean_prov = str(provider or "").strip()
    if clean_prov not in {"ollama", "openai_compatible", "anthropic", "gemini"}:
        raise ValueError(f"Unsupported provider '{clean_prov}'.")
    clean_transport = str(transport or "").strip()
    if clean_transport not in {"local", "online"}:
        raise ValueError(f"Unsupported transport '{clean_transport}'.")

    clean_model = validate_model_identifier(default_model, provider=clean_prov)
    clean_ep = str(endpoint_url or "").strip().rstrip("/")
    clean_key_env = str(api_key_env or "").strip() or None
    if clean_key_env and not _API_KEY_ENV_NAME_RE.fullmatch(clean_key_env):
        raise ValueError("api_key_env must match ^[A-Z][A-Z0-9_]{1,63}$ (never pass a raw secret).")

    rule = HostSharingRule(
        host_id=clean_id,
        provider=clean_prov,
        transport=clean_transport,
        endpoint_url=clean_ep,
        default_model=clean_model,
        api_key_env=clean_key_env,
        enabled=bool(enabled),
        allowed_projects=tuple(str(p).strip() for p in allowed_projects if str(p).strip()),
        allow_unscoped_atoms=bool(allow_unscoped_atoms),
        allow_preference_atoms=bool(allow_preference_atoms),
        redact_pii=True if clean_transport == "online" else bool(True if redact_pii is None else redact_pii),
        fail_on_secret_detection=True
        if clean_transport == "online"
        else bool(True if fail_on_secret_detection is None else fail_on_secret_detection),
        owner_consent_recorded=bool(enabled),
    )
    validate_endpoint_network_boundary(rule)
    policies = load_host_policies(policy_path)
    if clean_id in policies:
        raise ValueError(f"Custom host '{clean_id}' already exists.")
    policies[clean_id] = rule
    save_host_policies(policies, policy_path)
    return rule


def delete_custom_host_policy(host_id: str, *, policy_path: Path | None = None) -> bool:
    """Delete a user-created custom host profile while protecting built-in host profiles."""
    clean_id = str(host_id or "").strip().lower()
    if clean_id in BUILTIN_HOST_IDS:
        raise ValueError(f"Cannot delete built-in host profile '{clean_id}'.")
    policies = load_host_policies(policy_path)
    if clean_id not in policies:
        return False
    policies.pop(clean_id)
    save_host_policies(policies, policy_path)
    return True


LLAMA_CPP_SPECULATIVE_HOST_ID = "llama_cpp_speculative"


def register_speculative_host_policy(
    *,
    endpoint_url: str = "http://127.0.0.1:8081/v1",
    target_model: str = "speculative-local",
    enabled: bool = True,
    policy_path: Path | None = None,
) -> HostSharingRule:
    """Register or update the loopback llama.cpp speculative server host profile."""
    clean_model = validate_model_identifier(target_model, provider="openai_compatible")
    clean_ep = str(endpoint_url or "http://127.0.0.1:8081/v1").strip().rstrip("/")
    parsed = urlsplit(clean_ep)
    if parsed.scheme != "http" or (parsed.hostname or "").strip().lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise HostConsentError(
            f"Speculative llama-server endpoint must bind strictly to loopback HTTP; got {endpoint_url!r}."
        )
    rule = HostSharingRule(
        host_id=LLAMA_CPP_SPECULATIVE_HOST_ID,
        provider="openai_compatible",
        transport="local",
        endpoint_url=clean_ep,
        default_model=clean_model,
        api_key_env=None,
        enabled=bool(enabled),
        allowed_projects=("*",),
        allow_unscoped_atoms=True,
        allow_preference_atoms=True,
        redact_pii=False,
        fail_on_secret_detection=False,
        owner_consent_recorded=bool(enabled),
    )
    validate_endpoint_network_boundary(rule)
    policies = load_host_policies(policy_path)
    policies[LLAMA_CPP_SPECULATIVE_HOST_ID] = rule
    save_host_policies(policies, policy_path)
    return rule


def inspect_and_redact_outbound(
    text: str,
    *,
    redact_pii: bool = True,
    fail_on_secret_detection: bool = True,
) -> RedactionReport:
    """Scan outbound text for credentials and PII before any external transmission."""
    secret_hits: list[str] = []
    working = text
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(working):
            secret_hits.append(name)
            working = pattern.sub(f"[REDACTED_SECRET:{name}]", working)

    if secret_hits and fail_on_secret_detection:
        raise OutboundSecretBlockedError(
            f"Outbound transmission blocked: detected credential pattern(s) {secret_hits} in prompt payload."
        )

    pii_count = 0
    if redact_pii:
        for name, pattern in _PII_PATTERNS:
            working, count = pattern.subn(f"[REDACTED_PII:{name}]", working)
            pii_count += count

    return RedactionReport(
        sanitized_text=working,
        secret_hits=tuple(secret_hits),
        pii_redaction_count=pii_count,
    )


def _redact_outbound_with_map(
    text: str,
    *,
    redact_pii: bool = True,
    fail_on_secret_detection: bool = True,
) -> tuple[RedactionReport, dict[str, str]]:
    """Scan outbound text and emit deterministic per-call PII placeholders with a local-only rehydration map."""
    secret_hits: list[str] = []
    working = text
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(working):
            secret_hits.append(name)
            working = pattern.sub(f"[REDACTED_SECRET:{name}]", working)

    if secret_hits and fail_on_secret_detection:
        raise OutboundSecretBlockedError(
            f"Outbound transmission blocked: detected credential pattern(s) {secret_hits} in prompt payload."
        )

    pii_count = 0
    placeholder_to_original: dict[str, str] = {}
    value_to_placeholder: dict[tuple[str, str], str] = {}
    next_index = 1
    if redact_pii:
        for name, pattern in _PII_PATTERNS:
            def _replace_match(match: re.Match[str], kind: str = name) -> str:
                nonlocal pii_count, next_index
                pii_count += 1
                raw_val = match.group(0)
                canonical_val = raw_val.replace("\\\\", "\\") if kind == "windows_user_path" else raw_val
                key = (kind, canonical_val)
                placeholder = value_to_placeholder.get(key)
                if placeholder is None:
                    placeholder = f"[REDACTED_PII:{kind}:{next_index}]"
                    next_index += 1
                    value_to_placeholder[key] = placeholder
                    placeholder_to_original[placeholder] = canonical_val
                return placeholder

            working = pattern.sub(_replace_match, working)

    return (
        RedactionReport(
            sanitized_text=working,
            secret_hits=tuple(secret_hits),
            pii_redaction_count=pii_count,
        ),
        placeholder_to_original,
    )


def _rehydrate_text(text: str, placeholder_map: dict[str, str]) -> str:
    """Replace exact per-call [REDACTED_PII:<kind>:<N>] tokens locally."""
    if not placeholder_map or not isinstance(text, str) or "[REDACTED_PII:" not in text:
        return text
    return _PII_PLACEHOLDER_TOKEN_RE.sub(
        lambda m: placeholder_map.get(m.group(0), m.group(0)),
        text,
    )


def _rehydrate_draft_claims(draft_json: str, placeholder_map: dict[str, str]) -> str:
    """Replace exact per-call PII placeholder tokens inside parsed draft claims locally before grounding validation."""
    if not placeholder_map or not isinstance(draft_json, str) or len(draft_json) > 128_000:
        return draft_json
    try:
        payload = json.loads(draft_json)
    except (TypeError, json.JSONDecodeError):
        return draft_json
    if not isinstance(payload, dict) or not isinstance(payload.get("claims"), list):
        return draft_json

    changed = False
    rehydrated_claims: list[object] = []
    for item in payload["claims"]:
        if isinstance(item, str) and "[REDACTED_PII:" in item:
            updated = _rehydrate_text(item, placeholder_map)
            if updated != item:
                changed = True
            rehydrated_claims.append(updated)
        else:
            rehydrated_claims.append(item)
    if not changed:
        return draft_json
    return json.dumps({**payload, "claims": rehydrated_claims}, ensure_ascii=False)


def validate_endpoint_network_boundary(
    rule: HostSharingRule,
    *,
    skip_dns_resolution: bool = False,
) -> ValidatedRemoteTarget | None:
    """Enforce strict 127.0.0.1 loopback for local hosts and DNS-pinned HTTPS:443 public IPs for online hosts."""
    try:
        parsed = urlsplit(str(rule.endpoint_url or "").strip())
    except ValueError as exc:
        raise HostConsentError(f"Host '{rule.host_id}' has an invalid endpoint URL: {exc}") from exc

    hostname = (parsed.hostname or "").strip().lower()
    if not hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HostConsentError(
            f"Host '{rule.host_id}' has an invalid or credential-bearing endpoint URL: {rule.endpoint_url!r}"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise HostConsentError(f"Host '{rule.host_id}' has an invalid port: {exc}") from exc

    if rule.transport == "local":
        if parsed.scheme != "http" or hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise HostConsentError(
                f"Local host '{rule.host_id}' must bind strictly to loopback http://127.0.0.1, ::1, or localhost (got {rule.endpoint_url!r})."
            )
        return None

    # Online host: require HTTPS on port 443 and strictly public, DNS-pinned IP answers
    from scripts.ollama_scout import ValidatedRemoteTarget, _normalized_hostname, is_public_ip_address

    if parsed.scheme != "https":
        raise HostConsentError(
            f"Online host '{rule.host_id}' must use HTTPS transport (got {rule.endpoint_url!r})."
        )
    if port is not None and port != 443:
        raise HostConsentError(
            f"Online host '{rule.host_id}' must use standard HTTPS port 443 (got port {port})."
        )
    if hostname == "localhost":
        raise HostConsentError(f"SSRF blocked: online host '{hostname}' cannot target localhost.")
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not is_public_ip_address(str(literal_ip)):
        raise HostConsentError(
            f"SSRF blocked: online host '{hostname}' targets non-public IP {literal_ip}."
        )

    effective_port = 443
    if skip_dns_resolution:
        return ValidatedRemoteTarget(
            url=rule.endpoint_url.rstrip("/"),
            hostname=_normalized_hostname(hostname),
            port=effective_port,
            addresses=(str(literal_ip),) if literal_ip is not None else (),
        )

    try:
        addr_infos = socket.getaddrinfo(hostname, effective_port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise HostConsentError(f"DNS resolution failed for online host '{hostname}': {exc}") from exc
    if not addr_infos:
        raise HostConsentError(f"DNS resolution returned no addresses for '{hostname}'.")

    pinned_addresses: list[str] = []
    for info in addr_infos:
        ip_str = info[4][0]
        try:
            if not is_public_ip_address(str(ip_str)):
                raise HostConsentError(
                    f"SSRF blocked: online host '{hostname}' resolved to non-public IP {ip_str}."
                )
            normalized_ip = str(ipaddress.ip_address(str(ip_str)))
        except ValueError as exc:
            raise HostConsentError(
                f"Invalid IP address '{ip_str}' resolved for online host '{hostname}': {exc}"
            ) from exc
        if normalized_ip not in pinned_addresses:
            pinned_addresses.append(normalized_ip)
    if not pinned_addresses:
        raise HostConsentError(f"DNS resolution returned no public addresses for '{hostname}'.")

    return ValidatedRemoteTarget(
        url=rule.endpoint_url.rstrip("/"),
        hostname=_normalized_hostname(hostname),
        port=effective_port,
        addresses=tuple(pinned_addresses),
    )


def filter_atoms_for_host(
    retrieved_atoms: list,
    rule: HostSharingRule,
    *,
    mode: str | None = None,
    atoms_dir: str | Path | None = None,
    brain_dir: str | Path | None = None,
) -> list:
    """Filter retrieved atoms strictly to approved revisions, scopes, and epistemic contracts permitted for this host."""
    try:
        from .atom_mutator import approved_revision_is_current
    except ImportError:
        from scripts.atom_mutator import approved_revision_is_current

    atoms_root = Path(atoms_dir).resolve() if atoms_dir is not None else None
    brain_root = (
        Path(brain_dir).resolve()
        if brain_dir is not None
        else (atoms_root.parent if atoms_root is not None else None)
    )
    default_atoms_root = Path(ATOMS_DIR).resolve() if Path(ATOMS_DIR).is_dir() else None

    eligible = []
    allow_all_projects = any(str(p).strip() == "*" for p in rule.allowed_projects)
    normalized_allowed_projects = tuple(
        norm
        for p in rule.allowed_projects
        if str(p).strip() and str(p).strip() != "*"
        for norm in (normalize_project_tag(p),)
        if norm
    )

    for record in retrieved_atoms or []:
        content = record.get("content") if isinstance(record, dict) else getattr(record, "content", "")
        filename = record.get("filename") if isinstance(record, dict) else getattr(record, "filename", "")
        if not isinstance(content, str):
            continue
        parsed = split_frontmatter(content)
        if parsed is None:
            continue
        fm = parse_frontmatter_values(parsed[0])

        # Strictly require accepted + human_reviewed non-superseded atoms (unverified leads NEVER enter host scope).
        rev_status = str(fm.get("review_status") or "").strip()
        claim_state = str(fm.get("claim_state") or "").strip()
        rec_status = record.get("review_status") if isinstance(record, dict) else getattr(record, "review_status", None)
        if rev_status != "accepted" or claim_state != "human_reviewed":
            continue
        if rec_status is not None and str(rec_status).strip() != "accepted":
            continue
        sup_raw = fm.get("superseded_by") or []
        sup_list = parse_scalar_list(sup_raw) if isinstance(sup_raw, str) else list(sup_raw)
        if any(str(x).strip() for x in sup_list):
            continue
        if not all(fm.get(k) for k in ANCHOR_FIELDS):
            continue

        if not (isinstance(filename, str) and filename.endswith(".md") and Path(filename).name == filename):
            continue
        check_root = atoms_root
        check_brain = brain_root
        if check_root is None and default_atoms_root is not None and (default_atoms_root / filename).is_file():
            check_root = default_atoms_root
            check_brain = default_atoms_root.parent
        if check_root is not None:
            cand_path = (check_root / filename).resolve()
            if (
                cand_path.parent != check_root
                or not cand_path.is_file()
                or not approved_revision_is_current(
                    str(cand_path),
                    atoms_dir=str(check_root),
                    brain_dir=str(check_brain or check_root.parent),
                )
            ):
                continue
            try:
                if hashlib.sha256(cand_path.read_bytes()).hexdigest() != hashlib.sha256(content.encode("utf-8")).hexdigest():
                    continue
            except OSError:
                continue

        epistemic_label = str(fm.get("epistemic_label", "claim"))
        is_pref = epistemic_label == "preference" or bool(fm.get("epistemic_contract"))
        if is_pref and not rule.allow_preference_atoms:
            continue
        if mode == "preference" and not is_pref:
            continue
        if mode in {"standard", "research"} and is_pref:
            continue

        contexts_raw = fm.get("contexts") or []
        if isinstance(contexts_raw, str):
            contexts_raw = parse_scalar_list(contexts_raw)
        project_tags = [str(c).strip() for c in contexts_raw if str(c).strip().lower().startswith("project:")]

        if not project_tags:
            if not rule.allow_unscoped_atoms:
                continue
        elif not allow_all_projects:
            # Multi-project containment invariant: EVERY project tag on the atom must be permitted.
            if not normalized_allowed_projects or not all(
                bool(project_tags_match((tag,), normalized_allowed_projects))
                for tag in project_tags
            ):
                continue

        eligible.append(record)
    return eligible


def _audit_outbound_call(
    *,
    brain_dir: str,
    rule: HostSharingRule,
    model: str,
    atom_ids: list[str],
    pii_redactions: int,
    outcome: str,
) -> None:
    audit_path = os.path.join(brain_dir, ".sidecar", OUTBOUND_AUDIT_FILENAME)
    record = {
        "schema_version": 1,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "host_id": rule.host_id,
        "provider": rule.provider,
        "transport": rule.transport,
        "model": model,
        "shared_atom_count": len(atom_ids),
        "shared_atom_ids": atom_ids,
        "pii_redaction_count": pii_redactions,
        "outcome": outcome,
    }
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    with _AUDIT_LOCK:
        audit_parent = os.path.dirname(os.path.abspath(audit_path))
        os.makedirs(audit_parent, exist_ok=True)
        if _is_reparse_or_link(audit_parent) or (os.path.lexists(audit_path) and _is_reparse_or_link(audit_path)):
            raise PermissionError("Refusing to write outbound audit log through a symlink or reparse point.")
        existed_before = os.path.exists(audit_path)
        fd = os.open(audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if os.write(fd, line) != len(line):
                raise OSError("Incomplete write to outbound host audit log.")
            os.fsync(fd)
        finally:
            os.close(fd)
        if not existed_before:
            restrict_security_file_permissions(audit_path)


_GEMINI_TYPE_MAP = {
    "object": "OBJECT",
    "array": "ARRAY",
    "string": "STRING",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
}


def _to_gemini_response_schema(schema: dict) -> dict:
    """Convert a JSON Schema subset into Gemini's OpenAPI responseSchema format."""
    raw_type = str(schema.get("type", "object")).lower()
    gem: dict[str, object] = {"type": _GEMINI_TYPE_MAP.get(raw_type, raw_type.upper())}
    if isinstance(schema.get("description"), str):
        gem["description"] = schema["description"]
    if isinstance(schema.get("enum"), list):
        gem["enum"] = list(schema["enum"])
    if isinstance(schema.get("properties"), dict):
        gem["properties"] = {
            str(k): _to_gemini_response_schema(v)
            for k, v in schema["properties"].items()
            if isinstance(v, dict)
        }
    if isinstance(schema.get("required"), list):
        gem["required"] = [str(r) for r in schema["required"]]
    if isinstance(schema.get("items"), dict):
        gem["items"] = _to_gemini_response_schema(schema["items"])
    return gem


def _prepare_ollama_http_transport(
    rule: HostSharingRule,
    http_transport: _httpx_mod.BaseTransport | None,
    pinned_target: ValidatedRemoteTarget | None,
) -> tuple[_httpx_mod.BaseTransport | None, _PinnedHTTPTransportType | None, dict[str, str], str]:
    """Prepare headers and DNS-pinned transport for mock or online ``provider='ollama'`` rules."""
    api_key = (os.environ.get(rule.api_key_env) or "").strip() if rule.api_key_env else ""
    if rule.transport == "online" and rule.api_key_env and not api_key:
        raise HostConsentError(
            f"Missing API credential in environment variable '{rule.api_key_env}' for online host '{rule.host_id}'."
        )
    owned_transport: _PinnedHTTPTransportType | None = None
    eff_transport = http_transport
    if eff_transport is None and rule.transport == "online":
        resolved_t = pinned_target if pinned_target is not None else validate_endpoint_network_boundary(rule)
        if resolved_t is not None:
            owned_transport = _get_pinned_http_transport()(resolved_t)
            eff_transport = owned_transport
    target_host = (loopback_ollama_base_url(rule.endpoint_url) or rule.endpoint_url).rstrip("/")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return eff_transport, owned_transport, headers, target_host


def _invoke_provider_draft(
    rule: HostSharingRule,
    *,
    model: str,
    prompt: str,
    timeout_s: float | None = None,
    http_transport: _httpx_mod.BaseTransport | None = None,
    pinned_target: ValidatedRemoteTarget | None = None,
    json_schema: dict | None = None,
    schema_name: str = "grounded_claim_draft",
    gemini_response_schema: dict | None = None,
) -> str:
    """Invoke the configured model endpoint and return raw structured JSON text."""
    if not rule.enabled or (rule.transport != "local" and not rule.owner_consent_recorded):
        raise HostConsentError(f"Host policy '{rule.host_id}' is disabled or lacks owner consent.")
    effective_schema = json_schema or ANSWER_DRAFT_SCHEMA
    effective_timeout_s = float(timeout_s) if timeout_s is not None else (120.0 if rule.provider == "ollama" else 45.0)
    safe_model = validate_model_identifier(model, provider=rule.provider)
    if rule.provider == "ollama":
        if http_transport is not None or rule.transport == "online":
            httpx_mod = _get_httpx()
            eff_transport, owned_ollama_transport, ollama_headers, target_host = _prepare_ollama_http_transport(
                rule, http_transport, pinned_target
            )
            req_json: dict[str, object] = {
                "model": safe_model,
                "messages": [{"role": "user", "content": prompt}],
                "format": effective_schema,
                "stream": False,
                "options": {"temperature": 0},
            }
            if is_reasoning_model_name(safe_model):
                req_json["think"] = False
            ollama_kwargs: dict[str, object] = {
                "timeout": effective_timeout_s,
                "follow_redirects": False,
                "trust_env": False,
            }
            if eff_transport is not None:
                ollama_kwargs["transport"] = eff_transport
            entered_ollama = False
            try:
                with httpx_mod.Client(**ollama_kwargs) as client:
                    entered_ollama = True
                    resp = client.post(
                        f"{target_host}/api/chat",
                        headers=ollama_headers,
                        json=req_json,
                    )
                    if 300 <= resp.status_code < 400:
                        raise HostConsentError("HTTP redirects are forbidden on model endpoints.")
                    resp.raise_for_status()
                    data = resp.json()
                    raw_txt = str((data.get("message") or {}).get("content") or data.get("response") or "{}")
                    return strip_reasoning_think_blocks(raw_txt) or "{}"
            finally:
                if owned_ollama_transport is not None and not entered_ollama:
                    owned_ollama_transport.close()

        import ollama

        target_host = loopback_ollama_base_url(rule.endpoint_url) or rule.endpoint_url
        client = ollama.Client(
            host=target_host,
            timeout=effective_timeout_s,
            follow_redirects=False,
            trust_env=False,
        )
        resp = call_ollama_chat_maybe_no_think(
            client.chat,
            model=safe_model,
            messages=[{"role": "user", "content": prompt}],
            format=effective_schema,
            options={"temperature": 0},
        )
        return strip_reasoning_think_blocks(str(resp["message"]["content"])) or "{}"

    api_key = ""
    if rule.api_key_env:
        api_key = (os.environ.get(rule.api_key_env) or "").strip()
    if rule.transport == "online" and not api_key:
        raise HostConsentError(
            f"Missing API credential in environment variable '{rule.api_key_env}' for online host '{rule.host_id}'."
        )

    owned_pinned_transport: _PinnedHTTPTransportType | None = None
    effective_transport = http_transport
    if effective_transport is None and rule.transport == "online":
        resolved_target = pinned_target if pinned_target is not None else validate_endpoint_network_boundary(rule)
        if resolved_target is not None:
            owned_pinned_transport = _get_pinned_http_transport()(resolved_target)
            effective_transport = owned_pinned_transport

    client_kwargs: dict[str, object] = {
        "timeout": effective_timeout_s,
        "follow_redirects": False,
        "trust_env": False,
    }
    if effective_transport is not None:
        client_kwargs["transport"] = effective_transport

    httpx_mod = _get_httpx()
    client_entered = False
    try:
        with httpx_mod.Client(**client_kwargs) as client:
            client_entered = True
            if rule.provider == "openai_compatible":
                headers = {"Content-Type": "application/json"}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                body = {
                    "model": safe_model,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema_name,
                            "strict": True,
                            "schema": effective_schema,
                        },
                    },
                }
                target_base = (
                    rule.endpoint_url.replace("://localhost:", "://127.0.0.1:").replace("://localhost/", "://127.0.0.1/")
                    if rule.transport == "local"
                    else rule.endpoint_url
                )
                if rule.transport == "local" and target_base.endswith("://localhost"):
                    target_base = target_base[: -len("localhost")] + "127.0.0.1"
                resp = client.post(f"{target_base}/chat/completions", headers=headers, json=body)
                if 300 <= resp.status_code < 400:
                    raise HostConsentError("HTTP redirects are forbidden on model endpoints.")
                resp.raise_for_status()
                data = resp.json()
                return strip_reasoning_think_blocks(str(data["choices"][0]["message"]["content"])) or "{}"

            if rule.provider == "anthropic":
                tool_name = "emit_grounded_claims" if json_schema is None else schema_name
                headers = {
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                }
                body = {
                    "model": safe_model,
                    "max_tokens": 1024,
                    "temperature": 0,
                    "tools": [
                        {
                            "name": tool_name,
                            "description": "Emit structured JSON strictly matching the schema.",
                            "input_schema": effective_schema,
                        }
                    ],
                    "tool_choice": {"type": "tool", "name": tool_name},
                    "messages": [{"role": "user", "content": prompt}],
                }
                resp = client.post(f"{rule.endpoint_url}/messages", headers=headers, json=body)
                if 300 <= resp.status_code < 400:
                    raise HostConsentError("HTTP redirects are forbidden on model endpoints.")
                resp.raise_for_status()
                data = resp.json()
                for block in data.get("content", []):
                    if block.get("type") == "tool_use" and block.get("name") == tool_name:
                        return json.dumps(block.get("input", {}))
                return "{}"

            if rule.provider == "gemini":
                gem_schema = gemini_response_schema or (
                    _to_gemini_response_schema(json_schema)
                    if json_schema is not None
                    else {
                        "type": "OBJECT",
                        "properties": {
                            "claims": {
                                "type": "ARRAY",
                                "items": {"type": "STRING"},
                            }
                        },
                        "required": ["claims"],
                    }
                )
                headers = {
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key,
                }
                body = {
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0,
                        "responseMimeType": "application/json",
                        "responseSchema": gem_schema,
                    },
                }
                encoded_model = url_quote(safe_model, safe="._-")
                resp = client.post(
                    f"{rule.endpoint_url}/models/{encoded_model}:generateContent",
                    headers=headers,
                    json=body,
                )
                if 300 <= resp.status_code < 400:
                    raise HostConsentError("HTTP redirects are forbidden on model endpoints.")
                resp.raise_for_status()
                data = resp.json()
                parts = data["candidates"][0]["content"]["parts"]
                return str(parts[0]["text"])
    finally:
        if owned_pinned_transport is not None and not client_entered:
            owned_pinned_transport.close()

    raise ValueError(f"Unsupported provider: {rule.provider}")


def invoke_provider_stream(
    *,
    rule: HostSharingRule,
    target: ValidatedRemoteTarget | None = None,
    model: str,
    system_prompt: str = "",
    user_prompt: str,
    http_transport: _httpx_mod.BaseTransport | None = None,
    timeout_s: float | None = None,
) -> Iterator[str]:
    """Stream text chunks across Ollama, OpenAI-compatible, Anthropic, and Gemini endpoints."""
    if not rule.enabled or (rule.transport != "local" and not rule.owner_consent_recorded):
        raise HostConsentError(f"Host policy '{rule.host_id}' is disabled or lacks owner consent.")
    effective_timeout_s = float(timeout_s) if timeout_s is not None else (120.0 if rule.provider == "ollama" else 60.0)
    safe_model = validate_model_identifier(model, provider=rule.provider)
    messages: list[dict[str, str]] = []
    if system_prompt and system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    if rule.provider == "ollama":
        if http_transport is None and rule.transport != "online":
            target_host = (loopback_ollama_base_url(rule.endpoint_url) or rule.endpoint_url).rstrip("/")
            import ollama

            client = ollama.Client(
                host=target_host,
                timeout=effective_timeout_s,
                follow_redirects=False,
                trust_env=False,
            )
            stream_iter = call_ollama_chat_maybe_no_think(
                client.chat,
                model=safe_model,
                messages=messages,
                stream=True,
                options={"temperature": 0.2, "num_predict": 512},
            )
            for part in stream_iter:
                msg = part.get("message") if isinstance(part, dict) else getattr(part, "message", None)
                piece = (msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")) or ""
                if piece:
                    yield str(piece)
            return

        httpx_mod = _get_httpx()
        eff_transport, owned_ollama_transport, ollama_headers, target_host = _prepare_ollama_http_transport(
            rule, http_transport, target
        )
        stream_body: dict[str, object] = {
            "model": safe_model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": 0.2, "num_predict": 512},
        }
        if is_reasoning_model_name(safe_model):
            stream_body["think"] = False
        ollama_kwargs: dict[str, object] = {
            "timeout": effective_timeout_s,
            "follow_redirects": False,
            "trust_env": False,
        }
        if eff_transport is not None:
            ollama_kwargs["transport"] = eff_transport
        entered_ollama = False
        try:
            with httpx_mod.Client(**ollama_kwargs) as client:
                entered_ollama = True
                with client.stream(
                    "POST",
                    f"{target_host}/api/chat",
                    headers=ollama_headers,
                    json=stream_body,
                ) as resp:
                    if 300 <= resp.status_code < 400:
                        raise HostConsentError("HTTP redirects are forbidden on model endpoints.")
                    resp.raise_for_status()
                    for raw_line in resp.iter_lines():
                        line = raw_line.strip()
                        if not line:
                            continue
                        if line.startswith("data:"):
                            line = line[5:].strip()
                        if not line or line == "[DONE]":
                            continue
                        obj = json.loads(line)
                        piece = ((obj.get("message") or {}).get("content")) or obj.get("response") or ""
                        if piece:
                            yield str(piece)
        finally:
            if owned_ollama_transport is not None and not entered_ollama:
                owned_ollama_transport.close()
        return

    api_key = ""
    if rule.api_key_env:
        api_key = (os.environ.get(rule.api_key_env) or "").strip()
    if rule.transport == "online" and not api_key:
        raise HostConsentError(
            f"Missing API credential in environment variable '{rule.api_key_env}' for online host '{rule.host_id}'."
        )

    owned_pinned_transport: _PinnedHTTPTransportType | None = None
    effective_transport = http_transport
    if effective_transport is None and rule.transport == "online":
        resolved_target = target if target is not None else validate_endpoint_network_boundary(rule)
        if resolved_target is not None:
            owned_pinned_transport = _get_pinned_http_transport()(resolved_target)
            effective_transport = owned_pinned_transport

    client_kwargs: dict[str, object] = {
        "timeout": effective_timeout_s,
        "follow_redirects": False,
        "trust_env": False,
    }
    if effective_transport is not None:
        client_kwargs["transport"] = effective_transport

    httpx_mod = _get_httpx()
    client_entered = False
    try:
        with httpx_mod.Client(**client_kwargs) as client:
            client_entered = True
            if rule.provider == "openai_compatible":
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                }
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                body = {
                    "model": safe_model,
                    "temperature": 0.2,
                    "stream": True,
                    "messages": messages,
                }
                target_base = (
                    rule.endpoint_url.replace("://localhost:", "://127.0.0.1:").replace("://localhost/", "://127.0.0.1/")
                    if rule.transport == "local"
                    else rule.endpoint_url
                )
                if rule.transport == "local" and target_base.endswith("://localhost"):
                    target_base = target_base[: -len("localhost")] + "127.0.0.1"
                with client.stream(
                    "POST",
                    f"{target_base}/chat/completions",
                    headers=headers,
                    json=body,
                ) as resp:
                    if 300 <= resp.status_code < 400:
                        raise HostConsentError("HTTP redirects are forbidden on model endpoints.")
                    resp.raise_for_status()
                    for raw_line in resp.iter_lines():
                        line = raw_line.strip()
                        if not line or line.startswith(":"):
                            continue
                        payload_str = line[5:].strip() if line.startswith("data:") else (line if line.startswith("{") else "")
                        if not payload_str:
                            continue
                        if payload_str == "[DONE]":
                            break
                        data = json.loads(payload_str)
                        choices = data.get("choices") or []
                        if choices:
                            c0 = choices[0] or {}
                            delta = c0.get("delta") if isinstance(c0.get("delta"), dict) else (c0.get("message") or {})
                            piece = delta.get("content") if isinstance(delta, dict) else ""
                            if piece:
                                yield str(piece)
                return

            if rule.provider == "anthropic":
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                }
                body: dict[str, object] = {
                    "model": safe_model,
                    "max_tokens": 1024,
                    "temperature": 0.2,
                    "stream": True,
                    "messages": [{"role": "user", "content": user_prompt}],
                }
                if system_prompt and system_prompt.strip():
                    body["system"] = system_prompt
                with client.stream(
                    "POST",
                    f"{rule.endpoint_url}/messages",
                    headers=headers,
                    json=body,
                ) as resp:
                    if 300 <= resp.status_code < 400:
                        raise HostConsentError("HTTP redirects are forbidden on model endpoints.")
                    resp.raise_for_status()
                    for raw_line in resp.iter_lines():
                        line = raw_line.strip()
                        if not line or line.startswith("event:") or line.startswith(":"):
                            continue
                        payload_str = line[5:].strip() if line.startswith("data:") else (line if line.startswith("{") else "")
                        if not payload_str or payload_str == "[DONE]":
                            continue
                        data = json.loads(payload_str)
                        ev_type = data.get("type")
                        if ev_type == "content_block_delta":
                            delta = data.get("delta") or {}
                            piece = delta.get("text") or ""
                            if piece:
                                yield str(piece)
                        elif ev_type == "message_stop":
                            break
                        elif ev_type == "error":
                            err_msg = (data.get("error") or {}).get("message") or "Anthropic stream error"
                            raise RuntimeError(str(err_msg))
                        elif isinstance(data.get("content"), list):
                            for blk in data["content"]:
                                if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text"):
                                    yield str(blk["text"])
                return

            if rule.provider == "gemini":
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                    "x-goog-api-key": api_key,
                }
                gem_body: dict[str, object] = {
                    "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                    "generationConfig": {"temperature": 0.2},
                }
                if system_prompt and system_prompt.strip():
                    gem_body["systemInstruction"] = {"parts": [{"text": system_prompt}]}
                encoded_model = url_quote(safe_model, safe="._-")

                def _extract_gemini_parts(obj: object) -> Iterator[str]:
                    if isinstance(obj, list):
                        for elem in obj:
                            yield from _extract_gemini_parts(elem)
                        return
                    if not isinstance(obj, dict):
                        return
                    for cand in obj.get("candidates") or ():
                        if not isinstance(cand, dict):
                            continue
                        parts = (cand.get("content") or {}).get("parts") or ()
                        for part in parts:
                            if isinstance(part, dict) and part.get("text"):
                                yield str(part["text"])

                with client.stream(
                    "POST",
                    f"{rule.endpoint_url}/models/{encoded_model}:streamGenerateContent?alt=sse",
                    headers=headers,
                    json=gem_body,
                ) as resp:
                    if 300 <= resp.status_code < 400:
                        raise HostConsentError("HTTP redirects are forbidden on model endpoints.")
                    resp.raise_for_status()
                    non_sse_lines: list[str] = []
                    for raw_line in resp.iter_lines():
                        line = raw_line.strip()
                        if not line or line.startswith("event:") or line.startswith(":"):
                            continue
                        if line.startswith("data:"):
                            payload_str = line[5:].strip()
                            if not payload_str or payload_str == "[DONE]":
                                continue
                            yield from _extract_gemini_parts(json.loads(payload_str))
                        else:
                            if line.startswith("{") and line.endswith("}"):
                                try:
                                    parsed_line = json.loads(line)
                                except json.JSONDecodeError:
                                    non_sse_lines.append(raw_line)
                                else:
                                    yield from _extract_gemini_parts(parsed_line)
                            else:
                                non_sse_lines.append(raw_line)
                    if non_sse_lines:
                        joined = "\n".join(non_sse_lines).strip()
                        if joined:
                            yield from _extract_gemini_parts(json.loads(joined))
                return
    finally:
        if owned_pinned_transport is not None and not client_entered:
            owned_pinned_transport.close()

    raise ValueError(f"Unsupported provider: {rule.provider}")


def enforce_vault_online_policy(
    rule: HostSharingRule,
    *,
    brain_dir: str | Path | None,
    policy_path: Path | None = None,
    security_dir: str | Path | None = None,
) -> None:
    """Block outbound online model calls when a registered workspace enforces default_host_policy='local_only'."""
    if rule.transport != "online" or not brain_dir:
        return
    try:
        from .vault_workspaces import get_registered_vault_host_policy
    except ImportError:
        from scripts.vault_workspaces import get_registered_vault_host_policy

    matched_vid, vault_policy = get_registered_vault_host_policy(
        brain_dir,
        security_dir=security_dir,
        policy_path=policy_path,
    )
    if matched_vid is not None and vault_policy != "online_opt_in":
        raise HostConsentError(
            f"Vault workspace '{matched_vid}' enforces default_host_policy='{vault_policy}'; "
            f"online AI host '{rule.host_id}' is forbidden for this vault."
        )


def generate_grounded_answer_via_host(
    request: AnswerRequest,
    retrieved_atoms: list,
    *,
    host_id: str = "ollama_local",
    model_override: str | None = None,
    atoms_dir: str | None = None,
    brain_dir: str | None = None,
    policy_path: Path | None = None,
    security_dir: str | Path | None = None,
    http_transport: httpx.BaseTransport | None = None,
    skip_dns_check_for_mock_transport: bool = False,
    unverified_leads: Sequence[UnverifiedLead] | None = None,
    timeout_s: float | None = None,
) -> AnswerResult:
    """Execute consent check, scope filter, outbound redaction, DNS-pinned model call, and out-of-model grounding."""
    if atoms_dir is not None and brain_dir is None:
        atoms_dir = os.path.abspath(atoms_dir)
        brain_dir = os.path.dirname(atoms_dir)
    else:
        brain_dir = resolve_module_vault_path(globals(), "BRAIN_DIR", brain_dir)
        atoms_dir = resolve_module_vault_path(globals(), "ATOMS_DIR", atoms_dir, brain_dir=brain_dir)
    policies = load_host_policies(policy_path)
    if host_id not in policies:
        raise HostConsentError(f"Unknown AI host '{host_id}'. Configure it in host-sharing-policy.json first.")
    rule = policies[host_id]
    if not rule.enabled:
        raise HostConsentError(
            f"Host '{host_id}' ({rule.transport}) is disabled by default. "
            "Explicit owner opt-in is required before sharing vault context."
        )
    if rule.transport == "online" and not rule.owner_consent_recorded:
        raise HostConsentError(
            f"Online host '{host_id}' lacks recorded owner sharing consent."
        )
    enforce_vault_online_policy(
        rule,
        brain_dir=brain_dir,
        policy_path=policy_path,
        security_dir=security_dir,
    )
    if request.mode == "preference" and not rule.allow_preference_atoms:
        raise HostConsentError(
            f"Host '{host_id}' is not authorized to receive owner subjective preference memories."
        )

    chosen_model = validate_model_identifier(
        (model_override or "").strip() or rule.default_model,
        provider=rule.provider,
    )

    pinned_target = validate_endpoint_network_boundary(
        rule,
        skip_dns_resolution=bool(http_transport is not None and skip_dns_check_for_mock_transport),
    )

    leads_tuple: tuple[UnverifiedLead, ...] = ()
    if request.mode == "research":
        if unverified_leads is None:
            leads_tuple = collect_unverified_leads(
                request.query,
                atoms_dir=atoms_dir,
                brain_dir=brain_dir,
                project_scope=request.project_scope,
                limit=request.result_limit,
            )
        else:
            leads_tuple = tuple(unverified_leads)

    scoped_atoms = filter_atoms_for_host(
        retrieved_atoms,
        rule,
        mode=request.mode,
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    if not scoped_atoms:
        if request.mode == "research" and leads_tuple:
            return AnswerResult(
                False,
                f"{NO_APPROVED_MEMORY_RESPONSE}\n\n{format_unverified_leads_section(leads_tuple)}",
                (),
                "no_approved_atoms_in_host_scope",
                leads_tuple,
            )
        return AnswerResult(
            False,
            NO_APPROVED_MEMORY_RESPONSE,
            (),
            "No eligible approved atoms permitted for this host.",
            leads_tuple if request.mode == "research" else (),
        )

    raw_prompt = build_grounding_prompt(
        request,
        scoped_atoms,
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    redaction, pii_placeholder_map = _redact_outbound_with_map(
        raw_prompt,
        redact_pii=rule.redact_pii,
        fail_on_secret_detection=rule.fail_on_secret_detection,
    )

    atom_ids = [
        str(r.get("filename") if isinstance(r, dict) else getattr(r, "filename", ""))
        for r in scoped_atoms
    ]

    try:
        draft_json = _invoke_provider_draft(
            rule,
            model=chosen_model,
            prompt=redaction.sanitized_text,
            timeout_s=timeout_s,
            http_transport=http_transport,
            pinned_target=pinned_target,
        )
    except Exception:
        _audit_outbound_call(
            brain_dir=brain_dir,
            rule=rule,
            model=chosen_model,
            atom_ids=atom_ids,
            pii_redactions=redaction.pii_redaction_count,
            outcome="transport_error",
        )
        raise

    rehydrated_draft_json = _rehydrate_draft_claims(draft_json, pii_placeholder_map)
    result = validate_standard_answer(
        rehydrated_draft_json,
        scoped_atoms,
        atoms_dir=atoms_dir,
        mode=request.mode,
        unverified_leads=leads_tuple,
    )
    _audit_outbound_call(
        brain_dir=brain_dir,
        rule=rule,
        model=chosen_model,
        atom_ids=atom_ids,
        pii_redactions=redaction.pii_redaction_count,
        outcome="grounded_answer" if result.answerable else "abstained",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect and manage per-host AI sharing policies.")
    sub = parser.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list", help="List configured AI host sharing rules.")
    list_p.add_argument("--json", action="store_true")

    grant_p = sub.add_parser("configure", help="Enable or update a host sharing policy.")
    grant_p.add_argument("host_id")
    grant_p.add_argument("--enable", action="store_true")
    grant_p.add_argument("--disable", action="store_true")
    grant_p.add_argument("--allow-project", action="append", default=[], help="Allow project:<id> (or '*' for all).")
    grant_p.add_argument("--clear-projects", action="store_true", help="Clear all allowed project scopes.")
    grant_p.add_argument("--allow-unscoped", action=argparse.BooleanOptionalAction, default=None)
    grant_p.add_argument("--allow-preferences", action=argparse.BooleanOptionalAction, default=None)
    grant_p.add_argument("--endpoint-url")
    grant_p.add_argument("--default-model")

    args = parser.parse_args(argv)
    policies = load_host_policies()

    if args.command == "list":
        if args.json:
            out = {k: {**asdict(v), "allowed_projects": list(v.allowed_projects)} for k, v in sorted(policies.items())}
            print(json.dumps(out, indent=2, sort_keys=True))
        else:
            for hid, rule in sorted(policies.items()):
                state = "ENABLED " if rule.enabled else "DISABLED"
                scopes = ",".join(rule.allowed_projects) if rule.allowed_projects else "(none)"
                print(f"[{state}] {hid:20} | {rule.transport:6} | {rule.provider:17} | model={rule.default_model:22} | scopes={scopes}")
        return 0

    if args.command == "configure":
        if args.host_id not in policies:
            print(f"Unknown host_id '{args.host_id}'. Available: {', '.join(sorted(policies))}", file=sys.stderr)
            return 1
        if args.enable and args.disable:
            print("Cannot specify both --enable and --disable.", file=sys.stderr)
            return 1
        cur = policies[args.host_id]
        enabled = True if args.enable else (False if args.disable else cur.enabled)
        if args.clear_projects:
            projects = tuple(
                "*" if str(p).strip() == "*" else normalize_project_tag(p)
                for p in args.allow_project
                if str(p).strip()
            )
        elif args.allow_project:
            projects = tuple(
                "*" if str(p).strip() == "*" else normalize_project_tag(p)
                for p in args.allow_project
                if str(p).strip()
            )
        else:
            projects = cur.allowed_projects
        new_model = validate_model_identifier(args.default_model or cur.default_model, provider=cur.provider)
        updated = HostSharingRule(
            host_id=cur.host_id,
            provider=cur.provider,
            transport=cur.transport,
            endpoint_url=(args.endpoint_url or cur.endpoint_url).rstrip("/"),
            default_model=new_model,
            api_key_env=cur.api_key_env,
            enabled=enabled,
            allowed_projects=projects,
            allow_unscoped_atoms=cur.allow_unscoped_atoms if args.allow_unscoped is None else bool(args.allow_unscoped),
            allow_preference_atoms=cur.allow_preference_atoms if args.allow_preferences is None else bool(args.allow_preferences),
            redact_pii=True if cur.transport == "online" else cur.redact_pii,
            fail_on_secret_detection=True if cur.transport == "online" else cur.fail_on_secret_detection,
        )
        if updated.enabled or args.endpoint_url is not None:
            validate_endpoint_network_boundary(updated)
        policies[args.host_id] = updated
        saved_path = save_host_policies(policies)
        print(f"Saved updated policy for '{args.host_id}' to {saved_path}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
