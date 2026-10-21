// Settings clarity phase 4 (docs/plans/2026-10-15-settings-clarity.md,
// "Phase 4"; mock docs/mockups/2026-10-15-settings-clarity/routing-open.html):
// sorting, filtering and searching the lists on Settings -> Accounts and
// routing. They change only what shows; the sort and filters are remembered
// on this Mac. Times are New York local.
(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { RoutingSettings } from "../RoutingSettings";
import { GROUPS } from "../routingOverview";
import { ACCOUNT_SORTS, LISTS_KEY, SEARCH_OVER, matchesQuery, readListPrefs, sortAccounts } from "../routingLists";
import type { PlanUsageState, UsageAccount } from "../PlansCard";
import type { RouteAccount, RouteView } from "../routing";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

// Wed 2026-10-14, 11:40 AM in New York.
const MORNING = new Date("2026-10-14T15:40:00Z");
const OPEN_KEY = "khipu.routing.open";
const ALL_OPEN = JSON.stringify(GROUPS.map((g) => g.key));

function acct(label: string, harness: string, extra: Partial<RouteAccount>): RouteAccount {
  return {
    key: `${harness}|${label === "Claude second" ? "/logins/claude-second" : ""}`,
    label, harness, state: "unknown", checkable: harness !== "muse", models: [], witness: null, disagreement: null, last_seen: null,
    ...extra,
  };
}

function view(): RouteView {
  return {
    record: true,
    version: 12,
    checked_at: "2026-10-14T15:40:00Z",
    accounts: [
      acct("Claude main", "claude", {
        state: "limited", until: "2026-10-14T17:52:00Z", basis: "cli",
        witness: { source: "probe", written_by: { kind: "probe" }, at: "2026-10-14T15:36:00Z" },
      }),
      acct("Claude second", "claude", {
        state: "limited", until: "2026-10-14T18:52:00Z", basis: "cli",
        witness: { id: "r-runner", source: "runner", written_by: { kind: "runner", name: "Project A" }, at: "2026-10-14T15:13:00Z", message_time: true },
        disagreement: { id: "e-open", state: "open", source: "report", by: "a Claude Code conversation in project-b", at: "2026-10-14T15:31:00Z" },
      }),
      acct("Codex", "codex", {
        state: "open", witness: { source: "probe", written_by: { kind: "probe" }, at: "2026-10-14T15:36:00Z" },
      }),
      acct("Grok Build", "grok", {
        state: "paused", until: "2026-10-16T13:00:00Z", basis: "owner",
        witness: { source: "owner", written_by: { kind: "owner" }, at: "2026-10-13T22:05:00Z" },
      }),
    ],
    holds: [],
    note: null,
    rules: [
      { n: 3, text: "Never a -fast tier.", seeded: true, confirmed: false, source: "from your routing table", date: "2026-10-13" },
      { n: 7, text: "Khipu's routing record is guidance, not a gate.", seeded: true, confirmed: true, source: "from your routing table", date: "2026-10-13" },
    ],
    watched_rules: [3],
    unmatched: [],
    priority: {
      entries: [
        { account: "grok|", label: "Grok Build", in_settings: true },
        { account: "codex|", label: "Codex", in_settings: true },
        { account: "claude|", label: "Claude main", in_settings: true },
      ],
      pending: false, confirmed: true, replaced: { entries: [] },
    },
    sources: {
      runners: [
        { name: "Project A", path: "/runners/a", heartbeat_at: "2026-10-14T15:39:40Z", interval: 20, silent: false, unreadable: false,
          lanes: ["codex", "main", "secondary"], unmatched_lanes: [], files: {} },
      ],
      table: { stamped_at: "2026-10-14T13:20:00Z", changed_at: "2026-10-14T13:20:00Z" },
    },
    old_switches: { env: false, file: false },
    changes: [
      { version: 11, at: "2026-10-13T22:05:00Z", entities: ["account:grok|", "rule:9"], items: [
        { entity: "account:grok|", now: { basis: "owner", label: "Grok Build", state: "paused", until: "2026-10-16T13:00:00Z" },
          source: "owner", via: "app", was: { label: "Grok Build", state: "unknown", until: null }, by: "you" },
        { entity: "rule:9", n: 9, now: { text: "Muse is for reviews only." }, source: "report", via: "mcp", was: null,
          by: "a Claude Code conversation in project-a" },
      ] },
      { version: 12, at: "2026-10-14T15:36:00Z", entities: ["account:claude|", "rule:6"], items: [
        { entity: "account:claude|", now: { label: "Claude main", state: "limited", until: "2026-10-14T17:52:00Z", basis: "cli" },
          was: { label: "Claude main", state: "open" }, source: "probe", by: "Khipu usage probe" },
        { entity: "rule:6", n: 6, now: null, source: null, via: "app", was: { seeded: false, text: "Never send keys to Codex." }, by: "you" },
      ] },
    ],
    rule_checks: [],
    warn_after: { disagreement: 1800, not_refreshed: 300 },
    report_default_limit: 1800,
  } as RouteView;
}

function setting(value: unknown, def: unknown = value) {
  return { value, default: def, kind: "x", source: "default" };
}

function config(lanes: Record<string, string> = { codex: "Codex", main: "Claude main", secondary: "Claude second" }) {
  return {
    routing: {
      routing_stale_after: setting(43200),
      routing_conversation_hold: setting(86400),
      routing_conversation_hold_max: setting(604800),
      routing_owner_hold_reminder: setting(7776000),
      routing_disagreement_warn_after: setting(1800),
      routing_summary_chars: setting(1500),
      routing_prompt_chars: setting(400),
      routing_hold_chars: setting(300),
      routing_line_chars: setting(200),
      routing_prompt_budget_ms: setting(50),
      routing_resend_after_versions: setting(20),
      routing_resend_after: setting(21600),
      routing_not_refreshed_after: setting(300),
      routing_push_start: setting(true),
      routing_push_prompt: setting(true),
      routing_push_delegation: setting(true),
      routing_dispatch_scripts: setting([], []),
      routing_reset_check: setting(true),
      routing_reset_check_wait: setting(120),
      routing_reset_check_retries: setting([600, 1800]),
      routing_busy_load: setting(75),
      routing_busy_wait: setting(300),
      routing_busy_tries: setting(3),
      routing_sources: setting([{ name: "Project A", kind: "lanes", path: "/runners/a" }], []),
      routing_lane_map: setting({ "Project A": lanes }, {}),
      routing_table: setting("/notes/routing/ROUTING-NOW.md", null),
    },
  };
}

function planRow(extra: Partial<UsageAccount>): UsageAccount {
  return { harness: "claude", account: "Claude main", home: null, ok: true, error: null,
    windows: [{ name: "weekly", used_pct: 20, resets_at: "2026-10-17T19:00:00Z" }],
    probed_at: "2026-10-14T15:38:00Z", last_attempt_at: "2026-10-14T15:38:00Z", ...extra };
}

function plansOf(accounts: UsageAccount[]): PlanUsageState {
  return {
    data: { accounts }, error: null, errorAt: null, loading: false, refreshing: false,
    load: vi.fn(async () => undefined), refresh: vi.fn(async () => undefined), reread: vi.fn(async () => undefined),
  };
}

type Answer = string | Record<string, unknown> | ((args: Record<string, unknown> | undefined) => unknown);
let answers: Record<string, Answer>;
let store: Map<string, string>;

beforeEach(() => {
  store = new Map();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  });
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(MORNING);
  answers = {
    khipu_route_view: view(),
    khipu_config_show: config(),
    khipu_features_show: { features: { routing_push: { enabled: true, source: "file" } } },
  };
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    const a = answers[cmd] ?? { ok: true };
    const out = typeof a === "function" ? await a(args) : a;
    return typeof out === "string" ? out : JSON.stringify(out);
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  invokeMock.mockReset();
});

