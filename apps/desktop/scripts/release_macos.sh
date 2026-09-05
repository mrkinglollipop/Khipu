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
# After build, the portable bundle carries CLI + Python under Contents/Resources/khipu
# (bundle_cli.sh). Do not inject Info.plist LSEnvironment — that tied releases to
# the builder checkout and Homebrew python.
#
# --selftest-dmg-layout  dummy .app → UDZO with Applications alias; no signing.
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
SELFTEST_DMG=0

for arg in "$@"; do
  case "$arg" in
    --publish) PUBLISH=1 ;;
    --install) INSTALL_APPS=1 ;;
    --selftest-dmg-layout) SELFTEST_DMG=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# Drag-to-Applications installer window (Murmur make-dmg.sh pattern). Tauri's
# first DMG has the Applications alias; we recreate after re-sign, so the
# alias + Finder icon layout have to live here or the shipped image is a
# lone .app and strangers never see "drag this into Applications".
DMG_VOLNAME="Khipu"

DMG_BACKGROUND="$DESKTOP/src-tauri/dmg/dmg-background@2x.png"

stage_dmg_payload() {
  local app="$1"
  local stage="$2"
  ditto "$app" "$stage/Khipu.app"
  ln -s /Applications "$stage/Applications"
  # Branded backdrop (wordmark, quipu cords, drag arrow) — regenerate with
  # src-tauri/dmg/make_background.py if the palette or layout changes.
  mkdir "$stage/.background"
  cp "$DMG_BACKGROUND" "$stage/.background/background.png"
}

detach_dmg_volume() {
  local volpath="/Volumes/${DMG_VOLNAME}"
  if [[ -e "$volpath" ]]; then
    hdiutil detach "$volpath" -quiet 2>/dev/null || hdiutil detach "$volpath" -force -quiet 2>/dev/null || true
  fi
}

# Icon view: Khipu.app left, Applications right. Fail-soft — the alias is
# load-bearing; positions are nicety. Finder addresses the volume by name
# (must mount at /Volumes/Khipu, not a custom mountpoint).
layout_dmg_finder_window() {
  osascript <<EOF
tell application "Finder"
  tell disk "${DMG_VOLNAME}"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set the bounds of container window to {200, 120, 860, 520}
    set theViewOptions to the icon view options of container window
    set arrangement of theViewOptions to not arranged
    set icon size of theViewOptions to 128
    set background picture of theViewOptions to file ".background:background.png"
    set position of item "Khipu.app" of container window to {180, 170}
    set position of item "Applications" of container window to {480, 170}
    update without registering applications
    delay 1
    close
  end tell
end tell
EOF
}

verify_dmg_drag_install() {
  local dmg_path="$1"
  local mnt
  mnt="$(mktemp -d /tmp/khipu-dmg-verify-XXXXXX)"
  if ! hdiutil attach "$dmg_path" -nobrowse -readonly -mountpoint "$mnt" >/dev/null; then
    rmdir "$mnt" 2>/dev/null || true
    echo "error: could not attach $dmg_path to verify installer layout" >&2
    return 1
  fi
  local ok=1
  if [[ ! -d "$mnt/Khipu.app" ]]; then
    echo "error: DMG is missing Khipu.app" >&2
    ok=0
  fi
  if [[ ! -f "$mnt/.background/background.png" ]]; then
    echo "error: DMG is missing the branded background" >&2
    ok=0
  fi
  if [[ ! -L "$mnt/Applications" ]]; then
    echo "error: DMG is missing the Applications symlink (drag-install target)" >&2
    ok=0
  else
    local target
    target="$(readlink "$mnt/Applications")"
    if [[ "$target" != "/Applications" ]]; then
      echo "error: Applications symlink points at $target, expected /Applications" >&2
      ok=0
    fi
  fi
  hdiutil detach "$mnt" -quiet 2>/dev/null || hdiutil detach "$mnt" -force -quiet 2>/dev/null || true
  rmdir "$mnt" 2>/dev/null || true
  [[ "$ok" == 1 ]]
}

