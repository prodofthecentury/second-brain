# Second Brain — Operations & Recovery Playbook

This playbook documents deterministic installation, daily verification, graceful degradation, and recovery procedures for Second Brain. Every procedure respects the five non-negotiable system invariants:

1. **Durable Storage Invariance:** Canonical Markdown atoms (`brain/2-atoms/*.md`) and immutable source artifacts (`brain/sources/*`, `brain/0-raw/archive/*`) are the sole durable sources of truth. SQLite FTS5 (`brain/.sidecar/lexical.sqlite3`) and ChromaDB (`brain/.chroma/`) are disposable projections.
2. **Deterministic Compare-and-Swap (CAS):** All lifecycle transitions, edits, demotions, and two-atom merges pass through `scripts/atom_mutator.py` (invoked via CLI `scripts/review_atom.py` or Dashboard review controls) under the shared process-level writer lock (`brain/.sidecar/writer.lock`).
3. **Cryptographic Source Anchoring:** `review_status: accepted` requires a verified byte-range match (`evidence_start`..`evidence_end`) and SHA-256 digest (`evidence_sha256`, `evidence_quote_sha256`) against an immutable local source file, attested in `decision-record.jsonl`.
4. **Out-of-Model Epistemic Grounding:** Standard factual answers (`standard`) and owner stylistic guidelines (`preference`) are validated and rendered out-of-model by `scripts/answer_policy.py`. Unsupported claims are excised; empty verified claim sets structurally abstain.
5. **Fail-Closed Security & Loopback Isolation:** Dashboard and MCP interfaces bind strictly to `127.0.0.1` (`403 Forbidden` on wildcard/LAN addresses), enforce fail-closed authentication, and isolate external captures in quarantined raw queues.

---

## 1. Reproducible Setup (Windows, Linux, macOS)

### Windows (PowerShell)
```powershell
# Headless reproducible environment + vault + index setup (never mutates Git state):
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -NoLaunch

# Pull required local Ollama models (nomic-embed-text, llama3.2:3b) and rebuild Chroma vectors:
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -NoLaunch -PullModels -RebuildVectors
```

### Linux & macOS (Bash)
```bash
chmod +x ./setup.sh
./setup.sh --no-launch

# Optional: pull Ollama models and build dense Chroma index:
./setup.sh --no-launch --pull-models --rebuild-vectors
```

### Fresh Clean-Environment Verification
```bash
uv sync --frozen
uv run --frozen python scripts/doctor.py
uv run --frozen second-brain doctor
uv run --frozen python scripts/test_all.py
```

---

## 2. Rebuilding Disposable Indexes from Durable Markdown

Because search and telemetry SQLite databases (`brain/.sidecar/lexical.sqlite3`, `brain/.sidecar/notes.sqlite3`, `brain/.sidecar/telemetry.sqlite3`) and ChromaDB (`brain/.chroma/`) are strictly disposable projections of `brain/2-atoms/*.md` and `brain/notes/*.md`, you can delete and rebuild those index files at any time without losing canonical knowledge or approval attestations.

> **Important Archive Checkpoint Preservation:** Do **not** delete `brain/0-raw/archive/` blindly: `brain/0-raw/archive/vault-ledger.jsonl` (hash-chained Merkle audit checkpoints) and `brain/0-raw/archive/snapshots/*.json` (immutable point-in-time vault snapshots) hold durable historical checkpoint records under the Three-Store boundary, whereas `brain/.sidecar/` SQLite caches can be rebuilt cleanly at any time.

### 2.1 Rebuild SQLite FTS5 Lexical Index (Model-Free / Offline)
```bash
uv run --frozen python scripts/rebuild_lexical_index.py
```
- Acquires `brain/.sidecar/writer.lock`.
- Rebuilds `brain/.sidecar/lexical.sqlite3` atomically via temporary file swap.
- Works 100% offline without Ollama or GPU/C++ vector dependencies.

### 2.2 Rebuild ChromaDB Dense Vector Generation (Staged Swap)
```bash
uv run --frozen python scripts/rebuild_vectors.py
```
- Requires local `ollama serve` on `127.0.0.1:11434` with `nomic-embed-text` installed.
- Builds a brand-new cosine HNSW index in a staging directory, verifies all embeddings and writes `index-manifest.json`, retains up to 2 prior generations in `.previous-*` rollback directories, and atomically activates `brain/.chroma/`.

