# Khipu desktop (Tauri 2 + React)

P1d shell over `packages/cli/`.

## Install (recommended)

Download the latest Apple Silicon DMG (`Khipu_<version>_aarch64.dmg`) from the
[latest GitHub Release](https://github.com/mrkinglollipop/Khipu/releases/latest)
or [kinglollipop.com/khipu](https://kinglollipop.com/khipu/), open it, and drag
**Khipu.app** to **Applications**. Launch from Applications — the Welcome flow
configures Postgres, models, Graphify, and agent integrations. The app bundle
carries CLI + Python under `Contents/Resources/khipu`; no git clone or
`KHIPU_ROOT` env var is required.

**Gatekeeper / notarization**

| Build | What you get |
|---|---|
| **Notarized** (0.3.0+ when release credentials are set) | Developer ID signed + Apple notarized + stapled DMG — opens normally |
| **Developer ID, not notarized** (e.g. 0.2.9) | Signed app, no staple — if blocked, right-click **Khipu.app** → **Open** once |
| **Ad-hoc / unsigned** | Local dev builds only — not for distribution |

Maintainers: `release_macos.sh` runs `xcrun notarytool` + `xcrun stapler staple`
when `APPLE_ID` + `APPLE_APP_SPECIFIC_PASSWORD` (+ `APPLE_TEAM_ID`) or
`APPLE_API_KEY` + `APPLE_API_KEY_ID` + `APPLE_API_ISSUER` are in the environment.
Without those, the script prints **Blocked on Matt** and **exits nonzero** — it
does not silently publish. Use a prior stapled DMG or set the credentials.

## Auto-update (tarball — not the DMG)

The **updater** downloads a signed **`Khipu.app.tar.gz`** + minisign signature from
GitHub Releases — not the DMG. Defaults: **tauri-plugin-updater** +
`https://github.com/mrkinglollipop/Khipu/releases/latest/download/latest.json`

- Public key: `src-tauri/tauri.conf.json` (`plugins.updater.pubkey`).
- Private key off-repo: `~/.tauri/khipu.key` (`npx tauri signer generate -w ~/.tauri/khipu.key`).
- Launch: check-only (fail-soft log / tray note).
- Install: **Settings → Check for updates** (user-initiated download + install).
- `latest.json` lists **darwin-aarch64** only.

Compat matrix (Postgres / pgvector / Graphify versions) is fetched from
[`mrkinglollipop/khipu-compat`](https://github.com/mrkinglollipop/khipu-compat),
not from the app release feed.

## Maintainer release build

```bash
cd apps/desktop
export APPLE_SIGNING_IDENTITY='Developer ID Application: Name (TEAMID)'
# Notarization env is required for a zero exit (script fail-closes without it).
export APPLE_API_KEY='/path/to/AuthKey.p8'
export APPLE_API_KEY_ID='KEYID'
export APPLE_API_ISSUER='ISSUER-UUID'
# or: APPLE_ID + APPLE_APP_SPECIFIC_PASSWORD + APPLE_TEAM_ID
./scripts/release_macos.sh
# → Khipu.app + Khipu_<version>_aarch64.dmg in src-tauri/target/release/bundle/
# → updater tarball + .sig in .../bundle/macos/
```

Flags:

```bash
cd apps/desktop
./scripts/release_macos.sh --install    # copy build to /Applications/Khipu.app
./scripts/release_macos.sh --publish    # gh release create + kinglollipop.com verify
```

`--publish` requires `gh` auth, a **public** `KHIPU_RELEASE_REPO` (defaults to
origin), a version bump in `src-tauri/tauri.conf.json`, `package.json`, and
`src-tauri/Cargo.toml`, and a **stapled** DMG (`xcrun stapler validate`). The
script refuses private repos (updater fetches unauthenticated). After
`gh release create` it bumps kinglollipop.com Khipu copy and the
`_redirects` DMG pin (studio `scripts/bump_khipu_site.py`), deploys Pages, and
`scripts/verify_khipu_website.py` must see that version on the page plus a
working `/khipu/download` 302. `KHIPU_SKIP_SITE=1` skips that gate.

Notarization env (required for `release_macos.sh` to exit 0):

```bash
export APPLE_ID='you@example.com'
export APPLE_APP_SPECIFIC_PASSWORD='xxxx-xxxx-xxxx-xxxx'
export APPLE_TEAM_ID='TEAMID'
# or: APPLE_API_KEY, APPLE_API_KEY_ID, APPLE_API_ISSUER
```

## Dev

```bash
cd apps/desktop
npm install
# needs a DSN: `khipu secrets --set database_url` (stdin) or ~/.config/khipu/dsn
npm run tauri dev
```

Screens: Status · Search · Graph · Doctor · Settings · First-run · Components.  
Menu-bar tray uses the same B9 clay squircle as the Dock (`icons/trayIcon@2x.png`, not a template). Tooltip reflects `khipu doctor` / `khipu status` (fail-soft if CLI down).

Dev-only Dock face: `src-tauri/target/...` or `target/Khipu.dev.app`. Prefer **Khipu.app** from `/Applications` once installed.

## Icons

App icon (SSOT): `brand/khipu-icon-rounded.png` — B9 clay with transparent squircle corners.

```bash
npx tauri icon brand/khipu-icon-rounded.png
# then re-bake color tray from brand/khipu-icon-rounded.png → icons/trayIcon{,@2x}.png
```

Tray: `icons/trayIcon@2x.png` (+ `.rgba` fallback), full-color B9, `icon_as_template(false)`. Status item is kept alive via `app.manage(tray)`.
