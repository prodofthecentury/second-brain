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

  it("blocks RCDATA, inert <template>, rawtext, and quoted-attribute '>' CSP bypasses", () => {
    // 1. <title><head></title> RCDATA bypass
    const titleBypass = BuildCanvas.buildSandboxedPreviewDocument({
      files: [],
      entryHtml:
        '<!doctype html><html><title><head></title><body><img src="https://evil.example/x.png"></body></html>',
      cssBundle: "",
      jsBundle: "",
      hasPreviewableHtml: true,
    });
    assert.doesNotMatch(
      titleBypass,
      /<title\b[^>]*>[\s\S]*?Content-Security-Policy[\s\S]*?<\/title>/i,
      "CSP meta must never be injected inside <title>"
    );
    const withoutTitle = titleBypass.replace(/<title\b[^>]*>[\s\S]*?<\/title>/gi, "");
    assert.match(withoutTitle, /<head>\s*<meta http-equiv="Content-Security-Policy"/i);
    assert.ok(
      titleBypass.indexOf('http-equiv="Content-Security-Policy"') <
        titleBypass.indexOf("<title>"),
      "CSP meta must appear before <title> inside an active <head>"
    );

    // 2. <textarea><head></textarea> RCDATA bypass
    const textareaBypass = BuildCanvas.buildSandboxedPreviewDocument({
      files: [],
      entryHtml:
        "<!doctype html><html><textarea><head></textarea><body>hi</body></html>",
      cssBundle: "",
      jsBundle: "",
      hasPreviewableHtml: true,
    });
    assert.doesNotMatch(
      textareaBypass,
      /<textarea\b[^>]*>[\s\S]*?Content-Security-Policy[\s\S]*?<\/textarea>/i,
      "CSP meta must never be injected inside <textarea>"
    );
    const withoutTextarea = textareaBypass.replace(
      /<textarea\b[^>]*>[\s\S]*?<\/textarea>/gi,
      ""
    );
    assert.match(withoutTextarea, /<head>\s*<meta http-equiv="Content-Security-Policy"/i);
    assert.ok(
      textareaBypass.indexOf('http-equiv="Content-Security-Policy"') <
        textareaBypass.indexOf("<textarea>"),
      "CSP meta must appear before <textarea>"
    );

    // 3. <template><head></template> inert DocumentFragment bypass (plus nested <template>)
    const templateBypass = BuildCanvas.buildSandboxedPreviewDocument({
      files: [],
      entryHtml:
        "<!doctype html><html><template><head></template><body>hi</body></html>",
      cssBundle: "",
      jsBundle: "",
      hasPreviewableHtml: true,
    });
    assert.doesNotMatch(
      templateBypass,
      /<template\b[^>]*>[\s\S]*?Content-Security-Policy[\s\S]*?<\/template>/i,
      "CSP meta must never be injected inside <template>"
    );
    const withoutTemplate = templateBypass.replace(
      /<template\b[^>]*>[\s\S]*?<\/template>/gi,
      ""
    );
    assert.match(withoutTemplate, /<head>\s*<meta http-equiv="Content-Security-Policy"/i);
    assert.ok(
      templateBypass.indexOf('http-equiv="Content-Security-Policy"') <
        templateBypass.indexOf("<template>"),
      "CSP meta must appear before <template>"
    );

    const nestedTemplateBypass = BuildCanvas.buildSandboxedPreviewDocument({
      files: [],
      entryHtml:
        "<!doctype html><html><template><template></template><head></template><body>hi</body></html>",
      cssBundle: "",
      jsBundle: "",
      hasPreviewableHtml: true,
    });
    assert.ok(
      nestedTemplateBypass.indexOf('http-equiv="Content-Security-Policy"') <
        nestedTemplateBypass.indexOf("<template>"),
      "CSP meta must never be hijacked by nested <template> regions"
    );

    // 4. Quoted-attribute '>' bypass (<html data-x='><head>'> and double-quoted variant)
    const singleAttrBypass = BuildCanvas.buildSandboxedPreviewDocument({
      files: [],
      entryHtml: "<!doctype html><html data-x='><head>'><body>hi</body></html>",
      cssBundle: "",
      jsBundle: "",
      hasPreviewableHtml: true,
    });
    assert.ok(
      singleAttrBypass.includes("<html data-x='><head>'>"),
      "Original <html data-x='><head>'> attribute must remain completely intact"
    );
    assert.doesNotMatch(
      singleAttrBypass,
      /data-x='[^']*Content-Security-Policy[^']*'/i,
      "CSP meta must never be injected inside single-quoted attribute values"
    );
    const afterSingleHtmlTag = singleAttrBypass.slice(
      singleAttrBypass.indexOf("<html data-x='><head>'>") +
        "<html data-x='><head>'>".length
    );
    assert.match(
      afterSingleHtmlTag,
      /^\s*<head>\s*<meta http-equiv="Content-Security-Policy"/i
    );

    const doubleAttrBypass = BuildCanvas.buildSandboxedPreviewDocument({
      files: [],
      entryHtml: '<!doctype html><html data-x="><head>"><body>hi</body></html>',
      cssBundle: "",
      jsBundle: "",
      hasPreviewableHtml: true,
    });
    assert.ok(doubleAttrBypass.includes('<html data-x="><head>">'));
    const afterDoubleHtmlTag = doubleAttrBypass.slice(
      doubleAttrBypass.indexOf('<html data-x="><head>">') +
        '<html data-x="><head>">'.length
    );
    assert.match(
      afterDoubleHtmlTag,
      /^\s*<head>\s*<meta http-equiv="Content-Security-Policy"/i
    );

    // 5. Additional rawtext (<xmp>, <noscript>, <script> double-escaped) and <body><head> bypasses
    for (const [label, html] of [
      ["xmp", "<!doctype html><html><xmp><head></xmp><body>hi</body></html>"],
      ["noscript", "<!doctype html><html><noscript><head></noscript><body>hi</body></html>"],
      [
        "script_double_escaped",
        "<!doctype html><html><script><!-- <script></script><head></script><body>hi</body></html>",
      ],
      ["body_before_head", "<!doctype html><html><body><head>hi</body></html>"],
    ]) {
      const out = BuildCanvas.buildSandboxedPreviewDocument({
        files: [],
        entryHtml: html,
        cssBundle: "",
        jsBundle: "",
        hasPreviewableHtml: true,
      });
      const cspIdx = out.indexOf('http-equiv="Content-Security-Policy"');
      const bodyIdx = out.indexOf("<body>");
      assert.ok(cspIdx > 0, `${label}: CSP meta tag must be injected`);
      assert.ok(
        cspIdx < bodyIdx,
        `${label}: CSP meta tag must be injected in active <head> before <body>`
      );
      assert.match(
        out.slice(0, bodyIdx),
        /<head>\s*<meta http-equiv="Content-Security-Policy"/i,
        `${label}: CSP meta must reside in an active <head>`
      );
    }

    // 6. JS bundle injection must not be hijacked by </body> inside <textarea>
    const bodyEndInTextarea = BuildCanvas.buildSandboxedPreviewDocument({
      files: [],
      entryHtml:
        "<!doctype html><html><head></head><body><textarea></body></textarea><main>ok</main></body></html>",
      cssBundle: "",
      jsBundle: "window.__ok = true;",
      hasPreviewableHtml: true,
    });
    assert.doesNotMatch(
      bodyEndInTextarea,
      /<textarea>[\s\S]*window\.__ok[\s\S]*<\/textarea>/i,
      "Appended JS bundle must never be injected inside <textarea>"
    );
    assert.match(bodyEndInTextarea, /<main>ok<\/main>\s*<script>\s*window\.__ok = true;\s*<\/script>\s*<\/body>/i);
  });

  it("uses DOMParser when available in browser/Electron renderer and prepends CSP meta as doc.head.firstChild", () => {
    function makeMockElement(tagName, attrs = []) {
      const el = {
        nodeType: 1,
        tagName: tagName.toUpperCase(),
        localName: tagName.toLowerCase(),
        namespaceURI: "http://www.w3.org/1999/xhtml",
        attributes: attrs.map(([name, value]) => ({ name, value })),
        childNodes: [],
        get firstChild() {
          return this.childNodes[0] || null;
        },
        setAttribute(name, value) {
          const existing = this.attributes.find((a) => a.name === name);
          if (existing) existing.value = String(value);
          else this.attributes.push({ name, value: String(value) });
        },
        insertBefore(node, ref) {
          const idx = ref ? this.childNodes.indexOf(ref) : -1;
          if (idx === -1) this.childNodes.push(node);
          else this.childNodes.splice(idx, 0, node);
          return node;
        },
        appendChild(node) {
          this.childNodes.push(node);
          return node;
        },
        set textContent(val) {
          this.childNodes = [{ nodeType: 3, nodeValue: String(val) }];
        },
      };
      return el;
    }

    let parseCalls = 0;
    let createHeadCalls = 0;
    const prevDomParser = globalThis.DOMParser;
    globalThis.DOMParser = class MockDOMParser {
      parseFromString(html, mimeType) {
        parseCalls += 1;
        assert.equal(mimeType, "text/html");
        const htmlEl = makeMockElement("html", [["data-x", "><head>"]]);
        const bodyEl = makeMockElement("body");
        bodyEl.textContent = "hi";
        // Simulate missing head on first call, existing head with <title> on second call
        let headEl = null;
        if (parseCalls > 1) {
          headEl = makeMockElement("head");
          const titleEl = makeMockElement("title");
          titleEl.textContent = "<head>";
          headEl.appendChild(titleEl);
          htmlEl.appendChild(headEl);
        }
        htmlEl.appendChild(bodyEl);
        return {
          documentElement: htmlEl,
          get head() {
            return headEl;
          },
          body: bodyEl,
          createElement(tag) {
            if (tag.toLowerCase() === "head") createHeadCalls += 1;
            const created = makeMockElement(tag);
            if (tag.toLowerCase() === "head") headEl = created;
            return created;
          },
        };
      }
    };

    try {
      const noHeadOut = BuildCanvas.buildSandboxedPreviewDocument({
        files: [],
        entryHtml: "<!doctype html><html data-x='><head>'><body>hi</body></html>",
        cssBundle: "body { color: #fff; }",
        jsBundle: "if (1 < 2) { console.log('ok'); }",
        hasPreviewableHtml: true,
      });
      assert.equal(createHeadCalls, 1, "Must synthesize <head> if DOM doc.head is absent");
      assert.ok(noHeadOut.startsWith("<!DOCTYPE html>\n<html "));
      assert.match(
        noHeadOut,
        /<head><meta http-equiv="Content-Security-Policy" content="[^"]*connect-src 'none';"><style>/i
      );
      assert.ok(
        noHeadOut.includes("if (1 < 2)"),
        "Rawtext <script> must not entity-escape '<' characters"
      );

      const withTitleOut = BuildCanvas.buildSandboxedPreviewDocument({
        files: [],
        entryHtml:
          "<!doctype html><html><title><head></title><body>hi</body></html>",
        cssBundle: "",
        jsBundle: "",
        hasPreviewableHtml: true,
      });
      assert.match(
        withTitleOut,
        /<head><meta http-equiv="Content-Security-Policy" content="[^"]*"><title>&lt;head&gt;<\/title><\/head>/i,
        "CSP meta must be prepended as doc.head.firstChild before <title>"
      );
    } finally {
      if (prevDomParser === undefined) {
        delete globalThis.DOMParser;
      } else {
        globalThis.DOMParser = prevDomParser;
      }
    }
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
