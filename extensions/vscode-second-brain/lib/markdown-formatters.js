"use strict";

function computeClaimGroundingUnits(claimText) {
  const raw = String(claimText || "");
  const ordered = [];
  const seen = new Set();

  function addUnit(text) {
    const norm = String(text || "")
      .trim()
      .split(/\s+/)
      .filter(Boolean)
      .join(" ");
    if (norm.length >= 5 && !seen.has(norm)) {
      seen.add(norm);
      ordered.push(norm);
    }
  }

  const lines = raw.split(/\r?\n/);
  for (const rawLine of lines) {
    const stripped = rawLine.trim();
    if (!stripped) continue;
    const bulletMatch = stripped.match(/^(?:[-*]|\d+[.)])\s+(.*)$/);
    const lineUnit = (bulletMatch ? bulletMatch[1] : stripped)
      .split(/\s+/)
      .filter(Boolean)
      .join(" ");
    if (!lineUnit) continue;
    const sentences = lineUnit.split(/(?<=[.!?])\s+/);
    if (sentences.length > 1) {
      for (const sentence of sentences) {
        addUnit(sentence);
      }
    }
    addUnit(lineUnit);
  }

  const full = raw
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .join(" ");
  if (full) {
    const fullSentences = full.split(/(?<=[.!?])\s+/);
    for (const sentence of fullSentences) {
      addUnit(sentence);
    }
    addUnit(full);
  }

  return ordered;
}

function formatPlanMarkdown(plan, fallbackGoal = "") {
  const ready = Boolean(plan?.plan_ready);
  const lines = [
    `# Executive Plan: ${plan?.goal || fallbackGoal}`,
    "",
    `- **Status:** ${ready ? "READY (Citation-Grounded)" : "ABSTAINED (Insufficient Verified Memory)"}`,
    `- **Mode:** \`${plan?.mode_used || plan?.mode || "deterministic"}\``,
    `- **Retrieval Mode:** \`${plan?.retrieval_mode || "auto"}\``,
    `- **Summary:** ${plan?.summary || "No summary returned."}`,
  ];
  if ((plan?.matched_playbook_ids || []).length) {
    lines.push(
      `- **Matched Playbooks:** ${plan.matched_playbook_ids.map((id) => `\`${id}\``).join(", ")}`
    );
  }
  if ((plan?.working_context_atom_ids || []).length) {
    lines.push(
      `- **Working Context Atoms:** ${plan.working_context_atom_ids.map((id) => `\`${id}\``).join(", ")}`
    );
  }
  lines.push("");

  if ((plan?.preconditions || []).length) {
    lines.push("## Preconditions");
    for (const p of plan.preconditions) lines.push(`- ${p}`);
    lines.push("");
  }

  const steps = plan?.steps || [];
  if (steps.length) {
    lines.push("## Grounded Execution Steps");
    steps.forEach((step, idx) => {
      const provParts = [];
      if (step.source_playbook_id) provParts.push(`playbook:${step.source_playbook_id}`);
      if ((step.backing_atom_ids || []).length) {
        provParts.push(`atoms:${step.backing_atom_ids.join(",")}`);
      }
      const prov = provParts.length ? ` _(${provParts.join("; ")})_` : "";
      const cites = (step.citations || []).length
        ? ` — Citations: \`${step.citations.join("`, `")}\``
        : "";
      lines.push(`${idx + 1}. **${step.instruction || ""}**${prov}${cites}`);
    });
    lines.push("");
  }

  if ((plan?.expected_outputs || []).length) {
    lines.push("## Expected Outputs & Verification");
    for (const out of plan.expected_outputs) lines.push(`- ${out}`);
    lines.push("");
  }

  if ((plan?.rollback_checkpoints || []).length) {
    lines.push("## Rollback Checkpoints");
    for (const rb of plan.rollback_checkpoints) lines.push(`- ${rb}`);
    lines.push("");
  }

  if ((plan?.preference_constraints || []).length) {
    lines.push("## Owner Preference Guardrails");
    for (const pref of plan.preference_constraints) lines.push(`- ${pref}`);
    lines.push("");
  }

  if ((plan?.epistemic_gaps || []).length) {
    lines.push("## Epistemic Gaps & Notices");
    for (const gap of plan.epistemic_gaps) lines.push(`- ${gap}`);
    lines.push("");
  }

  return lines.join("\n");
}

