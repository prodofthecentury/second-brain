const {
  app,
  BrowserWindow,
  Tray,
  Menu,
  globalShortcut,
  ipcMain,
  dialog,
  nativeImage,
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

function readTokens() {
  const secDir = getSecurityDir();
  const apiTokenPath = path.join(secDir, "memory-api-token");
  const ownerTokenPath = path.join(secDir, "owner-token");
  const apiToken = fs.existsSync(apiTokenPath)
    ? fs.readFileSync(apiTokenPath, "utf8").trim()
    : "";
  const ownerToken = fs.existsSync(ownerTokenPath)
    ? fs.readFileSync(ownerTokenPath, "utf8").trim()
    : "";
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

function spawnBackendCli(args) {
  const root = getBackendRoot();
  const env = { ...process.env, PYTHONUTF8: "1" };
  if (app.isPackaged && !env.BRAIN_VAULT_DIR) {
    const userVault = path.join(app.getPath("userData"), "brain");
    fs.mkdirSync(userVault, { recursive: true });
    env.BRAIN_VAULT_DIR = userVault;
  }
  return spawn("uv", ["--directory", root, "run", "--frozen", "python", ...args], {
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

async function startApiSidecar() {
  if (!apiPort) {
    apiPort = await findFreePort(8765);
  }
  apiReady = false;
  if (apiProcess) {
    try {
      apiProcess.kill();
    } catch (_) {}
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
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        reject(new Error(`API sidecar exited early with code ${code}`));
      }
    });
  });
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

function createTray() {
  const icon = nativeImage.createFromPath(path.join(__dirname, "assets", "icon.png"));
  tray = new Tray(icon.resize({ width: 16, height: 16 }));
  tray.setToolTip("Second Brain — Local AI Memory");
  const contextMenu = Menu.buildFromTemplate([
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
    { type: "separator" },
    {
      label: "Restart API Sidecar",
      click: async () => {
        await startApiSidecar().catch(() => {});
        if (mainWindow) mainWindow.webContents.send("vault:refresh");
      },
    },
    {
      label: "Quit",
      click: () => {
        isQuitting = true;
        app.quit();
      },
    },
  ]);
  tray.setContextMenu(contextMenu);
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
    try {
      apiProcess.kill();
    } catch (_) {}
  }
});
