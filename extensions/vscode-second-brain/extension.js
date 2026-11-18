const vscode = require("vscode");
const http = require("http");
const fs = require("fs");
const path = require("path");
const os = require("os");
const crypto = require("crypto");

function getSecurityDir() {
  const explicit = (process.env.BRAIN_SECURITY_STATE_DIR || "").trim();
  if (explicit) {
    return path.resolve(explicit);
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
    const parentStat = fs.lstatSync(path.dirname(filePath));
    if (!parentStat.isDirectory() || parentStat.isSymbolicLink()) {
      return "";
    }
    const st = fs.lstatSync(filePath);
    if (!st.isFile() || st.isSymbolicLink() || st.size > 4096) {
      return "";
    }
    return fs.readFileSync(filePath, "utf8").trim();
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
  return readSafeTokenFile(path.join(getSecurityDir(), "memory-api-token"));
}

const readApiToken = readBearerToken;

function readOwnerToken() {
  const envOwnerToken =
    (process.env.BRAIN_OWNER_API_TOKEN || "").trim() ||
    (process.env.BRAIN_OWNER_TOKEN || "").trim();
  if (envOwnerToken && envOwnerToken.length >= 24) return envOwnerToken;

  const inspectedPath = vscode.workspace
    .getConfiguration("secondBrain")
    .inspect("ownerTokenPath");
  const configuredOwnerPath = String(inspectedPath?.globalValue || "").trim();
  const envOwnerFile = (process.env.BRAIN_OWNER_TOKEN_FILE || "").trim();
  const secDir = getSecurityDir();

  const candidate =
    (configuredOwnerPath && readSafeTokenFile(configuredOwnerPath)) ||
    (envOwnerFile && readSafeTokenFile(envOwnerFile)) ||
    readSafeTokenFile(path.join(secDir, "owner-api-token")) ||
    readSafeTokenFile(path.join(secDir, "owner-token"));
  return candidate && candidate.length >= 24 ? candidate : "";
}

function isPidAlive(pid) {
  const n = Number(pid);
  if (!Number.isInteger(n) || n <= 0) return false;
  try {
    process.kill(n, 0);
    return true;
  } catch (err) {
    return Boolean(err && err.code === "EPERM");
  }
}

function discoverLiveApiPort() {
  const explicitPortFile = (process.env.BRAIN_API_PORT_FILE || "").trim();
  const portFile = explicitPortFile
    ? path.resolve(explicitPortFile)
    : path.join(getSecurityDir(), "api-port");
  const raw = readSafeTokenFile(portFile);
  if (!raw) return 0;
  if (/^\d+$/.test(raw)) {
    const p = Number(raw);
    return p >= 1 && p <= 65535 ? p : 0;
  }
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return 0;
    const port = Math.trunc(Number(parsed.port));
    if (!Number.isFinite(port) || port < 1 || port > 65535) return 0;
    if (parsed.pid !== undefined && parsed.pid !== null) {
      if (!isPidAlive(parsed.pid)) return 0;
    }
    return port;
  } catch {
    return 0;
  }
}

function getPort() {
  const inspected = vscode.workspace
    .getConfiguration("secondBrain")
    .inspect("apiPort");
  const defaultPort =
    Math.trunc(Number(inspected?.defaultValue ?? 8765)) || 8765;
  const userGlobal = inspected?.globalValue;
  if (typeof userGlobal === "number" && Number.isFinite(userGlobal)) {
    const customPort = Math.trunc(userGlobal);
    if (customPort >= 1 && customPort <= 65535 && customPort !== defaultPort) {
      return customPort;
    }
  }
  const discovered = discoverLiveApiPort();
  if (discovered >= 1 && discovered <= 65535) {
    return discovered;
  }
  return 8765;
}

