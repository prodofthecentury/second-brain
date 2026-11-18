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
  modalBodyEl.appendChild(info);
  modalBodyEl.appendChild(claimWrap);
  modalBodyEl.appendChild(ta);

  const cancelBtn = el("button", "btn", "Cancel");
  cancelBtn.type = "button";
  cancelBtn.onclick = closeModal;

  const saveBtn = el("button", "btn primary", "Save Atom (CAS)");
  saveBtn.type = "button";
  saveBtn.onclick = async () => {
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving...";
    try {
      const res = await window.secondBrain.api(
        "PUT",
        `/v1/atoms/${encodeURIComponent(filename)}`,
        {
          content: ta.value,
          expected_sha256: fullAtom.sha256 || "",
          actor: "desktop_owner",
        }
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
  modalFooterEl.replaceChildren(cancelBtn, saveBtn);
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
  if (name === "import") loadScoutQueue();
  if (name === "ide") loadIdeStatus();
  if (name === "system") loadSystemView();
  if (name === "planner") loadPlannerView();
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
    loadChatHosts();
    if (activeView === "review") loadReviewQueue();
    if (activeView === "import") loadScoutQueue();
    if (activeView === "ide") loadIdeStatus();
    if (activeView === "system") loadSystemView();
    if (activeView === "planner") loadPlannerView();
    if (activeView === "proof") loadProofView();
    if (activeView === "graph") loadGraphView();
    if (activeView === "vault") loadVaultBrowser({ append: false });
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

  try {
    const res = await window.secondBrain.request(
      "POST",
      `/v1/notes/${encodeURIComponent(activeNote.filename)}/verify`,
      payload
    );
    if (res.ok) {
      const atomFn = res.data?.atom_filename || res.data?.filename || "atom";
      document.getElementById("verifyClaimInput").value = "";
      banner.textContent = `Verified Atom anchored: ${atomFn} (${
        res.data?.memory_type || memory_type
      })`;
      showToast(`Promoted excerpt to Verified Atom: ${atomFn}`, "ok");
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
    chatWarningEl.style.display = "none";
    chatSourcesEl.replaceChildren();
    if (chatSourcesHeaderEl) {
      chatSourcesHeaderEl.textContent = "Retrieved Sources (Click to Inspect)";
    }
    chatBannerEl.textContent = "Cleared multi-turn chat session history.";
    renderAllChatTurns();
  });
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
  .addEventListener("click", loadScoutQueue);

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

document.getElementById("refreshIdeBtn").addEventListener("click", loadIdeStatus);

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
    tr.appendChild(actTd);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
}

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
}

document
  .getElementById("refreshSystemBtn")
  .addEventListener("click", loadSystemView);
document
  .getElementById("switchVaultBtn")
  .addEventListener("click", async () => {
    try {
      const res = await window.secondBrain.switchVault();
      if (res && res.canceled) return;
      if (res && res.ok) {
        showToast(`Switched active vault to ${res.vaultDir}`, "ok");
        activeNote = null;
        setNoteBaseline("", "", "");
        loadNotes({ append: false });
        loadSystemView();
        refreshHealth();
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
  .addEventListener("click", loadPlannerView);

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
  const params = [`include_pending=${incPending ? "1" : "0"}`];
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
    makeKpiCard("Orphan Approved", d.orphan_approved_count ?? orphans.length)
  );

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
      card.appendChild(top);

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
  return {
    search: document.getElementById("graphSearchInput").value,
    project: document.getElementById("graphProjectInput").value,
    status: document.getElementById("graphStatusSelect").value,
    memoryType: document.getElementById("graphTypeSelect").value,
    includePending: document.getElementById("graphIncludePendingCheck").checked,
  };
}

function rerenderGraphFromState() {
  const viewer = ensureGraphViewerInitialized();
  const filtered = viewer.render(graphRawData, currentGraphFilters());
  const banner = document.getElementById("graphBanner");
  if (banner) {
    banner.textContent = `Showing ${filtered.nodes.length} node(s) and ${
      filtered.edges.length
    } edge(s)${graphRawData.fallback ? " (synthesized fallback view)" : ""}. Drag to pan, scroll to zoom, click a node to inspect.`;
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
  const params = [`include_pending=${incPending}`, "limit=300"];
  if (proj) params.push(`project=${encodeURIComponent(proj)}`);

  const fullRes = await window.secondBrain.request(
    "GET",
    `/v1/graph/full?${params.join("&")}`
  );
  if (fullRes.ok && fullRes.data && Array.isArray(fullRes.data.nodes)) {
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
    });
  }
  refreshHealth();
  loadNotes({ append: false });
}
