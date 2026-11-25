"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const nodeCrypto = require("node:crypto");

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const DESKTOP_RENDERER_DIR = path.join(REPO_ROOT, "desktop", "renderer");
const DESKTOP_MAIN_PATH = path.join(REPO_ROOT, "desktop", "main.js");
const INBOX_DROP_DIR = path.join(REPO_ROOT, "mobile", "inbox-drop");

// ─── Minimal Safe DOM Harness ────────────────────────────────────────────────

class MiniClassList {
  constructor(el) {
    this._el = el;
    this._set = new Set();
  }
  _syncFromString(str) {
    this._set = new Set(String(str || "").split(/\s+/).filter(Boolean));
  }
  _syncToEl() {
    this._el._className = [...this._set].join(" ");
  }
  add(...names) {
    for (const n of names) if (n) this._set.add(String(n));
    this._syncToEl();
  }
  remove(...names) {
    for (const n of names) this._set.delete(String(n));
    this._syncToEl();
  }
  toggle(name, force) {
    const key = String(name);
    if (force === undefined) {
      if (this._set.has(key)) this._set.delete(key);
      else this._set.add(key);
    } else if (force) {
      this._set.add(key);
    } else {
      this._set.delete(key);
    }
    this._syncToEl();
    return this._set.has(key);
  }
  contains(name) {
    return this._set.has(String(name));
  }
}

class MiniElement {
  constructor(tagName = "div", id = "") {
    this.nodeType = 1;
    this.tagName = String(tagName || "div").toUpperCase();
    this.id = id;
    this._className = "";
    this.classList = new MiniClassList(this);
    this.style = {};
    this.dataset = Object.create(null);
    this.attributes = Object.create(null);
    this.children = [];
    this.parentNode = null;
    this._listeners = Object.create(null);
    this._text = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.open = false;
    this.type = "";
    this.src = "";
    this.alt = "";
    this.href = "";
    this.controls = false;
    this.autoplay = false;
    this.loop = false;
    this.onclick = null;
  }

  get className() {
    return this._className;
  }
  set className(val) {
    this._className = String(val || "");
    this.classList._syncFromString(this._className);
  }

  get textContent() {
    if (!this.children.length) return this._text;
    return this.children.map((c) => c.textContent || "").join("");
  }
  set textContent(val) {
    this.children = [];
    this._text = String(val ?? "");
  }

  get selectedOptions() {
    const chosen = this.children.filter((c) => c.value === this.value);
    return chosen.length ? chosen : this.children.slice(0, 1);
  }

  setAttribute(k, v) {
    this.attributes[String(k)] = String(v);
    if (k === "id") this.id = String(v);
    if (k === "class") this.className = String(v);
  }
  getAttribute(k) {
    return Object.prototype.hasOwnProperty.call(this.attributes, k)
      ? this.attributes[k]
      : null;
  }
  appendChild(node) {
    if (!node) return node;
    node.parentNode = this;
    this.children.push(node);
    return node;
  }
  replaceChildren(...nodes) {
    this._text = "";
    this.children = [];
    for (const n of nodes) {
      if (n !== null && n !== undefined) {
        if (typeof n === "string") {
          this.children.push({ nodeType: 3, textContent: n });
        } else {
          n.parentNode = this;
          this.children.push(n);
        }
      }
    }
  }
  remove() {
    if (this.parentNode && Array.isArray(this.parentNode.children)) {
      this.parentNode.children = this.parentNode.children.filter((c) => c !== this);
    }
  }
  focus() {}
  addEventListener(ev, cb) {
    if (!this._listeners[ev]) this._listeners[ev] = [];
    this._listeners[ev].push(cb);
  }
  async dispatchEvent(ev, payload = {}) {
    const eventObj = {
      type: ev,
      target: this,
      currentTarget: this,
      preventDefault() {},
      stopPropagation() {},
      ...payload,
    };
    if (ev === "click" && typeof this.onclick === "function") {
      await this.onclick(eventObj);
    }
    for (const fn of this._listeners[ev] || []) {
      await fn(eventObj);
    }
  }
  async click() {
    return this.dispatchEvent("click");
  }
  querySelectorAll(selector) {
    const out = [];
    const match = (el) => {
      if (selector.startsWith(".")) {
        const cls = selector.slice(1).split(".");
        return cls.every((c) => el.classList.contains(c));
      }
      return el.tagName === selector.toUpperCase();
    };
    const walk = (node) => {
      for (const ch of node.children || []) {
        if (ch && ch.nodeType === 1) {
          if (match(ch)) out.push(ch);
          walk(ch);
        }
      }
    };
    walk(this);
    return out;
  }
  findAll(pred, out = []) {
    if (pred(this)) out.push(this);
    for (const ch of this.children || []) {
      if (ch && ch.nodeType === 1) ch.findAll(pred, out);
    }
    return out;
  }
}

