// Counting words shared across the app.
// Moved in slice A11 from Work.test.tsx when its screen was removed; these test helpers
// that live code still uses, and need no removed screen.

(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

// @ts-expect-error node:fs has no types here
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import { ageWords,
  plural } from "../work";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
});

const NOW = Date.now();
const ago = (ms: number) => new Date(NOW - ms).toISOString();
const S = 1000;
const MIN = 60 * S;
const H = 60 * MIN;
const DAY = 24 * H;

describe("work.ts words", () => {
  it("plural counts one and many", () => {
    expect(plural(1, "row")).toBe("1 row");
    expect(plural(2, "row")).toBe("2 rows");
    expect(plural(1120, "row")).toBe("1,120 rows");
    expect(plural(1, "needs you", "need you")).toBe("1 needs you");
    expect(plural(9, "needs you", "need you")).toBe("9 need you");
  });

  it("ageWords counts minutes, hours, then days", () => {
    const now = new Date(NOW);
    expect(ageWords(ago(10 * S), now)).toBe("just now");
    expect(ageWords(ago(18 * MIN), now)).toBe("18 min");
    expect(ageWords(ago(5 * H), now)).toBe("5 h");
    expect(ageWords(ago(1 * DAY + H), now)).toBe("1 day");
    expect(ageWords(ago(3 * DAY), now)).toBe("3 days");
    expect(ageWords(null, now)).toBe("");
  });

});
