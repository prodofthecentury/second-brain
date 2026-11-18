"use strict";

(function initChatStateModule(root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.SecondBrainChatState = api;
  }
})(
  typeof globalThis !== "undefined" ? globalThis : this,
  function createChatStateApi() {
    const RESEARCH_LEADS_WARNING_TITLE =
      "UNVERIFIED LEADS — NOT OWNER-APPROVED";

    function buildChatStreamRequest(params = {}) {
      const payload = {
        query: String(params.query || "").trim(),
        mode: String(params.mode || "stream"),
        host_id: String(params.host_id || "ollama_local"),
        retrieval_mode: String(params.retrieval_mode || "auto"),
        answer_view: String(params.answer_view || "standard"),
        include_preferences:
          params.include_preferences !== undefined
            ? Boolean(params.include_preferences)
            : true,
      };
      const scope = String(params.project_scope || "").trim();
      if (scope) payload.project_scope = scope;
      const modelOv = String(params.model_override || "").trim();
      if (modelOv) payload.model_override = modelOv;
      return payload;
    }

    function createChatSession() {
      let turns = [];
      let nextSeq = 1;
      let selectedTurnId = null;

      function getTurnById(turnId) {
        return turns.find((t) => t.id === turnId) || null;
      }

      return {
        startTurn(params = {}) {
          const id = `turn-${nextSeq++}`;
          const turn = {
            id,
            query: String(params.query || "").trim(),
            mode: String(params.mode || "stream"),
            host_id: String(params.host_id || "ollama_local"),
            model_override: String(params.model_override || "").trim(),
            retrieval_mode: String(params.retrieval_mode || "auto"),
            answer_view: String(params.answer_view || "standard"),
            project_scope: String(params.project_scope || "").trim(),
            include_preferences:
              params.include_preferences !== undefined
                ? Boolean(params.include_preferences)
                : true,
            createdAt: params.createdAt || new Date().toISOString(),
            assistantText: "",
            sources: [],
            warning: "",
            researchLeads: null,
            verifiedClaims: null,
            grounding: null,
            retrieval_ms: null,
            total_ms: null,
            status: "streaming",
            error: "",
          };
          turns.push(turn);
          selectedTurnId = id;
          return turn;
        },

        applyEvent(turnId, rawEventName, data = {}) {
          const turn = getTurnById(turnId);
          if (!turn || !data) return null;

          const eventName =
            rawEventName && rawEventName !== "message"
              ? String(rawEventName)
              : typeof data.type === "string"
              ? data.type
              : "message";

          if (eventName === "context") {
            turn.sources = Array.isArray(data.sources) ? [...data.sources] : [];
            if (data.retrieval_ms !== undefined && data.retrieval_ms !== null) {
              turn.retrieval_ms = Number(data.retrieval_ms);
            }
            turn.warning = data.warning ? String(data.warning) : "";
            if (data.host_id) turn.host_id = String(data.host_id);
            if (data.retrieval_mode) {
              turn.retrieval_mode = String(data.retrieval_mode);
            }
            if (data.answer_view) turn.answer_view = String(data.answer_view);
            if (
              Array.isArray(data.leads) &&
              data.leads.length > 0 &&
              !turn.researchLeads
            ) {
              turn.researchLeads = {
                header: RESEARCH_LEADS_WARNING_TITLE,
                label: RESEARCH_LEADS_WARNING_TITLE,
                text: "",
                leads: [...data.leads],
              };
            }
            if (turn.mode === "instant") {
              turn.assistantText = turn.sources.length
                ? `Instant retrieval complete (${turn.sources.length} source card(s) retrieved — no LLM invoked).\n\n` +
                  turn.sources
                    .map(
                      (s) =>
                        `- [${s.citation_id || s.tier || "S"}] **${
                          s.title || s.filename || "Source"
                        }**: ${s.claim || s.excerpt || s.snippet || ""}`
                    )
                    .join("\n")
                : "No matching verified atoms or working notes found.";
            }
          } else if (eventName === "research_leads") {
            turn.researchLeads = {
              header: RESEARCH_LEADS_WARNING_TITLE,
              label: String(data.label || RESEARCH_LEADS_WARNING_TITLE),
              text: String(data.text || ""),
              leads: Array.isArray(data.leads) ? [...data.leads] : [],
            };
          } else if (eventName === "token") {
            turn.assistantText += String(data.text ?? data.token ?? "");
          } else if (eventName === "verified_answer") {
            turn.verifiedClaims = data;
            const claims = Array.isArray(data.claims) ? data.claims : [];
            if (!data.answerable || !claims.length) {
              turn.assistantText = String(
                data.reason ||
                  data.text ||
                  "Abstained: no owner-verified claims directly answer this question."
              );
            } else {
              turn.assistantText = String(
                data.text ||
                  `Grounded in ${claims.length} owner-verified claim(s):`
              );
            }
          } else if (eventName === "grounding") {
            turn.grounding = { ...data };
          } else if (eventName === "abstain") {
            turn.status = "abstained";
            turn.assistantText = `Abstained: ${
              data.reason || data.text || "Insufficient verified context."
            }`;
          } else if (eventName === "cancelled") {
            turn.status = "cancelled";
          } else if (eventName === "error") {
            turn.status = "error";
            turn.error = String(data.error || "Stream failed");
          } else if (eventName === "done") {
            const ms = data.total_ms ?? data.elapsed_ms;
            if (ms !== undefined && ms !== null) {
              turn.total_ms = Number(ms);
            }
            if (turn.status === "streaming") {
              turn.status = "done";
            }
          }
          return turn;
        },

        markCancelled(turnId) {
          const turn = getTurnById(turnId);
          if (turn && turn.status === "streaming") {
            turn.status = "cancelled";
          }
          return turn;
        },

        getTurns() {
          return turns.map((t) => ({ ...t }));
        },

        getTurn(turnId) {
          return getTurnById(turnId);
        },

        selectTurn(turnId) {
          if (getTurnById(turnId)) {
            selectedTurnId = turnId;
          }
          return getTurnById(selectedTurnId);
        },

        getSelectedTurn() {
          return (
            getTurnById(selectedTurnId) ||
            (turns.length ? turns[turns.length - 1] : null)
          );
        },

        findSourceIndexByCitation(turnId, citationId) {
          const turn = getTurnById(turnId);
          if (!turn || !Array.isArray(turn.sources)) return -1;
          const target = String(citationId || "")
            .replace(/^\[|\]$/g, "")
            .trim()
            .toUpperCase();
          return turn.sources.findIndex(
            (s) =>
              String(s.citation_id || "")
                .trim()
                .toUpperCase() === target
          );
        },

        clearHistory() {
          turns = [];
          selectedTurnId = null;
        },

        exportMarkdown() {
          if (!turns.length) {
            return "# Second Brain Chat Export\n\n_No turns recorded._\n";
          }
          const blocks = ["# Second Brain Chat Session Export", ""];
          turns.forEach((turn, idx) => {
            blocks.push(`## Turn ${idx + 1} — ${turn.createdAt}`);
            blocks.push(
              `- **Mode**: \`${turn.mode}\` · **Retrieval**: \`${turn.retrieval_mode}\` · **View**: \`${turn.answer_view}\` · **Host**: \`${turn.host_id}\`${
                turn.model_override ? ` (\`${turn.model_override}\`)` : ""
              }${turn.project_scope ? ` · **Scope**: \`${turn.project_scope}\`` : ""}`
            );
            blocks.push("");
            blocks.push(`### User`);
            blocks.push(turn.query);
            blocks.push("");
            blocks.push(`### Assistant`);
            blocks.push(
              turn.assistantText ||
                (turn.error ? `_Error: ${turn.error}_` : "_(no response)_")
            );
            blocks.push("");

            if (turn.researchLeads) {
              blocks.push(`> **${RESEARCH_LEADS_WARNING_TITLE}**`);
              if (turn.researchLeads.text) {
                blocks.push(`> ${turn.researchLeads.text}`);
              }
              for (const ld of turn.researchLeads.leads || []) {
                const title = ld.title || ld.filename || ld.url || "Lead";
                const snip = ld.snippet || ld.excerpt || ld.text || "";
                blocks.push(
                  `> - [${ld.citation_id || "L"}] ${title}${snip ? `: ${snip}` : ""}`
                );
              }
              blocks.push("");
            }

            if (turn.grounding) {
              const g = turn.grounding;
              blocks.push(
                `- **Grounding Audit**: ${
                  g.fully_cited ? "Fully Cited" : "Citation Warnings"
                }${
                  Array.isArray(g.cited_sources) && g.cited_sources.length
                    ? ` (Cited: ${g.cited_sources.map((c) => `[${c}]`).join(" ")})`
                    : ""
                }`
              );
            }
            if (turn.retrieval_ms !== null || turn.total_ms !== null) {
              const lat = [
                turn.retrieval_ms !== null
                  ? `retrieval ${turn.retrieval_ms} ms`
                  : "",
                turn.total_ms !== null ? `total ${turn.total_ms} ms` : "",
              ]
                .filter(Boolean)
                .join(", ");
              blocks.push(`- **Latency**: ${lat}`);
            }

            if (turn.sources.length) {
              blocks.push("");
              blocks.push("#### Retrieved Sources");
              for (const src of turn.sources) {
                blocks.push(
                  `- **[${src.citation_id || src.tier || "S"}]** \`${
                    src.filename || src.title || "source"
                  }\` (${src.tier || "source"}): ${
                    src.claim || src.excerpt || src.snippet || ""
                  }`
                );
              }
            }
            blocks.push("");
            blocks.push("---");
            blocks.push("");
          });
          return blocks.join("\n");
        },
      };
    }

    function createStreamAbortManager({
      onAbortIpc = null,
      defaultTimeoutMs = 90000,
    } = {}) {
      let active = null;
      let seq = 0;

      function clearActiveTimer(entry) {
        if (entry && entry.timer) {
          clearTimeout(entry.timer);
          entry.timer = null;
        }
      }

      return {
        isActive() {
          return Boolean(active && !active.controller.signal.aborted);
        },

        getActiveReason() {
          return active ? active.reason : null;
        },

        abort(reason = "stop") {
          if (!active) {
            return { aborted: false, reason: null };
          }
          const current = active;
          active = null;
          clearActiveTimer(current);
          if (!current.controller.signal.aborted) {
            current.reason = String(reason || "stop");
            current.controller.abort(current.reason);
            return { aborted: true, reason: current.reason, streamId: current.id };
          }
          return { aborted: false, reason: current.reason, streamId: current.id };
        },

        beginStream({ timeoutMs = defaultTimeoutMs, onTimeout = null } = {}) {
          if (active && !active.controller.signal.aborted) {
            this.abort("new_submission");
          }
          const id = `stream-${++seq}`;
          const controller = new AbortController();
          const entry = {
            id,
            controller,
            reason: null,
            timer: null,
          };

          controller.signal.addEventListener(
            "abort",
            () => {
              const abortReason = entry.reason || "aborted";
              if (typeof onAbortIpc === "function") {
                Promise.resolve(onAbortIpc(abortReason)).catch(() => {});
              }
            },
            { once: true }
          );

          if (timeoutMs && timeoutMs > 0) {
            entry.timer = setTimeout(() => {
              if (active === entry && !entry.controller.signal.aborted) {
                this.abort("timeout");
                if (typeof onTimeout === "function") {
                  onTimeout();
                }
              }
            }, timeoutMs);
            if (entry.timer && typeof entry.timer.unref === "function") {
              entry.timer.unref();
            }
          }

          active = entry;
          return {
            streamId: id,
            signal: controller.signal,
            finish() {
              clearActiveTimer(entry);
              if (active === entry) {
                active = null;
              }
            },
          };
        },
      };
    }

    return {
      RESEARCH_LEADS_WARNING_TITLE,
      buildChatStreamRequest,
      createChatSession,
      createStreamAbortManager,
    };
  }
);
