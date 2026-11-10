// Attention items about projects that stopped reporting.
// Moved in slice A11 from WorkHealth.test.tsx when its screen was removed; these test helpers
// that live code still uses, and need no removed screen.

import { describe, expect, it } from "vitest";
import { buildAttention } from "../doctorAttention";

const at = "2026-10-15T19:17:00Z";
const doctor = (sources: Array<Record<string, unknown>>, pending = 0, inCopy = true) => ({
  ledger: {
    hub: { ok: true },
    in_offline_copy: inCopy,
    outbox: { pending, refused: 0 },
    projects: { "acme/atlas": { rows: 1120 }, "acme/harbor": { rows: 680 }, "acme/lumen": { rows: 80 } },
  },
  work: { sources, warnings: [] },
});

describe("Work attention items", () => {
  it("raises one item per project that stopped reporting, is too big or newer, and for waiting changes", () => {
    const { items } = buildAttention(
      doctor(
        [
          { project: "acme/harbor", state: "gone", as_of: at },
          { project: "acme/quarry", state: "too_big", problem: "The summary file is 412 KB, over the 256 KB limit." },
          { project: "acme/beacon", state: "newer_format", problem: "Written in a newer format (2) than this Khipu reads (1). Update Khipu to see it." },
          { project: "acme/atlas", state: "stale", as_of: at },
        ],
        3,
      ),
    );
    const work = items.filter((i) => i.key.startsWith("work:"));
    expect(work.map((i) => i.key)).toEqual(["work:gone:acme/harbor", "work:too_big:acme/quarry", "work:newer_format:acme/beacon", "work:outbox"]);
    expect(work.every((i) => i.tone === "warn")).toBe(true);
    expect(work[0].title).toBe("harbor stopped reporting");
    expect(work[0].fix).toEqual({ label: "Open Work settings", kind: "work-settings" });
    expect(work[1].cause).toBe("The summary file is 412 KB, over the 256 KB limit. Khipu shows the last good read.");
    expect(work[1].fix).toEqual({ label: "Raise the limit", kind: "work-settings" });
    expect(work[2].fix).toBeUndefined();
    expect(work[3].title).toBe("3 ledger changes are waiting for the database");
    expect(work[3].cause).toContain("Nothing is lost.");
  });

  it("raises nothing when the summaries are fresh and nothing waits", () => {
    const { items } = buildAttention(doctor([{ project: "acme/atlas", state: "ok" }]));
    expect(items.some((i) => i.key.startsWith("work:"))).toBe(false);
  });

});
