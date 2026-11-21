"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const nodeCrypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const VaultCrypto = require("../www/lib/vault-crypto.js");
const Epistemic = require("../www/lib/vault-epistemic-engine.js");

function generateEd25519KeypairRaw() {
  const { publicKey, privateKey } = nodeCrypto.generateKeyPairSync("ed25519");
  const pubSpki = publicKey.export({ type: "spki", format: "der" });
  const pubRaw32 = new Uint8Array(pubSpki.subarray(pubSpki.length - 32));
  return { privateKey, pubRaw32, pubB64: Buffer.from(pubRaw32).toString("base64") };
}

function signEd25519B64(privateKey, msgBytes) {
  const sig = nodeCrypto.sign(null, Buffer.from(msgBytes), privateKey);
  return sig.toString("base64");
}

async function buildSignedRemoteBackend({
  masterKey,
  vaultId,
  epoch,
  parentManifestId = null,
  deviceId = "macbook-a1b2c3d4",
  edKeypair,
  filesPlainMap,
  rawManifestFilesOverride = null,
  kdfMode = "keyfile",
  kdfSalt = null,
}) {
  const backend = new VaultCrypto.InMemoryOrBundleBackend();
  const kcEnv = await VaultCrypto.encryptSyncObject(masterKey, {
    vaultId,
    kind: "header",
    objectId: "key-check",
    plaintextBytes: VaultCrypto.utf8Bytes("second-brain-e2ee-v1:" + vaultId),
  });
  const headerDoc = {
    schema_version: 1,
    vault_id: vaultId,
    kdf_mode: kdfMode,
    kdf_salt_b64: kdfSalt ? VaultCrypto.bytesToBase64(kdfSalt) : null,
    scrypt_params: kdfMode === "passphrase" ? { n: 32768, r: 8, p: 1 } : null,
    key_check_b64: VaultCrypto.bytesToBase64(kcEnv),
    created_at: "2026-06-01T00:00:00+00:00",
  };
  await backend.putObject("vault-header.json", VaultCrypto.utf8Bytes(JSON.stringify(headerDoc)));

  const manifestFiles = {};
  if (rawManifestFilesOverride) {
    Object.assign(manifestFiles, rawManifestFilesOverride);
  } else {
    for (const [relPath, rawVal] of Object.entries(filesPlainMap)) {
      const bytes = typeof rawVal === "string" ? VaultCrypto.utf8Bytes(rawVal) : rawVal;
      const sha = await VaultCrypto.sha256Hex(bytes);
      const blobId = await VaultCrypto.computeBlindBlobId(masterKey, vaultId, sha);
      const ct = await VaultCrypto.encryptSyncObject(masterKey, {
        vaultId,
        kind: "blob",
        objectId: blobId,
        plaintextBytes: bytes,
      });
      await backend.putObject("objects/" + blobId.slice(0, 2) + "/" + blobId + ".enc", ct);
      manifestFiles[relPath] = {
        blob_id: blobId,
        sha256: sha,
        size: bytes.length,
        append_only_jsonl: relPath.startsWith("0-raw/archive/") && relPath.endsWith(".jsonl"),
      };
    }
  }

  const createdAt = "2026-06-01T00:00:00+00:00";
  const manifestDoc = {
    schema_version: 1,
    vault_id: vaultId,
    epoch,
    parent_manifest_id: parentManifestId,
    device_id: deviceId,
    created_at: createdAt,
    file_count: Object.keys(manifestFiles).length,
    files: manifestFiles,
  };
  const manifestPlain = VaultCrypto.utf8Bytes(JSON.stringify(manifestDoc));
  const manifestId = await VaultCrypto.sha256Hex(
    VaultCrypto.concatBytes([
      VaultCrypto.utf8Bytes(vaultId + ":" + epoch + ":" + createdAt + ":"),
      manifestPlain,
    ])
  );
  const manifestCt = await VaultCrypto.encryptSyncObject(masterKey, {
    vaultId,
    kind: "manifest",
    objectId: manifestId,
    plaintextBytes: manifestPlain,
  });
  await backend.putObject("manifests/" + manifestId + ".enc", manifestCt);

  const headDoc = {
    schema_version: 1,
    vault_id: vaultId,
    epoch,
    parent_manifest_id: parentManifestId,
    manifest_id: manifestId,
    manifest_ciphertext_sha256: await VaultCrypto.sha256Hex(manifestCt),
    device_id: deviceId,
    device_pubkey_b64: edKeypair.pubB64,
    created_at: createdAt,
  };
  headDoc.signature_b64 = signEd25519B64(edKeypair.privateKey, VaultCrypto.headSigningBytes(headDoc));
  headDoc.head_mac = await VaultCrypto.computeHeadMac(masterKey, vaultId, headDoc);
  await backend.putObject("head.json", VaultCrypto.utf8Bytes(JSON.stringify(headDoc)));

  return { backend, headDoc, manifestId, manifestFiles };
}

