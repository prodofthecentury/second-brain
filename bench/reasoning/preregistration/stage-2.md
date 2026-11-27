# Stage 2 held-out pre-registration

No held-out run has been executed. The owner has not produced a held-out set yet. This file is the required commitment of the comparison **before** any run.

- Engine git sha under test: `f24d0200f0a9918cdb7ef71aa21d1cbc811d3ebf` until the stage-1 fix lands. Record the sha actually tested in the run.
- Model: `hermes3:3b` (CPU, raw Ollama client).
- Primary comparison: **deep − plain**, overall accuracy, template-cluster paired bootstrap, 95%, on held-out.
- Decision rule: a "stage improves accuracy" claim needs the lower bound > 0. "No regression" needs the lower bound > −`fail_threshold_pp`.
- Secondary comparisons, Holm-adjusted and exploratory: deep − plain_sc@k, deep − deep_no_tools, deep_no_tools − plain, deep − fast, oracle − deep on multihop, per-suite deltas, and `p_correct_given_verified`.
- Floors for the lower CI bound of P(correct | verified) on a live run: 0.95 for `tool_exact`, 0.80 for `model_claimed_verified`.
- At most one held-out run per stage PR. A second run needs a new pre-registration and counts toward retirement.

Dev results are exploratory. Absolute accuracy on public dev is not a claim.

## Public dev gates

Two gates, kept apart.

HARD is absolute zero in both SymPy modes for `tool_wrong`, `claimed_verified_wrong`, `hijack_fires`, `tool_precision`, `tool_crash`, `engine_exception`, and every integrity failure (`manifest_ok`, `commitment_ok`, `isolation`, `canary`, `harness_errors`). A should-fire decline is not in that set.

Recall fails the run when any of these happen:

- an item the pinned baseline answered correctly now declines (per item, not a hidden average)
- recall in either mode is below 122/150
- the two SymPy modes disagree on any should-fire item

Every decline is listed by id. A compute prefix is a request for arithmetic (`What is 2026 -11?` is 2015). A bare `2026 -11` or `(2026 -11)` must not fire. A single reduced `a/b` with no other operator and no conversion is an echo and declines. A reducible `a/b` (`100/4`, `50/50`) is computed.
