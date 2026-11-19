# Second Brain Plugin for Antigravity

Local-first verifiable memory integration (`second-brain`) for Antigravity, exposing all 17 audited, project-scoped MCP tools over local stdio:
- **Verified Knowledge & Playbooks**: `search_memory`, `get_atom`, `list_playbooks`, `get_playbook`, `assemble_plan`
- **Knowledge Graph, Time-Travel & Cryptographic Ledger**: `list_contradictions`, `graph_neighbors`, `list_snapshots`, `recall_at_snapshot`, `verify_ledger`, `prove_leaf`
- **Working Notes & Quarantined/Sidecar Capture**: `search_notes(query, limit?, project?)`, `get_note`, `remember`, `add_to_inbox`, `record_experience`, `record_feedback`

## One-Click Setup

Register the plugin bundle, rules, skill, and live stdio MCP server directly from your Second Brain checkout:

```bash
uv run --frozen second-brain connect add antigravity --yes
```

What `second-brain connect add antigravity` does automatically:
- Generates (or reuses) your local 256-bit `mcp-token` under the owner-only `SecondBrain/security/` state directory.
- Installs `plugin.json`, `README.md`, `rules/AGENTS.md`, and `skills/second-brain-memory/SKILL.md` into `~/.gemini/config/plugins/second-brain/`.
- Writes `mcp_config.json` with your exact repository path and trusted-stdio token (`BRAIN_MCP_TRUSTED_STDIO=1` + `BRAIN_MCP_CLIENT_TOKEN`), creating a timestamped `.bak-<timestamp>` backup if an earlier config exists.

## Inspect or Remove

```bash
uv run --frozen second-brain connect add antigravity --dry-run
uv run --frozen second-brain connect status
uv run --frozen second-brain connect remove antigravity
```

`second-brain connect remove antigravity` symmetrically removes the `second-brain` MCP server entry and cleans up all Second Brain-installed plugin bundle files and empty subdirectories.

## Privacy & Trust Boundary

Your vault remains on this machine. When an external/cloud-hosted model invokes Second Brain MCP tools, recalled verified atoms, playbooks, and working-note excerpts are sent to that host's model provider as prompt context. Agents can never verify or approve canonical atoms or playbooks—only the human owner can approve memories via the Second Brain Desktop app, local Streamlit Dashboard, or `second-brain` CLI.
