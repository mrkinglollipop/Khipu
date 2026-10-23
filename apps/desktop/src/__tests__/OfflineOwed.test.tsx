// The Owed tab when the hub is away (offline slice 6): the banner, the
// "Waiting to sync" label and the "Saved on this Mac" notice, through App's
// real loadOwed / owedWrite, plus the pure helpers.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import App from "../App";
import { WELCOME_DONE_KEY } from "../Welcome";
import { clockTime, isHubUnreachable, OfflineBanner, parseOwedAnswer } from "../OfflineBanner";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke: (...args: unknown[]) => invokeMock(...args) }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn(async () => null), save: vi.fn(async () => null) }));
vi.mock("@tauri-apps/api/path", () => ({ resourceDir: vi.fn(async () => "/Applications/Khipu.app/Contents/Resources") }));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: vi.fn(async () => "0.4.0") }));
vi.mock("@tauri-apps/plugin-updater", () => ({ check: vi.fn(async () => null) }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: vi.fn(async () => {}) }));

beforeEach(() => {
  invokeMock.mockReset();
  const store = new Map<string, string>([[WELCOME_DONE_KEY, "1"]]);
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const REFRESHED = new Date(2026, 9, 16, 9, 5).toISOString(); // 09:05 local
const item = (id: number, over: Record<string, unknown> = {}) => ({
  id, text: `Send the invoice ${id}`, project: "atlas", kind: "followup", status: "open",
  owner: "user", opened_episode: 10 + id, opened_at: "2026-10-15T12:00:00Z", ...over,
});

function script(owed: (args: string[]) => unknown, write?: (args: string[]) => unknown) {
  invokeMock.mockImplementation(async (cmd: string, payload?: { args?: string[] }) => {
    if (cmd === "dsn_configured") return true;
    if (cmd === "khipu_work_view") {
      return JSON.stringify({
        format: "khipu-work-view", version: 1, generated_at: REFRESHED, refresh_s: 3600,
        projects: [], live: [], waiting: [],
        counts: { waiting: 0, blocking: 0, running: 0, flagged: 0, stale_sources: 0, ledger_outbox: 0 },
      });
    }
    if (cmd !== "run_khipu") return JSON.stringify({ ok: true });
    const args = payload?.args ?? [];
    if (args[0] === "owed") {
      const isWrite = args.some((a) => /^--(close|reopen|snooze)=/.test(a));
      const out = isWrite && write ? write(args) : owed(args);
      if (out instanceof Error) throw out;
      return JSON.stringify(out);
    }
    return "{}";
  });
}

async function openOwed() {
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: /^Work/ }));
}

describe("the Owed tab with the hub away", () => {
  it("asks for the envelope and says whose copy it shows, and when it is from", async () => {
    script(() => ({ results: [item(1)], stale: true, snapshot_refreshed_at: REFRESHED, waiting_to_sync: false }));
    await openOwed();
    const banner = await screen.findByText("Hub not reachable. Showing this Mac's copy from 09:05.");
    expect(banner.closest("[role='status']")).not.toBeNull();
    expect(invokeMock.mock.calls.some(([, p]) => p?.args?.[0] === "owed" && p.args.includes("--envelope"))).toBe(true);
  });

  it("shows no banner when the answer is fresh, and still takes a bare list", async () => {
    script(() => [item(1)]);
    await openOwed();
    await screen.findAllByText("Send the invoice 1");
    expect(screen.queryByText(/Hub not reachable/)).toBeNull();
  });

  it("a timed out load shows the retry banner, not a raw error", async () => {
    script(() => new Error("Khipu did not answer within 60 seconds. The hub may be unreachable."));
    await openOwed();
    await screen.findByText("Hub not reachable. Khipu will try again.");
    expect(screen.queryByText(/did not answer within/)).toBeNull();
  });

  it("a reload that fails after a good load keeps the rows under the banner", async () => {
    let down = false;
    script(() => (down ? new Error("Khipu did not answer within 60 seconds. The hub may be unreachable.") : [item(1)]));
    await openOwed();
    await screen.findAllByText("Send the invoice 1");
    down = true;
    await screen.findAllByText("Send the invoice 1");
    fireEvent.click(document.querySelector(".wk-owed-lede button") as HTMLElement);
    await screen.findByText("Hub not reachable. Khipu will try again.");
    expect(screen.getAllByText("Send the invoice 1").length).toBeGreaterThan(0);
    expect(screen.queryByText(/Couldn't read/)).toBeNull();
  });

  it("labels a row with a queued change in words", async () => {
    script(() => ({
      results: [item(1, { pending_sync: { action: "close" } }), item(2)],
      stale: true, snapshot_refreshed_at: REFRESHED, waiting_to_sync: true,
    }));
    await openOwed();
    await screen.findAllByText("Send the invoice 1");
    expect(screen.getAllByText("Waiting to sync")).toHaveLength(1);
  });

  it("a queued write says it is saved on this Mac, not an error", async () => {
    script(
      () => ({ results: [item(1)], stale: false, snapshot_refreshed_at: null, waiting_to_sync: false }),
      () => ({ ok: true, waiting_to_sync: true }),
    );
    await openOwed();
    fireEvent.click(await screen.findByRole("button", { name: "Done" }));
    await screen.findByText("Saved on this Mac. It will sync when the hub is back.");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("refreshes Work immediately after a successful single Owed write", async () => {
    script(
      () => ({ results: [item(1)], stale: false, snapshot_refreshed_at: null, waiting_to_sync: false }),
      () => ({ ok: true }),
    );
    await openOwed();
    await screen.findByRole("button", { name: "Done" });
    await waitFor(() => expect(invokeMock.mock.calls.filter(([cmd]) => cmd === "khipu_work_view").length).toBeGreaterThanOrEqual(2));
    const before = invokeMock.mock.calls.filter(([cmd]) => cmd === "khipu_work_view").length;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Done" }));
    });
    await waitFor(() => expect(invokeMock.mock.calls.filter(([cmd]) => cmd === "khipu_work_view").length).toBeGreaterThan(before));
  });
});

describe("the offline helpers", () => {
  it("reads the envelope and a bare list", () => {
    expect(parseOwedAnswer([{ id: 1 }])).toEqual({ rows: [{ id: 1 }], stale: false, refreshedAt: null, waitingToSync: false });
    expect(parseOwedAnswer({ results: [], stale: true, snapshot_refreshed_at: "x", waiting_to_sync: true })).toEqual({
      rows: [], stale: true, refreshedAt: "x", waitingToSync: true,
    });
    expect(parseOwedAnswer({ error: "no" })).toBeNull();
  });

  it("knows the timeout and connection errors", () => {
    expect(isHubUnreachable("Khipu did not answer within 60 seconds.")).toBe(true);
    expect(isHubUnreachable("connection timeout expired")).toBe(true);
    expect(isHubUnreachable("khipu exited 2: bad argument")).toBe(false);
  });

  it("leaves the time out when the stamp is missing", () => {
    expect(clockTime(null)).toBeNull();
    expect(clockTime("nonsense")).toBeNull();
    render(<OfflineBanner state={{ kind: "stale", refreshedAt: null }} />);
    expect(screen.getByRole("status")).toHaveTextContent("Hub not reachable. Showing this Mac's copy.");
  });
});
