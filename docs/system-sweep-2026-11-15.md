# System Sweep & Production Release Gate — 2026-11-15

## 1. Executive Verdict

All engineering milestones across Goals 0–16 for the **Second Brain** local pilot and Phase 2 / 3A / 4 product ecosystem are implemented, independently audited, reconciled, and verified at **100% pass rate** (`10/10` test suites in `scripts/test_all.py`: `72/72` tests in `scripts/system_safety_tests.py`, `10/10` tests in `scripts/phase2_safety_tests.py`, `10/10` tests in `scripts/phase3_cognitive_tests.py`, `25/25` tests in `scripts/phase4_product_tests.py`, plus `doctor`, `evaluation_dataset`, `audit_sources`, `scale_benchmark_1500`, `chroma_rebuild_canary`, and `smoke_test`). See [phase2-architecture-and-sweep-2026-11-15.md](phase2-architecture-and-sweep-2026-11-15.md) and [phase3a-and-phase4-architecture-2026-11-15.md](phase3a-and-phase4-architecture-2026-11-15.md) for Phase 2–4 architecture and security audit reconciliations.

Every Non-Negotiable System Invariant holds unconditionally across CLI, Streamlit Dashboard, MCP, Scout, and Refinery entrypoints:

1. **Durable Storage Invariance:** Canonical Markdown atoms (`brain/2-atoms/*.md`) and SHA-256 anchored source artifacts (`brain/sources/*`, `brain/0-raw/*`) remain the sole durable truth. SQLite FTS5 (`brain/.sidecar/lexical.sqlite3`) and ChromaDB (`brain/.chroma`) rebuild deterministically from disk files alone and degrade gracefully to FTS5 when the local embedding daemon is offline.
2. **Deterministic Compare-and-Swap (CAS):** Every atom transition (`approve`, `reject`, `quarantine`, `demote`, `edit`, `merge`) executes exclusively through `scripts/atom_mutator.py` under `writer.lock` with SHA-256 revision pinning, backup snapshots, and append-only `brain/0-raw/archive/decision-record.jsonl` audit records. Legacy root PowerShell bypass scripts (`ollama_refinery.ps1`, `chat_with_memory.ps1`) were replaced with canonical wrappers and locked via regression tests. Merged/superseded atoms are permanently terminal across all mutation actions and cycle-checked via transitive BFS.
3. **Cryptographic Source Anchoring:** Approvals require exact byte-offset substring verification against immutable SHA-256 source files on disk. `scripts/audit_sources.py` continuously audits anchor integrity and demotes stale revisions via CAS.
4. **Out-of-Model Epistemic Grounding:** Standard factual answers (`standard`) and owner preference responses (`preference`) are strictly isolated in `scripts/answer_policy.py`. Raw LLM drafts and model-authored citations are never rendered; every displayed claim must match an exact retrieved, currently approved, byte-anchored atom claim (`100.0%` claim precision, `100.0%` citation grounding, and `0.0%` adversarial trap leakage across `200` total multi-model evaluation runs).
5. **Fail-Closed Security & Hermetic Operation:** MCP requires a minimum 16-character bearer token before any audit/directory write (`scripts/mcp_server.py`), restricts reads strictly to non-superseded current `accepted` atoms, and isolates untrusted captures inside `brain/0-raw/inbox/`. Dashboard binds strictly to `127.0.0.1`, gates vault access behind salted `scrypt` authentication with thread-safe rate-limiting/lockout, enforces `SameSite=Strict` XSRF cookies, and rejects cross-origin HTTP/WebSocket upgrades with HTTP `403 Forbidden`. Scout pins validated public DNS answers, disables redirects and proxies, and blocks loopback, RFC 1918, NAT64 (`64:ff9b::/96`, `64:ff9b:1::/48`), and IPv4-mapped non-global addresses.

---

## 2. Current Repository, Vault & Runtime State

