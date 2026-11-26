"use strict";

(function initBuildCanvasModule(root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.SecondBrainBuildCanvas = api;
  }
})(
  typeof globalThis !== "undefined" ? globalThis : this,
  function createBuildCanvasApi() {
    const PREVIEW_IFRAME_CSP =
      "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none';";

    const PERSONA_CHIPS = Object.freeze([
      {
        id: "grounded_default",
        label: "Grounded Default",
        defaultWorkMode: "chat",
        description: "Strict verified vault recall and cited synthesis.",
      },
      {
        id: "coding",
        label: "Coding",
        defaultWorkMode: "build",
        description: "Modular engineering, clean diffs, edge-case checks, and tests.",
      },
      {
        id: "website_builder",
        label: "Website Builder",
        defaultWorkMode: "build",
        description: "Responsive editorial/product pages renderable in Build Canvas.",
      },
      {
        id: "app_builder",
        label: "App Builder",
        defaultWorkMode: "build",
        description: "Self-contained interactive web utility apps with state.",
      },
      {
        id: "game_builder",
        label: "Game Builder",
        defaultWorkMode: "build",
        description: "60fps Canvas/SVG mini-games with controls, HUD, and reset.",
      },
    ]);

    const EXT_BY_LANG = Object.freeze({
      html: "html",
      htm: "html",
      svg: "svg",
      css: "css",
      js: "js",
      javascript: "js",
      mjs: "js",
      json: "json",
      md: "md",
      markdown: "md",
      py: "py",
      python: "py",
      ts: "ts",
      tsx: "tsx",
      jsx: "jsx",
      txt: "txt",
    });

    const WINDOWS_RESERVED_STEM_RE = /^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i;

    function sanitizeRelativeArtifactPath(rawPath, fallbackName = "artifact.txt") {
      const cleaned = String(rawPath || "")
        .replace(/\\/g, "/")
        .replace(/^\/+|\/+$/g, "")
        .trim();
      if (!cleaned || cleaned.includes("\0") || /^[A-Za-z]:/.test(cleaned)) {
        return fallbackName;
      }
      const segments = cleaned.split("/").filter(Boolean);
      if (
        !segments.length ||
        segments.some(
          (s) =>
            s === "." ||
            s === ".." ||
            /[^A-Za-z0-9._-]/.test(s) ||
            s.endsWith(".") ||
            WINDOWS_RESERVED_STEM_RE.test(s)
        )
      ) {
        return fallbackName;
      }
      const joined = segments.join("/").slice(0, 140);
      return joined || fallbackName;
    }

    function parseFenceHeader(headerRaw, index) {
      const tokens = String(headerRaw || "")
        .trim()
        .split(/\s+/)
        .filter(Boolean);
      const langRaw = (tokens[0] || "txt").toLowerCase();
      const lang = EXT_BY_LANG[langRaw] || langRaw.replace(/[^a-z0-9_-]/g, "") || "txt";

      let explicitName = "";
      for (const tok of tokens.slice(1)) {
        const kv = tok.match(/^(?:file|filename|path)=["']?([^"']+)["']?$/i);
        if (kv) {
          explicitName = kv[1];
          break;
        }
        if (tok.includes(".") && /^[A-Za-z0-9._/-]+$/.test(tok)) {
          explicitName = tok;
          break;
        }
      }

      if (explicitName) {
        return {
          lang,
          filename: sanitizeRelativeArtifactPath(explicitName, `snippet_${index + 1}.${lang}`),
        };
      }
      if (lang === "html") return { lang, filename: "index.html" };
      if (lang === "css") return { lang, filename: "styles.css" };
      if (lang === "js") return { lang, filename: "app.js" };
      if (lang === "svg") return { lang, filename: "graphic.svg" };
      return { lang, filename: `snippet_${index + 1}.${lang}` };
    }

    function extractArtifactFiles(markdownText) {
      const text = String(markdownText || "");
      const fenceRe = /```([^\n]*)\r?\n([\s\S]*?)```/g;
      const byName = new Map();
      let idx = 0;
      let match;

      while ((match = fenceRe.exec(text)) !== null) {
        const { lang, filename } = parseFenceHeader(match[1], idx);
        const content = String(match[2] || "").replace(/\r\n/g, "\n");
        let finalName = filename;
        if (byName.has(finalName)) {
          const dot = finalName.lastIndexOf(".");
          const base = dot > 0 ? finalName.slice(0, dot) : finalName;
          const ext = dot > 0 ? finalName.slice(dot) : "";
          let suffix = 2;
          while (byName.has(`${base}-${suffix}${ext}`)) {
            suffix += 1;
          }
          finalName = `${base}-${suffix}${ext}`;
        }
        byName.set(finalName, {
          filename: finalName,
          lang,
          content,
        });
        idx += 1;
      }

      const files = [...byName.values()];
      const htmlFile =
        files.find((f) => f.filename.toLowerCase() === "index.html") ||
        files.find((f) => f.lang === "html" || f.filename.toLowerCase().endsWith(".html")) ||
        null;
      const svgFile = files.find((f) => f.lang === "svg" || f.filename.toLowerCase().endsWith(".svg")) || null;
      const cssFiles = files.filter((f) => f.lang === "css" || f.filename.toLowerCase().endsWith(".css"));
      const jsFiles = files.filter((f) => f.lang === "js" || f.filename.toLowerCase().endsWith(".js"));

      return {
        files,
        entryHtml: htmlFile ? htmlFile.content : svgFile ? svgFile.content : "",
        cssBundle: cssFiles.map((f) => f.content).join("\n\n"),
        jsBundle: jsFiles.map((f) => f.content).join("\n\n"),
        hasPreviewableHtml: Boolean(htmlFile || svgFile || cssFiles.length || jsFiles.length),
      };
    }

    const HTML_VOID_ELEMENTS = new Set([
      "area",
      "base",
      "basefont",
      "bgsound",
      "br",
      "col",
      "embed",
      "frame",
      "hr",
      "img",
      "input",
      "keygen",
      "link",
      "meta",
      "param",
      "source",
      "track",
      "wbr",
    ]);

    const HTML_RAWTEXT_ELEMENTS = new Set([
      "style",
      "script",
      "xmp",
      "iframe",
      "noembed",
      "noframes",
      "plaintext",
    ]);

    const TOKENIZER_OPAQUE_ELEMENTS = new Set([
      "script",
      "style",
      "title",
      "textarea",
      "xmp",
      "noscript",
      "iframe",
      "noembed",
      "noframes",
    ]);

    function escapeHtmlAttrValue(val) {
      return String(val)
        .replace(/&/g, "&amp;")
        .replace(/\u00a0/g, "&nbsp;")
        .replace(/"/g, "&quot;");
    }

    function escapeHtmlText(val) {
      return String(val)
        .replace(/&/g, "&amp;")
        .replace(/\u00a0/g, "&nbsp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    }

    function serializeHtmlChildNodes(nodes, parentTagLower) {
      let out = "";
      if (!nodes) return out;
      const isRawParent = HTML_RAWTEXT_ELEMENTS.has(parentTagLower);
      for (let i = 0; i < nodes.length; i += 1) {
        const child = nodes[i];
        if (!child) continue;
        const nType = child.nodeType;
        if (nType === 1) {
          out += serializeHtmlNode(child);
        } else if (nType === 3) {
          const textVal = String(child.nodeValue || "");
          out += isRawParent ? textVal : escapeHtmlText(textVal);
        } else if (nType === 8) {
          out += `<!--${String(child.nodeValue || "")}-->`;
        } else if (nType === 11) {
          out += serializeHtmlChildNodes(child.childNodes, parentTagLower);
        }
      }
      return out;
    }

    function serializeHtmlNode(node) {
      if (!node || node.nodeType !== 1) return "";
      const isHtmlNs =
        !node.namespaceURI ||
        node.namespaceURI === "http://www.w3.org/1999/xhtml";
      const rawName = String(node.localName || node.tagName || "");
      const tagLower = rawName.toLowerCase();
      const tagName = isHtmlNs ? tagLower : rawName;
      if (!tagName) return "";

      let attrs = "";
      if (node.attributes) {
        for (let i = 0; i < node.attributes.length; i += 1) {
          const attr = node.attributes[i];
          if (!attr || !attr.name) continue;
          attrs += ` ${attr.name}="${escapeHtmlAttrValue(attr.value || "")}"`;
        }
      }
      if (isHtmlNs && HTML_VOID_ELEMENTS.has(tagLower)) {
        return `<${tagName}${attrs}>`;
      }
      const childList =
        isHtmlNs && tagLower === "template" && node.content && node.content.childNodes
          ? node.content.childNodes
          : node.childNodes;
      const childrenHtml = serializeHtmlChildNodes(childList, tagLower);
      return `<${tagName}${attrs}>${childrenHtml}</${tagName}>`;
    }

    function buildDocumentWithDomParser(entryHtml, cspPolicy, safeCssBundle, safeJsBundle) {
      if (typeof DOMParser === "undefined") return null;
      try {
        const parser = new DOMParser();
        const doc = parser.parseFromString(String(entryHtml || ""), "text/html");
        if (!doc || !doc.documentElement) return null;

        let head = doc.head;
        if (!head) {
          head = doc.createElement("head");
          doc.documentElement.insertBefore(head, doc.documentElement.firstChild);
        }

        const meta = doc.createElement("meta");
        meta.setAttribute("http-equiv", "Content-Security-Policy");
        meta.setAttribute("content", cspPolicy);
        head.insertBefore(meta, head.firstChild);

        if (safeCssBundle) {
          const styleEl = doc.createElement("style");
          styleEl.textContent = `\n${safeCssBundle}\n`;
          head.appendChild(styleEl);
        }

        if (safeJsBundle) {
          let body = doc.body;
          if (!body) {
            body = doc.createElement("body");
            doc.documentElement.appendChild(body);
          }
          const scriptEl = doc.createElement("script");
          scriptEl.textContent = `\n${safeJsBundle}\n`;
          body.appendChild(scriptEl);
        }

        return `<!DOCTYPE html>\n${serializeHtmlNode(doc.documentElement)}`;
      } catch {
        return null;
      }
    }

    function isAsciiWhitespace(ch) {
      return ch === " " || ch === "\t" || ch === "\n" || ch === "\r" || ch === "\f";
    }

    function isAsciiAlpha(ch) {
      const code = ch.charCodeAt(0);
      return (code >= 65 && code <= 90) || (code >= 97 && code <= 122);
    }

    function scanTagEnd(src, startAfterName) {
      const len = src.length;
      let i = startAfterName;
      // HTML5 attribute tokenizer states:
      // 0: BEFORE_ATTR_NAME, 1: ATTR_NAME, 2: AFTER_ATTR_NAME,
      // 3: BEFORE_ATTR_VALUE, 4: ATTR_VALUE_DOUBLE, 5: ATTR_VALUE_SINGLE, 6: ATTR_VALUE_UNQUOTED
      let state = 0;
      while (i < len) {
        const ch = src[i];
        if (state === 0) {
          if (ch === ">") return i + 1;
          if (isAsciiWhitespace(ch) || ch === "/") {
            i += 1;
          } else {
            state = 1;
            i += 1;
          }
        } else if (state === 1) {
          if (ch === ">") return i + 1;
          if (ch === "=") {
            state = 3;
            i += 1;
          } else if (isAsciiWhitespace(ch)) {
            state = 2;
            i += 1;
          } else if (ch === "/") {
            state = 0;
            i += 1;
          } else {
            i += 1;
          }
        } else if (state === 2) {
          if (ch === ">") return i + 1;
          if (ch === "=") {
            state = 3;
            i += 1;
          } else if (isAsciiWhitespace(ch)) {
            i += 1;
          } else if (ch === "/") {
            state = 0;
            i += 1;
          } else {
            state = 1;
            i += 1;
          }
        } else if (state === 3) {
          if (ch === ">") return i + 1;
          if (isAsciiWhitespace(ch)) {
            i += 1;
          } else if (ch === '"') {
            state = 4;
            i += 1;
          } else if (ch === "'") {
            state = 5;
            i += 1;
          } else {
            state = 6;
            i += 1;
          }
        } else if (state === 4) {
          if (ch === '"') {
            state = 0;
          }
          i += 1;
        } else if (state === 5) {
          if (ch === "'") {
            state = 0;
          }
          i += 1;
        } else {
          if (ch === ">") return i + 1;
          if (isAsciiWhitespace(ch)) {
            state = 0;
          }
          i += 1;
        }
      }
      return -1;
    }

    function scanDoctypeEnd(src, startAfterName) {
      const len = src.length;
      let i = startAfterName;
      let quote = "";
      while (i < len) {
        const ch = src[i];
        if (quote) {
          if (ch === quote) quote = "";
        } else if (ch === '"' || ch === "'") {
          quote = ch;
        } else if (ch === ">") {
          return i + 1;
        }
        i += 1;
      }
      return -1;
    }

    function matchesCloseTagAt(src, pos, tagNameLower) {
      const nameStart = pos + 2;
      const nameEnd = nameStart + tagNameLower.length;
      if (nameEnd >= src.length) return false;
      if (src.slice(nameStart, nameEnd).toLowerCase() !== tagNameLower) return false;
      const boundary = src[nameEnd];
      return boundary === ">" || boundary === "/" || isAsciiWhitespace(boundary);
    }

    function skipOpaqueElementBody(src, startIndex, tagNameLower) {
      const len = src.length;
      let i = startIndex;
      if (tagNameLower === "plaintext") return len;

      if (tagNameLower === "script") {
        let escaped = false;
        let doubleEscaped = false;
        while (i < len) {
          if (!escaped && src.startsWith("<!--", i)) {
            escaped = true;
            i += 4;
            continue;
          }
          if (escaped && src.startsWith("-->", i)) {
            escaped = false;
            doubleEscaped = false;
            i += 3;
            continue;
          }
          if (escaped && !doubleEscaped && src[i] === "<" && i + 7 < len) {
            if (src.slice(i + 1, i + 7).toLowerCase() === "script") {
              const b = src[i + 7];
              if (b === ">" || b === "/" || isAsciiWhitespace(b)) {
                doubleEscaped = true;
                i += 7;
                continue;
              }
            }
          }
          if (src[i] === "<" && i + 1 < len && src[i + 1] === "/") {
            if (matchesCloseTagAt(src, i, "script")) {
              if (doubleEscaped) {
                doubleEscaped = false;
                i += 8;
                continue;
              }
              const endPos = scanTagEnd(src, i + 8);
              return endPos === -1 ? len : endPos;
            }
          }
          i += 1;
        }
        return len;
      }

      while (i < len) {
        if (src[i] === "<" && i + 1 < len && src[i + 1] === "/") {
          if (matchesCloseTagAt(src, i, tagNameLower)) {
            const endPos = scanTagEnd(src, i + 2 + tagNameLower.length);
            return endPos === -1 ? len : endPos;
          }
        }
        i += 1;
      }
      return len;
    }

    function skipComment(src, startIndex) {
      const len = src.length;
      if (src.startsWith("<!-->", startIndex)) return startIndex + 5;
      if (src.startsWith("<!--->", startIndex)) return startIndex + 6;
      let i = startIndex + 4;
      while (i < len) {
        if (src[i] === "-") {
          if (src.startsWith("-->", i)) return i + 3;
          if (src.startsWith("--!>", i)) return i + 4;
        }
        i += 1;
      }
      return len;
    }

    function tokenizeActiveHtmlTags(htmlDoc) {
      const src = String(htmlDoc || "");
      const len = src.length;
      const tokens = [];
      let i = 0;
      let templateDepth = 0;

      while (i < len) {
        const lt = src.indexOf("<", i);
        if (lt === -1) break;
        i = lt;

        if (src.startsWith("<!--", i)) {
          i = skipComment(src, i);
          continue;
        }

        if (i + 1 >= len) break;
        const next = src[i + 1];

        if (next === "!") {
          if (src.slice(i, i + 9).toLowerCase() === "<![cdata[") {
            const cdataEnd = src.indexOf("]]>", i + 9);
            i = cdataEnd === -1 ? len : cdataEnd + 3;
            continue;
          }
          if (
            src.slice(i + 2, i + 9).toLowerCase() === "doctype" &&
            (i + 9 >= len || isAsciiWhitespace(src[i + 9]) || src[i + 9] === ">")
          ) {
            const endPos = scanDoctypeEnd(src, i + 9);
            if (endPos === -1) break;
            if (templateDepth === 0) {
              tokens.push({
                kind: "doctype",
                tagName: "!doctype",
                index: i,
                length: endPos - i,
                match: src.slice(i, endPos),
              });
            }
            i = endPos;
            continue;
          }
          const bogusEnd = src.indexOf(">", i + 2);
          i = bogusEnd === -1 ? len : bogusEnd + 1;
          continue;
        }

        if (next === "?") {
          const piEnd = src.indexOf(">", i + 2);
          i = piEnd === -1 ? len : piEnd + 1;
          continue;
        }

        if (next === "/") {
          if (i + 2 < len && isAsciiAlpha(src[i + 2])) {
            let nameEnd = i + 3;
            while (
              nameEnd < len &&
              !isAsciiWhitespace(src[nameEnd]) &&
              src[nameEnd] !== "/" &&
              src[nameEnd] !== ">"
            ) {
              nameEnd += 1;
            }
            const tagName = src.slice(i + 2, nameEnd).toLowerCase();
            const endPos = scanTagEnd(src, nameEnd);
            if (endPos === -1) break;
            if (tagName === "template") {
              if (templateDepth > 0) templateDepth -= 1;
            } else if (templateDepth === 0) {
              tokens.push({
                kind: "end",
                tagName,
                index: i,
                length: endPos - i,
                match: src.slice(i, endPos),
              });
            }
            i = endPos;
            continue;
          }
          const bogusEnd = src.indexOf(">", i + 2);
          i = bogusEnd === -1 ? len : bogusEnd + 1;
          continue;
        }

        if (isAsciiAlpha(next)) {
          let nameEnd = i + 2;
          while (
            nameEnd < len &&
            !isAsciiWhitespace(src[nameEnd]) &&
            src[nameEnd] !== "/" &&
            src[nameEnd] !== ">"
          ) {
            nameEnd += 1;
          }
          const tagName = src.slice(i + 1, nameEnd).toLowerCase();
          const endPos = scanTagEnd(src, nameEnd);
          if (endPos === -1) break;

          if (tagName === "template") {
            if (templateDepth === 0) {
              tokens.push({
                kind: "template_start",
                tagName: "template",
                index: i,
                length: endPos - i,
                match: src.slice(i, endPos),
              });
            }
            templateDepth += 1;
            i = endPos;
            continue;
          }

          if (templateDepth === 0) {
            tokens.push({
              kind: "start",
              tagName,
              index: i,
              length: endPos - i,
              match: src.slice(i, endPos),
            });
          }

          if (TOKENIZER_OPAQUE_ELEMENTS.has(tagName) || tagName === "plaintext") {
            i = skipOpaqueElementBody(src, endPos, tagName);
          } else {
            i = endPos;
          }
          continue;
        }

        i += 1;
      }

      return tokens;
    }

    function findActiveTag(tokens, predicate, { last = false } = {}) {
      let found = null;
      for (const tok of tokens) {
        if (tok.kind === "template_start") continue;
        if (predicate(tok)) {
          found = tok;
          if (!last) return found;
        }
      }
      return found;
    }

    function findLeadingActiveHead(tokens) {
      for (const tok of tokens) {
        if (tok.kind === "doctype") continue;
        if (tok.kind === "start" && tok.tagName === "html") continue;
        if (tok.kind === "start" && tok.tagName === "head") return tok;
        if (tok.kind === "start" || tok.kind === "template_start") return null;
      }
      return null;
    }

    function injectHeadContent(htmlDoc, snippet) {
      const src = String(htmlDoc || "");
      const tokens = tokenizeActiveHtmlTags(src);
      const headHit = findLeadingActiveHead(tokens);
      if (headHit) {
        const insertAt = headHit.index + headHit.length;
        return `${src.slice(0, insertAt)}\n${snippet}${src.slice(insertAt)}`;
      }
      const htmlHit = findActiveTag(
        tokens,
        (tok) => tok.kind === "start" && tok.tagName === "html"
      );
      if (htmlHit) {
        const insertAt = htmlHit.index + htmlHit.length;
        return `${src.slice(0, insertAt)}\n<head>\n${snippet}\n</head>${src.slice(insertAt)}`;
      }
      const doctypeHit = findActiveTag(tokens, (tok) => tok.kind === "doctype");
      if (doctypeHit) {
        const insertAt = doctypeHit.index + doctypeHit.length;
        return `${src.slice(0, insertAt)}\n<head>\n${snippet}\n</head>${src.slice(insertAt)}`;
      }
      return `<!DOCTYPE html>\n<html>\n<head>\n<meta charset="UTF-8" />\n${snippet}\n</head>\n<body>\n${src}\n</body>\n</html>`;
    }

    function appendBeforeBodyEnd(htmlDoc, snippet) {
      const src = String(htmlDoc || "");
      const tokens = tokenizeActiveHtmlTags(src);
      const bodyEndHit = findActiveTag(
        tokens,
        (tok) => tok.kind === "end" && tok.tagName === "body",
        { last: true }
      );
      if (bodyEndHit) {
        // Splice at exact match index (avoiding String.prototype.replace string-pattern hazards like $&, $$, $', $`)
        return (
          src.slice(0, bodyEndHit.index) +
          `${snippet}\n${bodyEndHit.match}` +
          src.slice(bodyEndHit.index + bodyEndHit.length)
        );
      }
      const htmlEndHit = findActiveTag(
        tokens,
        (tok) => tok.kind === "end" && tok.tagName === "html",
        { last: true }
      );
      if (htmlEndHit) {
        return (
          src.slice(0, htmlEndHit.index) +
          `${snippet}\n${htmlEndHit.match}` +
          src.slice(htmlEndHit.index + htmlEndHit.length)
        );
      }
      return `${src}\n${snippet}`;
    }

    function buildSandboxedPreviewDocument(extracted, options = {}) {
      const data = extracted && typeof extracted === "object" ? extracted : extractArtifactFiles("");
      const titleText = String(options.title || "Second Brain Build Preview")
        .replace(/[<>&"]/g, "");
      const cspMeta = `<meta http-equiv="Content-Security-Policy" content="${PREVIEW_IFRAME_CSP}" />`;
      const safeCssBundle = String(data.cssBundle || "").replace(/<\/(style)/gi, "<\\/$1");
      const safeJsBundle = String(data.jsBundle || "").replace(/<\/(script)/gi, "<\\/$1");
      const cssTag = safeCssBundle ? `<style>\n${safeCssBundle}\n</style>` : "";
      const jsTag = safeJsBundle ? `<script>\n${safeJsBundle}\n</script>` : "";

      const entryTokens = data.entryHtml ? tokenizeActiveHtmlTags(data.entryHtml) : [];
      const hasFullHtmlEnvelope =
        findActiveTag(
          entryTokens,
          (tok) =>
            tok.kind === "doctype" || (tok.kind === "start" && tok.tagName === "html")
        ) !== null;

      if (data.entryHtml && hasFullHtmlEnvelope) {
        if (typeof DOMParser !== "undefined") {
          const domSerialized = buildDocumentWithDomParser(
            data.entryHtml,
            PREVIEW_IFRAME_CSP,
            safeCssBundle,
            safeJsBundle
          );
          if (domSerialized) {
            return domSerialized;
          }
        }
        let out = injectHeadContent(data.entryHtml, `${cspMeta}\n${cssTag}`.trim());
        if (jsTag) {
          out = appendBeforeBodyEnd(out, jsTag);
        }
        return out;
      }

      const bodyContent = data.entryHtml
        ? data.entryHtml
        : data.hasPreviewableHtml
        ? `<main id="app"><h3>Preview Ready</h3><div id="root"></div></main>`
        : `<main style="font-family:system-ui,sans-serif;padding:24px;color:#cbd5e1;background:#0f172a;min-height:100vh;"><h3>Build Canvas</h3><p>Switch to Build Mode and ask a builder persona (Website, App, Game, or Coding) to generate runnable HTML/CSS/JS artifacts here.</p></main>`;

      return [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="UTF-8" />',
        '<meta name="viewport" content="width=device-width, initial-scale=1.0" />',
        cspMeta,
        `<title>${titleText}</title>`,
        cssTag,
        "</head>",
        "<body>",
        bodyContent,
        jsTag,
        "</body>",
        "</html>",
      ]
        .filter(Boolean)
        .join("\n");
    }

    function runLocalDesignCritiquePreview(extracted) {
      const data = extracted && typeof extracted === "object" ? extracted : extractArtifactFiles("");
      const html = String(data.entryHtml || "");
      const css = String(data.cssBundle || "");
      const js = String(data.jsBundle || "");
      const combined = `${html}\n${css}\n${js}`;

      const panels = [
        {
          panel: "Product & UX",
          score: html.length > 80 || js.length > 80 ? 4 : 2,
          note: /<button|<input|<canvas|<form/i.test(combined)
            ? "Interactive controls/canvas present; verify clear primary CTA hierarchy."
            : "Static markup detected; consider adding interactive feedback states.",
        },
        {
          panel: "Visual Craft",
          score: css.length > 60 || /style=|:root|display:\s*(?:flex|grid)/i.test(combined) ? 4 : 2,
          note: /display:\s*(?:flex|grid)|clamp\(|@media/i.test(combined)
            ? "Responsive layout primitives detected."
            : "Add flex/grid spacing tokens and mobile breakpoint styling.",
        },
        {
          panel: "WCAG Accessibility",
          score: /aria-|alt=|<label|<main|<button/i.test(combined) ? 4 : 2,
          note: /aria-|alt=|<label/i.test(combined)
            ? "Semantic/ARIA markers detected; confirm keyboard focus contrast."
            : "Missing explicit aria-label, alt, or label associations on controls.",
        },
        {
          panel: "Frontend Architecture",
          score: data.files.length >= 2 || js.length > 40 ? 4 : 3,
          note: `Extracted ${data.files.length} artifact file(s) with clean HTML/CSS/JS separation.`,
        },
        {
          panel: "Security & Privacy",
          score: /eval\(|new Function\(|https?:\/\//i.test(combined) ? 2 : 5,
          note: /https?:\/\//i.test(combined)
            ? "External URLs in generated code will be blocked inside offline connect-src 'none' sandbox."
            : "Offline-safe self-contained artifact confined inside sandbox=\"allow-scripts\" (no allow-same-origin).",
        },
      ];
      return {
        status: "local_static_preview",
        fileCount: data.files.length,
        panels,
      };
    }

    return {
      PREVIEW_IFRAME_CSP,
      PERSONA_CHIPS,
      sanitizeRelativeArtifactPath,
      extractArtifactFiles,
      buildSandboxedPreviewDocument,
      runLocalDesignCritiquePreview,
    };
  }
);
