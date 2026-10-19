// Work view phase 4 (docs/plans/2026-10-15-work-view.md; mock
// docs/mockups/2026-10-15-work-view/ledger.html): a project's ledger page
// against `khipu ledger list|get|removed --json` fixtures shaped as
// packages/cli/khipu/ledger.py row_json prints them.
(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { LedgerPage } from "../LedgerPage";
import type { HistoryEntry, LedgerRow, RemovedRow, Vocab } from "../work";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
});

const NOW = Date.now();
const ago = (ms: number) => new Date(NOW - ms).toISOString();
const H = 3_600_000;
const DAY = 24 * H;

const VOCAB: Vocab = {
  state: [
    ["VERIFIED", "done"],
    ["MERGED-UNVERIFIED", "open"],
    ["BUILT-UNMERGED", "open"],
    ["IN FLIGHT", "open"],
    ["QUEUED", "open"],
    ["NOT STARTED", "open"],
    ["NEEDS OWNER", "needs"],
    ["DROPPED", "dropped"],
    ["DONE", "done"],
  ],
  kind: ["goal", "slice", "review", "loose-end", "needs-owner"],
};

function row(id: string, extra: Partial<LedgerRow> = {}): LedgerRow {
  return {
    id,
    title: `Row ${id}`,
    kind: "slice",
    parent: "",
    state: "QUEUED",
    evidence: "",
    owner: "none",
    updated: ago(H),
    link: "",
    refs: {},
    project: "atlas",
    history: [],
    ...extra,
  };
}

type Handlers = Record<string, (args: Record<string, unknown>) => unknown>;

function serve(rows: () => LedgerRow[], extra: Handlers = {}, removed: () => RemovedRow[] = () => []) {
  invokeMock.mockImplementation(async (cmd: string, args: Record<string, unknown>) => {
    if (extra[cmd]) return JSON.stringify(extra[cmd](args));
    if (cmd === "khipu_ledger_list") {
      return JSON.stringify({ ok: true, project: "atlas", offline: false, as_of: null, vocab: VOCAB, rows: rows() });
    }
    if (cmd === "khipu_ledger_removed") return JSON.stringify({ removed: removed() });
    if (cmd === "khipu_ledger_get") {
      const hit = rows().find((r) => r.id === args.id);
      return JSON.stringify(hit ?? { ok: false, error: `no row ${String(args.id)}` });
    }
    return JSON.stringify({ ok: true });
  });
}

function page(onChanged = vi.fn(async () => {}), onOwedClose = vi.fn(async () => {})) {
  render(
    <LedgerPage
      project="atlas"
      name="Atlas"
      view={null}
      onBack={() => {}}
      onChanged={onChanged}
      onOwedClose={onOwedClose}
    />,
  );
  return { onChanged, onOwedClose };
}

const rowButtons = () => document.querySelectorAll("button.lg-row");
const calls = (cmd: string) => invokeMock.mock.calls.filter(([c]) => c === cmd);

