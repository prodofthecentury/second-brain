"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

const DESKTOP_ROOT = path.resolve(__dirname, "..");

function collectDesktopSourceFiles(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === "node_modules" || entry.name === "tests") {
      continue;
    }
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      collectDesktopSourceFiles(full, out);
    } else if (
      entry.isFile() &&
      (entry.name.endsWith(".js") || entry.name.endsWith(".html"))
    ) {
      out.push(full);
    }
  }
  return out;
}

describe("Static Security & CSP Guard across desktop/", () => {
  it("contains zero unsafe HTML DOM sinks across all desktop/*.js and *.html files", () => {
    const files = collectDesktopSourceFiles(DESKTOP_ROOT);
    assert.ok(files.length >= 8, "Expected desktop JS/HTML source files");

    const forbiddenPatterns = [
      {
        name: "inner" + "HTML",
        re: new RegExp("\\b" + "inner" + "HTML" + "\\b"),
      },
      {
        name: "outer" + "HTML",
        re: new RegExp("\\b" + "outer" + "HTML" + "\\b"),
      },
      {
        name: "insertAdjacent" + "HTML",
        re: new RegExp("\\b" + "insertAdjacent" + "HTML" + "\\b"),
      },
      {
        name: "document.write",
        re: /\bdocument\s*\.\s*write\b/,
      },
    ];

    const violations = [];
    for (const filePath of files) {
      const content = fs.readFileSync(filePath, "utf8");
      const rel = path.relative(DESKTOP_ROOT, filePath);
      for (const pat of forbiddenPatterns) {
        if (pat.re.test(content)) {
          violations.push(`${rel}: matched forbidden sink ${pat.name}`);
        }
      }
    }
    assert.deepEqual(violations, []);
  });

  it("enforces strict Content-Security-Policy in index.html and capture.html with no inline scripts or inline event handlers", () => {
    for (const htmlName of ["index.html", "capture.html"]) {
      const htmlPath = path.join(DESKTOP_ROOT, "renderer", htmlName);
      const html = fs.readFileSync(htmlPath, "utf8");

      assert.match(html, /http-equiv="Content-Security-Policy"/);
      assert.match(html, /default-src 'self'/);
      assert.match(html, /script-src 'self'/);
      assert.match(html, /connect-src 'none'/);
      assert.match(html, /object-src 'none'/);
      assert.doesNotMatch(html, /script-src[^;]*unsafe-inline/);
      assert.doesNotMatch(html, /unsafe-eval/);
      assert.doesNotMatch(
        html,
        /\son(?:click|load|error|mouseover|focus)\s*=/i,
        `${htmlName} must not contain inline DOM event attributes`
      );
    }
  });
});
