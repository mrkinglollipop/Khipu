import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import App from "../App";
import { WELCOME_DONE_KEY } from "../Welcome";

const invokeMock = vi.fn();

vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));
vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: vi.fn(async () => null),
  save: vi.fn(async () => null),
}));
vi.mock("@tauri-apps/api/path", () => ({
  resourceDir: vi.fn(async () => "/Applications/Khipu.app/Contents/Resources"),
}));
vi.mock("@tauri-apps/api/app", () => ({
  getVersion: vi.fn(async () => "0.4.0"),
}));
vi.mock("@tauri-apps/plugin-updater", () => ({ check: vi.fn(async () => null) }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: vi.fn(async () => {}) }));

const live = { ok: true, red: [], harnesses: {} };
const episode = (id: number, summary = `Episode ${id}`) => ({
  id,
  summary,
  ts: `2026-10-10T12:${String(id % 60).padStart(2, "0")}:00Z`,
});
const activity = (count: number, total = count) => JSON.stringify({
  episode_count: total,
  recent: Array.from({ length: count }, (_, i) => episode(i + 1)),
});

beforeEach(() => {
  invokeMock.mockReset();
  HTMLElement.prototype.scrollIntoView = vi.fn();
  const store = new Map<string, string>([[WELCOME_DONE_KEY, "1"]]);
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function bootstrap(run: (args: string[]) => Promise<string> | string) {
  invokeMock.mockImplementation(async (cmd: string, payload?: { args?: string[] }) => {
    if (cmd === "dsn_configured") return true;
    if (cmd === "khipu_db_status") return JSON.stringify({ ok: true });
    if (cmd === "liveness_now") return JSON.stringify(live);
    if (cmd === "run_khipu") return run(payload?.args ?? []);
    return JSON.stringify({ ok: true });
  });
}

const doctor = () => JSON.stringify({ ok: true, capture_liveness: live, claude_homes: { homes: [] } });
const status = () => JSON.stringify({ counts: {}, recent_captures: [] });
const activityRow = (summary: string) => screen.getAllByText(summary)
  .map((node) => node.closest("button"))
  .find((node): node is HTMLButtonElement => node != null) as HTMLButtonElement;

describe("App — Activity", () => {
  it("UI-1 keeps the newest detail and saves the detail that opened the dialog", async () => {
    const detailReleases: Record<number, (value: string) => void> = {};
    const calls: string[][] = [];
    bootstrap((args) => {
      calls.push(args);
      if (args[0] === "doctor") return doctor();
      if (args[0] === "status") return status();
      if (args[0] === "activity" && args[1] === "--limit=40") return activity(2, 2);
      if (args[0] === "activity" && args[1]?.startsWith("--show=")) {
        const id = Number(args[1].slice("--show=".length));
        return new Promise<string>((resolve) => { detailReleases[id] = resolve; });
      }
      if (args[0] === "episode" && args[1] === "edit") return JSON.stringify({ ok: true });
      return "{}";
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Activity" }));
    await screen.findByText("Episode 1");
    fireEvent.click(activityRow("Episode 1"));
    await waitFor(() => expect(detailReleases[1]).toBeTypeOf("function"));
    fireEvent.click(activityRow("Episode 2"));
    await waitFor(() => expect(detailReleases[2]).toBeTypeOf("function"));

    await act(async () => { detailReleases[2](JSON.stringify({ ...episode(2, "B detail") })); });
    expect(await screen.findByText("B detail")).toBeInTheDocument();
    await act(async () => { detailReleases[1](JSON.stringify({ ...episode(1, "A stale detail") })); });
    await waitFor(() => expect(screen.queryByText("A stale detail")).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Edit summary" }));
    fireEvent.click(activityRow("Episode 1"));
    fireEvent.change(screen.getByRole("textbox", { name: "Episode summary" }), { target: { value: "B corrected" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls).toContainEqual(["episode", "edit", "2", "--summary=B corrected"]));
  });

  it("UI-10 rejects failed or malformed forget verdicts and names local cleanup failures", async () => {
    let verdict = JSON.stringify({ ok: false, error: "permission denied" });
    bootstrap((args) => {
      if (args[0] === "doctor") return doctor();
      if (args[0] === "status") return status();
      if (args[0] === "activity" && args[1] === "--limit=40") return activity(1, 1);
      if (args[0] === "activity" && args[1] === "--show=1") return JSON.stringify(episode(1));
      if (args[0] === "episode" && args[1] === "forget") return verdict;
      return "{}";
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Activity" }));
    await screen.findByText("Episode 1");
    fireEvent.click(activityRow("Episode 1"));
    await screen.findByRole("button", { name: "Forget" });
    fireEvent.click(screen.getByRole("button", { name: "Forget" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Forget" }));
    expect(await screen.findByText("permission denied")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getAllByText("Episode 1").length).toBeGreaterThan(1);

    verdict = "{}";
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Forget" }));
    expect(await screen.findByText("Could not verify that episode #1 was removed.")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    verdict = JSON.stringify({ ok: true, snapshot: { ok: false, error: "disk full" } });
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Forget" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("removed from database; local copy could not be removed: disk full");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("UI-21 accepts only the latest requested activity page", async () => {
    const releases: Array<(value: string) => void> = [];
    bootstrap((args) => {
      if (args[0] === "doctor") return doctor();
      if (args[0] === "status") return status();
      if (args[0] === "activity" && (args[1] === "--limit=40" || args[1] === "--limit=80")) {
        return new Promise<string>((resolve) => releases.push(resolve));
      }
      return "{}";
    });
    render(<App />);
    await waitFor(() => expect(releases).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "Activity" }));
    await waitFor(() => expect(releases.length).toBeGreaterThanOrEqual(2));
    const old40 = releases[0];
    const new40 = releases[releases.length - 1];
    await act(async () => { new40(activity(40, 120)); });
    expect(await screen.findByText("Episode 40")).toBeInTheDocument();
    const beforeExpand = releases.length;
    fireEvent.click(screen.getByRole("button", { name: "Show 40 more" }));
    await waitFor(() => expect(releases.length).toBeGreaterThan(beforeExpand));
    await act(async () => { releases[releases.length - 1](activity(80, 120)); });
    expect(await screen.findByText("Episode 80")).toBeInTheDocument();
    await act(async () => { old40(activity(40, 120)); });
    await waitFor(() => expect(screen.getByText("Episode 80")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Show 40 more" })).toBeInTheDocument();
  });
});
