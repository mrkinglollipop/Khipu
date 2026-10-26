// The account controls' plain-data rules: what a cap form sends, the priority
// rebuilt with one account changed, the log line an Undo finds, and the quota
// host check. Every value is made up.

import { describe, expect, it } from "vitest";
import {
  capRemoveArgs,
  capSentence,
  capSetArgs,
  charactersWords,
  checkedHost,
  draftFromCap,
  emptyCapDraft,
  findNewAction,
  movedBy,
  withAccountLast,
  withRole,
  withoutAccount,
} from "../accountControlsModel";
import type { LoggedAction } from "../accountControlsModel";
import type { CapStatus, RoutePriorityEntry } from "../routing";

const entry = (account: string, role: string | null = null, until: string | null = null): RoutePriorityEntry =>
  ({ account, label: account, role, until }) as RoutePriorityEntry;

describe("what a cap form sends", () => {
  it("sends a share of the week or the five hours, 1 to 100 percent", () => {
    expect(capSetArgs({ ...emptyCapDraft(), sentence: "share", pct: "95" })).toEqual({ cap: { kind: "share", window: "weekly", pct: 95 } });
    expect(capSetArgs({ ...emptyCapDraft(), sentence: "share", pct: "40", window: "five_hour" })).toEqual({
      cap: { kind: "share", window: "five_hour", pct: 40 },
    });
    for (const pct of ["", "0", "101", "-3", "abc"]) {
      expect(capSetArgs({ ...emptyCapDraft(), sentence: "share", pct })).toEqual({ error: "A percent from 1 to 100." });
    }
  });

  it("names the week for a per-day cap, which the CLI needs", () => {
    expect(capSetArgs({ ...emptyCapDraft(), sentence: "per_day", pct: "20" })).toEqual({ cap: { kind: "per_day", window: "weekly", pct: 20 } });
  });

  it("sends an even pace at 100 percent with its slack, and a check cap only as an even pace", () => {
    expect(capSetArgs({ ...emptyCapDraft(), sentence: "pace_target", slack: "5" })).toEqual({
      cap: { kind: "pace_target", window: "weekly", pct: 100, slack: 5, purpose: "job" },
    });
    expect(capSetArgs({ ...emptyCapDraft(), sentence: "pace_target", slack: "0", purpose: "check", tier: "sonnet", sensitiveOnly: true })).toEqual({
      cap: { kind: "pace_target", window: "weekly", pct: 100, slack: 0, purpose: "check", tier: "sonnet", sensitive_only: true },
    });
    // Tier and sensitivity belong to checks: a job cap never carries them.
    const job = capSetArgs({ ...emptyCapDraft(), sentence: "pace_target", slack: "2", tier: "opus", sensitiveOnly: true });
    expect(job).toEqual({ cap: { kind: "pace_target", window: "weekly", pct: 100, slack: 2, purpose: "job" } });
    expect(capSetArgs({ ...emptyCapDraft(), sentence: "pace_target", slack: "101" })).toHaveProperty("error");
    expect(capSetArgs({ ...emptyCapDraft(), sentence: "pace_target", slack: "-1" })).toHaveProperty("error");
  });

  it("checks dollars, days and the number of jobs", () => {
    expect(capSetArgs({ ...emptyCapDraft(), sentence: "spend", usd: "35", days: "7" })).toEqual({ cap: { kind: "spend", usd: 35, days: 7 } });
    expect(capSetArgs({ ...emptyCapDraft(), sentence: "spend", usd: "0", days: "7" })).toEqual({ error: "Dollars above 0." });
    expect(capSetArgs({ ...emptyCapDraft(), sentence: "spend", usd: "5", days: "367" })).toEqual({ error: "Days from 1 to 366." });
    expect(capSetArgs({ ...emptyCapDraft(), sentence: "concurrency", n: "2" })).toEqual({ cap: { kind: "concurrency", n: 2 } });
    for (const n of ["", "0", "1.5", "x"]) {
      expect(capSetArgs({ ...emptyCapDraft(), sentence: "concurrency", n })).toEqual({ error: "A whole number of jobs, at least 1." });
    }
  });

  it("fills the form from a cap and removes it by the fields that name it", () => {
    const pace: CapStatus = { kind: "pace_target", purpose: "check", tier: "haiku", sensitive_only: true, window: "weekly", pct: 100, slack: 5 };
    expect(draftFromCap(pace)).toMatchObject({ sentence: "pace_target", purpose: "check", tier: "haiku", sensitiveOnly: true, slack: "5", window: "weekly" });
    expect(capRemoveArgs(pace)).toEqual({ kind: "pace_target", window: "weekly", purpose: "check" });
    expect(capRemoveArgs({ kind: "spend", cap_usd: 35, days: 7 })).toEqual({ kind: "spend" });
    expect(capRemoveArgs({ kind: "share", window: "five_hour", pct: 50 })).toEqual({ kind: "share", window: "five_hour" });
    expect(draftFromCap({ kind: "spend", cap_usd: 35, days: 7 })).toMatchObject({ sentence: "spend", usd: "35", days: "7" });
  });

  it("says a cap in one plain sentence", () => {
    expect(capSentence({ kind: "share", window: "weekly", pct: 95 })).toBe("At most 95% of the week");
    expect(capSentence({ kind: "pace_target", purpose: "check", slack: 5 })).toBe("Checks: up to 5 points over an even pace");
    expect(capSentence({ kind: "concurrency", cap_n: 2 })).toBe("At most 2 jobs at once");
  });
});

