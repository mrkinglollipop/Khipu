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
};

export type RouteRule = { n: number; text: string; seeded?: boolean; confirmed?: boolean; date?: string | null };

export type RouteRunner = {
  name: string;
  heartbeat_at?: string | null;
  silent?: boolean;
  unreadable?: boolean;
  unreadable_since?: string | null;
};

export type RouteLane = { runner: string; lane: string; until?: string | null };

export type RouteChange = { version: number; at: string; entities: string[] };

export type RuleCheck = { rule: number; status: string; text: string; at?: string | null };

export type RouteView = {
  record?: boolean;
  version?: number;
  checked_at?: string;
  accounts?: RouteAccount[];
  holds?: RouteHold[];
  rules?: RouteRule[];
  note?: { text: string; at?: string | null } | null;
  unmatched?: RouteLane[];
  sources?: { runners?: RouteRunner[] } | null;
  changes?: RouteChange[];
  rule_checks?: RuleCheck[];
  /** Seconds; 0 means never. */
  warn_after?: { disagreement?: number; not_refreshed?: number };
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
function aheadWords(iso: string | null | undefined, now: Date): string {
  const d = parse(iso);
  if (!d) return "";
  return dayWords(d, now) ? whenLocal(iso, now) : `at ${clock(d)}`;
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
        : `${m.model} limit reached, back ${whenLocal(m.until, now)}`,
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
          ? `Its reset time passed at ${whenLocal(a.until, now)}; this account can't be checked without using a turn`
          : `Probably open: its limit was due to end at ${whenLocal(a.until, now)}, not checked yet`;
      break;
    default:
      if (a.silent) text = "Unknown: its only reporter stopped";
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
      return w.message_time ? `From ${cli}'s own message, reported by ${by}` : `Reported by ${by}`;
    }
  }
}

/** The plan figure an open account's row adds: "58% of its week used". */
export type PlanFigure = { pct: number; window: string };

const WINDOW_WORDS: Record<string, string> = { five_hour: "five hours", weekly: "week", daily: "day" };

export function windowWords(name: string | undefined): string {
  return (name && WINDOW_WORDS[name]) || (name ?? "window").replace(/_/g, " ");
}

/** Who said so and when, plus what else the row should say. */
export function accountSource(a: RouteAccount, now: Date, plan?: PlanFigure | null): string {
  const parts: string[] = [];
  const w = a.state === "unknown" ? a.last_seen : a.witness;
  if (w) {
    const when = saidWhen(w.at, now);
    parts.push(`${witnessWords(w, a.harness, a.state)}${when ? ` ${when}` : ""}`);
  }
  if (a.state === "limited" && a.checkable === false) parts.push("can't be checked without using a turn");
  if (a.state === "open" && a.witness?.source === "probe" && plan) {
    parts.push(`${Math.round(plan.pct)}% of its ${plan.window} used`);
  }
  return parts.join(" · ");
}

/** Which report shows over the other, when two sources disagree. */
function strongerWords(a: RouteAccount): string {
  const w = a.witness;
  const cli = cliName(a.harness);
  if (w?.source === "probe") return `Khipu's read of ${cli}`;
  if (w?.source === "check") return "Khipu's check";
  if (w?.source === "owner") return "Your own word";
  if (w?.message_time) return `${cli}'s own message`;
  return "The stronger report";
}

function weakerWords(d: RouteReport): string {
  if (d.source === "report") return "a conversation's report";
  if (d.source === "runner") return "a job runner's report";
  return "the other report";
}

/** "A Claude Code conversation in project-b reported it open at 11:31 AM.
 *  Claude's own message outranks a conversation's report, so the limit
 *  shows here; conversations are told both." Null when nothing disagrees. */
export function disagreementWords(a: RouteAccount, now: Date): string | null {
  const d = a.disagreement;
  if (!d) return null;
  const shown = a.state === "limited" ? "the limit" : `"${stateWord(a.state)}"`;
  const by = d.by ? capitalise(d.by) : capitalise(witnessWords(d, a.harness, d.state ?? ""));
  return (
    `${by} reported it ${stateWord(d.state)} ${saidWhen(d.at, now)}. ` +
    `${strongerWords(a)} outranks ${weakerWords(d)}, so ${shown} shows here; conversations are told both.`
  );
}

/** A hold's end and author: "Ends tomorrow 9:40 AM · set by a Claude Code
 *  conversation in project-a". */
export function holdSource(h: RouteHold, now: Date): string {
  const end = h.until ? `Ends ${whenLocal(h.until, now)}` : "Until you clear it";
  const by = h.owner ? `set by you ${saidWhen(h.at, now)}`.trim() : `set by ${h.by || "a conversation"}`;
  return `${end} · ${by}`;
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
    for (const m of a.models ?? []) sides.push([`${a.label} (${m.model})`, m.disagreement]);
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

export function quietRunners(view: RouteView): RouteRunner[] {
  return (view.sources?.runners ?? []).filter((r) => r.silent || r.unreadable);
}

/** "Your Project B job runner stopped reporting at 2:12 PM." */
export function runnerWords(r: RouteRunner, now: Date): string {
  if (r.silent) {
    return r.heartbeat_at
      ? `Your ${r.name} job runner stopped reporting at ${whenLocal(r.heartbeat_at, now)}.`
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
  const inflight = useRef<Promise<void> | null>(null);

  const load = useCallback((): Promise<void> => {
    if (inflight.current) return inflight.current;
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

  return { data, error, loading, lastOkAt, load };
}

export type RoutingState = ReturnType<typeof useRouting>;

/** "Khipu's record has not been refreshed since 2:58 PM" when the last good
 *  read is older than the record's own bound (5 minutes by default). */
export function notRefreshedWords(
  state: { data: RouteView | null; lastOkAt: string | null },
  now: Date,
): string | null {
  const { data, lastOkAt } = state;
  if (!data || !lastOkAt || !data.record) return null;
  const bound = data.warn_after?.not_refreshed ?? 5 * 60;
  const at = parse(lastOkAt);
  if (!bound || !at || now.getTime() - at.getTime() <= bound * 1000) return null;
  return `Khipu's record has not been refreshed since ${whenLocal(data.checked_at ?? lastOkAt, now)}`;
}
