// Work view phase 5: Settings, Work (mock settings-work.html). Every write
// goes through a fixed-argv command and the page reads again after it.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { WorkSettings, WORK_LIMITS, foundWords } from "../WorkSettings";
import type { WorkState } from "../work";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));
const pickMock = vi.fn();
vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: (...args: unknown[]) => pickMock(...args),
}));

const NOW = Date.parse("2026-10-15T20:00:00Z");
const ago = (s: number) => new Date(NOW - s * 1000).toISOString().replace(/\.\d+Z$/, "Z");

type Entry = { project: string; name?: string; file: string; on: boolean; show_now: boolean };
let entries: Entry[];
let workSettings: Record<string, { value: unknown; default: unknown }>;
let projects: Array<Record<string, unknown>>;
let hidden: Array<Record<string, unknown>>;
const calls: Array<[string, Record<string, unknown> | undefined]> = [];

function config() {
  return {
    work: { ...workSettings, work_projects: { value: entries, default: [] } },
    ledger: {
      ledger_title_chars: { value: 2000, default: 2000 },
      ledger_stale_after: { value: 604800, default: 604800 },
      ledger_id_chars: { value: 80, default: 80 },
      ledger_evidence_chars: { value: 1000, default: 1000 },
      ledger_purge_after: { value: 2592000, default: 2592000 },
    },
    work_suggested: [{ name: "Pebble", file: "/runners/pebble/state/work-summary.json" }],
  };
}

function work(): WorkState {
  const board = (project: string, state: string, extra: Record<string, unknown> = {}) => ({
    project,
    name: project,
    source: { state, as_of: ago(12), problem: null, ...extra },
    jobs: [{}, {}, {}],
    prs: Array.from({ length: 12 }, () => ({})),
    trains: [{}],
    merged: [],
    ledger: null,
    flagged: 0,
  });
  return {
    data: {
      projects: [board("acme/atlas", "ok"), board("acme/quarry", "too_big", { problem: "The summary file is 412 KB, over the 256 KB limit." })],
      waiting: [],
    },
    error: null,
    loading: false,
    readAt: NOW,
    load: vi.fn(async () => {}),
  } as unknown as WorkState;
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  calls.length = 0;
  entries = [
    { project: "acme/atlas", name: "Atlas", file: "/runners/atlas/state/work-summary.json", on: true, show_now: true },
    { project: "acme/quarry", file: "/runners/quarry/state/work-summary.json", on: true, show_now: false },
    { project: "acme/harbor", file: "/runners/harbor/state/work-summary.json", on: false, show_now: true },
  ];
  workSettings = {};
  for (const l of WORK_LIMITS) for (const f of l.fields) if (!f.key.startsWith("ledger_")) workSettings[f.key] = { value: f.key === "work_stale_after" ? "auto" : 50, default: 50 };
  workSettings.work_file_max_bytes = { value: 262144, default: 262144 };
  workSettings.work_gone_after = { value: 86400, default: 86400 };
  projects = [
    { project: "acme/atlas", name: "Atlas", hidden: false, created_at: "2026-10-12T08:40:00Z", rows: 1120, open: 19, needs_owner: 9 },
    { project: "acme/lumen", name: "Lumen", hidden: true, created_at: null, rows: 48, open: 3, needs_owner: 1 },
  ];
  hidden = [{ id: "42", text: "Look at the nightly report size again", project: "acme/harbor", kind: "followup" }];
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    calls.push([cmd, args]);
    if (cmd === "khipu_config_show") return JSON.stringify(config());
    if (cmd === "khipu_ledger_projects") return JSON.stringify({ ok: true, projects });
    if (cmd === "khipu_work_hidden") return JSON.stringify({ ok: true, hidden });
    if (cmd === "khipu_work_check")
      return JSON.stringify({
        ok: true, problems: [], counts: { jobs: 1, prs: 4, trains: 0 },
        project: String(args?.path).includes("pebble") ? "pebble" : "lumen", written_at: ago(6),
      });
    if (cmd === "khipu_config_set") {
      if (args?.key === "work_projects") entries = JSON.parse(String(args.value));
      else workSettings[String(args?.key)] = { value: args?.value === "auto" ? "auto" : Number(args?.value), default: 50 };
      return JSON.stringify({ ok: true });
    }
    if (cmd === "khipu_ledger_project_set") {
      const p = projects.find((x) => x.project === args?.project)!;
      if (typeof args?.hidden === "boolean") p.hidden = args.hidden;
      if (typeof args?.name === "string") p.name = args.name || p.project;
      return JSON.stringify({ ok: true });
    }
    if (cmd === "khipu_work_hide") {
      hidden = [];
      return JSON.stringify({ ok: true });
    }
    throw new Error(`unexpected ${cmd}`);
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  invokeMock.mockReset();
  pickMock.mockReset();
});

