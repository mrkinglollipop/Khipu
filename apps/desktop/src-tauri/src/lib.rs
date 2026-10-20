use std::collections::HashSet;
use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};
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

/// The app keys its "hub not reachable" state on this prefix: keep it.
const CLI_TIMEOUT_PREFIX: &str = "Khipu did not answer within";
const CLI_TIMEOUT_DEFAULT_SECS: u64 = 60;

fn cli_timeout_message(secs: u64) -> String {
    format!("{CLI_TIMEOUT_PREFIX} {secs} seconds. The hub may be unreachable.")
}

enum CliWaitError {
    Spawn(std::io::Error),
    TimedOut(u64),
}

/// `Command::output()` with a deadline: the child is killed when it passes.
/// `None` waits as long as the child runs, as `.output()` did.
fn output_with_timeout(mut cmd: Command, timeout: Option<std::time::Duration>) -> Result<std::process::Output, CliWaitError> {
    use std::io::Read;
    use std::process::Stdio;
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(CliWaitError::Spawn)?;
    fn drain<R: Read + Send + 'static>(pipe: Option<R>) -> std::thread::JoinHandle<Vec<u8>> {
        std::thread::spawn(move || {
            let mut buf = Vec::new();
            if let Some(mut p) = pipe {
                let _ = p.read_to_end(&mut buf);
            }
            buf
        })
    }
    let out_t = drain(child.stdout.take());
    let err_t = drain(child.stderr.take());
    let deadline = timeout.map(|t| std::time::Instant::now() + t);
    let status = loop {
        match child.try_wait().map_err(CliWaitError::Spawn)? {
            Some(st) => break st,
            None => {
                if deadline.is_some_and(|d| std::time::Instant::now() >= d) {
                    let _ = child.kill();
                    let _ = child.wait();
                    // Readers are left to end on their own: a grandchild may
                    // still hold the pipe open, and we must not wait on it.
                    return Err(CliWaitError::TimedOut(timeout.map_or(0, |t| t.as_secs())));
                }
                std::thread::sleep(std::time::Duration::from_millis(20));
            }
        }
    };
    Ok(std::process::Output {
        status,
        stdout: out_t.join().unwrap_or_default(),
        stderr: err_t.join().unwrap_or_default(),
    })
}

/// Seconds the app waits for one CLI answer: `app_cli_timeout` in the data
/// dir's config.json (0: no limit; missing or invalid: 60).
fn app_cli_timeout_secs_from(raw: Option<&str>) -> u64 {
    let Some(v) = raw.and_then(|r| serde_json::from_str::<Value>(r).ok()).and_then(|v| v.get("app_cli_timeout").cloned()) else {
        return CLI_TIMEOUT_DEFAULT_SECS;
    };
    match v.as_u64() {
        Some(n) if n <= 10 * 366 * 86_400 => n,
        _ => CLI_TIMEOUT_DEFAULT_SECS,
    }
}

fn app_cli_timeout_secs() -> u64 {
    let dir = data_dir_from_pointer().or_else(|| {
        std::env::var("HOME").ok().map(|h| PathBuf::from(h).join(".config/khipu"))
    });
    let raw = dir.and_then(|d| std::fs::read_to_string(d.join("config.json")).ok());
    app_cli_timeout_secs_from(raw.as_deref())
}

/// Long user actions that must never be cut short by the hub timeout: a
/// backup or import, `doctor` (a healthy one can run past a minute and must
/// never be reported as the hub being away), the job runners the app starts and waits on, and the
/// database move, preflight and connect (they copy data or dial a new hub).
/// The join and export paths run their own process and never come here.
fn cli_has_no_time_limit(args: &[String]) -> bool {
    let first = args.first().map(String::as_str).unwrap_or("");
    match first {
        "backup-local" | "import-local" | "doctor" | "nightly" | "graph-build" | "monthly" | "embed-backfill"
        | "library-backfill" | "join" => true,
        "db" => matches!(args.get(1).map(String::as_str), Some("move" | "preflight" | "connect")),
        // The Owed clean-up: a plan may make its capped model calls, and an
        // apply or undo changes thousands of rows in one transaction.
        "owed" => args.get(1).map(String::as_str) == Some("tidy"),
        _ => false,
    }
}

