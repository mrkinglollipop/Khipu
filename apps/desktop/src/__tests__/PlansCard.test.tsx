// Plan usage in the app (docs/plans/2026-10-14-plan-usage.md, phase 2): the
// Home card, the Settings card, the health report's Plans row and the
// attention item at 90%. The fixture is `khipu usage --json` as the CLI prints
// it, built from the recorded replies in packages/cli/tests/fixtures/usage/,
// with reset times moved to after the faked "now" (only Date is faked, so
// waitFor still runs on real timers).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  ACCOUNTS_MAX,
  MUSE_NO_TURN,
  PLAN_READ_FAILED,
  PlansCard,
  PlansSettingsCard,
  accountClash,
  accountsRefusal,
  defaultAccountLabel,
  fmtPct,
  fmtResetLocal,
  formProblem,
  isPassingGap,
  liveReadFailures,
  loginKey,
  meterTone,
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
} from "../PlansCard";
import type { PlanUsageState, UsageAccount, UsageReport } from "../PlansCard";
import { HealthReportRows, plansHealthRow } from "../healthRows";
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

/** Muse's last turn left five-hour at 95%, and that window reset yesterday. */
function withMuseResetAt95(): UsageReport {
  const r = realReport();
  r.accounts![3] = {
    ...r.accounts![3], plan: "pro", note: null, observed_at: "2026-10-13T14:00:00Z", stale: true,
    windows: [{ name: "five_hour", used_pct: 95, resets_at: YESTERDAY, window_mins: 300 }],
  };
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
  return <PlansCard state={state} />;
}

function Settings() {
  const state = usePlanUsage("settings");
  exposed = state;
  return <PlansSettingsCard state={state} active />;
}

function rowFor(name: string): HTMLElement {
  const el = screen.getAllByText(name).map((n) => n.closest(".plan-row")).find(Boolean);
  if (!el) throw new Error(`no row for ${name}`);
  return el as HTMLElement;
}

const usageCalls = () => calls.map(([c]) => c).filter((c) => c.startsWith("khipu_usage"));

