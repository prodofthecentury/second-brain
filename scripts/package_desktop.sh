#!/usr/bin/env bash
set -euo pipefail

# Build and sign Second Brain desktop installers on macOS (DMG/zip) or Linux (AppImage/deb),
# plus Python wheel/sdist, offline wheelhouse (universal arm64+x86_64 on macOS), VSIX extension,
# Browser Web Clippers (.zip/.xpi), Mobile PWA/Vault bundles, and SHA256SUMS.txt.
# Optional env vars for macOS notarization/signing:
#   CSC_LINK / CSC_KEY_PASSWORD / APPLE_ID / APPLE_APP_SPECIFIC_PASSWORD / APPLE_TEAM_ID

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESKTOP_DIR="$REPO_ROOT/desktop"
DIST_DIR="$REPO_ROOT/dist"

mkdir -p "$DIST_DIR"

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: 'uv' is required on PATH to package Second Brain (wheel, wheelhouse, VSIX, clippers, mobile bundles)." >&2
  exit 1
fi

cd "$REPO_ROOT"
CURRENT_VERSION="$(uv run --frozen python -c 'import second_brain; print(second_brain.__version__)' | tail -n 1 | tr -d '\r\n')"
if [ -n "$CURRENT_VERSION" ]; then
  for entry in "$DIST_DIR"/*; do
    [ -f "$entry" ] || continue
    base="$(basename "$entry")"
    case "$base" in
      SHA256SUMS.txt|.gitignore|second-brain-inbox-drop-pwa.zip|second-brain-mobile-vault-client.zip|SecondBrain.SecondBrain.yaml|second-brain.rb|second-brain-formula.rb|latest.yml|latest-mac.yml|latest-linux.yml|*"$CURRENT_VERSION"*)
        ;;
      *)
        rm -f "$entry"
        ;;
    esac
  done
fi
uv run --frozen python scripts/make_icons.py
uv build --out-dir "$DIST_DIR"
uv run --frozen python scripts/build_wheelhouse.py --out-dir "$DIST_DIR/wheelhouse"
uv run --frozen python scripts/build_vsix.py --out-dir "$DIST_DIR"
uv run --frozen python scripts/build_clipper.py --out-dir "$DIST_DIR"
uv run --frozen python scripts/build_distribution_manifests.py --pwa-only --out-dir "$DIST_DIR"

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
  rm -f builder-debug.yml builder-effective-config.yaml
  shopt -s nullglob
  raw_artifacts=(*.dmg *.zip *.AppImage *.deb *.whl *.tar.gz *.vsix *.xpi *.blockmap latest*.yml)
  if [ "${#raw_artifacts[@]}" -gt 0 ]; then
    sorted_artifacts=()
    while IFS= read -r item; do
      [ -n "$item" ] && sorted_artifacts+=("$item")
    done < <(LC_ALL=C printf '%s\n' "${raw_artifacts[@]}" | LC_ALL=C sort -u)
    if command -v sha256sum >/dev/null 2>&1; then
      sha256sum "${sorted_artifacts[@]}" > SHA256SUMS.txt
    elif command -v shasum >/dev/null 2>&1; then
      shasum -a 256 "${sorted_artifacts[@]}" > SHA256SUMS.txt
    fi
  fi
fi
