// Routing slice 15a: delivery is a read-only doctor report. These tests pin
// the evidence boundary so absent or failed CLI evidence never looks healthy.
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { DeliverySection } from "../RoutingSettings";
import { routingOverview } from "../routingOverview";
import { DELIVERY_HEALTH, DELIVERY_NOW, DELIVERY_VIEW } from "../../dev/deliveryFake";

afterEach(cleanup);

describe("Telling conversations: delivery and health", () => {
  it("shows only the identity and component doctor actually provides", () => {
    render(<DeliverySection data={DELIVERY_VIEW} health={DELIVERY_HEALTH} now={DELIVERY_NOW} />);
    const rows = within(screen.getByRole("list", { name: "Live conversations" })).getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("claude:fixture-a");
    expect(rows[0]).toHaveTextContent("Current component: main");
    expect(rows[1]).toHaveTextContent("2 changes owed");
    expect(rows[2]).toHaveTextContent("Can't check: delivery point history");
    expect(screen.queryByText(/project-a|last prompt/i)).toBeNull();
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
    expect(model.problems.map((item) => item.needs).join(" ")).toContain("2 changes owed");
    expect(model.tags.telling.every((tag) => tag.tone === "err")).toBe(true);
  });

});