test("RFC 7914 pure-JS scrypt matches official RFC vector and Node crypto.scryptSync at N=32768", async () => {
  // RFC 7914 Section 12 test vector 2: P="password", S="NaCl", N=1024, r=8, p=16, dkLen=64
  const rfcVec = VaultCrypto.scrypt("password", "NaCl", 1024, 8, 16, 64);
  assert.equal(
    VaultCrypto.bytesToHex(rfcVec),
    "fdbabe1c9d3472007856e7190d01e9fe7c6ad7cbc8237830e77376634b373162" +
      "2eaf30d92e22a3886ff109279d9830dac727afb94a83ee6d8360cbdfa2cc0640"
  );

  // Exact Second Brain vault_sync / vault_backup parameters: N=32768, r=8, p=1, dkLen=32
  const salt = new Uint8Array(nodeCrypto.randomBytes(32));
  const passphrase = "correct-horse-battery-staple-🚀";
  const jsDerived = VaultCrypto.deriveMasterKeyFromPassphrase(passphrase, salt);
  const nodeDerived = new Uint8Array(
    nodeCrypto.scryptSync(Buffer.from(passphrase, "utf8"), Buffer.from(salt), 32, {
      N: 32768,
      r: 8,
      p: 1,
      maxmem: 64 * 1024 * 1024,
    })
  );
  assert.equal(VaultCrypto.bytesToHex(jsDerived), VaultCrypto.bytesToHex(nodeDerived));
});

test("Ed25519 verifier passes valid signatures and rejects tampered/malleable signatures in both WebCrypto and pure-JS BigInt", async () => {
  const kp = generateEd25519KeypairRaw();
  const msg = VaultCrypto.utf8Bytes("second-brain-sync-head-v1\n{\"epoch\":4}");
  const sigBytes = VaultCrypto.base64ToBytes(signEd25519B64(kp.privateKey, msg));

  assert.equal(await VaultCrypto.verifyEd25519Signature(kp.pubRaw32, msg, sigBytes, { forcePureJs: false }), true);
  assert.equal(await VaultCrypto.verifyEd25519Signature(kp.pubRaw32, msg, sigBytes, { forcePureJs: true }), true);

  // Flip 1 bit in message
  const badMsg = new Uint8Array(msg);
  badMsg[5] ^= 0x01;
  assert.equal(await VaultCrypto.verifyEd25519Signature(kp.pubRaw32, badMsg, sigBytes, { forcePureJs: false }), false);
  assert.equal(await VaultCrypto.verifyEd25519Signature(kp.pubRaw32, badMsg, sigBytes, { forcePureJs: true }), false);

  // Flip 1 bit in signature
  const badSig = new Uint8Array(sigBytes);
  badSig[10] ^= 0x80;
  assert.equal(await VaultCrypto.verifyEd25519Signature(kp.pubRaw32, msg, badSig, { forcePureJs: true }), false);

  // Reject malleable S >= L (set top bits of byte 63)
  const malleableSig = new Uint8Array(sigBytes);
  malleableSig[63] = 0xff;
  assert.equal(await VaultCrypto.verifyEd25519Signature(kp.pubRaw32, msg, malleableSig, { forcePureJs: false }), false);
  assert.equal(await VaultCrypto.verifyEd25519Signature(kp.pubRaw32, msg, malleableSig, { forcePureJs: true }), false);

  // Reject all-zero low-order public key
  const zeroPub = new Uint8Array(32);
  zeroPub[0] = 1; // Neutral point (0, 1) has order 1 -> [8]A = O
  assert.equal(await VaultCrypto.verifyEd25519Signature(zeroPub, msg, sigBytes, { forcePureJs: true }), false);
});

