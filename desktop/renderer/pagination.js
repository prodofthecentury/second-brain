"use strict";

(function initPaginationModule(root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.SecondBrainPagination = api;
  }
})(
  typeof globalThis !== "undefined" ? globalThis : this,
  function createPaginationApi() {
    function extractItemsArray(data, preferredKey = "items") {
      if (!data || typeof data !== "object") return [];
      const candidates = [
        data[preferredKey],
        data.items,
        data.notes,
        data.atoms,
        data.entries,
        data.leaves,
        data.reports,
      ];
      for (const arr of candidates) {
        if (Array.isArray(arr)) return arr;
      }
      return [];
    }

    function createPaginator({
      pageSize = 100,
      keyField = "filename",
      getKey = null,
    } = {}) {
      const limit = Math.max(1, Math.min(500, Number(pageSize) || 100));
      const resolveKey =
        typeof getKey === "function"
          ? getKey
          : (item, idx) =>
              item && typeof item === "object" && item[keyField] !== undefined
                ? String(item[keyField])
                : `idx:${idx}`;

      let state = {
        items: [],
        total: 0,
        offset: 0,
        limit,
        lastPageCount: 0,
        hasLoaded: false,
      };

      return {
        getState() {
          return { ...state, items: [...state.items] };
        },
        getItems() {
          return [...state.items];
        },
        getPageSize() {
          return state.limit;
        },
        reset() {
          state = {
            items: [],
            total: 0,
            offset: 0,
            limit,
            lastPageCount: 0,
            hasLoaded: false,
          };
          return this.getState();
        },
        nextOffset() {
          return state.offset;
        },
        applyPage(data = {}, { append = false, preferredKey = "items" } = {}) {
          const pageItems = extractItemsArray(data, preferredKey);
          const respOffset =
            data && typeof data.offset === "number"
              ? data.offset
              : append
              ? state.offset
              : 0;

          let merged;
          if (!append) {
            merged = [...pageItems];
          } else {
            const seen = new Set(
              state.items.map((it, idx) => resolveKey(it, idx))
            );
            merged = [...state.items];
            pageItems.forEach((it, idx) => {
              const k = resolveKey(it, state.items.length + idx);
              if (!seen.has(k)) {
                seen.add(k);
                merged.push(it);
              }
            });
          }

          const rawTotal =
            data && typeof data.total === "number"
              ? data.total
              : merged.length;
          const total = Math.max(rawTotal, merged.length);

          state = {
            items: merged,
            total,
            offset: respOffset + pageItems.length,
            limit:
              data && typeof data.limit === "number" ? data.limit : state.limit,
            lastPageCount: pageItems.length,
            hasLoaded: true,
          };
          return this.getState();
        },
        canLoadMore() {
          return (
            state.hasLoaded &&
            state.items.length < state.total &&
            state.lastPageCount > 0
          );
        },
        remainingCount() {
          return Math.max(0, state.total - state.items.length);
        },
        summaryText(noun = "items") {
          return `Showing ${state.items.length} of ${state.total} ${noun}`;
        },
      };
    }

    function buildPaginatedPath(basePath, { limit = 100, offset = 0, params = {} } = {}) {
      const parts = [
        `limit=${encodeURIComponent(String(limit))}`,
        `offset=${encodeURIComponent(String(offset))}`,
      ];
      for (const [k, v] of Object.entries(params || {})) {
        if (v !== undefined && v !== null && String(v).trim() !== "") {
          parts.push(
            `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`
          );
        }
      }
      const sep = String(basePath).includes("?") ? "&" : "?";
      return `${basePath}${sep}${parts.join("&")}`;
    }

    function normalizeLedgerLeafOption(item) {
      if (!item || typeof item !== "object") {
        const s = String(item || "").trim();
        if (!s) return null;
        const isPb = s.startsWith("5-playbooks/") || s.startsWith("playbook:");
        return {
          value: s,
          kind: isPb ? "playbook" : "atom",
          label: `[${isPb ? "playbook" : "atom"}] ${s}`,
        };
      }
      const rel = String(
        item.rel_path || item.path || item.filename || ""
      ).trim();
      if (!rel) return null;
      const kind =
        String(item.kind || "").toLowerCase() === "playbook" ||
        rel.startsWith("5-playbooks/") ||
        rel.startsWith("playbook:")
          ? "playbook"
          : "atom";
      const normalizedRef =
        kind === "playbook" &&
        !rel.startsWith("5-playbooks/") &&
        !rel.startsWith("playbook:")
          ? `5-playbooks/${rel}`
          : rel;
      return {
        value: normalizedRef,
        kind,
        label: `[${kind}] ${normalizedRef}`,
      };
    }

    function renderPaginationBar(
      container,
      paginator,
      onLoadMore,
      { doc = null, noun = "items" } = {}
    ) {
      if (!container || !paginator) return;
      const ownerDoc =
        doc ||
        container.ownerDocument ||
        (typeof document !== "undefined" ? document : null);
      if (!ownerDoc) return;

      container.replaceChildren();
      const st = paginator.getState();
      if (!st.hasLoaded || st.total === 0) {
        container.style.display = "none";
        return;
      }
      container.style.display = "flex";
      const info = ownerDoc.createElement("span");
      info.className = "note-item-meta";
      info.textContent = paginator.summaryText(noun);
      container.appendChild(info);

      if (paginator.canLoadMore()) {
        const rem = paginator.remainingCount();
        const btn = ownerDoc.createElement("button");
        btn.type = "button";
        btn.className = "btn sm";
        btn.textContent = `Load More (${Math.min(st.limit, rem)} of ${rem} remaining)`;
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          btn.textContent = "Loading...";
          try {
            await onLoadMore();
          } finally {
            if (btn.isConnected) {
              btn.disabled = false;
            }
          }
        });
        container.appendChild(btn);
      }
    }

    return {
      createPaginator,
      buildPaginatedPath,
      normalizeLedgerLeafOption,
      renderPaginationBar,
    };
  }
);
