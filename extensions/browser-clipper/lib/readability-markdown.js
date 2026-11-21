"use strict";

/**
 * Pure DOM Readability-style main-content scorer and HTML-to-Markdown converter.
 * Strictly walks DOM nodes without raw HTML serialization sinks.
 */

function extractActiveTabContent() {
  const NOISE_TAGS = new Set([
    "SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "CANVAS", "IFRAME",
    "OBJECT", "EMBED", "NAV", "FOOTER", "ASIDE", "FORM", "BUTTON", "INPUT",
    "SELECT", "TEXTAREA", "DIALOG", "AUDIO", "VIDEO",
  ]);
  const NOISE_ROLES = new Set([
    "navigation", "banner", "contentinfo", "complementary", "search", "menubar", "menu", "dialog", "alertdialog",
  ]);
  const NOISE_ATTR_RE = /\b(?:cookie|consent|gdpr|paywall|newsletter|sidebar|nav(?:bar)?|menubar|breadcrumbs?|share[-_]?bar|social[-_]?links|promo|advert(?:isement)?|ad[-_]?slot|modal|popup|comment[-_]?form|related[-_]?posts)\b/i;
  const POSITIVE_ATTR_RE = /\b(?:article|post[-_]?(?:content|body)|entry[-_]?(?:content|body)|markdown[-_]?body|main[-_]?content|story[-_]?body|prose|doc[-_]?content)\b/i;

  function cleanInlineWs(str) {
    return String(str || "").replace(/[\r\n\t\u00a0]+/g, " ").replace(/ {2,}/g, " ");
  }

  function safeHref(rawHref, baseHref) {
    const trimmed = String(rawHref || "").trim();
    if (!trimmed || trimmed.startsWith("#")) return "";
    try {
      const resolved = new URL(trimmed, baseHref || "https://localhost/");
      if (resolved.protocol !== "http:" && resolved.protocol !== "https:") return "";
      if (resolved.username || resolved.password) return "";
      return resolved.toString();
    } catch {
      return "";
    }
  }

  function getAttr(el, name) {
    if (!el || typeof el.getAttribute !== "function") return "";
    return String(el.getAttribute(name) || "");
  }

  function isHiddenOrNoise(el) {
    if (!el || el.nodeType !== 1) return false;
    const tag = String(el.tagName || "").toUpperCase();
    if (NOISE_TAGS.has(tag)) return true;
    if (el.hidden === true || getAttr(el, "aria-hidden").toLowerCase() === "true") return true;
    const role = getAttr(el, "role").trim().toLowerCase();
    if (role && NOISE_ROLES.has(role)) return true;
    if (tag === "HEADER") {
      const parentTag = el.parentNode && el.parentNode.tagName ? String(el.parentNode.tagName).toUpperCase() : "";
      if (parentTag === "BODY" || parentTag === "HTML") return true;
    }
    if (tag !== "ARTICLE" && tag !== "MAIN" && tag !== "BODY") {
      const idCls = `${getAttr(el, "id")} ${getAttr(el, "class")}`;
      if (NOISE_ATTR_RE.test(idCls) && !POSITIVE_ATTR_RE.test(idCls)) return true;
    }
    return false;
  }

  function measureSubtreeStats(root) {
    let totalChars = 0;
    let linkChars = 0;
    let paraCount = 0;
    let codeCount = 0;
    let commaCount = 0;

    function walk(node, insideLink) {
      if (!node) return;
      if (node.nodeType === 3) {
        const txt = cleanInlineWs(node.nodeValue || "").trim();
        if (txt) {
          totalChars += txt.length;
          if (insideLink) linkChars += txt.length;
          const commas = txt.match(/[,;]/g);
          if (commas) commaCount += commas.length;
        }
        return;
      }
      if (node.nodeType !== 1 || isHiddenOrNoise(node)) return;
      const tag = String(node.tagName || "").toUpperCase();
      if (tag === "P" || tag === "LI") paraCount += 1;
      if (tag === "PRE") codeCount += 1;
      const nextLink = insideLink || tag === "A";
      const children = node.childNodes || [];
      for (let i = 0; i < children.length; i += 1) {
        walk(children[i], nextLink);
      }
    }

    walk(root, false);
    return { totalChars, linkChars, paraCount, codeCount, commaCount };
  }

  function selectReadableRoot(doc) {
    const body = doc && doc.body ? doc.body : doc;
    if (!body) return null;

    const candidates = [];
    function collect(node) {
      if (!node || node.nodeType !== 1 || isHiddenOrNoise(node)) return;
      const tag = String(node.tagName || "").toUpperCase();
      const role = getAttr(node, "role").trim().toLowerCase();
      const idCls = `${getAttr(node, "id")} ${getAttr(node, "class")}`;
      if (
        tag === "ARTICLE" ||
        tag === "MAIN" ||
        role === "main" ||
        POSITIVE_ATTR_RE.test(idCls) ||
        tag === "SECTION" ||
        tag === "DIV"
      ) {
        candidates.push(node);
      }
      const children = node.childNodes || [];
      for (let i = 0; i < children.length; i += 1) {
        collect(children[i]);
      }
    }
    collect(body);

    let bestNode = body;
    let bestScore = -1;
    for (let i = 0; i < candidates.length; i += 1) {
      const cand = candidates[i];
      const stats = measureSubtreeStats(cand);
      if (stats.totalChars < 80) continue;
      const tag = String(cand.tagName || "").toUpperCase();
      const role = getAttr(cand, "role").trim().toLowerCase();
      const idCls = `${getAttr(cand, "id")} ${getAttr(cand, "class")}`;

      let score = 0;
      if (tag === "ARTICLE") score += 35;
      else if (tag === "MAIN" || role === "main") score += 30;
      if (POSITIVE_ATTR_RE.test(idCls)) score += 25;
      if (NOISE_ATTR_RE.test(idCls)) score -= 30;

      score += Math.min(40, Math.floor(stats.totalChars / 100));
      score += Math.min(25, stats.paraCount * 3);
      score += Math.min(15, stats.codeCount * 4);
      score += Math.min(15, stats.commaCount);

      const linkDensity = stats.totalChars > 0 ? stats.linkChars / stats.totalChars : 0;
      if (linkDensity > 0.45) {
        score *= Math.max(0.1, 1 - linkDensity);
      }
      if (score > bestScore) {
        bestScore = score;
        bestNode = cand;
      }
    }
    return bestNode;
  }

  function collectRawText(node) {
    if (!node) return "";
    if (node.nodeType === 3) return String(node.nodeValue || "");
    if (node.nodeType !== 1 || isHiddenOrNoise(node)) return "";
    if (String(node.tagName || "").toUpperCase() === "BR") return "\n";
    const children = node.childNodes;
    if (children && children.length > 0) {
      let out = "";
      for (let i = 0; i < children.length; i += 1) {
        out += collectRawText(children[i]);
      }
      return out;
    }
    if (typeof node.textContent === "string") return node.textContent;
    return "";
  }

  function detectCodeLang(el) {
    let firstElem = el.firstElementChild || null;
    if (!firstElem && el.childNodes) {
      for (let i = 0; i < el.childNodes.length; i += 1) {
        if (el.childNodes[i] && el.childNodes[i].nodeType === 1) {
          firstElem = el.childNodes[i];
          break;
        }
      }
    }
    const cls = `${getAttr(el, "class")} ${firstElem ? getAttr(firstElem, "class") : ""}`;
    const m = cls.match(/(?:language|lang)-([a-z0-9_+#-]{1,24})/i);
    return m ? m[1].toLowerCase() : "";
  }

  function renderInline(node, baseHref) {
    if (!node) return "";
    if (node.nodeType === 3) {
      return cleanInlineWs(node.nodeValue || "");
    }
    if (node.nodeType !== 1 || isHiddenOrNoise(node)) return "";
    const tag = String(node.tagName || "").toUpperCase();
    if (tag === "BR") return "\n";

    let inner = "";
    const children = node.childNodes || [];
    for (let i = 0; i < children.length; i += 1) {
      inner += renderInline(children[i], baseHref);
    }
    if (tag === "CODE") {
      const trimmed = inner.trim().replace(/`/g, "'");
      return trimmed ? `\`${trimmed}\`` : "";
    }
    if (tag === "STRONG" || tag === "B") {
      const trimmed = inner.trim();
      return trimmed ? `**${trimmed}**` : "";
    }
    if (tag === "EM" || tag === "I") {
      const trimmed = inner.trim();
      return trimmed ? `*${trimmed}*` : "";
    }
    if (tag === "A") {
      const label = inner.trim();
      if (!label) return "";
      const href = safeHref(getAttr(node, "href"), baseHref);
      return href ? `[${label}](${href})` : label;
    }
    return inner;
  }

  function renderTable(tableEl, baseHref) {
    const rows = [];
    function collectRows(parent) {
      if (!parent || parent.nodeType !== 1) return;
      const children = parent.childNodes || [];
      for (let i = 0; i < children.length; i += 1) {
        const ch = children[i];
        if (!ch || ch.nodeType !== 1) continue;
        const ctag = String(ch.tagName || "").toUpperCase();
        if (ctag === "TR") {
          const cells = [];
          const tdKids = ch.childNodes || [];
          for (let j = 0; j < tdKids.length; j += 1) {
            const cell = tdKids[j];
            if (cell && cell.nodeType === 1) {
              const cellTag = String(cell.tagName || "").toUpperCase();
              if (cellTag === "TH" || cellTag === "TD") {
                const txt = renderInline(cell, baseHref).replace(/\s+/g, " ").replace(/\|/g, "\\|").trim();
                cells.push(txt);
              }
            }
          }
          if (cells.some(Boolean)) rows.push(cells);
        } else if (ctag === "THEAD" || ctag === "TBODY" || ctag === "TFOOT") {
          collectRows(ch);
        }
      }
    }
    collectRows(tableEl);
    if (!rows.length) return "";
    const colCount = Math.max(...rows.map((r) => r.length));
    if (colCount < 1) return "";
    const padRow = (r) => {
      const copy = r.slice(0, colCount);
      while (copy.length < colCount) copy.push("");
      return `| ${copy.join(" | ")} |`;
    };
    const headerLine = padRow(rows[0]);
    const sepLine = `| ${Array(colCount).fill("---").join(" | ")} |`;
    const bodyLines = rows.slice(1).map(padRow);
    return [headerLine, sepLine, ...bodyLines].join("\n");
  }

  function renderBlocks(node, baseHref, listDepth) {
    if (!node) return "";
    if (node.nodeType === 3) {
      return cleanInlineWs(node.nodeValue || "").trim();
    }
    if (node.nodeType !== 1 || isHiddenOrNoise(node)) return "";
    const tag = String(node.tagName || "").toUpperCase();

    if (/^H[1-6]$/.test(tag)) {
      const level = Number.parseInt(tag.slice(1), 10);
      const txt = renderInline(node, baseHref).replace(/\s+/g, " ").trim();
      return txt ? `\n\n${"#".repeat(level)} ${txt}\n\n` : "";
    }
    if (tag === "PRE") {
      const rawCode = collectRawText(node).replace(/\r\n/g, "\n").replace(/\r/g, "\n").trimEnd();
      if (!rawCode.trim()) return "";
      const lang = detectCodeLang(node);
      const fence = rawCode.includes("```") ? "~~~~" : "```";
      return `\n\n${fence}${lang}\n${rawCode}\n${fence}\n\n`;
    }
    if (tag === "TABLE") {
      const mdTable = renderTable(node, baseHref);
      return mdTable ? `\n\n${mdTable}\n\n` : "";
    }
    if (tag === "BLOCKQUOTE") {
      const inner = renderChildrenBlocks(node, baseHref, listDepth).trim();
      if (!inner) return "";
      const quoted = inner
        .split("\n")
        .map((ln) => (ln.trim() ? `> ${ln}` : ">"))
        .join("\n");
      return `\n\n${quoted}\n\n`;
    }
    if (tag === "UL" || tag === "OL") {
      const ordered = tag === "OL";
      const items = [];
      let idx = 1;
      const children = node.childNodes || [];
      for (let i = 0; i < children.length; i += 1) {
        const li = children[i];
        if (!li || li.nodeType !== 1 || isHiddenOrNoise(li)) continue;
        if (String(li.tagName || "").toUpperCase() !== "LI") continue;
        const itemText = renderListItem(li, baseHref, listDepth, ordered ? `${idx}. ` : "- ");
        if (itemText) {
          items.push(itemText);
          idx += 1;
        }
      }
      if (!items.length) return "";
      return listDepth === 0 ? `\n\n${items.join("\n")}\n\n` : `\n${items.join("\n")}`;
    }
    if (tag === "P" || tag === "FIGCAPTION" || tag === "DT" || tag === "DD") {
      const txt = renderInline(node, baseHref).replace(/[ \t]+\n/g, "\n").trim();
      return txt ? `\n\n${txt}\n\n` : "";
    }
    if (tag === "HR") {
      return "\n\n* * *\n\n";
    }
    return renderChildrenBlocks(node, baseHref, listDepth);
  }

  function renderListItem(liNode, baseHref, listDepth, marker) {
    const indent = "  ".repeat(listDepth);
    const inlineParts = [];
    const nestedLists = [];
    const children = liNode.childNodes || [];
    for (let i = 0; i < children.length; i += 1) {
      const ch = children[i];
      if (!ch) continue;
      if (ch.nodeType === 1) {
        const ctag = String(ch.tagName || "").toUpperCase();
        if (ctag === "UL" || ctag === "OL") {
          const sub = renderBlocks(ch, baseHref, listDepth + 1);
          if (sub.trim()) nestedLists.push(sub.replace(/^\n+/, ""));
          continue;
        }
      }
      inlineParts.push(renderInline(ch, baseHref));
    }
    const lead = inlineParts.join("").replace(/\s+/g, " ").trim();
    if (!lead && !nestedLists.length) return "";
    const headLine = `${indent}${marker}${lead}`;
    return nestedLists.length ? `${headLine}\n${nestedLists.join("\n")}` : headLine;
  }

  function renderChildrenBlocks(parent, baseHref, listDepth) {
    let acc = "";
    const children = parent.childNodes || [];
    for (let i = 0; i < children.length; i += 1) {
      acc += renderBlocks(children[i], baseHref, listDepth);
    }
    return acc;
  }

  const doc = typeof document !== "undefined" ? document : null;
  const href = typeof location !== "undefined" && location.href ? location.href : "https://localhost/";
  const selection =
    typeof window !== "undefined" && typeof window.getSelection === "function"
      ? String(window.getSelection() || "").trim()
      : "";
  if (!doc || !doc.body) {
    return { selection, pageText: "" };
  }
  const readableRoot = selectReadableRoot(doc);
  let markdown = renderBlocks(readableRoot, href, 0)
    .replace(/\r\n/g, "\n")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  if (!markdown && doc.body) {
    markdown = String(doc.body.innerText || "").trim();
  }
  return {
    selection: selection.slice(0, 32000),
    pageText: markdown.slice(0, 80000),
  };
}

function extractReadableMarkdownFromDom(doc, baseUrl) {
  const prevDoc = globalThis.document;
  const prevLoc = globalThis.location;
  const prevWin = globalThis.window;
  try {
    globalThis.document = doc;
    globalThis.location = { href: baseUrl || "https://example.org/article" };
    if (!globalThis.window) {
      globalThis.window = { getSelection: () => "" };
    }
    return extractActiveTabContent();
  } finally {
    globalThis.document = prevDoc;
    globalThis.location = prevLoc;
    globalThis.window = prevWin;
  }
}

const readabilityExported = {
  extractActiveTabContent,
  extractReadableMarkdownFromDom,
};

if (typeof module !== "undefined" && module.exports) {
  module.exports = readabilityExported;
} else {
  globalThis.SecondBrainReadability = readabilityExported;
}
