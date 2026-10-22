// Routing slice 15e (docs/plans/2026-10-20-routing-first-class.md, 2a and
// 13): the Pace, caps and advice group. The fixture is the approved mock's
// fictional data, shaped as `khipu route --json` (docs/mockups/2026-10-21-routing).
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { PaceSection } from "../RoutingSettings";
import { paceModel } from "../routingPace";
import { PACE_NOW, PACE_PLANS, PACE_VIEW } from "../../dev/paceFake";

afterEach(cleanup);

const folds = { isOpen: () => false, toggle: () => {}, openKeys: () => {} };

it("shows check allowance beside jobs without changing the job summary", () => {
  const view = structuredClone(PACE_VIEW);
  const codex = view.accounts!.find((a) => a.label === "Codex")!;
  codex.cap_statuses!.unshift({ kind: "pace_target", purpose: "check", window: "weekly",
    revision: "checks", pct: 100, slack: 5, allowance_pct: 26.96, used_pct: 24, verdict: "within" });
  codex.check_advice = { ...codex.advice, purpose: "check", verdict: "go", reason: "provider facts allow this work",
    caps: codex.cap_statuses!.map((c) => ({ ...c, applies: c.purpose === "check" })) };
  const row = paceModel(view, PACE_PLANS, PACE_NOW).rows.find((a) => a.label === "Codex")!;
  expect(row.caps.map((c) => c.name)).toContain("Week: Checks: up to 5 over even");
  expect(row.line.week).toContain("1.7 points ahead");
  render(<PaceSection data={view} plans={PACE_PLANS} now={PACE_NOW} folds={{ ...folds, isOpen: () => true }} />);
  fireEvent.click(screen.getByRole("button", { name: /^Codex/ }));
  expect(screen.getByText("Week: Checks: up to 5 over even")).toBeVisible();
  expect(screen.getByText("Checks")).toBeVisible();
  expect(row.advice?.checks?.label).toBe("Checks");
  expect(row.advice?.checks?.word).toBe("Go");
});

describe("Pace, caps and advice: the model", () => {
  it("puts the mock's line, tags and one open account on the group", () => {
    const model = paceModel(PACE_VIEW, PACE_PLANS, PACE_NOW);
    expect(model.line).toBe("Claude reaches a cap at 4:40 PM; Muse closed");
    expect(model.tags.map((t) => t.text)).toEqual(["Claude outpacing", "3 hold back for jobs"]);
    expect(model.rows.map((r) => r.label)).toEqual(["Claude", "Codex", "Grok", "Claude second", "GLM", "DeepSeek", "Muse"]);
    expect(model.rows.filter((r) => r.open).map((r) => r.label)).toEqual(["Claude"]);
    expect(model.crossings.map((c) => c.show)).toEqual(["Claude's pace"]);
  });

  it("words rates to two decimals and the reset the rate lands on", () => {
    const claude = paceModel(PACE_VIEW, PACE_PLANS, PACE_NOW).rows[0];
    expect(claude.windows.map((w) => w.rate)).toEqual(["9% an hour", "0.82% an hour"]);
    expect(claude.windows[0].rateTag?.text).toBe("Outpacing your 60%");
    expect(claude.windows[1].rateTag?.text).toBe("Outpacing");
    expect(claude.windows[0].r2).toBe("7.2% an hour lands on 60% at the 5:10 PM reset");
    expect(claude.windows[1].r2).toBe("0.59% an hour lands on 100% at the reset Mon 11:00 AM");
    const say = claude.say.map((p) => p.text).join("");
    expect(say).toContain("At the week's rate it would run out Sun 2:46 AM");
    expect(say).toContain(", 1 d 8 h before the reset.");
  });

  it("shows only the advice the CLI gives, and only a whole-limit target clears", () => {
    const model = paceModel(PACE_VIEW, PACE_PLANS, PACE_NOW);
    const claude = model.rows[0];
    expect(claude.caps.map((c) => c.name)).toEqual([
      "Five hours: at most 60%",
      "Week: at most 91%",
      "Week: even pace to 65%, 5 points slack",
      "Jobs: at most 13% a day",
    ]);
    expect(claude.advice).toMatchObject({
      word: "Hold back",
      tone: "hold",
      evid: "readings fresh",
      why: "6 points ahead of your even pace to 65%; near its 13% a day.",
    });
    // A 65% even-pace target is not a target usage closes on its own.
    expect(claude.advice?.why).not.toContain("Clears when");
    expect(model.rows[1].advice?.why).toContain("Clears when the allowance reaches 47%, about 4.5 h from now if no more is used.");
  });

  it("keeps an unknown reading unknown and a closed account closed", () => {
    const model = paceModel(PACE_VIEW, PACE_PLANS, PACE_NOW);
    const deepseek = model.rows.find((r) => r.label === "DeepSeek");
    expect(deepseek?.line).toEqual({ week: "$21.40 reported; a 3 h gap", advice: "Jobs: go, unsure" });
    expect(deepseek?.advice?.evid).toBe("evidence unknown: spend gap");
    const muse = model.rows.find((r) => r.label === "Muse");
    expect(muse?.line).toEqual({ week: "Back Mon 12:00 AM", advice: "Closed to all" });
    expect(muse?.open).toBe(false);
  });

  it("is empty only when no account has a pace or a cap", () => {
    expect(paceModel({ record: true, priority: null, accounts: [] }, null, PACE_NOW)).toMatchObject({
      rows: [], tags: [], crossings: [], line: "No accounts yet", empty: true,
    });
  });
});

