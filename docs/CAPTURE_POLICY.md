# Capture policy

This is the rule for what an agent may put in a note or a memory atom. The machine-readable copy is `docs/capture_policy.json`. Distillation, approval, and the MCP write tools load that file. Do not keep a second copy of the thresholds.

An atom is one durable, checkable, self-contained fact. The owner approves it later. Nothing in this policy auto-approves an atom.

## What qualifies

- Decisions, including a `Decision:` line from a meeting.
- Config values, ports, paths, hosts, and protocol lines, with the block name when the line sits inside one.
- Dates and deadlines written as absolute dates (`27 Nov 2026`), not `tomorrow`.
- People and roles (`Dr. Hale`, `Priya owns the staging check`).
- Preferences the owner states as a preference.
- Procedures and steps that say who does what.
- Todos that name an owner or a date.
- Measurements and results (`~2 tok/s on CPU`, `14 days of copies`).

## What does not make the cut

- Mood or feelings with no checkable fact (`I feel worn out by backups`).
- Filler and transient chatter.
- Headings, list markers, checkbox syntax, table header and separator rows, code fences, and lone braces.
- Truncated fragments (`Rotation to.`, `The hint is the.`, `should grab.`).
- Speculation (`I think`, `maybe`, `probably`) unless the text is explicitly marked tentative.
- Duplicates of a fact already captured.
- Secrets, passwords, API keys, and tokens.
- Relative dates (`tomorrow`, `yesterday`, `tonight`, `next week`, `next month`).
- A first-person sentence with no number, date, proper noun, decision, path, or other checkable token (`I keep forgetting.`).
- A secret written with a space (`password is swordfish`, `api key sk-test123`) or a token prefix such as `sk-`, `ghp_`, `xox`, or `AKIA`.

A heading date, block label, or speaker may be stored as context with its own verbatim span. The evidence quote then has to support only the fact, not repeat that context. A short config line with no sentence punctuation is still a fact.

## Atom shape

- One fact per atom.
- The subject is stated in the claim. A speaker label such as `Priya:` becomes the subject. The evidence span stays verbatim, including list or checkbox markup.
- At most 80 words in the claim.
- Absolute dates, not relative ones.
- Evidence is a verbatim span of the source note.
- The atom names its source note.
- Uncertain facts are marked tentative and stay pending. Never auto-approve them.

## Examples

Good:

- `The dashboard listens on port 8501.`
- `Decision: the public beta stays paused until the staging rate limit is raised.`
- `Saw Dr. Hale at 9:30 on 27 Nov 2026.`

Bad:

- `I feel worn out by backups.`
- `password: hunter2`
- `Rotation to.`
- `Ship the beta tomorrow.`

Agents can read the same policy from the local MCP resource `brain://policy/capture`.
