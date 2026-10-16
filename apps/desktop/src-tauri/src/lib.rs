use std::collections::HashSet;
use std::io::{BufRead, Write};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::Value;
use tauri::{
    image::Image,
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIcon, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager,
};
use tauri_plugin_updater::UpdaterExt;
use tauri_plugin_dialog::{DialogExt, FilePath};

const DSN_CACHE_TTL: Duration = Duration::from_secs(30);

struct DsnCache {
    value: bool,
    checked_at: Instant,
}

static DSN_CACHE: Mutex<Option<DsnCache>> = Mutex::new(None);
static FEEDBACK_SELECTED_PATHS: LazyLock<Mutex<HashSet<PathBuf>>> = LazyLock::new(|| Mutex::new(HashSet::new()));

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

fn env_raw_first(keys: &[&str]) -> Option<String> {
    for key in keys {
        if let Ok(value) = std::env::var(key) {
            if !value.is_empty() { return Some(value); }
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
    let (bc_key, bc_val) = khipu_bytecode_env();
    let output = Command::new(path)
        .arg("-c")
        .arg("import sys; print('%d.%d' % sys.version_info[:2])")
        .env(bc_key, bc_val)
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

/// The ten switches in `khipu.features.FEATURES`.
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
    "routing_push",
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
/// `relevance.cosine_floor_by_profile.<profile id>`: one model's own floor.
const COSINE_FLOOR_BY_PROFILE_PREFIX: &str = "relevance.cosine_floor_by_profile.";

const FEATURES_SHOW_ARGV: &[&str] = &["features"];
const CONFIG_SHOW_ARGV: &[&str] = &["config"];
const JOBS_STATUS_ARGV: &[&str] = &["jobs", "status"];
const RECALL_STATUS_ARGV: &[&str] = &["recall", "status"];
const GATEWAY_TOKEN_STATUS_ARGV: &[&str] = &["gateway", "token", "status"];
const GATEWAY_TOKEN_SET_ARGV: &[&str] = &["gateway", "token", "set"];
/// Settings -> Capture -> summariser ceiling: read every scope's calls and cap,
/// and give back the full synth ceiling for the rest of today. Fixed argv, so
/// the webview cannot reach `--cap`, `--unlimited` or another scope here (the
/// persisted ceiling goes through `config --set synth_daily_calls N`).
const BUDGET_STATUS_ARGV: &[&str] = &["budget", "status"];
const BUDGET_LIFT_SYNTH_TODAY_ARGV: &[&str] = &["budget", "lift", "synth", "--today"];
/// Home -> Plans and Settings -> Capture -> Plans: each subscription CLI's own
/// plan headroom (docs/plans/2026-10-14-plan-usage.md, phase 2). Status reads
/// the cache while it is younger than its max age (120 s); refresh probes
/// every account now. Fixed argv: the webview cannot reach `--max-age`.
const USAGE_STATUS_ARGV: &[&str] = &["usage", "--json"];
const USAGE_REFRESH_ARGV: &[&str] = &["usage", "--fresh", "--json"];
/// Home -> Routing, the health report's Routing row and the routing
/// attention items (docs/plans/2026-10-14-routing-truth.md, phase 4): the
/// routing record as `khipu route --json` prints it. Read only and fixed
/// argv: the webview cannot reach `--since`, an account, or any of `route`'s
/// write verbs (report, hold, pause, lift, clear, note, rule), and `route`
/// stays out of `ALLOWED_SUBCOMMANDS`.
const ROUTE_VIEW_ARGV: &[&str] = &["route", "--json"];
/// `config --set KEY N`, a whole number >= 0 (0 = unlimited) (`INT_SETTINGS`).
/// Only the summariser's ceiling is offered in the app.
const DAILY_CALL_KEYS: &[&str] = &["synth_daily_calls"];
/// `config --set synth_fallback gemini|none`: retry a window the summariser
/// provider failed on Gemini Flash, or leave it queued.
const SYNTH_FALLBACKS: &[&str] = &["gemini", "none"];
/// `config --set usage_accounts JSON` / `config --unset usage_accounts`: the
/// accounts Home -> Plans reads (Settings -> Capture & models -> Plans).
const USAGE_ACCOUNTS_KEY: &str = "usage_accounts";
const GRAPH_BACKUP_REMOTE_KEY: &str = "graph_backup_remote";

/// An rclone `remote:path` (the CLI checks it again): one token, the remote
/// name first, so it can never read as a flag.
fn checked_backup_remote(value: &str) -> Result<String, String> {
    let v = value.trim().trim_end_matches('/');
    let (remote, path) = v.split_once(':').unwrap_or(("", ""));
    let ok = v.len() <= 512
        && !remote.is_empty()
        && !path.is_empty()
        && remote.chars().next().is_some_and(|c| c.is_ascii_alphanumeric())
        && remote.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-'))
        && !v.chars().any(|c| c.is_control() || c.is_whitespace());
    if ok {
        Ok(v.to_string())
    } else {
        Err(format!("graph_backup_remote must look like r2:bucket/folder, got {value:?}"))
    }
}
const USAGE_HARNESSES: &[&str] = &["claude", "codex", "grok", "muse"];
const USAGE_ACCOUNTS_MAX: usize = 32;
/// Settings -> Routing (routing truth phase 4): the sizes, times and switches
/// the page offers, each `config --set KEY VALUE` or `config --unset=KEY`
/// (the default again). The reset check (phase 5), the delegation switch
/// (phase 6) and the copy for cloud agents (phase 7) join with their phases.
/// Lengths of time, in whole seconds (0 turns the limit off where the CLI
/// says so).
const ROUTING_DURATION_KEYS: &[&str] = &[
    "routing_stale_after",
    "routing_conversation_hold",
    "routing_conversation_hold_max",
    "routing_owner_hold_reminder",
    "routing_disagreement_warn_after",
    "routing_resend_after",
    "routing_not_refreshed_after",
    "routing_report_default_limit",
    "routing_poll_interval",
    "routing_reset_check_wait",
    "routing_busy_wait",
    "routing_priority_unconfirmed_warn_after",
];
/// Whole numbers of 0 or more.
const ROUTING_COUNT_KEYS: &[&str] = &[
    "routing_summary_chars",
    "routing_prompt_chars",
    "routing_prompt_budget_ms",
    "routing_resend_after_versions",
    "routing_hold_chars",
    "routing_line_chars",
    "routing_start_budget_ms",
    "routing_runner_silent_intervals",
    "routing_runner_tail_bytes",
    "routing_runner_limited_lines",
    "routing_busy_tries",
];
/// Numbers of 0 or more, a fraction allowed (a percentage).
const ROUTING_NUMBER_KEYS: &[&str] = &["routing_slow_read_pct", "routing_busy_load"];
/// On or off.
const ROUTING_SWITCH_KEYS: &[&str] = &[
    "routing_push_start",
    "routing_push_prompt",
    "routing_push_delegation",
    "routing_reset_check",
    "routing_pace_order",
];
/// The owner's own dispatch scripts, by file name (phase 6).
const ROUTING_DISPATCH_SCRIPTS_KEY: &str = "routing_dispatch_scripts";
const ROUTING_DISPATCH_SCRIPTS_MAX: usize = 16;
const ROUTING_RETRIES_KEY: &str = "routing_reset_check_retries";
const ROUTING_SOURCES_KEY: &str = "routing_sources";
const ROUTING_LANE_MAP_KEY: &str = "routing_lane_map";
/// The routing table file: a full path, read only (fix round 2, R2-1).
const ROUTING_TABLE_KEY: &str = "routing_table";
const ROUTING_SOURCES_MAX: usize = 32;

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

/// A daily call ceiling the CLI would accept, re-printed from the parsed value.
fn parse_daily_calls(key: &str, raw: &str) -> Result<String, String> {
    let v: u64 = raw
        .trim()
        .parse()
        .map_err(|_| format!("{key} must be a whole number of 0 or more, got {raw:?}"))?;
    if v > 10_000_000 {
        return Err(format!("{key} is too large, got {raw:?}"));
    }
    Ok(v.to_string())
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

/// The account list parsed and re-serialised, so only a JSON list of
/// `{label, harness, home}` text fields ever reaches argv. The CLI checks the
/// rest (a home that names no user, two entries for one login or one label).
#[cfg(test)]
fn checked_usage_accounts(raw: &str) -> Result<String, String> {
    checked_usage_accounts_kept(raw, &[])
}

/// An entry's account fields as `khipu.usage._same_entry` compares them:
/// text trimmed, a missing or empty field left out, any other field ignored.
fn same_entry_fields(entry: &Value) -> Option<serde_json::Map<String, Value>> {
    let obj = entry.as_object()?;
    let mut out = serde_json::Map::new();
    for field in ["label", "harness", "home"] {
        match obj.get(field) {
            None | Some(Value::Null) => {}
            Some(Value::String(s)) if s.is_empty() => {}
            Some(Value::String(s)) => {
                out.insert(field.to_string(), Value::String(s.trim().to_string()));
            }
            Some(other) => {
                out.insert(field.to_string(), other.clone());
            }
        }
    }
    Some(out)
}

/// As `checked_usage_accounts`, with `stored` the list saved now: an entry
/// the owner left as it is stored goes through exactly as stored, whatever
/// rule it breaks today (as the CLI keeps it); only entries he added or
/// changed are checked here.
fn checked_usage_accounts_kept(raw: &str, stored: &[Value]) -> Result<String, String> {
    let not_a_list = || "usage_accounts must be a JSON list of accounts".to_string();
    let value: serde_json::Value = serde_json::from_str(raw.trim()).map_err(|_| not_a_list())?;
    let entries = value.as_array().ok_or_else(not_a_list)?;
    if entries.len() > USAGE_ACCOUNTS_MAX {
        return Err(format!("usage_accounts holds at most {USAGE_ACCOUNTS_MAX} entries"));
    }
    let mut out = Vec::with_capacity(entries.len());
    for (i, entry) in entries.iter().enumerate() {
        let n = i + 1;
        if let Some(fields) = same_entry_fields(entry) {
            if let Some(kept) = stored.iter().find(|s| same_entry_fields(s).as_ref() == Some(&fields)) {
                out.push(kept.clone());
                continue;
            }
        }
        let obj = entry
            .as_object()
            .ok_or_else(|| format!("usage_accounts entry {n} is not an object"))?;
        let mut clean = serde_json::Map::new();
        for (field, v) in obj {
            let limit = match field.as_str() {
                "label" => 64,
                "harness" => 16,
                "home" => 1024,
                _ => return Err(format!("usage_accounts entry {n} has an unknown field {field:?}")),
            };
            if v.is_null() {
                continue;
            }
            let s = v
                .as_str()
                .ok_or_else(|| format!("usage_accounts entry {n}: {field} must be text"))?
                .trim();
            if has_control(s) {
                return Err(format!("usage_accounts entry {n}: {field} must be one line"));
            }
            if s.chars().count() > limit {
                return Err(format!("usage_accounts entry {n}: {field} is too long"));
            }
            if !s.is_empty() {
                clean.insert(field.clone(), serde_json::Value::String(s.to_string()));
            }
        }
        let harness = clean
            .get("harness")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_lowercase();
        if !USAGE_HARNESSES.contains(&harness.as_str()) {
            return Err(format!(
                "usage_accounts entry {n}: harness must be claude, codex, grok or muse"
            ));
        }
        clean.insert("harness".to_string(), serde_json::Value::String(harness));
        out.push(serde_json::Value::Object(clean));
    }
    serde_json::to_string(&out).map_err(|e| e.to_string())
}

fn routing_key(key: &str) -> bool {
    ROUTING_DURATION_KEYS.contains(&key)
        || ROUTING_COUNT_KEYS.contains(&key)
        || ROUTING_NUMBER_KEYS.contains(&key)
        || ROUTING_SWITCH_KEYS.contains(&key)
        || key == ROUTING_RETRIES_KEY
        || key == ROUTING_SOURCES_KEY
        || key == ROUTING_LANE_MAP_KEY
        || key == ROUTING_TABLE_KEY
        || key == ROUTING_DISPATCH_SCRIPTS_KEY
}

/// The dispatch scripts: a JSON list of file names only (letters, digits,
/// dots, dashes and underscores; no folder), re-serialised.
fn checked_dispatch_scripts(raw: &str) -> Result<String, String> {
    let bad = || "dispatch scripts must be a JSON list of file names".to_string();
    let value: serde_json::Value = serde_json::from_str(raw.trim()).map_err(|_| bad())?;
    let items = value.as_array().ok_or_else(bad)?;
    if items.len() > ROUTING_DISPATCH_SCRIPTS_MAX {
        return Err(format!("at most {ROUTING_DISPATCH_SCRIPTS_MAX} dispatch scripts"));
    }
    let mut out: Vec<String> = Vec::with_capacity(items.len());
    for item in items {
        let name = item.as_str().ok_or_else(bad)?.trim();
        let plain = !name.is_empty()
            && name.chars().count() <= 64
            && name.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
            && name != "."
            && name != ".."
            && !name.starts_with('-');
        if !plain {
            return Err(format!("{name:?} must be a file name only: letters, digits, dots, dashes and underscores"));
        }
        if !out.iter().any(|n| n == name) {
            out.push(name.to_string());
        }
    }
    serde_json::to_string(&out).map_err(|e| e.to_string())
}

/// A file's full path, one line, starting with `/` or `~/` (so never with
/// `-`), as the job runner folders are checked. The CLI refuses a file inside
/// a private folder.
fn checked_routing_path(what: &str, raw: &str) -> Result<String, String> {
    let v = raw.trim();
    if v.is_empty() || has_control(v) || v.chars().count() > 1024 {
        return Err(format!("{what} must be one line of at most 1024 characters"));
    }
    if !(v.starts_with('/') || v.starts_with("~/")) {
        return Err(format!("{what} needs its full path, starting with / or ~/"));
    }
    Ok(v.to_string())
}

/// One line of text inside a routing JSON setting.
fn routing_json_text(what: &str, v: &serde_json::Value, limit: usize) -> Result<String, String> {
    let s = v.as_str().ok_or_else(|| format!("{what} must be text"))?.trim();
    if s.is_empty() || has_control(s) || s.chars().count() > limit {
        return Err(format!("{what} must be one line of at most {limit} characters"));
    }
    Ok(s.to_string())
}

/// The job runner folders, parsed and re-serialised: only `{name, kind:
/// "lanes", path}` with an absolute path ever reaches argv. The CLI refuses a
/// folder inside a private one.
fn checked_routing_sources(raw: &str) -> Result<String, String> {
    let bad = || "job runner folders must be a JSON list of {name, kind, path}".to_string();
    let value: serde_json::Value = serde_json::from_str(raw.trim()).map_err(|_| bad())?;
    let entries = value.as_array().ok_or_else(bad)?;
    if entries.len() > ROUTING_SOURCES_MAX {
        return Err(format!("at most {ROUTING_SOURCES_MAX} job runner folders"));
    }
    let mut out = Vec::with_capacity(entries.len());
    for (i, entry) in entries.iter().enumerate() {
        let n = i + 1;
        let obj = entry.as_object().ok_or_else(bad)?;
        if obj.keys().any(|k| !matches!(k.as_str(), "name" | "kind" | "path")) {
            return Err(format!("folder {n} has a field Khipu doesn't keep"));
        }
        let name = routing_json_text(&format!("folder {n}'s name"), obj.get("name").unwrap_or(&serde_json::Value::Null), 64)?;
        let path = routing_json_text(&format!("folder {n}'s path"), obj.get("path").unwrap_or(&serde_json::Value::Null), 1024)?;
        if !(path.starts_with('/') || path.starts_with("~/")) {
            return Err(format!("folder {n} needs its full path, starting with / or ~/"));
        }
        out.push(serde_json::json!({"name": name, "kind": "lanes", "path": path}));
    }
    serde_json::to_string(&out).map_err(|e| e.to_string())
}

/// Which account each job lane uses: `{runner: {lane: account}}`, parsed
/// and re-serialised from text fields only.
fn checked_routing_lane_map(raw: &str) -> Result<String, String> {
    let bad = || "the lane map must be JSON like {\"runner\": {\"lane\": \"account\"}}".to_string();
    let value: serde_json::Value = serde_json::from_str(raw.trim()).map_err(|_| bad())?;
    let runners = value.as_object().ok_or_else(bad)?;
    if runners.len() > 64 {
        return Err("at most 64 job runners in the lane map".to_string());
    }
    let mut out = serde_json::Map::new();
    for (runner, lanes) in runners {
        let runner = routing_json_text("a runner's name", &serde_json::Value::String(runner.clone()), 64)?;
        let lanes = lanes.as_object().ok_or_else(bad)?;
        if lanes.len() > 256 {
            return Err(format!("at most 256 lanes for {runner}"));
        }
        let mut clean = serde_json::Map::new();
        for (lane, account) in lanes {
            let lane = routing_json_text("a lane's name", &serde_json::Value::String(lane.clone()), 64)?;
            clean.insert(lane, serde_json::Value::String(routing_json_text("an account", account, 1100)?));
        }
        out.insert(runner, serde_json::Value::Object(clean));
    }
    serde_json::to_string(&out).map_err(|e| e.to_string())
}

/// A routing setting's value as argv takes it, re-printed from the parsed
/// value: whole seconds, a whole number, on or off, or checked JSON.
fn routing_setting_value(key: &str, value: &str) -> Result<String, String> {
    let v = value.trim();
    if ROUTING_SWITCH_KEYS.contains(&key) {
        return match v {
            "on" | "off" => Ok(v.to_string()),
            _ => Err(format!("{key} must be on or off")),
        };
    }
    if key == ROUTING_SOURCES_KEY {
        return checked_routing_sources(v);
    }
    if key == ROUTING_LANE_MAP_KEY {
        return checked_routing_lane_map(v);
    }
    if key == ROUTING_TABLE_KEY {
        return checked_routing_path("the routing table file", v);
    }
    if key == ROUTING_DISPATCH_SCRIPTS_KEY {
        return checked_dispatch_scripts(v);
    }
    if key == ROUTING_RETRIES_KEY {
        if has_control(v) { return Err("retry times must be one line".to_string()); }
        let parsed: Vec<u64> = serde_json::from_str(v).map_err(|_| "retry times must be whole seconds in a JSON list".to_string())?;
        // None, one or two retries (an empty list: no retries, fix round 1, F7).
        if parsed.len() > 2 || parsed.iter().any(|n| *n == 0 || *n > 10 * 366 * 86_400)
            || parsed.windows(2).any(|w| w[0] >= w[1]) {
            return Err("retry times must be at most two increasing positive whole seconds".to_string());
        }
        let items: Vec<String> = parsed.iter().map(|n| n.to_string()).collect();
        return Ok(format!("[{}]", items.join(",")));
    }
    if ROUTING_NUMBER_KEYS.contains(&key) {
        // Digits with at most one decimal point: never a sign, an exponent,
        // "inf" or "NaN", so it can't be read as a flag or a special value.
        let plain = !v.is_empty()
            && v.len() <= 12
            && v.chars().all(|c| c.is_ascii_digit() || c == '.')
            && v.matches('.').count() <= 1
            && v.chars().any(|c| c.is_ascii_digit());
        let n: f64 = if plain { v.parse().unwrap_or(-1.0) } else { -1.0 };
        if !(n.is_finite() && (0.0..=100.0).contains(&n)) {
            return Err(format!("{key} must be a number from 0 to 100, got {value:?}"));
        }
        return Ok(n.to_string());
    }
    let n: u64 = v
        .parse()
        .map_err(|_| format!("{key} must be a whole number of 0 or more, got {value:?}"))?;
    let most: u64 = if ROUTING_DURATION_KEYS.contains(&key) { 10 * 366 * 86_400 } else { 100_000_000 };
    if n > most {
        return Err(format!("{key} is too large"));
    }
    Ok(n.to_string())
}

/// `expect` is the account list's version, required for `usage_accounts`
/// alone: a save made on a stale read is refused by the CLI (decision 15).
#[cfg(test)]
fn config_set_argv(key: &str, value: &str) -> Result<Vec<String>, String> {
    config_set_argv_expect(key, value, None)
}

#[cfg(test)]
fn config_set_argv_expect(key: &str, value: &str, expect: Option<&str>) -> Result<Vec<String>, String> {
    config_set_argv_kept(key, value, expect, &[])
}

/// `stored` is the account list saved now (`usage_accounts`'s `value` in
/// `khipu config`): entries kept as stored go through as they are.
fn config_set_argv_kept(key: &str, value: &str, expect: Option<&str>, stored: &[Value]) -> Result<Vec<String>, String> {
    if key == USAGE_ACCOUNTS_KEY {
        let mut argv = vec!["config".to_string(), "--from-app".to_string()];
        argv.push(format!("--expect={}", required_expect(expect, "the account list save")?));
        argv.extend(["--set".to_string(), USAGE_ACCOUNTS_KEY.to_string(), checked_usage_accounts_kept(value, stored)?]);
        return Ok(argv);
    }
    if expect.is_some() {
        return Err(format!("{key} takes no version to check"));
    }
    // A routing setting is the owner's (decision 15): the app's own save
    // says so, or the CLI refuses it as a conversation's.
    let set = |k: &str, v: String| {
        let mut argv = vec!["config".to_string()];
        if routing_key(k) {
            argv.push("--from-app".to_string());
        }
        argv.extend(["--set".to_string(), k.to_string(), v]);
        argv
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
        k if k.starts_with(COSINE_FLOOR_BY_PROFILE_PREFIX) => {
            checked_profile(&k[COSINE_FLOOR_BY_PROFILE_PREFIX.len()..])?;
            Ok(set(k, parse_unit_number(k, value, false)?))
        }
        "user_aliases" => Ok(set("user_aliases", checked_operand(key, value, true)?)),
        k if DAILY_CALL_KEYS.contains(&k) => Ok(set(k, parse_daily_calls(k, value)?)),
        "synth_fallback" => {
            let v = value.trim();
            if !SYNTH_FALLBACKS.contains(&v) {
                return Err(format!("synth_fallback must be gemini or none, got {value:?}"));
            }
            Ok(set("synth_fallback", v.to_string()))
        }
        GRAPH_BACKUP_REMOTE_KEY => Ok(set(GRAPH_BACKUP_REMOTE_KEY, checked_backup_remote(value)?)),
        k if PATH_KEYS.contains(&k) => Ok(set(k, checked_operand(key, value, false)?)),
        k if routing_key(k) => Ok(set(k, routing_setting_value(k, value)?)),
        _ => Err(format!("not a settable key: {key:?}")),
    }
}

fn config_unset_argv(key: &str) -> Result<Vec<String>, String> {
    config_unset_argv_expect(key, None)
}

fn config_unset_argv_expect(key: &str, expect: Option<&str>) -> Result<Vec<String>, String> {
    if key == USAGE_ACCOUNTS_KEY {
        let mut argv = vec!["config".to_string(), "--from-app".to_string()];
        argv.push(format!("--expect={}", required_expect(expect, "the account list save")?));
        argv.push(format!("--unset={key}"));
        return Ok(argv);
    }
    if expect.is_some() {
        return Err(format!("{key} takes no version to check"));
    }
    let by_profile = key
        .strip_prefix(COSINE_FLOOR_BY_PROFILE_PREFIX)
        .is_some_and(|id| checked_profile(id).is_ok());
    if !PATH_KEYS.contains(&key)
        && key != COSINE_FLOOR_KEY
        && key != USAGE_ACCOUNTS_KEY
        && key != GRAPH_BACKUP_REMOTE_KEY
        && !routing_key(key)
        && !by_profile
    {
        return Err(format!("not a resettable key: {key:?}"));
    }
    if routing_key(key) {
        return Ok(vec!["config".to_string(), "--from-app".to_string(), format!("--unset={key}")]);
    }
    Ok(vec!["config".to_string(), format!("--unset={key}")])
}

/// The owner's answer to a conversation's change to a routing setting.
fn config_request_argv(key: &str, action: &str) -> Result<Vec<String>, String> {
    if !routing_key(key) {
        return Err(format!("not a routing setting: {key:?}"));
    }
    if !matches!(action, "confirm" | "undo") {
        return Err(format!("not an answer to a request: {action:?}"));
    }
    Ok(vec!["config".to_string(), "--from-app".to_string(), "--answer-request".to_string(), key.to_string(),
            action.to_string()])
}

/// Embedding profile ids look like `gemini-embedding-2@768`; the CLI also
/// accepts `:` and `/` (an Ollama tag such as `nomic-embed-text:latest@768`).
/// The first character is alphanumeric, so an id can never read as a flag.
fn checked_profile(profile: &str) -> Result<&str, String> {
    let p = profile.trim();
    let ok = !p.is_empty()
        && p.len() <= 64
        && p.chars().next().is_some_and(|c| c.is_ascii_alphanumeric())
        && p.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '@' | '-' | ':' | '/'));
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
async fn khipu_config_set(key: String, value: String, expect: Option<String>) -> Result<String, String> {
    let mut stored = Vec::new();
    if key == USAGE_ACCOUNTS_KEY {
        // The list as saved now; the CLI refuses the save anyway when it
        // moved after `expect` was read.
        let raw = run_khipu_cli_async(fixed_argv(CONFIG_SHOW_ARGV)).await?;
        let config: Value = serde_json::from_str(raw.trim()).map_err(|_| "Khipu couldn't read the account list".to_string())?;
        if let Some(list) = config.pointer("/usage_accounts/value").and_then(Value::as_array) {
            stored = list.clone();
        }
    }
    run_khipu_cli_async(config_set_argv_kept(&key, &value, expect.as_deref(), &stored)?).await
}

#[tauri::command]
async fn khipu_config_unset(key: String, expect: Option<String>) -> Result<String, String> {
    run_khipu_cli_async(config_unset_argv_expect(&key, expect.as_deref())?).await
}

#[tauri::command]
async fn khipu_config_request(key: String, action: String) -> Result<String, String> {
    run_khipu_cli_async(config_request_argv(&key, &action)?).await
}

#[tauri::command]
async fn khipu_budget_status() -> Result<String, String> {
    run_khipu_cli_async(fixed_argv(BUDGET_STATUS_ARGV)).await
}

#[tauri::command]
async fn khipu_budget_lift_synth_today() -> Result<String, String> {
    run_khipu_cli_async(fixed_argv(BUDGET_LIFT_SYNTH_TODAY_ARGV)).await
}

#[tauri::command]
async fn khipu_usage_status() -> Result<String, String> {
    run_khipu_cli_async(fixed_argv(USAGE_STATUS_ARGV)).await
}

#[tauri::command]
async fn khipu_usage_refresh() -> Result<String, String> {
    run_khipu_cli_async(fixed_argv(USAGE_REFRESH_ARGV)).await
}

#[tauri::command]
async fn khipu_route_view() -> Result<String, String> {
    run_khipu_cli_async(fixed_argv(ROUTE_VIEW_ARGV)).await
}

// --- Settings -> Routing writes (routing truth phase 4, second half). The
// owner's changes to the routing record: pause, lift, clear or dismiss, the
// note, the standing rules and holds. Each is one fixed verb with
// `--from-app`, so the record stamps it `via: app` and doctor does not warn.
// Every field is checked here, a value that takes a flag travels joined to it
// (`--until=...`), and every free value travels after `--`, so nothing the
// webview sends can become a flag: not a value starting with `-`, and not a
// line break (refused in every one-line field). No shell is involved.

/// `--reason` values `khipu route hold` accepts.
const ROUTE_HOLD_REASONS: &[&str] = &["machine-busy", "disk-low", "keep", "outage", "other"];
/// The rule verbs the app offers (`khipu route rule ACTION`).
const ROUTE_RULE_ACTIONS: &[&str] = &["add", "edit", "retire", "restore", "confirm", "undo", "dismiss"];
/// The most owner's words the app hands the CLI in one change (a rule, the
/// note, a hold), in bytes of UTF-8. Not a limit on what the owner may say:
/// the CLI keeps a note and a rule at any length, and a hold up to the
/// owner's own `routing_hold_chars` (0: no limit). This only keeps one
/// argument well inside what the system lets a program be started with
/// (fix round 2, R2-4); Settings -> Routing says so.
const ROUTE_WORDS_MAX_BYTES: usize = 128 * 1024;

fn route_base(verb: &str) -> Vec<String> {
    vec!["route".to_string(), verb.to_string(), "--from-app".to_string()]
}

/// An account as the record names it: its key (`harness|login folder`) or
/// its label. One line, never starting with `-`.
fn checked_route_account(raw: &str) -> Result<String, String> {
    let v = raw.trim();
    if v.is_empty() {
        return Err("name the account".to_string());
    }
    if v.starts_with('-') || has_control(v) || v.chars().count() > 1100 {
        return Err("that is not an account Khipu knows".to_string());
    }
    Ok(v.to_string())
}

/// A report's or a hold's id (`o1a2b3c4d`, `h…`, a runner's `runner:…`).
fn checked_route_id(raw: &str) -> Result<String, String> {
    let v = raw.trim();
    let ok = !v.is_empty()
        && v.len() <= 128
        && v.chars().next().is_some_and(|c| c.is_ascii_alphanumeric())
        && v.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | ':' | '.' | '/' | '|'));
    if ok {
        Ok(v.to_string())
    } else {
        Err("that is not a report or hold Khipu knows".to_string())
    }
}

/// An end time the app computed: an ISO 8601 time with its offset, such as
/// `2026-10-16T09:00:00-04:00`. Digits first, so it can never read as a flag.
fn checked_route_until(raw: &str) -> Result<String, String> {
    let v = raw.trim();
    let ok = v.len() >= 16
        && v.len() <= 40
        && v.chars().next().is_some_and(|c| c.is_ascii_digit())
        && v.contains('T')
        && v.chars().all(|c| c.is_ascii_digit() || matches!(c, 'T' | 'Z' | ':' | '-' | '+' | '.'));
    if ok {
        Ok(v.to_string())
    } else {
        Err("the end must be a date and time".to_string())
    }
}

/// The owner's words: trimmed, not empty, at most `ROUTE_WORDS_MAX_BYTES`
/// (the size of one argument, not a length the owner chose), no control
/// characters except line breaks where `lines` allows them (the note). They
/// travel after `--`, so even `-x` stays words.
fn checked_route_words(raw: &str, lines: bool) -> Result<String, String> {
    let v = raw.trim();
    if v.is_empty() {
        return Err("write the words first".to_string());
    }
    if v.len() > ROUTE_WORDS_MAX_BYTES {
        return Err(format!(
            "these words are longer than the app can hand over in one change ({} KB of text); \
             shorten them",
            ROUTE_WORDS_MAX_BYTES / 1024
        ));
    }
    if v.chars().any(|c| c.is_control() && !(lines && (c == '\n' || c == '\t'))) {
        return Err(if lines {
            "the words hold a character Khipu can't keep".to_string()
        } else {
            "keep it to one line".to_string()
        });
    }
    Ok(v.to_string())
}

fn route_pause_argv(account: &str, until: Option<&str>) -> Result<Vec<String>, String> {
    let mut argv = route_base("pause");
    if let Some(t) = until.filter(|t| !t.trim().is_empty()) {
        argv.push(format!("--until={}", checked_route_until(t)?));
    }
    argv.push("--".to_string());
    argv.push(checked_route_account(account)?);
    Ok(argv)
}

fn route_lift_argv(account: &str) -> Result<Vec<String>, String> {
    let mut argv = route_base("lift");
    argv.push("--".to_string());
    argv.push(checked_route_account(account)?);
    Ok(argv)
}

/// Check one already configured account.  The CLI validates it against the
/// routing record before it starts its fixed usage probe.
fn route_check_argv(account: &str) -> Result<Vec<String>, String> {
    Ok(vec!["route".to_string(), "check".to_string(), "--".to_string(), checked_route_account(account)?])
}

/// Clear the account, or with `report` dismiss that one report.
fn route_clear_argv(account: &str, report: Option<&str>) -> Result<Vec<String>, String> {
    let mut argv = route_base("clear");
    if let Some(r) = report.filter(|r| !r.trim().is_empty()) {
        argv.push(format!("--report={}", checked_route_id(r)?));
    }
    argv.push("--".to_string());
    argv.push(checked_route_account(account)?);
    Ok(argv)
}

fn route_note_set_argv(text: &str) -> Result<Vec<String>, String> {
    let words = checked_route_words(text, true)?;
    let mut argv = route_base("note");
    argv.extend(["--".to_string(), "set".to_string(), words]);
    Ok(argv)
}

fn route_note_end_argv() -> Vec<String> {
    let mut argv = route_base("note");
    argv.extend(["--".to_string(), "end".to_string()]);
    argv
}

fn route_rule_argv(action: &str, n: Option<u32>, text: Option<&str>) -> Result<Vec<String>, String> {
    if !ROUTE_RULE_ACTIONS.contains(&action) {
        return Err(format!("not a rule change: {action:?}"));
    }
    let mut argv = route_base("rule");
    argv.extend(["--".to_string(), action.to_string()]);
    if action != "add" {
        let n = n.filter(|n| *n >= 1).ok_or_else(|| "name the rule's number".to_string())?;
        argv.push(n.to_string());
    }
    if action == "add" || action == "edit" {
        argv.push(checked_route_words(text.unwrap_or(""), false)?);
    }
    Ok(argv)
}

fn route_hold_add_argv(
    text: &str,
    reason: &str,
    scope: &str,
    account: Option<&str>,
    until: Option<&str>,
) -> Result<Vec<String>, String> {
    let words = checked_route_words(text, false)?;
    if !ROUTE_HOLD_REASONS.contains(&reason) {
        return Err(format!("not a kind of hold: {reason:?}"));
    }
    let mut argv = route_base("hold");
    argv.push(format!("--reason={reason}"));
    match scope {
        "all" => argv.push("--all".to_string()),
        "machine" => argv.push("--machine".to_string()),
        "account" => argv.push(format!("--account={}", checked_route_account(account.unwrap_or(""))?)),
        _ => return Err(format!("a hold is about all work, this Mac or one account, not {scope:?}")),
    }
    if let Some(t) = until.filter(|t| !t.trim().is_empty()) {
        argv.push(format!("--until={}", checked_route_until(t)?));
    }
    argv.extend(["--".to_string(), words]);
    Ok(argv)
}

fn route_hold_end_argv(id: &str) -> Result<Vec<String>, String> {
    let mut argv = route_base("hold");
    argv.extend(["--".to_string(), "end".to_string(), checked_route_id(id)?]);
    Ok(argv)
}

/// The owner's priority verbs (`khipu route priority ACTION`, routing truth
/// phase 4b).
const ROUTE_PRIORITY_ACTIONS: &[&str] = &["set", "confirm", "undo", "clear"];
/// The most entries one priority holds when read back (`routing.PRIORITY_MOST`).
const ROUTE_PRIORITY_MOST: usize = 64;

/// One account in the owner's priority as the webview sends it, in his order.
#[derive(serde::Deserialize, Debug, Clone)]
struct PriorityEntry {
    account: String,
    role: Option<String>,
    until: Option<String>,
}

/// `route priority --from-app [--entries=JSON] [--until=T] -- ACTION`. The
/// entries are rebuilt here from their checked fields alone (account, role,
/// end) into JSON that travels joined to `--entries=`, so no value the
/// webview sends can become a flag or an argument of its own; their order is
/// the owner's, kept as sent.
#[cfg(test)]
fn route_priority_argv(action: &str, entries: &[PriorityEntry], until: Option<&str>) -> Result<Vec<String>, String> {
    // The shape tests below are about the entries, not the version.
    route_priority_argv_expect(action, entries, until, Some("p1"))
}

/// `expect` is the priority's version as the owner read it (`--expect=V`,
/// before the `--`): a change made on a stale read is refused.
fn route_priority_argv_expect(
    action: &str,
    entries: &[PriorityEntry],
    until: Option<&str>,
    expect: Option<&str>,
) -> Result<Vec<String>, String> {
    if !ROUTE_PRIORITY_ACTIONS.contains(&action) {
        return Err(format!("not a change to the priority: {action:?}"));
    }
    let mut argv = route_base("priority");
    if action == "set" {
        if entries.is_empty() {
            return Err("put at least one account in the priority, or clear it".to_string());
        }
        if entries.len() > ROUTE_PRIORITY_MOST {
            return Err(format!("a priority holds at most {ROUTE_PRIORITY_MOST} accounts"));
        }
        let mut clean = Vec::with_capacity(entries.len());
        for e in entries {
            let mut item = serde_json::Map::new();
            item.insert("account".to_string(), Value::String(checked_route_account(&e.account)?));
            if let Some(role) = e.role.as_deref().filter(|r| !r.trim().is_empty()) {
                item.insert("role".to_string(), Value::String(checked_route_words(role, false)?));
            }
            if let Some(t) = e.until.as_deref().filter(|t| !t.trim().is_empty()) {
                item.insert("until".to_string(), Value::String(checked_route_until(t)?));
            }
            clean.push(Value::Object(item));
        }
        let json = Value::Array(clean).to_string();
        if json.len() > ROUTE_WORDS_MAX_BYTES {
            return Err("this priority is longer than the app can hand over in one change; shorten the roles".to_string());
        }
        argv.push(format!("--entries={json}"));
        if let Some(t) = until.filter(|t| !t.trim().is_empty()) {
            argv.push(format!("--until={}", checked_route_until(t)?));
        }
    }
    argv.push(format!("--expect={}", required_expect(expect, "the priority change")?));
    argv.extend(["--".to_string(), action.to_string()]);
    Ok(argv)
}

/// The version of the account list or the priority as the app read it: one
/// short token, so it can never read as a flag or carry a second argument.
fn checked_expect(raw: &str) -> Result<&str, String> {
    let v = raw.trim();
    if !v.is_empty() && v.len() <= 40 && v.chars().all(|c| c.is_ascii_alphanumeric()) {
        Ok(v)
    } else {
        Err("that is not a version Khipu knows; read the accounts again".to_string())
    }
}

/// A save that overwrites the account list or the priority must carry the
/// version it read, so a stale save can never replace a newer one.
fn required_expect<'a>(expect: Option<&'a str>, what: &str) -> Result<&'a str, String> {
    match expect.map(str::trim).filter(|e| !e.is_empty()) {
        Some(e) => checked_expect(e),
        None => Err(format!("{what} needs the version it read; reload and try again")),
    }
}