// The four probe shapes from the cross-family review: real Python-computed
// values (not the preview fixture) that the first tests missed.
describe("Pace, caps and advice: probe regressions", () => {
  const NOW = new Date(2026, 9, 21, 14, 40);
  const at = (day: number, hour: number, minute = 0) => new Date(2026, 9, day, hour, minute).toISOString();
  const pace = (over: Record<string, unknown>) => ({
    window: "weekly", cycle_resets_at: at(26, 11), trailing_h: 24, buckets: 48,
    valid_pct: 97.9, observed_pct: 97.9, gappy: false, rate_pct_per_h: 1, why: null, used_pct: 12,
    plan: { verdict: "underpacing", needed_pct_per_h: 0.5, runs_out_at: null, before_reset_s: null, unused_at_reset_pct: 25 },
    share_cap: null, day_cap: null, pace_target: null, headline: "plan", ...over,
  });
  const view = (accounts: unknown[]) => ({ record: true, priority: null, accounts } as never);
  const acct = (over: Record<string, unknown>) => ({
    key: "probe|", label: "Probe", harness: "claude", state: "open", pace: [], cap_statuses: [], ...over,
  });

  it.each([
    "reset time spread over more than an hour",
    "possible reset; waiting for the next reading",
    "last reading 2 h old",
  ])("withholds unplaceable cap figures: %s", (why) => {
    const cap = { kind: "pace_target", revision: "fake", window: "weekly", pct: 100, slack: 0,
      allowance_pct: 14.3, used_pct: 32, verdict: "ahead" };
    const row = paceModel(view([acct({
      pace: [pace({ why, used_pct: 32, pace_target: cap })], cap_statuses: [cap],
    })]), null, NOW).rows[0];
    expect(row.caps[0].state).toBe(`Not known: ${why}`);
    expect(row.caps[0].tag).toBeNull();
  });

  it("keeps a hold_back verdict when its evidence is unknown", () => {
    const reached = pace({
      used_pct: 95,
      share_cap: { revision: "r-w", pct: 91, verdict: "cap_reached", needed_pct_per_h: null, runs_out_at: null, before_reset_s: null, unused_at_reset_pct: null },
      headline: "share_cap",
    });
    const model = paceModel(view([acct({
      pace: [reached],
      cap_statuses: [{ kind: "share", revision: "r-w", window: "weekly", pct: 91, verdict: "cap_reached" }],
      advice: { account: "probe", label: "Probe", purpose: "job", verdict: "hold_back", reason: "share cap_reached", evidence: "unknown", numbers: {}, eligible: true, why_eligible: "provider facts allow this work", advisory: true },
    })]), null, NOW);
    expect(model.rows[0].advice?.word).toBe("Hold back");
    expect(model.rows[0].advice?.evid).toMatch(/^evidence unknown:/);
    expect(model.rows[0].line.advice).toBe("Jobs: hold back, unsure");
  });

  it("keeps a closed verdict when its evidence is unknown", () => {
    const model = paceModel(view([acct({
      pace: [pace({})],
      advice: { account: "probe", label: "Probe", purpose: "job", verdict: "closed", reason: "limited", evidence: "unknown", numbers: {}, eligible: true, why_eligible: "provider facts allow this work", advisory: true },
    })]), null, NOW);
    expect(model.rows[0].advice?.word).toBe("Closed");
    expect(model.rows[0].line.advice).not.toContain("go");
  });

  it("shows only the trailing average rate, never a reconstructed past", () => {
    const model = paceModel(view([acct({
      pace: [pace({
        used_pct: 12, rate_pct_per_h: 1, trailing_h: 24,
        plan: { verdict: "outpacing", needed_pct_per_h: 0.5, runs_out_at: at(25, 6, 40), before_reset_s: null, unused_at_reset_pct: null },
      })],
    })]), null, NOW);
    const trend = model.rows[0].windows[0].trend;
    expect(trend?.label).toContain("averaged 1% an hour");
    expect(trend?.label).toContain("trailing average");
    expect(trend?.label).not.toContain("from");
    expect(trend?.label).not.toContain("-12%");
    const ys = (trend?.used ?? "").split(" ").map((p) => p.split(",")[1]);
    expect(new Set(ys).size).toBe(1);
  });

  it("lets a reached share cap win the badge, summary and default opening", () => {
    const model = paceModel(view([acct({
      pace: [pace({
        used_pct: 65,
        share_cap: { revision: "r-w", pct: 60, verdict: "cap_reached", needed_pct_per_h: null, runs_out_at: null, before_reset_s: null, unused_at_reset_pct: null },
        headline: "share_cap",
      })],
      cap_statuses: [{ kind: "share", revision: "r-w", window: "weekly", pct: 60, verdict: "cap_reached" }],
      advice: { account: "probe", label: "Probe", purpose: "job", verdict: "hold_back", reason: "share cap_reached", evidence: "fresh", numbers: {}, eligible: true, why_eligible: "provider facts allow this work", advisory: true },
    })]), null, NOW);
    const row = model.rows[0];
    expect(row.tag).toEqual({ text: "At your 60%", tone: "err" });
    expect(row.say.map((p) => p.text).join("")).toContain("60%");
    expect(row.say.map((p) => p.text).join("")).not.toContain("will go unused");
    expect(row.line.week).toContain("60%");
    expect(row.line.week).not.toContain("unused");
    expect(row.open).toBe(true);
  });

  it("names a reached plan before the calm pace fallback", () => {
    const reached = pace({
      used_pct: 100,
      plan: { verdict: "limit_reached", needed_pct_per_h: null, runs_out_at: null, before_reset_s: null, unused_at_reset_pct: null },
    });
    const row = paceModel(view([acct({
      pace: [reached],
      cap_statuses: [],
      advice: { account: "probe", label: "Probe", purpose: "job", verdict: "closed", reason: "plan limit reached", evidence: "fresh", numbers: {}, eligible: false, why_eligible: "plan limit reached", advisory: true },
    })]), null, NOW).rows[0];
    expect(row.line.week).toBe("At plan limit");
    expect(row.line.week).not.toBe("On pace");
    expect(row.say.map((part) => part.text).join("")).toBe("At its week plan limit.");
    expect(row.say.map((part) => part.text).join("")).not.toContain("Nothing Khipu has read");
  });

  it("renders a reached plan limit before the calm pace fallback", () => {
    const reached = pace({
      used_pct: 100,
      plan: { verdict: "limit_reached", needed_pct_per_h: null, runs_out_at: null, before_reset_s: null, unused_at_reset_pct: null },
    });
    const data = view([acct({
      pace: [reached],
      cap_statuses: [],
      advice: { account: "probe", label: "Probe", purpose: "job", verdict: "closed", reason: "plan limit reached", evidence: "fresh", numbers: {}, eligible: false, why_eligible: "plan limit reached", advisory: true },
    })]);
    render(<PaceSection data={data} plans={null} now={NOW} folds={{ ...folds, isOpen: () => true }} />);
    const line = screen.getByRole("button", { name: /Probe.*At plan limit/ });
    expect(line).toBeVisible();
    expect(line).not.toHaveAccessibleName(/On pace/);
    fireEvent.click(line);
    const card = screen.getByRole("group", { name: "Probe pace" });
    expect(within(card).getByText("Limit reached")).toBeVisible();
    expect(within(card).getByText("At its week plan limit.")).toBeVisible();
    expect(within(card).queryByText("On pace")).toBeNull();
  });

  it("names the share cap that caused the hold, not the first share cap", () => {
    const five = pace({
      window: "five_hour", cycle_resets_at: at(21, 17, 10), trailing_h: 1, buckets: 12, used_pct: 10,
      share_cap: { revision: "r-5h", pct: 60, verdict: "underpacing", needed_pct_per_h: 25, runs_out_at: null, before_reset_s: null, unused_at_reset_pct: null },
      headline: "share_cap",
    });
    const week = pace({
      used_pct: 95,
      share_cap: { revision: "r-w", pct: 91, verdict: "cap_reached", needed_pct_per_h: null, runs_out_at: null, before_reset_s: null, unused_at_reset_pct: null },
      headline: "share_cap",
    });
    const model = paceModel(view([acct({
      pace: [five, week],
      cap_statuses: [
        { kind: "share", revision: "r-5h", window: "five_hour", pct: 60, verdict: "underpacing" },
        { kind: "share", revision: "r-w", window: "weekly", pct: 91, verdict: "cap_reached" },
      ],
      advice: { account: "probe", label: "Probe", purpose: "job", verdict: "hold_back", reason: "share cap_reached", evidence: "fresh", numbers: {}, eligible: true, why_eligible: "provider facts allow this work", advisory: true },
    })]), null, NOW);
    expect(model.rows[0].advice?.why).toBe("At your week 91%.");
  });

  it("words the lead-conversation row, the cap confirm date and the reading age from the CLI's fields", () => {
    const base = { account: "probe", label: "Probe", numbers: {}, eligible: true, why_eligible: "provider facts allow this work", advisory: true };
    const at = new Date(NOW.getTime() - 3 * 60000).toISOString();
    const model = paceModel(view([acct({
      pace: [pace({ used_pct: 31 })],
      cap_statuses: [
        { kind: "share", revision: "r-w", window: "weekly", pct: 91, verdict: "ok", confirmed_at: "2026-10-18T09:00:00Z" },
        { kind: "per_day", revision: "r-d", window: "weekly", pct: 13, verdict: "ok", confirmed_at: "2026-10-20T12:00:00Z" },
      ],
      advice: { ...base, purpose: "job", verdict: "go", reason: "provider facts allow this work", evidence: "fresh", newest_reading_at: at },
      lead_advice: { ...base, purpose: "orchestration", verdict: "go", reason: "provider facts allow this work", evidence: "fresh", newest_reading_at: at,
        caps: [{ kind: "per_day", pct: 13, applies: false }, { kind: "share", pct: 91, applies: true }] },
    })]), null, NOW);
    const row = model.rows[0];
    expect(row.advice?.evid).toBe("readings fresh, 3 min ago");
    expect(row.capsEvid).toBe("confirmed by you Oct 20");
    expect(row.advice?.lead).toEqual({ word: "Go", tone: "go", why: "Your daily cap applies only to jobs and checks. Under 91%." });
  });

  it("keeps the old words when the CLI carries none of the new fields", () => {
    const row = paceModel(PACE_VIEW, PACE_PLANS, PACE_NOW).rows[0];
    expect(row.advice?.lead ?? null).toBeNull();
    expect(row.capsEvid).toBe("confirmed by you");
  });

  it("shows the Lead conversations row and the dated caps heading", () => {
    const base = { account: "probe", label: "Probe", numbers: {}, eligible: true, why_eligible: "x", advisory: true };
    const data = view([acct({
      pace: [pace({ used_pct: 95 })],
      cap_statuses: [{ kind: "share", revision: "r", window: "weekly", pct: 91, verdict: "cap_reached", confirmed_at: "2026-10-20T12:00:00Z" }],
      advice: { ...base, purpose: "job", verdict: "hold_back", reason: "share cap_reached", evidence: "fresh" },
      lead_advice: { ...base, purpose: "orchestration", verdict: "hold_back", reason: "share cap_reached", evidence: "fresh" },
    })]);
    render(<PaceSection data={data} plans={null} now={NOW} folds={folds} />);
    expect(screen.getByText("Lead conversations")).toBeTruthy();
    expect(screen.getByText(/confirmed by you Oct 20/)).toBeTruthy();
  });
});

