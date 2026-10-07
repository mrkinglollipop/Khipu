use std::io::{BufRead, Write};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

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
/// they were built from; release builds fall back to `Contents/Resources/khipu`
/// when the CLI was bundled by `bundle_cli.sh`.
#[cfg(not(debug_assertions))]
fn bundled_khipu_root() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let resources_khipu = exe.parent()?.join("../Resources/khipu");
    let root = resources_khipu.canonicalize().ok()?;
    if root.join("packages/cli").is_dir() {
        Some(root)
    } else {
        None
    }
}

/// Startup self-heal: if a launcher ever wrote `__pycache__` inside the
/// signed bundle before this launch got a chance to redirect it
/// (`khipu_bytecode_env`), sweep it clean so THIS run's Gatekeeper check is
/// not tripped by a PAST run's mistake — the actual 0.3.15 failure mode.
/// This only helps FUTURE launches (the damage already happened by the time
/// Gatekeeper complains); it does not prevent a write during this session,
/// just keeps an already-installed bundle clean if anything still manages
/// one. Errors are ignored throughout — a failed cleanup must never block
/// startup.
#[cfg(not(debug_assertions))]
fn clean_bundled_pycache() {
    let Some(root) = bundled_khipu_root() else {
        return;
    };
    let mut removed = 0usize;
    let mut stack = vec![root];
    while let Some(dir) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let Ok(file_type) = entry.file_type() else {
                continue;
            };
            if !file_type.is_dir() {
                continue;
            }
            let path = entry.path();
            if path.file_name().and_then(|n| n.to_str()) == Some("__pycache__") {
                if std::fs::remove_dir_all(&path).is_ok() {
                    removed += 1;
                }
            } else {
                stack.push(path);
            }
        }
    }
    if removed > 0 {
        eprintln!(
            "[khipu] startup self-heal: removed {removed} __pycache__ dir(s) from the bundled CLI"
        );
    }
}

/// Debug builds run from the checkout, never a signed bundle — nothing to heal.
#[cfg(debug_assertions)]
fn clean_bundled_pycache() {}

fn khipu_pythonpath(root: &PathBuf) -> String {
    #[cfg(debug_assertions)]
    {
        format!(
            "{}:{}",
            root.join("packages/cli").display(),
            root.join(".python_libs").display()
        )
    }
    #[cfg(not(debug_assertions))]
    {
        format!(
            "{}:{}",
            root.join("packages/cli").display(),
            root.join("lib").display()
        )
    }
}

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
        if let Some(root) = bundled_khipu_root() {
            return Ok(root);
        }
        Err(
            "KHIPU_ROOT is not set and the bundled CLI is missing from this .app \
(Contents/Resources/khipu). Re-download the Khipu DMG or set KHIPU_ROOT."
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

/// Prefer `KHIPU_PYTHON` / `ALZY_PYTHON`, then bundled `Resources/khipu/python`,
/// then `which python3` on PATH. Debug builds may fall back to Homebrew python3.11.
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
            "[khipu] KHIPU_PYTHON is set to {v} but that path is not a file; falling back"
        );
    }
    #[cfg(not(debug_assertions))]
    {
        if let Some(root) = bundled_khipu_root() {
            let bundled = root.join("python/bin/python3.11");
            if bundled.is_file() {
                return Ok(bundled);
            }
        }
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
        "KHIPU_PYTHON is not set, bundled python is missing, and python3 was not found on PATH. \
Set KHIPU_PYTHON or reinstall from a DMG that includes the bundled CLI."
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

/// Bytecode cache location for every khipu CLI invocation, kept OUTSIDE the
/// signed .app bundle. An in-app update to a previous release wrote
/// `__pycache__/*.pyc` inside `Contents/Resources/khipu` on first run, which
/// broke the code signature and made Gatekeeper report the app as "damaged"
/// (0.3.15, withdrawn — confirmed with `codesign -vvv --deep --strict`).
/// PYTHONPYCACHEPREFIX redirects CPython's bytecode cache without disabling
/// it; if the directory can't be created (unwritable HOME, sandboxing) fall
/// back to PYTHONDONTWRITEBYTECODE=1 instead of silently writing back into
/// the bundle. Mirrors `khipu.paths.pycache_dir()` on the Python side.
fn khipu_bytecode_env() -> (&'static str, String) {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    let cache_dir = PathBuf::from(home).join("Library/Caches/Khipu/pycache");
    if std::fs::create_dir_all(&cache_dir).is_ok() {
        ("PYTHONPYCACHEPREFIX", cache_dir.display().to_string())
    } else {
        ("PYTHONDONTWRITEBYTECODE", "1".to_string())
    }
}

/// The desktop app's own version, handed to every CLI invocation as
/// `KHIPU_APP_VERSION`. `khipu.components_matrix.khipu_app_version()` reads
/// this first and otherwise falls back to a hard-coded release string, so a
/// bundled CLI launched by a newer app reported a stale version in the
/// Components pane (audit 2026-09-04). `CARGO_PKG_VERSION` is the same value
/// `tauri.conf.json` publishes, so the two can never drift.
fn khipu_app_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

fn run_khipu_cli(args: &[String]) -> Result<String, String> {
    run_khipu_cli_with_env(args, &[])
}

/// Same as `run_khipu_cli`, plus caller-supplied environment variables on the
/// spawned process only (never argv). This is how a DSN — which can carry a
/// password — reaches `khipu db preflight|connect|move`: the same "never in
/// argv, so it never shows in `ps`" rule `set_khipu_secret` follows via a
/// stdin pipe and `join_export`'s passphrase follows via env, applied here
/// via env like `join_export` (a one-shot `.output()` call has no long-lived
/// stdin pipe worth opening).
fn run_khipu_cli_with_env(args: &[String], extra_env: &[(String, String)]) -> Result<String, String> {
    let root = khipu_root()?;
    let py = khipu_python()?;
    let pythonpath = khipu_pythonpath(&root);
    let (bc_key, bc_val) = khipu_bytecode_env();
    let mut cmd = Command::new(&py);
    cmd.arg("-m")
        .arg("khipu")
        .args(args)
        .env("PYTHONPATH", &pythonpath)
        .env("KHIPU_ROOT", &root)
        .env("KHIPU_APP_VERSION", khipu_app_version())
        .env(bc_key, &bc_val);
    for (k, v) in extra_env {
        cmd.env(k, v);
    }
    let output = cmd
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

/// Sync `#[tauri::command]` fns run on the webview thread. Any `.output()`
/// there freezes tab paints and the working spinner. Offload CLI waits.
async fn run_khipu_cli_async(args: Vec<String>) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || run_khipu_cli(&args))
        .await
        .map_err(|e| format!("khipu worker join failed: {e}"))?
}

/// `run_khipu_cli_async` plus per-call env vars — see `run_khipu_cli_with_env`.
async fn run_khipu_cli_with_env_async(
    args: Vec<String>,
    extra_env: Vec<(String, String)>,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || run_khipu_cli_with_env(&args, &extra_env))
        .await
        .map_err(|e| format!("khipu worker join failed: {e}"))?
}

async fn spawn_blocking_cli<T, F>(f: F) -> Result<T, String>
where
    T: Send + 'static,
    F: FnOnce() -> Result<T, String> + Send + 'static,
{
    tauri::async_runtime::spawn_blocking(f)
        .await
        .map_err(|e| format!("khipu worker join failed: {e}"))?
}

/// Read-only presence report (`khipu secrets`, no arguments). Fixed argv, so
/// the webview cannot reach `secrets --set` through it; writes go through
/// `set_khipu_secret` and its stdin transport.
#[tauri::command]
async fn secrets_presence() -> Result<String, String> {
    run_khipu_cli_async(vec!["secrets".to_string()]).await
}

/// Prove a saved model key actually works (`khipu secrets verify --json`, no
/// arguments — `khipu.modelcheck.check_model_keys` decides which configured
/// providers to call). Fixed argv, the same shape as `secrets_presence`: this
/// makes exactly one real, cheap call per configured provider and never
/// returns the key itself, but `secrets` still stays OUT of
/// `ALLOWED_SUBCOMMANDS` on principle — no subcommand under it should ever
/// forward arbitrary argv from the webview.
/// (docs/plans/2026-09-05-setup-that-cannot-strand-you.md, "A model key is
/// proven on save with one real call".)
#[tauri::command]
async fn khipu_secrets_verify() -> Result<String, String> {
    run_khipu_cli_async(vec!["secrets".to_string(), "verify".to_string(), "--json".to_string()])
        .await
}

