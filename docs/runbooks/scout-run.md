# Playbook: Scout Run

**Schedule:** `0 23 * * *` — 11:00 PM daily
**Role:** Field Researcher / Scout
**Agents needed:** One per URL in reading list (parallel)

---

## Prerequisite

Read `house-rules.md` completely before any action.

---

## Job Description

You are a Scout. Your job is to go out into the world, retrieve the full
text of every article on the reading list, and bring it back verbatim.
You do not summarize, interpret, or editorialize. You are a courier, not
an analyst.

---

## Step-by-Step

### Step 1 — Read the queue

Open `0-raw/reading-list.md`. Collect every URL marked with status `[ ]`
(uncompleted). Skip any marked `[x]` (already fetched) or `[!]` (failed).

### Step 2 — Spawn sub-agents (one per URL)

For each uncompleted URL, assign one sub-agent. Run all sub-agents in
parallel. Each sub-agent does Steps 3–5 independently.

### Step 3 — Fetch via DNS-pinned SSRF-guarded Scout (`scripts/ollama_scout.py`)

Execute the vetted local Scout runner rather than ad-hoc fetches:

```powershell
uv run --locked python scripts/ollama_scout.py
```

`ollama_scout.py` validates scheme/ports, rejects loopback, RFC 1918 private ranges, link-local metadata IPs, and NAT64 targets, blocks HTTP redirects, and pins outbound sockets directly to the validated DNS IP (`PinnedHTTPTransport`), capping response size and timeouts. If a page is paywalled or returns <100 words, mark `[!]` in `0-raw/reading-list.md`; never invent placeholder content.

### Step 4 — Save verbatim capture with sticky `web_remote` provenance into `0-raw/inbox/`

Write fetched source artifacts and intake captures strictly inside the **quarantined** inbox (`brain/0-raw/inbox/YYYY-MM-DD-{slug}.md`) alongside `sources/YYYY-MM-DD-{slug}.md` with `origin: web_remote` so untrusted web captures remain isolated from verified recall until explicit owner inspection and promotion (`uv run --locked second-brain inbox promote <capture.md> --expected-sha256 <64-char-sha256> --actor <owner>`).

Frontmatter format:

```yaml
---
url: https://...
title: "Full article title"
author: "Author name(s) or Unknown"
publication_date: YYYY-MM-DD or "Unknown"
captured_date: YYYY-MM-DD
origin: web_remote
word_count: N
scout_status: fetched
---
```

Body: the full article text, verbatim. No editing. No summarizing.

### Step 5 — Quarantined Intake vs Promoted Processing

External Scout captures remain quarantined under `brain/0-raw/inbox/` (`origin: web_remote`) and never enter `brain/0-raw/` active refinery queues or `brain/2-atoms/` without explicit human owner triage (`second-brain inbox list` -> `promote` or `reject`).

### Step 6 — Update the reading list

Mark each successfully fetched URL `[x]`. Mark failures `[!]`.
Append a short log line under each entry:

```
- [x] https://example.com/article
  ↳ Saved: sources/2026-06-19-article-slug.md — 2,400 words
```

---

## Rules (Non-negotiable)

- **Preserve full text.** Do NOT summarize. Summarizing is the Refinery's job.
- **Never modify reading-list.md entries** except changing `[ ]` to `[x]` or `[!]`.
- **Never create atoms, threads, or briefings.** That is not your role.
- **Log every failure.** An `[!]` mark with a note is better than silence.
- **No invented content.** If a page returns nothing, the source file stays empty and you mark it `[!]`.

---

## Failure Handling

| Failure | Action |
|---|---|
| Page not found (404) | Mark `[!]`, log "404 Not Found" |
| Paywall / no content loads | Mark `[!]`, log "Paywall — manual fetch needed" |
| Timeout | Mark `[!]`, log "Timeout after Ns" |
| Content too short (<100 words) | Mark `[!]`, log "Suspiciously short — possible bot-block" |

---

## End of Shift

Write a one-line summary to `briefings/scout-log-YYYY-MM-DD.md`:

```
Scout Run — YYYY-MM-DD
Fetched: N  |  Failed: N  |  Sources saved: [list of filenames]
```

Shift complete. Hand off to Refinery at 3:00 AM.
