"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { createTestDom } = require("./helpers/dom_harness");
const {
  createPaginator,
  buildPaginatedPath,
  normalizeLedgerLeafOption,
  renderPaginationBar,
} = require("../renderer/pagination");

describe("Pagination State & Load More Controls", () => {
  it("tracks offset/limit/total, appends deduplicated pages, and renders Load More bar", async () => {
    const pager = createPaginator({ pageSize: 2, keyField: "filename" });
    assert.equal(pager.canLoadMore(), false);

    const url1 = buildPaginatedPath("/v1/notes", {
      limit: pager.getPageSize(),
      offset: pager.nextOffset(),
      params: { q: "arch", tag: "" },
    });
    assert.equal(url1, "/v1/notes?limit=2&offset=0&q=arch");

    pager.applyPage(
      {
        total: 5,
        count: 2,
        offset: 0,
        limit: 2,
        items: [{ filename: "a.md" }, { filename: "b.md" }],
      },
      { append: false }
    );
    assert.equal(pager.canLoadMore(), true);
    assert.equal(pager.nextOffset(), 2);
    assert.equal(pager.remainingCount(), 3);
    assert.equal(pager.summaryText("notes"), "Showing 2 of 5 notes");

    // Append page 2 (including a duplicate 'b.md' to verify deduplication)
    pager.applyPage(
      {
        total: 5,
        count: 2,
        offset: 2,
        limit: 2,
        items: [{ filename: "b.md" }, { filename: "c.md" }, { filename: "d.md" }],
      },
      { append: true }
    );
    assert.deepEqual(
      pager.getItems().map((x) => x.filename),
      ["a.md", "b.md", "c.md", "d.md"]
    );
    assert.equal(pager.canLoadMore(), true);
    assert.equal(pager.remainingCount(), 1);

    const { doc, body } = createTestDom();
    const bar = doc.createElement("div");
    body.appendChild(bar);
    let loadMoreClicks = 0;
    renderPaginationBar(
      bar,
      pager,
      async () => {
        loadMoreClicks += 1;
        pager.applyPage(
          {
            total: 5,
            count: 1,
            offset: 4,
            limit: 2,
            items: [{ filename: "e.md" }],
          },
          { append: true }
        );
      },
      { doc, noun: "notes" }
    );

    const btn = bar.querySelector("button");
    assert.ok(btn, "Expected Load More button");
    btn.click();
    await new Promise((r) => setTimeout(r, 10));
    assert.equal(loadMoreClicks, 1);
    assert.equal(pager.canLoadMore(), false);
    assert.equal(pager.getItems().length, 5);
  });

  it("normalizes Merkle ledger leaf options for both 2-atoms and 5-playbooks", () => {
    const atomLeaf = normalizeLedgerLeafOption({
      rel_path: "2-atoms/pg-rule.md",
      kind: "atom",
    });
    assert.deepEqual(atomLeaf, {
      value: "2-atoms/pg-rule.md",
      kind: "atom",
      label: "[atom] 2-atoms/pg-rule.md",
    });

    const pbLeaf = normalizeLedgerLeafOption({
      rel_path: "5-playbooks/release-check.md",
      kind: "playbook",
    });
    assert.deepEqual(pbLeaf, {
      value: "5-playbooks/release-check.md",
      kind: "playbook",
      label: "[playbook] 5-playbooks/release-check.md",
    });

    const shorthandPb = normalizeLedgerLeafOption({
      filename: "deploy.md",
      kind: "playbook",
    });
    assert.equal(shorthandPb.value, "5-playbooks/deploy.md");
  });
});
