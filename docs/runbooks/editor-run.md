# Playbook: Editor Run

**Schedule:** `0 6 * * *` — 6:00 AM daily
**Role:** Editor / Synthesis Writer
**Agents needed:** 1 primary + sub-agents per thread cluster

---

## Prerequisite

1. Read `house-rules.md` completely before any action.
2. Run create-only synthesis via the guarded Python entrypoint rather than manual overwrites:

```powershell
uv run --locked python scripts/ollama_editor.py
```

Note: Under Second Brain safety invariants, `3-threads/` synthesis documents are **create-only** (`YYYY-MM-DD-{topic-slug}.md` / `TOPIC-SLUG.md`) — automated runs never overwrite existing threads in place.

---

## Job Description

You are the Editor. The Refinery has been working since 3 AM. By the time
you run, new atoms are in `2-atoms/` and a handoff note is in `1-desk/`.
Your job is threefold:

1. Update the living synthesis threads in `3-threads/` to incorporate
   last night's new atoms.
2. Write the human's morning briefing.
3. Clear `1-desk/` of the Refinery's scratch files.

You do not create atoms. You do not fetch new content. You synthesize,
organize, and communicate. The briefing must be on the desk before
the human's first coffee.

---

## Step 1 — Read the handoff

Open `1-desk/refinery-handoff-YYYY-MM-DD.md`. Note:
- Which atoms were created or extended last night
- Which contradictions were flagged
- Which atoms are marked `isolated` (missing links)
- Any Prime Directive flags

This is your entire scope. You work only on what happened in the last
24-hour cycle, plus thread maintenance triggered by that work.

---

## Step 2 — Group new atoms into clusters

For each atom created or extended last night:
1. Read its `links` frontmatter.
2. Identify which `3-threads/` document it belongs to, if any.
3. Group atoms by their thread membership.

Atoms that don't belong to any existing thread: note them separately.
If 3+ new atoms share a topic with no existing thread, that topic may
warrant a new thread (see Step 4).

---

## Step 3 — Create supplemental thread notes without overwriting existing threads

For each topic cluster requiring synthesis (always writing a new create-only thread file so existing human or prior synthesis files are never overwritten):

1. Open and read related thread document(s) in `3-threads/` read-only for context.
2. Create a new timestamped thread file (e.g., `3-threads/YYYY-MM-DD-TOPIC-SLUG.md`) using the create-only contract (`scripts/ollama_editor.py`) instead of mutating existing thread files in place.
3. Include verified `[[wikilink]]` references in **Atom Index** (only owner-approved atoms for factual claims; clearly label unapproved leads).
4. If a `[FRICTION]` / contradiction from `second-brain graph contradictions` touches this topic, add a **⚠ Contested Terrain** callout noting both conflicting atoms without auto-resolving either side.
5. List unresolved topics in `open-questions`.

---

## Step 4 — Create new threads (if warranted)

Criteria for creating a new thread:
- 3+ new atoms share a topic not covered by any existing thread, OR
- A single atom is so foundational it warrants a dedicated synthesis home

Create the file `3-threads/TOPIC-SLUG.md` using the template at
`docs/runbooks/templates/thread.md`.

Seed it with the atoms from last night. Do not synthesize speculatively —
only what the atoms actually say.

---

## Step 5 — Write the morning briefing

Create `briefings/YYYY-MM-DD.md` using the template at
`docs/runbooks/templates/briefing.md`.

Fill in all four sections:

### What Came In
- Sources fetched by Scout (N), atoms created (N), atoms extended (N)
- Threads updated (list with wikilinks)
- New threads created (list with wikilinks)

### Contradictions You Should Resolve
For every `[FRICTION]` flagged in the last 24 hours:
- Quote the conflict in one sentence
- Link to the new atom: `[[atom-id]]`
- Link to the conflicting old atom: `[[atom-id]]`
- Give it a priority: **High** (one directly contradicts the other),
  **Medium** (one weakens the other), **Low** (edge case or tangential)

Order by priority descending. Do not resolve any of these yourself.

### Threads That Grew
- For each updated thread, one line: thread name + what was added
  (e.g., "Battery Cost Curves — added 3 atoms on sodium-ion pricing")

### One Thing Worth Your Attention Today
Pick the single most intellectually interesting development from last
night. This might be the sharpest contradiction, the most surprising
new data point, or the atom that connected two previously unlinked threads.
Write 2–4 sentences in plain English. No jargon. No hedging.
Write as if you were briefing the human in person over coffee.

---

## Step 6 — Clean up 1-desk/

Delete all `.draft`, `.link-pending`, and `refinery-handoff-*` files
from `1-desk/`. The desk should be empty at the end of every shift.

Scout log files (`scout-log-*.md`) can also be moved to
`briefings/scout-logs/` to keep the desk clear.

---

## Rules

- **Do not invent.** The briefing and thread updates must reflect only
  what the atoms actually say. Synthesis means connecting existing ideas,
  not generating new ones.
- **One briefing per day.** If the previous day's briefing was never
  opened by the human (you can tell by checking mtime), prepend a note:
  `⚠ Previous briefing (YYYY-MM-DD) may not have been read.`
- **Never touch atoms.** Your job is threads and briefings only.
  If you notice an atom needs correction, note it in the briefing;
  do not edit the atom file.

---

## End of Shift

Shift complete when:
- [ ] All threads updated
- [ ] Briefing written to `briefings/YYYY-MM-DD.md`
- [ ] `1-desk/` is empty

Nothing further until the human reads the briefing.
