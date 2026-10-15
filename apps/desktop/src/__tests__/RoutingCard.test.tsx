// Routing truth phase 4 (docs/plans/2026-10-14-routing-truth.md): the Home
// Routing card for each state in docs/mockups/2026-10-14-routing/
// (home-routing.html, home-routing-states.html, home-routing-conflict.html),
// the health report's Routing row and the routing attention items. Fixtures
// are `khipu route --json` as the CLI prints it, trimmed to the fields the
// app reads. Times are New York local, as in the mocks.
(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { RoutingCard, inPlansOrder } from "../RoutingCard";
// The stylesheet as written (vitest hands CSS imports over empty, `?raw`
// included). The app has no @types/node; vitest runs this file in node.
// @ts-expect-error node:fs has no types here
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
// @ts-expect-error node:child_process has no types here
import { execFileSync } from "node:child_process";
// @ts-expect-error node:os has no types here
import { tmpdir } from "node:os";

const appCss: string = readFileSync(
  `${(globalThis as unknown as { process: { cwd(): string } }).process.cwd()}/src/App.css`,
  "utf8",
);
import { accountSource, accountState, lastChangeWords, useRouting } from "../routing";
import type { RouteAccount, RouteView, RoutingState } from "../routing";
import { routingAttention } from "../doctorAttention";
import { HealthReportRows, routingHealthRow } from "../healthRows";
import type { UsageReport } from "../PlansCard";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  invokeMock.mockReset();
});

// Wed 2026-10-14, 11:40 AM in New York (the main mock's time).
const MORNING = new Date("2026-10-14T15:40:00Z");
// 3:05 PM the same day (the states mock's time).
const AFTERNOON = new Date("2026-10-14T19:05:00Z");

const conv = (project: string) => ({
  source: "report",
  written_by: { kind: "conversation", harness: "claude_code", project },
  by: `a Claude Code conversation in ${project}`,
});

const RULES = [
  "Grok never builds or fixes privacy or safety code.",
  "Every Grok result that is acted on gets a re-check from a different model family.",
  "Never a -fast tier.",
  "Never the paid xAI API.",
  "Khipu runs in Aegis only through Aegis's own pack, never through a Claude or other vendor import.",
  "Never send what is in API Keys or My Portfolio to Codex or Grok.",
  "Khipu's routing record is guidance, not a gate: an orchestrator with better live information may route differently, and says why.",
].map((text, i) => ({ n: i + 1, text, seeded: true, confirmed: i === 6 }));

function acct(label: string, harness: string, extra: Partial<RouteAccount>): RouteAccount {
  return {
    key: `${harness}|${label === "Claude second" ? "/logins/claude-second" : ""}`,
    label,
    harness,
    state: "unknown",
    checkable: harness !== "muse",
    models: [],
    witness: null,
    disagreement: null,
    last_seen: null,
    ...extra,
  };
}