function formatRadarMarkdown(data) {
  const alerts = data?.alerts || [];
  const drift = data?.drift_alerts || [];
  const orphans = data?.orphan_approved_atoms || [];

  const lines = [
    "# Contradiction & Source Drift Radar Report",
    "",
    `- **Contradiction Alerts:** ${data?.contradiction_count ?? alerts.length} (${data?.high_severity_count ?? 0} high severity)`,
    `- **Source Anchor Drift Alerts:** ${data?.drift_count ?? drift.length}`,
    `- **Orphan Approved Atoms:** ${data?.orphan_approved_count ?? orphans.length}`,
    "",
  ];

  lines.push("## Contradiction Pairs");
  if (!alerts.length) {
    lines.push("_0 polarity or numeric contradictions detected across active atoms._", "");
  } else {
    alerts.forEach((al, idx) => {
      lines.push(
        `### ${idx + 1}. [${String(al.severity || "medium").toUpperCase()}] ${(al.reasons || []).join(", ")}`
      );
      if ((al.shared_terms || []).length) {
        lines.push(`- **Shared Terms:** \`${al.shared_terms.join("`, `")}\``);
      }
      if (al.atom_a) {
        lines.push(
          `- **Atom A (\`${al.atom_a.filename}\`, ${al.atom_a.review_status || ""}):** ${al.atom_a.claim || ""}`
        );
      }
      if (al.atom_b) {
        lines.push(
          `- **Atom B (\`${al.atom_b.filename}\`, ${al.atom_b.review_status || ""}):** ${al.atom_b.claim || ""}`
        );
      }
      lines.push("");
    });
  }

  lines.push("## Source Anchor Drift Alerts");
  if (!drift.length) {
    lines.push("_All approved atom byte-anchors match their immutable source snapshots._", "");
  } else {
    for (const d of drift) {
      lines.push(
        `- **\`${d.filename || d.atom_id || "Atom"}\`:** ${d.reason || d.detail || "Source anchor drift"}`
      );
    }
    lines.push("");
  }

  lines.push("## Orphan Approved Atoms (Unlinked)");
  if (!orphans.length) {
    lines.push("_None — all approved atoms participate in graph edges._", "");
  } else {
    for (const fn of orphans) lines.push(`- \`${fn}\``);
    lines.push("");
  }

  return lines.join("\n");
}

function formatSnapshotDiffMarkdown(d) {
  const lines = [
    `# Vault Semantic Diff: \`${d.from_snapshot}\` -> \`${d.to_snapshot}\``,
    "",
    `- **Status:** ${d.unchanged ? "UNCHANGED (Identical Merkle Root)" : "CHANGED"}`,
    `- **Merkle Root Before:** \`${d.merkle_root_before || ""}\``,
    `- **Merkle Root After:** \`${d.merkle_root_after || ""}\``,
    "",
  ];

  const added = d.added_atoms || [];
  const removed = d.removed_atoms || [];
  const modified = d.modified_atoms || [];

  lines.push(`## Added Atoms (${added.length})`);
  if (!added.length) lines.push("_None_");
  for (const a of added) {
    lines.push(`- **\`${a.filename || a.atom_id || ""}\`**: ${a.claim || ""}`);
  }
  lines.push("");

  lines.push(`## Removed Atoms (${removed.length})`);
  if (!removed.length) lines.push("_None_");
  for (const a of removed) {
    lines.push(`- **\`${a.filename || a.atom_id || ""}\`**: ${a.claim || ""}`);
  }
  lines.push("");

  lines.push(`## Modified Atoms (${modified.length})`);
  if (!modified.length) lines.push("_None_");
  for (const m of modified) {
    lines.push(
      `- **\`${m.filename}\`** (\`${String(m.old_sha256 || "").slice(0, 12)}\` -> \`${String(m.new_sha256 || "").slice(0, 12)}\`)`,
      `  - **Before:** ${m.old_claim || m.before_claim || ""}`,
      `  - **After:** ${m.new_claim || m.after_claim || ""}`
    );
  }
  lines.push("");

  const pbAdded = d.added_playbooks || [];
  const pbRemoved = d.removed_playbooks || [];
  const pbMod = d.modified_playbooks || [];
  if (pbAdded.length || pbRemoved.length || pbMod.length) {
    lines.push("## Playbook Changes");
    for (const p of pbAdded) lines.push(`- Added: \`${p}\``);
    for (const p of pbRemoved) lines.push(`- Removed: \`${p}\``);
    for (const p of pbMod) lines.push(`- Modified: \`${p}\``);
    lines.push("");
  }

  return lines.join("\n");
}

