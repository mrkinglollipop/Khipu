use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::Value;
use tauri::{
    image::Image,
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIcon, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager,
};
use tauri_plugin_updater::UpdaterExt;

const DSN_CACHE_TTL: Duration = Duration::from_secs(30);

struct DsnCache {
    value: bool,
    checked_at: Instant,
}

static DSN_CACHE: Mutex<Option<DsnCache>> = Mutex::new(None);

/// Health and updater both refresh the tray tooltip asynchronously.
/// Merge under one mutex so they never clobber each other; update-available wins.
struct TrayTooltipState {
    health: Option<String>,
    update_available: Option<String>,
}

enum TrayTooltipSource {
    Health,
    UpdateAvailable,
}

static TRAY_TOOLTIP: Mutex<TrayTooltipState> = Mutex::new(TrayTooltipState {
    health: None,
    update_available: None,
});

fn apply_tray_tooltip(app: &AppHandle, source: TrayTooltipSource, text: Option<String>) {
    let tip = {
        let mut state = match TRAY_TOOLTIP.lock() {
            Ok(g) => g,
            Err(e) => e.into_inner(),
        };
        match source {
            TrayTooltipSource::Health => state.health = text,
            TrayTooltipSource::UpdateAvailable => state.update_available = text,
        }
        // Prefer update-available over health when both are set.
        state
            .update_available
            .clone()
            .or_else(|| state.health.clone())
            .unwrap_or_else(|| "Khipu".into())
    };
    if let Some(tray) = app.try_state::<TrayIcon>() {
        if let Err(e) = tray.set_tooltip(Some(&tip)) {
            eprintln!("[khipu-tray] set_tooltip failed: {e}");
        }
    }
}

fn env_first(keys: &[&str]) -> Option<String> {
    for key in keys {
        if let Ok(v) = std::env::var(key) {
            if !v.trim().is_empty() {
                return Some(v);
            }
        }
    }
    None
}

