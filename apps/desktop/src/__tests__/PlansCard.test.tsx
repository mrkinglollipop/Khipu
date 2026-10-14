// Plan usage in the app (docs/plans/2026-10-14-plan-usage.md, phase 2): the
// Home card, the Settings card, the health report's Plans row and the
// attention item at 90%. The fixture is `khipu usage --json` as the CLI prints
// it, built from the recorded replies in packages/cli/tests/fixtures/usage/.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  PlansCard,
  PlansSettingsCard,
  planAttention,
  plansHealthDetail,
  tightestOverall,
  tightestWindow,
  usageErrorPhrase,
  usePlanUsage,
} from "../PlansCard";
import type { UsageAccount, UsageReport } from "../PlansCard";
import { HealthReportRows, plansHealthRow } from "../healthRows";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

function realReport(): UsageReport {
  return {
    generated_at: "2026-10-14T13:57:29Z",
    max_age_s: 120,
    refreshing: false,
    accounts: [
      {
        harness: "claude", account: "Claude", home: null, plan: "max",
        windows: [
          { name: "five_hour", used_pct: 16, resets_at: "2026-01-01T12:00:00Z", window_mins: 300 },
          { name: "weekly", used_pct: 17, resets_at: "2026-01-02T12:00:00Z", window_mins: 10080 },
          { name: "weekly_fable", used_pct: 34, resets_at: "2026-01-09T12:00:00Z", window_mins: 10080, model: "Fable" },
        ],
        credits: { extra_usage_enabled: false }, version: "2.1.296", source: "live", observed_at: null,
        error: null, note: null, ok: true, probed_at: "2026-10-14T13:57:29Z", ms: 0,
        last_attempt_at: "2026-10-14T13:57:29Z", last_error: null, last_good_at: "2026-10-14T13:57:29Z",
        cached: false, stale: false,
      },
      {
        harness: "codex", account: "Codex", home: null, plan: "pro",
        windows: [
          { name: "weekly", used_pct: 40, resets_at: "2026-01-01T02:00:00Z", window_mins: 10080, slot: "primary" },
        ],
        credits: { balance: 1234.5, has_credits: true, unlimited: false }, version: "0.162.1", source: "live",
        observed_at: null, error: null, note: null, ok: true, probed_at: "2026-10-14T13:57:29Z", ms: 0,
        last_attempt_at: "2026-10-14T13:57:29Z", last_error: null, last_good_at: "2026-10-14T13:57:29Z",
        cached: false, stale: false,
      },
      {
        harness: "grok", account: "Grok", home: null, plan: "SuperGrok Heavy",
        windows: [{ name: "weekly", used_pct: 43.5, resets_at: "2026-01-11T12:00:00Z" }],
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
    summary_text: [
      "Claude (max): 5 h 16%, week 17%, week Fable 34%; tightest week Fable 34% resets Fri 12:00Z",
      "Codex (pro): week 40%; tightest week 40% resets Thu 02:00Z, credits 1234.5",
      "Grok (SuperGrok Heavy): week 43.5%; tightest week 43.5% resets Sun 12:00Z, prepaid 44",
      "Muse: no turn observed yet",
    ],
  };
}

function withCodexAt(pct: number): UsageReport {
  const r = realReport();
  r.accounts![1].windows![0].used_pct = pct;
  return r;
}

let status: UsageReport;
let fresh: UsageReport;
let releaseRefresh: (() => void) | null;
const calls: Array<[string, unknown]> = [];

beforeEach(() => {
  calls.length = 0;
  status = realReport();
  fresh = withCodexAt(94);
  releaseRefresh = null;
  invokeMock.mockImplementation(async (cmd: string, args?: unknown) => {
    calls.push([cmd, args]);
    if (cmd === "khipu_usage_status") return JSON.stringify(status);
    if (cmd === "khipu_usage_refresh") {
      await new Promise<void>((resolve) => {
        releaseRefresh = resolve;
      });
      return JSON.stringify(fresh);
    }
    throw new Error(`unexpected command ${cmd}`);
  });
});

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
});

function Home() {
  const state = usePlanUsage(true);
  return <PlansCard state={state} />;
}

