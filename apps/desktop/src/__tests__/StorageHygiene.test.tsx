// Slice 8, storage hygiene: the disk-space attention items, the health row,
// the Storage settings and the storage report dialog.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HealthChecksCard, STORAGE_SETTINGS, storageValueError } from "../HealthChecksCard";
import { buildAttention } from "../doctorAttention";
import { healthRows, HealthReportRows } from "../healthRows";
import { StorageReportDialog } from "../StorageReportDialog";

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
    const row = healthRows(parsed).find((r) => r.key === "disk_space_ok");
    expect(row?.status).toBe("ok");
    expect(row?.detail).toBe("Khipu's data disk has 8.0 GB free.");
  });

  it("is an error on red and a red row carries the same sentence and button", () => {
    const parsed = { disk_space_ok: false, disk_space: diskSpace("red", "Only 2.0 GB is free.") };
    const { items } = buildAttention(parsed);
    const item = items.find((i) => i.key === "disk_space");
    expect(item?.tone).toBe("err");
    const onFix = vi.fn();
    render(<HealthReportRows parsed={parsed} attention={items} onFixAction={onFix} />);
    expect(screen.getByText("Enough free disk space")).toBeInTheDocument();
    expect(screen.getByText("Only 2.0 GB is free.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open storage report" }));
    expect(onFix).toHaveBeenCalledWith({ label: "Open storage report", kind: "storage-report" }, undefined);
    cleanup();
  });

  it("says nothing when there is plenty of room", () => {
    const parsed = { disk_space_ok: true, disk_space: diskSpace("ok", "Plenty of room.") };
    expect(buildAttention(parsed).items.some((i) => i.key === "disk_space")).toBe(false);
    expect(healthRows(parsed).find((r) => r.key === "disk_space_ok")?.detail).toBeUndefined();
  });
});

describe("Storage settings", () => {
  const calls: Array<[string, Record<string, unknown> | undefined]> = [];
  let kept: number | null;
  beforeEach(() => {
    calls.length = 0;
    kept = null;
    invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
      calls.push([cmd, args]);
      if (cmd === "khipu_config_show") {
        const storage: Record<string, unknown> = {};
        for (const s of STORAGE_SETTINGS) {
          storage[s.key] = { value: 10, default: 10, kind: "number", source: "default", words: "" };
        }
        storage.commitments_backup_keep_days = {
          value: kept ?? 30,
          default: 30,
          kind: "days",
          source: kept == null ? "default" : "file",
          words: "",
        };
        return JSON.stringify({ storage });
      }
      if (cmd === "khipu_config_set") {
        kept = Number(args?.value);
        return JSON.stringify({ ok: true });
      }
      if (cmd === "khipu_config_unset") {
        kept = null;
        return JSON.stringify({ ok: true });
      }
      throw new Error(`unexpected command ${cmd}`);
    });
  });
  afterEach(() => cleanup());

  it("shows all seven rows with their defaults", async () => {
    render(<HealthChecksCard active />);
    expect(STORAGE_SETTINGS).toHaveLength(7);
    for (const s of STORAGE_SETTINGS) expect(await screen.findByLabelText(s.label)).toBeInTheDocument();
    expect(screen.getAllByText(/0 keeps every backup/)).toHaveLength(1);
  });

  it("saves and resets one through the config commands", async () => {
    render(<HealthChecksCard active />);
    const input = await screen.findByLabelText("Keep commitments backups for (days)");
    await waitFor(() => expect(input).toHaveValue("30"));
    fireEvent.change(input, { target: { value: "14" } });
    const row = input.closest(".set-field") as HTMLElement;
    fireEvent.click(row.querySelector("button") as HTMLButtonElement);
    // The field already shows the typed value, so wait for the write itself.
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_set", { key: "commitments_backup_keep_days", value: "14" }]));
    await waitFor(() => expect(input).toHaveValue("14"));
    fireEvent.click(await screen.findByRole("button", { name: "Use default" }));
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_unset", { key: "commitments_backup_keep_days" }]));
    await waitFor(() => expect(input).toHaveValue("30"));
  });

  it("refuses bad values before calling Khipu", () => {
    const days = STORAGE_SETTINGS.find((s) => s.key === "synth_usage_keep_days")!;
    const pct = STORAGE_SETTINGS.find((s) => s.key === "disk_warn_free_percent")!;
    expect(storageValueError("0", days)).toBeNull();
    expect(storageValueError("1.5", days)).toBe("Enter a whole number of days.");
    expect(storageValueError("2.5", pct)).toBeNull();
    expect(storageValueError("101", pct)).toBe("Enter between 0 and 100 percent.");
    expect(storageValueError("-1", pct)).not.toBeNull();
    expect(storageValueError("", pct)).not.toBeNull();
  });
});

describe("StorageReportDialog", () => {
  afterEach(() => cleanup());
  const answer = {
    disk_space: {
      level: "amber",
      message: "Khipu's data disk is getting full.",
      volumes: [
        { label: "Khipu's data", path: "/x", free_bytes: 8_000_000_000, total_bytes: 250_000_000_000, free_percent: 3.2, level: "amber" },
        { label: "temporary files", path: "/t", free_bytes: 90_000_000_000, total_bytes: 250_000_000_000, free_percent: 36, level: "ok" },
      ],
    },
    storage: {
      total_bytes: 786_000_000,
      places: [
        { name: "replica", bytes: 665_000_000, path: "/r" },
        { name: "logs", bytes: 25_000_000, path: "/l" },
      ],
    },
  };

  it("runs khipu storage and says each volume and place in plain words", async () => {
    const run = vi.fn().mockResolvedValue(JSON.stringify(answer));
    const onClose = vi.fn();
    render(<StorageReportDialog open run={run} onClose={onClose} />);
    expect(await screen.findByText(/8\.0 GB free of 250 GB \(3\.2% free\)\. Running low\./)).toBeInTheDocument();
    expect(screen.getByText(/90\.0 GB free of 250 GB \(36% free\)\. Plenty of room\./)).toBeInTheDocument();
    expect(screen.getByText("665 MB")).toBeInTheDocument();
    expect(screen.getByText("786 MB")).toBeInTheDocument();
    expect(run).toHaveBeenCalledWith(["storage"]);
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("says so in plain words when the report cannot be read", async () => {
    const run = vi.fn().mockRejectedValue(new Error("Khipu is not installed"));
    render(<StorageReportDialog open run={run} onClose={() => {}} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not read the storage report: Khipu is not installed");
  });
});
