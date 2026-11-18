"use strict";

const { execFileSync } = require("child_process");
const path = require("path");

/**
 * Custom electron-builder Windows signing hook (`win.signtoolOptions.sign`).
 *
 * Delegates to `scripts/sign_release.ps1 -SingleFile <path> -SkipManifest` when
 * code-signing credentials (`SECOND_BRAIN_CERT_THUMBPRINT`, `SECOND_BRAIN_PFX_PATH`,
 * or `SECOND_BRAIN_SELF_SIGNED=1`) are configured in the environment. When no
 * signing credentials are present (e.g., unsigned CI `--dir` smoke builds), skips
 * signing cleanly while keeping `rcedit` icon and version metadata embedding active.
 */
exports.default = async function sign(configuration) {
  if (process.platform !== "win32") {
    return;
  }

  const targetPath = configuration && configuration.path;
  if (!targetPath) {
    return;
  }

  const thumbprint = (process.env.SECOND_BRAIN_CERT_THUMBPRINT || "").trim();
  const pfxPath = (process.env.SECOND_BRAIN_PFX_PATH || "").trim();
  const selfSigned =
    (process.env.SECOND_BRAIN_SELF_SIGNED || "").trim() === "1" ||
    (process.env.SECOND_BRAIN_SELF_SIGNED || "").trim().toLowerCase() === "true";

  if (!thumbprint && !pfxPath && !selfSigned) {
    return;
  }

  const signScript = path.resolve(__dirname, "..", "..", "scripts", "sign_release.ps1");
  const psArgs = [
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    signScript,
    "-SingleFile",
    targetPath,
    "-SkipManifest",
  ];

  if (thumbprint) {
    psArgs.push("-Thumbprint", thumbprint);
  } else if (pfxPath) {
    psArgs.push("-PfxPath", pfxPath);
  } else if (selfSigned) {
    psArgs.push("-SelfSigned");
  }

  const timestampUrl = (process.env.SECOND_BRAIN_TIMESTAMP_URL || "").trim();
  if (timestampUrl) {
    psArgs.push("-TimestampUrl", timestampUrl);
  }

  execFileSync("powershell.exe", psArgs, {
    stdio: "inherit",
    windowsHide: true,
    env: process.env,
  });
};
