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
import { ChevronRight, Loader2, RefreshCw } from "lucide-react";
import { EmptyState, ListRow, Tag } from "./ui";
import { callCli, useCliState } from "./settingsCli";
import { fmtAgoSince } from "./time";
import type { Attention } from "./doctorAttention";
import { accountState } from "./routing";
import type { RouteView } from "./routing";

export type UsageWindow = {
  name?: string;
  used_pct?: number | null;
  resets_at?: string | null;
  window_mins?: number;
  model?: string;
  slot?: string;
};

export type UsageAccount = {
  /** `harness|login folder`, the routing record's key for the account. */
  key?: string | null;
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
  bad_reply: "answered in a way Khipu can't read",
  no_plan_limits: "no plan limits reported",
  no_key: "no GLM key stored",
  key_rejected: "key not accepted by z.ai",
  redirect_refused: "quota endpoint tried to redirect to another host",
  no_plan: "no GLM Coding Plan on this key",
  no_plan_figures: "didn't send its plan figures",
  invalid_account: "not a valid account entry",
  no_answer: "didn't answer",
  start_failed: "could not be started",
  cli_error: "reported an error when asked",
  internal_error: "Khipu could not run the check",
  pending: "first read still running",
  not_configured: "not in the account list",
  ambiguous_account: "shares its name with another account",
  no_sign_in: "signs in from its own CLI",
  sign_in_timeout: "sign-in not finished in time",
  login_folder_missing: "login folder not found on this Mac",
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
  /** When the last read failed, for "Last read failed 2 min ago". */
  const [errorAt, setErrorAt] = useState<string | null>(null);
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
          setErrorAt(new Date().toISOString());
          return;
        }
        const report = parseReport(res.data);
        if (report) {
          setData(report);
          setError(null);
          setErrorAt(null);
        } else {
          setError(PLAN_READ_UNREADABLE);
          setErrorAt(new Date().toISOString());
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

  return { data, error, errorAt, loading, refreshing, load, refresh, reread };
}

export type PlanUsageState = ReturnType<typeof usePlanUsage>;

// ---------------------------------------------------------------------------
// Whether Khipu can read each account, and signing in again from the app.
// ---------------------------------------------------------------------------

/** Whether Khipu can read the account now, in plain words: "Live: read 2 min
 *  ago", "Signed in, but its plan figures can't be read", "Not signed in",
 *  "CLI not installed". A read that worked but brought no figures (Muse
 *  before its first turn) is never "Live": it says when it was checked, and
 *  `checked` marks it so nobody is asked to sign in. */
export function liveWords(row: UsageAccount): { text: string; live: boolean; checked?: boolean } {
  const code = row.ok === false ? row.error : row.live_error ?? null;
  if (code === "not_installed") return { text: "CLI not installed", live: false };
  if (code === "not_signed_in") return { text: "Not signed in", live: false };
  if (code === "no_plan_limits") return { text: "Signed in, but its plan figures can't be read", live: false };
  if (code === "pending") return { text: "Reading it for the first time", live: false };
  if (row.ok === false) return { text: `Can't be read just now (${usageErrorPhrase(code)})`, live: false };
  const age = fmtAgoSince(row.last_attempt_at || row.probed_at);
  if (!(row.windows ?? []).some((w) => pctOf(w) != null)) {
    return { text: age ? `Checked ${age}, no figures yet` : "Checked, no figures yet", live: false, checked: true };
  }
  return { text: age ? `Live: read ${age}` : "Live", live: true };
}

/** The CLIs whose own login the app can start (`khipu.usage.SIGN_IN_ARGS`):
 *  each opens the browser and asks nothing on a terminal. */
const SIGN_IN_CLIS = new Set(["claude", "codex"]);
const CLI_TITLE: Record<string, string> = { claude: "Claude Code", codex: "Codex", grok: "Grok Build", muse: "Muse", glm: "GLM Coding Plan" };

/** "button" when the app can start this account's own login, "elsewhere"
 *  when the owner signs in from the CLI itself, null when it is live or its
 *  CLI is not installed. */
