import { describe, expect, it } from "vitest";
import { buildAttention } from "../doctorAttention";

describe("buildAttention", () => {
  it("uses doctor coverage totals and stale chunks before nested fallback rows", () => {
    const item = buildAttention({
      embed_coverage_ok: false,
      embed_coverage: { missing: 5, stale: 2, pct: 80, episodes: { missing: 1 } },
    }).items.find((entry) => entry.key === "embed_coverage");
    expect(item?.cause).toContain("5 chunks missing, 2 chunks stale, 80% covered");
  });

  it("describes queued captures without handing the owner a terminal command", () => {
    const item = buildAttention({ outbox_ok: false }).items.find((entry) => entry.key === "outbox");
    expect(item?.cause).toContain("queued on this Mac");
    expect(item?.cause).not.toMatch(/`khipu|run `/);
    expect(item?.cause).not.toContain("Refresh retries");
    expect(item?.cause).not.toContain("scheduled");
  });
});

describe("buildAttention: pending database updates", () => {
  it("names each pending migration and offers the action that applies them", () => {
    const { issues, items } = buildAttention({
      ok: false,
      schema_migrations_ok: false,
      schema_migrations: { ok: false, pending: ["0028_ledger", "0029_origin"], unknown_applied: [] },
    });
    const item = items.find((i) => i.key === "schema_migrations");
    expect(item?.tone).toBe("err");
    expect(item?.title).toBe("The memory database needs an update this version of Khipu expects");
    expect(item?.cause).toContain("0028_ledger, 0029_origin");
    expect(item?.fix).toEqual({ label: "Apply update", kind: "apply-migrations" });
    expect(issues).toContain("Database update pending: 0028_ledger, 0029_origin");
  });

  it("says the check failed, with no fix, when the read errored", () => {
    const { items } = buildAttention({
      schema_migrations_ok: false,
      schema_migrations: { ok: false, pending: [], error: "OperationalError: timeout" },
    });
    const item = items.find((i) => i.key === "schema_migrations");
    expect(item?.title).toBe("Khipu could not check the memory database's version");
    expect(item?.fix).toBeUndefined();
  });

  it("adds no item for a hub that is only newer than the code", () => {
    const { items } = buildAttention({
      schema_migrations_ok: true,
      schema_migrations: { ok: true, pending: [], unknown_applied: ["0030_future"] },
    });
    expect(items.find((i) => i.key === "schema_migrations")).toBeUndefined();
  });
});
