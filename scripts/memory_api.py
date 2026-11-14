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
from dataclasses import asdict
import datetime
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

from answer_policy import AnswerRequest
from atom_mutator import approved_revision_is_current
from cognitive_core import (
    compute_memory_fitness_report,
    peek_lexical_candidate_atom_ids,
    record_grounded_answer_telemetry,
    record_retrieval_telemetry,
    save_experience_record,
)
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
from inbox_workflow import list_inbox_items, scan_inbox_payload_risks
from index_manifest import index_manifest_is_current, load_index_manifest
from lexical_store import lexical_index_is_current
from memory_format import split_frontmatter
from memory_review import section_text
from recall import (
    RecallOptions,
    consume_last_degradation_warning,
    project_tags_match,
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
MIN_API_TOKEN_LEN = 24
MAX_API_BODY_BYTES = 65536
MAX_INBOX_API_CHARS = 16000
API_MAX_AUTH_FAILURES = 5
API_LOCKOUT_SECONDS = 60

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


def default_api_token_path() -> Path:
    override = (os.environ.get("BRAIN_MEMORY_API_TOKEN_FILE") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    elif os.environ.get("XDG_STATE_HOME"):
        base = Path(os.environ["XDG_STATE_HOME"]).expanduser()
    else:
        base = Path.home() / ".local" / "state"
    return base / "SecondBrain" / "security" / "memory-api-token"


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
    try:
        content = read_bounded_security_file(target, max_bytes=4096).strip()
    except OSError:
        return None
    return content if len(content) >= MIN_API_TOKEN_LEN else None


def reset_api_rate_limiter() -> None:
    with _API_RATE_LOCK:
        _API_AUTH_FAILURES.clear()


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
    enable_fitness_boost: bool = False
    active_context_profile: list[BoundedContextTag] = Field(default_factory=list, max_length=16)


class ApiAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(..., min_length=1, max_length=2000)
    mode: str = Field(default="standard", pattern=r"^(standard|preference)$")
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
    actions: list[BoundedExperienceItem] = Field(default_factory=list, max_length=30)
    tools_used: list[BoundedExperienceItem] = Field(default_factory=list, max_length=30)
    referenced_atoms: list[BoundedExperienceItem] = Field(default_factory=list, max_length=30)
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


class MemoryApiRuntimeConfig:
    def __init__(
        self,
        *,
        brain_dir: str = BRAIN_DIR,
        atoms_dir: str = ATOMS_DIR,
        inbox_dir: str = INBOX_DIR,
        experiences_dir: str = EXPERIENCES_DIR,
        playbooks_dir: str | None = None,
        lexical_index_path: str = LEXICAL_INDEX_PATH,
        chroma_path: str = CHROMA_PATH,
        telemetry_db_path: str = TELEMETRY_DB_PATH,
        token_path: Path | None = None,
        owner_token_path: Path | None = None,
        policy_path: Path | None = None,
        http_transport: httpx.BaseTransport | None = None,
        skip_dns_check_for_mock_transport: bool = False,
    ) -> None:
        self.brain_dir = os.path.abspath(brain_dir)
        self.atoms_dir = os.path.abspath(atoms_dir)
        self.inbox_dir = os.path.abspath(inbox_dir)
        self.experiences_dir = os.path.abspath(experiences_dir)
        if playbooks_dir is not None:
            self.playbooks_dir = os.path.abspath(playbooks_dir)
        elif self.brain_dir != os.path.abspath(BRAIN_DIR):
            self.playbooks_dir = os.path.join(self.brain_dir, "5-playbooks")
        else:
            self.playbooks_dir = os.path.abspath(PLAYBOOKS_DIR)
        self.lexical_index_path = os.path.abspath(lexical_index_path)
        self.chroma_path = os.path.abspath(chroma_path)
        self.telemetry_db_path = os.path.abspath(telemetry_db_path)
        self.token_path = token_path
        self.owner_token_path = owner_token_path
        self.policy_path = policy_path
        self.http_transport = http_transport
        self.skip_dns_check_for_mock_transport = skip_dns_check_for_mock_transport


class MemoryLoopbackHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, host: str, port: int, runtime_config: MemoryApiRuntimeConfig | None = None) -> None:
        verified_host = assert_api_loopback_bind(host)
        self.runtime = runtime_config or MemoryApiRuntimeConfig()
        expected = get_configured_api_token(self.runtime.token_path)
        if not expected:
            raise PermissionError(
                "Memory API startup aborted: no valid (>=24 char) bearer token configured. "
                "Run `second-brain serve --generate-token` first."
            )
        super().__init__((verified_host, port), MemoryApiRequestHandler)


def _audit_api_event(brain_dir: str, endpoint: str, method: str, status_code: int) -> None:
    audit_path = os.path.join(brain_dir, ".sidecar", "memory-api-audit.jsonl")
    record = {
        "schema_version": 1,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "method": method,
        "endpoint": endpoint,
        "status_code": status_code,
    }
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    with _API_AUDIT_LOCK:
        os.makedirs(os.path.dirname(audit_path), exist_ok=True)
        if os.path.lexists(audit_path) and _is_reparse_or_link(audit_path):
            raise PermissionError("Refusing to write API audit log through a symlink or reparse point.")
        fd = os.open(audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if os.write(fd, line) != len(line):
                raise OSError("Incomplete write to memory API audit log.")
            os.fsync(fd)
        finally:
            os.close(fd)


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
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
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
            msg = "Too Many Requests: authentication rate limit exceeded." if fail_code == 429 else "Unauthorized: invalid or missing bearer token."
            self._send_json(fail_code, {"error": msg})
            return False
        return True

    def _read_json_body(self) -> dict[str, object] | None:
        if self.headers.get("Transfer-Encoding") is not None:
            self._send_json(400, {"error": "Transfer-Encoding is not supported; send Content-Length."})
            return None
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._send_json(415, {"error": "Unsupported Media Type: expected application/json."})
            return None
        clen_raw = self.headers.get("Content-Length")
        if clen_raw is None:
            self._send_json(411, {"error": "Length Required."})
            return None
        try:
            clen = int(clen_raw)
        except ValueError:
            self._send_json(400, {"error": "Invalid Content-Length."})
            return None
        if clen < 0 or clen > MAX_API_BODY_BYTES:
            self._send_json(413, {"error": f"Payload Too Large: max {MAX_API_BODY_BYTES} bytes."})
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

    def do_GET(self) -> None:
        if not self._verify_security_envelope():
            return
        rt = self.server.runtime
        path = urlparse(self.path).path

        if path == "/v1/health":
            atom_files = [
                f for f in (os.listdir(rt.atoms_dir) if os.path.isdir(rt.atoms_dir) else [])
                if is_content_markdown(f)
            ]
            approved = sum(
                1 for f in atom_files
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
            hw = detect_hardware_profile(ollama_timeout=1.5, use_cache=True)
            policies = load_host_policies(rt.policy_path)
            hw_dict = asdict(hw)
            hw_dict["installed_ollama_models"] = list(hw.installed_ollama_models)
            hosts_summary = {
                hid: {
                    "provider": rule.provider,
                    "transport": rule.transport,
                    "endpoint_url": rule.endpoint_url,
                    "default_model": rule.default_model,
                    "enabled": rule.enabled,
                    "allowed_projects": list(rule.allowed_projects),
                    "allow_preference_atoms": rule.allow_preference_atoms,
                    "redact_pii": rule.redact_pii,
                }
                for hid, rule in sorted(policies.items())
            }
            _audit_api_event(rt.brain_dir, path, "GET", 200)
            self._send_json(200, {"hardware": hw_dict, "hosts": hosts_summary})
            return

        if path == "/v1/inbox/queue":
            items = list_inbox_items(rt.inbox_dir)
            _audit_api_event(rt.brain_dir, path, "GET", 200)
            self._send_json(
                200,
                {
                    "count": len(items),
                    "items": [
                        {
                            "filename": item.filename,
                            "sha256": item.sha256,
                            "size_bytes": item.size_bytes,
                            "modified_at": item.modified_at,
                            "origin": item.origin,
                            "risk_flags": list(item.risk_flags),
                        }
                        for item in items
                    ],
                },
            )
            return

        if path == "/v1/cognitive/fitness":
            report = compute_memory_fitness_report(
                atoms_dir=rt.atoms_dir,
                brain_dir=rt.brain_dir,
                db_path=rt.telemetry_db_path,
            )
            raw_pending = list(report.get("pending_review_bottlenecks") or [])
            raw_proc = list(report.get("procedure_maturity") or [])
            sanitized_report = {
                **report,
                "pending_review_bottleneck_count": len(raw_pending),
                "pending_review_bottlenecks": [
                    {k: v for k, v in item.items() if k != "atom_id"}
                    for item in raw_pending
                    if isinstance(item, dict)
                ],
                "procedure_maturity": [
                    item if item.get("approved_current") else {k: v for k, v in item.items() if k != "atom_id"}
                    for item in raw_proc
                    if isinstance(item, dict)
                ],
            }
            _audit_api_event(rt.brain_dir, path, "GET", 200)
            self._send_json(200, sanitized_report)
            return

        if path == "/v1/playbooks":
            parsed_url = urlparse(self.path)
            qs = parse_qs(parsed_url.query)
            proj_raw = (qs.get("project") or [""])[0].strip()
            if len(proj_raw) > 120:
                self._send_json(422, {"error": "Invalid project query filter."})
                return
            active_pbs = list_playbooks(
                playbooks_dir=rt.playbooks_dir,
                brain_dir=rt.brain_dir,
                atoms_dir=rt.atoms_dir,
                telemetry_db_path=rt.telemetry_db_path,
                active_only=True,
            )
            if proj_raw:
                # Require EVERY backing atom to individually match the scope (no merged-union laundering).
                active_pbs = [p for p in active_pbs if playbook_visible_in_scope(p, (proj_raw,))]
            _audit_api_event(rt.brain_dir, path, "GET", 200)
            self._send_json(
                200,
                {
                    "count": len(active_pbs),
                    "playbooks": [
                        {
                            "playbook_id": p.playbook_id,
                            "filename": p.filename,
                            "title": p.title,
                            "sha256": p.sha256,
                            "contexts": list(p.contexts),
                            "backing_atoms": list(p.backing_atoms),
                            "trigger": p.trigger,
                            "preconditions": list(p.preconditions),
                            "steps": list(p.steps),
                            "expected_outputs": list(p.expected_outputs),
                            "failure_recovery": list(p.failure_recovery),
                        }
                        for p in active_pbs
                    ],
                },
            )
            return

        if api_product.dispatch(self, "GET", path, urlparse(self.path).query, None):
            return
        self._send_json(404, {"error": f"Unknown endpoint: {path}"})

    def _dispatch_body_method(self, method: str) -> None:
        if not self._verify_security_envelope():
            return
        raw_json = self._read_json_body()
        if raw_json is None:
            return
        path = urlparse(self.path).path
        if not api_product.dispatch(self, method, path, urlparse(self.path).query, raw_json):
            self._send_json(404, {"error": f"Unknown endpoint: {path}"})

    def do_PUT(self) -> None:
        self._dispatch_body_method("PUT")

    def do_DELETE(self) -> None:
        self._dispatch_body_method("DELETE")

    def do_POST(self) -> None:
        if not self._verify_security_envelope():
            return
        raw_json = self._read_json_body()
        if raw_json is None:
            return
        rt = self.server.runtime
        path = urlparse(self.path).path

        try:
            if path == "/v1/recall":
                req = ApiRecallRequest.model_validate(raw_json)
                projects = (req.project_scope.strip(),) if req.project_scope and req.project_scope.strip() else ()
                opts = RecallOptions(
                    n_results=req.n_results,
                    follow_links=req.follow_links,
                    include_statuses={"accepted"},
                    projects=projects,
                    allow_cross_project=req.allow_cross_project,
                    retrieval_mode=req.retrieval_mode,
                    enable_fitness_boost=req.enable_fitness_boost,
                    active_context_profile=tuple(req.active_context_profile),
                    telemetry_db_path=rt.telemetry_db_path,
                )
                consume_last_degradation_warning()
                t0 = time.perf_counter()
                direct, neighbors = search_memory(
                    req.query,
                    opts,
                    atoms_dir=rt.atoms_dir,
                    index_path=rt.lexical_index_path,
                    chroma_path=rt.chroma_path,
                )
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
                self._send_json(
                    200,
                    {
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
                    },
                )
                return

            if path == "/v1/inbox":
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
                req = ApiExperienceCreateRequest.model_validate(raw_json)
                saved = save_experience_record(
                    goal=req.goal,
                    result_summary=req.result_summary,
                    success=req.success,
                    context=req.context,
                    actions=req.actions,
                    tools_used=req.tools_used,
                    referenced_atoms=req.referenced_atoms,
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
                req = ApiExecutivePlanRequest.model_validate(raw_json)
                plan_req = ExecutivePlanRequest(
                    goal=req.goal,
                    project_scope=req.project_scope,
                    allow_cross_project=req.allow_cross_project,
                    active_context_profile=tuple(req.active_context_profile),
                    retrieval_mode=req.retrieval_mode,
                    include_preferences=req.include_preferences,
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

            if api_product.dispatch(self, "POST", path, urlparse(self.path).query, raw_json):
                return
            self._send_json(404, {"error": f"Unknown endpoint: {path}"})
        except (HostConsentError, OutboundSecretBlockedError) as exc:
            _audit_api_event(rt.brain_dir, path, "POST", 403)
            self._send_json(403, {"error": str(exc)})
        except Exception as exc:
            # Pydantic ValidationError or ValueError
            if exc.__class__.__name__ == "ValidationError" or isinstance(exc, ValueError):
                self._send_json(422, {"error": str(exc)})
            else:
                self._send_json(500, {"error": f"Internal error: {exc.__class__.__name__}"})


class MemoryApiClient:
    """Typed local Python client SDK for owner-built AI hosts and plugins."""

    def __init__(self, *, port: int = DEFAULT_API_PORT, token: str | None = None, timeout: float = 45.0) -> None:
        resolved_token = (token or "").strip() or (get_configured_api_token() or "")
        if not resolved_token:
            raise PermissionError("MemoryApiClient requires a valid bearer token.")
        self.base_url = f"http://{ALLOWED_API_BIND_HOST}:{port}"
        self._headers = {
            "Authorization": f"Bearer {resolved_token}",
            "Content-Type": "application/json",
            "Host": f"{ALLOWED_API_BIND_HOST}:{port}",
        }
        self._timeout = timeout

    def _request(self, method: str, path: str, json_body: dict[str, object] | None = None) -> dict[str, object]:
        with httpx.Client(timeout=self._timeout, follow_redirects=False, trust_env=False) as client:
            resp = client.request(method, f"{self.base_url}{path}", headers=self._headers, json=json_body)
            resp.raise_for_status()
            return dict(resp.json())

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
    ) -> dict[str, object]:
        return self._request(
            "POST",
            "/v1/recall",
            {
                "query": query,
                "retrieval_mode": retrieval_mode,
                "n_results": n_results,
                "project_scope": project_scope,
                "allow_cross_project": allow_cross_project,
                "include_duplicates": include_duplicates,
            },
        )

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

    def add_to_inbox(self, content: str, *, source: str = "owner_host_sdk") -> dict[str, object]:
        return self._request("POST", "/v1/inbox", {"content": content, "source": source})

    def inbox_queue(self) -> dict[str, object]:
        return self._request("GET", "/v1/inbox/queue")

    def fitness(self) -> dict[str, object]:
        return self._request("GET", "/v1/cognitive/fitness")

    def playbooks(self, *, project_scope: str | None = None) -> dict[str, object]:
        if project_scope and project_scope.strip():
            return self._request("GET", f"/v1/playbooks?project={url_quote(project_scope.strip(), safe=':_/-')}")
        return self._request("GET", "/v1/playbooks")

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
        actions: list[str] | None = None,
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
                "actions": actions or [],
                "lessons": lessons or [],
                "candidate_procedure_title": candidate_procedure_title,
                "propose_to_inbox": propose_to_inbox,
            },
        )


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
            "token_path": str(default_api_token_path()),
            "endpoints": [
                "GET /v1/health",
                "GET /v1/profile",
                "GET /v1/playbooks",
                "POST /v1/recall",
                "POST /v1/answer",
                "POST /v1/plan",
                "POST /v1/inbox",
                "GET /v1/inbox/queue",
                "GET /v1/cognitive/fitness",
                "POST /v1/cognitive/experiences",
            ],
        },
        "local_ide_mcp_stdio": {
            "command": mcp_command,
            "args": mcp_args,
            "env": {
                "BRAIN_ENABLE_LOCAL_MCP": "1",
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
    parser.add_argument("--port", type=int, default=DEFAULT_API_PORT, help=f"Loopback TCP port (default {DEFAULT_API_PORT}).")
    parser.add_argument("--generate-token", action="store_true", help="Generate and save a fresh 256-bit API bearer token.")
    parser.add_argument("--print-adapter-config", action="store_true", help="Output IDE and custom host adapter JSON.")
    parser.add_argument(
        "--ensure-tokens",
        action="store_true",
        help="Create the agent API token and owner token if missing (used by the desktop app on first run).",
    )
    args = parser.parse_args(argv)

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
    server = MemoryLoopbackHTTPServer(args.host, args.port)
    # Machine-readable readiness line for the desktop app supervisor.
    print(f"SECOND_BRAIN_API_READY port={args.port}", flush=True)
    print(f"Second Brain Loopback Memory API listening on http://{ALLOWED_API_BIND_HOST}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
