"use strict";

/**
 * Pure loopback-only HTTP client for the Second Brain Browser Web Clipper.
 * Never transmits data off-device; rejects non-loopback hosts and invalid ports.
 */

const DEFAULT_PORT = 8765;
const CLIPPER_TOKEN_PREFIX = "clipper_";
const MAX_QUOTE_CHARS = 32000;
const MAX_PAGE_TEXT_CHARS = 200000;

function normalizeLoopbackPort(rawPort) {
  const effective = rawPort === undefined || rawPort === null || rawPort === "" ? DEFAULT_PORT : rawPort;
  const num = Number.parseInt(String(effective).trim(), 10);
  if (!Number.isInteger(num) || num < 1 || num > 65535) {
    throw new Error("Port must be an integer between 1 and 65535.");
  }
  return num;
}

function buildLoopbackBaseUrl(port) {
  const safePort = normalizeLoopbackPort(port);
  return `http://127.0.0.1:${safePort}`;
}

function validateClipperToken(token) {
  const cleaned = String(token || "").trim();
  if (!cleaned.startsWith(CLIPPER_TOKEN_PREFIX) || cleaned.length < 32) {
    throw new Error("Provide a valid paired scoped Clipper token (starts with 'clipper_').");
  }
  return cleaned;
}

function validateClipUrl(rawUrl) {
  const cleaned = String(rawUrl || "").trim();
  let parsed;
  try {
    parsed = new URL(cleaned);
  } catch {
    throw new Error("Active tab URL is not a valid http:// or https:// address.");
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error("Only http:// and https:// web pages can be clipped.");
  }
  if (parsed.username || parsed.password) {
    throw new Error("Refusing to clip a URL containing embedded credentials.");
  }
  return parsed.toString();
}

function parseTagsInput(rawTags) {
  if (Array.isArray(rawTags)) {
    return rawTags.map((t) => String(t || "").trim()).filter(Boolean).slice(0, 32);
  }
  return String(rawTags || "")
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean)
    .slice(0, 32);
}

function buildClipPayload({ url, title, selectedQuote, pageText, tags, destination }) {
  const cleanUrl = validateClipUrl(url);
  const cleanTitle = String(title || "").replace(/\s+/g, " ").trim().slice(0, 240);
  const cleanQuote = String(selectedQuote || "").trim().slice(0, MAX_QUOTE_CHARS);
  const cleanBody = String(pageText || "").trim().slice(0, MAX_PAGE_TEXT_CHARS);
  if (!cleanQuote && !cleanBody) {
    throw new Error("Nothing selected or extracted from the page to clip.");
  }
  const dest = destination === "inbox" ? "inbox" : "note";
  return {
    url: cleanUrl,
    title: cleanTitle,
    selected_quote: cleanQuote,
    page_text: cleanBody,
    tags: parseTagsInput(tags),
    destination: dest,
  };
}

async function clipperRequest({ port, token, method, path, body, fetchImpl }) {
  const fn = fetchImpl || globalThis.fetch;
  if (typeof fn !== "function") {
    throw new Error("fetch API is unavailable in this environment.");
  }
  const baseUrl = buildLoopbackBaseUrl(port);
  const safeToken = validateClipperToken(token);
  const cleanPath = String(path || "");
  if (!cleanPath.startsWith("/v1/clipper/")) {
    throw new Error("Clipper client is restricted to /v1/clipper/* endpoints.");
  }
  const headers = {
    Authorization: `Bearer ${safeToken}`,
  };
  const init = { method: method || "GET", headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const resp = await fn(`${baseUrl}${cleanPath}`, init);
  let data = {};
  try {
    data = await resp.json();
  } catch {
    data = { error: `HTTP ${resp.status}` };
  }
  if (!resp.ok) {
    throw new Error(data.error || `Loopback API returned HTTP ${resp.status}`);
  }
  return data;
}

async function testClipperConnection({ port, token, fetchImpl }) {
  return clipperRequest({ port, token, method: "GET", path: "/v1/clipper/status", fetchImpl });
}

async function sendWebClip({ port, token, clip, fetchImpl }) {
  const payload = buildClipPayload(clip);
  return clipperRequest({ port, token, method: "POST", path: "/v1/clipper/clip", body: payload, fetchImpl });
}

const exported = {
  DEFAULT_PORT,
  CLIPPER_TOKEN_PREFIX,
  normalizeLoopbackPort,
  buildLoopbackBaseUrl,
  validateClipperToken,
  validateClipUrl,
  parseTagsInput,
  buildClipPayload,
  clipperRequest,
  testClipperConnection,
  sendWebClip,
};

if (typeof module !== "undefined" && module.exports) {
  module.exports = exported;
} else {
  globalThis.SecondBrainClipper = exported;
}
