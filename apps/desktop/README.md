# Khipu desktop (Tauri 2 + React)

P1d shell over `packages/cli/`.

```bash
cd apps/desktop
npm install
# needs a DSN: `khipu secrets --set database_url` (stdin) or ~/.config/khipu/dsn
npm run tauri dev
```

Screens: Status · Search · Graph · Doctor · Settings · First-run.  
Menu-bar tray uses the same B9 clay squircle as the Dock (`icons/trayIcon@2x.png`, not a template). Tooltip reflects `khipu doctor` / `khipu status` (fail-soft if CLI down).

## Install (`/Applications`)

```bash
./scripts/release_macos.sh --install
# → /Applications/Khipu.app (needs APPLE_SIGNING_IDENTITY)
```

`release_macos.sh` injects Info.plist `LSEnvironment` with `KHIPU_ROOT` (repo root on the build machine, or `$KHIPU_ROOT`) and `KHIPU_PYTHON` (`$KHIPU_PYTHON` or `python3` on PATH), then re-signs. The release binary does **not** silently assume any repo location when unset — set those env vars or reinstall via the script.

External CLI contract (if launching without LSEnvironment): export `KHIPU_ROOT` to the repo root and `KHIPU_PYTHON` to a real `python3` (or ensure `python3` is on PATH).

Dev-only Dock face: `src-tauri/target/...` or `target/Khipu.dev.app`. Prefer **Khipu.app** from `/Applications` once installed.

## Auto-update (Tauri updater — Sparkle-equivalent)

Defaults: **tauri-plugin-updater** + **GitHub Releases** feed  
`https://github.com/mrkinglollipop/Khipu/releases/latest/download/latest.json`

- Public key lives in `src-tauri/tauri.conf.json` (`plugins.updater.pubkey`).
- Private key stays **off-repo**: `~/.tauri/khipu.key` (generate with `npx tauri signer generate -w ~/.tauri/khipu.key`).
- Launch: **check-only** (fail-soft log / tray note; no auto-download or restart).
- Install: **Settings → Check for updates** (user-initiated download + install).
- `latest.json` currently lists **darwin-aarch64** only (no fake x86_64 entry).

Publish a desktop release (build artifacts + `latest.json`):

```bash
./scripts/release_macos.sh --publish
# optional: ./scripts/release_macos.sh --install --publish
```

Requires `gh` auth and, in the environment: `APPLE_SIGNING_IDENTITY` (your
Developer ID Application identity — Tauri's bundler reads the same variable) and,
for `--publish`, `KHIPU_RELEASE_REPO=owner/name` (defaults to this checkout's
origin). The script refuses to publish to a private repo, because the updater
fetches release assets unauthenticated. Bump `version` in
`src-tauri/tauri.conf.json`, `package.json` and `src-tauri/Cargo.toml` first.

## Icons

App icon (SSOT): `brand/khipu-icon-rounded.png` — B9 clay with transparent squircle corners.

```bash
npx tauri icon brand/khipu-icon-rounded.png
# then re-bake color tray from brand/khipu-icon-rounded.png → icons/trayIcon{,@2x}.png
```

Tray: `icons/trayIcon@2x.png` (+ `.rgba` fallback), full-color B9, `icon_as_template(false)`. Status item is kept alive via `app.manage(tray)`.
