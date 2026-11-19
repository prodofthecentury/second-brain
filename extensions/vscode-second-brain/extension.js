"use strict";

const vscode = require("vscode");
const http = require("http");
const fs = require("fs");
const path = require("path");
const os = require("os");
const crypto = require("crypto");

const {
  computeClaimGroundingUnits,
  formatPlanMarkdown,
  formatRadarMarkdown,
  formatSnapshotDiffMarkdown,
  formatSnapshotRecallMarkdown,
  formatSnapshotVerifyMarkdown,
  formatPlaybookMarkdown,
  formatAtomProofMarkdown,
  formatInboxPreviewMarkdown,
  formatTasksMarkdown,
  formatSyncStatusMarkdown,
} = require("./lib/markdown-formatters");
const {
  isSafeNoteFilename,
  SecondBrainNoteFileSystemProvider,
} = require("./lib/notes-fs");
const {
  parseSafeExternalUrl,
  buildChatWebviewHtml,
} = require("./lib/chat-webview");

const DEFAULT_API_PORT = 8765;
const LOOPBACK_HOST = "127.0.0.1";
const VALID_CHAT_MODES = new Set(["instant", "verified", "stream"]);
const VALID_RETRIEVAL_MODES = new Set(["auto", "lexical", "dense", "rrf"]);
const VALID_ANSWER_VIEWS = new Set(["standard", "preference", "research"]);
const VALID_MEMORY_TYPES = ["semantic", "procedural", "episodic", "self"];

function getSecurityDir() {
  const envDir = (
    process.env.BRAIN_SECURITY_STATE_DIR ||
    process.env.SECOND_BRAIN_SECURITY_DIR ||
    ""
  ).trim();
  if (envDir) {
    return path.resolve(envDir);
  }
  if (process.platform === "win32") {
    const localAppData =
      process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
    return path.join(localAppData, "SecondBrain", "security");
  }
  const stateHome =
    process.env.XDG_STATE_HOME || path.join(os.homedir(), ".local", "state");
  return path.join(stateHome, "SecondBrain", "security");
}

function readSafeTokenFile(filePath) {
  if (!filePath) return "";
  try {
    const resolved = path.resolve(filePath);
    const parentStat = fs.lstatSync(path.dirname(resolved));
    if (!parentStat.isDirectory() || parentStat.isSymbolicLink()) {
      return "";
    }
    const st = fs.lstatSync(resolved);
    if (!st.isFile() || st.isSymbolicLink() || st.size > 4096) {
      return "";
    }
    return fs.readFileSync(resolved, "utf8").trim();
  } catch {
    return "";
  }
}

function readBearerToken() {
  const envToken = (process.env.BRAIN_MEMORY_API_TOKEN || "").trim();
  if (envToken) return envToken;
  const envFile = (process.env.BRAIN_MEMORY_API_TOKEN_FILE || "").trim();
  if (envFile) {
    const fromEnvFile = readSafeTokenFile(envFile);
    if (fromEnvFile) return fromEnvFile;
  }
  const secDir = getSecurityDir();
  return (
    readSafeTokenFile(path.join(secDir, "memory-api-token")) ||
    readSafeTokenFile(path.join(secDir, "api-token"))
  );
}

function readOwnerToken() {
  const envOwnerToken =
    (process.env.BRAIN_OWNER_API_TOKEN || "").trim() ||
    (process.env.BRAIN_OWNER_TOKEN || "").trim();
  if (envOwnerToken && envOwnerToken.length >= 16) return envOwnerToken;

  const inspectedPath = vscode.workspace
    .getConfiguration("secondBrain")
    .inspect("ownerTokenPath");
  const configuredOwnerPath = String(inspectedPath?.globalValue || "").trim();
  const envOwnerFile =
    (process.env.BRAIN_OWNER_TOKEN_FILE || "").trim() ||
    (process.env.BRAIN_OWNER_API_TOKEN_FILE || "").trim();
  const secDir = getSecurityDir();

  const candidate =
    (configuredOwnerPath && readSafeTokenFile(configuredOwnerPath)) ||
    (envOwnerFile && readSafeTokenFile(envOwnerFile)) ||
    readSafeTokenFile(path.join(secDir, "owner-token")) ||
    readSafeTokenFile(path.join(secDir, "owner-api-token"));
  return candidate && candidate.length >= 16 ? candidate : "";
}

function isPidAlive(pid) {
  const parsed = Number(pid);
  if (!Number.isInteger(parsed) || parsed <= 0) return false;
  if (parsed === process.pid) return true;
  try {
    process.kill(parsed, 0);
    return true;
  } catch (err) {
    return Boolean(err && err.code === "EPERM");
  }
}

function normalizeValidPort(raw) {
  const parsed = Math.trunc(Number(raw));
  if (!Number.isFinite(parsed) || parsed < 1 || parsed > 65535) {
    return null;
  }
  return parsed;
}

function getApiPortFilePath() {
  const envPortFile = (process.env.BRAIN_API_PORT_FILE || "").trim();
  if (envPortFile) return path.resolve(envPortFile);
  return path.join(getSecurityDir(), "api-port");
}

function discoverPortFromStateFile() {
  const raw = readSafeTokenFile(getApiPortFilePath());
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    if (parsed.host !== undefined && parsed.host !== LOOPBACK_HOST) {
      return null;
    }
    const port = normalizeValidPort(parsed.port);
    if (!port) return null;
    if (parsed.pid !== undefined && parsed.pid !== null) {
      if (!isPidAlive(parsed.pid)) return null;
    }
    return port;
  } catch {
    return null;
  }
}

function hasExplicitGlobalPortSetting() {
  const inspected = vscode.workspace
    .getConfiguration("secondBrain")
    .inspect("apiPort");
  return (
    inspected !== undefined &&
    inspected !== null &&
    inspected.globalValue !== undefined &&
    inspected.globalValue !== null
  );
}

function getPort() {
  const inspected = vscode.workspace
    .getConfiguration("secondBrain")
    .inspect("apiPort");
  if (inspected?.globalValue !== undefined && inspected?.globalValue !== null) {
    return normalizeValidPort(inspected.globalValue) ?? DEFAULT_API_PORT;
  }
  const discovered = discoverPortFromStateFile();
  if (discovered) return discovered;
  return DEFAULT_API_PORT;
}

