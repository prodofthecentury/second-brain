"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const EventEmitter = require("events");
const fs = require("fs");
const path = require("path");

const { createUpdaterController } = require("../lib/updater");

describe("Desktop Auto-Updater Controller", () => {
  it("enforces local-first autoDownload=false and runs check -> download -> sidecar shutdown before quitAndInstall", async () => {
    const calls = [];
    const emitted = [];
    const mockAutoUpdater = new EventEmitter();
    mockAutoUpdater.autoDownload = true;
    mockAutoUpdater.autoInstallOnAppQuit = true;
    mockAutoUpdater.checkForUpdates = async () => {
      calls.push("checkForUpdates");
      mockAutoUpdater.emit("checking-for-update");
      mockAutoUpdater.emit("update-available", { version: "1.2.0" });
      return { updateInfo: { version: "1.2.0" } };
    };
    mockAutoUpdater.downloadUpdate = async () => {
      calls.push("downloadUpdate");
      mockAutoUpdater.emit("download-progress", { percent: 50 });
      mockAutoUpdater.emit("update-downloaded", { version: "1.2.0" });
      return ["Second-Brain-Setup-1.2.0-x64.exe"];
    };
    mockAutoUpdater.quitAndInstall = (isSilent, forceRunAfter) => {
      calls.push(["quitAndInstall", isSilent, forceRunAfter]);
    };

    const ctrl = createUpdaterController({
      autoUpdater: mockAutoUpdater,
      isPackaged: true,
      currentVersion: "1.1.0",
      emitStatus: (st) => emitted.push(st.state),
      onBeforeQuitInstall: async () => {
        calls.push("stopSidecarBeforeQuit");
      },
    });

    assert.equal(mockAutoUpdater.autoDownload, false);
    assert.equal(mockAutoUpdater.autoInstallOnAppQuit, false);
    assert.equal(ctrl.getStatus().state, "idle");

    const chk = await ctrl.checkForUpdates();
    assert.equal(chk.ok, true);
    assert.equal(ctrl.getStatus().availableVersion, "1.2.0");

    const dl = await ctrl.downloadUpdate();
    assert.equal(dl.ok, true);
    assert.equal(ctrl.getStatus().downloaded, true);

    const inst = await ctrl.quitAndInstallUpdate();
    assert.equal(inst.ok, true);
    assert.deepEqual(calls, [
      "checkForUpdates",
      "downloadUpdate",
      "stopSidecarBeforeQuit",
      ["quitAndInstall", false, true],
    ]);
    assert.equal(emitted[0], "checking");
    assert.ok(emitted.includes("available"));
    assert.ok(emitted.includes("downloading"));
    assert.equal(emitted.at(-1), "downloaded");
  });

  it("refuses update checks in unpackaged dev mode and declares electron-updater + GitHub publish config", () => {
    const mockUpdater = new EventEmitter();
    mockUpdater.checkForUpdates = async () => {
      throw new Error("should not call checkForUpdates when unpackaged");
    };
    const ctrl = createUpdaterController({ autoUpdater: mockUpdater, isPackaged: false });
    return ctrl.checkForUpdates().then((res) => {
      assert.equal(res.ok, false);
      assert.equal(res.reason, "updater_unavailable_unpackaged");

      const pkg = JSON.parse(
        fs.readFileSync(path.resolve(__dirname, "..", "package.json"), "utf8")
      );
      assert.ok(pkg.dependencies["electron-updater"]);
      assert.ok(Array.isArray(pkg.build.publish));
      assert.equal(pkg.build.publish[0].provider, "github");
    });
  });

  it("rejects quitAndInstallUpdate before download completes and guards duplicate/invalid downloadUpdate calls", async () => {
    let beforeQuitCalls = 0;
    let quitInstallCalls = 0;
    let downloadCalls = 0;
    let resolvePendingDownload = null;

    const mockUpdater = new EventEmitter();
    mockUpdater.checkForUpdates = async () => {
      mockUpdater.emit("checking-for-update");
      mockUpdater.emit("update-available", { version: "2.0.0" });
      return { updateInfo: { version: "2.0.0" } };
    };
    mockUpdater.downloadUpdate = () => {
      downloadCalls += 1;
      return new Promise((resolve) => {
        resolvePendingDownload = () => {
          mockUpdater.emit("update-downloaded", { version: "2.0.0" });
          resolve(["Second-Brain-Setup-2.0.0.exe"]);
        };
      });
    };
    mockUpdater.quitAndInstall = () => {
      quitInstallCalls += 1;
    };

    const ctrl = createUpdaterController({
      autoUpdater: mockUpdater,
      isPackaged: true,
      currentVersion: "1.9.0",
      onBeforeQuitInstall: async () => {
        beforeQuitCalls += 1;
      },
    });

    // Cannot download or install in idle state
    const dlIdle = await ctrl.downloadUpdate();
    assert.equal(dlIdle.ok, false);
    assert.equal(dlIdle.reason, "no_update_available");
    assert.equal(downloadCalls, 0);

    const instIdle = await ctrl.quitAndInstallUpdate();
    assert.equal(instIdle.ok, false);
    assert.equal(instIdle.reason, "update_not_downloaded");
    assert.equal(beforeQuitCalls, 0);
    assert.equal(quitInstallCalls, 0);

    // Move to available state; install still rejected before download
    const chk = await ctrl.checkForUpdates();
    assert.equal(chk.ok, true);
    const instAvail = await ctrl.quitAndInstallUpdate();
    assert.equal(instAvail.ok, false);
    assert.equal(instAvail.reason, "update_not_downloaded");
    assert.equal(beforeQuitCalls, 0);
    assert.equal(quitInstallCalls, 0);

    // Start first download; duplicate call while downloading is rejected
    const firstDlPromise = ctrl.downloadUpdate();
    const dupDl = await ctrl.downloadUpdate();
    assert.equal(dupDl.ok, false);
    assert.equal(dupDl.reason, "download_already_in_progress");
    assert.equal(downloadCalls, 1);

    // Install still rejected mid-download
    const instMid = await ctrl.quitAndInstallUpdate();
    assert.equal(instMid.ok, false);
    assert.equal(instMid.reason, "update_not_downloaded");

    // Finish download; duplicate download after downloaded is also rejected
    resolvePendingDownload();
    const firstDl = await firstDlPromise;
    assert.equal(firstDl.ok, true);
    assert.equal(ctrl.getStatus().downloaded, true);

    const dlAfter = await ctrl.downloadUpdate();
    assert.equal(dlAfter.ok, false);
    assert.equal(dlAfter.reason, "update_already_downloaded");
    assert.equal(downloadCalls, 1);

    // Now quitAndInstallUpdate succeeds
    const instDone = await ctrl.quitAndInstallUpdate();
    assert.equal(instDone.ok, true);
    assert.equal(beforeQuitCalls, 1);
    assert.equal(quitInstallCalls, 1);
  });
});
