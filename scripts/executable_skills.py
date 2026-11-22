# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Layer 6 Cognitive Core (Stage 4): Owner-promoted Executable Skills, closed sandboxed runtime,
emergency kill-switch governance, deterministic rollback, and hash-chained execution audit trail.
"""

from __future__ import annotations

import argparse
import ast
from collections.abc import Callable
from dataclasses import dataclass, field
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ in ("executable_skills", "scripts.executable_skills"):
    sys.modules.setdefault("executable_skills", sys.modules[__name__])
    sys.modules.setdefault("scripts.executable_skills", sys.modules[__name__])

from atom_mutator import review_lock
from audit_sources import audit_sources
from cognitive_core import save_experience_record
from config import (
    ATOMS_DIR,
    BRAIN_DIR,
    CHROMA_PATH,
    EXPERIENCES_DIR,
    LEXICAL_INDEX_PATH,
    PLAYBOOKS_DIR,
    TELEMETRY_DB_PATH,
    _WINDOWS_RESERVED_STEMS,
    _is_reparse_or_link,
    file_sha256,
    is_content_markdown,
    normalize_markdown_filename,
    resolve_active_brain_dir,
    resolve_under,
    safe_file_in_dir,
    security_state_dir,
)
from host_adapters import (
    OutboundSecretBlockedError,
    inspect_and_redact_outbound,
    restrict_security_file_permissions,
)
from inbox_workflow import scan_inbox_payload_risks
from knowledge_graph import detect_contradictions
from lexical_store import LexicalIndexUnavailable
from memory_format import (
    frontmatter_has_key,
    remove_frontmatter_fields,
    render_memory_document,
    set_scalar_field,
    split_frontmatter,
)
from memory_review import parse_frontmatter_values, parse_scalar_list
from recall import MemoryRecallUnavailable, RecallOptions, project_tags_match, search_memory
from skill_playbooks import (
    DISALLOWED_AUTOMATED_ACTORS,
    PlaybookIntegrityReport,
    _backup_playbook_revision,
    _effective_corrections,
    _load_telemetry_counts,
    _normalize_playbook_id,
    _parse_bullet_or_numbered_lines,
    _raw_section_block,
    _sanitize_line,
    list_playbooks,
    verify_playbook_integrity,
)
from vault_ledger import verify_vault_ledger

SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
INPUT_PLACEHOLDER_RE = re.compile(r"\{input\.([a-zA-Z0-9_.-]+)\}")
MALFORMED_INPUT_BRACE_RE = re.compile(r"\{input(?![.][a-z][a-z0-9_]{0,31}\})[^}]*\}")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
FENCED_JSON_RE = re.compile(r"^```json\s*\r?\n(.*?)\r?\n^```\s*$", re.MULTILINE | re.DOTALL)

GENESIS_EXECUTION_HASH = "0" * 64
MAX_KILL_SWITCH_FILE_BYTES = 65536
MAX_SCRIPT_BODY_BYTES = 65536
MAX_WORKSPACE_WRITE_BYTES = 65536
POLL_INTERVAL_SECONDS = 0.025
_O_BINARY = getattr(os, "O_BINARY", 0)

REQUIRED_SKILL_SECTIONS: tuple[str, ...] = (
    "Backing Playbook & Governance",
    "Input Schema",
    "Ordered Sandboxed Steps",
    "Expected Outputs & Verification",
    "Failure Modes & Rollback",
)

ALLOWED_ACTION_TYPES: frozenset[str] = frozenset(
    {
        "vault_recall",
        "vault_audit",
        "workspace_write",
        "workspace_assert",
        "sandbox_python",
    }
)

ALLOWED_AUDIT_KINDS: frozenset[str] = frozenset({"sources", "ledger", "contradictions"})
ALLOWED_ASSERT_OPS: frozenset[str] = frozenset(
    {"exists", "non_empty", "contains", "sha256_equals", "json_key_equals"}
)
ALLOWED_INPUT_TYPES: frozenset[str] = frozenset({"string", "integer", "number", "boolean"})

FORBIDDEN_SANDBOX_IMPORTS: frozenset[str] = frozenset(
    {
        "ctypes",
        "winreg",
        "pty",
        "webbrowser",
        "urllib",
        "httpx",
        "requests",
        "http",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "multiprocessing",
        "subprocess",
        "importlib",
        "builtins",
        "_imp",
        "pickle",
        "shelve",
        "marshal",
        "asyncio",
    }
)

FORBIDDEN_BUILTIN_CALLS: frozenset[str] = frozenset(
    {"eval", "exec", "compile", "__import__", "breakpoint", "input"}
)

FORBIDDEN_OS_ATTR_CALLS: frozenset[str] = frozenset(
    {
        "system",
        "popen",
        "startfile",
        "fork",
        "forkpty",
        "kill",
        "killpg",
        "symlink",
        "link",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
    }
)


class ExecutableSkillValidationError(ValueError):
    """Raised when an Executable Skill promotion, verification, governance, or runtime gate fails."""

    def __init__(self, message: str, *, audit_record: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.audit_record = audit_record


class ExecutableSkillExecutionDeniedError(PermissionError, ExecutableSkillValidationError):
    """Raised when pre-execution safety, integrity, or kill-switch gates deny execution fail-closed."""

    def __init__(self, message: str, *, audit_record: dict[str, object] | None = None) -> None:
        PermissionError.__init__(self, message)
        self.audit_record = audit_record


class ExecutableSkillSecretBlockedError(OutboundSecretBlockedError, ExecutableSkillValidationError):
    """Raised when DLP secret scanning blocks credentials in skill definitions or runtime inputs."""

    def __init__(self, message: str, *, audit_record: dict[str, object] | None = None) -> None:
        OutboundSecretBlockedError.__init__(self, message)
        self.audit_record = audit_record


@dataclass(frozen=True)
class ExecutableSkillIntegrityReport:
    skill_id: str
    filename: str
    valid: bool
    status: str
    reason: str
    title: str = ""
    sha256: str = ""
    playbook_id: str = ""
    playbook_sha256: str = ""
    contexts: tuple[str, ...] = ()
    backing_atoms: tuple[str, ...] = ()
    backing_atom_contexts: tuple[tuple[str, ...], ...] = ()
    timeout_seconds: float = 15.0
    max_output_bytes: int = 65536
    early_override: bool = False
    input_schema: dict[str, dict[str, object]] = field(default_factory=dict)
    step_actions: tuple[dict[str, object], ...] = ()
    step_actions_sha256: str = ""
    expected_outputs: tuple[str, ...] = ()
    failure_recovery: tuple[str, ...] = ()
    promoted_at: str = ""
    promoted_by: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "skill_id": self.skill_id,
            "filename": self.filename,
            "valid": self.valid,
            "status": self.status,
            "reason": self.reason,
            "title": self.title,
            "sha256": self.sha256,
            "playbook_id": self.playbook_id,
            "playbook_sha256": self.playbook_sha256,
            "contexts": list(self.contexts),
            "backing_atoms": list(self.backing_atoms),
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "early_override": self.early_override,
            "input_schema": dict(self.input_schema),
            "step_actions": [dict(s) for s in self.step_actions],
            "step_actions_sha256": self.step_actions_sha256,
            "expected_outputs": list(self.expected_outputs),
            "failure_recovery": list(self.failure_recovery),
            "promoted_at": self.promoted_at,
            "promoted_by": self.promoted_by,
        }


def default_skills_dir(brain_dir: str = BRAIN_DIR) -> str:
    """Return the canonical `6-skills/` directory under a vault root."""
    return os.path.join(brain_dir, "6-skills")


def skill_decision_log_path(brain_dir: str = BRAIN_DIR) -> str:
    return os.path.join(brain_dir, "0-raw", "archive", "skill-decisions.jsonl")


def skill_execution_log_path(brain_dir: str = BRAIN_DIR) -> str:
    return os.path.join(brain_dir, "0-raw", "archive", "skill-executions.jsonl")


def skill_kill_switch_path(security_dir: str | Path | None = None) -> Path:
    if security_dir is not None and str(security_dir).strip():
        base = Path(os.path.abspath(str(Path(security_dir).expanduser())))
    else:
        base = security_state_dir()
    return base / "skill-kill-switch.json"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _resolve_vault_dirs(
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str | None = None,
    playbooks_dir: str | None = None,
    skills_dir: str | None = None,
    experiences_dir: str | None = None,
    lexical_index_path: str | None = None,
    chroma_path: str | None = None,
    telemetry_db_path: str | None = None,
) -> dict[str, str]:
    eff_brain = os.path.abspath(brain_dir)
    same_as_default = os.path.normcase(eff_brain) == os.path.normcase(os.path.abspath(BRAIN_DIR))
    return {
        "brain_dir": eff_brain,
        "atoms_dir": os.path.abspath(atoms_dir)
        if atoms_dir is not None
        else (os.path.abspath(ATOMS_DIR) if same_as_default else os.path.join(eff_brain, "2-atoms")),
        "playbooks_dir": os.path.abspath(playbooks_dir)
        if playbooks_dir is not None
        else (os.path.abspath(PLAYBOOKS_DIR) if same_as_default else os.path.join(eff_brain, "5-playbooks")),
        "skills_dir": os.path.abspath(skills_dir)
        if skills_dir is not None
        else default_skills_dir(eff_brain),
        "experiences_dir": os.path.abspath(experiences_dir)
        if experiences_dir is not None
        else (os.path.abspath(EXPERIENCES_DIR) if same_as_default else os.path.join(eff_brain, "4-experiences")),
        "lexical_index_path": os.path.abspath(lexical_index_path)
        if lexical_index_path is not None
        else (
            os.path.abspath(LEXICAL_INDEX_PATH)
            if same_as_default
            else os.path.join(eff_brain, ".sidecar", "lexical.sqlite3")
        ),
        "chroma_path": os.path.abspath(chroma_path)
        if chroma_path is not None
        else (os.path.abspath(CHROMA_PATH) if same_as_default else os.path.join(eff_brain, ".chroma")),
        "telemetry_db_path": os.path.abspath(telemetry_db_path)
        if telemetry_db_path is not None
        else (
            os.path.abspath(TELEMETRY_DB_PATH)
            if same_as_default
            else os.path.join(eff_brain, ".sidecar", "telemetry.sqlite3")
        ),
    }


def _assert_not_reparse(path: str | Path, label: str = "path") -> None:
    p = str(path)
    if os.path.lexists(p) and _is_reparse_or_link(p):
        raise PermissionError(f"Refusing operation through symlink, junction, or reparse point ({label}): {p}")


def _normalize_skill_id(raw_id: str) -> str:
    cleaned = str(raw_id or "").strip()
    if cleaned.lower().endswith(".md"):
        cleaned = cleaned[:-3]
    if not SKILL_ID_RE.fullmatch(cleaned) or cleaned.lower() in _WINDOWS_RESERVED_STEMS:
        raise ExecutableSkillValidationError(
            f"Invalid skill_id {raw_id!r}: must match ^[a-z0-9][a-z0-9_-]{{1,63}}$ without path separators."
        )
    return cleaned


def _validate_human_actor(actor: str) -> str:
    cleaned = _sanitize_line(actor, max_len=120)
    lowered = cleaned.lower()
    prefix = lowered.split(":", 1)[0].strip()
    if not cleaned or lowered in DISALLOWED_AUTOMATED_ACTORS or prefix in DISALLOWED_AUTOMATED_ACTORS:
        raise ExecutableSkillValidationError(
            f"Executable Skill operation requires an explicit human owner actor (got {actor!r})."
        )
    return cleaned


def _validate_hex64(value: str | None, field_name: str) -> str:
    norm = str(value or "").strip().lower()
    if not HEX64_RE.fullmatch(norm):
        raise ExecutableSkillValidationError(
            f"{field_name} must be an exact 64-character lowercase hex SHA-256 digest (got {value!r})."
        )
    return norm


def _clamp_timeout_seconds(value: object) -> float:
    if isinstance(value, bool):
        raise ExecutableSkillValidationError("timeout_seconds must be a finite number, not bool.")
    try:
        numeric = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ExecutableSkillValidationError(f"Invalid timeout_seconds: {value!r}") from exc
    if not math.isfinite(numeric):
        raise ExecutableSkillValidationError(f"timeout_seconds must be finite (got {value!r}).")
    return round(max(1.0, min(120.0, numeric)), 3)


def _clamp_max_output_bytes(value: object) -> int:
    if isinstance(value, bool):
        raise ExecutableSkillValidationError("max_output_bytes must be an integer, not bool.")
    try:
        numeric = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ExecutableSkillValidationError(f"Invalid max_output_bytes: {value!r}") from exc
    return max(1024, min(1048576, numeric))


def _scan_text_for_secrets_and_risks(text: str, *, context_label: str) -> None:
    try:
        inspect_and_redact_outbound(text, redact_pii=False, fail_on_secret_detection=True)
    except OutboundSecretBlockedError as exc:
        raise ExecutableSkillSecretBlockedError(f"{context_label}: {exc}") from exc

    risks = scan_inbox_payload_risks(text)
    blocking = [r for r in risks if r != "oversized_payload"]
    if blocking:
        if any(r.startswith("secret_credential:") for r in blocking):
            raise ExecutableSkillSecretBlockedError(
                f"{context_label} blocked due to credential risk flag(s): {', '.join(blocking)}"
            )
        raise ExecutableSkillValidationError(
            f"{context_label} blocked due to security risk flag(s): {', '.join(blocking)}"
        )


def _validate_sandbox_rel_path(raw_path: object, *, field_name: str = "rel_path") -> str:
    if not isinstance(raw_path, str):
        raise ExecutableSkillValidationError(f"{field_name} must be a relative path string.")
    cleaned = raw_path.strip().replace("\\", "/")
    if (
        not cleaned
        or len(cleaned) > 120
        or "\x00" in cleaned
        or ":" in cleaned
        or cleaned.startswith("/")
        or os.path.isabs(cleaned)
        or any(ord(ch) < 32 for ch in cleaned)
    ):
        raise ExecutableSkillValidationError(f"Unsafe or absolute sandbox {field_name}: {raw_path!r}")

    segments = cleaned.split("/")
    if len(segments) > 6:
        raise ExecutableSkillValidationError(f"Sandbox {field_name} exceeds maximum directory depth: {raw_path!r}")

    normalized_parts: list[str] = []
    for seg in segments:
        if not seg or seg in {".", ".."}:
            raise ExecutableSkillValidationError(f"Path traversal or empty segment forbidden in {field_name}: {raw_path!r}")
        if seg != seg.strip() or seg.endswith("."):
            raise ExecutableSkillValidationError(f"Invalid trailing dot/space segment in {field_name}: {raw_path!r}")
        if seg.startswith("_guard") or seg.startswith("_scripts") or seg.lower() == "inputs.json":
            raise ExecutableSkillValidationError(
                f"Sandbox {field_name} cannot target reserved sandbox control path: {raw_path!r}"
            )
        stem = os.path.splitext(seg)[0].rstrip(" .").lower()
        first_stem = seg.split(".", 1)[0].rstrip(" .").lower()
        if stem in _WINDOWS_RESERVED_STEMS or first_stem in _WINDOWS_RESERVED_STEMS:
            raise ExecutableSkillValidationError(f"Reserved device name forbidden in {field_name}: {raw_path!r}")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", seg):
            raise ExecutableSkillValidationError(
                f"Disallowed characters in sandbox {field_name} segment {seg!r}: use [A-Za-z0-9._-]."
            )
        normalized_parts.append(seg)
    return "/".join(normalized_parts)


def _validate_input_schema(
    raw_schema: dict[str, dict[str, object]] | None,
) -> dict[str, dict[str, object]]:
    if raw_schema is None:
        return {}
    if not isinstance(raw_schema, dict):
        raise ExecutableSkillValidationError("input_schema must be a dictionary mapping param_name to schema spec.")
    if len(raw_schema) > 32:
        raise ExecutableSkillValidationError("input_schema cannot declare more than 32 parameters.")

    allowed_spec_keys = {"type", "required", "default", "pattern", "max_length", "description"}
    normalized: dict[str, dict[str, object]] = {}
    for raw_name, raw_spec in sorted(raw_schema.items()):
        name = str(raw_name)
        if not PARAM_NAME_RE.fullmatch(name):
            raise ExecutableSkillValidationError(
                f"Invalid input parameter name {raw_name!r}: must match ^[a-z][a-z0-9_]{{0,31}}$."
            )
        if not isinstance(raw_spec, dict):
            raise ExecutableSkillValidationError(f"Schema for parameter {name!r} must be a dict.")
        extra_keys = set(raw_spec.keys()) - allowed_spec_keys
        if extra_keys:
            raise ExecutableSkillValidationError(
                f"Unsupported keys in input_schema[{name!r}]: {sorted(extra_keys)}"
            )
        param_type = str(raw_spec.get("type") or "").strip()
        if param_type not in ALLOWED_INPUT_TYPES:
            raise ExecutableSkillValidationError(
                f"Invalid type {param_type!r} for input_schema[{name!r}]; expected one of {sorted(ALLOWED_INPUT_TYPES)}."
            )
        req_val = raw_spec.get("required", False)
        if not isinstance(req_val, bool):
            raise ExecutableSkillValidationError(f"input_schema[{name!r}]['required'] must be a boolean.")

        spec_out: dict[str, object] = {
            "type": param_type,
            "required": req_val,
        }
        if "max_length" in raw_spec and raw_spec["max_length"] is not None:
            ml = raw_spec["max_length"]
            if isinstance(ml, bool) or not isinstance(ml, int) or ml < 1 or ml > 512:
                raise ExecutableSkillValidationError(
                    f"input_schema[{name!r}]['max_length'] must be an integer in 1..512."
                )
            spec_out["max_length"] = ml
        elif param_type == "string":
            spec_out["max_length"] = 512

        if "pattern" in raw_spec and raw_spec["pattern"] is not None:
            pat = raw_spec["pattern"]
            if not isinstance(pat, str) or not pat or len(pat) > 256:
                raise ExecutableSkillValidationError(
                    f"input_schema[{name!r}]['pattern'] must be a non-empty regex string (<=256 chars)."
                )
            try:
                re.compile(pat)
            except re.error as exc:
                raise ExecutableSkillValidationError(
                    f"Invalid regex in input_schema[{name!r}]['pattern']: {exc}"
                ) from exc
            spec_out["pattern"] = pat

        if "description" in raw_spec and raw_spec["description"] is not None:
            desc = _sanitize_line(str(raw_spec["description"]), max_len=240)
            _scan_text_for_secrets_and_risks(desc, context_label=f"input_schema[{name!r}].description")
            spec_out["description"] = desc

        if "default" in raw_spec and raw_spec["default"] is not None:
            validated_default = _validate_single_input_value(name, raw_spec["default"], spec_out)
            spec_out["default"] = validated_default

        normalized[name] = spec_out
    return normalized


def _validate_single_input_value(name: str, value: object, spec: dict[str, object]) -> object:
    ptype = str(spec["type"])
    if ptype == "string":
        if not isinstance(value, str):
            raise ExecutableSkillValidationError(f"Input {name!r} must be a string.")
        if "\x00" in value or any(ord(ch) < 32 and ch not in ("\n", "\r", "\t") for ch in value):
            raise ExecutableSkillValidationError(f"Input {name!r} contains disallowed control characters.")
        if len(value) <= 8192:
            _scan_text_for_secrets_and_risks(value, context_label=f"runtime input {name!r}")
        max_len = int(spec.get("max_length", 512) or 512)
        if len(value) > min(max_len, 512):
            raise ExecutableSkillValidationError(
                f"Input {name!r} exceeds max_length={min(max_len, 512)} (got {len(value)} chars)."
            )
        _scan_text_for_secrets_and_risks(value, context_label=f"runtime input {name!r}")
        pat = spec.get("pattern")
        if isinstance(pat, str) and pat:
            if not re.fullmatch(pat, value):
                raise ExecutableSkillValidationError(f"Input {name!r} does not match required pattern {pat!r}.")
        return value

    if ptype == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ExecutableSkillValidationError(f"Input {name!r} must be an integer.")
        return int(value)

    if ptype == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ExecutableSkillValidationError(f"Input {name!r} must be a finite number.")
        return float(value)

    if ptype == "boolean":
        if not isinstance(value, bool):
            raise ExecutableSkillValidationError(f"Input {name!r} must be a boolean.")
        return bool(value)

    raise ExecutableSkillValidationError(f"Unsupported schema type {ptype!r} for {name!r}.")


def _validate_runtime_inputs(
    raw_inputs: dict[str, object] | None,
    schema: dict[str, dict[str, object]],
) -> dict[str, object]:
    if raw_inputs is None:
        provided: dict[str, object] = {}
    elif isinstance(raw_inputs, dict):
        provided = dict(raw_inputs)
    else:
        raise ExecutableSkillValidationError("Runtime inputs must be a dictionary.")

    unknown = sorted(set(provided.keys()) - set(schema.keys()))
    if unknown:
        raise ExecutableSkillValidationError(f"Unexpected runtime input parameter(s): {unknown}")

    resolved: dict[str, object] = {}
    for name, spec in sorted(schema.items()):
        if name in provided and provided[name] is not None:
            resolved[name] = _validate_single_input_value(name, provided[name], spec)
        elif "default" in spec and spec["default"] is not None:
            resolved[name] = _validate_single_input_value(name, spec["default"], spec)
        elif bool(spec.get("required", False)):
            raise ExecutableSkillValidationError(f"Missing required runtime input parameter: {name!r}")
    return resolved


def _validate_template_placeholders(
    template: str,
    input_schema: dict[str, dict[str, object]],
    *,
    field_label: str,
) -> None:
    bad_brace = MALFORMED_INPUT_BRACE_RE.search(template)
    if bad_brace:
        raise ExecutableSkillValidationError(
            f"Malformed {{input.<key>}} placeholder {bad_brace.group(0)!r} in {field_label}."
        )
    for match in INPUT_PLACEHOLDER_RE.finditer(template):
        key = match.group(1)
        if not PARAM_NAME_RE.fullmatch(key) or key not in input_schema:
            raise ExecutableSkillValidationError(
                f"Template in {field_label} references undeclared input parameter '{key}'."
            )


def _interpolate_template(template: str, validated_inputs: dict[str, object]) -> str:
    """Single-pass regex replacement so input values can NEVER trigger format-string traversal or re-interpolation."""

    def _repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in validated_inputs:
            raise ExecutableSkillValidationError(
                f"Template placeholder '{{input.{key}}}' has no bound value in runtime inputs."
            )
        val = validated_inputs[key]
        if isinstance(val, bool):
            return "true" if val else "false"
        return str(val)

    return INPUT_PLACEHOLDER_RE.sub(_repl, template)


def _validate_sandbox_python_ast(script_body: str) -> None:
    if not isinstance(script_body, str) or not script_body.strip():
        raise ExecutableSkillValidationError("sandbox_python requires a non-empty script_body string.")
    encoded_len = len(script_body.encode("utf-8"))
    if encoded_len > MAX_SCRIPT_BODY_BYTES:
        raise ExecutableSkillValidationError(
            f"sandbox_python script_body exceeds {MAX_SCRIPT_BODY_BYTES} bytes ({encoded_len}B)."
        )
    if INPUT_PLACEHOLDER_RE.search(script_body) or MALFORMED_INPUT_BRACE_RE.search(script_body):
        raise ExecutableSkillValidationError(
            "sandbox_python script_body must never interpolate {input.<key>} placeholders; "
            "read runtime inputs from read-only SB_SANDBOX_INPUTS (inputs.json) instead."
        )
    _scan_text_for_secrets_and_risks(script_body, context_label="sandbox_python.script_body")

    try:
        tree = ast.parse(script_body, filename="<sandbox_python>", mode="exec")
    except SyntaxError as exc:
        raise ExecutableSkillValidationError(f"sandbox_python syntax error: {exc}") from exc

    os_aliases: set[str] = {"os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_mod = alias.name.split(".", 1)[0].strip()
                if root_mod in FORBIDDEN_SANDBOX_IMPORTS:
                    raise ExecutableSkillValidationError(
                        f"Forbidden module import '{alias.name}' in sandbox_python (disallowed: {root_mod})."
                    )
                if root_mod == "os" and alias.asname:
                    os_aliases.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            if (node.level or 0) > 0:
                raise ExecutableSkillValidationError("Relative imports are forbidden in sandbox_python.")
            root_mod = (node.module or "").split(".", 1)[0].strip()
            if root_mod in FORBIDDEN_SANDBOX_IMPORTS:
                raise ExecutableSkillValidationError(
                    f"Forbidden from-import '{node.module}' in sandbox_python (disallowed: {root_mod})."
                )
            if root_mod == "os":
                for alias in node.names:
                    if alias.name in FORBIDDEN_OS_ATTR_CALLS or alias.name == "*":
                        raise ExecutableSkillValidationError(
                            f"Forbidden os import 'from os import {alias.name}' in sandbox_python."
                        )
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_BUILTIN_CALLS:
                raise ExecutableSkillValidationError(
                    f"Forbidden dynamic call '{func.id}(...)' in sandbox_python."
                )
            if isinstance(func, ast.Attribute):
                if func.attr in FORBIDDEN_BUILTIN_CALLS:
                    raise ExecutableSkillValidationError(
                        f"Forbidden dynamic attribute call '.{func.attr}(...)' in sandbox_python."
                    )
                if isinstance(func.value, ast.Name) and func.value.id in os_aliases:
                    if func.attr in FORBIDDEN_OS_ATTR_CALLS:
                        raise ExecutableSkillValidationError(
                            f"Forbidden OS process/shell call '{func.value.id}.{func.attr}(...)' in sandbox_python."
                        )


def _validate_and_normalize_step_actions(
    raw_step_actions: list[dict[str, object]],
    playbook_steps: tuple[str, ...],
    input_schema: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    if not isinstance(raw_step_actions, (list, tuple)) or not raw_step_actions:
        raise ExecutableSkillValidationError("step_actions must be a non-empty list of step action dicts.")
    if len(raw_step_actions) != len(playbook_steps):
        raise ExecutableSkillValidationError(
            f"1:1 step correspondence violation: len(step_actions)={len(raw_step_actions)} "
            f"must equal len(playbook.steps)={len(playbook_steps)}."
        )

    normalized_list: list[dict[str, object]] = []
    meta_keys = {"step_index", "playbook_step"}

    for idx, (raw_action, pb_step) in enumerate(zip(raw_step_actions, playbook_steps), start=1):
        if not isinstance(raw_action, dict):
            raise ExecutableSkillValidationError(f"Step {idx} action must be a dictionary.")
        if "step_index" in raw_action and raw_action["step_index"] != idx:
            raise ExecutableSkillValidationError(
                f"Step {idx} step_index mismatch: expected {idx}, got {raw_action['step_index']!r}."
            )
        if "playbook_step" in raw_action and str(raw_action["playbook_step"]) != pb_step:
            raise ExecutableSkillValidationError(
                f"Step {idx} playbook_step mismatch against backing Playbook step {idx}."
            )

        action_type = str(raw_action.get("action_type") or "").strip()
        if action_type not in ALLOWED_ACTION_TYPES:
            raise ExecutableSkillValidationError(
                f"Step {idx} uses unknown or forbidden action_type {action_type!r}; "
                f"allowed catalog: {sorted(ALLOWED_ACTION_TYPES)}."
            )

        if action_type == "vault_recall":
            allowed = {"action_type", "query", "n_results", "output_file"} | meta_keys
            extra = sorted(set(raw_action.keys()) - allowed)
            if extra:
                raise ExecutableSkillValidationError(f"Step {idx} (vault_recall) has unknown keys: {extra}")
            query = raw_action.get("query")
            if not isinstance(query, str) or not query.strip() or len(query) > 500:
                raise ExecutableSkillValidationError(
                    f"Step {idx} (vault_recall) requires a non-empty query string (<=500 chars)."
                )
            _scan_text_for_secrets_and_risks(query, context_label=f"step[{idx}].query")
            _validate_template_placeholders(query, input_schema, field_label=f"step[{idx}].query")
            n_res = raw_action.get("n_results", 5)
            if isinstance(n_res, bool) or not isinstance(n_res, int) or n_res < 1 or n_res > 10:
                raise ExecutableSkillValidationError(
                    f"Step {idx} (vault_recall) n_results must be an integer in 1..10 (got {n_res!r})."
                )
            out_file = _validate_sandbox_rel_path(raw_action.get("output_file"), field_name=f"step[{idx}].output_file")
            normalized_list.append(
                {
                    "step_index": idx,
                    "playbook_step": pb_step,
                    "action_type": "vault_recall",
                    "query": query.strip(),
                    "n_results": int(n_res),
                    "output_file": out_file,
                }
            )

        elif action_type == "vault_audit":
            allowed = {"action_type", "audit_kind", "output_file"} | meta_keys
            extra = sorted(set(raw_action.keys()) - allowed)
            if extra:
                raise ExecutableSkillValidationError(f"Step {idx} (vault_audit) has unknown keys: {extra}")
            audit_kind = str(raw_action.get("audit_kind") or "").strip()
            if audit_kind not in ALLOWED_AUDIT_KINDS:
                raise ExecutableSkillValidationError(
                    f"Step {idx} (vault_audit) audit_kind must be one of {sorted(ALLOWED_AUDIT_KINDS)} "
                    f"(got {audit_kind!r})."
                )
            out_file = _validate_sandbox_rel_path(raw_action.get("output_file"), field_name=f"step[{idx}].output_file")
            normalized_list.append(
                {
                    "step_index": idx,
                    "playbook_step": pb_step,
                    "action_type": "vault_audit",
                    "audit_kind": audit_kind,
                    "output_file": out_file,
                }
            )

        elif action_type == "workspace_write":
            allowed = {"action_type", "rel_path", "content"} | meta_keys
            extra = sorted(set(raw_action.keys()) - allowed)
            if extra:
                raise ExecutableSkillValidationError(f"Step {idx} (workspace_write) has unknown keys: {extra}")
            rel_path = _validate_sandbox_rel_path(raw_action.get("rel_path"), field_name=f"step[{idx}].rel_path")
            content = raw_action.get("content")
            if not isinstance(content, str):
                raise ExecutableSkillValidationError(f"Step {idx} (workspace_write) requires string content.")
            if len(content.encode("utf-8")) > MAX_WORKSPACE_WRITE_BYTES:
                raise ExecutableSkillValidationError(
                    f"Step {idx} (workspace_write) content exceeds {MAX_WORKSPACE_WRITE_BYTES} bytes."
                )
            _scan_text_for_secrets_and_risks(content, context_label=f"step[{idx}].content")
            _validate_template_placeholders(content, input_schema, field_label=f"step[{idx}].content")
            normalized_list.append(
                {
                    "step_index": idx,
                    "playbook_step": pb_step,
                    "action_type": "workspace_write",
                    "rel_path": rel_path,
                    "content": content,
                }
            )

        elif action_type == "workspace_assert":
            allowed = {"action_type", "rel_path", "op", "expected", "json_key"} | meta_keys
            extra = sorted(set(raw_action.keys()) - allowed)
            if extra:
                raise ExecutableSkillValidationError(f"Step {idx} (workspace_assert) has unknown keys: {extra}")
            rel_path = _validate_sandbox_rel_path(raw_action.get("rel_path"), field_name=f"step[{idx}].rel_path")
            op = str(raw_action.get("op") or "").strip()
            if op not in ALLOWED_ASSERT_OPS:
                raise ExecutableSkillValidationError(
                    f"Step {idx} (workspace_assert) op must be one of {sorted(ALLOWED_ASSERT_OPS)} (got {op!r})."
                )
            raw_expected = raw_action.get("expected", "")
            if not isinstance(raw_expected, str) or len(raw_expected) > 1024:
                raise ExecutableSkillValidationError(
                    f"Step {idx} (workspace_assert) expected must be a string (<=1024 chars)."
                )
            _scan_text_for_secrets_and_risks(raw_expected, context_label=f"step[{idx}].expected")
            _validate_template_placeholders(raw_expected, input_schema, field_label=f"step[{idx}].expected")
            if op in {"contains", "sha256_equals"} and not raw_expected:
                raise ExecutableSkillValidationError(
                    f"Step {idx} (workspace_assert op={op!r}) requires a non-empty expected string."
                )
            raw_json_key = raw_action.get("json_key", "")
            if raw_json_key is None:
                raw_json_key = ""
            if not isinstance(raw_json_key, str) or len(raw_json_key) > 128:
                raise ExecutableSkillValidationError(
                    f"Step {idx} (workspace_assert) json_key must be a string (<=128 chars)."
                )
            if op == "json_key_equals" and not raw_json_key.strip():
                raise ExecutableSkillValidationError(
                    f"Step {idx} (workspace_assert op='json_key_equals') requires a non-empty json_key."
                )
            normalized_list.append(
                {
                    "step_index": idx,
                    "playbook_step": pb_step,
                    "action_type": "workspace_assert",
                    "rel_path": rel_path,
                    "op": op,
                    "expected": raw_expected,
                    "json_key": raw_json_key.strip(),
                }
            )

        elif action_type == "sandbox_python":
            allowed = {"action_type", "script_body", "output_file"} | meta_keys
            extra = sorted(set(raw_action.keys()) - allowed)
            if extra:
                raise ExecutableSkillValidationError(f"Step {idx} (sandbox_python) has unknown keys: {extra}")
            script_body = raw_action.get("script_body")
            if not isinstance(script_body, str):
                raise ExecutableSkillValidationError(f"Step {idx} (sandbox_python) script_body must be a string.")
            norm_script = script_body.replace("\r\n", "\n").replace("\r", "\n")
            _validate_sandbox_python_ast(norm_script)
            raw_out = raw_action.get("output_file", "")
            out_file = ""
            if raw_out is not None and str(raw_out).strip():
                out_file = _validate_sandbox_rel_path(raw_out, field_name=f"step[{idx}].output_file")
            normalized_list.append(
                {
                    "step_index": idx,
                    "playbook_step": pb_step,
                    "action_type": "sandbox_python",
                    "script_body": norm_script,
                    "output_file": out_file,
                }
            )

    return normalized_list


def _compute_step_actions_sha256(step_actions: list[dict[str, object]] | tuple[dict[str, object], ...]) -> str:
    return _sha256_text(_canonical_json(list(step_actions)))


def _append_skill_decision(brain_dir: str, record: dict[str, object]) -> None:
    log_path = skill_decision_log_path(brain_dir)
    raw_root = os.path.join(brain_dir, "0-raw")
    archive_dir = os.path.dirname(log_path)
    _assert_not_reparse(brain_dir, "brain_dir")
    _assert_not_reparse(raw_root, "0-raw")
    _assert_not_reparse(archive_dir, "0-raw/archive")
    os.makedirs(archive_dir, exist_ok=True)
    _assert_not_reparse(archive_dir, "0-raw/archive")
    _assert_not_reparse(log_path, "skill-decisions.jsonl")

    encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | _O_BINARY, 0o600)
    try:
        if os.write(fd, encoded) != len(encoded):
            raise OSError("Incomplete write to skill-decisions.jsonl.")
        os.fsync(fd)
    finally:
        os.close(fd)
    restrict_security_file_permissions(log_path)


def _latest_skill_decision(brain_dir: str, filename: str) -> tuple[bool, dict[str, Any] | None]:
    log_path = skill_decision_log_path(brain_dir)
    raw_root = os.path.join(brain_dir, "0-raw")
    archive_dir = os.path.dirname(log_path)
    if _is_reparse_or_link(brain_dir) or _is_reparse_or_link(raw_root) or _is_reparse_or_link(archive_dir):
        return False, None
    if not os.path.lexists(log_path):
        return True, None
    if _is_reparse_or_link(log_path) or not os.path.isfile(log_path):
        return False, None

    latest: dict[str, Any] | None = None
    try:
        with open(log_path, "r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    return False, None
                if not isinstance(rec, dict):
                    return False, None
                if rec.get("filename") == filename:
                    latest = rec
    except OSError:
        return False, None
    return True, latest


def _count_matching_successful_episodes(
    *,
    experiences_dir: str,
    playbook_id: str,
    backing_atoms: tuple[str, ...],
) -> tuple[bool, list[str]]:
    """Return (ok, matching_experience_ids). ok=False if experiences_dir is a symlink/reparse point."""
    if os.path.lexists(experiences_dir) and _is_reparse_or_link(experiences_dir):
        return False, []
    if not os.path.isdir(experiences_dir):
        return True, []

    atom_set = {normalize_markdown_filename(a) for a in backing_atoms if str(a).strip()}
    pb_norm = playbook_id.lower()
    pb_tokens = {pb_norm, f"{pb_norm}.md", f"playbook:{pb_norm}"}
    matched_ids: list[str] = []

    for fname in sorted(os.listdir(experiences_dir)):
        if not is_content_markdown(fname):
            continue
        raw_candidate = os.path.join(experiences_dir, fname)
        if os.path.lexists(raw_candidate) and _is_reparse_or_link(raw_candidate):
            return False, []
        fpath = safe_file_in_dir(experiences_dir, fname, allowed_exts={".md"}, must_exist=True)
        if not fpath or not os.path.isfile(fpath) or _is_reparse_or_link(fpath):
            continue
        try:
            text = Path(fpath).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        parsed = split_frontmatter(text)
        if parsed is None:
            continue
        fm = parse_frontmatter_values(parsed[0])
        if str(fm.get("type") or "").strip() != "experience":
            continue
        raw_success = fm.get("success")
        is_success = raw_success is True or str(raw_success or "").strip().lower() == "true"
        if not is_success:
            continue

        ref_atoms_raw = fm.get("referenced_atoms") or []
        if isinstance(ref_atoms_raw, str):
            ref_atoms_raw = parse_scalar_list(ref_atoms_raw)
        ref_atoms = {normalize_markdown_filename(str(a)) for a in ref_atoms_raw if str(a).strip()}
        hit = bool(ref_atoms & atom_set)

        if not hit:
            for list_field in ("candidate_skills", "tools_used", "context", "plan", "actions"):
                vals = fm.get(list_field) or []
                if isinstance(vals, str):
                    vals = parse_scalar_list(vals)
                for item in vals:
                    norm_item = str(item).strip().lower()
                    if norm_item in pb_tokens or f"playbook:{pb_norm}" in norm_item:
                        hit = True
                        break
                if hit:
                    break

        if not hit:
            cand_proc = str(fm.get("candidate_procedure") or "").strip().lower()
            if cand_proc in pb_tokens:
                hit = True

        if hit:
            exp_id = str(fm.get("experience_id") or os.path.splitext(fname)[0]).strip()
            matched_ids.append(exp_id)

    return True, matched_ids


def _evaluate_playbook_skill_readiness(
    pb: PlaybookIntegrityReport,
    *,
    brain_dir: str,
    playbooks_dir: str,
    experiences_dir: str,
    telemetry_db_path: str,
) -> dict[str, object]:
    pb_path = safe_file_in_dir(playbooks_dir, pb.filename, allowed_exts={".md"}, must_exist=True)
    ack_corr_map: dict[str, int] = {}
    if pb_path and os.path.isfile(pb_path) and not _is_reparse_or_link(pb_path):
        try:
            pb_parsed = split_frontmatter(Path(pb_path).read_text(encoding="utf-8"))
            if pb_parsed is not None:
                pb_fm = parse_frontmatter_values(pb_parsed[0])
                raw_ack = pb_fm.get("acknowledged_corrections")
                if isinstance(raw_ack, str) and raw_ack.strip().startswith("{"):
                    loaded_ack = json.loads(raw_ack)
                    if isinstance(loaded_ack, dict):
                        ack_corr_map = {str(k): int(v or 0) for k, v in loaded_ack.items()}
        except (OSError, UnicodeDecodeError, ValueError, TypeError):
            ack_corr_map = {}

    atom_maturity: list[dict[str, object]] = []
    citations_list: list[int] = []
    corrections_list: list[int] = []
    unack_list: list[int] = []
    telemetry_verifiable = True

    for atom_id in pb.backing_atoms:
        tele = _load_telemetry_counts(atom_id, telemetry_db_path)
        eff_corr = _effective_corrections(atom_id, telemetry_db_path, brain_dir)
        if tele is None or eff_corr is None:
            telemetry_verifiable = False
            citations_val = 0
            corr_val = 1
            unack_val = 1
        else:
            citations_val = int(tele[0])
            corr_val = int(eff_corr)
            ack_val = int(ack_corr_map.get(atom_id, 0) or 0)
            unack_val = max(0, corr_val - ack_val)

        citations_list.append(citations_val)
        corrections_list.append(corr_val)
        unack_list.append(unack_val)
        atom_maturity.append(
            {
                "atom_id": atom_id,
                "grounded_citations": citations_val,
                "corrections": corr_val,
                "unacknowledged_corrections": unack_val,
            }
        )

    exp_ok, matching_exp_ids = _count_matching_successful_episodes(
        experiences_dir=experiences_dir,
        playbook_id=pb.playbook_id,
        backing_atoms=pb.backing_atoms,
    )
    if not exp_ok:
        telemetry_verifiable = False

    min_citations = min(citations_list) if citations_list else 0
    max_corrections = max(corrections_list) if corrections_list else 0
    total_unack = sum(unack_list)
    successful_episodes = len(matching_exp_ids)

    eligible_without_override = bool(
        telemetry_verifiable
        and citations_list
        and all(c >= 3 for c in citations_list)
        and max_corrections == 0
        and total_unack == 0
        and (successful_episodes >= 1 or all(c >= 5 for c in citations_list))
    )
    return {
        "telemetry_verifiable": telemetry_verifiable,
        "atom_maturity": atom_maturity,
        "min_grounded_citations": min_citations,
        "max_corrections": max_corrections,
        "unacknowledged_corrections": total_unack,
        "successful_episodes": successful_episodes,
        "matching_experience_ids": matching_exp_ids,
        "eligible_without_override": eligible_without_override,
    }


def get_skill_kill_switch_status(
    *,
    brain_dir: str | None = BRAIN_DIR,
    security_dir: str | Path | None = None,
) -> dict[str, object]:
    """Read emergency kill-switch state; fail closed (`global_engaged=True`) on symlinks or corrupt JSON."""
    _ = brain_dir
    ks_path = skill_kill_switch_path(security_dir)
    ks_str = str(ks_path)
    parent_str = str(ks_path.parent)

    def _fail_closed(reason: str) -> dict[str, object]:
        return {
            "global_engaged": True,
            "global_reason": reason,
            "global_actor": "fail_closed_guard",
            "global_updated_at": "",
            "killed_skills": {},
            "corrupt": True,
            "path": ks_str,
        }

    if os.path.lexists(parent_str) and _is_reparse_or_link(parent_str):
        return _fail_closed("symlinked_kill_switch_parent_directory")
    if not os.path.lexists(ks_str):
        return {
            "global_engaged": False,
            "global_reason": "",
            "global_actor": "",
            "global_updated_at": "",
            "killed_skills": {},
            "corrupt": False,
            "path": ks_str,
        }
    if _is_reparse_or_link(ks_str):
        return _fail_closed("symlinked_kill_switch_file")

    try:
        st = os.lstat(ks_str)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_KILL_SWITCH_FILE_BYTES:
            return _fail_closed("invalid_or_oversized_kill_switch_file")
        raw_text = ks_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _fail_closed("corrupt_kill_switch_json")

    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 1
        or not isinstance(data.get("global_engaged"), bool)
        or not isinstance(data.get("killed_skills"), dict)
    ):
        return _fail_closed("invalid_kill_switch_schema")

    active_killed_skills: dict[str, dict[str, object]] = {}
    for raw_sid, entry in data["killed_skills"].items():
        if not isinstance(raw_sid, str) or not SKILL_ID_RE.fullmatch(raw_sid) or not isinstance(entry, dict):
            return _fail_closed("invalid_killed_skill_entry")
        if not isinstance(entry.get("engaged"), bool):
            return _fail_closed("invalid_killed_skill_flag")
        if entry["engaged"]:
            active_killed_skills[raw_sid] = {
                "engaged": True,
                "reason": str(entry.get("reason") or ""),
                "actor": str(entry.get("actor") or ""),
                "updated_at": str(entry.get("updated_at") or ""),
            }

    return {
        "global_engaged": bool(data["global_engaged"]),
        "global_reason": str(data.get("global_reason") or ""),
        "global_actor": str(data.get("global_actor") or ""),
        "global_updated_at": str(data.get("global_updated_at") or ""),
        "killed_skills": active_killed_skills,
        "corrupt": False,
        "path": ks_str,
    }


def _is_skill_killed(ks_status: dict[str, object], skill_id: str) -> tuple[bool, str]:
    if bool(ks_status.get("global_engaged")):
        reason = str(ks_status.get("global_reason") or "global")
        return True, f"global:{reason}"
    killed_map = ks_status.get("killed_skills")
    if isinstance(killed_map, dict) and skill_id in killed_map:
        entry = killed_map[skill_id]
        reason = str(entry.get("reason") or skill_id) if isinstance(entry, dict) else skill_id
        return True, f"skill:{skill_id}:{reason}"
    return False, ""


def set_skill_kill_switch(
    *,
    engaged: bool,
    scope: str = "global",
    actor: str,
    reason: str = "",
    brain_dir: str | None = BRAIN_DIR,
    security_dir: str | Path | None = None,
) -> dict[str, object]:
    """Engage or release the global or per-skill emergency kill-switch with owner attribution."""
    if not isinstance(engaged, bool):
        raise ExecutableSkillValidationError("engaged must be a boolean.")
    clean_actor = _validate_human_actor(actor)
    clean_reason = _sanitize_line(reason, max_len=500)
    if engaged and not clean_reason:
        raise ExecutableSkillValidationError("Engaging the skill kill-switch requires a non-empty reason.")

    raw_scope = str(scope or "").strip()
    norm_scope = "global" if raw_scope.lower() == "global" else _normalize_skill_id(raw_scope)

    ks_path = skill_kill_switch_path(security_dir)
    _assert_not_reparse(ks_path.parent, "security_dir")
    ks_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_not_reparse(ks_path.parent, "security_dir")
    _assert_not_reparse(ks_path, "skill-kill-switch.json")

    current = get_skill_kill_switch_status(brain_dir=brain_dir, security_dir=security_dir)
    if bool(current.get("corrupt")):
        if not engaged and norm_scope != "global":
            raise ExecutableSkillValidationError(
                "Kill-switch file is corrupt (fail-closed global lock active); "
                "release scope='global' explicitly after inspection to reset."
            )
        global_engaged = True if norm_scope != "global" else False
        global_reason = str(current.get("global_reason") or "") if global_engaged else ""
        global_actor = clean_actor
        global_updated_at = ""
        killed_skills: dict[str, dict[str, object]] = {}
    else:
        global_engaged = bool(current["global_engaged"])
        global_reason = str(current["global_reason"])
        global_actor = str(current["global_actor"])
        global_updated_at = str(current["global_updated_at"])
        killed_skills = {
            str(k): dict(v)
            for k, v in dict(current.get("killed_skills") or {}).items()  # type: ignore[arg-type]
            if isinstance(v, dict)
        }

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    if norm_scope == "global":
        global_engaged = engaged
        global_reason = clean_reason if engaged else ""
        global_actor = clean_actor
        global_updated_at = now_iso
    else:
        if engaged:
            killed_skills[norm_scope] = {
                "engaged": True,
                "reason": clean_reason,
                "actor": clean_actor,
                "updated_at": now_iso,
            }
        else:
            killed_skills.pop(norm_scope, None)

    payload = {
        "schema_version": 1,
        "global_engaged": global_engaged,
        "global_reason": global_reason,
        "global_actor": global_actor,
        "global_updated_at": global_updated_at,
        "killed_skills": killed_skills,
        "updated_at": now_iso,
    }
    encoded = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")

    fd, tmp_path = tempfile.mkstemp(dir=str(ks_path.parent), prefix=".skill-ks-", suffix=".tmp")
    closed = False
    try:
        if os.name != "nt":
            os.chmod(tmp_path, 0o600)
        if os.write(fd, encoded) != len(encoded):
            raise OSError("Incomplete write to skill-kill-switch.json.")
        os.fsync(fd)
        os.close(fd)
        closed = True
        os.replace(tmp_path, ks_path)
        restrict_security_file_permissions(ks_path)
    finally:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    if brain_dir and str(brain_dir).strip() and os.path.lexists(str(brain_dir)):
        _assert_not_reparse(brain_dir, "brain_dir")
        if os.path.isdir(str(brain_dir)):
            lock_path = os.path.join(str(brain_dir), ".sidecar", "writer.lock")
            _assert_not_reparse(os.path.dirname(lock_path), ".sidecar")
            os.makedirs(os.path.dirname(lock_path), exist_ok=True)
            with review_lock(lock_path):
                _append_skill_decision(
                    str(brain_dir),
                    {
                        "schema_version": 1,
                        "timestamp": now_iso,
                        "action": "engage_kill_switch" if engaged else "release_kill_switch",
                        "scope": norm_scope,
                        "engaged": engaged,
                        "actor": clean_actor,
                        "reason": clean_reason,
                    },
                )

    return get_skill_kill_switch_status(brain_dir=brain_dir, security_dir=security_dir)


def skill_visible_in_scope(skill: ExecutableSkillIntegrityReport, projects: tuple[str, ...]) -> bool:
    """Require EVERY backing procedural atom of an Executable Skill to match the caller's project scope."""
    clean = tuple(p for p in projects if str(p or "").strip())
    if not clean:
        return True
    if not skill.backing_atom_contexts or len(skill.backing_atom_contexts) != len(skill.backing_atoms):
        return False
    return all(bool(project_tags_match(tuple(ctx), clean)) for ctx in skill.backing_atom_contexts)