export function signInOffer(row: UsageAccount): "button" | "elsewhere" | null {
  // Only a sign-in problem is mended by signing in: a timeout or an error
  // offers nothing (settings clarity, phase 2).
  const code = row.ok === false ? row.error : row.live_error ?? null;
  if (code !== "not_signed_in" && code !== "no_plan_limits") return null;
  return SIGN_IN_CLIS.has(row.harness ?? "") ? "button" : "elsewhere";
}

type SignInAnswer = { ok?: boolean; error?: string; row?: UsageAccount };

/** What the owner reads once a sign-in ends, from the account read just after. */
export function signInOutcome(res: { ok: boolean; data?: SignInAnswer; refusal?: Record<string, unknown>; error?: string }): string {
  const answer = (res.ok ? res.data : (res.refusal as SignInAnswer | undefined)) ?? {};
  const after = answer.row ? liveWords(answer.row).text : null;
  if (res.ok && answer.ok) return after ? `Signed in. ${after}.` : "Signed in.";
  if (answer.error === "sign_in_timeout") {
    return "Khipu stopped waiting after 15 minutes. Press Sign in again to start over.";
  }
  if (answer.error === "not_configured") return "That account is no longer in the list below, so Khipu didn't start a sign-in.";
  if (answer.error === "ambiguous_account") {
    return "Two accounts share that name, so Khipu didn't start a sign-in. Give each its own name in the list below.";
  }
  if (after) return `The sign-in didn't finish. ${after}.`;
  return `The sign-in didn't start: ${res.error ?? "Khipu couldn't run it"}.`;
}

/** For an account whose sign-in is the problem: Sign in again (Claude Code,
 *  Codex), or where to sign in (Grok Build). The button starts the account's
 *  own login in its own login folder; the browser opens; Khipu reads the
 *  account again when the login ends, and the card shows its new state. */
export function SignInControl({ row, onSignedIn }: { row: UsageAccount; onSignedIn?: () => Promise<unknown> | void }) {
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const offer = signInOffer(row);
  const name = row.account || row.harness || "this account";
  const signIn = async () => {
    if (busy) return;
    setBusy(true);
    setSaid(null);
    try {
      const res = await callCli<SignInAnswer>("khipu_account_sign_in", { harness: row.harness, home: row.home ?? null });
      setSaid(signInOutcome(res));
      await onSignedIn?.();
    } finally {
      setBusy(false);
    }
  };
  if (!offer && !busy && !said) return null;
  return (
    <>
      {offer === "button" ? (
        <button type="button" className="sm" disabled={busy} aria-busy={busy || undefined}
          aria-label={`Sign ${name} in again`} onClick={() => void signIn()}>
          {busy ? <Loader2 size={14} className="spin" aria-hidden /> : null}
          Sign in again
        </button>
      ) : offer === "elsewhere" ? (
        <span className="meta">Sign in from {CLI_TITLE[row.harness ?? ""] ?? "its CLI"}</span>
      ) : null}
      {busy ? (
        <span className="meta" role="status">
          Finish signing in in your browser; Khipu reads the account again when you're done.
        </span>
      ) : said ? (
        <span className="meta" role="status">
          {said}
        </span>
      ) : null}
    </>
  );
}

