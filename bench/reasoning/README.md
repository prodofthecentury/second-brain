# Reasoning benchmark

Public dev suites, model-free safety gates, and the owner-only held-out tooling for the stage-6 harness. Nothing here is a measured model result.

The harness calls `scripts/reason_engine.py` and does not modify it. Every run uses a temporary vault. It never writes the owner's real vault unless `--canary-real-vault` is passed.

## Run

```bash
uv run python -m scripts.reason_bench validate
uv run python -m scripts.reason_bench run --tier ci-mock --backend mock
uv run python -m scripts.reason_bench run --tier pr --models hermes3:3b
uv run python -m scripts.reason_bench run --tier pr --models hermes3:3b --aa
second-brain reason-bench validate
./run_reason_bench.sh validate
```

PowerShell 5:

```powershell
.\run_reason_bench.ps1 -Tier pr -Models hermes3:3b
```

Exit 0 is pass or warn. Exit 1 is a HARD gate or a calibrated FAIL. Exit 2 is a config, hash, isolation, identity, or contract error.

`hermes3:3b` is the main target. Runs are CPU-only and model-agnostic: the client is a raw Ollama or OpenAI call, not `chat_with_fallback`.

## What CI checks

- `validate` checks schemas, references, solvers, hidden tests, canonical hashes, and forbidden held-out paths.
- `hard-gates` is absolute zero, in both SymPy modes, for `tool_wrong`, `claimed_verified_wrong`, `hijack_fires`, `tool_precision`, `tool_crash`, `engine_exception`, and every integrity failure (`manifest_ok`, `commitment_ok`, `isolation`, `canary`, `harness_errors`). A should-fire decline is not in that set.
- `hard-failure-fingerprint` runs the checker from the pinned harness. The pin is the Actions variable `PINNED_HARNESS_SHA`, or `.github/harness-pin` on the base branch. A missing pin exits 2. The job builds its environment from the pinned `uv.lock`.
- Replacing the fingerprint step with a no-op is not visible to that checker, because `pull_request` runs the workflow file from the pull request. Code-owner review plus the required checks are what stop that replacement.
- A Windows job checks out with `core.autocrlf=true` and re-runs `validate`.

## Gold notes

A single reduced `a/b` with no other operator and no conversion asked is an echo and must decline. `What is 2/3?`, `24/7`, `12/25` and `9/11` are that shape. A reducible `a/b` is computed: `100/4` is 25 and `50/50` is 1. `11 / 2 as a decimal` is 5.5. A question with a compute prefix is arithmetic: `What is 2026 -11?` is 2015 and `What is (10) -4?` is 6. Bare `2026 -11`, `(2026 -11)`, and `(10) -4` must not fire. A mixed-case fold of a whitelisted unit (`kM`, `cM`, `dM`, `kG`, `mI`) must decline. `KM` and `Km` to m are 1000. `CM` and `Cm` to m are 0.01.

## Two gates

HARD is absolute zero in both SymPy modes for `tool_wrong`, `claimed_verified_wrong`, `hijack_fires`, `tool_precision`, `tool_crash`, `engine_exception`, and every integrity failure. `should_fire_declined` is not a HARD tuple.

Recall is a separate gate. It fails when any should-fire item the pinned baseline answered now declines, when recall in either mode is below 122/150, or when the two SymPy modes disagree on any should-fire item. Every decline is printed by id.

The base units `m`, `g` and `s` are lower-case. An uppercase base-unit letter is not an SI symbol and must decline; any answer is a failure. The case-insensitive whitelist is `KM`, `KG`, `LB`, `OZ`, `FT`, `MI`, `MIN`, `CM`, `DM` and `KS`. Properly cased `Mm`, `Mg`, `Ms`, `Pm`, `Gm`, `Pg` and `Ps` are exact conversions.

## One-time bootstrap

