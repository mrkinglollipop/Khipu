// Rule names and account state sentences.
// Moved in slice A11 from RoutingCard.test.tsx when its screen was removed; these test helpers
// that live code still uses, and need no removed screen.

(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup } from "@testing-library/react";
// The stylesheet as written (vitest hands CSS imports over empty, `?raw`
// included). The app has no @types/node; vitest runs this file in node.
// @ts-expect-error node:fs has no types here
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
// @ts-expect-error node:child_process has no types here
import { execFileSync } from "node:child_process";
// @ts-expect-error node:os has no types here
import { tmpdir } from "node:os";

import { accountSource, accountState, midSentence, ruleWords } from "../routing";
import type { RouteAccount } from "../routing";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  invokeMock.mockReset();
});

function acct(label: string, harness: string, extra: Partial<RouteAccount>): RouteAccount {
  return {
    key: `${harness}|${label === "Claude second" ? "/logins/claude-second" : ""}`,
    label,
    harness,
    state: "unknown",
    checkable: harness !== "muse",
    models: [],
    witness: null,
    disagreement: null,
    last_seen: null,
    ...extra,
  };
}

describe("rule names (from the Home routing card's final fix round)", () => {
  it("keeps a rule's opening name as written and lower-cases a common opening word (C2)", () => {
    expect(ruleWords("Never a -fast tier.")).toBe("never a -fast tier");
    expect(ruleWords("Grok never builds or fixes privacy or safety code.")).toBe("Grok never builds or fixes privacy or safety code");
    expect(ruleWords("Khipu runs in Aegis only through Aegis's own pack.")).toBe("Khipu runs in Aegis only through Aegis's own pack");
    expect(ruleWords("API keys stay on this Mac.")).toBe("API keys stay on this Mac");
  });

});

describe("Home Routing card, phase 5 fix round 2", () => {
  it("says a reset check's outcome in the state, never 'not checked yet' beside it (R2-2)", () => {
    const now = new Date("2026-10-14T15:22:00Z");
    const reasons = [
      "Khipu read the same limit again",
      "Khipu could not read this account's usage",
      "The Mac is busy; Khipu will try again",
      "Another Khipu usage check is running; Khipu will try again",
    ];
    for (const reason of reasons) {
      const a = acct("Claude", "claude", {
        state: "probably_open", until: "2026-10-14T15:20:00Z", basis: "cli",
        witness: { source: "check", written_by: { kind: "check" }, at: "2026-10-14T15:22:00Z" },
        reset_check: { status: "same_limit", checks: 1, reason },
      });
      const state = accountState(a, now).text;
      expect(state).toBe(`Its reset time passed at 11:20 AM; ${midSentence(reason)}`);
      expect(state).not.toContain("not checked yet");
      const source = accountSource(a, now);
      expect(source).toBe("Checked by Khipu just now");
      expect(source).not.toContain(reason);
    }
    // A later limit the check read still names its reason under the limit.
    const later = acct("Claude", "claude", {
      state: "limited", until: "2026-10-14T17:00:00Z", basis: "cli",
      witness: { source: "check", written_by: { kind: "check" }, at: "2026-10-14T15:22:00Z" },
      reset_check: { status: "later_limit", checks: 1, reason: "Khipu read a later reset time" },
    });
    expect(accountSource(later, now)).toBe("Checked by Khipu just now · Khipu read a later reset time");
  });

  it("lower-cases a reset reason's first word after the semicolon unless it is a name", () => {
    expect(midSentence("This account can't be checked without using a turn"))
      .toBe("this account can't be checked without using a turn");
    expect(midSentence("The Mac is busy; Khipu will try again")).toBe("the Mac is busy; Khipu will try again");
    expect(midSentence("Another Khipu usage check is running")).toBe("another Khipu usage check is running");
    expect(midSentence("Khipu read the same limit again")).toBe("Khipu read the same limit again");
    expect(midSentence("Claude said nothing")).toBe("Claude said nothing");
    expect(midSentence("Thesis words stay")).toBe("Thesis words stay");
    expect(midSentence("This")).toBe("this");
    const a = acct("Claude second", "claude", {
      state: "probably_open", until: "2026-10-14T15:20:00Z", basis: "cli",
      witness: { source: "check", written_by: { kind: "check" }, at: "2026-10-14T15:22:00Z" },
      reset_check: { status: "cannot_check", checks: 0, reason: "This account can't be checked without using a turn" },
    });
    const state = accountState(a, new Date("2026-10-14T15:22:00Z")).text;
    expect(state).toBe("Its reset time passed at 11:20 AM; this account can't be checked without using a turn");
    expect(state).not.toContain("; This");
  });

});
