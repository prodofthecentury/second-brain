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

function buildClipPayload({ url, title, selectedQuote, pageText, rawHtml, tags, destination }) {
  const cleanUrl = validateClipUrl(url);
  const cleanTitle = String(title || "").replace(/\s+/g, " ").trim().slice(0, 240);
  const cleanQuote = String(selectedQuote || "").trim().slice(0, MAX_QUOTE_CHARS);
  const cleanBody = String(pageText || "").trim().slice(0, MAX_PAGE_TEXT_CHARS);
  const cleanHtml = String(rawHtml || "").trim().slice(0, 500000);
  if (!cleanQuote && !cleanBody && !cleanHtml) {
    throw new Error("Nothing selected or extracted from the page to clip.");
  }
  const dest = destination === "inbox" ? "inbox" : "note";
  const payload = {
    url: cleanUrl,
    title: cleanTitle,
    selected_quote: cleanQuote,
    page_text: cleanBody,
    tags: parseTagsInput(tags),
    destination: dest,
  };
  if (cleanHtml) {
    payload.raw_html = cleanHtml;
  }
  return payload;
}

function parsePairingCode(rawCode) {
  const cleaned = String(rawCode || "").trim().toUpperCase();
  const match = /^SBC-([A-Z0-9]{4})-([A-Z0-9]{4})-([A-Z0-9]{4})(?:-([0-9]{1,5}))?$/.exec(cleaned);
  if (!match) {
    throw new Error("Enter a valid 1-click pairing code (format: SBC-XXXX-XXXX-XXXX or SBC-XXXX-XXXX-XXXX-PORT).");
  }
  const baseCode = `SBC-${match[1]}-${match[2]}-${match[3]}`;
  let embeddedPort = null;
  if (match[4] !== undefined) {
    const num = Number.parseInt(match[4], 10);
    if (!Number.isInteger(num) || num < 1 || num > 65535) {
      throw new Error("Embedded loopback port in pairing code must be between 1 and 65535.");
    }
    embeddedPort = num;
  }
  return {
    code: embeddedPort !== null ? `${baseCode}-${embeddedPort}` : baseCode,
    baseCode,
    port: embeddedPort,
  };
}

function validatePairingCode(rawCode) {
  return parsePairingCode(rawCode).code;
}

async function redeemPairingCode({ port, code, origin, label, fetchImpl }) {
  const fn = fetchImpl || globalThis.fetch;
  if (typeof fn !== "function") {
    throw new Error("fetch API is unavailable in this environment.");
  }
  const parsedCode = parsePairingCode(code);
  const effectivePort = parsedCode.port !== null ? parsedCode.port : normalizeLoopbackPort(port);
  const baseUrl = buildLoopbackBaseUrl(effectivePort);
  const safeCode = parsedCode.code;
  const cleanOrigin = String(origin || "").trim().toLowerCase().replace(/\/+$/, "");
  if (!cleanOrigin) {
    throw new Error("Browser extension origin is unavailable.");
  }
  const resp = await fn(`${baseUrl}/v1/clipper/redeem`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      code: safeCode,
      origin: cleanOrigin,
      label: String(label || "Browser Clipper").trim().slice(0, 80) || "Browser Clipper",
    }),
  });
  let data = {};
  try {
    data = await resp.json();
  } catch {
    data = { error: `HTTP ${resp.status}` };
  }
  if (!resp.ok) {
    throw new Error(data.error || `Pairing code redemption failed (HTTP ${resp.status})`);
  }
  return { ...data, port: effectivePort };
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

function arrayBufferToBase64(buffer) {
  const bytes = buffer instanceof Uint8Array ? buffer : new Uint8Array(buffer || 0);
  if (typeof Buffer !== "undefined") {
    return Buffer.from(bytes).toString("base64");
  }
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

function buildMediaPayload({ url, title, kind, contentBase64, tags, destination }) {
  const cleanUrl = validateClipUrl(url);
  const cleanKind = String(kind || "").trim().toLowerCase();
  if (cleanKind !== "screenshot_png" && cleanKind !== "pdf_document") {
    throw new Error("Media kind must be 'screenshot_png' or 'pdf_document'.");
  }
  const cleanTitle = String(title || "").replace(/\s+/g, " ").trim().slice(0, 240);
  const rawB64 = String(contentBase64 || "").trim();
  if (!rawB64) {
    throw new Error("Captured media payload is empty.");
  }
  const dest = destination === "inbox" ? "inbox" : "note";
  return {
    url: cleanUrl,
    title: cleanTitle,
    kind: cleanKind,
    content_base64: rawB64,
    tags: parseTagsInput(tags),
    destination: dest,
  };
}

async function sendClipperMedia({ port, token, media, fetchImpl }) {
  const payload = buildMediaPayload(media);
  return clipperRequest({ port, token, method: "POST", path: "/v1/clipper/capture-media", body: payload, fetchImpl });
}

const exported = {
  DEFAULT_PORT,
  CLIPPER_TOKEN_PREFIX,
  normalizeLoopbackPort,
  buildLoopbackBaseUrl,
  validateClipperToken,
  parsePairingCode,
  validatePairingCode,
  validateClipUrl,
  parseTagsInput,
  arrayBufferToBase64,
  buildClipPayload,
  buildMediaPayload,
  clipperRequest,
  redeemPairingCode,
  testClipperConnection,
  sendWebClip,
  sendClipperMedia,
};

if (typeof module !== "undefined" && module.exports) {
  module.exports = exported;
} else {
  globalThis.SecondBrainClipper = exported;
}
