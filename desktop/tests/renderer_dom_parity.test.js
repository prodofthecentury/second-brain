"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

const RENDERER_DIR = path.resolve(__dirname, "..", "renderer");

function extractHtmlIds(html) {
  const ids = new Set();
  const re = /\bid="([^"]+)"/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    ids.add(m[1]);
  }
  return ids;
}

function extractGetElementByIds(jsSource) {
  const ids = new Set();
  const re = /\bgetElementById\(\s*["']([^"']+)["']\s*\)/g;
  let m;
  while ((m = re.exec(jsSource)) !== null) {
    ids.add(m[1]);
  }
  return ids;
}

describe("Renderer HTML <-> JS Element ID Parity", () => {
  it("every literal getElementById(...) in app.js exists in index.html", () => {
    const indexHtml = fs.readFileSync(
      path.join(RENDERER_DIR, "index.html"),
      "utf8"
    );
    const appJs = fs.readFileSync(path.join(RENDERER_DIR, "app.js"), "utf8");

    const htmlIds = extractHtmlIds(indexHtml);
    const jsIds = extractGetElementByIds(appJs);
    assert.ok(jsIds.size >= 60, "Expected comprehensive UI bindings in app.js");

    const missing = [...jsIds].filter((id) => !htmlIds.has(id));
    assert.deepEqual(
      missing,
      [],
      `app.js references missing index.html IDs: ${missing.join(", ")}`
    );
  });

  it("every literal getElementById(...) in capture.js exists in capture.html and capture.html has draggable bar", () => {
    const captureHtml = fs.readFileSync(
      path.join(RENDERER_DIR, "capture.html"),
      "utf8"
    );
    const captureJs = fs.readFileSync(
      path.join(RENDERER_DIR, "capture.js"),
      "utf8"
    );
    const stylesCss = fs.readFileSync(
      path.join(RENDERER_DIR, "styles.css"),
      "utf8"
    );

    const htmlIds = extractHtmlIds(captureHtml);
    const jsIds = extractGetElementByIds(captureJs);
    const missing = [...jsIds].filter((id) => !htmlIds.has(id));
    assert.deepEqual(missing, []);

    assert.match(captureHtml, /class="capture-drag-bar"/);
    assert.match(stylesCss, /-webkit-app-region:\s*drag/);
    assert.match(stylesCss, /-webkit-app-region:\s*no-drag/);
  });
});
