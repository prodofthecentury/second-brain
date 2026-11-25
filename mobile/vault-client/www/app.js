(function () {
  "use strict";

  const VaultCrypto = globalThis.SecondBrainVaultCrypto;
  const Epistemic = globalThis.SecondBrainEpistemicEngine;

  const OUTBOX_STORAGE_KEY = "sb_vault_client_sbid1_outbox_v1";
  const MAX_OUTBOX_ITEMS = 50;
  const NOTE_PREVIEW_COLLAPSE_CHARS = 320;

  const deviceStore = new VaultCrypto.EncryptedDeviceStore({
    recordId: "primary_vault",
    iterations: VaultCrypto.DEVICE_PBKDF2_ITERATIONS,
  });

  // Fallback in-memory outbox for environments without localStorage (e.g. headless Node tests)
  let memoryOutboxFallback = [];

  // Ephemeral in-memory pairing cache before user unlocks or sets PIN
  let pendingPairingMeta = null;
  let pendingDropPairing = null;
  let activeSearchCorpus = "verified";
  let pendingMediaAttachment = null;
  const inputsEditedWhileLocked = new Set();

  const LOCKED_TRACKED_INPUTS = [
    ["syncEndpointInput", "endpoint_url"],
    ["syncVaultIdInput", "vault_id"],
    ["syncAuthModeSelect", "auth_mode"],
    ["syncS3RegionInput", "s3_region"],
    ["syncS3AkInput", "s3_access_key_id"],
    ["syncS3SkInput", "s3_secret_access_key"],
    ["syncBearerInput", "bearer_token"],
    ["syncMasterKeyInput", "recovery_key_b64"],
  ];

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
    if (!node) {
      return;
    }
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function showBanner(msg, isError) {
    const banner = byId("statusBanner");
    if (!banner) {
      return;
    }
    banner.textContent = String(msg || "");
    banner.classList.remove("hidden", "banner-ok", "banner-error");
    banner.classList.add(isError ? "banner-error" : "banner-ok");
  }

  function updateNetworkIndicator() {
    const badge = byId("networkBadge");
    if (!badge) {
      return;
    }
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

  function collectTypedSyncMetaFromInputs(onlyEditedWhileLocked) {
    const endpoint = byId("syncEndpointInput").value.trim();
    const vaultId = byId("syncVaultIdInput").value.trim();
    const authMode = byId("syncAuthModeSelect").value;
    const region = byId("syncS3RegionInput").value.trim();
    const ak = byId("syncS3AkInput").value.trim();
    const sk = byId("syncS3SkInput").value.trim();
    const bearer = byId("syncBearerInput").value.trim();
    const masterKey = byId("syncMasterKeyInput").value.trim();

    if (onlyEditedWhileLocked) {
      const edited = {};
      if (inputsEditedWhileLocked.has("endpoint_url")) {
        edited.endpoint_url = endpoint;
      }
      if (inputsEditedWhileLocked.has("vault_id")) {
        edited.vault_id = vaultId;
      }
      if (inputsEditedWhileLocked.has("auth_mode") && ["none", "s3_sigv4", "bearer"].indexOf(authMode) !== -1) {
        edited.auth_mode = authMode;
      }
      if (inputsEditedWhileLocked.has("s3_region")) {
        edited.s3_region = region || "auto";
      }
      if (inputsEditedWhileLocked.has("s3_access_key_id")) {
        edited.s3_access_key_id = ak;
      }
      if (inputsEditedWhileLocked.has("s3_secret_access_key")) {
        edited.s3_secret_access_key = sk;
      }
      if (inputsEditedWhileLocked.has("bearer_token")) {
        edited.bearer_token = bearer;
      }
      if (inputsEditedWhileLocked.has("recovery_key_b64")) {
        edited.recovery_key_b64 = masterKey;
      }
      return edited;
    }

    return {
      endpoint_url: endpoint,
      vault_id: vaultId,
      auth_mode: ["none", "s3_sigv4", "bearer"].indexOf(authMode) !== -1 ? authMode : "none",
      s3_region: region || "auto",
      s3_access_key_id: ak,
      s3_secret_access_key: sk,
      bearer_token: bearer,
      recovery_key_b64: masterKey,
    };
  }

  function applyPairingMetaToInputs(meta) {
    if (!meta || typeof meta !== "object") {
      return;
    }
    if (meta.endpoint_url !== undefined && meta.endpoint_url !== null && String(meta.endpoint_url) !== "") {
      byId("syncEndpointInput").value = String(meta.endpoint_url);
    }
    if (meta.vault_id !== undefined && meta.vault_id !== null && String(meta.vault_id) !== "") {
      byId("syncVaultIdInput").value = String(meta.vault_id);
    }
    if (meta.auth_mode && ["none", "s3_sigv4", "bearer"].indexOf(meta.auth_mode) !== -1) {
      byId("syncAuthModeSelect").value = meta.auth_mode;
      updateAuthModeVisibility();
    }
    if (meta.s3_region) {
      byId("syncS3RegionInput").value = String(meta.s3_region);
    }
    if (meta.s3_access_key_id !== undefined && meta.s3_access_key_id !== null) {
      byId("syncS3AkInput").value = String(meta.s3_access_key_id);
    }
    if (meta.s3_secret_access_key !== undefined && meta.s3_secret_access_key !== null) {
      byId("syncS3SkInput").value = String(meta.s3_secret_access_key);
    }
    if (meta.bearer_token !== undefined && meta.bearer_token !== null) {
      byId("syncBearerInput").value = String(meta.bearer_token);
    }
    if (meta.recovery_key_b64 !== undefined && meta.recovery_key_b64 !== null) {
      byId("syncMasterKeyInput").value = String(meta.recovery_key_b64);
    }
    if (meta.inbox_drop_pairing && typeof meta.inbox_drop_pairing === "object") {
      pendingDropPairing = meta.inbox_drop_pairing;
      updateDropBadge();
    }
  }

  function clearDecryptedDomViewsOnLock() {
    clearChildren(byId("searchResultsList"));
    clearChildren(byId("chatOutputContainer"));
    clearChildren(byId("notesBrowseList"));
    clearChildren(byId("auditSummaryBox"));
    const proofBox = byId("proofOutputBox");
    if (proofBox) {
      proofBox.textContent = "";
      proofBox.classList.add("hidden");
    }
    clearChildren(byId("pendingAtomsList"));
    clearChildren(byId("syncConflictsList"));
    [
      "syncS3AkInput",
      "syncS3SkInput",
      "syncBearerInput",
      "syncPassphraseInput",
      "syncMasterKeyInput",
      "backupPassphraseInput",
      "vaultPairInput",
      "workingNoteTitleInput",
      "workingNoteTagsInput",
      "workingNoteBodyInput",
      "noteEditorFilenameInput",
      "noteEditorTitleInput",
      "noteEditorBodyInput",
      "dropTitleInput",
      "dropUrlInput",
      "dropTextInput",
      "dropCaptionInput",
      "dropTagsInput",
      "dropPairInput",
      "dropMediaCaptureInput",
    ].forEach(function (inputId) {
      const el = byId(inputId);
      if (el && "value" in el) {
        el.value = "";
      }
    });
    ["dropLocationBadge", "dropMediaBadge"].forEach(function (badgeId) {
      const el = byId(badgeId);
      if (el) {
        el.textContent = "";
      }
    });
    pendingMediaAttachment = null;
  }

  function updateDropBadge() {
    const state = getCurrentVaultStateOrNull();
    const dropCfg =
      pendingDropPairing ||
      (state && state.pairingMeta && state.pairingMeta.inbox_drop_pairing) ||
      null;
    const badge = byId("dropPairBadge");
    if (!badge) {
      return;
    }
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
    refreshOutboxUi();
  }

  async function applyNewVaultPairing(parsed) {
    if (!deviceStore.isLocked()) {
      const st = deviceStore.getUnlockedState();
      const typedNow = collectTypedSyncMetaFromInputs(true);
      const nextMeta = Object.assign({}, st.pairingMeta || {}, typedNow, parsed);
      if (parsed.inbox_drop_pairing && typeof parsed.inbox_drop_pairing === "object") {
        nextMeta.inbox_drop_pairing = parsed.inbox_drop_pairing;
        pendingDropPairing = parsed.inbox_drop_pairing;
      }
      const vaultChanged = Boolean(st.vaultId && parsed.vault_id && st.vaultId !== parsed.vault_id);
      const prevCursor = st.syncCursor || { epoch: 0, manifest_id: null, trusted_peers: {}, baseFileShas: {} };
      const nextCursor = vaultChanged
        ? {
            epoch: 0,
            manifest_id: null,
            trusted_peers: Object.assign({}, parsed.trusted_peers || {}),
            baseFileShas: {},
          }
        : {
            epoch: prevCursor.epoch || 0,
            manifest_id: prevCursor.manifest_id || null,
            trusted_peers: Object.assign({}, prevCursor.trusted_peers || {}, parsed.trusted_peers || {}),
            baseFileShas: prevCursor.baseFileShas || {},
          };
      await deviceStore.saveState(null, {
        vaultId: parsed.vault_id || st.vaultId || null,
        syncCursor: nextCursor,
        pairingMeta: nextMeta,
        files: vaultChanged ? {} : st.files || {},
      });
      pendingPairingMeta = null;
      applyPairingMetaToInputs(nextMeta);
      await refreshLockUi();
      return;
    }
    pendingPairingMeta = parsed;
    if (parsed.inbox_drop_pairing && typeof parsed.inbox_drop_pairing === "object") {
      pendingDropPairing = parsed.inbox_drop_pairing;
    }
    applyPairingMetaToInputs(parsed);
    updateDropBadge();
  }

  async function applyNewDropPairing(parsedDrop) {
    pendingDropPairing = parsedDrop;
    if (!deviceStore.isLocked()) {
      const st = deviceStore.getUnlockedState();
      const nextMeta = Object.assign({}, st.pairingMeta || {}, {
        inbox_drop_pairing: parsedDrop,
      });
      await deviceStore.saveState(null, {
        vaultId: st.vaultId,
        syncCursor: st.syncCursor,
        pairingMeta: nextMeta,
        files: st.files,
      });
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
        if (parsed.inbox_drop_pairing && typeof parsed.inbox_drop_pairing === "object") {
          pendingDropPairing = parsed.inbox_drop_pairing;
        }
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
    if (tabName === "notes") {
      refreshOutboxUi();
    }
    if (tabName === "audit" && !deviceStore.isLocked()) {
      renderAuditSummary();
      renderPendingAtomsReview();
    }
  }

  async function bindPinToHardwareKeystore(pin) {
    const hw = getHardwarePlugin();
    if (!hw || typeof hw.storeHardwareSecret !== "function") {
      return;
    }
    try {
      await hw.storeHardwareSecret({ key: "primary_vault_pin", value: String(pin) });
    } catch (_ignore) {
      // Best-effort hardware binding; never block PIN unlock/init if Keychain/Keystore unavailable
    }
  }

  async function unlockOrInitializeWithPin(pin) {
    if (!pin || pin.length < 4) {
      throw new Error("Unlock the encrypted device store with your PIN (min 4 characters) first.");
    }
    const typedWhileLocked = collectTypedSyncMetaFromInputs(true);
    const hasStored = await deviceStore.hasEncryptedSnapshot();
    if (hasStored) {
      const st = await deviceStore.unlock(pin);
      const hasPendingPair = Boolean(pendingPairingMeta && typeof pendingPairingMeta === "object");
      const hasPendingDrop = Boolean(pendingDropPairing && typeof pendingDropPairing === "object");
      const hasTypedOverrides = Object.keys(typedWhileLocked).length > 0;

      if (hasPendingPair || hasPendingDrop || hasTypedOverrides) {
        // Newly imported #vaultpair= / #pair= or newly typed inputs win over stale stored pairingMeta
        const mergedMeta = Object.assign(
          {},
          st.pairingMeta || {},
          typedWhileLocked,
          hasPendingPair ? pendingPairingMeta : {}
        );
        if (hasPendingDrop) {
          mergedMeta.inbox_drop_pairing = pendingDropPairing;
        } else if (hasPendingPair && pendingPairingMeta.inbox_drop_pairing) {
          mergedMeta.inbox_drop_pairing = pendingPairingMeta.inbox_drop_pairing;
        }
        const incomingVaultId =
          (hasPendingPair && pendingPairingMeta.vault_id) ||
          typedWhileLocked.vault_id ||
          st.vaultId ||
          null;
        const vaultSwitched = Boolean(
          hasPendingPair &&
            pendingPairingMeta.vault_id &&
            st.vaultId &&
            pendingPairingMeta.vault_id !== st.vaultId
        );
        const oldCursor = st.syncCursor || { epoch: 0, manifest_id: null, trusted_peers: {}, baseFileShas: {} };
        const mergedCursor = vaultSwitched
          ? {
              epoch: 0,
              manifest_id: null,
              trusted_peers: Object.assign({}, (pendingPairingMeta && pendingPairingMeta.trusted_peers) || {}),
              baseFileShas: {},
            }
          : {
              epoch: oldCursor.epoch || 0,
              manifest_id: oldCursor.manifest_id || null,
              trusted_peers: Object.assign(
                {},
                oldCursor.trusted_peers || {},
                (hasPendingPair && pendingPairingMeta.trusted_peers) || {}
              ),
              baseFileShas: oldCursor.baseFileShas || {},
            };
        await deviceStore.saveState(null, {
          vaultId: incomingVaultId,
          syncCursor: mergedCursor,
          pairingMeta: mergedMeta,
          files: vaultSwitched ? {} : st.files || {},
        });
      }
      pendingPairingMeta = null;
      inputsEditedWhileLocked.clear();
      await bindPinToHardwareKeystore(pin);
      return { initialized: false, state: deviceStore.getUnlockedState() };
    }

    const fullInputs = collectTypedSyncMetaFromInputs(false);
    const nonEmptyInit = {};
    Object.keys(fullInputs).forEach(function (k) {
      const val = fullInputs[k];
      if (val && !(k === "auth_mode" && val === "none") && !(k === "s3_region" && val === "auto")) {
        nonEmptyInit[k] = val;
      }
    });
    const initMeta = Object.assign({}, nonEmptyInit, typedWhileLocked, pendingPairingMeta || {});
    if (pendingDropPairing) {
      initMeta.inbox_drop_pairing = pendingDropPairing;
    }
    const initVaultId =
      (pendingPairingMeta && pendingPairingMeta.vault_id) ||
      nonEmptyInit.vault_id ||
      typedWhileLocked.vault_id ||
      null;
    await deviceStore.saveState(pin, {
      vaultId: initVaultId,
      syncCursor: {
        epoch: 0,
        manifest_id: null,
        trusted_peers: Object.assign({}, (pendingPairingMeta && pendingPairingMeta.trusted_peers) || {}),
        baseFileShas: {},
      },
      pairingMeta: Object.keys(initMeta).length > 0 ? initMeta : null,
      files: {},
    });
    pendingPairingMeta = null;
    inputsEditedWhileLocked.clear();
    await bindPinToHardwareKeystore(pin);
    return { initialized: true, state: deviceStore.getUnlockedState() };
  }

  async function persistCurrentInputsAndLock() {
    if (deviceStore.isLocked()) {
      return;
    }
    const st = deviceStore.getUnlockedState();
    const currentInputs = collectTypedSyncMetaFromInputs(false);
    const nextMeta = Object.assign({}, st.pairingMeta || {}, {
      endpoint_url: currentInputs.endpoint_url || (st.pairingMeta && st.pairingMeta.endpoint_url) || "",
      vault_id: currentInputs.vault_id || st.vaultId || (st.pairingMeta && st.pairingMeta.vault_id) || "",
      auth_mode: currentInputs.auth_mode || (st.pairingMeta && st.pairingMeta.auth_mode) || "none",
      s3_region: currentInputs.s3_region || (st.pairingMeta && st.pairingMeta.s3_region) || "auto",
      s3_access_key_id:
        currentInputs.s3_access_key_id || (st.pairingMeta && st.pairingMeta.s3_access_key_id) || "",
      s3_secret_access_key:
        currentInputs.s3_secret_access_key || (st.pairingMeta && st.pairingMeta.s3_secret_access_key) || "",
      bearer_token:
        currentInputs.bearer_token || (st.pairingMeta && st.pairingMeta.bearer_token) || "",
      recovery_key_b64:
        currentInputs.recovery_key_b64 || (st.pairingMeta && st.pairingMeta.recovery_key_b64) || "",
      inbox_drop_pairing:
        pendingDropPairing ||
        (st.pairingMeta && st.pairingMeta.inbox_drop_pairing) ||
        null,
    });
    await deviceStore.saveState(null, {
      vaultId: st.vaultId || currentInputs.vault_id || null,
      syncCursor: st.syncCursor,
      pairingMeta: nextMeta,
      files: st.files,
    });
    deviceStore.lock();
    clearDecryptedDomViewsOnLock();
    inputsEditedWhileLocked.clear();
  }

  async function ensureUnlockedForAction() {
    if (!deviceStore.isLocked()) {
      return deviceStore.getUnlockedState();
    }
    const pin = byId("devicePinInput").value;
    const outcome = await unlockOrInitializeWithPin(pin);
    byId("devicePinInput").value = "";
    await refreshLockUi();
    return outcome.state;
  }

  // ─── Sync & .sbvault Import Handlers ───────────────────────────────────────

  async function handleUnlockOrInit() {
    const pin = byId("devicePinInput").value;
    try {
      const outcome = await unlockOrInitializeWithPin(pin);
      byId("devicePinInput").value = "";
      await refreshLockUi();
      if (outcome.initialized) {
        showBanner("Initialized encrypted IndexedDB vault store (PBKDF2 210,000 iterations).", false);
      } else {
        showBanner("Unlocked on-device AES-256-GCM vault store.", false);
      }
    } catch (err) {
      showBanner(err.message, true);
    }
  }

  let activeSyncConflicts = [];

  function getHardwarePlugin() {
    if (
      typeof globalThis.Capacitor === "object" &&
      globalThis.Capacitor !== null &&
      globalThis.Capacitor.Plugins &&
      globalThis.Capacitor.Plugins.SecondBrainHardware
    ) {
      return globalThis.Capacitor.Plugins.SecondBrainHardware;
    }
    return null;
  }

  async function ensureOrCreateMobileIdentity(currentState) {
    const meta = (currentState && currentState.pairingMeta) || {};
    if (meta.mobile_identity && meta.mobile_identity.seedB64 && meta.mobile_identity.pubkeyB64) {
      return meta.mobile_identity;
    }
    const generated = await VaultCrypto.generateEd25519Keypair();
    const nextMeta = Object.assign({}, meta, { mobile_identity: generated });
    await deviceStore.saveState(null, {
      vaultId: currentState.vaultId,
      syncCursor: currentState.syncCursor,
      pairingMeta: nextMeta,
      files: currentState.files,
    });
    return generated;
  }

  function renderMobileIdentityBanner(identity) {
    const el = byId("mobileDeviceIdentityText");
    if (!el) return;
    if (!identity) {
      el.textContent = "No mobile Ed25519 write-back key generated yet.";
      return;
    }
    el.textContent =
      "Device ID: " +
      identity.deviceId +
      " | Public Key (pin via desktop `mobile register-peer`): " +
      identity.pubkeyB64;
  }

  function renderSyncConflictsList(conflicts) {
    const card = byId("syncConflictsCard");
    const badge = byId("syncConflictsCountBadge");
    const list = byId("syncConflictsList");
    if (!card || !badge || !list) return;
    clearChildren(list);
    if (!conflicts || conflicts.length === 0) {
      card.classList.add("hidden");
      badge.textContent = "0 conflicts";
      return;
    }
    card.classList.remove("hidden");
    badge.textContent = conflicts.length + " conflict(s)";
    conflicts.forEach(function (c, idx) {
      const row = makeEl("article", "result-card");
      row.appendChild(makeEl("strong", null, c.relPath));
      row.appendChild(
        makeEl("div", "citation-mono", "Local: " + c.localSha.slice(0, 10) + " vs Remote: " + c.remoteSha.slice(0, 10))
      );
      const btns = makeEl("div", "actions-inline");
      [
        ["keep_local", "Keep Local"],
        ["accept_remote", "Accept Remote"],
        ["keep_both", "Keep Both"],
      ].forEach(function (pair) {
        const b = makeEl("button", "btn btn-tiny", pair[1]);
        b.type = "button";
        b.addEventListener("click", async function () {
          try {
            const st = await ensureUnlockedForAction();
            const nextFiles = Epistemic.resolveSyncConflictOffline(st.files, c, pair[0]);
            const nextBase = Object.assign({}, (st.syncCursor && st.syncCursor.baseFileShas) || {});
            nextBase[c.relPath] = c.remoteSha;
            await deviceStore.saveState(null, {
              vaultId: st.vaultId,
              syncCursor: Object.assign({}, st.syncCursor || {}, { baseFileShas: nextBase }),
              pairingMeta: st.pairingMeta,
              files: nextFiles,
            });
            activeSyncConflicts.splice(idx, 1);
            renderSyncConflictsList(activeSyncConflicts);
            renderSyncedNotesList();
            showBanner("Resolved conflict on " + c.relPath + " (" + pair[0] + ").", false);
          } catch (err) {
            showBanner("Conflict resolve error: " + err.message, true);
          }
        });
        btns.appendChild(b);
      });
      row.appendChild(btns);
      list.appendChild(row);
    });
  }

  async function handlePushSync() {
    const btn = byId("pushSyncBtn");
    if (btn) btn.disabled = true;
    try {
      const currentState = await ensureUnlockedForAction();
      if (activeSyncConflicts.length > 0) {
        throw new Error("Resolve pending 3-way sync conflicts before pushing.");
      }
      const endpointUrl = byId("syncEndpointInput").value.trim();
      if (!endpointUrl) {
        throw new Error("Enter the HTTPS (or http://127.0.0.1) sync endpoint URL.");
      }
      const authMode = byId("syncAuthModeSelect").value;
      const regionVal = byId("syncS3RegionInput").value.trim() || "auto";
      const s3AkVal = byId("syncS3AkInput").value.trim();
      const s3SkVal = byId("syncS3SkInput").value.trim();
      const bearerVal = byId("syncBearerInput").value.trim();
      const masterKeyVal = byId("syncMasterKeyInput").value.trim();

      const transport = new VaultCrypto.HttpObjectSyncTransport({
        endpointUrl: endpointUrl,
        authMode: authMode,
        region: regionVal,
        accessKeyId: s3AkVal,
        secretAccessKey: s3SkVal,
        bearerToken: bearerVal,
      });

      const identity = await ensureOrCreateMobileIdentity(currentState);
      renderMobileIdentityBanner(identity);
      const freshState = deviceStore.getUnlockedState();

      const pushRes = await VaultCrypto.pushVaultToBackend(
        transport,
        {
          vaultId: byId("syncVaultIdInput").value.trim() || freshState.vaultId || undefined,
          passphrase: byId("syncPassphraseInput").value || undefined,
          masterKeyB64: masterKeyVal || (freshState.pairingMeta && freshState.pairingMeta.recovery_key_b64) || undefined,
          trustedPeers: (freshState.syncCursor && freshState.syncCursor.trusted_peers) || {},
        },
        freshState,
        identity
      );

      await deviceStore.saveState(null, {
        vaultId: pushRes.vaultId,
        syncCursor: pushRes.syncCursor,
        pairingMeta: freshState.pairingMeta,
        files: pushRes.files,
      });
      byId("syncStatsLine").textContent =
        "Pushed Epoch " +
        pushRes.epoch +
        " · Uploaded " +
        pushRes.uploadedBlobs +
        " blobs, reused " +
        pushRes.reusedBlobs;
      const hw = getHardwarePlugin();
      if (hw && typeof hw.postSyncNotification === "function") {
        hw.postSyncNotification({
          title: "Second Brain E2EE Push",
          body: "Signed epoch " + pushRes.epoch + " synced to remote.",
        }).catch(function () {});
      }
      await refreshLockUi();
      showBanner("Pushed signed E2EE vault epoch " + pushRes.epoch + " (device " + pushRes.deviceId + ").", false);
    } catch (err) {
      showBanner("Sync push failed: " + err.message, true);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function handleSaveMobileNote() {
    try {
      const state = await ensureUnlockedForAction();
      const fnInput = byId("noteEditorFilenameInput");
      const titleInput = byId("noteEditorTitleInput");
      const bodyInput = byId("noteEditorBodyInput");
      const rawTitle = titleInput ? titleInput.value.trim() : "";
      const rawBody = bodyInput ? bodyInput.value : "";
      const rawFn = fnInput ? fnInput.value.trim() : "";
      if (!rawTitle && !rawBody.trim() && !rawFn) {
        throw new Error("Enter a note title, filename, or Markdown body.");
      }
      const res = Epistemic.createOrUpdateNoteOffline(state.files, {
        filename: rawFn || rawTitle || "mobile-note",
        title: rawTitle || rawFn || "Mobile Note",
        body: rawBody,
        tags: ["mobile"],
      });
      await deviceStore.saveState(null, {
        vaultId: state.vaultId,
        syncCursor: state.syncCursor,
        pairingMeta: state.pairingMeta,
        files: res.files,
      });
      renderSyncedNotesList();
      showBanner("Saved encrypted note " + res.relPath + " (ready for Push Signed Delta).", false);
    } catch (err) {
      showBanner("Note save failed: " + err.message, true);
    }
  }

  function renderPendingAtomsReview() {
    const listEl = byId("pendingAtomsList");
    if (!listEl) return;
    clearChildren(listEl);
    const state = getCurrentVaultStateOrNull();
    if (!state) {
      listEl.appendChild(makeEl("p", "meta-line", "Unlock vault to review pending atoms."));
      return;
    }
    const pending = Epistemic.listPendingAtomsOffline(state.files);
    if (pending.length === 0) {
      listEl.appendChild(makeEl("p", "meta-line", "No pending atoms in synced vault store."));
      return;
    }
    pending.forEach(function (item) {
      const card = makeEl("article", "result-card");
      const head = makeEl("div", "row-between");
      head.appendChild(makeEl("strong", null, item.filename));
      head.appendChild(
        makeEl(
          "span",
          item.anchor_valid ? "badge badge-ok" : "badge badge-danger",
          item.anchor_valid ? "Anchor Valid" : "Invalid Anchor (" + item.anchor_reason + ")"
        )
      );
      card.appendChild(head);
      card.appendChild(makeEl("p", "claim-body", item.claim));
      const btns = makeEl("div", "actions-inline");
      const approveBtn = makeEl("button", "btn btn-tiny btn-primary", "Approve");
      approveBtn.type = "button";
      approveBtn.disabled = !item.anchor_valid;
      approveBtn.addEventListener("click", async function () {
        try {
          const cur = await ensureUnlockedForAction();
          const rev = Epistemic.reviewAtomOffline(cur.files, item.filename, "accepted", {
            expectedSha256: item.sha256,
          });
          await deviceStore.saveState(null, {
            vaultId: cur.vaultId,
            syncCursor: cur.syncCursor,
            pairingMeta: cur.pairingMeta,
            files: rev.files,
          });
          renderPendingAtomsReview();
          renderAuditSummary();
          showBanner("Approved " + item.filename + " (logged to decision-record.jsonl).", false);
        } catch (err) {
          showBanner("Approve failed: " + err.message, true);
        }
      });
      const rejectBtn = makeEl("button", "btn btn-tiny btn-danger", "Reject");
      rejectBtn.type = "button";
      rejectBtn.addEventListener("click", async function () {
        try {
          const cur = await ensureUnlockedForAction();
          const rev = Epistemic.reviewAtomOffline(cur.files, item.filename, "rejected", {
            expectedSha256: item.sha256,
          });
          await deviceStore.saveState(null, {
            vaultId: cur.vaultId,
            syncCursor: cur.syncCursor,
            pairingMeta: cur.pairingMeta,
            files: rev.files,
          });
          renderPendingAtomsReview();
          renderAuditSummary();
          showBanner("Rejected " + item.filename + ".", false);
        } catch (err) {
          showBanner("Reject failed: " + err.message, true);
        }
      });
      btns.appendChild(approveBtn);
      btns.appendChild(rejectBtn);
      card.appendChild(btns);
      listEl.appendChild(card);
    });
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
      const regionVal = byId("syncS3RegionInput").value.trim() || "auto";
      const s3AkVal = byId("syncS3AkInput").value.trim();
      const s3SkVal = byId("syncS3SkInput").value.trim();
      const bearerVal = byId("syncBearerInput").value.trim();
      const masterKeyVal = byId("syncMasterKeyInput").value.trim();

      const transport = new VaultCrypto.HttpObjectSyncTransport({
        endpointUrl: endpointUrl,
        authMode: authMode,
        region: regionVal,
        accessKeyId: s3AkVal,
        secretAccessKey: s3SkVal,
        bearerToken: bearerVal,
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
        masterKeyB64: masterKeyVal || undefined,
        trustedPeers: mergedTrusted,
        pinMode: Object.keys(mergedTrusted).length > 0 ? "strict" : "tofu",
        preserveConflicts: true,
      };

      const pullRes = await VaultCrypto.pullVaultFromBackend(transport, syncConfig, {
        files: currentState.files,
        syncCursor: currentState.syncCursor,
      });
      activeSyncConflicts = Array.isArray(pullRes.conflicts) ? pullRes.conflicts.slice() : [];
      renderSyncConflictsList(activeSyncConflicts);

      const nextMeta = Object.assign({}, currentState.pairingMeta || {}, pendingPairingMeta || {}, {
        endpoint_url: endpointUrl,
        vault_id: pullRes.vaultId,
        auth_mode: authMode,
        s3_region: regionVal,
        s3_access_key_id: s3AkVal,
        s3_secret_access_key: s3SkVal,
        bearer_token: bearerVal,
        recovery_key_b64: masterKeyVal || (currentState.pairingMeta && currentState.pairingMeta.recovery_key_b64) || "",
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
      pendingPairingMeta = null;

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

  // ─── Working Notes Browser & Offline SBID1 Outbox Queue ────────────────────

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

      const isLong = text.length > NOTE_PREVIEW_COLLAPSE_CHARS;
      const bodyEl = makeEl(
        "p",
        isLong ? "hint note-body note-collapsed" : "hint note-body",
        text
      );
      card.appendChild(bodyEl);

      if (isLong) {
        const toggleBtn = makeEl(
          "button",
          "btn-tiny note-toggle-btn",
          "Expand full note (" + text.length + " chars)"
        );
        toggleBtn.type = "button";
        toggleBtn.addEventListener("click", function () {
          const collapsed = bodyEl.classList.toggle("note-collapsed");
          toggleBtn.textContent = collapsed
            ? "Expand full note (" + text.length + " chars)"
            : "Collapse note";
        });
        card.appendChild(toggleBtn);
      }
      container.appendChild(card);
    }
  }

  function loadSbidOutbox() {
    try {
      if (typeof globalThis.localStorage !== "undefined" && globalThis.localStorage !== null) {
        const raw = globalThis.localStorage.getItem(OUTBOX_STORAGE_KEY);
        const parsed = raw ? JSON.parse(raw) : [];
        return Array.isArray(parsed) ? parsed : [];
      }
    } catch (_err) {
      // Fall back to in-memory outbox
    }
    return memoryOutboxFallback.slice();
  }

  function saveSbidOutbox(items) {
    const bounded = Array.isArray(items) ? items.slice(-MAX_OUTBOX_ITEMS) : [];
    memoryOutboxFallback = bounded.slice();
    try {
      if (typeof globalThis.localStorage !== "undefined" && globalThis.localStorage !== null) {
        globalThis.localStorage.setItem(OUTBOX_STORAGE_KEY, JSON.stringify(bounded));
      }
    } catch (_err) {
      // Ignore quota / storage errors; memoryOutboxFallback retains session items
    }
    refreshOutboxUi();
  }

  function refreshOutboxUi() {
    const card = byId("outboxCard");
    const badge = byId("outboxCountBadge");
    if (!card || !badge) {
      return;
    }
    const items = loadSbidOutbox();
    badge.textContent = items.length + " queued";
    if (items.length > 0) {
      card.classList.remove("hidden");
    } else {
      card.classList.add("hidden");
    }
  }

  function enqueueEncryptedSbidItem(encResult, endpointHint) {
    const queue = loadSbidOutbox();
    // Deduplicate by dropId; store ONLY forward-secret ECDH P-256 + AES-256-GCM ciphertext (zero plaintext!)
    const filtered = queue.filter(function (it) {
      return it && it.dropId !== encResult.dropId;
    });
    filtered.push({
      dropId: encResult.dropId,
      objectKey: encResult.objectKey,
      envelopeB64: VaultCrypto.bytesToBase64(encResult.envelopeBytes),
      createdAt: encResult.createdAt,
      endpointHint: String(endpointHint || ""),
    });
    saveSbidOutbox(filtered);
    return filtered.length;
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

  function buildDropTransportOrNull(pairing, endpointOverride) {
    const state = getCurrentVaultStateOrNull();
    const meta = (state && state.pairingMeta) || pendingPairingMeta || {};
    const endpoint = String(
      endpointOverride ||
        (pairing && pairing.target_url) ||
        byId("syncEndpointInput").value ||
        meta.endpoint_url ||
        ""
    )
      .trim()
      .replace(/\/+$/, "");
    if (!endpoint) {
      return null;
    }
    const ak = String(
      (pairing && pairing.s3_access_key_id) ||
        byId("syncS3AkInput").value ||
        meta.s3_access_key_id ||
        ""
    ).trim();
    const sk = String(
      (pairing && pairing.s3_secret_access_key) ||
        byId("syncS3SkInput").value ||
        meta.s3_secret_access_key ||
        ""
    ).trim();
    const bearer = String(byId("syncBearerInput").value || meta.bearer_token || "").trim();
    const region = String(
      (pairing && pairing.s3_region) ||
        byId("syncS3RegionInput").value ||
        meta.s3_region ||
        "auto"
    ).trim();
    const authMode = ak && sk ? "s3_sigv4" : bearer ? "bearer" : byId("syncAuthModeSelect").value;
    return new VaultCrypto.HttpObjectSyncTransport({
      endpointUrl: endpoint,
      authMode: authMode,
      region: region,
      accessKeyId: ak,
      secretAccessKey: sk,
      bearerToken: bearer,
    });
  }

  async function buildSbidDrop() {
    const pairing = resolveActiveDropPairing();
    const title = byId("dropTitleInput").value.trim();
    const url = byId("dropUrlInput").value.trim();
    const text = byId("dropTextInput").value.trim();
    if (!title && !url && !text && !pendingMediaAttachment) {
      throw new Error("Enter a title, URL, note text, or attach media to capture.");
    }
    const tags = byId("dropTagsInput")
      .value.split(",")
      .map(function (s) {
        return s.trim();
      })
      .filter(Boolean);
    const enc = await VaultCrypto.encryptInboxDropEnvelope(
      {
        title: title,
        url: url,
        text: text,
        tags: tags,
        attachment: pendingMediaAttachment || undefined,
        client: "mobile-vault",
      },
      pairing
    );
    return { enc: enc, pairing: pairing };
  }

  async function flushSbidOutbox(silentWhenEmpty) {
    const items = loadSbidOutbox();
    if (items.length === 0) {
      if (!silentWhenEmpty) {
        showBanner("Offline SBID1 outbox is already empty.", false);
      }
      return { sent: 0, remaining: 0 };
    }
    let pairing = null;
    try {
      pairing = resolveActiveDropPairing();
    } catch (_ignore) {
      pairing = null;
    }

    const remaining = [];
    let sent = 0;
    let lastErr = "";

    for (let i = 0; i < items.length; i += 1) {
      const item = items[i];
      if (!item || !item.objectKey || !item.envelopeB64) {
        continue;
      }
      try {
        const transport = buildDropTransportOrNull(pairing, item.endpointHint);
        if (!transport) {
          throw new Error("Configure sync/drop endpoint URL and unlock credentials to flush outbox.");
        }
        const envBytes = VaultCrypto.base64ToBytes(item.envelopeB64);
        await transport.putObject(item.objectKey, envBytes);
        sent += 1;
      } catch (err) {
        lastErr = err.message || String(err);
        remaining.push(item);
      }
    }

    saveSbidOutbox(remaining);
    if (remaining.length === 0) {
      showBanner("Flushed all " + sent + " queued SBID1 drop(s) to quarantined inbox.", false);
    } else if (!silentWhenEmpty) {
      showBanner(
        "Flushed " + sent + " drop(s); " + remaining.length + " still queued (" + lastErr + ").",
        sent === 0
      );
    }
    return { sent: sent, remaining: remaining.length };
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

      const endpointHint = String(built.pairing.target_url || byId("syncEndpointInput").value || "")
        .trim()
        .replace(/\/+$/, "");
      const isOffline = typeof navigator !== "undefined" && navigator.onLine === false;
      const transport = buildDropTransportOrNull(built.pairing, endpointHint);

      byId("dropTitleInput").value = "";
      byId("dropUrlInput").value = "";
      byId("dropTextInput").value = "";
      pendingMediaAttachment = null;
      const mediaEl = byId("dropMediaCaptureInput");
      if (mediaEl) mediaEl.value = "";

      if (isOffline || !transport) {
        const qCount = enqueueEncryptedSbidItem(built.enc, endpointHint);
        showBanner(
          "Queued forward-secret SBID1 capture in offline outbox (" + qCount + " queued).",
          false
        );
        return;
      }

      try {
        await transport.putObject(built.enc.objectKey, built.enc.envelopeBytes);
        showBanner("Encrypted & uploaded quarantined drop " + built.enc.objectKey + "!", false);
        if (loadSbidOutbox().length > 0) {
          await flushSbidOutbox(true);
        }
      } catch (netErr) {
        const qCount = enqueueEncryptedSbidItem(built.enc, endpointHint);
        showBanner(
          "Upload deferred (" +
            netErr.message +
            ") — queued encrypted SBID1 capture in outbox (" +
            qCount +
            " queued).",
          false
        );
      }
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
          ? rep.synced_with_head
            ? "Matches Checkpoint #" + rep.head_seq
            : "Uncommitted Live Delta"
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
    LOCKED_TRACKED_INPUTS.forEach(function (pair) {
      const el = byId(pair[0]);
      const fieldKey = pair[1];
      if (!el) {
        return;
      }
      const markDirty = function () {
        if (deviceStore.isLocked()) {
          inputsEditedWhileLocked.add(fieldKey);
        }
      };
      el.addEventListener("input", markDirty);
      el.addEventListener("change", markDirty);
    });

    window.addEventListener("online", function () {
      updateNetworkIndicator();
      if (loadSbidOutbox().length > 0) {
        flushSbidOutbox(true);
      }
    });
    window.addEventListener("offline", updateNetworkIndicator);

    document.querySelectorAll(".nav-tab").forEach(function (btn) {
      btn.addEventListener("click", function () {
        switchTab(btn.getAttribute("data-tab"));
      });
    });

    byId("vaultLockBtn").addEventListener("click", async function () {
      if (!deviceStore.isLocked()) {
        try {
          await persistCurrentInputsAndLock();
        } catch (_err) {
          deviceStore.lock();
          clearDecryptedDomViewsOnLock();
        }
        showBanner("Vault locked (credentials encrypted at rest; RAM plaintext cleared).", false);
        await refreshLockUi();
      } else {
        byId("devicePinInput").focus();
      }
    });

    byId("unlockVaultBtn").addEventListener("click", handleUnlockOrInit);
    const bioBtn = byId("biometricUnlockBtn");
    if (bioBtn) {
      bioBtn.addEventListener("click", async function () {
        const hw = getHardwarePlugin();
        if (!hw || typeof hw.unlockWithBiometrics !== "function") {
          showBanner("Native Biometrics available inside Capacitor iOS/Android shell; enter device PIN above.", true);
          return;
        }
        try {
          const authRes = await hw.unlockWithBiometrics({ reason: "Unlock Second Brain Encrypted Vault" });
          if (authRes && authRes.authenticated && typeof hw.loadHardwareSecret === "function") {
            const sec = await hw.loadHardwareSecret({ key: "primary_vault_pin" });
            if (sec && sec.found && sec.value) {
              byId("devicePinInput").value = sec.value;
              await handleUnlockOrInit();
              return;
            }
          }
          showBanner("Biometric prompt verified; enter PIN once to bind hardware secret.", false);
        } catch (err) {
          showBanner("Biometric unlock failed: " + (err && err.message ? err.message : String(err)), true);
        }
      });
    }
    byId("wipeVaultBtn").addEventListener("click", async function () {
      await deviceStore.wipe();
      clearDecryptedDomViewsOnLock();
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
    if (byId("pushSyncBtn")) {
      byId("pushSyncBtn").addEventListener("click", handlePushSync);
    }
    if (byId("genDeviceIdentityBtn")) {
      byId("genDeviceIdentityBtn").addEventListener("click", async function () {
        try {
          const st = await ensureUnlockedForAction();
          const identity = await ensureOrCreateMobileIdentity(st);
          renderMobileIdentityBanner(identity);
          showBanner("Mobile Ed25519 peer identity ready (" + identity.deviceId + ").", false);
        } catch (err) {
          showBanner("Identity generation failed: " + err.message, true);
        }
      });
    }
    if (byId("scheduleBgSyncBtn")) {
      byId("scheduleBgSyncBtn").addEventListener("click", async function () {
        const hw = getHardwarePlugin();
        if (!hw || typeof hw.scheduleBackgroundSync !== "function") {
          showBanner("Background sync (BGTaskScheduler / WorkManager) activates in the native iOS/Android app.", false);
          return;
        }
        try {
          const res = await hw.scheduleBackgroundSync({ intervalMinutes: 30 });
          showBanner("Background sync scheduled: " + JSON.stringify(res), false);
        } catch (err) {
          showBanner("Background sync scheduling failed: " + err.message, true);
        }
      });
    }
    if (byId("saveMobileNoteBtn")) {
      byId("saveMobileNoteBtn").addEventListener("click", handleSaveMobileNote);
    }
    if (byId("refreshPendingAtomsBtn")) {
      byId("refreshPendingAtomsBtn").addEventListener("click", renderPendingAtomsReview);
    }
    if (byId("dropMediaCaptureInput")) {
      byId("dropMediaCaptureInput").addEventListener("change", async function (ev) {
        const files = ev && ev.target && ev.target.files ? ev.target.files : [];
        if (files.length === 0) {
          pendingMediaAttachment = null;
          return;
        }
        const f = files[0];
        const MAX_MEDIA_RAW_BYTES = 131072;
        if (Number(f.size || 0) > MAX_MEDIA_RAW_BYTES) {
          pendingMediaAttachment = null;
          if (ev.target && "value" in ev.target) ev.target.value = "";
          showBanner(
            "Attachment exceeds 128 KiB raw payload ceiling for 256 KiB SBID1 drops (" + f.size + " bytes).",
            true
          );
          return;
        }
        try {
          let bytes = null;
          if (typeof f.arrayBuffer === "function") {
            bytes = new Uint8Array(await f.arrayBuffer());
          } else {
            bytes = VaultCrypto.utf8Bytes(String(f.name || ""));
          }
          pendingMediaAttachment = {
            filename: String(f.name || "capture.bin"),
            mime_type: String(f.type || "application/octet-stream"),
            size_bytes: bytes.length,
            sha256: VaultCrypto.sha256HexSync(bytes),
            data_b64: VaultCrypto.bytesToBase64(bytes),
          };
          const curTitle = byId("dropTitleInput").value.trim();
          if (!curTitle) byId("dropTitleInput").value = "Mobile Capture: " + f.name;
          const prevText = byId("dropTextInput").value.trim();
          byId("dropTextInput").value =
            (prevText ? prevText + "\n\n" : "") +
            "[Captured attachment queued: " + f.name + " (" + (f.type || "application/octet-stream") + ", " + bytes.length + " bytes)]";
        } catch (err) {
          pendingMediaAttachment = null;
          showBanner("Failed to read media capture: " + err.message, true);
        }
      });
    }
    byId("importSbvaultBtn").addEventListener("click", handleSbvaultImport);

    byId("applyVaultPairBtn").addEventListener("click", async function () {
      try {
        const raw = byId("vaultPairInput").value.trim();
        const parsed = VaultCrypto.parseVaultPairingFragment(raw);
        await applyNewVaultPairing(parsed);
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

    byId("saveDropPairBtn").addEventListener("click", async function () {
      try {
        const raw = byId("dropPairInput").value.trim();
        const parsedDrop = VaultCrypto.parseDropPairingFragment(raw);
        await applyNewDropPairing(parsedDrop);
        byId("dropPairInput").value = "";
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

    const flushBtn = byId("flushOutboxBtn");
    if (flushBtn) {
      flushBtn.addEventListener("click", function () {
        flushSbidOutbox(false);
      });
    }
    const clearOutboxBtn = byId("clearOutboxBtn");
    if (clearOutboxBtn) {
      clearOutboxBtn.addEventListener("click", function () {
        saveSbidOutbox([]);
        showBanner("Cleared offline SBID1 outbox queue.", false);
      });
    }

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
  refreshOutboxUi();
  refreshLockUi();
  registerServiceWorker();

  globalThis.SecondBrainMobileVaultApp = {
    deviceStore: deviceStore,
    unlockOrInitializeWithPin: unlockOrInitializeWithPin,
    persistCurrentInputsAndLock: persistCurrentInputsAndLock,
    applyNewVaultPairing: applyNewVaultPairing,
    importHashPairingOnBoot: importHashPairingOnBoot,
    renderSyncedNotesList: renderSyncedNotesList,
    handlePullSync: handlePullSync,
    handlePushSync: handlePushSync,
    handleSaveMobileNote: handleSaveMobileNote,
    renderPendingAtomsReview: renderPendingAtomsReview,
    handleSendDrop: handleSendDrop,
    loadSbidOutbox: loadSbidOutbox,
    flushSbidOutbox: flushSbidOutbox,
    refreshLockUi: refreshLockUi,
  };
})();