/// Prefer `KHIPU_ROOT` / `ALZY_ROOT`. Debug builds fall back to the checkout
/// they were built from; release builds require env (typically Info.plist
/// `LSEnvironment` injected by `release_macos.sh`).
fn khipu_root() -> Result<PathBuf, String> {
    if let Some(v) = env_first(&["KHIPU_ROOT", "ALZY_ROOT"]) {
        return Ok(PathBuf::from(v));
    }
    #[cfg(debug_assertions)]
    {
        // Dev builds run from the checkout: this crate is apps/desktop/src-tauri,
        // so the repo root is three levels up. Baked in at compile time.
        return Ok(PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../..")
            .canonicalize()
            .unwrap_or_else(|_| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..")));
    }
    #[cfg(not(debug_assertions))]
    {
        Err(
            "KHIPU_ROOT is not set. Reinstall with apps/desktop/scripts/release_macos.sh --install \
(sets Info.plist LSEnvironment), or export KHIPU_ROOT to the Khipu repo root before launching."
                .into(),
        )
    }
}

fn python_from_which() -> Option<PathBuf> {
    let output = Command::new("/usr/bin/which")
        .arg("python3")
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let path = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if path.is_empty() {
        return None;
    }
    let p = PathBuf::from(&path);
    if p.is_file() {
        Some(p)
    } else {
        None
    }
}

/// Runs `<path> -c "..."` to read (major, minor). None if the interpreter
/// can't be spawned or its version can't be parsed.
fn python_version(path: &PathBuf) -> Option<(u32, u32)> {
    let output = Command::new(path)
        .arg("-c")
        .arg("import sys; print('%d.%d' % sys.version_info[:2])")
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let (major_s, minor_s) = text.split_once('.')?;
    Some((major_s.parse().ok()?, minor_s.parse().ok()?))
}

/// Prefer `KHIPU_PYTHON` / `ALZY_PYTHON`, then `which python3` on PATH.
/// Debug builds may fall back to Homebrew python3.11 when present.
///
/// `KHIPU_PYTHON` / LSEnvironment is trusted as-is (the working happy path —
/// do not add a version check there). Only the `which python3` fallback is
/// gated: `/usr/bin/python3` is 3.9 on this Mac and the CLI needs 3.11+
/// (`typing.TypeAlias` etc.) — see audit F6. Same rejection message shape as
/// `ops/scripts/soak_probe.sh` uses for the same trap.
fn khipu_python() -> Result<PathBuf, String> {
    if let Some(v) = env_first(&["KHIPU_PYTHON", "ALZY_PYTHON"]) {
        let p = PathBuf::from(&v);
        if p.is_file() {
            return Ok(p);
        }
        eprintln!(
            "[khipu] KHIPU_PYTHON is set to {v} but that path is not a file; falling back to PATH"
        );
    }
    let mut which_rejected: Option<String> = None;
    if let Some(p) = python_from_which() {
        match python_version(&p) {
            Some((major, minor)) if major > 3 || (major == 3 && minor >= 11) => {
                return Ok(p);
            }
            Some((major, minor)) => {
                which_rejected = Some(format!(
                    "python {major}.{minor} at {} is below 3.11; set KHIPU_PYTHON to Homebrew 3.11+",
                    p.display()
                ));
            }
            None => {
                which_rejected = Some(format!(
                    "could not determine python version at {}; set KHIPU_PYTHON to Homebrew 3.11+",
                    p.display()
                ));
            }
        }
        if let Some(msg) = &which_rejected {
            eprintln!("[khipu] {msg}");
        }
    }
    #[cfg(debug_assertions)]
    {
        let brew = PathBuf::from("/opt/homebrew/bin/python3.11");
        if brew.is_file() {
            return Ok(brew);
        }
    }
    if let Some(msg) = which_rejected {
        return Err(msg);
    }
    Err(
        "KHIPU_PYTHON is not set and python3 was not found on PATH. \
Set KHIPU_PYTHON (or reinstall via release_macos.sh --install so Info.plist LSEnvironment includes it)."
            .into(),
    )
}

/// True when stdout is a JSON object/array — used so semantic CLI exits
/// (e.g. doctor/revisions exit 2) still surface payload to the UI.
fn stdout_looks_like_json(stdout: &str) -> bool {
    let t = stdout.trim();
    if t.is_empty() {
        return false;
    }
    let looks_object = t.starts_with('{') && t.ends_with('}');
    let looks_array = t.starts_with('[') && t.ends_with(']');
    if !(looks_object || looks_array) {
        return false;
    }
    serde_json::from_str::<Value>(t).is_ok()
}

fn run_khipu_cli(args: &[String]) -> Result<String, String> {
    let root = khipu_root()?;
    let py = khipu_python()?;
    let pythonpath = format!(
        "{}:{}",
        root.join("packages/cli").display(),
        root.join(".python_libs").display()
    );
    let output = Command::new(&py)
        .arg("-m")
        .arg("khipu")
        .args(args)
        .env("PYTHONPATH", &pythonpath)
        .env("KHIPU_ROOT", &root)
        .output()
        .map_err(|e| format!("spawn khipu CLI failed ({py:?}): {e}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).to_string();
    if !stderr.trim().is_empty() {
        eprintln!("[khipu-cli] {}", stderr.trim());
    }
    if !output.status.success() {
        // Semantic unhealthy exits still emit JSON; do not treat as invoke Err.
        if stdout_looks_like_json(&stdout) {
            return Ok(stdout);
        }
        return Err(format!(
            "khipu exited {}: {}\n{}",
            output.status.code().unwrap_or(-1),
            stderr.trim(),
            stdout.trim()
        ));
    }
    Ok(stdout)
}

/// Read-only presence report (`khipu secrets`, no arguments). Fixed argv, so
/// the webview cannot reach `secrets --set` through it; writes go through
/// `set_khipu_secret` and its stdin transport.
#[tauri::command]
fn secrets_presence() -> Result<String, String> {
    run_khipu_cli(&["secrets".to_string()])
}

/// Apply (or plan) the schema. `migrate` is a state-changing subcommand and is
/// deliberately NOT in `ALLOWED_SUBCOMMANDS`; this command fixes the argv to
/// exactly `migrate` / `migrate --dry-run` so the UI can offer setup without
/// forwarding arbitrary arguments.
#[tauri::command]
fn khipu_migrate(dry_run: bool) -> Result<String, String> {
    let mut args = vec!["migrate".to_string()];
    if dry_run {
        args.push("--dry-run".to_string());
    }
    run_khipu_cli(&args)
}

/// Secrets the UI may write, and the Keychain accounts they map to.
///
/// `secrets` is deliberately NOT in `ALLOWED_SUBCOMMANDS`: that path forwards
/// arbitrary argv from the webview, and a secret must never travel as an
/// argument. This command is the only way in, and it pipes the value to the
/// CLI's stdin.
const SETTABLE_SECRETS: &[&str] = &["gemini_api_key", "database_url"];

#[tauri::command]
fn set_khipu_secret(account: String, value: String) -> Result<String, String> {
    if !SETTABLE_SECRETS.contains(&account.as_str()) {
        eprintln!("[khipu] refused secret write from the UI: {account:?}");
        return Err(format!("not a settable secret: {account:?}"));
    }
    if value.trim().is_empty() {
        return Err("value is empty".to_string());
    }

    let root = khipu_root()?;
    let py = khipu_python()?;
    let pythonpath = format!(
        "{}:{}",
        root.join("packages/cli").display(),
        root.join(".python_libs").display()
    );
    let mut child = Command::new(&py)
        .arg("-m")
        .arg("khipu")
        .arg("secrets")
        .arg("--set")
        .arg(&account)
        .env("PYTHONPATH", &pythonpath)
        .env("KHIPU_ROOT", &root)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("spawn khipu CLI failed ({py:?}): {e}"))?;

    child
        .stdin
        .take()
        .ok_or_else(|| "no stdin on khipu CLI".to_string())?
        .write_all(value.trim().as_bytes())
        .map_err(|e| format!("writing secret to khipu CLI failed: {e}"))?;

    let output = child
        .wait_with_output()
        .map_err(|e| format!("khipu CLI did not exit cleanly: {e}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    if !output.status.success() {
        // The CLI reports its own failures as JSON on stdout; stderr may carry
        // the secret-free traceback. Never echo the value back either way.
        if stdout_looks_like_json(&stdout) {
            return Ok(stdout);
        }
        return Err(format!(
            "khipu exited {}",
            output.status.code().unwrap_or(-1)
        ));
    }
    Ok(stdout)
}

/// Subcommands the UI is allowed to invoke. `run_khipu` is the entire privilege
/// boundary between the webview and this machine, and it originally accepted any
/// argv at all — including `capture` (audit 2026-08-17).
///
/// This list is exactly what the front end invokes, enumerated from every
/// `runKhipu([...])` call site in `App.tsx` and `IntegrationsPanel.tsx` rather
/// than assumed. The first pass kept five more — `sessions`, `git-sync`,
/// `outbox`, `embed`, `graph-sync` — that no pane has ever called, so the
/// boundary handed a compromised webview paid Gemini calls (`embed backfill`,
/// which also deletes vectors), model extraction (`sessions drain`), and a
/// `git-sync` that pushes commits and merges PRs. Removed 2026-08-18.
///
/// `integrations` stays and keeps its install/uninstall: the Integrations pane
/// offers both as deliberate user actions, and uninstall is the documented
/// rollback. `paths` stays for the same reason — the Settings pane's "Set
/// folder" button is `paths --set`.
const ALLOWED_SUBCOMMANDS: &[&str] = &[
    "status", "doctor", "activity", "search", "graph", "revisions", "paths",
    "backup-local", "import-local", "integrations",
];

#[tauri::command]
fn run_khipu(args: Vec<String>) -> Result<String, String> {
    let sub = args.first().map(String::as_str).unwrap_or("");
    if !ALLOWED_SUBCOMMANDS.contains(&sub) {
        eprintln!("[khipu] refused CLI subcommand from the UI: {sub:?}");
        return Err(format!("subcommand not permitted from the app: {sub:?}"));
    }
    run_khipu_cli(&args)
}

fn dsn_configured_uncached() -> bool {
    if env_first(&["KHIPU_DATABASE_URL", "ALZY_DATABASE_URL"]).is_some() {
        return true;
    }
    if keychain_has_dsn("Khipu") || keychain_has_dsn("Alzy") {
        return true;
    }
    dirs_fallback_dsn().is_file()
}

#[tauri::command]
fn dsn_configured(force: bool) -> bool {
    if !force {
        if let Ok(guard) = DSN_CACHE.lock() {
            if let Some(c) = guard.as_ref() {
                if c.checked_at.elapsed() < DSN_CACHE_TTL {
                    return c.value;
                }
            }
        }
    }
    let value = dsn_configured_uncached();
    if let Ok(mut guard) = DSN_CACHE.lock() {
        *guard = Some(DsnCache {
            value,
            checked_at: Instant::now(),
        });
    }
    value
}

fn keychain_has_dsn(service: &str) -> bool {
    Command::new("security")
        .args([
            "find-generic-password",
            "-s",
            service,
            "-a",
            "database_url",
            "-w",
        ])
        .output()
        .map(|o| o.status.success() && !o.stdout.is_empty())
        .unwrap_or(false)
}

/// The data folder is relocatable from Settings, and the pointer that records
/// where it went always stays at the default location. Reading it here keeps
/// this check in step with `khipu.paths.data_dir()`; without it, a user who
/// moved the folder and put their `dsn` inside it was sent to the first-run
/// screen forever while the CLI connected perfectly well.
fn data_dir_from_pointer_json(raw: &str) -> Option<PathBuf> {
    let parsed: Value = serde_json::from_str(raw).ok()?;
    let path = parsed.get("path")?.as_str()?.trim();
    if path.is_empty() {
        // An empty or absent "path" means "no override", never "the current
        // directory" — the Python side had exactly that bug and resolved the
        // whole data dir to CWD (audit 2026-08-17).
        return None;
    }
    Some(PathBuf::from(path))
}

fn data_dir_from_pointer() -> Option<PathBuf> {
    if let Some(p) = env_first(&["KHIPU_DATA_DIR", "ALZY_DATA_DIR"]) {
        return Some(PathBuf::from(p));
    }
    let home = std::env::var("HOME").ok()?;
    let ptr = PathBuf::from(&home).join(".config/khipu/data_root.json");
    data_dir_from_pointer_json(&std::fs::read_to_string(ptr).ok()?)
}

fn dirs_fallback_dsn() -> PathBuf {
    if let Some(p) = env_first(&["KHIPU_DSN_FILE", "ALZY_DSN_FILE"]) {
        return PathBuf::from(p);
    }
    if let Some(dir) = data_dir_from_pointer() {
        let relocated = dir.join("dsn");
        if relocated.is_file() {
            return relocated;
        }
    }
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    let khipu = PathBuf::from(&home).join(".config/khipu/dsn");
    if khipu.is_file() {
        return khipu;
    }
    PathBuf::from(home).join(".config/alzy/dsn")
}

#[tauri::command]
fn health_snapshot() -> Result<Value, String> {
    let raw = run_khipu_cli(&["status".into()])?;
    serde_json::from_str(&raw).map_err(|e| format!("status JSON: {e}"))
}

fn tray_tooltip_from_doctor(raw: &str) -> String {
    match serde_json::from_str::<Value>(raw) {
        Ok(v) => {
            let ok = v.get("ok").and_then(|x| x.as_bool()).unwrap_or(false);
            let backup = v.get("backup_ok").and_then(|x| x.as_bool()).unwrap_or(false);
            let drift = v.get("drift_ok").and_then(|x| x.as_bool()).unwrap_or(false);
            // Missing key (older CLI) counts as green; an explicit false is red.
            let recording = v
                .get("capture_liveness_ok")
                .and_then(|x| x.as_bool())
                .unwrap_or(true);
            let git_sync = v.get("git_sync_ok").and_then(|x| x.as_bool()).unwrap_or(true);
            let eps = v
                .pointer("/status/counts/episodes")
                .and_then(|x| x.as_u64())
                .unwrap_or(0);
            if ok {
                format!("Khipu · OK · {eps} ep · recording/sync/backup/drift green")
            } else if !recording {
                let red: Vec<String> = v
                    .pointer("/capture_liveness/red")
                    .and_then(|x| x.as_array())
                    .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
                    .unwrap_or_default();
                format!("Khipu · NOT RECORDING · {} · {eps} ep", red.join(","))
            } else if !git_sync {
                format!("Khipu · WARN · git sync not landing · {eps} ep")
            } else if !backup {
                format!("Khipu · WARN · backup · {eps} ep")
            } else if !drift {
                format!("Khipu · WARN · drift · {eps} ep")
            } else {
                format!("Khipu · WARN · {eps} ep")
            }
        }
        Err(_) => "Khipu · doctor unavailable".into(),
    }
}

fn tray_tooltip_from_status(raw: &str) -> String {
    match serde_json::from_str::<Value>(raw) {
        Ok(v) => {
            let ok = v.get("dsn_ok").and_then(|x| x.as_bool()).unwrap_or(false);
            let eps = v
                .pointer("/counts/episodes")
                .and_then(|x| x.as_u64())
                .unwrap_or(0);
            if ok {
                format!("Khipu · OK · {eps} episodes")
            } else {
                "Khipu · DSN missing".into()
            }
        }
        Err(_) => "Khipu · status unavailable".into(),
    }
}

/// Launch path: check only. Never download/install/restart here — Settings owns install.
async fn check_for_update_at_launch(app: AppHandle) {
    let updater = match app.updater() {
        Ok(u) => u,
        Err(e) => {
            eprintln!("[khipu-updater] updater unavailable: {e}");
            return;
        }
    };
    match updater.check().await {
        Ok(Some(update)) => {
            eprintln!(
                "[khipu-updater] update available: v{} — install via Settings → Check for updates",
                update.version
            );
            apply_tray_tooltip(
                &app,
                TrayTooltipSource::UpdateAvailable,
                Some(format!("Khipu · update v{} available", update.version)),
            );
        }
        Ok(None) => {
            eprintln!("[khipu-updater] up to date");
            // Clear update tip so health (if any) is visible again.
            apply_tray_tooltip(&app, TrayTooltipSource::UpdateAvailable, None);
        }
        Err(e) => {
            eprintln!("[khipu-updater] check failed: {e}");
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            run_khipu,
            set_khipu_secret,
            secrets_presence,
            khipu_migrate,
            dsn_configured,
            health_snapshot
        ])
        .setup(|app| {
            let show_i = MenuItem::with_id(app, "show", "Show Khipu", true, None::<&str>)?;
            let doctor_i =
                MenuItem::with_id(app, "doctor", "Run doctor…", true, None::<&str>)?;
            let quit_i = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show_i, &doctor_i, &quit_i])?;

            // Color B9 squircle — same art as Dock. Not a template: template mode
            // would flatten it to a solid white square on macOS.
            let tray_icon = Image::from_bytes(include_bytes!("../icons/trayIcon@2x.png"))
                .unwrap_or_else(|_| {
                    const TRAY_RGBA: &[u8] = include_bytes!("../icons/trayIcon@2x.rgba");
                    Image::new(TRAY_RGBA, 44, 44)
                });

            // Placeholder first — doctor/status can take seconds; refresh after tray is up.
            let tray = TrayIconBuilder::new()
                .icon(tray_icon)
                .icon_as_template(false)
                .menu(&menu)
                .tooltip("Khipu · starting…")
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "quit" => app.exit(0),
                    "show" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                    }
                    "doctor" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                        // Off the menu thread: CLI doctor can take seconds.
                        let app2 = app.clone();
                        tauri::async_runtime::spawn(async move {
                            let tooltip = match tauri::async_runtime::spawn_blocking(|| {
                                match run_khipu_cli(&["doctor".into()]) {
                                    Ok(raw) => tray_tooltip_from_doctor(&raw),
                                    Err(e) => {
                                        eprintln!("[khipu-tray] doctor failed: {e}");
                                        match run_khipu_cli(&["status".into()]) {
                                            Ok(raw) => tray_tooltip_from_status(&raw),
                                            Err(e2) => {
                                                eprintln!("[khipu-tray] status failed: {e2}");
                                                "Khipu · offline / CLI error".into()
                                            }
                                        }
                                    }
                                }
                            })
                            .await
                            {
                                Ok(t) => t,
                                Err(e) => {
                                    eprintln!("[khipu-tray] doctor task join failed: {e}");
                                    "Khipu · doctor unavailable".into()
                                }
                            };
                            apply_tray_tooltip(
                                &app2,
                                TrayTooltipSource::Health,
                                Some(tooltip),
                            );
                        });
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                    }
                })
                .build(app)?;
            // Keep the tray handle alive for the app lifetime.
            app.manage(tray);

            let health_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                let tooltip = match tauri::async_runtime::spawn_blocking(|| {
                    match run_khipu_cli(&["doctor".into()]) {
                        Ok(raw) => tray_tooltip_from_doctor(&raw),
                        Err(_) => match run_khipu_cli(&["status".into()]) {
                            Ok(raw) => tray_tooltip_from_status(&raw),
                            Err(_) => "Khipu · offline / CLI error".into(),
                        },
                    }
                })
                .await
                {
                    Ok(t) => t,
                    Err(e) => {
                        eprintln!("[khipu-tray] initial health task join failed: {e}");
                        "Khipu · doctor unavailable".into()
                    }
                };
                apply_tray_tooltip(
                    &health_handle,
                    TrayTooltipSource::Health,
                    Some(tooltip),
                );
            });

            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                check_for_update_at_launch(handle).await;
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Khipu");
}

