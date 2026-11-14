# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = [
#     "mcp>=2.3,<3",
#     "ollama>=0.6,<1",
# ]
# ///
"""
Feature 4 — Native MCP Server
Exposes a deliberately limited MCP (Model Context Protocol) interface to a
trusted local stdio process. It is disabled unless BRAIN_ENABLE_LOCAL_MCP=1 is set.

Tools exposed:
  search_memory(query)     — Shared recall over currently approved atoms
  get_atom(atom_id)        — Read a specifically named, currently approved atom
  add_to_inbox(content)    — Add a size-limited, untrusted capture to 0-raw/

Resources: no listable resource catalog; direct approved atom resources only.
Tool/resource use is audited by canonical name and timestamp; input text is not logged.

Run only from an explicitly trusted local process:
    $env:BRAIN_ENABLE_LOCAL_MCP = "1"
    uv run scripts/mcp_server.py
"""

import os
import sys
import json
import glob
import datetime
import hmac
import secrets
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (
    ATOMS_DIR, THREADS_DIR, BRIEFINGS_DIR, RAW_DIR, INBOX_DIR,
    ensure_dirs, today, now_slug,
    is_content_markdown, normalize_markdown_filename, safe_file_in_dir
)
from recall import MemoryRecallUnavailable, RecallOptions, search_memory
from review_atom import approved_revision_is_current
from memory_review import parse_atom_record


MAX_INBOX_CHARS = 100_000
MCP_AUDIT_PATH = os.path.join(RAW_DIR, "archive", "mcp-audit.jsonl")
TOOL_NAMES = {"search_memory", "get_atom", "add_to_inbox"}


# ─── Security & Authorization ─────────────────────────────────────────────────

def default_mcp_token_path() -> Path:
    override = os.environ.get("BRAIN_MCP_TOKEN_FILE")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    elif os.environ.get("XDG_STATE_HOME"):
        base = Path(os.environ["XDG_STATE_HOME"]).expanduser()
    else:
        base = Path.home() / ".local" / "state"
    return base / "SecondBrain" / "security" / "mcp-token"


def get_configured_bearer_token() -> str | None:
    env_token = os.environ.get("BRAIN_MCP_BEARER_TOKEN")
    if env_token and env_token.strip():
        return env_token.strip()
    token_path = default_mcp_token_path()
    try:
        if token_path.is_file():
            content = token_path.read_text(encoding="utf-8").strip()
            if content:
                return content
    except OSError:
        pass
    return None


def generate_bearer_token() -> str:
    return secrets.token_hex(32)


def save_bearer_token(token: str, token_path: Path | None = None) -> Path:
    if not token or len(token) < 16:
        raise ValueError("MCP bearer token must contain at least 16 characters.")
    path = Path(token_path or default_mcp_token_path()).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(token.strip() + "\n", encoding="utf-8")
    try:
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def extract_bearer_token(params=None, ctx=None) -> str | None:
    if params is not None:
        if hasattr(params, "arguments") and isinstance(params.arguments, dict):
            for key in ("bearer_token", "auth_token", "token"):
                val = params.arguments.get(key)
                if val and isinstance(val, str):
                    return val.strip()
        if hasattr(params, "meta") and isinstance(params.meta, dict):
            for key in ("authorization", "auth_token", "token"):
                val = params.meta.get(key)
                if val and isinstance(val, str):
                    if val.lower().startswith("bearer "):
                        return val[7:].strip()
                    return val.strip()
    if ctx is not None and hasattr(ctx, "meta") and isinstance(ctx.meta, dict):
        val = ctx.meta.get("authorization")
        if val and isinstance(val, str):
            if val.lower().startswith("bearer "):
                return val[7:].strip()
            return val.strip()
    return None


