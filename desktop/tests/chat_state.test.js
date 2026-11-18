"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  RESEARCH_LEADS_WARNING_TITLE,
  buildChatStreamRequest,
  createChatSession,
  createStreamAbortManager,
} = require("../renderer/chat_state");

describe("Multi-turn Chat Session & AbortController Stream Manager", () => {
  it("preserves multi-turn history, per-turn sources, grounding, latency, and UNVERIFIED LEADS block", () => {
    const session = createChatSession();

    const t1 = session.startTurn(
      buildChatStreamRequest({
        query: "What is our PostgreSQL migration rule?",
        mode: "stream",
        host_id: "ollama_local",
        retrieval_mode: "rrf",
        answer_view: "standard",
        project_scope: "project:alpha",
      })
    );

    session.applyEvent(t1.id, "context", {
      retrieval_ms: 8.4,
      sources: [
        {
          citation_id: "V1",
          tier: "verified",
          filename: "pg-rule.md",
          claim: "Always lock schema migrations with advisory locks.",
        },
      ],
    });
    session.applyEvent(t1.id, "token", { text: "Use advisory locks " });
    session.applyEvent(t1.id, "token", { text: "for migrations [V1]." });
    session.applyEvent(t1.id, "grounding", {
      fully_cited: true,
      cited_sources: ["V1"],
      invalid_citations: [],
      uncited_sentences: [],
    });
    session.applyEvent(t1.id, "done", { total_ms: 142.5 });

    const t2 = session.startTurn(
      buildChatStreamRequest({
        query: "Any web scout notes on connection pooling?",
        mode: "stream",
        host_id: "ollama_local",
        model_override: "llama3.2:3b",
        retrieval_mode: "dense",
        answer_view: "research",
      })
    );
    // Test SSE envelope where event is 'message' and type is inside data.type
    session.applyEvent(t2.id, "message", {
      type: "context",
      retrieval_ms: 11.2,
      sources: [
        {
          citation_id: "L1",
          tier: "unverified_lead",
          filename: "inbox-pgbouncer.md",
          snippet: "Draft lead on transaction pooling.",
        },
      ],
    });
    session.applyEvent(t2.id, "message", {
      type: "research_leads",
      label: RESEARCH_LEADS_WARNING_TITLE,
      text: "Found 1 quarantined web lead [L1].",
      leads: [
        {
          citation_id: "L1",
          title: "PgBouncer Draft",
          snippet: "Transaction pooling caveats.",
        },
      ],
    });
    session.applyEvent(t2.id, "token", {
      text: "No owner-approved atoms cover connection pooling.",
    });
    session.applyEvent(t2.id, "done", { total_ms: 95.0 });

    const turns = session.getTurns();
    assert.equal(turns.length, 2);
    assert.equal(
      turns[0].assistantText,
      "Use advisory locks for migrations [V1]."
    );
    assert.equal(turns[0].retrieval_ms, 8.4);
    assert.equal(turns[0].total_ms, 142.5);
    assert.equal(turns[0].grounding.fully_cited, true);
    assert.equal(session.findSourceIndexByCitation(turns[0].id, "V1"), 0);

    // Research leads must be stored separately and NEVER merged into assistantText
    assert.ok(turns[1].researchLeads);
    assert.equal(turns[1].researchLeads.header, RESEARCH_LEADS_WARNING_TITLE);
    assert.doesNotMatch(turns[1].assistantText, /Found 1 quarantined web lead/);

    const exported = session.exportMarkdown();
    assert.match(exported, /## Turn 1/);
    assert.match(exported, /## Turn 2/);
    assert.match(exported, /UNVERIFIED LEADS — NOT OWNER-APPROVED/);
    assert.match(exported, /pg-rule\.md/);

    session.clearHistory();
    assert.equal(session.getTurns().length, 0);
  });

  it("cancels active stream via AbortController on Stop, new submission, and timeout", async () => {
    const ipcAborts = [];
    const manager = createStreamAbortManager({
      onAbortIpc: (reason) => ipcAborts.push(reason),
      defaultTimeoutMs: 5000,
    });

    // 1. Stop button cancellation
    const s1 = manager.beginStream();
    assert.equal(manager.isActive(), true);
    assert.equal(s1.signal.aborted, false);
    const stopRes = manager.abort("stop");
    assert.equal(stopRes.aborted, true);
    assert.equal(s1.signal.aborted, true);
    assert.equal(manager.isActive(), false);

    // 2. Automatic cancellation when a new submission starts
    const s2 = manager.beginStream();
    const s3 = manager.beginStream();
    assert.equal(s2.signal.aborted, true);
    assert.equal(s3.signal.aborted, false);
    s3.finish();
    assert.equal(manager.isActive(), false);

    // 3. Timeout cancellation
    let timedOutCalled = false;
    const s4 = manager.beginStream({
      timeoutMs: 25,
      onTimeout: () => {
        timedOutCalled = true;
      },
    });
    await new Promise((resolve) => setTimeout(resolve, 60));
    assert.equal(s4.signal.aborted, true);
    assert.equal(timedOutCalled, true);
    assert.deepEqual(ipcAborts, ["stop", "new_submission", "timeout"]);
  });
});
