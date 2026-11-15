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
const { spawn } = require("child_process");

const isSmokeTest = process.argv.includes("--smoke-test");

let mainWindow = null;
let captureWindow = null;
let tray = null;
let apiProcess = null;
let apiPort = Number(process.env.SECOND_BRAIN_PORT || 0);
let apiReady = false;
let isQuitting = false;

function getBackendRoot() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "backend");
  }
  return path.resolve(__dirname, "..");
}

function getSecurityDir() {
  if (process.platform === "win32") {
    const localAppData =
      process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
    return path.join(localAppData, "SecondBrain", "security");
  }
  const stateHome =
    process.env.XDG_STATE_HOME || path.join(os.homedir(), ".local", "state");
  return path.join(stateHome, "SecondBrain", "security");
}

function readSafeTokenFile(filePath) {
  try {
    const st = fs.lstatSync(filePath);
    if (!st.isFile() || st.isSymbolicLink() || st.size > 4096) {
      return "";
    }
    return fs.readFileSync(filePath, "utf8").trim();
  } catch {
    return "";
  }
}

function readTokens() {
  const secDir = getSecurityDir();
  const apiTokenPath = (process.env.BRAIN_MEMORY_API_TOKEN_FILE || "").trim()
    || path.join(secDir, "memory-api-token");
  const ownerTokenPath = (process.env.BRAIN_OWNER_TOKEN_FILE || "").trim()
    || path.join(secDir, "owner-token");
  const apiToken = (process.env.BRAIN_MEMORY_API_TOKEN || "").trim()
    || readSafeTokenFile(apiTokenPath);
  const ownerToken = readSafeTokenFile(ownerTokenPath);
  return { apiToken, ownerToken };
}

function findFreePort(preferred = 8765) {
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
    return path.join(os.homedir(), "Library", "Application Support", "SecondBrain", "brain");
  }
  const dataHome =
    process.env.XDG_DATA_HOME || path.join(os.homedir(), ".local", "share");
  return path.join(dataHome, "second-brain", "brain");
}

function getPackagedVenvDir() {
  return path.join(path.dirname(getDefaultPackagedVaultDir()), "venv");
}

function resolveConfiguredVaultPointer() {
  const pointerFile = path.join(path.dirname(getDefaultPackagedVaultDir()), "vault-path.txt");
  const raw = readSafeTokenFile(pointerFile);
  if (!raw) return "";
  try {
    const resolved = path.resolve(raw);
    const st = fs.lstatSync(resolved);
    if (st.isDirectory() && !st.isSymbolicLink()) {
      return resolved;
    }
  } catch {}
  return "";
}

