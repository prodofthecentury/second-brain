"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  claimGroundingUnits,
  isStepGroundedInClaim,
  splitCandidateSteps,
  buildPlaybookPrefillFromCandidate,
  buildPlaybookUpdatePrefill,
  buildPlaybookPromotePayload,
} = require("../renderer/playbook_units");

describe("Playbook Grounding Units & CAS Re-Promotion Helpers", () => {
  it("never splits on '; ' and preserves grounding-valid full sentences", () => {
    const claim =
      "Run pg_dump backup; verify SHA-256 digest; store offsite. Apply migration inside transaction! Check /v1/health endpoint.";
    const cand = {
      atom_id: "pg-migration-proc.md",
      claim,
    };

    const steps = splitCandidateSteps(cand);
    assert.deepEqual(steps, [
      "Run pg_dump backup; verify SHA-256 digest; store offsite.",
      "Apply migration inside transaction!",
      "Check /v1/health endpoint.",
    ]);
    for (const st of steps) {
      assert.equal(
        isStepGroundedInClaim(st, claim),
        true,
        `Step must pass _claim_grounding_units: ${st}`
      );
    }
  });

  it("splits newline bullet and numbered list items cleanly stripped of markers without splitting semicolons", () => {
    const claim = [
      "1. Freeze main branch; notify on-call engineer",
      "2) Run full pytest suite; verify zero regressions",
      "- Tag signed git release",
      "* Publish release checksums",
    ].join("\n");

    const prefill = buildPlaybookPrefillFromCandidate({
      atom_id: "release-checklist.md",
      claim,
    });

    assert.equal(prefill.playbookId, "pb-release-checklist");
    assert.equal(prefill.title, "Release Checklist");
    assert.deepEqual(prefill.steps, [
      "Freeze main branch; notify on-call engineer",
      "Run full pytest suite; verify zero regressions",
      "Tag signed git release",
      "Publish release checksums",
    ]);
    for (const st of prefill.steps) {
      assert.equal(isStepGroundedInClaim(st, claim), true);
    }
  });

  it("prefers candidate.grounding_units from the API when present", () => {
    const cand = {
      atom_id: "custom-proc.md",
      claim: " Step A; keep semi.\nStep B.",
      grounding_units: ["Step A; keep semi.", "Step B."],
    };
    const steps = splitCandidateSteps(cand);
    assert.deepEqual(steps, ["Step A; keep semi.", "Step B."]);
    assert.ok(claimGroundingUnits(cand.claim).has("Step A; keep semi."));
  });

  it("builds CAS re-promotion / update prefill and payload with expected_playbook_sha256", () => {
    const existingPb = {
      playbook_id: "release-smoke-check",
      filename: "release-smoke-check.md",
      title: "Release Smoke Check",
      trigger: "Before cutting a release tag",
      preconditions: ["Clean working tree"],
      steps: ["Run smoke test suite"],
      expected_outputs: ["Zero test failures"],
      failure_recovery: ["Revert release tag"],
      backing_atoms: ["smoke-proc.md"],
      sha256: "a".repeat(64),
    };

    const updatePrefill = buildPlaybookUpdatePrefill(existingPb);
    assert.equal(updatePrefill.playbookId, "release-smoke-check");
    assert.equal(updatePrefill.expectedPlaybookSha256, "a".repeat(64));
    assert.deepEqual(updatePrefill.backingAtoms, ["smoke-proc.md"]);

    const updatePayload = buildPlaybookPromotePayload({
      playbook_id: updatePrefill.playbookId,
      title: updatePrefill.title,
      backing_atoms: [["smoke-proc.md", "b".repeat(64)]],
      trigger: updatePrefill.trigger,
      preconditions: ["Clean working tree"],
      steps: ["Run smoke test suite"],
      expected_outputs: ["Zero test failures"],
      failure_recovery: ["Revert release tag"],
      expected_playbook_sha256: updatePrefill.expectedPlaybookSha256,
    });
    assert.equal(updatePayload.expected_playbook_sha256, "a".repeat(64));

    const initialPayload = buildPlaybookPromotePayload({
      playbook_id: "new-pb",
      title: "New Playbook",
      backing_atoms: [["smoke-proc.md", "b".repeat(64)]],
      trigger: "On deploy",
      steps: ["Run smoke test suite"],
      expected_playbook_sha256: "",
    });
    assert.equal(
      Object.prototype.hasOwnProperty.call(
        initialPayload,
        "expected_playbook_sha256"
      ),
      false,
      "Initial promotion must omit expected_playbook_sha256"
    );
  });
});
