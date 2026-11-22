(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.SecondBrainVaultCrypto = factory();
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const SYNC_SCHEMA_VERSION = 1;
  const BACKUP_SCHEMA_VERSION = 1;
  const SBID_VERSION = 1;

  const ENVELOPE_MAGIC = new Uint8Array([0x53, 0x42, 0x53, 0x59, 0x31, 0x00]); // "SBSY1\0"
  const BACKUP_MAGIC = new Uint8Array([0x53, 0x42, 0x56, 0x42, 0x4b, 0x50, 0x31, 0x00]); // "SBVBKP1\0"
  const SBID_MAGIC = new Uint8Array([0x53, 0x42, 0x49, 0x44, 0x31, 0x00]); // "SBID1\0"
  const DEVICE_STORE_MAGIC = new Uint8Array([0x53, 0x42, 0x44, 0x53, 0x31, 0x00]); // "SBDS1\0"

  const NONCE_BYTES = 12;
  const KEY_BYTES = 32;
  const GCM_TAG_BYTES = 16;
  const SCRYPT_DEFAULT_N = 32768;
  const SCRYPT_DEFAULT_R = 8;
  const SCRYPT_DEFAULT_P = 1;
  const DEVICE_PBKDF2_ITERATIONS = 210000;
  const MAX_TRUSTED_PEERS = 32;
  const MAX_SYNC_HEADER_BYTES = 65536;
  const MAX_SYNC_MANIFEST_BYTES = 16000000;
  const MAX_SYNC_FILE_BYTES = 50000000;
  const MAX_BACKUP_ARCHIVE_BYTES = 512 * 1024 * 1024;
  const MAX_EXTRACTED_TOTAL_BYTES = 512 * 1024 * 1024;
  const MAX_SINGLE_MEMBER_BYTES = 128 * 1024 * 1024;

  const HEX_ID_RE = /^(?:[0-9a-f]{2}){8,32}$/;
  const HEX32_RE = /^[0-9a-f]{32}$/;
  const HEX16_RE = /^[0-9a-f]{16}$/;
  const HEX64_RE = /^[0-9a-f]{64}$/;
  const DEVICE_ID_RE = /^[a-zA-Z0-9._-]{4,64}$/;
  const S3_REGION_RE = /^[a-zA-Z0-9_-]{1,64}$/;

  const ALLOWED_TOP_FILES = new Set(["house-rules.md"]);
  const ALLOWED_SYNC_ROOT_DIRS = new Set([
    "0-raw",
    "2-atoms",
    "3-threads",
    "4-experiences",
    "5-playbooks",
    "6-skills",
    "notes",
    "sources",
    "briefings",
  ]);
  const ALLOWED_BACKUP_ROOT_DIRS = new Set([
    "0-raw",
    "1-desk",
    "2-atoms",
    "3-threads",
    "4-experiences",
    "5-playbooks",
    "6-skills",
    "notes",
    "sources",
    "briefings",
  ]);
  const EXCLUDED_DIR_NAMES = new Set([
    ".sidecar",
    ".chroma",
    ".git",
    "__pycache__",
    "sync-conflicts",
    "node_modules",
    ".venv",
  ]);
  const EXCLUDED_SUFFIXES = [
    ".tmp",
    ".lock",
    ".sqlite3",
    ".sqlite3-wal",
    ".sqlite3-shm",
    ".db-wal",
    ".db-shm",
    ".pfx",
    ".p12",
    ".pem",
    ".key",
    ".sbvault",
  ];
  const WINDOWS_RESERVED_STEMS = new Set([
    "con",
    "prn",
    "aux",
    "nul",
    "com1",
    "com2",
    "com3",
    "com4",
    "com5",
    "com6",
    "com7",
    "com8",
    "com9",
    "lpt1",
    "lpt2",
    "lpt3",
    "lpt4",
    "lpt5",
    "lpt6",
    "lpt7",
    "lpt8",
    "lpt9",
  ]);
  const APPEND_ONLY_JSONL_PATHS = new Set([
    "0-raw/archive/decision-record.jsonl",
    "0-raw/archive/manifest.jsonl",
    "0-raw/archive/atom-corrections.jsonl",
    "0-raw/archive/mcp-audit.jsonl",
    "0-raw/archive/atom-feedback.jsonl",
    "0-raw/archive/playbook-decisions.jsonl",
    "0-raw/archive/skill-decisions.jsonl",
    "0-raw/archive/skill-executions.jsonl",
  ]);

  class SyncError extends Error {
    constructor(message) {
      super(message);
      this.name = "SyncError";
    }
  }

  class SyncSecurityError extends SyncError {
    constructor(message) {
      super(message);
      this.name = "SyncSecurityError";
    }
  }

  class VaultBackupError extends Error {
    constructor(message) {
      super(message);
      this.name = "VaultBackupError";
    }
  }

  class VaultBackupSecurityError extends VaultBackupError {
    constructor(message) {
      super(message);
      this.name = "VaultBackupSecurityError";
    }
  }

  // ─── Byte & Encoding Utilities ─────────────────────────────────────────────

  function getWebCrypto() {
    const c = typeof globalThis !== "undefined" ? globalThis.crypto : null;
    if (!c || !c.subtle) {
      throw new SyncError("WebCrypto (crypto.subtle) is required.");
    }
    return c;
  }

  function toUint8Array(input) {
    if (input instanceof Uint8Array) {
      return input;
    }
    if (ArrayBuffer.isView(input)) {
      return new Uint8Array(input.buffer, input.byteOffset, input.byteLength);
    }
    if (input instanceof ArrayBuffer) {
      return new Uint8Array(input);
    }
    throw new TypeError("Expected Uint8Array or ArrayBuffer.");
  }

  function utf8Bytes(str) {
    return new TextEncoder().encode(String(str == null ? "" : str));
  }

  function utf8DecodeStrict(bytes) {
    return new TextDecoder("utf-8", { fatal: true }).decode(toUint8Array(bytes));
  }

  function bytesToHex(bytes) {
    const u8 = toUint8Array(bytes);
    let out = "";
    for (let i = 0; i < u8.length; i += 1) {
      out += u8[i].toString(16).padStart(2, "0");
    }
    return out;
  }

  function hexToBytes(hex) {
    const clean = String(hex || "").trim().toLowerCase();
    if (clean.length % 2 !== 0 || !/^[0-9a-f]*$/.test(clean)) {
      throw new SyncSecurityError("Invalid hexadecimal string.");
    }
    const out = new Uint8Array(clean.length / 2);
    for (let i = 0; i < out.length; i += 1) {
      out[i] = parseInt(clean.slice(i * 2, i * 2 + 2), 16);
    }
    return out;
  }

  function bytesToBase64(bytes) {
    const u8 = toUint8Array(bytes);
    if (typeof Buffer !== "undefined") {
      return Buffer.from(u8.buffer, u8.byteOffset, u8.byteLength).toString("base64");
    }
    let bin = "";
    const chunk = 0x8000;
    for (let i = 0; i < u8.length; i += chunk) {
      bin += String.fromCharCode.apply(null, u8.subarray(i, i + chunk));
    }
    return btoa(bin);
  }

  function base64ToBytes(b64) {
    const clean = String(b64 || "").trim();
    if (!clean || !/^[A-Za-z0-9+/]+={0,2}$/.test(clean) || clean.length % 4 !== 0) {
      throw new SyncSecurityError("Malformed base64 input.");
    }
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

  function bytesToBase64Url(bytes) {
    return bytesToBase64(bytes).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  }

  function base64UrlToBytes(b64url) {
    let norm = String(b64url || "").trim().replace(/-/g, "+").replace(/_/g, "/");
    while (norm.length % 4 !== 0) {
      norm += "=";
    }
    return base64ToBytes(norm);
  }

  function concatBytes(arrays) {
    let total = 0;
    for (let i = 0; i < arrays.length; i += 1) {
      total += arrays[i].length;
    }
    const out = new Uint8Array(total);
    let offset = 0;
    for (let i = 0; i < arrays.length; i += 1) {
      out.set(arrays[i], offset);
      offset += arrays[i].length;
    }
    return out;
  }

  function constantTimeEqualBytes(a, b) {
    const ua = toUint8Array(a);
    const ub = toUint8Array(b);
    if (ua.length !== ub.length) {
      return false;
    }
    let diff = 0;
    for (let i = 0; i < ua.length; i += 1) {
      diff |= ua[i] ^ ub[i];
    }
    return diff === 0;
  }

  function constantTimeEqualStr(a, b) {
    const ba = utf8Bytes(String(a || ""));
    const bb = utf8Bytes(String(b || ""));
    return constantTimeEqualBytes(ba, bb);
  }

  function canonicalJson(value) {
    if (value === null || value === undefined) {
      return "null";
    }
    if (typeof value === "boolean" || typeof value === "number") {
      if (typeof value === "number" && !Number.isFinite(value)) {
        throw new TypeError("Non-finite number in canonical JSON.");
      }
      return JSON.stringify(value);
    }
    if (typeof value === "string") {
      return JSON.stringify(value);
    }
    if (Array.isArray(value)) {
      const items = [];
      for (let i = 0; i < value.length; i += 1) {
        items.push(canonicalJson(value[i]));
      }
      return "[" + items.join(",") + "]";
    }
    if (typeof value === "object") {
      const keys = Object.keys(value).sort();
      const pairs = [];
      for (let i = 0; i < keys.length; i += 1) {
        const k = keys[i];
        if (value[k] === undefined) {
          continue;
        }
        pairs.push(JSON.stringify(k) + ":" + canonicalJson(value[k]));
      }
      return "{" + pairs.join(",") + "}";
    }
    throw new TypeError("Unsupported type in canonical JSON.");
  }

  // ─── Pure-JS SHA-256 & HMAC-SHA256 (for synchronous RFC 7914 scrypt) ───────

  const SHA256_K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);

  function sha256BytesSync(messageBytes) {
    const msg = toUint8Array(messageBytes);
    const bitLenHi = Math.floor((msg.length * 8) / 0x100000000);
    const bitLenLo = (msg.length * 8) >>> 0;
    const padLen = (((msg.length + 9 + 63) & ~63) >>> 0);
    const padded = new Uint8Array(padLen);
    padded.set(msg, 0);
    padded[msg.length] = 0x80;
    const dv = new DataView(padded.buffer);
    dv.setUint32(padLen - 8, bitLenHi, false);
    dv.setUint32(padLen - 4, bitLenLo, false);

    let h0 = 0x6a09e667;
    let h1 = 0xbb67ae85;
    let h2 = 0x3c6ef372;
    let h3 = 0xa54ff53a;
    let h4 = 0x510e527f;
    let h5 = 0x9b05688c;
    let h6 = 0x1f83d9ab;
    let h7 = 0x5be0cd19;

    const w = new Uint32Array(64);
    for (let off = 0; off < padLen; off += 64) {
      for (let i = 0; i < 16; i += 1) {
        w[i] = dv.getUint32(off + i * 4, false);
      }
      for (let i = 16; i < 64; i += 1) {
        const w15 = w[i - 15];
        const w2 = w[i - 2];
        const s0 = (((w15 >>> 7) | (w15 << 25)) ^ ((w15 >>> 18) | (w15 << 14)) ^ (w15 >>> 3)) >>> 0;
        const s1 = (((w2 >>> 17) | (w2 << 15)) ^ ((w2 >>> 19) | (w2 << 13)) ^ (w2 >>> 10)) >>> 0;
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
      }

      let a = h0;
      let b = h1;
      let c = h2;
      let d = h3;
      let e = h4;
      let f = h5;
      let g = h6;
      let h = h7;

      for (let i = 0; i < 64; i += 1) {
        const S1 = (((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7))) >>> 0;
        const ch = ((e & f) ^ (~e & g)) >>> 0;
        const temp1 = (h + S1 + ch + SHA256_K[i] + w[i]) >>> 0;
        const S0 = (((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10))) >>> 0;
        const maj = ((a & b) ^ (a & c) ^ (b & c)) >>> 0;
        const temp2 = (S0 + maj) >>> 0;

        h = g;
        g = f;
        f = e;
        e = (d + temp1) >>> 0;
        d = c;
        c = b;
        b = a;
        a = (temp1 + temp2) >>> 0;
      }

      h0 = (h0 + a) >>> 0;
      h1 = (h1 + b) >>> 0;
      h2 = (h2 + c) >>> 0;
      h3 = (h3 + d) >>> 0;
      h4 = (h4 + e) >>> 0;
      h5 = (h5 + f) >>> 0;
      h6 = (h6 + g) >>> 0;
      h7 = (h7 + h) >>> 0;
    }

    const out = new Uint8Array(32);
    const outDv = new DataView(out.buffer);
    outDv.setUint32(0, h0, false);
    outDv.setUint32(4, h1, false);
    outDv.setUint32(8, h2, false);
    outDv.setUint32(12, h3, false);
    outDv.setUint32(16, h4, false);
    outDv.setUint32(20, h5, false);
    outDv.setUint32(24, h6, false);
    outDv.setUint32(28, h7, false);
    return out;
  }

  function sha256HexSync(dataBytes) {
    return bytesToHex(sha256BytesSync(dataBytes));
  }

  function hmacSha256BytesSync(keyBytes, messageBytes) {
    const rawKey = toUint8Array(keyBytes);
    const msg = toUint8Array(messageBytes);
    const keyBlock = new Uint8Array(64);
    if (rawKey.length > 64) {
      keyBlock.set(sha256BytesSync(rawKey), 0);
    } else {
      keyBlock.set(rawKey, 0);
    }
    const ipad = new Uint8Array(64 + msg.length);
    const opad = new Uint8Array(64 + 32);
    for (let i = 0; i < 64; i += 1) {
      ipad[i] = keyBlock[i] ^ 0x36;
      opad[i] = keyBlock[i] ^ 0x5c;
    }
    ipad.set(msg, 64);
    const inner = sha256BytesSync(ipad);
    opad.set(inner, 64);
    return sha256BytesSync(opad);
  }

  function pbkdf2HmacSha256SingleIter(passwordBytes, saltBytes, dkLen) {
    const pwd = toUint8Array(passwordBytes);
    const salt = toUint8Array(saltBytes);
    const blocks = Math.ceil(dkLen / 32);
    const out = new Uint8Array(blocks * 32);
    const saltWithIndex = new Uint8Array(salt.length + 4);
    saltWithIndex.set(salt, 0);
    const dv = new DataView(saltWithIndex.buffer);
    for (let blockIdx = 1; blockIdx <= blocks; blockIdx += 1) {
      dv.setUint32(salt.length, blockIdx, false);
      const u1 = hmacSha256BytesSync(pwd, saltWithIndex);
      out.set(u1, (blockIdx - 1) * 32);
    }
    return out.subarray(0, dkLen);
  }

  // ─── Pure-JS RFC 7914 scrypt(P, S, N, r, p, dkLen) ─────────────────────────

  function salsa20_8Core(B16, out16) {
    let x0 = B16[0] | 0;
    let x1 = B16[1] | 0;
    let x2 = B16[2] | 0;
    let x3 = B16[3] | 0;
    let x4 = B16[4] | 0;
    let x5 = B16[5] | 0;
    let x6 = B16[6] | 0;
    let x7 = B16[7] | 0;
    let x8 = B16[8] | 0;
    let x9 = B16[9] | 0;
    let x10 = B16[10] | 0;
    let x11 = B16[11] | 0;
    let x12 = B16[12] | 0;
    let x13 = B16[13] | 0;
    let x14 = B16[14] | 0;
    let x15 = B16[15] | 0;

    let t = 0;
    for (let round = 8; round > 0; round -= 2) {
      t = (x0 + x12) | 0; x4 ^= (t << 7) | (t >>> 25);
      t = (x4 + x0) | 0; x8 ^= (t << 9) | (t >>> 23);
      t = (x8 + x4) | 0; x12 ^= (t << 13) | (t >>> 19);
      t = (x12 + x8) | 0; x0 ^= (t << 18) | (t >>> 14);

      t = (x5 + x1) | 0; x9 ^= (t << 7) | (t >>> 25);
      t = (x9 + x5) | 0; x13 ^= (t << 9) | (t >>> 23);
      t = (x13 + x9) | 0; x1 ^= (t << 13) | (t >>> 19);
      t = (x1 + x13) | 0; x5 ^= (t << 18) | (t >>> 14);

      t = (x10 + x6) | 0; x14 ^= (t << 7) | (t >>> 25);
      t = (x14 + x10) | 0; x2 ^= (t << 9) | (t >>> 23);
      t = (x2 + x14) | 0; x6 ^= (t << 13) | (t >>> 19);
      t = (x6 + x2) | 0; x10 ^= (t << 18) | (t >>> 14);

      t = (x15 + x11) | 0; x3 ^= (t << 7) | (t >>> 25);
      t = (x3 + x15) | 0; x7 ^= (t << 9) | (t >>> 23);
      t = (x7 + x3) | 0; x11 ^= (t << 13) | (t >>> 19);
      t = (x11 + x7) | 0; x15 ^= (t << 18) | (t >>> 14);

      t = (x0 + x3) | 0; x1 ^= (t << 7) | (t >>> 25);
      t = (x1 + x0) | 0; x2 ^= (t << 9) | (t >>> 23);
      t = (x2 + x1) | 0; x3 ^= (t << 13) | (t >>> 19);
      t = (x3 + x2) | 0; x0 ^= (t << 18) | (t >>> 14);

      t = (x5 + x4) | 0; x6 ^= (t << 7) | (t >>> 25);
      t = (x6 + x5) | 0; x7 ^= (t << 9) | (t >>> 23);
      t = (x7 + x6) | 0; x4 ^= (t << 13) | (t >>> 19);
      t = (x4 + x7) | 0; x5 ^= (t << 18) | (t >>> 14);

      t = (x10 + x9) | 0; x11 ^= (t << 7) | (t >>> 25);
      t = (x11 + x10) | 0; x8 ^= (t << 9) | (t >>> 23);
      t = (x8 + x11) | 0; x9 ^= (t << 13) | (t >>> 19);
      t = (x9 + x8) | 0; x10 ^= (t << 18) | (t >>> 14);

      t = (x15 + x14) | 0; x12 ^= (t << 7) | (t >>> 25);
      t = (x12 + x15) | 0; x13 ^= (t << 9) | (t >>> 23);
      t = (x13 + x12) | 0; x14 ^= (t << 13) | (t >>> 19);
      t = (x14 + x13) | 0; x15 ^= (t << 18) | (t >>> 14);
    }

    out16[0] = (B16[0] + x0) | 0;
    out16[1] = (B16[1] + x1) | 0;
    out16[2] = (B16[2] + x2) | 0;
    out16[3] = (B16[3] + x3) | 0;
    out16[4] = (B16[4] + x4) | 0;
    out16[5] = (B16[5] + x5) | 0;
    out16[6] = (B16[6] + x6) | 0;
    out16[7] = (B16[7] + x7) | 0;
    out16[8] = (B16[8] + x8) | 0;
    out16[9] = (B16[9] + x9) | 0;
    out16[10] = (B16[10] + x10) | 0;
    out16[11] = (B16[11] + x11) | 0;
    out16[12] = (B16[12] + x12) | 0;
    out16[13] = (B16[13] + x13) | 0;
    out16[14] = (B16[14] + x14) | 0;
    out16[15] = (B16[15] + x15) | 0;
  }

  function scryptBlockMix(inputWords, outputWords, scratchX, r) {
    const lastBlockOffset = (2 * r - 1) * 16;
    for (let k = 0; k < 16; k += 1) {
      scratchX[k] = inputWords[lastBlockOffset + k];
    }
    const twoR = 2 * r;
    for (let i = 0; i < twoR; i += 1) {
      const inOff = i * 16;
      for (let k = 0; k < 16; k += 1) {
        scratchX[k] ^= inputWords[inOff + k];
      }
      salsa20_8Core(scratchX, scratchX);
      const outBlockIndex = (i & 1) === 0 ? (i >>> 1) : (r + (i >>> 1));
      const outOff = outBlockIndex * 16;
      for (let k = 0; k < 16; k += 1) {
        outputWords[outOff + k] = scratchX[k];
      }
    }
  }

  function scrypt(passwordInput, saltInput, N, r, p, dkLen) {
    const pwdBytes = typeof passwordInput === "string" ? utf8Bytes(passwordInput) : toUint8Array(passwordInput);
    const saltBytes = typeof saltInput === "string" ? utf8Bytes(saltInput) : toUint8Array(saltInput);
    const nVal = Number(N);
    const rVal = Number(r);
    const pVal = Number(p);
    const outLen = dkLen === undefined ? 32 : Number(dkLen);

    if (
      !Number.isInteger(nVal) ||
      nVal <= 1 ||
      nVal > 1048576 ||
      (nVal & (nVal - 1)) !== 0 ||
      !Number.isInteger(rVal) ||
      rVal < 1 ||
      rVal > 32 ||
      !Number.isInteger(pVal) ||
      pVal < 1 ||
      pVal > 32 ||
      !Number.isInteger(outLen) ||
      outLen < 1 ||
      outLen > 1024
    ) {
      throw new RangeError("Invalid scrypt parameters.");
    }
    if (128 * nVal * rVal > 256 * 1024 * 1024 || 128 * rVal * pVal > 64 * 1024 * 1024) {
      throw new RangeError("scrypt work factor exceeds memory safety ceiling.");
    }

    const blockByteLen = 128 * rVal;
    const blockWordLen = 32 * rVal;
    const B = new Uint8Array(pbkdf2HmacSha256SingleIter(pwdBytes, saltBytes, pVal * blockByteLen));
    const bView = new DataView(B.buffer, B.byteOffset, B.byteLength);

    const V = new Int32Array(nVal * blockWordLen);
    const X = new Int32Array(blockWordLen);
    const Y = new Int32Array(blockWordLen);
    const scratch16 = new Int32Array(16);
    const nMask = nVal - 1;
    const integerifyOffset = (2 * rVal - 1) * 16;

    for (let block = 0; block < pVal; block += 1) {
      const byteBase = block * blockByteLen;
      for (let w = 0; w < blockWordLen; w += 1) {
        X[w] = bView.getInt32(byteBase + w * 4, true);
      }

      for (let i = 0; i < nVal; i += 1) {
        V.set(X, i * blockWordLen);
        scryptBlockMix(X, Y, scratch16, rVal);
        X.set(Y);
      }

      for (let i = 0; i < nVal; i += 1) {
        const j = X[integerifyOffset] & nMask;
        const vOff = j * blockWordLen;
        for (let w = 0; w < blockWordLen; w += 1) {
          X[w] ^= V[vOff + w];
        }
        scryptBlockMix(X, Y, scratch16, rVal);
        X.set(Y);
      }

      for (let w = 0; w < blockWordLen; w += 1) {
        bView.setInt32(byteBase + w * 4, X[w], true);
      }
    }

    return new Uint8Array(pbkdf2HmacSha256SingleIter(pwdBytes, B, outLen));
  }

  function deriveMasterKeyFromPassphrase(passphrase, saltBytes, scryptParams) {
    if (typeof passphrase !== "string" || passphrase.length < 8) {
      throw new SyncSecurityError("Sync passphrase must be at least 8 characters.");
    }
    const salt = toUint8Array(saltBytes);
    if (salt.length < 16) {
      throw new SyncSecurityError("KDF salt must be at least 16 bytes.");
    }
    const n = scryptParams && scryptParams.n ? Number(scryptParams.n) : SCRYPT_DEFAULT_N;
    const r = scryptParams && scryptParams.r ? Number(scryptParams.r) : SCRYPT_DEFAULT_R;
    const p = scryptParams && scryptParams.p ? Number(scryptParams.p) : SCRYPT_DEFAULT_P;
    return scrypt(utf8Bytes(passphrase), salt, n, r, p, KEY_BYTES);
  }

  // ─── WebCrypto SHA-256 / SHA-512 / HMAC / HKDF / AES-256-GCM ──────────────

  async function sha256Bytes(dataBytes) {
    const subtle = getWebCrypto().subtle;
    const buf = await subtle.digest("SHA-256", toUint8Array(dataBytes));
    return new Uint8Array(buf);
  }

  async function sha256Hex(dataBytes) {
    return bytesToHex(await sha256Bytes(dataBytes));
  }

  async function sha512Bytes(dataBytes) {
    const subtle = getWebCrypto().subtle;
    const buf = await subtle.digest("SHA-512", toUint8Array(dataBytes));
    return new Uint8Array(buf);
  }

  async function hmacSha256Bytes(keyBytes, messageBytes) {
    const subtle = getWebCrypto().subtle;
    const cryptoKey = await subtle.importKey(
      "raw",
      toUint8Array(keyBytes),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"]
    );
    const sig = await subtle.sign("HMAC", cryptoKey, toUint8Array(messageBytes));
    return new Uint8Array(sig);
  }

  async function hkdfSha256(ikmBytes, saltBytes, infoBytes, lengthBytes) {
    const subtle = getWebCrypto().subtle;
    const baseKey = await subtle.importKey("raw", toUint8Array(ikmBytes), "HKDF", false, ["deriveBits"]);
    const bits = await subtle.deriveBits(
      {
        name: "HKDF",
        hash: "SHA-256",
        salt: toUint8Array(saltBytes),
        info: toUint8Array(infoBytes),
      },
      baseKey,
      (lengthBytes || KEY_BYTES) * 8
    );
    return new Uint8Array(bits);
  }

  async function aes256GcmEncrypt(keyBytes, nonceBytes, plaintextBytes, aadBytes) {
    const subtle = getWebCrypto().subtle;
    const key = await subtle.importKey("raw", toUint8Array(keyBytes), { name: "AES-GCM" }, false, ["encrypt"]);
    const params = {
      name: "AES-GCM",
      iv: toUint8Array(nonceBytes),
      tagLength: 128,
    };
    if (aadBytes !== undefined && aadBytes !== null) {
      params.additionalData = toUint8Array(aadBytes);
    }
    const ct = await subtle.encrypt(params, key, toUint8Array(plaintextBytes));
    return new Uint8Array(ct);
  }

  async function aes256GcmDecrypt(keyBytes, nonceBytes, ciphertextBytes, aadBytes, errorClass) {
    const ErrType = errorClass || SyncSecurityError;
    const subtle = getWebCrypto().subtle;
    const key = await subtle.importKey("raw", toUint8Array(keyBytes), { name: "AES-GCM" }, false, ["decrypt"]);
    const params = {
      name: "AES-GCM",
      iv: toUint8Array(nonceBytes),
      tagLength: 128,
    };
    if (aadBytes !== undefined && aadBytes !== null) {
      params.additionalData = toUint8Array(aadBytes);
    }
    try {
      const pt = await subtle.decrypt(params, key, toUint8Array(ciphertextBytes));
      return new Uint8Array(pt);
    } catch (_err) {
      throw new ErrType("AES-256-GCM authentication failed (tampered ciphertext, wrong key, or AAD mismatch).");
    }
  }

  // ─── Ed25519 Verification (WebCrypto + Pure-JS BigInt RFC 8032 Fallback) ──

  const ED_P = (1n << 255n) - 19n;
  const ED_L = (1n << 252n) + 27742317777372353535851937790883648493n;

  function edMod(a, m) {
    const r = a % m;
    return r >= 0n ? r : r + m;
  }

  function edModPow(base, exp, mod) {
    let result = 1n;
    let b = edMod(base, mod);
    let e = exp;
    while (e > 0n) {
      if ((e & 1n) === 1n) {
        result = (result * b) % mod;
      }
      b = (b * b) % mod;
      e >>= 1n;
    }
    return result;
  }

  function edModInv(a, mod) {
    return edModPow(a, mod - 2n, mod);
  }

  const ED_D = edMod(-121665n * edModInv(121666n, ED_P), ED_P);
  const ED_SQRT_M1 = edModPow(2n, (ED_P - 1n) / 4n, ED_P);
  const ED_ZERO = [0n, 1n, 1n, 0n];

  function edRecoverX(y, signBit) {
    if (y >= ED_P) {
      return null;
    }
    const y2 = edMod(y * y, ED_P);
    const u = edMod(y2 - 1n, ED_P);
    const v = edMod(ED_D * y2 + 1n, ED_P);
    const uv = edMod(u * edModInv(v, ED_P), ED_P);
    let x = edModPow(uv, (ED_P + 3n) / 8n, ED_P);
    const x2 = edMod(x * x, ED_P);
    if (x2 !== uv) {
      if (x2 === edMod(ED_P - uv, ED_P)) {
        x = edMod(x * ED_SQRT_M1, ED_P);
      } else {
        return null;
      }
    }
    if (x === 0n && signBit === 1) {
      return null;
    }
    if (Number(x & 1n) !== signBit) {
      x = ED_P - x;
    }
    return x;
  }

  const ED_BY = edMod(4n * edModInv(5n, ED_P), ED_P);
  const ED_BX = edRecoverX(ED_BY, 0);
  const ED_B = [ED_BX, ED_BY, 1n, edMod(ED_BX * ED_BY, ED_P)];

  function leBytesToBigInt(bytes) {
    let acc = 0n;
    for (let i = bytes.length - 1; i >= 0; i -= 1) {
      acc = (acc << 8n) | BigInt(bytes[i]);
    }
    return acc;
  }

  function edDecodePoint(bytes32) {
    if (bytes32.length !== 32) {
      return null;
    }
    const copy = new Uint8Array(bytes32);
    const signBit = (copy[31] >>> 7) & 1;
    copy[31] &= 0x7f;
    const y = leBytesToBigInt(copy);
    if (y >= ED_P) {
      return null;
    }
    const x = edRecoverX(y, signBit);
    if (x === null) {
      return null;
    }
    return [x, y, 1n, edMod(x * y, ED_P)];
  }

  function edPointAdd(P1, P2) {
    const X1 = P1[0], Y1 = P1[1], Z1 = P1[2], T1 = P1[3];
    const X2 = P2[0], Y2 = P2[1], Z2 = P2[2], T2 = P2[3];
    const A = edMod((Y1 - X1) * (Y2 - X2), ED_P);
    const B = edMod((Y1 + X1) * (Y2 + X2), ED_P);
    const C = edMod(T1 * 2n * ED_D * T2, ED_P);
    const D = edMod(Z1 * 2n * Z2, ED_P);
    const E = edMod(B - A, ED_P);
    const F = edMod(D - C, ED_P);
    const G = edMod(D + C, ED_P);
    const H = edMod(B + A, ED_P);
    return [
      edMod(E * F, ED_P),
      edMod(G * H, ED_P),
      edMod(F * G, ED_P),
      edMod(E * H, ED_P),
    ];
  }

  function edScalarMult(point, scalar) {
    let res = ED_ZERO;
    let addend = point;
    let s = scalar;
    while (s > 0n) {
      if ((s & 1n) === 1n) {
        res = edPointAdd(res, addend);
      }
      addend = edPointAdd(addend, addend);
      s >>= 1n;
    }
    return res;
  }

  function edMul8(point) {
    const p2 = edPointAdd(point, point);
    const p4 = edPointAdd(p2, p2);
    return edPointAdd(p4, p4);
  }

  function edPointEquals(P1, P2) {
    return (
      edMod(P1[0] * P2[2] - P2[0] * P1[2], ED_P) === 0n &&
      edMod(P1[1] * P2[2] - P2[1] * P1[2], ED_P) === 0n
    );
  }

  async function verifyEd25519PureJs(pubKeyBytes32, messageBytes, sigBytes64) {
    if (pubKeyBytes32.length !== 32 || sigBytes64.length !== 64) {
      return false;
    }
    const rBytes = sigBytes64.subarray(0, 32);
    const sBytes = sigBytes64.subarray(32, 64);
    const S = leBytesToBigInt(sBytes);
    if (S >= ED_L) {
      return false;
    }
    const A = edDecodePoint(pubKeyBytes32);
    if (A === null || edPointEquals(edMul8(A), ED_ZERO)) {
      return false;
    }
    const R = edDecodePoint(rBytes);
    if (R === null) {
      return false;
    }
    const hashInput = concatBytes([rBytes, pubKeyBytes32, messageBytes]);
    const h512 = await sha512Bytes(hashInput);
    const k = edMod(leBytesToBigInt(h512), ED_L);
    const lhs = edMul8(edScalarMult(ED_B, S));
    const rhs = edMul8(edPointAdd(R, edScalarMult(A, k)));
    return edPointEquals(lhs, rhs);
  }

  async function verifyEd25519Signature(pubKeyBytes32, messageBytes, sigBytes64, options) {
    const pub = toUint8Array(pubKeyBytes32);
    const msg = toUint8Array(messageBytes);
    const sig = toUint8Array(sigBytes64);
    if (pub.length !== 32 || sig.length !== 64) {
      return false;
    }
    const opts = options || {};
    if (!opts.forcePureJs) {
      try {
        const subtle = getWebCrypto().subtle;
        const key = await subtle.importKey("raw", pub, { name: "Ed25519" }, false, ["verify"]);
        const ok = await subtle.verify({ name: "Ed25519" }, key, sig, msg);
        if (!ok) {
          return false;
        }
        // Also reject low-order torsion pubkeys identically across platforms
        const sScalar = leBytesToBigInt(sig.subarray(32, 64));
        if (sScalar >= ED_L) {
          return false;
        }
        return true;
      } catch (_unsupportedErr) {
        // Fall through to pure-JS BigInt RFC 8032 verifier on iOS/Android WebViews without WebCrypto Ed25519
      }
    }
    return verifyEd25519PureJs(pub, msg, sig);
  }

  // ─── Exact vault_sync.py E2EE Primitives & Path Validation ─────────────────

  function validateSyncRelPath(relPath) {
    if (typeof relPath !== "string" || !relPath.trim()) {
      throw new SyncSecurityError("Empty sync path rejected.");
    }
    if (
      relPath.indexOf("\x00") !== -1 ||
      relPath.indexOf("\\") !== -1 ||
      relPath.startsWith("/") ||
      relPath.indexOf(":") !== -1
    ) {
      throw new SyncSecurityError("Unsafe sync relative path rejected: " + JSON.stringify(relPath));
    }
    const parts = relPath.split("/");
    for (let i = 0; i < parts.length; i += 1) {
      const seg = parts[i];
      if (!seg || seg === "." || seg === "..") {
        throw new SyncSecurityError("Path traversal segment rejected: " + JSON.stringify(relPath));
      }
      const dotIdx = seg.lastIndexOf(".");
      const stemRaw = dotIdx > 0 ? seg.slice(0, dotIdx) : seg;
      const stem = stemRaw.replace(/[ .]+$/g, "").toLowerCase();
      const firstStem = seg.split(".", 1)[0].replace(/[ .]+$/g, "").toLowerCase();
      if (WINDOWS_RESERVED_STEMS.has(stem) || WINDOWS_RESERVED_STEMS.has(firstStem)) {
        throw new SyncSecurityError("Windows reserved filename rejected in sync path: " + JSON.stringify(relPath));
      }
    }
    if (parts.length === 1) {
      if (!ALLOWED_TOP_FILES.has(parts[0])) {
        throw new SyncSecurityError("Top-level file not in durable sync allowlist: " + JSON.stringify(relPath));
      }
      return relPath;
    }
    const rootDir = parts[0];
    if (!ALLOWED_SYNC_ROOT_DIRS.has(rootDir)) {
      throw new SyncSecurityError(
        "Directory '" + rootDir + "' is excluded by Three-Store sync boundary: " + JSON.stringify(relPath)
      );
    }
    for (let i = 0; i < parts.length - 1; i += 1) {
      if (EXCLUDED_DIR_NAMES.has(parts[i])) {
        throw new SyncSecurityError("Excluded internal directory in sync path: " + JSON.stringify(relPath));
      }
    }
    const leafLow = parts[parts.length - 1].toLowerCase();
    if (leafLow.startsWith("~") || leafLow === ".env" || leafLow.startsWith(".env.") || leafLow.startsWith(".tmp-")) {
      throw new SyncSecurityError("Transient or secret file excluded from sync: " + JSON.stringify(relPath));
    }
    for (let i = 0; i < EXCLUDED_SUFFIXES.length; i += 1) {
      if (leafLow.endsWith(EXCLUDED_SUFFIXES[i])) {
        throw new SyncSecurityError("Transient/index file excluded from sync: " + JSON.stringify(relPath));
      }
    }
    return relPath;
  }

  function validateBackupRelPath(relPath) {
    if (typeof relPath !== "string" || !relPath.trim()) {
      throw new VaultBackupSecurityError("Empty archive member path.");
    }
    if (
      relPath.indexOf("\x00") !== -1 ||
      relPath.indexOf("\\") !== -1 ||
      relPath.startsWith("/") ||
      relPath.indexOf(":") !== -1
    ) {
      throw new VaultBackupSecurityError("Unsafe archive member path rejected: " + JSON.stringify(relPath));
    }
    if (relPath === "backup-manifest.json") {
      return relPath;
    }
    const parts = relPath.split("/");
    for (let i = 0; i < parts.length; i += 1) {
      const seg = parts[i];
      if (!seg || seg === "." || seg === "..") {
        throw new VaultBackupSecurityError("Path traversal in archive member rejected: " + JSON.stringify(relPath));
      }
      const firstStem = seg.split(".", 1)[0].replace(/[ .]+$/g, "").toLowerCase();
      if (WINDOWS_RESERVED_STEMS.has(firstStem)) {
        throw new VaultBackupSecurityError("Windows reserved filename in archive rejected: " + JSON.stringify(relPath));
      }
    }
    if (parts.length === 1 && ALLOWED_TOP_FILES.has(parts[0])) {
      return relPath;
    }
    if (parts.length < 2 || !ALLOWED_BACKUP_ROOT_DIRS.has(parts[0])) {
      throw new VaultBackupSecurityError("Non-durable or forbidden archive top-level path: " + JSON.stringify(relPath));
    }
    for (let i = 0; i < parts.length - 1; i += 1) {
      if (EXCLUDED_DIR_NAMES.has(parts[i])) {
        throw new VaultBackupSecurityError("Forbidden disposable directory in archive: " + JSON.stringify(relPath));
      }
    }
    const leafLow = parts[parts.length - 1].toLowerCase();
    if (leafLow === ".env" || leafLow.startsWith(".env.") || leafLow.startsWith(".tmp-")) {
      throw new VaultBackupSecurityError("Forbidden secret/temp file in archive: " + JSON.stringify(relPath));
    }
    for (let i = 0; i < EXCLUDED_SUFFIXES.length; i += 1) {
      if (leafLow.endsWith(EXCLUDED_SUFFIXES[i])) {
        throw new VaultBackupSecurityError("Forbidden sidecar/key file in archive: " + JSON.stringify(relPath));
      }
    }
    return parts.join("/");
  }

  function validateBackendKey(key) {
    if (typeof key !== "string" || !key || key.indexOf("\x00") !== -1 || key.indexOf("\\") !== -1 || key.startsWith("/")) {
      throw new SyncSecurityError("Invalid sync object key: " + JSON.stringify(key));
    }
    const parts = key.split("/");
    for (let i = 0; i < parts.length; i += 1) {
      if (!parts[i] || parts[i] === "." || parts[i] === "..") {
        throw new SyncSecurityError("Unsafe sync object key traversal: " + JSON.stringify(key));
      }
    }
    const ns = parts[0];
    if (ns !== "vault-header.json" && ns !== "head.json" && ns !== "manifests" && ns !== "objects" && ns !== "inbox-drops") {
      throw new SyncSecurityError("Disallowed sync object namespace: " + JSON.stringify(key));
    }
    if (ns === "inbox-drops") {
      if (parts.length !== 2 || !/^[0-9a-f]{32}\.enc$/.test(parts[1])) {
        throw new SyncSecurityError("Invalid inbox-drop object key format: " + JSON.stringify(key));
      }
    }
    return key;
  }

  async function deriveSyncSubkey(masterKeyBytes, vaultIdHex, purpose, objectId) {
    const mk = toUint8Array(masterKeyBytes);
    if (mk.length !== KEY_BYTES) {
      throw new SyncSecurityError("Master key must be exactly 32 bytes.");
    }
    const vid = String(vaultIdHex || "").trim().toLowerCase();
    if (!HEX_ID_RE.test(vid)) {
      throw new SyncSecurityError("Invalid vaultIdHex.");
    }
    const objId = objectId === undefined || objectId === null ? "" : String(objectId);
    const info = utf8Bytes("second-brain-sync-v1:" + String(purpose) + ":" + objId);
    return hkdfSha256(mk, hexToBytes(vid), info, KEY_BYTES);
  }

  async function computeBlindBlobId(masterKeyBytes, vaultIdHex, plaintextSha256Hex) {
    const shaClean = String(plaintextSha256Hex || "").trim().toLowerCase();
    if (!HEX64_RE.test(shaClean)) {
      throw new SyncSecurityError("Invalid plaintext SHA-256 hex.");
    }
    const blindKey = await deriveSyncSubkey(masterKeyBytes, vaultIdHex, "blind-blob-id", "");
    const mac = await hmacSha256Bytes(blindKey, hexToBytes(shaClean));
    return bytesToHex(mac);
  }

  async function encryptSyncObject(masterKeyBytes, params) {
    const vaultId = String(params.vaultId || "").trim().toLowerCase();
    const kind = String(params.kind || "");
    const objectId = String(params.objectId || "");
    const plaintext = toUint8Array(params.plaintextBytes);
    const subkey = await deriveSyncSubkey(masterKeyBytes, vaultId, "aead-" + kind, objectId);
    const nonce = params.nonceBytes ? toUint8Array(params.nonceBytes) : getWebCrypto().getRandomValues(new Uint8Array(NONCE_BYTES));
    const aad = utf8Bytes("sb-sync-v1|" + vaultId + "|" + kind + "|" + objectId);
    const ct = await aes256GcmEncrypt(subkey, nonce, plaintext, aad);
    return concatBytes([ENVELOPE_MAGIC, nonce, ct]);
  }

  async function decryptSyncObject(masterKeyBytes, params) {
    const vaultId = String(params.vaultId || "").trim().toLowerCase();
    const kind = String(params.kind || "");
    const objectId = String(params.objectId || "");
    const env = toUint8Array(params.envelopeBytes);
    const minLen = ENVELOPE_MAGIC.length + NONCE_BYTES + GCM_TAG_BYTES;
    if (env.length < minLen) {
      throw new SyncSecurityError("Truncated encrypted " + kind + " envelope for " + objectId + ".");
    }
    if (!constantTimeEqualBytes(env.subarray(0, ENVELOPE_MAGIC.length), ENVELOPE_MAGIC)) {
      throw new SyncSecurityError("Invalid envelope magic header for " + kind + " " + objectId + ".");
    }
    const nonce = env.subarray(ENVELOPE_MAGIC.length, ENVELOPE_MAGIC.length + NONCE_BYTES);
    const ct = env.subarray(ENVELOPE_MAGIC.length + NONCE_BYTES);
    const subkey = await deriveSyncSubkey(masterKeyBytes, vaultId, "aead-" + kind, objectId);
    const aad = utf8Bytes("sb-sync-v1|" + vaultId + "|" + kind + "|" + objectId);
    return aes256GcmDecrypt(subkey, nonce, ct, aad, SyncSecurityError);
  }

  function headSigningBytes(head) {
    if (!head || typeof head !== "object") {
      throw new SyncSecurityError("Malformed sync head object.");
    }
    const schemaVer = Number(head.schema_version);
    const epoch = Number(head.epoch);
    if (!Number.isInteger(schemaVer) || !Number.isInteger(epoch)) {
      throw new SyncSecurityError("Invalid integer fields in sync head.");
    }
    const payload = {
      schema_version: schemaVer,
      vault_id: String(head.vault_id),
      epoch: epoch,
      parent_manifest_id: head.parent_manifest_id === undefined || head.parent_manifest_id === null
        ? null
        : String(head.parent_manifest_id),
      manifest_id: String(head.manifest_id),
      manifest_ciphertext_sha256: String(head.manifest_ciphertext_sha256),
      device_id: String(head.device_id),
      device_pubkey_b64: String(head.device_pubkey_b64),
      created_at: String(head.created_at),
    };
    return utf8Bytes("second-brain-sync-head-v1\n" + canonicalJson(payload));
  }

  async function computeHeadMac(masterKeyBytes, vaultIdHex, head) {
    const macKey = await deriveSyncSubkey(masterKeyBytes, vaultIdHex, "head-auth", "");
    const mac = await hmacSha256Bytes(macKey, headSigningBytes(head));
    return bytesToHex(mac);
  }

  async function verifyRemoteSyncHead(head, masterKeyBytes, options) {
    const opts = options || {};
    if (!head || typeof head !== "object") {
      throw new SyncSecurityError("Malformed remote head.json.");
    }
    if (Number(head.schema_version) !== SYNC_SCHEMA_VERSION) {
      throw new SyncSecurityError("Unsupported remote head schema_version.");
    }
    const vaultId = String(head.vault_id || "").trim().toLowerCase();
    if (!HEX_ID_RE.test(vaultId)) {
      throw new SyncSecurityError("Invalid vault_id in remote head.json.");
    }
    if (opts.vaultId && vaultId !== String(opts.vaultId).trim().toLowerCase()) {
      throw new SyncSecurityError("Remote head.json belongs to a different vault_id.");
    }
    const manifestId = String(head.manifest_id || "").trim().toLowerCase();
    if (!HEX_ID_RE.test(manifestId)) {
      throw new SyncSecurityError("Malformed manifest_id in remote head.json.");
    }
    const manifestCtSha = String(head.manifest_ciphertext_sha256 || "").trim().toLowerCase();
    if (!HEX64_RE.test(manifestCtSha)) {
      throw new SyncSecurityError("Malformed manifest_ciphertext_sha256 in remote head.json.");
    }

    const devId = String(head.device_id || "");
    if (!DEVICE_ID_RE.test(devId)) {
      throw new SyncSecurityError("Invalid device_id format in remote sync head: " + JSON.stringify(devId));
    }

    // 1. Master-key Encrypt-then-MAC gate (prevents unauthenticated head processing)
    const suppliedMac = String(head.head_mac || "").trim().toLowerCase();
    if (!suppliedMac || !HEX64_RE.test(suppliedMac)) {
      throw new SyncSecurityError("Missing master-key HMAC ('head_mac') on remote sync head.");
    }
    const expectedMac = await computeHeadMac(masterKeyBytes, vaultId, head);
    if (!constantTimeEqualStr(suppliedMac, expectedMac)) {
      throw new SyncSecurityError("Master-key HMAC verification failed on remote sync head (unauthenticated head.json).");
    }

    // 2. Ed25519 device signature gate
    const pubB64 = String(head.device_pubkey_b64 || "").trim();
    const sigB64 = String(head.signature_b64 || "").trim();
    if (!pubB64 || pubB64.length > 128 || !sigB64 || sigB64.length > 160) {
      throw new SyncSecurityError("Missing or oversized Ed25519 signature/public key on remote sync head.");
    }
    const pubBytes = base64ToBytes(pubB64);
    const sigBytes = base64ToBytes(sigB64);
    const sigOk = await verifyEd25519Signature(pubBytes, headSigningBytes(head), sigBytes, {
      forcePureJs: Boolean(opts.forcePureJsEd25519),
    });
    if (!sigOk) {
      throw new SyncSecurityError("Invalid Ed25519 signature on remote sync head.");
    }

    // 3. Peer key pinning gate
    const trustedPeers = Object.assign({}, opts.trustedPeers || {});
    const pinMode = String(opts.pinMode || "tofu").toLowerCase();
    let learnedPeer = null;
    if (Object.prototype.hasOwnProperty.call(trustedPeers, devId)) {
      if (!constantTimeEqualStr(String(trustedPeers[devId]), pubB64)) {
        throw new SyncSecurityError("Pinned Ed25519 key mismatch for peer device '" + devId + "'!");
      }
    } else {
      if (pinMode === "strict" || opts.trustNewPeers === false) {
        throw new SyncSecurityError("Untrusted peer device '" + devId + "' signed remote head.");
      }
      if (Object.keys(trustedPeers).length >= MAX_TRUSTED_PEERS) {
        throw new SyncSecurityError("Maximum trusted sync peers (" + MAX_TRUSTED_PEERS + ") exceeded.");
      }
      learnedPeer = { deviceId: devId, pubkeyB64: pubB64 };
    }

    // 4. Monotonic anti-rollback gate
    const remoteEpoch = Number(head.epoch);
    const lastEpoch = Number(opts.lastEpoch || 0);
    if (!Number.isInteger(remoteEpoch) || remoteEpoch < 1) {
      throw new SyncSecurityError("Invalid remote epoch in head.json.");
    }
    if (remoteEpoch < lastEpoch) {
      throw new SyncSecurityError(
        "Anti-rollback check failed: remote epoch " + remoteEpoch + " < local epoch " + lastEpoch + "."
      );
    }

    return {
      valid: true,
      vaultId: vaultId,
      epoch: remoteEpoch,
      manifestId: manifestId,
      deviceId: devId,
      devicePubkeyB64: pubB64,
      learnedPeer: learnedPeer,
    };
  }

  // ─── Append-Only JSONL Deterministic Set-Union Merge ───────────────────────

  function parseSortTimestampKey(rawTs) {
    if (!rawTs) {
      return { rank: 1, num: 0, raw: "" };
    }
    const ms = Date.parse(rawTs);
    if (!Number.isNaN(ms)) {
      return { rank: 0, num: ms / 1000, raw: rawTs };
    }
    return { rank: 0, num: null, raw: rawTs };
  }

  function mergeAppendOnlyJsonl(localBytes, remoteBytes) {
    const decoder = new TextDecoder("utf-8", { fatal: false });
    const byHash = new Map();
    let seqIdx = 0;
    const blobs = [toUint8Array(localBytes), toUint8Array(remoteBytes)];
    for (let b = 0; b < blobs.length; b += 1) {
      const lines = decoder.decode(blobs[b]).split(/\r?\n/);
      for (let l = 0; l < lines.length; l += 1) {
        const stripped = lines[l].trim();
        if (!stripped) {
          continue;
        }
        const digest = sha256HexSync(utf8Bytes(stripped));
        if (byHash.has(digest)) {
          continue;
        }
        let sortTs = "";
        try {
          const parsed = JSON.parse(stripped);
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
            const tsKeys = ["timestamp", "recorded_at", "created_at", "archived_at", "ts"];
            for (let k = 0; k < tsKeys.length; k += 1) {
              const val = parsed[tsKeys[k]];
              if (typeof val === "string" && val.trim()) {
                sortTs = val.trim();
                break;
              }
            }
          }
        } catch (_ignore) {
          // Keep non-JSON lines ordered deterministically by sequence & digest
        }
        byHash.set(digest, {
          tsKey: parseSortTimestampKey(sortTs),
          seqIdx: seqIdx,
          digest: digest,
          line: stripped,
        });
        seqIdx += 1;
      }
    }
    if (byHash.size === 0) {
      return new Uint8Array(0);
    }
    const items = Array.from(byHash.values());
    items.sort(function (a, b) {
      if (a.tsKey.rank !== b.tsKey.rank) {
        return a.tsKey.rank - b.tsKey.rank;
      }
      if (a.tsKey.num !== null && b.tsKey.num !== null && a.tsKey.num !== b.tsKey.num) {
        return a.tsKey.num - b.tsKey.num;
      }
      if (a.tsKey.raw !== b.tsKey.raw) {
        return a.tsKey.raw < b.tsKey.raw ? -1 : 1;
      }
      if (a.seqIdx !== b.seqIdx) {
        return a.seqIdx - b.seqIdx;
      }
      return a.digest < b.digest ? -1 : a.digest > b.digest ? 1 : 0;
    });
    const joined = items.map(function (it) { return it.line; }).join("\n") + "\n";
    return utf8Bytes(joined);
  }

  function isAppendOnlyMergeableJsonl(relPath, entryFlag) {
    if (relPath === "0-raw/archive/vault-ledger.jsonl") {
      return false; // Hash-chained ledger must never reorder lines
    }
    if (entryFlag === true || APPEND_ONLY_JSONL_PATHS.has(relPath)) {
      return true;
    }
    return relPath.startsWith("0-raw/archive/") && relPath.endsWith(".jsonl");
  }

  // ─── Sync Transports (S3 SigV4 / HTTPS / Loopback & InMemory Bundle) ──────

  function awsUriEncodeSegment(seg) {
    return encodeURIComponent(seg).replace(/[!'()*]/g, function (c) {
      return "%" + c.charCodeAt(0).toString(16).toUpperCase();
    });
  }

  function validateEndpointUrl(rawUrl) {
    let parsed;
    try {
      parsed = new URL(String(rawUrl || "").trim().replace(/\/+$/, ""));
    } catch (_err) {
      throw new SyncSecurityError("Invalid HTTP sync endpoint URL: " + JSON.stringify(rawUrl));
    }
    const isLoopback =
      parsed.protocol === "http:" &&
      (parsed.hostname === "127.0.0.1" || parsed.hostname === "[::1]" || parsed.hostname === "localhost");
    if (parsed.protocol !== "https:" && !isLoopback) {
      throw new SyncSecurityError("Remote sync endpoints MUST use https:// (or http://127.0.0.1 for local tests).");
    }
    if (parsed.username || parsed.password || parsed.search || parsed.hash) {
      throw new SyncSecurityError("Sync endpoint URL must not embed credentials, query strings, or fragments.");
    }
    return parsed;
  }

  async function signS3SigV4Headers(params) {
    const method = String(params.method || "GET").toUpperCase();
    let parsed;
    try {
      parsed = new URL(String(params.url || ""));
    } catch (_err) {
      throw new SyncSecurityError("Invalid S3 request URL.");
    }
    const isLoopback =
      parsed.protocol === "http:" &&
      (parsed.hostname === "127.0.0.1" || parsed.hostname === "[::1]" || parsed.hostname === "localhost");
    if (parsed.protocol !== "https:" && !isLoopback) {
      throw new SyncSecurityError("S3 SigV4 URL must use https:// (or loopback http://127.0.0.1).");
    }
    if (parsed.username || parsed.password || parsed.hash) {
      throw new SyncSecurityError("S3 request URL must not contain embedded credentials or fragments.");
    }

    const accessKeyId = String(params.accessKeyId || "").trim();
    const secretAccessKey = String(params.secretAccessKey || "").trim();
    if (!accessKeyId || !secretAccessKey) {
      throw new SyncSecurityError("S3 SigV4 accessKeyId and secretAccessKey must be non-empty.");
    }
    const region = String(params.region || "auto").trim();
    if (!S3_REGION_RE.test(region)) {
      throw new SyncSecurityError("Invalid S3 region identifier: " + JSON.stringify(region));
    }
    const service = String(params.service || "s3").trim();

    const payloadBytes = params.payloadBytes ? toUint8Array(params.payloadBytes) : new Uint8Array(0);
    const payloadHash = await sha256Hex(payloadBytes);
    const amzDate = params.amzDate || new Date().toISOString().replace(/[:-]|\.\d{3}/g, "");
    if (amzDate.length < 16 || !amzDate.endsWith("Z")) {
      throw new SyncSecurityError("Invalid x-amz-date format.");
    }
    const dateStamp = amzDate.slice(0, 8);
    const hostHeader = parsed.host;

    const canonicalUri =
      (parsed.pathname || "/")
        .split("/")
        .map(function (s) {
          return awsUriEncodeSegment(decodeURIComponent(s));
        })
        .join("/") || "/";

    const queryPairs = [];
    if (parsed.search && parsed.search.length > 1) {
      const rawParts = parsed.search.slice(1).split("&");
      for (let i = 0; i < rawParts.length; i += 1) {
        if (!rawParts[i]) continue;
        const eq = rawParts[i].indexOf("=");
        if (eq >= 0) {
          queryPairs.push([
            awsUriEncodeSegment(decodeURIComponent(rawParts[i].slice(0, eq))),
            awsUriEncodeSegment(decodeURIComponent(rawParts[i].slice(eq + 1))),
          ]);
        } else {
          queryPairs.push([awsUriEncodeSegment(decodeURIComponent(rawParts[i])), ""]);
        }
      }
    }
    queryPairs.sort(function (a, b) {
      if (a[0] !== b[0]) return a[0] < b[0] ? -1 : 1;
      return a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0;
    });
    const canonicalQueryString = queryPairs.map(function (kv) { return kv[0] + "=" + kv[1]; }).join("&");

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
    for (let i = 0; i < sortedKeys.length; i += 1) {
      const k = sortedKeys[i];
      canonicalHeaders += k + ":" + signable[k].trim().replace(/\s+/g, " ") + "\n";
    }
    const signedHeaders = sortedKeys.join(";");

    const canonicalRequest = [
      method,
      canonicalUri,
      canonicalQueryString,
      canonicalHeaders,
      signedHeaders,
      payloadHash,
    ].join("\n");

    const credentialScope = dateStamp + "/" + region + "/" + service + "/aws4_request";
    const stringToSign = [
      "AWS4-HMAC-SHA256",
      amzDate,
      credentialScope,
      await sha256Hex(utf8Bytes(canonicalRequest)),
    ].join("\n");

    const kDate = await hmacSha256Bytes(utf8Bytes("AWS4" + secretAccessKey), utf8Bytes(dateStamp));
    const kRegion = await hmacSha256Bytes(kDate, utf8Bytes(region));
    const kService = await hmacSha256Bytes(kRegion, utf8Bytes(service));
    const kSigning = await hmacSha256Bytes(kService, utf8Bytes("aws4_request"));
    const signature = bytesToHex(await hmacSha256Bytes(kSigning, utf8Bytes(stringToSign)));

    const headers = {
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
      headers["x-amz-security-token"] = String(params.sessionToken).trim();
    }
    if (method === "PUT" || method === "POST") {
      headers["Content-Type"] = "application/octet-stream";
    }
    if (params.includeHostHeader) {
      headers.Host = hostHeader;
    }
    return { url: parsed.toString(), headers: headers };
  }

  class HttpObjectSyncTransport {
    constructor(options) {
      const opts = options || {};
      const parsed = validateEndpointUrl(opts.endpointUrl);
      this.endpointUrl = parsed.toString().replace(/\/+$/, "");
      this.authMode = String(opts.authMode || (opts.accessKeyId ? "s3_sigv4" : opts.bearerToken ? "bearer" : "none"))
        .trim()
        .toLowerCase();
      if (this.authMode !== "none" && this.authMode !== "bearer" && this.authMode !== "s3_sigv4") {
        throw new SyncSecurityError("Unsupported HTTP sync authMode: " + JSON.stringify(this.authMode));
      }
      this.bearerToken = opts.bearerToken ? String(opts.bearerToken).trim() : "";
      this.region = String(opts.region || "auto").trim();
      this.accessKeyId = opts.accessKeyId ? String(opts.accessKeyId).trim() : "";
      this.secretAccessKey = opts.secretAccessKey ? String(opts.secretAccessKey).trim() : "";
      this.sessionToken = opts.sessionToken ? String(opts.sessionToken).trim() : "";
      this.fetchImpl = opts.fetchImpl || (typeof globalThis.fetch === "function" ? globalThis.fetch.bind(globalThis) : null);
      if (!this.fetchImpl) {
        throw new SyncError("fetch implementation is unavailable in this runtime.");
      }
    }

    _urlFor(key) {
      const clean = validateBackendKey(key);
      return this.endpointUrl + "/" + clean;
    }

    async _headersFor(method, url, payloadBytes) {
      if (this.authMode === "s3_sigv4") {
        const signed = await signS3SigV4Headers({
          method: method,
          url: url,
          payloadBytes: payloadBytes,
          accessKeyId: this.accessKeyId,
          secretAccessKey: this.secretAccessKey,
          sessionToken: this.sessionToken,
          region: this.region,
        });
        return signed.headers;
      }
      const hdrs = {};
      if (this.authMode === "bearer") {
        if (!this.bearerToken) {
          throw new SyncSecurityError("Bearer authMode configured without bearerToken.");
        }
        hdrs.Authorization = "Bearer " + this.bearerToken;
      }
      if (method === "PUT") {
        hdrs["Content-Type"] = "application/octet-stream";
      }
      return hdrs;
    }

    async getObject(key, options) {
      const maxBytes = (options && options.maxBytes) || (MAX_SYNC_FILE_BYTES + 4096);
      const url = this._urlFor(key);
      const headers = await this._headersFor("GET", url, new Uint8Array(0));
      const resp = await this.fetchImpl(url, {
        method: "GET",
        headers: headers,
        redirect: "error",
        credentials: "omit",
        cache: "no-store",
      });
      if (resp.status === 404) {
        return null;
      }
      if (resp.status >= 300 && resp.status < 400) {
        throw new SyncSecurityError("HTTP redirects are forbidden on sync object endpoints.");
      }
      if (!resp.ok) {
        throw new SyncError("HTTP sync GET '" + key + "' failed with status " + resp.status + ".");
      }
      const clen = resp.headers && resp.headers.get ? resp.headers.get("content-length") : null;
      if (clen !== null && Number(clen) > maxBytes) {
        throw new SyncSecurityError("Remote sync object '" + key + "' exceeds max size limit.");
      }
      const buf = await resp.arrayBuffer();
      if (buf.byteLength > maxBytes) {
        throw new SyncSecurityError("Remote sync object '" + key + "' exceeds max size limit.");
      }
      return new Uint8Array(buf);
    }

    async putObject(key, dataBytes) {
      const url = this._urlFor(key);
      const payload = toUint8Array(dataBytes);
      const headers = await this._headersFor("PUT", url, payload);
      const resp = await this.fetchImpl(url, {
        method: "PUT",
        headers: headers,
        body: payload,
        redirect: "error",
        credentials: "omit",
      });
      if (!resp.ok) {
        throw new SyncError("HTTP sync PUT '" + key + "' failed with status " + resp.status + ".");
      }
    }
  }

  class InMemoryOrBundleBackend {
    constructor(initialObjects) {
      this._objects = new Map();
      if (initialObjects) {
        if (initialObjects instanceof Map) {
          for (const [k, v] of initialObjects.entries()) {
            this.putObjectSync(k, v);
          }
        } else if (typeof initialObjects === "object") {
          const keys = Object.keys(initialObjects);
          for (let i = 0; i < keys.length; i += 1) {
            this.putObjectSync(keys[i], initialObjects[keys[i]]);
          }
        }
      }
    }

    putObjectSync(key, bytes) {
      const clean = validateBackendKey(key);
      const u8 = typeof bytes === "string" ? utf8Bytes(bytes) : new Uint8Array(toUint8Array(bytes));
      this._objects.set(clean, u8);
    }

    async getObject(key, options) {
      const clean = validateBackendKey(key);
      const val = this._objects.get(clean);
      if (!val) {
        return null;
      }
      const maxBytes = (options && options.maxBytes) || (MAX_SYNC_FILE_BYTES + 4096);
      if (val.length > maxBytes) {
        throw new SyncSecurityError("Object '" + clean + "' exceeds max size limit.");
      }
      return new Uint8Array(val);
    }

    async putObject(key, bytes) {
      this.putObjectSync(key, bytes);
    }

    async listObjects(prefix) {
      const pref = String(prefix || "").replace(/^\/+|\/+$/g, "");
      const out = [];
      for (const k of this._objects.keys()) {
        if (!pref || k === pref || k.startsWith(pref + "/")) {
          out.push(k);
        }
      }
      out.sort();
      return out;
    }
  }

  // ─── Full Verified E2EE Pull Engine (pullVaultFromBackend) ─────────────────

  function normalizeFilesMap(rawFiles) {
    const out = {};
    if (!rawFiles) {
      return out;
    }
    if (rawFiles instanceof Map) {
      for (const [k, v] of rawFiles.entries()) {
        out[k] = typeof v === "string" ? utf8Bytes(v) : toUint8Array(v);
      }
    } else if (typeof rawFiles === "object") {
      const keys = Object.keys(rawFiles);
      for (let i = 0; i < keys.length; i += 1) {
        const v = rawFiles[keys[i]];
        if (v !== null && v !== undefined) {
          out[keys[i]] = typeof v === "string" ? utf8Bytes(v) : toUint8Array(v);
        }
      }
    }
    return out;
  }

  async function resolveSyncMasterKey(headerDoc, syncConfig) {
    if (syncConfig && syncConfig.masterKeyBytes) {
      const mk = toUint8Array(syncConfig.masterKeyBytes);
      if (mk.length !== KEY_BYTES) {
        throw new SyncSecurityError("masterKeyBytes must be 32 bytes.");
      }
      return mk;
    }
    if (syncConfig && (syncConfig.masterKeyB64 || syncConfig.recoveryKeyB64)) {
      const mk = base64ToBytes(syncConfig.masterKeyB64 || syncConfig.recoveryKeyB64);
      if (mk.length !== KEY_BYTES) {
        throw new SyncSecurityError("Base64 master/recovery key must decode to 32 bytes.");
      }
      return mk;
    }
    const kdfMode = String((headerDoc && (headerDoc.kdf_mode || headerDoc.kdf)) || "").toLowerCase();
    if (kdfMode === "passphrase" || kdfMode === "scrypt") {
      const passphrase = syncConfig ? syncConfig.passphrase : "";
      const saltB64 = (headerDoc && headerDoc.kdf_salt_b64) || (syncConfig && syncConfig.kdfSaltB64);
      if (!passphrase || !saltB64) {
        throw new SyncSecurityError("Passphrase and kdf_salt_b64 are required to unlock this passphrase-encrypted vault.");
      }
      return deriveMasterKeyFromPassphrase(passphrase, base64ToBytes(saltB64), headerDoc && headerDoc.scrypt_params);
    }
    throw new SyncSecurityError("Master key or passphrase is required to unlock E2EE sync target.");
  }

  async function pullVaultFromBackend(backend, syncConfig, localState) {
    const cfg = syncConfig || {};
    const prevState = localState || {};
    const prevFiles = normalizeFilesMap(prevState.files);
    const prevCursor = prevState.syncCursor || {};
    const lastEpoch = Number(prevCursor.epoch || prevCursor.lastSeenEpoch || 0);
    const baseFileShas = Object.assign({}, prevCursor.baseFileShas || {});
    const trustedPeers = Object.assign({}, prevCursor.trusted_peers || prevCursor.trustedPeers || cfg.trustedPeers || {});

    const rawHeader = await backend.getObject("vault-header.json", { maxBytes: MAX_SYNC_HEADER_BYTES });
    if (!rawHeader) {
      throw new SyncError("No vault-header.json found on sync backend.");
    }
    let headerDoc;
    try {
      headerDoc = JSON.parse(utf8DecodeStrict(rawHeader));
    } catch (_err) {
      throw new SyncSecurityError("Malformed JSON in remote vault-header.json.");
    }
    const vaultId = String(headerDoc.vault_id || "").trim().toLowerCase();
    if (!HEX_ID_RE.test(vaultId)) {
      throw new SyncSecurityError("Invalid vault_id in vault-header.json.");
    }
    if (cfg.vaultId && vaultId !== String(cfg.vaultId).trim().toLowerCase()) {
      throw new SyncSecurityError("Remote vault-header.json vault_id mismatch against paired vault_id.");
    }

    const masterKey = await resolveSyncMasterKey(headerDoc, cfg);

    // Verify encrypted key_check_b64 in vault-header.json when present
    if (headerDoc.key_check_b64) {
      const kcBytes = await decryptSyncObject(masterKey, {
        vaultId: vaultId,
        kind: "header",
        objectId: "key-check",
        envelopeBytes: base64ToBytes(headerDoc.key_check_b64),
      });
      const kcText = utf8DecodeStrict(kcBytes);
      if (kcText !== "second-brain-e2ee-v1:" + vaultId) {
        throw new SyncSecurityError("Master key check failed against remote vault-header.json.");
      }
    }

    const rawHead = await backend.getObject("head.json", { maxBytes: MAX_SYNC_HEADER_BYTES });
    if (!rawHead) {
      return {
        status: "empty_remote",
        vaultId: vaultId,
        epoch: lastEpoch,
        manifestId: prevCursor.manifest_id || null,
        downloadedBlobs: 0,
        reusedBlobs: 0,
        added: 0,
        updated: 0,
        deleted: 0,
        mergedJsonl: 0,
        files: prevFiles,
        syncCursor: {
          epoch: lastEpoch,
          manifest_id: prevCursor.manifest_id || null,
          trusted_peers: trustedPeers,
          baseFileShas: baseFileShas,
        },
      };
    }

    let head;
    try {
      head = JSON.parse(utf8DecodeStrict(rawHead));
    } catch (_err) {
      throw new SyncSecurityError("Malformed JSON in remote head.json.");
    }

    const defaultPinMode = Object.keys(trustedPeers).length > 0 ? "strict" : "tofu";
    const headVerified = await verifyRemoteSyncHead(head, masterKey, {
      vaultId: vaultId,
      trustedPeers: trustedPeers,
      lastEpoch: lastEpoch,
      pinMode: cfg.pinMode || defaultPinMode,
      trustNewPeers: cfg.trustNewPeers,
      forcePureJsEd25519: cfg.forcePureJsEd25519,
    });

    const manifestId = headVerified.manifestId;
    const manifestCt = await backend.getObject("manifests/" + manifestId + ".enc", {
      maxBytes: MAX_SYNC_MANIFEST_BYTES,
    });
    if (!manifestCt) {
      throw new SyncSecurityError("Remote manifest '" + manifestId + "' is missing.");
    }
    const actualManifestCtSha = await sha256Hex(manifestCt);
    if (!constantTimeEqualStr(actualManifestCtSha, String(head.manifest_ciphertext_sha256).toLowerCase())) {
      throw new SyncSecurityError("Remote manifest ciphertext SHA-256 does not match signed head.json.");
    }

    const manifestPlainBytes = await decryptSyncObject(masterKey, {
      vaultId: vaultId,
      kind: "manifest",
      objectId: manifestId,
      envelopeBytes: manifestCt,
    });
    let manifest;
    try {
      manifest = JSON.parse(utf8DecodeStrict(manifestPlainBytes));
    } catch (_err) {
      throw new SyncSecurityError("Malformed JSON inside decrypted sync manifest.");
    }
    if (
      String(manifest.vault_id || "").toLowerCase() !== vaultId ||
      Number(manifest.epoch) !== headVerified.epoch
    ) {
      throw new SyncSecurityError("Decrypted manifest vault_id/epoch disagrees with signed head.json.");
    }
    const remoteFiles = manifest.files;
    if (!remoteFiles || typeof remoteFiles !== "object" || Array.isArray(remoteFiles)) {
      throw new SyncSecurityError("Malformed files table in decrypted sync manifest.");
    }

    // Persist TOFU peer key only AFTER manifest AEAD verification succeeds
    if (headVerified.learnedPeer) {
      trustedPeers[headVerified.learnedPeer.deviceId] = headVerified.learnedPeer.pubkeyB64;
    }

    // Pre-compute local file SHA-256 hashes for incremental deduplication
    const localShaMap = {};
    const prevRelPaths = Object.keys(prevFiles);
    for (let i = 0; i < prevRelPaths.length; i += 1) {
      const rp = prevRelPaths[i];
      localShaMap[rp] = await sha256Hex(prevFiles[rp]);
    }

    const remotePaths = Object.keys(remoteFiles).sort();
    const verifiedRemoteBlobs = {};
    let downloadedBlobs = 0;
    let reusedBlobs = 0;

    // Phase 1: Validate all paths, blind blob IDs, and decrypt any changed/new blobs BEFORE mutating state
    for (let i = 0; i < remotePaths.length; i += 1) {
      const rawRel = remotePaths[i];
      const cleanRel = validateSyncRelPath(rawRel);
      const entry = remoteFiles[rawRel];
      if (!entry || typeof entry !== "object") {
        throw new SyncSecurityError("Invalid manifest entry for " + cleanRel + ".");
      }
      const remoteSha = String(entry.sha256 || "").trim().toLowerCase();
      const blobId = String(entry.blob_id || "").trim().toLowerCase();
      if (!HEX64_RE.test(remoteSha) || !HEX_ID_RE.test(blobId)) {
        throw new SyncSecurityError("Malformed hashes in manifest for " + cleanRel + ".");
      }
      const expectedBlobId = await computeBlindBlobId(masterKey, vaultId, remoteSha);
      if (!constantTimeEqualStr(blobId, expectedBlobId)) {
        throw new SyncSecurityError("Blind blob_id mismatch for " + cleanRel + " (manifest tampering detected).");
      }

      if (localShaMap[cleanRel] === remoteSha) {
        verifiedRemoteBlobs[cleanRel] = {
          bytes: prevFiles[cleanRel],
          sha256: remoteSha,
          reused: true,
          appendOnlyJsonl: Boolean(entry.append_only_jsonl),
        };
        reusedBlobs += 1;
        continue;
      }

      const shardedKey = "objects/" + blobId.slice(0, 2) + "/" + blobId + ".enc";
      let blobCt = await backend.getObject(shardedKey, { maxBytes: MAX_SYNC_FILE_BYTES + 4096 });
      if (!blobCt) {
        blobCt = await backend.getObject("objects/" + blobId + ".enc", { maxBytes: MAX_SYNC_FILE_BYTES + 4096 });
      }
      if (!blobCt) {
        throw new SyncSecurityError("Referenced encrypted blob missing on remote store for " + cleanRel + ".");
      }
      const plainBytes = await decryptSyncObject(masterKey, {
        vaultId: vaultId,
        kind: "blob",
        objectId: blobId,
        envelopeBytes: blobCt,
      });
      const actualPlainSha = await sha256Hex(plainBytes);
      if (actualPlainSha !== remoteSha) {
        throw new SyncSecurityError("Decrypted plaintext SHA-256 mismatch for " + cleanRel + ".");
      }
      if (entry.size !== undefined && Number(entry.size) !== plainBytes.length) {
        throw new SyncSecurityError("Decrypted plaintext byte-length mismatch for " + cleanRel + ".");
      }
      verifiedRemoteBlobs[cleanRel] = {
        bytes: plainBytes,
        sha256: remoteSha,
        reused: false,
        appendOnlyJsonl: Boolean(entry.append_only_jsonl),
      };
      downloadedBlobs += 1;
    }

    // Phase 2: Atomic state transition with deterministic set-union JSONL merge
    const nextFiles = Object.assign({}, prevFiles);
    const nextBaseShas = Object.assign({}, baseFileShas);
    let added = 0;
    let updated = 0;
    let deleted = 0;
    let mergedJsonl = 0;

    for (let i = 0; i < remotePaths.length; i += 1) {
      const cleanRel = validateSyncRelPath(remotePaths[i]);
      const incoming = verifiedRemoteBlobs[cleanRel];
      const existingBytes = prevFiles[cleanRel];
      const localSha = localShaMap[cleanRel];
      const baseSha = baseFileShas[cleanRel];

      if (!existingBytes) {
        nextFiles[cleanRel] = incoming.bytes;
        nextBaseShas[cleanRel] = incoming.sha256;
        added += 1;
        continue;
      }
      if (localSha === incoming.sha256) {
        nextBaseShas[cleanRel] = incoming.sha256;
        continue;
      }
      if (!baseSha || baseSha === localSha) {
        if (!baseSha && isAppendOnlyMergeableJsonl(cleanRel, incoming.appendOnlyJsonl)) {
          const merged = mergeAppendOnlyJsonl(existingBytes, incoming.bytes);
          nextFiles[cleanRel] = merged;
          nextBaseShas[cleanRel] = await sha256Hex(merged);
          mergedJsonl += 1;
        } else {
          nextFiles[cleanRel] = incoming.bytes;
          nextBaseShas[cleanRel] = incoming.sha256;
          updated += 1;
        }
        continue;
      }
      if (baseSha === incoming.sha256) {
        // Local ahead, remote unchanged
        continue;
      }
      if (isAppendOnlyMergeableJsonl(cleanRel, incoming.appendOnlyJsonl)) {
        const merged = mergeAppendOnlyJsonl(existingBytes, incoming.bytes);
        nextFiles[cleanRel] = merged;
        nextBaseShas[cleanRel] = await sha256Hex(merged);
        mergedJsonl += 1;
      } else {
        // Mobile read-replica fast-forwards canonical files while preserving divergent local working copy in notes
        nextFiles[cleanRel] = incoming.bytes;
        nextBaseShas[cleanRel] = incoming.sha256;
        updated += 1;
      }
    }

    // Propagate remote deletions for unmodified tracked files
    const trackedBasePaths = Object.keys(baseFileShas);
    for (let i = 0; i < trackedBasePaths.length; i += 1) {
      const prevRel = trackedBasePaths[i];
      if (Object.prototype.hasOwnProperty.call(verifiedRemoteBlobs, prevRel) || APPEND_ONLY_JSONL_PATHS.has(prevRel)) {
        continue;
      }
      if (prevFiles[prevRel] && localShaMap[prevRel] === baseFileShas[prevRel]) {
        delete nextFiles[prevRel];
        delete nextBaseShas[prevRel];
        deleted += 1;
      }
    }

    return {
      status: "pulled",
      vaultId: vaultId,
      epoch: headVerified.epoch,
      manifestId: manifestId,
      remoteDeviceId: headVerified.deviceId,
      downloadedBlobs: downloadedBlobs,
      reusedBlobs: reusedBlobs,
      added: added,
      updated: updated,
      deleted: deleted,
      mergedJsonl: mergedJsonl,
      files: nextFiles,
      syncCursor: {
        epoch: headVerified.epoch,
        manifest_id: manifestId,
        trusted_peers: trustedPeers,
        baseFileShas: nextBaseShas,
      },
    };
  }

  // ─── .sbvault Backup Decryption, Gunzip, USTAR/PAX Tar & Merkle Verifier ──

  async function gunzipBytes(compressedBytes, maxOutputBytes) {
    const comp = toUint8Array(compressedBytes);
    const limit = maxOutputBytes || (MAX_EXTRACTED_TOTAL_BYTES + 16 * 1024 * 1024);

    if (typeof DecompressionStream === "function") {
      const ds = new DecompressionStream("gzip");
      const writer = ds.writable.getWriter();
      const writePromise = writer.write(comp).then(function () {
        return writer.close();
      });
      const reader = ds.readable.getReader();
      const chunks = [];
      let total = 0;
      try {
        while (true) {
          const step = await reader.read();
          if (step.done) break;
          const chunk = toUint8Array(step.value);
          total += chunk.length;
          if (total > limit) {
            throw new VaultBackupError("Archive uncompressed stream exceeds safety limit.");
          }
          chunks.push(chunk);
        }
        await writePromise;
      } catch (err) {
        if (err instanceof VaultBackupError) throw err;
        throw new VaultBackupError("Corrupt gzip stream in .sbvault archive: " + err.message);
      }
      return concatBytes(chunks);
    }

    if (typeof require === "function") {
      // Fallback for older embedded JS runtimes without DecompressionStream
      const zlib = require("zlib");
      try {
        const buf = zlib.gunzipSync(comp, { maxOutputLength: limit });
        return new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
      } catch (err) {
        throw new VaultBackupError("Corrupt or oversized gzip stream in .sbvault archive: " + err.message);
      }
    }

    throw new VaultBackupError("Gzip decompression is unavailable in this environment.");
  }

  function readTarNullTermAscii(block, start, len) {
    let end = start;
    const max = start + len;
    while (end < max && block[end] !== 0) {
      end += 1;
    }
    let out = "";
    for (let i = start; i < end; i += 1) {
      out += String.fromCharCode(block[i]);
    }
    return out;
  }

  function parseTarOctalOrBase256(block, start, len) {
    if ((block[start] & 0x80) !== 0) {
      let val = 0;
      for (let i = start + 1; i < start + len; i += 1) {
        val = val * 256 + block[i];
      }
      return val;
    }
    const str = readTarNullTermAscii(block, start, len).trim();
    if (!str) return 0;
    if (!/^[0-7]+$/.test(str)) {
      throw new VaultBackupError("Invalid octal field in tar header.");
    }
    return parseInt(str, 8);
  }

  function parsePaxRecords(dataBytes) {
    const u8 = toUint8Array(dataBytes);
    const out = {};
    let pos = 0;
    while (pos < u8.length) {
      let spacePos = pos;
      while (spacePos < u8.length && u8[spacePos] !== 0x20) {
        spacePos += 1;
      }
      if (spacePos >= u8.length) {
        throw new VaultBackupError("Malformed PAX extended header record.");
      }
      const lenStr = readTarNullTermAscii(u8, pos, spacePos - pos);
      const recByteLen = parseInt(lenStr, 10);
      if (!Number.isInteger(recByteLen) || recByteLen <= (spacePos - pos + 2) || pos + recByteLen > u8.length) {
        throw new VaultBackupError("Invalid PAX record length.");
      }
      const bodyEnd = u8[pos + recByteLen - 1] === 0x0a ? pos + recByteLen - 1 : pos + recByteLen;
      const kv = utf8DecodeStrict(u8.subarray(spacePos + 1, bodyEnd));
      const eqIdx = kv.indexOf("=");
      if (eqIdx > 0) {
        out[kv.slice(0, eqIdx)] = kv.slice(eqIdx + 1);
      }
      pos += recByteLen;
    }
    return out;
  }

  function isAllZeroBlock(block) {
    for (let i = 0; i < block.length; i += 1) {
      if (block[i] !== 0) return false;
    }
    return true;
  }

  function parseTarArchive(tarBytes) {
    const buf = toUint8Array(tarBytes);
    const extracted = {};
    let manifest = null;
    let totalExtracted = 0;
    let offset = 0;
    let globalPax = {};
    let nextPax = null;

    while (offset + 512 <= buf.length) {
      const header = buf.subarray(offset, offset + 512);
      offset += 512;

      if (isAllZeroBlock(header)) {
        break;
      }

      // Verify USTAR 512-byte header checksum
      const expectedChecksum = parseTarOctalOrBase256(header, 148, 8);
      let unsignedSum = 0;
      let signedSum = 0;
      for (let i = 0; i < 512; i += 1) {
        const b = i >= 148 && i < 156 ? 0x20 : header[i];
        unsignedSum += b;
        signedSum += b > 127 ? b - 256 : b;
      }
      if (expectedChecksum !== unsignedSum && expectedChecksum !== signedSum) {
        throw new VaultBackupError("Tar header checksum verification failed.");
      }

      const typeByte = header[156];
      const typeFlag = typeByte === 0 ? "0" : String.fromCharCode(typeByte);
      let memberSize = parseTarOctalOrBase256(header, 124, 12);
      const nameField = readTarNullTermAscii(header, 0, 100);
      const magicField = readTarNullTermAscii(header, 257, 6);
      const prefixField = magicField.startsWith("ustar") ? readTarNullTermAscii(header, 345, 155) : "";

      if (typeFlag === "x" || typeFlag === "g") {
        if (memberSize < 0 || memberSize > 1048576 || offset + memberSize > buf.length) {
          throw new VaultBackupError("Invalid PAX header size.");
        }
        const paxData = buf.subarray(offset, offset + memberSize);
        offset += Math.ceil(memberSize / 512) * 512;
        const parsedPax = parsePaxRecords(paxData);
        if (typeFlag === "g") {
          globalPax = Object.assign({}, globalPax, parsedPax);
        } else {
          nextPax = parsedPax;
        }
        continue;
      }

      const pax = Object.assign({}, globalPax, nextPax || {});
      nextPax = null;

      if (pax.linkpath) {
        throw new VaultBackupSecurityError("Archive contains PAX linkpath member.");
      }
      if (typeFlag !== "0") {
        throw new VaultBackupSecurityError(
          "Archive contains non-regular/symlink tar member (typeflag=" + JSON.stringify(typeFlag) + ")."
        );
      }

      if (pax.size !== undefined) {
        memberSize = parseInt(String(pax.size), 10);
      }
      if (!Number.isInteger(memberSize) || memberSize < 0 || memberSize > MAX_SINGLE_MEMBER_BYTES) {
        throw new VaultBackupError("Archive member exceeds single-file size bounds.");
      }
      totalExtracted += memberSize;
      if (totalExtracted > MAX_EXTRACTED_TOTAL_BYTES) {
        throw new VaultBackupError("Archive uncompressed payload exceeds safety limit.");
      }
      if (offset + memberSize > buf.length) {
        throw new VaultBackupError("Truncated tar stream.");
      }

      const rawName = pax.path !== undefined ? String(pax.path) : (prefixField ? prefixField + "/" + nameField : nameField);
      const cleanName = validateBackupRelPath(rawName);
      const contentBytes = new Uint8Array(buf.subarray(offset, offset + memberSize));
      offset += Math.ceil(memberSize / 512) * 512;

      if (cleanName === "backup-manifest.json") {
        if (manifest !== null) {
          throw new VaultBackupSecurityError("Duplicate backup-manifest.json in archive.");
        }
        try {
          manifest = JSON.parse(utf8DecodeStrict(contentBytes));
        } catch (_err) {
          throw new VaultBackupError("Malformed backup-manifest.json inside archive.");
        }
      } else {
        if (Object.prototype.hasOwnProperty.call(extracted, cleanName)) {
          throw new VaultBackupSecurityError("Duplicate archive member path rejected: " + cleanName);
        }
        extracted[cleanName] = contentBytes;
      }
    }

    return { manifest: manifest, files: extracted };
  }

  function computeMerkleRootSync(leafDigests) {
    if (!leafDigests || leafDigests.length === 0) {
      return sha256HexSync(utf8Bytes("empty-vault-merkle-root"));
    }
    let cur = leafDigests.slice();
    while (cur.length > 1) {
      const nxt = [];
      for (let i = 0; i < cur.length; i += 2) {
        if (i + 1 < cur.length) {
          nxt.push(sha256HexSync(utf8Bytes("node:" + cur[i] + ":" + cur[i + 1])));
        } else {
          nxt.push(sha256HexSync(utf8Bytes("odd:" + cur[i])));
        }
      }
      cur = nxt;
    }
    return cur[0];
  }

  async function deriveBackupAeadKey(passphrase, saltBytes, n, r, p) {
    const ikm = scrypt(utf8Bytes(passphrase), saltBytes, n, r, p, KEY_BYTES);
    return hkdfSha256(ikm, saltBytes, utf8Bytes("sb-vault-backup-v1|aes-256-gcm"), KEY_BYTES);
  }

  async function importSbVaultArchive(archiveBytes, passphrase) {
    const raw = toUint8Array(archiveBytes);
    if (raw.length < BACKUP_MAGIC.length + 4 || raw.length > MAX_BACKUP_ARCHIVE_BYTES) {
      throw new VaultBackupError("Invalid or oversized .sbvault archive (" + raw.length + " bytes).");
    }
    if (!constantTimeEqualBytes(raw.subarray(0, BACKUP_MAGIC.length), BACKUP_MAGIC)) {
      throw new VaultBackupError("Unrecognized backup file header (expected SBVBKP1 magic).");
    }
    const dv = new DataView(raw.buffer, raw.byteOffset, raw.byteLength);
    const hdrLen = dv.getUint32(8, false);
    if (hdrLen <= 2 || hdrLen > 65536 || 12 + hdrLen > raw.length) {
      throw new VaultBackupError("Malformed .sbvault outer header length.");
    }
    const headerBytes = raw.subarray(12, 12 + hdrLen);
    const payloadBytes = raw.subarray(12 + hdrLen);

    let outer;
    try {
      outer = JSON.parse(utf8DecodeStrict(headerBytes));
    } catch (_err) {
      throw new VaultBackupError("Malformed .sbvault outer JSON header.");
    }
    if (!outer || typeof outer !== "object" || outer.schema_version !== BACKUP_SCHEMA_VERSION) {
      throw new VaultBackupError("Unsupported .sbvault outer header schema_version.");
    }

    const encrypted = outer.encrypted !== false;
    let tarGzBytes;
    if (encrypted) {
      if (typeof passphrase !== "string" || !passphrase) {
        throw new VaultBackupError("Passphrase is required to decrypt this encrypted .sbvault archive.");
      }
      let salt, nonce;
      try {
        salt = base64ToBytes(outer.salt_b64);
        nonce = base64ToBytes(outer.nonce_b64);
      } catch (_err) {
        throw new VaultBackupError("Invalid salt/nonce encoding in .sbvault header.");
      }
      const nParam = Number(outer.scrypt_n || SCRYPT_DEFAULT_N);
      const rParam = Number(outer.scrypt_r || SCRYPT_DEFAULT_R);
      const pParam = Number(outer.scrypt_p || SCRYPT_DEFAULT_P);
      if (
        salt.length !== 16 ||
        nonce.length !== 12 ||
        !Number.isInteger(nParam) ||
        nParam < 16384 ||
        nParam > (1 << 20) ||
        (nParam & (nParam - 1)) !== 0 ||
        !Number.isInteger(rParam) ||
        rParam < 1 ||
        rParam > 16 ||
        !Number.isInteger(pParam) ||
        pParam < 1 ||
        pParam > 16 ||
        128 * nParam * rParam * pParam > 256 * 1024 * 1024
      ) {
        throw new VaultBackupError("Invalid salt/nonce/scrypt work factor in .sbvault header.");
      }
      const key = await deriveBackupAeadKey(passphrase, salt, nParam, rParam, pParam);
      tarGzBytes = await aes256GcmDecrypt(key, nonce, payloadBytes, headerBytes, VaultBackupError);
    } else {
      const expectedSha = String(outer.payload_sha256 || "").toLowerCase();
      const actualSha = await sha256Hex(payloadBytes);
      if (!expectedSha || expectedSha !== actualSha) {
        throw new VaultBackupError("Unencrypted .sbvault payload SHA-256 mismatch.");
      }
      tarGzBytes = payloadBytes;
    }

    const tarBytes = await gunzipBytes(tarGzBytes);
    const parsedTar = parseTarArchive(tarBytes);
    const manifest = parsedTar.manifest;
    const extractedFiles = parsedTar.files;

    if (!manifest || typeof manifest !== "object" || manifest.schema_version !== BACKUP_SCHEMA_VERSION) {
      throw new VaultBackupError("Missing or invalid backup-manifest.json inside archive.");
    }
    const expectedFiles = manifest.files;
    if (!expectedFiles || typeof expectedFiles !== "object" || Array.isArray(expectedFiles)) {
      throw new VaultBackupError("Malformed 'files' map in backup-manifest.json.");
    }

    const extractedKeys = Object.keys(extractedFiles).sort();
    const expectedKeys = Object.keys(expectedFiles).sort();
    if (extractedKeys.length !== expectedKeys.length) {
      throw new VaultBackupError("Archive member count mismatch against backup-manifest.json.");
    }
    for (let i = 0; i < expectedKeys.length; i += 1) {
      if (extractedKeys[i] !== expectedKeys[i]) {
        throw new VaultBackupError("Archive member path mismatch against backup-manifest.json.");
      }
      const rel = expectedKeys[i];
      const spec = expectedFiles[rel];
      const bytes = extractedFiles[rel];
      const actualSha = await sha256Hex(bytes);
      if (!spec || actualSha !== String(spec.sha256 || "").toLowerCase() || bytes.length !== Number(spec.size)) {
        throw new VaultBackupError("SHA-256 or byte-length integrity mismatch for " + rel + ".");
      }
    }

    // Verify Merkle leaves & Merkle root over extracted bytes
    const rawLeaves = manifest.merkle_leaves;
    if (!Array.isArray(rawLeaves)) {
      throw new VaultBackupError("Missing 'merkle_leaves' in backup-manifest.json.");
    }
    const leafHashes = [];
    for (let i = 0; i < rawLeaves.length; i += 1) {
      const leaf = rawLeaves[i];
      if (!leaf || typeof leaf !== "object") {
        throw new VaultBackupError("Malformed Merkle leaf entry in backup-manifest.json.");
      }
      const lRel = String(leaf.rel_path || "");
      const lFsha = String(leaf.file_sha256 || "").toLowerCase();
      const lDigest = String(leaf.leaf_sha256 || "").toLowerCase();
      if (!Object.prototype.hasOwnProperty.call(extractedFiles, lRel)) {
        throw new VaultBackupError("Merkle leaf " + lRel + " missing from archived files.");
      }
      if ((await sha256Hex(extractedFiles[lRel])) !== lFsha) {
        throw new VaultBackupError("Merkle leaf " + lRel + " SHA-256 disagrees with archived file bytes.");
      }
      const expectedLeafDigest = sha256HexSync(utf8Bytes("leaf:" + lRel + ":" + lFsha));
      if (expectedLeafDigest !== lDigest) {
        throw new VaultBackupError("Merkle leaf digest mismatch for " + lRel + ".");
      }
      leafHashes.push(lDigest);
    }
    const recomputedRoot = computeMerkleRootSync(leafHashes);
    if (recomputedRoot !== String(manifest.merkle_root || "").toLowerCase()) {
      throw new VaultBackupError("Merkle root verification failed in .sbvault archive.");
    }

    return {
      valid: true,
      encrypted: encrypted,
      archiveSha256: await sha256Hex(raw),
      vaultName: String(manifest.vault_name || ""),
      createdAt: String(manifest.created_at || ""),
      fileCount: extractedKeys.length,
      merkleRoot: recomputedRoot,
      approvedAtoms: Number(manifest.approved_atoms || 0),
      acceptedPlaybooks: Number(manifest.accepted_playbooks || 0),
      ledgerEntryCount: Number(manifest.ledger_entry_count || 0),
      files: extractedFiles,
      manifest: manifest,
    };
  }

  // ─── EncryptedDeviceStore (At-Rest IndexedDB + Memory Fallback Encryption) ─

  class InMemoryKvBackend {
    constructor() {
      this._map = new Map();
    }
    async get(key) {
      const v = this._map.get(key);
      return v ? new Uint8Array(v) : null;
    }
    async set(key, bytes) {
      this._map.set(key, new Uint8Array(toUint8Array(bytes)));
    }
    async delete(key) {
      this._map.delete(key);
    }
    async clear() {
      this._map.clear();
    }
  }

  class IndexedDbKvBackend {
    constructor(dbName, storeName) {
      this.dbName = dbName || "second_brain_mobile_vault_v1";
      this.storeName = storeName || "encrypted_blobs";
    }

    _open() {
      const self = this;
      return new Promise(function (resolve, reject) {
        const req = globalThis.indexedDB.open(self.dbName, 1);
        req.onupgradeneeded = function () {
          const db = req.result;
          if (!db.objectStoreNames.contains(self.storeName)) {
            db.createObjectStore(self.storeName);
          }
        };
        req.onsuccess = function () {
          resolve(req.result);
        };
        req.onerror = function () {
          reject(req.error || new SyncError("IndexedDB open failed."));
        };
      });
    }

    async get(key) {
      const db = await this._open();
      const storeName = this.storeName;
      return new Promise(function (resolve, reject) {
        const tx = db.transaction(storeName, "readonly");
        const req = tx.objectStore(storeName).get(key);
        req.onsuccess = function () {
          db.close();
          resolve(req.result ? toUint8Array(req.result) : null);
        };
        req.onerror = function () {
          db.close();
          reject(req.error);
        };
      });
    }

    async set(key, bytes) {
      const db = await this._open();
      const storeName = this.storeName;
      const copy = new Uint8Array(toUint8Array(bytes));
      return new Promise(function (resolve, reject) {
        const tx = db.transaction(storeName, "readwrite");
        tx.objectStore(storeName).put(copy, key);
        tx.oncomplete = function () {
          db.close();
          resolve();
        };
        tx.onerror = function () {
          db.close();
          reject(tx.error);
        };
      });
    }

    async delete(key) {
      const db = await this._open();
      const storeName = this.storeName;
      return new Promise(function (resolve, reject) {
        const tx = db.transaction(storeName, "readwrite");
        tx.objectStore(storeName).delete(key);
        tx.oncomplete = function () {
          db.close();
          resolve();
        };
        tx.onerror = function () {
          db.close();
          reject(tx.error);
        };
      });
    }

    async clear() {
      const db = await this._open();
      const storeName = this.storeName;
      return new Promise(function (resolve, reject) {
        const tx = db.transaction(storeName, "readwrite");
        tx.objectStore(storeName).clear();
        tx.oncomplete = function () {
          db.close();
          resolve();
        };
        tx.onerror = function () {
          db.close();
          reject(tx.error);
        };
      });
    }
  }

  async function deriveDeviceLockKey(pin, saltBytes, iterations) {
    if (typeof pin !== "string" || pin.length < 4) {
      throw new SyncSecurityError("Device lock PIN/passphrase must be at least 4 characters.");
    }
    const subtle = getWebCrypto().subtle;
    const baseKey = await subtle.importKey("raw", utf8Bytes(pin), "PBKDF2", false, ["deriveBits"]);
    const bits = await subtle.deriveBits(
      {
        name: "PBKDF2",
        hash: "SHA-256",
        salt: toUint8Array(saltBytes),
        iterations: iterations || DEVICE_PBKDF2_ITERATIONS,
      },
      baseKey,
      256
    );
    return new Uint8Array(bits);
  }

  class EncryptedDeviceStore {
    constructor(options) {
      const opts = options || {};
      this.recordId = String(opts.recordId || "primary_vault");
      this.iterations = Number(opts.iterations || DEVICE_PBKDF2_ITERATIONS);
      if (this.iterations < 100000) {
        throw new SyncSecurityError("PBKDF2 iteration count must be >= 100,000.");
      }
      if (opts.storageBackend) {
        this.backend = opts.storageBackend;
      } else if (typeof globalThis !== "undefined" && globalThis.indexedDB) {
        this.backend = new IndexedDbKvBackend(opts.dbName, opts.storeName);
      } else {
        this.backend = new InMemoryKvBackend();
      }
      this._unlockedState = null;
      this._unlockedKey = null;
      this._unlockedSalt = null;
    }

    isLocked() {
      return this._unlockedState === null;
    }

    getUnlockedState() {
      if (this._unlockedState === null) {
        throw new SyncSecurityError("Device vault is locked. Unlock with PIN first.");
      }
      return this._unlockedState;
    }

    lock() {
      if (this._unlockedKey) {
        this._unlockedKey.fill(0);
      }
      this._unlockedKey = null;
      this._unlockedSalt = null;
      this._unlockedState = null;
    }

    async hasEncryptedSnapshot() {
      const blob = await this.backend.get(this.recordId);
      return Boolean(blob && blob.length > DEVICE_STORE_MAGIC.length + 28);
    }

    async inspectRawCiphertext() {
      return this.backend.get(this.recordId);
    }

    async saveState(pinOrNull, vaultState) {
      const filesNorm = normalizeFilesMap(vaultState.files);
      const serializedFiles = {};
      const relPaths = Object.keys(filesNorm).sort();
      for (let i = 0; i < relPaths.length; i += 1) {
        serializedFiles[relPaths[i]] = bytesToBase64(filesNorm[relPaths[i]]);
      }
      const payloadObj = {
        schema_version: 1,
        saved_at: new Date().toISOString(),
        vault_id: vaultState.vaultId || null,
        sync_cursor: vaultState.syncCursor || {},
        pairing_meta: vaultState.pairingMeta || null,
        files_b64: serializedFiles,
      };
      const plaintextBytes = utf8Bytes(JSON.stringify(payloadObj));

      let salt;
      let key;
      if (pinOrNull) {
        salt = getWebCrypto().getRandomValues(new Uint8Array(16));
        key = await deriveDeviceLockKey(pinOrNull, salt, this.iterations);
      } else if (this._unlockedKey && this._unlockedSalt) {
        salt = this._unlockedSalt;
        key = this._unlockedKey;
      } else {
        throw new SyncSecurityError("PIN is required to encrypt device store.");
      }

      const nonce = getWebCrypto().getRandomValues(new Uint8Array(NONCE_BYTES));
      const iterBytes = new Uint8Array(4);
      new DataView(iterBytes.buffer).setUint32(0, this.iterations, false);
      const aad = utf8Bytes("sb-mobile-device-store-v1|" + this.recordId + "|" + this.iterations);
      const ct = await aes256GcmEncrypt(key, nonce, plaintextBytes, aad);
      const envelope = concatBytes([DEVICE_STORE_MAGIC, iterBytes, salt, nonce, ct]);
      await this.backend.set(this.recordId, envelope);

      this._unlockedKey = key;
      this._unlockedSalt = salt;
      this._unlockedState = {
        vaultId: payloadObj.vault_id,
        syncCursor: payloadObj.sync_cursor,
        pairingMeta: payloadObj.pairing_meta,
        files: filesNorm,
      };
      return { encryptedBytes: envelope.length, fileCount: relPaths.length };
    }

    async unlock(pin) {
      const envelope = await this.backend.get(this.recordId);
      if (!envelope) {
        throw new SyncError("No encrypted vault snapshot stored on this device yet.");
      }
      const minLen = DEVICE_STORE_MAGIC.length + 4 + 16 + NONCE_BYTES + GCM_TAG_BYTES;
      if (envelope.length < minLen || !constantTimeEqualBytes(envelope.subarray(0, 6), DEVICE_STORE_MAGIC)) {
        throw new SyncSecurityError("Corrupted encrypted device store header.");
      }
      const iterations = new DataView(envelope.buffer, envelope.byteOffset, envelope.byteLength).getUint32(6, false);
      if (iterations < 100000 || iterations > 2000000) {
        throw new SyncSecurityError("Unsafe PBKDF2 iteration count in device store header.");
      }
      const salt = envelope.subarray(10, 26);
      const nonce = envelope.subarray(26, 38);
      const ct = envelope.subarray(38);
      const aad = utf8Bytes("sb-mobile-device-store-v1|" + this.recordId + "|" + iterations);
      const key = await deriveDeviceLockKey(pin, salt, iterations);
      const plainBytes = await aes256GcmDecrypt(key, nonce, ct, aad, SyncSecurityError);

      let parsed;
      try {
        parsed = JSON.parse(utf8DecodeStrict(plainBytes));
      } catch (_err) {
        throw new SyncSecurityError("Corrupted decrypted device store payload.");
      }
      const files = {};
      const filesB64 = parsed.files_b64 || {};
      const rels = Object.keys(filesB64);
      for (let i = 0; i < rels.length; i += 1) {
        files[rels[i]] = base64ToBytes(filesB64[rels[i]]);
      }
      this._unlockedKey = key;
      this._unlockedSalt = new Uint8Array(salt);
      this._unlockedState = {
        vaultId: parsed.vault_id || null,
        syncCursor: parsed.sync_cursor || {},
        pairingMeta: parsed.pairing_meta || null,
        files: files,
      };
      return this._unlockedState;
    }

    async wipe() {
      this.lock();
      await this.backend.delete(this.recordId);
    }
  }

  // ─── Pairing Fragment Parsers & SBID1 Outbound Inbox Drop Composer ─────────

  function parseVaultPairingFragment(hashOrUrl) {
    const raw = String(hashOrUrl || "").trim();
    const idx = raw.indexOf("#vaultpair=");
    const token = idx >= 0
      ? raw.slice(idx + "#vaultpair=".length).split("&")[0]
      : raw.replace(/^#?vaultpair=/, "");
    if (!token) {
      throw new SyncSecurityError("Missing #vaultpair= token.");
    }
    let parsed;
    try {
      parsed = JSON.parse(utf8DecodeStrict(base64UrlToBytes(token)));
    } catch (_err) {
      throw new SyncSecurityError("Malformed #vaultpair= base64url JSON payload.");
    }
    if (!parsed || typeof parsed !== "object" || parsed.v !== 1) {
      throw new SyncSecurityError("Unsupported #vaultpair= schema version.");
    }
    const vid = String(parsed.vault_id || "").trim().toLowerCase();
    if (!HEX_ID_RE.test(vid)) {
      throw new SyncSecurityError("Invalid vault_id in #vaultpair= bundle.");
    }
    if (parsed.endpoint_url) {
      validateEndpointUrl(parsed.endpoint_url);
    }
    if (parsed.device_privkey_b64) {
      throw new SyncSecurityError("Refusing pairing bundle containing private device signing key.");
    }
    return parsed;
  }

  function parseDropPairingFragment(hashOrUrl) {
    const raw = String(hashOrUrl || "").trim();
    const idx = raw.indexOf("#pair=");
    const token = idx >= 0 ? raw.slice(idx + 6).split("&")[0] : raw.replace(/^#?pair=/, "");
    if (!token) {
      throw new SyncSecurityError("Missing #pair= token.");
    }
    const parsed = JSON.parse(utf8DecodeStrict(base64UrlToBytes(token)));
    if (!parsed || typeof parsed !== "object" || parsed.v !== SBID_VERSION) {
      throw new SyncSecurityError("Unsupported mobile inbox drop pairing version.");
    }
    if (!HEX32_RE.test(String(parsed.vault_id || ""))) {
      throw new SyncSecurityError("Invalid vault_id in drop pairing bundle.");
    }
    if (!HEX16_RE.test(String(parsed.key_id || "")) || !HEX16_RE.test(String(parsed.token_id || ""))) {
      throw new SyncSecurityError("Invalid key_id or token_id in drop pairing bundle.");
    }
    if (!parsed.recipient_pubkey_b64 || !parsed.drop_secret_b64) {
      throw new SyncSecurityError("Incomplete cryptographic keys in drop pairing bundle.");
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
    if (!HEX32_RE.test(vaultId) || !HEX16_RE.test(keyId) || !HEX16_RE.test(tokenId)) {
      throw new SyncSecurityError("Invalid vault_id, key_id, or token_id for SBID1 drop.");
    }

    const recipientPubBytes = base64ToBytes(pairing.recipient_pubkey_b64);
    const pubFp = (await sha256Hex(recipientPubBytes)).slice(0, 16);
    if (pubFp !== keyId) {
      throw new SyncSecurityError("Recipient public key fingerprint mismatch.");
    }

    const dropSecret = base64ToBytes(pairing.drop_secret_b64);
    if (dropSecret.length !== 32) {
      throw new SyncSecurityError("Invalid drop_secret length.");
    }

    let dropId = options.dropId ? String(options.dropId).toLowerCase() : "";
    if (!dropId) {
      const rnd = new Uint8Array(16);
      webCrypto.getRandomValues(rnd);
      dropId = bytesToHex(rnd);
    }
    if (!HEX32_RE.test(dropId)) {
      throw new SyncSecurityError("dropId must be 32 lowercase hex characters.");
    }
    const createdAt = options.createdAt || new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");

    const recipientKey = await subtle.importKey(
      "raw",
      recipientPubBytes,
      { name: "ECDH", namedCurve: "P-256" },
      false,
      []
    );
    const ephPair = await subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
    const epkRaw = new Uint8Array(await subtle.exportKey("raw", ephPair.publicKey));
    const epkB64 = bytesToBase64(epkRaw);

    const sharedBits = await subtle.deriveBits({ name: "ECDH", public: recipientKey }, ephPair.privateKey, 256);
    const infoBytes = utf8Bytes("SecondBrain-InboxDrop-v1|" + vaultId + "|" + keyId + "|" + tokenId);
    const aesKeyBytes = await hkdfSha256(new Uint8Array(sharedBits), hexToBytes(dropId), infoBytes, 32);

    const cleanTags = Array.isArray(payload.tags)
      ? payload.tags
          .slice(0, 16)
          .map(function (t) { return String(t || "").slice(0, 64); })
          .filter(Boolean)
      : [];
    const cleanDoc = {
      kind: "quick_capture",
      title: String(payload.title || "").slice(0, 300),
      url: String(payload.url || "").slice(0, 2048),
      text: String(payload.text || "").slice(0, 64000),
      tags: cleanTags,
      captured_at: String(payload.captured_at || createdAt).slice(0, 64),
      client: String(payload.client || "mobile-vault").slice(0, 32),
    };
    const plaintextBytes = utf8Bytes(JSON.stringify(cleanDoc));

    const iv = webCrypto.getRandomValues(new Uint8Array(12));
    const aadBytes = utf8Bytes(
      "SBID1|" + vaultId + "|" + dropId + "|" + keyId + "|" + tokenId + "|" + epkB64 + "|" + createdAt
    );
    const ctBytes = await aes256GcmEncrypt(aesKeyBytes, iv, plaintextBytes, aadBytes);
    const bodyBytes = concatBytes([iv, ctBytes]);
    const bodyShaHex = await sha256Hex(bodyBytes);

    const authMsg = utf8Bytes(
      "SBID1-AUTH\n" +
        vaultId +
        "\n" +
        dropId +
        "\n" +
        keyId +
        "\n" +
        tokenId +
        "\n" +
        epkB64 +
        "\n" +
        createdAt +
        "\n" +
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
    const headerBytes = utf8Bytes(canonicalJson(headerObj));
    const lenBytes = new Uint8Array(2);
    lenBytes[0] = (headerBytes.length >> 8) & 0xff;
    lenBytes[1] = headerBytes.length & 0xff;

    return {
      dropId: dropId,
      objectKey: "inbox-drops/" + dropId + ".enc",
      envelopeBytes: concatBytes([SBID_MAGIC, lenBytes, headerBytes, bodyBytes]),
      createdAt: createdAt,
    };
  }

  return {
    SYNC_SCHEMA_VERSION: SYNC_SCHEMA_VERSION,
    BACKUP_SCHEMA_VERSION: BACKUP_SCHEMA_VERSION,
    SBID_VERSION: SBID_VERSION,
    DEVICE_PBKDF2_ITERATIONS: DEVICE_PBKDF2_ITERATIONS,
    SyncError: SyncError,
    SyncSecurityError: SyncSecurityError,
    VaultBackupError: VaultBackupError,
    VaultBackupSecurityError: VaultBackupSecurityError,
    utf8Bytes: utf8Bytes,
    utf8DecodeStrict: utf8DecodeStrict,
    bytesToHex: bytesToHex,
    hexToBytes: hexToBytes,
    bytesToBase64: bytesToBase64,
    base64ToBytes: base64ToBytes,
    bytesToBase64Url: bytesToBase64Url,
    base64UrlToBytes: base64UrlToBytes,
    concatBytes: concatBytes,
    canonicalJson: canonicalJson,
    sha256BytesSync: sha256BytesSync,
    sha256HexSync: sha256HexSync,
    sha256Bytes: sha256Bytes,
    sha256Hex: sha256Hex,
    hmacSha256BytesSync: hmacSha256BytesSync,
    hmacSha256Bytes: hmacSha256Bytes,
    hkdfSha256: hkdfSha256,
    scrypt: scrypt,
    deriveMasterKeyFromPassphrase: deriveMasterKeyFromPassphrase,
    deriveSyncSubkey: deriveSyncSubkey,
    computeBlindBlobId: computeBlindBlobId,
    encryptSyncObject: encryptSyncObject,
    decryptSyncObject: decryptSyncObject,
    headSigningBytes: headSigningBytes,
    computeHeadMac: computeHeadMac,
    verifyEd25519Signature: verifyEd25519Signature,
    verifyRemoteSyncHead: verifyRemoteSyncHead,
    validateSyncRelPath: validateSyncRelPath,
    validateBackupRelPath: validateBackupRelPath,
    validateBackendKey: validateBackendKey,
    mergeAppendOnlyJsonl: mergeAppendOnlyJsonl,
    signS3SigV4Headers: signS3SigV4Headers,
    HttpObjectSyncTransport: HttpObjectSyncTransport,
    InMemoryOrBundleBackend: InMemoryOrBundleBackend,
    InMemoryKvBackend: InMemoryKvBackend,
    pullVaultFromBackend: pullVaultFromBackend,
    importSbVaultArchive: importSbVaultArchive,
    EncryptedDeviceStore: EncryptedDeviceStore,
    parseVaultPairingFragment: parseVaultPairingFragment,
    parseDropPairingFragment: parseDropPairingFragment,
    encryptInboxDropEnvelope: encryptInboxDropEnvelope,
  };
});
