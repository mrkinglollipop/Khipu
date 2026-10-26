// Clickable Home: Plans rows open a Plan detail, Routing rows and priority
// names open a Routing detail (two different views), tiles and callouts open
// their health rows, and the plain-words helpers behind the rewording. Every
// fixture value is made up; times are New York local.
(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { PlansCard } from "../PlansCard";
import type { PlanUsageState, UsageAccount, UsageReport } from "../PlansCard";
import { RoutingCard } from "../RoutingCard";
import type { RouteView, RoutingState } from "../routing";
import { paceWords } from "../routing";
import { Tile } from "../ui";
import { buildAttention } from "../doctorAttention";
import { TILE_HEALTH_KEYS, healthKeysForAttention } from "../healthRows";
import { activityLines, capRow, cleanWhy, nearlyGoneWords, planDetail, routingDetail } from "../accountDetailModel";
import { captureCauseWords, longSpan, planWord, plainError, toastWords, waitedWords } from "../humanWords";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

// Mon 2026-03-09, 3:00 PM in New York.
const NOW = new Date("2026-03-09T19:00:00Z");
const READ_AT = "2026-03-09T18:58:00Z";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
  vi.useRealTimers();
});

function usage(): UsageReport {
  const base = { harness: "claude", home: null, ok: true, error: null, note: null, stale: false, cached: false,
    probed_at: READ_AT, last_attempt_at: READ_AT, last_good_at: READ_AT, last_error: null, ms: 1100, source: "live" };
  return {
    generated_at: READ_AT,
    accounts: [
      { ...base, key: "claude|", account: "Alpha", plan: "max",
        windows: [
          { name: "five_hour", used_pct: 8, resets_at: "2026-03-09T23:00:00Z" },
          { name: "weekly", used_pct: 80, resets_at: "2026-03-10T14:59:00Z" },
        ] },
      { ...base, key: "glm|", harness: "glm", account: "Gamma", plan: "lite",
        windows: [{ name: "weekly", used_pct: 96, resets_at: "2026-03-10T00:36:00Z" }] },
      { ...base, key: "muse|", harness: "muse", account: "Delta", plan: "40000000000000002", source: "turn",
        observed_at: READ_AT, windows: [{ name: "weekly", used_pct: 2, resets_at: "2026-03-15T23:00:00Z" }] },
    ] as UsageAccount[],
  };
}

function route(): RouteView {
  return {
    record: true, version: 4, checked_at: NOW.toISOString(),
    accounts: [
      { key: "claude|", label: "Alpha", harness: "claude", state: "open", configured: true,
        witness: { source: "probe", at: READ_AT },
        pace: [{ window: "weekly", trailing_h: 24, rate_pct_per_h: 0.3, plan: { verdict: "underpacing", unused_at_reset_pct: 16 } }],
        cap_statuses: [
          { kind: "pace_target", purpose: "job", slack: 0, allowance_pct: 11.1, used_pct: 37, confirmed_at: "2026-03-02T15:00:00Z" },
          { kind: "spend", cap_usd: 50, days: 7, usd: 4.2 },
        ],
        advice: { verdict: "hold_back", caps: [{ kind: "pace_target", why: "26 points ahead of even plus 0. Clears in 1 d 21 h." }] },
        lead_advice: { verdict: "go" } },
      { key: "glm|", label: "Gamma", harness: "glm", state: "open", configured: true, witness: { source: "probe", at: READ_AT } },
    ],
    holds: [], rules: [], note: null,
    changes: [
      { version: 3, at: "2026-03-06T18:07:00Z", entities: ["account:claude|"], items: [{ entity: "account:claude|", now: { state: "limited", until: "2026-03-12T18:09:00Z" }, by: "read from Alpha" }] },
      { version: 4, at: "2026-03-06T18:42:00Z", entities: ["account:claude|"], items: [{ entity: "account:claude|", now: { state: "open" } }] },
    ],
    rule_checks: [], warn_after: { disagreement: 1800, not_refreshed: 300 }, report_default_limit: 1800,
    priority: {
      entries: [
        { account: "claude|", label: "Alpha", role: "First choice for builds.", until: null, in_settings: true,
          exceptions: [{ text: "Reviews may run 10 points ahead of an even pace.", until: "2026-03-10T20:30:00Z", at: "2026-03-08T15:00:00Z" }] },
        { account: "glm|", label: "Gamma", role: "Overflow only.", until: null, in_settings: true },
      ],
      pending: false, confirmed: true, version: "p1",
    },
    plan_facts: { "claude|": { readable: true, window: "week", used_pct: 80, left_pct: 20, resets_at: "2026-03-10T14:59:00Z", seconds: 70000, pace_pct_per_hour: 1.4, hurry: true, words: "a lot left, little time" } },
  };
}

