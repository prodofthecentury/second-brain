"use strict";

(function initGraphModule(root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.SecondBrainGraph = api;
  }
})(
  typeof globalThis !== "undefined" ? globalThis : this,
  function createGraphApi() {
    const SVG_NS = "http://www.w3.org/2000/svg";

    function normalizeNode(raw) {
      if (!raw || typeof raw !== "object") return null;
      const id = String(raw.id || raw.filename || raw.atom_id || "").trim();
      if (!id) return null;
      const reviewStatus = String(
        raw.review_status || raw.status || (raw.verified ? "accepted" : "pending")
      ).toLowerCase();
      const verified =
        raw.verified !== undefined
          ? Boolean(raw.verified)
          : reviewStatus === "accepted";
      const memoryType = String(
        raw.epistemic_label === "preference"
          ? "self"
          : raw.memory_type || "semantic"
      ).toLowerCase();
      const contexts = Array.isArray(raw.contexts)
        ? raw.contexts.map((c) => String(c))
        : [];
      const label = String(
        raw.label || raw.claim || raw.title || id
      ).trim();
      const validFrom = String(raw.valid_from || "").trim();
      const validUntil = String(raw.valid_until || "").trim();
      const temporalStatus = String(raw.temporal_status || "active").trim().toLowerCase() || "active";
      return {
        id,
        label,
        shortLabel: label.length > 34 ? `${label.slice(0, 32)}…` : label,
        review_status: reviewStatus,
        memory_type: memoryType,
        contexts,
        verified,
        valid_from: validFrom,
        valid_until: validUntil,
        temporal_status: temporalStatus,
        raw,
      };
    }

    function evaluateTemporalStatusForNode(node, asOfRaw = "") {
      const asOfDate = String(asOfRaw || "").trim().slice(0, 10);
      const vf = String(node?.valid_from || "").trim().slice(0, 10);
      const vu = String(node?.valid_until || "").trim().slice(0, 10);
      if (!asOfDate) {
        return String(node?.temporal_status || "active").toLowerCase() || "active";
      }
      if (vf && vu && vf > vu) return "invalid_window";
      if (vf && asOfDate < vf) return "not_yet_valid";
      if (vu && asOfDate > vu) return "expired";
      return "active";
    }

    function synthesizeFallbackGraph(atoms = [], radarData = {}) {
      const nodeMap = new Map();
      for (const a of atoms || []) {
        const n = normalizeNode(a);
        if (n) nodeMap.set(n.id, n);
      }

      const edges = [];
      const edgeKeys = new Set();
      const addEdge = (from, to, kind) => {
        if (!from || !to || from === to) return;
        const pair = from < to ? `${from}|${to}|${kind}` : `${to}|${from}|${kind}`;
        if (edgeKeys.has(pair)) return;
        edgeKeys.add(pair);
        edges.push({ from, to, kind });
      };

      const alerts = Array.isArray(radarData?.alerts) ? radarData.alerts : [];
      for (const al of alerts) {
        const aFn = al?.atom_a?.filename;
        const bFn = al?.atom_b?.filename;
        if (aFn && !nodeMap.has(aFn)) {
          const na = normalizeNode(al.atom_a);
          if (na) nodeMap.set(na.id, na);
        }
        if (bFn && !nodeMap.has(bFn)) {
          const nb = normalizeNode(al.atom_b);
          if (nb) nodeMap.set(nb.id, nb);
        }
        if (aFn && bFn) {
          addEdge(aFn, bFn, "contradicts");
        }
      }

      const byContext = new Map();
      for (const node of nodeMap.values()) {
        for (const ctx of node.contexts) {
          const normCtx = ctx.trim().toLowerCase();
          if (!normCtx) continue;
          if (!byContext.has(normCtx)) byContext.set(normCtx, []);
          byContext.get(normCtx).push(node.id);
        }
      }
      for (const [ctx, ids] of byContext.entries()) {
        if (ids.length >= 2 && ids.length <= 12) {
          for (let i = 0; i < ids.length - 1; i += 1) {
            addEdge(ids[i], ids[i + 1], `context:${ctx}`);
          }
        }
      }

      return {
        nodes: [...nodeMap.values()],
        edges,
        fallback: true,
      };
    }

    function filterGraphData(graphData = {}, filters = {}) {
      const rawNodes = Array.isArray(graphData.nodes) ? graphData.nodes : [];
      const rawEdges = Array.isArray(graphData.edges) ? graphData.edges : [];

      const search = String(filters.search || "").trim().toLowerCase();
      const project = String(filters.project || "").trim().toLowerCase();
      const status = String(filters.status || "all").trim().toLowerCase();
      const memoryType = String(filters.memoryType || "all").trim().toLowerCase();
      const includePending =
        filters.includePending !== undefined
          ? Boolean(filters.includePending)
          : true;
      const asOf = String(filters.asOf || "").trim();
      const includeExpired =
        filters.includeExpired !== undefined
          ? Boolean(filters.includeExpired)
          : !asOf;

      const nodes = [];
      const keptIds = new Set();

      for (const item of rawNodes) {
        const node = item.shortLabel ? item : normalizeNode(item);
        if (!node) continue;

        const effTemporal = evaluateTemporalStatusForNode(node, asOf);
        if (
          !includeExpired &&
          (effTemporal === "expired" ||
            effTemporal === "not_yet_valid" ||
            effTemporal === "invalid_window")
        ) {
          continue;
        }

        if (!includePending && !node.verified && node.review_status !== "accepted") {
          continue;
        }
        if (status && status !== "all") {
          if (status === "accepted") {
            if (!node.verified && node.review_status !== "accepted") continue;
          } else if (node.review_status !== status) {
            continue;
          }
        }
        if (memoryType && memoryType !== "all") {
          if (memoryType === "preference" || memoryType === "self") {
            if (node.memory_type !== "self" && node.memory_type !== "preference") {
              continue;
            }
          } else if (node.memory_type !== memoryType) {
            continue;
          }
        }
        if (project) {
          const matchesProj = node.contexts.some((c) => {
            const lc = c.toLowerCase();
            return lc === project || lc === `project:${project}` || lc.includes(project);
          });
          if (!matchesProj) continue;
        }
        if (search) {
          const hay = `${node.id} ${node.label} ${node.contexts.join(" ")}`.toLowerCase();
          if (!hay.includes(search)) continue;
        }

        nodes.push(effTemporal !== node.temporal_status ? { ...node, temporal_status: effTemporal } : node);
        keptIds.add(node.id);
      }

      const edges = rawEdges
        .filter((e) => e && keptIds.has(String(e.from)) && keptIds.has(String(e.to)))
        .map((e) => ({
          from: String(e.from),
          to: String(e.to),
          kind: String(e.kind || e.relation || "related"),
        }));

      return { nodes, edges };
    }

    function nodeColorFor(node) {
      if (!node) return "#58a6ff";
      if (node.review_status === "needs_edit" || node.review_status === "quarantined") {
        return "#f85149";
      }
      if (node.temporal_status === "expired" || node.temporal_status === "invalid_window") {
        return "#6e7681";
      }
      if (node.temporal_status === "not_yet_valid") {
        return "#79c0ff";
      }
      if (!node.verified && node.review_status !== "accepted") {
        return "#d29922";
      }
      if (node.memory_type === "procedural") return "#2ea043";
      if (node.memory_type === "self" || node.memory_type === "preference") {
        return "#7c5cff";
      }
      if (node.memory_type === "episodic") return "#38bdf8";
      return "#58a6ff";
    }

    function edgeStyleFor(edge) {
      const k = String(edge?.kind || "").toLowerCase();
      if (k.includes("contradict") || k.includes("conflict")) {
        return { stroke: "#f85149", dash: "5,4", width: "2.1" };
      }
      if (k.includes("supersede")) {
        return { stroke: "#d29922", dash: "3,3", width: "1.7" };
      }
      return { stroke: "#485266", dash: "", width: "1.3" };
    }

    function computeForceLayout(
      nodes = [],
      edges = [],
      { width = 840, height = 540, iterations = 55 } = {}
    ) {
      const positions = new Map();
      const count = nodes.length;
      if (!count) return positions;

      const cx = width / 2;
      const cy = height / 2;
      if (count === 1) {
        positions.set(nodes[0].id, { x: cx, y: cy });
        return positions;
      }

      const radiusScale = Math.min(width, height) * 0.36;
      const state = nodes.map((n, i) => {
        const angle = i * 2.399963229728653;
        const r = Math.sqrt((i + 0.5) / count) * radiusScale;
        return {
          id: n.id,
          x: cx + Math.cos(angle) * r,
          y: cy + Math.sin(angle) * r,
          vx: 0,
          vy: 0,
        };
      });

      const idxById = new Map();
      state.forEach((s, idx) => idxById.set(s.id, idx));

      const idealDist = Math.max(
        65,
        Math.min(150, Math.sqrt((width * height) / Math.max(count, 2)) * 0.65)
      );
      const repulsion = idealDist * idealDist * 0.85;

      for (let iter = 0; iter < iterations; iter += 1) {
        const cooling = 1 - iter / (iterations + 5);
        for (const s of state) {
          s.vx = (cx - s.x) * 0.018;
          s.vy = (cy - s.y) * 0.018;
        }

        for (let i = 0; i < count; i += 1) {
          for (let j = i + 1; j < count; j += 1) {
            let dx = state[i].x - state[j].x;
            let dy = state[i].y - state[j].y;
            const distSq = Math.max(25, dx * dx + dy * dy);
            const dist = Math.sqrt(distSq);
            const force = repulsion / distSq;
            const fx = (dx / dist) * force;
            const fy = (dy / dist) * force;
            state[i].vx += fx;
            state[i].vy += fy;
            state[j].vx -= fx;
            state[j].vy -= fy;
          }
        }

        for (const e of edges) {
          const ia = idxById.get(e.from);
          const ib = idxById.get(e.to);
          if (ia === undefined || ib === undefined) continue;
          const dx = state[ib].x - state[ia].x;
          const dy = state[ib].y - state[ia].y;
          const dist = Math.max(1, Math.hypot(dx, dy));
          const delta = (dist - idealDist) * 0.07;
          const fx = (dx / dist) * delta;
          const fy = (dy / dist) * delta;
          state[ia].vx += fx;
          state[ia].vy += fy;
          state[ib].vx -= fx;
          state[ib].vy -= fy;
        }

        const maxStep = 28 * cooling;
        for (const s of state) {
          const stepLen = Math.hypot(s.vx, s.vy);
          if (stepLen > maxStep && stepLen > 0) {
            s.vx = (s.vx / stepLen) * maxStep;
            s.vy = (s.vy / stepLen) * maxStep;
          }
          s.x = Math.max(55, Math.min(width - 55, s.x + s.vx));
          s.y = Math.max(45, Math.min(height - 45, s.y + s.vy));
        }
      }

      for (const s of state) {
        positions.set(s.id, {
          x: Math.round(s.x * 10) / 10,
          y: Math.round(s.y * 10) / 10,
        });
      }
      return positions;
    }

    function createSvgEl(doc, tag) {
      return doc.createElementNS(SVG_NS, tag);
    }

    function createGraphViewer(container, options = {}) {
      const doc =
        options.doc ||
        (container && container.ownerDocument) ||
        (typeof document !== "undefined" ? document : null);
      const width = options.width || 860;
      const height = options.height || 540;

      let panX = 0;
      let panY = 0;
      let scale = 1;
      let selectedNodeId = null;
      let lastFiltered = { nodes: [], edges: [] };

      let svg = null;
      let viewport = null;
      let edgesGroup = null;
      let nodesGroup = null;

      function syncViewportTransform() {
        if (!viewport) return;
        viewport.setAttribute(
          "transform",
          `translate(${Math.round(panX * 10) / 10} ${
            Math.round(panY * 10) / 10
          }) scale(${Math.round(scale * 1000) / 1000})`
        );
      }

      function ensureSvgMounted() {
        if (!container || !doc) return;
        if (svg && svg.parentNode === container) return;
        container.replaceChildren();
        svg = createSvgEl(doc, "svg");
        svg.setAttribute("class", "graph-svg");
        svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
        svg.setAttribute("role", "img");
        svg.setAttribute("aria-label", "2D Knowledge Graph");

        viewport = createSvgEl(doc, "g");
        viewport.setAttribute("class", "graph-viewport");
        edgesGroup = createSvgEl(doc, "g");
        edgesGroup.setAttribute("class", "graph-edges");
        nodesGroup = createSvgEl(doc, "g");
        nodesGroup.setAttribute("class", "graph-nodes");

        viewport.appendChild(edgesGroup);
        viewport.appendChild(nodesGroup);
        svg.appendChild(viewport);
        container.appendChild(svg);
        syncViewportTransform();

        let dragging = false;
        let lastClientX = 0;
        let lastClientY = 0;

        svg.addEventListener("mousedown", (e) => {
          if (e.button !== undefined && e.button !== 0) return;
          dragging = true;
          lastClientX = e.clientX || 0;
          lastClientY = e.clientY || 0;
        });
        svg.addEventListener("mousemove", (e) => {
          if (!dragging) return;
          const dx = (e.clientX || 0) - lastClientX;
          const dy = (e.clientY || 0) - lastClientY;
          lastClientX = e.clientX || 0;
          lastClientY = e.clientY || 0;
          panX += dx;
          panY += dy;
          syncViewportTransform();
        });
        const endDrag = () => {
          dragging = false;
        };
        svg.addEventListener("mouseup", endDrag);
        svg.addEventListener("mouseleave", endDrag);
        svg.addEventListener("wheel", (e) => {
          if (typeof e.preventDefault === "function") e.preventDefault();
          const factor = (e.deltaY || 0) < 0 ? 1.14 : 0.88;
          scale = Math.max(0.2, Math.min(4.0, scale * factor));
          syncViewportTransform();
        });
      }

      function updateSelectionStyles() {
        if (!nodesGroup || !edgesGroup) return;
        for (const g of nodesGroup.children || []) {
          const nid = g.getAttribute("data-node-id");
          const circle = g.children && g.children[0];
          if (circle) {
            circle.setAttribute(
              "stroke",
              nid === selectedNodeId ? "#ffffff" : "#0d1117"
            );
            circle.setAttribute(
              "stroke-width",
              nid === selectedNodeId ? "3" : "1.8"
            );
          }
        }
        for (const line of edgesGroup.children || []) {
          const from = line.getAttribute("data-from");
          const to = line.getAttribute("data-to");
          const active =
            selectedNodeId && (from === selectedNodeId || to === selectedNodeId);
          line.setAttribute("stroke-opacity", active ? "1" : "0.62");
        }
      }

      return {
        zoomBy(factor) {
          scale = Math.max(0.2, Math.min(4.0, scale * Number(factor || 1)));
          syncViewportTransform();
          return this.getTransformState();
        },

        panBy(dx, dy) {
          panX += Number(dx || 0);
          panY += Number(dy || 0);
          syncViewportTransform();
          return this.getTransformState();
        },

        resetView() {
          panX = 0;
          panY = 0;
          scale = 1;
          syncViewportTransform();
          return this.getTransformState();
        },

        getTransformState() {
          return { panX, panY, scale };
        },

        getSelectedNodeId() {
          return selectedNodeId;
        },

        selectNodeById(nodeId) {
          const found = lastFiltered.nodes.find((n) => n.id === nodeId);
          if (!found) return null;
          selectedNodeId = found.id;
          updateSelectionStyles();
          const incident = lastFiltered.edges.filter(
            (e) => e.from === found.id || e.to === found.id
          );
          if (typeof options.onSelectNode === "function") {
            options.onSelectNode(found, incident);
          }
          return found;
        },

        render(graphData = {}, filters = {}) {
          lastFiltered = filterGraphData(graphData, filters);
          const { nodes, edges } = lastFiltered;

          if (!container || !doc) return lastFiltered;
          if (!nodes.length) {
            svg = null;
            container.replaceChildren();
            const empty = doc.createElement("div");
            empty.className = "status-banner";
            empty.style.margin = "14px";
            empty.textContent =
              "No graph nodes match the current filter criteria.";
            container.appendChild(empty);
            return lastFiltered;
          }

          ensureSvgMounted();
          edgesGroup.replaceChildren();
          nodesGroup.replaceChildren();

          const positions = computeForceLayout(nodes, edges, { width, height });

          for (const edge of edges) {
            const pFrom = positions.get(edge.from);
            const pTo = positions.get(edge.to);
            if (!pFrom || !pTo) continue;
            const style = edgeStyleFor(edge);
            const line = createSvgEl(doc, "line");
            line.setAttribute("x1", String(pFrom.x));
            line.setAttribute("y1", String(pFrom.y));
            line.setAttribute("x2", String(pTo.x));
            line.setAttribute("y2", String(pTo.y));
            line.setAttribute("stroke", style.stroke);
            line.setAttribute("stroke-width", style.width);
            line.setAttribute("stroke-opacity", "0.65");
            if (style.dash) {
              line.setAttribute("stroke-dasharray", style.dash);
            }
            line.setAttribute("data-from", edge.from);
            line.setAttribute("data-to", edge.to);
            line.setAttribute("data-kind", edge.kind);
            const titleEl = createSvgEl(doc, "title");
            titleEl.textContent = `${edge.from} --[${edge.kind}]--> ${edge.to}`;
            line.appendChild(titleEl);
            edgesGroup.appendChild(line);
          }

          for (const node of nodes) {
            const pos = positions.get(node.id);
            if (!pos) continue;
            const g = createSvgEl(doc, "g");
            g.setAttribute("class", "graph-node");
            g.setAttribute("data-node-id", node.id);
            g.setAttribute("transform", `translate(${pos.x} ${pos.y})`);
            g.style.cursor = "pointer";

            const circle = createSvgEl(doc, "circle");
            circle.setAttribute("r", "10");
            circle.setAttribute("fill", nodeColorFor(node));
            circle.setAttribute("stroke", "#0d1117");
            circle.setAttribute("stroke-width", "1.8");
            if (node.temporal_status === "expired" || node.temporal_status === "not_yet_valid") {
              circle.setAttribute("stroke-dasharray", "3,2");
            }

            const label = createSvgEl(doc, "text");
            label.setAttribute("x", "14");
            label.setAttribute("y", "4");
            label.setAttribute("fill", "#c9d1d9");
            label.setAttribute("font-size", "11");
            label.textContent = node.id.replace(/\.md$/i, "");

            const tooltip = createSvgEl(doc, "title");
            const temporalInfo =
              node.valid_from || node.valid_until || node.temporal_status !== "active"
                ? ` [${node.temporal_status}: ${node.valid_from || "-inf"}..${node.valid_until || "+inf"}]`
                : "";
            tooltip.textContent = `${node.id} (${node.memory_type}, ${node.review_status})${temporalInfo}\n${node.label}`;

            g.appendChild(circle);
            g.appendChild(label);
            g.appendChild(tooltip);

            g.addEventListener("click", (e) => {
              if (typeof e.stopPropagation === "function") e.stopPropagation();
              selectedNodeId = node.id;
              updateSelectionStyles();
              const incident = edges.filter(
                (ed) => ed.from === node.id || ed.to === node.id
              );
              if (typeof options.onSelectNode === "function") {
                options.onSelectNode(node, incident);
              }
            });

            nodesGroup.appendChild(g);
          }

          updateSelectionStyles();
          return lastFiltered;
        },
      };
    }

    return {
      SVG_NS,
      normalizeNode,
      synthesizeFallbackGraph,
      filterGraphData,
      nodeColorFor,
      edgeStyleFor,
      computeForceLayout,
      createGraphViewer,
    };
  }
);
