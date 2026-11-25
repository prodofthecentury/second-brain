# Playbook: Audit Run

**Schedule:** `0 22 * * 0` — 10:00 PM every Sunday
**Role:** Quality Assurance / Integrity Auditor
**Agents needed:** 1 (sequential scan, no parallelism needed)

---

## Prerequisite

1. Read `house-rules.md` completely before any action.
2. Run deterministic pre-flight audits first (read-only; never auto-demote without owner confirmation):

```powershell
uv run --locked python scripts/audit_sources.py --check
uv run --locked second-brain ledger verify
uv run --locked second-brain graph contradictions
uv run --locked second-brain gates status
```

---

## Job Description

You are the Auditor. You run once a week and your job is to tell the
human exactly where the vault needs a human decision. You do not fix
anything. You do not make judgment calls. You report, precisely and
honestly, and you stop.

A vault that silently accumulates problems is more dangerous than an
empty vault. Your briefing is the early-warning system.

---

## Step 1 — Scan for orphan atoms

An **orphan atom** has no incoming or outgoing wikilinks.
These atoms are islands — they exist in the vault but contribute nothing
to the knowledge graph. They are usually the result of a Cataloger
creating an atom without finding related notes, or a link target that
was deleted or never created.

For each orphan found:
- Record its ID, filename, and creation date
- Record what the atom claims (one-sentence summary)
- Note when it was last reviewed

---

## Step 2 — Scan for stale unapproved candidate atoms

A **stale candidate atom** has `review_status: pending` (with `claim_state: candidate` or `citation_integrity: missing` / `needs_edit`) and a
`last-reviewed` date older than 14 days (or no `last-reviewed` date at all).

Pending candidate atoms are working extractions, not settled canonical memory. After
two weeks without owner review, they either need primary-source byte anchoring and CAS transition to
`review_status: accepted` (`claim_state: human_reviewed`) or flagging for human check-in.

For each stale pending candidate atom found:
- Record its ID and claim
- Note how many days since last review
- Note whether it has been linked to from any threads

---

## Step 3 — Scan for unresolved FRICTION blocks

An **unresolved [FRICTION] block** is any `[FRICTION]` section in any
atom where the `Status` line still reads "Awaiting human judgment."

Track the `Flagged` date. If it is 7 or more days old, it is overdue.

For each overdue FRICTION:
- Record the new atom (the one containing the block)
- Record the conflicting existing atom
- Record the nature of the conflict
- Note how many days it has been unresolved

---

## Step 4 — Scan for Prime Directive breaches

A **Prime Directive breach** is any atom in `2-atoms/` that has:
- An empty `sources` field
- A `sources` field pointing to a file that does not exist in `sources/`
  or `0-raw/`
- A `sources` field pointing to another atom (not an original source)

These are the most serious finding. An atom without a traceable source
is a fabrication risk. Flag every single one.

---

## Step 5 — Scan for stale threads

A **stale thread** is any document in `3-threads/` with a
`last-updated` date older than 30 days.

Threads that are not being maintained may be:
- Genuinely complete (in which case they should be marked `status: complete`)
- Abandoned topics that no longer matter
- Topics that the Refinery has been populating with new atoms but the
  Editor has not been updating the thread for

For each stale thread:
- Record its name and topic
- Record days since last update
- Note how many atoms in `2-atoms/` link to it
- Flag if atoms were created in the last 30 days that should belong to it
  but were never added

---

## Step 6 — Scan for broken wikilinks

A **broken wikilink** is a `[[reference]]` in any atom or thread that
does not correspond to any file in `2-atoms/` or `3-threads/`.

These are usually the result of a renamed file, a note that was archived
without updating links, or a speculative link the Cartographer created
before the target atom existed.

List all broken links and their source files.

---

## Step 7 — Write the audit briefing

Create `briefings/audit-YYYY-MM-DD.md`.

Format:

```markdown
# Weekly Audit — YYYY-MM-DD

## Summary
- Orphan atoms: N
- Stale tentative atoms: N  
- Overdue FRICTION blocks: N
- Prime Directive breaches: N  ← most critical
- Stale threads: N
- Broken wikilinks: N

**Vault health score:** [Good / Needs Attention / Critical]
- Good: no Prime Directive breaches, <5 total issues
- Needs Attention: no Prime Directive breaches, 5–20 issues
- Critical: any Prime Directive breaches, or >20 issues

---

## 🚨 Prime Directive Breaches (act on these first)
[list each]

## ⚠ Overdue FRICTION Blocks
[list each, oldest first]

## Orphan Atoms
[list each]

## Stale Tentative Atoms
[list each]

## Stale Threads
[list each]

## Broken Wikilinks
[list each]

---

## Recommended Actions
1. [Most urgent action for the human]
2. [Second]
3. [Third]
(Maximum 5 recommendations. Specific, actionable, one sentence each.)
```

---

## Rules

- **Report only.** Fix nothing automatically. Not a single file.
- **Be precise.** Vague warnings are useless. Every item in the briefing
  must name the specific file.
- **Be honest about severity.** If there are 40 Prime Directive breaches,
  say so plainly. Do not soften.
- **Never skip a section** even if the count is zero. A zero count is
  reassuring and should be visible.

---

## End of Shift

Briefing written. Vault untouched. Done until next Sunday.
