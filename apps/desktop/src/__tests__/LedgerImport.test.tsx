// Settings -> Work -> Ledger -> Import a tracker... (docs/plans/2026-10-16-owed-and-ledger-ssot.md,
// slice 6, Design (e); mock docs/mockups/2026-10-15-work-view/settings-work.html), against
// `khipu ledger import --json` reports shaped as packages/cli/khipu/ledger_import.py
// run_import returns them.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { LedgerImport } from "../LedgerImport";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));
const openMock = vi.fn();
vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: (...args: unknown[]) => openMock(...args),
}));

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
  openMock.mockReset();
});

const INBOX = "/data/khipu/ledger-inbox";
const COPY = `${INBOX}/tracker.json`;
const REFUSAL = "Not read: the file is inside a private folder or a login folder, or is reached through a link.";

const COUNTS = {
  rows: 1068,
  history: 2670,
  with_parent: 267,
  by_state: { DONE: 600, QUEUED: 400, "NEEDS MATT": 68 },
  by_kind: { slice: 1000, goal: 68 },
  missing_parent: ["s-0003-x", "s-0011-x"],
};

function report(extra: Record<string, unknown> = {}) {
  return {
    ok: true,
    project: "owner/atlas",
    file: "tracker.json",
    digest: "abc",
    counts_in_file: COUNTS,
    masked: ["s-0007-x.evidence"],
    other_keys: ["updated", "version"],
    catch_up: false,
    in_inbox: true,
    result: "dry run",
    hub_checked: true,
    words: "Ready to import 1068 rows and 2670 history entries. Nothing was written.",
    ...extra,
  };
}

type Answers = { check?: unknown; import?: unknown; inbox?: string };

function serve(answers: Answers = {}) {
  invokeMock.mockImplementation(async (cmd: string, args?: { dryRun?: boolean }) => {
    if (cmd === "khipu_ledger_inbox") return answers.inbox ?? INBOX;
    if (cmd === "khipu_ledger_import") {
      const out = args?.dryRun ? answers.check ?? report() : answers.import;
      return JSON.stringify(out);
    }
    throw new Error(`unexpected ${cmd}`);
  });
}

const importCalls = () => invokeMock.mock.calls.filter(([c]) => c === "khipu_ledger_import");

async function openForm(projects = ["owner/atlas", "harbor"]) {
  render(<LedgerImport active projects={projects} />);
  const start = await screen.findByRole("button", { name: "Import a tracker…" });
  expect(start).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(start);
  expect(start).toHaveAttribute("aria-expanded", "true");
  return screen.getByRole("group", { name: "Import a tracker" });
}

async function choose(form: HTMLElement, path: string) {
  openMock.mockResolvedValueOnce(path);
  fireEvent.click(within(form).getByRole("button", { name: "Choose file…" }));
  await waitFor(() => expect(within(form).getByLabelText("File")).toHaveValue(path));
}

