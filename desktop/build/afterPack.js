"use strict";

const fs = require("fs");
const path = require("path");

/**
 * electron-builder afterPack hook:
 * Copies pre-built offline Python wheels from `<repo>/dist/wheelhouse/*.whl`
 * into the packaged app's `resources/backend/wheelhouse/` directory on
 * Windows, macOS, and Linux when present.
 */
exports.default = async function afterPack(context) {
  const repoRoot = path.resolve(__dirname, "..", "..");
  const srcWheelhouse = path.join(repoRoot, "dist", "wheelhouse");
  if (!fs.existsSync(srcWheelhouse)) {
    return;
  }
  const wheels = fs
    .readdirSync(srcWheelhouse)
    .filter((name) => name.endsWith(".whl"));
  if (wheels.length === 0) {
    return;
  }

  let resourcesDir;
  if (context.electronPlatformName === "darwin") {
    const productName =
      context.packager?.appInfo?.productFilename || "Second Brain";
    resourcesDir = path.join(
      context.appOutDir,
      `${productName}.app`,
      "Contents",
      "Resources"
    );
  } else {
    resourcesDir = path.join(context.appOutDir, "resources");
  }

  const dstWheelhouse = path.join(resourcesDir, "backend", "wheelhouse");
  fs.mkdirSync(dstWheelhouse, { recursive: true });
  for (const existing of fs.readdirSync(dstWheelhouse)) {
    if (existing.endsWith(".whl")) {
      fs.rmSync(path.join(dstWheelhouse, existing), { force: true });
    }
  }
  for (const whl of wheels) {
    fs.copyFileSync(path.join(srcWheelhouse, whl), path.join(dstWheelhouse, whl));
  }
  console.log(
    `[afterPack] Bundled ${wheels.length} offline wheels into ${dstWheelhouse}`
  );
};