function resolveUvBinary() {
  const explicit = (process.env.UV_BIN || "").trim();
  if (explicit && fs.existsSync(explicit)) return explicit;
  const home = os.homedir();
  const candidates = process.platform === "win32"
    ? [
        path.join(home, ".local", "bin", "uv.exe"),
        path.join(home, ".cargo", "bin", "uv.exe"),
        path.join(process.env.LOCALAPPDATA || path.join(home, "AppData", "Local"), "Microsoft", "WinGet", "Links", "uv.exe"),
        path.join(process.env.LOCALAPPDATA || path.join(home, "AppData", "Local"), "Programs", "uv", "uv.exe"),
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
  const venvRoot = app.isPackaged ? getPackagedVenvDir() : path.join(getBackendRoot(), ".venv");
  return process.platform === "win32"
    ? path.join(venvRoot, "Scripts", "python.exe")
    : path.join(venvRoot, "bin", "python");
}

function buildBackendEnv() {
  const env = { ...process.env, PYTHONUTF8: "1" };
  if (app.isPackaged) {
    env.UV_PROJECT_ENVIRONMENT = getPackagedVenvDir();
    if (!env.BRAIN_VAULT_DIR) {
      const configuredVault = resolveConfiguredVaultPointer() || getDefaultPackagedVaultDir();
      fs.mkdirSync(configuredVault, { recursive: true });
      env.BRAIN_VAULT_DIR = configuredVault;
    }
  }
  return env;
}

function killProcessTree(proc) {
  if (!proc) return;
  const pid = proc.pid;
  if (process.platform === "win32" && pid) {
    try {
      spawn("taskkill", ["/PID", String(pid), "/T", "/F"], {
        windowsHide: true,
        stdio: "ignore",
      });
    } catch (_) {}
  }
  try {
    proc.kill();
  } catch (_) {}
}

function spawnBackendCli(args) {
  const root = getBackendRoot();
  const env = buildBackendEnv();
  const venvPy = getVenvPythonPath();
  if (fs.existsSync(venvPy)) {
    return spawn(venvPy, args, {
      cwd: root,
      env,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
  }
  return spawn(resolveUvBinary(), ["--directory", root, "run", "--frozen", "python", ...args], {
    cwd: root,
    env,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
}

function runBackendCli(args) {
  return new Promise((resolve, reject) => {
    const proc = spawnBackendCli(args);
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d.toString("utf8")));
    proc.stderr.on("data", (d) => (stderr += d.toString("utf8")));
    proc.on("error", reject);
    proc.on("close", (code) => {
      if (code === 0) {
        resolve({ stdout, stderr });
      } else {
        reject(new Error(stderr || stdout || `CLI exited with code ${code}`));
      }
    });
  });
}

function runSpawnPromise(cmd, args, cwd, env) {
  return new Promise((resolve, reject) => {
    const proc = spawn(cmd, args, { cwd, env, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
    let stderr = "";
    proc.stderr.on("data", (d) => (stderr += d.toString("utf8")));
    proc.on("error", reject);
    proc.on("close", (code) => {
      if (code === 0) resolve();
      else reject(new Error(stderr || `${cmd} exited with code ${code}`));
    });
  });
}

async function ensureBackendBootstrapped() {
  const root = getBackendRoot();
  const venvPy = getVenvPythonPath();
  if (fs.existsSync(venvPy)) {
    return;
  }
  const env = buildBackendEnv();
  if (app.isPackaged) {
    fs.mkdirSync(path.dirname(getPackagedVenvDir()), { recursive: true });
  }
  try {
    await runSpawnPromise(resolveUvBinary(), ["--directory", root, "sync", "--frozen"], root, env);
  } catch (err) {
    if (err && err.code === "ENOENT") {
      if (process.platform === "win32") {
        const ps1 = path.join(root, "setup.ps1");
        if (fs.existsSync(ps1)) {
          await runSpawnPromise(
            "powershell.exe",
            ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1, "-NoLaunch"],
            root,
            env
          );
        }
      } else {
        const sh = path.join(root, "setup.sh");
        if (fs.existsSync(sh)) {
          await runSpawnPromise("bash", [sh, "--no-launch"], root, env);
        }
      }
    } else {
      throw err;
    }
  }
}

async function startApiSidecar() {
  await ensureBackendBootstrapped();
  if (!apiPort) {
    apiPort = await findFreePort(8765);
  }
  apiReady = false;
  if (apiProcess) {
    killProcessTree(apiProcess);
    apiProcess = null;
  }

  return new Promise((resolve, reject) => {
    const proc = spawnBackendCli([
      "scripts/memory_api.py",
      "--port",
      String(apiPort),
      "--ensure-tokens",
    ]);
    apiProcess = proc;

    let settled = false;
    const timeout = setTimeout(() => {
      if (!settled) {
        settled = true;
        reject(new Error("Timed out waiting for Second Brain API sidecar."));
      }
    }, 20000);

    const checkLine = (chunk) => {
      const text = chunk.toString("utf8");
      if (text.includes("SECOND_BRAIN_API_READY") && !settled) {
        settled = true;
        apiReady = true;
        clearTimeout(timeout);
        refreshTrayMenu();
        resolve(apiPort);
      }
    };

    proc.stdout.on("data", checkLine);
    proc.stderr.on("data", (chunk) => {
      if (!settled && chunk.toString("utf8").includes("SECOND_BRAIN_API_READY")) {
        checkLine(chunk);
      }
    });
    proc.on("error", (err) => {
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        reject(err);
      }
    });
    proc.on("close", (code) => {
      apiReady = false;
      if (apiProcess === proc) {
        apiProcess = null;
      }
      refreshTrayMenu();
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        reject(new Error(`API sidecar exited early with code ${code}`));
      }
    });
  });
}

function getVaultDir() {
  if (process.env.BRAIN_VAULT_DIR) {
    return path.resolve(process.env.BRAIN_VAULT_DIR);
  }
  if (app.isPackaged) {
    return resolveConfiguredVaultPointer() || getDefaultPackagedVaultDir();
  }
  return path.join(getBackendRoot(), "brain");
}

function stopApiSidecar() {
  if (apiProcess) {
    killProcessTree(apiProcess);
    apiProcess = null;
  }
  apiReady = false;
  refreshTrayMenu();
}

const ALLOWED_HTTP_METHODS = new Set(["GET", "POST", "PUT", "DELETE"]);

function apiRequest(method, apiPath, body = null) {
  const safeMethod = String(method || "").toUpperCase();
  if (!ALLOWED_HTTP_METHODS.has(safeMethod)) {
    return Promise.reject(new Error(`Disallowed HTTP method: ${String(method)}`));
  }
  if (typeof apiPath !== "string" || !apiPath.startsWith("/v1/") || /[\s\r\n]/.test(apiPath)) {
    return Promise.reject(new Error(`Disallowed API path: ${String(apiPath)}`));
  }
  method = safeMethod;
  const { apiToken, ownerToken } = readTokens();
  const payload = body !== null && body !== undefined ? JSON.stringify(body) : null;
  const headers = {
    Host: `127.0.0.1:${apiPort}`,
    Accept: "application/json",
  };
  if (apiToken) headers["Authorization"] = `Bearer ${apiToken}`;
  if (ownerToken) headers["X-Owner-Token"] = ownerToken;
  if (payload !== null) {
    headers["Content-Type"] = "application/json";
    headers["Content-Length"] = Buffer.byteLength(payload, "utf8");
  }

  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        hostname: "127.0.0.1",
        port: apiPort,
        path: apiPath,
        method,
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
          resolve({ status: res.statusCode || 500, ok: res.statusCode >= 200 && res.statusCode < 300, data });
        });
      }
    );
    req.on("error", reject);
    if (payload !== null) req.write(payload);
    req.end();
  });
}

