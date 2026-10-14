// Plan usage in the app (docs/plans/2026-10-14-plan-usage.md, phase 2): the
// Home card, the Settings card, the health report's Plans row and the
// attention item at 90%. The fixture is `khipu usage --json` as the CLI prints
// it, built from the recorded replies in packages/cli/tests/fixtures/usage/,
// with reset times moved to after the faked "now" (only Date is faked, so
// waitFor still runs on real timers).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  PLAN_READ_FAILED,
  PlansCard,
  PlansSettingsCard,
  fmtPct,
  fmtResetLocal,
  meterTone,
  planAttention,
  plansHealthDetail,
  resetPhrase,
  tightestOverall,
  tightestWindow,
  usageErrorPhrase,
  usePlanUsage,
} from "../PlansCard";
import type { PlanUsageState, UsageAccount, UsageReport } from "../PlansCard";
import { HealthReportRows, plansHealthRow } from "../healthRows";

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
        harness: "claude", account: "Claude", home: null, plan: "max",
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
        harness: "codex", account: "Codex", home: null, plan: "pro",
        windows: [
          { name: "weekly", used_pct: 40, resets_at: CODEX_RESET, window_mins: 10080, slot: "primary" },
        ],
        credits: { balance: 1234.5, has_credits: true, unlimited: false }, version: "0.162.1", source: "live",
        observed_at: null, error: null, note: null, ok: true, probed_at: "2026-10-14T13:57:29Z", ms: 0,
        last_attempt_at: "2026-10-14T13:57:29Z", last_error: null, last_good_at: "2026-10-14T13:57:29Z",
        cached: false, stale: false,
      },
      {
        harness: "grok", account: "Grok", home: null, plan: "SuperGrok Heavy",
        windows: [{ name: "weekly", used_pct: 43.5, resets_at: GROK_RESET }],
        credits: { prepaid_balance: 44.0 }, version: "1.0.52", source: "live", observed_at: null,
        error: null, note: null, ok: true, probed_at: "2026-10-14T13:57:29Z", ms: 0,
        last_attempt_at: "2026-10-14T13:57:29Z", last_error: null, last_good_at: "2026-10-14T13:57:29Z",
        cached: false, stale: false,
      },
      {
        harness: "muse", account: "Muse", home: null, plan: null, windows: [], credits: null,
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
let refuse: string | null;
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
      value: null, source: "default",
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
      return JSON.stringify(refuse ? { ok: false, error: refuse } : { ok: true });
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

  it("shows Muse's note when no turn has been observed, not 0%", async () => {
    render(<Home />);
    await waitFor(() => expect(screen.getByText("no turn observed yet")).toBeTruthy());
    const muse = within(rowFor("Muse"));
    expect(muse.queryByRole("meter")).toBeNull();
    expect(muse.queryByText(/0%/)).toBeNull();
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
      bad_reply: "unreadable reply",
      no_plan_limits: "no plan limits reported",
      invalid_account: "not a valid account entry",
      no_answer: "stopped without answering",
      start_failed: "could not be started",
      cli_error: "reported an error",
      internal_error: "Khipu could not run the check",
      pending: "first read still running",
    };
    for (const [code, phrase] of Object.entries(phrases)) expect(usageErrorPhrase(code)).toBe(phrase);
    expect(usageErrorPhrase("exit_3")).toBe("stopped without answering");
    expect(usageErrorPhrase("signal_9")).toBe("stopped without answering");
    expect(usageErrorPhrase("something_new")).toBe("couldn't read");
    expect(usageErrorPhrase(undefined)).toBe("couldn't read");
    status = {
      accounts: [...Object.keys(phrases), "exit_3"].map((code) => ({
        harness: "claude", account: `Acct ${code}`, plan: null, windows: [], ok: false, error: code,
        note: null, stale: false,
      })),
    };
    render(<Home />);
    await waitFor(() => expect(screen.getByText("didn't answer in time")).toBeTruthy());
    for (const phrase of Object.values(phrases)) expect(screen.getAllByText(phrase).length).toBeGreaterThan(0);
    expect(screen.queryByText("exit_3")).toBeNull();
    expect(screen.queryByText("couldn't read")).toBeNull();
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
    expect(codex.getByText("didn't answer in time")).toBeTruthy();
    expect(codex.getByText("figures from 2 days ago")).toBeTruthy();
    expect(within(rowFor("Muse")).getByText("as of the last turn, 2 h ago")).toBeTruthy();
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
    await waitFor(() => expect(screen.getByText("No plans to show")).toBeTruthy());
    expect(screen.queryByRole("meter")).toBeNull();
  });

  it("a window that has reset shows no old figure and no bar", async () => {
    status = withMuseResetAt95();
    render(<Home />);
    await waitFor(() => expect(screen.getByText("every limit has reset since it was read")).toBeTruthy());
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
    expect(c.getByText(/figures from .* ago/)).toBeTruthy();
    expect(within(screen.getByRole("group", { name: "Muse" })).getByText("no turn observed yet")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Probe now" }));
    await waitFor(() => expect(usageCalls()).toContain("khipu_usage_refresh"));
    await act(async () => release("khipu_usage_refresh"));
    await waitFor(() => expect(screen.getByText("94%")).toBeTruthy());
  });

  it("says it is reading while the first read runs, with Probe now held", async () => {
    holdStatus = true;
    render(<Settings />);
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("Reading plan usage…"));
    expect(screen.getByRole("button", { name: "Probe now" })).toBeDisabled();
    await act(async () => release("khipu_usage_status"));
    await screen.findByRole("group", { name: "Claude" });
    expect(screen.queryByText("Reading plan usage…")).toBeNull();
  });

  it("shows Khipu's note for a failed account and the age of kept figures", async () => {
    const r = realReport();
    r.accounts!.push(
      { harness: "claude", account: "Typo", ok: false, error: "invalid_account", windows: [],
        note: "invalid usage_accounts entry: unknown harness 'cluade'; one of claude, codex, grok, muse" },
      { harness: "claude", account: "Claude second", ok: false, error: "no_plan_limits", windows: [],
        note: "this login reports no plan limits (a login made with a setup token answers none); run /login in this home",
        last_attempt_at: "2026-10-14T13:59:00Z" },
    );
    Object.assign(r.accounts![1], {
      ok: false, error: "not_signed_in", stale: true, probed_at: "2026-10-12T14:00:00Z",
      last_good_at: "2026-10-12T14:00:00Z", last_attempt_at: "2026-10-14T13:59:30Z",
    });
    status = r;
    render(<Settings />);
    const typo = within(await screen.findByRole("group", { name: "Typo" }));
    expect(typo.getByText("not a valid account entry")).toBeTruthy();
    expect(typo.getByText(/unknown harness 'cluade'/)).toBeTruthy();
    expect(within(screen.getByRole("group", { name: "Claude second" })).getByText(/run \/login in this home/)).toBeTruthy();
    const codex = within(screen.getByRole("group", { name: "Codex" }));
    expect(codex.getByText(/figures from 2 days ago · last tried 30s ago/)).toBeTruthy();
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
        },
      ]),
    );
    await waitFor(() => expect(usageCalls().filter((c) => c === "khipu_usage_status")).toHaveLength(2));

    configShow = {
      usage_accounts: {
        value: [{ label: "Claude", harness: "claude" }, { label: "Odd", harness: "gemini" }],
        source: "file",
        accounts: [
          { label: "Claude", harness: "claude", home: null },
          { label: "Odd", harness: "gemini", home: null, invalid: "unknown harness 'gemini'" },
        ],
      },
    };
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Add" }));
    });
    await waitFor(() => expect(screen.getByText("Your list")).toBeTruthy());
    expect(screen.getByText("not valid: unknown harness 'gemini'")).toBeTruthy();
    calls.length = 0;
    fireEvent.click(screen.getByRole("button", { name: "Remove Claude" }));
    // Nothing valid is left, so the list goes back to the default.
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_unset", { key: "usage_accounts" }]));

    refuse = "usage_accounts entry 1: home '~mat/.x' names a user this Mac does not have";
    fireEvent.click(await screen.findByRole("button", { name: "Go back to every installed CLI" }));
    expect(await screen.findByText(refuse)).toBeTruthy();
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
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByText("Khipu could not save the account list. Try again in a minute.")).toBeTruthy();
    expect(screen.queryByText(/Traceback/)).toBeNull();
    spy.mockRestore();
  });
});
