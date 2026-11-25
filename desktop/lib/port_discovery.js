"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");

const LOOPBACK_HOST = "127.0.0.1";
const DEFAULT_API_PORT = 8765;
const DEFAULT_PYTHON_VERSION = "3.11";

function resolveSecurityDir({
  env = process.env,
  platform = process.platform,
  homedir = os.homedir(),
} = {}) {
  const explicitDir = String(env.BRAIN_SECURITY_STATE_DIR || "").trim();
  if (explicitDir) {
    return path.resolve(explicitDir);
  }
  if (platform === "win32") {
    const localAppData =
      String(env.LOCALAPPDATA || "").trim() ||
      path.join(homedir, "AppData", "Local");
    return path.join(localAppData, "SecondBrain", "security");
  }
  const stateHome =
    String(env.XDG_STATE_HOME || "").trim() ||
    path.join(homedir, ".local", "state");
  return path.join(stateHome, "SecondBrain", "security");
}

function resolvePortFilePath({
  env = process.env,
  platform = process.platform,
  homedir = os.homedir(),
} = {}) {
  const explicitFile = String(env.BRAIN_API_PORT_FILE || "").trim();
  if (explicitFile) {
    return path.resolve(explicitFile);
  }
  return path.join(resolveSecurityDir({ env, platform, homedir }), "api-port");
}

function readSafeSecurityFile(filePath, maxBytes = 4096) {
  try {
    const resolved = path.resolve(String(filePath || ""));
    const parentStat = fs.lstatSync(path.dirname(resolved));
    if (!parentStat.isDirectory() || parentStat.isSymbolicLink()) {
      return "";
    }
    const st = fs.lstatSync(resolved);
    if (!st.isFile() || st.isSymbolicLink() || st.size > maxBytes) {
      return "";
    }
    return fs.readFileSync(resolved, "utf8").trim();
  } catch {
    return "";
  }
}

function isPidAlive(pid) {
  const num = Number(pid);
  if (!Number.isInteger(num) || num <= 0) {
    return false;
  }
  try {
    process.kill(num, 0);
    return true;
  } catch (err) {
    if (err && err.code === "EPERM") {
      return true;
    }
    return false;
  }
}

function readPortDiscoveryFile(
  filePath,
  { checkPid = true, pidChecker = isPidAlive } = {}
) {
  const resolved = path.resolve(String(filePath || ""));
  const raw = readSafeSecurityFile(resolved, 4096);
  if (!raw) {
    let exists = false;
    try {
      exists = fs.existsSync(resolved);
    } catch {}
    return {
      path: resolved,
      exists,
      valid: false,
      alive: false,
      stale: false,
      record: null,
    };
  }
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") {
      return {
        path: resolved,
        exists: true,
        valid: false,
        alive: false,
        stale: false,
        record: null,
      };
    }
    const port = Number(parsed.port);
    const pid = Number(parsed.pid);
    const host = String(parsed.host || LOOPBACK_HOST).trim();
    const started_at = String(parsed.started_at || "").trim();
    if (
      host !== LOOPBACK_HOST ||
      !Number.isInteger(port) ||
      port < 1 ||
      port > 65535 ||
      !Number.isInteger(pid) ||
      pid <= 0
    ) {
      return {
        path: resolved,
        exists: true,
        valid: false,
        alive: false,
        stale: false,
        record: null,
      };
    }
    const record = { port, pid, host, started_at };
    const alive = checkPid ? Boolean(pidChecker(pid)) : true;
    return {
      path: resolved,
      exists: true,
      valid: true,
      alive,
      stale: !alive,
      record,
    };
  } catch {
    return {
      path: resolved,
      exists: true,
      valid: false,
      alive: false,
      stale: false,
      record: null,
    };
  }
}