const setCalls = (cmd: string) => calls.filter(([c]) => c === cmd).map(([, a]) => a);

// Every button stays disabled while a write and the read after it run, and a
// limit's Change button until the first read lands; a click on a disabled
// button does nothing, so each click waits for its button to be enabled.
const enabled = (name: string, role = "button", root: HTMLElement = document.body) =>
  waitFor(() => {
    const el = within(root).getByRole(role, { name });
    expect(el).toBeEnabled();
    return el;
  });

describe("Settings, Work", () => {
  it("lists the ledger's projects with their counts and renames and hides as the owner", async () => {
    render(<WorkSettings active work={work()} />);
    const list = await screen.findByRole("list", { name: "Ledger projects" });
    expect(within(list).getByText("1,120 rows · 19 open · 9 need you")).toBeInTheDocument();
    expect(within(list).getByText("48 rows · 3 open · 1 needs you")).toBeInTheDocument();
    expect(within(list).getByText("Hidden from the Work tab")).toBeInTheDocument();
    fireEvent.click(await enabled("Hide Atlas"));
    await waitFor(() => expect(setCalls("khipu_ledger_project_set")).toContainEqual({ project: "acme/atlas", name: null, hidden: true }));
    fireEvent.click(await enabled("Show Lumen"));
    await waitFor(() => expect(setCalls("khipu_ledger_project_set")).toContainEqual({ project: "acme/lumen", name: null, hidden: false }));
    fireEvent.click(await enabled("Rename Atlas"));
    const field = screen.getByLabelText("Name");
    fireEvent.change(field, { target: { value: "Atlas app" } });
    fireEvent.submit(field.closest("form")!);
    await waitFor(() => expect(setCalls("khipu_ledger_project_set")).toContainEqual({ project: "acme/atlas", name: "Atlas app", hidden: null }));
  });

  it("lists the Work tab's projects even when the ledger has none (the installed app said No projects yet)", async () => {
    projects = [];
    const state = work();
    (state.data as unknown as { waiting: unknown[] }).waiting = [
      { source: "commitment", key: "c1", project: "acme/church", id: "1", title: "a", kind: "blocker", since: ago(60), blocking: true, actions: [], masked: false },
      { source: "commitment", key: "c2", project: "acme/church", id: "2", title: "b", kind: "followup", since: ago(60), blocking: false, actions: [], masked: false },
      { source: "commitment", key: "c3", project: null, id: "3", title: "c", kind: "followup", since: ago(60), blocking: false, actions: [], masked: false },
      { source: "commitment", key: "c4", project: null, id: "4", title: null, kind: "blocker", since: ago(60), blocking: true, actions: [], masked: true },
    ];
    render(<WorkSettings active work={state} />);
    const list = await screen.findByRole("list", { name: "Projects on the Work tab with no ledger rows" });
    expect(screen.queryByText("No projects yet.")).toBeNull();
    expect(within(list).getByText("church")).toBeInTheDocument();
    expect(within(list).getByText("2 waiting on you")).toBeInTheDocument();
    expect(within(list).getByText("acme/atlas")).toBeInTheDocument();
    expect(within(list).getByText("acme/quarry")).toBeInTheDocument();
    expect(screen.getByText("Also waiting on you: 1 under Name withheld and 1 under No project.")).toBeInTheDocument();
  });

  it("shows each summary file's state and turns one off, on and removes it", async () => {
    render(<WorkSettings active work={work()} />);
    const list = await screen.findByRole("list", { name: "Summary files" });
    expect(within(list).getByText(/Updated 12 s ago · 3 jobs, 12 pull requests, 1 release train/)).toBeInTheDocument();
    expect(within(list).getByText(/over the 256 KB limit/)).toBeInTheDocument();
    expect(within(list).getByText("Turned off")).toBeInTheDocument();
    fireEvent.click(await enabled("Turn off the summary file for Atlas"));
    await waitFor(() => expect(entries.find((e) => e.project === "acme/atlas")?.on).toBe(false));
    fireEvent.click(await enabled("Turn on the summary file for harbor"));
    await waitFor(() => expect(entries.find((e) => e.project === "acme/harbor")?.on).toBe(true));
    fireEvent.click(await enabled("Remove the summary file for quarry"));
    await waitFor(() => expect(entries.map((e) => e.project)).toEqual(["acme/atlas", "acme/harbor"]));
  });

  it("switches off what a project's jobs say they are doing", async () => {
    render(<WorkSettings active work={work()} />);
    fireEvent.click(await enabled("Show what Atlas jobs say they are doing", "switch"));
    await waitFor(() => expect(entries.find((e) => e.project === "acme/atlas")?.show_now).toBe(false));
  });

  it("adds a summary file after a first read, choosing a new project named by the file", async () => {
    pickMock.mockResolvedValue("/runners/lumen/state/work-summary.json");
    render(<WorkSettings active work={work()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Add a summary file…" }));
    const form = screen.getByRole("form", { name: "Add a summary file" });
    fireEvent.change(within(form).getByLabelText("Project"), { target: { value: "\u0000new" } });
    fireEvent.click(within(form).getByRole("button", { name: "Choose file…" }));
    expect(await within(form).findByText("Found 1 job, 4 pull requests and no release trains, updated 6 s ago.")).toBeInTheDocument();
    // The key is filled in by an effect that runs after the read's words render.
    await waitFor(() => expect(within(form).getByLabelText("The new project's key")).toHaveValue("lumen"));
    fireEvent.submit(form);
    await waitFor(() =>
      expect(entries[entries.length - 1]).toEqual({ project: "lumen", file: "/runners/lumen/state/work-summary.json", on: true, show_now: true }),
    );
  });

  it("says when the file is for another project and still lets it be saved", async () => {
    render(<WorkSettings active work={work()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Add the Pebble runner" }));
    const form = screen.getByRole("form", { name: "Add a summary file" });
    expect(await within(form).findByText(/This file says it is for pebble, not acme\/lumen\./)).toBeInTheDocument();
    expect(within(form).getByLabelText("Summary file")).toHaveValue("/runners/pebble/state/work-summary.json");
    expect(within(form).getByRole("button", { name: "Add" })).toBeEnabled();
  });

  it("changes a limit to No limit and the out-of-date time to a fixed time", async () => {
    render(<WorkSettings active work={work()} />);
    fireEvent.click(await enabled("Change: Largest summary file Khipu reads"));
    const form = screen.getByRole("form", { name: "Change: Largest summary file Khipu reads" });
    const select = within(form).getByLabelText("Largest file");
    expect(within(select).getByRole("option", { name: "256 KB (the default)" })).toBeInTheDocument();
    fireEvent.change(select, { target: { value: "0" } });
    fireEvent.submit(form);
    await waitFor(() => expect(setCalls("khipu_config_set")).toContainEqual({ key: "work_file_max_bytes", value: "0" }));
    const stale = await enabled("Change: A summary is out of date after");
    expect(stale.parentElement).toHaveTextContent("Automatic");
    fireEvent.click(stale);
    const sform = screen.getByRole("form", { name: "Change: A summary is out of date after" });
    fireEvent.change(within(sform).getByLabelText("Out of date after"), { target: { value: "600" } });
    fireEvent.submit(sform);
    await waitFor(() => expect(setCalls("khipu_config_set")).toContainEqual({ key: "work_stale_after", value: "600" }));
  });

  it("offers every work and ledger cap and time", () => {
    const keys = WORK_LIMITS.flatMap((l) => l.fields.map((f) => f.key)).sort();
    expect(keys).toEqual(
      [
        "work_file_max_bytes", "work_max_jobs", "work_max_prs", "work_max_trains", "work_max_runs", "work_line_chars",
        "work_stale_after", "work_job_quiet_after", "work_gone_after", "work_finished_for", "work_merged_for",
        "work_clock_slack", "work_refresh", "ledger_id_chars", "ledger_title_chars", "ledger_evidence_chars",
        "ledger_stale_after", "ledger_purge_after",
      ].sort(),
    );
    for (const l of WORK_LIMITS) {
      for (const f of l.fields) {
        if (!["work_clock_slack", "work_refresh"].includes(f.key)) expect(f.options, f.key).toContain(0);
      }
    }
  });

  it("unhides an Owed item", async () => {
    render(<WorkSettings active work={work()} />);
    const list = await screen.findByRole("list", { name: "Hidden items" });
    expect(within(list).getByText("Owed · harbor")).toBeInTheDocument();
    fireEvent.click(await enabled("Unhide Look at the nightly report size again", "button", list));
    await waitFor(() => expect(setCalls("khipu_work_hide")).toContainEqual({ id: "42", hide: false }));
    expect(await screen.findByText("Nothing is hidden.")).toBeInTheDocument();
  });

  it("words a first read that found nothing", () => {
    expect(foundWords({ ok: false, problems: ["No file at that path."] }, new Date(NOW))).toBe("No file at that path.");
  });
});
