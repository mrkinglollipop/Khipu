/* Words and choices for Settings -> Routing (docs/plans/2026-10-14-routing-truth.md,
   phase 4; mock docs/mockups/2026-10-14-routing/settings-routing.html). Pure
   functions, so the page's tests can pin the words and the times. Every time
   is the viewer's own local time. */

import { accountState, capitalise, whenLocal } from "./routing";
import type { RouteAccount, RouteChange, RouteChangeItem, RouteView } from "./routing";

const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** An ISO 8601 time with the viewer's own offset: 2026-10-16T09:00:00-04:00. */
export function isoLocal(d: Date): string {
  const pad = (n: number) => String(Math.abs(Math.trunc(n))).padStart(2, "0");
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` +
    `${sign}${pad(off / 60)}:${pad(off % 60)}`
  );
}

function clock(d: Date): string {
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function at(base: Date, days: number, hour: number): Date {
  return new Date(base.getFullYear(), base.getMonth(), base.getDate() + days, hour, 0, 0, 0);
}

export type EndChoice = { id: string; label: string; iso: string | null };

/** The ends a pause or a hold offers: a few hours from now, tonight at 9 PM
 *  (before then), tomorrow at 9 AM, Friday at 9 AM, a date and time, and
 *  "Until I clear it" (`iso` null). `custom` asks for the date and time. */
export function endChoices(now: Date, firstHours: number): EndChoice[] {
  const out: EndChoice[] = [];
  const soon = new Date(now.getTime() + firstHours * HOUR * 1000);
  out.push({
    id: "soon",
    label: `In ${firstHours} hour${firstHours === 1 ? "" : "s"}, ${clock(soon)}`,
    iso: isoLocal(soon),
  });
  if (now.getHours() < 21) {
    const tonight = at(now, 0, 21);
    out.push({ id: "tonight", label: `Tonight, ${clock(tonight)}`, iso: isoLocal(tonight) });
  }
  const tomorrow = at(now, 1, 9);
  out.push({ id: "tomorrow", label: `Tomorrow, ${clock(tomorrow)}`, iso: isoLocal(tomorrow) });
  let toFriday = (5 - now.getDay() + 7) % 7;
  if (toFriday <= 1) toFriday += 7;
  const friday = at(now, toFriday, 9);
  const fridayWords =
    toFriday < 7 ? "Friday" : `Friday ${friday.toLocaleDateString([], { month: "short", day: "numeric" })}`;
  out.push({ id: "friday", label: `${fridayWords}, ${clock(friday)}`, iso: isoLocal(friday) });
  out.push({ id: "custom", label: "A date and time…", iso: null });
  out.push({ id: "never", label: "Until I clear it", iso: null });
  return out;
}

/** The end a form asked for: the ISO time to send, null for "until I clear
 *  it", or an error in words. `custom` is a datetime-local value. */
export function chosenEnd(
  choice: string,
  custom: string,
  choices: EndChoice[],
  now: Date,
): { iso: string | null } | { error: string } {
  if (choice === "never") return { iso: null };
  if (choice === "custom") {
    const d = custom ? new Date(custom) : null;
    if (!d || Number.isNaN(d.getTime())) return { error: "Choose the date and time it ends." };
    if (d.getTime() <= now.getTime()) return { error: "Choose a time that hasn't passed yet." };
    return { iso: isoLocal(d) };
  }
  const found = choices.find((c) => c.id === choice);
  return found?.iso ? { iso: found.iso } : { error: "Choose when it ends." };
}

/** "2 minutes", "12 hours", "7 days": a setting's length of time. */
export function durationWords(seconds: number): string {
  const plural = (n: number, one: string) => `${n.toLocaleString()} ${one}${n === 1 ? "" : "s"}`;
  if (seconds >= 2 * DAY && seconds % DAY === 0) return plural(seconds / DAY, "day");
  if (seconds >= HOUR && seconds % HOUR === 0) return plural(seconds / HOUR, "hour");
  if (seconds >= MINUTE && seconds % MINUTE === 0) return plural(seconds / MINUTE, "minute");
  return plural(seconds, "second");
}

/** "1 h 38 min ago", "12 min ago", "2 days ago": how long ago, in words. */
export function agoSpan(iso: string, now: Date): string {
  const mins = Math.max(0, Math.round((now.getTime() - new Date(iso).getTime()) / 60_000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return mins % 60 ? `${hours} h ${mins % 60} min ago` : `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

export type LimitUnit = "time" | "chars" | "ms" | "changes" | "bytes" | "lines" | "intervals" | "pct" | "load" | "attempts";

/** "512 KB", "1 MB", "300 bytes": a size in bytes. */
function bytesWords(value: number): string {
  const MB = 1024 * 1024;
  if (value >= MB && value % (MB / 4) === 0) return `${(value / MB).toLocaleString()} MB`;
  if (value >= 1024 && value % 1024 === 0) return `${(value / 1024).toLocaleString()} KB`;
  return `${value.toLocaleString()} byte${value === 1 ? "" : "s"}`;
}

/** One setting's value in words: "1,500 characters", "50 ms", "12 hours",
 *  "512 KB", "1%", or the setting's own word for 0 ("No limit", "Never"). */
export function limitWords(value: number, unit: LimitUnit, zero?: string): string {
  if (value === 0 && zero) return zero;
  const plural = (one: string) => `${value.toLocaleString()} ${one}${value === 1 ? "" : "s"}`;
  if (unit === "time") return durationWords(value);
  if (unit === "chars") return plural("character");
  if (unit === "ms") return `${value.toLocaleString()} ms`;
  if (unit === "bytes") return bytesWords(value);
  if (unit === "lines") return plural("line");
  if (unit === "intervals") return plural("interval");
  if (unit === "pct") return `${value.toLocaleString()}%`;
  if (unit === "load") return `load ${value.toLocaleString()}`;
  if (unit === "attempts") return plural("attempt");
  return plural("change");
}

/** A rule's source and date as the page shows it: "From your routing table,
 *  Oct 13" or "Added by you, Oct 15". */
export function ruleOrigin(source: string | null | undefined, date: string | null | undefined, now: Date): string {
  const where = capitalise((source ?? "").trim());
  let when = "";
  if (date && /^\d{4}-\d{2}-\d{2}$/.test(date)) {
    const [y, m, d] = date.split("-").map(Number);
    const day = new Date(y, m - 1, d);
    when = day.toLocaleDateString(
      [],
      y === now.getFullYear()
        ? { month: "short", day: "numeric" }
        : { month: "short", day: "numeric", year: "numeric" },
    );
  }
  return [where, when].filter(Boolean).join(", ");
}

const STATE_SAID: Record<string, string> = {
  limited: "limit reached",
  open: "open",
  down: "down",
  paused: "paused by you",
  probably_open: "probably open",
  unknown: "unknown",
};

export function stateSaid(state: unknown): string {
  return (typeof state === "string" && STATE_SAID[state]) || "unknown";
}

const HOLD_KIND: Record<string, string> = {
  machine_busy: "Machine busy",
  disk_low: "Disk low",
  keep: "Keep for one purpose",
  outage: "Outage",
  other: "Other",
};

export function holdKind(reason: string | null | undefined): string {
  return (reason && HOLD_KIND[reason]) || "Other";
}

function lowerFirst(text: string): string {
  return text ? text.charAt(0).toLowerCase() + text.slice(1) : text;
}

function str(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

export type ChangeRow = {
  key: string;
  at: string;
  /** The subject, shown in bold. */
  subject: string;
  text: string;
  /** Smaller words after the text, such as what it was before. */
  aside?: string;
  who: string;
  /** A removed rule the page can bring back. */
  restore?: number;
};

function whoWords(item: RouteChangeItem): string {
  if (item.by === "you") {
    // Khipu's owner check did not run for this line (khipu/routing.py `_mark_unchecked`).
    if (item.via?.endsWith("-unchecked")) return "Marked as you, but not checked by Khipu";
    return item.via === "app" ? "You, in Settings" : "You, outside the app";
  }
  if (item.source === "probe") return "The plan check";
  if (item.source === "check") return "Khipu's check";
  if (item.by) return capitalise(item.by.replace(/ lanes \(read by Khipu\)$/, " job runner"));
  if (item.source === "runner") return "Your job runner";
  if (item.source === "table") return "Your routing table";
  return "Khipu";
}

type EffectiveModel = { model: string; until: string | null; basis: string | null; side: unknown };

/** The models of an account's effective view, `[model, until, basis,
 *  [state, source] | null]` each (`routing._effective_account`). */
function effectiveModels(raw: unknown): EffectiveModel[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((m): m is unknown[] => Array.isArray(m) && typeof m[0] === "string")
    .map((m) => ({ model: m[0] as string, until: str(m[1]), basis: str(m[2]), side: m[3] ?? null }));
}

const SIDE_WHO: Record<string, string> = {
  report: "a conversation",
  runner: "a job runner",
  probe: "the plan check",
  check: "Khipu's check",
  owner: "you",
};

/** The side that disagrees, `[state, source]`, in words. */
function sideSaid(raw: unknown): { who: string; state: string } | null {
  if (!Array.isArray(raw) || typeof raw[0] !== "string") return null;
  return { who: SIDE_WHO[String(raw[1])] ?? "another report", state: stateSaid(raw[0]) };
}

function itemRow(change: RouteChange, item: RouteChangeItem, i: number, now: Date): ChangeRow {
  const base = { key: `${change.version}-${i}`, at: change.at, who: whoWords(item) };
  const [kind, ...rest] = item.entity.split(":");
  const ident = rest.join(":");
  const cur = item.now ?? null;
  const was = item.was ?? null;
  if (kind === "account") {
    const label = str(cur?.label) ?? str(was?.label) ?? "An account";
    if (!cur) return { ...base, subject: label, text: "no longer tracked" };
    // Worded as it stood when it changed, with each model's limit, and the
    // disagreements and model limits that came or went beside the state
    // (Codex review, finding 5).
    const then = new Date(change.at);
    const at = Number.isNaN(then.getTime()) ? now : then;
    const nowModels = effectiveModels(cur.models);
    const wasModels = effectiveModels(was?.models);
    const lastSeen = str(cur.last_seen);
    const entry = {
      key: ident,
      label,
      harness: "",
      ...cur,
      models: nowModels,
      disagreement: null,
      last_seen: lastSeen ? { state: lastSeen } : null,
    } as unknown as RouteAccount;
    const said = accountState(entry, at).text.trim();
    // A model's name keeps its capital ("Opus limit reached, ...").
    const text = nowModels.some((m) => said.startsWith(m.model)) ? said : lowerFirst(said);
    const parts: string[] = [];
    if (was && was.state !== cur.state) parts.push(`was ${stateSaid(was.state)}`);
    const dis = sideSaid(cur.disagreement);
    if (dis && JSON.stringify(cur.disagreement) !== JSON.stringify(was?.disagreement)) parts.push(`${dis.who} said it is ${dis.state}`);
    else if (!dis && sideSaid(was?.disagreement)) parts.push("the reports agree again");
    for (const m of nowModels) {
      const before = wasModels.find((x) => x.model === m.model);
      const side = sideSaid(m.side);
      if (side && JSON.stringify(m.side) !== JSON.stringify(before?.side)) parts.push(`${side.who} said ${m.model} is ${side.state}`);
      else if (!side && sideSaid(before?.side)) parts.push(`the reports about ${m.model} agree again`);
    }
    for (const m of wasModels) {
      if (!nowModels.some((x) => x.model === m.model)) parts.push(`${m.model} limit over`);
    }
    return { ...base, subject: label, text, aside: parts.length ? `(${parts.join("; ")})` : undefined };
  }
  if (kind === "hold") {
    const scope = str(cur?.scope) ?? str(was?.scope) ?? "all work";
    if (!cur) return { ...base, subject: "Hold", text: `on ${scope} ended` };
    const until = str(cur.until);
    const ends = until ? `ends ${whenLocal(until, now)}` : "until you clear it";
    const words = str(cur.text);
    return {
      ...base,
      subject: "Hold",
      text: `on ${scope}: ${lowerFirst(holdKind(str(cur.reason)))}, ${ends}`,
      aside: words ? `“${words}”` : undefined,
    };
  }
  if (kind === "rule") {
    const subject = `Standing rule ${ident}`;
    if (!cur) {
      return {
        ...base,
        subject,
        text: was?.pending === true && was.pending_kind === "added" ? "dismissed" : "removed",
        aside: was?.text ? `“${String(was.text)}”` : undefined,
        restore: item.n ?? Number(ident),
      };
    }
    if (cur.pending === true) {
      const action = cur.pending_kind === "changed" ? "changed" : "added";
      const by = str(cur.pending_by) ?? "a conversation";
      const prior = str(cur.prior_text);
      return {
        ...base,
        subject,
        text: `${action} by ${by} at the owner's request; the owner has not confirmed it yet`,
        aside: prior ? `“${String(cur.text)}”; it replaced “${prior}”` : `“${String(cur.text)}”`,
      };
    }
    if (was?.pending === true && cur.pending !== true) {
      if (was.text === cur.text) return { ...base, subject, text: "confirmed" };
      if (cur.text === was.prior_text) return { ...base, subject, text: "undone", aside: `“${String(cur.text)}”` };
      return { ...base, subject, text: "changed", aside: `“${String(cur.text)}”` };
    }
    if (!was) return { ...base, subject, text: "added", aside: `“${String(cur.text)}”` };
    if (was.text !== cur.text) return { ...base, subject, text: "changed", aside: `“${String(cur.text)}”` };
    if (was.seeded && !cur.seeded) return { ...base, subject, text: "confirmed" };
    return { ...base, subject, text: "changed" };
  }
  if (item.entity === "note") {
    if (!cur) return { ...base, subject: "Your note", text: "ended", aside: was?.text ? `“${String(was.text)}”` : undefined };
    return { ...base, subject: "Your note", text: was ? "changed" : "written", aside: `“${String(cur.text)}”` };
  }
  if (kind === "runner") {
    const subject = `${ident} job runner`;
    if (!cur) return { ...base, subject, text: "folder no longer read" };
    if (cur.unreadable) return { ...base, subject, text: "files can't be read" };
    if (cur.silent) return { ...base, subject, text: "stopped reporting" };
    return { ...base, subject, text: was ? "reporting again" : "folder added" };
  }
  if (kind === "lane") {
    const [runner, lane] = ident.split("/");
    const subject = `${runner}'s ${lane} lane`;
    if (!cur) return { ...base, subject, text: "its limit is over" };
    const until = str(cur.until);
    return {
      ...base,
      subject,
      text: `reached a limit${until ? `, until ${whenLocal(until, now)}` : ""}`,
      aside: "(not matched to an account)",
    };
  }
  if (item.entity === "table") return { ...base, subject: "Routing table", text: "NOW block changed" };
  return { ...base, subject: capitalise(kind), text: "changed" };
}

/** A change recorded before Khipu kept each change's details. */
function legacyRow(change: RouteChange, entity: string, i: number, view: RouteView): ChangeRow {
  const [kind, ...rest] = entity.split(":");
  const ident = rest.join(":");
  const base = { key: `${change.version}-${i}`, at: change.at, who: "Khipu", text: "changed" };
  if (kind === "account") {
    const label = (view.accounts ?? []).find((a) => a.key === ident)?.label ?? "An account";
    return { ...base, subject: label };
  }
  if (kind === "rule") return { ...base, subject: `Standing rule ${ident}` };
  if (entity === "note") return { ...base, subject: "Your note" };
  if (kind === "hold") return { ...base, subject: "A hold" };
  if (kind === "runner") return { ...base, subject: `${ident} job runner` };
  if (entity === "table") return { ...base, subject: "Routing table", text: "NOW block changed" };
  return { ...base, subject: capitalise(kind) };
}

export type ChangeDay = { key: string; label: string; rows: ChangeRow[] };

function dayKey(d: Date): string {
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
}

/** Recent changes, newest first, one list per local day: "Today",
 *  "Yesterday", then "Mon, Oct 12". */
export function changeDays(view: RouteView, now: Date): ChangeDay[] {
  const rows: ChangeRow[] = [];
  for (const change of view.changes ?? []) {
    const items = change.items;
    if (items && items.length) items.forEach((item, i) => rows.push(itemRow(change, item, i, now)));
    else (change.entities ?? []).forEach((e, i) => rows.push(legacyRow(change, e, i, view)));
  }
  rows.reverse();
  const today = dayKey(now);
  const yesterday = dayKey(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1));
  const days: ChangeDay[] = [];
  for (const row of rows) {
    const d = new Date(row.at);
    if (Number.isNaN(d.getTime())) continue;
    const key = dayKey(d);
    let day = days.find((x) => x.key === key);
    if (!day) {
      const label =
        key === today
          ? "Today"
          : key === yesterday
            ? "Yesterday"
            : d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
      day = { key, label, rows: [] };
      days.push(day);
    }
    day.rows.push(row);
  }
  return days;
}

/** The CLI names a setting by its key ("routing_table needs ..."); the page
 *  names it as the setting's own row does ("Routing table file needs ..."). */
export function settingWords(msg: string, key: string, words: string): string {
  return msg.replace(new RegExp(`\\b${key}\\b`, "g"), (_m: string, at: number) =>
    at === 0 ? words : words.charAt(0).toLowerCase() + words.slice(1),
  );
}

/** Why the routing table file can't be read, from the CLI's fixed phrase
 *  (`routing_sources._short_error`, or "no NOW block"), in plain words. */
export function tableErrorWords(error: string): string {
  const big = /^larger than (\d+) MB$/.exec(error);
  if (big) return `it is larger than ${big[1]} MB`;
  const words: Record<string, string> = {
    missing: "no file at that path",
    "a folder, not a file": "that is a folder, not a file",
    "not a regular file": "that is not a plain file",
    "no NOW block": "it has no NOW block",
    "not allowed to read it": "Khipu isn't allowed to read it",
  };
  return words[error] ?? "it can't be read";
}
