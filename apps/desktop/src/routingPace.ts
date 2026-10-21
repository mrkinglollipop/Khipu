/* Pace, caps and advice (routing slice 15e; docs/plans/2026-10-20-routing-first-class.md
   sections 2a and 13; mocks docs/mockups/2026-10-21-routing/pace-*.html).
   Pure: each account's windows, caps and jobs advice as `khipu route --json`
   carries them (slices 4Pb, 17a and 17c), in the page's own words, plus the
   group's closed line, its tags and the crossings that need the owner.

   Display only. Setting, confirming or removing a cap is an owner action in
   the CLI, so this model words facts and never offers one. Advice is
   advisory: nothing here ranks, picks, refuses or stops work. A figure the
   readings cannot support is never invented: an unknown cap says why, and a
   total that is not known is not shown from partial coverage. */

import { accountClassification, capitalise, whenLocal, windowWords } from "./routing";
import type { CapStatus, Pace, RouteAccount, RouteView } from "./routing";
import type { UsageReport } from "./PlansCard";

/** A tag's tone, as the app's Tag takes it. */
export type PaceTone = "warn" | "err" | "neutral";
export type PaceTag = { text: string; tone: PaceTone };

/** A sentence in parts, so the one figure it turns on can be bold. */
export type PaceSayPart = { text: string; bold?: boolean };

/** The small trend under a window, on the mock's 120 x 28 viewBox. The used
 *  line is the current level only: `route --json` carries no history, so no
 *  past usage is reconstructed from the trailing rate. The dashed line is
 *  the rate that lands on the window's headline target from the current
 *  level, and the cap line is that target when it is an owner cap. A window
 *  with no rate has no trend. */
export type PaceTrend = {
  used: string;
  need: { y1: number; y2: number } | null;
  capY: number | null;
  label: string;
};

export type PaceWindow = {
  key: string;
  name: string;
  usedPct: number | null;
  meterTone: "warn" | "err" | null;
  /** The r1 rate ("9% an hour"), or null when the reading can't tell. */
  rate: string | null;
  /** "Underpacing" or "On pace": the r1 word that is not a tag. */
  ratePlain: string | null;
  rateTag: PaceTag | null;
  /** The r2 line: the rate needed, the reset, or why there is no rate. */
  r2: string;
  trend: PaceTrend | null;
};

export type PaceCapRow = {
  key: string;
  name: string;
  state: string;
  tag: PaceTag | null;
};

/** Khipu's advice for jobs. `route --json` carries the job purpose only. */
export type PaceAdvice = {
  word: "Go" | "Hold back" | "Closed" | "Not for jobs";
  tone: "go" | "hold" | "closed";
  why: string;
  evid: string;
};

/** One account: its block, or the one line the group keeps closed. */
export type PaceRow = {
  key: string;
  label: string;
  /** "Max plan", from the plan read; null when it carries no label. */
  planLabel: string | null;
  tag: PaceTag;
  say: PaceSayPart[];
  windows: PaceWindow[];
  caps: PaceCapRow[];
  advice: PaceAdvice | null;
  /** Open at first look: its plan is outpacing or a cap is less than 6 h away.
   *  This is the approved mock's "only Claude open"; the README's Q3 wording
   *  also names an owner even-pace lead, which would open Codex too. Every
   *  line opens on click, so either account is one press away. */
  open: boolean;
  /** The account uses its plan limit faster than the trailing rate allows. */
  planOutpacing: boolean;
  line: { week: string; advice: string };
  /** The account's earliest crossing, for Needs you and the closed line. */
  crossing: Crossing | null;
};

export type PaceModel = {
  rows: PaceRow[];
  line: string;
  tags: PaceTag[];
  /** Crossings under 6 h away, each one problem: what Shows the account. */
  crossings: { key: string; label: string; needs: string; show: string; item: string }[];
  /** No account carries pace or caps: the group's empty state. */
  empty: boolean;
};

type CrossingTarget = "plan" | "share" | "day";

type Crossing = {
  at: string;
  when: string;
  hours: number;
  pct: number | null;
  target: CrossingTarget;
  window: string | null;
  beforeResetS: number | null;
};

const WINDOW_ADJ: Record<string, string> = { five_hour: "five-hour", weekly: "week" };
const WINDOW_NAME: Record<string, string> = { five_hour: "Five hours", weekly: "Week" };

const windowAdj = (w: string | null | undefined) => WINDOW_ADJ[w ?? ""] ?? windowWords(w ?? undefined);
const windowName = (w: string | null | undefined) => WINDOW_NAME[w ?? ""] ?? capitalise(windowWords(w ?? undefined));

/** One decimal at most: "11.4", "25", "1.7". */
function num1(n: number): string {
  return String(Math.round(n * 10) / 10);
}

/** A rate in percent an hour, as the mock words it: "9", "7.2", "0.82". */
function rateWords(n: number): string {
  return n < 0.01 ? "<0.01" : String(Math.round(n * 100) / 100);
}

/** "$35" whole, "$21.40" otherwise. */
function usdWords(n: number): string {
  return Number.isInteger(n) ? `$${n}` : `$${n.toFixed(2)}`;
}

