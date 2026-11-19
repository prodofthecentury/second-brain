#!/usr/bin/env bash
set -euo pipefail

# Build and sign Second Brain desktop installers on macOS (DMG/zip) or Linux (AppImage/deb),
# plus Python wheel, offline wheelhouse, VSIX extension, and SHA256SUMS.txt.
# Optional env vars for macOS notarization/signing:
#   CSC_LINK / CSC_KEY_PASSWORD / APPLE_ID / APPLE_APP_SPECIFIC_PASSWORD / APPLE_TEAM_ID

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESKTOP_DIR="$REPO_ROOT/desktop"
DIST_DIR="$REPO_ROOT/dist"

mkdir -p "$DIST_DIR"

if command -v uv >/dev/null 2>&1; then
  cd "$REPO_ROOT"
  uv run --frozen python scripts/make_icons.py
  uv build --wheel --out-dir "$DIST_DIR"
  uv run --frozen python scripts/build_wheelhouse.py --out-dir "$DIST_DIR/wheelhouse"
  uv run --frozen python scripts/build_vsix.py --out-dir "$DIST_DIR"
  uv run --frozen python scripts/build_clipper.py --out-dir "$DIST_DIR"
fi

cd "$DESKTOP_DIR"
if [ -f package-lock.json ]; then
  npm ci
elif [ ! -d node_modules ]; then
  npm install
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
  artifacts=(*.dmg *.zip *.AppImage *.deb *.whl *.vsix)
  if [ "${#artifacts[@]}" -gt 0 ]; then
    if command -v sha256sum >/dev/null 2>&1; then
      sha256sum "${artifacts[@]}" > SHA256SUMS.txt
    elif command -v shasum >/dev/null 2>&1; then
      shasum -a 256 "${artifacts[@]}" > SHA256SUMS.txt
    fi
  fi
fi
