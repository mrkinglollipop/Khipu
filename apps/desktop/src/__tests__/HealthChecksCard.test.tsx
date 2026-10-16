// Offline and self-healing, slice 1: the rate window is an owner setting, and
// the rate checks offer an app button, never a terminal command.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HealthChecksCard, windowMinutesError } from "../HealthChecksCard";
import { buildAttention } from "../doctorAttention";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

const calls: Array<[string, Record<string, unknown> | undefined]> = [];
let stored: number | null;

beforeEach(() => {
  calls.length = 0;
  stored = null;
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
          },
        });
      case "khipu_config_set":
        stored = Number(args?.value);
        return JSON.stringify({ ok: true });
      case "khipu_config_unset":
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
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(input).toHaveValue("90"));
    expect(calls).toContainEqual(["khipu_config_set", { key: "doctor_rate_window_minutes", value: "90" }]);
    fireEvent.click(screen.getByRole("button", { name: "Use default" }));
    await waitFor(() => expect(input).toHaveValue("60"));
    expect(calls).toContainEqual(["khipu_config_unset", { key: "doctor_rate_window_minutes" }]);
  });

  it("refuses a value out of range before calling Khipu", async () => {
    render(<HealthChecksCard active />);
    const input = await screen.findByLabelText("How far back rate checks look");
    await waitFor(() => expect(input).toHaveValue("60"));
    fireEvent.change(input, { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
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

  it("says the search fallback rate judges a window and leaves out offline searches", () => {
    const item = buildAttention({
      degraded_rate_ok: false,
      degraded_rate: { rate: 0.5, degraded: 10, sampled: 20, window_minutes: 120 },
    }).items.find((i) => i.key === "degraded_rate");
    expect(item?.cause).toContain("50% of the last 20 searches in the last 2 hours");
    expect(item?.cause).toContain("out of reach are not counted");
  });
});