function formatSnapshotRecallMarkdown(data, query, snapshotId) {
  const results = data?.results || data?.items || [];
  const lines = [
    `# Historical Snapshot Recall: \`${data?.snapshot_id || snapshotId}\``,
    "",
    `- **Query:** ${query}`,
    `- **Snapshot Merkle Root:** \`${data?.merkle_root || ""}\``,
    `- **Matches Returned:** ${results.length}`,
    "",
  ];
  if (!results.length) {
    lines.push("_No matching atoms recorded in this historical snapshot._", "");
  } else {
    results.forEach((r, idx) => {
      lines.push(
        `## ${idx + 1}. \`${r.filename || r.atom_id || "Atom"}\`${r.score !== undefined ? ` _(score: ${r.score})_` : ""}`,
        `- **SHA-256:** \`${r.file_sha256 || r.sha256 || ""}\``,
        `- **Claim:** ${r.claim || ""}`,
        ""
      );
      if (r.evidence) {
        lines.push(`> ${String(r.evidence).replace(/\r?\n/g, "\n> ")}`, "");
      }
    });
  }
  return lines.join("\n");
}

function formatSnapshotVerifyMarkdown(data) {
  const issues = data?.issues || [];
  const lines = [
    `# Snapshot Cryptographic Verification: \`${data?.snapshot_id || ""}\``,
    "",
    `- **Integrity Status:** ${data?.valid ? "VALID" : "INVALID"}`,
    `- **Stored Merkle Root:** \`${data?.stored_merkle_root || data?.merkle_root || ""}\``,
    `- **Computed Merkle Root:** \`${data?.computed_merkle_root || ""}\``,
    `- **Approved Atoms:** ${data?.atom_count ?? 0}`,
    `- **Accepted Playbooks:** ${data?.playbook_count ?? 0}`,
    "",
  ];
  if (issues.length) {
    lines.push("## Integrity Issues");
    for (const issue of issues) lines.push(`- ${issue}`);
    lines.push("");
  } else {
    lines.push("_All leaf hashes and Merkle tree digests verified cleanly._", "");
  }
  return lines.join("\n");
}

