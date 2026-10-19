import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ProjectsSettings, askedCount, dayWords } from "../ProjectsSettings";
import type { ProjectsList } from "../ProjectsSettings";
import { useCliState } from "../settingsCli";

// Settings > Projects with the Tauri bridge mocked: the list, the "same
// project?" question, the kept-separate list, and each owner action's argv.

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke: (...args: unknown[]) => invokeMock(...args) }));
afterEach(() => {
  cleanup();
  invokeMock.mockReset();
});

const NEW = "mrkinglollipop/olivewood";
const OLD = "mrkinglollipop/church-harness";
const FORK = "lollipop-labs/olivewood";
const SITE = "mrkinglollipop/khipu-site";
const SITE_OLD = "mrkinglollipop/site-2025";

const name = (n: string, role: "canonical" | "alias", extra: object = {}) => ({
  name: n, role, open_commitments: 1, standing_decisions: 2, episodes: 398, ...extra,
});

const LIST: ProjectsList = {
  groups: [
    {
      canonical: NEW,
      names: [name(NEW, "canonical", { episodes: 14 }), name(OLD, "alias", { source: "github", created_at: "2020-10-12T13:40:00Z" })],
      totals: { open_commitments: 9, standing_decisions: 31, episodes: 412 },
    },
    {
      canonical: "field-notes",
      names: [name("field-notes", "canonical"), name("notes-scratch", "alias", { source: "checkout", created_at: "2020-10-12T13:40:00Z", episodes: 1 })],
      totals: { open_commitments: 2, standing_decisions: 0, episodes: 57 },
    },
  ],
  pairs: [
    { a: FORK, b: NEW, state: "asked", created_at: "2020-10-19T02:05:00Z" },
    { a: SITE, b: SITE_OLD, state: "kept_separate", decided_at: "2020-10-03T12:00:00Z" },
  ],
  pair_counts: {
    [NEW]: { open_commitments: 9, episodes: 412, last_used: "2020-10-19T12:00:00Z" },
    [FORK]: { open_commitments: 1, episodes: 14, last_used: "2020-10-17T12:00:00Z" },
  },
  sweep: { at: "2020-10-19T02:05:00Z", result: "ok" },
};

function Harness() {
  const state = useCliState<ProjectsList>("khipu_projects_list", true);
  return <ProjectsSettings active state={state} />;
}

function bridge(list: ProjectsList | Error, extra: Record<string, (a?: unknown) => unknown> = {}) {
  invokeMock.mockImplementation(async (cmd: string, args?: unknown) => {
    if (cmd === "khipu_projects_list") {
      if (list instanceof Error) throw list;
      return JSON.stringify({ ok: true, ...list });
    }
    if (extra[cmd]) return JSON.stringify(extra[cmd](args));
    return JSON.stringify({ ok: true });
  });
}

const calls = (cmd: string) => invokeMock.mock.calls.filter(([c]) => c === cmd).map(([, a]) => a);

it("lists each project with its older names, how Khipu knew, and no word 'alias'", async () => {
  bridge(LIST);
  render(<Harness />);
  const group = await screen.findByRole("group", { name: NEW });
  expect(within(group).getByText("Current name")).toBeTruthy();
  expect(within(group).getByText(OLD)).toBeTruthy();
  expect(within(group).getByText("GitHub")).toBeTruthy();
  expect(within(group).getByText(/GitHub says this repo is now mrkinglollipop\/olivewood\. 398 conversations were saved/)).toBeTruthy();
  const notes = screen.getByRole("group", { name: "field-notes" });
  expect(within(notes).getByText("Same checkout")).toBeTruthy();
  expect(within(notes).getByText(/folder, no GitHub/)).toBeTruthy();
  expect(screen.getByText("GitHub checked", { exact: false }).textContent).toMatch(/^GitHub checked /);
  expect(document.body.textContent).not.toMatch(/alias/i);
});

it("Undo on an older name removes that name from the app", async () => {
  bridge(LIST);
  render(<Harness />);
  const group = await screen.findByRole("group", { name: NEW });
  fireEvent.click(within(group).getByRole("button", { name: /Undo/ }));
  await waitFor(() => expect(calls("khipu_project_name_remove")).toEqual([{ older: OLD }]));
  // Two reads on open (the app's start-up read and the section's own), then one after the change.
  await waitFor(() => expect(calls("khipu_projects_list").length).toBe(3));
});

it("asks the pair with no default answer, offers the most recently used name, and Merge sends it", async () => {
  bridge(LIST);
  render(<Harness />);
  await screen.findByText("These look like the same project");
  const select = screen.getByLabelText("Show it as") as HTMLSelectElement;
  expect(select.value).toBe(NEW);
  fireEvent.change(select, { target: { value: FORK } });
  fireEvent.click(screen.getByRole("button", { name: "Merge" }));
  await waitFor(() => expect(calls("khipu_project_merge")).toEqual([{ a: FORK, b: NEW, showAs: FORK }]));
  expect(calls("khipu_project_keep_separate")).toEqual([]);
});

