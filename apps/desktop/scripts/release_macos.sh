#!/usr/bin/env bash
# Build a signed macOS Khipu.app + updater artifacts, then optionally publish
# a GitHub Release carrying the tarball, its minisign signature, and the
# latest.json manifest tauri-plugin-updater reads.
#
# Prereqs:
#   - ~/.tauri/khipu.key (+ .pub) from: npx tauri signer generate -w ~/.tauri/khipu.key
#   - APPLE_SIGNING_IDENTITY in the environment (Developer ID Application …)
#   - gh auth for --publish; the target repo must be PUBLIC (an unauthenticated
#     updater client gets 404 for a private repo's release assets)
#
# After build, injects Info.plist LSEnvironment (KHIPU_ROOT / KHIPU_PYTHON) from
# the build machine (or env overrides), then re-signs so /Applications and
# updater tarballs carry a production-safe CLI contract.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
DESKTOP="$ROOT/apps/desktop"
KEY="${TAURI_SIGNING_PRIVATE_KEY_PATH:-$HOME/.tauri/khipu.key}"
# Signing identity and publish target belong to whoever is building, not to
# the repo: set them in the environment (a private ops note records the
# maintainer's values). APPLE_SIGNING_IDENTITY is the same variable Tauri's
# bundler reads, so it applies to `tauri build` too.
IDENTITY="${APPLE_SIGNING_IDENTITY:-}"
# Update channel: GitHub Releases of RELEASE_REPO (owner/name). Defaults to the
# repo this checkout's origin points at. tauri.conf.json plugins.updater.endpoints
# must be https://github.com/<RELEASE_REPO>/releases/latest/download/latest.json.
RELEASE_REPO="${KHIPU_RELEASE_REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)}"
VERSION="$(python3 -c "import json; print(json.load(open('$DESKTOP/src-tauri/tauri.conf.json'))['version'])")"
PUBLISH=0
INSTALL_APPS=0

for arg in "$@"; do
  case "$arg" in
    --publish) PUBLISH=1 ;;
    --install) INSTALL_APPS=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if [[ -z "$IDENTITY" ]]; then
  echo "APPLE_SIGNING_IDENTITY is not set (e.g. 'Developer ID Application: Name (TEAMID)')" >&2
  exit 1
fi
if [[ "$PUBLISH" == 1 ]]; then
  if [[ -z "$RELEASE_REPO" ]]; then
    echo "--publish needs KHIPU_RELEASE_REPO=owner/name (or a gh-visible origin)" >&2
    exit 1
  fi
  # The updater fetches release assets unauthenticated. A private repo answers
  # 404 to that, so publishing there ships a release nobody can install.
  if [[ "$(gh repo view "$RELEASE_REPO" --json isPrivate -q .isPrivate 2>/dev/null)" != "false" ]]; then
    echo "refusing to publish: $RELEASE_REPO is private or unreadable; the updater cannot fetch from it" >&2
    exit 1
  fi
  ENDPOINT="$(python3 -c "import json; print(json.load(open('$DESKTOP/src-tauri/tauri.conf.json'))['plugins']['updater']['endpoints'][0])")"
  if [[ "$ENDPOINT" != "https://github.com/$RELEASE_REPO/releases/latest/download/latest.json" ]]; then
    echo "refusing to publish: tauri.conf.json updater endpoint is $ENDPOINT, not the GitHub Releases feed of $RELEASE_REPO" >&2
    exit 1
  fi
fi
if [[ ! -f "$KEY" ]]; then
  echo "missing updater private key: $KEY" >&2
  echo "generate with: cd apps/desktop && npx tauri signer generate -w ~/.tauri/khipu.key" >&2
  exit 1
fi

export TAURI_SIGNING_PRIVATE_KEY_PATH="$KEY"
export TAURI_SIGNING_PRIVATE_KEY="$(cat "$KEY")"
# Key generated with --ci / no password
export TAURI_SIGNING_PRIVATE_KEY_PASSWORD="${TAURI_SIGNING_PRIVATE_KEY_PASSWORD:-}"
export APPLE_SIGNING_IDENTITY="$IDENTITY"
export CI="${CI:-true}"

cd "$DESKTOP"
npm run tauri -- build

BUNDLE="$DESKTOP/src-tauri/target/release/bundle/macos/Khipu.app"
DMG="$(ls -1 "$DESKTOP/src-tauri/target/release/bundle/dmg/"Khipu_*.dmg 2>/dev/null | head -1 || true)"
SIG="$(ls -1 "$DESKTOP/src-tauri/target/release/bundle/macos/"*.app.tar.gz.sig 2>/dev/null | head -1 || true)"
TGZ="$(ls -1 "$DESKTOP/src-tauri/target/release/bundle/macos/"*.app.tar.gz 2>/dev/null | head -1 || true)"

echo "app:  $BUNDLE"
echo "dmg:  ${DMG:-none}"
echo "tgz:  ${TGZ:-none}"
echo "sig:  ${SIG:-none}"

