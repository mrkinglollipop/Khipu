// The Home "Full health report" used to be all-or-nothing: red checks got a
// plain-language row, but with nothing red (or once you'd read the red rows)
// the rest of the report was a raw JSON dump. These tests cover the pure
// `healthRows()` mapping (label/status/order/skip) and the `HealthReportRows`
// render (icon per status, the shared attention sentence/action on red, and
// that it never renders the raw JSON itself — that lives behind App.tsx's
// nested "Raw report" disclosure, outside this component).
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { healthRows, HealthReportRows } from "../healthRows";
import type { Attention } from "../doctorAttention";

afterEach(() => {
  cleanup();
});

const ALL_OK_KEYS = [
  "hub_ok",
  "graph_backup_ok",
  "graph_offsite_ok",
  "drift_ok",
  "graph_drift_ok",
  "outbox_ok",
  "capture_liveness_ok",
  "git_sync_ok",
  "backup_ok",
  "dsn_file_ok",
  "index_freshness_ok",
  "embed_coverage_ok",
  "recall_probe_ok",
  "bundle_seal_ok",
];

function fixture(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  const base: Record<string, unknown> = {};
  for (const k of ALL_OK_KEYS) base[k] = true;
  return { ...base, ...overrides };
}

describe("healthRows (pure)", () => {
  it("returns one row per _ok key, all green, when everything passes", () => {
    const rows = healthRows(fixture());
    expect(rows).toHaveLength(14);
    expect(rows.every((r) => r.status === "ok")).toBe(true);
  });

  it("humanizes an unknown _ok key it has no plain-words label for", () => {
    const rows = healthRows(fixture({ weird_thing_ok: true }));
    const row = rows.find((r) => r.key === "weird_thing_ok");
    expect(row).toBeDefined();
    expect(row?.label).toBe("Weird thing");
    expect(row?.status).toBe("ok");
  });

  it("marks a key named under not_configured as skipped, not red or green", () => {
    const rows = healthRows(fixture({ not_configured: ["memory_root"] }));
    const row = rows.find((r) => r.key === "drift_ok");
    expect(row?.status).toBe("skipped");
  });

  it("sorts red first, then not-set-up, then green", () => {
    const rows = healthRows(
      fixture({
        recall_probe_ok: false,
        not_configured: ["graph_sqlite"],
      }),
    );
    expect(rows[0]).toMatchObject({ key: "recall_probe_ok", status: "err" });
    expect(rows[1]).toMatchObject({ key: "graph_drift_ok", status: "skipped" });
    expect(rows[rows.length - 1].status).toBe("ok");
  });
});

describe("HealthReportRows", () => {
  it("(a) renders 14 rows with a check mark and no raw JSON", () => {
    const { container } = render(
      <HealthReportRows parsed={fixture()} attention={[]} />,
    );
    expect(container.querySelectorAll(".row-item")).toHaveLength(14);
    expect(container.querySelectorAll("svg.ok")).toHaveLength(14);
    expect(container.querySelector("pre")).toBeNull();
  });

  it("(b) a red check renders first, red, with the shared attention sentence and action", () => {
    const attention: Attention[] = [
      {
        key: "recall_probe",
        tone: "err",
        title: "Nothing has proved that recall works end to end lately",
        cause:
          "The probe records a session, searches for it and removes it again.",
        fix: { label: "Run recall probe", kind: "recall-probe" },
      },
    ];
    const { container } = render(
      <HealthReportRows
        parsed={fixture({ recall_probe_ok: false })}
        attention={attention}
      />,
    );
    const rowItems = container.querySelectorAll(".row-item");
    expect(rowItems[0].querySelector("svg.err")).not.toBeNull();
    expect(rowItems[0].textContent).toContain("Memory round trip works");
    expect(rowItems[0].textContent).toContain(
      "The probe records a session, searches for it and removes it again.",
    );
    expect(
      screen.getByRole("button", { name: "Run recall probe" }),
    ).toBeInTheDocument();
  });

  it("(c) an unknown _ok key renders its humanized label", () => {
    render(
      <HealthReportRows
        parsed={fixture({ weird_thing_ok: true })}
        attention={[]}
      />,
    );
    expect(screen.getByText("Weird thing")).toBeInTheDocument();
  });

  it("(d) a not_configured key renders grey 'not set up', not red", () => {
    const { container } = render(
      <HealthReportRows
        parsed={fixture({ not_configured: ["memory_root"] })}
        attention={[]}
      />,
    );
    const rowItems = Array.from(container.querySelectorAll(".row-item"));
    const driftRow = rowItems.find((el) =>
      el.textContent?.includes("Memory index matches the database"),
    );
    expect(driftRow).toBeDefined();
    expect(driftRow?.textContent).toContain("not set up");
    expect(driftRow?.querySelector("svg.muted")).not.toBeNull();
    expect(driftRow?.querySelector("svg.err")).toBeNull();
  });
});
