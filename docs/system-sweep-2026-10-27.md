# System sweep — 2026-10-27

## Verdict

The project has working, source-backed owner review; a real local Chroma cosine index; public-IP-pinned Scout fetches; and a local dashboard launcher that starts on loopback. It is still not a release-ready factual assistant. A conservative two-term overlap gate blocks the two owner-personal trap queries, while a fresh audit caught that the gate was also suppressing the approved logging paraphrase because it ignored the atom's supporting Evidence section. The gate now checks both Claim and Evidence. That remains a literal heuristic, not a proof of answerability.

## Current state

- 14 canonical atoms; two current owner-approved atoms (`structured-logging.md`, `design-for-failure.md`). The corrected procedure and owner product-target draft remain pending. The sodium-ion lead remains unapproved because the underlying article is absent and its raw note is explicitly tentative.
- Chroma generation `ce48706db17d440fbba04b8c7f86c37e`, `nomic-embed-text`, 768 dimensions, cosine metric; 14/14 atoms indexed and current FTS index. Windows Chroma imports after installing the official signed Microsoft Visual C++ v14 x64 runtime.
- Ollama exposes four installed models; `llama3.2:3b` and `nomic-embed-text` are configured. `llava` is absent; another advertised vision model remains unverified and disabled.
- 24 queued captures, 14 queued URLs, no source Markdown pages or generated threads.
- `run_dashboard.ps1` returned HTTP 200/`ok` from `/_stcore/health`; its listener was `127.0.0.1:8501`. It was stopped after verification. The dashboard has no authentication.
- The code-only baseline is committed as `f4ab16e` (`chore: establish code-only project baseline`). The working tree is clean; the live `brain/` vault and `tmp/` remain local and ignored. There is no Git remote. The prior commit `bffca9e` still contains 18 old-layout vault files in local history, so do not add a remote or push until that history is reviewed or scrubbed.

## Retrieval result and change

Owner-approved dataset v2.0.0: five candidate-ranking queries, of which two are answerable from approved evidence and three are exploratory, plus two no-factual-answer traps. Raw candidate ranking remains unchanged by the trust relevance filter:

| Metric | Lexical | Dense |
| --- | ---: | ---: |
| Mean Recall@5 | 1.00 | 1.00 |
| Mean MRR | 1.00 | 1.00 |
| Mean nDCG@5 | 0.975 | 1.00 |
| Median retrieval latency | 5.624 ms | 54.150 ms |

The raw candidate-ranking metrics are over five positive queries and 14 atoms. They do not show that dense retrieval is better in representative use, and the ~9.6x median latency difference does not include p95 or end-to-end answer latency. Dataset v2.0.0 separates those five ranking labels into two approved answerability cases, three exploratory cases with pending targets, and two traps. The two answerable expected atoms appear in lexical and dense top-five and trusted context. This still does not evaluate generated answer correctness.

Before the relevance check, both trusted paths returned one approved result for each trap (four false-positive retrievals total). The test-first change requires at least two meaningful query tokens to occur in a candidate's `Claim` or `Evidence` section before lexical or dense recall can return it; linked neighbors receive the same check. The fresh live rerun returned **zero trusted results for both traps** and returned the approved `structured-logging.md` atom for the logging paraphrase and `design-for-failure.md` for failure handling through both paths. The three exploratory candidate labels remain unapproved and are correctly withheld from trusted context. Candidate ranking must not make pending atoms answerable.

At the time of the original sweep, the overlap test was a conservative literal match and answer-level grounding was unmeasured. The original 0/2 retrieval trap result did not measure generated answers. The follow-ups below record the later grounding implementation and live answer evaluation.

The current evaluator has no separately labelled dense-only, keyword-only, and combined-ranker categories; it measures lexical and dense candidate rankings independently. The default `search_memory` selects lexical when the FTS sidecar exists. Dataset `second-brain-retrieval-pilot` v2.0.0 and the exact dataset SHA, atom revision SHA values, split, answerability, expected claims, and trap rationale are stored in the result. Every current case is in the regression split; development and holdout are empty. This is a useful smoke regression set, not calibration evidence for score weights or general answerability.

## Other security and integrity findings

