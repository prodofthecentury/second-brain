"use strict";

(function initPlaybookUnitsModule(root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.SecondBrainPlaybookUnits = api;
  }
})(
  typeof globalThis !== "undefined" ? globalThis : this,
  function createPlaybookUnitsApi() {
    const LIST_MARKER_RE = /^(?:[-*]|\d+[.)])\s+(.*)$/;

    function collapseWhitespace(value) {
      return String(value ?? "")
        .trim()
        .split(/\s+/)
        .filter(Boolean)
        .join(" ");
    }

    function splitSentenceUnits(text) {
      return String(text ?? "")
        .split(/(?<=[.!?])\s+/)
        .map((s) => s.trim())
        .filter(Boolean);
    }

    function claimGroundingUnits(claimText) {
      const units = new Set();
      const raw = String(claimText ?? "");
      const full = collapseWhitespace(raw);
      if (full) {
        units.add(full);
      }
      for (const rawLine of raw.split(/\r?\n/)) {
        const stripped = rawLine.trim();
        if (!stripped) continue;
        const m = LIST_MARKER_RE.exec(stripped);
        const lineUnit = collapseWhitespace(m ? m[1] : stripped);
        if (lineUnit) {
          units.add(lineUnit);
        }
        for (const sentence of splitSentenceUnits(lineUnit)) {
          if (sentence) units.add(sentence);
        }
      }
      for (const sentence of splitSentenceUnits(full)) {
        if (sentence) units.add(sentence);
      }
      return units;
    }

    function isStepGroundedInClaim(stepText, claimText) {
      const norm = collapseWhitespace(stepText);
      if (norm.length < 5) return false;
      return claimGroundingUnits(claimText).has(norm);
    }

    function splitCandidateSteps(candidate) {
      if (!candidate || typeof candidate !== "object") {
        return [];
      }
      const claimRaw = String(candidate.claim ?? "");
      const fullClaim = collapseWhitespace(claimRaw);

      if (
        Array.isArray(candidate.grounding_units) &&
        candidate.grounding_units.length > 0
      ) {
        const seen = new Set();
        const normalized = [];
        for (const rawUnit of candidate.grounding_units) {
          const clean = collapseWhitespace(rawUnit);
          if (clean.length >= 5 && !seen.has(clean)) {
            seen.add(clean);
            normalized.push(clean);
          }
        }
        if (fullClaim && normalized.length >= 3 && normalized.includes(fullClaim)) {
          const withoutFull = normalized.filter((u) => u !== fullClaim);
          if (withoutFull.length >= 2) {
            return withoutFull;
          }
        }
        if (normalized.length > 0) {
          return normalized;
        }
      }

      if (!fullClaim) return [];
      const validUnits = claimGroundingUnits(claimRaw);
      const rawLines = claimRaw
        .split(/\r?\n/)
        .map((l) => l.trim())
        .filter(Boolean);

      const lineUnits = rawLines
        .map((line) => {
          const m = LIST_MARKER_RE.exec(line);
          return collapseWhitespace(m ? m[1] : line);
        })
        .filter(Boolean);

      if (lineUnits.length > 1) {
        const validLines = lineUnits.filter(
          (u) => u.length >= 5 && validUnits.has(u)
        );
        if (validLines.length > 0) {
          return validLines;
        }
      }

      const single = lineUnits[0] || fullClaim;
      const sentences = splitSentenceUnits(single);
      if (
        sentences.length > 1 &&
        sentences.every((s) => s.length >= 5 && validUnits.has(s))
      ) {
        return sentences;
      }

      return single.length >= 5 && validUnits.has(single) ? [single] : [];
    }

    function buildPlaybookPrefillFromCandidate(cand, existingFields = {}) {
      if (!cand || typeof cand !== "object") {
        return {
          playbookId: existingFields.playbookId || "",
          title: existingFields.title || "",
          steps: [],
          stepsText: "",
          atomId: "",
        };
      }
      const atomId = String(cand.atom_id || cand.filename || "");
      const stem = atomId
        .replace(/\.md$/i, "")
        .replace(/[^a-z0-9-]+/gi, "-")
        .replace(/^-+|-+$/g, "")
        .toLowerCase();

      let playbookId = String(existingFields.playbookId || "").trim();
      if (!playbookId && stem) {
        playbookId = `pb-${stem}`.slice(0, 48).replace(/-+$/g, "");
      }

      let title = String(existingFields.title || "").trim();
      if (!title && stem) {
        title = stem
          .replace(/-/g, " ")
          .replace(/\b\w/g, (c) => c.toUpperCase());
      }

      const steps = splitCandidateSteps(cand);
      return {
        playbookId,
        title,
        steps,
        stepsText: steps.join("\n"),
        atomId,
      };
    }

    function buildPlaybookUpdatePrefill(pb) {
      if (!pb || typeof pb !== "object") {
        return null;
      }
      const playbookId = String(
        pb.playbook_id || String(pb.filename || "").replace(/\.md$/i, "")
      ).trim();
      return {
        playbookId,
        title: String(pb.title || ""),
        trigger: String(pb.trigger || ""),
        preconditionsText: Array.isArray(pb.preconditions)
          ? pb.preconditions.join("\n")
          : "",
        stepsText: Array.isArray(pb.steps) ? pb.steps.join("\n") : "",
        expectedOutputsText: Array.isArray(pb.expected_outputs)
          ? pb.expected_outputs.join("\n")
          : "",
        failureRecoveryText: Array.isArray(pb.failure_recovery)
          ? pb.failure_recovery.join("\n")
          : "",
        expectedPlaybookSha256: String(pb.sha256 || "").trim(),
        backingAtoms: Array.isArray(pb.backing_atoms)
          ? pb.backing_atoms.map((a) => String(a))
          : [],
      };
    }

    function buildPlaybookPromotePayload(formState = {}) {
      const payload = {
        playbook_id: String(formState.playbook_id || "").trim(),
        title: String(formState.title || "").trim(),
        backing_atoms: Array.isArray(formState.backing_atoms)
          ? formState.backing_atoms
          : [],
        trigger: String(formState.trigger || "").trim(),
        preconditions: Array.isArray(formState.preconditions)
          ? formState.preconditions
          : [],
        steps: Array.isArray(formState.steps) ? formState.steps : [],
        expected_outputs: Array.isArray(formState.expected_outputs)
          ? formState.expected_outputs
          : [],
        failure_recovery: Array.isArray(formState.failure_recovery)
          ? formState.failure_recovery
          : [],
        actor: String(formState.actor || "desktop_owner").trim() || "desktop_owner",
        rationale:
          String(formState.rationale || "").trim() ||
          "Owner promoted via Desktop App",
        allow_early_owner_override: Boolean(formState.allow_early_owner_override),
        acknowledge_corrections: Boolean(formState.acknowledge_corrections),
        allow_multi_project: Boolean(formState.allow_multi_project),
      };
      const expectedSha = String(formState.expected_playbook_sha256 || "").trim();
      if (expectedSha) {
        payload.expected_playbook_sha256 = expectedSha;
      }
      return payload;
    }

    return {
      collapseWhitespace,
      claimGroundingUnits,
      isStepGroundedInClaim,
      splitCandidateSteps,
      buildPlaybookPrefillFromCandidate,
      buildPlaybookUpdatePrefill,
      buildPlaybookPromotePayload,
    };
  }
);