/// Index everything missing or changed (`khipu embed backfill`, no arguments).
///
/// `embed` stays OUT of `ALLOWED_SUBCOMMANDS` on purpose: that path forwards
/// arbitrary argv, and `embed` reaches paid Gemini calls, a `--profile` switch
/// and vector deletes. Settings → Search index needs exactly one of those
/// things, so it gets exactly one fixed argv and no arguments of its own —
/// the same shape as `khipu_migrate` and `secrets_presence`. The button is
/// behind a confirm dialog in the UI because it spends money.
const EMBED_BACKFILL_ARGV: &[&str] = &["embed", "backfill"];

#[tauri::command]
async fn khipu_embed_backfill() -> Result<String, String> {
    run_khipu_cli_async(EMBED_BACKFILL_ARGV.iter().map(|s| s.to_string()).collect()).await
}

/// Re-render + reload whichever scheduled LaunchAgents are already installed
/// (`khipu jobs refresh`, no arguments). `jobs` stays OUT of
/// `ALLOWED_SUBCOMMANDS` on purpose: `jobs install`/`jobs uninstall` change
/// launchd state (load/unload agents, write versions.json), and the app only
/// ever needs the read-and-rewrite-if-stale `refresh` verb — never install or
/// uninstall — so it gets exactly one fixed argv, the same shape as
/// `khipu_embed_backfill` and `khipu_migrate`.
const JOBS_REFRESH_ARGV: &[&str] = &["jobs", "refresh"];

#[tauri::command]
async fn khipu_jobs_refresh() -> Result<String, String> {
    run_khipu_cli_async(JOBS_REFRESH_ARGV.iter().map(|s| s.to_string()).collect()).await
}

/// D3: Home's "Right now" card — pending turns per live harness, queue
/// depth, captured-today — without waiting on the slower full `doctor` pass
/// (DB round trips, backup checks, etc.) that also carries this same data
/// under `capture_liveness`. `khipu sessions liveness` always prints JSON
/// (`session_capture.liveness_all()`, the same call doctor makes); fixed
/// argv, no arguments, exits non-zero when a harness is red but still
/// prints JSON (`run_khipu_cli` already treats that as Ok, not an invoke
/// error).
const LIVENESS_NOW_ARGV: &[&str] = &["sessions", "liveness"];

#[tauri::command]
async fn liveness_now() -> Result<String, String> {
    run_khipu_cli_async(LIVENESS_NOW_ARGV.iter().map(|s| s.to_string()).collect()).await
}

/// D3: Home's "Capture now" button — `khipu capture now`, no arguments, so
/// it flags the most recently active local session (K1's
/// `resolve_session_ref()`) for capture at its next Stop/PreCompact/
/// SessionEnd instead of waiting on the fixed cadence. Fixed argv: `capture`
/// stays out of `ALLOWED_SUBCOMMANDS` like every other state-changing verb
/// here, and the button never lets the webview pick a harness/session id of
/// its own.
const CAPTURE_NOW_ARGV: &[&str] = &["capture", "now"];

#[tauri::command]
async fn khipu_capture_now() -> Result<String, String> {
    run_khipu_cli_async(CAPTURE_NOW_ARGV.iter().map(|s| s.to_string()).collect()).await
}

// --- Settings parity (docs/plans/2026-09-30-settings-parity.md). Every setting
// the `khipu` CLI persists gets a fixed-argv command here. The webview never
// supplies argv: a name or key is checked against a constant list, a value is
// validated per key, and a value that reaches argv travels either as
// `--flag=value` or as the second operand of `config --set KEY VALUE`, where
// validation has already refused anything starting with `-` (argparse would
// read it as a flag). The gateway token travels on stdin. None of these verbs
// join `ALLOWED_SUBCOMMANDS`.

/// The nine switches in `khipu.features.FEATURES`.
const FEATURE_NAMES: &[&str] = &[
    "validity_ranking",
    "time_interpretation",
    "relevance_floor",
    "rerank",
    "reflect",
    "briefs",
    "graph_candidates",
    "decision_details",
    "auto_supersede",
];

/// The scheduled jobs `khipu jobs install|uninstall` accepts (`_JOBS_CHOICES`).
const JOB_NAMES: &[&str] = &[
    "nightly",
    "monthly",
    "graph_build",
    "notes_watch",
    "queue_drain",
    "recall_daemon",
];

const CAPTURE_MODES: &[&str] = &["legacy", "dual", "hub"];
/// `config --set KEY N` with N in [0, 1] (`FLOAT_SETTINGS`).
const UNIT_FLOAT_KEYS: &[&str] = &["dedup_similarity", "commitment_close_similarity"];
/// `config --set KEY PATH` / `config --unset KEY` (`PATH_SETTINGS`).
const PATH_KEYS: &[&str] = &[
    "memory_root",
    "memory_repo",
    "capture_v2",
    "graph_sqlite",
    "gemini_key_file",
];
const COSINE_FLOOR_KEY: &str = "relevance.cosine_floor";

const FEATURES_SHOW_ARGV: &[&str] = &["features"];
const CONFIG_SHOW_ARGV: &[&str] = &["config"];
const JOBS_STATUS_ARGV: &[&str] = &["jobs", "status"];
const RECALL_STATUS_ARGV: &[&str] = &["recall", "status"];
const GATEWAY_TOKEN_STATUS_ARGV: &[&str] = &["gateway", "token", "status"];
const GATEWAY_TOKEN_SET_ARGV: &[&str] = &["gateway", "token", "set"];

fn fixed_argv(argv: &[&str]) -> Vec<String> {
    argv.iter().map(|s| s.to_string()).collect()
}

fn has_control(s: &str) -> bool {
    s.chars().any(|c| c.is_control())
}

fn feature_set_argv(name: &str, enabled: bool) -> Result<Vec<String>, String> {
    if !FEATURE_NAMES.contains(&name) {
        return Err(format!("not a switch: {name:?}"));
    }
    Ok(vec![
        "features".to_string(),
        "--set".to_string(),
        name.to_string(),
        if enabled { "on" } else { "off" }.to_string(),
    ])
}

fn job_argv(verb: &str, name: &str) -> Result<Vec<String>, String> {
    if verb != "install" && verb != "uninstall" {
        return Err(format!("not a jobs verb: {verb:?}"));
    }
    if !JOB_NAMES.contains(&name) {
        return Err(format!("not a scheduled job: {name:?}"));
    }
    Ok(vec!["jobs".to_string(), verb.to_string(), name.to_string()])
}

/// A number the CLI would accept, re-printed from the parsed value so nothing
/// the caller typed (a sign, an exponent, whitespace) reaches argv.
fn parse_unit_number(key: &str, raw: &str, allow_zero: bool) -> Result<String, String> {
    let v: f64 = raw
        .trim()
        .parse()
        .map_err(|_| format!("{key} must be a number, got {raw:?}"))?;
    let ok = if allow_zero { (0.0..=1.0).contains(&v) } else { v > 0.0 && v <= 1.0 };
    if !ok {
        let low = if allow_zero { "0" } else { "above 0" };
        return Err(format!("{key} must be {low} and at most 1, got {raw:?}"));
    }
    Ok(format!("{v}"))
}

fn checked_operand(key: &str, raw: &str, allow_empty: bool) -> Result<String, String> {
    let v = raw.trim();
    if v.is_empty() {
        return if allow_empty {
            Ok(String::new())
        } else {
            Err(format!("{key} must not be empty"))
        };
    }
    if v.starts_with('-') {
        return Err(format!("{key} must not start with '-'"));
    }
    if has_control(v) {
        return Err(format!("{key} must not contain control characters"));
    }
    if v.len() > 4096 {
        return Err(format!("{key} is too long"));
    }
    Ok(v.to_string())
}

fn config_set_argv(key: &str, value: &str) -> Result<Vec<String>, String> {
    let set = |k: &str, v: String| {
        vec!["config".to_string(), "--set".to_string(), k.to_string(), v]
    };
    match key {
        "capture_mode" => {
            let v = value.trim();
            if !CAPTURE_MODES.contains(&v) {
                return Err(format!("capture_mode must be legacy, dual or hub, got {value:?}"));
            }
            Ok(vec!["config".to_string(), format!("--set-capture-mode={v}")])
        }
        "gateway_url" => {
            let v = value.trim();
            if !v.is_empty() {
                if !v.starts_with("https://") {
                    return Err("gateway_url must start with https://".to_string());
                }
                if v.len() > 2048 || v.chars().any(|c| c.is_control() || c.is_whitespace()) {
                    return Err("gateway_url must be one line with no spaces".to_string());
                }
            }
            Ok(vec!["config".to_string(), format!("--set-gateway-url={v}")])
        }
        k if UNIT_FLOAT_KEYS.contains(&k) => Ok(set(k, parse_unit_number(k, value, true)?)),
        COSINE_FLOOR_KEY => Ok(set(COSINE_FLOOR_KEY, parse_unit_number(key, value, false)?)),
        "user_aliases" => Ok(set("user_aliases", checked_operand(key, value, true)?)),
        k if PATH_KEYS.contains(&k) => Ok(set(k, checked_operand(key, value, false)?)),
        _ => Err(format!("not a settable key: {key:?}")),
    }
}

