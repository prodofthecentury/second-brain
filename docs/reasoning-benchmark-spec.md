# Stage-6 reasoning benchmark harness: implementation spec v2.2 (built first)

- **Status:** Approved v2.2. It answers the Brain Overseer's REJECT review of v1 (all 23 required changes; the change log is in §16). Nothing in here is a measured model result.
- **Previous version:** `/workspace/second-brain-specs/benchmark-harness-spec.v1.md`.
- **v2.1:** applies exactly the 14 required changes and the deviation conditions from the v2 re-review (`benchmark-harness-review-v2.md`). The v2 text is kept in `benchmark-harness-spec.v2.md`, and the diff is in `v2-to-v2.1.diff`.
- **v2.2:** applies the 7 conditions from the v2.1 approval (`benchmark-harness-review-v2.1.md`). The v2.1 text is kept in `benchmark-harness-spec.v2.1.md`, and the diff is in `v2.1-to-v2.2.diff`.
- **Target repo:** `prodofthecentury/second-brain` on top of PR #9 (`pr9` @ `f24d020`). I read it from a local clone and changed nothing in the repo.
- **Author:** Grok Bot (benchmark harness lead) for gork (coordinator). Written 2026-11-27 (NZDT).
- **Power numbers:** every power or MDE figure comes from running `/workspace/review-notes/power.py` (raw output in `/workspace/second-brain-specs/power-output.txt`). The template-plan figures come from `/workspace/second-brain-specs/power_ext.py`, which imports power.py's functions unchanged (raw output in `power-ext-output.txt`).

This file is the approved v2.2 spec, committed with the harness. The implementation follows it. Held-out plaintext is not in the repo.

## 0. TL;DR for the coding agent

1. Add `scripts/reason_bench.py`, `reason_bench_models.py` (a raw Ollama and OpenAI client, not `chat_with_fallback`), `reason_bench_scoring.py`, `reason_bench_heldout.py`, and `reason_bench_tests.py`. Data lives under `bench/reasoning/`.
2. Every item has a `template_id`. All CIs are cluster bootstraps clustered by template. Public dev has at least 100 templates: math 60 × 2, multihop 20 × 2, code 20 × 1, logic 12 × 2, for 204 items. `dev-core` is instance 0 of each template, 112 items. Held-out is private, owner-authored, disjoint, target 60–80 templates. The repo holds only a salted hash commitment.
3. Model-free suites run on every PR. `hijack` is at least 220 ordinary sentences; a fire of `try_exact_tool` is a HARD FAIL. `tools` measures precision (a tool answer must be right; declining is allowed) and reports recall on `expected_tool: "any"` controls.
4. Runners: `plain`, `plain_sc@k`, `fast`, `deep`, `deep_no_tools`, `tools_only`, `oracle`. The primary pre-registered comparison is deep − plain, overall accuracy on held-out.
5. HARD gates are absolute: `tool_wrong == 0`, `claimed_verified_wrong == 0` on deterministic trap and mock suites, `tool_crash == 0`, `engine_exception == 0`, hijack fires == 0, `answer_source` present once the engine contract says so, manifest and commitment OK, no isolation leak. `visible_pass_hidden_fail` is reported, not gated. Headline metric: P(correct | verified=True) with a Wilson CI and a template-cluster bootstrap CI.
6. An exact fraction compares as an exact `Fraction`, with no tolerance. That always holds for tool answers. Model answers get tolerance only when an item sets `approx_ok`.
7. `answer_source` is mandatory on every result record. Once the engine declares `contract_version >= 2`, a missing value exits 2. Before that, the harness derives it and the presence gate HARD-FAILs.
8. Hashing is CRLF-proof: `bench/reasoning/** text eol=lf`, canonical JSON and LF-normalized text, and a Windows job with `core.autocrlf=true` before checkout.
9. At `f24d020` the harness reports HARD FAIL. That is its acceptance test. `expected-hard-failures.f24d020.json` is the exact `(gate, item_id, sympy_mode)` list. The hard-gate job is non-required. The fingerprint check fails if a tuple appears or disappears without the same PR editing the file. The file may only shrink.

