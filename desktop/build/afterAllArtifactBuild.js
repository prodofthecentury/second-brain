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
 *
 * Without Developer ID and notary credentials the hook returns the artifact list unchanged.
 * Set SECOND_BRAIN_REQUIRE_NOTARIZATION=1 to fail instead of skipping.
 */

function credentialState(env) {
  const source = env || {};
  const appleId = String(source.APPLE_ID || "").trim();
  const applePassword = String(source.APPLE_APP_SPECIFIC_PASSWORD || "").trim();
  const appleTeamId = String(source.APPLE_TEAM_ID || "").trim();
  const apiKeyPathEnv = String(source.APPLE_API_KEY || "").trim();
  const apiKeyB64 = String(source.APPLE_API_KEY_BASE64 || "").trim();
  const apiKeyId = String(source.APPLE_API_KEY_ID || "").trim();
  const apiIssuer = String(source.APPLE_API_ISSUER || "").trim();
  const cscLink = String(source.CSC_LINK || source.MAC_CSC_LINK || "").trim();
  const cscName = String(source.CSC_NAME || "").trim();
  return {
    appleId,
    applePassword,
    appleTeamId,
    apiKeyPathEnv,
    apiKeyB64,
    apiKeyId,
    apiIssuer,
    hasAppleIdAuth: Boolean(appleId && applePassword && appleTeamId),
    hasApiKeyAuth: Boolean((apiKeyPathEnv || apiKeyB64) && apiKeyId && apiIssuer),
    hasSigningIdentity: Boolean(cscLink || cscName),
  };
}

function buildNotarySubmitArgs(dmgPath, env, resolvedKeyPath) {
  const creds = credentialState(env);
  if (!creds.hasSigningIdentity || (!creds.hasAppleIdAuth && !creds.hasApiKeyAuth)) {
    return null;
  }
  const args = ["notarytool", "submit", dmgPath, "--wait"];
  if (creds.hasApiKeyAuth) {
    args.push("--key", resolvedKeyPath, "--key-id", creds.apiKeyId, "--issuer", creds.apiIssuer);
  } else {
    args.push(
      "--apple-id",
      creds.appleId,
      "--password",
      "@env:APPLE_APP_SPECIFIC_PASSWORD",
      "--team-id",
      creds.appleTeamId
    );
  }
  return args;
}

function planDmgNotarization(dmgPath, env, resolvedKeyPath) {
  const submit = buildNotarySubmitArgs(dmgPath, env, resolvedKeyPath);
  if (!submit) {
    return [];
  }
  return [
    ["xcrun", submit],
    ["xcrun", ["stapler", "staple", dmgPath]],
    ["xcrun", ["stapler", "validate", dmgPath]],
    ["spctl", ["--assess", "--type", "open", "--context", "context:primary-signature", "--verbose=4", dmgPath]],
  ];
}

function notarizationRequired(env) {
  const flag = String((env && env.SECOND_BRAIN_REQUIRE_NOTARIZATION) || "").trim().toLowerCase();
  return flag === "1" || flag === "true";
}

function hostPlatform(env) {
  if (env && env.SIMULATED_PLATFORM) {
    return String(env.SIMULATED_PLATFORM);
  }
  return process.platform;
}

async function notarizeDmgArtifacts(buildResult, env, execFile) {
  const runtimeEnv = env || process.env;
  const run = execFile || execFileSync;
  const artifacts = (buildResult && Array.isArray(buildResult.artifactPaths) && buildResult.artifactPaths) || [];
  if (hostPlatform(runtimeEnv) !== "darwin") {
    return artifacts;
  }

  const dmgPaths = artifacts.filter((p) => typeof p === "string" && p.toLowerCase().endsWith(".dmg"));
  if (dmgPaths.length === 0) {
    return artifacts;
  }

  const creds = credentialState(runtimeEnv);
  if (!creds.hasSigningIdentity || (!creds.hasAppleIdAuth && !creds.hasApiKeyAuth)) {
    const msg = "[afterAllArtifactBuild] Skipping outer .dmg notarization; Developer ID or Notary credentials absent.";
    if (notarizationRequired(runtimeEnv)) {
      throw new Error(`${msg} (SECOND_BRAIN_REQUIRE_NOTARIZATION=1 enforced).`);
    }
    console.log(msg);
    return artifacts;
  }

  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "sb-dmg-notarize-"));
  let tempKeyFile = null;
  try {
    let resolvedKeyPath = creds.apiKeyPathEnv;
    if (creds.hasApiKeyAuth && !resolvedKeyPath && creds.apiKeyB64) {
      tempKeyFile = path.join(tmpDir, `AuthKey_${creds.apiKeyId}.p8`);
      fs.writeFileSync(tempKeyFile, Buffer.from(creds.apiKeyB64, "base64"), { mode: 0o600 });
      resolvedKeyPath = tempKeyFile;
    }

    for (const dmgPath of dmgPaths) {
      if (!runtimeEnv.SIMULATED_PLATFORM && !fs.existsSync(dmgPath)) {
        throw new Error(`[afterAllArtifactBuild] Missing expected .dmg artifact: ${dmgPath}`);
      }
      const plan = planDmgNotarization(dmgPath, runtimeEnv, resolvedKeyPath);
      console.log(`[afterAllArtifactBuild] Submitting ${path.basename(dmgPath)} to Apple Notary Service...`);
      for (const [command, args] of plan) {
        run(command, args, { stdio: "inherit" });
      }
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
}

exports.buildNotarySubmitArgs = buildNotarySubmitArgs;
exports.planDmgNotarization = planDmgNotarization;
exports.default = notarizeDmgArtifacts;