function executeHttpJson(port, method, apiPath, payload, token, ownerToken) {
  const headers = {
    Host: `${LOOPBACK_HOST}:${port}`,
    Accept: "application/json",
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (ownerToken) headers["X-Owner-Token"] = ownerToken;
  if (payload !== null) {
    headers["Content-Type"] = "application/json";
    headers["Content-Length"] = Buffer.byteLength(payload, "utf8");
  }
  return new Promise((resolve, reject) => {
    const req = http.request(
      { hostname: LOOPBACK_HOST, port, path: apiPath, method, headers },
      (res) => {
        let raw = "";
        res.setEncoding("utf8");
        res.on("data", (c) => (raw += c));
        res.on("end", () => {
          try {
            resolve({
              ok: res.statusCode >= 200 && res.statusCode < 300,
              status: res.statusCode,
              data: raw ? JSON.parse(raw) : {},
            });
          } catch {
            resolve({
              ok: false,
              status: res.statusCode,
              data: { raw },
            });
          }
        });
      }
    );
    req.setTimeout(30000, () => {
      req.destroy(new Error("Loopback API request timed out."));
    });
    req.on("error", reject);
    if (payload !== null) req.write(payload);
    req.end();
  });
}

async function apiRequest(method, apiPath, body = null, { useOwnerToken = true } = {}) {
  const port = getPort();
  const token = readBearerToken();
  const ownerToken = useOwnerToken ? readOwnerToken() : "";
  const payload = body !== null && body !== undefined ? JSON.stringify(body) : null;
  try {
    return await executeHttpJson(port, method, apiPath, payload, token, ownerToken);
  } catch (err) {
    if (!hasExplicitGlobalPortSetting()) {
      const rediscoveredPort = getPort();
      if (rediscoveredPort !== port) {
        return executeHttpJson(
          rediscoveredPort,
          method,
          apiPath,
          payload,
          token,
          ownerToken
        );
      }
    }
    throw err;
  }
}

async function openMarkdownPreview(markdownText) {
  const doc = await vscode.workspace.openTextDocument({
    content: markdownText,
    language: "markdown",
  });
  await vscode.window.showTextDocument(doc, { preview: true });
  return doc;
}

function parseListInput(raw, delimiterRegex = /[|,\n]+/) {
  if (Array.isArray(raw)) {
    return raw.map((x) => String(x || "").trim()).filter(Boolean);
  }
  return String(raw || "")
    .split(delimiterRegex)
    .map((s) => s.trim())
    .filter(Boolean);
}

class VaultTreeProvider {
  constructor(onRefreshCallback = null, { pageSize = 50 } = {}) {
    this._onDidChangeTreeData = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._onDidChangeTreeData.event;
    this._onRefreshCallback = onRefreshCallback;
    this._pageSize = Math.max(1, Math.min(500, Math.trunc(Number(pageSize) || 50)));
    this._pages = Object.create(null);
  }

  setPageSize(pageSize) {
    this._pageSize = Math.max(1, Math.min(500, Math.trunc(Number(pageSize) || 50)));
    this._pages = Object.create(null);
  }

  refresh() {
    this._pages = Object.create(null);
    this._onDidChangeTreeData.fire();
    if (typeof this._onRefreshCallback === "function") {
      this._onRefreshCallback();
    }
  }

  _buildSectionEndpoint(section, limit, offset) {
    const q = `limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}`;
    switch (section) {
      case "notes":
        return `/v1/notes?${q}`;
      case "inbox":
        return `/v1/inbox/queue?${q}`;
      case "review":
        return `/v1/review/queue?${q}`;
      case "atoms":
        return `/v1/atoms?${q}`;
      case "playbooks":
        return `/v1/playbooks?${q}`;
      default:
        return "";
    }
  }

  _extractSectionItems(section, data) {
    if (!data || typeof data !== "object") return [];
    if (Array.isArray(data.items)) return data.items;
    if (section === "notes" && Array.isArray(data.notes)) return data.notes;
    if (section === "atoms" && Array.isArray(data.atoms)) return data.atoms;
    if (section === "playbooks" && Array.isArray(data.playbooks)) return data.playbooks;
    return [];
  }

  async _ensureFirstPage(section) {
    if (this._pages[section]) {
      return this._pages[section];
    }
    const endpoint = this._buildSectionEndpoint(section, this._pageSize, 0);
    const res = await apiRequest("GET", endpoint, null, { useOwnerToken: true });
    if (!res.ok) {
      return { ok: false, status: res.status, error: res.data?.error || "" };
    }
    const items = this._extractSectionItems(section, res.data);
    const total = Number.isFinite(Number(res.data?.total))
      ? Number(res.data.total)
      : items.length;
    const effectiveLimit = Number.isFinite(Number(res.data?.limit))
      ? Math.max(1, Number(res.data.limit))
      : this._pageSize;
    const state = {
      ok: true,
      items: [...items],
      offset: items.length,
      limit: effectiveLimit,
      total,
    };
    this._pages[section] = state;
    return state;
  }

  async loadMore(section, requestedOffset, requestedLimit) {
    if (!section) return;
    const current = this._pages[section];
    const offset =
      requestedOffset !== undefined
        ? Math.max(0, Math.trunc(Number(requestedOffset)))
        : current
          ? current.offset
          : 0;
    const limit =
      requestedLimit !== undefined
        ? Math.max(1, Math.min(500, Math.trunc(Number(requestedLimit))))
        : current
          ? current.limit
          : this._pageSize;

    const endpoint = this._buildSectionEndpoint(section, limit, offset);
    if (!endpoint) return;
    const res = await apiRequest("GET", endpoint, null, { useOwnerToken: true });
    if (!res.ok) {
      vscode.window.showErrorMessage(
        `Could not load more ${section}: ${res.data?.error || res.status}`
      );
      return;
    }
    const nextBatch = this._extractSectionItems(section, res.data);
    const prevItems = current && Array.isArray(current.items) ? current.items : [];
    const merged = [...prevItems, ...nextBatch];
    const total = Number.isFinite(Number(res.data?.total))
      ? Number(res.data.total)
      : merged.length;
    this._pages[section] = {
      ok: true,
      items: merged,
      offset: offset + nextBatch.length,
      limit,
      total,
    };
    this._onDidChangeTreeData.fire();
  }

  _appendLoadMoreIfNeeded(children, section, pageState) {
    if (
      pageState.items.length > 0 &&
      pageState.offset < pageState.total
    ) {
      const moreItem = new vscode.TreeItem(
        "Load More...",
        vscode.TreeItemCollapsibleState.None
      );
      moreItem.description = `${pageState.items.length} of ${pageState.total}`;
      moreItem.tooltip = `Load next page of ${section} (offset ${pageState.offset})`;
      moreItem.contextValue = "secondBrain.loadMore";
      moreItem.iconPath = new vscode.ThemeIcon("ellipsis");
      moreItem.command = {
        command: "secondBrain.loadMoreSection",
        title: "Load More",
        arguments: [
          {
            section,
            offset: pageState.offset,
            limit: pageState.limit,
          },
        ],
      };
      children.push(moreItem);
    }
    return children;
  }

  getTreeItem(el) {
    return el;
  }

  async getChildren(element) {
    if (!element) {
      const makeRoot = (label, state, sectionKey) => {
        const ti = new vscode.TreeItem(label, state);
        ti.sectionKey = sectionKey;
        return ti;
      };
      return [
        makeRoot("Notes", vscode.TreeItemCollapsibleState.Expanded, "notes"),
        makeRoot(
          "Quarantined Inbox",
          vscode.TreeItemCollapsibleState.Collapsed,
          "inbox"
        ),
        makeRoot(
          "Review Queue",
          vscode.TreeItemCollapsibleState.Collapsed,
          "review"
        ),
        makeRoot(
          "Verified Atoms",
          vscode.TreeItemCollapsibleState.Collapsed,
          "atoms"
        ),
        makeRoot(
          "Playbooks",
          vscode.TreeItemCollapsibleState.Collapsed,
          "playbooks"
        ),
        makeRoot(
          "Snapshots",
          vscode.TreeItemCollapsibleState.Collapsed,
          "snapshots"
        ),
        makeRoot(
          "Contradiction & Drift Radar",
          vscode.TreeItemCollapsibleState.Collapsed,
          "radar"
        ),
      ];
    }

    const label = element.label;
    try {
      if (label === "Notes") {
        const page = await this._ensureFirstPage("notes");
        if (!page.ok) {
          return [
            new vscode.TreeItem("API offline — start Second Brain desktop or CLI"),
          ];
        }
        if (!page.items.length) {
          return [new vscode.TreeItem("No working notes yet")];
        }
        const children = page.items.map((n) => {
          const item = new vscode.TreeItem(
            n.title || n.filename,
            vscode.TreeItemCollapsibleState.None
          );
          item.description = (n.tags || []).join(", ");
          item.tooltip = `${n.filename} — click to edit working note`;
          item.contextValue = "secondBrain.note";
          item.iconPath = new vscode.ThemeIcon("note");
          item.sbData = { kind: "note", filename: n.filename, sha256: n.sha256, note: n };
          item.command = {
            command: "secondBrain.openTreeItem",
            title: "Edit Working Note",
            arguments: [{ kind: "note", filename: n.filename, note: n }],
          };
          return item;
        });
        return this._appendLoadMoreIfNeeded(children, "notes", page);
      }

      if (label === "Quarantined Inbox") {
        const page = await this._ensureFirstPage("inbox");
        if (!page.ok) {
          return [
            new vscode.TreeItem(
              page.status === 403
                ? "Owner token required to inspect Quarantined Inbox"
                : "API offline"
            ),
          ];
        }
        if (!page.items.length) {
          return [new vscode.TreeItem("No quarantined inbox items")];
        }
        const children = page.items.map((it) => {
          const risks = Array.isArray(it.risk_flags) ? it.risk_flags : [];
          const item = new vscode.TreeItem(
            it.filename,
            vscode.TreeItemCollapsibleState.None
          );
          item.description = risks.length
            ? `risks: ${risks.join(", ")}`
            : "quarantined";
          item.tooltip = it.preview || it.filename;
          item.contextValue = "secondBrain.inboxItem";
          item.iconPath = new vscode.ThemeIcon(risks.length ? "warning" : "mail");
          item.sbData = {
            kind: "inbox",
            filename: it.filename,
            sha256: it.sha256 || "",
            risk_flags: risks,
            inboxItem: it,
          };
          item.command = {
            command: "secondBrain.openTreeItem",
            title: "Preview Quarantined Inbox Item",
            arguments: [{ kind: "inbox", filename: it.filename }],
          };
          return item;
        });
        return this._appendLoadMoreIfNeeded(children, "inbox", page);
      }

      if (label === "Review Queue") {
        const page = await this._ensureFirstPage("review");
        if (!page.ok) {
          return [
            new vscode.TreeItem(
              page.status === 403
                ? "Owner token required to view Review Queue"
                : "API offline"
            ),
          ];
        }
        if (!page.items.length) {
          return [new vscode.TreeItem("No pending atoms awaiting review")];
        }
        const children = page.items.map((a) => {
          const item = new vscode.TreeItem(
            a.filename,
            vscode.TreeItemCollapsibleState.None
          );
          item.description = a.review_status || "pending";
          item.tooltip = a.claim || a.filename;
          item.contextValue = "secondBrain.pendingAtom";
          item.iconPath = new vscode.ThemeIcon("eye");
          item.sbData = {
            kind: "atom",
            filename: a.filename,
            sha256: a.sha256 || "",
            atom: a,
          };
          item.command = {
            command: "secondBrain.openTreeItem",
            title: "Inspect Pending Atom",
            arguments: [{ kind: "atom", filename: a.filename, atom: a }],
          };
          return item;
        });
        return this._appendLoadMoreIfNeeded(children, "review", page);
      }

      if (label === "Verified Atoms") {
        const page = await this._ensureFirstPage("atoms");
        if (!page.ok) return [new vscode.TreeItem("API offline")];
        if (!page.items.length) {
          return [new vscode.TreeItem("No verified atoms yet")];
        }
        const children = page.items.map((a) => {
          const item = new vscode.TreeItem(
            a.filename,
            vscode.TreeItemCollapsibleState.None
          );
          item.description = a.review_status || a.status || "";
          item.tooltip = a.claim || a.filename;
          item.contextValue = "secondBrain.verifiedAtom";
          item.iconPath = new vscode.ThemeIcon("shield");
          item.sbData = {
            kind: "atom",
            filename: a.filename,
            sha256: a.sha256 || "",
            atom: a,
          };
          item.command = {
            command: "secondBrain.openTreeItem",
            title: "Open Verified Atom",
            arguments: [{ kind: "atom", filename: a.filename, atom: a }],
          };
          return item;
        });
        return this._appendLoadMoreIfNeeded(children, "atoms", page);
      }

      if (label === "Playbooks") {
        const page = await this._ensureFirstPage("playbooks");
        if (!page.ok) return [new vscode.TreeItem("API offline")];
        if (!page.items.length) {
          return [new vscode.TreeItem("No promoted playbooks yet")];
        }
        const children = page.items.map((pb) => {
          const item = new vscode.TreeItem(
            pb.title || pb.filename,
            vscode.TreeItemCollapsibleState.None
          );
          item.description = pb.status || "accepted";
          item.tooltip = pb.trigger || pb.filename;
          item.contextValue = "secondBrain.playbook";
          item.iconPath = new vscode.ThemeIcon("checklist");
          item.sbData = {
            kind: "playbook",
            filename: pb.filename || `${pb.playbook_id}.md`,
            sha256: pb.sha256 || "",
            playbook: pb,
          };
          item.command = {
            command: "secondBrain.openTreeItem",
            title: "Open Playbook Checklist",
            arguments: [{ kind: "playbook", playbook: pb }],
          };
          return item;
        });
        return this._appendLoadMoreIfNeeded(children, "playbooks", page);
      }

      if (label === "Snapshots") {
        const res = await apiRequest("GET", "/v1/snapshots");
        if (!res.ok) return [new vscode.TreeItem("API offline")];
        const snaps = res.data?.items || res.data?.snapshots || [];
        if (!snaps.length) {
          return [new vscode.TreeItem("No snapshots recorded yet")];
        }
        return snaps.map((s) => {
          const item = new vscode.TreeItem(
            s.snapshot_id,
            vscode.TreeItemCollapsibleState.None
          );
          item.description = `${s.atom_count || 0} atoms${s.label ? " · " + s.label : ""}`;
          item.tooltip = `Merkle: ${s.merkle_root || ""} — click to diff vs live`;
          item.contextValue = "secondBrain.snapshot";
          item.iconPath = new vscode.ThemeIcon("history");
          item.sbData = {
            kind: "snapshot",
            snapshot_id: s.snapshot_id,
            snapshot: s,
          };
          item.command = {
            command: "secondBrain.openTreeItem",
            title: "Diff Snapshot vs Live",
            arguments: [{ kind: "snapshot", snapshot_id: s.snapshot_id }],
          };
          return item;
        });
      }

      if (label === "Contradiction & Drift Radar") {
        const res = await apiRequest("GET", "/v1/graph/contradictions");
        if (!res.ok) return [new vscode.TreeItem("API offline")];
        const alerts = res.data?.alerts || [];
        const drift = res.data?.drift_alerts || [];
        const orphans = res.data?.orphan_approved_atoms || [];
        const children = [];

        if (!alerts.length && !drift.length) {
          const cleanItem = new vscode.TreeItem(
            `0 conflicts · ${orphans.length} orphan atom(s)`,
            vscode.TreeItemCollapsibleState.None
          );
          cleanItem.iconPath = new vscode.ThemeIcon("pass");
          cleanItem.command = {
            command: "secondBrain.openTreeItem",
            title: "Open Radar Report",
            arguments: [{ kind: "radar", report: res.data }],
          };
          children.push(cleanItem);
        } else {
          for (const al of alerts) {
            const alertLabel = `[${String(al.severity || "medium").toUpperCase()}] ${
              al.atom_a?.filename || ""
            } vs ${al.atom_b?.filename || ""}`;
            const item = new vscode.TreeItem(
              alertLabel,
              vscode.TreeItemCollapsibleState.None
            );
            item.description = (al.reasons || []).join(", ");
            item.iconPath = new vscode.ThemeIcon("warning");
            item.command = {
              command: "secondBrain.openTreeItem",
              title: "Open Radar Report",
              arguments: [{ kind: "radar", report: res.data }],
            };
            children.push(item);
          }
          for (const dr of drift) {
            const item = new vscode.TreeItem(
              `[DRIFT] ${dr.filename || dr.atom_id || "Atom"}`,
              vscode.TreeItemCollapsibleState.None
            );
            item.description = "source anchor drift";
            item.iconPath = new vscode.ThemeIcon("error");
            item.command = {
              command: "secondBrain.openTreeItem",
              title: "Open Radar Report",
              arguments: [{ kind: "radar", report: res.data }],
            };
            children.push(item);
          }
        }
        return children;
      }
    } catch {
      return [new vscode.TreeItem("Start Second Brain API (`second-brain serve`)")];
    }
    return [];
  }
}

let activeExtensionState = null;

function extractTargetData(arg) {
  if (!arg) return {};
  if (typeof arg === "string") return { filename: arg };
  if (arg.sbData && typeof arg.sbData === "object") return arg.sbData;
  if (typeof arg === "object") return arg;
  return {};
}

function activate(context) {
  const statusBarItem = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right,
    100
  );
  statusBarItem.command = "secondBrain.quickRecall";
  statusBarItem.tooltip =
    "Second Brain Local Memory API — Click for Quick Recall or Streaming Chat";
  statusBarItem.text = "$(pulse) Second Brain: checking...";
  statusBarItem.show();
  context.subscriptions.push(statusBarItem);

  async function updateStatusBar() {
    try {
      const res = await apiRequest("GET", "/v1/health");
      if (res.ok && res.data) {
        const approved = res.data.approved_atom_count ?? res.data.atom_count ?? 0;
        statusBarItem.text = `$(pulse) Second Brain (${approved})`;
        statusBarItem.tooltip = `Second Brain Online (${LOOPBACK_HOST}:${getPort()}) · ${approved} approved atoms · Tier: ${
          res.data.hardware_tier || "local"
        }\nClick for Quick Recall or Streaming Chat`;
      } else {
        statusBarItem.text = "$(pulse) Second Brain: auth/error";
      }
    } catch {
      statusBarItem.text = "$(pulse) Second Brain: offline";
      statusBarItem.tooltip =
        "Second Brain API offline — launch Second Brain Desktop or run `second-brain serve`";
    }
  }

  const treeProvider = new VaultTreeProvider(() => updateStatusBar());
  context.subscriptions.push(
    vscode.window.registerTreeDataProvider("secondBrain.vaultView", treeProvider)
  );

  const noteFsProvider = new SecondBrainNoteFileSystemProvider({
    vscode,
    apiRequest,
    onVaultMutated: () => treeProvider.refresh(),
  });
  if (typeof vscode.workspace.registerFileSystemProvider === "function") {
    context.subscriptions.push(
      vscode.workspace.registerFileSystemProvider(
        "secondbrain-note",
        noteFsProvider,
        { isCaseSensitive: true }
      )
    );
  }

  async function openEditableNote(filename, initialNoteMeta = null) {
    if (!isSafeNoteFilename(filename)) {
      vscode.window.showErrorMessage(`Invalid note filename: ${filename}`);
      return null;
    }
    if (initialNoteMeta && initialNoteMeta.sha256) {
      noteFsProvider.primeNoteMetadata(filename, {
        sha256: initialNoteMeta.sha256,
        title: initialNoteMeta.title || "",
        tags: initialNoteMeta.tags || [],
        body: initialNoteMeta.body || "",
      });
    }
    const uri = SecondBrainNoteFileSystemProvider.toNoteUri(vscode, filename);
    const doc = await vscode.workspace.openTextDocument(uri);
    await vscode.window.showTextDocument(doc, { preview: false });
    return doc;
  }

  let chatPanel = null;
  let activeChatStream = null;
  let lastChatSourcesByCitation = Object.create(null);

  function abortActiveChatStream() {
    if (!activeChatStream) return false;
    const stream = activeChatStream;
    activeChatStream = null;
    stream.aborted = true;
    try {
      if (stream.res && !stream.res.destroyed) {
        stream.res.destroy();
      }
    } catch {}
    try {
      if (stream.req && !stream.req.destroyed) {
        stream.req.destroy();
      }
    } catch {}
    return true;
  }

  activeExtensionState = {
    abortActiveChatStream,
    disposeChatPanel() {
      abortActiveChatStream();
      if (chatPanel) {
        const p = chatPanel;
        chatPanel = null;
        try {
          p.dispose();
        } catch {}
      }
    },
    treeProvider,
    noteFsProvider,
  };

  context.subscriptions.push({
    dispose: () => {
      abortActiveChatStream();
      chatPanel = null;
    },
  });

  updateStatusBar();
  const pollTimer = setInterval(updateStatusBar, 30000);
  if (typeof pollTimer.unref === "function") pollTimer.unref();
  context.subscriptions.push({ dispose: () => clearInterval(pollTimer) });

  async function resolveAtomSha(filename, candidateSha = "") {
    if (candidateSha) return String(candidateSha);
    const res = await apiRequest(
      "GET",
      `/v1/atoms/${encodeURIComponent(filename)}`,
      null,
      { useOwnerToken: true }
    );
    if (!res.ok) {
      throw new Error(res.data?.error || `Could not inspect atom (${res.status})`);
    }
    const atom = res.data?.atom || res.data || {};
    return String(atom.sha256 || "");
  }

  async function pickAtomFromEndpoint(endpoint, filterFn, placeHolder) {
    const res = await apiRequest("GET", endpoint, null, { useOwnerToken: true });
    if (!res.ok) {
      vscode.window.showErrorMessage(
        `Could not list atoms: ${res.data?.error || res.status}`
      );
      return null;
    }
    const allItems = res.data?.items || res.data?.atoms || [];
    const list = filterFn ? allItems.filter(filterFn) : allItems;
    if (!list.length) {
      vscode.window.showInformationMessage("No matching atoms available.");
      return null;
    }
    return vscode.window.showQuickPick(
      list.map((a) => ({
        label: a.filename,
        description: a.review_status || "",
        detail: a.claim || "",
        filename: a.filename,
        sha256: a.sha256 || "",
        atom: a,
      })),
      { placeHolder }
    );
  }

  async function runAtomTransition(action, arg, endpoint, filterFn, verbLabel) {
    try {
      const target = extractTargetData(arg);
      let filename = target.filename;
      let sha256 = target.sha256 || target.atom?.sha256 || "";
      if (!filename) {
        const picked = await pickAtomFromEndpoint(
          endpoint,
          filterFn,
          `Select an atom to ${action}`
        );
        if (!picked) return;
        filename = picked.filename;
        sha256 = picked.sha256;
      }
      if (!isSafeNoteFilename(filename)) {
        vscode.window.showErrorMessage(`Invalid atom filename: ${filename}`);
        return;
      }
      const expectedSha = await resolveAtomSha(filename, sha256);
      const res = await apiRequest(
        "POST",
        `/v1/atoms/${encodeURIComponent(filename)}/${action}`,
        {
          expected_sha256: expectedSha,
          actor: String(target.actor || "vscode_owner"),
        },
        { useOwnerToken: true }
      );
      if (!res.ok) {
        vscode.window.showErrorMessage(
          `Atom ${action} failed (${res.status}): ${res.data?.error || "error"}`
        );
        return;
      }
      vscode.window.showInformationMessage(`${verbLabel}: ${filename}`);
      treeProvider.refresh();
    } catch (err) {
      vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
    }
  }

  context.subscriptions.push(
    vscode.commands.registerCommand("secondBrain.refreshVault", () => {
      treeProvider.refresh();
    }),

    vscode.commands.registerCommand(
      "secondBrain.loadMoreSection",
      async (payload) => {
        if (!payload || typeof payload !== "object") return;
        await treeProvider.loadMore(
          String(payload.section || ""),
          payload.offset,
          payload.limit
        );
      }
    ),

    vscode.commands.registerCommand("secondBrain.openTreeItem", async (payload) => {
      if (!payload || typeof payload !== "object") return;
      try {
        if (payload.kind === "note" && payload.filename) {
          await openEditableNote(payload.filename, payload.note || null);
          return;
        }

        if (payload.kind === "inbox" && payload.filename) {
          if (!isSafeNoteFilename(payload.filename)) return;
          const inboxRes = await apiRequest(
            "GET",
            `/v1/inbox/${encodeURIComponent(payload.filename)}`,
            null,
            { useOwnerToken: true }
          );
          if (!inboxRes.ok) {
            vscode.window.showErrorMessage(
              `Could not inspect quarantined item: ${
                inboxRes.data?.error || inboxRes.status
              }`
            );
            return;
          }
          await openMarkdownPreview(
            formatInboxPreviewMarkdown(payload.filename, inboxRes.data)
          );
          return;
        }

        if (payload.kind === "atom" && payload.filename) {
          if (!isSafeNoteFilename(payload.filename)) return;
          const [atomRes, proofRes] = await Promise.all([
            apiRequest("GET", `/v1/atoms/${encodeURIComponent(payload.filename)}`),
            apiRequest(
              "GET",
              `/v1/ledger/prove/${encodeURIComponent(payload.filename)}`
            ),
          ]);
          const atom = atomRes.ok
            ? atomRes.data?.atom || atomRes.data
            : payload.atom || {};
          const proof = proofRes.ok ? proofRes.data : null;
          await openMarkdownPreview(
            formatAtomProofMarkdown(payload.filename, atom, proof)
          );
          return;
        }

        if (payload.kind === "playbook" && payload.playbook) {
          await openMarkdownPreview(formatPlaybookMarkdown(payload.playbook));
          return;
        }

        if (payload.kind === "snapshot" && payload.snapshot_id) {
          const diffRes = await apiRequest("POST", "/v1/snapshots/diff", {
            from_snapshot: payload.snapshot_id,
            to_snapshot: "live",
          });
          if (!diffRes.ok) {
            vscode.window.showErrorMessage(
              `Snapshot diff failed: ${diffRes.data?.error || diffRes.status}`
            );
            return;
          }
          await openMarkdownPreview(formatSnapshotDiffMarkdown(diffRes.data));
          return;
        }

        if (payload.kind === "radar") {
          const report =
            payload.report ||
            (await apiRequest("GET", "/v1/graph/contradictions")).data ||
            {};
          await openMarkdownPreview(formatRadarMarkdown(report));
        }
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.createNote", async (arg) => {
      try {
        const opts = arg && typeof arg === "object" && !arg.sbData ? arg : {};
        const title =
          opts.title !== undefined
            ? String(opts.title)
            : await vscode.window.showInputBox({
                prompt: "Title for new working note",
                placeHolder: "e.g. Service Mesh Migration Notes",
              });
        if (title === undefined || !title.trim()) return;

        const rawTags =
          opts.tags !== undefined
            ? opts.tags
            : await vscode.window.showInputBox({
                prompt: "Optional comma-separated tags",
                placeHolder: "architecture, backend",
              });
        if (rawTags === undefined) return;

        const body =
          opts.body !== undefined
            ? String(opts.body)
            : `# ${title.trim()}\n\n`;

        const res = await apiRequest(
          "POST",
          "/v1/notes",
          {
            title: title.trim(),
            body,
            tags: parseListInput(rawTags, /[,|\n]+/),
          },
          { useOwnerToken: true }
        );
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Create note failed (${res.status}): ${res.data?.error || "error"}`
          );
          return;
        }
        const created = res.data?.note || res.data || {};
        treeProvider.refresh();
        if (created.filename) {
          await openEditableNote(created.filename, {
            sha256: created.sha256 || "",
            title: created.title || title.trim(),
            tags: created.tags || [],
            body: created.body ?? body,
          });
        }
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.editNote", async (arg) => {
      try {
        const target = extractTargetData(arg);
        let filename = target.filename;
        if (!filename) {
          const res = await apiRequest("GET", "/v1/notes?limit=100&offset=0");
          if (!res.ok) {
            vscode.window.showErrorMessage(
              `Could not list notes: ${res.data?.error || res.status}`
            );
            return;
          }
          const notes = res.data?.items || res.data?.notes || [];
          if (!notes.length) {
            vscode.window.showInformationMessage("No working notes found.");
            return;
          }
          const picked = await vscode.window.showQuickPick(
            notes.map((n) => ({
              label: n.title || n.filename,
              description: n.filename,
              detail: (n.tags || []).join(", "),
              filename: n.filename,
              note: n,
            })),
            { placeHolder: "Select a working note to edit" }
          );
          if (!picked) return;
          filename = picked.filename;
        }
        await openEditableNote(filename, target.note || null);
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.deleteNote", async (arg) => {
      try {
        const target = extractTargetData(arg);
        let filename = target.filename;
        let expectedSha = target.sha256 || "";
        if (!filename) {
          const res = await apiRequest("GET", "/v1/notes?limit=100&offset=0");
          if (!res.ok) {
            vscode.window.showErrorMessage(
              `Could not list notes: ${res.data?.error || res.status}`
            );
            return;
          }
          const notes = res.data?.items || res.data?.notes || [];
          if (!notes.length) {
            vscode.window.showInformationMessage("No working notes to delete.");
            return;
          }
          const picked = await vscode.window.showQuickPick(
            notes.map((n) => ({
              label: n.title || n.filename,
              description: n.filename,
              filename: n.filename,
              sha256: n.sha256 || "",
            })),
            { placeHolder: "Select a working note to delete" }
          );
          if (!picked) return;
          filename = picked.filename;
          expectedSha = picked.sha256;
        }
        if (!isSafeNoteFilename(filename)) {
          vscode.window.showErrorMessage(`Invalid note filename: ${filename}`);
          return;
        }
        if (!expectedSha) {
          expectedSha = noteFsProvider.getExpectedSha256(filename);
        }
        if (!expectedSha) {
          const getRes = await apiRequest(
            "GET",
            `/v1/notes/${encodeURIComponent(filename)}`
          );
          if (!getRes.ok) {
            vscode.window.showErrorMessage(
              `Could not load note '${filename}': ${
                getRes.data?.error || getRes.status
              }`
            );
            return;
          }
          expectedSha = String((getRes.data?.note || getRes.data || {}).sha256 || "");
        }
        const confirmed = await vscode.window.showWarningMessage(
          `Delete working note '${filename}'?`,
          { modal: true },
          "Delete"
        );
        if (confirmed !== "Delete") return;

        const delRes = await apiRequest(
          "DELETE",
          `/v1/notes/${encodeURIComponent(filename)}`,
          { expected_sha256: expectedSha },
          { useOwnerToken: true }
        );
        if (!delRes.ok) {
          vscode.window.showErrorMessage(
            `Delete note failed (${delRes.status}): ${
              delRes.data?.error || "error"
            }`
          );
          return;
        }
        noteFsProvider.forgetNote(filename);
        vscode.window.showInformationMessage(`Deleted note: ${filename}`);
        treeProvider.refresh();
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand(
      "secondBrain.verifyNoteExcerpt",
      async (arg) => {
        try {
          const target = extractTargetData(arg);
          const editor = vscode.window.activeTextEditor;
          let filename = target.filename || "";
          let excerpt = target.excerpt || "";

          if (!filename && editor?.document?.uri?.scheme === "secondbrain-note") {
            filename = SecondBrainNoteFileSystemProvider.fromNoteUri(
              editor.document.uri
            );
            if (editor.document.isDirty && typeof editor.document.save === "function") {
              await editor.document.save();
            }
          }
          if (!excerpt && editor && editor.selection) {
            excerpt = editor.document.getText(editor.selection).trim();
          }

          if (!filename) {
            const notesRes = await apiRequest("GET", "/v1/notes?limit=100&offset=0");
            if (!notesRes.ok) {
              vscode.window.showErrorMessage(
                `Could not list notes: ${notesRes.data?.error || notesRes.status}`
              );
              return;
            }
            const notes = notesRes.data?.items || notesRes.data?.notes || [];
            if (!notes.length) {
              vscode.window.showInformationMessage("No working notes available.");
              return;
            }
            const picked = await vscode.window.showQuickPick(
              notes.map((n) => ({
                label: n.title || n.filename,
                description: n.filename,
                filename: n.filename,
              })),
              { placeHolder: "Select target working note for excerpt verification" }
            );
            if (!picked) return;
            filename = picked.filename;
          }
          if (!isSafeNoteFilename(filename)) {
            vscode.window.showErrorMessage(`Invalid note filename: ${filename}`);
            return;
          }

          const noteRes = await apiRequest(
            "GET",
            `/v1/notes/${encodeURIComponent(filename)}`,
            null,
            { useOwnerToken: true }
          );
          if (!noteRes.ok) {
            vscode.window.showErrorMessage(
              `Could not fetch note '${filename}': ${
                noteRes.data?.error || noteRes.status
              }`
            );
            return;
          }
          const noteDoc = noteRes.data?.note || noteRes.data || {};
          const expectedNoteSha = String(
            target.expected_note_sha256 ||
              noteDoc.sha256 ||
              noteFsProvider.getExpectedSha256(filename) ||
              ""
          );

          if (!excerpt) {
            const inputExcerpt = await vscode.window.showInputBox({
              prompt: `Exact excerpt in '${filename}' to verify as an Atom`,
              placeHolder: "Paste or enter exact text from the note",
            });
            if (!inputExcerpt || !inputExcerpt.trim()) return;
            excerpt = inputExcerpt.trim();
          }

          const claim =
            target.claim !== undefined
              ? String(target.claim)
              : await vscode.window.showInputBox({
                  prompt: "Editable distilled claim for Verified Atom",
                  value: excerpt,
                });
          if (claim === undefined || !claim.trim()) return;

          let memoryType = target.memory_type;
          if (!memoryType) {
            const pickedType = await vscode.window.showQuickPick(
              VALID_MEMORY_TYPES.map((mt) => ({ label: mt, value: mt })),
              { placeHolder: "Select memory_type for Verified Atom" }
            );
            if (!pickedType) return;
            memoryType = pickedType.value || pickedType.label;
          }

          const rawTags =
            target.tags !== undefined
              ? target.tags
              : await vscode.window.showInputBox({
                  prompt: "Optional comma-separated context/project tags",
                  value: Array.isArray(noteDoc.tags) ? noteDoc.tags.join(", ") : "",
                });
          if (rawTags === undefined) return;

          const actor =
            target.actor !== undefined
              ? String(target.actor)
              : await vscode.window.showInputBox({
                  prompt: "Approving owner actor identifier",
                  value: "vscode_owner",
                });
          if (actor === undefined || !actor.trim()) return;

          const verifyPayload = {
            excerpt,
            claim: claim.trim(),
            memory_type: memoryType,
            tags: parseListInput(rawTags, /[,|\n]+/),
            actor: actor.trim(),
            expected_note_sha256: expectedNoteSha,
            expected_sha256: expectedNoteSha,
          };
          const verifyRes = await apiRequest(
            "POST",
            `/v1/notes/${encodeURIComponent(filename)}/verify`,
            verifyPayload,
            { useOwnerToken: true }
          );
          if (!verifyRes.ok) {
            vscode.window.showErrorMessage(
              `Verify excerpt failed (${verifyRes.status}): ${
                verifyRes.data?.error || "error"
              }`
            );
            return;
          }
          vscode.window.showInformationMessage(
            `Verified note excerpt into atom: ${
              verifyRes.data?.atom_filename || verifyRes.data?.filename || "created"
            }`
          );
          treeProvider.refresh();
        } catch (err) {
          vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
        }
      }
    ),

    vscode.commands.registerCommand(
      "secondBrain.promoteInboxItem",
      async (arg) => {
        try {
          const target = extractTargetData(arg);
          let filename = target.filename;
          if (!filename) {
            const qRes = await apiRequest("GET", "/v1/inbox/queue?limit=100&offset=0", null, {
              useOwnerToken: true,
            });
            if (!qRes.ok) {
              vscode.window.showErrorMessage(
                `Could not read Quarantined Inbox: ${
                  qRes.data?.error || qRes.status
                }`
              );
              return;
            }
            const items = qRes.data?.items || [];
            if (!items.length) {
              vscode.window.showInformationMessage("Quarantined Inbox is empty.");
              return;
            }
            const picked = await vscode.window.showQuickPick(
              items.map((it) => ({
                label: it.filename,
                description: (it.risk_flags || []).length
                  ? `Risks: ${it.risk_flags.join(", ")}`
                  : "Clean",
                detail: it.preview || "",
                filename: it.filename,
              })),
              { placeHolder: "Select a quarantined inbox item to promote" }
            );
            if (!picked) return;
            filename = picked.filename;
          }
          if (!isSafeNoteFilename(filename)) {
            vscode.window.showErrorMessage(`Invalid inbox filename: ${filename}`);
            return;
          }

          const inspectRes = await apiRequest(
            "GET",
            `/v1/inbox/${encodeURIComponent(filename)}`,
            null,
            { useOwnerToken: true }
          );
          if (!inspectRes.ok) {
            vscode.window.showErrorMessage(
              `Inspect inbox item failed (${inspectRes.status}): ${
                inspectRes.data?.error || "error"
              }`
            );
            return;
          }
          const doc = inspectRes.data || {};
          const expectedSha = String(doc.sha256 || target.sha256 || "");
          const riskFlags = Array.isArray(doc.risk_flags) ? doc.risk_flags : [];
          let acknowledgeRisks = false;

          if (riskFlags.length > 0) {
            const decision = await vscode.window.showWarningMessage(
              `Quarantined item '${filename}' flagged risks (${riskFlags.join(
                ", "
              )}). Acknowledge risks and promote?`,
              { modal: true },
              "Acknowledge & Promote"
            );
            if (decision !== "Acknowledge & Promote") return;
            acknowledgeRisks = decision === "Acknowledge & Promote";
          }

          const promRes = await apiRequest(
            "POST",
            "/v1/inbox/promote",
            {
              filename,
              expected_sha256: expectedSha,
              actor: String(target.actor || "vscode_owner"),
              acknowledge_risks: acknowledgeRisks,
            },
            { useOwnerToken: true }
          );
          if (!promRes.ok) {
            vscode.window.showErrorMessage(
              `Promote inbox item failed (${promRes.status}): ${
                promRes.data?.error || "error"
              }`
            );
            return;
          }
          vscode.window.showInformationMessage(
            `Promoted inbox item '${filename}' (${
              promRes.data?.atom_filename || promRes.data?.raw_filename || "promoted"
            }).`
          );
          treeProvider.refresh();
        } catch (err) {
          vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
        }
      }
    ),

    vscode.commands.registerCommand(
      "secondBrain.rejectInboxItem",
      async (arg) => {
        try {
          const target = extractTargetData(arg);
          let filename = target.filename;
          if (!filename) {
            const qRes = await apiRequest("GET", "/v1/inbox/queue?limit=100&offset=0", null, {
              useOwnerToken: true,
            });
            if (!qRes.ok) {
              vscode.window.showErrorMessage(
                `Could not read Quarantined Inbox: ${
                  qRes.data?.error || qRes.status
                }`
              );
              return;
            }
            const items = qRes.data?.items || [];
            if (!items.length) {
              vscode.window.showInformationMessage("Quarantined Inbox is empty.");
              return;
            }
            const picked = await vscode.window.showQuickPick(
              items.map((it) => ({
                label: it.filename,
                detail: it.preview || "",
                filename: it.filename,
              })),
              { placeHolder: "Select a quarantined inbox item to reject" }
            );
            if (!picked) return;
            filename = picked.filename;
          }
          if (!isSafeNoteFilename(filename)) {
            vscode.window.showErrorMessage(`Invalid inbox filename: ${filename}`);
            return;
          }

          let expectedSha = String(target.sha256 || "");
          if (!expectedSha) {
            const inspectRes = await apiRequest(
              "GET",
              `/v1/inbox/${encodeURIComponent(filename)}`,
              null,
              { useOwnerToken: true }
            );
            if (!inspectRes.ok) {
              vscode.window.showErrorMessage(
                `Inspect inbox item failed: ${
                  inspectRes.data?.error || inspectRes.status
                }`
              );
              return;
            }
            expectedSha = String(inspectRes.data?.sha256 || "");
          }

          const confirmed = await vscode.window.showWarningMessage(
            `Reject quarantined inbox item '${filename}'?`,
            { modal: true },
            "Reject"
          );
          if (confirmed !== "Reject") return;

          const rejRes = await apiRequest(
            "POST",
            "/v1/inbox/reject",
            {
              filename,
              expected_sha256: expectedSha,
              actor: String(target.actor || "vscode_owner"),
            },
            { useOwnerToken: true }
          );
          if (!rejRes.ok) {
            vscode.window.showErrorMessage(
              `Reject inbox item failed (${rejRes.status}): ${
                rejRes.data?.error || "error"
              }`
            );
            return;
          }
          vscode.window.showInformationMessage(`Rejected inbox item: ${filename}`);
          treeProvider.refresh();
        } catch (err) {
          vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
        }
      }
    ),

    vscode.commands.registerCommand("secondBrain.approveAtom", (arg) =>
      runAtomTransition(
        "approve",
        arg,
        "/v1/review/queue?limit=200&offset=0",
        null,
        "Approved atom"
      )
    ),

    vscode.commands.registerCommand("secondBrain.rejectAtom", (arg) =>
      runAtomTransition(
        "reject",
        arg,
        "/v1/review/queue?limit=200&offset=0",
        null,
        "Rejected atom"
      )
    ),

    vscode.commands.registerCommand("secondBrain.demoteAtom", (arg) =>
      runAtomTransition(
        "demote",
        arg,
        "/v1/atoms?limit=200&offset=0",
        (a) => a.verified || a.review_status === "accepted",
        "Demoted atom"
      )
    ),

    vscode.commands.registerCommand("secondBrain.quarantineAtom", (arg) =>
      runAtomTransition(
        "quarantine",
        arg,
        "/v1/atoms?limit=200&offset=0",
        null,
        "Quarantined atom"
      )
    ),

    vscode.commands.registerCommand("secondBrain.mergeAtoms", async (arg) => {
      try {
        const target = extractTargetData(arg);
        const [atomsRes, revRes] = await Promise.all([
          apiRequest("GET", "/v1/atoms?limit=200&offset=0", null, {
            useOwnerToken: true,
          }),
          apiRequest("GET", "/v1/review/queue?limit=200&offset=0", null, {
            useOwnerToken: true,
          }),
        ]);
        const byFilename = new Map();
        for (const a of [
          ...(atomsRes.ok ? atomsRes.data?.items || atomsRes.data?.atoms || [] : []),
          ...(revRes.ok ? revRes.data?.items || [] : []),
        ]) {
          if (a && a.filename && !byFilename.has(a.filename)) {
            byFilename.set(a.filename, a);
          }
        }
        const allAtoms = [...byFilename.values()];
        if (allAtoms.length < 2 && !(target.survivor_filename && target.superseded_filename)) {
          vscode.window.showInformationMessage(
            "At least two atoms are required to merge duplicates."
          );
          return;
        }

        let survivorFilename = target.survivor_filename || target.filename || "";
        let survivorSha = target.survivor_expected_sha256 || target.sha256 || "";
        if (!survivorFilename) {
          const pickedSurvivor = await vscode.window.showQuickPick(
            allAtoms.map((a) => ({
              label: a.filename,
              description: a.review_status || "",
              detail: a.claim || "",
              filename: a.filename,
              sha256: a.sha256 || "",
            })),
            { placeHolder: "Select SURVIVOR atom (canonical claim to retain)" }
          );
          if (!pickedSurvivor) return;
          survivorFilename = pickedSurvivor.filename;
          survivorSha = pickedSurvivor.sha256;
        }

        let supersededFilename = target.superseded_filename || "";
        let supersededSha = target.superseded_expected_sha256 || "";
        if (!supersededFilename) {
          const remaining = allAtoms.filter((a) => a.filename !== survivorFilename);
          const pickedSuperseded = await vscode.window.showQuickPick(
            remaining.map((a) => ({
              label: a.filename,
              description: a.review_status || "",
              detail: a.claim || "",
              filename: a.filename,
              sha256: a.sha256 || "",
            })),
            { placeHolder: `Select SUPERSEDED atom to merge into '${survivorFilename}'` }
          );
          if (!pickedSuperseded) return;
          supersededFilename = pickedSuperseded.filename;
          supersededSha = pickedSuperseded.sha256;
        }

        const rationale =
          target.rationale !== undefined
            ? String(target.rationale)
            : await vscode.window.showInputBox({
                prompt: "Rationale for merging duplicate atoms",
                value: "Consolidated duplicate claim",
              });
        if (rationale === undefined || !rationale.trim()) return;

        const [resolvedSurvivorSha, resolvedSupersededSha] = await Promise.all([
          resolveAtomSha(survivorFilename, survivorSha),
          resolveAtomSha(supersededFilename, supersededSha),
        ]);

        const mergeRes = await apiRequest(
          "POST",
          "/v1/atoms/merge",
          {
            survivor_filename: survivorFilename,
            survivor_expected_sha256: resolvedSurvivorSha,
            superseded_filename: supersededFilename,
            superseded_expected_sha256: resolvedSupersededSha,
            actor: String(target.actor || "vscode_owner"),
            rationale: rationale.trim(),
          },
          { useOwnerToken: true }
        );
        if (!mergeRes.ok) {
          vscode.window.showErrorMessage(
            `Atom merge failed (${mergeRes.status}): ${
              mergeRes.data?.error || "error"
            }`
          );
          return;
        }
        vscode.window.showInformationMessage(
          `Merged '${supersededFilename}' into '${survivorFilename}'.`
        );
        treeProvider.refresh();
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.promotePlaybook", async (arg) => {
      try {
        const opts = arg && typeof arg === "object" && !arg.sbData ? arg : {};
        const [candRes, existingRes] = await Promise.all([
          apiRequest("GET", "/v1/playbooks/candidates", null, {
            useOwnerToken: true,
          }),
          apiRequest("GET", "/v1/playbooks?include_inactive=1", null, {
            useOwnerToken: true,
          }),
        ]);
        if (!candRes.ok) {
          vscode.window.showErrorMessage(
            `Could not inspect playbook candidates: ${
              candRes.data?.error || candRes.status
            }`
          );
          return;
        }
        const candidates = candRes.data?.items || candRes.data?.candidates || [];
        if (!candidates.length) {
          vscode.window.showInformationMessage(
            "No approved procedural atom candidates available for playbook promotion."
          );
          return;
        }

        let selectedCandidates = opts.candidates;
        if (!Array.isArray(selectedCandidates) || !selectedCandidates.length) {
          const picked = await vscode.window.showQuickPick(
            candidates.map((c) => ({
              label: c.atom_id || c.filename,
              description: c.ready_for_promotion ? "ready" : "early override",
              detail: c.claim || "",
              candidate: c,
            })),
            {
              canPickMany: true,
              placeHolder: "Select backing procedural atom(s) for Skill Playbook",
            }
          );
          if (!picked || !picked.length) return;
          selectedCandidates = picked.map(
            (p) => p.candidate || candidates.find((c) => (c.atom_id || c.filename) === p.label)
          ).filter(Boolean);
        }

        const groundingSteps = [];
        const seenSteps = new Set();
        for (const cand of selectedCandidates) {
          const units =
            Array.isArray(cand.grounding_units) && cand.grounding_units.length
              ? cand.grounding_units
              : computeClaimGroundingUnits(cand.claim || "");
          for (const unit of units) {
            const cleanUnit = String(unit || "").trim();
            if (cleanUnit && !seenSteps.has(cleanUnit)) {
              seenSteps.add(cleanUnit);
              groundingSteps.push(cleanUnit);
            }
          }
        }

        if (!groundingSteps.length) {
          vscode.window.showErrorMessage(
            "Selected candidate atom(s) contain no grounding-valid claim steps."
          );
          return;
        }

        const playbookId =
          opts.playbook_id !== undefined
            ? String(opts.playbook_id)
            : await vscode.window.showInputBox({
                prompt: "Playbook slug identifier",
                value: String(
                  selectedCandidates[0].atom_id ||
                    selectedCandidates[0].filename ||
                    "skill-playbook"
                ).replace(/\.md$/i, ""),
              });
        if (!playbookId || !playbookId.trim()) return;

        const title =
          opts.title !== undefined
            ? String(opts.title)
            : await vscode.window.showInputBox({
                prompt: "Human-readable Skill Playbook title",
                value: playbookId.trim(),
              });
        if (!title || !title.trim()) return;

        const trigger =
          opts.trigger !== undefined
            ? String(opts.trigger)
            : await vscode.window.showInputBox({
                prompt: "Operational trigger condition (when to invoke this playbook)",
                placeHolder: "e.g. Before deploying database schema changes",
              });
        if (!trigger || !trigger.trim()) return;

        let chosenSteps = opts.steps;
        if (!Array.isArray(chosenSteps) || !chosenSteps.length) {
          const stepPicks = await vscode.window.showQuickPick(
            groundingSteps.map((s) => ({
              label: s,
              picked: true,
              stepText: s,
            })),
            {
              canPickMany: true,
              placeHolder:
                "Confirm grounding-valid checklist steps (exact Claim / grounding_units)",
            }
          );
          if (!stepPicks || !stepPicks.length) return;
          chosenSteps = stepPicks.map((p) => p.stepText || p.label);
        }

        const existingPlaybooks = existingRes.ok
          ? existingRes.data?.items || existingRes.data?.playbooks || []
          : [];
        const normId = playbookId.trim().replace(/\.md$/i, "");
        const existingPb = existingPlaybooks.find(
          (pb) =>
            pb.playbook_id === normId ||
            String(pb.filename || "").replace(/\.md$/i, "") === normId
        );

        const needsEarlyOverride = selectedCandidates.some(
          (c) => c.ready_for_promotion === false
        );

        const promotePayload = {
          playbook_id: normId,
          title: title.trim(),
          trigger: trigger.trim(),
          steps: chosenSteps,
          backing_atoms: selectedCandidates.map((c) => [
            String(c.atom_id || c.filename),
            String(c.sha256 || ""),
          ]),
          preconditions: parseListInput(opts.preconditions || [], /[|\n]+/),
          expected_outputs: parseListInput(opts.expected_outputs || [], /[|\n]+/),
          failure_recovery: parseListInput(opts.failure_recovery || [], /[|\n]+/),
          actor: String(opts.actor || "vscode_owner"),
          allow_early_owner_override: needsEarlyOverride,
          allow_multi_project: Boolean(opts.allow_multi_project),
        };
        if (existingPb?.sha256) {
          promotePayload.expected_playbook_sha256 = String(existingPb.sha256);
        }

        const promRes = await apiRequest(
          "POST",
          "/v1/playbooks/promote",
          promotePayload,
          { useOwnerToken: true }
        );
        if (!promRes.ok) {
          vscode.window.showErrorMessage(
            `Promote playbook failed (${promRes.status}): ${
              promRes.data?.error || "error"
            }`
          );
          return;
        }
        vscode.window.showInformationMessage(
          `Promoted Skill Playbook: ${
            promRes.data?.filename || promRes.data?.playbook_id || normId
          }`
        );
        treeProvider.refresh();
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand(
      "secondBrain.deprecatePlaybook",
      async (arg) => {
        try {
          const target = extractTargetData(arg);
          let filename = target.filename;
          let expectedSha = target.sha256 || target.playbook?.sha256 || "";
          if (!filename) {
            const pbRes = await apiRequest("GET", "/v1/playbooks");
            if (!pbRes.ok) {
              vscode.window.showErrorMessage(
                `Could not list playbooks: ${pbRes.data?.error || pbRes.status}`
              );
              return;
            }
            const pbs = pbRes.data?.items || pbRes.data?.playbooks || [];
            if (!pbs.length) {
              vscode.window.showInformationMessage("No active playbooks to deprecate.");
              return;
            }
            const picked = await vscode.window.showQuickPick(
              pbs.map((pb) => ({
                label: pb.title || pb.filename,
                description: pb.filename,
                detail: pb.trigger || "",
                filename: pb.filename || `${pb.playbook_id}.md`,
                sha256: pb.sha256 || "",
              })),
              { placeHolder: "Select an active Skill Playbook to deprecate" }
            );
            if (!picked) return;
            filename = picked.filename;
            expectedSha = picked.sha256;
          }
          if (!isSafeNoteFilename(filename)) {
            vscode.window.showErrorMessage(`Invalid playbook filename: ${filename}`);
            return;
          }

          const rationale =
            target.rationale !== undefined
              ? String(target.rationale)
              : await vscode.window.showInputBox({
                  prompt: `Rationale for deprecating '${filename}'`,
                  value: "Retired by owner",
                });
          if (rationale === undefined || !rationale.trim()) return;

          const depRes = await apiRequest(
            "POST",
            `/v1/playbooks/${encodeURIComponent(filename)}/deprecate`,
            {
              expected_sha256: expectedSha,
              actor: String(target.actor || "vscode_owner"),
              rationale: rationale.trim(),
            },
            { useOwnerToken: true }
          );
          if (!depRes.ok) {
            vscode.window.showErrorMessage(
              `Deprecate playbook failed (${depRes.status}): ${
                depRes.data?.error || "error"
              }`
            );
            return;
          }
          vscode.window.showInformationMessage(`Deprecated playbook: ${filename}`);
          treeProvider.refresh();
        } catch (err) {
          vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
        }
      }
    ),

    vscode.commands.registerCommand("secondBrain.rememberSelection", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) return;
      const text = editor.document.getText(editor.selection).trim();
      if (!text) {
        vscode.window.showWarningMessage(
          "Select text to remember in Second Brain."
        );
        return;
      }
      const title = await vscode.window.showInputBox({
        prompt: "Optional title for this memory",
        value: path.basename(editor.document.fileName),
      });
      if (title === undefined) return;
      try {
        const res = await apiRequest("POST", "/v1/remember", {
          content: text,
          title: title || undefined,
          source: `vscode:${path.basename(editor.document.fileName)}`,
          tags: ["vscode"],
        });
        if (res.ok) {
          const statusLabel =
            res.data?.status === "quarantined"
              ? "quarantined in inbox for owner review"
              : res.data?.status || res.data?.tier || "notes";
          vscode.window.showInformationMessage(
            `Remembered in Second Brain (${statusLabel}).`
          );
          treeProvider.refresh();
        } else {
          vscode.window.showErrorMessage(
            `Second Brain remember failed: ${res.data?.error || res.status}`
          );
        }
      } catch (err) {
        vscode.window.showErrorMessage(
          `Second Brain API unreachable: ${err.message}`
        );
      }
    }),

    vscode.commands.registerCommand("secondBrain.quickRecall", async () => {
      const query = await vscode.window.showInputBox({
        prompt:
          "Search Second Brain notes & verified atoms (or press Enter with empty input to open Streaming Chat)",
        placeHolder: "Type search keywords or press Enter for Chat...",
      });
      if (query === undefined) return;
      if (!query.trim()) {
        await vscode.commands.executeCommand("secondBrain.openChat");
        return;
      }
      try {
        const [recallRes, notesRes] = await Promise.all([
          apiRequest("POST", "/v1/recall", {
            query,
            n_results: 8,
            retrieval_mode: "auto",
          }),
          apiRequest(
            "GET",
            `/v1/notes?q=${encodeURIComponent(query)}&limit=10`
          ),
        ]);
        const atoms = recallRes.ok ? recallRes.data?.results || [] : [];
        const notes = notesRes.ok
          ? notesRes.data?.items || notesRes.data?.notes || []
          : [];
        const choices = [
          ...atoms.map((a) => ({
            label: `[Verified Atom] ${a.filename}`,
            description: a.filename,
            detail: a.claim || "",
            kind: "atom",
            atom: a,
          })),
          ...notes.map((n) => ({
            label: `[Note] ${n.title || n.filename}`,
            description: n.filename,
            detail: n.snippet || "",
            kind: "note",
          })),
        ];
        if (!choices.length) {
          vscode.window.showInformationMessage(
            "No matching verified atoms or working notes found."
          );
          return;
        }
        const picked = await vscode.window.showQuickPick(choices, {
          placeHolder: "Select a verified atom or note to open",
        });
        if (picked) {
          await vscode.commands.executeCommand("secondBrain.openTreeItem", {
            kind: picked.kind,
            filename: picked.description,
            atom: picked.atom,
          });
        }
      } catch (err) {
        vscode.window.showErrorMessage(
          `Second Brain API unreachable: ${err.message}`
        );
      }
    }),

    vscode.commands.registerCommand("secondBrain.openChat", async () => {
      if (chatPanel) {
        chatPanel.reveal(vscode.ViewColumn.Beside);
        return chatPanel;
      }

      try {
        const healthCheck = await apiRequest("GET", "/v1/health");
        if (healthCheck.status !== 200) {
          vscode.window.showErrorMessage(
            `Second Brain API unavailable (${healthCheck.status}): ${
              healthCheck.data?.error ||
              "Check API authentication or start Second Brain."
            }`
          );
          return null;
        }
      } catch (err) {
        vscode.window.showErrorMessage(
          `Second Brain API unreachable on ${LOOPBACK_HOST}:${getPort()}: ${err.message}`
        );
        return null;
      }

      if (chatPanel) {
        chatPanel.reveal(vscode.ViewColumn.Beside);
        return chatPanel;
      }

      const nonce = crypto.randomBytes(16).toString("base64");
      const panel = vscode.window.createWebviewPanel(
        "secondBrainChat",
        "Second Brain Chat",
        vscode.ViewColumn.Beside,
        { enableScripts: true, retainContextWhenHidden: true }
      );
      chatPanel = panel;
      panel.webview.html = buildChatWebviewHtml(nonce);

      if (typeof panel.onDidDispose === "function") {
        panel.onDidDispose(() => {
          abortActiveChatStream();
          if (chatPanel === panel) {
            chatPanel = null;
          }
        });
      }

      panel.webview.onDidReceiveMessage(async (msg) => {
        if (!msg || typeof msg !== "object" || typeof msg.action !== "string") {
          return;
        }

        if (msg.action === "stop" || msg.action === "cancel") {
          abortActiveChatStream();
          panel.webview.postMessage({
            type: "cancelled",
            text: "Stream stopped.",
          });
          return;
        }

        if (msg.action === "openExternal") {
          const safeUrl = parseSafeExternalUrl(msg.url);
          if (safeUrl && vscode.env && typeof vscode.env.openExternal === "function") {
            await vscode.env.openExternal(vscode.Uri.parse(safeUrl));
          }
          return;
        }

        if (msg.action === "openSource") {
          const citeId =
            typeof msg.citation_id === "string" ? msg.citation_id.trim() : "";
          const tracked = citeId ? lastChatSourcesByCitation[citeId] : null;
          const rawFilename = String(
            tracked?.filename || msg.filename || ""
          ).trim();
          const rawTier = String(tracked?.tier || msg.tier || "").trim();
          if (!isSafeNoteFilename(rawFilename)) return;

          const prefix = citeId.charAt(0).toUpperCase();
          if (rawTier === "note" || prefix === "N") {
            await vscode.commands.executeCommand("secondBrain.openTreeItem", {
              kind: "note",
              filename: rawFilename,
            });
          } else if (rawTier === "unverified_lead" || prefix === "L") {
            const inboxProbe = await apiRequest(
              "GET",
              `/v1/inbox/${encodeURIComponent(rawFilename)}`,
              null,
              { useOwnerToken: true }
            );
            if (inboxProbe.ok) {
              await openMarkdownPreview(
                formatInboxPreviewMarkdown(rawFilename, inboxProbe.data)
              );
            } else {
              await vscode.commands.executeCommand("secondBrain.openTreeItem", {
                kind: "note",
                filename: rawFilename,
              });
            }
          } else {
            await vscode.commands.executeCommand("secondBrain.openTreeItem", {
              kind: "atom",
              filename: rawFilename,
              atom: tracked || undefined,
            });
          }
          return;
        }

        if (msg.action === "warmup") {
          try {
            const wRes = await apiRequest("POST", "/v1/chat/warmup", {});
            if (wRes.ok) {
              panel.webview.postMessage({
                type: "status",
                text: `Model warmed: ${wRes.data?.model || "ollama"} (${
                  wRes.data?.warmup_ms ?? "?"
                } ms)`,
              });
            } else {
              panel.webview.postMessage({
                type: "error",
                text: `Warmup failed (${wRes.status}): ${
                  wRes.data?.error || "unavailable"
                }`,
              });
            }
          } catch (err) {
            panel.webview.postMessage({
              type: "error",
              text: `Warmup error: ${err.message}`,
            });
          }
          return;
        }

        if (msg.action !== "ask") return;

        const query =
          typeof msg.query === "string" ? msg.query.trim().slice(0, 2000) : "";
        if (!query) return;

        abortActiveChatStream();
        lastChatSourcesByCitation = Object.create(null);

        const mode = VALID_CHAT_MODES.has(msg.mode) ? msg.mode : "stream";
        const retrievalMode = VALID_RETRIEVAL_MODES.has(msg.retrieval_mode)
          ? msg.retrieval_mode
          : "auto";
        const answerView = VALID_ANSWER_VIEWS.has(msg.answer_view)
          ? msg.answer_view
          : "standard";
        const rawHostId =
          typeof msg.host_id === "string" ? msg.host_id.trim() : "";
        const hostId = /^[A-Za-z0-9_.-]{1,64}$/.test(rawHostId)
          ? rawHostId
          : "ollama_local";
        const projectScope =
          typeof msg.project_scope === "string" && msg.project_scope.trim()
            ? msg.project_scope.trim().slice(0, 120)
            : undefined;
        const modelOverride =
          typeof msg.model_override === "string" && msg.model_override.trim()
            ? msg.model_override.trim().slice(0, 128)
            : undefined;
        const includePreferences =
          msg.include_preferences === undefined
            ? true
            : Boolean(msg.include_preferences);

        const port = getPort();
        const token = readBearerToken();
        const ownerToken = readOwnerToken();
        const reqPayloadObj = {
          query,
          mode,
          retrieval_mode: retrievalMode,
          answer_view: answerView,
          host_id: hostId,
          include_preferences: includePreferences,
        };
        if (projectScope) reqPayloadObj.project_scope = projectScope;
        if (modelOverride) reqPayloadObj.model_override = modelOverride;

        const body = JSON.stringify(reqPayloadObj);
        const streamHeaders = {
          Host: `${LOOPBACK_HOST}:${port}`,
          Accept: "text/event-stream",
          Connection: "close",
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body, "utf8"),
        };
        if (token) streamHeaders["Authorization"] = `Bearer ${token}`;
        if (ownerToken) streamHeaders["X-Owner-Token"] = ownerToken;

        const streamHandle = { req: null, res: null, aborted: false };
        activeChatStream = streamHandle;

        const req = http.request(
          {
            hostname: LOOPBACK_HOST,
            port,
            path: "/v1/chat/stream",
            method: "POST",
            headers: streamHeaders,
            agent: false,
          },
          (res) => {
            streamHandle.res = res;
            if (streamHandle.aborted) {
              res.destroy();
              return;
            }
            res.setEncoding("utf8");
            if (res.statusCode !== 200) {
              let errRaw = "";
              res.on("data", (c) => (errRaw += c));
              res.on("end", () => {
                if (streamHandle.aborted) return;
                if (activeChatStream === streamHandle) activeChatStream = null;
                let errMsg = `HTTP ${res.statusCode}`;
                try {
                  const parsed = errRaw ? JSON.parse(errRaw) : {};
                  if (parsed && parsed.error) errMsg = parsed.error;
                } catch {}
                const fullErr = `Second Brain Chat failed (${res.statusCode}): ${errMsg}`;
                vscode.window.showErrorMessage(fullErr);
                panel.webview.postMessage({
                  type: "error",
                  text: fullErr,
                });
              });
              return;
            }

            panel.webview.postMessage({ type: "reset", text: "" });
            let buf = "";
            let ev = "message";

            const handleSseLine = (line) => {
              if (streamHandle.aborted) return;
              if (line.startsWith("event:")) {
                ev = line.slice(6).trim();
              } else if (line.startsWith("data:")) {
                try {
                  const data = JSON.parse(line.slice(5).trim());
                  const eventType =
                    ev && ev !== "message"
                      ? ev
                      : String(data.type || "message");

                  if (eventType === "context") {
                    const sources = Array.isArray(data.sources)
                      ? data.sources
                      : [
                          ...(data.verified || []),
                          ...(data.preferences || []),
                          ...(data.notes || []),
                          ...(data.leads || []),
                        ];
                    for (const s of sources) {
                      if (s && s.citation_id) {
                        lastChatSourcesByCitation[s.citation_id] = s;
                      }
                    }
                    panel.webview.postMessage({ type: "sources", sources });
                    panel.webview.postMessage({
                      type: "status",
                      text: `Retrieved ${sources.length} source(s) in ${
                        data.retrieval_ms ?? "?"
                      } ms (${data.retrieval_mode || retrievalMode} · ${
                        data.answer_view || answerView
                      })${data.warning ? " · " + data.warning : ""}`,
                    });
                    if (mode === "instant") {
                      const summary = sources
                        .map(
                          (s) =>
                            `- [${s.citation_id || s.tier || "V1"}] **${
                              s.title || s.filename || "Source"
                            }**: ${s.claim || s.excerpt || ""}`
                        )
                        .join("\n");
                      panel.webview.postMessage({
                        type: "markdown",
                        text: summary || "_No matching sources found._",
                      });
                    }
                  } else if (eventType === "research_leads") {
                    const leads = Array.isArray(data.leads) ? data.leads : [];
                    for (const lead of leads) {
                      if (lead && lead.citation_id) {
                        lastChatSourcesByCitation[lead.citation_id] = lead;
                      }
                    }
                    panel.webview.postMessage({
                      type: "research_leads",
                      label:
                        data.label ||
                        "UNVERIFIED LEADS — NOT OWNER-APPROVED",
                      text: data.text || "",
                      leads,
                    });
                  } else if (eventType === "token") {
                    panel.webview.postMessage({
                      type: "append",
                      text: data.text ?? data.token ?? "",
                    });
                  } else if (eventType === "verified_answer") {
                    if (!data.answerable) {
                      panel.webview.postMessage({
                        type: "markdown",
                        text:
                          data.reason ||
                          data.text ||
                          "Abstained: no verified claims answer this question.",
                      });
                    } else {
                      const formattedClaims = (data.claims || [])
                        .map((c, idx) => {
                          const cites = (c.evidence || [])
                            .map(
                              (e) =>
                                `  - \`${e.atom_id}\`: "${
                                  e.quote || e.evidence_quote || ""
                                }"`
                            )
                            .join("\n");
                          return `${idx + 1}. ${c.text}${cites ? "\n" + cites : ""}`;
                        })
                        .join("\n\n");
                      panel.webview.postMessage({
                        type: "markdown",
                        text: formattedClaims || data.text || "",
                      });
                    }
                  } else if (eventType === "grounding") {
                    panel.webview.postMessage({
                      type: "grounding",
                      grounding: data,
                    });
                  } else if (eventType === "abstain") {
                    panel.webview.postMessage({
                      type: "markdown",
                      text: `**Abstained:** ${data.reason || data.text || ""}`,
                    });
                  } else if (eventType === "error") {
                    panel.webview.postMessage({
                      type: "error",
                      text: `Error: ${data.error || "Stream aborted"}`,
                    });
                  } else if (eventType === "done") {
                    if (activeChatStream === streamHandle) {
                      activeChatStream = null;
                    }
                    const ms = data.total_ms ?? data.elapsed_ms;
                    if (ms !== undefined) {
                      panel.webview.postMessage({
                        type: "status",
                        text: `Completed in ${ms} ms`,
                      });
                    }
                  }
                } catch {}
                ev = "message";
              }
            };

            res.on("data", (chunk) => {
              if (streamHandle.aborted) return;
              buf += chunk;
              const lines = buf.split(/\r?\n/);
              buf = lines.pop() || "";
              for (const line of lines) {
                handleSseLine(line);
              }
            });

            res.on("end", () => {
              if (!streamHandle.aborted && buf.trim()) {
                handleSseLine(buf.trim());
              }
              if (activeChatStream === streamHandle) {
                activeChatStream = null;
              }
            });
          }
        );

        streamHandle.req = req;
        req.setTimeout(90000, () => {
          if (!streamHandle.aborted) {
            req.destroy(new Error("Stream timed out after 90 seconds."));
          }
        });
        req.on("error", (err) => {
          if (streamHandle.aborted) return;
          if (activeChatStream === streamHandle) {
            activeChatStream = null;
          }
          panel.webview.postMessage({
            type: "error",
            text: `Error: ${err.message}`,
          });
        });
        req.write(body);
        req.end();
      });

      return panel;
    }),

    vscode.commands.registerCommand("secondBrain.assemblePlan", async (arg) => {
      const opts = arg && typeof arg === "object" && !arg.sbData ? arg : {};
      const goal =
        opts.goal !== undefined
          ? String(opts.goal)
          : await vscode.window.showInputBox({
              prompt: "Enter task goal for grounded Executive Plan",
            });
      if (!goal || !goal.trim()) return;

      let mode = opts.mode;
      if (!mode) {
        const modePick = await vscode.window.showQuickPick(
          [
            {
              label: "deterministic",
              description: "Zero-LLM exact citation assembly",
              value: "deterministic",
            },
            {
              label: "model_assisted",
              description: "LLM synthesis constrained to verified citations",
              value: "model_assisted",
            },
          ],
          { placeHolder: "Select Executive Planner synthesis mode" }
        );
        if (!modePick) return;
        mode = modePick.value || modePick.label;
      }

      let retrievalMode = opts.retrieval_mode;
      if (!retrievalMode) {
        const retrPick = await vscode.window.showQuickPick(
          ["auto", "lexical", "dense", "rrf"].map((m) => ({
            label: m,
            value: m,
          })),
          { placeHolder: "Select retrieval mode" }
        );
        if (!retrPick) return;
        retrievalMode = retrPick.value || retrPick.label;
      }

      let includePreferences = opts.include_preferences;
      if (includePreferences === undefined) {
        const prefPick = await vscode.window.showQuickPick(
          [
            { label: "Include owner preferences", value: true },
            { label: "Exclude owner preferences", value: false },
          ],
          { placeHolder: "Include owner preference guardrails?" }
        );
        if (!prefPick) return;
        includePreferences = Boolean(prefPick.value);
      }

      const scope =
        opts.project_scope !== undefined
          ? String(opts.project_scope)
          : await vscode.window.showInputBox({
              prompt:
                "Optional project scope (e.g. project:alpha) — leave blank for default",
              placeHolder: "project:alpha",
            });
      if (scope === undefined) return;

      try {
        const res = await apiRequest(
          "POST",
          "/v1/plan",
          {
            goal: goal.trim(),
            mode,
            retrieval_mode: retrievalMode,
            include_preferences: Boolean(includePreferences),
            project_scope: scope.trim() || undefined,
          },
          { useOwnerToken: true }
        );
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Executive plan failed: ${res.data?.error || res.status}`
          );
          return;
        }
        await openMarkdownPreview(formatPlanMarkdown(res.data, goal.trim()));
      } catch (err) {
        vscode.window.showErrorMessage(
          `Second Brain plan failed: ${err.message}`
        );
      }
    }),

    vscode.commands.registerCommand("secondBrain.verifyLedger", async () => {
      try {
        const res = await apiRequest("GET", "/v1/ledger/verify");
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Ledger verify failed: ${res.data?.error || res.status}`
          );
          return;
        }
        const d = res.data || {};
        const valid = d.chain_valid ? "VALID" : "INVALID";
        const approved = d.approved_atoms ?? d.live_leaf_count ?? 0;
        const playbooks = d.accepted_playbooks ?? 0;
        const syncState = d.synced_with_head
          ? "synced w/ head"
          : "uncommitted live changes";
        vscode.window.showInformationMessage(
          `Vault Merkle Ledger: ${valid} · ${approved} approved atoms · ${playbooks} playbooks · ${syncState} · root ${String(
            d.live_merkle_root || ""
          ).slice(0, 16)}…`
        );
      } catch (err) {
        vscode.window.showErrorMessage(
          `Second Brain API unreachable: ${err.message}`
        );
      }
    }),

    vscode.commands.registerCommand("secondBrain.commitLedger", async (arg) => {
      const opts = arg && typeof arg === "object" && !arg.sbData ? arg : {};
      const note =
        opts.note !== undefined
          ? String(opts.note)
          : await vscode.window.showInputBox({
              prompt: "Optional ledger checkpoint note",
              placeHolder: "e.g. Approved security & deployment atoms",
            });
      if (note === undefined) return;
      try {
        const res = await apiRequest(
          "POST",
          "/v1/ledger/commit",
          {
            actor: String(opts.actor || "vscode_owner"),
            note: note.trim(),
          },
          { useOwnerToken: true }
        );
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Ledger commit failed (${res.status}): ${res.data?.error || "error"}`
          );
          return;
        }
        const d = res.data || {};
        vscode.window.showInformationMessage(
          `Committed Merkle Ledger checkpoint #${d.seq ?? "?"} (root ${String(
            d.merkle_root || ""
          ).slice(0, 16)}…, ${d.leaf_count ?? 0} leaves).`
        );
        treeProvider.refresh();
      } catch (err) {
        vscode.window.showErrorMessage(
          `Second Brain API unreachable: ${err.message}`
        );
      }
    }),

    vscode.commands.registerCommand("secondBrain.proveAtom", async (arg) => {
      try {
        const target = extractTargetData(arg);
        let chosenRef = "";
        let itemMeta = null;

        if (target.kind === "playbook" && (target.filename || target.playbook?.filename)) {
          const pbFn = target.filename || target.playbook.filename;
          chosenRef = `5-playbooks/${pbFn}`;
          itemMeta = target.playbook || { title: pbFn, status: "accepted" };
        } else if (target.filename) {
          chosenRef = target.filename.startsWith("5-playbooks/")
            ? target.filename
            : target.filename;
          itemMeta = target.atom || null;
        }

        if (!chosenRef) {
          const [atomsRes, pbRes] = await Promise.all([
            apiRequest("GET", "/v1/atoms?limit=200&offset=0"),
            apiRequest("GET", "/v1/playbooks?limit=200&offset=0"),
          ]);
          const verifiedAtoms = (
            atomsRes.ok ? atomsRes.data?.items || atomsRes.data?.atoms || [] : []
          ).filter((a) => a.verified || a.review_status === "accepted");
          const acceptedPlaybooks = (
            pbRes.ok ? pbRes.data?.items || pbRes.data?.playbooks || [] : []
          ).filter((pb) => !pb.status || pb.status === "accepted");

          const choices = [
            ...verifiedAtoms.map((a) => ({
              label: a.filename,
              description: `atom · ${String(a.sha256 || "").slice(0, 12)}`,
              detail: a.claim || "",
              ref: a.filename,
              meta: a,
            })),
            ...acceptedPlaybooks.map((pb) => {
              const fn = pb.filename || `${pb.playbook_id}.md`;
              return {
                label: `5-playbooks/${fn}`,
                description: `playbook · ${String(pb.sha256 || "").slice(0, 12)}`,
                detail: pb.title || pb.trigger || "",
                ref: `5-playbooks/${fn}`,
                meta: pb,
              };
            }),
          ];

          if (!choices.length) {
            vscode.window.showInformationMessage(
              "No approved atoms or accepted playbooks in vault yet to generate a Merkle proof."
            );
            return;
          }
          const picked = await vscode.window.showQuickPick(choices, {
            placeHolder:
              "Select an approved atom or accepted playbook (5-playbooks/<pb>.md) to prove",
          });
          if (!picked) return;
          chosenRef = picked.ref || picked.label;
          itemMeta = picked.meta || null;
        }

        const proofRes = await apiRequest(
          "GET",
          `/v1/ledger/prove/${encodeURIComponent(chosenRef)}`
        );
        if (!proofRes.ok) {
          vscode.window.showErrorMessage(
            `Merkle proof failed: ${proofRes.data?.error || proofRes.status}`
          );
          return;
        }
        await openMarkdownPreview(
          formatAtomProofMarkdown(chosenRef, itemMeta, proofRes.data)
        );
      } catch (err) {
        vscode.window.showErrorMessage(
          `Second Brain API unreachable: ${err.message}`
        );
      }
    }),

    vscode.commands.registerCommand("secondBrain.scanContradictions", async (arg) => {
      try {
        const enableNli = Boolean(arg && arg.enable_nli);
        const querySuffix = enableNli ? "?nli=1" : "";
        const res = await apiRequest("GET", `/v1/graph/contradictions${querySuffix}`);
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Contradiction scan failed: ${res.data?.error || res.status}`
          );
          return;
        }
        const count = res.data?.contradiction_count || 0;
        const driftCount = (res.data?.drift_alerts || []).length;
        if (count === 0 && driftCount === 0) {
          const action = await vscode.window.showInformationMessage(
            `Contradiction & Drift Radar: 0 conflicts, 0 drift alerts (${
              (res.data?.orphan_approved_atoms || []).length
            } orphan approved atoms).`,
            "Open Full Report"
          );
          if (action === "Open Full Report") {
            await openMarkdownPreview(formatRadarMarkdown(res.data));
          }
        } else {
          await openMarkdownPreview(formatRadarMarkdown(res.data));
        }
      } catch (err) {
        vscode.window.showErrorMessage(
          `Second Brain API unreachable: ${err.message}`
        );
      }
    }),

    vscode.commands.registerCommand("secondBrain.scanContradictionsNli", async () => {
      return vscode.commands.executeCommand("secondBrain.scanContradictions", {
        enable_nli: true,
      });
    }),

    vscode.commands.registerCommand("secondBrain.showTasks", async (arg) => {
      try {
        const opts = arg && typeof arg === "object" ? arg : {};
        const params = [];
        if (opts.project) params.push(`project=${encodeURIComponent(String(opts.project).trim())}`);
        if (opts.include_completed) params.push("include_completed=1");
        if (opts.include_raw !== false) params.push("include_raw=1");
        const qs = params.length ? `?${params.join("&")}` : "";
        const res = await apiRequest("GET", `/v1/notes/tasks${qs}`, null, {
          useOwnerToken: true,
        });
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Action items scan failed: ${res.data?.error || res.status}`
          );
          return;
        }
        await openMarkdownPreview(formatTasksMarkdown(res.data));
      } catch (err) {
        vscode.window.showErrorMessage(
          `Second Brain API unreachable: ${err.message}`
        );
      }
    }),

    vscode.commands.registerCommand("secondBrain.syncVault", async (arg) => {
      try {
        const opts = arg && typeof arg === "object" ? arg : {};
        let action = opts.action ? String(opts.action).toLowerCase() : "";
        if (!action) {
          const pick = await vscode.window.showQuickPick(
            [
              { label: "Status", action: "status", description: "Inspect E2EE sync epochs & open conflicts" },
              { label: "Push Encrypted", action: "push", description: "Encrypt durable vault files & push signed head" },
              { label: "Pull & Merge", action: "pull", description: "Verify signatures, 3-way merge, rebuild indexes" },
              { label: "Verify Remote", action: "verify", description: "Full cryptographic audit of remote encrypted blobs" },
            ],
            { placeHolder: "Zero-Knowledge E2EE Multi-Device Vault Sync" }
          );
          if (!pick) return;
          action = pick.action;
        }
        let res;
        if (action === "status") {
          res = await apiRequest("GET", "/v1/sync/status", null, { useOwnerToken: true });
        } else if (action === "push" || action === "pull" || action === "verify") {
          const payload = opts.passphrase ? { passphrase: String(opts.passphrase) } : {};
          if (action === "pull") payload.trust_new_peers = opts.trust_new_peers !== false;
          res = await apiRequest("POST", `/v1/sync/${action}`, payload, {
            useOwnerToken: true,
          });
        } else {
          vscode.window.showErrorMessage(`Unsupported sync action: ${action}`);
          return;
        }
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `E2EE Vault Sync (${action}) failed: ${res.data?.error || res.status}`
          );
          return;
        }
        if (action === "pull") treeProvider.refresh();
        await openMarkdownPreview(formatSyncStatusMarkdown(action, res.data));
      } catch (err) {
        vscode.window.showErrorMessage(
          `Second Brain API unreachable: ${err.message}`
        );
      }
    }),

    vscode.commands.registerCommand("secondBrain.createSnapshot", async () => {
      const label = await vscode.window.showInputBox({
        prompt: "Optional label for immutable Vault Snapshot",
        placeHolder: "e.g. pre-refactor-checkpoint",
      });
      if (label === undefined) return;
      try {
        const res = await apiRequest(
          "POST",
          "/v1/snapshots",
          { actor: "vscode_owner", label: label.trim() },
          { useOwnerToken: true }
        );
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Create snapshot failed: ${res.data?.error || res.status}`
          );
          return;
        }
        vscode.window.showInformationMessage(
          `Created Vault Snapshot: ${res.data?.snapshot_id} (${
            res.data?.atom_count ?? 0
          } atoms)`
        );
        treeProvider.refresh();
      } catch (err) {
        vscode.window.showErrorMessage(
          `Second Brain API unreachable: ${err.message}`
        );
      }
    }),

    vscode.commands.registerCommand("secondBrain.diffSnapshot", async (arg) => {
      try {
        const target = extractTargetData(arg);
        let snapshotId = target.snapshot_id || "";
        if (!snapshotId) {
          const snapRes = await apiRequest("GET", "/v1/snapshots");
          if (!snapRes.ok) {
            vscode.window.showErrorMessage(
              `Could not list snapshots: ${snapRes.data?.error || snapRes.status}`
            );
            return;
          }
          const snaps = snapRes.data?.items || snapRes.data?.snapshots || [];
          if (!snaps.length) {
            vscode.window.showInformationMessage(
              "No snapshots recorded yet. Run 'Second Brain: Create Vault Snapshot' first."
            );
            return;
          }
          const picked = await vscode.window.showQuickPick(
            snaps.map((s) => ({
              label: s.snapshot_id,
              description: `${s.atom_count ?? 0} atoms · ${s.created_at || ""}`,
              detail: s.label || `Merkle: ${s.merkle_root || ""}`,
            })),
            { placeHolder: "Select a snapshot to diff against the live vault" }
          );
          if (!picked) return;
          snapshotId = picked.label;
        }

        const diffRes = await apiRequest("POST", "/v1/snapshots/diff", {
          from_snapshot: snapshotId,
          to_snapshot: "live",
        });
        if (!diffRes.ok) {
          vscode.window.showErrorMessage(
            `Snapshot diff failed: ${diffRes.data?.error || diffRes.status}`
          );
          return;
        }
        await openMarkdownPreview(formatSnapshotDiffMarkdown(diffRes.data));
      } catch (err) {
        vscode.window.showErrorMessage(
          `Second Brain API unreachable: ${err.message}`
        );
      }
    }),

    vscode.commands.registerCommand("secondBrain.recallSnapshot", async (arg) => {
      try {
        const target = extractTargetData(arg);
        let snapshotId = target.snapshot_id || "";
        if (!snapshotId) {
          const snapRes = await apiRequest("GET", "/v1/snapshots");
          if (!snapRes.ok) {
            vscode.window.showErrorMessage(
              `Could not list snapshots: ${snapRes.data?.error || snapRes.status}`
            );
            return;
          }
          const snaps = snapRes.data?.items || snapRes.data?.snapshots || [];
          if (!snaps.length) {
            vscode.window.showInformationMessage("No snapshots recorded yet.");
            return;
          }
          const picked = await vscode.window.showQuickPick(
            snaps.map((s) => ({
              label: s.snapshot_id,
              description: `${s.atom_count ?? 0} atoms`,
              detail: s.label || "",
            })),
            { placeHolder: "Select historical snapshot for time-travel recall" }
          );
          if (!picked) return;
          snapshotId = picked.label;
        }

        const query =
          target.query !== undefined
            ? String(target.query)
            : await vscode.window.showInputBox({
                prompt: `Recall query at snapshot '${snapshotId}'`,
                placeHolder: "Search historical verified memory...",
              });
        if (!query || !query.trim()) return;

        const scope =
          target.project_scope !== undefined
            ? String(target.project_scope)
            : await vscode.window.showInputBox({
                prompt: "Optional project scope (leave blank for all)",
                placeHolder: "project:alpha",
              });
        if (scope === undefined) return;

        const recallPayload = {
          query: query.trim(),
          snapshot_id: snapshotId,
          limit: Number(target.limit) || 8,
        };
        if (scope.trim()) recallPayload.project_scope = scope.trim();

        const recallRes = await apiRequest(
          "POST",
          "/v1/snapshots/recall",
          recallPayload,
          { useOwnerToken: true }
        );
        if (!recallRes.ok) {
          vscode.window.showErrorMessage(
            `Snapshot recall failed (${recallRes.status}): ${
              recallRes.data?.error || "error"
            }`
          );
          return;
        }
        await openMarkdownPreview(
          formatSnapshotRecallMarkdown(recallRes.data, query.trim(), snapshotId)
        );
      } catch (err) {
        vscode.window.showErrorMessage(
          `Second Brain API unreachable: ${err.message}`
        );
      }
    }),

    vscode.commands.registerCommand("secondBrain.verifySnapshot", async (arg) => {
      try {
        const target = extractTargetData(arg);
        let snapshotId = target.snapshot_id || "";
        if (!snapshotId) {
          const snapRes = await apiRequest("GET", "/v1/snapshots");
          if (!snapRes.ok) {
            vscode.window.showErrorMessage(
              `Could not list snapshots: ${snapRes.data?.error || snapRes.status}`
            );
            return;
          }
          const snaps = snapRes.data?.items || snapRes.data?.snapshots || [];
          if (!snaps.length) {
            vscode.window.showInformationMessage("No snapshots recorded yet.");
            return;
          }
          const picked = await vscode.window.showQuickPick(
            snaps.map((s) => ({
              label: s.snapshot_id,
              description: `${s.atom_count ?? 0} atoms`,
              detail: s.label || "",
            })),
            { placeHolder: "Select snapshot to cryptographically verify" }
          );
          if (!picked) return;
          snapshotId = picked.label;
        }

        const verRes = await apiRequest("POST", "/v1/snapshots/verify", {
          snapshot_id: snapshotId,
        });
        if (!verRes.ok) {
          vscode.window.showErrorMessage(
            `Verify snapshot failed (${verRes.status}): ${
              verRes.data?.error || "error"
            }`
          );
          return;
        }
        const d = verRes.data || {};
        if (d.valid) {
          const action = await vscode.window.showInformationMessage(
            `Snapshot '${snapshotId}' VALID (${d.atom_count ?? 0} atoms, ${
              d.playbook_count ?? 0
            } playbooks, root ${String(d.merkle_root || "").slice(0, 16)}…).`,
            "Open Report"
          );
          if (action === "Open Report") {
            await openMarkdownPreview(formatSnapshotVerifyMarkdown(d));
          }
        } else {
          vscode.window.showErrorMessage(
            `Snapshot '${snapshotId}' INVALID: ${(d.issues || []).join("; ")}`
          );
          await openMarkdownPreview(formatSnapshotVerifyMarkdown(d));
        }
      } catch (err) {
        vscode.window.showErrorMessage(
          `Second Brain API unreachable: ${err.message}`
        );
      }
    }),

    vscode.commands.registerCommand(
      "secondBrain.recordExperience",
      async (arg) => {
        const opts = arg && typeof arg === "object" && !arg.sbData ? arg : {};
        const goal =
          opts.goal !== undefined
            ? String(opts.goal)
            : await vscode.window.showInputBox({
                prompt: "What task goal was attempted?",
                placeHolder:
                  "e.g. Migrate authentication middleware to strict token validation",
              });
        if (!goal || !goal.trim()) return;

        const resultSummary =
          opts.result_summary !== undefined
            ? String(opts.result_summary)
            : await vscode.window.showInputBox({
                prompt: "Summarize the outcome & verification",
                placeHolder:
                  "All 42 integration tests passed with zero regressions",
              });
        if (!resultSummary || !resultSummary.trim()) return;

        let success = opts.success;
        if (success === undefined) {
          const outcomePick = await vscode.window.showQuickPick(
            [
              { label: "Succeeded", success: true },
              { label: "Failed / Rolled Back", success: false },
            ],
            { placeHolder: "Did the task succeed?" }
          );
          if (!outcomePick) return;
          success = Boolean(outcomePick.success);
        }

        const rawLessons =
          opts.lessons !== undefined
            ? opts.lessons
            : await vscode.window.showInputBox({
                prompt: "Optional lessons learned (pipe | separated for multiple)",
                placeHolder: "Always pin schema version before migration...",
              });
        if (rawLessons === undefined) return;

        let contextList = parseListInput(opts.context || [], /[|,\n]+/);
        let planList = parseListInput(opts.plan || [], /[|\n]+/);
        let actionsList = parseListInput(opts.actions || [], /[|\n]+/);
        let toolsUsed = parseListInput(opts.tools_used || [], /[|,\n]+/);
        let referencedAtoms = parseListInput(
          opts.referenced_atoms || [],
          /[|,\n]+/
        );
        let reasoningSummary =
          opts.reasoning_summary !== undefined
            ? String(opts.reasoning_summary || "").trim() || null
            : null;
        let confidenceBefore =
          opts.confidence_before !== undefined && opts.confidence_before !== null
            ? Number(opts.confidence_before)
            : null;
        let confidenceAfter =
          opts.confidence_after !== undefined && opts.confidence_after !== null
            ? Number(opts.confidence_after)
            : null;
        let reflection =
          opts.reflection !== undefined
            ? String(opts.reflection || "").trim() || null
            : null;
        let candidateProcedureTitle =
          opts.candidate_procedure_title !== undefined
            ? String(opts.candidate_procedure_title || "").trim() || null
            : null;
        let proposeToInbox = Boolean(opts.propose_to_inbox);

        const hasProgrammaticExtras =
          opts.context !== undefined ||
          opts.actions !== undefined ||
          opts.tools_used !== undefined ||
          opts.referenced_atoms !== undefined ||
          opts.plan !== undefined ||
          opts.reasoning_summary !== undefined ||
          opts.confidence_before !== undefined ||
          opts.confidence_after !== undefined ||
          opts.reflection !== undefined ||
          opts.candidate_procedure_title !== undefined ||
          opts.propose_to_inbox !== undefined;

        if (!hasProgrammaticExtras) {
          const detailMode = await vscode.window.showQuickPick(
            [
              { label: "Save Experience Now", full: false },
              {
                label: "Add Extended Telemetry & Inbox Procedure Proposal...",
                full: true,
              },
            ],
            { placeHolder: "Choose episodic detail level" }
          );
          if (!detailMode) return;

          if (detailMode.full) {
            const ctxIn = await vscode.window.showInputBox({
              prompt: "Context tags (comma-separated, e.g. project:alpha, auth)",
            });
            if (ctxIn === undefined) return;
            contextList = parseListInput(ctxIn, /[,|\n]+/);

            const planIn = await vscode.window.showInputBox({
              prompt: "Planned steps (pipe | separated)",
            });
            if (planIn === undefined) return;
            planList = parseListInput(planIn, /[|\n]+/);

            const actIn = await vscode.window.showInputBox({
              prompt: "Executed actions (pipe | separated)",
            });
            if (actIn === undefined) return;
            actionsList = parseListInput(actIn, /[|\n]+/);

            const toolsIn = await vscode.window.showInputBox({
              prompt: "Tools used (comma-separated)",
            });
            if (toolsIn === undefined) return;
            toolsUsed = parseListInput(toolsIn, /[,|\n]+/);

            const refsIn = await vscode.window.showInputBox({
              prompt: "Referenced atom filenames (comma-separated)",
            });
            if (refsIn === undefined) return;
            referencedAtoms = parseListInput(refsIn, /[,|\n]+/);

            const reasonIn = await vscode.window.showInputBox({
              prompt: "Optional reasoning summary",
            });
            if (reasonIn === undefined) return;
            reasoningSummary = reasonIn.trim() || null;

            const confBeforeIn = await vscode.window.showInputBox({
              prompt: "Optional confidence before (0.0 to 1.0)",
            });
            if (confBeforeIn === undefined) return;
            if (confBeforeIn.trim()) {
              const n = Number(confBeforeIn.trim());
              if (Number.isFinite(n) && n >= 0 && n <= 1) confidenceBefore = n;
            }

            const confAfterIn = await vscode.window.showInputBox({
              prompt: "Optional confidence after (0.0 to 1.0)",
            });
            if (confAfterIn === undefined) return;
            if (confAfterIn.trim()) {
              const n = Number(confAfterIn.trim());
              if (Number.isFinite(n) && n >= 0 && n <= 1) confidenceAfter = n;
            }

            const reflIn = await vscode.window.showInputBox({
              prompt: "Optional post-task reflection",
            });
            if (reflIn === undefined) return;
            reflection = reflIn.trim() || null;

            const propPick = await vscode.window.showQuickPick(
              [
                { label: "Do not propose procedure to Inbox", propose: false },
                {
                  label: "Propose candidate procedure to Quarantined Inbox",
                  propose: true,
                },
              ],
              { placeHolder: "Propose reusable procedure to Quarantined Inbox?" }
            );
            if (!propPick) return;
            proposeToInbox = Boolean(propPick.propose);

            if (proposeToInbox) {
              const titleIn = await vscode.window.showInputBox({
                prompt: "Candidate procedure title",
                value: goal.trim().slice(0, 120),
              });
              if (titleIn === undefined) return;
              candidateProcedureTitle = titleIn.trim() || null;
            }
          }
        }

        const experiencePayload = {
          goal: goal.trim(),
          result_summary: resultSummary.trim(),
          success: Boolean(success),
          context: contextList,
          actions: actionsList,
          tools_used: toolsUsed,
          referenced_atoms: referencedAtoms,
          lessons: parseListInput(rawLessons, /[|\n]+/),
          plan: planList,
          reasoning_summary: reasoningSummary,
          confidence_before: confidenceBefore,
          confidence_after: confidenceAfter,
          reflection,
          candidate_procedure_title: candidateProcedureTitle,
          propose_to_inbox: proposeToInbox,
        };

        try {
          const res = await apiRequest(
            "POST",
            "/v1/cognitive/experiences",
            experiencePayload
          );
          if (!res.ok) {
            vscode.window.showErrorMessage(
              `Record experience failed: ${res.data?.error || res.status}`
            );
            return;
          }
          vscode.window.showInformationMessage(
            `Recorded episodic experience: ${
              res.data?.filename || res.data?.experience_id || "saved"
            }`
          );
          treeProvider.refresh();
        } catch (err) {
          vscode.window.showErrorMessage(
            `Second Brain API unreachable: ${err.message}`
          );
        }
      }
    )
  );
}

function deactivate() {
  if (activeExtensionState) {
    activeExtensionState.disposeChatPanel();
    activeExtensionState = null;
  }
}

module.exports = {
  activate,
  deactivate,
  getPort,
  isPidAlive,
  readBearerToken,
  readOwnerToken,
  VaultTreeProvider,
};