- **Git Hygiene & History Audit (Milestone 1):**
  - Active branch `master` tracks code, lockfiles, tests, installers, and documentation cleanly (`brain/` and `tmp/` remain gitignored).
  - Historical commit `bffca9e` (containing 18 legacy vault files with zero credentials/keys) was audited (`docs/git-history-audit-2026-11-15.md`), and an orphaned history-scrubbed branch `release-code-only` was verified for safe external distribution without rewriting local recovery history. No Git remote is configured (`git remote -v` is empty).
- **Locked Environment:** Python `3.11.15` on Windows x64 (`8` logical threads, `15.9 GiB` RAM, signed MSVC `vcruntime140.dll` verified), locked via `pyproject.toml` and `uv.lock` (`124` packages across core + `dashboard`, `online`, `mcp`, `scout`, `watchdog`, `evaluation`, and `dev` extras).
- **Local Vault & Disposable Projections:**
  - `14` canonical atoms in `brain/2-atoms/`; `2` hold current, verifiable human owner approval (`structured-logging.md` SHA-256 `eced2407...`, `design-for-failure.md` SHA-256 `48a4570a...`). Per Operating Instruction #2 (*Never Fabricate Approvals or Citations*), unreviewed atoms (`sodium-ion-battery-cost-record.md` and pending drafts) remain `pending` until human owner sign-off.
  - Active Chroma cosine generation: `ce48706db17d440fbba04b8c7f86c37e` (`nomic-embed-text`, `768` dimensions, `14/14` atoms indexed).
  - Active SQLite FTS5 lexical index: `brain/.sidecar/lexical.sqlite3` (`current`).
  - Local Ollama models verified: `llama3.2:3b`, `qwen2.5-coder:7b`, `nomic-embed-text:latest` (`llava` remains absent and vision stays disabled).

---

## 3. Milestone 2 & 5 — Retrieval Benchmark & Hybrid RRF Results

Evaluated via `scripts/evaluate_retrieval.py` on curated dataset `second-brain-retrieval-curated-30` (`v2.1.0`, SHA-256 `b54a9babbfd49bb23b81acdc9f5d31b8b02f81a864cbf35efd5db331aa7365a0`, persisted in `docs/retrieval-evaluation-results-2026-11-15.json`):

- **Dataset Composition (30 Curated Cases):**
  - `10` Standard Factual Positives (`answerable`, bound to SHA-256 verified owner-approved revisions).
  - `5` Subjective / Owner Preference Queries (`preference`, contract-isolated via `epistemic_contract: owner_subjective_preference`).
  - `5` Multi-Atom Synthetic / Exploratory Queries (`exploratory`).
  - `10` Adversarial / Personal / Unapproved Trap Queries (`unanswerable`, requiring strict abstention).
- **Partitioning:** `regression` (`8` cases: `5` scored + `3` traps), `development` (`10` cases: `7` scored + `3` traps), `holdout` (`12` cases: `8` scored + `4` traps).

| Metric ($k = 5$) | SQLite FTS5 (`lexical`) | Chroma Cosine (`dense`) | Hybrid RRF (`rrf`) | Target Gate |
| --- | ---: | ---: | ---: | ---: |
| Mean Candidate Recall@5 (Overall, $N=20$) | **1.000** | **1.000** | **1.000** | $\ge 0.95$ |
| Recall@5 — `answerable` ($N=10$) | **1.000** | **1.000** | **1.000** | $\ge 0.95$ |
| Recall@5 — `preference` ($N=5$) | **1.000** | **1.000** | **1.000** | $\ge 0.95$ |
| Recall@5 — `exploratory` ($N=5$) | **1.000** | **1.000** | **1.000** | $\ge 0.95$ |
| Recall@5 — `regression` / `development` / `holdout` | **1.00 / 1.00 / 1.00** | **1.00 / 1.00 / 1.00** | **1.00 / 1.00 / 1.00** | $\ge 0.95$ |
| Trusted Expected-Atom Recall (`answerable`) | **1.000** | **1.000** | **1.000** | $1.00$ |
| Mean MRR | `0.925` | `0.975` | `0.950` | Diagnostic |
| Mean nDCG@5 | `0.9380` | `0.9815` | `0.9631` | Diagnostic |
| Trusted Trap False Positives ($N=10$ traps) | **0 / 10** | **0 / 10** | **0 / 10** | `0` |
| Retrieval Latency P50 / P95 | `6.56 ms / 8.33 ms` | `63.16 ms / 83.68 ms` | `70.06 ms / 89.94 ms` | Host profile |

