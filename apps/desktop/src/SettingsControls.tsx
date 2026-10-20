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
import { fmtAgo } from "./time";
import "./CaptureModels.css";
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

const SIMILARITY_ENV = {
  dedup_similarity: "KHIPU_DEDUP_SIMILARITY",
  commitment_close_similarity: "KHIPU_COMMITMENT_CLOSE_SIMILARITY",
} as const;

/** Owed's limits (`khipu.config.COUNT_SETTINGS`), each liftable with 0. */
const OWED_LIMIT_ROWS: ReadonlyArray<readonly [string, string, string]> = [
  [
    "owed_orchestrator_slice_max",
    "Items for the orchestrator at the start of a conversation",
    "How many items a job left for its orchestrator a conversation is shown when it starts.",
  ],
  ["owed_page_max", "Most items on one page of Owed", "How many items one read of Owed returns. Show more reads the next page."],
  ["owed_bulk_max", "Most items changed at once", "How many items one Close… or Move to… changes together."],
  [
    "owed_tidy_calls",
    "Most model calls for one clean-up plan",
    "For the items no rule can sort. Past it they are kept as they are.",
  ],
];

const OWED_GITHUB_KEY = "owed_pr_check_github";

/** Two cards that share one read of the config: when and where a
 *  conversation is saved, and how commitments are owned and closed. */
