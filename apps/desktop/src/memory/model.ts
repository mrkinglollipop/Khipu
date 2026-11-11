// What the Memory place reads: a search row and a recent-conversation row
// share one shape, so the list, the result and the conversation page are built
// from the same thing. The JSON is what `khipu search` and `khipu activity`
// print (slice B7): `quote`, `title`, `tool`, `mac` and `project` are
// computed there. Nothing is guessed here; a missing field is left out.

import { parseCommandJson } from "../shared/parseOutput";

export type Run = (args: string[]) => Promise<string>;

export type MemoryRow = {
  id: number;
  title: string;
  /** The line a search matched (the owner's own line when "Only my words" is on). */
  quote: string | null;
  tool: string | null;
  /** The owner's name for the Mac that wrote it. null: not a Mac, say nothing. */
  mac: string | null;
  project: string | null;
  ts: string | null;
};

function text(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v.trim() : null;
}

function firstLine(summary: unknown): string | null {
  const s = text(summary);
  if (!s) return null;
  const line = s.split("\n").find((l) => l.trim()) ?? s;
  return line.length > 90 ? `${line.slice(0, 89).trimEnd()}…` : line;
}

/** One row out of `results[]` or `recent[]`; null when it is not a
 *  conversation (a search can also return topics and nodes) or has no id. */
export function toRow(raw: unknown): MemoryRow | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  if (r.kind !== undefined && r.kind !== "episode") return null;
  const id = Number(r.id);
  if (!Number.isInteger(id) || id <= 0) return null;
  return {
    id,
    title: text(r.title) ?? firstLine(r.summary) ?? firstLine(r.snippet) ?? firstLine(r.label) ?? "Conversation",
    quote: text(r.quote),
    tool: text(r.tool),
    mac: text(r.mac),
    project: text(r.project),
    ts: text(r.ts),
  };
}

export function rowsOf(list: unknown): MemoryRow[] {
  if (!Array.isArray(list)) return [];
  const out: MemoryRow[] = [];
  for (const item of list) {
    const row = toRow(item);
    if (row) out.push(row);
  }
  return out;
}

const TIME = new Intl.DateTimeFormat("en-GB", { hour: "numeric", minute: "2-digit", hour12: true });

function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

function parse(ts: string | null): Date | null {
  if (!ts) return null;
  const d = new Date(ts.replace(" ", "T"));
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "Today", "Yesterday", "Thu 30 Oct" (and the year when it is not this one). */
export function dayLabel(ts: string | null, now: Date = new Date()): string {
  const d = parse(ts);
  if (!d) return "Earlier";
  if (sameDay(d, now)) return "Today";
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (sameDay(d, y)) return "Yesterday";
  const base = d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
  return d.getFullYear() === now.getFullYear() ? base : `${base} ${d.getFullYear()}`;
}

/** "Today, 2:02 pm", "Thu 30 Oct, 9:05 pm". */
export function whenLabel(ts: string | null, now: Date = new Date()): string | null {
  const d = parse(ts);
  if (!d) return null;
  return `${dayLabel(ts, now)}, ${TIME.format(d)}`;
}

/** "Claude Code on M5 · Khipu". A part that is not known is left out, never
 *  replaced by a stand-in; a conversation with no Mac does not say "on". */
export function whereLabel(row: MemoryRow): string {
  const tool = row.tool ? (row.mac ? `${row.tool} on ${row.mac}` : row.tool) : row.mac ? `On ${row.mac}` : null;
  return [tool, row.project].filter(Boolean).join(" · ");
}

export function metaLabel(row: MemoryRow, opts: { youSaid?: boolean; now?: Date } = {}): string {
  return [opts.youSaid ? "You said" : null, whenLabel(row.ts, opts.now), whereLabel(row) || null].filter(Boolean).join(" · ");
}

export type DayGroup = { label: string; rows: MemoryRow[] };

/** Rows in the order given, grouped by calendar day (consecutive runs). */
export function groupByDay(rows: MemoryRow[], now: Date = new Date()): DayGroup[] {
  const groups: DayGroup[] = [];
  for (const row of rows) {
    const label = dayLabel(row.ts, now);
    const last = groups[groups.length - 1];
    if (last && last.label === label) last.rows.push(row);
    else groups.push({ label, rows: [row] });
  }
  return groups;
}

export type Part = { text: string; hit: boolean };

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** `value` cut into pieces, the pieces that match the search marked. The whole
 *  phrase is looked for first; when it is not there, each word of two letters
 *  or more is, but only in a line that has them all (exact words: the phrase only). */
export function highlight(value: string, query: string, exact: boolean): Part[] {
  const q = query.trim();
  if (!q || !value) return [{ text: value, hit: false }];
  let re = new RegExp(escapeRe(q), "gi");
  if (!exact && !re.test(value)) {
    // Words are marked only in a line that holds all of them (the rule the
    // search used to pick the line), so a stray "for" in another line stays plain.
    const words = Array.from(new Set(q.split(/\s+/).filter((w) => w.length >= 2)));
    const lower = value.toLowerCase();
    if (words.length === 0 || !words.every((w) => lower.includes(w.toLowerCase()))) return [{ text: value, hit: false }];
    re = new RegExp(words.map(escapeRe).join("|"), "gi");
  }
  re.lastIndex = 0;
  const parts: Part[] = [];
  let last = 0;
  for (const m of value.matchAll(re)) {
    const at = m.index ?? 0;
    if (m[0] === "") continue;
    if (at > last) parts.push({ text: value.slice(last, at), hit: false });
    parts.push({ text: m[0], hit: true });
    last = at + m[0].length;
  }
  if (last < value.length) parts.push({ text: value.slice(last), hit: false });
  return parts.length ? parts : [{ text: value, hit: false }];
}

/** One conversation as `activity --show` prints it. */
export type Conversation = {
  about: string | null;
  said: string[];
  decided: string[];
};

function strings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const out: string[] = [];
  for (const item of value) {
    if (typeof item === "string" && item.trim()) out.push(item.trim());
    else if (item && typeof item === "object") {
      const o = item as Record<string, unknown>;
      const t = text(o.text) ?? text(o.decision) ?? text(o.summary);
      if (t) out.push(t);
    }
  }
  return out;
}

export function conversationOf(raw: unknown): Conversation | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const r = raw as Record<string, unknown>;
  const verbatim = r.verbatim && typeof r.verbatim === "object" ? (r.verbatim as Record<string, unknown>) : null;
  return {
    // An episode the summariser left empty carries a stand-in sentence and
    // `summary_kind`; the title already came from the owner's line, so skip it.
    about: r.summary_kind ? null : text(r.summary),
    said: strings(verbatim?.quotes),
    decided: strings(r.decisions),
  };
}

/** What the search command prints when it cannot answer: `{ok:false,error}`. */
export type SearchOutcome =
  | { kind: "rows"; rows: MemoryRow[] }
  | { kind: "error"; message: string; needsHub: boolean };

export function readSearch(raw: string): SearchOutcome {
  const parsed = parseCommandJson(raw);
  if (parsed === undefined) {
    return { kind: "error", message: "Search gave an answer the app could not read.", needsHub: false };
  }
  const p = parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : null;
  if (!p || p.ok === false || !Array.isArray(p.results)) {
    const message = typeof p?.error === "string" && p.error.trim() ? p.error.trim() : "Search gave an answer the app could not read.";
    return { kind: "error", message, needsHub: /needs the hub/i.test(message) };
  }
  return { kind: "rows", rows: rowsOf(p.results) };
}
