"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { createTestDom } = require("./helpers/dom_harness");
const {
  sanitizeExternalUrl,
  renderMarkdownSafe,
} = require("../renderer/markdown");

describe("Safe Markdown DOM Renderer", () => {
  it("renders headings h1 through h6, bold, italic, inline code, and fenced code blocks", () => {
    const { doc, body } = createTestDom();
    const container = doc.createElement("div");
    body.appendChild(container);

    const md = [
      "# Heading One",
      "## Heading Two",
      "### Heading Three",
      "#### Heading Four",
      "##### Heading Five",
      "###### Heading Six",
      "",
      "Paragraph with **bold text**, *italic text*, and `const x = 1;` inline.",
      "",
      "```python",
      "def check():",
      "    return '<script>not_executed()</script>'",
      "```",
    ].join("\n");

    renderMarkdownSafe(container, md, { doc });

    for (let lvl = 1; lvl <= 6; lvl += 1) {
      const h = container.querySelector(`h${lvl}`);
      assert.ok(h, `Expected h${lvl} element`);
    }
    assert.equal(container.querySelector("h1").textContent, "Heading One");
    assert.equal(container.querySelector("h6").textContent, "Heading Six");
    assert.equal(container.querySelector("strong").textContent, "bold text");
    assert.equal(container.querySelector("em").textContent, "italic text");
    assert.equal(
      container.querySelector("code.md-inline-code").textContent,
      "const x = 1;"
    );

    const pre = container.querySelector("pre.md-code");
    assert.ok(pre);
    assert.equal(pre.dataset.lang, "python");
    assert.match(pre.textContent, /<script>not_executed\(\)<\/script>/);
    assert.equal(container.querySelectorAll("script").length, 0);
  });

  it("renders Markdown tables with column alignments and inline formatting", () => {
    const { doc, body } = createTestDom();
    const container = doc.createElement("div");
    body.appendChild(container);

    const md = [
      "| Feature | Status | Score |",
      "| :--- | :---: | ---: |",
      "| **BM25** | `ok` | 99 |",
      "| Merkle [V1] | verified | 100 |",
    ].join("\n");

    renderMarkdownSafe(container, md, { doc });

    const table = container.querySelector("table.md-table");
    assert.ok(table, "Expected rendered table");
    const ths = table.querySelectorAll("th");
    assert.equal(ths.length, 3);
    assert.equal(ths[0].style.textAlign, "left");
    assert.equal(ths[1].style.textAlign, "center");
    assert.equal(ths[2].style.textAlign, "right");

    const tds = table.querySelectorAll("td");
    assert.equal(tds.length, 6);
    assert.equal(tds[0].querySelector("strong").textContent, "BM25");
    assert.equal(tds[1].querySelector("code").textContent, "ok");
    assert.equal(
      tds[3].querySelector("button.citation-badge").dataset.citationId,
      "V1"
    );
  });

  it("renders task lists, nested ordered/unordered lists, and blockquotes", () => {
    const { doc, body } = createTestDom();
    const container = doc.createElement("div");
    body.appendChild(container);

    const md = [
      "- [x] Completed security audit",
      "- [ ] Pending benchmark run",
      "  - Nested bullet A",
      "  - Nested bullet B",
      "",
      "1. First ordered step",
      "   1. Nested sub-step",
      "",
      "> Verifiable local memory quote",
    ].join("\n");

    renderMarkdownSafe(container, md, { doc });

    const taskItems = container.querySelectorAll("li.md-task-item");
    assert.equal(taskItems.length, 2);
    const chk0 = taskItems[0].querySelector("input");
    const chk1 = taskItems[1].querySelector("input");
    assert.equal(chk0.checked, true);
    assert.equal(chk0.disabled, true);
    assert.equal(chk1.checked, false);
    assert.equal(chk1.disabled, true);

    const nestedUl = taskItems[1].querySelector("ul");
    assert.ok(nestedUl, "Expected nested <ul> inside second list item");
    assert.equal(nestedUl.querySelectorAll("li").length, 2);

    const ol = container.querySelector("ol");
    assert.ok(ol);
    assert.ok(ol.querySelector("ol"), "Expected nested <ol>");

    const bq = container.querySelector("blockquote");
    assert.ok(bq);
    assert.match(bq.textContent, /Verifiable local memory quote/);
  });

  it("renders clickable [V#]/[N#]/[P#]/[L#] citation badges and [[WikiLinks]]", () => {
    const { doc, body } = createTestDom();
    const container = doc.createElement("div");
    body.appendChild(container);

    const clickedCitations = [];
    const clickedWiki = [];
    renderMarkdownSafe(
      container,
      "Grounded in [V1], preference [P2], note [N3], lead [L4], and [[Project Roadmap|Roadmap]].",
      {
        doc,
        onCitationClick: (cid) => clickedCitations.push(cid),
        onWikiLinkClick: (target) => clickedWiki.push(target),
      }
    );

    const badges = container.querySelectorAll("button.citation-badge");
    assert.equal(badges.length, 4);
    assert.ok(badges[0].classList.contains("tier-v"));
    assert.ok(badges[1].classList.contains("tier-p"));
    assert.ok(badges[2].classList.contains("tier-n"));
    assert.ok(badges[3].classList.contains("tier-l"));

    for (const b of badges) b.click();
    assert.deepEqual(clickedCitations, ["V1", "P2", "N3", "L4"]);

    const wiki = container.querySelector(".wikilink-pill");
    assert.ok(wiki);
    assert.equal(wiki.textContent, "[[Roadmap]]");
    wiki.click();
    assert.deepEqual(clickedWiki, ["Project Roadmap"]);
  });

  it("allows only http/https/mailto links via external callback and rejects javascript:/data: XSS vectors", () => {
    assert.equal(
      sanitizeExternalUrl("https://example.com/docs?q=1"),
      "https://example.com/docs?q=1"
    );
    assert.equal(
      sanitizeExternalUrl("mailto:owner@example.com"),
      "mailto:owner@example.com"
    );
    assert.equal(sanitizeExternalUrl("javascript:alert(1)"), null);
    assert.equal(sanitizeExternalUrl("JAVASCRIPT:evil()"), null);
    assert.equal(
      sanitizeExternalUrl("data:text/html,<script>alert(1)</script>"),
      null
    );
    assert.equal(sanitizeExternalUrl("file:///etc/passwd"), null);
    assert.equal(sanitizeExternalUrl("vbscript:msgbox(1)"), null);

    const { doc, body } = createTestDom();
    const container = doc.createElement("div");
    body.appendChild(container);

    const opened = [];
    const hostileMd = [
      "Safe link: [Docs](https://example.com/guide) and [Mail](mailto:sec@example.com).",
      "Blocked JS: [ClickMe](javascript:alert(document.cookie))",
      "Blocked Data: [Payload](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
      "Raw tags: <script>window.pwned=true</script> <img src=x onerror=alert(1)>",
    ].join("\n\n");

    renderMarkdownSafe(container, hostileMd, {
      doc,
      onOpenExternal: (url) => opened.push(url),
    });

    const anchors = container.querySelectorAll("a");
    assert.equal(anchors.length, 2, "Only safe http/https/mailto links create <a>");
    assert.equal(anchors[0].getAttribute("href"), "https://example.com/guide");
    assert.equal(anchors[1].getAttribute("href"), "mailto:sec@example.com");

    const notDefault = !anchors[0].click();
    assert.equal(notDefault, true, "Click on link must preventDefault()");
    assert.deepEqual(opened, ["https://example.com/guide"]);

    assert.equal(container.querySelectorAll("script").length, 0);
    assert.equal(container.querySelectorAll("img").length, 0);
    assert.match(container.textContent, /<script>window\.pwned=true<\/script>/);
    assert.match(container.textContent, /<img src=x onerror=alert\(1\)>/);
  });
});