/** home-routing.html: the record at 11:40 AM. */
function mainView(): RouteView {
  return {
    record: true,
    version: 9,
    checked_at: "2026-10-14T15:40:00Z",
    accounts: [
      // The record's own order (limits first); the card follows Plans.
      acct("Claude second", "claude", {
        state: "limited", until: "2026-10-14T18:52:00Z", basis: "cli",
        witness: { source: "runner", written_by: { kind: "runner", name: "Project A" }, at: "2026-10-14T15:13:00Z", message_time: true },
      }),
      acct("Muse", "muse", {
        state: "limited", until: "2026-10-18T00:00:00Z", basis: "estimated",
        witness: { ...conv("project-a"), at: "2026-10-14T14:45:00Z" },
      }),
      acct("Claude main", "claude", {
        state: "limited", until: "2026-10-14T17:52:00Z", basis: "cli",
        witness: { source: "probe", written_by: { kind: "probe" }, at: "2026-10-14T15:36:00Z" },
      }),
      acct("Grok Build", "grok", {
        state: "paused", until: "2026-10-16T13:00:00Z", basis: "owner",
        witness: { source: "owner", written_by: { kind: "owner" }, at: "2026-10-13T22:05:00Z" },
      }),
      acct("Codex", "codex", {
        state: "open",
        witness: { source: "probe", written_by: { kind: "probe" }, at: "2026-10-14T15:36:00Z" },
      }),
    ],
    holds: [
      { id: "h1", scope_label: "this Mac", reason: "machine_busy", text: "No new launches while the Mac is busy (load over 75)",
        until: "2026-10-15T13:40:00Z", owner: false, by: "a Claude Code conversation in project-a", at: "2026-10-14T13:40:00Z" },
      { id: "h2", scope_label: "Muse", reason: "keep", text: "Keep it for second-opinion reviews",
        until: "2026-10-18T00:00:00Z", owner: true, by: "you", at: "2026-10-13T20:00:00Z" },
    ],
    note: { text: "We don't need cursor. Just grok. Secondary account for Claude too until that's canceled.", at: "2026-10-13T14:00:00Z" },
    rules: RULES,
    unmatched: [],
    sources: { runners: [{ name: "Project A", heartbeat_at: "2026-10-14T15:39:40Z", silent: false, unreadable: false }] },
    changes: [{ version: 9, at: "2026-10-14T15:36:00Z", entities: ["account:claude|"] }],
    rule_checks: [],
    warn_after: { disagreement: 1800, not_refreshed: 300 },
  };
}

const PLANS: UsageReport = {
  accounts: [
    { harness: "claude", account: "Claude main", plan: "max", windows: [{ name: "five_hour", used_pct: 100, resets_at: "2026-10-14T17:52:00Z" }] },
    { harness: "claude", account: "Claude second", plan: "max", windows: [] },
    { harness: "codex", account: "Codex", plan: "pro", windows: [{ name: "weekly", used_pct: 58, resets_at: "2026-10-19T00:00:00Z" }] },
    { harness: "grok", account: "Grok Build", windows: [{ name: "weekly", used_pct: 34, resets_at: "2026-10-19T23:00:00Z" }] },
    { harness: "muse", account: "Muse", windows: [] },
  ],
};

function stateOf(data: RouteView | null, extra: Partial<RoutingState> = {}): RoutingState {
  return { data, error: null, loading: false, lastOkAt: data ? new Date().toISOString() : null, load: vi.fn(), ...extra };
}

function rowFor(label: string): HTMLElement {
  const list = screen.getByRole("list", { name: "Accounts" });
  const row = within(list).getAllByRole("listitem").find((r) => r.textContent?.startsWith(label));
  if (!row) throw new Error(`no row for ${label}`);
  return row;
}

