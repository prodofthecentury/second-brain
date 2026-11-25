# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#     "httpx>=0.28,<1",
# ]
# ///
"""Cross-App "Jarvis" Orchestrator (Open WebUI + n8n + OpenHands + AppFlowy + Critique Theater).

Connects local-first Second Brain memory cleanly to loopback workflow engines:
- **Open WebUI:** Generates import-ready Open WebUI Tool JSON + loopback Proxy valves.
- **n8n:** Dispatches secret-scrubbed JSON webhooks strictly to loopback (`127.0.0.1`/`[::1]`).
- **OpenHands:** Dispatches grounded coding tasks strictly to loopback and logs reflections in `4-experiences/`.
- **AppFlowy / Task Boards:** Synchronizes `notes_store.extract_action_items()` into working-note task boards.
- **5-Panel Critique Theater:** Delegates to `design_critique.run_five_panel_critique()`.
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
import datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import urlsplit

import httpx

if __name__ in ("jarvis_orchestrator", "scripts.jarvis_orchestrator"):
    sys.modules.setdefault("jarvis_orchestrator", sys.modules[__name__])
    sys.modules.setdefault("scripts.jarvis_orchestrator", sys.modules[__name__])

from scripts.cognitive_core import save_experience_record  # noqa: E402
from scripts.config import ATOMS_DIR, BRAIN_DIR, _is_reparse_or_link, resolve_module_vault_path  # noqa: E402
from scripts.design_critique import get_design_presets, run_five_panel_critique  # noqa: E402
from scripts.host_adapters import inspect_and_redact_outbound  # noqa: E402
from scripts.notes_store import NotePaths, create_note, extract_action_items  # noqa: E402

DEFAULT_N8N_URL = "http://127.0.0.1:5678"
DEFAULT_OPENHANDS_URL = "http://127.0.0.1:3002"
DEFAULT_OPENWEBUI_URL = "http://127.0.0.1:3000"
DEFAULT_API_PORT = 8765
DEFAULT_PROXY_PORT = DEFAULT_API_PORT


class OrchestratorSecurityError(PermissionError):
    """Raised when an orchestrator target is non-loopback or leaks raw secrets."""


def validate_loopback_service_url(raw_url: str, *, service_name: str = "service") -> str:
    clean = str(raw_url or "").strip().rstrip("/")
    parsed = urlsplit(clean)
    hostname = (parsed.hostname or "").strip().lower()
    if parsed.scheme != "http" or hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password:
        raise OrchestratorSecurityError(
            f"{service_name} orchestrator target must be strictly local loopback HTTP (127.0.0.1/localhost/[::1]); got {raw_url!r}."
        )
    if hostname == "localhost":
        clean = clean.replace("://localhost:", "://127.0.0.1:", 1).replace("://localhost/", "://127.0.0.1/", 1)
        if clean.endswith("://localhost"):
            clean = clean[: -len("localhost")] + "127.0.0.1"
    return clean


def export_open_webui_tool(
    *,
    api_port: int = DEFAULT_API_PORT,
    proxy_port: int | None = None,
) -> dict[str, Any]:
    """Generate a ready-to-import Open WebUI Tool module + setup instructions for Second Brain."""
    eff_api_port = int(api_port)
    eff_proxy_port = eff_api_port if proxy_port is None else int(proxy_port)
    if not (1024 <= eff_api_port <= 65535) or not (1024 <= eff_proxy_port <= 65535):
        raise ValueError("Port values must be in 1024..65535.")
    openai_proxy_base = f"http://127.0.0.1:{eff_proxy_port}/v1/openai/v1"
    tool_py = f'''"""
