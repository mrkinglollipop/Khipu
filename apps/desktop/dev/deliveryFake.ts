// Routing slice 15a: fictional, stable `khipu route --json` and `khipu
// doctor` shapes for the desktop-only delivery preview. No real ledger, home,
// account, or routing record is read by this fixture.
import type { RouteDeliveryHealth, RouteView } from "../src/routing";

export const DELIVERY_NOW = new Date(2026, 9, 21, 14, 40);

export const DELIVERY_VIEW: RouteView = {
  record: true,
  priority: null,
  accounts: [],
  changes: [
    {
      version: 24,
      at: "2026-10-21T18:31:00Z",
      entities: ["rule:1"],
      items: [{ entity: "rule:1", n: 1, now: { text: "Keep security review on the high lane" }, was: { text: "Keep reviews careful" }, source: "owner", via: "app", by: "you" }],
    },
    {
      version: 23,
      at: "2026-10-21T17:55:00Z",
      entities: ["account:muse:main"],
      items: [{ entity: "account:muse:main", now: { label: "Muse", state: "limited" }, was: { label: "Muse", state: "open" }, source: "report", by: "a Claude Code conversation" }],
    },
  ].reverse(),
};

/** Exactly the health fields delivery currently receives from `khipu doctor`.
 * It intentionally omits friendly identity labels and per-change recipients. */
export const DELIVERY_HEALTH: RouteDeliveryHealth = {
  status: "red",
  reasons: ["budget-stall", "cross-mac-stale"],
  missing: [],
  identities: [
    { identity: "claude_code_0a1b2c3d-1111-2222-3333-444455556666.routing", component: "main", status: "green", owed: 0, reasons: [], missing: [] },
    { identity: "codex_9f8e7d6c-aaaa-bbbb-cccc-ddddeeeeffff.routing", component: "mcp", status: "red", owed: 2, reasons: ["budget-stall"], missing: [] },
    { identity: "claude_code_0a1b2c3d-1111-2222-3333-444455556666.routing", component: "agent:Explore-abcdef12", status: "unknown", reasons: [], missing: ["delivery point history"] },
  ],
  prompts: { status: "green", count: 38, skipped: 0, rate_pct: 0 },
  replication: { status: "red", age_seconds: 8040 },
  gateway: { status: "unknown" },
};