def inspect_executable_skill_candidates(
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str | None = None,
    playbooks_dir: str | None = None,
    skills_dir: str | None = None,
    experiences_dir: str | None = None,
    telemetry_db_path: str | None = None,
    security_dir: str | Path | None = None,
) -> list[dict[str, object]]:
    """Inspect active, verified Playbooks and evaluate promotion readiness for Stage 4 Executable Skills."""
    dirs = _resolve_vault_dirs(
        brain_dir=brain_dir,
        atoms_dir=atoms_dir,
        playbooks_dir=playbooks_dir,
        skills_dir=skills_dir,
        experiences_dir=experiences_dir,
        telemetry_db_path=telemetry_db_path,
    )
    _assert_not_reparse(dirs["brain_dir"], "brain_dir")
    if os.path.lexists(dirs["skills_dir"]):
        _assert_not_reparse(dirs["skills_dir"], "skills_dir")

    active_pbs = list_playbooks(
        playbooks_dir=dirs["playbooks_dir"],
        brain_dir=dirs["brain_dir"],
        atoms_dir=dirs["atoms_dir"],
        telemetry_db_path=dirs["telemetry_db_path"],
        active_only=True,
    )
    active_skills = list_executable_skills(
        skills_dir=dirs["skills_dir"],
        brain_dir=dirs["brain_dir"],
        atoms_dir=dirs["atoms_dir"],
        playbooks_dir=dirs["playbooks_dir"],
        telemetry_db_path=dirs["telemetry_db_path"],
        security_dir=security_dir,
        active_only=True,
    )
    skills_by_playbook: dict[str, list[str]] = {}
    for sk in active_skills:
        skills_by_playbook.setdefault(sk.playbook_id, []).append(sk.skill_id)

    candidates: list[dict[str, object]] = []
    for pb in active_pbs:
        readiness = _evaluate_playbook_skill_readiness(
            pb,
            brain_dir=dirs["brain_dir"],
            playbooks_dir=dirs["playbooks_dir"],
            experiences_dir=dirs["experiences_dir"],
            telemetry_db_path=dirs["telemetry_db_path"],
        )
        promoted_ids = sorted(skills_by_playbook.get(pb.playbook_id, []))
        eligible = bool(readiness["eligible_without_override"])
        candidates.append(
            {
                "playbook_id": pb.playbook_id,
                "playbook_filename": pb.filename,
                "playbook_sha256": pb.sha256,
                "title": pb.title,
                "step_count": len(pb.steps),
                "steps": list(pb.steps),
                "contexts": list(pb.contexts),
                "backing_atoms": list(pb.backing_atoms),
                "atom_maturity": readiness["atom_maturity"],
                "min_grounded_citations": readiness["min_grounded_citations"],
                "max_corrections": readiness["max_corrections"],
                "unacknowledged_corrections": readiness["unacknowledged_corrections"],
                "successful_episodes": readiness["successful_episodes"],
                "matching_experience_ids": readiness["matching_experience_ids"],
                "already_promoted_skill_ids": promoted_ids,
                "already_has_active_skill": bool(promoted_ids),
                "eligible_without_override": eligible,
                "maturity_stage": "executable_skill_ready" if eligible else "playbook_verified",
            }
        )
    return candidates


