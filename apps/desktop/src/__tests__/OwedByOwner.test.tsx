// Owed by owner, paging, many at once and the clean-up (docs/plans/
// 2026-10-16-owed-and-ledger-ssot.md, slice 5), through App's real Owed
// reads and writes. The paged answers are shaped as `khipu owed --owner=all`
// prints them (mcp_server._tool_owed): results, total, counts, next_cursor.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import App from "../App";
import { WELCOME_DONE_KEY } from "../Welcome";

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

type Item = Record<string, unknown> & { id: number };

const item = (id: number, owner: string, over: Record<string, unknown> = {}): Item => ({
  id, text: `Item number ${id}`, project: "acme/atlas", kind: "followup", status: "open",
  owner, owner_label: owner === "agent" ? "orchestrator" : null, opened_episode: 5, opened_at: "2026-10-15T12:00:00Z",
  ...over,
});

/** A flood shaped like project A's: mostly the orchestrator's, some the
 *  assistant's, the rest the owner's. */
function flood(n: number): Item[] {
  return Array.from({ length: n }, (_, i) => {
    const id = n - i;
    return item(id, id % 10 === 0 ? "user" : id % 10 < 3 ? "assistant" : "agent");
  });
}

const PAGE = 200;

/** `khipu owed` paged by id, newest first, like the CLI's keyset, with its
 *  owner filter. */
function pager(rows: Item[], over: Record<string, unknown> = {}) {
  return (args: string[]) => {
    const flag = (name: string) => args.find((a) => a.startsWith(`--${name}=`))?.slice(name.length + 3);
    const status = flag("status") ?? "open";
    const who = flag("owner") ?? "all";
    const inStatus = rows.filter((r) => (r.status ?? "open") === status).sort((a, b) => b.id - a.id);
    const mine = inStatus.filter((r) => who === "all" || r.owner === who);
    const cursor = flag("cursor");
    const after = cursor ? mine.filter((r) => r.id < Number(cursor)) : mine;
    const limit = Number(flag("limit") ?? 50);
    const page = after.slice(0, limit);
    const owner: Record<string, number> = {};
    const kind: Record<string, number> = {};
    // The owner counts ignore the owner filter, as commitments.page_owed's do.
    for (const r of inStatus) owner[String(r.owner)] = (owner[String(r.owner)] ?? 0) + 1;
    for (const r of mine) kind[String(r.kind)] = (kind[String(r.kind)] ?? 0) + 1;
    return {
      status, results: page, total: mine.length, counts: { owner, kind },
      next_cursor: after.length > page.length ? String(page[page.length - 1].id) : null, limit, ...over,
    };
  };
}

type Handler = (payload: Record<string, unknown>) => unknown;

function serve(owed: (args: string[]) => unknown, extra: Record<string, Handler> = {}) {
  invokeMock.mockImplementation(async (cmd: string, payload?: Record<string, unknown>) => {
    if (cmd === "dsn_configured") return true;
    if (extra[cmd]) return JSON.stringify(extra[cmd](payload ?? {}));
    if (cmd !== "run_khipu") return JSON.stringify({ ok: true });
    const args = (payload?.args as string[]) ?? [];
    if (args[0] === "owed") return JSON.stringify(owed(args));
    return "{}";
  });
}

const owedReads = () =>
  invokeMock.mock.calls
    .filter(([cmd, p]) => cmd === "run_khipu" && (p as { args: string[] }).args[0] === "owed")
    .map(([, p]) => (p as { args: string[] }).args);
const callsOf = (cmd: string) => invokeMock.mock.calls.filter(([c]) => c === cmd).map(([, p]) => p);

async function openWork() {
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: /^Work/ }));
}

function group(name: RegExp) {
  const summary = screen.getAllByText(name, { selector: "strong" })[0].closest("details");
  if (!summary) throw new Error(`no group ${name}`);
  return summary as HTMLElement;
}

