"use strict";

const {
  app,
  BrowserWindow,
  Tray,
  Menu,
  globalShortcut,
  ipcMain,
  dialog,
  nativeImage,
  shell,
} = require("electron");
const path = require("path");
const fs = require("fs");
const os = require("os");
const net = require("net");
const http = require("http");
const { fileURLToPath } = require("url");
const { spawn } = require("child_process");

const {
  resolveSecurityDir,
  resolvePortFilePath,
  readSafeSecurityFile,
  readPortDiscoveryFile,
  ensurePortDiscoveryFile,
  removePortDiscoveryFileIfOwned,
  formatShortcut,
  buildPackagedRuntimeConfig,
  buildUvBootstrapSteps,
  buildUvBootstrapArgs,
} = require("./lib/port_discovery");
const { createUpdaterController } = require("./lib/updater");

const IS_SMOKE_TEST = process.argv.includes("--smoke-test");
const isSmokeTest = IS_SMOKE_TEST;
const RENDERER_DIR = path.resolve(__dirname, "renderer");
const EXPLICIT_ENV_VAULT_DIR = (process.env.BRAIN_VAULT_DIR || "").trim();
const BOOTSTRAP_TIMEOUT_MS = Math.max(
  5000,
  Number(process.env.SECOND_BRAIN_BOOTSTRAP_TIMEOUT_MS || 180000)
);
const CAPTURE_ACCELERATOR = "CommandOrControl+Shift+Space";
const ALLOWED_EXTERNAL_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);

const IDE_PRIVACY_NOTICE =
  "When this host's AI uses Second Brain tools, the recalled verified memories and notes are sent to " +
  "that host's model provider (for example Anthropic, OpenAI, or Google). Your vault itself stays on this " +
  "machine, and agents can only add risk-scanned notes; they cannot verify or approve memories.";

let mainWindow = null;
let captureWindow = null;
let tray = null;
let apiProcess = null;
let spawnedApiPid = null;
let apiPort = Number(process.env.SECOND_BRAIN_PORT || 0);
let apiReady = false;
let isQuitting = false;
let activeChatReq = null;
let activeVaultOverride = "";

let shortcutState = {
  accelerator: CAPTURE_ACCELERATOR,
  displayLabel: formatShortcut(CAPTURE_ACCELERATOR, process.platform),
  registered: false,
  warning: "",
};

let startupState = {
  phase: "starting",
  message: "Initializing Second Brain...",
  progressLines: [],
  startedAt: new Date().toISOString(),
  updatedAt: new Date().toISOString(),
};

let backendStatus = {
  state: "starting",
  message: "Initializing Second Brain...",
  apiPort: 0,
  startupState,
  shortcutRegistered: false,
  shortcutWarning: "",
};

function getSafeUserDataDir() {
  try {
    const p = app.getPath("userData");
    if (p) return path.resolve(p);
  } catch {}
  if (process.platform === "win32") {
    const base =
      process.env.APPDATA || path.join(os.homedir(), "AppData", "Roaming");
    return path.join(base, "second-brain-desktop");
  }
  if (process.platform === "darwin") {
    return path.join(
      os.homedir(),
      "Library",
      "Application Support",
      "second-brain-desktop"
    );
  }
  const cfgHome =
    process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config");
  return path.join(cfgHome, "second-brain-desktop");
}

function getRuntimeConfig() {
  return buildPackagedRuntimeConfig({
    isPackaged: Boolean(app.isPackaged),
    resourcesPath: process.resourcesPath || "",
    userDataPath: getSafeUserDataDir(),
    devRepoRoot: path.resolve(__dirname, ".."),
    platform: process.platform,
  });
}

function getBackendRoot() {
  return getRuntimeConfig().backendRoot;
}

function getSecurityDir() {
  return resolveSecurityDir({
    env: process.env,
    platform: process.platform,
    homedir: os.homedir(),
  });
}

function getPortFilePath() {
  return resolvePortFilePath({
    env: process.env,
    platform: process.platform,
    homedir: os.homedir(),
  });
}

function readTokens() {
  const secDir = getSecurityDir();
  const apiTokenPath =
    (process.env.BRAIN_MEMORY_API_TOKEN_FILE || "").trim() ||
    path.join(secDir, "memory-api-token");
  const apiAltTokenPath = path.join(secDir, "api-token");
  const ownerTokenPath =
    (process.env.BRAIN_OWNER_TOKEN_FILE || "").trim() ||
    path.join(secDir, "owner-token");
  const ownerAltTokenPath = path.join(secDir, "owner-api-token");
  const apiToken =
    (process.env.BRAIN_MEMORY_API_TOKEN || "").trim() ||
    readSafeSecurityFile(apiTokenPath) ||
    readSafeSecurityFile(apiAltTokenPath);
  const ownerToken =
    (process.env.BRAIN_OWNER_TOKEN || "").trim() ||
    (process.env.BRAIN_OWNER_API_TOKEN || "").trim() ||
    readSafeSecurityFile(ownerTokenPath) ||
    readSafeSecurityFile(ownerAltTokenPath);
  return { apiToken, ownerToken };
}

function findFreePort(preferred = 0) {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", () => {
      const srv2 = net.createServer();
      srv2.unref();
      srv2.on("error", reject);
      srv2.listen(0, "127.0.0.1", () => {
        const { port } = srv2.address();
        srv2.close(() => resolve(port));
      });
    });
    srv.listen(preferred, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function getDefaultPackagedVaultDir() {
  if (process.platform === "win32") {
    const localAppData =
      process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
    return path.join(localAppData, "SecondBrain", "brain");
  }
  if (process.platform === "darwin") {
    return path.join(
      os.homedir(),
      "Library",
      "Application Support",
      "SecondBrain",
      "brain"
    );
  }
  const dataHome =
    process.env.XDG_DATA_HOME || path.join(os.homedir(), ".local", "share");
  return path.join(dataHome, "second-brain", "brain");
}

function getVaultPointerPath() {
  return path.join(getSecurityDir(), "vault-path.txt");
}

function resolveConfiguredVaultPointer() {
  const pointerFile = getVaultPointerPath();
  const raw = readSafeSecurityFile(pointerFile, 4096);
  if (!raw) return "";
  try {
    const resolved = path.resolve(raw);
    if (fs.existsSync(resolved)) {
      const st = fs.lstatSync(resolved);
      if (!st.isDirectory() || st.isSymbolicLink()) {
        return "";
      }
    }
    return resolved;
  } catch {
    return "";
  }
}

function getVaultDir() {
  if (activeVaultOverride) {
    return activeVaultOverride;
  }
  if (IS_SMOKE_TEST && EXPLICIT_ENV_VAULT_DIR) {
    return path.resolve(EXPLICIT_ENV_VAULT_DIR);
  }
  if (!IS_SMOKE_TEST) {
    const configured = resolveConfiguredVaultPointer();
    if (configured) return configured;
  }
  if (EXPLICIT_ENV_VAULT_DIR) {
    return path.resolve(EXPLICIT_ENV_VAULT_DIR);
  }
  if (app.isPackaged) {
    return getDefaultPackagedVaultDir();
  }
  return path.join(getBackendRoot(), "brain");
}

function appendStartupProgressLine(line) {
  const cleaned = String(line || "").trim();
  if (!cleaned) return;
  startupState.progressLines = [
    ...startupState.progressLines.slice(-29),
    cleaned.slice(0, 240),
  ];
  startupState.updatedAt = new Date().toISOString();
}

function emitBackendStatus(state, message = "") {
  startupState = {
    ...startupState,
    phase: state,
    message: message || startupState.message,
    updatedAt: new Date().toISOString(),
  };
  backendStatus = {
    state,
    message,
    apiPort,
    ready: state === "ready",
    vaultDir: getVaultDir(),
    startupState,
    shortcutRegistered: shortcutState.registered,
    shortcutWarning: shortcutState.warning,
    shortcutLabel: shortcutState.displayLabel,
    platform: process.platform,
  };
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("sb:backend-status", backendStatus);
  }
  if (captureWindow && !captureWindow.isDestroyed()) {
    captureWindow.webContents.send("sb:backend-status", backendStatus);
  }
}