const plansState = (report: UsageReport): PlanUsageState =>
  ({ data: report, error: null, errorAt: null, loading: false, refreshing: false, refresh: vi.fn(), reread: vi.fn() }) as unknown as PlanUsageState;

const routingState = (data: RouteView, load = vi.fn(async () => undefined)): RoutingState =>
  ({ data, error: null, loading: false, lastOkAt: NOW.toISOString(), firstRead: false, load }) as unknown as RoutingState;

describe("plain words", () => {
  it("never prints an id or a lower-case word as a plan", () => {
    expect(planWord("max")).toBe("Max");
    expect(planWord("40000000000000002")).toBe("");
    expect(planWord(null)).toBe("");
  });

  it("says nothing about a wait it has no number for, instead of 0 minutes", () => {
    expect(waitedWords(undefined)).toBe("");
    expect(waitedWords(0)).toBe("");
    expect(waitedWords(1725)).toBe("1 day 5 hours");
    expect(longSpan(45 * 3600)).toBe("1 day 21 hours");
    const out = buildAttention({ topics_embed_lag_ok: false, topics_embed_lag: {} });
    const item = out.items.find((i) => i.key === "topics_embed_lag")!;
    expect(item.title).toBe("A note is waiting to become findable by meaning");
    expect(item.cause).not.toMatch(/0 minutes|waited/);
    const slow = buildAttention({ topics_embed_lag_ok: false, topics_embed_lag: { lag_minutes: 90 } });
    expect(slow.items[0].cause).toContain("The oldest one has waited 90 minutes.");
  });

  it("turns raw errors and toasts into what failed", () => {
    expect(toastWords("khipu exited 1: error: connection timeout expired")).toBe(
      "Khipu couldn't reach the database just now (it took too long). It tries again at the next refresh.");
    expect(toastWords("khipu exited 2: something odd")).toBe(
      "Khipu couldn't finish that. Try again in a moment; the details are in the tooltip.");
    expect(toastWords("khipu exited 1: Traceback (most recent call last)\n  File x\nValueError: bad")).toContain("ValueError");
    expect(plainError("hub snapshot (layout 9; this version uses 7)")).toBe(
      "This Mac's offline copy was made by a newer Khipu than the one running. It is rebuilt at the next check.");
    expect(plainError("(layout 3; this version uses 7)")).toContain("older Khipu");
    expect(plainError("plain words stay")).toBe("plain words stay");
  });

  it("words a stuck capture without the raw model text", () => {
    const out = captureCauseWords("last capture attempt failed: extract: RuntimeError: extract: model returned non-JSON: '{ \"a\": 1'; 2 job(s) waiting 1725 min — nothing is draining them");
    expect(out).toBe("The last session couldn't be saved: the summariser sent back something Khipu couldn't read. 2 sessions are waiting to be saved; they have waited 1 day 5 hours");
    expect(out).not.toMatch(/RuntimeError|non-JSON|job\(s\)/);
    const attention = buildAttention({
      capture_liveness: { ok: false, red: ["claude_code"], harnesses: { claude_code: { reasons: ["2 job(s) waiting 90 min — nothing is draining them"] } } },
    });
    expect(attention.items[0].cause).toContain("2 sessions are waiting to be saved; they have waited 90 minutes");
  });

  it("does not call 4% left 'plenty of time' and says how much is left", () => {
    const fact = { readable: true, window: "week", used_pct: 96, left_pct: 4, resets_at: "2026-03-10T00:36:00Z", pace_pct_per_hour: 0.2, hurry: false, words: "plenty of time" };
    const text = paceWords(fact, NOW)!;
    expect(text).toBe("Almost used up: 4% of its week left, resets 8:36 PM: using it all would take about 0.2% an hour");
    expect(text).not.toMatch(/plenty of time|a lot left/);
    const twenty = paceWords({ ...fact, used_pct: 80, left_pct: 20, pace_pct_per_hour: 1.4, hurry: true, words: "a lot left, little time" }, NOW)!;
    expect(twenty).toBe("20% of its week left, resets 8:36 PM: using it all would take about 1.4% an hour");
  });
});

