let activeView = "notes";
let activeNote = null;
let notesCache = [];
let noteBaseline = { title: "", tags: "", body: "" };
let splitPreviewOpen = false;
let isChatStreaming = false;
let candidatesCache = [];
let approvedAtomsCache = [];
let snapshotsCache = [];

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
      ? `409 Conflict — ${rawMsg} (refresh and retry)`
      : "409 Conflict — item changed on disk; refresh and retry.";
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

// ─── Modal Helper (Atom Editor & Atom Inspector) ─────────────────────────────

const modalBackdrop = document.getElementById("atomModalBackdrop");
const modalTitleEl = document.getElementById("atomModalTitle");
const modalBodyEl = document.getElementById("atomModalBody");
const modalFooterEl = document.getElementById("atomModalFooter");

function closeModal() {
  modalBackdrop.classList.remove("open");
  modalBodyEl.replaceChildren();
  modalFooterEl.replaceChildren();
}

document.getElementById("atomModalCloseBtn").addEventListener("click", closeModal);
modalBackdrop.addEventListener("click", (e) => {
  if (e.target === modalBackdrop) closeModal();
});

async function openAtomInspectorModal(filename, fallbackData = null) {
  if (!filename) return;
  modalTitleEl.textContent = `Atom: ${filename}`;
  modalBodyEl.replaceChildren(el("div", "status-banner", "Loading atom details..."));
  modalFooterEl.replaceChildren();
  modalBackdrop.classList.add("open");

  let atom = fallbackData || {};
  try {
    const res = await window.secondBrain.api(
      "GET",
      `/v1/atoms/${encodeURIComponent(filename)}`
    );
    if (res.ok && res.data) {
      atom = res.data.atom || res.data;
    } else {
      const listRes = await window.secondBrain.api("GET", "/v1/atoms?limit=300");
      const found = (listRes.data?.items || []).find((a) => a.filename === filename);
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
    const provBar = el(
      "div",
      "note-item-meta",
      `Provenance Source: ${evSrc || "—"}${
        offsetText ? ` · Byte Offsets:${offsetText}` : ""
      }${meta.evidence_sha256 ? ` · Evidence SHA-256: ${shortSha(meta.evidence_sha256)}` : ""}`
    );
    modalBodyEl.appendChild(provBar);
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
  modalTitleEl.textContent = `Edit Atom: ${filename}`;
  modalBodyEl.replaceChildren(el("div", "status-banner", "Loading atom markdown..."));
  modalFooterEl.replaceChildren();
  modalBackdrop.classList.add("open");

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
    `Editing ${filename} (CAS sha256: ${shortSha(fullAtom.sha256)}). Saving transitions atom through owner CAS validation.`
  );
  const claimWrap = el("div", "field-col");
  claimWrap.appendChild(el("span", "field-label", "Claim Text (Quick Edit — syncs into ## Claim section)"));
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
  if (name === "review") loadReviewQueue();
  if (name === "ide") loadIdeStatus();
  if (name === "system") loadSystemView();
  if (name === "planner") loadPlannerView();
  if (name === "proof") loadProofView();
}

document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

function handleBackendStatus(st) {
  if (!st) return;
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
    loadNotes(document.getElementById("noteSearchInput").value);
    if (activeView === "review") loadReviewQueue();
    if (activeView === "ide") loadIdeStatus();
    if (activeView === "system") loadSystemView();
    if (activeView === "planner") loadPlannerView();
    if (activeView === "proof") loadProofView();
  } else if (st.state === "bootstrapping" || st.state === "starting") {
    dot.style.background = "#d29922";
    dot.style.boxShadow = "0 0 10px rgba(210, 153, 34, 0.55)";
    text.textContent = st.state === "bootstrapping" ? "Bootstrapping..." : "Starting API...";
    startupText.textContent = st.message || "Starting loopback Memory API...";
    startupBanner.className = "startup-banner warn visible";
  } else if (st.state === "error") {
    dot.style.background = "#f85149";
    dot.style.boxShadow = "0 0 10px rgba(248, 81, 73, 0.55)";
    text.textContent = "API stopped / error";
    startupText.textContent =
      st.message || "Memory API sidecar is stopped. Use Tray -> Restart Memory API.";
    startupBanner.className = "startup-banner danger visible";
  }
}

if (typeof window.secondBrain.onBackendStatus === "function") {
  window.secondBrain.onBackendStatus((st) => handleBackendStatus(st));
}

async function refreshHealth() {
  try {
    const status = await window.secondBrain.getStatus();
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
    document.getElementById("vaultMetrics").textContent = `Notes: ${nCount} · Verified Atoms: ${aCount}`;
  } catch {
    document.getElementById("apiStatusText").textContent = "Disconnected";
  }
}

// ─── TAB 1: Notes, Dirty Tracking, Safe Markdown Preview & Verify Excerpt ────

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

function confirmDiscardIfDirty() {
  if (!isNoteDirty()) return true;
  return window.confirm(
    "You have unsaved changes in the current note. Discard unsaved edits?"
  );
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
  if (
    window.confirm(
      `No note titled "${clean}" exists yet. Create a new note for [[${clean}]]?`
    )
  ) {
    if (!confirmDiscardIfDirty()) return;
    activeNote = null;
    document.getElementById("noteTitleInput").value = clean;
    document.getElementById("noteTagsInput").value = "";
    document.getElementById("noteBodyInput").value = `# ${clean}\n\n`;
    setNoteBaseline("", "", "");
    renderNotePreview();
    await saveActiveNote();
  }
}

