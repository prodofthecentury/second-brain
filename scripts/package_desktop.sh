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

if [ -d "$DIST_DIR" ]; then
  cd "$DIST_DIR"
  shopt -s nullglob
  artifacts=(*.dmg *.zip *.AppImage *.deb)
  if [ "${#artifacts[@]}" -gt 0 ]; then
    if command -v sha256sum >/dev/null 2>&1; then
      sha256sum "${artifacts[@]}" > SHA256SUMS.txt
    elif command -v shasum >/dev/null 2>&1; then
      shasum -a 256 "${artifacts[@]}" > SHA256SUMS.txt
    fi
  fi
fi
