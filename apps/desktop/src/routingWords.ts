/* Words and choices for Settings -> Routing (docs/plans/2026-10-14-routing-truth.md,
   phase 4; mock docs/mockups/2026-10-14-routing/settings-routing.html). Pure
   functions, so the page's tests can pin the words and the times. Every time
   is the viewer's own local time. */

import { capitalise } from "./routing";

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
    iso: isoLocal(soon) });
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

export type LimitUnit = "time" | "chars" | "ms" | "changes" | "bytes" | "lines" | "intervals" | "pct" | "load" | "attempts" | "days" | "rules";

/** "512 KB", "1 MB", "300 bytes": a size in bytes. */
export function bytesWords(value: number): string {
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
  if (unit === "days") return plural("day");
  if (unit === "rules") return plural("rule");
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
  unknown: "unknown" };

export function stateSaid(state: unknown): string {
  return (typeof state === "string" && STATE_SAID[state]) || "unknown";
}

const HOLD_KIND: Record<string, string> = {
  machine_busy: "Machine busy",
  disk_low: "Disk low",
  keep: "Keep for one purpose",
  outage: "Outage",
  other: "Other" };

export function holdKind(reason: string | null | undefined): string {
  return (reason && HOLD_KIND[reason]) || "Other";
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
  /** Who made it, for the "Changes by" filter. */
  by: ChangeBy;
  /** What it changed, for the "Changes to" filter. */
  about: ChangeAbout;
  /** A removed rule the page can bring back. */
  restore?: number;
  /** Who has this change (owner-local delivery data), or null. */
  recipients?: import("./routing").RouteChangeRecipients | null;
};

export type ChangeBy = "you" | "conversations" | "runners" | "khipu";
export type ChangeAbout = "accounts" | "rules" | "holds" | "settings";

export type ChangeDay = { key: string; label: string; rows: ChangeRow[] };