def promote_playbook_to_executable_skill(
    *,
    skill_id: str,
    playbook_id: str,
    expected_playbook_sha256: str,
    step_actions: list[dict[str, object]],
    input_schema: dict[str, dict[str, object]] | None = None,
    timeout_seconds: float = 15.0,
    max_output_bytes: int = 65536,
    actor: str,
    rationale: str = "",
    allow_early_owner_override: bool = False,
    expected_skill_sha256: str | None = None,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str | None = None,
    playbooks_dir: str | None = None,
    skills_dir: str | None = None,
    experiences_dir: str | None = None,
    telemetry_db_path: str | None = None,
    security_dir: str | Path | None = None,
) -> dict[str, object]:
    """CAS-locked owner promotion of a verified Playbook into a closed-action Executable Skill in `6-skills/`."""
    norm_skill_id = _normalize_skill_id(skill_id)
    try:
        norm_pb_id = _normalize_playbook_id(playbook_id)
    except Exception as exc:
        raise ExecutableSkillValidationError(str(exc)) from exc
    expected_pb_sha = _validate_hex64(expected_playbook_sha256, "expected_playbook_sha256")
    clean_actor = _validate_human_actor(actor)
    clean_rationale = _sanitize_line(rationale, max_len=500)
    if clean_rationale:
        _scan_text_for_secrets_and_risks(clean_rationale, context_label="rationale")
    clamped_timeout = _clamp_timeout_seconds(timeout_seconds)
    clamped_max_output = _clamp_max_output_bytes(max_output_bytes)
    normalized_schema = _validate_input_schema(input_schema)

    dirs = _resolve_vault_dirs(
        brain_dir=brain_dir,
        atoms_dir=atoms_dir,
        playbooks_dir=playbooks_dir,
        skills_dir=skills_dir,
        experiences_dir=experiences_dir,
        telemetry_db_path=telemetry_db_path,
    )
    _assert_not_reparse(dirs["brain_dir"], "brain_dir")
    _assert_not_reparse(dirs["playbooks_dir"], "playbooks_dir")
    _assert_not_reparse(dirs["atoms_dir"], "atoms_dir")

    sidecar_dir = os.path.join(dirs["brain_dir"], ".sidecar")
    _assert_not_reparse(sidecar_dir, ".sidecar")
    os.makedirs(sidecar_dir, exist_ok=True)
    _assert_not_reparse(dirs["skills_dir"], "skills_dir")
    os.makedirs(dirs["skills_dir"], exist_ok=True)
    _assert_not_reparse(dirs["skills_dir"], "skills_dir")

    ks_status = get_skill_kill_switch_status(brain_dir=dirs["brain_dir"], security_dir=security_dir)
    killed, ks_reason = _is_skill_killed(ks_status, norm_skill_id)
    if killed:
        raise ExecutableSkillValidationError(
            f"Cannot promote Executable Skill '{norm_skill_id}' while kill-switch is engaged ({ks_reason})."
        )

    lock_path = os.path.join(sidecar_dir, "writer.lock")
    with review_lock(lock_path):
        pb_filename = f"{norm_pb_id}.md"
        pb_path = os.path.join(dirs["playbooks_dir"], pb_filename)
        pb_rep = verify_playbook_integrity(
            pb_path,
            brain_dir=dirs["brain_dir"],
            atoms_dir=dirs["atoms_dir"],
            playbooks_dir=dirs["playbooks_dir"],
            telemetry_db_path=dirs["telemetry_db_path"],
        )
        if not pb_rep.valid or pb_rep.status != "active":
            raise ExecutableSkillValidationError(
                f"Backing Playbook '{norm_pb_id}' failed integrity verification ({pb_rep.status}: {pb_rep.reason})."
            )
        if pb_rep.sha256 != expected_pb_sha:
            raise ExecutableSkillValidationError(
                f"CAS hash mismatch on backing Playbook '{norm_pb_id}': "
                f"expected {expected_pb_sha}, found {pb_rep.sha256}."
            )

        readiness = _evaluate_playbook_skill_readiness(
            pb_rep,
            brain_dir=dirs["brain_dir"],
            playbooks_dir=dirs["playbooks_dir"],
            experiences_dir=dirs["experiences_dir"],
            telemetry_db_path=dirs["telemetry_db_path"],
        )
        if not readiness["telemetry_verifiable"]:
            raise ExecutableSkillValidationError(
                f"Telemetry/correction/experience state for Playbook '{norm_pb_id}' is unverifiable; "
                "refusing promotion (fail closed)."
            )
        if int(readiness["unacknowledged_corrections"]) > 0 or int(readiness["max_corrections"]) > 0:
            if not (allow_early_owner_override and clean_rationale):
                raise ExecutableSkillValidationError(
                    f"Backing procedural atom(s) of Playbook '{norm_pb_id}' have recorded corrections; "
                    "pass allow_early_owner_override=True with an explicit human rationale after review."
                )
        if not readiness["eligible_without_override"] and not (allow_early_owner_override and clean_rationale):
            raise ExecutableSkillValidationError(
                f"Playbook '{norm_pb_id}' has not yet reached autonomous skill promotion maturity "
                f"(min_citations={readiness['min_grounded_citations']}, "
                f"successful_episodes={readiness['successful_episodes']}); "
                "require >=3 citations + (>=1 successful episode or >=5 citations), or pass "
                "allow_early_owner_override=True with a non-empty human rationale."
            )

        normalized_steps = _validate_and_normalize_step_actions(
            step_actions,
            pb_rep.steps,
            normalized_schema,
        )
        steps_sha256 = _compute_step_actions_sha256(normalized_steps)

        verified_atom_hashes: dict[str, str] = {}
        for atom_name in pb_rep.backing_atoms:
            apath = safe_file_in_dir(dirs["atoms_dir"], atom_name, allowed_exts={".md"}, must_exist=True)
            if not apath or not os.path.isfile(apath) or _is_reparse_or_link(apath):
                raise ExecutableSkillValidationError(f"Backing procedural atom missing or unsafe: {atom_name!r}")
            asha = file_sha256(apath)
            if not asha:
                raise ExecutableSkillValidationError(f"Could not hash backing atom: {atom_name!r}")
            verified_atom_hashes[atom_name] = asha

        skill_filename = f"{norm_skill_id}.md"
        raw_target = os.path.join(dirs["skills_dir"], skill_filename)
        _assert_not_reparse(raw_target, skill_filename)
        target_path = safe_file_in_dir(dirs["skills_dir"], skill_filename, allowed_exts={".md"}, must_exist=False)
        if not target_path:
            raise ExecutableSkillValidationError(f"Unsafe skill target filename: {skill_filename!r}")

        previous_sha: str | None = None
        if os.path.lexists(target_path):
            _assert_not_reparse(target_path, skill_filename)
            if not os.path.isfile(target_path):
                raise PermissionError(f"Refusing to overwrite non-regular skill file: {skill_filename}")
            existing_text = Path(target_path).read_text(encoding="utf-8")
            existing_parsed = split_frontmatter(existing_text)
            existing_fm = parse_frontmatter_values(existing_parsed[0]) if existing_parsed else {}
            if str(existing_fm.get("type") or "") != "executable_skill":
                raise ExecutableSkillValidationError(
                    f"Refusing to overwrite non-executable-skill file '{skill_filename}' in 6-skills/."
                )
            previous_sha = file_sha256(target_path)

        if previous_sha:
            expected_sk_norm = _validate_hex64(expected_skill_sha256, "expected_skill_sha256")
            if expected_sk_norm != previous_sha:
                raise ExecutableSkillValidationError(
                    f"Existing Executable Skill '{skill_filename}' requires matching expected_skill_sha256 "
                    f"(expected {expected_skill_sha256!r}, found {previous_sha})."
                )
            _backup_playbook_revision(dirs["skills_dir"], target_path, f"{norm_skill_id}.{previous_sha[:12]}.md")
        elif expected_skill_sha256 is not None and str(expected_skill_sha256).strip():
            raise ExecutableSkillValidationError(
                f"Executable Skill '{skill_filename}' does not exist yet; omit expected_skill_sha256 for initial promotion."
            )

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        clean_title = _sanitize_line(pb_rep.title, max_len=200)
        input_schema_canonical = _canonical_json(normalized_schema)
        pretty_schema_json = json.dumps(normalized_schema, indent=2, sort_keys=True, ensure_ascii=False)
        pretty_steps_json = json.dumps(normalized_steps, indent=2, sort_keys=True, ensure_ascii=False)

        md_lines = [
            "---",
            f"skill_id: {json.dumps(norm_skill_id)}",
            "type: executable_skill",
            "skill_status: active",
            f"title: {json.dumps(clean_title, ensure_ascii=False)}",
            f"playbook_id: {json.dumps(pb_rep.playbook_id)}",
            f"playbook_sha256: {json.dumps(pb_rep.sha256)}",
            f"backing_atoms: {json.dumps(sorted(verified_atom_hashes.keys()), ensure_ascii=False)}",
            f"backing_atom_sha256s: {json.dumps(verified_atom_hashes, sort_keys=True)}",
            f"contexts: {json.dumps(list(pb_rep.contexts), ensure_ascii=False)}",
            f"promoted_at: {json.dumps(now_iso)}",
            f"promoted_by: {json.dumps(clean_actor, ensure_ascii=False)}",
            f"timeout_seconds: {json.dumps(clamped_timeout)}",
            f"max_output_bytes: {json.dumps(clamped_max_output)}",
            f"early_override: {'true' if allow_early_owner_override else 'false'}",
            f"input_schema_json: {json.dumps(input_schema_canonical, ensure_ascii=False)}",
            f"step_actions_sha256: {json.dumps(steps_sha256)}",
            "---",
            "",
            f"# {clean_title}",
            "",
            "## Backing Playbook & Governance",
            f"- Playbook: `{pb_rep.playbook_id}` (`{pb_rep.sha256}`)",
            f"- Backing Atoms: {', '.join(f'`{a}`' for a in sorted(verified_atom_hashes.keys()))}",
            f"- Promoted By: `{clean_actor}` at `{now_iso}`",
            f"- Sandbox Bounds: `timeout_seconds={clamped_timeout}`, `max_output_bytes={clamped_max_output}`",
            "",
            "## Input Schema",
            "```json",
            pretty_schema_json,
            "```",
            "",
            "## Ordered Sandboxed Steps",
            *(
                f"{s['step_index']}. [{s['action_type']}] {s['playbook_step']}"
                for s in normalized_steps
            ),
            "",
            "```json",
            pretty_steps_json,
            "```",
            "",
            "## Expected Outputs & Verification",
            *(f"- {item}" for item in pb_rep.expected_outputs),
            "",
            "## Failure Modes & Rollback",
            *(f"- {item}" for item in pb_rep.failure_recovery),
            "",
        ]
        encoded = "\n".join(md_lines).encode("utf-8")

        if previous_sha is None:
            o_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY
            try:
                fd = os.open(target_path, o_flags, 0o600)
            except FileExistsError as exc:
                raise ExecutableSkillValidationError(
                    f"Executable Skill '{skill_filename}' appeared concurrently during initial promotion."
                ) from exc
            try:
                if os.name != "nt":
                    os.chmod(target_path, 0o600)
                if os.write(fd, encoded) != len(encoded):
                    raise OSError("Incomplete Executable Skill write.")
                os.fsync(fd)
            finally:
                os.close(fd)
        else:
            fd, tmp_path = tempfile.mkstemp(dir=dirs["skills_dir"], prefix=f".{norm_skill_id}-", suffix=".tmp")
            closed = False
            try:
                if os.name != "nt":
                    os.chmod(tmp_path, 0o600)
                if os.write(fd, encoded) != len(encoded):
                    raise OSError("Incomplete Executable Skill update write.")
                os.fsync(fd)
                os.close(fd)
                closed = True
                os.replace(tmp_path, target_path)
            finally:
                if not closed:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

        new_sha = file_sha256(target_path) or ""
        decision = {
            "schema_version": 1,
            "timestamp": now_iso,
            "action": "promote_executable_skill",
            "skill_id": norm_skill_id,
            "filename": skill_filename,
            "previous_sha256": previous_sha,
            "new_sha256": new_sha,
            "playbook_id": pb_rep.playbook_id,
            "playbook_sha256": pb_rep.sha256,
            "backing_atom_sha256s": verified_atom_hashes,
            "step_actions_sha256": steps_sha256,
            "actor": clean_actor,
            "allow_early_owner_override": bool(allow_early_owner_override),
            "rationale": clean_rationale,
        }
        _append_skill_decision(dirs["brain_dir"], decision)
        return {
            "skill_id": norm_skill_id,
            "filename": skill_filename,
            "sha256": new_sha,
            "status": "active",
            "playbook_id": pb_rep.playbook_id,
            "playbook_sha256": pb_rep.sha256,
            "step_actions_sha256": steps_sha256,
            "contexts": list(pb_rep.contexts),
            "backing_atoms": sorted(verified_atom_hashes.keys()),
        }


