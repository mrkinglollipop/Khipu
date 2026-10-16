import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { LedgerPage } from "../LedgerPage";
import { WorkView } from "../WorkView";
import type { LedgerRow, WorkView as Doc } from "../work";

// Owner actions on the Work view and the ledger page: a change goes only to
// the item the owner can see named, and reading never writes.

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke: (...args: unknown[]) => invokeMock(...args) }));
afterEach(() => {
  cleanup();
  invokeMock.mockReset();
});

const vocab = {
  state: [["QUEUED", "open"], ["NEEDS OWNER", "needs"], ["DONE", "done"], ["DROPPED", "dropped"]],
  kind: ["slice"],
} as unknown as NonNullable<Doc["projects"][number]["ledger"]>["vocab"];

const row = (id: string): LedgerRow => ({
  id, title: `Row ${id}`, kind: "slice", parent: "", state: "QUEUED", evidence: "", owner: "none",
  updated: "2026-10-15T12:00:00Z", link: "", refs: {}, project: "atlas", history: [],
});

const changes = () =>
  invokeMock.mock.calls.filter(([c]) => ["khipu_ledger_set", "khipu_ledger_remove", "khipu_ledger_restore"].includes(c));

function page() {
  render(<LedgerPage project="atlas" name="Atlas" view={null} onBack={() => {}} onChanged={async () => {}}
    onOwedClose={async () => {}} />);
}

it("a withheld item with no key or id offers no change at all", async () => {
  const doc: Doc = {
    format: "khipu-work-view", version: 1, generated_at: "2026-10-15T12:00:00Z",
    projects: [{
      project: "atlas", name: "Atlas", source: { state: "none" }, jobs: [], prs: [], trains: [], merged: [], flagged: 0,
      ledger: { rows: 1, by_state: { "NEEDS OWNER": 1 }, goals: [], loose_ends: [], loose_end_count: 0,
        changed_today: [], vocab },
    }],
    live: [],
    waiting: [{ source: "ledger", key: null, project: null, id: null, title: null, blocking: false,
      actions: ["done", "answered"] }],
    counts: { waiting: 1, blocking: 0, running: 0, flagged: 0, stale_sources: 0, ledger_outbox: 0 },
  } as unknown as Doc;
  invokeMock.mockResolvedValue(JSON.stringify({ ok: true }));
  render(<WorkView work={{ data: doc, error: null, loading: false, readAt: null, load: async () => {} }}
    routing={{ data: null, error: null, loading: false, lastOkAt: null, firstRead: false, load: async () => {} }}
    owedSection={null} owedFocusKey={0} onOwedClose={async () => {}} onSnooze={() => {}} onOpenRouting={() => {}} />);
  expect(screen.queryByRole("button", { name: "Done" })).toBeNull();
  expect(screen.queryByRole("button", { name: /Answered/ })).toBeNull();
  await act(async () => {});
  expect(changes()).toEqual([]);
});

it("an older detail answer never changes the target of an open Remove confirmation", async () => {
  let releaseA!: (s: string) => void;
  const pendingA = new Promise<string>((resolve) => {
    releaseA = resolve;
  });
  invokeMock.mockImplementation(async (cmd: string, args: { id?: string }) => {
    if (cmd === "khipu_ledger_list")
      return JSON.stringify({ ok: true, project: "atlas", offline: false, vocab, rows: [row("a"), row("b")] });
    if (cmd === "khipu_ledger_removed") return JSON.stringify({ removed: [] });
    if (cmd === "khipu_ledger_get") return args.id === "a" ? pendingA : JSON.stringify(row("b"));
    return JSON.stringify({ ok: true });
  });
  page();
  await screen.findByText("Row a");
  fireEvent.click(screen.getByRole("button", { name: /^aRow a/ }));
  await waitFor(() => expect(invokeMock.mock.calls.some(([c, a]) => c === "khipu_ledger_get" && a.id === "a")).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: /^bRow b/ }));
  await screen.findByRole("heading", { name: "b" });
  fireEvent.click(screen.getByRole("button", { name: "Remove…" }));
  await screen.findByRole("dialog", { name: "Remove b?" });
  await act(async () => {
    releaseA(JSON.stringify(row("a")));
    await pendingA;
  });
  fireEvent.click(screen.getByRole("button", { name: /^Remove$/ }));
  await act(async () => {});
  expect(invokeMock.mock.calls.filter(([c]) => c === "khipu_ledger_remove").map(([, a]) => a.id)).toEqual(["b"]);
});

it("opening the ledger and reading its rows never changes one", async () => {
  invokeMock.mockImplementation(async (cmd: string) =>
    JSON.stringify(cmd === "khipu_ledger_list"
      ? { ok: true, project: "atlas", offline: false, vocab, rows: [row("a")] }
      : { removed: [] }));
  page();
  await screen.findByText("Row a");
  expect(changes()).toEqual([]);
});
