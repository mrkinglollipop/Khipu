import { describe, expect, it } from "vitest";
import { conversationOf, dayLabel, groupByDay, highlight, metaLabel, readSearch, toRow, whenLabel } from "../memory/model";

const NOW = new Date("2026-11-10T15:00:00");

describe("Memory rows", () => {
  it("reads a search row and leaves out what is not known", () => {
    const row = toRow({ kind: "episode", id: "121395", title: "Wrote a plan", quote: "keep it", tool: "Claude Code", mac: null, project: "Khipu", ts: "2026-11-10T09:02:00" });
    expect(row).toMatchObject({ id: 121395, title: "Wrote a plan", quote: "keep it", mac: null, project: "Khipu" });
    expect(metaLabel(row!, { youSaid: true, now: NOW })).toBe("You said · Today, 9:02 am · Claude Code · Khipu");
  });

  it("says 'on M5' only when a Mac is named", () => {
    const row = toRow({ id: 5, title: "x", tool: "Codex", mac: "M5", project: null, ts: null })!;
    expect(metaLabel(row)).toBe("Codex on M5");
  });

  it("skips topics and nodes, and rows without a usable id", () => {
    expect(toRow({ kind: "topic", id: "note:x" })).toBeNull();
    expect(toRow({ kind: "episode", id: "abc" })).toBeNull();
    expect(toRow(null)).toBeNull();
  });

  it("falls back to the first line of the summary for a title", () => {
    expect(toRow({ id: 3, summary: "First line.\nSecond." })!.title).toBe("First line.");
    expect(toRow({ id: 3 })!.title).toBe("Conversation");
  });

  it("names days and groups consecutive rows", () => {
    expect(dayLabel("2026-11-10T01:00:00", NOW)).toBe("Today");
    expect(dayLabel("2026-11-09T23:00:00", NOW)).toBe("Yesterday");
    expect(dayLabel("2026-10-30T21:05:00", NOW)).toBe("Fri 30 Oct");
    expect(whenLabel("2026-10-30T21:05:00", NOW)).toBe("Fri 30 Oct, 9:05 pm");
    expect(dayLabel(null, NOW)).toBe("Earlier");
    const rows = ["2026-11-10T10:00:00", "2026-11-10T08:00:00", "2026-11-09T08:00:00"].map((ts, i) => toRow({ id: i + 1, title: `t${i}`, ts })!);
    expect(groupByDay(rows, NOW).map((g) => [g.label, g.rows.length])).toEqual([["Today", 2], ["Yesterday", 1]]);
  });
});

describe("highlight", () => {
  it("marks the whole phrase", () => {
    expect(highlight("Leave room for Grok Bot.", "room for grok bot", false)).toEqual([
      { text: "Leave ", hit: false }, { text: "room for Grok Bot", hit: true }, { text: ".", hit: false },
    ]);
  });

  it("marks single words only in a line that has all of them", () => {
    expect(highlight("Bot room for Grok", "Grok room", false).filter((p) => p.hit).map((p) => p.text)).toEqual(["room", "Grok"]);
    expect(highlight("Ninety percent is fine for now.", "room for Grok Bot", false)).toEqual([{ text: "Ninety percent is fine for now.", hit: false }]);
  });

  it("marks the phrase only for exact words", () => {
    expect(highlight("room and Grok", "room Grok", true)).toEqual([{ text: "room and Grok", hit: false }]);
  });

  it("does not choke on characters that mean something in a pattern", () => {
    expect(highlight("a (b) c", "(b)", false).some((p) => p.hit && p.text === "(b)")).toBe(true);
  });
});

describe("search and conversation reads", () => {
  it("reads rows", () => {
    const out = readSearch(JSON.stringify({ ok: true, results: [{ kind: "episode", id: 1, title: "a" }, { kind: "topic", id: "t" }] }));
    expect(out.kind === "rows" && out.rows.map((r) => r.id)).toEqual([1]);
  });

  it("reads the refusal when only-my-words cannot run offline, and is not a no-match", () => {
    const out = readSearch(JSON.stringify({ ok: false, error: "searching only your words needs the hub; the offline snapshot cannot do it" }));
    expect(out).toMatchObject({ kind: "error", needsHub: true });
  });

  it("reads any other failure as a failure", () => {
    expect(readSearch("not json")).toMatchObject({ kind: "error", needsHub: false });
    expect(readSearch(JSON.stringify({ ok: true }))).toMatchObject({ kind: "error" });
  });

  it("takes what you said from the owner's lines, and hides an empty summary", () => {
    const c = conversationOf({ summary: "About.", decisions: ["Do it", { text: "And this" }, 5], verbatim: { quotes: ["one", " ", "two"], commands: ["ls"] } });
    expect(c).toEqual({ about: "About.", said: ["one", "two"], decided: ["Do it", "And this"] });
    expect(conversationOf({ summary: "No summary", summary_kind: "verbatim_only", verbatim: {} })!.about).toBeNull();
  });
});
