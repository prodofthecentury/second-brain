"use strict";

const obsidian = require("obsidian");
const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");

const VIEW_TYPE_RADAR = "second-brain-contradiction-radar";

function resolveSecurityDir() {
  const envDir = (process.env.BRAIN_SECURITY_STATE_DIR || "").trim();
  if (envDir) return path.resolve(envDir);
  if (process.platform === "win32") {
    return path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local"), "SecondBrain", "security");
  }
  return path.join(process.env.XDG_STATE_HOME || path.join(os.homedir(), ".local", "state"), "SecondBrain", "security");
}

function readSafeToken(filePath) {
  try {
    const st = fs.lstatSync(filePath);
    if (!st.isFile() || st.isSymbolicLink() || st.size > 4096) return "";
    return fs.readFileSync(filePath, "utf8").trim();
  } catch {
    return "";
  }
}

function discoverPort(defaultPort = 8765) {
  const raw = readSafeToken(path.join(resolveSecurityDir(), "api-port"));
  if (!raw) return defaultPort;
  try {
    const doc = JSON.parse(raw);
    const port = Math.trunc(Number(doc.port));
    return port >= 1 && port <= 65535 ? port : defaultPort;
  } catch {
    return defaultPort;
  }
}

function loopbackJson(port, method, apiPath, body = null, { ownerToken = "" } = {}) {
  const secDir = resolveSecurityDir();
  const bearer = readSafeToken(path.join(secDir, "memory-api-token")) || readSafeToken(path.join(secDir, "api-token"));
  const owner = ownerToken || readSafeToken(path.join(secDir, "owner-token"));
  const payload = body !== null ? JSON.stringify(body) : null;
  const headers = { Host: `127.0.0.1:${port}`, Accept: "application/json" };
  if (bearer) headers.Authorization = `Bearer ${bearer}`;
  if (owner) headers["X-Owner-Token"] = owner;
  if (payload !== null) {
    headers["Content-Type"] = "application/json";
    headers["Content-Length"] = Buffer.byteLength(payload, "utf8");
  }
  return new Promise((resolve, reject) => {
    const req = http.request({ hostname: "127.0.0.1", port, path: apiPath, method, headers }, (res) => {
      let raw = "";
      res.setEncoding("utf8");
      res.on("data", (c) => (raw += c));
      res.on("end", () => {
        try {
          resolve({ ok: res.statusCode >= 200 && res.statusCode < 300, status: res.statusCode, data: raw ? JSON.parse(raw) : {} });
        } catch {
          resolve({ ok: false, status: res.statusCode, data: { raw } });
        }
      });
    });
    req.setTimeout(15000, () => req.destroy(new Error("Loopback request timeout")));
    req.on("error", reject);
    if (payload !== null) req.write(payload);
    req.end();
  });
}

class ContradictionRadarView extends obsidian.ItemView {
  constructor(leaf, plugin) {
    super(leaf);
    this.plugin = plugin;
  }
  getViewType() {
    return VIEW_TYPE_RADAR;
  }
  getDisplayText() {
    return "Second Brain Contradiction Radar";
  }
  async onOpen() {
    await this.refreshRadar();
  }
  async refreshRadar() {
    const root = this.containerEl.children[1];
    root.empty();
    root.createEl("h4", { text: "Second Brain Contradiction Radar" });
    try {
      const port = discoverPort(this.plugin.settings.apiPort);
      const res = await loopbackJson(port, "GET", "/v1/graph/contradictions");
      const alerts = (res.data && res.data.alerts) || [];
      if (alerts.length === 0) {
        root.createEl("p", { cls: "sb-obs-ok", text: "0 verified claim contradictions detected." });
        return;
      }
      for (const item of alerts) {
        const card = root.createDiv({ cls: "sb-obs-card" });
        card.createEl("strong", { text: `${item.atom_a || ""} <-> ${item.atom_b || ""}` });
        card.createEl("div", { text: item.reason || item.rationale || "" });
      }
    } catch (err) {
      root.createEl("p", { cls: "sb-obs-err", text: `API offline: ${err.message}` });
    }
  }
}

class SecondBrainObsidianPlugin extends obsidian.Plugin {
  async onload() {
    this.settings = Object.assign({ apiPort: 8765 }, (await this.loadData()) || {});
    this.statusBarItemEl = this.addStatusBarItem();
    this.statusBarItemEl.setText("Second Brain: checking...");

    this.registerView(VIEW_TYPE_RADAR, (leaf) => new ContradictionRadarView(leaf, this));
    this.addRibbonIcon("shield-check", "Open Second Brain Contradiction Radar", () => this.activateRadarView());

    this.addCommand({
      id: "promote-selection-to-note-verify",
      name: "Stage & Remember Selection in Second Brain",
      editorCallback: async (editor, view) => {
        const selection = editor.getSelection().trim();
        if (!selection) {
          new obsidian.Notice("Select text to send to Second Brain.");
          return;
        }
        const port = discoverPort(this.settings.apiPort);
        const res = await loopbackJson(port, "POST", "/v1/remember", {
          content: selection,
          title: view.file ? view.file.basename : "Obsidian Capture",
          source: "obsidian",
        });
        if (res.ok) {
          new obsidian.Notice(`Second Brain saved: ${res.data.filename || "captured"}`);
        } else {
          new obsidian.Notice(`Second Brain error (${res.status}): ${res.data.error || "failed"}`);
        }
      },
    });

    this.addCommand({
      id: "open-contradiction-radar",
      name: "Open Contradiction Radar View",
      callback: () => this.activateRadarView(),
    });

    await this.refreshStatusBadge();
  }

  async refreshStatusBadge() {
    try {
      const port = discoverPort(this.settings.apiPort);
      const res = await loopbackJson(port, "GET", "/v1/health");
      if (res.ok) {
        this.statusBarItemEl.setText(`Second Brain (${res.data.approved_atom_count ?? 0})`);
      } else {
        this.statusBarItemEl.setText("Second Brain: auth error");
      }
    } catch {
      this.statusBarItemEl.setText("Second Brain: offline");
    }
  }

  async activateRadarView() {
    const leaf = this.app.workspace.getRightLeaf(false);
    await leaf.setViewState({ type: VIEW_TYPE_RADAR, active: true });
    this.app.workspace.revealLeaf(leaf);
  }
}

module.exports = SecondBrainObsidianPlugin;