inject_ls_environment() {
  local app="$1"
  local plist="$app/Contents/Info.plist"
  local root_val="${KHIPU_ROOT:-$ROOT}"
  local py_val="${KHIPU_PYTHON:-}"
  if [[ -z "$py_val" ]]; then
    py_val="$(command -v python3.11 2>/dev/null || command -v python3 2>/dev/null || true)"
  fi
  if [[ -z "$py_val" || ! -x "$py_val" ]]; then
    echo "KHIPU_PYTHON unset and no python3 on PATH; cannot inject LSEnvironment" >&2
    exit 1
  fi
  if [[ ! -f "$plist" ]]; then
    echo "Info.plist missing: $plist" >&2
    exit 1
  fi
  /usr/libexec/PlistBuddy -c "Delete :LSEnvironment" "$plist" 2>/dev/null || true
  /usr/libexec/PlistBuddy -c "Add :LSEnvironment dict" "$plist"
  /usr/libexec/PlistBuddy -c "Add :LSEnvironment:KHIPU_ROOT string $root_val" "$plist"
  /usr/libexec/PlistBuddy -c "Add :LSEnvironment:KHIPU_PYTHON string $py_val" "$plist"
  echo "LSEnvironment: KHIPU_ROOT=$root_val KHIPU_PYTHON=$py_val"
  # Plist edit invalidates the prior signature — re-sign deep + runtime.
  codesign --force --deep --options runtime --sign "$IDENTITY" "$app"
  codesign --verify --verbose=2 "$app"
}

repack_updater_artifacts() {
  local app="$1"
  local out_dir
  out_dir="$(dirname "$app")"
  local tgz_name="Khipu.app.tar.gz"
  local tgz_path="$out_dir/$tgz_name"
  # Drop any pre-inject updater artifacts (name may vary from Tauri).
  rm -f "$out_dir"/*.app.tar.gz "$out_dir"/*.app.tar.gz.sig
  # Match Tauri's updater layout: gzip tarball of Khipu.app
  tar -C "$out_dir" -czf "$tgz_path" "$(basename "$app")"
  # Prefer key path only — env TAURI_SIGNING_PRIVATE_KEY (set for `tauri build`)
  # conflicts with `-f` / --private-key-path on newer tauri-cli.
  env -u TAURI_SIGNING_PRIVATE_KEY npx tauri signer sign "$tgz_path" -f "$KEY"
  TGZ="$tgz_path"
  SIG="$tgz_path.sig"
  echo "repacked updater: $TGZ / $SIG"
}

if [[ ! -d "$BUNDLE" ]]; then
  echo "bundle missing after build" >&2
  exit 1
fi

inject_ls_environment "$BUNDLE"
# Updater tarball / DMG from tauri build predate LSEnvironment — always
# rebuild tarball so plain builds don't leave a stale pre-inject .app.tar.gz.
# Remove stale DMG file(s) (would lack LSEnvironment); do not publish them.
repack_updater_artifacts "$BUNDLE"
DMG_DIR="$DESKTOP/src-tauri/target/release/bundle/dmg"
if [[ -d "$DMG_DIR" ]]; then
  shopt -s nullglob
  stale_dmgs=("$DMG_DIR"/Khipu_*.dmg)
  shopt -u nullglob
  if ((${#stale_dmgs[@]} > 0)); then
    echo "note: removing stale pre-inject DMG(s) (built before LSEnvironment inject); use .app.tar.gz or --install" >&2
    rm -f "${stale_dmgs[@]}"
  fi
fi
DMG=""

if [[ "$INSTALL_APPS" -eq 1 ]]; then
  # /Applications delivery is the documented install path.
  rm -rf /Applications/Khipu.app
  ditto "$BUNDLE" /Applications/Khipu.app
  echo "installed /Applications/Khipu.app"
fi

if [[ "$PUBLISH" -eq 1 ]]; then
  if [[ -z "${TGZ:-}" || -z "${SIG:-}" ]]; then
    echo "missing updater tarball/signature — was createUpdaterArtifacts enabled?" >&2
    exit 1
  fi
  # One release per version: tarball + .sig + latest.json as assets. The
  # `latest` redirect GitHub keeps for the newest non-prerelease release is
  # what the app polls, so latest.json must be attached to every release.
  # Authenticity is the minisign signature inside latest.json, not the host.
  TAG="v$VERSION"
  NOTES_DIR="$(mktemp -d)"
  TGZ_NAME="$(basename "$TGZ")"
  ASSET_BASE="https://github.com/$RELEASE_REPO/releases/download/$TAG"
  cat >"$NOTES_DIR/latest.json" <<EOF
{
  "version": "$VERSION",
  "notes": "Khipu $VERSION",
  "pub_date": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "platforms": {
    "darwin-aarch64": {
      "signature": $(python3 -c "import json,sys; print(json.dumps(sys.stdin.read().strip()))" <"$SIG"),
      "url": "$ASSET_BASE/$TGZ_NAME"
    }
  }
}
EOF
  if gh release view "$TAG" --repo "$RELEASE_REPO" >/dev/null 2>&1; then
    echo "release $TAG already exists on $RELEASE_REPO; bump the version" >&2
    exit 1
  fi
  gh release create "$TAG" --repo "$RELEASE_REPO" --title "Khipu $VERSION" \
    --notes "Khipu $VERSION — signed macOS build (darwin-aarch64). \
Install: download Khipu.app.tar.gz, or let an installed Khipu update itself from Settings." \
    "$TGZ" "$SIG" "$NOTES_DIR/latest.json"
  echo "published $VERSION to https://github.com/$RELEASE_REPO/releases/tag/$TAG"
  # Prove the feed the app polls actually serves this version.
  curl -fsSL "https://github.com/$RELEASE_REPO/releases/latest/download/latest.json" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print('served version:', d['version'], '->', d['platforms']['darwin-aarch64']['url'])"
fi