def verify_executable_skill_integrity(
    skill_path_or_id: str,
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str | None = None,
    playbooks_dir: str | None = None,
    skills_dir: str | None = None,
    telemetry_db_path: str | None = None,
    security_dir: str | Path | None = None,
) -> ExecutableSkillIntegrityReport:
    """Fail-closed verification of Executable Skill file, decision log, kill-switch, Playbook & Atom anchors, and AST."""
    dirs = _resolve_vault_dirs(
        brain_dir=brain_dir,
        atoms_dir=atoms_dir,
        playbooks_dir=playbooks_dir,
        skills_dir=skills_dir,
        telemetry_db_path=telemetry_db_path,
    )
    raw_name = os.path.basename(str(skill_path_or_id or "").strip())
    fname = raw_name if raw_name.lower().endswith(".md") else f"{raw_name}.md"
    sk_id_fallback = os.path.splitext(fname)[0]

    if _is_reparse_or_link(dirs["brain_dir"]) or _is_reparse_or_link(dirs["skills_dir"]):
        return ExecutableSkillIntegrityReport(
            sk_id_fallback, fname, False, "invalid", "symlinked_skills_directory"
        )
    raw_cand = os.path.join(dirs["skills_dir"], fname)
    if os.path.lexists(raw_cand) and _is_reparse_or_link(raw_cand):
        return ExecutableSkillIntegrityReport(
            sk_id_fallback, fname, False, "invalid", "symlinked_skill_file"
        )
    resolved = safe_file_in_dir(dirs["skills_dir"], fname, allowed_exts={".md"}, must_exist=True)
    if not resolved or not os.path.isfile(resolved) or _is_reparse_or_link(resolved):
        return ExecutableSkillIntegrityReport(
            sk_id_fallback, fname, False, "invalid", "missing_or_unsafe_skill_file"
        )

    try:
        raw_bytes = Path(resolved).read_bytes()
        raw_text = raw_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return ExecutableSkillIntegrityReport(
            sk_id_fallback, fname, False, "invalid", "unreadable_skill_file"
        )
    sha = hashlib.sha256(raw_bytes).hexdigest()

    parsed = split_frontmatter(raw_text)
    if parsed is None:
        return ExecutableSkillIntegrityReport(
            sk_id_fallback, fname, False, "invalid", "missing_frontmatter", sha256=sha
        )

    fm_map: dict[str, Any] = dict(parse_frontmatter_values(parsed[0]))
    if str(fm_map.get("type") or "").strip() != "executable_skill":
        return ExecutableSkillIntegrityReport(
            sk_id_fallback, fname, False, "invalid", "invalid_document_type", sha256=sha
        )

    sk_id = str(fm_map.get("skill_id") or sk_id_fallback).strip()
    status = str(fm_map.get("skill_status") or "invalid").strip()
    title = str(fm_map.get("title") or sk_id).strip()
    pb_id = str(fm_map.get("playbook_id") or "").strip()
    pb_sha = str(fm_map.get("playbook_sha256") or "").strip().lower()
    promoted_at = str(fm_map.get("promoted_at") or "").strip()
    promoted_by = str(fm_map.get("promoted_by") or "").strip()
    early_override = str(fm_map.get("early_override") or "").strip().lower() == "true"
    steps_sha_expected = str(fm_map.get("step_actions_sha256") or "").strip().lower()

    contexts_raw = fm_map.get("contexts")
    if isinstance(contexts_raw, str):
        contexts_raw = parse_scalar_list(contexts_raw)
    contexts = tuple(str(x).strip() for x in contexts_raw if str(x).strip()) if isinstance(contexts_raw, list) else ()

    backing_list = fm_map.get("backing_atoms")
    if isinstance(backing_list, str):
        backing_list = parse_scalar_list(backing_list)
    backing_atoms_tuple = tuple(str(x).strip() for x in backing_list if str(x).strip()) if isinstance(backing_list, list) else ()

    raw_atom_hashes = fm_map.get("backing_atom_sha256s")
    atom_hashes_map: dict[str, str] | None = None
    if isinstance(raw_atom_hashes, str) and raw_atom_hashes.strip().startswith("{"):
        try:
            loaded_ah = json.loads(raw_atom_hashes)
            if isinstance(loaded_ah, dict):
                atom_hashes_map = {str(k): str(v) for k, v in loaded_ah.items()}
        except json.JSONDecodeError:
            atom_hashes_map = None

    try:
        timeout_sec = _clamp_timeout_seconds(fm_map.get("timeout_seconds", 15.0))
        max_out_bytes = _clamp_max_output_bytes(fm_map.get("max_output_bytes", 65536))
    except ExecutableSkillValidationError:
        return ExecutableSkillIntegrityReport(
            sk_id, fname, False, "invalid", "invalid_resource_bounds", title=title, sha256=sha
        )

    body = "\n".join(parsed[1])
    for sec in REQUIRED_SKILL_SECTIONS:
        if not _raw_section_block(body, sec).strip():
            return ExecutableSkillIntegrityReport(
                sk_id, fname, False, status, f"missing_required_section:{sec}", title=title, sha256=sha
            )

    expected_outputs = _parse_bullet_or_numbered_lines(_raw_section_block(body, "Expected Outputs & Verification"))
    failure_recovery = _parse_bullet_or_numbered_lines(_raw_section_block(body, "Failure Modes & Rollback"))

    if sk_id != sk_id_fallback or not SKILL_ID_RE.fullmatch(sk_id):
        return ExecutableSkillIntegrityReport(
            sk_id_fallback, fname, False, "invalid", "skill_id_filename_mismatch", title=title, sha256=sha
        )

    # Check kill-switch BEFORE active/playbook checks so an engaged kill-switch always surfaces status="killed".
    ks_status = get_skill_kill_switch_status(brain_dir=dirs["brain_dir"], security_dir=security_dir)
    killed, ks_reason = _is_skill_killed(ks_status, sk_id)
    if killed:
        return ExecutableSkillIntegrityReport(
            skill_id=sk_id,
            filename=fname,
            valid=False,
            status="killed",
            reason=f"kill_switch_engaged:{ks_reason}",
            title=title,
            sha256=sha,
            playbook_id=pb_id,
            playbook_sha256=pb_sha,
            contexts=contexts,
            backing_atoms=backing_atoms_tuple,
            timeout_seconds=timeout_sec,
            max_output_bytes=max_out_bytes,
            early_override=early_override,
            expected_outputs=expected_outputs,
            failure_recovery=failure_recovery,
            promoted_at=promoted_at,
            promoted_by=promoted_by,
        )

    log_ok, latest_decision = _latest_skill_decision(dirs["brain_dir"], fname)
    if not log_ok:
        return ExecutableSkillIntegrityReport(
            sk_id, fname, False, "invalid", "skill_decision_log_unreadable", title=title, sha256=sha
        )

    if status != "active":
        return ExecutableSkillIntegrityReport(
            skill_id=sk_id,
            filename=fname,
            valid=False,
            status=status,
            reason=f"skill_not_active:{status}",
            title=title,
            sha256=sha,
            playbook_id=pb_id,
            playbook_sha256=pb_sha,
            contexts=contexts,
            backing_atoms=backing_atoms_tuple,
            timeout_seconds=timeout_sec,
            max_output_bytes=max_out_bytes,
            early_override=early_override,
            expected_outputs=expected_outputs,
            failure_recovery=failure_recovery,
            promoted_at=promoted_at,
            promoted_by=promoted_by,
        )

    if (
        latest_decision is None
        or latest_decision.get("action") != "promote_executable_skill"
        or str(latest_decision.get("new_sha256") or "") != sha
    ):
        if latest_decision is not None and latest_decision.get("action") == "revoke_executable_skill":
            return ExecutableSkillIntegrityReport(
                sk_id,
                fname,
                False,
                "revoked",
                "skill_revoked_in_decision_log",
                title=title,
                sha256=sha,
                playbook_id=pb_id,
                playbook_sha256=pb_sha,
            )
        return ExecutableSkillIntegrityReport(
            sk_id,
            fname,
            False,
            "invalid",
            "skill_revision_not_owner_promoted",
            title=title,
            sha256=sha,
            playbook_id=pb_id,
            playbook_sha256=pb_sha,
        )

    if not pb_id or not HEX64_RE.fullmatch(pb_sha) or not backing_atoms_tuple or atom_hashes_map is None:
        return ExecutableSkillIntegrityReport(
            sk_id, fname, False, "invalid", "missing_playbook_or_atom_anchors", title=title, sha256=sha
        )

    # Re-verify backing Playbook integrity (automatically cascades any backing atom edit, demotion,
    # supersession, source anchor drift, or unacknowledged correction!).
    pb_rep = verify_playbook_integrity(
        os.path.join(dirs["playbooks_dir"], f"{pb_id}.md"),
        brain_dir=dirs["brain_dir"],
        atoms_dir=dirs["atoms_dir"],
        playbooks_dir=dirs["playbooks_dir"],
        telemetry_db_path=dirs["telemetry_db_path"],
    )
    if not pb_rep.valid or pb_rep.status != "active":
        return ExecutableSkillIntegrityReport(
            sk_id,
            fname,
            False,
            "stale",
            f"backing_playbook_invalid:{pb_rep.reason}",
            title=title,
            sha256=sha,
            playbook_id=pb_id,
            playbook_sha256=pb_sha,
            contexts=contexts,
            backing_atoms=backing_atoms_tuple,
        )
    if pb_rep.sha256 != pb_sha:
        return ExecutableSkillIntegrityReport(
            sk_id,
            fname,
            False,
            "stale",
            "stale_backing_playbook_sha256",
            title=title,
            sha256=sha,
            playbook_id=pb_id,
            playbook_sha256=pb_sha,
            contexts=contexts,
            backing_atoms=backing_atoms_tuple,
        )

    for atom_name in backing_atoms_tuple:
        apath = safe_file_in_dir(dirs["atoms_dir"], atom_name, allowed_exts={".md"}, must_exist=True)
        actual_asha = file_sha256(apath) if (apath and os.path.isfile(apath) and not _is_reparse_or_link(apath)) else None
        if not actual_asha or actual_asha != atom_hashes_map.get(atom_name):
            return ExecutableSkillIntegrityReport(
                sk_id,
                fname,
                False,
                "stale",
                f"stale_backing_atom_sha256:{atom_name}",
                title=title,
                sha256=sha,
                playbook_id=pb_id,
                playbook_sha256=pb_sha,
            )

    # Re-validate input_schema & step_actions + cryptographic step_actions_sha256 + AST + 1:1 Playbook steps
    raw_schema_str = str(fm_map.get("input_schema_json") or "{}")
    try:
        loaded_schema = json.loads(raw_schema_str)
        validated_schema = _validate_input_schema(loaded_schema)
    except (json.JSONDecodeError, ExecutableSkillValidationError) as exc:
        return ExecutableSkillIntegrityReport(
            sk_id, fname, False, "invalid", f"invalid_input_schema:{exc}", title=title, sha256=sha
        )

    steps_sec = _raw_section_block(body, "Ordered Sandboxed Steps")
    json_match = FENCED_JSON_RE.search(steps_sec)
    if json_match is None:
        return ExecutableSkillIntegrityReport(
            sk_id, fname, False, "invalid", "missing_step_actions_json_block", title=title, sha256=sha
        )
    try:
        parsed_steps_raw = json.loads(json_match.group(1))
        validated_steps = _validate_and_normalize_step_actions(parsed_steps_raw, pb_rep.steps, validated_schema)
    except (json.JSONDecodeError, ExecutableSkillValidationError) as exc:
        return ExecutableSkillIntegrityReport(
            sk_id, fname, False, "invalid", f"invalid_step_actions:{exc}", title=title, sha256=sha
        )

    computed_steps_sha = _compute_step_actions_sha256(validated_steps)
    if not HEX64_RE.fullmatch(steps_sha_expected) or computed_steps_sha != steps_sha_expected:
        return ExecutableSkillIntegrityReport(
            sk_id, fname, False, "invalid", "step_actions_sha256_mismatch", title=title, sha256=sha
        )

    return ExecutableSkillIntegrityReport(
        skill_id=sk_id,
        filename=fname,
        valid=True,
        status="active",
        reason="verified_active",
        title=title,
        sha256=sha,
        playbook_id=pb_rep.playbook_id,
        playbook_sha256=pb_rep.sha256,
        contexts=pb_rep.contexts,
        backing_atoms=pb_rep.backing_atoms,
        backing_atom_contexts=pb_rep.backing_atom_contexts,
        timeout_seconds=timeout_sec,
        max_output_bytes=max_out_bytes,
        early_override=early_override,
        input_schema=validated_schema,
        step_actions=tuple(validated_steps),
        step_actions_sha256=computed_steps_sha,
        expected_outputs=expected_outputs,
        failure_recovery=failure_recovery,
        promoted_at=promoted_at,
        promoted_by=promoted_by,
    )