describe("Pace, caps and advice: the group", () => {
  it("renders the open block, the closed lines and the push sub-fold", () => {
    render(<PaceSection data={PACE_VIEW} plans={PACE_PLANS} now={PACE_NOW} folds={folds} />);
    expect(screen.getByRole("group", { name: "Claude pace" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: /Your caps/ })).toBeTruthy();
    expect(screen.getByRole("heading", { name: /Khipu's advice/ })).toBeTruthy();
    expect(screen.getAllByText("6 points ahead").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Jobs: hold back").length).toBeGreaterThan(0);
    expect(screen.getByText("Closed to all")).toBeTruthy();
    expect(screen.getByText("When pace changes are sent to conversations")).toBeTruthy();
    // The CLI carries jobs advice only; a lead-conversation verdict would be invented.
    expect(screen.queryByText("Lead conversations")).toBeNull();
  });

  it("offers no owner action: no cap setting, confirming or removing", () => {
    render(<PaceSection data={PACE_VIEW} plans={PACE_PLANS} now={PACE_NOW} folds={folds} />);
    expect(screen.queryByRole("button", { name: /add a cap|set a cap|confirm|remove/i })).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("says when no account has a reading", () => {
    render(<PaceSection data={{ record: true, priority: null, accounts: [] }} plans={null} now={PACE_NOW} folds={folds} />);
    expect(screen.getByText("No usage readings yet")).toBeTruthy();
  });
});

describe("Owner-only partial spend display", () => {
  it.each([0, 21.40, 35, 35000])("shows $%s as reported without judging the cap or changing advice", (usd) => {
    const data = structuredClone(PACE_VIEW);
    const account = data.accounts!.find((a) => a.label === "DeepSeek")!;
    const cap = account.cap_statuses![0];
    Object.assign(cap, { reported: { usd, events: 1, gaps: [{
      runner: "project-c", from: "2026-10-20T07:00:00Z", to: "2026-10-20T10:00:00Z", seconds: 10800,
    }] } });
    const before = paceModel(PACE_VIEW, PACE_PLANS, PACE_NOW).rows.find((r) => r.label === "DeepSeek")!;
    const after = paceModel(data, PACE_PLANS, PACE_NOW).rows.find((r) => r.label === "DeepSeek")!;
    const amount = Number.isInteger(usd) ? `$${usd}` : `$${usd.toFixed(2)}`;
    expect(after.say.map((p) => p.text).join("")).toContain(`${amount} reported`);
    expect(after.say.map((p) => p.text).join("")).toContain("project-c runner has a 3 h gap");
    expect(after.caps[0].state).toContain(`${amount} reported`);
    expect(after.caps[0].state).toContain("gap Tue");
    expect(after.caps[0].tag).toBeNull();
    expect(after.advice).toEqual(before.advice);
    expect(after.tag).toEqual(before.tag);
    expect(after.crossing).toBeNull();
    render(<PaceSection data={data} plans={PACE_PLANS} now={PACE_NOW} folds={{ ...folds, isOpen: () => true }} />);
    fireEvent.click(screen.getByRole("button", { name: /DeepSeek/ }));
    expect(screen.getByText(new RegExp(`${amount.replace("$", "\\$")} reported · gap`))).toBeTruthy();
  });

  it("caps the gap list to the three largest and counts the rest", () => {
    const data = structuredClone(PACE_VIEW);
    const account = data.accounts!.find((a) => a.label === "DeepSeek")!;
    const gap = (day: number, hours: number) => ({
      runner: `runner-${day}`, from: new Date(2026, 9, day, 1).toISOString(),
      to: new Date(2026, 9, day, 1 + hours).toISOString(), seconds: hours * 3600,
    });
    Object.assign(account.cap_statuses![0], { reported: { usd: 21.40, events: 1,
      gaps: [gap(14, 1), gap(15, 6), gap(16, 2), gap(17, 5), gap(18, 3), gap(19, 4)] } });
    const row = paceModel(data, PACE_PLANS, PACE_NOW).rows.find((r) => r.label === "DeepSeek")!;
    expect(row.line.week).toBe("$21.40 reported; 6 gaps, largest 6 h");
    expect(row.caps[0].state).toContain("and 3 more");
    expect(row.caps[0].state).not.toContain("runner-");
    expect((row.caps[0].state.match(/gap /g) ?? []).length).toBe(3);
    const say = row.say.map((p) => p.text).join("");
    expect(say).toContain("and 3 more gaps");
    expect((say.match(/runner has a/g) ?? []).length).toBe(3);
  });

  it("counts gaps the CLI already folded into gaps_more", () => {
    const data = structuredClone(PACE_VIEW);
    const account = data.accounts!.find((a) => a.label === "DeepSeek")!;
    const gap = (day: number, hours: number) => ({
      runner: `runner-${day}`, from: new Date(2026, 9, day, 1).toISOString(),
      to: new Date(2026, 9, day, 1 + hours).toISOString(), seconds: hours * 3600,
    });
    Object.assign(account.cap_statuses![0], { reported: { usd: 21.40, events: 1, gaps_more: 4,
      gaps: [gap(15, 6), gap(17, 5), gap(19, 4)] } });
    const row = paceModel(data, PACE_PLANS, PACE_NOW).rows.find((r) => r.label === "DeepSeek")!;
    expect(row.line.week).toBe("$21.40 reported; 7 gaps, largest 6 h");
    expect(row.caps[0].state).toContain("and 4 more");
  });
});


it("names qualified check windows and says Not for checks", () => {
  const view = structuredClone(PACE_VIEW);
  const claude = view.accounts!.find((a) => a.label === "Claude")!;
  claude.cap_statuses!.push({ kind: "pace_target", purpose: "check", window: "weekly",
    revision: "checks", pct: 100, slack: 20, tier: "sonnet", sensitive_only: true, verdict: "within" });
  claude.check_advice = { ...claude.advice, purpose: "check", verdict: "not_eligible", reason: "tier not allowed",
    tier: "sonnet", sensitive: true };
  const row = paceModel(view, PACE_PLANS, PACE_NOW).rows.find((a) => a.label === "Claude")!;
  expect(row.caps.map((c) => c.name)).toContain("Week: Sonnet checks of sensitive code: up to 20 over even");
  expect(row.advice?.checks?.label).toBe("Sonnet checks of sensitive code");
  expect(row.advice?.checks?.word).toBe("Not for checks");
  render(<PaceSection data={view} plans={PACE_PLANS} now={PACE_NOW} folds={{ ...folds, isOpen: () => true }} />);
  expect(screen.getByText("Week: Sonnet checks of sensitive code: up to 20 over even")).toBeVisible();
  expect(screen.getByText("Sonnet checks of sensitive code")).toBeVisible();
  expect(screen.getByText("Not for checks")).toBeVisible();
});

it("uses plain Checks when check advice carries no tier", () => {
  const view = structuredClone(PACE_VIEW);
  const claude = view.accounts!.find((a) => a.label === "Claude")!;
  claude.cap_statuses!.push({ kind: "pace_target", purpose: "check", window: "weekly",
    revision: "checks", pct: 100, slack: 20, tier: "sonnet", sensitive_only: true, verdict: "within" });
  claude.cap_statuses!.push({ kind: "pace_target", purpose: "check", window: "five_hour",
    revision: "checks-5h", pct: 100, slack: 20, tier: "opus", sensitive_only: false, verdict: "within" });
  claude.check_advice = { ...claude.advice, purpose: "check", verdict: "hold_back", reason: "pace_target within",
    tier: null, sensitive: false };
  const row = paceModel(view, PACE_PLANS, PACE_NOW).rows.find((a) => a.label === "Claude")!;
  expect(row.advice?.checks?.label).toBe("Checks");
  expect(row.advice?.checks?.word).toBe("Hold back");
});
