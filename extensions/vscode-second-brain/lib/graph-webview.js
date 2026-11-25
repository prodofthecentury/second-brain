"use strict";

function buildGraphWebviewHtml({ nonce, initialData = {} } = {}) {
  const safeInitialJson = JSON.stringify(initialData || {})
    .replace(/</g, "\\u003c")
    .replace(/\u2028/g, "\\u2028")
    .replace(/\u2029/g, "\\u2029");

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
      gap: 5px;
      font-size: 12px;
      color: var(--vscode-descriptionForeground);
    }
    input[type="text"], input[type="date"] {
      background: var(--vscode-input-background);
      color: var(--vscode-input-foreground);
      border: 1px solid var(--vscode-input-border, var(--vscode-panel-border, #444));
      border-radius: 5px;
      padding: 4px 8px;
      font-family: inherit;
      font-size: 12px;
    }
    input[type="range"] {
      width: 190px;
      accent-color: var(--vscode-focusBorder, #4f8cf7);
    }
    button {
      background: var(--vscode-button-background);
      color: var(--vscode-button-foreground);
      border: none;
      border-radius: 5px;
      padding: 5px 11px;
      cursor: pointer;
      font-size: 12px;
      font-weight: 500;
    }
    button:hover { background: var(--vscode-button-hoverBackground); }
    button.secondary {
      background: var(--vscode-button-secondaryBackground, var(--vscode-sideBar-background));
      color: var(--vscode-button-secondaryForeground, var(--vscode-editor-foreground));
      border: 1px solid var(--vscode-panel-border, #444);
    }
    .meta {
      font-size: 12px;
      color: var(--vscode-descriptionForeground);
      padding: 6px 10px;
      border-radius: 5px;
      background: var(--vscode-sideBar-background);
      border: 1px solid var(--vscode-panel-border, #333);
    }
    .badge {
      display: inline-block;
      padding: 2px 7px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 600;
      background: var(--vscode-badge-background, #264f78);
      color: var(--vscode-badge-foreground, #ffffff);
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 290px;
      gap: 12px;
      align-items: start;
    }
    @media (max-width: 760px) {
      .layout { grid-template-columns: 1fr; }
    }
    .canvas-wrap {
      border: 1px solid var(--vscode-panel-border, #333);
      border-radius: 6px;
      background: var(--vscode-sideBar-background);
      overflow: hidden;
      min-height: 440px;
    }
    svg#graphSvg {
      width: 100%;
      height: 500px;
      display: block;
    }
    .inspector {
      border: 1px solid var(--vscode-panel-border, #333);
      border-radius: 6px;
      background: var(--vscode-sideBar-background);
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      font-size: 12.5px;
    }
    .insp-title { font-weight: 600; font-size: 13.5px; }
    .insp-kv {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      border-bottom: 1px solid var(--vscode-panel-border, #2d2d2d);
      padding-bottom: 4px;
    }
    .insp-k { color: var(--vscode-descriptionForeground); }
    .insp-v { text-align: right; word-break: break-word; }
    .legend {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      font-size: 11.5px;
      color: var(--vscode-descriptionForeground);
    }
    .legend-item { display: inline-flex; align-items: center; gap: 5px; }
    .dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      display: inline-block;
    }
  </style>
</head>
<body>
  <div class="row" style="justify-content: space-between;">
    <h3>Second Brain Knowledge Graph (Bi-Temporal Explorer)</h3>
    <div class="legend">
      <span class="legend-item"><span class="dot" style="background:#3fb950;"></span>Verified / Active</span>
      <span class="legend-item"><span class="dot" style="background:#d29922;"></span>Pending</span>
      <span class="legend-item"><span class="dot" style="background:#8b949e;"></span>Expired (Dashed)</span>
      <span class="legend-item"><span class="dot" style="background:#58a6ff;"></span>Future / Scheduled</span>
    </div>
  </div>

  <div class="row">
    <label class="ctrl-label">As Of: <input id="asOfInput" type="date" /></label>
    <input id="timelineSlider" type="range" min="0" max="0" value="0" aria-label="Bi-Temporal Timeline Slider" />
    <span id="timelineLabel" class="badge">Live (Now)</span>
    <button id="clearAsOfBtn" type="button" class="secondary">Live (Now)</button>
    <label class="ctrl-label"><input id="includeExpiredCheck" type="checkbox" checked /> Include Expired</label>
    <label class="ctrl-label"><input id="includePendingCheck" type="checkbox" /> Include Pending</label>
    <input id="projectInput" type="text" placeholder="Project scope (optional)" />
    <button id="reloadBtn" type="button">Reload</button>
    <button id="autoLinkBtn" type="button" class="secondary">Auto-Link</button>
  </div>

  <div id="temporalMeta" class="meta">Loading knowledge graph...</div>

  <div class="layout">
    <div class="canvas-wrap">
      <svg id="graphSvg" viewBox="0 0 920 500"></svg>
    </div>
    <div id="nodeInspector" class="inspector">
      <div class="insp-title">Node Inspector</div>
      <div class="meta">Click any graph node to inspect bi-temporal validity and open its backing atom.</div>
    </div>
  </div>

  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    const SVG_NS = "http://www.w3.org/2000/svg";
    let graphData = ${safeInitialJson};
    let selectedNodeId = "";
    const nodeCircleById = new Map();

    function updateNodeSelection(nextId, node) {
      selectedNodeId = nextId;
      for (const [nid, entry] of nodeCircleById.entries()) {
        const isSel = nid === selectedNodeId;
        entry.circle.setAttribute("r", isSel ? "16" : "13");
        entry.circle.setAttribute("stroke", isSel ? "#ffffff" : entry.colors.stroke);
        entry.circle.setAttribute("stroke-width", isSel ? "3" : "2");
      }
      renderInspector(node || null);
    }

    const asOfInput = document.getElementById("asOfInput");
    const timelineSlider = document.getElementById("timelineSlider");
    const timelineLabel = document.getElementById("timelineLabel");
    const clearAsOfBtn = document.getElementById("clearAsOfBtn");
    const includeExpiredCheck = document.getElementById("includeExpiredCheck");
    const includePendingCheck = document.getElementById("includePendingCheck");
    const projectInput = document.getElementById("projectInput");
    const reloadBtn = document.getElementById("reloadBtn");
    const autoLinkBtn = document.getElementById("autoLinkBtn");
    const temporalMeta = document.getElementById("temporalMeta");
    const graphSvg = document.getElementById("graphSvg");
    const nodeInspector = document.getElementById("nodeInspector");

    function filterPayload(actionName) {
      return {
        action: actionName || "reload",
        as_of: (asOfInput.value || "").trim(),
        include_expired: Boolean(includeExpiredCheck.checked),
        include_pending: Boolean(includePendingCheck.checked),
        project: (projectInput.value || "").trim(),
      };
    }

    function requestReload(actionName) {
      temporalMeta.textContent =
        actionName === "autoLink" ? "Running Auto-Link and refreshing graph..." : "Loading knowledge graph...";
      vscode.postMessage(filterPayload(actionName || "reload"));
    }

    function nodeColors(node) {
      const ts = String(node.temporal_status || "unbounded").toLowerCase();
      if (ts === "expired") {
        return { fill: "#2d333b", stroke: "#8b949e", dashed: true };
      }
      if (ts === "future") {
        return { fill: "#1f3a5f", stroke: "#58a6ff", dashed: true };
      }
      if (!node.verified && String(node.review_status || "").toLowerCase() === "pending") {
        return { fill: "#4a3812", stroke: "#d29922", dashed: false };
      }
      return { fill: "#1b4721", stroke: "#3fb950", dashed: false };
    }

    function appendKv(parent, label, value) {
      const row = document.createElement("div");
      row.className = "insp-kv";
      const k = document.createElement("span");
      k.className = "insp-k";
      k.textContent = label;
      const v = document.createElement("span");
      v.className = "insp-v";
      v.textContent = value || "-";
      row.appendChild(k);
      row.appendChild(v);
      parent.appendChild(row);
    }

    function renderInspector(node) {
      nodeInspector.replaceChildren();
      const head = document.createElement("div");
      head.className = "insp-title";
      head.textContent = node ? "Node Details" : "Node Inspector";
      nodeInspector.appendChild(head);

      if (!node) {
        const hint = document.createElement("div");
        hint.className = "meta";
        hint.textContent = "Click any graph node to inspect bi-temporal validity and open its backing atom.";
        nodeInspector.appendChild(hint);
        return;
      }

      const claimBox = document.createElement("div");
      claimBox.textContent = String(node.claim || node.label || node.id || "");
      nodeInspector.appendChild(claimBox);

      appendKv(nodeInspector, "Filename", node.filename || node.id);
      appendKv(nodeInspector, "Status", node.effective_status || node.review_status);
      appendKv(nodeInspector, "Temporal State", node.temporal_status || "unbounded");
      appendKv(nodeInspector, "Valid From", node.valid_from || "(unbounded)");
      appendKv(nodeInspector, "Valid Until", node.valid_until || "(unbounded)");
      appendKv(nodeInspector, "Type", node.memory_type || "semantic");
      appendKv(
        nodeInspector,
        "Contexts",
        Array.isArray(node.contexts) && node.contexts.length ? node.contexts.join(", ") : "-"
      );
      if (node.superseded_by) {
        appendKv(nodeInspector, "Superseded By", node.superseded_by);
      }

      if (node.filename) {
        const openBtn = document.createElement("button");
        openBtn.type = "button";
        openBtn.textContent = "Open in Editor";
        openBtn.addEventListener("click", () => {
          vscode.postMessage({
            action: "openNode",
            filename: String(node.filename || ""),
            id: String(node.id || ""),
            review_status: String(node.review_status || ""),
          });
        });
        nodeInspector.appendChild(openBtn);
      }
    }

    function syncTimelineControls(data) {
      const dates = Array.isArray(data.timeline_dates) ? data.timeline_dates : [];
      const activeAsOf = data.as_of ? String(data.as_of).slice(0, 10) : "";
      if (data.as_of !== undefined) {
        asOfInput.value = activeAsOf;
      }
      if (typeof data.include_expired === "boolean") {
        includeExpiredCheck.checked = data.include_expired;
      }
      if (typeof data.include_pending === "boolean") {
        includePendingCheck.checked = data.include_pending;
      }
      if (typeof data.project === "string") {
        projectInput.value = data.project;
      }

      timelineSlider.min = "0";
      timelineSlider.max = String(dates.length);
      if (!activeAsOf) {
        timelineSlider.value = String(dates.length);
        timelineLabel.textContent = "Live (Now)";
      } else {
        const idx = dates.indexOf(activeAsOf);
        timelineSlider.value = String(idx >= 0 ? idx : dates.length);
        timelineLabel.textContent = "As Of " + activeAsOf;
      }
    }

    function renderGraph(data) {
      graphData = data || {};
      syncTimelineControls(graphData);

      const nodes = Array.isArray(graphData.nodes) ? graphData.nodes : [];
      const edges = Array.isArray(graphData.edges) ? graphData.edges : [];
      const dates = Array.isArray(graphData.timeline_dates) ? graphData.timeline_dates : [];
      const asOfStr = graphData.as_of ? String(graphData.as_of) : "Live (Now)";
      const activeCount = Number(graphData.active_temporal_count || 0);
      const expiredCount = Number(graphData.expired_temporal_count || 0);
      const futureCount = Number(graphData.future_temporal_count || 0);

      temporalMeta.textContent =
        "Nodes: " +
        nodes.length +
        " | Edges: " +
        edges.length +
        " | Active Temporal: " +
        activeCount +
        " | Expired: " +
        expiredCount +
        " | Future: " +
        futureCount +
        " | Timeline Dates: " +
        dates.length +
        " | View: " +
        asOfStr;

      graphSvg.replaceChildren();
      nodeCircleById.clear();
      if (!nodes.length) {
        const empty = document.createElementNS(SVG_NS, "text");
        empty.setAttribute("x", "460");
        empty.setAttribute("y", "250");
        empty.setAttribute("text-anchor", "middle");
        empty.setAttribute("fill", "#8b949e");
        empty.setAttribute("font-size", "14");
        empty.textContent = "No graph nodes match the selected bi-temporal filter.";
        graphSvg.appendChild(empty);
        renderInspector(null);
        return;
      }

      const cx = 460;
      const cy = 250;
      const count = nodes.length;
      const posById = new Map();
      nodes.forEach((node, idx) => {
        const ring = count > 24 && idx % 2 === 1 ? 125 : 200;
        const angle = (2 * Math.PI * idx) / Math.max(1, count);
        const x = count === 1 ? cx : cx + Math.cos(angle) * ring * 1.45;
        const y = count === 1 ? cy : cy + Math.sin(angle) * ring * 0.92;
        posById.set(String(node.id || node.filename || idx), { x, y, node });
      });

      const edgeGroup = document.createElementNS(SVG_NS, "g");
      for (const edge of edges) {
        const src = posById.get(String(edge.from || ""));
        const dst = posById.get(String(edge.to || ""));
        if (!src || !dst) continue;
        const line = document.createElementNS(SVG_NS, "line");
        line.setAttribute("x1", src.x.toFixed(1));
        line.setAttribute("y1", src.y.toFixed(1));
        line.setAttribute("x2", dst.x.toFixed(1));
        line.setAttribute("y2", dst.y.toFixed(1));
        line.setAttribute("stroke", edge.kind === "contradiction" ? "#f85149" : "#6e7681");
        line.setAttribute("stroke-opacity", "0.65");
        line.setAttribute("stroke-width", edge.kind === "contradiction" ? "2" : "1.3");
        if (edge.kind === "supersedes") {
          line.setAttribute("stroke-dasharray", "5,4");
        }
        const title = document.createElementNS(SVG_NS, "title");
        title.textContent = String(edge.kind || "link") + ": " + edge.from + " -> " + edge.to;
        line.appendChild(title);
        edgeGroup.appendChild(line);
      }
      graphSvg.appendChild(edgeGroup);

      const nodeGroup = document.createElementNS(SVG_NS, "g");
      let matchedSelected = null;
      for (const [id, info] of posById.entries()) {
        const node = info.node;
        const colors = nodeColors(node);
        const g = document.createElementNS(SVG_NS, "g");
        g.style.cursor = "pointer";

        const circle = document.createElementNS(SVG_NS, "circle");
        circle.setAttribute("cx", info.x.toFixed(1));
        circle.setAttribute("cy", info.y.toFixed(1));
        circle.setAttribute("r", id === selectedNodeId ? "16" : "13");
        circle.setAttribute("fill", colors.fill);
        circle.setAttribute("stroke", id === selectedNodeId ? "#ffffff" : colors.stroke);
        circle.setAttribute("stroke-width", id === selectedNodeId ? "3" : "2");
        if (colors.dashed) {
          circle.setAttribute("stroke-dasharray", "4,3");
        }
        g.appendChild(circle);
        nodeCircleById.set(id, { circle, colors });

        const label = document.createElementNS(SVG_NS, "text");
        label.setAttribute("x", info.x.toFixed(1));
        label.setAttribute("y", (info.y + 26).toFixed(1));
        label.setAttribute("text-anchor", "middle");
        label.setAttribute("fill", "#d0d7de");
        label.setAttribute("font-size", "10.5");
        const rawText = String(node.label || node.filename || id);
        label.textContent = rawText.length > 26 ? rawText.slice(0, 24) + "..." : rawText;
        g.appendChild(label);

        const title = document.createElementNS(SVG_NS, "title");
        title.textContent =
          rawText +
          " (" +
          (node.temporal_status || "unbounded") +
          (node.valid_from ? ", from " + node.valid_from : "") +
          (node.valid_until ? ", until " + node.valid_until : "") +
          ")";
        g.appendChild(title);

        g.addEventListener("click", () => {
          updateNodeSelection(id, node);
        });
        nodeGroup.appendChild(g);
        if (id === selectedNodeId) {
          matchedSelected = node;
        }
      }
      graphSvg.appendChild(nodeGroup);
      renderInspector(matchedSelected);
    }

    timelineSlider.addEventListener("input", () => {
      const dates = Array.isArray(graphData.timeline_dates) ? graphData.timeline_dates : [];
      const idx = Number(timelineSlider.value);
      if (Number.isFinite(idx) && idx >= 0 && idx < dates.length) {
        asOfInput.value = dates[idx];
        timelineLabel.textContent = "As Of " + dates[idx];
      } else {
        asOfInput.value = "";
        timelineLabel.textContent = "Live (Now)";
      }
    });
    timelineSlider.addEventListener("change", () => {
      const dates = Array.isArray(graphData.timeline_dates) ? graphData.timeline_dates : [];
      const idx = Number(timelineSlider.value);
      if (Number.isFinite(idx) && idx >= 0 && idx < dates.length) {
        asOfInput.value = dates[idx];
        timelineLabel.textContent = "As Of " + dates[idx];
      } else {
        asOfInput.value = "";
        timelineLabel.textContent = "Live (Now)";
      }
      requestReload("reload");
    });

    asOfInput.addEventListener("change", () => requestReload("reload"));
    includeExpiredCheck.addEventListener("change", () => requestReload("reload"));
    includePendingCheck.addEventListener("change", () => requestReload("reload"));
    clearAsOfBtn.addEventListener("click", () => {
      asOfInput.value = "";
      requestReload("reload");
    });
    reloadBtn.addEventListener("click", () => requestReload("reload"));
    autoLinkBtn.addEventListener("click", () => requestReload("autoLink"));

    window.addEventListener("message", (event) => {
      const msg = event.data || {};
      if (msg.type === "graphData" && msg.data) {
        renderGraph(msg.data);
      } else if (msg.type === "graphError") {
        temporalMeta.textContent =
          "Error: " + String(msg.error || "Failed to load knowledge graph.");
      }
    });

    renderGraph(graphData);
  </script>
</body>
</html>`;
}

module.exports = {
  buildGraphWebviewHtml,
};
