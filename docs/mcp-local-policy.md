# MCP Local-Use Policy & 35-Tool Specification (`scripts/mcp_server.py`)

The Second Brain MCP server is **disabled by default**. Do not register it with
cloud-connected clients, remote agents, or services that may send vault content
off this machine. This repository ships only explicit opt-in local stdio/client
configurations (`second-brain connect`, `plugins/antigravity/second-brain/`,
`plugins/claude-code/second-brain/`) and intentionally provides no cloud-host registration.

## Authentication & Transport Guardrails

- **Transport (`assert_mcp_loopback_transport`):** Local stdio only; `BRAIN_MCP_BIND_ADDRESS` rejects any non-`127.0.0.1` address with `403 Forbidden`.
- **Enablement:** Requires `BRAIN_ENABLE_LOCAL_MCP=1` scoped only to the trusted child process.
- **Dual Constant-Time Bearer Auth:** Every JSON-RPC handler (`tools/call`, etc.) requires constant-time (`hmac.compare_digest`) authorization against `%LOCALAPPDATA%/SecondBrain/security/mcp-token` (or `$XDG_STATE_HOME/SecondBrain/security/mcp-token` / `BRAIN_MCP_BEARER_TOKEN`) via one of two paths (never inside tool `arguments`):
  1. **Per-request `_meta.authorization` Bearer header:** `Bearer <token>` inside request `_meta`.
  2. **Local stdio IDE handshake (`second-brain connect`):** `BRAIN_MCP_TRUSTED_STDIO=1` paired with `BRAIN_MCP_CLIENT_TOKEN=<token>`.
- **Granular Capability Allowlist (`BRAIN_MCP_ALLOWED_TOOLS`):** Optional comma-separated subset intersecting `TOOL_NAMES`; disabled tools return `Permission denied`.
- **Content-Free Audit Log (`brain/0-raw/archive/mcp-audit.jsonl`):** Appends `{schema_version, recorded_at, transport: "stdio", event, outcome: "authorized" | "denied"}` with zero queries, atom IDs, or text before executing any operation (`0600` + `os.fsync`, symlink-rejected; audit failure aborts the request).

Generate a 256-bit local bearer token and launch manually:

```powershell
uv run --locked --extra mcp python scripts/mcp_server.py --generate-token
$env:BRAIN_ENABLE_LOCAL_MCP = "1"
try {
    uv run --locked --extra mcp python scripts/mcp_server.py
} finally {
    Remove-Item Env:BRAIN_ENABLE_LOCAL_MCP -ErrorAction SilentlyContinue
}
```

## Complete 35-Tool Catalog (`scripts/mcp_server.py:TOOL_NAMES`)

