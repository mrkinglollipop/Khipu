/* The two detail views that open from Home, as plain data the components
   print. A Plan detail answers "how much is left and how does Khipu know";
   a Routing detail answers "can this account take work and on whose word".
   They share a source account but not a layout (owner decision D2). Both read
   only fields the CLI JSON has today; a section whose field does not exist
   yet says so in `gap` instead of guessing. */

import type { CapStatus, Pace, RouteAccount, RouteChange, RouteException, RouteHold, RoutePriorityEntry, RouteView } from "./routing";
import { accountSource, accountState, rateWords, whenLocal, windowWords } from "./routing";
import type { Tone } from "./routing";
import { fmtPct, hasReset, liveWindows, meterTone, planStatus, resetPhrase, roundPct, tightestWindow, usageErrorPhrase, windowLabel } from "./PlansCard";
import type { UsageAccount, UsageWindow } from "./PlansCard";
import { almostUsedUp, longSpan, planWord } from "./humanWords";
import { fmtAgoSince } from "./time";

const CLI_NAME: Record<string, string> = { claude: "Claude", codex: "Codex", grok: "Grok", muse: "Muse", glm: "GLM", deepseek: "DeepSeek", kimi: "Kimi" };

function cli(harness: string | undefined): string {
  return CLI_NAME[harness ?? ""] ?? (harness ? harness.charAt(0).toUpperCase() + harness.slice(1) : "its app");
}

function ord(n: number): string {
  const v = n % 100;
  const suffix = v >= 11 && v <= 13 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" } as Record<number, string>)[n % 10] ?? "th";
  return `${n}${suffix}`;
}

/** "Mon, Oct 26, 4:30 PM" style date for a long-range end; the row's own
 *  `whenLocal` for anything within a week. */
function dateTime(iso: string | null | undefined, now: Date): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const days = Math.abs(d.getTime() - now.getTime()) / 86_400_000;
  if (days < 6) return whenLocal(iso, now);
  return d.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function dayOnly(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString([], { month: "short", day: "numeric" });
}

// ---------------------------------------------------------------------------
// Plan detail
// ---------------------------------------------------------------------------

export type PlanWindowRow = { name: string; left: number; used: number; tone: "err" | "warn" | null; resets: string; stale: boolean };

export type PlanDetail = {
  name: string;
  plan: string;
  state: { text: string; tone: Tone };
  windows: PlanWindowRow[];
  /** Short lines beside the meters: credits and why a window is shown. */
  credits: string[];
  activity: string[];
  knows: { k: string; v: string }[];
};

function creditLines(credits: UsageAccount["credits"]): string[] {
  const out: string[] = [];
  for (const [key, value] of Object.entries(credits ?? {})) {
    if (typeof value !== "string" && typeof value !== "number") continue;
    const label = key.replace(/_/g, " ");
    out.push(`${label.charAt(0).toUpperCase()}${label.slice(1)}: ${value}`);
  }
  return out;
}

/** The pace object the activity talks about: the week when Khipu has one. */
function mainPace(a: RouteAccount | null | undefined): Pace | null {
  const paces = a?.pace ?? [];
  return paces.find((p) => p.window === "weekly") ?? paces[paces.length - 1] ?? null;
}

/** What the last hours of use mean, in sentences. Every number is Khipu's
 *  arithmetic; the words only say it. */
export function activityLines(a: RouteAccount | null | undefined, fact: { readable?: boolean; left_pct?: number; pace_pct_per_hour?: number; window?: string } | undefined, now: Date): string[] {
  const out: string[] = [];
  const p = mainPace(a);
  const span = p?.trailing_h === 1 ? "hour" : "day";
  if (p && typeof p.rate_pct_per_h === "number" && !p.gappy) {
    out.push(p.rate_pct_per_h <= 0 ? `No use in the last ${span}.` : `Using about ${rateWords(p.rate_pct_per_h)}% an hour over the last ${span}.`);
  } else if (p) {
    out.push(p.why ? `Not enough readings to tell the rate yet: ${p.why.charAt(0).toLowerCase()}${p.why.slice(1).replace(/\.$/, "")}.` : `Not enough readings in the last ${span} to tell the rate yet.`);
  }
  if (fact?.readable && typeof fact.left_pct === "number" && fact.left_pct > 0 && typeof fact.pace_pct_per_hour === "number") {
    out.push(`Using the ${fact.left_pct}% left before the reset would take about ${rateWords(fact.pace_pct_per_hour)}% an hour.`);
  }
  const plan = p?.plan;
  if (plan?.verdict === "limit_reached") out.push("The limit is reached.");
  else if (plan?.verdict === "outpacing" && plan.runs_out_at) {
    const early = plan.before_reset_s ? `, about ${longSpan(plan.before_reset_s)} before it resets` : "";
    out.push(`At this rate the ${windowWords(p?.window)} runs out ${dateTime(plan.runs_out_at, now)}${early}.`);
  } else if (plan?.verdict === "underpacing" && typeof plan.unused_at_reset_pct === "number" && plan.unused_at_reset_pct >= 1) {
    out.push(`About ${Math.round(plan.unused_at_reset_pct)} points would go unused at the reset.`);
  }
  const cap = p?.share_cap;
  if (cap && (cap.verdict === "outpacing" || cap.verdict === "cap_reached") && cap.runs_out_at) {
    out.push(`It reaches your ${cap.pct != null ? `${Math.round(cap.pct)}% ` : ""}cap ${dateTime(cap.runs_out_at, now)}.`);
  }
  if (out.length === 0) out.push("Nothing to forecast yet.");
  return out;
}

