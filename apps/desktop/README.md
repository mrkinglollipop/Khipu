# Khipu desktop app

The Khipu desktop app: a Tauri 2 and React 19 interface (Vite, TypeScript) that calls the bundled CLI in `packages/cli/`. One window (1100 by 720, minimum 960 by 600). Version 0.4.15. It needs macOS 12 or later on Apple silicon.

## Screens

The left rail has six destinations:

- **Home**: is memory working right now. Health callouts, tiles for Database, Recording, Search index and Backups, a Right now card, plan usage and routing cards, and recent captures.
- **Recall**: a Search | Graph switch. Search modes are Best match (hybrid, the default), By meaning and Exact words. Graph walks what a topic or node connects to.
- **Owed**: commitments by project, filtered Open, Closed or Stale, with snooze.
- **Activity**: every capture, newest first. Open one to read, correct or forget it.
- **Harnesses**: which agents are wired in and whether each is recording. Install, verify and remove per harness (Claude Code, Cursor, Aegis, Codex). Grok Bot runs in the cloud, so it has nothing local to install; its Verify probes the gateway.
- **Settings**: ten sections, in this order: Database, Capture & models, Routing, Optional features, Embeddings, Data & backups, Another Mac, Components, Updates, Advanced.

Two more views are not in the rail. The Welcome flow (six steps: Welcome, Database, Model, Graph, Agents, Finish) runs on first launch and again from Settings > Advanced > Run setup again. Conflicting edits is reached from Home or Settings > Advanced.

## Install