describe("the priority with one account changed", () => {
  const list = [entry("a|", "first", "2026-11-01T00:00:00Z"), entry("b|", "second"), entry("c|")];

  it("changes only the words, keeping every end", () => {
    expect(withRole(list, "b|", "  new words  ")).toEqual([
      { account: "a|", role: "first", until: "2026-11-01T00:00:00Z" },
      { account: "b|", role: "new words", until: null },
      { account: "c|", role: null, until: null },
    ]);
    expect(withRole(list, "a|", "   ")[0]).toEqual({ account: "a|", role: null, until: "2026-11-01T00:00:00Z" });
  });

  it("moves one place, and not past either end", () => {
    expect(movedBy(list, "b|", -1)?.map((e) => e.account)).toEqual(["b|", "a|", "c|"]);
    expect(movedBy(list, "b|", 1)?.map((e) => e.account)).toEqual(["a|", "c|", "b|"]);
    expect(movedBy(list, "a|", -1)).toBeNull();
    expect(movedBy(list, "c|", 1)).toBeNull();
    expect(movedBy(list, "zzz|", 1)).toBeNull();
  });

  it("removes an account, or puts one at the end with no words", () => {
    expect(withoutAccount(list, "b|").map((e) => e.account)).toEqual(["a|", "c|"]);
    expect(withAccountLast(list, "d|").slice(-1)[0]).toEqual({ account: "d|", role: null, until: null });
    expect(withAccountLast(list, "a|").map((e) => e.account)).toEqual(["b|", "c|", "a|"]);
  });

  it("counts the words' characters the way a person would", () => {
    expect(charactersWords("")).toBe("0 characters");
    expect(charactersWords("a")).toBe("1 character");
    expect(charactersWords("x".repeat(1412))).toBe("1,412 characters");
  });
});

describe("finding the line a write just made", () => {
  const line = (id: string, action: string, extra: Partial<LoggedAction> = {}): LoggedAction => ({ id, action, auth: "app", ...extra });

  it("takes the newest app line of this action that was not there before", () => {
    const after = [line("n2", "route.pause", { target: "b|" }), line("n1", "route.pause", { target: "a|" }), line("old", "route.pause", { target: "a|" })];
    expect(findNewAction(after, new Set(["old"]), "route.pause", "a|")?.id).toBe("n1");
    expect(findNewAction(after, new Set(["old"]), "route.pause")?.id).toBe("n2");
  });

  it("never picks an old line, another action, a direction's line, an undone or no-undo line", () => {
    const after = [
      line("seen", "route.cap.set"),
      line("other", "route.cap.remove"),
      line("dir", "route.cap.set", { auth: "direction" }),
      line("undone", "route.cap.set", { state: "undone" }),
      line("failed", "route.cap.set", { state: "failed" }),
      line("noundo", "route.cap.set", { no_undo: "cannot" }),
    ];
    expect(findNewAction(after, new Set(["seen"]), "route.cap.set")).toBeNull();
  });
});

describe("the quota host", () => {
  it("takes a bare address, with or without https://", () => {
    expect(checkedHost("api.example.com")).toEqual({ host: "api.example.com" });
    expect(checkedHost("  https://api.example.com/  ")).toEqual({ host: "api.example.com" });
  });

  it("refuses a path, a port, spaces or nothing", () => {
    for (const bad of ["", "api.example.com/usage", "api.example.com:8443", "api example.com", "a..b", "-x.com"]) {
      expect(checkedHost(bad)).toHaveProperty("error");
    }
  });
});