fn accounts_base(verb: &str) -> Vec<String> {
    vec!["accounts".to_string(), verb.to_string(), "--from-app".to_string()]
}

fn checked_harness(raw: &str) -> Result<String, String> {
    let h = raw.trim().to_lowercase();
    if USAGE_HARNESSES.contains(&h.as_str()) {
        Ok(h)
    } else {
        Err("harness must be claude, codex, grok or muse".to_string())
    }
}

/// An account key, `harness|login folder` (`claude|` for a default login).
fn checked_account_key(raw: &str) -> Result<String, String> {
    let v = raw.trim();
    let harness_ok = v.split_once('|').is_some_and(|(h, _)| USAGE_HARNESSES.contains(&h));
    if !harness_ok || v.starts_with('-') || has_control(v) || v.chars().count() > 1100 {
        return Err("that is not an account Khipu knows".to_string());
    }
    Ok(v.to_string())
}

/// A login folder: `~/...` or an absolute path, one line. Joined to its flag
/// with `=`, so it is never read as an option of its own.
fn checked_account_folder(raw: &str) -> Result<String, String> {
    let v = raw.trim();
    if !(v.starts_with("~/") || v.starts_with('/')) {
        return Err("a login folder starts with ~/ or is a full path starting with /".to_string());
    }
    if has_control(v) || v.chars().count() > 1024 {
        return Err("a login folder must be one line of plain text".to_string());
    }
    Ok(v.to_string())
}

