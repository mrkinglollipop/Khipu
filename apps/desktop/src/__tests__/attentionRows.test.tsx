// Attention items about the meaning index rebuild and retries.
// Moved in slice A11 from healthRows.test.tsx when its screen was removed; these test helpers
// that live code still uses, and need no removed screen.

import { afterEach, describe, expect, it } from "vitest";
import { cleanup } from "@testing-library/react";
import { buildAttention } from "../doctorAttention";

afterEach(() => {
  cleanup();
});

const ALL_OK_KEYS = [
  "hub_ok",
  "drift_ok",
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
  "topics_embed_lag_ok",
  "degraded_rate_ok",
];

function fixture(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  const base: Record<string, unknown> = {};
  for (const k of ALL_OK_KEYS) base[k] = true;
  return { ...base, ...overrides };
}

describe("offline copy row (hub_snapshot_ok)", () => {
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
    const item = buildAttention(parsed).items.find((i) => i.key === "ledger_copy");
    expect(item?.cause).toContain("database is locked");
  });

});