function apiRequest(method, apiPath, body = null, { useOwnerToken = true } = {}) {
  const port = getPort();
  const token = readBearerToken();
  const ownerToken = useOwnerToken ? readOwnerToken() : "";
  const payload = body !== null && body !== undefined ? JSON.stringify(body) : null;
  const headers = {
    Host: `127.0.0.1:${port}`,
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
      { hostname: "127.0.0.1", port, path: apiPath, method, headers },
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

async function openMarkdownPreview(markdownText) {
  const doc = await vscode.workspace.openTextDocument({
    content: markdownText,
    language: "markdown",
  });
  await vscode.window.showTextDocument(doc, { preview: true });
}

function formatPlanMarkdown(plan, fallbackGoal = "") {
  const ready = Boolean(plan?.plan_ready);
  const lines = [
    `# Executive Plan: ${plan?.goal || fallbackGoal}`,
    "",
    `- **Status:** ${ready ? "READY (Citation-Grounded)" : "ABSTAINED (Insufficient Verified Memory)"}`,
    `- **Mode:** \`${plan?.mode_used || "deterministic"}\``,
    `- **Summary:** ${plan?.summary || "No summary returned."}`,
  ];
  if ((plan?.matched_playbook_ids || []).length) {
    lines.push(
      `- **Matched Playbooks:** ${plan.matched_playbook_ids.map((id) => `\`${id}\``).join(", ")}`
    );
  }
  if ((plan?.working_context_atom_ids || []).length) {
    lines.push(
      `- **Working Context Atoms:** ${plan.working_context_atom_ids.map((id) => `\`${id}\``).join(", ")}`
    );
  }
  lines.push("");

  if ((plan?.preconditions || []).length) {
    lines.push("## Preconditions");
    for (const p of plan.preconditions) lines.push(`- ${p}`);
    lines.push("");
  }

  const steps = plan?.steps || [];
  if (steps.length) {
    lines.push("## Grounded Execution Steps");
    steps.forEach((step, idx) => {
      const provParts = [];
      if (step.source_playbook_id) provParts.push(`playbook:${step.source_playbook_id}`);
      if ((step.backing_atom_ids || []).length) {
        provParts.push(`atoms:${step.backing_atom_ids.join(",")}`);
      }
      const prov = provParts.length ? ` _(${provParts.join("; ")})_` : "";
      const cites = (step.citations || []).length
        ? ` — Citations: \`${step.citations.join("`, `")}\``
        : "";
      lines.push(`${idx + 1}. **${step.instruction || ""}**${prov}${cites}`);
    });
    lines.push("");
  }

  if ((plan?.expected_outputs || []).length) {
    lines.push("## Expected Outputs & Verification");
    for (const out of plan.expected_outputs) lines.push(`- ${out}`);
    lines.push("");
  }

  if ((plan?.rollback_checkpoints || []).length) {
    lines.push("## Rollback Checkpoints");
    for (const rb of plan.rollback_checkpoints) lines.push(`- ${rb}`);
    lines.push("");
  }

  if ((plan?.preference_constraints || []).length) {
    lines.push("## Owner Preference Guardrails");
    for (const pref of plan.preference_constraints) lines.push(`- ${pref}`);
    lines.push("");
  }

  if ((plan?.epistemic_gaps || []).length) {
    lines.push("## Epistemic Gaps & Notices");
    for (const gap of plan.epistemic_gaps) lines.push(`- ${gap}`);
    lines.push("");
  }

  return lines.join("\n");
}

function formatRadarMarkdown(data) {
  const alerts = data?.alerts || [];
  const drift = data?.drift_alerts || [];
  const orphans = data?.orphan_approved_atoms || [];

  const lines = [
    "# Contradiction & Source Drift Radar Report",
    "",
    `- **Contradiction Alerts:** ${data?.contradiction_count ?? alerts.length} (${data?.high_severity_count ?? 0} high severity)`,
    `- **Source Anchor Drift Alerts:** ${data?.drift_count ?? drift.length}`,
    `- **Orphan Approved Atoms:** ${data?.orphan_approved_count ?? orphans.length}`,
    "",
  ];

  lines.push("## Contradiction Pairs");
  if (!alerts.length) {
    lines.push("_0 polarity or numeric contradictions detected across active atoms._", "");
  } else {
    alerts.forEach((al, idx) => {
      lines.push(
        `### ${idx + 1}. [${String(al.severity || "medium").toUpperCase()}] ${(al.reasons || []).join(", ")}`
      );
      if ((al.shared_terms || []).length) {
        lines.push(`- **Shared Terms:** \`${al.shared_terms.join("`, `")}\``);
      }
      if (al.atom_a) {
        lines.push(
          `- **Atom A (\`${al.atom_a.filename}\`, ${al.atom_a.review_status || ""}):** ${al.atom_a.claim || ""}`
        );
      }
      if (al.atom_b) {
        lines.push(
          `- **Atom B (\`${al.atom_b.filename}\`, ${al.atom_b.review_status || ""}):** ${al.atom_b.claim || ""}`
        );
      }
      lines.push("");
    });
  }

  lines.push("## Source Anchor Drift Alerts");
  if (!drift.length) {
    lines.push("_All approved atom byte-anchors match their immutable source snapshots._", "");
  } else {
    for (const d of drift) {
      lines.push(
        `- **\`${d.filename || d.atom_id || "Atom"}\`:** ${d.reason || d.detail || "Source anchor drift"}`
      );
    }
    lines.push("");
  }

  lines.push("## Orphan Approved Atoms (Unlinked)");
  if (!orphans.length) {
    lines.push("_None — all approved atoms participate in graph edges._", "");
  } else {
    for (const fn of orphans) lines.push(`- \`${fn}\``);
    lines.push("");
  }

  return lines.join("\n");
}