#[cfg(test)]
mod tray_tooltip_tests {
    use super::tray_tooltip_from_doctor;

    const BASE: &str = r#"{"status":{"counts":{"episodes":4350}},"drift_ok":true,"backup_ok":true"#;

    #[test]
    fn green_when_ok() {
        let raw = format!("{BASE},\"ok\":true,\"capture_liveness_ok\":true,\"git_sync_ok\":true}}");
        assert_eq!(tray_tooltip_from_doctor(&raw), "Khipu · OK · 4350 ep · recording/sync/backup/drift green");
    }

    #[test]
    fn git_sync_red_names_it() {
        let raw = format!("{BASE},\"ok\":false,\"capture_liveness_ok\":true,\"git_sync_ok\":false}}");
        assert_eq!(tray_tooltip_from_doctor(&raw), "Khipu · WARN · git sync not landing · 4350 ep");
    }

    #[test]
    fn not_recording_outranks_git_sync() {
        let raw = format!(
            "{BASE},\"ok\":false,\"capture_liveness_ok\":false,\"git_sync_ok\":false,\"capture_liveness\":{{\"red\":[\"cursor\"]}}}}"
        );
        assert_eq!(tray_tooltip_from_doctor(&raw), "Khipu · NOT RECORDING · cursor · 4350 ep");
    }

