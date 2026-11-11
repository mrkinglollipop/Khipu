/* The Accounts place, in words (slice A4). Pure functions: what each account's
   row and page say, from the routing record (`khipu route --json`) and the
   plan read. Nothing here changes anything and nothing forecasts: every number
   is one Khipu gave (percent used, the reset time, the pace verdict, the time
   it says an account runs out). An account whose state Khipu does not know is
   never shown as having room. */

import { accountState, whenLocal, windowWords, spanWords } from "../routing";
import type { CapStatus, Pace, RouteAccount, RouteView } from "../routing";
import { signInOffer } from "../PlansCard";
import type { UsageAccount, UsageReport } from "../PlansCard";
import { capSentence } from "../accountControlsModel";
import { planWord } from "../humanWords";
import { endChoices } from "../routingWords";
import type { EndChoice } from "../routingWords";

export type RowTone = "ok" | "warn" | "err" | "none";

export type RowKind =
  | "room"
  | "paused"
  | "limited"
  | "down"
  | "signin"
  | "probably_open"
  | "noreading"
  | "unknown";

/** One window Khipu reads for an account, as the page lists it. */
export type WindowModel = {
  key: string;
  name: string;
  /** Percent left, as Khipu's percent used says it. */
  left: number;
  resets: string;
  word: string;
  tone: RowTone;
  /** Khipu's own sentence about this window, when it has one. */
  note: string | null;
};

export type StopAt =
  /** Zero or one "stop at" level and nothing else that limits the account. */
  | { kind: "level"; pct: number | null; window: string; cap: CapStatus | null; others: CapStatus[] }
  /** The limits cannot be shown as one level: they stay as the owner's rules. */
  | { kind: "rules"; caps: CapStatus[] };

export type RowModel = {
  key: string;
  label: string;
  /** The line under the name: where agents stop, or that rules decide. */
  sub: string | null;
  /** A report warning alongside the account's own state and figures. */
  warning: string | null;
  kind: RowKind;
  line: string;
  word: string;
  tone: RowTone;
  /** Percent left for the meter; null draws no meter. */
  left: number | null;
  /** Where agents stop on the meter's window, as a percent used. */
  stopAtPct: number | null;
  ahead: number;
};

const WINDOW_NAMES: Record<string, string> = { five_hour: "Next 5 hours", weekly: "This week", daily: "Today", monthly: "This month" };
const WINDOW_OF: Record<string, string> = { five_hour: "of the next 5 hours", weekly: "of the week", daily: "of the day", monthly: "of the month" };

export const windowName = (w: string): string => WINDOW_NAMES[w] ?? `This ${windowWords(w)}`;
const windowOf = (w: string): string => WINDOW_OF[w] ?? `of the ${windowWords(w)}`;

const isNum = (n: unknown): n is number => typeof n === "number" && Number.isFinite(n);
const parse = (iso: string | null | undefined): Date | null => {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
};

/** Khipu's verdict on one window, as a word. "Pace not known" when it has none. */
export function paceWord(p: Pace): { word: string; tone: RowTone } {
  const verdicts = [p.plan?.verdict, p.share_cap?.verdict];
  if (verdicts.includes("limit_reached")) return { word: "Out of room", tone: "err" };
  if (verdicts.includes("cap_reached")) return { word: "At your stop level", tone: "err" };
  switch (p.plan?.verdict) {
    case "outpacing":
      return { word: "Ahead of pace", tone: "warn" };
    case "on_pace":
      return { word: "On pace", tone: "ok" };
    case "underpacing":
      return { word: "Under pace", tone: "ok" };
    default:
      return { word: "Pace not known", tone: "none" };
  }
}

/** The windows Khipu reads for this account: the pace record's, then the
 *  plan fact's longest window when there is no pace. A window whose reset
 *  time has passed is not a reading. */
