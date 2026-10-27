import { describe, expect, it } from "vitest";
import {
  DEFAULT_PREFS,
  filterGuides,
  grokBotProgress,
  guideStates,
  hintState,
  isHttpsAddress,
  railBadge,
  startItems,
  startProgress,
} from "../guidesModel";
import type { GrokBotSignals, StartSignals } from "../guidesModel";
import { isAgentRecallHarness } from "../guideSignals";

const URL = "https://khipu.example.org";
const idle: GrokBotSignals = { gatewayUrl: "", keySaved: false, check: null, ack: 0 };
const none: StartSignals = { liveness: null, gatewayCheckOk: false, episodes: 0, accountsRead: 0, agentAsked: false };

describe("Grok Bot progress", () => {
  it("starts at step 1 with nothing done", () => {
    const p = grokBotProgress(idle);
    expect(p.current).toBe(1);
    expect(p.started).toBe(false);
    expect(p.passed).toBe(0);
  });

  it("ticks steps 1 and 2 from the saved address and key, and opens at step 3", () => {
    const p = grokBotProgress({ ...idle, gatewayUrl: URL, keySaved: true });
    expect(p.steps.map((s) => s.done)).toEqual([true, true, false, false, false]);
    expect(p.current).toBe(3);
    expect(p.passed).toBe(2);
    // An address and key alone (Aegis / Another Mac) do not mean Grok Bot setup started.
    expect(p.started).toBe(false);
    expect(p.finished).toBe(false);
  });

  it("does not take a plain http address for step 1", () => {
    expect(isHttpsAddress("http://khipu.example.org")).toBe(false);
    expect(grokBotProgress({ ...idle, gatewayUrl: "http://khipu.example.org" }).steps[0].done).toBe(false);
  });

  it("moves focus past a step the user did in Cursor but never shows it as done", () => {
    const p = grokBotProgress({ gatewayUrl: URL, keySaved: true, check: null, ack: 3 });
    expect(p.steps[2]).toEqual({ n: 3, done: false, acknowledged: true });
    expect(p.current).toBe(4);
  });

  it("is finished only when Khipu saw the gateway answer and both Cursor steps were acknowledged", () => {
    const base = { gatewayUrl: URL, keySaved: true } as const;
    const check = { ok: true, at: 1, address: URL };
    expect(grokBotProgress({ ...base, check, ack: 0 }).finished).toBe(false);
    expect(grokBotProgress({ ...base, check: { ok: false, at: 1, address: URL }, ack: 4 }).finished).toBe(false);
    expect(grokBotProgress({ ...base, check, ack: 4 }).finished).toBe(true);
  });

  it("treats a key that is still being read as not saved", () => {
    expect(grokBotProgress({ ...idle, gatewayUrl: URL, keySaved: null }).steps[1].done).toBe(false);
  });

  it("drops a passing gateway check when the saved address no longer matches", () => {
    const check = { ok: true, at: 1, address: URL };
    const p = grokBotProgress({ gatewayUrl: "https://other.example.org", keySaved: true, check, ack: 4 });
    expect(p.steps[4].done).toBe(false);
    expect(p.finished).toBe(false);
  });

  it("counts the setup as started only after an ack or a gateway check, not from an address alone", () => {
    expect(grokBotProgress({ ...idle, gatewayUrl: URL }).started).toBe(false);
    expect(grokBotProgress({ ...idle, gatewayUrl: URL, keySaved: true }).started).toBe(false);
    expect(grokBotProgress({ ...idle, gatewayUrl: URL, ack: 3 }).started).toBe(true);
    expect(grokBotProgress({ ...idle, gatewayUrl: URL, check: { ok: false, at: 1, address: URL } }).started).toBe(true);
  });
});

describe("Start here", () => {
  it("ticks each item from its own evidence", () => {
    const live = {
      ...none,
      liveness: { ok: true, harnesses: { claude_code: { ok: true, seen: true, captures: 3 } } },
      episodes: 12,
    };
    const items = startItems(live);
    expect(items.map((i) => i.done)).toEqual([true, true, false, false]);
    expect(startProgress(items)).toMatchObject({ done: 2, total: 4, next: { id: "accounts" } });
  });

  it("counts a passing Grok Bot gateway check as a connected agent", () => {
    expect(startItems({ ...none, gatewayCheckOk: true })[0].done).toBe(true);
  });

  it("does not count a harness that ran but saved nothing", () => {
    const items = startItems({ ...none, liveness: { ok: true, harnesses: { cursor: { ok: true, seen: true, captures: 0 } } } });
    expect(items[0].done).toBe(false);
  });
});