    #[test]
    fn older_cli_without_key_is_green() {
        let raw = format!("{BASE},\"ok\":true,\"capture_liveness_ok\":true}}");
        assert!(tray_tooltip_from_doctor(&raw).starts_with("Khipu · OK"));
    }
}

#[cfg(test)]
mod settable_secrets_tests {
    use super::{ALLOWED_SUBCOMMANDS, SETTABLE_SECRETS};

    #[test]
    fn secrets_is_not_reachable_through_the_generic_argv_path() {
        // `run_khipu` forwards arbitrary argv from the webview. If `secrets`
        // were allowed there, the UI could pass a key as an argument and put it
        // in `ps` output — the exact exposure set_khipu_secret exists to avoid.
        assert!(!ALLOWED_SUBCOMMANDS.contains(&"secrets"));
    }

    #[test]
    fn migrate_is_not_reachable_through_the_generic_argv_path() {
        // It writes the schema. The UI reaches it only via khipu_migrate,
        // whose argv is fixed.
        assert!(!ALLOWED_SUBCOMMANDS.contains(&"migrate"));
    }

    #[test]
    fn only_the_two_known_accounts_are_writable() {
        assert_eq!(SETTABLE_SECRETS, &["gemini_api_key", "database_url"]);
    }

    #[test]
    fn arbitrary_keychain_accounts_are_refused() {
        for probe in ["aws_secret_key", "", "gemini_api_key ", "GEMINI_API_KEY"] {
            assert!(
                !SETTABLE_SECRETS.contains(&probe),
                "unexpectedly writable: {probe:?}"
            );
        }
    }

