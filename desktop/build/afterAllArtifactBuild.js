"use strict";

const { execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

/**
 * electron-builder `afterAllArtifactBuild` hook for outer macOS `.dmg` notarization & stapling.
 *
 * `afterSign` (`./build/notarize.js`) notarizes the inner `Second Brain.app` bundle before `.dmg`
 * assembly, while this hook notarizes, staples, validates, and Gatekeeper-assesses each outer
 * `.dmg` image after `electron-builder` finishes creating all artifacts.
 */
exports.default = async function notarizeDmgArtifacts(buildResult) {
  const artifacts = (buildResult && Array.isArray(buildResult.artifactPaths) && buildResult.artifactPaths) || [];
  if (process.platform !== "darwin") {
    return artifacts;
  }

  const dmgPaths = artifacts.filter((p) => typeof p === "string" && p.toLowerCase().endsWith(".dmg"));
  if (dmgPaths.length === 0) {
    return artifacts;
  }

  const requireNotarization =
    (process.env.SECOND_BRAIN_REQUIRE_NOTARIZATION || "").trim() === "1" ||
    (process.env.SECOND_BRAIN_REQUIRE_NOTARIZATION || "").trim().toLowerCase() === "true";

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
    const msg = "[afterAllArtifactBuild] Skipping outer .dmg notarization; Developer ID or Notary credentials absent.";
    if (requireNotarization) {
      throw new Error(`${msg} (SECOND_BRAIN_REQUIRE_NOTARIZATION=1 enforced).`);
    }
    console.log(msg);
    return artifacts;
  }

  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "sb-dmg-notarize-"));
  let tempKeyFile = null;
  try {
    let resolvedKeyPath = apiKeyPathEnv;
    if (hasApiKeyAuth && !resolvedKeyPath && apiKeyB64) {
      tempKeyFile = path.join(tmpDir, `AuthKey_${apiKeyId}.p8`);
      fs.writeFileSync(tempKeyFile, Buffer.from(apiKeyB64, "base64"), { mode: 0o600 });
      resolvedKeyPath = tempKeyFile;
    }

    for (const dmgPath of dmgPaths) {
      if (!fs.existsSync(dmgPath)) {
        throw new Error(`[afterAllArtifactBuild] Missing expected .dmg artifact: ${dmgPath}`);
      }
      const notaryArgs = ["notarytool", "submit", dmgPath, "--wait"];
      if (hasApiKeyAuth) {
        notaryArgs.push("--key", resolvedKeyPath, "--key-id", apiKeyId, "--issuer", apiIssuer);
      } else {
        notaryArgs.push(
          "--apple-id",
          appleId,
          "--password",
          "@env:APPLE_APP_SPECIFIC_PASSWORD",
          "--team-id",
          appleTeamId
        );
      }

      console.log(`[afterAllArtifactBuild] Submitting ${path.basename(dmgPath)} to Apple Notary Service...`);
      execFileSync("xcrun", notaryArgs, { stdio: "inherit" });

      console.log(`[afterAllArtifactBuild] Stapling & validating ${path.basename(dmgPath)}...`);
      execFileSync("xcrun", ["stapler", "staple", dmgPath], { stdio: "inherit" });
      execFileSync("xcrun", ["stapler", "validate", dmgPath], { stdio: "inherit" });

      console.log(`[afterAllArtifactBuild] Assessing Gatekeeper primary signature on ${path.basename(dmgPath)}...`);
      execFileSync(
        "spctl",
        ["--assess", "--type", "open", "--context", "context:primary-signature", "--verbose=4", dmgPath],
        { stdio: "inherit" }
      );
    }
    return artifacts;
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
