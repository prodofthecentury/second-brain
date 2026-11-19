# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = [
#     "httpx>=0.28,<1",
#     "pydantic>=2.7,<3",
# ]
# ///
"""Host-independent loopback HTTP JSON Memory API, typed client SDK, and IDE/host adapter generator."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import datetime
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
from typing import Annotated
from urllib.parse import parse_qs, quote as url_quote, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ in ("memory_api", "scripts.memory_api"):
    sys.modules.setdefault("memory_api", sys.modules[__name__])
    sys.modules.setdefault("scripts.memory_api", sys.modules[__name__])

from answer_policy import AnswerRequest, collect_unverified_leads
from atom_mutator import approved_revision_is_current
from cognitive_core import (
    compute_memory_fitness_report,
    peek_lexical_candidate_atom_ids,
    record_grounded_answer_telemetry,
    record_retrieval_telemetry,
    save_experience_record,
)
import config as _config
from config import (
    ATOMS_DIR,
    BRAIN_DIR,
    CHROMA_PATH,
    EXPERIENCES_DIR,
    INBOX_DIR,
    LEXICAL_INDEX_PATH,
    PLAYBOOKS_DIR,
    TELEMETRY_DB_PATH,
    _is_reparse_or_link,
    ensure_dirs,
    is_content_markdown,
    safe_file_in_dir,
)
from executive_planner import ExecutivePlanRequest, assemble_executive_plan
from hardware_profile import detect_hardware_profile
from host_adapters import (
    HostConsentError,
    OutboundSecretBlockedError,
    generate_grounded_answer_via_host,
    load_host_policies,
    read_bounded_security_file,
    restrict_security_file_permissions,
    validate_model_identifier,
)
from inbox_workflow import scan_inbox_payload_risks
from index_manifest import index_manifest_is_current, load_index_manifest
from lexical_store import LexicalIndexUnavailable, lexical_index_is_current, rebuild_lexical_index
from memory_format import split_frontmatter
from memory_review import section_text
from recall import (
    MemoryRecallUnavailable,
    RecallOptions,
    consume_last_degradation_warning,
    search_memory,
    suggest_duplicate_atoms,
)
from skill_playbooks import list_playbooks, playbook_visible_in_scope
import api_product


def _extract_claim_text(content: str) -> str:
    parsed = split_frontmatter(content)
    body = "\n".join(parsed[1]) if parsed else content
    return section_text(body, "Claim")


ALLOWED_API_BIND_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8765
DEFAULT_MAX_WORKER_THREADS = 32
DEFAULT_MAX_SSE_STREAMS = 8
MIN_API_TOKEN_LEN = 24
MAX_API_BODY_BYTES = 65536
MAX_API_LARGE_BODY_BYTES = 2_200_000
MAX_INBOX_API_CHARS = 16000
API_MAX_AUTH_FAILURES = 5
API_LOCKOUT_SECONDS = 60
RECALL_REMEDIATION_MESSAGE = "Run 'second-brain rebuild-lexical' or start local Ollama."

_API_RATE_LOCK = threading.Lock()
_API_AUDIT_LOCK = threading.Lock()
_API_AUTH_FAILURES: dict[str, dict[str, float | int]] = {}
_MODEL_OVERRIDE_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")


def assert_api_loopback_bind(host: str | None) -> str:
    """Reject 0.0.0.0, ::, or non-127.0.0.1 binds with 403 Forbidden."""
    if not isinstance(host, str) or host.strip() != ALLOWED_API_BIND_HOST:
        raise PermissionError(
            f"403 Forbidden: Memory API must bind strictly to {ALLOWED_API_BIND_HOST} (got {host!r})."
        )
    return ALLOWED_API_BIND_HOST


def _fallback_security_state_dir() -> Path:
    sec_env = (
        os.environ.get("BRAIN_SECURITY_STATE_DIR")
        or os.environ.get("SECOND_BRAIN_SECURITY_DIR")
        or ""
    ).strip()
    if sec_env:
        return Path(sec_env).expanduser().resolve()
    cfg_sec = getattr(_config, "security_state_dir", None)
    if callable(cfg_sec):
        return Path(cfg_sec()).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    elif os.environ.get("XDG_STATE_HOME"):
        base = Path(os.environ["XDG_STATE_HOME"]).expanduser()
    else:
        base = Path.home() / ".local" / "state"
    return base / "SecondBrain" / "security"


_security_state_dir = _fallback_security_state_dir


def default_api_token_path() -> Path:
    override = (os.environ.get("BRAIN_MEMORY_API_TOKEN_FILE") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _fallback_security_state_dir() / "memory-api-token"


def default_api_port_path() -> Path:
    override = (os.environ.get("BRAIN_API_PORT_FILE") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    sec_override = (
        os.environ.get("BRAIN_SECURITY_STATE_DIR")
        or os.environ.get("SECOND_BRAIN_SECURITY_DIR")
        or ""
    ).strip()
    if sec_override:
        return Path(sec_override).expanduser().resolve() / "api-port"
    cfg_port_fn = getattr(_config, "api_port_file", None)
    if callable(cfg_port_fn):
        return Path(cfg_port_fn()).expanduser().resolve()
    return _fallback_security_state_dir() / "api-port"


def _pid_is_alive(pid: int) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_proc = kernel32.OpenProcess
            open_proc.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            open_proc.restype = wintypes.HANDLE
            get_exit = kernel32.GetExitCodeProcess
            get_exit.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            get_exit.restype = wintypes.BOOL
            close_h = kernel32.CloseHandle
            close_h.argtypes = [wintypes.HANDLE]
            close_h.restype = wintypes.BOOL

            handle = open_proc(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() == 5
            try:
                exit_code = wintypes.DWORD(0)
                if not get_exit(handle, ctypes.byref(exit_code)):
                    return False
                return int(exit_code.value) == 259
            finally:
                close_h(handle)
        except Exception:
            pass
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, OSError, SystemError, ValueError):
        return False


def write_active_api_port_file(
    port: int,
    *,
    host: str = ALLOWED_API_BIND_HOST,
    port_file: Path | None = None,
) -> Path:
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise ValueError(f"Invalid active API port {port!r}; expected 1..65535.")
    target = (Path(port_file) if port_file is not None else default_api_port_path()).expanduser().resolve()
    if os.path.lexists(str(target)) and _is_reparse_or_link(str(target)):
        raise PermissionError(f"Refusing to overwrite symlinked API port file: {target}")
    if os.path.lexists(str(target.parent)) and _is_reparse_or_link(str(target.parent)):
        raise PermissionError(f"Refusing to write API port file under symlinked directory: {target.parent}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _is_reparse_or_link(str(target.parent)) or (os.path.lexists(str(target)) and _is_reparse_or_link(str(target))):
        raise PermissionError(f"Refusing to write API port file through a symlink or reparse point: {target}")

    payload = {
        "port": int(port),
        "host": str(host),
        "pid": os.getpid(),
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".api-port-", suffix=".tmp")
    fd_closed = False
    try:
        if os.name != "nt":
            os.chmod(tmp_name, 0o600)
        if os.write(fd, encoded) != len(encoded):
            raise OSError("Incomplete API port file write.")
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


def remove_active_api_port_file(
    *,
    expected_pid: int | None = None,
    port_file: Path | None = None,
) -> None:
    target = (Path(port_file) if port_file is not None else default_api_port_path()).expanduser()
    if not os.path.lexists(str(target)):
        return
    if _is_reparse_or_link(str(target)) or (
        os.path.lexists(str(target.parent)) and _is_reparse_or_link(str(target.parent))
    ):
        return
    if expected_pid is not None:
        try:
            raw = read_bounded_security_file(Path(target), max_bytes=4096).strip()
            parsed = json.loads(raw) if raw else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(parsed, dict):
            return
        rec_pid = parsed.get("pid")
        if isinstance(rec_pid, bool) or not isinstance(rec_pid, int) or rec_pid != int(expected_pid):
            return
    try:
        os.remove(str(target))
    except OSError:
        pass


def discover_active_api_port(
    default_port: int = DEFAULT_API_PORT,
    *,
    port_file: Path | None = None,
) -> int:
    env_port = (os.environ.get("SECOND_BRAIN_PORT") or os.environ.get("BRAIN_MEMORY_API_PORT") or "").strip()
    if env_port:
        try:
            parsed_env = int(env_port)
            if 1 <= parsed_env <= 65535:
                return parsed_env
        except ValueError:
            pass

    target = (Path(port_file) if port_file is not None else default_api_port_path()).expanduser()
    if os.path.lexists(str(target)) and not _is_reparse_or_link(str(target)):
        if not (os.path.lexists(str(target.parent)) and _is_reparse_or_link(str(target.parent))):
            try:
                raw = read_bounded_security_file(Path(target), max_bytes=4096).strip()
            except (OSError, UnicodeDecodeError):
                raw = ""
            if raw:
                candidate_port: int | None = None
                candidate_pid: int | None = None
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, int) and not isinstance(parsed, bool):
                        candidate_port = parsed
                    elif isinstance(parsed, dict):
                        p_val = parsed.get("port")
                        if isinstance(p_val, int) and not isinstance(p_val, bool):
                            candidate_port = p_val
                        elif isinstance(p_val, str) and p_val.strip().isdigit():
                            candidate_port = int(p_val.strip())
                        pid_val = parsed.get("pid")
                        if isinstance(pid_val, int) and not isinstance(pid_val, bool):
                            candidate_pid = pid_val
                        elif isinstance(pid_val, str) and pid_val.strip().isdigit():
                            candidate_pid = int(pid_val.strip())
                except (json.JSONDecodeError, ValueError, TypeError):
                    if raw.isdigit():
                        candidate_port = int(raw)
                if candidate_port is not None and 1 <= candidate_port <= 65535:
                    if not candidate_pid or _pid_is_alive(int(candidate_pid)):
                        return int(candidate_port)
    return int(default_port)


def generate_memory_api_token(token_path: Path | None = None) -> tuple[str, Path]:
    target = (token_path or default_api_token_path()).expanduser().resolve()
    if os.path.lexists(str(target)) and _is_reparse_or_link(str(target)):
        raise PermissionError(f"Refusing to overwrite symlinked API token file: {target}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _is_reparse_or_link(str(target.parent)):
        raise PermissionError(f"Refusing to write API token under symlinked directory: {target.parent}")
    token = secrets.token_urlsafe(32)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".api-token-", suffix=".tmp")
    fd_closed = False
    try:
        if os.name != "nt":
            os.chmod(tmp_name, 0o600)
        encoded = (token + "\n").encode("utf-8")
        if os.write(fd, encoded) != len(encoded):
            raise OSError("Incomplete token write.")
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
    return token, target


def get_configured_api_token(token_path: Path | None = None) -> str | None:
    env_tok = (os.environ.get("BRAIN_MEMORY_API_TOKEN") or "").strip()
    if env_tok:
        return env_tok if len(env_tok) >= MIN_API_TOKEN_LEN else None
    target = token_path or default_api_token_path()
    if not os.path.lexists(str(target)):
        return None
    if _is_reparse_or_link(str(target)) or (
        os.path.lexists(str(Path(target).parent)) and _is_reparse_or_link(str(Path(target).parent))
    ):
        return None
    try:
        content = read_bounded_security_file(target, max_bytes=4096).strip()
    except OSError:
        return None
    return content if len(content) >= MIN_API_TOKEN_LEN else None


def reset_api_rate_limiter() -> None:
    with _API_RATE_LOCK:
        _API_AUTH_FAILURES.clear()
    api_product.reset_owner_rate_limiter()


def _check_and_record_auth(client_key: str, provided_header: str | None, expected_token: str | None) -> tuple[bool, int]:
    """Return (authorized, http_status_on_failure).

    Unauthenticated browser noise (missing/empty Authorization header) returns 401 without
    incrementing lockout counters, and a caller presenting the valid 256-bit Bearer token
    is never locked out by another local process's wrong guesses. Non-empty invalid
    Authorization headers are throttled with 429 after 5 attempts, resetting after expiry.
    """
    if not expected_token or len(expected_token) < MIN_API_TOKEN_LEN:
        return (False, 401)

    raw_header = provided_header.strip() if isinstance(provided_header, str) else ""
    if not raw_header:
        return (False, 401)

    candidate = raw_header[7:].strip() if raw_header.lower().startswith("bearer ") else ""
    matched = bool(candidate) and hmac.compare_digest(
        candidate.encode("utf-8"),
        expected_token.encode("utf-8"),
    )
    now = time.monotonic()
    with _API_RATE_LOCK:
        entry = _API_AUTH_FAILURES.get(client_key)
        count = int(entry.get("count", 0)) if entry else 0
        locked_until = float(entry.get("locked_until", 0.0)) if entry else 0.0
        if locked_until > 0.0 and now >= locked_until:
            count = 0
            locked_until = 0.0
            _API_AUTH_FAILURES.pop(client_key, None)

        if matched:
            _API_AUTH_FAILURES.pop(client_key, None)
            return (True, 200)

        if locked_until > 0.0 and now < locked_until:
            return (False, 429)

        count += 1
        new_locked_until = now + API_LOCKOUT_SECONDS if count >= API_MAX_AUTH_FAILURES else 0.0
        _API_AUTH_FAILURES[client_key] = {"count": count, "locked_until": new_locked_until}
        return (False, 429 if new_locked_until > 0.0 else 401)


def _max_body_bytes_for_route(method: str, path: str, *, is_owner: bool = False) -> int:
    if not is_owner:
        return MAX_API_BODY_BYTES
    if method == "POST" and path == "/v1/notes":
        return MAX_API_LARGE_BODY_BYTES
    if method == "PUT" and path.startswith("/v1/notes/") and not path.endswith(("/verify", "/backlinks")):
        return MAX_API_LARGE_BODY_BYTES
    if method == "PUT" and path.startswith("/v1/atoms/"):
        return MAX_API_LARGE_BODY_BYTES
    return MAX_API_BODY_BYTES


# ─── Typed API Schemas (Pydantic v2) ─────────────────────────────────────────

BoundedExperienceItem = Annotated[str, Field(min_length=1, max_length=500)]
BoundedContextTag = Annotated[str, Field(min_length=1, max_length=64)]


class ApiRecallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(..., min_length=1, max_length=2000)
    retrieval_mode: str = Field(default="auto", pattern=r"^(auto|lexical|dense|rrf)$")
    n_results: int = Field(default=5, ge=1, le=25)
    project_scope: str | None = Field(default=None, max_length=120)
    allow_cross_project: bool = False
    follow_links: bool = True
    include_duplicates: bool = False
    include_preferences: bool = False
    enable_fitness_boost: bool = False
    active_context_profile: list[BoundedContextTag] = Field(default_factory=list, max_length=16)


class ApiAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(..., min_length=1, max_length=2000)
    mode: str = Field(default="standard", pattern=r"^(standard|preference|research)$")
    retrieval_mode: str = Field(default="auto", pattern=r"^(auto|lexical|dense|rrf)$")
    n_results: int = Field(default=5, ge=1, le=15)
    project_scope: str | None = Field(default=None, max_length=120)
    allow_cross_project: bool = False
    host_id: str = Field(default="ollama_local", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    model_override: str | None = Field(default=None, max_length=128)

    @field_validator("model_override")
    @classmethod
    def _check_model_override(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if (
            not v
            or v.startswith("/")
            or ".." in v
            or "?" in v
            or "#" in v
            or "%" in v
            or any(ch.isspace() for ch in v)
            or not _MODEL_OVERRIDE_RE.fullmatch(v)
        ):
            raise ValueError(f"Invalid or unsafe model_override identifier: {v!r}")
        return validate_model_identifier(v)


class ApiInboxCaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(..., min_length=1, max_length=MAX_INBOX_API_CHARS)
    source: str = Field(default="api_client", min_length=1, max_length=200)


class ApiExperienceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(..., min_length=1, max_length=500)
    result_summary: str = Field(..., min_length=1, max_length=4000)
    success: bool = True
    context: list[BoundedExperienceItem] = Field(default_factory=list, max_length=20)
    plan: list[BoundedExperienceItem] = Field(default_factory=list, max_length=30)
    actions: list[BoundedExperienceItem] = Field(default_factory=list, max_length=30)
    tools_used: list[BoundedExperienceItem] = Field(default_factory=list, max_length=30)
    referenced_atoms: list[BoundedExperienceItem] = Field(default_factory=list, max_length=30)
    reasoning_summary: str | None = Field(default=None, max_length=4000)
    confidence_before: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence_after: float | None = Field(default=None, ge=0.0, le=1.0)
    new_memories: list[BoundedExperienceItem] = Field(default_factory=list, max_length=30)
    updated_memories: list[BoundedExperienceItem] = Field(default_factory=list, max_length=30)
    candidate_skills: list[BoundedExperienceItem] = Field(default_factory=list, max_length=20)
    reflection: str | None = Field(default=None, max_length=4000)
    lessons: list[BoundedExperienceItem] = Field(default_factory=list, max_length=20)
    candidate_procedure_title: str | None = Field(default=None, max_length=200)
    propose_to_inbox: bool = False


class ApiExecutivePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(..., min_length=1, max_length=2000)
    project_scope: str | None = Field(default=None, max_length=120)
    allow_cross_project: bool = False
    active_context_profile: list[BoundedContextTag] = Field(default_factory=list, max_length=16)
    retrieval_mode: str = Field(default="lexical", pattern=r"^(auto|lexical|dense|rrf)$")
    include_preferences: bool = False
    mode: str = Field(default="deterministic", pattern=r"^(deterministic|model_assisted)$")
    host_id: str = Field(default="ollama_local", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    model_override: str | None = Field(default=None, max_length=128)
    max_context_chars: int = Field(default=6000, ge=512, le=24000)
    max_atoms: int = Field(default=6, ge=1, le=15)

    @field_validator("model_override")
    @classmethod
    def _check_plan_model_override(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if (
            not v
            or v.startswith("/")
            or ".." in v
            or "?" in v
            or "#" in v
            or "%" in v
            or any(ch.isspace() for ch in v)
            or not _MODEL_OVERRIDE_RE.fullmatch(v)
        ):
            raise ValueError(f"Invalid or unsafe model_override identifier: {v!r}")
        return validate_model_identifier(v)


def _resolve_default_brain_dir() -> str:
    resolver = getattr(_config, "resolve_active_brain_dir", None)
    if callable(resolver):
        resolved = str(resolver() or "").strip()
        if resolved:
            return os.path.abspath(resolved)
    env_vault = (os.environ.get("BRAIN_VAULT_DIR") or "").strip()
    if env_vault:
        return os.path.abspath(env_vault)
    return os.path.abspath(_config.BRAIN_DIR)


class MemoryApiRuntimeConfig:
    def __init__(
        self,
        *,
        brain_dir: str | None = None,
        atoms_dir: str | None = None,
        inbox_dir: str | None = None,
        experiences_dir: str | None = None,
        playbooks_dir: str | None = None,
        lexical_index_path: str | None = None,
        chroma_path: str | None = None,
        telemetry_db_path: str | None = None,
        token_path: Path | None = None,
        owner_token_path: Path | None = None,
        policy_path: Path | None = None,
        http_transport: httpx.BaseTransport | None = None,
        skip_dns_check_for_mock_transport: bool = False,
    ) -> None:
        module_brain = os.path.abspath(BRAIN_DIR)
        active_brain = _resolve_default_brain_dir()
        if brain_dir is None or (os.path.abspath(brain_dir) == module_brain and active_brain != module_brain):
            self.brain_dir = active_brain
        else:
            self.brain_dir = os.path.abspath(brain_dir)
        brain_changed = self.brain_dir != module_brain

        def _resolve_child(explicit: str | None, module_default: str, rel_parts: tuple[str, ...]) -> str:
            if explicit is not None and not (brain_changed and os.path.abspath(explicit) == os.path.abspath(module_default)):
                return os.path.abspath(explicit)
            if not brain_changed:
                return os.path.abspath(module_default)
            return os.path.join(self.brain_dir, *rel_parts)

        self.atoms_dir = _resolve_child(atoms_dir, ATOMS_DIR, ("2-atoms",))
        self.inbox_dir = _resolve_child(inbox_dir, INBOX_DIR, ("0-raw", "inbox"))
        self.experiences_dir = _resolve_child(experiences_dir, EXPERIENCES_DIR, ("4-experiences",))
        self.playbooks_dir = _resolve_child(playbooks_dir, PLAYBOOKS_DIR, ("5-playbooks",))
        self.lexical_index_path = _resolve_child(lexical_index_path, LEXICAL_INDEX_PATH, (".sidecar", "lexical.sqlite3"))
        self.chroma_path = _resolve_child(chroma_path, CHROMA_PATH, (".chroma",))
        self.telemetry_db_path = _resolve_child(telemetry_db_path, TELEMETRY_DB_PATH, (".sidecar", "telemetry.sqlite3"))
        self.token_path = token_path
        self.owner_token_path = owner_token_path
        self.policy_path = policy_path
        self.http_transport = http_transport
        self.skip_dns_check_for_mock_transport = skip_dns_check_for_mock_transport

    @property
    def owner_token(self) -> str | None:
        return api_product.get_owner_token(self.owner_token_path)


MemoryApiRuntime = MemoryApiRuntimeConfig


class MemoryLoopbackHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(
        self,
        host: str,
        port: int,
        runtime_config: MemoryApiRuntimeConfig | None = None,
        *,
        max_workers: int = DEFAULT_MAX_WORKER_THREADS,
        max_stream_slots: int = DEFAULT_MAX_SSE_STREAMS,
    ) -> None:
        verified_host = assert_api_loopback_bind(host)
        if isinstance(port, bool) or not isinstance(port, int) or not (0 <= port <= 65535):
            raise ValueError(f"Invalid loopback TCP port {port!r}; expected 0..65535.")
        worker_cap = max(1, int(max_workers))
        stream_cap = max(1, int(max_stream_slots))
        self.runtime = runtime_config or MemoryApiRuntimeConfig()
        expected = get_configured_api_token(self.runtime.token_path)
        if not expected:
            raise PermissionError(
                "Memory API startup aborted: no valid (>=24 char) bearer token configured. "
                "Run `second-brain serve --generate-token` first."
            )
        if not os.path.exists(self.runtime.lexical_index_path):
            try:
                if not (os.path.lexists(self.runtime.brain_dir) and _is_reparse_or_link(self.runtime.brain_dir)):
                    os.makedirs(self.runtime.atoms_dir, exist_ok=True)
                    os.makedirs(os.path.dirname(self.runtime.lexical_index_path), exist_ok=True)
                    if os.path.isdir(self.runtime.atoms_dir) and not _is_reparse_or_link(self.runtime.atoms_dir):
                        rebuild_lexical_index(self.runtime.atoms_dir, self.runtime.lexical_index_path)
            except Exception:
                # Ignore benign errors on read-only or synthetic test harnesses.
                pass
        super().__init__((verified_host, port), MemoryApiRequestHandler)
        self.max_workers = worker_cap
        self._worker_pool = ThreadPoolExecutor(max_workers=worker_cap, thread_name_prefix="sb-api")
        self._inflight_slots = threading.BoundedSemaphore(worker_cap * 2)
        self._stream_slots = threading.BoundedSemaphore(stream_cap)
        self._pool_closed = False

    def _reject_overloaded_socket(self, request: socket.socket) -> None:
        body = b'{"error": "Service Unavailable: server concurrency limit reached."}\n'
        resp = (
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"Cache-Control: no-store\r\n"
            b"Connection: close\r\n"
            b"Retry-After: 1\r\n\r\n" + body
        )
        try:
            request.settimeout(0.05)
            try:
                request.recv(4096)
            except OSError:
                pass
            request.sendall(resp)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)

    def _process_request_Pool(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        try:
            self.process_request_thread(request, client_address)
        finally:
            try:
                self._inflight_slots.release()
            except ValueError:
                pass

    _process_request_pool = _process_request_Pool

    def process_request(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        if self._pool_closed or not self._inflight_slots.acquire(blocking=False):
            self._reject_overloaded_socket(request)
            return
        try:
            self._worker_pool.submit(self._process_request_Pool, request, client_address)
        except RuntimeError:
            try:
                self._inflight_slots.release()
            except ValueError:
                pass
            self._reject_overloaded_socket(request)

    def server_close(self) -> None:
        self._pool_closed = True
        pool = getattr(self, "_worker_pool", None)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        super().server_close()


def create_memory_api_server(
    host: str = ALLOWED_API_BIND_HOST,
    port: int = DEFAULT_API_PORT,
    runtime_config: MemoryApiRuntimeConfig | None = None,
    *,
    max_workers: int = DEFAULT_MAX_WORKER_THREADS,
) -> MemoryLoopbackHTTPServer:
    return MemoryLoopbackHTTPServer(host, port, runtime_config, max_workers=max_workers)


def serve_memory_api(
    host: str = ALLOWED_API_BIND_HOST,
    port: int = DEFAULT_API_PORT,
    runtime_config: MemoryApiRuntimeConfig | None = None,
) -> int:
    server = create_memory_api_server(host, port, runtime_config)
    bound_host, actual_port = server.server_address[0], server.server_address[1]
    port_file_path: Path | None = None
    try:
        try:
            port_file_path = write_active_api_port_file(actual_port, host=bound_host)
        except OSError:
            port_file_path = None
        print(f"SECOND_BRAIN_API_READY http://{bound_host}:{actual_port} port={actual_port}", flush=True)
        print(f"Second Brain Loopback Memory API listening on http://{bound_host}:{actual_port}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            remove_active_api_port_file(expected_pid=os.getpid(), port_file=port_file_path)
        finally:
            server.server_close()
    return 0


def _audit_api_event(brain_dir: str, endpoint: str, method: str, status_code: int) -> None:
    sidecar_dir = os.path.join(brain_dir, ".sidecar")
    audit_path = os.path.join(sidecar_dir, "memory-api-audit.jsonl")
    record = {
        "schema_version": 1,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "method": method,
        "endpoint": endpoint,
        "status_code": status_code,
    }
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    with _API_AUDIT_LOCK:
        if (
            (os.path.lexists(brain_dir) and _is_reparse_or_link(brain_dir))
            or (os.path.lexists(sidecar_dir) and _is_reparse_or_link(sidecar_dir))
            or (os.path.lexists(audit_path) and _is_reparse_or_link(audit_path))
        ):
            raise PermissionError("Refusing to write API audit log through a symlink or reparse point.")
        os.makedirs(sidecar_dir, exist_ok=True)
        if _is_reparse_or_link(sidecar_dir) or (os.path.lexists(audit_path) and _is_reparse_or_link(audit_path)):
            raise PermissionError("Refusing to write API audit log through a symlink or reparse point.")
        existed_before = os.path.exists(audit_path)
        fd = os.open(audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if os.write(fd, line) != len(line):
                raise OSError("Incomplete write to memory API audit log.")
            os.fsync(fd)
        finally:
            os.close(fd)
        if not existed_before:
            restrict_security_file_permissions(Path(audit_path))


class MemoryApiRequestHandler(BaseHTTPRequestHandler):
    server: MemoryLoopbackHTTPServer
    timeout = 15.0

    def handle(self) -> None:
        try:
            super().handle()
        except (TimeoutError, socket.timeout, ConnectionError):
            return

    def log_message(self, format: str, *args: object) -> None:
        # Suppress default noisy stderr request logging containing paths/params
        return

    def _send_json(self, status_code: int, payload: dict[str, object]) -> None:
        raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if getattr(self, "close_connection", False):
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
            return

    def _verify_security_envelope(self) -> bool:
        client_ip = self.client_address[0]
        if client_ip != ALLOWED_API_BIND_HOST:
            self._send_json(403, {"error": "403 Forbidden: non-loopback client rejected."})
            return False

        bound_port = self.server.server_address[1]
        allowed_hosts = {ALLOWED_API_BIND_HOST, f"{ALLOWED_API_BIND_HOST}:{bound_port}"}
        host_hdr = (self.headers.get("Host") or "").strip().lower()
        if host_hdr not in allowed_hosts:
            self._send_json(403, {"error": "403 Forbidden: invalid Host header (DNS-rebinding protection)."})
            return False

        origin_hdr = (self.headers.get("Origin") or "").strip().lower()
        if origin_hdr and origin_hdr != f"http://{ALLOWED_API_BIND_HOST}:{bound_port}":
            self._send_json(403, {"error": "403 Forbidden: cross-origin browser request rejected."})
            return False

        sec_fetch_site = self.headers.get("Sec-Fetch-Site")
        if sec_fetch_site is not None and sec_fetch_site.strip().lower() not in {"none", "same-origin"}:
            self._send_json(403, {"error": "403 Forbidden: cross-site browser request rejected."})
            return False

        expected = get_configured_api_token(self.server.runtime.token_path)
        auth_ok, fail_code = _check_and_record_auth(
            client_ip,
            self.headers.get("Authorization"),
            expected,
        )
        if not auth_ok:
            msg = (
                "Too Many Requests: authentication rate limit exceeded."
                if fail_code == 429
                else "Unauthorized: invalid or missing bearer token."
            )
            self._send_json(fail_code, {"error": msg})
            return False
        return True

    def _drain_rejected_request_body(self, clen: int) -> None:
        if clen <= 0:
            return
        budget = min(clen, MAX_API_LARGE_BODY_BYTES + 65536)
        prev_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(0.25)
            remaining = budget
            read_fn = self.rfile.read1 if hasattr(self.rfile, "read1") else self.rfile.read
            while remaining > 0:
                chunk = read_fn(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (TimeoutError, socket.timeout, OSError, ValueError):
            pass
        finally:
            try:
                self.connection.settimeout(prev_timeout)
            except OSError:
                pass

    def _read_json_body(self, *, max_bytes: int = MAX_API_BODY_BYTES) -> dict[str, object] | None:
        if self.headers.get("Transfer-Encoding") is not None:
            self.close_connection = True
            self._send_json(400, {"error": "Transfer-Encoding is not supported; send Content-Length."})
            return None
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self.close_connection = True
            self._send_json(415, {"error": "Unsupported Media Type: expected application/json."})
            return None
        clen_raw = self.headers.get("Content-Length")
        if clen_raw is None:
            self.close_connection = True
            self._send_json(411, {"error": "Length Required."})
            return None
        clen_trimmed = clen_raw.strip()
        if len(clen_trimmed) > 9:
            self.close_connection = True
            self._send_json(413, {"error": f"Payload Too Large: max {max_bytes} bytes."})
            self._drain_rejected_request_body(MAX_API_LARGE_BODY_BYTES)
            return None
        try:
            clen = int(clen_trimmed)
        except ValueError:
            self.close_connection = True
            self._send_json(400, {"error": "Invalid Content-Length."})
            return None
        if clen < 0 or clen > max_bytes:
            self.close_connection = True
            self._send_json(413, {"error": f"Payload Too Large: max {max_bytes} bytes."})
            if clen > 0:
                self._drain_rejected_request_body(clen)
            return None
        body_bytes = self.rfile.read(clen) if clen > 0 else b"{}"
        try:
            parsed = json.loads(body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "Malformed UTF-8 JSON body."})
            return None
        if not isinstance(parsed, dict):
            self._send_json(400, {"error": "JSON root must be an object."})
            return None
        return parsed

    def _handle_route_exception(self, method: str, path: str, exc: Exception) -> None:
        rt = self.server.runtime
        if isinstance(exc, (MemoryRecallUnavailable, LexicalIndexUnavailable)):
            try:
                _audit_api_event(rt.brain_dir, path, method, 503)
            except Exception:
                pass
            self._send_json(503, {"error": str(exc), "remediation": RECALL_REMEDIATION_MESSAGE})
            return
        if isinstance(exc, (PermissionError, HostConsentError, OutboundSecretBlockedError)):
            try:
                _audit_api_event(rt.brain_dir, path, method, 403)
            except Exception:
                pass
            self._send_json(403, {"error": str(exc)})
            return
        if isinstance(exc, FileNotFoundError):
            try:
                _audit_api_event(rt.brain_dir, path, method, 404)
            except Exception:
                pass
            self._send_json(404, {"error": str(exc)})
            return
        if exc.__class__.__name__ == "ValidationError" or isinstance(exc, ValueError):
            try:
                _audit_api_event(rt.brain_dir, path, method, 422)
            except Exception:
                pass
            self._send_json(422, {"error": str(exc)})
            return
        self._send_json(500, {"error": "Internal server error while processing request."})

    def do_GET(self) -> None:
        if not self._verify_security_envelope():
            return
        rt = self.server.runtime
        parsed_url = urlparse(self.path)
        path = parsed_url.path

        try:
            is_owner, throttled_429, had_failed_owner_hdr = api_product.evaluate_request_owner_auth(
                self, rt, path=path, method="GET", audit_fn=_audit_api_event
            )
            if throttled_429:
                self._send_json(429, {"error": "Too Many Requests: owner token authentication rate limit exceeded."})
                return

            if path == "/v1/health":
                if had_failed_owner_hdr:
                    _audit_api_event(rt.brain_dir, path, "GET", 403)
                atom_files = [
                    f
                    for f in (
                        os.listdir(rt.atoms_dir)
                        if (os.path.isdir(rt.atoms_dir) and not _is_reparse_or_link(rt.atoms_dir))
                        else []
                    )
                    if is_content_markdown(f)
                ]
                approved = sum(
                    1
                    for f in atom_files
                    if approved_revision_is_current(os.path.join(rt.atoms_dir, f), brain_dir=rt.brain_dir, atoms_dir=rt.atoms_dir)
                )
                lex_ok = lexical_index_is_current(rt.atoms_dir, rt.lexical_index_path)
                manifest = load_index_manifest(rt.chroma_path) if os.path.isdir(rt.chroma_path) else None
                dense_ok = bool(manifest and index_manifest_is_current(manifest, rt.atoms_dir))
                hw = detect_hardware_profile(ollama_timeout=1.5, use_cache=True)
                _audit_api_event(rt.brain_dir, path, "GET", 200)
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "api_version": "1.0.0",
                        "atom_count": len(atom_files),
                        "approved_atom_count": approved,
                        "lexical_index_current": lex_ok,
                        "dense_index_current": dense_ok,
                        "dense_generation_id": manifest.get("generation_id") if manifest else None,
                        "hardware_tier": hw.tier_id,
                    },
                )
                return

            if path == "/v1/profile":
                if had_failed_owner_hdr:
                    _audit_api_event(rt.brain_dir, path, "GET", 403)
                hw = detect_hardware_profile(ollama_timeout=1.5, use_cache=True)
                policies = load_host_policies(rt.policy_path)
                hw_dict = asdict(hw)
                hw_dict["installed_ollama_models"] = list(hw.installed_ollama_models)
                hw_dict["cpu_logical_cores"] = hw.cpu_threads
                hw_dict["ram_total_gb"] = hw.total_ram_gib
                hw_dict["vram_gb"] = hw.vram_gib
                hw_dict["has_gpu"] = bool(hw.gpu_backend and hw.gpu_backend != "cpu")
                hw_dict["recommended_chat_model"] = hw.recommended_generation_model
                hosts_summary = {
                    hid: {
                        "provider": rule.provider,
                        "transport": rule.transport,
                        "endpoint_url": rule.endpoint_url,
                        "default_model": rule.default_model,
                        "enabled": rule.enabled,
                        "allowed_projects": list(rule.allowed_projects),
                        "allow_unscoped_atoms": rule.allow_unscoped_atoms,
                        "allow_preference_atoms": rule.allow_preference_atoms,
                        "redact_pii": rule.redact_pii,
                        "fail_on_secret_detection": rule.fail_on_secret_detection,
                    }
                    for hid, rule in sorted(policies.items())
                }
                _audit_api_event(rt.brain_dir, path, "GET", 200)
                self._send_json(200, {"hardware": hw_dict, "hosts": hosts_summary})
                return

            if path == "/v1/cognitive/fitness":
                if had_failed_owner_hdr:
                    _audit_api_event(rt.brain_dir, path, "GET", 403)
                report = compute_memory_fitness_report(
                    atoms_dir=rt.atoms_dir,
                    brain_dir=rt.brain_dir,
                    db_path=rt.telemetry_db_path,
                )
                raw_pending = list(report.get("pending_review_bottlenecks") or [])
                raw_proc = list(report.get("procedure_maturity") or [])
                raw_high = list(report.get("high_utility_verified_atoms") or [])
                raw_cold = list(report.get("cold_verified_atoms") or [])
                if not is_owner:
                    pref_files = {
                        name
                        for name in (os.listdir(rt.atoms_dir) if os.path.isdir(rt.atoms_dir) else [])
                        if is_content_markdown(name)
                        and api_product._is_preference_atom_file(os.path.join(rt.atoms_dir, name))
                    }
                    if pref_files:
                        raw_high = [it for it in raw_high if isinstance(it, dict) and it.get("atom_id") not in pref_files]
                        raw_cold = [it for it in raw_cold if isinstance(it, dict) and it.get("atom_id") not in pref_files]
                        raw_proc = [it for it in raw_proc if isinstance(it, dict) and it.get("atom_id") not in pref_files]
                sanitized_report = {
                    **report,
                    "high_utility_verified_atoms": raw_high,
                    "high_fitness_atoms": raw_high,
                    "cold_verified_atoms": raw_cold,
                    "dormant_atoms": raw_cold,
                    "pending_review_bottleneck_count": len(raw_pending),
                    "pending_review_bottlenecks": (
                        raw_pending
                        if is_owner
                        else [
                            {k: v for k, v in item.items() if k != "atom_id"}
                            for item in raw_pending
                            if isinstance(item, dict)
                        ]
                    ),
                    "procedure_maturity": (
                        raw_proc
                        if is_owner
                        else [
                            item if item.get("approved_current") else {k: v for k, v in item.items() if k != "atom_id"}
                            for item in raw_proc
                            if isinstance(item, dict)
                        ]
                    ),
                }
                _audit_api_event(rt.brain_dir, path, "GET", 200)
                self._send_json(200, sanitized_report)
                return

            if path == "/v1/playbooks":
                if had_failed_owner_hdr:
                    _audit_api_event(rt.brain_dir, path, "GET", 403)
                qs = parse_qs(parsed_url.query)
                proj_raw = (qs.get("project") or [""])[0].strip()
                if len(proj_raw) > 120:
                    raise ValueError("Invalid project query filter.")
                inc_inactive = bool(is_owner and (qs.get("include_inactive") or ["0"])[0] in {"1", "true", "yes"})
                pbs = list_playbooks(
                    playbooks_dir=rt.playbooks_dir,
                    brain_dir=rt.brain_dir,
                    atoms_dir=rt.atoms_dir,
                    telemetry_db_path=rt.telemetry_db_path,
                    active_only=not inc_inactive,
                )
                if proj_raw:
                    # Require EVERY backing atom to individually match the scope (no merged-union laundering).
                    pbs = [p for p in pbs if playbook_visible_in_scope(p, (proj_raw,))]
                _audit_api_event(rt.brain_dir, path, "GET", 200)
                pb_list = [
                    {
                        "playbook_id": p.playbook_id,
                        "filename": p.filename,
                        "title": p.title,
                        "status": p.status,
                        "valid": p.valid,
                        "sha256": p.sha256,
                        "contexts": list(p.contexts),
                        "backing_atoms": list(p.backing_atoms),
                        "trigger": p.trigger,
                        "preconditions": list(p.preconditions),
                        "steps": list(p.steps),
                        "expected_outputs": list(p.expected_outputs),
                        "failure_recovery": list(p.failure_recovery),
                    }
                    for p in pbs
                ]
                self._send_json(200, {"count": len(pb_list), "playbooks": pb_list, "items": pb_list})
                return

            if api_product.dispatch(self, "GET", path, parsed_url.query, None):
                return
            self._send_json(404, {"error": f"Unknown endpoint: {path}"})
        except Exception as exc:
            self._handle_route_exception("GET", path, exc)

    def _dispatch_body_method(self, method: str) -> None:
        if not self._verify_security_envelope():
            return
        rt = self.server.runtime
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        is_owner, throttled_429, _ = api_product.evaluate_request_owner_auth(
            self, rt, path=path, method=method, audit_fn=_audit_api_event
        )
        if throttled_429:
            self._send_json(429, {"error": "Too Many Requests: owner token authentication rate limit exceeded."})
            return
        raw_json = self._read_json_body(max_bytes=_max_body_bytes_for_route(method, path, is_owner=is_owner))
        if raw_json is None:
            return
        try:
            if not api_product.dispatch(self, method, path, parsed_url.query, raw_json):
                self._send_json(404, {"error": f"Unknown endpoint: {path}"})
        except Exception as exc:
            self._handle_route_exception(method, path, exc)

    def do_PUT(self) -> None:
        self._dispatch_body_method("PUT")

    def do_DELETE(self) -> None:
        self._dispatch_body_method("DELETE")

    def do_POST(self) -> None:
        if not self._verify_security_envelope():
            return
        rt = self.server.runtime
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        is_owner, throttled_429, had_failed_owner_hdr = api_product.evaluate_request_owner_auth(
            self, rt, path=path, method="POST", audit_fn=_audit_api_event
        )
        if throttled_429:
            self._send_json(429, {"error": "Too Many Requests: owner token authentication rate limit exceeded."})
            return
        raw_json = self._read_json_body(max_bytes=_max_body_bytes_for_route("POST", path, is_owner=is_owner))
        if raw_json is None:
            return

        try:

            if path == "/v1/recall":
                if had_failed_owner_hdr:
                    _audit_api_event(rt.brain_dir, path, "POST", 403)
                req = ApiRecallRequest.model_validate(raw_json)
                projects = (req.project_scope.strip(),) if req.project_scope and req.project_scope.strip() else ()
                effective_include_prefs = bool(req.include_preferences) if is_owner else False
                if req.include_duplicates and not is_owner and not projects:
                    raise PermissionError(
                        "403 Forbidden: unscoped include_duplicates requires owner authorization or an explicit project_scope."
                    )

                fetch_n = min(25, req.n_results * 2) if not effective_include_prefs else req.n_results
                recall_kwargs: dict[str, object] = {
                    "n_results": fetch_n,
                    "follow_links": req.follow_links,
                    "include_statuses": {"accepted"},
                    "projects": projects,
                    "allow_cross_project": req.allow_cross_project,
                    "retrieval_mode": req.retrieval_mode,
                    "enable_fitness_boost": req.enable_fitness_boost,
                    "active_context_profile": tuple(req.active_context_profile),
                    "telemetry_db_path": rt.telemetry_db_path,
                }
                if "include_preferences" in inspect.signature(RecallOptions).parameters:
                    recall_kwargs["include_preferences"] = effective_include_prefs
                opts = RecallOptions(**recall_kwargs)

                consume_last_degradation_warning()
                t0 = time.perf_counter()
                direct, neighbors = search_memory(
                    req.query,
                    opts,
                    atoms_dir=rt.atoms_dir,
                    index_path=rt.lexical_index_path,
                    chroma_path=rt.chroma_path,
                )
                if not effective_include_prefs:
                    direct = [r for r in direct if not api_product.is_preference_atom_content(r.content)][: req.n_results]
                    neighbors = [r for r in neighbors if not api_product.is_preference_atom_content(r.content)]
                else:
                    direct = direct[: req.n_results]

                elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
                degradation_banner = consume_last_degradation_warning()
                try:
                    candidate_ids = peek_lexical_candidate_atom_ids(
                        req.query,
                        index_path=rt.lexical_index_path,
                        atoms_dir=rt.atoms_dir,
                    )
                    record_retrieval_telemetry(
                        [r.filename for r in direct],
                        candidate_atom_ids=candidate_ids,
                        mode=req.retrieval_mode,
                        latency_ms=elapsed_ms,
                        db_path=rt.telemetry_db_path,
                    )
                except Exception:
                    pass

                duplicates = (
                    suggest_duplicate_atoms(
                        rt.atoms_dir,
                        require_current_approval=True,
                        projects=projects,
                        brain_dir=rt.brain_dir,
                    )
                    if req.include_duplicates
                    else []
                )
                if duplicates and not effective_include_prefs:
                    pref_files = {
                        name
                        for name in (os.listdir(rt.atoms_dir) if os.path.isdir(rt.atoms_dir) else [])
                        if is_content_markdown(name)
                        and api_product._is_preference_atom_file(os.path.join(rt.atoms_dir, name))
                    }
                    if pref_files:
                        duplicates = [
                            d
                            for d in duplicates
                            if d.get("atom_a") not in pref_files and d.get("atom_b") not in pref_files
                        ]

                _audit_api_event(rt.brain_dir, path, "POST", 200)
                self._send_json(
                    200,
                    {
                        "latency_ms": elapsed_ms,
                        "degraded_to_lexical": bool(degradation_banner),
                        "degradation_warning": degradation_banner,
                        "results": [
                            {
                                "filename": r.filename,
                                "claim": _extract_claim_text(r.content),
                                "memory_type": r.memory_type,
                                "review_status": r.review_status,
                                "score": round(r.score, 4),
                                "contexts": list(r.contexts),
                                "match_reasons": list(r.match_reasons),
                            }
                            for r in direct
                        ],
                        "linked_neighbors": [
                            {
                                "filename": r.filename,
                                "claim": _extract_claim_text(r.content),
                                "match_reasons": list(r.match_reasons),
                            }
                            for r in neighbors
                        ],
                        "duplicate_suggestions": duplicates,
                    },
                )
                return

            if path == "/v1/answer":
                if had_failed_owner_hdr:
                    _audit_api_event(rt.brain_dir, path, "POST", 403)
                req = ApiAnswerRequest.model_validate(raw_json)
                projects = (req.project_scope.strip(),) if req.project_scope and req.project_scope.strip() else ()
                opts = RecallOptions(
                    n_results=req.n_results,
                    follow_links=True,
                    include_statuses={"accepted"},
                    projects=projects,
                    allow_cross_project=req.allow_cross_project,
                    retrieval_mode=req.retrieval_mode,
                )
                t0 = time.perf_counter()
                direct, neighbors = search_memory(
                    req.query,
                    opts,
                    atoms_dir=rt.atoms_dir,
                    index_path=rt.lexical_index_path,
                    chroma_path=rt.chroma_path,
                )
                ans_req = AnswerRequest(
                    query=req.query,
                    mode=req.mode,
                    project_scope=req.project_scope,
                    result_limit=req.n_results,
                )
                leads = (
                    collect_unverified_leads(
                        req.query,
                        atoms_dir=rt.atoms_dir,
                        brain_dir=rt.brain_dir,
                        project_scope=req.project_scope,
                        limit=req.n_results,
                        include_quarantined_inbox=bool(is_owner),
                    )
                    if req.mode == "research"
                    else ()
                )
                ans = generate_grounded_answer_via_host(
                    ans_req,
                    list(direct) + list(neighbors),
                    host_id=req.host_id,
                    model_override=req.model_override,
                    atoms_dir=rt.atoms_dir,
                    brain_dir=rt.brain_dir,
                    policy_path=rt.policy_path,
                    http_transport=rt.http_transport,
                    skip_dns_check_for_mock_transport=rt.skip_dns_check_for_mock_transport,
                    unverified_leads=leads if req.mode == "research" else None,
                )
                elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
                cited_atoms = [ev.atom_id for cl in ans.claims for ev in cl.evidence]
                try:
                    record_grounded_answer_telemetry(
                        cited_atoms,
                        abstained=not ans.answerable,
                        mode=req.mode,
                        latency_ms=elapsed_ms,
                        db_path=rt.telemetry_db_path,
                    )
                except Exception:
                    pass
                _audit_api_event(rt.brain_dir, path, "POST", 200)
                answer_payload: dict[str, object] = {
                    "answerable": ans.answerable,
                    "mode": req.mode,
                    "host_id": req.host_id,
                    "latency_ms": elapsed_ms,
                    "text": ans.text,
                    "reason": ans.reason,
                    "claims": [
                        {
                            "text": cl.text,
                            "is_preference": cl.is_preference,
                            "evidence": [asdict(ev) for ev in cl.evidence],
                        }
                        for cl in ans.claims
                    ],
                }
                if req.mode == "research":
                    answer_payload["unverified_leads"] = [lead.as_dict() for lead in ans.unverified_leads]
                self._send_json(200, answer_payload)
                return

            if path == "/v1/inbox":
                if had_failed_owner_hdr:
                    _audit_api_event(rt.brain_dir, path, "POST", 403)
                client_ip = self.client_address[0] if getattr(self, "client_address", None) else "127.0.0.1"
                if not api_product.check_capture_rate_limit(client_ip):
                    _audit_api_event(rt.brain_dir, path, "POST", 429)
                    self._send_json(
                        429,
                        {"error": "Too Many Requests: capture write rate limit exceeded (max 180 per minute)."},
                    )
                    return
                req = ApiInboxCaptureRequest.model_validate(raw_json)
                cleaned = "".join(ch for ch in req.content if ch in ("\n", "\r", "\t") or ord(ch) >= 32).strip()
                if not cleaned:
                    self._send_json(400, {"error": "Inbox content contains no printable characters."})
                    return
                src_clean = (
                    req.source.replace("--", "—")
                    .replace(">", "")
                    .replace("<", "")
                    .replace("\r", " ")
                    .replace("\n", " ")
                    .strip()[:200]
                )
                os.makedirs(rt.inbox_dir, exist_ok=True)
                if _is_reparse_or_link(rt.inbox_dir):
                    raise PermissionError("Refusing to write inbox capture through a symlink or reparse point.")
                slug = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m%d-%H%M%S")
                fname = f"{slug}-api-note.md"
                suffix = 1
                while True:
                    target = safe_file_in_dir(rt.inbox_dir, fname, allowed_exts={".md"}, must_exist=False)
                    if not target:
                        self._send_json(400, {"error": "Invalid inbox destination."})
                        return
                    payload_str = (
                        "<!-- second_brain_origin: api_local -->\n"
                        f"<!-- captured_via_memory_api_from: {src_clean} -->\n\n"
                        f"{cleaned}\n"
                    )
                    try:
                        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        try:
                            enc = payload_str.encode("utf-8")
                            if os.write(fd, enc) != len(enc):
                                raise OSError("Incomplete inbox write.")
                            os.fsync(fd)
                        finally:
                            os.close(fd)
                        break
                    except FileExistsError:
                        fname = f"{slug}-api-note-{suffix}.md"
                        suffix += 1
                risks = scan_inbox_payload_risks(payload_str)
                _audit_api_event(rt.brain_dir, path, "POST", 201)
                self._send_json(
                    201,
                    {
                        "filename": fname,
                        "quarantined": True,
                        "location": "0-raw/inbox/",
                        "risk_flags": list(risks),
                    },
                )
                return

            if path == "/v1/cognitive/experiences":
                if had_failed_owner_hdr:
                    _audit_api_event(rt.brain_dir, path, "POST", 403)
                req = ApiExperienceCreateRequest.model_validate(raw_json)
                saved = save_experience_record(
                    goal=req.goal,
                    result_summary=req.result_summary,
                    success=req.success,
                    context=req.context,
                    plan=req.plan,
                    actions=req.actions,
                    tools_used=req.tools_used,
                    referenced_atoms=req.referenced_atoms,
                    reasoning_summary=req.reasoning_summary,
                    confidence_before=req.confidence_before,
                    confidence_after=req.confidence_after,
                    new_memories=req.new_memories,
                    updated_memories=req.updated_memories,
                    candidate_skills=req.candidate_skills,
                    reflection=req.reflection,
                    lessons=req.lessons,
                    candidate_procedure_title=req.candidate_procedure_title,
                    propose_to_inbox=req.propose_to_inbox,
                    experiences_dir=rt.experiences_dir,
                    inbox_dir=rt.inbox_dir,
                    brain_dir=rt.brain_dir,
                )
                _audit_api_event(rt.brain_dir, path, "POST", 201)
                self._send_json(201, saved)
                return

            if path == "/v1/plan":
                if had_failed_owner_hdr:
                    _audit_api_event(rt.brain_dir, path, "POST", 403)
                req = ApiExecutivePlanRequest.model_validate(raw_json)
                plan_req = ExecutivePlanRequest(
                    goal=req.goal,
                    project_scope=req.project_scope,
                    allow_cross_project=req.allow_cross_project,
                    active_context_profile=tuple(req.active_context_profile),
                    retrieval_mode=req.retrieval_mode,
                    include_preferences=bool(req.include_preferences) if is_owner else False,
                    mode=req.mode,
                    host_id=req.host_id,
                    model_override=req.model_override,
                    max_context_chars=req.max_context_chars,
                    max_atoms=req.max_atoms,
                )
                plan_res = assemble_executive_plan(
                    plan_req,
                    brain_dir=rt.brain_dir,
                    atoms_dir=rt.atoms_dir,
                    playbooks_dir=rt.playbooks_dir,
                    lexical_index_path=rt.lexical_index_path,
                    chroma_path=rt.chroma_path,
                    telemetry_db_path=rt.telemetry_db_path,
                    policy_path=rt.policy_path,
                    http_transport=rt.http_transport,
                    skip_dns_check_for_mock_transport=rt.skip_dns_check_for_mock_transport,
                    save_briefing=False,
                )
                _audit_api_event(rt.brain_dir, path, "POST", 200)
                self._send_json(200, plan_res.as_dict())
                return

            if api_product.dispatch(self, "POST", path, parsed_url.query, raw_json):
                return
            self._send_json(404, {"error": f"Unknown endpoint: {path}"})
        except Exception as exc:
            self._handle_route_exception("POST", path, exc)


class MemoryApiClient:
    """Typed local Python client SDK for owner-built AI hosts, CLI scripts, and plugins."""

    def __init__(
        self,
        host: str = ALLOWED_API_BIND_HOST,
        port: int | None = None,
        *,
        token: str | None = None,
        owner_token: str | None = None,
        timeout: float = 45.0,
    ) -> None:
        verified_host = assert_api_loopback_bind(host)
        if port is None:
            actual_port = discover_active_api_port(DEFAULT_API_PORT)
        else:
            if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
                raise ValueError(f"Invalid loopback TCP port {port!r}; expected 1..65535.")
            actual_port = int(port)
        resolved_token = (token or "").strip() or (get_configured_api_token() or "")
        if not resolved_token:
            raise PermissionError("MemoryApiClient requires a valid bearer token.")
        self.host = verified_host
        self.port = actual_port
        self.base_url = f"http://{verified_host}:{actual_port}"
        self._owner_token = owner_token.strip() if (owner_token and owner_token.strip()) else None
        self._headers = {
            "Authorization": f"Bearer {resolved_token}",
            "Content-Type": "application/json",
            "Host": f"{verified_host}:{actual_port}",
        }
        if self._owner_token:
            self._headers[api_product.OWNER_HEADER] = self._owner_token
        self._timeout = timeout

    def _request(self, method: str, path: str, json_body: dict[str, object] | None = None) -> dict[str, object]:
        payload = json_body if json_body is not None else ({} if method in {"POST", "PUT", "DELETE"} else None)
        with httpx.Client(timeout=self._timeout, follow_redirects=False, trust_env=False) as client:
            resp = client.request(method, f"{self.base_url}{path}", headers=self._headers, json=payload)
            resp.raise_for_status()
            return dict(resp.json())

    # ─── System & Recall ─────────────────────────────────────────────────────

    def health(self) -> dict[str, object]:
        return self._request("GET", "/v1/health")

    def profile(self) -> dict[str, object]:
        return self._request("GET", "/v1/profile")

    def recall(
        self,
        query: str,
        *,
        retrieval_mode: str = "auto",
        n_results: int = 5,
        project_scope: str | None = None,
        allow_cross_project: bool = False,
        include_duplicates: bool = False,
        include_preferences: bool = False,
        follow_links: bool = True,
        enable_fitness_boost: bool = False,
        active_context_profile: list[str] | None = None,
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "query": query,
            "retrieval_mode": retrieval_mode,
            "n_results": int(n_results),
            "project_scope": project_scope,
            "allow_cross_project": bool(allow_cross_project),
            "follow_links": bool(follow_links),
            "include_duplicates": bool(include_duplicates),
            "include_preferences": bool(include_preferences),
            "enable_fitness_boost": bool(enable_fitness_boost),
            "active_context_profile": list(active_context_profile or []),
        }
        return self._request("POST", "/v1/recall", body)

    def answer(
        self,
        query: str,
        *,
        mode: str = "standard",
        retrieval_mode: str = "auto",
        host_id: str = "ollama_local",
        project_scope: str | None = None,
        model_override: str | None = None,
    ) -> dict[str, object]:
        return self._request(
            "POST",
            "/v1/answer",
            {
                "query": query,
                "mode": mode,
                "retrieval_mode": retrieval_mode,
                "host_id": host_id,
                "project_scope": project_scope,
                "model_override": model_override,
            },
        )

    # ─── Notes & Agent Capture ───────────────────────────────────────────────

    def list_notes(
        self,
        *,
        query: str = "",
        tag: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        params: list[str] = [f"limit={int(limit)}", f"offset={int(offset)}"]
        if query.strip():
            params.append(f"q={url_quote(query.strip(), safe='')}")
        if tag.strip():
            params.append(f"tag={url_quote(tag.strip(), safe=':_/-')}")
        return self._request("GET", f"/v1/notes?{'&'.join(params)}")

    def get_note(self, filename: str) -> dict[str, object]:
        return self._request("GET", f"/v1/notes/{url_quote(filename.strip(), safe='')}")

    def create_note(
        self,
        title: str = "",
        body: str = "",
        *,
        tags: list[str] | None = None,
    ) -> dict[str, object]:
        return self._request(
            "POST",
            "/v1/notes",
            {"title": title, "body": body, "tags": list(tags or [])},
        )

    def update_note(
        self,
        filename: str,
        body: str,
        *,
        expected_sha256: str,
        title: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"body": body, "expected_sha256": expected_sha256}
        if title is not None:
            payload["title"] = title
        if tags is not None:
            payload["tags"] = list(tags)
        return self._request("PUT", f"/v1/notes/{url_quote(filename.strip(), safe='')}", payload)

    def delete_note(self, filename: str, *, expected_sha256: str) -> dict[str, object]:
        return self._request(
            "DELETE",
            f"/v1/notes/{url_quote(filename.strip(), safe='')}",
            {"expected_sha256": expected_sha256},
        )

    def verify_note_excerpt(
        self,
        filename: str,
        excerpt: str,
        *,
        actor: str,
        expected_sha256: str,
        claim: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "excerpt": excerpt,
            "actor": actor,
            "expected_sha256": expected_sha256,
        }
        if claim is not None:
            payload["claim"] = claim
        return self._request(
            "POST",
            f"/v1/notes/{url_quote(filename.strip(), safe='')}/verify",
            payload,
        )

    def note_backlinks(self, filename: str) -> dict[str, object]:
        return self._request("GET", f"/v1/notes/{url_quote(filename.strip(), safe='')}/backlinks")

    def note_tasks(
        self,
        *,
        project: str | None = None,
        tag: str | None = None,
        include_completed: bool = False,
        include_raw: bool = False,
    ) -> dict[str, object]:
        params: list[str] = []
        if project and project.strip():
            params.append(f"project={url_quote(project.strip(), safe=':_/-')}")
        if tag and tag.strip():
            params.append(f"tag={url_quote(tag.strip(), safe=':_/-')}")
        if include_completed:
            params.append("include_completed=1")
        if include_raw:
            params.append("include_raw=1")
        suffix = f"?{'&'.join(params)}" if params else ""
        return self._request("GET", f"/v1/notes/tasks{suffix}")

    def remember(
        self,
        content: str,
        *,
        title: str = "",
        tags: list[str] | None = None,
        source: str = "sdk_client",
    ) -> dict[str, object]:
        return self._request(
            "POST",
            "/v1/remember",
            {"content": content, "title": title, "tags": tags or [], "source": source},
        )

    def bulk_import(
        self,
        source_dir: str,
        *,
        tags: list[str] | None = None,
        dry_run: bool = False,
        overwrite_modified: bool = False,
    ) -> dict[str, object]:
        return self._request(
            "POST",
            "/v1/import",
            {
                "source_dir": source_dir,
                "tags": list(tags or []),
                "dry_run": bool(dry_run),
                "overwrite_modified": bool(overwrite_modified),
            },
        )

    # ─── Inbox & Owner Review / Atom Governance ──────────────────────────────

    def add_to_inbox(self, content: str, *, source: str = "owner_host_sdk") -> dict[str, object]:
        return self._request("POST", "/v1/inbox", {"content": content, "source": source})

    def inbox_queue(self) -> dict[str, object]:
        return self._request("GET", "/v1/inbox/queue")

    def list_inbox(self) -> dict[str, object]:
        return self.inbox_queue()

    def inspect_inbox_item(self, filename: str) -> dict[str, object]:
        return self._request("GET", f"/v1/inbox/{url_quote(filename.strip(), safe='')}")

    def promote_inbox_item(
        self,
        filename: str,
        *,
        expected_sha256: str,
        actor: str,
        acknowledge_risks: bool = False,
    ) -> dict[str, object]:
        return self._request(
            "POST",
            "/v1/inbox/promote",
            {
                "filename": filename,
                "expected_sha256": expected_sha256,
                "actor": actor,
                "acknowledge_risks": bool(acknowledge_risks),
            },
        )

    def reject_inbox_item(
        self,
        filename: str,
        *,
        expected_sha256: str,
        actor: str,
    ) -> dict[str, object]:
        return self._request(
            "POST",
            "/v1/inbox/reject",
            {"filename": filename, "expected_sha256": expected_sha256, "actor": actor},
        )

    def list_review_queue(self) -> dict[str, object]:
        return self._request("GET", "/v1/review/queue")

    def list_atoms(
        self,
        *,
        query: str = "",
        project_scope: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, object]:
        params: list[str] = [f"limit={int(limit)}", f"offset={int(offset)}"]
        if query and query.strip():
            params.append(f"q={url_quote(query.strip(), safe='')}")
        if project_scope and project_scope.strip():
            params.append(f"project={url_quote(project_scope.strip(), safe=':_/-')}")
        return self._request("GET", f"/v1/atoms?{'&'.join(params)}")

    def get_atom(self, filename: str) -> dict[str, object]:
        return self._request("GET", f"/v1/atoms/{url_quote(filename.strip(), safe='')}")

    def transition_atom(
        self,
        filename: str,
        action: str,
        *,
        expected_sha256: str,
        actor: str,
    ) -> dict[str, object]:
        return self._request(
            "POST",
            f"/v1/atoms/{url_quote(filename.strip(), safe='')}/{url_quote(action.strip(), safe='')}",
            {"expected_sha256": expected_sha256, "actor": actor},
        )

    def edit_atom(
        self,
        filename: str,
        *,
        expected_sha256: str,
        actor: str,
        edited_content: str | None = None,
        claim: str | None = None,
        why_it_matters: str | None = None,
        contexts: list[str] | None = None,
        memory_type: str | None = None,
        epistemic_label: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "expected_sha256": expected_sha256,
            "actor": actor,
        }
        if edited_content is not None:
            payload["edited_content"] = edited_content
        if claim is not None:
            payload["claim"] = claim
        if why_it_matters is not None:
            payload["why_it_matters"] = why_it_matters
        if contexts is not None:
            payload["contexts"] = list(contexts)
        if memory_type is not None:
            payload["memory_type"] = memory_type
        if epistemic_label is not None:
            payload["epistemic_label"] = epistemic_label
        return self._request("PUT", f"/v1/atoms/{url_quote(filename.strip(), safe='')}", payload)

    def merge_atoms(
        self,
        survivor_filename: str,
        superseded_filename: str,
        *,
        survivor_expected_sha256: str,
        superseded_expected_sha256: str,
        actor: str,
        rationale: str,
    ) -> dict[str, object]:
        return self._request(
            "POST",
            "/v1/atoms/merge",
            {
                "survivor_filename": survivor_filename,
                "survivor_expected_sha256": survivor_expected_sha256,
                "superseded_filename": superseded_filename,
                "superseded_expected_sha256": superseded_expected_sha256,
                "actor": actor,
                "rationale": rationale,
            },
        )

    # ─── Cognitive, Playbooks, Planner & Hosts ───────────────────────────────

    def fitness(self) -> dict[str, object]:
        return self._request("GET", "/v1/cognitive/fitness")

    def record_feedback(
        self,
        atom_filename: str,
        *,
        helpful: bool = True,
        feedback: str | None = None,
        actor: str = "owner",
        note: str = "",
        feedback_kind: str | None = None,
    ) -> dict[str, object]:
        if feedback_kind is not None and feedback_kind.strip():
            fk_norm = feedback_kind.strip().lower()
            effective_helpful = fk_norm == "helpful"
        elif feedback is not None and feedback.strip():
            effective_helpful = feedback.strip().lower() in {"endorsed", "helpful", "positive"}
        else:
            effective_helpful = bool(helpful)
        signal = feedback if feedback is not None else (
            "endorsed"
            if effective_helpful
            else ("supersession" if (feedback_kind or "").strip().lower() == "supersession" else "corrected")
        )
        payload: dict[str, object] = {
            "atom_filename": atom_filename,
            "feedback": signal,
            "helpful": effective_helpful,
            "actor": actor,
            "note": note,
        }
        if feedback_kind is not None:
            payload["feedback_kind"] = feedback_kind
        return self._request("POST", "/v1/cognitive/feedback", payload)

    def atom_duplicates(self) -> dict[str, object]:
        return self._request("GET", "/v1/atoms/duplicates")

    def playbooks(
        self,
        *,
        project_scope: str | None = None,
        include_inactive: bool = False,
    ) -> dict[str, object]:
        params: list[str] = []
        if project_scope and project_scope.strip():
            params.append(f"project={url_quote(project_scope.strip(), safe=':_/-')}")
        if include_inactive:
            params.append("include_inactive=1")
        suffix = f"?{'&'.join(params)}" if params else ""
        return self._request("GET", f"/v1/playbooks{suffix}")

    def list_playbooks(
        self,
        *,
        project_scope: str | None = None,
        include_inactive: bool = False,
    ) -> dict[str, object]:
        return self.playbooks(project_scope=project_scope, include_inactive=include_inactive)

    def list_playbook_candidates(self) -> dict[str, object]:
        return self._request("GET", "/v1/playbooks/candidates")

    playbook_candidates = list_playbook_candidates

    def promote_playbook(
        self,
        *,
        playbook_id: str,
        title: str,
        backing_atoms: list[tuple[str, str]] | list[list[str]],
        trigger: str,
        steps: list[str],
        actor: str,
        preconditions: list[str] | None = None,
        expected_outputs: list[str] | None = None,
        failure_recovery: list[str] | None = None,
        rationale: str = "",
        allow_early_owner_override: bool = False,
        acknowledge_corrections: bool = False,
        expected_playbook_sha256: str | None = None,
        allow_multi_project: bool = False,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "playbook_id": playbook_id,
            "title": title,
            "backing_atoms": [[str(fn), str(sha)] for fn, sha in backing_atoms],
            "trigger": trigger,
            "preconditions": list(preconditions or []),
            "steps": list(steps),
            "expected_outputs": list(expected_outputs or []),
            "failure_recovery": list(failure_recovery or []),
            "actor": actor,
            "rationale": rationale,
            "allow_early_owner_override": bool(allow_early_owner_override),
            "acknowledge_corrections": bool(acknowledge_corrections),
            "allow_multi_project": bool(allow_multi_project),
        }
        if expected_playbook_sha256 is not None:
            payload["expected_playbook_sha256"] = expected_playbook_sha256
        return self._request("POST", "/v1/playbooks/promote", payload)

    def deprecate_playbook(
        self,
        filename: str,
        *,
        expected_sha256: str,
        actor: str,
        rationale: str,
    ) -> dict[str, object]:
        return self._request(
            "POST",
            f"/v1/playbooks/{url_quote(filename.strip(), safe='')}/deprecate",
            {
                "expected_sha256": expected_sha256,
                "actor": actor,
                "rationale": rationale,
            },
        )

    def list_hosts(self) -> dict[str, object]:
        return self._request("GET", "/v1/hosts")

    def update_host_policy(
        self,
        host_id: str,
        *,
        enabled: bool | None = None,
        endpoint_url: str | None = None,
        default_model: str | None = None,
        allowed_projects: list[str] | None = None,
        allow_unscoped_atoms: bool | None = None,
        allow_preference_atoms: bool | None = None,
        redact_pii: bool | None = None,
        fail_on_secret_detection: bool | None = None,
        api_key_env: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {}
        if enabled is not None:
            payload["enabled"] = bool(enabled)
        if endpoint_url is not None:
            payload["endpoint_url"] = endpoint_url
        if default_model is not None:
            payload["default_model"] = default_model
        if allowed_projects is not None:
            payload["allowed_projects"] = list(allowed_projects)
        if allow_unscoped_atoms is not None:
            payload["allow_unscoped_atoms"] = bool(allow_unscoped_atoms)
        if allow_preference_atoms is not None:
            payload["allow_preference_atoms"] = bool(allow_preference_atoms)
        if redact_pii is not None:
            payload["redact_pii"] = bool(redact_pii)
        if fail_on_secret_detection is not None:
            payload["fail_on_secret_detection"] = bool(fail_on_secret_detection)
        if api_key_env is not None:
            payload["api_key_env"] = api_key_env
        return self._request("PUT", f"/v1/hosts/{url_quote(host_id.strip(), safe='')}", payload)

    def plan(
        self,
        goal: str,
        *,
        project_scope: str | None = None,
        allow_cross_project: bool = False,
        active_context_profile: list[str] | None = None,
        retrieval_mode: str = "lexical",
        include_preferences: bool = False,
        mode: str = "deterministic",
        host_id: str = "ollama_local",
        model_override: str | None = None,
    ) -> dict[str, object]:
        return self._request(
            "POST",
            "/v1/plan",
            {
                "goal": goal,
                "project_scope": project_scope,
                "allow_cross_project": allow_cross_project,
                "active_context_profile": active_context_profile or [],
                "retrieval_mode": retrieval_mode,
                "include_preferences": include_preferences,
                "mode": mode,
                "host_id": host_id,
                "model_override": model_override,
            },
        )

    def record_experience(
        self,
        *,
        goal: str,
        result_summary: str,
        success: bool = True,
        context: list[str] | None = None,
        plan: list[str] | None = None,
        actions: list[str] | None = None,
        tools_used: list[str] | None = None,
        referenced_atoms: list[str] | None = None,
        reasoning_summary: str | None = None,
        confidence_before: float | None = None,
        confidence_after: float | None = None,
        new_memories: list[str] | None = None,
        updated_memories: list[str] | None = None,
        candidate_skills: list[str] | None = None,
        reflection: str | None = None,
        lessons: list[str] | None = None,
        candidate_procedure_title: str | None = None,
        propose_to_inbox: bool = False,
    ) -> dict[str, object]:
        return self._request(
            "POST",
            "/v1/cognitive/experiences",
            {
                "goal": goal,
                "result_summary": result_summary,
                "success": success,
                "context": context or [],
                "plan": plan or [],
                "actions": actions or [],
                "tools_used": tools_used or [],
                "referenced_atoms": referenced_atoms or [],
                "reasoning_summary": reasoning_summary,
                "confidence_before": confidence_before,
                "confidence_after": confidence_after,
                "new_memories": new_memories or [],
                "updated_memories": updated_memories or [],
                "candidate_skills": candidate_skills or [],
                "reflection": reflection,
                "lessons": lessons or [],
                "candidate_procedure_title": candidate_procedure_title,
                "propose_to_inbox": propose_to_inbox,
            },
        )

    # ─── Ledger, Knowledge Graph & Time-Travel Snapshots ─────────────────────

    def verify_ledger(self) -> dict[str, object]:
        return self._request("GET", "/v1/ledger/verify")

    def commit_ledger(self, *, actor: str = "owner", note: str = "") -> dict[str, object]:
        return self._request("POST", "/v1/ledger/commit", {"actor": actor, "note": note})

    def prove_atom(self, atom_filename: str) -> dict[str, object]:
        return self._request("GET", f"/v1/ledger/prove/{url_quote(atom_filename.strip(), safe='')}")

    def prove_leaf(self, target_ref: str) -> dict[str, object]:
        cleaned = str(target_ref or "").strip().replace("\\", "/").lstrip("/")
        segments = [
            "%2E" * len(seg) if seg in {".", ".."} else url_quote(seg, safe="")
            for seg in cleaned.split("/")
            if seg
        ]
        encoded = "/".join(segments)
        return self._request("GET", f"/v1/ledger/prove/{encoded}")

    def verify_proof(self, proof: dict[str, object]) -> dict[str, object]:
        return self._request("POST", "/v1/ledger/verify-proof", {"proof": proof})

    def graph_contradictions(
        self,
        *,
        project_scope: str | None = None,
        include_pending: bool = False,
        enable_nli: bool = False,
    ) -> dict[str, object]:
        params: list[str] = []
        if project_scope and project_scope.strip():
            params.append(f"project={url_quote(project_scope.strip(), safe=':_/-')}")
        if include_pending:
            params.append("include_pending=1")
        if enable_nli:
            params.append("nli=1")
        suffix = f"?{'&'.join(params)}" if params else ""
        return self._request("GET", f"/v1/graph/contradictions{suffix}")

    def graph_summary(self, *, project_scope: str | None = None) -> dict[str, object]:
        if project_scope and project_scope.strip():
            return self._request("GET", f"/v1/graph/summary?project={url_quote(project_scope.strip(), safe=':_/-')}")
        return self._request("GET", "/v1/graph/summary")

    def graph_neighbors(self, atom_filename: str, *, include_pending: bool = False) -> dict[str, object]:
        suffix = "?include_pending=1" if include_pending else ""
        return self._request("GET", f"/v1/graph/neighbors/{url_quote(atom_filename.strip(), safe='')}{suffix}")

    def audit_sources(self, *, demote_stale: bool = False, actor: str = "sdk_owner") -> dict[str, object]:
        if demote_stale:
            return self._request("POST", "/v1/audit/sources/demote-stale", {"actor": actor})
        return self._request("GET", "/v1/audit/sources")

    def list_snapshots(self) -> dict[str, object]:
        return self._request("GET", "/v1/snapshots")

    def create_snapshot(self, *, actor: str = "owner", label: str = "") -> dict[str, object]:
        return self._request("POST", "/v1/snapshots", {"actor": actor, "label": label})

    def diff_snapshots(self, from_snapshot: str, to_snapshot: str = "live") -> dict[str, object]:
        return self._request(
            "POST",
            "/v1/snapshots/diff",
            {"from_snapshot": from_snapshot, "to_snapshot": to_snapshot},
        )

    def recall_snapshot(
        self,
        query: str,
        snapshot_id: str,
        *,
        project_scope: str | None = None,
        limit: int = 5,
    ) -> dict[str, object]:
        return self._request(
            "POST",
            "/v1/snapshots/recall",
            {
                "query": query,
                "snapshot_id": snapshot_id,
                "project_scope": project_scope,
                "limit": int(limit),
            },
        )

    def verify_snapshot(self, snapshot_id: str) -> dict[str, object]:
        return self._request("POST", "/v1/snapshots/verify", {"snapshot_id": snapshot_id})

    def sync_status(self) -> dict[str, object]:
        return self._request("GET", "/v1/sync/status")

    def sync_conflicts(self) -> dict[str, object]:
        return self._request("GET", "/v1/sync/conflicts")

    def sync_init(
        self,
        target: str,
        *,
        backend_type: str = "local_folder",
        device_name: str | None = None,
        passphrase: str | None = None,
        bearer_token_env: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"target": target, "backend_type": backend_type}
        if device_name:
            payload["device_name"] = device_name
        if passphrase:
            payload["passphrase"] = passphrase
        if bearer_token_env:
            payload["bearer_token_env"] = bearer_token_env
        return self._request("POST", "/v1/sync/init", payload)

    def sync_join(
        self,
        target: str,
        *,
        backend_type: str = "local_folder",
        device_name: str | None = None,
        passphrase: str | None = None,
        recovery_key_b64: str | None = None,
        bearer_token_env: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"target": target, "backend_type": backend_type}
        if device_name:
            payload["device_name"] = device_name
        if passphrase:
            payload["passphrase"] = passphrase
        if recovery_key_b64:
            payload["recovery_key_b64"] = recovery_key_b64
        if bearer_token_env:
            payload["bearer_token_env"] = bearer_token_env
        return self._request("POST", "/v1/sync/join", payload)

    def sync_push(self, *, passphrase: str | None = None) -> dict[str, object]:
        payload: dict[str, object] = {}
        if passphrase:
            payload["passphrase"] = passphrase
        return self._request("POST", "/v1/sync/push", payload)

    def sync_pull(self, *, passphrase: str | None = None, trust_new_peers: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {"trust_new_peers": bool(trust_new_peers)}
        if passphrase:
            payload["passphrase"] = passphrase
        return self._request("POST", "/v1/sync/pull", payload)

    def sync_verify(self, *, passphrase: str | None = None) -> dict[str, object]:
        payload: dict[str, object] = {}
        if passphrase:
            payload["passphrase"] = passphrase
        return self._request("POST", "/v1/sync/verify", payload)

    # Convenience SDK method aliases
    inbox_item = inspect_inbox_item
    review_queue = list_review_queue
    atoms = list_atoms
    atom = get_atom
    playbook_candidates = list_playbook_candidates
    hosts = list_hosts


def _default_mcp_token_file() -> Path:
    override = (os.environ.get("BRAIN_MCP_TOKEN_FILE") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return default_api_token_path().parent / "mcp-token"


def render_host_adapter_configs(*, port: int = DEFAULT_API_PORT) -> dict[str, object]:
    """Generate local-only IDE / custom host connection descriptors without exposing secret values."""
    repo_root_path = Path(__file__).resolve().parent.parent
    repo_root = str(repo_root_path)
    if (repo_root_path / "pyproject.toml").exists():
        mcp_command = "uv"
        mcp_args = ["--directory", repo_root, "run", "--frozen", "python", "scripts/mcp_server.py"]
    else:
        mcp_command = sys.executable
        mcp_args = ["-m", "scripts.mcp_server"]
    return {
        "security_notice": (
            "Loopback-only local interfaces. Never expose 127.0.0.1 ports via reverse proxies/tunnels "
            "and never attach local MCP stdio directly to cloud-hosted agents without the outbound redaction gate."
        ),
        "loopback_http_api": {
            "base_url": f"http://{ALLOWED_API_BIND_HOST}:{port}",
            "auth_header": "Authorization: Bearer <contents of memory-api-token>",
            "owner_header": "X-Owner-Token: <contents of owner-token> (owner governance routes only)",
            "token_path": str(default_api_token_path()),
            "endpoints": [
                "GET /v1/health",
                "GET /v1/profile",
                "GET /v1/hosts",
                "PUT /v1/hosts/<host_id>",
                "GET /v1/playbooks",
                "GET /v1/playbooks/candidates",
                "POST /v1/playbooks/promote",
                "POST /v1/playbooks/<filename>/deprecate",
                "POST /v1/recall",
                "POST /v1/answer",
                "POST /v1/plan",
                "POST /v1/inbox",
                "GET /v1/inbox/queue",
                "GET /v1/inbox/<filename>",
                "POST /v1/inbox/promote",
                "POST /v1/inbox/reject",
                "GET /v1/review/queue",
                "GET /v1/atoms",
                "GET /v1/atoms/duplicates",
                "GET /v1/atoms/<filename>",
                "PUT /v1/atoms/<filename>",
                "POST /v1/atoms/merge",
                "POST /v1/atoms/<filename>/<action>",
                "GET /v1/cognitive/fitness",
                "POST /v1/cognitive/feedback",
                "POST /v1/cognitive/experiences",
                "GET /v1/notes",
                "POST /v1/notes",
                "GET /v1/notes/tasks",
                "GET /v1/notes/<filename>",
                "PUT /v1/notes/<filename>",
                "DELETE /v1/notes/<filename>",
                "POST /v1/notes/<filename>/verify",
                "GET /v1/notes/<filename>/backlinks",
                "POST /v1/remember",
                "POST /v1/import",
                "POST /v1/chat/stream",
                "POST /v1/chat/warmup",
                "GET /v1/ledger/verify",
                "POST /v1/ledger/commit",
                "POST /v1/ledger/verify-proof",
                "GET /v1/ledger/prove/<leaf>",
                "GET /v1/audit/sources",
                "POST /v1/audit/sources/demote-stale",
                "GET /v1/graph/contradictions",
                "GET /v1/graph/summary",
                "GET /v1/graph/neighbors/<filename>",
                "GET /v1/snapshots",
                "POST /v1/snapshots",
                "POST /v1/snapshots/diff",
                "POST /v1/snapshots/recall",
                "POST /v1/snapshots/verify",
                "GET /v1/sync/status",
                "GET /v1/sync/conflicts",
                "POST /v1/sync/init",
                "POST /v1/sync/join",
                "POST /v1/sync/push",
                "POST /v1/sync/pull",
                "POST /v1/sync/verify",
            ],
        },
        "local_ide_mcp_stdio": {
            "command": mcp_command,
            "args": mcp_args,
            "env": {
                "BRAIN_ENABLE_LOCAL_MCP": "1",
                "BRAIN_MCP_TRUSTED_STDIO": "1",
                "BRAIN_MCP_CLIENT_TOKEN": "<contents of mcp-token>",
                **({"BRAIN_VAULT_DIR": _resolve_default_brain_dir()} if not (repo_root_path / "pyproject.toml").exists() else {}),
            },
            "token_path": str(_default_mcp_token_file()),
            "required_request_meta": {
                "authorization": "Bearer <contents of mcp-token>",
            },
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Second Brain loopback HTTP JSON Memory API.")
    parser.add_argument("--host", default=ALLOWED_API_BIND_HOST, help="Bind interface (strictly 127.0.0.1).")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_API_PORT,
        help=f"Loopback TCP port (0..65535; 0 assigns an ephemeral port; default {DEFAULT_API_PORT}).",
    )
    parser.add_argument("--generate-token", action="store_true", help="Generate and save a fresh 256-bit API bearer token.")
    parser.add_argument("--print-adapter-config", action="store_true", help="Output IDE and custom host adapter JSON.")
    parser.add_argument(
        "--ensure-tokens",
        action="store_true",
        help="Create the agent API token and owner token if missing (used by the desktop app on first run).",
    )
    args = parser.parse_args(argv)

    if not (0 <= args.port <= 65535):
        parser.error("--port must be between 0 and 65535.")

    if args.ensure_tokens:
        if not get_configured_api_token():
            generate_memory_api_token()
        api_product.ensure_owner_token()

    if args.generate_token:
        _, saved = generate_memory_api_token()
        print(f"Generated fresh Memory API bearer token at: {saved}")
        return 0

    if args.print_adapter_config:
        print(json.dumps(render_host_adapter_configs(port=args.port), indent=2, sort_keys=True))
        return 0

    ensure_dirs()
    return serve_memory_api(args.host, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
