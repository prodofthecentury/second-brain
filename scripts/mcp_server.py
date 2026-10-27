# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = [
#     "mcp",
#     "ollama",
# ]
# ///
"""
Feature 4 — Native MCP Server
Exposes a deliberately limited MCP (Model Context Protocol) interface to a
trusted local process. It is disabled unless BRAIN_ENABLE_LOCAL_MCP=1 is set.

Tools exposed:
  search_memory(query)     — Semantic vector search across all atoms
  get_atom(atom_id)        — Read a specifically named, currently approved atom
  add_to_inbox(content)    — Add a size-limited, untrusted capture to 0-raw/

Resources exposed:
  brain://atoms/{id}       — Direct atom access
  brain://threads/{slug}   — Direct thread access
  brain://briefings/latest — Latest briefing

Run only from an explicitly trusted local process:
    $env:BRAIN_ENABLE_LOCAL_MCP = "1"
    uv run scripts/mcp_server.py
"""

import os
import sys
import json
import glob

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (
    ATOMS_DIR, THREADS_DIR, BRIEFINGS_DIR, RAW_DIR,
    ensure_dirs, today, now_slug,
    is_content_markdown, normalize_markdown_filename, safe_file_in_dir
)
from recall import MemoryRecallUnavailable, RecallOptions, search_memory
from review_atom import approved_revision_is_current


MAX_INBOX_CHARS = 100_000

app = Server("second-brain")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def semantic_search(query: str, n_results: int = 5) -> list[dict]:
    query = str(query or "")[:4096]
    try:
        n_results = max(1, min(int(n_results), 10))
    except (TypeError, ValueError):
        n_results = 5
    direct, neighbors = search_memory(query, RecallOptions(n_results=n_results, follow_links=True))
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


# ─── MCP Tools ────────────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_memory",
            description="Semantically search the Second Brain vault for relevant atomic notes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query":    {"type": "string", "description": "Natural language search query."},
                    "n_results": {"type": "integer", "description": "Number of results (default 5)."},
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
            description="Add a new note or idea to the Second Brain inbox for overnight processing.",
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


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    ensure_dirs()

    if name == "search_memory":
        query    = arguments["query"]
        n        = arguments.get("n_results", 5)
        try:
            results = semantic_search(query, n_results=n)
        except MemoryRecallUnavailable as exc:
            return [types.TextContent(type="text", text=f"Memory recall unavailable: {exc}")]
        if not results:
            text = "No relevant memories found for this query."
        else:
            parts = []
            for r in results:
                trust = (
                    f"status={r['review_status']}; type={r['memory_type']}; "
                    f"confidence={r['confidence']:.2f}; importance={r['importance']:.2f}; "
                    f"score={r['score']:.3f}"
                )
                parts.append(f"### {r['filename']}\n\n`{trust}`\n\n{r['content']}")
            text = "\n\n---\n\n".join(parts)
        return [types.TextContent(type="text", text=text)]

    elif name == "get_atom":
        atom_id = normalize_markdown_filename(arguments["atom_id"])
        path    = safe_file_in_dir(ATOMS_DIR, atom_id, allowed_exts={".md"})
        if not path:
            return [types.TextContent(type="text", text=f"Atom '{atom_id}' not found or invalid.")]
        if not approved_revision_is_current(path):
            return [types.TextContent(type="text", text="Atom is not currently owner-approved for external reading.")]
        content  = read_file_safe(path)
        text     = content if content else f"Atom '{atom_id}' not found."
        return [types.TextContent(type="text", text=text)]

    elif name == "add_to_inbox":
        content = str(arguments.get("content", ""))
        if not content.strip() or len(content) > MAX_INBOX_CHARS:
            return [types.TextContent(type="text", text=f"Inbox content must contain 1 to {MAX_INBOX_CHARS} characters.")]
        source  = arguments.get("source", "MCP agent")
        source = str(source).replace("--", "—").replace("\r", " ").replace("\n", " ")[:500]
        filename = f"{now_slug()}-mcp-note.md"
        path     = os.path.join(RAW_DIR, filename)
        suffix = 1
        while True:
            try:
                with open(path, "x", encoding="utf-8") as f:
                    f.write(f"<!-- second_brain_origin: mcp_local -->\n<!-- Added via local MCP on {today()} from: {source} -->\n\n{content}\n")
                break
            except FileExistsError:
                filename = f"{now_slug()}-mcp-note-{suffix}.md"
                path = os.path.join(RAW_DIR, filename)
                suffix += 1
        text = f"Note saved to inbox as '{filename}'. It will be processed on the next Refinery Run."
        return [types.TextContent(type="text", text=text)]

    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


# ─── MCP Resources ────────────────────────────────────────────────────────────

@app.list_resources()
async def list_resources() -> list[types.Resource]:
    # No listable resource catalog: it would enumerate private vault material.
    return []


@app.read_resource()
async def read_resource(uri: str) -> str:
    if uri.startswith("brain://atoms/"):
        filename = uri.removeprefix("brain://atoms/")
        path = safe_file_in_dir(ATOMS_DIR, filename, allowed_exts={".md"})
        if not path or not approved_revision_is_current(path):
            return ""
        return read_file_safe(path)
    return ""


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    if os.environ.get("BRAIN_ENABLE_LOCAL_MCP") != "1":
        raise SystemExit("MCP is disabled. Set BRAIN_ENABLE_LOCAL_MCP=1 only for a trusted local process; do not enable it in cloud-connected clients.")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
