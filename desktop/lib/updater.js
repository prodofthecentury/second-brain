"use strict";

/**
 * Local-first Electron auto-updater controller (`electron-updater`).
 *
 * Defaults strictly to owner-initiated updates (`autoDownload = false`,
 * `autoInstallOnAppQuit = false`) so Second Brain makes zero automatic
 * outbound update requests on launch unless explicitly requested.
 */
function resolveDefaultAutoUpdater() {
  try {
    const mod = require("electron-updater");
    return mod && mod.autoUpdater ? mod.autoUpdater : null;
  } catch {
    return null;
  }
}

function createUpdaterController(options = {}) {
  const appRef = options.app || null;
  const onBeforeQuitInstall =
    typeof options.onBeforeQuitInstall === "function"
      ? options.onBeforeQuitInstall
      : async () => {};
  const emitStatus =
    typeof options.emitStatus === "function" ? options.emitStatus : () => {};

  const currentVersion =
    (options.currentVersion && String(options.currentVersion)) ||
    (appRef && typeof appRef.getVersion === "function" ? String(appRef.getVersion()) : "1.0.0");

  const isPackaged =
    typeof options.isPackaged === "boolean"
      ? options.isPackaged
      : Boolean(appRef && appRef.isPackaged);

  const updater =
    options.autoUpdater !== undefined ? options.autoUpdater : resolveDefaultAutoUpdater();

  const state = {
    supported: Boolean(updater),
    packaged: isPackaged,
    autoDownload: false,
    autoInstallOnAppQuit: false,
    currentVersion,
    state: updater ? (isPackaged ? "idle" : "dev_mode") : "unavailable",
    availableVersion: "",
    releaseNotes: "",
    progressPercent: 0,
    downloaded: false,
    checkedAt: "",
    error: "",
  };

  function snapshot() {
    return { ...state };
  }

  function updateState(patch) {
    const sameState =
      patch.state &&
      patch.state === state.state &&
      patch.progressPercent === undefined &&
      !patch.error;
    Object.assign(state, patch);
    if (!sameState) {
      emitStatus(snapshot());
    }
  }

  if (updater && typeof updater.on === "function") {
    try {
      updater.autoDownload = false;
      updater.autoInstallOnAppQuit = false;
    } catch {}

    updater.on("checking-for-update", () => {
      updateState({ state: "checking", error: "", checkedAt: new Date().toISOString() });
    });

    updater.on("update-available", (info = {}) => {
      updateState({
        state: "available",
        availableVersion: String(info.version || ""),
        releaseNotes: typeof info.releaseNotes === "string" ? info.releaseNotes.slice(0, 2000) : "",
        progressPercent: 0,
        downloaded: false,
        error: "",
      });
    });

    updater.on("update-not-available", (info = {}) => {
      updateState({
        state: "up_to_date",
        availableVersion: String(info.version || currentVersion),
        progressPercent: 0,
        downloaded: false,
        error: "",
      });
    });

    updater.on("download-progress", (prog = {}) => {
      const pct = Number.isFinite(Number(prog.percent)) ? Math.max(0, Math.min(100, Number(prog.percent))) : 0;
      updateState({
        state: "downloading",
        progressPercent: Math.round(pct * 10) / 10,
        error: "",
      });
    });

    updater.on("update-downloaded", (info = {}) => {
      updateState({
        state: "downloaded",
        availableVersion: String(info.version || state.availableVersion || ""),
        progressPercent: 100,
        downloaded: true,
        error: "",
      });
    });

    updater.on("error", (err) => {
      updateState({
        state: "error",
        error: err && err.message ? String(err.message) : String(err || "Unknown updater error"),
      });
    });
  }

  async function checkForUpdates() {
    if (!updater) {
      updateState({
        state: "unavailable",
        error: "electron-updater runtime is not installed in this environment.",
      });
      return { ok: false, reason: "updater_unavailable", status: snapshot() };
    }
    if (!isPackaged && !options.allowUnpackaged) {
      updateState({
        state: "dev_mode",
        checkedAt: new Date().toISOString(),
        error: "Auto-update feed checks apply only to packaged desktop builds.",
      });
      return { ok: false, reason: "updater_unavailable_unpackaged", status: snapshot() };
    }
    try {
      updateState({ state: "checking", error: "", checkedAt: new Date().toISOString() });
      const res = await updater.checkForUpdates();
      return { ok: true, result: res || null, status: snapshot() };
    } catch (err) {
      const msg = err && err.message ? String(err.message) : String(err);
      updateState({ state: "error", error: msg });
      return { ok: false, reason: msg, status: snapshot() };
    }
  }

  async function downloadUpdate() {
    if (!updater || (!isPackaged && !options.allowUnpackaged)) {
      return { ok: false, reason: "updater_unavailable", status: snapshot() };
    }
    if (state.state !== "available") {
      const reason =
        state.state === "downloading"
          ? "download_already_in_progress"
          : state.downloaded || state.state === "downloaded"
          ? "update_already_downloaded"
          : "no_update_available";
      return { ok: false, reason, status: snapshot() };
    }
    try {
      updateState({ state: "downloading", error: "" });
      const files = await updater.downloadUpdate();
      return { ok: true, files: files || [], status: snapshot() };
    } catch (err) {
      const msg = err && err.message ? String(err.message) : String(err);
      updateState({ state: "error", error: msg });
      return { ok: false, reason: msg, status: snapshot() };
    }
  }

  async function quitAndInstallUpdate() {
    if (!updater) {
      return { ok: false, reason: "updater_unavailable", status: snapshot() };
    }
    if (state.downloaded !== true) {
      return { ok: false, reason: "update_not_downloaded", status: snapshot() };
    }
    await onBeforeQuitInstall();
    updater.quitAndInstall(false, true);
    return { ok: true, status: snapshot() };
  }

  return {
    getStatus: snapshot,
    checkForUpdates,
    downloadUpdate,
    quitAndInstallUpdate,
  };
}

module.exports = {
  createUpdaterController,
};