## 1. Stage-1 bugs the harness must catch

Observed at `f24d020` with a scripted or absent model, not as model accuracy:

| # | Observation |
|---|---|
| B1 | The pens question that sells half and then receives 5 more returns `101` via `word-template`. Correct is 53. |
| B2 | Without SymPy, `try_exact_tool("What is 2/3?")` returns a rounded decimal labelled exact. With SymPy it returns `2/3`. Default installs take the no-SymPy path. CI's natural pass installs the `math` extra. |
| B3 | The twelve ordinary sentences `hijack-b3-01` … `hijack-b3-12` are verbatim in `hijack.jsonl`. Exactly `hijack-b3-04` … `09` fire on both sympy modes. |
| B4 | `unsupported_numbers` is a substring test, so a wrong `4` counts as supported by a note that says `14`. |
| B5 | With no retrieved notes, any number in a draft is rejected. |
| B6 | The engine's wall-clock timeout raises `RuntimeError("reasoning timeout")` out of `reason()`. |
| B7 | `Fax 09-377 1234.` raises `SyntaxError` from `try_exact_tool` because the handler catches only `ValueError`, `ZeroDivisionError`, and `OverflowError`. |

The harness does not use `chat_with_fallback` or `retrieve_atoms`. Those paths can override the model, merge runtime options, redact the prompt, or read the active vault index.

## 2. Layout

Public data is under `bench/reasoning/`: `MANIFEST.json`, `heldout.commitment.json`, `preregistration/`, `heldout-results/` (aggregates only), `heldout-access.jsonl`, `templates/`, `suites/`, `corpus/dev/notes/`, `prompts/`, `mock/responses.jsonl`, `baselines/`, and gitignored `results/` and `cache/`. Scripts are `reason_bench.py` and the scoring, models, heldout, and tests modules. Wrappers are `run_reason_bench.sh` and `run_reason_bench.ps1`.

Tracked paths matching `*heldout*.jsonl`, `heldout/**`, `*.sbheldout`, `*.sbsealed`, `templates-private/**`, or `corpus/heldout/**` fail CI. The commitment, the access log, and `heldout-results/` are the exceptions.

`reason-bench` is a CLI passthrough. A wheel without `bench/` exits 2 with "needs a source checkout".

## 3. Data

Every item has `id`, `template_id`, `instance`, `suite`, `split` (`dev` in the repo), `question`, `answer`, `answer_type`, `approx_ok` (default null), `answerable`, `difficulty`, `tags`, and `provenance`. Non-code items have a sandbox reference that binds `ANSWER`. `expected_tool` is null or `"any"` and is recall-only.

Math: 60 templates, 2 instances, at least 10 template-trap templates, at least 6 fraction-exact templates, at least 4 all-but or more-than-half templates, at least 6 missing-information templates (about 10% of math items), at least 6 distractor-number templates, and at most 10 GSM8K-derived templates. This tree ships no GSM8K-derived items.

Multihop: 20 × 2, at least 15% unanswerable from at least 3 templates, at least 4 temporal items, at least 4 numeric aggregations whose answer is not in the notes, at least 2 substring-verify traps, at least 2 date-arithmetic items.

Code: 20 tasks, one instance each: 5 string, 5 list/dict, 5 parsing, 5 algorithmic. Sandbox allowlist only.

Logic: 12 × 2, each with exactly one brute-force solution.

`tools.jsonl` migrates the six `docs/reasoning-benchmarks.json` controls, keeps `gsm-pens` (answer 101) as a should-fire control, and adds fractions, dates, units, and at least 20 should-fire controls. The trap sibling (answer 53) lives in math.

`hijack.jsonl` has 220 sentences: 60 hand-written, including the 12 B3 sentences, and 160 from a fixed seed. Categories: phone 20, dates and times 30, versions 20, numeric ranges 20, scores and ratios 15, recipe fractions 20, "N times" 20, boxes narrative 20, ids 25, money 15, units in prose 15.

