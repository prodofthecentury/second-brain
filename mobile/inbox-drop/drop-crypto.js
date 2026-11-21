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
  const HEX32_RE = /^[0-9a-f]{32}$/;
  const HEX16_RE = /^[0-9a-f]{16}$/;
  const S3_REGION_RE = /^[a-zA-Z0-9-]{2,32}$/;

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
      "Content-Type": "application/octet-stream",
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
    if (params.sessionToken) {
      outHeaders["x-amz-security-token"] = String(params.sessionToken).trim();
    }
    if (params.includeHostHeader) {
      outHeaders.Host = hostHeader;
    }
    return { url: parsed.toString(), headers: outHeaders };
  }

  return {
    SBID_VERSION: SBID_VERSION,
    parsePairingFragment: parsePairingFragment,
    encryptInboxDropEnvelope: encryptInboxDropEnvelope,
    signS3PutRequest: signS3PutRequest,
    sha256Hex: sha256Hex,
  };
});