### 2.3 Recovering from an Interrupted or Corrupted Vector Rebuild
If power loss, process termination, or an Ollama timeout interrupts a vector rebuild:
1. **Immediate Runtime Behavior:** `validated_index_generation()` checks `brain/.chroma/index-manifest.json` against current atom SHA-256 digests and `indexed_count == atom_count`. If `.chroma/` or the manifest is missing or stale, dense retrieval fails closed (`MemoryRecallUnavailable`) and RRF / dense recall **automatically degrades to SQLite FTS5** with an explicit warning banner.
2. **Clean Recovery:**
   ```bash
   uv run --frozen python scripts/rebuild_vectors.py
   uv run --frozen python scripts/doctor.py
   ```
3. **Canary Verification of Staged Swap & Handle Cleanup:**
   ```bash
   uv run --frozen python scripts/chroma_rebuild_canary.py
   ```

---

## 3. Graceful Degradation When Ollama Is Offline

If the local Ollama daemon crashes or is stopped:
- **Lexical (`--mode lexical`), Auto (`--mode auto`), Hybrid RRF (`--mode rrf`), and Dense (`--mode dense`)** continue serving verified owner-approved memories via SQLite FTS5 when the lexical index is present, with zero unhandled exceptions.
- Both CLI (`chat_with_memory.py`) and Dashboard (`dashboard/app.py`) display an explicit `[WARNING]` banner stating that dense embeddings were unreachable and retrieval dynamically degraded to FTS5 lexical search.
- To restore full hybrid RRF + local answer synthesis:
  ```bash
  ollama serve
  uv run --frozen python scripts/doctor.py
  ```

---

## 4. Auditing Source Drift & Demoting Stale Approvals

Canonical approvals bind both the atom's SHA-256 and the anchored source artifact's SHA-256 + byte offsets.

### 4.1 Read-Only Cryptographic Source Audit
```bash
uv run --frozen python scripts/audit_sources.py
```
Inspects every atom in `brain/2-atoms/`, reporting `anchor_status`, `approval_current`, `stored_anchor`, and `current_source_sha256` without mutating any file.

### 4.2 Deterministic CAS Demotion of Stale Accepted Atoms
If an underlying source file was edited or moved after approval:
1. Recall (`should_include_record`) and answer grounding (`answer_policy.py`) **immediately stop serving** the stale atom even before demotion because `approved_revision_is_current()` returns `False`.
2. Demote stale frontmatter cleanly through the CAS mutator and record the decision in `decision-record.jsonl`:
   ```bash
   uv run --frozen python scripts/audit_sources.py --demote-stale --actor <owner-name>
   ```

---

## 5. CAS Lifecycle Mutations, Merges & Rollback Recovery

Never edit `brain/2-atoms/*.md` directly with ad-hoc scripts. Always use `scripts/review_atom.py` (or the Vault Explorer review controls in `dashboard/app.py`, both of which delegate exclusively to `scripts/atom_mutator.py`).

### 5.1 Single-Atom Review Transitions
```bash
# Inspect current SHA-256 first, then run CAS transition by canonical atom filename:
uv run --frozen python scripts/review_atom.py transition approve <atom>.md --expected-sha256 <sha256> --actor <owner>
uv run --frozen python scripts/review_atom.py transition reject <atom>.md --expected-sha256 <sha256> --actor <owner>
uv run --frozen python scripts/review_atom.py transition demote <atom>.md --expected-sha256 <sha256> --actor <owner>
uv run --frozen python scripts/review_atom.py transition quarantine <atom>.md --expected-sha256 <sha256> --actor <owner>
```
- Preference atoms (`epistemic_label: preference`) unconditionally receive `epistemic_contract: owner_subjective_preference` upon approval (any untrusted model-authored `epistemic_contract` is stripped on normalization) and render under `[Preference]` contracts rather than empirical factual claims.

### 5.2 Two-Atom CAS Merge & Terminal Supersession
1. Preview duplicate candidates (read-only, zero mutations):
   - In Dashboard -> **Vault Explorer** -> **Duplicate Suggestions (Read-Only Review Aid)**, or via `recall.suggest_duplicate_atoms()`.