function streamChat(sender, payload) {
  const { apiToken, ownerToken } = readTokens();
  const rawBody = JSON.stringify(payload);
  const headers = {
    Host: `127.0.0.1:${apiPort}`,
    Accept: "text/event-stream",
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(rawBody, "utf8"),
  };
  if (apiToken) headers["Authorization"] = `Bearer ${apiToken}`;
  if (ownerToken) headers["X-Owner-Token"] = ownerToken;

  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        hostname: "127.0.0.1",
        port: apiPort,
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
            let errMsg = `HTTP ${res.statusCode || 500}`;
            try {
              const parsedErr = errRaw ? JSON.parse(errRaw) : {};
              if (parsedErr && parsedErr.error) errMsg = String(parsedErr.error);
            } catch {}
            sender.send("chat:event", { event: "error", data: { error: errMsg, status: res.statusCode } });
            resolve({ ok: false, status: res.statusCode, error: errMsg });
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
              sender.send("chat:event", { event: currentEvent, data: parsed });
              currentEvent = "message";
            }
          }
        });
        res.on("end", () => resolve({ ok: res.statusCode >= 200 && res.statusCode < 300, status: res.statusCode }));
      }
    );
    req.on("error", reject);
    req.write(rawBody);
    req.end();
  });
}

function hardenWindowNavigation(win) {
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.webContents.on("will-navigate", (event) => {
    event.preventDefault();
  });
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 960,
    minHeight: 640,
    title: "Second Brain",
    backgroundColor: "#0d1117",
    icon: path.join(__dirname, "assets", "icon.png"),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
  hardenWindowNavigation(mainWindow);
  mainWindow.on("close", (event) => {
    if (!isQuitting && !isSmokeTest) {
      event.preventDefault();
      mainWindow.hide();
    }
  });
}