def is_authorized(params=None, ctx=None) -> bool:
    configured = get_configured_bearer_token()
    require_auth = os.environ.get("BRAIN_MCP_REQUIRE_AUTH") == "1"
    if not configured:
        return not require_auth
    provided = extract_bearer_token(params, ctx)
    if not provided:
        return False
    return hmac.compare_digest(provided, configured)


def get_allowed_tools() -> set[str]:
    raw = os.environ.get("BRAIN_MCP_ALLOWED_TOOLS")
    if raw is None or raw.strip() == "*":
        return set(TOOL_NAMES)
    allowed = {t.strip() for t in raw.split(",") if t.strip()}
    return allowed.intersection(TOOL_NAMES)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def assert_mcp_loopback_transport(host: str | None = None) -> str:
    """Reject any non-loopback network listener configuration for MCP."""
    candidate = str(
        host
        if host is not None
        else os.environ.get("BRAIN_MCP_BIND_ADDRESS", "127.0.0.1")
    ).strip()
    if candidate != "127.0.0.1":
        raise PermissionError(
            f"403 Forbidden: MCP transport must remain strictly local/loopback (127.0.0.1); got {candidate!r}."
        )
    return candidate


def semantic_search(query: str, n_results: int = 5, project: str | None = None) -> list[dict]:
    query = str(query or "")[:4096]
    try:
        n_results = max(1, min(int(n_results), 10))
    except (TypeError, ValueError):
        n_results = 5
    projects = (str(project).strip(),) if project and str(project).strip() else ()
    direct, neighbors = search_memory(
        query,
        RecallOptions(
            n_results=n_results,
            follow_links=True,
            include_statuses={"accepted"},
            projects=projects,
        ),
    )
    return [item.as_dict() for item in direct + neighbors]


def read_file_safe(path: str | None) -> str:
    if not path or not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def list_md_files(directory: str) -> list[str]:
    if not os.path.exists(directory):
        return []
    return sorted([f for f in os.listdir(directory) if is_content_markdown(f)])


def latest_briefing_path() -> str | None:
    files = sorted(glob.glob(os.path.join(BRIEFINGS_DIR, "[0-9]*.md")), reverse=True)
    return files[0] if files else None


def audit_mcp_use(action: str, audit_path: str = MCP_AUDIT_PATH) -> dict:
    """Append a content-free audit event before serving an MCP request."""
    if action not in {"tool:search_memory", "tool:get_atom", "tool:add_to_inbox", "tool:unknown", "resource:read"}:
        raise ValueError("MCP audit action is not allowlisted.")
    os.makedirs(os.path.dirname(os.path.abspath(audit_path)), exist_ok=True)
    record = {
        "schema_version": 1,
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "transport": "stdio",
        "event": action,
    }
    payload = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise OSError("MCP audit append was incomplete.")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return record


# ─── MCP Tools ────────────────────────────────────────────────────────────────