test("Three-Store path validator and sync adversarial attacks fail closed", async () => {
  assert.equal(VaultCrypto.validateSyncRelPath("house-rules.md"), "house-rules.md");
  assert.equal(VaultCrypto.validateSyncRelPath("2-atoms/alpha.md"), "2-atoms/alpha.md");
  assert.equal(VaultCrypto.validateSyncRelPath("6-skills/deploy.md"), "6-skills/deploy.md");

  const forbiddenPaths = [
    "../outside.md",
    "2-atoms/../secret.md",
    "2-atoms\\backslash.md",
    "/2-atoms/leading.md",
    "C:/2-atoms/drive.md",
    "2-atoms/con.md",
    "2-atoms/nul.txt",
    "2-atoms/com1.md",
    ".sidecar/lexical.sqlite3",
    "2-atoms/.sidecar/leak.md",
    ".chroma/index.bin",
    "0-raw/sync-conflicts/conflict.md",
    "2-atoms/temp.tmp",
    "2-atoms/writer.lock",
    "2-atoms/.env",
    "unlisted-root/file.md",
  ];
  for (const bad of forbiddenPaths) {
    assert.throws(() => VaultCrypto.validateSyncRelPath(bad), VaultCrypto.SyncSecurityError, "Expected reject: " + bad);
  }

  const masterKey = new Uint8Array(nodeCrypto.randomBytes(32));
  const vaultId = nodeCrypto.randomBytes(16).toString("hex");
  const edKp = generateEd25519KeypairRaw();

  const { backend, headDoc } = await buildSignedRemoteBackend({
    masterKey,
    vaultId,
    epoch: 3,
    edKeypair: edKp,
    filesPlainMap: { "notes/idea.md": "# Hello\nWorld\n" },
  });

  // Clean pull succeeds
  const pulled = await VaultCrypto.pullVaultFromBackend(
    backend,
    { masterKeyBytes: masterKey, vaultId },
    { files: {}, syncCursor: { epoch: 0 } }
  );
  assert.equal(pulled.epoch, 3);
  assert.equal(pulled.downloadedBlobs, 1);

  // Incremental pull reuses unchanged blob without downloading
  const pulled2 = await VaultCrypto.pullVaultFromBackend(
    backend,
    { masterKeyBytes: masterKey, vaultId },
    { files: pulled.files, syncCursor: pulled.syncCursor }
  );
  assert.equal(pulled2.downloadedBlobs, 0);
  assert.equal(pulled2.reusedBlobs, 1);

  // Anti-rollback attack (cursor at epoch 5 > remote epoch 3)
  await assert.rejects(
    () =>
      VaultCrypto.pullVaultFromBackend(
        backend,
        { masterKeyBytes: masterKey, vaultId },
        { files: pulled.files, syncCursor: { epoch: 5, trusted_peers: pulled.syncCursor.trusted_peers } }
      ),
    VaultCrypto.SyncSecurityError
  );

  // Pinned peer key spoofing attack
  const rogueKp = generateEd25519KeypairRaw();
  const rogueRemote = await buildSignedRemoteBackend({
    masterKey,
    vaultId,
    epoch: 4,
    deviceId: headDoc.device_id,
    edKeypair: rogueKp,
    filesPlainMap: { "notes/idea.md": "# Spoofed\n" },
  });
  await assert.rejects(
    () =>
      VaultCrypto.pullVaultFromBackend(
        rogueRemote.backend,
        { masterKeyBytes: masterKey, vaultId },
        { files: pulled.files, syncCursor: pulled.syncCursor }
      ),
    VaultCrypto.SyncSecurityError
  );

  // Missing head_mac fails closed
  const noMacHead = Object.assign({}, headDoc);
  delete noMacHead.head_mac;
  await assert.rejects(
    () => VaultCrypto.verifyRemoteSyncHead(noMacHead, masterKey, { vaultId, lastEpoch: 0 }),
    VaultCrypto.SyncSecurityError
  );

  // Swapped AAD between blob and manifest fails closed
  const envA = await VaultCrypto.encryptSyncObject(masterKey, {
    vaultId,
    kind: "blob",
    objectId: "aa".repeat(32),
    plaintextBytes: VaultCrypto.utf8Bytes("secret"),
  });
  await assert.rejects(
    () =>
      VaultCrypto.decryptSyncObject(masterKey, {
        vaultId,
        kind: "blob",
        objectId: "bb".repeat(32),
        envelopeBytes: envA,
      }),
    VaultCrypto.SyncSecurityError
  );
});

