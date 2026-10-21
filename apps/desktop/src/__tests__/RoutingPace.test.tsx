// Routing slice 15e (docs/plans/2026-10-20-routing-first-class.md, 2a and
// 13): the Pace, caps and advice group. The fixture is the approved mock's
// fictional data, shaped as `khipu route --json` (docs/mockups/2026-10-21-routing).
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { PaceSection } from "../RoutingSettings";
import { paceModel } from "../routingPace";
import { PACE_NOW, PACE_PLANS, PACE_VIEW } from "../../dev/paceFake";

afterEach(cleanup);

const folds = { isOpen: () => false, toggle: () => {}, openKeys: () => {} };

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
    expect(deepseek?.line).toEqual({ week: "Spend can't be told", advice: "Jobs: go, unsure" });
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
