# Second Brain — Local AI Memory for VS Code, Cursor & Windsurf

Local-first, cryptographically verifiable AI memory right inside VS Code, Cursor, and Windsurf. Every query and owner governance workflow runs strictly over your loopback `127.0.0.1` Memory API — keeping editable working notes, SHA-256 anchored Verified Atoms, Quarantined Inbox reviews, Skill Playbooks, Merkle inclusion proofs, and Time-Travel Snapshots on your machine.

---

## Prerequisites & Local API Setup

Start the local Second Brain Loopback API using **either**:

1. **Second Brain Desktop App** (recommended): runs in your OS system tray, launches the loopback API sidecar on a live ephemeral or configured port, writes `SecondBrain/security/api-port`, and provides owner review, vault switching, and one-click IDE MCP setup.
2. **CLI Server**:
   ```bash
   second-brain serve
   # or from a source checkout:
   uv run --frozen python scripts/memory_api.py --port 8765 --ensure-tokens
   ```

### Port Auto-Discovery & Authentication

- **Port Auto-Discovery**: When `secondBrain.apiPort` is not explicitly overridden in user/application settings, the extension reads `SecondBrain/security/api-port` (or `BRAIN_API_PORT_FILE` / `BRAIN_SECURITY_STATE_DIR/api-port`), verifies the recorded `pid` is alive, and connects to `127.0.0.1:<port>` — falling back to `8765` and re-discovering automatically on connection errors if the sidecar restarts.
- **Bearer Token (`Authorization: Bearer <token>`)**: Discovered from `BRAIN_MEMORY_API_TOKEN`, `BRAIN_MEMORY_API_TOKEN_FILE`, or `SecondBrain/security/memory-api-token` (`api-token` also supported). All security files are verified via `fs.lstatSync` as regular non-symlink files `<= 4096` bytes inside a non-symlink directory.
- **Owner Token (`X-Owner-Token`)**: Owner governance actions (note creation/CAS edits/verification, Quarantined Inbox promotion/rejection, Atom review/merge, Playbook promotion/deprecation, Ledger commits, and Vault Snapshots) read the owner token from `BRAIN_OWNER_API_TOKEN` / `BRAIN_OWNER_TOKEN`, `secondBrain.ownerTokenPath` (application-scoped), `BRAIN_OWNER_TOKEN_FILE`, or `SecondBrain/security/owner-token`.

---

## Features

### 1. Status Bar Health Indicator
- Displays `$(pulse) Second Brain (<N>)` when online (where `<N>` is the approved atom count), `$(pulse) Second Brain: offline` when the loopback server is stopped, or `$(pulse) Second Brain: auth/error` on authentication errors.
- Polls `/v1/health` on startup, every 30 seconds, and on Vault refresh.
- **Click** the status bar item to launch **Quick Recall** (or press `Enter` on an empty search box to jump straight into **Streaming Chat**).

### 2. Interactive Activity Bar `Vault Explorer` (with Pagination & Context Governance)
Open the **Second Brain** book icon in the Activity Bar to browse seven live sections (with header toolbar buttons for **Create Working Note**, **Open Streaming Chat**, and **Refresh Vault**, plus `"Load More..."` pagination across Notes, Quarantined Inbox, Review Queue, Verified Atoms, and Playbooks):
- **Notes**: Click any working note to open a real editable Markdown buffer (`secondbrain-note:/<filename>`) backed by CAS `PUT /v1/notes/<fn>` (`expected_sha256`) on `Ctrl+S` / `Cmd+S`, with interactive `409` conflict resolution (`Overwrite Remote`, `Reload Remote`, `Cancel`).
- **Quarantined Inbox**: Preview untrusted captures (`GET /v1/inbox/<fn>`), promote clean or risk-acknowledged items (`Promote Quarantined Inbox Item`), or reject unwanted captures.
- **Review Queue**: Inspect pending candidate atoms and run one-click `Approve`, `Reject`, `Quarantine`, or two-atom CAS `Merge`.
- **Verified Atoms**: Inspect approved atoms with their Claim, Anchored Evidence, Provenance Sources, and live **Merkle Inclusion Proof**, or `Demote` / `Quarantine` / `Merge`.
- **Playbooks**: Open operational Skill Playbook checklists, generate Merkle proofs for `5-playbooks/<pb>.md` leaves, promote procedural candidates from grounding-valid `grounding_units`, or deprecate retired playbooks.
- **Snapshots**: Run **Diff Snapshot vs Live Vault**, **Recall at Historical Snapshot**, or **Verify Snapshot Integrity**.
- **Contradiction & Drift Radar**: Inspect polarity/numeric conflicts, source anchor drift alerts, and orphan approved atoms.