function createMiniDocument() {
  const byId = new Map();
  const body = new MiniElement("body");
  return {
    body,
    _byId: byId,
    createElement(tag) {
      return new MiniElement(tag);
    },
    createElementNS(_ns, tag) {
      return new MiniElement(tag);
    },
    createTextNode(txt) {
      return { nodeType: 3, textContent: String(txt ?? "") };
    },
    getElementById(id) {
      if (!byId.has(id)) {
        const el = new MiniElement("div", id);
        byId.set(id, el);
      }
      return byId.get(id);
    },
    querySelectorAll() {
      return [];
    },
    querySelector() {
      return null;
    },
  };
}

function createMockLocalStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    _map: map,
    getItem(k) {
      return map.has(String(k)) ? map.get(String(k)) : null;
    },
    setItem(k, v) {
      map.set(String(k), String(v));
    },
    removeItem(k) {
      map.delete(String(k));
    },
    clear() {
      map.clear();
    },
  };
}

function createMockIndexedDb(backingStore = new Map()) {
  const stores = new Set(["keys"]);
  return {
    _backingStore: backingStore,
    open(_name, _version) {
      const req = { result: null, error: null, onupgradeneeded: null, onsuccess: null, onerror: null };
      const db = {
        objectStoreNames: {
          contains(storeName) {
            return stores.has(storeName);
          },
        },
        createObjectStore(storeName) {
          stores.add(storeName);
          return {};
        },
        close() {},
        transaction(_storeName, _mode) {
          const tx = {
            oncomplete: null,
            onerror: null,
            error: null,
            objectStore() {
              return {
                get(key) {
                  const r = { result: undefined, error: null, onsuccess: null, onerror: null };
                  queueMicrotask(() => {
                    r.result = backingStore.get(key);
                    if (typeof r.onsuccess === "function") r.onsuccess();
                  });
                  return r;
                },
                put(val, key) {
                  backingStore.set(key, val);
                  queueMicrotask(() => {
                    if (typeof tx.oncomplete === "function") tx.oncomplete();
                  });
                },
                delete(key) {
                  backingStore.delete(key);
                  queueMicrotask(() => {
                    if (typeof tx.oncomplete === "function") tx.oncomplete();
                  });
                },
              };
            },
          };
          return tx;
        },
      };
      req.result = db;
      queueMicrotask(() => {
        if (typeof req.onupgradeneeded === "function") req.onupgradeneeded();
        if (typeof req.onsuccess === "function") req.onsuccess();
      });
      return req;
    },
  };
}