/** "30 min", "4.5 h", "1 d 8 h": a span before or until something. */
function shortSpan(seconds: number): string {
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))} min`;
  const h = seconds / 3600;
  if (h < 10 && Math.abs(h - Math.round(h)) > 0.05) return `${num1(h)} h`;
  if (h < 24) return `${Math.round(h)} h`;
  const days = Math.floor(h / 24);
  const rest = Math.round(h % 24);
  return rest ? `${days} d ${rest} h` : `${days} d`;
}

function hoursUntil(iso: string | null | undefined, now: Date): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  return Number.isNaN(t) ? null : (t - now.getTime()) / 3600000;
}

function paceOf(a: RouteAccount, window: string | null | undefined): Pace {
  return (a.pace ?? []).find((p) => p.window === window) ?? { window: window ?? "" };
}

/** A pace reading that cannot be placed at now, as the advice module judges
 *  it. A window too thin for a rate but with a reading still says where
 *  usage stands. */
function readingUnknown(pace: Pace): boolean {
  if (pace.used_pct == null || !pace.buckets) return true;
  const why = pace.why || "";
  return !!why && !why.startsWith("only ");
}

/** The plan label from the plan read: "Max plan", "Pay as you go". */
function planLabel(plan: string | null | undefined): string | null {
  const p = (plan ?? "").trim();
  if (!p) return null;
  if (/^(payg|pay[-_ ]as[-_ ]you[-_ ]go)$/i.test(p)) return "Pay as you go";
  if (/plan$/i.test(p)) return capitalise(p);
  return `${capitalise(p)} plan`;
}

/** Why a window has no rate, as a sentence the page can show. */
function readingWhy(pace: Pace, label: string): string {
  const why = (pace.why ?? "").replace(/\.$/, "");
  if (!why) return "No rate yet.";
  if (/^this account can't be probed/.test(why)) return "Khipu can't read this account's plan.";
  if (/^possible reset/.test(why)) return "Two readings disagree so much one may be a reset; waiting for the next reading.";
  if (/^reset time disagrees/.test(why)) return `${label}'s reset time disagrees between readings.`;
  if (/^reset time spread/.test(why)) return `${label}'s reset time spreads over more than an hour between readings.`;
  if (/^reset passed/.test(why)) return "This window's reset time has passed; waiting for a new reading.";
  if (/^only /.test(why)) return `${capitalise(why)}, too few for a rate yet.`;
  if (/^readings disagree/.test(why)) return `${capitalise(why)}, so there's no rate.`;
  if (/^readings cover/.test(why)) return `${capitalise(why)}, so there's no rate yet.`;
  if (/^last reading/.test(why)) return `The ${why}, so there's no rate now.`;
  return `${capitalise(why)}.`;
}

