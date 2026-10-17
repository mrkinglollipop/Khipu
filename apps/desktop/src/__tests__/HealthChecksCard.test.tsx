// Offline and self-healing, slice 1: the rate window is an owner setting, and
// the rate checks offer an app button, never a terminal command.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HealthChecksCard, windowMinutesError } from "../HealthChecksCard";
import { buildAttention, promotionAttention } from "../doctorAttention";
import { healthRows } from "../healthRows";

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

describe("HealthChecksCard", () => {
  it("shows the window and saves a new one through the config command", async () => {
    render(<HealthChecksCard active />);
    const input = await screen.findByLabelText("How far back rate checks look");
    await waitFor(() => expect(input).toHaveValue("60"));
    fireEvent.change(input, { target: { value: "90" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Save" })[0]);
    // The field already shows the typed value, so wait for the write itself.
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_set", { key: "doctor_rate_window_minutes", value: "90" }]));
    await waitFor(() => expect(input).toHaveValue("90"));
    fireEvent.click(screen.getByRole("button", { name: "Use default" }));
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_unset", { key: "doctor_rate_window_minutes" }]));
    await waitFor(() => expect(input).toHaveValue("60"));
  });

  it("refuses a value out of range before calling Khipu", async () => {
    render(<HealthChecksCard active />);
    const input = await screen.findByLabelText("How far back rate checks look");
    await waitFor(() => expect(input).toHaveValue("60"));
    fireEvent.change(input, { target: { value: "2" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Save" })[0]);
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter between 5 and 10080 minutes.");
    expect(calls.some(([cmd]) => cmd === "khipu_config_set")).toBe(false);
  });

  it("checks whole minutes", () => {
    expect(windowMinutesError("60", 5, 10080)).toBeNull();
    expect(windowMinutesError("1.5", 5, 10080)).toBe("Enter a whole number of minutes.");
  });
});

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
    const row = healthRows(parsed).find((r) => r.key === "prompt_recall_snapshot_ok");
    expect(row?.status).toBe("ok");
    expect(row?.detail).toBe(message);
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

describe("HealthChecksCard: before a new version goes live", () => {
  it("offers Update anyway for the checks that held the last update back, once", async () => {
    render(<HealthChecksCard active />);
    expect(await screen.findByText(/The last update was held back by/)).toHaveTextContent(
      /Search rarely falls back.*Queued captures delivered/,
    );
    fireEvent.click(screen.getByRole("button", { name: "Update anyway" }));
    await waitFor(() =>
      expect(calls).toContainEqual(["khipu_config_set", { key: "promote_gate_ignore", value: "degraded_rate,outbox" }]),
    );
    expect(await screen.findByText(/The next update skips these checks once/)).toHaveTextContent(/lapses at/);
    expect(screen.queryByRole("button", { name: "Update anyway" })).toBeNull();
  });

  it("cancels a waiting Update anyway", async () => {
    ignored = ["outbox"];
    render(<HealthChecksCard active />);
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_unset", { key: "promote_gate_ignore" }]));
  });

  it("offers nothing when no check is holding an update back", async () => {
    blocked = [];
    render(<HealthChecksCard active />);
    expect(await screen.findByText("No health check is holding an update back.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Update anyway" })).toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("shows the wait setting", async () => {
    render(<HealthChecksCard active />);
    const input = await screen.findByLabelText("How long to wait for the database");
    await waitFor(() => expect(input).toHaveValue("10"));
  });
});

describe("promotionAttention", () => {
  it("names a queued promotion with no fix button", () => {
    const items = promotionAttention({ promotion: { queued: { ref: "origin/main", queued_at: "2026-10-16T12:00:00Z" } } });
    expect(items).toHaveLength(1);
    expect(items[0].key).toBe("promotion");
    expect(items[0].tone).toBe("warn");
    expect(items[0].title).toBe("A new version of Khipu is waiting for the database");
    expect(items[0].cause).toContain("by itself");
    expect(items[0].fix).toBeUndefined();
  });

  it("is empty when nothing is queued", () => {
    expect(promotionAttention({ promotion: { queued: null } })).toEqual([]);
    expect(promotionAttention({})).toEqual([]);
    expect(promotionAttention(null)).toEqual([]);
  });
});
