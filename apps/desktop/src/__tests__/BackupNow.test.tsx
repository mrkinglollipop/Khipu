// Settings › Backups › Back up now: the same `backup-local --out=~/Downloads`
// the older Data screen ran, with progress, a result line, and an honest error.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { BackupsPage } from "../settings/pages/Backups";
import type { HealthState, Loaded, MacsDoc } from "../settings/data";

const macs: Loaded<MacsDoc> = { data: { macs: [] }, error: null, loading: false };
const health: HealthState = { data: null, at: null, refreshing: false, error: null };
afterEach(cleanup);

describe("Back up now", () => {
  it("runs backup-local into Downloads, shows progress, then where it went, and refreshes the page", async () => {
    let finish: (v: string) => void = () => {};
    const run = vi.fn(() => new Promise<string>((r) => { finish = r; }));
    const done = vi.fn();
    render(<BackupsPage macs={macs} health={health} run={run} onBackedUp={done} />);
    fireEvent.click(screen.getByRole("button", { name: "Back up now" }));
    expect(run).toHaveBeenCalledWith(["backup-local", "--out=~/Downloads"]);
    expect(screen.getByRole("button", { name: "Backing up…" })).toBeDisabled();
    expect(screen.getByText(/Writing a zip/)).toBeTruthy();
    expect(done).not.toHaveBeenCalled();
    finish(JSON.stringify({ ok: true, archive: "/Users/x/Downloads/khipu-local-1.zip", files: 12 }));
    await waitFor(() => expect(screen.getByText(/Backed up 12 files to \/Users\/x\/Downloads\/khipu-local-1\.zip/)).toBeTruthy());
    expect(done).toHaveBeenCalledTimes(1);
  });

  it("shows the error and claims nothing when the backup fails", async () => {
    const done = vi.fn();
    const run = vi.fn(async () => JSON.stringify({ ok: false, error: "the backup could not be written: disk full" }));
    render(<BackupsPage macs={macs} health={health} run={run} onBackedUp={done} />);
    fireEvent.click(screen.getByRole("button", { name: "Back up now" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(/did not finish: the backup could not be written: disk full/));
    expect(screen.queryByText(/Backed up/)).toBeNull();
    expect(done).not.toHaveBeenCalled();
  });

  it("shows a thrown error too", async () => {
    const run = vi.fn(async () => { throw new Error("khipu worker died"); });
    render(<BackupsPage macs={macs} health={health} run={run} />);
    fireEvent.click(screen.getByRole("button", { name: "Back up now" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(/khipu worker died/));
    expect(screen.queryByText(/Backed up/)).toBeNull();
  });
});