fn checked_account_name(raw: &str) -> Result<String, String> {
    let v = raw.trim();
    if v.is_empty() {
        return Err("give the account a name".to_string());
    }
    if has_control(v) {
        return Err("a name must be one line of plain text".to_string());
    }
    if v.chars().count() > 64 {
        return Err("a name is at most 64 characters".to_string());
    }
    Ok(v.to_string())
}

/// One account as the webview sends it to Track (or as it showed a default).
#[derive(serde::Deserialize, Debug, Clone)]
struct AccountEntry {
    harness: String,
    home: Option<String>,
    label: Option<String>,
}

/// The entries rebuilt from checked fields into one JSON list. `home` and
/// `label` are left out when empty; the CLI decides the rest.
fn checked_account_entries(entries: &[AccountEntry]) -> Result<String, String> {
    if entries.len() > USAGE_ACCOUNTS_MAX {
        return Err(format!("Khipu tracks at most {USAGE_ACCOUNTS_MAX} accounts"));
    }
    let mut clean = Vec::with_capacity(entries.len());
    for e in entries {
        let mut item = serde_json::Map::new();
        item.insert("harness".to_string(), Value::String(checked_harness(&e.harness)?));
        if let Some(h) = e.home.as_deref().map(str::trim).filter(|h| !h.is_empty()) {
            item.insert("home".to_string(), Value::String(checked_account_folder(h)?));
        }
        if let Some(l) = e.label.as_deref().map(str::trim).filter(|l| !l.is_empty()) {
            item.insert("label".to_string(), Value::String(checked_account_name(l)?));
        }
        clean.push(Value::Object(item));
    }
    Ok(Value::Array(clean).to_string())
}

fn accounts_status_argv() -> Vec<String> {
    vec!["accounts".to_string(), "status".to_string()]
}

fn accounts_track_argv(
    entries: &[AccountEntry],
    expect: &str,
    shown_defaults: Option<&[AccountEntry]>,
    no_read: bool,
) -> Result<Vec<String>, String> {
    if entries.is_empty() {
        return Err("choose at least one account to track".to_string());
    }
    let mut argv = accounts_base("track");
    argv.push(format!("--expect={}", checked_expect(expect)?));
    argv.push(format!("--entries={}", checked_account_entries(entries)?));
    if let Some(d) = shown_defaults {
        argv.push(format!("--shown-defaults={}", checked_account_entries(d)?));
    }
    if no_read {
        argv.push("--no-read".to_string());
    }
    Ok(argv)
}

fn accounts_rename_argv(account: &str, name: &str, expect: &str) -> Result<Vec<String>, String> {
    let mut argv = accounts_base("rename");
    argv.push(format!("--expect={}", checked_expect(expect)?));
    argv.push(format!("--account={}", checked_account_key(account)?));
    argv.push(format!("--name={}", checked_account_name(name)?));
    Ok(argv)
}

/// The most holds one Stop tracking ends at once.
const ACCOUNTS_END_HOLDS_MOST: usize = 64;

fn accounts_stop_argv(
    account: &str,
    expect: &str,
    keep_priority: bool,
    keep_pause: bool,
    keep_lanes: bool,
    end_holds: &[String],
    confirm_last: bool,
) -> Result<Vec<String>, String> {
    if end_holds.len() > ACCOUNTS_END_HOLDS_MOST {
        return Err(format!("end at most {ACCOUNTS_END_HOLDS_MOST} holds at once"));
    }
    let mut argv = accounts_base("stop");
    argv.push(format!("--expect={}", checked_expect(expect)?));
    argv.push(format!("--account={}", checked_account_key(account)?));
    for (on, flag) in [
        (keep_priority, "--keep-priority"),
        (keep_pause, "--keep-pause"),
        (keep_lanes, "--keep-lanes"),
        (confirm_last, "--confirm-last"),
    ] {
        if on {
            argv.push(flag.to_string());
        }
    }
    for id in end_holds {
        argv.push(format!("--end-hold={}", checked_route_id(id)?));
    }
    Ok(argv)
}

fn accounts_move_argv(account: &str, folder: &str, expect: &str) -> Result<Vec<String>, String> {
    let mut argv = accounts_base("move");
    argv.push(format!("--expect={}", checked_expect(expect)?));
    argv.push(format!("--account={}", checked_account_key(account)?));
    argv.push(format!("--folder={}", checked_account_folder(folder)?));
    Ok(argv)
}

/// `decline` (Not this one) and `offer-again`: the CLI and, with no folder,
/// the default login.
fn accounts_offer_argv(verb: &str, harness: &str, folder: Option<&str>) -> Result<Vec<String>, String> {
    if !matches!(verb, "decline" | "offer-again") {
        return Err(format!("not a change to the offers: {verb:?}"));
    }
    let mut argv = accounts_base(verb);
    argv.push(format!("--harness={}", checked_harness(harness)?));
    if let Some(f) = folder.map(str::trim).filter(|f| !f.is_empty()) {
        argv.push(format!("--folder={}", checked_account_folder(f)?));
    }
    Ok(argv)
}

fn accounts_auto_track_argv(on: bool) -> Vec<String> {
    let mut argv = accounts_base("auto-track");
    argv.push(if on { "on" } else { "off" }.to_string());
    argv
}

/// `expect` is the list's version as the owner read it, required: his
/// answer never lands on a newer request than the one he saw.
fn accounts_request_argv(action: &str, expect: &str) -> Result<Vec<String>, String> {
    if !matches!(action, "confirm" | "undo") {
        return Err(format!("not an answer to a request: {action:?}"));
    }
    let mut argv = accounts_base("request");
    argv.push(format!("--expect={}", required_expect(Some(expect), "the answer to a request")?));
    argv.push(action.to_string());
    Ok(argv)
}

#[tauri::command]
async fn khipu_accounts_status() -> Result<String, String> {
    run_khipu_cli_async(accounts_status_argv()).await
}

#[tauri::command]
async fn khipu_accounts_track(
    entries: Vec<AccountEntry>,
    expect: String,
    shown_defaults: Option<Vec<AccountEntry>>,
    no_read: Option<bool>,
) -> Result<String, String> {
    run_khipu_cli_async(accounts_track_argv(&entries, &expect, shown_defaults.as_deref(), no_read.unwrap_or(false))?).await
}

#[tauri::command]
async fn khipu_accounts_rename(account: String, name: String, expect: String) -> Result<String, String> {
    run_khipu_cli_async(accounts_rename_argv(&account, &name, &expect)?).await
}

#[tauri::command]
async fn khipu_accounts_stop(
    account: String,
    expect: String,
    keep_priority: Option<bool>,
    keep_pause: Option<bool>,
    keep_lanes: Option<bool>,
    end_holds: Option<Vec<String>>,
    confirm_last: Option<bool>,
) -> Result<String, String> {
    run_khipu_cli_async(accounts_stop_argv(
        &account,
        &expect,
        keep_priority.unwrap_or(false),
        keep_pause.unwrap_or(false),
        keep_lanes.unwrap_or(false),
        end_holds.as_deref().unwrap_or(&[]),
        confirm_last.unwrap_or(false),
    )?)
    .await
}

#[tauri::command]
async fn khipu_accounts_move(account: String, folder: String, expect: String) -> Result<String, String> {
    run_khipu_cli_async(accounts_move_argv(&account, &folder, &expect)?).await
}

#[tauri::command]
async fn khipu_accounts_decline(harness: String, folder: Option<String>) -> Result<String, String> {
    run_khipu_cli_async(accounts_offer_argv("decline", &harness, folder.as_deref())?).await
}

#[tauri::command]
async fn khipu_accounts_offer_again(harness: String, folder: Option<String>) -> Result<String, String> {
    run_khipu_cli_async(accounts_offer_argv("offer-again", &harness, folder.as_deref())?).await
}

#[tauri::command]
async fn khipu_accounts_auto_track(on: bool) -> Result<String, String> {
    run_khipu_cli_async(accounts_auto_track_argv(on)).await
}

#[tauri::command]
async fn khipu_accounts_request(action: String, expect: String) -> Result<String, String> {
    run_khipu_cli_async(accounts_request_argv(&action, &expect)?).await
}

/// The CLIs whose own login the app can start (`khipu.usage.SIGN_IN_ARGS`):
/// each opens the owner's browser and asks nothing on a terminal.
const SIGN_IN_HARNESSES: &[&str] = &["claude", "codex"];

/// `~/x` as `<user home>/x`, for comparing one login folder written two ways.
fn expand_home(path: &str, user_home: Option<&str>) -> String {
    match (path.strip_prefix("~/"), user_home) {
        (Some(rest), Some(h)) => format!("{}/{}", h.trim_end_matches('/'), rest),
        _ => path.to_string(),
    }
}

/// `usage --sign-in --account=LABEL --account-key=HARNESS|FOLDER` for the
/// configured account that reads `harness` at `home` (None: the CLI's default
/// login). `config` is `khipu config`'s answer: the account must be one of its
/// `usage_accounts.accounts`, valid, and of a CLI the app can sign in; its
/// label and folder come from that list, never from the webview. The key is
/// the account's identity (`khipu.usage._key`), so two hand-edited accounts
/// sharing a label stay apart; the CLI resolves it again, refuses a label
/// that names more than one account, and builds the login's own argv itself.
fn sign_in_argv(config: &Value, harness: &str, home: Option<&str>) -> Result<Vec<String>, String> {
    if !SIGN_IN_HARNESSES.contains(&harness) {
        return Err("Khipu can't start this CLI's sign-in; sign in from the CLI itself".to_string());
    }
    let status = config.get("usage_accounts").ok_or("Khipu couldn't read the account list")?;
    let user_home = status.get("user_home").and_then(Value::as_str);
    let wanted = home.map(str::trim).filter(|h| !h.is_empty()).map(|h| expand_home(h, user_home));
    let accounts = status.get("accounts").and_then(Value::as_array).ok_or("Khipu couldn't read the account list")?;
    let found = accounts.iter().find(|a| {
        let same_cli = a.get("harness").and_then(Value::as_str) == Some(harness);
        let theirs = a
            .get("home")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|h| !h.is_empty())
            .map(|h| expand_home(h, user_home));
        same_cli && a.get("invalid").map_or(true, Value::is_null) && theirs == wanted
    });
    let account = found.ok_or("that login folder is not one of the accounts in Settings → Accounts and routing → Accounts")?;
    let label = account.get("label").and_then(Value::as_str).unwrap_or("").trim();
    if label.is_empty() || has_control(label) {
        return Err("that account has no label Khipu can name".to_string());
    }
    let folder = account.get("home").and_then(Value::as_str).map(str::trim).unwrap_or("");
    if has_control(folder) {
        return Err("that account has a login folder Khipu can't name".to_string());
    }
    Ok(vec![
        "usage".to_string(),
        "--sign-in".to_string(),
        format!("--account={label}"),
        format!("--account-key={harness}|{folder}"),
    ])
}

#[tauri::command]
async fn khipu_route_pause(account: String, until: Option<String>) -> Result<String, String> {
    run_khipu_cli_async(route_pause_argv(&account, until.as_deref())?).await
}

#[tauri::command]
async fn khipu_route_lift(account: String) -> Result<String, String> {
    run_khipu_cli_async(route_lift_argv(&account)?).await
}

#[tauri::command]
async fn khipu_route_check(account: String) -> Result<String, String> {
    run_khipu_cli_async(route_check_argv(&account)?).await
}

#[tauri::command]
async fn khipu_route_clear(account: String, report: Option<String>) -> Result<String, String> {
    run_khipu_cli_async(route_clear_argv(&account, report.as_deref())?).await
}

#[tauri::command]
async fn khipu_route_note_set(text: String) -> Result<String, String> {
    run_khipu_cli_async(route_note_set_argv(&text)?).await
}

#[tauri::command]
async fn khipu_route_note_end() -> Result<String, String> {
    run_khipu_cli_async(route_note_end_argv()).await
}

#[tauri::command]
async fn khipu_route_rule(action: String, n: Option<u32>, text: Option<String>) -> Result<String, String> {
    run_khipu_cli_async(route_rule_argv(&action, n, text.as_deref())?).await
}

#[tauri::command]
async fn khipu_route_hold_add(
    text: String,
    reason: String,
    scope: String,
    account: Option<String>,
    until: Option<String>,
) -> Result<String, String> {
    run_khipu_cli_async(route_hold_add_argv(&text, &reason, &scope, account.as_deref(), until.as_deref())?).await
}

#[tauri::command]
async fn khipu_route_hold_end(id: String) -> Result<String, String> {
    run_khipu_cli_async(route_hold_end_argv(&id)?).await
}

#[tauri::command]
async fn khipu_route_priority(
    action: String,
    entries: Option<Vec<PriorityEntry>>,
    until: Option<String>,
    expect: Option<String>,
) -> Result<String, String> {
    run_khipu_cli_async(
        route_priority_argv_expect(&action, entries.as_deref().unwrap_or(&[]), until.as_deref(), expect.as_deref())?,
    )
    .await
}

/// Sign in again: the CLI starts the account's own login (it opens the
/// browser) in its own session, waits for it to end and reads that account
/// again, so the answer is the account's new state. The account is checked
/// against the configured list read just now.
#[tauri::command]
async fn khipu_account_sign_in(harness: String, home: Option<String>) -> Result<String, String> {
    let raw = run_khipu_cli_async(fixed_argv(CONFIG_SHOW_ARGV)).await?;
    let config: Value = serde_json::from_str(raw.trim()).map_err(|_| "Khipu couldn't read the account list".to_string())?;
    run_khipu_cli_async(sign_in_argv(&config, &harness, home.as_deref())?).await
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

// --- Embeddings screen (docs/plans/2026-10-07-library-sources-byoe.md, "The
// user's view"). Every verb is a fixed argv built here from validated pieces;
// the webview names a command and passes ids, never argv. `embed` and
// `library` stay OUT of `ALLOWED_SUBCOMMANDS`, and so do the two job verbs
// below, which spawn detached and are written to a job file the screen reads.

const EMBED_PROVIDERS: &[&str] = &["gemini", "voyage", "openai-compatible"];
/// The only two top-level verbs `khipu_job_start` can spawn. Separate from
/// `SPAWN_ALLOWED_SUBCOMMANDS`, which stays as it was.
const JOB_SPAWN_VERBS: &[&str] = &["embed-backfill", "library-backfill"];
const EMBED_PROFILES_LIST_ARGV: &[&str] = &["embed", "profiles", "list"];
const EMBED_JOBS_ARGV: &[&str] = &["embed", "jobs"];
const EMBED_JOBS_CLEAR_ARGV: &[&str] = &["embed", "jobs", "--clear"];
const LIBRARY_LIST_ARGV: &[&str] = &["library", "list"];

/// Library names are `[a-z0-9_-]{1,40}` (`khipu.library.validate_name`).
fn checked_library(name: &str) -> Result<&str, String> {
    let n = name.trim();
    // Never a leading '-': the name is a positional argument and argparse would
    // read it as a flag (the CLI's own pattern would let it through).
    let ok = !n.is_empty()
        && n.len() <= 40
        && !n.starts_with('-')
        && n.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '_' | '-'));
    if ok {
        Ok(n)
    } else {
        Err(format!("not a library name (a-z, 0-9, _ and -, up to 40): {name:?}"))
    }
}

fn checked_provider(provider: &str) -> Result<&str, String> {
    let p = provider.trim();
    if EMBED_PROVIDERS.contains(&p) {
        Ok(p)
    } else {
        Err(format!("not an embedding provider: {provider:?}"))
    }
}