describe("Home Routing card, the main mock", () => {
  it("shows each account in the Plans order with its state, who said so and when", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(MORNING);
    render(<RoutingCard state={stateOf(mainView())} plans={PLANS} />);
    const rows = within(screen.getByRole("list", { name: "Accounts" })).getAllByRole("listitem");
    expect(rows.map((r) => r.querySelector(".ellip")?.textContent)).toEqual([
      "Claude main", "Claude second", "Codex", "Grok Build", "Muse",
    ]);
    expect(rowFor("Claude main")).toHaveTextContent("Limit reached, back at 1:52 PM");
    expect(rowFor("Claude main")).toHaveTextContent("Read from Claude 4 min ago");
    expect(rowFor("Claude second")).toHaveTextContent("Limit reached, back at 2:52 PM");
    expect(rowFor("Claude second")).toHaveTextContent(
      "From Claude's own message, seen by your Project A job runner 27 min ago");
    expect(rowFor("Codex")).toHaveTextContent("Open");
    expect(rowFor("Codex")).toHaveTextContent("Read from Codex 4 min ago · 58% of its week used");
    expect(rowFor("Grok Build")).toHaveTextContent("Paused by you until Fri 9:00 AM");
    expect(rowFor("Grok Build")).toHaveTextContent("Set by you yesterday at 6:05 PM");
    // A conversation's report is a guess, and Muse can't be checked.
    expect(rowFor("Muse")).toHaveTextContent("Limit reached, back about Sat 8:00 PM (a guess)");
    expect(rowFor("Muse")).toHaveTextContent(
      "Reported by a Claude Code conversation in project-a 55 min ago · can't be checked without using a turn");
    // No control that does nothing: Check now arrives with phase 5.
    expect(screen.queryByRole("button", { name: /check/i })).toBeNull();
  });

  it("shows the holds, the note word for word with its date, the folded rules and the footer", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(MORNING);
    render(<RoutingCard state={stateOf(mainView())} plans={PLANS} />);
    const holds = screen.getByRole("list", { name: "Holds" });
    const items = within(holds).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("This Mac");
    expect(items[0]).toHaveTextContent("No new launches while the Mac is busy (load over 75)");
    expect(items[0]).toHaveTextContent("Ends tomorrow 9:40 AM · set by a Claude Code conversation in project-a");
    expect(items[1]).toHaveTextContent("Ends Sat 8:00 PM · set by you yesterday at 4:00 PM");
    expect(screen.getByText(/Oct 13, shown word for word after your rules/)).toBeInTheDocument();
    expect(screen.getByText("“We don't need cursor. Just grok. Secondary account for Claude too until that's canceled.”"))
      .toBeInTheDocument();
    const summary = screen.getByText(/7 standing rules, shown first to every conversation/);
    expect(summary.closest("details")).not.toHaveAttribute("open");
    expect(summary).toHaveTextContent("6 not confirmed yet");
    expect(screen.getByText("Khipu reports this; your agents decide where work goes.")).toBeInTheDocument();
  });

  it("keeps the record's order for accounts Plans does not list", () => {
    const v = mainView();
    expect(inPlansOrder(v.accounts!, null).map((a) => a.label)).toEqual(v.accounts!.map((a) => a.label));
  });
});

