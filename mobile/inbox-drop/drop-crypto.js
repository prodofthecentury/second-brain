(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.SecondBrainDropCrypto = factory();
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const SBID_MAGIC = new Uint8Array([0x53, 0x42, 0x49, 0x44, 0x31, 0x00]); // "SBID1\0"
  const SBID_VERSION = 1;
  const SBMR_MAGIC = new Uint8Array([0x53, 0x42, 0x4d, 0x52, 0x31, 0x00]); // "SBMR1\0"
  const SBMR_VERSION = 1;
  const HEX32_RE = /^[0-9a-f]{32}$/;
  const HEX16_RE = /^[0-9a-f]{16}$/;
  const S3_REGION_RE = /^[a-zA-Z0-9-]{2,32}$/;
  const QUERY_STOP_WORDS = new Set([
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for", "from",
    "how", "in", "is", "it", "of", "on", "or", "should", "that", "the", "to", "what",
    "when", "where", "which", "who", "why", "with"
  ]);

  function getWebCrypto() {
    const c = typeof globalThis !== "undefined" ? globalThis.crypto : null;
    if (!c || !c.subtle) {
      throw new Error("WebCrypto (crypto.subtle) is required for E2EE Mobile Inbox Drop.");
    }
    return c;
  }

  function utf8Bytes(str) {
    return new TextEncoder().encode(String(str || ""));
  }

  function bytesToHex(bytes) {
    const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    let out = "";
    for (let i = 0; i < u8.length; i += 1) {
      out += u8[i].toString(16).padStart(2, "0");
    }
    return out;
  }

  function hexToBytes(hex) {
    const clean = String(hex || "").trim().toLowerCase();
    if (clean.length % 2 !== 0 || !/^[0-9a-f]*$/.test(clean)) {
      throw new Error("Invalid hex string.");
    }
    const out = new Uint8Array(clean.length / 2);
    for (let i = 0; i < out.length; i += 1) {
      out[i] = parseInt(clean.slice(i * 2, i * 2 + 2), 16);
    }
    return out;
  }

  function bytesToBase64(bytes) {
    const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    if (typeof Buffer !== "undefined") {
      return Buffer.from(u8).toString("base64");
    }
    let bin = "";
    for (let i = 0; i < u8.length; i += 1) {
      bin += String.fromCharCode(u8[i]);
    }
    return btoa(bin);
  }

  function base64ToBytes(b64) {
    const clean = String(b64 || "").trim();
    if (typeof Buffer !== "undefined") {
      return new Uint8Array(Buffer.from(clean, "base64"));
    }
    const bin = atob(clean);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i += 1) {
      out[i] = bin.charCodeAt(i);
    }
    return out;
  }

  function base64UrlToUtf8(b64url) {
    let norm = String(b64url || "").trim().replace(/-/g, "+").replace(/_/g, "/");
    while (norm.length % 4 !== 0) {
      norm += "=";
    }
    const bytes = base64ToBytes(norm);
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  }

  function concatBytes(arrays) {
    let total = 0;
    for (const a of arrays) {
      total += a.length;
    }
    const out = new Uint8Array(total);
    let offset = 0;
    for (const a of arrays) {
      out.set(a, offset);
      offset += a.length;
    }
    return out;
  }

  function canonicalHeaderJson(obj) {
    const keys = Object.keys(obj).sort();
    const parts = [];
    for (const k of keys) {
      parts.push(JSON.stringify(k) + ":" + JSON.stringify(obj[k]));
    }
    return "{" + parts.join(",") + "}";
  }

  async function sha256Hex(dataBytes) {
    const subtle = getWebCrypto().subtle;
    const digest = await subtle.digest("SHA-256", dataBytes);
    return bytesToHex(new Uint8Array(digest));
  }

  async function hmacSha256Bytes(keyBytes, messageBytes) {
    const subtle = getWebCrypto().subtle;
    const cryptoKey = await subtle.importKey(
      "raw",
      keyBytes,
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"]
    );
    const sig = await subtle.sign("HMAC", cryptoKey, messageBytes);
    return new Uint8Array(sig);
  }

  function parsePairingFragment(hashOrUrl) {
    const raw = String(hashOrUrl || "").trim();
    const idx = raw.indexOf("#pair=");
    const token = idx >= 0 ? raw.slice(idx + 6).split("&")[0] : raw.replace(/^#?pair=/, "");
    if (!token) {
      throw new Error("Missing #pair= token.");
    }
    const parsed = JSON.parse(base64UrlToUtf8(token));
    if (!parsed || typeof parsed !== "object" || parsed.v !== SBID_VERSION) {
      throw new Error("Unsupported mobile inbox drop pairing version.");
    }
    if (!HEX32_RE.test(String(parsed.vault_id || ""))) {
      throw new Error("Invalid vault_id in pairing bundle.");
    }
    if (!HEX16_RE.test(String(parsed.key_id || "")) || !HEX16_RE.test(String(parsed.token_id || ""))) {
      throw new Error("Invalid key_id or token_id in pairing bundle.");
    }
    if (!parsed.recipient_pubkey_b64 || !parsed.drop_secret_b64) {
      throw new Error("Incomplete cryptographic keys in pairing bundle.");
    }
    if (parsed.recall_key_b64 || parsed.recall_key_id) {
      if (!HEX16_RE.test(String(parsed.recall_key_id || "")) || !parsed.recall_key_b64) {
        throw new Error("Invalid recall_key_id or recall_key_b64 in pairing bundle.");
      }
    }
    return parsed;
  }

  async function encryptInboxDropEnvelope(payload, pairing, opts) {
    const webCrypto = getWebCrypto();
    const subtle = webCrypto.subtle;
    const options = opts || {};

    const vaultId = String(pairing.vault_id || "");
    const keyId = String(pairing.key_id || "");
    const tokenId = String(pairing.token_id || "");
    if (!HEX32_RE.test(vaultId)) {
      throw new Error("Invalid vault_id.");
    }
    if (!HEX16_RE.test(keyId) || !HEX16_RE.test(tokenId)) {
      throw new Error("Invalid key_id or token_id.");
    }

    const recipientPubBytes = base64ToBytes(pairing.recipient_pubkey_b64);
    const pubFp = (await sha256Hex(recipientPubBytes)).slice(0, 16);
    if (pubFp !== keyId) {
      throw new Error("Recipient public key fingerprint mismatch.");
    }

    const dropSecret = base64ToBytes(pairing.drop_secret_b64);
    if (dropSecret.length !== 32) {
      throw new Error("Invalid drop_secret length.");
    }

    let dropId = options.dropId ? String(options.dropId).toLowerCase() : "";
    if (!dropId) {
      const rnd = new Uint8Array(16);
      webCrypto.getRandomValues(rnd);
      dropId = bytesToHex(rnd);
    }
    if (!HEX32_RE.test(dropId)) {
      throw new Error("dropId must be 32 lowercase hex characters.");
    }
    const createdAt = options.createdAt || new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");

    const recipientKey = await subtle.importKey(
      "raw",
      recipientPubBytes,
      { name: "ECDH", namedCurve: "P-256" },
      false,
      []
    );
    const ephPair = await subtle.generateKey(
      { name: "ECDH", namedCurve: "P-256" },
      true,
      ["deriveBits"]
    );
    const epkRaw = new Uint8Array(await subtle.exportKey("raw", ephPair.publicKey));
    const epkB64 = bytesToBase64(epkRaw);

    const sharedBits = await subtle.deriveBits(
      { name: "ECDH", public: recipientKey },
      ephPair.privateKey,
      256
    );
    const hkdfBase = await subtle.importKey("raw", sharedBits, "HKDF", false, ["deriveKey"]);
    const infoBytes = utf8Bytes("SecondBrain-InboxDrop-v1|" + vaultId + "|" + keyId + "|" + tokenId);
    const aesKey = await subtle.deriveKey(
      {
        name: "HKDF",
        hash: "SHA-256",
        salt: hexToBytes(dropId),
        info: infoBytes,
      },
      hkdfBase,
      { name: "AES-GCM", length: 256 },
      false,
      ["encrypt"]
    );

    const cleanTags = Array.isArray(payload.tags)
      ? payload.tags.slice(0, 16).map((t) => String(t || "").slice(0, 64)).filter(Boolean)
      : [];
    const cleanDoc = {
      kind: "quick_capture",
      title: String(payload.title || "").slice(0, 300),
      url: String(payload.url || "").slice(0, 2048),
      text: String(payload.text || "").slice(0, 64000),
      tags: cleanTags,
      captured_at: String(payload.captured_at || createdAt).slice(0, 64),
      client: String(payload.client || "pwa").slice(0, 32),
    };
    const plaintextBytes = utf8Bytes(JSON.stringify(cleanDoc));

    const iv = new Uint8Array(12);
    webCrypto.getRandomValues(iv);
    const aadBytes = utf8Bytes(
      "SBID1|" + vaultId + "|" + dropId + "|" + keyId + "|" + tokenId + "|" + epkB64 + "|" + createdAt
    );
    const ctBuf = await subtle.encrypt(
      { name: "AES-GCM", iv: iv, additionalData: aadBytes, tagLength: 128 },
      aesKey,
      plaintextBytes
    );
    const bodyBytes = concatBytes([iv, new Uint8Array(ctBuf)]);
    const bodyShaHex = await sha256Hex(bodyBytes);

    const authMsg = utf8Bytes(
      "SBID1-AUTH\n" +
        vaultId + "\n" +
        dropId + "\n" +
        keyId + "\n" +
        tokenId + "\n" +
        epkB64 + "\n" +
        createdAt + "\n" +
        bodyShaHex
    );
    const authHmac = bytesToHex(await hmacSha256Bytes(dropSecret, authMsg));

    const headerObj = {
      auth_hmac: authHmac,
      created_at: createdAt,
      drop_id: dropId,
      epk_b64: epkB64,
      key_id: keyId,
      token_id: tokenId,
      v: SBID_VERSION,
      vault_id: vaultId,
    };
    const headerBytes = utf8Bytes(canonicalHeaderJson(headerObj));
    const lenBytes = new Uint8Array(2);
    lenBytes[0] = (headerBytes.length >> 8) & 0xff;
    lenBytes[1] = headerBytes.length & 0xff;

    const envelopeBytes = concatBytes([SBID_MAGIC, lenBytes, headerBytes, bodyBytes]);
    return {
      dropId: dropId,
      objectKey: "inbox-drops/" + dropId + ".enc",
      envelopeBytes: envelopeBytes,
      createdAt: createdAt,
    };
  }

  function awsUriEncodeSegment(seg) {
    return encodeURIComponent(seg).replace(/[!'()*]/g, (c) => "%" + c.charCodeAt(0).toString(16).toUpperCase());
  }

  async function signS3PutRequest(params) {
    const method = String(params.method || "PUT").toUpperCase();
    const parsed = new URL(String(params.url || ""));
    if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && parsed.hostname === "127.0.0.1")) {
      throw new Error("S3/R2 mobile inbox drop endpoint must use https:// (or http://127.0.0.1 for local tests).");
    }
    if (parsed.username || parsed.password || parsed.search || parsed.hash) {
      throw new Error("S3/R2 mobile inbox drop object URL must not contain embedded credentials, query strings, or fragments.");
    }
    const region = String(params.region || "auto").trim();
    if (!S3_REGION_RE.test(region)) {
      throw new Error("Invalid S3 region identifier.");
    }
    const accessKeyId = String(params.accessKeyId || "").trim();
    const secretAccessKey = String(params.secretAccessKey || "").trim();
    if (!accessKeyId || !secretAccessKey) {
      throw new Error("S3 Access Key ID and Secret Access Key are required.");
    }

    const payloadBytes = params.payloadBytes instanceof Uint8Array
      ? params.payloadBytes
      : new Uint8Array(params.payloadBytes || 0);
    const payloadHash = await sha256Hex(payloadBytes);

    const amzDate = params.amzDate || new Date().toISOString().replace(/[:-]|\.\d{3}/g, "");
    const dateStamp = amzDate.slice(0, 8);
    const hostHeader = parsed.host;

    const canonicalUri = (parsed.pathname || "/")
      .split("/")
      .map((s) => awsUriEncodeSegment(decodeURIComponent(s)))
      .join("/") || "/";

    const signable = {
      host: hostHeader,
      "x-amz-content-sha256": payloadHash,
      "x-amz-date": amzDate,
    };
    if (params.sessionToken) {
      signable["x-amz-security-token"] = String(params.sessionToken).trim();
    }

    const sortedKeys = Object.keys(signable).sort();
    let canonicalHeaders = "";
    for (const k of sortedKeys) {
      canonicalHeaders += k + ":" + signable[k].trim().replace(/\s+/g, " ") + "\n";
    }
    const signedHeaders = sortedKeys.join(";");

    const canonicalRequest = [
      method,
      canonicalUri,
      "",
      canonicalHeaders,
      signedHeaders,
      payloadHash,
    ].join("\n");

    const credentialScope = dateStamp + "/" + region + "/s3/aws4_request";
    const stringToSign = [
      "AWS4-HMAC-SHA256",
      amzDate,
      credentialScope,
      await sha256Hex(utf8Bytes(canonicalRequest)),
    ].join("\n");

    const kDate = await hmacSha256Bytes(utf8Bytes("AWS4" + secretAccessKey), utf8Bytes(dateStamp));
    const kRegion = await hmacSha256Bytes(kDate, utf8Bytes(region));
    const kService = await hmacSha256Bytes(kRegion, utf8Bytes("s3"));
    const kSigning = await hmacSha256Bytes(kService, utf8Bytes("aws4_request"));
    const signature = bytesToHex(await hmacSha256Bytes(kSigning, utf8Bytes(stringToSign)));

    const outHeaders = {
      "x-amz-content-sha256": payloadHash,
      "x-amz-date": amzDate,
      Authorization:
        "AWS4-HMAC-SHA256 Credential=" +
        accessKeyId +
        "/" +
        credentialScope +
        ", SignedHeaders=" +
        signedHeaders +
        ", Signature=" +
        signature,
    };
    if (method === "PUT" || method === "POST") {
      outHeaders["Content-Type"] = "application/octet-stream";
    }
    if (params.sessionToken) {
      outHeaders["x-amz-security-token"] = String(params.sessionToken).trim();
    }
    if (params.includeHostHeader) {
      outHeaders.Host = hostHeader;
    }
    return { url: parsed.toString(), headers: outHeaders };
  }

  async function signS3GetRequest(params) {
    return signS3PutRequest(Object.assign({}, params, {
      method: "GET",
      payloadBytes: new Uint8Array(0),
    }));
  }

  async function decryptMobileRecallEnvelope(envelopeBytes, pairing) {
    const subtle = getWebCrypto().subtle;
    const raw = envelopeBytes instanceof Uint8Array ? envelopeBytes : new Uint8Array(envelopeBytes || 0);
    if (raw.length < 8 + 12 + 16) {
      throw new Error("Truncated or invalid SBMR1 recall envelope.");
    }
    if (raw.length > 16777216) {
      throw new Error("Oversized SBMR1 recall envelope.");
    }
    for (let i = 0; i < SBMR_MAGIC.length; i += 1) {
      if (raw[i] !== SBMR_MAGIC[i]) {
        throw new Error("Invalid SBMR1 magic header.");
      }
    }
    const vaultId = String((pairing && pairing.vault_id) || "");
    const recallKeyId = String((pairing && pairing.recall_key_id) || "");
    const recallKeyB64 = String((pairing && pairing.recall_key_b64) || "");
    if (!HEX32_RE.test(vaultId)) {
      throw new Error("Invalid vault_id in pairing.");
    }
    if (!HEX16_RE.test(recallKeyId) || !recallKeyB64) {
      throw new Error("This pairing is write-only. Re-pair with `--include-recall` to decrypt vault recall.");
    }
    const recallKeyBytes = base64ToBytes(recallKeyB64);
    if (recallKeyBytes.length !== 32 || (await sha256Hex(recallKeyBytes)).slice(0, 16) !== recallKeyId) {
      throw new Error("Recall key fingerprint mismatch.");
    }

    const headerLen = (raw[6] << 8) | raw[7];
    if (headerLen <= 0 || headerLen > 4096 || raw.length < 8 + headerLen + 12 + 16) {
      throw new Error("Malformed SBMR1 header length.");
    }
    const headerText = new TextDecoder("utf-8", { fatal: true }).decode(raw.subarray(8, 8 + headerLen));
    const header = JSON.parse(headerText);
    if (!header || typeof header !== "object" || header.v !== SBMR_VERSION) {
      throw new Error("Unsupported SBMR1 envelope version.");
    }
    if (String(header.vault_id || "") !== vaultId) {
      throw new Error("SBMR1 vault_id mismatch.");
    }
    if (String(header.recall_key_id || "") !== recallKeyId) {
      throw new Error("SBMR1 recall_key_id mismatch (pairing needs rotated recall key).");
    }
    const saltHex = String(header.salt_hex || "");
    if (!HEX32_RE.test(saltHex)) {
      throw new Error("Invalid SBMR1 HKDF salt.");
    }

    const hkdfBase = await subtle.importKey("raw", recallKeyBytes, "HKDF", false, ["deriveKey"]);
    const aesKey = await subtle.deriveKey(
      {
        name: "HKDF",
        hash: "SHA-256",
        salt: hexToBytes(saltHex),
        info: utf8Bytes("SecondBrain-MobileRecall-v1|" + vaultId + "|" + recallKeyId),
      },
      hkdfBase,
      { name: "AES-GCM", length: 256 },
      false,
      ["decrypt"]
    );

    const bodyStart = 8 + headerLen;
    const iv = raw.subarray(bodyStart, bodyStart + 12);
    const ct = raw.subarray(bodyStart + 12);
    const exportedAt = String(header.exported_at || "");
    const atomCount = Number(header.atom_count);
    const reviewCount = Number(header.review_count);
    const aadBytes = utf8Bytes(
      "SBMR1|" + vaultId + "|" + recallKeyId + "|" + exportedAt + "|" + atomCount + "|" + reviewCount
    );
    let plainBuf;
    try {
      plainBuf = await subtle.decrypt(
        { name: "AES-GCM", iv: iv, additionalData: aadBytes, tagLength: 128 },
        aesKey,
        ct
      );
    } catch (_err) {
      throw new Error("SBMR1 recall index authentication or decryption failed.");
    }
    const doc = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(new Uint8Array(plainBuf)));
    if (!doc || typeof doc !== "object" || String(doc.vault_id || "") !== vaultId) {
      throw new Error("Decrypted SBMR1 catalog vault_id mismatch.");
    }
    return {
      vaultId: vaultId,
      exportedAt: String(doc.exported_at || exportedAt),
      atomCount: Array.isArray(doc.atoms) ? doc.atoms.length : 0,
      reviewCount: Array.isArray(doc.review_queue) ? doc.review_queue.length : 0,
      atoms: Array.isArray(doc.atoms) ? doc.atoms : [],
      reviewQueue: Array.isArray(doc.review_queue) ? doc.review_queue : [],
    };
  }

  function lightStem(word) {
    const w = String(word || "").toLowerCase();
    if (w.length <= 4) {
      return w;
    }
    if (w.endsWith("ies") && w.length > 5) {
      return w.slice(0, -3) + "y";
    }
    if (w.endsWith("ing") && w.length > 5) {
      return w.slice(0, -3);
    }
    if (w.endsWith("ed") && w.length > 4) {
      return w.slice(0, -2);
    }
    if (w.endsWith("es") && w.length > 4) {
      return w.slice(0, -2);
    }
    if (w.endsWith("s") && !w.endsWith("ss") && w.length > 4) {
      return w.slice(0, -1);
    }
    return w;
  }

  function tokenizeTerms(text) {
    const matches = String(text || "").toLowerCase().match(/[a-z0-9][a-z0-9_-]*/g);
    if (!matches) {
      return [];
    }
    const out = [];
    for (const raw of matches) {
      if (raw.length > 1 && !QUERY_STOP_WORDS.has(raw)) {
        out.push(lightStem(raw));
      }
      if (raw.indexOf("-") >= 0 || raw.indexOf("_") >= 0) {
        for (const sub of raw.split(/[-_]+/)) {
          if (sub.length > 1 && !QUERY_STOP_WORDS.has(sub)) {
            out.push(lightStem(sub));
          }
        }
      }
    }
    return out;
  }

  function normalizeProjectScope(scope) {
    const raw = String(scope || "").trim().toLowerCase();
    if (!raw) {
      return "";
    }
    return raw.startsWith("project:") ? raw : "project:" + raw;
  }

  function itemMatchesProjectScope(item, normScope) {
    if (!normScope) {
      return true;
    }
    const ctxs = Array.isArray(item.contexts) ? item.contexts : [];
    const projTags = [];
    for (const c of ctxs) {
      const lc = String(c || "").trim().toLowerCase();
      if (lc.startsWith("project:")) {
        projTags.push(lc);
      }
    }
    if (projTags.length === 0) {
      return false;
    }
    return projTags.every((t) => t === normScope);
  }

  function searchMobileRecallCatalog(catalog, query, opts) {
    const options = opts || {};
    const view = options.view === "review" ? "review" : "verified";
    const limit = Math.max(1, Math.min(Number(options.limit) || 50, 500));
    const normScope = normalizeProjectScope(options.projectScope || "");
    const sourceList = view === "review"
      ? (Array.isArray(catalog && catalog.reviewQueue) ? catalog.reviewQueue : [])
      : (Array.isArray(catalog && catalog.atoms) ? catalog.atoms : []);

    const scoped = sourceList.filter((item) => item && itemMatchesProjectScope(item, normScope));
    const qTerms = Array.from(new Set(tokenizeTerms(query || "")));
    if (qTerms.length === 0) {
      return scoped
        .slice()
        .sort((a, b) => String(a.filename || "").localeCompare(String(b.filename || "")))
        .slice(0, limit)
        .map((item) => Object.assign({}, item, { score: 1.0 }));
    }

    const qRawLower = String(query || "").trim().toLowerCase();
    const docs = scoped.map((item) => {
      const claimToks = tokenizeTerms(item.claim || "");
      const idToks = tokenizeTerms((item.id || "") + " " + (item.filename || ""));
      const tagToks = tokenizeTerms(
        (Array.isArray(item.tags) ? item.tags.join(" ") : "") +
          " " +
          (Array.isArray(item.contexts) ? item.contexts.join(" ") : "")
      );
      const evToks = tokenizeTerms((item.evidence || "") + " " + (item.why_it_matters || ""));
      const tf = new Map();
      const addWeight = (toks, w) => {
        for (const t of toks) {
          tf.set(t, (tf.get(t) || 0) + w);
        }
      };
      addWeight(claimToks, 3.0);
      addWeight(idToks, 2.0);
      addWeight(tagToks, 2.0);
      addWeight(evToks, 1.2);
      let dlen = 0;
      for (const v of tf.values()) {
        dlen += v;
      }
      return { item: item, tf: tf, dlen: dlen || 1.0 };
    });

    const df = new Map();
    for (const d of docs) {
      for (const term of d.tf.keys()) {
        df.set(term, (df.get(term) || 0) + 1);
      }
    }
    const nDocs = Math.max(1, docs.length);
    const avgDl = docs.reduce((acc, d) => acc + d.dlen, 0) / nDocs || 1.0;
    const k1 = 1.5;
    const b = 0.75;

    const scored = [];
    for (const d of docs) {
      let matched = 0;
      let bm25 = 0;
      const norm = k1 * (1 - b + b * (d.dlen / avgDl));
      for (const term of qTerms) {
        const freq = d.tf.get(term) || 0;
        if (freq > 0) {
          matched += 1;
          const docFreq = df.get(term) || 0;
          const idf = Math.log(1 + (nDocs - docFreq + 0.5) / (docFreq + 0.5));
          bm25 += idf * ((freq * (k1 + 1)) / (freq + norm));
        }
      }
      if (matched === 0) {
        continue;
      }
      if (qRawLower.length >= 4 && String(d.item.claim || "").toLowerCase().indexOf(qRawLower) >= 0) {
        bm25 += 2.0;
      }
      scored.push({
        item: d.item,
        matched: matched,
        score: Math.round(bm25 * 10000) / 10000,
      });
    }

    scored.sort((a, b) => {
      if (b.matched !== a.matched) {
        return b.matched - a.matched;
      }
      if (b.score !== a.score) {
        return b.score - a.score;
      }
      return String(a.item.filename || "").localeCompare(String(b.item.filename || ""));
    });

    return scored.slice(0, limit).map((entry) => Object.assign({}, entry.item, { score: entry.score }));
  }

  return {
    SBID_VERSION: SBID_VERSION,
    SBMR_VERSION: SBMR_VERSION,
    parsePairingFragment: parsePairingFragment,
    encryptInboxDropEnvelope: encryptInboxDropEnvelope,
    decryptMobileRecallEnvelope: decryptMobileRecallEnvelope,
    searchMobileRecallCatalog: searchMobileRecallCatalog,
    signS3PutRequest: signS3PutRequest,
    signS3GetRequest: signS3GetRequest,
    sha256Hex: sha256Hex,
  };
});
