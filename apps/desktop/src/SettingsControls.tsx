/* Settings controls for the CLI-owned settings the app used to leave to the
   terminal (docs/plans/2026-09-30-settings-parity.md). Each card reads its
   state from the CLI, writes through one fixed-argv Tauri command, then
   re-reads: what the screen shows is what the CLI reports, never what it sent.
   A refused change shows the CLI's own error text beside the field. */

import { useEffect, useId, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Loader2 } from "lucide-react";
import { Tag } from "./ui";
import { callCli, useCliState } from "./settingsCli";
import type { ConfigShape, JobsShape } from "./settingsCli";

// ---------------------------------------------------------------------------
// One labelled text field with Save (and optional Reset).
// ---------------------------------------------------------------------------

export function SettingField({
  label,
  help,
  value,
  placeholder,
  mono = false,
  inputMode,
  lockedBy,
  saveLabel = "Save",
  onSave,
  onReset,
  resetLabel = "Reset",
  canReset = false,
  type = "text",
  autoComplete = "off",
  clearOnSave = false,
  narrow = false,
}: {
  label: string;
  help?: ReactNode;
  /** What the CLI currently reports. The draft follows it after a re-read. */
  value: string;
  placeholder?: string;
  mono?: boolean;
  inputMode?: "decimal" | "text" | "url";
  /** Set when the environment decides the value: the field is read-only. */
  lockedBy?: string | null;
  saveLabel?: string;
  /** Resolves to null on success, or the CLI's error text. */
  onSave: (next: string) => Promise<string | null>;
  onReset?: () => Promise<string | null>;
  resetLabel?: string;
  canReset?: boolean;
  type?: "text" | "password";
  autoComplete?: string;
  /** Write-only fields (a token) empty themselves once the write lands. */
  clearOnSave?: boolean;
  /** A short number: do not stretch the input across the card. */
  narrow?: boolean;
}) {
  const id = useId();
  const [draft, setDraft] = useState(value);
  const [busy, setBusy] = useState<"save" | "reset" | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDraft(value);
  }, [value]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy || lockedBy) return;
    setBusy("save");
    const result = await onSave(draft);
    setError(result);
    if (result === null && clearOnSave) setDraft("");
    setBusy(null);
  }

  async function reset() {
    if (!onReset || busy || lockedBy) return;
    setBusy("reset");
    setError(await onReset());
    setBusy(null);
  }

  // Nothing to save until the draft differs from what the CLI reports.
  const unchanged = draft.trim() === value.trim();

  const describedBy = [help ? `${id}-help` : null, lockedBy ? `${id}-lock` : null, error ? `${id}-err` : null]
    .filter(Boolean)
    .join(" ");

  return (
    <form className={narrow ? "set-field narrow" : "set-field"} onSubmit={(e) => void submit(e)}>
      <div className="set-labelrow">
        <label className="set-label" htmlFor={id}>
          {label}
        </label>
        {lockedBy ? <Tag tone="neutral">Locked</Tag> : null}
      </div>
      {lockedBy ? (
        <p id={`${id}-lock`} className="set-help">
          Set by the environment variable <code>{lockedBy}</code>. Unset it to change this here.
        </p>
      ) : null}
      <div className="toolbar">
        <input
          id={id}
          className={mono ? "mono" : undefined}
          type={type}
          value={draft}
          placeholder={placeholder}
          inputMode={inputMode}
          autoComplete={autoComplete}
          spellCheck={false}
          disabled={Boolean(lockedBy)}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy || undefined}
          onChange={(e) => setDraft(e.target.value)}
        />
        <button type="submit" disabled={busy !== null || Boolean(lockedBy) || unchanged}>
          {busy === "save" ? <Loader2 size={14} className="spin" aria-hidden /> : null}
          {saveLabel}
        </button>
        {onReset && canReset ? (
          <button type="button" disabled={busy !== null || Boolean(lockedBy)} onClick={() => void reset()}>
            {busy === "reset" ? <Loader2 size={14} className="spin" aria-hidden /> : null}
            {resetLabel}
          </button>
        ) : null}
      </div>
      {help ? (
        <p id={`${id}-help`} className="set-help">
          {help}
        </p>
      ) : null}
      {error ? (
        <p id={`${id}-err`} className="set-error" role="alert">
          {error}
        </p>
      ) : null}
    </form>
  );
}

function fmtNumber(n: number | undefined): string {
  return n === undefined || n === null ? "" : String(n);
}

