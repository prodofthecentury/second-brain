(function () {
  "use strict";

  const VaultCrypto = globalThis.SecondBrainVaultCrypto;
  const Epistemic = globalThis.SecondBrainEpistemicEngine;

  const deviceStore = new VaultCrypto.EncryptedDeviceStore({
    recordId: "primary_vault",
    iterations: VaultCrypto.DEVICE_PBKDF2_ITERATIONS,
  });

  // Ephemeral in-memory pairing cache before user unlocks or sets PIN
  let pendingPairingMeta = null;
  let pendingDropPairing = null;
  let activeSearchCorpus = "verified";

  function byId(id) {
    return document.getElementById(id);
  }

  function makeEl(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function clearChildren(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function showBanner(msg, isError) {
    const banner = byId("statusBanner");
    banner.textContent = String(msg || "");
    banner.classList.remove("hidden", "banner-ok", "banner-error");
    banner.classList.add(isError ? "banner-error" : "banner-ok");
  }

  function updateNetworkIndicator() {
    const badge = byId("networkBadge");
    const online = typeof navigator !== "undefined" ? navigator.onLine !== false : true;
    if (!online) {
      badge.textContent = "Airplane / Offline Mode";
      badge.className = "pill pill-warn";
    } else {
      badge.textContent = "100% Offline Ready";
      badge.className = "pill pill-offline";
    }
  }

  function getActiveScopeOptions() {
    const rawProj = byId("globalProjectInput").value.trim();
    const rawAsOf = byId("globalAsOfInput").value.trim();
    return {
      project: rawProj || undefined,
      asOf: rawAsOf || undefined,
    };
  }

  function getCurrentVaultStateOrNull() {
    if (deviceStore.isLocked()) {
      return null;
    }
    return deviceStore.getUnlockedState();
  }

  function applyPairingMetaToInputs(meta) {
    if (!meta || typeof meta !== "object") {
      return;
    }
    if (meta.endpoint_url) {
      byId("syncEndpointInput").value = String(meta.endpoint_url);
    }
    if (meta.vault_id) {
      byId("syncVaultIdInput").value = String(meta.vault_id);
    }
    if (meta.auth_mode && ["none", "s3_sigv4", "bearer"].indexOf(meta.auth_mode) !== -1) {
      byId("syncAuthModeSelect").value = meta.auth_mode;
      updateAuthModeVisibility();
    }
    if (meta.s3_region) {
      byId("syncS3RegionInput").value = String(meta.s3_region);
    }
    if (meta.s3_access_key_id) {
      byId("syncS3AkInput").value = String(meta.s3_access_key_id);
    }
    if (meta.s3_secret_access_key) {
      byId("syncS3SkInput").value = String(meta.s3_secret_access_key);
    }
    if (meta.recovery_key_b64) {
      byId("syncMasterKeyInput").value = String(meta.recovery_key_b64);
    }
    if (meta.inbox_drop_pairing && typeof meta.inbox_drop_pairing === "object") {
      pendingDropPairing = meta.inbox_drop_pairing;
      updateDropBadge();
    }
  }

  function updateDropBadge() {
    const state = getCurrentVaultStateOrNull();
    const dropCfg =
      pendingDropPairing ||
      (state && state.pairingMeta && state.pairingMeta.inbox_drop_pairing) ||
      null;
    const badge = byId("dropPairBadge");
    if (dropCfg && dropCfg.key_id) {
      badge.textContent = "Paired (" + String(dropCfg.key_id).slice(0, 8) + ")";
      badge.className = "badge badge-ok";
    } else {
      badge.textContent = "Drop Unpaired";
      badge.className = "badge badge-warn";
    }
  }

  async function refreshLockUi() {
    const hasStored = await deviceStore.hasEncryptedSnapshot();
    const lockBtn = byId("vaultLockBtn");
    const lockCard = byId("lockGateCard");
    const lockTag = byId("lockStateTag");
    const unlockBtn = byId("unlockVaultBtn");

    if (deviceStore.isLocked()) {
      lockBtn.textContent = hasStored ? "Locked" : "Set PIN";
      lockBtn.className = "pill pill-btn pill-warn";
      lockCard.classList.remove("hidden");
      lockTag.textContent = hasStored ? "Encrypted Snapshot Stored" : "New Device Store";
      lockTag.className = "badge badge-warn";
      unlockBtn.textContent = hasStored ? "Unlock Vault" : "Initialize Encrypted Store";
      byId("syncEpochBadge").textContent = "Locked";
    } else {
      const st = deviceStore.getUnlockedState();
      const epoch = (st.syncCursor && st.syncCursor.epoch) || 0;
      const fileCount = Object.keys(st.files || {}).length;
      lockBtn.textContent = "Unlocked (" + fileCount + " files · Lock)";
      lockBtn.className = "pill pill-btn pill-ok";
      lockCard.classList.add("hidden");
      byId("syncEpochBadge").textContent = "Epoch " + epoch;
      if (st.pairingMeta) {
        applyPairingMetaToInputs(st.pairingMeta);
      }
      renderSyncedNotesList();
      renderAuditSummary();
    }
    updateDropBadge();
  }

  function scrubUrlFragment() {
    try {
      history.replaceState(null, "", window.location.pathname + window.location.search);
    } catch (_err) {
      window.location.hash = "";
    }
  }

  function importHashPairingOnBoot() {
    const rawHash = window.location.hash || "";
    if (!rawHash) {
      return;
    }
    if (rawHash.indexOf("vaultpair=") !== -1) {
      try {
        const parsed = VaultCrypto.parseVaultPairingFragment(rawHash);
        pendingPairingMeta = parsed;
        applyPairingMetaToInputs(parsed);
        showBanner(
          "Imported zero-knowledge vault pairing metadata (" +
            String(parsed.vault_id).slice(0, 12) +
            "…) and scrubbed URL hash.",
          false
        );
      } catch (err) {
        showBanner("Vault pairing import failed: " + err.message, true);
      } finally {
        scrubUrlFragment();
      }
      return;
    }
    if (rawHash.indexOf("pair=") !== -1) {
      try {
        pendingDropPairing = VaultCrypto.parseDropPairingFragment(rawHash);
        updateDropBadge();
        showBanner("Imported SBID1 Inbox Drop key and scrubbed URL hash.", false);
      } catch (err) {
        showBanner("Drop pairing import failed: " + err.message, true);
      } finally {
        scrubUrlFragment();
      }
    }
  }

  function consumeShareTargetQuery() {
    if (!window.location.search) {
      return;
    }
    const params = new URLSearchParams(window.location.search);
    let hasShare = false;
    if (params.get("title")) {
      byId("dropTitleInput").value = params.get("title");
      hasShare = true;
    }
    if (params.get("url")) {
      byId("dropUrlInput").value = params.get("url");
      hasShare = true;
    }
    if (params.get("text")) {
      const txt = params.get("text");
      if (!byId("dropUrlInput").value && /^https?:\/\/\S+$/i.test(txt.trim())) {
        byId("dropUrlInput").value = txt.trim();
      } else {
        byId("dropTextInput").value = txt;
      }
      hasShare = true;
    }
    if (hasShare) {
      switchTab("notes");
      try {
        history.replaceState(null, "", window.location.pathname);
      } catch (_ignore) {
        // Safe in file:// contexts
      }
    }
  }

  function updateAuthModeVisibility() {
    const mode = byId("syncAuthModeSelect").value;
    const sigv4El = byId("sigv4Fields");
    const bearerEl = byId("bearerFields");
    if (mode === "s3_sigv4") {
      sigv4El.classList.remove("hidden");
      bearerEl.classList.add("hidden");
    } else if (mode === "bearer") {
      sigv4El.classList.add("hidden");
      bearerEl.classList.remove("hidden");
    } else {
      sigv4El.classList.add("hidden");
      bearerEl.classList.add("hidden");
    }
  }

  function switchTab(tabName) {
    const tabs = ["sync", "search", "chat", "notes", "audit"];
    for (let i = 0; i < tabs.length; i += 1) {
      const t = tabs[i];
      const panel = byId("tab-" + t);
      if (t === tabName) {
        panel.classList.remove("hidden");
      } else {
        panel.classList.add("hidden");
      }
    }
    const navButtons = document.querySelectorAll(".nav-tab");
    navButtons.forEach(function (btn) {
      if (btn.getAttribute("data-tab") === tabName) {
        btn.classList.add("nav-active");
      } else {
        btn.classList.remove("nav-active");
      }
    });
    if (tabName === "notes" && !deviceStore.isLocked()) {
      renderSyncedNotesList();
    }
    if (tabName === "audit" && !deviceStore.isLocked()) {
      renderAuditSummary();
    }
  }

  async function ensureUnlockedForAction() {
    if (!deviceStore.isLocked()) {
      return deviceStore.getUnlockedState();
    }
    const pin = byId("devicePinInput").value;
    if (!pin || pin.length < 4) {
      throw new Error("Unlock the encrypted device store with your PIN (min 4 characters) first.");
    }
    const hasStored = await deviceStore.hasEncryptedSnapshot();
    if (hasStored) {
      const st = await deviceStore.unlock(pin);
      byId("devicePinInput").value = "";
      await refreshLockUi();
      return st;
    }
    await deviceStore.saveState(pin, {
      vaultId: byId("syncVaultIdInput").value.trim() || null,
      syncCursor: { epoch: 0, manifest_id: null, trusted_peers: {}, baseFileShas: {} },
      pairingMeta: pendingPairingMeta,
      files: {},
    });
    byId("devicePinInput").value = "";
    await refreshLockUi();
    return deviceStore.getUnlockedState();
  }

  // ─── Sync & .sbvault Import Handlers ───────────────────────────────────────

  async function handleUnlockOrInit() {
    const pin = byId("devicePinInput").value;
    try {
      const hasStored = await deviceStore.hasEncryptedSnapshot();
      if (hasStored) {
        await deviceStore.unlock(pin);
        showBanner("Unlocked on-device AES-256-GCM vault store.", false);
      } else {
        await deviceStore.saveState(pin, {
          vaultId: byId("syncVaultIdInput").value.trim() || (pendingPairingMeta && pendingPairingMeta.vault_id) || null,
          syncCursor: {
            epoch: 0,
            manifest_id: null,
            trusted_peers: (pendingPairingMeta && pendingPairingMeta.trusted_peers) || {},
            baseFileShas: {},
          },
          pairingMeta: pendingPairingMeta,
          files: {},
        });
        showBanner("Initialized encrypted IndexedDB vault store (PBKDF2 210,000 iterations).", false);
      }
      byId("devicePinInput").value = "";
      await refreshLockUi();
    } catch (err) {
      showBanner(err.message, true);
    }
  }

  async function handlePullSync() {
    const btn = byId("pullSyncBtn");
    btn.disabled = true;
    try {
      const currentState = await ensureUnlockedForAction();
      const endpointUrl = byId("syncEndpointInput").value.trim();
      if (!endpointUrl) {
        throw new Error("Enter the HTTPS (or http://127.0.0.1) sync endpoint URL.");
      }
      const authMode = byId("syncAuthModeSelect").value;
      const transport = new VaultCrypto.HttpObjectSyncTransport({
        endpointUrl: endpointUrl,
        authMode: authMode,
        region: byId("syncS3RegionInput").value.trim() || "auto",
        accessKeyId: byId("syncS3AkInput").value.trim(),
        secretAccessKey: byId("syncS3SkInput").value.trim(),
        bearerToken: byId("syncBearerInput").value.trim(),
      });

      const pairedPeers =
        (pendingPairingMeta && pendingPairingMeta.trusted_peers) ||
        (currentState.pairingMeta && currentState.pairingMeta.trusted_peers) ||
        {};
      const mergedTrusted = Object.assign(
        {},
        pairedPeers,
        (currentState.syncCursor && currentState.syncCursor.trusted_peers) || {}
      );

      const syncConfig = {
        vaultId: byId("syncVaultIdInput").value.trim() || currentState.vaultId || undefined,
        passphrase: byId("syncPassphraseInput").value || undefined,
        masterKeyB64: byId("syncMasterKeyInput").value.trim() || undefined,
        trustedPeers: mergedTrusted,
        pinMode: Object.keys(mergedTrusted).length > 0 ? "strict" : "tofu",
      };

      const pullRes = await VaultCrypto.pullVaultFromBackend(transport, syncConfig, {
        files: currentState.files,
        syncCursor: currentState.syncCursor,
      });

      const nextMeta = Object.assign({}, currentState.pairingMeta || {}, pendingPairingMeta || {}, {
        endpoint_url: endpointUrl,
        vault_id: pullRes.vaultId,
        auth_mode: authMode,
        s3_region: byId("syncS3RegionInput").value.trim() || "auto",
        trusted_peers: pullRes.syncCursor.trusted_peers,
        inbox_drop_pairing:
          pendingDropPairing ||
          (currentState.pairingMeta && currentState.pairingMeta.inbox_drop_pairing) ||
          null,
      });

      await deviceStore.saveState(null, {
        vaultId: pullRes.vaultId,
        syncCursor: pullRes.syncCursor,
        pairingMeta: nextMeta,
        files: pullRes.files,
      });

      byId("syncVaultIdInput").value = pullRes.vaultId;
      byId("syncStatsLine").textContent =
        "Epoch " +
        pullRes.epoch +
        " · Downloaded " +
        pullRes.downloadedBlobs +
        " blobs, reused " +
        pullRes.reusedBlobs +
        " · Total files: " +
        Object.keys(pullRes.files).length;
      await refreshLockUi();
      showBanner(
        "Verified & synced vault epoch " +
          pullRes.epoch +
          " (" +
          Object.keys(pullRes.files).length +
          " files encrypted at rest).",
        false
      );
    } catch (err) {
      showBanner("Sync pull failed: " + err.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  async function handleSbvaultImport() {
    const btn = byId("importSbvaultBtn");
    btn.disabled = true;
    try {
      const currentState = await ensureUnlockedForAction();
      const fileInput = byId("sbvaultFileInput");
      if (!fileInput.files || fileInput.files.length === 0) {
        throw new Error("Choose a .sbvault archive file to import.");
      }
      const file = fileInput.files[0];
      const buf = await file.arrayBuffer();
      const passphrase = byId("backupPassphraseInput").value;
      const unpacked = await VaultCrypto.importSbVaultArchive(new Uint8Array(buf), passphrase);

      await deviceStore.saveState(null, {
        vaultId: currentState.vaultId,
        syncCursor: currentState.syncCursor || { epoch: 0, manifest_id: null, trusted_peers: {}, baseFileShas: {} },
        pairingMeta: currentState.pairingMeta || pendingPairingMeta,
        files: unpacked.files,
      });
      byId("backupPassphraseInput").value = "";
      await refreshLockUi();
      showBanner(
        "Imported verified .sbvault (" +
          unpacked.fileCount +
          " files, " +
          unpacked.approvedAtoms +
          " approved atoms, Merkle root " +
          unpacked.merkleRoot.slice(0, 12) +
          "…).",
        false
      );
    } catch (err) {
      showBanner("Backup import failed: " + err.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  // ─── Offline Search Rendering ──────────────────────────────────────────────

  function renderSearchResults() {
    const listEl = byId("searchResultsList");
    clearChildren(listEl);
    if (deviceStore.isLocked()) {
      showBanner("Unlock the vault store first to search offline.", true);
      return;
    }
    const query = byId("searchQueryInput").value.trim();
    if (!query) {
      listEl.appendChild(makeEl("p", "meta-line", "Enter a search query above."));
      return;
    }
    const state = deviceStore.getUnlockedState();
    const scope = getActiveScopeOptions();

    if (activeSearchCorpus === "notes") {
      const noteHits = Epistemic.searchWorkingNotes(state.files, query, {
        project: scope.project,
        limit: 20,
      });
      if (noteHits.length === 0) {
        listEl.appendChild(makeEl("div", "result-card", "No working notes matched this query in the active scope."));
        return;
      }
      for (let i = 0; i < noteHits.length; i += 1) {
        const hit = noteHits[i];
        const card = makeEl("article", "result-card");
        const top = makeEl("div", "row-between");
        top.appendChild(makeEl("strong", null, hit.title));
        top.appendChild(makeEl("span", "badge badge-warn", "Unverified Note"));
        card.appendChild(top);
        card.appendChild(makeEl("p", "claim-body", hit.snippet));
        card.appendChild(makeEl("div", "citation-mono", hit.attribution + " · BM25 " + hit.score));
        listEl.appendChild(card);
      }
      return;
    }

    const mode = activeSearchCorpus === "preference" ? "preference" : "standard";
    const hits = Epistemic.searchVerifiedVault(state.files, query, {
      project: scope.project,
      asOf: scope.asOf,
      mode: mode,
      limit: 20,
    });

    if (hits.length === 0) {
      listEl.appendChild(
        makeEl(
          "div",
          "result-card",
          "0 verified owner-approved atoms matched this query in the current project/temporal scope."
        )
      );
      return;
    }

    for (let i = 0; i < hits.length; i += 1) {
      const h = hits[i];
      const card = makeEl("article", "result-card");
      const top = makeEl("div", "row-between");
      top.appendChild(makeEl("strong", null, h.filename));
      top.appendChild(
        makeEl(
          "span",
          h.is_preference ? "badge badge-pref" : "badge badge-ok",
          h.is_preference ? "Owner Preference" : "Verified Fact"
        )
      );
      card.appendChild(top);
      card.appendChild(makeEl("p", "claim-body", h.claim));
      card.appendChild(makeEl("blockquote", "quote-box", h.verbatim_source_quote));
      card.appendChild(
        makeEl(
          "div",
          "citation-mono",
          h.citation + " · SHA-256 " + h.evidence_sha256.slice(0, 12) + "… · score " + h.score
        )
      );
      listEl.appendChild(card);
    }
  }

  // ─── Offline Grounded Chat Rendering ───────────────────────────────────────

  async function handleAskChat() {
    const out = byId("chatOutputContainer");
    clearChildren(out);
    if (deviceStore.isLocked()) {
      showBanner("Unlock the vault store first to run grounded chat.", true);
      return;
    }
    const query = byId("chatQueryInput").value.trim();
    if (!query) {
      showBanner("Enter a question first.", true);
      return;
    }
    const mode = byId("chatModeSelect").value;
    const scope = getActiveScopeOptions();
    const state = deviceStore.getUnlockedState();

    try {
      const ans = await Epistemic.answerGroundedQuestion(state.files, query, {
        mode: mode,
        project: scope.project,
        asOf: scope.asOf,
        limit: 5,
      });

      const headerCard = makeEl("div", "result-card");
      const row = makeEl("div", "row-between");
      row.appendChild(makeEl("strong", null, "Mode: " + mode.toUpperCase()));
      row.appendChild(
        makeEl(
          "span",
          ans.abstained ? "badge badge-warn" : "badge badge-ok",
          ans.abstained ? "Abstained (Fail-Closed)" : ans.claims.length + " Verified Claim(s)"
        )
      );
      headerCard.appendChild(row);
      if (ans.abstained) {
        headerCard.appendChild(makeEl("p", "claim-body", ans.text.split("\n\n[UNVERIFIED LEADS]")[0]));
      }
      out.appendChild(headerCard);

      for (let i = 0; i < ans.claims.length; i += 1) {
        const c = ans.claims[i];
        const card = makeEl("article", "result-card");
        const top = makeEl("div", "row-between");
        top.appendChild(
          makeEl(
            "span",
            c.is_preference ? "badge badge-pref" : "badge badge-ok",
            c.is_preference ? "[Preference] Owner Subjective" : "Owner-Approved Verbatim Claim"
          )
        );
        card.appendChild(top);
        card.appendChild(makeEl("p", "claim-body", (c.is_preference ? "[Preference] " : "") + c.text));
        for (let j = 0; j < c.evidence.length; j += 1) {
          const ev = c.evidence[j];
          card.appendChild(makeEl("blockquote", "quote-box", ev.evidence_quote));
          card.appendChild(
            makeEl(
              "div",
              "citation-mono",
              "[" +
                ev.atom_id +
                " -> " +
                ev.evidence_source +
                "[" +
                ev.evidence_start +
                ":" +
                ev.evidence_end +
                "]] (SHA-256 " +
                ev.evidence_sha256.slice(0, 12) +
                "…)"
            )
          );
        }
        out.appendChild(card);
      }

      if (ans.unverified_leads && ans.unverified_leads.length > 0) {
        const leadsCard = makeEl("section", "result-card");
        const lTop = makeEl("div", "row-between");
        lTop.appendChild(makeEl("strong", null, "[UNVERIFIED LEADS]"));
        lTop.appendChild(makeEl("span", "badge badge-warn", "Not Approved Facts"));
        leadsCard.appendChild(lTop);
        for (let i = 0; i < ans.unverified_leads.length; i += 1) {
          const lead = ans.unverified_leads[i];
          leadsCard.appendChild(
            makeEl(
              "p",
              "hint",
              "[" + lead.source_kind + " · " + lead.review_status + "] " + lead.lead_id + ": " + lead.summary
            )
          );
        }
        out.appendChild(leadsCard);
      }
    } catch (err) {
      showBanner("Grounded chat error: " + err.message, true);
    }
  }

  // ─── Working Notes Browser & SBID1 Outbound Inbox Drop ─────────────────────

  function renderSyncedNotesList() {
    const container = byId("notesBrowseList");
    clearChildren(container);
    const state = getCurrentVaultStateOrNull();
    if (!state) {
      container.appendChild(makeEl("p", "meta-line", "Unlock vault to browse synced working notes."));
      return;
    }
    const paths = Object.keys(state.files || {})
      .filter(function (p) {
        return p.startsWith("notes/") && p.toLowerCase().endsWith(".md") && !p.endsWith("/README.md");
      })
      .sort();

    if (paths.length === 0) {
      container.appendChild(makeEl("p", "meta-line", "No working notes in notes/ yet."));
      return;
    }
    for (let i = 0; i < paths.length; i += 1) {
      const rel = paths[i];
      const shortName = rel.slice("notes/".length);
      let text = "";
      try {
        text = VaultCrypto.utf8DecodeStrict(state.files[rel]);
      } catch (_err) {
        continue;
      }
      const card = makeEl("article", "result-card");
      const row = makeEl("div", "row-between");
      row.appendChild(makeEl("strong", null, "[note: " + shortName + "]"));
      row.appendChild(makeEl("span", "badge badge-warn", "Unverified Note"));
      card.appendChild(row);
      card.appendChild(makeEl("p", "hint", text.slice(0, 320)));
      container.appendChild(card);
    }
  }

  function resolveActiveDropPairing() {
    if (pendingDropPairing) {
      return pendingDropPairing;
    }
    const state = getCurrentVaultStateOrNull();
    if (state && state.pairingMeta && state.pairingMeta.inbox_drop_pairing) {
      return state.pairingMeta.inbox_drop_pairing;
    }
    throw new Error("Pair an SBID1 drop key (#pair= or #vaultpair=) first.");
  }

  async function buildSbidDrop() {
    const pairing = resolveActiveDropPairing();
    const title = byId("dropTitleInput").value.trim();
    const url = byId("dropUrlInput").value.trim();
    const text = byId("dropTextInput").value.trim();
    if (!title && !url && !text) {
      throw new Error("Enter a title, URL, or note text to capture.");
    }
    const tags = byId("dropTagsInput")
      .value.split(",")
      .map(function (s) { return s.trim(); })
      .filter(Boolean);
    const enc = await VaultCrypto.encryptInboxDropEnvelope(
      { title: title, url: url, text: text, tags: tags, client: "mobile-vault" },
      pairing
    );
    return { enc: enc, pairing: pairing };
  }

  async function handleSendDrop(exportOnly) {
    try {
      const built = await buildSbidDrop();
      if (exportOnly) {
        const blob = new Blob([built.enc.envelopeBytes], { type: "application/octet-stream" });
        const dlUrl = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = dlUrl;
        link.download = built.enc.dropId + ".enc";
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(dlUrl);
        showBanner("Exported encrypted " + built.enc.objectKey + ".", false);
        return;
      }

      const endpoint = String(
        built.pairing.target_url || byId("syncEndpointInput").value || ""
      )
        .trim()
        .replace(/\/+$/, "");
      if (!endpoint) {
        throw new Error("Configure sync/drop endpoint URL or use Export .enc.");
      }
      const ak = String(built.pairing.s3_access_key_id || byId("syncS3AkInput").value || "").trim();
      const sk = String(built.pairing.s3_secret_access_key || byId("syncS3SkInput").value || "").trim();
      const region = String(built.pairing.s3_region || byId("syncS3RegionInput").value || "auto").trim();
      const transport = new VaultCrypto.HttpObjectSyncTransport({
        endpointUrl: endpoint,
        authMode: ak && sk ? "s3_sigv4" : byId("syncAuthModeSelect").value,
        region: region,
        accessKeyId: ak,
        secretAccessKey: sk,
        bearerToken: byId("syncBearerInput").value.trim(),
      });
      await transport.putObject(built.enc.objectKey, built.enc.envelopeBytes);
      byId("dropTitleInput").value = "";
      byId("dropUrlInput").value = "";
      byId("dropTextInput").value = "";
      showBanner("Encrypted & uploaded quarantined drop " + built.enc.objectKey + "!", false);
    } catch (err) {
      showBanner("Inbox Drop error: " + err.message, true);
    }
  }

  // ─── Offline Merkle & Ledger Audit Rendering ───────────────────────────────

  function renderAuditSummary() {
    const box = byId("auditSummaryBox");
    clearChildren(box);
    const state = getCurrentVaultStateOrNull();
    if (!state) {
      box.appendChild(makeEl("p", "meta-line", "Unlock vault to verify Merkle root and audit ledger."));
      return;
    }
    const rep = Epistemic.verifyVaultLedgerOffline(state.files);
    const badgeRow = makeEl("div", "row-between");
    badgeRow.appendChild(
      makeEl(
        "span",
        rep.chain_valid ? "badge badge-ok" : "badge badge-danger",
        rep.chain_valid ? "Hash Chain Valid" : "Ledger Tampered / Invalid"
      )
    );
    badgeRow.appendChild(
      makeEl(
        "span",
        rep.synced_with_head ? "badge badge-ok" : "badge badge-neutral",
        rep.entry_count > 0
          ? (rep.synced_with_head ? "Matches Checkpoint #" + rep.head_seq : "Uncommitted Live Delta")
          : "No Checkpoints Yet"
      )
    );
    box.appendChild(badgeRow);
    box.appendChild(makeEl("div", "citation-mono", "Live Merkle Root: " + rep.live_merkle_root));
    box.appendChild(
      makeEl(
        "p",
        "meta-line",
        "Approved Atoms: " +
          rep.approved_atoms +
          " · Active Playbooks: " +
          rep.accepted_playbooks +
          " · Ledger Entries: " +
          rep.entry_count
      )
    );
    if (rep.errors.length > 0) {
      box.appendChild(makeEl("p", "banner banner-error", rep.errors.join(" | ")));
    }
  }

  function handleProveLeaf() {
    const outBox = byId("proofOutputBox");
    if (deviceStore.isLocked()) {
      showBanner("Unlock vault first.", true);
      return;
    }
    const target = byId("proofTargetInput").value.trim();
    if (!target) {
      showBanner("Enter an atom or playbook path to prove.", true);
      return;
    }
    try {
      const state = deviceStore.getUnlockedState();
      const proof = Epistemic.proveLeafOffline(state.files, target);
      const valid = Epistemic.verifyMerkleProofOffline(proof);
      outBox.textContent = JSON.stringify({ verified_offline: valid, proof: proof }, null, 2);
      outBox.classList.remove("hidden");
    } catch (err) {
      outBox.classList.add("hidden");
      showBanner(err.message, true);
    }
  }

  // ─── Event Wiring & Boot ───────────────────────────────────────────────────

  function wireEvents() {
    window.addEventListener("online", updateNetworkIndicator);
    window.addEventListener("offline", updateNetworkIndicator);

    document.querySelectorAll(".nav-tab").forEach(function (btn) {
      btn.addEventListener("click", function () {
        switchTab(btn.getAttribute("data-tab"));
      });
    });

    byId("vaultLockBtn").addEventListener("click", async function () {
      if (!deviceStore.isLocked()) {
        deviceStore.lock();
        showBanner("Vault locked and RAM plaintext cleared.", false);
        await refreshLockUi();
      } else {
        byId("devicePinInput").focus();
      }
    });

    byId("unlockVaultBtn").addEventListener("click", handleUnlockOrInit);
    byId("wipeVaultBtn").addEventListener("click", async function () {
      await deviceStore.wipe();
      showBanner("Wiped encrypted device vault store.", false);
      await refreshLockUi();
    });

    byId("clearScopeBtn").addEventListener("click", function () {
      byId("globalProjectInput").value = "";
      byId("globalAsOfInput").value = "";
      showBanner("Cleared project scope and bi-temporal date filter.", false);
    });

    byId("syncAuthModeSelect").addEventListener("change", updateAuthModeVisibility);
    byId("pullSyncBtn").addEventListener("click", handlePullSync);
    byId("importSbvaultBtn").addEventListener("click", handleSbvaultImport);

    byId("applyVaultPairBtn").addEventListener("click", function () {
      try {
        const raw = byId("vaultPairInput").value.trim();
        const parsed = VaultCrypto.parseVaultPairingFragment(raw);
        pendingPairingMeta = parsed;
        applyPairingMetaToInputs(parsed);
        byId("vaultPairInput").value = "";
        showBanner("Applied #vaultpair= metadata for vault " + parsed.vault_id.slice(0, 12) + "…", false);
      } catch (err) {
        showBanner(err.message, true);
      }
    });

    ["searchCorpusVerifiedBtn", "searchCorpusPrefBtn", "searchCorpusNotesBtn"].forEach(function (btnId) {
      byId(btnId).addEventListener("click", function () {
        activeSearchCorpus = byId(btnId).getAttribute("data-corpus");
        ["searchCorpusVerifiedBtn", "searchCorpusPrefBtn", "searchCorpusNotesBtn"].forEach(function (otherId) {
          byId(otherId).classList.toggle("seg-active", otherId === btnId);
        });
        if (byId("searchQueryInput").value.trim()) {
          renderSearchResults();
        }
      });
    });

    byId("runSearchBtn").addEventListener("click", renderSearchResults);
    byId("searchQueryInput").addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") {
        renderSearchResults();
      }
    });

    byId("askChatBtn").addEventListener("click", handleAskChat);

    byId("saveDropPairBtn").addEventListener("click", function () {
      try {
        const raw = byId("dropPairInput").value.trim();
        pendingDropPairing = VaultCrypto.parseDropPairingFragment(raw);
        byId("dropPairInput").value = "";
        updateDropBadge();
        showBanner("Saved SBID1 forward-secret drop pairing.", false);
      } catch (err) {
        showBanner(err.message, true);
      }
    });

    byId("sendDropBtn").addEventListener("click", function () {
      handleSendDrop(false);
    });
    byId("exportDropEncBtn").addEventListener("click", function () {
      handleSendDrop(true);
    });

    byId("runAuditBtn").addEventListener("click", renderAuditSummary);
    byId("proveLeafBtn").addEventListener("click", handleProveLeaf);
  }

  function registerServiceWorker() {
    if (typeof navigator !== "undefined" && "serviceWorker" in navigator) {
      navigator.serviceWorker.register("./sw.js").catch(function () {
        // Ignore SW registration errors in file:// or headless test environments
      });
    }
  }

  updateNetworkIndicator();
  updateAuthModeVisibility();
  wireEvents();
  importHashPairingOnBoot();
  consumeShareTargetQuery();
  refreshLockUi();
  registerServiceWorker();
})();