# Build a UDZO DMG whose Finder window is the standard drag-to-Applications
# installer. skip_sign=1 for --selftest-dmg-layout (no Developer ID needed).
build_portable_dmg() {
  local app="$1"
  local dmg_path="$2"
  local skip_sign="${3:-0}"
  local stage rw
  stage="$(mktemp -d /tmp/khipu-dmg-stage-XXXXXX)"
  rw="$(mktemp /tmp/khipu-dmg-rw-XXXXXX)"
  rm -f "$rw"
  rw="${rw}.dmg"
  mkdir -p "$(dirname "$dmg_path")"
  rm -f "$dmg_path"
  stage_dmg_payload "$app" "$stage"
  hdiutil create -volname "$DMG_VOLNAME" -srcfolder "$stage" -ov -format UDRW "$rw" >/dev/null
  rm -rf "$stage"
  detach_dmg_volume
  if ! hdiutil attach -readwrite -noverify -noautoopen "$rw" >/dev/null; then
    rm -f "$rw"
    echo "error: could not attach read-write DMG for Finder layout" >&2
    return 1
  fi
  if ! layout_dmg_finder_window; then
    echo "warning: Finder did not apply icon positions; Applications alias is still in the image" >&2
  fi
  sync
  detach_dmg_volume
  hdiutil convert "$rw" -format UDZO -imagekey zlib-level=9 -ov -o "$dmg_path" >/dev/null
  rm -f "$rw"
  verify_dmg_drag_install "$dmg_path"
  if [[ "$skip_sign" != 1 ]]; then
    codesign --force --timestamp --sign "$IDENTITY" "$dmg_path"
  fi
}

selftest_dmg_layout() {
  local tmp dummy dmg
  tmp="$(mktemp -d /tmp/khipu-dmg-selftest-XXXXXX)"
  dummy="$tmp/Khipu.app"
  mkdir -p "$dummy/Contents/MacOS"
  cat > "$dummy/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>Khipu</string>
  <key>CFBundleIdentifier</key><string>com.matt.khipu.selftest</string>
  <key>CFBundleName</key><string>Khipu</string>
  <key>CFBundlePackageType</key><string>APPL</string>
</dict></plist>
PLIST
  printf '#!/bin/sh\nexit 0\n' > "$dummy/Contents/MacOS/Khipu"
  chmod +x "$dummy/Contents/MacOS/Khipu"
  dmg="$tmp/Khipu_selftest.dmg"
  build_portable_dmg "$dummy" "$dmg" 1
  rm -rf "$tmp"
  echo "dmg layout selftest ok"
}

if [[ "$SELFTEST_DMG" == 1 ]]; then
  selftest_dmg_layout
  exit 0
fi

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
# Cursor sandbox CARGO_TARGET_DIR collides with nested resource name `khipu/`
# and can lock the output binary. Force the crate-local target.
export CARGO_TARGET_DIR="$DESKTOP/src-tauri/target"

"$DESKTOP/scripts/bundle_cli.sh"

