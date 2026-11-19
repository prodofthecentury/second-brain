"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");

const clipper = require("../lib/clipper-client.js");
const EXT_DIR = path.resolve(__dirname, "..");

test("manifest.json enforces MV3 loopback-only host_permissions and CSP", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(EXT_DIR, "manifest.json"), "utf8"));
  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual(manifest.host_permissions, ["http://127.0.0.1/*"]);
  assert.ok(manifest.content_security_policy.extension_pages.includes("connect-src http://127.0.0.1:*"));
  assert.ok(manifest.browser_specific_settings && manifest.browser_specific_settings.gecko.id);
});

test("zero innerHTML usage across all browser clipper JS files", () => {
  for (const rel of ["background.js", "popup.js", "lib/clipper-client.js"]) {
    const content = fs.readFileSync(path.join(EXT_DIR, rel), "utf8");
    assert.equal(content.includes("innerHTML"), false, `Forbidden innerHTML in ${rel}`);
    assert.equal(content.includes("outerHTML"), false, `Forbidden outerHTML in ${rel}`);
    assert.equal(content.includes("document.write"), false, `Forbidden document.write in ${rel}`);
  }
});

test("clipper-client rejects invalid ports, non-clipper tokens, non-http URLs, and non-clipper paths", async () => {
  assert.throws(() => clipper.normalizeLoopbackPort(0));
  assert.throws(() => clipper.normalizeLoopbackPort(70000));
  assert.equal(clipper.buildLoopbackBaseUrl(8765), "http://127.0.0.1:8765");

  assert.throws(() => clipper.validateClipperToken("agent_full_token_must_fail_1234567890"));
  assert.throws(() => clipper.validateClipperToken("clipper_short"));
  assert.equal(
    clipper.validateClipperToken("clipper_abcdefghijklmnopqrstuvwxyz123456"),
    "clipper_abcdefghijklmnopqrstuvwxyz123456"
  );

  assert.throws(() => clipper.validateClipUrl("javascript:alert(1)"));
  assert.throws(() => clipper.validateClipUrl("file:///etc/passwd"));
  assert.throws(() => clipper.validateClipUrl("https://user:pass@example.com/secret"));
  assert.equal(clipper.validateClipUrl("https://example.com/article"), "https://example.com/article");

  await assert.rejects(
    () =>
      clipper.clipperRequest({
        port: 8765,
        token: "clipper_abcdefghijklmnopqrstuvwxyz123456",
        method: "GET",
        path: "/v1/atoms",
        fetchImpl: async () => {
          throw new Error("should not execute");
        },
      }),
    /restricted to \/v1\/clipper\//
  );
});

test("sendWebClip posts verifiable clip payload to 127.0.0.1 loopback server", async () => {
  let received = null;
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => {
      body += c;
    });
    req.on("end", () => {
      received = { method: req.method, url: req.url, auth: req.headers.authorization, body: JSON.parse(body || "{}") };
      res.writeHead(201, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          status: "saved",
          destination: "note",
          filename: "example-article.md",
          location: "notes/",
          snapshot_path: "0-raw/note-snapshots/clip-test.md",
          snapshot_sha256: "a".repeat(64),
          quarantined: false,
        })
      );
    });
  });

  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const port = server.address().port;
    const token = "clipper_abcdefghijklmnopqrstuvwxyz123456";
    const res = await clipper.sendWebClip({
      port,
      token,
      clip: {
        url: "https://example.com/post",
        title: "Example Post",
        selectedQuote: "Verbatim quote with exact provenance.",
        pageText: "Full cleaned page text.",
        tags: "project:alpha, research",
        destination: "note",
      },
    });
    assert.equal(res.status, "saved");
    assert.equal(received.method, "POST");
    assert.equal(received.url, "/v1/clipper/clip");
    assert.equal(received.auth, `Bearer ${token}`);
    assert.deepEqual(received.body.tags, ["project:alpha", "research"]);
    assert.equal(received.body.selected_quote, "Verbatim quote with exact provenance.");
  } finally {
    server.close();
  }
});
