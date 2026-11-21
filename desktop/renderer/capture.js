"use strict";

const destEl = document.getElementById("qcDestination");
const tagsEl = document.getElementById("qcTags");
const titleEl = document.getElementById("qcTitle");
const bodyEl = document.getElementById("qcBody");
const saveBtn = document.getElementById("qcSave");
const cancelBtn = document.getElementById("qcCancel");
const closeTopBtn = document.getElementById("qcCloseTop");
const statusEl = document.getElementById("qcStatus");
const shortcutHintEl = document.getElementById("qcShortcutHint");
const mediaFileInputEl = document.getElementById("qcMediaFileInput");
const attachMediaBtn = document.getElementById("qcAttachMediaBtn");
const recordAudioBtn = document.getElementById("qcRecordAudioBtn");
const mediaBadgeEl = document.getElementById("qcMediaBadge");

let isSaving = false;
let pendingMedia = null; // { filename, content_base64, byteLength }
let activeRecorder = null;
let activeStream = null;
let audioChunks = [];

function applyPlatformLabels(platform) {
  if (platform !== "darwin") return;
  if (shortcutHintEl) {
    shortcutHintEl.textContent = "⌘+Enter save · Esc close";
  }
  if (bodyEl) {
    bodyEl.placeholder =
      "Capture a thought, snippet, decision, or paste a screenshot (⌘+V)... (⌘+Enter to save)";
  }
}

function showStatus(msg, kind = "info") {
  if (!statusEl) return;
  if (!msg) {
    statusEl.style.display = "none";
    statusEl.textContent = "";
    statusEl.className = "status-banner no-drag";
    return;
  }
  statusEl.style.display = "block";
  statusEl.textContent = msg;
  statusEl.className =
    kind === "error"
      ? "status-banner banner-danger no-drag"
      : kind === "warn"
      ? "status-banner banner-warn no-drag"
      : "status-banner banner-ok no-drag";
}

function updateMediaBadge() {
  if (!mediaBadgeEl) return;
  if (!pendingMedia) {
    mediaBadgeEl.style.display = "none";
    mediaBadgeEl.textContent = "";
    return;
  }
  mediaBadgeEl.style.display = "block";
  const kb = Math.max(1, Math.round((pendingMedia.byteLength || 0) / 1024));
  mediaBadgeEl.textContent = `Staged media: ${pendingMedia.filename} (${kb} KB) — click Save to ingest into 0-raw/.`;
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const str = String(reader.result || "");
      const idx = str.indexOf(",");
      resolve(idx >= 0 ? str.slice(idx + 1) : str);
    };
    reader.onerror = () => reject(new Error("Failed to read media bytes."));
    reader.readAsDataURL(blob);
  });
}

async function stageBlobForCapture(blob, fallbackName) {
  if (!blob || blob.size === 0) {
    showStatus("Selected media is empty.", "warn");
    return;
  }
  if (blob.size > 24 * 1024 * 1024) {
    showStatus("Media exceeds 24 MiB upload limit.", "error");
    return;
  }
  const content_base64 = await blobToBase64(blob);
  pendingMedia = {
    filename: fallbackName,
    content_base64,
    byteLength: blob.size,
  };
  updateMediaBadge();
  syncDestinationUi();
}

function parseTagsInput(raw) {
  const tags = String(raw || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return tags.length ? tags : ["quick-capture"];
}

function syncDestinationUi() {
  if (!saveBtn) return;
  if (isSaving) {
    saveBtn.textContent = "Saving...";
    return;
  }
  if (pendingMedia) {
    saveBtn.textContent = "Ingest Media";
    return;
  }
  const isInbox = destEl && destEl.value === "inbox";
  saveBtn.textContent = isInbox ? "Queue in Inbox" : "Save Note";
}

if (destEl) {
  destEl.addEventListener("change", syncDestinationUi);
}

if (attachMediaBtn && mediaFileInputEl) {
  attachMediaBtn.addEventListener("click", () => mediaFileInputEl.click());
  mediaFileInputEl.addEventListener("change", async () => {
    const f = mediaFileInputEl.files && mediaFileInputEl.files[0];
    if (f) {
      await stageBlobForCapture(f, f.name || "capture.bin");
    }
    mediaFileInputEl.value = "";
  });
}

window.addEventListener("paste", async (e) => {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  for (const item of items) {
    if (item.kind === "file" && item.type.startsWith("image/")) {
      const file = item.getAsFile();
      if (file) {
        e.preventDefault();
        const ext = (file.type.split("/")[1] || "png").replace(/[^a-z0-9]/gi, "") || "png";
        const stamp = new Date().toISOString().replace(/[:.]/g, "-");
        await stageBlobForCapture(file, `screenshot-${stamp}.${ext}`);
        return;
      }
    }
  }
});

function stopVoiceStream() {
  if (activeStream) {
    for (const track of activeStream.getTracks()) {
      track.stop();
    }
    activeStream = null;
  }
}

if (recordAudioBtn) {
  recordAudioBtn.addEventListener("click", async () => {
    if (activeRecorder && activeRecorder.state === "recording") {
      activeRecorder.stop();
      return;
    }
    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function" || typeof MediaRecorder === "undefined") {
      showStatus("Microphone recording is unavailable on this host.", "warn");
      return;
    }
    try {
      activeStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      audioChunks = [];
      activeRecorder = new MediaRecorder(activeStream);
      recordAudioBtn.textContent = "Stop Recording";
      showStatus("Recording voice memo... click Stop Recording when finished.", "info");
      activeRecorder.ondataavailable = (ev) => {
        if (ev.data && ev.data.size > 0) audioChunks.push(ev.data);
      };
      activeRecorder.onstop = async () => {
        recordAudioBtn.textContent = "Record Voice";
        stopVoiceStream();
        const blob = new Blob(audioChunks, { type: "audio/webm" });
        const stamp = new Date().toISOString().replace(/[:.]/g, "-");
        showStatus("");
        await stageBlobForCapture(blob, `voice-memo-${stamp}.webm`);
      };
      activeRecorder.start();
    } catch (err) {
      stopVoiceStream();
      recordAudioBtn.textContent = "Record Voice";
      showStatus(`Microphone unavailable: ${err?.message || String(err)}`, "error");
    }
  });
}

