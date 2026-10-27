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
    expect(item!.cause).toMatch(/held on this Mac/);
    expect(item!.cause).not.toMatch(/`khipu|retry-dead/);
  });

  it("no outbox or capture wording hands the owner a terminal command", () => {
    const { items } = buildAttention({
      outbox_ok: false,
      outbox: { pending: 3, dead: 0 },
      capture_liveness: {
        ok: false,
        red: ["claude_code"],
        harnesses: { claude_code: { reasons: ["last capture attempt failed: database unreachable"] } },
      },
    });
    expect(items.length).toBeGreaterThan(1);
    for (const i of items) expect(i.cause, i.key).not.toMatch(/`khipu|run `|retry-dead/);
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

  it("mixed hook + drain reasons still offer Reinstall hook", () => {
    const { items } = buildAttention({
      capture_liveness: {
        ok: false,
        red: ["claude_code"],
        harnesses: {
          claude_code: {
            reasons: ["hook error on last run: boom", "last capture attempt failed: x"],
          },
        },
      },
    });
    const item = items.find((i) => i.key === "liveness:claude_code");
    expect(item, "liveness item").toBeDefined();
    expect(item!.fix?.kind).toBe("reinstall-hook");
  });

  it("a hook error that mentions drain is still fixed with Reinstall hook", () => {
    const { items } = buildAttention({
      capture_liveness: {
        ok: false,
        red: ["claude_code"],
        harnesses: { claude_code: { reasons: ["hook error on last run: drain lock held"] } },
      },
    });
    const item = items.find((i) => i.key === "liveness:claude_code");
    expect(item, "liveness item").toBeDefined();
    expect(item!.fix?.kind).toBe("reinstall-hook");
    expect(item!.cause).not.toMatch(/reinstalling the hook will not clear/i);
  });

  it("cadence not firing still offers Reinstall hook", () => {
    const { items } = buildAttention({
      capture_liveness: {
        ok: false,
        red: ["claude_code"],
        harnesses: { claude_code: { reasons: ["5 turn(s) over 121 min without a capture being due — cadence not firing"] } },
      },
    });
    const item = items.find((i) => i.key === "liveness:claude_code");
    expect(item, "liveness item").toBeDefined();
    expect(item!.fix?.kind).toBe("reinstall-hook");
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
