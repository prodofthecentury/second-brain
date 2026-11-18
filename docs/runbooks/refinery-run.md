# Playbook: Refine one capture

The Refinery drafts candidate atoms from one raw capture. It does not decide
truth, approve claims, edit existing atoms, or delete source material.

## Before starting

1. Run `uv run scripts/doctor.py` and read every warning. A stale or missing
   lexical index must be rebuilt before recall is used.
2. Choose one specific file from `0-raw/`. Leave remote `.intake` pointers
   quarantined unless the owner has explicitly promoted that exact pointer.
3. Do not run parallel Refinery processes on the same capture.

## Process one capture

```powershell
uv run scripts/ollama_refinery.py --file brain/0-raw/<capture.md> --keep-source
```

The source stays in place with `--keep-source`. New atom files are created
without overwriting existing files and start as `review_status: pending`,
`claim_state: candidate`, and `citation_integrity: missing`. The system stamps
the intake origin; model-authored trust fields are discarded.

If generation fails, stop. Do not write a fallback atom or mark the capture
processed. Fix the local model/runtime issue and retry the same capture.

## Review candidates

Inspect the atom and its named source. The evidence section must contain an
exact excerpt that appears in one unambiguous location in that source. Then
review the exact file revision:

```powershell
uv run scripts/memory_review.py --status pending
Get-FileHash brain/2-atoms/<atom.md> -Algorithm SHA256
uv run scripts/review_atom.py approve <atom.md> --expected-sha256 <sha256> --actor <owner>
```

Approval checks the source bytes and quote, records the atom revision hash, and
rebuilds the lexical index. An anchored quote proves that the excerpt exists;
the owner still decides whether it supports the claim. Any later edit makes the
old approval ineligible for recall.

## Preserve originals

- Never overwrite an existing atom or source file.
- Keep raw captures, external response evidence, and atom revision history.
- Do not resolve contradictions, merge, supersede, or delete memories in this
  playbook.
- Do not start the daemon, Scout, or a batch shift until the owner has reviewed
  the queued work and the relevant runtime gate is ready.