function formatPlaybookMarkdown(pb) {
  const lines = [
    `# Skill Playbook: ${pb.title || pb.playbook_id || pb.filename}`,
    "",
    `- **Playbook ID:** \`${pb.playbook_id || pb.filename || ""}\``,
    `- **Status:** \`${pb.status || "accepted"}\``,
    `- **SHA-256:** \`${pb.sha256 || ""}\``,
    `- **Contexts:** ${(pb.contexts || []).join(", ") || "unscoped"}`,
    `- **Backing Atoms:** ${(pb.backing_atoms || []).map((a) => `\`${a}\``).join(", ") || "none"}`,
    `- **Trigger:** ${pb.trigger || "—"}`,
    "",
  ];
  if ((pb.preconditions || []).length) {
    lines.push("## Preconditions");
    for (const p of pb.preconditions) lines.push(`- [ ] ${p}`);
    lines.push("");
  }
  if ((pb.steps || []).length) {
    lines.push("## Operational Checklist Steps");
    pb.steps.forEach((s, idx) => lines.push(`${idx + 1}. [ ] ${s}`));
    lines.push("");
  }
  if ((pb.expected_outputs || []).length) {
    lines.push("## Expected Outputs");
    for (const o of pb.expected_outputs) lines.push(`- ${o}`);
    lines.push("");
  }
  if ((pb.failure_recovery || []).length) {
    lines.push("## Failure Recovery & Rollback");
    for (const r of pb.failure_recovery) lines.push(`- ${r}`);
    lines.push("");
  }
  return lines.join("\n");
}

function formatAtomProofMarkdown(ref, itemMeta, proof) {
  const isPlaybook =
    String(ref || "").startsWith("5-playbooks/") ||
    String(ref || "").startsWith("playbook:") ||
    String(proof?.rel_path || "").startsWith("5-playbooks/");
  const headingPrefix = isPlaybook ? "Verified Playbook Leaf" : "Verified Atom";
  const lines = [
    `# ${headingPrefix}: ${ref}`,
    "",
    `- **Review Status:** \`${itemMeta?.review_status || itemMeta?.status || "accepted"}\``,
    `- **Kind:** \`${isPlaybook ? "playbook" : itemMeta?.memory_type || "semantic"}\``,
    `- **File SHA-256:** \`${proof?.file_sha256 || itemMeta?.sha256 || ""}\``,
  ];
  if (Array.isArray(itemMeta?.sources) && itemMeta.sources.length) {
    lines.push(`- **Sources:** ${itemMeta.sources.map((s) => `\`${s}\``).join(", ")}`);
  }
  lines.push("");

  if (itemMeta?.title && isPlaybook) {
    lines.push("## Title", itemMeta.title, "");
  }
  if (itemMeta?.claim) {
    lines.push("## Claim", itemMeta.claim, "");
  }
  if (itemMeta?.evidence) {
    lines.push("## Anchored Evidence", `> ${String(itemMeta.evidence).replace(/\r?\n/g, "\n> ")}`, "");
  }

  if (proof && proof.merkle_root) {
    lines.push(
      "## Cryptographic Merkle Inclusion Proof",
      `- **Leaf Path:** \`${proof.rel_path || ref}\``,
      `- **Leaf SHA-256:** \`${proof.leaf_sha256 || ""}\``,
      `- **Live Vault Merkle Root:** \`${proof.merkle_root}\``,
      `- **Ledger Head Entry SHA-256:** \`${proof.ledger_head_entry_sha256 || "(uncommitted)"}\``,
      ""
    );
    const sibs = proof.siblings || [];
    if (sibs.length) {
      lines.push("| Level | Position | Sibling SHA-256 |", "| :--- | :--- | :--- |");
      sibs.forEach((s, i) => {
        lines.push(`| ${i} | \`${s.position}\` | \`${s.sha256}\` |`);
      });
      lines.push("");
    } else {
      lines.push("_Single-leaf Merkle tree (0 sibling hashes required)._", "");
    }
    lines.push("### Portable Proof Bundle", "```json", JSON.stringify(proof, null, 2), "```", "");
  }

  return lines.join("\n");
}

function formatInboxPreviewMarkdown(filename, doc) {
  const risks = Array.isArray(doc?.risk_flags) ? doc.risk_flags : [];
  const rawBody = String(doc?.content ?? doc?.preview ?? "");
  const fence = rawBody.includes("````") ? "`````" : "````";
  const lines = [
    `# Quarantined Inbox Item: \`${filename}\``,
    "",
    "> **UNTRUSTED QUARANTINED CAPTURE** — Inspect carefully before promoting to the vault.",
    "",
    `- **SHA-256:** \`${doc?.sha256 || ""}\``,
    `- **Risk Flags:** ${risks.length ? risks.map((r) => `\`${r}\``).join(", ") : "_None detected_"}`,
    "",
    "## Captured Content (Read-Only Preview)",
    `${fence}text`,
    rawBody,
    fence,
    "",
  ];
  return lines.join("\n");
}

module.exports = {
  computeClaimGroundingUnits,
  formatPlanMarkdown,
  formatRadarMarkdown,
  formatSnapshotDiffMarkdown,
  formatSnapshotRecallMarkdown,
  formatSnapshotVerifyMarkdown,
  formatPlaybookMarkdown,
  formatAtomProofMarkdown,
  formatInboxPreviewMarkdown,
};
