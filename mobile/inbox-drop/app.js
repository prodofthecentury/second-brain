(function () {
  "use strict";

  const STORAGE_PAIRING_KEY = "sb_inbox_drop_pairing_v1";
  const STORAGE_OUTBOX_KEY = "sb_inbox_drop_outbox_v1";

  const elBadge = document.getElementById("pairingBadge");
  const elBanner = document.getElementById("statusBanner");
  const elTitle = document.getElementById("dropTitle");
  const elUrl = document.getElementById("dropUrl");
  const elText = document.getElementById("dropText");
  const elTags = document.getElementById("dropTags");
  const elSendBtn = document.getElementById("sendDropBtn");
  const elDownloadBtn = document.getElementById("downloadEncBtn");
  const elOutboxCard = document.getElementById("outboxCard");
  const elOutboxCount = document.getElementById("outboxCount");
  const elFlushBtn = document.getElementById("flushOutboxBtn");
  const elPairingDetails = document.getElementById("pairingDetails");
  const elPairingSummary = document.getElementById("pairingSummary");
  const elPairInput = document.getElementById("pairInput");
  const elTargetInput = document.getElementById("s3TargetInput");
  const elRegionInput = document.getElementById("s3RegionInput");
  const elAkInput = document.getElementById("s3AccessKeyInput");
  const elSkInput = document.getElementById("s3SecretKeyInput");
  const elSavePairingBtn = document.getElementById("savePairingBtn");
  const elClearPairingBtn = document.getElementById("clearPairingBtn");

  function showStatus(msg, isError) {
    elBanner.textContent = String(msg || "");
    elBanner.classList.remove("hidden", "banner-error", "banner-ok");
    elBanner.classList.add(isError ? "banner-error" : "banner-ok");
  }

  function loadPairing() {
    try {
      const raw = localStorage.getItem(STORAGE_PAIRING_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (_err) {
      return null;
    }
  }

  function savePairing(bundle) {
    localStorage.setItem(STORAGE_PAIRING_KEY, JSON.stringify(bundle));
    refreshPairingUi();
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
      elBadge.textContent = "Paired (" + String(p.key_id || "").slice(0, 8) + ")";
      elBadge.className = "badge badge-ok";
      elPairingSummary.textContent =
        "Paired to vault " +
        String(p.vault_id).slice(0, 12) +
        "… (token " +
        String(p.token_id).slice(0, 8) +
        "…).";
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

  function importHashIfPresent() {
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
      savePairing(parsed);
      showStatus("Paired zero-knowledge P-256 drop key and scrubbed URL hash.", false);
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
      throw new Error("Configure S3/R2 endpoint and write-scoped credentials (or use Save .enc File).");
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

  elSavePairingBtn.addEventListener("click", function () {
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
      savePairing(base);
      elPairInput.value = "";
      elSkInput.value = "";
      showStatus("Saved zero-knowledge pairing configuration.", false);
    } catch (err) {
      showStatus(err.message, true);
    }
  });

  elClearPairingBtn.addEventListener("click", function () {
    localStorage.removeItem(STORAGE_PAIRING_KEY);
    refreshPairingUi();
    showStatus("Cleared device pairing keys from browser storage.", false);
  });

  importHashIfPresent();
  populateShareTargetParams();
  refreshPairingUi();
  refreshOutboxUi();

  if ("serviceWorker" in navigator && window.location.protocol === "https:") {
    navigator.serviceWorker.register("sw.js").catch(function () {});
  }
})();
