# Second Brain Plugin for Claude Code

Local-first verifiable memory integration (`second-brain`) for Claude Code, exposing owner-verified atoms (`search_memory`, `get_atom`), working notes (`search_notes`, `get_note`, `remember`), and quarantined inbox capture (`add_to_inbox`) over local stdio MCP.

## One-Click Setup

Register the `second-brain` MCP server in user scope (`~/.claude.json`) with one command:

```bash
uv run --frozen second-brain connect add claude-code
```

What `second-brain connect add claude-code` does automatically:
- Generates (or reuses) your local 256-bit `mcp-token` under the owner-only `SecondBrain/security/` state directory.
- Atomically merges the `second-brain` stdio server entry (with your exact repository path, `BRAIN_MCP_TRUSTED_STDIO=1`, and `BRAIN_MCP_CLIENT_TOKEN`) into `~/.claude.json` while preserving all existing MCP servers and creating a `.bak-<timestamp>` backup.

To also load the bundled `second-brain-memory` skill and `rules/AGENTS.md` directly in Claude Code, point `--plugin-dir` at `plugins/claude-code/second-brain` (with `BRAIN_MCP_CLIENT_TOKEN` set if using `.mcp.json` standalone instead of `second-brain connect add claude-code`).

## Inspect or Remove

```bash
uv run --frozen second-brain connect add claude-code --dry-run
uv run --frozen second-brain connect status
uv run --frozen second-brain connect remove claude-code
```

## Privacy & Trust Boundary

Your vault remains on this machine. When Claude Code invokes Second Brain MCP tools, recalled verified atoms and working-note excerpts are sent to Anthropic as prompt context. Agents can never verify or approve canonical atoms—only the human owner can approve memories via the Second Brain Desktop app, local Streamlit Dashboard, or `second-brain` CLI.
