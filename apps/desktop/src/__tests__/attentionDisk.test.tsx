// Attention items about disk space.
// Moved in slice A11 from StorageHygiene.test.tsx when its screen was removed; these test helpers
// that live code still uses, and need no removed screen.

import { describe, expect, it, vi } from "vitest";
import { buildAttention } from "../doctorAttention";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

const diskSpace = (level: "ok" | "amber" | "red", message: string) => ({
  ok: level !== "red",
  level,
  message,
  fix: { label: "Open storage report", kind: "storage-report" },
});

describe("disk space attention", () => {
  it("warns on amber even though disk_space_ok is true", () => {
    const parsed = { disk_space_ok: true, disk_space: diskSpace("amber", "Khipu's data disk has 8.0 GB free.") };
    const item = buildAttention(parsed).items.find((i) => i.key === "disk_space");
    expect(item?.tone).toBe("warn");
    expect(item?.cause).toBe("Khipu's data disk has 8.0 GB free.");
    expect(item?.fix).toEqual({ label: "Open storage report", kind: "storage-report" });
  });

  it("says nothing when there is plenty of room", () => {
    const parsed = { disk_space_ok: true, disk_space: diskSpace("ok", "Plenty of room.") };
    expect(buildAttention(parsed).items.some((i) => i.key === "disk_space")).toBe(false);
  });

});
