const vscode = require("vscode");
const http = require("http");
const fs = require("fs");
const path = require("path");
const os = require("os");
const crypto = require("crypto");

function getSecurityDir() {
  if (process.platform === "win32") {
    const localAppData = process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
    return path.join(localAppData, "SecondBrain", "security");
  }
  const stateHome = process.env.XDG_STATE_HOME || path.join(os.homedir(), ".local", "state");
  return path.join(stateHome, "SecondBrain", "security");
}

function readApiToken() {
  const tokenPath = path.join(getSecurityDir(), "memory-api-token");
  try {
    const st = fs.lstatSync(tokenPath);
    if (!st.isFile() || st.isSymbolicLink() || st.size > 4096) {
      return "";
    }
    return fs.readFileSync(tokenPath, "utf8").trim();
  } catch {
    return "";
  }
}

function getPort() {
  return vscode.workspace.getConfiguration("secondBrain").get("apiPort", 8765);
}

function apiRequest(method, apiPath, body = null) {
  const port = getPort();
  const token = readApiToken();
  const payload = body !== null ? JSON.stringify(body) : null;
  const headers = {
    Host: `127.0.0.1:${port}`,
    Accept: "application/json",
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
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
    req.on("error", reject);
    if (payload !== null) req.write(payload);
    req.end();
  });
}

class VaultTreeProvider {
  constructor() {
    this._onDidChangeTreeData = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._onDidChangeTreeData.event;
  }
  refresh() {
    this._onDidChangeTreeData.fire();
  }
  getTreeItem(el) {
    return el;
  }
  async getChildren(element) {
    if (!element) {
      return [
        new vscode.TreeItem("Notes", vscode.TreeItemCollapsibleState.Expanded),
        new vscode.TreeItem("Verified Atoms", vscode.TreeItemCollapsibleState.Collapsed),
        new vscode.TreeItem("Playbooks", vscode.TreeItemCollapsibleState.Collapsed),
        new vscode.TreeItem("Snapshots", vscode.TreeItemCollapsibleState.Collapsed),
      ];
    }
    try {
      if (element.label === "Notes") {
        const res = await apiRequest("GET", "/v1/notes?limit=50");
        if (!res.ok) return [new vscode.TreeItem("API offline — start Second Brain desktop or CLI")];
        return (res.data?.items || res.data?.notes || []).map((n) => {
          const item = new vscode.TreeItem(n.title || n.filename, vscode.TreeItemCollapsibleState.None);
          item.description = (n.tags || []).join(", ");
          item.tooltip = n.filename;
          return item;
        });
      }
      if (element.label === "Verified Atoms") {
        const res = await apiRequest("GET", "/v1/atoms?limit=50");
        if (!res.ok) return [new vscode.TreeItem("API offline")];
        return (res.data?.items || res.data?.atoms || []).map((a) => {
          const item = new vscode.TreeItem(a.filename, vscode.TreeItemCollapsibleState.None);
          item.description = a.review_status || a.status || "";
          item.tooltip = a.claim || "";
          return item;
        });
      }
      if (element.label === "Playbooks") {
        const res = await apiRequest("GET", "/v1/playbooks");
        if (!res.ok) return [new vscode.TreeItem("API offline")];
        const pbs = res.data?.playbooks || [];
        if (!pbs.length) return [new vscode.TreeItem("No promoted playbooks yet")];
        return pbs.map((pb) => {
          const item = new vscode.TreeItem(pb.title || pb.filename, vscode.TreeItemCollapsibleState.None);
          item.description = pb.status || "accepted";
          item.tooltip = pb.trigger || pb.filename;
          return item;
        });
      }
      if (element.label === "Snapshots") {
        const res = await apiRequest("GET", "/v1/snapshots");
        if (!res.ok) return [new vscode.TreeItem("API offline")];
        const snaps = res.data?.snapshots || [];
        if (!snaps.length) return [new vscode.TreeItem("No snapshots recorded yet")];
        return snaps.map((s) => {
          const item = new vscode.TreeItem(s.snapshot_id, vscode.TreeItemCollapsibleState.None);
          item.description = `${s.atom_count || 0} atoms`;
          item.tooltip = `Merkle: ${s.merkle_root || ""}`;
          return item;
        });
      }
    } catch {
      return [new vscode.TreeItem("Start Second Brain API (`second-brain serve`)")];
    }
    return [];
  }
}

