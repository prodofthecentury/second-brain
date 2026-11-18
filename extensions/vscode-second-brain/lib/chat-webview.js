"use strict";

function parseSafeExternalUrl(rawUrl) {
  if (typeof rawUrl !== "string") return null;
  const trimmed = rawUrl.trim();
  if (!trimmed || trimmed.length > 2048) return null;
  try {
    const parsed = new URL(trimmed);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      return parsed.toString();
    }
    return null;
  } catch {
    return null;
  }
}

function appendInlineMarkdown(doc, parent, rawText, hooks) {
  const text = String(rawText ?? "");
  if (!text) return;

  const tokenRe =
    /`([^`\n]+)`|\[([VNPL]\d+)\]|\[([^\]]+)\]\(([^)\s]+)\)|\*\*\*([^*]+)\*\*\*|\*\*([^*]+)\*\*|__([^_]+)__|\*([^*\n]+)\*|(?<![A-Za-z0-9])_([^_\n]+)_(?![A-Za-z0-9])/g;

  let lastIdx = 0;
  let match;
  while ((match = tokenRe.exec(text)) !== null) {
    if (match.index > lastIdx) {
      parent.appendChild(doc.createTextNode(text.slice(lastIdx, match.index)));
    }

    if (match[1] !== undefined) {
      const codeEl = doc.createElement("code");
      codeEl.className = "md-inline-code";
      codeEl.textContent = match[1];
      parent.appendChild(codeEl);
    } else if (match[2] !== undefined) {
      const citeId = match[2];
      const prefix = citeId.charAt(0).toLowerCase();
      const badge = doc.createElement("button");
      badge.type = "button";
      badge.className = "cite-badge cite-" + prefix;
      badge.textContent = "[" + citeId + "]";
      if (badge.dataset) badge.dataset.citationId = citeId;
      badge.addEventListener("click", (ev) => {
        if (ev && typeof ev.stopPropagation === "function") ev.stopPropagation();
        if (hooks && typeof hooks.onCiteClick === "function") {
          hooks.onCiteClick(citeId);
        }
      });
      parent.appendChild(badge);
    } else if (match[3] !== undefined && match[4] !== undefined) {
      const label = match[3];
      const rawHref = match[4];
      const safeUrl = parseSafeExternalUrl(rawHref);
      if (safeUrl) {
        const link = doc.createElement("a");
        link.href = "#";
        link.className = "md-link";
        link.textContent = label;
        if (link.dataset) link.dataset.url = safeUrl;
        link.addEventListener("click", (ev) => {
          if (ev && typeof ev.preventDefault === "function") ev.preventDefault();
          if (hooks && typeof hooks.onLinkClick === "function") {
            hooks.onLinkClick(safeUrl);
          }
        });
        parent.appendChild(link);
      } else {
        parent.appendChild(doc.createTextNode(label + " (" + rawHref + ")"));
      }
    } else if (match[5] !== undefined) {
      const strong = doc.createElement("strong");
      const em = doc.createElement("em");
      appendInlineMarkdown(doc, em, match[5], hooks);
      strong.appendChild(em);
      parent.appendChild(strong);
    } else if (match[6] !== undefined || match[7] !== undefined) {
      const strong = doc.createElement("strong");
      appendInlineMarkdown(doc, strong, match[6] ?? match[7], hooks);
      parent.appendChild(strong);
    } else if (match[8] !== undefined || match[9] !== undefined) {
      const em = doc.createElement("em");
      appendInlineMarkdown(doc, em, match[8] ?? match[9], hooks);
      parent.appendChild(em);
    }

    lastIdx = tokenRe.lastIndex;
  }

  if (lastIdx < text.length) {
    parent.appendChild(doc.createTextNode(text.slice(lastIdx)));
  }
}

function splitTableRowCells(line) {
  let trimmed = line.trim();
  if (trimmed.startsWith("|")) trimmed = trimmed.slice(1);
  if (trimmed.endsWith("|")) trimmed = trimmed.slice(0, -1);
  return trimmed.split("|").map((c) => c.trim());
}

function isTableSeparatorLine(line) {
  if (!line || !line.includes("|") || !line.includes("-")) return false;
  const cells = splitTableRowCells(line);
  if (!cells.length) return false;
  return cells.every((c) => /^:?-{3,}:?$/.test(c));
}

function renderMarkdownDom(doc, container, markdownText, hooks) {
  container.replaceChildren();
  const normalized = String(markdownText ?? "").replace(/\r\n/g, "\n");
  if (!normalized.trim()) {
    return;
  }

  const lines = normalized.split("\n");
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) {
      i += 1;
      continue;
    }

    const fenceMatch = line.match(/^\s*```([A-Za-z0-9_+.-]*)\s*$/);
    if (fenceMatch) {
      const lang = fenceMatch[1] || "";
      const codeLines = [];
      i += 1;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
        codeLines.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1;

      const pre = doc.createElement("pre");
      pre.className = "md-pre";
      if (lang) {
        const langBadge = doc.createElement("div");
        langBadge.className = "md-code-lang";
        langBadge.textContent = lang;
        pre.appendChild(langBadge);
      }
      const code = doc.createElement("code");
      code.textContent = codeLines.join("\n");
      pre.appendChild(code);
      container.appendChild(pre);
      continue;
    }

    const headingMatch = line.match(/^(#{1,6})\s+(.*)$/);
    if (headingMatch) {
      const level = Math.min(6, headingMatch[1].length);
      const hEl = doc.createElement("h" + level);
      hEl.className = "md-h" + level;
      appendInlineMarkdown(doc, hEl, headingMatch[2].trim(), hooks);
      container.appendChild(hEl);
      i += 1;
      continue;
    }

    if (/^\s*(?:---|\*\*\*|___)\s*$/.test(line)) {
      container.appendChild(doc.createElement("hr"));
      i += 1;
      continue;
    }

    if (
      line.includes("|") &&
      i + 1 < lines.length &&
      isTableSeparatorLine(lines[i + 1])
    ) {
      const headers = splitTableRowCells(line);
      const aligns = splitTableRowCells(lines[i + 1]).map((cell) => {
        if (cell.startsWith(":") && cell.endsWith(":")) return "center";
        if (cell.endsWith(":")) return "right";
        return "left";
      });
      i += 2;

      const table = doc.createElement("table");
      table.className = "md-table";
      const thead = doc.createElement("thead");
      const headRow = doc.createElement("tr");
      headers.forEach((hText, colIdx) => {
        const th = doc.createElement("th");
        if (aligns[colIdx] && th.style) th.style.textAlign = aligns[colIdx];
        appendInlineMarkdown(doc, th, hText, hooks);
        headRow.appendChild(th);
      });
      thead.appendChild(headRow);
      table.appendChild(thead);

      const tbody = doc.createElement("tbody");
      while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
        const rowCells = splitTableRowCells(lines[i]);
        const tr = doc.createElement("tr");
        for (let colIdx = 0; colIdx < headers.length; colIdx++) {
          const td = doc.createElement("td");
          if (aligns[colIdx] && td.style) td.style.textAlign = aligns[colIdx];
          appendInlineMarkdown(doc, td, rowCells[colIdx] ?? "", hooks);
          tr.appendChild(td);
        }
        tbody.appendChild(tr);
        i += 1;
      }
      table.appendChild(tbody);
      container.appendChild(table);
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const quoteLines = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quoteLines.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      const bq = doc.createElement("blockquote");
      bq.className = "md-quote";
      appendInlineMarkdown(doc, bq, quoteLines.join("\n"), hooks);
      container.appendChild(bq);
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      const ul = doc.createElement("ul");
      ul.className = "md-ul";
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        const itemBody = lines[i].replace(/^\s*[-*+]\s+/, "");
        const li = doc.createElement("li");
        const taskMatch = itemBody.match(/^\[([ xX])\]\s+(.*)$/);
        if (taskMatch) {
          li.className = "md-task-item";
          const cb = doc.createElement("input");
          cb.type = "checkbox";
          cb.disabled = true;
          cb.checked = taskMatch[1].toLowerCase() === "x";
          li.appendChild(cb);
          const span = doc.createElement("span");
          appendInlineMarkdown(doc, span, taskMatch[2], hooks);
          li.appendChild(span);
        } else {
          appendInlineMarkdown(doc, li, itemBody, hooks);
        }
        ul.appendChild(li);
        i += 1;
      }
      container.appendChild(ul);
      continue;
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      const ol = doc.createElement("ol");
      ol.className = "md-ol";
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
        const itemBody = lines[i].replace(/^\s*\d+[.)]\s+/, "");
        const li = doc.createElement("li");
        appendInlineMarkdown(doc, li, itemBody, hooks);
        ol.appendChild(li);
        i += 1;
      }
      container.appendChild(ol);
      continue;
    }

    const paraLines = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^\s*```/.test(lines[i]) &&
      !/^(#{1,6})\s+/.test(lines[i]) &&
      !/^\s*(?:---|\*\*\*|___)\s*$/.test(lines[i]) &&
      !/^\s*>\s?/.test(lines[i]) &&
      !/^\s*[-*+]\s+/.test(lines[i]) &&
      !/^\s*\d+[.)]\s+/.test(lines[i]) &&
      !(
        lines[i].includes("|") &&
        i + 1 < lines.length &&
        isTableSeparatorLine(lines[i + 1])
      )
    ) {
      paraLines.push(lines[i]);
      i += 1;
    }

    const p = doc.createElement("p");
    p.className = "md-p";
    paraLines.forEach((pLine, idx) => {
      if (idx > 0) p.appendChild(doc.createElement("br"));
      appendInlineMarkdown(doc, p, pLine, hooks);
    });
    container.appendChild(p);
  }
}

function buildChatWebviewHtml(nonce) {
  const safeHelpersSource = [
    parseSafeExternalUrl.toString(),
    appendInlineMarkdown.toString(),
    splitTableRowCells.toString(),
    isTableSeparatorLine.toString(),
    renderMarkdownDom.toString(),
  ].join("\n\n");

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
    .ctrl-label {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 11.5px;
      color: var(--vscode-descriptionForeground);
    }
    input, select {
      background: var(--vscode-input-background);
      color: var(--vscode-input-foreground);
      border: 1px solid var(--vscode-input-border, var(--vscode-panel-border, #444));
      border-radius: 5px;
      padding: 5px 8px;
      font-family: inherit;
      font-size: 12px;
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
    button:disabled { opacity: 0.55; cursor: default; }
    button.secondary {
      background: var(--vscode-button-secondaryBackground, var(--vscode-sideBar-background));
      color: var(--vscode-button-secondaryForeground, var(--vscode-editor-foreground));
      border: 1px solid var(--vscode-panel-border, #444);
    }
    button.stop-btn {
      border-color: var(--vscode-errorForeground, #f85149);
      color: var(--vscode-errorForeground, #f85149);
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
      line-height: 1.55;
      background: var(--vscode-textCodeBlock-background, var(--vscode-sideBar-background));
      border: 1px solid var(--vscode-panel-border, #444);
      padding: 12px;
      border-radius: 6px;
      min-height: 140px;
      overflow-x: auto;
    }
    .md-p { margin: 0 0 8px; }
    .md-p:last-child { margin-bottom: 0; }
    .md-h1, .md-h2, .md-h3, .md-h4, .md-h5, .md-h6 { margin: 8px 0 6px; }
    .md-pre {
      background: var(--vscode-editor-background);
      border: 1px solid var(--vscode-panel-border, #444);
      border-radius: 5px;
      padding: 8px 10px;
      overflow-x: auto;
      margin: 6px 0;
    }
    .md-code-lang {
      font-size: 10.5px;
      color: var(--vscode-descriptionForeground);
      margin-bottom: 4px;
      text-transform: uppercase;
    }
    .md-inline-code {
      background: var(--vscode-editor-background);
      padding: 1px 4px;
      border-radius: 3px;
      font-family: var(--vscode-editor-font-family, monospace);
      font-size: 12px;
    }
    .md-quote {
      margin: 6px 0;
      padding: 4px 10px;
      border-left: 3px solid var(--vscode-textBlockQuote-border, #58a6ff);
      color: var(--vscode-descriptionForeground);
    }
    .md-table {
      border-collapse: collapse;
      width: 100%;
      margin: 8px 0;
      font-size: 12px;
    }
    .md-table th, .md-table td {
      border: 1px solid var(--vscode-panel-border, #444);
      padding: 5px 8px;
    }
    .md-table th {
      background: var(--vscode-sideBar-background);
      font-weight: 600;
    }
    .md-task-item {
      list-style: none;
      display: flex;
      align-items: center;
      gap: 6px;
      margin-left: -16px;
    }
    .md-link {
      color: var(--vscode-textLink-foreground, #3794ff);
      text-decoration: underline;
    }
    .cite-badge {
      display: inline-block;
      padding: 1px 5px;
      margin: 0 2px;
      font-size: 11px;
      font-weight: 600;
      border-radius: 4px;
      border: 1px solid var(--vscode-panel-border, #555);
      background: var(--vscode-badge-background, #333);
      color: var(--vscode-badge-foreground, #fff);
      cursor: pointer;
    }
    .cite-badge.cite-l {
      border-color: var(--vscode-editorWarning-foreground, #d29922);
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
    .research-leads {
      padding: 10px 12px;
      border-radius: 6px;
      border: 2px dashed var(--vscode-editorWarning-foreground, #d29922);
      background: var(--vscode-sideBar-background);
      display: none;
    }
    .leads-banner {
      font-weight: 700;
      font-size: 11.5px;
      letter-spacing: 0.4px;
      color: var(--vscode-editorWarning-foreground, #d29922);
      margin-bottom: 6px;
    }
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
      cursor: pointer;
    }
    .src-card:hover {
      border-color: var(--vscode-focusBorder, #3794ff);
    }
    .src-head { font-weight: 600; margin-bottom: 3px; }
    .src-meta { font-size: 11px; color: var(--vscode-descriptionForeground); margin-bottom: 3px; }
  </style>
</head>
<body>
  <h3>Second Brain Grounded Chat</h3>
  <div class="row">
    <input id="q" style="flex:1; min-width:200px;" placeholder="Ask your vault... (Press Enter)" />
    <button id="go" type="button">Ask</button>
    <button id="stop" class="secondary stop-btn" type="button">Stop</button>
    <button id="warmup" class="secondary" type="button">Warmup</button>
  </div>
  <div class="row">
    <label class="ctrl-label">Mode
      <select id="m">
        <option value="stream">stream</option>
        <option value="verified">verified</option>
        <option value="instant">instant</option>
      </select>
    </label>
    <label class="ctrl-label">Retrieval
      <select id="retrievalMode">
        <option value="auto">auto</option>
        <option value="lexical">lexical</option>
        <option value="dense">dense</option>
        <option value="rrf">rrf</option>
      </select>
    </label>
    <label class="ctrl-label">View
      <select id="answerView">
        <option value="standard">standard</option>
        <option value="preference">preference</option>
        <option value="research">research</option>
      </select>
    </label>
    <input id="hostId" style="width:125px;" value="ollama_local" placeholder="Host (ollama_local)" />
    <input id="modelOverride" style="width:135px;" placeholder="Model override" />
    <input id="scope" style="width:125px;" placeholder="Scope (optional)" />
    <label class="ctrl-label"><input id="incPrefs" type="checkbox" checked /> Preferences</label>
  </div>
  <div id="status" class="status">Ready — retrieved [V#]/[N#]/[P#]/[L#] sources appear immediately.</div>
  <div id="out" class="answer-box">Ask a question above to search verified atoms and working notes.</div>
  <div id="grounding" class="grounding"></div>
  <div id="researchLeads" class="research-leads"></div>
  <div style="font-weight:600; font-size:12px;">Retrieved Vault Sources (click card or [V#]/[N#] badge to open)</div>
  <div id="sources" class="sources"></div>

  <script nonce="${nonce}">
    ${safeHelpersSource}

    const vscode = acquireVsCodeApi();
    const qEl = document.getElementById("q");
    const mEl = document.getElementById("m");
    const retrievalModeEl = document.getElementById("retrievalMode");
    const answerViewEl = document.getElementById("answerView");
    const hostIdEl = document.getElementById("hostId");
    const modelOverrideEl = document.getElementById("modelOverride");
    const scopeEl = document.getElementById("scope");
    const incPrefsEl = document.getElementById("incPrefs");
    const stopBtn = document.getElementById("stop");
    const outEl = document.getElementById("out");
    const statusEl = document.getElementById("status");
    const groundingEl = document.getElementById("grounding");
    const researchLeadsEl = document.getElementById("researchLeads");
    const sourcesEl = document.getElementById("sources");

    let assistantMarkdownBuffer = "";
    const sourcesByCitationId = Object.create(null);

    const mdHooks = {
      onCiteClick(citationId) {
        const src = sourcesByCitationId[citationId] || {};
        vscode.postMessage({
          action: "openSource",
          citation_id: citationId,
          tier: src.tier || "",
          filename: src.filename || "",
        });
      },
      onLinkClick(url) {
        vscode.postMessage({
          action: "openExternal",
          url,
        });
      },
    };

    function renderAssistantMarkdown(text) {
      assistantMarkdownBuffer = String(text ?? "");
      if (!assistantMarkdownBuffer.trim()) {
        outEl.replaceChildren();
        return;
      }
      renderMarkdownDom(document, outEl, assistantMarkdownBuffer, mdHooks);
    }

    function sendQuery() {
      const query = qEl.value.trim();
      if (!query) return;
      assistantMarkdownBuffer = "";
      outEl.replaceChildren(document.createTextNode("Retrieving..."));
      statusEl.textContent = "Querying loopback Memory API...";
      groundingEl.style.display = "none";
      groundingEl.replaceChildren();
      researchLeadsEl.style.display = "none";
      researchLeadsEl.replaceChildren();
      sourcesEl.replaceChildren();
      for (const k of Object.keys(sourcesByCitationId)) {
        delete sourcesByCitationId[k];
      }
      vscode.postMessage({
        action: "ask",
        query,
        mode: mEl.value,
        retrieval_mode: retrievalModeEl.value,
        answer_view: answerViewEl.value,
        host_id: (hostIdEl.value || "").trim() || "ollama_local",
        model_override: (modelOverrideEl.value || "").trim() || undefined,
        project_scope: (scopeEl.value || "").trim() || undefined,
        include_preferences: Boolean(incPrefsEl.checked),
      });
    }

    document.getElementById("go").addEventListener("click", sendQuery);
    qEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter") sendQuery();
    });
    stopBtn.addEventListener("click", () => {
      statusEl.textContent = "Stopping stream...";
      vscode.postMessage({ action: "stop" });
    });
    document.getElementById("warmup").addEventListener("click", () => {
      statusEl.textContent = "Warming up local model...";
      vscode.postMessage({ action: "warmup" });
    });

    function renderSources(list) {
      sourcesEl.replaceChildren();
      for (const k of Object.keys(sourcesByCitationId)) {
        delete sourcesByCitationId[k];
      }
      if (!list || !list.length) {
        const empty = document.createElement("div");
        empty.className = "src-meta";
        empty.textContent = "No matching sources returned.";
        sourcesEl.appendChild(empty);
        return;
      }
      for (const s of list) {
        if (s && s.citation_id) {
          sourcesByCitationId[s.citation_id] = s;
        }
        const card = document.createElement("div");
        card.className = "src-card";
        if (card.dataset) {
          card.dataset.citationId = s.citation_id || "";
          card.dataset.filename = s.filename || "";
          card.dataset.tier = s.tier || "";
        }
        const head = document.createElement("div");
        head.className = "src-head";
        if (s.citation_id) {
          const badge = document.createElement("button");
          badge.type = "button";
          badge.className = "cite-badge cite-" + String(s.citation_id).charAt(0).toLowerCase();
          badge.textContent = "[" + s.citation_id + "]";
          head.appendChild(badge);
          head.appendChild(document.createTextNode(" "));
        }
        head.appendChild(document.createTextNode(s.title || s.filename || "Source"));
        const meta = document.createElement("div");
        meta.className = "src-meta";
        meta.textContent = (s.tier || "memory") + (s.filename ? " · " + s.filename : "");
        const body = document.createElement("div");
        body.textContent = s.claim || s.excerpt || "";
        card.appendChild(head);
        card.appendChild(meta);
        card.appendChild(body);
        card.addEventListener("click", () => {
          vscode.postMessage({
            action: "openSource",
            citation_id: s.citation_id || "",
            tier: s.tier || "",
            filename: s.filename || "",
          });
        });
        sourcesEl.appendChild(card);
      }
      if (assistantMarkdownBuffer.trim()) {
        renderMarkdownDom(document, outEl, assistantMarkdownBuffer, mdHooks);
      }
    }

    function renderResearchLeads(payload) {
      researchLeadsEl.replaceChildren();
      researchLeadsEl.style.display = "block";
      const banner = document.createElement("div");
      banner.className = "leads-banner";
      const rawLabel = String(payload.label || "").trim();
      banner.textContent = rawLabel.includes("UNVERIFIED LEADS — NOT OWNER-APPROVED")
        ? rawLabel
        : "UNVERIFIED LEADS — NOT OWNER-APPROVED" + (rawLabel ? " · " + rawLabel : "");
      researchLeadsEl.appendChild(banner);

      if (payload.text) {
        const textWrap = document.createElement("div");
        renderMarkdownDom(document, textWrap, String(payload.text), mdHooks);
        researchLeadsEl.appendChild(textWrap);
      }

      const leads = Array.isArray(payload.leads) ? payload.leads : [];
      for (const lead of leads) {
        if (lead && lead.citation_id && !sourcesByCitationId[lead.citation_id]) {
          sourcesByCitationId[lead.citation_id] = lead;
        }
        const card = document.createElement("div");
        card.className = "src-card";
        const head = document.createElement("div");
        head.className = "src-head";
        const prefix = lead.citation_id ? "[" + lead.citation_id + "] " : "";
        head.textContent = prefix + (lead.title || lead.filename || "Unverified Lead");
        const body = document.createElement("div");
        body.textContent = lead.excerpt || lead.claim || lead.preview || "";
        card.appendChild(head);
        card.appendChild(body);
        if (lead && (lead.filename || lead.citation_id)) {
          card.addEventListener("click", () => {
            vscode.postMessage({
              action: "openSource",
              citation_id: lead.citation_id || "",
              tier: lead.tier || "unverified_lead",
              filename: lead.filename || "",
            });
          });
        }
        researchLeadsEl.appendChild(card);
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
      if (msg.type === "status") {
        statusEl.textContent = msg.text || "";
      } else if (msg.type === "reset") {
        renderAssistantMarkdown(msg.text || "");
      } else if (msg.type === "append") {
        renderAssistantMarkdown(assistantMarkdownBuffer + (msg.text || ""));
      } else if (msg.type === "markdown") {
        renderAssistantMarkdown(msg.text || "");
      } else if (msg.type === "cancelled") {
        statusEl.textContent = msg.text || "Stream cancelled.";
      } else if (msg.type === "error") {
        statusEl.textContent = msg.text || "Request failed";
        if (!assistantMarkdownBuffer.trim()) {
          outEl.replaceChildren(document.createTextNode(msg.text || "Error"));
        }
      } else if (msg.type === "sources") {
        renderSources(msg.sources || []);
      } else if (msg.type === "research_leads") {
        renderResearchLeads(msg);
      } else if (msg.type === "grounding") {
        renderGrounding(msg.grounding || {});
      }
    });
  </script>
</body>
</html>`;
}

module.exports = {
  parseSafeExternalUrl,
  appendInlineMarkdown,
  renderMarkdownDom,
  buildChatWebviewHtml,
};
