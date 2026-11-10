// Memory › Project work lists (slice A8): the projects, one project's list in
// three parts, and a line marked done "by you" through the same command the
// old ledger page uses.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ProjectLists } from "../memory/ProjectLists";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke: (...a: unknown[]) => invokeMock(...a) }));

const day = (n: number) => new Date(Date.now() - n * 86400000).toISOString();
const VOCAB = { state: [["NOT STARTED", "open"], ["IN FLIGHT", "open"], ["DONE", "done"]], kind: ["goal"] };
type R = { id: string; title: string; state: string; updated: string; owner: string; refs: Record<string, unknown>; history: Array<{ by: string; to?: string }> };
let rows: R[];
let sets: Array<Record<string, unknown> | undefined>;
let setReply: Record<string, unknown>;
const r = (id: string, title: string, state: string, d: number, extra: Partial<R> = {}): R => ({ id, title, state, updated: day(d), owner: "", refs: {}, history: [], ...extra });

beforeEach(() => {
  sets = [];
  setReply = { ok: true };
  rows = [r("a", "Ship the thing", "IN FLIGHT", 1, { owner: "Codex" }), r("b", "Fresh finish", "DONE", 2), r("c", "Old finish", "DONE", 30)];
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    if (cmd === "khipu_ledger_projects")
      return JSON.stringify({ ok: true, projects: [
        { project: "p", name: "Khipu", hidden: false, rows: rows.length, open: rows.filter((x) => x.state !== "DONE").length },
        { project: "q", name: "Recipe app", hidden: false, rows: 0, open: 0 },
        { project: "h", name: "Hidden one", hidden: true, rows: 3, open: 1 },
      ] });
    if (cmd === "khipu_ledger_list") return JSON.stringify({ ok: true, project: "p", offline: false, vocab: VOCAB, rows });
    if (cmd === "khipu_ledger_set") {
      sets.push(args);
      const change = args?.change as { state: string };
      if (setReply.ok !== false && !setReply.queued && !setReply.late) {
        const row = rows.find((x) => x.id === args?.id)!;
        row.state = change.state;
        row.updated = new Date().toISOString();
        row.history = [{ by: "you, in the app", to: change.state }];
      }
      return JSON.stringify(setReply);
    }
    throw new Error(`unexpected ${cmd}`);
  });
});
afterEach(() => {
  cleanup();
  invokeMock.mockReset();
});

describe("ProjectLists", () => {
  it("lists projects with counts, hides hidden ones, and opens the first list", async () => {
    render(<ProjectLists />);
    expect(await screen.findByText("1 not done · 2 done")).toBeTruthy();
    expect(screen.getByText("No work list yet")).toBeTruthy();
    expect(screen.queryByText("Hidden one")).toBeNull();
    expect(await screen.findByRole("heading", { name: "Not done" })).toBeTruthy();
    expect(screen.getByText("Codex is on it")).toBeTruthy();
  });

  it("puts only this week's finished lines under Done this week, and See everything shows the rest", async () => {
    render(<ProjectLists />);
    await screen.findByText("Fresh finish");
    expect(screen.queryByText("Old finish")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "See everything" }));
    expect(screen.getByText("Old finish")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Everything done" })).toBeTruthy();
  });

  it("marks a line done with if_state and shows 'Marked done by you'", async () => {
    render(<ProjectLists />);
    fireEvent.click(await screen.findByRole("button", { name: "Mark done: Ship the thing" }));
    await waitFor(() => expect(screen.getAllByText("Marked done by you").length).toBe(1));
    expect(sets).toEqual([{ project: "p", id: "a", change: { state: "DONE", if_state: "IN FLIGHT" } }]);
  });

  it("says so when the change was kept waiting, and does not claim it was done by you", async () => {
    setReply = { ok: true, queued: true };
    render(<ProjectLists />);
    fireEvent.click(await screen.findByRole("button", { name: "Mark done: Ship the thing" }));
    expect(await screen.findByText(/Saved on this Mac/)).toBeTruthy();
    expect(screen.queryByText("Marked done by you")).toBeNull();
  });

  it("shows a refusal and leaves the line alone", async () => {
    setReply = { ok: false, error: "The row moved." };
    render(<ProjectLists />);
    fireEvent.click(await screen.findByRole("button", { name: "Mark done: Ship the thing" }));
    expect(await screen.findByText(/Not changed: The row moved\./)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Mark done: Ship the thing" })).toBeTruthy();
  });

  it("shows no removed words on screen", async () => {
    const { container } = render(<ProjectLists />);
    await screen.findByText("Fresh finish");
    expect(container.textContent ?? "").not.toMatch(/ledger|harness|episode|hold|override|owed/i);
  });
});