function notifyMainWindowRefresh() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("app:refresh");
  }
}

function resolveUvBinary() {
  const explicit = (process.env.UV_BIN || "").trim();
  if (explicit && fs.existsSync(explicit)) return explicit;
  const home = os.homedir();
  const candidates =
    process.platform === "win32"
      ? [
          path.join(home, ".local", "bin", "uv.exe"),
          path.join(home, ".cargo", "bin", "uv.exe"),
          path.join(
            process.env.LOCALAPPDATA || path.join(home, "AppData", "Local"),
            "Microsoft",
            "WinGet",
            "Links",
            "uv.exe"
          ),
          path.join(
            process.env.LOCALAPPDATA || path.join(home, "AppData", "Local"),
            "Programs",
            "uv",
            "uv.exe"
          ),
        ]
      : [
          path.join(home, ".local", "bin", "uv"),
          path.join(home, ".cargo", "bin", "uv"),
          "/opt/homebrew/bin/uv",
          "/usr/local/bin/uv",
          "/usr/bin/uv",
        ];
  for (const candidate of candidates) {
    try {
      if (fs.existsSync(candidate)) return candidate;
    } catch {}
  }
  return "uv";
}

function getVenvPythonPath() {
  return getRuntimeConfig().venvPython;
}

function buildBackendEnv() {
  const cfg = getRuntimeConfig();
  const activeVaultDir = getVaultDir();
  try {
    if (!fs.existsSync(activeVaultDir)) {
      fs.mkdirSync(activeVaultDir, { recursive: true });
    }
    if (!fs.existsSync(cfg.runtimeCwd)) {
      fs.mkdirSync(cfg.runtimeCwd, { recursive: true });
    }
  } catch {}
  const scriptsDir = path.join(cfg.backendRoot, "scripts");
  const existingPyPath = String(process.env.PYTHONPATH || "").trim();
  const env = {
    ...process.env,
    PYTHONUTF8: "1",
    PYTHONDONTWRITEBYTECODE: "1",
    BRAIN_VAULT_DIR: activeVaultDir,
    PYTHONPATH: existingPyPath
      ? `${scriptsDir}${path.delimiter}${existingPyPath}`
      : scriptsDir,
  };
  if (app.isPackaged) {
    env.UV_PROJECT_ENVIRONMENT = cfg.venvDir;
  }
  return env;
}

function cleanupOwnedPortFile(pid) {
  if (!pid) return;
  try {
    removePortDiscoveryFileIfOwned(getPortFilePath(), pid);
  } catch {}
}

function killProcessTree(proc) {
  apiReady = false;
  if (!proc) return Promise.resolve();

  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (!settled) {
        settled = true;
        clearTimeout(fallbackTimer);
        resolve();
      }
    };
    const fallbackTimer = setTimeout(finish, 3000);

    const waitClose = new Promise((resClose) => {
      if (proc.exitCode !== null || proc.signalCode !== null) {
        resClose();
        return;
      }
      proc.once("close", () => resClose());
      proc.once("exit", () => resClose());
    });

    const pid = proc.pid;
    if (process.platform === "win32" && pid && proc.exitCode === null) {
      let killerDone = false;
      const onKillerExit = () => {
        if (killerDone) return;
        killerDone = true;
        try {
          if (proc.exitCode === null) proc.kill();
        } catch {}
        waitClose.then(finish);
      };
      try {
        const killer = spawn("taskkill", ["/PID", String(pid), "/T", "/F"], {
          windowsHide: true,
          stdio: "ignore",
        });
        killer.once("close", onKillerExit);
        killer.once("error", onKillerExit);
      } catch {
        onKillerExit();
      }
      return;
    }

    try {
      if (proc.exitCode === null) {
        proc.kill("SIGTERM");
        setTimeout(() => {
          try {
            if (proc.exitCode === null) proc.kill("SIGKILL");
          } catch {}
        }, 1200);
      }
    } catch {}
    waitClose.then(finish);
  });
}

function spawnBackendCli(args) {
  const cfg = getRuntimeConfig();
  const env = buildBackendEnv();
  const resolvedArgs = cfg.resolveCliArgs(args);
  if (fs.existsSync(cfg.venvPython)) {
    return spawn(cfg.venvPython, resolvedArgs, {
      cwd: cfg.runtimeCwd,
      env,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
  }
  return spawn(
    resolveUvBinary(),
    ["--directory", cfg.backendRoot, "run", "--frozen", "--no-dev", "python", ...resolvedArgs],
    {
      cwd: cfg.runtimeCwd,
      env,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    }
  );
}

function runBackendCli(args, { timeoutMs = 60000 } = {}) {
  return new Promise((resolve, reject) => {
    const proc = spawnBackendCli(args);
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      killProcessTree(proc).finally(() => {
        reject(new Error(`Backend CLI timed out after ${timeoutMs}ms.`));
      });
    }, timeoutMs);
    proc.stdout.on("data", (d) => (stdout += d.toString("utf8")));
    proc.stderr.on("data", (d) => (stderr += d.toString("utf8")));
    proc.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
    proc.on("close", (code) => {
      clearTimeout(timer);
      if (timedOut) return;
      if (code === 0) {
        resolve({ stdout, stderr });
      } else {
        reject(
          new Error((stderr || stdout || `CLI exited with code ${code}`).trim())
        );
      }
    });
  });
}

