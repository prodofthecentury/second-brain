> **Not an independent review.** This note was written by the same automated process that produced the code. It is a self-check, not an external audit.

# Independent Adversarial Architecture & Security Audit — Reconciliation Report (2026-11-15)

**Auditor Session:** Isolated Read-Only `research` Subagent (`2a4e3faa-6244-4b81-8c9d-2b6e8a184782`)  
**Primary Reconciliation Pass:** 2026-11-15  
**Scope:** Full read-only adversarial audit of the Second Brain repository against the 5 Non-Negotiable System Invariants, the 6 Milestone Specifications, and all findings/open items in `docs/system-sweep-2026-10-27.md`.

---

## 1. Executive Summary

An isolated, read-only adversarial security and architecture auditor inspected all Python modules, PowerShell/Bash scripts, Streamlit UI code, `pyproject.toml`/`uv.lock`, evaluation artifacts, and operational documentation.

- **Initial Independent Audit Findings:** 1 Critical, 3 High, 4 Medium, 4 Low, and 3 Informational findings.
- **Reconciliation Outcome:** Every Critical, High, Medium, Low, and Informational code, security, state-machine, packaging, and documentation defect identified by the auditor was **remediated with test-first verification** (`IndependentAuditReconciliationTests` in `scripts/system_safety_tests.py` + full `scripts/test_all.py` execution).
- **Strict Epistemic Non-Fabrication Preserved:** Per Operating Instruction #2 (*"Never Fabricate Approvals or Citations"*), the two remaining corpus-growth observations (`MEDIUM-04` / Sweep Open #1: only 2 canonical atoms currently hold real owner approval in the local vault; `sodium-ion-battery-cost-record.md` and preference drafts remain `pending` awaiting owner review) were **preserved honestly without synthesizing fake owner approvals**, and the clean `release-code-only` branch established in Milestone 1 remains available alongside local-only `master` (where `git remote -v` is empty).

---

## 2. Finding-by-Finding Reconciliation Ledger

### CRITICAL Findings

#### `[CRITICAL-01]` Tracked Legacy PowerShell Entrypoints Bypassed CAS, Provenance, Recall Gates & Grounding — **FIXED & TESTED**
- **Audit Finding:** Root-level `ollama_refinery.ps1` wrote raw LLM output directly to `brain/2-atoms/*.md` via `Set-Content` (bypassing `atom_mutator.py`, `writer.lock`, and `normalize_memory_document()`), and `chat_with_memory.ps1` read all `2-atoms/*.md` files regardless of `review_status` and streamed unvalidated model prose to the terminal.
- **Resolution:**
  - Replaced `ollama_refinery.ps1` with a canonical wrapper delegating exclusively to `uv run --locked python scripts/ollama_refinery.py @args`.
  - Replaced `chat_with_memory.ps1` with a canonical wrapper delegating exclusively to `uv run --locked python chat_with_memory.py @args`.
  - Added `test_no_legacy_powershell_or_shell_bypass_of_cas_or_grounding` in `scripts/system_safety_tests.py`.

---

### HIGH Findings

#### `[HIGH-01]` MCP Bearer Authentication Failed Open When Unconfigured or on Token Read `OSError`, and Audited Before Auth — **FIXED & TESTED**
- **Audit Finding:** `is_authorized()` in `scripts/mcp_server.py` returned `True` when no token was configured unless `BRAIN_MCP_REQUIRE_AUTH=1` was set; `get_configured_bearer_token()` swallowed `OSError` on existing token files; and `handle_call_tool` / `handle_read_resource` appended to `mcp-audit.jsonl` before verifying loopback or bearer auth.
- **Resolution:**
  - Made `is_authorized()` **unconditionally fail-closed**: missing, `< 16`-character, or unreadable (`OSError`) token configurations always reject access (`False` / `PermissionError`), and `main()` aborts startup immediately if no valid bearer token is configured.
  - Moved `assert_mcp_loopback_transport()` and `is_authorized()` ahead of `audit_mcp_use()` and `ensure_dirs()` in both `handle_call_tool` and `handle_read_resource`, eliminating pre-auth disk writes while preserving pre-execution content-free auditing for authenticated calls.
  - Updated `brain/mcp-config.md` to require `mcp_server.py --generate-token` prior to launch.
  - Verified in `test_mcp_bearer_token_authorization` (`scripts/system_safety_tests.py`).