describe("detail data", () => {
  it("builds the plan detail from usage and the routing record", () => {
    const row = usage().accounts![0];
    const d = planDetail(row, route(), NOW);
    expect(d.plan).toBe("Max");
    expect(d.windows.map((w) => [w.name, w.left])).toEqual([["Five hours", 92], ["Week", 20]]);
    expect(d.knows[0]).toEqual({ k: "Plan read", v: "Claude's own plan check, 2 min ago (took 1.1 s)." });
    expect(d.activity).toContain("Using about 0.3% an hour over the last day.");
    expect(d.activity).toContain("About 16 points would go unused at the reset.");
    // The week is the fuller window, so there is nothing to explain.
    expect(d.credits).toEqual([]);
  });

  it("says a Muse read came from a reply, and flags nearly-gone plans", () => {
    const rows = usage().accounts!;
    expect(planDetail(rows[2], route(), NOW).knows[0].v).toMatch(/^From Muse's last reply, 2 min ago\./);
    expect(nearlyGoneWords(rows[1], NOW)).toBe("Almost used up: 4% left until 8:36 PM");
    expect(nearlyGoneWords(rows[0], NOW)).toBeNull();
    expect(activityLines(null, undefined, NOW)).toEqual(["Nothing to forecast yet."]);
  });

  it("lists caps with their ends, exceptions with their end times, and none when absent", () => {
    const view = route();
    const d = routingDetail(view.accounts![0], view, usage().accounts![0], NOW);
    expect(d.caps[0]).toEqual({
      title: "Jobs: keep at or under an even pace",
      now: "An even pace allows 11.1% by now; 37% is used.",
      ends: "Yours since Mar 2 · no end",
    });
    expect(d.caps[1].title).toBe("At most $50 per rolling 7 days");
    expect(d.exceptions).toEqual([{
      text: "Reviews may run 10 points ahead of an even pace.",
      ends: "Ends tomorrow 4:30 PM",
      by: "you said so Mar 8",
    }]);
    expect(d.advice.map((a) => [a.k, a.verdict])).toEqual([["New jobs", "Hold back"], ["Your main conversations", "Fine to use"]]);
    expect(d.advice[0].why).toBe("26 points ahead of an even pace. Clears in 1 day 21 hours.");
    expect(d.rank).toBe("1st of 2");
    expect(d.history.map((h) => h.text)).toEqual(["Has room again.", "Limit reached, back Thu 2:09 PM (read from Alpha)."]);
    expect(d.limits).toHaveLength(1);
    const other = routingDetail(view.accounts![1], view, undefined, NOW);
    expect(other.exceptions).toEqual([]);
    expect(other.rank).toBe("2nd of 2");
    expect(other.gaps.join(" ")).toContain("Exceptions with end times");
  });

  it("gives a cap its own end when the CLI sends one", () => {
    const row = capRow({ kind: "share", pct: 95, window: "weekly", used_pct: 37, until: "2026-03-10T20:30:00Z" }, () => null, NOW);
    expect(row.ends).toBe("Ends tomorrow 4:30 PM");
    expect(cleanWhy("1 d 21 h plus 0")).toBe("1 day 21 hours");
  });
});

describe("Plans rows open the Plan detail", () => {
  it("is a button per row, and the sheet shows usage, activity and how Khipu knows", async () => {
    render(<PlansCard state={plansState(usage())} routing={route()} />);
    const open = screen.getByRole("button", { name: /^Alpha Max: 20% of its week left, .* Open usage details$/ });
    expect(screen.getByRole("button", { name: /^Gamma Lite: 4% of its week left/ })).toBeInTheDocument();
    // An id is never printed as a plan.
    expect(screen.queryByText(/40000000000000002/)).toBeNull();
    open.focus();
    fireEvent.click(open);
    const dialog = screen.getByRole("dialog");
    expect(dialog.querySelector('[data-view="plan"]')).not.toBeNull();
    const view = within(dialog);
    expect(view.getByRole("heading", { name: "Alpha Max" })).toBeInTheDocument();
    for (const h of ["How much is left", "Activity and forecast", "How Khipu knows"]) {
      expect(view.getByRole("heading", { name: h })).toBeInTheDocument();
    }
    expect(view.getByText("20% left")).toBeInTheDocument();
    // The plan view is not the routing view.
    expect(view.queryByRole("heading", { name: "Your caps" })).toBeNull();
    expect(view.queryByRole("heading", { name: "Exceptions in force" })).toBeNull();
  });

  it("closes on Escape and gives focus back to the row", async () => {
    render(<PlansCard state={plansState(usage())} routing={route()} />);
    const open = screen.getByRole("button", { name: /^Alpha Max/ });
    open.focus();
    fireEvent.click(open);
    fireEvent(screen.getByRole("dialog"), new Event("cancel", { cancelable: true }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(open));
  });

  it("closes from the close button and from the backdrop", () => {
    render(<PlansCard state={plansState(usage())} routing={route()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Alpha Max/ }));
    fireEvent.click(screen.getByRole("button", { name: "Close details" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Alpha Max/ }));
    fireEvent.click(screen.getByRole("dialog"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("says a nearly-gone plan in words beside the bar", () => {
    render(<PlansCard state={plansState(usage())} routing={route()} />);
    expect(screen.getByText("Almost used up: 4% left until 8:36 PM")).toBeInTheDocument();
  });
});

describe("Routing rows and priority names open the Routing detail", () => {
  it("opens from a row with state, caps, exceptions, advice, words and history", () => {
    render(<RoutingCard state={routingState(route())} plans={usage()} />);
    const open = screen.getByRole("button", { name: /^Alpha: Open\. Open routing details$/ });
    open.focus();
    fireEvent.click(open);
    const dialog = screen.getByRole("dialog");
    expect(dialog.querySelector('[data-view="routing"]')).not.toBeNull();
    expect(dialog.querySelector('[data-view="plan"]')).toBeNull();
    const view = within(dialog);
    for (const h of ["State and how it was read", "Exceptions in force", "Your caps", "Khipu's advice", "Recent changes", "Limit reports"]) {
      expect(view.getByRole("heading", { name: h })).toBeInTheDocument();
    }
    expect(view.getByRole("heading", { name: "Your words for this account · 1st of 2" })).toBeInTheDocument();
    expect(view.getByText("Reviews may run 10 points ahead of an even pace.")).toBeInTheDocument();
    expect(view.getByText("Ends tomorrow 4:30 PM")).toBeInTheDocument();
    expect(view.getByText("First choice for builds.")).toBeInTheDocument();
    // The routing view is not the plan view.
    expect(view.queryByRole("heading", { name: "How Khipu knows" })).toBeNull();
    expect(view.queryByRole("heading", { name: "How much is left" })).toBeNull();
  });

  it("says 'None.' only for an empty list, and the gap when the record stores no list", () => {
    const data = route();
    // Gamma has words but no stored list; Zeta is not in the priority at all.
    data.accounts!.push({ key: "zeta|", label: "Zeta", harness: "claude", state: "open", configured: true });
    data.priority!.entries.push({ account: "eta|", label: "Eta", role: "Spare.", until: null, in_settings: true, exceptions: [] });
    data.accounts!.push({ key: "eta|", label: "Eta", harness: "claude", state: "open", configured: true });
    render(<RoutingCard state={routingState(data)} plans={usage()} />);
    const sectionOf = () => within(screen.getByRole("dialog")).getByRole("heading", { name: "Exceptions in force" }).parentElement!;
    fireEvent.click(screen.getByRole("button", { name: /^Gamma: Open/ }));
    expect(sectionOf()).not.toHaveTextContent("None.");
    expect(sectionOf()).toHaveTextContent("Not stored as rows yet");
    expect(within(screen.getByRole("dialog")).getByText("Overflow only.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close details" }));
    fireEvent.click(screen.getByRole("button", { name: /^Zeta: Open/ }));
    expect(sectionOf()).toHaveTextContent("None.");
    expect(screen.getByRole("dialog")).not.toHaveTextContent("Not shown yet");
    fireEvent.click(screen.getByRole("button", { name: "Close details" }));
    fireEvent.click(screen.getByRole("button", { name: /^Eta: Open/ }));
    expect(sectionOf()).toHaveTextContent("None.");
  });

  it("opens from a priority name at the owner's words, and Escape returns focus", async () => {
    const scroll = vi.fn();
    (Element.prototype as unknown as { scrollIntoView: () => void }).scrollIntoView = scroll;
    render(<RoutingCard state={routingState(route())} plans={usage()} />);
    const name = screen.getByRole("button", { name: "Gamma: your words for this account" });
    name.focus();
    fireEvent.click(name);
    expect(within(screen.getByRole("dialog")).getByRole("heading", { name: "Gamma" })).toBeInTheDocument();
    expect(scroll).toHaveBeenCalled();
    fireEvent(screen.getByRole("dialog"), new Event("cancel", { cancelable: true }));
    await waitFor(() => expect(document.activeElement).toBe(name));
  });
});

describe("review round 2", () => {
  it("the Plan detail follows the live report: Refresh plan changes what it shows", () => {
    const { rerender } = render(<PlansCard state={plansState(usage())} routing={route()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Alpha Max/ }));
    expect(within(screen.getByRole("dialog")).getByText("20% left")).toBeInTheDocument();
    const next = usage();
    next.accounts![0].windows![1].used_pct = 50;
    rerender(<PlansCard state={plansState(next)} routing={route()} />);
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText("50% left")).toBeInTheDocument();
    expect(dialog.queryByText("20% left")).toBeNull();
  });

  it("closes the Plan detail when its account leaves the report", () => {
    const { rerender } = render(<PlansCard state={plansState(usage())} routing={route()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Alpha Max/ }));
    const gone = usage();
    gone.accounts = gone.accounts!.slice(1);
    rerender(<PlansCard state={plansState(gone)} routing={route()} />);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("matches an account without a key by its identity, never by position", () => {
    const keyless = (account: string, used: number): UsageReport => {
      const r = usage();
      r.accounts = [{ ...r.accounts![0], key: null, account, windows: [{ name: "weekly", used_pct: used, resets_at: "2026-03-10T14:59:00Z" }] }];
      return r;
    };
    const { rerender } = render(<PlansCard state={plansState(keyless("Alpha", 80))} routing={route()} />);
    const open = screen.getByRole("button", { name: /^Alpha Max/ });
    open.focus();
    fireEvent.click(open);
    expect(within(screen.getByRole("dialog")).getByText("20% left")).toBeInTheDocument();
    // Same position, different account: the sheet must not switch to it.
    rerender(<PlansCard state={plansState(keyless("Omega", 10))} routing={route()} />);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText("90% left")).toBeNull();
  });

  it("stays closed when the account comes back, and focus returns to the row", async () => {
    const { rerender } = render(<PlansCard state={plansState(usage())} routing={route()} />);
    const open = screen.getByRole("button", { name: /^Gamma Lite/ });
    open.focus();
    fireEvent.click(open);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    const gone = usage();
    gone.accounts = gone.accounts!.filter((r) => r.key !== "glm|");
    rerender(<PlansCard state={plansState(gone)} routing={route()} />);
    expect(screen.queryByRole("dialog")).toBeNull();
    rerender(<PlansCard state={plansState(usage())} routing={route()} />);
    expect(screen.queryByRole("dialog")).toBeNull();
    // The row is the same DOM node on a keyless swap, so focus can return to it.
    const keyless = (account: string): UsageReport => {
      const r = usage();
      r.accounts = [{ ...r.accounts![0], key: null, account }];
      return r;
    };
    rerender(<PlansCard state={plansState(keyless("Alpha"))} routing={route()} />);
    const row = screen.getByRole("button", { name: /^Alpha Max/ });
    row.focus();
    fireEvent.click(row);
    rerender(<PlansCard state={plansState(keyless("Omega"))} routing={route()} />);
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: /^Omega Max/ })));
  });

  it("says the real cause of a failed capture, and the summariser sentence only for non-JSON", () => {
    expect(captureCauseWords("last capture attempt failed: provider down")).toBe("The last session couldn't be saved: provider down");
    expect(captureCauseWords("last capture attempt failed: extract: HTTPError 429; 1 job(s) waiting 90 min — nothing is draining them")).toBe(
      "The last session couldn't be saved: HTTPError 429. A session is waiting to be saved; it has waited 90 minutes");
    expect(captureCauseWords("last capture attempt failed: capture rc=1")).toContain("capture rc=1");
    expect(captureCauseWords("last capture attempt failed: extract: model returned non-JSON: 'x'")).toContain("summariser sent back");
    const out = buildAttention({
      capture_liveness: { ok: false, red: ["claude_code"], harnesses: { claude_code: { reasons: ["last capture attempt failed: provider down"] } } },
    });
    expect(out.items[0].cause).toContain("provider down");
    expect(out.items[0].cause).not.toContain("summariser");
  });

  it("calls only the database's own timeout a database problem", () => {
    expect(plainError("lock timeout expired")).toBe("lock timeout expired");
    expect(plainError("could not connect to the model host")).toBe("could not connect to the model host");
    expect(plainError("connection timeout expired")).toContain("couldn't reach the database");
    expect(toastWords("khipu exited 1: error: lock timeout expired")).toBe(
      "Khipu couldn't finish that. Try again in a moment; the details are in the tooltip.");
  });

  it("does not print a left figure for a window that has reset since it was read", () => {
    const report = usage();
    // Weekly reset passed 5 minutes ago; the 80% was counted before it.
    report.accounts![0].windows![1].resets_at = "2026-03-09T18:55:00Z";
    render(<PlansCard state={plansState(report)} routing={route()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Alpha Max/ }));
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText("Figure is from before the reset")).toBeInTheDocument();
    expect(dialog.queryByText("20% left")).toBeNull();
    expect(dialog.getByText("92% left")).toBeInTheDocument();
  });

  it("says the non-week window is shown because it is the closest to its limit", () => {
    const row = usage().accounts![0];
    row.windows![0].used_pct = 90;
    row.windows![1].used_pct = 10;
    const d = planDetail(row, route(), NOW);
    expect(d.credits).toEqual(["Home shows the five-hour window because it is the closest to its limit."]);
  });

  it("every attention item's Details marks a rendered row, the whole report when it has none", async () => {
    const { HealthReportRows, markHealthRows } = await import("../healthRows");
    const { promotionAttention } = await import("../doctorAttention");
    const parsed = {
      hub_ok: true,
      backup_ok: false,
      topics_embed_lag_ok: false, topics_embed_lag: {},
      capture_liveness_ok: false,
      capture_liveness: { ok: false, red: ["claude_code"], harnesses: { claude_code: { reasons: ["x"] } } },
      promotion: { queued: { queued_at: "2026-03-09T18:00:00Z" } },
    } as Record<string, unknown>;
    const items = [...buildAttention(parsed).items, ...promotionAttention(parsed)];
    expect(items.map((i) => i.key)).toEqual(expect.arrayContaining(["backup", "topics_embed_lag", "liveness:claude_code", "promotion"]));
    render(<HealthReportRows parsed={parsed} attention={items} />);
    for (const item of items) {
      const marked = markHealthRows(document, healthKeysForAttention(item.key));
      expect(marked.length, item.key).toBeGreaterThan(0);
      marked.forEach((el) => expect(el.classList.contains("health-hl")).toBe(true));
      if (item.key === "promotion") expect(marked[0].hasAttribute("data-health-report")).toBe(true);
      else expect(marked.every((el) => el.hasAttribute("data-health-key"))).toBe(true);
    }
  });

  it("a tile's rows are rendered and marked", async () => {
    const { HealthReportRows, markHealthRows } = await import("../healthRows");
    render(<HealthReportRows parsed={{ backup_ok: false, graph_backup_ok: true, graph_offsite_ok: true }} attention={[]} />);
    const marked = markHealthRows(document, TILE_HEALTH_KEYS.backups);
    expect(marked.map((el) => el.getAttribute("data-health-key")).sort()).toEqual(["backup_ok", "graph_backup_ok", "graph_offsite_ok"]);
  });
});

describe("health tiles and callouts", () => {
  it("a tile with an opener is one button; without it stays a plain tile", () => {
    const open = vi.fn();
    const { rerender } = render(<Tile label="Backups" value="Stale" onOpen={open} openLabel="Backups: show its health checks" />);
    fireEvent.click(screen.getByRole("button", { name: "Backups: show its health checks" }));
    expect(open).toHaveBeenCalledTimes(1);
    rerender(<Tile label="Backups" value="Stale" />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("maps every tile and callout to the health rows it is about", () => {
    expect(TILE_HEALTH_KEYS.backups).toEqual(["backup_ok", "graph_backup_ok", "graph_offsite_ok"]);
    expect(TILE_HEALTH_KEYS.recording).toContain("capture_liveness_ok");
    expect(healthKeysForAttention("liveness:claude_code")).toEqual(["capture_liveness_ok"]);
    expect(healthKeysForAttention("backup")).toEqual(["backup_ok"]);
    expect(healthKeysForAttention("topics_embed_lag")).toEqual(["topics_embed_lag_ok"]);
    expect(healthKeysForAttention("routing:rules")).toContain("routing");
    expect(healthKeysForAttention("work:outbox")).toEqual(["work"]);
  });
});