fn checked_dim(dim: i64) -> Result<i64, String> {
    if (1..=8192).contains(&dim) {
        Ok(dim)
    } else {
        Err(format!("dimensions must be between 1 and 8192, got {dim}"))
    }
}

/// A model id as providers spell them (`voyage-3`, `nomic-embed-text:latest`).
fn checked_model(model: &str) -> Result<&str, String> {
    let m = model.trim();
    let ok = !m.is_empty()
        && m.len() <= 100
        && m.chars().next().is_some_and(|c| c.is_ascii_alphanumeric())
        && m.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-' | ':' | '/'));
    if ok {
        Ok(m)
    } else {
        Err(format!("not a model id: {model:?}"))
    }
}

/// `https://...`, or plain `http://` only for localhost / 127.0.0.1.
fn checked_endpoint(endpoint: &str) -> Result<&str, String> {
    let e = endpoint.trim();
    if e.len() > 2048 || e.chars().any(|c| c.is_control() || c.is_whitespace()) {
        return Err("endpoint must be one line with no spaces".to_string());
    }
    let local = ["http://localhost", "http://127.0.0.1"].iter().any(|pre| {
        e.strip_prefix(pre)
            .is_some_and(|rest| rest.is_empty() || rest.starts_with(':') || rest.starts_with('/'))
    });
    if (e.starts_with("https://") && e.len() > "https://".len()) || local {
        Ok(e)
    } else {
        Err("endpoint must start with https:// (http:// is allowed only for localhost or 127.0.0.1)".to_string())
    }
}

fn checked_space(space: &str) -> Result<String, String> {
    let s = space.trim();
    if s == "memory" {
        return Ok(s.to_string());
    }
    match s.strip_prefix("library:") {
        Some(name) => Ok(format!("library:{}", checked_library(name)?)),
        None => Err(format!("space must be memory or library:<name>, got {space:?}")),
    }
}

/// A folder or file path: non-empty, no control characters, never a flag.
fn checked_path(key: &str, raw: &str) -> Result<String, String> {
    checked_operand(key, raw, false)
}

fn embed_estimate_argv(profile: &str, space: &str, stale: bool) -> Result<Vec<String>, String> {
    let mut a = vec![
        "embed".to_string(),
        "estimate".to_string(),
        format!("--profile={}", checked_profile(profile)?),
        format!("--space={}", checked_space(space)?),
    ];
    if stale {
        a.push("--stale".to_string());
    }
    Ok(a)
}

fn embed_test_key_argv(
    provider: &str,
    endpoint: Option<&str>,
    model: Option<&str>,
) -> Result<Vec<String>, String> {
    let provider = checked_provider(provider)?;
    let mut a = vec![
        "embed".to_string(),
        "test-key".to_string(),
        format!("--provider={provider}"),
    ];
    if let Some(e) = endpoint.filter(|e| !e.trim().is_empty()) {
        if provider != "openai-compatible" {
            return Err("only an openai-compatible model has an endpoint".to_string());
        }
        a.push(format!("--endpoint={}", checked_endpoint(e)?));
    }
    if let Some(m) = model.filter(|m| !m.trim().is_empty()) {
        a.push(format!("--model={}", checked_model(m)?));
    }
    Ok(a)
}

fn embed_profile_add_argv(
    id: &str,
    provider: &str,
    model: &str,
    dim: i64,
    endpoint: Option<&str>,
) -> Result<Vec<String>, String> {
    let id = checked_profile(id)?;
    let provider = checked_provider(provider)?;
    let model = checked_model(model)?;
    let dim = checked_dim(dim)?;
    if id != format!("{model}@{dim}") {
        return Err(format!("profile id must be {model}@{dim}, got {id:?}"));
    }
    let mut a = vec![
        "embed".to_string(),
        "profiles".to_string(),
        "add".to_string(),
        id.to_string(),
        format!("--provider={provider}"),
        format!("--model={model}"),
        format!("--dim={dim}"),
    ];
    match endpoint.filter(|e| !e.trim().is_empty()) {
        Some(e) => {
            if provider != "openai-compatible" {
                return Err("only an openai-compatible model has an endpoint".to_string());
            }
            a.push(format!("--endpoint={}", checked_endpoint(e)?));
        }
        None if provider == "openai-compatible" => {
            return Err("an openai-compatible model needs an endpoint".to_string());
        }
        None => {}
    }
    Ok(a)
}

/// Always `--yes`: the screen asks first, and the CLI refuses a profile that
/// is in use.
fn embed_profile_delete_argv(id: &str) -> Result<Vec<String>, String> {
    Ok(vec![
        "embed".to_string(),
        "profiles".to_string(),
        "delete".to_string(),
        checked_profile(id)?.to_string(),
        "--yes".to_string(),
    ])
}

fn library_argv(verb: &str, name: &str) -> Result<Vec<String>, String> {
    if !["status", "scan", "enable", "disable"].contains(&verb) {
        return Err(format!("not a library verb: {verb:?}"));
    }
    Ok(vec!["library".to_string(), verb.to_string(), checked_library(name)?.to_string()])
}

/// `library set-profile NAME PROFILE`: the CLI refuses unless PROFILE covers
/// every chunk of the library, so there is no force flag to forward.
fn library_set_profile_argv(name: &str, profile: &str) -> Result<Vec<String>, String> {
    Ok(vec![
        "library".to_string(),
        "set-profile".to_string(),
        checked_library(name)?.to_string(),
        checked_profile(profile)?.to_string(),
    ])
}

fn library_add_argv(name: &str, root: &str, profile: &str) -> Result<Vec<String>, String> {
    Ok(vec![
        "library".to_string(),
        "add".to_string(),
        checked_library(name)?.to_string(),
        format!("--root={}", checked_path("folder", root)?),
        format!("--profile={}", checked_profile(profile)?),
    ])
}

fn library_remove_argv(name: &str) -> Result<Vec<String>, String> {
    Ok(vec![
        "library".to_string(),
        "remove".to_string(),
        checked_library(name)?.to_string(),
        "--yes".to_string(),
    ])
}

fn library_import_argv(
    name: &str,
    path: &str,
    strip_prefix: Option<&str>,
    profile: Option<&str>,
) -> Result<Vec<String>, String> {
    let mut a = vec![
        "library".to_string(),
        "import".to_string(),
        checked_library(name)?.to_string(),
        checked_path("index file", path)?,
    ];
    if let Some(p) = strip_prefix.filter(|p| !p.is_empty()) {
        // Not trimmed: a trailing slash is part of the prefix.
        if has_control(p) || p.len() > 4096 {
            return Err("strip prefix must be one line".to_string());
        }
        a.push(format!("--strip-prefix={p}"));
    }
    if let Some(p) = profile.filter(|p| !p.trim().is_empty()) {
        a.push(format!("--profile={}", checked_profile(p)?));
    }
    Ok(a)
}

/// A job id the app makes up for itself: `app-<unix seconds>-<8 hex>`. It
/// matches the CLI's `[A-Za-z0-9][A-Za-z0-9._-]{0,79}` and is never taken from
/// the webview.
fn new_job_id() -> String {
    use std::hash::{BuildHasher, Hasher};
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let mut h = std::collections::hash_map::RandomState::new().build_hasher();
    h.write_u128(secs);
    h.write_u32(std::process::id());
    format!("app-{}-{:08x}", secs / 1_000_000_000, h.finish() as u32)
}

fn job_id_ok(id: &str) -> bool {
    let mut chars = id.chars();
    id.len() <= 80
        && chars.next().is_some_and(|c| c.is_ascii_alphanumeric())
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}

/// argv for a detached backfill. `kind` is `memory` (needs `profile`) or
/// `library` (needs `library`; `profile` re-embeds under another model).
fn job_start_argv(
    kind: &str,
    profile: Option<&str>,
    library: Option<&str>,
    stale: bool,
    job_id: &str,
) -> Result<Vec<String>, String> {
    if !job_id_ok(job_id) {
        return Err(format!("not a job id: {job_id:?}"));
    }
    let profile = profile.map(str::trim).filter(|p| !p.is_empty());
    let mut a: Vec<String>;
    match kind {
        "memory" => {
            let p = profile.ok_or_else(|| "a memory job needs a profile".to_string())?;
            a = vec![JOB_SPAWN_VERBS[0].to_string(), format!("--profile={}", checked_profile(p)?)];
        }
        "library" => {
            let name = library.ok_or_else(|| "a library job needs a library".to_string())?;
            a = vec![JOB_SPAWN_VERBS[1].to_string(), checked_library(name)?.to_string()];
            if let Some(p) = profile {
                a.push(format!("--profile={}", checked_profile(p)?));
            }
            if stale {
                a.push("--stale".to_string());
            }
        }
        other => return Err(format!("not a job kind: {other:?}")),
    }
    a.push(format!("--job-id={job_id}"));
    Ok(a)
}

/// Where job files live: `<data dir>/jobs`, the same folder
/// `khipu.embed_jobs.jobs_dir()` writes to.
fn jobs_dir_path() -> PathBuf {
    if let Some(d) = data_dir_from_pointer() {
        return d.join("jobs");
    }
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    PathBuf::from(home).join(".config/khipu/jobs")
}

/// The pid to stop for `job_id`, from that job's file: only a file that names
/// this job id and says `running` yields a pid.
fn cancel_target(raw_job_file: &str, job_id: &str) -> Result<u32, String> {
    if !job_id_ok(job_id) {
        return Err(format!("not a job id: {job_id:?}"));
    }
    let v: Value = serde_json::from_str(raw_job_file).map_err(|_| "the job file is unreadable".to_string())?;
    if v.get("job").and_then(Value::as_str) != Some(job_id) {
        return Err("the job file names a different job".to_string());
    }
    if v.get("state").and_then(Value::as_str) != Some("running") {
        return Err("that job is not running".to_string());
    }
    match v.get("pid").and_then(Value::as_u64) {
        Some(pid) if pid > 1 && pid <= u32::MAX as u64 => Ok(pid as u32),
        _ => Err("the job file has no process id".to_string()),
    }
}

fn process_is_current_job(command: &str, job_id: &str) -> bool {
    let args: Vec<&str> = command.split_whitespace().collect();
    let expected = format!("--job-id={job_id}");
    args.iter().any(|arg| *arg == "khipu")
        && (args.iter().any(|arg| *arg == "embed-backfill" || *arg == "library-backfill")
            || args.windows(2).any(|pair| pair == ["embed", "backfill"] || pair == ["library", "backfill"]))
        && (args.iter().any(|arg| *arg == expected.as_str()) || args.windows(2).any(|pair| pair == ["--job-id", job_id]))
}

fn cancel_job_sync(job_id: &str) -> Result<Value, String> {
    let path = jobs_dir_path().join(format!("{job_id}.json"));
    if !job_id_ok(job_id) {
        return Err(format!("not a job id: {job_id:?}"));
    }
    let raw = std::fs::read_to_string(&path).map_err(|e| format!("no job file for {job_id}: {e}"))?;
    let pid = cancel_target(&raw, job_id)?;
    // A pid can be reused after a crash: only stop a process that is a khipu run.
    let cmd = Command::new("/bin/ps")
        .args(["-o", "command=", "-p", &pid.to_string()])
        .output()
        .map_err(|e| format!("could not check process {pid}: {e}"))?;
    let cmdline = String::from_utf8_lossy(&cmd.stdout).to_string();
    if !cmd.status.success() || !process_is_current_job(&cmdline, job_id) {
        return Err("that job's process is no longer running".to_string());
    }
    let out = Command::new("/bin/kill")
        .args(["-TERM", &pid.to_string()])
        .output()
        .map_err(|e| format!("could not stop process {pid}: {e}"))?;
    if !out.status.success() {
        return Err(format!("could not stop process {pid}"));
    }
    Ok(serde_json::json!({ "ok": true, "job": job_id, "pid": pid }))
}

#[tauri::command]
async fn khipu_embed_profiles_list() -> Result<String, String> {
    run_khipu_cli_async(fixed_argv(EMBED_PROFILES_LIST_ARGV)).await
}

#[tauri::command]
async fn khipu_embed_profile_add(
    id: String,
    provider: String,
    model: String,
    dim: i64,
    endpoint: Option<String>,
) -> Result<String, String> {
    run_khipu_cli_async(embed_profile_add_argv(&id, &provider, &model, dim, endpoint.as_deref())?).await
}

#[tauri::command]
async fn khipu_embed_profile_delete(id: String) -> Result<String, String> {
    run_khipu_cli_async(embed_profile_delete_argv(&id)?).await
}

#[tauri::command]
async fn khipu_embed_estimate(profile: String, space: String, stale: bool) -> Result<String, String> {
    run_khipu_cli_async(embed_estimate_argv(&profile, &space, stale)?).await
}

/// The key is read from the Keychain by the CLI itself (`embed test-key`), so
/// no secret travels here, in argv or in env.
#[tauri::command]
async fn khipu_embed_test_key(
    provider: String,
    endpoint: Option<String>,
    model: Option<String>,
) -> Result<String, String> {
    run_khipu_cli_async(embed_test_key_argv(&provider, endpoint.as_deref(), model.as_deref())?).await
}

#[tauri::command]
async fn khipu_embed_jobs() -> Result<String, String> {
    run_khipu_cli_async(fixed_argv(EMBED_JOBS_ARGV)).await
}

#[tauri::command]
async fn khipu_embed_jobs_clear() -> Result<String, String> {
    run_khipu_cli_async(fixed_argv(EMBED_JOBS_CLEAR_ARGV)).await
}

#[tauri::command]
async fn khipu_job_start(
    kind: String,
    profile: Option<String>,
    library: Option<String>,
    stale: bool,
) -> Result<Value, String> {
    let job_id = new_job_id();
    let args = job_start_argv(&kind, profile.as_deref(), library.as_deref(), stale, &job_id)?;
    spawn_blocking_cli(move || {
        let (pid, log_path) = spawn_detached_cli(&args, &args[0])?;
        Ok(serde_json::json!({
            "ok": true,
            "job_id": job_id,
            "pid": pid,
            "log_path": log_path.to_string_lossy(),
        }))
    })
    .await
}

#[tauri::command]
async fn khipu_job_cancel(job_id: String) -> Result<Value, String> {
    spawn_blocking_cli(move || cancel_job_sync(&job_id)).await
}

#[tauri::command]
async fn khipu_library_list() -> Result<String, String> {
    run_khipu_cli_async(fixed_argv(LIBRARY_LIST_ARGV)).await
}

#[tauri::command]
async fn khipu_library_status(name: String) -> Result<String, String> {
    run_khipu_cli_async(library_argv("status", &name)?).await
}

#[tauri::command]
async fn khipu_library_add(name: String, root: String, profile: String) -> Result<String, String> {
    run_khipu_cli_async(library_add_argv(&name, &root, &profile)?).await
}

#[tauri::command]
async fn khipu_library_remove(name: String) -> Result<String, String> {
    run_khipu_cli_async(library_remove_argv(&name)?).await
}

#[tauri::command]
async fn khipu_library_enable(name: String) -> Result<String, String> {
    run_khipu_cli_async(library_argv("enable", &name)?).await
}

#[tauri::command]
async fn khipu_library_disable(name: String) -> Result<String, String> {
    run_khipu_cli_async(library_argv("disable", &name)?).await
}

#[tauri::command]
async fn khipu_library_scan(name: String) -> Result<String, String> {
    run_khipu_cli_async(library_argv("scan", &name)?).await
}

#[tauri::command]
async fn khipu_library_set_profile(name: String, profile: String) -> Result<String, String> {
    run_khipu_cli_async(library_set_profile_argv(&name, &profile)?).await
}