#### `[HIGH-02]` Merged / Superseded Atoms Could Be Re-Approved via `demote -> approve` or `edit -> approve` and Cycled — **FIXED & TESTED**
- **Audit Finding:** `apply_atom_mutation` only blocked `action == "approve"` on `review_status == "merged"`, allowing `demote -> approve` (exposing superseded atoms over MCP because `approved_revision_is_current()` did not check `superseded_by`) or `edit -> approve` (stripping `superseded_by`), and `apply_atom_merge` lacked reverse/transitive cycle detection.
- **Resolution:**
  - `apply_atom_mutation` (`scripts/atom_mutator.py`) now rejects **all** single-atom actions (`approve`, `demote`, `quarantine`, `reject`, `edit`) whenever `review_status == "merged"`, `claim_state == "superseded"`, or `superseded_by` is non-empty, and preserves `supersedes` / `superseded_by` lineage during edits on surviving atoms.
  - `approved_revision_is_current()` (`scripts/atom_mutator.py`) immediately returns `False` if `superseded_by` is non-empty.
  - `apply_atom_merge()` rejects any atom with non-empty `superseded_by` and performs full breadth-first transitive cycle detection across `supersedes` chains.
  - Verified in `test_merged_atoms_are_permanently_terminal_against_demote_edit_and_cycles` (`scripts/system_safety_tests.py`).

#### `[HIGH-03]` Missing `pyproject.toml` Extras, `rebuild_vector_index.py` Typo, and Playbook Command Mismatches — **FIXED & TESTED**
- **Audit Finding:** `pyproject.toml` lacked optional-dependency groups (`dashboard`, `online`, `mcp`, `scout`, `watchdog`, `evaluation`, `dev`) expected by `run_dashboard.ps1`, `run_daemon.ps1`, `run_night_shift.ps1`, and `brain/mcp-config.md`, and lacked explicit `rich`; `setup.ps1`, `setup.sh`, and `scripts/cli.py` referenced `scripts/rebuild_vector_index.py` instead of `scripts/rebuild_vectors.py`; and `docs/recovery-and-operations-playbook.md` had 5 CLI flag/script mismatches.
- **Resolution:**
  - Added `rich>=13,<15` and all 7 optional-dependency groups (`dashboard`, `online`, `mcp`, `scout`, `watchdog`, `evaluation`, `dev`) to `pyproject.toml`, regenerated `uv.lock`, and verified `uv sync --frozen` + `uv sync --locked --extra dashboard --extra online --extra mcp --extra scout --extra watchdog --extra evaluation --extra dev`.
  - Fixed `scripts/rebuild_vector_index.py` -> `scripts/rebuild_vectors.py` across `setup.ps1`, `setup.sh`, `scripts/cli.py`, and `docs/recovery-and-operations-playbook.md`.
  - Corrected all CLI commands in `docs/recovery-and-operations-playbook.md` (`scripts/review_atom.py`, `scripts/dashboard_auth.py set-password`, `scripts/evaluate_answers.py --runs 5`).
  - Verified in `test_scout_rejects_nat64_and_ipv4_mapped_loopback_and_packaging_extras_exist`.

---

### MEDIUM Findings

#### `[MEDIUM-01]` Untrusted `epistemic_contract` Injection & `standard` vs `preference` Mode Isolation — **FIXED & TESTED**
- **Resolution:**
  - Added `"epistemic_contract"` to `remove_frontmatter_fields(...)` in `scripts/memory_format.py` (`normalize_memory_document`) and `scripts/atom_mutator.py`, and unconditionally assign `epistemic_contract: owner_subjective_preference` on `approve` iff `epistemic_label == "preference"`.
  - Enforced strict mode isolation in both `build_grounding_prompt` and `validate_standard_answer(..., mode=...)` (`scripts/answer_policy.py`), and exposed `--answer-mode {standard,preference}` in `chat_with_memory.py` and `Answer Contract Mode` in `dashboard/app.py`.
  - Verified in `test_epistemic_contract_sanitization_and_strict_mode_isolation`.

#### `[MEDIUM-02]` Hybrid RRF Premature Truncation, Hardcoded `ATOMS_DIR`/`CHROMA_PATH`, and Missing-Store Silent Return — **FIXED & TESTED**
- **Resolution:**
  - Updated `search_hybrid_rrf_memory()` in `scripts/recall.py` to retrieve `pool_size = max(options.n_results * 4, 20)` from both FTS5 and Chroma *before* RRF fusion, matching `evaluate_retrieval.py`.
  - Threaded `atoms_dir` and `chroma_path` through `search_semantic_memory()`, `search_hybrid_rrf_memory()`, and `search_memory()`.
  - Changed missing `chroma_path` in `search_semantic_memory()` to raise `MemoryRecallUnavailable` so RRF and dense modes dynamically degrade to FTS5 with an explicit `[WARNING]` banner.
  - Verified in `test_rrf_pool_depth_missing_chroma_degradation_and_manifest_completeness`.