fn cli_timeout_for(args: &[String]) -> Option<std::time::Duration> {
    if cli_has_no_time_limit(args) {
        return None;
    }
    match app_cli_timeout_secs() {
        0 => None,
        n => Some(std::time::Duration::from_secs(n)),
    }
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
    let output = match output_with_timeout(cmd, cli_timeout_for(args)) {
        Ok(o) => o,
        Err(CliWaitError::Spawn(e)) => return Err(format!("spawn khipu CLI failed ({py:?}): {e}")),
        Err(CliWaitError::TimedOut(secs)) => return Err(cli_timeout_message(secs)),
    };
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
    "replica_refresh",
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
/// Settings clarity phase 9 (9C): the login folders Khipu could track, as
/// `khipu usage --discover --json` reports them (paths included: the app's,
/// never an agent's). `--now` is Look for accounts now, which ignores the
/// once-a-day wait. Fixed argv either way.
const ACCOUNTS_DISCOVER_ARGV: &[&str] = &["usage", "--discover", "--json"];
const ACCOUNTS_DISCOVER_NOW_ARGV: &[&str] = &["usage", "--discover", "--now", "--json"];
/// Forget which logins conversations used: clears the recorded login folder
/// from every saved conversation's state.
const ACCOUNTS_FORGET_LOGIN_PATHS_ARGV: &[&str] = &["usage", "--forget-login-paths"];
/// `config --set KEY PATH` / `config --unset=KEY` for where Khipu finds each
/// CLI and its Node (`khipu.config.CLI_SETTINGS`, decision 10).
const CLI_PATH_KEYS: &[&str] = &["cli_path.claude", "cli_path.codex", "cli_path.grok", "cli_path.muse"];
const NODE_FOLDER_KEY: &str = "node_folder";
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
/// Owed's limits (`khipu.config.COUNT_SETTINGS`): whole numbers of 0 or
/// more, 0 for no limit, each `config --set KEY N` or `config --unset=KEY`.
const OWED_COUNT_KEYS: &[&str] = &["owed_orchestrator_slice_max", "owed_page_max", "owed_bulk_max", "owed_tidy_calls"];
/// Whether Owed's close rules ask GitHub, read-only, if a pull request merged.
const OWED_GITHUB_KEY: &str = "owed_pr_check_github";

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
/// Settings -> Data, "This Mac's copy": how old the copy may get before the
/// background job updates it, how often the job looks, and how long a failed
/// database connection is remembered. Lengths of time in
/// whole seconds (the page shows minutes); `config --set KEY N` or
/// `config --unset=KEY` (the default again).
const REPLICA_DURATION_KEYS: &[&str] = &["replica_refresh_after", "replica_check_every", "hub_down_memo_for"];

/// A whole number of seconds: digits only, at most ten years, and the check
/// interval at least a minute (the CLI's own floor).
fn replica_setting_value(key: &str, value: &str) -> Result<String, String> {
    let v = value.trim();
    let n: u64 = if !v.is_empty() && v.len() <= 12 && v.chars().all(|c| c.is_ascii_digit()) {
        v.parse().unwrap_or(u64::MAX)
    } else {
        u64::MAX
    };
    if n > 10 * 366 * 86_400 {
        return Err(format!("{key} must be a whole number of seconds, got {value:?}"));
    }
    if key == "replica_check_every" && n < 60 {
        return Err(format!("{key} must be at least 60 seconds"));
    }
    Ok(n.to_string())
}
/// Settings -> Health checks, "Storage" (slice 8): the free-space thresholds,
/// how long backups and usage lines are kept, and the log size cap. Each is
/// `config --set KEY N` or `config --unset=KEY`. Tuples are (key, largest
/// allowed value, whole numbers only); 0 is allowed everywhere.
const STORAGE_KEYS: &[(&str, f64, bool)] = &[
    ("disk_warn_free_gb", 1_000_000.0, false),
    ("disk_warn_free_percent", 100.0, false),
    ("disk_fail_free_gb", 1_000_000.0, false),
    ("disk_fail_free_percent", 100.0, false),
    ("commitments_backup_keep_days", 36_500.0, true),
    ("synth_usage_keep_days", 36_500.0, true),
    ("log_max_mb", 1_000_000.0, false),
];

fn storage_key(key: &str) -> bool {
    STORAGE_KEYS.iter().any(|(k, _, _)| *k == key)
}

/// A plain non-negative number (digits, one optional decimal part), within the
/// key's largest value; the day keys take whole numbers only.
fn storage_setting_value(key: &str, raw: &str) -> Result<String, String> {
    let (_, high, whole) = STORAGE_KEYS
        .iter()
        .find(|(k, _, _)| *k == key)
        .ok_or_else(|| format!("not a storage setting: {key:?}"))?;
    let v = raw.trim();
    let (int, frac) = match v.split_once('.') {
        Some((i, f)) => (i, Some(f)),
        None => (v, None),
    };
    let digits = |t: &str| !t.is_empty() && t.len() <= 12 && t.bytes().all(|b| b.is_ascii_digit());
    let shaped = digits(int) && frac.map_or(true, |f| !*whole && digits(f));
    let n: f64 = if shaped { v.parse().unwrap_or(f64::MAX) } else { f64::MAX };
    if n > *high {
        return Err(if *whole {
            format!("{key} must be a whole number from 0 to {high}, got {raw:?}")
        } else {
            format!("{key} must be a number from 0 to {high}, got {raw:?}")
        });
    }
    Ok(v.to_string())
}
/// Settings -> Work (Work view phase 5): every cap and time of the live view
/// and the ledger, each `config --set KEY N` or `config --unset=KEY`; 0 lifts
/// a cap. Whole numbers of 0 or more.
const WORK_COUNT_KEYS: &[&str] = &[
    "work_file_max_bytes",
    "work_max_jobs",
    "work_max_prs",
    "work_max_trains",
    "work_max_runs",
    "work_line_chars",
    "ledger_id_chars",
    "ledger_title_chars",
    "ledger_evidence_chars",
];
/// Lengths of time, in whole seconds.
const WORK_DURATION_KEYS: &[&str] = &[
    "work_job_quiet_after",
    "work_gone_after",
    "work_finished_for",
    "work_merged_for",
    "work_clock_slack",
    "work_refresh",
    "ledger_stale_after",
    "ledger_purge_after",
];
/// Seconds, or `auto` (five of the runner's own intervals).
const WORK_STALE_KEY: &str = "work_stale_after";
const WORK_PROJECTS_KEY: &str = "work_projects";
const WORK_PROJECTS_MAX: usize = 64;
const LEDGER_PROJECT_NAME_CHARS: usize = 80;

fn work_key(key: &str) -> bool {
    WORK_COUNT_KEYS.contains(&key) || WORK_DURATION_KEYS.contains(&key) || key == WORK_STALE_KEY || key == WORK_PROJECTS_KEY
}

fn work_setting_value(key: &str, raw: &str) -> Result<String, String> {
    let v = raw.trim();
    if key == WORK_PROJECTS_KEY {
        return checked_work_projects(v);
    }
    if key == WORK_STALE_KEY && v == "auto" {
        return Ok("auto".to_string());
    }
    let n: u64 = if !v.is_empty() && v.len() <= 12 && v.bytes().all(|b| b.is_ascii_digit()) {
        v.parse().map_err(|_| format!("{key} is too large"))?
    } else {
        return Err(format!("{key} must be a whole number of 0 or more, got {raw:?}"));
    };
    let most: u64 = if WORK_COUNT_KEYS.contains(&key) { 10_000_000_000 } else { 10 * 366 * 86_400 };
    if n > most {
        return Err(format!("{key} is too large"));
    }
    Ok(n.to_string())
}

/// The summary files, parsed and re-serialised: only `{project, name?, file,
/// on, show_now}` with a full path reaches argv. The CLI refuses a file inside
/// a private folder and a project key it can't take.
fn checked_work_projects(raw: &str) -> Result<String, String> {
    let bad = || "summary files must be a JSON list of {project, name, file, on, show_now}".to_string();
    let value: serde_json::Value = serde_json::from_str(raw).map_err(|_| bad())?;
    let entries = value.as_array().ok_or_else(bad)?;
    if entries.len() > WORK_PROJECTS_MAX {
        return Err(format!("at most {WORK_PROJECTS_MAX} summary files"));
    }
    let mut out = Vec::with_capacity(entries.len());
    for (i, entry) in entries.iter().enumerate() {
        let n = i + 1;
        let obj = entry.as_object().ok_or_else(bad)?;
        if obj.keys().any(|k| !matches!(k.as_str(), "project" | "name" | "file" | "on" | "show_now")) {
            return Err(format!("summary file {n} has a field Khipu doesn't keep"));
        }
        let project = checked_ledger_project(obj.get("project").and_then(|p| p.as_str()).unwrap_or(""))?;
        let file = checked_routing_path(
            &format!("summary file {n}"),
            obj.get("file").and_then(|f| f.as_str()).ok_or_else(|| format!("summary file {n} needs its full path"))?,
        )?;
        let switch = |k: &str| -> Result<bool, String> {
            match obj.get(k) {
                None => Ok(true),
                Some(serde_json::Value::Bool(b)) => Ok(*b),
                Some(_) => Err(format!("summary file {n}: {k} must be true or false")),
            }
        };
        let mut clean = serde_json::Map::new();
        clean.insert("project".into(), project.into());
        if let Some(name) = obj.get("name").filter(|v| v.as_str().is_some_and(|s| !s.trim().is_empty())) {
            clean.insert("name".into(), routing_json_text(&format!("summary file {n}'s name"), name, LEDGER_PROJECT_NAME_CHARS)?.into());
        }
        clean.insert("file".into(), file.into());
        clean.insert("on".into(), switch("on")?.into());
        clean.insert("show_now".into(), switch("show_now")?.into());
        out.push(serde_json::Value::Object(clean));
    }
    serde_json::to_string(&out).map_err(|e| e.to_string())
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
/// "When the hub is away" (offline slice 6): lengths of time in whole
/// seconds, set and reset like the routing durations but by the app's own
/// save of an ordinary setting (no `--from-app`). Mirrors
/// `offline.SETTINGS` in the CLI.
const OFFLINE_DURATION_KEYS: &[&str] = &["hub_statement_timeout", "hub_dead_connection_after", "app_cli_timeout"];
/// The hub's search role (swarm-ready A): whole numbers of 0 or more, an
/// ordinary save the CLI applies on the hub before storing. Mirrors
/// `read_role.SETTINGS` in the CLI.
const READ_ROLE_KEYS: &[&str] = &[
    "read_role_connection_limit",
    "read_role_statement_timeout",
    "read_role_idle_in_transaction_timeout",
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

/// `config --set KEY N` / `config --unset=KEY` for a length of time in whole
/// minutes (`khipu.config.MINUTE_SETTINGS`), with each key's allowed range.
const MINUTE_KEYS: &[(&str, u64, u64)] =
    &[("doctor_rate_window_minutes", 5, 7 * 24 * 60), ("promote_hub_wait_minutes", 0, 240)];

/// The owner's one-time "Update anyway" (`khipu.config` key
/// `promote_gate_ignore`): the red checks, comma-separated, that the next
/// promotion skips once. It clears itself after that run, or after a day.
/// The owner's only, so the app's save carries `--from-app`.
const GATE_IGNORE_KEY: &str = "promote_gate_ignore";

/// A setting only the owner changes: the app's save says `--from-app`.
fn owner_key(key: &str) -> bool {
    routing_key(key) || key == GATE_IGNORE_KEY
}

fn parse_gate_ignore(raw: &str) -> Result<String, String> {
    let names: Vec<&str> = raw.split(',').map(str::trim).filter(|n| !n.is_empty()).collect();
    if names.is_empty() {
        return Err(format!("{GATE_IGNORE_KEY} must name at least one check; cancel it to clear it"));
    }
    if names.len() > 32 {
        return Err(format!("{GATE_IGNORE_KEY} names too many checks"));
    }
    for n in &names {
        if n.is_empty() || n.len() > 40 || !n.bytes().all(|b| b.is_ascii_lowercase() || b == b'_') {
            return Err(format!("not a check name: {n:?}"));
        }
    }
    Ok(names.join(","))
}

fn minute_key(key: &str) -> Option<(u64, u64)> {
    MINUTE_KEYS.iter().find(|(k, _, _)| *k == key).map(|(_, low, high)| (*low, *high))
}

fn parse_minutes(key: &str, raw: &str) -> Result<String, String> {
    let (low, high) = minute_key(key).ok_or_else(|| format!("not a minutes setting: {key:?}"))?;
    let v: u64 = raw
        .trim()
        .parse()
        .map_err(|_| format!("{key} must be a whole number of minutes, got {raw:?}"))?;
    if !(low..=high).contains(&v) {
        return Err(format!("{key} must be between {low} and {high} minutes, got {raw:?}"));
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

/// As `checked_usage_accounts`, with `stored` the list saved now: an entry
/// the owner left exactly as it is stored (the same JSON value, at its own
/// place or, after a reorder, at another) goes through untouched, whatever
/// rule it breaks today (as the CLI keeps it); each stored entry answers
/// one sent entry at most. Only entries he added or changed are checked
/// here.
fn checked_usage_accounts_kept(raw: &str, stored: &[Value]) -> Result<String, String> {
    let not_a_list = || "usage_accounts must be a JSON list of accounts".to_string();
    let value: serde_json::Value = serde_json::from_str(raw.trim()).map_err(|_| not_a_list())?;
    let entries = value.as_array().ok_or_else(not_a_list)?;
    if entries.len() > USAGE_ACCOUNTS_MAX {
        return Err(format!("usage_accounts holds at most {USAGE_ACCOUNTS_MAX} entries"));
    }
    let mut used = vec![false; stored.len()];
    for (i, entry) in entries.iter().enumerate() {
        if stored.get(i) == Some(entry) {
            used[i] = true;
        }
    }
    let mut out = Vec::with_capacity(entries.len());
    for (i, entry) in entries.iter().enumerate() {
        let n = i + 1;
        if stored.get(i) == Some(entry) {
            out.push(entry.clone());
            continue;
        }
        if let Some(j) = (0..stored.len()).find(|&j| !used[j] && &stored[j] == entry) {
            used[j] = true;
            out.push(entry.clone());
            continue;
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
    let most: u64 = if ROUTING_DURATION_KEYS.contains(&key) || OFFLINE_DURATION_KEYS.contains(&key) { 10 * 366 * 86_400 } else { 100_000_000 };
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
    // A routing setting and the one-time "Update anyway" are the owner's
    // (decision 15): the app's own save says so, or the CLI refuses it as a
    // conversation's.
    let set = |k: &str, v: String| {
        let mut argv = vec!["config".to_string()];
        if owner_key(k) {
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
        k if minute_key(k).is_some() => Ok(set(k, parse_minutes(k, value)?)),
        GATE_IGNORE_KEY => Ok(set(GATE_IGNORE_KEY, parse_gate_ignore(value)?)),
        "synth_fallback" => {
            let v = value.trim();
            if !SYNTH_FALLBACKS.contains(&v) {
                return Err(format!("synth_fallback must be gemini or none, got {value:?}"));
            }
            Ok(set("synth_fallback", v.to_string()))
        }
        GRAPH_BACKUP_REMOTE_KEY => Ok(set(GRAPH_BACKUP_REMOTE_KEY, checked_backup_remote(value)?)),
        k if OWED_COUNT_KEYS.contains(&k) => Ok(set(k, parse_daily_calls(k, value)?)),
        OWED_GITHUB_KEY => {
            let v = value.trim();
            if v != "on" && v != "off" {
                return Err(format!("{OWED_GITHUB_KEY} must be on or off, got {value:?}"));
            }
            Ok(set(OWED_GITHUB_KEY, v.to_string()))
        }
        k if PATH_KEYS.contains(&k) => Ok(set(k, checked_operand(key, value, false)?)),
        k if cli_setting_key(k) => Ok(set(k, checked_cli_setting_path(k, value)?)),
        k if routing_key(k) => Ok(set(k, routing_setting_value(k, value)?)),
        k if REPLICA_DURATION_KEYS.contains(&k) => Ok(set(k, replica_setting_value(k, value)?)),
        k if OFFLINE_DURATION_KEYS.contains(&k) => Ok(set(k, routing_setting_value(k, value)?)),
        k if READ_ROLE_KEYS.contains(&k) => Ok(set(k, parse_daily_calls(k, value)?)),
        k if storage_key(k) => Ok(set(k, storage_setting_value(k, value)?)),
        k if work_key(k) => Ok(set(k, work_setting_value(k, value)?)),
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
        && !OWED_COUNT_KEYS.contains(&key)
        && key != OWED_GITHUB_KEY
        && minute_key(key).is_none()
        && key != GATE_IGNORE_KEY
        && !cli_setting_key(key)
        && !routing_key(key)
        && !REPLICA_DURATION_KEYS.contains(&key)
        && !OFFLINE_DURATION_KEYS.contains(&key)
        && !READ_ROLE_KEYS.contains(&key)
        && !storage_key(key)
        && !work_key(key)
        && !by_profile
    {
        return Err(format!("not a resettable key: {key:?}"));
    }
    if owner_key(key) {
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

// --- Settings clarity phase 9 (9C): finding and adding accounts. Reads for
// the app only (they carry login-folder paths); each is a fixed argv built
// here, every value joined to its flag with `=` so it can never read as a
// flag of its own. The app's account writes are 9B's `khipu_accounts_*`.

fn cli_setting_key(key: &str) -> bool {
    CLI_PATH_KEYS.contains(&key) || key == NODE_FOLDER_KEY
}

/// A CLI or Node folder the owner picked: a full path (or `~/...`), one line,
/// never a flag. The CLI checks again that it is an executable file (a CLI)
/// or a folder holding an executable `node`.
fn checked_cli_setting_path(key: &str, raw: &str) -> Result<String, String> {
    let v = raw.trim();
    if !(v.starts_with('/') || v.starts_with("~/")) {
        return Err(format!("{key} must be a full path starting with / or ~/"));
    }
    if has_control(v) || v.chars().count() > 4096 {
        return Err(format!("{key} must be one line of plain text"));
    }
    Ok(v.to_string())
}

/// A login folder for Check or one read: `~/...` or a full path, one line.
/// A relative folder, a flag-shaped value or a control character is refused
/// before anything runs.
fn checked_login_folder(raw: &str) -> Result<String, String> {
    let v = raw.trim();
    if v.starts_with('-') {
        return Err("a login folder must not start with '-'".to_string());
    }
    if !(v.starts_with("~/") || v.starts_with('/')) {
        return Err("a login folder starts with ~/ or is a full path starting with /".to_string());
    }
    if has_control(v) || v.chars().count() > 1024 {
        return Err("a login folder must be one line of plain text".to_string());
    }
    Ok(v.to_string())
}

fn checked_usage_harness(raw: &str) -> Result<String, String> {
    let h = raw.trim().to_lowercase();
    if USAGE_HARNESSES.contains(&h.as_str()) {
        Ok(h)
    } else {
        Err("harness must be claude, codex, grok or muse".to_string())
    }
}

fn accounts_discover_argv(now: bool) -> Vec<String> {
    fixed_argv(if now { ACCOUNTS_DISCOVER_NOW_ARGV } else { ACCOUNTS_DISCOVER_ARGV })
}

/// The owner's Check on one found folder (decision 20: the CLI may write
/// into it when asked, so it runs only when the owner presses Check).
fn accounts_check_argv(folder: &str) -> Result<Vec<String>, String> {
    Ok(vec![
        "usage".to_string(),
        "--discover".to_string(),
        format!("--check={}", checked_login_folder(folder)?),
        "--json".to_string(),
    ])
}

/// One read of an account that is not saved (the dialog's step 3). No folder
/// is the CLI's default login; Muse takes none.
fn accounts_try_argv(harness: &str, folder: Option<&str>) -> Result<Vec<String>, String> {
    let h = checked_usage_harness(harness)?;
    let mut argv = vec!["usage".to_string(), "--try".to_string(), format!("--harness={h}")];
    if let Some(f) = folder.map(str::trim).filter(|f| !f.is_empty()) {
        if h == "muse" {
            return Err("Muse has one sign-in, so it takes no login folder".to_string());
        }
        argv.push(format!("--folder={}", checked_login_folder(f)?));
    }
    Ok(argv)
}

#[tauri::command]
async fn khipu_accounts_discover(now: Option<bool>) -> Result<String, String> {
    run_khipu_cli_async(accounts_discover_argv(now.unwrap_or(false))).await
}

#[tauri::command]
async fn khipu_accounts_check(folder: String) -> Result<String, String> {
    run_khipu_cli_async(accounts_check_argv(&folder)?).await
}

#[tauri::command]
async fn khipu_accounts_try(harness: String, folder: Option<String>) -> Result<String, String> {
    run_khipu_cli_async(accounts_try_argv(&harness, folder.as_deref())?).await
}

#[tauri::command]
async fn khipu_accounts_forget_login_paths() -> Result<String, String> {
    run_khipu_cli_async(fixed_argv(ACCOUNTS_FORGET_LOGIN_PATHS_ARGV)).await
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
/// The format is defined once, in packages/cli/khipu/config.py (`is_version`):
/// ASCII letters and digits in parts joined by single dots, at most 64
/// characters (`p12.a4def5fc278cfc609`). A pytest runs this function on
/// versions the Python code makes, so the two can never disagree.
fn checked_expect(raw: &str) -> Result<&str, String> {
    let v = raw.trim();
    let parts_ok = v.split('.').all(|p| !p.is_empty() && p.chars().all(|c| c.is_ascii_alphanumeric()));
    if !v.is_empty() && v.len() <= 64 && parts_ok {
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

// --- Settings -> Owner actions (owner direction, scope 2.5 and section 4).
// The list of every owner change, one Undo per line,
// and the check's own settings. The app is the owner, so every write carries
// `--from-app` and applies at once; each value is checked here and travels
// after `--`, so nothing the webview sends can become a flag.

const OWNER_DIRECTION_SHOW_ARGV: &[&str] = &["owner-direction", "show"];
const OWNER_DIRECTION_ACTIONS_ARGV: &[&str] = &["owner-direction", "actions", "--json"];
const OWNER_DIRECTION_ACKNOWLEDGE_ARGV: &[&str] = &["owner-direction", "acknowledge", "--from-app"];
/// The keys `khipu owner-direction set` takes.
const OWNER_DIRECTION_KEYS: &[&str] = &["latest_window_h", "session_binding", "trust", "untrust"];

/// An owner-actions log line's id: 8 lowercase hex characters.
fn owner_direction_undo_argv(id: &str) -> Result<Vec<String>, String> {
    let ok = id.len() == 8 && id.chars().all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c));
    if !ok {
        return Err("that is not an owner action Khipu knows".to_string());
    }
    Ok(fixed_argv(&["owner-direction", "undo", "--from-app", "--", id]))
}

fn owner_direction_switch_argv(on: bool) -> Vec<String> {
    fixed_argv(&["owner-direction", if on { "on" } else { "off" }, "--from-app"])
}

fn owner_direction_set_argv(key: &str, value: &str) -> Result<Vec<String>, String> {
    if !OWNER_DIRECTION_KEYS.contains(&key) {
        return Err(format!("not an owner direction setting: {key:?}"));
    }
    let value = value.trim();
    let ok = match key {
        "latest_window_h" => {
            value.len() <= 12
                && value.chars().all(|c| c.is_ascii_digit() || c == '.')
                && value.parse::<f64>().is_ok_and(|n| n > 0.0)
        }
        "session_binding" => value == "on" || value == "off",
        _ => match value.split_once(':') {
            Some((harness, entry)) => {
                value.len() <= 64
                    && !harness.is_empty()
                    && !entry.is_empty()
                    && harness.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
                    && entry.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
            }
            None => false,
        },
    };
    if !ok {
        return Err(match key {
            "session_binding" => "choose on or off".to_string(),
            "trust" | "untrust" => "that is not a front end Khipu knows".to_string(),
            _ => "enter a number above 0".to_string(),
        });
    }
    Ok(fixed_argv(&["owner-direction", "set", "--from-app", "--", key, value]))
}

#[tauri::command]
async fn khipu_owner_direction_show() -> Result<String, String> {
    run_khipu_cli_async(fixed_argv(OWNER_DIRECTION_SHOW_ARGV)).await
}

#[tauri::command]
async fn khipu_owner_direction_actions() -> Result<String, String> {
    run_khipu_cli_async(fixed_argv(OWNER_DIRECTION_ACTIONS_ARGV)).await
}

#[tauri::command]
async fn khipu_owner_direction_acknowledge() -> Result<String, String> {
    run_khipu_cli_async(fixed_argv(OWNER_DIRECTION_ACKNOWLEDGE_ARGV)).await
}

#[tauri::command]
async fn khipu_owner_direction_undo(id: String) -> Result<String, String> {
    run_khipu_cli_async(owner_direction_undo_argv(&id)?).await
}

#[tauri::command]
async fn khipu_owner_direction_switch(on: bool) -> Result<String, String> {
    run_khipu_cli_async(owner_direction_switch_argv(on)).await
}

#[tauri::command]
async fn khipu_owner_direction_set(key: String, value: String) -> Result<String, String> {
    run_khipu_cli_async(owner_direction_set_argv(&key, &value)?).await
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

// ---- Work: the live view and the ledger (docs/plans/2026-10-15-work-view.md, phase 4)
//
// Fixed argv for the Work tab and a project's ledger page. `work` and
// `ledger` stay out of `ALLOWED_SUBCOMMANDS`: the webview names one of these
// commands and its checked fields, never an argv. Every value that takes a
// flag travels joined to it (`--project=...`), and a row id travels after
// `--`, so nothing the webview sends can become a flag. Every write carries
// `--from-app`, so the ledger records it as "you, in the app".

/// The most text the app hands the CLI in one ledger field, in bytes. Not a
/// limit on the ledger (the CLI applies the owner's `ledger_*` limits); it
/// keeps one argument well inside what a program can be started with.
const LEDGER_TEXT_MAX_BYTES: usize = 64 * 1024;
/// `--children` choices `khipu ledger remove` accepts.
const LEDGER_CHILDREN: &[&str] = &["remove", "move-up"];

/// A ledger project key (`owner/repo`, a name): one line of at most 200
/// characters without `#`, as `ledger._check_project` takes it.
fn checked_ledger_project(raw: &str) -> Result<String, String> {
    let v = raw.trim();
    if v.is_empty() || v.chars().count() > 200 || v.contains('#') || has_control(v) {
        return Err("that is not a ledger project Khipu knows".to_string());
    }
    Ok(v.to_string())
}

/// A ledger row id: a letter or digit, then letters, digits and `. _ : / -`.
/// Not trimmed: ids are kept exactly as stored, so a stray space or line
/// break is refused rather than quietly naming another row.
fn checked_ledger_id(raw: &str) -> Result<String, String> {
    let v = raw;
    let ok = !v.is_empty()
        && v.len() <= 1024
        && v.chars().next().is_some_and(|c| c.is_ascii_alphanumeric())
        && v.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | ':' | '/' | '-'));
    if ok {
        Ok(v.to_string())
    } else {
        Err("that is not a ledger row Khipu knows".to_string())
    }
}

/// The owner's words for one field. `lines` allows line breaks (a note);
/// every other control character is refused.
fn checked_ledger_text(raw: &str, lines: bool) -> Result<String, String> {
    if raw.len() > LEDGER_TEXT_MAX_BYTES {
        return Err(format!(
            "this is longer than the app can hand over in one change ({} KB of text); shorten it",
            LEDGER_TEXT_MAX_BYTES / 1024
        ));
    }
    if raw.chars().any(|c| c.is_control() && !(lines && (c == '\n' || c == '\t'))) {
        return Err(if lines {
            "the words hold a character Khipu can't keep".to_string()
        } else {
            "keep it to one line".to_string()
        });
    }
    Ok(raw.to_string())
}

/// A commitment's id (`khipu owed`): digits only.
fn checked_commitment_id(raw: &str) -> Result<String, String> {
    let v = raw.trim();
    if !v.is_empty() && v.len() <= 20 && v.chars().all(|c| c.is_ascii_digit()) {
        Ok(v.to_string())
    } else {
        Err("that is not a commitment Khipu knows".to_string())
    }
}

fn ledger_base(verb: &str, project: &str, write: bool) -> Result<Vec<String>, String> {
    let mut argv = vec!["ledger".to_string(), verb.to_string(), "--json".to_string()];
    if write {
        argv.push("--from-app".to_string());
    }
    argv.push(format!("--project={}", checked_ledger_project(project)?));
    Ok(argv)
}

fn work_view_argv() -> Vec<String> {
    vec!["work".to_string(), "--json".to_string()]
}

fn ledger_list_argv(project: &str) -> Result<Vec<String>, String> {
    ledger_base("list", project, false)
}

fn ledger_get_argv(project: &str, id: &str) -> Result<Vec<String>, String> {
    let mut argv = ledger_base("get", project, false)?;
    argv.extend(["--".to_string(), checked_ledger_id(id)?]);
    Ok(argv)
}

fn ledger_removed_argv(project: &str) -> Result<Vec<String>, String> {
    ledger_base("removed", project, false)
}

/// One change to a row from the app. A field left out (None) is not
/// changed; `if_state` is the state the app showed, so a row another writer
/// moved meanwhile is refused instead of overwritten.
#[derive(serde::Deserialize, Debug, Clone, Default)]
struct LedgerChange {
    state: Option<String>,
    evidence: Option<String>,
    owner: Option<String>,
    title: Option<String>,
    link: Option<String>,
    note: Option<String>,
    if_state: Option<String>,
}

fn ledger_set_argv(project: &str, id: &str, change: &LedgerChange) -> Result<Vec<String>, String> {
    let mut argv = ledger_base("set", project, true)?;
    let mut any = false;
    for (flag, value) in [
        ("state", &change.state),
        ("evidence", &change.evidence),
        ("owner", &change.owner),
        ("title", &change.title),
        ("link", &change.link),
    ] {
        if let Some(v) = value {
            argv.push(format!("--{flag}={}", checked_ledger_text(v, false)?));
            any = true;
        }
    }
    if let Some(n) = change.note.as_deref().filter(|n| !n.trim().is_empty()) {
        argv.push(format!("--note={}", checked_ledger_text(n, true)?));
        any = true;
    }
    if !any {
        return Err("change a field or write a note first".to_string());
    }
    if let Some(s) = change.if_state.as_deref().filter(|s| !s.trim().is_empty()) {
        argv.push(format!("--if-state={}", checked_ledger_text(s, false)?));
    }
    argv.extend(["--".to_string(), checked_ledger_id(id)?]);
    Ok(argv)
}

fn ledger_remove_argv(project: &str, id: &str, note: Option<&str>, children: Option<&str>) -> Result<Vec<String>, String> {
    let mut argv = ledger_base("remove", project, true)?;
    if let Some(c) = children.filter(|c| !c.is_empty()) {
        if !LEDGER_CHILDREN.contains(&c) {
            return Err(format!("not a choice for the rows under it: {c:?}"));
        }
        argv.push(format!("--children={c}"));
    }
    if let Some(n) = note.filter(|n| !n.trim().is_empty()) {
        argv.push(format!("--note={}", checked_ledger_text(n, true)?));
    }
    argv.extend(["--".to_string(), checked_ledger_id(id)?]);
    Ok(argv)
}

fn ledger_restore_argv(project: &str, id: &str) -> Result<Vec<String>, String> {
    let mut argv = ledger_base("restore", project, true)?;
    argv.extend(["--".to_string(), checked_ledger_id(id)?]);
    Ok(argv)
}

/// The ledger inbox, `<data dir>/ledger-inbox/` (`paths.ledger_inbox`): where
/// a project's orchestrator copies its old tracker, and where Choose file
/// opens. Khipu never writes, moves or deletes the original.
fn ledger_inbox_dir(data_dir: &Path) -> PathBuf {
    data_dir.join("ledger-inbox")
}

/// A tracker file to import: one full path as the file dialog gives it, not
/// trimmed, with no control character. The CLI's own reader (`safe_read`)
/// decides the rest, the login-folder refusal included.
fn checked_tracker_path(raw: &str) -> Result<String, String> {
    if !raw.starts_with('/') || raw.len() > 4096 || raw.ends_with('/') || has_control(raw) {
        return Err("choose the tracker file again; that is not a full path to one file".to_string());
    }
    Ok(raw.to_string())
}

/// `ledger import --json --from-app --project=P [--dry-run] -- PATH`: Check
/// this file (a dry run, nothing written) and Import. The path travels after
/// `--`, so it can never become a flag.
fn ledger_import_argv(project: &str, path: &str, dry_run: bool) -> Result<Vec<String>, String> {
    let mut argv = ledger_base("import", project, true)?;
    if dry_run {
        argv.push("--dry-run".to_string());
    }
    argv.extend(["--".to_string(), checked_tracker_path(path)?]);
    Ok(argv)
}

/// `work hide|unhide -- ID`: Khipu's own record of a commitment hidden from
/// Waiting on you until its words change.
fn work_hide_argv(id: &str, hide: bool) -> Result<Vec<String>, String> {
    let verb = if hide { "hide" } else { "unhide" };
    Ok(vec!["work".to_string(), verb.to_string(), "--".to_string(), checked_commitment_id(id)?])
}

/// `work same|not-same -- PROJECT#ROW COMMITMENT`: the owner's answer to
/// "Looks like".
fn work_same_argv(project: &str, row: &str, commitment: &str, same: bool) -> Result<Vec<String>, String> {
    let verb = if same { "same" } else { "not-same" };
    Ok(vec![
        "work".to_string(),
        verb.to_string(),
        "--".to_string(),
        format!("{}#{}", checked_ledger_project(project)?, checked_ledger_id(row)?),
        checked_commitment_id(commitment)?,
    ])
}

// ---- Owed: several items at once, and the one-time clean-up
// (docs/plans/2026-10-16-owed-and-ledger-ssot.md, slice 5)
//
// Fixed argv for the changes Owed makes to many commitments. `owed` has no
// `--from-app` flag: the CLI knows the app started it by walking its own
// parents (`routing._caller`), which is what lets a change to several of the
// owner's own items pass the owner gate. Ids are digits only and travel
// joined to their flag; the reason is one line joined to `--reason=`, so
// nothing the webview sends can become a flag.

/// The most ids one change carries, so the argument stays well inside what
/// a program can be started with. Not Owed's own limit: the CLI applies
/// `owed_bulk_max`, which the owner can lift in Settings.
const OWED_IDS_MAX: usize = 10_000;
/// The longest reason the app hands over, in characters.
const OWED_REASON_MAX_CHARS: usize = 500;
/// The longest label for an agent owner ("orchestrator", "PR author").
const OWED_LABEL_MAX_CHARS: usize = 60;
const OWED_OWNERS: &[&str] = &["user", "assistant", "agent"];

/// `owed --close=IDS --reason=R` or `owed --reassign=IDS --to=OWNER
/// [--label=L] --reason=R`: one call, one transaction, one result per id.
fn owed_change_argv(
    action: &str,
    ids: &[String],
    reason: &str,
    to: Option<&str>,
    label: Option<&str>,
) -> Result<Vec<String>, String> {
    if ids.is_empty() {
        return Err("choose at least one item".to_string());
    }
    if ids.len() > OWED_IDS_MAX {
        return Err(format!("the app changes at most {OWED_IDS_MAX} items at once; choose fewer"));
    }
    let mut seen: Vec<String> = Vec::with_capacity(ids.len());
    for raw in ids {
        let id = checked_commitment_id(raw)?;
        if id.trim_start_matches('0').is_empty() {
            return Err("that is not a commitment Khipu knows".to_string());
        }
        if !seen.contains(&id) {
            seen.push(id);
        }
    }
    let reason = reason.trim();
    if reason.is_empty() {
        return Err("say why, in a few words".to_string());
    }
    if reason.chars().count() > OWED_REASON_MAX_CHARS || has_control(reason) {
        return Err(format!("keep the reason to one line of at most {OWED_REASON_MAX_CHARS} characters"));
    }
    let joined = seen.join(",");
    let mut argv = vec!["owed".to_string()];
    match action {
        "close" => {
            if to.is_some() || label.is_some_and(|l| !l.trim().is_empty()) {
                return Err("a close takes no new owner".to_string());
            }
            argv.push(format!("--close={joined}"));
        }
        "reassign" => {
            let owner = to.map(str::trim).unwrap_or("");
            if !OWED_OWNERS.contains(&owner) {
                return Err("choose who it moves to".to_string());
            }
            argv.push(format!("--reassign={joined}"));
            argv.push(format!("--to={owner}"));
            if let Some(l) = label.map(str::trim).filter(|l| !l.is_empty()) {
                if owner != "agent" {
                    return Err("only an agent or a job takes a label".to_string());
                }
                if l.chars().count() > OWED_LABEL_MAX_CHARS || has_control(l) {
                    return Err(format!("keep the label to one line of at most {OWED_LABEL_MAX_CHARS} characters"));
                }
                argv.push(format!("--label={l}"));
            }
        }
        _ => return Err(format!("not a change Owed makes to several items: {action:?}")),
    }
    argv.push(format!("--reason={reason}"));
    Ok(argv)
}

/// Where the app keeps a project's clean-up plan between Make a plan and
/// Apply, inside Khipu's own data folder: the webview never names a path,
/// and Apply applies exactly the plan it showed. The file name is the
/// project's safe characters plus a hash of the whole key, so two projects
/// never share one.
fn owed_tidy_plan_path(data_dir: &Path, project: &str) -> PathBuf {
    let safe: String = project
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '-' { c } else { '_' })
        .take(60)
        .collect();
    // FNV-1a, 64 bit: stable across builds, unlike the std hasher.
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in project.as_bytes() {
        h ^= u64::from(*b);
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    data_dir.join("state").join("owed-tidy").join(format!("plan-{safe}-{h:016x}.json"))
}

fn owed_tidy_base(project: &str) -> Result<Vec<String>, String> {
    Ok(vec!["owed".to_string(), "tidy".to_string(), format!("--project={}", checked_ledger_project(project)?)])
}

fn owed_tidy_plan_argv(project: &str, plan: &Path) -> Result<Vec<String>, String> {
    let mut argv = owed_tidy_base(project)?;
    argv.push(format!("--plan-out={}", plan.to_string_lossy()));
    Ok(argv)
}

fn owed_tidy_apply_argv(project: &str, plan: &Path) -> Result<Vec<String>, String> {
    let mut argv = owed_tidy_base(project)?;
    argv.push(format!("--apply={}", plan.to_string_lossy()));
    Ok(argv)
}

/// A clean-up batch id as `owed_tidy.apply` makes it (`tidy-<time>-<hex>`).
fn owed_tidy_undo_argv(project: &str, batch: &str) -> Result<Vec<String>, String> {
    let ok = batch.starts_with("tidy-")
        && batch.len() <= 64
        && batch.chars().all(|c| c.is_ascii_alphanumeric() || c == '-');
    if !ok {
        return Err("that is not a clean-up Khipu knows".to_string());
    }
    let mut argv = owed_tidy_base(project)?;
    argv.push(format!("--undo={batch}"));
    Ok(argv)
}

/// The plan document for the preview: totals, samples, notes and the model
/// line, without the per-item list (thousands of rows the app never shows;
/// Apply reads them from the file). `items_count` says how many there were.
fn owed_tidy_plan_for_app(raw: &str) -> Result<String, String> {
    let mut doc: Value = serde_json::from_str(raw).map_err(|_| "the clean-up plan could not be read".to_string())?;
    if doc.get("format").and_then(Value::as_str) != Some("khipu-owed-tidy-plan") {
        return Err("the clean-up plan is not in a form this app reads".to_string());
    }
    let obj = doc.as_object_mut().ok_or_else(|| "the clean-up plan could not be read".to_string())?;
    let count = obj.remove("items").and_then(|i| i.as_array().map(Vec::len)).unwrap_or(0);
    obj.insert("items_count".to_string(), Value::from(count));
    serde_json::to_string(&doc).map_err(|e| e.to_string())
}

/// Khipu's data folder, as `app_cli_timeout_secs` finds it.
fn khipu_data_dir() -> Result<PathBuf, String> {
    data_dir_from_pointer()
        .or_else(|| std::env::var("HOME").ok().map(|h| PathBuf::from(h).join(".config/khipu")))
        .ok_or_else(|| "Khipu's data folder could not be found".to_string())
}

#[tauri::command]
async fn khipu_owed_change(
    action: String,
    ids: Vec<String>,
    reason: String,
    to: Option<String>,
    label: Option<String>,
) -> Result<String, String> {
    run_khipu_cli_async(owed_change_argv(&action, &ids, &reason, to.as_deref(), label.as_deref())?).await
}

#[tauri::command]
async fn khipu_owed_tidy_plan(project: String) -> Result<String, String> {
    let path = owed_tidy_plan_path(&khipu_data_dir()?, &project);
    let argv = owed_tidy_plan_argv(&project, &path)?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("could not make the folder for the plan: {e}"))?;
    }
    let _ = std::fs::remove_file(&path);
    let out = run_khipu_cli_async(argv).await?;
    let raw = match std::fs::read_to_string(&path) {
        Ok(raw) => raw,
        // No file: the CLI said why on stdout (a refusal is JSON with ok false).
        Err(_) => return Ok(out),
    };
    owed_tidy_plan_for_app(&raw)
}

#[tauri::command]
async fn khipu_owed_tidy_apply(project: String) -> Result<String, String> {
    let path = owed_tidy_plan_path(&khipu_data_dir()?, &project);
    if !path.is_file() {
        return Err("make a plan first; there is no plan for this project to apply".to_string());
    }
    run_khipu_cli_async(owed_tidy_apply_argv(&project, &path)?).await
}

#[tauri::command]
async fn khipu_owed_tidy_undo(project: String, batch: String) -> Result<String, String> {
    run_khipu_cli_async(owed_tidy_undo_argv(&project, &batch)?).await
}

fn ledger_projects_argv() -> Vec<String> {
    vec!["ledger".to_string(), "projects".to_string(), "--json".to_string()]
}

/// `ledger project`: the owner's rename (empty: the key again) or hide or
/// show of one project, from the app (`--from-app`, the owner gate).
fn ledger_project_argv(project: &str, name: Option<&str>, hidden: Option<bool>) -> Result<Vec<String>, String> {
    if name.is_none() && hidden.is_none() {
        return Err("say what to change about the project".to_string());
    }
    let mut argv = ledger_base("project", project, true)?;
    if let Some(n) = name {
        let n = n.trim();
        if n.chars().count() > LEDGER_PROJECT_NAME_CHARS || has_control(n) {
            return Err(format!("a project's name is one line of at most {LEDGER_PROJECT_NAME_CHARS} characters"));
        }
        argv.push(format!("--rename={n}"));
    }
    match hidden {
        Some(true) => argv.push("--hide".to_string()),
        Some(false) => argv.push("--show".to_string()),
        None => {}
    }
    Ok(argv)
}

fn work_hidden_argv() -> Vec<String> {
    vec!["work".to_string(), "hidden".to_string(), "--json".to_string()]
}

/// `work check --json -- FILE`: a summary file's first read before it is
/// added. Read-only; the CLI refuses a file inside a private folder.
fn work_check_argv(path: &str) -> Result<Vec<String>, String> {
    let file = checked_routing_path("the summary file", path)?;
    Ok(vec!["work".to_string(), "check".to_string(), "--json".to_string(), "--".to_string(), file])
}

#[tauri::command]
async fn khipu_ledger_projects() -> Result<String, String> {
    run_khipu_cli_async(ledger_projects_argv()).await
}

#[tauri::command]
async fn khipu_ledger_project_set(project: String, name: Option<String>, hidden: Option<bool>) -> Result<String, String> {
    run_khipu_cli_async(ledger_project_argv(&project, name.as_deref(), hidden)?).await
}

#[tauri::command]
async fn khipu_work_hidden() -> Result<String, String> {
    run_khipu_cli_async(work_hidden_argv()).await
}

#[tauri::command]
async fn khipu_work_check(path: String) -> Result<String, String> {
    run_khipu_cli_async(work_check_argv(&path)?).await
}

#[tauri::command]
async fn khipu_work_view() -> Result<String, String> {
    run_khipu_cli_async(work_view_argv()).await
}

#[tauri::command]
async fn khipu_ledger_list(project: String) -> Result<String, String> {
    run_khipu_cli_async(ledger_list_argv(&project)?).await
}

#[tauri::command]
async fn khipu_ledger_get(project: String, id: String) -> Result<String, String> {
    run_khipu_cli_async(ledger_get_argv(&project, &id)?).await
}

#[tauri::command]
async fn khipu_ledger_set(project: String, id: String, change: LedgerChange) -> Result<String, String> {
    run_khipu_cli_async(ledger_set_argv(&project, &id, &change)?).await
}

#[tauri::command]
async fn khipu_ledger_remove(
    project: String,
    id: String,
    note: Option<String>,
    children: Option<String>,
) -> Result<String, String> {
    run_khipu_cli_async(ledger_remove_argv(&project, &id, note.as_deref(), children.as_deref())?).await
}

#[tauri::command]
async fn khipu_ledger_removed(project: String) -> Result<String, String> {
    run_khipu_cli_async(ledger_removed_argv(&project)?).await
}

#[tauri::command]
async fn khipu_ledger_restore(project: String, id: String) -> Result<String, String> {
    run_khipu_cli_async(ledger_restore_argv(&project, &id)?).await
}

/// The ledger inbox's full path, made (owner only) when missing.
#[tauri::command]
async fn khipu_ledger_inbox() -> Result<String, String> {
    let inbox = ledger_inbox_dir(&khipu_data_dir()?);
    std::fs::create_dir_all(&inbox).map_err(|e| format!("Khipu couldn't make its ledger inbox: {e}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&inbox, std::fs::Permissions::from_mode(0o700));
    }
    Ok(inbox.to_string_lossy().into_owned())
}

#[tauri::command]
async fn khipu_ledger_import(project: String, path: String, dry_run: bool) -> Result<String, String> {
    run_khipu_cli_async(ledger_import_argv(&project, &path, dry_run)?).await
}

#[tauri::command]
async fn khipu_work_hide(id: String, hide: bool) -> Result<String, String> {
    run_khipu_cli_async(work_hide_argv(&id, hide)?).await
}

#[tauri::command]
async fn khipu_work_same(project: String, row: String, commitment: String, same: bool) -> Result<String, String> {
    run_khipu_cli_async(work_same_argv(&project, &row, &commitment, same)?).await
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

/// CLIs the new-account sign-in is offered for (docs/plans/2026-10-15-settings-clarity.md,
/// Guardrails). Codex joins after the Keychain proof (a second `CODEX_HOME`
/// keeps its own sign-in) and Grok Build after its device-code proof; until
/// then the builders below refuse them whatever the webview sends.
const SIGN_IN_NEW_OFFERED: &[&str] = &["claude"];
const SIGN_IN_NEW_NAME_MAX: usize = 64;
/// The CLI's `signin.SLUG_MAX`: a collision suffix (`-2`) counts inside it.
const SIGN_IN_NEW_SLUG_MAX: usize = 32;
/// How long quitting waits for the reaper before leaving it behind.
const SIGN_IN_REAP_WAIT: Duration = Duration::from_secs(4);

/// What Khipu itself made for the sign-in in progress: the login folder the
/// CLI returned (with its CLI) and the current attempt's id. The sign-in argv
/// is built from these, never from webview text.
#[derive(Default)]
struct SignInNewState {
    folder: Option<(String, String)>,
    attempt: Option<String>,
}

static SIGN_IN_NEW: Mutex<SignInNewState> = Mutex::new(SignInNewState { folder: None, attempt: None });

fn sign_in_state() -> std::sync::MutexGuard<'static, SignInNewState> {
    SIGN_IN_NEW.lock().unwrap_or_else(|e| e.into_inner())
}

fn sign_in_new_harness(harness: &str, offered: &[&str]) -> Result<(), String> {
    if !["claude", "codex", "grok"].contains(&harness) || !offered.contains(&harness) {
        return Err("Khipu can't sign in a new account for this CLI yet".to_string());
    }
    Ok(())
}

/// `usage --sign-in-folder --harness=H --name=N`: the CLI makes the login folder.
fn new_folder_argv(harness: &str, name: &str, offered: &[&str]) -> Result<Vec<String>, String> {
    sign_in_new_harness(harness, offered)?;
    let chars = name.chars().count();
    if name.trim().is_empty() || chars > SIGN_IN_NEW_NAME_MAX {
        return Err(format!("The name must be 1 to {SIGN_IN_NEW_NAME_MAX} characters"));
    }
    if has_control(name) || name.contains('|') {
        return Err("The name can't hold a line break, a control character or |".to_string());
    }
    Ok(vec![
        "usage".to_string(),
        "--sign-in-folder".to_string(),
        format!("--harness={harness}"),
        format!("--name={name}"),
    ])
}

/// True for `~/.<cli>-<slug>`: slug starts and ends with a letter or digit,
/// holds only `[a-z0-9-]`, and is 1 to 32 characters (a `-2` suffix included).
fn sign_in_folder_shape(cli: &str, folder: &str) -> Option<String> {
    let name = folder.strip_prefix("~/")?;
    if name.contains('/') || name.contains("..") || has_control(name) {
        return None;
    }
    let slug = name.strip_prefix('.')?.strip_prefix(cli)?.strip_prefix('-')?;
    let ok_char = |c: char| c.is_ascii_lowercase() || c.is_ascii_digit();
    let (first, last) = (slug.chars().next()?, slug.chars().last()?);
    if slug.len() > SIGN_IN_NEW_SLUG_MAX || !ok_char(first) || !ok_char(last) {
        return None;
    }
    if !slug.chars().all(|c| ok_char(c) || c == '-') {
        return None;
    }
    Some(name.to_string())
}

/// `usage --sign-in-new --harness=H --folder=F [--again]`, only for a folder
/// Khipu created for this CLI that is still a real folder directly under the
/// home folder (not a symlink, not an escape).
fn sign_in_new_argv(
    harness: &str,
    created: Option<(&str, &str)>,
    user_home: &std::path::Path,
    offered: &[&str],
    again: bool,
) -> Result<Vec<String>, String> {
    sign_in_new_harness(harness, offered)?;
    let (cli, folder) = created.ok_or("Name the account first, so Khipu can make its login folder")?;
    if cli != harness {
        return Err("That login folder belongs to another CLI".to_string());
    }
    let name = sign_in_folder_shape(harness, folder).ok_or("That login folder isn't one Khipu made")?;
    let path = user_home.join(&name);
    let meta = std::fs::symlink_metadata(&path).map_err(|_| "Khipu can't find that login folder".to_string())?;
    if meta.file_type().is_symlink() || !meta.is_dir() {
        return Err("That login folder isn't a plain folder".to_string());
    }
    let parent = std::fs::canonicalize(&path).ok().and_then(|p| p.parent().map(|p| p.to_path_buf()));
    if parent.is_none() || parent != std::fs::canonicalize(user_home).ok() {
        return Err("That login folder isn't directly in your home folder".to_string());
    }
    let mut argv = vec![
        "usage".to_string(),
        "--sign-in-new".to_string(),
        format!("--harness={harness}"),
        format!("--folder={folder}"),
    ];
    if again {
        argv.push("--again".to_string());
    }
    Ok(argv)
}

/// An attempt id is exactly 16 lowercase hex characters.
fn sign_in_attempt_id(id: &str) -> Result<&str, String> {
    if id.len() == 16 && id.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
        Ok(id)
    } else {
        Err("That isn't a sign-in Khipu started".to_string())
    }
}

fn sign_in_status_argv(id: &str) -> Result<Vec<String>, String> {
    Ok(vec!["usage".to_string(), format!("--sign-in-status={}", sign_in_attempt_id(id)?)])
}

/// Cancel stops only the attempt this app started (`stored_id`, from
/// `SignInNewState`). `remove_from` is the folder Khipu made (also from that
/// state, never from the webview) when the owner's Cancel should remove it
/// if still empty; it is passed even with no attempt, since a sign-in that
/// failed, timed out or showed no code has no attempt left to stop.
fn sign_in_cancel_argv(stored_id: Option<&str>, remove_from: Option<(&str, &str)>) -> Result<Vec<String>, String> {
    if stored_id.is_none() && remove_from.is_none() {
        return Err("No sign-in is running".to_string());
    }
    let mut argv = vec!["usage".to_string()];
    match stored_id {
        Some(id) => argv.push(format!("--sign-in-cancel={}", sign_in_attempt_id(id)?)),
        None => argv.push("--sign-in-cancel".to_string()),
    }
    if let Some((cli, folder)) = remove_from {
        if !["claude", "codex", "grok"].contains(&cli) || sign_in_folder_shape(cli, folder).is_none() {
            return Err("That login folder isn't one Khipu made".to_string());
        }
        argv.push("--remove-folder".to_string());
        argv.push(format!("--harness={cli}"));
        argv.push(format!("--folder={folder}"));
    }
    Ok(argv)
}

/// True for the status answers that end an attempt (the CLI deletes its
/// record), so this window stops holding the attempt id.
fn sign_in_status_is_final(answer: &Value) -> bool {
    let state = answer.get("state").and_then(Value::as_str);
    matches!(state, Some("signed_in" | "not_signed_in" | "couldnt_check" | "timeout"))
        || answer.get("error").and_then(Value::as_str) == Some("no_attempt")
}

fn sign_in_user_home() -> Result<PathBuf, String> {
    std::env::var("HOME").map(PathBuf::from).map_err(|_| "Khipu can't find your home folder".to_string())
}

fn sign_in_answer(raw: &str) -> Result<Value, String> {
    serde_json::from_str(raw.trim()).map_err(|_| "Khipu gave an answer the app couldn't read".to_string())
}

/// Which CLIs the dialog may offer Sign in to another account for. The CLI
/// says whether each is installed and proven; a CLI not in
/// `SIGN_IN_NEW_OFFERED` is never offered here whatever the CLI answers.
/// A non-zero CLI exit that still printed JSON reaches the webview as that JSON.
#[tauri::command]
async fn khipu_account_sign_in_offers() -> Result<String, String> {
    let raw = run_khipu_cli_async(fixed_argv(&["usage", "--sign-in-offers"])).await?;
    let mut answer = sign_in_answer(&raw)?;
    if let Some(map) = answer.as_object_mut() {
        for (cli, entry) in map.iter_mut() {
            if !SIGN_IN_NEW_OFFERED.contains(&cli.as_str()) {
                if let Some(e) = entry.as_object_mut() {
                    e.insert("offered".to_string(), Value::Bool(false));
                }
            }
        }
    }
    Ok(answer.to_string())
}

/// Make (or reuse) the login folder for a new account and remember it here.
#[tauri::command]
async fn khipu_account_new_folder(harness: String, name: String) -> Result<String, String> {
    let argv = new_folder_argv(&harness, &name, SIGN_IN_NEW_OFFERED)?;
    let raw = run_khipu_cli_async(argv).await?;
    let answer = sign_in_answer(&raw)?;
    if answer.get("ok").and_then(Value::as_bool) == Some(true) {
        let folder = answer.get("folder").and_then(Value::as_str).unwrap_or("");
        if sign_in_folder_shape(&harness, folder).is_none() {
            return Err("Khipu made a login folder the app won't use".to_string());
        }
        // A running attempt stays held (it is ended only by cancel, status or
        // Start again), so this window can still stop its own login.
        sign_in_state().folder = Some((harness, folder.to_string()));
    }
    Ok(raw)
}

/// Start signing in to the folder Khipu made. Returns at once with the attempt.
#[tauri::command]
async fn khipu_account_sign_in_new(harness: String, again: Option<bool>) -> Result<String, String> {
    let created = sign_in_state().folder.clone();
    let argv = sign_in_new_argv(
        &harness,
        created.as_ref().map(|(c, f)| (c.as_str(), f.as_str())),
        &sign_in_user_home()?,
        SIGN_IN_NEW_OFFERED,
        again.unwrap_or(false),
    )?;
    let raw = run_khipu_cli_async(argv).await?;
    let answer = sign_in_answer(&raw)?;
    if answer.get("ok").and_then(Value::as_bool) == Some(true) {
        let id = answer.pointer("/attempt/id").and_then(Value::as_str).unwrap_or("");
        sign_in_state().attempt = Some(sign_in_attempt_id(id)?.to_string());
    }
    Ok(raw)
}

#[tauri::command]
async fn khipu_account_sign_in_status() -> Result<String, String> {
    let id = sign_in_state().attempt.clone().ok_or("No sign-in is running")?;
    let raw = run_khipu_cli_async(sign_in_status_argv(&id)?).await?;
    if sign_in_answer(&raw).map(|a| sign_in_status_is_final(&a)).unwrap_or(false) {
        let mut state = sign_in_state();
        if state.attempt.as_deref() == Some(id.as_str()) {
            state.attempt = None;
        }
    }
    Ok(raw)
}

/// Stop this window's sign-in; `remove_folder` also removes the empty folder
/// Khipu made for it (the CLI refuses to remove one that has signed in).
#[tauri::command]
async fn khipu_account_sign_in_cancel(remove_folder: Option<bool>) -> Result<String, String> {
    let (id, folder) = {
        let state = sign_in_state();
        (state.attempt.clone(), state.folder.clone())
    };
    let remove_from = if remove_folder.unwrap_or(false) { folder.as_ref() } else { None };
    let argv = sign_in_cancel_argv(id.as_deref(), remove_from.map(|(c, f)| (c.as_str(), f.as_str())))?;
    let raw = run_khipu_cli_async(argv).await?;
    if let Ok(answer) = sign_in_answer(&raw) {
        let ok = answer.get("ok").and_then(Value::as_bool) == Some(true);
        let mut state = sign_in_state();
        if (ok || answer.get("error").and_then(Value::as_str) == Some("no_attempt")) && state.attempt == id {
            state.attempt = None;
        }
        if ok && answer.get("folder_removed").and_then(Value::as_bool) == Some(true) && state.folder == folder {
            state.folder = None;
        }
    }
    Ok(raw)
}

/// On quit: end a sign-in this app left running. Best effort, a few seconds.
fn reap_sign_in_on_exit() {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(run_khipu_cli(&fixed_argv(&["usage", "--sign-in-reap"])));
    });
    if let Ok(Err(e)) = rx.recv_timeout(SIGN_IN_REAP_WAIT) {
        eprintln!("[khipu-signin] reap on quit failed: {e}");
    }
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

/// Rebuild this Mac's offline copy of memory now (the fix doctor names for
/// prompt-time recall). `snapshot` stays out of `ALLOWED_SUBCOMMANDS`; this
/// command fixes the argv to exactly `snapshot refresh`.
const SNAPSHOT_REFRESH_ARGV: &[&str] = &["snapshot", "refresh"];

#[tauri::command]
async fn khipu_snapshot_refresh() -> Result<String, String> {
    run_khipu_cli_async(fixed_argv(SNAPSHOT_REFRESH_ARGV)).await
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
    "episode", "owed", "storage",
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
            khipu_snapshot_refresh,
            khipu_config_request,
            khipu_config_unset,
            khipu_budget_status,
            khipu_budget_lift_synth_today,
            khipu_usage_status,
            khipu_usage_refresh,
            khipu_accounts_discover,
            khipu_accounts_check,
            khipu_accounts_try,
            khipu_accounts_forget_login_paths,
            khipu_route_view,
            khipu_route_pause,
            khipu_route_lift,
            khipu_route_check,
            khipu_owner_direction_show,
            khipu_owner_direction_actions,
            khipu_owner_direction_undo,
            khipu_owner_direction_acknowledge,
            khipu_owner_direction_switch,
            khipu_owner_direction_set,
            khipu_route_clear,
            khipu_route_note_set,
            khipu_route_note_end,
            khipu_route_rule,
            khipu_route_hold_add,
            khipu_route_hold_end,
            khipu_route_priority,
            khipu_work_view,
            khipu_ledger_list,
            khipu_ledger_get,
            khipu_ledger_set,
            khipu_ledger_remove,
            khipu_ledger_removed,
            khipu_ledger_restore,
            khipu_ledger_inbox,
            khipu_ledger_import,
            khipu_work_hide,
            khipu_work_same,
            khipu_owed_change,
            khipu_owed_tidy_plan,
            khipu_owed_tidy_apply,
            khipu_owed_tidy_undo,
            khipu_ledger_projects,
            khipu_ledger_project_set,
            khipu_work_hidden,
            khipu_work_check,
            khipu_account_sign_in,
            khipu_account_sign_in_offers,
            khipu_account_new_folder,
            khipu_account_sign_in_new,
            khipu_account_sign_in_status,
            khipu_account_sign_in_cancel,
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
        .build(tauri::generate_context!())
        .expect("error while building Khipu")
        .run(|_app, event| {
            if let tauri::RunEvent::Exit = event {
                reap_sign_in_on_exit();
            }
        });
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
                  "models", "episode", "owed", "storage"] {
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
        assert_eq!(ALLOWED_SUBCOMMANDS.len(), 15);
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
        assert_eq!(ALLOWED_SUBCOMMANDS.len(), 15);
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
    fn owner_direction_commands_are_fixed_verbs_marked_from_the_app() {
        assert_eq!(fixed_argv(OWNER_DIRECTION_SHOW_ARGV), v(&["owner-direction", "show"]));
        assert_eq!(fixed_argv(OWNER_DIRECTION_ACTIONS_ARGV), v(&["owner-direction", "actions", "--json"]));
        assert_eq!(
            fixed_argv(OWNER_DIRECTION_ACKNOWLEDGE_ARGV),
            v(&["owner-direction", "acknowledge", "--from-app"])
        );
        assert_eq!(
            owner_direction_undo_argv("a1b2c3d4").unwrap(),
            v(&["owner-direction", "undo", "--from-app", "--", "a1b2c3d4"])
        );
        assert_eq!(owner_direction_switch_argv(true), v(&["owner-direction", "on", "--from-app"]));
        assert_eq!(owner_direction_switch_argv(false), v(&["owner-direction", "off", "--from-app"]));
        assert_eq!(
            owner_direction_set_argv("latest_window_h", "2.5").unwrap(),
            v(&["owner-direction", "set", "--from-app", "--", "latest_window_h", "2.5"])
        );
        assert_eq!(
            owner_direction_set_argv("session_binding", "off").unwrap(),
            v(&["owner-direction", "set", "--from-app", "--", "session_binding", "off"])
        );
        assert_eq!(
            owner_direction_set_argv("trust", "claude_code:sdk-cli").unwrap(),
            v(&["owner-direction", "set", "--from-app", "--", "trust", "claude_code:sdk-cli"])
        );
        // Nothing the webview sends becomes a flag or a key the CLI was not built for.
        for (key, value) in [
            ("enabled", "on"),
            ("latest_window_h", "-1"),
            ("latest_window_h", "0"),
            ("latest_window_h", "six"),
            ("latest_window_h", "1e3"),
            ("earlier_window_min", "30"),
            ("session_binding", "yes"),
            ("trust", "--from-app"),
            ("trust", "claude_code"),
            ("untrust", "codex:x\n--y"),
        ] {
            assert!(owner_direction_set_argv(key, value).is_err(), "{key}={value}");
        }
        for id in ["", "-a1b2c3d", "a1b2c3d4e5", "a1b2 c3d", "A1B2C3D4"] {
            assert!(owner_direction_undo_argv(id).is_err(), "{id:?}");
        }
        assert!(!ALLOWED_SUBCOMMANDS.contains(&"owner-direction"));
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
    fn replica_settings_are_whole_seconds() {
        assert_eq!(config_set_argv("replica_refresh_after", "1200").unwrap(), v(&["config", "--set", "replica_refresh_after", "1200"]));
        assert_eq!(config_set_argv("replica_refresh_after", " 0 ").unwrap(), v(&["config", "--set", "replica_refresh_after", "0"]));
        assert_eq!(config_set_argv("replica_check_every", "60").unwrap(), v(&["config", "--set", "replica_check_every", "60"]));
        assert_eq!(config_unset_argv("replica_check_every").unwrap(), v(&["config", "--unset=replica_check_every"]));
        assert_eq!(config_set_argv("hub_down_memo_for", "90").unwrap(), v(&["config", "--set", "hub_down_memo_for", "90"]));
        assert_eq!(config_set_argv("hub_down_memo_for", "0").unwrap(), v(&["config", "--set", "hub_down_memo_for", "0"]));
        assert_eq!(config_unset_argv("hub_down_memo_for").unwrap(), v(&["config", "--unset=hub_down_memo_for"]));
        for bad in ["-1", "1.5", "abc", ""] {
            assert!(config_set_argv("hub_down_memo_for", bad).is_err(), "{bad}");
        }
        for bad in ["-1", "abc", "1e3", ""] {
            assert!(config_set_argv("replica_refresh_after", bad).is_err(), "{bad}");
        }
        assert!(config_set_argv("replica_check_every", "30").is_err());
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
    fn the_storage_settings_are_checked_and_resettable() {
        for (key, high, whole) in STORAGE_KEYS {
            assert_eq!(config_set_argv(key, " 0 ").unwrap(), v(&["config", "--set", key, "0"]));
            assert_eq!(config_set_argv(key, &format!("{high}")).unwrap()[3], format!("{high}"));
            assert!(config_set_argv(key, &format!("{}", high + 1.0)).is_err());
            for bad in ["", "-1", "abc", "--unset", "1e3", "1.", ".5", "1 2"] {
                assert!(config_set_argv(key, bad).is_err(), "{key} accepted {bad:?}");
            }
            assert_eq!(config_set_argv(key, "2.5").is_ok(), !*whole, "{key} decimal");
            assert_eq!(config_unset_argv(key).unwrap(), v(&["config", &format!("--unset={key}")]));
        }
        assert_eq!(
            config_set_argv("disk_warn_free_percent", "5.5").unwrap(),
            v(&["config", "--set", "disk_warn_free_percent", "5.5"])
        );
        assert!(config_set_argv("disk_warn_free_percent", "100.5").is_err());
        assert!(config_set_argv("synth_usage_keep_days", "1.5").is_err());
        assert!(config_set_argv("disk_unknown", "1").is_err());
        assert!(config_unset_argv("disk_unknown").is_err());
        assert!(ALLOWED_SUBCOMMANDS.contains(&"storage"));
    }

    #[test]
    fn the_doctor_rate_window_is_whole_minutes_in_range() {
        let key = "doctor_rate_window_minutes";
        assert_eq!(config_set_argv(key, " 90 ").unwrap(), v(&["config", "--set", key, "90"]));
        assert_eq!(config_set_argv(key, "5").unwrap(), v(&["config", "--set", key, "5"]));
        for bad in ["", "0", "4", "-5", "1.5", "abc", "--unset", "10081"] {
            assert!(config_set_argv(key, bad).is_err(), "accepted {bad:?}");
        }
        assert_eq!(config_unset_argv(key).unwrap(), v(&["config", "--unset=doctor_rate_window_minutes"]));
        assert_eq!(SNAPSHOT_REFRESH_ARGV, &["snapshot", "refresh"]);
        assert!(!ALLOWED_SUBCOMMANDS.contains(&"snapshot"));
    }

    #[test]
    fn the_promotion_gate_settings_are_checked() {
        let wait = "promote_hub_wait_minutes";
        assert_eq!(config_set_argv(wait, "0").unwrap(), v(&["config", "--set", wait, "0"]));
        assert_eq!(config_set_argv(wait, "240").unwrap(), v(&["config", "--set", wait, "240"]));
        assert!(config_set_argv(wait, "241").is_err());
        let key = "promote_gate_ignore";
        assert_eq!(
            config_set_argv(key, " outbox , degraded_rate ").unwrap(),
            v(&["config", "--from-app", "--set", key, "outbox,degraded_rate"])
        );
        for bad in ["", " , ", "Outbox", "out box", "--unset", "a-b", &"x".repeat(41)] {
            assert!(config_set_argv(key, bad).is_err(), "accepted {bad:?}");
        }
        let many = (0..33).map(|_| "a").collect::<Vec<_>>().join(",");
        assert!(config_set_argv(key, &many).is_err());
        assert_eq!(config_unset_argv(key).unwrap(), v(&["config", "--from-app", "--unset=promote_gate_ignore"]));
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
        for bad in ["", "--x", "a b", "a=b", "v\n1", &"a".repeat(65)] {
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
        assert_eq!(JOB_NAMES.len(), 7);
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
    fn a_slow_cli_is_killed_at_the_timeout_and_says_so() {
        let mut cmd = Command::new("/bin/sleep");
        cmd.arg("5");
        let started = std::time::Instant::now();
        match output_with_timeout(cmd, Some(std::time::Duration::from_millis(300))) {
            Err(CliWaitError::TimedOut(secs)) => {
                assert!(cli_timeout_message(secs).starts_with("Khipu did not answer within"));
            }
            _ => panic!("expected a timeout"),
        }
        assert!(started.elapsed() < std::time::Duration::from_secs(3), "child was not killed");
    }

    #[test]
    fn a_fast_cli_returns_its_output_and_no_limit_waits() {
        let mut cmd = Command::new("/bin/echo");
        cmd.arg("hello");
        let out = output_with_timeout(cmd, Some(std::time::Duration::from_secs(5))).ok().unwrap();
        assert!(out.status.success());
        assert_eq!(String::from_utf8_lossy(&out.stdout).trim(), "hello");
        let mut slow = Command::new("/bin/sh");
        slow.args(["-c", "sleep 0.3; echo done"]);
        let out = output_with_timeout(slow, None).ok().unwrap();
        assert_eq!(String::from_utf8_lossy(&out.stdout).trim(), "done");
    }

    #[test]
    fn the_cli_timeout_reads_the_config_and_falls_back_to_sixty() {
        assert_eq!(app_cli_timeout_secs_from(None), 60);
        assert_eq!(app_cli_timeout_secs_from(Some("not json")), 60);
        assert_eq!(app_cli_timeout_secs_from(Some("{}")), 60);
        assert_eq!(app_cli_timeout_secs_from(Some(r#"{"app_cli_timeout": "x"}"#)), 60);
        assert_eq!(app_cli_timeout_secs_from(Some(r#"{"app_cli_timeout": -3}"#)), 60);
        assert_eq!(app_cli_timeout_secs_from(Some(r#"{"app_cli_timeout": 12}"#)), 12);
        assert_eq!(app_cli_timeout_secs_from(Some(r#"{"app_cli_timeout": 0}"#)), 0);
    }

    #[test]
    fn long_user_actions_have_no_time_limit() {
        for argv in [
            v(&["backup-local"]), v(&["import-local", "x"]), v(&["doctor", "--probe"]), v(&["nightly"]), v(&["embed-backfill"]),
            v(&["db", "move"]), v(&["db", "preflight"]), v(&["db", "connect", "--use-stored"]),
        ] {
            assert!(cli_timeout_for(&argv).is_none(), "{argv:?} should have no limit");
        }
        assert!(!cli_has_no_time_limit(&v(&["owed", "--envelope"])));
        assert!(!cli_has_no_time_limit(&v(&["db", "status"])));
    }

    #[test]
    fn the_read_role_settings_are_ordinary_settings() {
        for key in READ_ROLE_KEYS {
            assert_eq!(config_set_argv(key, "20").unwrap(), v(&["config", "--set", key, "20"]));
            assert_eq!(config_set_argv(key, "0").unwrap(), v(&["config", "--set", key, "0"]));
            assert_eq!(config_unset_argv(key).unwrap(), v(&["config", &format!("--unset={key}")]));
            assert!(config_set_argv(key, "-1").is_err());
            assert!(config_set_argv(key, "1.5").is_err());
            assert!(config_set_argv_expect(key, "5", Some("v1")).is_err());
        }
        let src = std::fs::read_to_string(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../packages/cli/khipu/read_role.py"),
        ).unwrap();
        let settings = src.split("SETTINGS: dict").nth(1).unwrap().split("\n}").next().unwrap();
        for k in READ_ROLE_KEYS {
            assert!(settings.contains(&format!("\"{k}\":")), "{k} missing from read_role.SETTINGS");
        }
    }

    #[test]
    fn the_offline_durations_are_ordinary_settings() {
        for key in OFFLINE_DURATION_KEYS {
            assert_eq!(config_set_argv(key, "45").unwrap(), v(&["config", "--set", key, "45"]));
            assert_eq!(config_set_argv(key, "0").unwrap(), v(&["config", "--set", key, "0"]));
            assert_eq!(config_unset_argv(key).unwrap(), v(&["config", &format!("--unset={key}")]));
            assert!(config_set_argv(key, "-1").is_err());
            assert!(config_set_argv(key, "abc").is_err());
            assert!(config_set_argv(key, "1e3").is_err());
            assert!(config_set_argv(key, &(10 * 366 * 86_400 + 1).to_string()).is_err());
            assert!(config_set_argv_expect(key, "5", Some("v1")).is_err());
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
        let offline_src = std::fs::read_to_string(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../packages/cli/khipu/offline.py"),
        ).unwrap();
        let offline = offline_src.split("SETTINGS: dict").nth(1).unwrap().split("\n}").next().unwrap();
        for k in OFFLINE_DURATION_KEYS {
            assert!(offline.contains(&format!("\"{k}\":")), "{k} missing from offline.SETTINGS");
        }
        assert_eq!(offline.matches("\"duration\"").count(), OFFLINE_DURATION_KEYS.len());
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
        assert_eq!(ALLOWED_SUBCOMMANDS.len(), 15);
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
        for name in ["khipu-prompt-recall", "khipu-recall-client.py", "khipu-owner-turn.py"] { assert!(script.contains(name), "{name}"); }
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
        for bad in ["", "--x", "p 1", "p=1", "p\n1", "p|1", ".p1", "p1.", "p1..a2", "p-1"] {
            assert!(route_priority_argv_expect("clear", &[], None, Some(bad)).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn versions_in_the_python_format_are_accepted() {
        // The format packages/cli/khipu/config.py defines (`is_version`); a
        // pytest also feeds this check versions the Python code makes.
        let longest = format!("p{}.a{}", "1".repeat(24), "f".repeat(16));
        for good in ["p0", "a4def5fc278cfc609", "p0.a4def5fc278cfc609", "pe1a2b3c4.a4def5fc278cfc609", longest.as_str()] {
            assert_eq!(checked_expect(good), Ok(good), "{good:?}");
        }
        assert!(checked_expect(&"a".repeat(65)).is_err());
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
        let sent = r#"[{"harness": "codex", "label": "Codex"}, {"harness": "legacy-cli", "label": "Legacy", "note": 1},
                       {"harness": "Claude", "label": "Work", "home": "~/.claude-work"}]"#;
        let out: Value = serde_json::from_str(&checked_usage_accounts_kept(sent, &stored).unwrap()).unwrap();
        assert_eq!(out[1], stored[1]);
        assert_eq!(out[2], serde_json::json!({"harness": "claude", "label": "Work", "home": "~/.claude-work"}));
        let argv = config_set_argv_kept(USAGE_ACCOUNTS_KEY, sent, Some("a1"), &stored).unwrap();
        assert_eq!(argv[..5], v(&["config", "--from-app", "--expect=a1", "--set", USAGE_ACCOUNTS_KEY])[..]);
        // Changed (even only by spaces), or with nothing stored, it is checked as any new entry.
        let changed = r#"[{"harness": "legacy-cli", "label": "Renamed"}]"#;
        assert!(checked_usage_accounts_kept(changed, &stored).is_err());
        let spaced = r#"[{"harness": "legacy-cli", "label": " Legacy ", "note": 1}]"#;
        assert!(checked_usage_accounts_kept(spaced, &stored).is_err());
        assert!(checked_usage_accounts(r#"[{"harness": "legacy-cli", "label": "Legacy"}]"#).is_err());
    }

    #[test]
    fn each_untouched_entry_keeps_its_own_stored_value() {
        // Two entries the same in their account fields but not in their
        // others: each goes through as itself, never as the first.
        let stored: Vec<Value> = serde_json::from_str(
            r#"[{"harness": "legacy-cli", "label": "Legacy", "note": "first"},
                {"harness": "legacy-cli", "label": "Legacy", "note": "second"}]"#,
        )
        .unwrap();
        let sent = serde_json::to_string(&stored).unwrap();
        let out: Value = serde_json::from_str(&checked_usage_accounts_kept(&sent, &stored).unwrap()).unwrap();
        assert_eq!(out, Value::Array(stored.clone()));
        // Reordered, each is still itself.
        let reordered = serde_json::to_string(&[stored[1].clone(), stored[0].clone()]).unwrap();
        let out: Value = serde_json::from_str(&checked_usage_accounts_kept(&reordered, &stored).unwrap()).unwrap();
        assert_eq!(out, serde_json::json!([stored[1], stored[0]]));
        // One stored entry answers one sent entry: a copy of it is new, and checked.
        let twice = serde_json::to_string(&[stored[0].clone(), stored[0].clone()]).unwrap();
        assert!(checked_usage_accounts_kept(&twice, &stored).is_err());
    }

    #[test]
    fn an_untouched_entry_that_is_not_an_object_goes_through_as_stored() {
        let stored: Vec<Value> = serde_json::from_str(r#"[{"harness": "codex", "label": "Codex"}, null, 7]"#).unwrap();
        let sent = r#"[{"harness": "codex", "label": "Codex"}, null, 7, {"harness": "claude", "label": "Work"}]"#;
        let out: Value = serde_json::from_str(&checked_usage_accounts_kept(sent, &stored).unwrap()).unwrap();
        assert_eq!(out[1], Value::Null);
        assert_eq!(out[2], serde_json::json!(7));
        // One not stored is still refused.
        assert!(checked_usage_accounts_kept(r#"[null, "x"]"#, &stored).is_err());
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

#[cfg(test)]
mod sign_in_new_tests {
    use super::*;

    const ALL: &[&str] = &["claude", "codex", "grok"];

    fn v(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    /// A unique scratch home under the system temp dir, removed on drop.
    struct Home(PathBuf);
    impl Home {
        fn new(tag: &str) -> Home {
            let nanos = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
            let p = std::env::temp_dir().join(format!("khipu-signin-{tag}-{}-{nanos}", std::process::id()));
            std::fs::create_dir_all(&p).unwrap();
            Home(p)
        }
        fn mkdir(&self, name: &str) {
            std::fs::create_dir(self.0.join(name)).unwrap();
        }
    }
    impl Drop for Home {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn built(h: &Home, harness: &str, folder: &str, offered: &[&str]) -> Result<Vec<String>, String> {
        sign_in_new_argv(harness, Some((harness, folder)), &h.0, offered, false)
    }

    #[test]
    fn production_offers_claude_only() {
        assert_eq!(SIGN_IN_NEW_OFFERED, &["claude"]);
    }

    #[test]
    fn claude_slug_folder_is_accepted_and_again_is_appended() {
        let h = Home::new("ok");
        h.mkdir(".claude-client");
        h.mkdir(".claude-client-2");
        assert_eq!(
            built(&h, "claude", "~/.claude-client", SIGN_IN_NEW_OFFERED).unwrap(),
            v(&["usage", "--sign-in-new", "--harness=claude", "--folder=~/.claude-client"])
        );
        assert_eq!(
            sign_in_new_argv("claude", Some(("claude", "~/.claude-client-2")), &h.0, SIGN_IN_NEW_OFFERED, true).unwrap(),
            v(&["usage", "--sign-in-new", "--harness=claude", "--folder=~/.claude-client-2", "--again"])
        );
    }

    #[test]
    fn codex_and_grok_are_refused_in_production_and_pass_only_when_offered() {
        let h = Home::new("gate");
        h.mkdir(".codex-lab");
        h.mkdir(".grok-second");
        assert!(built(&h, "codex", "~/.codex-lab", SIGN_IN_NEW_OFFERED).is_err());
        assert!(built(&h, "grok", "~/.grok-second", SIGN_IN_NEW_OFFERED).is_err());
        assert!(built(&h, "codex", "~/.codex-lab", &["claude", "codex"]).is_ok());
        assert!(built(&h, "grok", "~/.grok-second", &["claude", "codex"]).is_err());
        assert!(built(&h, "grok", "~/.grok-second", ALL).is_ok());
    }

    #[test]
    fn muse_and_unknown_harnesses_are_refused_even_if_listed() {
        let h = Home::new("muse");
        h.mkdir(".muse-x");
        assert!(built(&h, "muse", "~/.muse-x", &["claude", "muse"]).is_err());
        assert!(built(&h, "--sign-in", "~/.claude-a", &["--sign-in"]).is_err());
    }

    #[test]
    fn odd_folder_names_are_refused() {
        let h = Home::new("odd");
        h.mkdir(".claude-a");
        let bad = [
            "~/..claude-a", "~/.claude-..", "~/.claude-a/..", "~/x/.claude-a", "/abs/.claude-a",
            "~/.claude-a/b", "~/.claude-a\n", "~/.claude-a\u{7}", "~/.claude-", "~/.claude--", "~/.claude--a",
            "~/.claude-a-", "~/.claude-A", "~/.claude-a b", "~/.claude", "~/claude-a", ".claude-a", "",
            "~/.claude-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ];
        for f in bad {
            assert!(built(&h, "claude", f, SIGN_IN_NEW_OFFERED).is_err(), "{f:?}");
        }
    }

    #[test]
    fn longest_slug_is_thirty_two_characters_suffix_included() {
        let h = Home::new("len");
        for (slug, ok) in [
            ("a".repeat(32), true),
            (format!("{}-2", "a".repeat(30)), true),
            ("a".repeat(33), false),
            (format!("{}-2", "a".repeat(31)), false),
            (format!("{}-20", "a".repeat(30)), false),
            ("a".repeat(40), false),
        ] {
            h.mkdir(&format!(".claude-{slug}"));
            assert_eq!(built(&h, "claude", &format!("~/.claude-{slug}"), SIGN_IN_NEW_OFFERED).is_ok(), ok, "{slug}");
        }
    }

    #[test]
    fn slug_limit_matches_the_cli() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../packages/cli/khipu/signin.py");
        let src = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
        assert!(src.contains(&format!("\nSLUG_MAX = {SIGN_IN_NEW_SLUG_MAX}\n")), "SLUG_MAX differs from the CLI");
    }

    #[test]
    fn a_folder_of_another_cli_or_none_is_refused() {
        let h = Home::new("cli");
        h.mkdir(".claude-a");
        h.mkdir(".codex-a");
        assert!(sign_in_new_argv("codex", Some(("claude", "~/.claude-a")), &h.0, ALL, false).is_err());
        assert!(sign_in_new_argv("codex", Some(("codex", "~/.claude-a")), &h.0, ALL, false).is_err());
        assert!(built(&h, "codex", "~/.claude-a", ALL).is_err());
        assert!(sign_in_new_argv("claude", None, &h.0, SIGN_IN_NEW_OFFERED, false).is_err());
    }

    #[test]
    fn a_folder_that_does_not_exist_or_is_a_file_is_refused() {
        let h = Home::new("missing");
        assert!(built(&h, "claude", "~/.claude-gone", SIGN_IN_NEW_OFFERED).is_err());
        std::fs::write(h.0.join(".claude-file"), b"x").unwrap();
        assert!(built(&h, "claude", "~/.claude-file", SIGN_IN_NEW_OFFERED).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn a_symlink_that_points_outside_the_home_is_refused() {
        let h = Home::new("link");
        let outside = Home::new("outside");
        std::os::unix::fs::symlink(&outside.0, h.0.join(".claude-a")).unwrap();
        assert!(built(&h, "claude", "~/.claude-a", SIGN_IN_NEW_OFFERED).is_err());
        // Even a link to another folder inside the home is not a folder Khipu made.
        h.mkdir("real");
        std::os::unix::fs::symlink(h.0.join("real"), h.0.join(".claude-b")).unwrap();
        assert!(built(&h, "claude", "~/.claude-b", SIGN_IN_NEW_OFFERED).is_err());
    }

    #[test]
    fn attempt_ids_are_sixteen_lowercase_hex() {
        let id = "0123456789abcdef";
        assert_eq!(sign_in_status_argv(id).unwrap(), v(&["usage", "--sign-in-status=0123456789abcdef"]));
        for bad in ["", "0123456789abcde", "0123456789abcdef0", "0123456789ABCDEF", "0123456789abcdeg",
                    "0123456789abcde;", "--remove-folder--x-"] {
            assert!(sign_in_status_argv(bad).is_err(), "{bad:?}");
            assert!(sign_in_cancel_argv(Some(bad), None).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn cancel_stops_only_its_own_attempt() {
        let mine = "0123456789abcdef";
        let made = Some(("claude", "~/.claude-client"));
        assert_eq!(sign_in_cancel_argv(Some(mine), None).unwrap(), v(&["usage", "--sign-in-cancel=0123456789abcdef"]));
        assert_eq!(
            sign_in_cancel_argv(Some(mine), made).unwrap(),
            v(&["usage", "--sign-in-cancel=0123456789abcdef", "--remove-folder", "--harness=claude",
                "--folder=~/.claude-client"])
        );
        assert!(sign_in_cancel_argv(None, None).is_err());
    }

    #[test]
    fn cancel_with_no_attempt_only_removes_the_folder_khipu_made() {
        assert_eq!(
            sign_in_cancel_argv(None, Some(("claude", "~/.claude-client"))).unwrap(),
            v(&["usage", "--sign-in-cancel", "--remove-folder", "--harness=claude", "--folder=~/.claude-client"])
        );
        for (cli, folder) in [("claude", "~/.claude-a/.."), ("claude", "~/.codex-a"), ("muse", "~/.muse-a"),
                              ("claude", "/abs/.claude-a"), ("claude", "~/.claude-a\n")] {
            assert!(sign_in_cancel_argv(None, Some((cli, folder))).is_err(), "{folder:?}");
        }
    }

    #[test]
    fn final_status_answers_release_the_attempt() {
        for state in ["signed_in", "not_signed_in", "couldnt_check", "timeout"] {
            assert!(sign_in_status_is_final(&serde_json::json!({"ok": true, "state": state})), "{state}");
        }
        assert!(sign_in_status_is_final(&serde_json::json!({"ok": false, "error": "no_attempt"})));
        assert!(!sign_in_status_is_final(&serde_json::json!({"ok": true, "state": "waiting"})));
        assert!(!sign_in_status_is_final(&serde_json::json!({"ok": true, "state": "starting"})));
    }

    /// A fake `python` that answers the sign-in CLI calls with fixed JSON, so the
    /// Tauri commands run end to end with no real login.
    fn fake_cli() -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let nanos = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let dir = std::env::temp_dir().join(format!("khipu-signin-cli-{}-{nanos}", std::process::id()));
        std::fs::create_dir_all(dir.join("home/.claude-a")).unwrap();
        std::fs::create_dir_all(dir.join("home/.claude-b")).unwrap();
        let script = dir.join("python");
        std::fs::write(
            &script,
            r#"#!/bin/sh
case "$*" in
  *--sign-in-folder*) echo '{"ok": true, "folder": "~/.claude-b", "reused": false}' ;;
  *--sign-in-new*) echo '{"ok": true, "attempt": {"id": "0123456789abcdef"}, "state": "waiting"}' ;;
  *--sign-in-cancel*) echo '{"ok": true, "state": "cancelled"}' ;;
  *--sign-in-status*) echo '{"ok": true, "state": "waiting"}' ;;
  *) echo '{}' ;;
esac
"#,
        )
        .unwrap();
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
        dir
    }

    #[cfg(unix)]
    #[test]
    fn making_a_folder_does_not_forget_a_running_attempt() {
        let dir = fake_cli();
        let saved = (std::env::var_os("KHIPU_PYTHON"), std::env::var_os("HOME"));
        std::env::set_var("KHIPU_PYTHON", dir.join("python"));
        std::env::set_var("HOME", dir.join("home"));
        {
            let mut s = sign_in_state();
            s.folder = Some(("claude".to_string(), "~/.claude-a".to_string()));
            s.attempt = None;
        }
        let run = || {
            // A sign-in starts and is running.
            tauri::async_runtime::block_on(khipu_account_sign_in_new("claude".to_string(), None)).unwrap();
            let held = sign_in_state().attempt.clone();
            // The dialog is opened again and a new name is entered: the CLI makes
            // a folder while the attempt still runs in the CLI's record.
            tauri::async_runtime::block_on(khipu_account_new_folder("claude".to_string(), "b".to_string())).unwrap();
            let kept = sign_in_state().attempt.clone();
            // The running attempt must still be stoppable from the app.
            let cancelled = tauri::async_runtime::block_on(khipu_account_sign_in_cancel(Some(false)));
            (held, kept, cancelled)
        };
        let (held, kept, cancelled) = run();
        for (key, value) in [("KHIPU_PYTHON", saved.0), ("HOME", saved.1)] {
            match value {
                Some(v) => std::env::set_var(key, v),
                None => std::env::remove_var(key),
            }
        }
        {
            let mut s = sign_in_state();
            s.folder = None;
            s.attempt = None;
        }
        let _ = std::fs::remove_dir_all(&dir);
        assert_eq!(held.as_deref(), Some("0123456789abcdef"));
        assert_eq!(kept.as_deref(), Some("0123456789abcdef"));
        assert!(cancelled.is_ok(), "cancel after making a folder: {cancelled:?}");
    }

    #[test]
    fn new_folder_name_rules() {
        assert_eq!(
            new_folder_argv("claude", "Claude client", SIGN_IN_NEW_OFFERED).unwrap(),
            v(&["usage", "--sign-in-folder", "--harness=claude", "--name=Claude client"])
        );
        let long = "n".repeat(64);
        assert!(new_folder_argv("claude", &long, SIGN_IN_NEW_OFFERED).is_ok());
        for bad in ["", "   ", "a|b", "a\nb", "a\u{0}b", "a\tb"] {
            assert!(new_folder_argv("claude", bad, SIGN_IN_NEW_OFFERED).is_err(), "{bad:?}");
        }
        assert!(new_folder_argv("claude", &"n".repeat(65), SIGN_IN_NEW_OFFERED).is_err());
        assert!(new_folder_argv("codex", "x", SIGN_IN_NEW_OFFERED).is_err());
        assert!(new_folder_argv("codex", "x", ALL).is_ok());
        assert!(new_folder_argv("muse", "x", &["muse"]).is_err());
    }
}

#[cfg(test)]
mod accounts_command_tests {
    use super::*;

    fn v(a: &[&str]) -> Vec<String> {
        a.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn discover_and_forget_are_fixed_argv() {
        assert_eq!(accounts_discover_argv(false), v(&["usage", "--discover", "--json"]));
        assert_eq!(accounts_discover_argv(true), v(&["usage", "--discover", "--now", "--json"]));
        assert_eq!(fixed_argv(ACCOUNTS_FORGET_LOGIN_PATHS_ARGV), v(&["usage", "--forget-login-paths"]));
        assert!(!ALLOWED_SUBCOMMANDS.contains(&"usage"));
    }

    #[test]
    fn check_joins_the_folder_to_its_flag() {
        assert_eq!(
            accounts_check_argv("~/.codex-work").unwrap(),
            v(&["usage", "--discover", "--check=~/.codex-work", "--json"])
        );
        assert_eq!(
            accounts_check_argv("  /Volumes/Shared/.claude-team  ").unwrap(),
            v(&["usage", "--discover", "--check=/Volumes/Shared/.claude-team", "--json"])
        );
    }

    #[test]
    fn check_refuses_flags_control_characters_and_relative_folders() {
        for bad in ["", "-x", "--now", "--check=/x", ".codex-work", "codex-work", "~.codex-work", "~",
            "~/.codex\n--now", "/tmp/a\rb", "/tmp/\u{7f}x"] {
            assert!(accounts_check_argv(bad).is_err(), "accepted {bad:?}");
        }
        assert!(accounts_check_argv(&format!("/{}", "a".repeat(1100))).is_err());
    }

    #[test]
    fn try_builds_exact_argv_per_harness() {
        assert_eq!(accounts_try_argv("claude", None).unwrap(), v(&["usage", "--try", "--harness=claude"]));
        assert_eq!(accounts_try_argv("Codex", Some("")).unwrap(), v(&["usage", "--try", "--harness=codex"]));
        assert_eq!(
            accounts_try_argv("claude", Some("~/.claude-client")).unwrap(),
            v(&["usage", "--try", "--harness=claude", "--folder=~/.claude-client"])
        );
        assert_eq!(
            accounts_try_argv("grok", Some("/home/someone/.grok-second")).unwrap(),
            v(&["usage", "--try", "--harness=grok", "--folder=/home/someone/.grok-second"])
        );
        assert_eq!(accounts_try_argv("muse", None).unwrap(), v(&["usage", "--try", "--harness=muse"]));
    }

    #[test]
    fn try_refuses_unknown_harnesses_muse_folders_and_bad_folders() {
        for bad in ["", "cursor", "--harness", "clau\nde", "claude --now", "-claude"] {
            assert!(accounts_try_argv(bad, None).is_err(), "accepted harness {bad:?}");
        }
        assert!(accounts_try_argv("muse", Some("~/.muse")).is_err());
        for bad in ["-x", "--folder=/x", "relative/folder", "~/.claude\n--now", ".claude-x"] {
            assert!(accounts_try_argv("claude", Some(bad)).is_err(), "accepted folder {bad:?}");
        }
    }

    #[test]
    fn cli_settings_save_and_clear_through_config() {
        for key in ["cli_path.claude", "cli_path.codex", "cli_path.grok", "cli_path.muse"] {
            assert_eq!(
                config_set_argv(key, "/opt/tools/bin/cli").unwrap(),
                v(&["config", "--set", key, "/opt/tools/bin/cli"])
            );
            assert_eq!(config_unset_argv(key).unwrap(), v(&["config", &format!("--unset={key}")]));
        }
        assert_eq!(
            config_set_argv("node_folder", "~/.nvm/versions/node/v20.11.0/bin").unwrap(),
            v(&["config", "--set", "node_folder", "~/.nvm/versions/node/v20.11.0/bin"])
        );
        assert_eq!(config_unset_argv("node_folder").unwrap(), v(&["config", "--unset=node_folder"]));
    }

    #[test]
    fn cli_settings_refuse_relative_flag_shaped_and_multi_line_paths() {
        for key in ["cli_path.codex", "node_folder"] {
            for bad in ["", "codex", "bin/codex", "-x", "--set", "/opt/a\n--unset=x", "/opt/\tx"] {
                assert!(config_set_argv(key, bad).is_err(), "{key} accepted {bad:?}");
            }
        }
        for unknown in ["cli_path.cursor", "cli_path.", "cli_path", "node", "node_folders"] {
            assert!(config_set_argv(unknown, "/opt/x").is_err(), "accepted key {unknown:?}");
            assert!(config_unset_argv(unknown).is_err(), "unset accepted key {unknown:?}");
        }
    }

    #[test]
    fn cli_setting_keys_match_the_python_settings() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../packages/cli/khipu/config.py");
        let src = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
        let line = src.split("CLI_SETTINGS = (").nth(1).unwrap().split(')').next().unwrap();
        for k in CLI_PATH_KEYS.iter().chain(std::iter::once(&NODE_FOLDER_KEY)) {
            assert!(line.contains(&format!("\"{k}\"")), "{k} missing from CLI_SETTINGS");
        }
    }

    /// Sign in now on an untracked folder (Section "Adding an account by
    /// hand"): the app saves the account first, then calls the unchanged
    /// `khipu_account_sign_in`. Before the save the folder is not in the list
    /// and no sign-in argv can be built; after it, the exact argv is.
    #[test]
    fn sign_in_now_needs_the_save_first() {
        let before = serde_json::json!({"usage_accounts": {"accounts": [
            {"label": "Claude", "harness": "claude"}
        ], "user_home": "/home/owner"}});
        assert!(sign_in_argv(&before, "claude", Some("~/.claude-old")).is_err());
        let after = serde_json::json!({"usage_accounts": {"accounts": [
            {"label": "Claude", "harness": "claude"},
            {"label": "Claude old", "harness": "claude", "home": "~/.claude-old"}
        ], "user_home": "/home/owner"}});
        assert_eq!(
            sign_in_argv(&after, "claude", Some("~/.claude-old")).unwrap(),
            v(&["usage", "--sign-in", "--account=Claude old", "--account-key=claude|~/.claude-old"])
        );
        // A refused save leaves the list as it was: still no sign-in.
        assert!(sign_in_argv(&before, "claude", Some("/home/owner/.claude-old")).is_err());
    }
}

#[cfg(test)]
mod work_command_tests {
    use super::*;

    fn change() -> LedgerChange {
        LedgerChange::default()
    }

    #[test]
    fn work_and_ledger_stay_off_the_generic_path() {
        assert!(!ALLOWED_SUBCOMMANDS.contains(&"work"));
        assert!(!ALLOWED_SUBCOMMANDS.contains(&"ledger"));
    }

    #[test]
    fn reads_are_fixed_and_join_the_project_to_its_flag() {
        assert_eq!(work_view_argv(), vec!["work", "--json"]);
        assert_eq!(ledger_list_argv("owner/atlas").unwrap(), vec!["ledger", "list", "--json", "--project=owner/atlas"]);
        assert_eq!(
            ledger_get_argv("atlas", "s-3.2").unwrap(),
            vec!["ledger", "get", "--json", "--project=atlas", "--", "s-3.2"]
        );
        assert_eq!(ledger_removed_argv("atlas").unwrap(), vec!["ledger", "removed", "--json", "--project=atlas"]);
    }

    #[test]
    fn a_set_carries_from_app_its_fields_and_the_state_it_was_shown() {
        let c = LedgerChange {
            state: Some("DONE".into()),
            note: Some("Use the provider we pay for.\nKeep email as the fallback.".into()),
            if_state: Some("NEEDS OWNER".into()),
            ..change()
        };
        assert_eq!(
            ledger_set_argv("atlas", "n-7", &c).unwrap(),
            vec![
                "ledger", "set", "--json", "--from-app", "--project=atlas", "--state=DONE",
                "--note=Use the provider we pay for.\nKeep email as the fallback.", "--if-state=NEEDS OWNER",
                "--", "n-7",
            ]
        );
    }

    #[test]
    fn a_value_starting_with_a_dash_stays_a_value() {
        let c = LedgerChange { evidence: Some("--state=DROPPED".into()), ..change() };
        let argv = ledger_set_argv("atlas", "s-1", &c).unwrap();
        assert!(argv.contains(&"--evidence=--state=DROPPED".to_string()));
        assert!(!argv.contains(&"--state=DROPPED".to_string()));
    }

    #[test]
    fn an_empty_field_clears_it_but_a_change_needs_something() {
        let c = LedgerChange { link: Some(String::new()), ..change() };
        assert!(ledger_set_argv("atlas", "s-1", &c).unwrap().contains(&"--link=".to_string()));
        assert!(ledger_set_argv("atlas", "s-1", &change()).is_err());
        let blank_note = LedgerChange { note: Some("   ".into()), ..change() };
        assert!(ledger_set_argv("atlas", "s-1", &blank_note).is_err());
    }

    #[test]
    fn one_line_fields_refuse_a_line_break_and_bad_ids_are_refused() {
        let c = LedgerChange { title: Some("one\ntwo".into()), ..change() };
        assert!(ledger_set_argv("atlas", "s-1", &c).is_err());
        for bad in ["", "-x", "--state", "a b", "a#b", "s-1\n"] {
            assert!(checked_ledger_id(bad).is_err(), "{bad:?}");
        }
        for bad in ["", "a#b", "a\nb"] {
            assert!(checked_ledger_project(bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn remove_asks_for_a_known_children_choice() {
        assert_eq!(
            ledger_remove_argv("atlas", "g-1", Some("duplicate"), Some("move-up")).unwrap(),
            vec!["ledger", "remove", "--json", "--from-app", "--project=atlas", "--children=move-up",
                 "--note=duplicate", "--", "g-1"]
        );
        assert_eq!(
            ledger_remove_argv("atlas", "s-1", None, None).unwrap(),
            vec!["ledger", "remove", "--json", "--from-app", "--project=atlas", "--", "s-1"]
        );
        assert!(ledger_remove_argv("atlas", "g-1", None, Some("--all")).is_err());
        assert_eq!(
            ledger_restore_argv("atlas", "s-1").unwrap(),
            vec!["ledger", "restore", "--json", "--from-app", "--project=atlas", "--", "s-1"]
        );
    }

    #[test]
    fn import_runs_the_fixed_argv_with_the_path_after_the_separator() {
        assert_eq!(
            ledger_import_argv("owner/atlas", "/data/ledger-inbox/tracker.json", true).unwrap(),
            vec!["ledger", "import", "--json", "--from-app", "--project=owner/atlas", "--dry-run", "--",
                 "/data/ledger-inbox/tracker.json"]
        );
        assert_eq!(
            ledger_import_argv("atlas", "/data/ledger-inbox/tracker.json", false).unwrap(),
            vec!["ledger", "import", "--json", "--from-app", "--project=atlas", "--", "/data/ledger-inbox/tracker.json"]
        );
        // A name that starts with a dash stays a path after `--`.
        assert_eq!(ledger_import_argv("atlas", "/data/-x.json", false).unwrap().last().unwrap(), "/data/-x.json");
    }

    #[test]
    fn import_refuses_a_path_that_is_not_one_full_path_and_a_bad_project() {
        for bad in ["", "-x", "--dry-run", "tracker.json", "~/tracker.json", "/a\nb.json", "/a\u{0}b", " /a.json"] {
            assert!(ledger_import_argv("atlas", bad, false).is_err(), "{bad:?}");
        }
        assert!(ledger_import_argv("atlas", &format!("/{}", "a".repeat(5000)), false).is_err());
        for bad in ["", "a#b", "a\nb"] {
            assert!(ledger_import_argv(bad, "/data/tracker.json", false).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn the_ledger_inbox_is_a_folder_in_the_data_folder() {
        assert_eq!(ledger_inbox_dir(Path::new("/data")), PathBuf::from("/data/ledger-inbox"));
    }

    #[test]
    fn hide_and_same_take_only_ids() {
        assert_eq!(work_hide_argv("42", true).unwrap(), vec!["work", "hide", "--", "42"]);
        assert_eq!(work_hide_argv("42", false).unwrap(), vec!["work", "unhide", "--", "42"]);
        assert!(work_hide_argv("--json", true).is_err());
        assert_eq!(
            work_same_argv("owner/atlas", "n-7", "42", true).unwrap(),
            vec!["work", "same", "--", "owner/atlas#n-7", "42"]
        );
        assert_eq!(work_same_argv("atlas", "n-7", "42", false).unwrap()[1], "not-same");
        assert!(work_same_argv("atlas", "n-7", "4 2", true).is_err());
    }

    fn ids(n: usize) -> Vec<String> {
        (1..=n).map(|i| i.to_string()).collect()
    }

    #[test]
    fn a_bulk_close_is_one_call_with_every_id_and_the_reason() {
        let argv = owed_change_argv("close", &ids(50), "Merged in the release", None, None).unwrap();
        let joined = (1..=50).map(|i| i.to_string()).collect::<Vec<_>>().join(",");
        assert_eq!(argv, vec!["owed".to_string(), format!("--close={joined}"), "--reason=Merged in the release".to_string()]);
        // The same id twice goes once; spaces round an id are dropped.
        assert_eq!(
            owed_change_argv("close", &["7".into(), " 8 ".into(), "7".into()], "done", None, None).unwrap(),
            vec!["owed", "--close=7,8", "--reason=done"]
        );
    }

    #[test]
    fn a_move_names_the_new_owner_and_an_agent_s_label() {
        assert_eq!(
            owed_change_argv("reassign", &ids(2), "Job items", Some("agent"), Some("orchestrator")).unwrap(),
            vec!["owed", "--reassign=1,2", "--to=agent", "--label=orchestrator", "--reason=Job items"]
        );
        assert_eq!(
            owed_change_argv("reassign", &ids(1), "Mine after all", Some("user"), None).unwrap(),
            vec!["owed", "--reassign=1", "--to=user", "--reason=Mine after all"]
        );
        // A label belongs only to an agent; a blank one is left out.
        assert_eq!(
            owed_change_argv("reassign", &ids(1), "x", Some("assistant"), Some("  ")).unwrap(),
            vec!["owed", "--reassign=1", "--to=assistant", "--reason=x"]
        );
        assert!(owed_change_argv("reassign", &ids(1), "x", Some("assistant"), Some("builder")).is_err());
        assert!(owed_change_argv("reassign", &ids(1), "x", None, None).is_err());
        assert!(owed_change_argv("reassign", &ids(1), "x", Some("everyone"), None).is_err());
        assert!(owed_change_argv("reassign", &ids(1), "x", Some("agent"), Some("a\nb")).is_err());
        assert!(owed_change_argv("close", &ids(1), "x", Some("agent"), None).is_err());
    }

    #[test]
    fn a_bulk_change_refuses_what_is_not_an_id_a_reason_or_an_action() {
        for bad in [vec![], vec!["".to_string()], vec!["1,2".to_string()], vec!["-1".to_string()],
                    vec!["--reopen=3".to_string()], vec!["0".to_string()], vec!["12a".to_string()]] {
            assert!(owed_change_argv("close", &bad, "done", None, None).is_err(), "{bad:?}");
        }
        for reason in ["", "   ", "one\ntwo"] {
            assert!(owed_change_argv("close", &ids(2), reason, None, None).is_err(), "{reason:?}");
        }
        assert!(owed_change_argv("close", &ids(1), &"x".repeat(OWED_REASON_MAX_CHARS + 1), None, None).is_err());
        for action in ["delete", "reopen", "snooze", "--close", ""] {
            assert!(owed_change_argv(action, &ids(1), "x", None, None).is_err(), "{action:?}");
        }
        assert!(owed_change_argv("close", &ids(OWED_IDS_MAX + 1), "x", None, None).is_err());
        // A reason that looks like a flag stays the reason's value.
        let argv = owed_change_argv("close", &ids(1), "--reopen=1", None, None).unwrap();
        assert_eq!(argv.last().unwrap(), "--reason=--reopen=1");
    }

    #[test]
    fn the_clean_up_runs_on_fixed_argv_with_the_plan_kept_where_khipu_chose() {
        let dir = PathBuf::from("/data/khipu");
        let path = owed_tidy_plan_path(&dir, "owner/atlas");
        assert!(path.starts_with("/data/khipu/state/owed-tidy"));
        assert_eq!(path.extension().and_then(|e| e.to_str()), Some("json"));
        assert_ne!(path, owed_tidy_plan_path(&dir, "owner_atlas"), "two projects never share a plan file");
        assert_eq!(path, owed_tidy_plan_path(&dir, "owner/atlas"));
        let shown = path.to_string_lossy().to_string();
        assert_eq!(
            owed_tidy_plan_argv("owner/atlas", &path).unwrap(),
            vec!["owed".to_string(), "tidy".into(), "--project=owner/atlas".into(), format!("--plan-out={shown}")]
        );
        assert_eq!(
            owed_tidy_apply_argv("owner/atlas", &path).unwrap(),
            vec!["owed".to_string(), "tidy".into(), "--project=owner/atlas".into(), format!("--apply={shown}")]
        );
        assert_eq!(
            owed_tidy_undo_argv("owner/atlas", "tidy-20261016T120000Z-a1b2c3").unwrap(),
            vec!["owed", "tidy", "--project=owner/atlas", "--undo=tidy-20261016T120000Z-a1b2c3"]
        );
        for bad in ["", "--apply=x", "tidy 1", "../x", "tidy-1\n"] {
            assert!(owed_tidy_undo_argv("atlas", bad).is_err(), "{bad:?}");
        }
        assert!(owed_tidy_plan_argv("a#b", &path).is_err());
    }

    #[test]
    fn the_app_reads_the_plan_without_its_item_list() {
        let raw = r#"{"format":"khipu-owed-tidy-plan","version":1,"project":"atlas",
            "totals":{"open":3},"samples":{"move":[]},"items":[{"id":1},{"id":2}]}"#;
        let shown: Value = serde_json::from_str(&owed_tidy_plan_for_app(raw).unwrap()).unwrap();
        assert!(shown.get("items").is_none());
        assert_eq!(shown["totals"]["open"], 3);
        assert_eq!(shown["items_count"], 2);
        assert!(owed_tidy_plan_for_app(r#"{"format":"something-else","version":1}"#).is_err());
        assert!(owed_tidy_plan_for_app("not json").is_err());
    }

    #[test]
    fn the_clean_up_is_not_cut_short_but_other_owed_calls_are() {
        let v = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        assert!(cli_has_no_time_limit(&v(&["owed", "tidy", "--project=atlas", "--plan-out=/x.json"])));
        assert!(!cli_has_no_time_limit(&v(&["owed", "--close=1,2", "--reason=x"])));
    }

    #[test]
    fn the_owed_limits_and_the_github_switch_are_settable_and_resettable() {
        let v = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        for key in ["owed_orchestrator_slice_max", "owed_page_max", "owed_bulk_max", "owed_tidy_calls"] {
            assert_eq!(config_set_argv(key, " 0 ").unwrap(), v(&["config", "--set", key, "0"]));
            assert_eq!(config_set_argv(key, "750").unwrap(), v(&["config", "--set", key, "750"]));
            assert_eq!(config_unset_argv(key).unwrap(), v(&["config", &format!("--unset={key}")]));
            for bad in ["-1", "1.5", "", "lots"] {
                assert!(config_set_argv(key, bad).is_err(), "{key}={bad:?}");
            }
        }
        assert_eq!(config_set_argv("owed_pr_check_github", "off").unwrap(), v(&["config", "--set", "owed_pr_check_github", "off"]));
        assert_eq!(config_set_argv("owed_pr_check_github", " on ").unwrap(), v(&["config", "--set", "owed_pr_check_github", "on"]));
        assert!(config_set_argv("owed_pr_check_github", "maybe").is_err());
        assert_eq!(config_unset_argv("owed_pr_check_github").unwrap(), v(&["config", "--unset=owed_pr_check_github"]));
    }

    fn v(a: &[&str]) -> Vec<String> {
        a.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn settings_work_reads_are_fixed() {
        assert_eq!(ledger_projects_argv(), v(&["ledger", "projects", "--json"]));
        assert_eq!(work_hidden_argv(), v(&["work", "hidden", "--json"]));
        assert_eq!(
            work_check_argv("/runners/atlas/state/work-summary.json").unwrap(),
            v(&["work", "check", "--json", "--", "/runners/atlas/state/work-summary.json"])
        );
        assert_eq!(work_check_argv(" ~/r/w.json ").unwrap()[4], "~/r/w.json");
        for bad in ["", "relative/w.json", "--json", "/a\nb.json"] {
            assert!(work_check_argv(bad).is_err(), "accepted {bad:?}");
        }
    }

    #[test]
    fn a_project_change_is_the_owners_and_names_one_change() {
        assert_eq!(
            ledger_project_argv("owner/atlas", Some(" Atlas "), None).unwrap(),
            v(&["ledger", "project", "--json", "--from-app", "--project=owner/atlas", "--rename=Atlas"])
        );
        assert_eq!(
            ledger_project_argv("atlas", None, Some(true)).unwrap(),
            v(&["ledger", "project", "--json", "--from-app", "--project=atlas", "--hide"])
        );
        assert_eq!(ledger_project_argv("atlas", Some(""), Some(false)).unwrap()[5..], v(&["--rename=", "--show"]));
        assert!(ledger_project_argv("atlas", None, None).is_err());
        assert!(ledger_project_argv("atlas", Some(&"x".repeat(81)), None).is_err());
        assert!(ledger_project_argv("atlas", Some("two\nlines"), None).is_err());
        assert!(ledger_project_argv("a#b", None, Some(true)).is_err());
    }

    #[test]
    fn every_work_and_ledger_limit_is_settable_and_resettable() {
        for key in WORK_COUNT_KEYS.iter().chain(WORK_DURATION_KEYS) {
            assert_eq!(config_set_argv(key, " 0 ").unwrap(), v(&["config", "--set", key, "0"]));
            assert_eq!(config_set_argv(key, "300").unwrap()[3], "300");
            for bad in ["", "-1", "1.5", "abc", "--unset", "auto", "99999999999"] {
                assert!(config_set_argv(key, bad).is_err(), "{key} accepted {bad:?}");
            }
            assert_eq!(config_unset_argv(key).unwrap(), v(&["config", &format!("--unset={key}")]));
        }
        assert_eq!(config_set_argv("work_stale_after", "auto").unwrap()[3], "auto");
        assert_eq!(config_set_argv("work_stale_after", "600").unwrap()[3], "600");
        assert!(config_set_argv("work_stale_after", "automatic").is_err());
        assert!(config_unset_argv("work_stale_after").is_ok());
        // Not offered by the app: the owner word is set where it is used.
        assert!(config_set_argv("ledger_owner_word", "NEEDS MATT").is_err());
    }

    #[test]
    fn the_summary_files_are_checked_and_re_serialised() {
        let raw = r#"[{"project":"owner/atlas","name":" Atlas ","file":"/r/atlas/state/work-summary.json","on":false},
                     {"project":"lumen","file":"~/r/lumen/w.json","show_now":false}]"#;
        let got: serde_json::Value = serde_json::from_str(&config_set_argv("work_projects", raw).unwrap()[3]).unwrap();
        assert_eq!(
            got,
            serde_json::json!([
                {"project": "owner/atlas", "name": "Atlas", "file": "/r/atlas/state/work-summary.json", "on": false, "show_now": true},
                {"project": "lumen", "file": "~/r/lumen/w.json", "on": true, "show_now": false}
            ])
        );
        assert_eq!(config_set_argv("work_projects", "[]").unwrap()[3], "[]");
        for bad in [
            "{}",
            r#"[{"project":"a","file":"relative.json"}]"#,
            r#"[{"project":"a#b","file":"/w.json"}]"#,
            r#"[{"project":"a","file":"/w.json","extra":1}]"#,
            r#"[{"project":"a","file":"/w.json","on":"yes"}]"#,
            r#"[{"project":"a"}]"#,
        ] {
            assert!(config_set_argv("work_projects", bad).is_err(), "accepted {bad}");
        }
        let many: Vec<String> = (0..65).map(|i| format!(r#"{{"project":"p{i}","file":"/w{i}.json"}}"#)).collect();
        assert!(config_set_argv("work_projects", &format!("[{}]", many.join(","))).is_err());
        assert!(config_unset_argv("work_projects").is_ok());
    }
}
