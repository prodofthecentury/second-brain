# Task Queue: Dependable Local Brain Pilot

This queue is reconciled with current code and
`docs/system-sweep-2026-10-27.md`. Full outcomes, existing behavior, changes,
acceptance criteria, dependencies, verification, and likely files are in
`tasks/plan.md`. Keep task IDs and status aligned between the two files.

## Verified foundations — complete

- [x] Durable Markdown atoms, source archive manifest, and rebuildable indexes
  are implemented; smoke and archive/vector canaries are recorded in the sweep.
- [x] Review listing and owner transitions `approve`, `reject`, `quarantine`,
  and `demote` use the canonical CAS mutator, exact source anchors for
  approvals, history backups, audit records, and index refresh. Edit/review
  conflict and re-gating checks exist. Two-atom merge/supersession and the
  source-change review workflow remain open under Goal 3.
- [x] Shared structured recall contract is in `scripts/recall.py` and used by
  CLI, dashboard, and MCP. It returns `RecallResult` records and linked
  neighbors and supports status/context filters in `RecallOptions`. Caller
  controls, project scopes, complete match explanations, and duplicate
  suggestions remain open under Goal 6.
- [x] Native Chroma is working on this host; dense and lexical candidate
  ranking, review evidence, Scout pinning, and Git/tmp inventory are recorded
  in the current system sweep. The five positive labels include pending target
  atoms, so they are exploratory retrieval diagnostics only; they do not count
  as positive approved-answer cases.
- [x] The approved logging paraphrase and failure-handling query survive the
  Claim+Evidence overlap filter; both current trap queries return zero trusted
  results. CLI/dashboard validate model-selected exact claims against current
  approval and source anchors, and abstain without approved context.

## Ordered active goals

### Goal 0 — Reconcile roadmap with implementation — complete

- [x] Compare every unchecked item in the old queue with current source and
  sweep; replace stale Task 5/6 descriptions with actual remaining work.
- [x] Put the next goal set into `tasks/plan.md` and this queue in dependency
  order; move installer and multi-host adapters to deferred product horizon.
- [x] Verification: plan and queue task IDs, states, order, and next task agree.

### Goal 1 — Curate reviewed examples and version the evaluation set — IN PROGRESS

- [x] Audit all 14 atom source anchors and prepare
  `docs/owner-review-batch-proposal-2026-10-27.md`. The owner approved the
  exact `design-for-failure.md` claim, authorized a pending source-backed
  project-decision draft, and authorized correcting the procedure candidate
  while keeping it pending. No other approval was inferred.
- [x] Prepare and present a source-backed sample for fact, procedure, and
  project-decision categories. Preference classification and safe answer mode
  remain unresolved; do not invent a preference atom.
- [ ] Resolve a safe answer mode for owner-reported preferences; current
  mutation policy refuses to approve `epistemic_label: preference` as factual
  standard recall.
- [x] Keep sodium-ion pending until its primary article is available and
  reviewed.
- [x] Add answerability, expected claims, answer mode, trap rationale, dataset
  version, split, and atom revision hashes to evaluation cases.
- [x] Validate every positive answerability label against a current
  owner-approved atom revision and manually audit labels before scoring.
  Pending targets are explicitly exploratory. `scripts/evaluation_dataset.py`
  validates the contract before `evaluate_retrieval.py` scores it.
- [x] Record dataset version/hash and separate candidate-ranking scores from
  approved-answer expected-atom retrieval coverage; fail closed when dense
  embedding is unavailable.
- [ ] Add reviewed development and held-out cases; the current seven are
  regression-only and too small to support broad quality claims.
- **Depends on:** Goal 0.
- **Details:** `tasks/plan.md` Goal 1.

### Goal 2 — Enforce and measure answer-level grounding

- [x] Define typed `AnswerRequest`, `GroundedClaim`, `EvidenceReference`, and
  `AnswerResult` contracts.
- [x] Validate standard factual drafts against exact current approved claims,
  retrieved atom bytes, canonical approvals, and current source anchors before
  either chat surface renders output. Raw model text and model-authored
  citations are withheld; the program selects full anchored excerpts.
- [x] Add `scripts/evaluate_answers.py` for repeated end-to-end local model
  evaluation with answerable/trap metrics, atom/dataset/model/index provenance,
  and local ignored output.
- [ ] Keep research leads distinct and explicitly labelled; render provenance
  from program-verified data. Standard claim provenance is program-rendered;
  a separate research mode is not implemented.
- [x] Add repeated answer-level trap evaluation and supported/unexpected claim,
  citation, abstention, and coverage metrics. A three-repeat run on the current
  two positive/two trap regression cases completed with the local 7B model.
- [ ] Grow the answer set with reviewed development and holdout examples and
  test more models and hardware before using these pilot scores for model or
  release decisions.
- **Depends on:** Goal 1.
- **Details:** `tasks/plan.md` Goal 2.

### Goal 3 — Complete review and trust maintenance

- [x] Add `scripts/audit_sources.py`: report recorded and current source hashes,
  anchor status, atom revision, and current approval; read-only by default.
