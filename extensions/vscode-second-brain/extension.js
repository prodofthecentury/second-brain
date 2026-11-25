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
const { buildGraphWebviewHtml } = require("./lib/graph-webview");

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
          const tsTag =
            a.temporal_status === "expired"
              ? " [EXPIRED]"
              : a.temporal_status === "not_yet_valid"
              ? " [FUTURE]"
              : "";
          item.description = `${a.review_status || "pending"}${tsTag}`;
          item.tooltip =
            (a.claim || a.filename) +
            (a.valid_from || a.valid_until
              ? `\nValidity: ${a.valid_from || "-inf"} -> ${a.valid_until || "+inf"} (${a.temporal_status || "active"})`
              : "");
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
          const tsTag =
            a.temporal_status === "expired"
              ? " [EXPIRED]"
              : a.temporal_status === "not_yet_valid"
              ? " [FUTURE]"
              : "";
          item.description = `${a.review_status || a.status || ""}${tsTag}`;
          item.tooltip =
            (a.claim || a.filename) +
            (a.valid_from || a.valid_until
              ? `\nValidity: ${a.valid_from || "-inf"} -> ${a.valid_until || "+inf"} (${a.temporal_status || "active"})`
              : "");
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
            item.description =
              dr.reason ||
              (Array.isArray(dr.drift_reasons) && dr.drift_reasons.length
                ? dr.drift_reasons.join(", ")
                : "source anchor drift");
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
  let graphPanel = null;
  let activeChatStream = null;
  let lastChatSourcesByCitation = Object.create(null);
  let activeChatThreadId = "";
  let chatTurnHistory = [];

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

  let contextSignalTimer = null;

  function clearContextSignalTimer() {
    if (contextSignalTimer) {
      clearTimeout(contextSignalTimer);
      contextSignalTimer = null;
    }
  }

  function disposeGraphPanel() {
    if (graphPanel) {
      const gp = graphPanel;
      graphPanel = null;
      try {
        gp.dispose();
      } catch {}
    }
  }

  activeExtensionState = {
    abortActiveChatStream,
    disposeGraphPanel,
    clearContextSignalTimer,
    disposeChatPanel() {
      abortActiveChatStream();
      clearContextSignalTimer();
      if (chatPanel) {
        const p = chatPanel;
        chatPanel = null;
        try {
          p.dispose();
        } catch {}
      }
      disposeGraphPanel();
    },
    treeProvider,
    noteFsProvider,
  };

  context.subscriptions.push({
    dispose: () => {
      abortActiveChatStream();
      clearContextSignalTimer();
      disposeGraphPanel();
      chatPanel = null;
      graphPanel = null;
    },
  });

  function schedulePredictiveContextSignal(editor) {
    try {
      const cfg =
        vscode.workspace && typeof vscode.workspace.getConfiguration === "function"
          ? vscode.workspace.getConfiguration("secondBrain")
          : null;
      if (
        cfg &&
        typeof cfg.get === "function" &&
        cfg.get("enablePredictiveContextSignal", true) === false
      ) {
        return;
      }
      const doc = editor && editor.document;
      if (!doc) return;
      const scheme = String(doc.uri?.scheme || "file").toLowerCase();
      if (scheme !== "file" && scheme !== "secondbrain-note") return;
      const rawPath = String(doc.fileName || doc.uri?.fsPath || doc.uri?.path || "").trim();
      if (!rawPath) return;
      const base = path.basename(rawPath);
      const stem = base ? path.basename(base, path.extname(base)) : "";
      if (!stem || stem.length < 2) return;

      let projectScope = "";
      try {
        const folder =
          typeof vscode.workspace?.getWorkspaceFolder === "function" && doc.uri
            ? vscode.workspace.getWorkspaceFolder(doc.uri)
            : Array.isArray(vscode.workspace?.workspaceFolders)
            ? vscode.workspace.workspaceFolders[0]
            : null;
        const folderName = String(folder?.name || "")
          .trim()
          .toLowerCase()
          .replace(/[^a-z0-9_-]+/g, "_")
          .replace(/^_+|_+$/g, "");
        if (folderName) {
          projectScope = `project:${folderName}`;
        }
      } catch {}

      clearContextSignalTimer();
      contextSignalTimer = setTimeout(() => {
        contextSignalTimer = null;
        const payload = {
          active_file: stem.slice(0, 120),
          cursor_topic: stem.slice(0, 120),
          prewarm: true,
        };
        if (projectScope) {
          payload.project_scope = projectScope;
        }
        apiRequest("POST", "/v1/cognitive/context-signal", payload, {
          useOwnerToken: true,
        }).catch(() => {});
      }, 1500);
      if (typeof contextSignalTimer.unref === "function") {
        contextSignalTimer.unref();
      }
    } catch {}
  }

  if (typeof vscode.window.onDidChangeActiveTextEditor === "function") {
    context.subscriptions.push(
      vscode.window.onDidChangeActiveTextEditor((editor) => {
        schedulePredictiveContextSignal(editor);
      })
    );
  }

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
        const includeExpired = Boolean(msg.include_expired);
        const asOf =
          typeof msg.as_of === "string" && msg.as_of.trim()
            ? msg.as_of.trim().slice(0, 64)
            : "";

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
          include_expired: includeExpired,
        };
        if (asOf) reqPayloadObj.as_of = asOf;
        if (projectScope) reqPayloadObj.project_scope = projectScope;
        if (modelOverride) reqPayloadObj.model_override = modelOverride;
        const explicitTid =
          typeof msg.thread_id === "string" && msg.thread_id.trim()
            ? msg.thread_id.trim()
            : activeChatThreadId;
        if (explicitTid) reqPayloadObj.thread_id = explicitTid;
        if (chatTurnHistory.length > 0) {
          reqPayloadObj.history = chatTurnHistory.slice(-10);
        }
        let streamedAssistantBuf = "";

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
                    if (data.thread_id) {
                      activeChatThreadId = String(data.thread_id);
                    }
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
                    const tokPiece = String(data.text ?? data.token ?? "");
                    streamedAssistantBuf += tokPiece;
                    panel.webview.postMessage({
                      type: "append",
                      text: tokPiece,
                    });
                  } else if (eventType === "verified_answer") {
                    if (!data.answerable) {
                      const abstText =
                        data.reason ||
                        data.text ||
                        "Abstained: no verified claims answer this question.";
                      streamedAssistantBuf = String(abstText);
                      panel.webview.postMessage({
                        type: "markdown",
                        text: abstText,
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
                      const verText = formattedClaims || data.text || "";
                      streamedAssistantBuf = String(verText);
                      panel.webview.postMessage({
                        type: "markdown",
                        text: verText,
                      });
                    }
                  } else if (eventType === "grounding") {
                    panel.webview.postMessage({
                      type: "grounding",
                      grounding: data,
                    });
                  } else if (eventType === "abstain") {
                    const abstMsg = `**Abstained:** ${data.reason || data.text || ""}`;
                    streamedAssistantBuf = abstMsg;
                    panel.webview.postMessage({
                      type: "markdown",
                      text: abstMsg,
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
                    chatTurnHistory.push({ role: "user", content: query });
                    if (streamedAssistantBuf.trim()) {
                      chatTurnHistory.push({
                        role: "assistant",
                        content: streamedAssistantBuf.trim().slice(0, 4000),
                      });
                    }
                    if (chatTurnHistory.length > 20) {
                      chatTurnHistory = chatTurnHistory.slice(-20);
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
              { label: "Status", action: "status", description: "Inspect E2EE sync epochs, keychain & open conflicts" },
              { label: "Push Encrypted", action: "push", description: "Encrypt durable vault files & push signed head" },
              { label: "Pull & Merge", action: "pull", description: "Verify signatures, 3-way merge, rebuild indexes" },
              { label: "Verify Remote", action: "verify", description: "Full cryptographic audit of remote encrypted blobs" },
              { label: "Prune Orphan Blobs (GC)", action: "gc", description: "Delete unreferenced remote ciphertext blobs" },
              { label: "Seal Master Key in Keychain/DPAPI", action: "seal-keychain", description: "Move master key out of JSON into OS Keychain / DPAPI" },
              { label: "Toggle Background Auto-Sync", action: "auto", description: "Enable or disable background pull+push scheduler" },
              { label: "Resolve Conflicts (3-Way)", action: "resolve", description: "Inspect unified diff & resolve quarantined sync conflicts" },
            ],
            { placeHolder: "Zero-Knowledge E2EE Multi-Device Vault Sync" }
          );
          if (!pick) return;
          action = pick.action;
        }
        if (action === "resolve") {
          await vscode.commands.executeCommand("secondBrain.resolveSyncConflicts");
          return;
        }
        let res;
        if (action === "status") {
          res = await apiRequest("GET", "/v1/sync/status", null, { useOwnerToken: true });
        } else if (action === "auto") {
          const curAuto = await apiRequest("GET", "/v1/sync/auto", null, { useOwnerToken: true });
          const nextEnabled = !(curAuto.ok && curAuto.data?.enabled);
          res = await apiRequest(
            "POST",
            "/v1/sync/auto",
            { enabled: nextEnabled, interval_seconds: Number(opts.interval_seconds) || 300 },
            { useOwnerToken: true }
          );
          if (res.ok) {
            vscode.window.showInformationMessage(
              `Background E2EE Auto-Sync ${res.data?.enabled ? "enabled" : "disabled"} (${res.data?.interval_seconds || 300}s).`
            );
            return;
          }
        } else if (
          action === "push" ||
          action === "pull" ||
          action === "verify" ||
          action === "gc" ||
          action === "seal-keychain"
        ) {
          const payload = opts.passphrase ? { passphrase: String(opts.passphrase) } : {};
          if (action === "pull") payload.trust_new_peers = opts.trust_new_peers !== false;
          if (action === "gc" && opts.dry_run) payload.dry_run = true;
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
        if (target.as_of !== undefined && String(target.as_of).trim()) {
          recallPayload.as_of = String(target.as_of).trim();
        } else if (target.query === undefined) {
          const asOfInput = await vscode.window.showInputBox({
            prompt: "Optional as_of ISO-8601 date override (blank = snapshot timestamp)",
            placeHolder: "YYYY-MM-DD",
          });
          if (asOfInput === undefined) return;
          if (asOfInput.trim()) recallPayload.as_of = asOfInput.trim();
        }
        if (target.include_expired !== undefined) {
          recallPayload.include_expired = Boolean(target.include_expired);
        }

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
    ),

    vscode.commands.registerCommand("secondBrain.switchVault", async () => {
      try {
        const listRes = await apiRequest("GET", "/v1/vaults", null, {
          useOwnerToken: true,
        });
        if (!listRes.ok) {
          vscode.window.showErrorMessage(
            `Could not list vault workspaces: ${listRes.data?.error || listRes.status}`
          );
          return;
        }
        const vaults = listRes.data?.vaults || [];
        if (!vaults.length) {
          vscode.window.showInformationMessage("No vault workspaces registered.");
          return;
        }
        const picked = await vscode.window.showQuickPick(
          vaults.map((v) => ({
            label: `${v.active ? "$(check) " : ""}${v.label || v.vault_id}`,
            description: v.vault_id,
            detail: `${v.path} (${v.atom_count ?? 0} atoms, ${v.note_count ?? 0} notes)`,
            vaultId: v.vault_id,
          })),
          { placeHolder: "Select active Second Brain vault workspace" }
        );
        if (!picked) return;
        const swRes = await apiRequest(
          "POST",
          "/v1/vaults/switch",
          { vault_id: picked.vaultId },
          { useOwnerToken: true }
        );
        if (!swRes.ok) {
          vscode.window.showErrorMessage(
            `Vault switch failed: ${swRes.data?.error || swRes.status}`
          );
          return;
        }
        vscode.window.showInformationMessage(
          `Switched Second Brain workspace to '${swRes.data?.active_vault_id || picked.vaultId}'.`
        );
        treeProvider.refresh();
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.federatedSearch", async () => {
      const query = await vscode.window.showInputBox({
        prompt: "Opt-in read-only federated search across opted-in vaults",
        placeHolder: "e.g. PostgreSQL migration policy",
      });
      if (!query || !query.trim()) return;
      try {
        const res = await apiRequest(
          "POST",
          "/v1/vaults/federated-recall",
          { query: query.trim(), limit: 10 },
          { useOwnerToken: true }
        );
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Federated search failed: ${res.data?.error || res.status}`
          );
          return;
        }
        const hits = res.data?.results || [];
        const lines = [
          `# Second Brain — Opt-In Federated Cross-Vault Search`,
          ``,
          `- **Query:** ${query.trim()}`,
          `- **Searched Vaults:** ${(res.data?.searched_vaults || []).join(", ") || "(none opted-in)"}`,
          `- **Matches:** ${hits.length}`,
          ``,
        ];
        for (const h of hits) {
          lines.push(`## [${h.vault_label || h.vault_id}] ${h.filename} (score ${h.score})`);
          lines.push(``);
          lines.push(String(h.content || "").trim());
          lines.push(``);
        }
        const doc = await vscode.workspace.openTextDocument({
          language: "markdown",
          content: lines.join("\n"),
        });
        await vscode.window.showTextDocument(doc, { preview: true });
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.pairClipper", async (arg) => {
      const opts = arg && typeof arg === "object" ? arg : {};
      let mode = opts.origin ? "origin" : opts.mode || "";
      if (!mode) {
        const modePick = await vscode.window.showQuickPick(
          [
            {
              label: "Generate 1-Click Pairing Code (Recommended)",
              mode: "code",
              description: "Mint a 5-minute single-use SBC-XXXX-XXXX-XXXX code & copy to clipboard",
            },
            {
              label: "Pair by Browser Extension Origin",
              mode: "origin",
              description: "Paste chrome-extension:// or moz-extension:// origin manually",
            },
          ],
          { placeHolder: "Pair Second Brain Browser Web Clipper" }
        );
        if (!modePick) return;
        mode = modePick.mode;
      }

      if (mode === "code") {
        const label =
          opts.label !== undefined
            ? String(opts.label)
            : await vscode.window.showInputBox({
                prompt: "Browser label",
                value: "Browser Clipper",
              });
        if (label === undefined) return;
        try {
          const res = await apiRequest(
            "POST",
            "/v1/clipper/pair-code",
            { label: label.trim() || "Browser Clipper", ttl_seconds: 300 },
            { useOwnerToken: true }
          );
          if (!res.ok) {
            vscode.window.showErrorMessage(
              `Pairing code generation failed: ${res.data?.error || res.status}`
            );
            return;
          }
          const code = String(res.data?.code || "");
          if (code && vscode.env && vscode.env.clipboard) {
            await vscode.env.clipboard.writeText(code);
          }
          vscode.window.showInformationMessage(
            `1-Click Clipper Pairing Code copied to clipboard: ${code} (valid 5 min). Paste into the extension popup.`
          );
        } catch (err) {
          vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
        }
        return;
      }

      const origin =
        opts.origin !== undefined
          ? String(opts.origin)
          : await vscode.window.showInputBox({
              prompt: "Browser extension origin (copy from Second Brain Clipper popup -> Pair)",
              placeHolder: "chrome-extension://<32-char-id> or moz-extension://<uuid>",
            });
      if (!origin || !origin.trim()) return;
      const label =
        opts.label !== undefined
          ? String(opts.label)
          : await vscode.window.showInputBox({
              prompt: "Browser label",
              value: "Browser Clipper",
            });
      if (label === undefined) return;
      try {
        const res = await apiRequest(
          "POST",
          "/v1/clipper/pair",
          { origin: origin.trim(), label: label.trim() || "Browser Clipper" },
          { useOwnerToken: true }
        );
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Clipper pairing failed: ${res.data?.error || res.status}`
          );
          return;
        }
        const token = String(res.data?.token || "");
        if (token && vscode.env && vscode.env.clipboard) {
          await vscode.env.clipboard.writeText(token);
        }
        vscode.window.showInformationMessage(
          `Paired '${res.data?.label}' (${res.data?.pairing_id}). Scoped clipper_* token copied to clipboard.`
        );
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.newChatThread", async (arg) => {
      const opts = arg && typeof arg === "object" ? arg : {};
      try {
        const res = await apiRequest(
          "POST",
          "/v1/chat/threads",
          {
            title: String(opts.title || ""),
            project_scope: opts.project_scope || undefined,
            host_id: String(opts.host_id || "ollama_local"),
            mode: String(opts.mode || "stream"),
          },
          { useOwnerToken: true }
        );
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Create chat thread failed: ${res.data?.error || res.status}`
          );
          return;
        }
        activeChatThreadId = String(res.data?.thread_id || "");
        chatTurnHistory = [];
        vscode.window.showInformationMessage(
          `Started persistent Second Brain chat thread '${activeChatThreadId}'.`
        );
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.pinChatTurn", async () => {
      try {
        if (activeChatThreadId) {
          const res = await apiRequest(
            "POST",
            `/v1/chat/threads/${encodeURIComponent(activeChatThreadId)}/pin`,
            {},
            { useOwnerToken: true }
          );
          if (res.ok) {
            vscode.window.showInformationMessage(
              `Pinned latest chat answer to Working Note: ${res.data?.filename || "saved"}`
            );
            treeProvider.refresh();
            return;
          }
        }
        const lastAssistant = [...chatTurnHistory]
          .reverse()
          .find((t) => t.role === "assistant" && t.content);
        if (!lastAssistant) {
          vscode.window.showInformationMessage(
            "Ask a question in Second Brain Chat first to pin an answer."
          );
          return;
        }
        const nRes = await apiRequest(
          "POST",
          "/v1/notes",
          {
            title: "Pinned Chat Synthesis",
            body: lastAssistant.content,
            tags: ["pinned-chat"],
          },
          { useOwnerToken: true }
        );
        if (!nRes.ok) {
          vscode.window.showErrorMessage(
            `Pin chat turn failed: ${nRes.data?.error || nRes.status}`
          );
          return;
        }
        vscode.window.showInformationMessage(
          `Pinned chat answer to Working Note: ${nRes.data?.filename || "saved"}`
        );
        treeProvider.refresh();
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.autoLinkGraph", async (arg) => {
      const opts = arg && typeof arg === "object" ? arg : {};
      try {
        const res = await apiRequest(
          "POST",
          "/v1/graph/auto-link",
          {
            actor: "vscode_owner",
            project_scope: opts.project_scope || undefined,
            include_pending: Boolean(opts.include_pending),
            dry_run: Boolean(opts.dry_run),
          },
          { useOwnerToken: true }
        );
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Graph auto-link failed: ${res.data?.error || res.status}`
          );
          return;
        }
        vscode.window.showInformationMessage(
          `Auto-linked ${res.data?.updated_atom_count ?? 0} atom(s) (orphans ${res.data?.orphan_before ?? "?"} -> ${res.data?.orphan_after ?? "?"}).`
        );
        treeProvider.refresh();
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.resolveSyncConflicts", async () => {
      try {
        const listRes = await apiRequest("GET", "/v1/sync/conflicts", null, {
          useOwnerToken: true,
        });
        if (!listRes.ok) {
          vscode.window.showErrorMessage(
            `Could not read sync conflicts: ${listRes.data?.error || listRes.status}`
          );
          return;
        }
        const conflicts = listRes.data?.conflicts || [];
        if (!conflicts.length) {
          vscode.window.showInformationMessage("0 unresolved E2EE sync conflicts.");
          return;
        }
        const picked = await vscode.window.showQuickPick(
          conflicts.map((c) => ({
            label: c.rel_path,
            description: `epoch ${c.remote_epoch ?? "?"} · ${c.conflict_kind || "divergent_edit"}`,
            detail: `conflict_id: ${c.conflict_id}`,
            conflict: c,
          })),
          { placeHolder: "Select a sync conflict to inspect & resolve (3-way)" }
        );
        if (!picked) return;
        const cid = picked.conflict.conflict_id || picked.conflict.rel_path;
        const detRes = await apiRequest(
          "GET",
          `/v1/sync/conflicts/${encodeURIComponent(cid)}`,
          null,
          { useOwnerToken: true }
        );
        if (detRes.ok && detRes.data) {
          const md = [
            `# 3-Way Sync Conflict: \`${detRes.data.rel_path}\``,
            "",
            `- **Conflict ID**: \`${detRes.data.conflict_id}\``,
            `- **Local SHA-256**: \`${detRes.data.current_local_sha256 || "missing"}\``,
            `- **Remote SHA-256**: \`${detRes.data.remote_sha256 || "deleted"}\``,
            "",
            "## Unified Diff (Local vs Remote)",
            "```diff",
            detRes.data.unified_diff || "(no text diff)",
            "```",
          ].join("\n");
          await openMarkdownPreview(md);
        }
        const stratPick = await vscode.window.showQuickPick(
          [
            { label: "Keep Local", strategy: "keep_local" },
            { label: "Accept Remote", strategy: "accept_remote" },
            { label: "Keep Both (save peer copy)", strategy: "keep_both" },
          ],
          { placeHolder: `Choose resolution strategy for ${picked.label}` }
        );
        if (!stratPick) return;
        const res = await apiRequest(
          "POST",
          "/v1/sync/conflicts/resolve",
          {
            conflict_id: cid,
            resolution: stratPick.strategy,
            expected_local_sha256: detRes.data?.current_local_sha256 || undefined,
            actor: "vscode_owner",
          },
          { useOwnerToken: true }
        );
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Conflict resolution failed: ${res.data?.error || res.status}`
          );
          return;
        }
        vscode.window.showInformationMessage(
          `Resolved '${picked.label}' via ${stratPick.strategy} (${res.data?.remaining_conflicts ?? 0} remaining).`
        );
        treeProvider.refresh();
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.manageOnnxModels", async (arg) => {
      const opts = arg && typeof arg === "object" ? arg : {};
      let action = opts.action || "";
      if (!action) {
        const pick = await vscode.window.showQuickPick(
          [
            {
              label: "Status — Inspect Local Neural ONNX Weights",
              action: "status",
            },
            {
              label: "Pull — Download & SHA-256 Verify Pinned ONNX Weights",
              action: "pull",
            },
            {
              label: "Verify — Run SHA-256 & CPU Smoke Inference Check",
              action: "verify",
            },
          ],
          { placeHolder: "Second Brain Local Neural ONNX Models (Cross-Encoder & DeBERTa NLI)" }
        );
        if (!pick) return;
        action = pick.action;
      }
      try {
        if (action === "status") {
          const res = await apiRequest("GET", "/v1/models/onnx", null, {
            useOwnerToken: true,
          });
          if (!res.ok) {
            vscode.window.showErrorMessage(
              `ONNX status failed: ${res.data?.error || res.status}`
            );
            return;
          }
          await openMarkdownPreview(
            [
              "# Second Brain — Local Neural ONNX Models",
              "",
              `- **Runtime Available:** \`${Boolean(
                res.data?.onnx_runtime_ready ?? res.data?.runtime_available
              )}\``,
              `- **All Verified:** \`${Boolean(res.data?.all_verified)}\``,
              `- **Models Root:** \`${res.data?.models_root || ""}\``,
              "",
              "```json",
              JSON.stringify(res.data?.slots || {}, null, 2),
              "```",
            ].join("\n")
          );
          return;
        }
        const endpoint =
          action === "pull" ? "/v1/models/onnx/pull" : "/v1/models/onnx/verify";
        const body =
          action === "pull"
            ? { slot: opts.slot || "all", force: Boolean(opts.force) }
            : { slot: opts.slot || "all", smoke_test: opts.smoke_test !== false };
        const res = await apiRequest("POST", endpoint, body, {
          useOwnerToken: true,
        });
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `ONNX ${action} failed: ${res.data?.error || res.status}`
          );
          return;
        }
        vscode.window.showInformationMessage(
          `ONNX ${action} complete: all_verified=${Boolean(res.data?.all_verified)}.`
        );
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.setAtomValidity", async (arg) => {
      const target = extractTargetData(arg);
      let filename = target.filename || "";
      let expectedSha = target.sha256 || target.atom?.sha256 || "";
      let curFrom = target.atom?.valid_from || "";
      let curUntil = target.atom?.valid_until || "";

      try {
        if (!filename) {
          const listRes = await apiRequest("GET", "/v1/atoms?limit=200", null, {
            useOwnerToken: true,
          });
          const items = listRes.data?.items || [];
          if (!items.length) {
            vscode.window.showInformationMessage("No atoms found in vault.");
            return;
          }
          const picked = await vscode.window.showQuickPick(
            items.map((a) => ({
              label: a.filename,
              description: `${a.review_status || ""}${
                a.temporal_status && a.temporal_status !== "active"
                  ? " [" + a.temporal_status + "]"
                  : ""
              }`,
              detail: a.claim || "",
              atom: a,
            })),
            { placeHolder: "Select atom to set bi-temporal validity window" }
          );
          if (!picked) return;
          filename = picked.atom.filename;
          expectedSha = picked.atom.sha256 || "";
          curFrom = picked.atom.valid_from || "";
          curUntil = picked.atom.valid_until || "";
        }

        if (!expectedSha) {
          const getRes = await apiRequest(
            "GET",
            `/v1/atoms/${encodeURIComponent(filename)}`,
            null,
            { useOwnerToken: true }
          );
          if (getRes.ok && getRes.data) {
            const doc = getRes.data.atom || getRes.data;
            expectedSha = doc.sha256 || "";
            curFrom = doc.valid_from || curFrom;
            curUntil = doc.valid_until || curUntil;
          }
        }

        const validFrom =
          target.valid_from !== undefined
            ? String(target.valid_from || "")
            : await vscode.window.showInputBox({
                prompt: `valid_from for ${filename} (YYYY-MM-DD or ISO-8601, blank to clear)`,
                value: curFrom,
                placeHolder: "e.g. 2026-01-01",
              });
        if (validFrom === undefined) return;

        const validUntil =
          target.valid_until !== undefined
            ? String(target.valid_until || "")
            : await vscode.window.showInputBox({
                prompt: `valid_until for ${filename} (YYYY-MM-DD or ISO-8601, blank to clear)`,
                value: curUntil,
                placeHolder: "e.g. 2026-12-31",
              });
        if (validUntil === undefined) return;

        const res = await apiRequest(
          "POST",
          `/v1/atoms/${encodeURIComponent(filename)}/validity`,
          {
            expected_sha256: expectedSha,
            valid_from: validFrom.trim() || null,
            valid_until: validUntil.trim() || null,
            actor: "vscode_owner",
          },
          { useOwnerToken: true }
        );
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Set validity failed: ${res.data?.error || res.status}`
          );
          return;
        }
        vscode.window.showInformationMessage(
          `Updated ${filename} validity (${res.data?.valid_from || "-inf"} -> ${
            res.data?.valid_until || "+inf"
          }, status=${res.data?.temporal_status || "active"}).`
        );
        treeProvider.refresh();
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.mobileInboxDrop", async (arg) => {
      const opts = arg && typeof arg === "object" ? arg : {};
      let action = opts.action || "";
      if (!action) {
        const pick = await vscode.window.showQuickPick(
          [
            {
              label: "Pair Mobile PWA / iOS Shortcut (SBID1 E2EE)",
              action: "pair",
            },
            {
              label: "Export Encrypted Recall Index for Paired PWAs (SBMR1)",
              action: "export_recall",
            },
            {
              label: "Rotate Mobile Recall Encryption Key (SBMR1)",
              action: "rotate_recall",
            },
            {
              label: "Pull & Decrypt Mobile Inbox Drops into Quarantined Inbox",
              action: "pull",
            },
            {
              label: "List / Revoke Paired Mobile Devices",
              action: "revoke",
            },
          ],
          { placeHolder: "Second Brain Zero-Knowledge Mobile Inbox Drop" }
        );
        if (!pick) return;
        action = pick.action;
      }
      try {
        if (action === "pair") {
          const label =
            opts.device_label !== undefined
              ? String(opts.device_label)
              : await vscode.window.showInputBox({
                  prompt: "Mobile device label",
                  value: "Mobile Quick-Capture",
                });
          if (label === undefined) return;
          const pwaUrl =
            opts.pwa_base_url !== undefined
              ? String(opts.pwa_base_url)
              : await vscode.window.showInputBox({
                  prompt: "Optional static PWA base URL (blank for #pair=... fragment only)",
                  placeHolder: "https://pages.example/inbox-drop/",
                });
          if (pwaUrl === undefined) return;
          let includeRecall;
          if (opts.include_recall !== undefined) {
            includeRecall = Boolean(opts.include_recall);
          } else {
            const recallPick = await vscode.window.showQuickPick(
              [
                {
                  label: "Yes — Enable Zero-Knowledge Recall & Review Tab (SBMR1, Recommended)",
                  value: true,
                },
                {
                  label: "No — Drop-Only Key (Write-Only Blind Capture)",
                  value: false,
                },
              ],
              { placeHolder: "Include encrypted mobile recall key (SBMR1) in pairing URL?" }
            );
            if (!recallPick) return;
            includeRecall = Boolean(recallPick.value);
          }
          const res = await apiRequest(
            "POST",
            "/v1/sync/inbox-drop/pair",
            {
              device_label: label.trim() || "Mobile Quick-Capture",
              pwa_base_url: pwaUrl.trim(),
              include_s3_credentials: Boolean(opts.include_s3_credentials),
              include_recall: includeRecall,
            },
            { useOwnerToken: true }
          );
          if (!res.ok) {
            vscode.window.showErrorMessage(
              `Mobile drop pairing failed: ${res.data?.error || res.status}`
            );
            return;
          }
          const shareLink = String(
            res.data?.pwa_pairing_url || res.data?.pairing_fragment || ""
          );
          if (shareLink && vscode.env && vscode.env.clipboard) {
            await vscode.env.clipboard.writeText(shareLink);
          }
          vscode.window.showInformationMessage(
            `Paired '${res.data?.device_label}' (token_id=${res.data?.token_id}, include_recall=${Boolean(
              res.data?.include_recall ?? includeRecall
            )}). Pairing link copied to clipboard.`
          );
          return res.data;
        }
        if (action === "export_recall") {
          const res = await apiRequest(
            "POST",
            "/v1/sync/inbox-drop/export-recall",
            {
              include_pending_review: opts.include_pending_review !== false,
              sync_to_backend: opts.sync_to_backend !== false,
            },
            { useOwnerToken: true }
          );
          if (!res.ok) {
            vscode.window.showErrorMessage(
              `Export mobile recall index failed: ${res.data?.error || res.status}`
            );
            return;
          }
          vscode.window.showInformationMessage(
            `Exported encrypted mobile recall index (${res.data?.atom_count ?? 0} atoms -> ${
              res.data?.object_key || "remote"
            }).`
          );
          return res.data;
        }
        if (action === "rotate_recall") {
          const res = await apiRequest(
            "POST",
            "/v1/sync/inbox-drop/rotate-recall",
            {
              reexport: opts.reexport !== false,
              vault_id: opts.vault_id || undefined,
            },
            { useOwnerToken: true }
          );
          if (!res.ok) {
            vscode.window.showErrorMessage(
              `Rotate mobile recall key failed: ${res.data?.error || res.status}`
            );
            return;
          }
          vscode.window.showInformationMessage(
            `Rotated SBMR1 recall key for vault ${res.data?.vault_id || "active"}. Create new pairings for devices needing the new key.`
          );
          return res.data;
        }
        if (action === "pull") {
          const res = await apiRequest(
            "POST",
            "/v1/sync/inbox-drop/pull",
            { delete_consumed: opts.delete_consumed !== false },
            { useOwnerToken: true }
          );
          if (!res.ok) {
            vscode.window.showErrorMessage(
              `Pull mobile drops failed: ${res.data?.error || res.status}`
            );
            return;
          }
          vscode.window.showInformationMessage(
            `Mobile Inbox Drops pulled: ${res.data?.ingested_count ?? 0} staged into 0-raw/inbox/, ${
              res.data?.skipped_duplicate_count ?? 0
            } duplicate(s) skipped.`
          );
          treeProvider.refresh();
          return;
        }
        const listRes = await apiRequest(
          "GET",
          "/v1/sync/inbox-drop/pairings",
          null,
          { useOwnerToken: true }
        );
        if (!listRes.ok) {
          vscode.window.showErrorMessage(
            `List mobile pairings failed: ${listRes.data?.error || listRes.status}`
          );
          return;
        }
        const active = (listRes.data?.pairings || []).filter((p) => !p.revoked);
        if (!active.length) {
          vscode.window.showInformationMessage("No active mobile drop pairings.");
          return;
        }
        const picked = await vscode.window.showQuickPick(
          active.map((p) => ({
            label: p.label || p.device_label || p.token_id,
            description: `token_id: ${p.token_id}`,
            detail: `Created ${p.created_at || ""}`,
            tokenId: p.token_id,
          })),
          { placeHolder: "Select mobile drop pairing to revoke" }
        );
        if (!picked) return;
        const revRes = await apiRequest(
          "DELETE",
          `/v1/sync/inbox-drop/${encodeURIComponent(picked.tokenId)}`,
          {},
          { useOwnerToken: true }
        );
        if (!revRes.ok) {
          vscode.window.showErrorMessage(
            `Revoke failed: ${revRes.data?.error || revRes.status}`
          );
          return;
        }
        vscode.window.showInformationMessage(
          `Revoked mobile drop token '${picked.tokenId}'.`
        );
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.ingestMedia", async (arg) => {
      const opts = arg && typeof arg === "object" ? arg : {};
      let filePath = opts.filePath ? String(opts.filePath) : "";
      if (!filePath) {
        const picked = await vscode.window.showOpenDialog({
          canSelectMany: false,
          openLabel: "Ingest Media into Second Brain",
          filters: {
            "Supported Media & Documents": [
              "png",
              "jpg",
              "jpeg",
              "webp",
              "wav",
              "mp3",
              "m4a",
              "webm",
              "ogg",
              "flac",
              "pdf",
              "docx",
              "epub",
              "html",
              "csv",
              "tsv",
            ],
          },
        });
        if (!picked || !picked.length) return;
        filePath = picked[0].fsPath;
      }
      try {
        const st = fs.statSync(filePath);
        if (st.size === 0 || st.size > 24 * 1024 * 1024) {
          vscode.window.showErrorMessage(
            "Media file must be non-empty and at most 24 MiB."
          );
          return;
        }
        const rawBytes = fs.readFileSync(filePath);
        const res = await apiRequest(
          "POST",
          "/v1/ingest/media",
          {
            filename: path.basename(filePath),
            content_base64: rawBytes.toString("base64"),
          },
          { useOwnerToken: true }
        );
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Media ingestion failed: ${res.data?.error || res.status}`
          );
          return;
        }
        const d = res.data || {};
        vscode.window.showInformationMessage(
          `${d.quarantined ? "Quarantined in Inbox" : "Ingested"} (${
            d.category || "media"
          }): ${d.output_rel_path || d.output_filename}`
        );
        treeProvider.refresh();
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain media ingest error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.installGitHook", async (arg) => {
      const opts = arg && typeof arg === "object" ? arg : {};
      const wsDefault =
        vscode.workspace.workspaceFolders &&
        vscode.workspace.workspaceFolders.length
          ? vscode.workspace.workspaceFolders[0].uri.fsPath
          : "";
      const repo =
        opts.repo !== undefined
          ? String(opts.repo)
          : await vscode.window.showInputBox({
              prompt: "Git repository path for Second Brain post-commit hook",
              value: wsDefault,
            });
      if (!repo || !repo.trim()) return;
      try {
        const res = await apiRequest(
          "POST",
          "/v1/git/hook/install",
          {
            repo: repo.trim(),
            project_scope: opts.project_scope || undefined,
          },
          { useOwnerToken: true }
        );
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Install Git hook failed: ${res.data?.error || res.status}`
          );
          return;
        }
        vscode.window.showInformationMessage(
          `Installed Second Brain post-commit hook (${res.data?.project_scope || "repo"}).`
        );
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.uninstallGitHook", async (arg) => {
      const opts = arg && typeof arg === "object" ? arg : {};
      const wsDefault =
        vscode.workspace.workspaceFolders &&
        vscode.workspace.workspaceFolders.length
          ? vscode.workspace.workspaceFolders[0].uri.fsPath
          : "";
      const repo =
        opts.repo !== undefined
          ? String(opts.repo)
          : await vscode.window.showInputBox({
              prompt: "Git repository path to remove Second Brain post-commit hook",
              value: wsDefault,
            });
      if (!repo || !repo.trim()) return;
      try {
        const res = await apiRequest(
          "POST",
          "/v1/git/hook/uninstall",
          { repo: repo.trim() },
          { useOwnerToken: true }
        );
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Uninstall Git hook failed: ${res.data?.error || res.status}`
          );
          return;
        }
        vscode.window.showInformationMessage(
          `Second Brain post-commit hook: ${res.data?.status || "removed"}.`
        );
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.distillGitCommits", async (arg) => {
      const opts = arg && typeof arg === "object" ? arg : {};
      const wsDefault =
        vscode.workspace.workspaceFolders &&
        vscode.workspace.workspaceFolders.length
          ? vscode.workspace.workspaceFolders[0].uri.fsPath
          : "";
      const repo =
        opts.repo !== undefined
          ? String(opts.repo)
          : await vscode.window.showInputBox({
              prompt: "Git repository path to distill recent commits into memory",
              value: wsDefault,
            });
      if (!repo || !repo.trim()) return;
      const maxCommits = Number(opts.max_commits) || 5;
      try {
        const res = await apiRequest(
          "POST",
          "/v1/git/distill",
          {
            repo: repo.trim(),
            project_scope: opts.project_scope || undefined,
            max_commits: maxCommits,
            stage_inbox: opts.stage_inbox !== false,
          },
          { useOwnerToken: true }
        );
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Distill Git commits failed: ${res.data?.error || res.status}`
          );
          return;
        }
        const d = res.data || {};
        vscode.window.showInformationMessage(
          `Distilled ${d.distilled_count ?? 0} commit(s) into ${d.project_scope || "vault"}.`
        );
        treeProvider.refresh();
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.backupVault", async (arg) => {
      const opts = arg && typeof arg === "object" ? arg : {};
      let action = opts.action || "";
      if (!action) {
        const picked = await vscode.window.showQuickPick(
          [
            {
              label: "Create Encrypted .sbvault Backup",
              action: "create",
              description: "AES-256-GCM + Merkle-verified Three-Store archive",
            },
            {
              label: "Verify .sbvault Backup Archive",
              action: "verify",
              description: "In-memory AEAD, SHA-256 & Merkle root check",
            },
            {
              label: "Restore .sbvault Archive to Folder",
              action: "restore",
              description: "Extract verified durable vault & rebuild FTS5 indexes",
            },
          ],
          { placeHolder: "Portable Second Brain Vault Backup & Restore" }
        );
        if (!picked) return;
        action = picked.action;
      }

      try {
        if (action === "create") {
          const outputPath =
            opts.output_path ||
            (await vscode.window.showInputBox({
              prompt: "Destination .sbvault path (must be outside live vault)",
              placeHolder: "C:\\Backups\\second-brain.sbvault",
            }));
          if (!outputPath || !outputPath.trim()) return;
          const passphrase =
            opts.passphrase !== undefined
              ? opts.passphrase
              : await vscode.window.showInputBox({
                  prompt: "Backup passphrase (at least 8 characters)",
                  password: true,
                });
          if (!passphrase && !opts.unencrypted) return;
          const res = await apiRequest(
            "POST",
            "/v1/vault/backup/create",
            {
              output_path: outputPath.trim(),
              passphrase: opts.unencrypted ? undefined : passphrase,
              unencrypted: Boolean(opts.unencrypted),
            },
            { useOwnerToken: true }
          );
          if (!res.ok) {
            vscode.window.showErrorMessage(
              `Backup creation failed: ${res.data?.error || res.status}`
            );
            return;
          }
          vscode.window.showInformationMessage(
            `Created backup: ${res.data?.backup_path} (${res.data?.file_count ?? 0} files).`
          );
          return;
        }

        if (action === "verify") {
          const backupPath =
            opts.backup_path ||
            (await vscode.window.showInputBox({
              prompt: "Path to .sbvault archive to verify",
            }));
          if (!backupPath || !backupPath.trim()) return;
          const passphrase =
            opts.passphrase !== undefined
              ? opts.passphrase
              : await vscode.window.showInputBox({
                  prompt: "Backup passphrase (leave blank if unencrypted)",
                  password: true,
                });
          if (passphrase === undefined) return;
          const res = await apiRequest(
            "POST",
            "/v1/vault/backup/verify",
            {
              backup_path: backupPath.trim(),
              passphrase: passphrase || undefined,
            },
            { useOwnerToken: true }
          );
          if (!res.ok) {
            vscode.window.showErrorMessage(
              `Backup verification failed: ${res.data?.error || res.status}`
            );
            return;
          }
          vscode.window.showInformationMessage(
            `Backup VALID (${res.data?.cipher}): ${res.data?.file_count ?? 0} files, Merkle ${String(
              res.data?.merkle_root || ""
            ).slice(0, 16)}…`
          );
          return;
        }

        if (action === "restore") {
          const backupPath =
            opts.backup_path ||
            (await vscode.window.showInputBox({
              prompt: "Path to .sbvault archive to restore",
            }));
          if (!backupPath || !backupPath.trim()) return;
          const targetDir =
            opts.target_brain_dir ||
            (await vscode.window.showInputBox({
              prompt: "Destination vault folder to restore into",
            }));
          if (!targetDir || !targetDir.trim()) return;
          const passphrase =
            opts.passphrase !== undefined
              ? opts.passphrase
              : await vscode.window.showInputBox({
                  prompt: "Backup passphrase (leave blank if unencrypted)",
                  password: true,
                });
          if (passphrase === undefined) return;
          const res = await apiRequest(
            "POST",
            "/v1/vault/backup/restore",
            {
              backup_path: backupPath.trim(),
              target_brain_dir: targetDir.trim(),
              passphrase: passphrase || undefined,
              force: Boolean(opts.force),
            },
            { useOwnerToken: true }
          );
          if (!res.ok) {
            vscode.window.showErrorMessage(
              `Backup restore failed: ${res.data?.error || res.status}`
            );
            return;
          }
          vscode.window.showInformationMessage(
            `Restored ${res.data?.restored_files ?? 0} files into ${res.data?.target_brain_dir}.`
          );
        }
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    // ── Stage 4 Executable Skills (Promote / Execute in Sandbox / Kill-Switch / Audit) ──
    vscode.commands.registerCommand(
      "secondBrain.manageExecutableSkills",
      async (arg) => {
        try {
          const opts = arg && typeof arg === "object" ? arg : {};
          const action =
            opts.action ||
            (
              await vscode.window.showQuickPick(
                [
                  { label: "list", description: "List Stage 4 Executable Skills & Kill-Switch Status" },
                  { label: "promote", description: "Promote Verified Playbook to Executable Skill" },
                  { label: "execute", description: "Execute Promoted Skill in Isolated Sandbox" },
                  { label: "dry_run", description: "Dry-Run Skill Verification without Subprocess" },
                  { label: "revoke", description: "Revoke Promoted Executable Skill (CAS SHA-256 Verified)" },
                  { label: "kill_switch", description: "Engage or Release Global / Per-Skill Kill-Switch" },
                  { label: "audit", description: "Verify Hash-Chained Execution Audit Trail" },
                ],
                { placeHolder: "Select Stage 4 Executable Skill action" }
              )
            )?.label;
          if (!action) return;

          const fetchExecutableSkillsCatalog = async () => {
            try {
              const listRes = await apiRequest("GET", "/v1/skills?include_inactive=1", null, {
                useOwnerToken: true,
              });
              if (listRes.ok && Array.isArray(listRes.data?.skills)) {
                return listRes.data.skills;
              }
            } catch (_skListErr) {
              // Fall back to empty catalog
            }
            return [];
          };

          if (action === "list") {
            const res = await apiRequest("GET", "/v1/skills?include_inactive=1", null, {
              useOwnerToken: true,
            });
            if (!res.ok) {
              vscode.window.showErrorMessage(`Skills query failed: ${res.data?.error || res.status}`);
              return;
            }
            const ks = res.data?.kill_switch?.global_engaged ? "KILL-SWITCH ENGAGED" : "ARMED";
            vscode.window.showInformationMessage(
              `Executable Skills: ${res.data?.count ?? 0} (${ks})`
            );
            return res.data;
          }

          if (action === "promote") {
            let candidates = [];
            try {
              const candRes = await apiRequest("GET", "/v1/skills/candidates", null, {
                useOwnerToken: true,
              });
              if (candRes.ok && Array.isArray(candRes.data?.candidates)) {
                candidates = candRes.data.candidates;
              }
            } catch (_candErr) {
              candidates = [];
            }

            let selectedCand = null;
            let playbookId = opts.playbook_id || "";
            if (!playbookId && candidates.length > 0) {
              const pickedCand = await vscode.window.showQuickPick(
                candidates.map((c) => ({
                  label: String(c.playbook_id || ""),
                  description: `${Array.isArray(c.steps) ? c.steps.length : 1} step(s) · ${c.eligible_without_override ? "eligible" : "override"}`,
                  detail: String(c.title || ""),
                  candidate: c,
                })),
                { placeHolder: "Select backing active Playbook to promote to Executable Skill" }
              );
              if (!pickedCand) return;
              selectedCand = pickedCand.candidate;
              playbookId = pickedCand.label;
            }
            if (!playbookId) {
              playbookId = await vscode.window.showInputBox({
                prompt: "Backing Active Playbook ID",
              });
            }
            if (!playbookId) return;

            if (!selectedCand) {
              selectedCand =
                candidates.find(
                  (c) =>
                    String(c.playbook_id || "") === playbookId.trim() ||
                    String(c.filename || "") === playbookId.trim()
                ) || null;
            }
            let playbookSteps = Array.isArray(selectedCand?.steps) ? selectedCand.steps : [];
            let expectedSha =
              opts.expected_playbook_sha256 ||
              String(selectedCand?.playbook_sha256 || selectedCand?.sha256 || "");

            if (!playbookSteps.length || !expectedSha) {
              try {
                const pbRes = await apiRequest("GET", "/v1/playbooks?limit=200", null, {
                  useOwnerToken: true,
                });
                const pbItems = Array.isArray(pbRes.data?.playbooks)
                  ? pbRes.data.playbooks
                  : Array.isArray(pbRes.data?.items)
                    ? pbRes.data.items
                    : [];
                const matchedPb = pbItems.find(
                  (p) =>
                    String(p.playbook_id || "") === playbookId.trim() ||
                    String(p.filename || "").replace(/\.md$/i, "") === playbookId.trim()
                );
                if (matchedPb) {
                  if (!expectedSha && matchedPb.sha256) {
                    expectedSha = String(matchedPb.sha256);
                  }
                  if (!playbookSteps.length && Array.isArray(matchedPb.steps)) {
                    playbookSteps = matchedPb.steps;
                  }
                }
              } catch (_pbErr) {
                // Fall back to explicit owner input below
              }
            }

            const skillId =
              opts.skill_id ||
              (await vscode.window.showInputBox({
                prompt: "Executable Skill ID (slug)",
                value: `${playbookId.trim().replace(/[^a-z0-9_-]+/gi, "-").toLowerCase()}-skill`,
              }));
            if (!skillId) return;

            if (!expectedSha) {
              expectedSha = await vscode.window.showInputBox({
                prompt: "Expected Playbook SHA-256",
              });
            }
            if (!expectedSha) return;

            let stepActions = Array.isArray(opts.step_actions) ? opts.step_actions : null;
            if (!stepActions) {
              const stepCount = Math.max(1, playbookSteps.length || 1);
              const modePick = await vscode.window.showQuickPick(
                [
                  {
                    label: "auto",
                    description: `Auto-map all ${stepCount} step(s) as deterministic read-only vault_audit actions`,
                  },
                  {
                    label: "interactive",
                    description: `Configure each of the ${stepCount} step action(s) individually`,
                  },
                  {
                    label: "json",
                    description: `Paste custom step_actions JSON array (${stepCount} step(s))`,
                  },
                ],
                { placeHolder: `Configure ${stepCount} step action(s) for ${playbookId.trim()}` }
              );
              if (!modePick) return;

              if (modePick.label === "auto") {
                const auditKinds = ["sources", "ledger", "contradictions"];
                stepActions = Array.from({ length: stepCount }, (_, idx) => ({
                  action_type: "vault_audit",
                  audit_kind: auditKinds[idx % auditKinds.length],
                  output_file: `step_${String(idx + 1).padStart(2, "0")}_audit.json`,
                }));
              } else if (modePick.label === "json") {
                const rawJson = await vscode.window.showInputBox({
                  prompt: `Paste JSON array with exactly ${stepCount} step action(s)`,
                  value: JSON.stringify(
                    Array.from({ length: stepCount }, (_, idx) => ({
                      action_type: "vault_audit",
                      audit_kind: "sources",
                      output_file: `step_${String(idx + 1).padStart(2, "0")}_audit.json`,
                    }))
                  ),
                });
                if (!rawJson) return;
                try {
                  const parsed = JSON.parse(rawJson);
                  if (!Array.isArray(parsed) || parsed.length !== stepCount) {
                    vscode.window.showErrorMessage(
                      `step_actions JSON must be an array of exactly ${stepCount} step(s).`
                    );
                    return;
                  }
                  stepActions = parsed;
                } catch (parseErr) {
                  vscode.window.showErrorMessage(`Invalid step_actions JSON: ${parseErr.message}`);
                  return;
                }
              } else {
                stepActions = [];
                for (let idx = 0; idx < stepCount; idx += 1) {
                  const stepLabel = playbookSteps[idx]
                    ? `Step ${idx + 1}/${stepCount}: ${String(playbookSteps[idx]).slice(0, 80)}`
                    : `Step ${idx + 1}/${stepCount}`;
                  const actPick = await vscode.window.showQuickPick(
                    [
                      { label: "vault_audit", description: "Read-only sources / ledger / contradictions audit" },
                      { label: "vault_recall", description: "Grounded vault recall saved to JSON" },
                      { label: "workspace_write", description: "Write deterministic text artifact in sandbox" },
                      { label: "workspace_assert", description: "Assert file exists / non_empty / contains / json_key" },
                      { label: "sandbox_python", description: "Execute isolated sandbox Python script" },
                    ],
                    { placeHolder: `Action type for ${stepLabel}` }
                  );
                  if (!actPick) return;
                  const outDefault = `step_${String(idx + 1).padStart(2, "0")}_out.json`;
                  if (actPick.label === "vault_audit") {
                    const kindPick = await vscode.window.showQuickPick(
                      ["sources", "ledger", "contradictions"],
                      { placeHolder: `Audit kind for ${stepLabel}` }
                    );
                    if (!kindPick) return;
                    stepActions.push({
                      action_type: "vault_audit",
                      audit_kind: kindPick,
                      output_file: `step_${String(idx + 1).padStart(2, "0")}_${kindPick}.json`,
                    });
                  } else if (actPick.label === "vault_recall") {
                    const qText = await vscode.window.showInputBox({
                      prompt: `Vault recall query for ${stepLabel}`,
                      value: String(playbookSteps[idx] || "verified runbook"),
                    });
                    if (!qText) return;
                    stepActions.push({
                      action_type: "vault_recall",
                      query: qText.trim(),
                      n_results: 5,
                      output_file: outDefault,
                    });
                  } else if (actPick.label === "workspace_write") {
                    const relP = await vscode.window.showInputBox({
                      prompt: `Relative workspace file path for ${stepLabel}`,
                      value: `step_${String(idx + 1).padStart(2, "0")}.txt`,
                    });
                    if (!relP) return;
                    const bodyTxt = await vscode.window.showInputBox({
                      prompt: `Content for ${relP.trim()}`,
                      value: String(playbookSteps[idx] || "ok"),
                    });
                    if (bodyTxt === undefined) return;
                    stepActions.push({
                      action_type: "workspace_write",
                      rel_path: relP.trim(),
                      content: bodyTxt,
                    });
                  } else if (actPick.label === "workspace_assert") {
                    const relP = await vscode.window.showInputBox({
                      prompt: `Relative workspace file path to assert for ${stepLabel}`,
                      value: "step_01_sources.json",
                    });
                    if (!relP) return;
                    const opPick = await vscode.window.showQuickPick(
                      ["exists", "non_empty", "contains", "sha256_equals", "json_key_equals"],
                      { placeHolder: `Assertion operator for ${relP.trim()}` }
                    );
                    if (!opPick) return;
                    const assertAction = {
                      action_type: "workspace_assert",
                      rel_path: relP.trim(),
                      op: opPick,
                    };
                    if (opPick === "json_key_equals") {
                      const jKey = await vscode.window.showInputBox({ prompt: "Dotted JSON key path" });
                      if (!jKey) return;
                      assertAction.json_key = jKey.trim();
                    }
                    if (opPick === "contains" || opPick === "sha256_equals" || opPick === "json_key_equals") {
                      const expVal = await vscode.window.showInputBox({ prompt: `Expected value for ${opPick}` });
                      if (expVal === undefined) return;
                      assertAction.expected = expVal;
                    }
                    stepActions.push(assertAction);
                  } else {
                    const pyBody = await vscode.window.showInputBox({
                      prompt: `Sandboxed Python script body for ${stepLabel}`,
                      value: `import json\nfrom pathlib import Path\nPath(${JSON.stringify(outDefault)}).write_text(json.dumps({"ok": True}), encoding="utf-8")\n`,
                    });
                    if (!pyBody) return;
                    stepActions.push({
                      action_type: "sandbox_python",
                      script_body: pyBody,
                      output_file: outDefault,
                    });
                  }
                }
              }
            }

            const rationale =
              opts.rationale !== undefined
                ? String(opts.rationale)
                : await vscode.window.showInputBox({
                    prompt: "Owner promotion rationale (recorded in decision ledger)",
                    value: "Promoted from VS Code",
                  });
            if (rationale === undefined) return;

            let inputSchema = opts.input_schema;
            if (inputSchema === undefined && !Array.isArray(opts.step_actions)) {
              const rawSchemaJson = await vscode.window.showInputBox({
                prompt:
                  'Optional runtime input_schema JSON (e.g. {"env":{"type":"string","required":true}}; leave blank for none)',
                value: "",
              });
              if (rawSchemaJson === undefined) return;
              if (rawSchemaJson.trim()) {
                try {
                  const parsedSchema = JSON.parse(rawSchemaJson);
                  if (!parsedSchema || typeof parsedSchema !== "object" || Array.isArray(parsedSchema)) {
                    vscode.window.showErrorMessage("input_schema must be a JSON object.");
                    return;
                  }
                  inputSchema = parsedSchema;
                } catch (parseErr) {
                  vscode.window.showErrorMessage(`Invalid input_schema JSON: ${parseErr.message}`);
                  return;
                }
              }
            }

            const promoteBody = {
              skill_id: skillId.trim(),
              playbook_id: playbookId.trim(),
              expected_playbook_sha256: expectedSha.trim(),
              step_actions: stepActions,
              actor: opts.actor || "vscode_owner",
              rationale: rationale.trim() || "Promoted from VS Code",
              allow_early_owner_override:
                opts.allow_early_owner_override !== undefined
                  ? Boolean(opts.allow_early_owner_override)
                  : true,
            };
            if (inputSchema && typeof inputSchema === "object" && !Array.isArray(inputSchema)) {
              promoteBody.input_schema = inputSchema;
            }

            const res = await apiRequest(
              "POST",
              "/v1/skills/promote",
              promoteBody,
              { useOwnerToken: true }
            );
            if (!res.ok) {
              vscode.window.showErrorMessage(`Skill promotion failed: ${res.data?.error || res.status}`);
              return;
            }
            vscode.window.showInformationMessage(`Promoted Executable Skill: ${res.data?.skill_id}`);
            return res.data;
          }

          if (action === "execute" || action === "dry_run") {
            const catalogSkills = await fetchExecutableSkillsCatalog();

            let skillId = opts.skill_id ? String(opts.skill_id).trim() : "";
            if (!skillId) {
              const activeSkills = catalogSkills.filter(
                (s) => s && s.valid !== false && s.status !== "revoked"
              );
              if (activeSkills.length > 0) {
                const pickedSk = await vscode.window.showQuickPick(
                  activeSkills.map((s) => ({
                    label: String(s.skill_id || ""),
                    description: `${s.status || "active"} · ${
                      Object.keys(s.input_schema || {}).length
                    } input(s)`,
                    detail: String(s.title || s.playbook_id || ""),
                  })),
                  { placeHolder: "Select Executable Skill to run" }
                );
                if (!pickedSk) return;
                skillId = pickedSk.label.trim();
              } else {
                const rawSkId = await vscode.window.showInputBox({
                  prompt: "Executable Skill ID to run",
                });
                if (!rawSkId) return;
                skillId = rawSkId.trim();
              }
            }

            const matchedSkill =
              catalogSkills.find((s) => String(s.skill_id || "") === skillId) || null;
            const schema =
              matchedSkill &&
              matchedSkill.input_schema &&
              typeof matchedSkill.input_schema === "object"
                ? matchedSkill.input_schema
                : {};

            let runtimeInputs =
              opts.inputs && typeof opts.inputs === "object" && !Array.isArray(opts.inputs)
                ? { ...opts.inputs }
                : null;
            if (!runtimeInputs) {
              runtimeInputs = {};
              const paramEntries = Object.entries(schema);
              for (const [paramName, rawSpec] of paramEntries) {
                const spec = rawSpec && typeof rawSpec === "object" ? rawSpec : {};
                const pType = String(spec.type || "string").toLowerCase();
                const reqLabel = spec.required ? "required" : "optional";
                const descLabel = spec.description ? ` — ${spec.description}` : "";
                const defaultStr = spec.default !== undefined ? String(spec.default) : "";
                const rawVal = await vscode.window.showInputBox({
                  prompt: `Skill input '${paramName}' (${pType}, ${reqLabel})${descLabel}`,
                  value: defaultStr,
                });
                if (rawVal === undefined) return;
                const trimmed = rawVal.trim();
                if (!trimmed && !spec.required) {
                  continue;
                }
                if (pType === "boolean") {
                  if (["true", "1", "yes", "on"].includes(trimmed.toLowerCase())) {
                    runtimeInputs[paramName] = true;
                  } else if (["false", "0", "no", "off"].includes(trimmed.toLowerCase())) {
                    runtimeInputs[paramName] = false;
                  } else {
                    vscode.window.showErrorMessage(
                      `Invalid boolean for '${paramName}': ${rawVal}`
                    );
                    return;
                  }
                } else if (pType === "integer") {
                  const n = Number.parseInt(trimmed, 10);
                  if (!Number.isFinite(n) || !/^-?\d+$/.test(trimmed)) {
                    vscode.window.showErrorMessage(
                      `Invalid integer for '${paramName}': ${rawVal}`
                    );
                    return;
                  }
                  runtimeInputs[paramName] = n;
                } else if (pType === "number") {
                  const n = Number(trimmed);
                  if (!trimmed || !Number.isFinite(n)) {
                    vscode.window.showErrorMessage(
                      `Invalid number for '${paramName}': ${rawVal}`
                    );
                    return;
                  }
                  runtimeInputs[paramName] = n;
                } else {
                  runtimeInputs[paramName] = rawVal;
                }
              }
            }

            const res = await apiRequest(
              "POST",
              `/v1/skills/${encodeURIComponent(skillId)}/execute`,
              {
                actor: opts.actor || "vscode_owner",
                dry_run: action === "dry_run" || Boolean(opts.dry_run),
                inputs: runtimeInputs,
              },
              { useOwnerToken: true }
            );
            if (!res.ok) {
              vscode.window.showErrorMessage(`Skill execution failed: ${res.data?.error || res.status}`);
              return;
            }
            vscode.window.showInformationMessage(
              `Skill ${skillId}: ${res.data?.status} (audit ${String(res.data?.execution_sha256 || "").slice(0, 12)})`
            );
            return res.data;
          }

          if (action === "revoke") {
            const catalogSkills = await fetchExecutableSkillsCatalog();

            let skillId = opts.skill_id ? String(opts.skill_id).trim() : "";
            if (!skillId) {
              const revokable = catalogSkills.filter((s) => s && s.status !== "revoked");
              if (!revokable.length) {
                vscode.window.showInformationMessage("No active Executable Skills to revoke.");
                return;
              }
              const pickedSk = await vscode.window.showQuickPick(
                revokable.map((s) => ({
                  label: String(s.skill_id || ""),
                  description: `${s.status || "active"} · sha256 ${String(s.sha256 || "").slice(0, 12)}`,
                  detail: String(s.title || s.playbook_id || ""),
                })),
                { placeHolder: "Select Executable Skill to revoke" }
              );
              if (!pickedSk) return;
              skillId = pickedSk.label.trim();
            }

            const matchedSkill =
              catalogSkills.find((s) => String(s.skill_id || "") === skillId) || null;
            let expectedSha =
              opts.expected_skill_sha256 || String(matchedSkill?.sha256 || "");
            if (!expectedSha) {
              const rawSha = await vscode.window.showInputBox({
                prompt: `Expected SHA-256 of skill '${skillId}' for CAS revocation`,
              });
              if (!rawSha) return;
              expectedSha = rawSha.trim();
            }
            const reason =
              opts.reason !== undefined
                ? String(opts.reason)
                : await vscode.window.showInputBox({
                    prompt: "Revocation reason (written to audit trail)",
                    value: "Revoked by owner from VS Code",
                  });
            if (reason === undefined || !reason.trim()) return;

            const res = await apiRequest(
              "POST",
              `/v1/skills/${encodeURIComponent(skillId)}/revoke`,
              {
                expected_skill_sha256: expectedSha,
                actor: opts.actor || "vscode_owner",
                reason: reason.trim(),
              },
              { useOwnerToken: true }
            );
            if (!res.ok) {
              vscode.window.showErrorMessage(`Skill revocation failed: ${res.data?.error || res.status}`);
              return;
            }
            vscode.window.showInformationMessage(`Revoked Executable Skill: ${skillId}`);
            return res.data;
          }

          if (action === "kill_switch") {
            let scope = opts.scope ? String(opts.scope).trim() : "";
            if (!scope) {
              const scopeChoices = [{ label: "global", description: "Global kill-switch across all Executable Skills" }];
              const catalogSkills = await fetchExecutableSkillsCatalog();
              for (const sk of catalogSkills) {
                if (sk && sk.skill_id) {
                  scopeChoices.push({
                    label: String(sk.skill_id),
                    description: `Per-skill kill-switch (${sk.status || "active"})`,
                  });
                }
              }
              const pickedScope = await vscode.window.showQuickPick(scopeChoices, {
                placeHolder: "Select kill-switch scope (global or specific skill)",
              });
              if (!pickedScope) return;
              scope = pickedScope.label;
            }
            let engaged;
            if (opts.engaged !== undefined) {
              engaged = Boolean(opts.engaged);
            } else {
              const pickedChoice = await vscode.window.showQuickPick(["engage", "release"], {
                placeHolder: `Engage or release Executable Skill Kill-Switch (${scope})?`,
              });
              if (!pickedChoice) return;
              engaged = pickedChoice === "engage";
            }
            const res = await apiRequest(
              "POST",
              "/v1/skills/kill-switch",
              {
                engaged,
                scope,
                actor: opts.actor || "vscode_owner",
                reason: opts.reason || "VS Code owner kill-switch toggle",
              },
              { useOwnerToken: true }
            );
            if (!res.ok) {
              vscode.window.showErrorMessage(`Kill-switch update failed: ${res.data?.error || res.status}`);
              return;
            }
            vscode.window.showInformationMessage(
              `Skill Kill-Switch (${scope}): engaged=${engaged}`
            );
            return res.data;
          }

          if (action === "audit") {
            const res = await apiRequest("GET", "/v1/skills/executions?limit=25", null, {
              useOwnerToken: true,
            });
            if (!res.ok) {
              vscode.window.showErrorMessage(`Audit check failed: ${res.data?.error || res.status}`);
              return;
            }
            const chain = res.data?.audit_chain || {};
            vscode.window.showInformationMessage(
              `Skill Audit Chain: ${chain.chain_valid ? "VALID" : "INVALID"} (${chain.entry_count ?? 0} entries)`
            );
            return res.data;
          }
        } catch (err) {
          vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
        }
      }
    ),

    // ── Full On-Device Mobile Vault Client (iOS / Android) ──
    vscode.commands.registerCommand("secondBrain.mobileVaultClient", async (arg) => {
      try {
        const opts = arg && typeof arg === "object" ? arg : {};
        const action =
          opts.action ||
          (
            await vscode.window.showQuickPick(
              [
                { label: "pair", description: "Pair iOS / Android Mobile Vault Client (#vaultpair=)" },
                { label: "export", description: "Export Standalone Offline Mobile Vault App Bundle (.zip)" },
                { label: "status", description: "List Paired Mobile Vault Clients" },
                { label: "revoke", description: "Revoke a Paired Mobile Vault Client" },
              ],
              { placeHolder: "Select Mobile Vault Client action" }
            )
          )?.label;
        if (!action) return;

        if (action === "pair") {
          const label =
            opts.label ||
            (await vscode.window.showInputBox({
              prompt: "Mobile device label",
              value: "iOS / Android Vault App",
            }));
          if (!label) return;
          const res = await apiRequest(
            "POST",
            "/v1/mobile/pair",
            {
              label: label.trim(),
              include_recovery_key: Boolean(opts.include_recovery_key),
              include_inbox_drop_pairing:
                opts.include_inbox_drop_pairing !== undefined
                  ? Boolean(opts.include_inbox_drop_pairing)
                  : true,
            },
            { useOwnerToken: true }
          );
          if (!res.ok) {
            vscode.window.showErrorMessage(`Mobile pairing failed: ${res.data?.error || res.status}`);
            return;
          }
          const shareUri = String(
            res.data?.pwa_pairing_url || res.data?.pairing_url || res.data?.pairing_fragment || ""
          ).trim();
          let copied = false;
          if (shareUri && vscode.env && vscode.env.clipboard && typeof vscode.env.clipboard.writeText === "function") {
            try {
              await vscode.env.clipboard.writeText(shareUri);
              copied = true;
            } catch (_clipErr) {
              copied = false;
            }
          }
          const msgActions = shareUri ? ["Show Pairing URI", "Copy Pairing URI"] : [];
          const pickedMsg = await vscode.window.showInformationMessage(
            `Paired Mobile Vault Client: ${res.data?.pairing_id}${copied ? " (#vaultpair= URI copied to clipboard)" : ""}`,
            ...msgActions
          );
          if (pickedMsg === "Copy Pairing URI" && shareUri && vscode.env?.clipboard?.writeText) {
            await vscode.env.clipboard.writeText(shareUri);
          } else if (pickedMsg === "Show Pairing URI" && shareUri) {
            const doc = await vscode.workspace.openTextDocument({
              language: "markdown",
              content: [
                `# Mobile Vault Client Pairing: ${res.data?.pairing_id || ""}`,
                "",
                "Open this zero-knowledge `#vaultpair=` URI on your phone (never sent to any server):",
                "",
                "```text",
                shareUri,
                "```",
                "",
              ].join("\n"),
            });
            await vscode.window.showTextDocument(doc, { preview: true });
          }
          return res.data;
        }

        if (action === "export") {
          const res = await apiRequest(
            "POST",
            "/v1/mobile/export",
            { out_dir: opts.out_dir || "" },
            { useOwnerToken: true }
          );
          if (!res.ok) {
            vscode.window.showErrorMessage(`Mobile bundle export failed: ${res.data?.error || res.status}`);
            return;
          }
          vscode.window.showInformationMessage(`Exported Mobile Vault Client: ${res.data?.zip_path}`);
          return res.data;
        }

        if (action === "status") {
          const res = await apiRequest("GET", "/v1/mobile/status", null, { useOwnerToken: true });
          if (!res.ok) {
            vscode.window.showErrorMessage(`Mobile status failed: ${res.data?.error || res.status}`);
            return;
          }
          vscode.window.showInformationMessage(
            `Paired Mobile Vault Clients: ${(res.data?.pairings || []).length}`
          );
          return res.data;
        }

        if (action === "revoke") {
          let pairingId = opts.pairing_id ? String(opts.pairing_id).trim() : "";
          if (!pairingId) {
            const listRes = await apiRequest("GET", "/v1/mobile/status", null, {
              useOwnerToken: true,
            });
            if (!listRes.ok) {
              vscode.window.showErrorMessage(
                `Mobile status failed: ${listRes.data?.error || listRes.status}`
              );
              return;
            }
            const active = (listRes.data?.pairings || []).filter((p) => p && !p.revoked);
            if (!active.length) {
              vscode.window.showInformationMessage("No active Mobile Vault Client pairings to revoke.");
              return;
            }
            const picked = await vscode.window.showQuickPick(
              active.map((p) => ({
                label: String(p.label || p.pairing_id),
                description: String(p.pairing_id || ""),
                detail: `Created ${p.created_at || ""}`,
                pairingId: String(p.pairing_id || ""),
              })),
              { placeHolder: "Select Mobile Vault Client pairing to revoke" }
            );
            if (!picked) return;
            pairingId = picked.pairingId;
          }
          const revRes = await apiRequest(
            "POST",
            "/v1/mobile/revoke",
            { pairing_id: pairingId },
            { useOwnerToken: true }
          );
          if (!revRes.ok) {
            vscode.window.showErrorMessage(
              `Mobile Vault Client revoke failed: ${revRes.data?.error || revRes.status}`
            );
            return;
          }
          vscode.window.showInformationMessage(
            `Revoked Mobile Vault Client pairing: ${pairingId}`
          );
          return revRes.data;
        }
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    // ── Local Creative Media Studio (Offline Image & Video Generation) ──
    vscode.commands.registerCommand("secondBrain.generateCreativeImage", async (arg) => {
      try {
        const opts = arg && typeof arg === "object" ? arg : {};
        const prompt =
          opts.prompt ||
          (await vscode.window.showInputBox({
            prompt: "Offline Image Generation Prompt (local-only, DLP-gated)",
          }));
        if (!prompt || !prompt.trim()) return;
        const res = await apiRequest(
          "POST",
          "/v1/media/image",
          {
            prompt: prompt.trim(),
            style: opts.style || "concept_card",
            backend: opts.backend || "auto",
            output_format: opts.output_format || "png",
            ground_in_vault: Boolean(opts.ground_in_vault),
            project_scope: opts.project_scope || undefined,
          },
          { useOwnerToken: true }
        );
        if (!res.ok) {
          vscode.window.showErrorMessage(`Image generation failed: ${res.data?.error || res.status}`);
          return;
        }
        vscode.window.showInformationMessage(
          `Generated offline image: ${res.data?.filename} (${res.data?.backend_used})`
        );
        return res.data;
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.generateCreativeVideo", async (arg) => {
      try {
        const opts = arg && typeof arg === "object" ? arg : {};
        const prompt =
          opts.prompt ||
          (await vscode.window.showInputBox({
            prompt: "Offline Video Generation Prompt (.mp4 / .gif, local-only, DLP-gated)",
          }));
        if (!prompt || !prompt.trim()) return;
        const res = await apiRequest(
          "POST",
          "/v1/media/video",
          {
            prompt: prompt.trim(),
            style: opts.style || "concept_card",
            backend: opts.backend || "auto",
            output_format: opts.output_format || "mp4",
            frames: opts.frames || 16,
            fps: opts.fps || 12,
            ground_in_vault: Boolean(opts.ground_in_vault),
            project_scope: opts.project_scope || undefined,
          },
          { useOwnerToken: true }
        );
        if (!res.ok) {
          vscode.window.showErrorMessage(`Video generation failed: ${res.data?.error || res.status}`);
          return;
        }
        vscode.window.showInformationMessage(
          `Generated offline video: ${res.data?.filename} (${res.data?.backend_used})`
        );
        return res.data;
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    // ── Bulk Import (Folder or Single File into Quarantined Inbox) ──
    vscode.commands.registerCommand("secondBrain.bulkImport", async (arg) => {
      try {
        const opts = arg && typeof arg === "object" ? arg : {};
        let action = opts.action || "";
        if (!action) {
          const picked = await vscode.window.showQuickPick(
            [
              {
                label: "folder",
                description: "Import local Markdown/note folder into Quarantined Inbox",
              },
              {
                label: "file",
                description: "Import a single local file (.md, .txt, .json) into Quarantined Inbox",
              },
            ],
            { placeHolder: "Select Second Brain Bulk Import mode" }
          );
          if (!picked) return;
          action = picked.label;
        }

        if (action === "file") {
          const defaultFile =
            opts.path ||
            (vscode.window.activeTextEditor?.document?.uri?.scheme === "file"
              ? vscode.window.activeTextEditor.document.uri.fsPath
              : "");
          const filePath =
            opts.path ||
            (await vscode.window.showInputBox({
              prompt: "Absolute local file path to import into 0-raw/inbox/",
              value: defaultFile,
            }));
          if (!filePath || !filePath.trim()) return;
          const tags =
            opts.tags !== undefined
              ? parseListInput(opts.tags)
              : parseListInput(
                  await vscode.window.showInputBox({
                    prompt: "Optional comma-separated tags for imported note",
                    value: "",
                  })
                );
          const res = await apiRequest(
            "POST",
            "/v1/import/file",
            { path: filePath.trim(), tags },
            { useOwnerToken: true }
          );
          if (!res.ok) {
            vscode.window.showErrorMessage(`File import failed: ${res.data?.error || res.status}`);
            return;
          }
          treeProvider.refresh();
          vscode.window.showInformationMessage(
            `Imported file to Quarantined Inbox: ${res.data?.destination || res.data?.filename || filePath.trim()}`
          );
          return res.data;
        }

        const sourceDir =
          opts.source_dir ||
          (await vscode.window.showInputBox({
            prompt: "Local folder path containing notes/transcripts to import",
          }));
        if (!sourceDir || !sourceDir.trim()) return;
        const tags =
          opts.tags !== undefined
            ? parseListInput(opts.tags)
            : parseListInput(
                await vscode.window.showInputBox({
                  prompt: "Optional comma-separated tags for imported files",
                  value: "",
                })
              );
        const res = await apiRequest(
          "POST",
          "/v1/import",
          {
            source_dir: sourceDir.trim(),
            tags,
            dry_run: Boolean(opts.dry_run),
            overwrite_modified: Boolean(opts.overwrite_modified),
          },
          { useOwnerToken: true }
        );
        if (!res.ok) {
          vscode.window.showErrorMessage(`Bulk folder import failed: ${res.data?.error || res.status}`);
          return;
        }
        treeProvider.refresh();
        vscode.window.showInformationMessage(
          `Bulk import complete: ${res.data?.imported ?? 0} imported, ${
            res.data?.skipped ?? 0
          } skipped.`
        );
        return res.data;
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    // ── Active Web Scout (Queue & Discovery) ──
    vscode.commands.registerCommand("secondBrain.manageScout", async (arg) => {
      try {
        const opts = arg && typeof arg === "object" ? arg : {};
        const action =
          opts.action ||
          (
            await vscode.window.showQuickPick(
              [
                { label: "list", description: "Inspect Web Scout reading queue" },
                { label: "enqueue", description: "Queue a URL or RSS/Atom feed for Web Scout" },
                { label: "run", description: "Start Web Scout background discovery job" },
              ],
              { placeHolder: "Select Active Web Scout action" }
            )
          )?.label;
        if (!action) return;

        if (action === "list") {
          const res = await apiRequest("GET", "/v1/scout/queue", null, { useOwnerToken: true });
          if (!res.ok) {
            vscode.window.showErrorMessage(`Scout queue failed: ${res.data?.error || res.status}`);
            return;
          }
          const items = Array.isArray(res.data?.items) ? res.data.items : [];
          const lines = ["# Active Web Scout Reading Queue", "", `Total entries: ${res.data?.total ?? items.length}`, ""];
          for (const it of items) {
            lines.push(`- [${it.status || "queued"}] (${it.kind || "url"}) ${it.url}`);
          }
          if (!items.length) lines.push("_Queue is empty._");
          await openMarkdownPreview(lines.join("\n"));
          return res.data;
        }

        if (action === "enqueue") {
          const url =
            opts.url ||
            (await vscode.window.showInputBox({
              prompt: "HTTPS/HTTP URL to add to Web Scout queue",
              placeHolder: "https://example.com/article",
            }));
          if (!url || !url.trim()) return;
          let isFeed = Boolean(opts.is_feed);
          if (opts.is_feed === undefined) {
            const kindPick = await vscode.window.showQuickPick(["url", "feed"], {
              placeHolder: "Entry type (single article URL or RSS/Atom feed)",
            });
            if (!kindPick) return;
            isFeed = kindPick === "feed";
          }
          const res = await apiRequest(
            "POST",
            "/v1/scout/queue",
            { url: url.trim(), is_feed: isFeed },
            { useOwnerToken: true }
          );
          if (!res.ok) {
            vscode.window.showErrorMessage(`Scout enqueue failed: ${res.data?.error || res.status}`);
            return;
          }
          vscode.window.showInformationMessage(
            `Queued ${isFeed ? "feed" : "URL"} for Web Scout: ${res.data?.url || url.trim()}`
          );
          return res.data;
        }

        if (action === "run") {
          const res = await apiRequest("POST", "/v1/scout/run", {}, { useOwnerToken: true });
          if (!res.ok) {
            vscode.window.showErrorMessage(`Scout run failed: ${res.data?.error || res.status}`);
            return;
          }
          vscode.window.showInformationMessage(
            `Started Web Scout job ${res.data?.job_id || ""} (${res.data?.status || "queued"}).`
          );
          return res.data;
        }
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    // ── Background Maintenance Jobs (Refinery / Editor / Doctor) ──
    vscode.commands.registerCommand("secondBrain.runOperatorJob", async (arg) => {
      try {
        const opts = arg && typeof arg === "object" ? arg : {};
        const kind =
          opts.kind ||
          (
            await vscode.window.showQuickPick(
              [
                { label: "refinery", description: "Run Knowledge Refinery (extract & link notes)" },
                { label: "editor", description: "Run Memory Editor / Consolidation job" },
                { label: "doctor", description: "Run Vault Health & Diagnostics (Doctor)" },
                { label: "status", description: "Inspect background job status by job_id" },
              ],
              { placeHolder: "Select Second Brain operator maintenance job" }
            )
          )?.label;
        if (!kind) return;

        if (kind === "status") {
          const jobId =
            opts.job_id ||
            (await vscode.window.showInputBox({ prompt: "Operator job_id to inspect" }));
          if (!jobId || !jobId.trim()) return;
          const res = await apiRequest(
            "GET",
            `/v1/jobs/${encodeURIComponent(jobId.trim())}`,
            null,
            { useOwnerToken: true }
          );
          if (!res.ok) {
            vscode.window.showErrorMessage(`Job lookup failed: ${res.data?.error || res.status}`);
            return;
          }
          await openMarkdownPreview(
            [
              `# Operator Job: ${res.data?.job_id || jobId.trim()}`,
              "",
              `- **Kind:** ${res.data?.kind || "unknown"}`,
              `- **Status:** ${res.data?.status || "unknown"}`,
              `- **Exit Code:** ${res.data?.returncode ?? "-"}`,
              "",
              "```json",
              JSON.stringify(res.data, null, 2),
              "```",
            ].join("\n")
          );
          return res.data;
        }

        if (!["refinery", "editor", "doctor"].includes(kind)) {
          vscode.window.showErrorMessage(`Unsupported operator job kind: ${kind}`);
          return;
        }
        const res = await apiRequest("POST", `/v1/jobs/${kind}`, {}, { useOwnerToken: true });
        if (!res.ok) {
          vscode.window.showErrorMessage(`Job start failed: ${res.data?.error || res.status}`);
          return;
        }
        vscode.window.showInformationMessage(
          `Started ${kind} job ${res.data?.job_id || ""} (${res.data?.status || "running"}).`
        );
        return res.data;
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    // ── Interactive Knowledge Graph Webview w/ Bi-Temporal Timeline Scrubber ──
    vscode.commands.registerCommand("secondBrain.openKnowledgeGraph", async (arg) => {
      try {
        const opts = arg && typeof arg === "object" ? arg : {};
        const buildGraphQueryPath = (filters) => {
          const qs = new URLSearchParams();
          qs.set("limit", String(opts.limit || 300));
          qs.set("include_pending", filters.include_pending ? "1" : "0");
          if (filters.include_expired !== undefined) {
            qs.set("include_expired", filters.include_expired ? "1" : "0");
          }
          if (filters.as_of) {
            qs.set("as_of", String(filters.as_of).trim());
          }
          if (filters.project) {
            qs.set("project", String(filters.project).trim());
          }
          return `/v1/graph/full?${qs.toString()}`;
        };

        const initialFilters = {
          as_of: opts.as_of ? String(opts.as_of).trim() : "",
          include_expired:
            opts.include_expired !== undefined
              ? Boolean(opts.include_expired)
              : !opts.as_of,
          include_pending: Boolean(opts.include_pending),
          project: opts.project ? String(opts.project).trim() : "",
        };

        const res = await apiRequest("GET", buildGraphQueryPath(initialFilters), null, {
          useOwnerToken: true,
        });
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Knowledge Graph fetch failed: ${res.data?.error || res.status}`
          );
          return;
        }

        const mergedInitial = {
          ...(res.data || {}),
          ...initialFilters,
          as_of: res.data?.as_of ?? (initialFilters.as_of || null),
          include_expired:
            typeof res.data?.include_expired === "boolean"
              ? res.data.include_expired
              : initialFilters.include_expired,
        };

        if (graphPanel) {
          graphPanel.reveal(vscode.ViewColumn.Beside);
          graphPanel.webview.postMessage({ type: "graphData", data: mergedInitial });
          return { panel: graphPanel, data: mergedInitial };
        }

        const nonce = crypto.randomBytes(16).toString("base64");
        const panel = vscode.window.createWebviewPanel(
          "secondBrainGraph",
          "Second Brain Knowledge Graph",
          vscode.ViewColumn.Beside,
          { enableScripts: true, retainContextWhenHidden: true }
        );
        graphPanel = panel;
        panel.webview.html = buildGraphWebviewHtml({ nonce, initialData: mergedInitial });

        if (typeof panel.onDidDispose === "function") {
          panel.onDidDispose(() => {
            if (graphPanel === panel) {
              graphPanel = null;
            }
          });
        }

        panel.webview.onDidReceiveMessage(async (msg) => {
          const postGraphError = (errText) => {
            try {
              panel.webview.postMessage({
                type: "graphError",
                error: String(errText || "Knowledge Graph request failed"),
              });
            } catch {}
          };
          try {
            if (!msg || typeof msg !== "object" || typeof msg.action !== "string") {
              return;
            }
            if (msg.action === "openNode") {
              const fname = String(msg.filename || "").trim();
              if (!isSafeNoteFilename(fname)) return;
              await vscode.commands.executeCommand("secondBrain.openTreeItem", {
                kind: "atom",
                filename: fname,
              });
              return;
            }
            if (msg.action === "autoLink") {
              const linkRes = await apiRequest(
                "POST",
                "/v1/graph/auto-link",
                {
                  actor: "vscode_owner",
                  project_scope: msg.project ? String(msg.project).trim() : undefined,
                  include_pending: Boolean(msg.include_pending),
                },
                { useOwnerToken: true }
              );
              if (!linkRes.ok) {
                const errMsg = `Auto-link failed: ${linkRes.data?.error || linkRes.status}`;
                postGraphError(errMsg);
                vscode.window.showErrorMessage(errMsg);
                return;
              }
              treeProvider.refresh();
            }
            if (msg.action === "reload" || msg.action === "autoLink") {
              const nextFilters = {
                as_of: msg.as_of ? String(msg.as_of).trim() : "",
                include_expired:
                  msg.include_expired !== undefined
                    ? Boolean(msg.include_expired)
                    : !msg.as_of,
                include_pending: Boolean(msg.include_pending),
                project: msg.project ? String(msg.project).trim() : "",
              };
              const reloadRes = await apiRequest(
                "GET",
                buildGraphQueryPath(nextFilters),
                null,
                { useOwnerToken: true }
              );
              if (!reloadRes.ok) {
                const errMsg = `Knowledge Graph reload failed: ${reloadRes.data?.error || reloadRes.status}`;
                postGraphError(errMsg);
                vscode.window.showErrorMessage(errMsg);
                return;
              }
              panel.webview.postMessage({
                type: "graphData",
                data: {
                  ...(reloadRes.data || {}),
                  ...nextFilters,
                  as_of: reloadRes.data?.as_of ?? (nextFilters.as_of || null),
                  include_expired:
                    typeof reloadRes.data?.include_expired === "boolean"
                      ? reloadRes.data.include_expired
                      : nextFilters.include_expired,
                },
              });
            }
          } catch (err) {
            const errMsg = `Knowledge Graph error: ${err?.message || String(err)}`;
            postGraphError(errMsg);
            vscode.window.showErrorMessage(errMsg);
          }
        });

        return { panel, data: mergedInitial };
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    // ── Multi-Vault Workspaces & Opt-In Federated Recall ──
    vscode.commands.registerCommand("secondBrain.manageWorkspaces", async (arg) => {
      try {
        const opts = arg && typeof arg === "object" ? arg : {};
        const action =
          opts.action ||
          (
            await vscode.window.showQuickPick(
              [
                { label: "list", description: "List registered Multi-Vault workspaces" },
                { label: "create", description: "Create & register a new isolated vault workspace" },
                { label: "switch", description: "Switch active vault workspace" },
                { label: "policy", description: "Update workspace label, host policy & federated search flag" },
                { label: "federated_recall", description: "Search across federated-searchable workspaces" },
                { label: "remove", description: "Unregister an inactive workspace (files preserved)" },
              ],
              { placeHolder: "Select Multi-Vault Workspace action" }
            )
          )?.label;
        if (!action) return;

        if (action === "list") {
          const res = await apiRequest("GET", "/v1/vaults", null, { useOwnerToken: true });
          if (!res.ok) {
            vscode.window.showErrorMessage(`List vaults failed: ${res.data?.error || res.status}`);
            return;
          }
          const vaults = Array.isArray(res.data?.vaults) ? res.data.vaults : [];
          const lines = [
            "# Second Brain Multi-Vault Workspaces",
            "",
            `Active Vault ID: \`${res.data?.active_vault_id || "-"}\``,
            "",
          ];
          for (const v of vaults) {
            lines.push(
              `- **${v.label || v.vault_id}** (\`${v.vault_id}\`) · policy=\`${
                v.default_host_policy || "local_only"
              }\` · federated=\`${Boolean(v.federated_searchable)}\` · path=\`${v.path}\``
            );
          }
          await openMarkdownPreview(lines.join("\n"));
          return res.data;
        }

        if (action === "create") {
          const label =
            opts.label ||
            (await vscode.window.showInputBox({
              prompt: "Workspace label (e.g. Research Vault)",
            }));
          if (!label || !label.trim()) return;
          const vpath =
            opts.path ||
            (await vscode.window.showInputBox({
              prompt: "Absolute path for new workspace directory",
            }));
          if (!vpath || !vpath.trim()) return;
          const hostPolicy =
            opts.default_host_policy ||
            (await vscode.window.showQuickPick(["local_only", "ask", "allow"], {
              placeHolder: "Default host sharing policy for this workspace",
            }));
          if (!hostPolicy) return;
          let fedSearch = Boolean(opts.federated_searchable);
          if (opts.federated_searchable === undefined) {
            const fedPick = await vscode.window.showQuickPick(["No (Isolated)", "Yes (Federated Searchable)"], {
              placeHolder: "Allow opt-in federated recall across this workspace?",
            });
            if (!fedPick) return;
            fedSearch = fedPick.startsWith("Yes");
          }
          const res = await apiRequest(
            "POST",
            "/v1/vaults",
            {
              label: label.trim(),
              path: vpath.trim(),
              vault_id: opts.vault_id || undefined,
              default_host_policy: hostPolicy,
              federated_searchable: fedSearch,
              isolated_token: Boolean(opts.isolated_token),
              activate: Boolean(opts.activate),
            },
            { useOwnerToken: true }
          );
          if (!res.ok) {
            vscode.window.showErrorMessage(`Create vault failed: ${res.data?.error || res.status}`);
            return;
          }
          if (opts.activate) treeProvider.refresh();
          vscode.window.showInformationMessage(`Created workspace '${res.data?.label}' (${res.data?.vault_id}).`);
          return res.data;
        }

        if (action === "switch" || action === "policy" || action === "remove") {
          let vaultId = opts.vault_id ? String(opts.vault_id).trim() : "";
          if (!vaultId) {
            const listRes = await apiRequest("GET", "/v1/vaults", null, { useOwnerToken: true });
            if (!listRes.ok) {
              vscode.window.showErrorMessage(`List vaults failed: ${listRes.data?.error || listRes.status}`);
              return;
            }
            const candidates = (listRes.data?.vaults || []).filter((v) =>
              action === "remove" ? v.vault_id !== listRes.data?.active_vault_id : true
            );
            if (!candidates.length) {
              vscode.window.showInformationMessage("No matching workspaces available.");
              return;
            }
            const picked = await vscode.window.showQuickPick(
              candidates.map((v) => ({
                label: String(v.vault_id || ""),
                description: `${v.label || ""} · policy=${v.default_host_policy || "local_only"} · federated=${Boolean(
                  v.federated_searchable
                )}`,
                detail: String(v.path || ""),
              })),
              { placeHolder: `Select workspace for '${action}'` }
            );
            if (!picked) return;
            vaultId = picked.label;
          }

          if (action === "switch") {
            const res = await apiRequest(
              "POST",
              "/v1/vaults/switch",
              { vault_id: vaultId },
              { useOwnerToken: true }
            );
            if (!res.ok) {
              vscode.window.showErrorMessage(`Switch vault failed: ${res.data?.error || res.status}`);
              return;
            }
            treeProvider.refresh();
            vscode.window.showInformationMessage(`Switched active workspace to '${vaultId}'.`);
            return res.data;
          }

          if (action === "remove") {
            const res = await apiRequest(
              "DELETE",
              `/v1/vaults/${encodeURIComponent(vaultId)}`,
              {},
              { useOwnerToken: true }
            );
            if (!res.ok) {
              vscode.window.showErrorMessage(`Remove vault failed: ${res.data?.error || res.status}`);
              return;
            }
            vscode.window.showInformationMessage(`Removed workspace '${vaultId}' from registry.`);
            return res.data;
          }

          const patchBody = {};
          if (opts.label !== undefined) patchBody.label = String(opts.label);
          if (opts.default_host_policy !== undefined) {
            patchBody.default_host_policy = String(opts.default_host_policy);
          }
          if (opts.federated_searchable !== undefined) {
            patchBody.federated_searchable = Boolean(opts.federated_searchable);
          }
          if (!Object.keys(patchBody).length) {
            const polPick = await vscode.window.showQuickPick(["local_only", "ask", "allow"], {
              placeHolder: `New default host policy for '${vaultId}'`,
            });
            if (!polPick) return;
            const fedPick = await vscode.window.showQuickPick(["true", "false"], {
              placeHolder: `Allow federated search on '${vaultId}'?`,
            });
            if (!fedPick) return;
            patchBody.default_host_policy = polPick;
            patchBody.federated_searchable = fedPick === "true";
          }
          const res = await apiRequest(
            "PATCH",
            `/v1/vaults/${encodeURIComponent(vaultId)}`,
            patchBody,
            { useOwnerToken: true }
          );
          if (!res.ok) {
            vscode.window.showErrorMessage(`Update vault policy failed: ${res.data?.error || res.status}`);
            return;
          }
          vscode.window.showInformationMessage(`Updated policy for workspace '${vaultId}'.`);
          return res.data;
        }

        if (action === "federated_recall") {
          const query =
            opts.query ||
            (await vscode.window.showInputBox({
              prompt: "Federated recall query across opted-in workspaces",
            }));
          if (!query || !query.trim()) return;
          const res = await apiRequest(
            "POST",
            "/v1/vaults/federated-recall",
            {
              query: query.trim(),
              limit: opts.limit || 5,
              project_scope: opts.project_scope || undefined,
              as_of: opts.as_of || undefined,
              include_expired: Boolean(opts.include_expired),
            },
            { useOwnerToken: true }
          );
          if (!res.ok) {
            vscode.window.showErrorMessage(`Federated recall failed: ${res.data?.error || res.status}`);
            return;
          }
          const results = Array.isArray(res.data?.results) ? res.data.results : [];
          const lines = [
            `# Federated Recall: "${query.trim()}"`,
            "",
            `Hits: ${results.length}`,
            "",
          ];
          for (const hit of results) {
            lines.push(
              `- **[${hit.vault_label || hit.vault_id || "vault"}]** \`${hit.filename || ""}\` — ${hit.claim || hit.text || ""}`
            );
          }
          await openMarkdownPreview(lines.join("\n"));
          return res.data;
        }
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    }),

    // ── Sharing & Host Privacy Policies ──
    vscode.commands.registerCommand("secondBrain.manageHostPolicies", async (arg) => {
      try {
        const opts = arg && typeof arg === "object" ? arg : {};
        const listRes = await apiRequest("GET", "/v1/hosts", null, { useOwnerToken: true });
        if (!listRes.ok) {
          vscode.window.showErrorMessage(
            `Load host policies failed: ${listRes.data?.error || listRes.status}`
          );
          return;
        }
        const policies = listRes.data && typeof listRes.data === "object" ? listRes.data : {};
        const hostIds = Object.keys(policies).sort();
        if (!hostIds.length) {
          vscode.window.showInformationMessage("No configured host privacy policies found.");
          return;
        }

        let hostId = opts.host_id ? String(opts.host_id).trim() : "";
        if (!hostId) {
          const pickedHost = await vscode.window.showQuickPick(
            hostIds.map((hid) => {
              const p = policies[hid] || {};
              return {
                label: hid,
                description: `${p.enabled ? "enabled" : "disabled"} · ${p.transport || "local"} · projects=[${(
                  p.allowed_projects || []
                ).join(", ")}]`,
                detail: `model=${p.default_model || "-"} · pii_redact=${Boolean(p.redact_pii)}`,
              };
            }),
            { placeHolder: "Select Host Adapter to inspect or update sharing policy" }
          );
          if (!pickedHost) return;
          hostId = pickedHost.label;
        }

        const current = policies[hostId] || {};
        const updateFields = [
          "enabled",
          "default_model",
          "endpoint_url",
          "allowed_projects",
          "allow_unscoped_atoms",
          "allow_preference_atoms",
          "redact_pii",
          "fail_on_secret_detection",
          "api_key_env",
        ];
        let payload = opts.updates && typeof opts.updates === "object" ? { ...opts.updates } : null;
        if (!payload) {
          const direct = {};
          for (const k of updateFields) {
            if (opts[k] !== undefined) direct[k] = opts[k];
          }
          if (Object.keys(direct).length > 0) {
            payload = direct;
          }
        }

        if (!payload) {
          const op = await vscode.window.showQuickPick(
            [
              { label: "toggle_enabled", description: `Currently enabled=${Boolean(current.enabled)}` },
              {
                label: "allowed_projects",
                description: `Current: ${(current.allowed_projects || []).join(", ") || "(none)"}`,
              },
              {
                label: "default_model",
                description: `Current: ${current.default_model || "(default)"}`,
              },
              {
                label: "toggle_preferences",
                description: `Currently allow_preference_atoms=${Boolean(current.allow_preference_atoms)}`,
              },
              {
                label: "toggle_unscoped",
                description: `Currently allow_unscoped_atoms=${Boolean(current.allow_unscoped_atoms)}`,
              },
              { label: "inspect", description: "View full host policy JSON in preview" },
            ],
            { placeHolder: `Configure sharing policy for host '${hostId}'` }
          );
          if (!op) return;

          if (op.label === "inspect") {
            await openMarkdownPreview(
              [`# Host Privacy Policy: ${hostId}`, "", "```json", JSON.stringify(current, null, 2), "```"].join(
                "\n"
              )
            );
            return current;
          }
          payload = {};
          if (op.label === "toggle_enabled") {
            payload.enabled = !current.enabled;
          } else if (op.label === "toggle_preferences") {
            payload.allow_preference_atoms = !current.allow_preference_atoms;
          } else if (op.label === "toggle_unscoped") {
            payload.allow_unscoped_atoms = !current.allow_unscoped_atoms;
          } else if (op.label === "allowed_projects") {
            const rawProj = await vscode.window.showInputBox({
              prompt: `Allowed project scopes for '${hostId}' (comma-separated, blank for none)`,
              value: (current.allowed_projects || []).join(", "),
            });
            if (rawProj === undefined) return;
            payload.allowed_projects = parseListInput(rawProj);
          } else if (op.label === "default_model") {
            const model = await vscode.window.showInputBox({
              prompt: `Default model for '${hostId}'`,
              value: String(current.default_model || ""),
            });
            if (model === undefined) return;
            payload.default_model = model.trim();
          }
        }

        const putRes = await apiRequest(
          "PUT",
          `/v1/hosts/${encodeURIComponent(hostId)}`,
          payload,
          { useOwnerToken: true }
        );
        if (!putRes.ok) {
          vscode.window.showErrorMessage(
            `Update host policy failed: ${putRes.data?.error || putRes.status}`
          );
          return;
        }
        vscode.window.showInformationMessage(
          `Updated host policy '${hostId}': enabled=${Boolean(putRes.data?.enabled)}`
        );
        return putRes.data;
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API error: ${err.message}`);
      }
    })
  );
}

function deactivate() {
  if (activeExtensionState) {
    if (typeof activeExtensionState.clearContextSignalTimer === "function") {
      activeExtensionState.clearContextSignalTimer();
    }
    if (typeof activeExtensionState.disposeGraphPanel === "function") {
      activeExtensionState.disposeGraphPanel();
    }
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