function planReadLines(row: UsageAccount | undefined, a: RouteAccount | null | undefined, now: Date): { k: string; v: string }[] {
  const out: { k: string; v: string }[] = [];
  if (!row) return [{ k: "Plan read", v: "Khipu has not read this plan yet." }];
  const name = cli(row.harness);
  const read = fmtAgoSince(row.probed_at || row.observed_at, now.getTime());
  const took = typeof row.ms === "number" && row.ms > 0 ? ` (took ${(row.ms / 1000).toFixed(1)} s)` : "";
  if (row.source === "turn" || row.observed_at) {
    out.push({ k: "Plan read", v: `From ${name}'s last reply${read ? `, ${read}` : ""}. ${name} can only be read by using a turn.` });
  } else if (row.ok === false && !(row.windows ?? []).length) {
    out.push({ k: "Plan read", v: `Not read: ${usageErrorPhrase(row.error)}.` });
  } else {
    out.push({ k: "Plan read", v: `${name}'s own plan check${read ? `, ${read}` : ""}${took}.` });
  }
  const good = fmtAgoSince(row.last_good_at, now.getTime());
  if (good && row.source !== "turn") out.push({ k: "Last good read", v: good });
  const code = row.ok === false ? row.error : row.live_error;
  if (code && code !== "pending") {
    out.push({ k: "Latest try", v: `Failed: ${usageErrorPhrase(code)}. The figures above are from ${good ?? "an earlier read"}.` });
  }
  const witnessAt = a?.witness?.at;
  const probed = row.probed_at;
  if (witnessAt && probed && Math.abs(new Date(probed).getTime() - new Date(witnessAt).getTime()) > 5 * 60_000) {
    out.push({ k: "Routing uses", v: `The ${whenLocal(witnessAt, now)} reading. Routing has not taken the newer plan read yet.` });
  }
  const status = planStatus(row, now);
  if (status.say && !out.some((o) => o.v.includes(status.say as string))) out.push({ k: "Status", v: status.say });
  return out;
}

