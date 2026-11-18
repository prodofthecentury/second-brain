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

### Step 3 — Fetch the content

Use WebBridge (or system browser) to open the URL in the real browser,
inheriting the user's existing cookies and session. Extract:

- Full article body text (not just the visible fold)
- Title
- Author(s) if shown
- Publication date if shown
- Word count

If the page is paywalled and no content loads after login: mark `[!]` and
log the failure. Never generate placeholder content for missing pages.

### Step 4 — Save to sources/

Write a new file: `sources/YYYY-MM-DD-{slug}.md`

Where `{slug}` is a URL-safe lowercase version of the title (max 60 chars).

Frontmatter format:

```yaml
---
url: https://...
title: "Full article title"
author: "Author name(s) or Unknown"
publication_date: YYYY-MM-DD or "Unknown"
captured_date: YYYY-MM-DD
word_count: N
scout_status: fetched
---
```

Body: the full article text, verbatim. No editing. No summarizing.

### Step 5 — Drop intake pointer

Create a one-line pointer file in `0-raw/`:

Filename: `YYYY-MM-DD-{slug}.intake`

Content:
```
source: sources/YYYY-MM-DD-{slug}.md
queued_for_refinery: true
```

This is what the Refinery Run watches for. Without this pointer, the
Refinery will not know a new source is ready.

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
