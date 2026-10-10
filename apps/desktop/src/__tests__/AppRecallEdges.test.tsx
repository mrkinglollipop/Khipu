import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import App from "../App";
import { WELCOME_DONE_KEY } from "../Welcome";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke: (...args: unknown[]) => invokeMock(...args) }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn(async () => null), save: vi.fn(async () => null) }));
vi.mock("@tauri-apps/api/path", () => ({ resourceDir: vi.fn(async () => "/Resources") }));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: vi.fn(async () => "0.4.0") }));
vi.mock("@tauri-apps/plugin-updater", () => ({ check: vi.fn(async () => null) }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: vi.fn(async () => {}) }));

beforeEach(() => {
  invokeMock.mockReset();
  vi.stubGlobal("localStorage", { getItem: (key: string) => key === WELCOME_DONE_KEY ? "1" : null, setItem: () => {}, removeItem: () => {}, clear: () => {} });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("App — Recall edges", () => {
  it("UI-9 gives an offline-only empty result a reconnect instruction", async () => {
    invokeMock.mockImplementation(async (cmd: string, payload?: { args?: string[] }) => {
      if (cmd === "dsn_configured") return true;
      if (cmd === "khipu_db_status") return JSON.stringify({ ok: true });
      if (cmd === "liveness_now") return JSON.stringify({ ok: true, harnesses: {} });
      if (cmd !== "run_khipu") return JSON.stringify({ ok: true });
      const args = payload?.args ?? [];
      if (args[0] === "doctor") return JSON.stringify({ ok: true, capture_liveness: { ok: true, harnesses: {} }, claude_homes: { homes: [] } });
      if (args[0] === "search") return JSON.stringify({ results: [], stale: true, snapshot_refreshed_at: "2026-10-10T11:00:00Z", filters_dropped: ["project"] });
      return JSON.stringify({ counts: {}, recent: [], episode_count: 0 });
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Recall" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Search" }), { target: { value: "offline miss" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("No matches in offline copy")).toBeInTheDocument();
    expect(screen.getByText(/Reconnect to search the current shared memory/)).toBeInTheDocument();
    expect(screen.getByText(/Updated 2026-10-10T11:00:00Z/)).toBeInTheDocument();
    expect(screen.queryByText(/filter chips are still applied/)).not.toBeInTheDocument();
  });

  it("UI-5 keeps an invalid-date CLI verdict as a persistent search failure", async () => {
    invokeMock.mockImplementation(async (cmd: string, payload?: { args?: string[] }) => {
      if (cmd === "dsn_configured") return true;
      if (cmd === "khipu_db_status") return JSON.stringify({ ok: true });
      if (cmd === "liveness_now") return JSON.stringify({ ok: true, harnesses: {} });
      if (cmd !== "run_khipu") return JSON.stringify({ ok: true });
      const args = payload?.args ?? [];
      if (args[0] === "doctor") return JSON.stringify({ ok: true, capture_liveness: { ok: true, harnesses: {} }, claude_homes: { homes: [] } });
      if (args[0] === "search") return JSON.stringify({ ok: false, error: "Invalid isoformat string: 'not-a-date'" });
      return JSON.stringify({ counts: {}, recent: [], episode_count: 0 });
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Recall" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Search" }), { target: { value: "dated query" } });
    fireEvent.click(screen.getByRole("button", { name: "+ Filter" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Since" }), { target: { value: "not-a-date" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(document.body).toHaveTextContent("Invalid isoformat string: 'not-a-date'"));
    expect(screen.getByText("Search failed")).toBeInTheDocument();
    expect(screen.queryByText("No results")).not.toBeInTheDocument();
  });

  it("UI-6 keeps newer search and graph results when older replies arrive last", async () => {
    const searches: Record<string, (value: string) => void> = {};
    const graphs: Record<string, (value: string) => void> = {};
    invokeMock.mockImplementation(async (cmd: string, payload?: { args?: string[] }) => {
      if (cmd === "dsn_configured") return true;
      if (cmd === "khipu_db_status") return JSON.stringify({ ok: true });
      if (cmd === "liveness_now") return JSON.stringify({ ok: true, harnesses: {} });
      if (cmd !== "run_khipu") return JSON.stringify({ ok: true });
      const args = payload?.args ?? [];
      if (args[0] === "doctor") return JSON.stringify({ ok: true, capture_liveness: { ok: true, harnesses: {} }, claude_homes: { homes: [] } });
      if (args[0] === "search") return new Promise<string>((resolve) => { searches[args[args.length - 1] ?? ""] = resolve; });
      if (args[0] === "graph") return new Promise<string>((resolve) => { graphs[args[args.length - 1] ?? ""] = resolve; });
      return JSON.stringify({ counts: {}, recent: [], episode_count: 0 });
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Recall" }));
    for (const query of ["older", "newer"]) {
      fireEvent.change(screen.getByRole("textbox", { name: "Search" }), { target: { value: query } });
      fireEvent.click(screen.getByRole("button", { name: "Search" }));
      await waitFor(() => expect(searches[query]).toBeTypeOf("function"));
    }
    await act(async () => { searches.newer(JSON.stringify({ results: [{ kind: "episode", id: 2, label: "Newer search hit" }] })); });
    expect(await screen.findByText("Newer search hit")).toBeInTheDocument();
    await act(async () => { searches.older(JSON.stringify({ results: [{ kind: "episode", id: 1, label: "Older search hit" }] })); });
    expect(screen.getByText("Newer search hit")).toBeInTheDocument();
    expect(screen.queryByText("Older search hit")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: "Graph" }));
    for (const node of ["topic:older", "topic:newer"]) {
      fireEvent.change(screen.getByPlaceholderText("node id — e.g. topic:khipu"), { target: { value: node } });
      fireEvent.click(screen.getByRole("button", { name: "Walk" }));
      await waitFor(() => expect(graphs[node]).toBeTypeOf("function"));
    }
    await act(async () => { graphs["topic:newer"](JSON.stringify({ id: "topic:newer", walk: [{ node_id: "topic:new-neighbor" }] })); });
    expect(await screen.findByText("topic:new-neighbor")).toBeInTheDocument();
    await act(async () => { graphs["topic:older"](JSON.stringify({ id: "topic:older", walk: [{ node_id: "topic:old-neighbor" }] })); });
    expect(screen.getByText("topic:new-neighbor")).toBeInTheDocument();
    expect(screen.queryByText("topic:old-neighbor")).not.toBeInTheDocument();
  });

  it("UI-8 exact navigation cancels a pending search and its busy state", async () => {
    let releaseSearch: ((value: string) => void) | undefined;
    invokeMock.mockImplementation(async (cmd: string, payload?: { args?: string[] }) => {
      if (cmd === "dsn_configured") return true;
      if (cmd === "khipu_db_status") return JSON.stringify({ ok: true });
      if (cmd === "liveness_now") return JSON.stringify({ ok: true, harnesses: {} });
      if (cmd !== "run_khipu") return JSON.stringify({ ok: true });
      const args = payload?.args ?? [];
      if (args[0] === "doctor") return JSON.stringify({ ok: true, capture_liveness: { ok: true, harnesses: {} }, claude_homes: { homes: [] } });
      if (args[0] === "activity" && args[1] === "--limit=40") return JSON.stringify({ episode_count: 1, recent: [{ id: 41, summary: "Activity source" }] });
      if (args[0] === "activity" && args[1] === "--show=41") return JSON.stringify({ id: 41, summary: "Exact detail" });
      if (args[0] === "search") return new Promise<string>((resolve) => { releaseSearch = resolve; });
      return JSON.stringify({ counts: {}, recent: [] });
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Recall" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Search" }), { target: { value: "pending" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText("Searching…");
    fireEvent.click(screen.getByRole("button", { name: "Activity" }));
    fireEvent.click((await screen.findByText("Activity source")).closest("button") as HTMLButtonElement);
    await screen.findByText("Exact detail");
    fireEvent.click(screen.getByRole("button", { name: "Open in Recall" }));
    await waitFor(() => expect(screen.queryByText("Searching…")).not.toBeInTheDocument());
    const recallPanel = screen.getByRole("heading", { name: "Recall" }).closest("section") as HTMLElement;
    expect(within(recallPanel).getByText("Exact detail")).toBeInTheDocument();
    await act(async () => { releaseSearch?.(JSON.stringify({ results: [{ kind: "episode", id: 99, label: "Stale hit" }] })); });
    expect(screen.queryByText("Stale hit")).not.toBeInTheDocument();
  });

  it("UI-11 forget removes only the episode with a shared id", async () => {
    invokeMock.mockImplementation(async (cmd: string, payload?: { args?: string[] }) => {
      if (cmd === "dsn_configured") return true;
      if (cmd === "khipu_db_status") return JSON.stringify({ ok: true });
      if (cmd === "liveness_now") return JSON.stringify({ ok: true, harnesses: {} });
      if (cmd !== "run_khipu") return JSON.stringify({ ok: true });
      const args = payload?.args ?? [];
      if (args[0] === "doctor") return JSON.stringify({ ok: true, capture_liveness: { ok: true, harnesses: {} }, claude_homes: { homes: [] } });
      if (args[0] === "activity" && args[1] === "--limit=40") return JSON.stringify({ episode_count: 1, recent: [{ id: 7, summary: "Episode seven" }] });
      if (args[0] === "activity" && args[1] === "--show=7") return JSON.stringify({ id: 7, summary: "Episode seven" });
      if (args[0] === "search") return JSON.stringify({ results: [{ kind: "episode", id: 7, label: "Episode hit" }, { kind: "topic", id: 7, label: "Topic hit" }] });
      if (args[0] === "episode" && args[1] === "forget") return JSON.stringify({ ok: true, snapshot: { ok: true } });
      return JSON.stringify({ counts: {}, recent: [] });
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Recall" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Search" }), { target: { value: "shared id" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText("Topic hit");
    fireEvent.click(screen.getByRole("button", { name: "Activity" }));
    fireEvent.click((await screen.findByText("Episode seven")).closest("button") as HTMLButtonElement);
    fireEvent.click(await screen.findByRole("button", { name: "Forget" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Forget" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Recall" }));
    expect(screen.queryByText("Episode hit")).not.toBeInTheDocument();
    expect(screen.getByText("Topic hit")).toBeInTheDocument();
  });
});
