# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Autonomous agent runtime.

The runtime writes its own memories under ``brain/7-agent/memory/`` and its own
skills under ``brain/7-agent/skills/``. It can recall those memories on the next
step and run skills whose actions stay inside the existing sandbox allow-list.
Owner-signed atoms in ``brain/2-atoms/`` are not modified here.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cognitive_core import save_experience_record
from config import BRAIN_DIR, _is_reparse_or_link, safe_file_in_dir
from executable_skills import (
    ALLOWED_ACTION_TYPES,
    ALLOWED_ASSERT_OPS,
    ALLOWED_AUDIT_KINDS,
    MAX_WORKSPACE_WRITE_BYTES,
    POLL_INTERVAL_SECONDS,
    ExecutableSkillValidationError,
    _SANDBOX_RUNNER_SOURCE,
    _SANDBOX_SITECUSTOMIZE_SOURCE,
    _build_sandbox_child_env,
    _evaluate_workspace_assert,
    _is_skill_killed,
    _normalize_skill_id,
    _scan_text_for_secrets_and_risks,
    _terminate_process_tree,
    _validate_human_actor,
    _validate_sandbox_python_ast,
    _write_workspace_file_safe,
    get_skill_kill_switch_status,
    set_skill_kill_switch,
)
from inbox_workflow import scan_inbox_payload_risks
from memory_format import split_frontmatter
from memory_review import parse_frontmatter_values
from recall import RecallOptions, normalize_project_tag, project_tags_match, search_memory

MAX_AGENT_DEPTH = 3
DEFAULT_MAX_STEPS = 8
MAX_AGENT_STEPS = 24
DEFAULT_SCHEDULE_SECONDS = 900
MEMORY_TYPES = frozenset({"semantic", "episodic", "procedural", "self"})
_TOKEN_RE = re.compile(r"[A-Za-z0-9]{3,}")
_STEPS_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
_STEP_LINE_RE = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+\S", re.MULTILINE)
_LEARN_BATCH = 40
_LEARN_CLAIM_CHARS = 1800


class AgentRuntimeError(ValueError):
    """Raised when an agent write or run violates the runtime contract."""


def agent_root(brain_dir: str = BRAIN_DIR) -> str:
    return os.path.join(os.path.abspath(brain_dir), "7-agent")