function writePortDiscoveryFileAtomic(
  filePath,
  { port, pid, host = LOOPBACK_HOST, started_at = new Date().toISOString() } = {}
) {
  const resolved = path.resolve(String(filePath || ""));
  const cleanPort = Number(port);
  const cleanPid = Number(pid);
  const cleanHost = String(host || LOOPBACK_HOST).trim();
  if (cleanHost !== LOOPBACK_HOST) {
    throw new Error(`Refusing non-loopback host in api-port file: ${cleanHost}`);
  }
  if (!Number.isInteger(cleanPort) || cleanPort < 1 || cleanPort > 65535) {
    throw new Error(`Invalid port for api-port file: ${String(port)}`);
  }
  if (!Number.isInteger(cleanPid) || cleanPid <= 0) {
    throw new Error(`Invalid pid for api-port file: ${String(pid)}`);
  }

  const parentDir = path.dirname(resolved);
  fs.mkdirSync(parentDir, { recursive: true, mode: 0o700 });
  const parentStat = fs.lstatSync(parentDir);
  if (!parentStat.isDirectory() || parentStat.isSymbolicLink()) {
    throw new Error("Refusing to write api-port under a symlinked directory.");
  }
  if (fs.existsSync(resolved) && fs.lstatSync(resolved).isSymbolicLink()) {
    throw new Error("Refusing to overwrite symlinked api-port file.");
  }

  const record = {
    port: cleanPort,
    pid: cleanPid,
    host: cleanHost,
    started_at: String(started_at || new Date().toISOString()),
  };
  const payload = `${JSON.stringify(record)}\n`;
  const nonce = crypto.randomBytes(6).toString("hex");
  const tmpPath = path.join(
    parentDir,
    `.api-port-${process.pid}-${Date.now()}-${nonce}.tmp`
  );

  let fd = null;
  try {
    fd = fs.openSync(tmpPath, "wx", 0o600);
    fs.writeFileSync(fd, payload, "utf8");
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fd = null;
    fs.renameSync(tmpPath, resolved);
  } finally {
    if (fd !== null) {
      try {
        fs.closeSync(fd);
      } catch {}
    }
    if (fs.existsSync(tmpPath)) {
      try {
        fs.unlinkSync(tmpPath);
      } catch {}
    }
  }
  return { path: resolved, record };
}

function ensurePortDiscoveryFile(
  filePath,
  { port, pid, host = LOOPBACK_HOST, started_at } = {},
  { pidChecker = isPidAlive } = {}
) {
  const existing = readPortDiscoveryFile(filePath, {
    checkPid: true,
    pidChecker,
  });
  if (
    existing.valid &&
    existing.alive &&
    existing.record.port === Number(port) &&
    existing.record.pid === Number(pid)
  ) {
    return {
      written: false,
      alreadyPresent: true,
      path: existing.path,
      record: existing.record,
    };
  }
  const written = writePortDiscoveryFileAtomic(filePath, {
    port,
    pid,
    host,
    started_at,
  });
  return {
    written: true,
    alreadyPresent: false,
    path: written.path,
    record: written.record,
  };
}

function removePortDiscoveryFileIfOwned(filePath, expectedPid) {
  const resolved = path.resolve(String(filePath || ""));
  const pidNum = Number(expectedPid);
  if (!Number.isInteger(pidNum) || pidNum <= 0) {
    return { removed: false, reason: "invalid_expected_pid", path: resolved };
  }
  const existing = readPortDiscoveryFile(resolved, { checkPid: false });
  if (!existing.exists) {
    return { removed: false, reason: "missing", path: resolved };
  }
  if (!existing.valid || existing.record.pid !== pidNum) {
    return { removed: false, reason: "pid_mismatch", path: resolved };
  }
  try {
    if (!fs.lstatSync(resolved).isSymbolicLink()) {
      fs.unlinkSync(resolved);
      return { removed: true, reason: "removed", path: resolved };
    }
  } catch (err) {
    if (err && err.code === "ENOENT") {
      return { removed: false, reason: "missing", path: resolved };
    }
  }
  return { removed: false, reason: "unlink_failed", path: resolved };
}

function resolveClientApiPort({
  configuredPort = 0,
  defaultPort = DEFAULT_API_PORT,
  env = process.env,
  platform = process.platform,
  homedir = os.homedir(),
  pidChecker = isPidAlive,
} = {}) {
  const explicitNum = Number(configuredPort || 0);
  const portFilePath = resolvePortFilePath({ env, platform, homedir });
  if (
    Number.isInteger(explicitNum) &&
    explicitNum > 0 &&
    explicitNum !== defaultPort
  ) {
    return { port: explicitNum, source: "configured", path: portFilePath };
  }
  const discovered = readPortDiscoveryFile(portFilePath, {
    checkPid: true,
    pidChecker,
  });
  if (discovered.valid && discovered.alive) {
    return {
      port: discovered.record.port,
      source: "port_file",
      path: portFilePath,
      record: discovered.record,
    };
  }
  return {
    port: explicitNum > 0 ? explicitNum : defaultPort,
    source: "fallback",
    path: portFilePath,
    staleRecord: discovered.stale ? discovered.record : null,
  };
}

function isMacPlatform(platform = process.platform) {
  return platform === "darwin";
}

function modKeyLabel(platform = process.platform) {
  return isMacPlatform(platform) ? "⌘" : "Ctrl+";
}

function formatShortcut(combo, platform = process.platform) {
  const isMac = isMacPlatform(platform);
  const raw = String(combo || "");
  if (!isMac) {
    return raw.replace(/CommandOrControl/g, "Ctrl");
  }
  return raw
    .replace(/CommandOrControl\+Shift\+/g, "⌘⇧")
    .replace(/Ctrl\+Shift\+/g, "⌘⇧")
    .replace(/CommandOrControl\+/g, "⌘")
    .replace(/Ctrl\+/g, "⌘");
}