function activate(context) {
  const treeProvider = new VaultTreeProvider();
  vscode.window.registerTreeDataProvider("secondBrain.vaultView", treeProvider);

  context.subscriptions.push(
    vscode.commands.registerCommand("secondBrain.refreshVault", () => treeProvider.refresh()),

    vscode.commands.registerCommand("secondBrain.rememberSelection", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) return;
      const text = editor.document.getText(editor.selection).trim();
      if (!text) {
        vscode.window.showWarningMessage("Select text to remember in Second Brain.");
        return;
      }
      const title = await vscode.window.showInputBox({
        prompt: "Optional title for this memory",
        value: path.basename(editor.document.fileName),
      });
      try {
        const res = await apiRequest("POST", "/v1/remember", {
          content: text,
          title: title || undefined,
          source: `vscode:${path.basename(editor.document.fileName)}`,
          tags: ["vscode"],
        });
        if (res.ok) {
          const statusLabel = res.data?.status === "quarantined"
            ? "quarantined in inbox for owner review"
            : (res.data?.status || res.data?.tier || "notes");
          vscode.window.showInformationMessage(`Remembered in Second Brain (${statusLabel}).`);
          treeProvider.refresh();
        } else {
          vscode.window.showErrorMessage(`Second Brain remember failed: ${res.data?.error || res.status}`);
        }
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API unreachable: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.quickRecall", async () => {
      const query = await vscode.window.showInputBox({ prompt: "Search Second Brain notes & verified atoms" });
      if (!query) return;
      try {
        const [recallRes, notesRes] = await Promise.all([
          apiRequest("POST", "/v1/recall", { query, n_results: 8, retrieval_mode: "auto" }),
          apiRequest("GET", `/v1/notes?query=${encodeURIComponent(query)}&limit=10`),
        ]);
        const atoms = recallRes.ok ? (recallRes.data?.results || []) : [];
        const notes = notesRes.ok ? (notesRes.data?.items || notesRes.data?.notes || []) : [];
        const choices = [
          ...atoms.map((a) => ({
            label: `[Verified Atom] ${a.filename}`,
            description: a.filename,
            detail: a.claim || "",
            kind: "atom",
            claim: a.claim || "",
            contexts: a.contexts || [],
          })),
          ...notes.map((n) => ({
            label: `[Note] ${n.title || n.filename}`,
            description: n.filename,
            detail: n.snippet || "",
            kind: "note",
          })),
        ];
        if (!choices.length) {
          vscode.window.showInformationMessage("No matching verified atoms or working notes found.");
          return;
        }
        const picked = await vscode.window.showQuickPick(choices, { placeHolder: "Select a verified atom or note to preview" });
        if (picked) {
          if (picked.kind === "atom") {
            const proofRes = await apiRequest("GET", `/v1/ledger/prove/${encodeURIComponent(picked.description)}`);
            const md = [
              `# Verified Atom: ${picked.description}`,
              "",
              `**Contexts:** ${(picked.contexts || []).join(", ") || "none"}`,
              proofRes.ok ? `**Merkle Root:** \`${proofRes.data?.merkle_root || ""}\`` : "",
              "",
              "## Claim",
              picked.claim,
            ].filter(Boolean).join("\n");
            const doc = await vscode.workspace.openTextDocument({ content: md, language: "markdown" });
            await vscode.window.showTextDocument(doc, { preview: true });
          } else {
            const full = await apiRequest("GET", `/v1/notes/${encodeURIComponent(picked.description)}`);
            const doc = await vscode.workspace.openTextDocument({
              content: full.data?.note?.body || full.data?.body || "",
              language: "markdown",
            });
            await vscode.window.showTextDocument(doc, { preview: true });
          }
        }
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API unreachable: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.openChat", () => {
      const nonce = crypto.randomBytes(16).toString("base64");
      const panel = vscode.window.createWebviewPanel(
        "secondBrainChat",
        "Second Brain Chat",
        vscode.ViewColumn.Beside,
        { enableScripts: true }
      );
      panel.webview.html = `<!DOCTYPE html><html><head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';" /></head><body style="font-family:sans-serif;padding:14px;background:#0d1117;color:#e6edf3;">
        <h3>Second Brain Streaming Chat</h3>
        <div style="display:flex;gap:8px;margin-bottom:10px;">
          <input id="q" style="flex:1;padding:8px;background:#161b22;color:#fff;border:1px solid #30363d;border-radius:6px;" placeholder="Ask your vault..." />
          <select id="m" style="padding:8px;background:#161b22;color:#fff;border:1px solid #30363d;border-radius:6px;">
            <option value="stream">Stream</option>
            <option value="verified">Verified</option>
            <option value="instant">Instant</option>
          </select>
          <button id="go" style="padding:8px 14px;background:#7c5cff;color:#fff;border:none;border-radius:6px;cursor:pointer;">Ask</button>
        </div>
        <pre id="out" style="white-space:pre-wrap;line-height:1.5;background:#161b22;padding:12px;border-radius:8px;min-height:220px;"></pre>
        <script nonce="${nonce}">
          const vscode = acquireVsCodeApi();
          document.getElementById("go").onclick = () => {
            document.getElementById("out").textContent = "Retrieving...";
            vscode.postMessage({ query: document.getElementById("q").value, mode: document.getElementById("m").value });
          };
          window.addEventListener("message", (e) => {
            const msg = e.data;
            const out = document.getElementById("out");
            if (msg.type === "reset") out.textContent = msg.text || "";
            if (msg.type === "append") out.textContent += msg.text || "";
          });
        </script></body></html>`;

      panel.webview.onDidReceiveMessage(async ({ query, mode }) => {
        if (!query) return;
        const port = getPort();
        const token = readApiToken();
        const body = JSON.stringify({ query, mode });
        const req = http.request(
          {
            hostname: "127.0.0.1",
            port,
            path: "/v1/chat/stream",
            method: "POST",
            headers: {
              Host: `127.0.0.1:${port}`,
              Authorization: `Bearer ${token}`,
              "Content-Type": "application/json",
              "Content-Length": Buffer.byteLength(body, "utf8"),
            },
          },
          (res) => {
            panel.webview.postMessage({ type: "reset", text: "" });
            let buf = "";
            let ev = "message";
            res.setEncoding("utf8");
            res.on("data", (chunk) => {
              buf += chunk;
              const lines = buf.split(/\r?\n/);
              buf = lines.pop() || "";
              for (const line of lines) {
                if (line.startsWith("event:")) ev = line.slice(6).trim();
                else if (line.startsWith("data:")) {
                  try {
                    const data = JSON.parse(line.slice(5).trim());
                    if (ev === "token") panel.webview.postMessage({ type: "append", text: data.text ?? data.token ?? "" });
                    else if (ev === "verified_answer") panel.webview.postMessage({ type: "reset", text: data.text || data.answer || "" });
                    else if (ev === "abstain") panel.webview.postMessage({ type: "reset", text: `Abstained: ${data.reason || data.text || ""}` });
                    else if (ev === "error") panel.webview.postMessage({ type: "reset", text: `Error: ${data.error || ""}` });
                    else if (ev === "context" && mode === "instant") {
                      const summary = (data.sources || []).map((s) => `- ${s.title || s.filename}: ${s.excerpt || s.claim || ""}`).join("\n");
                      panel.webview.postMessage({ type: "reset", text: summary || "No matches." });
                    }
                  } catch {}
                  ev = "message";
                }
              }
            });
          }
        );
        req.on("error", (err) => panel.webview.postMessage({ type: "reset", text: `Error: ${err.message}` }));
        req.write(body);
        req.end();
      });
    }),

    vscode.commands.registerCommand("secondBrain.assemblePlan", async () => {
      const goal = await vscode.window.showInputBox({ prompt: "Enter task goal for grounded Executive Plan" });
      if (!goal) return;
      try {
        const res = await apiRequest("POST", "/v1/plan", { goal, mode: "deterministic" });
        const doc = await vscode.workspace.openTextDocument({
          content: JSON.stringify(res.data, null, 2),
          language: "json",
        });
        await vscode.window.showTextDocument(doc, { preview: true });
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain plan failed: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.verifyLedger", async () => {
      try {
        const res = await apiRequest("GET", "/v1/ledger/verify");
        if (!res.ok) {
          vscode.window.showErrorMessage(`Ledger verify failed: ${res.data?.error || res.status}`);
          return;
        }
        const valid = res.data?.chain_valid ? "VALID" : "INVALID";
        vscode.window.showInformationMessage(
          `Vault Merkle Ledger: ${valid} · ${res.data?.approved_atoms || 0} approved atoms · root ${(res.data?.live_merkle_root || "").slice(0, 16)}…`
        );
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API unreachable: ${err.message}`);
      }
    }),

    vscode.commands.registerCommand("secondBrain.scanContradictions", async () => {
      try {
        const res = await apiRequest("GET", "/v1/graph/contradictions");
        if (!res.ok) {
          vscode.window.showErrorMessage(`Contradiction scan failed: ${res.data?.error || res.status}`);
          return;
        }
        const count = res.data?.contradiction_count || 0;
        if (count === 0) {
          vscode.window.showInformationMessage("Contradiction Radar: 0 conflicts detected across vault.");
        } else {
          const doc = await vscode.workspace.openTextDocument({
            content: JSON.stringify(res.data, null, 2),
            language: "json",
          });
          await vscode.window.showTextDocument(doc, { preview: true });
        }
      } catch (err) {
        vscode.window.showErrorMessage(`Second Brain API unreachable: ${err.message}`);
      }
    })
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