function rowFor(name: string): HTMLElement {
  const el = screen.getAllByText(name).map((n) => n.closest(".plan-row")).find(Boolean);
  if (!el) throw new Error(`no row for ${name}`);
  return el as HTMLElement;
}

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
    expect(within(rowFor("Codex")).getByText(/week · resets /)).toBeTruthy();
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

  it("maps every fixed error code to a short phrase", async () => {
    const codes = ["timeout", "not_signed_in", "not_installed", "bad_reply", "no_plan_limits", "exit_3"];
    status = {
      accounts: codes.map((code) => ({
        harness: "claude", account: `Acct ${code}`, plan: null, windows: [], ok: false, error: code,
        note: null, stale: false,
      })),
    };
    render(<Home />);
    await waitFor(() => expect(screen.getByText("didn't answer in time")).toBeTruthy());
    for (const phrase of ["not signed in", "not installed", "unreadable reply", "no plan limits reported", "couldn't read"]) {
      expect(screen.getByText(phrase)).toBeTruthy();
    }
    expect(screen.queryByText("exit_3")).toBeNull();
    expect(usageErrorPhrase("cli_error")).toBe("couldn't read");
    expect(usageErrorPhrase(undefined)).toBe("couldn't read");
  });

  it("keeps the last figures of a failed probe with its phrase and the stale tag", async () => {
    const r = realReport();
    Object.assign(r.accounts![1], { ok: false, error: "timeout", stale: true });
    r.accounts![3] = { ...r.accounts![3], plan: "pro", note: null, observed_at: "2026-10-14T12:00:00Z",
      windows: [{ name: "five_hour", used_pct: 12, resets_at: "2026-10-14T15:00:00Z", window_mins: 300 }] };
    status = r;
    render(<Home />);
    await waitFor(() => expect(screen.getByText("40%")).toBeTruthy());
    const codex = within(rowFor("Codex"));
    expect(codex.getByText("didn't answer in time")).toBeTruthy();
    expect(codex.getByText("stale")).toBeTruthy();
    expect(within(rowFor("Muse")).getByText("as of the last turn")).toBeTruthy();
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
    releaseRefresh?.();
    await waitFor(() => expect(screen.getByText("94%")).toBeTruthy());
    expect(button.getAttribute("aria-busy")).toBeNull();
  });

  it("shows an empty state when no account is configured", async () => {
    status = { generated_at: "2026-10-14T13:57:29Z", max_age_s: 120, refreshing: false, accounts: [], summary_text: [] };
    render(<Home />);
    await waitFor(() => expect(screen.getByText("No plans to show")).toBeTruthy());
    expect(screen.queryByRole("meter")).toBeNull();
  });
});

describe("tightest window and the 90% alert", () => {
  it("picks the highest used percent per account and across accounts", () => {
    const r = realReport();
    expect(tightestWindow(r.accounts![0])?.name).toBe("weekly_fable");
    expect(tightestWindow(r.accounts![3])).toBeNull();
    expect(tightestOverall(r)?.row.account).toBe("Grok");
    expect(plansHealthDetail(r)).toBe("Grok SuperGrok Heavy week at 43.5%, resets Sun 12:00Z");
    expect(planAttention(r)).toEqual([]);
  });

  it("raises one attention item and a red Plans row at 90%", () => {
    const r = withCodexAt(94);
    expect(plansHealthDetail(r)).toBe("Codex Pro week at 94%, resets Thu 02:00Z");
    const items = planAttention(r);
    expect(items).toHaveLength(1);
    expect(items[0].key).toBe("plans");
    expect(items[0].cause).toContain("Codex Pro week at 94%, resets Thu 02:00Z");
    expect(plansHealthRow(r)).toMatchObject({ key: "plans", status: "err", label: "Plans" });
    expect(plansHealthRow(withCodexAt(89.9))?.status).toBe("ok");
    expect(plansHealthRow({ accounts: [] })?.status).toBe("skipped");
    expect(plansHealthRow(null)).toBeNull();
  });

  it("puts the Plans row in the health report", () => {
    const { unmount } = render(<HealthReportRows parsed={{ hub_ok: true }} attention={[]} plans={realReport()} />);
    expect(screen.getByText("Plans")).toBeTruthy();
    expect(screen.getByText("Grok SuperGrok Heavy week at 43.5%, resets Sun 12:00Z")).toBeTruthy();
    unmount();
    const hot = withCodexAt(94);
    render(<HealthReportRows parsed={{ hub_ok: true }} attention={planAttention(hot)} plans={hot} />);
    expect(screen.getByText(/Codex Pro week at 94%, resets Thu 02:00Z\. Work sent/)).toBeTruthy();
  });
});

describe("PlansSettingsCard", () => {
  it("lists every window with its reset and the probe time, and probes on demand", async () => {
    render(<PlansSettingsCard active />);
    const claude = await screen.findByRole("group", { name: "Claude" });
    const c = within(claude);
    for (const label of ["five-hour", "week", "Fable week"]) expect(c.getByText(label)).toBeTruthy();
    expect(c.getAllByText(/^resets /)).toHaveLength(3);
    expect(c.getByText(/read .* ago/)).toBeTruthy();
    expect(within(screen.getByRole("group", { name: "Muse" })).getByText("no turn observed yet")).toBeTruthy();
    expect(screen.getByText("usage_accounts")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Probe now" }));
    await waitFor(() => expect(calls.map(([cmd]) => cmd)).toContain("khipu_usage_refresh"));
    releaseRefresh?.();
    await waitFor(() => expect(screen.getByText("94%")).toBeTruthy());
  });
});