describe("Owed by owner", () => {
  it("heads each owner's group with the CLI's own count and shows an agent's label", async () => {
    serve(pager(flood(4000)));
    await openWork();
    await waitFor(() => expect(document.querySelectorAll(".owed-row").length).toBe(3 * PAGE));
    expect(within(group(/^Needs you$/)).getByText("400")).toBeInTheDocument();
    expect(within(group(/^Yours \(the assistant's\)$/)).getByText("800")).toBeInTheDocument();
    expect(within(group(/^For agents and jobs$/)).getByText("2,800")).toBeInTheDocument();
    expect(within(group(/^For agents and jobs$/)).getAllByText("orchestrator").length).toBeGreaterThan(0);
    const reads = owedReads();
    for (const owner of ["user", "assistant", "agent"]) {
      expect(reads.some((a) => a.includes("--status=open") && a.includes(`--owner=${owner}`))).toBe(true);
    }
    expect(reads.filter((a) => a.includes("--status=closed")).every((a) => a.includes("--owner=all"))).toBe(true);
    expect(reads.every((a) => a.includes(`--limit=${PAGE}`))).toBe(true);
  });

  it("shows the owner's quiet items after his active ones, grouped by weeks, never hidden", async () => {
    serve(pager([
      item(5, "user", { text: "Active item of yours" }),
      item(4, "user", { text: "Quiet three weeks", quiet_weeks: 3 }),
      item(3, "user", { text: "Quiet seven weeks", quiet_weeks: 7 }),
      item(2, "user", { text: "Also quiet three weeks", quiet_weeks: 3 }),
      item(1, "assistant", { text: "The assistant's own" }),
    ]));
    await openWork();
    const needs = await waitFor(() => group(/^Needs you$/));
    const three = group(/^Quiet for 3 weeks$/);
    const seven = group(/^Quiet for 7 weeks$/);
    expect(needs.contains(three) && needs.contains(seven)).toBe(true);
    expect(within(three).getByText("Quiet three weeks")).toBeInTheDocument();
    expect(within(three).getByText("Also quiet three weeks")).toBeInTheDocument();
    expect(within(seven).getByText("Quiet seven weeks")).toBeInTheDocument();
    expect(within(three).queryByText("Active item of yours")).toBeNull();
    const order = Array.from(needs.querySelectorAll(".owed-row")).map((r) => r.textContent ?? "");
    const at = (t: string) => order.findIndex((x) => x.includes(t));
    expect(at("Active item of yours")).toBeGreaterThanOrEqual(0);
    expect(at("Active item of yours")).toBeLessThan(at("Quiet three weeks"));
    expect(at("Quiet three weeks")).toBeLessThan(at("Quiet seven weeks"));
  });

  it("pages through 4,000 items with the cursor, none twice and none missed", async () => {
    serve(pager(flood(4000)));
    await openWork();
    await screen.findByText("Showing 200 of 2,800");
    expect(screen.getByText("Showing 200 of 800")).toBeInTheDocument();
    expect(screen.getByText("Showing 200 of 400")).toBeInTheDocument();
    let shown = document.querySelectorAll(".owed-row").length;
    expect(shown).toBe(3 * PAGE);
    // By text, not by role: computing roles over thousands of rows is what
    // makes a jsdom query slow, not the app.
    const moreButtons = () =>
      Array.from(document.querySelectorAll<HTMLButtonElement>(".owed-more button")).filter(
        (b) => b.textContent === "Show 200 more",
      );
    for (;;) {
      const more = moreButtons();
      if (more.length === 0) break;
      fireEvent.click(more[0]);
      shown += PAGE;
      const want = shown;
      await waitFor(() => expect(document.querySelectorAll(".owed-row").length).toBe(want));
    }
    expect(shown).toBe(4000);
    expect(document.querySelectorAll(".owed-more button")).toHaveLength(0);
    const ids = Array.from(document.querySelectorAll(".owed-row")).map((r) => r.getAttribute("data-id"));
    expect(new Set(ids).size).toBe(4000);
    // Each owner's next page starts after the last item its previous page
    // returned: 13 more pages for the agents, 3 for the assistant, 1 for you.
    const paged = owedReads().filter((a) => a.some((x) => x.startsWith("--cursor=")));
    expect(paged).toHaveLength(17);
  });

  it("Needs you, Waiting on you and the rail badge count only the owner's items", async () => {
    // The owner's 250 are the oldest, behind 3,000 newer items for the
    // orchestrator: no first page holds them, the counts still do.
    const rows = [
      ...Array.from({ length: 250 }, (_, i) => item(i + 1, "user")),
      ...Array.from({ length: 3000 }, (_, i) => item(i + 251, "agent")),
    ];
    const waiting = rows.filter((r) => r.owner === "user").map((r) => ({
      source: "commitment", key: `commitment:${r.id}`, project: "acme/atlas", id: String(r.id),
      title: String(r.text), kind: "followup", since: "2026-10-15T12:00:00Z", blocking: false,
      also_owed: null, actions: ["done", "snooze", "open_owed", "hide"],
    }));
    const doc = {
      format: "khipu-work-view", version: 1, generated_at: new Date().toISOString(), projects: [], live: [], waiting,
      counts: { waiting: waiting.length, blocking: 0, running: 0, flagged: 0, stale_sources: 0, ledger_outbox: 0 },
      problems: {}, ledger_offline: false, refresh_s: 5,
    };
    serve(pager(rows), { khipu_work_view: () => doc });
    await openWork();
    const badge = await screen.findByLabelText(/waiting on you/i, { selector: ".nav-badge" });
    expect(badge).toHaveTextContent(/^250$/);
    await waitFor(() => expect(within(group(/^Needs you$/)).getByText("250")).toBeInTheDocument());
    expect(within(group(/^For agents and jobs$/)).getByText("3,000")).toBeInTheDocument();
  });
});

describe("Owed, many at once", () => {
  const fifty = () => [...Array.from({ length: 50 }, (_, i) => item(100 + i, "user")), item(1, "agent"), item(2, "agent")];

  it("select 50 and Close calls the command once with all 50 ids and the reason", async () => {
    serve(pager(fifty()), { khipu_owed_change: () => ({ ok: true, results: [] }) });
    await openWork();
    const needs = await waitFor(() => group(/^Needs you$/));
    fireEvent.click(within(needs).getByRole("checkbox", { name: "Select every item shown in Needs you" }));
    const bar = await screen.findByRole("toolbar", { name: "Selected items" });
    expect(within(bar).getByText("50 selected")).toBeInTheDocument();
    // Snooze takes one item at a time in the CLI, so it is not offered here.
    expect(within(bar).queryByRole("button", { name: /Snooze/ })).toBeNull();
    fireEvent.click(within(bar).getByRole("button", { name: "Close…" }));
    const dlg = await screen.findByRole("dialog", { name: "Close 50 items" });
    const confirm = within(dlg).getByRole("button", { name: "Close 50" });
    expect(confirm).toBeDisabled();
    fireEvent.change(within(dlg).getByLabelText("Why"), { target: { value: "Done in the release" } });
    fireEvent.click(confirm);
    await waitFor(() => expect(callsOf("khipu_owed_change")).toHaveLength(1));
    const sent = callsOf("khipu_owed_change")[0] as { action: string; ids: string[]; reason: string };
    expect(sent.action).toBe("close");
    expect(sent.reason).toBe("Done in the release");
    expect([...sent.ids].sort()).toEqual(Array.from({ length: 50 }, (_, i) => String(100 + i)).sort());
  });

  it("Move to… gives the chosen items to an agent with its label", async () => {
    serve(pager(fifty()), { khipu_owed_change: () => ({ ok: true, results: [] }) });
    await openWork();
    await waitFor(() => group(/^Needs you$/));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select: Item number 149" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select: Item number 148" }));
    const bar = await screen.findByRole("toolbar", { name: "Selected items" });
    fireEvent.click(within(bar).getByRole("button", { name: "Move to…" }));
    const dlg = await screen.findByRole("dialog", { name: "Move 2 items" });
    fireEvent.change(within(dlg).getByLabelText("Move to"), { target: { value: "agent" } });
    fireEvent.change(within(dlg).getByLabelText("Who"), { target: { value: "orchestrator" } });
    fireEvent.change(within(dlg).getByLabelText("Why"), { target: { value: "A job's own items" } });
    fireEvent.click(within(dlg).getByRole("button", { name: "Move 2" }));
    await waitFor(() => expect(callsOf("khipu_owed_change")).toHaveLength(1));
    expect(callsOf("khipu_owed_change")[0]).toEqual({
      action: "reassign", ids: ["149", "148"], reason: "A job's own items", to: "agent", label: "orchestrator",
    });
  });

  it("offers Close on an item that looks settled, and no Not settled", async () => {
    serve(pager([item(7, "user", { kind: "question", looks_settled: "Looks settled by decision 79627" })]), {
      khipu_owed_change: () => ({ ok: true, results: [] }),
    });
    await openWork();
    const line = await screen.findByText("Looks settled by decision 79627");
    const row = line.closest(".owed-row") as HTMLElement;
    expect(within(row).queryByRole("button", { name: "Not settled" })).toBeNull();
    fireEvent.click(within(row).getByRole("button", { name: "Close as settled" }));
    await waitFor(() => expect(callsOf("khipu_owed_change")).toHaveLength(1));
    expect(callsOf("khipu_owed_change")[0]).toEqual({
      action: "close", ids: ["7"], reason: "Looks settled by decision 79627", to: null, label: null,
    });
  });
});

describe("Owed, looks done", () => {
  const done = (id: number, reason: string) =>
    item(id, "user", { text: `Done-looking ${id}`, looks_done: { rule: "pr", reason, evidence: null }, group: "Looks done" });
  const rows = () => [
    done(9, "pull request #41 merged (from the runner's summary)"),
    done(8, "pull request #40 merged"),
    item(7, "user", { text: "Still active" }),
    item(6, "user", { text: "Quiet one", quiet_weeks: 3 }),
  ];

  it("lists a Looks done group with its count, ahead of the rest, without duplicates", async () => {
    serve(pager(rows()));
    await openWork();
    const looks = await waitFor(() => group(/^Looks done$/));
    expect(within(looks).getByText("2")).toBeInTheDocument();
    expect(within(looks).getByText("Done-looking 9")).toBeInTheDocument();
    expect(within(looks).getByText("Looks done: pull request #41 merged (from the runner's summary)")).toBeInTheDocument();
    const needsAll = group(/^Needs you$/);
    expect(within(needsAll).getAllByText("Done-looking 9")).toHaveLength(1);
    expect(within(needsAll).getAllByText("Done-looking 8")).toHaveLength(1);
    expect(within(looks).queryByText("Still active")).toBeNull();
    const needs = group(/^Needs you$/);
    const order = Array.from(needs.querySelectorAll(".owed-row")).map((r) => r.textContent ?? "");
    const at = (t: string) => order.findIndex((x) => x.includes(t));
    expect(at("Done-looking 9")).toBeLessThan(at("Still active"));
    expect(at("Still active")).toBeLessThan(at("Quiet one"));
  });

  it("Close on one row closes just that id with its reason", async () => {
    serve(pager(rows()), { khipu_owed_change: () => ({ ok: true, results: [] }) });
    await openWork();
    const looks = await waitFor(() => group(/^Looks done$/));
    const row = within(looks).getByText("Done-looking 8").closest(".owed-row") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(callsOf("khipu_owed_change")).toHaveLength(1));
    expect(callsOf("khipu_owed_change")[0]).toEqual({
      action: "close", ids: ["8"], reason: "Looked done: pull request #40 merged", to: null, label: null,
    });
  });

  it("Close all that look done closes every row in the group in one change", async () => {
    serve(pager(rows()), { khipu_owed_change: () => ({ ok: true, results: [] }) });
    await openWork();
    await waitFor(() => group(/^Looks done$/));
    fireEvent.click(screen.getByRole("button", { name: "Close all that look done" }));
    await waitFor(() => expect(callsOf("khipu_owed_change")).toHaveLength(1));
    expect(callsOf("khipu_owed_change")[0]).toEqual({
      action: "close", ids: ["9", "8"], reason: "Looked done: closed by you in the app", to: null, label: null,
    });
  });
});