    #[test]
    fn the_rust_and_python_allowlists_agree() {
        // khipu/cli.py SETTABLE_SECRETS is the enforcing copy; this one is the
        // first gate. A drift between them would let the UI offer a secret the
        // CLI then refuses.
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../packages/cli/khipu/cli.py");
        let cli = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
        let block = cli
            .split("SETTABLE_SECRETS = {")
            .nth(1)
            .expect("SETTABLE_SECRETS literal");
        for account in SETTABLE_SECRETS {
            assert!(
                block.contains(&format!("\"{account}\"")),
                "{account} is writable from Rust but absent from cli.py"
            );
        }
    }
}

#[cfg(test)]
mod run_khipu_guard_tests {
    use super::ALLOWED_SUBCOMMANDS;

    #[test]
    fn every_subcommand_the_ui_calls_is_allowed() {
        for s in ["status", "doctor", "activity", "search", "graph", "revisions",
                  "paths", "backup-local", "import-local", "integrations"] {
            assert!(ALLOWED_SUBCOMMANDS.contains(&s), "UI calls `{s}` but it is not allowed");
        }
    }

    #[test]
    fn state_changing_subcommands_are_not_reachable_from_the_ui() {
        for s in ["capture", "regen-memory", "gateway", "mcp"] {
            assert!(!ALLOWED_SUBCOMMANDS.contains(&s), "`{s}` must not be callable from the webview");
        }
    }

