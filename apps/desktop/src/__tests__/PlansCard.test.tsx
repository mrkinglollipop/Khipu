// What PlansCard.tsx still provides after slice A11 removed the Home plans
// card and the Settings plans card: usePlanUsage (App and the setup account
// step read with it), usePlanAttention, and the plan-figure helpers. The
// tests are the ones from the old card's file whose subject still exists;
// the hook's read tests drive it through a small probe instead of the card.
// The fixture is `khipu usage --json` as the CLI prints it, built from the
// recorded replies in packages/cli/tests/fixtures/usage/, with reset times
// moved to after the faked "now" (only Date is faked).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { PLAN_READ_FAILED,
  accountsRefusal,
  fmtResetLocal,
  isPassingGap,
  needsOwner,
  passingGapHeld,
  planAttention,
  planPauses,
  planStatus,
  plansHealthDetail,
  resetPhrase,
  tightestOverall,
  tightestWindow,
  usageErrorPhrase,
  usePlanAttention,
  usePlanUsage,
  windowLabel,
} from "../PlansCard";
import type { PlanUsageState, UsageAccount, UsageReport } from "../PlansCard";
import type { RouteView } from "../routing";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

// Wed 2026-10-14 14:00 UTC.
const NOW = new Date("2026-10-14T14:00:00Z");
const CODEX_RESET = "2026-10-15T02:00:00Z";
const GROK_RESET = "2026-10-18T12:00:00Z";
const YESTERDAY = "2026-10-13T19:00:00Z";
const LOCAL_WITH_DATE: Intl.DateTimeFormatOptions = {
  weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
};
const LOCAL_WEEKDAY: Intl.DateTimeFormatOptions = { weekday: "short", hour: "numeric", minute: "2-digit" };

function realReport(): UsageReport {
  return {
    generated_at: "2026-10-14T13:57:29Z",
    max_age_s: 120,
    refreshing: false,
    accounts: [
      {
        key: "claude|", harness: "claude", account: "Claude", home: null, plan: "max",
        windows: [
          { name: "five_hour", used_pct: 16, resets_at: "2026-10-14T18:00:00Z", window_mins: 300 },
          { name: "weekly", used_pct: 17, resets_at: "2026-10-17T12:00:00Z", window_mins: 10080 },
          { name: "weekly_fable", used_pct: 34, resets_at: "2026-10-20T12:00:00Z", window_mins: 10080, model: "Fable" },
        ],
        credits: { extra_usage_enabled: false }, version: "2.1.296", source: "live", observed_at: null,
        error: null, note: null, ok: true, probed_at: "2026-10-14T13:57:29Z", ms: 0,
        last_attempt_at: "2026-10-14T13:57:29Z", last_error: null, last_good_at: "2026-10-14T13:57:29Z",
        cached: false, stale: false,
      },
      {
        key: "codex|", harness: "codex", account: "Codex", home: null, plan: "pro",
        windows: [
          { name: "weekly", used_pct: 40, resets_at: CODEX_RESET, window_mins: 10080, slot: "primary" },
        ],
        credits: { balance: 1234.5, has_credits: true, unlimited: false }, version: "0.162.1", source: "live",
        observed_at: null, error: null, note: null, ok: true, probed_at: "2026-10-14T13:57:29Z", ms: 0,
        last_attempt_at: "2026-10-14T13:57:29Z", last_error: null, last_good_at: "2026-10-14T13:57:29Z",
        cached: false, stale: false,
      },
      {
        key: "grok|", harness: "grok", account: "Grok", home: null, plan: "SuperGrok Heavy",
        windows: [{ name: "weekly", used_pct: 43.5, resets_at: GROK_RESET }],
        credits: { prepaid_balance: 44.0 }, version: "1.0.52", source: "live", observed_at: null,
        error: null, note: null, ok: true, probed_at: "2026-10-14T13:57:29Z", ms: 0,
        last_attempt_at: "2026-10-14T13:57:29Z", last_error: null, last_good_at: "2026-10-14T13:57:29Z",
        cached: false, stale: false,
      },
      {
        key: "muse|", harness: "muse", account: "Muse", home: null, plan: null, windows: [], credits: null,
        version: "1.4.6", source: "last_observed", observed_at: null, error: null,
        note: "no turn observed yet", ok: true, probed_at: "2026-10-14T13:57:29Z", ms: 0,
        last_attempt_at: "2026-10-14T13:57:29Z", last_error: null, last_good_at: "2026-10-14T13:57:29Z",
        cached: false, stale: false,
      },
    ] as UsageAccount[],
    summary_text: [],
  };
}

