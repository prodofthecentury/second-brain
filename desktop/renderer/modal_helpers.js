"use strict";

(function initModalHelpersModule(root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.SecondBrainModal = api;
  }
})(
  typeof globalThis !== "undefined" ? globalThis : this,
  function createModalHelpersApi() {
    function formatShortcutText(text, platform = "win32") {
      const raw = String(text || "");
      if (platform !== "darwin") return raw;
      return raw
        .replace(/Ctrl\+Shift\+Space/g, "⌘⇧Space")
        .replace(/Ctrl\+Enter/g, "⌘+Enter")
        .replace(/Ctrl\+/g, "⌘");
    }

    function applyPlatformShortcutsToDom(doc, platform = "win32") {
      if (!doc || platform !== "darwin") return 0;
      let updated = 0;
      const kbds = doc.querySelectorAll ? doc.querySelectorAll(".kbd") : [];
      for (const node of kbds) {
        const next = formatShortcutText(node.textContent, platform);
        if (next !== node.textContent) {
          node.textContent = next;
          updated += 1;
        }
      }
      return updated;
    }

    function createModalController({
      backdropEl,
      titleEl,
      bodyEl,
      footerEl,
      closeBtnEl = null,
      doc = null,
    } = {}) {
      const ownerDoc =
        doc ||
        (backdropEl && backdropEl.ownerDocument) ||
        (typeof document !== "undefined" ? document : null);

      let pendingCancelHandler = null;

      function close({ fromCancel = true } = {}) {
        if (!backdropEl) return;
        backdropEl.classList.remove("open");
        if (bodyEl) bodyEl.replaceChildren();
        if (footerEl) footerEl.replaceChildren();
        if (fromCancel && typeof pendingCancelHandler === "function") {
          const fn = pendingCancelHandler;
          pendingCancelHandler = null;
          fn();
        } else {
          pendingCancelHandler = null;
        }
      }

      if (closeBtnEl) {
        closeBtnEl.addEventListener("click", () => close({ fromCancel: true }));
      }
      if (backdropEl) {
        backdropEl.addEventListener("click", (e) => {
          if (e.target === backdropEl) {
            close({ fromCancel: true });
          }
        });
      }

      function open({
        title = "Dialog",
        renderBody = null,
        renderFooter = null,
        onCancel = null,
      } = {}) {
        if (!backdropEl || !ownerDoc) return;
        if (typeof pendingCancelHandler === "function") {
          const prev = pendingCancelHandler;
          pendingCancelHandler = null;
          prev();
        }
        pendingCancelHandler = typeof onCancel === "function" ? onCancel : null;
        if (titleEl) titleEl.textContent = String(title);
        if (bodyEl) {
          bodyEl.replaceChildren();
          if (typeof renderBody === "function") {
            renderBody(bodyEl, ownerDoc);
          }
        }
        if (footerEl) {
          footerEl.replaceChildren();
          if (typeof renderFooter === "function") {
            renderFooter(footerEl, ownerDoc);
          }
        }
        backdropEl.classList.add("open");
      }

      function confirm({
        title = "Confirm Action",
        message = "",
        detail = "",
        confirmLabel = "Confirm",
        cancelLabel = "Cancel",
        confirmClass = "primary",
        actorDefault = null,
        actorLabel = "Owner Actor",
      } = {}) {
        if (!backdropEl || !ownerDoc) {
          return Promise.resolve({
            confirmed: false,
            actor: actorDefault || "",
          });
        }

        return new Promise((resolve) => {
          let settled = false;
          let actorInput = null;

          const finish = (confirmed) => {
            if (settled) return;
            settled = true;
            const actorVal = actorInput
              ? String(actorInput.value || "").trim() ||
                String(actorDefault || "desktop_owner")
              : String(actorDefault || "");
            pendingCancelHandler = null;
            close({ fromCancel: false });
            resolve({ confirmed: Boolean(confirmed), actor: actorVal });
          };

          open({
            title,
            onCancel: () => finish(false),
            renderBody(body, d) {
              const p = d.createElement("div");
              p.className = "sub-panel";
              p.textContent = String(message || "");
              body.appendChild(p);

              if (detail) {
                const det = d.createElement("div");
                det.className = "note-item-meta";
                det.textContent = String(detail);
                body.appendChild(det);
              }

              if (actorDefault !== null && actorDefault !== undefined) {
                const col = d.createElement("div");
                col.className = "field-col";
                const lbl = d.createElement("span");
                lbl.className = "field-label";
                lbl.textContent = actorLabel;
                actorInput = d.createElement("input");
                actorInput.type = "text";
                actorInput.value = String(actorDefault);
                col.appendChild(lbl);
                col.appendChild(actorInput);
                body.appendChild(col);
              }
            },
            renderFooter(footer, d) {
              const cancelBtn = d.createElement("button");
              cancelBtn.type = "button";
              cancelBtn.className = "btn";
              cancelBtn.textContent = cancelLabel;
              cancelBtn.addEventListener("click", () => finish(false));

              const okBtn = d.createElement("button");
              okBtn.type = "button";
              okBtn.className = `btn ${confirmClass}`.trim();
              okBtn.textContent = confirmLabel;
              okBtn.addEventListener("click", () => finish(true));

              footer.appendChild(cancelBtn);
              footer.appendChild(okBtn);
            },
          });
        });
      }

      function isOpen() {
        return Boolean(backdropEl && backdropEl.classList.contains("open"));
      }

      return {
        open,
        close,
        confirm,
        isOpen,
      };
    }

    return {
      formatShortcutText,
      applyPlatformShortcutsToDom,
      createModalController,
    };
  }
);
