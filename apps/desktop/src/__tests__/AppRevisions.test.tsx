import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

const doc = JSON.stringify({ ok: true, capture_liveness: { ok: true, harnesses: {} }, claude_homes: { homes: [] } });
const rev = (slug: string, id: number) => JSON.stringify({ recent: [{ id, slug, note: `${slug} row` }] });

describe("App — Revisions", () => {
  it("UI-22 keeps cache identity by trimmed slug and ignores reversed replies", async () => {
    const releases: Record<string, (value: string) => void> = {};
    invokeMock.mockImplementation(async (cmd: string, payload?: { args?: string[] }) => {
      if (cmd === "dsn_configured") return true;
      if (cmd === "khipu_db_status") return JSON.stringify({ ok: true });
      if (cmd === "liveness_now") return JSON.stringify({ ok: true, harnesses: {} });
      if (cmd !== "run_khipu") return JSON.stringify({ ok: true });
      const args = payload?.args ?? [];
      if (args[0] === "doctor") return doc;
      if (args[0] === "status") return JSON.stringify({ counts: {}, recent_captures: [] });
      if (args[0] === "activity") return JSON.stringify({ episode_count: 0, recent: [] });
      if (args[0] === "revisions") {
        const slugIndex = args.indexOf("--slug");
        const slug = slugIndex >= 0 ? (args[slugIndex + 1] ?? "") : "";
        if (!slug) return rev("unfiltered", 1);
        return new Promise<string>((resolve) => { releases[slug] = resolve; });
      }
      return "{}";
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /Conflicting edits/ }));
    await screen.findByText("unfiltered row");
    const filter = screen.getByPlaceholderText("filter slug (optional)");
    fireEvent.change(filter, { target: { value: "a" } });
    fireEvent.click(screen.getByRole("button", { name: "List" }));
    await waitFor(() => expect(releases.a).toBeTypeOf("function"));
    fireEvent.change(filter, { target: { value: "b" } });
    fireEvent.click(screen.getByRole("button", { name: "List" }));
    await waitFor(() => expect(releases.b).toBeTypeOf("function"));
    await act(async () => { releases.b(rev("b", 2)); });
    expect(await screen.findByText("b row")).toBeInTheDocument();
    await act(async () => { releases.a(rev("a", 3)); });
    expect(screen.queryByText("a row")).not.toBeInTheDocument();
    fireEvent.change(filter, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "List" }));
    expect(await screen.findByText("unfiltered row")).toBeInTheDocument();
  });

  it("UI-22 ignores an older request error after a newer slug succeeds", async () => {
    let rejectOld: ((reason?: unknown) => void) | undefined;
    invokeMock.mockImplementation(async (cmd: string, payload?: { args?: string[] }) => {
      if (cmd === "dsn_configured") return true;
      if (cmd === "khipu_db_status") return JSON.stringify({ ok: true });
      if (cmd === "liveness_now") return JSON.stringify({ ok: true, harnesses: {} });
      if (cmd !== "run_khipu") return JSON.stringify({ ok: true });
      const args = payload?.args ?? [];
      if (args[0] === "doctor") return doc;
      if (args[0] === "status") return JSON.stringify({ counts: {}, recent_captures: [] });
      if (args[0] === "activity") return JSON.stringify({ episode_count: 0, recent: [] });
      if (args[0] === "revisions") {
        const slugIndex = args.indexOf("--slug");
        const slug = slugIndex >= 0 ? args[slugIndex + 1] : "";
        if (!slug) return rev("unfiltered", 1);
        if (slug === "old") return new Promise<string>((_resolve, reject) => { rejectOld = reject; });
        return rev(slug, 2);
      }
      return "{}";
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /Conflicting edits/ }));
    await screen.findByText("unfiltered row");
    const filter = screen.getByPlaceholderText("filter slug (optional)");
    fireEvent.change(filter, { target: { value: "old" } });
    fireEvent.click(screen.getByRole("button", { name: "List" }));
    await waitFor(() => expect(rejectOld).toBeTypeOf("function"));
    fireEvent.change(filter, { target: { value: "new" } });
    fireEvent.click(screen.getByRole("button", { name: "List" }));
    expect(await screen.findByText("new row")).toBeInTheDocument();
    await act(async () => { rejectOld?.(new Error("stale failure")); });
    expect(screen.queryByText(/stale failure/)).not.toBeInTheDocument();
  });
});