function withCodexAt(pct: number): UsageReport {
  const r = realReport();
  r.accounts![1].windows![0].used_pct = pct;
  return r;
}

type Deferred = { resolve: () => void };
let status: UsageReport;
let fresh: UsageReport;
let holdStatus: boolean;
let pending: Record<string, Deferred[]>;
let configShow: Record<string, unknown>;
/** The CLI's `{"ok": false, ...}` refusal of `config --set`, or null. */
let refuse: Record<string, unknown> | null;
const calls: Array<[string, unknown]> = [];

function release(cmd: string) {
  const d = pending[cmd]?.shift();
  if (!d) throw new Error(`nothing pending for ${cmd}`);
  d.resolve();
}

beforeEach(() => {
  vi.useFakeTimers({ now: NOW, toFake: ["Date"] });
  calls.length = 0;
  status = realReport();
  fresh = withCodexAt(94);
  holdStatus = false;
  pending = { khipu_usage_status: [], khipu_usage_refresh: [] };
  refuse = null;
  configShow = {
    usage_accounts: {
      value: null, source: "default", version: "av1",
      accounts: [
        { label: "Claude", harness: "claude", home: null },
        { label: "Codex", harness: "codex", home: null },
      ],
    },
  };
  const wait = (cmd: string) =>
    new Promise<void>((resolve) => {
      pending[cmd].push({ resolve });
    });
  invokeMock.mockImplementation(async (cmd: string, args?: unknown) => {
    calls.push([cmd, args]);
    if (cmd === "khipu_usage_status") {
      const answer = JSON.stringify(status);
      if (holdStatus) await wait(cmd);
      return answer;
    }
    if (cmd === "khipu_usage_refresh") {
      await wait(cmd);
      return JSON.stringify(fresh);
    }
    if (cmd === "khipu_config_show") return JSON.stringify(configShow);
    if (cmd === "khipu_config_set" || cmd === "khipu_config_unset") {
      return JSON.stringify(refuse ? { ok: false, ...refuse } : { ok: true });
    }
    throw new Error(`unexpected command ${cmd}`);
  });
});

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
  vi.useRealTimers();
});

let exposed: PlanUsageState | null = null;

function Home() {
  const state = usePlanUsage(true);
  exposed = state;
  const codex = state.data?.accounts?.find((a) => a.account === "Codex")?.windows?.[0];
  return (
    <div>
      <button type="button" aria-busy={state.refreshing || undefined} disabled={state.loading || state.refreshing}
        onClick={() => void state.refresh()}>
        {state.refreshing ? <span className="spin" /> : null}Refresh
      </button>
      {codex ? <p>{100 - (codex.used_pct ?? 0)}% left</p> : null}
      {state.error ? <p>{state.error}</p> : null}
    </div>
  );
}

const usageCalls = () => calls.map(([c]) => c).filter((c) => c.startsWith("khipu_usage"));

