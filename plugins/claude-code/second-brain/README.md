# Second Brain Plugin for Claude Code

Local-first verifiable memory integration (`second-brain`) for Claude Code, combining **Gen-4 automatic lifecycle recall & closed-loop episodic logging** (`~/.claude/settings.json` hooks) with **all 36 audited, project-scoped MCP tools on demand** over local stdio:
- **Automatic Gen-4 Cognitive Lifecycle Hooks (`~/.claude/settings.json`)**:
  - `SessionStart`: surfaces active verified Skill Playbooks, active Stage 4 Executable Skills, active owner-promoted Principles & Mental Models, workspace-scoped verified atoms, fast structural contradiction/drift alerts (`enable_nli=False`), and open project action items once at session start.
  - `UserPromptSubmit`: deduplicates per session using per-conversation lock-free state files, checks the L0 Predictive Memory Bus, prevents workspace recall starvation by prioritizing scoped project atoms alongside global memories, surfaces relevant Principles/Mental Models, Playbooks, inline contradiction warnings, concise `meta_router` tool hints, and scoped working notes wrapped in `<second_brain_auto_context untrusted="true">`. Set `BRAIN_HOOK_AUTO_RECALL=0` to disable automatic injection while keeping on-demand MCP tools active.
  - `Stop`: closed-loop episodic logger (opt-outable via `BRAIN_HOOK_AUTO_EXPERIENCE=0`; never blocks stopping) that records compact DLP- and risk-scanned turn summaries into `brain/4-experiences/` without ever mutating `2-atoms/`.
- **Verified Knowledge, Principles, Playbooks & Stage 4 Executable Skills**: `search_memory`, `get_atom`, `list_playbooks`, `get_playbook`, `list_skills`, `get_skill`, `execute_skill`, `list_principles`, `assemble_plan`
- **Knowledge Graph, Gen-4 Hierarchy/Simulation/Swarm/Curiosity, Quantitative Verification, Time-Travel, Multi-Vault & Cryptographic Ledger**: `list_contradictions`, `graph_neighbors`, `zoom_memory`, `get_concept`, `simulate_impact`, `verify_symbolic`, `run_debate`, `curiosity_gaps`, `record_context_signal`, `list_snapshots`, `recall_at_snapshot`, `verify_ledger`, `prove_leaf`, `list_vaults`, `federated_recall`
- **Working Notes, Tasks, Creative Media, Git Distiller & Quarantined Capture**: `search_notes(query, limit?, project?)`, `get_note`, `extract_action_items`, `media_status`, `list_generated_media`, `git_hook_status`, `distill_git_commits`, `remember`, `add_to_inbox`, `record_experience`, `record_feedback`

## One-Click Setup

Register the `second-brain` MCP server, lifecycle hooks, skill, and rules in user scope (`~/.claude.json` + `~/.claude/`) with one command:

```bash
uv run --frozen second-brain connect add claude-code --yes
```

What `second-brain connect add claude-code` does automatically:
- Generates (or reuses) your local 256-bit `mcp-token` under the owner-only `SecondBrain/security/` state directory.
- Atomically merges the `second-brain` stdio server entry into `~/.claude.json` while preserving all existing MCP servers and creating a token-redacted `.bak-<timestamp>` backup.
- Atomically merges Second Brain `SessionStart`, `UserPromptSubmit`, and `Stop` hooks into `~/.claude/settings.json` while preserving 100% of existing user settings and user hooks.
- Installs `~/.claude/skills/second-brain-memory/SKILL.md`, `~/.claude/rules/second-brain.md`, and a managed `<!-- BEGIN SECOND-BRAIN-AGENTS -->...<!-- END SECOND-BRAIN-AGENTS -->` block inside `~/.claude/AGENTS.md`.

You can also load `plugins/claude-code/second-brain` directly via `claude --plugin-dir plugins/claude-code/second-brain`: `.mcp.json` safely validates `CLAUDE_PLUGIN_ROOT` / `SECOND_BRAIN_REPO_ROOT` (never walking relative to arbitrary CWD) and provisions/reads your owner-only local `mcp-token` in-process without requiring manual `BRAIN_MCP_CLIENT_TOKEN` exports.

## Inspect or Remove

```bash
uv run --frozen second-brain connect add claude-code --dry-run
uv run --frozen second-brain connect status
uv run --frozen second-brain connect remove claude-code
```

`second-brain connect remove claude-code` symmetrically removes `second-brain` from `~/.claude.json`, strips ONLY Second Brain's managed hook entries from `~/.claude/settings.json`, deletes Second Brain-owned skill/rule files, and strips only the fenced Second Brain block from `~/.claude/AGENTS.md` while preserving all user-authored rules and hooks.

## Privacy & Trust Boundary

Your vault remains on this machine. When Claude Code uses Second Brain auto-recall hooks or MCP tools, recalled verified atoms, principles, playbooks, and working-note excerpts are sent to Anthropic as prompt context. Preference / epistemic-contract atoms and memories tagged for other projects are excluded automatically. Agents can never verify or approve canonical atoms, principles, or playbooks—only the human owner can approve memories via the Second Brain Desktop app, local Streamlit Dashboard, or `second-brain` CLI.
