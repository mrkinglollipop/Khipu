/* Routing in the app (docs/plans/2026-10-14-routing-truth.md, phase 4): the
   routing record as `khipu route --json` prints it, read through one
   fixed-argv command (`khipu_route_view`), and the plain words the Home card,
   the health report's Routing row and the attention items use. Khipu reports
   which accounts are open; it never picks one, so nothing here ranks, picks
   or refuses anything. Every time is the viewer's own local time. The words
   follow the scope's table ("In the record" / "In the app"). */

import { useCallback, useEffect, useRef, useState } from "react";
import { callCli } from "./settingsCli";

export type RouteAuthor = { kind?: string; harness?: string; project?: string; session?: string; name?: string };

export type RouteWitness = {
  id?: string;
  source?: string;
  written_by?: RouteAuthor;
  /** The author in words (`author_words`), set by the CLI. */
  by?: string;
  at?: string | null;
  message_time?: boolean;
  cleared?: boolean;
  observed?: boolean;
  /** A limit reported with no time: it lasts `report_default_limit`. */
  until_default?: boolean;
};

export type RouteReport = RouteWitness & {
  state?: string;
  since?: string | null;
  until?: string | null;
};

export type RouteModel = {
  model: string;
  until?: string | null;
  basis?: string | null;
  source?: string;
  at?: string | null;
  /** The report behind this model's limit, and who wrote it in words. */
  id?: string;
  by?: string;
  disagreement?: RouteReport | null;
};

export type RouteAccount = {
  key: string;
  label: string;
  harness: string;
  state: string;
  until?: string | null;
  basis?: string | null;
  reason?: string | null;
  checkable?: boolean;
  witness?: RouteWitness | null;
  disagreement?: RouteReport | null;
  last_seen?: RouteReport | null;
  models?: RouteModel[];
  silent?: boolean;
};

export type RouteHold = {
  id: string;
  scope_label: string;
  reason: string;
  text?: string | null;
  until?: string | null;
  owner?: boolean;
  by?: string;
  at?: string | null;
  /** An owner hold older than the reminder setting (90 days by default). */
  remind?: boolean;
};

export type RouteRule = {
  n: number;
  text: string;
  seeded?: boolean;
  confirmed?: boolean;
  date?: string | null;
  /** Where the rule came from, in the owner's or the seed's words. */
  source?: string | null;
  via?: string | null;
  at?: string | null;
};

export type RouteRunnerFile = { error?: string | null; good_at?: string | null; since?: string | null };

export type RouteRunner = {
  name: string;
  path?: string;
  heartbeat_at?: string | null;
  /** Seconds between its heartbeats. */
  interval?: number | null;
  silent?: boolean;
  unreadable?: boolean;
  unreadable_since?: string | null;
  files?: Record<string, RouteRunnerFile>;
  lanes?: string[];
  unmatched_lanes?: string[];
};

export type RouteLane = { runner: string; lane: string; until?: string | null };

/** One changed entity in a version: what it says now and said before (its
 *  effective view; null when it ended or began) and who caused it. */
export type RouteChangeItem = {
  entity: string;
  now?: Record<string, unknown> | null;
  was?: Record<string, unknown> | null;
  source?: string | null;
  via?: string | null;
  /** The author in words, set by the CLI. */
  by?: string | null;
  /** A removed rule's number. */
  n?: number | null;
};

export type RouteChange = { version: number; at: string; entities: string[]; items?: RouteChangeItem[] };

export type RuleCheck = {
  rule: number;
  status: string;
  text: string;
  at?: string | null;
  /** The job runner it was judged on, and which of its files. */
  runner?: string;
  file?: "settings" | "events" | string;
};

export type RouteView = {
  record?: boolean;
  version?: number;
  checked_at?: string;
  accounts?: RouteAccount[];
  holds?: RouteHold[];
  rules?: RouteRule[];
  note?: { text: string; at?: string | null } | null;
  unmatched?: RouteLane[];
  sources?: {
    runners?: RouteRunner[];
    refused?: string[];
    table?: { path?: string; stamped_at?: string | null; changed_at?: string | null; error?: string | null } | null;
  } | null;
  /** The numbers of the rules Khipu has a check for (`routing_sources.watched`). */
  watched_rules?: number[];
  /** The old hook's off switches as the app's CLI sees them. */
  old_switches?: { env?: boolean; file?: boolean };
  changes?: RouteChange[];
  rule_checks?: RuleCheck[];
  /** Seconds; 0 means never. */
  warn_after?: { disagreement?: number; not_refreshed?: number };
  /** Seconds a limit reported without a time lasts. */
  report_default_limit?: number;
};

