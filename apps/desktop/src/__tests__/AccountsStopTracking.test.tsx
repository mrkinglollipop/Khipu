// Stop tracking from the Accounts place (fix round, item 1): the dialog gets the
// job-slot map the way the older Accounts screen did, so it shows the choice
// before it sends `keepLanes`. A map that could not be read is never emptied.
(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Accounts } from "../accounts/Accounts";
import { useRouting } from "../routing";
import type { RouteAccount, RouteView } from "../routing";
import { status9B } from "./accountsFixtures";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke: (...args: unknown[]) => invokeMock(...args) }));

const NOW = new Date("2026-11-10T19:00:00Z");
const at = (min: number) => new Date(NOW.getTime() + min * 60_000).toISOString();
const calls: Array<[string, Record<string, unknown> | undefined]> = [];
let laneConfig: "map" | "fails" | "empty" | "hold";
let releaseHeld: (() => void) | null = null;

const accts: RouteAccount[] = [
  { key: "claude|", label: "Claude main", harness: "claude", state: "open", cap_statuses: [],
    pace: [{ window: "weekly", used_pct: 38, cycle_resets_at: at(3000), plan: { verdict: "on_pace" } }] },
  { key: "codex|", label: "Codex", harness: "codex", state: "open", cap_statuses: [],
    pace: [{ window: "weekly", used_pct: 20, cycle_resets_at: at(3000), plan: { verdict: "on_pace" } }] },
];

beforeEach(() => {
  calls.length = 0;
  laneConfig = "map";
  releaseHeld = null;
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    calls.push([cmd, args]);
    switch (cmd) {
      case "khipu_route_view": {
        const view: RouteView = {
          record: true, checked_at: at(-3), this_mac: "M5", accounts: JSON.parse(JSON.stringify(accts)),
          priority: { entries: accts.map((a) => ({ account: a.key, label: a.label })) },
        };
        return JSON.stringify(view);
      }
      case "khipu_route_override_view": return JSON.stringify({ ok: true, overrides: [] });
      case "khipu_route_override_candidates": return JSON.stringify({ ok: true, candidates: [] });
      case "khipu_accounts_status":
        return JSON.stringify(status9B({
          accounts: [{ label: "Claude main", harness: "claude", key: "claude|" }, { label: "Codex", harness: "codex", key: "codex|" }],
        }));
      case "khipu_config_show":
        if (laneConfig === "fails") throw new Error("config unreadable");
        if (laneConfig === "empty") return JSON.stringify({ routing: { routing_lane_map: { value: {} } } });
        if (laneConfig === "hold") {
          await new Promise<void>((r) => { releaseHeld = r; });
        }
        return JSON.stringify({ routing: { routing_lane_map: { value: { "Project A": { build: "claude|" } } } } });
      case "khipu_accounts_stop":
        return JSON.stringify({ ok: true, label: "Claude main", none: false });
      default:
        return JSON.stringify({ ok: true });
    }
  });
});
afterEach(() => cleanup());

function Place() {
  const routing = useRouting(true);
  return <Accounts routing={routing} plans={null} thisMac="M5" now={NOW} />;
}

async function openAccount() {
  render(<Place />);
  fireEvent.click(await screen.findByRole("button", { name: /^Claude main/ }));
  const stop = await screen.findByRole("button", { name: "Stop tracking…" });
  await waitFor(() => expect(stop).toBeEnabled());
  // The lane map is read by now (or has failed), whichever the test set up.
  await waitFor(() => expect(calls.some(([c]) => c === "khipu_config_show")).toBe(true));
  await new Promise((r) => setTimeout(r, 20));
  return stop;
}

async function openStopDialog() {
  fireEvent.click(await openAccount());
}

describe("Stop tracking in the Accounts place", () => {
  it("shows which job slots use the account and sends the choice made", async () => {
    await openStopDialog();
    const box = await screen.findByRole("checkbox", { name: "Remove it from 1 job slot: build" });
    expect(box).toBeChecked();
    fireEvent.click(box); // keep the slot
    fireEvent.click(screen.getByRole("button", { name: "Stop tracking" }));
    await waitFor(() => expect(calls.filter(([c]) => c === "khipu_accounts_stop")).toHaveLength(1));
    expect(calls.find(([c]) => c === "khipu_accounts_stop")![1]).toMatchObject({ account: "claude|", keepLanes: true });
  });

  it("removes the slots when the box stays checked", async () => {
    await openStopDialog();
    await screen.findByRole("checkbox", { name: "Remove it from 1 job slot: build" });
    fireEvent.click(screen.getByRole("button", { name: "Stop tracking" }));
    await waitFor(() => expect(calls.filter(([c]) => c === "khipu_accounts_stop")).toHaveLength(1));
    expect(calls.find(([c]) => c === "khipu_accounts_stop")![1]).toMatchObject({ keepLanes: false });
  });

  it("keeps the job slots when the map could not be read", async () => {
    laneConfig = "fails";
    await openStopDialog();
    await screen.findByRole("heading", { name: /Stop tracking Claude main/ });
    expect(screen.queryByRole("checkbox", { name: /job slot/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Stop tracking" }));
    await waitFor(() => expect(calls.filter(([c]) => c === "khipu_accounts_stop")).toHaveLength(1));
    expect(calls.find(([c]) => c === "khipu_accounts_stop")![1]).toMatchObject({ keepLanes: true });
  });

  it("reads the job-slot map again when the dialog opens, so a slot assigned since is named", async () => {
    laneConfig = "empty"; // what the screen read when it opened
    const stop = await openAccount();
    laneConfig = "map"; // another caller has assigned the account a slot since
    fireEvent.click(stop);
    expect(await screen.findByRole("checkbox", { name: "Remove it from 1 job slot: build" })).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Stop tracking" }));
    await waitFor(() => expect(calls.filter(([c]) => c === "khipu_accounts_stop")).toHaveLength(1));
    expect(calls.find(([c]) => c === "khipu_accounts_stop")![1]).toMatchObject({ keepLanes: false });
  });

  it("keeps the job slots when the dialog is used before the re-read answers", async () => {
    const stop = await openAccount();
    laneConfig = "hold"; // the re-read never answers during this test
    fireEvent.click(stop);
    await screen.findByRole("heading", { name: /Stop tracking Claude main/ });
    expect(screen.queryByRole("checkbox", { name: /job slot/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Stop tracking" }));
    await waitFor(() => expect(calls.filter(([c]) => c === "khipu_accounts_stop")).toHaveLength(1));
    expect(calls.find(([c]) => c === "khipu_accounts_stop")![1]).toMatchObject({ keepLanes: true });
    releaseHeld?.();
  });

  it("keeps the job slots when the re-read fails, though the first read had worked", async () => {
    const stop = await openAccount();
    laneConfig = "fails";
    fireEvent.click(stop);
    await screen.findByRole("heading", { name: /Stop tracking Claude main/ });
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByRole("checkbox", { name: /job slot/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Stop tracking" }));
    await waitFor(() => expect(calls.filter(([c]) => c === "khipu_accounts_stop")).toHaveLength(1));
    expect(calls.find(([c]) => c === "khipu_accounts_stop")![1]).toMatchObject({ keepLanes: true });
  });
});
