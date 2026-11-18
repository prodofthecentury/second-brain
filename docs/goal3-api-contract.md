# Goal 3 Loopback API Contract (client-facing additions)

All routes live on `http://127.0.0.1:<port>` behind `Authorization: Bearer <api-token>`;
owner-only routes additionally require `X-Owner-Token: <owner-token>` (403 otherwise).
Every list route supports `?limit=<1..500>&offset=<0..>` and returns
`{"total": N, "count": n, "offset": o, "limit": l, "items": [...]}` (legacy keys kept).

## Port discovery
* `SecondBrain/security/api-port` (same directory as `api-token`) is written atomically by
  `scripts/memory_api.py serve` once bound: JSON `{"port": 51234, "pid": 9876, "host": "127.0.0.1", "started_at": "<iso>"}`.
  It is removed on clean shutdown only when `pid` matches. Clients: if user/application
  setting `apiPort` is unset/default (8765), read this file, verify `pid` is alive when possible,
  and fall back to 8765.
* Env override: `BRAIN_API_PORT_FILE`.

## Chat — `POST /v1/chat/stream` (SSE `text/event-stream`, `data: <json>\n\n`)
Body: `{query, mode: instant|verified|stream, project_scope?, model_override?, include_preferences?,
host_id?: "ollama_local", retrieval_mode?: auto|lexical|dense|rrf, answer_view?: standard|preference|research}`.
Events (`type`): `context` (`verified[]`, `preferences[]`, `notes[]`, `leads[]`, `sources[]` with
`tier` ∈ verified|preference|note|unverified_lead and `citation_id` V#/P#/N#/L#, `retrieval_mode`,
`answer_view`, `host_id`, `retrieval_ms`, `warning`), `research_leads` (owner + research only:
`label`, `text`, `leads[]` — render as a visibly separate "UNVERIFIED LEADS — NOT OWNER-APPROVED"
block, never as fact), `token` (`text`), `verified_answer` (`answerable`, `text`, `claims[]`),
`grounding` (`cited_sources`, `invalid_citations`, `uncited_sentences`, `fully_cited`),
`abstain` (`text`), `error` (`error`), `done` (`total_ms`). Clients abort via `AbortController` /
`req.destroy()`; the server tolerates disconnects.

## Notes (existing)
`GET /v1/notes?limit&offset&q&tag`, `GET/PUT/DELETE /v1/notes/<fn>` (PUT/DELETE need `expected_sha256`,
owner), `POST /v1/notes` (owner) `{title, body, tags[], memory_type?}`, `POST /v1/notes/<fn>/verify`
(owner) `{excerpt, claim?, actor, expected_note_sha256, memory_type?, tags?}`.

## Atoms & review (existing)
`GET /v1/atoms?limit&offset` → items have `filename, claim, review_status, verified, sha256, contexts, memory_type`.
`GET /v1/review/queue?limit&offset` (owner). `POST /v1/atoms/<fn>/{approve,reject,demote,quarantine}`
`{expected_sha256, actor}`. `POST /v1/atoms/merge` `{survivor_filename, survivor_expected_sha256,
superseded_filename, superseded_expected_sha256, actor, rationale}`. `PUT /v1/atoms/<fn>`.

## Inbox (existing, owner)
`GET /v1/inbox/queue`, `GET /v1/inbox/<fn>` (→ `content`, `preview`, `risk_flags`, `sha256`),
`POST /v1/inbox/promote` `{filename, expected_sha256, actor, acknowledge_risks}`, `POST /v1/inbox/reject`.

## Playbooks
`GET /v1/playbooks?include_inactive=1`, `GET /v1/playbooks/candidates` (owner; items include `claim`,
`grounding_units[]` = exact step strings that pass `skill_playbooks._claim_grounding_units`),
`POST /v1/playbooks/promote` (owner) `{playbook_id, title, trigger, steps[], backing_atoms[], actor,
preconditions?, expected_outputs?, failure_recovery?, contexts?, allow_multi_project?,
expected_playbook_sha256?}` — `expected_playbook_sha256` is REQUIRED when re-promoting/updating an
existing playbook (409 on mismatch). `POST /v1/playbooks/<fn>/deprecate` `{actor}`.

## Ledger
`GET /v1/ledger/verify` (now includes `entries[]` checkpoint history: `seq, timestamp, actor, note,
merkle_root, leaf_count, approved_atoms, accepted_playbooks, entry_sha256`), `GET /v1/ledger/history?limit&offset`,
`POST /v1/ledger/commit` (owner) `{actor, note}`, `GET /v1/ledger/prove/<ref>` where `<ref>` is
`<atom>.md`, `2-atoms/<atom>.md`, `5-playbooks/<pb>.md` or `playbook:<pb>.md`, `POST /v1/ledger/verify-proof`.
`GET /v1/ledger/leaves?limit&offset` → `items[] = {rel_path, kind: atom|playbook}` (proof selectors).

## Audit
`GET /v1/audit/sources` (owner) → `stale_count`, `reports[]` each with `stale_approval: bool`, `action`.
`POST /v1/audit/sources/demote-stale` (owner) `{actor}` → `demoted_count`, `demoted[]`.

## Graph
`GET /v1/graph/summary?project=`, `GET /v1/graph/full?project=&include_pending=0|1&limit=` →
`{nodes:[{id, label, review_status, memory_type, contexts, verified}], edges:[{from, to, kind}]}`,
`GET /v1/graph/neighbors/<fn>`, `GET /v1/graph/contradictions`.

## Snapshots
`GET /v1/snapshots`, `POST /v1/snapshots` (owner), `POST /v1/snapshots/diff`, `POST /v1/snapshots/recall`
`{query, snapshot_id, limit?, project_scope?}`, `POST /v1/snapshots/verify` `{snapshot_id}` → `{valid, ...}`.

## Cognitive
`POST /v1/cognitive/feedback` (owner) `{atom_filename, feedback: endorsed|corrected|superseded, actor, note}`
— `superseded` is recorded as supersession (NOT a correction); `actor`/`note` persisted durably.
`POST /v1/cognitive/experiences` `{goal, result_summary, success, context[], actions[], tools_used[],
referenced_atoms[], lessons[], plan[], reasoning_summary?, confidence_before?, confidence_after?,
reflection?, candidate_procedure_title?, propose_to_inbox?}`.

## Vault browsers (owner)
`GET /v1/vault/threads`, `GET /v1/vault/experiences`, `GET /v1/vault/briefings` (paginated; items
`{filename, title, modified_at, size_bytes}`), `GET /v1/vault/{threads,experiences,briefings}/<fn>`
→ `{filename, content}` (render as text/Markdown; untrusted).

## Operator jobs (owner)
`GET /v1/scout/queue` → `items[] = {url, status}` (from `0-raw/reading-list.md`),
`POST /v1/scout/queue` `{url}` (http/https only; appended as `- [ ] <url>`),
`POST /v1/scout/run` `{max_items?: 1..20}`, `POST /v1/jobs/refinery`, `POST /v1/jobs/editor`,
`POST /v1/jobs/doctor` → `{job_id, status: "started"|"completed", ...}`; `GET /v1/jobs/<job_id>` →
`{job_id, kind, status: running|completed|failed, returncode?, output_tail}`. Jobs run as bounded
subprocesses (timeout 900 s) with `cwd` = the project root (never `resources/backend` when read-only).

## Import (owner)
`POST /v1/import` `{source_dir, tags[], dry_run, overwrite_modified}` — imports `.md .markdown .txt
.pdf .docx .epub .html .htm .csv .tsv`. `POST /v1/import/file` `{path, tags[]}` imports one document.
