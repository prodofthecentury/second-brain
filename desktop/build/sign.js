"use strict";

const { execFileSync } = require("child_process");
const path = require("path");

/**
 * Custom electron-builder Windows signing hook (`win.signtoolOptions.sign`).
 *
 * Delegates to `scripts/sign_release.ps1 -SingleFile <path> -SkipManifest` when
 * code-signing credentials (`SECOND_BRAIN_CERT_THUMBPRINT`, `SECOND_BRAIN_PFX_PATH`,
 * EV HSM `SECOND_BRAIN_CSP_NAME` + `SECOND_BRAIN_KEY_CONTAINER`, Azure Trusted Signing
 * `SECOND_BRAIN_AZURE_TRUSTED_SIGNING_ENDPOINT`, or `SECOND_BRAIN_SELF_SIGNED=1`)
 * are configured in the environment. When no signing credentials are present (e.g.,
 * unsigned CI `--dir` smoke builds), skips signing cleanly unless
 * `SECOND_BRAIN_REQUIRE_TRUSTED_SIGN=1` is enforced.
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
  const cspName = (process.env.SECOND_BRAIN_CSP_NAME || "").trim();
  const keyContainer = (process.env.SECOND_BRAIN_KEY_CONTAINER || "").trim();
  const azureEndpoint = (process.env.SECOND_BRAIN_AZURE_TRUSTED_SIGNING_ENDPOINT || "").trim();
  const azureAccount = (process.env.SECOND_BRAIN_AZURE_CODE_SIGNING_ACCOUNT || "").trim();
  const azureProfile = (process.env.SECOND_BRAIN_AZURE_CERT_PROFILE || "").trim();
  const requireTrusted =
    (process.env.SECOND_BRAIN_REQUIRE_TRUSTED_SIGN || "").trim() === "1" ||
    (process.env.SECOND_BRAIN_REQUIRE_TRUSTED_SIGN || "").trim().toLowerCase() === "true";
  const selfSigned =
    (process.env.SECOND_BRAIN_SELF_SIGNED || "").trim() === "1" ||
    (process.env.SECOND_BRAIN_SELF_SIGNED || "").trim().toLowerCase() === "true";

  const hasAnyProvider = Boolean(
    thumbprint ||
      pfxPath ||
      (cspName && keyContainer) ||
      (azureEndpoint && azureAccount && azureProfile) ||
      selfSigned ||
      requireTrusted
  );
  if (!hasAnyProvider) {
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
  } else if (selfSigned && !requireTrusted) {
    psArgs.push("-SelfSigned");
  }

  if (requireTrusted) {
    psArgs.push("-RequireTrustedChain");
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
