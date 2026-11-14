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
  find . -maxdepth 1 -type f \( -name '*.dmg' -o -name '*.zip' -o -name '*.AppImage' -o -name '*.deb' \) \
    -printf '%f\n' 2>/dev/null || ls -1 *.dmg *.zip *.AppImage *.deb 2>/dev/null | xargs -r sha256sum > SHA256SUMS.txt || true
fi
