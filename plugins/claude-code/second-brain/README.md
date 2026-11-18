# Second Brain Plugin for Claude Code

Local-first verifiable memory integration (`second-brain`) for Claude Code, exposing all 17 audited, project-scoped MCP tools over local stdio:
- **Verified Knowledge & Playbooks**: `search_memory`, `get_atom`, `list_playbooks`, `get_playbook`, `assemble_plan`
- **Knowledge Graph, Time-Travel & Cryptographic Ledger**: `list_contradictions`, `graph_neighbors`, `list_snapshots`, `recall_at_snapshot`, `verify_ledger`, `prove_leaf`
- **Working Notes & Quarantined/Sidecar Capture**: `search_notes(query, limit?, project?)`, `get_note`, `remember`, `add_to_inbox`, `record_experience`, `record_feedback`

## One-Click Setup

Register the `second-brain` MCP server, skill, and rules in user scope (`~/.claude.json` + `~/.claude/`) with one command:

```bash
uv run --frozen second-brain connect add claude-code --yes
```

What `second-brain connect add claude-code` does automatically:
- Generates (or reuses) your local 256-bit `mcp-token` under the owner-only `SecondBrain/security/` state directory.
- Atomically merges the `second-brain` stdio server entry into `~/.claude.json` while preserving all existing MCP servers and creating a `.bak-<timestamp>` backup.
- Installs `~/.claude/skills/second-brain-memory/SKILL.md`, `~/.claude/rules/second-brain.md`, and a managed `<!-- BEGIN SECOND-BRAIN-AGENTS -->...<!-- END SECOND-BRAIN-AGENTS -->` block inside `~/.claude/AGENTS.md`.

You can also load `plugins/claude-code/second-brain` directly via `claude --plugin-dir plugins/claude-code/second-brain`: `.mcp.json` safely validates `CLAUDE_PLUGIN_ROOT` / `SECOND_BRAIN_REPO_ROOT` (never walking relative to arbitrary CWD) and provisions/reads your owner-only local `mcp-token` in-process without requiring manual `BRAIN_MCP_CLIENT_TOKEN` exports.

## Inspect or Remove

```bash
uv run --frozen second-brain connect add claude-code --dry-run
uv run --frozen second-brain connect status
uv run --frozen second-brain connect remove claude-code
```

`second-brain connect remove claude-code` symmetrically removes `second-brain` from `~/.claude.json`, deletes Second Brain-owned skill/rule files, and strips only the fenced Second Brain block from `~/.claude/AGENTS.md` while preserving all user-authored rules.

## Privacy & Trust Boundary

Your vault remains on this machine. When Claude Code invokes Second Brain MCP tools, recalled verified atoms, playbooks, and working-note excerpts are sent to Anthropic as prompt context. Agents can never verify or approve canonical atoms or playbooks—only the human owner can approve memories via the Second Brain Desktop app, local Streamlit Dashboard, or `second-brain` CLI.