function runBootstrapStep(cmd, args, cwd, env, { timeoutMs = BOOTSTRAP_TIMEOUT_MS, label = "Bootstrapping" } = {}) {
  return new Promise((resolve, reject) => {
    const proc = spawn(cmd, args, {
      cwd,
      env,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stderr = "";
    let lineBuf = "";
    let timedOut = false;

    const timer = setTimeout(() => {
      timedOut = true;
      const msg = `${label} timed out after ${Math.round(timeoutMs / 1000)}s.`;
      appendStartupProgressLine(msg);
      emitBackendStatus("error", msg);
      killProcessTree(proc).finally(() => reject(new Error(msg)));
    }, timeoutMs);

    const handleStreamChunk = (chunk, isErr = false) => {
      const text = chunk.toString("utf8");
      if (isErr) {
        stderr = (stderr + text).slice(-2000);
      }
      lineBuf += text;
      const lines = lineBuf.split(/\r?\n/);
      lineBuf = lines.pop() || "";
      for (const ln of lines) {
        const clean = ln.trim();
        if (clean) {
          appendStartupProgressLine(clean);
          emitBackendStatus("bootstrapping", `${label}: ${clean.slice(0, 140)}`);
        }
      }
    };

    proc.stdout.on("data", (d) => handleStreamChunk(d, false));
    proc.stderr.on("data", (d) => handleStreamChunk(d, true));
    proc.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
    proc.on("close", (code) => {
      clearTimeout(timer);
      if (timedOut) return;
      if (lineBuf.trim()) {
        appendStartupProgressLine(lineBuf.trim());
      }
      if (code === 0) {
        resolve();
      } else {
        reject(new Error((stderr || `${cmd} exited with code ${code}`).trim()));
      }
    });
  });
}

function directoryExistsSafe(dirPath) {
  try {
    const st = fs.lstatSync(dirPath);
    return st.isDirectory() && !st.isSymbolicLink();
  } catch {
    return false;
  }
}

async function ensureBackendBootstrapped() {
  const cfg = getRuntimeConfig();
  const venvPy = getVenvPythonPath();
  const bootstrapMarker = path.join(cfg.venvDir, ".sb-bootstrap-complete");
  const expectedMarker = app.isPackaged ? `${String(app.getVersion() || "1.0.0")}:py3.11:v2` : "dev";
  let markerMatches = false;
  if (fs.existsSync(venvPy)) {
    if (!app.isPackaged) {
      markerMatches = true;
    } else {
      try {
        const st = fs.lstatSync(bootstrapMarker);
        if (st.isFile() && !st.isSymbolicLink() && st.size <= 128) {
          markerMatches = fs.readFileSync(bootstrapMarker, "utf8").trim() === expectedMarker;
        }
      } catch {
        markerMatches = false;
      }
    }
  }
  let freshlySynced = false;

  if (!fs.existsSync(venvPy) || !markerMatches) {
    startupState.progressLines = [];
    emitBackendStatus(
      "bootstrapping",
      "Preparing isolated Python environment..."
    );
    const env = buildBackendEnv();
    fs.mkdirSync(cfg.runtimeCwd, { recursive: true });
    if (app.isPackaged) {
      fs.mkdirSync(path.dirname(cfg.venvDir), { recursive: true });
    }
    let hasWheelhouse = false;
    if (directoryExistsSafe(cfg.wheelhouseDir)) {
      try {
        hasWheelhouse = fs
          .readdirSync(cfg.wheelhouseDir)
          .some((name) => name.endsWith(".whl"));
      } catch {
        hasWheelhouse = false;
      }
    }
    const uvBin = resolveUvBinary();
    const strictOffline = process.env.SECOND_BRAIN_STRICT_OFFLINE === "1";
    const bootstrapSteps = buildUvBootstrapSteps({
      backendRoot: cfg.backendRoot,
      wheelhouseDir: cfg.wheelhouseDir,
      venvDir: cfg.venvDir,
      venvPython: cfg.venvPython,
      hasWheelhouse,
      strictOffline,
    });

    try {
      for (const step of bootstrapSteps) {
        await runBootstrapStep(uvBin, step.args, cfg.runtimeCwd, env, {
          timeoutMs: BOOTSTRAP_TIMEOUT_MS,
          label: step.label,
        });
      }
      freshlySynced = true;
    } catch (err) {
      if (
        hasWheelhouse &&
        err?.code !== "ENOENT" &&
        !strictOffline
      ) {
        const onlineSteps = buildUvBootstrapSteps({
          backendRoot: cfg.backendRoot,
          hasWheelhouse: false,
        });
        for (const step of onlineSteps) {
          await runBootstrapStep(uvBin, step.args, cfg.runtimeCwd, env, {
            timeoutMs: BOOTSTRAP_TIMEOUT_MS,
            label: `${step.label} (online fallback)`,
          });
        }
        freshlySynced = true;
      } else if (err && err.code === "ENOENT") {
        if (process.platform === "win32") {
          const ps1 = path.join(cfg.backendRoot, "setup.ps1");
          if (fs.existsSync(ps1)) {
            await runBootstrapStep(
              "powershell.exe",
              [
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                ps1,
                "-NoLaunch",
              ],
              cfg.runtimeCwd,
              env,
              { timeoutMs: BOOTSTRAP_TIMEOUT_MS, label: "Setup bootstrap" }
            );
            freshlySynced = true;
          } else {
            throw err;
          }
        } else {
          const sh = path.join(cfg.backendRoot, "setup.sh");
          if (fs.existsSync(sh)) {
            await runBootstrapStep("bash", [sh, "--no-launch"], cfg.runtimeCwd, env, {
              timeoutMs: BOOTSTRAP_TIMEOUT_MS,
              label: "Setup bootstrap",
            });
            freshlySynced = true;
          } else {
            throw err;
          }
        }
      } else {
        throw err;
      }
    }
    if (freshlySynced && app.isPackaged && fs.existsSync(venvPy)) {
      await runBootstrapStep(
        venvPy,
        ["-c", "import second_brain, cryptography, httpx, yaml"],
        cfg.runtimeCwd,
        env,
        { timeoutMs: 30000, label: "Verifying isolated Python runtime" }
      );
      try {
        fs.writeFileSync(bootstrapMarker, `${expectedMarker}\n`, { encoding: "utf8", mode: 0o600 });
      } catch {}
    }
  }

  const lexicalDb = path.join(getVaultDir(), ".sidecar", "lexical.sqlite3");
  if (freshlySynced || !fs.existsSync(lexicalDb)) {
    try {
      emitBackendStatus("bootstrapping", "Initializing vault sidecar index...");
      await runBackendCli(["scripts/cli.py", "install"], {
        timeoutMs: Math.min(60000, BOOTSTRAP_TIMEOUT_MS),
      });
    } catch {
      // Non-fatal fallback: memory_api.py ensure_dirs() will still initialize core folders.
    }
  }
}

function spawnMemoryApiOnce(requestedPort) {
  return new Promise((resolve, reject) => {
    const proc = spawnBackendCli([
      "scripts/memory_api.py",
      "--host",
      "127.0.0.1",
      "--port",
      String(requestedPort),
      "--ensure-tokens",
    ]);
    apiProcess = proc;
    const thisPid = proc.pid || null;

    let settled = false;
    let stdoutBuf = "";
    let stderrTail = "";
    const timeout = setTimeout(() => {
      if (!settled) {
        settled = true;
        const msg = "Timed out waiting for Second Brain API sidecar.";
        emitBackendStatus("error", msg);
        reject(new Error(msg));
      }
    }, 25000);

    const inspectReadyLine = (line) => {
      if (!line.includes("SECOND_BRAIN_API_READY") || settled) return;
      const portMatch =
        line.match(/\bport=(\d+)/) || line.match(/127\.0\.0\.1:(\d+)/);
      const boundPort =
        Number(portMatch?.[1] || 0) ||
        (requestedPort > 0 ? Number(requestedPort) : 0);
      settled = true;
      clearTimeout(timeout);
      if (!boundPort || boundPort <= 0) {
        resolve({ proc, boundPort: 0 });
        return;
      }
      apiPort = boundPort;
      spawnedApiPid = thisPid;
      apiReady = true;
      if (thisPid && thisPid > 0) {
        try {
          ensurePortDiscoveryFile(getPortFilePath(), {
            port: boundPort,
            pid: thisPid,
            host: "127.0.0.1",
          });
        } catch {}
      }
      refreshTrayMenu();
      emitBackendStatus(
        "ready",
        `Loopback Memory API ready on 127.0.0.1:${apiPort}`
      );
      resolve({ proc, boundPort });
    };

    const onStdoutChunk = (chunk) => {
      stdoutBuf += chunk.toString("utf8");
      const lines = stdoutBuf.split(/\r?\n/);
      stdoutBuf = lines.pop() || "";
      for (const ln of lines) {
        inspectReadyLine(ln);
      }
      if (!settled && stdoutBuf.includes("SECOND_BRAIN_API_READY")) {
        const portMatch = stdoutBuf.match(/\bport=(\d+)/);
        if (portMatch) inspectReadyLine(stdoutBuf);
      }
    };

    proc.stdout.on("data", onStdoutChunk);
    proc.stderr.on("data", (chunk) => {
      const text = chunk.toString("utf8");
      stderrTail = (stderrTail + text).slice(-600);
      for (const ln of text.split(/\r?\n/)) {
        if (!settled && ln.includes("SECOND_BRAIN_API_READY")) {
          inspectReadyLine(ln);
        }
      }
    });
    proc.on("error", (err) => {
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        const msg = err?.message || String(err);
        emitBackendStatus("error", `API sidecar failed to spawn: ${msg}`);
        reject(err);
      }
    });
    proc.on("close", (code) => {
      const wasReady = apiReady && apiProcess === proc;
      if (apiProcess === proc) {
        apiReady = false;
        apiProcess = null;
      }
      if (thisPid && spawnedApiPid === thisPid) {
        cleanupOwnedPortFile(thisPid);
        spawnedApiPid = null;
      }
      refreshTrayMenu();
      const detail = stderrTail.trim()
        ? ` (${stderrTail.trim().split(/\r?\n/).pop()})`
        : "";
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        const msg = `API sidecar exited early with code ${code}${detail}`;
        emitBackendStatus("error", msg);
        reject(new Error(msg));
      } else if (wasReady && !isQuitting) {
        emitBackendStatus(
          "error",
          `Memory API stopped (exit code ${code})${detail}`
        );
      }
    });
  });
}

