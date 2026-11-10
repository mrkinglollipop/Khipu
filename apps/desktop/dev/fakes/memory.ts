// Memory's canned answers for the dev preview (slice A7): recent
// conversations, one conversation, search (with and without --owner-words) and
// forget. Fictional, shaped as `khipu activity` and `khipu search` print them
// (slice B7: quote, title, tool, mac, project on each row). Switches:
//   memOffline: true   "Only my words" is refused, as it is when the hub is down
// Forget answers like the real call but changes nothing anywhere; a forgotten
// conversation only drops out of this page's own copy for the session.
import type { FakeHandler } from "./index";

const hoursAgo = (h: number) => new Date(Date.now() - h * 3600_000).toISOString();

type Conv = {
  id: number; ts: string; tool: string | null; mac: string | null; project: string | null; title: string;
  about: string; said: string[]; decided: string[];
};

const CONVS: Conv[] = [
  { id: 4101, ts: hoursAgo(1), tool: "Claude Code", mac: "M5", project: "Khipu", title: "Making the Khipu app simpler",
    about: "Planning a smaller Khipu app: one list of questions, accounts, and search.",
    said: ["I just want to open it, see if anything needs me, and close it.", "Use M1 and M5 for the Macs, never “this Mac”."],
    decided: ["The app gets three places: Today, Accounts and Memory."] },
  { id: 4102, ts: hoursAgo(4), tool: "Codex", mac: "M5", project: "Khipu", title: "Review of the needs-you clean-up",
    about: "Codex checked that old summary items no longer reach your list.",
    said: ["If I can’t answer it with a yes or a no, it shouldn’t be on my list."], decided: ["Only real questions reach the list."] },
  { id: 4103, ts: hoursAgo(28), tool: "Claude Code", mac: "M1", project: "Church slides", title: "Sunday slides layout",
    about: "Laid out this week’s slides with bigger verse text.",
    said: ["Make the verse text bigger, people at the back can’t read it."], decided: ["Verse text at 44 points."] },
  { id: 4104, ts: hoursAgo(24 * 9 + 3), tool: "Claude Code", mac: "M5", project: "Khipu", title: "Grok’s weekly limit",
    about: "Set where agents stop on Grok so Grok Bot isn’t starved.",
    said: ["Grok shares one week with Grok Bot, so agents should stop before they eat all of it. Leave room for Grok Bot.", "Ninety percent is fine for now."],
    decided: ["Agents stop at 90% of Grok’s week."] },
  { id: 4105, ts: hoursAgo(24 * 10 + 8), tool: "Cursor", mac: "M5", project: "Gear tracker", title: "Backpack weight list",
    about: "Sorted the gear list by weight and flagged duplicates.", said: ["Sort it heaviest first."], decided: [] },
  { id: 4106, ts: hoursAgo(24 * 12), tool: "Claude Code", mac: null, project: "Khipu", title: "Two Macs on different versions",
    about: "Looked at why the second Mac was a version behind.", said: ["Update together, or not at all."], decided: ["Updates go to both Macs together."] },
];

const gone = new Set<number>();
const live = () => CONVS.filter((c) => !gone.has(c.id));

const clip = (s: string, n = 300) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

function matches(line: string, q: string): boolean {
  const l = line.toLowerCase();
  const phrase = q.toLowerCase();
  return l.includes(phrase) || phrase.split(/\s+/).filter(Boolean).every((w) => l.includes(w));
}

function row(c: Conv, quote: string | null) {
  return { kind: "episode", id: String(c.id), label: c.title, snippet: c.about, ts: c.ts, harness: "claude_code",
    quote, title: c.title, tool: c.tool, mac: c.mac, project: c.project };
}

export const memoryFakes: FakeHandler = (cmd, args, flags) => {
  if (cmd !== "run_khipu") return undefined;
  const a = (args?.args as string[] | undefined) ?? [];
  if (a[0] === "activity" && a[1] === "--limit") {
    return { recent: live().map((c) => ({ id: c.id, ts: c.ts, summary: c.about, scope: c.project,
      quote: c.said[0], title: c.title, tool: c.tool, mac: c.mac, project: c.project })) };
  }
  if (a[0] === "activity" && a[1]?.startsWith("--show=")) {
    const c = CONVS.find((x) => x.id === Number(a[1].slice(7)));
    if (!c || gone.has(c.id)) return undefined;
    return { id: c.id, ts: c.ts, session_id: `claude_code:${c.id}`, scope: c.project, summary: c.about,
      topics: [], people: [], decisions: c.decided, preferences: [], edges: [], raw: {}, verbatim: { quotes: c.said } };
  }
  if (a[0] === "search") {
    const q = a[a.length - 1];
    const mine = a.includes("--owner-words");
    if (mine && flags.memOffline) return { ok: false, error: "searching only your words needs the hub; the offline snapshot cannot do it" };
    const rows = live().flatMap((c) => {
      const owner = c.said.find((s) => matches(s, q));
      if (owner) return [row(c, clip(owner))];
      if (!mine && matches(`${c.title} ${c.about}`, q)) return [row(c, clip(c.about))];
      return [];
    });
    return { ok: true, results: rows };
  }
  if (a[0] === "episode" && a[1] === "forget") {
    gone.add(Number(a[2]));
    return { ok: true, soft_deleted: true, partial: [], snapshot: { ok: true }, legacy_file: { ok: true } };
  }
  return undefined;
};