function formatSnapshotDiffMarkdown(d) {
  const lines = [
    `# Vault Semantic Diff: \`${d.from_snapshot}\` -> \`${d.to_snapshot}\``,
    "",
    `- **Status:** ${d.unchanged ? "UNCHANGED (Identical Merkle Root)" : "CHANGED"}`,
    `- **Merkle Root Before:** \`${d.merkle_root_before || ""}\``,
    `- **Merkle Root After:** \`${d.merkle_root_after || ""}\``,
    "",
  ];

  const added = d.added_atoms || [];
  const removed = d.removed_atoms || [];
  const modified = d.modified_atoms || [];

  lines.push(`## Added Atoms (${added.length})`);
  if (!added.length) lines.push("_None_");
  for (const a of added) {
    lines.push(`- **\`${a.filename || a.atom_id || ""}\`**: ${a.claim || ""}`);
  }
  lines.push("");

  lines.push(`## Removed Atoms (${removed.length})`);
  if (!removed.length) lines.push("_None_");
  for (const a of removed) {
    lines.push(`- **\`${a.filename || a.atom_id || ""}\`**: ${a.claim || ""}`);
  }
  lines.push("");

  lines.push(`## Modified Atoms (${modified.length})`);
  if (!modified.length) lines.push("_None_");
  for (const m of modified) {
    lines.push(
      `- **\`${m.filename}\`** (\`${String(m.old_sha256 || "").slice(0, 12)}\` -> \`${String(m.new_sha256 || "").slice(0, 12)}\`)`,
      `  - **Before:** ${m.old_claim || ""}`,
      `  - **After:** ${m.new_claim || ""}`
    );
  }
  lines.push("");

  const pbAdded = d.added_playbooks || [];
  const pbRemoved = d.removed_playbooks || [];
  const pbMod = d.modified_playbooks || [];
  if (pbAdded.length || pbRemoved.length || pbMod.length) {
    lines.push("## Playbook Changes");
    for (const p of pbAdded) lines.push(`- Added: \`${p}\``);
    for (const p of pbRemoved) lines.push(`- Removed: \`${p}\``);
    for (const p of pbMod) lines.push(`- Modified: \`${p}\``);
    lines.push("");
  }

  return lines.join("\n");
}

function formatPlaybookMarkdown(pb) {
  const lines = [
    `# Skill Playbook: ${pb.title || pb.playbook_id || pb.filename}`,
    "",
    `- **Playbook ID:** \`${pb.playbook_id || pb.filename || ""}\``,
    `- **Status:** \`${pb.status || "accepted"}\``,
    `- **SHA-256:** \`${pb.sha256 || ""}\``,
    `- **Contexts:** ${(pb.contexts || []).join(", ") || "unscoped"}`,
    `- **Backing Atoms:** ${(pb.backing_atoms || []).map((a) => `\`${a}\``).join(", ") || "none"}`,
    `- **Trigger:** ${pb.trigger || "—"}`,
    "",
  ];
  if ((pb.preconditions || []).length) {
    lines.push("## Preconditions");
    for (const p of pb.preconditions) lines.push(`- [ ] ${p}`);
    lines.push("");
  }
  if ((pb.steps || []).length) {
    lines.push("## Operational Checklist Steps");
    pb.steps.forEach((s, idx) => lines.push(`${idx + 1}. [ ] ${s}`));
    lines.push("");
  }
  if ((pb.expected_outputs || []).length) {
    lines.push("## Expected Outputs");
    for (const o of pb.expected_outputs) lines.push(`- ${o}`);
    lines.push("");
  }
  if ((pb.failure_recovery || []).length) {
    lines.push("## Failure Recovery & Rollback");
    for (const r of pb.failure_recovery) lines.push(`- ${r}`);
    lines.push("");
  }
  return lines.join("\n");
}

function formatAtomProofMarkdown(filename, atom, proof) {
  const lines = [
    `# Verified Atom: ${filename}`,
    "",
    `- **Review Status:** \`${atom?.review_status || "accepted"}\``,
    `- **Memory Type:** \`${atom?.memory_type || "semantic"}\``,
    `- **Origin:** \`${atom?.origin || "vault"}\``,
    `- **File SHA-256:** \`${proof?.file_sha256 || atom?.sha256 || ""}\``,
  ];
  if (Array.isArray(atom?.sources) && atom.sources.length) {
    lines.push(`- **Sources:** ${atom.sources.map((s) => `\`${s}\``).join(", ")}`);
  }
  lines.push("");

  if (atom?.claim) {
    lines.push("## Claim", atom.claim, "");
  }
  if (atom?.evidence) {
    lines.push("## Anchored Evidence", `> ${atom.evidence.replace(/\r?\n/g, "\n> ")}`, "");
  }

  if (proof && proof.merkle_root) {
    lines.push(
      "## Cryptographic Merkle Inclusion Proof",
      `- **Leaf Path:** \`${proof.rel_path || ""}\``,
      `- **Leaf SHA-256:** \`${proof.leaf_sha256 || ""}\``,
      `- **Live Vault Merkle Root:** \`${proof.merkle_root}\``,
      `- **Ledger Head Entry SHA-256:** \`${proof.ledger_head_entry_sha256 || "(uncommitted)"}\``,
      ""
    );
    const sibs = proof.siblings || [];
    if (sibs.length) {
      lines.push("| Level | Position | Sibling SHA-256 |", "| :--- | :--- | :--- |");
      sibs.forEach((s, i) => {
        lines.push(`| ${i} | \`${s.position}\` | \`${s.sha256}\` |`);
      });
      lines.push("");
    } else {
      lines.push("_Single-leaf Merkle tree (0 sibling hashes required)._", "");
    }
    lines.push("### Portable Proof Bundle", "```json", JSON.stringify(proof, null, 2), "```", "");
  }

  return lines.join("\n");
}