describe("Home Routing card, the other states", () => {
  const states: [string, Partial<RouteAccount>, string, string][] = [
    ["a model-scoped limit", {
      harness: "claude", state: "open",
      witness: { source: "probe", at: "2026-10-14T18:59:00Z" },
      models: [{ model: "Opus", until: "2026-10-15T13:00:00Z", basis: "cli", source: "probe" }],
    }, "Opus limit reached, back tomorrow 9:00 AM; other models open", "Read from Claude 6 min ago"],
    ["a reset that can't be checked", {
      harness: "muse", state: "probably_open", until: "2026-10-14T18:52:00Z", basis: "cli", checkable: false,
      witness: { source: "runner", written_by: { kind: "runner", name: "Project A" }, at: "2026-10-14T15:13:00Z", message_time: true },
    }, "Its reset time passed at 2:52 PM; this account can't be checked without using a turn",
      "From Muse's own message, seen by your Project A job runner at 11:13 AM"],
    ["probably open", {
      harness: "codex", state: "probably_open", until: "2026-10-14T19:04:00Z", basis: "cli",
      witness: { source: "probe", at: "2026-10-14T14:00:00Z" },
    }, "Probably open: its limit was due to end at 3:04 PM, not checked yet", "Read from Codex at 10:00 AM"],
    ["a guess", {
      harness: "grok", state: "limited", until: "2026-10-14T19:30:00Z", basis: "estimated",
      witness: { ...conv("Khipu"), at: "2026-10-14T19:00:00Z" },
    }, "Limit reached, back about 3:30 PM (a guess)", "Reported by a Claude Code conversation in Khipu 5 min ago"],
    ["down with its reason", {
      harness: "muse", state: "down", reason: "not signed in",
      witness: { source: "probe", at: "2026-10-14T18:55:00Z" },
    }, "Down: not signed in", "Khipu's plan check 10 min ago"],
    ["down, out of service", {
      harness: "codex", state: "down", reason: null,
      witness: { source: "runner", written_by: { kind: "runner", name: "Project A" }, at: "2026-10-14T13:10:00Z" },
    }, "Down: out of service", "Seen by your Project A job runner at 9:10 AM"],
    ["unknown with no report", {}, "Unknown: no report yet", ""],
    ["unknown, last seen open", {
      last_seen: { state: "open", source: "probe", at: "2026-10-14T05:02:00Z" },
    }, "Unknown: last seen open 14 h ago", "Read from Claude at 1:02 AM"],
    ["unknown, its only reporter stopped", {
      silent: true, last_seen: { state: "open", source: "runner", written_by: { kind: "runner", name: "Project B" }, at: "2026-10-14T18:12:00Z" },
    }, "Unknown: its only reporter stopped", "Seen by your Project B job runner 53 min ago"],
    // Fix round 1 (F5): a Clear that went out of date is the owner's
    // Clear, never a sighting of the account open.
    ["unknown, last cleared by you", {
      harness: "codex", last_seen: { state: "open", source: "owner", by: "you", cleared: true, at: "2026-10-14T06:05:00Z" },
    }, "Unknown: last cleared by you 13 h ago", "Cleared by you at 2:05 AM"],
    // Fix round 1 (F6): times read as part of the sentence.
    ["a model-scoped limit ending today", {
      harness: "claude", state: "open",
      witness: { source: "probe", at: "2026-10-14T18:59:00Z" },
      models: [{ model: "Opus", until: "2026-10-14T20:00:00Z", basis: "cli", source: "probe" }],
    }, "Opus limit reached, back at 4:00 PM; other models open", "Read from Claude 6 min ago"],
    ["a reset that passed yesterday and can't be checked", {
      harness: "muse", state: "probably_open", until: "2026-10-14T00:00:00Z", basis: "cli", checkable: false,
      witness: { source: "runner", written_by: { kind: "runner", name: "Project A" }, at: "2026-10-13T15:13:00Z", message_time: true },
    }, "Its reset time passed yesterday at 8:00 PM; this account can't be checked without using a turn",
      "From Muse's own message, seen by your Project A job runner yesterday at 11:13 AM"],
    ["probably open since an earlier day", {
      harness: "codex", state: "probably_open", until: "2026-10-12T00:00:00Z", basis: "cli",
      witness: { source: "probe", at: "2026-10-11T14:00:00Z" },
    }, "Probably open: its limit was due to end Sun at 8:00 PM, not checked yet", "Read from Codex Sun at 10:00 AM"],
    // Fix round 1 (F7): a report that gave no time says so, and how long
    // Khipu keeps it.
    ["a guess from a report without a time", {
      harness: "grok", state: "limited", until: "2026-10-14T19:30:00Z", basis: "estimated",
      witness: { ...conv("Khipu"), at: "2026-10-14T19:00:00Z", until_default: true },
    }, "Limit reached, back about 3:30 PM (a guess)",
      "Reported without a time by a Claude Code conversation in Khipu 5 min ago; Khipu keeps a report like that for 30 minutes"],
    ["paused with no end", {
      state: "paused", until: null, witness: { source: "owner", at: "2026-10-14T18:00:00Z" },
    }, "Paused by you until you clear it", "Set by you at 2:00 PM"],
  ];

  it.each(states)("%s", (_name, extra, stateText, sourceText) => {
    const a = acct("Account", (extra.harness as string) ?? "claude", extra);
    expect(accountState(a, AFTERNOON).text).toBe(stateText);
    expect(accountSource(a, AFTERNOON)).toBe(sourceText);
  });

  it("says when Khipu's record has not been refreshed, and keeps the last view", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(AFTERNOON);
    const v = { ...mainView(), checked_at: "2026-10-14T18:58:00Z" };
    render(<RoutingCard state={stateOf(v, { lastOkAt: "2026-10-14T18:58:00Z", error: "Khipu could not read the routing record. It tries again every minute." })} />);
    expect(screen.getByText("Khipu's record has not been refreshed since 2:58 PM")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Accounts" })).toBeInTheDocument();
  });

  it("shows no routing record on this Mac instead of any account as open", () => {
    render(<RoutingCard state={stateOf({ record: false, accounts: [] })} />);
    expect(screen.getByText("No routing record on this Mac")).toBeInTheDocument();
    expect(screen.queryByText("Open")).toBeNull();
  });

  it("says why when the record could not be read at all", () => {
    render(<RoutingCard state={stateOf(null, { error: "Khipu could not read the routing record. It tries again every minute." })} />);
    expect(screen.getByText("Could not read the routing record")).toBeInTheDocument();
  });

  it("names a job runner that stopped reporting, on Home and in the health report", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(AFTERNOON);
    const v: RouteView = {
      ...mainView(),
      sources: { runners: [{ name: "Project B", heartbeat_at: "2026-10-14T18:12:00Z", silent: true }] },
      accounts: [acct("Grok Build", "grok", {
        state: "limited", until: "2026-10-14T19:30:00Z", basis: "estimated", witness: { ...conv("Khipu"), at: "2026-10-14T19:00:00Z" },
      })],
      changes: [{ version: 3, at: "2026-10-14T19:00:00Z", entities: ["account:grok|"] }],
    };
    const items = routingAttention(v, AFTERNOON);
    expect(items).toEqual([{
      key: "routing:runner:Project B",
      tone: "warn",
      title: "A job runner stopped reporting",
      cause: "Your Project B job runner's last report was at 2:12 PM. Limits it finds won't reach your conversations until it runs again. Khipu stopped nothing.",
    }]);
    const row = routingHealthRow(v, AFTERNOON);
    expect(row).toEqual({
      key: "routing",
      status: "err",
      label: "Routing",
      detail: "Your Project B job runner stopped reporting at 2:12 PM. Last change 3:00 PM: Grok Build limit reached (a guess). No standing rule looks broken.",
    });
    render(<HealthReportRows parsed={{}} attention={items} routing={v} />);
    expect(screen.getByText("Routing")).toBeInTheDocument();
    expect(screen.getByText(/Your Project B job runner stopped reporting at 2:12 PM\. Last change 3:00 PM/)).toBeInTheDocument();
  });
});

