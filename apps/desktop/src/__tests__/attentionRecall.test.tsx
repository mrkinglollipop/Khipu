// Attention items about recall and the offline copy.
// Moved in slice A11 from HealthChecksCard.test.tsx when its screen was removed; these test helpers
// that live code still uses, and need no removed screen.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import { buildAttention } from "../doctorAttention";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

const calls: Array<[string, Record<string, unknown> | undefined]> = [];
let stored: number | null;
let ignored: string[];
let blocked: string[];

beforeEach(() => {
  calls.length = 0;
  stored = null;
  ignored = [];
  blocked = ["degraded_rate", "outbox"];
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    calls.push([cmd, args]);
    switch (cmd) {
      case "khipu_config_show":
        return JSON.stringify({
          minute_settings: {
            doctor_rate_window_minutes: {
              value: stored ?? 60,
              source: stored == null ? "default" : "file",
              default: 60,
              min: 5,
              max: 10080,
            },
            promote_hub_wait_minutes: { value: 10, source: "default", default: 10, min: 0, max: 240 },
          },
          promote_gate_ignore: {
            value: ignored,
            choices: ["outbox", "degraded_rate"],
            blocked_by: blocked,
            expires_at: ignored.length ? "2026-10-17T12:00:00Z" : null,
          },
        });
      case "khipu_config_set":
        if (args?.key === "promote_gate_ignore") {
          ignored = String(args.value).split(",");
          return JSON.stringify({ ok: true });
        }
        stored = Number(args?.value);
        return JSON.stringify({ ok: true });
      case "khipu_config_unset":
        if (args?.key === "promote_gate_ignore") {
          ignored = [];
          return JSON.stringify({ ok: true });
        }
        stored = null;
        return JSON.stringify({ ok: true });
      default:
        throw new Error(`unexpected command ${cmd}`);
    }
  });
});

afterEach(() => cleanup());

describe("buildAttention: rate checks", () => {
  it("offers Refresh offline copy when prompt-time recall misses its deadline", () => {
    const item = buildAttention({
      prompt_recall_outcomes_ok: false,
      prompt_recall_outcomes: { available: true, insufficient: false, count: 40, failure_rate: 0.9, window_minutes: 60 },
    }).items.find((i) => i.key === "prompt_recall_outcomes");
    expect(item?.tone).toBe("err");
    expect(item?.cause).toContain("90% of the last 40 prompts in the last 60 minutes");
    expect(item?.cause).toContain("clears by itself");
    expect(item?.cause).not.toContain("khipu ");
    expect(item?.fix).toEqual({ label: "Refresh offline copy", kind: "refresh-snapshot" });
  });

  it("names too little record as a warning with no fix", () => {
    const item = buildAttention({
      prompt_recall_outcomes_ok: false,
      prompt_recall_outcomes: { available: true, insufficient: true, count: 3 },
    }).items.find((i) => i.key === "prompt_recall_outcomes");
    expect(item?.tone).toBe("warn");
    expect(item?.fix).toBeUndefined();
  });

  it("offers the same button when recall is reading the database", () => {
    const item = buildAttention({ prompt_recall_snapshot_ok: true, prompt_recall_snapshot: { ok: true, fresh: false } })
      .items.find((i) => i.key === "prompt_recall_snapshot");
    expect(item?.fix?.kind).toBe("refresh-snapshot");
  });

  it("the health row says the same as Home's notice when the offline copy is not trusted", () => {
    const parsed = {
      prompt_recall_snapshot_ok: true,
      prompt_recall_snapshot: { ok: true, fresh: false, exists: true, age_seconds: 480 },
    };
    const item = buildAttention(parsed).items.find((i) => i.key === "prompt_recall_snapshot");
    expect(item?.cause).toMatch(/not trusted yet/);
    expect(item?.cause).toMatch(/by itself/);
    const old = buildAttention({ prompt_recall_snapshot_ok: true, prompt_recall_snapshot: { ok: true, fresh: false, exists: true, age_seconds: 90000 } })
      .items.find((i) => i.key === "prompt_recall_snapshot");
    expect(old?.cause).toMatch(/more than a day old/);
  });

  it("shows an offline copy built for another version as amber, with the row still green", () => {
    const message = "This Mac's offline copy was built for an older version of Khipu (layout 5; this version uses 6).";
    const parsed = {
      prompt_recall_snapshot_ok: true,
      prompt_recall_snapshot: { ok: true, fresh: true, level: "amber", message },
    };
    const item = buildAttention(parsed).items.find((i) => i.key === "replica_schema");
    expect(item?.tone).toBe("warn");
    expect(item?.cause).toBe(message);
    expect(item?.fix?.kind).toBe("refresh-snapshot");
    expect(buildAttention({ prompt_recall_snapshot_ok: true, prompt_recall_snapshot: { ok: true, fresh: true } })
      .items.find((i) => i.key === "replica_schema")).toBeUndefined();
  });

  it("says the search fallback rate judges a window and leaves out offline searches", () => {
    const item = buildAttention({
      degraded_rate_ok: false,
      degraded_rate: { rate: 0.5, degraded: 10, sampled: 20, window_minutes: 120 },
    }).items.find((i) => i.key === "degraded_rate");
    expect(item?.cause).toContain("50% of the last 20 searches in the last 2 hours");
    expect(item?.cause).toContain("out of reach are not counted");
  });

});
