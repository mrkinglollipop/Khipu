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

  it("names the supported manual recovery for queued captures", () => {
    const item = buildAttention({ outbox_ok: false }).items.find((entry) => entry.key === "outbox");
    expect(item?.cause).toContain("After reconnecting, run `khipu outbox drain`");
    expect(item?.cause).not.toContain("Refresh retries");
    expect(item?.cause).not.toContain("scheduled");
  });
});