describe("Home hint", () => {
  const items = startItems(none);
  it("names the next unfinished item", () => {
    const h = hintState(items, DEFAULT_PREFS);
    expect(h).toMatchObject({ kind: "next", done: 0, total: 4, item: { id: "connect" } });
  });

  it("stays hidden once hidden, and when Start here was hidden", () => {
    expect(hintState(items, { ...DEFAULT_PREFS, hintHidden: true }).kind).toBe("hidden");
    expect(hintState(items, { ...DEFAULT_PREFS, startHidden: true }).kind).toBe("hidden");
  });

  it("says it is checking until the live evidence has been read, never a count from empty defaults", () => {
    expect(hintState(items, DEFAULT_PREFS, { prefs: true, signals: false }).kind).toBe("loading");
    // Nothing at all until the saved hide/seen choices are read, so a hidden hint never flashes.
    expect(hintState(items, DEFAULT_PREFS, { prefs: false, signals: false }).kind).toBe("hidden");
    // A hint the owner hid stays hidden even while signals load.
    expect(hintState(items, { ...DEFAULT_PREFS, hintHidden: true }, { prefs: true, signals: false }).kind).toBe("hidden");
    expect(hintState(items, DEFAULT_PREFS, { prefs: true, signals: true }).kind).toBe("next");
  });

  it("says 'You're set up' once, then folds away", () => {
    const all = startItems({ liveness: { ok: true, harnesses: { cursor: { ok: true, seen: true, captures: 1 } } }, gatewayCheckOk: false, episodes: 1, accountsRead: 1, agentAsked: true });
    expect(hintState(all, DEFAULT_PREFS).kind).toBe("all-set");
    expect(hintState(all, { ...DEFAULT_PREFS, setUpSeen: true }).kind).toBe("hidden");
  });
});

describe("rail badge and row states", () => {
  const addressOnly = grokBotProgress({ gatewayUrl: URL, keySaved: false, check: null, ack: 0 });
  const started = grokBotProgress({ gatewayUrl: URL, keySaved: false, check: null, ack: 3 });
  const finished = grokBotProgress({ gatewayUrl: URL, keySaved: true, check: { ok: true, at: 1, address: URL }, ack: 4 });

  it("counts a started, unfinished setup plus problems, never unread guides", () => {
    expect(railBadge(grokBotProgress(idle), 0)).toBe(0);
    expect(railBadge(addressOnly, 0)).toBe(0);
    expect(railBadge(started, 0)).toBe(1);
    expect(railBadge(started, 2)).toBe(3);
    expect(railBadge(finished, 0)).toBe(0);
  });

  it("shows each row's live state", () => {
    const st = guideStates({ ...none, grok: started, problems: 1, accountsRead: 5, liveness: { ok: true, harnesses: { claude_code: { ok: true, seen: true, captures: 2 } } } });
    expect(st.claude_code).toEqual({ tone: "ok", label: "Recording" });
    expect(st.cursor).toEqual({ tone: "neutral", label: "Not set up" });
    expect(st.grok_bot).toEqual({ tone: "warn", label: "2 of 5 steps" });
    expect(st.add_account.label).toBe("5 accounts");
    expect(st.red).toEqual({ tone: "err", label: "1 problem" });
    expect(st.grok_build.label).toBe("Not supported yet");
  });

  it("'When something turns red' follows doctor, not only capture liveness", () => {
    const base = { ...none, grok: started, problems: 0 };
    // Liveness has no red harness, but doctor has three problems: not "No problems".
    expect(guideStates({ ...base, doctorProblems: 3 }).red).toEqual({ tone: "err", label: "3 problems" });
    expect(guideStates({ ...base, doctorProblems: 1 }).red).toEqual({ tone: "err", label: "1 problem" });
    expect(guideStates({ ...base, doctorProblems: 0 }).red).toEqual({ tone: "ok", label: "No problems" });
    // Doctor not answered yet: no health claim at all.
    expect(guideStates({ ...base, doctorProblems: null }).red).toEqual({ tone: "neutral", label: "Checking" });
  });

  it("a harness whose last capture is over a week old reads Idle, never green Recording", () => {
    const wk = 7 * 86400;
    const live = (age: number | null) => ({ ok: true, harnesses: { aegis: { ok: true, seen: true, captures: 3, last_captured_age_s: age } } });
    expect(guideStates({ ...none, grok: started, problems: 0, liveness: live(wk + 60) }).aegis).toEqual({ tone: "neutral", label: "Idle" });
    expect(guideStates({ ...none, grok: started, problems: 0, liveness: live(wk - 60) }).aegis).toEqual({ tone: "ok", label: "Recording" });
    // Unknown age claims nothing either way.
    expect(guideStates({ ...none, grok: started, problems: 0, liveness: live(null) }).aegis).toEqual({ tone: "ok", label: "Recording" });
  });
});

describe("search", () => {
  it("matches titles and aliases", () => {
    expect(filterGuides("grok").map((g) => g.id)).toEqual(expect.arrayContaining(["grok_bot", "grok_build"]));
    expect(filterGuides("backup").map((g) => g.id)).toContain("backups");
    expect(filterGuides("key").map((g) => g.id)).toContain("grok_bot");
    expect(filterGuides("zzzz")).toEqual([]);
    expect(filterGuides("  ").length).toBeGreaterThan(10);
  });
});

describe("agent recall harness", () => {
  it("counts real agents and ignores this app and the doctor", () => {
    expect(isAgentRecallHarness("cursor")).toBe(true);
    expect(isAgentRecallHarness("mcp")).toBe(true);
    expect(isAgentRecallHarness("doctor")).toBe(false);
    expect(isAgentRecallHarness("desktop")).toBe(false);
    expect(isAgentRecallHarness("")).toBe(false);
    expect(isAgentRecallHarness(undefined)).toBe(false);
  });
});
