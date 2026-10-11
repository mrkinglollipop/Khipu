import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { WELCOME_DONE_KEY } from "../Welcome";

const invokeMock = vi.fn();

vi.mock("@tauri-apps/api/core", () => ({ invoke: (...args: unknown[]) => invokeMock(...args) }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn(async () => null), save: vi.fn(async () => null) }));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: vi.fn(async () => "0.4.0") }));
vi.mock("@tauri-apps/plugin-updater", () => ({ check: vi.fn(async () => null) }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: vi.fn(async () => {}) }));

const doctor = () => JSON.stringify({ ok: true, capture_liveness: { ok: true, red: [], harnesses: {} }, claude_homes: { homes: [] } });
const status = () => JSON.stringify({ counts: {}, recent_captures: [] });
const episode = (id: number, summary = `Episode ${id}`) => ({ id, summary, ts: "2026-10-10T12:00:00Z" });

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
    if (cmd === "liveness_now") return JSON.stringify({ ok: true, red: [], harnesses: {} });
    if (cmd === "run_khipu") return run(payload?.args ?? []);
    return JSON.stringify({ ok: true });
  });
}

async function openRecall() {
  fireEvent.click(screen.getByRole("button", { name: "Recall" }));
  return screen.findByRole("textbox", { name: "Search" });
}

