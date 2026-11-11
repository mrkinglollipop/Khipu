// Accounts › Room right now, in words (slice A4): what the list and page say from
// the routing record. Every value is made up. Each rule here is one of the slice's:
// numbers are Khipu's own, an unknown state is never room, a limit that is not one
// "stop at" level is never dropped.
(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

import { describe, expect, it } from "vitest";
import {
  accountWindows,
  grantEnds,
  orderedAccounts,
  pauseEnds,
  readLine,
  rowModel,
  ruleSentences,
  stopAt,
  stopAtLine,
  stopAtOptions,
} from "../accounts/accountsModel";
import type { Pace, RouteAccount, RouteView } from "../routing";

it("shows a successful usage row's same-account note in the existing warning line", () => {
  const a = account();
  const note = "Reads the same account as Claude second: both folders are signed in to one account. Sign one of them in to the other account.";
  expect(rowModel(a, { accounts: [a] }, { accounts: [
    { key: a.key, ok: true, same_account_as: "Claude second", note },
  ] }, 0, NOW)).toMatchObject({ kind: "room", warning: note, word: "On pace", tone: "warn", left: 62 });
  expect(rowModel(a, { accounts: [a] }, { accounts: [
    { key: a.key, ok: true },
  ] }, 0, NOW).word).not.toBe("Same account");
});

it.each([
  ["limited", "Out of room", "err"], ["paused", "Paused", "none"], ["down", "Down", "err"],
])("keeps a duplicate account's %s state and reading", (state, word, tone) => {
  const a = account({ state, until: at(70) });
  const note = "Reads the same account as Claude second: both folders are signed in to one account.";
  const r = rowModel(a, view([a]), { accounts: [{ key: a.key, ok: true, same_account_as: "Claude second", note }] }, 0, NOW);
  expect(r).toMatchObject({ kind: state, word, tone, warning: note, left: null });
  expect(accountWindows(a, view([a]), NOW)).toHaveLength(1);
});

it("keeps the sign-in state beside a same-account warning", () => {
  const a = account();
  expect(rowModel(a, view([a]), { accounts: [{ key: a.key, ok: false, error: "not_signed_in",
    same_account_as: "Claude second", note: "Same account warning" }] }, 0, NOW))
    .toMatchObject({ kind: "signin", word: "Needs sign-in", warning: "Same account warning", left: null });
});

const NOW = new Date("2026-11-10T19:00:00Z"); // 2:00 PM in New York
const at = (min: number) => new Date(NOW.getTime() + min * 60_000).toISOString();

const win = (window: string, used: number, resetsInMin: number, verdict: string, extra: Partial<Pace> = {}): Pace => ({
  window, used_pct: used, cycle_resets_at: at(resetsInMin), plan: { verdict }, ...extra,
});
const account = (over: Partial<RouteAccount> = {}): RouteAccount => ({
  key: "claude|", label: "Claude", harness: "claude", state: "open", pace: [win("weekly", 38, 3000, "on_pace")], cap_statuses: [], ...over,
});
const view = (accounts: RouteAccount[], over: Partial<RouteView> = {}): RouteView => ({ accounts, record: true, checked_at: at(-3), ...over });

describe("a row says what Khipu says, and no more", () => {
  it("shows percent left, the reset time and the pace word from the record", () => {
    const a = account({ pace: [win("five_hour", 60, 70, "outpacing")] });
    const r = rowModel(a, view([a]), null, 0, NOW);
    expect(r.kind).toBe("room");
    expect(r.left).toBe(40);
    expect(r.line).toBe("40% left of the next 5 hours · resets 3:10 PM");
    expect(r.word).toBe("Ahead of pace");
    expect(r.tone).toBe("warn");
  });

  it("speaks for the window with the least left", () => {
    const a = account({ pace: [win("five_hour", 10, 100, "underpacing"), win("weekly", 90, 3000, "on_pace")] });
    const r = rowModel(a, view([a]), null, 0, NOW);
    expect(r.left).toBe(10);
    expect(r.line).toContain("of the week");
  });

  it("makes no forecast of its own: a run-out note appears only when Khipu gave a time", () => {
    const quiet = account({ pace: [win("five_hour", 60, 70, "outpacing")] });
    expect(accountWindows(quiet, view([quiet]), NOW)[0].note).toBeNull();
    const told = account({ pace: [win("five_hour", 60, 70, "outpacing", { plan: { verdict: "outpacing", runs_out_at: at(50), before_reset_s: 1200 } })] });
    expect(accountWindows(told, view([told]), NOW)[0].note).toBe("Khipu reads it running out 2:50 PM, 20 minutes before it resets.");
  });

  it("does not use a window whose reset time has passed", () => {
    const a = account({ pace: [win("weekly", 38, -5, "on_pace")] });
    expect(accountWindows(a, view([a]), NOW)).toEqual([]);
    expect(rowModel(a, view([a]), null, 0, NOW).kind).toBe("noreading");
  });

  it("falls back to the plan fact when there is no pace, and says the pace is not known", () => {
    const a = account({ pace: [] });
    const v = view([a], { plan_facts: { "claude|": { readable: true, left_pct: 58, window: "weekly", resets_at: at(3000) } } });
    const r = rowModel(a, v, null, 0, NOW);
    expect(r.left).toBe(58);
    expect(r.word).toBe("Pace not known");
  });
});

describe("each plain state", () => {
  it("an account with no reading yet says so and draws no meter", () => {
    const a = account({ pace: [] });
    const r = rowModel(a, view([a]), null, 0, NOW);
    expect(r).toMatchObject({ kind: "noreading", word: "No reading yet", left: null });
  });

  it("a paused account says so with its end", () => {
    const a = account({ state: "paused", until: at(60 * 24) });
    const r = rowModel(a, view([a]), null, 0, NOW);
    expect(r).toMatchObject({ kind: "paused", word: "Paused", left: null });
    expect(r.line).toMatch(/^Paused by you until tomorrow 2:00 PM$/);
  });

  it("an account at its limit says out of room and when it is back", () => {
    const a = account({ state: "limited", until: at(70) });
    const r = rowModel(a, view([a]), null, 0, NOW);
    expect(r).toMatchObject({ kind: "limited", word: "Out of room", tone: "err", left: null });
    expect(r.line).toMatch(/back at 3:10 PM/);
  });

  it("an account whose sign-in ran out is not shown as having room, whatever its last reading was", () => {
    const a = account({ pace: [win("weekly", 8, 3000, "underpacing")] });
    const plans = { accounts: [{ key: "claude|", harness: "claude", ok: false, error: "not_signed_in", windows: [] }] };
    const r = rowModel(a, view([a]), plans, 0, NOW);
    expect(r).toMatchObject({ kind: "signin", word: "Needs sign-in", left: null });
  });

  it("a state Khipu does not know is never room", () => {
    for (const state of ["unknown", "something_new", ""]) {
      const a = account({ state });
      const r = rowModel(a, view([a]), null, 0, NOW);
      expect(r.kind).toBe("unknown");
      expect(r.left).toBeNull();
      expect(r.word).toBe("Not known");
    }
  });

  it("an account that is only probably open is not drawn as room", () => {
    const a = account({ state: "probably_open", until: at(-20) });
    expect(rowModel(a, view([a]), null, 0, NOW)).toMatchObject({ kind: "probably_open", left: null });
  });

  it("counts the conversations running ahead", () => {
    const a = account();
    expect(rowModel(a, view([a]), null, 2, NOW).ahead).toBe(2);
  });
});

describe("where agents stop: one level, or the owner's rules, never dropped", () => {
  const share = (window: string, pct: number) => ({ kind: "share", window, pct });

  it("no limit at all is a level of none", () => {
    expect(stopAt(account())).toMatchObject({ kind: "level", pct: null, others: [] });
    expect(stopAtLine(stopAt(account()))).toBeNull();
  });

  it("one share limit is the level and marks the meter", () => {
    const a = account({ pace: [win("weekly", 81, 3000, "on_pace")], cap_statuses: [share("weekly", 90)] });
    expect(stopAt(a)).toMatchObject({ kind: "level", pct: 90, window: "weekly" });
    const r = rowModel(a, view([a]), null, 0, NOW);
    expect(r.stopAtPct).toBe(90);
    expect(r.sub).toBe("Agents stop at 90%");
  });

  it("two share limits cannot be one level: set by your rules, and every one is listed", () => {
    const a = account({ cap_statuses: [share("five_hour", 60), share("weekly", 91)] });
    const s = stopAt(a);
    expect(s.kind).toBe("rules");
    expect(stopAtLine(s)).toBe("Set by your rules");
    expect(ruleSentences((s as { caps: unknown[] }).caps as never)).toEqual(["At most 60% of the five hours", "At most 91% of the week"]);
  });

  it("a different kind of limit stays beside the level instead of being dropped", () => {
    const even = { kind: "pace_target", purpose: "job" as const, window: "weekly", pct: 100, slack: 5 };
    const a = account({ cap_statuses: [share("weekly", 90), even] });
    const s = stopAt(a);
    expect(s).toMatchObject({ kind: "level", pct: 90 });
    expect((s as { others: unknown[] }).others).toHaveLength(1);
    expect(stopAtLine(stopAt(account({ cap_statuses: [even] })))).toBe("Also set by your rules");
  });

  it("offers the usual levels and keeps the account's own level when it is none of them", () => {
    expect(stopAtOptions(null).map((o) => o.label)).toEqual(["No limit, use it all", "At 80%", "At 85%", "At 90%", "At 95%"]);
    expect(stopAtOptions(70).map((o) => o.label)).toContain("At 70%");
    expect(stopAtOptions(70).find((o) => o.value === "70")).toBeTruthy();
  });
});

describe("ends the controls offer", () => {
  it("a run-ahead grant always has a time: no choice means no end", () => {
    const ends = grantEnds(NOW);
    expect(ends.some((c) => c.id === "never")).toBe(false);
    expect(ends.map((c) => c.id)).not.toContain("never");
    expect(ends[0].iso).toMatch(/^2026-11-10T16:00:00-05:00$/);
  });

  it("a pause may have none", () => {
    expect(pauseEnds(NOW).some((c) => c.id === "never")).toBe(true);
  });
});

describe("the list", () => {
  it("is in the owner's order, then the rest as the record lists them", () => {
    const a = account({ key: "a|", label: "A" });
    const b = account({ key: "b|", label: "B" });
    const c = account({ key: "c|", label: "C" });
    const v = view([a, b, c], { priority: { entries: [{ account: "c|", label: "C" }, { account: "a|", label: "A" }] } });
    expect(orderedAccounts(v).map((x) => x.key)).toEqual(["c|", "a|", "b|"]);
  });

  it("says how long ago the record was read", () => {
    expect(readLine(view([]), NOW)).toBe("Read 3 minutes ago.");
    expect(readLine({ accounts: [] }, NOW)).toBeNull();
  });
});