/** Client-side range check for immediate feedback. The Tauri command and the
 *  CLI enforce the same range; this only saves a round trip. */
export function unitRangeError(raw: string, allowZero: boolean): string | null {
  const v = Number(raw.trim());
  if (raw.trim() === "" || Number.isNaN(v)) return "Enter a number.";
  if (allowZero ? v < 0 || v > 1 : v <= 0 || v > 1) {
    return allowZero ? "Enter a number from 0 to 1." : "Enter a number above 0 and up to 1.";
  }
  return null;
}

async function writeConfig(key: string, value: string, reload: () => Promise<unknown>): Promise<string | null> {
  const res = await callCli("khipu_config_set", { key, value });
  await reload();
  return res.ok ? null : res.error;
}

async function unsetConfig(key: string, reload: () => Promise<unknown>): Promise<string | null> {
  const res = await callCli("khipu_config_unset", { key });
  await reload();
  return res.ok ? null : res.error;
}

// ---------------------------------------------------------------------------
// Capture & models: capture_mode, the two similarity thresholds, user_aliases.
// ---------------------------------------------------------------------------

const CAPTURE_MODE_OPTIONS: ReadonlyArray<readonly [string, string, string]> = [
  ["legacy", "Files only", "The older capture script writes session files. Khipu stores nothing."],
  ["dual", "Files and Khipu", "The capture script writes files and Khipu stores each capture too."],
  ["hub", "Khipu only", "Khipu stores each capture. The session files are rebuilt from Khipu."],
];

const SIMILARITY_FIELDS: ReadonlyArray<{
  key: "dedup_similarity" | "commitment_close_similarity";
  env: string;
  label: string;
  help: string;
}> = [
  {
    key: "dedup_similarity",
    env: "KHIPU_DEDUP_SIMILARITY",
    label: "Merge near-duplicate captures at",
    help: "Two captures in the same project within five minutes are merged when they are at least this similar, from 0 to 1.",
  },
  {
    key: "commitment_close_similarity",
    env: "KHIPU_COMMITMENT_CLOSE_SIMILARITY",
    label: "Close an open commitment at",
    help: "A decision or finished item closes an open commitment in the same project when it is at least this similar, from 0 to 1.",
  },
];