export function accountWindows(a: RouteAccount, view: RouteView | null | undefined, now: Date): WindowModel[] {
  const out: WindowModel[] = [];
  for (const p of a.pace ?? []) {
    if (!isNum(p.used_pct)) continue;
    const reset = parse(p.cycle_resets_at);
    if (reset && reset.getTime() <= now.getTime()) continue;
    const { word, tone } = paceWord(p);
    let note: string | null = null;
    const runsOut = parse(p.plan?.runs_out_at);
    if (p.plan?.verdict === "outpacing" && runsOut && isNum(p.plan.before_reset_s) && p.plan.before_reset_s > 0) {
      note = `Khipu reads it running out ${whenLocal(runsOut.toISOString(), now)}, ${spanWords(p.plan.before_reset_s)} before it resets.`;
    }
    out.push({
      key: p.window,
      name: windowName(p.window),
      left: Math.max(0, Math.min(100, Math.round(100 - p.used_pct))),
      resets: reset ? whenLocal(p.cycle_resets_at, now) : "",
      word,
      tone,
      note,
    });
  }
  if (out.length) return out;
  const fact = view?.plan_facts?.[a.key];
  if (fact?.readable && isNum(fact.left_pct) && !fact.stale && !fact.reset_passed) {
    const w = fact.window ?? "weekly";
    out.push({
      key: w,
      name: windowName(w),
      left: Math.max(0, Math.min(100, Math.round(fact.left_pct))),
      resets: fact.resets_at ? whenLocal(fact.resets_at, now) : "",
      word: "Pace not known",
      tone: "none",
      note: null,
    });
  }
  return out;
}

/** The window the row speaks for: the one with the least left. */
export function tightest(ws: WindowModel[]): WindowModel | null {
  return ws.reduce<WindowModel | null>((best, w) => (best == null || w.left < best.left ? w : best), null);
}

/** One "stop at" level when the account's limits are exactly that. Anything
 *  else (two share limits, an even-pace limit, a spend or job-count limit)
 *  is kept as the owner's rules, listed beside the level. */
export function stopAt(a: RouteAccount): StopAt {
  const caps = a.cap_statuses ?? [];
  const shares = caps.filter((c) => c.kind === "share");
  const others = caps.filter((c) => c.kind !== "share");
  if (shares.length > 1) return { kind: "rules", caps };
  const cap = shares[0] ?? null;
  const windows = (a.pace ?? []).map((p) => p.window);
  const window = cap?.window || (windows.includes("weekly") ? "weekly" : windows[0] ?? "weekly");
  return { kind: "level", pct: cap && isNum(cap.pct) ? cap.pct : null, window, cap, others };
}

export const STOP_AT_LEVELS = [80, 85, 90, 95];

/** The choices for the stop-at select: no limit, the usual levels, and the
 *  account's own level when it is none of them. */
export function stopAtOptions(current: number | null): { value: string; label: string }[] {
  const levels = current != null && !STOP_AT_LEVELS.includes(current) ? [...STOP_AT_LEVELS, current].sort((x, y) => x - y) : STOP_AT_LEVELS;
  return [{ value: "", label: "No limit, use it all" }, ...levels.map((n) => ({ value: String(n), label: `At ${n}%` }))];
}

/** "Agents stop at 90%." or "Set by your rules." for the line under a name. */
export function stopAtLine(s: StopAt): string | null {
  if (s.kind === "rules") return "Set by your rules";
  if (s.pct != null) return `Agents stop at ${s.pct}%`;
  return s.others.length ? "Also set by your rules" : null;
}

/** Each other limit on the account as one plain sentence. */
export function ruleSentences(caps: CapStatus[]): string[] {
  return caps.map((c) => capSentence(c));
}

function usageFor(a: RouteAccount, plans: UsageReport | null | undefined): UsageAccount | undefined {
  return (plans?.accounts ?? []).find((r) => r.key === a.key);
}

/** The plan name for the page header, "Max plan"; null when the read carries none. */
export function planLabel(a: RouteAccount, plans: UsageReport | null | undefined): string | null {
  const w = planWord(usageFor(a, plans)?.plan);
  return w ? `${w} plan` : null;
}

