// The rail's health line and the Harnesses badge: a Claude home Khipu found
// with no Khipu in it is a warning, never "All harnesses recording"; a red
// heartbeat or a dead database still wins.
import { describe, expect, it } from "vitest";
import { claudeHomesGap, harnessesBadge, railHealthLine, t3HealthGap, t3WarningReason } from "../railHealth";
import type { LivenessPayload } from "../IntegrationsPanel";

const OK: LivenessPayload = { ok: true, red: [], harnesses: { claude_code: { ok: true, seen: true } } };
const NONE = { missing: 0, unreadable: 0 };
const FAILED = { missing: 0, unreadable: 0, checkFailed: true };

describe("claudeHomesGap", () => {
  it("counts found homes without Khipu, and found homes it could not read", () => {
    expect(
      claudeHomesGap({
        homes: [
          { exists: true, installed: true },
          { exists: true, installed: false },
          { exists: true, installed: false },
          { exists: true, installed: false, error: "bad json" },
          { exists: false, installed: false },
        ],
      }),
    ).toEqual({ missing: 2, unreadable: 1 });
  });

  it("is no gap when doctor has no block (older CLI)", () => {
    expect(claudeHomesGap(null)).toEqual(NONE);
    expect(claudeHomesGap(undefined)).toEqual(NONE);
    expect(claudeHomesGap({})).toEqual(NONE);
  });

  it("flags a failed check when the block carries an error", () => {
    expect(claudeHomesGap({ homes: [], error: "ImportError: nope" })).toEqual({
      missing: 0,
      unreadable: 0,
      checkFailed: true,
    });
  });
});

describe("railHealthLine", () => {
  it("stays green when every found home is installed", () => {
    expect(railHealthLine(true, OK, NONE)).toEqual({ tone: "ok", text: "All harnesses recording" });
  });

  it("does not call an explicitly failed liveness read healthy when it has no red rows", () => {
    expect(railHealthLine(true, { ok: false, red: [], harnesses: {} }, NONE)).toEqual({
      tone: "err",
      text: "Couldn't check recording",
    });
  });

  it("treats an error-bearing liveness payload as failed collection evidence", () => {
    expect(railHealthLine(true, { ok: true, red: [], harnesses: {}, error: "hub unavailable" } as never, NONE)).toEqual({
      tone: "err",
      text: "Couldn't check recording",
    });
  });

  it("does not call unseen or absent harness evidence healthy", () => {
    expect(railHealthLine(true, { ok: true, red: [], harnesses: {} }, NONE)).toEqual({
      tone: "warn",
      text: "Recording not checked",
    });
    expect(railHealthLine(true, { ok: true, red: [], harnesses: { claude_code: { ok: true, seen: false } } }, NONE).tone).toBe("warn");
  });

  it("keeps named red evidence ahead of a false aggregate verdict", () => {
    expect(railHealthLine(true, { ok: false, red: ["claude_code"], harnesses: {} }, NONE)).toEqual({
      tone: "err",
      text: "1 harness not recording",
    });
  });

  it("warns instead of reading green while a found Claude home is not installed", () => {
    expect(railHealthLine(true, OK, { missing: 1, unreadable: 0 })).toEqual({
      tone: "warn",
      text: "1 Claude home not installed",
    });
    expect(railHealthLine(true, OK, { missing: 2, unreadable: 0 })).toEqual({
      tone: "warn",
      text: "2 Claude homes not installed",
    });
    expect(railHealthLine(true, OK, { missing: 0, unreadable: 1 })).toEqual({
      tone: "warn",
      text: "1 Claude home can't be read",
    });
  });

  it("keeps known Claude-home and T3 warnings ahead of generic unproved harness state", () => {
    expect(railHealthLine(true, { ok: true, red: [], harnesses: {} }, { missing: 1, unreadable: 0 })).toEqual({
      tone: "warn",
      text: "1 Claude home not installed",
    });
    expect(railHealthLine(true, { ok: true, red: [], harnesses: {} }, NONE, { warning: true, lookupFailed: false })).toEqual({
      tone: "warn",
      text: "T3 needs a linked capture",
    });
  });

  it("warns instead of reading green when the Claude homes check failed", () => {
    expect(railHealthLine(true, OK, FAILED)).toEqual({ tone: "warn", text: "Couldn't check Claude homes" });
  });

  it("lets a red heartbeat and a dead database win over the warning", () => {
    const gap = { missing: 1, unreadable: 0 };
    expect(railHealthLine(true, { ...OK, red: ["claude_code"] }, gap)).toEqual({
      tone: "err",
      text: "1 harness not recording",
    });
    expect(railHealthLine(true, { ...OK, red: ["a", "b"] }, gap).text).toBe("2 harnesses not recording");
    expect(railHealthLine(false, OK, gap)).toEqual({ tone: "err", text: "Database not reachable" });
    expect(railHealthLine(true, { ...OK, red: ["claude_code"] }, FAILED).tone).toBe("err");
  });

  it("says it is checking until the heartbeat has loaded", () => {
    expect(railHealthLine(true, null, { missing: 1, unreadable: 0 })).toEqual({
      tone: "warn",
      text: "Checking harnesses…",
    });
  });
});