test("EncryptedDeviceStore encrypts at rest with PBKDF2 210k + AES-256-GCM and locks cleanly", async () => {
  const kv = new VaultCrypto.InMemoryKvBackend();
  const store = new VaultCrypto.EncryptedDeviceStore({
    storageBackend: kv,
    recordId: "test_vault",
  });
  const sensitiveText = "SUPER_SECRET_VAULT_MEMORY_TOKEN_9918273";
  await store.saveState("pin-7890", {
    vaultId: "11".repeat(16),
    syncCursor: { epoch: 2 },
    files: { "notes/private.md": VaultCrypto.utf8Bytes(sensitiveText) },
  });

  const rawCiphertext = await store.inspectRawCiphertext();
  assert.ok(rawCiphertext && rawCiphertext.length > 64);
  const rawLatin1 = Buffer.from(rawCiphertext).toString("utf8");
  assert.equal(rawLatin1.includes(sensitiveText), false, "Plaintext leaked into device store ciphertext!");

  store.lock();
  assert.equal(store.isLocked(), true);
  assert.throws(() => store.getUnlockedState(), VaultCrypto.SyncSecurityError);

  await assert.rejects(() => store.unlock("wrong-pin"), VaultCrypto.SyncSecurityError);
  const unlocked = await store.unlock("pin-7890");
  assert.equal(VaultCrypto.utf8DecodeStrict(unlocked.files["notes/private.md"]), sensitiveText);
});

test("Offline Epistemic Engine enforces byte-offset anchors, project containment, bi-temporal bounds, DLP, and out-of-model grounding", async () => {
  const srcAlpha = "# Alpha Architecture\n\nAlpha service enforces mutual TLS 1.3 client certificates on port 8443.\n";
  const srcBytes = VaultCrypto.utf8Bytes(srcAlpha);
  const srcSha = VaultCrypto.sha256HexSync(srcBytes);
  const quote = "Alpha service enforces mutual TLS 1.3 client certificates on port 8443.";
  const startByte = VaultCrypto.utf8Bytes("# Alpha Architecture\n\n").length;
  const endByte = startByte + VaultCrypto.utf8Bytes(quote).length;
  const quoteSha = VaultCrypto.sha256HexSync(VaultCrypto.utf8Bytes(quote));

  const atomAlpha = [
    "---",
    "id: 2026-06-01-alpha-mtls",
    "type: atom",
    "memory_type: semantic",
    "review_status: accepted",
    "claim_state: human_reviewed",
    "citation_integrity: anchored",
    "epistemic_label: claim",
    "confidence: 0.92",
    "importance: 0.85",
    "sources:",
    '  - "0-raw/alpha-spec.md"',
    "contexts:",
    '  - "project:alpha"',
    "superseded_by: []",
    "evidence_source: 0-raw/alpha-spec.md",
    "evidence_sha256: " + srcSha,
    "evidence_start: " + startByte,
    "evidence_end: " + endByte,
    "evidence_quote_sha256: " + quoteSha,
    "---",
    "",
    "## Claim",
    "Alpha service requires mutual TLS 1.3 client certificates on port 8443.",
    "",
    "## Evidence",
    "> " + quote,
    "",
  ].join("\n");
  const atomAlphaSha = VaultCrypto.sha256HexSync(VaultCrypto.utf8Bytes(atomAlpha));

  const atomPending = [
    "---",
    "id: 2026-06-01-alpha-rumor",
    "type: atom",
    "memory_type: semantic",
    "review_status: pending",
    "claim_state: candidate",
    "epistemic_label: claim",
    "contexts:",
    '  - "project:alpha"',
    "---",
    "",
    "## Claim",
    "Alpha service might disable mutual TLS 1.3 certificates next quarter.",
    "",
    "## Evidence",
    "> Unverified rumor about Alpha mutual TLS 1.3 certificates.",
    "",
  ].join("\n");

  const decisionLog =
    JSON.stringify({
      schema_version: 1,
      action: "approve",
      actor: "owner",
      atom_filename: "alpha-mtls.md",
      content_sha256_after: atomAlphaSha,
    }) + "\n";

  const filesMap = {
    "0-raw/alpha-spec.md": srcBytes,
    "0-raw/archive/decision-record.jsonl": VaultCrypto.utf8Bytes(decisionLog),
    "2-atoms/alpha-mtls.md": VaultCrypto.utf8Bytes(atomAlpha),
    "2-atoms/alpha-rumor.md": VaultCrypto.utf8Bytes(atomPending),
    "notes/scratch.md": VaultCrypto.utf8Bytes("# Scratch\nAlpha TLS notes for local debugging #project:alpha\n"),
  };

  // Verified search returns ONLY alpha-mtls.md in project:alpha, and 0 in project:beta
  const hitsAlpha = Epistemic.searchVerifiedVault(filesMap, "Alpha mutual TLS port", { project: "project:alpha" });
  assert.equal(hitsAlpha.length, 1);
  assert.equal(hitsAlpha[0].filename, "alpha-mtls.md");

  const hitsBeta = Epistemic.searchVerifiedVault(filesMap, "Alpha mutual TLS port", { project: "project:beta" });
  assert.equal(hitsBeta.length, 0);

  // Working notes are strictly isolated as unverified_note
  const noteHits = Epistemic.searchWorkingNotes(filesMap, "Alpha TLS debugging", { project: "project:alpha" });
  assert.equal(noteHits.length, 1);
  assert.equal(noteHits[0].trust_tier, "unverified_note");
  assert.equal(noteHits[0].attribution, "[note: scratch.md]");

  // Research mode separates verified claims from [UNVERIFIED LEADS]
  const researchAns = await Epistemic.answerGroundedQuestion(filesMap, "Alpha mutual TLS certificates", {
    mode: "research",
    project: "project:alpha",
  });
  assert.equal(researchAns.abstained, false);
  assert.equal(researchAns.claims.length, 1);
  assert.equal(researchAns.unverified_leads.length, 1);
  assert.equal(researchAns.unverified_leads[0].lead_id, "alpha-rumor.md");
  assert.ok(researchAns.text.includes("[UNVERIFIED LEADS]"));

  // Local model adapter: strips unsupported paraphrases out-of-model & blocks secrets via DLP
  const paraphrasedAns = await Epistemic.answerGroundedQuestion(filesMap, "Alpha mutual TLS certificates", {
    mode: "verified",
    project: "project:alpha",
    localModelAdapter: async () => ({
      claims: ["Alpha probably uses TLS 1.2 on port 443."], // Hallucinated paraphrase -> must be rejected!
    }),
  });
  assert.equal(paraphrasedAns.abstained, true);
  assert.equal(paraphrasedAns.claims.length, 0);

  await assert.rejects(
    () =>
      Epistemic.answerGroundedQuestion(
        filesMap,
        "Alpha TLS with token sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456",
        {
          mode: "verified",
          project: "project:alpha",
          localModelAdapter: async () => ({ claims: [] }),
        }
      ),
    Epistemic.OutboundSecretBlockedError
  );
});

