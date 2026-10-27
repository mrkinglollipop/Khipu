// Routing truth phase 4b in the app: the owner's priority on Home and in
// Settings -> Routing, the spend pace beside each account, and each Plans
// account saying whether Khipu can read it, with Sign in again.
(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { PlansSettingsCard, SignInControl, liveWords, signInOffer, signInOutcome } from "../PlansCard";
import type { PlanUsageState, UsageAccount } from "../PlansCard";
import { RoutingCard } from "../RoutingCard";
import { RoutingSettings } from "../RoutingSettings";
import { paceWords, priorityHeadline } from "../routing";
import { accountWeek } from "../routingOverview";
import type { RoutePriority, RouteView, RoutingState } from "../routing";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

const NOW = new Date("2026-10-15T14:00:00Z");
const TWO_MIN_AGO = "2026-10-15T13:58:00Z";
const SECOND = "claude|/logins/claude-second";

function row(extra: Partial<UsageAccount>): UsageAccount {
  return { harness: "claude", account: "Claude", home: null, ok: true, error: null,
    windows: [{ name: "weekly", used_pct: 10, resets_at: null }],
    probed_at: TWO_MIN_AGO, last_attempt_at: TWO_MIN_AGO, ...extra };
}

type Answer = string | object | ((args: Record<string, unknown> | undefined) => unknown);
let answers: Record<string, Answer>;

// Settings clarity phase 3 folds Settings -> Accounts and routing into
// groups; these tests read every group and account line open.
const ALL_OPEN = [
  "accounts", "priority", "rules", "holds", "telling", "resets", "runners", "table", "changes", "sub:hears", "sub:sizes",
  ...["claude|", SECOND, "codex|", "grok|"].map((k) => `account:${k}`),
];

// jsdom here has no usable localStorage; an in-memory one stands in.
function memoryStorage() {
  const m = new Map<string, string>();
  return {
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => void m.set(k, String(v)),
    removeItem: (k: string) => void m.delete(k),
    clear: () => m.clear(),
  };
}

beforeEach(() => {
  vi.stubGlobal("localStorage", memoryStorage());
  localStorage.setItem("khipu.routing.open", JSON.stringify(ALL_OPEN));
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  answers = {};
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    const a = answers[cmd] ?? { ok: true };
    const out = typeof a === "function" ? a(args) : a;
    return typeof out === "string" ? out : JSON.stringify(out);
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  invokeMock.mockReset();
});

// Routing writes carry a per-operation id (`op`, routing slice 3b) so a
// retried write is recognised as the same change. These tests read the
// arguments that say what the owner asked for; RoutingSettings.test.tsx
// covers the ids themselves.
function calls(cmd: string) {
  return invokeMock.mock.calls.filter((c) => c[0] === cmd).map((c) => {
    const { op: _op, ...args } = (c[1] ?? {}) as Record<string, unknown>;
    return args;
  });
}

// ---------------------------------------------------------------------------
// Plans: whether Khipu can read each account, and Sign in again
// ---------------------------------------------------------------------------

describe("each account says whether Khipu can read it", () => {
  it("words every state in plain words", () => {
    expect(liveWords(row({}))).toEqual({ text: "Read 2 min ago", live: true });
    expect(liveWords(row({ ok: false, error: "no_plan_limits" })).text).toBe("Signed in, but its plan figures can't be read");
    expect(liveWords(row({ ok: false, error: "not_signed_in" })).text).toBe("Not signed in");
    expect(liveWords(row({ ok: false, error: "not_installed" })).text).toBe("CLI not installed");
    // Codex's last-turn figures after a signed-out live read are not live.
    expect(liveWords(row({ harness: "codex", live_error: "not_signed_in" })).text).toBe("Not signed in");
    expect(liveWords(row({ ok: false, error: "timeout" })).text).toBe("Can't be read just now (didn't answer in time)");
  });

  it("offers Sign in again for Claude and Codex, and says where to sign in for Grok and Muse", () => {
    expect(signInOffer(row({}))).toBeNull();
    expect(signInOffer(row({ ok: false, error: "no_plan_figures", home: "~/.claude-second" }))).toBeNull();
    expect(liveWords(row({ ok: false, error: "no_plan_figures" })).text).toBe(
      "Can't be read just now (didn't send its plan figures)");
    expect(signInOffer(row({ ok: false, error: "not_signed_in" }))).toBe("button");
    expect(signInOffer(row({ ok: false, error: "no_plan_limits", home: "~/.claude-second" }))).toBe("button");
    expect(signInOffer(row({ harness: "codex", ok: false, error: "not_signed_in" }))).toBe("button");
    expect(signInOffer(row({ ok: false, error: "not_installed" }))).toBeNull();
    expect(signInOffer(row({ harness: "grok", ok: false, error: "not_signed_in" }))).toBe("elsewhere");
    // Settings clarity phase 2: only a sign-in problem offers a sign-in.
    for (const error of ["timeout", "cli_error", "bad_reply", "no_answer", "pending", "invalid_account"]) {
      expect(signInOffer(row({ harness: "grok", ok: false, error }))).toBeNull();
    }
    render(<SignInControl row={row({ harness: "grok", account: "Grok", ok: false, error: "not_signed_in" })} />);
    expect(screen.getByText("Sign in from Grok Build")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
    cleanup();
    render(<SignInControl row={row({ harness: "muse", account: "Muse", ok: false, error: "no_plan_limits" })} />);
    expect(screen.getByText("Sign in from Muse")).toBeTruthy();
    cleanup();
    const { container } = render(<SignInControl row={row({})} />);
    expect(container.textContent).toBe("");
  });

  it("sends only the account's CLI and login folder, then reads again and shows the new state", async () => {
    const second = row({ account: "Claude second", home: "~/.claude-second", ok: false, error: "no_plan_limits" });
    const state: PlanUsageState = {
      data: { accounts: [row({ ok: false, error: "not_signed_in" }), second] }, error: null, errorAt: null, loading: false,
      refreshing: false, load: vi.fn(), refresh: vi.fn(), reread: vi.fn(async () => undefined),
    };
    answers.khipu_config_show = { usage_accounts: { value: null, source: "default", accounts: [] } };
    answers.khipu_account_sign_in = (args) => ({
      ok: true, account: args?.home ? "Claude second" : "Claude",
      row: row({ account: args?.home ? "Claude second" : "Claude", probed_at: NOW.toISOString(), last_attempt_at: NOW.toISOString() }),
    });
    render(<PlansSettingsCard state={state} active />);
    fireEvent.click(within(screen.getByRole("group", { name: "Claude second" })).getByRole("button", { name: "Sign Claude second in again" }));
    await waitFor(() => expect(calls("khipu_account_sign_in")).toEqual([{ harness: "claude", home: "~/.claude-second" }]));
    await screen.findByText("Signed in. Read just now.");
    expect(state.reread).toHaveBeenCalledTimes(1);
    fireEvent.click(within(screen.getByRole("group", { name: "Claude" })).getByRole("button", { name: "Sign Claude in again" }));
    await waitFor(() => expect(calls("khipu_account_sign_in")[1]).toEqual({ harness: "claude", home: null }));
  });

  it("says a sign-in left open or refused in plain words", () => {
    expect(signInOutcome({ ok: false, error: "x", refusal: { ok: false, error: "sign_in_timeout" } })).toBe(
      "Khipu stopped waiting after 15 minutes. Press Sign in again to start over.");
    expect(signInOutcome({ ok: false, error: "x", refusal: { ok: false, error: "exit_1",
      row: row({ ok: false, error: "not_signed_in" }) } })).toBe("The sign-in didn't finish. Not signed in.");
    expect(signInOutcome({ ok: false, error: "khipu exited 2" })).toBe("The sign-in didn't start: khipu exited 2.");
  });
});

// ---------------------------------------------------------------------------
// The owner's priority on Home and in Settings -> Routing
// ---------------------------------------------------------------------------

const PRIORITY: RoutePriority = {
  entries: [
    { account: SECOND, label: "Claude second", role: "all Claude work first", until: "2026-10-17T19:00:00Z", in_settings: true },
    { account: "codex|", label: "Codex", role: "cross-family reviews", until: null, in_settings: true },
    { account: "claude|", label: "Claude", role: "orchestration only", until: null, in_settings: true },
  ],
  until: null, at: "2026-10-15T13:00:00Z", via: "app", pending: false, confirmed: true, version: "p7",
};

function view(priority: RoutePriority | null = PRIORITY): RouteView {
  return {
    record: true, version: 9, checked_at: NOW.toISOString(),
    accounts: [
      { key: "claude|", label: "Claude", harness: "claude", state: "open", configured: true,
        witness: { source: "probe", at: TWO_MIN_AGO } },
      { key: SECOND, label: "Claude second", harness: "claude", state: "open", configured: true,
        witness: { source: "probe", at: TWO_MIN_AGO } },
      { key: "codex|", label: "Codex", harness: "codex", state: "open", configured: true,
        witness: { source: "probe", at: TWO_MIN_AGO } },
      { key: "grok|", label: "Grok", harness: "grok", state: "open", configured: true,
        witness: { source: "probe", at: TWO_MIN_AGO } },
    ],
    holds: [], rules: [], note: null, changes: [], rule_checks: [],
    warn_after: { disagreement: 1800, not_refreshed: 300 }, report_default_limit: 1800,
    priority,
    plan_facts: {
      [SECOND]: { readable: true, window: "week", used_pct: 12, left_pct: 88, resets_at: "2026-10-17T18:00:00Z",
        seconds: 187200, pace_pct_per_hour: 1.692, hurry: true, words: "a lot left, little time" },
      "grok|": { readable: false, why: "plan figures can't be read: sign in again" },
    },
    pace_order: true,
  };
}

function stateOf(data: RouteView, load = vi.fn(async () => undefined)): RoutingState {
  return { data, error: null, loading: false, lastOkAt: NOW.toISOString(), firstRead: false, load };
}

describe("the priority on Home", () => {
  it("shows the owner's order at the top in local time", () => {
    render(<RoutingCard state={stateOf(view())} />);
    const prio = document.querySelector(".route-prio");
    expect(prio?.textContent).toBe("Priority: Claude second until Sat 3:00 PM, then Codex, then Claude");
    expect(screen.queryByRole("button", { name: "Confirm" })).toBeNull();
  });

  it("marks one a conversation set, with Confirm and Undo", async () => {
    const load = vi.fn(async () => undefined);
    const pending = { ...PRIORITY, pending: true, confirmed: false, pending_by: "a Claude Code conversation", via: "mcp" };
    render(<RoutingCard state={stateOf(view(pending), load)} />);
    expect(screen.getByText("Not confirmed")).toBeTruthy();
    expect(screen.getByText("Set by a Claude Code conversation at your request; you haven't confirmed it yet.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(calls("khipu_route_priority")).toEqual([{ action: "confirm", expect: "p7" }]));
    await waitFor(() => expect(load).toHaveBeenCalledWith(true));
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(calls("khipu_route_priority")[1]).toEqual({ action: "undo", expect: "p7" }));
  });

  it("collects an end-or-keep choice before confirming a held priority on Home", async () => {
    const load = vi.fn(async () => undefined);
    const pending = { ...PRIORITY, pending: true, confirmed: false };
    answers.khipu_route_priority = (args) => args?.holds
      ? { ok: true }
      : { ok: false, error: "say end or keep in holds for: h1" };
    const data = view(pending);
    data.holds = [{ id: "h1", scope_label: "Claude", reason: "keep", owner: true, until: null }];
    render(<RoutingCard state={stateOf(data, load)} />);
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await screen.findByRole("group", { name: "End or keep each hold" });
    fireEvent.click(screen.getByRole("radio", { name: "Keep it" }));
    fireEvent.click(screen.getByRole("button", { name: "Save and confirm" }));
    await waitFor(() => expect(calls("khipu_route_priority")).toEqual([
      { action: "confirm", expect: "p7" },
      { action: "confirm", expect: "p7", holds: { h1: "keep" } },
    ]));
  });
});

describe("the priority and the pace in Settings -> Routing", () => {
  function config(extra: Record<string, unknown> = {}) {
    const s = (value: unknown) => ({ value, default: value, kind: "x", source: "default" });
    return { routing: { routing_pace_order: s(true), routing_priority_unconfirmed_warn_after: s(86400), ...extra } };
  }

  async function page(data: RouteView = view()) {
    answers.khipu_route_view = data;
    answers.khipu_config_show = config();
    answers.khipu_features_show = { features: { routing_push: { enabled: true, source: "file" } } };
    render(<RoutingSettings active />);
    await screen.findByRole("list", { name: "Accounts" });
  }

  const sent = () => calls("khipu_route_priority");

  it("moves, removes and adds accounts, always sending the whole list in the order shown", async () => {
    await page();
    const list = screen.getByRole("list", { name: "Your priority, first to last" });
    expect(within(list).getAllByRole("listitem").map((li) => li.textContent?.split(".")[1]?.trim().split(/all|cross|orch/)[0]))
      .toEqual(["Claude second", "Codex", "Claude"]);
    fireEvent.click(screen.getByRole("button", { name: "Move Codex up" }));
    await waitFor(() => expect(sent()[0]).toEqual({ action: "set", until: null, expect: "p7", entries: [
      { account: "codex|", role: "cross-family reviews", until: null },
      { account: SECOND, role: "all Claude work first", until: "2026-10-17T19:00:00Z" },
      { account: "claude|", role: "orchestration only", until: null },
    ] }));
    fireEvent.click(screen.getByRole("button", { name: "Remove Codex from your priority" }));
    await waitFor(() => expect((sent()[1] as { entries: unknown[] }).entries).toEqual([
      { account: SECOND, role: "all Claude work first", until: "2026-10-17T19:00:00Z" },
      { account: "claude|", role: "orchestration only", until: null },
    ]));
    fireEvent.change(screen.getByLabelText("Add to the end"), { target: { value: "grok|" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect((sent()[2] as { entries: { account: string }[] }).entries.map((e) => e.account))
      .toEqual([SECOND, "codex|", "claude|", "grok|"]));
  });

  it("sends a role that looks like an option as words, and an end as a time", async () => {
    await page();
    fireEvent.click(screen.getByRole("button", { name: "Change Claude's role or end" }));
    const form = screen.getByRole("form", { name: "Change Claude in your priority" });
    fireEvent.change(within(form).getByLabelText("Its role, in your words"), { target: { value: "--from-app -- clear" } });
    fireEvent.change(within(form).getByLabelText("Claude keeps its place"), { target: { value: "tomorrow" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(sent()).toHaveLength(1));
    const last = (sent()[0] as { entries: { account: string; role: string; until: string }[] }).entries[2];
    expect(last.account).toBe("claude|");
    expect(last.role).toBe("--from-app -- clear");
    expect(last.until).toMatch(/^2026-10-16T09:00:00/);
    expect((sent()[0] as { expect: string }).expect).toBe("p7");
  });

  it("shows a plain sentence, not the CLI's text, when the priority moved since it was read", async () => {
    await page();
    answers.khipu_route_priority = () => ({ ok: false, code: "version_moved", error: "priority version moved" });
    fireEvent.click(screen.getByRole("button", { name: "Move Codex up" }));
    expect(await screen.findByText(/changed somewhere else after this page read it/)).toBeTruthy();
    expect(screen.queryByText(/priority version moved/)).toBeNull();
  });

  it("clears the whole priority after asking, and confirms or undoes one a conversation set", async () => {
    await page({ ...view(), priority: { ...PRIORITY, pending: true, confirmed: false, pending_by: "a Codex conversation",
      owner_request: "the owner asked for Codex first", replaced: { entries: [PRIORITY.entries[2]] } } });
    expect(screen.getByText("What it said you asked: “the owner asked for Codex first”")).toBeTruthy();
    expect(screen.getByText(/It replaced: 1\. Claude: orchestration only/)).toBeTruthy();
    const group = screen.getByRole("group", { name: "A conversation set your priority" });
    fireEvent.click(within(group).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(sent()).toEqual([{ action: "confirm", expect: "p7" }]));
    fireEvent.click(within(group).getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(sent()[1]).toEqual({ action: "undo", expect: "p7" }));
    fireEvent.click(screen.getByRole("button", { name: "Clear the priority" }));
    fireEvent.click(screen.getByRole("button", { name: "Clear it" }));
    await waitFor(() => expect(sent()[2]).toEqual({ action: "clear", expect: "p7" }));
  });

  it("shows each account's pace beside it, and the ordering switch and its warning setting", async () => {
    await page(view(null));
    expect(screen.getByText("No priority set. Conversations hear the accounts in their usual order.")).toBeTruthy();
    const accounts = screen.getByRole("list", { name: "Accounts" });
    // An account's line opens its Routing detail, which says the pace.
    const detailOf = (start: string) => {
      const li = within(accounts).getAllByRole("listitem").find((x) => x.textContent?.startsWith(start))!;
      fireEvent.click(within(li).getByRole("button"));
      return screen.getByRole("dialog");
    };
    const second = detailOf("Claude second");
    expect(second).toHaveTextContent("88% of its week left, resets Sat 2:00 PM: using it all would take about 1.7% an hour");
    fireEvent.click(within(second).getByRole("button", { name: "Close details" }));
    const grok = detailOf("Grok");
    expect(grok).toHaveTextContent("Plan figures can't be read: sign in again");
    fireEvent.click(within(grok).getByRole("button", { name: "Close details" }));
    const sw = screen.getByRole("switch", { name: "With no priority set, list accounts by how soon their allowance runs out unused" });
    expect(sw).toBeChecked();
    fireEvent.click(sw);
    await waitFor(() => expect(calls("khipu_config_set")).toEqual([{ key: "routing_pace_order", value: "off" }]));
    expect(screen.getByRole("button", { name: "Change: Warn when a priority a conversation set is still unconfirmed after" })
      .closest(".route-limit")).toHaveTextContent("24 hours");
  });
});

describe("the words", () => {
  it("say the pace and the headline from the CLI's figures", () => {
    expect(paceWords({ readable: true, window: "week", left_pct: 0, resets_at: "2026-10-17T18:00:00Z" }, NOW))
      .toBe("0% of its week left, resets Sat 2:00 PM: nothing left to use until then");
    expect(priorityHeadline({ ...PRIORITY, until: "2026-10-20T13:00:00Z" }, NOW))
      .toBe("Priority: Claude second until Sat 3:00 PM, then Codex, then Claude (all of it until Tue 9:00 AM)");
  });
  it("give figures that may be out of date no pace, and say when a reset passed", () => {
    const stale = { readable: true, window: "week", left_pct: 26, used_pct: 74, resets_at: "2026-10-19T02:00:00Z",
      stale: true, figures_at: "2026-10-15T12:38:00Z", pace_pct_per_hour: undefined, words: "figures may be out of date" };
    const text = paceWords(stale, NOW) ?? "";
    expect(text).toContain("26% of its week left when read 82 min ago");
    expect(text).toContain("may be out of date, so no pace is given");
    expect(text).not.toMatch(/an hour|plenty of time|little time/);
    const overview = accountWeek(stale, NOW);
    expect(overview.week).toContain("when read 82 min ago");
    expect(overview.pace).toBe("These figures may be out of date, so no pace is given");
    const passed = { readable: false, why: "its reset time passed; figures out of date", reset_passed: true };
    expect(paceWords(passed, NOW)).toBe("Its reset time passed; figures out of date");
    expect(accountWeek(passed, NOW).week).toBe("Its reset time passed; figures out of date");
  });
});