async function startApiSidecar() {
  if (apiProcess) {
    const prev = apiProcess;
    const prevPid = spawnedApiPid || prev.pid;
    apiProcess = null;
    spawnedApiPid = null;
    await killProcessTree(prev);
    cleanupOwnedPortFile(prevPid);
  }
  apiReady = false;
  refreshTrayMenu();

  try {
    await ensureBackendBootstrapped();
  } catch (err) {
    const msg = err?.message || String(err);
    emitBackendStatus("error", `Bootstrap failed: ${msg}`);
    throw err;
  }

  const configuredPort = Number(process.env.SECOND_BRAIN_PORT || 0);
  const initialPortArg = configuredPort === 0 ? 0 : configuredPort;
  apiPort = configuredPort > 0 ? configuredPort : 0;
  emitBackendStatus(
    "starting",
    configuredPort > 0
      ? `Starting loopback Memory API on 127.0.0.1:${configuredPort}...`
      : "Starting loopback Memory API on ephemeral loopback port..."
  );

  const firstAttempt = await spawnMemoryApiOnce(initialPortArg);
  if (firstAttempt.boundPort > 0) {
    return firstAttempt.boundPort;
  }

  if (firstAttempt.proc) {
    const prevPid = firstAttempt.proc.pid;
    apiProcess = null;
    spawnedApiPid = null;
    await killProcessTree(firstAttempt.proc);
    cleanupOwnedPortFile(prevPid);
  }
  const fallbackPort = await findFreePort(0);
  const secondAttempt = await spawnMemoryApiOnce(fallbackPort);
  return secondAttempt.boundPort;
}

async function stopApiSidecar() {
  apiReady = false;
  const pidToClean = spawnedApiPid || (apiProcess && apiProcess.pid) || null;
  spawnedApiPid = null;
  if (apiProcess) {
    const proc = apiProcess;
    apiProcess = null;
    await killProcessTree(proc);
  }
  cleanupOwnedPortFile(pidToClean);
  apiPort = Number(process.env.SECOND_BRAIN_PORT || 0);
  refreshTrayMenu();
  if (!isQuitting) {
    emitBackendStatus("error", "Memory API stopped by owner.");
  }
}

const startApiServer = startApiSidecar;
const stopApiServer = stopApiSidecar;