test("Static security audit of mobile/vault-client web & native shell configurations", () => {
  const clientRoot = path.resolve(__dirname, "..");
  const wwwDir = path.join(clientRoot, "www");

  function walkFiles(dir) {
    const out = [];
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        out.push(...walkFiles(full));
      } else {
        out.push(full);
      }
    }
    return out;
  }

  const webFiles = walkFiles(wwwDir).filter((f) => /\.(js|html|css)$/i.test(f));
  assert.ok(webFiles.length >= 6);

  const bannedPatterns = [
    /\.innerHTML\b/,
    /\.outerHTML\b/,
    /\.insertAdjacentHTML\b/,
    /\bdocument\.write\s*\(/,
    /\beval\s*\(/,
    /\bnew\s+Function\s*\(/,
  ];
  for (const file of webFiles) {
    const src = fs.readFileSync(file, "utf8");
    for (const pat of bannedPatterns) {
      assert.equal(pat.test(src), false, "Forbidden unsafe pattern " + pat + " found in " + file);
    }
  }

  const indexHtml = fs.readFileSync(path.join(wwwDir, "index.html"), "utf8");
  assert.ok(indexHtml.includes("http-equiv=\"Content-Security-Policy\""));
  assert.ok(indexHtml.includes("default-src 'self'"));
  assert.ok(indexHtml.includes("script-src 'self'"));
  assert.ok(indexHtml.includes("style-src 'self'"));
  assert.ok(indexHtml.includes("object-src 'none'"));
  assert.equal(/\sstyle=/i.test(indexHtml), false, "Inline style attribute violates style-src 'self'");

  const capConfig = JSON.parse(fs.readFileSync(path.join(clientRoot, "capacitor.config.json"), "utf8"));
  assert.equal(capConfig.appId, "com.secondbrain.vault");
  assert.equal(capConfig.server.iosScheme, "https");
  assert.equal(capConfig.server.androidScheme, "https");
  assert.deepEqual(capConfig.server.allowNavigation, []);

  const androidManifest = fs.readFileSync(
    path.join(clientRoot, "android", "app", "src", "main", "AndroidManifest.xml"),
    "utf8"
  );
  assert.ok(androidManifest.includes('android:usesCleartextTraffic="false"'));
  assert.ok(androidManifest.includes('android:allowBackup="false"'));

  const infoPlist = fs.readFileSync(path.join(clientRoot, "ios", "App", "App", "Info.plist"), "utf8");
  assert.ok(infoPlist.includes("<key>UIFileSharingEnabled</key>"));
  assert.ok(infoPlist.includes("<key>NSAllowsArbitraryLoads</key>"));
  assert.ok(infoPlist.includes("sbvault"));
});