ALL_TOOLS = [
    types.Tool(
        name="search_memory",
        description="Semantically search the Second Brain vault for relevant atomic notes.",
        inputSchema={
            "type": "object",
            "properties": {
                "query":        {"type": "string", "description": "Natural language search query."},
                "n_results":    {"type": "integer", "description": "Number of results (default 5)."},
                "project":      {"type": "string", "description": "Optional project scope tag (e.g. project:second_brain)."},
                "bearer_token": {"type": "string", "description": "Optional MCP bearer token for authorization."},
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="get_atom",
        description="Read the full content of a specific atom by filename.",
        inputSchema={
            "type": "object",
            "properties": {
                "atom_id":      {"type": "string", "description": "Atom filename (e.g. 2026-0619-sodium-ion.md)"},
                "bearer_token": {"type": "string", "description": "Optional MCP bearer token for authorization."},
            },
            "required": ["atom_id"],
        },
    ),
    types.Tool(
        name="add_to_inbox",
        description="Add a new note or idea to the Second Brain inbox for overnight processing.",
        inputSchema={
            "type": "object",
            "properties": {
                "content":      {"type": "string", "maxLength": MAX_INBOX_CHARS, "description": "Untrusted raw capture, limited to 100,000 characters."},
                "source":       {"type": "string", "description": "Where this came from (optional)."},
                "bearer_token": {"type": "string", "description": "Optional MCP bearer token for authorization."},
            },
            "required": ["content"],
        },
    ),
]


async def handle_list_tools(_ctx, _params) -> types.ListToolsResult:
    assert_mcp_loopback_transport()
    if not is_authorized(_params, _ctx):
        raise PermissionError("403 Forbidden: Invalid or missing MCP bearer token.")
    allowed = get_allowed_tools()
    tools = [tool for tool in ALL_TOOLS if tool.name in allowed]
    return types.ListToolsResult(tools=tools)


async def handle_call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    name = params.name
    arguments = params.arguments or {}
    audit_mcp_use(f"tool:{name}" if name in TOOL_NAMES else "tool:unknown")
    ensure_dirs()
    assert_mcp_loopback_transport()

    if not is_authorized(params, _ctx):
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="Unauthorized: Invalid or missing MCP bearer token.")],
            isError=True,
        )

    if name not in get_allowed_tools():
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Permission denied: Tool '{name}' is disabled by tool policy.")],
            isError=True,
        )

    if name == "search_memory":
        query    = arguments["query"]
        n        = arguments.get("n_results", 5)
        project  = arguments.get("project")
        try:
            results = semantic_search(query, n_results=n, project=project)
        except MemoryRecallUnavailable as exc:
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"Memory recall unavailable: {exc}")], isError=True)
        if not results:
            text = "No relevant memories found for this query."
        else:
            parts = []
            for r in results:
                reasons = ",".join(r.get("match_reasons", []))
                trust = (
                    f"status={r['review_status']}; type={r['memory_type']}; "
                    f"confidence={r['confidence']:.2f}; importance={r['importance']:.2f}; "
                    f"score={r['score']:.3f}; match={reasons}"
                )
                parts.append(f"### {r['filename']}\n\n`{trust}`\n\n{r['content']}")
            text = "\n\n---\n\n".join(parts)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    elif name == "get_atom":
        raw_atom_id = str(arguments.get("atom_id", ""))
        if "../" in raw_atom_id or "..\\" in raw_atom_id or "/" in raw_atom_id or "\\" in raw_atom_id:
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"Atom '{raw_atom_id}' not found or invalid.")])
        atom_id = normalize_markdown_filename(raw_atom_id)
        path    = safe_file_in_dir(ATOMS_DIR, atom_id, allowed_exts={".md"})
        if not path:
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"Atom '{atom_id}' not found or invalid.")])
        if not approved_revision_is_current(path):
            return types.CallToolResult(content=[types.TextContent(type="text", text="Atom is not currently owner-approved for external reading.")])
        try:
            record = parse_atom_record(path)
            if record.get("review_status") != "accepted":
                return types.CallToolResult(content=[types.TextContent(type="text", text="Atom is not currently owner-approved for external reading.")])
        except Exception:
            return types.CallToolResult(content=[types.TextContent(type="text", text="Atom is not currently owner-approved for external reading.")])
        content  = read_file_safe(path)
        text     = content if content else f"Atom '{atom_id}' not found."
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    elif name == "add_to_inbox":
        for key in ("content", "source", "filename", "path"):
            val = str(arguments.get(key, ""))
            if "../" in val or "..\\" in val or "/.." in val or "\\.." in val:
                raise ValueError("Path traversal attempt detected in arguments.")

        content = str(arguments.get("content", ""))
        if "\x00" in content:
            raise ValueError("Invalid content: null bytes detected.")

        if not content.strip() or len(content) > MAX_INBOX_CHARS:
            raise ValueError(f"Inbox content must contain 1 to {MAX_INBOX_CHARS} characters.")

        source  = arguments.get("source", "MCP agent")
        source = str(source).replace("--", "—").replace("\r", " ").replace("\n", " ")[:500]
        os.makedirs(INBOX_DIR, exist_ok=True)
        filename = f"{now_slug()}-mcp-note.md"
        target_path = safe_file_in_dir(INBOX_DIR, filename, allowed_exts={".md"}, must_exist=False)
        if not target_path:
            raise ValueError("Path traversal attempt detected: destination outside inbox.")

        suffix = 1
        while True:
            try:
                descriptor = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    payload = (
                        f"<!-- second_brain_origin: mcp_local -->\n"
                        f"<!-- Added via local MCP on {today()} from: {source} -->\n\n"
                        f"{content}\n"
                    ).encode("utf-8")
                    written = os.write(descriptor, payload)
                    if written != len(payload):
                        raise OSError("Incomplete write to quarantined inbox.")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                break
            except FileExistsError:
                filename = f"{now_slug()}-mcp-note-{suffix}.md"
                target_path = safe_file_in_dir(INBOX_DIR, filename, allowed_exts={".md"}, must_exist=False)
                if not target_path:
                    raise ValueError("Path traversal attempt detected: destination outside inbox.")
                suffix += 1
        text = f"Note saved to quarantined inbox as '{filename}'. It will be processed on the next Refinery Run."
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    return types.CallToolResult(content=[types.TextContent(type="text", text=f"Unknown tool: {name}")], isError=True)


