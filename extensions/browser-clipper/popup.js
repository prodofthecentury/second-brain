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
      const extractorFn =
        globalThis.SecondBrainReadability && globalThis.SecondBrainReadability.extractActiveTabContent
          ? globalThis.SecondBrainReadability.extractActiveTabContent
          : () => ({
              selection: String(window.getSelection ? window.getSelection() : "").trim(),
              pageText: document.body ? String(document.body.innerText || "").slice(0, 80000) : "",
            });
      const injected = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: extractorFn,
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

  if ($("oneClickPairBtn")) {
    $("oneClickPairBtn").addEventListener("click", async () => {
      const pairBtn = $("oneClickPairBtn");
      pairBtn.disabled = true;
      try {
        const code = $("pairCodeInput").value;
        const parsedPair = globalThis.SecondBrainClipper.parsePairingCode
          ? globalThis.SecondBrainClipper.parsePairingCode(code)
          : { port: null };
        const fallbackPort =
          parsedPair.port !== null
            ? parsedPair.port
            : globalThis.SecondBrainClipper.normalizeLoopbackPort($("portInput").value);
        const redeemed = await globalThis.SecondBrainClipper.redeemPairingCode({
          port: fallbackPort,
          code,
          origin: $("originOutput").value,
        });
        const activePort = Number.isInteger(redeemed.port) ? redeemed.port : fallbackPort;
        const mintedToken = String(redeemed.token || "");
        $("portInput").value = String(activePort);
        $("tokenInput").value = mintedToken;
        $("pairCodeInput").value = "";
        await chrome.storage.local.set({ [STORAGE_KEY_PORT]: activePort, [STORAGE_KEY_TOKEN]: mintedToken });
        const ok = await checkConnection(activePort, mintedToken);
        $("settingsHint").textContent = ok
          ? `1-click paired (${redeemed.pairing_id || "active"})!`
          : "Paired, verifying connection…";
        if (ok) {
          $("settingsPanel").classList.add("hidden");
        }
      } catch (err) {
        $("settingsHint").textContent = err && err.message ? err.message : "1-click pairing failed.";
      } finally {
        pairBtn.disabled = false;
      }
    });
  }

  function renderCaptureResult(res, kindLabel) {
    if (res.quarantined) {
      const snapNote = res.snapshot_sha256
        ? ` Snapshot SHA-256: ${String(res.snapshot_sha256).slice(0, 16)}…`
        : " (snapshot withheld until owner inbox review)";
      showResult(
        "warn",
        `Quarantined to ${res.location}${res.filename} (DLP/risk flags: ${(res.risk_flags || []).join(", ")}).${snapNote}`
      );
      return;
    }
    const sha = String(res.media_sha256 || res.snapshot_sha256 || "").slice(0, 16);
    const detail = res.media_archive_path ? ` Archive: ${res.media_archive_path}` : ` Snapshot: ${res.snapshot_path}`;
    showResult("ok", `${kindLabel} saved to ${res.location}${res.filename}.${detail} (${sha}…).`);
  }

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
      renderCaptureResult(res, "Clip");
    } catch (err) {
      showResult("err", err && err.message ? err.message : "Failed to clip page.");
    } finally {
      btn.disabled = false;
    }
  });

  if ($("captureScreenshotBtn")) {
    $("captureScreenshotBtn").addEventListener("click", async () => {
      const btn = $("captureScreenshotBtn");
      btn.disabled = true;
      try {
        const port = $("portInput").value || 8765;
        const token = $("tokenInput").value || "";
        const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
        const tab = tabs && tabs[0];
        if (!tab || !chrome.tabs.captureVisibleTab) {
          throw new Error("Visible tab screenshot API is unavailable.");
        }
        const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId || null, { format: "png" });
        const res = await globalThis.SecondBrainClipper.sendClipperMedia({
          port,
          token,
          media: {
            url: $("urlInput").value || tab.url,
            title: $("titleInput").value || tab.title,
            kind: "screenshot_png",
            contentBase64: dataUrl,
            tags: $("tagsInput").value,
            destination: $("destSelect").value,
          },
        });
        renderCaptureResult(res, "Screenshot");
      } catch (err) {
        showResult("err", err && err.message ? err.message : "Failed to capture screenshot.");
      } finally {
        btn.disabled = false;
      }
    });
  }

  if ($("capturePdfBtn")) {
    $("capturePdfBtn").addEventListener("click", async () => {
      const btn = $("capturePdfBtn");
      btn.disabled = true;
      try {
        const port = $("portInput").value || 8765;
        const token = $("tokenInput").value || "";
        const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
        const tab = tabs && tabs[0];
        const targetUrl = $("urlInput").value || (tab && tab.url) || "";
        let b64Pdf = "";
        if (chrome.scripting && tab && tab.id) {
          try {
            const injected = await chrome.scripting.executeScript({
              target: { tabId: tab.id },
              func: async () => {
                const resp = await fetch(window.location.href, { credentials: "include" });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const buf = await resp.arrayBuffer();
                if (buf.byteLength > 24 * 1024 * 1024) throw new Error("PDF exceeds 24 MiB limit");
                const bytes = new Uint8Array(buf);
                let bin = "";
                for (let i = 0; i < bytes.length; i += 0x8000) {
                  bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
                }
                return btoa(bin);
              },
            });
            b64Pdf = (injected && injected[0] && injected[0].result) || "";
          } catch {
            b64Pdf = "";
          }
        }
        if (!b64Pdf && targetUrl) {
          const resp = await fetch(targetUrl);
          if (!resp.ok) throw new Error(`Unable to fetch PDF (${resp.status}).`);
          b64Pdf = globalThis.SecondBrainClipper.arrayBufferToBase64(await resp.arrayBuffer());
        }
        const res = await globalThis.SecondBrainClipper.sendClipperMedia({
          port,
          token,
          media: {
            url: targetUrl,
            title: $("titleInput").value || (tab && tab.title) || "",
            kind: "pdf_document",
            contentBase64: b64Pdf,
            tags: $("tagsInput").value,
            destination: $("destSelect").value,
          },
        });
        renderCaptureResult(res, "PDF");
      } catch (err) {
        showResult("err", err && err.message ? err.message : "Failed to capture PDF.");
      } finally {
        btn.disabled = false;
      }
    });
  }
}

document.addEventListener("DOMContentLoaded", initPopup);