def list_executable_skills(
    *,
    skills_dir: str | None = None,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str | None = None,
    playbooks_dir: str | None = None,
    telemetry_db_path: str | None = None,
    security_dir: str | Path | None = None,
    active_only: bool = True,
) -> list[ExecutableSkillIntegrityReport]:
    """List Executable Skills in `brain/6-skills/` with real-time cryptographic & kill-switch verification."""
    dirs = _resolve_vault_dirs(
        brain_dir=brain_dir,
        atoms_dir=atoms_dir,
        playbooks_dir=playbooks_dir,
        skills_dir=skills_dir,
        telemetry_db_path=telemetry_db_path,
    )
    if not os.path.isdir(dirs["skills_dir"]) or _is_reparse_or_link(dirs["skills_dir"]):
        return []

    reports: list[ExecutableSkillIntegrityReport] = []
    for fname in sorted(os.listdir(dirs["skills_dir"])):
        if not is_content_markdown(fname):
            continue
        rep = verify_executable_skill_integrity(
            os.path.join(dirs["skills_dir"], fname),
            brain_dir=dirs["brain_dir"],
            atoms_dir=dirs["atoms_dir"],
            playbooks_dir=dirs["playbooks_dir"],
            skills_dir=dirs["skills_dir"],
            telemetry_db_path=dirs["telemetry_db_path"],
            security_dir=security_dir,
        )
        if active_only and not rep.valid:
            continue
        reports.append(rep)
    return reports


def revoke_executable_skill(
    skill_id: str,
    *,
    expected_skill_sha256: str,
    actor: str,
    reason: str,
    brain_dir: str = BRAIN_DIR,
    skills_dir: str | None = None,
) -> dict[str, object]:
    """CAS-locked human owner revocation of an Executable Skill in `6-skills/`."""
    norm_id = _normalize_skill_id(skill_id)
    expected_sha = _validate_hex64(expected_skill_sha256, "expected_skill_sha256")
    clean_actor = _validate_human_actor(actor)
    clean_reason = _sanitize_line(reason, max_len=500)
    if not clean_reason:
        raise ExecutableSkillValidationError("Executable Skill revocation requires a non-empty reason.")

    dirs = _resolve_vault_dirs(brain_dir=brain_dir, skills_dir=skills_dir)
    _assert_not_reparse(dirs["brain_dir"], "brain_dir")
    _assert_not_reparse(dirs["skills_dir"], "skills_dir")

    sidecar_dir = os.path.join(dirs["brain_dir"], ".sidecar")
    _assert_not_reparse(sidecar_dir, ".sidecar")
    os.makedirs(sidecar_dir, exist_ok=True)
    lock_path = os.path.join(sidecar_dir, "writer.lock")

    fname = f"{norm_id}.md"
    with review_lock(lock_path):
        _assert_not_reparse(dirs["skills_dir"], "skills_dir")
        raw_cand = os.path.join(dirs["skills_dir"], fname)
        _assert_not_reparse(raw_cand, fname)
        target = safe_file_in_dir(dirs["skills_dir"], fname, allowed_exts={".md"}, must_exist=True)
        if not target or not os.path.isfile(target) or _is_reparse_or_link(target):
            raise ExecutableSkillValidationError(f"Executable Skill not found or unsafe: {skill_id!r}")

        current_sha = file_sha256(target) or ""
        if current_sha != expected_sha:
            raise ExecutableSkillValidationError(
                f"CAS hash mismatch on Executable Skill '{fname}': expected {expected_sha}, found {current_sha}."
            )

        raw_text = Path(target).read_text(encoding="utf-8")
        parsed_sk = split_frontmatter(raw_text)
        if parsed_sk is None:
            raise ExecutableSkillValidationError(f"Executable Skill '{fname}' is missing valid frontmatter.")
        existing_fm = parse_frontmatter_values(parsed_sk[0])
        if str(existing_fm.get("type") or "").strip() != "executable_skill":
            raise ExecutableSkillValidationError(f"File '{fname}' is not an Executable Skill.")
        if not frontmatter_has_key(parsed_sk[0], "skill_status"):
            raise ExecutableSkillValidationError(f"Executable Skill '{fname}' is missing skill_status field.")
        cur_status = str(existing_fm.get("skill_status") or "").strip()
        if cur_status != "active":
            raise ExecutableSkillValidationError(
                f"Executable Skill '{fname}' is not active (status={cur_status!r})."
            )

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        updated_fm = remove_frontmatter_fields(parsed_sk[0], {"revoked_at", "revoked_by", "revoked_reason"})
        updated_fm = set_scalar_field(updated_fm, "skill_status", "revoked", "type")
        updated_fm = set_scalar_field(updated_fm, "revoked_at", json.dumps(now_iso), "step_actions_sha256")
        updated_fm = set_scalar_field(
            updated_fm, "revoked_by", json.dumps(clean_actor, ensure_ascii=False), "revoked_at"
        )
        updated_fm = set_scalar_field(
            updated_fm, "revoked_reason", json.dumps(clean_reason, ensure_ascii=False), "revoked_by"
        )
        updated_text = render_memory_document(updated_fm, parsed_sk[1])

        _backup_playbook_revision(dirs["skills_dir"], target, f"{norm_id}.{current_sha[:12]}.md")

        encoded = updated_text.encode("utf-8")
        fd, tmp_path = tempfile.mkstemp(dir=dirs["skills_dir"], prefix=".rev-sk-", suffix=".tmp")
        closed = False
        try:
            if os.name != "nt":
                os.chmod(tmp_path, 0o600)
            if os.write(fd, encoded) != len(encoded):
                raise OSError("Incomplete Executable Skill revocation write.")
            os.fsync(fd)
            os.close(fd)
            closed = True
            os.replace(tmp_path, target)
        finally:
            if not closed:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        new_sha = file_sha256(target) or ""
        _append_skill_decision(
            dirs["brain_dir"],
            {
                "schema_version": 1,
                "timestamp": now_iso,
                "action": "revoke_executable_skill",
                "skill_id": norm_id,
                "filename": fname,
                "previous_sha256": current_sha,
                "new_sha256": new_sha,
                "actor": clean_actor,
                "reason": clean_reason,
            },
        )
        return {
            "skill_id": norm_id,
            "filename": fname,
            "status": "revoked",
            "previous_sha256": current_sha,
            "sha256": new_sha,
            "actor": clean_actor,
            "reason": clean_reason,
        }


def _read_execution_chain_entries(log_file: Path) -> tuple[list[dict[str, Any]], list[str]]:
    raw_root = log_file.parent.parent
    for check_p, label in (
        (log_file.parent.parent.parent, "brain_dir"),
        (raw_root, "0-raw"),
        (log_file.parent, "0-raw/archive"),
        (log_file, "skill-executions.jsonl"),
    ):
        if os.path.lexists(str(check_p)) and _is_reparse_or_link(str(check_p)):
            return [], [f"Execution audit {label} is a symlink or reparse point (rejected fail-closed)."]

    if not log_file.exists():
        return [], []
    if not log_file.is_file():
        return [], ["skill-executions.jsonl is not a regular file."]

    try:
        raw_text = log_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [], [f"skill-executions.jsonl unreadable or corrupted: {exc}"]

    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    expected_prev = GENESIS_EXECUTION_HASH

    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            errors.append(f"Line {line_no}: invalid JSON ({exc})")
            break
        if not isinstance(obj, dict):
            errors.append(f"Line {line_no}: record must be a JSON object")
            break

        idx_val = obj.get("execution_index")
        if isinstance(idx_val, bool) or not isinstance(idx_val, int) or idx_val != len(entries) + 1:
            errors.append(f"Line {line_no}: non-monotonic execution_index={idx_val!r} (expected {len(entries) + 1})")
            break

        prev_val = str(obj.get("prev_execution_sha256") or "")
        if prev_val != expected_prev:
            errors.append(
                f"Line {line_no}: broken prev_execution_sha256 chain "
                f"(recorded={prev_val[:12]}, expected={expected_prev[:12]})"
            )
            break

        recorded_sha = str(obj.get("execution_sha256") or "")
        body = {k: v for k, v in obj.items() if k != "execution_sha256"}
        computed_sha = _sha256_text(_canonical_json(body))
        if not HEX64_RE.fullmatch(recorded_sha) or recorded_sha != computed_sha:
            errors.append(
                f"Line {line_no}: execution_sha256 digest mismatch "
                f"(recorded={recorded_sha[:12]}, computed={computed_sha[:12]})"
            )
            break

        expected_prev = recorded_sha
        entries.append(obj)

    return entries, errors


def verify_skill_execution_audit_chain(brain_dir: str = BRAIN_DIR) -> dict[str, object]:
    """Verify genesis `0*64`, monotonic `execution_index`, `prev_execution_sha256`, and `execution_sha256` digests."""
    eff_brain = os.path.abspath(brain_dir)
    log_file = Path(skill_execution_log_path(eff_brain))
    entries, errors = _read_execution_chain_entries(log_file)
    valid = len(errors) == 0
    head_sha = entries[-1]["execution_sha256"] if entries else GENESIS_EXECUTION_HASH
    return {
        "valid": valid,
        "chain_valid": valid,
        "entry_count": len(entries),
        "head_index": entries[-1]["execution_index"] if entries else 0,
        "head_execution_sha256": head_sha,
        "errors": errors,
        "entries": entries,
    }