title: Second Brain Local Memory Bridge
author: Second Brain
version: 1.3.0
description: Search approved Second Brain atoms & run grounded recall over loopback 127.0.0.1.
"""
import httpx
from pydantic import BaseModel, Field

class Tools:
    class Valves(BaseModel):
        SECOND_BRAIN_API_URL: str = Field(default="http://127.0.0.1:{eff_api_port}")
        SECOND_BRAIN_PROXY_URL: str = Field(default="{openai_proxy_base}")
        BEARER_TOKEN: str = Field(default="")

    def __init__(self):
        self.valves = self.Valves()

    def search_second_brain(self, query: str, project: str = "") -> str:
        """Query owner-approved Second Brain knowledge atoms via loopback REST."""
        headers = {{"Authorization": f"Bearer {{self.valves.BEARER_TOKEN}}"}} if self.valves.BEARER_TOKEN else {{}}
        payload = {{"query": query, "n_results": 6}}
        if project:
            payload["projects"] = [project]
        with httpx.Client(trust_env=False, timeout=15.0) as client:
            r = client.post(f"{{self.valves.SECOND_BRAIN_API_URL}}/v1/recall", headers=headers, json=payload)
            r.raise_for_status()
            return r.text
'''
    return {
        "status": "ok",
        "tool_name": "second_brain_memory_bridge",
        "openai_proxy_base_url": openai_proxy_base,
        "memory_api_base_url": f"http://127.0.0.1:{eff_api_port}",
        "python_tool_source": tool_py,
    }


def trigger_n8n_workflow(
    webhook_url: str,
    payload: dict[str, Any],
    *,
    http_transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Dispatch a secret-scanned webhook trigger strictly to a loopback n8n instance."""
    clean_url = validate_loopback_service_url(webhook_url, service_name="n8n")
    raw_json = json.dumps(payload or {}, ensure_ascii=False)
    red = inspect_and_redact_outbound(raw_json, redact_pii=False, fail_on_secret_detection=True)
    sanitized_body = json.loads(red.sanitized_text)

    client_kw: dict[str, Any] = {"trust_env": False, "follow_redirects": False, "timeout": 15.0}
    if http_transport is not None:
        client_kw["transport"] = http_transport
    with httpx.Client(**client_kw) as client:
        resp = client.post(clean_url, json=sanitized_body)
    return {
        "status": "dispatched" if resp.status_code < 400 else "upstream_error",
        "webhook_url": clean_url,
        "http_status": resp.status_code,
        "response_preview": resp.text[:1000],
    }


