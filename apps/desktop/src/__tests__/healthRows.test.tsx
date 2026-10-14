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
import { buildAttention } from "../doctorAttention";
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
  // Phase 6 (honesty): D1 nightly-step evidence + D6 lag/degrade checks.
  "notes_reconcile_ok",
  "embed_provider_ok",
  "commitments_hygiene_ok",
  "mark_stale_ok",
  "topics_embed_lag_ok",
  "degraded_rate_ok",
];

function fixture(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  const base: Record<string, unknown> = {};
  for (const k of ALL_OK_KEYS) base[k] = true;
  return { ...base, ...overrides };
}

describe("healthRows (pure)", () => {
  it("returns one row per _ok key, all green, when everything passes", () => {
    const rows = healthRows(fixture());
    expect(rows).toHaveLength(20);
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

  it("a nightly step never recorded yet (Phase 6) is grey skipped, not red — false red on a fresh install/update", () => {
    const rows = healthRows(
      fixture({
        notes_reconcile_ok: true,
        not_configured: ["notes_reconcile"],
      }),
    );
    const row = rows.find((r) => r.key === "notes_reconcile_ok");
    expect(row?.status).toBe("skipped");
    expect(row?.label).toBe("Notes reconciled by the nightly");
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

  it("Phase 6: every new nightly-step / lag / degrade key has a plain-words label", () => {
    const rows = healthRows(fixture());
    const byKey = Object.fromEntries(rows.map((r) => [r.key, r.label]));
    expect(byKey.notes_reconcile_ok).toBe("Notes reconciled by the nightly");
    expect(byKey.embed_provider_ok).toBe("Embedding provider reachable");
    expect(byKey.commitments_hygiene_ok).toBe("Commitments hygiene ran");
    expect(byKey.mark_stale_ok).toBe("Stale commitments aged out");
    expect(byKey.topics_embed_lag_ok).toBe("Search index catching up on time");
    expect(byKey.degraded_rate_ok).toBe("Search rarely falls back to keyword-only");
  });
});

describe("HealthReportRows", () => {
  it("(a) renders 20 rows with a check mark and no raw JSON", () => {
    const { container } = render(
      <HealthReportRows parsed={fixture()} attention={[]} />,
    );
    expect(container.querySelectorAll(".row-item")).toHaveLength(20);
    expect(container.querySelectorAll("svg.ok")).toHaveLength(20);
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

describe("summariser rows (synth_provider / synth_budget)", () => {
  const synthBlocks = {
    synth_provider_ok: true,
    synth_provider: { provider: "claude-cli", model: "haiku", key_present: null, auth: "subscription", binary: "/opt/bin/claude" },
    synth_budget_ok: true,
    synth_budget: {
      calls: 1227,
      cap: 5000,
      cap_source: "config",
      exhausted: false,
      estimated_usd_today: 0.42,
      usage_by_provider: {
        gemini: { calls: 200, estimated_usd: 0.42 },
        "claude-cli": { calls: 1027, estimated_usd: 0, billing: "subscription" },
      },
    },
  };

  it("labels the provider row and pairs both flags with their detail blocks", () => {
    const rows = healthRows(fixture(synthBlocks));
    const provider = rows.find((r) => r.key === "synth_provider_ok");
    expect(provider?.label).toBe("Summariser provider ready");
    expect(provider?.detail).toBe("Claude on my own login (claude -p) · haiku · claude found");
    const budget = rows.find((r) => r.key === "synth_budget_ok");
    expect(budget?.detail).toBe(
      "1,227 of 5,000 calls today (set in Settings) · about $0.42 today · " +
        "Gemini Flash (API key): 200 calls, $0.42; Claude on my own login (claude -p): 1027 calls, subscription",
    );
  });

  it("adds the fallback to a non-Gemini provider's detail", () => {
    const rows = healthRows(
      fixture({
        ...synthBlocks,
        synth_provider: { ...synthBlocks.synth_provider, fallback: { setting: "gemini", source: "default", gemini_key: false } },
      }),
    );
    expect(rows.find((r) => r.key === "synth_provider_ok")?.detail).toBe(
      "Claude on my own login (claude -p) · haiku · claude found · Fallback: Gemini Flash, no key",
    );
  });

  it("an API provider without a key renders red with the attention sentence and the detail", () => {
    const parsed = fixture({
      ...synthBlocks,
      synth_provider_ok: false,
      synth_provider: { provider: "anthropic", model: "claude-haiku-5-5", key_present: false },
    });
    const { items } = buildAttention(parsed);
    render(<HealthReportRows parsed={parsed} attention={items} />);
    const row = screen.getByText("Summariser provider ready").closest(".row-item");
    expect(row?.querySelector("svg.err")).not.toBeNull();
    expect(row?.textContent).toContain("No key is stored for the anthropic summariser");
    expect(row?.textContent).toContain("Claude Haiku (API key) · claude-haiku-5-5 · no key");
  });

  it("a spent ceiling shows calls of cap on its red row", () => {
    const parsed = fixture({
      ...synthBlocks,
      synth_budget_ok: false,
      synth_budget: { calls: 5000, cap: 5000, cap_source: "default", exhausted: true, estimated_usd_today: 10 },
    });
    const { items } = buildAttention(parsed);
    expect(items.find((i) => i.key === "synth_budget")?.title).toBe("The summariser reached today's call ceiling");
    render(<HealthReportRows parsed={parsed} attention={items} />);
    const row = screen.getByText("Summariser within today's call ceiling").closest(".row-item");
    expect(row?.textContent).toContain("5,000 of 5,000 calls today (default) · about $10.00 today");
    expect(row?.textContent).toContain("lift it for today under Settings");
  });
});