fn config_unset_argv(key: &str) -> Result<Vec<String>, String> {
    if !PATH_KEYS.contains(&key) && key != COSINE_FLOOR_KEY {
        return Err(format!("not a resettable key: {key:?}"));
    }
    Ok(vec!["config".to_string(), format!("--unset={key}")])
}

/// Embedding profile ids look like `gemini-embedding-2@768`.
fn checked_profile(profile: &str) -> Result<&str, String> {
    let p = profile.trim();
    let ok = !p.is_empty()
        && p.len() <= 64
        && !p.starts_with('-')
        && p.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '@' | '-'));
    if ok {
        Ok(p)
    } else {
        Err(format!("not an embedding profile id: {profile:?}"))
    }
}

fn embed_status_argv(profile: Option<&str>) -> Result<Vec<String>, String> {
    let mut args = vec!["embed".to_string(), "status".to_string()];
    if let Some(p) = profile {
        args.push(format!("--profile={}", checked_profile(p)?));
    }
    Ok(args)
}

/// Never `--force`: the CLI refuses a profile that is missing vectors.
fn embed_activate_argv(profile: &str) -> Result<Vec<String>, String> {
    Ok(vec![
        "embed".to_string(),
        "activate".to_string(),
        checked_profile(profile)?.to_string(),
    ])
}

fn gateway_token_input(value: &str) -> Result<String, String> {
    let v = value.trim();
    if v.is_empty() {
        return Err("token is empty".to_string());
    }
    if v.chars().any(|c| c.is_control() || c.is_whitespace()) {
        return Err("token must be a single line with no spaces".to_string());
    }
    Ok(v.to_string())
}

#[tauri::command]
async fn khipu_features_show() -> Result<String, String> {
    run_khipu_cli_async(fixed_argv(FEATURES_SHOW_ARGV)).await
}

#[tauri::command]
async fn khipu_feature_set(name: String, enabled: bool) -> Result<String, String> {
    run_khipu_cli_async(feature_set_argv(&name, enabled)?).await
}

#[tauri::command]
async fn khipu_config_show() -> Result<String, String> {
    run_khipu_cli_async(fixed_argv(CONFIG_SHOW_ARGV)).await
}

#[tauri::command]
async fn khipu_config_set(key: String, value: String) -> Result<String, String> {
    run_khipu_cli_async(config_set_argv(&key, &value)?).await
}

#[tauri::command]
async fn khipu_config_unset(key: String) -> Result<String, String> {
    run_khipu_cli_async(config_unset_argv(&key)?).await
}

#[tauri::command]
async fn khipu_jobs_status() -> Result<String, String> {
    run_khipu_cli_async(fixed_argv(JOBS_STATUS_ARGV)).await
}

#[tauri::command]
async fn khipu_job_install(name: String) -> Result<String, String> {
    run_khipu_cli_async(job_argv("install", &name)?).await
}

#[tauri::command]
async fn khipu_job_uninstall(name: String) -> Result<String, String> {
    run_khipu_cli_async(job_argv("uninstall", &name)?).await
}

#[tauri::command]
async fn khipu_recall_status() -> Result<String, String> {
    run_khipu_cli_async(fixed_argv(RECALL_STATUS_ARGV)).await
}

#[tauri::command]
async fn khipu_gateway_token_status() -> Result<String, String> {
    run_khipu_cli_async(fixed_argv(GATEWAY_TOKEN_STATUS_ARGV)).await
}

/// The token goes to the CLI's stdin, never argv, and is never logged or
/// returned (the CLI prints only path, mode and size).
#[tauri::command]
async fn khipu_gateway_token_set(value: String) -> Result<String, String> {
    let token = gateway_token_input(&value)?;
    spawn_blocking_cli(move || run_khipu_cli_with_stdin(&fixed_argv(GATEWAY_TOKEN_SET_ARGV), &token)).await
}

#[tauri::command]
async fn khipu_embed_status(profile: Option<String>) -> Result<String, String> {
    run_khipu_cli_async(embed_status_argv(profile.as_deref())?).await
}

#[tauri::command]
async fn khipu_embed_activate(profile: String) -> Result<String, String> {
    run_khipu_cli_async(embed_activate_argv(&profile)?).await
}

/// `khipu <args>` with `stdin_text` piped to stdin. The text is never echoed
/// into an error message.
fn run_khipu_cli_with_stdin(args: &[String], stdin_text: &str) -> Result<String, String> {
    let root = khipu_root()?;
    let py = khipu_python()?;
    let pythonpath = khipu_pythonpath(&root);
    let (bc_key, bc_val) = khipu_bytecode_env();
    let mut child = Command::new(&py)
        .arg("-m")
        .arg("khipu")
        .args(args)
        .env("PYTHONPATH", &pythonpath)
        .env("KHIPU_ROOT", &root)
        .env("KHIPU_APP_VERSION", khipu_app_version())
        .env(bc_key, &bc_val)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("spawn khipu CLI failed ({py:?}): {e}"))?;
    child
        .stdin
        .take()
        .ok_or_else(|| "no stdin on khipu CLI".to_string())?
        .write_all(stdin_text.as_bytes())
        .map_err(|e| format!("writing to khipu CLI failed: {e}"))?;
    let output = child
        .wait_with_output()
        .map_err(|e| format!("khipu CLI did not exit cleanly: {e}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    if !output.status.success() {
        if stdout_looks_like_json(&stdout) {
            return Ok(stdout);
        }
        return Err(format!("khipu exited {}", output.status.code().unwrap_or(-1)));
    }
    Ok(stdout)
}

/// Apply (or plan) the schema. `migrate` is a state-changing subcommand and is
/// deliberately NOT in `ALLOWED_SUBCOMMANDS`; this command fixes the argv to
/// exactly `migrate` / `migrate --dry-run` so the UI can offer setup without
/// forwarding arbitrary arguments.
#[tauri::command]
async fn khipu_migrate(dry_run: bool) -> Result<String, String> {
    let mut args = vec!["migrate".to_string()];
    if dry_run {
        args.push("--dry-run".to_string());
    }
    run_khipu_cli_async(args).await
}

// --- Setup pipeline (docs/plans/2026-09-05-setup-that-cannot-strand-you.md,
// Phase 2): the Database step (Welcome), the Move dialog (Settings), and the
// Another Mac gate all call these. `db` is a state-changing subcommand
// (writes the stored connection, can install LaunchAgents) so it stays OUT
// of `ALLOWED_SUBCOMMANDS`, the same shape as `migrate`/`embed`/`jobs`: each
// verb gets exactly one fixed-argv command here, and any DSN travels as a
// per-call env var, never argv — see `run_khipu_cli_with_env`.

fn db_preflight_argv() -> Vec<String> {
    vec!["db".to_string(), "preflight".to_string()]
}

fn db_connect_argv(root_crt: &Option<String>) -> Vec<String> {
    let mut args = vec!["db".to_string(), "connect".to_string()];
    if let Some(p) = root_crt {
        let trimmed = p.trim();
        if !trimmed.is_empty() {
            args.push("--root-crt".to_string());
            args.push(trimmed.to_string());
        }
    }
    args
}

/// `khipu db connect --use-stored`: the CLI verb Phase 2 needed and Phase 1
/// did not add — finishing setup (upkeep + prove) right after
/// `install_local_postgres` or a join import, both of which already wrote the
/// DSN to Keychain themselves. Re-sending that DSN through the webview just
/// to hand it back would put a password the frontend doesn't otherwise hold
/// into JS state for no reason, so instead the CLI resolves the
/// already-configured DSN itself (see `cmd_db` in cli.py). No DSN, so no env
/// var to set either.
fn db_connect_stored_argv() -> Vec<String> {
    vec!["db".to_string(), "connect".to_string(), "--use-stored".to_string()]
}

fn db_move_argv(dry_run: bool) -> Vec<String> {
    let mut args = vec!["db".to_string(), "move".to_string()];
    if dry_run {
        args.push("--dry-run".to_string());
    }
    args
}

/// `khipu db move --new-local`: the Move dialog's "a new database on this
/// Mac" target. A second CLI verb Phase 2 needed and Phase 1 did not add —
/// `install_local_postgres` stores the DSN it creates itself, which would
/// switch the active connection to the new, empty database before anything
/// is copied into it, so provisioning has to happen inside the same process
/// as the move (see `khipu.setup.provision_local_move_target`'s docstring).
/// No DSN crosses the webview boundary either way.
fn db_move_new_local_argv(dry_run: bool) -> Vec<String> {
    let mut args = vec!["db".to_string(), "move".to_string(), "--new-local".to_string()];
    if dry_run {
        args.push("--dry-run".to_string());
    }
    args
}

