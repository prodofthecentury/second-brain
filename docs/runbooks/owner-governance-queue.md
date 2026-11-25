# Owner Governance Queue Runbook

**Document Version:** 1.0.0  
**Target Actor:** `camer` (Vault Human Owner)  
**Governance Scope:** `brain/0-raw/inbox/`, `brain/2-atoms/`, `brain/5-playbooks/`, `brain/6-skills/`  
**Security Reference:** `AGENTS.md` Invariant 9, `docs/independent-review-2026-11-22-v1.0.0.md` §6.3

---

## 1. Architectural Guardrails & Governance Policy

Under Second Brain epistemic architecture and AI safety policies (`AGENTS.md` and `CODEX_CLI_GOAL.md` Invariant 9):
1. **Agents Cannot Fabricate Approvals**: Automated background agents, Refinery, Scout, and CLI subagents run untrusted or assistive operations. They can draft candidates, organize notes, fetch sources, and stage captures.
2. **Owner Verification Required**: All promotions to canonical atoms (`review_status: accepted`), playbook promotions (`brain/5-playbooks/`), executable skill deployments (`brain/6-skills/`), and quarantined inbox releases require cryptographic verification and explicit human approval by the vault owner (`--actor camer`).
3. **No Identity Spoofing**: Agents must never execute commands with `--actor camer` or forge owner approval signatures. This runbook provides the exact, pre-computed, verified CLI commands for human owner execution.

---

## 2. Quarantined Inbox Items (`brain/0-raw/inbox/`)

The following captures are quarantined in `brain/0-raw/inbox/` awaiting owner triage:

### Git Commit Captures (10 items)
- `git-commit-20261121-0ba732a3.md`
- `git-commit-20261121-6e6a5c12.md`
- `git-commit-20261121-92fc83e3.md`
- `git-commit-20261121-ad7d7b2a.md`
- `git-commit-20261122-137de2eb.md`
- `git-commit-20261122-759cdfa2.md`
- `git-commit-20261122-aee5ed92.md`
- `git-commit-20261122-cf77fd03.md`
- `git-commit-20261122-da1e517e.md`
- `git-commit-20261122-e0f48e97.md`

### Web Remote Ingestion Captures (9 `.intake` items in `brain/0-raw/`)
- `2026-11-23-beating-the-averages-ae1b2e08aa01.intake`
- `2026-11-23-bliki-monolith-first-f7af51116f79.intake`
- `2026-11-23-choose-boring-technology-f9c2faed5b01.intake`
- `2026-11-23-llm-powered-autonomous-agents-19472db6d6f4.intake`
- `2026-11-23-makers-schedule-managers-schedule-3a1b1c09395f.intake`
- `2026-11-23-microservices-87183b91a8a6.intake`
- `2026-11-23-software-20-229f3ea9a386.intake`
- `2026-11-23-the-unreasonable-effectiveness-of-recurrent-neural-networks-b574e6d28551.intake`
- `2026-11-23-things-i-dont-know-as-of-2018-overreacted-903ca4a521cc.intake`

### Owner Triage Commands
To inspect SHA-256 digests and triage any inbox item:
```powershell
# List all quarantined items and their SHA-256 prefixes in inbox
uv run --locked second-brain inbox list

# Inspect a specific capture (shows full 64-char SHA-256)
uv run --locked second-brain inbox inspect <filename>

# Promote capture to active processing (requires exact CAS SHA-256)
uv run --locked second-brain inbox promote <filename> --expected-sha256 <64-char-sha256> --actor camer

# Or reject capture with an auditable rationale
uv run --locked second-brain inbox reject <filename> --expected-sha256 <64-char-sha256> --rationale "Irrelevant or obsolete" --actor camer
```

---

## 3. Candidate Memory Atoms Review (`brain/2-atoms/`)

Refinery extracts atomic claims into `brain/2-atoms/` with:
- `review_status: pending`
- `claim_state: candidate`
- `citation_integrity: missing`

### Owner Review & Approval Workflow
```powershell
# List pending candidate atoms and their current SHA-256 digests
uv run --locked python scripts/memory_review.py --status pending

# Inspect one candidate atom and verify its exact byte-offset source anchor
uv run --locked python scripts/memory_review.py --atom <atom-filename.md>

# Approve atom via CAS (checks source byte anchor and logs to 0-raw/archive/decision-record.jsonl)
uv run --locked second-brain review transition approve <atom-filename.md> --expected-sha256 <64-char-sha256> --actor camer

# Or reject / request edits via CAS
uv run --locked second-brain review transition reject <atom-filename.md> --expected-sha256 <64-char-sha256> --actor camer
```

---

## 4. Skill Playbooks & Executable Skills Promotion

### Playbooks (`brain/5-playbooks/`)
Existing verified playbooks in `brain/5-playbooks/`:
- `index-rebuild-recovery.md`
- `parnas-modular-decomposition.md`

To inspect procedural candidates and promote a new CAS-locked Playbook:
```powershell
# View candidate procedural atoms and backing SHA-256 digests
uv run --locked second-brain playbooks candidates
uv run --locked second-brain playbooks list

# Promote a procedural atom into an active Playbook via CAS
uv run --locked second-brain playbooks promote --playbook-id <id> --title "<Title>" --atom <atom.md>:<64-char-sha256> --trigger "<Trigger>" --precondition "<Precondition>" --step "<Step 1>" --expected-output "<Output>" --failure-recovery "<Recovery>" --actor camer
```

### Executable Skills (`brain/6-skills/`)
Stage 4 Executable Skills provide sandbox-confined, deterministic procedural execution (`0` promoted skills currently in `brain/6-skills/`). To inspect Playbook readiness and promote a Stage 4 Executable Skill:
```powershell
# View verified Playbooks eligible for Executable Skill promotion
uv run --locked second-brain skills candidates
uv run --locked second-brain skills list

# Promote an active verified Playbook to a Stage 4 Executable Skill via CAS
uv run --locked second-brain skills promote <skill-id> --playbook-id <playbook-id> --expected-playbook-sha256 <64-char-playbook-sha256> --step-actions-file <steps.json> --actor camer
```

---

## 5. Canonical Atom Project Scoping Note

The 75 existing approved atoms in `brain/2-atoms/` have exact SHA-256 byte signatures anchored in:
- `brain/0-raw/archive/decision-record.jsonl`
- `brain/0-raw/archive/vault-ledger.jsonl`
- `docs/retrieval-evaluation-proposed.json` (v3.0.0 benchmark dataset, 90 queries)

If the owner explicitly wants to add `contexts: [project:second_brain]` to unscoped approved atoms, `--apply` safely preserves YAML lists, backs up originals to `2-atoms/.history/`, re-verifies source byte anchors, appends attested `decision-record.jsonl` entries, refreshes `lexical.sqlite3`, and commits a `vault-ledger.jsonl` checkpoint (note: any benchmark query in `docs/retrieval-evaluation-proposed.json` whose atom SHA-256 was previously pinned still requires owner re-pinning after migration):
```powershell
# Dry run check of atom tags
uv run --locked python scripts/owner_scope_migration.py --dry-run

# Apply scoping with interactive human owner authorization (updates decision log, lexical index & ledger checkpoint)
uv run --locked python scripts/owner_scope_migration.py --apply --actor camer
```

---

## 6. Ledger & System Integrity Verification

After performing approvals and promotions, verify vault health:
```powershell
# Verify Merkle DAG ledger integrity
uv run --locked second-brain ledger verify

# Run full system diagnostics
uv run --locked second-brain doctor
```
