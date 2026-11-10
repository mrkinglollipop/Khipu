// Plain words for plans, waits, errors and capture causes.
// Moved in slice A11 from ClickableHome.test.tsx when its screen was removed; these test helpers
// that live code still uses, and need no removed screen.

(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import { buildAttention } from "../doctorAttention";
import { captureCauseWords, longSpan, planWord, plainError, toastWords, waitedWords } from "../humanWords";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

// Mon 2026-03-09, 3:00 PM in New York.
const NOW = new Date("2026-03-09T19:00:00Z");

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
  vi.useRealTimers();
});

describe("plain words", () => {
  it("never prints an id or a lower-case word as a plan", () => {
    expect(planWord("max")).toBe("Max");
    expect(planWord("40000000000000002")).toBe("");
    expect(planWord(null)).toBe("");
  });

  it("says nothing about a wait it has no number for, instead of 0 minutes", () => {
    expect(waitedWords(undefined)).toBe("");
    expect(waitedWords(0)).toBe("");
    expect(waitedWords(1725)).toBe("1 day 5 hours");
    expect(longSpan(45 * 3600)).toBe("1 day 21 hours");
    const out = buildAttention({ topics_embed_lag_ok: false, topics_embed_lag: {} });
    const item = out.items.find((i) => i.key === "topics_embed_lag")!;
    expect(item.title).toBe("A note is waiting to become findable by meaning");
    expect(item.cause).not.toMatch(/0 minutes|waited/);
    const slow = buildAttention({ topics_embed_lag_ok: false, topics_embed_lag: { lag_minutes: 90 } });
    expect(slow.items[0].cause).toContain("The oldest one has waited 90 minutes.");
  });

  it("turns raw errors and toasts into what failed", () => {
    expect(toastWords("khipu exited 1: error: connection timeout expired")).toBe(
      "Khipu couldn't reach the database just now (it took too long). It tries again at the next refresh.");
    expect(toastWords("khipu exited 2: something odd")).toBe(
      "Khipu couldn't finish that. Try again in a moment; the details are in the tooltip.");
    expect(toastWords("khipu exited 1: Traceback (most recent call last)\n  File x\nValueError: bad")).toContain("ValueError");
    expect(plainError("hub snapshot (layout 9; this version uses 7)")).toBe(
      "This Mac's offline copy was made by a newer Khipu than the one running. It is rebuilt at the next check.");
    expect(plainError("(layout 3; this version uses 7)")).toContain("older Khipu");
    expect(plainError("plain words stay")).toBe("plain words stay");
  });

  it("words a stuck capture without the raw model text", () => {
    const out = captureCauseWords("last capture attempt failed: extract: RuntimeError: extract: model returned non-JSON: '{ \"a\": 1'; 2 job(s) waiting 1725 min — nothing is draining them");
    expect(out).toBe("The last session couldn't be saved: the summariser sent back something Khipu couldn't read. 2 sessions are waiting to be saved; they have waited 1 day 5 hours");
    expect(out).not.toMatch(/RuntimeError|non-JSON|job\(s\)/);
    const attention = buildAttention({
      capture_liveness: { ok: false, red: ["claude_code"], harnesses: { claude_code: { reasons: ["2 job(s) waiting 90 min — nothing is draining them"] } } },
    });
    expect(attention.items[0].cause).toContain("2 sessions are waiting to be saved; they have waited 90 minutes");
  });

});

describe("review round 2", () => {
  it("says the real cause of a failed capture, and the summariser sentence only for non-JSON", () => {
    expect(captureCauseWords("last capture attempt failed: provider down")).toBe("The last session couldn't be saved: provider down");
    expect(captureCauseWords("last capture attempt failed: extract: HTTPError 429; 1 job(s) waiting 90 min — nothing is draining them")).toBe(
      "The last session couldn't be saved: HTTPError 429. A session is waiting to be saved; it has waited 90 minutes");
    expect(captureCauseWords("last capture attempt failed: capture rc=1")).toContain("capture rc=1");
    expect(captureCauseWords("last capture attempt failed: extract: model returned non-JSON: 'x'")).toContain("summariser sent back");
    const out = buildAttention({
      capture_liveness: { ok: false, red: ["claude_code"], harnesses: { claude_code: { reasons: ["last capture attempt failed: provider down"] } } },
    });
    expect(out.items[0].cause).toContain("provider down");
    expect(out.items[0].cause).not.toContain("summariser");
  });

  it("calls only the database's own timeout a database problem", () => {
    expect(plainError("lock timeout expired")).toBe("lock timeout expired");
    expect(plainError("could not connect to the model host")).toBe("could not connect to the model host");
    expect(plainError("connection timeout expired")).toContain("couldn't reach the database");
    expect(toastWords("khipu exited 1: error: lock timeout expired")).toBe(
      "Khipu couldn't finish that. Try again in a moment; the details are in the tooltip.");
  });

});