function encodeBase64Url(str) {
  return Buffer.from(str, "utf8")
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

// ─── Defect #4: mobile/inbox-drop localStorage secret sealing ────────────────

describe("Defect #4: mobile/inbox-drop never stores raw decryption or S3 secrets in localStorage", () => {
  function bootInboxDropApp({ localStorage, indexedDB, hash = "" }) {
    const doc = createMiniDocument();
    const cryptoJs = fs.readFileSync(path.join(INBOX_DROP_DIR, "drop-crypto.js"), "utf8");
    const appJs = fs.readFileSync(path.join(INBOX_DROP_DIR, "app.js"), "utf8");
    const sandbox = {
      location: { hash, search: "", pathname: "/", protocol: "https:" },
      history: {
        replaceState(_s, _t, nextPath) {
          sandbox.location.pathname = nextPath;
          sandbox.location.hash = "";
        },
      },
      document: doc,
      localStorage,
      indexedDB,
      crypto: nodeCrypto.webcrypto,
      navigator: { onLine: true },
      URL,
      URLSearchParams,
      TextEncoder,
      TextDecoder,
      Uint8Array,
      ArrayBuffer,
      Blob,
      btoa: (s) => Buffer.from(String(s), "binary").toString("base64"),
      atob: (s) => Buffer.from(String(s), "base64").toString("binary"),
      Promise,
      Set,
      Map,
      console,
      setTimeout,
      clearTimeout,
    };
    sandbox.window = sandbox;
    sandbox.self = sandbox;
    sandbox.globalThis = sandbox;
    const win = sandbox;
    const ctx = vm.createContext(sandbox);
    vm.runInContext(cryptoJs, ctx, { filename: "mobile/inbox-drop/drop-crypto.js" });
    vm.runInContext(appJs, ctx, { filename: "mobile/inbox-drop/app.js" });
    return { doc, win, ctx, sandbox };
  }

  it("seals recall_key_b64, drop_secret_b64, hmac_secret_b64 & s3_secret_access_key via non-extractable WebCrypto IndexedDB key", async () => {
    const kp = await nodeCrypto.webcrypto.subtle.generateKey(
      { name: "ECDH", namedCurve: "P-256" },
      true,
      ["deriveBits"]
    );
    const pubRaw = new Uint8Array(await nodeCrypto.webcrypto.subtle.exportKey("raw", kp.publicKey));
    const keyId = nodeCrypto.createHash("sha256").update(pubRaw).digest("hex").slice(0, 16);

    const recallKeyBytes = nodeCrypto.randomBytes(32);
    const recallKeyId = nodeCrypto.createHash("sha256").update(recallKeyBytes).digest("hex").slice(0, 16);
    const recallSecret = Buffer.from(recallKeyBytes).toString("base64");
    const dropSecret = Buffer.from(nodeCrypto.randomBytes(32)).toString("base64");
    const hmacSecret = Buffer.from(nodeCrypto.randomBytes(32)).toString("base64");
    const s3Secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY999";

    const pairPayload = {
      v: 1,
      vault_id: "aa".repeat(16),
      key_id: keyId,
      token_id: "1122334455667788",
      recipient_pubkey_b64: Buffer.from(pubRaw).toString("base64"),
      drop_secret_b64: dropSecret,
      hmac_secret_b64: hmacSecret,
      recall_key_b64: recallSecret,
      recall_key_id: recallKeyId,
      target_url: "https://relay.example.com/vault",
      s3_region: "us-east-1",
    };
    const pairFrag = "#pair=" + encodeBase64Url(JSON.stringify(pairPayload));

    const localStorage = createMockLocalStorage();
    const idbBacking = new Map();
    const indexedDB = createMockIndexedDb(idbBacking);

    const firstBoot = bootInboxDropApp({ localStorage, indexedDB });
    await firstBoot.win.__sbInboxDropAppReady;

    // Save pairing + S3 secret access key through UI button
    firstBoot.doc.getElementById("pairInput").value = pairFrag;
    firstBoot.doc.getElementById("s3AccessKeyInput").value = "AKIAEXAMPLE123456789";
    firstBoot.doc.getElementById("s3SecretKeyInput").value = s3Secret;
    await firstBoot.doc.getElementById("savePairingBtn").click();

    const storedRaw = localStorage.getItem("sb_inbox_drop_pairing_v1");
    assert.ok(storedRaw, "Pairing metadata envelope must be saved in localStorage");
    const storedObj = JSON.parse(storedRaw);

    // Raw secrets MUST NEVER appear anywhere in localStorage
    for (const secretVal of [recallSecret, dropSecret, hmacSecret, s3Secret]) {
      assert.equal(
        storedRaw.includes(secretVal),
        false,
        "Raw secret leaked into plaintext localStorage!"
      );
    }
    for (const field of [
      "recall_key_b64",
      "drop_secret_b64",
      "hmac_secret_b64",
      "s3_secret_access_key",
    ]) {
      assert.equal(
        Object.prototype.hasOwnProperty.call(storedObj, field),
        false,
        `Plaintext secret field '${field}' must not exist in localStorage`
      );
    }
    assert.ok(storedObj.sealed_secrets_v1 && storedObj.sealed_secrets_v1.ct_b64);

    // Key stored in IndexedDB MUST be non-extractable WebCrypto CryptoKey
    const storedKey = idbBacking.get("device_pairing_wrap_v1");
    assert.ok(storedKey, "Device wrap key must be persisted in IndexedDB");
    assert.equal(storedKey.extractable, false, "IndexedDB wrap key must be non-extractable");

    // Second boot with same localStorage + IndexedDB must cleanly unseal secrets
    const secondBoot = bootInboxDropApp({ localStorage, indexedDB });
    await secondBoot.win.__sbInboxDropAppReady;
    assert.match(
      secondBoot.doc.getElementById("recallCapabilityBadge").textContent,
      /E2EE Recall/i,
      "Unsealed recall_key_b64 must restore recall capability across reload"
    );

    // Forget Device Key must scrub localStorage AND IndexedDB key handle
    await secondBoot.doc.getElementById("clearPairingBtn").click();
    assert.equal(localStorage.getItem("sb_inbox_drop_pairing_v1"), null);
    assert.equal(idbBacking.has("device_pairing_wrap_v1"), false);
  });

  it("auto-migrates & scrubs legacy plaintext keys in localStorage, and keeps secrets RAM-only without IndexedDB", async () => {
    const legacyRecallBytes = nodeCrypto.randomBytes(32);
    const legacyRecallId = nodeCrypto.createHash("sha256").update(legacyRecallBytes).digest("hex").slice(0, 16);
    const legacyRecall = Buffer.from(legacyRecallBytes).toString("base64");
    const legacyS3Secret = "LEGACY_PLAINTEXT_S3_SECRET_VALUE_888";
    const legacyObj = {
      v: 1,
      vault_id: "bb".repeat(16),
      key_id: "deadbeef12345678",
      token_id: "0011223344556677",
      recipient_pubkey_b64: Buffer.from(nodeCrypto.randomBytes(65)).toString("base64"),
      recall_key_b64: legacyRecall,
      recall_key_id: legacyRecallId,
      s3_secret_access_key: legacyS3Secret,
    };

    // Case A: IndexedDB available -> migrates plaintext keys into sealed_secrets_v1
    const lsMigrate = createMockLocalStorage({
      sb_inbox_drop_pairing_v1: JSON.stringify(legacyObj),
    });
    const idb = createMockIndexedDb(new Map());
    const migratedBoot = bootInboxDropApp({ localStorage: lsMigrate, indexedDB: idb });
    await migratedBoot.win.__sbInboxDropAppReady;
    const afterMigrate = lsMigrate.getItem("sb_inbox_drop_pairing_v1");
    assert.equal(afterMigrate.includes(legacyRecall), false);
    assert.equal(afterMigrate.includes(legacyS3Secret), false);
    assert.ok(JSON.parse(afterMigrate).sealed_secrets_v1);

    // Case B: IndexedDB unavailable -> scrubs plaintext from localStorage and keeps secrets RAM-only
    const lsRamOnly = createMockLocalStorage({
      sb_inbox_drop_pairing_v1: JSON.stringify(legacyObj),
    });
    const ramBoot = bootInboxDropApp({ localStorage: lsRamOnly, indexedDB: undefined });
    await ramBoot.win.__sbInboxDropAppReady;
    const afterRam = lsRamOnly.getItem("sb_inbox_drop_pairing_v1");
    assert.equal(afterRam.includes(legacyRecall), false);
    assert.equal(afterRam.includes(legacyS3Secret), false);
    assert.equal(JSON.parse(afterRam).sealed_secrets_v1, undefined);
    assert.match(
      ramBoot.doc.getElementById("recallCapabilityBadge").textContent,
      /E2EE Recall/i,
      "Secrets must remain active in RAM for current session even when IndexedDB is unavailable"
    );
  });
});

// ─── Defects #12 & #15: Desktop Renderer Schema Parity, Media Allowlist & Lifecycle ─

describe("Defects #12 & #15: Desktop renderer schema parity, safe data_uri validation, vault-switch reset & ready loaders", () => {
  function mountDesktopRenderer(routeHandler) {
    const doc = createMiniDocument();
    const apiCalls = [];
    let backendStatusCb = null;

    const win = {
      SecondBrainMarkdown: require(path.join(DESKTOP_RENDERER_DIR, "markdown.js")),
      SecondBrainPlaybookUnits: require(path.join(DESKTOP_RENDERER_DIR, "playbook_units.js")),
      SecondBrainPagination: require(path.join(DESKTOP_RENDERER_DIR, "pagination.js")),
      SecondBrainChatState: require(path.join(DESKTOP_RENDERER_DIR, "chat_state.js")),
      SecondBrainGraph: require(path.join(DESKTOP_RENDERER_DIR, "graph.js")),
      SecondBrainModal: require(path.join(DESKTOP_RENDERER_DIR, "modal_helpers.js")),
      addEventListener() {},
      secondBrain: {
        platform: "win32",
        onBackendStatus(cb) {
          backendStatusCb = cb;
        },
        onRefresh() {},
        async request(method, reqPath, body) {
          apiCalls.push({ method, path: reqPath, body });
          return routeHandler(method, reqPath, body);
        },
        async api(method, reqPath, body) {
          apiCalls.push({ method, path: reqPath, body });
          return routeHandler(method, reqPath, body);
        },
      },
    };

    const appJs = fs.readFileSync(path.join(DESKTOP_RENDERER_DIR, "app.js"), "utf8");
    const sandbox = {
      window: win,
      document: doc,
      navigator: { clipboard: { async writeText() {} } },
      URL,
      URLSearchParams,
      Set,
      Map,
      Promise,
      Date,
      Math,
      JSON,
      RegExp,
      Number,
      String,
      Boolean,
      Array,
      Object,
      Error,
      console,
      setTimeout,
      clearTimeout,
      setInterval,
      clearInterval,
      encodeURIComponent,
      decodeURIComponent,
      requestAnimationFrame: (fn) => setTimeout(fn, 0),
    };
    sandbox.globalThis = sandbox;
    const ctx = vm.createContext(sandbox);
    vm.runInContext(appJs, ctx, { filename: "desktop/renderer/app.js" });

    return { doc, win, ctx, apiCalls, triggerBackendStatus: (st) => backendStatusCb && backendStatusCb(st) };
  }

  it("renders revoked vs active mobile clients, pwa_pairing_url, step_actions count, neural_backends_unlocked, grounded_atom_ids, video backend guard & data_uri allowlist", async () => {
    let mediaFileResponse = null;
    const mounted = mountDesktopRenderer(async (method, reqPath, body) => {
      if (reqPath === "/v1/mobile/status") {
        return {
          ok: true,
          data: {
            pairings: [
              { pairing_id: "mob_active", label: "Pixel Active", revoked: false, created_at: "2026-11-20" },
              { pairing_id: "mob_revoked", label: "Old iPhone", revoked: true, created_at: "2026-11-01" },
            ],
          },
        };
      }
      if (reqPath === "/v1/mobile/pair" && method === "POST") {
        return {
          ok: true,
          data: {
            pairing_id: "mob_new_1",
            pwa_pairing_url: "https://localhost/vault-client/#vaultpair=TOKEN_ABC123",
            pairing_fragment: "#vaultpair=TOKEN_ABC123",
          },
        };
      }
      if (reqPath.startsWith("/v1/skills?include_inactive=1")) {
        return {
          ok: true,
          data: {
            kill_switch: { global_engaged: false, killed_skills: {} },
            skills: [
              {
                skill_id: "audit-three-step",
                playbook_id: "pb-three",
                title: "Three Step Skill",
                status: "active",
                valid: true,
                step_actions: [{ action_type: "vault_audit" }, { action_type: "vault_audit" }, { action_type: "vault_audit" }],
                sha256: "a".repeat(64),
              },
            ],
          },
        };
      }
      if (reqPath === "/v1/skills/candidates") return { ok: true, data: { candidates: [] } };
      if (reqPath.startsWith("/v1/skills/executions")) return { ok: true, data: { executions: [] } };
      if (reqPath === "/v1/media/status") {
        return {
          ok: true,
          data: {
            tier_id: "tier_3_gpu_accelerated",
            max_width: 1024,
            max_height: 1024,
            neural_backends_unlocked: true,
          },
        };
      }
      if (reqPath.startsWith("/v1/media/gallery")) return { ok: true, data: { items: [] } };
      if (reqPath === "/v1/media/video" && method === "POST") {
        return {
          ok: true,
          data: {
            media_id: "vid_001",
            filename: "clip.mp4",
            backend_used: body.backend,
            width: 512,
            height: 384,
            sha256: "b".repeat(64),
            grounded_atom_ids: ["atom-1.md", "atom-2.md"],
          },
        };
      }
      if (reqPath.startsWith("/v1/media/file/")) {
        return mediaFileResponse || { ok: false, status: 404, data: { error: "not mocked" } };
      }
      return { ok: true, data: {} };
    });

    // Wait briefly for initial startup calls to settle
    await new Promise((r) => setTimeout(r, 20));

    // 1. Mobile Vault Client pairings: active gets Revoke button; revoked displays "revoked" and NO Revoke button
    await vm.runInContext("loadMobileVaultClient()", mounted.ctx);
    const mobileRows = mounted.doc.getElementById("mobileVaultPairingsWrap").children;
    assert.equal(mobileRows.length, 2);
    const activeRowText = mobileRows[0].textContent;
    const activeButtons = mobileRows[0].findAll((el) => el.tagName === "BUTTON");
    assert.ok(activeRowText.includes("active"));
    assert.equal(activeButtons.length, 1, "Active pairing must render Revoke button");
    assert.equal(activeButtons[0].textContent, "Revoke");

    const revokedRowText = mobileRows[1].textContent;
    const revokedButtons = mobileRows[1].findAll((el) => el.tagName === "BUTTON");
    assert.ok(revokedRowText.includes("revoked"), "Revoked pairing must display 'revoked' status");
    assert.equal(revokedRowText.includes("active"), false);
    assert.equal(revokedButtons.length, 0, "Revoked pairing must NOT render Revoke button");

    // 2. Mobile Vault Client pair reads pwa_pairing_url
    await mounted.doc.getElementById("mobileVaultPairBtn").click();
    assert.ok(
      mounted.doc
        .getElementById("mobileVaultOutput")
        .textContent.includes("https://localhost/vault-client/#vaultpair=TOKEN_ABC123")
    );

    // 3. Executable Skills reads sk.step_actions (Steps: 3, not Steps: 0)
    await vm.runInContext("loadExecutableSkills()", mounted.ctx);
    const skillsText = mounted.doc.getElementById("executableSkillsGrid").textContent;
    assert.ok(skillsText.includes("Steps: 3"), `Expected 'Steps: 3' from step_actions, got: ${skillsText}`);

    // 4. Creative Media reads st.neural_backends_unlocked -> "badge ok"
    await vm.runInContext("loadCreativeMediaStudio()", mounted.ctx);
    assert.equal(mounted.doc.getElementById("creativeHardwareBadge").className, "badge ok");

    // 5. Video generation maps image-only backends (sd_webui_local & diffusers_local) -> "auto" AND reads grounded_atom_ids
    mediaFileResponse = {
      ok: true,
      data: {
        media_id: "vid_001",
        filename: "clip.mp4",
        mime_type: "video/mp4",
        size_bytes: 12,
        sha256: "b".repeat(64),
        data_uri: "data:video/mp4;base64,AAAAIGZ0eXA=",
      },
    };
    mounted.doc.getElementById("creativePromptInput").value = "Architecture animation";
    for (const imgOnlyBackend of ["sd_webui_local", "diffusers_local"]) {
      mounted.apiCalls.length = 0;
      mounted.doc.getElementById("creativeBackendSelect").value = imgOnlyBackend;
      await mounted.doc.getElementById("creativeGenVideoBtn").click();
      const postCall = mounted.apiCalls.find((c) => c.method === "POST" && c.path === "/v1/media/video");
      assert.ok(postCall, "Expected POST /v1/media/video");
      assert.equal(
        postCall.body.backend,
        "auto",
        `Image-only backend '${imgOnlyBackend}' must be remapped to 'auto' for /v1/media/video`
      );
      assert.ok(
        mounted.doc.getElementById("creativeStudioOutput").textContent.includes("grounded=2 atoms"),
        "Must read d.grounded_atom_ids length"
      );
    }

    // 6. previewCreativeAsset allowlist blocks unsafe SVG / HTML / javascript: URIs
    for (const badCase of [
      { mime_type: "image/svg+xml", data_uri: "data:image/svg+xml;base64,PHN2Zy8+" },
      { mime_type: "image/png", data_uri: "data:text/html;base64,PHNjcmlwdD4=" },
      { mime_type: "image/png", data_uri: "javascript:alert(1)" },
      { mime_type: "video/mp4", data_uri: "data:image/png;base64,iVBORw0KGgo=" },
    ]) {
      mediaFileResponse = {
        ok: true,
        data: {
          media_id: "bad_asset",
          filename: "bad.bin",
          mime_type: badCase.mime_type,
          size_bytes: 16,
          sha256: "c".repeat(64),
          data_uri: badCase.data_uri,
        },
      };
      await vm.runInContext("previewCreativeAsset('bad_asset')", mounted.ctx);
      const wrap = mounted.doc.getElementById("creativePreviewWrap");
      const mediaNodes = wrap.findAll((el) => el.tagName === "IMG" || el.tagName === "VIDEO");
      assert.equal(mediaNodes.length, 0, `Unsafe URI (${badCase.data_uri}) must NOT mount <img>/<video>`);
      assert.ok(wrap.textContent.includes("Blocked unsafe"));
    }

    // Valid PNG mounts <img> with expected src
    mediaFileResponse = {
      ok: true,
      data: {
        media_id: "good_png",
        filename: "ok.png",
        mime_type: "image/png",
        size_bytes: 16,
        sha256: "d".repeat(64),
        data_uri: "data:image/png;base64,iVBORw0KGgo=",
      },
    };
    await vm.runInContext("previewCreativeAsset('good_png')", mounted.ctx);
    const okImgs = mounted.doc
      .getElementById("creativePreviewWrap")
      .findAll((el) => el.tagName === "IMG");
    assert.equal(okImgs.length, 1);
    assert.equal(okImgs[0].src, "data:image/png;base64,iVBORw0KGgo=");
  });

  it("resets activeNote & reloads active view on vault switch, and triggers all secondary loaders on backend ready", async () => {
    const mounted = mountDesktopRenderer(async (_method, reqPath) => {
      if (reqPath === "/v1/vaults/switch") {
        return { ok: true, data: { active_vault_id: "work-vault" } };
      }
      return { ok: true, data: { notes: [], items: [], pairings: [], skills: [], candidates: [], workspaces: [] } };
    });

    await new Promise((r) => setTimeout(r, 20));
    mounted.apiCalls.length = 0;

    // Trigger backend "ready" -> MUST invoke skills, mobile, media, daemon, and git hook endpoints
    mounted.triggerBackendStatus({ state: "ready", apiPort: 45123 });
    await new Promise((r) => setTimeout(r, 20));
    const readyPaths = new Set(mounted.apiCalls.map((c) => c.path));
    for (const requiredPrefix of [
      "/v1/skills",
      "/v1/mobile/status",
      "/v1/media/status",
      "/v1/daemon/status",
      "/v1/git/hook",
    ]) {
      assert.ok(
        [...readyPaths].some((p) => p.startsWith(requiredPrefix)),
        `handleBackendStatus('ready') skipped loader for ${requiredPrefix}`
      );
    }

    // Seed an active note in vault A, switch view to "graph", then switch vaults via switchWorkspaceVault
    vm.runInContext(
      `
      activeNote = { filename: "secret-vault-a.md", sha256: "1234" };
      activeView = "graph";
      document.getElementById("noteTitleInput").value = "Vault A Secret Title";
      document.getElementById("noteTagsInput").value = "vault-a";
      document.getElementById("noteBodyInput").value = "Vault A private contents";
      setNoteBaseline("Vault A Secret Title", "vault-a", "Vault A private contents");
      `,
      mounted.ctx
    );
    mounted.apiCalls.length = 0;

    await vm.runInContext("switchWorkspaceVault('work-vault')", mounted.ctx);
    assert.equal(vm.runInContext("activeNote", mounted.ctx), null, "activeNote must be cleared on vault switch");
    assert.equal(mounted.doc.getElementById("noteTitleInput").value, "");
    assert.equal(mounted.doc.getElementById("noteTagsInput").value, "");
    assert.equal(mounted.doc.getElementById("noteBodyInput").value, "");
    assert.ok(
      mounted.apiCalls.some((c) => c.path.startsWith("/v1/graph")),
      "Active view ('graph') must reload after vault switch"
    );
  });

  it("desktop/main.js before-quit prevents premature exit and awaits killProcessTree before app.quit()", async () => {
    const mainSrc = fs.readFileSync(DESKTOP_MAIN_PATH, "utf8");
    const marker = "let quitCleanupComplete = false;";
    const idx = mainSrc.indexOf(marker);
    assert.ok(idx > 0, "Expected quitCleanupComplete guard in desktop/main.js");
    const snippet = mainSrc.slice(idx);

    let beforeQuitHandler = null;
    let quitCalls = 0;
    let cleanedPid = null;
    let resolveKill = null;
    const killPromise = new Promise((r) => {
      resolveKill = r;
    });

    const sandbox = {
      isQuitting: false,
      apiReady: true,
      spawnedApiPid: 4242,
      apiProcess: { pid: 4242 },
      abortActiveChatStream() {},
      globalShortcut: { unregisterAll() {} },
      killProcessTree() {
        return killPromise;
      },
      cleanupOwnedPortFile(pid) {
        cleanedPid = pid;
      },
      app: {
        on(ev, cb) {
          if (ev === "before-quit") beforeQuitHandler = cb;
        },
        quit() {
          quitCalls += 1;
        },
      },
      Promise,
    };
    vm.runInNewContext(snippet, sandbox, { filename: "desktop/main.js#before-quit" });
    assert.equal(typeof beforeQuitHandler, "function");

    // 1. First before-quit prevents immediate exit and begins awaiting killProcessTree
    let prevented1 = false;
    beforeQuitHandler({
      preventDefault() {
        prevented1 = true;
      },
    });
    assert.equal(prevented1, true, "First before-quit must call event.preventDefault()");
    assert.equal(quitCalls, 0, "app.quit() must NOT run before killProcessTree settles");
    assert.equal(cleanedPid, null);

    // 2. Duplicate before-quit while killProcessTree is STILL in-flight must ALSO preventDefault
    let prevented2 = false;
    beforeQuitHandler({
      preventDefault() {
        prevented2 = true;
      },
    });
    assert.equal(prevented2, true, "Duplicate before-quit during cleanup must call event.preventDefault()");
    assert.equal(quitCalls, 0);

    // 3. Resolve killProcessTree -> cleans owned port file and triggers app.quit()
    resolveKill();
    await new Promise((r) => setTimeout(r, 10));
    assert.equal(cleanedPid, 4242);
    assert.equal(quitCalls, 1, "app.quit() must be called once cleanup finishes");

    // 4. Subsequent before-quit after cleanup completes allows Electron to exit cleanly
    let prevented3 = false;
    beforeQuitHandler({
      preventDefault() {
        prevented3 = true;
      },
    });
    assert.equal(prevented3, false);
  });
});
