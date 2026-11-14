let activeNote = null;
let notesCache = [];

function switchView(name) {
  document.querySelectorAll(".nav-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === name);
  });
  document.querySelectorAll(".view").forEach((v) => {
    v.classList.toggle("active", v.id === `view-${name}`);
  });
  if (name === "review") loadReviewQueue();
  if (name === "ide") loadIdeStatus();
  if (name === "system") loadSystemView();
}

document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

async function refreshHealth() {
  try {
    const status = await window.secondBrain.getStatus();
    const res = await window.secondBrain.request("GET", "/v1/health");
    const dot = document.getElementById("apiDot");
    const text = document.getElementById("apiStatusText");
    if (res.ok) {
      dot.style.background = "#2ea043";
      text.textContent = `Loopback :${status.port} ready`;
    } else {
      dot.style.background = "#f85149";
      text.textContent = "API error";
    }
    const atomsRes = await window.secondBrain.request("GET", "/v1/atoms?limit=1");
    const notesRes = await window.secondBrain.request("GET", "/v1/notes?limit=1");
    const nCount = notesRes.data?.total ?? notesRes.data?.items?.length ?? notesRes.data?.notes?.length ?? notesCache.length;
    const aCount = atomsRes.data?.total ?? atomsRes.data?.items?.length ?? atomsRes.data?.atoms?.length ?? "?";
    document.getElementById("vaultMetrics").textContent = `Notes: ${nCount} · Atoms: ${aCount}`;
  } catch {
    document.getElementById("apiStatusText").textContent = "Disconnected";
  }
}

async function loadNotes(query = "") {
  const q = query ? `?query=${encodeURIComponent(query)}&limit=200` : "?limit=200";
  const res = await window.secondBrain.request("GET", `/v1/notes${q}`);
  if (!res.ok) return;
  notesCache = res.data?.items || res.data?.notes || [];
  const listEl = document.getElementById("notesList");
  listEl.replaceChildren();
  for (const note of notesCache) {
    const item = document.createElement("div");
    item.className = "note-item" + (activeNote && activeNote.filename === note.filename ? " active" : "");
    const title = document.createElement("div");
    title.className = "note-item-title";
    title.textContent = note.title || note.filename;
    const meta = document.createElement("div");
    meta.className = "note-item-meta";
    meta.textContent = `${note.filename} · ${(note.tags || []).join(", ")}`;
    item.appendChild(title);
    item.appendChild(meta);
    item.addEventListener("click", () => openNote(note.filename));
    listEl.appendChild(item);
  }
}

async function openNote(filename) {
  const res = await window.secondBrain.request("GET", `/v1/notes/${encodeURIComponent(filename)}`);
  if (!res.ok) return;
  activeNote = res.data.note || res.data;
  document.getElementById("noteTitleInput").value = activeNote.title || "";
  document.getElementById("noteTagsInput").value = (activeNote.tags || []).join(", ");
  document.getElementById("noteBodyInput").value = activeNote.body || "";

  const blRes = await window.secondBrain.request("GET", `/v1/notes/${encodeURIComponent(filename)}/backlinks`);
  const blEl = document.getElementById("noteBacklinks");
  const backlinks = blRes.ok ? (blRes.data?.items || blRes.data?.backlinks || []) : [];
  blEl.textContent = backlinks.length
    ? `Backlinks (${backlinks.length}): ${backlinks.map((b) => b.title || b.filename).join(", ")}`
    : "Backlinks: none";

  document.querySelectorAll(".note-item").forEach((el) => {
    el.classList.toggle("active", el.querySelector(".note-item-meta")?.textContent.startsWith(filename));
  });
}

function parseTags(str) {
  return str.split(",").map((s) => s.trim()).filter(Boolean);
}

async function saveActiveNote() {
  const title = document.getElementById("noteTitleInput").value.trim() || "Untitled Note";
  const tags = parseTags(document.getElementById("noteTagsInput").value);
  const body = document.getElementById("noteBodyInput").value;
  const banner = document.getElementById("notesBanner");

  if (activeNote && activeNote.filename) {
    const res = await window.secondBrain.request(
      "PUT",
      `/v1/notes/${encodeURIComponent(activeNote.filename)}`,
      { title, tags, body, expected_sha256: activeNote.sha256 }
    );
    if (res.ok) {
      activeNote = { ...(activeNote || {}), title, tags, body, ...(res.data.note || res.data) };
      banner.textContent = `Saved ${activeNote.filename} (${activeNote.sha256.slice(0, 12)})`;
      await loadNotes(document.getElementById("noteSearchInput").value);
      refreshHealth();
    } else {
      banner.textContent = `Save failed: ${res.data?.error || res.status}`;
    }
  } else {
    const res = await window.secondBrain.request("POST", "/v1/notes", { title, tags, body });
    if (res.ok) {
      activeNote = { ...(activeNote || {}), title, tags, body, ...(res.data.note || res.data) };
      banner.textContent = `Created ${activeNote.filename}`;
      await loadNotes(document.getElementById("noteSearchInput").value);
      refreshHealth();
    } else {
      banner.textContent = `Create failed: ${res.data?.error || res.status}`;
    }
  }
}

