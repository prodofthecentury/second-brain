"use strict";

const { describe, it, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const os = require("os");
const path = require("path");
const {
  resolveSecurityDir,
  resolvePortFilePath,
  isPidAlive,
  readPortDiscoveryFile,
  writePortDiscoveryFileAtomic,
  ensurePortDiscoveryFile,
  removePortDiscoveryFileIfOwned,
  resolveClientApiPort,
  formatShortcut,
  buildPackagedRuntimeConfig,
  buildUvBootstrapArgs,
} = require("../lib/port_discovery");

describe("Port Discovery, Cross-Platform Shortcuts & Packaged Runtime Config", () => {
  const tempDirs = [];

  function makeTempDir() {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "sb-port-test-"));
    tempDirs.push(dir);
    return dir;
  }

  afterEach(() => {
    while (tempDirs.length) {
      const d = tempDirs.pop();
      try {
        fs.rmSync(d, { recursive: true, force: true });
      } catch {}
    }
  });

  it("resolves BRAIN_SECURITY_STATE_DIR and BRAIN_API_PORT_FILE overrides cleanly", () => {
    const tmp = makeTempDir();
    const secDir = resolveSecurityDir({
      env: { BRAIN_SECURITY_STATE_DIR: tmp },
      platform: "win32",
    });
    assert.equal(secDir, path.resolve(tmp));

    const defaultPortFile = resolvePortFilePath({
      env: { BRAIN_SECURITY_STATE_DIR: tmp },
      platform: "linux",
    });
    assert.equal(defaultPortFile, path.join(path.resolve(tmp), "api-port"));

    const explicitFile = path.join(tmp, "custom-port.json");
    assert.equal(
      resolvePortFilePath({ env: { BRAIN_API_PORT_FILE: explicitFile } }),
      path.resolve(explicitFile)
    );
  });

  it("atomically writes, reads, validates live vs stale PIDs, and removes only when PID matches", () => {
    const tmp = makeTempDir();
    const portFile = path.join(tmp, "security", "api-port");

    // Live PID = process.pid
    assert.equal(isPidAlive(process.pid), true);

    const written = writePortDiscoveryFileAtomic(portFile, {
      port: 51423,
      pid: process.pid,
      host: "127.0.0.1",
      started_at: "2026-11-18T00:00:00Z",
    });
    assert.equal(written.record.port, 51423);
    assert.equal(written.record.pid, process.pid);

    const readLive = readPortDiscoveryFile(portFile);
    assert.equal(readLive.exists, true);
    assert.equal(readLive.valid, true);
    assert.equal(readLive.alive, true);
    assert.equal(readLive.stale, false);

    // ensurePortDiscoveryFile does NOT overwrite when Python already wrote matching {port, pid}
    const ensured = ensurePortDiscoveryFile(portFile, {
      port: 51423,
      pid: process.pid,
    });
    assert.equal(ensured.alreadyPresent, true);
    assert.equal(ensured.written, false);

    // Client port discovery picks up live port file when configuredPort is unset or default (8765)
    const clientResolved = resolveClientApiPort({
      configuredPort: 0,
      env: { BRAIN_API_PORT_FILE: portFile },
    });
    assert.equal(clientResolved.port, 51423);
    assert.equal(clientResolved.source, "port_file");

    // Refuses to remove when PID belongs to another process
    const wrongPidRemove = removePortDiscoveryFileIfOwned(
      portFile,
      process.pid + 99999
    );
    assert.equal(wrongPidRemove.removed, false);
    assert.equal(fs.existsSync(portFile), true);

    // Removes cleanly when PID matches
    const ownRemove = removePortDiscoveryFileIfOwned(portFile, process.pid);
    assert.equal(ownRemove.removed, true);
    assert.equal(fs.existsSync(portFile), false);
  });

  it("detects stale dead PIDs and falls back to 8765", () => {
    const tmp = makeTempDir();
    const portFile = path.join(tmp, "api-port");
    writePortDiscoveryFileAtomic(portFile, {
      port: 59999,
      pid: 99999991,
      host: "127.0.0.1",
    });

    const staleInfo = readPortDiscoveryFile(portFile, {
      checkPid: true,
      pidChecker: () => false,
    });
    assert.equal(staleInfo.valid, true);
    assert.equal(staleInfo.alive, false);
    assert.equal(staleInfo.stale, true);

    const fallback = resolveClientApiPort({
      configuredPort: 0,
      defaultPort: 8765,
      env: { BRAIN_API_PORT_FILE: portFile },
      pidChecker: () => false,
    });
    assert.equal(fallback.port, 8765);
    assert.equal(fallback.source, "fallback");
  });

  it("never uses resourcesPath/backend as writable cwd when packaged and supports wheelhouse offline sync", () => {
    const resourcesPath = path.join("/opt", "SecondBrain", "resources");
    const userDataPath = path.join("/home", "owner", ".config", "SecondBrain");

    const cfg = buildPackagedRuntimeConfig({
      isPackaged: true,
      resourcesPath,
      userDataPath,
      devRepoRoot: "/unused-dev",
      platform: "linux",
    });

    assert.equal(cfg.backendRoot, path.join(resourcesPath, "backend"));
    assert.equal(cfg.runtimeCwd, path.resolve(userDataPath));
    assert.notEqual(
      cfg.runtimeCwd,
      cfg.backendRoot,
      "Packaged runtimeCwd must NEVER equal read-only resourcesPath/backend"
    );
    assert.equal(cfg.venvDir, path.join(path.resolve(userDataPath), "venv"));

    const resolvedCli = cfg.resolveCliArgs([
      "scripts/memory_api.py",
      "--port",
      "0",
    ]);
    assert.equal(
      resolvedCli[0],
      path.join(cfg.backendRoot, "scripts/memory_api.py")
    );

    const offlineArgs = buildUvBootstrapArgs({
      backendRoot: cfg.backendRoot,
      wheelhouseDir: cfg.wheelhouseDir,
      hasWheelhouse: true,
    });
    assert.deepEqual(offlineArgs, [
      "--directory",
      cfg.backendRoot,
      "sync",
      "--frozen",
      "--offline",
      "--find-links",
      cfg.wheelhouseDir,
    ]);

    assert.equal(
      formatShortcut("CommandOrControl+Shift+Space", "darwin"),
      "⌘⇧Space"
    );
    assert.equal(
      formatShortcut("CommandOrControl+Shift+Space", "win32"),
      "Ctrl+Shift+Space"
    );
  });
});
