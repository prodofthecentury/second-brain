# Second Brain — Local AI Memory for VS Code, Cursor & Windsurf

Local-first, cryptographically verifiable AI memory right inside VS Code, Cursor, and Windsurf. Every query runs over your loopback `127.0.0.1` Memory API — keeping working notes, SHA-256 anchored Verified Atoms, Skill Playbooks, Merkle inclusion proofs, and Time-Travel Snapshots on your machine.

---

## Prerequisites & Local API Setup

Start the local Second Brain Loopback API using **either**:

1. **Second Brain Desktop App** (recommended): runs in your OS system tray, manages the loopback API sidecar automatically, and provides owner review, vault switching, and one-click IDE MCP setup.
2. **CLI Server**:
   ```bash
   second-brain serve
   # or from a source checkout:
   uv run --frozen python scripts/memory_api.py --port 8765 --ensure-tokens
   ```

### Authentication & Token Discovery

The extension authenticates automatically over `127.0.0.1` using:
1. `BRAIN_MEMORY_API_TOKEN` environment variable (if set),
2. `BRAIN_MEMORY_API_TOKEN_FILE` (verified regular non-symlink file `<= 4 KB`), or
3. Your OS security directory (`%LOCALAPPDATA%\SecondBrain\security\memory-api-token` on Windows, `$XDG_STATE_HOME/SecondBrain/security/memory-api-token` or `~/.local/state/SecondBrain/security/memory-api-token` on macOS/Linux).

Owner-gated actions invoked locally (such as `Create Vault Snapshot`) read `owner-token` from `BRAIN_OWNER_TOKEN_FILE` or the same `SecondBrain/security/` directory when present.

---

## Features

### 1. Status Bar Health Indicator
- Displays `$(shield) Second Brain: <N> verified` (or `offline` when the loopback server is stopped) in the right status bar.
- Polls `/v1/health` on startup and Vault refresh.
- **Click** the status bar item to launch **Quick Recall** (or press `Enter` on an empty search box to jump straight into **Streaming Chat**).

### 2. Interactive Activity Bar `Vault Explorer`
Open the **Second Brain** book icon in the Activity Bar to browse five live sections (with header toolbar buttons for **Open Streaming Chat** and **Refresh Vault**):
- **Notes**: Click any working note to open its Markdown body and backlinks in a preview tab.
- **Verified Atoms**: Click any approved atom to inspect its Claim, Anchored Evidence, Provenance Sources, and live **Merkle Inclusion Proof**.
- **Playbooks**: Click any promoted Skill Playbook to open an operational checklist Markdown preview (Trigger, Preconditions, Steps, Expected Outputs, Rollback).
- **Snapshots**: Click any immutable snapshot to run a **Semantic Diff vs Live Vault** and view Added, Removed, and Modified atoms/playbooks.
- **Contradiction & Drift Radar**: Inspect polarity/numeric conflicts, source anchor drift alerts, and orphan approved atoms.

### 3. Native-Themed Grounded Streaming Chat
- Supports **Stream** (token-by-token synthesis with post-stream citation verification), **Verified** (exact Claim-ID selection with source quotes), and **Instant** (`<20ms` lexical retrieval without invoking a model).
- Displays `[V#]` Verified Atom, `[N#]` Working Note, and `[P#]` Owner Preference source cards in every mode, plus a **Grounding Audit** banner highlighting cited sources, invalid citations, or uncited sentences.
- Adapts automatically to VS Code Light, Dark, and High-Contrast themes under a strict nonce-only Content Security Policy.

---

## Commands & Keybindings

| Command | Shortcut / Menu | Description |
| :--- | :--- | :--- |
| `Second Brain: Remember Selection` | `Ctrl+Alt+M` (`Cmd+Alt+M` on macOS) / Editor right-click menu | Risk-scans and saves selected code or prose to Second Brain (`/v1/remember`). |
| `Second Brain: Quick Recall` | `Ctrl+Alt+R` (`Cmd+Alt+R` on macOS) / Status Bar click | Fuzzy search across Verified Atoms and Working Notes with instant Markdown preview. |
| `Second Brain: Open Streaming Chat` | Vault Explorer title bar | Opens the side-by-side Grounded Chat webview (`Stream`, `Verified`, `Instant`, `Warmup`, `project_scope`). |
| `Second Brain: Assemble Executive Plan` | Command Palette | Generates a formatted Markdown Executive Plan grounded in verified Playbooks and Atoms. |
| `Second Brain: Verify Vault Merkle Ledger` | Command Palette | Verifies hash-chain integrity, approved atom/playbook counts, and live Merkle root sync. |
| `Second Brain: Prove Verified Atom Membership` | Command Palette | Generates a cryptographic Merkle inclusion proof and sibling path table for an approved atom. |
| `Second Brain: Scan Contradiction & Drift Radar` | Command Palette | Scans active atoms for polarity/numeric contradictions, source drift, and orphan nodes. |
| `Second Brain: Create Vault Snapshot` | Command Palette | Captures an immutable point-in-time snapshot of approved atoms and playbooks. |
| `Second Brain: Diff Snapshot vs Live Vault` | Command Palette | Computes a semantic diff between any historical snapshot and the live vault. |
| `Second Brain: Record Episodic Experience` | Command Palette | Logs a structured Layer-4 task experience (`goal`, `result_summary`, `success`, `lessons`). |
| `Second Brain: Refresh Vault` | Vault Explorer title bar | Refreshes the Vault Explorer tree and Status Bar health indicator. |

---

## Extension Settings

- `secondBrain.apiPort` (`number`, default `8765`): Loopback TCP port (`127.0.0.1`) of your local Second Brain HTTP API.