#[tauri::command]
async fn khipu_library_import(
    name: String,
    path: String,
    strip_prefix: Option<String>,
    profile: Option<String>,
) -> Result<String, String> {
    run_khipu_cli_async(library_import_argv(&name, &path, strip_prefix.as_deref(), profile.as_deref())?).await
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
const SETTABLE_SECRETS: &[&str] = &[
    "gemini_api_key",
    "anthropic_api_key",
    "openai_api_key",
    "openrouter_api_key",
    "database_url",
    "openai_compat_api_key",
    "voyage_api_key",
];

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

/// Export a join kit to disk. The passphrase travels via env, never argv —
/// same exposure model as `set_khipu_secret`'s stdin pipe. The CLI refuses an
/// empty passphrase unless `allow_unencrypted` (the owner's explicit opt-out).
#[tauri::command]
async fn join_export(
    passphrase: String,
    out_path: String,
    allow_unencrypted: Option<bool>,
) -> Result<String, String> {
    spawn_blocking_cli(move || {
        join_export_sync(passphrase, out_path, allow_unencrypted.unwrap_or(false))
    })
    .await
}

fn join_export_sync(
    passphrase: String,
    out_path: String,
    allow_unencrypted: bool,
) -> Result<String, String> {
    if out_path.trim().is_empty() {
        return Err("out_path is empty".to_string());
    }
    let root = khipu_root()?;
    let py = khipu_python()?;
    let pythonpath = khipu_pythonpath(&root);
    let (bc_key, bc_val) = khipu_bytecode_env();
    let mut cmd = Command::new(&py);
    cmd.arg("-m")
        .arg("khipu")
        .arg("join")
        .arg("export")
        .arg("--out")
        .arg(out_path.trim());
    if allow_unencrypted {
        cmd.arg("--allow-unencrypted");
    }
    let output = cmd
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
async fn join_advertise(
    passphrase: String,
    timeout: u32,
    allow_unencrypted: Option<bool>,
) -> Result<String, String> {
    spawn_blocking_cli(move || {
        join_advertise_sync(passphrase, timeout, allow_unencrypted.unwrap_or(false))
    })
    .await
}

fn join_advertise_sync(
    passphrase: String,
    timeout: u32,
    allow_unencrypted: bool,
) -> Result<String, String> {
    let root = khipu_root()?;
    let py = khipu_python()?;
    let pythonpath = khipu_pythonpath(&root);
    let (bc_key, bc_val) = khipu_bytecode_env();
    let tout = timeout.max(60);
    let mut cmd = Command::new(&py);
    cmd.arg("-m")
        .arg("khipu")
        .arg("join")
        .arg("advertise")
        .arg("--timeout")
        .arg(tout.to_string());
    if allow_unencrypted {
        cmd.arg("--allow-unencrypted");
    }
    let mut child = cmd
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
    let (pid, log_path) = spawn_detached_cli(&[subcommand.clone()], &subcommand)?;
    let engine_log_path = engine_job_log_path(&subcommand);
    Ok(serde_json::json!({
        "ok": true,
        "pid": pid,
        "log_path": log_path.to_string_lossy(),
        "engine_log_path": engine_log_path.to_string_lossy(),
        "subcommand": subcommand,
    }))
}

/// Start `khipu <args>` detached: stdout and stderr go to a log file next to
/// the data folder's `dsn`, the child is reaped in the background, and the
/// caller gets the pid and the log path back immediately. `args` must already
/// be validated (fixed argv built in Rust); `label` only names the log file.
fn spawn_detached_cli(args: &[String], label: &str) -> Result<(u32, PathBuf), String> {
    let root = khipu_root()?;
    let py = khipu_python()?;
    let pythonpath = khipu_pythonpath(&root);
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let log_path = dirs_fallback_dsn()
        .parent()
        .map(|p| p.join(format!("khipu-job-{label}-{stamp}.log")))
        .unwrap_or_else(|| PathBuf::from(format!("/tmp/khipu-job-{label}-{stamp}.log")));
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
        .args(args)
        .env("PYTHONPATH", &pythonpath)
        .env("KHIPU_ROOT", &root)
        .env("KHIPU_APP_VERSION", khipu_app_version())
        .env(bc_key, &bc_val)
        .stdout(Stdio::from(log_file))
        .stderr(Stdio::from(err_file))
        .spawn()
        .map_err(|e| format!("spawn khipu {label} failed ({py:?}): {e}"))?;
    let pid = child.id();
    // Reap in the background so the Unix zombie does not linger until Tauri
    // exits. Must not `.wait()` / `.output()` on this thread (C8 fire-and-forget).
    std::thread::spawn(move || {
        if let Err(e) = child.wait() {
            eprintln!("[khipu] wait on spawned job failed: {e}");
        }
    });
    Ok((pid, log_path))
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
    if keychain_services_from_env().iter().any(|service| keychain_has_dsn(service)) {
        return true;
    }
    dirs_fallback_dsn().is_file()
}

fn keychain_services(service: Option<&str>, enabled: bool) -> Vec<String> {
    if !enabled { return Vec::new(); }
    let configured = service.unwrap_or("Khipu");
    let mut services = vec![configured.to_string()];
    if configured != "Alzy" { services.push("Alzy".to_string()); }
    services
}

fn keychain_services_from_env() -> Vec<String> {
    let enabled = !env_raw_first(&["KHIPU_KEYCHAIN", "ALZY_KEYCHAIN"])
        .is_some_and(|raw| matches!(raw.trim().to_ascii_lowercase().as_str(), "0" | "false" | "off"));
    let service = match std::env::var("KHIPU_KEYCHAIN_SERVICE") {
        Ok(value) if !value.is_empty() => Some(value),
        _ => std::env::var("ALZY_KEYCHAIN_SERVICE").ok(),
    };
    keychain_services(service.as_deref(), enabled)
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
    expand_home_path(path)
}

fn expand_home_path(raw: &str) -> Option<PathBuf> {
    let path = raw.trim();
    if path == "~" { return std::env::var("HOME").ok().map(PathBuf::from); }
    if let Some(rest) = path.strip_prefix("~/") {
        return std::env::var("HOME").ok().map(|home| PathBuf::from(home).join(rest));
    }
    (!path.is_empty()).then(|| PathBuf::from(path))
}

fn data_dir_from_pointer() -> Option<PathBuf> {
    if let Some(p) = env_first(&["KHIPU_DATA_DIR", "ALZY_DATA_DIR"]) {
        return expand_home_path(&p);
    }
    let home = std::env::var("HOME").ok()?;
    let ptr = PathBuf::from(&home).join(".config/khipu/data_root.json");
    data_dir_from_pointer_json(&std::fs::read_to_string(ptr).ok()?)
}

fn dirs_fallback_dsn() -> PathBuf {
    if let Some(p) = env_first(&["KHIPU_DSN_FILE", "ALZY_DSN_FILE"]) {
        return expand_home_path(&p).unwrap_or_else(|| PathBuf::from(p));
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

fn approve_feedback_attachment_for_session(path: &std::path::Path) -> Result<PathBuf, String> {
    let canonical = path.canonicalize().map_err(|e| format!("attachment not found: {}: {e}", path.display()))?;
    if !canonical.is_file() { return Err(format!("attachment not found: {}", canonical.display())); }
    FEEDBACK_SELECTED_PATHS.lock().map_err(|_| "attachment selection registry unavailable".to_string())?.insert(canonical.clone());
    Ok(canonical)
}

fn forget_feedback_attachment_for_session(path: &std::path::Path) {
    if let Ok(canonical) = path.canonicalize() {
        if let Ok(mut selected) = FEEDBACK_SELECTED_PATHS.lock() { selected.remove(&canonical); }
    }
}

fn approved_feedback_attachment(path: &std::path::Path) -> Result<PathBuf, String> {
    let canonical = path.canonicalize().map_err(|_| "attachment was not selected in this app session".to_string())?;
    if FEEDBACK_SELECTED_PATHS.lock().map(|paths| paths.contains(&canonical)).unwrap_or(false) { Ok(canonical) }
    else { Err("attachment was not selected in this app session".to_string()) }
}

#[tauri::command]
async fn select_feedback_attachments(app: AppHandle) -> Result<Vec<String>, String> {
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    app.dialog().file().pick_files(move |selected| { let _ = tx.send(selected); });
    let selected = tauri::async_runtime::spawn_blocking(move || rx.recv()).await
        .map_err(|e| format!("attachment picker task failed: {e}"))?
        .map_err(|_| "attachment picker closed unexpectedly".to_string())?
        .unwrap_or_default();
    selected.into_iter().map(|file: FilePath| {
        let path = file.into_path().map_err(|_| "attachment picker returned a non-file path".to_string())?;
        approve_feedback_attachment_for_session(&path).map(|path| path.display().to_string())
    }).collect()
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
        let submitted = PathBuf::from(raw.trim());
        if raw.trim().is_empty() {
            continue;
        }
        let path = approved_feedback_attachment(&submitted)?;
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
            let Some(backup) = v.get("backup_ok").and_then(|x| x.as_bool()) else { return "Khipu · doctor unavailable".into(); };
            let Some(drift) = v.get("drift_ok").and_then(|x| x.as_bool()) else { return "Khipu · doctor unavailable".into(); };
            let Some(recording) = v.get("capture_liveness_ok").and_then(|x| x.as_bool()) else { return "Khipu · doctor unavailable".into(); };
            let Some(git_sync) = v.get("git_sync_ok").and_then(|x| x.as_bool()) else { return "Khipu · doctor unavailable".into(); };
            let eps = v
                .pointer("/status/counts/episodes")
                .and_then(|x| x.as_u64())
                .unwrap_or(0);
            if ok && backup && drift && recording && git_sync {
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
                format!("Khipu · doctor unavailable · status only · {eps} episodes")
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
            khipu_config_request,
            khipu_config_unset,
            khipu_budget_status,
            khipu_budget_lift_synth_today,
            khipu_usage_status,
            khipu_usage_refresh,
            khipu_route_view,
            khipu_route_pause,
            khipu_route_lift,
            khipu_route_check,
            khipu_route_clear,
            khipu_route_note_set,
            khipu_route_note_end,
            khipu_route_rule,
            khipu_route_hold_add,
            khipu_route_hold_end,
            khipu_route_priority,
            khipu_account_sign_in,
            khipu_accounts_status,
            khipu_accounts_track,
            khipu_accounts_rename,
            khipu_accounts_stop,
            khipu_accounts_move,
            khipu_accounts_decline,
            khipu_accounts_offer_again,
            khipu_accounts_auto_track,
            khipu_accounts_request,
            khipu_jobs_status,
            khipu_job_install,
            khipu_job_uninstall,
            khipu_recall_status,
            khipu_gateway_token_status,
            khipu_gateway_token_set,
            khipu_embed_status,
            khipu_embed_activate,
            khipu_embed_profiles_list,
            khipu_embed_profile_add,
            khipu_embed_profile_delete,
            khipu_embed_estimate,
            khipu_embed_test_key,
            khipu_embed_jobs,
            khipu_embed_jobs_clear,
            khipu_job_start,
            khipu_job_cancel,
            khipu_library_list,
            khipu_library_status,
            khipu_library_add,
            khipu_library_remove,
            khipu_library_enable,
            khipu_library_disable,
            khipu_library_scan,
            khipu_library_import,
            khipu_library_set_profile,
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
            select_feedback_attachments,
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
    fn missing_required_health_evidence_is_unavailable() {
        let raw = format!("{BASE},\"ok\":true,\"capture_liveness_ok\":true}}");
        assert_eq!(tray_tooltip_from_doctor(&raw), "Khipu · doctor unavailable");
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
        assert_eq!(
            SETTABLE_SECRETS,
            &[
                "gemini_api_key",
                "anthropic_api_key",
                "openai_api_key",
                "openrouter_api_key",
                "database_url",
                "openai_compat_api_key",
                "voyage_api_key",
            ]
        );
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
        for s in ["features", "config", "jobs", "recall", "gateway", "embed", "budget", "usage", "route"] {
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
        assert_eq!(fixed_argv(BUDGET_STATUS_ARGV), v(&["budget", "status"]));
        assert_eq!(fixed_argv(BUDGET_LIFT_SYNTH_TODAY_ARGV), v(&["budget", "lift", "synth", "--today"]));
    }

    #[test]
    fn plan_usage_commands_are_fixed_argv() {
        assert_eq!(fixed_argv(USAGE_STATUS_ARGV), v(&["usage", "--json"]));
        assert_eq!(fixed_argv(USAGE_REFRESH_ARGV), v(&["usage", "--fresh", "--json"]));
        // Neither reaches --max-age, and `usage` stays out of the generic allowlist.
        for argv in [USAGE_STATUS_ARGV, USAGE_REFRESH_ARGV] {
            assert!(!argv.iter().any(|a| a.starts_with("--max-age")));
        }
        assert!(!ALLOWED_SUBCOMMANDS.contains(&"usage"));
    }

    #[test]
    fn routing_owner_writes_are_fixed_verbs_marked_from_the_app() {
        let acct = "claude|/logins/claude-second";
        assert_eq!(
            route_pause_argv(acct, Some("2026-10-16T09:00:00-04:00")).unwrap(),
            v(&["route", "pause", "--from-app", "--until=2026-10-16T09:00:00-04:00", "--", acct])
        );
        assert_eq!(route_pause_argv("Codex", None).unwrap(), v(&["route", "pause", "--from-app", "--", "Codex"]));
        assert_eq!(route_lift_argv("Codex").unwrap(), v(&["route", "lift", "--from-app", "--", "Codex"]));
        assert_eq!(route_check_argv("Codex").unwrap(), v(&["route", "check", "--", "Codex"]));
        assert_eq!(route_clear_argv("Codex", None).unwrap(), v(&["route", "clear", "--from-app", "--", "Codex"]));
        assert_eq!(
            route_clear_argv("Codex", Some("e1a2b3c4")).unwrap(),
            v(&["route", "clear", "--from-app", "--report=e1a2b3c4", "--", "Codex"])
        );
        assert_eq!(
            route_note_set_argv("Codex first.\nThen Grok.").unwrap(),
            v(&["route", "note", "--from-app", "--", "set", "Codex first.\nThen Grok."])
        );
        assert_eq!(route_note_end_argv(), v(&["route", "note", "--from-app", "--", "end"]));
        assert_eq!(
            route_rule_argv("add", None, Some("Never a -fast tier.")).unwrap(),
            v(&["route", "rule", "--from-app", "--", "add", "Never a -fast tier."])
        );
        assert_eq!(
            route_rule_argv("edit", Some(3), Some("Never a fast tier.")).unwrap(),
            v(&["route", "rule", "--from-app", "--", "edit", "3", "Never a fast tier."])
        );
        for action in ["retire", "restore", "confirm", "undo", "dismiss"] {
            assert_eq!(
                route_rule_argv(action, Some(6), None).unwrap(),
                v(&["route", "rule", "--from-app", "--", action, "6"])
            );
        }
        assert_eq!(
            route_hold_add_argv("No new launches", "machine-busy", "machine", None, None).unwrap(),
            v(&["route", "hold", "--from-app", "--reason=machine-busy", "--machine", "--", "No new launches"])
        );
        assert_eq!(
            route_hold_add_argv("Keep it", "keep", "account", Some("muse|"), Some("2026-10-17T20:00:00Z")).unwrap(),
            v(&["route", "hold", "--from-app", "--reason=keep", "--account=muse|", "--until=2026-10-17T20:00:00Z",
                "--", "Keep it"])
        );
        assert_eq!(
            route_hold_end_argv("h1a2b3c4").unwrap(),
            v(&["route", "hold", "--from-app", "--", "end", "h1a2b3c4"])
        );
    }

    #[test]
    fn a_routing_value_that_looks_like_a_flag_never_becomes_one() {
        // Free words always travel after "--": the CLI reads them as words.
        let argv = route_rule_argv("add", None, Some("--owner")).unwrap();
        let sep = argv.iter().position(|a| a == "--").unwrap();
        assert_eq!(&argv[sep + 1..], &v(&["add", "--owner"])[..]);
        let argv = route_note_set_argv("-x\n--from-app").unwrap();
        assert_eq!(argv.last().unwrap(), "-x\n--from-app");
        assert_eq!(argv.iter().position(|a| a == "--").unwrap(), argv.len() - 3);
        let argv = route_hold_add_argv("-x words", "other", "all", None, None).unwrap();
        assert_eq!(argv[argv.len() - 2..], v(&["--", "-x words"])[..]);
        // One-line fields refuse a line break; ids, accounts and times refuse
        // a leading "-" or anything that is not their shape.
        assert!(route_rule_argv("add", None, Some("one\n--from-app")).is_err());
        assert!(route_rule_argv("edit", Some(1), Some("a\rb")).is_err());
        assert!(route_hold_add_argv("one\ntwo", "other", "all", None, None).is_err());
        assert!(route_pause_argv("--owner", None).is_err());
        assert!(route_check_argv("--owner").is_err());
        assert!(route_check_argv("Codex\n--from-app").is_err());
        assert!(route_pause_argv("Codex\n--until=1h", None).is_err());
        assert!(route_pause_argv("Codex", Some("--from-app")).is_err());
        assert!(route_pause_argv("Codex", Some("1h")).is_err());
        assert!(route_pause_argv("Codex", Some("2026-10-16T09:00:00Z\n--x")).is_err());
        assert!(route_clear_argv("Codex", Some("-e1")).is_err());
        assert!(route_clear_argv("Codex", Some("e1 --from-app")).is_err());
        assert!(route_hold_end_argv("--all").is_err());
        assert!(route_hold_end_argv("h1\nend").is_err());
        assert!(route_hold_add_argv("x", "--all", "all", None, None).is_err());
        assert!(route_hold_add_argv("x", "other", "--machine", None, None).is_err());
        assert!(route_hold_add_argv("x", "other", "account", Some("-c"), None).is_err());
        assert!(route_rule_argv("--from-app", Some(1), None).is_err());
        assert!(route_rule_argv("retire", Some(0), None).is_err());
        assert!(route_rule_argv("retire", None, None).is_err());
        assert!(route_rule_argv("add", None, Some("   ")).is_err());
        assert!(route_note_set_argv(&"x".repeat(ROUTE_WORDS_MAX_BYTES + 1)).is_err());
        assert!(route_note_set_argv("a\u{0}b").is_err());
        // Nothing but these fixed verbs: no report, no wait, no sources.
        for verb in ["report", "wait", "sources"] {
            assert!(!ROUTE_RULE_ACTIONS.contains(&verb));
        }
        assert!(!ALLOWED_SUBCOMMANDS.contains(&"route"));
    }

    #[test]
    fn routing_settings_are_reprinted_or_refused() {
        assert_eq!(config_set_argv("routing_stale_after", "43200").unwrap(), v(&["config", "--from-app", "--set", "routing_stale_after", "43200"]));
        assert_eq!(config_set_argv("routing_summary_chars", " 0 ").unwrap(), v(&["config", "--from-app", "--set", "routing_summary_chars", "0"]));
        assert_eq!(config_set_argv("routing_push_prompt", "off").unwrap(), v(&["config", "--from-app", "--set", "routing_push_prompt", "off"]));
        // Every limit Khipu enforces on the owner's words or a line can be
        // raised from Settings -> Routing (fix round 1, F3).
        assert_eq!(config_set_argv("routing_hold_chars", "0").unwrap(), v(&["config", "--from-app", "--set", "routing_hold_chars", "0"]));
        assert_eq!(config_set_argv("routing_line_chars", "400").unwrap(), v(&["config", "--from-app", "--set", "routing_line_chars", "400"]));
        for (k, bad) in [("routing_stale_after", "-1"), ("routing_stale_after", "12h"), ("routing_prompt_chars", "--x"),
                         ("routing_push_start", "yes"), ("routing_summary_chars", "1e3"),
                         ("routing_stale_after", "999999999999")] {
            assert!(config_set_argv(k, bad).is_err(), "{k}={bad}");
        }
        assert_eq!(config_set_argv("routing_reset_check", "on").unwrap(), v(&["config", "--from-app", "--set", "routing_reset_check", "on"]));
        assert_eq!(config_set_argv("routing_reset_check_wait", "120").unwrap(), v(&["config", "--from-app", "--set", "routing_reset_check_wait", "120"]));
        assert_eq!(config_set_argv("routing_reset_check_retries", "[600,1800]").unwrap(), v(&["config", "--from-app", "--set", "routing_reset_check_retries", "[600,1800]"]));
        // The retries can be removed, or kept to one (fix round 1, F7).
        assert_eq!(config_set_argv("routing_reset_check_retries", "[]").unwrap(), v(&["config", "--from-app", "--set", "routing_reset_check_retries", "[]"]));
        assert_eq!(config_set_argv("routing_reset_check_retries", "[600]").unwrap(), v(&["config", "--from-app", "--set", "routing_reset_check_retries", "[600]"]));
        assert_eq!(config_set_argv("routing_busy_load", "0").unwrap(), v(&["config", "--from-app", "--set", "routing_busy_load", "0"]));
        assert_eq!(config_set_argv("routing_busy_load", "75").unwrap(), v(&["config", "--from-app", "--set", "routing_busy_load", "75"]));
        assert_eq!(config_set_argv("routing_busy_wait", "300").unwrap(), v(&["config", "--from-app", "--set", "routing_busy_wait", "300"]));
        assert_eq!(config_set_argv("routing_busy_tries", "3").unwrap(), v(&["config", "--from-app", "--set", "routing_busy_tries", "3"]));
        for (k, bad) in [("routing_reset_check", "yes"), ("routing_reset_check_wait", "-1"),
                         ("routing_reset_check_retries", "[600,600]"), ("routing_reset_check_retries", "[600,\n1800]"),
                         ("routing_reset_check_retries", "[0]"), ("routing_reset_check_retries", "[1,2,3]"),
                         ("routing_busy_load", "-1"), ("routing_busy_wait", "one"), ("routing_busy_tries", "-3")] {
            assert!(config_set_argv(k, bad).is_err(), "{k}={bad}");
        }
        assert_eq!(config_unset_argv("routing_reset_check").unwrap(), v(&["config", "--from-app", "--unset=routing_reset_check"]));
        // Phase 6: the push before handing work to another agent, and the
        // owner's dispatch scripts, by file name only.
        assert_eq!(config_set_argv("routing_push_delegation", "off").unwrap(), v(&["config", "--from-app", "--set", "routing_push_delegation", "off"]));
        assert_eq!(config_unset_argv("routing_push_delegation").unwrap(), v(&["config", "--from-app", "--unset=routing_push_delegation"]));
        assert!(config_set_argv("routing_push_delegation", "yes").is_err());
        assert_eq!(
            config_set_argv("routing_dispatch_scripts", r#"[" dispatch.py ", "run-job.sh", "dispatch.py"]"#).unwrap(),
            v(&["config", "--from-app", "--set", "routing_dispatch_scripts", r#"["dispatch.py","run-job.sh"]"#])
        );
        assert_eq!(config_set_argv("routing_dispatch_scripts", "[]").unwrap(), v(&["config", "--from-app", "--set", "routing_dispatch_scripts", "[]"]));
        assert_eq!(config_unset_argv("routing_dispatch_scripts").unwrap(), v(&["config", "--from-app", "--unset=routing_dispatch_scripts"]));
        for bad in [r#"["/bin/dispatch.py"]"#, r#"["a b"]"#, r#"[".."]"#, r#"["-x"]"#, r#"["-c"]"#, r#"["a\nb"]"#, r#"[3]"#,
                    "dispatch.py", r#"{"a": 1}"#] {
            assert!(config_set_argv("routing_dispatch_scripts", bad).is_err(), "{bad}");
        }
        let many: Vec<String> = (0..17).map(|i| format!("\"s{i}.py\"")).collect();
        assert!(config_set_argv("routing_dispatch_scripts", &format!("[{}]", many.join(","))).is_err());
        // Phase 7 settings are not offered yet.
        for k in ["routing_cloud_copy", "routing_rules_seed"] {
            assert!(config_set_argv(k, "on").is_err(), "{k}");
            assert!(config_unset_argv(k).is_err(), "{k}");
        }
        assert_eq!(config_unset_argv("routing_stale_after").unwrap(), v(&["config", "--from-app", "--unset=routing_stale_after"]));
        let sources = config_set_argv(
            "routing_sources",
            r#"[{"name": " Project A ", "kind": "anything", "path": "/runners/a"}]"#,
        )
        .unwrap();
        assert_eq!(sources[4], r#"[{"kind":"lanes","name":"Project A","path":"/runners/a"}]"#);
        for bad in [r#"[{"name": "a", "path": "relative/a"}]"#, r#"[{"name": "a", "path": "/a", "extra": 1}]"#,
                    r#"[{"name": "a\n--x", "path": "/a"}]"#, r#"{"name": "a"}"#, "-x"] {
            assert!(config_set_argv("routing_sources", bad).is_err(), "{bad}");
        }
        let map = config_set_argv("routing_lane_map", r#"{"Project B": {"opus-batch": "Claude main"}}"#).unwrap();
        assert_eq!(map[4], r#"{"Project B":{"opus-batch":"Claude main"}}"#);
        for bad in [r#"{"B": {"lane": 3}}"#, r#"{"B": ["lane"]}"#, r#"{"B": {"la\nne": "x"}}"#, "[]"] {
            assert!(config_set_argv("routing_lane_map", bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn every_routing_setting_built_so_far_can_be_changed_from_the_app() {
        // Codex review, finding 6: the record's, the job runners' and the
        // start's sizes and times, each re-printed or refused.
        for (k, raw, out) in [
            ("routing_report_default_limit", "3600", "3600"),
            ("routing_poll_interval", "0", "0"),
            ("routing_start_budget_ms", " 2000 ", "2000"),
            ("routing_runner_silent_intervals", "0", "0"),
            ("routing_runner_tail_bytes", "1048576", "1048576"),
            ("routing_runner_limited_lines", "0", "0"),
            ("routing_slow_read_pct", "0.5", "0.5"),
            ("routing_slow_read_pct", "2", "2"),
            ("routing_slow_read_pct", "0", "0"),
        ] {
            assert_eq!(config_set_argv(k, raw).unwrap(), v(&["config", "--from-app", "--set", k, out]), "{k}={raw}");
            assert_eq!(config_unset_argv(k).unwrap(), v(&["config", "--from-app", &format!("--unset={k}")]));
        }
        for (k, bad) in [("routing_slow_read_pct", "-1"), ("routing_slow_read_pct", "1e2"), ("routing_slow_read_pct", "NaN"),
                         ("routing_slow_read_pct", "inf"), ("routing_slow_read_pct", "1.2.3"), ("routing_slow_read_pct", "."),
                         ("routing_slow_read_pct", "101"), ("routing_slow_read_pct", "--x"),
                         ("routing_runner_tail_bytes", "-5"), ("routing_poll_interval", "10s")] {
            assert!(config_set_argv(k, bad).is_err(), "{k}={bad}");
        }
    }

    #[test]
    fn a_routing_setting_is_saved_and_answered_as_the_owner() {
        // A conversation's shell is refused a routing setting; the app's own
        // save carries --from-app, one step, and answers a waiting request.
        assert_eq!(config_set_argv("routing_lane_map", "{}").unwrap(), v(&["config", "--from-app", "--set", "routing_lane_map", "{}"]));
        assert_eq!(config_set_argv("synth_fallback", "none").unwrap()[1], "--set");
        assert_eq!(
            config_request_argv("routing_lane_map", "confirm").unwrap(),
            v(&["config", "--from-app", "--answer-request", "routing_lane_map", "confirm"])
        );
        assert_eq!(config_request_argv("routing_stale_after", "undo").unwrap()[4], "undo");
        assert!(config_request_argv("routing_lane_map", "--x").is_err());
        assert!(config_request_argv("usage_accounts", "confirm").is_err());
        assert!(config_request_argv("--from-app", "confirm").is_err());
    }

    #[test]
    fn the_routing_table_file_is_offered_as_a_full_path() {
        // Fix round 2, R2-1: Settings -> Routing chooses and clears the table file.
        assert_eq!(
            config_set_argv("routing_table", " ~/notes/ROUTING-NOW.md ").unwrap(),
            v(&["config", "--from-app", "--set", "routing_table", "~/notes/ROUTING-NOW.md"])
        );
        assert_eq!(
            config_set_argv("routing_table", "/notes/routing/ROUTING-NOW.md").unwrap(),
            v(&["config", "--from-app", "--set", "routing_table", "/notes/routing/ROUTING-NOW.md"])
        );
        assert_eq!(config_unset_argv("routing_table").unwrap(), v(&["config", "--from-app", "--unset=routing_table"]));
        let long = format!("/{}", "a".repeat(1024));
        for bad in ["", "   ", "notes/ROUTING-NOW.md", "~notes", "-x", "--from-app", "/a\n--x", "/a\rb", long.as_str()] {
            assert!(config_set_argv("routing_table", bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn the_owners_words_are_not_cut_at_a_fixed_length() {
        // Fix round 2, R2-4: a note, a rule or a hold longer than the old
        // 4,000 characters reaches the CLI, which applies the owner's own
        // routing_hold_chars (0: no limit). Only an argument too big to hand
        // over is refused.
        let long = "word ".repeat(2000);
        assert!(long.trim().chars().count() > 4000);
        assert_eq!(route_note_set_argv(&long).unwrap().last().unwrap(), long.trim());
        assert_eq!(route_rule_argv("add", None, Some(&long)).unwrap().last().unwrap(), long.trim());
        assert_eq!(route_rule_argv("edit", Some(2), Some(&long)).unwrap().last().unwrap(), long.trim());
        assert_eq!(
            route_hold_add_argv(&long, "other", "all", None, None).unwrap().last().unwrap(),
            long.trim()
        );
        let at_most = "x".repeat(ROUTE_WORDS_MAX_BYTES);
        assert!(route_note_set_argv(&at_most).is_ok());
        let err = route_hold_add_argv(&format!("{at_most}x"), "other", "all", None, None).unwrap_err();
        assert!(err.contains("128 KB"), "{err}");
    }

    #[test]
    fn the_routing_view_is_one_fixed_read_only_argv() {
        assert_eq!(fixed_argv(ROUTE_VIEW_ARGV), v(&["route", "--json"]));
        // No write verb, no owner flag, no --since or account the webview could steer.
        for word in ["report", "hold", "pause", "lift", "clear", "note", "rule", "wait", "sources",
                     "--from-app", "--owner", "--since", "--summary", "--brief"] {
            assert!(!ROUTE_VIEW_ARGV.contains(&word), "`{word}` must not be in the routing read");
        }
        assert!(!ALLOWED_SUBCOMMANDS.contains(&"route"));
    }

    #[test]
    fn the_summariser_ceiling_is_a_whole_number_reprinted() {
        assert_eq!(config_set_argv("synth_daily_calls", "5000").unwrap(), v(&["config", "--set", "synth_daily_calls", "5000"]));
        assert_eq!(config_set_argv("synth_daily_calls", " 0 ").unwrap(), v(&["config", "--set", "synth_daily_calls", "0"]));
        assert_eq!(config_set_argv("synth_daily_calls", "+12").unwrap(), v(&["config", "--set", "synth_daily_calls", "12"]));
        for bad in ["", "-1", "1.5", "1e3", "abc", "--cap", "99999999999"] {
            assert!(config_set_argv("synth_daily_calls", bad).is_err(), "accepted {bad:?}");
        }
        // The other ceilings are not offered by the app.
        assert!(config_set_argv("embed_daily_calls", "10").is_err());
        assert!(config_unset_argv("synth_daily_calls").is_err());
    }

    #[test]
    fn the_graph_backup_remote_is_one_remote_path() {
        assert_eq!(
            config_set_argv("graph_backup_remote", " r2:my-bucket/graph/ ").unwrap(),
            v(&["config", "--set", "graph_backup_remote", "r2:my-bucket/graph"])
        );
        for bad in ["", "bucket", "r2:", ":path", "--set:x", "r2:a b", "r2:a\n--x"] {
            assert!(config_set_argv("graph_backup_remote", bad).is_err(), "accepted {bad:?}");
        }
        assert_eq!(config_unset_argv("graph_backup_remote").unwrap(), v(&["config", "--unset=graph_backup_remote"]));
    }

    #[test]
    fn the_summariser_fallback_is_gemini_or_none() {
        assert_eq!(config_set_argv("synth_fallback", "gemini").unwrap(), v(&["config", "--set", "synth_fallback", "gemini"]));
        assert_eq!(config_set_argv("synth_fallback", " none ").unwrap(), v(&["config", "--set", "synth_fallback", "none"]));
        for bad in ["", "Gemini", "off", "anthropic", "--set", "none\n--x", "gemini none"] {
            let err = config_set_argv("synth_fallback", bad).unwrap_err();
            assert!(err.contains("gemini") && err.contains("none"), "bad error for {bad:?}: {err}");
        }
    }

    #[test]
    fn usage_accounts_are_a_json_list_reprinted() {
        let argv = config_set_argv_expect(
            "usage_accounts",
            r#" [ {"label": " Claude second ", "harness": "Claude", "home": "~/.claude-second"},
                  {"harness": "grok", "home": null, "label": ""} ] "#,
            Some("v3a"),
        )
        .unwrap();
        assert_eq!(
            argv,
            v(&[
                "config",
                "--from-app",
                "--expect=v3a",
                "--set",
                "usage_accounts",
                r#"[{"harness":"claude","home":"~/.claude-second","label":"Claude second"},{"harness":"grok"}]"#,
            ])
        );
        assert_eq!(config_set_argv_expect("usage_accounts", "[]", Some("v3a")).unwrap(), v(&["config", "--from-app", "--expect=v3a", "--set", "usage_accounts", "[]"]));
        let many = format!("[{}]", vec![r#"{"harness":"claude"}"#; 33].join(","));
        let long_label = format!(r#"[{{"harness":"claude","label":"{}"}}]"#, "x".repeat(65));
        for bad in [
            "",
            "--set",
            "{}",
            "\"claude\"",
            "[1]",
            "[\"claude\"]",
            r#"[{"harness":"cluade"}]"#,
            r#"[{"label":"No harness"}]"#,
            r#"[{"harness":"claude","token":"x"}]"#,
            r#"[{"harness":"claude","home":3}]"#,
            r#"[{"harness":"claude","label":"a\nb"}]"#,
            many.as_str(),
            long_label.as_str(),
        ] {
            assert!(config_set_argv_expect("usage_accounts", bad, Some("v3a")).is_err(), "accepted {bad:?}");
        }
        assert_eq!(
            config_set_argv_expect("usage_accounts", "[]", Some("v3a")).unwrap(),
            v(&["config", "--from-app", "--expect=v3a", "--set", "usage_accounts", "[]"])
        );
        assert_eq!(
            config_unset_argv_expect("usage_accounts", Some("v3a")).unwrap(),
            v(&["config", "--from-app", "--expect=v3a", "--unset=usage_accounts"])
        );
        for bad in ["", "--x", "a b", "a=b", "v\n1", &"a".repeat(41)] {
            assert!(config_set_argv_expect("usage_accounts", "[]", Some(bad)).is_err(), "{bad:?}");
            assert!(config_unset_argv_expect("usage_accounts", Some(bad)).is_err(), "{bad:?}");
        }
        // Other keys take no version.
        assert!(config_set_argv_expect("capture_mode", "hub", Some("v1")).is_err());
        for missing in [None, Some(""), Some("  ")] {
            let set = config_set_argv_expect("usage_accounts", "[]", missing).unwrap_err();
            assert!(set.contains("needs the version it read"), "{set}");
            assert!(config_unset_argv_expect("usage_accounts", missing).is_err(), "{missing:?}");
        }
        assert!(config_unset_argv_expect("memory_root", Some("v1")).is_err());
    }

    #[test]
    fn every_switch_builds_an_on_and_an_off_argv() {
        assert_eq!(FEATURE_NAMES.len(), 10);
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
        for ok in ["a", "x.y_z-1@2", "nomic-embed-text:latest@768"] {
            assert!(embed_activate_argv(ok).is_ok(), "rejected {ok:?}");
        }
        for bad in ["", " ", "-f", "--force", "a b", "a;b", "a\nb", "gemini$", "@768", "..", &"x".repeat(65)] {
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
mod embeddings_command_tests {
    use super::*;

    fn v(a: &[&str]) -> Vec<String> {
        a.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn read_only_commands_are_fixed_argv() {
        assert_eq!(fixed_argv(EMBED_PROFILES_LIST_ARGV), v(&["embed", "profiles", "list"]));
        assert_eq!(fixed_argv(EMBED_JOBS_ARGV), v(&["embed", "jobs"]));
        assert_eq!(fixed_argv(EMBED_JOBS_CLEAR_ARGV), v(&["embed", "jobs", "--clear"]));
        assert_eq!(fixed_argv(LIBRARY_LIST_ARGV), v(&["library", "list"]));
    }

    #[test]
    fn neither_embed_nor_library_nor_the_job_verbs_join_an_allowlist() {
        for s in ["embed", "library", "embed-backfill", "library-backfill"] {
            assert!(!ALLOWED_SUBCOMMANDS.contains(&s), "`{s}` must stay out of ALLOWED_SUBCOMMANDS");
            assert!(!SPAWN_ALLOWED_SUBCOMMANDS.contains(&s), "`{s}` must stay out of SPAWN_ALLOWED_SUBCOMMANDS");
        }
        assert_eq!(ALLOWED_SUBCOMMANDS.len(), 14);
        assert_eq!(SPAWN_ALLOWED_SUBCOMMANDS, &["nightly", "graph-build", "monthly"]);
        assert_eq!(JOB_SPAWN_VERBS, &["embed-backfill", "library-backfill"]);
    }

    #[test]
    fn estimate_argv_names_profile_and_space() {
        assert_eq!(
            embed_estimate_argv("nomic-embed-text@768", "memory", false).unwrap(),
            v(&["embed", "estimate", "--profile=nomic-embed-text@768", "--space=memory"])
        );
        assert_eq!(
            embed_estimate_argv("voyage-3@1024", "library:biblical", true).unwrap(),
            v(&["embed", "estimate", "--profile=voyage-3@1024", "--space=library:biblical", "--stale"])
        );
    }

    #[test]
    fn estimate_refuses_bad_profiles_and_spaces() {
        for bad in ["", "-x", "--force", "a b", "a;b"] {
            assert!(embed_estimate_argv(bad, "memory", false).is_err(), "profile {bad:?}");
        }
        for bad in ["", "libraries", "library:", "library:Bad", "library:a b", "library:../x", "memory --stale", "--space=x", "library:-x"] {
            assert!(embed_estimate_argv("a@1", bad, false).is_err(), "space {bad:?}");
        }
    }

    #[test]
    fn test_key_takes_a_provider_and_optional_endpoint_and_model() {
        assert_eq!(
            embed_test_key_argv("voyage", None, Some("voyage-3")).unwrap(),
            v(&["embed", "test-key", "--provider=voyage", "--model=voyage-3"])
        );
        assert_eq!(
            embed_test_key_argv("openai-compatible", Some("http://localhost:11434"), Some("nomic-embed-text")).unwrap(),
            v(&["embed", "test-key", "--provider=openai-compatible", "--endpoint=http://localhost:11434", "--model=nomic-embed-text"])
        );
        assert_eq!(embed_test_key_argv("gemini", Some("  "), None).unwrap(), v(&["embed", "test-key", "--provider=gemini"]));
    }

    #[test]
    fn test_key_refuses_unknown_providers_and_bad_endpoints() {
        for bad in ["", "openai", "Gemini", "gemini --model x", "-p"] {
            assert!(embed_test_key_argv(bad, None, None).is_err(), "provider {bad:?}");
        }
        for bad in [
            "http://example.com", "ftp://x", "localhost:11434", "https://", "http://localhost.evil.com",
            "http://127.0.0.1.evil.com", "https://a b", "https://a\nb", "-x", "http://localhostevil",
        ] {
            assert!(
                embed_test_key_argv("openai-compatible", Some(bad), Some("m")).is_err(),
                "endpoint {bad:?}"
            );
        }
        assert!(embed_test_key_argv("voyage", Some("https://api.example.com"), None).is_err());
        for bad in ["-m", "--model=x", "a b", "a\nb", "a;b"] {
            assert!(embed_test_key_argv("voyage", None, Some(bad)).is_err(), "model {bad:?}");
        }
    }

    #[test]
    fn endpoint_allows_https_and_loopback_http() {
        for ok in ["https://api.openai.com", "https://x.example.com/v1", "http://localhost", "http://localhost:1234/v1", "http://127.0.0.1:8080"] {
            assert!(checked_endpoint(ok).is_ok(), "rejected {ok:?}");
        }
    }

    #[test]
    fn profile_add_argv_checks_every_piece_and_that_the_id_is_model_at_dim() {
        assert_eq!(
            embed_profile_add_argv("voyage-3@1024", "voyage", "voyage-3", 1024, None).unwrap(),
            v(&["embed", "profiles", "add", "voyage-3@1024", "--provider=voyage", "--model=voyage-3", "--dim=1024"])
        );
        assert_eq!(
            embed_profile_add_argv("nomic-embed-text@768", "openai-compatible", "nomic-embed-text", 768, Some("http://localhost:11434")).unwrap(),
            v(&["embed", "profiles", "add", "nomic-embed-text@768", "--provider=openai-compatible", "--model=nomic-embed-text", "--dim=768", "--endpoint=http://localhost:11434"])
        );
        // The id must be exactly model@dim.
        assert!(embed_profile_add_argv("voyage-3@512", "voyage", "voyage-3", 1024, None).is_err());
        assert!(embed_profile_add_argv("other@1024", "voyage", "voyage-3", 1024, None).is_err());
        // Dimensions 1..=8192.
        for bad in [0, -1, 8193, i64::MAX] {
            assert!(embed_profile_add_argv(&format!("m@{bad}"), "voyage", "m", bad, None).is_err(), "dim {bad}");
        }
        assert!(embed_profile_add_argv("m@8192", "voyage", "m", 8192, None).is_ok());
        assert!(embed_profile_add_argv("m@1", "voyage", "m", 1, None).is_ok());
        // OpenAI-compatible needs an endpoint; the others refuse one.
        assert!(embed_profile_add_argv("m@8", "openai-compatible", "m", 8, None).is_err());
        assert!(embed_profile_add_argv("m@8", "gemini", "m", 8, Some("https://x.example.com")).is_err());
        assert!(embed_profile_add_argv("m@8", "nope", "m", 8, None).is_err());
        assert!(embed_profile_add_argv("-m@8", "voyage", "-m", 8, None).is_err());
    }

    #[test]
    fn profile_delete_always_confirms_and_checks_the_id() {
        assert_eq!(
            embed_profile_delete_argv("voyage-3@1024").unwrap(),
            v(&["embed", "profiles", "delete", "voyage-3@1024", "--yes"])
        );
        for bad in ["", "-1", "--yes", "a b", "a;b"] {
            assert!(embed_profile_delete_argv(bad).is_err(), "accepted {bad:?}");
        }
    }

    #[test]
    fn library_verbs_take_a_checked_name() {
        for verb in ["status", "scan", "enable", "disable"] {
            assert_eq!(library_argv(verb, "biblical").unwrap(), v(&["library", verb, "biblical"]));
            for bad in ["", "Biblical", "a b", "-x", "a/b", "a.b", "a\nb", &"x".repeat(41)] {
                assert!(library_argv(verb, bad).is_err(), "{verb} accepted {bad:?}");
            }
        }
        for verb in ["remove", "add", "backfill", "import", "", "scan --x"] {
            assert!(library_argv(verb, "x").is_err(), "verb {verb:?}");
        }
        assert!(library_argv("scan", &"x".repeat(40)).is_ok());
        assert!(library_argv("scan", "a_b-9").is_ok());
    }

    #[test]
    fn library_set_profile_argv_never_forces_and_checks_both_ids() {
        assert_eq!(
            library_set_profile_argv("biblical", "nomic-embed-text@768").unwrap(),
            v(&["library", "set-profile", "biblical", "nomic-embed-text@768"])
        );
        for bad in ["", "-x", "Bad", "a b", "a/b"] {
            assert!(library_set_profile_argv(bad, "a@1").is_err(), "name {bad:?}");
        }
        for bad in ["", "-f", "--force", "a b", "a;b", "a\nb"] {
            assert!(library_set_profile_argv("biblical", bad).is_err(), "profile {bad:?}");
        }
        assert!(!library_set_profile_argv("biblical", "a@1").unwrap().iter().any(|a| a.contains("force")));
    }

    #[test]
    fn library_add_and_remove_argv() {
        assert_eq!(
            library_add_argv("talks", "/Users/me/Documents/talks", "voyage-3@1024").unwrap(),
            v(&["library", "add", "talks", "--root=/Users/me/Documents/talks", "--profile=voyage-3@1024"])
        );
        assert_eq!(library_remove_argv("talks").unwrap(), v(&["library", "remove", "talks", "--yes"]));
        for bad in ["", "   ", "-x", "--root=/", "a\nb", "a\0b"] {
            assert!(library_add_argv("talks", bad, "a@1").is_err(), "root {bad:?}");
        }
        assert!(library_add_argv("Talks", "/x", "a@1").is_err());
        assert!(library_add_argv("talks", "/x", "--bad").is_err());
        assert!(library_remove_argv("../x").is_err());
    }

    #[test]
    fn library_import_argv_keeps_the_prefix_verbatim() {
        assert_eq!(
            library_import_argv("biblical", "/Users/me/g.sqlite", Some("Biblical System/corpus/"), Some("voyage-3@1024")).unwrap(),
            v(&["library", "import", "biblical", "/Users/me/g.sqlite", "--strip-prefix=Biblical System/corpus/", "--profile=voyage-3@1024"])
        );
        assert_eq!(
            library_import_argv("biblical", "/x.jsonl", Some(""), None).unwrap(),
            v(&["library", "import", "biblical", "/x.jsonl"])
        );
        for bad in ["", "-x", "--profile=x", "a\nb"] {
            assert!(library_import_argv("biblical", bad, None, None).is_err(), "path {bad:?}");
        }
        assert!(library_import_argv("biblical", "/x", Some("a\nb"), None).is_err());
        assert!(library_import_argv("biblical", "/x", None, Some("-p")).is_err());
        assert!(library_import_argv("Bad", "/x", None, None).is_err());
    }

    #[test]
    fn job_start_builds_the_two_detached_verbs() {
        assert_eq!(
            job_start_argv("memory", Some("nomic-embed-text@768"), None, false, "app-1-abc").unwrap(),
            v(&["embed-backfill", "--profile=nomic-embed-text@768", "--job-id=app-1-abc"])
        );
        assert_eq!(
            job_start_argv("library", None, Some("biblical"), false, "app-1-abc").unwrap(),
            v(&["library-backfill", "biblical", "--job-id=app-1-abc"])
        );
        assert_eq!(
            job_start_argv("library", Some("voyage-3@1024"), Some("biblical"), true, "app-1-abc").unwrap(),
            v(&["library-backfill", "biblical", "--profile=voyage-3@1024", "--stale", "--job-id=app-1-abc"])
        );
    }

    #[test]
    fn job_start_refuses_bad_kinds_missing_pieces_and_bad_ids() {
        assert!(job_start_argv("memory", None, None, false, "app-1").is_err());
        assert!(job_start_argv("memory", Some("  "), None, false, "app-1").is_err());
        assert!(job_start_argv("library", None, None, false, "app-1").is_err());
        assert!(job_start_argv("library", None, Some("Bad Name"), false, "app-1").is_err());
        assert!(job_start_argv("nightly", Some("a@1"), None, false, "app-1").is_err());
        assert!(job_start_argv("", None, None, false, "app-1").is_err());
        assert!(job_start_argv("memory", Some("--x"), None, false, "app-1").is_err());
        for bad in ["", "-x", "a b", "a/b", "../x", &"x".repeat(81)] {
            assert!(job_start_argv("memory", Some("a@1"), None, false, bad).is_err(), "id {bad:?}");
        }
    }

    #[test]
    fn generated_job_ids_are_valid_and_distinct() {
        let a = new_job_id();
        let b = new_job_id();
        assert!(job_id_ok(&a), "{a}");
        assert_ne!(a, b);
    }

    #[test]
    fn cancel_only_targets_a_running_job_that_names_its_own_pid() {
        let running = r#"{"job":"app-1-abc","state":"running","pid":4242}"#;
        assert_eq!(cancel_target(running, "app-1-abc").unwrap(), 4242);
        // A different id, a finished job, no pid, a tiny pid and junk all refuse.
        assert!(cancel_target(running, "app-2-abc").is_err());
        for raw in [
            r#"{"job":"app-1-abc","state":"done","pid":4242}"#,
            r#"{"job":"app-1-abc","state":"cancelled","pid":4242}"#,
            r#"{"job":"app-1-abc","state":"running"}"#,
            r#"{"job":"app-1-abc","state":"running","pid":0}"#,
            r#"{"job":"app-1-abc","state":"running","pid":1}"#,
            r#"{"job":"app-1-abc","state":"running","pid":-5}"#,
            r#"{"job":"app-1-abc","state":"running","pid":"4242"}"#,
            "not json",
            "",
        ] {
            assert!(cancel_target(raw, "app-1-abc").is_err(), "accepted {raw:?}");
        }
        for bad in ["", "-1", "../x", "a b"] {
            assert!(cancel_target(running, bad).is_err(), "id {bad:?}");
            assert!(cancel_job_sync(bad).is_err(), "cancel_job_sync {bad:?}");
        }
    }

    #[test]
    fn cosine_floor_by_profile_takes_a_profile_and_a_number_above_zero() {
        assert_eq!(
            config_set_argv("relevance.cosine_floor_by_profile.voyage-3@1024", "0.5").unwrap(),
            v(&["config", "--set", "relevance.cosine_floor_by_profile.voyage-3@1024", "0.5"])
        );
        assert_eq!(
            config_unset_argv("relevance.cosine_floor_by_profile.voyage-3@1024").unwrap(),
            v(&["config", "--unset=relevance.cosine_floor_by_profile.voyage-3@1024"])
        );
        for bad in ["0", "-0.1", "1.1", "nan", "", "--x", "abc"] {
            assert!(config_set_argv("relevance.cosine_floor_by_profile.a@1", bad).is_err(), "value {bad:?}");
        }
        for bad_key in [
            "relevance.cosine_floor_by_profile.",
            "relevance.cosine_floor_by_profile.-x",
            "relevance.cosine_floor_by_profile.a b",
            "relevance.cosine_floor_by_profile.a;b",
            "relevance.cosine_floor_by_profile",
        ] {
            assert!(config_set_argv(bad_key, "0.5").is_err(), "set key {bad_key:?}");
            assert!(config_unset_argv(bad_key).is_err(), "unset key {bad_key:?}");
        }
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

#[cfg(test)]
mod audit_1010_regression_tests {
    use super::*;

    #[test]
    fn feedback_refuses_unselected_and_reads_approved_file() {
        let path = std::env::temp_dir().join(format!("khipu-feedback-{}", std::process::id()));
        std::fs::write(&path, b"private attachment").unwrap();
        assert!(read_feedback_attachments(&[path.display().to_string()]).is_err());
        approve_feedback_attachment_for_session(&path).unwrap();
        assert_eq!(read_feedback_attachments(&[path.display().to_string()]).unwrap()[0].1, b"private attachment");
        forget_feedback_attachment_for_session(&path);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn pointer_tilde_expands() {
        let resolved = data_dir_from_pointer_json(r#"{"path":"~/khipu-test"}"#).unwrap();
        assert_eq!(resolved, PathBuf::from(std::env::var("HOME").unwrap()).join("khipu-test"));
    }

    #[test]
    fn data_dir_env_and_pointer_keep_dsn_and_jobs_together() {
        if std::env::var("KHIPU_RS10_CHILD").as_deref() == Ok("1") {
            let home = PathBuf::from(std::env::var("HOME").unwrap());
            let pointer_data = home.join("pointer-data");
            std::fs::create_dir_all(pointer_data.join("jobs")).unwrap();
            std::fs::write(pointer_data.join("dsn"), "postgres://pointer").unwrap();
            std::fs::create_dir_all(home.join(".config/khipu")).unwrap();
            std::fs::write(home.join(".config/khipu/data_root.json"), r#"{"path":"~/pointer-data"}"#).unwrap();
            assert_eq!(dirs_fallback_dsn(), pointer_data.join("dsn"));
            assert_eq!(jobs_dir_path(), pointer_data.join("jobs"));
            let env_data = home.join("env-data");
            std::fs::create_dir_all(env_data.join("jobs")).unwrap();
            std::fs::write(env_data.join("dsn"), "postgres://env").unwrap();
            std::env::set_var("KHIPU_DATA_DIR", "~/env-data");
            assert_eq!(dirs_fallback_dsn(), env_data.join("dsn"));
            assert_eq!(jobs_dir_path(), env_data.join("jobs"));
            return;
        }
        let home = std::env::temp_dir().join(format!("khipu-rs10-{}", std::process::id()));
        std::fs::create_dir_all(&home).unwrap();
        let status = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "audit_1010_regression_tests::data_dir_env_and_pointer_keep_dsn_and_jobs_together"])
            .env("KHIPU_RS10_CHILD", "1").env("HOME", &home)
            .env_remove("KHIPU_DATA_DIR").env_remove("ALZY_DATA_DIR")
            .env_remove("KHIPU_DSN_FILE").env_remove("ALZY_DSN_FILE").status().unwrap();
        let _ = std::fs::remove_dir_all(home);
        assert!(status.success());
    }

    #[test]
    fn doctor_needs_every_green_field_and_status_is_not_full_health() {
        for field in ["backup_ok", "drift_ok", "capture_liveness_ok", "git_sync_ok"] {
            let raw = format!(r#"{{"ok":true,"status":{{"counts":{{"episodes":1}}}},"backup_ok":true,"drift_ok":true,"capture_liveness_ok":true,"git_sync_ok":true,"{field}":false}}"#);
            assert!(!tray_tooltip_from_doctor(&raw).contains(" · OK · "), "{field}");
        }
        for field in ["backup_ok", "drift_ok", "capture_liveness_ok", "git_sync_ok"] {
            let raw = format!(r#"{{"ok":true,"status":{{"counts":{{"episodes":1}}}},"backup_ok":true,"drift_ok":true,"capture_liveness_ok":true,"git_sync_ok":true}}"#).replace(&format!(",\"{field}\":true"), "");
            assert_eq!(tray_tooltip_from_doctor(&raw), "Khipu · doctor unavailable", "{field}");
        }
        assert_eq!(tray_tooltip_from_status(r#"{"dsn_ok":true,"counts":{"episodes":1}}"#), "Khipu · doctor unavailable · status only · 1 episodes");
    }

    #[test]
    fn python_version_uses_isolated_safe_cache_env() {
        use std::os::unix::fs::PermissionsExt;
        if std::env::var("KHIPU_RS7_CHILD").as_deref() == Ok("1") {
            assert_eq!(python_version(&PathBuf::from(std::env::var("KHIPU_RS7_FIXTURE").unwrap())), Some((3, 11)));
            return;
        }
        let temp = std::env::temp_dir().join(format!("khipu-python-{}", std::process::id()));
        std::fs::create_dir_all(&temp).unwrap();
        let fixture = temp.join("python");
        std::fs::write(&fixture, "#!/bin/sh\n[ \"$PYTHONPYCACHEPREFIX\" = \"$HOME/Library/Caches/Khipu/pycache\" ] && [ -z \"$PYTHONDONTWRITEBYTECODE\" ] && echo 3.11 || echo 3.10\n").unwrap();
        let mut permissions = std::fs::metadata(&fixture).unwrap().permissions(); permissions.set_mode(0o700); std::fs::set_permissions(&fixture, permissions).unwrap();
        let status = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "audit_1010_regression_tests::python_version_uses_isolated_safe_cache_env"])
            .env("KHIPU_RS7_CHILD", "1").env("KHIPU_RS7_FIXTURE", &fixture).env("HOME", &temp)
            .env_remove("PYTHONPYCACHEPREFIX").env_remove("PYTHONDONTWRITEBYTECODE").status().unwrap();
        let _ = std::fs::remove_dir_all(temp); assert!(status.success());
    }

    #[test]
    fn cancel_requires_exact_backfill_job_not_another_khipu_daemon() {
        assert!(!process_is_current_job("python -m khipu recall --job-id=app-1", "app-1"));
        assert!(!process_is_current_job("python -m khipu embed-backfill --job-id=other", "app-1"));
        assert!(process_is_current_job("python -m khipu embed-backfill --job-id=app-1", "app-1"));
    }

    #[test]
    fn keychain_services_match_custom_and_disabled_python_rules() {
        assert_eq!(keychain_services(Some("Custom"), false), Vec::<String>::new());
        assert_eq!(keychain_services(Some("Custom"), true), vec!["Custom", "Alzy"]);
        assert_eq!(keychain_services(Some(""), true), vec!["", "Alzy"]);
        assert_eq!(keychain_services(Some(" "), true), vec![" ", "Alzy"]);
    }

    #[test]
    fn bundle_stages_both_recall_launchers() {
        let script = include_str!("../../scripts/bundle_cli.sh");
        for name in ["khipu-prompt-recall", "khipu-recall-client.py"] { assert!(script.contains(name), "{name}"); }
    }

    #[test]
    fn manifest_validator_rejects_stale_and_accepts_matching_feed() {
        let script = include_str!("../../scripts/release_macos.sh");
        let start = script.find("import json, sys\n").unwrap(); let end = script[start..].find("\nPY\n").unwrap() + start;
        let temp = std::env::temp_dir().join(format!("khipu-manifest-{}", std::process::id())); std::fs::create_dir_all(&temp).unwrap();
        let validator = temp.join("validator.py"); let stale = temp.join("stale.json"); let matching = temp.join("matching.json");
        std::fs::write(&validator, &script[start..end]).unwrap();
        std::fs::write(&stale, r#"{"version":"old","platforms":{"darwin-aarch64":{"url":"old"}}}"#).unwrap();
        std::fs::write(&matching, r#"{"version":"1","platforms":{"darwin-aarch64":{"url":"asset"}}}"#).unwrap();
        assert!(!Command::new("python3").arg(&validator).arg(&stale).arg("1").arg("asset").status().unwrap().success());
        assert!(!Command::new("python3").arg(&validator).arg(&matching).arg("1").arg("wrong-asset").status().unwrap().success());
        assert!(Command::new("python3").arg(&validator).arg(&matching).arg("1").arg("asset").status().unwrap().success());
        let _ = std::fs::remove_dir_all(temp);
    }

    #[test]
    fn install_fragment_handles_copy_final_move_and_rollback_failures() {
        let script = include_str!("../../scripts/release_macos.sh");
        let start = script.find("install_staged_app() {").unwrap();
        let end = script[start..].find("\n}\n\nif [[ \"$INSTALL_APPS\"").unwrap() + start + 2;
        let temp = std::env::temp_dir().join(format!("khipu-rs12-{}", std::process::id()));
        let source = temp.join("source/Khipu.app"); let apps = temp.join("Applications");
        std::fs::create_dir_all(source.join("Contents/MacOS")).unwrap(); std::fs::create_dir_all(&apps).unwrap();
        std::fs::write(source.join("Contents/Info.plist"), "new").unwrap(); std::fs::write(source.join("Contents/MacOS/Khipu"), "new").unwrap();
        use std::os::unix::fs::PermissionsExt;
        let mut perms = std::fs::metadata(source.join("Contents/MacOS/Khipu")).unwrap().permissions(); perms.set_mode(0o700); std::fs::set_permissions(source.join("Contents/MacOS/Khipu"), perms).unwrap();
        let body = format!("{}\nrun() {{ mode=$1; target=$2/Khipu.app; rm -rf \"$target\" \"$2\"/.khipu-install.*; mkdir -p \"$target\"; echo old >\"$target/marker\"; n=0; ditto() {{ if [ \"$mode\" = copyfail ]; then mkdir -p \"$2\"; echo partial >\"$2/partial\"; return 1; fi; cp -R \"$1\" \"$2\"; }}; mv() {{ n=$((n+1)); [ \"$mode\" = finalfail ] && [ \"$n\" = 2 ] && return 1; [ \"$mode\" = rollbackfail ] && [ \"$n\" -ge 2 ] && return 1; command mv \"$@\"; }}; install_staged_app '{}' \"$target\"; rc=$?; case \"$mode\" in copyfail|finalfail) [ $rc -ne 0 ] && [ \"$(cat \"$target/marker\")\" = old ];; rollbackfail) [ $rc -ne 0 ] && [ ! -e \"$target\" ] && [ \"$(cat \"$(find \"$2\" -name previous.app)/marker\")\" = old ];; success) [ $rc -eq 0 ] && [ -f \"$target/Contents/Info.plist\" ] && ! find \"$2\" -name previous.app | grep -q .;; esac; }}\nrun copyfail '{}' || exit 1; run finalfail '{}' || exit 1; run rollbackfail '{}' || exit 1; run success '{}' || exit 1\n", &script[start..end], source.display(), apps.display(), apps.display(), apps.display(), apps.display());
        let out = Command::new("/bin/bash").arg("-c").arg(body).output().unwrap();
        let _ = std::fs::remove_dir_all(temp);
        assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    }
}

#[cfg(test)]
mod priority_and_sign_in_tests {
    use super::*;

    fn v(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    fn entry(account: &str, role: Option<&str>, until: Option<&str>) -> PriorityEntry {
        PriorityEntry { account: account.into(), role: role.map(Into::into), until: until.map(Into::into) }
    }

    #[test]
    fn the_priority_travels_as_one_joined_json_argument_in_the_owners_order() {
        let entries = [
            entry("claude|/logins/claude-second", Some("all Claude work first"), Some("2026-10-17T15:00:00-04:00")),
            entry("codex|", Some("--from-app -- clear"), None),
            entry("claude|", None, None),
        ];
        let argv = route_priority_argv("set", &entries, Some("2026-10-20T09:00:00Z")).unwrap();
        assert_eq!(argv[..3], v(&["route", "priority", "--from-app"])[..]);
        assert_eq!(argv[argv.len() - 2..], v(&["--", "set"])[..]);
        assert_eq!(argv.len(), 8);
        assert_eq!(argv[5], "--expect=p1");
        let json = argv[3].strip_prefix("--entries=").unwrap();
        let parsed: Value = serde_json::from_str(json).unwrap();
        let order: Vec<&str> = parsed.as_array().unwrap().iter().map(|e| e["account"].as_str().unwrap()).collect();
        assert_eq!(order, ["claude|/logins/claude-second", "codex|", "claude|"]);
        // A role that looks like a flag stays words inside the JSON.
        assert_eq!(parsed[1]["role"], "--from-app -- clear");
        assert_eq!(argv[4], "--until=2026-10-20T09:00:00Z");
    }

    #[test]
    fn a_value_can_never_become_an_option() {
        assert!(route_priority_argv("set", &[entry("--from-app", None, None)], None).is_err());
        assert!(route_priority_argv("set", &[entry("Codex\n--x", None, None)], None).is_err());
        assert!(route_priority_argv("set", &[entry("Codex", Some("a\nb"), None)], None).is_err());
        assert!(route_priority_argv("set", &[entry("Codex", None, Some("--until"))], None).is_err());
        assert!(route_priority_argv("set", &[entry("Codex", None, None)], Some("1h")).is_err());
        assert!(route_priority_argv("--clear", &[], None).is_err());
        assert!(route_priority_argv("set", &[], None).is_err());
        let many: Vec<PriorityEntry> = (0..65).map(|_| entry("Codex", None, None)).collect();
        assert!(route_priority_argv("set", &many, None).is_err());
        // Confirm, undo and clear carry nothing but their version and verb, whatever is sent.
        for action in ["confirm", "undo"] {
            let argv = route_priority_argv_expect(action, &[entry("Codex", Some("x"), None)], Some("2026-10-20T09:00:00Z"), Some("p1")).unwrap();
            assert_eq!(argv, v(&["route", "priority", "--from-app", "--expect=p1", "--", action]));
        }
        let clear = route_priority_argv_expect("clear", &[entry("Codex", Some("x"), None)], Some("2026-10-20T09:00:00Z"), Some("p1")).unwrap();
        assert_eq!(clear, v(&["route", "priority", "--from-app", "--expect=p1", "--", "clear"]));
    }

    #[test]
    fn the_priority_version_joins_its_flag_before_the_verb() {
        let argv = route_priority_argv_expect("clear", &[], None, Some("p9f")).unwrap();
        assert_eq!(argv, v(&["route", "priority", "--from-app", "--expect=p9f", "--", "clear"]));
        let set = route_priority_argv_expect("set", &[entry("Codex", None, None)], None, Some("p0")).unwrap();
        assert_eq!(set[set.len() - 3], "--expect=p0");
        for bad in ["", "--x", "p 1", "p=1", "p\n1", "p|1"] {
            assert!(route_priority_argv_expect("clear", &[], None, Some(bad)).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn a_priority_change_without_its_version_is_refused() {
        for missing in [None, Some(""), Some("  ")] {
            let clear = route_priority_argv_expect("clear", &[], None, missing).unwrap_err();
            assert!(clear.contains("needs the version it read"), "{clear}");
            assert!(route_priority_argv_expect("set", &[entry("Codex", None, None)], None, missing).is_err());
            // Confirm and undo answer the request the owner saw, so a newer one is never answered unseen.
            assert!(route_priority_argv_expect("confirm", &[], None, missing).is_err());
            assert!(route_priority_argv_expect("undo", &[], None, missing).is_err());
        }
    }

    #[test]
    fn an_account_left_as_stored_goes_through_as_stored() {
        let stored: Vec<Value> = serde_json::from_str(
            r#"[{"harness": "codex", "label": "Codex"}, {"harness": "legacy-cli", "label": "Legacy", "note": 1}]"#,
        )
        .unwrap();
        // The app sends the untouched entry back and adds one: Add is not blocked by it.
        let sent = r#"[{"harness": "codex", "label": "Codex"}, {"harness": "legacy-cli", "label": " Legacy ", "note": 1},
                       {"harness": "Claude", "label": "Work", "home": "~/.claude-work"}]"#;
        let out: Value = serde_json::from_str(&checked_usage_accounts_kept(sent, &stored).unwrap()).unwrap();
        assert_eq!(out[1], stored[1]);
        assert_eq!(out[2], serde_json::json!({"harness": "claude", "label": "Work", "home": "~/.claude-work"}));
        let argv = config_set_argv_kept(USAGE_ACCOUNTS_KEY, sent, Some("a1"), &stored).unwrap();
        assert_eq!(argv[..5], v(&["config", "--from-app", "--expect=a1", "--set", USAGE_ACCOUNTS_KEY])[..]);
        // Changed, or with nothing stored, it is checked as any new entry.
        let changed = r#"[{"harness": "legacy-cli", "label": "Renamed"}]"#;
        assert!(checked_usage_accounts_kept(changed, &stored).is_err());
        assert!(checked_usage_accounts(r#"[{"harness": "legacy-cli", "label": "Legacy"}]"#).is_err());
    }

    fn acct(harness: &str, home: Option<&str>, label: Option<&str>) -> AccountEntry {
        AccountEntry { harness: harness.into(), home: home.map(Into::into), label: label.map(Into::into) }
    }

    #[test]
    fn track_rebuilds_its_entries_from_checked_fields() {
        let argv = accounts_track_argv(
            &[acct("Claude", Some("~/.claude-work"), Some(" Work ")), acct("muse", None, Some(""))],
            "v7",
            Some(&[acct("claude", None, Some("Claude"))]),
            true,
        )
        .unwrap();
        assert_eq!(
            argv,
            v(&[
                "accounts",
                "track",
                "--from-app",
                "--expect=v7",
                r#"--entries=[{"harness":"claude","home":"~/.claude-work","label":"Work"},{"harness":"muse"}]"#,
                r#"--shown-defaults=[{"harness":"claude","label":"Claude"}]"#,
                "--no-read",
            ])
        );
        let plain = accounts_track_argv(&[acct("codex", None, None)], "v7", None, false).unwrap();
        assert_eq!(plain.len(), 5);
        assert!(accounts_track_argv(&[], "v7", None, false).is_err());
        assert!(accounts_track_argv(&[acct("cluade", None, None)], "v7", None, false).is_err());
        assert!(accounts_track_argv(&[acct("claude", Some("relative"), None)], "v7", None, false).is_err());
        assert!(accounts_track_argv(&[acct("claude", Some("~/a\nb"), None)], "v7", None, false).is_err());
        assert!(accounts_track_argv(&[acct("claude", None, Some("a\nb"))], "v7", None, false).is_err());
        assert!(accounts_track_argv(&[acct("claude", None, Some(&"x".repeat(65)))], "v7", None, false).is_err());
        assert!(accounts_track_argv(&[acct("claude", None, None)], "--x", None, false).is_err());
        let many: Vec<AccountEntry> = (0..33).map(|_| acct("claude", None, None)).collect();
        assert!(accounts_track_argv(&many, "v7", None, false).is_err());
        assert!(accounts_track_argv(&[acct("claude", None, None)], "v7", Some(&many), false).is_err());
    }

    #[test]
    fn rename_and_move_check_the_key_name_folder_and_version() {
        assert_eq!(
            accounts_rename_argv("claude|~/.claude-work", " Work Mac ", "v2").unwrap(),
            v(&["accounts", "rename", "--from-app", "--expect=v2", "--account=claude|~/.claude-work", "--name=Work Mac"])
        );
        assert_eq!(
            accounts_move_argv("claude|", "/opt/x/.claude-new", "v2").unwrap(),
            v(&["accounts", "move", "--from-app", "--expect=v2", "--account=claude|", "--folder=/opt/x/.claude-new"])
        );
        for key in ["", "--from-app", "claude", "other|", "-claude|", "claude|a\nb", "Claude"] {
            assert!(accounts_rename_argv(key, "Work", "v2").is_err(), "{key:?}");
            assert!(accounts_move_argv(key, "~/x", "v2").is_err(), "{key:?}");
        }
        for name in ["", "   ", "a\nb", &"n".repeat(65)] {
            assert!(accounts_rename_argv("claude|", name, "v2").is_err(), "{name:?}");
        }
        for folder in ["", "relative", "--x", "~/a\nb", "~"] {
            assert!(accounts_move_argv("claude|", folder, "v2").is_err(), "{folder:?}");
        }
        assert!(accounts_rename_argv("claude|", "Work", "").is_err());
        assert!(accounts_move_argv("claude|", "~/x", "v 2").is_err());
    }

    #[test]
    fn stop_carries_only_the_kept_flags_and_checked_hold_ids() {
        assert_eq!(
            accounts_stop_argv("codex|", "v4", false, false, false, &[], false).unwrap(),
            v(&["accounts", "stop", "--from-app", "--expect=v4", "--account=codex|"])
        );
        assert_eq!(
            accounts_stop_argv("codex|", "v4", true, true, true, &["h1a2b".to_string(), "runner:x".to_string()], true).unwrap(),
            v(&[
                "accounts",
                "stop",
                "--from-app",
                "--expect=v4",
                "--account=codex|",
                "--keep-priority",
                "--keep-pause",
                "--keep-lanes",
                "--confirm-last",
                "--end-hold=h1a2b",
                "--end-hold=runner:x",
            ])
        );
        assert!(accounts_stop_argv("codex|", "v4", false, false, false, &["--x".to_string()], false).is_err());
        assert!(accounts_stop_argv("codex|", "v4", false, false, false, &["a\nb".to_string()], false).is_err());
        let many: Vec<String> = (0..65).map(|i| format!("h{i}")).collect();
        assert!(accounts_stop_argv("codex|", "v4", false, false, false, &many, false).is_err());
        assert!(accounts_stop_argv("--confirm-last", "v4", false, false, false, &[], false).is_err());
        assert!(accounts_stop_argv("codex|", "", false, false, false, &[], false).is_err());
    }

    #[test]
    fn offers_switch_and_request_answers_build_fixed_argv() {
        assert_eq!(
            accounts_offer_argv("decline", "Codex", None).unwrap(),
            v(&["accounts", "decline", "--from-app", "--harness=codex"])
        );
        assert_eq!(
            accounts_offer_argv("offer-again", "claude", Some("~/.claude-old")).unwrap(),
            v(&["accounts", "offer-again", "--from-app", "--harness=claude", "--folder=~/.claude-old"])
        );
        assert_eq!(accounts_offer_argv("decline", "claude", Some("  ")).unwrap().len(), 4);
        assert!(accounts_offer_argv("track", "claude", None).is_err());
        assert!(accounts_offer_argv("decline", "--x", None).is_err());
        assert!(accounts_offer_argv("decline", "claude", Some("--folder")).is_err());
        assert_eq!(accounts_auto_track_argv(true), v(&["accounts", "auto-track", "--from-app", "on"]));
        assert_eq!(accounts_auto_track_argv(false), v(&["accounts", "auto-track", "--from-app", "off"]));
        assert_eq!(accounts_request_argv("confirm", "a1b2").unwrap(), v(&["accounts", "request", "--from-app", "--expect=a1b2", "confirm"]));
        assert_eq!(accounts_request_argv("undo", "a1b2").unwrap(), v(&["accounts", "request", "--from-app", "--expect=a1b2", "undo"]));
        for missing in ["", "  ", "-x"] {
            assert!(accounts_request_argv("undo", missing).is_err(), "{missing:?}");
        }
        assert!(accounts_request_argv("--x", "a1").is_err());
        assert!(accounts_request_argv("clear", "a1").is_err());
        assert_eq!(accounts_status_argv(), v(&["accounts", "status"]));
    }

    fn config() -> Value {
        serde_json::json!({"usage_accounts": {"user_home": "/home/owner", "accounts": [
            {"label": "Claude", "harness": "claude", "home": null},
            {"label": "Claude second", "harness": "claude", "home": "~/.claude-second"},
            {"label": "Codex", "harness": "codex", "home": null},
            {"label": "Grok", "harness": "grok", "home": null},
            {"label": "Broken", "harness": "claude", "home": "~/.broken", "invalid": "no such folder"},
        ]}})
    }

    #[test]
    fn sign_in_names_only_a_configured_account_and_passes_no_extra_arguments() {
        let c = config();
        assert_eq!(sign_in_argv(&c, "claude", None).unwrap(), v(&["usage", "--sign-in", "--account=Claude", "--account-key=claude|"]));
        assert_eq!(
            sign_in_argv(&c, "claude", Some("~/.claude-second")).unwrap(),
            v(&["usage", "--sign-in", "--account=Claude second", "--account-key=claude|~/.claude-second"])
        );
        // The same folder written in full is the same login.
        assert_eq!(
            sign_in_argv(&c, "claude", Some("/home/owner/.claude-second")).unwrap(),
            v(&["usage", "--sign-in", "--account=Claude second", "--account-key=claude|~/.claude-second"])
        );
        assert_eq!(sign_in_argv(&c, "codex", Some("")).unwrap(), v(&["usage", "--sign-in", "--account=Codex", "--account-key=codex|"]));
    }

    #[test]
    fn sign_in_carries_the_accounts_identity_when_two_share_a_label() {
        let c = serde_json::json!({"usage_accounts": {"user_home": "/home/owner", "accounts": [
            {"label": "Same", "harness": "claude", "home": null},
            {"label": "Same", "harness": "codex", "home": null},
        ]}});
        assert_eq!(sign_in_argv(&c, "codex", None).unwrap(), v(&["usage", "--sign-in", "--account=Same", "--account-key=codex|"]));
        assert_eq!(sign_in_argv(&c, "claude", None).unwrap(), v(&["usage", "--sign-in", "--account=Same", "--account-key=claude|"]));
    }

    #[test]
    fn sign_in_refuses_a_folder_that_is_not_a_configured_account() {
        let c = config();
        for home in ["~/.claude-other", "/tmp/evil", "--from-app", "~/.claude-second --x"] {
            assert!(sign_in_argv(&c, "claude", Some(home)).is_err(), "{home}");
        }
        assert!(sign_in_argv(&c, "claude", Some("~/.broken")).is_err());
        assert!(sign_in_argv(&c, "codex", Some("~/.claude-second")).is_err());
        assert!(sign_in_argv(&c, "grok", None).is_err());
        assert!(sign_in_argv(&c, "muse", None).is_err());
        assert!(sign_in_argv(&c, "--sign-in", None).is_err());
        assert!(sign_in_argv(&serde_json::json!({}), "claude", None).is_err());
    }
}