def dispatch_openhands_task(
    goal: str,
    *,
    repo_path: str = "",
    openhands_url: str = DEFAULT_OPENHANDS_URL,
    project_scope: str | None = None,
    brain_dir: str | Path | None = None,
    http_transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Dispatch an autonomous coding task to loopback OpenHands & record an episode in 4-experiences/."""
    eff_brain = resolve_module_vault_path(globals(), "BRAIN_DIR", str(brain_dir) if brain_dir is not None else None)
    clean_url = validate_loopback_service_url(openhands_url, service_name="OpenHands")
    clean_goal = str(goal or "").strip()
    if not clean_goal:
        raise ValueError("OpenHands task goal is required.")
    red_goal = inspect_and_redact_outbound(clean_goal, redact_pii=True, fail_on_secret_detection=True)

    task_body: dict[str, Any] = {
        "initial_user_msg": red_goal.sanitized_text,
        "repository": str(repo_path or "").strip(),
    }
    client_kw: dict[str, Any] = {"trust_env": False, "follow_redirects": False, "timeout": 20.0}
    if http_transport is not None:
        client_kw["transport"] = http_transport
    with httpx.Client(**client_kw) as client:
        resp = client.post(f"{clean_url}/api/conversations", json=task_body)

    success_flag = resp.status_code < 400
    exp = save_experience_record(
        goal=f"OpenHands Dispatch: {red_goal.sanitized_text[:100]}",
        result_summary=f"HTTP {resp.status_code}: {resp.text[:300]}",
        success=success_flag,
        context=[project_scope] if project_scope else ["coding"],
        actions=["validate_loopback_openhands", "redact_outbound_goal", "dispatch_api_conversations"],
        tools_used=["jarvis_orchestrator:openhands"],
        reasoning_summary="Dispatched scoped coding task to local OpenHands loopback service.",
        lessons=["Keep autonomous coding tasks scoped and verified before merging PRs."],
        brain_dir=eff_brain,
    )
    return {
        "status": "dispatched" if success_flag else "upstream_error",
        "openhands_url": clean_url,
        "http_status": resp.status_code,
        "experience_file": exp.get("filename"),
        "response_preview": resp.text[:1000],
    }


def sync_appflowy_board_tasks(
    *,
    external_board_tasks: list[dict[str, Any]] | None = None,
    project: str | None = None,
    brain_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Pull local Second Brain note tasks & stage any imported AppFlowy board tasks into a Working Note."""
    eff_brain = resolve_module_vault_path(globals(), "BRAIN_DIR", str(brain_dir) if brain_dir is not None else None)
    existing_items = extract_action_items(
        paths=NotePaths.for_brain(eff_brain),
        include_completed=True,
        include_raw=False,
        project=project,
    )
    existing_texts = {str(it.get("text") or "").strip().casefold() for it in existing_items if isinstance(it, dict)}

    imported_lines: list[str] = []
    for bt in (external_board_tasks or []):
        if not isinstance(bt, dict):
            continue
        title_txt = str(bt.get("title") or bt.get("text") or "").strip()
        if not title_txt:
            continue
        red = inspect_and_redact_outbound(title_txt, redact_pii=False, fail_on_secret_detection=True)
        if red.sanitized_text.casefold() in existing_texts:
            continue
        done_mark = "x" if bool(bt.get("done") or bt.get("completed")) else " "
        imported_lines.append(f"- [{done_mark}] {red.sanitized_text}")

    created_note = None
    if imported_lines:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        tags = ["appflowy-board", "tasks"]
        if project:
            tags.append(project if project.startswith("project:") else f"project:{project}")
        created_note = create_note(
            f"AppFlowy Board Sync ({stamp})",
            "## Synced Board Action Items\n\n" + "\n".join(imported_lines) + "\n",
            origin="agent_capture",
            capture_source="appflowy_board_sync",
            tags=tags,
            paths=NotePaths.for_brain(eff_brain),
        )

    return {
        "status": "synced",
        "existing_task_count": len(existing_items),
        "existing_tasks": existing_items[:100],
        "imported_count": len(imported_lines),
        "created_note": created_note.get("filename") if created_note else None,
    }


def orchestrator_status(*, http_client: Any = None) -> dict[str, Any]:
    probes = {
        "open_webui": DEFAULT_OPENWEBUI_URL,
        "n8n": DEFAULT_N8N_URL,
        "openhands": DEFAULT_OPENHANDS_URL,
    }
    service_status: dict[str, Any] = {}
    for name, base_url in probes.items():
        reachable = False
        try:
            if http_client is not None:
                r = http_client.get(base_url, timeout=1.0)
                reachable = r.status_code < 500
            else:
                with httpx.Client(trust_env=False, timeout=0.6) as client:
                    r = client.get(base_url)
                    reachable = r.status_code < 500
        except Exception:
            reachable = False
        service_status[name] = {"endpoint": base_url, "reachable": reachable}

    return {
        "status": "ready",
        "services": service_status,
        "design_presets_count": len(get_design_presets()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("open-webui-tool")
    p_n8n = sub.add_parser("n8n-trigger")
    p_n8n.add_argument("--url", required=True)
    p_n8n.add_argument("--payload-json", default="{}")
    p_oh = sub.add_parser("openhands-dispatch")
    p_oh.add_argument("--goal", required=True)
    p_oh.add_argument("--url", default=DEFAULT_OPENHANDS_URL)
    sub.add_parser("appflowy-sync")

    args = parser.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps(orchestrator_status(), indent=2))
    elif args.cmd == "open-webui-tool":
        print(json.dumps(export_open_webui_tool(), indent=2))
    elif args.cmd == "n8n-trigger":
        print(json.dumps(trigger_n8n_workflow(args.url, json.loads(args.payload_json)), indent=2))
    elif args.cmd == "openhands-dispatch":
        print(json.dumps(dispatch_openhands_task(args.goal, openhands_url=args.url), indent=2))
    else:
        print(json.dumps(sync_appflowy_board_tasks(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