async function page() {
  const out = render(<RoutingSettings active plansState={plansOf([planRow({ key: "claude|" })])} />);
  await screen.findByRole("combobox", { name: "Sort by" });
  return out;
}

/** The accounts on the page, in order. */
const names = () =>
  [...document.querySelectorAll<HTMLElement>('button[data-route-item^="account:"]')].map((b) => b.textContent ?? "");
const order = () => ["Grok Build", "Codex", "Claude main", "Claude second"].map((n) => ({ n, i: names().findIndex((t) => t.includes(n)) }));
const written = () =>
  invokeMock.mock.calls.filter(([cmd]) => cmd === "khipu_route_priority" || cmd === "khipu_config_set" || cmd === "khipu_route_rule");
const chip = (name: RegExp | string) => screen.getByRole("button", { name });

describe("Accounts: sort and filter (unit)", () => {
  const v = view();
  v.plan_facts = {
    "claude|": { readable: true, left_pct: 10, pace_pct_per_hour: 5 },
    "codex|": { readable: true, left_pct: 80, pace_pct_per_hour: 1 },
    "grok|": { readable: false },
  };
  const base = v.accounts!;
  const keys = (list: RouteAccount[]) => list.map((a) => a.label);
  const now = MORNING;

  it("orders by priority, back soonest, most left, spend pace and name, and never changes the input", () => {
    const before = keys(base);
    expect(keys(sortAccounts(base, "priority", v, now)).slice(0, 3)).toEqual(["Grok Build", "Codex", "Claude main"]);
    // Open accounts are back now; then the limit that ends first; paused ends last.
    expect(keys(sortAccounts(base, "soonest", v, now))).toEqual(["Codex", "Claude main", "Claude second", "Grok Build"]);
    expect(keys(sortAccounts(base, "left", v, now)).slice(0, 2)).toEqual(["Codex", "Claude main"]);
    expect(keys(sortAccounts(base, "pace", v, now)).slice(0, 2)).toEqual(["Claude main", "Codex"]);
    expect(keys(sortAccounts(base, "name", v, now))).toEqual(["Claude main", "Claude second", "Codex", "Grok Build"]);
    expect(keys(base)).toEqual(before);
    expect(ACCOUNT_SORTS[0]!.key).toBe("priority");
  });

  it("matches every word of a search, ignoring case", () => {
    expect(matchesQuery("grok never", "Never the paid xAI API, Grok work")).toBe(true);
    expect(matchesQuery("grok zebra", "Never the paid xAI API, Grok work")).toBe(false);
    expect(matchesQuery("  ", "anything")).toBe(true);
  });
});