export function CaptureTuningCard({ active }: { active: boolean }) {
  const cfg = useCliState<ConfigShape>("khipu_config_show", active);
  const modeId = useId();
  const [modeError, setModeError] = useState<string | null>(null);
  const [modeBusy, setModeBusy] = useState(false);
  const c = cfg.data;
  const mode = c?.capture_mode ?? "dual";
  const modeLocked = c?.capture_mode_source === "env";
  const fileAliases = Array.isArray(c?.config?.user_aliases) ? (c?.config?.user_aliases as string[]) : [];
  const aliases = c?.user_aliases ?? [];
  const aliasesFromEnv = aliases.length > 0 && aliases.join(",") !== fileAliases.join(",");

  async function changeMode(next: string) {
    setModeBusy(true);
    setModeError(await writeConfig("capture_mode", next, cfg.reload));
    setModeBusy(false);
  }

  return (
    <div className="section-card">
      <div className="section-head">How captures are stored</div>
      <div className="section-body">
        {cfg.error && !c ? (
          <p className="set-error" role="alert">
            Could not read the settings: {cfg.error}
          </p>
        ) : null}
        <div className="set-field">
          <div className="set-labelrow">
            <label className="set-label" htmlFor={modeId}>
              Where a capture is written
            </label>
            {modeLocked ? <Tag tone="neutral">Locked</Tag> : null}
          </div>
          {modeLocked ? (
            <p className="set-help">
              Set by the environment variable <code>KHIPU_CAPTURE_MODE</code>. Unset it to change this here.
            </p>
          ) : null}
          <div className="toolbar">
            <select
              id={modeId}
              value={mode}
              disabled={!c || modeLocked || modeBusy}
              aria-describedby={`${modeId}-help`}
              onChange={(e) => void changeMode(e.target.value)}
            >
              {CAPTURE_MODE_OPTIONS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            {modeBusy ? <Loader2 size={14} className="spin" aria-hidden /> : null}
          </div>
          <p id={`${modeId}-help`} className="set-help">
            {CAPTURE_MODE_OPTIONS.find(([v]) => v === mode)?.[2]}
          </p>
          {modeError ? (
            <p className="set-error" role="alert">
              {modeError}
            </p>
          ) : null}
        </div>

        {SIMILARITY_FIELDS.map((f) => {
          const row = c?.float_settings?.[f.key];
          return (
            <SettingField
              key={f.key}
              label={f.label}
              mono
              narrow
              inputMode="decimal"
              value={fmtNumber(row?.value)}
              lockedBy={row?.source === "env" ? f.env : null}
              help={
                <>
                  {f.help} Default {fmtNumber(row?.default)}.
                </>
              }
              onSave={async (v) => {
                const bad = unitRangeError(v, true);
                return bad ?? writeConfig(f.key, v, cfg.reload);
              }}
            />
          );
        })}

        <SettingField
          label="Your names"
          value={aliases.join(", ")}
          placeholder="matt, matthew"
          help={
            <>
              Names that mean you when Khipu decides who owns a commitment. Separate them with commas.
              {aliasesFromEnv ? (
                <>
                  {" "}
                  The environment variable <code>KHIPU_USER_ALIASES</code> is overriding what is saved here.
                </>
              ) : null}
            </>
          }
          onSave={(v) => writeConfig("user_aliases", v, cfg.reload)}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Another Mac: gateway_url.
// ---------------------------------------------------------------------------

export function GatewayUrlCard({ active }: { active: boolean }) {
  const cfg = useCliState<ConfigShape>("khipu_config_show", active);
  const c = cfg.data;
  const effective = c?.gateway_url ?? "";
  const fileValue = typeof c?.config?.gateway_url === "string" ? (c.config.gateway_url as string) : "";
  const fromEnv = Boolean(effective) && effective !== fileValue;
  return (
    <div className="section-card">
      <div className="section-head">Gateway address</div>
      <div className="section-body">
        <SettingField
          label="Public https address of the Khipu gateway"
          mono
          inputMode="url"
          placeholder="https://khipu.example.org"
          value={fileValue}
          saveLabel="Save address"
          help={
            <>
              Cloud harnesses reach your memory here, and a bearer token travels with each request, so the address
              must start with https://. Leave it empty and save to clear it.
              {fromEnv ? (
                <>
                  {" "}
                  The environment variable <code>KHIPU_GATEWAY_URL</code> is in force right now and overrides the
                  saved address: <code>{effective}</code>.
                </>
              ) : null}
            </>
          }
          onSave={(v) => writeConfig("gateway_url", v, cfg.reload)}
        />
        {cfg.error && !c ? (
          <p className="set-error" role="alert">
            Could not read the settings: {cfg.error}
          </p>
        ) : null}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Data: path overrides with Reset.
// ---------------------------------------------------------------------------

const PATH_ROWS: ReadonlyArray<readonly [string, string, string]> = [
  ["memory_root", "Session file folder", "Where the older file-based memory keeps its session files."],
  ["memory_repo", "Memory git repository", "The repository that folder is pushed from, usually its parent."],
  ["capture_v2", "Capture script", "The older capture script that Files and Khipu mode chains to."],
  ["graph_sqlite", "Graph database file", "The older graph file that the graph sync copies from."],
  ["gemini_key_file", "Gemini key file", "A last-resort file the Gemini key is read from when nothing else has it."],
];

export function PathOverridesCard({ active }: { active: boolean }) {
  const cfg = useCliState<ConfigShape>("khipu_config_show", active);
  const paths = cfg.data?.paths ?? {};
  return (
    <div className="section-card">
      <div className="section-head">Older file locations</div>
      <div className="section-body">
        <p className="set-help">
          Most people never change these. Reset returns a location to unset, meaning Khipu makes no guess about it.
        </p>
        {cfg.error && !cfg.data ? (
          <p className="set-error" role="alert">
            Could not read the settings: {cfg.error}
          </p>
        ) : null}
        {PATH_ROWS.map(([key, label, what]) => {
          const row = paths[key];
          const source = row?.source ?? "unset";
          const envName = source.startsWith("env:") ? source.slice(4) : null;
          return (
            <SettingField
              key={key}
              label={label}
              mono
              value={row?.value ?? ""}
              placeholder="Not set"
              lockedBy={envName}
              canReset={source === "file"}
              onReset={() => unsetConfig(key, cfg.reload)}
              help={
                <>
                  {what}
                  {row?.value && row.exists === false ? (
                    <>
                      {" "}
                      <Tag tone="warn">Not found on this Mac</Tag>
                    </>
                  ) : null}
                </>
              }
              onSave={(v) => writeConfig(key, v, cfg.reload)}
            />
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Advanced: per-job install / remove.
// ---------------------------------------------------------------------------

/** Plain one-liners taken from what each job runs (khipu/jobs.py `run_nightly`,
 *  `run_monthly`, `run_graph_build`, the notes-watch and queue-drain plists and
 *  their comments, and khipu/recall_daemon.py). */
const JOB_ROWS: ReadonlyArray<readonly [string, string, string]> = [
  [
    "nightly",
    "Nightly upkeep",
    "Consolidates the day's captures, indexes anything new, ages out stale commitments, and builds topic briefs when that switch is on.",
  ],
  ["monthly", "Monthly topic pass", "Runs the monthly topic pass on the first of the month, with its own model."],
  [
    "graph_build",
    "Graph rebuild",
    "Rebuilds the knowledge graph each night and copies an off-site backup when one is due.",
  ],
  [
    "notes_watch",
    "Notes watcher",
    "Picks up edited notes soon after you save them, instead of waiting for the next session to end.",
  ],
  [
    "queue_drain",
    "Queued captures",
    "Every 5 minutes, saves captures that another harness queued but could not store right away.",
  ],
  [
    "recall_daemon",
    "Recall service",
    "Keeps a small process running so recall answers each prompt faster. It has its own switch under Optional features.",
  ],
];

function jobWhen(iso: string | null | undefined): string {
  if (!iso) return "not run yet";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "not run yet";
  const hours = Math.round((Date.now() - t) / 3_600_000);
  if (hours < 1) return "ran within the hour";
  if (hours < 48) return `ran ${hours} h ago`;
  return `ran ${Math.round(hours / 24)} d ago`;
}

export function JobsCard({ active }: { active: boolean }) {
  const jobs = useCliState<JobsShape>("khipu_jobs_status", active);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});

  async function run(name: string, verb: "install" | "uninstall") {
    setBusy(name);
    setConfirm(null);
    const res = await callCli(verb === "install" ? "khipu_job_install" : "khipu_job_uninstall", { name });
    await jobs.reload();
    setErrors((prev) => {
      const next = { ...prev };
      if (res.ok) delete next[name];
      else next[name] = res.error;
      return next;
    });
    setBusy(null);
  }

  return (
    <div className="section-card">
      <div className="section-head">Background jobs</div>
      <div className="section-body">
        <p className="set-help">
          These run on a schedule through macOS. Install adds the job; Remove takes it off the schedule and leaves
          its logs.
        </p>
        {jobs.error && !jobs.data ? (
          <p className="set-error" role="alert">
            Could not read the jobs: {jobs.error}
          </p>
        ) : null}
        <div className="rows">
          {JOB_ROWS.map(([name, label, what]) => {
            const j = jobs.data?.[name];
            const installed = j?.plist_loaded === true;
            return (
              <div key={name} className="job-row">
                <div className="job-main">
                  <span className="job-name">{label}</span>
                  <span className="job-desc">{what}</span>
                  <span className="job-meta">
                    {jobs.data
                      ? installed
                        ? /KeepAlive/.test(j?.next_schedule ?? "")
                          ? "Always running"
                          : `${j?.next_schedule ?? "scheduled"} · ${jobWhen(j?.last_run_iso)}`
                        : "Not installed"
                      : "Reading…"}
                  </span>
                  {errors[name] ? (
                    <span className="set-error" role="alert">
                      {errors[name]}
                    </span>
                  ) : null}
                </div>
                {jobs.data ? (
                  installed ? (
                    confirm === name ? (
                      <span className="job-actions">
                        <button type="button" className="danger" disabled={busy !== null} onClick={() => void run(name, "uninstall")}>
                          Remove {label.toLowerCase()}
                        </button>
                        <button type="button" onClick={() => setConfirm(null)}>
                          Keep it
                        </button>
                      </span>
                    ) : (
                      <span className="job-actions">
                        <Tag tone="ok" dot>
                          Installed
                        </Tag>
                        <button
                          type="button"
                          disabled={busy !== null}
                          aria-label={`Remove ${label.toLowerCase()}`}
                          onClick={() => setConfirm(name)}
                        >
                          {busy === name ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                          Remove
                        </button>
                      </span>
                    )
                  ) : (
                    <button
                      type="button"
                      disabled={busy !== null}
                      aria-label={`Install ${label.toLowerCase()}`}
                      onClick={() => void run(name, "install")}
                    >
                      {busy === name ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                      Install
                    </button>
                  )
                ) : null}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Gateway bearer token: write-only.
// ---------------------------------------------------------------------------

type TokenStatus = { path?: string; exists?: boolean; mode?: string; bytes?: number };

export function GatewayTokenCard({ active }: { active: boolean }) {
  const status = useCliState<TokenStatus>("khipu_gateway_token_status", active);
  const s = status.data;
  return (
    <div className="section-card">
      <div className="section-head">
        Gateway token
        <span className="spacer" />
        {s ? (
          s.exists ? (
            <Tag tone="ok" dot>
              Stored
            </Tag>
          ) : (
            <Tag tone="warn" dot>
              Not stored
            </Tag>
          )
        ) : null}
      </div>
      <div className="section-body">
        <SettingField
          label="Bearer token"
          mono
          type="password"
          autoComplete="new-password"
          value=""
          placeholder={s?.exists ? "A token is stored. Paste a new one to replace it" : "Paste the token"}
          saveLabel="Save token"
          clearOnSave
          help={
            <>
              Aegis, a harness on this Mac, reads this token from a file to reach your gateway. Once saved, the token is never shown here again.
              {s?.path ? (
                <>
                  {" "}
                  File: <code>{s.path}</code>.
                </>
              ) : null}
              {s?.exists && s.bytes ? ` The stored token is ${s.bytes} bytes.` : ""}
            </>
          }
          onSave={async (v) => {
            const res = await callCli("khipu_gateway_token_set", { value: v });
            await status.reload();
            return res.ok ? null : res.error;
          }}
        />
        {status.error && !s ? (
          <p className="set-error" role="alert">
            Could not read the token status: {status.error}
          </p>
        ) : null}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Search index: make a profile active.
// ---------------------------------------------------------------------------

type EmbedProfile = { id: string; model?: string; dim?: number; active?: boolean };
type EmbedStatus = { active_profile?: string; profiles?: EmbedProfile[] };
type EmbedCoverage = { episodes?: { missing?: number }; topics?: { missing?: number } };

export function EmbedProfilesCard({ active }: { active: boolean }) {
  const status = useCliState<EmbedStatus>("khipu_embed_status", active);
  const [missing, setMissing] = useState<Record<string, number | "unknown">>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const profiles = status.data?.profiles ?? [];
  const key = profiles.map((p) => `${p.id}:${p.active ? 1 : 0}`).join("|");

  useEffect(() => {
    let cancelled = false;
    const inactive = profiles.filter((p) => !p.active);
    void (async () => {
      const found: Record<string, number | "unknown"> = {};
      for (const p of inactive) {
        const res = await callCli<EmbedCoverage>("khipu_embed_status", { profile: p.id });
        found[p.id] = res.ok
          ? (res.data.episodes?.missing ?? 0) + (res.data.topics?.missing ?? 0)
          : "unknown";
      }
      if (!cancelled) setMissing(found);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  async function activate(id: string) {
    setBusy(id);
    const res = await callCli("khipu_embed_activate", { profile: id });
    await status.reload();
    setError(res.ok ? null : res.error);
    setBusy(null);
  }

  if (profiles.length === 0) return null;
  return (
    <div className="section-card">
      <div className="section-head">Index profiles</div>
      <div className="section-body">
        <p className="set-help">
          Search reads one profile at a time. A profile can only be made active once every session and topic has
          its vectors. Finish indexing the missing items first: the app does that only for the active profile, so
          for another one run <code>khipu embed backfill --profile ID</code> in a terminal.
        </p>
        <div className="rows">
          {profiles.map((p) => {
            const m = missing[p.id];
            const blocked = !p.active && (m === undefined || m === "unknown" || m > 0);
            return (
              <div key={p.id} className="job-row">
                <div className="job-main">
                  <span className="job-name mono">{p.id}</span>
                  <span className="job-meta">
                    {p.model ?? "model unknown"}
                    {p.dim ? ` · ${p.dim} dimensions` : ""}
                    {!p.active && m === "unknown" ? " · could not check its coverage" : ""}
                    {!p.active && typeof m === "number" && m > 0 ? ` · ${m} items still missing vectors` : ""}
                    {!p.active && typeof m === "number" && m === 0 ? " · every item has vectors" : ""}
                  </span>
                </div>
                {p.active ? (
                  <Tag tone="accent">Active</Tag>
                ) : (
                  <button type="button" disabled={blocked || busy !== null} onClick={() => void activate(p.id)}>
                    {busy === p.id ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                    Make active
                  </button>
                )}
              </div>
            );
          })}
        </div>
        {error ? (
          <p className="set-error" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </div>
  );
}