- **Project Scoping & Hierarchical Links:** `RecallOptions` enforces `project:<id>` namespace isolation across CLI (`--project`, `--allow-cross-project`), Dashboard, and MCP, gating cross-project `[[wiki-link]]` neighbor traversal unless `allow_cross_project=True` is explicitly requested, and providing read-only lexical/jaccard duplicate suggestions (`scripts/recall.py`).
- **Live Graceful Degradation Verification:** Killing the `ollama` daemon mid-session while executing `--retrieval-mode rrf` and `--retrieval-mode dense` cleanly caught `MemoryRecallUnavailable`, emitted `[WARNING] Dense embedding service unavailable ... dynamically degraded to FTS5 lexical retrieval`, and returned exact grounded FTS5 results with zero unhandled exceptions.

---

## 4. Milestone 3 — Grounded Multi-Model Answer Evaluation ($N = 5$)

Executed via `scripts/evaluate_answers.py` (`5` deterministic passes $\times$ `20` scored cases = `100` full end-to-end runs per local model; `50` positive runs + `50` adversarial trap runs each):

| Metric ($N = 5$ passes $\times$ 20 cases = 100 runs/model) | `llama3.2:3b` (Primary 3B) | `qwen2.5-coder:7b` (Mid-Tier 7B) | Invariant Requirement |
| --- | ---: | ---: | ---: |
| **Claim Precision** (`1 - unexpected_displayed_claim_rate`) | **100.0%** (`0` unexpected) | **100.0%** (`0` unexpected) | **100.0%** |
| **Citation Grounding** (`validated_claim_citation_rate`) | **100.0%** (`15/15` cited) | **100.0%** (`47/47` cited) | **100.0%** |
| **Zero Hallucination / Trap Abstention** (`50` trap runs) | **100.0%** (`0/50` leaked) | **100.0%** (`0/50` leaked) | **100.0%** (`0` leaks) |
| Positive Expected Claim Coverage (`50` positive runs) | `30.0%` (`15/50`) | **94.0%** (`47/50`) | Diagnostic |
| Positive End-to-End Latency (P50 / P95) | `10.60 s / 14.92 s` | `28.60 s / 33.09 s` | Host CPU profile |

- **Scope & Hardware Note:** On this CPU-only 16 GiB Windows host, out-of-model grounding in `scripts/answer_policy.py` prevented **100%** of hallucinated claims and trap leaks across both 3B and 7B local models (`0` unverified claims displayed across `200` total runs). Because only two canonical atoms currently carry real human owner approval in the local vault, these metrics validate strict grounding enforcement on the pilot corpus rather than general open-domain recall.

---

## 5. Milestone 4 & 6 — Security Hardening & Independent Adversarial Review

