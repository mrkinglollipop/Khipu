/* Plan usage in the app (docs/plans/2026-10-14-plan-usage.md, phase 2): how
   much of each subscription CLI's plan is left, read from `khipu usage
   --json` through two fixed-argv commands (`khipu_usage_status`, cache first;
   `khipu_usage_refresh`, probes now). Khipu only reports these figures; it
   never moves work between plans. The shape mirrors `khipu/usage.py`
   (`report()`); a window a CLI does not report is absent, never zero.

   Figures can be old on purpose (Muse's last turn, Codex's rollout files, the
   last good read kept after a failed probe). A window whose reset time has
   passed has started again, so its old percent no longer applies: it never
   raises the alert, turns the health row red or colours a bar. Every reset
   time is the viewer's own local time, the same on the card, the callout and
   the health report. */

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Loader2, RefreshCw } from "lucide-react";
import { EmptyState, ListRow, Tag } from "./ui";
import { callCli, useCliState } from "./settingsCli";
import { fmtAgeSince } from "./time";
import type { Attention } from "./doctorAttention";

export type UsageWindow = {
  name?: string;
  used_pct?: number | null;
  resets_at?: string | null;
  window_mins?: number;
  model?: string;
  slot?: string;
};

export type UsageAccount = {
  harness?: string;
  account?: string;
  home?: string | null;
  plan?: string | null;
  windows?: UsageWindow[];
  credits?: Record<string, unknown> | null;
  version?: string | null;
  source?: string | null;
  observed_at?: string | null;
  ok?: boolean;
  error?: string | null;
  note?: string | null;
  probed_at?: string | null;
  ms?: number | null;
  last_attempt_at?: string | null;
  last_error?: string | null;
  last_good_at?: string | null;
  /** Codex's live read failed and the figures came from its last turn: the
   *  fixed code of that failure (the note says it in plain words). */
  live_error?: string | null;
  cached?: boolean;
  refreshing?: boolean;
  stale?: boolean;
};

export type UsageReport = {
  generated_at?: string;
  max_age_s?: number;
  refreshing?: boolean;
  accounts?: UsageAccount[];
  summary_text?: string[];
};

export const PLAN_WARN_PCT = 75;
export const PLAN_ALERT_PCT = 90;

/** The fixed error codes `khipu/usage.py` reports, in plain words. A CLI's
 *  own text never reaches the row; the row's `note` is Khipu's own words. */
const ERROR_PHRASE: Record<string, string> = {
  timeout: "didn't answer in time",
  not_signed_in: "not signed in",
  not_installed: "not installed",
  bad_reply: "unreadable reply",
  no_plan_limits: "no plan limits reported",
  invalid_account: "not a valid account entry",
  no_answer: "stopped without answering",
  start_failed: "could not be started",
  cli_error: "reported an error",
  internal_error: "Khipu could not run the check",
  pending: "first read still running",
};

export function usageErrorPhrase(code: string | null | undefined): string {
  if (code && ERROR_PHRASE[code]) return ERROR_PHRASE[code];
  if (code && /^(exit|signal)_\d+$/.test(code)) return ERROR_PHRASE.no_answer;
  return "couldn't read";
}

/** What the owner sees when `khipu usage` itself fails; the CLI's own text
 *  goes to the console only. */
export const PLAN_READ_FAILED = "The usage check did not run. Try again in a minute.";
const PLAN_READ_UNREADABLE = "The usage check gave an answer Khipu could not read.";

/** One decimal place: the figure shown and the figure the 75% and 90% lines
 *  are judged on are the same number. */
export function roundPct(n: number): number {
  return Math.round(n * 10) / 10;
}

function pctOf(w: UsageWindow): number | null {
  const v = w.used_pct;
  return typeof v === "number" && Number.isFinite(v) ? roundPct(v) : null;
}

export function fmtPct(n: number): string {
  return `${roundPct(n)}%`;
}