describe("Tidy this list", () => {
  const plan = {
    format: "khipu-owed-tidy-plan", version: 1, project: "acme/atlas", planned_at: "2026-10-16T12:00:00Z",
    totals: {
      open: 3687, stay_user: 600, keep_other: 104,
      move: { total: 2700, agent: 2650, assistant: 50, user: 0 },
      close: { total: 283, pr: 200, decision: 33, duplicate: 50 },
    },
    model: { used: true, calls: 25, cap: 25, candidates: 1400, unjudged: 400 },
    samples: {
      stay_user: [{ id: 3, kind: "question", text: "Pick the provider", reason: "names you" }],
      move: [{ id: 9, kind: "followup", text: "Rebase the stack onto main", reason: "a job's item" }],
      close_pr: [{ id: 11, kind: "blocker", text: "Wait for PR #366", reason: "pull request #366 merged" }],
    },
    notes: [], items_count: 3687,
  };

  it("plans, shows totals and samples, applies, then undoes the applied batch", async () => {
    serve(pager([item(1, "user"), item(2, "agent", { project: "acme/harbor" })]), {
      khipu_owed_tidy_plan: () => plan,
      khipu_owed_tidy_apply: () => ({
        ok: true, batch: "tidy-20261016T120000Z-a1b2c3", project: "acme/atlas", changed: 2983,
        counts: { close: 283, reassign: 2700 }, skipped: [{ id: 5, why: "changed after the plan" }],
      }),
      khipu_owed_tidy_undo: () => ({ ok: true, batch: "tidy-20261016T120000Z-a1b2c3", restored: 2983, left_alone: [] }),
    });
    await openWork();
    fireEvent.click(await screen.findByRole("button", { name: "Tidy this list…" }));
    const dlg = await screen.findByRole("dialog", { name: "Tidy this list" });
    fireEvent.change(within(dlg).getByLabelText("Project"), { target: { value: "acme/atlas" } });
    fireEvent.click(within(dlg).getByRole("button", { name: "Make a plan" }));
    await within(dlg).findByText("3,687 open items");
    expect(callsOf("khipu_owed_tidy_plan")).toEqual([{ project: "acme/atlas" }]);
    expect(within(dlg).getByText("Stay yours")).toBeInTheDocument();
    expect(within(dlg).getByText("2,700")).toBeInTheDocument();
    expect(within(dlg).getByText("Rebase the stack onto main")).toBeInTheDocument();
    expect(within(dlg).getByText(/400 items were left as they are/)).toBeInTheDocument();
    expect(callsOf("khipu_owed_tidy_apply")).toHaveLength(0);
    fireEvent.click(within(dlg).getByRole("button", { name: "Apply" }));
    await within(dlg).findByText(/Closed 283 and moved 2,700/);
    expect(callsOf("khipu_owed_tidy_apply")).toEqual([{ project: "acme/atlas" }]);
    expect(within(dlg).getByText(/1 item changed after the plan/)).toBeInTheDocument();
    fireEvent.click(within(dlg).getByRole("button", { name: "Undo this clean-up" }));
    await within(dlg).findByText(/Put back 2,983 items/);
    expect(callsOf("khipu_owed_tidy_undo")).toEqual([{ project: "acme/atlas", batch: "tidy-20261016T120000Z-a1b2c3" }]);
  });

  it("Not now leaves the list as it is", async () => {
    serve(pager([item(1, "user")]), { khipu_owed_tidy_plan: () => plan });
    await openWork();
    fireEvent.click(await screen.findByRole("button", { name: "Tidy this list…" }));
    const dlg = await screen.findByRole("dialog", { name: "Tidy this list" });
    fireEvent.click(within(dlg).getByRole("button", { name: "Make a plan" }));
    await within(dlg).findByText("3,687 open items");
    fireEvent.click(within(dlg).getByRole("button", { name: "Not now" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Tidy this list" })).toBeNull());
    expect(callsOf("khipu_owed_tidy_apply")).toHaveLength(0);
  });
});
