/* Plan usage in the app (docs/plans/2026-10-14-plan-usage.md, phase 2): how
   much of each subscription CLI's plan is left, read from `khipu usage
   --json` through two fixed-argv commands (`khipu_usage_status`, cache first;
   `khipu_usage_refresh`, probes now). Khipu only reports these figures; it
   never moves work between plans. The shape mirrors `khipu/usage.py`
   (`report()`); a window a CLI does not report is absent, never zero. */

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";
import { EmptyState, ListRow, Tag } from "./ui";
import { callCli } from "./settingsCli";
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
 *  own text never reaches the row, so these are all there is to show. */
const ERROR_PHRASE: Record<string, string> = {
  timeout: "didn't answer in time",
  not_signed_in: "not signed in",
  not_installed: "not installed",
  bad_reply: "unreadable reply",
  no_plan_limits: "no plan limits reported",
};

export function usageErrorPhrase(code: string | null | undefined): string {
  return (code && ERROR_PHRASE[code]) || "couldn't read";
}

function pctOf(w: UsageWindow): number | null {
  const v = w.used_pct;
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** The window closest to its limit, or null when the account reports none. */
export function tightestWindow(row: UsageAccount): UsageWindow | null {
  let best: UsageWindow | null = null;
  for (const w of row.windows ?? []) {
    const p = pctOf(w);
    if (p == null) continue;
    if (best == null || p > (pctOf(best) ?? -1)) best = w;
  }
  return best;
}

/** The tightest window across every account. */
export function tightestOverall(
  report: UsageReport | null | undefined,
): { row: UsageAccount; window: UsageWindow } | null {
  let out: { row: UsageAccount; window: UsageWindow } | null = null;
  for (const row of report?.accounts ?? []) {
    const w = tightestWindow(row);
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

export function fmtPct(n: number): string {
  return `${Number.isInteger(n) ? n : n.toFixed(1)}%`;
}

function parseIso(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** The reset in the viewer's own time zone, e.g. "Tue 9:57 PM". */
export function fmtResetLocal(iso: string | null | undefined): string | null {
  const d = parseIso(iso);
  return d ? d.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }) : null;
}

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** The reset in UTC, as the CLI and the health report write it: "Tue 04:57Z". */
export function fmtResetUtc(iso: string | null | undefined): string | null {
  const d = parseIso(iso);
  if (!d) return null;
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${DAYS[d.getUTCDay()]} ${hh}:${mm}Z`;
}

export function meterTone(pct: number): "err" | "warn" | null {
  if (pct >= PLAN_ALERT_PCT) return "err";
  if (pct >= PLAN_WARN_PCT) return "warn";
  return null;
}

/** "Codex Pro week at 94%, resets Tue 04:57Z" for the tightest window across
 *  every account; null when no account reports a window. */
export function plansHealthDetail(report: UsageReport | null | undefined): string | null {
  const t = tightestOverall(report);
  if (!t) return null;
  const reset = fmtResetUtc(t.window.resets_at);
  return `${planName(t.row)} ${windowLabel(t.window)} at ${fmtPct(pctOf(t.window) ?? 0)}${reset ? `, resets ${reset}` : ""}`;
}

/** One attention item when any window is at or above 90%. */
export function planAttention(report: UsageReport | null | undefined): Attention[] {
  const hot: string[] = [];
  for (const row of report?.accounts ?? []) {
    for (const w of row.windows ?? []) {
      const p = pctOf(w);
      if (p == null || p < PLAN_ALERT_PCT) continue;
      const reset = fmtResetUtc(w.resets_at);
      hot.push(`${planName(row)} ${windowLabel(w)} at ${fmtPct(p)}${reset ? `, resets ${reset}` : ""}`);
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

function parseReport(data: unknown): UsageReport | null {
  if (!data || typeof data !== "object") return null;
  const accounts = (data as UsageReport).accounts;
  return Array.isArray(accounts) ? (data as UsageReport) : null;
}

/** The report, read cache first while `active`; `refresh` probes every account now. */
export function usePlanUsage(active: boolean) {
  const [data, setData] = useState<UsageReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const latest = useRef(0);

  const run = useCallback(async (command: "khipu_usage_status" | "khipu_usage_refresh") => {
    const request = ++latest.current;
    const setBusy = command === "khipu_usage_refresh" ? setRefreshing : setLoading;
    setBusy(true);
    const res = await callCli<UsageReport>(command);
    setBusy(false);
    if (latest.current !== request) return;
    if (!res.ok) {
      setError(res.error);
      return;
    }
    const report = parseReport(res.data);
    if (report) {
      setData(report);
      setError(null);
    } else {
      setError("Khipu gave an unreadable answer.");
    }
  }, []);

  const load = useCallback(() => run("khipu_usage_status"), [run]);
  const refresh = useCallback(() => run("khipu_usage_refresh"), [run]);

  useEffect(() => {
    if (active) void load();
  }, [active, load]);

  return { data, error, loading, refreshing, load, refresh };
}

export type PlanUsageState = ReturnType<typeof usePlanUsage>;

function Meter({ pct, label }: { pct: number; label: string }) {
  const tone = meterTone(pct);
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

function StatusTags({ row }: { row: UsageAccount }) {
  return (
    <>
      {row.observed_at ? <Tag>as of the last turn</Tag> : null}
      {row.stale ? <Tag tone="warn">stale</Tag> : null}
    </>
  );
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
  "No claude, codex, grok or muse command was found on this Mac, and usage_accounts in Khipu's config.json lists none.";

/** One account on Home: its tightest window as a bar, or why there is none. */
function PlanRow({ row }: { row: UsageAccount }) {
  const w = tightestWindow(row);
  const p = w ? pctOf(w) : null;
  const reset = w ? fmtResetLocal(w.resets_at) : null;
  return (
    <ListRow className="plan-row">
      <span className="plan-name ellip">
        {row.account || row.harness}
        {row.plan ? <span className="meta"> {row.plan}</span> : null}
      </span>
      {w && p != null ? (
        <>
          <Meter pct={p} label={`${planName(row)} ${windowLabel(w)}`} />
          <span className={`plan-pct ${meterTone(p) ?? ""}`}>{fmtPct(p)}</span>
          <span className="plan-when meta">
            {windowLabel(w)}
            {reset ? ` · resets ${reset}` : ""}
          </span>
        </>
      ) : (
        <span className="plan-none t2">
          {row.ok === false ? usageErrorPhrase(row.error) : row.note || "no limits reported"}
        </span>
      )}
      <span className="plan-tags">
        {row.ok === false && w ? <Tag tone="warn">{usageErrorPhrase(row.error)}</Tag> : null}
        <StatusTags row={row} />
      </span>
    </ListRow>
  );
}

/** Home → Plans: one row per account, the tightest window of each. */
export function PlansCard({ state }: { state: PlanUsageState }) {
  const { data, error, loading, refreshing, refresh } = state;
  const accounts = data?.accounts ?? [];
  return (
    <div className="card plans-card">
      <div className="card-head">
        Plans
        <span className="spacer" />
        <RefreshButton busy={refreshing} label="Refresh" onClick={() => void refresh()} />
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
            <PlanRow key={`${row.harness}|${row.home ?? ""}|${i}`} row={row} />
          ))}
          {error ? (
            <ListRow>
              <span className="meta">Last read failed: {error}</span>
            </ListRow>
          ) : null}
        </>
      )}
    </div>
  );
}

/** Settings → Capture & models → Plans: every account, every window, its
 *  reset and when it was read, and Probe now. Accounts are read only here. */
export function PlansSettingsCard({ active }: { active: boolean }) {
  const st = usePlanUsage(active);
  const accounts = st.data?.accounts ?? [];
  return (
    <div className="section-card">
      <div className="section-head">Plans</div>
      <div className="section-body">
        {st.error ? (
          <p className="set-error" role="alert">
            Could not read plan usage: {st.error}
          </p>
        ) : null}
        {st.data && accounts.length === 0 ? (
          <p className="set-help">{NO_ACCOUNTS_HINT}</p>
        ) : null}
        {accounts.map((row, i) => {
          const probed = fmtAgeSince(row.last_attempt_at || row.probed_at);
          const windows = (row.windows ?? []).filter((w) => pctOf(w) != null);
          return (
            <div key={`${row.harness}|${row.home ?? ""}|${i}`} className="rows plan-account" role="group" aria-label={row.account || row.harness}>
              <div className="row-item">
                <span className="row-main">
                  {row.account || row.harness}
                  {row.plan ? <span className="meta"> {row.plan}</span> : null}
                </span>
                {row.ok === false ? <Tag tone="warn">{usageErrorPhrase(row.error)}</Tag> : null}
                <StatusTags row={row} />
                <span className="row-meta">
                  {row.home || "default login"}
                  {probed ? ` · read ${probed} ago` : ""}
                </span>
              </div>
              {windows.length === 0 ? (
                <div className="row-item">
                  <span className="row-main t2">
                    {row.ok === false ? usageErrorPhrase(row.error) : row.note || "no limits reported"}
                  </span>
                </div>
              ) : (
                windows.map((w) => {
                  const p = pctOf(w) as number;
                  const reset = fmtResetLocal(w.resets_at);
                  return (
                    <div key={w.name} className="row-item plan-window">
                      <span className="plan-wname">{windowLabel(w)}</span>
                      <Meter pct={p} label={`${planName(row)} ${windowLabel(w)}`} />
                      <span className={`plan-pct ${meterTone(p) ?? ""}`}>{fmtPct(p)}</span>
                      <span className="row-meta">{reset ? `resets ${reset}` : "no reset reported"}</span>
                    </div>
                  );
                })
              )}
            </div>
          );
        })}
        <div className="toolbar">
          <RefreshButton busy={st.refreshing} label="Probe now" onClick={() => void st.refresh()} />
          <span className="muted">Asks every CLI again now; takes up to about seven seconds.</span>
        </div>
        <p className="set-help">
          Accounts come from <code>usage_accounts</code> in Khipu's config.json (empty: every installed CLI, default
          login). No command sets it yet; edit the file, then Probe now.
        </p>
      </div>
    </div>
  );
}