- **Review and provenance:** approval requires an exact quote in a uniquely resolved local source, stores source hash/byte offsets, and is tied to the exact atom revision and latest owner decision. Current approval was explicitly authorized by the owner. Source anchoring is not independent proof that the local captured guidance is authoritative.
- **Scout:** validates every DNS answer, pins the HTTP connection to those addresses, rejects redirects, disables environment proxies, and caps response size. External captures remain `web_remote` and quarantined. The canary test exercises a real local server through the pinned transport.
- **MCP:** disabled unless explicitly enabled; no listable resource catalog; approved-only atom reads; bounded untrusted inbox writes. It has no client authentication. Do not attach a cloud-connected client.
- **Dashboard:** loopback binding is verified, not authenticated access. Its safety depends on keeping it local and not placing it behind a proxy/tunnel.
- **Dependencies:** there is no `pyproject.toml` or `uv.lock`; the launcher resolves and installed 44 packages at runtime. It worked on this machine, but dependency versions and fresh/offline setup are not reproducibly pinned.
- **`tmp/`:** inventory contains exactly four PNG page previews under `tmp/pdfs/`, rendered from the PDFs listed in the learning-design memo. They are reproducible derived previews, retained on disk and ignored by `.gitignore`. No other files were found under `tmp/`.
- **Independent reviewer:** the required read-only Claude Opus review was attempted using the saved session, but failed because the CLI OAuth session had expired and could not be refreshed. No independent review is claimed.

## Verification performed

- `uv run --python 3.11 scripts/smoke_test.py` — passed after adding lexical/dense evidence-supported paraphrase checks, no-context abstention checks, and trap regression assertions. The new paraphrase regression failed before the Claim+Evidence implementation.
- `uv run --python 3.11 scripts/evaluate_retrieval.py --output docs/retrieval-evaluation-results-2026-10-27.json` — passed against generation `ce48706db17d440fbba04b8c7f86c37e` and dataset v2.0.0; five candidate-ranking queries, two answerability queries, three exploratory queries, and two traps; expected approved atoms appear in both trusted paths; trap false positives `0`.
- `uv run --python 3.11 scripts/chroma_rebuild_canary.py` — `CHROMA_REBUILD_CANARY_OK` for both canary atoms.
- `uv run --python 3.11 --with httpx --with trafilatura --with beautifulsoup4 python scripts/system_safety_tests.py` — 14 tests passed after the relevance change.
- `uv run scripts/archive_manifest_canary.py` — passed after the relevance change.
- `uv run scripts/chroma_rebuild_canary.py` — passed against persistent Chroma after the relevance change.
- `uv run python -m compileall -q scripts dashboard chat_with_memory.py` — passed. The full staged baseline check reports 129 trailing-whitespace lines and three extra blank EOFs in existing project sources/docs; no whitespace cleanup was applied because some spaces occur in prompt and UI text.
- Shipped dashboard launcher — started successfully; health endpoint returned `ok`; only loopback listener observed; stopped and confirmed no listener remains.
- `uv run --python 3.11 scripts/doctor.py` — current 14-atom indexes and two approved atoms reported OK; vision is unverified/unavailable and dashboard is stopped after the launcher check.

## Still open

1. Add more owner-approved memories only after source review; keep sodium-ion pending until primary evidence is present.
2. Expand the gold set and manually inspect missed matches, false matches, and answer output. Add explicit dense-only/keyword-only/hybrid categories and p95 latency before making a dense/default decision.
3. Evaluate answer-level factuality on repeated model outputs and broader trap questions. Empty-context abstention is now structural in the CLI and dashboard, but that does not prove grounded answers when context is present.
4. Before any future remote is configured, review or remove the 18 old-layout vault files retained in local Git history. The current code-only baseline is local and clean; the live vault remains excluded.
5. Obtain the required independent Claude review after its CLI authentication is restored, then reconcile its findings.

## Product direction

The owner clarified the long-term goal: a one-click installable Brain plugin
for any PC, with local/offline mode and online mode. Offline must be a
first-class, state-of-the-art target, not a weak fallback. Online mode may offer
stronger models and additional capabilities; it must have explicit controls
over which data is sent to which host. Owner-built AI hosts are also in scope.
The current repository is not close to meeting that distribution claim. It has
no packaged plugin contract, host-adapter API, supported-host matrix, portable
installer, or cross-platform verification. The safe direction is a
host-independent memory core plus narrowly scoped adapters. Keep the current
local runtime and trust gates as the first core proving ground while designing
the online tier's data-sharing and permission boundaries. “Better than anything
available today” remains an unproven evaluation target; define representative
tasks and compare against current alternatives before making that claim. A
one-click installer must probe each PC and expose a truthful capability profile
because hardware cannot provide identical model quality everywhere.

## Commands and operator notes

Rebuild and inspect indexes with `uv run scripts/rebuild_lexical_index.py`, `uv run scripts/rebuild_vectors.py`, and `uv run scripts/doctor.py`. The vector rebuild requires the configured local embedder and the Windows Visual C++ runtime for native Chroma. Keep external ingestion, MCP, and unattended workflows off until their remaining release gates are met.