function appendInlineMarkdown(parent, text) {
  const tokenRe = /(\[\[[^\]]+\]\]|`[^`]+`|\*\*[^*]+\*\*)/g;
  let lastIdx = 0;
  let match;
  while ((match = tokenRe.exec(text)) !== null) {
    if (match.index > lastIdx) {
      parent.appendChild(document.createTextNode(text.slice(lastIdx, match.index)));
    }
    const tok = match[0];
    if (tok.startsWith("[[") && tok.endsWith("]]")) {
      const inner = tok.slice(2, -2);
      const parts = inner.split("|");
      const target = parts[0].trim();
      const label = (parts[1] || parts[0]).trim();
      const linkPill = el("span", "wikilink-pill", `[[${label}]]`);
      linkPill.title = `Open note: ${target}`;
      linkPill.onclick = () => resolveAndOpenWikiLink(target);
      parent.appendChild(linkPill);
    } else if (tok.startsWith("`") && tok.endsWith("`")) {
      parent.appendChild(el("code", "md-inline-code", tok.slice(1, -1)));
    } else if (tok.startsWith("**") && tok.endsWith("**")) {
      parent.appendChild(el("strong", "", tok.slice(2, -2)));
    }
    lastIdx = match.index + tok.length;
  }
  if (lastIdx < text.length) {
    parent.appendChild(document.createTextNode(text.slice(lastIdx)));
  }
}

function renderMarkdownSafe(container, markdown) {
  container.replaceChildren();
  const lines = String(markdown || "").split(/\r?\n/);
  let inCode = false;
  let codeLines = [];
  let listNode = null;
  let listType = "";

  const flushList = () => {
    if (listNode) {
      container.appendChild(listNode);
      listNode = null;
      listType = "";
    }
  };

  for (const rawLine of lines) {
    if (rawLine.trim().startsWith("```")) {
      flushList();
      if (!inCode) {
        inCode = true;
        codeLines = [];
      } else {
        const pre = el("pre", "md-code");
        pre.appendChild(el("code", "", codeLines.join("\n")));
        container.appendChild(pre);
        inCode = false;
        codeLines = [];
      }
      continue;
    }
    if (inCode) {
      codeLines.push(rawLine);
      continue;
    }

    const trimmed = rawLine.trim();
    if (!trimmed) {
      flushList();
      continue;
    }

    if (/^---+$/.test(trimmed)) {
      flushList();
      container.appendChild(el("hr"));
      continue;
    }

    const headingMatch = /^(#{1,4})\s+(.*)$/.exec(trimmed);
    if (headingMatch) {
      flushList();
      const level = headingMatch[1].length;
      const h = el(`h${level}`);
      appendInlineMarkdown(h, headingMatch[2]);
      container.appendChild(h);
      continue;
    }

    if (trimmed.startsWith(">")) {
      flushList();
      const bq = el("blockquote");
      appendInlineMarkdown(bq, trimmed.replace(/^>\s?/, ""));
      container.appendChild(bq);
      continue;
    }

    const ulMatch = /^[-*]\s+(.*)$/.exec(trimmed);
    if (ulMatch) {
      if (listType !== "ul") {
        flushList();
        listNode = el("ul");
        listType = "ul";
      }
      const li = el("li");
      appendInlineMarkdown(li, ulMatch[1]);
      listNode.appendChild(li);
      continue;
    }

    const olMatch = /^\d+\.\s+(.*)$/.exec(trimmed);
    if (olMatch) {
      if (listType !== "ol") {
        flushList();
        listNode = el("ol");
        listType = "ol";
      }
      const li = el("li");
      appendInlineMarkdown(li, olMatch[1]);
      listNode.appendChild(li);
      continue;
    }

    flushList();
    const p = el("p");
    appendInlineMarkdown(p, rawLine);
    container.appendChild(p);
  }

  flushList();
  if (inCode && codeLines.length) {
    const pre = el("pre", "md-code");
    pre.appendChild(el("code", "", codeLines.join("\n")));
    container.appendChild(pre);
  }
  if (!container.children.length) {
    container.appendChild(
      el("div", "note-item-meta", "Preview will appear as you type Markdown or [[WikiLinks]]...")
    );
  }
}

function renderNotePreview() {
  if (!splitPreviewOpen) return;
  renderMarkdownSafe(
    document.getElementById("notePreviewPane"),
    document.getElementById("noteBodyInput").value
  );
}

async function loadNotes(query = "") {
  const q = query
    ? `?q=${encodeURIComponent(query)}&limit=200`
    : "?limit=200";
  const res = await window.secondBrain.request("GET", `/v1/notes${q}`);
  if (!res.ok) return;
  notesCache = res.data?.items || res.data?.notes || [];
  const listEl = document.getElementById("notesList");
  listEl.replaceChildren();
  if (!notesCache.length) {
    listEl.appendChild(el("div", "status-banner", "No notes found."));
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
    const tagText = (note.tags || []).length ? ` · ${(note.tags || []).join(", ")}` : "";
    item.appendChild(el("div", "note-item-meta", `${note.filename}${tagText}`));
    item.addEventListener("click", () => openNote(note.filename));
    listEl.appendChild(item);
  }
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
  if (!skipDirtyPrompt && activeNote?.filename !== filename && !confirmDiscardIfDirty()) {
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
        banner.textContent = `Saved ${activeNote.filename} (${shortSha(activeNote.sha256)})`;
        if (!silentSuccessToast) {
          showToast(`Saved ${activeNote.filename}`, "ok", 2500);
        }
        await loadNotes(document.getElementById("noteSearchInput").value);
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
      banner.textContent = `Created ${activeNote.filename} (${shortSha(activeNote.sha256)})`;
      if (!silentSuccessToast) {
        showToast(`Created ${activeNote.filename}`, "ok", 2500);
      }
      await loadNotes(document.getElementById("noteSearchInput").value);
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
    const msg = "Highlighted excerpt must be at least 12 characters to anchor as an Atom.";
    banner.textContent = msg;
    showToast(msg, "warn");
    return;
  }

  if (!activeNote || !activeNote.filename || isNoteDirty()) {
    banner.textContent = "Auto-saving unsaved note edits before anchoring excerpt...";
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
      banner.textContent = `Verified Atom anchored: ${atomFn} (${res.data?.memory_type || memory_type})`;
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
  if (!window.confirm(`Move note "${label}" (${activeNote.filename}) to trash?`)) {
    return;
  }
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
      document.getElementById("notesBanner").textContent = `Moved ${deletedName} to trash.`;
      showToast(`Moved ${deletedName} to trash`, "ok");
      await loadNotes(document.getElementById("noteSearchInput").value);
      refreshHealth();
    } else {
      showToast(apiErrorMessage(res, "Delete failed"), "danger");
    }
  } catch (err) {
    showToast(`Delete error: ${err?.message || String(err)}`, "danger");
  }
}

