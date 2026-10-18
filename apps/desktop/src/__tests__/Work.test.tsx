// Work view phase 4 (docs/plans/2026-10-15-work-view.md; mocks
// docs/mockups/2026-10-15-work-view/work.html and work-states.html): the
// Work tab's Waiting on you, Running now and project boards, against
// `khipu work --json` fixtures shaped as packages/cli/khipu/work.py
// build_view prints them, plus the word helpers in work.ts.
(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

// @ts-expect-error node:fs has no types here
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { WorkView } from "../WorkView";
import {
  ageWords,
  badgeLabel,
  boardFolds,
  goalBar,
  plural,
  shortProject,
  sourceLine,
  useWork,
  wordFor,
} from "../work";
import type { LedgerGoal, Vocab, WaitItem, WorkBoard, WorkJob, WorkView as WorkViewDoc } from "../work";
import type { RoutingState } from "../routing";

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
const S = 1000;
const MIN = 60 * S;
const H = 60 * MIN;
const DAY = 24 * H;

const VOCAB: Vocab = {
  state: [
    ["complete", "done"],
    ["doing", "open"],
    ["waiting on owner", "needs"],
    ["scrapped", "dropped"],
  ],
  kind: ["goal", "slice", "needs-owner"],
};

function board(project: string | null, name: string | null, source: WorkBoard["source"], extra: Partial<WorkBoard> = {}): WorkBoard {
  return { project, name, source, jobs: [], prs: [], trains: [], merged: [], ledger: null, flagged: 0, ...extra };
}

function job(project: string, id: string, extra: Partial<WorkJob> = {}): WorkJob {
  return {
    id,
    project,
    state: "running",
    state_known: true,
    state_words: "Running",
    started_at: ago(27 * MIN),
    updated_at: ago(1 * MIN),
    summary: `Job ${id}`,
    for: {},
    now: [],
    account: "Codex",
    model: "gpt-6.1-sol",
    account_ref: { label: "Codex", matched: false },
    ...extra,
  };
}

function wait(project: string | null, id: string, extra: Partial<WaitItem> = {}): WaitItem {
  return {
    source: "ledger",
    key: `ledger:${project}#${id}`,
    project,
    id,
    title: `Item ${id}`,
    kind: "needs-owner",
    since: ago(5 * H),
    blocking: false,
    also_owed: null,
    actions: ["done", "drop", "answered", "open_ledger"],
    ...extra,
  };
}

function view(extra: Partial<WorkViewDoc> = {}): WorkViewDoc {
  const waiting = extra.waiting ?? [];
  return {
    format: "khipu-work-view",
    version: 1,
    generated_at: ago(0),
    projects: [],
    live: [],
    waiting,
    counts: {
      waiting: waiting.length,
      blocking: waiting.filter((w) => w.blocking).length,
      running: 0,
      flagged: 0,
      stale_sources: 0,
      ledger_outbox: 0,
    },
    problems: {},
    ledger_offline: false,
    refresh_s: 5,
    ...extra,
  };
}

const ledgerPart = (vocab: Vocab = VOCAB) => ({
  rows: 3,
  by_state: { doing: 2, "waiting on owner": 1 },
  goals: [],
  loose_ends: [],
  loose_end_count: 2,
  changed_today: [],
  vocab,
});

const stubRouting = (data: RoutingState["data"] = null): RoutingState => ({
  data,
  error: null,
  loading: false,
  lastOkAt: null,
  firstRead: false,
  load: async () => {},
});

function serve(doc: WorkViewDoc, extra: Record<string, (args: Record<string, unknown>) => unknown> = {}) {
  invokeMock.mockImplementation(async (cmd: string, args: Record<string, unknown>) => {
    if (cmd === "khipu_work_view") return JSON.stringify(doc);
    if (extra[cmd]) return JSON.stringify(extra[cmd](args));
    return JSON.stringify({ ok: true });
  });
}

function Harness({
  onOwedClose = async () => {},
  onSnooze = () => {},
  routing = stubRouting(),
  onImportTracker,
  onOpenWorkSettings = () => {},
}: {
  onOwedClose?: (id: number) => Promise<void>;
  onSnooze?: (id: string, title: string) => void;
  routing?: RoutingState;
  onImportTracker?: () => void;
  onOpenWorkSettings?: () => void;
}) {
  const work = useWork(true);
  return (
    <WorkView
      work={work}
      routing={routing}
      plans={null}
      owedSection={<p>The Owed body</p>}
      owedFocusKey={0}
      onOwedClose={onOwedClose}
      onSnooze={onSnooze}
      onOpenRouting={() => {}}
      onImportTracker={onImportTracker}
      onOpenWorkSettings={onOpenWorkSettings}
    />
  );
}

const viewReads = () => invokeMock.mock.calls.filter(([c]) => c === "khipu_work_view").length;

describe("work.ts words", () => {
  it("plural counts one and many", () => {
    expect(plural(1, "row")).toBe("1 row");
    expect(plural(2, "row")).toBe("2 rows");
    expect(plural(1120, "row")).toBe("1,120 rows");
    expect(plural(1, "needs you", "need you")).toBe("1 needs you");
    expect(plural(9, "needs you", "need you")).toBe("9 need you");
  });

  it("ageWords counts minutes, hours, then days", () => {
    const now = new Date(NOW);
    expect(ageWords(ago(10 * S), now)).toBe("just now");
    expect(ageWords(ago(18 * MIN), now)).toBe("18 min");
    expect(ageWords(ago(5 * H), now)).toBe("5 h");
    expect(ageWords(ago(1 * DAY + H), now)).toBe("1 day");
    expect(ageWords(ago(3 * DAY), now)).toBe("3 days");
    expect(ageWords(null, now)).toBe("");
  });

  it("wordFor takes the project's own word for a meaning", () => {
    expect(wordFor(VOCAB, "done")).toBe("complete");
    expect(wordFor(VOCAB, "dropped")).toBe("scrapped");
    expect(wordFor(undefined, "done")).toBeNull();
    const defaults: Vocab = { state: [["VERIFIED", "done"], ["NEEDS OWNER", "needs"], ["DROPPED", "dropped"], ["DONE", "done"]], kind: [] };
    expect(wordFor(defaults, "done")).toBe("DONE");
  });

  it("goalBar names every segment and counts verified and needs you", () => {
    const goal: LedgerGoal = {
      id: "g-1",
      title: "Tester build",
      kind: "goal",
      parent: null,
      state: "IN FLIGHT",
      state_words: "In progress",
      meaning: "open",
      owner: "none",
      updated_at: ago(H),
      children: 5,
      by_state: { VERIFIED: 2, "NEEDS OWNER": 1, QUEUED: 2 },
      verified: 2,
      done: 2,
    };
    const bar = goalBar(goal);
    expect(bar.label).toBe("2 verified, 1 needs you, 2 queued");
    expect(bar.words).toBe("2 of 5 verified · 1 needs you");
    expect(bar.parts).toEqual([
      { cls: "b-ver", pct: 40 },
      { cls: "b-need", pct: 20 },
    ]);
    expect(goalBar({ ...goal, children: 0, by_state: {}, verified: 0 }).words).toBe("No slices yet");
  });

  it("sourceLine words each source state", () => {
    const now = new Date(NOW);
    const at = ago(23 * MIN);
    const line = (source: WorkBoard["source"]) => sourceLine(board("p", "P", source), now);
    expect(line({ state: "ok", as_of: ago(9 * S), dropped: 2, capped: { jobs: { shown: 50, of: 64 } } })).toEqual({
      text: "Updated 9 s ago. 2 items couldn't be read and are left out. Showing the newest 50 of 64 jobs.",
      tone: "ok",
    });
    expect(line({ state: "ok", as_of: ago(9 * S), dropped: 1 }).text).toContain("1 item couldn't be read and is left out.");
    expect(line({ state: "stale", as_of: at }).text).toMatch(/^No update for 23 min; showing what it said at \d+:\d\d [AP]M\.$/);
    expect(line({ state: "stale", as_of: at }).tone).toBe("warn");
    expect(line({ state: "gone", as_of: ago(2 * DAY) }).text).toMatch(/^Not reporting since /);
    expect(line({ state: "missing" }).text).toBe("No summary file yet at the place you set.");
    expect(line({ state: "too_big", problem: "The summary file is 412 KB, over the 256 KB limit.", as_of: at }).text).toMatch(
      /^The summary file is 412 KB, over the 256 KB limit\. Showing what it said at /,
    );
    expect(line({ state: "unreadable", as_of: at }).text).toMatch(/^Couldn't read the newest summary; showing what it said at /);
    expect(line({ state: "unreadable" }).text).toBe("Couldn't read the summary file.");
    expect(
      line({ state: "newer_format", problem: "Written in a newer format (2) than this Khipu reads (1). Update Khipu to see it." }).text,
    ).toBe("Written in a newer format (2) than this Khipu reads (1). Update Khipu to see it.");
    expect(line({ state: "wrong_project", problem: "This file says it is for harbor, not delta." }).text).toBe(
      "This file says it is for harbor, not delta. Nothing from it is shown.",
    );
    expect(line({ state: "refused" }).text).toBe(
      "Not read: the file is inside a private or login folder, is reached through a link, or has more than one name.",
    );
    expect(line({ state: "off" }).text).toBe("Its summary file is turned off.");
    expect(line({ state: "none" }).text).toBe("No job runner reports for this project. The ledger is below.");
  });

  it("sourceLine names the per-project run cap and keeps the notes on a last good read", () => {
    const now = new Date(NOW);
    const at = ago(23 * MIN);
    const line = (source: WorkBoard["source"]) => sourceLine(board("p", "P", source), now).text;
    expect(line({ state: "ok", as_of: ago(9 * S), capped: { runs: { shown: 20, of: 31 } } })).toBe(
      "Updated 9 s ago. Showing the newest 20 of 31 test runs.",
    );
    expect(line({ state: "unreadable", as_of: at, capped: { jobs: { shown: 1, of: 3 } } })).toMatch(
      /^Couldn't read the newest summary; showing what it said at .*\. Showing the newest 1 of 3 jobs\.$/,
    );
    expect(
      line({ state: "unreadable", as_of: at, problem: "The newest summary is older than the last one read; showing the last one." }),
    ).toMatch(/^The newest summary is older than the last one read; showing what it said at \d+:\d\d [AP]M\.$/);
  });

  it("a board folds when nothing runs, nothing is flagged and nothing waits, or when it stopped reporting", () => {
    expect(boardFolds(board("p", "P", { state: "ok" }), 0)).toBe(true);
    expect(boardFolds(board("p", "P", { state: "ok" }), 1)).toBe(false);
    expect(boardFolds(board("p", "P", { state: "ok" }, { flagged: 1 }), 0)).toBe(false);
    expect(boardFolds(board("p", "P", { state: "gone" }, { flagged: 1 }), 3)).toBe(true);
  });

  it("the rail badge's name says what the number counts", () => {
    expect(badgeLabel(12, 3)).toBe("12 waiting on you, 3 blocking");
    expect(badgeLabel(1, 0)).toBe("1 waiting on you");
  });
});

describe("Work tab boards", () => {
  it("shows each source state's words on its board", async () => {
    const states: Array<[string, WorkBoard["source"], RegExp]> = [
      ["Okay", { state: "ok", as_of: ago(9 * S), dropped: 2, capped: { jobs: { shown: 50, of: 64 } } },
        /2 items couldn't be read and are left out\. Showing the newest 50 of 64 jobs\./],
      ["Stale", { state: "stale", as_of: ago(23 * MIN) }, /No update for 23 min; showing what it said at/],
      ["Missing", { state: "missing" }, /No summary file yet at the place you set\./],
      ["Big", { state: "too_big", problem: "The summary file is 412 KB, over the 256 KB limit.", as_of: ago(40 * MIN) },
        /The summary file is 412 KB, over the 256 KB limit\. Showing what it said at/],
      ["Garbled", { state: "unreadable", as_of: ago(30 * MIN) }, /Couldn't read the newest summary; showing what it said at/],
      ["Newer", { state: "newer_format", problem: "Written in a newer format (2) than this Khipu reads (1). Update Khipu to see it." },
        /Written in a newer format \(2\) than this Khipu reads \(1\)\. Update Khipu to see it\./],
      ["Delta", { state: "wrong_project", problem: "This file says it is for harbor, not delta." },
        /This file says it is for harbor, not delta\. Nothing from it is shown\./],
      ["Ember", { state: "refused" }, /Not read: the file is inside a private or login folder, is reached through a link, or has more than one name\./],
      ["Quiet", { state: "off" }, /Its summary file is turned off\./],
      ["Lumen", { state: "none" }, /No job runner reports for this project\. The ledger is below\./],
    ];
    const projects = states.map(([name, source]) => board(name.toLowerCase(), name, source, { flagged: 1 }));
    serve(view({ projects }));
    render(<Harness />);
    for (const [name, , words] of states) {
      const head = await screen.findByRole("heading", { name });
      const card = head.closest(".card") as HTMLElement;
      expect(within(card).getByText(words)).toBeInTheDocument();
    }
  });

  it("offers Raise the limit and Open Settings where the file needs a setting changed", async () => {
    const states: Array<[string, WorkBoard["source"], string | null]> = [
      ["Big", { state: "too_big", problem: "The summary file is 412 KB, over the 256 KB limit.", as_of: ago(40 * MIN) }, "Raise the limit"],
      ["Capped", { state: "ok", as_of: ago(9 * S), capped: { jobs: { shown: 50, of: 64 } } }, "Raise the limit"],
      ["Missing", { state: "missing" }, "Open Settings"],
      ["Delta", { state: "wrong_project", problem: "This file says it is for harbor, not delta." }, "Open Settings"],
      ["Ember", { state: "refused" }, "Open Settings"],
      ["Fine", { state: "ok", as_of: ago(9 * S) }, null],
    ];
    serve(view({ projects: states.map(([name, source]) => board(name.toLowerCase(), name, source, { flagged: 1 })) }));
    const open = vi.fn();
    render(<Harness onOpenWorkSettings={open} />);
    for (const [name, , label] of states) {
      const card = (await screen.findByRole("heading", { name })).closest(".card") as HTMLElement;
      for (const other of ["Raise the limit", "Open Settings"]) {
        if (other !== label) expect(within(card).queryByRole("button", { name: other })).toBeNull();
      }
      if (label) fireEvent.click(within(card).getByRole("button", { name: label }));
    }
    expect(open).toHaveBeenCalledTimes(5);
  });

  it("a board that stopped reporting folds and unfolds with Show", async () => {
    const gone = board("harbor", "Harbor", { state: "gone", as_of: ago(2 * DAY) }, { flagged: 1 });
    serve(view({ projects: [gone], waiting: [wait("harbor", "n-1")] }));
    render(<Harness />);
    const head = await screen.findByRole("heading", { name: "Harbor" });
    const card = head.closest(".card") as HTMLElement;
    expect(card).toHaveClass("folded");
    expect(within(card).getByText(/Not reporting since .* · 1 flagged · 1 waiting on you/)).toBeInTheDocument();
    const show = within(card).getByRole("button", { name: "Show" });
    expect(show).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(show);
    expect(within(card).getByRole("button", { name: "Fold" })).toHaveAttribute("aria-expanded", "true");
    expect(card).not.toHaveClass("folded");
    expect(within(card).getByText(/^Not reporting since .*\.$/)).toBeInTheDocument();
  });

  it("shows the first-run empty state with Import a tracker when there are no projects", async () => {
    serve(view());
    const onImportTracker = vi.fn();
    render(<Harness onImportTracker={onImportTracker} />);
    expect(await screen.findByText("No projects yet")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Import a tracker…" }));
    expect(onImportTracker).toHaveBeenCalledTimes(1);
    // Summary files come with the rest of Settings -> Work (phase 5).
    expect(screen.queryByRole("button", { name: /Add a summary file/ })).toBeNull();
    expect(screen.getByRole("heading", { name: "Owed" })).toBeInTheDocument();
  });

  it("shows the outbox notice in the singular", async () => {
    const v = view({ projects: [board("a", "Atlas", { state: "none" })] });
    v.counts.ledger_outbox = 1;
    serve(v);
    render(<Harness />);
    expect(await screen.findByText("1 ledger change is waiting for the database")).toBeInTheDocument();
  });
});

describe("Waiting on you", () => {
  const atlas = board("atlas", "Atlas", { state: "none" }, { ledger: ledgerPart() });
  const harbor = board("harbor", "Harbor", { state: "none" }, { ledger: ledgerPart() });

  it("keeps the order given, shows 8 then Show all, says Blocking in words, and filters by project", async () => {
    const items = [
      wait("atlas", "n-1", { blocking: true, title: "First, blocking" }),
      ...Array.from({ length: 6 }, (_, i) => wait("atlas", `n-${i + 2}`, { title: `Atlas item ${i + 2}` })),
      wait("harbor", "h-1", { title: "Harbor one" }),
      wait("harbor", "h-2", { title: "Harbor two" }),
      wait(null, "rule:9", { source: "routing", key: "routing:rule:9", title: "Confirm a standing rule a conversation changed", actions: ["open_routing"] }),
    ];
    serve(view({ projects: [atlas, harbor], waiting: items }));
    render(<Harness />);
    const list = await screen.findByRole("list", { name: "Waiting on you" });
    expect(screen.getByText("10 across 3 projects · 1 blocking")).toBeInTheDocument();
    let rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(8);
    expect(rows.map((r) => r.querySelector(".wk-wait-text")?.textContent)).toEqual(items.slice(0, 8).map((i) => i.title));
    expect(within(rows[0]).getByText("Blocking")).toHaveClass("tag", "err");
    const more = screen.getByRole("button", { name: "Show all 10" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(more);
    rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(10);
    expect(within(rows[9]).getByText("Khipu")).toBeInTheDocument();
    expect(within(rows[9]).getByRole("button", { name: "Open Routing settings" })).toBeInTheDocument();
    const filter = screen.getByRole("button", { name: "Harbor 2" });
    expect(filter).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(filter);
    expect(filter).toHaveAttribute("aria-pressed", "true");
    rows = within(list).getAllByRole("listitem");
    expect(rows.map((r) => r.querySelector(".wk-wait-text")?.textContent)).toEqual(["Harbor one", "Harbor two"]);
    fireEvent.click(screen.getByRole("button", { name: "Blocking 1" }));
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
  });

  it("names an item's project by Owed's rule, and never calls a withheld or missing one Khipu", async () => {
    const owed = (project: string | null, id: string, extra: Partial<WaitItem> = {}) =>
      wait(project, id, { source: "commitment", key: `commitment:${id}`, title: `Owed ${id}`, actions: ["done", "open_owed"], ...extra });
    const items = [
      owed("acme/aegis", "1"),
      owed(null, "2", { masked: true }),
      owed(null, "3"),
      owed("claude_code:abc", "4"),
    ];
    serve(view({ projects: [], waiting: items }));
    render(<Harness />);
    const list = await screen.findByRole("list", { name: "Waiting on you" });
    const rows = within(list).getAllByRole("listitem");
    const words = (i: number) => rows[i].querySelector(".wk-wait-meta")?.textContent ?? rows[i].textContent ?? "";
    expect(words(0)).toContain(shortProject("acme/aegis"));
    expect(words(1)).toContain("Name withheld");
    expect(words(2)).toContain("No project");
    expect(words(3)).toContain("Session");
    for (const i of [0, 1, 2, 3]) expect(within(rows[i]).queryByText("Khipu")).toBeNull();
    expect(screen.queryByRole("button", { name: /^Khipu / })).toBeNull();
  });

  it("the project filter row wraps inside the card instead of running past its edge", () => {
    const css = readFileSync(`${(globalThis as unknown as { process: { cwd(): string } }).process.cwd()}/src/Work.css`, "utf8");
    const rule = css.match(/\.wk-filters \.seg \{([^}]*)\}/)?.[1] ?? "";
    // App.css's .seg is `flex: none`: as a flex item it keeps its one-line
    // width, so its own flex-wrap never fires. It has to shrink to the card.
    expect(rule).toMatch(/flex:\s*1 1 auto/);
    expect(rule).toMatch(/min-width:\s*0/);
    expect(rule).toMatch(/flex-wrap:\s*wrap/);
  });

  it("Done on a ledger item sets the project's own done word, then reads the view again", async () => {
    serve(view({ projects: [atlas], waiting: [wait("atlas", "n-1", { title: "Choose the provider" })] }), {
      khipu_ledger_set: () => ({ ok: true, line: "n-1 -> complete" }),
    });
    render(<Harness />);
    const list = await screen.findByRole("list", { name: "Waiting on you" });
    await waitFor(() => expect(viewReads()).toBe(1));
    fireEvent.click(within(list).getByRole("button", { name: "Done" }));
    await waitFor(() =>
      expect(invokeMock).toHaveBeenCalledWith("khipu_ledger_set", {
        project: "atlas",
        id: "n-1",
        change: { state: "complete", if_state: "waiting on owner" },
      }),
    );
    await waitFor(() => expect(viewReads()).toBeGreaterThanOrEqual(2));
  });

  it("Done on an item also in Owed closes the commitment too", async () => {
    const onOwedClose = vi.fn(async () => {});
    serve(view({ projects: [atlas], waiting: [wait("atlas", "n-1", { also_owed: "42", actions: ["done", "open_owed"] })] }), {
      khipu_ledger_set: () => ({ ok: true, state: "complete" }),
    });
    render(<Harness onOwedClose={onOwedClose} />);
    const list = await screen.findByRole("list", { name: "Waiting on you" });
    expect(within(list).getByText("Also in Owed")).toBeInTheDocument();
    fireEvent.click(within(list).getByRole("button", { name: "Done" }));
    await waitFor(() => expect(onOwedClose).toHaveBeenCalledWith(42));
  });

  it("shows a refused write in the CLI's words", async () => {
    serve(view({ projects: [atlas], waiting: [wait("atlas", "n-1")] }), {
      khipu_ledger_set: () => ({ ok: false, error: "row n-1 moved to doing since you looked", reason: "moved" }),
    });
    render(<Harness />);
    const list = await screen.findByRole("list", { name: "Waiting on you" });
    fireEvent.click(within(list).getByRole("button", { name: "Done" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Khipu didn't make that change: row n-1 moved to doing since you looked",
    );
  });

  it("Hide and These are the same go through their commands", async () => {
    const commitment = wait("harbor", "8", {
      source: "commitment",
      key: "commitment:8",
      kind: "question",
      title: "Keep the old export format?",
      looks_like: { id: "d-1", title: "Decide on the export format" },
      actions: ["done", "snooze", "open_owed", "hide", "same", "not_same"],
    });
    const onSnooze = vi.fn();
    serve(view({ projects: [harbor], waiting: [commitment] }), {
      khipu_work_hide: () => ({ ok: true }),
      khipu_work_same: () => ({ ok: true }),
    });
    render(<Harness onSnooze={onSnooze} />);
    const list = await screen.findByRole("list", { name: "Waiting on you" });
    expect(within(list).getByText("Owed: question")).toBeInTheDocument();
    expect(within(list).getByText("Looks like “Decide on the export format” in the ledger")).toBeInTheDocument();
    fireEvent.click(within(list).getByRole("button", { name: "These are the same" }));
    await waitFor(() =>
      expect(invokeMock).toHaveBeenCalledWith("khipu_work_same", { project: "harbor", row: "d-1", commitment: "8", same: true }),
    );
    await waitFor(() => expect(within(list).getByRole("button", { name: "Hide until it changes" })).not.toBeDisabled());
    fireEvent.click(within(list).getByRole("button", { name: "Hide until it changes" }));
    await waitFor(() => expect(invokeMock).toHaveBeenCalledWith("khipu_work_hide", { id: "8", hide: true }));
    fireEvent.click(within(list).getByRole("button", { name: "Snooze" }));
    expect(onSnooze).toHaveBeenCalledWith("8", "Keep the old export format?");
  });

  it("says nothing is waiting when nothing is", async () => {
    serve(view({ projects: [atlas] }));
    render(<Harness />);
    expect(await screen.findByText("Nothing is waiting on you in any project.")).toBeInTheDocument();
  });
});

describe("Running now", () => {
  const lines = [1, 2, 3, 4].map((n) => ({ at: ago((10 - n) * MIN), text: `Line ${n}` }));

  it("shows one status line and up to 3 when the job is opened", async () => {
    const j = job("atlas", "j-1", { summary: "Fixing the two review findings", for: { pr: "41" }, now: lines });
    serve(view({ projects: [board("atlas", "Atlas", { state: "ok", as_of: ago(5 * S) }, { jobs: [j] })], live: [j] }));
    render(<Harness />);
    const list = await screen.findByRole("list", { name: "Running jobs" });
    expect(screen.getByText("1 job in 1 project")).toBeInTheDocument();
    const status = within(list).getByRole("list", { name: "What it says it is doing" });
    expect(within(status).getAllByRole("listitem").map((l) => l.textContent?.replace(/^.*?[AP]M/, ""))).toEqual(["Line 4"]);
    const title = within(list).getByRole("button", { name: /Fixing the two review findings · pull request 41/ });
    expect(title).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(title);
    expect(title).toHaveAttribute("aria-expanded", "true");
    expect(within(status).getAllByRole("listitem").map((l) => l.textContent?.replace(/^.*?[AP]M/, ""))).toEqual([
      "Line 4",
      "Line 3",
      "Line 2",
    ]);
    expect(within(list).getByText("Codex · gpt-6.1-sol")).toBeInTheDocument();
    expect(within(list).getByText("Running")).toBeInTheDocument();
  });

  it("a quiet job says how long it has been silent", async () => {
    const j = job("harbor", "j-2", { quiet: true, updated_at: ago(18 * MIN), now: lines });
    serve(view({ projects: [board("harbor", "Harbor", { state: "ok", as_of: ago(5 * S) }, { jobs: [j] })], live: [j] }));
    render(<Harness />);
    expect(await screen.findByText("No word from this job for 18 min")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "What it says it is doing" })).toBeNull();
  });

  it("a job on a stale board carries its as-of time", async () => {
    const j = job("quarry", "j-3");
    serve(view({ projects: [board("quarry", "Quarry", { state: "stale", as_of: ago(23 * MIN) }, { jobs: [j] })], live: [j] }));
    render(<Harness />);
    const list = await screen.findByRole("list", { name: "Running jobs" });
    expect(within(list).getByText(/^as of \d+:\d\d [AP]M$/)).toHaveClass("tag", "warn");
  });

  it("says no jobs are running and how many projects reported", async () => {
    serve(view({ projects: [board("a", "Atlas", { state: "ok", as_of: ago(5 * S) }), board("b", "Bay", { state: "ok", as_of: ago(5 * S) })] }));
    render(<Harness />);
    expect(await screen.findByText("No jobs are running. 2 projects reported in the last minute.")).toBeInTheDocument();
  });
});

describe("Withheld fields and an incomplete view", () => {
  it("shows plain words where the view withheld a name, title or summary, and offers no change it can't name", async () => {
    const named = board("atlas", null, { state: "ok", as_of: ago(9 * S) }, {
      ledger: ledgerPart(),
      jobs: [job("atlas", "j-1", { summary: null })],
      prs: [{ id: "41", stage: "open", stage_known: true, stage_words: "Open", title: null, stage_at: ago(H) }],
      flagged: 1,
    });
    const keyless = board(null, null, { state: "none" }, { flagged: 1 });
    const withheld = wait("atlas", "n-1", { title: null });
    const nameless = wait(null, "n-2", { key: null, id: null, title: null });
    const v = view({ projects: [named, keyless], waiting: [withheld, nameless], live: [job("atlas", "j-1", { summary: null })] });
    serve(v);
    render(<Harness />);

    // A withheld board name falls back to its key; with no key either, plain words.
    const atlas = (await screen.findByRole("heading", { name: "atlas" })).closest(".card") as HTMLElement;
    expect(within(atlas).getByText("Title withheld")).toBeInTheDocument();
    expect(within(atlas).getByRole("button", { name: "Open the ledger" })).toBeInTheDocument();
    const nobody = screen.getByRole("heading", { name: "Name withheld" }).closest(".card") as HTMLElement;
    expect(within(nobody).queryByRole("button", { name: "Open the ledger" })).toBeNull();

    const running = screen.getByRole("list", { name: "Running jobs" });
    expect(within(running).getByRole("button", { name: /Summary withheld/ })).toBeInTheDocument();

    const rows = within(screen.getByRole("list", { name: "Waiting on you" })).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("Title withheld")).toBeInTheDocument();
    expect(within(rows[0]).getByRole("button", { name: "Done" })).toBeInTheDocument();
    // No key, id or project: shown, but nothing to change.
    expect(within(rows[1]).getByText("Title withheld")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Name withheld")).toBeInTheDocument();
    expect(within(rows[1]).queryByRole("button")).toBeNull();
    expect(screen.queryByText(/null|undefined/)).toBeNull();
  });

  it("says the view is incomplete when work.py couldn't load its privacy list", async () => {
    serve(view({
      projects: [board("atlas", "Atlas", { state: "none" })],
      problems: { privacy: "Khipu couldn't load its list of login folders and user names." },
    }));
    render(<Harness />);
    expect(
      await screen.findByText(
        "This view is incomplete: Khipu couldn't load its list of login folders and user names, so every name, title and line that could hold one is left out.",
      ),
    ).toBeInTheDocument();
  });

  it("shows no incomplete-view notice when the privacy list loaded", async () => {
    serve(view({ projects: [board("atlas", "Atlas", { state: "none" })] }));
    render(<Harness />);
    await screen.findByRole("heading", { name: "Atlas" });
    expect(screen.queryByText(/This view is incomplete/)).toBeNull();
  });
});