describe("PlansCard on Home", () => {
  it("renders the four accounts of the real report, each with its tightest window", async () => {
    render(<Home />);
    await waitFor(() => expect(screen.getByText("Muse")).toBeTruthy());
    expect(calls[0]).toEqual(["khipu_usage_status", undefined]);
    for (const name of ["Claude", "Codex", "Grok", "Muse"]) expect(rowFor(name)).toBeTruthy();
    // Claude's tightest is the Fable week at 34%, not the five-hour window.
    const claude = within(rowFor("Claude"));
    expect(claude.getByText("34%")).toBeTruthy();
    expect(claude.getByText(/Fable week/)).toBeTruthy();
    expect(claude.getByRole("meter").getAttribute("aria-valuenow")).toBe("34");
    expect(within(rowFor("Grok")).getByText("43.5%")).toBeTruthy();
    expect(within(rowFor("Codex")).getByText(`week · resets ${fmtResetLocal(CODEX_RESET, NOW)}`)).toBeTruthy();
    expect(screen.getAllByRole("meter")).toHaveLength(3);
  });

  it("says Muse has no figures yet when no turn has been observed, not 0% and not Live", async () => {
    render(<Home />);
    await waitFor(() => expect(screen.getByText(MUSE_NO_TURN)).toBeTruthy());
    const muse = within(rowFor("Muse"));
    expect(muse.queryByRole("meter")).toBeNull();
    expect(muse.queryByText(/0%/)).toBeNull();
    expect(muse.queryByText(/Live/)).toBeNull();
    expect(screen.queryByText(/Live/)).toBeNull();
    expect(muse.queryByText(/Sign in/)).toBeNull();
  });

  it("warns at 75% and alerts at 90%", async () => {
    status = withCodexAt(80);
    const { unmount } = render(<Home />);
    await waitFor(() => expect(screen.getByText("80%")).toBeTruthy());
    expect(within(rowFor("Codex")).getByRole("meter").className).toBe("plan-meter warn");
    expect(within(rowFor("Grok")).getByRole("meter").className).toBe("plan-meter");
    unmount();
    status = withCodexAt(94);
    render(<Home />);
    await waitFor(() => expect(screen.getByText("94%")).toBeTruthy());
    expect(within(rowFor("Codex")).getByRole("meter").className).toBe("plan-meter err");
  });

  it("maps every fixed error code to its own plain phrase", async () => {
    const phrases: Record<string, string> = {
      timeout: "didn't answer in time",
      not_signed_in: "not signed in",
      not_installed: "not installed",
      bad_reply: "answered in a way Khipu can't read",
      no_plan_limits: "no plan limits reported",
      invalid_account: "not a valid account entry",
      no_answer: "didn't answer",
      start_failed: "could not be started",
      cli_error: "reported an error when asked",
      internal_error: "Khipu could not run the check",
      pending: "first read still running",
    };
    for (const [code, phrase] of Object.entries(phrases)) expect(usageErrorPhrase(code)).toBe(phrase);
    expect(usageErrorPhrase("exit_3")).toBe("didn't answer");
    expect(usageErrorPhrase("signal_9")).toBe("didn't answer");
    expect(usageErrorPhrase("something_new")).toBe("couldn't read");
    expect(usageErrorPhrase(undefined)).toBe("couldn't read");
    status = {
      accounts: [...Object.keys(phrases), "exit_3"].map((code) => ({
        harness: "claude", account: `Acct ${code}`, plan: null, windows: [], ok: false, error: code,
        note: null, stale: false,
      })),
    };
    render(<Home />);
    // A row cached before every code had a note says the CLI's name and the
    // code's phrase; each row says it once.
    await waitFor(() => expect(screen.getByText("Can't read its plan: Claude Code didn't answer in time")).toBeTruthy());
    expect(within(rowFor("Acct not_signed_in")).getByText("Not signed in")).toBeTruthy();
    expect(within(rowFor("Acct no_plan_limits")).getByText("Its sign-in reports no plan limits")).toBeTruthy();
    expect(within(rowFor("Acct pending")).getByText("Reading it for the first time")).toBeTruthy();
    expect(within(rowFor("Acct internal_error")).getByText("Can't read its plan: Khipu could not run the check")).toBeTruthy();
    expect(within(rowFor("Acct exit_3")).getByText("Can't read its plan: Claude Code didn't answer")).toBeTruthy();
    for (const el of document.querySelectorAll(".plan-row")) expect(el.querySelectorAll(".plan-say")).toHaveLength(1);
    expect(screen.queryByText("exit_3")).toBeNull();
    expect(screen.queryByText(/couldn't read/)).toBeNull();
  });

  it("keeps the last figures of a failed probe with its phrase and their age", async () => {
    const r = realReport();
    Object.assign(r.accounts![1], {
      ok: false, error: "timeout", stale: true, probed_at: "2026-10-12T14:00:00Z",
      last_good_at: "2026-10-12T14:00:00Z", last_attempt_at: "2026-10-14T13:59:30Z",
    });
    r.accounts![3] = { ...r.accounts![3], plan: "pro", note: null, observed_at: "2026-10-14T12:00:00Z", stale: true,
      windows: [{ name: "five_hour", used_pct: 12, resets_at: "2026-10-14T15:00:00Z", window_mins: 300 }] };
    status = r;
    render(<Home />);
    await waitFor(() => expect(screen.getByText("40%")).toBeTruthy());
    const codex = within(rowFor("Codex"));
    expect(codex.getByText("Can't read it now; figures from 2 days ago")).toBeTruthy();
    expect(codex.getByRole("meter").classList.contains("dim")).toBe(true);
    expect(within(rowFor("Muse")).getByText("as of its last turn, 2 h ago")).toBeTruthy();
  });

  it("Refresh calls the refresh command, spins while it runs, then shows the new figures", async () => {
    render(<Home />);
    await waitFor(() => expect(screen.getByText("40%")).toBeTruthy());
    const button = screen.getByRole("button", { name: "Refresh" });
    fireEvent.click(button);
    await waitFor(() => expect(button.getAttribute("aria-busy")).toBe("true"));
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(button.querySelector(".spin")).toBeTruthy();
    expect(calls.find(([c]) => c === "khipu_usage_refresh")).toEqual(["khipu_usage_refresh", undefined]);
    await act(async () => release("khipu_usage_refresh"));
    await waitFor(() => expect(screen.getByText("94%")).toBeTruthy());
    expect(button.getAttribute("aria-busy")).toBeNull();
  });

  it("a load asked for during a refresh never replaces the refresh's figures", async () => {
    render(<Home />);
    await waitFor(() => expect(screen.getByText("40%")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(pending.khipu_usage_refresh).toHaveLength(1));
    // The Home header's Refresh, or coming back to Home, calls load().
    await act(async () => {
      void exposed!.load();
    });
    expect(usageCalls()).toEqual(["khipu_usage_status", "khipu_usage_refresh"]);
    await act(async () => release("khipu_usage_refresh"));
    await waitFor(() => expect(screen.getByText("94%")).toBeTruthy());
    expect(screen.queryByText("40%")).toBeNull();
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
    await waitFor(() => expect(screen.getByText("94%")).toBeTruthy());
    expect(usageCalls()).toEqual(["khipu_usage_status", "khipu_usage_refresh"]);
  });

  it("never shows the CLI's own failure text", async () => {
    const raw = "khipu exited 1: error: Could not determine home directory.";
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    invokeMock.mockImplementation(async () => {
      throw raw;
    });
    render(<Home />);
    await waitFor(() => expect(screen.getByText("Could not read plan usage")).toBeTruthy());
    expect(screen.getByText(PLAN_READ_FAILED)).toBeTruthy();
    expect(screen.queryByText(/khipu exited|home directory/)).toBeNull();
    expect(spy).toHaveBeenCalled();
    spy.mockRestore();
  });

  it("shows an empty state when no account is configured", async () => {
    status = { generated_at: "2026-10-14T13:57:29Z", max_age_s: 120, refreshing: false, accounts: [], summary_text: [] };
    render(<Home />);
    await waitFor(() => expect(screen.getByText("No accounts to read yet")).toBeTruthy());
    expect(screen.queryByRole("meter")).toBeNull();
  });

  it("a window that has reset shows no old figure and no bar", async () => {
    status = withMuseResetAt95();
    render(<Home />);
    await waitFor(() => expect(screen.getByText("Every limit has reset since it was read")).toBeTruthy());
    const muse = within(rowFor("Muse"));
    expect(muse.queryByRole("meter")).toBeNull();
    expect(muse.queryByText("95%")).toBeNull();
  });
});

describe("tightest window and the 90% alert", () => {
  it("picks the highest used percent per account and across accounts", () => {
    const r = realReport();
    expect(tightestWindow(r.accounts![0], NOW)?.name).toBe("weekly_fable");
    expect(tightestWindow(r.accounts![3], NOW)).toBeNull();
    expect(tightestOverall(r, NOW)?.row.account).toBe("Grok");
    expect(plansHealthDetail(r, NOW)).toBe(`Grok SuperGrok Heavy week at 43.5%, resets ${fmtResetLocal(GROK_RESET, NOW)}`);
    expect(planAttention(r, NOW)).toEqual([]);
  });

  it("raises one attention item and a red Plans row at 90%, in local time", () => {
    const r = withCodexAt(94);
    const reset = fmtResetLocal(CODEX_RESET, NOW)!;
    expect(reset).toBe(new Date(CODEX_RESET).toLocaleString([], LOCAL_WEEKDAY));
    expect(plansHealthDetail(r, NOW)).toBe(`Codex Pro week at 94%, resets ${reset}`);
    const items = planAttention(r, NOW);
    expect(items).toHaveLength(1);
    expect(items[0].key).toBe("plans");
    expect(items[0].cause).toContain(`Codex Pro week at 94%, resets ${reset}`);
    expect(items[0].cause).not.toMatch(/\d\d:\d\dZ/);
    expect(plansHealthRow(r, NOW)).toMatchObject({ key: "plans", status: "err", label: "Plans" });
    expect(plansHealthRow(withCodexAt(89.9), NOW)?.status).toBe("ok");
    expect(plansHealthRow({ accounts: [] }, NOW)?.status).toBe("skipped");
    expect(plansHealthRow(null, NOW)).toBeNull();
  });

  it("the callout and the card give the same reset for one window", async () => {
    status = withCodexAt(94);
    render(<Home />);
    await waitFor(() => expect(screen.getByText("94%")).toBeTruthy());
    const reset = fmtResetLocal(CODEX_RESET, NOW)!;
    expect(within(rowFor("Codex")).getByText(`week · resets ${reset}`)).toBeTruthy();
    expect(planAttention(status, NOW)[0].cause).toContain(`resets ${reset}.`);
  });

  it("a window whose reset has passed raises no alert and no red row", () => {
    const r = withMuseResetAt95();
    expect(planAttention(r, NOW)).toEqual([]);
    const row = plansHealthRow(r, NOW)!;
    expect(row.status).toBe("ok");
    expect(row.detail).not.toContain("Muse");
    expect(tightestWindow(r.accounts![3], NOW)).toBeNull();
    // Kept figures for a failed login that has not reset yet still alert, as last known.
    const kept = realReport();
    Object.assign(kept.accounts![0], { ok: false, error: "not_signed_in", stale: true });
    kept.accounts![0].windows![0].used_pct = 92;
    const items = planAttention(kept, NOW);
    expect(items).toHaveLength(1);
    expect(items[0].cause).toContain("Claude Max five-hour at 92% when last read, resets ");
  });

  it("judges the 75% and 90% lines on the figure it shows", () => {
    expect(fmtPct(89.96)).toBe("90%");
    expect(fmtPct(43.5)).toBe("43.5%");
    expect(fmtPct(90)).toBe("90%");
    expect(fmtPct(89.94)).toBe("89.9%");
    expect(meterTone(89.96)).toBe("err");
    expect(meterTone(74.96)).toBe("warn");
    expect(meterTone(74.94)).toBeNull();
    expect(planAttention(withCodexAt(89.96), NOW)).toHaveLength(1);
    expect(plansHealthRow(withCodexAt(89.96), NOW)?.status).toBe("err");
    expect(planAttention(withCodexAt(89.94), NOW)).toEqual([]);
    expect(plansHealthRow(withCodexAt(89.94), NOW)?.status).toBe("ok");
    expect(plansHealthRow(withCodexAt(90), NOW)?.status).toBe("err");
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

  it("puts the Plans row in the health report", () => {
    const { unmount } = render(<HealthReportRows parsed={{ hub_ok: true }} attention={[]} plans={realReport()} />);
    expect(screen.getByText("Plans")).toBeTruthy();
    expect(screen.getByText(`Grok SuperGrok Heavy week at 43.5%, resets ${fmtResetLocal(GROK_RESET, NOW)}`)).toBeTruthy();
    unmount();
    const hot = withCodexAt(94);
    render(<HealthReportRows parsed={{ hub_ok: true }} attention={planAttention(hot)} plans={hot} />);
    expect(screen.getByText(/Codex Pro week at 94%, resets .*\. Work sent/)).toBeTruthy();
  });

  it("is never green when nothing was read, and names accounts that failed", () => {
    const failed = (r: UsageReport, i: number, error: string) =>
      Object.assign(r.accounts![i], { ok: false, error, windows: [], stale: false });
    const all = realReport();
    all.accounts = all.accounts!.slice(0, 2);
    failed(all, 0, "not_signed_in");
    failed(all, 1, "timeout");
    expect(plansHealthRow(all, NOW)).toMatchObject({
      status: "err",
      detail: "could not read any plan: Claude (not signed in), Codex (didn't answer in time)",
    });
    const partial = realReport();
    failed(partial, 0, "not_signed_in");
    const row = plansHealthRow(partial, NOW)!;
    expect(row.status).toBe("ok");
    expect(row.note).toBe("could not read Claude (not signed in)");
    // Muse answered and reports no limits yet: that is evidence, not a failure.
    expect(plansHealthRow({ accounts: [realReport().accounts![3]] }, NOW)).toMatchObject({
      status: "ok", detail: "no plan limits reported",
    });
    const { unmount } = render(<HealthReportRows parsed={{ hub_ok: true }} attention={[]} plans={all} />);
    expect(screen.getByText("Could not read any plan: Claude (not signed in), Codex (didn't answer in time).")).toBeTruthy();
    expect(screen.queryByText(/see the raw report/)).toBeNull();
    unmount();
    render(<HealthReportRows parsed={{ hub_ok: true }} attention={[]} plans={partial} />);
    expect(screen.getByText(/; could not read Claude \(not signed in\)$/)).toBeTruthy();
  });
});

describe("PlansSettingsCard", () => {
  it("lists every window with its reset and when its figures were read, and probes on demand", async () => {
    render(<Settings />);
    const claude = await screen.findByRole("group", { name: "Claude" });
    const c = within(claude);
    for (const label of ["five-hour", "week", "Fable week"]) expect(c.getByText(label)).toBeTruthy();
    expect(c.getAllByText(/^resets /)).toHaveLength(3);
    expect(c.getByText(/^Live: read .* ago$/)).toBeTruthy();
    const muse = within(screen.getByRole("group", { name: "Muse" }));
    expect(muse.getByText(MUSE_NO_TURN)).toBeTruthy();
    expect(muse.queryByText(/Live/)).toBeNull();
    expect(muse.getByText(/^Checked .* ago$/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Check all now" }));
    await waitFor(() => expect(usageCalls()).toContain("khipu_usage_refresh"));
    await act(async () => release("khipu_usage_refresh"));
    await waitFor(() => expect(screen.getByText("94%")).toBeTruthy());
  });

  it("says it is reading while the first read runs, with Probe now held", async () => {
    holdStatus = true;
    render(<Settings />);
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("Reading plan usage…"));
    expect(screen.getByRole("button", { name: "Check all now" })).toBeDisabled();
    await act(async () => release("khipu_usage_status"));
    await screen.findByRole("group", { name: "Claude" });
    expect(screen.queryByText("Reading plan usage…")).toBeNull();
  });

  it("shows Khipu's note for a failed account and the age of kept figures", async () => {
    const r = realReport();
    r.accounts!.push(
      { harness: "claude", account: "Typo", ok: false, error: "invalid_account", windows: [],
        note: 'This account entry is not valid: Khipu does not read the CLI "cluade"; it reads Claude Code, Codex, Grok Build and Muse' },
      { harness: "claude", account: "Claude second", ok: false, error: "no_plan_limits", windows: [],
        note: "This sign-in reports no plan limits. Open Claude Code with this login folder and sign in again (/login)",
        last_attempt_at: "2026-10-14T13:59:00Z" },
    );
    Object.assign(r.accounts![1], {
      ok: false, error: "not_signed_in", stale: true, probed_at: "2026-10-12T14:00:00Z",
      last_good_at: "2026-10-12T14:00:00Z", last_attempt_at: "2026-10-14T13:59:30Z",
    });
    status = r;
    render(<Settings />);
    const typo = within(await screen.findByRole("group", { name: "Typo" }));
    expect(typo.getByText(/^Can't read its plan: this account entry is not valid: Khipu does not read the CLI "cluade"/)).toBeTruthy();
    expect(
      within(screen.getByRole("group", { name: "Claude second" })).getByText(
        /Open Claude Code with this login folder and sign in again \(\/login\)/,
      ),
    ).toBeTruthy();
    const codex = within(screen.getByRole("group", { name: "Codex" }));
    expect(codex.getByText("Codex is not signed in.")).toBeTruthy();
    expect(codex.getByText("Figures from 2 days ago")).toBeTruthy();
    expect(codex.getByRole("button", { name: "Sign Codex in again" })).toBeTruthy();
    const details = codex.getByRole("button", { name: "Details" });
    expect(details.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(details);
    expect(details.getAttribute("aria-expanded")).toBe("true");
    expect(codex.getByText("not_signed_in")).toBeTruthy();
    expect(codex.getByText(/, 30s ago$/)).toBeTruthy();
  });

  it("keeps the last figures and offers no sign-in when Claude sent no plan figures", async () => {
    const r = realReport();
    const claude = r.accounts!.find((a) => a.harness === "claude")!;
    r.accounts!.push({
      ...claude, account: "Claude second", home: "~/.claude-second", ok: false, error: "no_plan_figures",
      stale: true, last_good_at: claude.probed_at, last_attempt_at: claude.probed_at,
      note: "Claude Code did not send its plan figures just now; Khipu keeps the last ones and asks again",
    });
    status = r;
    render(<Settings />);
    const second = within(await screen.findByRole("group", { name: "Claude second" }));
    expect(second.getByText(/did not send its plan figures just now/)).toBeTruthy();
    expect(second.getAllByRole("meter").length).toBeGreaterThan(0);
    expect(second.queryByRole("button", { name: /Sign .* in again/ })).toBeNull();
    expect(second.queryByText(/sign in again|no plan limits/i)).toBeNull();
  });

  it("shows a reset that has passed as such, with no warning colour", async () => {
    status = withMuseResetAt95();
    render(<Settings />);
    const muse = within(await screen.findByRole("group", { name: "Muse" }));
    expect(muse.getByText(/^reset since /)).toBeTruthy();
    expect(muse.getByRole("meter").className).toBe("plan-meter");
  });

  it("adds, removes and resets the accounts read, then reads again", async () => {
    render(<Settings />);
    await screen.findByRole("group", { name: "Claude" });
    await waitFor(() => expect(screen.getByText("Default")).toBeTruthy());
    expect(screen.queryByText(/edit the file/)).toBeNull();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: " Claude second " } });
    fireEvent.change(screen.getByLabelText("Login folder"), { target: { value: " ~/.claude-second " } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() =>
      expect(calls).toContainEqual([
        "khipu_config_set",
        {
          key: "usage_accounts",
          value: JSON.stringify([
            { label: "Claude", harness: "claude" },
            { label: "Codex", harness: "codex" },
            { label: "Claude second", harness: "claude", home: "~/.claude-second" },
          ]),
          expect: "av1",
        },
      ]),
    );
    await waitFor(() => expect(usageCalls().filter((c) => c === "khipu_usage_status")).toHaveLength(2));

    configShow = {
      usage_accounts: {
        value: [{ label: "Claude", harness: "claude" }, { label: "Odd", harness: "gemini" }],
        source: "file",
        version: "av2",
        accounts: [
          { label: "Claude", harness: "claude", home: null },
          { label: "Odd", harness: "gemini", home: null, invalid: 'Khipu does not read the CLI "gemini"' },
        ],
      },
    };
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Grok work" } });
    fireEvent.change(screen.getByLabelText("CLI"), { target: { value: "grok" } });
    fireEvent.change(screen.getByLabelText("Login folder"), { target: { value: "~/.grok-work" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Add" }));
    });
    await waitFor(() => expect(screen.getByText("Your list")).toBeTruthy());
    expect(screen.getByText('not valid: Khipu does not read the CLI "gemini"')).toBeTruthy();
    calls.length = 0;
    fireEvent.click(screen.getByRole("button", { name: "Remove Claude" }));
    // The invalid entry was not touched, so it goes back as stored: only Khipu
    // decides what is valid.
    await waitFor(() =>
      expect(calls).toContainEqual([
        "khipu_config_set",
        { key: "usage_accounts", value: JSON.stringify([{ label: "Odd", harness: "gemini" }]), expect: "av2" },
      ]),
    );

    refuse = { error: "usage_accounts holds at most 32 entries, got 33", code: "too_many", index: null, field: null };
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    fireEvent.click(await screen.findByRole("button", { name: "Go back to every installed CLI" }));
    expect(await screen.findByText("Khipu reads at most 32 accounts. Remove one before adding another.")).toBeTruthy();
    expect(screen.queryByText(/usage_accounts/)).toBeNull();
    spy.mockRestore();
  });

  it("never shows the CLI's own text when saving the list fails to run", async () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(<Settings />);
    await waitFor(() => expect(screen.getByText("Default")).toBeTruthy());
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_config_set") throw "khipu exited 1: Traceback (most recent call last)";
      if (cmd === "khipu_config_show") return JSON.stringify(configShow);
      return JSON.stringify(status);
    });
    fireEvent.change(screen.getByLabelText("Login folder"), { target: { value: "~/.claude-second" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByText("Khipu could not save the account list. Try again in a minute.")).toBeTruthy();
    expect(screen.queryByText(/Traceback/)).toBeNull();
    spy.mockRestore();
  });
});

describe("adding an account", () => {
  const addButton = () => screen.getByRole("button", { name: "Add" });
  const configSets = () => calls.filter(([c]) => c === "khipu_config_set");

  it("names an account added with no Name after its CLI and login folder", async () => {
    render(<Settings />);
    await waitFor(() => expect(screen.getByText("Default")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("CLI"), { target: { value: "codex" } });
    fireEvent.change(screen.getByLabelText("Login folder"), { target: { value: "~/.codex-work" } });
    fireEvent.click(addButton());
    await waitFor(() =>
      expect(calls).toContainEqual([
        "khipu_config_set",
        {
          key: "usage_accounts",
          value: JSON.stringify([
            { label: "Claude", harness: "claude" },
            { label: "Codex", harness: "codex" },
            { label: "Codex (~/.codex-work)", harness: "codex", home: "~/.codex-work" },
          ]),
          expect: "av1",
        },
      ]),
    );
    expect(defaultAccountLabel("grok", null)).toBe("Grok Build");
    const long = defaultAccountLabel("grok", `/Volumes/Work/${"x".repeat(100)}/grok`);
    expect(long).toHaveLength(64);
    expect(long).toMatch(/^Grok Build \(…x+\/grok\)$/);
  });

  it("asks for a different Name or Login folder in plain words, before anything is saved", async () => {
    render(<Settings />);
    await waitFor(() => expect(screen.getByText("Default")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "codex" } });
    fireEvent.change(screen.getByLabelText("CLI"), { target: { value: "codex" } });
    fireEvent.change(screen.getByLabelText("Login folder"), { target: { value: "~/.codex-work" } });
    fireEvent.click(addButton());
    expect(await screen.findByText("An account is already named Codex. Type a different Name.")).toBeTruthy();
    const name = screen.getByLabelText("Name");
    expect(name.getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(name);
    expect(configSets()).toHaveLength(0);

    fireEvent.change(name, { target: { value: "Codex again" } });
    expect(name.getAttribute("aria-invalid")).toBeNull();
    fireEvent.change(screen.getByLabelText("Login folder"), { target: { value: "" } });
    fireEvent.click(addButton());
    expect(
      await screen.findByText("Codex with its default login is already listed as Codex. Choose a different Login folder."),
    ).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByLabelText("Login folder"));
    expect(configSets()).toHaveLength(0);
    expect(screen.queryByText(/usage_accounts/)).toBeNull();
    // Muse has one login: the message does not send the owner to a field they cannot use.
    expect(
      accountClash([{ label: "Muse", harness: "muse", home: null }], { label: "Muse two", harness: "muse", home: null })
        ?.message,
    ).toBe("Muse is already listed as Muse. Muse has one login, so it is read once.");
  });

  it("clears the form once the list is saved, while the new account is still being read", async () => {
    render(<Settings />);
    await screen.findByRole("group", { name: "Claude" });
    await waitFor(() => expect(screen.getByText("Default")).toBeTruthy());
    holdStatus = true;
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Claude second" } });
    fireEvent.change(screen.getByLabelText("Login folder"), { target: { value: "~/.claude-second" } });
    fireEvent.click(addButton());
    await waitFor(() => expect(configSets()).toHaveLength(1));
    // The re-read that probes the new account is still running...
    await waitFor(() => expect(pending.khipu_usage_status).toHaveLength(1));
    // ...and the form is already empty and ready, so a second press cannot repeat the entry.
    await waitFor(() => expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe(""));
    expect((screen.getByLabelText("Login folder") as HTMLInputElement).value).toBe("");
    expect((addButton() as HTMLButtonElement).disabled).toBe(false);
    release("khipu_usage_status");
    await waitFor(() => expect(exposed?.loading).toBe(false));
  });
});

describe("entries the editor did not change go back as stored (review round 4)", () => {
  const untouched = { harness: "claude", label: "Untouched", home: ".claude-legacy" };
  const sent = () => {
    const sets = calls.filter(([c]) => c === "khipu_config_set");
    return JSON.parse((sets[sets.length - 1][1] as { value: string }).value);
  };

  beforeEach(() => {
    configShow = {
      usage_accounts: {
        value: [{ label: " Codex ", harness: "codex" }, untouched],
        source: "file",
        version: "av2",
        accounts: [
          { label: "Codex", harness: "codex", home: null },
          { label: "Untouched", harness: "claude", home: ".claude-legacy", invalid: "relative folder" },
        ],
      },
    };
  });

  it("adding an account keeps an invalid entry and the others exactly as stored", async () => {
    render(<Settings />);
    await waitFor(() => expect(screen.getByText("Your list")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Client" } });
    fireEvent.change(screen.getByLabelText("Login folder"), { target: { value: "~/.claude-client" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(calls.some(([c]) => c === "khipu_config_set")).toBe(true));
    expect(sent()).toEqual([
      { label: " Codex ", harness: "codex" },
      untouched,
      { label: "Client", harness: "claude", home: "~/.claude-client" },
    ]);
  });

  it("removing one account keeps an invalid entry as stored", async () => {
    render(<Settings />);
    await waitFor(() => expect(screen.getByText("Your list")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Remove Codex" }));
    await waitFor(() => expect(calls.some(([c]) => c === "khipu_config_set")).toBe(true));
    expect(sent()).toEqual([untouched]);
  });
});

describe("the account-list empty state (review P2)", () => {
  it.each([
    ["missing", { value: null, source: "default", version: "av-empty-missing", accounts: [{ label: "Claude", harness: "claude", home: null }] }],
    ["empty", { value: [], source: "file", version: "av-empty-file", accounts: [] }],
  ])("keeps %s distinct from an explicit empty list", async (_case, usage_accounts) => {
    configShow = { usage_accounts };
    render(<Settings />);
    await screen.findByText(usage_accounts.source === "default" ? "Default" : "Your list");
    if (usage_accounts.source === "default") {
      expect(screen.queryByRole("button", { name: "Go back to every installed CLI" })).toBeNull();
    } else {
      expect(screen.getByRole("button", { name: "Go back to every installed CLI" })).toBeInTheDocument();
    }
  });

  it("removing the final explicitly configured account sets [] instead of unsetting it", async () => {
    configShow = {
      usage_accounts: {
        value: [{ label: "Claude work", harness: "claude", home: "~/.claude-work" }],
        source: "file", version: "av-empty-final",
        accounts: [{ label: "Claude work", harness: "claude", home: "~/.claude-work" }],
      },
    };
    render(<Settings />);
    await screen.findByText("Your list");
    fireEvent.click(screen.getByRole("button", { name: "Remove Claude work" }));
    await waitFor(() => expect(calls).toContainEqual([
      "khipu_config_set",
      { key: "usage_accounts", value: "[]", expect: "av-empty-final" },
    ]));
    expect(calls.some(([cmd]) => cmd === "khipu_config_unset")).toBe(false);
  });
});

describe("a refused account list, in plain words (fix round 3)", () => {
  const addButton = () => screen.getByRole("button", { name: "Add" });
  const configSets = () => calls.filter(([c]) => c === "khipu_config_set");
  let spy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    spy = vi.spyOn(console, "error").mockImplementation(() => {});
    configShow = {
      usage_accounts: {
        value: null, source: "default", user_home: "/Users/me", version: "av3",
        accounts: [
          { label: "Claude", harness: "claude", home: null },
          { label: "Codex", harness: "codex", home: null },
          { label: "Codex work", harness: "codex", home: "~/.codex-work" },
        ],
      },
    };
  });
  afterEach(() => spy.mockRestore());

  async function typeAccount(fields: { name?: string; cli?: string; folder?: string }) {
    render(<Settings />);
    await waitFor(() => expect(screen.getByText("Default")).toBeTruthy());
    if (fields.name != null) fireEvent.change(screen.getByLabelText("Name"), { target: { value: fields.name } });
    if (fields.cli != null) fireEvent.change(screen.getByLabelText("CLI"), { target: { value: fields.cli } });
    if (fields.folder != null) fireEvent.change(screen.getByLabelText("Login folder"), { target: { value: fields.folder } });
    fireEvent.click(addButton());
  }

  it("catches a Login folder with the slash after ~ forgotten before saving", async () => {
    await typeAccount({ cli: "codex", folder: "~.codex-work" });
    expect(
      await screen.findByText(
        "Start the Login folder with ~/ (for example ~/.codex-work), or give the full path starting with /.",
      ),
    ).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByLabelText("Login folder"));
    expect(configSets()).toHaveLength(0);
    expect(formProblem({ label: "x", harness: "codex", home: "codex-work" })?.field).toBe("home");
    expect(formProblem({ label: "x", harness: "codex", home: "~/.codex-work" })).toBeNull();
    expect(formProblem({ label: "x", harness: "codex", home: "/Volumes/Work/.codex" })).toBeNull();
  });

  it("compares a login after expanding ~, as khipu config does", async () => {
    await typeAccount({ name: "Work full path", cli: "codex", folder: "/Users/me/.codex-work/" });
    expect(
      await screen.findByText(
        "Codex with the login folder /Users/me/.codex-work/ is already listed as Codex work. Choose a different Login folder.",
      ),
    ).toBeTruthy();
    expect(configSets()).toHaveLength(0);
    expect(loginKey("~/.codex-work", "/Users/me/")).toBe("/Users/me/.codex-work");
    expect(loginKey("/Users/me//./.codex-work/", "/Users/me")).toBe("/Users/me/.codex-work");
    expect(loginKey(null, "/Users/me")).toBe("");
  });

  it("caps the Name at 64 characters and refuses a longer one before saving", async () => {
    await typeAccount({ name: "n".repeat(65), cli: "grok", folder: "~/.grok-work" });
    expect(screen.getByLabelText("Name").getAttribute("maxLength")).toBe("64");
    expect(await screen.findByText("That Name is too long: use 64 characters or fewer.")).toBeTruthy();
    expect(configSets()).toHaveLength(0);
  });

  it("says a CLI refusal in its own words, points at the field and names the account by its Name", async () => {
    // khipu config compares the expanded folder; with no user_home known the
    // app cannot, so the refusal arrives with a code, the entry and the field.
    (configShow.usage_accounts as Record<string, unknown>).user_home = null;
    refuse = {
      error: "usage_accounts entry 4 (Work full path) reads the same login as an earlier entry",
      code: "login_taken", index: 3, field: "home", other: 2,
    };
    await typeAccount({ name: "Work full path", cli: "codex", folder: "/Users/me/.codex-work" });
    expect(
      await screen.findByText(
        "Codex with the login folder /Users/me/.codex-work reads the same sign-in as Codex work. Choose a different Login folder.",
      ),
    ).toBeTruthy();
    const folder = screen.getByLabelText("Login folder");
    expect(folder.getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(folder);
    expect(screen.queryByText(/usage_accounts|entry 4|reads the same login/)).toBeNull();
  });

  it("never shows the text of a refusal it has no sentence for, or of the app's own check", async () => {
    refuse = { error: "usage_accounts entry 3: home '~nouser/x' names a user this Mac does not have" };
    await typeAccount({ cli: "codex", folder: "~/.codex-two" });
    expect(
      await screen.findByText("Khipu could not save the account list. Try again in a minute."),
    ).toBeTruthy();
    expect(screen.queryByText(/usage_accounts|home '/)).toBeNull();
    // The app's own command refuses before the CLI runs: an invoke error, not JSON.
    expect(
      accountsRefusal({ error: "usage_accounts entry 3: label is too long" }, [], 2).message,
    ).toBe("Khipu could not save the account list. Try again in a minute.");
    const sent = [{ label: "Claude", harness: "claude" }, { label: "Typo", harness: "claude", home: "~nouser/x" }];
    expect(
      accountsRefusal({ error: "x", refusal: { code: "no_such_folder", index: 1, field: "home" } }, sent, 1),
    ).toEqual({
      field: "home",
      message: "Khipu cannot find the login folder ~nouser/x. Start it with ~/ (for example ~/.codex-work), or give the full path starting with /.",
    });
    expect(accountsRefusal({ error: "x", refusal: { code: "name_taken", index: 1, field: "label", other: 0 } }, sent, null))
      .toEqual({ field: null, message: "Two accounts are named Typo. Remove one of them." });
  });
});

describe("the account cap and failures that are not about an entry (fix round 4)", () => {
  const configSets = () => calls.filter(([c]) => c === "khipu_config_set");
  const TOO_MANY = "Khipu reads at most 32 accounts. Remove one before adding another.";
  let spy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    spy = vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => spy.mockRestore());

  it("refuses a 33rd account before saving, saying to remove one", async () => {
    expect(ACCOUNTS_MAX).toBe(32);
    const accounts = Array.from({ length: 32 }, (_, i) => ({ label: `Claude ${i}`, harness: "claude", home: `~/.claude-${i}` }));
    configShow = { usage_accounts: { value: accounts, source: "file", user_home: "/Users/me", accounts } };
    render(<Settings />);
    await waitFor(() => expect(screen.getByText("Your list")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Codex work" } });
    fireEvent.change(screen.getByLabelText("CLI"), { target: { value: "codex" } });
    fireEvent.change(screen.getByLabelText("Login folder"), { target: { value: "~/.codex-work" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByText(TOO_MANY)).toBeTruthy();
    expect(configSets()).toHaveLength(0);
    expect(screen.getByLabelText("Name").getAttribute("aria-invalid")).toBeNull();
    expect(screen.getByLabelText("Login folder").getAttribute("aria-invalid")).toBeNull();
    // An entry marked not valid is stored as it is, so it counts too.
    const withInvalid = [...accounts.slice(1), { label: "Odd", harness: "gemini", home: null, invalid: "not read" }];
    configShow = { usage_accounts: { value: withInvalid, source: "file", user_home: "/Users/me", accounts: withInvalid, version: "v1" } };
    cleanup();
    render(<Settings />);
    await waitFor(() => expect(screen.getByText("Your list")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Codex work" } });
    fireEvent.change(screen.getByLabelText("CLI"), { target: { value: "codex" } });
    fireEvent.change(screen.getByLabelText("Login folder"), { target: { value: "~/.codex-work" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByText(TOO_MANY)).toBeTruthy();
    expect(configSets()).toHaveLength(0);
  });

  it("a list read with no version is never saved over: nothing is sent and it says so in plain words", async () => {
    const accounts = [{ label: "Claude", harness: "claude", home: null }];
    configShow = { usage_accounts: { value: accounts, source: "file", user_home: "/Users/me", accounts } };
    render(<Settings />);
    await waitFor(() => expect(screen.getByText("Your list")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Codex work" } });
    fireEvent.change(screen.getByLabelText("CLI"), { target: { value: "codex" } });
    fireEvent.change(screen.getByLabelText("Login folder"), { target: { value: "~/.codex-work" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByText(/The account list changed while you were working\. Khipu read it again/)).toBeTruthy();
    expect(configSets()).toHaveLength(0);
  });

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

describe("a Codex whose live read failed (fix round 3)", () => {
  function signedOutCodex(): UsageReport {
    const r = realReport();
    Object.assign(r.accounts![1], {
      ok: true, source: "rollout", observed_at: "2026-10-11T14:00:00Z", stale: true, live_error: "not_signed_in",
      note: "Codex is not signed in, so these figures are from its last turn. Open Codex and sign in again",
    });
    return r;
  }

  it("says not signed in beside its last-turn figures on Home and in Settings", async () => {
    status = signedOutCodex();
    const { unmount } = render(<Home />);
    await waitFor(() => expect(screen.getByText("40%")).toBeTruthy());
    const codex = within(rowFor("Codex"));
    expect(codex.getByText("Not signed in")).toBeTruthy();
    expect(rowFor("Codex").querySelectorAll(".plan-say")).toHaveLength(1);
    unmount();
    render(<Settings />);
    const group = within(await screen.findByRole("group", { name: "Codex" }));
    expect(group.getByText(/^Codex is not signed in, so these figures are from its last turn. Open Codex and sign in again/)).toBeTruthy();
    // The note already says the figures are from its last turn: said once.
    expect(group.queryByText(/^As of its last turn/)).toBeNull();
    expect(group.getAllByText(/last turn/)).toHaveLength(1);
  });

  it("labels figures from Codex's session-file fallback as from its last conversation in Settings", async () => {
    const r = realReport();
    Object.assign(r.accounts![1], {
      ok: true, source: "rollout", observed_at: "2026-10-11T14:00:00Z", live_error: "no_answer",
      note: "Codex did not answer just now; these figures are from its last turn",
    });
    status = r;
    render(<Settings />);
    const group = within(await screen.findByRole("group", { name: "Codex" }));
    expect(group.getByText(/^From its last conversation/)).toBeTruthy();
    expect(group.queryByText(/^As of its last turn/)).toBeNull();
  });

  it("names it in the health report's Plans row", () => {
    const r = signedOutCodex();
    expect(liveReadFailures(r)).toEqual(["Codex is showing its last turn (not signed in)"]);
    const row = plansHealthRow(r, NOW)!;
    expect(row.status).toBe("ok");
    expect(row.note).toBe("Codex is showing its last turn (not signed in)");
    expect(liveReadFailures(realReport())).toEqual([]);
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
  const PAUSED_UNTIL = "2026-10-21T21:26:00Z";
  const pausedGrok: RouteView = {
    accounts: [{ key: "grok|", label: "Grok", harness: "grok", state: "paused", until: PAUSED_UNTIL }],
  };

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

  function HomeWith({ routing }: { routing: RouteView | null }) {
    const state = usePlanUsage(true);
    return <PlansCard state={state} routing={routing} />;
  }

  it("renders each row of the table with exactly one status phrase", async () => {
    status = tableReport();
    render(<HomeWith routing={null} />);
    await waitFor(() => expect(screen.getByText(MUSE_NO_TURN)).toBeTruthy());
    const said = (name: string) => [...rowFor(name).querySelectorAll(".plan-say")].map((n) => n.textContent);
    expect(said("Claude")).toEqual([]);
    expect(within(rowFor("Claude")).getByRole("meter")).toBeTruthy();
    expect(said("Kept")).toEqual(["Can't read it now; figures from 2 days ago"]);
    expect(within(rowFor("Kept")).getByRole("meter").className).toBe("plan-meter dim");
    expect(said("Grok")).toEqual(["Can't read its plan: Grok Build didn't answer"]);
    expect(within(rowFor("Grok")).queryByText(/Sign in/)).toBeNull();
    expect(said("Signed out")).toEqual(["Not signed in"]);
    expect(within(rowFor("Signed out")).getByRole("button", { name: "Sign Signed out in again" })).toBeTruthy();
    expect(said("Muse")).toEqual([MUSE_NO_TURN]);
    expect(said("Muse two")).toEqual(["as of its last turn, 2 h ago"]);
    expect(screen.queryByText(/Live/)).toBeNull();
    expect(screen.queryByText(/The last read failed/)).toBeNull();
  });

  it("shows the owner's pause first, from the routing record", async () => {
    status = tableReport();
    render(<HomeWith routing={pausedGrok} />);
    const pause = planPauses(pausedGrok, new Date()).get("grok|")!;
    expect(pause).toMatch(/^Paused by you until /);
    await waitFor(() => expect(within(rowFor("Grok")).getByText(pause)).toBeTruthy());
    const tail = rowFor("Grok").querySelector(".plan-none")!;
    expect(tail.firstElementChild!.textContent).toBe(pause);
    expect(tail.querySelectorAll(".plan-say")).toHaveLength(1);
    expect(within(rowFor("Claude")).queryByText(/Paused/)).toBeNull();
  });

  it("shows no pause when the routing record could not be read", async () => {
    status = tableReport();
    render(<HomeWith routing={null} />);
    await waitFor(() => expect(rowFor("Grok")).toBeTruthy());
    expect(screen.queryByText(/Paused by you/)).toBeNull();
    expect(planPauses(null).size).toBe(0);
  });

  it("says in the card's head when the whole last read failed, with no loose footer", async () => {
    render(<Home />);
    await waitFor(() => expect(screen.getByText("40%")).toBeTruthy());
    invokeMock.mockImplementation(async () => {
      throw "khipu exited 1";
    });
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    await act(async () => {
      await exposed!.reread();
    });
    expect(screen.getByText(/^Last read failed .*; showing earlier figures$/)).toBeTruthy();
    expect(screen.getByText("40%")).toBeTruthy();
    expect(screen.queryByText(/these figures are from the read before it/)).toBeNull();
    spy.mockRestore();
  });

  it("keeps every line about an account inside its block in Settings, the pause first", async () => {
    status = tableReport();
    function SettingsWith() {
      const state = usePlanUsage("settings");
      return <PlansSettingsCard state={state} active routing={pausedGrok} />;
    }
    const { container } = render(<SettingsWith />);
    const grok = await screen.findByRole("group", { name: "Grok" });
    const head = grok.querySelector("h2")!;
    expect(head.textContent).toBe("GrokSuperGrok Heavy plan");
    expect(head.nextElementSibling!.className).toBe("plan-paused");
    expect(within(grok).getByText("Can't read its plan: Grok Build didn't answer. No figures to show.")).toBeTruthy();
    expect(grok.className).toBe("plan-block warn");
    const outside = (sel: string) => [...container.querySelectorAll(sel)].filter((n) => !n.closest(".plan-block"));
    for (const sel of ['[role="meter"]', ".plan-fail", ".plan-reads", ".plan-paused", ".plan-empty", ".plan-details"]) {
      expect(outside(sel)).toEqual([]);
    }
    expect(outside("button").map((b) => b.textContent)).toEqual(
      expect.not.arrayContaining(["Details", "Sign in again"]),
    );
    expect(container.querySelectorAll(".plan-block")).toHaveLength(6);
    expect(container.querySelector(".plan-account")).toBeNull();
  });

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
  function reused(): UsageReport {
    const r = realReport();
    return {
      ...r,
      accounts: [
        { ...r.accounts![1], account: "Work", key: "codex|" },
        { ...r.accounts![0], account: "Claude second", home: "~/.claude-second", key: "claude|/logins/second" },
      ],
    };
  }

  it("never gives a pause to another account that carries the paused one's name", async () => {
    status = reused();
    function Both() {
      const state = usePlanUsage("settings");
      return (
        <>
          <PlansCard state={state} routing={record} />
          <PlansSettingsCard state={state} active routing={record} />
        </>
      );
    }
    render(<Both />);
    const work = await screen.findByRole("group", { name: "Work" });
    expect(within(work).queryByText(/Paused by you/)).toBeNull();
    expect(within(rowFor("Work")).queryByText(/Paused by you/)).toBeNull();
    // Matched by key, the renamed account keeps its own pause.
    expect(within(screen.getByRole("group", { name: "Claude second" })).getByText("Paused by you until you clear it")).toBeTruthy();
    expect(within(rowFor("Claude second")).getByText("Paused by you until you clear it")).toBeTruthy();
  });

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