describe("Import a tracker", () => {
  it("opens Choose file in the ledger inbox and offers the known projects in order", async () => {
    serve();
    const form = await openForm(["owner/atlas", "harbor"]);
    await choose(form, COPY);
    expect(openMock).toHaveBeenCalledWith(
      expect.objectContaining({ directory: false, multiple: false, defaultPath: INBOX }),
    );
    const options = Array.from(document.querySelectorAll("datalist option")).map((o) => (o as HTMLOptionElement).value);
    expect(options).toEqual(["owner/atlas", "harbor"]);
    expect(within(form).getByRole("button", { name: "Check this file" })).toBeDisabled();
    expect(within(form).getByRole("button", { name: "Import" })).toBeDisabled();
  });

  it("says a copy in the inbox is a hard cut, and says nothing for a file elsewhere", async () => {
    serve();
    const form = await openForm();
    await choose(form, COPY);
    expect(
      within(form).getByText(
        "This file is a copy in Khipu's ledger inbox. A copy never changes, so Bring in the changes is not used for it: the switch to Khipu is a hard cut.",
      ),
    ).toBeInTheDocument();
    await choose(form, "/work/atlas/tracker/tracker.json");
    expect(within(form).queryByText(/hard cut/)).toBeNull();
  });

  it("Check this file runs the dry run and shows what the file holds", async () => {
    serve();
    const form = await openForm();
    await choose(form, COPY);
    fireEvent.change(within(form).getByLabelText("Project"), { target: { value: "owner/atlas" } });
    fireEvent.click(within(form).getByRole("button", { name: "Check this file" }));
    expect(await within(form).findByText("Found 1,068 rows and 2,670 history entries. Nothing is written until you choose Import.")).toBeInTheDocument();
    expect(importCalls()).toEqual([["khipu_ledger_import", { project: "owner/atlas", path: COPY, dryRun: true }]]);
    const states = within(form).getByRole("table", { name: "Rows in each state" });
    expect(within(states).getByRole("row", { name: "DONE 600" })).toBeInTheDocument();
    expect(within(states).getByRole("row", { name: "NEEDS MATT 68" })).toBeInTheDocument();
    const kinds = within(form).getByRole("table", { name: "Rows of each kind" });
    expect(within(kinds).getByRole("row", { name: "slice 1,000" })).toBeInTheDocument();
    expect(within(form).getByText("267 rows have a parent.")).toBeInTheDocument();
    expect(
      within(form).getByText("2 rows name a parent that isn't in the file; they are kept as they are: s-0003-x, s-0011-x."),
    ).toBeInTheDocument();
    expect(
      within(form).getByText(
        "1 field looks like a key or password; that part will be hidden in Khipu and stays only in the old file: s-0007-x.evidence.",
      ),
    ).toBeInTheDocument();
    expect(within(form).getByText("Not rows, so not imported: updated, version.")).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Import" })).toBeEnabled();
  });

  it("shows the ids already present when the dry run refuses", async () => {
    const words = "Nothing was imported: these ids already exist in owner/atlas: s-1, s-2. Choose a different project or a new one.";
    serve({ check: report({ ok: false, result: "refused", reason: "exists", existing_ids: ["s-1", "s-2"], words }) });
    const form = await openForm();
    await choose(form, COPY);
    fireEvent.change(within(form).getByLabelText("Project"), { target: { value: "owner/atlas" } });
    fireEvent.click(within(form).getByRole("button", { name: "Check this file" }));
    expect(await within(form).findByText(words)).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Import" })).toBeDisabled();
  });

  it("Import runs the real import and shows the verification", async () => {
    const words =
      "Imported 1068 rows and 2670 history entries; every count matches and the round trip gives the file back except the masked fields listed.";
    serve({ import: report({ result: "done", import_id: 4, words, check: { ok: true } }) });
    const form = await openForm();
    await choose(form, COPY);
    fireEvent.change(within(form).getByLabelText("Project"), { target: { value: "a new one" } });
    fireEvent.click(within(form).getByRole("button", { name: "Check this file" }));
    await within(form).findByText(/^Found 1,068 rows/);
    fireEvent.click(within(form).getByRole("button", { name: "Import" }));
    expect(await within(form).findByText(words)).toBeInTheDocument();
    expect(importCalls()[1]).toEqual(["khipu_ledger_import", { project: "a new one", path: COPY, dryRun: false }]);
    expect(within(form).getByRole("status")).toHaveTextContent(words);
  });

  it("says why an import was rolled back", async () => {
    const words = "Nothing was imported: the check after writing found differences, so it was undone. It found counts that differ: rows.";
    serve({ import: report({ ok: false, result: "rolled back", reason: "mismatch", words }) });
    const form = await openForm();
    await choose(form, COPY);
    fireEvent.change(within(form).getByLabelText("Project"), { target: { value: "harbor" } });
    fireEvent.click(within(form).getByRole("button", { name: "Check this file" }));
    await within(form).findByText(/^Found 1,068 rows/);
    fireEvent.click(within(form).getByRole("button", { name: "Import" }));
    expect(await within(form).findByText(words)).toBeInTheDocument();
  });

  it("shows the importer's plain refusal for a file inside a login folder", async () => {
    serve({ check: { ok: false, error: REFUSAL, reason: "refused" } });
    const form = await openForm();
    await choose(form, "/home/someone/.agent-login/projects/a/tracker.json");
    fireEvent.change(within(form).getByLabelText("Project"), { target: { value: "harbor" } });
    fireEvent.click(within(form).getByRole("button", { name: "Check this file" }));
    expect(await within(form).findByText(REFUSAL)).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Import" })).toBeDisabled();
  });

  it("a changed file or project asks for a new check before Import", async () => {
    serve();
    const form = await openForm();
    await choose(form, COPY);
    fireEvent.change(within(form).getByLabelText("Project"), { target: { value: "harbor" } });
    fireEvent.click(within(form).getByRole("button", { name: "Check this file" }));
    await within(form).findByText(/^Found 1,068 rows/);
    fireEvent.change(within(form).getByLabelText("Project"), { target: { value: "owner/atlas" } });
    expect(within(form).getByRole("button", { name: "Import" })).toBeDisabled();
    expect(within(form).queryByText(/^Found 1,068 rows/)).toBeNull();
  });
});
