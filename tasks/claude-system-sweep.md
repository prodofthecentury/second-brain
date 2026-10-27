# External Red-Team System Sweep

Conduct a final read-only release review of the actual repository. Do not write files, run commands, use network, or soften findings. This is an independent sweep after three design passes.

## Independently measured runtime evidence

- `uv run scripts/smoke_test.py` passed: `SMOKE_TEST_OK`.
- `uv run python -m compileall -q scripts dashboard chat_with_memory.py` passed.
- `uv run scripts/memory_review.py --status all --json` parsed all 13 atoms.
- After starting the already-installed Ollama server, `doctor.py --json` exited 0: it found `llama3.2:3b`, `nomic-embed-text`, and `qwen3.6:latest`; Chroma persistent client remains unavailable and the JSON fallback is active. It warns that `llava` is absent but names `qwen3.6:latest` as a vision fallback; it reports 23 raw captures and 14 queued URLs.
- A direct local API probe succeeded: `llama3.2:3b` returned exactly `HEALTHY` and `nomic-embed-text` returned one 768-dimensional vector.
- The dashboard **launcher is defective**: `run_dashboard.ps1` uses `uv run --with … streamlit …`, which selected Python 3.14.6 even though `dashboard/app.py` declares `>=3.11,<3.12`; after 90 seconds it never listened on port 8501. A controlled equivalent with `uv run --python 3.11 --with … streamlit run dashboard/app.py` did listen on 8501. Do not mistake that controlled validation for a fixed launcher.

## Architecture contract being proposed, not yet implemented

Three durable stores; atomic per-atom frontmatter writes plus snapshot/backup, no ledger in v1; a separately rebuilt SQLite FTS/vector/telemetry sidecar; fail-closed embedding/index generations; source/citation integrity; coordinated re-gating after every human edit; structural answer provenance; quarantine and scoped local MCP; no real-time extraction or heavy model features on this hardware. These are known required upgrades, not completed work.

## Sweep task

Review the current code against the runtime evidence and that contract. Produce:

1. A strict release verdict: `not releasable`, `conditionally releasable`, or `releasable`, with the exact meaning of the status.
2. Only Critical/Required findings that are directly supported by source or measured evidence, prioritized.
3. A minimal re-verification sequence after each required repair. Include exact failure-injection cases that must pass before calling the system trusted.
4. A short sweep of the preceding architecture contract itself: name any remaining claim that is unproven or cannot be guaranteed on the current hardware.

Do not propose unrelated features. Do not call this clean merely because smoke tests pass.