    #[test]
    fn subcommands_no_pane_calls_are_not_reachable_either() {
        // Allowlisted until 2026-08-18 despite no `runKhipu([...])` call site in
        // any .tsx. Each one hands a compromised webview something real: paid
        // Gemini calls and vector deletes, model extraction, PG writes, and a
        // git push that opens and merges PRs.
        for s in ["sessions", "git-sync", "outbox", "embed", "graph-sync"] {
            assert!(!ALLOWED_SUBCOMMANDS.contains(&s),
                    "`{s}` is not called by any pane and must not be reachable");
        }
    }

    #[test]
    fn the_allowlist_is_exactly_what_the_front_end_invokes() {
        // Grown by hand, so pin the size: adding an entry without a call site is
        // how the five above got in.
        assert_eq!(ALLOWED_SUBCOMMANDS.len(), 10);
    }

    #[test]
    fn an_empty_or_unknown_subcommand_is_not_allowed() {
        assert!(!ALLOWED_SUBCOMMANDS.contains(&""));
        assert!(!ALLOWED_SUBCOMMANDS.contains(&"--help"));
    }
}

#[cfg(test)]
mod data_dir_pointer_tests {
    use super::data_dir_from_pointer_json;

    #[test]
    fn a_relocated_folder_is_read_from_the_pointer() {
        let out = data_dir_from_pointer_json(r#"{"path": "/Volumes/Elsewhere/khipu"}"#);
        assert_eq!(out.unwrap().to_str().unwrap(), "/Volumes/Elsewhere/khipu");
    }

    #[test]
    fn surrounding_whitespace_is_trimmed() {
        let out = data_dir_from_pointer_json(r#"{"path": "  /tmp/kh  "}"#);
        assert_eq!(out.unwrap().to_str().unwrap(), "/tmp/kh");
    }

    #[test]
    fn a_pointer_without_a_path_is_no_override_not_the_current_directory() {
        assert!(data_dir_from_pointer_json(r#"{"updated_at": "2026-08-18"}"#).is_none());
        assert!(data_dir_from_pointer_json(r#"{"path": ""}"#).is_none());
        assert!(data_dir_from_pointer_json(r#"{"path": "   "}"#).is_none());
    }

    #[test]
    fn a_pointer_that_is_not_json_is_ignored_rather_than_fatal() {
        assert!(data_dir_from_pointer_json("not json at all").is_none());
        assert!(data_dir_from_pointer_json("").is_none());
        assert!(data_dir_from_pointer_json(r#"{"path": 42}"#).is_none());
    }
}