## Follow-up — 2026-11-01

The evaluation contract is now version 2.0.0 and distinguishes five raw
candidate-ranking labels from two answerable examples, three exploratory
pending-target examples, and two no-answer traps. Answerable cases pin exact
atom SHA-256 values and expected claims; a validator checks current atom bytes
and the canonical owner-approval decision before scoring. The evaluator now
reports candidate metrics separately from approved-answer expected-atom
coverage and records dataset version/hash and index generation. The 28-test
system-safety suite and smoke test pass after these changes.

The CLI and dashboard now buffer model output as a structured draft and display
only exact current approved atom claims with source-derived excerpts and
program-rendered citations. The validator uses the canonical approval decision
log under the shared writer lock; callers cannot inject their own approval
predicate. The dashboard stores validated drafts and revalidates them before
history replay, and it no longer renders raw retrieved atom bodies in the chat
path. Chat output uses plain text rendering to prevent Markdown link or markup
spoofing. Unit tests cover exact claims, approval revocation, source changes,
stale revisions, and malformed evidence. This still does not prove entailment
beyond the owner's exact approved Claim text, and a source manually changed
outside the shared writer protocol can race after a completed render.

An independent read-only Codex CLI adversarial review ran against an isolated
copy of the four answer-boundary files. It found history replay of stale
answers, raw retrieved text shown before validation, a caller-controlled
approval-verifier bypass, model-selected evidence excerpts, and a read/render
race. These were addressed by structured history revalidation, removal of raw
retrieval previews, mandatory canonical approval checks, deriving the displayed
excerpt from verified source bytes, and using the shared writer lock while
validating. The review was not a release audit of the rest of the repository.
The 28-test safety suite and smoke test pass after these changes.
The dashboard runtime returned HTTP 200 for both its Streamlit health endpoint
and the main page while bound to 127.0.0.1:8502; the listener was stopped and
confirmed closed. A full chat turn was not exercised because Ollama remained
unavailable.

During the initial 2026-11-01 sweep, Ollama was not reachable. Later that day,
the local service became available. The dense evaluator still preserves its
last successful result if the embedder is unavailable. The dataset remains
regression-only.

### Live trust and answer-grounding follow-up — 2026-11-01

Added `scripts/audit_sources.py`. It reports each atom revision, recorded
anchor, current source hash, anchor status, and canonical approval state without
mutating by default. Its explicit `--demote-stale --actor <owner>` path uses
the CAS mutator and decision log to demote stale accepted revisions; it never
repairs an anchor or re-approves an atom. The read-only audit of the current
private vault found both approved atoms current and the other twelve pending;
it made no atom changes. Review UI integration and two-atom merge remain open.

A fresh retrieval evaluation was also run after Ollama became available. It
used the same dataset SHA and index generation and again returned 1.00 Recall@5
and MRR for lexical and dense candidate ranking, 0 trusted trap results, and
1.00 expected-atom recall for both answerable cases. The persisted result in
`docs/retrieval-evaluation-results-2026-10-27.json` records 6.551 ms lexical
median and 57.647 ms dense median for this run. These are single-run latency
figures over 14 atoms, not hardware-independent performance claims.

Added `scripts/evaluate_answers.py`. It repeats complete model calls through
the same trusted recall and answer validator as the chat interfaces, records
only validated claims and provenance (not raw model drafts), and separates
answerable coverage from trap leakage. Ollama's JSON-schema output mode is used
with a claim-string-only schema; the model does not author revision hashes,
atom IDs, evidence quotes, or source citations. The validator maps exact claim
strings back to currently approved retrieved atoms and renders source details.

On this Windows host, `qwen2.5-coder:7b` (Ollama model ID
`dae161e27b0e`) completed three independent outputs for each of two answerable
cases and two no-answer traps. Dataset SHA-256 was
`625f61fff06515bd6688fd1e853a6c64752b036fe621847d99200b8ae756770b`, using
lexical recall and dense generation `ce48706db17d440fbba04b8c7f86c37e` as the
recorded local index generation. Results: **6/6 expected positive claims
covered, 0/6 unexpected displayed claims, 6/6 traps abstained, and all 6
displayed claims had program-verified citations**. Positive end-to-end latency
was 6.107 s p50 and 49.759 s p95 across six runs, including cold model load;
four subsequent warm positive calls took 5.154–6.131 s. The nearest-rank p95
over six samples is the maximum, so this is a rough local signal. The JSON
observations are in the ignored local file
`brain/.sidecar/answer-evaluation-latest.json`; only aggregate evidence is
recorded here.