- [x] Add opt-in `--demote-stale --actor <owner>` to move stale accepted atoms
  to pending through the canonical CAS mutator and append-only decision log.
- [x] Show source path/hash/byte range, current quote/hash, anchor state,
  related atom status, and stale approval demotion in Vault Explorer.
- [x] Implement `apply_atom_merge`: owner-directed two-atom supersession with
  CAS on both inputs, bidirectional links, backups, paired append-only audit,
  survivor re-review, and both files preserved.
- [x] Expose explicit merge in `review_atom.py`; keep the established one-atom
  transition CLI syntax working.
- [ ] Add owner-facing merge control and finish quarantine/reject/demote
  controls with clear action consequences.
- [ ] Verify recall excludes superseded atoms and finish source-change review;
  merge rollback is covered for second-write, index-refresh, and audit failures.
- **Depends on:** Goals 1–2.
- **Details:** `tasks/plan.md` Goal 3.

### Goal 4 — Close dashboard and MCP access gaps

- [x] Threat-model dashboard local access; add a fail-closed owner password
  gate before vault reads, a salted scrypt verifier, idle expiry, manual lock,
  and explicit loopback/CORS/XSRF/host settings.
- [ ] Exercise real cross-origin reads and WebSocket/state-changing requests;
  the installed app is still local-only and unsupported behind proxy/tunnel.
- [x] Define trusted local MCP launch and content-free audit policy; keep MCP
  opt-in, narrow, and unavailable to cloud-connected clients.
- [x] Migrate MCP to the installed SDK v2 callback/result contract; verify its
  scope and audit-failure behavior. External-data quarantine remains covered
  by prior ingestion tests.
- **Depends on:** Goal 3.
- **Details:** `tasks/plan.md` Goal 4.

### Goal 5 — Make setup, health, and recovery reproducible

- [x] Add the Python 3.11 package manifest and uv lock; split the model-free
  core from dashboard, online-model, MCP, Scout, multimodal, watchdog, and
  evaluation extras.
- [ ] Finish the installer across supported operating systems and validate a
  clean-profile setup. Windows setup currently bootstraps uv, installs the
  dashboard extra, initializes lexical recall, and launches the local UI.
- [ ] Finish reporting Python/OS/CPU, installed optional capabilities, Ollama models,
  lexical status, vector model/dimensions/generation, atom count, and
  degradation reason. Current CLI reports verified approval counts, but lacks complete native-runtime
  and memory/resource diagnostics.
- [ ] Document and verify clean setup, offline lexical use, reproducible index
  content, and rollback after failed/interrupted rebuild.
- **Evidence:** Isolated Windows wheel install and vault initialization passed;
  full cross-platform setup, dashboard launch from a wheel, and recovery matrix
  remain unverified.
- **Depends on:** Goals 2–3.
- **Details:** `tasks/plan.md` Goal 5.

### Goal 6 — Expose project-scoped recall consistently

- [ ] Standardize `project:<id>` context values and expose project/status filters
  through CLI, dashboard, and MCP using the shared recall contract.
- [ ] Explain lexical, dense, context, and linked-neighbor matches concretely.
- [ ] Add read-only duplicate suggestions with provenance/status/revision data;
  no automatic mutation or merge.
- [ ] Verify consistent results and offline project search across interfaces.
- **Depends on:** Goals 2 and 5.
- **Details:** `tasks/plan.md` Goal 6.

### Goal 7 — Expand benchmark and decide retrieval modes by evidence

- [ ] Add reviewed regression, development, and holdout examples; compare
  lexical, dense, and combined ranking separately.
- [ ] Report relevance, traps, answer grounding, end-to-end latency, p50/p95,
  and machine resource use bound to dataset/atom/model/generation versions.
- [ ] Keep weights provisional; enable dense by default only if a representative
  held-out benefit meets a declared hardware latency target.
- **Depends on:** Goals 1, 2, 5, and 6.
- **Details:** `tasks/plan.md` Goal 7.

### Goal 8 — Independent review and local pilot release gate

- [ ] Restore independent reviewer authentication when available; have Claude
  Opus perform read-only adversarial review of actual current code and evidence.
- [ ] Reconcile each finding, make fixes or document evidence-backed tradeoffs,
  then perform the primary agent's final self-audit.
- [ ] Run full static, behavioral, failure-injection, integrity, security,
  retrieval, performance, and runtime sweeps; make no release claim until all
  defined gates pass.
- **Depends on:** Goals 1–7.
- **Details:** `tasks/plan.md` Goal 8.

## Deferred product horizon

- [ ] Host-independent memory API, local/online/owner-built AI adapters,
  production cross-platform one-click installation, per-host sharing controls,
  and hardware-aware capability profiles. Goal 5 now has the package, lock,
  bootstrap CLI, and Windows setup foundation; broad host portability remains.
- [ ] Benchmark state-of-the-art claims against current alternatives on
  representative tasks and declared hardware before making them.
- **Starts only after:** Goal 8 passes. See `tasks/plan.md` Deferred product
  horizon and `CODEX_CLI_GOAL.md` for the owner's complete product contract.
