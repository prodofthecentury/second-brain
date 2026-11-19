"use strict";

if (typeof importScripts === "function") {
  importScripts("lib/clipper-client.js");
}

const STORAGE_KEY_PORT = "secondBrainPort";
const STORAGE_KEY_TOKEN = "secondBrainClipperToken";
const MENU_CLIP_SELECTION = "second-brain-clip-selection";
const MENU_CLIP_INBOX = "second-brain-clip-inbox";

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

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (!tab || !tab.url) return;
  const destination = info.menuItemId === MENU_CLIP_INBOX ? "inbox" : "note";
  const saved = await chrome.storage.local.get([STORAGE_KEY_PORT, STORAGE_KEY_TOKEN]);
  const port = saved[STORAGE_KEY_PORT] || 8765;
  const token = saved[STORAGE_KEY_TOKEN] || "";
  if (!token) {
    flashBadge("PAIR", "#d97706");
    return;
  }

  let pageText = "";
  try {
    if (chrome.scripting && tab.id) {
      const injected = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => (document.body ? String(document.body.innerText || "").slice(0, 50000) : ""),
      });
      pageText = (injected && injected[0] && injected[0].result) || "";
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
        selectedQuote: info.selectionText || "",
        pageText,
        tags: [],
        destination,
      },
    });
    flashBadge(res.quarantined ? "Q" : "OK", res.quarantined ? "#d97706" : "#16a34a");
  } catch {
    flashBadge("ERR", "#dc2626");
  }
});
