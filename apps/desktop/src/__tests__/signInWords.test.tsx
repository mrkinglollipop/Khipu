// Sign-in state and outcome words.
// Moved in slice A11 from RoutingPriority.test.tsx when its screen was removed; these test helpers
// that live code still uses, and need no removed screen.

(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import { liveWords, signInOutcome } from "../PlansCard";
import type { UsageAccount } from "../PlansCard";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

const NOW = new Date("2026-10-15T14:00:00Z");
const TWO_MIN_AGO = "2026-10-15T13:58:00Z";
const SECOND = "claude|/logins/claude-second";

function row(extra: Partial<UsageAccount>): UsageAccount {
  return { harness: "claude", account: "Claude", home: null, ok: true, error: null,
    windows: [{ name: "weekly", used_pct: 10, resets_at: null }],
    probed_at: TWO_MIN_AGO, last_attempt_at: TWO_MIN_AGO, ...extra };
}

type Answer = string | object | ((args: Record<string, unknown> | undefined) => unknown);
let answers: Record<string, Answer>;

// Settings clarity phase 3 folds Settings -> Accounts and routing into
// groups; these tests read every group and account line open.
const ALL_OPEN = [
  "accounts", "priority", "rules", "holds", "telling", "resets", "runners", "table", "changes", "sub:hears", "sub:sizes",
  ...["claude|", SECOND, "codex|", "grok|"].map((k) => `account:${k}`),
];

// jsdom here has no usable localStorage; an in-memory one stands in.
function memoryStorage() {
  const m = new Map<string, string>();
  return {
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => void m.set(k, String(v)),
    removeItem: (k: string) => void m.delete(k),
    clear: () => m.clear(),
  };
}

beforeEach(() => {
  vi.stubGlobal("localStorage", memoryStorage());
  localStorage.setItem("khipu.routing.open", JSON.stringify(ALL_OPEN));
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  answers = {};
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    const a = answers[cmd] ?? { ok: true };
    const out = typeof a === "function" ? a(args) : a;
    return typeof out === "string" ? out : JSON.stringify(out);
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  invokeMock.mockReset();
});

// ---------------------------------------------------------------------------
// Plans: whether Khipu can read each account, and Sign in again
// ---------------------------------------------------------------------------

describe("each account says whether Khipu can read it", () => {
  it("words every state in plain words", () => {
    expect(liveWords(row({}))).toEqual({ text: "Read 2 min ago", live: true });
    expect(liveWords(row({ ok: false, error: "no_plan_limits" })).text).toBe("Signed in, but its plan figures can't be read");
    expect(liveWords(row({ ok: false, error: "not_signed_in" })).text).toBe("Not signed in");
    expect(liveWords(row({ ok: false, error: "not_installed" })).text).toBe("CLI not installed");
    // Codex's last-turn figures after a signed-out live read are not live.
    expect(liveWords(row({ harness: "codex", live_error: "not_signed_in" })).text).toBe("Not signed in");
    expect(liveWords(row({ ok: false, error: "timeout" })).text).toBe("Can't be read just now (didn't answer in time)");
  });

  it("says a sign-in left open or refused in plain words", () => {
    expect(signInOutcome({ ok: false, error: "x", refusal: { ok: false, error: "sign_in_timeout" } })).toBe(
      "Khipu stopped waiting after 15 minutes. Press Sign in again to start over.");
    expect(signInOutcome({ ok: false, error: "x", refusal: { ok: false, error: "exit_1",
      row: row({ ok: false, error: "not_signed_in" }) } })).toBe("The sign-in didn't finish. Not signed in.");
    expect(signInOutcome({ ok: false, error: "khipu exited 2" })).toBe("The sign-in didn't start: khipu exited 2.");
  });

});