/** True when the account's own sign-in is what stops Khipu reading it. */
export function needsSignIn(a: RouteAccount, plans: UsageReport | null | undefined): boolean {
  const row = usageFor(a, plans);
  if (!row) return false;
  if (signInOffer(row)) return true;
  return row.ok === false && row.error === "not_signed_in";
}

/** The order the accounts show in: the owner's priority first, then the rest as the record lists them. */
export function orderedAccounts(view: RouteView | null | undefined): RouteAccount[] {
  const all = view?.accounts ?? [];
  const entries = view?.priority?.entries ?? [];
  const first = entries.map((e) => all.find((a) => a.key === e.account)).filter((a): a is RouteAccount => !!a);
  const seen = new Set(first.map((a) => a.key));
  return [...first, ...all.filter((a) => !seen.has(a.key))];
}

export function rowModel(
  a: RouteAccount,
  view: RouteView | null | undefined,
  plans: UsageReport | null | undefined,
  ahead: number,
  now: Date,
): RowModel {
  const stop = stopAt(a);
  const usage = plans?.accounts?.find((row) => row.key === a.key);
  const warning = usage?.same_account_as && usage.note ? usage.note : null;
  const base = { key: a.key, label: a.label, sub: stopAtLine(stop), warning, left: null, stopAtPct: null, ahead };
  const state = accountState(a, now);
  switch (a.state) {
    case "paused":
      return { ...base, kind: "paused", line: state.text, word: "Paused", tone: "none" };
    default:
      break;
  }
  if (needsSignIn(a, plans)) {
    return { ...base, kind: "signin", line: "The sign-in ran out, so Khipu can't read its room", word: "Needs sign-in", tone: "warn" };
  }
  switch (a.state) {
    case "limited":
      return { ...base, kind: "limited", line: state.text, word: "Out of room", tone: "err" };
    case "down":
      return { ...base, kind: "down", line: state.text, word: "Down", tone: "err" };
    case "probably_open":
      return { ...base, kind: "probably_open", line: state.text, word: "Probably open", tone: "warn" };
    case "open":
      break;
    default:
      return { ...base, kind: "unknown", line: state.text, word: "Not known", tone: "none" };
  }
  const ws = accountWindows(a, view, now);
  const w = tightest(ws);
  if (!w) {
    return { ...base, kind: "noreading", line: "Khipu has no reading for this account yet", word: "No reading yet", tone: "none" };
  }
  const level = stop.kind === "level" && stop.pct != null && stop.window === w.key ? stop.pct : null;
  const extra = a.models?.length && state.text !== "Open" ? ` · ${state.text}` : "";
  const line =
    `${w.left}% left ${windowOf(w.key)}${w.resets ? ` · resets ${w.resets}` : ""}` + extra;
  return { ...base, kind: "room", line, word: w.word, tone: warning ? "warn" : w.tone, left: w.left, stopAtPct: level };
}

/** "Read 3 minutes ago." from the record's own check time. */
export function readLine(view: RouteView | null | undefined, now: Date): string | null {
  const at = parse(view?.checked_at);
  if (!at) return null;
  const mins = Math.max(0, Math.round((now.getTime() - at.getTime()) / 60_000));
  if (mins < 1) return "Read just now.";
  if (mins < 60) return `Read ${mins} ${mins === 1 ? "minute" : "minutes"} ago.`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `Read ${hours} ${hours === 1 ? "hour" : "hours"} ago.`;
  return `Read ${Math.round(hours / 24)} days ago.`;
}

/** The ends a run-ahead grant offers. Always a time: the app never offers "no end". */
export function grantEnds(now: Date): EndChoice[] {
  return endChoices(now, 2).filter((c) => c.id !== "never");
}

/** The ends a pause offers, "Until I clear it" included (a pause may have none). */
export function pauseEnds(now: Date): EndChoice[] {
  return endChoices(now, 1);
}