function Meter({ pct, label, tone, dim }: { pct: number; label: string; tone: "err" | "warn" | null; dim?: boolean }) {
  const width = Math.min(100, Math.max(0, pct));
  return (
    <span
      className={["plan-meter", tone, dim ? "dim" : null].filter(Boolean).join(" ")}
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

export function RefreshButton({ busy, label, onClick }: { busy: boolean; label: string; onClick: () => void }) {
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

const NO_ACCOUNTS_TITLE = "No accounts to read yet";
const NO_ACCOUNTS_HINT =
  "List your accounts in Settings → Accounts and routing and Khipu shows how much of each plan is left. It can read Claude Code, Codex, Grok Build, Muse and the GLM Coding Plan.";
const NO_ACCOUNTS_HINT_HERE =
  "List your accounts below and Khipu shows how much of each plan is left. It can read Claude Code, Codex, Grok Build, Muse and the GLM Coding Plan.";
export const MUSE_NO_TURN = "No figures yet. Muse reports its plan after its first turn.";
const NO_RESETTING_LIMIT = "Its plan shows no limit that resets.";

/** Figures exist, but every window has reset since they were read. */
export function onlyResetWindows(row: UsageAccount, now: Date): boolean {
  return liveWindows(row, now).length === 0 && (row.windows ?? []).some((w) => pctOf(w) != null);
}

/** The code behind an account's trouble: its failed read, or its failed live
 *  read when the figures came from a fallback (Codex's last turn). */
function troubleCode(row: UsageAccount): string | null {
  return row.ok === false ? row.error ?? null : row.live_error ?? null;
}

/** The one rule, for Home and Settings alike, on which failed reads need the
 *  owner: only they can fix a sign-in that is gone or reports no plan limits.
 *  Every other failure but `pending` is a passing gap (`no_plan_figures`, a
 *  timeout, a CLI that didn't answer): the last figures stay shown with their
 *  age and Khipu asks again. */
const SIGN_IN_CODES: ReadonlySet<string> = new Set(["not_signed_in", "no_plan_limits"]);

export function needsOwner(code: string | null | undefined): boolean {
  return !!code && SIGN_IN_CODES.has(code);
}

export function isPassingGap(code: string | null | undefined): boolean {
  return !!code && code !== "pending" && !needsOwner(code);
}

/** True while a passing gap still has figures in force no older than
 *  `staleAfterS` seconds (0: never out of date): the account shows them and
 *  counts as no problem. Older kept figures, or none, make it a problem. */
export function passingGapHeld(row: UsageAccount, staleAfterS: number, now: Date = new Date()): boolean {
  if (!isPassingGap(troubleCode(row)) || liveWindows(row, now).length === 0) return false;
  if (!(staleAfterS > 0)) return true;
  const at = parseIso(figuresAt(row));
  return at != null && now.getTime() - at.getTime() < staleAfterS * 1000;
}

/** The kept window an Accounts row shows for a passing gap: the tightest one
 *  still in force, as left and reset. */
export function keptWindow(row: UsageAccount, now: Date = new Date()): { left: number; label: string; resets_at?: string | null } | null {
  const w = tightestWindow(row, now);
  const p = w ? pctOf(w) : null;
  return w && p != null ? { left: roundPct(Math.max(0, 100 - p)), label: windowLabel(w), resets_at: w.resets_at } : null;
}

/** The failure's cause in Khipu's own words: the row's note (`usage.py`
 *  `_note()` gives one for every code), else the code's phrase after the CLI's
 *  name for a row cached before notes covered every code. */
function failureCause(row: UsageAccount, code: string): string {
  const note = row.ok === false ? (row.note ?? "").trim().replace(/\.$/, "") : "";
  if (note) return /^(This|The) /.test(note) ? note.charAt(0).toLowerCase() + note.slice(1) : note;
  if (code === "internal_error") return ERROR_PHRASE.internal_error;
  return `${CLI_TITLE[row.harness ?? ""] ?? "its CLI"} ${usageErrorPhrase(code)}`;
}

/** What one account's row says, once: whether its figures are from this read,
 *  kept from an earlier one, or absent, and the single status phrase (null
 *  when the figures speak for themselves). Settings clarity, phase 2. */
export type PlanStatus = {
  figures: "live" | "kept" | "none";
  say: string | null;
  tone: "warn" | "grey" | null;
  code: string | null;
};

export function planStatus(row: UsageAccount, now: Date = new Date()): PlanStatus {
  const code = troubleCode(row);
  const hasFigures = liveWindows(row, now).length > 0;
  const figures: PlanStatus["figures"] = !hasFigures ? "none" : code ? "kept" : "live";
  const age = fmtAgoSince(figuresAt(row));
  if (needsOwner(code)) {
    return { figures, say: code === "no_plan_limits" ? "Its sign-in reports no plan limits" : "Not signed in", tone: "warn", code };
  }
  if (code === "pending") return { figures, say: "Reading it for the first time", tone: "grey", code };
  if (code) {
    if (hasFigures) {
      const from = row.observed_at ? `figures from its last turn, ${age}` : `figures from ${age}`;
      return { figures, say: `Can't read it now; ${age ? from : "showing earlier figures"}`, tone: "warn", code };
    }
    if (onlyResetWindows(row, now)) {
      return { figures, say: "Can't read it now; every limit has reset since it was read", tone: "warn", code };
    }
    return { figures, say: `Can't read its plan: ${failureCause(row, code)}`, tone: "warn", code };
  }
  if (onlyResetWindows(row, now)) return { figures, say: "Every limit has reset since it was read", tone: "grey", code };
  if (!hasFigures) {
    if (row.harness === "muse") return { figures, say: MUSE_NO_TURN, tone: "grey", code };
    // A read that worked but found no figures says when it was checked.
    const checked = fmtAgoSince(row.last_attempt_at || row.probed_at);
    const say = checked ? `${NO_RESETTING_LIMIT.slice(0, -1)}; checked ${checked}` : NO_RESETTING_LIMIT;
    return { figures, say, tone: "grey", code };
  }
  if (row.observed_at) {
    return { figures, say: age ? `as of its last turn, ${age}` : "as of its last turn", tone: "grey", code };
  }
  if (row.stale && age) return { figures, say: `figures from ${age}`, tone: "grey", code };
  return { figures, say: null, tone: null, code };
}

/** "Paused by you until Wed, Oct 21, 9:26 PM" for each account the routing
 *  record says the owner paused, by its stable key (a name can be reused by
 *  another login). Empty when the record could not be read: Plans then shows
 *  no pause rather than guessing. */
export function planPauses(view: RouteView | null | undefined, now: Date = new Date()): Map<string, string> {
  const out = new Map<string, string>();
  for (const a of view?.accounts ?? []) {
    if (a.state === "paused") out.set(a.key, accountState(a, now).text);
  }
  return out;
}

function accountName(row: UsageAccount): string {
  return row.account || row.harness || "Account";
}

function StatusPhrase({ status }: { status: PlanStatus }) {
  if (!status.say) return null;
  return (
    <span className={status.tone === "grey" ? "plan-say grey" : "plan-say"}>
      {status.tone === "warn" ? <span className="hdot warn" aria-hidden="true" /> : null}
      {status.say}
    </span>
  );
}

/** One account on Home: its tightest window still in force as a bar, and at
 *  most one status phrase, after the pause when the owner paused it. */
function PlanRow({ row, now, pause, onSignedIn }: {
  row: UsageAccount; now: Date; pause?: string; onSignedIn?: () => Promise<unknown> | void;
}) {
  const status = planStatus(row, now);
  const w = tightestWindow(row, now);
  const p = w ? pctOf(w) : null;
  const reset = w ? resetPhrase(w.resets_at, now) : null;
  const dim = status.figures === "kept";
  const tail = (
    <>
      {pause ? <Tag>{pause}</Tag> : null}
      <StatusPhrase status={status} />
      <SignInControl row={row} onSignedIn={onSignedIn} />
    </>
  );
  return (
    <ListRow className="plan-row">
      <span className="plan-name ellip">
        {accountName(row)}
        {row.plan ? <span className="meta"> {row.plan}</span> : null}
      </span>
      {w && p != null ? (
        <>
          <Meter pct={p} label={`${planName(row)} ${windowLabel(w)}`} tone={meterTone(p)} dim={dim} />
          <span className={`plan-pct ${meterTone(p) ?? (dim ? "dim" : "")}`}>{fmtPct(p)}</span>
          <span className="plan-when meta">
            {windowLabel(w)}
            {reset ? ` · ${reset}` : ""}
          </span>
          <span className="plan-say-col">{tail}</span>
        </>
      ) : (
        <span className="plan-none">{tail}</span>
      )}
    </ListRow>
  );
}

/** Home → Plans: one row per account, the tightest window of each. `routing`
 *  is the routing record when it was read, for the pauses. */
export function PlansCard({ state, routing, onOpenAccounts, noneTracked = false, onAddAccount }: {
  state: PlanUsageState;
  routing?: RouteView | null;
  onOpenAccounts?: () => void;
  /** The owner stopped tracking every account ("Read no accounts", phase 9). */
  noneTracked?: boolean;
  onAddAccount?: () => void;
}) {
  const { data, error, errorAt, loading, refreshing, refresh, reread } = state;
  const accounts = data?.accounts ?? [];
  const now = new Date();
  const pauses = planPauses(routing, now);
  const failedAge = fmtAgoSince(errorAt);
  return (
    <div className="card plans-card">
      <div className="card-head">
        Plans
        <span className="spacer" />
        {data != null && error ? (
          <span className="plans-head-note">
            <span className="hdot warn" aria-hidden="true" />
            Last read failed {failedAge ?? "just now"}; showing earlier figures
          </span>
        ) : null}
        <RefreshButton busy={refreshing || loading} label="Refresh" onClick={() => void refresh()} />
      </div>
      {noneTracked ? (
        <div className="empty">
          <b>No accounts tracked</b>
          <span>You stopped tracking every account on this Mac. Add one to see how much of its plan is left.</span>
          {onAddAccount ? (
            <button type="button" className="sm plans-empty-btn" onClick={onAddAccount}>
              Add an account…
            </button>
          ) : null}
        </div>
      ) : data == null ? (
        error ? (
          <EmptyState title="Could not read plan usage" hint={error} />
        ) : (
          <div className="empty" role="status">
            <b>{loading ? "Reading plan usage…" : "Plan usage not read yet"}</b>
            <span>Asks each subscription CLI how much of its plan is left; no model call is made.</span>
          </div>
        )
      ) : accounts.length === 0 ? (
        <div className="empty">
          <b>{NO_ACCOUNTS_TITLE}</b>
          <span>{NO_ACCOUNTS_HINT}</span>
          {onOpenAccounts ? (
            <button type="button" className="sm plans-empty-btn" onClick={onOpenAccounts}
              aria-label="Which accounts Khipu reads: opens Settings, Accounts and routing">
              Which accounts Khipu reads
            </button>
          ) : null}
        </div>
      ) : (
        accounts.map((row, i) => (
          <PlanRow key={`${row.harness}|${row.home ?? ""}|${i}`} row={row} now={now}
            pause={row.key ? pauses.get(row.key) : undefined} onSignedIn={reread} />
        ))
      )}
    </div>
  );
}

/** "Wed, Oct 14, 3:38 PM, 2 min ago"; null without a time. */
function whenAndAge(iso: string | null | undefined): string | null {
  const d = parseIso(iso);
  if (!d) return null;
  const local = d.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const age = fmtAgoSince(iso);
  return age ? `${local}, ${age}` : local;
}

/** The one failure line of a Settings block: the cause, what Khipu still
 *  shows, and the one thing to do if there is one. */
function failureLine(row: UsageAccount, status: PlanStatus, now: Date): string | null {
  const code = status.code;
  if (!code || code === "pending") return null;
  if (needsOwner(code)) {
    const note = (row.note ?? "").trim();
    if (note) return /[.)]$/.test(note) ? note : `${note}.`;
    const cli = CLI_TITLE[row.harness ?? ""] ?? "This CLI";
    return code === "no_plan_limits" ? "This sign-in reports no plan limits." : `${cli} is not signed in.`;
  }
  const cause = failureCause(row, code);
  // The figures' age is on the block's read line, so it is said once.
  const checked = fmtAgoSince(row.last_attempt_at || row.probed_at);
  const when = checked ? ` Last checked ${checked}.` : "";
  if (status.figures !== "none") return `Can't read it now: ${cause}. Showing earlier figures.${when}`;
  if (onlyResetWindows(row, now)) return `Can't read it now: ${cause}. Every limit has reset since it was read.${when}`;
  return `Can't read its plan: ${cause}. No figures to show.${when}`;
}

/** One account in Settings: a bordered block under its name, holding every
 *  line about it (the pause first, each window, the one failure line with
 *  sign-in, when it was read, and Details), so no line floats loose. */
export function PlanBlock({ row, now, pause, onSignedIn }: {
  row: UsageAccount; now: Date; pause?: string; onSignedIn?: () => Promise<unknown> | void;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const name = accountName(row);
  const status = planStatus(row, now);
  const windows = (row.windows ?? []).filter((w) => pctOf(w) != null);
  const fail = failureLine(row, status, now);
  const dim = status.figures === "kept" || (status.code != null && status.figures === "none");
  const triedAge = fmtAgoSince(row.last_attempt_at || row.probed_at);
  const figuresAge = fmtAgoSince(figuresAt(row));
  const login = row.home || "default login";
  return (
    <section className={status.tone === "warn" ? "plan-block warn" : "plan-block"} role="group" aria-label={name}>
      <h2 className="plan-block-head">
        {name}
        {row.plan ? <span className="meta">{`${row.plan.charAt(0).toUpperCase()}${row.plan.slice(1)} plan`}</span> : null}
      </h2>
      {pause ? (
        <div className="plan-paused">
          <Tag>{pause}</Tag>
          <span>Khipu still reads its plan; a read uses no turn.</span>
        </div>
      ) : null}
      {windows.length ? (
        <ul className="plan-wins" aria-label={`${name} windows`}>
          {windows.map((w) => {
            const p = pctOf(w) as number;
            const reset = resetPhrase(w.resets_at, now);
            const tone = hasReset(w, now) ? null : meterTone(p);
            return (
              <li key={w.name} className="plan-win">
                <span className="plan-wname">{windowLabel(w)}</span>
                <Meter pct={p} label={`${planName(row)} ${windowLabel(w)}`} tone={tone} dim={dim} />
                <span className={`plan-pct ${tone ?? (dim ? "dim" : "")}`}>{fmtPct(p)}</span>
                <span className="plan-reset">{reset ?? "no reset reported"}</span>
              </li>
            );
          })}
        </ul>
      ) : null}
      {fail ? (
        <div className="plan-fail">
          <span className="hdot warn" aria-hidden="true" />
          <span>{fail}</span>
          <SignInControl row={row} onSignedIn={onSignedIn} />
        </div>
      ) : !windows.length ? (
        <p className="plan-empty">
          {status.code === "pending" ? "Reading it for the first time." : row.harness === "muse" ? MUSE_NO_TURN : NO_RESETTING_LIMIT}
        </p>
      ) : null}
      <div className="plan-reads">
        {status.figures === "live" && !row.observed_at && !row.stale ? (
          <Tag tone="ok" dot>
            {triedAge ? `Live: read ${triedAge}` : "Live"}
          </Tag>
        ) : row.source === "rollout" && windows.length && !fail?.includes("last turn") ? (
          // Codex's session-file fallback, kept for tracked accounts only (decision
          // 21); a failure line that already says "last turn" says it once.
          <span>{figuresAge ? `From its last conversation, ${figuresAge}` : "From its last conversation"}</span>
        ) : row.observed_at && windows.length && !fail?.includes("last turn") ? (
          <span>{figuresAge ? `As of its last turn, ${figuresAge}` : "As of its last turn"}</span>
        ) : windows.length ? (
          <span>{figuresAge ? `Figures from ${figuresAge}` : "Earlier figures"}</span>
        ) : !status.code && triedAge ? (
          <span>Checked {triedAge}</span>
        ) : null}
        <span>{login}</span>
      </div>
      {status.code && status.code !== "pending" ? (
        <div className="plan-details-fold">
          <button type="button" className="disclosure plan-details-btn" aria-expanded={open} aria-controls={`${id}-details`}
            onClick={() => setOpen((o) => !o)}>
            <ChevronRight size={14} className="disclosure-chevron" aria-hidden />
            Details
          </button>
          <dl className="plan-details" id={`${id}-details`} hidden={!open}>
            <dt>Short code</dt>
            <dd>
              <code>{status.code}</code>
            </dd>
            <dt>Last attempt</dt>
            <dd>{whenAndAge(row.last_attempt_at || row.probed_at) ?? "Not recorded"}</dd>
            <dt>Last good read</dt>
            <dd>{whenAndAge(row.last_good_at || row.observed_at) ?? "None yet"}</dd>
            <dt>Login</dt>
            <dd>{login}</dd>
          </dl>
        </div>
      ) : null}
    </section>
  );
}

/** Plans as one card: one block per account, Check all now, and which
 *  accounts are read (`usage_accounts`). Shares Home's reads (`state`).
 *  Settings → Accounts and routing now shows each block inside its account
 *  (settings clarity phase 3); this card stays for its unit tests. */
export function PlansSettingsCard({ state, active, routing }: {
  state: PlanUsageState;
  active: boolean;
  routing?: RouteView | null;
}) {
  const st = state;
  const accounts = st.data?.accounts ?? [];
  const now = new Date();
  const pauses = planPauses(routing, now);
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
        {st.data && accounts.length === 0 ? <p className="set-help">{NO_ACCOUNTS_HINT_HERE}</p> : null}
        {accounts.length ? (
          <div className="plan-blocks">
            {accounts.map((row, i) => (
              <PlanBlock key={`${row.harness}|${row.home ?? ""}|${i}`} row={row} now={now}
                pause={row.key ? pauses.get(row.key) : undefined} onSignedIn={st.reread} />
            ))}
          </div>
        ) : null}
        <div className="toolbar">
          <RefreshButton busy={st.refreshing || st.loading} label="Check all now" onClick={() => void st.refresh()} />
          <span className="muted">Asks every CLI again now; takes up to about seven seconds. No model call is made.</span>
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
export type UsageAccountsStatus = {
  value: unknown[] | null;
  source: "file" | "default";
  accounts: UsageAccountEntry[];
  /** The owner's own folder, so "~/.x" and "/Users/me/.x" compare as one login. */
  user_home?: string | null;
  /** The list as read: a save carries it and is refused once the list moved. */
  version?: string | null;
};

export const USAGE_HARNESS_OPTIONS: Array<[string, string]> = [
  ["claude", "Claude Code"],
  ["codex", "Codex"],
  ["grok", "Grok Build"],
  ["muse", "Muse"],
  ["glm", "GLM Coding Plan"],
];

function harnessName(h: string): string {
  return USAGE_HARNESS_OPTIONS.find(([v]) => v === h)?.[1] ?? h;
}

const LABEL_MAX = 64;
const FOLDER_MAX = 1024;
/** The most accounts read: `khipu config` and the app's own command refuse a longer list. */
export const ACCOUNTS_MAX = 32;

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

function shownAs(e: UsageAccountEntry): StoredEntry {
  return e.home ? { label: e.label, harness: e.harness, home: e.home } : { label: e.label, harness: e.harness };
}

/** The list to store, one entry per entry in `entries`: one shown from
 *  `status` goes back exactly as stored, an invalid one too (only Khipu
 *  decides what is valid); one added here as `{label, harness, home?}`. */
export function storable(entries: UsageAccountEntry[], status?: UsageAccountsStatus | null): unknown[] {
  const raw = status?.source === "file" && Array.isArray(status.value) ? status.value : null;
  return entries.map((e) => {
    const at = status ? status.accounts.indexOf(e) : -1;
    return raw && at >= 0 && at < raw.length ? raw[at] : shownAs(e);
  });
}

/** What a refusal is about: the entry at fault (null when the list as a
 *  whole is), the earlier entry it repeats, and whether it is the entry
 *  being added from the form (then the owner is pointed at a field). */
type RefusalCtx = { entry: StoredEntry | null; other: StoredEntry | null; field: string | null; added: boolean };

function fieldWords(c: RefusalCtx): string {
  const which = c.field === "label" ? "Name" : "Login folder";
  return c.added || !c.entry ? `The ${which}` : `The ${which} of ${c.entry.label}`;
}

/** A failure that is not about the entry typed (the list as sent, Khipu not
 *  starting, the app's own command): it never points at the fields. */
const ACCOUNTS_NOT_SAVED = "Khipu could not save the account list. Try again in a minute.";
const TOO_MANY = `Khipu reads at most ${ACCOUNTS_MAX} accounts. Remove one before adding another.`;

/** Each refusal code `khipu config --set usage_accounts` gives, as Khipu's
 *  own sentence. The CLI's text never reaches the owner: it names the
 *  setting, counts entries the owner never added and says "home". */
const ACCOUNT_REFUSAL: Record<string, (c: RefusalCtx) => string> = {
  bad_list: () => ACCOUNTS_NOT_SAVED,
  bad_entry: () => ACCOUNTS_NOT_SAVED,
  too_many: () => TOO_MANY,
  not_one_line: (c) => `${fieldWords(c)} must be one line of plain text.`,
  too_long: (c) =>
    c.field === "label"
      ? `${fieldWords(c)} is too long: use ${LABEL_MAX} characters or fewer.`
      : `${fieldWords(c)} is too long.`,
  unknown_cli: () => "Khipu reads Claude Code, Codex, Grok Build, Muse and the GLM Coding Plan. Choose one of them under CLI.",
  muse_folder: () => "Muse has one sign-in, so leave the Login folder empty.",
  glm_folder: () => "GLM is read with its key, so leave the Login folder empty.",
  zai_home: () => "That login folder runs Claude Code against z.ai, so it is the GLM account. Track GLM instead.",
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
  name_has_bar: () => "A name cannot contain the | sign. Type a different Name.",
  name_is_cli: () => "That name is the name of a CLI. Type a different Name.",
  name_is_id: () => "That name is the id of another account. Type a different Name.",
  version_moved: () => "The account list changed while you were working. Khipu read it again; check it and try once more.",
  version_needed: () => "The account list changed while you were working. Khipu read it again; check it and try once more.",
  last_account: () => "This is the last account Khipu reads. Confirm to stop reading accounts altogether.",
  name_needed: () => "Give this account a name.",
  folder_not_found: () => "Khipu cannot find that login folder. Check the folder and try again.",
  owner_only: () => "Only you can change this, from Khipu's Settings.",
  request_waiting: () => "A request to change the account list is already waiting for your answer.",
  not_tracked: () => "Khipu does not track that account.",
  no_such_hold: () => "That hold has already ended.",
  no_request: () => "No request to change the account list is waiting.",
  bad_folder: () => "Give the login folder starting with ~/ or its full path starting with /.",
  bad_command: () => ACCOUNTS_NOT_SAVED,
  write_failed: () => ACCOUNTS_NOT_SAVED,
  config_unreadable: () => "Khipu cannot read its settings file, so it changed nothing.",
  record_refused: () => "Khipu's routing record could not take this change. Open Accounts and routing to see what is saved now.",
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
  const r = res.refusal ?? {};
  // The app's own command refuses a missing version with no code.
  const code = typeof r.code === "string" ? r.code : res.error.includes("needs the version it read") ? "version_needed" : "";
  const index = typeof r.index === "number" ? r.index : null;
  const otherIndex = typeof r.other === "number" ? r.other : null;
  const field = typeof r.field === "string" ? r.field : null;
  const say = Object.prototype.hasOwnProperty.call(ACCOUNT_REFUSAL, code) ? ACCOUNT_REFUSAL[code] : null;
  if (!say) {
    // The app's own command refuses a list over the cap before the CLI runs,
    // as an error with no code: judged on the list sent, never on its text.
    return { field: null, message: sent.length > ACCOUNTS_MAX ? TOO_MANY : ACCOUNTS_NOT_SAVED };
  }
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
    const list = next == null ? [] : storable(next, status);
    // The version of the list this editor loaded, not one fetched now.
    const expect = status?.version?.trim() ? status.version : null;
    if (!expect) {
      // Never saved without the version it read: read the list, then try again.
      await cfg.reload();
      setSaving(false);
      setErr(ACCOUNT_REFUSAL.version_needed({ entry: null, other: null, field: null, added: false }));
      setClash(null);
      return false;
    }
    const res = next === null
      ? await callCli("khipu_config_unset", { key: "usage_accounts", expect })
      : await callCli("khipu_config_set", { key: "usage_accounts", value: JSON.stringify(list), expect });
    if (res.ok) onSaved?.();
    await cfg.reload();
    setSaving(false);
    if (!res.ok) {
      const problem = accountsRefusal(res, (next ?? []).map(shownAs), added);
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
    if (entries.length >= ACCOUNTS_MAX) {
      // Checked before anything is sent: the refusal would otherwise come
      // from the app's own command, with no code to say it by.
      setErr(TOO_MANY);
      setClash(null);
      return;
    }
    const folder = harness === "muse" || harness === "glm" ? null : home.trim() || null;
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
      next.length - 1,
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
          value={harness === "muse" || harness === "glm" ? "" : home}
          maxLength={FOLDER_MAX}
          placeholder={harness === "muse" ? "Muse has one login" : harness === "glm" ? "GLM is read with its key" : "~/.claude-second…"}
          disabled={harness === "muse" || harness === "glm"}
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