def list_skill_executions(
    brain_dir: str = BRAIN_DIR,
    skill_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return verified skill execution audit entries (fails closed if audit chain is tampered or symlinked)."""
    chain = verify_skill_execution_audit_chain(brain_dir)
    if not chain["valid"]:
        errors = "; ".join(str(e) for e in chain.get("errors", []))  # type: ignore[union-attr]
        if "symlink or reparse point" in errors:
            raise PermissionError(f"Refusing to read skill execution audit log: {errors}")
        raise ExecutableSkillValidationError(
            f"Tampered or corrupted skill-executions.jsonl audit chain: {errors}"
        )
    entries = list(chain["entries"])  # type: ignore[arg-type]
    if skill_id is not None and str(skill_id).strip():
        norm_sid = _normalize_skill_id(skill_id)
        entries = [e for e in entries if e.get("skill_id") == norm_sid]
    clamped_limit = max(1, min(1000, int(limit)))
    return entries[-clamped_limit:]


def _append_skill_execution_record(brain_dir: str, payload_without_chain: dict[str, object]) -> dict[str, object]:
    """Serialize and append one hash-chained execution audit entry under `writer.lock`."""
    eff_brain = os.path.abspath(brain_dir)
    log_file = Path(skill_execution_log_path(eff_brain))
    raw_root = log_file.parent.parent
    sidecar_dir = Path(eff_brain) / ".sidecar"

    for check_dir, label in (
        (Path(eff_brain), "brain_dir"),
        (raw_root, "0-raw"),
        (log_file.parent, "0-raw/archive"),
        (sidecar_dir, ".sidecar"),
    ):
        _assert_not_reparse(check_dir, label)

    log_file.parent.mkdir(parents=True, exist_ok=True)
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    _assert_not_reparse(log_file.parent, "0-raw/archive")
    _assert_not_reparse(log_file, "skill-executions.jsonl")

    with review_lock(str(sidecar_dir / "writer.lock")):
        entries, errors = _read_execution_chain_entries(log_file)
        if errors:
            joined = "; ".join(errors)
            if "symlink or reparse point" in joined:
                raise PermissionError(joined)
            raise ExecutableSkillValidationError(
                f"Cannot append to tampered/corrupted skill-executions.jsonl: {joined}"
            )
        prev_sha = str(entries[-1]["execution_sha256"]) if entries else GENESIS_EXECUTION_HASH
        next_idx = len(entries) + 1
        record: dict[str, object] = {
            "schema_version": 1,
            "execution_index": next_idx,
            "prev_execution_sha256": prev_sha,
            **payload_without_chain,
        }
        record["execution_sha256"] = _sha256_text(_canonical_json(record))
        encoded_line = (_canonical_json(record) + "\n").encode("utf-8")
        fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND | _O_BINARY, 0o600)
        try:
            if os.write(fd, encoded_line) != len(encoded_line):
                raise OSError("Incomplete write to skill-executions.jsonl.")
            os.fsync(fd)
        finally:
            os.close(fd)
        restrict_security_file_permissions(log_file)
        return record


_SANDBOX_SITECUSTOMIZE_SOURCE = '''"""Second Brain Stage 4 Executable Skill runtime sandbox guard (`_guard/sitecustomize.py`)."""


def _install_sandbox_guard() -> None:
    import builtins as _builtins
    import io as _io
    import os as _os
    import socket as _socket
    import sys as _sys

    _NET_ERR = "Network access is disabled inside Second Brain Executable Skill sandbox"
    _FS_ERR = "Filesystem access outside SB_SANDBOX_WORKSPACE is disabled inside Second Brain sandbox"
    _CHDIR_ERR = "os.chdir outside SB_SANDBOX_WORKSPACE is disabled inside Second Brain sandbox"

    _WORKSPACE_RAW = _os.environ.get("SB_SANDBOX_WORKSPACE") or ""
    _WORKSPACE_REAL = _os.path.realpath(_WORKSPACE_RAW) if _WORKSPACE_RAW else ""
    _NORM_WS = _os.path.normcase(_WORKSPACE_REAL) if _WORKSPACE_REAL else ""

    _INPUTS_RAW = _os.environ.get("SB_SANDBOX_INPUTS") or ""
    _INPUTS_REAL = _os.path.realpath(_INPUTS_RAW) if _INPUTS_RAW else ""
    _NORM_INPUTS = _os.path.normcase(_INPUTS_REAL) if _INPUTS_REAL else ""

    _GUARD_DIR_REAL = _os.path.dirname(_INPUTS_REAL) if _INPUTS_REAL else ""
    _NORM_GUARD_DIR = _os.path.normcase(_GUARD_DIR_REAL) if _GUARD_DIR_REAL else ""

    _STDLIB_NORM_ROOTS = []
    for _cand in (
        getattr(_sys, "prefix", ""),
        getattr(_sys, "base_prefix", ""),
        getattr(_sys, "exec_prefix", ""),
        getattr(_sys, "base_exec_prefix", ""),
    ):
        if not _cand or not isinstance(_cand, str):
            continue
        try:
            _rp = _os.path.realpath(_cand)
            _np = _os.path.normcase(_rp)
        except Exception:
            continue
        if not _np or _np == _NORM_GUARD_DIR:
            continue
        if _NORM_WS:
            try:
                if _os.path.normcase(_os.path.commonpath([_rp, _WORKSPACE_REAL])) == _np:
                    continue
            except Exception:
                pass
        if _np not in _STDLIB_NORM_ROOTS:
            _STDLIB_NORM_ROOTS.append(_np)

    _FORBIDDEN_MODULES = frozenset({
        "ctypes", "_ctypes", "winreg", "pty", "webbrowser", "httpx", "requests",
        "http", "ftplib", "smtplib", "telnetlib", "xmlrpc", "multiprocessing",
        "subprocess", "pickle", "shelve", "asyncio", "gc", "inspect",
    })
    _FORBIDDEN_EXACT_OR_PREFIX = (
        "urllib.request",
        "urllib.response",
        "urllib.robotparser",
    )

    class _BlockedModuleFinder:
        def find_spec(self, fullname, path=None, target=None):
            root = fullname.split(".", 1)[0]
            if root in _FORBIDDEN_MODULES or any(
                fullname == p or fullname.startswith(p + ".") for p in _FORBIDDEN_EXACT_OR_PREFIX
            ):
                raise ImportError(f"Module {fullname!r} is blocked inside Second Brain Executable Skill sandbox")
            return None

    def _deny_network(*_args, **_kwargs):
        raise PermissionError(_NET_ERR)

    _socket.socket.connect = _deny_network
    _socket.socket.connect_ex = _deny_network
    _socket.socket.bind = _deny_network
    _socket.socket.sendto = _deny_network
    _socket.create_connection = _deny_network
    if hasattr(_socket, "create_server"):
        _socket.create_server = _deny_network
    _socket.getaddrinfo = _deny_network

    _orig_open = _builtins.open
    _orig_io_open = _io.open
    _orig_fileio = _io.FileIO
    _orig_os_open = _os.open
    _orig_chdir = _os.chdir
    _orig_remove = _os.remove
    _orig_unlink = _os.unlink
    _orig_mkdir = _os.mkdir
    _orig_makedirs = _os.makedirs
    _orig_rmdir = _os.rmdir
    _orig_rename = _os.rename
    _orig_replace = _os.replace
    _orig_truncate = getattr(_os, "truncate", None)
    _orig_chmod = getattr(_os, "chmod", None)
    _orig_utime = getattr(_os, "utime", None)

    def _is_inside_workspace(target) -> bool:
        if not _NORM_WS or isinstance(target, int):
            return False
        try:
            p = _os.fsdecode(target)
        except Exception:
            return False
        if not p or "\\x00" in p:
            return False
        try:
            abs_p = _os.path.abspath(p)
            real_p = _os.path.realpath(abs_p)
            return (
                _os.path.normcase(_os.path.commonpath([_WORKSPACE_REAL, abs_p])) == _NORM_WS
                and _os.path.normcase(_os.path.commonpath([_WORKSPACE_REAL, real_p])) == _NORM_WS
            )
        except Exception:
            return False

    def _is_allowed_read(target) -> bool:
        if isinstance(target, int):
            return False
        if _is_inside_workspace(target):
            return True
        try:
            p = _os.fsdecode(target)
        except Exception:
            return False
        if not p or "\\x00" in p:
            return False
        if p in ("/dev/null", "/dev/urandom", "/dev/random", "NUL", "nul"):
            return True
        try:
            abs_p = _os.path.abspath(p)
            real_p = _os.path.realpath(abs_p)
            norm_abs = _os.path.normcase(abs_p)
            norm_real = _os.path.normcase(real_p)
        except Exception:
            return False
        if _NORM_INPUTS and norm_abs == _NORM_INPUTS and norm_real == _NORM_INPUTS:
            return True
        for _root in _STDLIB_NORM_ROOTS:
            try:
                if _os.path.normcase(_os.path.commonpath([_root, norm_real])) == _root:
                    return True
            except Exception:
                continue
        return False

    def _guarded_open(file, mode="r", *args, **kwargs):
        mode_str = str(mode or "r")
        if any(flag in mode_str for flag in ("w", "a", "x", "+")):
            if not _is_inside_workspace(file):
                raise PermissionError(f"{_FS_ERR}: {file!r}")
        else:
            if not _is_allowed_read(file):
                raise PermissionError(f"{_FS_ERR}: {file!r}")
        return _orig_open(file, mode, *args, **kwargs)

    def _guarded_io_open(file, mode="r", *args, **kwargs):
        mode_str = str(mode or "r")
        if any(flag in mode_str for flag in ("w", "a", "x", "+")):
            if not _is_inside_workspace(file):
                raise PermissionError(f"{_FS_ERR}: {file!r}")
        else:
            if not _is_allowed_read(file):
                raise PermissionError(f"{_FS_ERR}: {file!r}")
        return _orig_io_open(file, mode, *args, **kwargs)

    class _GuardedFileIO(_orig_fileio):
        def __init__(self, file, mode="r", closefd=True, opener=None):
            mode_str = str(mode or "r")
            if opener is not None:
                raise PermissionError(f"{_FS_ERR}: custom FileIO opener forbidden")
            if any(flag in mode_str for flag in ("w", "a", "x", "+")):
                if not _is_inside_workspace(file):
                    raise PermissionError(f"{_FS_ERR}: {file!r}")
            else:
                if not _is_allowed_read(file):
                    raise PermissionError(f"{_FS_ERR}: {file!r}")
            super().__init__(file, mode, closefd=closefd, opener=opener)

    _WRITE_FLAGS = (
        getattr(_os, "O_WRONLY", 1)
        | getattr(_os, "O_RDWR", 2)
        | getattr(_os, "O_CREAT", 0)
        | getattr(_os, "O_TRUNC", 0)
        | getattr(_os, "O_APPEND", 0)
    )

    def _guarded_os_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            raise PermissionError(f"{_FS_ERR}: dir_fd forbidden")
        if flags & _WRITE_FLAGS:
            if not _is_inside_workspace(path):
                raise PermissionError(f"{_FS_ERR}: {path!r}")
        else:
            if not _is_allowed_read(path):
                raise PermissionError(f"{_FS_ERR}: {path!r}")
        return _orig_os_open(path, flags, mode)

    def _guarded_chdir(path):
        if not _is_inside_workspace(path):
            raise PermissionError(f"{_CHDIR_ERR}: {path!r}")
        return _orig_chdir(path)

    def _wrap_single_path(fn):
        def _wrapped(path, *args, **kwargs):
            if "dir_fd" in kwargs and kwargs["dir_fd"] is not None:
                raise PermissionError(f"{_FS_ERR}: dir_fd forbidden")
            if not _is_inside_workspace(path):
                raise PermissionError(f"{_FS_ERR}: {path!r}")
            return fn(path, *args, **kwargs)
        return _wrapped

    def _wrap_two_paths(fn):
        def _wrapped(src, dst, *args, **kwargs):
            if "src_dir_fd" in kwargs and kwargs["src_dir_fd"] is not None:
                raise PermissionError(f"{_FS_ERR}: dir_fd forbidden")
            if "dst_dir_fd" in kwargs and kwargs["dst_dir_fd"] is not None:
                raise PermissionError(f"{_FS_ERR}: dir_fd forbidden")
            if not _is_inside_workspace(src) or not _is_inside_workspace(dst):
                raise PermissionError(f"{_FS_ERR}: {src!r} -> {dst!r}")
            return fn(src, dst, *args, **kwargs)
        return _wrapped

    def _deny_link(*_args, **_kwargs):
        raise PermissionError("Creating symlinks or hardlinks is disabled inside Second Brain sandbox")

    def _audit_hook(event, args):
        if event.startswith("socket.") or event == "urllib.Request":
            raise PermissionError(_NET_ERR)
        if event in (
            "subprocess.Popen", "os.system", "os.exec", "os.spawn",
            "os.fork", "os.posix_spawn", "ctypes.dlopen", "ctypes.dlsym",
        ):
            raise PermissionError(f"Blocked runtime event {event!r} inside Second Brain sandbox")
        if event in ("os.symlink", "os.link"):
            raise PermissionError("Creating symlinks or hardlinks is disabled inside Second Brain sandbox")
        if event == "os.chdir":
            p = args[0] if args else None
            if not _is_inside_workspace(p):
                raise PermissionError(f"{_CHDIR_ERR}: {p!r}")
        elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.truncate", "os.chmod", "os.utime"):
            p = args[0] if args else None
            if not _is_inside_workspace(p):
                raise PermissionError(f"{_FS_ERR}: {p!r}")
        elif event == "os.rename":
            src = args[0] if len(args) > 0 else None
            dst = args[1] if len(args) > 1 else None
            if not _is_inside_workspace(src) or not _is_inside_workspace(dst):
                raise PermissionError(f"{_FS_ERR}: {src!r} -> {dst!r}")
        elif event in ("os.listdir", "os.scandir"):
            p = args[0] if args and args[0] is not None else "."
            if not _is_allowed_read(p):
                raise PermissionError(f"{_FS_ERR}: {p!r}")
        elif event == "open":
            p = args[0] if len(args) > 0 else None
            m = str(args[1] or "") if len(args) > 1 else ""
            fl = int(args[2] or 0) if len(args) > 2 and isinstance(args[2], int) else 0
            is_write = any(c in m for c in ("w", "a", "x", "+")) or bool(fl & _WRITE_FLAGS)
            if is_write:
                if not _is_inside_workspace(p):
                    raise PermissionError(f"{_FS_ERR}: {p!r}")
            else:
                if not _is_allowed_read(p):
                    raise PermissionError(f"{_FS_ERR}: {p!r}")

    _builtins.open = _guarded_open
    _io.open = _guarded_io_open
    _io.FileIO = _GuardedFileIO
    if "_io" in _sys.modules:
        setattr(_sys.modules["_io"], "open", _guarded_io_open)
        setattr(_sys.modules["_io"], "FileIO", _GuardedFileIO)
    _os.open = _guarded_os_open
    _os.chdir = _guarded_chdir
    _os.remove = _wrap_single_path(_orig_remove)
    _os.unlink = _wrap_single_path(_orig_unlink)
    _os.mkdir = _wrap_single_path(_orig_mkdir)
    _os.makedirs = _wrap_single_path(_orig_makedirs)
    _os.rmdir = _wrap_single_path(_orig_rmdir)
    _os.rename = _wrap_two_paths(_orig_rename)
    _os.replace = _wrap_two_paths(_orig_replace)
    if _orig_truncate is not None:
        _os.truncate = _wrap_single_path(_orig_truncate)
    if _orig_chmod is not None:
        _os.chmod = _wrap_single_path(_orig_chmod)
    if _orig_utime is not None:
        _os.utime = _wrap_single_path(_orig_utime)
    _os.symlink = _deny_link
    _os.link = _deny_link
    if "pathlib" in _sys.modules:
        _pl = _sys.modules["pathlib"]
        _acc = getattr(_pl, "_normal_accessor", None)
        if _acc is not None:
            for _attr in ("open", "mkdir", "unlink", "rmdir", "rename", "replace", "symlink", "chmod", "utime"):
                if hasattr(_os, _attr):
                    setattr(_acc, _attr, getattr(_os, _attr))
    _sys.meta_path.insert(0, _BlockedModuleFinder())
    if hasattr(_sys, "addaudithook"):
        _sys.addaudithook(_audit_hook)


_install_sandbox_guard()
del _install_sandbox_guard
'''

_SANDBOX_RUNNER_SOURCE = '''"""Bootstrap loader for `python -I -B`: activates `_guard/sitecustomize.py` before user script."""
import os as _os
import sys as _sys

_guard_dir = _os.path.dirname(_os.path.abspath(__file__))
_sc_path = _os.path.join(_guard_dir, "sitecustomize.py")
with open(_sc_path, "rb") as _fh:
    _sc_bytes = _fh.read()

_target_script = _sys.argv[1]
with open(_target_script, "rb") as _sfh:
    _script_bytes = _sfh.read()

_sc_code = compile(_sc_bytes, _sc_path, "exec")
_script_code = compile(_script_bytes, _target_script, "exec")
del _sc_bytes, _script_bytes, _fh, _sfh, _sc_path, _guard_dir

_sc_ns = {"__name__": "sitecustomize", "__builtins__": __builtins__}
exec(_sc_code, _sc_ns)
_sc_ns.clear()
del _sc_ns, _sc_code
_sys.modules.pop("sitecustomize", None)

_sys.argv = [_target_script]
_main_globals = {
    "__name__": "__main__",
    "__file__": _target_script,
    "__doc__": None,
    "__package__": None,
    "__builtins__": __builtins__,
}
del _target_script, _os, _sys
exec(_script_code, _main_globals)
'''


def _build_sandbox_child_env(
    *,
    guard_dir: str,
    workspace_dir: str,
    inputs_json_path: str,
    sandbox_tmp_dir: str,
) -> dict[str, str]:
    """Construct a minimal, secret-free environment for `sandbox_python` subprocesses."""
    env: dict[str, str] = {}
    for os_key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"):
        val = os.environ.get(os_key)
        if val:
            env[os_key] = val
    env["TEMP"] = sandbox_tmp_dir
    env["TMP"] = sandbox_tmp_dir
    env["TMPDIR"] = sandbox_tmp_dir
    env["PYTHONPATH"] = guard_dir
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["SB_SANDBOX_WORKSPACE"] = workspace_dir
    env["SB_SANDBOX_INPUTS"] = inputs_json_path
    return env


def _terminate_process_tree(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        taskkill_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
        if os.path.isfile(taskkill_exe):
            try:
                subprocess.run(
                    [taskkill_exe, "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=5.0,
                    check=False,
                )
            except Exception:
                pass
        try:
            proc.kill()
        except OSError:
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        pass


def _snapshot_workspace(workspace_dir: str) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    if not os.path.isdir(workspace_dir):
        return snapshot
    for root, dirs, files in os.walk(workspace_dir, followlinks=False):
        for d in dirs:
            _assert_not_reparse(os.path.join(root, d), "workspace subdir")
        for f in files:
            fp = os.path.join(root, f)
            _assert_not_reparse(fp, "workspace file")
            rel = os.path.relpath(fp, workspace_dir).replace("\\", "/")
            snapshot[rel] = Path(fp).read_bytes()
    return snapshot


def _clear_readonly_tree(root_dir: str) -> None:
    if not os.path.lexists(root_dir) or _is_reparse_or_link(root_dir):
        return
    for cur_root, dirs, files in os.walk(root_dir, topdown=False, followlinks=False):
        for fname in files:
            fp = os.path.join(cur_root, fname)
            if not _is_reparse_or_link(fp):
                try:
                    os.chmod(fp, stat.S_IREAD | stat.S_IWRITE)
                except OSError:
                    pass
        for dname in dirs:
            dp = os.path.join(cur_root, dname)
            if not _is_reparse_or_link(dp):
                try:
                    os.chmod(dp, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
                except OSError:
                    pass


def _restore_workspace_snapshot(workspace_dir: str, snapshot: dict[str, bytes]) -> None:
    """Deterministically wipe partial/failed workspace mutations and restore pre-run snapshot."""
    _assert_not_reparse(workspace_dir, "workspace_dir")
    if os.path.isdir(workspace_dir):
        for entry in os.listdir(workspace_dir):
            full = os.path.join(workspace_dir, entry)
            if _is_reparse_or_link(full) or os.path.isfile(full):
                try:
                    if not _is_reparse_or_link(full):
                        os.chmod(full, stat.S_IREAD | stat.S_IWRITE)
                    os.unlink(full)
                except OSError:
                    pass
            elif os.path.isdir(full):
                _clear_readonly_tree(full)
                shutil.rmtree(full)

    for rel, data in sorted(snapshot.items()):
        safe_rel = _validate_sandbox_rel_path(rel, field_name="snapshot_rel_path")
        dest = resolve_under(workspace_dir, safe_rel, must_exist=False)
        if not dest:
            raise PermissionError(f"Unsafe workspace rollback path: {rel!r}")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        Path(dest).write_bytes(data)


def _hash_workspace_artifacts(workspace_dir: str, *, max_total_bytes: int) -> dict[str, str]:
    _assert_not_reparse(workspace_dir, "workspace_dir")
    hashes: dict[str, str] = {}
    total_bytes = 0
    for root, dirs, files in os.walk(workspace_dir, followlinks=False):
        for d in sorted(dirs):
            _assert_not_reparse(os.path.join(root, d), "workspace directory")
        for f in sorted(files):
            fp = os.path.join(root, f)
            _assert_not_reparse(fp, "workspace artifact")
            st = os.lstat(fp)
            if not stat.S_ISREG(st.st_mode):
                raise PermissionError(f"Non-regular file inside sandbox workspace: {f!r}")
            total_bytes += st.st_size
            if total_bytes > max_total_bytes:
                raise ExecutableSkillValidationError(
                    f"Workspace artifacts exceed total byte ceiling ({total_bytes} > {max_total_bytes})."
                )
            rel = os.path.relpath(fp, workspace_dir).replace("\\", "/")
            hashes[rel] = file_sha256(fp) or ""
    return dict(sorted(hashes.items()))


def _write_workspace_file_safe(
    workspace_dir: str,
    rel_path: str,
    data: bytes,
    *,
    max_output_bytes: int,
) -> str:
    if len(data) > max_output_bytes:
        raise ExecutableSkillValidationError(
            f"Workspace write to '{rel_path}' exceeds max_output_bytes ({len(data)} > {max_output_bytes})."
        )
    safe_rel = _validate_sandbox_rel_path(rel_path, field_name="workspace_rel_path")
    dest = resolve_under(workspace_dir, safe_rel, must_exist=False)
    if not dest:
        raise PermissionError(f"Refusing unsafe workspace write path: {rel_path!r}")
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    _assert_not_reparse(parent, "workspace parent")
    _assert_not_reparse(dest, "workspace file")
    Path(dest).write_bytes(data)
    return dest


def _read_bounded_redacted_stream(log_path: str, max_bytes: int) -> tuple[str, bool]:
    if not os.path.isfile(log_path) or _is_reparse_or_link(log_path):
        return "", False
    st = os.stat(log_path)
    truncated = st.st_size > max_bytes
    with open(log_path, "rb") as fh:
        raw = fh.read(max_bytes)
    decoded = raw.decode("utf-8", errors="replace")
    redacted = inspect_and_redact_outbound(
        decoded,
        redact_pii=True,
        fail_on_secret_detection=False,
    )
    return redacted.sanitized_text, truncated


def _evaluate_workspace_assert(
    workspace_dir: str,
    *,
    rel_path: str,
    op: str,
    expected: str,
    json_key: str,
) -> tuple[bool, str]:
    safe_rel = _validate_sandbox_rel_path(rel_path, field_name="assert_rel_path")
    target = resolve_under(workspace_dir, safe_rel, must_exist=False)
    if not target or (os.path.lexists(target) and _is_reparse_or_link(target)):
        return False, f"unsafe_or_symlinked_target:{rel_path}"
    exists = os.path.isfile(target)

    if op == "exists":
        return (True, "exists_ok") if exists else (False, f"missing_file:{rel_path}")
    if not exists:
        return False, f"missing_file:{rel_path}"

    raw_bytes = Path(target).read_bytes()
    if op == "non_empty":
        return (True, f"non_empty_ok:{len(raw_bytes)}B") if len(raw_bytes) > 0 else (False, f"empty_file:{rel_path}")

    if op == "sha256_equals":
        actual_sha = hashlib.sha256(raw_bytes).hexdigest()
        want_sha = expected.strip().lower()
        return (
            (True, f"sha256_matched:{actual_sha[:12]}")
            if actual_sha == want_sha
            else (False, f"sha256_mismatch:expected={want_sha[:12]},actual={actual_sha[:12]}")
        )

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return False, f"non_utf8_file:{rel_path}"

    if op == "contains":
        return (True, "contains_ok") if expected in text else (False, f"substring_not_found:{rel_path}")

    if op == "json_key_equals":
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            return False, f"invalid_json:{exc}"
        cur: Any = doc
        for part in json_key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return False, f"missing_json_key:{json_key}"
        if isinstance(cur, bool):
            matched = expected.strip().lower() == ("true" if cur else "false") or str(cur) == expected
        elif isinstance(cur, str):
            matched = cur == expected
        else:
            matched = str(cur) == expected or _canonical_json(cur) == expected
        return (
            (True, f"json_key_matched:{json_key}")
            if matched
            else (False, f"json_key_mismatch:{json_key}={cur!r} != {expected!r}")
        )

    return False, f"unsupported_assert_op:{op}"


def _copy_workspace_out(workspace_dir: str, keep_sandbox_copy_dir: str, *, brain_dir: str) -> str:
    dest_abs = os.path.abspath(keep_sandbox_copy_dir)
    brain_abs = os.path.abspath(brain_dir)
    try:
        if os.path.normcase(os.path.commonpath([brain_abs, dest_abs])) == os.path.normcase(brain_abs):
            raise PermissionError("keep_sandbox_copy_dir must not point inside the canonical brain vault directory.")
    except ValueError:
        pass

    parent_dir = os.path.dirname(dest_abs)
    if os.path.lexists(parent_dir):
        _assert_not_reparse(parent_dir, "keep_sandbox_copy_dir parent")
    if os.path.lexists(dest_abs):
        _assert_not_reparse(dest_abs, "keep_sandbox_copy_dir")
        shutil.rmtree(dest_abs)
    os.makedirs(dest_abs, exist_ok=True)

    for root, dirs, files in os.walk(workspace_dir, followlinks=False):
        rel_root = os.path.relpath(root, workspace_dir)
        out_root = dest_abs if rel_root == "." else os.path.join(dest_abs, rel_root)
        os.makedirs(out_root, exist_ok=True)
        for d in dirs:
            _assert_not_reparse(os.path.join(root, d), "workspace copy dir")
            os.makedirs(os.path.join(out_root, d), exist_ok=True)
        for f in files:
            src_f = os.path.join(root, f)
            _assert_not_reparse(src_f, "workspace copy file")
            shutil.copy2(src_f, os.path.join(out_root, f))
    return dest_abs


def execute_promoted_skill(
    skill_id: str,
    *,
    inputs: dict[str, object] | None = None,
    actor: str,
    dry_run: bool = False,
    keep_sandbox_copy_dir: str | None = None,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str | None = None,
    playbooks_dir: str | None = None,
    skills_dir: str | None = None,
    experiences_dir: str | None = None,
    lexical_index_path: str | None = None,
    chroma_path: str | None = None,
    telemetry_db_path: str | None = None,
    security_dir: str | Path | None = None,
    _mid_run_hook: Callable[[int], None] | None = None,
) -> dict[str, object]:
    """Execute a verified Executable Skill inside an isolated closed-action sandbox with tamper-evident auditing."""
    t0 = time.monotonic()
    dirs = _resolve_vault_dirs(
        brain_dir=brain_dir,
        atoms_dir=atoms_dir,
        playbooks_dir=playbooks_dir,
        skills_dir=skills_dir,
        experiences_dir=experiences_dir,
        lexical_index_path=lexical_index_path,
        chroma_path=chroma_path,
        telemetry_db_path=telemetry_db_path,
    )
    _assert_not_reparse(dirs["brain_dir"], "brain_dir")

    # Fail closed immediately if the existing execution audit chain is symlinked or tampered.
    pre_chain = verify_skill_execution_audit_chain(dirs["brain_dir"])
    if not pre_chain["valid"]:
        errs = "; ".join(str(x) for x in pre_chain.get("errors", []))  # type: ignore[union-attr]
        if "symlink or reparse point" in errs:
            raise PermissionError(f"Execution audit chain symlink rejected: {errs}")
        raise ExecutableSkillValidationError(
            f"Refusing skill execution because skill-executions.jsonl audit chain is tampered: {errs}"
        )

    norm_skill_id = _normalize_skill_id(skill_id)
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now_dt.isoformat(timespec="seconds")
    exec_id = f"exec-{now_dt.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"

    def _log_denied(
        *,
        actor_label: str,
        status_val: str,
        denied_reason: str,
        killed_flag: bool = False,
        sk_sha: str = "",
        pb_id_val: str = "",
        pb_sha_val: str = "",
    ) -> dict[str, object]:
        dur_ms = round((time.monotonic() - t0) * 1000.0, 3)
        return _append_skill_execution_record(
            dirs["brain_dir"],
            {
                "execution_id": exec_id,
                "timestamp": now_iso,
                "skill_id": norm_skill_id,
                "skill_sha256": sk_sha,
                "playbook_id": pb_id_val,
                "playbook_sha256": pb_sha_val,
                "actor": actor_label,
                "status": status_val,
                "dry_run": bool(dry_run),
                "killed_by_switch": bool(killed_flag),
                "rollback_executed": False,
                "denied_reason": _sanitize_line(denied_reason, max_len=400),
                "step_results": [],
                "artifact_hashes": {},
                "duration_ms": dur_ms,
                "experience_id": None,
            },
        )

    try:
        clean_actor = _validate_human_actor(actor)
    except ExecutableSkillValidationError as exc:
        rec = _log_denied(
            actor_label=_sanitize_line(str(actor or "invalid_actor"), max_len=120) or "invalid_actor",
            status_val="denied",
            denied_reason=f"disallowed_actor:{exc}",
        )
        raise ExecutableSkillExecutionDeniedError(str(exc), audit_record=rec) from exc

    # Pre-execution emergency kill-switch gate
    ks_pre = get_skill_kill_switch_status(brain_dir=dirs["brain_dir"], security_dir=security_dir)
    is_killed_pre, ks_reason_pre = _is_skill_killed(ks_pre, norm_skill_id)
    if is_killed_pre:
        rec = _log_denied(
            actor_label=clean_actor,
            status_val="killed_by_switch",
            denied_reason=f"kill_switch_engaged:{ks_reason_pre}",
            killed_flag=True,
        )
        raise ExecutableSkillExecutionDeniedError(
            f"Execution of skill '{norm_skill_id}' blocked by emergency kill-switch ({ks_reason_pre}).",
            audit_record=rec,
        )

    # Full fail-closed cryptographic integrity verification (skill -> decision log -> Playbook -> Atoms -> AST)
    rep = verify_executable_skill_integrity(
        norm_skill_id,
        brain_dir=dirs["brain_dir"],
        atoms_dir=dirs["atoms_dir"],
        playbooks_dir=dirs["playbooks_dir"],
        skills_dir=dirs["skills_dir"],
        telemetry_db_path=dirs["telemetry_db_path"],
        security_dir=security_dir,
    )
    if not rep.valid:
        killed_flag = rep.status == "killed" or rep.reason.startswith("kill_switch_engaged:")
        rec = _log_denied(
            actor_label=clean_actor,
            status_val="killed_by_switch" if killed_flag else "denied",
            denied_reason=rep.reason,
            killed_flag=killed_flag,
            sk_sha=rep.sha256,
            pb_id_val=rep.playbook_id,
            pb_sha_val=rep.playbook_sha256,
        )
        raise ExecutableSkillExecutionDeniedError(
            f"Refusing to execute skill '{norm_skill_id}' (status={rep.status}, reason={rep.reason}).",
            audit_record=rec,
        )

    # Validate runtime inputs against input_schema + DLP secret & risk scanning
    try:
        validated_inputs = _validate_runtime_inputs(inputs, rep.input_schema)
    except ExecutableSkillSecretBlockedError as exc:
        rec = _log_denied(
            actor_label=clean_actor,
            status_val="denied",
            denied_reason=f"input_secret_blocked:{exc}",
            sk_sha=rep.sha256,
            pb_id_val=rep.playbook_id,
            pb_sha_val=rep.playbook_sha256,
        )
        raise ExecutableSkillSecretBlockedError(str(exc), audit_record=rec) from exc
    except ExecutableSkillValidationError as exc:
        rec = _log_denied(
            actor_label=clean_actor,
            status_val="denied",
            denied_reason=f"input_validation_failed:{exc}",
            sk_sha=rep.sha256,
            pb_id_val=rep.playbook_id,
            pb_sha_val=rep.playbook_sha256,
        )
        raise ExecutableSkillValidationError(str(exc), audit_record=rec) from exc

    if keep_sandbox_copy_dir is not None:
        dest_check = os.path.abspath(keep_sandbox_copy_dir)
        try:
            if os.path.normcase(os.path.commonpath([dirs["brain_dir"], dest_check])) == os.path.normcase(
                dirs["brain_dir"]
            ):
                raise PermissionError("keep_sandbox_copy_dir cannot reside inside the canonical brain vault.")
        except ValueError:
            pass
        if os.path.lexists(dest_check):
            _assert_not_reparse(dest_check, "keep_sandbox_copy_dir")

    # Dry-run mode: return verified execution plan, step preview, and rollback checkpoints without subprocesses
    if dry_run:
        step_preview: list[dict[str, object]] = []
        for s in rep.step_actions:
            atype = str(s["action_type"])
            preview_item: dict[str, object] = {
                "step_index": s["step_index"],
                "playbook_step": s["playbook_step"],
                "action_type": atype,
                "status": "dry_run_preview",
            }
            if atype == "vault_recall":
                preview_item["resolved_query"] = _interpolate_template(str(s["query"]), validated_inputs)
                preview_item["output_file"] = s["output_file"]
            elif atype == "vault_audit":
                preview_item["audit_kind"] = s["audit_kind"]
                preview_item["output_file"] = s["output_file"]
            elif atype == "workspace_write":
                preview_item["rel_path"] = s["rel_path"]
                preview_item["resolved_bytes"] = len(
                    _interpolate_template(str(s["content"]), validated_inputs).encode("utf-8")
                )
            elif atype == "workspace_assert":
                preview_item["rel_path"] = s["rel_path"]
                preview_item["op"] = s["op"]
            elif atype == "sandbox_python":
                preview_item["script_sha256"] = _sha256_text(str(s["script_body"]))
                preview_item["output_file"] = s.get("output_file", "")
            step_preview.append(preview_item)

        dur_ms = round((time.monotonic() - t0) * 1000.0, 3)
        audit_rec = _append_skill_execution_record(
            dirs["brain_dir"],
            {
                "execution_id": exec_id,
                "timestamp": now_iso,
                "skill_id": rep.skill_id,
                "skill_sha256": rep.sha256,
                "playbook_id": rep.playbook_id,
                "playbook_sha256": rep.playbook_sha256,
                "actor": clean_actor,
                "status": "dry_run_verified",
                "dry_run": True,
                "killed_by_switch": False,
                "rollback_executed": False,
                "denied_reason": "",
                "step_results": step_preview,
                "artifact_hashes": {},
                "duration_ms": dur_ms,
                "experience_id": None,
            },
        )
        return {
            "execution_id": exec_id,
            "execution_index": audit_rec["execution_index"],
            "execution_sha256": audit_rec["execution_sha256"],
            "skill_id": rep.skill_id,
            "skill_sha256": rep.sha256,
            "playbook_id": rep.playbook_id,
            "playbook_sha256": rep.playbook_sha256,
            "status": "dry_run_verified",
            "dry_run": True,
            "killed_by_switch": False,
            "rollback_executed": False,
            "validated_inputs": validated_inputs,
            "step_preview": step_preview,
            "step_results": step_preview,
            "expected_outputs": list(rep.expected_outputs),
            "rollback_checkpoints": list(rep.failure_recovery),
            "artifact_hashes": {},
            "duration_ms": dur_ms,
        }

    # Snapshot canonical vault hashes before live execution to verify post-run vault immutability
    skill_file_path = os.path.join(dirs["skills_dir"], rep.filename)
    playbook_file_path = os.path.join(dirs["playbooks_dir"], f"{rep.playbook_id}.md")
    pre_vault_hashes: dict[str, str] = {
        skill_file_path: rep.sha256,
        playbook_file_path: rep.playbook_sha256,
    }
    for atom_name in rep.backing_atoms:
        apath = safe_file_in_dir(dirs["atoms_dir"], atom_name, allowed_exts={".md"}, must_exist=True)
        if not apath or not os.path.isfile(apath):
            raise ExecutableSkillExecutionDeniedError(f"Backing atom disappeared before execution: {atom_name}")
        pre_vault_hashes[apath] = file_sha256(apath) or ""

    skill_projects = tuple(c for c in rep.contexts if str(c).strip().lower().startswith("project:"))

    step_results: list[dict[str, object]] = []
    artifact_hashes: dict[str, str] = {}
    final_status = "completed"
    killed_by_switch = False
    rollback_executed = False
    copied_sandbox_path: str | None = None
    deadline = time.monotonic() + rep.timeout_seconds

    with tempfile.TemporaryDirectory(prefix="sb-skill-sandbox-") as raw_sandbox_dir:
        sandbox_root = os.path.realpath(raw_sandbox_dir)
        _assert_not_reparse(sandbox_root, "sandbox_root")

        workspace_dir = os.path.join(sandbox_root, "workspace")
        guard_dir = os.path.join(sandbox_root, "_guard")
        scripts_dir = os.path.join(sandbox_root, "_scripts")
        sandbox_tmp_dir = os.path.join(sandbox_root, "_tmp")
        for sub in (workspace_dir, guard_dir, scripts_dir, sandbox_tmp_dir):
            os.makedirs(sub, exist_ok=True)
            _assert_not_reparse(sub, "sandbox subdirectory")

        inputs_json_path = os.path.join(sandbox_root, "inputs.json")
        sitecustomize_path = os.path.join(guard_dir, "sitecustomize.py")
        runner_path = os.path.join(guard_dir, "_runner.py")

        try:
            Path(inputs_json_path).write_bytes(
                (json.dumps(validated_inputs, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
            )
            Path(sitecustomize_path).write_bytes(_SANDBOX_SITECUSTOMIZE_SOURCE.encode("utf-8"))
            Path(runner_path).write_bytes(_SANDBOX_RUNNER_SOURCE.encode("utf-8"))
            for ro_file in (inputs_json_path, sitecustomize_path, runner_path):
                os.chmod(ro_file, stat.S_IREAD)

            pre_workspace_snapshot = _snapshot_workspace(workspace_dir)

            for idx_zero, step in enumerate(rep.step_actions):
                step_num = int(step["step_index"])
                atype = str(step["action_type"])
                step_t0 = time.monotonic()

                if _mid_run_hook is not None:
                    _mid_run_hook(idx_zero)

                ks_live = get_skill_kill_switch_status(brain_dir=dirs["brain_dir"], security_dir=security_dir)
                is_killed_now, ks_reason_now = _is_skill_killed(ks_live, rep.skill_id)
                if is_killed_now:
                    killed_by_switch = True
                    final_status = "killed_by_switch"
                    _restore_workspace_snapshot(workspace_dir, pre_workspace_snapshot)
                    rollback_executed = True
                    step_results.append(
                        {
                            "step_index": step_num,
                            "action_type": atype,
                            "status": "killed_by_switch",
                            "detail": f"kill_switch_engaged:{ks_reason_now}",
                            "duration_ms": round((time.monotonic() - step_t0) * 1000.0, 3),
                        }
                    )
                    break

                if time.monotonic() >= deadline:
                    final_status = "timed_out"
                    _restore_workspace_snapshot(workspace_dir, pre_workspace_snapshot)
                    rollback_executed = True
                    step_results.append(
                        {
                            "step_index": step_num,
                            "action_type": atype,
                            "status": "timed_out",
                            "detail": f"timeout_exceeded:{rep.timeout_seconds}s",
                            "duration_ms": round((time.monotonic() - step_t0) * 1000.0, 3),
                        }
                    )
                    break

                # Re-validate step definition against closed catalog at runtime (defense-in-depth)
                _validate_and_normalize_step_actions(
                    [{k: v for k, v in step.items() if k != "step_index"}],
                    (str(step["playbook_step"]),),
                    rep.input_schema,
                )
                if atype == "vault_recall":
                    resolved_query = _interpolate_template(str(step["query"]), validated_inputs)
                    _scan_text_for_secrets_and_risks(resolved_query, context_label=f"step[{step_num}].resolved_query")
                    n_res = int(step["n_results"])
                    out_file = str(step["output_file"])
                    recall_opts = RecallOptions(
                        n_results=n_res,
                        follow_links=False,
                        include_statuses={"accepted"},
                        projects=skill_projects,
                        retrieval_mode="lexical",
                        telemetry_db_path=dirs["telemetry_db_path"],
                    )
                    try:
                        direct, _ = search_memory(
                            resolved_query,
                            recall_opts,
                            atoms_dir=dirs["atoms_dir"],
                            index_path=dirs["lexical_index_path"],
                            chroma_path=dirs["chroma_path"],
                        )
                    except (MemoryRecallUnavailable, LexicalIndexUnavailable):
                        direct = []
                    payload = {
                        "query": resolved_query,
                        "count": len(direct),
                        "results": [r.as_dict() for r in direct],
                    }
                    raw_out_bytes = (
                        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
                    ).encode("utf-8")
                    written_path = _write_workspace_file_safe(
                        workspace_dir,
                        out_file,
                        raw_out_bytes,
                        max_output_bytes=rep.max_output_bytes,
                    )
                    step_results.append(
                        {
                            "step_index": step_num,
                            "action_type": atype,
                            "status": "ok",
                            "output_file": out_file,
                            "output_sha256": file_sha256(written_path) or "",
                            "result_count": len(direct),
                            "duration_ms": round((time.monotonic() - step_t0) * 1000.0, 3),
                        }
                    )

                elif atype == "vault_audit":
                    audit_kind = str(step["audit_kind"])
                    out_file = str(step["output_file"])
                    if audit_kind == "sources":
                        audit_data: object = audit_sources(
                            atoms_dir=dirs["atoms_dir"],
                            brain_dir=dirs["brain_dir"],
                            demote_stale=False,
                        )
                    elif audit_kind == "ledger":
                        audit_data = verify_vault_ledger(
                            brain_dir=dirs["brain_dir"],
                            atoms_dir=dirs["atoms_dir"],
                            playbooks_dir=dirs["playbooks_dir"],
                        )
                    elif audit_kind == "contradictions":
                        contra = detect_contradictions(
                            brain_dir=dirs["brain_dir"],
                            atoms_dir=dirs["atoms_dir"],
                            project_scope=skill_projects[0] if len(skill_projects) == 1 else None,
                            include_pending=False,
                            enable_nli=False,
                        )
                        audit_data = {
                            "contradiction_count": contra.get("contradiction_count", 0),
                            "high_severity_count": contra.get("high_severity_count", 0),
                            "drift_count": contra.get("drift_count", 0),
                            "contradictions": contra.get("contradictions", []),
                            "drift_issues": contra.get("drift_issues", []),
                        }
                    else:
                        raise ExecutableSkillValidationError(f"Unsupported audit_kind: {audit_kind!r}")

                    raw_out_bytes = (
                        json.dumps(audit_data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
                    ).encode("utf-8")
                    written_path = _write_workspace_file_safe(
                        workspace_dir,
                        out_file,
                        raw_out_bytes,
                        max_output_bytes=rep.max_output_bytes,
                    )
                    step_results.append(
                        {
                            "step_index": step_num,
                            "action_type": atype,
                            "status": "ok",
                            "audit_kind": audit_kind,
                            "output_file": out_file,
                            "output_sha256": file_sha256(written_path) or "",
                            "duration_ms": round((time.monotonic() - step_t0) * 1000.0, 3),
                        }
                    )

                elif atype == "workspace_write":
                    rel_path = str(step["rel_path"])
                    resolved_content = _interpolate_template(str(step["content"]), validated_inputs)
                    _scan_text_for_secrets_and_risks(
                        resolved_content, context_label=f"step[{step_num}].resolved_content"
                    )
                    written_path = _write_workspace_file_safe(
                        workspace_dir,
                        rel_path,
                        resolved_content.encode("utf-8"),
                        max_output_bytes=rep.max_output_bytes,
                    )
                    step_results.append(
                        {
                            "step_index": step_num,
                            "action_type": atype,
                            "status": "ok",
                            "rel_path": rel_path,
                            "output_sha256": file_sha256(written_path) or "",
                            "duration_ms": round((time.monotonic() - step_t0) * 1000.0, 3),
                        }
                    )

                elif atype == "workspace_assert":
                    rel_path = str(step["rel_path"])
                    op = str(step["op"])
                    resolved_expected = _interpolate_template(str(step.get("expected", "")), validated_inputs)
                    json_key = str(step.get("json_key", ""))
                    ok_assert, detail = _evaluate_workspace_assert(
                        workspace_dir,
                        rel_path=rel_path,
                        op=op,
                        expected=resolved_expected,
                        json_key=json_key,
                    )
                    if not ok_assert:
                        final_status = "failed_rolled_back"
                        _restore_workspace_snapshot(workspace_dir, pre_workspace_snapshot)
                        rollback_executed = True
                        step_results.append(
                            {
                                "step_index": step_num,
                                "action_type": atype,
                                "status": "assertion_failed",
                                "rel_path": rel_path,
                                "op": op,
                                "detail": detail,
                                "duration_ms": round((time.monotonic() - step_t0) * 1000.0, 3),
                            }
                        )
                        break
                    step_results.append(
                        {
                            "step_index": step_num,
                            "action_type": atype,
                            "status": "ok",
                            "rel_path": rel_path,
                            "op": op,
                            "detail": detail,
                            "duration_ms": round((time.monotonic() - step_t0) * 1000.0, 3),
                        }
                    )

                elif atype == "sandbox_python":
                    script_body = str(step["script_body"])
                    declared_out = str(step.get("output_file") or "")
                    script_path = os.path.join(scripts_dir, f"step_{step_num:02d}.py")
                    Path(script_path).write_bytes(script_body.encode("utf-8"))
                    os.chmod(script_path, stat.S_IREAD)

                    stdout_log = os.path.join(sandbox_tmp_dir, f"step_{step_num:02d}.stdout")
                    stderr_log = os.path.join(sandbox_tmp_dir, f"step_{step_num:02d}.stderr")
                    child_env = _build_sandbox_child_env(
                        guard_dir=guard_dir,
                        workspace_dir=workspace_dir,
                        inputs_json_path=inputs_json_path,
                        sandbox_tmp_dir=sandbox_tmp_dir,
                    )

                    popen_kwargs: dict[str, Any] = {
                        "cwd": workspace_dir,
                        "env": child_env,
                        "stdin": subprocess.DEVNULL,
                        "shell": False,
                    }
                    if os.name == "nt":
                        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    else:
                        popen_kwargs["start_new_session"] = True

                    sub_killed = False
                    sub_timed_out = False
                    killed_detail = ""
                    with open(stdout_log, "wb") as out_fh, open(stderr_log, "wb") as err_fh:
                        proc = subprocess.Popen(
                            [sys.executable, "-I", "-B", "-X", "utf8", runner_path, script_path],
                            stdout=out_fh,
                            stderr=err_fh,
                            **popen_kwargs,
                        )
                        while proc.poll() is None:
                            ks_mid = get_skill_kill_switch_status(
                                brain_dir=dirs["brain_dir"], security_dir=security_dir
                            )
                            killed_now, ks_mid_reason = _is_skill_killed(ks_mid, rep.skill_id)
                            if killed_now:
                                sub_killed = True
                                killed_detail = ks_mid_reason
                                _terminate_process_tree(proc)
                                break
                            if time.monotonic() >= deadline:
                                sub_timed_out = True
                                _terminate_process_tree(proc)
                                break
                            time.sleep(POLL_INTERVAL_SECONDS)

                    stdout_text, stdout_trunc = _read_bounded_redacted_stream(stdout_log, rep.max_output_bytes)
                    stderr_text, stderr_trunc = _read_bounded_redacted_stream(stderr_log, rep.max_output_bytes)

                    if sub_killed:
                        killed_by_switch = True
                        final_status = "killed_by_switch"
                        _restore_workspace_snapshot(workspace_dir, pre_workspace_snapshot)
                        rollback_executed = True
                        step_results.append(
                            {
                                "step_index": step_num,
                                "action_type": atype,
                                "status": "killed_by_switch",
                                "returncode": proc.returncode,
                                "detail": f"kill_switch_engaged:{killed_detail}",
                                "stdout": stdout_text,
                                "stderr": stderr_text,
                                "output_truncated": bool(stdout_trunc or stderr_trunc),
                                "duration_ms": round((time.monotonic() - step_t0) * 1000.0, 3),
                            }
                        )
                        break

                    if sub_timed_out:
                        final_status = "timed_out"
                        _restore_workspace_snapshot(workspace_dir, pre_workspace_snapshot)
                        rollback_executed = True
                        step_results.append(
                            {
                                "step_index": step_num,
                                "action_type": atype,
                                "status": "timed_out",
                                "returncode": proc.returncode,
                                "detail": f"timeout_exceeded:{rep.timeout_seconds}s",
                                "stdout": stdout_text,
                                "stderr": stderr_text,
                                "output_truncated": bool(stdout_trunc or stderr_trunc),
                                "duration_ms": round((time.monotonic() - step_t0) * 1000.0, 3),
                            }
                        )
                        break

                    rc = int(proc.returncode if proc.returncode is not None else 1)
                    out_sha = ""
                    missing_declared_output = False
                    if rc == 0 and declared_out:
                        resolved_out = resolve_under(workspace_dir, declared_out, must_exist=False)
                        if (
                            not resolved_out
                            or not os.path.isfile(resolved_out)
                            or _is_reparse_or_link(resolved_out)
                        ):
                            missing_declared_output = True
                        else:
                            out_sha = file_sha256(resolved_out) or ""

                    if rc != 0 or missing_declared_output:
                        final_status = "failed_rolled_back"
                        _restore_workspace_snapshot(workspace_dir, pre_workspace_snapshot)
                        rollback_executed = True
                        step_results.append(
                            {
                                "step_index": step_num,
                                "action_type": atype,
                                "status": "failed",
                                "returncode": rc,
                                "detail": (
                                    f"missing_declared_output:{declared_out}"
                                    if missing_declared_output
                                    else f"nonzero_exit:{rc}"
                                ),
                                "stdout": stdout_text,
                                "stderr": stderr_text,
                                "output_truncated": bool(stdout_trunc or stderr_trunc),
                                "duration_ms": round((time.monotonic() - step_t0) * 1000.0, 3),
                            }
                        )
                        break

                    step_results.append(
                        {
                            "step_index": step_num,
                            "action_type": atype,
                            "status": "ok",
                            "returncode": 0,
                            "output_file": declared_out,
                            "output_sha256": out_sha,
                            "stdout": stdout_text,
                            "stderr": stderr_text,
                            "output_truncated": bool(stdout_trunc or stderr_trunc),
                            "duration_ms": round((time.monotonic() - step_t0) * 1000.0, 3),
                        }
                    )

            if final_status == "completed":
                try:
                    artifact_hashes = _hash_workspace_artifacts(
                        workspace_dir,
                        max_total_bytes=max(rep.max_output_bytes * 4, 1048576),
                    )
                except (PermissionError, ExecutableSkillValidationError) as exc:
                    final_status = "failed_rolled_back"
                    _restore_workspace_snapshot(workspace_dir, pre_workspace_snapshot)
                    rollback_executed = True
                    artifact_hashes = {}
                    step_results.append(
                        {
                            "step_index": len(rep.step_actions),
                            "action_type": "workspace_finalize",
                            "status": "failed",
                            "detail": str(exc),
                            "duration_ms": 0.0,
                        }
                    )

            if keep_sandbox_copy_dir is not None:
                copied_sandbox_path = _copy_workspace_out(
                    workspace_dir,
                    keep_sandbox_copy_dir,
                    brain_dir=dirs["brain_dir"],
                )
        finally:
            _clear_readonly_tree(sandbox_root)

    # Post-execution canonical vault SHA-256 invariance verification
    for canon_path, expected_hash in pre_vault_hashes.items():
        _assert_not_reparse(canon_path, "canonical vault file")
        actual_hash = file_sha256(canon_path) or ""
        if actual_hash != expected_hash:
            raise ExecutableSkillValidationError(
                f"Post-execution canonical vault integrity violation on '{canon_path}': "
                f"expected {expected_hash}, found {actual_hash}."
            )

    duration_ms = round((time.monotonic() - t0) * 1000.0, 3)

    # Persist episodic ExperienceRecord in brain/4-experiences/ linking backing atoms & executable skill tool
    exp_saved = save_experience_record(
        goal=f"Execute promoted skill {rep.skill_id}: {rep.title}",
        result_summary=(
            f"Executable Skill {rep.skill_id} finished with status={final_status} "
            f"(steps={len(step_results)}/{len(rep.step_actions)}, artifacts={len(artifact_hashes)}, "
            f"rollback_executed={rollback_executed}, killed_by_switch={killed_by_switch}) in {duration_ms:.1f}ms."
        ),
        success=(final_status == "completed"),
        context=list(rep.contexts),
        plan=[str(s["playbook_step"]) for s in rep.step_actions],
        actions=[f"step_{r['step_index']}:{r['action_type']}:{r['status']}" for r in step_results],
        tools_used=[f"executable_skill:{rep.skill_id}", f"playbook:{rep.playbook_id}"],
        referenced_atoms=list(rep.backing_atoms),
        candidate_skills=[rep.skill_id],
        experiences_dir=dirs["experiences_dir"],
        inbox_dir=os.path.join(dirs["brain_dir"], "0-raw", "inbox"),
        brain_dir=dirs["brain_dir"],
    )
    experience_id = str(exp_saved["experience_id"])

    audit_rec = _append_skill_execution_record(
        dirs["brain_dir"],
        {
            "execution_id": exec_id,
            "timestamp": now_iso,
            "skill_id": rep.skill_id,
            "skill_sha256": rep.sha256,
            "playbook_id": rep.playbook_id,
            "playbook_sha256": rep.playbook_sha256,
            "actor": clean_actor,
            "status": final_status,
            "dry_run": False,
            "killed_by_switch": killed_by_switch,
            "rollback_executed": rollback_executed,
            "denied_reason": "",
            "step_results": step_results,
            "artifact_hashes": artifact_hashes,
            "duration_ms": duration_ms,
            "experience_id": experience_id,
        },
    )

    return {
        "execution_id": exec_id,
        "execution_index": audit_rec["execution_index"],
        "execution_sha256": audit_rec["execution_sha256"],
        "prev_execution_sha256": audit_rec["prev_execution_sha256"],
        "skill_id": rep.skill_id,
        "skill_sha256": rep.sha256,
        "playbook_id": rep.playbook_id,
        "playbook_sha256": rep.playbook_sha256,
        "status": final_status,
        "success": final_status == "completed",
        "dry_run": False,
        "killed_by_switch": killed_by_switch,
        "rollback_executed": rollback_executed,
        "step_results": step_results,
        "artifact_hashes": artifact_hashes,
        "experience_id": experience_id,
        "experience_filename": exp_saved["filename"],
        "sandbox_copy_dir": copied_sandbox_path,
        "duration_ms": duration_ms,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage & execute owner-gated Second Brain Stage 4 Executable Skills.")
    sub = parser.add_subparsers(dest="command", required=True)

    cand_p = sub.add_parser("candidates", help="Inspect verified Playbooks for Executable Skill readiness.")
    cand_p.add_argument("--json", action="store_true")

    list_p = sub.add_parser("list", help="List Executable Skills and verify live cryptographic integrity.")
    list_p.add_argument("--all", action="store_true", help="Include stale, revoked, or killed skills.")
    list_p.add_argument("--json", action="store_true")

    ver_p = sub.add_parser("verify", help="Verify cryptographic integrity of one Executable Skill.")
    ver_p.add_argument("skill_id")
    ver_p.add_argument("--json", action="store_true")

    rev_p = sub.add_parser("revoke", help="Revoke an active Executable Skill via CAS.")
    rev_p.add_argument("skill_id")
    rev_p.add_argument("--expected-sha256", required=True)
    rev_p.add_argument("--actor", required=True)
    rev_p.add_argument("--reason", required=True)

    ks_p = sub.add_parser("kill-switch", help="Inspect, engage, or release the Executable Skill kill-switch.")
    ks_p.add_argument("--status", action="store_true", help="Display current kill-switch state.")
    ks_p.add_argument("--engage", action="store_true")
    ks_p.add_argument("--release", action="store_true")
    ks_p.add_argument("--scope", default="global")
    ks_p.add_argument("--actor", default="")
    ks_p.add_argument("--reason", default="")
    ks_p.add_argument("--json", action="store_true")

    exec_p = sub.add_parser("execute", help="Execute a promoted Executable Skill inside the isolated sandbox.")
    exec_p.add_argument("skill_id")
    exec_p.add_argument("--actor", required=True)
    exec_p.add_argument("--inputs-json", default="{}")
    exec_p.add_argument("--dry-run", action="store_true")
    exec_p.add_argument("--keep-sandbox-dir", default=None)
    exec_p.add_argument("--json", action="store_true")

    hist_p = sub.add_parser("executions", help="List hash-chained skill execution audit entries.")
    hist_p.add_argument("--skill-id", default=None)
    hist_p.add_argument("--limit", type=int, default=50)
    hist_p.add_argument("--json", action="store_true")

    audit_p = sub.add_parser("verify-audit", help="Verify the hash-chained skill-executions.jsonl audit log.")
    audit_p.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    cli_brain = resolve_active_brain_dir()

    if args.command == "candidates":
        cands = inspect_executable_skill_candidates(brain_dir=cli_brain)
        if args.json:
            print(json.dumps(cands, indent=2, sort_keys=True))
        else:
            if not cands:
                print("No active verified Playbook candidates found.")
            for c in cands:
                print(
                    f"{c['playbook_id']:28} | stage={c['maturity_stage']:22} | "
                    f"min_cites={c['min_grounded_citations']} | episodes={c['successful_episodes']}"
                )
        return 0

    if args.command == "list":
        skills = list_executable_skills(brain_dir=cli_brain, active_only=not args.all)
        if args.json:
            print(json.dumps([s.as_dict() for s in skills], indent=2, sort_keys=True))
        else:
            if not skills:
                print("No matching Executable Skills found.")
            for s in skills:
                badge = "ACTIVE" if s.valid else f"INACTIVE({s.status}:{s.reason})"
                print(f"[{badge}] {s.skill_id:28} | playbook={s.playbook_id} | sha256={s.sha256[:12]}")
        return 0

    if args.command == "verify":
        rep = verify_executable_skill_integrity(args.skill_id, brain_dir=cli_brain)
        if args.json:
            print(json.dumps(rep.as_dict(), indent=2, sort_keys=True))
        else:
            print(f"skill={rep.skill_id} valid={rep.valid} status={rep.status} reason={rep.reason}")
        return 0 if rep.valid else 1

    if args.command == "revoke":
        res = revoke_executable_skill(
            skill_id=args.skill_id,
            expected_skill_sha256=args.expected_sha256,
            actor=args.actor,
            reason=args.reason,
            brain_dir=cli_brain,
        )
        print(json.dumps(res, indent=2, sort_keys=True))
        return 0

    if args.command == "kill-switch":
        if args.engage and args.release:
            raise ExecutableSkillValidationError("Specify at most one of --engage or --release.")
        if args.engage or args.release:
            st = set_skill_kill_switch(
                engaged=bool(args.engage),
                scope=args.scope,
                actor=args.actor,
                reason=args.reason,
                brain_dir=cli_brain,
            )
        else:
            st = get_skill_kill_switch_status(brain_dir=cli_brain)
        print(json.dumps(st, indent=2, sort_keys=True))
        return 0

    if args.command == "execute":
        parsed_inputs = json.loads(args.inputs_json)
        res = execute_promoted_skill(
            skill_id=args.skill_id,
            inputs=parsed_inputs,
            actor=args.actor,
            dry_run=bool(args.dry_run),
            keep_sandbox_copy_dir=args.keep_sandbox_dir,
            brain_dir=cli_brain,
        )
        print(json.dumps(res, indent=2, sort_keys=True))
        return 0 if res.get("status") in {"completed", "dry_run_verified"} else 1

    if args.command == "executions":
        rows = list_skill_executions(brain_dir=cli_brain, skill_id=args.skill_id, limit=args.limit)
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0

    if args.command == "verify-audit":
        chain = verify_skill_execution_audit_chain(brain_dir=cli_brain)
        print(json.dumps(chain, indent=2, sort_keys=True))
        return 0 if chain["valid"] else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