fn db_status_argv() -> Vec<String> {
    vec!["db".to_string(), "status".to_string()]
}

#[tauri::command]
async fn khipu_db_status() -> Result<String, String> {
    run_khipu_cli_async(db_status_argv()).await
}

#[tauri::command]
async fn khipu_db_preflight(dsn: String) -> Result<String, String> {
    run_khipu_cli_with_env_async(db_preflight_argv(), vec![("KHIPU_DB_DSN".to_string(), dsn)]).await
}

#[tauri::command]
async fn khipu_db_connect(dsn: String, root_crt: Option<String>) -> Result<String, String> {
    run_khipu_cli_with_env_async(
        db_connect_argv(&root_crt),
        vec![("KHIPU_DB_DSN".to_string(), dsn)],
    )
    .await
}

#[tauri::command]
async fn khipu_db_connect_stored() -> Result<String, String> {
    run_khipu_cli_async(db_connect_stored_argv()).await
}

#[tauri::command]
async fn khipu_db_move(target_dsn: String, dry_run: bool) -> Result<String, String> {
    run_khipu_cli_with_env_async(
        db_move_argv(dry_run),
        vec![("KHIPU_DB_TARGET_DSN".to_string(), target_dsn)],
    )
    .await
}

#[tauri::command]
async fn khipu_db_move_new_local(dry_run: bool) -> Result<String, String> {
    run_khipu_cli_async(db_move_new_local_argv(dry_run)).await
}

#[tauri::command]
async fn select_compat_row(
    mode: String,
    pgvector_extversion: Option<String>,
    server_version: Option<String>,
    pgvector: Option<String>,
) -> Result<String, String> {
    let mut args = vec![
        "components".into(),
        "select-compat-row".into(),
        "--mode".into(),
        mode,
    ];
    if let Some(v) = pgvector_extversion {
        if !v.trim().is_empty() {
            args.push("--pgvector-extversion".into());
            args.push(v.trim().to_string());
        }
    }
    if let Some(v) = server_version {
        if !v.trim().is_empty() {
            args.push("--server-version".into());
            args.push(v.trim().to_string());
        }
    }
    if let Some(v) = pgvector {
        if !v.trim().is_empty() {
            args.push("--pgvector".into());
            args.push(v.trim().to_string());
        }
    }
    run_khipu_cli_async(args).await
}

#[tauri::command]
async fn install_local_postgres() -> Result<String, String> {
    run_khipu_cli_async(vec!["components".into(), "install-local-postgres".into()]).await
}

#[tauri::command]
async fn bootstrap_local_backup() -> Result<String, String> {
    run_khipu_cli_async(vec!["components".into(), "bootstrap-local-backup".into()]).await
}

#[tauri::command]
async fn install_graphify() -> Result<String, String> {
    run_khipu_cli_async(vec!["components".into(), "install-graphify".into()]).await
}

#[tauri::command]
async fn components_status() -> Result<String, String> {
    run_khipu_cli_async(vec!["components".into(), "status-json".into()]).await
}

#[tauri::command]
async fn upgrade_postgres() -> Result<String, String> {
    run_khipu_cli_async(vec!["components".into(), "upgrade-postgres".into()]).await
}

#[tauri::command]
async fn upgrade_graphify() -> Result<String, String> {
    run_khipu_cli_async(vec!["components".into(), "upgrade-graphify".into()]).await
}

#[tauri::command]
async fn check_remote_postgres(full: bool) -> Result<String, String> {
    let mut args = vec!["components".into(), "check-remote".into()];
    if full {
        args.push("--full".into());
    }
    run_khipu_cli_async(args).await
}

/// Secrets the UI may write, and the Keychain accounts they map to.
///
/// `secrets` is deliberately NOT in `ALLOWED_SUBCOMMANDS`: that path forwards
/// arbitrary argv from the webview, and a secret must never travel as an
/// argument. This command is the only way in, and it pipes the value to the
/// CLI's stdin.
const SETTABLE_SECRETS: &[&str] = &["gemini_api_key", "database_url", "openai_compat_api_key", "voyage_api_key"];

#[tauri::command]
async fn set_khipu_secret(account: String, value: String) -> Result<String, String> {
    spawn_blocking_cli(move || set_khipu_secret_sync(account, value)).await
}