The tools suite, the fraction traps, and the hijack suite each run twice: `sympy_mode=natural` and `sympy_mode=off` (`unittest.mock.patch` of `_sympy_value` to `ImportError`). HARD gates apply to both. `run.json` records `sympy_available` and `sympy_version`.

Named dev traps include the pens-half item, the all-but class item, exact fractions, the substring note, the boxes all-but item, the leading-zero fax question, a hijack-in-question item, a plan-not-executed item, and missing-information items. They are listed in `traps.dev.jsonl` and also live in their parent suites.

No HumanEval, MBPP, HotpotQA, 2Wiki, or MATH-500. External GSM8K, GSM-Plus, and GSM-Symbolic are optional paired-delta downloads into the gitignored cache, labelled contaminated, never a gate and never held-out. GSM-Symbolic is off by default (§0).

Canonical hashes: JSONL is parsed, keys sorted, rows sorted by id, joined with `\n` and a trailing newline. JSON is canonical. Text strips a BOM, normalizes newlines to LF, and strips trailing whitespace per line. `validate` and `run` recompute the manifest. A mismatch exits 2 unless `lock --bump-version X.Y.Z --reason …` rewrote it.

## 3.9 Held-out integrity

The owner alone authors held-out templates, on a laptop, never in this repo or `/workspace`. Templates are sealed (`templates.sbsealed`) before `heldout-generate` and `heldout-check-disjoint`. Those commands decrypt in memory and load modules from strings. They do not write template plaintext.

Sealed layout: canonical header JSON, `0x0A`, AES-256-GCM ciphertext with tag. KDF is scrypt, `n >= 2^17`, `r = 8`, `p = 1`, a fresh 32-byte salt, and `hashlib.scrypt(..., maxmem=256 MiB)` so Windows is not forced to lower `n`. HKDF-SHA256 splits `master` into `sb-sealed-aead-v1` and `sb-heldout-hmac-v1`. A fresh 96-bit nonce is drawn on every seal. AAD is the exact header bytes. The passphrase comes from `getpass` only. Sealing asks twice. No TTY exits 2.

Raw, sealed, and temp paths are realpath-checked and refused under `/workspace`, the repo root, or a cloud-sync folder (`OneDrive*`, `Dropbox`, `Google Drive`, `My Drive`, `iCloudDrive`, `iCloud Drive`, or the OneDrive environment variables).

`write_heldout_aggregate` may write inside the repo only after it proves the payload has no question, note, hidden test, model output, or plaintext item id. Per-item keys are HMAC hex digests.

Disjointness masks numbers and capitalised names and rejects word 4-gram Jaccard at or above 0.5. Only the three summary fields are committed.

The access log and the no-looking rule are honour-based. The commitment proves the set was not changed after the fact. It does not prove nobody looked. Retire the set after 3 runs or any item-level look.

## 4. Runners and the raw client

`plain` is one chain-of-thought call. `plain_sc@k` is k samples at temperature 0.6; a tie is `ABSTAIN`; for code, the first sample that passes the visible tests is kept. `k` defaults to 3. `fast` and `deep` call `reason` with those profiles. `deep_no_tools` passes `tools=frozenset()` when the engine accepts it, and otherwise patches `try_exact_tool` to return None. `tools_only` calls `reason` with no model. `oracle` is plain with exactly the gold-support notes.

Fairness notes are printed in every `summary.md`: deep's draft prompt asks for one or two sentences, prompt-template hashes are recorded, and the share of correct answers from fallback extraction is reported.

The raw client posts to Ollama `/api/chat` or OpenAI `/v1/chat/completions`. It does not merge hardware options, redact PII, fall back to another model, or rewrite think blocks. `BRAIN_GEN_MODEL` differing from the requested model exits 2. The response model must match. The `/api/show` digest is checked at warm-up and at the end. `sha256` of the prompt sent must equal `sha256` of the prompt intended. Early stop happens only after an `ANSWER:` line is closed by a newline, the stream ends, or `num_predict` tokens are reached. Derived token counts are labelled.

`num_predict` is 384 for math and logic, 256 for multihop, and 512 for code. Headline temperature is 0, seed 1234, `num_ctx` 4096, explicit `num_thread`.