# Nested CPython / pip dylibs ship with vendor or ad-hoc signatures. Notary
# rejects those (Developer ID + secure timestamp + hardened runtime).
sign_macho_tree() {
  local tree="$1"
  local entitlements="$DESKTOP/scripts/python-hardened.entitlements"
  python3 - "$tree" "$IDENTITY" "$entitlements" <<'PY'
import os, subprocess, sys
from pathlib import Path

tree, identity, entitlements = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
macho_magics = (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf")
python_names = {"python", "python3", "python3.11", "libpython3.11.dylib"}
signed = 0
for dirpath, _, filenames in os.walk(tree):
    for name in filenames:
        path = Path(dirpath) / name
        try:
            head = path.read_bytes()[:4]
        except OSError:
            continue
        if head not in macho_magics:
            continue
        cmd = [
            "codesign",
            "--force",
            "--options",
            "runtime",
            "--timestamp",
            "--sign",
            identity,
        ]
        if path.name in python_names:
            cmd.extend(["--entitlements", str(entitlements)])
        cmd.append(str(path))
        subprocess.run(cmd, check=True)
        signed += 1
print(f"signed {signed} nested Mach-O under {tree}")
PY
}

sign_macho_tree "$DESKTOP/khipu-resources"

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

# Tauri signs the outer .app; nested Resources Mach-O still need Developer ID
# + timestamp (notary Invalid without this). Re-sign inside-out, then rebuild
# the DMG so the image matches the sealed .app.
codesign --force --deep --options runtime --timestamp --sign "$IDENTITY" "$BUNDLE"
codesign --verify --deep --strict --verbose=2 "$BUNDLE"

recreate_portable_dmg() {
  local app="$1"
  local dmg_dir="$DESKTOP/src-tauri/target/release/bundle/dmg"
  local dmg_path="$dmg_dir/Khipu_${VERSION}_aarch64.dmg"
  mkdir -p "$dmg_dir"
  rm -f "$dmg_dir"/Khipu_*.dmg
  build_portable_dmg "$app" "$dmg_path" 0
  DMG="$dmg_path"
  echo "recreated portable dmg: $DMG"
}

recreate_portable_dmg "$BUNDLE"

# Task 2/3: portable bundle — no LSEnvironment inject (Resources/khipu is SSOT).
repack_updater_artifacts "$BUNDLE"
echo "portable dmg: ${DMG:-none}"

dmg_is_stapled() {
  local dmg="$1"
  [[ -n "$dmg" && -f "$dmg" ]] && xcrun stapler validate "$dmg" >/dev/null 2>&1
}

notarize_dmg() {
  local dmg="$1"
  if [[ -z "$dmg" || ! -f "$dmg" ]]; then
    echo "no DMG to notarize" >&2
    return 1
  fi

  if dmg_is_stapled "$dmg"; then
    echo "already stapled: $dmg"
    return 0
  fi

  local have_api_key=0 have_apple_id=0
  if [[ -n "${APPLE_API_KEY:-}" && -n "${APPLE_API_KEY_ID:-}" && -n "${APPLE_API_ISSUER:-}" ]]; then
    have_api_key=1
  fi
  if [[ -n "${APPLE_ID:-}" && -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" ]]; then
    have_apple_id=1
  fi
  if [[ "$have_api_key" -eq 0 && "$have_apple_id" -eq 0 ]]; then
    echo "Blocked on Matt: notarization skipped — set APPLE_ID + APPLE_APP_SPECIFIC_PASSWORD (+ APPLE_TEAM_ID), or APPLE_API_KEY + APPLE_API_KEY_ID + APPLE_API_ISSUER" >&2
    return 1
  fi

  local submit_args=()
  if [[ "$have_api_key" -eq 1 ]]; then
    submit_args=(--key "$APPLE_API_KEY" --key-id "$APPLE_API_KEY_ID" --issuer "$APPLE_API_ISSUER")
  else
    if [[ -z "${APPLE_TEAM_ID:-}" ]]; then
      echo "Blocked on Matt: APPLE_TEAM_ID required with APPLE_ID notarization" >&2
      return 1
    fi
    submit_args=(--apple-id "$APPLE_ID" --password "$APPLE_APP_SPECIFIC_PASSWORD" --team-id "$APPLE_TEAM_ID")
  fi

  echo "notarytool submit: $dmg"
  xcrun notarytool submit "$dmg" "${submit_args[@]}" --wait
  xcrun stapler staple "$dmg"
  if ! dmg_is_stapled "$dmg"; then
    echo "stapler validate failed after staple: $dmg" >&2
    return 1
  fi
  echo "notarized and stapled: $dmg"
}

notarize_dmg "$DMG"

# The notarization ticket covers the .app inside the DMG; staple it to the
# bundle too, then rebuild the updater tarball from the STAPLED app so a
# post-update first launch does not need the network for Gatekeeper.
xcrun stapler staple "$BUNDLE"
xcrun stapler validate "$BUNDLE"
repack_updater_artifacts "$BUNDLE"

if [[ "$INSTALL_APPS" -eq 1 ]]; then
  # /Applications delivery is the documented install path.
  rm -rf /Applications/Khipu.app
  ditto "$BUNDLE" /Applications/Khipu.app
  echo "installed /Applications/Khipu.app"
fi

if [[ "$PUBLISH" -eq 1 ]]; then
  if [[ -z "${TGZ:-}" || -z "${SIG:-}" || -z "${DMG:-}" || ! -f "$DMG" ]]; then
    echo "missing DMG or updater tarball/signature — was createUpdaterArtifacts enabled?" >&2
    exit 1
  fi
  if ! dmg_is_stapled "$DMG"; then
    echo "refusing --publish: $DMG is not Apple-stapled (stapler validate failed)" >&2
    exit 1
  fi
  if ! xcrun stapler validate "$BUNDLE" >/dev/null 2>&1; then
    echo "refusing --publish: $BUNDLE is not stapled" >&2
    exit 1
  fi
  # One release per version: versioned DMG + stable `Khipu_aarch64.dmg` alias
  # (kinglollipop.com /khipu/download → GitHub `/releases/latest/download/…`)
  # + tarball + .sig + latest.json. The `latest` redirect GitHub keeps for the
  # newest non-prerelease release is what the app polls, so latest.json must
  # be attached to every release. Authenticity is the minisign signature
  # inside latest.json, not the host.
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
  RELEASE_NOTES=$(cat <<EOF
${KHIPU_RELEASE_NOTES:-}

Khipu $VERSION — portable macOS arm64.

**Install:** download \`Khipu_${VERSION}_aarch64.dmg\`, drag Khipu.app to Applications, complete Welcome (local PostgreSQL 19 via Docker Desktop, or a remote PG 19 DSN).

**Update:** an installed Khipu pulls \`Khipu.app.tar.gz\` from this release via Settings (same minisign feed as 0.2.x).

Compatibility matrix lives on \`mrkinglollipop/khipu-compat\`, not this repo \`/releases/latest\`.
EOF
)
  # Copy in $NOTES_DIR so a stable name never lands next to Khipu_*.dmg
  # (the glob at the top of this script would otherwise get ambiguous).
  STABLE_DMG="$NOTES_DIR/Khipu_aarch64.dmg"
  cp "$DMG" "$STABLE_DMG"
  gh release create "$TAG" --repo "$RELEASE_REPO" --title "Khipu $VERSION" \
    --notes "$RELEASE_NOTES" \
    "$DMG" "$STABLE_DMG" "$TGZ" "$SIG" "$NOTES_DIR/latest.json"
  echo "published $VERSION to https://github.com/$RELEASE_REPO/releases/tag/$TAG"
  # Prove the feed the app polls actually serves this version.
  curl -fsSL "https://github.com/$RELEASE_REPO/releases/latest/download/latest.json" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print('served version:', d['version'], '->', d['platforms']['darwin-aarch64']['url'])"
  STUDIO="${KING_LOLLIPOP_STUDIO:-/Volumes/Cloud Storage/Code/king-lollipop-studio}"
  if [[ -d "$STUDIO/tools/x_post" ]]; then
    echo "==> X draft in studio (not published)"
    if ! (cd "$STUDIO" && python3 -m tools.x_post draft-from-github --app khipu --tag "$TAG" --commit --push); then
      echo "warning: X draft failed (GitHub release $TAG is already live)" >&2
    fi
  else
    echo "warning: studio not at $STUDIO; skip X draft (set KING_LOLLIPOP_STUDIO)" >&2
  fi
  if [[ "${KHIPU_SKIP_SITE:-}" == 1 ]]; then
    echo "skipping kinglollipop.com sync (KHIPU_SKIP_SITE=1)"
  else
    echo "==> kinglollipop.com Khipu $VERSION"
    if [[ -f "$STUDIO/scripts/bump_khipu_site.py" ]]; then
      python3 "$STUDIO/scripts/bump_khipu_site.py" "$VERSION"
      (
        cd "$STUDIO"
        branch="$(git rev-parse --abbrev-ref HEAD)"
        if [[ "$branch" != "main" ]]; then
          echo "warning: studio HEAD is $branch, not main; site bump left uncommitted" >&2
        else
          head_before="$(git rev-parse HEAD)"
          git add -- site/khipu/index.html site/news.json site/assets/now.js site/_redirects
          if git diff --cached --quiet; then
            echo "studio site copy already at $VERSION"
          else
            git commit -m "chore(site): bump Khipu page to $VERSION"
            if git rev-parse refs/remotes/origin/main >/dev/null 2>&1; then
              origin_main="$(git rev-parse refs/remotes/origin/main)"
              if [[ "$head_before" == "$origin_main" ]]; then
                git push origin HEAD:main || echo "warning: studio site bump push failed (Pages deploy still uses this tree)" >&2
              else
                echo "warning: studio main has unpushed commits; not pushing site bump (refusing to push unrelated WIP)" >&2
              fi
            fi
          fi
        fi
      )
      if [[ -x "$STUDIO/scripts/deploy-site.sh" ]]; then
        "$STUDIO/scripts/deploy-site.sh"
      else
        echo "Blocked on Matt: $STUDIO/scripts/deploy-site.sh missing" >&2
        exit 1
      fi
    else
      echo "warning: $STUDIO/scripts/bump_khipu_site.py missing; verifying live copy only" >&2
    fi
    python3 "$DESKTOP/scripts/verify_khipu_website.py" "$VERSION"
  fi
fi
