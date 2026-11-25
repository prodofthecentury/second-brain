"use strict";

const MdHelper =
  (typeof window !== "undefined" && window.SecondBrainMarkdown) ||
  (typeof require === "function" ? require("./markdown") : null);
const PbHelper =
  (typeof window !== "undefined" && window.SecondBrainPlaybookUnits) ||
  (typeof require === "function" ? require("./playbook_units") : null);
const PageHelper =
  (typeof window !== "undefined" && window.SecondBrainPagination) ||
  (typeof require === "function" ? require("./pagination") : null);
const ChatHelper =
  (typeof window !== "undefined" && window.SecondBrainChatState) ||
  (typeof require === "function" ? require("./chat_state") : null);
const BuildHelper =
  (typeof window !== "undefined" && window.SecondBrainBuildCanvas) ||
  (typeof require === "function" ? require("./build_canvas") : null);
const GraphHelper =
  (typeof window !== "undefined" && window.SecondBrainGraph) ||
  (typeof require === "function" ? require("./graph") : null);
const ModalHelper =
  (typeof window !== "undefined" && window.SecondBrainModal) ||
  (typeof require === "function" ? require("./modal_helpers") : null);

let activeView = "notes";
let activeNote = null;
let notesCache = [];
let noteBaseline = { title: "", tags: "", body: "" };
let splitPreviewOpen = false;
let isChatStreaming = false;
let candidatesCache = [];
let playbooksCache = [];
let allLoadedAtoms = [];
let approvedAtomsCache = [];
let snapshotsCache = [];
let hostsCache = {};

let graphRawData = { nodes: [], edges: [] };
let graphViewer = null;

let activeVaultCategory = "threads";
let activeVaultDoc = null;
let vaultRawMode = false;

const activeJobsMap = new Map();
let jobPollTimer = null;

const notesPaginator = PageHelper.createPaginator({
  pageSize: 100,
  keyField: "filename",
});
const reviewQueuePaginator = PageHelper.createPaginator({
  pageSize: 50,
  keyField: "filename",
});
const atomsPaginator = PageHelper.createPaginator({
  pageSize: 100,
  keyField: "filename",
});
const ledgerLeavesPaginator = PageHelper.createPaginator({
  pageSize: 100,
  getKey: (item) => String(item?.rel_path || item?.filename || item || ""),
});
const ledgerHistoryPaginator = PageHelper.createPaginator({
  pageSize: 25,
  getKey: (item) => String(item?.seq ?? item?.entry_sha256 ?? ""),
});
const vaultPaginator = PageHelper.createPaginator({
  pageSize: 50,
  keyField: "filename",
});

const chatSession = ChatHelper.createChatSession();
let activeStreamingTurnId = null;
const chatAbortManager = ChatHelper.createStreamAbortManager({
  onAbortIpc: () => {
    if (window.secondBrain && typeof window.secondBrain.abortChat === "function") {
      return window.secondBrain.abortChat();
    }
    return Promise.resolve();
  },
  defaultTimeoutMs: 90000,
});

const DEFAULT_PRIVACY_NOTICE =
  "When this host's AI uses Second Brain tools, recalled verified memories and notes are sent to " +
  "that host's model provider (for example Anthropic, OpenAI, or Google). Your vault itself stays on " +
  "this machine, and agents can only add risk-scanned notes; they cannot verify or approve memories.";

// ─── Safe DOM & Toast Helpers ────────────────────────────────────────────────

function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "" && text !== null && text !== undefined) {
    node.textContent = String(text);
  }
  return node;
}

function makeKpiCard(label, value, sub = "", badgeClass = "") {
  const card = el("div", "kpi-card");
  card.appendChild(el("div", "kpi-label", label));
  if (badgeClass) {
    const wrap = el("div", "kpi-value");
    wrap.appendChild(el("span", `badge ${badgeClass}`, String(value)));
    card.appendChild(wrap);
  } else {
    card.appendChild(el("div", "kpi-value", String(value)));
  }
  if (sub) {
    card.appendChild(el("div", "kpi-sub", sub));
  }
  return card;
}

function showToast(message, kind = "info", durationMs = 4800) {
  const container = document.getElementById("toastContainer");
  if (!container || !message) return;
  const toast = el("div", `toast ${kind}`);
  toast.appendChild(el("span", "", message));
  const closeBtn = el("button", "toast-close", "×");
  closeBtn.type = "button";
  closeBtn.onclick = () => toast.remove();
  toast.appendChild(closeBtn);
  container.appendChild(toast);
  if (durationMs > 0) {
    setTimeout(() => {
      if (toast.isConnected) toast.remove();
    }, durationMs);
  }
}

function apiErrorMessage(res, fallback = "Request failed") {
  if (!res) return fallback;
  const status = Number(res.status || 0);
  const rawMsg =
    (res.data && typeof res.data.error === "string" && res.data.error) ||
    (typeof res.error === "string" && res.error) ||
    "";
  if (status === 403) {
    return rawMsg
      ? `403 Forbidden — ${rawMsg}`
      : "403 Forbidden — owner credential or host policy authorization required.";
  }
  if (status === 409) {
    return rawMsg
      ? `409 CAS Conflict — ${rawMsg} (refresh and retry)`
      : "409 CAS Conflict — item changed on disk; refresh and retry.";
  }
  if (status === 422) {
    return rawMsg
      ? `422 Validation Error — ${rawMsg}`
      : "422 Validation Error — check required fields and SHA-256 preconditions.";
  }
  if (status === 503) {
    const rem =
      res.data && typeof res.data.remediation === "string" && res.data.remediation
        ? ` (${res.data.remediation})`
        : "";
    return rawMsg
      ? `503 Service Unavailable — ${rawMsg}${rem}`
      : `503 Service Unavailable — Memory API sidecar or index is offline.${rem}`;
  }
  if (rawMsg) return rawMsg;
  return `${fallback} (HTTP ${status || "error"})`;
}

function parseTags(str) {
  return String(str || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function parseLines(str) {
  return String(str || "")
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function shortSha(sha) {
  return sha ? String(sha).slice(0, 12) : "—";
}

function openExternalSafe(url) {
  const clean = MdHelper.sanitizeExternalUrl(url);
  if (!clean) {
    showToast("Blocked unsafe external URL.", "warn");
    return;
  }
  if (window.secondBrain && typeof window.secondBrain.openExternal === "function") {
    window.secondBrain.openExternal(clean).catch((err) => {
      showToast(`Could not open link: ${err?.message || String(err)}`, "danger");
    });
  }
}

function renderRichMarkdown(container, markdown, extraOpts = {}) {
  return MdHelper.renderMarkdownSafe(container, markdown, {
    onOpenExternal: openExternalSafe,
    onWikiLinkClick: resolveAndOpenWikiLink,
    ...extraOpts,
  });
}

// ─── Modal Controller (Atom Editor, Atom Inspector & Confirmations) ──────────

const modalBackdrop = document.getElementById("atomModalBackdrop");
const modalTitleEl = document.getElementById("atomModalTitle");
const modalBodyEl = document.getElementById("atomModalBody");
const modalFooterEl = document.getElementById("atomModalFooter");
const modalCloseBtnEl = document.getElementById("atomModalCloseBtn");

const modalCtrl = ModalHelper.createModalController({
  backdropEl: modalBackdrop,
  titleEl: modalTitleEl,
  bodyEl: modalBodyEl,
  footerEl: modalFooterEl,
  closeBtnEl: modalCloseBtnEl,
});

function closeModal() {
  modalCtrl.close();
}

function appendTemporalValidityBadges(container, atom) {
  if (!atom || typeof atom !== "object") return;
  const vf = String(atom.valid_from || "").trim();
  const vu = String(atom.valid_until || "").trim();
  const ts = String(atom.temporal_status || "").trim();
  if (!vf && !vu && (!ts || ts === "active")) return;
  const cls =
    ts === "expired" || ts === "invalid_window"
      ? "danger"
      : ts === "not_yet_valid"
      ? "warn"
      : "ok";
  const winLabel = `${vf || "−∞"} -> ${vu || "+∞"}`;
  container.appendChild(
    el("span", `badge ${cls}`, `${ts ? ts.toUpperCase() + " " : ""}[${winLabel}]`)
  );
}

async function openAtomInspectorModal(filename, fallbackData = null) {
  if (!filename) return;
  modalCtrl.open({
    title: `Atom: ${filename}`,
    renderBody(body) {
      body.appendChild(el("div", "status-banner", "Loading atom details..."));
    },
  });

  let atom = fallbackData || {};
  try {
    const res = await window.secondBrain.api(
      "GET",
      `/v1/atoms/${encodeURIComponent(filename)}`
    );
    if (res.ok && res.data) {
      atom = res.data.atom || res.data;
    } else {
      const found = allLoadedAtoms.find((a) => a.filename === filename);
      if (found) atom = { ...atom, ...found };
    }
  } catch {}

  modalBodyEl.replaceChildren();
  const badges = el("div", "toolbar");
  badges.appendChild(
    el(
      "span",
      `badge ${atom.verified || atom.review_status === "accepted" ? "ok" : "warn"}`,
      atom.review_status || (atom.verified ? "accepted" : "atom")
    )
  );
  if (atom.memory_type) {
    badges.appendChild(el("span", "badge", atom.memory_type));
  }
  if (atom.origin) {
    badges.appendChild(el("span", "badge", `origin: ${atom.origin}`));
  }
  if (atom.sha256) {
    badges.appendChild(el("span", "badge mono", `sha256:${shortSha(atom.sha256)}`));
  }
  appendTemporalValidityBadges(badges, atom);
  const revDetails = atom.review_details || {};
  const meta = atom.metadata || {};
  const anchorVerified =
    revDetails.anchor_verified ??
    revDetails.source_anchor_verified ??
    revDetails.evidence_matches_source ??
    Boolean(atom.approved_current || atom.verified);
  const evStart =
    meta.evidence_start ?? revDetails.evidence_start ?? atom.evidence_start;
  const evEnd =
    meta.evidence_end ?? revDetails.evidence_end ?? atom.evidence_end;
  const evSrc =
    meta.evidence_source ||
    revDetails.evidence_source ||
    (Array.isArray(atom.sources) && atom.sources[0]) ||
    "";
  const offsetText =
    evStart !== undefined && evEnd !== undefined
      ? ` [${evStart}..${evEnd}]`
      : "";
  badges.appendChild(
    el(
      "span",
      `badge ${anchorVerified ? "ok" : "warn"}`,
      anchorVerified
        ? `Byte-Offset Source Anchor Verified ✓${offsetText}`
        : `Source Anchor Unverified${offsetText}`
    )
  );
  modalBodyEl.appendChild(badges);

  if (evSrc || meta.evidence_sha256) {
    modalBodyEl.appendChild(
      el(
        "div",
        "note-item-meta",
        `Provenance Source: ${evSrc || "—"}${
          offsetText ? ` · Byte Offsets:${offsetText}` : ""
        }${
          meta.evidence_sha256
            ? ` · Evidence SHA-256: ${shortSha(meta.evidence_sha256)}`
            : ""
        }`
      )
    );
  }

  const claimCol = el("div", "field-col");
  claimCol.appendChild(el("span", "field-label", "Claim"));
  claimCol.appendChild(el("div", "sub-panel", atom.claim || "(no claim text)"));
  modalBodyEl.appendChild(claimCol);

  const evText = atom.evidence || atom.excerpt || "";
  if (evText) {
    const evCol = el("div", "field-col");
    evCol.appendChild(el("span", "field-label", "Anchored Evidence"));
    evCol.appendChild(el("div", "mono-box", evText));
    modalBodyEl.appendChild(evCol);
  }

  if (Array.isArray(atom.sources) && atom.sources.length) {
    const srcCol = el("div", "field-col");
    srcCol.appendChild(el("span", "field-label", "Sources"));
    srcCol.appendChild(el("div", "note-item-meta", atom.sources.join(", ")));
    modalBodyEl.appendChild(srcCol);
  }

  if (atom.content) {
    const rawCol = el("div", "field-col");
    rawCol.appendChild(el("span", "field-label", "Canonical Markdown"));
    rawCol.appendChild(el("div", "mono-box", atom.content));
    modalBodyEl.appendChild(rawCol);
  }

  const editBtn = el("button", "btn", "Edit Atom Markdown");
  editBtn.type = "button";
  editBtn.onclick = () => openAtomEditorModal(atom);
  const doneBtn = el("button", "btn primary", "Done");
  doneBtn.type = "button";
  doneBtn.onclick = closeModal;
  modalFooterEl.replaceChildren(editBtn, doneBtn);
}

async function openAtomEditorModal(atomSummary) {
  const filename = atomSummary?.filename;
  if (!filename) return;
  modalCtrl.open({
    title: `Edit Atom: ${filename}`,
    renderBody(body) {
      body.appendChild(el("div", "status-banner", "Loading atom markdown..."));
    },
  });

  let fullAtom = { ...atomSummary };
  try {
    const res = await window.secondBrain.api(
      "GET",
      `/v1/atoms/${encodeURIComponent(filename)}`
    );
    if (res.ok && res.data) {
      fullAtom = { ...fullAtom, ...(res.data.atom || res.data) };
    }
  } catch (err) {
    showToast(`Failed to load atom: ${err?.message || String(err)}`, "danger");
  }

  const initialContent =
    fullAtom.content ||
    [
      "---",
      `id: ${filename.replace(/\.md$/i, "")}`,
      "type: atom",
      `memory_type: ${fullAtom.memory_type || "semantic"}`,
      `review_status: ${fullAtom.review_status || "pending"}`,
      "---",
      "",
      "## Claim",
      fullAtom.claim || "",
      "",
      "## Evidence",
      fullAtom.evidence || "",
      "",
    ].join("\n");

  modalBodyEl.replaceChildren();
  const info = el(
    "div",
    "status-banner",
    `Editing ${filename} (CAS sha256: ${shortSha(
      fullAtom.sha256
    )}). Saving transitions atom through owner CAS validation.`
  );
  const claimWrap = el("div", "field-col");
  claimWrap.appendChild(
    el(
      "span",
      "field-label",
      "Claim Text (Quick Edit — syncs into ## Claim section)"
    )
  );
  const claimInput = el("input");
  claimInput.type = "text";
  claimInput.value = fullAtom.claim || "";

  const ta = el("textarea", "editor-textarea");
  ta.style.minHeight = "260px";
  ta.value = initialContent;

  claimInput.addEventListener("input", () => {
    const nextClaim = claimInput.value;
    if (/^##\s+Claim[ \t]*\r?\n[\s\S]*?(?=\r?\n##\s+|\s*$)/m.test(ta.value)) {
      ta.value = ta.value.replace(
        /(^##\s+Claim[ \t]*\r?\n)[\s\S]*?(?=\r?\n##\s+|\s*$)/m,
        (_, hdr) => `${hdr}${nextClaim}\n`
      );
    }
  });

  claimWrap.appendChild(claimInput);

  const temporalRow = el("div", "form-grid");
  const vfCol = el("div", "field-col");
  vfCol.appendChild(el("span", "field-label", "Bi-Temporal valid_from (YYYY-MM-DD, optional)"));
  const vfInput = el("input");
  vfInput.type = "text";
  vfInput.placeholder = "e.g. 2026-01-01";
  vfInput.value = fullAtom.valid_from || "";
  vfCol.appendChild(vfInput);

  const vuCol = el("div", "field-col");
  vuCol.appendChild(el("span", "field-label", "Bi-Temporal valid_until (YYYY-MM-DD, optional)"));
  const vuInput = el("input");
  vuInput.type = "text";
  vuInput.placeholder = "e.g. 2026-12-31";
  vuInput.value = fullAtom.valid_until || "";
  vuCol.appendChild(vuInput);
  temporalRow.appendChild(vfCol);
  temporalRow.appendChild(vuCol);

  modalBodyEl.appendChild(info);
  modalBodyEl.appendChild(claimWrap);
  modalBodyEl.appendChild(temporalRow);
  modalBodyEl.appendChild(ta);

  const cancelBtn = el("button", "btn", "Cancel");
  cancelBtn.type = "button";
  cancelBtn.onclick = closeModal;

  const saveValidityBtn = el("button", "btn good", "Save Validity Window Only");
  saveValidityBtn.type = "button";
  saveValidityBtn.onclick = async () => {
    saveValidityBtn.disabled = true;
    try {
      const res = await window.secondBrain.api(
        "POST",
        `/v1/atoms/${encodeURIComponent(filename)}/validity`,
        {
          expected_sha256: fullAtom.sha256 || "",
          valid_from: vfInput.value.trim() || null,
          valid_until: vuInput.value.trim() || null,
          actor: "desktop_owner",
        }
      );
      if (res.ok) {
        showToast(
          `Updated validity window for ${filename} (${res.data?.temporal_status || "active"})`,
          "ok"
        );
        closeModal();
        if (activeView === "review") loadReviewQueue();
        if (activeView === "proof") loadProofView();
        if (activeView === "graph") loadGraphView();
        refreshHealth();
      } else {
        showToast(apiErrorMessage(res, "Validity update failed"), "danger");
      }
    } catch (err) {
      showToast(`Validity error: ${err?.message || String(err)}`, "danger");
    } finally {
      saveValidityBtn.disabled = false;
    }
  };

  const saveBtn = el("button", "btn primary", "Save Atom (CAS)");
  saveBtn.type = "button";
  saveBtn.onclick = async () => {
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving...";
    try {
      const putPayload = {
        content: ta.value,
        expected_sha256: fullAtom.sha256 || "",
        actor: "desktop_owner",
      };
      if (vfInput.value.trim() !== String(fullAtom.valid_from || "").trim()) {
        putPayload.valid_from = vfInput.value.trim() || null;
      }
      if (vuInput.value.trim() !== String(fullAtom.valid_until || "").trim()) {
        putPayload.valid_until = vuInput.value.trim() || null;
      }
      const res = await window.secondBrain.api(
        "PUT",
        `/v1/atoms/${encodeURIComponent(filename)}`,
        putPayload
      );
      if (res.ok) {
        showToast(`Saved atom ${filename}`, "ok");
        closeModal();
        if (activeView === "review") loadReviewQueue();
        if (activeView === "proof") loadProofView();
        if (activeView === "graph") loadGraphView();
        refreshHealth();
      } else {
        showToast(apiErrorMessage(res, "Atom edit failed"), "danger");
      }
    } catch (err) {
      showToast(`Atom edit failed: ${err?.message || String(err)}`, "danger");
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = "Save Atom (CAS)";
    }
  };
  modalFooterEl.replaceChildren(cancelBtn, saveValidityBtn, saveBtn);
}

// ─── View Navigation & Backend Status ────────────────────────────────────────

function switchView(name) {
  activeView = name;
  document.querySelectorAll(".nav-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === name);
  });
  document.querySelectorAll(".view").forEach((v) => {
    v.classList.toggle("active", v.id === `view-${name}`);
  });
  if (name === "chat") loadChatHosts();
  if (name === "review") loadReviewQueue();
  if (name === "import") {
    loadScoutQueue();
    loadCreativeMediaStudio();
  }
  if (name === "ide") {
    loadIdeStatus();
    loadGitHookStatus();
  }
  if (name === "system") {
    loadSystemView();
    loadDaemonStatus();
    loadMobileVaultClient();
  }
  if (name === "planner") {
    loadPlannerView();
    loadExecutableSkills();
    loadGen4Workbench();
  }
  if (name === "proof") loadProofView();
  if (name === "graph") loadGraphView();
  if (name === "vault") loadVaultBrowser({ append: false });
}

document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

function applyShortcutStatus(st) {
  if (!st) return;
  if (st.platform) {
    ModalHelper.applyPlatformShortcutsToDom(document, st.platform);
  }
  const warnEl = document.getElementById("shortcutWarningText");
  if (warnEl) {
    if (st.shortcutWarning) {
      warnEl.style.display = "block";
      warnEl.textContent = `⚠ ${st.shortcutWarning}`;
    } else {
      warnEl.style.display = "none";
      warnEl.textContent = "";
    }
  }
}

function resetActiveVaultUiState() {
  activeNote = null;
  activeVaultDoc = null;
  notesCache = [];
  const titleEl = document.getElementById("noteTitleInput");
  const tagsEl = document.getElementById("noteTagsInput");
  const bodyEl = document.getElementById("noteBodyInput");
  const bannerEl = document.getElementById("notesBanner");
  if (titleEl) titleEl.value = "";
  if (tagsEl) tagsEl.value = "";
  if (bodyEl) bodyEl.value = "";
  if (bannerEl) bannerEl.textContent = "Switched vault — select a note or create a new one.";
  if (typeof setNoteBaseline === "function") setNoteBaseline("", "", "");
  if (typeof renderBacklinksBar === "function") renderBacklinksBar([]);
  if (typeof renderNotePreview === "function") renderNotePreview();
  if (typeof renderActiveVaultDocument === "function") renderActiveVaultDocument();
  document
    .querySelectorAll(".note-item.active")
    .forEach((n) => n.classList.remove("active"));
}

function refreshActiveViewForCurrentVault() {
  loadChatHosts();
  loadDaemonStatus();
  loadGitHookStatus();
  loadMobileVaultClient();
  loadExecutableSkills();
  loadCreativeMediaStudio();
  if (activeView === "review") loadReviewQueue();
  if (activeView === "import") loadScoutQueue();
  if (activeView === "ide") loadIdeStatus();
  if (activeView === "system") loadSystemView();
  if (activeView === "planner") {
    loadPlannerView();
    loadGen4Workbench();
  }
  if (activeView === "proof") loadProofView();
  if (activeView === "graph") loadGraphView();
  if (activeView === "vault") loadVaultBrowser({ append: false });
}

function handleBackendStatus(st) {
  if (!st) return;
  applyShortcutStatus(st);
  const dot = document.getElementById("apiDot");
  const text = document.getElementById("apiStatusText");
  const startupBanner = document.getElementById("backendStartupBanner");
  const startupText = document.getElementById("backendStartupText");

  if (st.state === "ready") {
    dot.style.background = "#2ea043";
    dot.style.boxShadow = "0 0 10px rgba(46, 160, 67, 0.55)";
    text.textContent = `Loopback :${st.apiPort} ready`;
    startupBanner.className = "startup-banner";
    refreshHealth();
    loadNotes({ append: false });
    loadWorkspaces();
    refreshActiveViewForCurrentVault();
  } else if (st.state === "bootstrapping" || st.state === "starting") {
    dot.style.background = "#d29922";
    dot.style.boxShadow = "0 0 10px rgba(210, 153, 34, 0.55)";
    text.textContent =
      st.state === "bootstrapping" ? "Bootstrapping..." : "Starting API...";
    startupText.textContent = st.message || "Starting loopback Memory API...";
    startupBanner.className = "startup-banner warn visible";
  } else if (st.state === "error") {
    dot.style.background = "#f85149";
    dot.style.boxShadow = "0 0 10px rgba(248, 81, 73, 0.55)";
    text.textContent = "API stopped / error";
    startupText.textContent =
      st.message ||
      "Memory API sidecar is stopped. Use Tray -> Restart Memory API.";
    startupBanner.className = "startup-banner danger visible";
  }
}

if (
  window.secondBrain &&
  typeof window.secondBrain.onBackendStatus === "function"
) {
  window.secondBrain.onBackendStatus((st) => handleBackendStatus(st));
}

async function refreshHealth() {
  try {
    const status = await window.secondBrain.getStatus();
    applyShortcutStatus(status);
    if (!status.ready) {
      if (status.backendStatus) handleBackendStatus(status.backendStatus);
      return;
    }
    const res = await window.secondBrain.request("GET", "/v1/health");
    const dot = document.getElementById("apiDot");
    const text = document.getElementById("apiStatusText");
    const startupBanner = document.getElementById("backendStartupBanner");
    if (res.ok) {
      dot.style.background = "#2ea043";
      dot.style.boxShadow = "0 0 10px rgba(46, 160, 67, 0.55)";
      text.textContent = `Loopback :${status.port} ready`;
      startupBanner.className = "startup-banner";
    } else {
      dot.style.background = "#f85149";
      text.textContent = "API error";
    }
    const [atomsRes, notesRes] = await Promise.all([
      window.secondBrain.request("GET", "/v1/atoms?limit=1"),
      window.secondBrain.request("GET", "/v1/notes?limit=1"),
    ]);
    const nCount =
      notesRes.data?.total ??
      notesRes.data?.items?.length ??
      notesRes.data?.notes?.length ??
      notesCache.length;
    const aCount =
      res.data?.approved_atom_count ??
      atomsRes.data?.total ??
      atomsRes.data?.items?.length ??
      0;
    document.getElementById(
      "vaultMetrics"
    ).textContent = `Notes: ${nCount} · Verified Atoms: ${aCount}`;
  } catch {
    document.getElementById("apiStatusText").textContent = "Disconnected";
  }
}

// ─── TAB 1: Notes, Pagination, Safe Markdown Preview & Verify Excerpt ────────

function setNoteBaseline(title = "", tags = "", body = "") {
  noteBaseline = {
    title: String(title || ""),
    tags: String(tags || ""),
    body: String(body || ""),
  };
  updateDirtyIndicator();
}

function isNoteDirty() {
  const curTitle = document.getElementById("noteTitleInput").value;
  const curTags = document.getElementById("noteTagsInput").value;
  const curBody = document.getElementById("noteBodyInput").value;
  return (
    curTitle !== noteBaseline.title ||
    curTags !== noteBaseline.tags ||
    curBody !== noteBaseline.body
  );
}

function updateDirtyIndicator() {
  const badge = document.getElementById("noteDirtyBadge");
  if (badge) {
    badge.style.display = isNoteDirty() ? "inline-flex" : "none";
  }
}

async function confirmDiscardIfDirty() {
  if (!isNoteDirty()) return true;
  const res = await modalCtrl.confirm({
    title: "Unsaved Note Edits",
    message:
      "You have unsaved changes in the current note. Discard unsaved edits?",
    confirmLabel: "Discard Edits",
    confirmClass: "danger",
  });
  return res.confirmed;
}

async function resolveAndOpenWikiLink(rawTarget) {
  const clean = String(rawTarget || "").split("|")[0].trim();
  if (!clean) return;
  const norm = clean.toLowerCase();
  const match = notesCache.find((n) => {
    const fn = String(n.filename || "").toLowerCase();
    const stem = fn.replace(/\.md$/, "");
    const title = String(n.title || "").toLowerCase();
    return fn === norm || stem === norm || title === norm;
  });
  if (match) {
    switchView("notes");
    await openNote(match.filename);
    return;
  }
  const searchRes = await window.secondBrain.api(
    "GET",
    `/v1/notes?q=${encodeURIComponent(clean)}&limit=10`
  );
  const candidates = searchRes.data?.items || searchRes.data?.notes || [];
  const exact = candidates.find(
    (n) =>
      String(n.title || "").toLowerCase() === norm ||
      String(n.filename || "").replace(/\.md$/i, "").toLowerCase() === norm
  );
  if (exact) {
    switchView("notes");
    await openNote(exact.filename);
    return;
  }
  const createRes = await modalCtrl.confirm({
    title: "Create Linked Note",
    message: `No note titled "${clean}" exists yet. Create a new note for [[${clean}]]?`,
    confirmLabel: "Create Note",
  });
  if (createRes.confirmed) {
    if (!(await confirmDiscardIfDirty())) return;
    activeNote = null;
    document.getElementById("noteTitleInput").value = clean;
    document.getElementById("noteTagsInput").value = "";
    document.getElementById("noteBodyInput").value = `# ${clean}\n\n`;
    setNoteBaseline("", "", "");
    renderNotePreview();
    await saveActiveNote();
  }
}

function renderNotePreview() {
  if (!splitPreviewOpen) return;
  renderRichMarkdown(
    document.getElementById("notePreviewPane"),
    document.getElementById("noteBodyInput").value,
    {
      emptyPlaceholder:
        "Preview will appear as you type Markdown or [[WikiLinks]]...",
    }
  );
}

async function loadNotes({ append = false } = {}) {
  const query = document.getElementById("noteSearchInput").value.trim();
  const tag = document.getElementById("noteTagFilterInput").value.trim();
  if (!append) {
    notesPaginator.reset();
  }
  const pathUrl = PageHelper.buildPaginatedPath("/v1/notes", {
    limit: notesPaginator.getPageSize(),
    offset: notesPaginator.nextOffset(),
    params: { q: query, tag },
  });
  const res = await window.secondBrain.request("GET", pathUrl);
  if (!res.ok) return;

  notesPaginator.applyPage(res.data || {}, {
    append,
    preferredKey: "items",
  });
  notesCache = notesPaginator.getItems();

  const listEl = document.getElementById("notesList");
  listEl.replaceChildren();
  if (!notesCache.length) {
    listEl.appendChild(el("div", "status-banner", "No notes found."));
    PageHelper.renderPaginationBar(
      document.getElementById("notesPaginationBar"),
      notesPaginator,
      () => loadNotes({ append: true }),
      { noun: "notes" }
    );
    return;
  }

  for (const note of notesCache) {
    const item = el(
      "div",
      "note-item" +
        (activeNote && activeNote.filename === note.filename ? " active" : "")
    );
    item.dataset.filename = note.filename;
    item.appendChild(el("div", "note-item-title", note.title || note.filename));
    const tagText = (note.tags || []).length
      ? ` · ${(note.tags || []).join(", ")}`
      : "";
    item.appendChild(el("div", "note-item-meta", `${note.filename}${tagText}`));
    item.addEventListener("click", () => openNote(note.filename));
    listEl.appendChild(item);
  }

  PageHelper.renderPaginationBar(
    document.getElementById("notesPaginationBar"),
    notesPaginator,
    () => loadNotes({ append: true }),
    { noun: "notes" }
  );
}

function renderBacklinksBar(backlinks) {
  const blEl = document.getElementById("noteBacklinks");
  blEl.replaceChildren();
  blEl.appendChild(el("span", "", `Backlinks (${backlinks.length}):`));
  if (!backlinks.length) {
    blEl.appendChild(el("span", "note-item-meta", "none"));
    return;
  }
  for (const bl of backlinks) {
    const pill = el("button", "pill clickable", bl.title || bl.filename);
    pill.type = "button";
    pill.title = `Open ${bl.filename}`;
    pill.onclick = () => openNote(bl.filename);
    blEl.appendChild(pill);
  }
}

async function openNote(filename, { skipDirtyPrompt = false } = {}) {
  if (
    !skipDirtyPrompt &&
    activeNote?.filename !== filename &&
    !(await confirmDiscardIfDirty())
  ) {
    return;
  }
  const res = await window.secondBrain.request(
    "GET",
    `/v1/notes/${encodeURIComponent(filename)}`
  );
  if (!res.ok) {
    showToast(apiErrorMessage(res, `Could not open ${filename}`), "danger");
    return;
  }
  activeNote = res.data.note || res.data;
  const titleVal = activeNote.title || "";
  const tagsVal = (activeNote.tags || []).join(", ");
  const bodyVal = activeNote.body || "";

  document.getElementById("noteTitleInput").value = titleVal;
  document.getElementById("noteTagsInput").value = tagsVal;
  document.getElementById("noteBodyInput").value = bodyVal;
  setNoteBaseline(titleVal, tagsVal, bodyVal);
  renderNotePreview();

  const blRes = await window.secondBrain.request(
    "GET",
    `/v1/notes/${encodeURIComponent(filename)}/backlinks`
  );
  const backlinks = blRes.ok
    ? blRes.data?.items || blRes.data?.backlinks || []
    : [];
  renderBacklinksBar(backlinks);

  document.querySelectorAll(".note-item").forEach((node) => {
    node.classList.toggle("active", node.dataset.filename === filename);
  });

  const noteProjTag = Array.isArray(activeNote?.tags)
    ? activeNote.tags.find((t) => String(t || "").toLowerCase().startsWith("project:")) || ""
    : "";
  emitCognitiveContextSignal({
    activeFile: activeNote?.filename || filename,
    projectScope: noteProjTag,
    cursorTopic: titleVal || filename,
  });
}

async function saveActiveNote({ silentSuccessToast = false } = {}) {
  const titleInput = document.getElementById("noteTitleInput");
  const tagsInput = document.getElementById("noteTagsInput");
  const bodyInput = document.getElementById("noteBodyInput");
  const title = titleInput.value.trim() || "Untitled Note";
  const tags = parseTags(tagsInput.value);
  const body = bodyInput.value;
  const banner = document.getElementById("notesBanner");

  try {
    if (activeNote && activeNote.filename) {
      const res = await window.secondBrain.request(
        "PUT",
        `/v1/notes/${encodeURIComponent(activeNote.filename)}`,
        { title, tags, body, expected_sha256: activeNote.sha256 }
      );
      if (res.ok) {
        activeNote = {
          ...(activeNote || {}),
          title,
          tags,
          body,
          ...(res.data.note || res.data),
        };
        setNoteBaseline(titleInput.value, tagsInput.value, bodyInput.value);
        banner.textContent = `Saved ${activeNote.filename} (${shortSha(
          activeNote.sha256
        )})`;
        if (!silentSuccessToast) {
          showToast(`Saved ${activeNote.filename}`, "ok", 2500);
        }
        await loadNotes({ append: false });
        refreshHealth();
        return true;
      }
      const errMsg = apiErrorMessage(res, "Save failed");
      banner.textContent = errMsg;
      showToast(errMsg, "danger");
      return false;
    }

    const res = await window.secondBrain.request("POST", "/v1/notes", {
      title,
      tags,
      body,
    });
    if (res.ok) {
      activeNote = {
        ...(activeNote || {}),
        title,
        tags,
        body,
        ...(res.data.note || res.data),
      };
      setNoteBaseline(titleInput.value, tagsInput.value, bodyInput.value);
      banner.textContent = `Created ${activeNote.filename} (${shortSha(
        activeNote.sha256
      )})`;
      if (!silentSuccessToast) {
        showToast(`Created ${activeNote.filename}`, "ok", 2500);
      }
      await loadNotes({ append: false });
      refreshHealth();
      return true;
    }
    const errMsg = apiErrorMessage(res, "Create note failed");
    banner.textContent = errMsg;
    showToast(errMsg, "danger");
    return false;
  } catch (err) {
    const msg = `Save error: ${err?.message || String(err)}`;
    banner.textContent = msg;
    showToast(msg, "danger");
    return false;
  }
}

async function verifySelectionAsAtom() {
  const banner = document.getElementById("notesBanner");
  const textarea = document.getElementById("noteBodyInput");
  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;

  if (start === end) {
    const msg =
      "Highlight an excerpt in the note editor first (at least 12 characters) to verify it as an Atom.";
    banner.textContent = msg;
    showToast(msg, "warn");
    textarea.focus();
    return;
  }

  const excerpt = textarea.value.slice(start, end).trim();
  if (excerpt.length < 12) {
    const msg =
      "Highlighted excerpt must be at least 12 characters to anchor as an Atom.";
    banner.textContent = msg;
    showToast(msg, "warn");
    return;
  }

  if (!activeNote || !activeNote.filename || isNoteDirty()) {
    banner.textContent =
      "Auto-saving unsaved note edits before anchoring excerpt...";
    const saved = await saveActiveNote({ silentSuccessToast: true });
    if (!saved || !activeNote?.filename) {
      return;
    }
  }

  const memory_type = document.getElementById("verifyMemoryTypeSelect").value;
  const claimOverride = document.getElementById("verifyClaimInput").value.trim();
  const actor =
    document.getElementById("verifyActorInput").value.trim() || "desktop_owner";
  const tags = parseTags(document.getElementById("noteTagsInput").value);

  const payload = {
    excerpt,
    actor,
    expected_sha256: activeNote.sha256,
    expected_note_sha256: activeNote.sha256,
    memory_type,
    tags,
  };
  if (claimOverride) {
    payload.claim = claimOverride;
  }
  const verifyValidFrom = document.getElementById("verifyValidFromInput")?.value.trim();
  const verifyValidUntil = document.getElementById("verifyValidUntilInput")?.value.trim();
  if (verifyValidFrom) payload.valid_from = verifyValidFrom;
  if (verifyValidUntil) payload.valid_until = verifyValidUntil;

  try {
    const res = await window.secondBrain.request(
      "POST",
      `/v1/notes/${encodeURIComponent(activeNote.filename)}/verify`,
      payload
    );
    if (res.ok) {
      const atomFn = res.data?.atom_filename || res.data?.filename || "atom";
      const linked = Array.isArray(res.data?.auto_linked_atoms) ? res.data.auto_linked_atoms : [];
      const dupes = Array.isArray(res.data?.duplicate_candidates) ? res.data.duplicate_candidates : [];
      const conflicts = Array.isArray(res.data?.contradiction_alerts) ? res.data.contradiction_alerts : [];
      document.getElementById("verifyClaimInput").value = "";
      let linkInfo = "";
      if (linked.length) linkInfo += ` · Auto-linked: ${linked.join(", ")}`;
      if (dupes.length) linkInfo += ` · ${dupes.length} duplicate candidate(s)`;
      if (conflicts.length) linkInfo += ` · ${conflicts.length} contradiction alert(s)`;
      banner.textContent = `Verified Atom anchored: ${atomFn} (${
        res.data?.memory_type || memory_type
      })${linkInfo}`;
      showToast(`Promoted excerpt to Verified Atom: ${atomFn}${linkInfo}`, conflicts.length ? "warn" : "ok");
      refreshHealth();
    } else {
      const errMsg = apiErrorMessage(res, "Verify excerpt failed");
      banner.textContent = errMsg;
      showToast(errMsg, "danger");
    }
  } catch (err) {
    const errMsg = `Verify error: ${err?.message || String(err)}`;
    banner.textContent = errMsg;
    showToast(errMsg, "danger");
  }
}

async function deleteActiveNote() {
  if (!activeNote || !activeNote.filename) {
    showToast("Select a saved note first.", "warn");
    return;
  }
  const label = activeNote.title || activeNote.filename;
  const confirmDel = await modalCtrl.confirm({
    title: "Move Note to Trash",
    message: `Move note "${label}" (${activeNote.filename}) to trash?`,
    confirmLabel: "Move to Trash",
    confirmClass: "danger",
  });
  if (!confirmDel.confirmed) return;

  try {
    const res = await window.secondBrain.request(
      "DELETE",
      `/v1/notes/${encodeURIComponent(activeNote.filename)}`,
      { expected_sha256: activeNote.sha256 }
    );
    if (res.ok) {
      const deletedName = activeNote.filename;
      activeNote = null;
      document.getElementById("noteTitleInput").value = "";
      document.getElementById("noteTagsInput").value = "";
      document.getElementById("noteBodyInput").value = "";
      setNoteBaseline("", "", "");
      renderBacklinksBar([]);
      renderNotePreview();
      document.getElementById(
        "notesBanner"
      ).textContent = `Moved ${deletedName} to trash.`;
      showToast(`Moved ${deletedName} to trash`, "ok");
      await loadNotes({ append: false });
      refreshHealth();
    } else {
      showToast(apiErrorMessage(res, "Delete failed"), "danger");
    }
  } catch (err) {
    showToast(`Delete error: ${err?.message || String(err)}`, "danger");
  }
}

document.getElementById("newNoteBtn").addEventListener("click", async () => {
  if (!(await confirmDiscardIfDirty())) return;
  activeNote = null;
  document.getElementById("noteTitleInput").value = "";
  document.getElementById("noteTagsInput").value = "";
  document.getElementById("noteBodyInput").value = "";
  setNoteBaseline("", "", "");
  renderBacklinksBar([]);
  renderNotePreview();
  document
    .querySelectorAll(".note-item")
    .forEach((n) => n.classList.remove("active"));
  document.getElementById("noteTitleInput").focus();
});

document.getElementById("togglePreviewBtn").addEventListener("click", (e) => {
  splitPreviewOpen = !splitPreviewOpen;
  document
    .getElementById("editorSplitWrap")
    .classList.toggle("preview-open", splitPreviewOpen);
  e.currentTarget.classList.toggle("primary", splitPreviewOpen);
  renderNotePreview();
});

for (const inputId of ["noteTitleInput", "noteTagsInput", "noteBodyInput"]) {
  document.getElementById(inputId).addEventListener("input", () => {
    updateDirtyIndicator();
    if (inputId === "noteBodyInput") renderNotePreview();
  });
}

document
  .getElementById("saveNoteBtn")
  .addEventListener("click", () => saveActiveNote());
document
  .getElementById("verifyExcerptBtn")
  .addEventListener("click", verifySelectionAsAtom);
document
  .getElementById("deleteNoteBtn")
  .addEventListener("click", deleteActiveNote);
document
  .getElementById("noteSearchInput")
  .addEventListener("input", () => loadNotes({ append: false }));
document
  .getElementById("noteTagFilterInput")
  .addEventListener("input", () => loadNotes({ append: false }));

let notesTasksOpen = false;

async function loadNotesTasks() {
  const wrap = document.getElementById("notesTasksList");
  const proj = document.getElementById("tasksProjectInput").value.trim();
  const incCompleted = Boolean(
    document.getElementById("tasksIncludeCompletedCheck").checked
  );
  const incRaw = Boolean(
    document.getElementById("tasksIncludeRawCheck").checked
  );
  const params = [
    `include_completed=${incCompleted ? "1" : "0"}`,
    `include_raw=${incRaw ? "1" : "0"}`,
  ];
  if (proj) params.push(`project=${encodeURIComponent(proj)}`);
  wrap.replaceChildren(el("div", "note-item-meta", "Scanning action items..."));
  try {
    const res = await window.secondBrain.request(
      "GET",
      `/v1/notes/tasks?${params.join("&")}`
    );
    if (!res.ok) {
      wrap.replaceChildren(
        el("div", "status-banner banner-warn", apiErrorMessage(res, "Task scan failed"))
      );
      return;
    }
    const items = res.data?.tasks || res.data?.items || [];
    wrap.replaceChildren();
    if (!items.length) {
      wrap.appendChild(
        el("div", "note-item-meta", "No matching open tasks or action items found.")
      );
      return;
    }
    const list = el("div", "cards-grid");
    for (const t of items.slice(0, 100)) {
      const row = el("div", "sub-panel");
      const head = el("div", "toolbar");
      head.appendChild(
        el("span", `badge ${t.completed ? "ok" : "warn"}`, t.completed ? "DONE" : "OPEN")
      );
      if (t.priority) {
        head.appendChild(el("span", "badge danger", `P:${t.priority}`));
      }
      if (t.due_date) {
        head.appendChild(el("span", "badge mono", `due:${t.due_date}`));
      }
      head.appendChild(
        el(
          "span",
          "note-item-meta mono",
          `${t.filename || ""}:${t.line_number ?? "?"}`
        )
      );
      if (t.source_kind === "note" && t.filename) {
        const openBtn = el("button", "btn sm", "Open Note");
        openBtn.type = "button";
        openBtn.style.marginLeft = "auto";
        openBtn.onclick = () => selectNote(t.filename);
        head.appendChild(openBtn);
      }
      row.appendChild(head);
      row.appendChild(el("div", "", t.text || ""));
      list.appendChild(row);
    }
    wrap.appendChild(list);
  } catch (err) {
    wrap.replaceChildren(
      el("div", "status-banner banner-warn", `Task scan error: ${err?.message || String(err)}`)
    );
  }
}

document.getElementById("toggleTasksBtn").addEventListener("click", (e) => {
  notesTasksOpen = !notesTasksOpen;
  const card = document.getElementById("notesTasksCard");
  card.style.display = notesTasksOpen ? "block" : "none";
  e.currentTarget.classList.toggle("primary", notesTasksOpen);
  if (notesTasksOpen) loadNotesTasks();
});
document.getElementById("refreshTasksBtn").addEventListener("click", loadNotesTasks);

// ─── TAB 2: Upgraded Multi-Turn Streaming Chat ───────────────────────────────

const chatOutputEl = document.getElementById("chatOutput");
const chatSourcesEl = document.getElementById("chatSources");
const chatSourcesHeaderEl = document.getElementById("chatSourcesHeader");
const chatBannerEl = document.getElementById("chatBanner");
const chatWarningEl = document.getElementById("chatWarningBar");
const chatSendBtn = document.getElementById("chatSendBtn");
const chatStopBtn = document.getElementById("chatStopBtn");

function setChatStreamingState(streaming) {
  isChatStreaming = streaming;
  if (chatStopBtn) chatStopBtn.disabled = !streaming;
}

function populateHostSelectElement(selectEl, hostsObj) {
  if (!selectEl || !hostsObj || typeof hostsObj !== "object") return;
  const entries = Object.entries(hostsObj);
  if (!entries.length) return;
  const prev = selectEl.value || "ollama_local";
  selectEl.replaceChildren();
  for (const [hid, rule] of entries) {
    const opt = el(
      "option",
      "",
      `${hid}${rule && rule.enabled === false ? " (disabled)" : ""}`
    );
    opt.value = hid;
    selectEl.appendChild(opt);
  }
  if ([...selectEl.options].some((o) => o.value === prev)) {
    selectEl.value = prev;
  }
}

async function loadChatHosts() {
  try {
    const hostsRes = await window.secondBrain.request("GET", "/v1/hosts");
    let resolved = null;
    if (
      hostsRes.ok &&
      hostsRes.data &&
      typeof hostsRes.data === "object" &&
      !hostsRes.data.error
    ) {
      resolved = hostsRes.data.hosts || hostsRes.data;
    } else {
      const profRes = await window.secondBrain.request("GET", "/v1/profile");
      if (profRes.ok && profRes.data?.hosts) {
        resolved = profRes.data.hosts;
      }
    }
    if (resolved && Object.keys(resolved).length) {
      hostsCache = resolved;
      populateHostSelectElement(
        document.getElementById("chatHostSelect"),
        resolved
      );
      populateHostSelectElement(
        document.getElementById("planHostSelect"),
        resolved
      );
    }
    await loadChatThreads();
  } catch {}
}

async function loadChatThreads() {
  const sel = document.getElementById("chatThreadSelect");
  if (!sel) return;
  try {
    const res = await window.secondBrain.request("GET", "/v1/chat/threads?limit=50");
    if (!res.ok) return;
    const threads = Array.isArray(res.data?.threads) ? res.data.threads : [];
    const curId = chatSession.getThreadId() || sel.value || "";
    sel.replaceChildren();
    const blankOpt = el("option", "", "(New / Unsaved Session)");
    blankOpt.value = "";
    sel.appendChild(blankOpt);
    for (const th of threads) {
      const opt = el(
        "option",
        "",
        `${th.title || th.thread_id} (${th.turn_count ?? 0} turns)`
      );
      opt.value = th.thread_id;
      sel.appendChild(opt);
    }
    if ([...sel.options].some((o) => o.value === curId)) {
      sel.value = curId;
    }
  } catch {}
}

function tierBadgeInfo(src) {
  const cid = String(src.citation_id || "");
  const tier = String(src.tier || "").toLowerCase();
  if (tier === "verified" || cid.startsWith("V")) {
    return { cls: "tier-v", label: `[${cid || "V"}] Verified Atom` };
  }
  if (tier === "preference" || cid.startsWith("P")) {
    return { cls: "tier-p", label: `[${cid || "P"}] Preference` };
  }
  if (tier === "unverified_lead" || cid.startsWith("L")) {
    return { cls: "tier-l", label: `[${cid || "L"}] Unverified Lead` };
  }
  return { cls: "tier-n", label: `[${cid || "N"}] Working Note` };
}

function renderChatSourceCards(turn, highlightCitationId = null) {
  chatSourcesEl.replaceChildren();
  const sources = turn ? turn.sources || [] : [];
  if (chatSourcesHeaderEl) {
    chatSourcesHeaderEl.textContent = turn
      ? `Retrieved Sources (${sources.length}) — ${turn.query.slice(0, 36)}${
          turn.query.length > 36 ? "…" : ""
        }`
      : "Retrieved Sources (Click to Inspect)";
  }
  if (!sources.length) {
    chatSourcesEl.appendChild(
      el("div", "status-banner", "No matching sources retrieved for this turn.")
    );
    return;
  }

  let highlightedEl = null;
  const targetCid = highlightCitationId
    ? String(highlightCitationId).replace(/^\[|\]$/g, "").toUpperCase()
    : "";

  for (const src of sources) {
    const cid = String(src.citation_id || "").toUpperCase();
    const isMatch = Boolean(targetCid && cid === targetCid);
    const card = el(
      "div",
      `source-card${isMatch ? " highlighted" : ""}`
    );
    if (cid) card.dataset.citationId = cid;
    const badgeSpec = tierBadgeInfo(src);
    card.appendChild(el("span", `badge ${badgeSpec.cls}`, badgeSpec.label));
    card.appendChild(
      el("div", "source-card-title", src.title || src.filename || "Source")
    );
    if (src.filename && src.filename !== src.title) {
      card.appendChild(el("div", "source-card-meta", src.filename));
    }
    card.appendChild(
      el("div", "", src.claim || src.excerpt || src.snippet || "")
    );
    card.onclick = () => {
      const tier = String(src.tier || "").toLowerCase();
      if (tier === "note" || cid.startsWith("N")) {
        switchView("notes");
        openNote(src.filename);
      } else if (tier === "unverified_lead" || cid.startsWith("L")) {
        if (src.url) openExternalSafe(src.url);
      } else if (src.filename) {
        openAtomInspectorModal(src.filename, src);
      }
    };
    chatSourcesEl.appendChild(card);
    if (isMatch) highlightedEl = card;
  }

  if (highlightedEl && typeof highlightedEl.scrollIntoView === "function") {
    highlightedEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function handleCitationBadgeClick(turnId, citationId) {
  const turn = chatSession.selectTurn(turnId);
  if (!turn) return;
  renderAllChatTurns();
  renderChatSourceCards(turn, citationId);
}

function buildTurnGroundingElement(g) {
  const fullyCited = Boolean(g.fully_cited);
  const box = el("div", `grounding-bar ${fullyCited ? "ok" : "warn"}`);

  const headerRow = el("div", "toolbar");
  headerRow.appendChild(
    el(
      "span",
      `badge ${fullyCited ? "ok" : "warn"}`,
      fullyCited
        ? "Grounding Audit: Fully Cited"
        : "Grounding Audit: Citation Warnings"
    )
  );
  if (g.first_token_ms !== undefined && g.first_token_ms !== null) {
    headerRow.appendChild(
      el("span", "badge mono", `First token: ${g.first_token_ms} ms`)
    );
  }
  const cited = g.cited_sources || [];
  if (cited.length) {
    headerRow.appendChild(
      el("span", "note-item-meta", `Cited: ${cited.map((c) => `[${c}]`).join(" ")}`)
    );
  }
  const withheldCount = Number(
    g.withheld_claims ??
      g.withheld_unsupported_claims ??
      g.unsupported_claim_count ??
      (Array.isArray(g.uncited_sentences) ? g.uncited_sentences.length : 0)
  );
  headerRow.appendChild(
    el(
      "span",
      `badge ${withheldCount > 0 ? "warn" : "ok"}`,
      `Withheld / Unsupported: ${withheldCount}`
    )
  );
  box.appendChild(headerRow);

  const invalid = g.invalid_citations || [];
  if (invalid.length) {
    box.appendChild(
      el(
        "div",
        "",
        `Invalid / out-of-context citations flagged: ${invalid
          .map((c) => `[${c}]`)
          .join(", ")}`
      )
    );
  }

  const uncited = g.uncited_sentences || [];
  if (uncited.length) {
    box.appendChild(
      el("div", "field-label", `Uncited synthesis sentences (${uncited.length}):`)
    );
    const ul = el("ul");
    ul.style.margin = "2px 0 0";
    ul.style.paddingLeft = "18px";
    for (const s of uncited) {
      ul.appendChild(el("li", "", s));
    }
    box.appendChild(ul);
  }
  return box;
}

function buildTurnResearchLeadsWarningCard(turn) {
  const rl = turn.researchLeads;
  if (!rl) return null;
  const box = el("div", "chat-leads-warning");
  box.appendChild(
    el(
      "div",
      "chat-leads-warning-title",
      ChatHelper.RESEARCH_LEADS_WARNING_TITLE
    )
  );
  if (rl.text) {
    const body = el("div", "md-rendered");
    renderRichMarkdown(body, rl.text, {
      onCitationClick: (cid) => handleCitationBadgeClick(turn.id, cid),
    });
    box.appendChild(body);
  }
  const leads = Array.isArray(rl.leads) ? rl.leads : [];
  for (const ld of leads) {
    const item = el("div", "sub-panel");
    const top = el("div", "toolbar");
    if (ld.citation_id) {
      const cBtn = el("button", "citation-badge tier-l", `[${ld.citation_id}]`);
      cBtn.type = "button";
      cBtn.onclick = () => handleCitationBadgeClick(turn.id, ld.citation_id);
      top.appendChild(cBtn);
    } else {
      top.appendChild(el("span", "badge tier-l", "Unverified Lead"));
    }
    top.appendChild(
      el("strong", "", ld.title || ld.filename || ld.source || "Untrusted Lead")
    );
    item.appendChild(top);
    const snip = ld.snippet || ld.excerpt || ld.text || "";
    if (snip) item.appendChild(el("div", "note-item-meta", snip));
    box.appendChild(item);
  }
  return box;
}

function buildTurnVerifiedClaimsCards(turn) {
  const data = turn.verifiedClaims;
  if (!data || !data.answerable || !Array.isArray(data.claims) || !data.claims.length) {
    return null;
  }
  const wrap = el("div", "field-col");
  wrap.style.gap = "8px";
  for (const cl of data.claims) {
    const card = el("div", "claim-card");
    const top = el("div", "toolbar");
    top.appendChild(
      el(
        "span",
        `badge ${cl.is_preference ? "tier-p" : "tier-v"}`,
        cl.is_preference ? "Owner Preference" : "Verified Claim"
      )
    );
    for (const ev of cl.evidence || []) {
      if (ev.atom_id) {
        const pill = el("button", "pill clickable", ev.atom_id);
        pill.type = "button";
        pill.onclick = () => openAtomInspectorModal(ev.atom_id);
        top.appendChild(pill);
      }
    }
    card.appendChild(top);
    card.appendChild(el("div", "", cl.text || ""));
    for (const ev of cl.evidence || []) {
      const quote = ev.quote || ev.evidence_quote || "";
      if (quote) {
        card.appendChild(el("div", "claim-quote", `"${quote}"`));
      }
    }
    wrap.appendChild(card);
  }
  return wrap;
}

const debateBriefsByTurnId = new Map();
const cognitiveRoutesByTurnId = new Map();

function buildTurnCognitiveRouteCard(turn) {
  const route = turn?.cognitiveRoute || cognitiveRoutesByTurnId.get(turn?.id);
  if (!route || typeof route !== "object") return null;

  const wrap = el("div", "card");
  wrap.style.marginTop = "8px";
  wrap.style.gap = "6px";

  const hdr = el("div", "toolbar");
  hdr.appendChild(el("strong", "", "Cognitive Meta-Router"));
  if (route.mode) {
    hdr.appendChild(el("span", "badge ok mono", `mode:${String(route.mode)}`));
  }
  if (route.path) {
    hdr.appendChild(
      el(
        "span",
        `badge ${route.path === "deliberative_path" ? "warn" : "ok"} mono`,
        String(route.path)
      )
    );
  }
  if (route.complexity) {
    hdr.appendChild(
      el("span", "badge mono", `complexity:${String(route.complexity)}`)
    );
  }
  if (route.profile) {
    hdr.appendChild(el("span", "badge", `profile:${String(route.profile)}`));
  }
  if (route.contested) {
    hdr.appendChild(el("span", "badge warn", "contested"));
  }
  wrap.appendChild(hdr);

  const budget =
    route.budget && typeof route.budget === "object" ? route.budget : null;
  if (budget) {
    const bParts = [];
    if (budget.max_hops !== undefined) bParts.push(`max_hops=${budget.max_hops}`);
    if (budget.candidate_samples_k !== undefined) {
      bParts.push(`k=${budget.candidate_samples_k}`);
    }
    if (budget.max_rounds !== undefined) bParts.push(`rounds=${budget.max_rounds}`);
    if (budget.use_critic_pass) bParts.push("critic=on");
    if (bParts.length) {
      wrap.appendChild(
        el("div", "note-item-meta", `Budget: ${bParts.join(" · ")}`)
      );
    }
  }

  const signals =
    route.signals && typeof route.signals === "object" ? route.signals : null;
  if (signals) {
    const sigLines = [];
    if (
      signals.causal_simulation &&
      typeof signals.causal_simulation === "object"
    ) {
      const nodes = Array.isArray(signals.causal_simulation.impacted_nodes)
        ? signals.causal_simulation.impacted_nodes
        : [];
      const target = signals.causal_simulation.target
        ? ` (${String(signals.causal_simulation.target)})`
        : "";
      sigLines.push(`Impact Simulation${target}: ${nodes.length} impacted node(s)`);
    }
    if (
      signals.quantitative_derivation &&
      typeof signals.quantitative_derivation === "object"
    ) {
      const q = signals.quantitative_derivation;
      const ans = q.majority_answer ? ` = ${String(q.majority_answer)}` : "";
      sigLines.push(`Symbolic Sandbox: ${String(q.status || "ran")}${ans}`);
    }
    if (
      signals.cognitive_swarm &&
      typeof signals.cognitive_swarm === "object"
    ) {
      const roles =
        signals.cognitive_swarm.roles &&
        typeof signals.cognitive_swarm.roles === "object"
          ? signals.cognitive_swarm.roles
          : {};
      const cites = Array.isArray(roles.knowledge?.atom_citations)
        ? roles.knowledge.atom_citations.length
        : 0;
      const warns = Array.isArray(roles.critic?.contradiction_warnings)
        ? roles.critic.contradiction_warnings.length
        : 0;
      sigLines.push(
        `Cognitive Swarm: ${cites} citation(s), ${warns} contradiction warning(s)`
      );
    }
    if (sigLines.length) {
      const sigBox = el("div", "sub-panel");
      for (const line of sigLines) {
        sigBox.appendChild(el("div", "note-item-meta", line));
      }
      wrap.appendChild(sigBox);
    }
  }

  return wrap;
}

function buildTurnDebateBriefCard(turn) {
  const brief = turn?.debateBrief || debateBriefsByTurnId.get(turn?.id);
  if (!brief || typeof brief !== "object") return null;

  const wrap = el("div", "card");
  wrap.style.marginTop = "8px";
  wrap.style.gap = "8px";

  const hdr = el("div", "toolbar");
  hdr.appendChild(el("strong", "", "Two-Sided Debate Brief"));
  if (brief.topic) {
    hdr.appendChild(el("span", "badge", `Topic: ${String(brief.topic).slice(0, 64)}`));
  }
  const contras = Array.isArray(brief.contradictions_detected)
    ? brief.contradictions_detected
    : [];
  hdr.appendChild(
    el(
      "span",
      `badge ${contras.length > 0 ? "warn" : "ok"}`,
      `${contras.length} contradiction(s) detected`
    )
  );
  wrap.appendChild(hdr);

  const grid = el("div");
  grid.style.display = "grid";
  grid.style.gridTemplateColumns = "repeat(auto-fit, minmax(230px, 1fr))";
  grid.style.gap = "10px";

  const buildSideCol = (sideObj, fallbackTitle, badgeCls) => {
    const col = el("div", "sub-panel");
    col.style.display = "flex";
    col.style.flexDirection = "column";
    col.style.gap = "6px";

    const sideHdr = el("div", "toolbar");
    sideHdr.appendChild(el("span", `badge ${badgeCls}`, sideObj?.label || fallbackTitle));
    const claims = Array.isArray(sideObj?.claims) ? sideObj.claims : [];
    sideHdr.appendChild(el("span", "note-item-meta", `${claims.length} claim(s)`));
    col.appendChild(sideHdr);

    if (!claims.length) {
      col.appendChild(el("div", "note-item-meta", "No matching verified claims on this side."));
    } else {
      for (const c of claims) {
        const item = el("div", "claim-card");
        const top = el("div", "toolbar");
        if (c.filename) {
          const pill = el("button", "pill clickable", c.filename);
          pill.type = "button";
          pill.onclick = (evt) => {
            evt.stopPropagation();
            openAtomInspectorModal(c.filename, c);
          };
          top.appendChild(pill);
        }
        if (c.role_reason) {
          top.appendChild(el("span", "badge", c.role_reason));
        }
        if (c.confidence) {
          top.appendChild(el("span", "badge mono", `conf:${c.confidence}`));
        }
        item.appendChild(top);
        item.appendChild(el("div", "", c.claim || "(empty claim)"));
        if (Array.isArray(c.contexts) && c.contexts.length) {
          item.appendChild(el("div", "note-item-meta", `Contexts: ${c.contexts.join(", ")}`));
        }
        col.appendChild(item);
      }
    }

    const failures = Array.isArray(sideObj?.related_failures) ? sideObj.related_failures : [];
    if (failures.length) {
      col.appendChild(el("div", "field-label", `Historical Failure Warnings (${failures.length}):`));
      for (const f of failures) {
        col.appendChild(
          el(
            "div",
            "note-item-meta",
            `⚠ ${f.experience || "experience"}: ${f.excerpt || ""}`
          )
        );
      }
    }
    return col;
  };

  grid.appendChild(buildSideCol(brief.thesis, "Thesis (Supporting)", "ok"));
  grid.appendChild(buildSideCol(brief.antithesis, "Antithesis (Conflicting / Cautionary)", "warn"));
  wrap.appendChild(grid);

  const actions = Array.isArray(brief.owner_adjudication_actions)
    ? brief.owner_adjudication_actions
    : [];
  if (actions.length) {
    const actRow = el("div", "toolbar");
    actRow.appendChild(el("span", "field-label", "Owner Adjudication Options:"));
    for (const act of actions) {
      const tag = el("span", "badge", act.label || act.action || "Option");
      if (act.description) tag.title = String(act.description);
      actRow.appendChild(tag);
    }
    wrap.appendChild(actRow);
  }

  return wrap;
}

function buildChatTurnCard(turn, isSelected) {
  const card = el("div", `chat-turn-card${isSelected ? " selected" : ""}`);
  card.dataset.turnId = turn.id;
  card.addEventListener("click", () => {
    chatSession.selectTurn(turn.id);
    renderAllChatTurns();
    renderChatSourceCards(chatSession.getSelectedTurn());
  });

  const header = el("div", "chat-user-header");
  header.appendChild(el("div", "chat-user-query", turn.query));

  const metaBadges = el("div", "toolbar");
  metaBadges.appendChild(el("span", "badge mono", turn.mode));
  metaBadges.appendChild(el("span", "badge mono", turn.retrieval_mode));
  if (turn.answer_view && turn.answer_view !== "standard") {
    metaBadges.appendChild(el("span", "badge warn", `view:${turn.answer_view}`));
  }
  metaBadges.appendChild(el("span", "badge", turn.host_id));
  if (turn.retrieval_ms !== null) {
    metaBadges.appendChild(
      el("span", "badge mono", `ret:${turn.retrieval_ms}ms`)
    );
  }
  if (turn.total_ms !== null) {
    metaBadges.appendChild(el("span", "badge mono", `tot:${turn.total_ms}ms`));
  }
  if (turn.status === "cancelled") {
    metaBadges.appendChild(el("span", "badge warn", "Cancelled"));
  } else if (turn.status === "error") {
    metaBadges.appendChild(el("span", "badge danger", "Error"));
  }
  header.appendChild(metaBadges);
  card.appendChild(header);

  if (turn.warning) {
    card.appendChild(el("div", "status-banner banner-warn", turn.warning));
  }

  const cognitiveRouteCard = buildTurnCognitiveRouteCard(turn);
  if (cognitiveRouteCard) card.appendChild(cognitiveRouteCard);

  const body = el("div", "chat-assistant-body md-rendered");
  if (turn.assistantText) {
    renderRichMarkdown(body, turn.assistantText, {
      onCitationClick: (cid) => handleCitationBadgeClick(turn.id, cid),
    });
  } else if (turn.status === "error") {
    body.appendChild(
      el("div", "status-banner banner-danger", turn.error || "Stream failed")
    );
  } else if (turn.status === "cancelled") {
    body.appendChild(
      el("div", "note-item-meta", "Stream cancelled before completion.")
    );
  } else {
    body.appendChild(el("div", "note-item-meta", "Streaming response..."));
  }
  card.appendChild(body);

  const verifiedWrap = buildTurnVerifiedClaimsCards(turn);
  if (verifiedWrap) card.appendChild(verifiedWrap);

  const debateCard = buildTurnDebateBriefCard(turn);
  if (debateCard) card.appendChild(debateCard);

  const leadsCard = buildTurnResearchLeadsWarningCard(turn);
  if (leadsCard) card.appendChild(leadsCard);

  if (turn.grounding) {
    card.appendChild(buildTurnGroundingElement(turn.grounding));
  }

  return card;
}

function renderAllChatTurns() {
  const turns = chatSession.getTurns();
  const selected = chatSession.getSelectedTurn();
  chatOutputEl.replaceChildren();
  if (!turns.length) {
    chatOutputEl.appendChild(
      el(
        "div",
        "note-item-meta",
        "Ask a question above to search working notes, verified atoms, and owner preferences across a multi-turn session."
      )
    );
    return;
  }
  for (const t of turns) {
    chatOutputEl.appendChild(
      buildChatTurnCard(t, Boolean(selected && selected.id === t.id))
    );
  }
  refreshBuildCanvasFromTurn(selected);
}

if (window.secondBrain && typeof window.secondBrain.onChatEvent === "function") {
  window.secondBrain.onChatEvent(({ event, data }) => {
    if (!data || !activeStreamingTurnId) return;
    const turn = chatSession.applyEvent(activeStreamingTurnId, event, data);
    if (!turn) return;

    const resolvedEvent =
      event && event !== "message" ? event : data.type || "message";

    if (resolvedEvent === "context") {
      const ms =
        turn.retrieval_ms !== null ? `${turn.retrieval_ms} ms` : "instant";
      chatBannerEl.textContent = `Retrieved ${turn.sources.length} source(s) in ${ms}`;
      if (turn.warning) {
        chatWarningEl.style.display = "block";
        chatWarningEl.textContent = turn.warning;
      } else {
        chatWarningEl.style.display = "none";
      }
      renderChatSourceCards(turn);
    } else if (resolvedEvent === "cognitive_route") {
      const routeObj = data && typeof data === "object" ? { ...data } : null;
      if (routeObj) {
        turn.cognitiveRoute = routeObj;
        cognitiveRoutesByTurnId.set(turn.id, routeObj);
        const modeLbl = routeObj.mode || "deliberative";
        const pathLbl = routeObj.path || "deliberative_path";
        const profileLbl = routeObj.profile || "auto";
        chatBannerEl.textContent = `Meta-Router: mode=${modeLbl} · path=${pathLbl} · profile=${profileLbl}`;
      }
    } else if (resolvedEvent === "debate_brief") {
      const briefObj = data.brief && typeof data.brief === "object" ? data.brief : data;
      turn.debateBrief = briefObj;
      debateBriefsByTurnId.set(turn.id, briefObj);
      const tCount = Array.isArray(briefObj?.thesis?.claims) ? briefObj.thesis.claims.length : 0;
      const aCount = Array.isArray(briefObj?.antithesis?.claims) ? briefObj.antithesis.claims.length : 0;
      chatBannerEl.textContent = `Assembled Debate Brief: ${tCount} thesis vs ${aCount} antithesis claim(s).`;
    } else if (resolvedEvent === "cancelled") {
      chatBannerEl.textContent = "Chat stream cancelled.";
      setChatStreamingState(false);
    } else if (resolvedEvent === "error") {
      const errMsg = turn.error || "Stream failed";
      chatBannerEl.textContent = `Error: ${errMsg}`;
      showToast(errMsg, "danger");
      setChatStreamingState(false);
    } else if (resolvedEvent === "done") {
      if (turn.total_ms !== null) {
        chatBannerEl.textContent += ` · Completed in ${turn.total_ms} ms`;
      }
      setChatStreamingState(false);
      loadChatThreads();
    }

    renderAllChatTurns();
    const scrollBox = document.getElementById("chatMainScroll");
    if (scrollBox && resolvedEvent === "token") {
      scrollBox.scrollTop = scrollBox.scrollHeight;
    }
  });
}

document
  .getElementById("chatAnswerViewSelect")
  .addEventListener("change", (e) => {
    if (e.target.value === "preference") {
      document.getElementById("chatIncludePrefsCheck").checked = true;
    }
  });

async function sendChat() {
  const queryInput = document.getElementById("chatQueryInput");
  const query = queryInput.value.trim();
  if (!query) {
    showToast("Enter a question to query your vault.", "warn");
    return;
  }

  if (activeStreamingTurnId && chatAbortManager.isActive()) {
    chatSession.markCancelled(activeStreamingTurnId);
  }

  const streamHandle = chatAbortManager.beginStream({
    timeoutMs: 90000,
    onTimeout: () => {
      if (activeStreamingTurnId) {
        chatSession.applyEvent(activeStreamingTurnId, "error", {
          error: "Chat stream timed out after 90 seconds.",
        });
        renderAllChatTurns();
      }
      setChatStreamingState(false);
    },
  });

  const priorHistory = chatSession.buildPriorHistory();
  const threadSelVal = document.getElementById("chatThreadSelect")?.value || "";
  const activeTid = chatSession.getThreadId() || threadSelVal;

  const reqPayload = ChatHelper.buildChatStreamRequest({
    query,
    mode: document.getElementById("chatModeSelect").value,
    host_id: document.getElementById("chatHostSelect").value || "ollama_local",
    model_override: document.getElementById("chatModelInput").value,
    retrieval_mode: document.getElementById("chatRetrievalModeSelect").value,
    answer_view: document.getElementById("chatAnswerViewSelect").value,
    project_scope: document.getElementById("chatScopeInput").value,
    include_preferences: document.getElementById("chatIncludePrefsCheck")
      .checked,
    include_expired: Boolean(
      document.getElementById("chatIncludeExpiredCheck")?.checked
    ),
    as_of: document.getElementById("chatAsOfInput")?.value || "",
    thread_id: activeTid,
    work_mode: activeWorkMode,
    persona_id: activePersonaId,
    behavior_mode: document.getElementById("behaviorModeSelect")?.value || "autopilot",
    custom_rules: document.getElementById("chatCustomRulesInput")?.value || "",
    done_criteria: document.getElementById("chatDoneCriteriaInput")?.value || "",
    history: priorHistory,
  });

  const turn = chatSession.startTurn(reqPayload);
  activeStreamingTurnId = turn.id;
  queryInput.value = "";
  chatWarningEl.style.display = "none";
  chatBannerEl.textContent = "Retrieving context...";
  setChatStreamingState(true);
  renderAllChatTurns();
  renderChatSourceCards(turn);

  try {
    const res = await window.secondBrain.streamChat(reqPayload);
    if (
      res &&
      !res.ok &&
      !res.cancelled &&
      !streamHandle.signal.aborted &&
      turn.status === "streaming"
    ) {
      const errMsg = res.error || `HTTP ${res.status}`;
      chatSession.applyEvent(turn.id, "error", { error: errMsg });
      chatBannerEl.textContent = `Error: ${errMsg}`;
      renderAllChatTurns();
      showToast(errMsg, "danger");
    }
  } catch (err) {
    if (!streamHandle.signal.aborted) {
      const errMsg = err?.message || String(err);
      chatSession.applyEvent(turn.id, "error", { error: errMsg });
      chatBannerEl.textContent = `Error: ${errMsg}`;
      renderAllChatTurns();
      showToast(errMsg, "danger");
    }
  } finally {
    streamHandle.finish();
    if (activeStreamingTurnId === turn.id) {
      setChatStreamingState(false);
    }
  }
}

document.getElementById("chatSendBtn").addEventListener("click", sendChat);
document.getElementById("chatQueryInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendChat();
});
document.getElementById("chatStopBtn").addEventListener("click", () => {
  if (activeStreamingTurnId) {
    chatSession.markCancelled(activeStreamingTurnId);
  }
  chatAbortManager.abort("stop");
  setChatStreamingState(false);
  chatBannerEl.textContent = "Chat stream stopped by owner.";
  renderAllChatTurns();
});
document
  .getElementById("chatClearHistoryBtn")
  .addEventListener("click", () => {
    chatAbortManager.abort("clear");
    activeStreamingTurnId = null;
    setChatStreamingState(false);
    chatSession.clearHistory();
    debateBriefsByTurnId.clear();
    cognitiveRoutesByTurnId.clear();
    chatWarningEl.style.display = "none";
    chatSourcesEl.replaceChildren();
    if (chatSourcesHeaderEl) {
      chatSourcesHeaderEl.textContent = "Retrieved Sources (Click to Inspect)";
    }
    chatBannerEl.textContent = "Cleared multi-turn chat session history.";
    chatSession.setThreadId("");
    const thSel = document.getElementById("chatThreadSelect");
    if (thSel) thSel.value = "";
    renderAllChatTurns();
  });

const chatThreadSelEl = document.getElementById("chatThreadSelect");
if (chatThreadSelEl) {
  chatThreadSelEl.addEventListener("change", async () => {
    const tid = chatThreadSelEl.value;
    if (!tid) {
      chatSession.clearHistory();
      debateBriefsByTurnId.clear();
      cognitiveRoutesByTurnId.clear();
      chatSession.setThreadId("");
      renderAllChatTurns();
      chatSourcesEl.replaceChildren();
      return;
    }
    const res = await window.secondBrain.request("GET", `/v1/chat/threads/${encodeURIComponent(tid)}`);
    if (!res.ok) {
      showToast(apiErrorMessage(res, "Could not load chat thread"), "danger");
      return;
    }
    debateBriefsByTurnId.clear();
    cognitiveRoutesByTurnId.clear();
    const count = chatSession.loadThreadTurns(res.data || {});
    applyThreadBuildSettings(res.data || {});
    renderAllChatTurns();
    renderChatSourceCards(chatSession.getSelectedTurn());
    chatBannerEl.textContent = `Loaded thread '${res.data?.title || tid}' (${count} turn(s)).`;
  });
}

const chatNewThreadBtnEl = document.getElementById("chatNewThreadBtn");
if (chatNewThreadBtnEl) {
  chatNewThreadBtnEl.addEventListener("click", async () => {
    const scope = document.getElementById("chatScopeInput")?.value.trim() || undefined;
    const host_id = document.getElementById("chatHostSelect")?.value || "ollama_local";
    const mode = document.getElementById("chatModeSelect")?.value || "stream";
    const res = await window.secondBrain.request("POST", "/v1/chat/threads", {
      title: "",
      project_scope: scope,
      host_id,
      mode,
      work_mode: activeWorkMode,
      persona_id: activePersonaId,
      behavior_mode: document.getElementById("behaviorModeSelect")?.value || "autopilot",
      custom_rules: document.getElementById("chatCustomRulesInput")?.value || "",
      done_criteria: document.getElementById("chatDoneCriteriaInput")?.value || "",
    });
    if (!res.ok) {
      showToast(apiErrorMessage(res, "Could not create chat thread"), "danger");
      return;
    }
    chatSession.clearHistory();
    chatSession.setThreadId(res.data?.thread_id || "");
    await loadChatThreads();
    if (chatThreadSelEl && res.data?.thread_id) {
      chatThreadSelEl.value = res.data.thread_id;
    }
    renderAllChatTurns();
    chatSourcesEl.replaceChildren();
    chatBannerEl.textContent = `Started persistent thread ${res.data?.thread_id || ""}.`;
    showToast("Started new multi-turn chat thread", "ok");
  });
}

// ── Dual-Mode Chat vs Build Canvas, Personas & Per-Chat Rules ──
let activeWorkMode = "chat";
let activePersonaId = "grounded_default";
let activeRightPaneTab = "sources";
let lastExtractedArtifacts = { files: [], entryHtml: "", cssBundle: "", jsBundle: "", hasPreviewableHtml: false };
let lastPreviewSrcdoc = "";
let buildPreviewDebounceTimer = null;

function applyBuildPreviewSrcdoc(nextSrcdoc) {
  const previewFrame = document.getElementById("buildPreviewFrame");
  if (!previewFrame) return;
  const normalized = String(nextSrcdoc || "");
  if (normalized === lastPreviewSrcdoc) return;
  lastPreviewSrcdoc = normalized;
  previewFrame.srcdoc = normalized;
}

const personaChipEls = [
  document.getElementById("personaChipGrounded"),
  document.getElementById("personaChipCoding"),
  document.getElementById("personaChipWebsite"),
  document.getElementById("personaChipApp"),
  document.getElementById("personaChipGame"),
].filter(Boolean);

function setRightPaneTab(tabName) {
  activeRightPaneTab = tabName;
  const sourcesPane = document.getElementById("chatSourcesPane");
  const buildPane = document.getElementById("buildCanvasPane");
  const previewFrame = document.getElementById("buildPreviewFrame");
  const previewNote = document.getElementById("buildPreviewEmptyNote");
  const filesView = document.getElementById("buildFilesView");
  const critiqueView = document.getElementById("buildCritiqueView");

  const tabBtns = {
    sources: document.getElementById("buildPaneTabsSourcesBtn"),
    preview: document.getElementById("buildPaneTabsPreviewBtn"),
    files: document.getElementById("buildPaneTabsFilesBtn"),
    critique: document.getElementById("buildPaneTabsCritiqueBtn"),
  };
  for (const [key, btn] of Object.entries(tabBtns)) {
    if (btn) btn.classList.toggle("primary", key === tabName);
  }
  if (sourcesPane) sourcesPane.style.display = tabName === "sources" ? "flex" : "none";
  if (buildPane) buildPane.style.display = tabName === "sources" ? "none" : "flex";
  if (previewFrame) previewFrame.style.display = tabName === "preview" ? "block" : "none";
  if (previewNote) previewNote.style.display = tabName === "preview" ? "block" : "none";
  if (filesView) filesView.style.display = tabName === "files" ? "flex" : "none";
  if (critiqueView) critiqueView.style.display = tabName === "critique" ? "flex" : "none";
}

function setWorkMode(workMode, { autoSwitchTab = true } = {}) {
  activeWorkMode = workMode === "build" ? "build" : "chat";
  const gridEl = document.getElementById("chatLayoutGrid");
  const chatBtn = document.getElementById("workModeChatBtn");
  const buildBtn = document.getElementById("workModeBuildBtn");
  if (gridEl) gridEl.classList.toggle("build-mode-active", activeWorkMode === "build");
  if (chatBtn) chatBtn.classList.toggle("primary", activeWorkMode === "chat");
  if (buildBtn) buildBtn.classList.toggle("primary", activeWorkMode === "build");
  if (autoSwitchTab) {
    setRightPaneTab(activeWorkMode === "build" ? "preview" : "sources");
  }
}

function selectPersonaChip(personaId, { syncWorkMode = true } = {}) {
  const pid = String(personaId || "grounded_default");
  activePersonaId = pid;
  for (const chip of personaChipEls) {
    chip.classList.toggle("primary", chip.dataset.personaId === pid);
  }
  if (syncWorkMode && BuildHelper && Array.isArray(BuildHelper.PERSONA_CHIPS)) {
    const found = BuildHelper.PERSONA_CHIPS.find((p) => p.id === pid);
    if (found) {
      setWorkMode(found.defaultWorkMode, { autoSwitchTab: true });
    }
  }
}

function applyThreadBuildSettings(threadDoc) {
  if (!threadDoc || typeof threadDoc !== "object") return;
  if (threadDoc.persona_id) {
    selectPersonaChip(threadDoc.persona_id, { syncWorkMode: false });
  }
  if (threadDoc.work_mode) {
    setWorkMode(threadDoc.work_mode, { autoSwitchTab: false });
  }
  const behSel = document.getElementById("behaviorModeSelect");
  if (behSel && threadDoc.behavior_mode) {
    behSel.value = threadDoc.behavior_mode;
  }
  const rulesEl = document.getElementById("chatCustomRulesInput");
  if (rulesEl) rulesEl.value = String(threadDoc.custom_rules || "");
  const doneEl = document.getElementById("chatDoneCriteriaInput");
  if (doneEl) doneEl.value = String(threadDoc.done_criteria || "");
}

function refreshBuildCanvasFromTurn(turn) {
  if (!BuildHelper) return;
  const markdown = turn ? String(turn.assistantText || "") : "";
  const extracted = BuildHelper.extractArtifactFiles(markdown);
  lastExtractedArtifacts = extracted;

  const previewFrame = document.getElementById("buildPreviewFrame");
  if (previewFrame) {
    const title = turn?.query ? turn.query.slice(0, 60) : "Second Brain Build Preview";
    if (turn && turn.status === "streaming") {
      if (extracted.hasPreviewableHtml) {
        const nextDoc = BuildHelper.buildSandboxedPreviewDocument(extracted, { title });
        if (nextDoc !== lastPreviewSrcdoc) {
          if (buildPreviewDebounceTimer) clearTimeout(buildPreviewDebounceTimer);
          buildPreviewDebounceTimer = setTimeout(() => {
            buildPreviewDebounceTimer = null;
            applyBuildPreviewSrcdoc(nextDoc);
          }, 500);
        }
      }
    } else {
      if (buildPreviewDebounceTimer) {
        clearTimeout(buildPreviewDebounceTimer);
        buildPreviewDebounceTimer = null;
      }
      const nextDoc = BuildHelper.buildSandboxedPreviewDocument(extracted, { title });
      applyBuildPreviewSrcdoc(nextDoc);
    }
  }

  const filesSel = document.getElementById("buildFilesSelect");
  const filePre = document.getElementById("buildFileContentPre");
  if (filesSel && filePre) {
    const prevName = filesSel.value;
    filesSel.replaceChildren();
    if (!extracted.files.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "(No fenced code artifacts in selected turn)";
      filesSel.appendChild(opt);
      filePre.textContent = "";
    } else {
      for (const f of extracted.files) {
        const opt = document.createElement("option");
        opt.value = f.filename;
        opt.textContent = `${f.filename} (${f.lang}, ${f.content.length} chars)`;
        filesSel.appendChild(opt);
      }
      const chosen = extracted.files.find((f) => f.filename === prevName) || extracted.files[0];
      filesSel.value = chosen.filename;
      filePre.textContent = chosen.content;
    }
  }
  if (activeWorkMode === "build" && extracted.hasPreviewableHtml && activeRightPaneTab === "sources") {
    setRightPaneTab("preview");
  }
}

document.getElementById("workModeChatBtn")?.addEventListener("click", () => setWorkMode("chat"));
document.getElementById("workModeBuildBtn")?.addEventListener("click", () => setWorkMode("build"));
document.getElementById("buildPaneTabsSourcesBtn")?.addEventListener("click", () => setRightPaneTab("sources"));
document.getElementById("buildPaneTabsPreviewBtn")?.addEventListener("click", () => setRightPaneTab("preview"));
document.getElementById("buildPaneTabsFilesBtn")?.addEventListener("click", () => setRightPaneTab("files"));
document.getElementById("buildPaneTabsCritiqueBtn")?.addEventListener("click", () => setRightPaneTab("critique"));

for (const chip of personaChipEls) {
  chip.addEventListener("click", () => selectPersonaChip(chip.dataset.personaId || "grounded_default"));
}

document.getElementById("toggleBuildRulesBtn")?.addEventListener("click", () => {
  const drawer = document.getElementById("buildRulesDrawer");
  if (drawer) {
    drawer.style.display = drawer.style.display === "none" ? "flex" : "none";
  }
});

document.getElementById("saveThreadRulesBtn")?.addEventListener("click", async () => {
  const tid = chatSession.getThreadId() || document.getElementById("chatThreadSelect")?.value || "";
  if (!tid) {
    showToast("Rules saved for this local session (create/select a persistent thread to save to vault sidecar).", "ok");
    return;
  }
  const res = await window.secondBrain.request("PATCH", `/v1/chat/threads/${encodeURIComponent(tid)}`, {
    work_mode: activeWorkMode,
    persona_id: activePersonaId,
    behavior_mode: document.getElementById("behaviorModeSelect")?.value || "autopilot",
    custom_rules: document.getElementById("chatCustomRulesInput")?.value || "",
    done_criteria: document.getElementById("chatDoneCriteriaInput")?.value || "",
  });
  if (res.ok) {
    showToast("Saved per-thread Build Mode rules & DoD.", "ok");
  } else {
    showToast(apiErrorMessage(res, "Failed to save thread rules"), "danger");
  }
});

document.getElementById("buildFilesSelect")?.addEventListener("change", (e) => {
  const filePre = document.getElementById("buildFileContentPre");
  const found = (lastExtractedArtifacts.files || []).find((f) => f.filename === e.target.value);
  if (filePre) filePre.textContent = found ? found.content : "";
});

document.getElementById("buildCritiqueRunBtn")?.addEventListener("click", async () => {
  if (!BuildHelper) return;
  setRightPaneTab("critique");
  const outEl = document.getElementById("buildCritiqueOutput");
  if (!outEl) return;
  outEl.replaceChildren(el("div", "note-item-meta", "Running 5-Panelist Design Critique..."));
  let critiqueData = null;
  const apiRes = await window.secondBrain.request("POST", "/v1/design/critique", {
    files: lastExtractedArtifacts.files || [],
    persona_id: activePersonaId,
    done_criteria: document.getElementById("chatDoneCriteriaInput")?.value || "",
  });
  if (apiRes.ok && apiRes.data && Array.isArray(apiRes.data.panels)) {
    critiqueData = apiRes.data;
  } else {
    critiqueData = BuildHelper.runLocalDesignCritiquePreview(lastExtractedArtifacts);
  }
  outEl.replaceChildren();
  for (const p of critiqueData.panels || []) {
    const item = el("div", "card", "");
    item.appendChild(el("strong", "", `${p.panel} (${p.score}/5)`));
    item.appendChild(el("div", "note-item-meta", p.note || p.findings || ""));
    outEl.appendChild(item);
  }
});

document.getElementById("buildExportFolderBtn")?.addEventListener("click", async () => {
  const files = lastExtractedArtifacts.files || [];
  if (!files.length) {
    showToast("No extracted code files in the selected turn to export.", "warn");
    return;
  }
  if (!window.secondBrain || typeof window.secondBrain.exportBuildArtifacts !== "function") {
    showToast("Desktop folder export bridge unavailable.", "danger");
    return;
  }
  const exp = await window.secondBrain.exportBuildArtifacts(files);
  if (exp && exp.ok) {
    showToast(`Exported ${(exp.written || []).length} file(s) to ${exp.directory}`, "ok");
  } else if (!exp?.cancelled) {
    showToast(exp?.error || "Artifact export failed", "danger");
  }
});

refreshBuildCanvasFromTurn(null);

// ── Offline Two-Way Voice Loop (Whisper STT + Kokoro-82M ONNX TTS) ──
let activeVoiceAudio = null;
let activeVoiceAudioUrl = "";
let activeMediaRecorder = null;
let activeMediaStream = null;
let activeRecordedChunks = [];

function stopVoicePlayback() {
  if (activeVoiceAudio) {
    try {
      activeVoiceAudio.pause();
      activeVoiceAudio.src = "";
    } catch {}
    activeVoiceAudio = null;
  }
  if (activeVoiceAudioUrl) {
    try {
      URL.revokeObjectURL(activeVoiceAudioUrl);
    } catch {}
    activeVoiceAudioUrl = "";
  }
  const stopBtn = document.getElementById("chatStopVoiceBtn");
  if (stopBtn) stopBtn.disabled = true;
}

function playBase64WavAudio(audioB64) {
  if (!audioB64) return;
  stopVoicePlayback();
  const bin = atob(String(audioB64));
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) {
    bytes[i] = bin.charCodeAt(i);
  }
  const blob = new Blob([bytes], { type: "audio/wav" });
  activeVoiceAudioUrl = URL.createObjectURL(blob);
  const audio = new Audio(activeVoiceAudioUrl);
  activeVoiceAudio = audio;
  const stopBtn = document.getElementById("chatStopVoiceBtn");
  if (stopBtn) stopBtn.disabled = false;
  audio.addEventListener("ended", () => stopVoicePlayback());
  audio.addEventListener("error", () => stopVoicePlayback());
  audio.play().catch((err) => {
    stopVoicePlayback();
    showToast(`Voice playback error: ${err?.message || err}`, "warn");
  });
}

document.getElementById("chatStopVoiceBtn")?.addEventListener("click", () => {
  stopVoicePlayback();
  showToast("Stopped spoken playback.", "ok");
});

document.getElementById("chatReadAloudBtn")?.addEventListener("click", async () => {
  const sel = chatSession.getSelectedTurn();
  const textToSpeak = sel && sel.assistantText ? sel.assistantText.trim() : "";
  if (!textToSpeak) {
    showToast("Ask or select an assistant answer first to speak aloud.", "warn");
    return;
  }
  const res = await window.secondBrain.request("POST", "/v1/voice/speak", {
    text: textToSpeak.slice(0, 8000),
    voice: "af_heart",
    speed: 1.0,
  });
  if (!res.ok) {
    showToast(apiErrorMessage(res, "Kokoro TTS unavailable"), "warn");
    return;
  }
  if (res.data?.audio_b64) {
    playBase64WavAudio(res.data.audio_b64);
  }
});

function uint8ToBase64(u8) {
  const CHUNK_SIZE = 0x8000;
  const parts = [];
  for (let i = 0; i < u8.length; i += CHUNK_SIZE) {
    parts.push(String.fromCharCode.apply(null, u8.subarray(i, i + CHUNK_SIZE)));
  }
  return btoa(parts.join(""));
}

document.getElementById("chatVoiceTurnBtn")?.addEventListener("click", async () => {
  const micBtn = document.getElementById("chatVoiceTurnBtn");
  if (activeMediaRecorder && activeMediaRecorder.state === "recording") {
    activeMediaRecorder.stop();
    if (micBtn) micBtn.textContent = "Mic";
    return;
  }
  if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function" || typeof MediaRecorder === "undefined") {
    showToast("Microphone recording is unavailable in this environment.", "warn");
    return;
  }
  stopVoicePlayback();
  try {
    activeMediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    activeRecordedChunks = [];
    activeMediaRecorder = new MediaRecorder(activeMediaStream);
    activeMediaRecorder.addEventListener("dataavailable", (evt) => {
      if (evt.data && evt.data.size > 0) activeRecordedChunks.push(evt.data);
    });
    activeMediaRecorder.addEventListener("stop", async () => {
      try {
        if (activeMediaStream) {
          for (const tr of activeMediaStream.getTracks()) tr.stop();
          activeMediaStream = null;
        }
        const blob = new Blob(activeRecordedChunks, { type: "audio/webm" });
        activeRecordedChunks = [];
        const arrBuf = await blob.arrayBuffer();
        const u8 = new Uint8Array(arrBuf);
        const b64 = uint8ToBase64(u8);
        chatBannerEl.textContent = "Transcribing speech & running grounded voice turn...";
        const activeTid =
          chatSession.getThreadId() ||
          document.getElementById("chatThreadSelect")?.value ||
          "";
        const voicePayload = {
          audio_b64: b64,
          filename: "voice-turn.webm",
          mode: document.getElementById("chatModeSelect")?.value || "stream",
          host_id: document.getElementById("chatHostSelect")?.value || "ollama_local",
          project_scope: document.getElementById("chatScopeInput")?.value || "",
        };
        if (activeTid) {
          voicePayload.thread_id = activeTid;
        }
        const res = await window.secondBrain.request(
          "POST",
          "/v1/voice/conversation-turn",
          voicePayload
        );
        if (!res.ok) {
          const errMsg = apiErrorMessage(res, "Voice turn failed");
          chatBannerEl.textContent = `Voice turn error: ${errMsg}`;
          showToast(errMsg, "danger");
          return;
        }
        if (res.data?.thread_id) {
          chatSession.setThreadId(res.data.thread_id);
        }
        const t = chatSession.startTurn({
          query: res.data?.transcript || "(Voice input)",
          mode: document.getElementById("chatModeSelect")?.value || "stream",
        });
        chatSession.applyEvent(t.id, "context", {
          sources: res.data?.sources || [],
          host_id: document.getElementById("chatHostSelect")?.value || "ollama_local",
          thread_id: res.data?.thread_id || activeTid || undefined,
        });
        chatSession.applyEvent(t.id, "token", { text: res.data?.answer_text || "" });
        if (res.data?.grounding) chatSession.applyEvent(t.id, "grounding", res.data.grounding);
        chatSession.applyEvent(t.id, "done", {});
        chatBannerEl.textContent = `Voice turn complete (${(res.data?.sources || []).length} source(s)).`;
        renderAllChatTurns();
        renderChatSourceCards(t);
        if (res.data?.tts_available && res.data?.audio_b64) {
          playBase64WavAudio(res.data.audio_b64);
        } else if (res.data?.tts_warning) {
          showToast(res.data.tts_warning, "warn");
        }
      } catch (err) {
        const errMsg = err?.message || String(err);
        chatBannerEl.textContent = `Voice turn failed: ${errMsg}`;
        showToast(`Voice turn error: ${errMsg}`, "danger");
      } finally {
        if (activeMediaStream) {
          try {
            for (const tr of activeMediaStream.getTracks()) tr.stop();
          } catch {}
          activeMediaStream = null;
        }
        activeRecordedChunks = [];
        if (micBtn) micBtn.textContent = "Mic";
      }
    });
    activeMediaRecorder.start();
    if (micBtn) micBtn.textContent = "Stop Mic";
    showToast("Listening... click Stop Mic to send voice query.", "ok");
  } catch (err) {
    if (micBtn) micBtn.textContent = "Mic";
    showToast(`Microphone error: ${err?.message || err}`, "danger");
  }
});

const chatPinTurnBtnEl = document.getElementById("chatPinTurnBtn");
if (chatPinTurnBtnEl) {
  chatPinTurnBtnEl.addEventListener("click", async () => {
    const tid = chatSession.getThreadId() || document.getElementById("chatThreadSelect")?.value || "";
    if (!tid) {
      const sel = chatSession.getSelectedTurn();
      if (!sel || !sel.assistantText) {
        showToast("Ask a question first to pin an answer to Working Notes.", "warn");
        return;
      }
      const nRes = await window.secondBrain.request("POST", "/v1/notes", {
        title: `Pinned Answer: ${sel.query.slice(0, 60)}`,
        body: `## Question\n${sel.query}\n\n## Answer\n${sel.assistantText}\n`,
        tags: ["pinned-chat"],
      });
      if (nRes.ok) {
        showToast(`Pinned answer to note ${nRes.data?.filename || ""}`, "ok");
        await loadNotes({ append: false });
      } else {
        showToast(apiErrorMessage(nRes, "Pin to note failed"), "danger");
      }
      return;
    }
    const sel = chatSession.getSelectedTurn();
    const payload = sel && sel.dbTurnId ? { turn_id: sel.dbTurnId } : {};
    const res = await window.secondBrain.request(
      "POST",
      `/v1/chat/threads/${encodeURIComponent(tid)}/pin`,
      payload
    );
    if (res.ok) {
      showToast(`Pinned turn to Working Note: ${res.data?.filename || ""}`, "ok");
      await loadNotes({ append: false });
    } else {
      showToast(apiErrorMessage(res, "Pin turn failed"), "danger");
    }
  });
}

const chatDeleteThreadBtnEl = document.getElementById("chatDeleteThreadBtn");
if (chatDeleteThreadBtnEl) {
  chatDeleteThreadBtnEl.addEventListener("click", async () => {
    const tid = chatSession.getThreadId() || document.getElementById("chatThreadSelect")?.value || "";
    if (!tid) {
      showToast("Select a saved chat thread to delete.", "warn");
      return;
    }
    const res = await window.secondBrain.request(
      "DELETE",
      `/v1/chat/threads/${encodeURIComponent(tid)}`,
      {}
    );
    if (res.ok) {
      chatSession.clearHistory();
      chatSession.setThreadId("");
      await loadChatThreads();
      renderAllChatTurns();
      chatSourcesEl.replaceChildren();
      showToast(`Deleted thread ${tid}`, "ok");
    } else {
      showToast(apiErrorMessage(res, "Delete thread failed"), "danger");
    }
  });
}
document
  .getElementById("chatExportMdBtn")
  .addEventListener("click", async () => {
    const md = chatSession.exportMarkdown();
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(md);
      }
    } catch {}
    try {
      const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = el("a");
      a.href = url;
      a.download = `second-brain-chat-${new Date()
        .toISOString()
        .slice(0, 19)
        .replace(/[:T]/g, "-")}.md`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    } catch {}
    showToast("Exported multi-turn chat session Markdown", "ok");
  });

document.getElementById("chatWarmupBtn").addEventListener("click", async () => {
  chatBannerEl.textContent = "Warming up local Ollama model...";
  try {
    const res = await window.secondBrain.request("POST", "/v1/chat/warmup", {});
    if (res.ok) {
      const modelName = res.data?.model || "local model";
      const ms =
        res.data?.warmup_ms !== undefined ? ` in ${res.data.warmup_ms} ms` : "";
      const msg = `Model warmed: ${modelName}${ms}`;
      chatBannerEl.textContent = msg;
      showToast(msg, "ok");
    } else {
      const errMsg = apiErrorMessage(res, "Model warmup failed");
      chatBannerEl.textContent = errMsg;
      showToast(errMsg, "warn");
    }
  } catch (err) {
    const errMsg = `Warmup failed: ${err?.message || String(err)}`;
    chatBannerEl.textContent = errMsg;
    showToast(errMsg, "danger");
  }
});

// ─── TAB 3: Quarantined Inbox, Atom Review, Pagination & CAS Merge ───────────

function buildInboxCard(item) {
  const card = el("div", "card");
  const top = el("div", "toolbar");
  top.appendChild(el("strong", "", item.filename || "Inbox Item"));
  const risks = Array.isArray(item.risk_flags) ? item.risk_flags : [];
  if (risks.length > 0) {
    for (const rf of risks) {
      top.appendChild(el("span", "badge danger", rf));
    }
  } else {
    top.appendChild(el("span", "badge ok", "0 risk flags"));
  }
  card.appendChild(top);

  const metaParts = [
    item.origin ? `origin: ${item.origin}` : "",
    item.size_bytes !== undefined ? `${item.size_bytes} B` : "",
    item.sha256 ? `sha:${shortSha(item.sha256)}` : "",
  ].filter(Boolean);
  card.appendChild(el("div", "note-item-meta", metaParts.join(" · ")));

  if (item.preview) {
    card.appendChild(el("div", "sub-panel", item.preview));
  }

  const inspectDrawer = el("div", "sub-panel");
  inspectDrawer.style.display = "none";
  card.appendChild(inspectDrawer);

  let ackCheck = null;
  if (risks.length > 0) {
    const ackLabel = el("label", "inline-check");
    ackCheck = document.createElement("input");
    ackCheck.type = "checkbox";
    ackLabel.appendChild(ackCheck);
    ackLabel.appendChild(
      el(
        "span",
        "",
        "I inspected the raw payload and acknowledge quarantine risk flags"
      )
    );
    card.appendChild(ackLabel);
  }

  const actions = el("div", "toolbar");
  const inspectBtn = el("button", "btn sm", "Inspect");
  inspectBtn.type = "button";
  inspectBtn.onclick = async () => {
    if (inspectDrawer.style.display === "block") {
      inspectDrawer.style.display = "none";
      inspectBtn.textContent = "Inspect";
      return;
    }
    inspectBtn.disabled = true;
    inspectBtn.textContent = "Loading...";
    try {
      const res = await window.secondBrain.api(
        "GET",
        `/v1/inbox/${encodeURIComponent(item.filename)}`
      );
      inspectDrawer.replaceChildren();
      const doc = res.ok && res.data ? res.data : item;
      const metaRow = el("div", "toolbar");
      metaRow.appendChild(
        el("span", "badge", `origin: ${doc.origin || item.origin || "untrusted"}`)
      );
      metaRow.appendChild(
        el("span", "badge mono", `sha256: ${doc.sha256 || item.sha256 || ""}`)
      );
      const docRisks = Array.isArray(doc.risk_flags) ? doc.risk_flags : risks;
      if (docRisks.length > 0) {
        for (const rf of docRisks) {
          metaRow.appendChild(el("span", "badge danger", rf));
        }
      } else {
        metaRow.appendChild(el("span", "badge ok", "0 risk flags"));
      }
      inspectDrawer.appendChild(metaRow);

      const rawPre = el(
        "pre",
        "mono-box",
        res.ok && res.data
          ? res.data.content || res.data.preview || "(empty payload)"
          : item.preview ||
              apiErrorMessage(res, "Preview only (full inspect unavailable)")
      );
      rawPre.style.marginTop = "6px";
      inspectDrawer.appendChild(rawPre);
      inspectDrawer.style.display = "block";
      inspectBtn.textContent = "Hide Raw";
    } catch (err) {
      showToast(`Inspect failed: ${err?.message || String(err)}`, "danger");
      inspectBtn.textContent = "Inspect";
    } finally {
      inspectBtn.disabled = false;
    }
  };

  const promoteBtn = el("button", "btn sm good", "Promote");
  promoteBtn.type = "button";
  if (ackCheck) {
    promoteBtn.disabled = true;
    ackCheck.addEventListener("change", () => {
      promoteBtn.disabled = !ackCheck.checked;
    });
  }
  promoteBtn.onclick = async () => {
    if (risks.length > 0 && (!ackCheck || !ackCheck.checked)) {
      showToast(
        "Inspect the raw payload and check the quarantine risk acknowledgment box first.",
        "warn"
      );
      return;
    }
    promoteBtn.disabled = true;
    try {
      const res = await window.secondBrain.request("POST", "/v1/inbox/promote", {
        filename: item.filename,
        expected_sha256: item.sha256,
        actor: "desktop_owner",
        acknowledge_risks: Boolean(ackCheck?.checked),
      });
      if (res.ok) {
        showToast(`Promoted ${item.filename}`, "ok");
        loadReviewQueue();
      } else {
        showToast(apiErrorMessage(res, "Inbox promote failed"), "danger");
        promoteBtn.disabled = false;
      }
    } catch (err) {
      showToast(`Promote error: ${err?.message || String(err)}`, "danger");
      promoteBtn.disabled = false;
    }
  };

  const rejectBtn = el("button", "btn sm danger", "Reject");
  rejectBtn.type = "button";
  rejectBtn.onclick = async () => {
    rejectBtn.disabled = true;
    try {
      const res = await window.secondBrain.request("POST", "/v1/inbox/reject", {
        filename: item.filename,
        expected_sha256: item.sha256,
        actor: "desktop_owner",
      });
      if (res.ok) {
        showToast(`Rejected ${item.filename}`, "ok");
        loadReviewQueue();
      } else {
        showToast(apiErrorMessage(res, "Inbox reject failed"), "danger");
        rejectBtn.disabled = false;
      }
    } catch (err) {
      showToast(`Reject error: ${err?.message || String(err)}`, "danger");
      rejectBtn.disabled = false;
    }
  };

  actions.appendChild(inspectBtn);
  actions.appendChild(promoteBtn);
  actions.appendChild(rejectBtn);
  card.appendChild(actions);
  return card;
}

function buildAtomReviewCard(atom) {
  const card = el("div", "card");
  const top = el("div", "toolbar");
  top.appendChild(el("strong", "", atom.filename));
  const status = atom.review_status || atom.status || "pending";
  top.appendChild(
    el("span", `badge ${status === "needs_edit" ? "danger" : "warn"}`, status)
  );
  if (atom.memory_type) {
    top.appendChild(el("span", "badge", atom.memory_type));
  }
  if (atom.sha256) {
    top.appendChild(el("span", "badge mono", shortSha(atom.sha256)));
  }
  appendTemporalValidityBadges(top, atom);
  card.appendChild(top);

  card.appendChild(el("div", "", atom.claim || atom.title || "(no claim)"));
  if (atom.evidence) {
    card.appendChild(el("div", "mono-box", atom.evidence));
  }
  const srcList = Array.isArray(atom.sources) ? atom.sources.join(", ") : "";
  card.appendChild(
    el(
      "div",
      "note-item-meta",
      `Origin: ${atom.origin || "unknown"}${
        srcList ? " · Sources: " + srcList : ""
      }`
    )
  );

  const row = el("div", "toolbar");
  const transitions = [
    ["approve", "good", "Approve"],
    ["quarantine", "warn", "Quarantine"],
    ["demote", "", "Demote"],
    ["reject", "danger", "Reject"],
  ];
  for (const [act, cls, label] of transitions) {
    const btn = el("button", `btn sm ${cls}`.trim(), label);
    btn.type = "button";
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const res = await window.secondBrain.request(
          "POST",
          `/v1/atoms/${encodeURIComponent(atom.filename)}/${act}`,
          { expected_sha256: atom.sha256, actor: "desktop_owner" }
        );
        if (res.ok) {
          showToast(`${label}: ${atom.filename}`, "ok");
          loadReviewQueue();
          refreshHealth();
        } else {
          showToast(apiErrorMessage(res, `${label} failed`), "danger");
          btn.disabled = false;
        }
      } catch (err) {
        showToast(`${label} error: ${err?.message || String(err)}`, "danger");
        btn.disabled = false;
      }
    };
    row.appendChild(btn);
  }
  const editBtn = el("button", "btn sm", "Edit Atom");
  editBtn.type = "button";
  editBtn.onclick = () => openAtomEditorModal(atom);
  row.appendChild(editBtn);

  card.appendChild(row);
  return card;
}

function buildApprovedAtomCard(atom) {
  const card = el("div", "card");
  const top = el("div", "toolbar");
  top.appendChild(el("strong", "", atom.filename));
  top.appendChild(el("span", "badge ok", "accepted"));
  if (atom.memory_type) top.appendChild(el("span", "badge", atom.memory_type));
  top.appendChild(el("span", "badge mono", shortSha(atom.sha256)));
  appendTemporalValidityBadges(top, atom);
  card.appendChild(top);
  card.appendChild(el("div", "", atom.claim || ""));

  const govRow = el("div", "toolbar");
  const inspectBtn = el("button", "btn sm", "Inspect Provenance");
  inspectBtn.type = "button";
  inspectBtn.onclick = () => openAtomInspectorModal(atom.filename, atom);
  govRow.appendChild(inspectBtn);

  const editBtn = el("button", "btn sm", "Edit Claim");
  editBtn.type = "button";
  editBtn.onclick = () => openAtomEditorModal(atom);
  govRow.appendChild(editBtn);

  for (const [act, cls, label] of [
    ["quarantine", "warn", "Quarantine"],
    ["demote", "", "Demote"],
  ]) {
    const tBtn = el("button", `btn sm ${cls}`.trim(), label);
    tBtn.type = "button";
    tBtn.onclick = async () => {
      tBtn.disabled = true;
      try {
        const res = await window.secondBrain.request(
          "POST",
          `/v1/atoms/${encodeURIComponent(atom.filename)}/${act}`,
          { expected_sha256: atom.sha256, actor: "desktop_owner" }
        );
        if (res.ok) {
          showToast(`${label} applied to ${atom.filename}`, "ok");
          loadReviewQueue();
          refreshHealth();
        } else {
          showToast(apiErrorMessage(res, `${label} failed`), "danger");
          tBtn.disabled = false;
        }
      } catch (err) {
        showToast(`${label} error: ${err?.message || String(err)}`, "danger");
        tBtn.disabled = false;
      }
    };
    govRow.appendChild(tBtn);
  }
  card.appendChild(govRow);

  const fbRow = el("div", "toolbar");
  const noteInput = el("input");
  noteInput.type = "text";
  noteInput.placeholder = "Feedback note (optional)...";
  noteInput.style.flex = "1";
  noteInput.style.minWidth = "120px";
  fbRow.appendChild(noteInput);

  for (const [fb, cls, label] of [
    ["endorsed", "good", "Endorse"],
    ["corrected", "warn", "Corrected"],
    ["superseded", "", "Superseded"],
  ]) {
    const btn = el("button", `btn sm ${cls}`.trim(), label);
    btn.type = "button";
    btn.onclick = async () => {
      const res = await window.secondBrain.api(
        "POST",
        "/v1/cognitive/feedback",
        {
          atom_filename: atom.filename,
          feedback: fb,
          actor: "desktop_owner",
          note: noteInput.value.trim(),
        }
      );
      if (res.ok) {
        noteInput.value = "";
        showToast(`Recorded '${fb}' feedback on ${atom.filename}`, "ok");
      } else {
        showToast(apiErrorMessage(res, "Feedback failed"), "danger");
      }
    };
    fbRow.appendChild(btn);
  }

  card.appendChild(fbRow);
  return card;
}

function applyReviewSectionFilter(filterMode) {
  document.querySelectorAll("[data-review-filter]").forEach((btn) => {
    btn.classList.toggle("primary", btn.dataset.reviewFilter === filterMode);
  });
  const showInbox = filterMode === "all" || filterMode === "inbox";
  const showPending = filterMode === "all" || filterMode === "pending";
  const showApproved = filterMode === "all" || filterMode === "approved";

  const setDisp = (id, show) => {
    const node = document.getElementById(id);
    if (node) node.style.display = show ? "" : "none";
  };

  setDisp("reviewHeaderInbox", showInbox);
  setDisp("inboxQueueGrid", showInbox);
  setDisp("reviewHeaderPending", showPending);
  setDisp("reviewGrid", showPending);
  if (!showPending) setDisp("reviewPaginationBar", false);
  setDisp("reviewHeaderApproved", showApproved);
  setDisp("reviewMergeCard", showApproved);
  setDisp("approvedAtomsGrid", showApproved);
  if (!showApproved) setDisp("atomsPaginationBar", false);
}

document.querySelectorAll("[data-review-filter]").forEach((btn) => {
  btn.addEventListener("click", () => {
    applyReviewSectionFilter(btn.dataset.reviewFilter || "all");
  });
});

async function loadReviewPendingPage({ append = false } = {}) {
  const reviewGrid = document.getElementById("reviewGrid");
  if (!append) {
    reviewQueuePaginator.reset();
    reviewGrid.replaceChildren(
      el("div", "status-banner", "Loading review queue...")
    );
  }
  const url = PageHelper.buildPaginatedPath("/v1/review/queue", {
    limit: reviewQueuePaginator.getPageSize(),
    offset: reviewQueuePaginator.nextOffset(),
  });
  const reviewRes = await window.secondBrain.request("GET", url);
  reviewQueuePaginator.applyPage(reviewRes.data || {}, {
    append,
    preferredKey: "items",
  });

  reviewGrid.replaceChildren();
  const reviewItems = reviewQueuePaginator.getItems();
  if (!reviewItems.length) {
    reviewGrid.appendChild(
      el(
        "div",
        "status-banner",
        "Pending & quarantined atom review queue is clear."
      )
    );
  } else {
    for (const atom of reviewItems) {
      reviewGrid.appendChild(buildAtomReviewCard(atom));
    }
  }
  PageHelper.renderPaginationBar(
    document.getElementById("reviewPaginationBar"),
    reviewQueuePaginator,
    () => loadReviewPendingPage({ append: true }),
    { noun: "pending atoms" }
  );
}

function syncMergeSelectorsFromAtoms() {
  const survSel = document.getElementById("mergeSurvivorSelect");
  const supSel = document.getElementById("mergeSupersededSelect");
  const prevSurv = survSel.value;
  const prevSup = supSel.value;
  survSel.replaceChildren();
  supSel.replaceChildren();

  for (const a of allLoadedAtoms) {
    for (const sel of [survSel, supSel]) {
      const opt = el("option", "", `${a.filename} (${shortSha(a.sha256)})`);
      opt.value = a.filename;
      opt.dataset.sha = a.sha256 || "";
      sel.appendChild(opt);
    }
  }
  if (prevSurv && [...survSel.options].some((o) => o.value === prevSurv)) {
    survSel.value = prevSurv;
  }
  if (prevSup && [...supSel.options].some((o) => o.value === prevSup)) {
    supSel.value = prevSup;
  } else if (supSel.options.length > 1) {
    supSel.selectedIndex = 1;
  }

  const moreBtn = document.getElementById("loadMoreMergeAtomsBtn");
  if (moreBtn) {
    moreBtn.style.display = atomsPaginator.canLoadMore()
      ? "inline-block"
      : "none";
  }
}

async function loadAtomsPage({ append = false } = {}) {
  const approvedGrid = document.getElementById("approvedAtomsGrid");
  if (!append) {
    atomsPaginator.reset();
  }
  const url = PageHelper.buildPaginatedPath("/v1/atoms", {
    limit: atomsPaginator.getPageSize(),
    offset: atomsPaginator.nextOffset(),
  });
  const atomsRes = await window.secondBrain.request("GET", url);
  atomsPaginator.applyPage(atomsRes.data || {}, {
    append,
    preferredKey: "items",
  });

  allLoadedAtoms = atomsPaginator.getItems();
  approvedAtomsCache = allLoadedAtoms.filter(
    (a) => a.verified || a.review_status === "accepted"
  );

  syncMergeSelectorsFromAtoms();

  approvedGrid.replaceChildren();
  if (!approvedAtomsCache.length) {
    approvedGrid.appendChild(
      el("div", "status-banner", "No approved atoms yet.")
    );
  } else {
    for (const atom of approvedAtomsCache) {
      approvedGrid.appendChild(buildApprovedAtomCard(atom));
    }
  }
  PageHelper.renderPaginationBar(
    document.getElementById("atomsPaginationBar"),
    atomsPaginator,
    () => loadAtomsPage({ append: true }),
    { noun: "atoms" }
  );
}

async function loadReviewQueue() {
  const inboxGrid = document.getElementById("inboxQueueGrid");
  const dupListEl = document.getElementById("duplicatesList");
  inboxGrid.replaceChildren(el("div", "status-banner", "Loading inbox..."));

  const [inboxRes, dupRes] = await Promise.all([
    window.secondBrain.request("GET", "/v1/inbox/queue"),
    window.secondBrain.request("GET", "/v1/atoms/duplicates"),
    loadReviewPendingPage({ append: false }),
    loadAtomsPage({ append: false }),
  ]);

  inboxGrid.replaceChildren();
  const inboxItems = inboxRes.data?.items || [];
  if (!inboxItems.length) {
    inboxGrid.appendChild(
      el("div", "status-banner", "Quarantined inbox is empty.")
    );
  } else {
    for (const item of inboxItems) {
      inboxGrid.appendChild(buildInboxCard(item));
    }
  }

  const survSel = document.getElementById("mergeSurvivorSelect");
  const supSel = document.getElementById("mergeSupersededSelect");
  dupListEl.replaceChildren();
  const duplicates = dupRes.ok ? dupRes.data?.duplicates || [] : [];
  if (!duplicates.length) {
    dupListEl.textContent =
      "No duplicate atom pairs detected across approved memory.";
  } else {
    for (const dup of duplicates) {
      const aFn = dup.atom_a || dup.filename_a || dup[0] || "";
      const bFn = dup.atom_b || dup.filename_b || dup[1] || "";
      const row = el("div", "toolbar");
      row.style.marginBottom = "6px";
      row.appendChild(
        el(
          "span",
          "",
          `${aFn}  <->  ${bFn}${
            dup.similarity !== undefined
              ? ` (similarity: ${dup.similarity})`
              : ""
          }`
        )
      );
      const loadBtn = el("button", "btn sm", "Load into Merge");
      loadBtn.type = "button";
      loadBtn.onclick = () => {
        survSel.value = aFn;
        supSel.value = bFn;
        document.getElementById("mergeRationaleInput").focus();
      };
      row.appendChild(loadBtn);
      dupListEl.appendChild(row);
    }
  }
}

document
  .getElementById("refreshReviewBtn")
  .addEventListener("click", loadReviewQueue);
document
  .getElementById("loadMoreMergeAtomsBtn")
  .addEventListener("click", () => loadAtomsPage({ append: true }));
document.getElementById("runMergeBtn").addEventListener("click", async () => {
  const survSel = document.getElementById("mergeSurvivorSelect");
  const supSel = document.getElementById("mergeSupersededSelect");
  const survivor_filename = survSel.value;
  const superseded_filename = supSel.value;
  const rationale = document.getElementById("mergeRationaleInput").value.trim();

  if (!survivor_filename || !superseded_filename) {
    showToast("Select two atoms to merge.", "warn");
    return;
  }
  if (survivor_filename === superseded_filename) {
    showToast("Survivor and superseded atoms must be different.", "warn");
    return;
  }
  if (!rationale) {
    showToast("Enter a merge rationale for the immutable audit log.", "warn");
    return;
  }
  const survivor_expected_sha256 =
    survSel.selectedOptions[0]?.dataset.sha || "";
  const superseded_expected_sha256 =
    supSel.selectedOptions[0]?.dataset.sha || "";

  const res = await window.secondBrain.api("POST", "/v1/atoms/merge", {
    survivor_filename,
    survivor_expected_sha256,
    superseded_filename,
    superseded_expected_sha256,
    actor: "desktop_owner",
    rationale,
  });
  if (res.ok) {
    document.getElementById("mergeRationaleInput").value = "";
    showToast(`Merged ${superseded_filename} into ${survivor_filename}`, "ok");
    loadReviewQueue();
    refreshHealth();
  } else {
    showToast(apiErrorMessage(res, "Atom merge failed"), "danger");
  }
});

// ─── TAB 4: Bulk Import, Single Document Import, Web Scout & Operator Jobs ───

const importDryRunCheck = document.getElementById("importDryRunCheck");
const runImportBtn = document.getElementById("runImportBtn");

importDryRunCheck.addEventListener("change", () => {
  runImportBtn.textContent = importDryRunCheck.checked
    ? "Preview Import (Dry Run)"
    : "Import Notes";
});

document
  .getElementById("pickImportDirBtn")
  .addEventListener("click", async () => {
    const dir = await window.secondBrain.pickFolder();
    if (dir) document.getElementById("importDirInput").value = dir;
  });

runImportBtn.addEventListener("click", async () => {
  const source_dir = document.getElementById("importDirInput").value.trim();
  if (!source_dir) {
    showToast("Choose a source folder containing documents or notes.", "warn");
    return;
  }
  const tags = parseTags(document.getElementById("importTagsInput").value);
  const dry_run = Boolean(importDryRunCheck.checked);
  const overwrite_modified = Boolean(
    document.getElementById("importOverwriteCheck").checked
  );
  const resEl = document.getElementById("importResult");
  const kpiGrid = document.getElementById("importKpiGrid");

  runImportBtn.disabled = true;
  resEl.className = "status-banner";
  resEl.textContent = dry_run
    ? "Scanning folder (Dry Run preview)..."
    : "Importing notes into vault...";
  kpiGrid.replaceChildren();

  try {
    const res = await window.secondBrain.request("POST", "/v1/import", {
      source_dir,
      tags,
      dry_run,
      overwrite_modified,
    });
    if (res.ok && res.data) {
      const d = res.data;
      const importedCount = d.imported ?? d.created ?? 0;
      const skippedCount =
        d.skipped ??
        Number(d.skipped_too_large || 0) + Number(d.skipped_unreadable || 0);
      const isDry = d.dry_run !== undefined ? Boolean(d.dry_run) : dry_run;

      resEl.className = `status-banner ${isDry ? "banner-warn" : "banner-ok"}`;
      resEl.textContent = isDry
        ? `Dry Run Complete — no vault files modified.${
            d.notes_dir ? " Target: " + d.notes_dir : ""
          }`
        : `Import Complete${
            d.elapsed_s !== undefined ? ` in ${d.elapsed_s}s` : ""
          }.${d.notes_dir ? " Target: " + d.notes_dir : ""}`;

      const errorList = Array.isArray(d.errors) ? d.errors : [];
      const errorCount =
        d.error_count ??
        (errorList.length || Number(d.skipped_unreadable || 0));

      kpiGrid.appendChild(makeKpiCard("Scanned", d.scanned ?? 0));
      kpiGrid.appendChild(
        makeKpiCard(isDry ? "Would Import" : "Imported", importedCount, "", "ok")
      );
      kpiGrid.appendChild(
        makeKpiCard(isDry ? "Would Update" : "Updated", d.updated ?? 0)
      );
      kpiGrid.appendChild(makeKpiCard("Unchanged", d.unchanged ?? 0));
      kpiGrid.appendChild(makeKpiCard("Skipped", skippedCount));
      kpiGrid.appendChild(
        makeKpiCard(
          "Errors",
          errorCount,
          "",
          errorCount > 0 ? "danger" : "ok"
        )
      );
      kpiGrid.appendChild(
        makeKpiCard("Conflicts Preserved", d.conflicts_preserved ?? 0)
      );
      if (errorList.length) {
        const errBox = el("div", "sub-panel");
        errBox.style.gridColumn = "1 / -1";
        errBox.appendChild(
          el("strong", "", `Import Errors (${errorList.length}):`)
        );
        for (const itemErr of errorList.slice(0, 12)) {
          errBox.appendChild(
            el(
              "div",
              "note-item-meta",
              typeof itemErr === "string"
                ? itemErr
                : `${itemErr.file || itemErr.path || "file"}: ${
                    itemErr.error || ""
                  }`
            )
          );
        }
        kpiGrid.appendChild(errBox);
      }

      showToast(
        isDry
          ? `Dry Run scanned ${d.scanned ?? 0} file(s)`
          : `Imported ${importedCount} note(s), updated ${d.updated ?? 0}`,
        "ok"
      );
      if (!isDry) {
        loadNotes({ append: false });
        refreshHealth();
      }
    } else {
      const errMsg = apiErrorMessage(res, "Import failed");
      resEl.className = "status-banner banner-danger";
      resEl.textContent = errMsg;
      showToast(errMsg, "danger");
    }
  } catch (err) {
    const errMsg = `Import error: ${err?.message || String(err)}`;
    resEl.className = "status-banner banner-danger";
    resEl.textContent = errMsg;
    showToast(errMsg, "danger");
  } finally {
    runImportBtn.disabled = false;
  }
});

// Single Document Picker & Import
document
  .getElementById("pickImportFileBtn")
  .addEventListener("click", async () => {
    if (
      window.secondBrain &&
      typeof window.secondBrain.pickFile === "function"
    ) {
      const filePath = await window.secondBrain.pickFile();
      if (filePath) document.getElementById("importFileInput").value = filePath;
    }
  });

document
  .getElementById("runImportFileBtn")
  .addEventListener("click", async () => {
    const filePath = document.getElementById("importFileInput").value.trim();
    const resEl = document.getElementById("importFileResult");
    const btn = document.getElementById("runImportFileBtn");
    if (!filePath) {
      showToast("Choose a document file to import.", "warn");
      return;
    }
    const tags = parseTags(document.getElementById("importFileTagsInput").value);
    btn.disabled = true;
    resEl.className = "status-banner";
    resEl.textContent = `Importing ${filePath}...`;

    try {
      const res = await window.secondBrain.request("POST", "/v1/import/file", {
        path: filePath,
        tags,
      });
      if (res.ok && res.data) {
        const fn =
          res.data.filename ||
          res.data.note_filename ||
          res.data.note?.filename ||
          filePath;
        resEl.className = "status-banner banner-ok";
        resEl.textContent = `Imported document -> ${fn}`;
        showToast(`Imported document: ${fn}`, "ok");
        loadNotes({ append: false });
        refreshHealth();
      } else {
        const errMsg = apiErrorMessage(res, "Document import failed");
        resEl.className = "status-banner banner-danger";
        resEl.textContent = errMsg;
        showToast(errMsg, "danger");
      }
    } catch (err) {
      const errMsg = `Document import error: ${err?.message || String(err)}`;
      resEl.className = "status-banner banner-danger";
      resEl.textContent = errMsg;
      showToast(errMsg, "danger");
    } finally {
      btn.disabled = false;
    }
  });

// Web Scout Queue & Operator Jobs
async function loadScoutQueue() {
  const listEl = document.getElementById("scoutQueueList");
  if (!listEl) return;
  const res = await window.secondBrain.request("GET", "/v1/scout/queue");
  listEl.replaceChildren();
  if (!res.ok) {
    listEl.textContent = apiErrorMessage(res, "Web Scout queue unavailable");
    return;
  }
  const items = res.data?.items || [];
  if (!items.length) {
    listEl.textContent =
      "Reading queue is empty (0-raw/reading-list.md). Add an http/https URL above.";
    return;
  }
  for (const item of items) {
    const row = el("div", "toolbar");
    row.style.marginBottom = "4px";
    const statusStr = String(item.status || "queued").toLowerCase();
    row.appendChild(
      el(
        "span",
        `badge ${statusStr === "done" || statusStr === "completed" ? "ok" : "warn"}`,
        statusStr
      )
    );
    const linkBtn = el("button", "pill clickable", item.url || "");
    linkBtn.type = "button";
    linkBtn.onclick = () => openExternalSafe(item.url);
    row.appendChild(linkBtn);
    listEl.appendChild(row);
  }
}

document
  .getElementById("refreshScoutBtn")
  .addEventListener("click", () => {
    loadScoutQueue();
    loadCreativeMediaStudio();
  });

document
  .getElementById("addScoutUrlBtn")
  .addEventListener("click", async () => {
    const input = document.getElementById("scoutUrlInput");
    const rawUrl = input.value.trim();
    const safeUrl = MdHelper.sanitizeExternalUrl(rawUrl);
    if (!safeUrl || (!safeUrl.startsWith("http://") && !safeUrl.startsWith("https://"))) {
      showToast("Enter a valid http:// or https:// URL for Web Scout.", "warn");
      return;
    }
    const res = await window.secondBrain.request("POST", "/v1/scout/queue", {
      url: safeUrl,
    });
    if (res.ok) {
      input.value = "";
      showToast("Added URL to Web Scout reading queue", "ok");
      loadScoutQueue();
    } else {
      showToast(apiErrorMessage(res, "Failed to queue URL"), "danger");
    }
  });

function renderActiveJobs() {
  const wrap = document.getElementById("activeJobsList");
  if (!wrap) return;
  wrap.replaceChildren();
  if (!activeJobsMap.size) {
    wrap.textContent = "No operator jobs triggered in this session yet.";
    return;
  }
  for (const job of [...activeJobsMap.values()].reverse()) {
    const card = el("div", "step-card");
    card.style.marginBottom = "8px";
    const top = el("div", "toolbar");
    top.appendChild(el("strong", "", `${job.kind || "job"} (${job.job_id})`));
    const st = String(job.status || "running").toLowerCase();
    const cls =
      st === "completed"
        ? "ok"
        : st === "failed"
        ? "danger"
        : "warn";
    top.appendChild(el("span", `badge ${cls}`, st.toUpperCase()));
    if (job.returncode !== undefined && job.returncode !== null) {
      top.appendChild(el("span", "badge mono", `exit:${job.returncode}`));
    }
    card.appendChild(top);
    if (job.output_tail) {
      card.appendChild(el("pre", "mono-box", job.output_tail));
    }
    wrap.appendChild(card);
  }
}

function scheduleJobPoll() {
  if (jobPollTimer) return;
  jobPollTimer = setInterval(async () => {
    const runningIds = [...activeJobsMap.values()]
      .filter((j) => j.status === "running" || j.status === "started")
      .map((j) => j.job_id);
    if (!runningIds.length) {
      clearInterval(jobPollTimer);
      jobPollTimer = null;
      return;
    }
    for (const jid of runningIds) {
      try {
        const res = await window.secondBrain.request(
          "GET",
          `/v1/jobs/${encodeURIComponent(jid)}`
        );
        if (res.ok && res.data) {
          const prev = activeJobsMap.get(jid) || {};
          const updated = { ...prev, ...res.data, job_id: jid };
          activeJobsMap.set(jid, updated);
          if (updated.status === "completed") {
            showToast(`Job ${updated.kind || jid} completed`, "ok");
            refreshHealth();
            if (activeView === "import") loadScoutQueue();
          } else if (updated.status === "failed") {
            showToast(`Job ${updated.kind || jid} failed`, "danger");
          }
        }
      } catch {}
    }
    renderActiveJobs();
  }, 1500);
}

function registerTriggeredJob(data, fallbackKind) {
  const jid = data?.job_id || `${fallbackKind}-${Date.now()}`;
  activeJobsMap.set(jid, {
    job_id: jid,
    kind: data?.kind || fallbackKind,
    status: data?.status || "started",
    returncode: data?.returncode,
    output_tail: data?.output_tail || data?.output || "",
  });
  renderActiveJobs();
  if (
    data?.job_id &&
    (data.status === "started" || data.status === "running")
  ) {
    scheduleJobPoll();
  }
}

document.getElementById("runScoutBtn").addEventListener("click", async () => {
  const rawMax = Number(document.getElementById("scoutMaxItemsInput").value || 5);
  const max_items = Math.max(1, Math.min(20, Number.isFinite(rawMax) ? rawMax : 5));
  const btn = document.getElementById("runScoutBtn");
  btn.disabled = true;
  try {
    const res = await window.secondBrain.request("POST", "/v1/scout/run", {
      max_items,
    });
    if (res.ok && res.data) {
      registerTriggeredJob(res.data, "scout");
      showToast("Triggered Web Scout crawl job", "ok");
      loadScoutQueue();
    } else {
      showToast(apiErrorMessage(res, "Web Scout trigger failed"), "danger");
    }
  } finally {
    btn.disabled = false;
  }
});

for (const [btnId, jobEndpoint, jobKind] of [
  ["runRefineryJobBtn", "/v1/jobs/refinery", "refinery"],
  ["runEditorJobBtn", "/v1/jobs/editor", "editor"],
  ["runDoctorJobBtn", "/v1/jobs/doctor", "doctor"],
]) {
  document.getElementById(btnId).addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      const res = await window.secondBrain.request("POST", jobEndpoint, {});
      if (res.ok && res.data) {
        registerTriggeredJob(res.data, jobKind);
        showToast(`Started ${jobKind} job`, "ok");
      } else {
        showToast(apiErrorMessage(res, `Failed to start ${jobKind}`), "danger");
      }
    } finally {
      btn.disabled = false;
    }
  });
}

// ─── TAB 5: IDE Hub ──────────────────────────────────────────────────────────

async function loadIdeStatus() {
  const grid = document.getElementById("ideGrid");
  grid.replaceChildren(
    el("div", "status-banner", "Checking IDE & agent host MCP configurations...")
  );
  try {
    const status = await window.secondBrain.ideAction("status");
    grid.replaceChildren();
    const privacyNotice = status.privacy_notice || DEFAULT_PRIVACY_NOTICE;
    const targets = status.targets || status;
    for (const [key, info] of Object.entries(targets)) {
      if (!info || typeof info !== "object") continue;
      const card = el("div", "card");
      const connected = Boolean(info.connected);
      const label = info.name || info.label || key;

      const head = el("div", "toolbar");
      head.appendChild(el("strong", "", label));
      head.appendChild(
        el(
          "span",
          `badge ${connected ? "ok" : info.installed ? "warn" : ""}`,
          connected ? "Connected" : info.installed ? "Available" : "Not installed"
        )
      );
      card.appendChild(head);
      card.appendChild(
        el("div", "note-item-meta", info.config_path || info.path || "")
      );

      const btn = el(
        "button",
        connected ? "btn danger" : "btn primary",
        connected ? "Disconnect" : "1-Click Connect"
      );
      btn.type = "button";
      btn.onclick = async () => {
        if (!connected) {
          const conf = await modalCtrl.confirm({
            title: `Cloud Provider Privacy Notice (${label})`,
            message: privacyNotice,
            detail: `Connect ${label} to your local Second Brain MCP server now?`,
            confirmLabel: `Connect ${label}`,
          });
          if (!conf.confirmed) return;
        } else {
          const conf = await modalCtrl.confirm({
            title: `Disconnect ${label}`,
            message: `Disconnect ${label} from Second Brain MCP?`,
            confirmLabel: "Disconnect",
            confirmClass: "danger",
          });
          if (!conf.confirmed) return;
        }
        btn.disabled = true;
        try {
          await window.secondBrain.ideAction(
            connected ? "remove" : "add",
            key,
            connected ? {} : { confirmPrivacy: true }
          );
          showToast(
            connected
              ? `Disconnected ${label}`
              : `Connected ${label} — restart host to pick up MCP tools.`,
            "ok"
          );
          loadIdeStatus();
        } catch (err) {
          showToast(`IDE action failed: ${err?.message || String(err)}`, "danger");
          btn.disabled = false;
        }
      };
      card.appendChild(btn);
      grid.appendChild(card);
    }
  } catch (err) {
    grid.replaceChildren(
      el(
        "div",
        "status-banner banner-danger",
        `IDE status error: ${err?.message || String(err)}`
      )
    );
  }
}

document.getElementById("refreshIdeBtn").addEventListener("click", () => {
  loadIdeStatus();
  loadGitHookStatus();
});

document.getElementById("addCustomMcpHostBtn").addEventListener("click", async () => {
  const hid = document.getElementById("customMcpHostIdInput").value.trim().toLowerCase();
  const label = document.getElementById("customMcpLabelInput").value.trim() || hid;
  const configPath = document.getElementById("customMcpPathInput").value.trim();
  const serversKey = document.getElementById("customMcpKeyInput").value.trim() || "mcpServers";
  const configFormat = document.getElementById("customMcpFormatSelect").value;
  if (!hid || !configPath) {
    showToast("Host ID and Config File Path are required.", "warn");
    return;
  }
  try {
    await window.secondBrain.ideAction("custom-add", hid, {
      label,
      configPath,
      serversKey,
      configFormat,
    });
    document.getElementById("customMcpHostIdInput").value = "";
    document.getElementById("customMcpLabelInput").value = "";
    document.getElementById("customMcpPathInput").value = "";
    showToast(`Registered custom MCP host: ${hid}`, "ok");
    loadIdeStatus();
  } catch (err) {
    showToast(`Custom MCP registration failed: ${err?.message || String(err)}`, "danger");
  }
});

// ─── TAB 6: System Health, Host Sharing Policies, Fitness & Experiences ──────

function renderSystemHostsTable(hostsObj) {
  const wrap = document.getElementById("systemHostsWrap");
  wrap.replaceChildren();
  const entries = Object.entries(hostsObj || {});
  if (!entries.length) {
    wrap.appendChild(el("div", "status-banner", "No host policies loaded."));
    return;
  }

  populateHostSelectElement(document.getElementById("planHostSelect"), hostsObj);
  populateHostSelectElement(document.getElementById("chatHostSelect"), hostsObj);

  const table = el("table", "data-table");
  const thead = el("thead");
  const htr = el("tr");
  for (const col of [
    "Host",
    "Enabled",
    "Endpoint URL",
    "Default Model",
    "Allowed Projects",
    "Prefs / PII",
    "API Key Env",
    "Action",
  ]) {
    htr.appendChild(el("th", "", col));
  }
  thead.appendChild(htr);
  table.appendChild(thead);

  const tbody = el("tbody");
  for (const [hostId, rule] of entries) {
    const tr = el("tr");
    const hostTd = el("td");
    hostTd.appendChild(el("strong", "", hostId));
    hostTd.appendChild(
      el("div", "note-item-meta", `${rule.provider || ""} · ${rule.transport || ""}`)
    );
    tr.appendChild(hostTd);

    const enabledTd = el("td");
    const enabledChk = document.createElement("input");
    enabledChk.type = "checkbox";
    enabledChk.checked = Boolean(rule.enabled);
    enabledTd.appendChild(enabledChk);
    tr.appendChild(enabledTd);

    const epTd = el("td");
    const epInput = el("input");
    epInput.type = "text";
    epInput.value = rule.endpoint_url || "";
    epInput.style.width = "175px";
    epTd.appendChild(epInput);
    tr.appendChild(epTd);

    const modelTd = el("td");
    const modelInput = el("input");
    modelInput.type = "text";
    modelInput.value = rule.default_model || "";
    modelInput.style.width = "135px";
    modelTd.appendChild(modelInput);
    tr.appendChild(modelTd);

    const projTd = el("td");
    const projInput = el("input");
    projInput.type = "text";
    projInput.value = (rule.allowed_projects || []).join(", ");
    projInput.placeholder = "* or project:alpha";
    projInput.style.width = "135px";
    projTd.appendChild(projInput);
    tr.appendChild(projTd);

    const flagsTd = el("td");
    const unscopedLbl = el("label", "inline-check");
    const unscopedChk = document.createElement("input");
    unscopedChk.type = "checkbox";
    unscopedChk.checked = Boolean(rule.allow_unscoped_atoms);
    unscopedLbl.appendChild(unscopedChk);
    unscopedLbl.appendChild(el("span", "", "Unscoped"));

    const prefLbl = el("label", "inline-check");
    prefLbl.style.marginLeft = "8px";
    const prefChk = document.createElement("input");
    prefChk.type = "checkbox";
    prefChk.checked = Boolean(rule.allow_preference_atoms);
    prefLbl.appendChild(prefChk);
    prefLbl.appendChild(el("span", "", "Prefs"));

    const piiLbl = el("label", "inline-check");
    piiLbl.style.marginLeft = "8px";
    const piiChk = document.createElement("input");
    piiChk.type = "checkbox";
    piiChk.checked = rule.redact_pii !== false;
    piiLbl.appendChild(piiChk);
    piiLbl.appendChild(el("span", "", "Redact PII"));
    flagsTd.appendChild(unscopedLbl);
    flagsTd.appendChild(prefLbl);
    flagsTd.appendChild(piiLbl);
    tr.appendChild(flagsTd);

    const keyTd = el("td");
    const keyInput = el("input");
    keyInput.type = "text";
    keyInput.value = rule.api_key_env || "";
    keyInput.placeholder =
      rule.transport === "local" ? "(local)" : "OPENAI_API_KEY";
    keyInput.style.width = "125px";
    keyTd.appendChild(keyInput);
    tr.appendChild(keyTd);

    const actTd = el("td");
    const saveBtn = el("button", "btn sm primary", "Save");
    saveBtn.type = "button";
    saveBtn.onclick = async () => {
      saveBtn.disabled = true;
      try {
        const body = {
          enabled: Boolean(enabledChk.checked),
          endpoint_url: epInput.value.trim(),
          default_model: modelInput.value.trim(),
          allowed_projects: parseTags(projInput.value),
          allow_unscoped_atoms: Boolean(unscopedChk.checked),
          allow_preference_atoms: Boolean(prefChk.checked),
          redact_pii: Boolean(piiChk.checked),
        };
        if (keyInput.value.trim()) {
          body.api_key_env = keyInput.value.trim();
        }
        const res = await window.secondBrain.api(
          "PUT",
          `/v1/hosts/${encodeURIComponent(hostId)}`,
          body
        );
        if (res.ok) {
          showToast(`Updated host policy: ${hostId}`, "ok");
          loadSystemView();
        } else {
          showToast(apiErrorMessage(res, `Failed to update ${hostId}`), "danger");
        }
      } catch (err) {
        showToast(`Save error: ${err?.message || String(err)}`, "danger");
      } finally {
        saveBtn.disabled = false;
      }
    };
    actTd.appendChild(saveBtn);
    if (rule.is_builtin === false) {
      const delBtn = el("button", "btn sm danger", "Delete");
      delBtn.type = "button";
      delBtn.style.marginLeft = "4px";
      delBtn.onclick = async () => {
        try {
          const res = await window.secondBrain.api(
            "DELETE",
            `/v1/hosts/${encodeURIComponent(hostId)}`
          );
          if (res.ok) {
            showToast(`Deleted custom host ${hostId}`, "ok");
            loadSystemView();
          } else {
            showToast(apiErrorMessage(res, `Failed to delete ${hostId}`), "danger");
          }
        } catch (err) {
          showToast(`Delete error: ${err?.message || String(err)}`, "danger");
        }
      };
      actTd.appendChild(delBtn);
    }
    tr.appendChild(actTd);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
}

document
  .getElementById("addCustomModelHostBtn")
  .addEventListener("click", async () => {
    const hostId = document
      .getElementById("customModelHostIdInput")
      .value.trim()
      .toLowerCase();
    const provider = document.getElementById("customModelProviderSelect").value;
    const transport = document.getElementById("customModelTransportSelect").value;
    const endpointUrl = document
      .getElementById("customModelEndpointInput")
      .value.trim();
    const defaultModel = document
      .getElementById("customModelNameInput")
      .value.trim();
    const apiKeyEnv = document
      .getElementById("customModelApiKeyEnvInput")
      .value.trim();
    if (!hostId || !endpointUrl || !defaultModel) {
      showToast(
        "Host ID, Endpoint URL, and Default Model are required.",
        "warn"
      );
      return;
    }
    try {
      const res = await window.secondBrain.api("POST", "/v1/hosts", {
        host_id: hostId,
        provider,
        transport,
        endpoint_url: endpointUrl,
        default_model: defaultModel,
        api_key_env: apiKeyEnv || null,
        allowed_projects: ["*"],
        enabled: false,
      });
      if (res.ok) {
        document.getElementById("customModelHostIdInput").value = "";
        document.getElementById("customModelEndpointInput").value = "";
        document.getElementById("customModelNameInput").value = "";
        document.getElementById("customModelApiKeyEnvInput").value = "";
        showToast(`Created custom host profile: ${hostId}`, "ok");
        loadSystemView();
      } else {
        showToast(apiErrorMessage(res, "Failed to create custom host"), "danger");
      }
    } catch (err) {
      showToast(`Create host failed: ${err?.message || String(err)}`, "danger");
    }
  });

async function loadSystemView() {
  const healthKpis = document.getElementById("systemHealthKpis");
  const hwCard = document.getElementById("systemHardwareCard");
  const fitWrap = document.getElementById("systemFitnessWrap");

  healthKpis.replaceChildren(el("div", "status-banner", "Loading health..."));

  const [appSt, healthRes, profileRes, hostsRes, fitnessRes] = await Promise.all(
    [
      window.secondBrain.getStatus(),
      window.secondBrain.request("GET", "/v1/health"),
      window.secondBrain.request("GET", "/v1/profile"),
      window.secondBrain.request("GET", "/v1/hosts"),
      window.secondBrain.request("GET", "/v1/cognitive/fitness"),
    ]
  );

  document.getElementById("systemVaultPathText").textContent = `Vault folder: ${
    appSt.vaultDir || "(default)"
  }`;

  healthKpis.replaceChildren();
  const h = healthRes.data || {};
  healthKpis.appendChild(
    makeKpiCard(
      "API Status",
      healthRes.ok ? "ONLINE" : "OFFLINE",
      `v${h.api_version || "1.0"} · :${appSt.port || "?"}`,
      healthRes.ok ? "ok" : "danger"
    )
  );
  healthKpis.appendChild(makeKpiCard("Total Atoms", h.atom_count ?? 0));
  healthKpis.appendChild(
    makeKpiCard("Approved Atoms", h.approved_atom_count ?? 0)
  );
  healthKpis.appendChild(
    makeKpiCard(
      "Lexical FTS5",
      h.lexical_index_current ? "CURRENT" : "SYNC NEEDED",
      "SQLite BM25",
      h.lexical_index_current ? "ok" : "warn"
    )
  );
  healthKpis.appendChild(
    makeKpiCard(
      "Dense Vector",
      h.dense_index_current ? "CURRENT" : "LEXICAL ONLY",
      h.dense_generation_id ? shortSha(h.dense_generation_id) : "optional",
      h.dense_index_current ? "ok" : "warn"
    )
  );
  healthKpis.appendChild(
    makeKpiCard("Hardware Tier", h.hardware_tier || "unknown")
  );

  hwCard.replaceChildren(el("strong", "", "Hardware Tier & Local Ollama Models"));
  const hw = profileRes.data?.hardware || {};
  const hwGrid = el("div", "kpi-grid");
  hwGrid.appendChild(makeKpiCard("Tier", hw.tier_id || h.hardware_tier || "—"));
  hwGrid.appendChild(
    makeKpiCard(
      "CPU / RAM",
      `${hw.cpu_logical_cores ?? hw.cpu_cores ?? hw.cpu_threads ?? "?"} cores`,
      (hw.ram_total_gb ?? hw.total_ram_gib) !== undefined
        ? `${hw.ram_total_gb ?? hw.total_ram_gib} GB RAM`
        : ""
    )
  );
  hwGrid.appendChild(
    makeKpiCard(
      "Accelerator",
      hw.gpu_name || (hw.has_gpu ? "GPU detected" : "CPU-only"),
      (hw.vram_gb ?? hw.vram_gib) ? `${hw.vram_gb ?? hw.vram_gib} GB VRAM` : ""
    )
  );
  hwGrid.appendChild(
    makeKpiCard(
      "Ollama Daemon",
      hw.ollama_reachable ? "REACHABLE" : "OFFLINE",
      hw.recommended_chat_model || hw.recommended_generation_model || "",
      hw.ollama_reachable ? "ok" : "warn"
    )
  );
  hwCard.appendChild(hwGrid);

  const modelsRow = el("div", "backlinks-bar");
  modelsRow.appendChild(el("span", "field-label", "Installed Local Models:"));
  const models = hw.installed_ollama_models || [];
  if (!models.length) {
    modelsRow.appendChild(el("span", "note-item-meta", "none detected"));
  } else {
    for (const m of models) {
      modelsRow.appendChild(el("span", "badge mono", m));
    }
  }
  hwCard.appendChild(modelsRow);
  const oversized = hw.oversized_installed_models || [];
  if (oversized.length) {
    hwCard.appendChild(
      el(
        "div",
        "note-item-meta",
        `Skipped oversized GGUF model(s) exceeding host RAM/VRAM budget: ${oversized.join(", ")}`
      )
    );
  }

  const resolvedHosts =
    hostsRes.ok &&
    hostsRes.data &&
    typeof hostsRes.data === "object" &&
    !hostsRes.data.error
      ? hostsRes.data.hosts || hostsRes.data
      : profileRes.data?.hosts || {};
  hostsCache = resolvedHosts;
  renderSystemHostsTable(resolvedHosts);

  fitWrap.replaceChildren();
  const fit = fitnessRes.data || {};
  const fitKpis = el("div", "kpi-grid");
  fitKpis.appendChild(
    makeKpiCard(
      "Review Bottlenecks",
      fit.pending_review_bottleneck_count ??
        (fit.pending_review_bottlenecks || []).length
    )
  );
  fitKpis.appendChild(
    makeKpiCard("Tracked Procedures", (fit.procedure_maturity || []).length)
  );
  fitKpis.appendChild(
    makeKpiCard(
      "Stale / Dormant",
      (
        fit.stale_or_dormant_atoms ||
        fit.dormant_atoms ||
        fit.cold_verified_atoms ||
        []
      ).length
    )
  );
  fitKpis.appendChild(
    makeKpiCard(
      "High-Utility Atoms",
      (
        fit.top_performing_atoms ||
        fit.high_fitness_atoms ||
        fit.high_utility_verified_atoms ||
        []
      ).length
    )
  );
  fitWrap.appendChild(fitKpis);

  const procs = fit.procedure_maturity || [];
  if (procs.length) {
    const procBar = el("div", "backlinks-bar");
    procBar.style.marginTop = "8px";
    procBar.appendChild(el("span", "field-label", "Procedure Maturity:"));
    for (const p of procs.slice(0, 12)) {
      procBar.appendChild(
        el(
          "span",
          "badge",
          `${p.atom_id || "pending-atom"} · ${
            p.maturity_stage || p.stage || "candidate"
          }`
        )
      );
    }
    fitWrap.appendChild(procBar);
  }
  loadOnnxModelsStatus();
  loadSyncStatus();
  loadInboxDropPairings();
  loadWorkspaces();
  loadClipperPairings();
  loadDaemonStatus();
  loadMobileVaultClient();
}

async function loadOnnxModelsStatus() {
  const kpiWrap = document.getElementById("onnxStatusKpis");
  const outEl = document.getElementById("onnxOutput");
  if (!kpiWrap || !outEl) return;
  kpiWrap.replaceChildren();
  try {
    const res = await window.secondBrain.request("GET", "/v1/models/onnx");
    if (!res.ok) {
      outEl.textContent = apiErrorMessage(res, "ONNX status unavailable");
      return;
    }
    const d = res.data || {};
    const slots = d.slots || {};
    const rtReady = Boolean(d.onnx_runtime_ready ?? d.runtime_available);
    kpiWrap.appendChild(
      makeKpiCard(
        "Runtime",
        rtReady ? "ONNXRUNTIME READY" : "FALLBACK ONLY",
        d.models_root || "",
        rtReady ? "ok" : "warn"
      )
    );
    const ce = slots["cross-encoder"] || {};
    kpiWrap.appendChild(
      makeKpiCard(
        "Cross-Encoder Reranker",
        ce.verified ? "VERIFIED" : ce.installed ? "NEEDS VERIFY" : "NOT PULLED",
        ce.model_id || ce.repo_id || "ms-marco-MiniLM-L-6-v2",
        ce.verified ? "ok" : "warn"
      )
    );
    const nli = slots.nli || {};
    kpiWrap.appendChild(
      makeKpiCard(
        "Bidirectional NLI",
        nli.verified ? "VERIFIED" : nli.installed ? "NEEDS VERIFY" : "NOT PULLED",
        nli.model_id || nli.repo_id || "nli-deberta-v3-xsmall",
        nli.verified ? "ok" : "warn"
      )
    );
    outEl.textContent = d.all_verified
      ? `All pinned ONNX weights verified in ${d.models_root || "security/models"} (neural CPU reranking & clause-aligned NLI active).`
      : "ONNX weights not yet installed — deterministic fallback engines active until you click Pull Verified Weights.";
  } catch (err) {
    outEl.textContent = `ONNX status error: ${err?.message || String(err)}`;
  }
}

const onnxRefreshBtnEl = document.getElementById("onnxRefreshBtn");
if (onnxRefreshBtnEl) onnxRefreshBtnEl.addEventListener("click", loadOnnxModelsStatus);

const onnxPullBtnEl = document.getElementById("onnxPullBtn");
if (onnxPullBtnEl) {
  onnxPullBtnEl.addEventListener("click", async () => {
    const outEl = document.getElementById("onnxOutput");
    onnxPullBtnEl.disabled = true;
    if (outEl) outEl.textContent = "Pulling & SHA-256 verifying pinned HuggingFace ONNX weights...";
    try {
      const res = await window.secondBrain.request("POST", "/v1/models/onnx/pull", { slot: "all" });
      if (!res.ok) {
        const msg = apiErrorMessage(res, "ONNX pull failed");
        if (outEl) outEl.textContent = msg;
        showToast(msg, "danger");
        return;
      }
      showToast("Pulled & verified local ONNX weights!", "ok");
      await loadOnnxModelsStatus();
    } catch (err) {
      showToast(`ONNX pull error: ${err?.message || String(err)}`, "danger");
    } finally {
      onnxPullBtnEl.disabled = false;
    }
  });
}

const onnxVerifyBtnEl = document.getElementById("onnxVerifyBtn");
if (onnxVerifyBtnEl) {
  onnxVerifyBtnEl.addEventListener("click", async () => {
    const outEl = document.getElementById("onnxOutput");
    onnxVerifyBtnEl.disabled = true;
    try {
      const res = await window.secondBrain.request("POST", "/v1/models/onnx/verify", {
        slot: "all",
        smoke_test: true,
      });
      if (!res.ok) {
        const msg = apiErrorMessage(res, "ONNX verification failed");
        if (outEl) outEl.textContent = msg;
        showToast(msg, "danger");
        return;
      }
      const ok = Boolean(res.data?.all_verified);
      if (outEl) {
        outEl.textContent = ok
          ? "SHA-256 digests and CPU smoke inference passed on both Cross-Encoder and DeBERTa NLI!"
          : `ONNX verification reported missing/invalid slot(s) (all_verified=${ok}).`;
      }
      showToast(ok ? "ONNX weights & smoke inference verified!" : "ONNX weights missing or invalid", ok ? "ok" : "warn");
      await loadOnnxModelsStatus();
    } catch (err) {
      showToast(`ONNX verify error: ${err?.message || String(err)}`, "danger");
    } finally {
      onnxVerifyBtnEl.disabled = false;
    }
  });
}

async function loadInboxDropPairings() {
  const wrap = document.getElementById("inboxDropPairingsWrap");
  const outEl = document.getElementById("inboxDropOutput");
  if (!wrap) return;
  try {
    const res = await window.secondBrain.request("GET", "/v1/sync/inbox-drop/pairings");
    wrap.replaceChildren();
    if (!res.ok) {
      if (outEl) outEl.textContent = apiErrorMessage(res, "Mobile inbox drop unavailable");
      return;
    }
    const pairings = Array.isArray(res.data?.pairings) ? res.data.pairings : [];
    const activeCount = pairings.filter((p) => !p.revoked).length;
    if (res.data?.key_id && outEl && outEl.textContent.startsWith("Configure E2EE Sync")) {
      outEl.textContent = `Vault P-256 Drop Key ID: ${res.data.key_id} · ${activeCount} active mobile pairing(s).`;
    }
    if (!pairings.length) {
      wrap.appendChild(el("div", "note-item-meta", "No mobile quick-capture devices paired yet."));
      return;
    }
    for (const p of pairings) {
      const row = el("div", "toolbar");
      row.style.marginTop = "6px";
      row.appendChild(el("strong", "", p.label || p.device_label || "Mobile Device"));
      row.appendChild(el("span", "badge mono", `token:${p.token_id || ""}`));
      row.appendChild(
        el("span", `badge ${p.include_recall ? "ok" : ""}`, p.include_recall ? "SBMR1 Recall" : "Drop-Only")
      );
      row.appendChild(
        el("span", `badge ${p.revoked ? "danger" : "ok"}`, p.revoked ? "revoked" : "active")
      );
      if (!p.revoked) {
        const revBtn = el("button", "btn sm danger", "Revoke");
        revBtn.type = "button";
        revBtn.style.marginLeft = "auto";
        revBtn.onclick = async () => {
          const r = await window.secondBrain.request(
            "DELETE",
            `/v1/sync/inbox-drop/${encodeURIComponent(p.token_id)}`,
            {}
          );
          showToast(
            r.ok ? `Revoked mobile drop token ${p.token_id}` : apiErrorMessage(r, "Revoke failed"),
            r.ok ? "ok" : "danger"
          );
          await loadInboxDropPairings();
        };
        row.appendChild(revBtn);
      }
      wrap.appendChild(row);
    }
  } catch (err) {
    if (outEl) outEl.textContent = `Mobile drop error: ${err?.message || String(err)}`;
  }
}

const inboxDropRefreshBtnEl = document.getElementById("inboxDropRefreshBtn");
if (inboxDropRefreshBtnEl) inboxDropRefreshBtnEl.addEventListener("click", loadInboxDropPairings);

const inboxDropPairBtnEl = document.getElementById("inboxDropPairBtn");
if (inboxDropPairBtnEl) {
  inboxDropPairBtnEl.addEventListener("click", async () => {
    const label = (document.getElementById("inboxDropDeviceLabelInput")?.value || "Mobile Quick-Capture").trim();
    const pwaUrl = (document.getElementById("inboxDropPwaUrlInput")?.value || "").trim();
    const incRecall = Boolean(document.getElementById("inboxDropIncludeRecallCheck")?.checked);
    const incS3 = Boolean(document.getElementById("inboxDropIncludeS3Check")?.checked);
    const outEl = document.getElementById("inboxDropOutput");
    const res = await window.secondBrain.request("POST", "/v1/sync/inbox-drop/pair", {
      device_label: label || "Mobile Quick-Capture",
      pwa_base_url: pwaUrl,
      include_recall: incRecall,
      include_s3_credentials: incS3,
    });
    if (!res.ok) {
      const msg = apiErrorMessage(res, "Mobile pairing failed");
      if (outEl) outEl.textContent = msg;
      showToast(msg, "danger");
      return;
    }
    const shareTarget = res.data?.pwa_pairing_url || res.data?.pairing_fragment || "";
    try {
      if (shareTarget && navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(shareTarget);
      }
    } catch {}
    if (outEl) {
      outEl.textContent = `Paired '${res.data?.device_label}' (token_id=${res.data?.token_id}, include_recall=${Boolean(res.data?.include_recall)}, copied to clipboard): ${shareTarget}`;
    }
    showToast("Mobile E2EE Inbox Drop pairing copied to clipboard!", "ok");
    await loadInboxDropPairings();
  });
}

const inboxDropExportRecallBtnEl = document.getElementById("inboxDropExportRecallBtn");
if (inboxDropExportRecallBtnEl) {
  inboxDropExportRecallBtnEl.addEventListener("click", async () => {
    const outEl = document.getElementById("inboxDropOutput");
    const res = await window.secondBrain.request("POST", "/v1/sync/inbox-drop/export-recall", {
      include_pending_review: true,
      sync_to_backend: true,
    });
    if (!res.ok) {
      const msg = apiErrorMessage(res, "Export mobile recall index failed");
      if (outEl) outEl.textContent = msg;
      showToast(msg, "danger");
      return;
    }
    const d = res.data || {};
    if (outEl) {
      outEl.textContent = `Exported ${d.object_key || "mobile-recall"}: ${d.atom_count ?? 0} verified atom(s), ${d.review_count ?? 0} review item(s) (uploaded=${Boolean(d.uploaded_to_sync)}).`;
    }
    showToast("Exported encrypted Mobile Recall Index (SBMR1)", "ok");
  });
}

const inboxDropRotateRecallBtnEl = document.getElementById("inboxDropRotateRecallBtn");
if (inboxDropRotateRecallBtnEl) {
  inboxDropRotateRecallBtnEl.addEventListener("click", async () => {
    const conf = await modalCtrl.confirm({
      title: "Rotate Mobile Recall Key (SBMR1)",
      message: "Rotating the Mobile Recall key invalidates existing recall-enabled pairings until you re-export and re-pair authorized devices.",
      confirmLabel: "Rotate Key",
      confirmClass: "warn",
    });
    if (!conf.confirmed) return;
    const outEl = document.getElementById("inboxDropOutput");
    const res = await window.secondBrain.request("POST", "/v1/sync/inbox-drop/rotate-recall", {});
    if (!res.ok) {
      const msg = apiErrorMessage(res, "Rotate mobile recall key failed");
      if (outEl) outEl.textContent = msg;
      showToast(msg, "danger");
      return;
    }
    if (outEl) {
      outEl.textContent = `Rotated Mobile Recall key -> recall_key_id=${res.data?.recall_key_id || "updated"}. Re-export Recall Index & re-pair devices.`;
    }
    showToast("Mobile Recall Key rotated", "warn");
    await loadInboxDropPairings();
  });
}

const inboxDropPullBtnEl = document.getElementById("inboxDropPullBtn");
if (inboxDropPullBtnEl) {
  inboxDropPullBtnEl.addEventListener("click", async () => {
    const outEl = document.getElementById("inboxDropOutput");
    const res = await window.secondBrain.request("POST", "/v1/sync/inbox-drop/pull", {
      delete_consumed: true,
    });
    if (!res.ok) {
      const msg = apiErrorMessage(res, "Pull mobile drops failed");
      if (outEl) outEl.textContent = msg;
      showToast(msg, "danger");
      return;
    }
    const d = res.data || {};
    if (outEl) {
      outEl.textContent = `Pulled mobile inbox drops: ${d.ingested_count ?? 0} staged into 0-raw/inbox/, ${d.skipped_duplicate_count ?? 0} duplicate(s) skipped, ${d.rejected_count ?? 0} rejected.`;
    }
    showToast(`Pulled ${d.ingested_count ?? 0} mobile drop(s) into Quarantined Inbox`, "ok");
    refreshHealth();
  });
}

async function loadSyncStatus() {
  const kpiWrap = document.getElementById("syncStatusKpis");
  const outEl = document.getElementById("syncOutput");
  const confWrap = document.getElementById("syncConflictsList");
  kpiWrap.replaceChildren();
  confWrap.replaceChildren();
  try {
    const res = await window.secondBrain.request("GET", "/v1/sync/status");
    if (!res.ok) {
      outEl.textContent = apiErrorMessage(res, "Sync status unavailable");
      return;
    }
    const s = res.data || {};
    kpiWrap.appendChild(
      makeKpiCard(
        "E2EE Sync",
        s.configured ? "CONFIGURED" : "NOT CONFIGURED",
        s.backend_type || "local_folder / https",
        s.configured ? "ok" : "warn"
      )
    );
    kpiWrap.appendChild(makeKpiCard("Local Epoch", s.local_epoch ?? 0));
    kpiWrap.appendChild(
      makeKpiCard(
        "Remote Epoch",
        s.remote_epoch ?? 0,
        s.remote_reachable === false ? "unreachable" : s.remote_device_id || ""
      )
    );
    kpiWrap.appendChild(makeKpiCard("Durable Files", s.durable_file_count ?? 0));
    kpiWrap.appendChild(
      makeKpiCard(
        "Key Sealing",
        s.keychain_sealed ? "OS KEYCHAIN" : String(s.kdf_mode || "keyfile").toUpperCase(),
        s.keychain_backend || "",
        s.keychain_sealed ? "ok" : ""
      )
    );
    const autoSt = s.auto_sync || {};
    kpiWrap.appendChild(
      makeKpiCard(
        "Auto-Sync",
        autoSt.enabled ? "ENABLED" : "OFF",
        autoSt.enabled ? `${autoSt.interval_seconds || 300}s · ${autoSt.last_status || "idle"}` : "",
        autoSt.enabled ? "ok" : ""
      )
    );
    kpiWrap.appendChild(
      makeKpiCard(
        "Conflicts",
        s.conflict_count ?? 0,
        "",
        (s.conflict_count || 0) > 0 ? "danger" : "ok"
      )
    );

    if (s.configured) {
      const targetInput = document.getElementById("syncTargetInput");
      if (!targetInput.value && s.target) targetInput.value = s.target;
      const backendSel = document.getElementById("syncBackendSelect");
      if (s.backend_type) backendSel.value = s.backend_type;
      const authSel = document.getElementById("syncAuthModeSelect");
      if (authSel && s.auth_mode) authSel.value = s.auth_mode;
      const regIn = document.getElementById("syncS3RegionInput");
      if (regIn && s.s3_region) regIn.value = s.s3_region;
      outEl.textContent = `Vault ${shortSha(s.vault_id || "")} · Device ${s.device_id || ""} · Auth ${s.auth_mode || "bearer"} · Mode ${s.kdf_mode || "keyfile"}${s.keychain_sealed ? " (DPAPI/Keychain)" : ""} · Local pending edits: ${s.local_changed_count ?? 0}`;
    } else {
      outEl.textContent =
        "Not configured yet. Enter a local sync folder (OneDrive/Syncthing/NAS) or https:// S3/R2 target and click Init or Join.";
    }

    const conflicts = Array.isArray(s.conflicts) ? s.conflicts : [];
    if (conflicts.length) {
      confWrap.appendChild(
        el("div", "field-label", `Quarantined Sync Conflicts (${conflicts.length}):`)
      );
      for (const c of conflicts.slice(0, 20)) {
        const row = el("div", "diff-card removed");
        const bar = el("div", "toolbar");
        bar.appendChild(
          el(
            "span",
            "",
            `${c.rel_path || "?"} · epoch ${c.remote_epoch ?? "?"} (${c.conflict_kind || "divergent_edit"})`
          )
        );
        const resBtn = el("button", "btn sm primary", "Resolve 3-Way…");
        resBtn.type = "button";
        resBtn.style.marginLeft = "auto";
        resBtn.onclick = () => openSyncConflictResolverModal(c);
        bar.appendChild(resBtn);
        row.appendChild(bar);
        confWrap.appendChild(row);
      }
    }
  } catch (err) {
    outEl.textContent = `Sync status error: ${err?.message || String(err)}`;
  }
}

async function openSyncConflictResolverModal(conflictSummary) {
  const cid = conflictSummary?.conflict_id || conflictSummary?.rel_path || "";
  if (!cid) return;
  modalCtrl.open({
    title: `3-Way Conflict Resolver: ${conflictSummary.rel_path || cid}`,
    renderBody(body) {
      body.appendChild(el("div", "status-banner", "Loading 3-way conflict versions & unified diff..."));
    },
  });

  const res = await window.secondBrain.request(
    "GET",
    `/v1/sync/conflicts/${encodeURIComponent(cid)}`
  );
  modalBodyEl.replaceChildren();
  modalFooterEl.replaceChildren();
  if (!res.ok || !res.data) {
    modalBodyEl.appendChild(
      el("div", "status-banner banner-danger", apiErrorMessage(res, "Failed to load conflict detail"))
    );
    const closeBtn = el("button", "btn", "Close");
    closeBtn.type = "button";
    closeBtn.onclick = closeModal;
    modalFooterEl.appendChild(closeBtn);
    return;
  }

  const det = res.data;
  modalBodyEl.appendChild(
    el(
      "div",
      "status-banner banner-warn",
      `Path: ${det.rel_path} · Local SHA: ${shortSha(det.current_local_sha256)} · Remote SHA: ${shortSha(det.remote_sha256 || "")}${
        det.is_canonical_atom
          ? " · Manual merges on canonical atoms automatically reset review_status=pending until owner re-approval."
          : ""
      }`
    )
  );

  if (det.base_content) {
    const baseCol = el("div", "field-col");
    baseCol.appendChild(el("span", "field-label", "Common Ancestor (Base)"));
    baseCol.appendChild(el("pre", "mono-box", det.base_content));
    modalBodyEl.appendChild(baseCol);
  }

  const diffCol = el("div", "field-col");
  diffCol.appendChild(el("span", "field-label", "Unified Diff (Local vs Remote)"));
  diffCol.appendChild(el("pre", "mono-box", det.unified_diff || "(no line diff)"));
  modalBodyEl.appendChild(diffCol);

  const mergeCol = el("div", "field-col");
  mergeCol.appendChild(
    el("span", "field-label", "Manual 3-Way Merge Editor (edit below and click Save Manual Merge)")
  );
  const mergeTa = el("textarea", "editor-textarea");
  mergeTa.style.minHeight = "180px";
  mergeTa.value = det.local_content || det.remote_content || "";
  mergeCol.appendChild(mergeTa);
  modalBodyEl.appendChild(mergeCol);

  async function applyResolution(strategy) {
    const payload = {
      conflict_id: det.conflict_id || det.rel_path,
      resolution: strategy,
      expected_local_sha256: det.current_local_sha256 || undefined,
      actor: "desktop_owner",
    };
    if (strategy === "manual_merge") {
      payload.merged_content = mergeTa.value;
    }
    const r = await window.secondBrain.request("POST", "/v1/sync/conflicts/resolve", payload);
    if (!r.ok) {
      showToast(apiErrorMessage(r, "Conflict resolution failed"), "danger");
      return;
    }
    showToast(`Resolved ${det.rel_path} (${strategy})`, "ok");
    closeModal();
    await Promise.all([loadSyncStatus(), refreshHealth(), loadNotes({ append: false })]);
  }

  const cancelBtn = el("button", "btn", "Cancel");
  cancelBtn.type = "button";
  cancelBtn.onclick = closeModal;

  const keepLocalBtn = el("button", "btn", "Keep Local");
  keepLocalBtn.type = "button";
  keepLocalBtn.onclick = () => applyResolution("keep_local");

  const acceptRemoteBtn = el("button", "btn warn", "Accept Remote");
  acceptRemoteBtn.type = "button";
  acceptRemoteBtn.onclick = () => applyResolution("accept_remote");

  const keepBothBtn = el("button", "btn", "Keep Both");
  keepBothBtn.type = "button";
  keepBothBtn.onclick = () => applyResolution("keep_both");

  const manualBtn = el("button", "btn primary", "Save Manual Merge");
  manualBtn.type = "button";
  manualBtn.onclick = () => applyResolution("manual_merge");

  modalFooterEl.replaceChildren(cancelBtn, keepLocalBtn, acceptRemoteBtn, keepBothBtn, manualBtn);
}

function collectSyncInitJoinOptions() {
  const authMode = document.getElementById("syncAuthModeSelect")?.value || "bearer";
  const useKc = Boolean(document.getElementById("syncUseKeychainCheck")?.checked);
  const s3Reg = (document.getElementById("syncS3RegionInput")?.value || "auto").trim() || "auto";
  const s3Ak = (document.getElementById("syncS3AccessEnvInput")?.value || "").trim();
  const s3Sk = (document.getElementById("syncS3SecretEnvInput")?.value || "").trim();
  const opts = {
    auth_mode: authMode,
    use_keychain: useKc,
    s3_region: s3Reg,
  };
  if (authMode === "s3_sigv4") {
    if (s3Ak) opts.s3_access_key_env = s3Ak;
    if (s3Sk) opts.s3_secret_key_env = s3Sk;
  }
  return opts;
}

async function runSyncAction(endpoint, body, successLabel) {
  const outEl = document.getElementById("syncOutput");
  outEl.textContent = `Running ${endpoint}...`;
  try {
    const res = await window.secondBrain.request("POST", endpoint, body);
    if (!res.ok) {
      const msg = apiErrorMessage(res, `${successLabel} failed`);
      outEl.textContent = msg;
      showToast(msg, "danger");
      return;
    }
    const d = res.data || {};
    let detail = `${successLabel} complete`;
    if (d.epoch !== undefined) detail += ` · epoch ${d.epoch}`;
    if (d.uploaded_blobs !== undefined) {
      detail += ` · uploaded ${d.uploaded_blobs}, reused ${d.reused_blobs ?? 0}`;
    }
    if (d.added !== undefined) {
      detail += ` · added ${d.added}, updated ${d.updated ?? 0}, merged JSONL ${d.merged_jsonl ?? 0}, conflicts ${d.conflict_count ?? 0}`;
    }
    if (d.valid !== undefined) {
      detail += ` · valid=${d.valid} (${d.verified_blobs ?? 0} verified blobs)`;
    }
    if (d.recovery_key_b64) {
      detail += ` · Save Recovery Key safely: ${d.recovery_key_b64}`;
    }
    outEl.textContent = detail;
    showToast(successLabel, d.valid === false ? "danger" : "ok");
    await loadSyncStatus();
  } catch (err) {
    const msg = `${successLabel} error: ${err?.message || String(err)}`;
    outEl.textContent = msg;
    showToast(msg, "danger");
  }
}

document.getElementById("syncRefreshBtn").addEventListener("click", loadSyncStatus);
document.getElementById("syncInitBtn").addEventListener("click", () => {
  const target = document.getElementById("syncTargetInput").value.trim();
  if (!target) {
    showToast("Enter a sync target folder or https:// URL first.", "danger");
    return;
  }
  const body = {
    target,
    backend_type: document.getElementById("syncBackendSelect").value,
    ...collectSyncInitJoinOptions(),
  };
  const dev = document.getElementById("syncDeviceNameInput").value.trim();
  const pw = document.getElementById("syncPassphraseInput").value;
  if (dev) body.device_name = dev;
  if (pw) body.passphrase = pw;
  runSyncAction("/v1/sync/init", body, "E2EE Sync Initialized");
});
document.getElementById("syncJoinBtn").addEventListener("click", () => {
  const target = document.getElementById("syncTargetInput").value.trim();
  if (!target) {
    showToast("Enter a sync target folder or https:// URL first.", "danger");
    return;
  }
  const body = {
    target,
    backend_type: document.getElementById("syncBackendSelect").value,
    ...collectSyncInitJoinOptions(),
  };
  const dev = document.getElementById("syncDeviceNameInput").value.trim();
  const pw = document.getElementById("syncPassphraseInput").value;
  const rk = document.getElementById("syncRecoveryKeyInput").value.trim();
  if (dev) body.device_name = dev;
  if (pw) body.passphrase = pw;
  if (rk) body.recovery_key_b64 = rk;
  runSyncAction("/v1/sync/join", body, "Joined E2EE Sync Vault");
});
document.getElementById("syncPushBtn").addEventListener("click", () => {
  const pw = document.getElementById("syncPassphraseInput").value;
  runSyncAction("/v1/sync/push", pw ? { passphrase: pw } : {}, "Encrypted Sync Push");
});
document.getElementById("syncPullBtn").addEventListener("click", () => {
  const pw = document.getElementById("syncPassphraseInput").value;
  runSyncAction(
    "/v1/sync/pull",
    pw ? { passphrase: pw, trust_new_peers: true } : { trust_new_peers: true },
    "Encrypted Sync Pull"
  );
});
document.getElementById("syncVerifyBtn").addEventListener("click", () => {
  const pw = document.getElementById("syncPassphraseInput").value;
  runSyncAction("/v1/sync/verify", pw ? { passphrase: pw } : {}, "Cryptographic Sync Verify");
});
const syncGcBtnEl = document.getElementById("syncGcBtn");
if (syncGcBtnEl) {
  syncGcBtnEl.addEventListener("click", () => {
    const pw = document.getElementById("syncPassphraseInput").value;
    runSyncAction("/v1/sync/gc", pw ? { passphrase: pw } : {}, "Pruned Orphan Sync Blobs");
  });
}
const syncSealKcBtnEl = document.getElementById("syncSealKeychainBtn");
if (syncSealKcBtnEl) {
  syncSealKcBtnEl.addEventListener("click", () => {
    const pw = document.getElementById("syncPassphraseInput").value;
    runSyncAction("/v1/sync/seal-keychain", pw ? { passphrase: pw } : {}, "Sealed Master Key in Keychain/DPAPI");
  });
}
const syncAutoEnableBtnEl = document.getElementById("syncAutoEnableBtn");
if (syncAutoEnableBtnEl) {
  syncAutoEnableBtnEl.addEventListener("click", () => {
    const iv = Number.parseInt(document.getElementById("syncAutoIntervalInput")?.value || "300", 10) || 300;
    runSyncAction("/v1/sync/auto", { enabled: true, interval_seconds: iv }, "Enabled Background Auto-Sync");
  });
}
const syncAutoDisableBtnEl = document.getElementById("syncAutoDisableBtn");
if (syncAutoDisableBtnEl) {
  syncAutoDisableBtnEl.addEventListener("click", () => {
    runSyncAction("/v1/sync/auto", { enabled: false }, "Disabled Background Auto-Sync");
  });
}
const syncAutoRunOnceBtnEl = document.getElementById("syncAutoRunOnceBtn");
if (syncAutoRunOnceBtnEl) {
  syncAutoRunOnceBtnEl.addEventListener("click", () => {
    runSyncAction("/v1/sync/auto", { run_once: true }, "Executed Auto-Sync Cycle");
  });
}

// ─── Multi-Vault Workspaces & Browser Web Clipper (v0.3.0) ───────────────────

async function loadWorkspaces() {
  const sidebarSel = document.getElementById("sidebarVaultSelect");
  const listWrap = document.getElementById("workspacesListWrap");
  const outEl = document.getElementById("workspacesOutput");
  if (!sidebarSel && !listWrap) return;
  try {
    const res = await window.secondBrain.request("GET", "/v1/vaults");
    if (!res.ok) {
      if (outEl) outEl.textContent = apiErrorMessage(res, "Workspaces unavailable");
      return;
    }
    const data = res.data || {};
    const vaults = Array.isArray(data.vaults) ? data.vaults : [];
    if (sidebarSel) {
      sidebarSel.replaceChildren();
      for (const v of vaults) {
        const opt = document.createElement("option");
        opt.value = v.vault_id || "default";
        opt.textContent = `${v.label || v.vault_id}${v.active ? " (Active)" : ""}`;
        opt.selected = Boolean(v.active);
        sidebarSel.appendChild(opt);
      }
    }
    if (listWrap) {
      listWrap.replaceChildren();
      for (const v of vaults) {
        const row = el("div", "toolbar");
        row.style.marginBottom = "6px";
        row.appendChild(el("strong", "", v.label || v.vault_id));
        row.appendChild(el("span", `badge ${v.active ? "ok" : ""}`, v.active ? "active" : v.vault_id));
        row.appendChild(
          el(
            "span",
            `badge ${v.federated_searchable ? "ok" : "warn"}`,
            v.federated_searchable ? "federated" : "isolated"
          )
        );
        row.appendChild(
          el("span", "note-item-meta", `${v.path || ""} (${v.atom_count ?? 0} atoms, ${v.note_count ?? 0} notes)`)
        );
        if (!v.active) {
          const swBtn = el("button", "btn sm primary", "Switch");
          swBtn.type = "button";
          swBtn.style.marginLeft = "auto";
          swBtn.onclick = () => switchWorkspaceVault(v.vault_id);
          row.appendChild(swBtn);
        }
        const fedBtn = el("button", "btn sm", v.federated_searchable ? "Disable Fed" : "Opt-In Fed");
        fedBtn.type = "button";
        if (v.active) fedBtn.style.marginLeft = "auto";
        fedBtn.onclick = async () => {
          try {
            const r = await window.secondBrain.request("PATCH", `/v1/vaults/${encodeURIComponent(v.vault_id)}`, {
              federated_searchable: !v.federated_searchable,
            });
            showToast(
              r.ok ? `Updated ${v.vault_id} federated_searchable=${!v.federated_searchable}` : apiErrorMessage(r, "Update failed"),
              r.ok ? "ok" : "danger"
            );
            await loadWorkspaces();
          } catch (err) {
            showToast(err?.message || "Update failed", "danger");
          }
        };
        row.appendChild(fedBtn);
        if (!v.active) {
          const rmBtn = el("button", "btn sm danger", "Unregister");
          rmBtn.type = "button";
          rmBtn.onclick = async () => {
            const r = await window.secondBrain.request("DELETE", `/v1/vaults/${encodeURIComponent(v.vault_id)}`, {});
            showToast(r.ok ? `Unregistered ${v.vault_id} (files kept)` : apiErrorMessage(r, "Remove failed"), r.ok ? "ok" : "danger");
            await loadWorkspaces();
          };
          row.appendChild(rmBtn);
        }
        listWrap.appendChild(row);
      }
    }
    if (outEl) {
      outEl.textContent = `Active workspace: ${data.active_vault_id || "default"} (${data.active_vault_path || ""}) · ${vaults.length} registered vault(s).`;
    }
  } catch (err) {
    if (outEl) outEl.textContent = `Workspace error: ${err?.message || String(err)}`;
  }
}

async function switchWorkspaceVault(vaultId) {
  if (!vaultId) return;
  if (!(await confirmDiscardIfDirty())) {
    await loadWorkspaces();
    return;
  }
  const res = await window.secondBrain.request("POST", "/v1/vaults/switch", { vault_id: vaultId });
  if (!res.ok) {
    showToast(apiErrorMessage(res, "Vault switch failed"), "danger");
    await loadWorkspaces();
    return;
  }
  showToast(`Switched active vault -> ${res.data?.active_vault_id || vaultId}`, "ok");
  resetActiveVaultUiState();
  await Promise.all([loadWorkspaces(), refreshHealth(), loadNotes({ append: false })]);
  refreshActiveViewForCurrentVault();
}

const sidebarVaultSelectEl = document.getElementById("sidebarVaultSelect");
if (sidebarVaultSelectEl) {
  sidebarVaultSelectEl.addEventListener("change", () => switchWorkspaceVault(sidebarVaultSelectEl.value));
}

const wsRefreshBtnEl = document.getElementById("workspacesRefreshBtn");
if (wsRefreshBtnEl) wsRefreshBtnEl.addEventListener("click", loadWorkspaces);

const wsCreateBtnEl = document.getElementById("wsCreateBtn");
if (wsCreateBtnEl) {
  wsCreateBtnEl.addEventListener("click", async () => {
    const label = (document.getElementById("wsLabelInput")?.value || "").trim();
    const vpath = (document.getElementById("wsPathInput")?.value || "").trim();
    const outEl = document.getElementById("workspacesOutput");
    if (!label || !vpath) {
      showToast("Enter both a vault label and directory path.", "danger");
      return;
    }
    const res = await window.secondBrain.request("POST", "/v1/vaults", {
      label,
      path: vpath,
      isolated_token: Boolean(document.getElementById("wsIsolatedTokenCheck")?.checked),
      federated_searchable: Boolean(document.getElementById("wsFederatedCheck")?.checked),
      activate: Boolean(document.getElementById("wsActivateCheck")?.checked),
    });
    if (!res.ok) {
      const msg = apiErrorMessage(res, "Create vault failed");
      if (outEl) outEl.textContent = msg;
      showToast(msg, "danger");
      return;
    }
    const tokNote = res.data?.vault_token ? ` · Save isolated token: ${res.data.vault_token}` : "";
    if (outEl) outEl.textContent = `Created vault '${res.data?.vault_id}' at ${res.data?.path}${tokNote}`;
    showToast(`Created vault '${res.data?.vault_id}'`, "ok");
    await loadWorkspaces();
    if (res.data?.active) {
      resetActiveVaultUiState();
      await Promise.all([refreshHealth(), loadNotes({ append: false })]);
      refreshActiveViewForCurrentVault();
    }
  });
}

const wsFedSearchBtnEl = document.getElementById("wsFedSearchBtn");
if (wsFedSearchBtnEl) {
  wsFedSearchBtnEl.addEventListener("click", async () => {
    const q = (document.getElementById("wsFedQueryInput")?.value || "").trim();
    const wrap = document.getElementById("wsFedResultsWrap");
    if (!q) {
      showToast("Enter a query for federated search.", "warn");
      return;
    }
    const res = await window.secondBrain.request("POST", "/v1/vaults/federated-recall", { query: q, limit: 8 });
    if (wrap) wrap.replaceChildren();
    if (!res.ok) {
      showToast(apiErrorMessage(res, "Federated recall failed"), "danger");
      return;
    }
    const hits = res.data?.results || [];
    if (!wrap) return;
    if (!hits.length) {
      wrap.appendChild(
        el(
          "div",
          "note-item-meta",
          `No approved matches across opted-in vaults (searched: ${(res.data?.searched_vaults || []).join(", ") || "none"}).`
        )
      );
      return;
    }
    for (const h of hits) {
      const card = el("div", "sub-panel");
      card.style.marginTop = "6px";
      card.appendChild(el("span", "badge ok", h.vault_label || h.vault_id || "vault"));
      card.appendChild(el("strong", "", ` ${h.filename || ""}`));
      card.appendChild(el("div", "note-item-meta", `score=${h.score ?? "?"}`));
      wrap.appendChild(card);
    }
  });
}

async function loadClipperPairings() {
  const wrap = document.getElementById("clipperPairingsWrap");
  const outEl = document.getElementById("clipperOutput");
  if (!wrap) return;
  try {
    const res = await window.secondBrain.request("GET", "/v1/clipper/pairings");
    wrap.replaceChildren();
    if (!res.ok) {
      if (outEl) outEl.textContent = apiErrorMessage(res, "Clipper pairings unavailable");
      return;
    }
    const items = res.data?.pairings || [];
    if (!items.length) {
      wrap.appendChild(el("div", "note-item-meta", "No browser extensions paired yet."));
      return;
    }
    for (const p of items) {
      const row = el("div", "toolbar");
      row.style.marginTop = "6px";
      row.appendChild(el("strong", "", p.label || "Browser Clipper"));
      row.appendChild(el("span", "badge mono", p.origin || ""));
      row.appendChild(el("span", "note-item-meta", p.token_preview || ""));
      const revBtn = el("button", "btn sm danger", "Revoke");
      revBtn.type = "button";
      revBtn.style.marginLeft = "auto";
      revBtn.onclick = async () => {
        const r = await window.secondBrain.request("DELETE", `/v1/clipper/pair/${encodeURIComponent(p.pairing_id)}`, {});
        showToast(r.ok ? `Revoked ${p.pairing_id}` : apiErrorMessage(r, "Revoke failed"), r.ok ? "ok" : "danger");
        await loadClipperPairings();
      };
      row.appendChild(revBtn);
      wrap.appendChild(row);
    }
  } catch (err) {
    if (outEl) outEl.textContent = `Clipper error: ${err?.message || String(err)}`;
  }
}

const clipperRefreshBtnEl = document.getElementById("clipperRefreshBtn");
if (clipperRefreshBtnEl) clipperRefreshBtnEl.addEventListener("click", loadClipperPairings);

const clipperPairCodeBtnEl = document.getElementById("clipperPairCodeBtn");
if (clipperPairCodeBtnEl) {
  clipperPairCodeBtnEl.addEventListener("click", async () => {
    const label = (document.getElementById("clipperLabelInput")?.value || "Browser Clipper").trim();
    const outEl = document.getElementById("clipperOutput");
    const res = await window.secondBrain.request("POST", "/v1/clipper/pair-code", {
      label,
      ttl_seconds: 300,
    });
    if (!res.ok) {
      const msg = apiErrorMessage(res, "Generate pairing code failed");
      if (outEl) outEl.textContent = msg;
      showToast(msg, "danger");
      return;
    }
    const code = res.data?.code || "";
    try {
      if (code && navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(code);
      }
    } catch {}
    if (outEl) {
      outEl.textContent = `1-Click Pairing Code (copied to clipboard, valid 5 min): ${code} — Paste into Clipper popup and click 'Pair in 1 Click'.`;
    }
    showToast(`Copied 1-Click Pairing Code: ${code}`, "ok");
  });
}

const clipperPairBtnEl = document.getElementById("clipperPairBtn");
if (clipperPairBtnEl) {
  clipperPairBtnEl.addEventListener("click", async () => {
    const origin = (document.getElementById("clipperOriginInput")?.value || "").trim();
    const label = (document.getElementById("clipperLabelInput")?.value || "Browser Clipper").trim();
    const outEl = document.getElementById("clipperOutput");
    if (!origin) {
      showToast("Paste a chrome-extension://, moz-extension://, or safari-web-extension:// origin first.", "danger");
      return;
    }
    const res = await window.secondBrain.request("POST", "/v1/clipper/pair", { origin, label });
    if (!res.ok) {
      const msg = apiErrorMessage(res, "Pairing failed");
      if (outEl) outEl.textContent = msg;
      showToast(msg, "danger");
      return;
    }
    if (outEl) {
      outEl.textContent = `Paired ${res.data?.label} (${res.data?.pairing_id}) · Copy scoped token into Clipper popup: ${res.data?.token}`;
    }
    showToast("Browser Clipper paired! Copy the scoped token from the status banner.", "ok");
    await loadClipperPairings();
  });
}

document
  .getElementById("refreshSystemBtn")
  .addEventListener("click", () => {
    loadSystemView();
    loadDaemonStatus();
    loadMobileVaultClient();
    refreshUpdaterStatus();
  });

function renderUpdaterBanner(st) {
  const banner = document.getElementById("updaterStatusBanner");
  const dlBtn = document.getElementById("updaterDownloadBtn");
  const instBtn = document.getElementById("updaterInstallBtn");
  if (!banner || !st) return;
  const ver = st.currentVersion ? `v${st.currentVersion}` : "local";
  if (st.state === "available") {
    banner.textContent = `Update v${st.availableVersion} available (current ${ver}). Click Download Update to fetch differential package.`;
  } else if (st.state === "downloading") {
    banner.textContent = `Downloading update v${st.availableVersion || ""} (${st.progressPercent || 0}%)...`;
  } else if (st.state === "downloaded") {
    banner.textContent = `Update v${st.availableVersion || ""} downloaded and verified. Click Restart & Install to cleanly stop the sidecar and apply.`;
  } else if (st.state === "up_to_date") {
    banner.textContent = `Desktop ${ver} is up to date (last checked: ${st.checkedAt || "now"}).`;
  } else if (st.state === "error") {
    banner.textContent = `Update check error: ${st.error || "unknown error"}`;
  } else if (st.state === "dev_mode") {
    banner.textContent = `Running unpackaged dev build (${ver}); signed feed checks activate in packaged installers.`;
  } else {
    banner.textContent = `Desktop ${ver} — manual local-first update checks only (autoDownload=false).`;
  }
  if (dlBtn) dlBtn.disabled = st.state !== "available";
  if (instBtn) instBtn.disabled = !st.downloaded && st.state !== "downloaded";
}

async function refreshUpdaterStatus() {
  if (!window.secondBrain || typeof window.secondBrain.getUpdaterStatus !== "function") return;
  try {
    const st = await window.secondBrain.getUpdaterStatus();
    renderUpdaterBanner(st);
  } catch {}
}

if (window.secondBrain && typeof window.secondBrain.onUpdaterStatus === "function") {
  window.secondBrain.onUpdaterStatus((st) => renderUpdaterBanner(st));
}

document.getElementById("updaterCheckBtn").addEventListener("click", async () => {
  if (!window.secondBrain || typeof window.secondBrain.checkForUpdates !== "function") return;
  try {
    const res = await window.secondBrain.checkForUpdates();
    if (res && res.status) renderUpdaterBanner(res.status);
  } catch (err) {
    showToast(`Update check failed: ${err?.message || String(err)}`, "warn");
  }
});

document.getElementById("updaterDownloadBtn").addEventListener("click", async () => {
  if (!window.secondBrain || typeof window.secondBrain.downloadUpdate !== "function") return;
  try {
    const res = await window.secondBrain.downloadUpdate();
    if (res && res.status) renderUpdaterBanner(res.status);
  } catch (err) {
    showToast(`Update download failed: ${err?.message || String(err)}`, "warn");
  }
});

document.getElementById("updaterInstallBtn").addEventListener("click", async () => {
  if (!window.secondBrain || typeof window.secondBrain.installUpdate !== "function") return;
  try {
    await window.secondBrain.installUpdate();
  } catch (err) {
    showToast(`Update install failed: ${err?.message || String(err)}`, "danger");
  }
});
document
  .getElementById("switchVaultBtn")
  .addEventListener("click", async () => {
    try {
      if (!(await confirmDiscardIfDirty())) return;
      const res = await window.secondBrain.switchVault();
      if (res && res.canceled) return;
      if (res && res.ok) {
        showToast(`Switched active vault to ${res.vaultDir}`, "ok");
        resetActiveVaultUiState();
        loadNotes({ append: false });
        loadWorkspaces();
        loadSystemView();
        refreshHealth();
        refreshActiveViewForCurrentVault();
      } else if (res && res.error) {
        showToast(res.error, "danger");
      }
    } catch (err) {
      showToast(`Switch vault failed: ${err?.message || String(err)}`, "danger");
    }
  });

function parseOptionalConfidence(rawVal) {
  const s = String(rawVal || "").trim();
  if (!s) return undefined;
  const num = Number(s);
  if (!Number.isFinite(num) || num < 0 || num > 1) {
    return NaN;
  }
  return num;
}

document.getElementById("recordExpBtn").addEventListener("click", async () => {
  const goal = document.getElementById("expGoalInput").value.trim();
  const result_summary = document.getElementById("expSummaryInput").value.trim();
  if (!goal || !result_summary) {
    showToast(
      "Goal and Result Summary are required to log an experience.",
      "warn"
    );
    return;
  }

  const confBefore = parseOptionalConfidence(
    document.getElementById("expConfBeforeInput").value
  );
  const confAfter = parseOptionalConfidence(
    document.getElementById("expConfAfterInput").value
  );
  if (Number.isNaN(confBefore) || Number.isNaN(confAfter)) {
    showToast(
      "Confidence Before / After must be numbers between 0.0 and 1.0.",
      "warn"
    );
    return;
  }

  const payload = {
    goal,
    result_summary,
    success: Boolean(document.getElementById("expSuccessCheck").checked),
    context: parseTags(document.getElementById("expContextInput").value),
    tools_used: parseTags(document.getElementById("expToolsInput").value),
    referenced_atoms: parseTags(document.getElementById("expAtomsInput").value),
    plan: parseLines(document.getElementById("expPlanInput").value),
    actions: parseLines(document.getElementById("expActionsInput").value),
    lessons: parseLines(document.getElementById("expLessonsInput").value),
    candidate_procedure_title:
      document.getElementById("expCandTitleInput").value.trim() || null,
    propose_to_inbox: Boolean(document.getElementById("expProposeCheck").checked),
  };

  const reasoning = document.getElementById("expReasoningInput").value.trim();
  if (reasoning) payload.reasoning_summary = reasoning;
  if (confBefore !== undefined) payload.confidence_before = confBefore;
  if (confAfter !== undefined) payload.confidence_after = confAfter;
  const reflection = document.getElementById("expReflectionInput").value.trim();
  if (reflection) payload.reflection = reflection;

  const res = await window.secondBrain.api(
    "POST",
    "/v1/cognitive/experiences",
    payload
  );
  if (res.ok) {
    for (const id of [
      "expGoalInput",
      "expContextInput",
      "expToolsInput",
      "expAtomsInput",
      "expSummaryInput",
      "expReasoningInput",
      "expPlanInput",
      "expActionsInput",
      "expLessonsInput",
      "expConfBeforeInput",
      "expConfAfterInput",
      "expReflectionInput",
      "expCandTitleInput",
    ]) {
      const node = document.getElementById(id);
      if (node) node.value = "";
    }
    showToast(
      `Recorded experience ${
        res.data?.filename || res.data?.experience_id || ""
      }`,
      "ok"
    );
  } else {
    showToast(apiErrorMessage(res, "Failed to record experience"), "danger");
  }
});

// ─── TAB 7: Playbooks & Grounded Executive Planner ───────────────────────────

function syncPlaybookUpdateModeUi() {
  const expectedSha = document
    .getElementById("pbExpectedShaInput")
    .value.trim();
  const banner = document.getElementById("pbUpdateModeBanner");
  const resetBtn = document.getElementById("pbResetUpdateBtn");
  const promoteBtn = document.getElementById("promotePlaybookBtn");
  const heading = document.getElementById("pbFormHeading");

  if (expectedSha) {
    const idVal = document.getElementById("pbIdInput").value.trim() || "playbook";
    banner.style.display = "block";
    banner.textContent = `CAS Update Mode: updating '${idVal}' anchored to expected_playbook_sha256=${shortSha(
      expectedSha
    )} (409 Conflict if modified concurrently).`;
    resetBtn.style.display = "inline-block";
    promoteBtn.textContent = "Update Playbook (CAS)";
    heading.textContent = `Update / Re-Promote Existing Skill Playbook (${idVal})`;
  } else {
    banner.style.display = "none";
    banner.textContent = "";
    resetBtn.style.display = "none";
    promoteBtn.textContent = "Promote Playbook";
    heading.textContent =
      "Promote Verified Procedural Atom(s) to Skill Playbook";
  }
}

document
  .getElementById("pbExpectedShaInput")
  .addEventListener("input", syncPlaybookUpdateModeUi);

document.getElementById("pbResetUpdateBtn").addEventListener("click", () => {
  document.getElementById("pbExpectedShaInput").value = "";
  syncPlaybookUpdateModeUi();
});

async function ensureBackingAtomCheckbox(atomId) {
  const wrap = document.getElementById("pbBackingSelectWrap");
  const cleanFn = atomId.endsWith(".md") ? atomId : `${atomId}.md`;
  const existing = [
    ...wrap.querySelectorAll("input[type='checkbox']"),
  ].find(
    (c) => c.dataset.atomId === atomId || c.dataset.atomId === cleanFn
  );
  if (existing) {
    existing.checked = true;
    return;
  }
  try {
    const res = await window.secondBrain.api(
      "GET",
      `/v1/atoms/${encodeURIComponent(cleanFn)}`
    );
    const atom = res.ok && res.data ? res.data.atom || res.data : null;
    if (atom && atom.sha256) {
      const chkLbl = el("label", "inline-check");
      chkLbl.style.marginRight = "14px";
      const chk = document.createElement("input");
      chk.type = "checkbox";
      chk.checked = true;
      chk.dataset.atomId = cleanFn;
      chk.dataset.sha = atom.sha256;
      chkLbl.appendChild(chk);
      chkLbl.appendChild(
        el("span", "", `${cleanFn} (${shortSha(atom.sha256)})`)
      );
      wrap.appendChild(chkLbl);
    }
  } catch {}
}

async function prefillPlaybookForUpdate(pb) {
  const prefill = PbHelper.buildPlaybookUpdatePrefill(pb);
  if (!prefill) return;

  document.getElementById("pbIdInput").value = prefill.playbookId;
  document.getElementById("pbTitleInput").value = prefill.title;
  document.getElementById("pbTriggerInput").value = prefill.trigger;
  document.getElementById("pbPreconditionsInput").value =
    prefill.preconditionsText;
  document.getElementById("pbStepsInput").value = prefill.stepsText;
  document.getElementById("pbExpectedOutputsInput").value =
    prefill.expectedOutputsText;
  document.getElementById("pbRollbackInput").value =
    prefill.failureRecoveryText;
  document.getElementById("pbExpectedShaInput").value =
    prefill.expectedPlaybookSha256;

  document
    .querySelectorAll("#pbBackingSelectWrap input[type='checkbox']")
    .forEach((chk) => {
      chk.checked = false;
    });
  for (const aid of prefill.backingAtoms) {
    await ensureBackingAtomCheckbox(aid);
  }

  syncPlaybookUpdateModeUi();
  const promoteCard = document.getElementById("playbookPromoteCard");
  if (promoteCard && typeof promoteCard.scrollIntoView === "function") {
    promoteCard.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  showToast(
    `Loaded playbook '${prefill.playbookId}' into CAS Update form`,
    "info",
    3000
  );
}

function buildPlaybookCard(pb) {
  const card = el("div", "card");
  const isActive =
    pb.valid !== false &&
    (!pb.status || pb.status === "accepted" || pb.status === "active");
  const head = el("div", "toolbar");
  head.appendChild(
    el("strong", "", pb.title || pb.playbook_id || pb.filename || "Playbook")
  );
  head.appendChild(
    el(
      "span",
      `badge ${isActive ? "ok" : "warn"}`,
      isActive
        ? "ACTIVE"
        : `INACTIVE (${pb.reason || pb.status || "deprecated"})`
    )
  );
  if (pb.sha256) {
    head.appendChild(el("span", "badge mono", `sha:${shortSha(pb.sha256)}`));
  }
  card.appendChild(head);

  card.appendChild(
    el(
      "div",
      "note-item-meta",
      `${pb.filename || pb.playbook_id || ""}${
        (pb.contexts || []).length ? " · " + pb.contexts.join(", ") : ""
      }`
    )
  );

  if (pb.trigger) {
    card.appendChild(el("div", "sub-panel", `Trigger: ${pb.trigger}`));
  }

  if ((pb.preconditions || []).length) {
    card.appendChild(
      el("div", "note-item-meta", `Preconditions: ${pb.preconditions.join("; ")}`)
    );
  }

  if ((pb.steps || []).length) {
    const ol = el("ol");
    ol.style.margin = "2px 0";
    ol.style.paddingLeft = "20px";
    ol.style.fontSize = "12.5px";
    for (const s of pb.steps) {
      ol.appendChild(el("li", "", s));
    }
    card.appendChild(ol);
  }

  if ((pb.expected_outputs || []).length) {
    card.appendChild(
      el("div", "note-item-meta", `Expected: ${pb.expected_outputs.join("; ")}`)
    );
  }
  if ((pb.failure_recovery || []).length) {
    card.appendChild(
      el("div", "note-item-meta", `Rollback: ${pb.failure_recovery.join("; ")}`)
    );
  }

  const atomsRow = el("div", "backlinks-bar");
  atomsRow.appendChild(el("span", "field-label", "Backing Atoms:"));
  for (const aid of pb.backing_atoms || []) {
    const pill = el("button", "pill clickable", aid);
    pill.type = "button";
    pill.onclick = () => openAtomInspectorModal(aid);
    atomsRow.appendChild(pill);
  }
  card.appendChild(atomsRow);

  const actRow = el("div", "toolbar");
  const editUpdateBtn = el("button", "btn sm primary", "Edit / Update");
  editUpdateBtn.type = "button";
  editUpdateBtn.onclick = () => prefillPlaybookForUpdate(pb);
  actRow.appendChild(editUpdateBtn);

  if (isActive && pb.filename) {
    const reasonInput = el("input");
    reasonInput.type = "text";
    reasonInput.placeholder = "Deprecation rationale...";
    reasonInput.style.flex = "1";
    const depBtn = el("button", "btn sm danger", "Deprecate");
    depBtn.type = "button";
    depBtn.onclick = async () => {
      const rationale =
        reasonInput.value.trim() || "Deprecated via Desktop Playbook Manager";
      const conf = await modalCtrl.confirm({
        title: "Deprecate Playbook",
        message: `Deprecate playbook "${pb.title || pb.filename}"?`,
        detail: `Rationale: ${rationale}`,
        confirmLabel: "Deprecate Playbook",
        confirmClass: "danger",
      });
      if (!conf.confirmed) return;

      const res = await window.secondBrain.api(
        "POST",
        `/v1/playbooks/${encodeURIComponent(pb.filename)}/deprecate`,
        {
          expected_sha256: pb.sha256 || "",
          actor: "desktop_owner",
          rationale,
        }
      );
      if (res.ok) {
        showToast(`Deprecated playbook ${pb.filename}`, "ok");
        loadPlannerView();
      } else {
        showToast(apiErrorMessage(res, "Deprecate playbook failed"), "danger");
      }
    };
    actRow.appendChild(reasonInput);
    actRow.appendChild(depBtn);
  }
  card.appendChild(actRow);

  return card;
}

function prefillFromCandidate(cand) {
  if (!cand) return;
  const idInput = document.getElementById("pbIdInput");
  const titleInput = document.getElementById("pbTitleInput");
  const stepsInput = document.getElementById("pbStepsInput");

  const result = PbHelper.buildPlaybookPrefillFromCandidate(cand, {
    playbookId: idInput.value,
    title: titleInput.value,
  });

  idInput.value = result.playbookId;
  titleInput.value = result.title;
  stepsInput.value = result.stepsText;

  document
    .querySelectorAll("#pbBackingSelectWrap input[type='checkbox']")
    .forEach((chk) => {
      if (chk.dataset.atomId === cand.atom_id) chk.checked = true;
    });
  showToast(
    `Pre-filled ${result.steps.length} grounding-valid step(s) from ${cand.atom_id}`,
    "ok",
    2800
  );
}

async function loadPlannerView() {
  const scope = document.getElementById("planScopeInput").value.trim();
  const pbQuery = scope
    ? `?include_inactive=1&project=${encodeURIComponent(scope)}`
    : "?include_inactive=1";

  const [pbRes, candRes, profRes] = await Promise.all([
    window.secondBrain.request("GET", `/v1/playbooks${pbQuery}`),
    window.secondBrain.request("GET", "/v1/playbooks/candidates"),
    window.secondBrain.request("GET", "/v1/profile"),
  ]);

  if (profRes.ok && profRes.data?.hosts) {
    populateHostSelectElement(
      document.getElementById("planHostSelect"),
      profRes.data.hosts
    );
  }

  const grid = document.getElementById("playbooksGrid");
  grid.replaceChildren();
  const items = pbRes.data?.playbooks || pbRes.data?.items || [];
  playbooksCache = items;
  if (!items.length) {
    grid.appendChild(
      el(
        "div",
        "status-banner",
        "No skill playbooks registered for this scope yet."
      )
    );
  } else {
    for (const pb of items) {
      grid.appendChild(buildPlaybookCard(pb));
    }
  }

  const candGrid = document.getElementById("candidatesGrid");
  const backingWrap = document.getElementById("pbBackingSelectWrap");
  candGrid.replaceChildren();
  backingWrap.replaceChildren();

  let candidates = candRes.ok
    ? candRes.data?.candidates || candRes.data?.items || []
    : [];
  if (!candidates.length) {
    const atomsRes = await window.secondBrain.api("GET", "/v1/atoms?limit=200");
    const procAtoms = (atomsRes.data?.items || []).filter(
      (a) => a.verified && a.memory_type === "procedural"
    );
    candidates = procAtoms.map((a) => ({
      atom_id: a.filename,
      claim: a.claim,
      sha256: a.sha256,
      maturity_stage: "approved_procedural",
      grounded_citations: 0,
      corrections: 0,
      eligible_without_override: false,
      already_in_active_playbook: false,
    }));
  }
  candidatesCache = candidates;

  if (!candidates.length) {
    candGrid.appendChild(
      el(
        "div",
        "status-banner",
        "No approved procedural atoms available yet. Verify a note excerpt with 'Procedural Rule' type to create one."
      )
    );
    backingWrap.textContent = "No approved procedural atoms found.";
    return;
  }

  for (const cand of candidates) {
    const cCard = el("div", "card");
    const top = el("div", "toolbar");
    top.appendChild(el("strong", "", cand.atom_id));
    top.appendChild(
      el(
        "span",
        `badge ${cand.eligible_without_override ? "ok" : "warn"}`,
        cand.maturity_stage || "candidate"
      )
    );
    if (cand.already_in_active_playbook) {
      top.appendChild(el("span", "badge", "In Playbook"));
    }
    cCard.appendChild(top);
    cCard.appendChild(el("div", "", cand.claim || ""));
    cCard.appendChild(
      el(
        "div",
        "note-item-meta",
        `Grounded citations: ${cand.grounded_citations ?? 0} · Corrections: ${
          cand.corrections ?? 0
        } · sha:${shortSha(cand.sha256)}`
      )
    );
    const fillBtn = el("button", "btn sm primary", "Select & Pre-fill Steps");
    fillBtn.type = "button";
    fillBtn.onclick = () => prefillFromCandidate(cand);
    cCard.appendChild(fillBtn);
    candGrid.appendChild(cCard);

    const chkLbl = el("label", "inline-check");
    chkLbl.style.marginRight = "14px";
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.dataset.atomId = cand.atom_id;
    chk.dataset.sha = cand.sha256 || "";
    chkLbl.appendChild(chk);
    chkLbl.appendChild(
      el("span", "", `${cand.atom_id} (${shortSha(cand.sha256)})`)
    );
    backingWrap.appendChild(chkLbl);
  }
}

function renderExecutivePlanOutput(outEl, plan) {
  outEl.replaceChildren();
  const ready = Boolean(plan.plan_ready);
  const top = el("div", "toolbar");
  top.appendChild(
    el(
      "span",
      `badge ${ready ? "ok" : "warn"}`,
      ready ? "READY (Citation-Grounded)" : "ABSTAINED"
    )
  );
  if (plan.mode_used) {
    top.appendChild(el("span", "badge mono", `mode: ${plan.mode_used}`));
  }
  for (const pbId of plan.matched_playbook_ids || []) {
    top.appendChild(el("span", "badge tier-v", `playbook:${pbId}`));
  }
  outEl.appendChild(top);

  if (plan.summary) {
    const sumEl = el("div", "", plan.summary);
    sumEl.style.marginTop = "6px";
    outEl.appendChild(sumEl);
  }

  const appendListSection = (heading, arr) => {
    if (!Array.isArray(arr) || !arr.length) return;
    const sec = el("div", "field-col");
    sec.style.marginTop = "10px";
    sec.appendChild(el("span", "field-label", heading));
    const ul = el("ul");
    ul.style.margin = "2px 0";
    ul.style.paddingLeft = "20px";
    for (const item of arr) {
      ul.appendChild(el("li", "", item));
    }
    sec.appendChild(ul);
    outEl.appendChild(sec);
  };

  appendListSection("Preconditions", plan.preconditions);

  const steps = plan.steps || [];
  if (steps.length) {
    const stepsWrap = el("div", "field-col");
    stepsWrap.style.marginTop = "10px";
    stepsWrap.style.gap = "8px";
    stepsWrap.appendChild(el("span", "field-label", "Grounded Execution Steps"));
    steps.forEach((step, idx) => {
      const sc = el("div", "step-card");
      const badges = el("div", "toolbar");
      badges.appendChild(el("strong", "", `Step ${idx + 1}`));
      if (step.kind) badges.appendChild(el("span", "badge", step.kind));
      if (step.source_playbook_id) {
        badges.appendChild(
          el("span", "badge tier-v", `playbook:${step.source_playbook_id}`)
        );
      }
      for (const aid of step.backing_atom_ids || []) {
        const p = el("button", "pill clickable", `atom:${aid}`);
        p.type = "button";
        p.onclick = () => openAtomInspectorModal(aid);
        badges.appendChild(p);
      }
      sc.appendChild(badges);
      sc.appendChild(el("div", "", step.instruction || ""));
      if ((step.citations || []).length) {
        sc.appendChild(
          el("div", "note-item-meta", `Citations: ${step.citations.join(" · ")}`)
        );
      }
      stepsWrap.appendChild(sc);
    });
    outEl.appendChild(stepsWrap);
  }

  const evidenceAtomIds = [
    ...new Set([
      ...(Array.isArray(plan.working_context_atom_ids)
        ? plan.working_context_atom_ids
        : []),
      ...steps.flatMap((st) =>
        Array.isArray(st.backing_atom_ids) ? st.backing_atom_ids : []
      ),
    ]),
  ];
  const citedAtomObjects = Array.isArray(plan.cited_atoms)
    ? plan.cited_atoms
    : Array.isArray(plan.evidence_atoms)
    ? plan.evidence_atoms
    : evidenceAtomIds.map((aid) => {
        const cleanFn = String(aid || "").endsWith(".md") ? aid : `${aid}.md`;
        const cached = approvedAtomsCache.find(
          (a) => a.filename === aid || a.filename === cleanFn
        );
        return (
          cached || {
            filename: cleanFn,
            atom_id: aid,
            claim: "Owner-verified backing atom cited by grounded plan.",
          }
        );
      });

  if (citedAtomObjects.length) {
    const evWrap = el("div", "field-col");
    evWrap.style.marginTop = "10px";
    evWrap.style.gap = "6px";
    evWrap.appendChild(el("span", "field-label", "Cited Atom Evidence"));
    for (const ea of citedAtomObjects) {
      const fn = ea.filename || ea.atom_id || "";
      const card = el("div", "claim-card");
      const row = el("div", "toolbar");
      row.appendChild(el("span", "badge tier-v", "Verified Evidence"));
      if (fn) {
        const btn = el("button", "pill clickable", fn);
        btn.type = "button";
        btn.onclick = () => openAtomInspectorModal(fn, ea);
        row.appendChild(btn);
      }
      if (ea.sha256) {
        row.appendChild(el("span", "badge mono", `sha:${shortSha(ea.sha256)}`));
      }
      card.appendChild(row);
      if (ea.claim) {
        card.appendChild(el("div", "", ea.claim));
      }
      if (ea.evidence || ea.quote) {
        card.appendChild(
          el("div", "claim-quote", `"${ea.evidence || ea.quote}"`)
        );
      }
      evWrap.appendChild(card);
    }
    outEl.appendChild(evWrap);
  }

  appendListSection("Expected Outputs & Verification", plan.expected_outputs);
  appendListSection("Rollback Checkpoints", plan.rollback_checkpoints);
  appendListSection("Owner Preference Guardrails", plan.preference_constraints);
  appendListSection(
    "Missing Gaps & Epistemic Notices",
    plan.missing_gaps || plan.epistemic_gaps
  );
}

async function runExecutivePlan() {
  const goal = document.getElementById("planGoalInput").value.trim();
  const banner = document.getElementById("plannerBanner");
  const out = document.getElementById("planOutput");
  if (!goal) {
    banner.textContent = "Enter a goal to assemble an Executive Plan.";
    showToast("Enter a task goal first.", "warn");
    return;
  }
  const project_scope =
    document.getElementById("planScopeInput").value.trim() || undefined;
  const active_context_profile = parseTags(
    document.getElementById("planContextProfileInput").value
  );
  const mode = document.getElementById("planModeSelect").value;
  const host_id =
    document.getElementById("planHostSelect").value || "ollama_local";
  const include_preferences = Boolean(
    document.getElementById("planIncludePrefsCheck").checked
  );

  banner.textContent = "Assembling grounded executive plan...";
  try {
    const res = await window.secondBrain.request("POST", "/v1/plan", {
      goal,
      project_scope,
      active_context_profile,
      mode,
      host_id,
      include_preferences,
    });
    if (res.ok && res.data) {
      banner.textContent = res.data.plan_ready
        ? `Plan assembled (${res.data.mode_used || mode}) — ${
            (res.data.steps || []).length
          } grounded step(s).`
        : "Abstained: insufficient verified playbooks/atoms for this goal.";
      renderExecutivePlanOutput(out, res.data);
    } else {
      const errMsg = apiErrorMessage(res, "Executive plan failed");
      banner.textContent = errMsg;
      out.replaceChildren(el("div", "status-banner banner-danger", errMsg));
      showToast(errMsg, "danger");
    }
  } catch (err) {
    const errMsg = `Planner error: ${err?.message || String(err)}`;
    banner.textContent = errMsg;
    showToast(errMsg, "danger");
  }
}

document
  .getElementById("prefillStepsFromCandidateBtn")
  .addEventListener("click", () => {
    const checked = document.querySelector(
      "#pbBackingSelectWrap input[type='checkbox']:checked"
    );
    const targetId = checked?.dataset.atomId || candidatesCache[0]?.atom_id;
    const cand = candidatesCache.find((c) => c.atom_id === targetId);
    if (!cand) {
      showToast("No procedural candidate available to pre-fill from.", "warn");
      return;
    }
    prefillFromCandidate(cand);
  });

document
  .getElementById("promotePlaybookBtn")
  .addEventListener("click", async () => {
    const checkedBoxes = [
      ...document.querySelectorAll(
        "#pbBackingSelectWrap input[type='checkbox']:checked"
      ),
    ];
    const backing_atoms = checkedBoxes.map((c) => [
      c.dataset.atomId,
      c.dataset.sha,
    ]);
    const playbook_id = document.getElementById("pbIdInput").value.trim();
    const title = document.getElementById("pbTitleInput").value.trim();
    const trigger = document.getElementById("pbTriggerInput").value.trim();
    const steps = parseLines(document.getElementById("pbStepsInput").value);
    const expected_playbook_sha256 = document
      .getElementById("pbExpectedShaInput")
      .value.trim();

    if (
      !backing_atoms.length ||
      !playbook_id ||
      !title ||
      !trigger ||
      !steps.length
    ) {
      showToast(
        "Select at least one backing atom and fill Playbook ID, Title, Trigger, and Steps.",
        "warn"
      );
      return;
    }

    const payload = PbHelper.buildPlaybookPromotePayload({
      playbook_id,
      title,
      backing_atoms,
      trigger,
      preconditions: parseLines(
        document.getElementById("pbPreconditionsInput").value
      ),
      steps,
      expected_outputs: parseLines(
        document.getElementById("pbExpectedOutputsInput").value
      ),
      failure_recovery: parseLines(
        document.getElementById("pbRollbackInput").value
      ),
      actor: "desktop_owner",
      rationale:
        document.getElementById("pbRationaleInput").value.trim() ||
        (expected_playbook_sha256
          ? "Owner updated playbook via Desktop App"
          : "Owner promoted via Desktop App"),
      allow_early_owner_override: Boolean(
        document.getElementById("pbAllowEarlyCheck").checked
      ),
      acknowledge_corrections: Boolean(
        document.getElementById("pbAckCorrectionsCheck").checked
      ),
      allow_multi_project: Boolean(
        document.getElementById("pbAllowMultiProjCheck").checked
      ),
      expected_playbook_sha256,
    });

    const res = await window.secondBrain.api(
      "POST",
      "/v1/playbooks/promote",
      payload
    );

    if (res.ok) {
      const wasUpdate = Boolean(expected_playbook_sha256);
      document.getElementById("pbExpectedShaInput").value = "";
      syncPlaybookUpdateModeUi();
      showToast(
        wasUpdate
          ? `Updated Skill Playbook: ${title} (sha:${shortSha(res.data?.sha256)})`
          : `Promoted Skill Playbook: ${title}`,
        "ok"
      );
      loadPlannerView();
    } else {
      const errMsg = apiErrorMessage(res, "Playbook promotion failed");
      if (res.status === 409) {
        const banner = document.getElementById("pbUpdateModeBanner");
        banner.style.display = "block";
        banner.className = "status-banner banner-danger";
        banner.textContent = `${errMsg} — Click 'Refresh Playbooks' and 'Edit / Update' to load the latest SHA-256.`;
      }
      showToast(errMsg, "danger");
    }
  });

document
  .getElementById("runPlanBtn")
  .addEventListener("click", runExecutivePlan);
document
  .getElementById("refreshPlaybooksBtn")
  .addEventListener("click", () => {
    loadPlannerView();
    loadExecutableSkills();
  });

// ─── TAB 8: Merkle Ledger, History, Leaves, Source Audit, Radar & Snapshots ──

function renderLedgerCheckpointHistoryTable(entries) {
  const wrap = document.getElementById("ledgerHistoryWrap");
  if (!wrap) return;
  wrap.replaceChildren();
  if (!Array.isArray(entries) || !entries.length) {
    wrap.appendChild(
      el("div", "note-item-meta", "No signed Merkle checkpoints committed yet.")
    );
    return;
  }

  const title = el(
    "div",
    "field-label",
    `Signed Checkpoint History (${entries.length}):`
  );
  title.style.marginBottom = "4px";
  wrap.appendChild(title);

  const tbl = el("table", "data-table");
  const thead = el("thead");
  const htr = el("tr");
  for (const col of [
    "Seq",
    "Timestamp",
    "Actor",
    "Note",
    "Leaves (Atoms / Playbooks)",
    "Merkle Root",
    "Entry SHA-256",
  ]) {
    htr.appendChild(el("th", "", col));
  }
  thead.appendChild(htr);
  tbl.appendChild(thead);

  const tbody = el("tbody");
  for (const entry of entries) {
    const tr = el("tr");
    tr.appendChild(el("td", "kpi-sub", `#${entry.seq ?? "?"}`));
    tr.appendChild(el("td", "note-item-meta", entry.timestamp || entry.created_at || ""));
    tr.appendChild(el("td", "", entry.actor || "owner"));
    tr.appendChild(el("td", "", entry.note || "—"));
    tr.appendChild(
      el(
        "td",
        "",
        `${entry.leaf_count ?? 0} (${entry.approved_atoms ?? 0} / ${
          entry.accepted_playbooks ?? 0
        })`
      )
    );
    tr.appendChild(el("td", "kpi-sub", shortSha(entry.merkle_root)));
    tr.appendChild(el("td", "kpi-sub", shortSha(entry.entry_sha256)));
    tbody.appendChild(tr);
  }
  tbl.appendChild(tbody);
  wrap.appendChild(tbl);
}

async function loadLedgerHistoryPage({ append = false, fallbackEntries = [] } = {}) {
  if (!append) {
    ledgerHistoryPaginator.reset();
  }
  const histUrl = PageHelper.buildPaginatedPath("/v1/ledger/history", {
    limit: ledgerHistoryPaginator.getPageSize(),
    offset: ledgerHistoryPaginator.nextOffset(),
  });
  const histRes = await window.secondBrain.request("GET", histUrl);
  if (histRes.ok && histRes.data) {
    ledgerHistoryPaginator.applyPage(histRes.data, {
      append,
      preferredKey: "entries",
    });
  } else if (!append && Array.isArray(fallbackEntries) && fallbackEntries.length) {
    ledgerHistoryPaginator.applyPage(
      {
        items: fallbackEntries,
        total: fallbackEntries.length,
        offset: 0,
      },
      { append: false }
    );
  }

  renderLedgerCheckpointHistoryTable(ledgerHistoryPaginator.getItems());
  PageHelper.renderPaginationBar(
    document.getElementById("ledgerHistoryPaginationBar"),
    ledgerHistoryPaginator,
    () => loadLedgerHistoryPage({ append: true }),
    { noun: "checkpoints" }
  );
}

async function loadLedgerLeavesPage({ append = false } = {}) {
  const atomSel = document.getElementById("proveAtomSelect");
  const prevVal = atomSel.value;
  if (!append) {
    ledgerLeavesPaginator.reset();
  }

  const leavesUrl = PageHelper.buildPaginatedPath("/v1/ledger/leaves", {
    limit: ledgerLeavesPaginator.getPageSize(),
    offset: ledgerLeavesPaginator.nextOffset(),
  });
  const leavesRes = await window.secondBrain.request("GET", leavesUrl);

  if (leavesRes.ok && leavesRes.data) {
    ledgerLeavesPaginator.applyPage(leavesRes.data, {
      append,
      preferredKey: "items",
    });
  } else if (!append) {
    const [atomsRes, pbRes] = await Promise.all([
      window.secondBrain.request("GET", "/v1/atoms?limit=200"),
      window.secondBrain.request("GET", "/v1/playbooks"),
    ]);
    const atomLeaves = (atomsRes.data?.items || [])
      .filter((a) => a.verified || a.review_status === "accepted")
      .map((a) => ({ rel_path: `2-atoms/${a.filename}`, kind: "atom" }));
    const pbLeaves = (pbRes.data?.playbooks || pbRes.data?.items || [])
      .filter((p) => p.valid !== false)
      .map((p) => ({
        rel_path: `5-playbooks/${p.filename || `${p.playbook_id}.md`}`,
        kind: "playbook",
      }));
    const combined = [...atomLeaves, ...pbLeaves];
    ledgerLeavesPaginator.applyPage(
      { items: combined, total: combined.length, offset: 0 },
      { append: false }
    );
  }

  atomSel.replaceChildren();
  const placeholderOpt = el(
    "option",
    "",
    "-- Select approved atom or playbook leaf --"
  );
  placeholderOpt.value = "";
  atomSel.appendChild(placeholderOpt);

  for (const rawLeaf of ledgerLeavesPaginator.getItems()) {
    const norm = PageHelper.normalizeLedgerLeafOption(rawLeaf);
    if (!norm) continue;
    const opt = el("option", "", norm.label);
    opt.value = norm.value;
    atomSel.appendChild(opt);
  }
  if (prevVal && [...atomSel.options].some((o) => o.value === prevVal)) {
    atomSel.value = prevVal;
  }

  const moreBtn = document.getElementById("loadMoreLeavesBtn");
  if (moreBtn) {
    moreBtn.style.display = ledgerLeavesPaginator.canLoadMore()
      ? "inline-block"
      : "none";
  }
}

async function loadLedgerStatus() {
  const kpiGrid = document.getElementById("ledgerKpiGrid");
  const rootsBar = document.getElementById("ledgerRootsBar");

  const [res] = await Promise.all([
    window.secondBrain.request("GET", "/v1/ledger/verify"),
    loadLedgerLeavesPage({ append: false }),
  ]);

  kpiGrid.replaceChildren();
  rootsBar.replaceChildren();
  if (!res.ok || !res.data) {
    rootsBar.textContent = apiErrorMessage(
      res,
      "Failed to verify Merkle ledger"
    );
    return;
  }
  const d = res.data;
  const valid = Boolean(d.chain_valid);
  const synced = Boolean(d.synced_with_head);
  kpiGrid.appendChild(
    makeKpiCard(
      "Chain Valid",
      valid ? "VALID" : "TAMPERED",
      `Head #${d.head_seq ?? 0}`,
      valid ? "ok" : "danger"
    )
  );
  kpiGrid.appendChild(makeKpiCard("Checkpoints", d.entry_count ?? 0));
  kpiGrid.appendChild(
    makeKpiCard("Live Leaves", d.live_leaf_count ?? d.leaf_count ?? 0)
  );
  kpiGrid.appendChild(makeKpiCard("Approved Atoms", d.approved_atoms ?? 0));
  kpiGrid.appendChild(makeKpiCard("Playbooks", d.accepted_playbooks ?? 0));
  kpiGrid.appendChild(
    makeKpiCard(
      "Synced w/ Head",
      synced ? "SYNCED" : "UNCOMMITTED",
      d.entry_count === 0 ? "Commit initial checkpoint" : "",
      synced ? "ok" : "warn"
    )
  );

  const rootRow = el("div", "toolbar");
  rootRow.appendChild(el("span", "field-label", "Live Merkle Root:"));
  rootRow.appendChild(el("span", "badge mono", d.live_merkle_root || "—"));
  rootRow.appendChild(el("span", "field-label", "Head Checkpoint Root:"));
  rootRow.appendChild(
    el("span", "badge mono", d.head_merkle_root || "(no checkpoints yet)")
  );
  rootsBar.appendChild(rootRow);

  if (Array.isArray(d.errors) && d.errors.length) {
    for (const err of d.errors) {
      rootsBar.appendChild(el("div", "status-banner banner-danger", err));
    }
  }

  await loadLedgerHistoryPage({
    append: false,
    fallbackEntries: Array.isArray(d.entries) ? d.entries : [],
  });
}

document
  .getElementById("loadMoreLeavesBtn")
  .addEventListener("click", () => loadLedgerLeavesPage({ append: true }));

document.getElementById("proveAtomSelect").addEventListener("change", (e) => {
  if (e.target.value) {
    document.getElementById("proveAtomInput").value = e.target.value;
  }
});

// Source Audit & 1-Click Demote Stale Approvals
async function loadSourceAudit() {
  const sumEl = document.getElementById("sourceAuditSummary");
  const tableWrap = document.getElementById("sourceAuditTableWrap");
  const demoteBtn = document.getElementById("demoteStaleSourcesBtn");
  if (!sumEl || !tableWrap) return;

  const res = await window.secondBrain.request("GET", "/v1/audit/sources");
  tableWrap.replaceChildren();
  if (!res.ok || !res.data) {
    sumEl.className = "status-banner banner-danger";
    sumEl.textContent = apiErrorMessage(res, "Source anchor audit failed");
    if (demoteBtn) demoteBtn.disabled = true;
    return;
  }

  const reports = res.data.reports || res.data.items || [];
  const staleCount = Number(
    res.data.stale_count ?? reports.filter((r) => r.stale_approval).length
  );

  if (demoteBtn) {
    demoteBtn.disabled = staleCount === 0;
    demoteBtn.textContent =
      staleCount > 0
        ? `Demote Stale Approvals (${staleCount})`
        : "Demote Stale Approvals (0 stale)";
  }

  sumEl.className = `status-banner ${
    staleCount > 0 ? "banner-warn" : "banner-ok"
  }`;
  sumEl.textContent =
    staleCount > 0
      ? `Detected ${staleCount} stale source-anchored approval(s) across ${reports.length} audited atom(s). Review and click 'Demote Stale Approvals' to re-gate.`
      : `All ${reports.length} audited atom source anchors are current (0 stale approvals).`;

  const staleOrInteresting = reports.filter(
    (r) => r.stale_approval || (r.action && r.action !== "ok" && r.action !== "none")
  );
  const displayRows = staleOrInteresting.length
    ? staleOrInteresting
    : reports.slice(0, 15);
  if (!displayRows.length) return;

  const tbl = el("table", "data-table");
  const thead = el("thead");
  const htr = el("tr");
  for (const col of ["Atom", "Stale Approval", "Status / Action", "Inspect"]) {
    htr.appendChild(el("th", "", col));
  }
  thead.appendChild(htr);
  tbl.appendChild(thead);

  const tbody = el("tbody");
  for (const rep of displayRows) {
    const fn = rep.filename || rep.atom_id || "";
    const tr = el("tr");
    tr.appendChild(el("td", "kpi-sub", fn));
    tr.appendChild(
      el(
        "td",
        "",
        rep.stale_approval ? "STALE (Drift Detected)" : "Valid"
      )
    );
    tr.appendChild(
      el(
        "td",
        "note-item-meta",
        rep.action || rep.reason || rep.status || "verified"
      )
    );
    const actTd = el("td");
    if (fn) {
      const btn = el("button", "btn sm", "Inspect");
      btn.type = "button";
      btn.onclick = () => openAtomInspectorModal(fn);
      actTd.appendChild(btn);
    }
    tr.appendChild(actTd);
    tbody.appendChild(tr);
  }
  tbl.appendChild(tbody);
  tableWrap.appendChild(tbl);
}

document
  .getElementById("refreshSourceAuditBtn")
  .addEventListener("click", loadSourceAudit);

document
  .getElementById("demoteStaleSourcesBtn")
  .addEventListener("click", async () => {
    const defaultActor =
      document.getElementById("auditActorInput").value.trim() || "desktop_owner";
    const conf = await modalCtrl.confirm({
      title: "Demote Stale Source Approvals",
      message:
        "Demote all approved atoms whose source evidence byte-offsets or SHA-256 anchors have drifted? Demoted atoms return to the owner review queue.",
      confirmLabel: "Demote Stale Approvals",
      confirmClass: "warn",
      actorDefault: defaultActor,
      actorLabel: "Owner Actor (recorded in immutable audit log)",
    });
    if (!conf.confirmed) return;

    const actor = conf.actor || defaultActor;
    document.getElementById("auditActorInput").value = actor;
    const res = await window.secondBrain.api(
      "POST",
      "/v1/audit/sources/demote-stale",
      { actor }
    );
    if (res.ok && res.data) {
      const count = res.data.demoted_count ?? (res.data.demoted || []).length;
      showToast(`Demoted ${count} stale approval(s)`, "ok");
      await Promise.all([loadSourceAudit(), loadLedgerStatus(), refreshHealth()]);
    } else {
      showToast(apiErrorMessage(res, "Demote stale approvals failed"), "danger");
    }
  });

const HEX64_RE = /^[0-9a-f]{64}$/;

async function sha256HexWebCrypto(text) {
  const bytes = new TextEncoder().encode(String(text));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const arr = new Uint8Array(digest);
  let hex = "";
  for (let i = 0; i < arr.length; i += 1) {
    hex += arr[i].toString(16).padStart(2, "0");
  }
  return hex;
}

async function verifyMerkleProofWebCrypto(proof) {
  const trace = [];
  if (!proof || typeof proof !== "object") {
    return { valid: false, reason: "Proof bundle must be a JSON object.", trace };
  }
  const relPath = String(proof.rel_path || "");
  const fileSha = String(proof.file_sha256 || "").toLowerCase();
  const leafSha = String(proof.leaf_sha256 || "").toLowerCase();
  const expectedRoot = String(proof.merkle_root || "").toLowerCase();
  const siblings =
    proof.siblings === undefined || proof.siblings === null
      ? []
      : proof.siblings;

  if (
    !relPath ||
    !HEX64_RE.test(fileSha) ||
    !HEX64_RE.test(leafSha) ||
    !HEX64_RE.test(expectedRoot) ||
    !Array.isArray(siblings)
  ) {
    return {
      valid: false,
      reason: "Proof bundle has missing or malformed SHA-256 hex fields.",
      trace,
    };
  }

  const schemes = [
    {
      leafPrefix: "leaf:",
      nodePrefix: "node:",
      oddPrefix: "odd:",
    },
    {
      leafPrefix: "second-brain:leaf:v1:",
      nodePrefix: "second-brain:node:v1:",
      oddPrefix: "second-brain:odd:v1:",
    },
  ];

  let chosenScheme = null;
  let computedLeaf = "";
  for (const sc of schemes) {
    const cand = await sha256HexWebCrypto(
      `${sc.leafPrefix}${relPath}:${fileSha}`
    );
    if (cand === leafSha) {
      chosenScheme = sc;
      computedLeaf = cand;
      break;
    }
  }
  if (!chosenScheme) {
    computedLeaf = await sha256HexWebCrypto(`leaf:${relPath}:${fileSha}`);
    trace.push(
      `Leaf MISMATCH: SHA256("leaf:${relPath}:${shortSha(
        fileSha
      )}…") = ${shortSha(computedLeaf)} != ${shortSha(leafSha)}`
    );
    return { valid: false, reason: "Leaf preimage digest mismatch.", trace };
  }

  trace.push(
    `Step 0 (Leaf): SHA256("${chosenScheme.leafPrefix}${relPath}:${shortSha(
      fileSha
    )}…") -> ${leafSha}`
  );

  let cur = leafSha;
  for (let i = 0; i < siblings.length; i += 1) {
    const step = siblings[i];
    if (!step || typeof step !== "object") {
      return { valid: false, reason: `Malformed sibling at level ${i}.`, trace };
    }
    const pos = String(step.position || "");
    const sib = String(step.sha256 || "").toLowerCase();
    if (!HEX64_RE.test(sib)) {
      return {
        valid: false,
        reason: `Invalid sibling hash at level ${i}.`,
        trace,
      };
    }
    if (pos === "right") {
      const next = await sha256HexWebCrypto(
        `${chosenScheme.nodePrefix}${cur}:${sib}`
      );
      trace.push(
        `Level ${i} (pair w/ right ${shortSha(sib)}): SHA256("${
          chosenScheme.nodePrefix
        }${shortSha(cur)}:${shortSha(sib)}") -> ${next}`
      );
      cur = next;
    } else if (pos === "left") {
      const next = await sha256HexWebCrypto(
        `${chosenScheme.nodePrefix}${sib}:${cur}`
      );
      trace.push(
        `Level ${i} (pair w/ left ${shortSha(sib)}): SHA256("${
          chosenScheme.nodePrefix
        }${shortSha(sib)}:${shortSha(cur)}") -> ${next}`
      );
      cur = next;
    } else if (pos === "odd") {
      if (sib !== cur) {
        trace.push(
          `Level ${i} (odd duplicate MISMATCH): sibling ${shortSha(
            sib
          )} != current ${shortSha(cur)}`
        );
        return {
          valid: false,
          reason: `Odd node mismatch at level ${i}.`,
          trace,
        };
      }
      const next = await sha256HexWebCrypto(`${chosenScheme.oddPrefix}${cur}`);
      trace.push(
        `Level ${i} (odd promotion): SHA256("${chosenScheme.oddPrefix}${shortSha(
          cur
        )}") -> ${next}`
      );
      cur = next;
    } else {
      return { valid: false, reason: `Unknown sibling position '${pos}'.`, trace };
    }
  }

  const rootMatched = cur === expectedRoot;
  trace.push(
    rootMatched
      ? `Root Match ✓: computed ${cur} === merkle_root`
      : `Root MISMATCH: computed ${cur} !== ${expectedRoot}`
  );
  return {
    valid: rootMatched,
    computedRoot: cur,
    reason: rootMatched ? "Verified" : "Computed Merkle root did not match.",
    trace,
  };
}

function buildAuditTraceList(trace) {
  const ol = el("ol", "audit-path-list");
  for (const stepText of trace || []) {
    ol.appendChild(el("li", "audit-step", stepText));
  }
  return ol;
}

async function renderAtomProofCard(proof) {
  const area = document.getElementById("proofResultArea");
  area.replaceChildren();
  const box = el("div", "sub-panel");
  const webCheck = await verifyMerkleProofWebCrypto(proof);

  const head = el("div", "toolbar");
  head.appendChild(el("span", "badge ok", "Merkle Inclusion Proof Generated"));
  head.appendChild(
    el(
      "span",
      `badge ${webCheck.valid ? "ok" : "danger"}`,
      webCheck.valid
        ? "Offline WebCrypto Proof Verified ✓"
        : `WebCrypto Verification Failed (${webCheck.reason})`
    )
  );
  head.appendChild(el("span", "badge mono", proof.rel_path || ""));
  const copyBtn = el("button", "btn sm", "Copy Proof JSON");
  copyBtn.type = "button";
  copyBtn.onclick = async () => {
    const raw = JSON.stringify(proof, null, 2);
    try {
      await navigator.clipboard.writeText(raw);
      showToast("Copied portable Merkle proof JSON to clipboard", "ok");
    } catch {
      showToast("Loaded proof into Offline Verifier box below", "info");
    }
    document.getElementById("verifyProofTextarea").value = raw;
  };
  head.appendChild(copyBtn);
  box.appendChild(head);

  const hashes = el("div", "note-item-meta");
  hashes.style.marginTop = "6px";
  hashes.textContent = `File SHA-256: ${proof.file_sha256 || ""} · Leaf SHA-256: ${
    proof.leaf_sha256 || ""
  } · Merkle Root: ${proof.merkle_root || ""}`;
  box.appendChild(hashes);

  if (webCheck.trace && webCheck.trace.length) {
    const traceTitle = el(
      "div",
      "field-label",
      "Step-by-Step WebCrypto SHA-256 Audit Path:"
    );
    traceTitle.style.marginTop = "8px";
    box.appendChild(traceTitle);
    box.appendChild(buildAuditTraceList(webCheck.trace));
  }

  document.getElementById("verifyProofTextarea").value = JSON.stringify(
    proof,
    null,
    2
  );
  area.appendChild(box);
}

async function quickRadarAtomTransition(filename, action) {
  if (!filename) return;
  const getRes = await window.secondBrain.api(
    "GET",
    `/v1/atoms/${encodeURIComponent(filename)}`
  );
  let sha = getRes.data?.sha256 || getRes.data?.atom?.sha256 || "";
  if (!sha) {
    sha =
      allLoadedAtoms.find((a) => a.filename === filename)?.sha256 || "";
  }
  if (!sha) {
    showToast(`Could not resolve CAS sha256 for ${filename}`, "danger");
    return;
  }
  const res = await window.secondBrain.api(
    "POST",
    `/v1/atoms/${encodeURIComponent(filename)}/${action}`,
    { expected_sha256: sha, actor: "desktop_owner" }
  );
  if (res.ok) {
    showToast(`${action} applied to ${filename}`, "ok");
    scanContradictions();
    loadLedgerStatus();
    refreshHealth();
  } else {
    showToast(
      apiErrorMessage(res, `Failed to ${action} ${filename}`),
      "danger"
    );
  }
}

async function openGraphNeighborsModal(filenameA, filenameB = null) {
  const targets = [filenameA, filenameB].filter(Boolean);
  if (!targets.length) return;
  modalCtrl.open({
    title: `Graph Neighbors: ${targets.join(" & ")}`,
    renderBody(body) {
      body.appendChild(
        el("div", "status-banner", "Loading knowledge graph neighborhood...")
      );
    },
  });

  const responses = await Promise.all(
    targets.map((fn) =>
      window.secondBrain.api(
        "GET",
        `/v1/graph/neighbors/${encodeURIComponent(fn)}?include_pending=1`
      )
    )
  );

  modalBodyEl.replaceChildren();
  targets.forEach((fn, idx) => {
    const res = responses[idx];
    const sec = el("div", "sub-panel");
    sec.appendChild(el("strong", "", `Neighborhood for ${fn}`));
    if (!res.ok || !res.data) {
      sec.appendChild(
        el(
          "div",
          "note-item-meta",
          apiErrorMessage(res, "Could not load neighbors")
        )
      );
      modalBodyEl.appendChild(sec);
      return;
    }
    const edges = res.data.edges || [];
    const neighbors = res.data.neighbors || [];
    sec.appendChild(
      el(
        "div",
        "note-item-meta",
        `${edges.length} edge(s) · ${neighbors.length} connected node(s)`
      )
    );
    if (!edges.length && !neighbors.length) {
      sec.appendChild(
        el("div", "note-item-meta", "No graph edges connected to this atom.")
      );
    } else {
      const bar = el("div", "backlinks-bar");
      bar.style.marginTop = "6px";
      for (const e of edges.slice(0, 20)) {
        const desc = `${e.from || ""} --[${
          e.kind || e.relation || "edge"
        }]--> ${e.to || ""}`;
        bar.appendChild(el("span", "badge mono", desc));
      }
      for (const nb of neighbors.slice(0, 20)) {
        const nbId = typeof nb === "string" ? nb : nb.id || nb.filename || "";
        if (!nbId) continue;
        const btn = el("button", "pill clickable", nbId);
        btn.type = "button";
        btn.onclick = () => {
          if (nbId.endsWith(".md")) openAtomInspectorModal(nbId);
        };
        bar.appendChild(btn);
      }
      sec.appendChild(bar);
    }
    modalBodyEl.appendChild(sec);
  });

  const doneBtn = el("button", "btn primary", "Close");
  doneBtn.type = "button";
  doneBtn.onclick = closeModal;
  modalFooterEl.replaceChildren(doneBtn);
}

async function scanContradictions() {
  const scope = document.getElementById("radarScopeInput").value.trim();
  const incPending = Boolean(
    document.getElementById("radarIncludePendingCheck").checked
  );
  const enableNli = Boolean(
    document.getElementById("radarEnableNliCheck").checked
  );
  const params = [`include_pending=${incPending ? "1" : "0"}`];
  if (enableNli) params.push("nli=1");
  if (scope) params.push(`project=${encodeURIComponent(scope)}`);

  const [radarRes, summaryRes] = await Promise.all([
    window.secondBrain.request(
      "GET",
      `/v1/graph/contradictions?${params.join("&")}`
    ),
    window.secondBrain.request(
      "GET",
      `/v1/graph/summary${scope ? "?project=" + encodeURIComponent(scope) : ""}`
    ),
  ]);

  const kpiGrid = document.getElementById("radarKpiGrid");
  const grid = document.getElementById("radarGrid");
  const driftGrid = document.getElementById("radarDriftGrid");
  const orphansBar = document.getElementById("radarOrphansBar");

  kpiGrid.replaceChildren();
  grid.replaceChildren();
  driftGrid.replaceChildren();
  orphansBar.replaceChildren();

  const d = radarRes.data || {};
  const alerts = d.alerts || [];
  const driftAlerts = d.drift_alerts || [];
  const orphans = d.orphan_approved_atoms || [];

  kpiGrid.appendChild(
    makeKpiCard("Approved Graph Nodes", summaryRes.data?.node_count ?? 0)
  );
  kpiGrid.appendChild(
    makeKpiCard("Knowledge Edges", summaryRes.data?.edge_count ?? 0)
  );
  kpiGrid.appendChild(
    makeKpiCard(
      "Contradictions",
      d.contradiction_count ?? alerts.length,
      "",
      alerts.length ? "warn" : "ok"
    )
  );
  kpiGrid.appendChild(
    makeKpiCard(
      "High Severity",
      d.high_severity_count ?? 0,
      "",
      (d.high_severity_count || 0) > 0 ? "danger" : "ok"
    )
  );
  kpiGrid.appendChild(
    makeKpiCard(
      "Source Anchor Drift",
      d.drift_count ?? driftAlerts.length,
      "",
      driftAlerts.length ? "danger" : "ok"
    )
  );
  kpiGrid.appendChild(
    makeKpiCard(
      "Expired Validity",
      d.expired_count ?? (d.expired_atoms || []).length,
      "",
      (d.expired_count || 0) > 0 ? "warn" : "ok"
    )
  );
  kpiGrid.appendChild(
    makeKpiCard("Orphan Approved", d.orphan_approved_count ?? orphans.length)
  );
  if (d.nli_status || enableNli) {
    kpiGrid.appendChild(
      makeKpiCard(
        "Local NLI",
        String(d.nli_status || "disabled").toUpperCase(),
        `${d.nli_evaluated_pairs ?? 0} pair(s) evaluated`,
        d.nli_status === "completed" ? "ok" : "warn"
      )
    );
  }

  if (!alerts.length) {
    grid.appendChild(
      el(
        "div",
        "status-banner banner-ok",
        "0 polarity or numeric contradictions detected in selected scope."
      )
    );
  } else {
    for (const al of alerts) {
      const card = el("div", "card");
      const top = el("div", "toolbar");
      const sev = String(al.severity || "medium").toLowerCase();
      top.appendChild(
        el(
          "span",
          `badge ${sev === "high" ? "danger" : "warn"}`,
          `${sev.toUpperCase()} CONFLICT`
        )
      );
      for (const r of al.reasons || []) {
        top.appendChild(el("span", "badge", r));
      }
      for (const cat of al.exclusive_categories || []) {
        top.appendChild(el("span", "badge warn", `exclusive:${cat}`));
      }
      if (al.nli_adjudication && al.nli_adjudication.verdict) {
        const v = String(al.nli_adjudication.verdict);
        top.appendChild(
          el(
            "span",
            `badge ${v === "contradiction" ? "danger" : v === "compatible" ? "ok" : "warn"}`,
            `NLI:${v}`
          )
        );
      }
      card.appendChild(top);
      if (al.nli_adjudication && al.nli_adjudication.rationale) {
        card.appendChild(
          el("div", "note-item-meta", `NLI Rationale: ${al.nli_adjudication.rationale}`)
        );
      }

      if ((al.shared_terms || []).length) {
        card.appendChild(
          el(
            "div",
            "note-item-meta",
            `Shared terms: ${al.shared_terms.join(", ")}`
          )
        );
      }

      for (const [label, side] of [
        ["Atom A", al.atom_a],
        ["Atom B", al.atom_b],
      ]) {
        if (!side) continue;
        const box = el("div", "sub-panel");
        box.appendChild(
          el(
            "strong",
            "",
            `${label}: ${side.filename || ""} (${side.review_status || ""})`
          )
        );
        box.appendChild(el("div", "", side.claim || ""));
        const btnRow = el("div", "toolbar");
        btnRow.style.marginTop = "6px";
        const qBtn = el("button", "btn sm warn", `Quarantine ${label}`);
        qBtn.type = "button";
        qBtn.onclick = () =>
          quickRadarAtomTransition(side.filename, "quarantine");
        const dBtn = el("button", "btn sm", `Demote ${label}`);
        dBtn.type = "button";
        dBtn.onclick = () => quickRadarAtomTransition(side.filename, "demote");
        const nBtn = el("button", "btn sm", "Inspect Neighbors");
        nBtn.type = "button";
        nBtn.onclick = () =>
          openGraphNeighborsModal(
            side.filename,
            label === "Atom A" ? al.atom_b?.filename : al.atom_a?.filename
          );
        btnRow.appendChild(qBtn);
        btnRow.appendChild(dBtn);
        btnRow.appendChild(nBtn);
        box.appendChild(btnRow);
        card.appendChild(box);
      }
      grid.appendChild(card);
    }
  }

  if (driftAlerts.length) {
    const driftTitle = el("div", "field-label", "Source Anchor Drift Alerts:");
    driftTitle.style.marginBottom = "6px";
    driftGrid.appendChild(driftTitle);
    for (const dr of driftAlerts) {
      driftGrid.appendChild(
        el(
          "div",
          "diff-card removed",
          `${dr.filename || dr.atom_id || "Atom"}: ${
            dr.reason || dr.detail || "Source evidence byte-anchor drifted"
          }`
        )
      );
    }
  }

  orphansBar.appendChild(
    el("span", "field-label", `Orphan Approved Atoms (${orphans.length}):`)
  );
  if (!orphans.length) {
    orphansBar.appendChild(el("span", "note-item-meta", "none"));
  } else {
    for (const fn of orphans) {
      const p = el("button", "pill clickable", fn);
      p.type = "button";
      p.onclick = () => openAtomInspectorModal(fn);
      orphansBar.appendChild(p);
    }
  }
}

async function loadSnapshotsList() {
  const res = await window.secondBrain.request("GET", "/v1/snapshots");
  const fromSel = document.getElementById("snapshotSelect");
  const toSel = document.getElementById("snapshotToSelect");
  const tableWrap = document.getElementById("snapshotsTableWrap");

  fromSel.replaceChildren();
  toSel.replaceChildren();
  tableWrap.replaceChildren();

  const liveOpt = el("option", "", "live (Current Live Vault)");
  liveOpt.value = "live";
  toSel.appendChild(liveOpt);

  snapshotsCache = res.data?.snapshots || res.data?.items || [];
  if (!snapshotsCache.length) {
    tableWrap.appendChild(
      el("div", "status-banner", "No vault snapshots recorded yet.")
    );
    return;
  }

  const tbl = el("table", "data-table");
  const thead = el("thead");
  const htr = el("tr");
  for (const c of [
    "Snapshot ID",
    "Label",
    "Created (UTC)",
    "Atoms",
    "Playbooks",
    "Merkle Root",
    "Select",
  ]) {
    htr.appendChild(el("th", "", c));
  }
  thead.appendChild(htr);
  tbl.appendChild(thead);

  const tbody = el("tbody");
  for (const s of snapshotsCache) {
    const labelText = `${s.snapshot_id}${
      s.label ? " — " + s.label : ""
    } (${s.atom_count ?? 0} atoms)`;
    const optFrom = el("option", "", labelText);
    optFrom.value = s.snapshot_id;
    fromSel.appendChild(optFrom);

    const optTo = el("option", "", labelText);
    optTo.value = s.snapshot_id;
    toSel.appendChild(optTo);

    const tr = el("tr");
    tr.appendChild(el("td", "kpi-sub", s.snapshot_id));
    tr.appendChild(el("td", "", s.label || "—"));
    tr.appendChild(el("td", "note-item-meta", s.created_at || ""));
    tr.appendChild(el("td", "", String(s.atom_count ?? 0)));
    tr.appendChild(el("td", "", String(s.playbook_count ?? 0)));
    tr.appendChild(el("td", "kpi-sub", shortSha(s.merkle_root)));
    const tdBtn = el("td");
    const useBtn = el("button", "btn sm", "Use From");
    useBtn.type = "button";
    useBtn.onclick = () => {
      fromSel.value = s.snapshot_id;
    };
    tdBtn.appendChild(useBtn);
    tr.appendChild(tdBtn);
    tbody.appendChild(tr);
  }
  tbl.appendChild(tbody);
  tableWrap.appendChild(tbl);
}

function renderSnapshotDiff(outEl, d) {
  outEl.replaceChildren();
  const top = el("div", "toolbar");
  top.appendChild(
    el(
      "span",
      `badge ${d.unchanged ? "ok" : "warn"}`,
      d.unchanged
        ? "UNCHANGED (Identical Merkle Root)"
        : "SEMANTIC CHANGES DETECTED"
    )
  );
  top.appendChild(
    el("span", "badge mono", `${d.from_snapshot} -> ${d.to_snapshot}`)
  );
  outEl.appendChild(top);

  const roots = el(
    "div",
    "note-item-meta",
    `Root Before: ${shortSha(d.merkle_root_before)} · Root After: ${shortSha(
      d.merkle_root_after
    )}`
  );
  roots.style.margin = "6px 0";
  outEl.appendChild(roots);

  const added = d.added_atoms || [];
  const removed = d.removed_atoms || [];
  const modified = d.modified_atoms || [];

  for (const a of added) {
    const c = el("div", "diff-card added");
    c.style.marginTop = "6px";
    c.appendChild(
      el("strong", "", `+ Added Atom: ${a.filename || a.atom_id || ""}`)
    );
    c.appendChild(el("div", "", a.claim || ""));
    outEl.appendChild(c);
  }
  for (const a of removed) {
    const c = el("div", "diff-card removed");
    c.style.marginTop = "6px";
    c.appendChild(
      el("strong", "", `- Removed Atom: ${a.filename || a.atom_id || ""}`)
    );
    c.appendChild(el("div", "", a.claim || ""));
    outEl.appendChild(c);
  }
  for (const m of modified) {
    const c = el("div", "diff-card modified");
    c.style.marginTop = "6px";
    c.appendChild(
      el(
        "strong",
        "",
        `~ Modified Atom: ${m.filename} (${shortSha(
          m.old_sha256
        )} -> ${shortSha(m.new_sha256)})`
      )
    );
    c.appendChild(el("div", "note-item-meta", `Before: ${m.old_claim || ""}`));
    c.appendChild(el("div", "", `After: ${m.new_claim || ""}`));
    if (
      m.old_valid_from ||
      m.new_valid_from ||
      m.old_valid_until ||
      m.new_valid_until
    ) {
      c.appendChild(
        el(
          "div",
          "note-item-meta",
          `Validity: [${m.old_valid_from || "—"} .. ${
            m.old_valid_until || "—"
          }] -> [${m.new_valid_from || "—"} .. ${m.new_valid_until || "—"}]`
        )
      );
    }
    outEl.appendChild(c);
  }

  const pbChanges = [
    ...(d.added_playbooks || []).map((x) => `+ Playbook: ${x}`),
    ...(d.removed_playbooks || []).map((x) => `- Playbook: ${x}`),
    ...(d.modified_playbooks || []).map((x) => `~ Playbook: ${x}`),
  ];
  if (pbChanges.length) {
    const pbRow = el("div", "backlinks-bar");
    pbRow.style.marginTop = "8px";
    for (const p of pbChanges) {
      pbRow.appendChild(el("span", "badge", p));
    }
    outEl.appendChild(pbRow);
  }
}

function renderSnapshotRecall(outEl, d, query) {
  outEl.replaceChildren();
  const top = el("div", "toolbar");
  top.appendChild(
    el("span", "badge ok", `Point-in-Time Recall @ ${d.snapshot_id}`)
  );
  top.appendChild(el("span", "badge mono", `Merkle: ${shortSha(d.merkle_root)}`));
  if (d.as_of) {
    top.appendChild(el("span", "badge mono", `as_of: ${d.as_of}`));
  }
  if (d.rerank_backend) {
    top.appendChild(el("span", "badge", `reranker: ${d.rerank_backend}`));
  }
  outEl.appendChild(top);

  const results = d.results || [];
  if (!results.length) {
    const empty = el(
      "div",
      "note-item-meta",
      `0 approved atoms matched "${query}" in snapshot ${d.snapshot_id}.`
    );
    empty.style.marginTop = "8px";
    outEl.appendChild(empty);
    return;
  }

  const queryTerms = String(query || "")
    .toLowerCase()
    .split(/[^a-z0-9_]+/)
    .filter((w) => w.length >= 2);

  results.forEach((r, idx) => {
    const c = el("div", "step-card");
    c.style.marginTop = "8px";
    const row = el("div", "toolbar");
    row.appendChild(el("strong", "", `#${idx + 1} ${r.filename}`));
    row.appendChild(el("span", "badge tier-v", `score: ${r.score}`));
    row.appendChild(
      el("span", "badge mono", `sha:${shortSha(r.file_sha256)}`)
    );
    if (r.temporal_status && r.temporal_status !== "unbounded") {
      row.appendChild(
        el(
          "span",
          `badge ${r.temporal_status === "active" ? "ok" : "warn"}`,
          `validity: ${r.temporal_status}`
        )
      );
    }
    if (r.valid_from || r.valid_until) {
      row.appendChild(
        el(
          "span",
          "badge mono",
          `${r.valid_from || "—"}..${r.valid_until || "—"}`
        )
      );
    }
    for (const ctx of r.contexts || []) {
      row.appendChild(el("span", "pill", ctx));
    }
    c.appendChild(row);
    c.appendChild(el("div", "", r.claim || ""));

    const matched =
      Array.isArray(r.matched_terms) && r.matched_terms.length
        ? r.matched_terms
        : Array.isArray(r.match_reasons) && r.match_reasons.length
        ? r.match_reasons
        : queryTerms.filter((t) =>
            `${r.claim || ""} ${r.filename || ""}`.toLowerCase().includes(t)
          );
    if (matched.length) {
      const mBar = el("div", "backlinks-bar");
      mBar.style.marginTop = "4px";
      mBar.appendChild(el("span", "field-label", "Matched terms:"));
      for (const term of [...new Set(matched)]) {
        mBar.appendChild(el("span", "badge mono", term));
      }
      c.appendChild(mBar);
    }
    outEl.appendChild(c);
  });
}

function renderSnapshotVerification(outEl, d, snapshotId) {
  outEl.replaceChildren();
  const valid = Boolean(
    d.valid ??
      d.verified ??
      d.merkle_valid ??
      (d.status === "valid" || !d.errors?.length)
  );
  const top = el("div", "toolbar");
  top.appendChild(
    el(
      "span",
      `badge ${valid ? "ok" : "danger"}`,
      valid
        ? `Snapshot Verified ✓ (${d.snapshot_id || snapshotId})`
        : `Snapshot Verification Failed (${d.snapshot_id || snapshotId})`
    )
  );
  if (d.merkle_root) {
    top.appendChild(el("span", "badge mono", `Merkle: ${d.merkle_root}`));
  }
  outEl.appendChild(top);

  const kpis = el("div", "kpi-grid");
  kpis.style.marginTop = "8px";
  kpis.appendChild(
    makeKpiCard(
      "Integrity",
      valid ? "VALID" : "TAMPERED",
      "",
      valid ? "ok" : "danger"
    )
  );
  kpis.appendChild(makeKpiCard("Atoms", d.atom_count ?? d.approved_atoms ?? 0));
  kpis.appendChild(
    makeKpiCard("Playbooks", d.playbook_count ?? d.accepted_playbooks ?? 0)
  );
  if (d.created_at) {
    kpis.appendChild(makeKpiCard("Created (UTC)", d.created_at));
  }
  outEl.appendChild(kpis);

  if (Array.isArray(d.errors) && d.errors.length) {
    for (const errText of d.errors) {
      const errBanner = el("div", "status-banner banner-danger", errText);
      errBanner.style.marginTop = "6px";
      outEl.appendChild(errBanner);
    }
  }
}

async function loadProofView() {
  await Promise.all([
    loadLedgerStatus(),
    loadSourceAudit(),
    scanContradictions(),
    loadSnapshotsList(),
  ]);
}

document
  .getElementById("refreshProofBtn")
  .addEventListener("click", loadProofView);
document
  .getElementById("commitLedgerBtn")
  .addEventListener("click", async () => {
    const note = document.getElementById("ledgerNoteInput").value.trim();
    const res = await window.secondBrain.request("POST", "/v1/ledger/commit", {
      actor: "desktop_owner",
      note,
    });
    if (res.ok) {
      document.getElementById("ledgerNoteInput").value = "";
      showToast(
        `Committed signed Merkle checkpoint #${res.data?.seq ?? ""}`,
        "ok"
      );
      loadLedgerStatus();
    } else {
      showToast(apiErrorMessage(res, "Ledger commit failed"), "danger");
    }
  });

document.getElementById("proveAtomBtn").addEventListener("click", async () => {
  const targetRef =
    document.getElementById("proveAtomInput").value.trim() ||
    document.getElementById("proveAtomSelect").value;
  if (!targetRef) {
    showToast(
      "Select or enter an approved atom or playbook leaf (e.g. 5-playbooks/pb.md) to prove.",
      "warn"
    );
    return;
  }
  const res = await window.secondBrain.request(
    "GET",
    `/v1/ledger/prove/${encodeURIComponent(targetRef)}`
  );
  if (res.ok && res.data) {
    await renderAtomProofCard(res.data);
    showToast(`Generated Merkle inclusion proof for ${targetRef}`, "ok");
  } else {
    showToast(apiErrorMessage(res, "Leaf proof failed"), "danger");
  }
});

document
  .getElementById("verifyProofBtn")
  .addEventListener("click", async () => {
    const raw = document.getElementById("verifyProofTextarea").value.trim();
    const out = document.getElementById("verifyProofResult");
    out.replaceChildren();
    if (!raw) {
      showToast("Paste a Merkle proof JSON bundle to verify.", "warn");
      return;
    }
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch {
      showToast("Proof input is not valid JSON.", "danger");
      out.appendChild(el("span", "badge danger", "Invalid JSON"));
      return;
    }
    const webCheck = await verifyMerkleProofWebCrypto(parsed);
    let serverValid = webCheck.valid;
    try {
      const res = await window.secondBrain.api(
        "POST",
        "/v1/ledger/verify-proof",
        {
          proof: parsed,
        }
      );
      if (res.ok && res.data && typeof res.data.valid === "boolean") {
        serverValid = Boolean(res.data.valid);
      }
    } catch {}

    const valid = Boolean(webCheck.valid && serverValid);
    const wrap = el("div", "field-col");
    wrap.style.marginTop = "6px";
    const badgeRow = el("div", "toolbar");
    badgeRow.appendChild(
      el(
        "span",
        `badge ${valid ? "ok" : "danger"}`,
        valid
          ? "Offline WebCrypto Proof Verified ✓"
          : `PROOF INVALID / TAMPERED (${webCheck.reason || "failed"})`
      )
    );
    wrap.appendChild(badgeRow);
    if (webCheck.trace && webCheck.trace.length) {
      wrap.appendChild(buildAuditTraceList(webCheck.trace));
    }
    out.appendChild(wrap);
    showToast(
      valid
        ? "Merkle proof verified valid via WebCrypto"
        : "Merkle proof failed verification",
      valid ? "ok" : "danger"
    );
  });

document
  .getElementById("scanRadarBtn")
  .addEventListener("click", scanContradictions);
document
  .getElementById("createSnapshotBtn")
  .addEventListener("click", async () => {
    const label = document.getElementById("snapshotLabelInput").value.trim();
    const res = await window.secondBrain.request("POST", "/v1/snapshots", {
      actor: "desktop_owner",
      label,
    });
    if (res.ok && res.data) {
      document.getElementById("snapshotLabelInput").value = "";
      showToast(`Created snapshot ${res.data.snapshot_id}`, "ok");
      await loadSnapshotsList();
    } else {
      showToast(apiErrorMessage(res, "Create snapshot failed"), "danger");
    }
  });

document
  .getElementById("diffSnapshotBtn")
  .addEventListener("click", async () => {
    const from_snapshot = document.getElementById("snapshotSelect").value;
    const to_snapshot =
      document.getElementById("snapshotToSelect").value || "live";
    if (!from_snapshot) {
      showToast("Create or select a From Snapshot first.", "warn");
      return;
    }
    const res = await window.secondBrain.request("POST", "/v1/snapshots/diff", {
      from_snapshot,
      to_snapshot,
    });
    if (res.ok && res.data) {
      renderSnapshotDiff(document.getElementById("snapshotOutput"), res.data);
    } else {
      showToast(apiErrorMessage(res, "Snapshot diff failed"), "danger");
    }
  });

document
  .getElementById("verifySnapshotBtn")
  .addEventListener("click", async () => {
    const snapshot_id = document.getElementById("snapshotSelect").value;
    if (!snapshot_id) {
      showToast("Create or select a Snapshot first to verify.", "warn");
      return;
    }
    const res = await window.secondBrain.request(
      "POST",
      "/v1/snapshots/verify",
      {
        snapshot_id,
      }
    );
    if (res.ok && res.data) {
      renderSnapshotVerification(
        document.getElementById("snapshotOutput"),
        res.data,
        snapshot_id
      );
      const ok = Boolean(
        res.data.valid ?? res.data.verified ?? res.data.merkle_valid ?? true
      );
      showToast(
        ok
          ? `Snapshot ${snapshot_id} integrity verified`
          : `Snapshot ${snapshot_id} failed integrity check`,
        ok ? "ok" : "danger"
      );
    } else {
      showToast(apiErrorMessage(res, "Snapshot verify failed"), "danger");
    }
  });

document
  .getElementById("recallSnapshotBtn")
  .addEventListener("click", async () => {
    const snapshot_id = document.getElementById("snapshotSelect").value;
    const query = document.getElementById("snapshotQueryInput").value.trim();
    const project_scope =
      document.getElementById("snapshotRecallScopeInput").value.trim() ||
      undefined;
    const as_of =
      document.getElementById("snapshotAsOfInput").value.trim() || undefined;
    const include_expired = Boolean(
      document.getElementById("snapshotIncludeExpiredCheck").checked
    );
    if (!snapshot_id || !query) {
      showToast("Select a snapshot and enter a recall query.", "warn");
      return;
    }
    const res = await window.secondBrain.request(
      "POST",
      "/v1/snapshots/recall",
      {
        snapshot_id,
        query,
        project_scope,
        as_of,
        include_expired,
        limit: 10,
      }
    );
    if (res.ok && res.data) {
      renderSnapshotRecall(
        document.getElementById("snapshotOutput"),
        res.data,
        query
      );
    } else {
      showToast(apiErrorMessage(res, "Snapshot recall failed"), "danger");
    }
  });

// ─── TAB 9: Interactive 2D Knowledge Graph ───────────────────────────────────

let graphTimelineDates = [];

function ensureGraphViewerInitialized() {
  if (graphViewer) return graphViewer;
  const container = document.getElementById("graphCanvasContainer");
  graphViewer = GraphHelper.createGraphViewer(container, {
    width: 880,
    height: 540,
    onSelectNode: (node, incidentEdges) =>
      inspectSelectedGraphNode(node, incidentEdges),
  });
  return graphViewer;
}

function currentGraphFilters() {
  const asOfEl = document.getElementById("graphAsOfInput");
  const incExpEl = document.getElementById("graphIncludeExpiredCheck");
  return {
    search: document.getElementById("graphSearchInput").value,
    project: document.getElementById("graphProjectInput").value,
    status: document.getElementById("graphStatusSelect").value,
    memoryType: document.getElementById("graphTypeSelect").value,
    includePending: document.getElementById("graphIncludePendingCheck").checked,
    asOf: asOfEl ? asOfEl.value.trim() : "",
    includeExpired: incExpEl ? incExpEl.checked : true,
  };
}

function rerenderGraphFromState() {
  const viewer = ensureGraphViewerInitialized();
  const filters = currentGraphFilters();
  const filtered = viewer.render(graphRawData, filters);
  const banner = document.getElementById("graphBanner");
  if (banner) {
    const asOfTxt = filters.asOf ? ` | as_of=${filters.asOf}` : "";
    banner.textContent = `Showing ${filtered.nodes.length} node(s) and ${
      filtered.edges.length
    } edge(s)${asOfTxt}${graphRawData.fallback ? " (synthesized fallback view)" : ""}. Drag to pan, scroll to zoom, click a node to inspect.`;
  }
}

async function inspectSelectedGraphNode(node, localEdges = []) {
  const pane = document.getElementById("graphInspectorBody");
  if (!pane || !node) return;
  pane.replaceChildren(
    el("div", "status-banner", `Loading neighborhood for ${node.id}...`)
  );

  const [nbRes, atomRes] = await Promise.all([
    window.secondBrain.request(
      "GET",
      `/v1/graph/neighbors/${encodeURIComponent(node.id)}?include_pending=1`
    ),
    window.secondBrain.request(
      "GET",
      `/v1/atoms/${encodeURIComponent(node.id)}`
    ),
  ]);

  pane.replaceChildren();
  const card = el("div", "card");
  const badges = el("div", "toolbar");
  badges.appendChild(el("strong", "", node.id));
  badges.appendChild(
    el(
      "span",
      `badge ${node.verified ? "ok" : "warn"}`,
      node.review_status || "atom"
    )
  );
  badges.appendChild(el("span", "badge", node.memory_type || "semantic"));
  appendTemporalValidityBadges(badges, node);
  card.appendChild(badges);

  if (node.contexts && node.contexts.length) {
    card.appendChild(
      el("div", "note-item-meta", `Contexts: ${node.contexts.join(", ")}`)
    );
  }

  const fullAtom =
    atomRes.ok && atomRes.data ? atomRes.data.atom || atomRes.data : null;
  card.appendChild(
    el("div", "sub-panel", fullAtom?.claim || node.label || "(no claim)")
  );
  if (fullAtom?.evidence) {
    card.appendChild(el("div", "mono-box", fullAtom.evidence));
  }

  const actRow = el("div", "toolbar");
  const openBtn = el("button", "btn sm primary", "Open Atom Inspector");
  openBtn.type = "button";
  openBtn.onclick = () => openAtomInspectorModal(node.id, fullAtom || node.raw);
  const editBtn = el("button", "btn sm", "Edit Atom");
  editBtn.type = "button";
  editBtn.onclick = () =>
    openAtomEditorModal(fullAtom || { filename: node.id, claim: node.label });
  actRow.appendChild(openBtn);
  actRow.appendChild(editBtn);
  card.appendChild(actRow);
  pane.appendChild(card);

  const nbCard = el("div", "card");
  nbCard.style.marginTop = "10px";
  const apiEdges =
    nbRes.ok && Array.isArray(nbRes.data?.edges)
      ? nbRes.data.edges
      : localEdges;
  const apiNeighbors =
    nbRes.ok && Array.isArray(nbRes.data?.neighbors) ? nbRes.data.neighbors : [];
  nbCard.appendChild(
    el("strong", "", `Connected Edges (${apiEdges.length})`)
  );

  if (!apiEdges.length && !apiNeighbors.length) {
    nbCard.appendChild(
      el("div", "note-item-meta", "No connected graph neighbors.")
    );
  } else {
    for (const edge of apiEdges.slice(0, 30)) {
      const otherId = edge.from === node.id ? edge.to : edge.from;
      const row = el("div", "toolbar");
      row.appendChild(
        el("span", "badge mono", edge.kind || edge.relation || "related")
      );
      if (otherId) {
        const jumpBtn = el("button", "pill clickable", otherId);
        jumpBtn.type = "button";
        jumpBtn.onclick = () => {
          if (!graphViewer?.selectNodeById(otherId)) {
            openAtomInspectorModal(otherId);
          }
        };
        row.appendChild(jumpBtn);
      }
      nbCard.appendChild(row);
    }
  }
  pane.appendChild(nbCard);
}

async function loadGraphView() {
  ensureGraphViewerInitialized();
  const proj = document.getElementById("graphProjectInput").value.trim();
  const incPending = document.getElementById("graphIncludePendingCheck").checked
    ? "1"
    : "0";
  const asOfInputEl = document.getElementById("graphAsOfInput");
  const sliderEl = document.getElementById("graphTimelineSlider");
  const incExpEl = document.getElementById("graphIncludeExpiredCheck");
  const metaBadge = document.getElementById("graphTemporalMeta");
  const asOf = asOfInputEl ? asOfInputEl.value.trim() : "";
  const incExpired = incExpEl ? (incExpEl.checked ? "1" : "0") : "1";

  const params = [
    `include_pending=${incPending}`,
    `include_expired=${incExpired}`,
    "limit=300",
  ];
  if (proj) params.push(`project=${encodeURIComponent(proj)}`);
  if (asOf) params.push(`as_of=${encodeURIComponent(asOf)}`);

  const fullRes = await window.secondBrain.request(
    "GET",
    `/v1/graph/full?${params.join("&")}`
  );
  if (fullRes.ok && fullRes.data && Array.isArray(fullRes.data.nodes)) {
    graphTimelineDates = Array.isArray(fullRes.data.timeline_dates)
      ? fullRes.data.timeline_dates
      : [];
    if (sliderEl) {
      sliderEl.max = String(graphTimelineDates.length);
      const matchIdx = asOf ? graphTimelineDates.indexOf(asOf) : -1;
      sliderEl.value = matchIdx >= 0 ? String(matchIdx + 1) : "0";
    }
    if (metaBadge) {
      const actCnt = fullRes.data.active_temporal_count ?? fullRes.data.nodes.length;
      const expCnt = fullRes.data.expired_temporal_count ?? 0;
      const futCnt = fullRes.data.future_temporal_count ?? 0;
      metaBadge.textContent = `${asOf ? `as_of ${asOf}` : "Live (Now)"} · active=${actCnt} exp=${expCnt} future=${futCnt}`;
    }
    graphRawData = {
      nodes: fullRes.data.nodes,
      edges: Array.isArray(fullRes.data.edges) ? fullRes.data.edges : [],
      fallback: false,
    };
  } else {
    const [atomsRes, radarRes] = await Promise.all([
      window.secondBrain.request("GET", "/v1/atoms?limit=300"),
      window.secondBrain.request(
        "GET",
        `/v1/graph/contradictions?include_pending=${incPending}`
      ),
    ]);
    graphRawData = GraphHelper.synthesizeFallbackGraph(
      atomsRes.data?.items || [],
      radarRes.data || {}
    );
  }
  rerenderGraphFromState();
}

for (const id of [
  "graphSearchInput",
  "graphStatusSelect",
  "graphTypeSelect",
  "graphIncludePendingCheck",
]) {
  document.getElementById(id).addEventListener("input", rerenderGraphFromState);
  document.getElementById(id).addEventListener("change", rerenderGraphFromState);
}
document.getElementById("graphAsOfInput")?.addEventListener("change", loadGraphView);
document.getElementById("graphIncludeExpiredCheck")?.addEventListener("change", loadGraphView);
document.getElementById("graphClearAsOfBtn")?.addEventListener("click", () => {
  const asOfEl = document.getElementById("graphAsOfInput");
  const sliderEl = document.getElementById("graphTimelineSlider");
  if (asOfEl) asOfEl.value = "";
  if (sliderEl) sliderEl.value = "0";
  loadGraphView();
});
document.getElementById("graphTimelineSlider")?.addEventListener("change", (e) => {
  const idx = Number(e.target?.value || 0);
  const asOfEl = document.getElementById("graphAsOfInput");
  if (asOfEl) {
    asOfEl.value = idx > 0 && idx <= graphTimelineDates.length ? graphTimelineDates[idx - 1] : "";
  }
  loadGraphView();
});
document
  .getElementById("graphProjectInput")
  .addEventListener("change", loadGraphView);
document
  .getElementById("graphProjectInput")
  .addEventListener("input", rerenderGraphFromState);
document.getElementById("graphZoomInBtn").addEventListener("click", () => {
  ensureGraphViewerInitialized().zoomBy(1.22);
});
document.getElementById("graphZoomOutBtn").addEventListener("click", () => {
  ensureGraphViewerInitialized().zoomBy(0.82);
});
document.getElementById("graphResetViewBtn").addEventListener("click", () => {
  ensureGraphViewerInitialized().resetView();
});
const graphAutoLinkBtnEl = document.getElementById("graphAutoLinkBtn");
if (graphAutoLinkBtnEl) {
  graphAutoLinkBtnEl.addEventListener("click", async () => {
    const proj = document.getElementById("graphProjectInput")?.value.trim() || undefined;
    const incPending = Boolean(document.getElementById("graphIncludePendingCheck")?.checked);
    const res = await window.secondBrain.request("POST", "/v1/graph/auto-link", {
      actor: "desktop_owner",
      project_scope: proj,
      include_pending: incPending,
      dry_run: false,
    });
    if (!res.ok) {
      showToast(apiErrorMessage(res, "Auto-link graph failed"), "danger");
      return;
    }
    const d = res.data || {};
    showToast(
      `Auto-linked ${d.updated_atom_count ?? 0} atom(s) · Orphans ${d.orphan_before ?? "?"} -> ${d.orphan_after ?? "?"}`,
      "ok"
    );
    await loadGraphView();
  });
}
document
  .getElementById("refreshGraphBtn")
  .addEventListener("click", loadGraphView);

// ─── TAB 10: Vault Explorer (3-threads/, 4-experiences/, briefings/) ─────────

function renderActiveVaultDocument() {
  const viewer = document.getElementById("vaultDocViewer");
  const metaBar = document.getElementById("vaultDocMetaBar");
  const modeBtn = document.getElementById("vaultRenderModeBtn");
  if (!viewer || !metaBar) return;

  modeBtn.textContent = vaultRawMode
    ? "View Rendered Markdown"
    : "View Raw Source";

  viewer.replaceChildren();
  if (!activeVaultDoc) {
    metaBar.textContent = "Select a vault document on the left to view";
    viewer.appendChild(
      el(
        "div",
        "note-item-meta",
        `Browse ${activeVaultCategory} files on the left.`
      )
    );
    return;
  }

  metaBar.textContent = `${activeVaultCategory}/${activeVaultDoc.filename} (Untrusted Vault Document)`;
  if (vaultRawMode) {
    viewer.appendChild(el("pre", "mono-box", activeVaultDoc.content || ""));
  } else {
    renderRichMarkdown(viewer, activeVaultDoc.content || "", {
      emptyPlaceholder: "(Empty vault document)",
    });
  }
}

async function openVaultDocument(category, filename) {
  const res = await window.secondBrain.request(
    "GET",
    `/v1/vault/${encodeURIComponent(category)}/${encodeURIComponent(filename)}`
  );
  if (!res.ok || !res.data) {
    showToast(
      apiErrorMessage(res, `Failed to open ${category}/${filename}`),
      "danger"
    );
    return;
  }
  activeVaultDoc = {
    category,
    filename: res.data.filename || filename,
    content: res.data.content || "",
  };
  document.querySelectorAll("#vaultFilesList .note-item").forEach((n) => {
    n.classList.toggle("active", n.dataset.filename === filename);
  });
  renderActiveVaultDocument();
}

async function loadVaultBrowser({ append = false } = {}) {
  const listEl = document.getElementById("vaultFilesList");
  if (!append) {
    vaultPaginator.reset();
    activeVaultDoc = null;
    renderActiveVaultDocument();
  }
  const url = PageHelper.buildPaginatedPath(
    `/v1/vault/${encodeURIComponent(activeVaultCategory)}`,
    {
      limit: vaultPaginator.getPageSize(),
      offset: vaultPaginator.nextOffset(),
    }
  );
  const res = await window.secondBrain.request("GET", url);
  listEl.replaceChildren();
  if (!res.ok) {
    listEl.appendChild(
      el(
        "div",
        "status-banner",
        apiErrorMessage(res, `No ${activeVaultCategory} endpoint or directory.`)
      )
    );
    document.getElementById("vaultPaginationBar").style.display = "none";
    return;
  }

  vaultPaginator.applyPage(res.data || {}, {
    append,
    preferredKey: "items",
  });
  const items = vaultPaginator.getItems();
  if (!items.length) {
    listEl.appendChild(
      el("div", "status-banner", `No files in ${activeVaultCategory} yet.`)
    );
  } else {
    for (const item of items) {
      const row = el(
        "div",
        "note-item" +
          (activeVaultDoc?.filename === item.filename ? " active" : "")
      );
      row.dataset.filename = item.filename;
      row.appendChild(el("div", "note-item-title", item.title || item.filename));
      const meta = [
        item.filename,
        item.modified_at ? String(item.modified_at).slice(0, 19) : "",
        item.size_bytes !== undefined ? `${item.size_bytes} B` : "",
      ]
        .filter(Boolean)
        .join(" · ");
      row.appendChild(el("div", "note-item-meta", meta));
      row.onclick = () => openVaultDocument(activeVaultCategory, item.filename);
      listEl.appendChild(row);
    }
  }

  PageHelper.renderPaginationBar(
    document.getElementById("vaultPaginationBar"),
    vaultPaginator,
    () => loadVaultBrowser({ append: true }),
    { noun: activeVaultCategory }
  );
}

document.querySelectorAll("[data-vault-cat]").forEach((btn) => {
  btn.addEventListener("click", () => {
    activeVaultCategory = btn.dataset.vaultCat || "threads";
    document.querySelectorAll("[data-vault-cat]").forEach((b) => {
      b.classList.toggle("primary", b.dataset.vaultCat === activeVaultCategory);
    });
    loadVaultBrowser({ append: false });
  });
});

document.getElementById("vaultRenderModeBtn").addEventListener("click", () => {
  vaultRawMode = !vaultRawMode;
  renderActiveVaultDocument();
});
document
  .getElementById("refreshVaultBtn")
  .addEventListener("click", () => loadVaultBrowser({ append: false }));

// ─── v1.0.0: Multimodal Media Upload, Git Distiller, Daemon & Portable Backup ──

function readBlobAsBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const str = String(reader.result || "");
      const idx = str.indexOf(",");
      resolve(idx >= 0 ? str.slice(idx + 1) : str);
    };
    reader.onerror = () => reject(new Error("Could not read file bytes."));
    reader.readAsDataURL(blob);
  });
}

async function uploadMediaBlobToVault(blob, filename) {
  const outEl = document.getElementById("mediaUploadResult");
  if (!blob || blob.size === 0) {
    showToast("Select or record non-empty media first.", "warn");
    return;
  }
  if (blob.size > 24 * 1024 * 1024) {
    outEl.textContent = "Rejected: media payload exceeds 24 MiB upload limit.";
    showToast("Media exceeds 24 MiB upload limit.", "danger");
    return;
  }
  outEl.textContent = `Uploading & processing ${filename}...`;
  try {
    const content_base64 = await readBlobAsBase64(blob);
    const res = await window.secondBrain.request("POST", "/v1/ingest/media", {
      filename,
      content_base64,
    });
    if (res.ok && res.data) {
      const d = res.data;
      const flagSuffix =
        Array.isArray(d.dlp_flags) && d.dlp_flags.length
          ? ` [DLP flags: ${d.dlp_flags.join(", ")}]`
          : "";
      outEl.textContent = `${d.quarantined ? "Quarantined" : "Ingested"} (${
        d.category
      }): ${d.output_rel_path} (sha256=${shortSha(d.source_sha256)})${flagSuffix}`;
      showToast(
        `${d.quarantined ? "Quarantined in Inbox" : "Ingested"}: ${
          d.output_filename
        }`,
        d.quarantined ? "warn" : "ok"
      );
      refreshHealth();
    } else {
      const msg = apiErrorMessage(res, "Media ingestion failed");
      outEl.textContent = msg;
      showToast(msg, "danger");
    }
  } catch (err) {
    outEl.textContent = `Media upload error: ${err?.message || String(err)}`;
    showToast("Media upload error", "danger");
  }
}

document
  .getElementById("mediaUploadSubmitBtn")
  .addEventListener("click", async () => {
    const inp = document.getElementById("mediaUploadFileInput");
    const file = inp.files && inp.files[0];
    if (!file) {
      showToast("Choose a media file first.", "warn");
      return;
    }
    await uploadMediaBlobToVault(file, file.name || "uploaded-media.bin");
    inp.value = "";
  });

let importMediaRecorder = null;
let importMediaStream = null;
let importAudioChunks = [];

function stopImportMediaStream() {
  if (importMediaStream) {
    for (const t of importMediaStream.getTracks()) t.stop();
    importMediaStream = null;
  }
}

document
  .getElementById("mediaUploadRecordBtn")
  .addEventListener("click", async () => {
    const btn = document.getElementById("mediaUploadRecordBtn");
    const outEl = document.getElementById("mediaUploadResult");
    if (importMediaRecorder && importMediaRecorder.state === "recording") {
      importMediaRecorder.stop();
      return;
    }
    if (
      !navigator.mediaDevices ||
      typeof navigator.mediaDevices.getUserMedia !== "function" ||
      typeof MediaRecorder === "undefined"
    ) {
      showToast("Microphone recording is unavailable in this environment.", "warn");
      return;
    }
    try {
      importMediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      importAudioChunks = [];
      importMediaRecorder = new MediaRecorder(importMediaStream);
      btn.textContent = "Stop Voice Memo";
      outEl.textContent = "Recording voice memo... click Stop Voice Memo to transcribe & ingest.";
      importMediaRecorder.ondataavailable = (ev) => {
        if (ev.data && ev.data.size > 0) importAudioChunks.push(ev.data);
      };
      importMediaRecorder.onstop = async () => {
        btn.textContent = "Record Voice Memo";
        stopImportMediaStream();
        const blob = new Blob(importAudioChunks, { type: "audio/webm" });
        const stamp = new Date().toISOString().replace(/[:.]/g, "-");
        await uploadMediaBlobToVault(blob, `voice-memo-${stamp}.webm`);
      };
      importMediaRecorder.start();
    } catch (err) {
      stopImportMediaStream();
      btn.textContent = "Record Voice Memo";
      outEl.textContent = `Microphone error: ${err?.message || String(err)}`;
    }
  });

window.addEventListener("paste", async (e) => {
  if (activeView !== "import") return;
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  for (const item of items) {
    if (item.kind === "file" && item.type.startsWith("image/")) {
      const file = item.getAsFile();
      if (file) {
        e.preventDefault();
        const ext = (file.type.split("/")[1] || "png").replace(/[^a-z0-9]/gi, "") || "png";
        const stamp = new Date().toISOString().replace(/[:.]/g, "-");
        await uploadMediaBlobToVault(file, `clipboard-screenshot-${stamp}.${ext}`);
        return;
      }
    }
  }
});

// Git Post-Commit Hook & Coding Session Distiller
async function loadGitHookStatus() {
  const repo = document.getElementById("gitHookRepoInput").value.trim();
  const badge = document.getElementById("gitHookStatusBadge");
  const outEl = document.getElementById("gitHookOutput");
  const q = repo ? `?repo=${encodeURIComponent(repo)}` : "";
  const res = await window.secondBrain.request("GET", `/v1/git/hook${q}`);
  if (res.ok && res.data) {
    const installed = Boolean(res.data.installed);
    badge.textContent = installed ? "hook installed" : "not installed";
    badge.className = `badge ${installed ? "ok" : "warn"}`;
    outEl.textContent = `Repo: ${res.data.repo_root} · Hook: ${res.data.hook_path}`;
  } else {
    badge.textContent = "unavailable";
    badge.className = "badge";
    outEl.textContent = apiErrorMessage(res, "Select a valid Git repository path.");
  }
}

document
  .getElementById("gitHookRefreshBtn")
  .addEventListener("click", loadGitHookStatus);

document
  .getElementById("gitHookBrowseBtn")
  .addEventListener("click", async () => {
    const folder = await window.secondBrain.pickFolder();
    if (folder) {
      document.getElementById("gitHookRepoInput").value = folder;
      loadGitHookStatus();
    }
  });

document
  .getElementById("gitHookInstallBtn")
  .addEventListener("click", async () => {
    const repo =
      document.getElementById("gitHookRepoInput").value.trim() || undefined;
    const project_scope =
      document.getElementById("gitHookProjectInput").value.trim() || undefined;
    const res = await window.secondBrain.request("POST", "/v1/git/hook/install", {
      repo,
      project_scope,
    });
    if (res.ok && res.data) {
      showToast(`Installed post-commit hook (${res.data.project_scope})`, "ok");
      loadGitHookStatus();
    } else {
      showToast(apiErrorMessage(res, "Failed to install Git hook"), "danger");
    }
  });

document
  .getElementById("gitHookUninstallBtn")
  .addEventListener("click", async () => {
    const repo =
      document.getElementById("gitHookRepoInput").value.trim() || undefined;
    const res = await window.secondBrain.request(
      "POST",
      "/v1/git/hook/uninstall",
      { repo }
    );
    if (res.ok && res.data) {
      showToast(`Post-commit hook: ${res.data.status}`, "ok");
      loadGitHookStatus();
    } else {
      showToast(apiErrorMessage(res, "Failed to remove Git hook"), "danger");
    }
  });

document
  .getElementById("gitDistillBtn")
  .addEventListener("click", async () => {
    const repo =
      document.getElementById("gitHookRepoInput").value.trim() || undefined;
    const project_scope =
      document.getElementById("gitHookProjectInput").value.trim() || undefined;
    const max_commits = Math.max(
      1,
      Math.min(
        25,
        parseInt(document.getElementById("gitDistillMaxInput").value, 10) || 5
      )
    );
    const outEl = document.getElementById("gitHookOutput");
    outEl.textContent = "Distilling recent commits with DLP screening...";
    const res = await window.secondBrain.request("POST", "/v1/git/distill", {
      repo,
      project_scope,
      max_commits,
      stage_inbox: true,
    });
    if (res.ok && res.data) {
      const d = res.data;
      outEl.textContent = `Distilled ${d.distilled_count} commit(s) (${d.skipped_duplicate_count} already distilled) in ${d.project_scope}.`;
      showToast(`Distilled ${d.distilled_count} commit(s)`, "ok");
      refreshHealth();
    } else {
      const msg = apiErrorMessage(res, "Git distillation failed");
      outEl.textContent = msg;
      showToast(msg, "danger");
    }
  });


// Watchdog Daemon & One-Shot Index/Vector Sync
async function loadDaemonStatus() {
  const kpis = document.getElementById("daemonStatusKpis");
  const outEl = document.getElementById("daemonOutput");
  const res = await window.secondBrain.request("GET", "/v1/daemon/status");
  if (!res.ok || !res.data) {
    outEl.textContent = apiErrorMessage(res, "Daemon status unavailable.");
    return;
  }
  const d = res.data;
  kpis.replaceChildren(
    makeKpiCard(
      "Lexical FTS5",
      d.lexical_index_current ? "Current" : "Needs Sync",
      "",
      d.lexical_index_current ? "ok" : "warn"
    ),
    makeKpiCard(
      "Dense Chroma",
      d.dense_index_current ? "Current" : "Optional/Stale",
      d.dense_generation_id ? shortSha(d.dense_generation_id) : "none"
    ),
    makeKpiCard("0-raw Queue", d.raw_queue_count ?? 0),
    makeKpiCard("Quarantined Inbox", d.inbox_quarantine_count ?? 0)
  );
  const autoEnabled = Boolean(d.auto_sync && d.auto_sync.enabled);
  outEl.textContent = `Active vault: ${d.brain_dir} · Auto-Sync: ${
    autoEnabled ? `ON (${d.auto_sync.interval_seconds}s)` : "OFF"
  }`;
}

document
  .getElementById("daemonRefreshBtn")
  .addEventListener("click", loadDaemonStatus);

document
  .getElementById("daemonSyncOnceBtn")
  .addEventListener("click", async () => {
    const auto_sync = Boolean(
      document.getElementById("daemonSyncAutoCheck").checked
    );
    const outEl = document.getElementById("daemonOutput");
    outEl.textContent = "Running one-shot FTS5 + Chroma vector sync & stale anchor check...";
    const res = await window.secondBrain.request("POST", "/v1/daemon/sync-once", {
      auto_sync,
    });
    if (res.ok && res.data) {
      const ds = res.data.daemon_sync || {};
      const stale = ds.atoms ? ds.atoms.stale_count ?? 0 : 0;
      const vecSt =
        ds.atoms && ds.atoms.vector_sync ? ds.atoms.vector_sync.status : "n/a";
      outEl.textContent = `Synced notes & atoms · Vector sync: ${vecSt} · Stale source anchors: ${stale}`;
      showToast("Completed one-shot index & vector sync", "ok");
      loadDaemonStatus();
      refreshHealth();
    } else {
      const msg = apiErrorMessage(res, "One-shot daemon sync failed");
      outEl.textContent = msg;
      showToast(msg, "danger");
    }
  });


// Portable Three-Store Vault Backup & Restore (.sbvault)
document
  .getElementById("backupPickSaveBtn")
  .addEventListener("click", async () => {
    if (typeof window.secondBrain.pickSaveBackup === "function") {
      const chosen = await window.secondBrain.pickSaveBackup();
      if (chosen) document.getElementById("backupOutPathInput").value = chosen;
    }
  });

document
  .getElementById("backupPickOpenBtn")
  .addEventListener("click", async () => {
    if (typeof window.secondBrain.pickBackupFile === "function") {
      const chosen = await window.secondBrain.pickBackupFile();
      if (chosen) document.getElementById("backupVerifyPathInput").value = chosen;
    }
  });

document
  .getElementById("backupPickTargetBtn")
  .addEventListener("click", async () => {
    const chosen = await window.secondBrain.pickFolder();
    if (chosen) {
      document.getElementById("backupRestoreTargetInput").value = chosen;
    }
  });

document
  .getElementById("backupCreateBtn")
  .addEventListener("click", async () => {
    const output_path = document.getElementById("backupOutPathInput").value.trim();
    const passphrase =
      document.getElementById("backupPassphraseInput").value || undefined;
    const unencrypted = Boolean(
      document.getElementById("backupUnencryptedCheck").checked
    );
    const outEl = document.getElementById("backupOutput");
    if (!output_path) {
      showToast("Choose a destination .sbvault path first.", "warn");
      return;
    }
    outEl.textContent = "Creating portable Three-Store .sbvault backup...";
    const res = await window.secondBrain.request(
      "POST",
      "/v1/vault/backup/create",
      {
        output_path,
        passphrase: unencrypted ? undefined : passphrase,
        unencrypted,
      }
    );
    if (res.ok && res.data) {
      const d = res.data;
      document.getElementById("backupVerifyPathInput").value = d.backup_path;
      outEl.textContent = `Created ${
        d.encrypted ? "AES-256-GCM encrypted" : "unencrypted"
      } backup: ${d.backup_path} (${d.file_count} files, Merkle ${shortSha(
        d.merkle_root
      )})`;
      showToast("Vault backup created", "ok");
    } else {
      const msg = apiErrorMessage(res, "Backup creation failed");
      outEl.textContent = msg;
      showToast(msg, "danger");
    }
  });


document
  .getElementById("backupVerifyBtn")
  .addEventListener("click", async () => {
    const backup_path = document
      .getElementById("backupVerifyPathInput")
      .value.trim();
    const passphrase =
      document.getElementById("backupPassphraseInput").value || undefined;
    const outEl = document.getElementById("backupOutput");
    if (!backup_path) {
      showToast("Select a .sbvault archive to verify.", "warn");
      return;
    }
    outEl.textContent = "Verifying .sbvault AEAD tag, SHA-256 hashes & Merkle root in memory...";
    const res = await window.secondBrain.request(
      "POST",
      "/v1/vault/backup/verify",
      { backup_path, passphrase }
    );
    if (res.ok && res.data) {
      const d = res.data;
      outEl.textContent = `VALID (${d.cipher}): ${d.file_count} files, ${d.approved_atoms} approved atoms, Merkle ${shortSha(
        d.merkle_root
      )}`;
      showToast("Backup archive verified ✓", "ok");
    } else {
      const msg = apiErrorMessage(res, "Backup verification failed");
      outEl.textContent = msg;
      showToast(msg, "danger");
    }
  });


document
  .getElementById("backupRestoreBtn")
  .addEventListener("click", async () => {
    const backup_path = document
      .getElementById("backupVerifyPathInput")
      .value.trim();
    const target_brain_dir = document
      .getElementById("backupRestoreTargetInput")
      .value.trim();
    const passphrase =
      document.getElementById("backupPassphraseInput").value || undefined;
    const force = Boolean(
      document.getElementById("backupForceRestoreCheck").checked
    );
    const outEl = document.getElementById("backupOutput");
    if (!backup_path || !target_brain_dir) {
      showToast("Provide both .sbvault archive path and target vault directory.", "warn");
      return;
    }
    outEl.textContent = "Restoring Three-Store vault & rebuilding FTS5 indexes...";
    const res = await window.secondBrain.request(
      "POST",
      "/v1/vault/backup/restore",
      { backup_path, target_brain_dir, passphrase, force }
    );
    if (res.ok && res.data) {
      const d = res.data;
      outEl.textContent = `Restored ${d.restored_files} files into ${d.target_brain_dir} (${d.lexical_indexed_atoms} atoms & ${d.indexed_notes} notes indexed).`;
      showToast("Vault restored & FTS5 rebuilt ✓", "ok");
    } else {
      const msg = apiErrorMessage(res, "Backup restore failed");
      outEl.textContent = msg;
      showToast(msg, "danger");
    }
  });

// ─── Stage 4 Executable Skills, Mobile Vault Client & Creative Media Studio ─

const SAFE_IMAGE_DATA_URI_RE =
  /^data:image\/(?:png|webp|gif|jpeg);base64,[A-Za-z0-9+/]+={0,2}$/;
const SAFE_VIDEO_DATA_URI_RE =
  /^data:video\/mp4;base64,[A-Za-z0-9+/]+={0,2}$/;
const IMAGE_ONLY_MEDIA_BACKENDS = new Set(["sd_webui_local", "diffusers_local"]);

function isSafeMediaDataUri(uri, expectedKind) {
  if (typeof uri !== "string" || !uri) return false;
  if (expectedKind === "video") {
    return SAFE_VIDEO_DATA_URI_RE.test(uri);
  }
  return SAFE_IMAGE_DATA_URI_RE.test(uri);
}

async function loadCreativeMediaStudio() {
  const badge = document.getElementById("creativeHardwareBadge");
  const galleryWrap = document.getElementById("creativeGalleryWrap");
  if (!badge || !galleryWrap) return;

  const [stRes, galRes] = await Promise.all([
    window.secondBrain.request("GET", "/v1/media/status"),
    window.secondBrain.request("GET", "/v1/media/gallery?limit=12"),
  ]);

  if (stRes.ok && stRes.data) {
    const st = stRes.data;
    badge.textContent = `${st.tier_id || st.creative_tier || "tier_0"} · max ${st.max_width || 768}x${st.max_height || 768}`;
    const neuralUnlocked = Boolean(
      st.neural_backends_unlocked ?? st.neural_generation_eligible
    );
    badge.className = neuralUnlocked ? "badge ok" : "badge";
  }

  galleryWrap.replaceChildren();
  const items = (galRes.ok && Array.isArray(galRes.data?.items)) ? galRes.data.items : [];
  for (const item of items) {
    const card = el("div", "card");
    const hdr = el("div", "toolbar");
    hdr.appendChild(el("strong", "", item.title || item.filename || item.media_id));
    hdr.appendChild(el("span", "badge mono", item.media_type || "media"));
    card.appendChild(hdr);
    card.appendChild(
      el(
        "div",
        "note-item-meta",
        `${item.backend_used || "local"} · ${item.width || "?"}x${item.height || "?"} · SHA ${shortSha(item.sha256)}`
      )
    );
    const prevBtn = el("button", "btn sm", "Preview Asset");
    prevBtn.type = "button";
    prevBtn.addEventListener("click", () => previewCreativeAsset(item.media_id));
    card.appendChild(prevBtn);
    galleryWrap.appendChild(card);
  }
}

async function previewCreativeAsset(mediaId) {
  const wrap = document.getElementById("creativePreviewWrap");
  if (!wrap || !mediaId) return;
  wrap.style.display = "block";
  wrap.replaceChildren(el("div", "note-item-meta", `Loading ${mediaId}...`));
  const res = await window.secondBrain.request(
    "GET",
    `/v1/media/file/${encodeURIComponent(mediaId)}?encoding=base64`
  );
  if (!res.ok || !res.data?.data_uri) {
    wrap.replaceChildren(el("div", "note-item-meta", apiErrorMessage(res, "Preview failed")));
    return;
  }
  wrap.replaceChildren();
  const d = res.data;
  wrap.appendChild(
    el("div", "note-item-meta", `${d.filename} (${d.mime_type}, ${d.size_bytes} bytes, SHA-256 ${shortSha(d.sha256)})`)
  );
  const isVideo = String(d.mime_type || "").toLowerCase() === "video/mp4";
  const expectedKind = isVideo ? "video" : "image";
  if (!isSafeMediaDataUri(d.data_uri, expectedKind)) {
    wrap.appendChild(
      el(
        "div",
        "status-banner banner-danger",
        "Blocked unsafe or unsupported media data URI."
      )
    );
    return;
  }
  if (isVideo) {
    const vid = document.createElement("video");
    vid.controls = true;
    vid.autoplay = false;
    vid.loop = true;
    vid.src = d.data_uri;
    vid.style.maxWidth = "100%";
    vid.style.maxHeight = "320px";
    vid.style.marginTop = "6px";
    wrap.appendChild(vid);
  } else {
    const img = document.createElement("img");
    img.src = d.data_uri;
    img.alt = d.filename || d.media_id;
    img.style.maxWidth = "100%";
    img.style.maxHeight = "320px";
    img.style.marginTop = "6px";
    wrap.appendChild(img);
  }
}

async function triggerCreativeMediaGen(kind, outputFormat) {
  const promptEl = document.getElementById("creativePromptInput");
  const outEl = document.getElementById("creativeStudioOutput");
  if (!promptEl || !outEl) return;
  const prompt = promptEl.value.trim();
  if (!prompt) {
    showToast("Enter a creative generation prompt first.", "warn");
    return;
  }
  let selectedBackend =
    document.getElementById("creativeBackendSelect")?.value || "auto";
  if (kind === "video" && IMAGE_ONLY_MEDIA_BACKENDS.has(selectedBackend)) {
    showToast(
      `Backend '${selectedBackend}' is image-only; falling back to 'auto' for video generation.`,
      "warn"
    );
    selectedBackend = "auto";
  }
  const payload = {
    prompt,
    title: (document.getElementById("creativeTitleInput")?.value || "").trim(),
    style: document.getElementById("creativeStyleSelect")?.value || "concept_card",
    backend: selectedBackend,
    output_format: outputFormat,
    project_scope: (document.getElementById("creativeScopeInput")?.value || "").trim() || undefined,
    ground_in_vault: Boolean(document.getElementById("creativeGroundVaultCheck")?.checked),
    attach_to_note: Boolean(document.getElementById("creativeAttachNoteCheck")?.checked),
  };
  outEl.textContent = `Synthesizing offline ${kind} (${outputFormat}) via ${payload.backend}...`;
  const endpoint = kind === "video" ? "/v1/media/video" : "/v1/media/image";
  const res = await window.secondBrain.request("POST", endpoint, payload);
  if (res.ok && res.data) {
    const d = res.data;
    const groundedIds = Array.isArray(d.grounded_atom_ids)
      ? d.grounded_atom_ids
      : Array.isArray(d.referenced_atoms)
        ? d.referenced_atoms
        : [];
    outEl.textContent = `Generated ${d.filename} (${d.backend_used}, ${d.width}x${d.height}, SHA ${shortSha(d.sha256)}, grounded=${groundedIds.length} atoms).`;
    showToast(`Generated ${d.filename}`, "ok");
    await previewCreativeAsset(d.media_id);
    await loadCreativeMediaStudio();
  } else {
    const msg = apiErrorMessage(res, "Creative media generation failed");
    outEl.textContent = msg;
    showToast(msg, "danger");
  }
}

document.getElementById("creativeRefreshBtn")?.addEventListener("click", loadCreativeMediaStudio);
document.getElementById("creativeGenImageBtn")?.addEventListener("click", () => triggerCreativeMediaGen("image", "png"));
document.getElementById("creativeGenVideoBtn")?.addEventListener("click", () => triggerCreativeMediaGen("video", "mp4"));
document.getElementById("creativeGenGifBtn")?.addEventListener("click", () => triggerCreativeMediaGen("video", "gif"));

async function loadMobileVaultClient() {
  const wrap = document.getElementById("mobileVaultPairingsWrap");
  if (!wrap) return;
  const res = await window.secondBrain.request("GET", "/v1/mobile/status");
  if (!res.ok || !res.data) return;
  wrap.replaceChildren();
  const pairings = Array.isArray(res.data.pairings) ? res.data.pairings : [];
  for (const p of pairings) {
    const isRevoked = Boolean(p.revoked || p.status === "revoked");
    const statusLabel = isRevoked ? "revoked" : (p.status || "active");
    const row = el("div", "toolbar");
    row.appendChild(el("span", "badge mono", p.pairing_id || ""));
    row.appendChild(el("strong", "", p.label || "Mobile Client"));
    row.appendChild(el("span", "note-item-meta", `${statusLabel} · ${p.created_at || ""}`));
    if (!isRevoked) {
      const revBtn = el("button", "btn sm danger", "Revoke");
      revBtn.type = "button";
      revBtn.style.marginLeft = "auto";
      revBtn.addEventListener("click", async () => {
        const r = await window.secondBrain.request("POST", "/v1/mobile/revoke", {
          pairing_id: p.pairing_id,
        });
        if (r.ok) {
          showToast(`Revoked mobile client ${p.pairing_id}`, "ok");
          loadMobileVaultClient();
        } else {
          showToast(apiErrorMessage(r, "Revoke failed"), "danger");
        }
      });
      row.appendChild(revBtn);
    }
    wrap.appendChild(row);
  }
}

document.getElementById("mobileVaultRefreshBtn")?.addEventListener("click", loadMobileVaultClient);
document.getElementById("mobileVaultPairBtn")?.addEventListener("click", async () => {
  const outEl = document.getElementById("mobileVaultOutput");
  if (!outEl) return;
  const payload = {
    label: (document.getElementById("mobileVaultLabelInput")?.value || "Mobile Vault App").trim(),
    pwa_base_url: (document.getElementById("mobileVaultBaseUrlInput")?.value || "https://localhost/vault-client/").trim(),
    include_recovery_key: Boolean(document.getElementById("mobileVaultIncludeKeyCheck")?.checked),
    include_inbox_drop_pairing: Boolean(document.getElementById("mobileVaultIncludeDropCheck")?.checked),
  };
  const res = await window.secondBrain.request("POST", "/v1/mobile/pair", payload);
  if (res.ok && res.data) {
    const shareUri =
      res.data.pwa_pairing_url ||
      res.data.pairing_url ||
      res.data.pairing_fragment ||
      "";
    outEl.textContent = `Paired ${res.data.pairing_id}: ${shareUri}`;
    showToast("Mobile Vault Client paired ✓", "ok");
    loadMobileVaultClient();
  } else {
    const msg = apiErrorMessage(res, "Mobile Vault Client pairing failed");
    outEl.textContent = msg;
    showToast(msg, "danger");
  }
});

document.getElementById("mobileVaultExportBtn")?.addEventListener("click", async () => {
  const outEl = document.getElementById("mobileVaultOutput");
  if (!outEl) return;
  outEl.textContent = "Exporting offline iOS / Android Mobile Vault Client bundle...";
  const res = await window.secondBrain.request("POST", "/v1/mobile/export", {});
  if (res.ok && res.data) {
    outEl.textContent = `Exported ${res.data.zip_path} (${res.data.size_bytes} bytes, SHA-256 ${shortSha(res.data.sha256)})`;
    showToast("Exported Mobile Vault Client bundle ✓", "ok");
  } else {
    const msg = apiErrorMessage(res, "Export failed");
    outEl.textContent = msg;
    showToast(msg, "danger");
  }
});

async function loadExecutableSkills() {
  const badge = document.getElementById("skillKillSwitchBadge");
  const pbSelect = document.getElementById("skillPlaybookSelect");
  const grid = document.getElementById("executableSkillsGrid");
  const execWrap = document.getElementById("skillExecutionsWrap");
  if (!badge || !grid) return;

  const [skRes, candRes, exRes] = await Promise.all([
    window.secondBrain.request("GET", "/v1/skills?include_inactive=1"),
    window.secondBrain.request("GET", "/v1/skills/candidates"),
    window.secondBrain.request("GET", "/v1/skills/executions?limit=10"),
  ]);

  const killSwitchState = (skRes.ok && skRes.data?.kill_switch) || {};
  const killedSkillsMap = killSwitchState.killed_skills || {};
  if (skRes.ok && skRes.data) {
    const globalKilled = Boolean(killSwitchState.global_engaged);
    badge.textContent = globalKilled ? "KILL-SWITCH ENGAGED" : "SANDBOX ARMED";
    badge.className = globalKilled ? "badge danger" : "badge ok";
  }

  if (pbSelect && candRes.ok && Array.isArray(candRes.data?.candidates)) {
    pbSelect.replaceChildren();
    for (const c of candRes.data.candidates) {
      const opt = el("option", "", `${c.playbook_id} (${c.title || c.playbook_id}) [${shortSha(c.playbook_sha256)}]`);
      opt.value = c.playbook_id;
      opt.dataset.sha256 = c.playbook_sha256 || "";
      opt.dataset.stepsCount = String((c.steps || []).length || 1);
      pbSelect.appendChild(opt);
    }
  }

  grid.replaceChildren();
  const skills = (skRes.ok && Array.isArray(skRes.data?.skills)) ? skRes.data.skills : [];
  for (const sk of skills) {
    const card = el("div", "card");
    const hdr = el("div", "toolbar");
    hdr.appendChild(el("strong", "", sk.title || sk.skill_id));
    hdr.appendChild(el("span", `badge ${sk.valid ? "ok" : "warn"}`, sk.status || "unknown"));
    const schemaKeys = Object.keys(sk.input_schema || {});
    if (schemaKeys.length) {
      hdr.appendChild(el("span", "badge mono", `inputs:${schemaKeys.length}`));
    }
    card.appendChild(hdr);
    const stepCount = Array.isArray(sk.step_actions)
      ? sk.step_actions.length
      : Array.isArray(sk.steps)
        ? sk.steps.length
        : 0;
    card.appendChild(
      el(
        "div",
        "note-item-meta",
        `ID: ${sk.skill_id} · Playbook: ${sk.playbook_id} · Steps: ${stepCount} · SHA ${shortSha(sk.sha256)}`
      )
    );
    const bar = el("div", "toolbar");
    const dryBtn = el("button", "btn sm", schemaKeys.length ? "Dry Run (Inputs)..." : "Dry Run");
    dryBtn.type = "button";
    dryBtn.addEventListener("click", () => openSkillExecutePrompt(sk, true));
    const runBtn = el("button", "btn sm primary", schemaKeys.length ? "Execute (Inputs)..." : "Execute in Sandbox");
    runBtn.type = "button";
    runBtn.addEventListener("click", () => openSkillExecutePrompt(sk, false));
    bar.appendChild(dryBtn);
    bar.appendChild(runBtn);

    const perSkillKilled = Boolean(killedSkillsMap[sk.skill_id]) || sk.status === "killed";
    const ksBtn = el("button", `btn sm ${perSkillKilled ? "good" : "warn"}`, perSkillKilled ? "Unkill Skill" : "Kill Skill");
    ksBtn.type = "button";
    ksBtn.addEventListener("click", async () => {
      const actor = (document.getElementById("skillActorInput")?.value || "desktop_owner").trim();
      const kr = await window.secondBrain.request("POST", "/v1/skills/kill-switch", {
        engaged: !perSkillKilled,
        scope: sk.skill_id,
        actor,
        reason: `${!perSkillKilled ? "Engaged" : "Released"} per-skill kill-switch from Desktop UI`,
      });
      showToast(
        kr.ok ? `Skill ${sk.skill_id} kill-switch ${!perSkillKilled ? "engaged" : "released"}` : apiErrorMessage(kr, "Kill-switch failed"),
        kr.ok ? (!perSkillKilled ? "warn" : "ok") : "danger"
      );
      loadExecutableSkills();
    });
    bar.appendChild(ksBtn);

    if (sk.status === "active") {
      const revBtn = el("button", "btn sm danger", "Revoke");
      revBtn.type = "button";
      revBtn.addEventListener("click", async () => {
        const actor = (document.getElementById("skillActorInput")?.value || "desktop_owner").trim();
        const r = await window.secondBrain.request(
          "POST",
          `/v1/skills/${encodeURIComponent(sk.skill_id)}/revoke`,
          {
            expected_skill_sha256: sk.sha256,
            actor,
            reason: "Revoked by owner from Desktop UI",
          }
        );
        if (r.ok) {
          showToast(`Revoked skill ${sk.skill_id}`, "ok");
          loadExecutableSkills();
        } else {
          showToast(apiErrorMessage(r, "Revoke failed"), "danger");
        }
      });
      bar.appendChild(revBtn);
    }
    card.appendChild(bar);
    grid.appendChild(card);
  }

  if (execWrap && exRes.ok && exRes.data) {
    const chain = exRes.data.audit_chain || {};
    const execs = Array.isArray(exRes.data.executions) ? exRes.data.executions : [];
    execWrap.replaceChildren();
    execWrap.appendChild(
      el(
        "div",
        "note-item-meta",
        `Audit Chain: ${chain.chain_valid ? "VALID" : "INVALID"} (${chain.entry_count ?? 0} hash-chained execution records)`
      )
    );
    for (const ex of execs.slice(0, 6)) {
      execWrap.appendChild(
        el(
          "div",
          "note-item-meta",
          `[${ex.status}] ${ex.skill_id} · actor=${ex.actor} · rollback=${Boolean(ex.rollback_executed)} · sha=${shortSha(ex.execution_sha256)}`
        )
      );
    }
  }
}

function openSkillExecutePrompt(sk, dryRun) {
  const schema = sk && typeof sk.input_schema === "object" && sk.input_schema ? sk.input_schema : {};
  const keys = Object.keys(schema);
  if (!keys.length) {
    executeSkillAction(sk.skill_id, dryRun, {});
    return;
  }

  const controls = {};
  let jsonOverrideArea = null;

  modalCtrl.open({
    title: `${dryRun ? "Dry Run" : "Execute"} Skill: ${sk.skill_id}`,
    renderBody(body, doc) {
      const intro = el(
        "div",
        "note-item-meta",
        `Provide validated runtime inputs matching ${sk.skill_id}'s input_schema before ${dryRun ? "dry-running" : "executing in the sandbox"}.`
      );
      body.appendChild(intro);
      for (const pname of keys) {
        const spec = schema[pname] || {};
        const ptype = String(spec.type || "string").toLowerCase();
        const col = el("div", "field-col");
        col.style.marginTop = "8px";
        const lbl = el(
          "span",
          "field-label",
          `${pname} (${ptype}${spec.required ? ", required" : ""}${spec.pattern ? ` · pattern ${spec.pattern}` : ""})`
        );
        col.appendChild(lbl);
        let inputEl;
        if (ptype === "boolean") {
          const checkWrap = el("label", "inline-check");
          inputEl = doc.createElement("input");
          inputEl.type = "checkbox";
          inputEl.checked = Boolean(spec.default);
          checkWrap.appendChild(inputEl);
          checkWrap.appendChild(doc.createTextNode(String(spec.default ?? false)));
          col.appendChild(checkWrap);
        } else if (Array.isArray(spec.enum) && spec.enum.length) {
          inputEl = doc.createElement("select");
          for (const ev of spec.enum) {
            const opt = doc.createElement("option");
            opt.value = String(ev);
            opt.textContent = String(ev);
            if (spec.default !== undefined && String(spec.default) === String(ev)) {
              opt.selected = true;
            }
            inputEl.appendChild(opt);
          }
          col.appendChild(inputEl);
        } else {
          inputEl = doc.createElement("input");
          inputEl.type = ptype === "integer" || ptype === "number" ? "number" : "text";
          if (ptype === "integer") inputEl.step = "1";
          if (spec.default !== undefined && spec.default !== null) {
            inputEl.value = String(spec.default);
          } else {
            inputEl.placeholder = `Enter ${pname}...`;
          }
          col.appendChild(inputEl);
        }
        controls[pname] = { inputEl, spec, ptype };
        body.appendChild(col);
      }

      const ovCol = el("div", "field-col");
      ovCol.style.marginTop = "10px";
      ovCol.appendChild(el("span", "field-label", "Optional Raw JSON Inputs Override (overrides fields above if non-empty)"));
      jsonOverrideArea = doc.createElement("textarea");
      jsonOverrideArea.rows = 2;
      jsonOverrideArea.placeholder = '{"service": "shards"}';
      ovCol.appendChild(jsonOverrideArea);
      body.appendChild(ovCol);
    },
    renderFooter(footer, doc) {
      const cancelBtn = doc.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "btn";
      cancelBtn.textContent = "Cancel";
      cancelBtn.addEventListener("click", () => modalCtrl.close());

      const runBtn = doc.createElement("button");
      runBtn.type = "button";
      runBtn.className = dryRun ? "btn primary" : "btn good";
      runBtn.textContent = dryRun ? "Run Dry-Run" : "Execute in Sandbox";
      runBtn.addEventListener("click", () => {
        let collected = {};
        const rawJson = (jsonOverrideArea?.value || "").trim();
        if (rawJson) {
          try {
            collected = JSON.parse(rawJson);
          } catch (err) {
            showToast(`Invalid inputs JSON: ${err.message}`, "danger");
            return;
          }
          if (!collected || typeof collected !== "object" || Array.isArray(collected)) {
            showToast("Inputs JSON must be an object.", "danger");
            return;
          }
        } else {
          for (const [pname, entry] of Object.entries(controls)) {
            const { inputEl, spec, ptype } = entry;
            if (ptype === "boolean") {
              collected[pname] = Boolean(inputEl.checked);
            } else {
              const rawVal = String(inputEl.value ?? "").trim();
              if (!rawVal && !spec.required && spec.default === undefined) {
                continue;
              }
              if (ptype === "integer") {
                collected[pname] = rawVal ? parseInt(rawVal, 10) : spec.default;
              } else if (ptype === "number") {
                collected[pname] = rawVal ? Number(rawVal) : spec.default;
              } else {
                collected[pname] = rawVal;
              }
            }
          }
        }
        modalCtrl.close({ fromCancel: false });
        executeSkillAction(sk.skill_id, dryRun, collected);
      });

      footer.appendChild(cancelBtn);
      footer.appendChild(runBtn);
    },
  });
}

async function executeSkillAction(skillId, dryRun, inputs = {}) {
  const outEl = document.getElementById("executableSkillsOutput");
  const actor = (document.getElementById("skillActorInput")?.value || "desktop_owner").trim();
  if (outEl) {
    outEl.textContent = `${dryRun ? "Dry-running" : "Executing"} ${skillId} inside isolated sandbox...`;
  }
  const res = await window.secondBrain.request(
    "POST",
    `/v1/skills/${encodeURIComponent(skillId)}/execute`,
    { actor, dry_run: Boolean(dryRun), inputs: inputs || {} }
  );
  if (res.ok && res.data) {
    const d = res.data;
    if (outEl) {
      outEl.textContent = `Execution ${d.execution_id || ""}: status=${d.status} (${d.duration_ms ?? 0} ms, audit=${shortSha(d.execution_sha256)})`;
    }
    showToast(`Skill ${skillId}: ${d.status}`, d.status === "completed" || d.status === "dry_run_verified" ? "ok" : "warn");
    loadExecutableSkills();
  } else {
    const msg = apiErrorMessage(res, "Skill execution denied");
    if (outEl) outEl.textContent = msg;
    showToast(msg, "danger");
    loadExecutableSkills();
  }
}

document.getElementById("refreshSkillsBtn")?.addEventListener("click", loadExecutableSkills);
document.getElementById("skillPrefillAuditActionsBtn")?.addEventListener("click", () => {
  const pbSelect = document.getElementById("skillPlaybookSelect");
  const opt = pbSelect?.selectedOptions?.[0];
  const count = Math.max(1, Number(opt?.dataset?.stepsCount || 1));
  const actions = [];
  for (let i = 0; i < count; i += 1) {
    actions.push({
      action_type: "vault_audit",
      audit_kind: "sources",
      output_file: `step_${i + 1}_audit.json`,
    });
  }
  const area = document.getElementById("skillActionsJsonInput");
  if (area) area.value = JSON.stringify(actions, null, 2);
});

document.getElementById("promoteExecutableSkillBtn")?.addEventListener("click", async () => {
  const outEl = document.getElementById("executableSkillsOutput");
  const pbSelect = document.getElementById("skillPlaybookSelect");
  const opt = pbSelect?.selectedOptions?.[0];
  const skillId = (document.getElementById("skillIdInput")?.value || "").trim();
  const playbookId = pbSelect?.value || "";
  const expectedSha = opt?.dataset?.sha256 || "";
  const rawActions = (document.getElementById("skillActionsJsonInput")?.value || "").trim();
  const rawSchema = (document.getElementById("skillInputSchemaJsonInput")?.value || "").trim();
  if (!skillId || !playbookId || !rawActions) {
    showToast("Provide Skill ID, Backing Playbook, and Step Actions JSON.", "warn");
    return;
  }
  let parsedActions;
  try {
    parsedActions = JSON.parse(rawActions);
  } catch (err) {
    showToast(`Invalid Step Actions JSON: ${err.message}`, "danger");
    return;
  }
  let parsedSchema = {};
  if (rawSchema) {
    try {
      parsedSchema = JSON.parse(rawSchema);
    } catch (err) {
      showToast(`Invalid Input Schema JSON: ${err.message}`, "danger");
      return;
    }
  }
  const res = await window.secondBrain.request("POST", "/v1/skills/promote", {
    skill_id: skillId,
    playbook_id: playbookId,
    expected_playbook_sha256: expectedSha,
    input_schema: parsedSchema,
    step_actions: parsedActions,
    timeout_seconds: Number(document.getElementById("skillTimeoutInput")?.value || 15),
    actor: (document.getElementById("skillActorInput")?.value || "desktop_owner").trim(),
    rationale: (document.getElementById("skillRationaleInput")?.value || "Owner Desktop promotion").trim(),
    allow_early_owner_override: Boolean(document.getElementById("skillAllowEarlyCheck")?.checked),
  });
  if (res.ok && res.data) {
    if (outEl) outEl.textContent = `Promoted Executable Skill ${res.data.skill_id} (SHA ${shortSha(res.data.sha256)})`;
    showToast(`Promoted ${res.data.skill_id} ✓`, "ok");
    loadExecutableSkills();
  } else {
    const msg = apiErrorMessage(res, "Skill promotion failed");
    if (outEl) outEl.textContent = msg;
    showToast(msg, "danger");
  }
});

document.getElementById("skillEngageGlobalKillBtn")?.addEventListener("click", async () => {
  const actor = (document.getElementById("skillActorInput")?.value || "desktop_owner").trim();
  const res = await window.secondBrain.request("POST", "/v1/skills/kill-switch", {
    engaged: true,
    scope: "global",
    actor,
    reason: "Emergency global kill-switch engaged from Desktop UI",
  });
  if (res.ok) {
    showToast("Global Executable Skill Kill-Switch ENGAGED", "warn");
    loadExecutableSkills();
  } else {
    showToast(apiErrorMessage(res, "Kill-switch update failed"), "danger");
  }
});

document.getElementById("skillReleaseGlobalKillBtn")?.addEventListener("click", async () => {
  const actor = (document.getElementById("skillActorInput")?.value || "desktop_owner").trim();
  const res = await window.secondBrain.request("POST", "/v1/skills/kill-switch", {
    engaged: false,
    scope: "global",
    actor,
    reason: "Global kill-switch released by owner",
  });
  if (res.ok) {
    showToast("Global Kill-Switch released ✓", "ok");
    loadExecutableSkills();
  } else {
    showToast(apiErrorMessage(res, "Kill-switch release failed"), "danger");
  }
});

// ─── Gen-4 Cognition Workbench & Predictive Context Signal ───────────────────

let cognitiveSignalDebounceTimer = null;

function emitCognitiveContextSignal({ activeFile = "", projectScope = "", cursorTopic = "" } = {}) {
  if (!window.secondBrain || typeof window.secondBrain.request !== "function") return;
  const cleanFile = String(activeFile || "").trim().slice(0, 260);
  const cleanScope = String(projectScope || "").trim().slice(0, 120);
  const cleanTopic = String(cursorTopic || "").trim().slice(0, 260);
  if (!cleanFile && !cleanScope && !cleanTopic) return;

  if (cognitiveSignalDebounceTimer) {
    clearTimeout(cognitiveSignalDebounceTimer);
  }
  cognitiveSignalDebounceTimer = setTimeout(() => {
    cognitiveSignalDebounceTimer = null;
    const payload = { prewarm: true };
    if (cleanFile) payload.active_file = cleanFile;
    if (cleanScope) payload.project_scope = cleanScope;
    if (cleanTopic) payload.cursor_topic = cleanTopic;
    Promise.resolve()
      .then(() => window.secondBrain.request("POST", "/v1/cognitive/context-signal", payload))
      .catch(() => {});
  }, 1200);
}

for (const scopeInputId of ["chatScopeInput", "graphProjectInput", "planScopeInput"]) {
  const scopeEl = document.getElementById(scopeInputId);
  if (scopeEl) {
    scopeEl.addEventListener("change", () => {
      const val = scopeEl.value.trim();
      if (val) {
        emitCognitiveContextSignal({ projectScope: val, cursorTopic: val });
      }
    });
  }
}

document.getElementById("sidebarVaultSelect")?.addEventListener("change", (evt) => {
  const vid = String(evt?.target?.value || "").trim();
  if (vid) {
    emitCognitiveContextSignal({ cursorTopic: `vault:${vid}` });
  }
});

async function runGen4Zoom(parentId = "") {
  const outEl = document.getElementById("gen4ZoomOutput");
  if (!outEl) return;
  const query = (document.getElementById("gen4ZoomQueryInput")?.value || "").trim();
  const targetLevel = document.getElementById("gen4ZoomLevelSelect")?.value || "L5";
  const project = (document.getElementById("gen4ZoomProjectInput")?.value || "").trim();

  outEl.replaceChildren(el("div", "note-item-meta", "Running Fractal Zoom recall..."));
  const body = { query, target_level: targetLevel, limit: 8 };
  if (parentId) body.parent_id = parentId;
  if (project) body.project = project;

  const res = await window.secondBrain.request("POST", "/v1/recall/zoom", body);
  outEl.replaceChildren();
  if (!res.ok || !res.data) {
    outEl.appendChild(
      el("div", "status-banner banner-danger", apiErrorMessage(res, "Fractal Zoom failed"))
    );
    return;
  }

  const d = res.data;
  const pathSteps = Array.isArray(d.path) ? d.path : [];
  if (pathSteps.length) {
    const crumb = el("div", "toolbar");
    crumb.appendChild(el("span", "field-label", "Zoom Path:"));
    for (const st of pathSteps) {
      crumb.appendChild(
        el(
          "span",
          "badge",
          `${st.level} (${st.level_name || ""}: ${(st.selected_ids || []).length})`
        )
      );
    }
    outEl.appendChild(crumb);
  }

  const nodes = Array.isArray(d.nodes) ? d.nodes : [];
  if (!nodes.length) {
    outEl.appendChild(
      el("div", "note-item-meta", "No matching owner-approved nodes at this zoom level.")
    );
    return;
  }

  const grid = el("div", "cards-grid");
  grid.style.marginTop = "6px";
  for (const node of nodes) {
    const card = el("div", "card");
    const hdr = el("div", "toolbar");
    hdr.appendChild(el("span", "badge ok", `${node.level || ""} ${node.level_name || ""}`.trim()));
    hdr.appendChild(el("strong", "", node.title || node.node_id || "Node"));
    if (node.score !== undefined && node.score !== null) {
      hdr.appendChild(el("span", "badge mono", `score:${node.score}`));
    }
    card.appendChild(hdr);

    if (node.summary) {
      card.appendChild(el("div", "", node.summary));
    }
    const metaParts = [
      node.domain ? `domain:${node.domain}` : "",
      node.project_scope ? `scope:${node.project_scope}` : "",
      node.source_path ? `src:${node.source_path}` : "",
      node.byte_end > node.byte_start ? `bytes:[${node.byte_start}..${node.byte_end}]` : "",
    ].filter(Boolean);
    if (metaParts.length) {
      card.appendChild(el("div", "note-item-meta", metaParts.join(" · ")));
    }

    const bar = el("div", "toolbar");
    if (node.atom_filename) {
      const atomBtn = el("button", "btn sm", `Inspect ${node.atom_filename}`);
      atomBtn.type = "button";
      atomBtn.addEventListener("click", () => openAtomInspectorModal(node.atom_filename));
      bar.appendChild(atomBtn);
    }
    if (node.level && node.level !== "L5" && node.node_id) {
      const drillBtn = el("button", "btn sm primary", "Drill Down");
      drillBtn.type = "button";
      drillBtn.addEventListener("click", () => runGen4Zoom(node.node_id));
      bar.appendChild(drillBtn);
    }
    if (bar.childNodes.length) card.appendChild(bar);
    grid.appendChild(card);
  }
  outEl.appendChild(grid);
}

async function inspectGen4Concept(conceptId) {
  const detailEl = document.getElementById("gen4ConceptDetail");
  if (!detailEl || !conceptId) return;
  detailEl.style.display = "block";
  detailEl.replaceChildren(el("div", "note-item-meta", `Loading concept ${conceptId}...`));
  const project = (document.getElementById("gen4ConceptsProjectInput")?.value || "").trim();
  const qs = project ? `?project=${encodeURIComponent(project)}` : "";
  const res = await window.secondBrain.request(
    "GET",
    `/v1/concepts/${encodeURIComponent(conceptId)}${qs}`
  );
  detailEl.replaceChildren();
  if (!res.ok || !res.data) {
    detailEl.appendChild(
      el("div", "status-banner banner-danger", apiErrorMessage(res, "Failed to inspect concept"))
    );
    return;
  }
  const c = res.data;
  const top = el("div", "toolbar");
  top.appendChild(el("strong", "", `Living Concept: ${c.name || c.concept_id}`));
  top.appendChild(el("span", "badge ok", `${c.atom_count ?? 0} atoms`));
  top.appendChild(el("span", "badge mono", `conf:${Number(c.current_confidence ?? 0).toFixed(2)}`));
  const closeBtn = el("button", "btn sm", "Close");
  closeBtn.type = "button";
  closeBtn.style.marginLeft = "auto";
  closeBtn.addEventListener("click", () => {
    detailEl.style.display = "none";
  });
  top.appendChild(closeBtn);
  detailEl.appendChild(top);

  const edges = Array.isArray(c.edges) ? c.edges : [];
  if (edges.length) {
    detailEl.appendChild(el("div", "field-label", `Causal / Semantic Edges (${edges.length}):`));
    for (const ed of edges.slice(0, 10)) {
      detailEl.appendChild(
        el(
          "div",
          "note-item-meta",
          `${ed.src_concept} —[${ed.relation}]→ ${ed.dst_concept} (w=${Number(ed.weight ?? 0).toFixed(2)})`
        )
      );
    }
  } else {
    detailEl.appendChild(el("div", "note-item-meta", "No typed concept edges recorded yet."));
  }

  const timeline = Array.isArray(c.confidence_timeline) ? c.confidence_timeline : [];
  if (timeline.length) {
    detailEl.appendChild(
      el(
        "div",
        "note-item-meta",
        `Confidence Timeline: ${timeline
          .map((pt) => `${pt.quarter || pt.period || "?"}:${Number(pt.confidence ?? pt.avg_confidence ?? 0).toFixed(2)}`)
          .join(" · ")}`
      )
    );
  }
}

async function loadGen4Concepts() {
  const listEl = document.getElementById("gen4ConceptsList");
  if (!listEl) return;
  const project = (document.getElementById("gen4ConceptsProjectInput")?.value || "").trim();
  const qs = project ? `?project=${encodeURIComponent(project)}&limit=30` : "?limit=30";
  const res = await window.secondBrain.request("GET", `/v1/concepts${qs}`);
  listEl.replaceChildren();
  if (!res.ok || !res.data) {
    listEl.appendChild(
      el("div", "note-item-meta", apiErrorMessage(res, "Living Concepts unavailable"))
    );
    return;
  }
  const concepts = Array.isArray(res.data.concepts) ? res.data.concepts : [];
  if (!concepts.length) {
    listEl.appendChild(el("div", "note-item-meta", "No Living Concepts indexed yet."));
    return;
  }
  for (const c of concepts.slice(0, 16)) {
    const card = el("div", "card");
    const hdr = el("div", "toolbar");
    hdr.appendChild(el("strong", "", c.name || c.concept_id || "Concept"));
    hdr.appendChild(el("span", "badge ok", `${c.atom_count ?? 0} atoms`));
    hdr.appendChild(
      el("span", "badge mono", `conf:${Number(c.current_confidence ?? 0).toFixed(2)}`)
    );
    card.appendChild(hdr);
    const meta = [
      Array.isArray(c.projects) && c.projects.length ? `projects: ${c.projects.join(", ")}` : "",
      `playbooks:${(c.playbooks || []).length}`,
      `mistakes:${(c.mistakes || []).length}`,
      `questions:${(c.open_questions || []).length}`,
    ]
      .filter(Boolean)
      .join(" · ");
    card.appendChild(el("div", "note-item-meta", meta));

    const bar = el("div", "toolbar");
    const inspBtn = el("button", "btn sm", "Inspect Edges & Timeline");
    inspBtn.type = "button";
    inspBtn.addEventListener("click", () => inspectGen4Concept(c.concept_id));
    bar.appendChild(inspBtn);
    const simBtn = el("button", "btn sm", "Simulate Impact");
    simBtn.type = "button";
    simBtn.addEventListener("click", () => {
      const targetEl = document.getElementById("gen4SimTargetInput");
      if (targetEl) targetEl.value = c.name || c.concept_id || "";
      runGen4ImpactSimulation();
    });
    bar.appendChild(simBtn);
    card.appendChild(bar);
    listEl.appendChild(card);
  }
}

async function promoteGen4DistilledArtifact(kind, item, action) {
  const outEl = document.getElementById("gen4DistillOutput");
  const actor = (document.getElementById("gen4DistillActorInput")?.value || "desktop_owner").trim();
  const reason = (document.getElementById("gen4DistillReasonInput")?.value || "").trim();
  const basePath = kind === "principle" ? "/v1/principles/" : "/v1/mental-models/";
  const res = await window.secondBrain.request(
    "POST",
    `${basePath}${encodeURIComponent(item.filename)}/promote`,
    {
      expected_sha256: item.sha256,
      actor,
      action,
      reason: reason || `Owner ${action} via Desktop Gen-4 Workbench`,
    }
  );
  if (res.ok) {
    const msg = `${action === "promote" ? "Promoted" : "Revoked"} ${item.filename} (CAS verified).`;
    if (outEl) outEl.textContent = msg;
    showToast(msg, "ok");
    loadGen4DistilledArtifacts();
  } else {
    const msg = apiErrorMessage(res, `Failed to ${action} ${item.filename}`);
    if (outEl) outEl.textContent = msg;
    showToast(msg, "danger");
  }
}

function renderDistilledArtifactCards(containerEl, items, kind) {
  if (!containerEl) return;
  containerEl.replaceChildren();
  if (!items.length) {
    containerEl.appendChild(
      el(
        "div",
        "note-item-meta",
        kind === "principle"
          ? "No principles staged in 7-principles/ yet."
          : "No mental models staged in 8-mental-models/ yet."
      )
    );
    return;
  }
  for (const item of items) {
    const card = el("div", "card");
    const hdr = el("div", "toolbar");
    hdr.appendChild(el("strong", "", item.title || item.filename));
    const isActive = item.status === "active" && item.valid;
    hdr.appendChild(el("span", `badge ${isActive ? "ok" : "warn"}`, item.status || "draft"));
    if (item.sha256) {
      hdr.appendChild(el("span", "badge mono", `sha:${shortSha(item.sha256)}`));
    }
    card.appendChild(hdr);
    if (item.statement) {
      card.appendChild(el("div", "", String(item.statement).slice(0, 320)));
    }
    card.appendChild(
      el(
        "div",
        "note-item-meta",
        `${item.filename} · backing atoms: ${(item.source_atoms || []).length}${
          Array.isArray(item.projects) && item.projects.length
            ? ` · projects: ${item.projects.join(", ")}`
            : ""
        }${item.reason ? ` · ${item.reason}` : ""}`
      )
    );
    const bar = el("div", "toolbar");
    if (item.status !== "active") {
      const promBtn = el("button", "btn sm good", "Promote (CAS)");
      promBtn.type = "button";
      promBtn.addEventListener("click", () => promoteGen4DistilledArtifact(kind, item, "promote"));
      bar.appendChild(promBtn);
    } else {
      const revBtn = el("button", "btn sm warn", "Revoke (CAS)");
      revBtn.type = "button";
      revBtn.addEventListener("click", () => promoteGen4DistilledArtifact(kind, item, "revoke"));
      bar.appendChild(revBtn);
    }
    card.appendChild(bar);
    containerEl.appendChild(card);
  }
}

async function loadGen4DistilledArtifacts() {
  const prEl = document.getElementById("gen4PrinciplesList");
  const mmEl = document.getElementById("gen4MentalModelsList");
  const outEl = document.getElementById("gen4DistillOutput");
  const [prRes, mmRes] = await Promise.all([
    window.secondBrain.request("GET", "/v1/principles?active_only=false"),
    window.secondBrain.request("GET", "/v1/mental-models?active_only=false"),
  ]);
  const principles = prRes.ok && Array.isArray(prRes.data?.principles) ? prRes.data.principles : [];
  const models = mmRes.ok && Array.isArray(mmRes.data?.mental_models) ? mmRes.data.mental_models : [];
  renderDistilledArtifactCards(prEl, principles, "principle");
  renderDistilledArtifactCards(mmEl, models, "mental_model");
  if (outEl) {
    outEl.textContent = `Loaded ${principles.length} principle(s) and ${models.length} mental model(s) (active + draft).`;
  }
}

async function runGen4ImpactSimulation() {
  const outEl = document.getElementById("gen4SimOutput");
  if (!outEl) return;
  const target = (document.getElementById("gen4SimTargetInput")?.value || "").trim();
  if (!target) {
    showToast("Enter a target concept or module name to simulate impact.", "warn");
    return;
  }
  const maxHops = Number(document.getElementById("gen4SimHopsSelect")?.value || 3);
  const project = (document.getElementById("gen4SimProjectInput")?.value || "").trim();
  outEl.replaceChildren(el("div", "note-item-meta", `Simulating downstream impact for '${target}'...`));
  const payload = { target, max_hops: maxHops };
  if (project) payload.project = project;
  const res = await window.secondBrain.request("POST", "/v1/cognitive/simulate", payload);
  outEl.replaceChildren();
  if (!res.ok || !res.data) {
    outEl.appendChild(
      el("div", "status-banner banner-danger", apiErrorMessage(res, "Impact simulation failed"))
    );
    return;
  }
  const d = res.data;
  const prov = d.provenance_summary || {};
  const summaryRow = el("div", "toolbar");
  summaryRow.appendChild(el("strong", "", `Target: ${d.target || target}`));
  summaryRow.appendChild(el("span", "badge", `Impacted: ${d.impacted_count ?? 0}`));
  summaryRow.appendChild(
    el("span", "badge ok", `verified_vault_edge: ${prov.verified_vault_edge ?? 0}`)
  );
  summaryRow.appendChild(
    el("span", "badge", `code_ast_dependency: ${prov.code_ast_dependency ?? 0}`)
  );
  summaryRow.appendChild(
    el("span", "badge warn", `inferred_risk: ${prov.inferred_risk ?? 0}`)
  );
  outEl.appendChild(summaryRow);

  const nodes = Array.isArray(d.impacted_nodes) ? d.impacted_nodes : [];
  if (!nodes.length) {
    outEl.appendChild(
      el("div", "note-item-meta", "No downstream impacted concepts, AST dependents, or historical failures found.")
    );
    return;
  }
  for (const imp of nodes) {
    const row = el("div", "card");
    row.style.marginTop = "6px";
    const hdr = el("div", "toolbar");
    hdr.appendChild(el("span", "badge mono", `hop ${imp.hop ?? 1}`));
    hdr.appendChild(el("strong", "", imp.node || imp.node_key || "Node"));
    const pCls =
      imp.provenance === "verified_vault_edge"
        ? "ok"
        : imp.provenance === "inferred_risk"
        ? "warn"
        : "";
    hdr.appendChild(el("span", `badge ${pCls}`, imp.provenance || "edge"));
    if (imp.relation) {
      hdr.appendChild(el("span", "note-item-meta", imp.relation));
    }
    row.appendChild(hdr);
    if (imp.evidence) {
      const evText = Array.isArray(imp.evidence) ? imp.evidence.join(", ") : String(imp.evidence);
      if (evText) row.appendChild(el("div", "note-item-meta", `Evidence: ${evText}`));
    }
    outEl.appendChild(row);
  }
}

async function loadGen4Curiosity() {
  const outEl = document.getElementById("gen4CuriosityOutput");
  if (!outEl) return;
  const res = await window.secondBrain.request("GET", "/v1/cognitive/curiosity");
  outEl.replaceChildren();
  if (!res.ok || !res.data) {
    outEl.appendChild(
      el("div", "note-item-meta", apiErrorMessage(res, "Self-Model & Curiosity unavailable"))
    );
    return;
  }
  const domains = Array.isArray(res.data.domains) ? res.data.domains : [];
  const targets = Array.isArray(res.data.curiosity_targets) ? res.data.curiosity_targets : [];

  const kpiRow = el("div", "toolbar");
  kpiRow.appendChild(el("span", "badge", `Tracked Domains: ${domains.length}`));
  kpiRow.appendChild(
    el("span", `badge ${targets.length ? "warn" : "ok"}`, `Curiosity Gaps: ${targets.length}`)
  );
  outEl.appendChild(kpiRow);

  if (domains.length) {
    outEl.appendChild(el("div", "field-label", "Domain Competence Profile:"));
    for (const dom of domains.slice(0, 12)) {
      outEl.appendChild(
        el(
          "div",
          "note-item-meta",
          `${dom.domain}: competence=${Number(dom.competence_score ?? 0).toFixed(2)} · atoms=${dom.atom_count ?? 0} · playbooks=${dom.playbook_count ?? 0} · failures=${dom.failure_count ?? 0}`
        )
      );
    }
  }

  if (targets.length) {
    outEl.appendChild(el("div", "field-label", "Prioritized Curiosity / Study Targets:"));
    for (const t of targets.slice(0, 10)) {
      const item = el("div", "card");
      item.style.marginTop = "6px";
      const hdr = el("div", "toolbar");
      hdr.appendChild(el("strong", "", t.goal_title || t.domain || "Study Gap"));
      hdr.appendChild(el("span", "badge warn", `priority:${Number(t.priority_score ?? 0).toFixed(2)}`));
      hdr.appendChild(
        el("span", "badge mono", `${t.current_atoms ?? 0}/${t.target_atoms ?? 0} atoms (deficit ${t.deficit ?? 0})`)
      );
      item.appendChild(hdr);
      if (t.suggested_scout_query) {
        item.appendChild(el("div", "note-item-meta", `Suggested study query: ${t.suggested_scout_query}`));
      }
      outEl.appendChild(item);
    }
  } else {
    outEl.appendChild(
      el("div", "note-item-meta", "No directed curiosity gaps flagged across tracked domains.")
    );
  }
}

function loadGen4Workbench() {
  if (!window.secondBrain || typeof window.secondBrain.request !== "function") return;
  loadGen4Concepts();
  loadGen4DistilledArtifacts();
  loadGen4Curiosity();
}

document.getElementById("gen4RefreshAllBtn")?.addEventListener("click", loadGen4Workbench);
document.getElementById("gen4ZoomRunBtn")?.addEventListener("click", () => runGen4Zoom(""));
document.getElementById("gen4ZoomQueryInput")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") runGen4Zoom("");
});
document.getElementById("gen4ConceptsRefreshBtn")?.addEventListener("click", loadGen4Concepts);
document.getElementById("gen4DistillRefreshBtn")?.addEventListener("click", loadGen4DistilledArtifacts);
document.getElementById("gen4DistillRunBtn")?.addEventListener("click", async () => {
  const outEl = document.getElementById("gen4DistillOutput");
  const res = await window.secondBrain.request("POST", "/v1/principles/distill", {});
  if (res.ok && res.data) {
    const pCnt = (res.data.principles_created || []).length;
    const mCnt = (res.data.mental_models_created || []).length;
    const msg = `Distilled ${pCnt} new principle draft(s) and ${mCnt} mental model draft(s).`;
    if (outEl) outEl.textContent = msg;
    showToast(msg, "ok");
    loadGen4DistilledArtifacts();
  } else {
    const msg = apiErrorMessage(res, "Distillation failed");
    if (outEl) outEl.textContent = msg;
    showToast(msg, "danger");
  }
});
document.getElementById("gen4SimRunBtn")?.addEventListener("click", runGen4ImpactSimulation);
document.getElementById("gen4SimTargetInput")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") runGen4ImpactSimulation();
});
document.getElementById("gen4CuriosityRefreshBtn")?.addEventListener("click", loadGen4Curiosity);

// ─── Global Shortcuts & Init ─────────────────────────────────────────────────

window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && modalCtrl.isOpen()) {
    closeModal();
    return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    if (activeView === "notes") saveActiveNote();
  }
  const keyMap = {
    1: "notes",
    2: "chat",
    3: "review",
    4: "import",
    5: "ide",
    6: "system",
    7: "planner",
    8: "proof",
    9: "graph",
    0: "vault",
  };
  if ((e.ctrlKey || e.metaKey) && Object.prototype.hasOwnProperty.call(keyMap, e.key)) {
    e.preventDefault();
    switchView(keyMap[e.key]);
  }
});

if (window.secondBrain) {
  if (window.secondBrain.platform) {
    ModalHelper.applyPlatformShortcutsToDom(
      document,
      window.secondBrain.platform
    );
  }
  if (typeof window.secondBrain.onRefresh === "function") {
    window.secondBrain.onRefresh(() => {
      loadNotes({ append: false });
      refreshHealth();
      loadWorkspaces();
      refreshActiveViewForCurrentVault();
    });
  }
  refreshHealth();
  loadNotes({ append: false });
  loadWorkspaces();
  refreshActiveViewForCurrentVault();
}