Call or token budget overruns score wrong as `budget_exceeded`. An engine `RuntimeError("model call budget is spent")` or `RuntimeError("reasoning timeout")` also counts as `engine_exception`. Any other exception escaping `reason()` is `engine_exception` (or `engine_timeout` when the message is the wall-clock timeout). A hang guard sized from measured tok/s marks `hung`, which scores wrong and counts in the timeout FAIL rule only.

Retrieval uses a temporary vault. `BRAIN_VAULT_DIR` is pointed at that vault. The retriever calls `search_memory` with an explicit atoms dir, lexical index, and chroma path, `retrieval_mode="lexical"`, and `use_l0_bus=False`. Every retrieved row must resolve under the temp vault. A canary is written to a disposable vault in CI, not to the owner's real vault, unless `--canary-real-vault` is set.

`--aa` forces `--cache off`. Any other cache mode with `--aa` exits 2. The cache is read-disabled for scoring when the latest A/A `flip_rate` is above 0.

## 5. Scoring

Extraction order: engine abstain, last `ANSWER:` line, a bare value only when the whole string parses as the answer type, then a typed fallback, else `NONE`. Never substring. Fractions use `Fraction`, including `Fraction(Decimal(s))` for decimals. Tool answers ignore `approx_ok`. Dates are day-first. Unanswerable is correct only for `ABSTAIN`. Code runs visible and hidden tests together in `run_sandboxed_python`. A visible pass with a hidden fail is reported and is not a HARD gate.

## 6. `answer_source`

`tool_exact`, `model_verified_tests`, `model_claimed_verified` (the old `model_grounded`; at `f24d020` this is `verified=bool(sources)` plus the substring number check), `model_unverified`, `delegated`, `abstained`. Before contract version 2 the origin is `derived` and `engine_answer_source_present` HARD-FAILs. Non-engine runners set the field with origin `harness`.

## 7. Statistics

The primary CI is a template-cluster bootstrap, B = 10,000, seed 20261127, percentile 95%, pure Python. Wilson intervals are labelled item-level. Design effect, `n_eff`, and the ANOVA ρ̂ are reported. Paired comparisons use the cluster bootstrap. Exact McNemar is secondary and ignores clustering. Below about 50 templates the verdict uses the wider of a BCa cluster interval and a cluster-t interval with a CR2 correction. Holm adjusts secondary p-values.

## 8. Gates

Absolute HARD gates, exit 1: `tool_wrong` 0, `claimed_verified_wrong` 0 on trap and mock suites, `tool_crash` 0, `engine_exception` 0, `hijack_fires` 0, `tool_precision` 1.0, `engine_answer_source_present`, `manifest_ok`, `commitment_ok`, `isolation`, `harness_errors` 0, `model_identity`. Live `claimed_verified_wrong` uses the pre-registered floor for the lower CI bound of P(correct | verified), default 0.95 for `tool_exact` and 0.80 for `model_claimed_verified`, plus the §8.3 relative FAIL.

At `f24d020` the fingerprint includes the failures the engine actually produces, including B1, B2 on the SymPy-off pass, B3, B4 through the mock, B7, missing `answer_source`, and the fast profile's escaped model-call budget error. No xfail list is applied to the gates.

Comparability key: digest, server version, `num_thread`, `num_ctx`, `num_predict`, temperature, seed, suite version, harness version, prompt hashes, hardware fingerprint. Anything other than `git_sha` differing from the baseline makes accuracy and latency "not comparable". HARD gates still apply. Until an A/A run exists, FAIL/WARN thresholds are 10 pp and 5 pp and are labelled uncalibrated.

## 9. CLI and CI

```bash
uv run python -m scripts.reason_bench validate
uv run python -m scripts.reason_bench generate --suite math --bump-version 2.2.0
uv run python -m scripts.reason_bench run --tier pr --models hermes3:3b
uv run python -m scripts.reason_bench run --tier ci-mock --backend mock
uv run python -m scripts.reason_bench run --tier ci-live-smoke --models hermes3:3b
uv run python -m scripts.reason_bench fingerprint --check bench/reasoning/expected-hard-failures.f24d020.json
uv run python -m scripts.reason_bench lock --bump-version X.Y.Z --reason "..."
```