describe("harnessesBadge", () => {
  it("shows no badge when nothing needs attention", () => {
    expect(harnessesBadge(OK, NONE)).toBeNull();
  });

  it("counts red harnesses first, else the Claude homes that need attention", () => {
    expect(harnessesBadge({ ...OK, red: ["claude_code"] }, { missing: 2, unreadable: 0 })).toEqual({
      n: 1,
      quiet: false,
    });
    expect(harnessesBadge(OK, { missing: 1, unreadable: 1 })).toEqual({ n: 2, quiet: false });
  });

  it("flags attention when the Claude homes check failed", () => {
    expect(harnessesBadge(OK, FAILED)).toEqual({ n: 1, quiet: false });
    expect(harnessesBadge({ ...OK, red: ["a", "b"] }, FAILED)).toEqual({ n: 2, quiet: false });
  });

  it("flags attention when liveness collection carries an error", () => {
    expect(
      harnessesBadge({ ...OK, error: "hub unavailable" } as never, NONE),
    ).toEqual({ n: 1, quiet: false });
  });
});

describe("T3 health", () => {
  it("keeps the rail out of green when T3 cannot link its active threads", () => {
    const gap = t3HealthGap({
      detected: true,
      lookup: { ok: false, error: "table not found" },
      warnings: ["table not found"],
    });
    expect(railHealthLine(true, OK, NONE, gap)).toEqual({ tone: "warn", text: "T3 threads not linking" });
    expect(harnessesBadge(OK, NONE, gap)).toEqual({ n: 1, quiet: false });
  });

  it("names a provider-hooks warning as that, not as a missing linked capture", () => {
    const t3 = {
      detected: true,
      lookup: { ok: true },
      warnings: ["T3 provider cursor has no Khipu hooks, so sessions on it get no Khipu routing start block"],
      providers: [{ id: "cursor", driver: "cursor", name: "cursor", hooks: "missing" }],
    };
    expect(t3WarningReason(t3)).toBe("hooks");
    expect(railHealthLine(true, OK, NONE, t3HealthGap(t3))).toEqual({ tone: "warn", text: "T3 providers without hooks" });
    expect(t3WarningReason({ ...t3, warnings: [...t3.warnings, "T3 was used in the last day without a thread-linked capture"] })).toBe("unlinked");
    expect(t3WarningReason({ ...t3, warnings: [] })).toBeNull();
  });
});

describe("partial T3 providers are amber, never missing or red", () => {
  const partial = { detected: true, lookup: { ok: true }, providers: [{ driver: "antigravity", hooks: "partial" }] };

  it("raises nothing on its own: Antigravity in T3 is partial for good", () => {
    expect(t3WarningReason({ ...partial, warnings: [] })).toBeNull();
    expect(t3HealthGap({ ...partial, warnings: [] })).toEqual({ warning: false, lookupFailed: false });
  });

  it("names a partial provider's own warning as partial, not as hooks missing", () => {
    const t3 = { ...partial, warnings: ["T3 provider Cursor is missing Khipu routing after a delegation"] };
    expect(t3WarningReason(t3)).toBe("partial");
    const gap = t3HealthGap(t3);
    expect(railHealthLine(true, OK, NONE, gap)).toEqual({ tone: "warn", text: "T3 providers partly set up" });
    expect(harnessesBadge(OK, NONE, gap)).toEqual({ n: 1, quiet: false });
  });

  it("still calls a provider with no hooks at all 'missing'", () => {
    const t3 = { detected: true, warnings: ["x"], providers: [{ hooks: "partial" }, { hooks: "missing" }] };
    expect(t3WarningReason(t3)).toBe("hooks");
  });
});
