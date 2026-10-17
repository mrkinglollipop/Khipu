// Work view phase 5: the Work row in the health report and the Work
// attention items (mock work-states.html, sections 3, 4 and 6), from doctor's
// `ledger` and `work` blocks. Everything here warns; nothing gates.
import { describe, expect, it } from "vitest";
import { buildAttention } from "../doctorAttention";
import { healthRows, workHealthRow } from "../healthRows";

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

describe("the Work health row", () => {
  it("says where the ledger is kept and how each summary stands", () => {
    const row = workHealthRow(
      doctor([
        { project: "acme/atlas", state: "stale", as_of: at },
        { project: "acme/harbor", state: "ok", as_of: at },
        { project: "acme/quarry", state: "too_big", as_of: at },
        { project: "acme/pebble", state: "off" },
      ]),
    )!;
    expect(row.key).toBe("work");
    expect(row.label).toBe("Work");
    expect(row.status).toBe("err");
    const time = new Date(at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    expect(row.detail).toBe(
      `Ledger: 1,880 rows in 3 projects, in the offline copy and the nightly backup. Summaries: 1 fresh, 1 not updating since ${time} (atlas), 1 over its size limit (quarry), 1 turned off.`,
    );
  });

  it("is green when every summary is fresh and nothing waits", () => {
    const row = workHealthRow(doctor([{ project: "acme/atlas", state: "ok", as_of: at }]))!;
    expect(row.status).toBe("ok");
    expect(healthRows({ ...doctor([{ project: "acme/atlas", state: "ok" }]) }).some((r) => r.key === "work")).toBe(true);
  });

  it("names ledger changes waiting and a ledger missing from the offline copy", () => {
    const row = workHealthRow(doctor([], 3, false))!;
    expect(row.status).toBe("err");
    expect(row.detail).toContain("not in the offline copy yet");
    expect(row.detail).toContain("3 ledger changes are waiting for the database.");
    expect(row.detail).toContain("No summary files.");
  });

  it("is grey with no ledger and no summary file, and absent before doctor reads them", () => {
    expect(workHealthRow({ ledger: { projects: {} }, work: { sources: [] } })?.status).toBe("skipped");
    expect(workHealthRow({})).toBeNull();
  });
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