class VaultTreeProvider {
  constructor(onRefreshCallback = null) {
    this._onDidChangeTreeData = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._onDidChangeTreeData.event;
    this._onRefreshCallback = onRefreshCallback;
  }
  refresh() {
    this._onDidChangeTreeData.fire();
    if (typeof this._onRefreshCallback === "function") {
      this._onRefreshCallback();
    }
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
        new vscode.TreeItem(
          "Contradiction & Drift Radar",
          vscode.TreeItemCollapsibleState.Collapsed
        ),
      ];
    }
    try {
      if (element.label === "Notes") {
        const res = await apiRequest("GET", "/v1/notes?limit=60");
        if (!res.ok) {
          return [new vscode.TreeItem("API offline — start Second Brain desktop or CLI")];
        }
        const notes = res.data?.items || res.data?.notes || [];
        if (!notes.length) return [new vscode.TreeItem("No working notes yet")];
        return notes.map((n) => {
          const item = new vscode.TreeItem(
            n.title || n.filename,
            vscode.TreeItemCollapsibleState.None
          );
          item.description = (n.tags || []).join(", ");
          item.tooltip = `${n.filename} — click to open preview`;
          item.iconPath = new vscode.ThemeIcon("note");
          item.command = {
            command: "secondBrain.openTreeItem",
            title: "Open Note",
            arguments: [{ kind: "note", filename: n.filename }],
          };
          return item;
        });
      }

      if (element.label === "Verified Atoms") {
        const res = await apiRequest("GET", "/v1/atoms?limit=60");
        if (!res.ok) return [new vscode.TreeItem("API offline")];
        const atoms = res.data?.items || res.data?.atoms || [];
        if (!atoms.length) return [new vscode.TreeItem("No verified atoms yet")];
        return atoms.map((a) => {
          const item = new vscode.TreeItem(
            a.filename,
            vscode.TreeItemCollapsibleState.None
          );
          item.description = a.review_status || a.status || "";
          item.tooltip = a.claim || a.filename;
          item.iconPath = new vscode.ThemeIcon("shield");
          item.command = {
            command: "secondBrain.openTreeItem",
            title: "Open Verified Atom",
            arguments: [{ kind: "atom", filename: a.filename, atom: a }],
          };
          return item;
        });
      }

      if (element.label === "Playbooks") {
        const res = await apiRequest("GET", "/v1/playbooks");
        if (!res.ok) return [new vscode.TreeItem("API offline")];
        const pbs = res.data?.playbooks || [];
        if (!pbs.length) return [new vscode.TreeItem("No promoted playbooks yet")];
        return pbs.map((pb) => {
          const item = new vscode.TreeItem(
            pb.title || pb.filename,
            vscode.TreeItemCollapsibleState.None
          );
          item.description = pb.status || "accepted";
          item.tooltip = pb.trigger || pb.filename;
          item.iconPath = new vscode.ThemeIcon("checklist");
          item.command = {
            command: "secondBrain.openTreeItem",
            title: "Open Playbook Checklist",
            arguments: [{ kind: "playbook", playbook: pb }],
          };
          return item;
        });
      }

      if (element.label === "Snapshots") {
        const res = await apiRequest("GET", "/v1/snapshots");
        if (!res.ok) return [new vscode.TreeItem("API offline")];
        const snaps = res.data?.snapshots || [];
        if (!snaps.length) return [new vscode.TreeItem("No snapshots recorded yet")];
        return snaps.map((s) => {
          const item = new vscode.TreeItem(
            s.snapshot_id,
            vscode.TreeItemCollapsibleState.None
          );
          item.description = `${s.atom_count || 0} atoms${s.label ? " · " + s.label : ""}`;
          item.tooltip = `Merkle: ${s.merkle_root || ""} — click to diff vs live`;
          item.iconPath = new vscode.ThemeIcon("history");
          item.command = {
            command: "secondBrain.openTreeItem",
            title: "Diff Snapshot vs Live",
            arguments: [{ kind: "snapshot", snapshot_id: s.snapshot_id }],
          };
          return item;
        });
      }

      if (element.label === "Contradiction & Drift Radar") {
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
            const label = `[${String(al.severity || "medium").toUpperCase()}] ${
              al.atom_a?.filename || ""
            } vs ${al.atom_b?.filename || ""}`;
            const item = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
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

function buildChatWebviewHtml(nonce) {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';" />
  <style>
    * { box-sizing: border-box; }
    body {
      font-family: var(--vscode-font-family, sans-serif);
      font-size: var(--vscode-font-size, 13px);
      background: var(--vscode-editor-background);
      color: var(--vscode-editor-foreground);
      padding: 14px;
      margin: 0;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    h3 { margin: 0 0 2px; font-size: 15px; }
    .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    input, select {
      background: var(--vscode-input-background);
      color: var(--vscode-input-foreground);
      border: 1px solid var(--vscode-input-border, var(--vscode-panel-border, #444));
      border-radius: 5px;
      padding: 6px 9px;
      font-family: inherit;
      font-size: 12.5px;
    }
    button {
      background: var(--vscode-button-background);
      color: var(--vscode-button-foreground);
      border: none;
      border-radius: 5px;
      padding: 6px 12px;
      cursor: pointer;
      font-size: 12.5px;
      font-weight: 500;
    }
    button:hover { background: var(--vscode-button-hoverBackground); }
    button.secondary {
      background: var(--vscode-button-secondaryBackground, var(--vscode-sideBar-background));
      color: var(--vscode-button-secondaryForeground, var(--vscode-editor-foreground));
      border: 1px solid var(--vscode-panel-border, #444);
    }
    .status {
      font-size: 11.5px;
      color: var(--vscode-descriptionForeground);
      padding: 5px 9px;
      border-radius: 5px;
      background: var(--vscode-sideBar-background);
      border: 1px solid var(--vscode-panel-border, transparent);
    }
    .answer-box {
      white-space: pre-wrap;
      line-height: 1.55;
      background: var(--vscode-textCodeBlock-background, var(--vscode-sideBar-background));
      border: 1px solid var(--vscode-panel-border, #444);
      padding: 12px;
      border-radius: 6px;
      min-height: 140px;
    }
    .grounding {
      padding: 8px 10px;
      border-radius: 6px;
      font-size: 12px;
      border: 1px solid var(--vscode-panel-border, #444);
      background: var(--vscode-sideBar-background);
      display: none;
    }
    .grounding.ok { border-left: 3px solid var(--vscode-testing-iconPassed, #2ea043); }
    .grounding.warn { border-left: 3px solid var(--vscode-editorWarning-foreground, #d29922); }
    .sources {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .src-card {
      padding: 8px 10px;
      border-radius: 6px;
      background: var(--vscode-sideBar-background);
      border: 1px solid var(--vscode-panel-border, #444);
      font-size: 12px;
    }
    .src-head { font-weight: 600; margin-bottom: 3px; }
    .src-meta { font-size: 11px; color: var(--vscode-descriptionForeground); margin-bottom: 3px; }
  </style>
</head>
<body>
  <h3>Second Brain Grounded Chat</h3>
  <div class="row">
    <input id="q" style="flex:1; min-width:180px;" placeholder="Ask your vault... (Press Enter)" />
    <select id="m">
      <option value="stream">Stream</option>
      <option value="verified">Verified</option>
      <option value="instant">Instant</option>
    </select>
    <input id="scope" style="width:135px;" placeholder="Scope (optional)" />
    <button id="go" type="button">Ask</button>
    <button id="warmup" class="secondary" type="button">Warmup</button>
  </div>
  <div id="status" class="status">Ready — retrieved [V#]/[N#]/[P#] sources appear immediately.</div>
  <div id="out" class="answer-box">Ask a question above to search verified atoms and working notes.</div>
  <div id="grounding" class="grounding"></div>
  <div style="font-weight:600; font-size:12px;">Retrieved Vault Sources</div>
  <div id="sources" class="sources"></div>

  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    const qEl = document.getElementById("q");
    const mEl = document.getElementById("m");
    const scopeEl = document.getElementById("scope");
    const outEl = document.getElementById("out");
    const statusEl = document.getElementById("status");
    const groundingEl = document.getElementById("grounding");
    const sourcesEl = document.getElementById("sources");

    function sendQuery() {
      const query = qEl.value.trim();
      if (!query) return;
      outEl.textContent = "Retrieving...";
      statusEl.textContent = "Querying loopback Memory API...";
      groundingEl.style.display = "none";
      groundingEl.replaceChildren();
      sourcesEl.replaceChildren();
      vscode.postMessage({
        action: "ask",
        query,
        mode: mEl.value,
        project_scope: scopeEl.value.trim() || undefined,
      });
    }

    document.getElementById("go").addEventListener("click", sendQuery);
    qEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter") sendQuery();
    });
    document.getElementById("warmup").addEventListener("click", () => {
      statusEl.textContent = "Warming up local Ollama model...";
      vscode.postMessage({ action: "warmup" });
    });

    function renderSources(list) {
      sourcesEl.replaceChildren();
      if (!list || !list.length) {
        const empty = document.createElement("div");
        empty.className = "src-meta";
        empty.textContent = "No matching sources returned.";
        sourcesEl.appendChild(empty);
        return;
      }
      for (const s of list) {
        const card = document.createElement("div");
        card.className = "src-card";
        const head = document.createElement("div");
        head.className = "src-head";
        const tag = s.citation_id ? "[" + s.citation_id + "] " : "";
        head.textContent = tag + (s.title || s.filename || "Source");
        const meta = document.createElement("div");
        meta.className = "src-meta";
        meta.textContent = (s.tier || "memory") + (s.filename ? " · " + s.filename : "");
        const body = document.createElement("div");
        body.textContent = s.claim || s.excerpt || "";
        card.appendChild(head);
        card.appendChild(meta);
        card.appendChild(body);
        sourcesEl.appendChild(card);
      }
    }

    function renderGrounding(g) {
      groundingEl.replaceChildren();
      groundingEl.style.display = "block";
      groundingEl.className = "grounding " + (g.fully_cited ? "ok" : "warn");
      const title = document.createElement("strong");
      title.textContent = g.fully_cited
        ? "Grounding Audit Passed (Fully Cited)"
        : "Grounding Audit Warning";
      groundingEl.appendChild(title);

      const detail = document.createElement("div");
      const cited = (g.cited_sources || []).map((c) => "[" + c + "]").join(" ");
      const invalid = (g.invalid_citations || []).map((c) => "[" + c + "]").join(", ");
      const uncitedCount = (g.uncited_sentences || []).length;
      const withheldCount = Number(
        g.withheld_claims ??
          g.withheld_unsupported_claims ??
          g.unsupported_claim_count ??
          0
      );
      const parts = [];
      if (cited) parts.push("Cited: " + cited);
      if (invalid) parts.push("Invalid citations: " + invalid);
      if (uncitedCount) parts.push("Uncited sentences: " + uncitedCount);
      if (withheldCount > 0) parts.push("Withheld unsupported claims: " + withheldCount);
      detail.textContent = parts.join(" · ") || "No citations detected.";
      groundingEl.appendChild(detail);

      if (uncitedCount) {
        const ul = document.createElement("ul");
        for (const s of g.uncited_sentences) {
          const li = document.createElement("li");
          li.textContent = s;
          ul.appendChild(li);
        }
        groundingEl.appendChild(ul);
      }
    }

    window.addEventListener("message", (e) => {
      const msg = e.data || {};
      if (msg.type === "status") statusEl.textContent = msg.text || "";
      else if (msg.type === "reset") outEl.textContent = msg.text || "";
      else if (msg.type === "append") outEl.textContent += msg.text || "";
      else if (msg.type === "error") {
        statusEl.textContent = msg.text || "Request failed";
        outEl.textContent = msg.text || "Error";
      } else if (msg.type === "sources") {
        renderSources(msg.sources || []);
      } else if (msg.type === "grounding") {
        renderGrounding(msg.grounding || {});
      }
    });
  </script>
</body>
</html>`;
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
        statusBarItem.tooltip = `Second Brain Online (127.0.0.1:${getPort()}) · ${approved} approved atoms · Tier: ${
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
  vscode.window.registerTreeDataProvider("secondBrain.vaultView", treeProvider);

  updateStatusBar();
  const pollTimer = setInterval(updateStatusBar, 30000);
  context.subscriptions.push({ dispose: () => clearInterval(pollTimer) });

  context.subscriptions.push(
    vscode.commands.registerCommand("secondBrain.refreshVault", () => {
      treeProvider.refresh();
    }),

    vscode.commands.registerCommand("secondBrain.openTreeItem", async (payload) => {
      if (!payload || typeof payload !== "object") return;
      try {
        if (payload.kind === "note" && payload.filename) {
          const [noteRes, blRes] = await Promise.all([
            apiRequest("GET", `/v1/notes/${encodeURIComponent(payload.filename)}`),
            apiRequest(
              "GET",
              `/v1/notes/${encodeURIComponent(payload.filename)}/backlinks`
            ),
          ]);
          if (!noteRes.ok) {
            vscode.window.showErrorMessage(
              `Could not load note: ${noteRes.data?.error || noteRes.status}`
            );
            return;
          }
          const n = noteRes.data?.note || noteRes.data || {};
          const backlinks = blRes.ok
            ? blRes.data?.items || blRes.data?.backlinks || []
            : [];
          const header = [
            `<!-- Note: ${payload.filename} | SHA-256: ${String(n.sha256 || "").slice(0, 12)} -->`,
            backlinks.length
              ? `<!-- Backlinks: ${backlinks.map((b) => b.filename).join(", ")} -->`
              : "",
          ]
            .filter(Boolean)
            .join("\n");
          await openMarkdownPreview(`${header}\n\n${n.body || ""}`);
          return;
        }

        if (payload.kind === "atom" && payload.filename) {
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
        vscode.window.showErrorMessage(
          `Second Brain API error: ${err.message}`
        );
      }
    }),

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
          placeHolder: "Select a verified atom or note to preview",
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
      try {
        const healthCheck = await apiRequest("GET", "/v1/health");
        if (healthCheck.status !== 200) {
          vscode.window.showErrorMessage(
            `Second Brain API unavailable (${healthCheck.status}): ${
              healthCheck.data?.error || "Check API authentication or start Second Brain."
            }`
          );
          return;
        }
      } catch (err) {
        vscode.window.showErrorMessage(
          `Second Brain API unreachable on 127.0.0.1:${getPort()}: ${err.message}`
        );
        return;
      }

      const nonce = crypto.randomBytes(16).toString("base64");
      const panel = vscode.window.createWebviewPanel(
        "secondBrainChat",
        "Second Brain Chat",
        vscode.ViewColumn.Beside,
        { enableScripts: true }
      );
      panel.webview.html = buildChatWebviewHtml(nonce);

      panel.webview.onDidReceiveMessage(async (msg) => {
        if (!msg || typeof msg !== "object") return;

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

        const { query, mode, project_scope } = msg;
        if (!query) return;
        const port = getPort();
        const token = readBearerToken();
        const ownerToken = readOwnerToken();
        const body = JSON.stringify({ query, mode, project_scope });
        const streamHeaders = {
          Host: `127.0.0.1:${port}`,
          Accept: "text/event-stream",
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body, "utf8"),
        };
        if (token) streamHeaders["Authorization"] = `Bearer ${token}`;
        if (ownerToken) streamHeaders["X-Owner-Token"] = ownerToken;

        const req = http.request(
          {
            hostname: "127.0.0.1",
            port,
            path: "/v1/chat/stream",
            method: "POST",
            headers: streamHeaders,
          },
          (res) => {
            res.setEncoding("utf8");
            if (res.statusCode !== 200) {
              let errRaw = "";
              res.on("data", (c) => (errRaw += c));
              res.on("end", () => {
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
            res.on("data", (chunk) => {
              buf += chunk;
              const lines = buf.split(/\r?\n/);
              buf = lines.pop() || "";
              for (const line of lines) {
                if (line.startsWith("event:")) {
                  ev = line.slice(6).trim();
                } else if (line.startsWith("data:")) {
                  try {
                    const data = JSON.parse(line.slice(5).trim());
                    if (ev === "context") {
                      const sources = data.sources || [];
                      panel.webview.postMessage({ type: "sources", sources });
                      panel.webview.postMessage({
                        type: "status",
                        text: `Retrieved ${sources.length} source(s) in ${
                          data.retrieval_ms ?? "?"
                        } ms${data.warning ? " · " + data.warning : ""}`,
                      });
                      if (mode === "instant") {
                        const summary = sources
                          .map(
                            (s) =>
                              `[${s.citation_id || s.tier}] ${
                                s.title || s.filename
                              }: ${s.claim || s.excerpt || ""}`
                          )
                          .join("\n\n");
                        panel.webview.postMessage({
                          type: "reset",
                          text: summary || "No matching sources found.",
                        });
                      }
                    } else if (ev === "token") {
                      panel.webview.postMessage({
                        type: "append",
                        text: data.text ?? data.token ?? "",
                      });
                    } else if (ev === "verified_answer") {
                      if (!data.answerable) {
                        panel.webview.postMessage({
                          type: "reset",
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
                                  `  - ${e.atom_id}: "${e.quote || e.evidence_quote || ""}"`
                              )
                              .join("\n");
                            return `${idx + 1}. ${c.text}${cites ? "\n" + cites : ""}`;
                          })
                          .join("\n\n");
                        panel.webview.postMessage({
                          type: "reset",
                          text: formattedClaims || data.text || "",
                        });
                      }
                    } else if (ev === "grounding") {
                      panel.webview.postMessage({
                        type: "grounding",
                        grounding: data,
                      });
                    } else if (ev === "abstain") {
                      panel.webview.postMessage({
                        type: "reset",
                        text: `Abstained: ${data.reason || data.text || ""}`,
                      });
                    } else if (ev === "error") {
                      panel.webview.postMessage({
                        type: "error",
                        text: `Error: ${data.error || "Stream aborted"}`,
                      });
                    } else if (ev === "done") {
                      const ms = data.elapsed_ms ?? data.total_ms;
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
              }
            });
          }
        );
        req.setTimeout(90000, () => {
          req.destroy(new Error("Stream timed out after 90 seconds."));
        });
        req.on("error", (err) =>
          panel.webview.postMessage({
            type: "error",
            text: `Error: ${err.message}`,
          })
        );
        req.write(body);
        req.end();
      });
    }),

    vscode.commands.registerCommand("secondBrain.assemblePlan", async () => {
      const goal = await vscode.window.showInputBox({
        prompt: "Enter task goal for grounded Executive Plan",
      });
      if (!goal) return;
      const scope = await vscode.window.showInputBox({
        prompt: "Optional project scope (e.g. project:alpha) — leave blank for default",
        placeHolder: "project:alpha",
      });
      if (scope === undefined) return;
      try {
        const res = await apiRequest("POST", "/v1/plan", {
          goal,
          project_scope: scope.trim() || undefined,
          mode: "deterministic",
        });
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Executive plan failed: ${res.data?.error || res.status}`
          );
          return;
        }
        await openMarkdownPreview(formatPlanMarkdown(res.data, goal));
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
        const syncState = d.synced_with_head ? "synced w/ head" : "uncommitted live changes";
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

    vscode.commands.registerCommand("secondBrain.proveAtom", async () => {
      try {
        const atomsRes = await apiRequest("GET", "/v1/atoms?limit=100");
        if (!atomsRes.ok) {
          vscode.window.showErrorMessage(
            `Could not list atoms: ${atomsRes.data?.error || atomsRes.status}`
          );
          return;
        }
        const verified = (atomsRes.data?.items || []).filter(
          (a) => a.verified || a.review_status === "accepted"
        );
        if (!verified.length) {
          vscode.window.showInformationMessage(
            "No approved atoms in vault yet to generate a Merkle proof."
          );
          return;
        }
        const picked = await vscode.window.showQuickPick(
          verified.map((a) => ({
            label: a.filename,
            description: String(a.sha256 || "").slice(0, 12),
            detail: a.claim || "",
            atom: a,
          })),
          { placeHolder: "Select an approved atom to generate a Merkle inclusion proof" }
        );
        if (!picked) return;
        const proofRes = await apiRequest(
          "GET",
          `/v1/ledger/prove/${encodeURIComponent(picked.label)}`
        );
        if (!proofRes.ok) {
          vscode.window.showErrorMessage(
            `Merkle proof failed: ${proofRes.data?.error || proofRes.status}`
          );
          return;
        }
        await openMarkdownPreview(
          formatAtomProofMarkdown(picked.label, picked.atom, proofRes.data)
        );
      } catch (err) {
        vscode.window.showErrorMessage(
          `Second Brain API unreachable: ${err.message}`
        );
      }
    }),

    vscode.commands.registerCommand("secondBrain.scanContradictions", async () => {
      try {
        const res = await apiRequest("GET", "/v1/graph/contradictions");
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

    vscode.commands.registerCommand("secondBrain.diffSnapshot", async () => {
      try {
        const snapRes = await apiRequest("GET", "/v1/snapshots");
        if (!snapRes.ok) {
          vscode.window.showErrorMessage(
            `Could not list snapshots: ${snapRes.data?.error || snapRes.status}`
          );
          return;
        }
        const snaps = snapRes.data?.snapshots || [];
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
        const diffRes = await apiRequest("POST", "/v1/snapshots/diff", {
          from_snapshot: picked.label,
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

    vscode.commands.registerCommand("secondBrain.recordExperience", async () => {
      const goal = await vscode.window.showInputBox({
        prompt: "What task goal was attempted?",
        placeHolder: "e.g. Migrate authentication middleware to strict token validation",
      });
      if (!goal) return;
      const result_summary = await vscode.window.showInputBox({
        prompt: "Summarize the outcome & verification",
        placeHolder: "All 42 integration tests passed with zero regressions",
      });
      if (!result_summary) return;
      const outcomePick = await vscode.window.showQuickPick(
        [
          { label: "Succeeded", success: true },
          { label: "Failed / Rolled Back", success: false },
        ],
        { placeHolder: "Did the task succeed?" }
      );
      if (!outcomePick) return;
      const lesson = await vscode.window.showInputBox({
        prompt: "Optional key lesson learned",
        placeHolder: "Always pin schema version before migration...",
      });
      if (lesson === undefined) return;
      try {
        const res = await apiRequest("POST", "/v1/cognitive/experiences", {
          goal: goal.trim(),
          result_summary: result_summary.trim(),
          success: outcomePick.success,
          lessons: lesson.trim() ? [lesson.trim()] : [],
        });
        if (!res.ok) {
          vscode.window.showErrorMessage(
            `Record experience failed: ${res.data?.error || res.status}`
          );
          return;
        }
        vscode.window.showInformationMessage(
          `Recorded episodic experience: ${res.data?.filename || res.data?.experience_id || "saved"}`
        );
      } catch (err) {
        vscode.window.showErrorMessage(
          `Second Brain API unreachable: ${err.message}`
        );
      }
    })
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