### 3. Hardened Singleton Streaming Chat Webview
- **Singleton Panel & Instant Stream Cancellation**: Reuses and reveals a single chat panel; automatically aborts active SSE requests (`req.destroy()`) on re-submit, **Stop** button click, or panel disposal.
- **Full Retrieval & Governance Controls**: Configure `mode` (`stream`, `verified`, `instant`), `retrieval_mode` (`auto`, `lexical`, `dense`, `rrf`), `answer_view` (`standard`, `preference`, `research`), `host_id`, `model_override`, `project_scope`, and `include_preferences`.
- **XSS-Safe Rich DOM Markdown & Interactive Citations**: Renders headings, bold/italic, inline and fenced code blocks, tables, lists, task lists, safe `http`/`https` external links, clickable source cards, and inline `[V#]`/`[N#]`/`[P#]`/`[L#]` citation badges that open the target Note or Atom directly in VS Code.
- **Explicit Research Lead Quarantine Banner**: Renders `research_leads` inside a visually distinct `"UNVERIFIED LEADS — NOT OWNER-APPROVED"` block under a strict per-panel nonce Content Security Policy.

---

## Commands & Keybindings

| Command | Shortcut / Menu | Description |
| :--- | :--- | :--- |
| `Second Brain: Remember Selection` | `Ctrl+Alt+M` (`Cmd+Alt+M` on macOS) / Editor right-click menu | Risk-scans and saves selected code or prose to Second Brain (`/v1/remember`). |
| `Second Brain: Quick Recall` | `Ctrl+Alt+R` (`Cmd+Alt+R` on macOS) / Status Bar click | Search across Verified Atoms and Working Notes (or press `Enter` on empty input to open Chat). |
| `Second Brain: Open Streaming Chat` | Vault Explorer title bar | Opens or reveals the singleton Grounded Streaming Chat webview. |
| `Second Brain: Create Working Note` | Vault Explorer title bar / Command Palette | Creates a new working note (`POST /v1/notes`) and opens it in an editable `secondbrain-note:` tab. |
| `Second Brain: Edit Working Note` | Note inline action / Command Palette | Opens a working note in an editable `secondbrain-note:` editor with CAS `expected_sha256` save. |
| `Second Brain: Delete Working Note` | Note inline action / Command Palette | Deletes a working note (`DELETE /v1/notes/<fn>`) with CAS `expected_sha256`. |
| `Second Brain: Verify Note Excerpt as Atom` | Editor right-click menu / Note context menu / Command Palette | Anchors a selected note excerpt into a Verified Atom (`POST /v1/notes/<fn>/verify`). |
| `Second Brain: Promote Quarantined Inbox Item` | Inbox inline action / Command Palette | Inspects `risk_flags`, prompts for explicit risk acknowledgement when needed, and promotes (`POST /v1/inbox/promote`). |
| `Second Brain: Reject Quarantined Inbox Item` | Inbox inline action / Command Palette | Rejects a quarantined inbox capture (`POST /v1/inbox/reject`). |
| `Second Brain: Approve Pending Atom` | Review Queue inline action / Command Palette | Approves a pending atom (`POST /v1/atoms/<fn>/approve`). |
| `Second Brain: Reject Atom` | Review Queue inline action / Command Palette | Rejects a pending atom (`POST /v1/atoms/<fn>/reject`). |
| `Second Brain: Demote Verified Atom` | Verified Atom context menu / Command Palette | Demotes an approved atom back to review (`POST /v1/atoms/<fn>/demote`). |
| `Second Brain: Quarantine Atom` | Atom context menu / Command Palette | Quarantines an atom (`POST /v1/atoms/<fn>/quarantine`). |
| `Second Brain: Merge Duplicate Atoms` | Atom context menu / Command Palette | Performs a two-atom CAS merge (`POST /v1/atoms/merge`) recording survivor, superseded, and rationale. |
| `Second Brain: Promote Skill Playbook` | Command Palette | Promotes procedural atom candidates using exact `grounding_units` steps (`POST /v1/playbooks/promote`). |
| `Second Brain: Deprecate Skill Playbook` | Playbook context menu / Command Palette | Deprecates an active playbook with CAS `expected_sha256` and rationale (`POST /v1/playbooks/<fn>/deprecate`). |
| `Second Brain: Assemble Executive Plan` | Command Palette | Generates a grounded Executive Plan (`deterministic`/`model_assisted`, `auto`/`lexical`/`dense`/`rrf`, `include_preferences`, `project_scope`). |
| `Second Brain: Verify Vault Merkle Ledger` | Command Palette | Verifies hash-chain integrity, approved atom/playbook counts, and live Merkle root sync (`GET /v1/ledger/verify`). |
| `Second Brain: Commit Vault Merkle Ledger Checkpoint` | Command Palette | Commits a signed/audited Merkle root checkpoint (`POST /v1/ledger/commit`). |
| `Second Brain: Prove Verified Atom or Playbook Membership` | Atom & Playbook context menus / Command Palette | Generates a cryptographic Merkle inclusion proof for `<atom>.md` or `5-playbooks/<pb>.md`. |
| `Second Brain: Scan Contradiction & Drift Radar` | Command Palette | Scans active atoms for polarity/numeric contradictions, source drift, and orphan nodes. |
| `Second Brain: Create Vault Snapshot` | Command Palette | Captures an immutable point-in-time snapshot of approved atoms and playbooks (`POST /v1/snapshots`). |
| `Second Brain: Diff Snapshot vs Live Vault` | Snapshot context menu / Command Palette | Computes a semantic diff between any historical snapshot and the live vault (`POST /v1/snapshots/diff`). |
| `Second Brain: Recall at Historical Snapshot` | Snapshot context menu / Command Palette | Queries verified memory as it existed at a historical snapshot (`POST /v1/snapshots/recall`). |
| `Second Brain: Verify Snapshot Integrity` | Snapshot context menu / Command Palette | Verifies leaf hashes and Merkle root integrity of a saved snapshot (`POST /v1/snapshots/verify`). |
| `Second Brain: Record Episodic Experience` | Command Palette | Logs a structured Layer-4 task experience with full telemetry and optional Quarantined Inbox procedure proposal (`POST /v1/cognitive/experiences`). |
| `Second Brain: Refresh Vault` | Vault Explorer title bar | Resets pagination and refreshes the Vault Explorer tree and Status Bar health indicator. |

---

## Extension Settings

Both settings are strictly `application`-scoped (restricted in untrusted workspaces so malicious workspace configs cannot redirect API traffic or token paths):

- `secondBrain.apiPort` (`number`, default `8765`, scope `application`): Explicit loopback TCP port override on `127.0.0.1`. When unset in user/application settings, the extension auto-discovers the live port from `SecondBrain/security/api-port` (or `BRAIN_API_PORT_FILE`) and falls back to `8765`.
- `secondBrain.ownerTokenPath` (`string`, default `""`, scope `application`): Optional explicit path to the local Second Brain owner token file (`<= 4096` bytes, non-symlink).