| Tier / Trust Boundary | Tool Name | Behavior & Epistemic Guarantee |
| --- | --- | --- |
| **Canonical Recall & Evidence** | `search_memory` | Shared verified recall across current owner-approved `2-atoms/` only (`project`, `as_of`, `include_expired`). |
| **Canonical Recall & Evidence** | `get_atom` | Reads one named atom only when its current SHA-256 owner approval & byte-offset source anchor still verify. |
| **Canonical Recall & Evidence** | `list_playbooks` | Lists owner-promoted `5-playbooks/` (`active` only in default scope; optional `project` filter). |
| **Canonical Recall & Evidence** | `get_playbook` | Reads one named owner-promoted playbook only after verifying integrity & live backing atoms. |
| **Canonical Recall & Evidence** | `list_skills` | Lists owner-promoted Stage 4 Executable Skills (`6-skills/`). |
| **Canonical Recall & Evidence** | `get_skill` | Inspects one named Stage 4 Executable Skill after verification. |
| **Canonical Recall & Evidence** | `execute_skill` | Executes only an owner-promoted Stage 4 sandboxed skill (bounded actions, kill-switch checked, hash-chained audit). |
| **Canonical Recall & Evidence** | `list_principles` | Lists active owner-promoted Principles (`7-principles/`) and Causal Mental Models (`8-mental-models/`) verified against live backing atoms without exposing raw host paths. |
| **Canonical Recall & Evidence** | `assemble_plan` | Bounded token-budget grounded plan combining approved atoms and active playbooks/skills. |
| **Canonical Recall & Evidence** | `list_contradictions` | Read-only Contradiction & Drift Radar over verified/active graph nodes. |
| **Canonical Recall & Evidence** | `graph_neighbors` | Bounded 1–2 hop approved `[[wikilink]]` neighbor traversal (`project` contained). |
| **Canonical Recall & Evidence** | `list_snapshots` | Lists durable point-in-time vault snapshots in `0-raw/archive/snapshots/`. |
| **Canonical Recall & Evidence** | `recall_at_snapshot` | Cross-Encoder point-in-time snapshot recall with non-owner redaction for revoked/modified atoms & playbooks. |
| **Canonical Recall & Evidence** | `verify_ledger` | Verifies cryptographic SHA-256 hash chain of `0-raw/archive/vault-ledger.jsonl`. |
| **Canonical Recall & Evidence** | `prove_leaf` | Returns Merkle inclusion proof for `kind`: `auto` \| `atom` \| `playbook` \| `skill`. |
| **Canonical Recall & Evidence** | `list_vaults` | Lists non-secret registered multi-vault workspace metadata. |
| **Canonical Recall & Evidence** | `federated_recall` | Strictly opt-in read-only cross-vault recall (`federated_searchable=true` vaults only). |
| **Canonical Recall & Evidence** | `zoom_memory` | Top-down L0–L5 fractal zoom retrieval across Vault, Domain, Project, Concept, Approved Atom, and Raw Byte Quotes (`project` scoped). |
| **Canonical Recall & Evidence** | `get_concept` | Retrieves a Living Concept Object with evolving confidence timeline, linked approved facts, mistakes, and typed causal edges (`project` scoped). |
| **Canonical Recall & Evidence** | `simulate_impact` | Read-only World-Model blast radius simulation across verified concept edges and past incidents (`project` scoped; host filesystem `repo_dir` scanning disabled over external MCP). |
| **Canonical Recall & Evidence** | `verify_symbolic` | Read-only quantitative symbolic equivalence checker, AST-jailed Python computation sandbox, or SafeVerify spec-integrity check. |
| **Canonical Recall & Evidence** | `run_debate` | Read-only two-sided adversarial Thesis vs Antithesis synthesis over verified atoms with provenance and failure history (`project` scoped). |
| **Canonical Recall & Evidence** | `curiosity_gaps` | Read-only Self-Model domain competence & prioritized curiosity gap report (`project` scoped; host filesystem paths stripped). |
| **Local Media & Hook Read** | `media_status` | Reports local creative media backends, hardware gates & PyAV `libx264` availability. |
| **Local Media & Hook Read** | `list_generated_media` | Lists provenance-verified artifacts in `sources/generated/` (no external network upload). |
| **Local Media & Hook Read** | `git_hook_status` | Reports installed `.git/hooks/post-commit` distiller status for a repo. |
| **Untrusted Working Notes** | `search_notes` | Searches unverified owner working notes (`brain/notes/`), tagged `<owner_notes untrusted="true">`. |
| **Untrusted Working Notes** | `get_note` | Reads one working note (`<owner_notes untrusted="true">`); never treated as verified claim. |
| **Untrusted Working Notes** | `extract_action_items` | Extracts checklist/action items from working notes (`<owner_notes untrusted="true">`). |
| **Untrusted Working Notes** | `remember` | Saves an unverified working note (DLP-scanned; secrets/injections route to `0-raw/inbox/`). |
| **Quarantined Intake & Episodic** | `add_to_inbox` | Writes up to 100,000 chars into `0-raw/inbox/` with sticky `mcp_local` provenance & DLP quarantine. |
| **Quarantined Intake & Episodic** | `distill_git_commits` | Extracts DLP-scrubbed commit episodes into `4-experiences/` + quarantined `0-raw/inbox/`. |
| **Quarantined Intake & Episodic** | `record_experience` | Appends create-only episodic record in `4-experiences/`. |
| **Sidecar Cache & Telemetry Only** | `record_context_signal` | Pre-warms the disposable L0 Predictive Memory Bus cache from risk-scanned IDE context signals without mutating canonical stores. |
| **Sidecar Cache & Telemetry Only** | `record_feedback` | Updates disposable sidecar telemetry (`agent_negative_feedback`); cannot deactivate playbooks or alter trust. |