This is a useful smoke result, not a model ranking or release claim: only two
answerable prompts and two traps exist, all are regression examples, and the
evaluation was run on one CPU-only model/host. A 16 GB machine cannot run the
installed 23 GB Qwen model within its memory budget. The smaller configured
`llama3.2:3b` did not answer consistently in preliminary runs. More reviewed
examples, repeated holdout runs, model comparisons, and hardware profiles are
still required. The configured default `llama3.2:3b` produced no expected
positive claim in one repeat with the final claim-only prompt, while
`qwen2.5-coder:7b` passed the three-repeat smoke set. That model comparison is
not balanced: only the 7B model received three repetitions. Research lead
mode, dashboard authentication, dependency locking, and a full release audit
remain open.

## Follow-up — 2026-11-01 dashboard authentication

The Vault Explorer now shows the atom revision hash, evidence excerpt, pinned
source path/hash/byte range, current source quote/hash, anchor validity, and
related atom statuses. A stale approved atom can be explicitly demoted through
the canonical CAS mutator. Merged atoms cannot be re-approved through the
single-atom transition API. Two-atom merge is available in the CLI and core
mutator, but the dashboard still has no merge control.

The dashboard now uses a fail-closed local owner password gate before vault
reads or rendering. A salted scrypt verifier is stored in per-user application
state; login sessions expire after 30 idle minutes and have a manual lock
control. Streamlit CORS, XSRF, strict XSRF cookie behavior, allowed host, and
allowed origin are explicitly configured for `127.0.0.1`. `run_dashboard.ps1`
configures the owner password on first launch. A live browser check verified
locked, unlocked, and manually locked states. Unit/config tests pass. This gate
does not protect against another process running as the same OS account, and
MCP remains unauthenticated and unsafe for cloud-connected clients. Automated
cross-origin request/WebSocket probes are still open; remote dashboard hosting
remains unsupported. See `docs/dashboard-security.md`.


### MCP SDK compatibility follow-up — 2026-11-01

The MCP package has no repository-wide version lock yet, and `uv run --with mcp`
resolved SDK 2.3.0. The server used removed v1 decorator APIs, so a fresh setup
with the current package could not import or launch it. Migrated to SDK v2
constructor callbacks and typed list/call/resource result objects. The narrow
tool set, opt-in stdio boundary, empty listable resource catalog, approved-only
resource reads, and content-free pre-operation audit remain in place.

Focused MCP checks, all 49 system safety tests, smoke test, Python compilation,
PowerShell parsing, and `git diff --check` pass. Cross-origin dashboard
WebSocket/state-changing probes remain open; the previously attempted browser
probe was rejected by the UI automation URL policy and was not worked around.


### Installable package and Windows bootstrap follow-up — 2026-11-01

Added `pyproject.toml` and `uv.lock` for Python 3.11, a model-free base
package, and separate optional extras for the dashboard, online model client,
MCP, Scout, multimodal capture, watchdog, and evaluation. The wheel includes
both script modules and the dashboard. `second-brain install` initializes the
vault and current SQLite FTS index; `doctor` reports platform/runtime, optional
modules, local atom and approval counts, Ollama models, lexical status, and
verified vector generation metadata. Wheel installs place the vault in a
per-user data directory, while an editable checkout keeps the repository's
`brain/` path.

`setup.ps1` now uses uv-managed Python 3.11, syncs the locked dashboard extra,
initializes lexical recall, and launches the password-protected local dashboard
unless `-NoLaunch` is passed. It tries WinGet to install uv when neither uv nor
a system Python bootstrap is present, with pip as the fallback. It does not
install the Ollama server or model weights. The public WinGet package ID was
checked against the [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).

Verification on this Windows x64 host: `setup.ps1 -NoLaunch` completed;
`uv sync --locked --extra dev` succeeded; all 50 system safety tests and the
smoke test passed; Python compilation and PowerShell parsing passed; and the
wheel built successfully. An isolated Python 3.11 environment installed the
wheel with its dashboard extra, selected a temporary user-data vault, launched
`second-brain dashboard`, and returned HTTP 200 from both Streamlit health and
app endpoints. A protected-configuration launch also returned HTTP 200 on a
test port. The listener was stopped. Cross-origin/WebSocket attack probes are
still open, as are macOS/Linux installers, the no-uv WinGet bootstrap branch,
recovery-matrix testing, model-runtime installation, arbitrary-host adapters,
and offline quality benchmarks. This is an install foundation and Windows
local UI, not yet a one-click universal AI plugin or evidence for a state-of-
the-art claim.