describe("Accounts: Sort by and Show accounts", () => {
  it("follows the priority list by default and writes nothing when the sort changes", async () => {
    await page();
    const entriesBefore = JSON.stringify(view().priority!.entries);
    const o = order();
    expect(o[0]!.i).toBeLessThan(o[1]!.i);
    expect(o[1]!.i).toBeLessThan(o[2]!.i);
    expect(screen.getByRole("combobox", { name: "Sort by" })).toHaveValue("priority");
    fireEvent.change(screen.getByRole("combobox", { name: "Sort by" }), { target: { value: "name" } });
    expect(names().map((t) => t.replace(/\s.*/, ""))[0]).toBe("Claude");
    expect(names()[0]).toContain("Claude main");
    expect(names()[3]).toContain("Grok Build");
    fireEvent.change(screen.getByRole("combobox", { name: "Sort by" }), { target: { value: "priority" } });
    expect(written()).toEqual([]);
    expect(JSON.stringify(view().priority!.entries)).toBe(entriesBefore);
  });

  it("counts every account under each chip, and a filter that hides a problem still counts it", async () => {
    await page();
    expect(chip("All, 4 accounts")).toHaveAttribute("aria-pressed", "true");
    expect(chip("Open, 1 account")).toBeInTheDocument();
    expect(chip("At the limit, 2 accounts")).toBeInTheDocument();
    expect(chip("Paused, 1 account")).toBeInTheDocument();
    const needs = screen.getByRole("button", { name: /^Needs a look, \d+ accounts?$/ }).getAttribute("aria-label")!;
    expect(needs).toMatch(/^Needs a look, [2-9] accounts$/);
    fireEvent.click(chip("Open, 1 account"));
    expect(chip("Open, 1 account")).toHaveAttribute("aria-pressed", "true");
    expect(names()).toHaveLength(1);
    expect(names()[0]).toContain("Codex");
    // Counts did not move, and the hidden problems are named.
    expect(screen.getByRole("button", { name: needs })).toBeInTheDocument();
    expect(chip("All, 4 accounts")).toBeInTheDocument();
    expect(screen.getByText(/accounts that need a look are hidden by this filter/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show all accounts" }));
    expect(names()).toHaveLength(4);
    expect(screen.queryByText(/hidden by this filter/)).toBeNull();
  });

  it("keeps every account chip and its rows aligned with the Accounts classification", async () => {
    const record = view();
    record.accounts = [
      acct("Open one", "open-one", { state: "open" }),
      acct("Open two", "open-two", { state: "open" }),
      acct("Open three", "open-three", { state: "open" }),
      // Its limit is past, but Khipu cannot spend a turn to verify it.
      acct("Muse", "muse", { state: "probably_open", checkable: false, until: "2026-10-14T15:00:00Z" }),
      acct("Probably checked", "probably", { state: "probably_open", until: "2026-10-14T15:00:00Z" }),
      acct("Limited", "limited", { state: "limited", until: "2026-10-14T17:00:00Z" }),
      acct("Unknown", "unknown", { state: "unknown" }),
      acct("Paused", "paused", { state: "paused", until: "2026-10-16T13:00:00Z" }),
      acct("Down", "down", { state: "down", reason: "an outage" }),
    ];
    record.holds = [{ id: "active-hold", scope_label: "this Mac", reason: "machine_busy", until: "2026-10-14T17:00:00Z" }];
    answers.khipu_route_view = record;
    render(<RoutingSettings active plansState={plansOf([
      planRow({ key: "unknown|", harness: "grok", account: "Unknown", ok: false, error: "not_signed_in", windows: [] }),
    ])} />);
    await screen.findByRole("combobox", { name: "Sort by" });

    expect(document.querySelector('[data-settings-group="accounts"]')).toHaveTextContent(
      "9 accounts: 3 open, 2 probably open, 1 at its limit, 1 paused, 1 down, 1 unknown",
    );
    const rows = () => [...document.querySelectorAll<HTMLElement>('button[data-route-item^="account:"]')].map((b) => b.textContent ?? "");
    const expectRows = (label: string, expected: string[]) => {
      fireEvent.click(chip(label));
      expect(rows()).toHaveLength(expected.length);
      for (const name of expected) expect(rows().join(" ")).toContain(name);
    };

    expectRows("All, 9 accounts", ["Open one", "Open two", "Open three", "Muse", "Probably checked", "Limited", "Unknown", "Paused", "Down"]);
    expect(rows().join(" ")).toContain("this account can't be checked without using a turn");
    expect(rows().join(" ")).toContain("Limit reached");
    expect(rows().join(" ")).toContain("Unknown: no report yet");
    expect(rows().join(" ")).toContain("Paused by you until");
    expect(rows().join(" ")).toContain("Down: an outage");
    expect(document.querySelector('[data-settings-group="holds"]')).toHaveTextContent("1 hold");
    expectRows("Needs a look, 3 accounts", ["Limited", "Unknown", "Down"]);
    expectRows("Open, 3 accounts", ["Open one", "Open two", "Open three"]);
    expectRows("At the limit, 1 account", ["Limited"]);
    expectRows("Paused, 1 account", ["Paused"]);
  });

  it("says so when a filter leaves no accounts, with a way back", async () => {
    answers.khipu_route_view = { ...view(), accounts: view().accounts!.filter((a) => a.state !== "paused") };
    await page();
    fireEvent.click(chip("Paused, 0 accounts"));
    expect(screen.getByText(/No accounts here\./)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show all 3 accounts" }));
    expect(names()).toHaveLength(3);
    // The button that was clicked is gone: focus lands on the All chip, not the page.
    await waitFor(() => expect(document.activeElement).toBe(chip("All, 3 accounts")));
  });

  it("remembers the sort and the filter on this Mac and restores them", async () => {
    const first = await page();
    fireEvent.change(screen.getByRole("combobox", { name: "Sort by" }), { target: { value: "soonest" } });
    fireEvent.click(chip("At the limit, 2 accounts"));
    expect(JSON.parse(store.get(LISTS_KEY)!)).toMatchObject({ accountSort: "soonest", accountFilter: "limit" });
    first.unmount();
    await page();
    expect(screen.getByRole("combobox", { name: "Sort by" })).toHaveValue("soonest");
    expect(chip("At the limit, 2 accounts")).toHaveAttribute("aria-pressed", "true");
    expect(names()).toHaveLength(2);
  });

  it("ignores remembered values it does not know", () => {
    store.set(LISTS_KEY, JSON.stringify({ accountSort: "nope", accountFilter: "open", changeBy: 5 }));
    expect(readListPrefs()).toMatchObject({ accountSort: "priority", accountFilter: "open", changeBy: "all" });
  });

  it("still works when storage throws", async () => {
    vi.stubGlobal("localStorage", {
      getItem: () => { throw new Error("blocked"); },
      setItem: () => { throw new Error("blocked"); },
      removeItem: () => { throw new Error("blocked"); },
    });
    await page();
    fireEvent.change(screen.getByRole("combobox", { name: "Sort by" }), { target: { value: "name" } });
    expect(screen.getByRole("combobox", { name: "Sort by" })).toHaveValue("name");
    fireEvent.click(chip("Open, 1 account"));
    expect(names()).toHaveLength(1);
  });

  it("Show from Needs you reaches an account a remembered filter hides", async () => {
    store.set(LISTS_KEY, JSON.stringify({ accountFilter: "open" }));
    await page();
    expect(names()).toHaveLength(1);
    const needs = await screen.findByRole("region", { name: "Needs you" });
    fireEvent.click(within(needs).getAllByRole("button", { name: /^Show / })[0]!);
    await waitFor(() => expect(names()).toHaveLength(4));
    expect(JSON.parse(store.get(LISTS_KEY)!).accountFilter).toBe("all");
    await waitFor(() => expect(document.activeElement?.getAttribute("data-route-item") ?? "").toMatch(/^account:/));
  });
});

describe("Standing rules: search", () => {
  const manyRules = (n: number) =>
    Array.from({ length: n }, (_, i) => ({
      n: i + 1, text: i === 4 ? "Never the paid xAI API." : `Rule number ${i + 1} about builds.`,
      seeded: false, confirmed: true, source: "your words", date: "2026-10-13",
    }));

  it("appears only over eight rules, and filters them", async () => {
    store.set(OPEN_KEY, ALL_OPEN);
    answers.khipu_route_view = { ...view(), rules: manyRules(SEARCH_OVER), watched_rules: [] };
    const first = await page();
    expect(screen.queryByRole("searchbox", { name: "Search standing rules" })).toBeNull();
    first.unmount();
    answers.khipu_route_view = { ...view(), rules: manyRules(SEARCH_OVER + 3), watched_rules: [] };
    await page();
    const box = await screen.findByRole("searchbox", { name: "Search standing rules" });
    expect(box).toHaveAttribute("placeholder", "Search the 11 rules");
    fireEvent.change(box, { target: { value: "xai paid" } });
    const list = screen.getByRole("list", { name: "Standing rules" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    // The chips still count every rule.
    expect(chip("All, 11 rules")).toBeInTheDocument();
    fireEvent.change(box, { target: { value: "zebra" } });
    expect(screen.getByText(/No rules match “zebra”\./)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear search" }));
    expect(within(screen.getByRole("list", { name: "Standing rules" })).getAllByRole("listitem")).toHaveLength(11);
  });
});

describe("Job runner lanes: search", () => {
  const lanes = (n: number) => Array.from({ length: n }, (_, i) => `lane-${String(i + 1).padStart(2, "0")}`);

  it("appears only over eight lanes, and filters by lane, runner and account", async () => {
    store.set(OPEN_KEY, ALL_OPEN);
    const few = await page();
    expect(screen.queryByRole("searchbox", { name: "Search job runner lanes" })).toBeNull();
    few.unmount();

    const v = view();
    v.sources!.runners![0] = { ...v.sources!.runners![0]!, lanes: lanes(10) };
    answers.khipu_route_view = v;
    answers.khipu_config_show = config({ "lane-01": "Codex", "lane-02": "Codex", "lane-03": "Claude main" });
    await page();
    const box = await screen.findByRole("searchbox", { name: "Search job runner lanes" });
    expect(box).toHaveAttribute("placeholder", "Search the 10 lanes");
    fireEvent.change(box, { target: { value: "codex" } });
    expect(screen.getByText(/2 of 10 lanes match\./)).toBeInTheDocument();
    // The 7 lanes with no account are hidden and named.
    expect(screen.getByText(/lanes without an account are hidden by this search/)).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Lanes" }).textContent).toContain("lane-01");
    fireEvent.change(box, { target: { value: "zebra" } });
    expect(screen.getByText(/No lanes match “zebra”\./)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Lanes" })).toBeNull();
    // The folder rows live in the Job runner folders fold, whatever the search.
    expect(document.querySelector('[data-route-item="runner:Project A"]')).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Clear search" }));
    expect(within(screen.getByRole("list", { name: "Lanes" })).getAllByRole("listitem")).toHaveLength(10);
  });
});

describe("Recent changes: days and filters", () => {
  async function changes() {
    store.set(OPEN_KEY, ALL_OPEN);
    await page();
    await screen.findByRole("list", { name: "Today" });
  }

  it("shows Today first and earlier days only after Show earlier days", async () => {
    await changes();
    expect(screen.queryByRole("list", { name: "Yesterday" })).toBeNull();
    expect(screen.getByText("Yesterday has 2 more changes")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show earlier days" }));
    expect(screen.getByRole("list", { name: "Yesterday" })).toHaveTextContent("Grok Build paused by you");
    expect(screen.queryByRole("button", { name: "Show earlier days" })).toBeNull();
    await waitFor(() => expect(document.activeElement).toHaveTextContent("Yesterday"));
  });

  it("filters by who made a change and what it changed, with counts nowhere", async () => {
    await changes();
    const by = screen.getByRole("group", { name: "Changes by" });
    const about = screen.getByRole("group", { name: "Changes to" });
    fireEvent.click(within(by).getByRole("button", { name: "You" }));
    expect(within(screen.getByRole("list", { name: "Today" })).getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByRole("list", { name: "Today" })).toHaveTextContent("Standing rule 6 removed");
    expect(screen.getByText("Yesterday has 1 more change")).toBeInTheDocument();
    fireEvent.click(within(about).getByRole("button", { name: "Accounts" }));
    expect(screen.getByText("No changes today match these filters.")).toBeInTheDocument();
    expect(within(by).getByRole("button", { name: "You" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Show earlier days" }));
    expect(screen.getByRole("list", { name: "Yesterday" })).toHaveTextContent("Grok Build paused by you");
    fireEvent.click(within(by).getByRole("button", { name: "Job runners" }));
    expect(screen.getByText("No changes match these filters.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show every change" }));
    expect(within(by).getByRole("button", { name: "Everyone" })).toHaveAttribute("aria-pressed", "true");
    expect(within(about).getByRole("button", { name: "All kinds" })).toHaveAttribute("aria-pressed", "true");
    expect(within(screen.getByRole("list", { name: "Today" })).getAllByRole("listitem")).toHaveLength(2);
  });

  it("filters conversations and Khipu's own changes, and remembers the filters", async () => {
    await changes();
    fireEvent.click(within(screen.getByRole("group", { name: "Changes by" })).getByRole("button", { name: "Khipu" }));
    expect(screen.getByRole("list", { name: "Today" })).toHaveTextContent("Claude main");
    expect(JSON.parse(store.get(LISTS_KEY)!).changeBy).toBe("khipu");
    fireEvent.click(within(screen.getByRole("group", { name: "Changes by" })).getByRole("button", { name: "Conversations" }));
    expect(screen.getByText("Yesterday has 1 more change")).toBeInTheDocument();
  });

  it("says there are no changes yet when there are none", async () => {
    store.set(OPEN_KEY, ALL_OPEN);
    answers.khipu_route_view = { ...view(), changes: [] };
    await page();
    expect(await screen.findByText("No changes yet.")).toBeInTheDocument();
  });
});