describe("Home Routing card, two sources disagree and a rule may be broken", () => {
  function conflictView(): RouteView {
    const v = mainView();
    v.accounts = [
      acct("Claude second", "claude", {
        state: "limited", until: "2026-10-14T18:52:00Z", basis: "cli",
        witness: { source: "runner", written_by: { kind: "runner", name: "Project A" }, at: "2026-10-14T15:13:00Z", message_time: true },
        disagreement: { state: "open", ...conv("project-b"), at: "2026-10-14T15:31:00Z", since: "2026-10-14T15:31:00Z" },
      }),
    ];
    v.rule_checks = [{
      rule: 3, status: "broken", at: "2026-10-14T15:22:00Z", runner: "Project A", file: "settings",
      text: "A standing rule may be broken: Project A's settings name a model ending in -fast for its grokcheck lane.",
    }, { rule: 4, status: "cannot_check", text: "Project A's grok lane: whether it uses the paid xAI API can't be checked: the launch environment is set inside grok" }];
    return v;
  }

  it("shows the stronger report, names both and marks the rule", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(MORNING);
    render(<RoutingCard state={stateOf(conflictView())} />);
    const row = rowFor("Claude second");
    expect(row).toHaveTextContent("Limit reached, back at 2:52 PM");
    expect(within(row).getByText("Sources disagree")).toBeInTheDocument();
    expect(row).toHaveTextContent(
      "A Claude Code conversation in project-b reported it open 9 min ago. Claude's own message outranks a conversation's report, so the limit shows here; conversations are told both.");
    const summary = screen.getByText(/7 standing rules/);
    expect(summary).toHaveTextContent("1 may be broken");
    const broken = screen.getByText(/Never a -fast tier\./).closest("li");
    expect(broken).toHaveClass("route-rule-broken");
    expect(broken).toHaveTextContent("may be broken, 11:22 AM");
  });

  it("raises a broken rule as an attention item that stops nothing, and the health row red", () => {
    const items = routingAttention(conflictView(), MORNING);
    expect(items).toEqual([{
      key: "routing:rules",
      tone: "warn",
      title: "A standing rule may be broken",
      cause: "Your rule 3 says: Never a -fast tier. Your Project A job runner's settings name a model ending in -fast for its grokcheck lane (seen at 11:22 AM in its settings file). Khipu stopped nothing; it only reports.",
    }]);
    const row = routingHealthRow(conflictView(), MORNING);
    expect(row?.status).toBe("err");
    expect(row?.detail).toContain("A standing rule may be broken: Project A's settings name a model ending in -fast for its grokcheck lane.");
    expect(row?.detail).not.toContain("No standing rule looks broken");
  });

  it("warns once sources have disagreed for longer than the warn time", () => {
    const later = new Date("2026-10-14T16:05:00Z");
    const items = routingAttention(conflictView(), later).filter((a) => a.key.startsWith("routing:disagree"));
    expect(items).toEqual([{
      key: "routing:disagree:Claude second",
      tone: "warn",
      title: "Sources disagree about Claude second",
      cause: "Sources have disagreed about Claude second for more than 30 minutes. The Routing card shows the stronger report and names both; conversations are told both.",
    }]);
    expect(routingAttention({ ...conflictView(), warn_after: { disagreement: 0 } }, later)
      .some((a) => a.key.startsWith("routing:disagree"))).toBe(false);
  });
});

