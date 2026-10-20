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

describe("offline copy row (hub_snapshot_ok)", () => {
  it("a copy behind the database stays green and says which kinds of change it lacks", () => {
    const parsed = fixture({
      hub_snapshot_ok: true,
      hub_snapshot: {
        ok: true,
        state: "behind",
        age_seconds: 7 * 3600,
        tables: {
          episodes: { lag_seconds: 0, missing: 0 },
          topics: { lag_seconds: 7 * 3600, missing: 2 },
          decisions: { lag_seconds: null, error: "UndefinedTable: decisions" },
        },
      },
    });
    const row = healthRows(parsed).find((r) => r.key === "hub_snapshot_ok");
    expect(row?.status).toBe("ok");
    expect(row?.label).toBe("Offline copy of memory");
    expect(row?.detail).toBe(
      "behind the database · rebuilt in full 7 h ago · topics 7 h behind, 2 to catch up · could not compare decisions",
    );
  });

  it("an in-step copy says so", () => {
    const parsed = fixture({
      hub_snapshot_ok: true,
      hub_snapshot: { ok: true, state: "fresh", age_seconds: 600, tables: { episodes: { lag_seconds: 0, missing: 0 } } },
    });
    expect(healthRows(parsed).find((r) => r.key === "hub_snapshot_ok")?.detail).toBe(
      "in step with the database · rebuilt in full 10 min ago · every kind of change in step",
    );
  });

  it("with the database unreachable it gives the last comparison", () => {
    const checked = new Date(Date.now() - 2 * 3600 * 1000).toISOString();
    const parsed = fixture({
      hub_snapshot_ok: true,
      hub_snapshot: {
        ok: true, state: "unknown", age_seconds: 3 * 3600,
        last_known: { measured_at: checked, checked_at: checked, state: "fresh" },
      },
    });
    expect(healthRows(parsed).find((r) => r.key === "hub_snapshot_ok")?.detail).toBe(
      "not compared with the database just now · rebuilt in full 3 h ago · 2 h ago it was in step with the database",
    );
  });

  it("ages the last comparison by this Mac's clock, never the database's", () => {
    // The database's clock runs 45 minutes ahead of this Mac's.
    const hubTime = new Date(Date.now() + 45 * 60 * 1000).toISOString();
    const macTime = new Date(Date.now() - 10 * 60 * 1000).toISOString();
    const block = (last: Record<string, string>) => fixture({
      hub_snapshot_ok: true,
      hub_snapshot: { ok: true, state: "unknown", age_seconds: 2 * 3600, last_known: { state: "fresh", ...last } },
    });
    const detail = (last: Record<string, string>) =>
      healthRows(block(last)).find((r) => r.key === "hub_snapshot_ok")?.detail;
    expect(detail({ measured_at: hubTime, checked_at: macTime })).toBe(
      "not compared with the database just now · rebuilt in full 2 h ago · 10 min ago it was in step with the database",
    );
    expect(detail({ measured_at: hubTime })).toBe(
      "not compared with the database just now · rebuilt in full 2 h ago · when last compared it was in step with the database",
    );
  });

  it("a failed rebuild is red, names the error and promises no Settings action", () => {
    const parsed = fixture({
      hub_snapshot_ok: false,
      hub_snapshot: {
        ok: false,
        state: "stale",
        reason: "snapshot_refresh_failed",
        age_seconds: 20 * 3600,
        refresh_error: { at: "2026-10-15T06:05:00+00:00", error: "OperationalError: timeout" },
      },
    });
    const { items } = buildAttention(parsed);
    const item = items.find((i) => i.key === "hub_snapshot");
    expect(item?.title).toBe("The offline copy of your memory is out of date");
    expect(item?.cause).toContain("The last full rebuild failed: OperationalError: timeout.");
    expect(item?.cause).not.toContain("Settings");
    render(<HealthReportRows parsed={parsed} attention={items} />);
    const row = screen.getByText("Offline copy of memory").closest(".row-item");
    expect(row?.querySelector("svg.err")).not.toBeNull();
    expect(row?.textContent).toContain("the last rebuild failed: OperationalError: timeout");
  });

  it("an overdue rebuild says how long ago the nightly job last ran it", () => {
    const parsed = fixture({
      hub_snapshot_ok: false,
      hub_snapshot: { ok: false, state: "stale", reason: "snapshot_refresh_overdue", age_seconds: 30 * 3600 },
    });
    const item = buildAttention(parsed).items.find((i) => i.key === "hub_snapshot");
    expect(item?.cause).toContain("The nightly job rebuilds it in full and last did so 30 h ago.");
  });
});