async function verifySelectionAsAtom() {
  const banner = document.getElementById("notesBanner");
  if (!activeNote || !activeNote.filename) {
    banner.textContent = "Save or select a note first.";
    return;
  }
  const textarea = document.getElementById("noteBodyInput");
  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const excerpt = (start !== end ? textarea.value.slice(start, end) : textarea.value).trim();
  if (excerpt.length < 12) {
    banner.textContent = "Select at least 12 characters of evidence to verify as an atom.";
    return;
  }
  const res = await window.secondBrain.request(
    "POST",
    `/v1/notes/${encodeURIComponent(activeNote.filename)}/verify`,
    { excerpt, actor: "desktop-owner", expected_sha256: activeNote.sha256 }
  );
  if (res.ok) {
    banner.textContent = `Verified atom created: ${res.data.atom_filename || res.data.filename}`;
    refreshHealth();
  } else {
    banner.textContent = `Verify failed: ${res.data?.error || res.status}`;
  }
}

async function deleteActiveNote() {
  if (!activeNote || !activeNote.filename) return;
  const res = await window.secondBrain.request(
    "DELETE",
    `/v1/notes/${encodeURIComponent(activeNote.filename)}`,
    { expected_sha256: activeNote.sha256 }
  );
  if (res.ok) {
    document.getElementById("notesBanner").textContent = `Deleted ${activeNote.filename}`;
    activeNote = null;
    document.getElementById("noteTitleInput").value = "";
    document.getElementById("noteTagsInput").value = "";
    document.getElementById("noteBodyInput").value = "";
    await loadNotes(document.getElementById("noteSearchInput").value);
    refreshHealth();
  }
}

document.getElementById("newNoteBtn").addEventListener("click", () => {
  activeNote = null;
  document.getElementById("noteTitleInput").value = "";
  document.getElementById("noteTagsInput").value = "";
  document.getElementById("noteBodyInput").value = "";
  document.getElementById("noteBacklinks").textContent = "Backlinks: none";
  document.getElementById("noteTitleInput").focus();
});
document.getElementById("saveNoteBtn").addEventListener("click", saveActiveNote);
document.getElementById("verifyExcerptBtn").addEventListener("click", verifySelectionAsAtom);
document.getElementById("deleteNoteBtn").addEventListener("click", deleteActiveNote);
document.getElementById("noteSearchInput").addEventListener("input", (e) => loadNotes(e.target.value));

// ─── Streaming Chat ──────────────────────────────────────────────────────────
const chatOutputEl = document.getElementById("chatOutput");
const chatSourcesEl = document.getElementById("chatSources");
const chatBannerEl = document.getElementById("chatBanner");

window.secondBrain.onChatEvent(({ event, data }) => {
  if (event === "context") {
    const ms = data.retrieval_ms !== undefined ? `${data.retrieval_ms} ms` : "instant";
    chatBannerEl.textContent = `Retrieved ${(data.sources || []).length} sources in ${ms}`;
    chatSourcesEl.replaceChildren();
    for (const src of data.sources || []) {
      const card = document.createElement("div");
      card.className = "source-card";
      const t = document.createElement("div");
      t.className = "source-card-title";
      t.textContent = src.title || src.filename || src.source_id || "Source";
      const m = document.createElement("div");
      m.className = "source-card-meta";
      m.textContent = `${src.tier || src.kind || "memory"} · ${src.citation_id || src.filename || ""}`;
      const body = document.createElement("div");
      body.textContent = src.excerpt || src.claim || src.snippet || "";
      card.appendChild(t);
      card.appendChild(m);
      card.appendChild(body);
      chatSourcesEl.appendChild(card);
    }
  } else if (event === "token") {
    chatOutputEl.textContent += data.text ?? data.token ?? "";
  } else if (event === "verified_answer") {
    chatOutputEl.textContent = data.text || data.answer || JSON.stringify(data, null, 2);
  } else if (event === "abstain") {
    chatOutputEl.textContent = `Abstained: ${data.reason || data.text || JSON.stringify(data)}`;
  } else if (event === "error") {
    chatBannerEl.textContent = `Error: ${data.error || JSON.stringify(data)}`;
  } else if (event === "done") {
    const ms = data.elapsed_ms ?? data.total_ms;
    if (ms !== undefined) {
      chatBannerEl.textContent += ` · Completed in ${ms} ms`;
    }
  }
});