function toggleCaptureWindow() {
  if (!captureWindow) {
    captureWindow = new BrowserWindow({
      width: 560,
      height: 300,
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
    captureWindow.loadFile(path.join(__dirname, "renderer", "capture.html"));
    hardenWindowNavigation(captureWindow);
    captureWindow.on("closed", () => {
      captureWindow = null;
    });
  }
  if (captureWindow.isVisible()) {
    captureWindow.hide();
  } else {
    captureWindow.center();
    captureWindow.show();
    captureWindow.focus();
  }
}

function refreshTrayMenu() {
  if (!tray) return;
  const statusLabel = apiReady && apiPort
    ? `Memory API: Running (127.0.0.1:${apiPort})`
    : "Memory API: Stopped";
  let openAtLogin = false;
  try {
    openAtLogin = Boolean(app.getLoginItemSettings().openAtLogin);
  } catch (_) {}

  const contextMenu = Menu.buildFromTemplate([
    { label: statusLabel, enabled: false },
    { type: "separator" },
    {
      label: "Open Second Brain",
      click: () => {
        if (mainWindow) {
          mainWindow.show();
          mainWindow.focus();
        }
      },
    },
    {
      label: "Quick Capture (Ctrl+Shift+Space)",
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
    { type: "separator" },
    {
      label: apiReady ? "Restart Memory API" : "Start Memory API",
      click: async () => {
        await startApiSidecar().catch(() => {});
        refreshTrayMenu();
        if (mainWindow) mainWindow.webContents.send("vault:refresh");
      },
    },
    {
      label: "Stop Memory API",
      enabled: Boolean(apiProcess || apiReady),
      click: () => stopApiSidecar(),
    },
    {
      label: "Launch at Login",
      type: "checkbox",
      checked: openAtLogin,
      click: (menuItem) => {
        try {
          app.setLoginItemSettings({ openAtLogin: Boolean(menuItem.checked) });
        } catch (_) {}
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
    },
  ]);
  tray.setToolTip(`Second Brain — ${statusLabel}`);
  tray.setContextMenu(contextMenu);
}

function createTray() {
  const icon = nativeImage.createFromPath(path.join(__dirname, "assets", "icon.png"));
  tray = new Tray(icon.resize({ width: 16, height: 16 }));
  refreshTrayMenu();
  tray.on("double-click", () => {
    if (mainWindow) {
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

ipcMain.handle("app:status", () => ({
  port: apiPort,
  ready: apiReady,
  apiUrl: `http://127.0.0.1:${apiPort}`,
  backendRoot: getBackendRoot(),
}));

ipcMain.handle("api:request", async (_event, { method, path: apiPath, body }) => {
  return apiRequest(method, apiPath, body);
});

ipcMain.handle("api:streamChat", async (event, payload) => {
  return streamChat(event.sender, payload);
});

ipcMain.handle("dialog:pickFolder", async () => {
  const res = await dialog.showOpenDialog(mainWindow, {
    properties: ["openDirectory"],
  });
  return res.canceled ? null : res.filePaths[0];
});

const ALLOWED_IDE_ACTIONS = new Set(["status", "add", "remove"]);
const IDE_TARGET_RE = /^[a-z0-9-]+$/;

ipcMain.handle("ide:action", async (_event, payload = {}) => {
  const { action, target } = payload && typeof payload === "object" ? payload : {};
  if (!ALLOWED_IDE_ACTIONS.has(action)) {
    throw new Error(`Disallowed IDE action: ${String(action)}`);
  }
  if (action === "add" || action === "remove") {
    if (typeof target !== "string" || target.startsWith("-") || !IDE_TARGET_RE.test(target)) {
      throw new Error(`Invalid IDE target: ${String(target)}`);
    }
  } else if (target !== undefined && target !== null && target !== "") {
    if (typeof target !== "string" || target.startsWith("-") || !IDE_TARGET_RE.test(target)) {
      throw new Error(`Invalid IDE target: ${String(target)}`);
    }
  }
  const args = ["scripts/ide_connector.py", action];
  if (target && action !== "status") args.push(target);
  if (action === "add") args.push("--yes");
  args.push("--json");
  const { stdout } = await runBackendCli(args);
  return JSON.parse(stdout);
});

ipcMain.handle("capture:close", () => {
  if (captureWindow) captureWindow.hide();
});

ipcMain.handle("capture:saved", () => {
  if (mainWindow) mainWindow.webContents.send("vault:refresh");
});

app.whenReady().then(async () => {
  try {
    await startApiSidecar();
  } catch (err) {
    console.error("Failed to start API sidecar:", err);
  }

  createMainWindow();
  if (!isSmokeTest) {
    createTray();
    globalShortcut.register("CommandOrControl+Shift+Space", toggleCaptureWindow);
  }

  if (isSmokeTest) {
    const health = await apiRequest("GET", "/v1/health").catch((e) => ({ ok: false, error: String(e) }));
    console.log("ELECTRON_SMOKE_RESULT", JSON.stringify({ apiPort, apiReady, health }));
    isQuitting = true;
    app.quit();
  }
});

app.on("before-quit", () => {
  isQuitting = true;
  globalShortcut.unregisterAll();
  if (apiProcess) {
    killProcessTree(apiProcess);
    apiProcess = null;
  }
});
