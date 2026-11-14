const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("secondBrain", {
  getStatus: () => ipcRenderer.invoke("app:status"),
  request: (method, path, body = null) =>
    ipcRenderer.invoke("api:request", { method, path, body }),
  streamChat: (payload) => ipcRenderer.invoke("api:streamChat", payload),
  pickFolder: () => ipcRenderer.invoke("dialog:pickFolder"),
  ideAction: (action, target = null) =>
    ipcRenderer.invoke("ide:action", { action, target }),
  closeCapture: () => ipcRenderer.invoke("capture:close"),
  notifyMainRefresh: () => ipcRenderer.invoke("capture:saved"),
  onChatEvent: (callback) => {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("chat:event", listener);
    return () => ipcRenderer.removeListener("chat:event", listener);
  },
  onRefresh: (callback) => {
    const listener = () => callback();
    ipcRenderer.on("vault:refresh", listener);
    return () => ipcRenderer.removeListener("vault:refresh", listener);
  },
});