async function sendChat() {
  const query = document.getElementById("chatQueryInput").value.trim();
  if (!query) return;
  const mode = document.getElementById("chatModeSelect").value;
  const project_scope = document.getElementById("chatScopeInput").value.trim() || undefined;
  chatOutputEl.textContent = "";
  chatSourcesEl.replaceChildren();
  chatBannerEl.textContent = "Retrieving...";
  await window.secondBrain.streamChat({ query, mode, project_scope });
}

document.getElementById("chatSendBtn").addEventListener("click", sendChat);
document.getElementById("chatQueryInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendChat();
});
document.getElementById("chatWarmupBtn").addEventListener("click", async () => {
  chatBannerEl.textContent = "Warming up local model...";
  const res = await window.secondBrain.request("POST", "/v1/chat/warmup", {});
  chatBannerEl.textContent = res.ok ? `Model warmed: ${JSON.stringify(res.data)}` : `Warmup failed: ${res.status}`;
});

// ─── Inbox & Review ──────────────────────────────────────────────────────────
async function loadReviewQueue() {
  const grid = document.getElementById("reviewGrid");
  grid.replaceChildren();
  const loadingBanner = document.createElement("div");
  loadingBanner.className = "status-banner";
  loadingBanner.textContent = "Loading...";
  grid.appendChild(loadingBanner);
  const [inboxRes, reviewRes] = await Promise.all([
    window.secondBrain.request("GET", "/v1/inbox/queue"),
    window.secondBrain.request("GET", "/v1/review/queue"),
  ]);
  grid.replaceChildren();

  for (const item of inboxRes.data?.items || []) {
    const card = document.createElement("div");
    card.className = "card";
    const strong = document.createElement("strong");
    strong.textContent = `Inbox: ${item.filename || ""}`;
    const meta = document.createElement("div");
    meta.className = "note-item-meta";
    meta.textContent = item.risk_flags?.join(", ") || "quarantined";
    card.appendChild(strong);
    card.appendChild(meta);
    const row = document.createElement("div");
    row.className = "toolbar";
    const promoteBtn = document.createElement("button");
    promoteBtn.className = "btn good";
    promoteBtn.textContent = "Promote";
    promoteBtn.onclick = async () => {
      await window.secondBrain.request("POST", "/v1/inbox/promote", {
        filename: item.filename,
        expected_sha256: item.sha256,
        actor: "desktop-owner",
        acknowledge_risks: true,
      });
      loadReviewQueue();
    };
    const rejectBtn = document.createElement("button");
    rejectBtn.className = "btn danger";
    rejectBtn.textContent = "Reject";
    rejectBtn.onclick = async () => {
      await window.secondBrain.request("POST", "/v1/inbox/reject", {
        filename: item.filename,
        expected_sha256: item.sha256,
        actor: "desktop-owner",
      });
      loadReviewQueue();
    };
    row.appendChild(promoteBtn);
    row.appendChild(rejectBtn);
    card.appendChild(row);
    grid.appendChild(card);
  }

  for (const atom of reviewRes.data?.items || []) {
    const card = document.createElement("div");
    card.className = "card";
    const title = document.createElement("strong");
    title.textContent = atom.filename;
    const claim = document.createElement("div");
    claim.textContent = atom.claim || atom.title || "";
    const meta = document.createElement("div");
    meta.className = "note-item-meta";
    meta.textContent = `Status: ${atom.review_status || atom.status || "pending"}`;
    const row = document.createElement("div");
    row.className = "toolbar";
    for (const [act, cls, label] of [
      ["approve", "good", "Approve"],
      ["reject", "danger", "Reject"],
    ]) {
      const btn = document.createElement("button");
      btn.className = `btn ${cls}`;
      btn.textContent = label;
      btn.onclick = async () => {
        await window.secondBrain.request(
          "POST",
          `/v1/atoms/${encodeURIComponent(atom.filename)}/${act}`,
          { expected_sha256: atom.sha256, actor: "desktop-owner" }
        );
        loadReviewQueue();
        refreshHealth();
      };
      row.appendChild(btn);
    }
    card.appendChild(title);
    card.appendChild(claim);
    card.appendChild(meta);
    card.appendChild(row);
    grid.appendChild(card);
  }
  if (!grid.children.length) {
    const emptyBanner = document.createElement("div");
    emptyBanner.className = "status-banner";
    emptyBanner.textContent = "Inbox and review queues are clear.";
    grid.appendChild(emptyBanner);
  }
}
document.getElementById("refreshReviewBtn").addEventListener("click", loadReviewQueue);

