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
- `hard-gates` runs the absolute gates and is non-required. At engine `f24d020` it is red on purpose.
- `hard-failure-fingerprint` requires the observed `(gate, item_id, sympy_mode)` set to equal `expected-hard-failures.f24d020.json`. That file may only shrink.
- A Windows job checks out with `core.autocrlf=true` and re-runs `validate`.

GSM-Symbolic stays off unless the owner passes `--external gsm-symbolic --owner-noncommercial-confirm "<statement>"`. A confirmation from an agent does not count. If the dataset card's licence is not `cc-by-nc-nd-4.0`, the run exits 2.

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