2. Merge two duplicate atoms atomically under dual SHA-256 CAS locks:
   ```bash
   uv run --frozen python scripts/review_atom.py merge \
     <survivor>.md \
     <superseded>.md \
     --survivor-sha256 <survivor-sha256> \
     --superseded-sha256 <superseded-sha256> \
     --actor <owner> \
     --rationale "Duplicate capture of identical architectural rule"
   ```
- Before mutating either file, `atom_mutator.py` writes `.bak` snapshots to `brain/2-atoms/.history/<atom>.<sha256>.bak`.
- If the second write, decision log append, or lexical index refresh fails, both atoms automatically roll back to their exact pre-merge bytes.
- Merged/superseded atoms (`review_status: merged`, `claim_state: superseded`, `superseded_by: [<survivor>]`) are **permanently terminal**: they cannot be edited, demoted, or re-approved, are rejected by `approved_revision_is_current()`, and are excluded across all recall and MCP surfaces.

---

## 6. Security Operations (Dashboard & MCP)

### 6.1 Dashboard Loopback & Password Reset
- Listeners reject non-`127.0.0.1` binds (`0.0.0.0`, `::`, LAN IPs) with `403 Forbidden`.
- Thread-safe cross-session rate limiting locks authentication for 60 seconds after 5 failed attempts.
- To set or rotate the owner password and launch:
  ```bash
  uv run --frozen python scripts/dashboard_auth.py set-password
  uv run --frozen second-brain dashboard
  ```

### 6.2 MCP Fail-Closed Bearer Token & Quarantined Inbox
- MCP is disabled unless `BRAIN_ENABLE_LOCAL_MCP=1` is set and **fails closed** without a configured local bearer token (`>= 16` chars, generated as 64-hex CSPRNG and compared via `hmac.compare_digest`).
- Clients authenticate via one of two constant-time paths (**never** inside tool `arguments`):
  1. **Per-request MCP `_meta` bearer header:** `authorization: Bearer <token>` inside request `_meta`.
  2. **Phase 4 local stdio handshake (`second-brain connect`):** `BRAIN_MCP_TRUSTED_STDIO=1` paired with `BRAIN_MCP_CLIENT_TOKEN=<token>` injected into the local IDE's stdio server environment (`second-brain connect {add,remove,status}`).
- Generate a 256-bit local bearer token (stored with `0600` permissions in `%LOCALAPPDATA%/SecondBrain/security/mcp-token` on Windows or `$XDG_STATE_HOME/SecondBrain/security/mcp-token`, defaulting to `~/.local/state/SecondBrain/security/mcp-token`, on Linux):
  ```bash
  BRAIN_ENABLE_LOCAL_MCP=1 uv run --frozen python scripts/mcp_server.py --generate-token
  ```
- Scope allowed MCP tools via `BRAIN_MCP_ALLOWED_TOOLS` (e.g., `search_memory,get_atom`).
- All `add_to_inbox` captures (and risky/secret `remember` payloads) write exclusively to `brain/0-raw/inbox/` with sticky provenance, path-traversal blocking, null-byte rejection, and a 100,000-character limit. Quarantined inbox captures remain isolated from automatic Refinery runs until explicitly reviewed and promoted by the owner into `brain/0-raw/`.

---

## 7. Production Gate & Evaluation Suite

Run the complete verification gate before any release tag or schema change:
```bash
# 1. Unified 36-suite CI test orchestrator (doctor, dataset schema, source audit, Phase 1-10 safety/cognitive/product/Merkle/release/platform/ecosystem/gen4/formal-discovery/live-runtime suites, 1,500-note scale canary, Chroma canary, archive manifest canary, live smoke test)
uv run --frozen python scripts/test_all.py

# 2. Verify cryptographic Merkle audit ledger, Contradiction Radar & Human Owner Gates
uv run --frozen second-brain ledger verify
uv run --frozen second-brain graph contradictions
uv run --frozen second-brain gates status

# 3. Comparative external SOTA benchmark (--offline dense baseline) & 90-case Retrieval Benchmark
uv run --frozen second-brain sota-benchmark --atoms 300 --offline
uv run --frozen python scripts/evaluate_retrieval.py --output docs/retrieval-evaluation-results-2026-11-22.json

# 4. Multi-run Grounded Answer Evaluation (N >= 5 passes)
uv run --frozen python scripts/evaluate_answers.py --model qwen2.5-coder:7b --runs 5
```