/** "Five hours", "Week", "Fable week": a window's name as a row heading. */
function windowHeading(w: UsageWindow): string {
  const label = windowLabel(w);
  const text = label === "five-hour" ? "five hours" : label;
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}`;
}

export function planDetail(row: UsageAccount, view: RouteView | null | undefined, now: Date): PlanDetail {
  const a = (view?.accounts ?? []).find((x) => x.key === row.key) ?? null;
  const fact = a ? view?.plan_facts?.[a.key] : undefined;
  const tight = tightestWindow(row, now);
  const windows: PlanWindowRow[] = (row.windows ?? [])
    .filter((w) => typeof w.used_pct === "number")
    .map((w: UsageWindow) => {
      const used = roundPct(w.used_pct as number);
      const reset = hasReset(w, now);
      return {
        name: windowHeading(w),
        used,
        left: roundPct(Math.max(0, 100 - used)),
        tone: reset ? null : meterTone(used),
        resets: resetPhrase(w.resets_at, now) ?? "no reset time reported",
        stale: reset,
      };
    });
  const credits = creditLines(row.credits);
  // The Home row shows one window; say why when it is not the week.
  const live = liveWindows(row, now);
  if (tight && live.length > 1 && windowLabel(tight) !== "week") {
    credits.unshift(`Home shows the ${windowLabel(tight)} window because it is the closest to its limit.`);
  }
  return {
    name: row.account || row.harness || "Account",
    plan: planWord(row.plan),
    state: a ? accountState(a, now) : { text: "Not in the routing record", tone: null },
    windows,
    credits,
    activity: activityLines(a, fact, now),
    knows: planReadLines(row, a, now),
  };
}

/** The sentence a Plans row says when its plan is nearly gone: the count and
 *  the reset, so a red bar is never the only signal. */
export function nearlyGoneWords(row: UsageAccount, now: Date): string | null {
  const w = tightestWindow(row, now);
  if (!w || typeof w.used_pct !== "number") return null;
  const left = roundPct(Math.max(0, 100 - w.used_pct));
  if (!almostUsedUp(left)) return null;
  const reset = w.resets_at ? whenLocal(w.resets_at, now) : "";
  return `Almost used up: ${fmtPct(left)} left${reset ? ` until ${reset}` : ""}`;
}

// ---------------------------------------------------------------------------
// Routing detail
// ---------------------------------------------------------------------------

export type CapRow = { title: string; now: string | null; ends: string };
export type ExceptionRow = { text: string; ends: string; by: string | null };
export type AdviceRow = { k: string; verdict: string; why: string | null };
export type HistoryRow = { at: string; text: string };

export type RoutingDetail = {
  name: string;
  state: { text: string; tone: Tone };
  source: string;
  disagreement: string | null;
  caps: CapRow[];
  exceptions: ExceptionRow[];
  holds: string[];
  advice: AdviceRow[];
  rank: string | null;
  role: string | null;
  history: HistoryRow[];
  limits: HistoryRow[];
  /** The oldest change the record still keeps, for "nothing since" lines. */
  historyFrom: string | null;
  gaps: string[];
  /** The account has an entry but the record stores no exception list. */
  exceptionsUnknown: boolean;
};

function purposeWord(purpose: string | undefined): string {
  return purpose === "check" ? "Checks" : "Jobs";
}

/** One owner cap as a title, its figures now, and when it ends. */
export function capRow(c: CapStatus, windowResets: (w: string | null | undefined) => string | null, now: Date): CapRow {
  let title: string;
  let figures: string | null = null;
  const who = purposeWord(c.purpose);
  switch (c.kind) {
    case "pace_target":
      title = c.slack && c.slack > 0 ? `${who}: up to ${c.slack} points over an even pace` : `${who}: keep at or under an even pace`;
      if (typeof c.allowance_pct === "number" && typeof c.used_pct === "number") {
        figures = `An even pace allows ${fmtPct(c.allowance_pct)} by now; ${fmtPct(c.used_pct)} is used.`;
      }
      break;
    case "share":
      title = `At most ${c.pct ?? "?"}% of the ${windowWords(c.window ?? undefined)}`;
      if (typeof c.used_pct === "number") figures = `${fmtPct(c.used_pct)} used now.`;
      break;
    case "per_day":
      title = `At most ${c.pct ?? "?"}% in a rolling day`;
      if (typeof c.used_pct === "number") figures = `${fmtPct(c.used_pct)} used in the last day.`;
      break;
    case "spend":
      title = `At most $${c.cap_usd ?? "?"} per rolling ${c.days ?? 7} days`;
      if (typeof c.usd === "number") figures = `$${c.usd.toFixed(2)} spent${typeof c.cap_usd === "number" && c.cap_usd > 0 ? ` (${Math.round((c.usd / c.cap_usd) * 100)}%)` : ""}.`;
      break;
    case "concurrency":
      title = `${who}: at most ${c.cap_n ?? "?"} at a time`;
      break;
    default:
      title = `${who}: a cap Khipu does not describe yet`;
  }
  const resets = c.kind === "share" || c.kind === "per_day" ? windowResets(c.window) : null;
  const since = c.confirmed_at ? ` since ${dayOnly(c.confirmed_at)}` : "";
  const ends = c.until
    ? `Ends ${dateTime(c.until, now)}`
    : resets
      ? `Resets with the ${windowWords(c.window ?? undefined)}, ${dateTime(resets, now)}`
      : `Yours${since} · no end`;
  return { title, now: figures, ends };
}

const VERDICT_WORD: Record<string, string> = {
  go: "Fine to use",
  hold_back: "Hold back",
  closed: "Closed",
  not_eligible: "Not used for this",
};

/** Cleans the CLI's cap sentences of the shorthand the audit named. */
export function cleanWhy(text: string): string {
  return text
    .replace(/\s*plus 0\b/g, "")
    .replace(/(\d+) d (\d+) h\b/g, (_m, d: string, h: string) => `${d} ${Number(d) === 1 ? "day" : "days"} ${h} ${Number(h) === 1 ? "hour" : "hours"}`)
    .replace(/(\d+) d\b/g, (_m, d: string) => `${d} ${Number(d) === 1 ? "day" : "days"}`)
    .replace(/(\d+) h\b/g, (_m, h: string) => `${h} ${Number(h) === 1 ? "hour" : "hours"}`)
    .replace(/points ahead of even\b/g, "points ahead of an even pace")
    .trim();
}

function adviceRows(a: RouteAccount): AdviceRow[] {
  const rows: AdviceRow[] = [];
  for (const [k, adv] of [["New jobs", a.advice], ["Checks", a.check_advice], ["Your main conversations", a.lead_advice]] as const) {
    if (!adv?.verdict) continue;
    const why = (adv.caps ?? []).map((c) => c.why).filter((w): w is string => !!w && adv.verdict === "hold_back")[0] ?? null;
    rows.push({ k, verdict: VERDICT_WORD[adv.verdict] ?? adv.verdict, why: why ? cleanWhy(why) : null });
  }
  return rows;
}

function changeText(item: { now?: Record<string, unknown> | null }, now: Date): string | null {
  const st = item.now?.state;
  const until = typeof item.now?.until === "string" ? (item.now.until as string) : null;
  if (st === "limited") return `Limit reached${until ? `, back ${dateTime(until, now)}` : ""}`;
  if (st === "open") return "Has room again";
  if (st === "paused") return `Paused by you${until ? ` until ${dateTime(until, now)}` : ""}`;
  if (st === "probably_open") return "Probably open again";
  if (st === "down") return "Down";
  return null;
}

function accountChanges(view: RouteView, key: string, now: Date): { rows: HistoryRow[]; limits: HistoryRow[]; upkeep: number } {
  const entity = `account:${key}`;
  const rows: HistoryRow[] = [];
  const limits: HistoryRow[] = [];
  let upkeep = 0;
  const changes: RouteChange[] = [...(view.changes ?? [])].reverse();
  for (const ch of changes) {
    const item = (ch.items ?? []).find((i) => i.entity === entity);
    if (!item) continue;
    const text = changeText(item, now);
    if (!text) {
      upkeep += 1;
      continue;
    }
    const by = item.by ? ` (${item.by})` : "";
    const row = { at: dateTime(ch.at, now), text: `${text}${by}.` };
    rows.push(row);
    if (item.now?.state === "limited") limits.push(row);
  }
  return { rows: rows.slice(0, 10), limits: limits.slice(0, 10), upkeep };
}

function holdLine(h: RouteHold, now: Date): string {
  const label = h.scope_label.charAt(0).toUpperCase() + h.scope_label.slice(1);
  return h.until ? `${label}, until ${dateTime(h.until, now)}` : label;
}

export function routingDetail(a: RouteAccount, view: RouteView, plans: UsageAccount | undefined, now: Date): RoutingDetail {
  const state = accountState(a, now);
  const resetOf = (window: string | null | undefined) => {
    const p = (a.pace ?? []).find((x) => x.window === window);
    if (p?.cycle_resets_at) return p.cycle_resets_at;
    const w = (plans?.windows ?? []).find((x) => x.name === (window === "weekly" ? "weekly" : window));
    return w?.resets_at ?? null;
  };
  const caps = (a.cap_statuses ?? []).map((c) => capRow(c, resetOf, now));
  const entries = view.priority?.entries ?? [];
  const idx = entries.findIndex((e) => e.account === a.key);
  const entry: RoutePriorityEntry | null = idx >= 0 ? entries[idx] : null;
  const exceptions: ExceptionRow[] = (entry?.exceptions ?? []).map((e: RouteException) => ({
    text: e.text,
    ends: e.until ? `Ends ${dateTime(e.until, now)}` : e.ends_when ? `Ends ${e.ends_when}` : "No end set",
    by: e.at ? `you said so ${dayOnly(e.at)}` : null,
  }));
  const holds = (view.holds ?? []).filter((h) => h.scope?.account === a.key).map((h) => holdLine(h, now));
  const hist = accountChanges(view, a.key, now);
  const oldest = (view.changes ?? [])[0]?.at ?? null;
  const gaps: string[] = [];
  // Exceptions only exist inside an entry's words. No entry: there are none.
  // An entry with no stored list: they may sit in its words, so say that
  // instead of "None."
  const exceptionsUnknown = !!entry && !Array.isArray(entry.exceptions);
  if (exceptionsUnknown) gaps.push("Exceptions with end times show here once Khipu stores them as rows; until then they are only inside your words below.");
  if (caps.length && (a.cap_statuses ?? []).some((c) => !c.until && c.kind !== "share" && c.kind !== "per_day")) {
    gaps.push("A cap has no end of its own yet: it lasts until you remove it.");
  }
  if (hist.upkeep > 1) hist.rows.push({ at: "Recently", text: `Tracking settings changed ${hist.upkeep} times (Khipu upkeep).` });
  return {
    name: a.label,
    state,
    source: accountSource(a, now, null),
    disagreement: null,
    caps,
    exceptions,
    holds,
    advice: adviceRows(a),
    rank: idx >= 0 ? `${ord(idx + 1)} of ${entries.length}` : null,
    role: entry?.role?.trim() || null,
    history: hist.rows,
    limits: hist.limits,
    historyFrom: oldest ? dayOnly(oldest) : null,
    gaps,
    exceptionsUnknown,
  };
}
