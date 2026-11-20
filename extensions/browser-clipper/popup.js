"use strict";

const STORAGE_KEY_PORT = "secondBrainPort";
const STORAGE_KEY_TOKEN = "secondBrainClipperToken";

let cachedPageText = "";

function $(id) {
  return document.getElementById(id);
}

function setStatus(state, text) {
  const dot = $("statusDot");
  dot.className = "dot" + (state ? ` ${state}` : "");
  $("statusText").textContent = text;
}

function showResult(kind, text) {
  const box = $("resultBox");
  box.className = `result-box ${kind}`;
  box.textContent = text;
}

async function loadActiveTabInfo() {
  try {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    const tab = tabs && tabs[0];
    if (!tab) return;
    $("titleInput").value = tab.title || "";
    $("urlInput").value = tab.url || "";
    if (chrome.scripting && tab.id && tab.url && /^https?:\/\//i.test(tab.url)) {
      const injected = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => ({
          selection: String(window.getSelection ? window.getSelection() : "").trim(),
          pageText: document.body ? String(document.body.innerText || "").slice(0, 80000) : "",
        }),
      });
      const data = injected && injected[0] && injected[0].result;
      if (data) {
        if (data.selection) $("quoteInput").value = data.selection;
        cachedPageText = data.pageText || "";
      }
    }
  } catch {
    cachedPageText = "";
  }
}

async function checkConnection(port, token) {
  if (!token) {
    setStatus("err", "Not paired — click Pair to register extension origin");
    $("settingsPanel").classList.remove("hidden");
    return false;
  }
  try {
    const res = await globalThis.SecondBrainClipper.testClipperConnection({ port, token });
    const vaultName = res.active_vault || "brain";
    setStatus("ok", `Connected to 127.0.0.1:${port} (${vaultName})`);
    return true;
  } catch (err) {
    setStatus("err", err && err.message ? err.message : "Loopback API unreachable");
    return false;
  }
}

async function initPopup() {
  const extOrigin =
    (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.getURL)
      ? new URL(chrome.runtime.getURL("")).origin
      : "";
  $("originOutput").value = extOrigin;

  const saved = await chrome.storage.local.get([STORAGE_KEY_PORT, STORAGE_KEY_TOKEN]);
  const port = saved[STORAGE_KEY_PORT] || 8765;
  const token = saved[STORAGE_KEY_TOKEN] || "";
  $("portInput").value = String(port);
  $("tokenInput").value = token;

  await loadActiveTabInfo();
  await checkConnection(port, token);

  $("toggleSettingsBtn").addEventListener("click", () => {
    $("settingsPanel").classList.toggle("hidden");
  });

  $("copyOriginBtn").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("originOutput").value);
      $("settingsHint").textContent = "Copied origin! Paste into Desktop System -> Browser Clipper or `second-brain clipper pair`.";
    } catch {
      $("originOutput").select();
      $("settingsHint").textContent = "Select and copy the origin above.";
    }
  });

  $("saveSettingsBtn").addEventListener("click", async () => {
    try {
      const safePort = globalThis.SecondBrainClipper.normalizeLoopbackPort($("portInput").value);
      const safeToken = globalThis.SecondBrainClipper.validateClipperToken($("tokenInput").value);
      await chrome.storage.local.set({ [STORAGE_KEY_PORT]: safePort, [STORAGE_KEY_TOKEN]: safeToken });
      const ok = await checkConnection(safePort, safeToken);
      $("settingsHint").textContent = ok
        ? "Paired & verified against local 127.0.0.1 API."
        : "Saved, but API rejected token or is offline.";
    } catch (err) {
      $("settingsHint").textContent = err && err.message ? err.message : "Invalid settings.";
    }
  });

  $("clipBtn").addEventListener("click", async () => {
    const btn = $("clipBtn");
    btn.disabled = true;
    try {
      const port = $("portInput").value || 8765;
      const token = $("tokenInput").value || "";
      const includePage = $("includePageTextCheck").checked;
      const res = await globalThis.SecondBrainClipper.sendWebClip({
        port,
        token,
        clip: {
          url: $("urlInput").value,
          title: $("titleInput").value,
          selectedQuote: $("quoteInput").value,
          pageText: includePage ? cachedPageText : "",
          tags: $("tagsInput").value,
          destination: $("destSelect").value,
        },
      });
      if (res.quarantined) {
        const snapNote = res.snapshot_sha256
          ? ` Snapshot SHA-256: ${String(res.snapshot_sha256).slice(0, 16)}…`
          : " (snapshot withheld until owner inbox review)";
        showResult(
          "warn",
          `Quarantined to ${res.location}${res.filename} (DLP/risk flags: ${(res.risk_flags || []).join(", ")}).${snapNote}`
        );
      } else {
        showResult(
          "ok",
          `Saved to ${res.location}${res.filename}. Verifiable snapshot: ${res.snapshot_path} (${String(res.snapshot_sha256 || "").slice(0, 16)}…).`
        );
      }
    } catch (err) {
      showResult("err", err && err.message ? err.message : "Failed to clip page.");
    } finally {
      btn.disabled = false;
    }
  });
}

document.addEventListener("DOMContentLoaded", initPopup);