async function chooseVaultDirectory() {
  const res = await dialog.showOpenDialog(mainWindow || undefined, {
    title: "Select Second Brain Vault Folder",
    defaultPath: getVaultDir(),
    properties: ["openDirectory", "createDirectory"],
  });
  if (res.canceled || !res.filePaths || !res.filePaths[0]) {
    return { ok: false, canceled: true, vaultDir: getVaultDir() };
  }
  const vaultDir = path.resolve(res.filePaths[0]);
  const st = fs.lstatSync(vaultDir);
  if (!st.isDirectory() || st.isSymbolicLink()) {
    throw new Error(
      "Selected vault path must be a real folder (not a symlink)."
    );
  }
  const pointerFile = getVaultPointerPath();
  const secDir = path.dirname(pointerFile);
  fs.mkdirSync(secDir, { recursive: true, mode: 0o700 });
  if (fs.lstatSync(secDir).isSymbolicLink()) {
    throw new Error("Refusing to write vault-path.txt under a symlinked security directory.");
  }
  if (fs.existsSync(pointerFile) && fs.lstatSync(pointerFile).isSymbolicLink()) {
    throw new Error("Refusing to write vault-path.txt through a symlink.");
  }
  fs.writeFileSync(pointerFile, `${vaultDir}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  activeVaultOverride = IS_SMOKE_TEST ? vaultDir : "";

  await stopApiServer();
  try {
    await startApiServer();
  } catch (err) {
    const msg = err?.message || String(err);
    emitBackendStatus(
      "error",
      `Switched vault folder, but API restart failed: ${msg}`
    );
    refreshTrayMenu();
    return { ok: false, error: msg, vaultDir };
  }
  refreshTrayMenu();
  notifyMainWindowRefresh();
  return { ok: true, vaultDir, port: apiPort };
}

const ALLOWED_HTTP_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE"]);

function apiRequest(method, apiPath, body = null) {
  const safeMethod = String(method || "").toUpperCase();
  if (!ALLOWED_HTTP_METHODS.has(safeMethod)) {
    return Promise.reject(new Error(`Disallowed HTTP method: ${String(method)}`));
  }
  if (
    typeof apiPath !== "string" ||
    !apiPath.startsWith("/v1/") ||
    /[\s\r\n]/.test(apiPath)
  ) {
    return Promise.reject(new Error(`Disallowed API path: ${String(apiPath)}`));
  }
  if (
    !apiReady ||
    !apiProcess ||
    apiProcess.exitCode !== null ||
    isQuitting ||
    !apiPort
  ) {
    return Promise.resolve({
      ok: false,
      status: 503,
      data: { error: "Memory API sidecar is not running." },
    });
  }

  const targetPort = apiPort;
  const { apiToken, ownerToken } = readTokens();
  const payload =
    body !== null && body !== undefined ? JSON.stringify(body) : null;
  const headers = {
    Host: `127.0.0.1:${targetPort}`,
    Accept: "application/json",
  };
  if (apiToken) headers["Authorization"] = `Bearer ${apiToken}`;
  if (ownerToken) headers["X-Owner-Token"] = ownerToken;
  if (payload !== null) {
    headers["Content-Type"] = "application/json";
    headers["Content-Length"] = Buffer.byteLength(payload, "utf8");
  }

  return new Promise((resolve) => {
    const req = http.request(
      {
        hostname: "127.0.0.1",
        port: targetPort,
        path: apiPath,
        method: safeMethod,
        headers,
      },
      (res) => {
        let raw = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => (raw += chunk));
        res.on("end", () => {
          let data = null;
          try {
            data = raw ? JSON.parse(raw) : {};
          } catch {
            data = { raw };
          }
          const ok = res.statusCode >= 200 && res.statusCode < 300;
          if (
            ok &&
            safeMethod === "POST" &&
            (apiPath === "/v1/vaults/switch" ||
              (apiPath === "/v1/vaults" && body && body.activate))
          ) {
            if (!IS_SMOKE_TEST) {
              activeVaultOverride = "";
            } else if (data && (data.active_vault_path || data.path)) {
              activeVaultOverride = String(data.active_vault_path || data.path);
            }
            refreshTrayMenu();
          }
          resolve({
            status: res.statusCode || 500,
            ok,
            data,
          });
        });
      }
    );
    req.setTimeout(60000, () => {
      req.destroy(new Error("API request timed out after 60s."));
    });
    req.on("error", (err) => {
      if (isQuitting || !apiReady || !apiProcess || apiProcess.exitCode !== null) {
        resolve({
          ok: false,
          status: 503,
          data: { error: "Memory API sidecar is not running." },
        });
        return;
      }
      resolve({
        ok: false,
        status: 503,
        data: { error: err?.message || "Local Memory API request failed." },
      });
    });
    if (payload !== null) req.write(payload);
    req.end();
  });
}

function abortActiveChatStream() {
  if (activeChatReq) {
    activeChatReq._cancelled = true;
    try {
      activeChatReq.destroy();
    } catch {}
    activeChatReq = null;
    return { ok: true, cancelled: true };
  }
  return { ok: true, cancelled: false };
}

function streamChat(sender, payload) {
  if (
    !apiReady ||
    !apiProcess ||
    apiProcess.exitCode !== null ||
    isQuitting ||
    !apiPort
  ) {
    const errMsg = "Memory API sidecar is not running.";
    if (sender && !sender.isDestroyed()) {
      sender.send("chat:event", {
        event: "error",
        data: { error: errMsg, status: 503 },
      });
    }
    return Promise.resolve({
      ok: false,
      status: 503,
      data: { error: errMsg },
      error: errMsg,
    });
  }
  if (activeChatReq) {
    abortActiveChatStream();
  }
  const targetPort = apiPort;
  const { apiToken, ownerToken } = readTokens();
  const rawBody = JSON.stringify(payload || {});
  const headers = {
    Host: `127.0.0.1:${targetPort}`,
    Accept: "text/event-stream",
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(rawBody, "utf8"),
  };
  if (apiToken) headers["Authorization"] = `Bearer ${apiToken}`;
  if (ownerToken) headers["X-Owner-Token"] = ownerToken;

  return new Promise((resolve) => {
    const req = http.request(
      {
        hostname: "127.0.0.1",
        port: targetPort,
        path: "/v1/chat/stream",
        method: "POST",
        headers,
      },
      (res) => {
        res.setEncoding("utf8");
        if ((res.statusCode || 500) < 200 || (res.statusCode || 500) >= 300) {
          let errRaw = "";
          res.on("data", (chunk) => (errRaw += chunk));
          res.on("end", () => {
            if (activeChatReq === req) activeChatReq = null;
            let errMsg = `HTTP ${res.statusCode || 500}`;
            try {
              const parsedErr = errRaw ? JSON.parse(errRaw) : {};
              if (parsedErr && parsedErr.error) errMsg = String(parsedErr.error);
            } catch {}
            if (!sender.isDestroyed()) {
              sender.send("chat:event", {
                event: "error",
                data: { error: errMsg, status: res.statusCode },
              });
            }
            resolve({
              ok: false,
              status: res.statusCode,
              data: { error: errMsg },
              error: errMsg,
            });
          });
          return;
        }
        let buffer = "";
        let currentEvent = "message";
        res.on("data", (chunk) => {
          buffer += chunk;
          const lines = buffer.split(/\r?\n/);
          buffer = lines.pop() || "";
          for (const line of lines) {
            if (line.startsWith("event:")) {
              currentEvent = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
              const rawData = line.slice(5).trim();
              let parsed = rawData;
              try {
                parsed = JSON.parse(rawData);
              } catch {}
              const resolvedEvent =
                currentEvent && currentEvent !== "message"
                  ? currentEvent
                  : parsed &&
                    typeof parsed === "object" &&
                    typeof parsed.type === "string"
                  ? parsed.type
                  : currentEvent;
              if (!sender.isDestroyed()) {
                sender.send("chat:event", {
                  event: resolvedEvent,
                  data: parsed,
                });
              }
              currentEvent = "message";
            }
          }
        });
        res.on("end", () => {
          if (activeChatReq === req) activeChatReq = null;
          resolve({
            ok: res.statusCode >= 200 && res.statusCode < 300,
            status: res.statusCode,
          });
        });
      }
    );

    activeChatReq = req;
    req.setTimeout(90000, () => {
      req._timedOut = true;
      req.destroy(new Error("Chat stream timed out after 90 seconds."));
    });

    req.on("error", (err) => {
      if (activeChatReq === req) activeChatReq = null;
      if (req._cancelled) {
        if (!sender.isDestroyed()) {
          sender.send("chat:event", {
            event: "cancelled",
            data: { cancelled: true },
          });
        }
        resolve({ ok: false, cancelled: true });
        return;
      }
      const errMsg = req._timedOut
        ? "Chat stream timed out after 90 seconds."
        : err?.message || String(err);
      if (!sender.isDestroyed()) {
        sender.send("chat:event", {
          event: "error",
          data: { error: errMsg },
        });
      }
      resolve({ ok: false, status: 503, data: { error: errMsg }, error: errMsg });
    });

    req.write(rawBody);
    req.end();
  });
}

function assertTrustedRenderer(event, { allowCapture = false } = {}) {
  const frameUrl = event?.senderFrame?.url || "";
  if (typeof frameUrl !== "string" || !frameUrl.startsWith("file://")) {
    throw new Error("403 Forbidden: untrusted IPC sender origin.");
  }
  let resolvedFile = "";
  try {
    resolvedFile = path.resolve(fileURLToPath(frameUrl));
  } catch {
    throw new Error("403 Forbidden: invalid IPC sender file URL.");
  }
  const normFile =
    process.platform === "win32" ? resolvedFile.toLowerCase() : resolvedFile;
  const normRendererRoot =
    (process.platform === "win32" ? RENDERER_DIR.toLowerCase() : RENDERER_DIR) +
    path.sep;
  if (!normFile.startsWith(normRendererRoot)) {
    throw new Error(
      "403 Forbidden: IPC sender must reside inside desktop/renderer/."
    );
  }
  const isFromCapture = Boolean(
    captureWindow &&
      !captureWindow.isDestroyed() &&
      event.sender === captureWindow.webContents
  );
  if (isFromCapture && !allowCapture) {
    throw new Error(
      "403 Forbidden: IPC action is restricted from Quick Capture window."
    );
  }
  return { isFromCapture };
}

function hardenWindowNavigation(win) {
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.webContents.on("will-navigate", (event) => {
    event.preventDefault();
  });
}

function showAndFocusMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    createMainWindow();
  }
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  }
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1360,
    height: 880,
    minWidth: 980,
    minHeight: 660,
    title: "Second Brain",
    backgroundColor: "#0d1117",
    show: !IS_SMOKE_TEST,
    icon: path.join(__dirname, "assets", "icon.png"),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  hardenWindowNavigation(mainWindow);
  mainWindow.webContents.on("did-finish-load", () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send("sb:backend-status", backendStatus);
    }
  });
  mainWindow.on("close", (event) => {
    if (!isQuitting && !isSmokeTest) {
      event.preventDefault();
      mainWindow.hide();
    }
  });
  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
  return mainWindow;
}

function toggleCaptureWindow() {
  if (!captureWindow || captureWindow.isDestroyed()) {
    captureWindow = new BrowserWindow({
      width: 580,
      height: 390,
      frame: false,
      resizable: false,
      alwaysOnTop: true,
      skipTaskbar: true,
      backgroundColor: "#161b22",
      webPreferences: {
        preload: path.join(__dirname, "preload.js"),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    });
    hardenWindowNavigation(captureWindow);
    captureWindow.webContents.on("did-finish-load", () => {
      if (captureWindow && !captureWindow.isDestroyed()) {
        captureWindow.webContents.send("sb:backend-status", backendStatus);
        captureWindow.webContents.send("sb:capture-focus");
      }
    });
    captureWindow.on("closed", () => {
      captureWindow = null;
    });
    captureWindow.loadFile(path.join(__dirname, "renderer", "capture.html"));
    captureWindow.center();
    captureWindow.show();
    captureWindow.focus();
    return;
  }
  if (captureWindow.isVisible()) {
    captureWindow.hide();
  } else {
    captureWindow.center();
    captureWindow.show();
    captureWindow.focus();
    captureWindow.webContents.send("sb:backend-status", backendStatus);
    captureWindow.webContents.send("sb:capture-focus");
  }
}

function refreshTrayMenu() {
  if (!tray) return;
  const statusLabel =
    apiReady && apiPort
      ? `Memory API: Running (127.0.0.1:${apiPort})`
      : backendStatus.state === "starting" ||
        backendStatus.state === "bootstrapping"
      ? "Memory API: Starting..."
      : "Memory API: Stopped";
  let openAtLogin = false;
  try {
    openAtLogin = Boolean(app.getLoginItemSettings().openAtLogin);
  } catch {}

  const shortcutDisplay = formatShortcut(CAPTURE_ACCELERATOR, process.platform);
  const template = [{ label: statusLabel, enabled: false }];
  if (shortcutState.warning) {
    template.push({
      label: `⚠ ${shortcutState.warning}`,
      enabled: false,
    });
  }
  template.push(
    { type: "separator" },
    {
      label: "Open Second Brain",
      click: () => showAndFocusMainWindow(),
    },
    {
      label: shortcutState.registered
        ? `Quick Capture (${shortcutDisplay})`
        : "Quick Capture (Shortcut unavailable)",
      click: () => toggleCaptureWindow(),
    },
    {
      label: "Open Vault Folder",
      click: () => {
        const vaultPath = getVaultDir();
        fs.mkdirSync(vaultPath, { recursive: true });
        shell.openPath(vaultPath);
      },
    },
    {
      label: "Switch Vault Folder…",
      click: () => {
        chooseVaultDirectory().catch((err) => {
          emitBackendStatus(
            "error",
            `Vault switch failed: ${err?.message || String(err)}`
          );
        });
      },
    },
    { type: "separator" },
    {
      label: apiReady ? "Restart Memory API" : "Start Memory API",
      click: async () => {
        try {
          await stopApiServer();
          await startApiServer();
          refreshTrayMenu();
          notifyMainWindowRefresh();
        } catch (err) {
          const msg = err?.message || String(err);
          emitBackendStatus("error", `Restart failed: ${msg}`);
          refreshTrayMenu();
        }
      },
    },
    {
      label: "Stop Memory API",
      enabled: Boolean(apiProcess || apiReady),
      click: () => {
        stopApiSidecar().catch(() => {});
      },
    },
    {
      label: "Launch at Login",
      type: "checkbox",
      checked: openAtLogin,
      click: (menuItem) => {
        try {
          app.setLoginItemSettings({ openAtLogin: Boolean(menuItem.checked) });
        } catch {}
        refreshTrayMenu();
      },
    },
    { type: "separator" },
    {
      label: "Quit",
      click: () => {
        isQuitting = true;
        app.quit();
      },
    }
  );

  const contextMenu = Menu.buildFromTemplate(template);
  const tipSuffix = shortcutState.warning ? ` (${shortcutState.warning})` : "";
  tray.setToolTip(`Second Brain — ${statusLabel}${tipSuffix}`);
  tray.setContextMenu(contextMenu);
}

function createTray() {
  const icon = nativeImage.createFromPath(
    path.join(__dirname, "assets", "icon.png")
  );
  const resized = icon.resize({ width: 16, height: 16 });
  if (process.platform === "darwin") {
    resized.setTemplateImage(false);
  }
  tray = new Tray(resized);
  refreshTrayMenu();
  if (process.platform === "win32") {
    tray.on("click", () => showAndFocusMainWindow());
    tray.on("double-click", () => showAndFocusMainWindow());
  } else if (process.platform !== "darwin") {
    // Linux StatusNotifier / AppIndicator single-click opens/focuses main window
    tray.on("click", () => showAndFocusMainWindow());
  }
}

function registerCaptureShortcut() {
  const displayLabel = formatShortcut(CAPTURE_ACCELERATOR, process.platform);
  let ok = false;
  try {
    ok = Boolean(
      globalShortcut.register(CAPTURE_ACCELERATOR, toggleCaptureWindow) &&
        globalShortcut.isRegistered(CAPTURE_ACCELERATOR)
    );
  } catch {
    ok = false;
  }
  shortcutState = {
    accelerator: CAPTURE_ACCELERATOR,
    displayLabel,
    registered: ok,
    warning: ok
      ? ""
      : `Quick Capture shortcut (${displayLabel}) is already in use by another app. Use Tray -> Quick Capture instead.`,
  };
  refreshTrayMenu();
  emitBackendStatus(backendStatus.state, backendStatus.message);
  return shortcutState;
}

ipcMain.handle("app:status", (event) => {
  assertTrustedRenderer(event, { allowCapture: true });
  const portFile = readPortDiscoveryFile(getPortFilePath());
  return {
    port: apiPort,
    ready: Boolean(
      apiReady && apiProcess && apiProcess.exitCode === null && !isQuitting
    ),
    apiUrl: apiPort ? `http://127.0.0.1:${apiPort}` : "",
    backendRoot: getBackendRoot(),
    runtimeCwd: getRuntimeConfig().runtimeCwd,
    vaultDir: getVaultDir(),
    platform: process.platform,
    shortcutRegistered: shortcutState.registered,
    shortcutWarning: shortcutState.warning,
    shortcutLabel: shortcutState.displayLabel,
    startupState,
    portFile: {
      path: portFile.path,
      exists: portFile.exists,
      valid: portFile.valid,
      alive: portFile.alive,
      record: portFile.record,
    },
    backendStatus,
  };
});

