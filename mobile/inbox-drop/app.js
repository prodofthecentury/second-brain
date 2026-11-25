(function () {
  "use strict";

  const STORAGE_PAIRING_KEY = "sb_inbox_drop_pairing_v1";
  const STORAGE_OUTBOX_KEY = "sb_inbox_drop_outbox_v1";
  const STORAGE_RECALL_CIPHER_KEY = "sb_mobile_recall_cipher_v1";
  const IDB_NAME = "sb_inbox_drop_keystore_v1";
  const IDB_STORE = "keys";
  const IDB_WRAP_KEY_ID = "device_pairing_wrap_v1";
  const SECRET_PAIRING_FIELDS = [
    "recall_key_b64",
    "drop_secret_b64",
    "hmac_secret_b64",
    "s3_secret_access_key",
  ];

  const elBadge = document.getElementById("pairingBadge");
  const elBanner = document.getElementById("statusBanner");
  const elTabCaptureBtn = document.getElementById("tabCaptureBtn");
  const elTabRecallBtn = document.getElementById("tabRecallBtn");
  const elPanelCapture = document.getElementById("panelCapture");
  const elPanelRecall = document.getElementById("panelRecall");

  const elTitle = document.getElementById("dropTitle");
  const elUrl = document.getElementById("dropUrl");
  const elText = document.getElementById("dropText");
  const elTags = document.getElementById("dropTags");
  const elSendBtn = document.getElementById("sendDropBtn");
  const elDownloadBtn = document.getElementById("downloadEncBtn");
  const elOutboxCard = document.getElementById("outboxCard");
  const elOutboxCount = document.getElementById("outboxCount");
  const elFlushBtn = document.getElementById("flushOutboxBtn");

  const elRecallCapBadge = document.getElementById("recallCapabilityBadge");
  const elSyncRecallBtn = document.getElementById("syncRecallBtn");
  const elImportRecallInput = document.getElementById("importRecallInput");
  const elRecallQuery = document.getElementById("recallQueryInput");
  const elRecallProj = document.getElementById("recallProjectSelect");
  const elViewVerifiedBtn = document.getElementById("viewVerifiedBtn");
  const elViewReviewBtn = document.getElementById("viewReviewBtn");
  const elVerifiedCount = document.getElementById("verifiedCount");
  const elReviewCount = document.getElementById("reviewCount");
  const elRecallMeta = document.getElementById("recallMetaLine");
  const elRecallResults = document.getElementById("recallResultsList");

  const elPairingDetails = document.getElementById("pairingDetails");
  const elPairingSummary = document.getElementById("pairingSummary");
  const elPairInput = document.getElementById("pairInput");
  const elTargetInput = document.getElementById("s3TargetInput");
  const elRegionInput = document.getElementById("s3RegionInput");
  const elAkInput = document.getElementById("s3AccessKeyInput");
  const elSkInput = document.getElementById("s3SecretKeyInput");
  const elSavePairingBtn = document.getElementById("savePairingBtn");
  const elClearPairingBtn = document.getElementById("clearPairingBtn");

  let activePairing = null;
  let decryptedCatalog = null;
  let activeRecallView = "verified";

  function showStatus(msg, isError) {
    elBanner.textContent = String(msg || "");
    elBanner.classList.remove("hidden", "banner-error", "banner-ok");
    elBanner.classList.add(isError ? "banner-error" : "banner-ok");
  }

  function switchTab(tabName) {
    const isRecall = tabName === "recall";
    elTabCaptureBtn.setAttribute("aria-selected", isRecall ? "false" : "true");
    elTabRecallBtn.setAttribute("aria-selected", isRecall ? "true" : "false");
    elTabCaptureBtn.classList.toggle("tab-active", !isRecall);
    elTabRecallBtn.classList.toggle("tab-active", isRecall);
    elPanelCapture.classList.toggle("hidden", isRecall);
    elPanelRecall.classList.toggle("hidden", !isRecall);
  }

  function openKeystoreDb() {
    return new Promise(function (resolve, reject) {
      if (typeof indexedDB === "undefined" || !indexedDB || typeof indexedDB.open !== "function") {
        reject(new Error("IndexedDB unavailable"));
        return;
      }
      const req = indexedDB.open(IDB_NAME, 1);
      req.onupgradeneeded = function () {
        const db = req.result;
        if (!db.objectStoreNames.contains(IDB_STORE)) {
          db.createObjectStore(IDB_STORE);
        }
      };
      req.onsuccess = function () {
        resolve(req.result);
      };
      req.onerror = function () {
        reject(req.error || new Error("IndexedDB open failed"));
      };
    });
  }

  async function getOrCreateDeviceWrapKey(createIfMissing) {
    const c = typeof globalThis !== "undefined" ? globalThis.crypto : null;
    if (!c || !c.subtle) {
      return null;
    }
    let db = null;
    try {
      db = await openKeystoreDb();
      const existing = await new Promise(function (resolve, reject) {
        const tx = db.transaction(IDB_STORE, "readonly");
        const store = tx.objectStore(IDB_STORE);
        const getReq = store.get(IDB_WRAP_KEY_ID);
        getReq.onsuccess = function () {
          resolve(getReq.result || null);
        };
        getReq.onerror = function () {
          reject(getReq.error || new Error("Keystore read failed"));
        };
      });
      if (existing) {
        return existing;
      }
      if (!createIfMissing) {
        return null;
      }
      // Non-extractable AES-256-GCM wrapping key: JS can use it to seal/unseal but can never export raw key bytes.
      const wrapKey = await c.subtle.generateKey(
        { name: "AES-GCM", length: 256 },
        false,
        ["encrypt", "decrypt"]
      );
      await new Promise(function (resolve, reject) {
        const tx = db.transaction(IDB_STORE, "readwrite");
        tx.objectStore(IDB_STORE).put(wrapKey, IDB_WRAP_KEY_ID);
        tx.oncomplete = function () {
          resolve();
        };
        tx.onerror = function () {
          reject(tx.error || new Error("Keystore write failed"));
        };
      });
      return wrapKey;
    } catch (_idbErr) {
      return null;
    } finally {
      if (db && typeof db.close === "function") {
        try {
          db.close();
        } catch (_closeErr) {
          // Ignore close errors
        }
      }
    }
  }

  async function deleteDeviceWrapKey() {
    let db = null;
    try {
      db = await openKeystoreDb();
      await new Promise(function (resolve, reject) {
        const tx = db.transaction(IDB_STORE, "readwrite");
        tx.objectStore(IDB_STORE).delete(IDB_WRAP_KEY_ID);
        tx.oncomplete = function () {
          resolve();
        };
        tx.onerror = function () {
          reject(tx.error || new Error("Keystore delete failed"));
        };
      });
    } catch (_err) {
      // Ignore if IndexedDB unavailable
    } finally {
      if (db && typeof db.close === "function") {
        try {
          db.close();
        } catch (_closeErr) {
          // Ignore
        }
      }
    }
  }

  function pairingSecretsAad(bundle) {
    const vid = String((bundle && bundle.vault_id) || "");
    const tid = String((bundle && bundle.token_id) || "");
    return new TextEncoder().encode("SBDP-SECRETS-V1:" + vid + ":" + tid);
  }

  function splitPairingFields(bundle) {
    const publicPart = {};
    const secretPart = {};
    const keys = Object.keys(bundle || {});
    for (let i = 0; i < keys.length; i += 1) {
      const k = keys[i];
      if (k === "sealed_secrets_v1" || k === "secrets_sealed" || k === "secrets_ephemeral_ram_only") {
        continue;
      }
      if (SECRET_PAIRING_FIELDS.indexOf(k) >= 0) {
        if (bundle[k]) {
          secretPart[k] = bundle[k];
        }
      } else {
        publicPart[k] = bundle[k];
      }
    }
    return { publicPart: publicPart, secretPart: secretPart };
  }

  function loadPairing() {
    return activePairing;
  }

  async function savePairing(bundle) {
    activePairing = bundle ? Object.assign({}, bundle) : null;
    if (!bundle) {
      localStorage.removeItem(STORAGE_PAIRING_KEY);
      refreshPairingUi();
      return;
    }
    const split = splitPairingFields(bundle);
    const toStore = Object.assign({}, split.publicPart);
    const hasSecrets = Object.keys(split.secretPart).length > 0;
    if (hasSecrets) {
      const wrapKey = await getOrCreateDeviceWrapKey(true);
      const c = typeof globalThis !== "undefined" ? globalThis.crypto : null;
      if (wrapKey && c && c.subtle) {
        const iv = new Uint8Array(12);
        c.getRandomValues(iv);
        const plainBytes = new TextEncoder().encode(JSON.stringify(split.secretPart));
        const ctBuf = await c.subtle.encrypt(
          { name: "AES-GCM", iv: iv, additionalData: pairingSecretsAad(bundle) },
          wrapKey,
          plainBytes
        );
        toStore.secrets_sealed = true;
        toStore.sealed_secrets_v1 = {
          iv_b64: bytesToBase64Local(iv),
          ct_b64: bytesToBase64Local(new Uint8Array(ctBuf)),
        };
      } else {
        // Never fall back to plaintext localStorage for decryption/S3 secrets: keep secrets in RAM only.
        toStore.secrets_sealed = false;
        toStore.secrets_ephemeral_ram_only = true;
      }
    }
    // Defense-in-depth scrub guaranteeing zero raw secret keys ever reach localStorage
    for (let i = 0; i < SECRET_PAIRING_FIELDS.length; i += 1) {
      delete toStore[SECRET_PAIRING_FIELDS[i]];
    }
    localStorage.setItem(STORAGE_PAIRING_KEY, JSON.stringify(toStore));
    refreshPairingUi();
  }

  async function hydratePairingFromStorage() {
    let stored = null;
    try {
      const raw = localStorage.getItem(STORAGE_PAIRING_KEY);
      stored = raw ? JSON.parse(raw) : null;
    } catch (_err) {
      stored = null;
    }
    if (!stored || typeof stored !== "object") {
      activePairing = null;
      refreshPairingUi();
      return null;
    }

    // Migrate and scrub any legacy plaintext secret fields previously written to localStorage
    const hasLegacyPlaintext = SECRET_PAIRING_FIELDS.some(function (f) {
      return Boolean(stored[f]);
    });
    if (hasLegacyPlaintext) {
      await savePairing(stored);
      return activePairing;
    }

    const hydrated = Object.assign({}, stored);
    delete hydrated.sealed_secrets_v1;
    delete hydrated.secrets_sealed;
    delete hydrated.secrets_ephemeral_ram_only;

    if (stored.sealed_secrets_v1 && stored.sealed_secrets_v1.iv_b64 && stored.sealed_secrets_v1.ct_b64) {
      const wrapKey = await getOrCreateDeviceWrapKey(false);
      const c = typeof globalThis !== "undefined" ? globalThis.crypto : null;
      if (wrapKey && c && c.subtle) {
        try {
          const iv = base64ToBytesLocal(stored.sealed_secrets_v1.iv_b64);
          const ct = base64ToBytesLocal(stored.sealed_secrets_v1.ct_b64);
          const plainBuf = await c.subtle.decrypt(
            { name: "AES-GCM", iv: iv, additionalData: pairingSecretsAad(stored) },
            wrapKey,
            ct
          );
          const decoded = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(new Uint8Array(plainBuf)));
          if (decoded && typeof decoded === "object") {
            for (let i = 0; i < SECRET_PAIRING_FIELDS.length; i += 1) {
              const fk = SECRET_PAIRING_FIELDS[i];
              if (typeof decoded[fk] === "string" && decoded[fk]) {
                hydrated[fk] = decoded[fk];
              }
            }
          }
        } catch (_unsealErr) {
          showStatus("Device wrapping key unavailable or changed — please re-import your #pair= URI.", true);
        }
      }
    }
    activePairing = hydrated;
    refreshPairingUi();
    return activePairing;
  }

  function loadOutbox() {
    try {
      const raw = localStorage.getItem(STORAGE_OUTBOX_KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch (_err) {
      return [];
    }
  }

  function saveOutbox(items) {
    localStorage.setItem(STORAGE_OUTBOX_KEY, JSON.stringify(items.slice(-25)));
    refreshOutboxUi();
  }

  function refreshOutboxUi() {
    const items = loadOutbox();
    elOutboxCount.textContent = String(items.length);
    if (items.length > 0) {
      elOutboxCard.classList.remove("hidden");
    } else {
      elOutboxCard.classList.add("hidden");
    }
  }

  function refreshPairingUi() {
    const p = loadPairing();
    if (p && p.vault_id && p.recipient_pubkey_b64) {
      const hasRecall = Boolean(p.recall_key_b64 && p.recall_key_id);
      elBadge.textContent =
        (hasRecall ? "Drop + Recall (" : "Paired (") + String(p.key_id || "").slice(0, 8) + ")";
      elBadge.className = "badge badge-ok";
      elRecallCapBadge.textContent = hasRecall
        ? "E2EE Recall (" + String(p.recall_key_id).slice(0, 8) + ")"
        : "Drop-Only Key";
      elRecallCapBadge.className = hasRecall ? "badge badge-ok" : "badge badge-warn";
      elPairingSummary.textContent =
        "Paired to vault " +
        String(p.vault_id).slice(0, 12) +
        "… (token " +
        String(p.token_id).slice(0, 8) +
        "…, recall=" +
        (hasRecall ? "enabled" : "off") +
        ").";
      if (p.target_url) {
        elTargetInput.value = p.target_url;
      }
      if (p.s3_region) {
        elRegionInput.value = p.s3_region;
      }
      if (p.s3_access_key_id) {
        elAkInput.value = p.s3_access_key_id;
      }
    } else {
      elBadge.textContent = "Unpaired";
      elBadge.className = "badge badge-warn";
      elRecallCapBadge.textContent = "Unpaired";
      elRecallCapBadge.className = "badge badge-warn";
      elPairingDetails.open = true;
    }
  }

  function mergePreservedS3Fields(nextBundle, prevBundle) {
    if (!prevBundle) {
      return nextBundle;
    }
    if (!nextBundle.target_url && prevBundle.target_url) {
      nextBundle.target_url = prevBundle.target_url;
    }
    if ((!nextBundle.s3_region || nextBundle.s3_region === "auto") && prevBundle.s3_region) {
      nextBundle.s3_region = prevBundle.s3_region;
    }
    if (!nextBundle.s3_access_key_id && prevBundle.s3_access_key_id) {
      nextBundle.s3_access_key_id = prevBundle.s3_access_key_id;
    }
    if (
      !nextBundle.s3_secret_access_key &&
      prevBundle.s3_secret_access_key &&
      (!nextBundle.s3_access_key_id || nextBundle.s3_access_key_id === prevBundle.s3_access_key_id)
    ) {
      nextBundle.s3_secret_access_key = prevBundle.s3_secret_access_key;
    }
    return nextBundle;
  }

  async function importHashIfPresent() {
    if (!window.location.hash || window.location.hash.indexOf("pair=") === -1) {
      return;
    }
    const rawHash = window.location.hash;
    try {
      const prev = loadPairing();
      const parsed = mergePreservedS3Fields(
        window.SecondBrainDropCrypto.parsePairingFragment(rawHash),
        prev
      );
      await savePairing(parsed);
      showStatus("Paired zero-knowledge device keys and scrubbed URL hash.", false);
    } catch (err) {
      showStatus("Pairing import failed: " + err.message, true);
    } finally {
      try {
        history.replaceState(null, "", window.location.pathname + window.location.search);
      } catch (_histErr) {
        window.location.hash = "";
      }
    }
  }

  function populateShareTargetParams() {
    if (!window.location.search) {
      return;
    }
    const params = new URLSearchParams(window.location.search);
    let consumed = false;
    if (params.get("tab") === "recall") {
      switchTab("recall");
      if (params.get("q")) {
        elRecallQuery.value = params.get("q");
      }
      consumed = true;
    }
    if (params.get("title")) {
      elTitle.value = params.get("title");
      consumed = true;
    }
    if (params.get("url")) {
      elUrl.value = params.get("url");
      consumed = true;
    }
    if (params.get("text")) {
      const sharedText = params.get("text");
      if (!elUrl.value && /^https?:\/\/\S+$/i.test(sharedText.trim())) {
        elUrl.value = sharedText.trim();
      } else {
        elText.value = sharedText;
      }
      consumed = true;
    }
    if (consumed) {
      try {
        history.replaceState(null, "", window.location.pathname + window.location.hash);
      } catch (_histErr) {
        // Ignore non-SameOrigin history errors in local file:// contexts.
      }
    }
  }

  async function buildEncryptedDrop() {
    const pairing = loadPairing();
    if (!pairing) {
      throw new Error("Pair this device first under 'Device Pairing & S3/R2 Target'.");
    }
    const title = elTitle.value.trim();
    const url = elUrl.value.trim();
    const text = elText.value.trim();
    if (!title && !url && !text) {
      throw new Error("Enter a URL, title, or note text to capture.");
    }
    const tags = elTags.value
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const enc = await window.SecondBrainDropCrypto.encryptInboxDropEnvelope(
      { title: title, url: url, text: text, tags: tags, client: "mobile-pwa" },
      pairing
    );
    return { enc: enc, pairing: pairing };
  }

  async function uploadEncryptedEnvelope(objectKey, envelopeBytes, pairing) {
    const targetBase = String(pairing.target_url || elTargetInput.value || "").trim().replace(/\/+$/, "");
    const ak = String(pairing.s3_access_key_id || elAkInput.value || "").trim();
    const sk = String(pairing.s3_secret_access_key || elSkInput.value || "").trim();
    const region = String(pairing.s3_region || elRegionInput.value || "auto").trim() || "auto";
    if (!targetBase || !ak || !sk) {
      throw new Error("Configure S3/R2 endpoint and scoped credentials (or use Save .enc File).");
    }
    const putUrl = targetBase + "/" + objectKey;
    const signed = await window.SecondBrainDropCrypto.signS3PutRequest({
      method: "PUT",
      url: putUrl,
      payloadBytes: envelopeBytes,
      accessKeyId: ak,
      secretAccessKey: sk,
      region: region,
    });
    const resp = await fetch(signed.url, {
      method: "PUT",
      headers: signed.headers,
      body: envelopeBytes,
      mode: "cors",
      credentials: "omit",
    });
    if (!resp.ok) {
      throw new Error("S3/R2 PUT failed with HTTP " + resp.status);
    }
  }

  function bytesToBase64Local(u8) {
    let bin = "";
    for (let i = 0; i < u8.length; i += 1) {
      bin += String.fromCharCode(u8[i]);
    }
    return btoa(bin);
  }

  function base64ToBytesLocal(b64) {
    const bin = atob(b64);
    const u8 = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i += 1) {
      u8[i] = bin.charCodeAt(i);
    }
    return u8;
  }

  function refreshProjectOptions(catalog) {
    const current = elRecallProj.value;
    const scopes = new Set();
    const allItems = []
      .concat(Array.isArray(catalog.atoms) ? catalog.atoms : [])
      .concat(Array.isArray(catalog.reviewQueue) ? catalog.reviewQueue : []);
    for (const item of allItems) {
      for (const ctx of Array.isArray(item.contexts) ? item.contexts : []) {
        const clean = String(ctx || "").trim().toLowerCase();
        if (clean.startsWith("project:")) {
          scopes.add(clean);
        }
      }
    }
    while (elRecallProj.options.length > 1) {
      elRecallProj.remove(1);
    }
    for (const sc of Array.from(scopes).sort()) {
      const opt = document.createElement("option");
      opt.value = sc;
      opt.textContent = sc;
      elRecallProj.appendChild(opt);
    }
    if (current && scopes.has(current)) {
      elRecallProj.value = current;
    }
  }

  function prepareReviewNoteDrop(item) {
    const fname = String(item.filename || item.id || "atom.md");
    const revPrefix = String(item.sha256 || "").slice(0, 12);
    const isPending = !item.verified;
    elTitle.value = (isPending ? "Review candidate: " : "Correction / note on: ") + fname;
    elUrl.value = "";
    elTags.value = "mobile-review, atom:" + String(item.id || fname.replace(/\.md$/, ""));
    elText.value =
      "Target Atom: " + fname + (revPrefix ? " (rev " + revPrefix + ")" : "") + "\n" +
      "Current Claim: " + String(item.claim || "") + "\n\n" +
      (isPending ? "Owner Review Note (confirm / edit / reject): " : "Follow-up / Correction Note: ");
    switchTab("capture");
    elText.focus();
    showStatus("Prepared encrypted review note for " + fname + " in Drop Note tab.", false);
  }

  function renderRecallResults() {
    while (elRecallResults.firstChild) {
      elRecallResults.removeChild(elRecallResults.firstChild);
    }
    if (!decryptedCatalog) {
      elVerifiedCount.textContent = "0";
      elReviewCount.textContent = "0";
      return;
    }
    elVerifiedCount.textContent = String(decryptedCatalog.atomCount || 0);
    elReviewCount.textContent = String(decryptedCatalog.reviewCount || 0);

    const results = window.SecondBrainDropCrypto.searchMobileRecallCatalog(
      decryptedCatalog,
      elRecallQuery.value,
      {
        view: activeRecallView,
        projectScope: elRecallProj.value,
        limit: 50,
      }
    );
    elRecallMeta.textContent =
      "Snapshot " +
      String(decryptedCatalog.exportedAt || "") +
      " • Showing " +
      results.length +
      " " +
      (activeRecallView === "review" ? "pending candidate(s)" : "verified atom(s)") +
      ".";

    if (results.length === 0) {
      const empty = document.createElement("p");
      empty.className = "hint";
      empty.textContent =
        activeRecallView === "review"
          ? "No matching pending review items."
          : "No matching owner-verified atoms found.";
      elRecallResults.appendChild(empty);
      return;
    }

    for (const item of results) {
      const article = document.createElement("article");
      article.className = "atom-card";

      const topRow = document.createElement("div");
      topRow.className = "row-between atom-card-head";

      const fnameSpan = document.createElement("span");
      fnameSpan.className = "atom-filename";
      fnameSpan.textContent = String(item.filename || item.id || "");
      topRow.appendChild(fnameSpan);

      const statusBadge = document.createElement("span");
      statusBadge.className = item.verified ? "badge badge-ok" : "badge badge-warn";
      statusBadge.textContent = item.verified ? "Verified" : "Unverified (" + String(item.review_status || "pending") + ")";
      topRow.appendChild(statusBadge);
      article.appendChild(topRow);

      const claimP = document.createElement("p");
      claimP.className = "atom-claim";
      claimP.textContent = String(item.claim || "");
      article.appendChild(claimP);

      const chipList = []
        .concat(Array.isArray(item.contexts) ? item.contexts : [])
        .concat(Array.isArray(item.tags) ? item.tags : []);
      if (chipList.length > 0) {
        const chipsDiv = document.createElement("div");
        chipsDiv.className = "chip-row";
        for (const tag of chipList.slice(0, 8)) {
          const chip = document.createElement("span");
          chip.className = "chip";
          chip.textContent = String(tag);
          chipsDiv.appendChild(chip);
        }
        article.appendChild(chipsDiv);
      }

      const details = document.createElement("details");
      details.className = "atom-details";
      const summary = document.createElement("summary");
      summary.textContent = "Evidence & Provenance";
      details.appendChild(summary);

      const evQuote = document.createElement("blockquote");
      evQuote.className = "atom-evidence";
      evQuote.textContent = String(item.evidence || "(no excerpt)");
      details.appendChild(evQuote);

      if (item.evidence_source) {
        const srcLine = document.createElement("p");
        srcLine.className = "atom-meta";
        srcLine.textContent = "Source: " + String(item.evidence_source);
        details.appendChild(srcLine);
      }
      if (Array.isArray(item.links) && item.links.length > 0) {
        const linkLine = document.createElement("p");
        linkLine.className = "atom-meta";
        linkLine.textContent = "Linked: " + item.links.join(", ");
        details.appendChild(linkLine);
      }

      const reviewBtn = document.createElement("button");
      reviewBtn.type = "button";
      reviewBtn.className = "btn btn-small review-drop-btn";
      reviewBtn.textContent = item.verified ? "Drop Correction / Follow-up Note" : "Drop Review Decision Note";
      reviewBtn.addEventListener("click", function () {
        prepareReviewNoteDrop(item);
      });
      details.appendChild(reviewBtn);

      article.appendChild(details);
      elRecallResults.appendChild(article);
    }
  }

  async function ingestEncryptedRecallBytes(envelopeBytes, persistCipher) {
    const pairing = loadPairing();
    if (!pairing) {
      throw new Error("Pair this device first under 'Device Pairing & S3/R2 Target'.");
    }
    const catalog = await window.SecondBrainDropCrypto.decryptMobileRecallEnvelope(envelopeBytes, pairing);
    decryptedCatalog = catalog;
    if (persistCipher) {
      try {
        localStorage.setItem(STORAGE_RECALL_CIPHER_KEY, bytesToBase64Local(envelopeBytes));
      } catch (_storageErr) {
        // Ignore quota limits on very large vaults; in-memory catalog stays active.
      }
    }
    refreshProjectOptions(catalog);
    renderRecallResults();
    return catalog;
  }

  async function restoreCachedRecallIfAvailable() {
    const pairing = loadPairing();
    if (!pairing || !pairing.recall_key_b64) {
      return;
    }
    const cachedB64 = localStorage.getItem(STORAGE_RECALL_CIPHER_KEY);
    if (!cachedB64) {
      return;
    }
    try {
      await ingestEncryptedRecallBytes(base64ToBytesLocal(cachedB64), false);
    } catch (_err) {
      localStorage.removeItem(STORAGE_RECALL_CIPHER_KEY);
    }
  }

  elTabCaptureBtn.addEventListener("click", function () {
    switchTab("capture");
  });
  elTabRecallBtn.addEventListener("click", function () {
    switchTab("recall");
  });

  elViewVerifiedBtn.addEventListener("click", function () {
    activeRecallView = "verified";
    elViewVerifiedBtn.classList.add("seg-active");
    elViewReviewBtn.classList.remove("seg-active");
    renderRecallResults();
  });

  elViewReviewBtn.addEventListener("click", function () {
    activeRecallView = "review";
    elViewReviewBtn.classList.add("seg-active");
    elViewVerifiedBtn.classList.remove("seg-active");
    renderRecallResults();
  });

  elRecallQuery.addEventListener("input", renderRecallResults);
  elRecallProj.addEventListener("change", renderRecallResults);

  elSyncRecallBtn.addEventListener("click", async function () {
    elSyncRecallBtn.disabled = true;
    try {
      const pairing = loadPairing();
      if (!pairing) {
        throw new Error("Pair this device first.");
      }
      if (!pairing.recall_key_b64 || !pairing.recall_key_id) {
        throw new Error("Re-pair with `second-brain sync drop-pair --include-recall` to enable E2EE Recall.");
      }
      const targetBase = String(pairing.target_url || elTargetInput.value || "").trim().replace(/\/+$/, "");
      const ak = String(pairing.s3_access_key_id || elAkInput.value || "").trim();
      const sk = String(pairing.s3_secret_access_key || elSkInput.value || "").trim();
      const region = String(pairing.s3_region || elRegionInput.value || "auto").trim() || "auto";
      if (!targetBase || !ak || !sk) {
        throw new Error("Configure S3/R2 endpoint & credentials, or tap 'Load .enc File' to import offline.");
      }
      const objKey = String(pairing.recall_object_key || ("mobile-recall/" + pairing.vault_id + ".enc"));
      const signed = await window.SecondBrainDropCrypto.signS3GetRequest({
        url: targetBase + "/" + objKey,
        accessKeyId: ak,
        secretAccessKey: sk,
        region: region,
      });
      const resp = await fetch(signed.url, {
        method: "GET",
        headers: signed.headers,
        mode: "cors",
        credentials: "omit",
      });
      if (!resp.ok) {
        throw new Error("S3/R2 GET " + objKey + " failed with HTTP " + resp.status);
      }
      const buf = new Uint8Array(await resp.arrayBuffer());
      const cat = await ingestEncryptedRecallBytes(buf, true);
      showStatus(
        "Synced & decrypted " + cat.atomCount + " verified atom(s) + " + cat.reviewCount + " review item(s).",
        false
      );
    } catch (err) {
      showStatus(err.message, true);
    } finally {
      elSyncRecallBtn.disabled = false;
    }
  });

  elImportRecallInput.addEventListener("change", async function (ev) {
    const file = ev.target && ev.target.files ? ev.target.files[0] : null;
    if (!file) {
      return;
    }
    try {
      const buf = new Uint8Array(await file.arrayBuffer());
      const cat = await ingestEncryptedRecallBytes(buf, true);
      showStatus(
        "Loaded & decrypted " + cat.atomCount + " verified atom(s) + " + cat.reviewCount + " review item(s).",
        false
      );
    } catch (err) {
      showStatus("Recall decrypt failed: " + err.message, true);
    } finally {
      elImportRecallInput.value = "";
    }
  });

  elSendBtn.addEventListener("click", async function () {
    elSendBtn.disabled = true;
    try {
      const built = await buildEncryptedDrop();
      try {
        await uploadEncryptedEnvelope(built.enc.objectKey, built.enc.envelopeBytes, built.pairing);
        elTitle.value = "";
        elUrl.value = "";
        elText.value = "";
        showStatus("Encrypted & dropped " + built.enc.objectKey + " to S3/R2!", false);
      } catch (netErr) {
        const q = loadOutbox();
        q.push({
          objectKey: built.enc.objectKey,
          envelopeB64: bytesToBase64Local(built.enc.envelopeBytes),
          queuedAt: built.enc.createdAt,
        });
        saveOutbox(q);
        showStatus("Offline/upload error (" + netErr.message + ") — saved encrypted envelope in Outbox.", true);
      }
    } catch (err) {
      showStatus(err.message, true);
    } finally {
      elSendBtn.disabled = false;
    }
  });

  elDownloadBtn.addEventListener("click", async function () {
    try {
      const built = await buildEncryptedDrop();
      const blob = new Blob([built.enc.envelopeBytes], { type: "application/octet-stream" });
      const dlUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = dlUrl;
      link.download = built.enc.dropId + ".enc";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(dlUrl);
      showStatus("Saved encrypted " + built.enc.dropId + ".enc (place inside sync inbox-drops/ folder).", false);
    } catch (err) {
      showStatus(err.message, true);
    } finally {
      elDownloadBtn.disabled = false;
    }
  });

  elFlushBtn.addEventListener("click", async function () {
    const pairing = loadPairing();
    if (!pairing) {
      showStatus("Pair device first.", true);
      return;
    }
    const items = loadOutbox();
    const remaining = [];
    let sent = 0;
    for (const item of items) {
      try {
        await uploadEncryptedEnvelope(item.objectKey, base64ToBytesLocal(item.envelopeB64), pairing);
        sent += 1;
      } catch (_err) {
        remaining.push(item);
      }
    }
    saveOutbox(remaining);
    showStatus("Flushed " + sent + " queued drop(s); " + remaining.length + " remaining.", remaining.length > 0);
  });

  elSavePairingBtn.addEventListener("click", async function () {
    try {
      const prev = loadPairing();
      const rawPair = elPairInput.value.trim();
      const base = rawPair
        ? window.SecondBrainDropCrypto.parsePairingFragment(rawPair)
        : (prev ? Object.assign({}, prev) : null);
      if (!base) {
        throw new Error("Paste a valid #pair= URI first.");
      }
      if (elTargetInput.value.trim()) {
        base.target_url = elTargetInput.value.trim().replace(/\/+$/, "");
      }
      if (elRegionInput.value.trim()) {
        base.s3_region = elRegionInput.value.trim();
      }
      if (elAkInput.value.trim()) {
        const nextAk = elAkInput.value.trim();
        if (prev && prev.s3_access_key_id && nextAk !== prev.s3_access_key_id && !elSkInput.value.trim()) {
          delete base.s3_secret_access_key;
        }
        base.s3_access_key_id = nextAk;
      }
      if (elSkInput.value.trim()) {
        base.s3_secret_access_key = elSkInput.value.trim();
      }
      mergePreservedS3Fields(base, prev);
      await savePairing(base);
      elPairInput.value = "";
      elSkInput.value = "";
      showStatus("Saved zero-knowledge pairing configuration (secrets sealed outside plaintext localStorage).", false);
      await restoreCachedRecallIfAvailable();
    } catch (err) {
      showStatus(err.message, true);
    }
  });

  elClearPairingBtn.addEventListener("click", async function () {
    activePairing = null;
    decryptedCatalog = null;
    localStorage.removeItem(STORAGE_PAIRING_KEY);
    localStorage.removeItem(STORAGE_RECALL_CIPHER_KEY);
    await deleteDeviceWrapKey();
    refreshPairingUi();
    renderRecallResults();
    showStatus("Cleared device pairing keys, keystore handle, and cached recall ciphertext.", false);
  });

  window.__sbInboxDropAppReady = (async function initApp() {
    await hydratePairingFromStorage();
    await importHashIfPresent();
    populateShareTargetParams();
    refreshPairingUi();
    refreshOutboxUi();
    await restoreCachedRecallIfAvailable();
  })();

  if ("serviceWorker" in navigator && window.location.protocol === "https:") {
    navigator.serviceWorker.register("sw.js").catch(function () {});
  }
})();