function search(value: string) {
  fireEvent.change(screen.getByRole("textbox", { name: "Search" }), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
}

function result(id: number, label = `Episode ${id}`) {
  return { results: [{ kind: "episode", id, label, snippet: `${label} snippet` }] };
}

describe("App — Recall audit", () => {
  it("UI-5 treats failed, malformed, and schema-less search replies as failures", async () => {
    let reply = JSON.stringify({ ok: false, error: "hub refused search" });
    bootstrap((args) => {
      if (args[0] === "doctor") return doctor();
      if (args[0] === "status") return status();
      if (args[0] === "activity") return JSON.stringify({ episode_count: 0, recent: [] });
      if (args[0] === "search") return reply;
      return "{}";
    });
    render(<App />);
    await openRecall();
    search("failure case");
    expect(await screen.findByText("Search failed")).toBeInTheDocument();
    expect(document.body).toHaveTextContent("hub refused search");
    expect(screen.queryByText("No results")).not.toBeInTheDocument();

    reply = "not json";
    search("invalid json");
    expect(await screen.findByText("Search failed")).toBeInTheDocument();
    expect(screen.queryByText("No results")).not.toBeInTheDocument();

    reply = JSON.stringify({ ok: true });
    search("missing results");
    expect(await screen.findByText("Search failed")).toBeInTheDocument();
    expect(screen.queryByText("No results")).not.toBeInTheDocument();
  });

  it("UI-6 keeps the latest search and graph response while older work settles", async () => {
    const searches: Record<string, (value: string) => void> = {};
    const graphs: Record<string, (value: string) => void> = {};
    bootstrap((args) => {
      if (args[0] === "doctor") return doctor();
      if (args[0] === "status") return status();
      if (args[0] === "activity") return JSON.stringify({ episode_count: 0, recent: [] });
      if (args[0] === "search") return new Promise<string>((resolve) => { searches[args[args.length - 1] ?? ""] = resolve; });
      if (args[0] === "graph") return new Promise<string>((resolve) => { graphs[args[args.length - 1] ?? ""] = resolve; });
      return "{}";
    });
    render(<App />);
    await openRecall();
    search("A");
    await waitFor(() => expect(searches.A).toBeTypeOf("function"));
    search("B");
    await waitFor(() => expect(searches.B).toBeTypeOf("function"));
    await act(async () => { searches.A(JSON.stringify(result(1, "A result"))); });
    expect(screen.getByText("Searching…")).toBeInTheDocument();
    expect(screen.queryByText("A result")).not.toBeInTheDocument();
    await act(async () => { searches.B(JSON.stringify(result(2, "B result"))); });
    expect(await screen.findByText("B result")).toBeInTheDocument();
    expect(screen.queryByText("A result")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: "Graph" }));
    const node = screen.getByPlaceholderText("node id — e.g. topic:khipu");
    fireEvent.change(node, { target: { value: "topic:A" } });
    fireEvent.click(screen.getByRole("button", { name: "Walk" }));
    await waitFor(() => expect(graphs["topic:A"]).toBeTypeOf("function"));
    fireEvent.change(node, { target: { value: "topic:B" } });
    fireEvent.click(screen.getByRole("button", { name: "Walk" }));
    await waitFor(() => expect(graphs["topic:B"]).toBeTypeOf("function"));
    await act(async () => { graphs["topic:A"](JSON.stringify({ id: "topic:A", walk: [{ node_id: "topic:old" }] })); });
    expect(screen.queryByText("topic:old")).not.toBeInTheDocument();
    await act(async () => { graphs["topic:B"](JSON.stringify({ id: "topic:B", walk: [{ node_id: "topic:new" }] })); });
    expect(await screen.findByText("topic:new")).toBeInTheDocument();
  });

  it("UI-7 removes exactly the selected filter before searching again", async () => {
    const calls: string[][] = [];
    bootstrap((args) => {
      if (args[0] === "doctor") return doctor();
      if (args[0] === "status") return status();
      if (args[0] === "activity") return JSON.stringify({ episode_count: 0, recent: [] });
      if (args[0] === "search") { calls.push(args); return JSON.stringify({ results: [] }); }
      return "{}";
    });
    render(<App />);
    await openRecall();
    fireEvent.change(screen.getByRole("textbox", { name: "Search" }), { target: { value: "keep filters" } });
    fireEvent.click(screen.getByRole("button", { name: "+ Filter" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Project" }), { target: { value: "project-a" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Since" }), { target: { value: "7d" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Harness" }), { target: { value: "codex" } });
    fireEvent.click(screen.getByRole("button", { name: "Episode" }));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    const remove = (label: string) => {
      const button = screen.getByText(label).closest("button");
      fireEvent.click(button?.querySelector(".x") as HTMLElement);
    };
    remove("Project · project-a");
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1]).toEqual(["search", "--mode", "hybrid", "--limit", "20", "--since=7d", "--kind=episode", "--harness=codex", "--", "keep filters"]);
    remove("Since · 7d");
    await waitFor(() => expect(calls).toHaveLength(3));
    expect(calls[2]).not.toContain("--since=7d");
    expect(calls[2]).toContain("--kind=episode");
    expect(calls[2]).toContain("--harness=codex");
    remove("Kind · Episode");
    await waitFor(() => expect(calls).toHaveLength(4));
    expect(calls[3]).not.toContain("--kind=episode");
    expect(calls[3]).toContain("--harness=codex");
    remove("Harness · Codex");
    await waitFor(() => expect(calls).toHaveLength(5));
    expect(calls[4]).not.toContain("--harness=codex");
  });

  it("UI-8 opens the exact activity episode in Recall without a word search", async () => {
    const calls: string[][] = [];
    bootstrap((args) => {
      calls.push(args);
      if (args[0] === "doctor") return doctor();
      if (args[0] === "status") return status();
      if (args[0] === "activity" && args[1] === "--limit=40") return JSON.stringify({ episode_count: 1, recent: [episode(41, "Activity source")] });
      if (args[0] === "activity" && args[1] === "--show=41") return JSON.stringify(episode(41, "Exact episode detail"));
      return "{}";
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Activity" }));
    fireEvent.click((await screen.findByText("Activity source")).closest("button") as HTMLButtonElement);
    await screen.findByText("Exact episode detail");
    fireEvent.click(screen.getByRole("button", { name: "Open in Recall" }));
    expect(await screen.findByText("Exact episode detail")).toBeInTheDocument();
    expect(calls.filter((args) => args[0] === "search")).toHaveLength(0);
  });

  it("UI-9 labels stale snapshot hits and misses as offline copy results", async () => {
    let response = JSON.stringify({ stale: true, snapshot_refreshed_at: "2026-10-10T11:00:00Z", mode: "literal", filters_dropped: ["project"], results: [] });
    bootstrap((args) => {
      if (args[0] === "doctor") return doctor();
      if (args[0] === "status") return status();
      if (args[0] === "activity") return JSON.stringify({ episode_count: 0, recent: [] });
      if (args[0] === "search") return response;
      return "{}";
    });
    render(<App />);
    await openRecall();
    search("offline miss");
    expect(await screen.findByText("No matches in offline copy")).toBeInTheDocument();
    expect(document.body).toHaveTextContent("Search: literal");
    expect(document.body).toHaveTextContent("Filters not used: project");
    response = JSON.stringify({ stale: true, snapshot_refreshed_at: "2026-10-10T11:00:00Z", mode: "semantic", filters_dropped: ["harness"], results: result(9, "Offline hit").results });
    search("offline hit");
    expect(await screen.findByText("Offline hit")).toBeInTheDocument();
    expect(document.body).toHaveTextContent("Search: semantic");
  });

  it("UI-11 removes forgotten recall cache and fences an older search reply", async () => {
    let releaseOld: ((value: string) => void) | undefined;
    bootstrap((args) => {
      if (args[0] === "doctor") return doctor();
      if (args[0] === "status") return status();
      if (args[0] === "activity" && args[1] === "--limit=40") return JSON.stringify({ episode_count: 1, recent: [episode(7, "Activity 7")] });
      if (args[0] === "activity" && args[1] === "--show=7") return JSON.stringify(episode(7, "Cached episode"));
      if (args[0] === "search" && args[args.length - 1] === "cached") return JSON.stringify(result(7, "Cached episode"));
      if (args[0] === "search" && args[args.length - 1] === "old") return new Promise<string>((resolve) => { releaseOld = resolve; });
      if (args[0] === "episode" && args[1] === "forget") return JSON.stringify({ ok: true, soft_deleted: true, snapshot: { ok: true } });
      return "{}";
    });
    render(<App />);
    await openRecall();
    search("cached");
    await screen.findByText("Cached episode");
    search("old");
    await waitFor(() => expect(releaseOld).toBeTypeOf("function"));
    fireEvent.click(screen.getByRole("button", { name: "Activity" }));
    fireEvent.click((await screen.findByText("Activity 7")).closest("button") as HTMLButtonElement);
    await screen.findByText("Cached episode");
    fireEvent.click(screen.getByRole("button", { name: "Forget" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Forget" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await act(async () => { releaseOld?.(JSON.stringify(result(7, "Forgotten again"))); });
    fireEvent.click(screen.getByRole("button", { name: "Recall" }));
    await waitFor(() => expect(screen.queryByText("Forgotten again")).not.toBeInTheDocument());
    expect(screen.queryByText("Cached episode")).not.toBeInTheDocument();
  });
});
