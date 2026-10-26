// Doctor rows that did not run (skipped / not applicable) and attention wording
// for buried outbox / non-hook capture failures.
import { describe, expect, it } from "vitest";
import { buildAttention } from "../doctorAttention";
import { healthRows } from "../healthRows";

describe("doctor attention wording", () => {
  it("buried outbox jobs (pending 0) are not told to run `khipu outbox drain`", () => {
    const { items } = buildAttention({
      outbox_ok: false,
      outbox: { pending: 0, dead: 1, native_held: [], quarantined: 0 },
    });
    const item = items.find((i) => i.key === "outbox");
    expect(item, "outbox attention item").toBeDefined();
    expect(item!.cause).not.toMatch(/outbox drain/);
    expect(item!.cause).toMatch(/retry-dead|retry/i);
  });

  it("a drain failure or stale queue is not fixed with Reinstall hook", () => {
    const { items } = buildAttention({
      capture_liveness: {
        ok: false,
        red: ["claude_code"],
        harnesses: { claude_code: { reasons: ["last capture attempt failed: database unreachable"] } },
      },
    });
    const item = items.find((i) => i.key === "liveness:claude_code");
    expect(item, "liveness item").toBeDefined();
    expect(item!.fix?.kind).not.toBe("reinstall-hook");
  });
});

describe("health rows for checks that did not run", () => {
  it("a schema check that got no answer from the database is not a green row", () => {
    const rows = healthRows({
      hub_ok: false,
      schema_migrations_ok: true,
      schema_migrations: { ok: true, skipped: "the database did not answer", pending: [] },
      not_configured: [],
    });
    expect(rows.find((r) => r.key === "schema_migrations_ok")!.status).not.toBe("ok");
  });

  it("a check marked applicable:false is not a green row", () => {
    const rows = healthRows({
      git_sync_ok: true,
      git_sync: { ok: true, applicable: false, note: "not the sync host" },
      not_configured: [],
    });
    expect(rows.find((r) => r.key === "git_sync_ok")!.status).not.toBe("ok");
  });
});