Download the latest Apple silicon DMG (`Khipu_<version>_aarch64.dmg`) from the [latest GitHub Release](https://github.com/mrkinglollipop/Khipu/releases/latest) or the project website. Open it and drag **Khipu.app** to **Applications**. Launch it from Applications. The Welcome flow sets up the database, a session-summary model, the graph builder and agent integrations, then runs a final health check.

The app bundle carries the CLI and Python under `Contents/Resources/khipu`. You do not need a git clone or the `KHIPU_ROOT` environment variable. (`KHIPU_ROOT`, if set, overrides where the app looks for the CLI. Debug builds fall back to the source checkout.)

**Gatekeeper and notarization**

| Build | What you get |
|---|---|
| Notarized | Developer ID signed, Apple notarized and stapled. Opens normally. |
| Developer ID, not notarized | Signed, no staple. If macOS blocks it, right-click **Khipu.app** and choose **Open** once. |
| Ad-hoc or unsigned | Local dev builds only. Not for distribution. |

## Updates

The updater uses `tauri-plugin-updater`. It reads `https://github.com/mrkinglollipop/Khipu/releases/latest/download/latest.json` and downloads a signed `Khipu.app.tar.gz` plus a minisign signature. It does not use the DMG. The public key is in `src-tauri/tauri.conf.json` (`plugins.updater.pubkey`). The private signing key is kept outside the repo.

- On launch the app only checks. If a newer version exists, the tray tooltip reads "Khipu · update vX available". Nothing is downloaded or installed.
- To install, go to Settings > Updates > Check for updates. The app downloads, installs and relaunches.
- After an upgrade, the app can show a one-time notice for that version.
- `latest.json` lists `darwin-aarch64` only.

The compatibility matrix (Postgres, pgvector and Graphify versions) is fetched from [`mrkinglollipop/khipu-compat`](https://github.com/mrkinglollipop/khipu-compat), not from the app release feed. `scripts/bundle_cli.sh` stages a floor copy from `docs/compat/khipu-graphify-postgres.json`.

## Tray

The menu-bar tray icon is full color, not a template image. The menu has **Show Khipu**, **Run doctor…** and **Quit**. A left-click shows and focuses the window. The tooltip starts as "Khipu · starting…", then reflects `khipu doctor` (OK, WARN, NOT RECORDING, or doctor unavailable), falling back to `khipu status`. An available update replaces the health tooltip.

## Maintainer release build

Use a checkout on a case-sensitive APFS volume. Tauri stages the bundled CLI at `target/release/khipu/` next to the `target/release/Khipu` binary, and on a case-insensitive volume the two collide. `release_macos.sh` refuses early and says so.

The script also refuses to build when `packages/cli/khipu/__init__.py` and `src-tauri/tauri.conf.json` disagree on the version. Bump the version in all four places: `src-tauri/tauri.conf.json`, `package.json`, `src-tauri/Cargo.toml` and `packages/cli/khipu/__init__.py`.

```bash
cd apps/desktop
export APPLE_SIGNING_IDENTITY='Developer ID Application: Name (TEAMID)'
# Notarization credentials are required for a zero exit. Use one set:
export APPLE_API_KEY='/path/to/AuthKey.p8'
export APPLE_API_KEY_ID='KEYID'
export APPLE_API_ISSUER='ISSUER-UUID'
# or: APPLE_ID, APPLE_APP_SPECIFIC_PASSWORD and APPLE_TEAM_ID
./scripts/release_macos.sh
```

The script runs `scripts/bundle_cli.sh`, builds with Tauri, signs, notarizes with `notarytool` and staples. Without notarization credentials it prints a "Blocked" message naming the variables to set and exits nonzero. It does not publish silently.

Output:

- `Khipu.app`, the updater tarball and its `.sig` in `src-tauri/target/release/bundle/macos/`
- `Khipu_<version>_aarch64.dmg` in `src-tauri/target/release/bundle/dmg/`

Flags:

```bash
./scripts/release_macos.sh --install    # copy the build to /Applications/Khipu.app
./scripts/release_macos.sh --publish    # create the GitHub release and verify the website
```

`--publish` needs:

- `gh` authenticated.
- A public release repo (`KHIPU_RELEASE_REPO`, which defaults to origin). The updater fetches without authentication, so the script refuses a private repo.
- A stapled DMG (`xcrun stapler validate`).

It uploads the versioned DMG, a stable `Khipu_aarch64.dmg`, the updater tarball, its `.sig` and `latest.json`, then checks the fetched `latest.json`. It then syncs the project website and runs `scripts/verify_khipu_website.py`, which must see the new version. Set `KHIPU_SKIP_SITE=1` to skip the website step.

## Development

Prerequisites: Node and npm, Rust, and the Xcode command-line tools. Use a case-sensitive volume (see above).

`src-tauri/tauri.conf.json` bundles `khipu-resources/` as `Contents/Resources/khipu`. That folder is generated and gitignored, so a fresh checkout does not have it. Create it first:

```bash
cd apps/desktop
./scripts/bundle_cli.sh      # needs network; arm64 macOS only
npm install
npm run tauri dev
```

The dev server runs at `http://localhost:1430`. The app needs a database connection string. Store one with `khipu secrets --set database_url` (reads stdin), or set `KHIPU_DSN_FILE`, or place a file at `~/.config/khipu/dsn`. Prefer the installed **Khipu.app** for daily use.

Other scripts:

| Command | What it does |
|---|---|
| `npm run dev` | Vite dev server only |
| `npm run build` | `tsc` then `vite build` |
| `npm run check:setup` | `vitest run` |

Rust tests live in `src-tauri/src/lib.rs` and run with `cargo test` from `src-tauri/`.

## Icons

The app icon source is `brand/khipu-icon-rounded.png` (transparent squircle corners).

```bash
npx tauri icon brand/khipu-icon-rounded.png
# then re-bake the color tray images from the same source into
# src-tauri/icons/trayIcon.png and trayIcon@2x.png
```

The tray uses `src-tauri/icons/trayIcon@2x.png` (plus a `.rgba` copy the app embeds), full color, with `icon_as_template(false)`. The app keeps the tray handle alive with `app.manage(tray)`.
