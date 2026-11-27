# Second Brain Plugin for Antigravity

Local-first verifiable memory integration (`second-brain`) for Antigravity, combining **Gen-4 automatic lifecycle recall & closed-loop episodic logging** (`hooks.json`) with **all 36 audited, project-scoped MCP tools on demand** over local stdio:
- **Automatic Gen-4 Cognitive Lifecycle Hooks (`hooks.json`)**:
  - `SessionStart`: surfaces active verified Skill Playbooks, active Stage 4 Executable Skills, active owner-promoted Principles & Mental Models, workspace-scoped verified atoms, fast structural contradiction/drift alerts (`enable_nli=False`), and open project action items once at conversation startup.
  - `PreInvocation`: deduplicates per conversation using lock-free per-conversation state files (fast ~35ms skip on repeated tool-loop invocations), checks the L0 Predictive Memory Bus, prevents workspace recall starvation by prioritizing scoped project atoms alongside global memories, surfaces relevant Principles/Mental Models, Playbooks, inline contradiction warnings, concise `meta_router` tool hints, and scoped working notes wrapped in `<second_brain_auto_context untrusted="true">`. Set `BRAIN_HOOK_AUTO_RECALL=0` to disable automatic injection while keeping on-demand MCP tools active.
  - `Stop`: closed-loop episodic logger (opt-outable via `BRAIN_HOOK_AUTO_EXPERIENCE=0`; never blocks stopping) that records compact DLP- and risk-scanned turn summaries into `brain/4-experiences/` on normal substantive turn completion without ever mutating `2-atoms/`.
- **Verified Knowledge, Principles, Playbooks & Stage 4 Executable Skills**: `search_memory`, `get_atom`, `list_playbooks`, `get_playbook`, `list_skills`, `get_skill`, `execute_skill`, `list_principles`, `assemble_plan`
- **Knowledge Graph, Gen-4 Hierarchy/Simulation/Swarm/Curiosity, Quantitative Verification, Time-Travel, Multi-Vault & Cryptographic Ledger**: `list_contradictions`, `graph_neighbors`, `zoom_memory`, `get_concept`, `simulate_impact`, `verify_symbolic`, `run_debate`, `curiosity_gaps`, `record_context_signal`, `list_snapshots`, `recall_at_snapshot`, `verify_ledger`, `prove_leaf`, `list_vaults`, `federated_recall`
- **Working Notes, Tasks, Creative Media, Git Distiller & Quarantined Capture**: `search_notes(query, limit?, project?)`, `get_note`, `extract_action_items`, `media_status`, `list_generated_media`, `git_hook_status`, `distill_git_commits`, `remember`, `add_to_inbox`, `record_experience`, `record_feedback`

## One-Click Setup

Register the plugin bundle, rules, skill, auto-recall/stop hooks, and live stdio MCP server directly from your Second Brain checkout:

```bash
uv run --frozen second-brain connect add antigravity --yes
```

What `second-brain connect add antigravity` does automatically:
- Generates (or reuses) your local 256-bit `mcp-token` under the owner-only `SecondBrain/security/` state directory.
- Installs `plugin.json`, `README.md`, `hooks.json` (`SessionStart`, `PreInvocation`, `Stop`), `rules/AGENTS.md`, and `skills/second-brain-memory/SKILL.md` into `~/.gemini/config/plugins/second-brain/`.
- Writes `mcp_config.json` with your exact repository `.venv` Python path and trusted-stdio token (`BRAIN_MCP_TRUSTED_STDIO=1` + `BRAIN_MCP_CLIENT_TOKEN`), storing token-redacted `.bak-<timestamp>` backups outside the plugin directory (`~/.gemini/config/.second-brain-backups/`) when updating an existing config.

## Inspect or Remove

```bash
uv run --frozen second-brain connect add antigravity --dry-run
uv run --frozen second-brain connect status
uv run --frozen second-brain connect remove antigravity
```

`second-brain connect remove antigravity` symmetrically removes the `second-brain` MCP server entry and cleans up all Second Brain-installed plugin bundle files (including `hooks.json`) and empty subdirectories.

## Privacy & Trust Boundary

Your vault remains on this machine. When an external/cloud-hosted model uses Second Brain auto-recall hooks or MCP tools, recalled verified atoms, principles, playbooks, and working-note excerpts are sent to that host's model provider as prompt context. Preference / epistemic-contract atoms and memories tagged for other projects are excluded automatically. Agents can never verify or approve canonical atoms, principles, or playbooks—only the human owner can approve memories via the Second Brain Desktop app, local Streamlit Dashboard, or `second-brain` CLI.