ipcMain.handle("api:request", async (event, payload = {}) => {
  const { isFromCapture } = assertTrustedRenderer(event, { allowCapture: true });
  const { method, path: apiPath, body } =
    payload && typeof payload === "object" ? payload : {};
  const safeMethod = String(method || "").toUpperCase();
  if (isFromCapture) {
    if (
      safeMethod !== "POST" ||
      (apiPath !== "/v1/notes" &&
        apiPath !== "/v1/inbox" &&
        apiPath !== "/v1/ingest/media")
    ) {
      throw new Error(
        "403 Forbidden: Quick Capture window may only invoke POST /v1/notes, POST /v1/inbox, or POST /v1/ingest/media."
      );
    }
  }
  return apiRequest(safeMethod, apiPath, body);
});

const handleStreamChatIpc = async (event, payload) => {
  assertTrustedRenderer(event, { allowCapture: false });
  return streamChat(event.sender, payload);
};
ipcMain.handle("chat:stream", handleStreamChatIpc);
ipcMain.handle("api:streamChat", handleStreamChatIpc);

const handleAbortChatIpc = (event) => {
  assertTrustedRenderer(event, { allowCapture: false });
  return abortActiveChatStream();
};
ipcMain.handle("chat:abort", handleAbortChatIpc);
ipcMain.handle("sb:cancel-stream", handleAbortChatIpc);

