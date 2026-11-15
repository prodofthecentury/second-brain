# Second Brain Plugin for Antigravity

Local-first verifiable memory integration (`second-brain`) for Antigravity, exposing owner-verified atoms (`search_memory`, `get_atom`), working notes (`search_notes`, `get_note`, `remember`), and quarantined inbox capture (`add_to_inbox`) over local stdio MCP.

## One-Click Setup

Register the plugin bundle and live stdio MCP server directly from your Second Brain checkout:

```bash
uv run --frozen second-brain connect add antigravity
```

What `second-brain connect add antigravity` does automatically:
- Generates (or reuses) your local 256-bit `mcp-token` under the owner-only `SecondBrain/security/` state directory.
- Copies `plugin.json`, `rules/AGENTS.md`, and `skills/second-brain-memory/SKILL.md` into `~/.gemini/config/plugins/second-brain/`.
- Writes `mcp_config.json` with your exact repository path and trusted-stdio token (`BRAIN_MCP_TRUSTED_STDIO=1` + `BRAIN_MCP_CLIENT_TOKEN`), creating a timestamped `.bak-<timestamp>` backup if an earlier config exists.

## Inspect or Remove

```bash
uv run --frozen second-brain connect add antigravity --dry-run
uv run --frozen second-brain connect status
uv run --frozen second-brain connect remove antigravity
```

## Privacy & Trust Boundary

Your vault remains on this machine. When an external/cloud-hosted model invokes Second Brain MCP tools, recalled verified atoms and working-note excerpts are sent to that host's model provider as prompt context. Agents can never verify or approve canonical atoms—only the human owner can approve memories via the Second Brain Desktop app, local Streamlit Dashboard, or `second-brain` CLI.