function buildPackagedRuntimeConfig({
  isPackaged = false,
  resourcesPath = "",
  userDataPath = "",
  devRepoRoot = "",
  platform = process.platform,
} = {}) {
  const backendRoot = isPackaged
    ? path.join(resourcesPath, "backend")
    : path.resolve(devRepoRoot);
  const runtimeCwd = isPackaged
    ? path.resolve(userDataPath)
    : path.resolve(devRepoRoot);
  const venvDir = isPackaged
    ? path.join(runtimeCwd, "venv")
    : path.join(backendRoot, ".venv");
  const venvPython =
    platform === "win32"
      ? path.join(venvDir, "Scripts", "python.exe")
      : path.join(venvDir, "bin", "python");
  const wheelhouseDir = path.join(backendRoot, "wheelhouse");

  const resolveCliArgs = (args = []) =>
    args.map((arg, idx) => {
      if (
        idx === 0 &&
        typeof arg === "string" &&
        !path.isAbsolute(arg) &&
        (arg.startsWith("scripts/") || arg.startsWith("scripts\\"))
      ) {
        return path.join(backendRoot, arg);
      }
      return arg;
    });

  return {
    backendRoot,
    runtimeCwd,
    venvDir,
    venvPython,
    wheelhouseDir,
    resolveCliArgs,
  };
}

function buildUvBootstrapSteps({
  backendRoot,
  wheelhouseDir,
  venvDir,
  venvPython,
  hasWheelhouse = false,
  pythonVersion = DEFAULT_PYTHON_VERSION,
  strictOffline = false,
} = {}) {
  const pyVer = String(pythonVersion || DEFAULT_PYTHON_VERSION);
  const resolvedWheelhouse =
    wheelhouseDir || path.join(String(backendRoot || ""), "wheelhouse");
  if (hasWheelhouse) {
    const resolvedVenvDir =
      venvDir || path.join(String(backendRoot || ""), ".venv");
    const resolvedVenvPython =
      venvPython ||
      (process.platform === "win32"
        ? path.join(resolvedVenvDir, "Scripts", "python.exe")
        : path.join(resolvedVenvDir, "bin", "python"));
    const venvArgs = [
      "venv",
      String(resolvedVenvDir),
      "--python",
      pyVer,
      "--clear",
    ];
    if (strictOffline) {
      venvArgs.push("--offline");
    }
    return [
      {
        label: "Creating isolated Python runtime",
        args: venvArgs,
      },
      {
        label: "Offline wheelhouse install",
        args: [
          "pip",
          "install",
          "--offline",
          "--no-index",
          "--find-links",
          String(resolvedWheelhouse),
          "--python",
          String(resolvedVenvPython),
          "second-brain",
        ],
      },
    ];
  }
  return [
    {
      label: "Python env sync",
      args: [
        "--directory",
        String(backendRoot),
        "sync",
        "--frozen",
        "--no-dev",
        "--python",
        pyVer,
      ],
    },
  ];
}

function buildUvBootstrapArgs({
  backendRoot,
  wheelhouseDir,
  venvPython,
  hasWheelhouse = false,
  pythonVersion = DEFAULT_PYTHON_VERSION,
} = {}) {
  const pyVer = String(pythonVersion || DEFAULT_PYTHON_VERSION);
  const resolvedWheelhouse =
    wheelhouseDir || path.join(String(backendRoot || ""), "wheelhouse");
  if (hasWheelhouse) {
    const args = [
      "pip",
      "install",
      "--offline",
      "--no-index",
      "--find-links",
      String(resolvedWheelhouse),
    ];
    if (venvPython) {
      args.push("--python", String(venvPython));
    }
    args.push("second-brain");
    return args;
  }
  return [
    "--directory",
    String(backendRoot),
    "sync",
    "--frozen",
    "--no-dev",
    "--python",
    pyVer,
  ];
}

module.exports = {
  LOOPBACK_HOST,
  DEFAULT_API_PORT,
  DEFAULT_PYTHON_VERSION,
  resolveSecurityDir,
  resolvePortFilePath,
  readSafeSecurityFile,
  isPidAlive,
  readPortDiscoveryFile,
  writePortDiscoveryFileAtomic,
  ensurePortDiscoveryFile,
  removePortDiscoveryFileIfOwned,
  resolveClientApiPort,
  isMacPlatform,
  modKeyLabel,
  formatShortcut,
  buildPackagedRuntimeConfig,
  buildUvBootstrapSteps,
  buildUvBootstrapArgs,
};