/** The short cause after "evidence unknown:". */
function evidenceCause(a: RouteAccount): string {
  const statuses = a.cap_statuses ?? [];
  const spend = statuses.find((s) => s.kind === "spend" && s.verdict === "unknown");
  if (spend) return /gap/.test(spend.why ?? "") ? "spend gap" : "spend can't be told";
  const conc = statuses.find((s) => s.kind === "concurrency" && (s.verdict === "unknown" || s.jobs == null));
  if (conc) return "running count expired";
  const pace = (a.pace ?? []).find((p) => readingUnknown(p));
  if (!pace) return "readings can't be placed";
  const why = pace.why ?? "";
  if (/^possible reset/.test(why)) return "possible reset";
  if (/^reset time/.test(why)) return "reset time";
  if (/^reset passed/.test(why)) return "reset passed";
  if (/^readings (cover|disagree)/.test(why) || pace.gappy) return "gappy readings";
  if (/^last reading/.test(why)) return "old readings";
  if (/^only /.test(why)) return "too few readings";
  if (/can't be probed/.test(why)) return "plan not read";
  return "readings can't be placed";
}

/** When the trailing average reaches `pct`, for the trend's dashed line. */
function trendOf(pace: Pace, name: string, need: number | null, capPct: number | null): PaceTrend | null {
  const rate = pace.rate_pct_per_h;
  if (pace.why || rate == null || pace.used_pct == null) return null;
  const points = 24;
  const hours = pace.trailing_h ?? 24;
  const end = pace.used_pct;
  const needEnd = need != null ? end + need * hours : null;
  const values = [end, ...(capPct != null ? [capPct] : []), ...(needEnd != null ? [needEnd] : [])];
  const lo = Math.min(...values) - 2;
  const hi = Math.max(...values) + 2;
  const span = hi - lo || 1;
  const X = (i: number) => Math.round((i * 120) / points * 10) / 10;
  const Y = (v: number) => Math.round((28 - ((v - lo) * 28) / span) * 10) / 10;
  const used = Array.from({ length: points + 1 }, (_, i) => `${X(i)},${Y(end)}`).join(" ");
  const period = hours === 1 ? "in the last hour" : "over the last 24 hours";
  let text = rate < 0.01
    ? `${name}: no use ${period}`
    : `${name}: the last ${hours === 1 ? "hour" : "24 hours"} averaged ${rateWords(rate)}% an hour (trailing average)`;
  if (need != null) {
    const cmp = rate > 1.15 * need ? "faster" : rate < 0.85 * need ? "slower" : "close to";
    text += capPct != null
      ? `, ${cmp} than the dashed rate that lands on your ${Math.round(capPct)}% cap`
      : `, ${cmp} than the dashed rate that lands on the limit`;
  }
  if (capPct != null && need == null) text += `; your ${Math.round(capPct)}% cap line above`;
  return {
    used,
    need: need != null ? { y1: Y(end), y2: Y(end + need * hours) } : null,
    capY: capPct != null ? Y(capPct) : null,
    label: text,
  };
}

/** One window's row: the meter, the rate with its verdict, the rate that
 *  lands on the limit, and the trend. */
function windowRow(pace: Pace, label: string, now: Date): PaceWindow {
  const name = windowName(pace.window);
  const headline = pace.headline ?? "plan";
  const target = headline === "share_cap" ? pace.share_cap : headline === "day_cap" ? pace.day_cap : pace.plan;
  const verdict = target?.verdict ?? "unknown";
  const sharePct = headline === "share_cap" ? (pace.share_cap?.pct ?? null) : null;
  const dayPct = headline === "day_cap" ? (pace.day_cap?.pct ?? null) : null;
  const capPct = sharePct ?? dayPct;
  const need = target && "needed_pct_per_h" in target ? target.needed_pct_per_h ?? null : null;
  const rate = pace.rate_pct_per_h ?? null;
  const resetAt = pace.cycle_resets_at;
  const reset = resetAt ? whenLocal(resetAt, now) : null;

  // The rate that lands on the limit is worth saying while it is within reach
  // of the current one; far past it, the row just says when the window resets.
  const sayNeed = !pace.why && need != null && rate != null && need <= 5 * Math.max(rate, 0.01);

  let r2: string;
  if (pace.why) r2 = readingWhy(pace, label);
  else if (sayNeed) {
    const onto = capPct != null ? Math.round(capPct) : 100;
    const at = reset
      ? headline === "plan" ? ` at the reset ${reset}` : ` at the ${reset} reset`
      : " at the reset";
    r2 = `${rateWords(need!)}% an hour lands on ${onto}%${at}`;
  } else if (reset) r2 = `Resets ${reset}`;
  else r2 = "No reset time yet";

  let ratePlain: string | null = null;
  let rateTag: PaceTag | null = null;
  if (pace.why) ratePlain = null;
  else if (verdict === "outpacing") {
    rateTag = { text: sharePct != null ? `Outpacing your ${Math.round(sharePct)}%` : dayPct != null ? `Over your ${Math.round(dayPct)}% a day` : "Outpacing", tone: "warn" };
  } else if (verdict === "cap_reached") {
    rateTag = { text: sharePct != null ? `At your ${Math.round(sharePct)}%` : "At the cap", tone: "err" };
  } else if (verdict === "limit_reached") rateTag = { text: "Limit reached", tone: "err" };
  else if (verdict === "over") rateTag = { text: dayPct != null ? `Over your ${Math.round(dayPct)}% a day` : "Over", tone: "err" };
  else if (verdict === "underpacing") ratePlain = "Underpacing";
  else if (verdict === "on_pace") ratePlain = "On pace";

  // Warn at 80% of an owner cap, err once a target is reached.
  let meterTone: "warn" | "err" | null = null;
  if (verdict === "cap_reached" || verdict === "limit_reached" || verdict === "over") meterTone = "err";
  else if (capPct != null && (pace.used_pct ?? 0) >= 0.8 * capPct) meterTone = "warn";

  return {
    key: pace.window || "window",
    name,
    usedPct: pace.used_pct != null ? Math.round(pace.used_pct) : null,
    meterTone,
    rate: !pace.why && rate != null
      ? rate < 0.01
        ? `No use ${pace.trailing_h === 1 ? "in the last hour" : "over the last 24 hours"}`
        : `${rateWords(rate)}% an hour`
      : null,
    ratePlain,
    rateTag,
    r2,
    trend: trendOf(pace, name, sayNeed ? need : null, capPct),
  };
}

/** A cap row's name, as the mock words the fact you confirmed. */
function capName(s: CapStatus): string {
  switch (s.kind) {
    case "share":
      return `${windowName(s.window)}: at most ${Math.round(s.pct ?? 0)}%`;
    case "per_day":
      return `Jobs: at most ${Math.round(s.pct ?? 0)}% a day`;
    case "pace_target":
      return (s.pct ?? 0) >= 100
        ? `${windowName(s.window)}: no more than ${num1(s.slack ?? 0)} points ahead of even`
        : `${windowName(s.window)}: even pace to ${Math.round(s.pct ?? 0)}%, ${num1(s.slack ?? 0)} points slack`;
    case "spend":
      return `At most ${usdWords(s.cap_usd ?? 0)} a rolling ${s.days ?? 0} days`;
    case "concurrency":
      return (s.cap_n ?? 0) === 1 ? "One job at a time" : `At most ${s.cap_n ?? 0} jobs at once`;
    default:
      return s.kind;
  }
}

/** A cap row's state, from the figures that judged it; never a number the
 *  reading cannot support. */
function capState(a: RouteAccount, s: CapStatus, now: Date): { state: string; tag: PaceTag | null } {
  switch (s.kind) {
    case "share": {
      const pace = paceOf(a, s.window);
      const cap = pace.share_cap;
      const used = pace.used_pct;
      if (!cap || cap.verdict === "unknown") return { state: "Not known yet", tag: null };
      if (cap.verdict === "cap_reached") {
        return { state: `${used != null ? `${Math.round(used)}% now` : "At it"} · reached`, tag: { text: "Reached", tone: "err" } };
      }
      if (cap.runs_out_at) {
        return { state: `${used != null ? `${Math.round(used)}% now` : "Used"} · reaches it ${whenLocal(cap.runs_out_at, now)}`, tag: null };
      }
      return { state: used != null ? `${Math.round(used)}% now` : "Not known yet", tag: null };
    }
    case "per_day": {
      const day = paceOf(a, s.window).day_cap;
      if (!day || day.verdict === "unknown") return { state: day?.why ? `Not known: ${day.why}` : "Not known yet", tag: null };
      const used = day.used_24h_pct != null ? `${num1(day.used_24h_pct)}% in the last 24 h` : "";
      if (day.verdict === "over") return { state: `${used} · over it`, tag: { text: "Over", tone: "err" } };
      if (day.reaches_cap_at) {
        return { state: `${used} · reaches ${Math.round(day.pct ?? 0)}% at ${whenLocal(day.reaches_cap_at, now)}`, tag: null };
      }
      return { state: used || "Not known yet", tag: null };
    }
    case "pace_target": {
      if (s.verdict === "unknown" || s.allowance_pct == null) return { state: `Not known: ${s.why || "no reading yet"}`, tag: null };
      const state = `Allowed ${num1(s.allowance_pct)}% by now, used ${s.used_pct != null ? num1(s.used_pct) : "none"}`;
      if (s.verdict === "ahead") {
        return { state, tag: { text: `${num1(Math.max(0, (s.used_pct ?? 0) - s.allowance_pct))} points ahead`, tone: "warn" } };
      }
      if (s.verdict === "recovering") return { state, tag: { text: "Coming back", tone: "warn" } };
      return { state, tag: null };
    }
    case "spend": {
      if (s.verdict === "unknown") return { state: `The total can't be told: ${s.why || "the spend reading is incomplete"}`, tag: null };
      if (s.usd == null) return { state: "Not known yet", tag: null };
      const usd = `${usdWords(s.usd)} reported`;
      if (s.verdict === "over") return { state: usd, tag: { text: "Over", tone: "err" } };
      if (s.verdict === "reached") return { state: usd, tag: { text: "At it", tone: "err" } };
      if (s.verdict === "near") return { state: usd, tag: { text: "Near it", tone: "warn" } };
      return { state: usd, tag: null };
    }
    case "concurrency": {
      if (s.jobs == null) return { state: "Not known: the runner's last report has expired", tag: null };
      const state = `${s.jobs} of ${s.cap_n ?? 0} running`;
      if (s.verdict === "full") return { state, tag: { text: "Full", tone: "warn" } };
      return { state, tag: null };
    }
    default:
      return { state: "Not known yet", tag: null };
  }
}

/** Every future crossing among the plan limit and window caps, earliest
 *  first. A closed account is not heading anywhere. */
function crossings(a: RouteAccount, now: Date): Crossing[] {
  const classification = accountClassification(a);
  if (classification === "limited" || classification === "down" || classification === "paused") return [];
  const found: Crossing[] = [];
  for (const pace of a.pace ?? []) {
    for (const [obj, pct, target] of [
      [pace.share_cap, pace.share_cap?.pct ?? null, "share"],
      [pace.day_cap, pace.day_cap?.pct ?? null, "day"],
      [pace.plan, 100, "plan"],
    ] as const) {
      const at = obj && "runs_out_at" in obj
        ? obj.runs_out_at ?? null
        : obj && "reaches_cap_at" in obj ? obj.reaches_cap_at ?? null : null;
      const hours = hoursUntil(at, now);
      if (!at || hours == null || hours <= 0) continue;
      found.push({
        at,
        when: whenLocal(at, now),
        hours,
        pct,
        target,
        window: pace.window,
        beforeResetS: obj && "before_reset_s" in obj ? obj.before_reset_s ?? null : null,
      });
    }
  }
  return found.sort((x, y) => x.hours - y.hours);
}

function earliestCrossing(a: RouteAccount, now: Date): Crossing | null {
  return crossings(a, now)[0] ?? null;
}

/** The account's plan limit is being used faster than its trailing rate
 *  allows. This is the "outpacing" the group's tag counts. */
function planOutpacing(a: RouteAccount): boolean {
  return (a.pace ?? []).some((p) => p.plan?.verdict === "outpacing");
}

/** The share cap this account has already reached, if any: the pace object
 *  for its window first, then its cap status for that same verdict. */
function reachedShareCap(a: RouteAccount): { window: string | null; pct: number } | null {
  const pace = (a.pace ?? []).find((p) => p.share_cap?.verdict === "cap_reached");
  if (pace?.share_cap?.pct != null) return { window: pace.window, pct: pace.share_cap.pct };
  const status = (a.cap_statuses ?? []).find((s) => s.kind === "share" && s.verdict === "cap_reached");
  return status?.pct != null ? { window: status.window ?? null, pct: status.pct } : null;
}

/** The account's tag: the one thing to say about it, in the mock's order. */
function accountTag(a: RouteAccount): PaceTag {
  const classification = accountClassification(a);
  if (classification === "limited") return { text: "Limit reached", tone: "err" };
  if (classification === "down") return { text: "Down", tone: "err" };
  if (classification === "paused") return { text: "Paused", tone: "neutral" };
  if (a.advice?.verdict === "closed") return { text: "On hold", tone: "err" };
  const statuses = a.cap_statuses ?? [];
  const spend = statuses.find((s) => s.kind === "spend");
  if (spend?.verdict === "over" || spend?.verdict === "reached") return { text: "Over your spend cap", tone: "err" };
  if (spend?.verdict === "near") return { text: "Near your spend cap", tone: "warn" };
  if (spend?.verdict === "unknown") return { text: "Spend can't be told", tone: "neutral" };
  const conc = statuses.find((s) => s.kind === "concurrency");
  if (conc?.verdict === "full") return { text: `${conc.jobs ?? 0} of ${conc.cap_n ?? 0} running`, tone: "warn" };
  const paces = a.pace ?? [];
  if (paces.length && paces.every((p) => p.why || readingUnknown(p))) return { text: "Can't tell yet", tone: "neutral" };
  if (!paces.length) return { text: "No pace", tone: "neutral" };
  const reachedShare = reachedShareCap(a);
  if (reachedShare) return { text: `At your ${Math.round(reachedShare.pct)}%`, tone: "err" };
  const planOut = paces.find((p) => p.plan?.verdict === "outpacing");
  if (planOut) return { text: "Outpacing", tone: "warn" };
  const shareOut = paces.find((p) => p.share_cap?.verdict === "outpacing");
  if (shareOut) return { text: `Outpacing your ${Math.round(shareOut.share_cap?.pct ?? 0)}%`, tone: "warn" };
  const dayOut = paces.find((p) => p.day_cap?.verdict === "over" || p.day_cap?.verdict === "near");
  if (dayOut) return { text: `${dayOut.day_cap?.verdict === "over" ? "Over" : "Near"} your ${Math.round(dayOut.day_cap?.pct ?? 0)}% a day`, tone: "warn" };
  const target = statuses.find((s) => s.kind === "pace_target");
  if (target?.verdict === "ahead") return { text: "Ahead of your pace", tone: "warn" };
  if (target?.verdict === "recovering") return { text: "Coming back to your pace", tone: "warn" };
  if (paces.some((p) => p.plan?.verdict === "underpacing")) return { text: "Underpacing", tone: "neutral" };
  if (paces.every((p) => p.plan?.verdict === "unknown")) return { text: "Can't tell yet", tone: "neutral" };
  return { text: "On pace", tone: "neutral" };
}

/** The sentence under an account's name: the headline in plain words. */
function sayOf(a: RouteAccount, now: Date): PaceSayPart[] {
  const classification = accountClassification(a);
  const label = a.label;
  if (classification === "limited") {
    const atLimit = (a.pace ?? []).find((p) => p.plan?.verdict === "limit_reached");
    const window = atLimit?.window ? `${windowAdj(atLimit.window)} ` : "";
    return [
      { text: `At its ${window}limit` },
      { text: a.until ? `, back ${whenLocal(a.until, now)}.` : "." },
    ];
  }
  if (classification === "paused") return [{ text: "Paused by you." }];
  if (classification === "down") return [{ text: `Down: ${a.reason || "out of service"}.` }];

  const paces = a.pace ?? [];
  const unknown = paces.filter((p) => readingUnknown(p));
  if (paces.length && unknown.length === paces.length) {
    const first = unknown[0];
    return [{ text: readingWhy(first, label) }];
  }

  if (!paces.length) {
    // Caps without a plan read: a concurrency count still says what Khipu
    // knows, and an unread spend total says why instead of a partial figure.
    const spend = (a.cap_statuses ?? []).find((s) => s.kind === "spend");
    const conc = (a.cap_statuses ?? []).find((s) => s.kind === "concurrency");
    if (conc) return [{ text: `Khipu doesn't read ${label}'s plan, so there is no pace. It counts the jobs its runners report.` }];
    if (spend?.verdict === "unknown") {
      return [{ text: `The spend total can't be told: ${spend.why || "the spend readings are incomplete"}.` }];
    }
    return [{ text: `Khipu doesn't read ${label}'s plan, so there is no pace.` }];
  }

  const reachedShare = reachedShareCap(a);
  if (reachedShare) {
    return [{ text: `At your ${windowAdj(reachedShare.window)} ${Math.round(reachedShare.pct)}% cap.` }];
  }

  const crossing = earliestCrossing(a, now);
  if (crossing) {
    const parts: PaceSayPart[] = [];
    if (crossing.target === "share") {
      parts.push({ text: `Reaches your ${windowAdj(crossing.window)} ${Math.round(crossing.pct ?? 0)}% at ` });
      parts.push({ text: crossing.when, bold: true });
      parts.push({
        text: `${crossing.beforeResetS != null && crossing.beforeResetS > 0 ? `, ${shortSpan(crossing.beforeResetS)} before that window resets.` : "."}`,
      });
    } else if (crossing.target === "day") {
      parts.push({ text: `Reaches ${Math.round(crossing.pct ?? 0)}% a day at ` });
      parts.push({ text: crossing.when, bold: true });
      parts.push({ text: "." });
    } else {
      parts.push({ text: "At this rate it runs out " });
      parts.push({ text: crossing.when, bold: true });
      parts.push({
        text: `${crossing.beforeResetS != null && crossing.beforeResetS > 0 ? `, ${shortSpan(crossing.beforeResetS)} before the reset.` : "."}`,
      });
    }
    // The mock adds where the plan itself lands when a cap is what comes
    // first: the window's own forecast, from the same pace object.
    if (crossing.target !== "plan") {
      const plan = paces.find((p) => p.plan?.verdict === "outpacing" && p.plan.runs_out_at && p.plan.runs_out_at !== crossing.at);
      if (plan?.plan?.runs_out_at) {
        const adj = windowAdj(plan.window);
        parts.push({ text: ` At the ${adj === "week" ? "week's" : adj} rate it would run out ${whenLocal(plan.plan.runs_out_at, now)}` });
        parts.push({ text: `${plan.plan.before_reset_s != null && plan.plan.before_reset_s > 0 ? `, ${shortSpan(plan.plan.before_reset_s)} before the reset.` : "."}` });
      }
    }
    return parts;
  }

  const target = (a.cap_statuses ?? []).find((s) => s.kind === "pace_target");
  if (target?.verdict === "ahead" && target.used_pct != null && target.allowance_pct != null) {
    const week = paceOf(a, "weekly");
    const planVerdict = week.plan?.verdict ?? "unknown";
    const lead = planVerdict === "on_pace"
      ? "On pace for the plan limit"
      : planVerdict === "underpacing" ? "Under the plan limit at this rate" : "Within the plan limit at this rate";
    return [{ text: `${lead}, but ${num1(Math.max(0, target.used_pct - target.allowance_pct))} points ahead of the even pace you set.` }];
  }
  if (target?.verdict === "recovering") {
    return [{ text: `Back inside your even pace to ${Math.round(target.pct ?? 100)}% after being ahead.` }];
  }
  const unused = paces.find((p) => p.window === "weekly" && p.plan?.verdict === "underpacing" && p.plan.unused_at_reset_pct != null)
    ?? paces.find((p) => p.plan?.verdict === "underpacing" && p.plan.unused_at_reset_pct != null);
  if (unused?.plan?.unused_at_reset_pct != null) {
    return [{ text: `About ${Math.round(unused.plan.unused_at_reset_pct)} points of the ${windowAdj(unused.window)} will go unused at the reset at this rate.` }];
  }
  if (paces.some((p) => p.plan?.verdict === "on_pace")) return [{ text: "On pace for the plan limit." }];
  return [{ text: "Nothing Khipu has read is heading for a limit." }];
}

/** The one-line words for an account the group keeps closed. */
function lineOf(a: RouteAccount, now: Date, advice: PaceAdvice | null): { week: string; advice: string } {
  const classification = accountClassification(a);
  const adviceWords = advice
    ? classification === "limited" || classification === "paused" || classification === "down" || advice.word === "Closed"
      ? "Closed to all"
      : advice.word === "Not for jobs"
        ? "Not for jobs"
        : `${advice.word === "Hold back" ? "Jobs: hold back" : "Jobs: go"}${advice.evid.startsWith("evidence unknown") ? ", unsure" : ""}`
    : "";
  if (classification === "limited") return { week: a.until ? `Back ${whenLocal(a.until, now)}` : "At its limit", advice: adviceWords };
  if (classification === "paused") return { week: "Paused by you", advice: adviceWords };
  if (classification === "down") return { week: "Down", advice: adviceWords };
  const paces = a.pace ?? [];
  const spend = (a.cap_statuses ?? []).find((s) => s.kind === "spend");
  if (spend?.verdict === "unknown") return { week: "Spend can't be told", advice: adviceWords };
  if (!paces.length) return { week: "No pace: plan not read", advice: adviceWords };
  if (paces.every((p) => readingUnknown(p))) return { week: "Can't tell yet", advice: adviceWords };
  const reachedShare = reachedShareCap(a);
  if (reachedShare) return { week: `At your ${Math.round(reachedShare.pct)}% cap`, advice: adviceWords };
  const crossing = earliestCrossing(a, now);
  if (crossing) {
    const what = crossing.target === "plan"
      ? "Runs out"
      : crossing.target === "day"
        ? `Reaches ${Math.round(crossing.pct ?? 0)}% a day`
        : `Reaches ${Math.round(crossing.pct ?? 0)}%`;
    return { week: `${what} ${crossing.when}`, advice: adviceWords };
  }
  const target = (a.cap_statuses ?? []).find((s) => s.kind === "pace_target");
  if (target?.verdict === "ahead" && target.used_pct != null && target.allowance_pct != null) {
    return { week: `${num1(Math.max(0, target.used_pct - target.allowance_pct))} points ahead of your even pace`, advice: adviceWords };
  }
  const unused = paces.find((p) => p.window === "weekly" && p.plan?.verdict === "underpacing" && p.plan.unused_at_reset_pct != null)
    ?? paces.find((p) => p.plan?.verdict === "underpacing" && p.plan.unused_at_reset_pct != null);
  if (unused?.plan?.unused_at_reset_pct != null) {
    return { week: `About ${Math.round(unused.plan.unused_at_reset_pct)} points unused at the reset`, advice: adviceWords };
  }
  return { week: "On pace", advice: adviceWords };
}

/** When the allowance reaches the used figure: the mock's "clears when"
 *  sentence, from the cap's own window and pace target. */
function clearsAt(s: CapStatus): string | null {
  if (s.used_pct == null || s.allowance_pct == null || s.pct == null || !s.window) return null;
  const windowHours = s.window === "five_hour" ? 5 : s.window === "weekly" ? 7 * 24 : null;
  // Only a target at the whole limit clears without moving its goalposts;
  // below 100 a target is an even pace, and being ahead of it is not a gap
  // that usage alone closes.
  if (!windowHours || s.pct < 100) return null;
  const reaches = s.used_pct + 1;
  const hours = ((reaches - s.allowance_pct) * windowHours) / s.pct;
  if (!Number.isFinite(hours) || hours <= 0 || hours > 7 * 24) return null;
  return `Clears when the allowance reaches ${num1(reaches)}%, about ${shortSpan(hours * 3600)} from now if no more is used.`;
}

/** The advice's reason in plain words: the CLI's "kind verdict" pairs or
 *  state, then the one figure that makes it actionable. */
function adviceWhy(a: RouteAccount, advice: NonNullable<RouteAccount["advice"]>, now: Date): string {
  const reason = (advice.reason ?? "").replace(/\.$/, "");
  const status = (kind: string) => (a.cap_statuses ?? []).find((s) => s.kind === kind);
  const target = status("pace_target");
  const parts = reason.split("; ").filter(Boolean).map((part) => {
    switch (part) {
      case "held":
        return "a hold is on this account";
      case "paused by the owner":
        return "paused by you";
      case "limited":
        return a.until ? `at its plan limit until ${whenLocal(a.until, now)}` : "at its plan limit";
      case "down":
        return "down";
      case "plan limit reached":
        return "at its plan limit";
      case "pace_target ahead": {
        const to = target?.pct != null ? Math.round(target.pct) : 100;
        const delta = target?.used_pct != null && target.allowance_pct != null
          ? `${num1(Math.max(0, target.used_pct - target.allowance_pct))} points ahead of`
          : "ahead of";
        return to >= 100 ? `${delta} even plus ${num1(target?.slack ?? 0)}` : `${delta} your even pace to ${to}%`;
      }
      case "pace_target recovering": {
        const to = target?.pct != null ? Math.round(target.pct) : 100;
        return `back inside your even pace to ${to}%`;
      }
      case "per_day over":
        return `over your ${Math.round(status("per_day")?.pct ?? 0)}% a day`;
      case "per_day near":
        return `near its ${Math.round(status("per_day")?.pct ?? 0)}% a day`;
      case "concurrency full": {
        const conc = status("concurrency");
        return `${conc?.jobs ?? "All"} of ${conc?.cap_n ?? "its"} jobs running`;
      }
      case "share cap_reached": {
        const reachedPace = (a.pace ?? []).find((p) => p.share_cap?.verdict === "cap_reached");
        const shares = (a.cap_statuses ?? []).filter((s) => s.kind === "share");
        const share = shares.find((s) => s.verdict === "cap_reached" && (!reachedPace || s.window === reachedPace.window))
          ?? shares.find((s) => reachedPace != null && s.window === reachedPace.window);
        const pct = share?.pct ?? reachedPace?.share_cap?.pct ?? 0;
        return `at your ${windowAdj(share?.window ?? reachedPace?.window)} ${Math.round(pct)}%`;
      }
      case "spend over":
      case "spend reached": {
        const spend = status("spend");
        return `${part === "spend over" ? "over" : "at"} your ${spend?.cap_usd != null ? usdWords(spend.cap_usd) : "spend"} cap`;
      }
      case "a cap crosses within the hour at the trailing average": {
        const crossing = earliestCrossing(a, now);
        if (!crossing) return "a cap crosses within the hour at the trailing average";
        return `${a.label} reaches ${crossing.target === "plan" ? "its plan limit" : `${Math.round(crossing.pct ?? 0)}%`} in ${shortSpan(crossing.hours * 3600)}`;
      }
      default:
        return part;
    }
  });

  if (advice.verdict === "not_eligible") {
    return reason ? `${capitalise(reason)}.` : "The provider's facts do not allow this work for this account.";
  }
  if (advice.verdict === "closed") {
    return `${capitalise(parts.join("; "))}.`;
  }
  if (advice.verdict === "hold_back") {
    // The CLI's hold reason names the caps that hold; a daily cap that is
    // near (but not over) is still the context the mock shows beside it.
    const day = status("per_day");
    if (day?.verdict === "near" && !parts.some((p) => p.startsWith("near its"))) {
      parts.push(`near its ${Math.round(day.pct ?? 0)}% a day`);
    }
    const text = `${capitalise(parts.join("; ") || "a cap holds jobs back")}.`;
    const clear = target?.verdict === "ahead" ? clearsAt(target) : null;
    return clear ? `${text} ${clear}` : text;
  }
  // go
  if (advice.evidence === "unknown") {
    return "Khipu can't tell, so it says go with the evidence marked unknown.";
  }
  const eligibility = parts.length === 0
    || parts[0] === "provider facts allow this work"
    || parts[0] === "no provider fact restricts this account";
  if (!eligibility) return `${capitalise(parts.join("; "))}.`;
  const crossing = earliestCrossing(a, now);
  if (crossing) {
    const what = crossing.target === "plan"
      ? `Runs out in ${shortSpan(crossing.hours * 3600)}`
      : `Reaches ${Math.round(crossing.pct ?? 0)}% in ${shortSpan(crossing.hours * 3600)}`;
    return `${what}. Khipu says hold back when a cap is less than an hour away.`;
  }
  return "Room to spare.";
}

/** The advice row: the verdict word, why in plain words, and the evidence. */
function adviceOf(a: RouteAccount, now: Date): PaceAdvice | null {
  const advice = a.advice;
  if (!advice) return null;
  const verdict = advice.verdict ?? "go";
  const word = verdict === "go" ? "Go" : verdict === "hold_back" ? "Hold back" : verdict === "closed" ? "Closed" : "Not for jobs";
  const tone = verdict === "go" ? "go" : verdict === "hold_back" ? "hold" : "closed";
  const evidence = advice.evidence ?? "unknown";
  const conc = (a.cap_statuses ?? []).find((s) => s.kind === "concurrency" && s.jobs != null);
  const evid = evidence === "fresh"
    ? !(a.pace ?? []).length && conc ? "running count fresh" : "readings fresh"
    : evidence === "stale"
      ? "evidence stale"
      : `evidence unknown: ${evidenceCause(a)}`;
  return { word, tone, why: adviceWhy(a, advice, now), evid };
}

/** Whether the group opens the account at first look. The approved mock
 *  opens the one account that needs attention (its plan limit is outpacing
 *  or a cap is under 6 h away) and keeps every other account to one line,
 *  even one ahead of an even-pace cap or outpacing a share cap. The README's
 *  Q3 also names an owner even-pace lead; the mock is the screen, so Codex
 *  stays closed there. */
function openByDefault(a: RouteAccount, now: Date): boolean {
  const classification = accountClassification(a);
  if (classification === "limited" || classification === "paused" || classification === "down") return false;
  if (reachedShareCap(a)) return true;
  const crossing = earliestCrossing(a, now);
  if (crossing && crossing.hours <= 6) return true;
  return planOutpacing(a);
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** The whole group, pure: one row per account with pace or caps, the closed
 *  line, the tags, and the crossings that need the owner inside 6 h. */
export function paceModel(view: RouteView, plans: UsageReport | null | undefined, now: Date): PaceModel {
  const rows: PaceRow[] = [];
  for (const a of view.accounts ?? []) {
    if (!(a.pace ?? []).length && !(a.cap_statuses ?? []).length) continue;
    const planRow = (plans?.accounts ?? []).find((p) => p.key === a.key);
    const advice = adviceOf(a, now);
    rows.push({
      key: a.key,
      label: a.label,
      planLabel: planLabel(planRow?.plan),
      tag: accountTag(a),
      say: sayOf(a, now),
      windows: (a.pace ?? []).map((p) => windowRow(p, a.label, now)),
      caps: (a.cap_statuses ?? []).map((s) => ({ key: `${s.kind}:${s.window ?? ""}:${s.revision ?? ""}`, name: capName(s), ...capState(a, s, now) })),
      advice,
      open: openByDefault(a, now),
      planOutpacing: planOutpacing(a),
      line: lineOf(a, now, advice),
      crossing: earliestCrossing(a, now),
    });
  }

  const everyone = new Map((view.accounts ?? []).map((a) => [a.key, a]));
  const outpacing = rows.filter((r) => r.planOutpacing);
  const closed = rows.filter((r) => {
    const a = everyone.get(r.key);
    if (!a) return false;
    return ["limited", "paused", "down"].includes(accountClassification(a));
  });
  const unknown = rows.filter((r) => r.tag.text === "Can't tell yet");
  const holdBack = rows.filter((r) => r.advice?.word === "Hold back");
  const soon = rows
    .filter((r) => r.crossing && r.crossing.hours > 0 && r.crossing.hours <= 6)
    .sort((x, y) => (x.crossing?.hours ?? 0) - (y.crossing?.hours ?? 0));

  // The closed line: the first crossing, then who is closed, then the count
  // Khipu can't tell; a calm group says so in one line.
  const parts: string[] = [];
  const first = rows
    .filter((r) => r.crossing)
    .sort((x, y) => (x.crossing?.hours ?? 0) - (y.crossing?.hours ?? 0))[0];
  if (first?.crossing) {
    parts.push(first.crossing.target === "plan"
      ? `${first.label} runs out at ${first.crossing.when}`
      : `${first.label} reaches a cap at ${first.crossing.when}`);
  } else if (outpacing.length === 1) parts.push(`${outpacing[0].label} is outpacing`);
  else if (outpacing.length > 1) parts.push(`${outpacing.length} accounts outpacing`);
  if (closed.length) parts.push(`${closed.map((r) => r.label).join(", ")} closed`);
  if (unknown.length) parts.push(`${plural(unknown.length, "account", "accounts")} can't be told yet`);
  const line = parts.length
    ? parts.join("; ")
    : rows.length ? `${plural(rows.length, "account", "accounts")}, none outpacing` : "No accounts yet";

  const tags: PaceTag[] = [];
  if (outpacing.length) {
    tags.push({ text: outpacing.length === 1 ? `${outpacing[0].label} outpacing` : `${outpacing.length} outpacing`, tone: "warn" });
  }
  if (holdBack.length) tags.push({ text: `${holdBack.length} hold back for jobs`, tone: "warn" });
  if (unknown.length) tags.push({ text: `${unknown.length} can't be told`, tone: "neutral" });

  const need = soon.map((r) => {
    const c = r.crossing!;
    const target = c.target === "plan"
      ? "its plan limit"
      : c.target === "day" ? `${Math.round(c.pct ?? 0)}% a day` : `your ${windowAdj(c.window)} ${Math.round(c.pct ?? 0)}%`;
    const needs = c.target === "plan"
      ? `${r.label} runs out at ${c.when}`
      : `${r.label} reaches ${target} at ${c.when}`;
    return { key: `pace:${r.key}`, label: r.label, needs, show: `${r.label}'s pace`, item: `pace:${r.key}` };
  });

  return { rows, line, tags, crossings: need, empty: !rows.length };
}