describe("routing attention and health row edges", () => {
  it("names a limit from a lane not matched to an account, never a guessed account", () => {
    const v: RouteView = { ...mainView(), unmatched: [{ runner: "Project A", lane: "opus-batch", until: "2026-10-14T18:00:00Z" }] };
    const items = routingAttention(v, MORNING);
    expect(items).toEqual([{
      key: "routing:lane:Project A/opus-batch",
      tone: "warn",
      title: "A limit came from a lane not matched to an account",
      cause: "A limit came from Project A's opus-batch lane, which isn't matched to an account; it ends at 2:00 PM. Conversations are told the lane's name only, never a guessed account.",
    }]);
  });

  it("raises nothing without a record, and the health row is grey", () => {
    expect(routingAttention({ record: false, accounts: [] }, MORNING)).toEqual([]);
    expect(routingAttention(null, MORNING)).toEqual([]);
    expect(routingHealthRow({ record: false, accounts: [] }, MORNING)).toEqual({ key: "routing", status: "skipped", label: "Routing" });
    expect(routingHealthRow(null, MORNING)).toBeNull();
  });

  it("is green with the last change when nothing is broken", () => {
    const row = routingHealthRow(mainView(), MORNING);
    expect(row).toEqual({
      key: "routing", status: "ok", label: "Routing",
      detail: "Last change 11:36 AM: Claude main limit reached. No standing rule looks broken.",
    });
    expect(lastChangeWords({ ...mainView(), changes: [{ version: 2, at: "2026-10-14T15:00:00Z", entities: ["note", "rule:1"] }] }, MORNING))
      .toBe("Last change 11:00 AM: Your note changed and 1 more.");
  });
});

describe("useRouting", () => {
  it("reads the record through the fixed-argv command and keeps the last view on a failure", async () => {
    invokeMock.mockResolvedValueOnce(JSON.stringify(mainView()));
    const { result } = renderHook(() => useRouting(true));
    await waitFor(() => expect(result.current.data?.version).toBe(9));
    expect(invokeMock).toHaveBeenCalledWith("khipu_route_view", undefined);
    invokeMock.mockRejectedValueOnce("boom");
    await act(async () => {
      await result.current.load();
    });
    expect(result.current.data?.version).toBe(9);
    expect(result.current.error).toBe("Khipu could not read the routing record. It tries again every minute.");
  });

  it("does not read while inactive", () => {
    renderHook(() => useRouting(false));
    expect(invokeMock).not.toHaveBeenCalled();
  });
});