describe("ledger copy row", () => {
  it("sits next to the offline copy row and says the ledger's age", () => {
    const rows = healthRows({
      hub_snapshot_ok: true,
      ledger_copy_ok: true,
      hub_ok: true,
      ledger_copy: { ok: true, state: "fresh", age_seconds: 600 },
    });
    const keys = rows.map((r) => r.key);
    expect(keys.indexOf("ledger_copy_ok")).toBe(keys.indexOf("hub_snapshot_ok") + 1);
    const row = rows.find((r) => r.key === "ledger_copy_ok")!;
    expect(row.label).toBe("Offline copy of the ledger");
    expect(row.status).toBe("ok");
    expect(row.detail).toBe("in step · last caught up 10 min ago");
  });

  it("goes red with the failure, when it started and the next retry", () => {
    const now = Date.now();
    const parsed = {
      ledger_copy_ok: false,
      ledger_copy: {
        ok: false,
        state: "failing",
        age_seconds: 3 * 3600,
        last_error: {
          error: "database is locked",
          since: new Date(now - 45 * 60_000).toISOString(),
          next_retry_at: new Date(now + 5 * 60_000).toISOString(),
        },
      },
    };
    const row = healthRows(parsed).find((r) => r.key === "ledger_copy_ok")!;
    expect(row.status).toBe("err");
    expect(row.detail).toContain("updates keep failing");
    expect(row.detail).toContain("last caught up 3 h ago");
    expect(row.detail).toContain("the last update failed: database is locked");
    expect(row.detail).toContain("failing since 45 min ago");
    expect(row.detail).toMatch(/next try at /);
    const item = buildAttention(parsed).items.find((i) => i.key === "ledger_copy");
    expect(item?.cause).toContain("database is locked");
  });

  it("says nothing to fix on a fresh install with no ledger yet", () => {
    const row = healthRows({ ledger_copy_ok: true, ledger_copy: { ok: true, state: "not_applicable" } })
      .find((r) => r.key === "ledger_copy_ok")!;
    expect(row.status).toBe("ok");
    expect(row.detail).toBe("no ledger in the offline copy yet");
  });
});

describe("read_role_ok on a hub that cannot host the search role", () => {
  const parsed = {
    hub_ok: true,
    read_role_ok: true,
    read_role: {
      state: "cannot_host",
      level: "amber",
      warnings: ["Searches share the save connections on this hub: its owner role cannot create roles."],
    },
  };

  it("is a warn row between red and green, carrying the sentence", () => {
    const rows = healthRows({ ...parsed, disk_space_ok: false });
    const row = rows.find((r) => r.key === "read_role_ok")!;
    expect(row.status).toBe("warn");
    expect(row.detail).toContain("share the save connections");
    expect(rows.map((r) => r.status)).toEqual(["err", "warn", "ok"]);
  });

  it("renders with the warn icon and the fix sentence", () => {
    const { container } = render(<HealthReportRows parsed={parsed} attention={[]} />);
    expect(container.querySelector("svg.warn")).not.toBeNull();
    expect(screen.getByText(/share the save connections/)).toBeInTheDocument();
  });
});
