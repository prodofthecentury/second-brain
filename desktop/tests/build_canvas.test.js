"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const BuildCanvas = require("../renderer/build_canvas.js");
const ChatState = require("../renderer/chat_state.js");

describe("Dual-Mode Build Canvas & Persona State", () => {
  it("extracts multi-file HTML/CSS/JS artifacts and wraps with strict sandboxed srcdoc CSP", () => {
    const sample = [
      "Here is a minimal timer app [V1].",
      "",
      "```html index.html",
      "<main><h1 id=\"clock\">00:00</h1><button id=\"startBtn\">Start</button></main>",
      "```",
      "",
      "```css styles.css",
      "body { display: grid; place-items: center; }",
      "```",
      "",
      "```js app.js",
      "document.getElementById('startBtn').addEventListener('click', () => {});",
      "```",
    ].join("\n");

    const extracted = BuildCanvas.extractArtifactFiles(sample);
    assert.equal(extracted.hasPreviewableHtml, true);
    assert.deepEqual(
      extracted.files.map((f) => f.filename),
      ["index.html", "styles.css", "app.js"]
    );

    const srcdoc = BuildCanvas.buildSandboxedPreviewDocument(extracted, {
      title: "Timer App",
    });
    assert.match(srcdoc, /Content-Security-Policy/);
    assert.match(srcdoc, /connect-src 'none'/);
    assert.match(srcdoc, /display: grid/);
    assert.match(srcdoc, /startBtn/);
  });

  it("blocks path traversal and absolute drive paths when sanitizing artifact filenames", () => {
    assert.equal(
      BuildCanvas.sanitizeRelativeArtifactPath("../../etc/passwd", "safe.txt"),
      "safe.txt"
    );
    assert.equal(
      BuildCanvas.sanitizeRelativeArtifactPath("C:\\Windows\\system32\\cmd.exe", "safe.txt"),
      "safe.txt"
    );
    assert.equal(
      BuildCanvas.sanitizeRelativeArtifactPath("src/app.js", "fallback.js"),
      "src/app.js"
    );
    assert.equal(
      BuildCanvas.sanitizeRelativeArtifactPath("CON", "fallback.txt"),
      "fallback.txt"
    );
    assert.equal(
      BuildCanvas.sanitizeRelativeArtifactPath("sub/nul.js", "fallback.js"),
      "fallback.js"
    );
    assert.equal(
      BuildCanvas.sanitizeRelativeArtifactPath("COM1.html", "fallback.html"),
      "fallback.html"
    );
  });

  it("prevents commented <head> tags from hijacking CSP injection into an HTML comment", () => {
    const withRealHead = BuildCanvas.buildSandboxedPreviewDocument({
      files: [],
      entryHtml:
        "<!DOCTYPE html><html><!-- <head><title>trap</title> --><head><title>Real</title></head><body>Hi</body></html>",
      cssBundle: "",
      jsBundle: "",
      hasPreviewableHtml: true,
    });
    const strippedReal = withRealHead.replace(/<!--[\s\S]*?-->/g, "");
    assert.match(strippedReal, /<meta http-equiv="Content-Security-Policy"/);
    assert.match(strippedReal, /connect-src 'none'/);
    assert.doesNotMatch(
      withRealHead,
      /<!--[^>]*Content-Security-Policy[^>]*-->/,
      "CSP meta tag must never be injected inside an HTML comment"
    );

    const withoutRealHead = BuildCanvas.buildSandboxedPreviewDocument({
      files: [],
      entryHtml: "<!DOCTYPE html><html><!-- <head> --><body>No real head</body></html>",
      cssBundle: "",
      jsBundle: "",
      hasPreviewableHtml: true,
    });
    const strippedNoHead = withoutRealHead.replace(/<!--[\s\S]*?-->/g, "");
    assert.match(strippedNoHead, /<meta http-equiv="Content-Security-Policy"/);

    const doctypeOnlyCommented = BuildCanvas.buildSandboxedPreviewDocument({
      files: [],
      entryHtml: "<!DOCTYPE html><!-- <html><head> --><body>Direct body</body>",
      cssBundle: "",
      jsBundle: "",
      hasPreviewableHtml: true,
    });
    const strippedDoctype = doctypeOnlyCommented.replace(/<!--[\s\S]*?-->/g, "");
    assert.match(strippedDoctype, /<meta http-equiv="Content-Security-Policy"/);
  });

  it("preserves $&, $$, $', and $` replacement tokens verbatim inside JS bundles", () => {
    const trickyJs = "const tokens = '$& $$ $\\' $`'; console.log(tokens);";
    const srcdoc = BuildCanvas.buildSandboxedPreviewDocument({
      files: [],
      entryHtml: "<!DOCTYPE html><html><head></head><body><main>App</main></body></html>",
      cssBundle: "",
      jsBundle: trickyJs,
      hasPreviewableHtml: true,
    });
    assert.ok(srcdoc.includes(trickyJs), "JS bundle must not have $& / $$ / $' / $` expanded by String.replace");
    assert.equal(srcdoc.match(/<\/body>/gi)?.length, 1, "Must keep exactly one closing </body> tag");
  });

  it("escapes </script> and </style> breakouts inside embedded JS and CSS bundles", () => {
    const srcdoc = BuildCanvas.buildSandboxedPreviewDocument({
      files: [],
      entryHtml: "<!DOCTYPE html><html><head></head><body><div id='x'></div></body></html>",
      cssBundle: "body::after { content: '</STYLE><b>css-breakout</b></style>'; }",
      jsBundle: "const payload = '</script><img src=x onerror=alert(1)></SCRIPT>';",
      hasPreviewableHtml: true,
    });
    const scriptMatches = srcdoc.match(/<\/script>/gi) || [];
    assert.equal(scriptMatches.length, 1, "Only the single legitimate closing </script> tag may exist");
    const styleMatches = srcdoc.match(/<\/style>/gi) || [];
    assert.equal(styleMatches.length, 1, "Only the single legitimate closing </style> tag may exist");
    assert.ok(srcdoc.includes("<\\/script>"));
    assert.ok(srcdoc.includes("<\\/SCRIPT>"));
    assert.ok(srcdoc.includes("<\\/STYLE>"));
    assert.ok(srcdoc.includes("<\\/style>"));
  });

  it("preserves multiple unnamed JS and CSS fenced blocks without silent overwrite", () => {
    const md = [
      "```css",
      "body { color: red; }",
      "```",
      "```css",
      "main { padding: 12px; }",
      "```",
      "```js",
      "const a = 1;",
      "```",
      "```js",
      "const b = 2;",
      "```",
    ].join("\n");
    const extracted = BuildCanvas.extractArtifactFiles(md);
    assert.deepEqual(
      extracted.files.map((f) => f.filename),
      ["styles.css", "styles-2.css", "app.js", "app-2.js"]
    );
    assert.match(extracted.cssBundle, /color: red/);
    assert.match(extracted.cssBundle, /padding: 12px/);
    assert.match(extracted.jsBundle, /const a = 1;/);
    assert.match(extracted.jsBundle, /const b = 2;/);
  });

  it("forwards work_mode, persona_id, behavior_mode, custom_rules, and done_criteria in chat_state", () => {
    const req = ChatState.buildChatStreamRequest({
      query: "Build a stopwatch",
      mode: "stream",
      work_mode: "build",
      persona_id: "game_builder",
      behavior_mode: "interview",
      custom_rules: "Use 60fps canvas only.",
      done_criteria: "Working pause and restart button.",
    });
    assert.equal(req.work_mode, "build");
    assert.equal(req.persona_id, "game_builder");
    assert.equal(req.behavior_mode, "interview");
    assert.equal(req.custom_rules, "Use 60fps canvas only.");
    assert.equal(req.done_criteria, "Working pause and restart button.");
  });
});
