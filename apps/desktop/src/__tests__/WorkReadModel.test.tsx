// Work sync, slice 3 (the read model and the app): polls that never pile up,
// "Updated" from when the view was built, a ledger page that refreshes
// without wiping an edit, and offline and waiting-write state that always shows.
(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, renderHook, screen, within } from "@testing-library/react";
import { LedgerPage } from "../LedgerPage";
import { WorkView } from "../WorkView";
import { useWork } from "../work";
import type { LedgerRow, Vocab, WorkBoard, WorkView as WorkViewDoc } from "../work";
import type { RoutingState } from "../routing";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
  vi.useRealTimers();
});

const NOW = Date.now();
const ago = (ms: number) => new Date(NOW - ms).toISOString();
const MIN = 60_000;
const H = 60 * MIN;

const VOCAB: Vocab = {
  state: [
    ["VERIFIED", "done"],
    ["IN FLIGHT", "open"],
    ["QUEUED", "open"],
    ["NEEDS OWNER", "needs"],
    ["DROPPED", "dropped"],
    ["DONE", "done"],
  ],
  kind: ["goal", "slice", "needs-owner"],
};

function view(extra: Partial<WorkViewDoc> = {}): WorkViewDoc {
  return {
    format: "khipu-work-view",
    version: 1,
    generated_at: ago(0),
    projects: [],
    live: [],
    waiting: [],
    counts: { waiting: 0, blocking: 0, running: 0, flagged: 0, stale_sources: 0, ledger_outbox: 0 },
    problems: {},
    ledger_offline: false,
    refresh_s: 5,
    ...extra,
  };
}

const stubRouting: RoutingState = { data: null, error: null, loading: false, lastOkAt: null, firstRead: false, load: async () => {} };

function Harness() {
  const work = useWork(true);
  return (
    <WorkView
      work={work}
      routing={stubRouting}
      plans={null}
      owedSection={<p>The Owed body</p>}
      owedFocusKey={0}
      onOwedClose={async () => {}}
      onSnooze={() => {}}
      onOpenRouting={() => {}}
      onOpenWorkSettings={() => {}}
    />
  );
}

const serveView = (doc: WorkViewDoc) =>
  invokeMock.mockImplementation(async (cmd: string) => (cmd === "khipu_work_view" ? JSON.stringify(doc) : JSON.stringify({ ok: true })));

const reads = (cmd: string) => invokeMock.mock.calls.filter(([c]) => c === cmd).length;
const tick = (ms: number) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

describe("useWork polling", () => {
  it("starts the next read a full interval after a slow read ends, never during it", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    invokeMock.mockImplementation(
      (cmd: string) =>
        new Promise((resolve) => setTimeout(() => resolve(cmd === "khipu_work_view" ? JSON.stringify(view()) : "{}"), 13_000)),
    );
    renderHook(() => useWork(true));
    await tick(0);
    expect(reads("khipu_work_view")).toBe(1);
    await tick(12_900);
    expect(reads("khipu_work_view")).toBe(1);
    await tick(100); // the read ends at 13 s
    await tick(4_900); // 17.9 s
    expect(reads("khipu_work_view")).toBe(1);
    await tick(200); // 18.1 s
    expect(reads("khipu_work_view")).toBe(2);
  });

  it("load(true) after a write waits for the read under way, then reads again", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    invokeMock.mockImplementation(
      () => new Promise((resolve) => setTimeout(() => resolve(JSON.stringify(view())), 1_000)),
    );
    const { result } = renderHook(() => useWork(true));
    await tick(0);
    let done = false;
    act(() => {
      void result.current.load(true).then(() => {
        done = true;
      });
    });
    expect(reads("khipu_work_view")).toBe(1);
    await tick(1_000);
    expect(reads("khipu_work_view")).toBe(2);
    await tick(1_000);
    expect(done).toBe(true);
  });
});

