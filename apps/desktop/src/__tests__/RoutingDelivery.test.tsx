// Routing slice 15a: delivery is a read-only doctor report. These tests pin
// the evidence boundary so absent or failed CLI evidence never looks healthy.
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { DeliverySection } from "../RoutingSettings";
import { routingOverview } from "../routingOverview";
import { DELIVERY_HEALTH, DELIVERY_NOW, DELIVERY_VIEW } from "../../dev/deliveryFake";

afterEach(cleanup);

const SESSION_ID_SHAPED = [
  /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i,
  /[0-9a-f]{16,}/i,
  /[0-9a-f]{8,}/i,
  /claude_code_/,
  /\.routing\b/,
  /Explore-abcdef/,
  /fixture-/,
];

describe("Telling conversations: delivery and health", () => {
  it("shows generic conversation labels, never raw identities or component ids", () => {
    const { container } = render(<DeliverySection data={DELIVERY_VIEW} health={DELIVERY_HEALTH} now={DELIVERY_NOW} />);
    const rows = within(screen.getByRole("list", { name: "Live conversations" })).getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("Conversation 1");
    expect(rows[0]).toHaveTextContent("Claude");
    expect(rows[0]).toHaveTextContent("main");
    expect(rows[1]).toHaveTextContent("Conversation 2");
    expect(rows[1]).toHaveTextContent("Codex");
    expect(rows[1]).toHaveTextContent("2 changes owed");
    expect(rows[2]).toHaveTextContent("Conversation 1");
    expect(rows[2]).toHaveTextContent("a helper agent");
    expect(rows[2]).toHaveTextContent("Can't check: delivery point history");
    expect(screen.queryByText(/project-a|last prompt/i)).toBeNull();
    for (const pattern of SESSION_ID_SHAPED) expect(container.textContent).not.toMatch(pattern);
  });

  it("shows friendly names, Macs, last prompts and who has each change, with no owner action", () => {
    const view = {
      ...DELIVERY_VIEW,
      delivery_rows: [
        { label: "Fix the flaky build", harness: "claude_code", component: "main", mac: "This Mac", last_prompt_at: "2026-10-21T18:38:00Z", status: "green", owed: 0 },
        { label: "Conversation 2", harness: "codex", component: "mcp", mac: "Studio Mac", last_prompt_at: "2026-10-21T17:40:00Z", status: "red", owed: 2, reasons: ["budget-stall"] },
      ],
      delivery_macs: [{ label: "MacBook Air", age_seconds: 8040, behind: true }],
      changes: DELIVERY_VIEW.changes?.map((change, i) => ({
        ...change,
        recipients: i === 0
          ? { have_count: 1, live_count: 2, have: ["Fix the flaky build"], waiting: ["Conversation 2"] }
          : { have_count: 2, live_count: 2, have: [], waiting: [] },
      })),
    } as typeof DELIVERY_VIEW;
    const { container } = render(<DeliverySection data={view} health={DELIVERY_HEALTH} now={DELIVERY_NOW} />);
    const rows = within(screen.getByRole("list", { name: "Live conversations" })).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Fix the flaky build");
    expect(rows[0]).toHaveTextContent("This Mac");
    expect(rows[0]).toHaveTextContent("2 min ago");
    expect(rows[0]).toHaveTextContent("No changes owed");
    expect(rows[1]).toHaveTextContent("Conversation 2");
    expect(rows[1]).toHaveTextContent("Studio Mac");
    expect(rows[1]).toHaveTextContent("1 h ago");
    const changes = within(screen.getByRole("list", { name: "Recent changes and who has them" }));
    expect(changes.getByText("1 of 2 have it")).toBeTruthy();
    expect(changes.getByText(/Waiting: Conversation 2/)).toBeTruthy();
    expect(changes.getByText("2 of 2 have it")).toBeTruthy();
    const macs = screen.getByRole("list", { name: "Other Macs and the cloud" });
    expect(macs).toHaveTextContent("MacBook Air");
    expect(macs).toHaveTextContent("Behind");
    expect(screen.queryByText("Who has it isn't available")).toBeNull();
    expect(within(screen.getByRole("list", { name: "Live conversations" })).queryAllByRole("button")).toHaveLength(0);
    for (const pattern of SESSION_ID_SHAPED) expect(container.textContent).not.toMatch(pattern);
  });

  it("shows when the cached health report was read", () => {
    render(<DeliverySection data={DELIVERY_VIEW} health={DELIVERY_HEALTH} healthReadAt={DELIVERY_NOW} now={DELIVERY_NOW} />);
    expect(screen.getByText(/health report read at/i)).toHaveTextContent("40");
  });

  it("does not call a conversation up to date while telling is off", () => {
    render(<DeliverySection data={DELIVERY_VIEW} health={DELIVERY_HEALTH} tellingOff now={DELIVERY_NOW} />);
    expect(screen.queryByText("No changes owed")).toBeNull();
    expect(screen.getByRole("list", { name: "Live conversations" })).toHaveTextContent("Not being told");
  });

  it("shows Off before any delivery summary when the push is off", () => {
    const model = routingOverview(null, null, null, null, { routing: {}, push: { enabled: false }, delivery: DELIVERY_HEALTH }, DELIVERY_NOW);
    expect(model.lines.telling).toBe("Off");
  });

  it("keeps has-it information unknown until the doctor exposes it", () => {
    render(<DeliverySection data={DELIVERY_VIEW} health={DELIVERY_HEALTH} now={DELIVERY_NOW} />);
    const changes = within(screen.getByRole("list", { name: "Recent changes and who has them" }));
    expect(changes.getAllByText("Who has it isn't available")).toHaveLength(2);
    expect(screen.getByRole("list", { name: "Other Macs and the cloud" })).toHaveTextContent("Other MacsBehind");
    expect(screen.getByRole("list", { name: "Other Macs and the cloud" })).toHaveTextContent("Cloud copyCan't be checked");
  });

  it("does not reuse delivery rows after a failed doctor read", () => {
    render(<DeliverySection data={DELIVERY_VIEW} health={DELIVERY_HEALTH} healthError="doctor timed out" now={DELIVERY_NOW} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't read delivery health: doctor timed out");
    expect(screen.queryByRole("list", { name: "Live conversations" })).toBeNull();
    expect(screen.getByRole("list", { name: "Recent changes and who has them" })).toBeTruthy();
  });

  it("calls absent evidence unknown and adds no delivery action", () => {
    render(<DeliverySection data={{ record: true, priority: null, accounts: [], changes: [] }} now={DELIVERY_NOW} />);
    expect(screen.getByRole("status")).toHaveTextContent("Delivery health hasn't been reported yet");
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("No routing changes yet.")).toBeTruthy();
  });

  it("fails closed when a nominally green identity lacks its owed count", () => {
    render(<DeliverySection data={DELIVERY_VIEW} health={{ status: "green", identities: [{ identity: "codex:partial", component: "main", status: "green" }] }} now={DELIVERY_NOW} />);
    expect(screen.getByRole("list", { name: "Live conversations" })).toHaveTextContent("Can't be checked");
    expect(screen.queryByText("No changes owed")).toBeNull();
  });

  it("keeps a copy with no age unknown even if its status claims green", () => {
    render(<DeliverySection data={DELIVERY_VIEW} health={{ ...DELIVERY_HEALTH, replication: { status: "green" } }} now={DELIVERY_NOW} />);
    const copies = screen.getByRole("list", { name: "Other Macs and the cloud" });
    expect(copies).not.toHaveTextContent("Current");
    expect(copies).toHaveTextContent("Can't be checked");
  });

  it("surfaces delivery failures in Needs you even when the routing record failed", () => {
    const model = routingOverview(null, "route failed", null, null, { routing: null, push: null, delivery: DELIVERY_HEALTH }, DELIVERY_NOW);
    expect(model.problems.filter((item) => item.group === "telling")).toHaveLength(2);
    const needs = model.problems.map((item) => item.needs).join(" ");
    expect(needs).toContain("Conversation 2");
    expect(needs).toContain("Codex");
    expect(needs).toContain("2 changes owed");
    for (const pattern of SESSION_ID_SHAPED) expect(needs).not.toMatch(pattern);
    expect(model.tags.telling.every((tag) => tag.tone === "err")).toBe(true);
  });

});