fn set_khipu_secret_sync(account: String, value: String) -> Result<String, String> {
    if !SETTABLE_SECRETS.contains(&account.as_str()) {
        eprintln!("[khipu] refused secret write from the UI: {account:?}");
        return Err(format!("not a settable secret: {account:?}"));
    }
    if value.trim().is_empty() {
        return Err("value is empty".to_string());
    }

    let root = khipu_root()?;
    let py = khipu_python()?;
    let pythonpath = khipu_pythonpath(&root);
    let (bc_key, bc_val) = khipu_bytecode_env();
    let mut child = Command::new(&py)
        .arg("-m")
        .arg("khipu")
        .arg("secrets")
        .arg("--set")
        .arg(&account)
        .env("PYTHONPATH", &pythonpath)
        .env("KHIPU_ROOT", &root)
        .env("KHIPU_APP_VERSION", khipu_app_version())
        .env(bc_key, &bc_val)
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

/// Export a join kit to disk. Optional passphrase travels via env, never argv —
/// same exposure model as `set_khipu_secret`'s stdin pipe. Empty passphrase
/// writes a plaintext kit (the file is the secret).
#[tauri::command]
async fn join_export(passphrase: String, out_path: String) -> Result<String, String> {
    spawn_blocking_cli(move || join_export_sync(passphrase, out_path)).await
}

fn join_export_sync(passphrase: String, out_path: String) -> Result<String, String> {
    if out_path.trim().is_empty() {
        return Err("out_path is empty".to_string());
    }
    let root = khipu_root()?;
    let py = khipu_python()?;
    let pythonpath = khipu_pythonpath(&root);
    let (bc_key, bc_val) = khipu_bytecode_env();
    let output = Command::new(&py)
        .arg("-m")
        .arg("khipu")
        .arg("join")
        .arg("export")
        .arg("--out")
        .arg(out_path.trim())
        .env("PYTHONPATH", &pythonpath)
        .env("KHIPU_ROOT", &root)
        .env("KHIPU_APP_VERSION", khipu_app_version())
        .env(bc_key, &bc_val)
        .env("KHIPU_JOIN_PASSPHRASE", passphrase.trim())
        .output()
        .map_err(|e| format!("spawn khipu join export failed ({py:?}): {e}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    if !output.status.success() {
        if stdout_looks_like_json(&stdout) {
            return Ok(stdout);
        }
        return Err(format!(
            "khipu join export exited {}",
            output.status.code().unwrap_or(-1)
        ));
    }
    Ok(stdout)
}

/// Import a join kit from disk. Passphrase travels via env, never argv.
#[tauri::command]
async fn join_import(passphrase: String, file_path: String) -> Result<String, String> {
    spawn_blocking_cli(move || join_import_sync(passphrase, file_path)).await
}

fn join_import_sync(passphrase: String, file_path: String) -> Result<String, String> {
    if file_path.trim().is_empty() {
        return Err("file_path is empty".to_string());
    }
    let root = khipu_root()?;
    let py = khipu_python()?;
    let pythonpath = khipu_pythonpath(&root);
    let (bc_key, bc_val) = khipu_bytecode_env();
    let output = Command::new(&py)
        .arg("-m")
        .arg("khipu")
        .arg("join")
        .arg("import")
        .arg("--file")
        .arg(file_path.trim())
        .env("PYTHONPATH", &pythonpath)
        .env("KHIPU_ROOT", &root)
        .env("KHIPU_APP_VERSION", khipu_app_version())
        .env(bc_key, &bc_val)
        .env("KHIPU_JOIN_PASSPHRASE", passphrase.trim())
        .output()
        .map_err(|e| format!("spawn khipu join import failed ({py:?}): {e}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    if !output.status.success() {
        if stdout_looks_like_json(&stdout) {
            return Ok(stdout);
        }
        return Err(format!(
            "khipu join import exited {}",
            output.status.code().unwrap_or(-1)
        ));
    }
    Ok(stdout)
}

/// Advertise join kit on LAN (Bonjour + TLS). Spawns a background job and returns
/// the first JSON line (PIN, port, timeout) while the server keeps running.
#[tauri::command]
async fn join_advertise(passphrase: String, timeout: u32) -> Result<String, String> {
    spawn_blocking_cli(move || join_advertise_sync(passphrase, timeout)).await
}

fn join_advertise_sync(passphrase: String, timeout: u32) -> Result<String, String> {
    let root = khipu_root()?;
    let py = khipu_python()?;
    let pythonpath = khipu_pythonpath(&root);
    let (bc_key, bc_val) = khipu_bytecode_env();
    let tout = timeout.max(60);
    let mut child = Command::new(&py)
        .arg("-m")
        .arg("khipu")
        .arg("join")
        .arg("advertise")
        .arg("--timeout")
        .arg(tout.to_string())
        .env("PYTHONPATH", &pythonpath)
        .env("KHIPU_ROOT", &root)
        .env("KHIPU_APP_VERSION", khipu_app_version())
        .env(bc_key, &bc_val)
        .env("KHIPU_JOIN_PASSPHRASE", passphrase.trim())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("spawn khipu join advertise failed ({py:?}): {e}"))?;
    let stdout = child.stdout.take().ok_or_else(|| "no stdout on join advertise".to_string())?;
    let mut reader = std::io::BufReader::new(stdout);
    let mut first_line = String::new();
    reader
        .read_line(&mut first_line)
        .map_err(|e| format!("read join advertise banner failed: {e}"))?;
    let pid = child.id();
    std::thread::spawn(move || {
        if let Err(e) = child.wait() {
            eprintln!("[khipu] join advertise wait failed: {e}");
        }
    });
    if first_line.trim().is_empty() {
        return Err("join advertise produced no status line".to_string());
    }
    let mut banner = parse_json_line(&first_line)?;
    if let Some(obj) = banner.as_object_mut() {
        obj.insert("pid".into(), Value::from(pid));
    }
    Ok(serde_json::to_string(&banner).unwrap_or(first_line))
}

fn parse_json_line(line: &str) -> Result<Value, String> {
    serde_json::from_str(line.trim()).map_err(|e| format!("invalid JSON from join advertise: {e}"))
}

/// Receive join kit from a nearby Mac (Bonjour browse + TLS + import).
#[tauri::command]
async fn join_receive(passphrase: String, pin: String, out_path: Option<String>) -> Result<String, String> {
    spawn_blocking_cli(move || join_receive_sync(passphrase, pin, out_path)).await
}

fn join_receive_sync(passphrase: String, pin: String, out_path: Option<String>) -> Result<String, String> {
    let pin_trim = pin.trim();
    if pin_trim.len() != 6 || !pin_trim.chars().all(|c| c.is_ascii_digit()) {
        return Err("pin must be six digits".to_string());
    }
    let root = khipu_root()?;
    let py = khipu_python()?;
    let pythonpath = khipu_pythonpath(&root);
    let (bc_key, bc_val) = khipu_bytecode_env();
    let mut cmd = Command::new(&py);
    cmd.arg("-m")
        .arg("khipu")
        .arg("join")
        .arg("receive")
        .arg("--pin")
        .arg(pin_trim)
        .env("PYTHONPATH", &pythonpath)
        .env("KHIPU_ROOT", &root)
        .env("KHIPU_APP_VERSION", khipu_app_version())
        .env(bc_key, &bc_val)
        .env("KHIPU_JOIN_PASSPHRASE", passphrase.trim());
    if let Some(path) = out_path {
        let trimmed = path.trim();
        if !trimmed.is_empty() {
            cmd.arg("--out").arg(trimmed);
        }
    }
    let output = cmd
        .output()
        .map_err(|e| format!("spawn khipu join receive failed ({py:?}): {e}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    if !output.status.success() {
        if stdout_looks_like_json(&stdout) {
            return Ok(stdout);
        }
        return Err(format!(
            "khipu join receive exited {}",
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
/// `runKhipu([...])` call site in `App.tsx`, `Welcome.tsx`, and
/// `IntegrationsPanel.tsx` rather than assumed. The first pass kept five more
/// — `sessions`, `git-sync`, `outbox`, `embed`, `graph-sync` — that no pane
/// should call through `run_khipu`. Welcome first-run activates cloud embed
/// via `models welcome` (in-process), not the `embed` subcommand. The
/// boundary must not hand a compromised webview paid Gemini calls
/// (`embed backfill`, which also deletes vectors), model extraction
/// (`sessions drain`), or a `git-sync` that pushes commits and merges PRs.
/// Removed 2026-08-18.
///
/// `integrations` stays and keeps its install/uninstall: the Integrations pane
/// offers both as deliberate user actions, and uninstall is the documented
/// rollback. `paths` stays for the same reason — the Settings pane's "Set
/// folder" button is `paths --set`.
/// `episode` is the one state-changing entry here, added deliberately: the
/// Activity pane's "Forget" button (`episode forget ID`) is an explicit user
/// action behind a confirm dialog, and soft-deleting one's own episode is the
/// documented way to remove something the capture hook recorded. Its argv is
/// still constrained by the CLI itself — `episode` accepts only `forget ID`
/// and `edit ID --summary TEXT` (the Activity pane's "Edit summary", which
/// corrects a summary and re-embeds it; it cannot reach any other table).
/// `owed` now carries the Owed screen's three writes as well as its reads:
/// `--close` / `--reopen` / `--snooze ID --until …` all act on one commitment
/// row by id, and `--until` is parsed by the CLI (a value it cannot read as a
/// date is refused, never bound).
const ALLOWED_SUBCOMMANDS: &[&str] = &[
    "status", "doctor", "activity", "search", "graph", "revisions", "paths",
    "backup-local", "import-local", "integrations", "sources", "models",
    "episode", "owed",
];

/// Fire-and-forget job runners — long-running consolidate/graphify passes.
/// Deliberately separate from `ALLOWED_SUBCOMMANDS`: these spawn detached and
/// return immediately; the UI must not block on `.output()`.
const SPAWN_ALLOWED_SUBCOMMANDS: &[&str] = &["nightly", "graph-build", "monthly"];

#[tauri::command]
async fn run_khipu(args: Vec<String>) -> Result<String, String> {
    let sub = args.first().cloned().unwrap_or_default();
    if !ALLOWED_SUBCOMMANDS.contains(&sub.as_str()) {
        eprintln!("[khipu] refused CLI subcommand from the UI: {sub:?}");
        return Err(format!("subcommand not permitted from the app: {sub:?}"));
    }
    run_khipu_cli_async(args).await
}

#[tauri::command]
fn spawn_khipu(subcommand: String) -> Result<Value, String> {
    if !SPAWN_ALLOWED_SUBCOMMANDS.contains(&subcommand.as_str()) {
        eprintln!("[khipu] refused spawn subcommand from the UI: {subcommand:?}");
        return Err(format!("subcommand not permitted for spawn: {subcommand:?}"));
    }
    let root = khipu_root()?;
    let py = khipu_python()?;
    let pythonpath = khipu_pythonpath(&root);
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let log_path = dirs_fallback_dsn()
        .parent()
        .map(|p| p.join(format!("khipu-job-{subcommand}-{stamp}.log")))
        .unwrap_or_else(|| PathBuf::from(format!("/tmp/khipu-job-{subcommand}-{stamp}.log")));
    if let Some(parent) = log_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let log_file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|e| format!("open spawn log {}: {e}", log_path.display()))?;
    let err_file = log_file
        .try_clone()
        .map_err(|e| format!("clone spawn log handle: {e}"))?;
    let (bc_key, bc_val) = khipu_bytecode_env();
    let mut child = Command::new(&py)
        .arg("-m")
        .arg("khipu")
        .arg(&subcommand)
        .env("PYTHONPATH", &pythonpath)
        .env("KHIPU_ROOT", &root)
        .env("KHIPU_APP_VERSION", khipu_app_version())
        .env(bc_key, &bc_val)
        .stdout(Stdio::from(log_file))
        .stderr(Stdio::from(err_file))
        .spawn()
        .map_err(|e| format!("spawn khipu {subcommand} failed ({py:?}): {e}"))?;
    let pid = child.id();
    // Reap in the background so the Unix zombie does not linger until Tauri
    // exits. Must not `.wait()` / `.output()` on this thread (C8 fire-and-forget).
    std::thread::spawn(move || {
        if let Err(e) = child.wait() {
            eprintln!("[khipu] wait on spawned job failed: {e}");
        }
    });
    let engine_log_path = engine_job_log_path(&subcommand);
    Ok(serde_json::json!({
        "ok": true,
        "pid": pid,
        "log_path": log_path.to_string_lossy(),
        "engine_log_path": engine_log_path.to_string_lossy(),
        "subcommand": subcommand,
    }))
}

/// Engine stdout for spawnable jobs — same stems as `jobs.py` `_JOB_SPECS`
/// (`khipu-nightly` / `khipu-monthly` / `khipu-graph`) under
/// `~/Library/Logs/Khipu/`, falling back to `~/Library/Logs/<KHIPU_LEGACY_LOG_DIR>`
/// when that env var is set and the Khipu dir does not exist yet (an upgrade
/// whose launchd jobs still write to the product's pre-rename log directory).
/// Distinct from the wrapper spawn log.
fn engine_job_log_path(subcommand: &str) -> PathBuf {
    let stem = match subcommand {
        "nightly" => "khipu-nightly",
        "monthly" => "khipu-monthly",
        "graph-build" => "khipu-graph",
        other => other,
    };
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    let khipu_dir = PathBuf::from(&home).join("Library/Logs/Khipu");
    let log_dir = if khipu_dir.is_dir() {
        khipu_dir
    } else {
        match std::env::var("KHIPU_LEGACY_LOG_DIR") {
            Ok(legacy) if !legacy.is_empty() => {
                PathBuf::from(&home).join("Library/Logs").join(legacy)
            }
            _ => khipu_dir,
        }
    };
    log_dir.join(format!("{stem}.out.log"))
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
async fn dsn_configured(force: bool) -> bool {
    match tauri::async_runtime::spawn_blocking(move || dsn_configured_sync(force)).await {
        Ok(v) => v,
        Err(e) => {
            eprintln!("[khipu] dsn_configured worker join failed: {e}");
            false
        }
    }
}

fn dsn_configured_sync(force: bool) -> bool {
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
async fn health_snapshot() -> Result<Value, String> {
    let raw = run_khipu_cli_async(vec!["status".into()]).await?;
    serde_json::from_str(&raw).map_err(|e| format!("status JSON: {e}"))
}

const FEEDBACK_ATTACH_MAX_BYTES: u64 = 15 * 1024 * 1024;
const FEEDBACK_MAX_FILES: usize = 10;
const FEEDBACK_URL: &str = "https://kinglollipop.com/api/feedback";

fn attachment_bytes_cap_ok(total: u64) -> Result<(), String> {
    if total > FEEDBACK_ATTACH_MAX_BYTES {
        return Err(format!(
            "attachments exceed {} MiB total",
            FEEDBACK_ATTACH_MAX_BYTES / (1024 * 1024)
        ));
    }
    Ok(())
}

fn feedback_os_string() -> String {
    let os = std::env::consts::OS;
    if os == "macos" {
        if let Ok(out) = Command::new("sw_vers").arg("-productVersion").output() {
            if out.status.success() {
                let ver = String::from_utf8_lossy(&out.stdout).trim().to_string();
                if !ver.is_empty() {
                    return format!("macos {ver}");
                }
            }
        }
    }
    os.to_string()
}

fn feedback_idempotency_key() -> String {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    let pid = std::process::id();
    format!("{millis}-{pid}")
}

fn read_feedback_attachments(paths: &[String]) -> Result<Vec<(PathBuf, Vec<u8>)>, String> {
    if paths.len() > FEEDBACK_MAX_FILES {
        return Err(format!("too many attachments (max {FEEDBACK_MAX_FILES})"));
    }
    let mut files = Vec::new();
    let mut total: u64 = 0;
    for raw in paths {
        let path = PathBuf::from(raw.trim());
        if raw.trim().is_empty() {
            continue;
        }
        if !path.is_file() {
            return Err(format!("attachment not found: {}", path.display()));
        }
        let meta = std::fs::metadata(&path)
            .map_err(|e| format!("read attachment metadata {}: {e}", path.display()))?;
        let len = meta.len();
        total = total.saturating_add(len);
        attachment_bytes_cap_ok(total)?;
        let bytes = std::fs::read(&path)
            .map_err(|e| format!("read attachment {}: {e}", path.display()))?;
        files.push((path, bytes));
    }
    Ok(files)
}

/// Metadata only — do not fetch file bytes through the webview asset protocol
/// just to show sizes (that would load the whole attachment into JS).
fn feedback_file_sizes_sync(paths: Vec<String>) -> Result<Vec<Option<u64>>, String> {
    Ok(paths
        .iter()
        .map(|raw| {
            let path = PathBuf::from(raw.trim());
            std::fs::metadata(&path)
                .ok()
                .filter(|m| m.is_file())
                .map(|m| m.len())
        })
        .collect())
}

fn send_feedback_sync(
    reply_to: String,
    message: String,
    app_version: String,
    paths: Vec<String>,
) -> Result<(), String> {
    let reply_to = reply_to.trim().to_string();
    let message = message.trim().to_string();
    if reply_to.is_empty() || !reply_to.contains('@') {
        return Err("reply_to must be a valid email".into());
    }
    if message.is_empty() {
        return Err("message is required".into());
    }

    let attachments = read_feedback_attachments(&paths)?;
    let os = feedback_os_string();
    let idempotency_key = feedback_idempotency_key();

    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(60))
        .build()
        .map_err(|e| format!("HTTP client: {e}"))?;

    let mut form = reqwest::blocking::multipart::Form::new()
        .text("reply_to", reply_to)
        .text("message", message)
        .text("app_version", app_version)
        .text("os", os)
        .text("idempotency_key", idempotency_key)
        .text("company", String::new());

    for (path, bytes) in attachments {
        let file_name = path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("attachment")
            .to_string();
        let part = reqwest::blocking::multipart::Part::bytes(bytes).file_name(file_name);
        form = form.part("files", part);
    }

    let response = client
        .post(FEEDBACK_URL)
        .multipart(form)
        .send()
        .map_err(|e| format!("feedback request failed: {e}"))?;

    let status = response.status();
    if !status.is_success() {
        let body = response.text().unwrap_or_default();
        let snippet: String = body.chars().take(500).collect();
        return Err(format!(
            "feedback server returned {}: {}",
            status.as_u16(),
            snippet.trim()
        ));
    }
    Ok(())
}

#[tauri::command]
async fn send_feedback(
    reply_to: String,
    message: String,
    app_version: String,
    paths: Vec<String>,
) -> Result<(), String> {
    spawn_blocking_cli(move || send_feedback_sync(reply_to, message, app_version, paths)).await
}

#[tauri::command]
async fn feedback_file_sizes(paths: Vec<String>) -> Result<Vec<Option<u64>>, String> {
    spawn_blocking_cli(move || feedback_file_sizes_sync(paths)).await
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
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            run_khipu,
            spawn_khipu,
            set_khipu_secret,
            join_export,
            join_import,
            join_advertise,
            join_receive,
            secrets_presence,
            khipu_secrets_verify,
            khipu_migrate,
            khipu_embed_backfill,
            khipu_jobs_refresh,
            khipu_features_show,
            khipu_feature_set,
            khipu_config_show,
            khipu_config_set,
            khipu_config_unset,
            khipu_jobs_status,
            khipu_job_install,
            khipu_job_uninstall,
            khipu_recall_status,
            khipu_gateway_token_status,
            khipu_gateway_token_set,
            khipu_embed_status,
            khipu_embed_activate,
            liveness_now,
            khipu_capture_now,
            khipu_db_status,
            khipu_db_preflight,
            khipu_db_connect,
            khipu_db_connect_stored,
            khipu_db_move,
            khipu_db_move_new_local,
            select_compat_row,
            install_local_postgres,
            bootstrap_local_backup,
            install_graphify,
            components_status,
            upgrade_postgres,
            upgrade_graphify,
            check_remote_postgres,
            dsn_configured,
            health_snapshot,
            send_feedback,
            feedback_file_sizes
        ])
        .setup(|app| {
            // See clean_bundled_pycache: sweep any __pycache__ a past launch
            // left inside the signed bundle before it can trip this run's
            // Gatekeeper check.
            clean_bundled_pycache();
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

            // After an app update the LaunchAgents may still point at the
            // previous bundle's Python and paths (they were baked in at
            // whatever install rendered them, and nothing else re-renders
            // them). Re-render what is already installed at every launch so
            // a bundled-Python or Application Support path change lands
            // without requiring a manual reinstall.
            tauri::async_runtime::spawn(async move {
                match run_khipu_cli_async(
                    JOBS_REFRESH_ARGV.iter().map(|s| s.to_string()).collect(),
                )
                .await
                {
                    Ok(out) => {
                        let snippet: String = out.chars().take(200).collect();
                        eprintln!("[khipu-jobs] refresh: {snippet}");
                    }
                    Err(e) => {
                        eprintln!("[khipu-jobs] refresh failed: {e}");
                    }
                }
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
mod feedback_attachment_tests {
    use super::{attachment_bytes_cap_ok, FEEDBACK_ATTACH_MAX_BYTES};

    #[test]
    fn cap_allows_exactly_fifteen_mib() {
        assert!(attachment_bytes_cap_ok(FEEDBACK_ATTACH_MAX_BYTES).is_ok());
    }

    #[test]
    fn cap_rejects_one_byte_over() {
        assert!(attachment_bytes_cap_ok(FEEDBACK_ATTACH_MAX_BYTES + 1).is_err());
    }

    #[test]
    fn cap_allows_zero() {
        assert!(attachment_bytes_cap_ok(0).is_ok());
    }

    #[test]
    fn rejects_more_than_max_files() {
        let paths: Vec<String> = (0..11).map(|i| format!("/tmp/f{i}")).collect();
        let err = super::read_feedback_attachments(&paths).unwrap_err();
        assert!(err.contains("too many"));
    }
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
    fn components_is_not_reachable_through_the_generic_argv_path() {
        assert!(!ALLOWED_SUBCOMMANDS.contains(&"components"));
    }

    #[test]
    fn join_is_not_reachable_through_the_generic_argv_path() {
        // DSN lives in the join kit; export/import only via join_export /
        // join_import so argv cannot carry secrets through run_khipu.
        assert!(!ALLOWED_SUBCOMMANDS.contains(&"join"));
    }

    #[test]
    fn only_the_known_accounts_are_writable() {
        assert_eq!(SETTABLE_SECRETS, &["gemini_api_key", "database_url", "openai_compat_api_key", "voyage_api_key"]);
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
mod db_command_tests {
    use super::{
        db_connect_argv, db_connect_stored_argv, db_move_argv, db_move_new_local_argv,
        db_preflight_argv, db_status_argv, ALLOWED_SUBCOMMANDS,
    };

    #[test]
    fn db_is_not_reachable_through_the_generic_argv_path() {
        // `db connect`/`db move` write the stored connection and the DSN can
        // carry a password; each verb gets its own fixed-argv command
        // instead (khipu_db_status/preflight/connect/move), same shape as
        // `migrate`/`embed`/`jobs`.
        assert!(!ALLOWED_SUBCOMMANDS.contains(&"db"));
    }

    #[test]
    fn preflight_argv_is_fixed_and_carries_no_dsn() {
        assert_eq!(db_preflight_argv(), vec!["db".to_string(), "preflight".to_string()]);
    }

    #[test]
    fn connect_argv_has_no_root_crt_by_default() {
        assert_eq!(db_connect_argv(&None), vec!["db".to_string(), "connect".to_string()]);
    }

    #[test]
    fn connect_argv_adds_root_crt_when_given() {
        assert_eq!(
            db_connect_argv(&Some("/tmp/root.crt".to_string())),
            vec![
                "db".to_string(),
                "connect".to_string(),
                "--root-crt".to_string(),
                "/tmp/root.crt".to_string(),
            ]
        );
    }

    #[test]
    fn connect_argv_ignores_a_blank_root_crt_path() {
        assert_eq!(
            db_connect_argv(&Some("   ".to_string())),
            vec!["db".to_string(), "connect".to_string()]
        );
    }

    #[test]
    fn connect_stored_argv_uses_the_use_stored_flag_and_no_dsn() {
        assert_eq!(
            db_connect_stored_argv(),
            vec!["db".to_string(), "connect".to_string(), "--use-stored".to_string()]
        );
    }

    #[test]
    fn move_argv_default_is_a_real_move_not_a_dry_run() {
        assert_eq!(db_move_argv(false), vec!["db".to_string(), "move".to_string()]);
    }

    #[test]
    fn move_argv_dry_run_adds_the_flag() {
        assert_eq!(
            db_move_argv(true),
            vec!["db".to_string(), "move".to_string(), "--dry-run".to_string()]
        );
    }

    #[test]
    fn status_argv_is_fixed_and_takes_no_dsn() {
        assert_eq!(db_status_argv(), vec!["db".to_string(), "status".to_string()]);
    }

    #[test]
    fn move_new_local_argv_never_carries_a_dsn() {
        assert_eq!(
            db_move_new_local_argv(false),
            vec!["db".to_string(), "move".to_string(), "--new-local".to_string()]
        );
        assert_eq!(
            db_move_new_local_argv(true),
            vec![
                "db".to_string(),
                "move".to_string(),
                "--new-local".to_string(),
                "--dry-run".to_string(),
            ]
        );
    }
}

#[cfg(test)]
mod run_khipu_guard_tests {
    use super::ALLOWED_SUBCOMMANDS;

    #[test]
    fn every_subcommand_the_ui_calls_is_allowed() {
        for s in ["status", "doctor", "activity", "search", "graph", "revisions",
                  "paths", "backup-local", "import-local", "integrations", "sources",
                  "models", "episode", "owed"] {
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
        // Each one hands a compromised webview something real: paid Gemini
        // calls and vector deletes, model extraction, PG writes, and a git
        // push that opens and merges PRs. Welcome first-run uses
        // `models welcome` (in-process activate), not `embed`.
        for s in ["sessions", "git-sync", "outbox", "embed", "graph-sync"] {
            assert!(!ALLOWED_SUBCOMMANDS.contains(&s),
                    "`{s}` is not called by any pane and must not be reachable");
        }
    }

    #[test]
    fn the_allowlist_is_exactly_what_the_front_end_invokes() {
        // Grown by hand, so pin the size: adding an entry without a call site is
        // how the five above got in.
        assert_eq!(ALLOWED_SUBCOMMANDS.len(), 14);
    }

    #[test]
    fn index_now_is_a_fixed_argv_command_not_an_allowlist_entry() {
        // Settings -> Search index needs `embed backfill` and nothing else.
        // It gets it as a fixed argv (khipu_embed_backfill) so the webview
        // cannot reach `--profile`, `--kind` or any other embed verb.
        use super::EMBED_BACKFILL_ARGV;
        assert_eq!(EMBED_BACKFILL_ARGV, &["embed", "backfill"]);
        assert!(!ALLOWED_SUBCOMMANDS.contains(&"embed"));
    }

    #[test]
    fn jobs_refresh_is_a_fixed_argv_command_not_an_allowlist_entry() {
        // The app only ever needs `jobs refresh`; `jobs install`/`jobs
        // uninstall` change launchd state, so `jobs` stays out of the
        // allowlist and the webview gets exactly this fixed argv.
        use super::JOBS_REFRESH_ARGV;
        assert_eq!(JOBS_REFRESH_ARGV, &["jobs", "refresh"]);
        assert!(!ALLOWED_SUBCOMMANDS.contains(&"jobs"));
    }

    #[test]
    fn an_empty_or_unknown_subcommand_is_not_allowed() {
        assert!(!ALLOWED_SUBCOMMANDS.contains(&""));
        assert!(!ALLOWED_SUBCOMMANDS.contains(&"--help"));
    }
}

#[cfg(test)]
mod settings_command_tests {
    use super::*;

    fn v(a: &[&str]) -> Vec<String> {
        a.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn none_of_these_verbs_join_the_generic_allowlist() {
        for s in ["features", "config", "jobs", "recall", "gateway", "embed"] {
            assert!(!ALLOWED_SUBCOMMANDS.contains(&s), "`{s}` must stay out of the allowlist");
        }
        assert_eq!(ALLOWED_SUBCOMMANDS.len(), 14);
    }

    #[test]
    fn read_only_commands_are_fixed_argv() {
        assert_eq!(fixed_argv(FEATURES_SHOW_ARGV), v(&["features"]));
        assert_eq!(fixed_argv(CONFIG_SHOW_ARGV), v(&["config"]));
        assert_eq!(fixed_argv(JOBS_STATUS_ARGV), v(&["jobs", "status"]));
        assert_eq!(fixed_argv(RECALL_STATUS_ARGV), v(&["recall", "status"]));
        assert_eq!(fixed_argv(GATEWAY_TOKEN_STATUS_ARGV), v(&["gateway", "token", "status"]));
        assert_eq!(fixed_argv(GATEWAY_TOKEN_SET_ARGV), v(&["gateway", "token", "set"]));
    }

    #[test]
    fn every_switch_builds_an_on_and_an_off_argv() {
        assert_eq!(FEATURE_NAMES.len(), 9);
        for n in FEATURE_NAMES {
            assert_eq!(feature_set_argv(n, true).unwrap(), v(&["features", "--set", n, "on"]));
            assert_eq!(feature_set_argv(n, false).unwrap(), v(&["features", "--set", n, "off"]));
        }
    }

    #[test]
    fn the_switch_list_matches_the_python_registry() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../packages/cli/khipu/features.py");
        let src = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
        let block = src.split("FEATURES: dict[str, str] = {").nth(1).expect("FEATURES literal");
        let block = block.split("\n}").next().unwrap();
        for n in FEATURE_NAMES {
            assert!(block.contains(&format!("\"{n}\":")), "{n} missing from features.py");
        }
        assert_eq!(block.matches("\": \"").count(), FEATURE_NAMES.len(), "features.py has a switch the app lacks");
    }

    #[test]
    fn a_bad_switch_name_is_refused() {
        for bad in ["", "rerank ", "RERANK", "rerank on", "--help", "-x", "recall.daemon", "briefs\n"] {
            assert!(feature_set_argv(bad, true).is_err(), "accepted {bad:?}");
        }
    }

    #[test]
    fn job_argv_is_install_or_uninstall_of_a_known_job() {
        assert_eq!(job_argv("install", "recall_daemon").unwrap(), v(&["jobs", "install", "recall_daemon"]));
        assert_eq!(job_argv("uninstall", "nightly").unwrap(), v(&["jobs", "uninstall", "nightly"]));
        for n in JOB_NAMES {
            assert!(job_argv("install", n).is_ok());
        }
        assert_eq!(JOB_NAMES.len(), 6);
    }

    #[test]
    fn job_argv_refuses_unknown_names_and_verbs() {
        for bad in ["", "embed_media_backfill", "nightly monthly", "--help", "-a", "../nightly", "Nightly"] {
            assert!(job_argv("install", bad).is_err(), "accepted {bad:?}");
        }
        for verb in ["refresh", "status", "", "install --all"] {
            assert!(job_argv(verb, "nightly").is_err(), "accepted verb {verb:?}");
        }
    }

    #[test]
    fn capture_mode_uses_the_equals_form() {
        assert_eq!(config_set_argv("capture_mode", "hub").unwrap(), v(&["config", "--set-capture-mode=hub"]));
        assert_eq!(config_set_argv("capture_mode", " legacy ").unwrap(), v(&["config", "--set-capture-mode=legacy"]));
        for bad in ["", "HUB", "hub --unset", "both", "-hub"] {
            assert!(config_set_argv("capture_mode", bad).is_err(), "accepted {bad:?}");
        }
    }

    #[test]
    fn gateway_url_is_empty_or_https() {
        assert_eq!(
            config_set_argv("gateway_url", "https://k.example.org").unwrap(),
            v(&["config", "--set-gateway-url=https://k.example.org"])
        );
        assert_eq!(config_set_argv("gateway_url", "  ").unwrap(), v(&["config", "--set-gateway-url="]));
        for bad in ["http://k.example.org", "k.example.org", "https://a b", "https://a\nb", "--x", "ftp://x"] {
            assert!(config_set_argv("gateway_url", bad).is_err(), "accepted {bad:?}");
        }
    }

    #[test]
    fn similarity_knobs_take_zero_to_one_and_print_the_parsed_number() {
        for key in UNIT_FLOAT_KEYS {
            assert_eq!(config_set_argv(key, "0.8").unwrap(), v(&["config", "--set", key, "0.8"]));
            assert_eq!(config_set_argv(key, "0").unwrap(), v(&["config", "--set", key, "0"]));
            assert_eq!(config_set_argv(key, "1").unwrap(), v(&["config", "--set", key, "1"]));
            assert_eq!(config_set_argv(key, " +0.50 ").unwrap(), v(&["config", "--set", key, "0.5"]));
            assert_eq!(config_set_argv(key, "1e-1").unwrap(), v(&["config", "--set", key, "0.1"]));
        }
    }

    #[test]
    fn cosine_floor_is_above_zero_up_to_one() {
        assert_eq!(
            config_set_argv("relevance.cosine_floor", "0.65").unwrap(),
            v(&["config", "--set", "relevance.cosine_floor", "0.65"])
        );
        assert!(config_set_argv("relevance.cosine_floor", "1").is_ok());
        assert!(config_set_argv("relevance.cosine_floor", "0").is_err());
    }

    #[test]
    fn a_number_can_never_reach_argv_as_a_flag_or_junk() {
        for key in ["dedup_similarity", "commitment_close_similarity", "relevance.cosine_floor"] {
            for bad in ["-0.1", "-1", "--set", "-", "1.01", "2", "nan", "NaN", "inf", "-inf", "", "abc", "0.5 0.6", "0,5"] {
                assert!(config_set_argv(key, bad).is_err(), "{key} accepted {bad:?}");
            }
        }
    }

    #[test]
    fn user_aliases_and_paths_refuse_flags_and_control_characters() {
        assert_eq!(config_set_argv("user_aliases", "matt, matthew").unwrap(), v(&["config", "--set", "user_aliases", "matt, matthew"]));
        assert_eq!(config_set_argv("user_aliases", "").unwrap(), v(&["config", "--set", "user_aliases", ""]));
        for key in ["user_aliases", "memory_root", "memory_repo", "capture_v2", "graph_sqlite", "gemini_key_file"] {
            for bad in ["-x", "--unset", "--set-gateway-url=https://x", "a\nb", "a\0b", "a\tb"] {
                assert!(config_set_argv(key, bad).is_err(), "{key} accepted {bad:?}");
            }
        }
    }

    #[test]
    fn path_keys_are_non_empty_and_passed_after_set() {
        for key in PATH_KEYS {
            assert_eq!(config_set_argv(key, "/Volumes/X/mem").unwrap(), v(&["config", "--set", key, "/Volumes/X/mem"]));
            assert!(config_set_argv(key, "  ").is_err(), "{key} accepted an empty path");
        }
        assert_eq!(
            config_set_argv("memory_root", "~/Memory notes").unwrap(),
            v(&["config", "--set", "memory_root", "~/Memory notes"])
        );
    }

    #[test]
    fn unknown_config_keys_are_refused() {
        for bad in ["", "features", "models", "config", "database_url", "capture_modes", "relevance", "--set", "Memory_root"] {
            assert!(config_set_argv(bad, "1").is_err(), "accepted {bad:?}");
            assert!(config_unset_argv(bad).is_err(), "unset accepted {bad:?}");
        }
    }

    #[test]
    fn unset_takes_path_keys_and_the_cosine_floor_only() {
        for key in PATH_KEYS {
            assert_eq!(config_unset_argv(key).unwrap(), v(&["config", &format!("--unset={key}")]));
        }
        assert_eq!(
            config_unset_argv("relevance.cosine_floor").unwrap(),
            v(&["config", "--unset=relevance.cosine_floor"])
        );
        for not_resettable in ["capture_mode", "gateway_url", "user_aliases", "dedup_similarity"] {
            assert!(config_unset_argv(not_resettable).is_err());
        }
    }

    #[test]
    fn the_config_key_lists_match_the_python_settings() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../packages/cli/khipu/config.py");
        let src = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
        let paths = src.split("PATH_SETTINGS: dict").nth(1).unwrap().split("\n}").next().unwrap();
        for k in PATH_KEYS {
            assert!(paths.contains(&format!("\"{k}\":")), "{k} missing from PATH_SETTINGS");
        }
        let floats = src.split("FLOAT_SETTINGS: dict").nth(1).unwrap().split("\n}").next().unwrap();
        for k in UNIT_FLOAT_KEYS {
            assert!(floats.contains(&format!("\"{k}\":")), "{k} missing from FLOAT_SETTINGS");
        }
        let cli = std::fs::read_to_string(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../packages/cli/khipu/cli.py"),
        ).unwrap();
        let jobs = cli.split("_JOBS_CHOICES = (").nth(1).unwrap().split(')').next().unwrap();
        for n in JOB_NAMES {
            assert!(jobs.contains(&format!("\"{n}\"")), "{n} missing from _JOBS_CHOICES");
        }
    }

    #[test]
    fn embed_status_is_fixed_or_takes_a_checked_profile() {
        assert_eq!(embed_status_argv(None).unwrap(), v(&["embed", "status"]));
        assert_eq!(
            embed_status_argv(Some("gemini-embedding-2@768")).unwrap(),
            v(&["embed", "status", "--profile=gemini-embedding-2@768"])
        );
        assert!(embed_status_argv(Some("--profile=x")).is_err());
    }

    #[test]
    fn embed_activate_never_forces_and_checks_the_profile() {
        assert_eq!(
            embed_activate_argv("gemini-embedding-001@768").unwrap(),
            v(&["embed", "activate", "gemini-embedding-001@768"])
        );
        for ok in ["a", "x.y_z-1@2"] {
            assert!(embed_activate_argv(ok).is_ok(), "rejected {ok:?}");
        }
        for bad in ["", " ", "-f", "--force", "a b", "a;b", "a\nb", "a/b", "gemini$", &"x".repeat(65)] {
            assert!(embed_activate_argv(bad).is_err(), "accepted {bad:?}");
        }
        assert!(!embed_activate_argv("gemini-embedding-2@768").unwrap().iter().any(|a| a.contains("force")));
    }

    #[test]
    fn the_gateway_token_is_stdin_only_and_never_in_argv() {
        // The token has no argv builder at all: its argv is the fixed
        // constant, and the value goes through `run_khipu_cli_with_stdin`.
        assert_eq!(GATEWAY_TOKEN_SET_ARGV, &["gateway", "token", "set"]);
        assert_eq!(gateway_token_input("  abc123  ").unwrap(), "abc123");
        for bad in ["", "   ", "two words", "a\nb", "a\0b"] {
            assert!(gateway_token_input(bad).is_err(), "accepted {bad:?}");
        }
        let err = gateway_token_input("has space").unwrap_err();
        assert!(!err.contains("has space"), "error echoed the token");
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