describe("Work tab freshness", () => {
  it("counts Updated from when the view was built, not when it arrived", async () => {
    serveView(view({ generated_at: ago(3 * MIN) }));
    render(<Harness />);
    expect(await screen.findByText(/^Updated 3 min ago$/)).toBeInTheDocument();
  });

  it("says the ledger is the offline copy even with no waiting writes", async () => {
    serveView(view({ ledger_offline: true, ledger_as_of: ago(2 * H) }));
    render(<Harness />);
    expect(await screen.findByText(/offline copy/i)).toBeInTheDocument();
  });

  it("shows a waiting-writes problem the CLI reports", async () => {
    serveView(view({ problems: { outbox: "synthetic" }, counts: { ...view().counts, ledger_refused: 2 } as WorkViewDoc["counts"] }));
    render(<Harness />);
    expect(await screen.findByText(/ledger changes saved on this Mac/i)).toBeInTheDocument();
  });

  it("a stale board never calls offline ledger parts live", async () => {
    const atlas: WorkBoard = {
      project: "atlas",
      name: "Atlas",
      source: { state: "stale", problem: null, as_of: ago(20 * MIN), dropped: 0, other_project: 0, capped: {}, clamped: 0, waiting_list_ignored: 0 },
      jobs: [],
      prs: [],
      trains: [],
      merged: [],
      flagged: 0,
      ledger: { rows: 1, by_state: { QUEUED: 1 }, goals: [], loose_ends: [], loose_end_count: 0, changed_today: [], vocab: VOCAB },
    } as unknown as WorkBoard;
    serveView(view({ projects: [atlas], ledger_offline: true, ledger_as_of: ago(H) }));
    render(<Harness />);
    await screen.findByText("Atlas");
    fireEvent.click(screen.getByRole("button", { name: /^Show/ }));
    expect(await screen.findByText(/Its jobs show with/)).toBeInTheDocument();
    expect(screen.queryByText(/The ledger parts are live/)).toBeNull();
    expect(screen.getByText(/The ledger parts are from the offline copy/)).toBeInTheDocument();
  });
});

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

function serveLedger(rows: () => LedgerRow[], list: Record<string, unknown> = {}) {
  invokeMock.mockImplementation(async (cmd: string, args: Record<string, unknown>) => {
    if (cmd === "khipu_ledger_list") {
      return JSON.stringify({ ok: true, project: "atlas", offline: false, as_of: null, vocab: VOCAB, rows: rows(), ...list });
    }
    if (cmd === "khipu_ledger_removed") return JSON.stringify({ removed: [] });
    if (cmd === "khipu_ledger_get") {
      const hit = rows().find((r) => r.id === args.id);
      return JSON.stringify(hit ? { ...hit, offline: false, as_of: null } : { ok: false, error: "no row" });
    }
    return JSON.stringify({ ok: true });
  });
}

function ledgerPage(initialRow?: string) {
  return render(
    <LedgerPage
      project="atlas"
      name="Atlas"
      initialRow={initialRow}
      view={null}
      onBack={() => {}}
      onChanged={async () => {}}
      onOwedClose={async () => {}}
    />,
  );
}

describe("LedgerPage refresh", () => {
  it("reads the list again after each interval and shows a change made elsewhere", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    let title = "Before";
    serveLedger(() => [row("s-1", { title })]);
    ledgerPage();
    await tick(0);
    expect(screen.getByText("Before")).toBeInTheDocument();
    title = "Changed elsewhere";
    await tick(5_100);
    expect(screen.getByText("Changed elsewhere")).toBeInTheDocument();
  });

  it("keeps an edit being typed while the row changes elsewhere", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    let current = row("s-1", { title: "Original" });
    serveLedger(() => [current]);
    ledgerPage("s-1");
    await tick(0);
    const form = screen.getByRole("form", { name: "Change this row" });
    fireEvent.change(within(form).getByLabelText("Title"), { target: { value: "My new title" } });
    current = { ...current, evidence: "Added elsewhere", updated: ago(0) };
    await tick(5_100);
    await tick(5_100);
    expect(within(screen.getByRole("form", { name: "Change this row" })).getByLabelText("Title")).toHaveValue("My new title");
  });

  it("refreshes the open row when its form is untouched", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    let current = row("s-1", { title: "Original" });
    serveLedger(() => [current]);
    ledgerPage("s-1");
    await tick(0);
    current = { ...current, title: "Renamed elsewhere", updated: ago(0) };
    await tick(5_100);
    expect(within(screen.getByRole("form", { name: "Change this row" })).getByLabelText("Title")).toHaveValue("Renamed elsewhere");
  });

  it("stops reading once the page closes", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    serveLedger(() => [row("s-1")]);
    const { unmount } = ledgerPage();
    await tick(0);
    await tick(5_100);
    const before = reads("khipu_ledger_list");
    expect(before).toBeGreaterThanOrEqual(2);
    unmount();
    await tick(20_000);
    expect(reads("khipu_ledger_list")).toBe(before);
  });
});

describe("LedgerPage offline and waiting writes", () => {
  it("says it shows the offline copy even when nothing is waiting", async () => {
    serveLedger(() => [row("s-1")], { offline: true, as_of: ago(2 * H), pending: 0 });
    ledgerPage();
    expect(await screen.findByText(/offline copy/i)).toBeInTheDocument();
  });

  it("marks rows still waiting to be sent and counts them", async () => {
    serveLedger(() => [row("s-1"), row("s-2", { queued: true } as Partial<LedgerRow>)], { pending: 1 });
    ledgerPage();
    expect(await screen.findByText("Not sent yet")).toBeInTheDocument();
    expect(screen.getByText(/1 change is waiting to be sent/)).toBeInTheDocument();
  });
});
