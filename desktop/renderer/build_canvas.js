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

    function findTagOutsideComments(htmlDoc, targetTagRe, { last = false } = {}) {
      const src = String(htmlDoc || "");
      const tokenRe =
        /<!--[\s\S]*?(?:--!?>|$)|<script\b[^>]*>[\s\S]*?(?:<\/script\s*>|$)|<style\b[^>]*>[\s\S]*?(?:<\/style\s*>|$)|<[^>]+>/gi;
      let found = null;
      let m;
      while ((m = tokenRe.exec(src)) !== null) {
        const tok = m[0];
        if (tok.startsWith("<!--") || /^<(?:script|style)\b/i.test(tok)) {
          continue;
        }
        if (targetTagRe.test(tok)) {
          found = { index: m.index, length: tok.length, match: tok };
          if (!last) return found;
        }
      }
      return found;
    }

    function injectHeadContent(htmlDoc, snippet) {
      const src = String(htmlDoc || "");
      const headHit = findTagOutsideComments(src, /^<head\b[^>]*>$/i);
      if (headHit) {
        const insertAt = headHit.index + headHit.length;
        return `${src.slice(0, insertAt)}\n${snippet}${src.slice(insertAt)}`;
      }
      const htmlHit = findTagOutsideComments(src, /^<html\b[^>]*>$/i);
      if (htmlHit) {
        const insertAt = htmlHit.index + htmlHit.length;
        return `${src.slice(0, insertAt)}\n<head>\n${snippet}\n</head>${src.slice(insertAt)}`;
      }
      const doctypeHit = findTagOutsideComments(src, /^<!doctype\b[^>]*>$/i);
      if (doctypeHit) {
        const insertAt = doctypeHit.index + doctypeHit.length;
        return `${src.slice(0, insertAt)}\n<head>\n${snippet}\n</head>${src.slice(insertAt)}`;
      }
      return `<!DOCTYPE html>\n<html>\n<head>\n<meta charset="UTF-8" />\n${snippet}\n</head>\n<body>\n${src}\n</body>\n</html>`;
    }

    function appendBeforeBodyEnd(htmlDoc, snippet) {
      const src = String(htmlDoc || "");
      const bodyEndHit = findTagOutsideComments(src, /^<\/body\s*>$/i, { last: true });
      if (bodyEndHit) {
        // Splice at exact match index (avoiding String.prototype.replace string-pattern hazards like $&, $$, $', $`)
        return (
          src.slice(0, bodyEndHit.index) +
          `${snippet}\n${bodyEndHit.match}` +
          src.slice(bodyEndHit.index + bodyEndHit.length)
        );
      }
      if (/<\/body>/i.test(src) && !/<!--/.test(src)) {
        return src.replace(/<\/body>/i, () => `${snippet}\n</body>`);
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

      if (
        data.entryHtml &&
        findTagOutsideComments(data.entryHtml, /^<(?:!doctype|html)\b[^>]*>$/i) !== null
      ) {
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
