"use strict";

const { execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

/**
 * electron-builder `afterSign` hook for macOS Developer ID Notarization & Gatekeeper verification.
 *
 * Supports two Apple Notary Service authentication modes:
 *   1. Apple ID + App-Specific Password + Team ID:
 *      `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID`
 *   2. App Store Connect API Key (.p8 path or base64):
 *      `APPLE_API_KEY` (or `APPLE_API_KEY_BASE64`), `APPLE_API_KEY_ID`, `APPLE_API_ISSUER`
 *
 * Behavior:
 *   - On non-macOS platforms: no-op.
 *   - When signing/notarization credentials are absent: logs notice and skips cleanly unless
 *     `SECOND_BRAIN_REQUIRE_NOTARIZATION=1` is set (which fails closed).
 *   - When credentials are present:
 *       1. Verifies Developer ID signature (`codesign --verify --deep --strict --verbose=2`).
 *       2. Archives `.app` via `ditto -c -k --keepParent`.
 *       3. Submits to Apple Notary Service via `xcrun notarytool submit --wait`.
 *       4. Staples notarization ticket via `xcrun stapler staple` & `xcrun stapler validate`.
 *       5. Verifies macOS Gatekeeper acceptance via `spctl --assess --type execute --verbose=4`.
 */
exports.default = async function notarizeApp(context) {
  if (!context || context.electronPlatformName !== "darwin") {
    return;
  }

  const requireNotarization =
    (process.env.SECOND_BRAIN_REQUIRE_NOTARIZATION || "").trim() === "1" ||
    (process.env.SECOND_BRAIN_REQUIRE_NOTARIZATION || "").trim().toLowerCase() === "true";

  const productName =
    (context.packager && context.packager.appInfo && context.packager.appInfo.productFilename) ||
    "Second Brain";
  const appPath = path.join(context.appOutDir, `${productName}.app`);
  if (!fs.existsSync(appPath)) {
    throw new Error(`[notarize] Packaged macOS app bundle not found: ${appPath}`);
  }

  const cscLink = (process.env.CSC_LINK || process.env.MAC_CSC_LINK || "").trim();
  const cscName = (process.env.CSC_NAME || "").trim();

  const appleId = (process.env.APPLE_ID || "").trim();
  const applePassword = (process.env.APPLE_APP_SPECIFIC_PASSWORD || "").trim();
  const appleTeamId = (process.env.APPLE_TEAM_ID || "").trim();

  const apiKeyPathEnv = (process.env.APPLE_API_KEY || "").trim();
  const apiKeyB64 = (process.env.APPLE_API_KEY_BASE64 || "").trim();
  const apiKeyId = (process.env.APPLE_API_KEY_ID || "").trim();
  const apiIssuer = (process.env.APPLE_API_ISSUER || "").trim();

  const hasAppleIdAuth = Boolean(appleId && applePassword && appleTeamId);
  const hasApiKeyAuth = Boolean((apiKeyPathEnv || apiKeyB64) && apiKeyId && apiIssuer);
  const hasSigningIdentity = Boolean(cscLink || cscName);

  if (!hasSigningIdentity || (!hasAppleIdAuth && !hasApiKeyAuth)) {
    const missing = [];
    if (!hasSigningIdentity) {
      missing.push("CSC_LINK/CSC_NAME (Developer ID Application certificate)");
    }
    if (!hasAppleIdAuth && !hasApiKeyAuth) {
      missing.push(
        "APPLE_ID+APPLE_APP_SPECIFIC_PASSWORD+APPLE_TEAM_ID (or APPLE_API_KEY+APPLE_API_KEY_ID+APPLE_API_ISSUER)"
      );
    }
    const msg = `[notarize] Skipping Apple Notary Service submission; missing: ${missing.join("; ")}`;
    if (requireNotarization) {
      throw new Error(`${msg} (SECOND_BRAIN_REQUIRE_NOTARIZATION=1 enforced).`);
    }
    console.log(msg);
    return;
  }

  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "sb-notarize-"));
  const zipPath = path.join(tmpDir, `${productName.replace(/\s+/g, "-")}-notarize.zip`);
  let tempKeyFile = null;

  try {
    console.log(`[notarize] Verifying Developer ID code signature on ${appPath}...`);
    execFileSync("codesign", ["--verify", "--deep", "--strict", "--verbose=2", appPath], {
      stdio: "inherit",
    });

    console.log(`[notarize] Creating ditto archive for Apple Notary Service...`);
    execFileSync("ditto", ["-c", "-k", "--keepParent", appPath, zipPath], {
      stdio: "inherit",
    });

    const notaryArgs = ["notarytool", "submit", zipPath, "--wait"];
    if (hasApiKeyAuth) {
      let resolvedKeyPath = apiKeyPathEnv;
      if (!resolvedKeyPath && apiKeyB64) {
        tempKeyFile = path.join(tmpDir, `AuthKey_${apiKeyId}.p8`);
        fs.writeFileSync(tempKeyFile, Buffer.from(apiKeyB64, "base64"), { mode: 0o600 });
        resolvedKeyPath = tempKeyFile;
      }
      notaryArgs.push("--key", resolvedKeyPath, "--key-id", apiKeyId, "--issuer", apiIssuer);
    } else {
      notaryArgs.push(
        "--apple-id",
        appleId,
        "--password",
        applePassword,
        "--team-id",
        appleTeamId
      );
    }

    console.log(`[notarize] Submitting ${path.basename(zipPath)} to Apple Notary Service...`);
    execFileSync("xcrun", notaryArgs, { stdio: "inherit" });

    console.log(`[notarize] Stapling notarization ticket to ${appPath}...`);
    execFileSync("xcrun", ["stapler", "staple", appPath], { stdio: "inherit" });
    execFileSync("xcrun", ["stapler", "validate", appPath], { stdio: "inherit" });

    console.log(`[notarize] Verifying macOS Gatekeeper acceptance (spctl)...`);
    execFileSync("spctl", ["--assess", "--type", "execute", "--verbose=4", appPath], {
      stdio: "inherit",
    });
    console.log(`[notarize] Successfully notarized, stapled, and Gatekeeper-verified ${appPath}.`);
  } finally {
    if (tempKeyFile && fs.existsSync(tempKeyFile)) {
      try {
        fs.unlinkSync(tempKeyFile);
      } catch (_) {}
    }
    try {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    } catch (_) {}
  }
};