it("Keep separate sends the pair and nothing else", async () => {
  bridge(LIST);
  render(<Harness />);
  fireEvent.click(await screen.findByRole("button", { name: "Keep separate" }));
  await waitFor(() => expect(calls("khipu_project_keep_separate")).toEqual([{ a: FORK, b: NEW }]));
  expect(calls("khipu_project_merge")).toEqual([]);
});

it("a refused answer shows the CLI's words and leaves the question up", async () => {
  bridge(LIST, { khipu_project_merge: () => ({ ok: false, error: "not an asked pair; nothing was changed" }) });
  render(<Harness />);
  fireEvent.click(await screen.findByRole("button", { name: "Merge" }));
  expect((await screen.findByRole("alert")).textContent).toContain("not an asked pair");
  expect(screen.getByText("These look like the same project")).toBeTruthy();
});

it("Undo on a kept-separate pair undoes the owner-action line that recorded it", async () => {
  bridge(LIST, {
    khipu_owner_direction_actions: () => ({
      actions: [
        { id: "aaaaaaaa", at: "x", action: "project.keep_separate", target: `${SITE}|${SITE_OLD}`, state: "undone" },
        { id: "bbbbbbbb", at: "x", action: "project.keep_separate", target: `${SITE}|${SITE_OLD}`, state: "applied" },
      ],
    }),
  });
  render(<Harness />);
  await screen.findByText("Kept separate");
  const row = screen.getByText(SITE).closest(".oldname") as HTMLElement;
  fireEvent.click(within(row).getByRole("button", { name: /Undo/ }));
  await waitFor(() => expect(calls("khipu_owner_direction_undo")).toEqual([{ id: "bbbbbbbb" }]));
});

it("Undo on a kept-separate pair with no undoable record says so and changes nothing", async () => {
  bridge(LIST, { khipu_owner_direction_actions: () => ({ actions: [] }) });
  render(<Harness />);
  await screen.findByText("Kept separate");
  const row = screen.getByText(SITE).closest(".oldname") as HTMLElement;
  fireEvent.click(within(row).getByRole("button", { name: /Undo/ }));
  expect((await within(row).findByRole("alert")).textContent).toContain("no undoable record");
  expect(calls("khipu_owner_direction_undo")).toEqual([]);
});

it("Add another name sends the older and current name", async () => {
  bridge({ ...LIST, pairs: [] });
  render(<Harness />);
  fireEvent.click(await screen.findByRole("button", { name: "Add another name…" }));
  fireEvent.change(screen.getByLabelText("Older name"), { target: { value: " o/old " } });
  fireEvent.change(screen.getByLabelText("Current name"), { target: { value: "o/new" } });
  fireEvent.click(screen.getByRole("button", { name: "Add" }));
  await waitFor(() => expect(calls("khipu_project_name_add")).toEqual([{ older: "o/old", current: "o/new" }]));
});

it("the empty state says no project has more than one name, and hides the question and kept list", async () => {
  bridge({ groups: [], pairs: [], sweep: { result: null } });
  render(<Harness />);
  expect(await screen.findByText("No project has more than one name.")).toBeTruthy();
  expect(screen.queryByText("Kept separate")).toBeNull();
  expect(screen.queryByText("These look like the same project")).toBeNull();
  expect(screen.getByText("GitHub has not been checked yet")).toBeTruthy();
});

it("loading and error are their own states, not the empty one", async () => {
  invokeMock.mockImplementation(() => new Promise(() => {}));
  render(<Harness />);
  expect(await screen.findByText(/Reading your projects/)).toBeTruthy();
  expect(screen.queryByText(/No project has more than one name/)).toBeNull();
  cleanup();
  bridge(new Error("hub is not reachable"));
  render(<Harness />);
  expect((await screen.findByRole("alert")).textContent).toContain("Could not read your projects: hub is not reachable");
  expect(screen.queryByText(/No project has more than one name/)).toBeNull();
});

it("the GitHub line reads the last sweep and never runs one", async () => {
  bridge({ ...LIST, sweep: { at: "2020-10-12T02:05:00Z", result: "skipped", reason: "gh is not signed in" } });
  render(<Harness />);
  expect((await screen.findByText(/GitHub was not checked/)).textContent).toContain("gh is not signed in");
  expect(invokeMock.mock.calls.every(([c]) => c === "khipu_projects_list")).toBe(true);
});

it("counts the asked pairs for the section list and words days", () => {
  expect(askedCount(LIST)).toBe(1);
  expect(askedCount(null)).toBe(0);
  const now = new Date(2020, 9, 19, 15, 0);
  expect(dayWords(new Date(2020, 9, 19, 13, 40).toISOString(), now)).toMatch(/^Today, /);
  expect(dayWords(new Date(2020, 9, 12, 13, 40).toISOString(), now)).toMatch(/Oct/);
});