describe("Home Routing card, fix round 1", () => {
  it("names a model's own disagreement on the row the attention item points at (F1)", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(AFTERNOON);
    const v = mainView();
    v.accounts = [acct("Claude main", "claude", {
      state: "open",
      witness: { source: "probe", at: "2026-10-14T18:59:00Z" },
      models: [{
        model: "Opus", until: "2026-10-16T13:00:00Z", basis: "cli", source: "report",
        disagreement: { state: "open", ...conv("project-c"), at: "2026-10-14T17:00:00Z", since: "2026-10-14T17:00:00Z" },
      }],
    })];
    render(<RoutingCard state={stateOf(v)} />);
    const row = rowFor("Claude main");
    expect(row).toHaveTextContent("Opus limit reached, back Fri 9:00 AM; other models open");
    expect(within(row).getByText("Sources disagree")).toBeInTheDocument();
    expect(row).toHaveTextContent(
      "A Claude Code conversation in project-c reported Opus open at 1:00 PM. Claude's own message outranks a conversation's report, so the Opus limit shows here; conversations are told both.");
    // The attention item names the same model, and the card has its note.
    const items = routingAttention(v, AFTERNOON).filter((a) => a.key.startsWith("routing:disagree"));
    expect(items.map((a) => a.title)).toEqual(["Sources disagree about Claude main (Opus)"]);
  });

  it("reminds the owner of his own old hold, and only that one (F2)", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(MORNING);
    const v = mainView();
    v.holds = [
      { id: "old", scope_label: "Muse", reason: "keep", text: "Keep it for reviews", until: null, owner: true, by: "you",
        at: "2026-07-01T15:00:00Z", remind: true },
      { id: "new", scope_label: "Codex", reason: "keep", text: "Keep it for builds", until: null, owner: true, by: "you",
        at: "2026-10-13T15:00:00Z", remind: false },
    ];
    render(<RoutingCard state={stateOf(v)} />);
    const items = within(screen.getByRole("list", { name: "Holds" })).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("You set this 105 days ago. Is it still needed?");
    expect(within(items[0]).getByText("Reminder")).toBeInTheDocument();
    expect(items[1]).not.toHaveTextContent("Is it still needed?");
    expect(within(items[1]).queryByText("Reminder")).toBeNull();
  });
});

// Fix round 2: the card laid out by a real browser engine. jsdom has no
// layout, so a computed style says nothing about whether text fits; this
// renders the real card with the real App.css in headless Chrome at the
// app's default window width (1100) and at the mock's narrow width (700),
// and finds every plain word that was broken across two lines. Paths and
// hyphenated names may break; a word like "yesterday" or "Mac" never.
const CHROME = [
  (globalThis as unknown as { process: { env: Record<string, string | undefined> } }).process.env.KHIPU_TEST_CHROME,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
].find((p): p is string => !!p && existsSync(p));

const MEASURE = `(function(){
  var card = document.querySelector(".routing-card"), broken = [];
  var walk = document.createTreeWalker(card, NodeFilter.SHOW_TEXT), n;
  while ((n = walk.nextNode())) {
    var re = /\\S+/g, m;
    while ((m = re.exec(n.textContent))) {
      if (!/^[A-Za-z,.'\\u2019]+$/.test(m[0])) continue;
      var r = document.createRange(); r.setStart(n, m.index); r.setEnd(n, m.index + m[0].length);
      var tops = {};
      Array.prototype.forEach.call(r.getClientRects(), function (x) { if (x.width > 0) tops[Math.round(x.top)] = 1; });
      if (Object.keys(tops).length > 1) broken.push(m[0]);
    }
  }
  var srcs = Array.prototype.map.call(card.querySelectorAll(".route-row .route-src"), function (e) { return Math.round(e.getBoundingClientRect().width); });
  var de = document.documentElement;
  document.body.setAttribute("data-out", JSON.stringify({ broken: broken, srcWidths: srcs, sideScroll: de.scrollWidth > de.clientWidth }));
})();`;