describe("LedgerPage", () => {
  it("renders a 1,000-row ledger 100 rows at a time", async () => {
    const rows = [
      ...Array.from({ length: 10 }, (_, i) => row(`g-${i}`, { kind: "goal", state: "IN FLIGHT", title: `Goal ${i}` })),
      ...Array.from({ length: 990 }, (_, i) =>
        row(`s-${i}`, { parent: `g-${i % 10}`, state: i < 200 ? "VERIFIED" : "QUEUED" }),
      ),
    ];
    serve(() => rows);
    page();
    await waitFor(() => expect(rowButtons()).toHaveLength(100));
    expect(screen.getByRole("heading", { name: "1,000 rows" })).toBeInTheDocument();
    expect(screen.getByText("Showing 100 of 800 rows. 200 finished rows are hidden.")).toBeInTheDocument();
    expect(screen.getByText("Goals")).toHaveClass("lg-group");
    expect(screen.getAllByText("Under: Goal 0").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Show the next 100" }));
    expect(rowButtons()).toHaveLength(200);
    fireEvent.click(screen.getByRole("button", { name: "Hide finished" }));
    expect(screen.getByRole("button", { name: "Hide finished" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText("Showing 100 of 1,000 rows.")).toBeInTheDocument();
  });

  it("words the row counts with the right plurals", async () => {
    serve(() => [row("n-1", { kind: "needs-owner", state: "NEEDS OWNER" })]);
    page();
    expect(await screen.findByRole("heading", { name: "1 row" })).toBeInTheDocument();
    expect(screen.getByText(/· 0 open · 1 needs you · last change/)).toBeInTheDocument();
    cleanup();
    serve(() => [
      row("a"),
      ...Array.from({ length: 9 }, (_, i) => row(`n-${i}`, { kind: "needs-owner", state: "NEEDS OWNER" })),
      row("b"),
    ]);
    page();
    expect(await screen.findByRole("heading", { name: "11 rows" })).toBeInTheDocument();
    expect(screen.getByText(/· 2 open · 9 need you · last change/)).toBeInTheDocument();
    cleanup();
    serve(() => [row("a"), row("b")]);
    page();
    expect(await screen.findByRole("heading", { name: "2 rows" })).toBeInTheDocument();
  });

  it("changing a row's state sends if_state and the new state, then reads the row again and shows the change", async () => {
    let current = row("s-1", {
      title: "Second sign-in provider",
      history: [{ ts: ago(2 * DAY), from: null, to: "QUEUED", note: "seeded" } as HistoryEntry],
    });
    serve(() => [current], {
      khipu_ledger_set: (args) => {
        const change = args.change as { state: string; note?: string };
        current = {
          ...current,
          state: change.state,
          updated: ago(0),
          history: [
            ...current.history,
            { ts: ago(0), from: "QUEUED", to: change.state, note: change.note ?? "", by: "you, in the app", changes: {} },
          ],
        };
        return { ok: true, line: "s-1 -> IN FLIGHT" };
      },
    });
    const { onChanged } = page();
    await waitFor(() => expect(rowButtons()).toHaveLength(1));
    fireEvent.click(rowButtons()[0]);
    const form = await screen.findByRole("form", { name: "Change this row" });
    expect(rowButtons()[0]).toHaveAttribute("aria-current", "true");
    expect(screen.getByText("Added as Queued")).toBeInTheDocument();
    expect(screen.getByText("imported, author not recorded", { exact: false })).toBeInTheDocument();
    fireEvent.change(within(form).getByLabelText("New state"), { target: { value: "IN FLIGHT" } });
    fireEvent.change(within(form).getByLabelText("Your note"), { target: { value: "Started on it" } });
    const gets = calls("khipu_ledger_get").length;
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(invokeMock).toHaveBeenCalledWith("khipu_ledger_set", {
        project: "atlas",
        id: "s-1",
        change: { state: "IN FLIGHT", note: "Started on it", if_state: "QUEUED" },
      }),
    );
    await waitFor(() => expect(calls("khipu_ledger_get").length).toBeGreaterThan(gets));
    expect(await screen.findByText("Queued → In progress")).toBeInTheDocument();
    expect(screen.getByText("“Started on it”")).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalled();
    const hist = screen.getByRole("list", { name: "History of s-1" });
    expect(within(hist).getAllByRole("listitem")[0]).toHaveTextContent("Queued → In progress");
  });

  it("a Needs you row is answered with the done word and closes its Owed item too", async () => {
    let current = row("n-7", { kind: "needs-owner", state: "NEEDS OWNER", refs: { commitment: 42 } });
    serve(() => [current], {
      khipu_ledger_set: () => {
        current = { ...current, state: "DONE" };
        return { ok: true, state: "DONE" };
      },
    });
    const { onOwedClose } = page();
    await waitFor(() => expect(rowButtons()).toHaveLength(1));
    fireEvent.click(rowButtons()[0]);
    const form = await screen.findByRole("form", { name: "Answer this" });
    expect(within(form).getByLabelText("New state")).toHaveValue("DONE");
    expect(within(form).getByText("Saved as “you, in the app”. The Owed item closes too.")).toBeInTheDocument();
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onOwedClose).toHaveBeenCalledWith(42));
  });

  it.each([
    ["move-up", "Move them up to the top level"],
    ["remove", "Remove them too"],
  ])("Remove on a row with children sends children %s, then the row leaves the list", async (choice, label) => {
    let rows = [
      row("g-1", { kind: "goal", title: "Sign-in rework", state: "IN FLIGHT" }),
      row("s-1", { parent: "g-1" }),
      row("s-2", { parent: "g-1" }),
    ];
    serve(() => rows, {
      khipu_ledger_remove: () => {
        rows = rows.filter((r) => r.id !== "g-1");
        return { ok: true, line: "removed g-1" };
      },
    });
    const { onChanged } = page();
    await waitFor(() => expect(rowButtons()).toHaveLength(3));
    fireEvent.click(rowButtons()[0]);
    await screen.findByRole("form", { name: "Change this row" });
    fireEvent.click(screen.getByRole("button", { name: "Remove…" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove g-1?" });
    expect(within(dialog).getByText("The 2 rows under it")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Move them up to the top level")).toBeChecked();
    fireEvent.click(within(dialog).getByLabelText(label));
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() =>
      expect(invokeMock).toHaveBeenCalledWith("khipu_ledger_remove", { project: "atlas", id: "g-1", children: choice }),
    );
    await waitFor(() => expect(rowButtons()).toHaveLength(2));
    expect(screen.queryByText("Row g-1")).toBeNull();
    expect(onChanged).toHaveBeenCalled();
  });

  it("Removed (N) lists removed rows and Restore calls restore", async () => {
    const removed: RemovedRow[] = [
      { project: "atlas", id: "le-9", title: "Duplicate loose end", kind: "loose-end", state: "QUEUED",
        removed_at: ago(DAY), removed_by: "you, in the app", note: "", days_left: 29 },
      { project: "atlas", id: "le-10", title: "Written by mistake", kind: "loose-end", state: "QUEUED",
        removed_at: ago(2 * DAY), removed_by: "a Claude Code conversation in Atlas", note: "", days_left: 1 },
    ];
    serve(() => [row("a")], { khipu_ledger_restore: () => ({ ok: true }) }, () => removed);
    const { onChanged } = page();
    fireEvent.click(await screen.findByRole("button", { name: "Removed (2)" }));
    const dialog = await screen.findByRole("dialog", { name: "Removed rows" });
    expect(within(dialog).getByText("Duplicate loose end")).toBeInTheDocument();
    expect(within(dialog).getByText(/Can be restored for 29 more days\./)).toBeInTheDocument();
    expect(within(dialog).getByText(/Can be restored for 1 more day\./)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Restore le-9" }));
    await waitFor(() => expect(invokeMock).toHaveBeenCalledWith("khipu_ledger_restore", { project: "atlas", id: "le-9" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("names a missing parent and an old open row", async () => {
    serve(() => [row("le-33", { kind: "loose-end", parent: "g-old-2", updated: ago(9 * DAY) })]);
    page();
    expect(await screen.findByText("Parent not found: g-old-2")).toBeInTheDocument();
    expect(screen.getByText("9 days")).toHaveClass("lg-when", "old");
  });
});