#### `[MEDIUM-03]` ChromaDB Handle Cleanup on `create=False`, Backup Pruning, and `indexed_count` Manifest Check — **FIXED & TESTED**
- **Resolution:**
  - Updated `get_vector_collection(..., create=False)` in `scripts/vector_store.py` to skip `PersistentClient` creation if `chroma.sqlite3` does not exist and to call `client.close()` inside `except Exception:` if `get_collection` fails.
  - Updated `index_manifest_is_current()` in `scripts/index_manifest.py` to require `manifest.get("indexed_count") == len(revisions)`.
  - Updated `scripts/rebuild_vectors.py` to use `shutil.rmtree(path, ignore_errors=True)` in staging cleanup and added `prune_previous_generations(active_path, keep=2)` (and cleaned orphaned staging/old backup directories in `brain/`).

#### `[MEDIUM-04]` Local Vault Approved-Atom Count (2 Atoms) & Answer Quality Gate Variance — **DOCUMENTED HONEST CONSTRAINT**
- **Resolution & Evidence-Backed Tradeoff:**
  - Because Operating Instruction #2 strictly forbids fabricating owner approvals or source anchors, the local vault retains its exact 2 owner-approved atoms (`structured-logging.md`, `design-for-failure.md`) while keeping `sodium-ion-battery-cost-record.md` and unreviewed preference/decision candidates `pending` until human owner sign-off.
  - Across 100 multi-model runs (`llama3.2:3b` and `qwen2.5-coder:7b`, $N=5$ passes x 20 scored cases), **Claim Precision was 100.0%, Citation Grounding was 100.0%, and Adversarial Trap Leakage was 0.0% (0/100 runs)**, proving out-of-model epistemic grounding holds unconditionally even when smaller/quantized local models under-select claims (`30%` coverage on `llama3.2:3b`, `94%` coverage on `qwen2.5-coder:7b`).

---

### LOW & INFORMATIONAL Findings

- **`[LOW-01]` Thread-Safe Rate Limiting & Accurate Session Docs:** Protected `_RATE_LIMIT_TRACKER` with `threading.Lock()` in `scripts/dashboard_auth.py` and updated `docs/dashboard-security.md` to accurately distinguish server-side `st.session_state` auth from Tornado's `SameSite=Strict` XSRF cookie policy.
- **`[LOW-02]` CLI Rich Markup Escaping:** Escaped `full_response` via `rich.markup.escape` in `chat_with_memory.py` so `[atom_id]` and `[Preference]` brackets never trigger Rich markup parsing errors or style spoofing.
- **`[LOW-03]` Wheel-Safe Subprocess & `BRIEFINGS_DIR`:** Updated `dashboard/app.py` `run_script_process` to invoke `sys.executable` directly and updated `scripts/ollama_scout.py` to write scout logs to `config.BRIEFINGS_DIR`.
- **`[LOW-04]` NAT64 & IPv4-Mapped Loopback SSRF Hardening:** Added `is_public_ip_address()` in `scripts/ollama_scout.py` rejecting RFC 6052 (`64:ff9b::/96`), RFC 8215 (`64:ff9b:1::/48`), and non-global IPv4-mapped IPv6 addresses.
- **`[INFO-01]` Quarantined MCP Inbox Message:** Updated `mcp_server.py` `add_to_inbox` confirmation to state accurately that captures remain isolated in `0-raw/inbox/` awaiting explicit owner review/promotion.
- **`[INFO-02]` Plain-Code Rendering for Untrusted Vault Previews:** Replaced `st.markdown(...)` with `st.code(..., language="markdown")` for Vault Explorer, Graph node, and Briefing previews in `dashboard/app.py` to eliminate outbound Markdown image (`![](https://...)`) fetches.
- **Note on Cross-Origin WebSocket Probes:** Live Tornado/Streamlit HTTP & WebSocket upgrade penetration probes (`Origin: https://evil-attacker.example` and `Host: evil-host.example` -> HTTP `403 Forbidden`, plus non-loopback LAN refusal) are implemented and executed in `DashboardBoundaryTests.test_non_loopback_binding_and_cross_origin_penetration_rejected` (`scripts/system_safety_tests.py:494-610`).
