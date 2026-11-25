"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { createTestDom } = require("./helpers/dom_harness");
const {
  applyPlatformShortcutsToDom,
  createModalController,
} = require("../renderer/modal_helpers");
const {
  SVG_NS,
  synthesizeFallbackGraph,
  filterGraphData,
  createGraphViewer,
} = require("../renderer/graph");

describe("Modal Workflow Helpers & Interactive 2D SVG Knowledge Graph", () => {
  it("handles confirmation modal workflow with actor input and cancel/confirm resolution", async () => {
    const { doc, body } = createTestDom();
    const backdrop = doc.createElement("div");
    backdrop.className = "modal-backdrop";
    const titleEl = doc.createElement("span");
    const bodyEl = doc.createElement("div");
    const footerEl = doc.createElement("div");
    const closeBtn = doc.createElement("button");
    backdrop.appendChild(titleEl);
    backdrop.appendChild(bodyEl);
    backdrop.appendChild(footerEl);
    backdrop.appendChild(closeBtn);
    body.appendChild(backdrop);

    const modal = createModalController({
      backdropEl: backdrop,
      titleEl,
      bodyEl,
      footerEl,
      closeBtnEl: closeBtn,
      doc,
    });

    // Confirm with actor override
    const pConfirm = modal.confirm({
      title: "Demote Stale Approvals",
      message: "Demote 2 stale atoms?",
      confirmLabel: "Demote",
      actorDefault: "desktop_owner",
    });
    assert.equal(modal.isOpen(), true);
    const actorInput = bodyEl.querySelector("input");
    assert.ok(actorInput);
    actorInput.value = "alice_owner";

    const buttons = footerEl.querySelectorAll("button");
    assert.equal(buttons.length, 2);
    buttons[1].click(); // Confirm button

    const res1 = await pConfirm;
    assert.deepEqual(res1, { confirmed: true, actor: "alice_owner" });
    assert.equal(modal.isOpen(), false);

    // Cancel via close button
    const pCancel = modal.confirm({
      title: "Delete Note",
      message: "Move to trash?",
    });
    assert.equal(modal.isOpen(), true);
    closeBtn.click();
    const res2 = await pCancel;
    assert.equal(res2.confirmed, false);
    assert.equal(modal.isOpen(), false);
  });

  it("rewrites shortcut badges to macOS ⌘ symbols on darwin", () => {
    const { doc, body } = createTestDom();
    const k1 = doc.createElement("span");
    k1.className = "kbd";
    k1.textContent = "Ctrl+1";
    const k2 = doc.createElement("span");
    k2.className = "kbd";
    k2.textContent = "Ctrl+Shift+Space";
    body.appendChild(k1);
    body.appendChild(k2);

    assert.equal(applyPlatformShortcutsToDom(doc, "win32"), 0);
    assert.equal(k1.textContent, "Ctrl+1");

    const count = applyPlatformShortcutsToDom(doc, "darwin");
    assert.equal(count, 2);
    assert.equal(k1.textContent, "⌘1");
    assert.equal(k2.textContent, "⌘⇧Space");
  });

  it("synthesizes fallback graph, filters nodes/edges, renders SVG via createElementNS, and handles pan/zoom & node click", () => {
    const atoms = [
      {
        filename: "pg-lock.md",
        claim: "Always acquire advisory locks before DDL.",
        review_status: "accepted",
        memory_type: "procedural",
        contexts: ["project:alpha"],
        verified: true,
      },
      {
        filename: "pg-nolock.md",
        claim: "Never acquire advisory locks before DDL.",
        review_status: "pending",
        memory_type: "semantic",
        contexts: ["project:alpha"],
        verified: false,
      },
      {
        filename: "theme-pref.md",
        claim: "Prefer dark mode UI.",
        review_status: "accepted",
        memory_type: "self",
        contexts: [],
        verified: true,
      },
    ];
    const radar = {
      alerts: [
        {
          severity: "high",
          atom_a: { filename: "pg-lock.md" },
          atom_b: { filename: "pg-nolock.md" },
        },
      ],
    };

    const fallbackGraph = synthesizeFallbackGraph(atoms, radar);
    assert.equal(fallbackGraph.fallback, true);
    assert.equal(fallbackGraph.nodes.length, 3);
    assert.ok(
      fallbackGraph.edges.some((e) => e.kind === "contradicts"),
      "Expected contradiction edge"
    );

    const acceptedOnly = filterGraphData(fallbackGraph, {
      includePending: false,
    });
    assert.equal(acceptedOnly.nodes.length, 2);

    const { doc, body } = createTestDom();
    const container = doc.createElement("div");
    body.appendChild(container);

    const selectedEvents = [];
    const viewer = createGraphViewer(container, {
      doc,
      width: 800,
      height: 500,
      onSelectNode: (node, edges) => selectedEvents.push({ node, edges }),
    });

    viewer.render(fallbackGraph, { includePending: true });

    const svg = container.querySelector("svg");
    assert.ok(svg);
    assert.equal(svg.namespaceURI, SVG_NS);

    const nodeGroups = svg.querySelectorAll("g.graph-node");
    assert.equal(nodeGroups.length, 3);

    // Pan & zoom state
    viewer.zoomBy(1.5);
    viewer.panBy(40, -25);
    assert.deepEqual(viewer.getTransformState(), {
      panX: 40,
      panY: -25,
      scale: 1.5,
    });
    viewer.resetView();
    assert.deepEqual(viewer.getTransformState(), {
      panX: 0,
      panY: 0,
      scale: 1,
    });

    // Click node to trigger neighbor inspector
    nodeGroups[0].click();
    assert.equal(selectedEvents.length, 1);
    assert.equal(selectedEvents[0].node.id, "pg-lock.md");
    assert.ok(selectedEvents[0].edges.length >= 1);
  });

  it("filters bi-temporal nodes via asOf & includeExpired and styles expired SVG circles dashed", () => {
    const graphPayload = {
      nodes: [
        {
          id: "active-2026.md",
          label: "Current API v2 Policy",
          review_status: "accepted",
          verified: true,
          valid_from: "2026-01-01",
          valid_until: "2026-12-31",
          temporal_status: "active",
        },
        {
          id: "legacy-2024.md",
          label: "Deprecated API v1 Policy",
          review_status: "accepted",
          verified: true,
          valid_from: "2024-01-01",
          valid_until: "2024-12-31",
          temporal_status: "expired",
        },
        {
          id: "future-2028.md",
          label: "Planned API v3 Policy",
          review_status: "accepted",
          verified: true,
          valid_from: "2028-01-01",
          temporal_status: "not_yet_valid",
        },
      ],
      edges: [
        { from: "legacy-2024.md", to: "active-2026.md", kind: "supersedes" },
      ],
    };

    // As Of 2024-06-15 -> only legacy-2024.md is active
    const hist2024 = filterGraphData(graphPayload, {
      asOf: "2024-06-15",
      includeExpired: false,
      includePending: true,
    });
    assert.deepEqual(hist2024.nodes.map((n) => n.id), ["legacy-2024.md"]);
    assert.equal(hist2024.edges.length, 0);

    // As Of 2026-06-15 with includeExpired=false -> only active-2026.md
    const mid2026 = filterGraphData(graphPayload, {
      asOf: "2026-06-15",
      includeExpired: false,
      includePending: true,
    });
    assert.deepEqual(mid2026.nodes.map((n) => n.id), ["active-2026.md"]);

    // As Of 2026-06-15 with includeExpired=true -> legacy-2024 marked expired, active-2026 active, future-2028 marked not_yet_valid
    const withExpired = filterGraphData(graphPayload, {
      asOf: "2026-06-15",
      includeExpired: true,
      includePending: true,
    });
    assert.equal(withExpired.nodes.length, 3);
    const byId = new Map(withExpired.nodes.map((n) => [n.id, n]));
    assert.equal(byId.get("legacy-2024.md").temporal_status, "expired");
    assert.equal(byId.get("active-2026.md").temporal_status, "active");
    assert.equal(byId.get("future-2028.md").temporal_status, "not_yet_valid");

    // Render SVG and verify expired node carries dashed styling
    const { doc, body } = createTestDom();
    const container = doc.createElement("div");
    body.appendChild(container);
    const viewer = createGraphViewer(container, { doc, width: 640, height: 420 });
    viewer.render(graphPayload, {
      asOf: "2026-06-15",
      includeExpired: true,
      includePending: true,
    });
    const circles = container.querySelectorAll("circle");
    assert.equal(circles.length, 3);
    assert.ok(
      circles.some((c) => c.getAttribute("stroke-dasharray")),
      "Expected at least one dashed SVG circle for expired/future bi-temporal node"
    );
  });
});