function applyBackendStatus(st) {
  if (!st) return;
  if (st.platform) {
    applyPlatformLabels(st.platform);
  }
  if (st.state === "bootstrapping" || st.state === "starting") {
    showStatus(st.message || "Memory API is starting up...", "warn");
  } else if (st.state === "error") {
    showStatus(st.message || "Memory API is stopped or unavailable.", "error");
  } else if (st.state === "ready") {
    showStatus("");
  }
}

async function initCapture() {
  if (window.secondBrain && window.secondBrain.platform) {
    applyPlatformLabels(window.secondBrain.platform);
  }
  if (bodyEl) bodyEl.focus();
  try {
    const appSt = await window.secondBrain.getStatus();
    if (appSt && appSt.platform) {
      applyPlatformLabels(appSt.platform);
    }
    if (appSt && !appSt.ready && appSt.backendStatus) {
      applyBackendStatus(appSt.backendStatus);
    }
  } catch {}
}

if (window.secondBrain && typeof window.secondBrain.onBackendStatus === "function") {
  window.secondBrain.onBackendStatus((st) => applyBackendStatus(st));
}

if (window.secondBrain && typeof window.secondBrain.onCaptureFocus === "function") {
  window.secondBrain.onCaptureFocus(() => {
    showStatus("");
    if (bodyEl) bodyEl.focus();
  });
}

async function save() {
  if (isSaving) return;
  const body = (bodyEl ? bodyEl.value : "").trim();
  if (!body && !pendingMedia) {
    showStatus("Enter note content or attach/paste media before saving.", "warn");
    if (bodyEl) bodyEl.focus();
    return;
  }

  const dest = destEl ? destEl.value : "notes";
  const tags = parseTagsInput(tagsEl ? tagsEl.value : "");
  const rawTitle = (titleEl ? titleEl.value : "").trim();
  const title = rawTitle || body.split(/\r?\n/)[0].slice(0, 60) || "Quick Note";

  isSaving = true;
  if (saveBtn) saveBtn.disabled = true;
  if (cancelBtn) cancelBtn.disabled = true;
  syncDestinationUi();
  showStatus("");

  try {
    let res = null;
    if (pendingMedia) {
      res = await window.secondBrain.request("POST", "/v1/ingest/media", {
        filename: pendingMedia.filename,
        content_base64: pendingMedia.content_base64,
      });
      if (!res || !res.ok) {
        const errMsg =
          res?.data?.error ||
          res?.error ||
          `Media ingestion failed (HTTP ${res?.status || "unavailable"})`;
        showStatus(errMsg, "error");
        return;
      }
    }

    if (body) {
      if (dest === "inbox") {
        const headerLines = [];
        if (rawTitle) headerLines.push(`# ${rawTitle}`);
        if (tags.length) headerLines.push(`Tags: ${tags.join(", ")}`);
        const content = headerLines.length
          ? `${headerLines.join("\n")}\n\n${body}`
          : body;
        res = await window.secondBrain.request("POST", "/v1/inbox", {
          content,
          source: "desktop_quick_capture",
        });
      } else {
        res = await window.secondBrain.request("POST", "/v1/notes", {
          title,
          body,
          tags,
        });
      }
    }

    if (res && res.ok) {
      if (titleEl) titleEl.value = "";
      if (bodyEl) bodyEl.value = "";
      if (tagsEl) tagsEl.value = "";
      pendingMedia = null;
      updateMediaBadge();
      showStatus("");
      await window.secondBrain.notifyMainRefresh();
      await window.secondBrain.closeCapture();
    } else {
      const errMsg =
        res?.data?.error ||
        res?.error ||
        `Save failed (HTTP ${res?.status || "unavailable"})`;
      showStatus(errMsg, "error");
      if (bodyEl) bodyEl.focus();
    }
  } catch (err) {
    showStatus(`Save failed: ${err?.message || String(err)}`, "error");
    if (bodyEl) bodyEl.focus();
  } finally {
    isSaving = false;
    if (saveBtn) saveBtn.disabled = false;
    if (cancelBtn) cancelBtn.disabled = false;
    syncDestinationUi();
  }
}

function requestClose() {
  stopVoiceStream();
  pendingMedia = null;
  updateMediaBadge();
  syncDestinationUi();
  if (!isSaving && window.secondBrain) {
    window.secondBrain.closeCapture();
  }
}

if (saveBtn) saveBtn.addEventListener("click", save);
if (cancelBtn) cancelBtn.addEventListener("click", requestClose);
if (closeTopBtn) closeTopBtn.addEventListener("click", requestClose);

window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !isSaving) {
    e.preventDefault();
    requestClose();
  } else if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    save();
  }
});

if (typeof window !== "undefined" && window.secondBrain) {
  initCapture();
}