// ─── Bulk Import ─────────────────────────────────────────────────────────────
document.getElementById("pickImportDirBtn").addEventListener("click", async () => {
  const dir = await window.secondBrain.pickFolder();
  if (dir) document.getElementById("importDirInput").value = dir;
});
document.getElementById("runImportBtn").addEventListener("click", async () => {
  const source_dir = document.getElementById("importDirInput").value.trim();
  if (!source_dir) return;
  const tags = parseTags(document.getElementById("importTagsInput").value);
  const resEl = document.getElementById("importResult");
  resEl.textContent = "Importing...";
  const res = await window.secondBrain.request("POST", "/v1/import", { source_dir, tags });
  resEl.textContent = res.ok ? JSON.stringify(res.data) : `Import failed: ${res.data?.error || res.status}`;
  loadNotes();
  refreshHealth();
});

// ─── IDE Hub ─────────────────────────────────────────────────────────────────
async function loadIdeStatus() {
  const grid = document.getElementById("ideGrid");
  grid.replaceChildren();
  const checkingBanner = document.createElement("div");
  checkingBanner.className = "status-banner";
  checkingBanner.textContent = "Checking IDE integrations...";
  grid.appendChild(checkingBanner);
  try {
    const status = await window.secondBrain.ideAction("status");
    grid.replaceChildren();
    const targets = status.targets || status;
    for (const [key, info] of Object.entries(targets)) {
      const card = document.createElement("div");
      card.className = "card";
      const connected = Boolean(info.connected);
      const titleEl = document.createElement("strong");
      titleEl.textContent = info.name || info.label || key;
      const pathEl = document.createElement("div");
      pathEl.className = "note-item-meta";
      pathEl.textContent = info.config_path || info.path || "";
      const pillWrap = document.createElement("div");
      const pill = document.createElement("span");
      pill.className = "pill";
      pill.textContent = connected ? "Connected" : (info.installed ? "Available" : "Not installed");
      pillWrap.appendChild(pill);
      card.appendChild(titleEl);
      card.appendChild(pathEl);
      card.appendChild(pillWrap);
      const btn = document.createElement("button");
      btn.className = connected ? "btn danger" : "btn primary";
      btn.textContent = connected ? "Disconnect" : "1-Click Connect";
      btn.onclick = async () => {
        await window.secondBrain.ideAction(connected ? "remove" : "add", key);
        loadIdeStatus();
      };
      card.appendChild(btn);
      grid.appendChild(card);
    }
  } catch (err) {
    grid.replaceChildren();
    const errBanner = document.createElement("div");
    errBanner.className = "status-banner";
    errBanner.textContent = `IDE status error: ${err?.message || String(err)}`;
    grid.appendChild(errBanner);
  }
}
document.getElementById("refreshIdeBtn").addEventListener("click", loadIdeStatus);

// ─── System View ─────────────────────────────────────────────────────────────
async function loadSystemView() {
  const grid = document.getElementById("systemGrid");
  grid.replaceChildren();
  const loadingBanner = document.createElement("div");
  loadingBanner.className = "status-banner";
  loadingBanner.textContent = "Loading...";
  grid.appendChild(loadingBanner);
  const [health, profile, playbooks] = await Promise.all([
    window.secondBrain.request("GET", "/v1/health"),
    window.secondBrain.request("GET", "/v1/profile"),
    window.secondBrain.request("GET", "/v1/playbooks"),
  ]);
  grid.replaceChildren();
  const sections = [
    ["Health", health.data],
    ["Hardware Profile", profile.data],
    ["Verified Playbooks", playbooks.data],
  ];
  for (const [heading, data] of sections) {
    const card = document.createElement("div");
    card.className = "card";
    const strong = document.createElement("strong");
    strong.textContent = heading;
    const pre = document.createElement("pre");
    pre.style.whiteSpace = "pre-wrap";
    pre.style.fontSize = "12px";
    pre.textContent = JSON.stringify(data, null, 2);
    card.appendChild(strong);
    card.appendChild(pre);
    grid.appendChild(card);
  }
}
document.getElementById("refreshSystemBtn").addEventListener("click", loadSystemView);

// ─── Global Shortcuts & Init ─────────────────────────────────────────────────
window.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    saveActiveNote();
  }
  const tabs = ["notes", "chat", "review", "import", "ide", "system"];
  if ((e.ctrlKey || e.metaKey) && e.key >= "1" && e.key <= "6") {
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