const handleSwitchVaultIpc = async (event) => {
  assertTrustedRenderer(event, { allowCapture: false });
  return chooseVaultDirectory();
};
ipcMain.handle("vault:switch", handleSwitchVaultIpc);
ipcMain.handle("sb:choose-vault", handleSwitchVaultIpc);

const handlePickFolderIpc = async (event) => {
  assertTrustedRenderer(event, { allowCapture: false });
  const res = await dialog.showOpenDialog(mainWindow || undefined, {
    properties: ["openDirectory"],
  });
  return res.canceled ? null : res.filePaths[0];
};
ipcMain.handle("dialog:pick-folder", handlePickFolderIpc);
ipcMain.handle("dialog:pickFolder", handlePickFolderIpc);

ipcMain.handle("build:export-files", async (event, payload = {}) => {
  assertTrustedRenderer(event, { allowCapture: false });
  const rawFiles = Array.isArray(payload?.files) ? payload.files : [];
  if (!rawFiles.length) {
    return { ok: false, error: "No build artifacts to export." };
  }
  if (rawFiles.length > 24) {
    return { ok: false, error: "Too many build artifact files (max 24)." };
  }
  const dlg = await dialog.showOpenDialog(mainWindow || undefined, {
    title: "Choose Folder to Export Build Artifacts",
    properties: ["openDirectory", "createDirectory"],
  });
  if (dlg.canceled || !dlg.filePaths.length) {
    return { ok: false, cancelled: true };
  }
  const WINDOWS_RESERVED_STEM_RE = /^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i;
  const baseReal = fs.realpathSync(dlg.filePaths[0]);
  const basePrefix = baseReal.endsWith(path.sep) ? baseReal : baseReal + path.sep;
  const written = [];
  for (const item of rawFiles) {
    const rawRel = String(item?.filename || "")
      .replace(/\\/g, "/")
      .replace(/^\/+|\/+$/g, "")
      .trim();
    if (!rawRel || rawRel.includes("\0") || /^[A-Za-z]:/.test(rawRel)) {
      throw new Error(`Invalid relative artifact path: ${rawRel}`);
    }
    const segs = rawRel.split("/").filter(Boolean);
    if (
      !segs.length ||
      segs.some(
        (s) =>
          s === "." ||
          s === ".." ||
          /[^A-Za-z0-9._-]/.test(s) ||
          s.endsWith(".") ||
          WINDOWS_RESERVED_STEM_RE.test(s)
      )
    ) {
      throw new Error(`Unsafe artifact path segment in: ${rawRel}`);
    }
    const contentStr = String(item?.content ?? "");
    if (Buffer.byteLength(contentStr, "utf8") > 1_000_000) {
      throw new Error(`Artifact exceeds 1 MB limit: ${rawRel}`);
    }
    const targetPath = path.resolve(baseReal, ...segs);
    if (!targetPath.startsWith(basePrefix)) {
      throw new Error(`Resolved path escapes target directory: ${rawRel}`);
    }
    let cur = baseReal;
    for (const dirSeg of segs.slice(0, -1)) {
      cur = path.join(cur, dirSeg);
      if (fs.existsSync(cur)) {
        if (fs.lstatSync(cur).isSymbolicLink()) {
          throw new Error(`Refusing symlinked export subdirectory: ${cur}`);
        }
      } else {
        fs.mkdirSync(cur);
      }
    }
    if (fs.existsSync(targetPath) && fs.lstatSync(targetPath).isSymbolicLink()) {
      throw new Error(`Refusing symlinked export file: ${targetPath}`);
    }
    fs.writeFileSync(targetPath, contentStr, "utf8");
    written.push(targetPath);
  }
  return { ok: true, directory: baseReal, written };
});

ipcMain.handle("dialog:pick-save-backup", async (event) => {
  assertTrustedRenderer(event, { allowCapture: false });
  const stamp = new Date().toISOString().slice(0, 10);
  const res = await dialog.showSaveDialog(mainWindow || undefined, {
    title: "Save Portable Second Brain Backup (.sbvault)",
    defaultPath: `second-brain-backup-${stamp}.sbvault`,
    filters: [{ name: "Second Brain Vault Backup", extensions: ["sbvault"] }],
  });
  return res.canceled ? null : res.filePath;
});

ipcMain.handle("dialog:pick-backup-file", async (event) => {
  assertTrustedRenderer(event, { allowCapture: false });
  const res = await dialog.showOpenDialog(mainWindow || undefined, {
    title: "Select Second Brain Backup Archive (.sbvault)",
    properties: ["openFile"],
    filters: [{ name: "Second Brain Vault Backup", extensions: ["sbvault"] }],
  });
  return res.canceled ? null : res.filePaths[0];
});

ipcMain.handle("dialog:pick-file", async (event) => {
  assertTrustedRenderer(event, { allowCapture: false });
  const res = await dialog.showOpenDialog(mainWindow || undefined, {
    title: "Select Document to Import into Second Brain",
    properties: ["openFile"],
    filters: [
      {
        name: "Supported Documents",
        extensions: [
          "md",
          "markdown",
          "txt",
          "pdf",
          "docx",
          "epub",
          "html",
          "htm",
          "csv",
          "tsv",
        ],
      },
    ],
  });
  return res.canceled || !res.filePaths || !res.filePaths[0]
    ? null
    : res.filePaths[0];
});