export function CaptureTuningCard({
  active,
  turns = 5,
  minutes = 20,
}: {
  active: boolean;
  /** The engine's own cadence, shown read-only. */
  turns?: number;
  minutes?: number;
}) {
  const cfg = useCliState<ConfigShape>("khipu_config_show", active);
  const modeId = useId();
  const [modeError, setModeError] = useState<string | null>(null);
  const [modeBusy, setModeBusy] = useState(false);
  const c = cfg.data;
  const readFailed = cfg.error != null && !c;
  const mode = c ? (c.capture_mode ?? "dual") : "";
  const modeLocked = c?.capture_mode_source === "env";
  const fileAliases = Array.isArray(c?.config?.user_aliases) ? (c?.config?.user_aliases as string[]) : [];
  const aliases = c?.user_aliases ?? [];
  const aliasesFromEnv = aliases.length > 0 && aliases.join(",") !== fileAliases.join(",");

  const owedLimits = c?.count_settings ?? {};
  const githubId = useId();
  const [githubBusy, setGithubBusy] = useState(false);
  const [githubError, setGithubError] = useState<string | null>(null);
  const githubStored = c?.config?.[OWED_GITHUB_KEY];
  const github = githubStored === "off" ? "off" : "on";

  async function changeGithub(next: string) {
    setGithubBusy(true);
    setGithubError(await writeConfig(OWED_GITHUB_KEY, next, cfg.reload));
    setGithubBusy(false);
  }

  async function changeMode(next: string) {
    setModeBusy(true);
    setModeError(await writeConfig("capture_mode", next, cfg.reload));
    setModeBusy(false);
  }

  function similarity(
    key: "dedup_similarity" | "commitment_close_similarity",
    label: string,
    help: string,
  ) {
    const row = c?.float_settings?.[key];
    return (
      <SettingField
        label={label}
        mono
        narrow
        inputMode="decimal"
        value={fmtNumber(row?.value)}
        lockedBy={row?.source === "env" ? SIMILARITY_ENV[key] : null}
        help={row?.default != null ? `${help} Default ${fmtNumber(row.default)}.` : help}
        onSave={async (v) => {
          const bad = unitRangeError(v, true);
          return bad ?? writeConfig(key, v, cfg.reload);
        }}
      />
    );
  }

  return (
    <>
      <div className="section-card">
        <div className="section-head">When Khipu saves a conversation</div>
        <div className="section-body">
          {readFailed ? (
            <p className="set-error" role="alert">
              Could not read the settings: {cfg.error}
            </p>
          ) : null}
          <p className="cm-readonly">
            After {turns} turns or {minutes} minutes, before a conversation is compacted, and when it ends. The same
            on every harness.
          </p>
          <p className="set-help">Keys, tokens and passwords are masked before anything is summarised or stored.</p>
          <div className="set-field">
            <div className="set-labelrow">
              <label className="set-label" htmlFor={modeId}>
                Where captures are written
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
                value={c ? mode : ""}
                disabled={!c || modeLocked || modeBusy}
                aria-describedby={c ? `${modeId}-help` : undefined}
                onChange={(e) => void changeMode(e.target.value)}
              >
                {c ? (
                  CAPTURE_MODE_OPTIONS.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))
                ) : (
                  <option value="">{readFailed ? "Could not read" : "Reading…"}</option>
                )}
              </select>
              {modeBusy ? <Loader2 size={14} className="spin" aria-hidden /> : null}
            </div>
            {c ? (
              <p id={`${modeId}-help`} className="set-help">
                {CAPTURE_MODE_OPTIONS.find(([v]) => v === mode)?.[2]}
              </p>
            ) : null}
            {modeError ? (
              <p className="set-error" role="alert">
                {modeError}
              </p>
            ) : null}
          </div>
          {similarity(
            "dedup_similarity",
            "Merge near-duplicate captures at",
            "From 0 to 1; higher merges only closer matches. A word matching rule can also merge close captures.",
          )}
        </div>
      </div>

      <div className="section-card">
        <div className="section-head">Commitments</div>
        <div className="section-body">
          <p className="set-help">
            A commitment is something a conversation said is still owed: a follow-up, a blocker, a question or a
            promise. They are listed on Owed.
          </p>
          <SettingField
            label="Your names"
            value={aliases.join(", ")}
            placeholder="matt, matthew"
            help={
              <>
                Names that mean you when Khipu decides who owns a commitment. Separate them with commas. Default:
                none.
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
          {similarity(
            "commitment_close_similarity",
            "Close a commitment when a later capture matches it at",
            "From 0 to 1. A word and completion rule can also close a commitment.",
          )}
          {OWED_LIMIT_ROWS.map(([key, label, help]) => {
            const row = owedLimits[key];
            return (
              <SettingField
                key={key}
                label={label}
                mono
                narrow
                inputMode="decimal"
                value={row ? String(row.value) : ""}
                canReset={row?.source === "file"}
                onReset={() => unsetConfig(key, cfg.reload)}
                help={row ? `${help} 0 means no limit. Default ${row.default}.` : `${help} 0 means no limit.`}
                onSave={async (v) => {
                  const t = v.trim();
                  if (!/^\d+$/.test(t)) return "Enter a whole number, 0 or more (0 means no limit).";
                  return writeConfig(key, t, cfg.reload);
                }}
              />
            );
          })}
          <div className="set-field">
            <div className="set-labelrow">
              <label className="set-label" htmlFor={githubId}>
                Ask GitHub whether a pull request merged
              </label>
            </div>
            <div className="toolbar">
              <select
                id={githubId}
                value={c ? github : ""}
                disabled={!c || githubBusy}
                aria-describedby={`${githubId}-help`}
                onChange={(e) => void changeGithub(e.target.value)}
              >
                {c ? (
                  <>
                    <option value="on">On</option>
                    <option value="off">Off</option>
                  </>
                ) : (
                  <option value="">{readFailed ? "Could not read" : "Reading…"}</option>
                )}
              </select>
              {githubBusy ? <Loader2 size={14} className="spin" aria-hidden /> : null}
            </div>
            <p id={`${githubId}-help`} className="set-help">
              An item that names pull requests closes when every one of them has merged or closed. Khipu reads
              their state with <code>gh</code>, and only reads. Default: on.
            </p>
            {githubError ? (
              <p className="set-error" role="alert">
                {githubError}
              </p>
            ) : null}
          </div>
        </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// When the hub is away, waits: three waits, in whole seconds.
// ---------------------------------------------------------------------------

const OFFLINE_ROWS: ReadonlyArray<readonly [string, string]> = [
  ["hub_statement_timeout", "Wait on the hub for a read or write (seconds)"],
  ["hub_dead_connection_after", "Give up on a silent hub connection after (seconds)"],
  ["app_cli_timeout", "Wait for an answer in this app (seconds)"],
];

export function OfflineWaitsCard({ active }: { active: boolean }) {
  const cfg = useCliState<ConfigShape>("khipu_config_show", active);
  const rows = cfg.data?.offline ?? {};
  return (
    <>
      <div className="section-head">Waiting on the hub</div>
      <div className="section-body">
        <p className="set-help">
          How long Khipu waits before it uses this Mac's copy of Owed and Decisions. 0 means no limit.
        </p>
        {cfg.error && !cfg.data ? (
          <p className="set-error" role="alert">
            Could not read the settings: {cfg.error}
          </p>
        ) : null}
        {OFFLINE_ROWS.map(([key, label]) => {
          const row = rows[key];
          return (
            <SettingField
              key={key}
              label={label}
              mono
              narrow
              inputMode="decimal"
              value={row ? String(row.value) : ""}
              canReset={row?.source === "file"}
              onReset={() => unsetConfig(key, cfg.reload)}
              help={row ? `${row.words}. Default ${row.default}.` : undefined}
              onSave={async (v) => {
                const t = v.trim();
                if (!/^\d+$/.test(t)) return "Enter a whole number of seconds, 0 or more.";
                return writeConfig(key, t, cfg.reload);
              }}
            />
          );
        })}
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Searches on the hub: the read role's limit and two timeouts (whole numbers).
// Saving one changes the role on the hub itself, for every Mac.
// ---------------------------------------------------------------------------

const READ_ROLE_ROWS: ReadonlyArray<readonly [string, string, string]> = [
  ["read_role_connection_limit", "Most hub connections searches may hold", "connections"],
  ["read_role_statement_timeout", "Longest one search may run on the hub (seconds)", "seconds"],
  ["read_role_idle_in_transaction_timeout", "Close a search connection left idle after (seconds)", "seconds"],
];

export function SearchRoleCard({ active }: { active: boolean }) {
  const cfg = useCliState<ConfigShape>("khipu_config_show", active);
  const rows = cfg.data?.read_role ?? {};
  return (
    <>
      <div className="section-head">Searches on the hub</div>
      <div className="section-body">
        <p className="set-help">
          Searches use their own database role, so they can never take the connections saves need. These limits
          apply on the hub for every Mac and the gateway. 0 means no limit.
        </p>
        {cfg.error && !cfg.data ? (
          <p className="set-error" role="alert">
            Could not read the settings: {cfg.error}
          </p>
        ) : null}
        {READ_ROLE_ROWS.map(([key, label, unit]) => {
          const row = rows[key];
          return (
            <SettingField
              key={key}
              label={label}
              mono
              narrow
              inputMode="decimal"
              value={row ? String(row.value) : ""}
              canReset={row?.source === "file"}
              onReset={() => unsetConfig(key, cfg.reload)}
              help={row ? `${row.words}. Default ${row.default}.` : undefined}
              onSave={async (v) => {
                const t = v.trim();
                if (!/^\d+$/.test(t)) return `Enter a whole number of ${unit}, 0 or more.`;
                return writeConfig(key, t, cfg.reload);
              }}
            />
          );
        })}
      </div>
    </>
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
// Data: where the graph backup's off-site copy goes.
// ---------------------------------------------------------------------------

/** Same shape the Tauri command and the CLI enforce: `remote:path`, one token,
 *  the remote name first. This only saves a round trip. */
export function backupRemoteError(raw: string): string | null {
  const v = raw.trim().replace(/\/+$/, "");
  const i = v.indexOf(":");
  const remote = i > 0 ? v.slice(0, i) : "";
  const path = i > 0 ? v.slice(i + 1) : "";
  if (!remote || !path || !/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(remote) || /\s/.test(v)) {
    return "Enter an rclone remote and folder, such as r2:my-bucket/khipu-graph.";
  }
  return null;
}

const BACKUP_SOURCE_WORDS: Record<string, string> = {
  env: "the environment variable KHIPU_GRAPH_BACKUP_REMOTE",
  config: "this Mac's settings",
  default: "the default",
};

export function GraphBackupRemoteCard({ active }: { active: boolean }) {
  const cfg = useCliState<ConfigShape>("khipu_config_show", active);
  const row = cfg.data?.graph_backup_remote;
  const source = row?.source ?? "default";
  return (
    <div className="section-card">
      <div className="section-head">Graph backup destination</div>
      <div className="section-body">
        <p className="set-help">
          On the Mac that builds the graph, a copy of the latest graph snapshot goes here about once a week, through
          rclone. The remote must be named r2.
        </p>
        {cfg.error && !cfg.data ? (
          <p className="set-error" role="alert">
            Could not read the settings: {cfg.error}
          </p>
        ) : null}
        <SettingField
          label="Off-site copy goes to"
          mono
          value={row?.value ?? ""}
          placeholder={row?.default ?? "r2:khipu-backups/khipu-graph"}
          lockedBy={source === "env" ? "KHIPU_GRAPH_BACKUP_REMOTE" : null}
          canReset={source === "config"}
          resetLabel="Use the default"
          onReset={() => unsetConfig("graph_backup_remote", cfg.reload)}
          help={
            row
              ? `Comes from ${BACKUP_SOURCE_WORDS[source] ?? source}.${row.error ? ` Not usable: ${row.error}` : ""}`
              : "Reading the setting…"
          }
          onSave={async (v) => backupRemoteError(v) ?? writeConfig("graph_backup_remote", v.trim(), cfg.reload)}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Data: how current this Mac's copy of the hub is kept.
// ---------------------------------------------------------------------------

/** Minutes the field shows for a CLI value in seconds. */
function minutesText(seconds: number): string {
  return String(Math.round((seconds / 60) * 100) / 100);
}

/** Whole minutes only, at least `min`; the CLI re-checks in seconds. */
export function replicaMinutesError(raw: string, min: number): string | null {
  const v = raw.trim();
  if (!/^\d{1,7}$/.test(v) || Number(v) < min) {
    return min > 0 ? `Enter a whole number of minutes, ${min} or more.` : "Enter a whole number of minutes, 0 or more.";
  }
  return null;
}

const REPLICA_ROWS: ReadonlyArray<readonly [string, string, string, number]> = [
  [
    "replica_refresh_after",
    "Bring the copy up to date after (minutes)",
    "While the database answers, a background job brings this Mac's copy up to date when its last update is this old. 0 updates it at every check. Default 15.",
    0,
  ],
  [
    "replica_check_every",
    "Check every (minutes)",
    "How often the background job looks. At least 1. Default 5.",
    1,
  ],
];

export function ReplicaCopyCard({ active }: { active: boolean }) {
  const cfg = useCliState<ConfigShape>("khipu_config_show", active);
  return (
    <>
      <div className="section-head">This Mac's copy</div>
      <div className="section-body">
        {cfg.error && !cfg.data ? (
          <p className="set-error" role="alert">
            Could not read the settings: {cfg.error}
          </p>
        ) : null}
        {REPLICA_ROWS.map(([key, label, help, min]) => {
          const row = cfg.data?.replica?.[key];
          return (
            <SettingField
              key={key}
              label={label}
              help={help}
              narrow
              inputMode="decimal"
              value={row ? minutesText(row.value) : ""}
              canReset={row?.source === "file"}
              onReset={() => unsetConfig(key, cfg.reload)}
              onSave={async (v) =>
                replicaMinutesError(v, min) ?? writeConfig(key, String(Number(v.trim()) * 60), cfg.reload)
              }
            />
          );
        })}
        <SettingField
          label="Skip the database after a failed connection for (seconds)"
          help="After a connection to the database fails, recall and search use this Mac's copy without trying again for this long. 0 always tries. Default 60."
          narrow
          inputMode="decimal"
          value={cfg.data?.replica?.hub_down_memo_for ? String(cfg.data.replica.hub_down_memo_for.value) : ""}
          canReset={cfg.data?.replica?.hub_down_memo_for?.source === "file"}
          onReset={() => unsetConfig("hub_down_memo_for", cfg.reload)}
          onSave={async (v) =>
            /^\d{1,7}$/.test(v.trim())
              ? writeConfig("hub_down_memo_for", String(Number(v.trim())), cfg.reload)
              : "Enter a whole number of seconds, 0 or more."
          }
        />
      </div>
    </>
  );
}

/** Settings -> Data: every offline setting in one card. The copy group says how
 *  current this Mac's copy is kept and how long a failed connection is
 *  remembered; the waits group says how long a hub call may take before the
 *  copy answers instead. */
export function HubAwayCard({ active }: { active: boolean }) {
  return (
    <section className="section-card" aria-label="When the hub is away">
      <div className="section-head">When the hub is away</div>
      <div className="section-body">
        <p className="set-help">
          Khipu answers from this Mac's copy of your memory whenever the hub is out of reach, and catches up by
          itself when it answers again.
        </p>
      </div>
      <ReplicaCopyCard active={active} />
      <OfflineWaitsCard active={active} />
      <SearchRoleCard active={active} />
    </section>
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
    "replica_refresh",
    "This Mac's copy",
    "Keeps this Mac's copy of your memory current between nightly runs, so recall stays fast and works offline.",
  ],
  [
    "recall_daemon",
    "Recall service",
    "Keeps a small process running so recall answers each prompt faster. It has its own switch under Optional features.",
  ],
];

function jobWhen(iso: string | null | undefined): string {
  if (!iso) return "no run on record";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "no run on record";
  const seconds = (Date.now() - t) / 1000;
  if (seconds < 3600) return "ran within the hour";
  return `ran ${fmtAgo(seconds)}`;
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