describe("usePlanUsage reads (moved from the old Home card's tests)", () => {
  it("Refresh calls the refresh command, spins while it runs, then shows the new figures", async () => {
    render(<Home />);
    await waitFor(() => expect(screen.getByText("60% left")).toBeTruthy());
    const button = screen.getByRole("button", { name: "Refresh" });
    fireEvent.click(button);
    await waitFor(() => expect(button.getAttribute("aria-busy")).toBe("true"));
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(button.querySelector(".spin")).toBeTruthy();
    expect(calls.find(([c]) => c === "khipu_usage_refresh")).toEqual(["khipu_usage_refresh", undefined]);
    await act(async () => release("khipu_usage_refresh"));
    await waitFor(() => expect(screen.getByText("6% left")).toBeTruthy());
    expect(button.getAttribute("aria-busy")).toBeNull();
  });

  it("a load asked for during a refresh never replaces the refresh's figures", async () => {
    render(<Home />);
    await waitFor(() => expect(screen.getByText("60% left")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(pending.khipu_usage_refresh).toHaveLength(1));
    // The Home header's Refresh, or coming back to Home, calls load().
    await act(async () => {
      void exposed!.load();
    });
    expect(usageCalls()).toEqual(["khipu_usage_status", "khipu_usage_refresh"]);
    await act(async () => release("khipu_usage_refresh"));
    await waitFor(() => expect(screen.getByText("6% left")).toBeTruthy());
    expect(screen.queryByText("60% left")).toBeNull();
  });

  it("starts one read at a time: loads share it and a refresh follows it", async () => {
    holdStatus = true;
    render(<Home />);
    await waitFor(() => expect(pending.khipu_usage_status).toHaveLength(1));
    expect(screen.getByRole("button", { name: "Refresh" })).toBeDisabled();
    await act(async () => {
      void exposed!.load();
      void exposed!.load();
      void exposed!.refresh();
      void exposed!.refresh();
    });
    expect(usageCalls()).toEqual(["khipu_usage_status"]);
    await act(async () => release("khipu_usage_status"));
    await waitFor(() => expect(usageCalls()).toEqual(["khipu_usage_status", "khipu_usage_refresh"]));
    await act(async () => release("khipu_usage_refresh"));
    await waitFor(() => expect(screen.getByText("6% left")).toBeTruthy());
    expect(usageCalls()).toEqual(["khipu_usage_status", "khipu_usage_refresh"]);
  });

  it("never shows the CLI's own failure text", async () => {
    const raw = "khipu exited 1: error: Could not determine home directory.";
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    invokeMock.mockImplementation(async () => {
      throw raw;
    });
    render(<Home />);
    await waitFor(() => expect(screen.getByText(PLAN_READ_FAILED)).toBeTruthy());
    expect(screen.getByText(PLAN_READ_FAILED)).toBeTruthy();
    expect(screen.queryByText(/khipu exited|home directory/)).toBeNull();
    expect(spy).toHaveBeenCalled();
    spy.mockRestore();
  });
});

describe("tightest window and resets", () => {
  it("picks the highest used percent per account and across accounts", () => {
    const r = realReport();
    expect(tightestWindow(r.accounts![0], NOW)?.name).toBe("weekly_fable");
    expect(tightestWindow(r.accounts![3], NOW)).toBeNull();
    expect(tightestOverall(r, NOW)?.row.account).toBe("Grok");
    expect(plansHealthDetail(r, NOW)).toBe(`Grok SuperGrok Heavy week at 43.5%, resets ${fmtResetLocal(GROK_RESET, NOW)}`);
    expect(planAttention(r, NOW)).toEqual([]);
  });

  it("dates a reset that has passed or is six or more days out", () => {
    const withDate = (iso: string) => new Date(iso).toLocaleString([], LOCAL_WITH_DATE);
    expect(resetPhrase(YESTERDAY, NOW)).toBe(`reset since ${withDate(YESTERDAY)}`);
    const nearlyAWeek = "2026-10-21T13:00:00Z";
    expect(resetPhrase(nearlyAWeek, NOW)).toBe(`resets ${withDate(nearlyAWeek)}`);
    const billingEnd = "2026-11-04T00:00:00Z";
    expect(resetPhrase(billingEnd, NOW)).toBe(`resets ${withDate(billingEnd)}`);
    expect(resetPhrase("2026-10-16T09:00:00Z", NOW)).toBe(
      `resets ${new Date("2026-10-16T09:00:00Z").toLocaleString([], LOCAL_WEEKDAY)}`,
    );
    expect(resetPhrase("2026-10-14T14:40:00Z", NOW)).toBe("resets in 40 min");
    expect(resetPhrase(null, NOW)).toBeNull();
  });
});

describe("the account cap and failures that are not about an entry (fix round 4)", () => {
  const TOO_MANY = "Khipu reads at most 32 accounts. Remove one before adding another.";
  let spy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    spy = vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => spy.mockRestore());

  it("says the cap when the app's own command refuses a list over it, with no code", () => {
    const sent = Array.from({ length: 33 }, (_, i) => ({ label: `c${i}`, harness: "claude" }));
    expect(accountsRefusal({ error: "usage_accounts holds at most 32 entries" }, sent, 32)).toEqual({
      field: null,
      message: TOO_MANY,
    });
  });

  it("never points at the Name or Login folder for a failure that is not about them", () => {
    const sent = [{ label: "Claude", harness: "claude" }, { label: "Work", harness: "codex", home: "~/.codex-work" }];
    for (const error of [
      "khipu worker join failed: task panicked",
      "bundled Python not found at /Applications/Khipu.app/Contents/Resources/python/bin/python3",
      "khipu exited 1: Traceback (most recent call last)",
      "spawn khipu: No such file or directory",
    ]) {
      const problem = accountsRefusal({ error }, sent, 1);
      expect(problem).toEqual({ field: null, message: "Khipu could not save the account list. Try again in a minute." });
      expect(problem.message).not.toMatch(/Name|Login folder/);
    }
    for (const code of ["bad_list", "bad_entry"]) {
      expect(accountsRefusal({ error: "x", refusal: { code, index: 1 } }, sent, 1).message).not.toMatch(
        /Name|Login folder/,
      );
    }
  });
});

