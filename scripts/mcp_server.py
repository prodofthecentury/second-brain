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
import tempfile
from collections.abc import Mapping
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
MIN_BEARER_TOKEN_LENGTH = 16
MCP_AUDIT_PATH = os.path.join(RAW_DIR, "archive", "mcp-audit.jsonl")
TOOL_NAMES = {"search_memory", "get_atom", "add_to_inbox"}
UNAVAILABLE_ATOM_MESSAGE = "Atom not found or not currently owner-approved for external reading."


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
    if env_token is not None:
        cleaned = env_token.strip()
        if len(cleaned) < 16:
            raise PermissionError("Configured BRAIN_MCP_BEARER_TOKEN must contain at least 16 characters.")
        return cleaned
    token_path = default_mcp_token_path()
    if token_path.exists():
        try:
            content = token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PermissionError(f"MCP token file could not be read safely: {exc}") from exc
        if len(content) < 16:
            raise PermissionError("MCP token file is empty or shorter than 16 characters.")
        return content
    return None


def generate_bearer_token() -> str:
    return secrets.token_hex(32)


def save_bearer_token(token: str, token_path: Path | None = None) -> Path:
    cleaned = str(token or "").strip()
    if len(cleaned) < MIN_BEARER_TOKEN_LENGTH:
        raise ValueError(f"MCP bearer token must contain at least {MIN_BEARER_TOKEN_LENGTH} characters.")
    path = Path(token_path or default_mcp_token_path()).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if os.name != "nt":
            os.chmod(path.parent, 0o700)
    except OSError:
        pass
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".mcp-token-")
    try:
        try:
            if os.name != "nt":
                os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(cleaned + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            try:
                os.remove(temporary)
            except OSError:
                pass
    return path


def _extract_token_from_meta(meta: object) -> str | None:
    if meta is None:
        return None
    mapping = None
    if isinstance(meta, Mapping):
        mapping = meta
    elif hasattr(meta, "model_extra") and isinstance(meta.model_extra, Mapping):
        mapping = meta.model_extra
    for key in ("authorization", "bearer_token", "auth_token", "token"):
        val = mapping.get(key) if mapping is not None else getattr(meta, key, None)
        if isinstance(val, str) and val.strip():
            cleaned = val.strip()
            if cleaned.lower().startswith("bearer "):
                cleaned = cleaned[7:].strip()
            if cleaned:
                return cleaned
    return None


def extract_bearer_token(params=None, ctx=None) -> str | None:
    for holder in (params, ctx):
        if holder is None:
            continue
        candidates = []
        if isinstance(holder, Mapping):
            candidates.extend([holder.get("meta"), holder.get("_meta")])
        else:
            candidates.extend([getattr(holder, "meta", None), getattr(holder, "_meta", None)])
            extra = getattr(holder, "model_extra", None)
            if isinstance(extra, Mapping):
                candidates.extend([extra.get("_meta"), extra.get("meta")])
        for meta in candidates:
            token = _extract_token_from_meta(meta)
            if token:
                return token
    return None


def is_authorized(params=None, ctx=None) -> bool:
    """Fail-closed bearer token authorization across all MCP handlers."""
    try:
        configured = get_configured_bearer_token()
    except PermissionError:
        return False
    if not configured or len(configured) < MIN_BEARER_TOKEN_LENGTH:
        return False
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


def audit_mcp_use(
    event_type: str | None = None,
    outcome: str = "authorized",
    audit_path: str | None = None,
    *,
    action: str | None = None,
) -> dict:
    """Append a content-free audit event before serving an MCP request."""
    resolved_event = event_type if event_type is not None else action
    if audit_path is None and outcome not in {"authorized", "denied"} and (
        "/" in outcome or "\\" in outcome or outcome.endswith(".jsonl")
    ):
        audit_path = outcome
        outcome = "authorized"
    if resolved_event not in {"tool:search_memory", "tool:get_atom", "tool:add_to_inbox", "tool:unknown", "resource:read"}:
        raise ValueError("MCP audit action is not allowlisted.")
    if outcome not in {"authorized", "denied"}:
        raise ValueError("MCP audit outcome is not allowlisted.")
    target_path = audit_path or MCP_AUDIT_PATH
    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
    record = {
        "schema_version": 1,
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "transport": "stdio",
        "event": resolved_event,
        "outcome": outcome,
    }
    payload = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
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
                "query":     {"type": "string", "description": "Natural language search query."},
                "n_results": {"type": "integer", "description": "Number of results (default 5)."},
                "project":   {"type": "string", "description": "Optional project scope tag (e.g. project:second_brain)."},
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
                "atom_id": {"type": "string", "description": "Atom filename (e.g. 2026-0619-sodium-ion.md)"},
            },
            "required": ["atom_id"],
        },
    ),
    types.Tool(
        name="add_to_inbox",
        description="Add a new note or idea to the Second Brain quarantined inbox (0-raw/inbox/) for owner review.",
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "maxLength": MAX_INBOX_CHARS, "description": "Untrusted raw capture, limited to 100,000 characters."},
                "source":  {"type": "string", "description": "Where this came from (optional)."},
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
    assert_mcp_loopback_transport()
    name = params.name
    arguments = params.arguments or {}
    event_name = f"tool:{name}" if name in TOOL_NAMES else "tool:unknown"
    auth_ok = is_authorized(params, _ctx)

    if not auth_ok:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="Unauthorized: Invalid or missing MCP bearer token.")],
            isError=True,
        )

    audit_mcp_use(event_name, outcome="authorized")
    ensure_dirs()

    if name not in get_allowed_tools():
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Permission denied: Tool '{name}' is disabled by tool policy.")],
            isError=True,
        )

    if name == "search_memory":
        query = str(arguments.get("query") or "").strip()
        if not query:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="Invalid search request: 'query' must be a non-empty string.")],
                isError=True,
            )
        n       = arguments.get("n_results", 5)
        project = arguments.get("project")
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
        raw_atom_id = str(arguments.get("atom_id") or "").strip()
        if not raw_atom_id or "../" in raw_atom_id or "..\\" in raw_atom_id or "/" in raw_atom_id or "\\" in raw_atom_id:
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"Atom '{raw_atom_id}' not found or invalid.")])
        atom_id = normalize_markdown_filename(raw_atom_id)
        path    = safe_file_in_dir(ATOMS_DIR, atom_id, allowed_exts={".md"})
        if not path:
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"Atom '{atom_id}' not found or invalid.")])
        vault_root = os.path.dirname(os.path.abspath(ATOMS_DIR))
        if not approved_revision_is_current(path, brain_dir=vault_root, atoms_dir=ATOMS_DIR):
            return types.CallToolResult(content=[types.TextContent(type="text", text=UNAVAILABLE_ATOM_MESSAGE)])
        try:
            record = parse_atom_record(path)
            if record.get("review_status") != "accepted" or bool(record.get("superseded_by")):
                return types.CallToolResult(content=[types.TextContent(type="text", text=UNAVAILABLE_ATOM_MESSAGE)])
        except Exception:
            return types.CallToolResult(content=[types.TextContent(type="text", text=UNAVAILABLE_ATOM_MESSAGE)])
        content = read_file_safe(path)
        if not content:
            return types.CallToolResult(content=[types.TextContent(type="text", text=UNAVAILABLE_ATOM_MESSAGE)])
        return types.CallToolResult(content=[types.TextContent(type="text", text=content)])

    elif name == "add_to_inbox":
        for key in ("source", "filename", "path"):
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
        text = (
            f"Note saved to quarantined inbox as '{filename}'. "
            "It remains isolated in 0-raw/inbox/ until owner review and promotion before Refinery processing."
        )
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
    assert_mcp_loopback_transport()
    auth_ok = is_authorized(params, _ctx)
    if not auth_ok:
        if os.path.isdir(os.path.dirname(os.path.abspath(MCP_AUDIT_PATH))):
            audit_mcp_use("resource:read", outcome="denied")
        return types.ReadResourceResult(contents=[])
    audit_mcp_use("resource:read", outcome="authorized")
    uri = str(params.uri)
    if uri.startswith("brain://atoms/"):
        filename = uri.removeprefix("brain://atoms/")
        path = safe_file_in_dir(ATOMS_DIR, filename, allowed_exts={".md"})
        vault_root = os.path.dirname(os.path.abspath(ATOMS_DIR))
        if not path or not approved_revision_is_current(path, brain_dir=vault_root, atoms_dir=ATOMS_DIR):
            return types.ReadResourceResult(contents=[])
        try:
            record = parse_atom_record(path)
            if record.get("review_status") != "accepted" or bool(record.get("superseded_by")):
                return types.ReadResourceResult(contents=[])
        except Exception:
            return types.ReadResourceResult(contents=[])
        content = read_file_safe(path)
        if not content:
            return types.ReadResourceResult(contents=[])
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
    try:
        configured = get_configured_bearer_token()
    except PermissionError as exc:
        raise SystemExit(
            f"MCP startup aborted (fail-closed auth): {exc} "
            "Run `python scripts/mcp_server.py --generate-token` or set BRAIN_MCP_BEARER_TOKEN."
        ) from exc
    if not configured or len(configured) < MIN_BEARER_TOKEN_LENGTH:
        raise SystemExit(
            "MCP startup aborted: bearer token is required (16+ characters). "
            "Run `python scripts/mcp_server.py --generate-token` or set BRAIN_MCP_BEARER_TOKEN."
        )
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import argparse
    import asyncio

    cli_parser = argparse.ArgumentParser(add_help=False)
    cli_parser.add_argument("--generate-token", action="store_true", help="Generate and persist a cryptographic MCP bearer token.")
    cli_parser.add_argument("--print-token", action="store_true", help="Print the generated bearer token even when stdout is not a TTY.")
    known_args, _ = cli_parser.parse_known_args()
    if known_args.generate_token:
        new_token = generate_bearer_token()
        saved = save_bearer_token(new_token)
        print(f"MCP bearer token generated and stored at {saved}")
        if known_args.print_token or sys.stdout.isatty():
            print(f"BRAIN_MCP_BEARER_TOKEN={new_token}")
        raise SystemExit(0)
    asyncio.run(main())