document.getElementById("newNoteBtn").addEventListener("click", () => {
  if (!confirmDiscardIfDirty()) return;
  activeNote = null;
  document.getElementById("noteTitleInput").value = "";
  document.getElementById("noteTagsInput").value = "";
  document.getElementById("noteBodyInput").value = "";
  setNoteBaseline("", "", "");
  renderBacklinksBar([]);
  renderNotePreview();
  document.querySelectorAll(".note-item").forEach((n) => n.classList.remove("active"));
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

document.getElementById("saveNoteBtn").addEventListener("click", () => saveActiveNote());
document.getElementById("verifyExcerptBtn").addEventListener("click", verifySelectionAsAtom);
document.getElementById("deleteNoteBtn").addEventListener("click", deleteActiveNote);
document
  .getElementById("noteSearchInput")
  .addEventListener("input", (e) => loadNotes(e.target.value));

// ─── TAB 2: Streaming Chat ───────────────────────────────────────────────────

const chatOutputEl = document.getElementById("chatOutput");
const chatSourcesEl = document.getElementById("chatSources");
const chatBannerEl = document.getElementById("chatBanner");
const chatWarningEl = document.getElementById("chatWarningBar");
const chatVerifiedClaimsEl = document.getElementById("chatVerifiedClaims");
const chatGroundingEl = document.getElementById("chatGroundingBar");
const chatSendBtn = document.getElementById("chatSendBtn");
const chatStopBtn = document.getElementById("chatStopBtn");

function setChatStreamingState(streaming) {
  isChatStreaming = streaming;
  chatSendBtn.disabled = streaming;
  if (chatStopBtn) chatStopBtn.disabled = !streaming;
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
  return { cls: "tier-n", label: `[${cid || "N"}] Working Note` };
}

function renderChatSourceCards(sources) {
  chatSourcesEl.replaceChildren();
  if (!sources.length) {
    chatSourcesEl.appendChild(
      el("div", "status-banner", "No matching sources retrieved for this query.")
    );
    return;
  }
  for (const src of sources) {
    const card = el("div", "source-card");
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
      if (tier === "note" || String(src.citation_id || "").startsWith("N")) {
        switchView("notes");
        openNote(src.filename);
      } else if (src.filename) {
        openAtomInspectorModal(src.filename, src);
      }
    };
    chatSourcesEl.appendChild(card);
  }
}

function renderGroundingBar(g) {
  chatGroundingEl.replaceChildren();
  chatGroundingEl.style.display = "block";
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
      `Withheld / Unsupported Claims: ${withheldCount}`
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
  chatGroundingEl.appendChild(box);
}

function renderVerifiedClaims(data) {
  chatVerifiedClaimsEl.replaceChildren();
  const claims = data.claims || [];
  if (!data.answerable || !claims.length) {
    chatVerifiedClaimsEl.style.display = "none";
    chatOutputEl.textContent =
      data.reason ||
      data.text ||
      "Abstained: no owner-verified claims directly answer this question.";
    return;
  }
  chatOutputEl.textContent =
    data.text || `Grounded in ${claims.length} owner-verified claim(s):`;
  chatVerifiedClaimsEl.style.display = "flex";
  for (const cl of claims) {
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
    chatVerifiedClaimsEl.appendChild(card);
  }
}

window.secondBrain.onChatEvent(({ event, data }) => {
  if (!data) return;
  if (event === "context") {
    const ms =
      data.retrieval_ms !== undefined ? `${data.retrieval_ms} ms` : "instant";
    const sources = data.sources || [];
    chatBannerEl.textContent = `Retrieved ${sources.length} source(s) in ${ms}`;
    if (data.warning) {
      chatWarningEl.style.display = "block";
      chatWarningEl.textContent = data.warning;
    } else {
      chatWarningEl.style.display = "none";
    }
    renderChatSourceCards(sources);
    if (document.getElementById("chatModeSelect").value === "instant") {
      chatOutputEl.textContent = sources.length
        ? `Instant retrieval complete (${sources.length} source cards on the right — no LLM invoked).\n\n` +
          sources
            .map(
              (s) =>
                `[${s.citation_id || s.tier}] ${s.title || s.filename}: ${
                  s.claim || s.excerpt || ""
                }`
            )
            .join("\n\n")
        : "No matching verified atoms or working notes found.";
    }
  } else if (event === "token") {
    chatOutputEl.textContent += data.text ?? data.token ?? "";
  } else if (event === "verified_answer") {
    renderVerifiedClaims(data);
  } else if (event === "grounding") {
    renderGroundingBar(data);
  } else if (event === "abstain") {
    chatOutputEl.textContent = `Abstained: ${
      data.reason || data.text || "Insufficient verified context."
    }`;
  } else if (event === "cancelled") {
    chatBannerEl.textContent = "Chat stream cancelled by owner.";
    setChatStreamingState(false);
  } else if (event === "error") {
    const errMsg = data.error || "Stream failed";
    chatBannerEl.textContent = `Error: ${errMsg}`;
    showToast(errMsg, "danger");
    setChatStreamingState(false);
  } else if (event === "done") {
    const ms = data.elapsed_ms ?? data.total_ms;
    if (ms !== undefined) {
      chatBannerEl.textContent += ` · Completed in ${ms} ms`;
    }
    setChatStreamingState(false);
  }
});

async function sendChat() {
  const query = document.getElementById("chatQueryInput").value.trim();
  if (!query) {
    showToast("Enter a question to query your vault.", "warn");
    return;
  }
  const mode = document.getElementById("chatModeSelect").value;
  const project_scope =
    document.getElementById("chatScopeInput").value.trim() || undefined;

  chatOutputEl.textContent = "";
  chatSourcesEl.replaceChildren();
  chatWarningEl.style.display = "none";
  chatVerifiedClaimsEl.style.display = "none";
  chatVerifiedClaimsEl.replaceChildren();
  chatGroundingEl.style.display = "none";
  chatGroundingEl.replaceChildren();
  chatBannerEl.textContent = "Retrieving context...";
  setChatStreamingState(true);

  try {
    const res = await window.secondBrain.streamChat({
      query,
      mode,
      project_scope,
    });
    if (res && !res.ok && !res.cancelled && chatBannerEl.textContent === "Retrieving context...") {
      const errMsg = res.error || `HTTP ${res.status}`;
      chatBannerEl.textContent = `Error: ${errMsg}`;
      showToast(errMsg, "danger");
    }
  } catch (err) {
    const errMsg = err?.message || String(err);
    chatBannerEl.textContent = `Error: ${errMsg}`;
    showToast(errMsg, "danger");
  } finally {
    setChatStreamingState(false);
  }
}

document.getElementById("chatSendBtn").addEventListener("click", sendChat);
document.getElementById("chatQueryInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendChat();
});
document.getElementById("chatStopBtn").addEventListener("click", async () => {
  await window.secondBrain.abortChat();
  setChatStreamingState(false);
});
document.getElementById("chatWarmupBtn").addEventListener("click", async () => {
  chatBannerEl.textContent = "Warming up local Ollama model...";
  try {
    const res = await window.secondBrain.request("POST", "/v1/chat/warmup", {});
    if (res.ok) {
      const modelName = res.data?.model || "local model";
      const ms = res.data?.warmup_ms !== undefined ? ` in ${res.data.warmup_ms} ms` : "";
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

// ─── TAB 3: Quarantined Inbox, Atom Review & CAS Merge ───────────────────────

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
          : item.preview || apiErrorMessage(res, "Preview only (full inspect unavailable)")
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
      `Origin: ${atom.origin || "unknown"}${srcList ? " · Sources: " + srcList : ""}`
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
  noteInput.style.minWidth = "130px";
  fbRow.appendChild(noteInput);

  for (const [fb, cls, label] of [
    ["endorsed", "good", "Endorse"],
    ["corrected", "warn", "Corrected"],
  ]) {
    const btn = el("button", `btn sm ${cls}`, label);
    btn.type = "button";
    btn.onclick = async () => {
      const res = await window.secondBrain.api("POST", "/v1/cognitive/feedback", {
        atom_filename: atom.filename,
        feedback: fb,
        actor: "desktop_owner",
        note: noteInput.value.trim(),
      });
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
  const view = document.getElementById("view-review");
  if (!view) return;
  document.querySelectorAll("[data-review-filter]").forEach((btn) => {
    btn.classList.toggle("primary", btn.dataset.reviewFilter === filterMode);
  });
  const inboxGrid = document.getElementById("inboxQueueGrid");
  const reviewGrid = document.getElementById("reviewGrid");
  const approvedGrid = document.getElementById("approvedAtomsGrid");
  const headers = view.querySelectorAll(".section-header");
  const mergeCard = approvedGrid?.previousElementSibling;

  const showInbox = filterMode === "all" || filterMode === "inbox";
  const showPending = filterMode === "all" || filterMode === "pending";
  const showApproved = filterMode === "all" || filterMode === "approved";

  if (headers[0]) headers[0].style.display = showInbox ? "" : "none";
  if (inboxGrid) inboxGrid.style.display = showInbox ? "" : "none";
  if (headers[1]) headers[1].style.display = showPending ? "" : "none";
  if (reviewGrid) reviewGrid.style.display = showPending ? "" : "none";
  if (headers[2]) headers[2].style.display = showApproved ? "" : "none";
  if (mergeCard && mergeCard.classList.contains("card")) {
    mergeCard.style.display = showApproved ? "" : "none";
  }
  if (approvedGrid) approvedGrid.style.display = showApproved ? "" : "none";
}

document.querySelectorAll("[data-review-filter]").forEach((btn) => {
  btn.addEventListener("click", () => {
    applyReviewSectionFilter(btn.dataset.reviewFilter || "all");
  });
});

async function loadReviewQueue() {
  const inboxGrid = document.getElementById("inboxQueueGrid");
  const reviewGrid = document.getElementById("reviewGrid");
  const approvedGrid = document.getElementById("approvedAtomsGrid");
  const dupListEl = document.getElementById("duplicatesList");

  inboxGrid.replaceChildren(el("div", "status-banner", "Loading inbox..."));
  reviewGrid.replaceChildren(el("div", "status-banner", "Loading review queue..."));

  const [inboxRes, reviewRes, atomsRes, dupRes] = await Promise.all([
    window.secondBrain.request("GET", "/v1/inbox/queue"),
    window.secondBrain.request("GET", "/v1/review/queue"),
    window.secondBrain.request("GET", "/v1/atoms?limit=100"),
    window.secondBrain.request("GET", "/v1/atoms/duplicates"),
  ]);

  inboxGrid.replaceChildren();
  const inboxItems = inboxRes.data?.items || [];
  if (!inboxItems.length) {
    inboxGrid.appendChild(el("div", "status-banner", "Quarantined inbox is empty."));
  } else {
    for (const item of inboxItems) {
      inboxGrid.appendChild(buildInboxCard(item));
    }
  }

  reviewGrid.replaceChildren();
  const reviewItems = reviewRes.data?.items || [];
  if (!reviewItems.length) {
    reviewGrid.appendChild(
      el("div", "status-banner", "Pending & quarantined atom review queue is clear.")
    );
  } else {
    for (const atom of reviewItems) {
      reviewGrid.appendChild(buildAtomReviewCard(atom));
    }
  }

  const allAtoms = atomsRes.data?.items || atomsRes.data?.atoms || [];
  approvedAtomsCache = allAtoms.filter((a) => a.verified || a.review_status === "accepted");

  const survSel = document.getElementById("mergeSurvivorSelect");
  const supSel = document.getElementById("mergeSupersededSelect");
  survSel.replaceChildren();
  supSel.replaceChildren();
  for (const a of allAtoms) {
    for (const sel of [survSel, supSel]) {
      const opt = el("option", "", `${a.filename} (${shortSha(a.sha256)})`);
      opt.value = a.filename;
      opt.dataset.sha = a.sha256 || "";
      sel.appendChild(opt);
    }
  }
  if (supSel.options.length > 1) supSel.selectedIndex = 1;

  dupListEl.replaceChildren();
  const duplicates = dupRes.ok ? dupRes.data?.duplicates || [] : [];
  if (!duplicates.length) {
    dupListEl.textContent = "No duplicate atom pairs detected across approved memory.";
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
            dup.similarity !== undefined ? ` (similarity: ${dup.similarity})` : ""
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

  approvedGrid.replaceChildren();
  if (!approvedAtomsCache.length) {
    approvedGrid.appendChild(el("div", "status-banner", "No approved atoms yet."));
  } else {
    for (const atom of approvedAtomsCache.slice(0, 100)) {
      approvedGrid.appendChild(buildApprovedAtomCard(atom));
    }
  }
}

document.getElementById("refreshReviewBtn").addEventListener("click", loadReviewQueue);
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

// ─── TAB 4: Bulk Import ──────────────────────────────────────────────────────

const importDryRunCheck = document.getElementById("importDryRunCheck");
const runImportBtn = document.getElementById("runImportBtn");

importDryRunCheck.addEventListener("change", () => {
  runImportBtn.textContent = importDryRunCheck.checked
    ? "Preview Import (Dry Run)"
    : "Import Notes";
});

document.getElementById("pickImportDirBtn").addEventListener("click", async () => {
  const dir = await window.secondBrain.pickFolder();
  if (dir) document.getElementById("importDirInput").value = dir;
});

runImportBtn.addEventListener("click", async () => {
  const source_dir = document.getElementById("importDirInput").value.trim();
  if (!source_dir) {
    showToast("Choose a source folder containing Markdown or text files.", "warn");
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
        d.error_count ?? (errorList.length || Number(d.skipped_unreadable || 0));

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
        errBox.appendChild(el("strong", "", `Import Errors (${errorList.length}):`));
        for (const itemErr of errorList.slice(0, 12)) {
          errBox.appendChild(
            el(
              "div",
              "note-item-meta",
              typeof itemErr === "string"
                ? itemErr
                : `${itemErr.file || itemErr.path || "file"}: ${itemErr.error || ""}`
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
        loadNotes();
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
          const ok = window.confirm(
            `Cloud Provider Privacy Notice (${label}):\n\n${privacyNotice}\n\nConnect ${label} to your local Second Brain MCP server now?`
          );
          if (!ok) return;
        } else {
          if (!window.confirm(`Disconnect ${label} from Second Brain MCP?`)) {
            return;
          }
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

  const plannerHostSel = document.getElementById("planHostSelect");
  if (plannerHostSel) {
    const prev = plannerHostSel.value || "ollama_local";
    plannerHostSel.replaceChildren();
    for (const [hid, rule] of entries) {
      const opt = el(
        "option",
        "",
        `${hid} (${rule.provider || ""}${rule.enabled ? "" : " · disabled"})`
      );
      opt.value = hid;
      plannerHostSel.appendChild(opt);
    }
    if ([...plannerHostSel.options].some((o) => o.value === prev)) {
      plannerHostSel.value = prev;
    }
  }

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
    keyInput.placeholder = rule.transport === "local" ? "(local)" : "OPENAI_API_KEY";
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

  const [appSt, healthRes, profileRes, hostsRes, fitnessRes] = await Promise.all([
    window.secondBrain.getStatus(),
    window.secondBrain.request("GET", "/v1/health"),
    window.secondBrain.request("GET", "/v1/profile"),
    window.secondBrain.request("GET", "/v1/hosts"),
    window.secondBrain.request("GET", "/v1/cognitive/fitness"),
  ]);

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
    hostsRes.ok && hostsRes.data && typeof hostsRes.data === "object" && !hostsRes.data.error
      ? hostsRes.data.hosts || hostsRes.data
      : profileRes.data?.hosts || {};
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
    makeKpiCard(
      "Tracked Procedures",
      (fit.procedure_maturity || []).length
    )
  );
  fitKpis.appendChild(
    makeKpiCard(
      "Stale / Dormant",
      (fit.stale_or_dormant_atoms || fit.dormant_atoms || fit.cold_verified_atoms || []).length
    )
  );
  fitKpis.appendChild(
    makeKpiCard(
      "High-Utility Atoms",
      (fit.top_performing_atoms || fit.high_fitness_atoms || fit.high_utility_verified_atoms || []).length
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
          `${p.atom_id || "pending-atom"} · ${p.maturity_stage || p.stage || "candidate"}`
        )
      );
    }
    fitWrap.appendChild(procBar);
  }
}

document.getElementById("refreshSystemBtn").addEventListener("click", loadSystemView);
document.getElementById("switchVaultBtn").addEventListener("click", async () => {
  try {
    const res = await window.secondBrain.switchVault();
    if (res && res.canceled) return;
    if (res && res.ok) {
      showToast(`Switched active vault to ${res.vaultDir}`, "ok");
      activeNote = null;
      setNoteBaseline("", "", "");
      loadNotes();
      loadSystemView();
      refreshHealth();
    } else if (res && res.error) {
      showToast(res.error, "danger");
    }
  } catch (err) {
    showToast(`Switch vault failed: ${err?.message || String(err)}`, "danger");
  }
});

document.getElementById("recordExpBtn").addEventListener("click", async () => {
  const goal = document.getElementById("expGoalInput").value.trim();
  const result_summary = document.getElementById("expSummaryInput").value.trim();
  if (!goal || !result_summary) {
    showToast("Goal and Result Summary are required to log an experience.", "warn");
    return;
  }
  const payload = {
    goal,
    result_summary,
    success: Boolean(document.getElementById("expSuccessCheck").checked),
    context: parseTags(document.getElementById("expContextInput").value),
    actions: parseLines(document.getElementById("expActionsInput").value),
    lessons: parseLines(document.getElementById("expLessonsInput").value),
    candidate_procedure_title:
      document.getElementById("expCandTitleInput").value.trim() || null,
    propose_to_inbox: Boolean(document.getElementById("expProposeCheck").checked),
  };
  const res = await window.secondBrain.api(
    "POST",
    "/v1/cognitive/experiences",
    payload
  );
  if (res.ok) {
    document.getElementById("expGoalInput").value = "";
    document.getElementById("expSummaryInput").value = "";
    document.getElementById("expActionsInput").value = "";
    document.getElementById("expLessonsInput").value = "";
    document.getElementById("expCandTitleInput").value = "";
    showToast(
      `Recorded experience ${res.data?.filename || res.data?.experience_id || ""}`,
      "ok"
    );
  } else {
    showToast(apiErrorMessage(res, "Failed to record experience"), "danger");
  }
});

// ─── TAB 7: Playbooks & Grounded Executive Planner ───────────────────────────

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

  if (isActive && pb.filename) {
    const depRow = el("div", "toolbar");
    const reasonInput = el("input");
    reasonInput.type = "text";
    reasonInput.placeholder = "Deprecation rationale...";
    reasonInput.style.flex = "1";
    const depBtn = el("button", "btn sm danger", "Deprecate");
    depBtn.type = "button";
    depBtn.onclick = async () => {
      const rationale =
        reasonInput.value.trim() || "Deprecated via Desktop Playbook Manager";
      if (!window.confirm(`Deprecate playbook "${pb.title || pb.filename}"?`)) {
        return;
      }
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
    depRow.appendChild(reasonInput);
    depRow.appendChild(depBtn);
    card.appendChild(depRow);
  }

  return card;
}

function prefillFromCandidate(cand) {
  if (!cand) return;
  const stem = String(cand.atom_id || "")
    .replace(/\.md$/i, "")
    .replace(/[^a-z0-9-]+/gi, "-")
    .toLowerCase();
  const idInput = document.getElementById("pbIdInput");
  const titleInput = document.getElementById("pbTitleInput");
  const stepsInput = document.getElementById("pbStepsInput");
  if (!idInput.value.trim() && stem) idInput.value = `pb-${stem}`.slice(0, 48);
  if (!titleInput.value.trim() && stem) {
    titleInput.value = stem.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }
  const lines = String(cand.claim || "")
    .split(/\r?\n|(?:;\s+)/)
    .map((l) => l.replace(/^(?:\d+[.)]\s*|[-*]\s*)/, "").trim())
    .filter(Boolean);
  stepsInput.value = lines.join("\n");
  document
    .querySelectorAll("#pbBackingSelectWrap input[type='checkbox']")
    .forEach((chk) => {
      if (chk.dataset.atomId === cand.atom_id) chk.checked = true;
    });
  showToast(`Pre-filled steps from ${cand.atom_id}`, "ok", 2600);
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
    const plannerHostSel = document.getElementById("planHostSelect");
    const prev = plannerHostSel.value || "ollama_local";
    plannerHostSel.replaceChildren();
    for (const [hid, rule] of Object.entries(profRes.data.hosts)) {
      const opt = el(
        "option",
        "",
        `${hid}${rule.enabled ? "" : " (disabled)"}`
      );
      opt.value = hid;
      plannerHostSel.appendChild(opt);
    }
    if ([...plannerHostSel.options].some((o) => o.value === prev)) {
      plannerHostSel.value = prev;
    }
  }

  const grid = document.getElementById("playbooksGrid");
  grid.replaceChildren();
  const items = pbRes.data?.playbooks || pbRes.data?.items || [];
  if (!items.length) {
    grid.appendChild(
      el("div", "status-banner", "No skill playbooks registered for this scope yet.")
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

  let candidates = candRes.ok ? candRes.data?.candidates || [] : [];
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
    chkLbl.appendChild(el("span", "", `${cand.atom_id} (${shortSha(cand.sha256)})`));
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
        card.appendChild(el("div", "claim-quote", `"${ea.evidence || ea.quote}"`));
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
        ? `Plan assembled (${res.data.mode_used || mode}) — ${(res.data.steps || []).length} grounded step(s).`
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

    if (!backing_atoms.length || !playbook_id || !title || !trigger || !steps.length) {
      showToast(
        "Select at least one backing atom and fill Playbook ID, Title, Trigger, and Steps.",
        "warn"
      );
      return;
    }

    const res = await window.secondBrain.api("POST", "/v1/playbooks/promote", {
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
        "Owner promoted via Desktop App",
      allow_early_owner_override: Boolean(
        document.getElementById("pbAllowEarlyCheck").checked
      ),
      acknowledge_corrections: Boolean(
        document.getElementById("pbAckCorrectionsCheck").checked
      ),
      allow_multi_project: Boolean(
        document.getElementById("pbAllowMultiProjCheck").checked
      ),
    });

    if (res.ok) {
      showToast(`Promoted Skill Playbook: ${title}`, "ok");
      loadPlannerView();
    } else {
      showToast(apiErrorMessage(res, "Playbook promotion failed"), "danger");
    }
  });

document.getElementById("runPlanBtn").addEventListener("click", runExecutivePlan);
document
  .getElementById("refreshPlaybooksBtn")
  .addEventListener("click", loadPlannerView);

// ─── TAB 8: Merkle Ledger, Contradiction Radar & Time-Travel Snapshots ───────

async function loadLedgerStatus() {
  const kpiGrid = document.getElementById("ledgerKpiGrid");
  const rootsBar = document.getElementById("ledgerRootsBar");
  const atomSel = document.getElementById("proveAtomSelect");

  const [res, atomsRes] = await Promise.all([
    window.secondBrain.request("GET", "/v1/ledger/verify"),
    window.secondBrain.request("GET", "/v1/atoms?limit=200"),
  ]);

  const verifiedAtoms = (atomsRes.data?.items || []).filter(
    (a) => a.verified || a.review_status === "accepted"
  );
  atomSel.replaceChildren();
  const placeholderOpt = el("option", "", "-- Select verified atom --");
  placeholderOpt.value = "";
  atomSel.appendChild(placeholderOpt);
  for (const a of verifiedAtoms) {
    const opt = el("option", "", a.filename);
    opt.value = a.filename;
    atomSel.appendChild(opt);
  }

  kpiGrid.replaceChildren();
  rootsBar.replaceChildren();
  if (!res.ok || !res.data) {
    rootsBar.textContent = apiErrorMessage(res, "Failed to verify Merkle ledger");
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
  kpiGrid.appendChild(
    makeKpiCard("Approved Atoms", d.approved_atoms ?? verifiedAtoms.length)
  );
  kpiGrid.appendChild(
    makeKpiCard("Playbooks", d.accepted_playbooks ?? 0)
  );
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
}

document.getElementById("proveAtomSelect").addEventListener("change", (e) => {
  if (e.target.value) {
    document.getElementById("proveAtomInput").value = e.target.value;
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
  const siblings = proof.siblings === undefined || proof.siblings === null ? [] : proof.siblings;

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
    const cand = await sha256HexWebCrypto(`${sc.leafPrefix}${relPath}:${fileSha}`);
    if (cand === leafSha) {
      chosenScheme = sc;
      computedLeaf = cand;
      break;
    }
  }
  if (!chosenScheme) {
    computedLeaf = await sha256HexWebCrypto(`leaf:${relPath}:${fileSha}`);
    trace.push(
      `Leaf MISMATCH: SHA256("leaf:${relPath}:${shortSha(fileSha)}…") = ${shortSha(
        computedLeaf
      )} != ${shortSha(leafSha)}`
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
      return { valid: false, reason: `Invalid sibling hash at level ${i}.`, trace };
    }
    if (pos === "right") {
      const next = await sha256HexWebCrypto(`${chosenScheme.nodePrefix}${cur}:${sib}`);
      trace.push(
        `Level ${i} (pair w/ right ${shortSha(sib)}): SHA256("${
          chosenScheme.nodePrefix
        }${shortSha(cur)}:${shortSha(sib)}") -> ${next}`
      );
      cur = next;
    } else if (pos === "left") {
      const next = await sha256HexWebCrypto(`${chosenScheme.nodePrefix}${sib}:${cur}`);
      trace.push(
        `Level ${i} (pair w/ left ${shortSha(sib)}): SHA256("${
          chosenScheme.nodePrefix
        }${shortSha(sib)}:${shortSha(cur)}") -> ${next}`
      );
      cur = next;
    } else if (pos === "odd") {
      if (sib !== cur) {
        trace.push(
          `Level ${i} (odd duplicate MISMATCH): sibling ${shortSha(sib)} != current ${shortSha(cur)}`
        );
        return { valid: false, reason: `Odd node mismatch at level ${i}.`, trace };
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
    const listRes = await window.secondBrain.api("GET", "/v1/atoms?limit=300");
    sha =
      (listRes.data?.items || []).find((a) => a.filename === filename)?.sha256 ||
      "";
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
    showToast(apiErrorMessage(res, `Failed to ${action} ${filename}`), "danger");
  }
}

async function openGraphNeighborsModal(filenameA, filenameB = null) {
  const targets = [filenameA, filenameB].filter(Boolean);
  if (!targets.length) return;
  modalTitleEl.textContent = `Graph Neighbors: ${targets.join(" & ")}`;
  modalBodyEl.replaceChildren(
    el("div", "status-banner", "Loading knowledge graph neighborhood...")
  );
  modalFooterEl.replaceChildren();
  modalBackdrop.classList.add("open");

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
        el("div", "note-item-meta", apiErrorMessage(res, "Could not load neighbors"))
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
      sec.appendChild(el("div", "note-item-meta", "No graph edges connected to this atom."));
    } else {
      const bar = el("div", "backlinks-bar");
      bar.style.marginTop = "6px";
      for (const e of edges.slice(0, 20)) {
        const desc = `${e.from || ""} --[${e.kind || e.relation || "edge"}]--> ${
          e.to || ""
        }`;
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
          el("div", "note-item-meta", `Shared terms: ${al.shared_terms.join(", ")}`)
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
        qBtn.onclick = () => quickRadarAtomTransition(side.filename, "quarantine");
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

  orphansBar.appendChild(el("span", "field-label", `Orphan Approved Atoms (${orphans.length}):`));
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

  snapshotsCache = res.data?.snapshots || [];
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
      d.unchanged ? "UNCHANGED (Identical Merkle Root)" : "SEMANTIC CHANGES DETECTED"
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
    c.appendChild(el("strong", "", `+ Added Atom: ${a.filename || a.atom_id || ""}`));
    c.appendChild(el("div", "", a.claim || ""));
    outEl.appendChild(c);
  }
  for (const a of removed) {
    const c = el("div", "diff-card removed");
    c.style.marginTop = "6px";
    c.appendChild(el("strong", "", `- Removed Atom: ${a.filename || a.atom_id || ""}`));
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
        `~ Modified Atom: ${m.filename} (${shortSha(m.old_sha256)} -> ${shortSha(
          m.new_sha256
        )})`
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
  top.appendChild(el("span", "badge ok", `Point-in-Time Recall @ ${d.snapshot_id}`));
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
    row.appendChild(el("span", "badge mono", `sha:${shortSha(r.file_sha256)}`));
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
    d.valid ?? d.verified ?? d.merkle_valid ?? (d.status === "valid" || !d.errors?.length)
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
  await Promise.all([loadLedgerStatus(), scanContradictions(), loadSnapshotsList()]);
}

document.getElementById("refreshProofBtn").addEventListener("click", loadProofView);
document.getElementById("commitLedgerBtn").addEventListener("click", async () => {
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
  const atom =
    document.getElementById("proveAtomInput").value.trim() ||
    document.getElementById("proveAtomSelect").value;
  if (!atom) {
    showToast("Select or enter an approved atom filename to prove.", "warn");
    return;
  }
  const res = await window.secondBrain.request(
    "GET",
    `/v1/ledger/prove/${encodeURIComponent(atom)}`
  );
  if (res.ok && res.data) {
    await renderAtomProofCard(res.data);
    showToast(`Generated Merkle inclusion proof for ${atom}`, "ok");
  } else {
    showToast(apiErrorMessage(res, "Atom proof failed"), "danger");
  }
});

document.getElementById("verifyProofBtn").addEventListener("click", async () => {
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
    const res = await window.secondBrain.api("POST", "/v1/ledger/verify-proof", {
      proof: parsed,
    });
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
    valid ? "Merkle proof verified valid via WebCrypto" : "Merkle proof failed verification",
    valid ? "ok" : "danger"
  );
});

document.getElementById("scanRadarBtn").addEventListener("click", scanContradictions);
document.getElementById("createSnapshotBtn").addEventListener("click", async () => {
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

document.getElementById("diffSnapshotBtn").addEventListener("click", async () => {
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

document.getElementById("verifySnapshotBtn").addEventListener("click", async () => {
  const snapshot_id = document.getElementById("snapshotSelect").value;
  if (!snapshot_id) {
    showToast("Create or select a Snapshot first to verify.", "warn");
    return;
  }
  const res = await window.secondBrain.request("POST", "/v1/snapshots/verify", {
    snapshot_id,
  });
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

document.getElementById("recallSnapshotBtn").addEventListener("click", async () => {
  const snapshot_id = document.getElementById("snapshotSelect").value;
  const query = document.getElementById("snapshotQueryInput").value.trim();
  const project_scope =
    document.getElementById("snapshotRecallScopeInput").value.trim() || undefined;
  if (!snapshot_id || !query) {
    showToast("Select a snapshot and enter a recall query.", "warn");
    return;
  }
  const res = await window.secondBrain.request("POST", "/v1/snapshots/recall", {
    snapshot_id,
    query,
    project_scope,
    limit: 10,
  });
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

// ─── Global Shortcuts & Init ─────────────────────────────────────────────────

window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && modalBackdrop.classList.contains("open")) {
    closeModal();
    return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    if (activeView === "notes") saveActiveNote();
  }
  const tabs = [
    "notes",
    "chat",
    "review",
    "import",
    "ide",
    "system",
    "planner",
    "proof",
  ];
  if ((e.ctrlKey || e.metaKey) && e.key >= "1" && e.key <= "8") {
    e.preventDefault();
    switchView(tabs[Number(e.key) - 1]);
  }
});

window.secondBrain.onRefresh(() => {
  loadNotes(document.getElementById("noteSearchInput").value);
  refreshHealth();
});

refreshHealth();
loadNotes();
