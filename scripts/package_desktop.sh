#!/usr/bin/env bash
set -euo pipefail

# Build and sign Second Brain desktop installers on macOS (DMG/zip) or Linux (AppImage/deb).
# Optional env vars for macOS notarization/signing:
#   CSC_LINK / CSC_KEY_PASSWORD / APPLE_ID / APPLE_APP_SPECIFIC_PASSWORD / APPLE_TEAM_ID

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESKTOP_DIR="$REPO_ROOT/desktop"
DIST_DIR="$REPO_ROOT/dist"

cd "$DESKTOP_DIR"
if [ ! -d node_modules ]; then
  npm ci
fi

UNAME_S="$(uname -s)"
case "$UNAME_S" in
  Darwin)
    npm run dist:mac
    ;;
  Linux)
    npm run dist:linux
    ;;
  *)
    echo "Unsupported host OS for package_desktop.sh: $UNAME_S (use scripts/package_desktop.ps1 on Windows)" >&2
    exit 1
    ;;
esac

if command -v uv >/dev/null 2>&1; then
  cd "$REPO_ROOT"
  mkdir -p "$DIST_DIR"
  uv build --wheel --out-dir "$DIST_DIR"
  uv run --frozen python - <<'PY'
import json, pathlib, zipfile
root = pathlib.Path("extensions/vscode-second-brain")
pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
ver = pkg["version"]
out = pathlib.Path("dist") / f"second-brain-vscode-{ver}.vsix"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr(
        "[Content_Types].xml",
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="json" ContentType="application/json"/>'
        '<Default Extension="js" ContentType="application/javascript"/>'
        '<Default Extension="md" ContentType="text/markdown"/>'
        '<Default Extension="vsixmanifest" ContentType="text/xml"/>'
        "</Types>",
    )
    z.writestr(
        "extension.vsixmanifest",
        '<?xml version="1.0" encoding="utf-8"?>'
        '<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">'
        f'<Metadata><Identity Language="en-US" Id="{pkg["name"]}" Version="{ver}" Publisher="{pkg["publisher"]}"/>'
        f'<DisplayName>{pkg["displayName"]}</DisplayName>'
        f'<Description>{pkg["description"]}</Description></Metadata>'
        '<Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>'
        '<Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json"/></Assets>'
        "</PackageManifest>",
    )
    for p in sorted(root.rglob("*")):
        if p.is_file():
            z.write(p, f"extension/{p.relative_to(root).as_posix()}")
PY
fi

if [ -d "$DIST_DIR" ]; then
  cd "$DIST_DIR"
  shopt -s nullglob
  artifacts=(*.dmg *.zip *.AppImage *.deb *.whl *.vsix)
  if [ "${#artifacts[@]}" -gt 0 ]; then
    if command -v sha256sum >/dev/null 2>&1; then
      sha256sum "${artifacts[@]}" > SHA256SUMS.txt
    elif command -v shasum >/dev/null 2>&1; then
      shasum -a 256 "${artifacts[@]}" > SHA256SUMS.txt
    fi
  fi
fi