Held-out commands are in the README. They require a TTY.

`ci.yml` runs `reason_bench_tests` inside `test_all.py`. The non-required `hard-gates` job and the `hard-failure-fingerprint` job install `uv sync --locked --extra math`. The Windows job sets `core.autocrlf=true` before checkout, then runs `validate` and the hash and sandbox smoke tests. `advanced-runtime.yml` replaces the old live two-model comparison with `run --tier ci-live-smoke --models hermes3:3b` and uploads the artifact. Accuracy on 8 items is not gated. That job fails on HARD gates.

## 10. Runtime budget

Estimates, not measurements. A 3B Q4 CPU call is assumed to take about 20–50 seconds. `pr` tier dev-core plain + deep is about one night. The first real `pr` run must replace this table with measured `gen_tok_per_s` and calls per item. That run is gork's, on the laptop, and is not part of this harness PR.

## 11. Power

Paired McNemar MDE at 80% power, two-sided α = 0.05, from `power.py`. At the `pr` tier (112 templates × 1 instance) a real +5 pp gain will normally read as no detectable difference (about 12–17 pp depending on ψ). Held-out at 30 templates cannot see 10 pp. Gates are exact counts, so one tool error fails regardless of power. Absolute accuracy on public dev can be inflated by contamination. Claims rest on paired deltas on the private held-out set.

## 12. Example items

The verbatim dev examples (pens half, 2/3, 5/6 pizza, Mere's apples, Kestrel/Hamilton, Atlas 14 GB, the 168 GB aggregation, the three code tasks, and the three logic puzzles) are materialized in `bench/reasoning/suites/` and `corpus/dev/notes/`. Gold answers were checked against the references and the logic solvers in the sandbox.

## 13. Tests and acceptance

`reason_bench_tests.py` covers canonical hashes, forbidden paths, normalizers, Wilson / McNemar / Holm / cluster bootstrap, `answer_source` contract exit 2, B1, B4, B7, a clean record, `visible_pass_hidden_fail` not gating, early stop, identity checks, budgets, hang guard, cache read-disable, hidden-test non-leakage, resume, `deep_no_tools`, the B3 fired-id set, and the held-out seal round trip.

Acceptance at `f24d020`: the harness HARD-FAILs, the B3 fired ids are exactly 04–09 on both sympy modes, the fingerprint matches the file, `validate` is green, and the Windows CRLF job is green. One live `ci-live-smoke` is the advanced workflow's job. gork's `pr` tier and A/A run, and the owner's held-out templates, are separate tasks and do not block this PR.

## 14. Build order

Scoring, hashing, hijack and tools, raw clients, runners, dev templates, held-out sealing, then CI. That is the order this harness was built.

## 15. Out of scope

Chat-path runners, MCP `reason` timing, planning tasks, LLM-as-judge, a larger remote reference model, and external sets as gates.

## 16. Change log

The 23 v1 review items and the 14 v2.1 conditions are applied as in the approved v2.2 review. Deviations kept from that review:

- GSM-pens stays in `tools` as a should-fire control. Its trap sibling is the math item.
- Sealing uses AES-256-GCM and scrypt through the repo's `cryptography` dependency instead of `age`.
- Held-out authoring starts at a 30-template minimum. The owner does that work.
- GSM-Symbolic stays disabled until the owner confirms non-commercial evaluation.
- A wall-clock hang guard exists so a stuck call cannot block an overnight run. Scoring uses token and call budgets. `hung` is not an accuracy gate by itself.
- CI writes a canary into a disposable vault. The owner's real vault is not written unless `--canary-real-vault` is set.

The hard-gate job is non-required (`continue-on-error` on the CI workflow so a red gate does not fail the required workflow). The fingerprint job is the check that must stay green. Making the hard-gate job required, with an empty fingerprint, is a stage-1 merge criterion.