ipcMain.handle("shell:open-external", async (event, payload = {}) => {
  assertTrustedRenderer(event, { allowCapture: false });
  const rawUrl =
    typeof payload === "string"
      ? payload
      : payload && typeof payload === "object"
      ? String(payload.url || "")
      : "";
  const trimmed = rawUrl.trim();
  if (!trimmed || trimmed.length > 2048 || /[\u0000-\u001f\u007f]/.test(trimmed)) {
    throw new Error("Blocked invalid external URL.");
  }
  let parsed;
  try {
    parsed = new URL(trimmed);
  } catch {
    throw new Error("Blocked malformed external URL.");
  }
  if (!ALLOWED_EXTERNAL_PROTOCOLS.has(parsed.protocol)) {
    throw new Error(
      `Blocked disallowed external URL protocol: ${parsed.protocol}`
    );
  }
  await shell.openExternal(parsed.toString());
  return { ok: true };
});

const ALLOWED_IDE_ACTIONS = new Set(["status", "add", "remove", "custom-add", "custom-remove"]);
const IDE_TARGET_RE = /^[a-z0-9_-]{2,48}$/;

ipcMain.handle("ide:action", async (event, payload = {}) => {
  assertTrustedRenderer(event, { allowCapture: false });
  const safePayload = payload && typeof payload === "object" ? payload : {};
  const { action, target } = safePayload;
  const options =
    safePayload.options && typeof safePayload.options === "object"
      ? safePayload.options
      : safePayload;
  const confirmPrivacy = options?.confirmPrivacy === true;

  if (!ALLOWED_IDE_ACTIONS.has(action)) {
    throw new Error(`Disallowed IDE action: ${String(action)}`);
  }
  if (action === "add" || action === "remove" || action === "custom-add" || action === "custom-remove") {
    if (
      typeof target !== "string" ||
      target.startsWith("-") ||
      !IDE_TARGET_RE.test(target)
    ) {
      throw new Error(`Invalid IDE target: ${String(target)}`);
    }
  } else if (target !== undefined && target !== null && target !== "") {
    if (
      typeof target !== "string" ||
      target.startsWith("-") ||
      !IDE_TARGET_RE.test(target)
    ) {
      throw new Error(`Invalid IDE target: ${String(target)}`);
    }
  }
  if (action === "add" && !confirmPrivacy) {
    throw new Error(
      "Privacy notice confirmation is required before connecting an external IDE host."
    );
  }
  const args = ["scripts/ide_connector.py", action];
  if (target && action !== "status") args.push(target);
  if (action === "add" && options?.confirmPrivacy === true) {
    args.push("--yes");
  }
  if (action === "custom-add") {
    const cfgPath = String(options?.configPath || "").trim();
    if (!cfgPath || cfgPath.includes("\0")) {
      throw new Error("configPath is required for custom-add.");
    }
    if (options?.label) {
      args.push("--label", String(options.label).slice(0, 120));
    }
    args.push("--config-path", cfgPath);
    if (options?.serversKey) {
      args.push("--servers-key", String(options.serversKey).slice(0, 64));
    }
    if (options?.configFormat) {
      args.push("--format", String(options.configFormat).slice(0, 16));
    }
  }
  args.push("--json");
  const { stdout } = await runBackendCli(args);
  const parsed = JSON.parse(stdout);
  if (action === "status" && parsed && typeof parsed === "object") {
    parsed.privacy_notice = IDE_PRIVACY_NOTICE;
  }
  return parsed;
});

ipcMain.handle("capture:close", (event) => {
  assertTrustedRenderer(event, { allowCapture: true });
  if (captureWindow && !captureWindow.isDestroyed()) {
    captureWindow.hide();
  }
});

ipcMain.handle("capture:saved", (event) => {
  assertTrustedRenderer(event, { allowCapture: true });
  notifyMainWindowRefresh();
});

const updaterController = createUpdaterController({
  app,
  emitStatus: (st) => {
    try {
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("updater:event", st);
      }
    } catch {}
  },
  onBeforeQuitInstall: async () => {
    isQuitting = true;
    abortActiveChatStream();
    await stopApiSidecar();
  },
});

ipcMain.handle("updater:status", (event) => {
  assertTrustedRenderer(event, { allowCapture: false });
  return updaterController.getStatus();
});

ipcMain.handle("updater:check", async (event) => {
  assertTrustedRenderer(event, { allowCapture: false });
  return updaterController.checkForUpdates();
});

ipcMain.handle("updater:download", async (event) => {
  assertTrustedRenderer(event, { allowCapture: false });
  return updaterController.downloadUpdate();
});

ipcMain.handle("updater:install", async (event) => {
  assertTrustedRenderer(event, { allowCapture: false });
  return updaterController.quitAndInstallUpdate();
});

app.on("activate", () => {
  if (!IS_SMOKE_TEST) {
    showAndFocusMainWindow();
  }
});

app.whenReady().then(async () => {
  if (IS_SMOKE_TEST) {
    try {
      await startApiSidecar();
    } catch (err) {
      console.error("Failed to start API sidecar:", err);
    }
    const win = createMainWindow();
    await new Promise((resolve) => {
      if (!win || win.isDestroyed() || !win.webContents.isLoading()) {
        resolve();
      } else {
        win.webContents.once("did-finish-load", resolve);
      }
    });
    const health = await apiRequest("GET", "/v1/health").catch((e) => ({
      ok: false,
      error: String(e),
    }));
    const apiReadyBeforeCleanup = apiReady;
    const portInfo = readPortDiscoveryFile(getPortFilePath());
    console.log(
      "ELECTRON_SMOKE_RESULT",
      JSON.stringify({
        apiPort,
        apiReady: apiReadyBeforeCleanup,
        health,
        portFile: {
          path: portInfo.path,
          exists: portInfo.exists,
          valid: portInfo.valid,
          alive: portInfo.alive,
          record: portInfo.record,
        },
      })
    );
    isQuitting = true;
    apiReady = false;
    abortActiveChatStream();
    const pidToClean = spawnedApiPid || (apiProcess && apiProcess.pid) || null;
    spawnedApiPid = null;
    if (apiProcess) {
      const proc = apiProcess;
      apiProcess = null;
      await killProcessTree(proc);
    }
    cleanupOwnedPortFile(pidToClean);
    const smokeOk = Boolean(apiReadyBeforeCleanup && health && health.ok);
    app.exit(smokeOk ? 0 : 1);
    return;
  }

  createMainWindow();
  createTray();
  registerCaptureShortcut();

  startApiSidecar()
    .then(() => {
      notifyMainWindowRefresh();
    })
    .catch((err) => {
      console.error("Failed to start API sidecar:", err);
      emitBackendStatus("error", err?.message || String(err));
    });
});

let quitCleanupComplete = false;
let quitCleanupInFlight = false;

app.on("before-quit", (event) => {
  if (quitCleanupComplete) {
    return;
  }
  if (quitCleanupInFlight) {
    event.preventDefault();
    return;
  }
  isQuitting = true;
  apiReady = false;
  abortActiveChatStream();
  globalShortcut.unregisterAll();
  const pidToClean = spawnedApiPid || (apiProcess && apiProcess.pid) || null;
  if (apiProcess) {
    event.preventDefault();
    quitCleanupInFlight = true;
    spawnedApiPid = null;
    const proc = apiProcess;
    apiProcess = null;
    Promise.resolve()
      .then(() => killProcessTree(proc))
      .catch(() => {})
      .finally(() => {
        cleanupOwnedPortFile(pidToClean);
        quitCleanupComplete = true;
        quitCleanupInFlight = false;
        app.quit();
      });
  } else {
    spawnedApiPid = null;
    cleanupOwnedPortFile(pidToClean);
    quitCleanupComplete = true;
  }
});
