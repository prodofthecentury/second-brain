const destEl = document.getElementById("qcDestination");
const tagsEl = document.getElementById("qcTags");
const titleEl = document.getElementById("qcTitle");
const bodyEl = document.getElementById("qcBody");
const saveBtn = document.getElementById("qcSave");
const cancelBtn = document.getElementById("qcCancel");
const statusEl = document.getElementById("qcStatus");

let isSaving = false;

function showStatus(msg, kind = "info") {
  if (!statusEl) return;
  if (!msg) {
    statusEl.style.display = "none";
    statusEl.textContent = "";
    statusEl.className = "status-banner";
    return;
  }
  statusEl.style.display = "block";
  statusEl.textContent = msg;
  statusEl.className =
    kind === "error"
      ? "status-banner banner-danger"
      : kind === "warn"
      ? "status-banner banner-warn"
      : "status-banner banner-ok";
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
  const isInbox = destEl && destEl.value === "inbox";
  saveBtn.textContent = isSaving
    ? "Saving..."
    : isInbox
    ? "Queue in Inbox"
    : "Save Note";
}

if (destEl) {
  destEl.addEventListener("change", syncDestinationUi);
}

function applyBackendStatus(st) {
  if (!st) return;
  if (st.state === "bootstrapping" || st.state === "starting") {
    showStatus(st.message || "Memory API is starting up...", "warn");
  } else if (st.state === "error") {
    showStatus(st.message || "Memory API is stopped or unavailable.", "error");
  } else if (st.state === "ready") {
    showStatus("");
  }
}

async function initCapture() {
  if (bodyEl) bodyEl.focus();
  try {
    const appSt = await window.secondBrain.getStatus();
    if (appSt && !appSt.ready && appSt.backendStatus) {
      applyBackendStatus(appSt.backendStatus);
    }
  } catch {}
}

if (typeof window.secondBrain.onBackendStatus === "function") {
  window.secondBrain.onBackendStatus((st) => applyBackendStatus(st));
}

if (typeof window.secondBrain.onCaptureFocus === "function") {
  window.secondBrain.onCaptureFocus(() => {
    showStatus("");
    if (bodyEl) bodyEl.focus();
  });
}

async function save() {
  if (isSaving) return;
  const body = (bodyEl ? bodyEl.value : "").trim();
  if (!body) {
    showStatus("Enter note content before saving.", "warn");
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
    let res;
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

    if (res && res.ok) {
      if (titleEl) titleEl.value = "";
      if (bodyEl) bodyEl.value = "";
      if (tagsEl) tagsEl.value = "";
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

if (saveBtn) saveBtn.addEventListener("click", save);
if (cancelBtn) {
  cancelBtn.addEventListener("click", () => {
    if (!isSaving) window.secondBrain.closeCapture();
  });
}
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !isSaving) {
    e.preventDefault();
    window.secondBrain.closeCapture();
  } else if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    save();
  }
});

initCapture();