function layOut(cardHtml: string, width: number): { broken: string[]; srcWidths: number[]; sideScroll: boolean } {
  const dir = mkdtempSync(`${tmpdir()}/khipu-routing-layout-`);
  try {
    const page = `${dir}/card.html`;
    writeFileSync(page, `<!doctype html><html lang="en"><head><meta charset="utf-8"><style>${appCss}</style></head>` +
      `<body><div id="root"><div class="shell"><nav class="rail"></nav><main class="main"><section class="panel">` +
      `<div class="panel-body wide">${cardHtml}</div></section></main></div></div><script>${MEASURE}</script></body></html>`);
    const dom: string = execFileSync(CHROME, [
      "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", `--user-data-dir=${dir}/profile`,
      `--window-size=${width},1600`, "--virtual-time-budget=2000", "--dump-dom", `file://${page}`,
    ], { encoding: "utf8", timeout: 60000, stdio: ["ignore", "pipe", "ignore"] });
    const m = dom.match(/data-out="([^"]*)"/);
    if (!m) throw new Error("the page did not report its layout");
    return JSON.parse(m[1].replace(/&quot;/g, '"').replace(/&amp;/g, "&"));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

describe.skipIf(!CHROME)("Home Routing card, fix round 2 (laid out in headless Chrome)", () => {
  function longView(): RouteView {
    const v = mainView();
    v.accounts = [
      // A model-scoped limit for two models next to a short source (R2-1).
      acct("Claude main", "claude", {
        state: "open",
        witness: { source: "probe", written_by: { kind: "probe" }, at: "2026-10-14T18:59:00Z" },
        models: [
          { model: "Opus", until: "2026-10-15T13:00:00Z", basis: "cli", source: "probe" },
          { model: "Sonnet", until: "2026-10-15T13:00:00Z", basis: "cli", source: "probe" },
        ],
      }),
      // A long state next to a long source (R2-1).
      acct("Muse", "muse", {
        state: "probably_open", until: "2026-10-14T00:00:00Z", basis: "cli", checkable: false,
        witness: { source: "runner", written_by: { kind: "runner", name: "Project A" }, at: "2026-10-13T15:13:00Z", message_time: true },
      }),
    ];
    return v;
  }

  function cardHtml(): string {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(AFTERNOON);
    const { container } = render(<RoutingCard state={stateOf(longView())} />);
    expect(container.textContent).toContain("Its reset time passed yesterday at 8:00 PM; this account can't be checked without using a turn");
    expect(container.textContent).toContain("This Mac");
    return container.innerHTML;
  }

  it("keeps who said so readable beside a long state at the default window width (R2-1)", () => {
    const out = layOut(cardHtml(), 1100);
    expect(out.broken).toEqual([]);
    expect(out.sideScroll).toBe(false);
    for (const w of out.srcWidths) expect(w).toBeGreaterThanOrEqual(150);
  }, 90000);

  it("keeps a hold's scope on one line at the narrow width (R2-2)", () => {
    const out = layOut(cardHtml(), 700);
    expect(out.broken).toEqual([]);
    expect(out.sideScroll).toBe(false);
  }, 90000);

  it("still breaks a long path in the note, the rules and the holds instead of running past the card", () => {
    const long = "/a/very/long/path/with/no/spaces/".repeat(10);
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(AFTERNOON);
    const v = longView();
    v.note = { text: `See ${long}`, at: "2026-10-13T14:00:00Z" };
    v.holds = [{ ...v.holds![0], text: long }];
    v.rules = [{ n: 1, text: long }];
    const { container } = render(<RoutingCard state={stateOf(v)} />);
    for (const width of [1100, 700]) expect(layOut(container.innerHTML, width).sideScroll).toBe(false);
  }, 90000);
});

describe("Home Routing card, fix round 2", () => {
  it("raises the broken rule in the words the approved conflict mock shows (R2-3)", () => {
    const mock: string = readFileSync(
      `${(globalThis as unknown as { process: { cwd(): string } }).process.cwd()}/../../docs/mockups/2026-10-14-routing/home-routing-conflict.html`,
      "utf8",
    );
    const m = mock.match(/<b>A standing rule may be broken<\/b><span>([^<]*)<\/span>/);
    expect(m).not.toBeNull();
    const v = mainView();
    v.accounts = [];
    v.rule_checks = [{
      rule: 3, status: "broken", at: "2026-10-14T15:22:00Z", runner: "Project A", file: "settings",
      text: "A standing rule may be broken: Project A's settings name a model ending in -fast for its grokcheck lane.",
    }];
    const [item] = routingAttention(v, MORNING);
    expect(item.cause).toBe(m![1].replace(/&#x27;|&#39;/g, "'"));
  });
});
