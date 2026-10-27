import { describe, expect, it } from "vitest";
import { grokBotProgress, guideStates, railBadge } from "../guidesModel";
import { capSentence, capSetArgs, emptyCapDraft } from "../accountControlsModel";

describe("Guides row states", () => {
  const base = { liveness: null, gatewayCheckOk: false, episodes: null, accountsRead: 0, agentAsked: false };
  const grok = grokBotProgress({ gatewayUrl: "", keySaved: null, check: null, ack: 0 });

  it("does not call health clear while the harness check has not answered", () => {
    const states = guideStates({ ...base, grok, problems: null });
    expect(states.red).toEqual({ tone: "neutral", label: "Checking" });
    expect(railBadge(grok, null)).toBe(0);
  });

  it("still says No problems once health answered with none", () => {
    expect(guideStates({ ...base, grok, problems: 0 }).red.label).toBe("No problems");
  });

  it("the T3 row reads the T3 health gap rather than a fixed Covered", () => {
    expect(guideStates({ ...base, grok, problems: 0 }).t3.label).toBe("Covered");
    const warn = guideStates({ ...base, grok, problems: 0, t3: { warning: true, lookupFailed: false } }).t3;
    expect(warn.tone).toBe("warn");
    expect(warn.label).not.toBe("Covered");
    const failed = guideStates({ ...base, grok, problems: 0, t3: { warning: true, lookupFailed: true } }).t3;
    expect(failed.tone).toBe("err");
  });

  it("the T3 label and its description agree: never Needs a look beside Nothing extra to do", () => {
    const calm = guideStates({ ...base, grok, problems: 0 }).t3;
    expect(calm.blurb).toBeUndefined();
    // The live case 2026-10-27: antigravity in T3 has no Khipu hooks.
    const hooks = guideStates({ ...base, grok, problems: 0, t3: { warning: true, lookupFailed: false, reason: "hooks" } }).t3;
    expect(hooks.tone).toBe("warn");
    expect(hooks.label).toBe("A provider has no hooks");
    expect(hooks.blurb).toContain("no Khipu hooks");
    expect(hooks.blurb).not.toMatch(/Nothing extra to do/);
    for (const reason of ["unlinked", "partial", "other"] as const) {
      const row = guideStates({ ...base, grok, problems: 0, t3: { warning: true, lookupFailed: false, reason } }).t3;
      expect(row.blurb, reason).toBeTruthy();
      expect(row.blurb).not.toMatch(/Nothing extra to do/);
    }
  });

  it("Grok Bot is not Connected on two clicks and a gateway ping", () => {
    const p = grokBotProgress({
      gatewayUrl: "https://gw.example.test", keySaved: true, ack: 4,
      check: { ok: true, address: "https://gw.example.test" } as never,
    });
    const states = guideStates({ ...base, grok: p, problems: 0 });
    expect(states.grok_bot.label).toBe("Ready to test");
  });
});

describe("Account caps", () => {
  it("a per-day cap is saved on the weekly window even if the five-hour window was picked before switching kind", () => {
    const d = { ...emptyCapDraft(), sentence: "per_day" as const, window: "five_hour" as const, pct: "40" };
    const out = capSetArgs(d);
    expect("cap" in out).toBe(true);
    if ("cap" in out) {
      expect(out.cap.window).toBe("weekly");
      expect(capSentence({ kind: "per_day", window: out.cap.window, pct: 40 } as never)).toContain("week");
    }
  });

  it("a share cap keeps the window the user picked", () => {
    const out = capSetArgs({ ...emptyCapDraft(), sentence: "share", window: "five_hour", pct: "40" });
    expect("cap" in out && out.cap.window).toBe("five_hour");
  });
});
