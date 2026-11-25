/* Chat send path. Loaded after app.js so it shares the same globals.
   Token events still arrive from window.secondBrain.streamChat. */
async function sendChat() {
  const queryInput = document.getElementById("chatQueryInput");
  const query = queryInput.value.trim();
  if (!query) {
    showToast("Enter a question to query your vault.", "warn");
    return;
  }

  if (activeStreamingTurnId && chatAbortManager.isActive()) {
    chatSession.markCancelled(activeStreamingTurnId);
  }

  const streamHandle = chatAbortManager.beginStream({
    timeoutMs: 90000,
    onTimeout: () => {
      if (activeStreamingTurnId) {
        chatSession.applyEvent(activeStreamingTurnId, "error", {
          error: "Chat stream timed out after 90 seconds.",
        });
        renderAllChatTurns();
      }
      setChatStreamingState(false);
    },
  });

  const priorHistory = chatSession.buildPriorHistory();
  const threadSelVal = document.getElementById("chatThreadSelect")?.value || "";
  const activeTid = chatSession.getThreadId() || threadSelVal;

  const reqPayload = ChatHelper.buildChatStreamRequest({
    query,
    mode: document.getElementById("chatModeSelect").value,
    host_id: document.getElementById("chatHostSelect").value || "ollama_local",
    model_override: document.getElementById("chatModelInput").value,
    retrieval_mode: document.getElementById("chatRetrievalModeSelect").value,
    answer_view: document.getElementById("chatAnswerViewSelect").value,
    project_scope: document.getElementById("chatScopeInput").value,
    include_preferences: document.getElementById("chatIncludePrefsCheck")
      .checked,
    include_expired: Boolean(
      document.getElementById("chatIncludeExpiredCheck")?.checked
    ),
    as_of: document.getElementById("chatAsOfInput")?.value || "",
    thread_id: activeTid,
    work_mode: activeWorkMode,
    persona_id: activePersonaId,
    behavior_mode: document.getElementById("behaviorModeSelect")?.value || "autopilot",
    custom_rules: document.getElementById("chatCustomRulesInput")?.value || "",
    done_criteria: document.getElementById("chatDoneCriteriaInput")?.value || "",
    history: priorHistory,
  });

  const turn = chatSession.startTurn(reqPayload);
  activeStreamingTurnId = turn.id;
  queryInput.value = "";
  chatWarningEl.style.display = "none";
  chatBannerEl.textContent = "Retrieving context...";
  setChatStreamingState(true);
  renderAllChatTurns();
  renderChatSourceCards(turn);

  try {
    const res = await window.secondBrain.streamChat(reqPayload);
    if (
      res &&
      !res.ok &&
      !res.cancelled &&
      !streamHandle.signal.aborted &&
      turn.status === "streaming"
    ) {
      const errMsg = res.error || `HTTP ${res.status}`;
      chatSession.applyEvent(turn.id, "error", { error: errMsg });
      chatBannerEl.textContent = `Error: ${errMsg}`;
      renderAllChatTurns();
      showToast(errMsg, "danger");
    }
  } catch (err) {
    if (!streamHandle.signal.aborted) {
      const errMsg = err?.message || String(err);
      chatSession.applyEvent(turn.id, "error", { error: errMsg });
      chatBannerEl.textContent = `Error: ${errMsg}`;
      renderAllChatTurns();
      showToast(errMsg, "danger");
    }
  } finally {
    streamHandle.finish();
    if (activeStreamingTurnId === turn.id) {
      setChatStreamingState(false);
    }
  }
}

document.getElementById("chatSendBtn").addEventListener("click", sendChat);
document.getElementById("chatQueryInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendChat();
});
