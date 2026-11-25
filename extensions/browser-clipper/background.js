"use strict";

if (typeof SecondBrainClipper === "undefined" && typeof importScripts === "function") {
  importScripts("lib/clipper-client.js");
}
if (typeof SecondBrainReadability === "undefined" && typeof importScripts === "function") {
  importScripts("lib/readability-markdown.js");
}

const STORAGE_KEY_PORT = "secondBrainPort";
const STORAGE_KEY_TOKEN = "secondBrainClipperToken";
const MENU_CLIP_SELECTION = "second-brain-clip-selection";
const MENU_CLIP_INBOX = "second-brain-clip-inbox";
const MENU_CLIP_SCREENSHOT = "second-brain-clip-screenshot";
const MENU_CLIP_PDF = "second-brain-clip-pdf";

function setupContextMenus() {
  if (!chrome.contextMenus) return;
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_CLIP_SELECTION,
      title: "Clip Selection to Second Brain (Working Note)",
      contexts: ["selection"],
    });
    chrome.contextMenus.create({
      id: MENU_CLIP_INBOX,
      title: "Clip Page/Selection to Second Brain (Quarantined Inbox)",
      contexts: ["selection", "page"],
    });
    chrome.contextMenus.create({
      id: MENU_CLIP_SCREENSHOT,
      title: "Capture Tab Screenshot (PNG) to Second Brain",
      contexts: ["page"],
    });
    chrome.contextMenus.create({
      id: MENU_CLIP_PDF,
      title: "Capture In-Browser PDF to Second Brain",
      contexts: ["page"],
    });
  });
}

chrome.runtime.onInstalled.addListener(setupContextMenus);
if (chrome.runtime.onStartup) {
  chrome.runtime.onStartup.addListener(setupContextMenus);
}

function flashBadge(text, color) {
  if (!chrome.action || !chrome.action.setBadgeText) return;
  chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setBadgeText({ text });
  setTimeout(() => chrome.action.setBadgeText({ text: "" }), 3000);
}

async function getPairedConfig() {
  const saved = await chrome.storage.local.get([STORAGE_KEY_PORT, STORAGE_KEY_TOKEN]);
  return {
    port: saved[STORAGE_KEY_PORT] || 8765,
    token: saved[STORAGE_KEY_TOKEN] || "",
  };
}

async function clipTabContent(tab, selectionText, destination) {
  if (!tab || !tab.url) return;
  const { port, token } = await getPairedConfig();
  if (!token) {
    flashBadge("PAIR", "#d97706");
    return;
  }

  let pageText = "";
  let selectedQuote = selectionText || "";
  try {
    if (chrome.scripting && tab.id) {
      const extractorFn =
        globalThis.SecondBrainReadability && globalThis.SecondBrainReadability.extractActiveTabContent
          ? globalThis.SecondBrainReadability.extractActiveTabContent
          : () => ({
              selection: String(window.getSelection ? window.getSelection() : "").trim(),
              pageText: document.body ? String(document.body.innerText || "").slice(0, 50000) : "",
            });
      const injected = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: extractorFn,
      });
      const resData = injected && injected[0] && injected[0].result;
      if (typeof resData === "string") {
        pageText = resData;
      } else if (resData) {
        pageText = resData.pageText || "";
        if (!selectedQuote && resData.selection) selectedQuote = resData.selection;
      }
    }
  } catch {
    pageText = "";
  }

  try {
    const res = await globalThis.SecondBrainClipper.sendWebClip({
      port,
      token,
      clip: {
        url: tab.url,
        title: tab.title || "",
        selectedQuote,
        pageText,
        tags: [],
        destination,
      },
    });
    flashBadge(res.quarantined ? "Q" : "OK", res.quarantined ? "#d97706" : "#16a34a");
  } catch {
    flashBadge("ERR", "#dc2626");
  }
}

async function clipTabScreenshot(tab, destination) {
  if (!tab || !tab.url || !chrome.tabs || !chrome.tabs.captureVisibleTab) return;
  const { port, token } = await getPairedConfig();
  if (!token) {
    flashBadge("PAIR", "#d97706");
    return;
  }
  try {
    const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId || null, { format: "png" });
    const res = await globalThis.SecondBrainClipper.sendClipperMedia({
      port,
      token,
      media: {
        url: tab.url,
        title: tab.title || "",
        kind: "screenshot_png",
        contentBase64: dataUrl,
        tags: [],
        destination: destination || "note",
      },
    });
    flashBadge(res.quarantined ? "Q" : "PNG", res.quarantined ? "#d97706" : "#16a34a");
  } catch {
    flashBadge("ERR", "#dc2626");
  }
}

async function clipTabPdf(tab, destination) {
  if (!tab || !tab.url) return;
  const { port, token } = await getPairedConfig();
  if (!token) {
    flashBadge("PAIR", "#d97706");
    return;
  }
  try {
    let base64Pdf = "";
    if (chrome.scripting && tab.id) {
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
        base64Pdf = (injected && injected[0] && injected[0].result) || "";
      } catch {
        base64Pdf = "";
      }
    }
    if (!base64Pdf) {
      const resp = await fetch(tab.url);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      base64Pdf = globalThis.SecondBrainClipper.arrayBufferToBase64(await resp.arrayBuffer());
    }
    const res = await globalThis.SecondBrainClipper.sendClipperMedia({
      port,
      token,
      media: {
        url: tab.url,
        title: tab.title || "",
        kind: "pdf_document",
        contentBase64: base64Pdf,
        tags: [],
        destination: destination || "note",
      },
    });
    flashBadge(res.quarantined ? "Q" : "PDF", res.quarantined ? "#d97706" : "#16a34a");
  } catch {
    flashBadge("ERR", "#dc2626");
  }
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === MENU_CLIP_SCREENSHOT) {
    await clipTabScreenshot(tab, "note");
    return;
  }
  if (info.menuItemId === MENU_CLIP_PDF) {
    await clipTabPdf(tab, "note");
    return;
  }
  const destination = info.menuItemId === MENU_CLIP_INBOX ? "inbox" : "note";
  await clipTabContent(tab, info.selectionText || "", destination);
});

if (chrome.commands && chrome.commands.onCommand) {
  chrome.commands.onCommand.addListener(async (command) => {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    const tab = tabs && tabs[0];
    if (!tab) return;
    if (command === "clip-page") {
      await clipTabContent(tab, "", "note");
    } else if (command === "capture-screenshot") {
      await clipTabScreenshot(tab, "note");
    } else if (command === "capture-pdf") {
      await clipTabPdf(tab, "note");
    }
  });
}
