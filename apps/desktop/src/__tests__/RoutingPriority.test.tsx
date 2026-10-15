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

beforeEach(() => {
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
  vi.useRealTimers();
  invokeMock.mockReset();
});

function calls(cmd: string) {
  return invokeMock.mock.calls.filter((c) => c[0] === cmd).map((c) => c[1]);
}

// ---------------------------------------------------------------------------
// Plans: whether Khipu can read each account, and Sign in again
// ---------------------------------------------------------------------------

describe("each account says whether Khipu can read it", () => {
  it("words every state in plain words", () => {
    expect(liveWords(row({}))).toEqual({ text: "Live: read 2 min ago", live: true });
    expect(liveWords(row({ ok: false, error: "no_plan_limits" })).text).toBe("Signed in, but its plan figures can't be read");
    expect(liveWords(row({ ok: false, error: "not_signed_in" })).text).toBe("Not signed in");
    expect(liveWords(row({ ok: false, error: "not_installed" })).text).toBe("CLI not installed");
    // Codex's last-turn figures after a signed-out live read are not live.
    expect(liveWords(row({ harness: "codex", live_error: "not_signed_in" })).text).toBe("Not signed in");
    expect(liveWords(row({ ok: false, error: "timeout" })).text).toBe("Can't be read just now (didn't answer in time)");
  });

  it("offers Sign in again for Claude and Codex, and says where to sign in for Grok and Muse", () => {
    expect(signInOffer(row({}))).toBeNull();
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
    await screen.findByText("Signed in. Live: read just now.");
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
  until: null, at: "2026-10-15T13:00:00Z", via: "app", pending: false, confirmed: true,
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
    expect(screen.getByText("Priority: Claude second until Sat 3:00 PM, then Codex, then Claude")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Confirm" })).toBeNull();
  });

  it("marks one a conversation set, with Confirm and Undo", async () => {
    const load = vi.fn(async () => undefined);
    const pending = { ...PRIORITY, pending: true, confirmed: false, pending_by: "a Claude Code conversation", via: "mcp" };
    render(<RoutingCard state={stateOf(view(pending), load)} />);
    expect(screen.getByText("Not confirmed")).toBeTruthy();
    expect(screen.getByText("Set by a Claude Code conversation at your request; you haven't confirmed it yet.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(calls("khipu_route_priority")).toEqual([{ action: "confirm" }]));
    await waitFor(() => expect(load).toHaveBeenCalledWith(true));
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(calls("khipu_route_priority")[1]).toEqual({ action: "undo" }));
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
    await waitFor(() => expect(sent()[0]).toEqual({ action: "set", until: null, entries: [
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
  });

  it("clears the whole priority after asking, and confirms or undoes one a conversation set", async () => {
    await page({ ...view(), priority: { ...PRIORITY, pending: true, confirmed: false, pending_by: "a Codex conversation",
      owner_request: "the owner asked for Codex first", replaced: { entries: [PRIORITY.entries[2]] } } });
    expect(screen.getByText("What it said you asked: “the owner asked for Codex first”")).toBeTruthy();
    expect(screen.getByText(/It replaced: 1\. Claude: orchestration only/)).toBeTruthy();
    const group = screen.getByRole("group", { name: "A conversation set your priority" });
    fireEvent.click(within(group).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(sent()).toEqual([{ action: "confirm" }]));
    fireEvent.click(within(group).getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(sent()[1]).toEqual({ action: "undo" }));
    fireEvent.click(screen.getByRole("button", { name: "Clear the priority" }));
    fireEvent.click(screen.getByRole("button", { name: "Clear it" }));
    await waitFor(() => expect(sent()[2]).toEqual({ action: "clear" }));
  });

  it("shows each account's pace beside it, and the ordering switch and its warning setting", async () => {
    await page(view(null));
    expect(screen.getByText("No priority set. Conversations hear the accounts in their usual order.")).toBeTruthy();
    const accounts = screen.getByRole("list", { name: "Accounts" });
    const second = within(accounts).getAllByRole("listitem").find((li) => li.textContent?.startsWith("Claude second"))!;
    expect(second).toHaveTextContent("88% of its week left, resets Sat 2:00 PM: about 1.7% an hour to use it all (a lot left, little time)");
    const grok = within(accounts).getAllByRole("listitem").find((li) => li.textContent?.startsWith("Grok"))!;
    expect(grok).toHaveTextContent("Plan figures can't be read: sign in again");
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
});
