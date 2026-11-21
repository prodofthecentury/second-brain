"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("secondBrain", {
  platform: process.platform,
  getStatus: () => ipcRenderer.invoke("app:status"),
  request: (method, path, body = null) =>
    ipcRenderer.invoke("api:request", { method, path, body }),
  api: (method, path, body = null) =>
    ipcRenderer.invoke("api:request", { method, path, body }),
  streamChat: (payload) => ipcRenderer.invoke("chat:stream", payload),
  abortChat: () => ipcRenderer.invoke("chat:abort"),
  cancelChat: () => ipcRenderer.invoke("chat:abort"),
  pickFolder: () => ipcRenderer.invoke("dialog:pick-folder"),
  pickFile: () => ipcRenderer.invoke("dialog:pick-file"),
  pickSaveBackup: () => ipcRenderer.invoke("dialog:pick-save-backup"),
  pickBackupFile: () => ipcRenderer.invoke("dialog:pick-backup-file"),
  openExternal: (url) => ipcRenderer.invoke("shell:open-external", { url }),
  switchVault: () => ipcRenderer.invoke("vault:switch"),
  chooseVault: () => ipcRenderer.invoke("vault:switch"),
  ideAction: (action, target = null, options = {}) =>
    ipcRenderer.invoke("ide:action", {
      action,
      target,
      options: options && typeof options === "object" ? options : {},
    }),
  closeCapture: () => ipcRenderer.invoke("capture:close"),
  notifyMainRefresh: () => ipcRenderer.invoke("capture:saved"),
  onChatEvent: (callback) => {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("chat:event", listener);
    return () => ipcRenderer.removeListener("chat:event", listener);
  },
  onRefresh: (callback) => {
    const listener = () => callback();
    ipcRenderer.on("app:refresh", listener);
    return () => ipcRenderer.removeListener("app:refresh", listener);
  },
  onBackendStatus: (callback) => {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("sb:backend-status", listener);
    return () => ipcRenderer.removeListener("sb:backend-status", listener);
  },
  onCaptureFocus: (callback) => {
    const listener = () => callback();
    ipcRenderer.on("sb:capture-focus", listener);
    return () => ipcRenderer.removeListener("sb:capture-focus", listener);
  },
});