function parseIso(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** True when the window's reset time has passed: its percent was counted
 *  before the reset and no longer applies. */
export function hasReset(w: UsageWindow, now: Date = new Date()): boolean {
  const d = parseIso(w.resets_at);
  return d != null && d.getTime() <= now.getTime();
}

/** Windows with a figure that still applies (reset not yet passed). */
export function liveWindows(row: UsageAccount, now: Date = new Date()): UsageWindow[] {
  return (row.windows ?? []).filter((w) => pctOf(w) != null && !hasReset(w, now));
}

/** The window closest to its limit among those still in force, or null. */
export function tightestWindow(row: UsageAccount, now: Date = new Date()): UsageWindow | null {
  let best: UsageWindow | null = null;
  for (const w of liveWindows(row, now)) {
    if (best == null || (pctOf(w) ?? -1) > (pctOf(best) ?? -1)) best = w;
  }
  return best;
}

/** The tightest window across every account. */
export function tightestOverall(
  report: UsageReport | null | undefined,
  now: Date = new Date(),
): { row: UsageAccount; window: UsageWindow } | null {
  let out: { row: UsageAccount; window: UsageWindow } | null = null;
  for (const row of report?.accounts ?? []) {
    const w = tightestWindow(row, now);
    if (w && (out == null || (pctOf(w) ?? -1) > (pctOf(out.window) ?? -1))) out = { row, window: w };
  }
  return out;
}

/** "five-hour", "week", "day", "Fable week", "60 min". */
export function windowLabel(w: UsageWindow): string {
  const name = w.name || "window";
  if (name === "five_hour") return "five-hour";
  if (name === "weekly") return "week";
  if (name === "daily") return "day";
  if (name.startsWith("weekly_")) return `${w.model || name.slice("weekly_".length).replace(/_/g, " ")} week`;
  const mins = /^(\d+)_min$/.exec(name);
  if (mins) return `${mins[1]} min`;
  return name.replace(/_/g, " ");
}

/** "Codex Pro", "Claude main Max", "Grok SuperGrok Heavy". */
export function planName(row: UsageAccount): string {
  const label = row.account || row.harness || "Account";
  const plan = (row.plan || "").trim();
  return plan ? `${label} ${plan.charAt(0).toUpperCase()}${plan.slice(1)}` : label;
}

const HOUR_MS = 60 * 60 * 1000;
const WEEKDAY_ONLY_MS = 6 * 24 * HOUR_MS;

/** A reset in the viewer's own time zone: "Tue 9:57 PM" within the next six
 *  days, otherwise with its date ("Tue, Oct 13, 3:00 PM"), so a passed or a
 *  far reset never reads as the coming weekday. */
export function fmtResetLocal(iso: string | null | undefined, now: Date = new Date()): string | null {
  const d = parseIso(iso);
  if (!d) return null;
  const ahead = d.getTime() - now.getTime();
  if (ahead > 0 && ahead < WEEKDAY_ONLY_MS) {
    return d.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
  }
  return d.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

/** "resets in 40 min", "resets Tue 9:57 PM", or "reset since Mon, Oct 12,
 *  3:00 PM"; null when the CLI gave no reset time. */
export function resetPhrase(iso: string | null | undefined, now: Date = new Date()): string | null {
  const d = parseIso(iso);
  if (!d) return null;
  const ahead = d.getTime() - now.getTime();
  if (ahead <= 0) return `reset since ${fmtResetLocal(iso, now)}`;
  if (ahead < HOUR_MS) return `resets in ${Math.max(1, Math.round(ahead / 60000))} min`;
  return `resets ${fmtResetLocal(iso, now)}`;
}

export function meterTone(pct: number): "err" | "warn" | null {
  const p = roundPct(pct);
  if (p >= PLAN_ALERT_PCT) return "err";
  if (p >= PLAN_WARN_PCT) return "warn";
  return null;
}

/** Figures kept from an earlier read (a failed probe, an old observation). */
function isLastKnown(row: UsageAccount): boolean {
  return Boolean(row.stale) || row.ok === false;
}

/** "Codex Pro week at 94%, resets Wed 10:00 PM"; "... at 95% when last read"
 *  for kept figures. */
function windowSentence(row: UsageAccount, w: UsageWindow, now: Date): string {
  const reset = resetPhrase(w.resets_at, now);
  const last = isLastKnown(row) ? " when last read" : "";
  return `${planName(row)} ${windowLabel(w)} at ${fmtPct(pctOf(w) ?? 0)}${last}${reset ? `, ${reset}` : ""}`;
}

/** The tightest window still in force across every account; null when none. */
export function plansHealthDetail(report: UsageReport | null | undefined, now: Date = new Date()): string | null {
  const t = tightestOverall(report, now);
  return t ? windowSentence(t.row, t.window, now) : null;
}

/** Accounts that answered nothing usable: a failed probe with no figures
 *  kept. "Claude (not signed in)". */
export function unreadAccounts(report: UsageReport | null | undefined, now: Date = new Date()): string[] {
  return (report?.accounts ?? [])
    .filter((row) => row.ok === false && liveWindows(row, now).length === 0)
    .map((row) => `${row.account || row.harness || "Account"} (${usageErrorPhrase(row.error)})`);
}

/** Accounts whose figures came from a fallback because the live read failed
 *  (Codex signed out, say): "Codex is showing its last turn (not signed in)". */
export function liveReadFailures(report: UsageReport | null | undefined): string[] {
  return (report?.accounts ?? [])
    .filter((row) => row.ok !== false && row.live_error)
    .map((row) => `${row.account || row.harness || "Account"} is showing its last turn (${usageErrorPhrase(row.live_error)})`);
}

/** The tag for a row: why its read failed (figures kept from before), or why
 *  its live read failed (figures from a fallback); null when neither. */
function failureTag(row: UsageAccount): string | null {
  if (row.ok === false) return usageErrorPhrase(row.error);
  if (row.live_error) return usageErrorPhrase(row.live_error);
  return null;
}

/** One attention item when any window still in force is at or above 90%. */
export function planAttention(report: UsageReport | null | undefined, now: Date = new Date()): Attention[] {
  const hot: string[] = [];
  for (const row of report?.accounts ?? []) {
    for (const w of liveWindows(row, now)) {
      const p = pctOf(w);
      if (p == null || p < PLAN_ALERT_PCT) continue;
      hot.push(windowSentence(row, w, now));
    }
  }
  if (!hot.length) return [];
  return [
    {
      key: "plans",
      tone: "warn",
      title: hot.length === 1 ? "A plan is nearly used up" : `${hot.length} plan limits are nearly used up`,
      cause: `${hot.join("; ")}. Work sent to it may be refused until it resets.`,
    },
  ];
}

/** The earliest reset still ahead among windows at or above 90%: the moment
 *  the attention item next changes on its own. Null when there is none. */
export function nextAlertReset(report: UsageReport | null | undefined, now: Date = new Date()): number | null {
  let next: number | null = null;
  for (const row of report?.accounts ?? []) {
    for (const w of liveWindows(row, now)) {
      const p = pctOf(w);
      const d = parseIso(w.resets_at);
      if (p == null || p < PLAN_ALERT_PCT || !d) continue;
      if (next == null || d.getTime() < next) next = d.getTime();
    }
  }
  return next;
}

const MINUTE_MS = 60_000;
const MAX_TIMER_MS = 2_147_483_647;

/** `planAttention` kept current while a view stays open: judged afresh when
 *  new figures arrive, every minute while the item shows (so "resets in N
 *  min" counts down), and at the earliest reset among its windows (so a
 *  window that has reset drops out then, not at the next read). */
export function usePlanAttention(report: UsageReport | null | undefined): Attention[] {
  const [now, setNow] = useState(() => new Date());
  const [seen, setSeen] = useState(report);
  let at = now;
  if (seen !== report) {
    // New figures: judge them on the clock now, not the last tick's.
    at = new Date();
    setSeen(report);
    setNow(at);
  }
  const items = useMemo(() => planAttention(report, at), [report, at]);
  const showing = items.length > 0;
  const next = nextAlertReset(report, at);
  useEffect(() => {
    if (!showing) return;
    const tick = window.setInterval(() => setNow(new Date()), MINUTE_MS);
    return () => window.clearInterval(tick);
  }, [showing]);
  useEffect(() => {
    if (next == null) return;
    const wait = Math.min(MAX_TIMER_MS, Math.max(0, next - Date.now()) + 50);
    const timer = window.setTimeout(() => setNow(new Date()), wait);
    return () => window.clearTimeout(timer);
  }, [next]);
  return items;
}

function parseReport(data: unknown): UsageReport | null {
  if (!data || typeof data !== "object") return null;
  const accounts = (data as UsageReport).accounts;
  return Array.isArray(accounts) ? (data as UsageReport) : null;
}

type ReadKind = "status" | "refresh";

/** The report, read cache first whenever `active` changes to a truthy view
 *  ("home", "settings"); `refresh` probes every account now. One read at a
 *  time: a load asked for while any read runs gets that read's answer, and a
 *  refresh asked for while a status read runs follows it, so a cache read can
 *  never replace a fresh probe and no two reads start the CLIs twice. */
export function usePlanUsage(active: unknown) {
  const [data, setData] = useState<UsageReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const inflight = useRef<{ kind: ReadKind; promise: Promise<void> } | null>(null);
  const queuedRefresh = useRef<Promise<void> | null>(null);

  const read = useCallback((kind: ReadKind): Promise<void> => {
    const setBusy = kind === "refresh" ? setRefreshing : setLoading;
    setBusy(true);
    const entry: { kind: ReadKind; promise: Promise<void> } = { kind, promise: Promise.resolve() };
    entry.promise = (async () => {
      try {
        const res = await callCli<UsageReport>(kind === "refresh" ? "khipu_usage_refresh" : "khipu_usage_status");
        if (!res.ok) {
          console.error("khipu usage failed:", res.error);
          setError(PLAN_READ_FAILED);
          return;
        }
        const report = parseReport(res.data);
        if (report) {
          setData(report);
          setError(null);
        } else {
          setError(PLAN_READ_UNREADABLE);
        }
      } finally {
        if (inflight.current === entry) inflight.current = null;
        setBusy(false);
      }
    })();
    inflight.current = entry;
    return entry.promise;
  }, []);

  const load = useCallback((): Promise<void> => inflight.current?.promise ?? read("status"), [read]);

  /** A status read that starts after any read now running, for a change
   *  (the account list) that the running read cannot have seen. */
  const reread = useCallback(async (): Promise<void> => {
    while (inflight.current || queuedRefresh.current) {
      await (queuedRefresh.current ?? inflight.current?.promise);
    }
    return read("status");
  }, [read]);

  const refresh = useCallback((): Promise<void> => {
    const current = inflight.current;
    if (current?.kind === "refresh") return current.promise;
    if (!current) return read("refresh");
    if (!queuedRefresh.current) {
      setRefreshing(true);
      queuedRefresh.current = current.promise.then(() => {
        queuedRefresh.current = null;
        return read("refresh");
      });
    }
    return queuedRefresh.current;
  }, [read]);

  useEffect(() => {
    if (active) void load();
  }, [active, load]);

  return { data, error, loading, refreshing, load, refresh, reread };
}

export type PlanUsageState = ReturnType<typeof usePlanUsage>;

function Meter({ pct, label, tone }: { pct: number; label: string; tone: "err" | "warn" | null }) {
  const width = Math.min(100, Math.max(0, pct));
  return (
    <span
      className={tone ? `plan-meter ${tone}` : "plan-meter"}
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={width}
      aria-valuetext={`${fmtPct(pct)} used`}
    >
      <i style={{ width: `${width}%` }} />
    </span>
  );
}

/** When the figures were read: Muse's last turn, the last good probe kept
 *  after a failure, or this read. */
function figuresAt(row: UsageAccount): string | null {
  return row.observed_at || row.last_good_at || row.probed_at || null;
}

function StatusTags({ row }: { row: UsageAccount }) {
  const age = fmtAgeSince(figuresAt(row));
  if (row.observed_at) {
    return <Tag tone={row.stale ? "warn" : "neutral"}>{age ? `as of the last turn, ${age} ago` : "as of the last turn"}</Tag>;
  }
  if (row.stale) return <Tag tone="warn">{age ? `figures from ${age} ago` : "stale"}</Tag>;
  return null;
}

function RefreshButton({ busy, label, onClick }: { busy: boolean; label: string; onClick: () => void }) {
  return (
    <button type="button" className="sm" disabled={busy} aria-busy={busy || undefined} onClick={onClick}>
      {busy ? (
        <Loader2 size={14} className="spin" aria-hidden />
      ) : (
        <RefreshCw size={14} strokeWidth={1.75} aria-hidden />
      )}
      {label}
    </button>
  );
}

const NO_ACCOUNTS_HINT =
  "No claude, codex, grok or muse command was found on this Mac, and no account is listed under Settings → Capture & models → Plans.";
const ALL_RESET = "every limit has reset since it was read";

/** Figures exist, but every window has reset since they were read. */
export function onlyResetWindows(row: UsageAccount, now: Date): boolean {
  return liveWindows(row, now).length === 0 && (row.windows ?? []).some((w) => pctOf(w) != null);
}

/** One account on Home: its tightest window still in force as a bar, or why
 *  there is none. */
function PlanRow({ row, now }: { row: UsageAccount; now: Date }) {
  const w = tightestWindow(row, now);
  const p = w ? pctOf(w) : null;
  const reset = w ? resetPhrase(w.resets_at, now) : null;
  const allReset = onlyResetWindows(row, now);
  return (
    <ListRow className="plan-row">
      <span className="plan-name ellip">
        {row.account || row.harness}
        {row.plan ? <span className="meta"> {row.plan}</span> : null}
      </span>
      {w && p != null ? (
        <>
          <Meter pct={p} label={`${planName(row)} ${windowLabel(w)}`} tone={meterTone(p)} />
          <span className={`plan-pct ${meterTone(p) ?? ""}`}>{fmtPct(p)}</span>
          <span className="plan-when meta">
            {windowLabel(w)}
            {reset ? ` · ${reset}` : ""}
          </span>
        </>
      ) : (
        <span className="plan-none t2">
          {allReset ? ALL_RESET : row.ok === false ? usageErrorPhrase(row.error) : row.note || "no limits reported"}
        </span>
      )}
      <span className="plan-tags">
        {row.ok === false && (w || allReset) ? <Tag tone="warn">{usageErrorPhrase(row.error)}</Tag> : null}
        {row.ok !== false && row.live_error ? <Tag tone="warn">{usageErrorPhrase(row.live_error)}</Tag> : null}
        <StatusTags row={row} />
      </span>
    </ListRow>
  );
}

/** Home → Plans: one row per account, the tightest window of each. */
export function PlansCard({ state }: { state: PlanUsageState }) {
  const { data, error, loading, refreshing, refresh } = state;
  const accounts = data?.accounts ?? [];
  const now = new Date();
  return (
    <div className="card plans-card">
      <div className="card-head">
        Plans
        <span className="spacer" />
        <RefreshButton busy={refreshing || loading} label="Refresh" onClick={() => void refresh()} />
      </div>
      {data == null ? (
        error ? (
          <EmptyState title="Could not read plan usage" hint={error} />
        ) : (
          <EmptyState
            title={loading ? "Reading plan usage" : "Plan usage not read yet"}
            hint="Asks each subscription CLI how much of its plan is left; no model call is made."
          />
        )
      ) : accounts.length === 0 ? (
        <EmptyState title="No plans to show" hint={NO_ACCOUNTS_HINT} />
      ) : (
        <>
          {accounts.map((row, i) => (
            <PlanRow key={`${row.harness}|${row.home ?? ""}|${i}`} row={row} now={now} />
          ))}
          {error ? (
            <ListRow>
              <span className="meta">The last read failed; these figures are from the read before it.</span>
            </ListRow>
          ) : null}
        </>
      )}
    </div>
  );
}

/** "figures from 2 days ago · last tried 30s ago" for kept figures, "figures
 *  from 30s ago" for a fresh read, "last tried 30s ago" with no figures. */
function readWhen(row: UsageAccount, hasFigures: boolean): string {
  const tried = row.last_attempt_at || row.probed_at || null;
  const triedAge = fmtAgeSince(tried);
  if (!hasFigures) return triedAge ? `last tried ${triedAge} ago` : "";
  const figures = figuresAt(row);
  const figuresAge = fmtAgeSince(figures);
  const parts = figuresAge ? [`figures from ${figuresAge} ago`] : [];
  const gap = figures && tried ? Math.abs(new Date(tried).getTime() - new Date(figures).getTime()) : 0;
  if (triedAge && (row.ok === false || gap > 60_000)) parts.push(`last tried ${triedAge} ago`);
  return parts.join(" · ");
}

/** Settings → Capture & models → Plans: every account, every window, its
 *  reset and when its figures were read, Probe now, and which accounts are
 *  read (`usage_accounts`). Shares Home's reads (`state`). */
export function PlansSettingsCard({ state, active }: { state: PlanUsageState; active: boolean }) {
  const st = state;
  const accounts = st.data?.accounts ?? [];
  const now = new Date();
  return (
    <div className="section-card">
      <div className="section-head">Plans</div>
      <div className="section-body">
        {st.error ? (
          <p className="set-error" role="alert">
            {st.error}
          </p>
        ) : null}
        {st.loading && st.data == null ? (
          <p className="set-help" role="status">
            Reading plan usage…
          </p>
        ) : null}
        {st.data && accounts.length === 0 ? <p className="set-help">{NO_ACCOUNTS_HINT}</p> : null}
        {accounts.map((row, i) => {
          const windows = (row.windows ?? []).filter((w) => pctOf(w) != null);
          const when = readWhen(row, windows.length > 0);
          return (
            <div key={`${row.harness}|${row.home ?? ""}|${i}`} className="rows plan-account" role="group" aria-label={row.account || row.harness}>
              <div className="row-item">
                <span className="row-main">
                  {row.account || row.harness}
                  {row.plan ? <span className="meta"> {row.plan}</span> : null}
                </span>
                {failureTag(row) ? <Tag tone="warn">{failureTag(row)}</Tag> : null}
                <StatusTags row={row} />
                <span className="row-meta">
                  {row.home || "default login"}
                  {when ? ` · ${when}` : ""}
                </span>
              </div>
              {windows.length === 0 ? (
                <div className="row-item">
                  <span className="row-main t2">
                    {row.note || (row.ok === false ? usageErrorPhrase(row.error) : "no limits reported")}
                  </span>
                </div>
              ) : (
                <>
                  {windows.map((w) => {
                    const p = pctOf(w) as number;
                    const reset = resetPhrase(w.resets_at, now);
                    const tone = hasReset(w, now) ? null : meterTone(p);
                    return (
                      <div key={w.name} className="row-item plan-window">
                        <span className="plan-wname">{windowLabel(w)}</span>
                        <Meter pct={p} label={`${planName(row)} ${windowLabel(w)}`} tone={tone} />
                        <span className={`plan-pct ${tone ?? ""}`}>{fmtPct(p)}</span>
                        <span className="row-meta">{reset ?? "no reset reported"}</span>
                      </div>
                    );
                  })}
                  {row.note ? (
                    <div className="row-item">
                      <span className="row-main t2">{row.note}</span>
                    </div>
                  ) : null}
                </>
              )}
            </div>
          );
        })}
        <div className="toolbar">
          <RefreshButton busy={st.refreshing || st.loading} label="Probe now" onClick={() => void st.refresh()} />
          <span className="muted">Asks every CLI again now; takes up to about seven seconds.</span>
        </div>
        <UsageAccountsField active={active} onChanged={st.reread} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Which accounts are read: `config --set usage_accounts JSON` / `--unset`.
// ---------------------------------------------------------------------------

export type UsageAccountEntry = { label: string; harness: string; home?: string | null; invalid?: string };
type UsageAccountsStatus = {
  value: UsageAccountEntry[] | null;
  source: "file" | "default";
  accounts: UsageAccountEntry[];
  /** The owner's own folder, so "~/.x" and "/Users/me/.x" compare as one login. */
  user_home?: string | null;
};

export const USAGE_HARNESS_OPTIONS: Array<[string, string]> = [
  ["claude", "Claude Code"],
  ["codex", "Codex"],
  ["grok", "Grok Build"],
  ["muse", "Muse"],
];

function harnessName(h: string): string {
  return USAGE_HARNESS_OPTIONS.find(([v]) => v === h)?.[1] ?? h;
}

const LABEL_MAX = 64;
const FOLDER_MAX = 1024;

/** The name an account gets when the Name field is left empty: the CLI's
 *  name, with the login folder when there is one ("Codex (~/.codex-work)"),
 *  so a second login never takes the name of the default one. At most 64
 *  characters, as `khipu config` stores. */
export function defaultAccountLabel(harness: string, home: string | null | undefined): string {
  const name = harnessName(harness);
  const folder = (home ?? "").trim();
  if (!folder) return name;
  const room = LABEL_MAX - name.length - 3;
  return `${name} (${folder.length > room ? `…${folder.slice(folder.length - room + 1)}` : folder})`;
}

/** A login folder for comparing two entries, the way `khipu config` does:
 *  "~" expanded to the owner's folder when it is known, repeated slashes,
 *  "/." and a trailing slash dropped. "" is the default login. */
export function loginKey(home: string | null | undefined, userHome?: string | null): string {
  let f = (home ?? "").trim();
  if (!f) return "";
  if (userHome && (f === "~" || f.startsWith("~/"))) f = `${userHome.replace(/\/+$/, "")}${f.slice(1)}`;
  const key = f
    .replace(/\/{2,}/g, "/")
    .replace(/(?:\/\.)+(?=\/|$)/g, "")
    .replace(/\/+$/, "");
  return key || "/";
}

export type FormProblem = { field: "name" | "home" | null; message: string };

// eslint-disable-next-line no-control-regex
const CONTROL = /[\u0000-\u001f\u007f]/;

/** Why the Name or Login folder as typed cannot be saved, or null: the checks
 *  `khipu config` and the app's own command make, said in plain words before
 *  anything is sent. A folder starts with "~/" or "/": "~.codex-work" (the
 *  slash forgotten) names no folder this Mac has. */
export function formProblem(entry: UsageAccountEntry): FormProblem | null {
  const label = entry.label.trim();
  if (CONTROL.test(label)) return { field: "name", message: "The Name must be one line of plain text." };
  if (label.length > LABEL_MAX) {
    return { field: "name", message: `That Name is too long: use ${LABEL_MAX} characters or fewer.` };
  }
  const folder = (entry.home ?? "").trim();
  if (!folder) return null;
  if (CONTROL.test(folder)) return { field: "home", message: "The Login folder must be one line of plain text." };
  if (folder.length > FOLDER_MAX) return { field: "home", message: "That Login folder is too long." };
  if (!(folder === "~" || folder.startsWith("~/") || folder.startsWith("/"))) {
    return {
      field: "home",
      message: "Start the Login folder with ~/ (for example ~/.codex-work), or give the full path starting with /.",
    };
  }
  return null;
}

/** Why `entry` cannot be added beside `entries`, in plain words, or null.
 *  The same two rules `khipu config` applies (each login once, each name
 *  once, a login compared after "~" is expanded), checked here so the owner
 *  is pointed at the field to change. */
export function accountClash(
  entries: UsageAccountEntry[],
  entry: UsageAccountEntry,
  userHome?: string | null,
): FormProblem | null {
  const valid = entries.filter((e) => !e.invalid);
  const key = loginKey(entry.home, userHome);
  const login = valid.find((e) => e.harness === entry.harness && loginKey(e.home, userHome) === key);
  if (login && entry.harness === "muse") {
    return { field: "name", message: `Muse is already listed as ${login.label}. Muse has one login, so it is read once.` };
  }
  if (login) {
    const where = key ? `the login folder ${entry.home}` : "its default login";
    return {
      field: "home",
      message: `${harnessName(entry.harness)} with ${where} is already listed as ${login.label}. Choose a different Login folder.`,
    };
  }
  const name = entry.label.trim().toLowerCase();
  const named = valid.find((e) => e.label.trim().toLowerCase() === name);
  if (named) {
    return { field: "name", message: `An account is already named ${named.label}. Type a different Name.` };
  }
  return null;
}

type StoredEntry = { label: string; harness: string; home?: string };

/** The list to store: the valid entries shown, as `{label, harness, home?}`. */
function storable(entries: UsageAccountEntry[]): StoredEntry[] {
  return entries
    .filter((e) => !e.invalid)
    .map((e) => (e.home ? { label: e.label, harness: e.harness, home: e.home } : { label: e.label, harness: e.harness }));
}

/** What a refusal is about: the entry at fault (null when the list as a
 *  whole is), the earlier entry it repeats, and whether it is the entry
 *  being added from the form (then the owner is pointed at a field). */
type RefusalCtx = { entry: StoredEntry | null; other: StoredEntry | null; field: string | null; added: boolean };

function fieldWords(c: RefusalCtx): string {
  const which = c.field === "label" ? "Name" : "Login folder";
  return c.added || !c.entry ? `The ${which}` : `The ${which} of ${c.entry.label}`;
}

const ACCOUNTS_NOT_SAVED = "Khipu could not save the account list. Check the Name and Login folder and try again.";

/** Each refusal code `khipu config --set usage_accounts` gives, as Khipu's
 *  own sentence. The CLI's text never reaches the owner: it names the
 *  setting, counts entries the owner never added and says "home". */
const ACCOUNT_REFUSAL: Record<string, (c: RefusalCtx) => string> = {
  bad_list: () => ACCOUNTS_NOT_SAVED,
  bad_entry: () => ACCOUNTS_NOT_SAVED,
  too_many: () => "Khipu reads at most 32 accounts. Remove one before adding another.",
  not_one_line: (c) => `${fieldWords(c)} must be one line of plain text.`,
  too_long: (c) =>
    c.field === "label"
      ? `${fieldWords(c)} is too long: use ${LABEL_MAX} characters or fewer.`
      : `${fieldWords(c)} is too long.`,
  unknown_cli: () => "Khipu reads Claude Code, Codex, Grok Build and Muse. Choose one of them under CLI.",
  muse_folder: () => "Muse has one sign-in, so leave the Login folder empty.",
  no_such_folder: (c) =>
    `Khipu cannot find the login folder ${c.entry?.home ?? ""}${c.added || !c.entry ? "" : ` of ${c.entry.label}`}. ` +
    "Start it with ~/ (for example ~/.codex-work), or give the full path starting with /.",
  name_taken: (c) =>
    c.added
      ? `An account is already named ${c.other?.label ?? c.entry?.label ?? ""}. Type a different Name.`
      : `Two accounts are named ${c.entry?.label ?? ""}. Remove one of them.`,
  login_taken: (c) => {
    if (!c.added) {
      return `${c.entry?.label ?? "An account"} and ${c.other?.label ?? "another account"} read the same sign-in. Remove one of them.`;
    }
    const where = c.entry?.home ? `the login folder ${c.entry.home}` : "its default login";
    const other = c.other?.label ?? "an account already listed";
    return `${harnessName(c.entry?.harness ?? "")} with ${where} reads the same sign-in as ${other}. Choose a different Login folder.`;
  },
};

/** A failed save in plain words, never the text the CLI or the app's own
 *  command wrote (that goes to the console): `sent` is the list sent and
 *  `added` the index of the entry typed in the form, if any. */
export function accountsRefusal(
  res: { error: string; refusal?: Record<string, unknown> },
  sent: StoredEntry[],
  added: number | null,
): FormProblem {
  console.error("khipu config --set usage_accounts refused:", res.error);
  if (/^(khipu exited|spawn khipu)/i.test(res.error)) {
    return { field: null, message: "Khipu could not save the account list. Try again in a minute." };
  }
  const r = res.refusal ?? {};
  const code = typeof r.code === "string" ? r.code : "";
  const index = typeof r.index === "number" ? r.index : null;
  const otherIndex = typeof r.other === "number" ? r.other : null;
  const field = typeof r.field === "string" ? r.field : null;
  const say = Object.prototype.hasOwnProperty.call(ACCOUNT_REFUSAL, code) ? ACCOUNT_REFUSAL[code] : null;
  if (!say) return { field: null, message: ACCOUNTS_NOT_SAVED };
  const ctx: RefusalCtx = {
    entry: index != null ? (sent[index] ?? null) : null,
    other: otherIndex != null ? (sent[otherIndex] ?? null) : null,
    field,
    added: index != null && index === added,
  };
  const at = !ctx.added ? null : field === "label" ? "name" : field === "home" ? "home" : null;
  return { field: at, message: say(ctx) };
}

export function UsageAccountsField({ active, onChanged }: { active: boolean; onChanged?: () => Promise<unknown> | void }) {
  const cfg = useCliState<{ usage_accounts?: UsageAccountsStatus }>("khipu_config_show", active);
  const status = cfg.data?.usage_accounts;
  const entries = status?.accounts ?? [];
  const [label, setLabel] = useState("");
  const [harness, setHarness] = useState("claude");
  const [home, setHome] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [clash, setClash] = useState<"name" | "home" | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const homeRef = useRef<HTMLInputElement>(null);
  const id = useId();
  const hasInvalid = entries.some((e) => e.invalid);

  /** Stores the list; `onSaved` runs the moment the write succeeds. The
   *  re-read that follows probes a new account (seconds), so nothing here
   *  waits on it: the form is free again as soon as the list is saved. */
  async function save(
    next: UsageAccountEntry[] | null,
    onSaved?: () => void,
    added: number | null = null,
  ): Promise<boolean> {
    setSaving(true);
    setErr(null);
    setClash(null);
    const list = next == null ? [] : storable(next);
    const res = list.length
      ? await callCli("khipu_config_set", { key: "usage_accounts", value: JSON.stringify(list) })
      : await callCli("khipu_config_unset", { key: "usage_accounts" });
    if (res.ok) onSaved?.();
    await cfg.reload();
    setSaving(false);
    if (!res.ok) {
      const problem = accountsRefusal(res, list, added);
      setErr(problem.message);
      setClash(problem.field);
      if (problem.field) (problem.field === "name" ? nameRef : homeRef).current?.focus();
      return false;
    }
    void onChanged?.();
    return true;
  }

  async function add(e: FormEvent) {
    e.preventDefault();
    const folder = harness === "muse" ? null : home.trim() || null;
    const entry: UsageAccountEntry = {
      label: label.trim() || defaultAccountLabel(harness, folder),
      harness,
      home: folder,
    };
    const problem = formProblem(entry) ?? accountClash(entries, entry, status?.user_home);
    if (problem) {
      setErr(problem.message);
      setClash(problem.field);
      if (problem.field) (problem.field === "name" ? nameRef : homeRef).current?.focus();
      return;
    }
    const next = [...entries, entry];
    await save(
      next,
      () => {
        setLabel("");
        setHome("");
      },
      storable(next).length - 1,
    );
  }

  return (
    <div className="set-field">
      <div className="set-labelrow">
        <span className="set-label" id={`${id}-head`}>
          Accounts read
        </span>
        {status ? <Tag tone="neutral">{status.source === "file" ? "Your list" : "Default"}</Tag> : null}
      </div>
      <p className="set-help">
        {status?.source === "file"
          ? "Only the accounts below are read."
          : "Every installed CLI is read with its default login. Add an account to read a second login, such as another Claude or Grok sign-in."}
      </p>
      {cfg.error && !status ? (
        <p className="set-error" role="alert">
          Could not read the account list. Close and reopen this section to try again.
        </p>
      ) : null}
      {entries.length ? (
        <div className="rows" role="list" aria-labelledby={`${id}-head`}>
          {entries.map((e, i) => (
            <div key={`${e.harness}|${e.home ?? ""}|${i}`} className="row-item" role="listitem">
              <span className="row-main">
                {e.label}
                <span className="meta"> {harnessName(e.harness)}</span>
              </span>
              {e.invalid ? <Tag tone="warn">not valid: {e.invalid}</Tag> : null}
              <span className="row-meta">{e.home || "default login"}</span>
              <button
                type="button"
                className="sm"
                disabled={saving || !status}
                aria-label={`Remove ${e.label}`}
                onClick={() => void save(entries.filter((_, j) => j !== i))}
              >
                Remove
              </button>
            </div>
          ))}
        </div>
      ) : null}
      <form className="toolbar" onSubmit={(e) => void add(e)} aria-label="Add an account">
        <label className="muted" htmlFor={`${id}-label`}>
          Name
        </label>
        <input
          id={`${id}-label`}
          ref={nameRef}
          value={label}
          maxLength={LABEL_MAX}
          placeholder="Claude second…"
          autoComplete="off"
          spellCheck={false}
          aria-invalid={clash === "name" || undefined}
          aria-describedby={err ? `${id}-err` : undefined}
          onChange={(e) => {
            setLabel(e.target.value);
            if (clash) setClash(null);
          }}
        />
        <label className="muted" htmlFor={`${id}-harness`}>
          CLI
        </label>
        <select id={`${id}-harness`} value={harness} onChange={(e) => setHarness(e.target.value)}>
          {USAGE_HARNESS_OPTIONS.map(([v, name]) => (
            <option key={v} value={v}>
              {name}
            </option>
          ))}
        </select>
        <label className="muted" htmlFor={`${id}-home`}>
          Login folder
        </label>
        <input
          id={`${id}-home`}
          ref={homeRef}
          value={harness === "muse" ? "" : home}
          maxLength={FOLDER_MAX}
          placeholder={harness === "muse" ? "Muse has one login" : "~/.claude-second…"}
          disabled={harness === "muse"}
          autoComplete="off"
          spellCheck={false}
          aria-invalid={clash === "home" || undefined}
          aria-describedby={err ? `${id}-err` : undefined}
          onChange={(e) => {
            setHome(e.target.value);
            if (clash) setClash(null);
          }}
        />
        <button type="submit" className="sm" disabled={saving || !status}>
          {saving ? <Loader2 size={14} className="spin" aria-hidden /> : null}
          Add
        </button>
      </form>
      <p className="set-help">
        The login folder is where that CLI keeps a second sign-in; leave it empty for the default login. With no
        Name, the account is named after its CLI and login folder.
        {hasInvalid ? " Entries marked not valid are left out when you add or remove an account." : ""}
      </p>
      {status?.source === "file" ? (
        <div className="toolbar">
          <button type="button" className="sm" disabled={saving} onClick={() => void save(null)}>
            Go back to every installed CLI
          </button>
        </div>
      ) : null}
      {err ? (
        <p className="set-error" role="alert" id={`${id}-err`}>
          {err}
        </p>
      ) : null}
    </div>
  );
}