### Security Controls Verified by Live Penetration & Unit Tests
- **MCP Boundary (`scripts/mcp_server.py`):** Unconditionally fail-closed `_meta`-only bearer token verification (`hmac.compare_digest`, $\ge 16$ chars, `0600` atomic token store, startup abort if unconfigured) runs *prior* to `audit_mcp_use()` and `ensure_dirs()`; loopback transport check blocks non-`127.0.0.1` environment variables; `get_atom` and `handle_read_resource` reject `pending`, `rejected`, `quarantined`, `merged`, and `superseded` atoms; `add_to_inbox` writes exclusively to `brain/0-raw/inbox/` (`O_CREAT | O_EXCL`, max `100,000` chars, control-byte & path-traversal sanitization).
- **Dashboard Boundary (`scripts/dashboard_auth.py`, `dashboard/app.py`):** Rejects `None`, `0.0.0.0`, and non-loopback binds (`assert_loopback_address`); enforces salted `scrypt` password verification, 30-minute idle timeout, manual lock, and thread-safe cross-session rate-limiting (`5` failures -> `60s` lockout); live penetration test (`test_non_loopback_binding_and_cross_origin_penetration_rejected`) launches Tornado/Streamlit and confirms `Origin: https://evil-attacker.example` WebSocket upgrades and forged `Host` headers receive `HTTP 403 Forbidden`. Untrusted vault/briefing/inbox previews render via `st.code` rather than raw Markdown to prevent outbound image beacons.
- **Independent Adversarial Audit (`docs/independent-review-2026-11-15.md`):** Isolated read-only auditor `2a4e3faa-6244-4b81-8c9d-2b6e8a184782` performed a full-repo review. All `1` Critical, `3` High, `4` Medium, `4` Low, and `3` Informational findings were reconciled in code/docs and locked with dedicated regression tests (`IndependentAuditReconciliationTests` in `scripts/system_safety_tests.py`).

---

## 6. Verification Ledger (2026-11-15)

- `uv sync --reinstall-package second-brain --frozen` — **PASS** (`124` packages locked and verified).
- `uv run --frozen python scripts/test_all.py --json-output brain/.sidecar/test-all-latest.json` — **10/10 SUITES PASS (`100% PASS` in `81.01s`)**:
  1. `doctor` (`scripts/doctor.py`) — **PASS** (`4.20s`, `0` `FAIL` checks).
  2. `evaluation_dataset` (`scripts/evaluation_dataset.py`) — **PASS** (`0.25s`, `30` cases across `regression`/`development`/`holdout`, SHA-256 revision bindings verified).
  3. `audit_sources` (`scripts/audit_sources.py`) — **PASS** (`0.71s`, all approved source byte offsets & SHA-256 hashes verified).
  4. `system_safety_tests` (`scripts/system_safety_tests.py`) — **PASS** (`22.85s`, `72` tests).
  5. `phase2_safety_tests` (`scripts/phase2_safety_tests.py`) — **PASS** (`4.21s`, `10/10` tests; see [phase2-architecture-and-sweep-2026-11-15.md](phase2-architecture-and-sweep-2026-11-15.md)).
  6. `phase3_cognitive_tests` (`scripts/phase3_cognitive_tests.py`) — **PASS** (`5.43s`, `10/10` tests; see [phase3a-and-phase4-architecture-2026-11-15.md](phase3a-and-phase4-architecture-2026-11-15.md)).
  7. `phase4_product_tests` (`scripts/phase4_product_tests.py`) — **PASS** (`8.56s`, `25/25` tests; see [phase3a-and-phase4-architecture-2026-11-15.md](phase3a-and-phase4-architecture-2026-11-15.md)).
  8. `scale_benchmark_1500` (`scripts/scale_benchmark.py`) — **PASS** (`3.38s`, 1,500-note synthetic corpus scale canary).
  9. `chroma_rebuild_canary` (`scripts/chroma_rebuild_canary.py`) — **PASS** (`2.13s`, real ChromaDB staged rebuild & cleanup canary).
  10. `smoke_test` (`scripts/smoke_test.py`) — **PASS** (`29.30s`, end-to-end vault intake, CAS, grounding, lexical & dense recall verified).
- `powershell -ExecutionPolicy Bypass -File .\setup.ps1 -NoLaunch` — **PASS** (exit code `0`; verified OS/RAM/CPU/MSVC/GPU inspection, locked `uv` sync, Ollama check, vault/FTS5 init, `second-brain doctor`, and `scripts/doctor.py` with zero Git mutations).