# ─── MCP Resources ────────────────────────────────────────────────────────────

async def handle_list_resources(_ctx, _params) -> types.ListResourcesResult:
    assert_mcp_loopback_transport()
    if not is_authorized(_params, _ctx):
        raise PermissionError("403 Forbidden: Invalid or missing MCP bearer token.")
    # No listable resource catalog: it would enumerate private vault material.
    return types.ListResourcesResult(resources=[])


async def handle_read_resource(_ctx, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
    audit_mcp_use("resource:read")
    assert_mcp_loopback_transport()
    if not is_authorized(params, _ctx):
        return types.ReadResourceResult(contents=[])
    uri = str(params.uri)
    if uri.startswith("brain://atoms/"):
        filename = uri.removeprefix("brain://atoms/")
        path = safe_file_in_dir(ATOMS_DIR, filename, allowed_exts={".md"})
        if not path or not approved_revision_is_current(path):
            return types.ReadResourceResult(contents=[])
        try:
            record = parse_atom_record(path)
            if record.get("review_status") != "accepted":
                return types.ReadResourceResult(contents=[])
        except Exception:
            return types.ReadResourceResult(contents=[])
        content = read_file_safe(path)
        return types.ReadResourceResult(contents=[types.TextResourceContents(uri=uri, mimeType="text/markdown", text=content)])
    return types.ReadResourceResult(contents=[])


app = Server(
    "second-brain",
    version="0.1.0",
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool,
    on_list_resources=handle_list_resources,
    on_read_resource=handle_read_resource,
)


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    if os.environ.get("BRAIN_ENABLE_LOCAL_MCP") != "1":
        raise SystemExit("MCP is disabled. Set BRAIN_ENABLE_LOCAL_MCP=1 only for a trusted local process; do not enable it in cloud-connected clients.")
    assert_mcp_loopback_transport()
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import argparse
    import asyncio

    cli_parser = argparse.ArgumentParser(add_help=False)
    cli_parser.add_argument("--generate-token", action="store_true", help="Generate and persist a cryptographic MCP bearer token.")
    known_args, _ = cli_parser.parse_known_args()
    if known_args.generate_token:
        new_token = generate_bearer_token()
        saved = save_bearer_token(new_token)
        print(f"MCP bearer token generated and stored at {saved}")
        print(f"BRAIN_MCP_BEARER_TOKEN={new_token}")
        raise SystemExit(0)
    asyncio.run(main())