describe("the 90% item while Home stays open", () => {
  function AttentionProbe({ report }: { report: UsageReport }) {
    const items = usePlanAttention(report);
    return <div data-testid="plan-attention">{items.map((a) => a.cause).join(" | ")}</div>;
  }

  it("counts down every minute and drops out at the reset, with no new read", () => {
    vi.useFakeTimers({ now: NOW, toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    const r = realReport();
    r.accounts![0].windows![0] = { name: "five_hour", used_pct: 95, resets_at: "2026-10-14T14:30:00Z", window_mins: 300 };
    render(<AttentionProbe report={r} />);
    const text = () => screen.getByTestId("plan-attention").textContent;
    expect(text()).toContain("Claude Max five-hour at 95%, resets in 30 min");
    act(() => {
      vi.advanceTimersByTime(10 * 60_000);
    });
    expect(text()).toContain("Claude Max five-hour at 95%, resets in 20 min");
    act(() => {
      vi.advanceTimersByTime(20 * 60_000);
    });
    expect(text()).toBe("");
  });

  it("judges figures that arrive later on the clock at their arrival", () => {
    vi.useFakeTimers({ now: NOW, toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    const calm = realReport();
    const { rerender } = render(<AttentionProbe report={calm} />);
    expect(screen.getByTestId("plan-attention").textContent).toBe("");
    // Nothing ticks while nothing is near its limit; two hours later a read
    // brings a window that reset an hour ago.
    vi.setSystemTime(new Date("2026-10-14T16:00:00Z"));
    const late = realReport();
    late.accounts![0].windows![0] = { name: "five_hour", used_pct: 97, resets_at: "2026-10-14T15:00:00Z", window_mins: 300 };
    rerender(<AttentionProbe report={late} />);
    expect(screen.getByTestId("plan-attention").textContent).toBe("");
  });
});

describe("one status phrase per account (settings clarity, phase 2)", () => {
  /** Every state in the scope's table, one account each. */
  function tableReport(): UsageReport {
    const r = realReport();
    const [claude, codex, grok] = r.accounts!;
    return {
      ...r,
      accounts: [
        claude,
        { ...codex, account: "Kept", ok: false, error: "timeout", stale: true, probed_at: "2026-10-12T14:00:00Z",
          last_good_at: "2026-10-12T14:00:00Z", last_attempt_at: "2026-10-14T13:59:30Z",
          note: "Codex didn't answer in time" },
        { ...grok, windows: [], ok: false, error: "no_answer", note: "Grok Build didn't answer", last_good_at: null },
        { ...codex, account: "Signed out", windows: [], ok: false, error: "not_signed_in",
          note: "Codex is not signed in. Open Codex and sign in again" },
        r.accounts![3],
        { ...r.accounts![3], account: "Muse two", plan: "pro", note: null, observed_at: "2026-10-14T12:00:00Z",
          windows: [{ name: "five_hour", used_pct: 12, resets_at: "2026-10-14T15:00:00Z", window_mins: 300 }] },
      ],
    };
  }

  it("judges the status from the report alone", () => {
    const r = tableReport();
    expect(planStatus(r.accounts![0], NOW)).toEqual({ figures: "live", say: null, tone: null, code: null });
    expect(planStatus(r.accounts![1], NOW).figures).toBe("kept");
    expect(planStatus(r.accounts![2], NOW).figures).toBe("none");
  });
});

describe("per-account lookups use the backend's stable key, never the label (review P2)", () => {
  // Claude was paused, then removed from the list, and its name reused for a
  // Codex login: the record still holds the paused "claude|" account.
  const record: RouteView = {
    accounts: [
      { key: "claude|", label: "Work", harness: "claude", state: "paused", until: "2026-10-21T21:26:00Z" },
      { key: "claude|/logins/second", label: "Old name", harness: "claude", state: "paused", until: null },
    ],
  };

  it("shows no pause for a row with no key rather than guessing by name", () => {
    expect(planPauses(record).get("Work")).toBeUndefined();
    expect(planPauses(record).get("claude|")).toMatch(/^Paused by you until /);
  });
});

describe("one rule for passing gaps and codes that need the owner (shared with Settings)", () => {
  const NOW = new Date("2026-10-14T15:40:00Z");
  const kept = (extra: Partial<UsageAccount> = {}): UsageAccount => ({
    harness: "claude", account: "Claude second", ok: false, error: "no_plan_figures", stale: true,
    windows: [{ name: "weekly", used_pct: 40, resets_at: "2026-10-17T19:00:00Z" }],
    last_good_at: "2026-10-14T08:40:00Z", probed_at: "2026-10-14T15:38:00Z", ...extra,
  });

  it("sorts the codes: sign-in trouble needs the owner, every other failure but pending passes", () => {
    for (const c of ["not_signed_in", "no_plan_limits"]) {
      expect(needsOwner(c)).toBe(true);
      expect(isPassingGap(c)).toBe(false);
    }
    for (const c of ["no_plan_figures", "timeout", "no_answer", "exit_1", "cli_error", "bad_reply"]) {
      expect(needsOwner(c)).toBe(false);
      expect(isPassingGap(c)).toBe(true);
    }
    for (const c of ["pending", null, undefined, ""]) expect(isPassingGap(c)).toBe(false);
  });

  it("says Home's kept-figures words for a passing gap and holds it until the stale setting", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW);
    try {
      expect(planStatus(kept(), NOW)).toMatchObject({ figures: "kept", say: "Can't read it now; figures from 7 h ago", code: "no_plan_figures" });
      expect(passingGapHeld(kept(), 12 * 3600, NOW)).toBe(true);
      expect(passingGapHeld(kept(), 6 * 3600, NOW)).toBe(false);
      expect(passingGapHeld(kept(), 0, NOW)).toBe(true);
      expect(passingGapHeld(kept({ windows: [] }), 12 * 3600, NOW)).toBe(false);
      expect(passingGapHeld(kept({ error: "not_signed_in" }), 12 * 3600, NOW)).toBe(false);
      expect(planStatus(kept({ error: "not_signed_in" }), NOW).say).toBe("Not signed in");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("rows read from a tool's own login (Cursor, OpenCode Go)", () => {
  it("names Cursor's pools by their model words and says a rejected login can't be read", () => {
    expect(windowLabel({ name: "monthly_cursor_models", model: "Cursor models" })).toBe("Cursor models month");
    expect(windowLabel({ name: "monthly_other_models" })).toBe("other models month");
    expect(usageErrorPhrase("key_rejected", "cursor")).toBe("can't read: its login was not accepted");
    expect(usageErrorPhrase("no_plan", "opencode")).toBe("no usage plan on this login");
  });
});