def _dirs(brain_dir: str) -> dict[str, str]:
    root = agent_root(brain_dir)
    paths = {
        "root": root,
        "goals": os.path.join(root, "goals"),
        "memory": os.path.join(root, "memory"),
        "skills": os.path.join(root, "skills"),
        "runs": os.path.join(root, "runs"),
        "experiences": os.path.join(os.path.abspath(brain_dir), "4-experiences"),
        "inbox": os.path.join(os.path.abspath(brain_dir), "0-raw", "inbox"),
        "atoms": os.path.join(os.path.abspath(brain_dir), "2-atoms"),
        "executions": os.path.join(root, "executions.jsonl"),
        "schedule": os.path.join(root, "schedule.json"),
    }
    for key in ("goals", "memory", "skills", "runs", "experiences", "inbox"):
        os.makedirs(paths[key], exist_ok=True)
    return paths


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _slug(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    return (slug or "item")[:limit].strip("-") or "item"


def _agent_id(raw: str) -> str:
    cleaned = _slug(raw, 32)
    if not cleaned or cleaned in {"auto", "system", "llm", "model", "agent"}:
        raise AgentRuntimeError(f"Invalid agent id: {raw!r}")
    return cleaned


def _write_exclusive(path: str, text: str) -> None:
    data = text.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.write(fd, data) != len(data):
            raise OSError(f"Incomplete write: {path}")
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _frontmatter(text: str) -> dict[str, Any]:
    parsed = split_frontmatter(text)
    if not parsed:
        return {}
    return parse_frontmatter_values(parsed[0])


def _episode_exists(dirs: dict[str, str], episode_id: str) -> bool:
    token = str(episode_id or "").strip()
    if not token or "/" in token or "\\" in token:
        return False
    exp_dir = dirs["experiences"]
    if os.path.isdir(exp_dir):
        for name in os.listdir(exp_dir):
            if not name.endswith(".md"):
                continue
            path = os.path.join(exp_dir, name)
            if token in name:
                return True
            try:
                fm = _frontmatter(_read_text(path))
            except OSError:
                continue
            if str(fm.get("experience_id") or "") == token:
                return True
    log_path = dirs["executions"]
    if os.path.isfile(log_path):
        try:
            with open(log_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    if token and token in line:
                        return True
        except OSError:
            return False
    return False


def _quarantine(dirs: dict[str, str], claim: str, flags: tuple[str, ...]) -> str:
    digest = hashlib.sha256(claim.encode("utf-8")).hexdigest()[:16]
    name = f"agent-quarantine-{digest}.md"
    path = safe_file_in_dir(dirs["inbox"], name, allowed_exts={".md"}, must_exist=False)
    if not path:
        raise AgentRuntimeError("Could not quarantine the agent claim.")
    if not os.path.exists(path):
        body = (
            "---\norigin: agent_runtime\nstatus: quarantined\n---\n\n"
            f"Flags: {', '.join(flags)}\n\nThe claim was withheld from agent memory.\n"
        )
        _write_exclusive(path, body)
    return name


def write_agent_memory(
    agent_id: str,
    memory_type: str,
    claim: str,
    episode_id: str,
    *,
    project: str = "",
    filename: str | None = None,
    brain_dir: str = BRAIN_DIR,
) -> str:
    """Write one agent-tier memory. Returns the filename inside ``7-agent/memory``."""
    dirs = _dirs(brain_dir)
    author = _agent_id(agent_id)
    kind = str(memory_type or "").strip()
    if kind not in MEMORY_TYPES:
        raise AgentRuntimeError(f"memory_type must be one of {sorted(MEMORY_TYPES)}")
    text = str(claim or "").strip()
    if not text:
        raise AgentRuntimeError("Agent memory requires a claim.")
    flags = scan_inbox_payload_risks(text)
    if flags:
        _quarantine(dirs, text, flags)
        raise AgentRuntimeError(f"Agent claim quarantined: {', '.join(flags)}")
    name = filename or f"{_slug(text)}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}.md"
    if "2-atoms" in name or name.startswith(("/", "\\")) or ".." in name.replace("\\", "/"):
        raise AgentRuntimeError("Agent memory cannot be written into owner atoms.")
    target = safe_file_in_dir(dirs["memory"], name, allowed_exts={".md"}, must_exist=False)
    atoms_root = os.path.abspath(dirs["atoms"])
    if not target or os.path.commonpath([os.path.abspath(target), atoms_root]) == atoms_root:
        raise AgentRuntimeError("Agent memory path is outside 7-agent/memory.")
    if not _episode_exists(dirs, episode_id):
        raise AgentRuntimeError(f"Unknown episode: {episode_id}")
    if os.path.exists(target):
        raise AgentRuntimeError(f"Agent memory already exists: {name}")
    project_line = f"project: {json.dumps(project)}\n" if project else ""
    body = (
        "---\n"
        "type: agent_memory\n"
        f"memory_type: {kind}\n"
        "origin: agent_runtime\n"
        "review_status: agent_active\n"
        f"authored_by: agent_runtime:{author}\n"
        f"evidence_episode: {json.dumps(episode_id)}\n"
        f"{project_line}"
        "---\n\n"
        "## Claim\n"
        f"{text}\n"
    )
    _write_exclusive(target, body)
    return os.path.basename(target)


def _note_scope(tags: object) -> str:
    found: list[str] = []
    items = tags if isinstance(tags, (list, tuple)) else ()
    for raw in items:
        text = str(raw or "").strip()
        if text.lower().startswith("project:"):
            found.append(text)
    if len(found) > 1:
        return "mixed"
    return found[0] if found else ""


def _scope_visible(stored: str, requested: str) -> bool:
    asked = str(requested or "").strip()
    if not asked:
        return True
    if not stored or stored == "mixed":
        return False
    scope = normalize_project_tag(asked)
    return bool(scope) and bool(project_tags_match((stored,), (scope,)))


def _write_ledger(path: str, payload: dict[str, str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    data = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def search_agent_memory(query: str, *, brain_dir: str = BRAIN_DIR, project: str = "", limit: int = 6) -> list[dict[str, str]]:
    """Return agent_active memories whose claim shares a query term."""
    memory_dir = os.path.join(agent_root(brain_dir), "memory")
    if not os.path.isdir(memory_dir):
        return []
    terms = {token.casefold() for token in _TOKEN_RE.findall(query or "")}
    hits: list[dict[str, str]] = []
    for name in sorted(os.listdir(memory_dir)):
        if not name.endswith(".md"):
            continue
        path = safe_file_in_dir(memory_dir, name, allowed_exts={".md"}, must_exist=True)
        if not path:
            continue
        text = _read_text(path)
        fm = _frontmatter(text)
        if str(fm.get("review_status") or "") != "agent_active":
            continue
        if str(fm.get("superseded_by") or "").strip():
            continue
        if not _scope_visible(str(fm.get("project") or ""), project):
            continue
        parsed = split_frontmatter(text)
        body = "\n".join(parsed[1]) if parsed else text
        claim = body.split("## Claim", 1)[-1].strip() if "## Claim" in body else body.strip()
        if terms and not terms.intersection({token.casefold() for token in _TOKEN_RE.findall(claim)}):
            continue
        hits.append({"filename": name, "claim": claim, "authored_by": str(fm.get("authored_by") or "")})
        if len(hits) >= limit:
            break
    return hits


def _render_skill(
    skill_id: str,
    *,
    agent_id: str,
    trigger: str,
    steps: list[dict[str, Any]],
    status: str,
    successes: int,
    failures: int,
    project: str = "",
) -> str:
    payload = json.dumps(steps, indent=2, sort_keys=True)
    project_line = f"project: {json.dumps(project)}\n" if project else ""
    return (
        "---\n"
        f"skill_id: {skill_id}\n"
        f"status: {status}\n"
        f"trigger: {json.dumps(trigger)}\n"
        f"{project_line}"
        f"authored_by: agent_runtime:{agent_id}\n"
        f"successes: {successes}\n"
        f"failures: {failures}\n"
        "---\n\n"
        "## Ordered Sandboxed Steps\n\n"
        f"```json\n{payload}\n```\n"
    )


def _load_skill(path: str) -> dict[str, Any]:
    text = _read_text(path)
    fm = _frontmatter(text)
    match = _STEPS_RE.search(text)
    if not match:
        raise AgentRuntimeError(f"Skill file has no JSON steps: {os.path.basename(path)}")
    steps = json.loads(match.group(1))
    if not isinstance(steps, list) or not steps:
        raise AgentRuntimeError("Skill steps must be a non-empty list.")
    return {"frontmatter": fm, "steps": steps, "text": text, "path": path}


def _validate_steps(steps: list[dict[str, Any]]) -> None:
    if not steps:
        raise AgentRuntimeError("A skill needs at least one step.")
    for step in steps:
        action = str(step.get("action_type") or "")
        if action not in ALLOWED_ACTION_TYPES:
            raise AgentRuntimeError(f"Unsupported skill action: {action!r}")
        if action == "sandbox_python":
            _validate_sandbox_python_ast(str(step.get("script_body") or ""))
        elif action == "vault_audit":
            kind = str(step.get("audit_kind") or "")
            if kind not in ALLOWED_AUDIT_KINDS:
                raise AgentRuntimeError(f"audit_kind must be one of {sorted(ALLOWED_AUDIT_KINDS)}")
        elif action == "workspace_write":
            rel_path = str(step.get("rel_path") or "")
            content = step.get("content")
            if not rel_path or not isinstance(content, str):
                raise AgentRuntimeError("workspace_write requires rel_path and string content.")
            if len(content.encode("utf-8")) > MAX_WORKSPACE_WRITE_BYTES:
                raise AgentRuntimeError("workspace_write content is too large.")
        elif action == "workspace_assert":
            if not str(step.get("rel_path") or ""):
                raise AgentRuntimeError("workspace_assert requires rel_path.")
            op = str(step.get("op") or "")
            if op not in ALLOWED_ASSERT_OPS:
                raise AgentRuntimeError(f"workspace_assert op must be one of {sorted(ALLOWED_ASSERT_OPS)}")


def draft_agent_skill(
    agent_id: str,
    skill_id: str,
    steps: list[dict[str, Any]],
    *,
    trigger: str,
    brain_dir: str = BRAIN_DIR,
    project: str = "",
) -> str:
    dirs = _dirs(brain_dir)
    author = _agent_id(agent_id)
    norm_id = _normalize_skill_id(skill_id)
    _validate_steps(steps)
    name = f"{norm_id}.md"
    target = safe_file_in_dir(dirs["skills"], name, allowed_exts={".md"}, must_exist=False)
    if not target:
        raise AgentRuntimeError(f"Unsafe skill filename: {name}")
    body = _render_skill(
        norm_id,
        agent_id=author,
        trigger=trigger,
        steps=steps,
        status="agent_trial",
        successes=0,
        failures=0,
        project=project,
    )
    if os.path.exists(target):
        os.remove(target)
    _write_exclusive(target, body)
    return name


def _append_execution(dirs: dict[str, str], record: dict[str, Any]) -> None:
    line = json.dumps(record, sort_keys=True) + "\n"
    with open(dirs["executions"], "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def _run_python(script_body: str, *, skill_id: str, security_dir: str | None, timeout: float) -> tuple[str, str]:
    """Run one script inside the same closed sandbox used by owner-promoted skills."""
    with tempfile.TemporaryDirectory(prefix="sb-agent-skill-") as raw_root:
        sandbox_root = os.path.realpath(raw_root)
        workspace_dir = os.path.join(sandbox_root, "workspace")
        guard_dir = os.path.join(sandbox_root, "_guard")
        scripts_dir = os.path.join(sandbox_root, "_scripts")
        sandbox_tmp_dir = os.path.join(sandbox_root, "_tmp")
        for sub in (workspace_dir, guard_dir, scripts_dir, sandbox_tmp_dir):
            os.makedirs(sub, exist_ok=True)
        inputs_json_path = os.path.join(sandbox_root, "inputs.json")
        runner_path = os.path.join(guard_dir, "_runner.py")
        script_path = os.path.join(scripts_dir, "step.py")
        Path(inputs_json_path).write_text("{}\n", encoding="utf-8")
        Path(os.path.join(guard_dir, "sitecustomize.py")).write_bytes(_SANDBOX_SITECUSTOMIZE_SOURCE.encode("utf-8"))
        Path(runner_path).write_bytes(_SANDBOX_RUNNER_SOURCE.encode("utf-8"))
        Path(script_path).write_text(script_body, encoding="utf-8")
        stdout_log = os.path.join(sandbox_tmp_dir, "stdout.txt")
        stderr_log = os.path.join(sandbox_tmp_dir, "stderr.txt")
        child_env = _build_sandbox_child_env(
            guard_dir=guard_dir,
            workspace_dir=workspace_dir,
            inputs_json_path=inputs_json_path,
            sandbox_tmp_dir=sandbox_tmp_dir,
        )
        with open(stdout_log, "wb") as out_fh, open(stderr_log, "wb") as err_fh:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-B", "-X", "utf8", runner_path, script_path],
                cwd=workspace_dir,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=out_fh,
                stderr=err_fh,
                shell=False,
                start_new_session=True,
            )
            deadline = time.monotonic() + timeout
            stopped = False
            reason = ""
            while proc.poll() is None:
                killed, ks_reason = _is_skill_killed(get_skill_kill_switch_status(security_dir=security_dir), skill_id)
                if killed:
                    stopped = True
                    reason = ks_reason
                    _terminate_process_tree(proc)
                    break
                if time.monotonic() >= deadline:
                    _terminate_process_tree(proc)
                    return "failed", "timeout"
                time.sleep(POLL_INTERVAL_SECONDS)
        if stopped:
            return "stopped", reason
        if proc.returncode not in (0, None):
            detail = Path(stderr_log).read_text(encoding="utf-8", errors="replace")[:300]
            return "failed", detail or f"exit {proc.returncode}"
        return "ok", Path(stdout_log).read_text(encoding="utf-8", errors="replace")[:300]


def _run_agent_step(
    step: dict[str, Any],
    *,
    inputs: dict[str, Any] | None,
    skill_id: str,
    brain_dir: str,
    security_dir: str | None,
    timeout: float,
    workspace: str,
) -> tuple[str, str, bool]:
    """Run one allow-listed action. The third value is true only when the step did real work."""
    action = str(step.get("action_type") or "")
    try:
        if action == "vault_recall":
            query = str(inputs.get("query") if inputs and inputs.get("query") else step.get("query") or "")
            _scan_text_for_secrets_and_risks(query, context_label="vault_recall.query")
            found = _recall(query, brain_dir, security_dir)
            payload = json.dumps({"query": query, "count": len(found), "filenames": found}, sort_keys=True) + "\n"
            out_file = str(step.get("output_file") or "recall.json")
            _write_workspace_file_safe(
                workspace,
                out_file,
                payload.encode("utf-8"),
                max_output_bytes=MAX_WORKSPACE_WRITE_BYTES,
            )
            return "ok", f"recall {len(found)}", bool(found)
        if action == "vault_audit":
            kind = str(step.get("audit_kind") or "")
            report = _run_vault_audit(kind, brain_dir)
            raw = (json.dumps(report, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")
            _write_workspace_file_safe(
                workspace,
                str(step.get("output_file") or "audit.json"),
                raw,
                max_output_bytes=MAX_WORKSPACE_WRITE_BYTES,
            )
            count = len(report) if isinstance(report, list) else ""
            suffix = f" {count}" if count != "" else ""
            return "ok", f"audit {kind}{suffix}", True
        if action == "workspace_write":
            content = str(step.get("content") or "")
            _scan_text_for_secrets_and_risks(content, context_label="workspace_write.content")
            _write_workspace_file_safe(
                workspace,
                str(step.get("rel_path") or ""),
                content.encode("utf-8"),
                max_output_bytes=MAX_WORKSPACE_WRITE_BYTES,
            )
            return "ok", f"wrote {step.get('rel_path')}", True
        if action == "workspace_assert":
            ok, assert_detail = _evaluate_workspace_assert(
                workspace,
                rel_path=str(step.get("rel_path") or ""),
                op=str(step.get("op") or ""),
                expected=str(step.get("expected") or ""),
                json_key=str(step.get("json_key") or ""),
            )
            if not ok:
                return "failed", assert_detail, False
            return "ok", assert_detail, True
        if action == "sandbox_python":
            run_status, run_detail = _run_python(
                str(step.get("script_body") or ""),
                skill_id=skill_id,
                security_dir=security_dir,
                timeout=timeout,
            )
            return run_status, run_detail, run_status == "ok"
    except (AgentRuntimeError, ExecutableSkillValidationError, OSError, PermissionError, ValueError) as exc:
        return "failed", str(exc), False
    return "failed", f"unsupported {action}", False


def _run_vault_audit(kind: str, brain_dir: str) -> object:
    root = os.path.abspath(brain_dir)
    atoms = os.path.join(root, "2-atoms")
    if kind == "sources":
        from audit_sources import audit_sources

        return audit_sources(atoms_dir=atoms, brain_dir=root, demote_stale=False)
    if kind == "ledger":
        from vault_ledger import verify_vault_ledger

        return verify_vault_ledger(brain_dir=root, atoms_dir=atoms, playbooks_dir=os.path.join(root, "5-playbooks"))
    if kind == "contradictions":
        from knowledge_graph import detect_contradictions

        report = detect_contradictions(
            brain_dir=root,
            atoms_dir=atoms,
            include_pending=False,
            enable_nli=False,
        )
        return {
            "contradiction_count": report.get("contradiction_count", 0),
            "high_severity_count": report.get("high_severity_count", 0),
            "drift_count": report.get("drift_count", 0),
        }
    raise AgentRuntimeError(f"Unsupported audit_kind: {kind}")


def _recall(query: str, brain_dir: str, security_dir: str | None = None) -> list[str]:
    atoms = os.path.join(brain_dir, "2-atoms")
    index = os.path.join(brain_dir, ".sidecar", "lexical.sqlite3")
    names: list[str] = []
    try:
        direct, _neighbors = search_memory(
            query,
            RecallOptions(n_results=5, retrieval_mode="lexical", include_statuses={"accepted"}),
            atoms_dir=atoms,
            index_path=index,
            chroma_path=os.path.join(brain_dir, ".chroma"),
        )
        names = [item.filename for item in direct]
    except Exception:
        names = []
    try:
        for hit in search_connected_knowledge(
            query,
            brain_dir=brain_dir,
            limit=5,
            learn=False,
            security_dir=security_dir,
        ):
            if hit.get("source") == "agent_skill":
                continue
            label = hit["filename"]
            if label not in names:
                names.append(label)
    except Exception:
        return names
    return names


def execute_agent_skill(
    skill_id: str,
    inputs: dict[str, Any] | None = None,
    *,
    agent_id: str,
    run_id: str,
    brain_dir: str = BRAIN_DIR,
    security_dir: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    dirs = _dirs(brain_dir)
    author = _agent_id(agent_id)
    norm_id = _normalize_skill_id(skill_id)
    path = safe_file_in_dir(dirs["skills"], f"{norm_id}.md", allowed_exts={".md"}, must_exist=True)
    if not path:
        raise AgentRuntimeError(f"Unknown agent skill: {skill_id}")
    loaded = _load_skill(path)
    fm = loaded["frontmatter"]
    if str(fm.get("status") or "") == "agent_suspended":
        raise AgentRuntimeError(f"Skill {norm_id} is suspended.")
    successes = int(fm.get("successes") or 0)
    failures = int(fm.get("failures") or 0)
    killed, reason = _is_skill_killed(get_skill_kill_switch_status(security_dir=security_dir), norm_id)
    if killed:
        record = {"skill_id": norm_id, "run_id": run_id, "status": "stopped", "detail": reason, "at": _now()}
        _append_execution(dirs, record)
        _mark_run(dirs, run_id, "stopped", detail=reason)
        return record
    status = "ok"
    detail = ""
    productive = False
    try:
        _validate_steps(loaded["steps"])
    except (AgentRuntimeError, ExecutableSkillValidationError) as exc:
        status, detail = "failed", str(exc)
    with tempfile.TemporaryDirectory(prefix="sb-agent-workspace-") as workspace:
        if status == "ok":
            for step in loaded["steps"]:
                status, detail, step_productive = _run_agent_step(
                    step,
                    inputs=inputs,
                    skill_id=norm_id,
                    brain_dir=os.path.abspath(brain_dir),
                    security_dir=security_dir,
                    timeout=timeout,
                    workspace=workspace,
                )
                productive = productive or step_productive
                if status != "ok":
                    break
    if status == "ok" and productive:
        successes += 1
        skill_status = "agent_active" if successes >= 3 and failures == 0 else "agent_trial"
    elif status in {"ok", "stopped"}:
        skill_status = str(fm.get("status") or "agent_trial")
    else:
        failures += 1
        skill_status = "agent_suspended"
        status = "failed"
    body = _render_skill(
        norm_id,
        agent_id=author,
        trigger=str(fm.get("trigger") or ""),
        steps=loaded["steps"],
        status=skill_status,
        successes=successes,
        failures=failures,
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    record = {
        "execution_id": f"exec-{hashlib.sha256((_now() + norm_id).encode()).hexdigest()[:12]}",
        "skill_id": norm_id,
        "run_id": run_id,
        "status": "stopped" if status == "stopped" else status,
        "detail": detail,
        "skill_status": skill_status,
        "at": _now(),
        "agent_id": f"agent_runtime:{author}",
    }
    _append_execution(dirs, record)
    if status == "stopped":
        _mark_run(dirs, run_id, "stopped", detail=detail)
    return record


def _mark_run(dirs: dict[str, str], run_id: str, status: str, *, detail: str = "", depth: int | None = None, goal: str = "") -> str:
    safe_id = _slug(run_id, 48)
    name = f"{safe_id}.md"
    path = safe_file_in_dir(dirs["runs"], name, allowed_exts={".md"}, must_exist=False)
    if not path:
        raise AgentRuntimeError(f"Unsafe run id: {run_id}")
    depth_line = f"depth: {depth}\n" if depth is not None else ""
    body = (
        "---\n"
        f"run_id: {safe_id}\n"
        f"status: {status}\n"
        f"{depth_line}"
        "---\n\n"
        f"{goal}\n\n{detail}\n"
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    return name


def list_agent_skills(*, brain_dir: str = BRAIN_DIR, project: str = "") -> list[dict[str, str]]:
    _ = project
    skills_dir = os.path.join(agent_root(brain_dir), "skills")
    if not os.path.isdir(skills_dir):
        return []
    found: list[dict[str, str]] = []
    for name in sorted(os.listdir(skills_dir)):
        path = safe_file_in_dir(skills_dir, name, allowed_exts={".md"}, must_exist=True)
        if not path:
            continue
        fm = _frontmatter(_read_text(path))
        found.append(
            {
                "skill_id": str(fm.get("skill_id") or os.path.splitext(name)[0]),
                "status": str(fm.get("status") or ""),
                "trigger": str(fm.get("trigger") or ""),
            }
        )
    return found


def _same_vault(left: str, right: str) -> bool:
    try:
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))
    except OSError:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def owner_vault_roots(brain_dir: str, security_dir: str | None = None) -> list[tuple[str, str]]:
    """Every vault this owner registered, plus the vault the agent is already in.

    A project tag stays inside the active vault. Other vaults are still searched:
    the agent is not asked to name them. Vaults the owner did not register are skipped.
    """
    explicit = security_dir is not None
    sec = security_dir
    if sec is None:
        try:
            from config import security_state_dir

            candidate = security_state_dir()
            if (Path(candidate) / "vaults.json").is_file():
                sec = str(candidate)
        except Exception:
            sec = None
    roots: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(label: str, path: str) -> None:
        if not path:
            return
        try:
            if _is_reparse_or_link(path) or not os.path.isdir(path):
                return
            key = os.path.normcase(os.path.realpath(path))
        except OSError:
            return
        if key in seen:
            return
        seen.add(key)
        roots.append(((label or "vault").strip() or "vault", os.path.abspath(path)))

    add("active", brain_dir)
    if not sec:
        return roots
    try:
        from vault_workspaces import list_vaults

        listing = list_vaults(security_dir=sec, current_brain_dir=brain_dir)
    except Exception:
        return roots
    vaults = [item for item in (listing.get("vaults") or []) if isinstance(item, dict)]
    listed = any(_same_vault(str(item.get("path") or ""), brain_dir) for item in vaults)
    if not explicit and not listed:
        return roots
    for item in vaults:
        add(str(item.get("label") or item.get("vault_id") or "vault"), str(item.get("path") or ""))
    return roots


def _matching_skill(goal: str, brain_dir: str, security_dir: str | None = None) -> tuple[str, str] | None:
    terms = {token.casefold() for token in _TOKEN_RE.findall(goal)}
    if not terms:
        return None
    for _label, root in owner_vault_roots(brain_dir, security_dir):
        for skill in list_agent_skills(brain_dir=root):
            if skill["status"] not in {"agent_trial", "agent_active"}:
                continue
            trigger_terms = {token.casefold() for token in _TOKEN_RE.findall(skill["trigger"])}
            if terms.intersection(trigger_terms):
                return skill["skill_id"], root
    return None


def run_agent(
    goal: str,
    *,
    parent_run_id: str | None = None,
    project: str = "",
    max_steps: int = DEFAULT_MAX_STEPS,
    depth: int = 1,
    agent_id: str = "planner",
    brain_dir: str = BRAIN_DIR,
    security_dir: str | None = None,
) -> dict[str, Any]:
    """Run one goal. A goal containing `` and `` spawns one child for the second clause."""
    if depth < 1 or depth > MAX_AGENT_DEPTH:
        raise AgentRuntimeError(f"Agent depth must be 1..{MAX_AGENT_DEPTH}.")
    steps = max(1, min(MAX_AGENT_STEPS, int(max_steps)))
    clean_goal = " ".join(str(goal or "").split())
    if not clean_goal:
        raise AgentRuntimeError("Agent run requires a goal.")
    dirs = _dirs(brain_dir)
    author = _agent_id(agent_id)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S%f")
    run_id = parent_run_id or f"run-{stamp}"
    if parent_run_id:
        run_id = f"{_slug(parent_run_id, 24)}-d{depth}-{stamp[-6:]}"
    own_goal, child_goal = clean_goal, ""
    if " and " in clean_goal and depth < MAX_AGENT_DEPTH:
        own_goal, child_goal = clean_goal.split(" and ", 1)
        own_goal, child_goal = own_goal.strip(), child_goal.strip()
    _mark_run(dirs, run_id, "running", depth=depth, goal=own_goal)
    matched = _matching_skill(own_goal, brain_dir, security_dir)
    if matched is None:
        skill_id = _normalize_skill_id(f"recall-{_slug(own_goal, 24)}")
        skill_brain = brain_dir
        draft_agent_skill(
            author,
            skill_id,
            [{"action_type": "vault_recall", "query": own_goal, "output_file": "recall.json"}],
            trigger=own_goal,
            brain_dir=brain_dir,
        )
    else:
        skill_id, skill_brain = matched
    executed = execute_agent_skill(
        skill_id,
        {"query": own_goal},
        agent_id=author,
        run_id=run_id,
        brain_dir=skill_brain,
        security_dir=security_dir,
    )
    if executed["status"] == "stopped":
        return {"run_id": run_id, "status": "stopped", "depth": depth, "skill_id": skill_id, "memory_ids": [], "child_run_id": None}
    experience = save_experience_record(
        goal=own_goal,
        result_summary=f"Agent {author} ran {skill_id}: {executed['status']}. {executed.get('detail', '')}",
        success=executed["status"] == "ok",
        actions=[f"execute {skill_id}"],
        tools_used=[skill_id],
        brain_dir=os.path.abspath(brain_dir),
        experiences_dir=dirs["experiences"],
        inbox_dir=dirs["inbox"],
    )
    memory_name = write_agent_memory(
        author,
        "episodic",
        f"Worked on {own_goal}. Skill {skill_id} finished {executed['status']}.",
        str(experience["experience_id"]),
        project=project,
        brain_dir=brain_dir,
    )
    child_id = None
    if child_goal and steps >= 2:
        child = run_agent(
            child_goal,
            parent_run_id=run_id,
            project=project,
            max_steps=steps - 1,
            depth=depth + 1,
            agent_id=author,
            brain_dir=brain_dir,
            security_dir=security_dir,
        )
        child_id = str(child["run_id"])
    _mark_run(dirs, run_id, "done", depth=depth, goal=own_goal, detail=f"memory={memory_name} child={child_id or ''}")
    return {
        "run_id": run_id,
        "status": "done",
        "depth": depth,
        "skill_id": skill_id,
        "memory_ids": [memory_name],
        "child_run_id": child_id,
        "experience_id": experience["experience_id"],
    }


def agent_status(brain_dir: str = BRAIN_DIR) -> dict[str, Any]:
    root = agent_root(brain_dir)
    schedule_path = os.path.join(root, "schedule.json")
    enabled = False
    if os.path.isfile(schedule_path):
        try:
            enabled = bool(json.loads(_read_text(schedule_path)).get("enabled"))
        except (OSError, json.JSONDecodeError):
            enabled = False
    runs = 0
    runs_dir = os.path.join(root, "runs")
    if os.path.isdir(runs_dir):
        runs = len([name for name in os.listdir(runs_dir) if name.endswith(".md")])
    memories = 0
    memory_dir = os.path.join(root, "memory")
    if os.path.isdir(memory_dir):
        memories = len([name for name in os.listdir(memory_dir) if name.endswith(".md")])
    return {
        "enabled": enabled,
        "runs": runs,
        "memories": memories,
        "skills": list_agent_skills(brain_dir=brain_dir),
        "summary": f"{'on' if enabled else 'off'}, {memories} memories, {runs} runs",
    }


def set_agent_schedule(
    enabled: bool,
    *,
    actor: str,
    brain_dir: str = BRAIN_DIR,
    interval_seconds: int = DEFAULT_SCHEDULE_SECONDS,
) -> dict[str, Any]:
    try:
        clean_actor = _validate_human_actor(actor)
    except ExecutableSkillValidationError as exc:
        raise AgentRuntimeError(str(exc)) from exc
    dirs = _dirs(brain_dir)
    payload = {
        "enabled": bool(enabled),
        "enabled_by": clean_actor if enabled else "",
        "interval_seconds": max(60, int(interval_seconds)),
        "updated_at": _now(),
    }
    with open(dirs["schedule"], "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
    return payload


def learn_from_notes(*, brain_dir: str = BRAIN_DIR) -> dict[str, int]:
    """Turn new and edited notes into memories the next agent can use.

    Existing notes are not edited or deleted. A later edit adds another memory.
    Risky text is left in the note and is not copied into agent memory.
    """
    from notes_store import parse_note

    dirs = _dirs(brain_dir)
    notes_dir = os.path.join(os.path.abspath(brain_dir), "notes")
    ledger_path = os.path.join(dirs["root"], "learned-notes.json")
    ledger: dict[str, str] = {}
    if os.path.isfile(ledger_path):
        try:
            loaded = json.loads(_read_text(ledger_path))
            if isinstance(loaded, dict):
                ledger = {str(key): str(value) for key, value in loaded.items()}
        except (OSError, json.JSONDecodeError):
            ledger = {}
    learned = skills = skipped = 0
    if not os.path.isdir(notes_dir):
        return {"learned": 0, "skills": 0, "skipped": 0}
    changed = False
    for name in sorted(os.listdir(notes_dir)):
        if learned >= _LEARN_BATCH:
            break
        if not name.endswith(".md") or name.startswith("."):
            continue
        path = safe_file_in_dir(notes_dir, name, allowed_exts={".md"}, must_exist=True)
        if not path:
            continue
        try:
            text = _read_text(path)
        except OSError:
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if ledger.get(name) == digest:
            continue
        meta, body = parse_note(text, name)
        title = str(meta.get("title") or name).strip() or name
        scope = _note_scope(meta.get("tags"))
        readable = f"{title}\n{body}".strip()
        if len(readable) < 12 or scan_inbox_payload_risks(readable):
            ledger[name] = digest
            skipped += 1
            changed = True
            continue
        claim = f"From note {name} ({title}): {body.strip() or title}"[:_LEARN_CLAIM_CHARS].strip()
        episode_id = f"note-{digest[:16]}"
        _append_execution(dirs, {"episode_id": episode_id, "kind": "note_learned", "note": name})
        try:
            write_agent_memory(
                "learner",
                "semantic",
                claim,
                episode_id,
                project=scope,
                filename=f"note-{_slug(title)}-{digest[:8]}.md",
                brain_dir=brain_dir,
            )
        except AgentRuntimeError as exc:
            if "already exists" not in str(exc):
                ledger[name] = digest
                skipped += 1
                changed = True
                continue
        learned += 1
        changed = True
        procedural = len(_STEP_LINE_RE.findall(body)) >= 2 or "how to" in readable.casefold()
        if procedural and scope != "mixed":
            try:
                draft_agent_skill(
                    "learner",
                    f"note-{_slug(title, 24)}-{digest[:6]}",
                    [{"action_type": "vault_recall", "query": title[:200], "output_file": "recall.json"}],
                    trigger=title[:200],
                    project=scope,
                    brain_dir=brain_dir,
                )
                skills += 1
            except (AgentRuntimeError, ExecutableSkillValidationError):
                pass
        ledger[name] = digest
    if changed:
        _write_ledger(ledger_path, ledger)
    return {"learned": learned, "skills": skills, "skipped": skipped}


def search_connected_knowledge(
    query: str,
    *,
    brain_dir: str = BRAIN_DIR,
    limit: int = 8,
    project: str = "",
    learn: bool = True,
    security_dir: str | None = None,
) -> list[dict[str, str]]:
    """Search notes, memories, and skills across every vault this owner connected.

    A project filter applies only inside the active vault, so a note in another
    vault is found without anyone naming that vault. Approved-atom federation
    stays opt-in and is not used here.
    """
    if learn:
        try:
            learn_from_notes(brain_dir=brain_dir)
        except Exception:
            pass
    cap = max(1, min(int(limit), 20))
    hits: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    terms = {token.casefold() for token in _TOKEN_RE.findall(query or "")}

    def add(source: str, filename: str, title: str, text: str, vault: str, root: str) -> None:
        key = (os.path.normcase(root), source, filename)
        if key in seen:
            return
        seen.add(key)
        hits.append(
            {
                "source": source,
                "filename": filename,
                "title": title,
                "text": text[:_LEARN_CLAIM_CHARS],
                "vault": vault,
            }
        )

    try:
        from notes_store import NotePaths, search_notes
    except Exception:
        search_notes = None  # type: ignore[assignment]
        NotePaths = None  # type: ignore[assignment]

    for label, root in owner_vault_roots(brain_dir, security_dir):
        vault_project = project if _same_vault(root, brain_dir) else ""
        if search_notes is not None and NotePaths is not None:
            try:
                for note in search_notes(
                    query,
                    limit=cap,
                    project=vault_project or None,
                    paths=NotePaths.for_brain(root),
                ):
                    add("note", note.filename, note.title, note.snippet or note.title, label, root)
            except Exception:
                pass
        for memory in search_agent_memory(query, brain_dir=root, project=vault_project, limit=cap):
            add("agent_memory", memory["filename"], memory["filename"], memory["claim"], label, root)
        skills_dir = os.path.join(agent_root(root), "skills")
        if not terms or not os.path.isdir(skills_dir):
            continue
        scanned = 0
        for name in sorted(os.listdir(skills_dir)):
            if scanned >= 200:
                break
            scanned += 1
            path = safe_file_in_dir(skills_dir, name, allowed_exts={".md"}, must_exist=True)
            if not path:
                continue
            try:
                frontmatter = _frontmatter(_read_text(path))
            except OSError:
                continue
            if str(frontmatter.get("status") or "") not in {"agent_trial", "agent_active"}:
                continue
            if not _scope_visible(str(frontmatter.get("project") or ""), vault_project):
                continue
            trigger = str(frontmatter.get("trigger") or "")
            if not terms.intersection({token.casefold() for token in _TOKEN_RE.findall(trigger)}):
                continue
            add(
                "agent_skill",
                name,
                str(frontmatter.get("skill_id") or name),
                f"Use this skill for the request.\n{trigger}",
                label,
                root,
            )
    skills = [hit for hit in hits if hit["source"] == "agent_skill"][:3]
    rest = [hit for hit in hits if hit["source"] != "agent_skill"]
    return (skills + rest)[:cap]


def run_scheduled_goals(*, brain_dir: str = BRAIN_DIR, force: bool = False) -> list[dict[str, Any]]:
    """Run the oldest open goal when the agent schedule is enabled."""
    schedule_path = os.path.join(agent_root(brain_dir), "schedule.json")
    if not os.path.isfile(schedule_path):
        return []
    dirs = _dirs(brain_dir)
    try:
        schedule = json.loads(_read_text(dirs["schedule"]))
    except (OSError, json.JSONDecodeError):
        return []
    if not schedule.get("enabled"):
        return []
    try:
        _validate_human_actor(str(schedule.get("enabled_by") or ""))
    except ExecutableSkillValidationError:
        return []
    try:
        learn_from_notes(brain_dir=brain_dir)
    except Exception:
        pass
    stamp_path = os.path.join(dirs["root"], "last-tick.txt")
    now = time.time()
    if not force and os.path.isfile(stamp_path):
        try:
            last = float(_read_text(stamp_path).strip() or "0")
        except ValueError:
            last = 0.0
        if now - last < float(schedule.get("interval_seconds") or DEFAULT_SCHEDULE_SECONDS):
            return []
    goal_text = ""
    goal_name = ""
    for name in sorted(os.listdir(dirs["goals"])):
        path = safe_file_in_dir(dirs["goals"], name, allowed_exts={".md"}, must_exist=True)
        if not path:
            continue
        text = _read_text(path)
        fm = _frontmatter(text)
        if str(fm.get("status") or "open") != "open":
            continue
        parsed = split_frontmatter(text)
        goal_text = ("\n".join(parsed[1]) if parsed else text).strip()
        goal_name = name
        break
    with open(stamp_path, "w", encoding="utf-8") as handle:
        handle.write(str(now))
    if not goal_text:
        return []
    result = run_agent(goal_text, brain_dir=brain_dir)
    goal_path = os.path.join(dirs["goals"], goal_name)
    done = _read_text(goal_path).replace("status: open", "status: done", 1)
    with open(goal_path, "w", encoding="utf-8") as handle:
        handle.write(done)
    return [result]


def stop_agent_runs(*, actor: str, reason: str, brain_dir: str = BRAIN_DIR, security_dir: str | None = None) -> dict[str, Any]:
    set_agent_schedule(False, actor=actor, brain_dir=brain_dir)
    return set_skill_kill_switch(
        engaged=True,
        scope="global",
        actor=actor,
        reason=reason,
        brain_dir=brain_dir,
        security_dir=security_dir,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="second-brain agent", description="Run the autonomous agent runtime.")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="Run one goal now.")
    run_p.add_argument("--goal", required=True)
    run_p.add_argument("--project", default="")
    run_p.add_argument("--json", action="store_true")
    start_p = sub.add_parser("start", help="Enable the scheduled goal tick.")
    start_p.add_argument("--actor", required=True, help="Human owner who is arming the schedule.")
    stop_p = sub.add_parser("stop", help="Disable the tick and engage the skill kill switch.")
    stop_p.add_argument("--actor", required=True)
    stop_p.add_argument("--reason", required=True)
    status_p = sub.add_parser("status", help="Show agent memory, skill, and schedule counts.")
    status_p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "run":
        result = run_agent(args.goal, project=args.project)
        print(json.dumps(result, sort_keys=True) if args.json else f"run {result['run_id']} {result['status']}")
        return 0
    if args.command == "start":
        payload = set_agent_schedule(True, actor=args.actor)
        print(json.dumps(payload, sort_keys=True))
        return 0
    if args.command == "stop":
        payload = stop_agent_runs(actor=args.actor, reason=args.reason)
        print(json.dumps({"stopped": True, "kill_switch": payload.get("global_engaged", True)}))
        return 0
    print(json.dumps(agent_status(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
