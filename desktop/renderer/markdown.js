"use strict";

(function initMarkdownModule(root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.SecondBrainMarkdown = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function createMarkdownApi() {
  const ALLOWED_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);
  const TABLE_SEP_RE = /^\s*\|?\s*:?-{2,}:?(?:\s*\|\s*:?-{2,}:?)+\s*\|?\s*$/;

  function sanitizeExternalUrl(rawUrl) {
    const trimmed = String(rawUrl || "").trim();
    if (
      !trimmed ||
      trimmed.length > 2048 ||
      /[\u0000-\u001f\u007f]/.test(trimmed)
    ) {
      return null;
    }
    try {
      const parsed = new URL(trimmed);
      if (!ALLOWED_PROTOCOLS.has(parsed.protocol)) {
        return null;
      }
      return parsed.toString();
    } catch {
      return null;
    }
  }

  function createEl(doc, tag, className = "", text = "") {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== "" && text !== null && text !== undefined) {
      node.textContent = String(text);
    }
    return node;
  }

  function citationTierClass(cid) {
    const prefix = String(cid || "").charAt(0).toUpperCase();
    if (prefix === "V") return "tier-v";
    if (prefix === "P") return "tier-p";
    if (prefix === "L") return "tier-l";
    return "tier-n";
  }

  function appendInlineMarkdown(parent, text, options = {}) {
    const doc =
      options.doc ||
      (parent && parent.ownerDocument) ||
      (typeof document !== "undefined" ? document : null);
    if (!doc) return;

    const str = String(text ?? "");
    if (!str) return;

    const inlineTokenRe =
      /(`[^`\r\n]+`|\[\[[^\]]+\]\]|\[([VNPL]\d+)\]|\[([^\]]+)\]\(([^)\r\n]+)\)|\*\*\*([^*]+)\*\*\*|\*\*([^*]+)\*\*|__([^_]+)__|\*([^*\r\n]+)\*|(?<![A-Za-z0-9])_([^_\r\n]+)_(?![A-Za-z0-9]))/g;

    let lastIdx = 0;
    let match;
    while ((match = inlineTokenRe.exec(str)) !== null) {
      if (match.index > lastIdx) {
        parent.appendChild(doc.createTextNode(str.slice(lastIdx, match.index)));
      }
      const full = match[0];

      if (full.startsWith("`") && full.endsWith("`")) {
        parent.appendChild(createEl(doc, "code", "md-inline-code", full.slice(1, -1)));
      } else if (full.startsWith("[[") && full.endsWith("]]")) {
        const inner = full.slice(2, -2);
        const parts = inner.split("|");
        const target = parts[0].trim();
        const label = (parts[1] || parts[0]).trim();
        const pill = createEl(doc, "span", "wikilink-pill", `[[${label}]]`);
        pill.title = `Open note: ${target}`;
        if (typeof options.onWikiLinkClick === "function") {
          pill.addEventListener("click", () => options.onWikiLinkClick(target));
        }
        parent.appendChild(pill);
      } else if (match[2]) {
        const cid = match[2];
        const tierCls = citationTierClass(cid);
        const badge = createEl(
          doc,
          "button",
          `citation-badge ${tierCls}`,
          `[${cid}]`
        );
        badge.type = "button";
        badge.dataset.citationId = cid;
        badge.title = `Jump to source [${cid}]`;
        if (typeof options.onCitationClick === "function") {
          badge.addEventListener("click", (e) => {
            e.stopPropagation();
            options.onCitationClick(cid);
          });
        }
        parent.appendChild(badge);
      } else if (match[3] !== undefined && match[4] !== undefined) {
        const linkLabel = match[3];
        const rawHref = match[4].trim();
        const safeUrl = sanitizeExternalUrl(rawHref);
        if (safeUrl) {
          const anchor = createEl(doc, "a", "md-link");
          anchor.setAttribute("href", safeUrl);
          anchor.setAttribute("rel", "noopener noreferrer");
          anchor.title = safeUrl;
          appendInlineMarkdown(anchor, linkLabel, options);
          anchor.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            if (typeof options.onOpenExternal === "function") {
              options.onOpenExternal(safeUrl);
            }
          });
          parent.appendChild(anchor);
        } else {
          parent.appendChild(doc.createTextNode(full));
        }
      } else if (match[5] !== undefined) {
        const strong = createEl(doc, "strong");
        const em = createEl(doc, "em");
        appendInlineMarkdown(em, match[5], options);
        strong.appendChild(em);
        parent.appendChild(strong);
      } else if (match[6] !== undefined || match[7] !== undefined) {
        const boldInner = match[6] !== undefined ? match[6] : match[7];
        const strong = createEl(doc, "strong");
        appendInlineMarkdown(strong, boldInner, options);
        parent.appendChild(strong);
      } else if (match[8] !== undefined || match[9] !== undefined) {
        const emInner = match[8] !== undefined ? match[8] : match[9];
        const em = createEl(doc, "em");
        appendInlineMarkdown(em, emInner, options);
        parent.appendChild(em);
      } else {
        parent.appendChild(doc.createTextNode(full));
      }

      lastIdx = match.index + full.length;
    }

    if (lastIdx < str.length) {
      parent.appendChild(doc.createTextNode(str.slice(lastIdx)));
    }
  }

  function splitTableCells(rowLine) {
    let trimmed = String(rowLine || "").trim();
    if (trimmed.startsWith("|")) trimmed = trimmed.slice(1);
    if (trimmed.endsWith("|")) trimmed = trimmed.slice(0, -1);
    const cells = [];
    let cur = "";
    for (let i = 0; i < trimmed.length; i += 1) {
      const ch = trimmed[i];
      if (ch === "\\" && i + 1 < trimmed.length && trimmed[i + 1] === "|") {
        cur += "|";
        i += 1;
      } else if (ch === "|") {
        cells.push(cur.trim());
        cur = "";
      } else {
        cur += ch;
      }
    }
    cells.push(cur.trim());
    return cells;
  }

  function parseTableAlignments(sepLine) {
    return splitTableCells(sepLine).map((cell) => {
      const c = cell.trim();
      const left = c.startsWith(":");
      const right = c.endsWith(":");
      if (left && right) return "center";
      if (right) return "right";
      if (left) return "left";
      return "";
    });
  }

  function isPotentialTableHeader(line) {
    const trimmed = String(line || "").trim();
    return trimmed.includes("|") && !trimmed.startsWith("```");
  }

  function computeIndentDepth(rawLine) {
    let spaces = 0;
    for (let i = 0; i < rawLine.length; i += 1) {
      const ch = rawLine[i];
      if (ch === " ") spaces += 1;
      else if (ch === "\t") spaces += 2;
      else break;
    }
    return Math.floor(spaces / 2);
  }

  function renderMarkdownSafe(container, markdown, options = {}) {
    if (!container) return container;
    const doc =
      options.doc ||
      container.ownerDocument ||
      (typeof document !== "undefined" ? document : null);
    if (!doc) return container;

    const opts = { ...options, doc };
    container.replaceChildren();

    const lines = String(markdown ?? "").split(/\r?\n/);
    let inCode = false;
    let codeLang = "";
    let codeLines = [];
    let quoteLines = [];
    let listStack = [];

    const flushQuote = () => {
      if (!quoteLines.length) return;
      const bq = createEl(doc, "blockquote");
      quoteLines.forEach((ql, idx) => {
        if (idx > 0) bq.appendChild(createEl(doc, "br"));
        appendInlineMarkdown(bq, ql, opts);
      });
      container.appendChild(bq);
      quoteLines = [];
    };

    const flushList = () => {
      listStack = [];
    };

    const pushListItem = (depth, listType, contentText) => {
      while (
        listStack.length > 1 &&
        listStack[listStack.length - 1].depth > depth
      ) {
        listStack.pop();
      }

      if (!listStack.length) {
        const rootList = createEl(doc, listType);
        container.appendChild(rootList);
        listStack.push({ depth: 0, type: listType, listEl: rootList, lastLi: null });
      } else {
        const top = listStack[listStack.length - 1];
        if (depth > top.depth && top.lastLi) {
          const nestedList = createEl(doc, listType);
          top.lastLi.appendChild(nestedList);
          listStack.push({
            depth,
            type: listType,
            listEl: nestedList,
            lastLi: null,
          });
        } else if (top.type !== listType && top.depth === depth) {
          if (listStack.length === 1) {
            listStack.pop();
            const switchedRoot = createEl(doc, listType);
            container.appendChild(switchedRoot);
            listStack.push({
              depth: 0,
              type: listType,
              listEl: switchedRoot,
              lastLi: null,
            });
          } else {
            listStack.pop();
            const parentEntry = listStack[listStack.length - 1];
            const switchedNested = createEl(doc, listType);
            (parentEntry.lastLi || container).appendChild(switchedNested);
            listStack.push({
              depth,
              type: listType,
              listEl: switchedNested,
              lastLi: null,
            });
          }
        }
      }

      const activeList = listStack[listStack.length - 1];
      const li = createEl(doc, "li");
      const taskMatch =
        listType === "ul" ? /^\[([ xX])\]\s+(.*)$/.exec(contentText) : null;
      if (taskMatch) {
        li.className = "md-task-item";
        const chk = createEl(doc, "input", "md-task-checkbox");
        chk.type = "checkbox";
        chk.disabled = true;
        chk.checked = taskMatch[1].toLowerCase() === "x";
        li.appendChild(chk);
        const span = createEl(doc, "span");
        appendInlineMarkdown(span, taskMatch[2], opts);
        li.appendChild(span);
      } else {
        appendInlineMarkdown(li, contentText, opts);
      }
      activeList.listEl.appendChild(li);
      activeList.lastLi = li;
    };

    for (let i = 0; i < lines.length; i += 1) {
      const rawLine = lines[i];
      const trimmed = rawLine.trim();

      if (trimmed.startsWith("```")) {
        flushQuote();
        flushList();
        if (!inCode) {
          inCode = true;
          codeLang = trimmed.slice(3).trim().slice(0, 32);
          codeLines = [];
        } else {
          const pre = createEl(doc, "pre", "md-code");
          if (codeLang) pre.dataset.lang = codeLang;
          const codeEl = createEl(doc, "code", "", codeLines.join("\n"));
          pre.appendChild(codeEl);
          container.appendChild(pre);
          inCode = false;
          codeLang = "";
          codeLines = [];
        }
        continue;
      }

      if (inCode) {
        codeLines.push(rawLine);
        continue;
      }

      if (!trimmed) {
        flushQuote();
        flushList();
        continue;
      }

      if (
        isPotentialTableHeader(rawLine) &&
        i + 1 < lines.length &&
        TABLE_SEP_RE.test(lines[i + 1])
      ) {
        flushQuote();
        flushList();
        const headerCells = splitTableCells(rawLine);
        const alignments = parseTableAlignments(lines[i + 1]);
        const wrap = createEl(doc, "div", "md-table-wrap");
        const table = createEl(doc, "table", "data-table md-table");
        const thead = createEl(doc, "thead");
        const headTr = createEl(doc, "tr");
        headerCells.forEach((cellText, colIdx) => {
          const th = createEl(doc, "th");
          if (alignments[colIdx]) th.style.textAlign = alignments[colIdx];
          appendInlineMarkdown(th, cellText, opts);
          headTr.appendChild(th);
        });
        thead.appendChild(headTr);
        table.appendChild(thead);

        const tbody = createEl(doc, "tbody");
        i += 2;
        while (i < lines.length) {
          const rowRaw = lines[i].trim();
          if (!rowRaw || !rowRaw.includes("|") || rowRaw.startsWith("```")) {
            i -= 1;
            break;
          }
          const cells = splitTableCells(lines[i]);
          const tr = createEl(doc, "tr");
          for (let c = 0; c < headerCells.length; c += 1) {
            const td = createEl(doc, "td");
            if (alignments[c]) td.style.textAlign = alignments[c];
            appendInlineMarkdown(td, cells[c] ?? "", opts);
            tr.appendChild(td);
          }
          tbody.appendChild(tr);
          i += 1;
        }
        table.appendChild(tbody);
        wrap.appendChild(table);
        container.appendChild(wrap);
        continue;
      }

      if (/^(?:---+|\*\*\*+|___+)$/.test(trimmed)) {
        flushQuote();
        flushList();
        container.appendChild(createEl(doc, "hr"));
        continue;
      }

      const headingMatch = /^(#{1,6})\s+(.*)$/.exec(trimmed);
      if (headingMatch) {
        flushQuote();
        flushList();
        const level = headingMatch[1].length;
        const h = createEl(doc, `h${level}`);
        appendInlineMarkdown(h, headingMatch[2], opts);
        container.appendChild(h);
        continue;
      }

      if (trimmed.startsWith(">")) {
        flushList();
        quoteLines.push(trimmed.replace(/^>\s?/, ""));
        continue;
      }
      flushQuote();

      const depth = computeIndentDepth(rawLine);
      const ulMatch = /^[-*+]\s+(.*)$/.exec(trimmed);
      if (ulMatch) {
        pushListItem(depth, "ul", ulMatch[1]);
        continue;
      }

      const olMatch = /^\d+[.)]\s+(.*)$/.exec(trimmed);
      if (olMatch) {
        pushListItem(depth, "ol", olMatch[1]);
        continue;
      }

      flushList();
      const p = createEl(doc, "p");
      appendInlineMarkdown(p, rawLine, opts);
      container.appendChild(p);
    }

    flushQuote();
    flushList();
    if (inCode && codeLines.length) {
      const pre = createEl(doc, "pre", "md-code");
      if (codeLang) pre.dataset.lang = codeLang;
      pre.appendChild(createEl(doc, "code", "", codeLines.join("\n")));
      container.appendChild(pre);
    }

    if (!container.children.length && options.emptyPlaceholder) {
      container.appendChild(
        createEl(doc, "div", "note-item-meta", options.emptyPlaceholder)
      );
    }
    return container;
  }

  return {
    sanitizeExternalUrl,
    appendInlineMarkdown,
    renderMarkdownSafe,
  };
});