export type Tone = "ok" | "warn" | "err" | null;

const MINUTE_MS = 60_000;
const DAY_MS = 24 * 60 * MINUTE_MS;

function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

function clock(d: Date): string {
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/** "" today, "tomorrow", "yesterday", "Sat" within six days, else "Oct 21". */
function dayWords(d: Date, now: Date): string {
  const days = Math.round((startOfDay(d) - startOfDay(now)) / DAY_MS);
  if (days === 0) return "";
  if (days === 1) return "tomorrow";
  if (days === -1) return "yesterday";
  if (Math.abs(days) < 7) return d.toLocaleDateString([], { weekday: "short" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

/** "2:52 PM" today, else "Sat 8:00 PM" or "tomorrow 9:40 AM". */
export function whenLocal(iso: string | null | undefined, now: Date): string {
  const d = parse(iso);
  if (!d) return "";
  const day = dayWords(d, now);
  return day ? `${day} ${clock(d)}` : clock(d);
}

/** "at 2:52 PM" today, else "Sat 8:00 PM": a time ahead. */
export function aheadWords(iso: string | null | undefined, now: Date): string {
  const d = parse(iso);
  if (!d) return "";
  return dayWords(d, now) ? whenLocal(iso, now) : `at ${clock(d)}`;
}

/** "at 1:52 PM" today, else "yesterday at 1:52 PM" or "Mon at 8:00 PM":
 *  a time that has passed, as the end of a sentence. */
export function pastWords(iso: string | null | undefined, now: Date): string {
  const d = parse(iso);
  if (!d) return "";
  const day = dayWords(d, now);
  return day ? `${day} at ${clock(d)}` : `at ${clock(d)}`;
}

/** "just now", "4 min ago" within the hour, "at 11:13 AM" today, else
 *  "yesterday at 6:05 PM": when something was said. */
export function saidWhen(iso: string | null | undefined, now: Date): string {
  const d = parse(iso);
  if (!d) return "";
  const ago = now.getTime() - d.getTime();
  if (ago >= 0 && ago < MINUTE_MS) return "just now";
  if (ago >= 0 && ago < 60 * MINUTE_MS) return `${Math.round(ago / MINUTE_MS)} min ago`;
  const day = dayWords(d, now);
  return day ? `${day} at ${clock(d)}` : `at ${clock(d)}`;
}

/** "14 h ago", "3 days ago": how old a last sighting is. */
function ageWords(iso: string | null | undefined, now: Date): string {
  const d = parse(iso);
  if (!d) return "";
  const mins = Math.max(0, Math.round((now.getTime() - d.getTime()) / MINUTE_MS));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

/** "30 minutes", "2 hours". */
export function spanWords(seconds: number): string {
  const mins = Math.round(seconds / 60);
  if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"}`;
  const hours = Math.round(mins / 60);
  return `${hours} hour${hours === 1 ? "" : "s"}`;
}

export function capitalise(text: string): string {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

const CLI_NAMES: Record<string, string> = { claude: "Claude", codex: "Codex", grok: "Grok", muse: "Muse" };

function cliName(harness: string): string {
  return CLI_NAMES[harness] ?? harness;
}

function ahead(iso: string | null | undefined, now: Date): boolean {
  const d = parse(iso);
  return d != null && d.getTime() > now.getTime();
}

function modelLimits(a: RouteAccount, now: Date): string[] {
  return (a.models ?? [])
    .filter((m) => ahead(m.until, now))
    .map((m) =>
      m.basis === "estimated"
        ? `${m.model} limit reached, back about ${whenLocal(m.until, now)} (a guess)`
        : `${m.model} limit reached, back ${aheadWords(m.until, now)}`,
    );
}

function stateWord(state: string | undefined): string {
  if (state === "limited" || state === "down" || state === "paused" || state === "open") return state;
  return "in an unknown state";
}

/** The state of one account in the scope's words, and its dot's tone. */
export function accountState(a: RouteAccount, now: Date): { text: string; tone: Tone } {
  const models = modelLimits(a, now);
  let text: string;
  let tone: Tone = null;
  switch (a.state) {
    case "paused":
      text = a.until ? `Paused by you until ${whenLocal(a.until, now)}` : "Paused by you until you clear it";
      break;
    case "limited":
      tone = "warn";
      if (!a.until) text = "Limit reached";
      else if (a.basis === "estimated") text = `Limit reached, back about ${whenLocal(a.until, now)} (a guess)`;
      else text = `Limit reached, back ${aheadWords(a.until, now)}`;
      break;
    case "down":
      tone = "err";
      text = `Down: ${a.reason || "out of service"}`;
      break;
    case "open":
      if (models.length) return { text: `${models.join("; ")}; other models open`, tone: "warn" };
      return { text: "Open", tone: "ok" };
    case "probably_open":
      text =
        a.checkable === false
          ? `Its reset time passed ${pastWords(a.until, now)}; this account can't be checked without using a turn`
          : `Probably open: its limit was due to end ${pastWords(a.until, now)}, not checked yet`;
      break;
    default:
      if (a.silent) text = "Unknown: its only reporter stopped";
      else if (a.last_seen?.cleared) text = `Unknown: last cleared by you ${ageWords(a.last_seen.at, now)}`;
      else if (a.last_seen) text = `Unknown: last seen ${stateWord(a.last_seen.state)} ${ageWords(a.last_seen.at, now)}`;
      else text = "Unknown: no report yet";
  }
  if (models.length) text += `; ${models.join("; ")}`;
  return { text, tone };
}

/** Who said so, in words: "Read from Claude", "From Claude's own message,
 *  seen by your Project A job runner", "Reported by a Claude Code
 *  conversation in project-a". */
function witnessWords(w: RouteWitness, harness: string, state: string): string {
  const cli = cliName(harness);
  switch (w.source) {
    case "probe":
      if (w.observed) return `Read from ${cli}'s last turn`;
      return state === "down" ? "Khipu's plan check" : `Read from ${cli}`;
    case "check":
      return "Checked by Khipu";
    case "owner":
      return w.cleared ? "Cleared by you" : "Set by you";
    case "runner": {
      const name = w.written_by?.name;
      const runner = name ? `your ${name} job runner` : "a job runner";
      return w.message_time ? `From ${cli}'s own message, seen by ${runner}` : `Seen by ${runner}`;
    }
    default: {
      const by = w.by || "a conversation";
      if (w.message_time) return `From ${cli}'s own message, reported by ${by}`;
      return w.until_default ? `Reported without a time by ${by}` : `Reported by ${by}`;
    }
  }
}

/** The plan figure an open account's row adds: "58% of its week used". */
export type PlanFigure = { pct: number; window: string };

const WINDOW_WORDS: Record<string, string> = { five_hour: "five hours", weekly: "week", daily: "day" };

export function windowWords(name: string | undefined): string {
  return (name && WINDOW_WORDS[name]) || (name ?? "window").replace(/_/g, " ");
}

/** Who said so and when, plus what else the row should say. `reportDefault`
 *  is the record's `report_default_limit` (seconds), named when the limit
 *  shown came from a report without a time. */
export function accountSource(
  a: RouteAccount,
  now: Date,
  plan?: PlanFigure | null,
  reportDefault: number = 30 * 60,
): string {
  const parts: string[] = [];
  const w = a.state === "unknown" ? a.last_seen : a.witness;
  if (w) {
    const when = saidWhen(w.at, now);
    let said = `${witnessWords(w, a.harness, a.state)}${when ? ` ${when}` : ""}`;
    if (a.state === "limited" && w.until_default && w.source !== "runner" && !w.message_time) {
      said += `; Khipu keeps a report like that for ${spanWords(reportDefault)}`;
    }
    parts.push(said);
  }
  if (a.state === "limited" && a.checkable === false) parts.push("can't be checked without using a turn");
  if (a.state === "open" && a.witness?.source === "probe" && plan) {
    parts.push(`${Math.round(plan.pct)}% of its ${plan.window} used`);
  }
  return parts.join(" · ");
}

/** Which report shows over the other, when two sources disagree: the
 *  source of the one shown and whether its time is the CLI's own. */
function strongerWords(source: string | undefined, ownTime: boolean, harness: string): string {
  const cli = cliName(harness);
  if (source === "probe") return `Khipu's read of ${cli}`;
  if (source === "check") return "Khipu's check";
  if (source === "owner") return "Your own word";
  if (ownTime) return `${cli}'s own message`;
  return "The stronger report";
}

function weakerWords(d: RouteReport): string {
  if (d.source === "report") return "a conversation's report";
  if (d.source === "runner") return "a job runner's report";
  return "the other report";
}

function disagreementSentence(
  d: RouteReport,
  harness: string,
  subject: string,
  stronger: string,
  shown: string,
  now: Date,
): string {
  const by = d.by ? capitalise(d.by) : capitalise(witnessWords(d, harness, d.state ?? ""));
  return (
    `${by} reported ${subject} ${stateWord(d.state)} ${saidWhen(d.at, now)}. ` +
    `${stronger} outranks ${weakerWords(d)}, so ${shown} shows here; conversations are told both.`
  );
}

/** "A Claude Code conversation in project-b reported it open at 11:31 AM.
 *  Claude's own message outranks a conversation's report, so the limit
 *  shows here; conversations are told both." Null when nothing disagrees. */
export function disagreementWords(a: RouteAccount, now: Date): string | null {
  const d = a.disagreement;
  if (!d) return null;
  const shown = a.state === "limited" ? "the limit" : `"${stateWord(a.state)}"`;
  const w = a.witness;
  return disagreementSentence(d, a.harness, "it", strongerWords(w?.source, !!w?.message_time, a.harness), shown, now);
}

/** The same for each model whose own limit a report disagrees with: "A
 *  Claude Code conversation in project-c reported Opus open at 1:00 PM.
 *  Claude's own message outranks a conversation's report, so the Opus limit
 *  shows here; conversations are told both." Only models whose limit is
 *  shown (its end is ahead), as the attention item names them. */
export function modelDisagreementWords(a: RouteAccount, now: Date): string[] {
  return (a.models ?? [])
    .filter((m) => m.disagreement && ahead(m.until, now))
    .map((m) => {
      const stronger = strongerWords(m.source, m.basis === "cli" && m.source !== "probe", a.harness);
      return disagreementSentence(m.disagreement!, a.harness, m.model, stronger, `the ${m.model} limit`, now);
    });
}

/** A hold's end and author: "Ends tomorrow 9:40 AM · set by a Claude Code
 *  conversation in project-a". */
export function holdSource(h: RouteHold, now: Date): string {
  const end = h.until ? `Ends ${whenLocal(h.until, now)}` : "Until you clear it";
  const by = h.owner ? `set by you ${saidWhen(h.at, now)}`.trim() : `set by ${h.by || "a conversation"}`;
  return `${end} · ${by}`;
}

/** The reminder the owner gets on his own old hold (the CLI sets `remind`
 *  once it is older than the reminder setting, 90 days by default): "You
 *  set this 120 days ago. Is it still needed?" Null otherwise. */
export function holdReminder(h: RouteHold, now: Date): string | null {
  if (!h.remind) return null;
  const at = parse(h.at);
  const days = at ? Math.floor((now.getTime() - at.getTime()) / DAY_MS) : null;
  const when = days != null && days > 0 ? `${days} day${days === 1 ? "" : "s"} ago` : "a while ago";
  return `You set this ${when}. Is it still needed?`;
}

const HOLD_REASON_WORDS: Record<string, string> = {
  machine_busy: "The Mac is busy",
  disk_low: "The disk is low",
  keep: "Kept for one purpose",
  outage: "An outage",
};

export function holdWords(h: RouteHold): string {
  return h.text || HOLD_REASON_WORDS[h.reason] || "A hold";
}

/** Disagreements older than the warn time (0 means never). */
export function longDisagreements(view: RouteView, now: Date): { label: string; since: string }[] {
  const after = view.warn_after?.disagreement ?? 30 * 60;
  if (!after) return [];
  const out: { label: string; since: string }[] = [];
  for (const a of view.accounts ?? []) {
    const sides: [string, RouteReport | null | undefined][] = [[a.label, a.disagreement]];
    // Only a model whose limit the card shows (its end is ahead), so the
    // item always points at a note on the card (modelDisagreementWords).
    for (const m of a.models ?? []) if (ahead(m.until, now)) sides.push([`${a.label} (${m.model})`, m.disagreement]);
    for (const [label, d] of sides) {
      const began = parse(d?.since ?? d?.at);
      if (began && now.getTime() - began.getTime() > after * 1000) out.push({ label, since: began.toISOString() });
    }
  }
  return out;
}

export function brokenChecks(view: RouteView): RuleCheck[] {
  return (view.rule_checks ?? []).filter((c) => c.status === "broken");
}

/** The broken checks grouped by rule, in the order each rule first appears:
 *  one rule broken on two lanes is one broken rule, not two. */
export function brokenRules(view: RouteView): { rule: number; checks: RuleCheck[] }[] {
  const out: { rule: number; checks: RuleCheck[] }[] = [];
  for (const c of brokenChecks(view)) {
    const g = out.find((x) => x.rule === c.rule);
    if (g) g.checks.push(c);
    else out.push({ rule: c.rule, checks: [c] });
  }
  return out;
}

// Words a rule commonly opens with that are not names, so "Never a -fast
// tier." reads "never a -fast tier" after "Your rule says". Any other first
// word keeps its letter as written: "Grok", "Khipu" or "Codex" is a name.
const RULE_OPENERS = new Set([
  "a", "after", "all", "always", "an", "any", "ask", "at", "avoid", "before", "both", "check", "do",
  "don't", "each", "every", "for", "from", "give", "if", "in", "keep", "leave", "let", "make", "never",
  "no", "not", "nothing", "on", "once", "one", "only", "prefer", "put", "read", "route", "run", "send",
  "some", "stop", "the", "treat", "until", "use", "wait", "when", "while", "with", "without", "write",
]);

/** A rule's text as it reads after "Your rule says": the closing period
 *  dropped and a common opening word lower-cased; a name stays as written. */
export function ruleWords(text: string): string {
  const t = text.trim().replace(/\.$/, "");
  const first = t.split(/\s/, 1)[0] ?? "";
  const plain = /^[A-Z][a-z']*[,:;]?$/.test(first) && RULE_OPENERS.has(first.replace(/[,:;]$/, "").toLowerCase());
  return plain ? t.charAt(0).toLowerCase() + t.slice(1) : t;
}

export function quietRunners(view: RouteView): RouteRunner[] {
  return (view.sources?.runners ?? []).filter((r) => r.silent || r.unreadable);
}

/** "Your Project B job runner stopped reporting at 2:12 PM." */
export function runnerWords(r: RouteRunner, now: Date): string {
  if (r.silent) {
    return r.heartbeat_at
      ? `Your ${r.name} job runner stopped reporting ${pastWords(r.heartbeat_at, now)}.`
      : `Your ${r.name} job runner has not reported yet.`;
  }
  return r.unreadable_since
    ? `Khipu can't read ${r.name}'s files since ${whenLocal(r.unreadable_since, now)}.`
    : `Khipu can't read ${r.name}'s files.`;
}

/** What one changed entity now says: "Grok Build limit reached (a guess)". */
function entityWords(entity: string, view: RouteView): string {
  const [kind, ...rest] = entity.split(":");
  const ident = rest.join(":");
  if (kind === "account") {
    const a = (view.accounts ?? []).find((x) => x.key === ident);
    if (!a) return "an account is no longer tracked";
    const word: Record<string, string> = {
      limited: a.basis === "estimated" ? "limit reached (a guess)" : "limit reached",
      open: "open",
      paused: "paused by you",
      down: "down",
      probably_open: "probably open",
    };
    return `${a.label} ${word[a.state] ?? "unknown"}`;
  }
  if (kind === "hold") {
    const h = (view.holds ?? []).find((x) => x.id === ident);
    return h ? `hold on ${h.scope_label} set` : "a hold ended";
  }
  if (kind === "rule") return `standing rule ${ident} changed`;
  if (entity === "note") return view.note ? "your note changed" : "your note ended";
  if (kind === "runner") {
    const r = (view.sources?.runners ?? []).find((x) => x.name === ident);
    return r?.silent ? `${ident} job runner stopped reporting` : `${ident} job runner reporting`;
  }
  if (kind === "lane") {
    const [runner, lane] = ident.split("/");
    return `${runner}'s ${lane} lane changed`;
  }
  if (entity === "table") return "the routing table's NOW block changed";
  return entity;
}

/** "Last change 3:00 PM: Grok Build limit reached (a guess)." or null. */
export function lastChangeWords(view: RouteView, now: Date): string | null {
  const changes = view.changes ?? [];
  const last = changes[changes.length - 1];
  if (!last || !last.entities?.length) return null;
  const first = capitalise(entityWords(last.entities[0], view));
  const more = last.entities.length - 1;
  return `Last change ${whenLocal(last.at, now)}: ${first}${more > 0 ? ` and ${more} more` : ""}.`;
}

export const ROUTE_READ_FAILED = "Khipu could not read the routing record. It tries again every minute.";
const ROUTE_READ_UNREADABLE = "The routing record gave an answer Khipu could not read.";

/** The record, read whenever `active` turns true, again every minute while
 *  it stays so, and on `load()`. A failed read keeps the last good view;
 *  the card then says since when it is old. */
export function useRouting(active: boolean) {
  const [data, setData] = useState<RouteView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [lastOkAt, setLastOkAt] = useState<string | null>(null);
  // True from the moment `active` turns true until the read it starts
  // settles, so the card does not call a record old while that read is under
  // way. Set during render, not in an effect, so not even one frame shows it.
  const [wasActive, setWasActive] = useState(active);
  const [firstRead, setFirstRead] = useState(active);
  if (active !== wasActive) {
    setWasActive(active);
    if (active) setFirstRead(true);
  }
  const inflight = useRef<Promise<void> | null>(null);

  const load = useCallback((fresh = false): Promise<void> => {
    // After a write, a read already under way may predate it: wait for it,
    // then read again.
    if (inflight.current) return fresh ? inflight.current.then(() => load(false)) : inflight.current;
    setLoading(true);
    const p = (async () => {
      try {
        const res = await callCli<RouteView>("khipu_route_view");
        if (!res.ok) {
          console.error("khipu route failed:", res.error);
          setError(ROUTE_READ_FAILED);
          return;
        }
        const view = res.data;
        if (view && typeof view === "object" && Array.isArray(view.accounts)) {
          setData(view);
          setError(null);
          setLastOkAt(new Date().toISOString());
        } else {
          setError(ROUTE_READ_UNREADABLE);
        }
      } finally {
        inflight.current = null;
        setLoading(false);
        setFirstRead(false);
      }
    })();
    inflight.current = p;
    return p;
  }, []);

  useEffect(() => {
    if (!active) return;
    void load();
    const tick = window.setInterval(() => void load(), MINUTE_MS);
    return () => window.clearInterval(tick);
  }, [active, load]);

  return { data, error, loading, lastOkAt, firstRead, load };
}

export type RoutingState = ReturnType<typeof useRouting>;

/** "Khipu's record has not been refreshed since 2:58 PM" when a read failed
 *  and the last good one is older than the record's own bound (5 minutes by
 *  default), or when the record's own `checked_at` is past that bound. Never
 *  while the first read after Home turns active again is under way: the view
 *  is old then only because nobody was looking. */
export function notRefreshedWords(
  state: { data: RouteView | null; lastOkAt: string | null; error?: string | null; firstRead?: boolean },
  now: Date,
): string | null {
  const { data, lastOkAt, error, firstRead } = state;
  if (!data || !lastOkAt || !data.record || firstRead) return null;
  const bound = data.warn_after?.not_refreshed ?? 5 * 60;
  if (!bound) return null;
  const past = (iso: string | null | undefined) => {
    const at = parse(iso);
    return !!at && now.getTime() - at.getTime() > bound * 1000;
  };
  if (!(error && past(lastOkAt)) && !past(data.checked_at)) return null;
  return `Khipu's record has not been refreshed since ${whenLocal(data.checked_at ?? lastOkAt, now)}`;
}