1. The harness pin is the Actions variable `PINNED_HARNESS_SHA`, or the base file `.github/harness-pin`. It is not a line in `ci.yml`. `ci.yml` is compared with that line masked, so filling a pin value in is not a workflow rewrite. Editing any other workflow step fails the checker.
2. Suite bumps come from `bench/reasoning/owner-approvals.json` on the base branch. A signed tag counts only when `git verify-tag --raw` reports the fingerprint in `.github/owner-signing-fingerprint` on the pinned commit. Any other key is rejected. The committed fingerprint file is `none`, so no tag is trusted until that file names a key.
3. An engine-surface change (`scripts/**` other than tests, `config/reasoning.json`, `pyproject.toml`, `uv.lock`) is absolute zero: 0 HARD tuples in both SymPy modes, using the pull request's helpers and config and the pinned suite and `reason_bench` modules.
4. `expected-hard-failures.f24d020.json` is a non-regenerable record. It contains fatal `engine_exception` tuples, and `fingerprint --write` refuses those. Do not regenerate it.
5. After the engine is at 0 HARD under the pin, the 0-tuple baseline regenerated at that commit is `bench/reasoning/baseline/fingerprint.json`. Later pulls are judged against that regeneration. Residue is not accepted.

GSM-Symbolic stays off unless the owner types the confirmation at an interactive TTY after `--external gsm-symbolic`. A CLI string does not count. If the dataset card's licence is not `cc-by-nc-nd-4.0`, the run exits 2. Dev notes use `claim_state: synthetic_benchmark`. Nothing in this harness writes an owner approval.

## Held-out rules

The owner writes private templates on their own machine, outside this repo and outside `/workspace`. Agents must not author, read, or commit held-out plaintext.

1. Draft templates with the kit below. Keep them in `%USERPROFILE%\SecondBrainPrivate\heldout\templates\`.
2. Seal them: `uv run python -m scripts.reason_bench heldout-seal-templates --in THAT_DIR --out templates.sbsealed`. The command asks for a passphrase twice on a TTY and then deletes the plaintext folder. There is no passphrase flag, environment variable, or file.
3. Check disjointness: `heldout-check-disjoint --templates-sealed templates.sbsealed`. A 4-gram Jaccard of 0.5 or more fails. Commit only `{n_private_templates, max_jaccard, passed}`.
4. Generate: `heldout-generate --templates-sealed templates.sbsealed --out h1.sbheldout`. The sealed file holds the items, hidden tests, corpus, seed, and salt. Plaintext is not written.
5. Put the resulting commitment in `heldout.commitment.json` before any scored run. One held-out run per stage PR, and only after `preregistration/<stage>.md` is committed.

The commitment in the repo today is the empty placeholder: `sha256(32 zero bytes || empty canonical JSONL)`. Replace it when a real set exists. The salt of a real set stays inside the sealed file.

`heldout-results/` receives aggregates only. Item ids in those files are `HMAC-SHA256` digests. The access log is honour-based. The passphrase stops accidental reads. It cannot prove that nobody looked.

Do not put sealed files, held-out JSONL, or private templates in a cloud-sync folder or in this repo.

## Template kit

A public dev template is a Python module:

```python
TEMPLATE_ID = "t999"
ANSWER_TYPE = "integer"  # or fraction, entity, ...
TAGS = ["everyday"]
DIFFICULTY = 1
INSTANCES = 2
SKIP_NUMBER_CHECK = False

def sample(rng):
    return {"a": rng.randint(2, 20), "b": rng.randint(2, 20)}

def pinned(instance):
    return {"a": 4, "b": 5} if instance == 0 else None

def render(params):
    return f"A box holds {params['a']} red tokens and {params['b']} blue tokens. How many tokens are there?"

def solve(params):
    return params["a"] + params["b"]  # int, not bool

def reference_for(params):
    return f"ANSWER = {params['a']} + {params['b']}"

def build_item(instance):
    ...  # id math-dev-<template>-<instance>, split dev, approx_ok None
```

`validate` samples `sample` 200 times. `solve` must be deterministic. Integer answers must be `int`. The rendered question must contain every sampled number, except a `render_missing_info` variant. Logic templates return exactly one solution from `solver`. Code tasks ship `reference_solution`, `visible_tests`, and `hidden_tests` and may import only the sandbox allowlist.

Held-out templates use the same `sample` / `render` / `solve` surface. Set `provenance.source` to `owner-curated` and `llm_drafted` to true only when the owner drafted them in a session that shares no memory with the coding agents. Do not use a benchmarked model to write them.

Rebuild committed dev JSONL with:

```bash
uv run python -m scripts.reason_bench generate --suite all
uv run python -m scripts.reason_bench lock --bump-version 2.2.0 --reason "regenerated dev suites"
```
