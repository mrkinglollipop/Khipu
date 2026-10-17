// Routing truth phase 4, second half (docs/plans/2026-10-14-routing-truth.md):
// Settings -> Routing as in docs/mockups/2026-10-14-routing/settings-routing.html.
// Each control renders, checks what it can before it writes, calls its
// fixed-argv command with the right fields, re-reads, and shows the CLI's
// own error in plain words when the CLI refuses. Times are New York local.
(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { RoutingSettings } from "../RoutingSettings";
import { brokenRules, ruleResultLine } from "../routing";
import type { RouteAccount, RouteView } from "../routing";
import { changeDays, chosenEnd, durationWords, endChoices, isoLocal, limitWords, ruleOrigin, settingWords, tableErrorWords } from "../routingWords";
// The stylesheet as written, for the headless Chrome layout check (vitest
// hands CSS imports over empty). The app has no @types/node.
// @ts-expect-error node:fs has no types here
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
// @ts-expect-error node:child_process has no types here
import { spawnSync } from "node:child_process";
// @ts-expect-error node:os has no types here
import { tmpdir } from "node:os";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

// Wed 2026-10-14, 11:40 AM in New York (the mock's time).
const MORNING = new Date("2026-10-14T15:40:00Z");
const KEYS = ["claude|", "claude|/logins/claude-second", "codex|", "grok|", "muse|"];

function acct(label: string, harness: string, extra: Partial<RouteAccount>): RouteAccount {
  return {
    key: `${harness}|${label === "Claude second" ? "/logins/claude-second" : ""}`,
    label,
    harness,
    state: "unknown",
    checkable: harness !== "muse",
    models: [],
    witness: null,
    disagreement: null,
    last_seen: null,
    ...extra,
  };
}

function view(): RouteView {
  return {
    record: true,
    version: 12,
    checked_at: "2026-10-14T15:40:00Z",
    accounts: [
      acct("Claude main", "claude", {
        state: "limited", until: "2026-10-14T17:52:00Z", basis: "cli",
        witness: { source: "probe", written_by: { kind: "probe" }, at: "2026-10-14T15:36:00Z" },
      }),
      acct("Claude second", "claude", {
        state: "limited", until: "2026-10-14T18:52:00Z", basis: "cli",
        witness: { id: "r-runner", source: "runner", written_by: { kind: "runner", name: "Project A" }, at: "2026-10-14T15:13:00Z", message_time: true },
        disagreement: { id: "e-open", state: "open", source: "report", by: "a Claude Code conversation in project-b", at: "2026-10-14T15:31:00Z" },
      }),
      acct("Codex", "codex", {
        state: "open",
        witness: { source: "probe", written_by: { kind: "probe" }, at: "2026-10-14T15:36:00Z" },
      }),
      acct("Grok Build", "grok", {
        state: "paused", until: "2026-10-16T13:00:00Z", basis: "owner",
        witness: { source: "owner", written_by: { kind: "owner" }, at: "2026-10-13T22:05:00Z" },
      }),
    ],
    holds: [
      { id: "h1", scope_label: "this Mac", reason: "machine_busy", text: "No new launches while the Mac is busy (load over 75)",
        until: "2026-10-15T13:40:00Z", owner: false, by: "a Claude Code conversation in project-a", at: "2026-10-14T13:40:00Z" },
    ],
    note: { text: "We don't need cursor. Just grok.", at: "2026-10-13T14:00:00Z" },
    rules: [
      { n: 3, text: "Never a -fast tier.", seeded: true, confirmed: false, source: "from your routing table", date: "2026-10-13" },
      { n: 7, text: "Khipu's routing record is guidance, not a gate.", seeded: true, confirmed: true, source: "from your routing table", date: "2026-10-13" },
    ],
    watched_rules: [3],
    unmatched: [],
    sources: {
      runners: [
        { name: "Project A", path: "/runners/a", heartbeat_at: "2026-10-14T15:39:40Z", interval: 20, silent: false, unreadable: false,
          lanes: ["codex", "main", "secondary"], unmatched_lanes: [], files: {} },
        { name: "Project B", path: "/runners/b", heartbeat_at: "2026-10-14T14:02:00Z", interval: 30, silent: true, unreadable: false,
          lanes: ["opus-batch", "secondary"], unmatched_lanes: ["opus-batch"],
          files: { heartbeat: { error: null, good_at: "2026-10-14T14:02:00Z" } } },
      ],
      table: { stamped_at: "2026-10-14T13:20:00Z", changed_at: "2026-10-14T13:20:00Z" },
    },
    old_switches: { env: false, file: false },
    changes: [
      { version: 11, at: "2026-10-13T22:05:00Z", entities: ["account:grok|"], items: [
        // Owner items are `khipu route --json` output, captured in a temp
        // HOME after the app's own argv (fix round 1, F1).
        { entity: "account:grok|", now: { basis: "owner", disagreement: null, label: "Grok Build", last_seen: null, models: [], reason: null, scope: "all", state: "paused", until: "2026-10-16T13:00:00Z" },
          source: "owner", via: "app", was: { basis: null, disagreement: null, label: "Grok Build", last_seen: null, models: [], reason: null, scope: "all", state: "unknown", until: null }, by: "you" },
      ] },
      { version: 12, at: "2026-10-14T15:36:00Z", entities: ["account:claude|", "rule:6"], items: [
        { entity: "account:claude|", now: { label: "Claude main", state: "limited", until: "2026-10-14T17:52:00Z", basis: "cli" },
          was: { label: "Claude main", state: "open" }, source: "probe", by: "Khipu usage probe" },
        { entity: "rule:6", n: 6, now: null, source: null, via: "app", was: { seeded: false, text: "Never send keys to Codex." }, by: "you" },
      ] },
    ],
    rule_checks: [],
    warn_after: { disagreement: 1800, not_refreshed: 300 },
    report_default_limit: 1800,
  };
}

function setting(value: unknown, def: unknown = value) {
  return { value, default: def, kind: "x", source: "default" };
}

function config(extra: Record<string, unknown> = {}) {
  return {
    routing: {
      routing_stale_after: setting(43200),
      routing_conversation_hold: setting(86400),
      routing_conversation_hold_max: setting(604800),
      routing_owner_hold_reminder: setting(7776000),
      routing_disagreement_warn_after: setting(1800),
      routing_summary_chars: setting(1500),
      routing_prompt_chars: setting(400),
      routing_hold_chars: setting(300),
      routing_line_chars: setting(200),
      routing_prompt_budget_ms: setting(50),
      routing_resend_after_versions: setting(20),
      routing_resend_after: setting(21600),
      routing_not_refreshed_after: setting(300),
      routing_push_start: setting(true),
      routing_push_prompt: setting(true),
      routing_push_delegation: setting(true),
      routing_dispatch_scripts: setting([], []),
      routing_reset_check: setting(true),
      routing_reset_check_wait: setting(120),
      routing_reset_check_retries: setting([600, 1800]),
      routing_busy_load: setting(75),
      routing_busy_wait: setting(300),
      routing_busy_tries: setting(3),
      routing_sources: setting([
        { name: "Project A", kind: "lanes", path: "/runners/a" },
        { name: "Project B", kind: "lanes", path: "/runners/b" },
      ], []),
      routing_lane_map: setting({ "Project A": { codex: "Codex", main: "Claude main", secondary: "Claude second" },
        "Project B": { secondary: "Claude second" } }, {}),
      routing_table: setting("/notes/routing/ROUTING-NOW.md", null),
      ...extra,
    },
  };
}

type Answer = string | Record<string, unknown> | ((args: Record<string, unknown> | undefined) => unknown);
let answers: Record<string, Answer>;

// Settings clarity phase 3 folds the page into groups; these tests read
// every group open, every account line open and Telling's inner folds open.
const ALL_OPEN = [
  "accounts", "priority", "rules", "holds", "telling", "resets", "runners", "table", "changes", "sub:hears", "sub:sizes",
  ...KEYS.map((k) => `account:${k}`),
];

// jsdom here has no usable localStorage; an in-memory one stands in.
function memoryStorage() {
  const m = new Map<string, string>();
  return {
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => void m.set(k, String(v)),
    removeItem: (k: string) => void m.delete(k),
    clear: () => m.clear(),
  };
}

beforeEach(() => {
  vi.stubGlobal("localStorage", memoryStorage());
  localStorage.setItem("khipu.routing.open", JSON.stringify(ALL_OPEN));
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(MORNING);
  answers = {
    khipu_route_view: view(),
    khipu_config_show: config(),
    khipu_features_show: { features: { routing_push: { enabled: true, source: "file" } } },
  };
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    const a = answers[cmd] ?? { ok: true };
    const out = typeof a === "function" ? await a(args) : a;
    return typeof out === "string" ? out : JSON.stringify(out);
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  invokeMock.mockReset();
});

function calls(cmd: string) {
  return invokeMock.mock.calls.filter((c) => c[0] === cmd).map((c) => c[1]);
}

async function page() {
  render(<RoutingSettings active />);
  await screen.findByRole("list", { name: "Accounts" });
}

function row(listName: string, start: string): HTMLElement {
  const list = screen.getByRole("list", { name: listName });
  const found = within(list).getAllByRole("listitem").find((r) => r.textContent?.startsWith(start));
  if (!found) throw new Error(`no row ${start} in ${listName}`);
  return found;
}

describe("Settings -> Routing: the words and choices", () => {
  it("words times, lengths and a rule's origin in local time", () => {
    expect(isoLocal(new Date("2026-10-16T13:00:00Z"))).toBe("2026-10-16T09:00:00-04:00");
    expect(durationWords(120)).toBe("2 minutes");
    expect(durationWords(43200)).toBe("12 hours");
    expect(durationWords(7776000)).toBe("90 days");
    expect(limitWords(1500, "chars")).toBe("1,500 characters");
    expect(limitWords(0, "chars", "No limit")).toBe("No limit");
    expect(ruleOrigin("from your routing table", "2026-10-13", MORNING)).toBe("From your routing table, Oct 13");
  });

  it("offers the ends a pause or a hold can take, and checks a typed one", () => {
    const choices = endChoices(MORNING, 1);
    expect(choices.map((c) => c.label)).toEqual([
      "In 1 hour, 12:40 PM", "Tonight, 9:00 PM", "Tomorrow, 9:00 AM", "Friday, 9:00 AM", "A date and time…",
      "Until I clear it",
    ]);
    expect(chosenEnd("tomorrow", "", choices, MORNING)).toEqual({ iso: "2026-10-15T09:00:00-04:00" });
    expect(chosenEnd("never", "", choices, MORNING)).toEqual({ iso: null });
    expect(chosenEnd("custom", "", choices, MORNING)).toEqual({ error: "Choose the date and time it ends." });
    expect(chosenEnd("custom", "2026-10-01T09:00", choices, MORNING)).toEqual({ error: "Choose a time that hasn't passed yet." });
    expect(chosenEnd("custom", "2026-10-20T08:30", choices, MORNING)).toEqual({ iso: "2026-10-20T08:30:00-04:00" });
  });

  it("lists recent changes newest first, one list per day", () => {
    const days = changeDays(view(), MORNING);
    expect(days.map((d) => d.label)).toEqual(["Today", "Yesterday"]);
    expect(days[0].rows.map((r) => `${r.subject} ${r.text}`)).toEqual([
      "Standing rule 6 removed", "Claude main limit reached, back at 1:52 PM",
    ]);
    expect(days[0].rows[1].aside).toBe("(was open)");
    expect(days[0].rows[1].who).toBe("The plan check");
    expect(days[0].rows[0].restore).toBe(6);
    expect(days[1].rows[0]).toMatchObject({ subject: "Grok Build", text: "paused by you until Fri 9:00 AM", who: "You, in Settings" });
  });

  it("words pending-rule history as added, confirmed, undone, or dismissed", () => {
    const at = "2026-10-14T15:40:00Z";
    const rows = changeDays({ changes: [
      { version: 1, at, entities: ["rule:1"], items: [{ entity: "rule:1", now: { text: "Added", pending: true, pending_kind: "added", pending_by: "a Codex conversation" }, was: null }] },
      { version: 2, at, entities: ["rule:1"], items: [{ entity: "rule:1", now: { text: "Added", pending: false }, was: { text: "Added", pending: true } }] },
      { version: 3, at, entities: ["rule:1"], items: [{ entity: "rule:1", now: { text: "Original", pending: false }, was: { text: "Changed", pending: true, prior_text: "Original" } }] },
      { version: 4, at, entities: ["rule:1"], items: [{ entity: "rule:1", now: null, was: { text: "Temporary", pending: true, pending_kind: "added" } }] },
    ] } as RouteView, MORNING).flatMap((day) => day.rows);
    expect(rows.map((r) => r.text)).toEqual(["dismissed", "undone", "confirmed", "added by a Codex conversation at the owner's request; the owner has not confirmed it yet"]);
  });

  it("words the owner's own edit or removal of a pending change as that, and shows a pending change's new words", () => {
    const at = "2026-10-14T15:40:00Z";
    const rows = changeDays({ changes: [
      { version: 1, at, entities: ["rule:1"], items: [{ entity: "rule:1", now: { text: "New", pending: true, pending_kind: "changed", pending_by: "a Codex conversation", prior_text: "Old" }, was: { text: "Old" } }] },
      { version: 2, at, entities: ["rule:1"], items: [{ entity: "rule:1", now: { text: "Owner's own", pending: false }, was: { text: "New", pending: true, pending_kind: "changed", prior_text: "Old" } }] },
      { version: 3, at, entities: ["rule:1"], items: [{ entity: "rule:1", now: null, was: { text: "Again", pending: true, pending_kind: "changed", prior_text: "Owner's own" } }] },
    ] } as RouteView, MORNING).flatMap((day) => day.rows);
    expect(rows.map((r) => r.text)).toEqual(["removed", "changed", "changed by a Codex conversation at the owner's request; the owner has not confirmed it yet"]);
    expect(rows[2].aside).toBe("“New”; it replaced “Old”");
  });
});

// A lift, an ended note and an ended hold, each made in the app: real
// `khipu route --json` change items (temp HOME, the app's argv).
const OWNER_ENDINGS = [
  { version: 13, at: "2026-10-14T15:37:00Z", entities: ["account:muse|"], items: [
    { entity: "account:muse|", now: { basis: null, disagreement: null, label: "Muse", last_seen: null, models: [], reason: null, scope: "all", state: "unknown", until: null },
      source: "owner", via: "app", was: { basis: "owner", disagreement: null, label: "Muse", last_seen: null, models: [], reason: null, scope: "all", state: "paused", until: null }, by: "you" },
  ] },
  { version: 14, at: "2026-10-14T15:37:00Z", entities: ["note"], items: [
    { entity: "note", now: null, source: null, via: "app", was: { text: "Codex first." }, by: "you" },
  ] },
  { version: 15, at: "2026-10-14T15:37:00Z", entities: ["hold:hf6539c84"], items: [
    { entity: "hold:hf6539c84", now: null, source: null, via: "app", was: { owner: true, reason: "other", scope: "this Mac", text: "Keep it quiet.", until: null }, by: "you" },
  ] },
];

describe("Settings -> Routing: who made each change", () => {
  it("names you, in Settings, for every change made in the app", () => {
    const v = { ...view(), changes: [...(view().changes ?? []), ...OWNER_ENDINGS] } as RouteView;
    const rows = changeDays(v, MORNING).flatMap((d) => d.rows);
    const owner = rows.filter((r) => r.subject !== "Claude main");
    expect(owner).toHaveLength(5);
    for (const r of owner) expect(r.who).toBe("You, in Settings");
  });

  it("says when a change marked as yours did not pass Khipu's owner check", () => {
    const unchecked = OWNER_ENDINGS.map((c) => ({ ...c, items: c.items.map((i) => ({ ...i, via: "app-unchecked" })) }));
    const rows = changeDays({ ...view(), changes: unchecked } as RouteView, MORNING).flatMap((d) => d.rows);
    expect(rows).toHaveLength(3);
    for (const r of rows) expect(r.who).toBe("Marked as you, but not checked by Khipu");
  });
});

describe("Settings -> Routing: accounts", () => {
  it("shows each account with its state and who said so, and Check now when probeable", async () => {
    await page();
    expect(row("Accounts", "Claude main")).toHaveTextContent("Limit reached, back at 1:52 PM");
    expect(row("Accounts", "Claude main")).toHaveTextContent("Read from Claude 4 min ago");
    expect(row("Accounts", "Grok Build")).toHaveTextContent("Paused by you until Fri 9:00 AM");
    expect(within(row("Accounts", "Claude main")).getByRole("button", { name: "Check now" })).toBeInTheDocument();
    // Open accounts have nothing to clear.
    expect(within(row("Accounts", "Codex")).queryByRole("button", { name: /Clear/ })).toBeNull();
  });

  it("marks an account paused until a chosen time, focusing the form and closing on Escape", async () => {
    await page();
    const btn = screen.getByRole("button", { name: "Mark Codex paused until…" });
    fireEvent.click(btn);
    expect(btn).toHaveAttribute("aria-expanded", "true");
    const select = screen.getByLabelText("Mark Codex paused until");
    expect(select).toHaveFocus();
    expect(screen.getByText(/This doesn't stop anything that is running/)).toBeInTheDocument();
    fireEvent.keyDown(select, { key: "Escape" });
    expect(screen.queryByLabelText("Mark Codex paused until")).toBeNull();

    fireEvent.click(btn);
    fireEvent.change(screen.getByLabelText("Mark Codex paused until"), { target: { value: "never" } });
    fireEvent.click(screen.getByRole("button", { name: "Mark Codex paused" }));
    await waitFor(() => expect(calls("khipu_route_pause")).toEqual([{ account: "codex|", until: null }]));
  });

  it("refuses a typed time that has passed without calling the CLI", async () => {
    await page();
    fireEvent.click(screen.getByRole("button", { name: "Mark Codex paused until…" }));
    fireEvent.change(screen.getByLabelText("Mark Codex paused until"), { target: { value: "custom" } });
    fireEvent.change(screen.getByLabelText("Date and time it ends"), { target: { value: "2026-10-01T09:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Mark Codex paused" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Choose a time that hasn't passed yet.");
    expect(calls("khipu_route_pause")).toEqual([]);
  });

  it("lifts a pause and clears a limit, showing the CLI's refusal in its words", async () => {
    answers.khipu_route_lift = { ok: false, error: "that account isn't paused" };
    await page();
    fireEvent.click(screen.getByRole("button", { name: "Lift the pause for Grok Build" }));
    await waitFor(() => expect(calls("khipu_route_lift")).toEqual([{ account: "grok|" }]));
    expect(await screen.findByRole("alert")).toHaveTextContent("Khipu didn't make that change: that account isn't paused");
    fireEvent.click(screen.getByRole("button", { name: "Clear the limit for Claude main" }));
    await waitFor(() => expect(calls("khipu_route_clear")).toEqual([{ account: "claude|" }]));
    // Each write re-reads the record.
    expect(calls("khipu_route_view").length).toBeGreaterThanOrEqual(3);
  });

  it("dismisses either side of a disagreement, named by whose report it is (F6)", async () => {
    await page();
    const panel = screen.getByRole("group", { name: "Claude second: two reports disagree" });
    expect(panel).toHaveTextContent("Two reports disagree.");
    expect(panel).toHaveTextContent("Open: reported by a Claude Code conversation in project-b 9 min ago.");
    const buttons = within(panel).getAllByRole("button");
    expect(buttons.map((b) => b.textContent)).toEqual(["Dismiss the runner's report", "Dismiss the conversation's report"]);
    fireEvent.click(buttons[1]);
    await waitFor(() =>
      expect(calls("khipu_route_clear")).toEqual([{ account: "claude|/logins/claude-second", report: "e-open" }]),
    );
    fireEvent.click(within(screen.getByRole("group", { name: "Claude second: two reports disagree" })).getByRole("button", { name: "Dismiss the runner's report" }));
    await waitFor(() =>
      expect(calls("khipu_route_clear")[1]).toEqual({ account: "claude|/logins/claude-second", report: "r-runner" }),
    );
  });
});

describe("Settings -> Routing: the note", () => {
  it("edits the note word for word, refusing empty words", async () => {
    await page();
    expect(screen.getByText("“We don't need cursor. Just grok.”")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit note" }));
    const box = screen.getByLabelText("Your note");
    fireEvent.change(box, { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "Save note" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Write the note first.");
    expect(calls("khipu_route_note_set")).toEqual([]);
    fireEvent.change(box, { target: { value: "Codex first.\nThen Grok." } });
    fireEvent.click(screen.getByRole("button", { name: "Save note" }));
    await waitFor(() => expect(calls("khipu_route_note_set")).toEqual([{ text: "Codex first.\nThen Grok." }]));
  });

  it("ends the note only after asking", async () => {
    await page();
    fireEvent.click(screen.getByRole("button", { name: "End this note" }));
    expect(calls("khipu_route_note_end")).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "End the note" }));
    await waitFor(() => expect(calls("khipu_route_note_end")).toHaveLength(1));
  });
});

describe("Settings -> Routing: standing rules", () => {
  it("shows the date, source, seeded and watched marks", async () => {
    await page();
    const three = row("Standing rules", "3.");
    expect(three).toHaveTextContent("From your routing table, Oct 13");
    expect(three).toHaveTextContent("Seeded, confirm");
    expect(three).toHaveTextContent("Checked by Khipu");
    const seven = row("Standing rules", "7.");
    expect(seven).toHaveTextContent("Confirmed");
    expect(seven).toHaveTextContent("Passed to every conversation as written. Khipu has no way to check this one.");
    expect(within(seven).queryByRole("button", { name: "Confirm rule 7" })).toBeNull();
    expect(screen.queryByText(/Watched/)).toBeNull();
  });

  // Settings clarity phase 5: a checked rule says what its check found.
  it("says nothing wrong found only from a check that ran, with its time", async () => {
    await page();
    // The view carries no result for rule 3 yet: no pass is claimed.
    expect(row("Standing rules", "3.")).not.toHaveTextContent("nothing wrong found");
    cleanup();
    answers.khipu_route_view = { ...view(), rule_results: [{ rule: 3, check: "fast_tier", result: "ok", at: "2026-10-14T15:36:00Z", lanes: 3, unchecked: 0 }] };
    await page();
    expect(row("Standing rules", "3.")).toHaveTextContent(
      "Checked by Khipu in your job runners' settings: nothing wrong found, 4 min ago.",
    );
  });

  it("says it can't check yet with no job runner folder, or no lane the rule is about", () => {
    expect(ruleResultLine(true, { rule: 3, result: "no_runner" }).text).toBe(
      "Checked by Khipu, but not yet: add a job runner folder so it has something to read.",
    );
    expect(ruleResultLine(true, { rule: 3, result: "no_lane" }).text).toBe(
      "Checked by Khipu, but not yet: none of your job runners has a lane this rule is about.",
    );
    expect(ruleResultLine(true, { rule: 3, check: "fast_tier", result: "cannot_check" }).text).toBe(
      "Checked by Khipu, but not here: the model is set inside the launch script, which Khipu doesn't read.",
    );
    expect(ruleResultLine(true, { rule: 3, result: "unreadable" })).toEqual({
      text: "Checked by Khipu, but not now: Khipu can't read your job runners' settings.",
      tone: "",
    });
    expect(ruleResultLine(true, { rule: 1, check: "grok_private", result: "cannot_check" }).text).toBe(
      "Checked by Khipu, but not here: Khipu hasn't read your job runner's list of added work yet.",
    );
    expect(ruleResultLine(true, { rule: 2, check: "grok_confirm", result: "ok", at: "2026-10-14T15:36:00Z", unchecked: 1 }).text).toContain(
      "1 lane couldn't be checked: the confirming lane's kind isn't set",
    );
    for (const r of ["no_runner", "no_lane", "cannot_check", "unreadable", "broken"])
      expect(ruleResultLine(true, { rule: 3, result: r, at: "2026-10-14T15:36:00Z" }).text).not.toContain("nothing wrong found");
  });

  it("never shows a stale check as fresh", async () => {
    const cell = (runner: string, outcome: string, o: object = {}) => ({
      runner, outcome, reads: { settings: { read: true, good_at: "2026-10-13T15:36:00Z", failed: false, since: null, stale: false, ...o } } });
    answers.khipu_route_view = {
      ...view(),
      rule_results: [{
        rule: 3, check: "fast_tier", result: "failed", lanes: 2, unchecked: 0,
        checks: [{ check: "fast_tier", result: "failed", lanes: 2, unchecked: 0,
          cells: [cell("Project A", "failed", { failed: true, since: "2026-10-14T15:00:00Z", stale: true }), cell("Project B", "ok", { good_at: "2026-10-14T15:36:00Z" })] }],
      }],
    };
    await page();
    const three = row("Standing rules", "3.");
    expect(three).toHaveTextContent("Checked by Khipu, but not now: Khipu couldn't read Project A's settings; it last read them 24 h ago.");
    expect(three).not.toHaveTextContent("nothing wrong found");
    expect(three).not.toHaveTextContent("just now");
    expect(three).not.toHaveTextContent("Project B");
    expect(ruleResultLine(true, { rule: 3, result: "stale", checks: [{ check: "fast_tier", result: "stale",
      cells: [cell("Project A", "stale", { good_at: "2026-10-14T03:00:00Z", stale: true })] }] }, Date.parse("2026-10-14T16:00:00Z"))).toEqual({
      text: "Checked by Khipu, but not now: Khipu last read Project A's settings 13 h ago.", tone: "" });
  });

  it("shows every check's outcome for a rule with several checks", async () => {
    const at = "2026-10-14T15:36:00Z";
    answers.khipu_route_view = {
      ...view(),
      rule_results: [{
        rule: 3, check: "paid_xai", result: "cannot_check",
        checks: [
          { check: "grok_private", result: "ok", at, lanes: 1, unchecked: 0, sources: [] },
          { check: "paid_xai", result: "cannot_check", lanes: 0, unchecked: 1, sources: [] },
        ],
      }],
    };
    await page();
    const three = row("Standing rules", "3.");
    expect(three).toHaveTextContent("Checked by Khipu for Grok on private work: passed, 4 min ago.");
    expect(three).toHaveTextContent("Checked by Khipu for the paid xAI API, but not here: the launch environment is set inside the launch script, which Khipu doesn't read.");
    expect(three).not.toHaveTextContent("nothing wrong found");
    expect(three.querySelectorAll(".rule-result")).toHaveLength(2);
    expect(three).not.toHaveClass("broken");
  });

  // Codex review round 2 (A): a runner Khipu never read is named, never passed over.
  it("names a job runner Khipu has never read and claims no pass", async () => {
    const read = (o: object = {}) => ({ read: true, good_at: "2026-10-14T15:36:00Z", failed: false, since: null, stale: false, ...o });
    answers.khipu_route_view = {
      ...view(),
      rule_results: [{
        rule: 3, check: "fast_tier", result: "not_read", at: null, lanes: 0, unchecked: 0,
        checks: [{ check: "fast_tier", result: "not_read", at: null, lanes: 0, unchecked: 0, cells: [
          { runner: "project A", outcome: "ok", lanes: 3, unchecked: 0, reads: { settings: read() } },
          { runner: "project B", outcome: "not_read", lanes: 0, unchecked: 0,
            reads: { settings: read({ read: false, good_at: null, failed: true, since: "2026-10-14T15:36:00Z" }) } },
        ] }],
      }],
    };
    await page();
    const three = row("Standing rules", "3.");
    expect(three).toHaveTextContent("Checked by Khipu, but not now: Khipu has never read project B's settings.");
    expect(three).not.toHaveTextContent("nothing wrong found");
    expect(three).not.toHaveTextContent("just now");
    expect(three).not.toHaveTextContent("project A");
  });

  // Codex review round 3: a cap that left input out is never a pass.
  it("says a check was not fully checked and what a cap left out", async () => {
    const read = { read: true, good_at: "2026-10-14T15:36:00Z", failed: false, since: null, stale: false };
    answers.khipu_route_view = {
      ...view(),
      rule_results: [{
        rule: 3, check: "fast_tier", result: "incomplete", at: null, lanes: 1024, unchecked: 0,
        checks: [{ check: "fast_tier", result: "incomplete", at: null, lanes: 1024, unchecked: 0, cells: [
          { runner: "project A", outcome: "incomplete", lanes: 1024, unchecked: 0, reads: { settings: read }, skipped: { lanes: 3 } },
          { runner: "project B", outcome: "incomplete", lanes: 2, unchecked: 0, reads: { settings: read, events: read },
            skipped: { lanes: 1, added: 12 } },
        ] }],
      }],
    };
    await page();
    const three = row("Standing rules", "3.");
    expect(three).toHaveTextContent(
      "Checked by Khipu, but not fully: Khipu left out 3 lanes in project A; and 1 lane and 12 lines of added work in project B.",
    );
    expect(three).not.toHaveTextContent("nothing wrong found");
    expect(screen.getByRole("button", { name: "May be broken, 0 rules" })).toBeInTheDocument();
  });

  // The privacy check reads each runner's whole events log once, then only what is new.
  it("says it is still checking a long log, and what an ok check covered", async () => {
    const read = { read: true, good_at: "2026-10-14T15:36:00Z", failed: false, since: null, stale: false };
    const cell = (scan: object, outcome: string) => ({ runner: "project A", outcome, lanes: 2, unchecked: 0, reads: { settings: read, events: read }, scan });
    const rule = (result: string, scan: object) => ({
      ...view(),
      rules: [{ n: 3, text: "Grok never builds or fixes privacy or safety code.", source: "from your routing table", date: "2026-10-13" }],
      rule_results: [{ rule: 3, check: "grok_private", result, at: result === "ok" ? "2026-10-14T15:36:00Z" : null, lanes: 2, unchecked: 0,
        checks: [{ check: "grok_private", result, at: result === "ok" ? "2026-10-14T15:36:00Z" : null, lanes: 2, unchecked: 0, cells: [cell(scan, result)] }] }],
    });
    answers.khipu_route_view = rule("checking", { read: 2_000_000, size: 8_000_000, lines: 9000, done: false });
    await page();
    expect(row("Standing rules", "3.")).toHaveTextContent(
      "Checked by Khipu, but still checking: Khipu has read 25% of project A's list of added work so far.",
    );
    expect(row("Standing rules", "3.")).not.toHaveTextContent("nothing wrong found");
    cleanup();
    // A last line left without its newline for over ten minutes is never read.
    const v = rule("incomplete", { read: 70, size: 140, lines: 1, done: false });
    Object.assign(v.rule_results![0].checks![0].cells![0], { skipped: { unfinished_line: 1 } });
    answers.khipu_route_view = v;
    await page();
    expect(row("Standing rules", "3.")).toHaveTextContent(
      "Checked by Khipu, but not fully: Khipu left out an unfinished last line of added work in project A.",
    );
    cleanup();
    answers.khipu_route_view = rule("ok", { read: 8_000_000, size: 8_000_000, lines: 36000, done: true });
    await page();
    expect(row("Standing rules", "3.")).toHaveTextContent("nothing wrong found, 4 min ago. It read all 36,000 lines of added work.");
  });

  // Codex review round 2 (C): each check's line is its own, never another check's finding.
  it("keeps a broken check's finding off the rule's other check", async () => {
    const text = "A standing rule may be broken: project A's settings name a model ending in -fast for its codex lane.";
    answers.khipu_route_view = {
      ...view(),
      rule_results: [{
        rule: 3, check: "fast_tier", result: "broken", text, more: 0,
        checks: [
          { check: "fast_tier", result: "broken", text, more: 0, cells: [] },
          { check: "paid_xai", result: "cannot_check", text: null, more: 0, lanes: 0, unchecked: 1, cells: [] },
        ],
      }],
    };
    await page();
    const three = row("Standing rules", "3.");
    expect(three).toHaveClass("broken");
    const lines = [...three.querySelectorAll(".rule-result")].map((x) => x.textContent ?? "");
    const xai = lines.find((l) => l.includes("for the paid xAI API")) ?? "";
    expect(xai).toBe("Checked by Khipu for the paid xAI API, but not here: the launch environment is set inside the launch script, which Khipu doesn't read.");
    expect(xai).not.toContain("-fast");
    expect(lines.find((l) => l.includes("-fast"))).toBe("Checked by Khipu for models ending in -fast: may be broken. Project A's settings name a model ending in -fast for its codex lane.");
  });

  it("shows a broken check on the rule's own row and in Home's list", async () => {
    const text = "A standing rule may be broken: Project A's settings name a model ending in -fast for its codex lane.";
    const v: RouteView = {
      ...view(),
      rule_checks: [{ rule: 3, status: "broken", text, at: "2026-10-14T15:36:00Z", runner: "Project A", file: "settings" }],
      rule_results: [{ rule: 3, check: "fast_tier", result: "broken", text, more: 0 }],
    };
    answers.khipu_route_view = v;
    await page();
    const three = row("Standing rules", "3.");
    expect(three).toHaveClass("broken");
    expect(three).toHaveTextContent(
      "Checked by Khipu: may be broken. Project A's settings name a model ending in -fast for its codex lane.",
    );
    // Home's Routing card and attention list read the same check.
    expect(brokenRules(v).map((g) => g.rule)).toEqual([3]);
  });

  it("words the help line by how many rules Khipu can check", async () => {
    await page();
    expect(screen.getByText(/Khipu can check 1 of them by reading your job runners' settings/)).toBeInTheDocument();
    expect(screen.getByText(/Khipu never stops any work\./)).toBeInTheDocument();
  });

  it("filters rules with named, counted chips", async () => {
    answers.khipu_route_view = {
      ...view(),
      rule_results: [{ rule: 3, check: "fast_tier", result: "broken", text: "A standing rule may be broken: x.", more: 0 }],
    };
    await page();
    const chips = screen.getByRole("group", { name: "Show rules" });
    const all = within(chips).getByRole("button", { name: "All, 2 rules" });
    expect(all).toHaveAttribute("aria-pressed", "true");
    within(chips).getByRole("button", { name: "To confirm, 1 rule" });
    within(chips).getByRole("button", { name: "Checked by Khipu, 1 rule" });
    const broken = within(chips).getByRole("button", { name: "May be broken, 1 rule" });
    fireEvent.click(broken);
    expect(broken).toHaveAttribute("aria-pressed", "true");
    const list = screen.getByRole("list", { name: "Standing rules" });
    expect(within(list).getAllByRole("listitem").map((li) => li.textContent?.slice(0, 2))).toEqual(["3."]);
    fireEvent.click(within(chips).getByRole("button", { name: "All, 2 rules" }));
    expect(within(screen.getByRole("list", { name: "Standing rules" })).getAllByRole("listitem")).toHaveLength(2);
  });

  it("confirms a rule only after asking", async () => {
    await page();
    fireEvent.click(screen.getByRole("button", { name: "Confirm rule 3" }));
    expect(calls("khipu_route_rule")).toEqual([]);
    const ask = screen.getByRole("group", { name: "Confirm rule 3?" });
    fireEvent.click(within(ask).getByRole("button", { name: "Confirm rule 3" }));
    await waitFor(() => expect(calls("khipu_route_rule")).toEqual([{ action: "confirm", n: 3 }]));
  });

  it("shows a pending conversation rule with its prior wording, then clears it after confirmation", async () => {
    let confirmed = false;
    answers.khipu_route_rule = (args) => {
      if (args?.action === "confirm") confirmed = true;
      return { ok: true };
    };
    answers.khipu_route_view = () => {
      const v = view();
      v.rules = [{ n: 3, text: "Never a fast tier, ever.", seeded: true, confirmed,
        pending: !confirmed, pending_kind: "changed", pending_by: "a Codex conversation", prior_text: confirmed ? null : "Never a -fast tier.",
        source: "from your routing table", date: "2026-10-13" }];
      return v;
    };
    await page();
    const three = row("Standing rules", "3.");
    expect(three).toHaveTextContent("Changed by a Codex conversation at the owner's request; the owner has not confirmed it yet.");
    expect(three).toHaveTextContent("It replaced: “Never a -fast tier.”");
    fireEvent.click(screen.getByRole("button", { name: "Confirm rule 3" }));
    fireEvent.click(within(screen.getByRole("group", { name: "Confirm rule 3?" })).getByRole("button", { name: "Confirm rule 3" }));
    await waitFor(() => expect(calls("khipu_route_rule")).toContainEqual({ action: "confirm", n: 3 }));
    await waitFor(() => expect(screen.queryByText(/owner has not confirmed/)).toBeNull());
    expect(screen.queryByRole("button", { name: "Undo pending rule 3" })).toBeNull();
  });

  it("undo restores the prior wording, and a refused undo stays visible", async () => {
    let undone = false;
    answers.khipu_route_rule = (args) => {
      if (args?.action === "undo") undone = true;
      return { ok: true };
    };
    answers.khipu_route_view = () => {
      const v = view();
      v.rules = [{ n: 3, text: undone ? "Never a -fast tier." : "Never a fast tier, ever.", seeded: true, confirmed: false,
        pending: !undone, pending_kind: "changed", pending_by: "a Codex conversation", prior_text: undone ? null : "Never a -fast tier.",
        source: "from your routing table", date: "2026-10-13" }];
      return v;
    };
    await page();
    fireEvent.click(screen.getByRole("button", { name: "Undo pending rule 3" }));
    await waitFor(() => expect(calls("khipu_route_rule")).toContainEqual({ action: "undo", n: 3 }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Undo pending rule 3" })).toBeNull());
    expect(row("Standing rules", "3.")).toHaveTextContent("Never a -fast tier.");

    undone = false;
    answers.khipu_route_rule = { ok: false, error: "the pending rule changed again" };
    await page();
    // page() returns once any Accounts list shows, and the first page's list is
    // still there, so wait for the second page's Undo button itself.
    const undoButtons = await screen.findAllByRole("button", { name: "Undo pending rule 3" });
    fireEvent.click(undoButtons[undoButtons.length - 1]);
    expect(await screen.findByRole("alert")).toHaveTextContent("the pending rule changed again");
  });

  it("edits a rule after asking, and shows the CLI's error", async () => {
    answers.khipu_route_rule = { ok: false, error: "there is no standing rule 3" };
    await page();
    fireEvent.click(screen.getByRole("button", { name: "Edit rule 3" }));
    fireEvent.change(screen.getByLabelText("Rule 3"), { target: { value: "Never a fast tier, ever." } });
    fireEvent.click(screen.getByRole("button", { name: "Save…" }));
    expect(screen.getByText(/Change rule 3 to these words\?/)).toBeInTheDocument();
    expect(calls("khipu_route_rule")).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Change rule 3" }));
    await waitFor(() =>
      expect(calls("khipu_route_rule")).toEqual([{ action: "edit", n: 3, text: "Never a fast tier, ever." }]),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("there is no standing rule 3");
  });

  it("removes a rule after asking and offers Undo", async () => {
    let removed = false;
    answers.khipu_route_rule = (args) => {
      if (args?.action === "retire") removed = true;
      if (args?.action === "restore") removed = false;
      return { ok: true };
    };
    answers.khipu_route_view = () => {
      const v = view();
      if (removed) v.rules = v.rules!.filter((r) => r.n !== 3);
      return v;
    };
    await page();
    fireEvent.click(screen.getByRole("button", { name: "Remove rule 3" }));
    expect(calls("khipu_route_rule")).toEqual([]);
    const ask = screen.getByRole("group", { name: "Remove rule 3?" });
    fireEvent.click(within(ask).getByRole("button", { name: "Remove rule 3" }));
    await waitFor(() => expect(screen.getByText("Rule 3 removed.")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() =>
      expect(calls("khipu_route_rule")).toEqual([{ action: "retire", n: 3 }, { action: "restore", n: 3 }]),
    );
  });

  it("adds a rule after asking, and never with empty words", async () => {
    await page();
    const box = screen.getByLabelText("New rule");
    fireEvent.click(screen.getByRole("button", { name: "Add rule…" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Write the rule's words first.");
    fireEvent.change(box, { target: { value: "-fast is never used" } });
    fireEvent.click(screen.getByRole("button", { name: "Add rule…" }));
    expect(screen.getByText(/Add this rule\?/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add the rule" }));
    await waitFor(() => expect(calls("khipu_route_rule")).toEqual([{ action: "add", text: "-fast is never used" }]));
  });

  it("brings back a removed rule from Recent changes", async () => {
    await page();
    fireEvent.click(screen.getByRole("button", { name: "Bring back standing rule 6" }));
    await waitFor(() => expect(calls("khipu_route_rule")).toEqual([{ action: "restore", n: 6 }]));
  });
});

describe("Settings -> Routing: keyboard focus around inline questions (F5)", () => {
  function within_(label: string): boolean {
    return screen.getByRole("group", { name: label }).contains(document.activeElement);
  }

  it("moves focus into Add this rule?, and Escape gives it back to Add rule…", async () => {
    await page();
    fireEvent.change(screen.getByLabelText("New rule"), { target: { value: "Keep Codex for reviews." } });
    const add = screen.getByRole("button", { name: "Add rule…" });
    add.focus();
    fireEvent.click(add);
    await waitFor(() => expect(within_("Add this rule?")).toBe(true));
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Add rule…" })));
  });

  it("gives focus to Undo once a rule is removed, and back to Remove when kept", async () => {
    await page();
    const remove = screen.getByRole("button", { name: "Remove rule 3" });
    remove.focus();
    fireEvent.click(remove);
    await waitFor(() => expect(within_("Remove rule 3?")).toBe(true));
    fireEvent.click(within(screen.getByRole("group", { name: "Remove rule 3?" })).getByRole("button", { name: "Keep it" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Remove rule 3" })));
    answers.khipu_route_view = () => {
      const v = view();
      return calls("khipu_route_rule").length ? { ...v, rules: v.rules!.filter((r) => r.n !== 3) } : v;
    };
    fireEvent.click(screen.getByRole("button", { name: "Remove rule 3" }));
    fireEvent.click(within(await screen.findByRole("group", { name: "Remove rule 3?" })).getByRole("button", { name: "Remove rule 3" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Undo" })));
  });

  it("moves focus into End your note?, and Escape closes it and returns focus", async () => {
    await page();
    const end = screen.getByRole("button", { name: "End this note" });
    end.focus();
    fireEvent.click(end);
    await waitFor(() => expect(within_("End your note?")).toBe(true));
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("group", { name: "End your note?" })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "End this note" })));
  });
});

describe("Settings -> Routing: holds", () => {
  it("lists holds with kind, author and end, and ends one now", async () => {
    await page();
    const hold = row("Holds", "This Mac");
    expect(hold).toHaveTextContent("No new launches while the Mac is busy (load over 75)");
    expect(hold).toHaveTextContent("Machine busy · set by a Claude Code conversation in project-a at 9:40 AM · ends tomorrow 9:40 AM");
    fireEvent.click(screen.getByRole("button", { name: "End the hold on this Mac" }));
    await waitFor(() => expect(calls("khipu_route_hold_end")).toEqual([{ id: "h1" }]));
  });

  it("adds a hold with its words, scope, kind and end, until cleared too", async () => {
    await page();
    fireEvent.click(screen.getByRole("button", { name: "Add hold" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Write what conversations should know.");
    fireEvent.change(screen.getByLabelText("Words"), { target: { value: "Keep it for reviews" } });
    fireEvent.change(screen.getByLabelText("About"), { target: { value: "codex|" } });
    fireEvent.change(screen.getByLabelText("Kind"), { target: { value: "keep" } });
    fireEvent.change(screen.getByLabelText("Ends"), { target: { value: "never" } });
    fireEvent.click(screen.getByRole("button", { name: "Add hold" }));
    await waitFor(() =>
      expect(calls("khipu_route_hold_add")).toEqual([
        { text: "Keep it for reviews", reason: "keep", scope: "account", account: "codex|", until: null },
      ]),
    );
  });

  it("says a hold's longest words by the field, checks them first, and lets you raise the limit (F3)", async () => {
    await page();
    await waitFor(() => expect(screen.getByText("At most 300 characters")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Words"), { target: { value: "x".repeat(301) } });
    fireEvent.click(screen.getByRole("button", { name: "Add hold" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("A hold's words are at most 300 characters (these are 301 characters).");
    expect(alert).toHaveTextContent("raise the limit under Longest words in a hold below");
    expect(alert.textContent).not.toMatch(/routing_/);
    expect(calls("khipu_route_hold_add")).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Change: Longest words in a hold" }));
    fireEvent.change(screen.getByLabelText("Longest words"), { target: { value: "0" } });
    fireEvent.click(within(screen.getByRole("form", { name: "Change: Longest words in a hold" })).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls("khipu_config_set")).toEqual([{ key: "routing_hold_chars", value: "0" }]));
    expect(screen.getByRole("button", { name: "Change: Longest line" })).toBeEnabled();
  });

  it("changes how long a conversation's hold lasts, and its most", async () => {
    await page();
    expect(await screen.findByText("24 hours · at most 7 days")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Change: How long a conversation's hold lasts" }));
    fireEvent.change(screen.getByLabelText("At most"), { target: { value: "0" } });
    fireEvent.click(within(screen.getByRole("form", { name: "Change: How long a conversation's hold lasts" })).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(calls("khipu_config_set")).toEqual([{ key: "routing_conversation_hold_max", value: "0" }]),
    );
  });
});

describe("Settings -> Routing: telling conversations", () => {
  it("has the master switch and the three moments", async () => {
    await page();
    expect(screen.getByRole("switch", { name: "Tell conversations about routing" })).toBeChecked();
    expect(screen.getByRole("switch", { name: "Before handing work to another agent" })).toBeChecked();
    expect(screen.queryByText(/Copy for cloud agents/)).toBeNull();
    expect(screen.getByText("Checking for resets")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch", { name: "Tell conversations about routing" }));
    await waitFor(() => expect(calls("khipu_feature_set")).toEqual([{ name: "routing_push", enabled: false }]));
    fireEvent.click(screen.getByRole("switch", { name: "Before each prompt" }));
    await waitFor(() =>
      expect(calls("khipu_config_set")).toEqual([{ key: "routing_push_prompt", value: "off" }]),
    );
  });

  it("turns the push before handing work to another agent off and on", async () => {
    answers.khipu_config_show = () =>
      calls("khipu_config_set").length ? config({ routing_push_delegation: setting(false, true) }) : config();
    await page();
    const sw = screen.getByRole("switch", { name: "Before handing work to another agent" });
    fireEvent.click(sw);
    await waitFor(() =>
      expect(calls("khipu_config_set")).toEqual([{ key: "routing_push_delegation", value: "off" }]),
    );
    await waitFor(() => expect(screen.getByRole("switch", { name: "Before handing work to another agent" })).not.toBeChecked());
    fireEvent.click(screen.getByRole("switch", { name: "Before handing work to another agent" }));
    await waitFor(() => expect(calls("khipu_config_set")).toHaveLength(2));
    expect(calls("khipu_config_set")[1]).toEqual({ key: "routing_push_delegation", value: "on" });
  });

  it("saves your dispatch scripts as file names, and refuses a folder", async () => {
    answers.khipu_config_show = () =>
      calls("khipu_config_set").length ? config({ routing_dispatch_scripts: setting(["dispatch.py", "run-job.sh"], []) }) : config();
    await page();
    const row = screen.getByText("Your dispatch scripts").closest(".row-item") as HTMLElement;
    expect(within(row).getByText("None")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Change your dispatch scripts" }));
    const form = screen.getByRole("form", { name: "Change your dispatch scripts" });
    const input = within(form).getByLabelText("File names");
    fireEvent.change(input, { target: { value: "scripts/dispatch.py" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent(/no folder/);
    expect(calls("khipu_config_set")).toEqual([]);
    // A name that starts with a dash would read as an option: refused, as the CLI and Rust do.
    fireEvent.change(input, { target: { value: "-c" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent(/no folder/);
    expect(calls("khipu_config_set")).toEqual([]);
    fireEvent.change(input, { target: { value: " dispatch.py, run-job.sh,dispatch.py " } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(calls("khipu_config_set")).toEqual([{ key: "routing_dispatch_scripts", value: '["dispatch.py","run-job.sh"]' }]),
    );
    await waitFor(() => expect(within(row).getByText("dispatch.py, run-job.sh")).toBeInTheDocument());
  });

  it("locks the master switch when the environment decides it", async () => {
    answers.khipu_features_show = { features: { routing_push: { enabled: false, source: "env" } } };
    await page();
    await waitFor(() => expect(screen.getByRole("switch", { name: "Tell conversations about routing" })).toBeDisabled());
    expect(screen.getByText("KHIPU_FEATURE_ROUTING_PUSH")).toBeInTheDocument();
  });

  it("shows every size and time, each removable on the spot", async () => {
    await page();
    await screen.findByText("1,500 characters");
    for (const words of ["400 characters", "50 ms", "20 changes or 6 hours", "12 hours", "30 minutes", "5 minutes"]) {
      expect(screen.getByText(words)).toBeInTheDocument();
    }
    fireEvent.click(screen.getByRole("button", { name: "Change: Longest summary at the start of a conversation" }));
    const select = screen.getByLabelText("Longest summary");
    expect(within(select).getByRole("option", { name: "No limit" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "1,500 characters (the default)" })).toBeInTheDocument();
    fireEvent.change(select, { target: { value: "0" } });
    fireEvent.click(within(screen.getByRole("form", { name: "Change: Longest summary at the start of a conversation" })).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls("khipu_config_set")).toEqual([{ key: "routing_summary_chars", value: "0" }]));
  });

  it("shows the CLI's refusal of a size", async () => {
    answers.khipu_config_set = { ok: false, error: "routing_stale_after must be a whole number of 0 or more" };
    await page();
    await screen.findByText("1,500 characters");
    fireEvent.click(screen.getByRole("button", { name: "Change: Treat a report as out of date after" }));
    fireEvent.change(screen.getByLabelText("Out of date after"), { target: { value: "0" } });
    fireEvent.click(within(screen.getByRole("form", { name: "Change: Treat a report as out of date after" })).getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Khipu didn't make that change: routing_stale_after must be a whole number of 0 or more",
    );
  });

  it("says whether either old off switch is set and how to clear it", async () => {
    await page();
    // Khipu can't see the shell's environment, so it never says the setting
    // is unset there (F4).
    expect(screen.queryByText(/switches are not set/)).toBeNull();
    expect(screen.getByText(/Khipu\s+can't see your shell's settings/)).toBeInTheDocument();
    expect(screen.getByText(/routing_now_disable/).closest("p")).toHaveTextContent("isn't there");
    cleanup();
    answers.khipu_route_view = { ...view(), old_switches: { env: false, file: true } };
    await page();
    expect(screen.getByText(/Your old "routing off" switch is set/)).toBeInTheDocument();
    expect(screen.getByText(/delete it to turn the telling back on/)).toBeInTheDocument();
  });
});

describe("Settings -> Routing: checking for resets", () => {
  it("writes the master switch, wait, retries, and each busy setting through fixed controls", async () => {
    await page();
    fireEvent.click(await screen.findByRole("switch", { name: "Check an account at its reset time" }));
    await waitFor(() => expect(calls("khipu_config_set")).toContainEqual({ key: "routing_reset_check", value: "off" }));

    fireEvent.click(screen.getByRole("button", { name: "Change: Wait after the reset time" }));
    fireEvent.change(screen.getByLabelText("Wait"), { target: { value: "300" } });
    fireEvent.click(within(screen.getByRole("form", { name: "Change: Wait after the reset time" })).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls("khipu_config_set")).toContainEqual({ key: "routing_reset_check_wait", value: "300" }));

    fireEvent.change(screen.getByLabelText("First retry in seconds"), { target: { value: "900" } });
    fireEvent.change(screen.getByLabelText("Second retry in seconds"), { target: { value: "2400" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Save" }).find((b) => b.closest(".route-limit")?.textContent?.includes("Try again after"))!);
    await waitFor(() => expect(calls("khipu_config_set")).toContainEqual({ key: "routing_reset_check_retries", value: "[900,2400]" }));

    fireEvent.click(screen.getByRole("button", { name: "Change: Wait while the Mac is busy" }));
    fireEvent.change(screen.getByLabelText("Load"), { target: { value: "90" } });
    fireEvent.change(screen.getByLabelText("Wait"), { target: { value: "600" } });
    fireEvent.change(screen.getByLabelText("Busy attempts"), { target: { value: "5" } });
    fireEvent.click(within(screen.getByRole("form", { name: "Change: Wait while the Mac is busy" })).getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(calls("khipu_config_set")).toEqual(expect.arrayContaining([
        { key: "routing_busy_load", value: "90" },
        { key: "routing_busy_wait", value: "600" },
        { key: "routing_busy_tries", value: "5" },
      ]));
    });
  });

  it("hydrates retry times, names account schedules and keeps Check now outcomes in the row", async () => {
    const v = view();
    v.accounts![0]!.next_check = "2026-10-14T18:02:00Z";
    v.accounts![0]!.reset_check = { status: "cannot_check", reason: "this account can't be checked without using a turn" };
    answers.khipu_route_view = v;
    answers.khipu_config_show = config({ routing_reset_check_retries: setting([900, 2400]) });
    answers.khipu_route_check = { ok: true, reason: "not checked: the Mac was busy" };
    await page();
    expect(await screen.findByLabelText("First retry in seconds")).toHaveValue("900");
    expect(screen.getByText(/Claude main:.*this account can't be checked without using a turn/)).toBeInTheDocument();
    expect(row("Accounts", "Claude main")).toHaveTextContent("Khipu checks again at 2:02 PM.");
    fireEvent.click(within(row("Accounts", "Claude main")).getByRole("button", { name: "Check now" }));
    await waitFor(() => expect(calls("khipu_route_check")).toEqual([{ account: "claude|" }]));
    // The page has its own status line ("Reading what needs you…" without a plan read): look in the row.
    expect(await within(row("Accounts", "Claude main")).findByRole("status")).toHaveTextContent("not checked: the Mac was busy");
  });

  it("words the busy-Mac row and offers no wait, no attempts and no retries (fix round 1, F7)", async () => {
    expect(limitWords(3, "attempts")).toBe("3 attempts");
    expect(limitWords(1, "attempts")).toBe("1 attempt");
    answers.khipu_config_show = config({ routing_busy_load: setting(0), routing_reset_check_retries: setting([]) });
    await page();
    const busy = screen.getByRole("button", { name: "Change: Wait while the Mac is busy" }).closest(".route-limit")!;
    expect(busy).toHaveTextContent("Never wait · 5 minutes · 3 attempts");
    const retries = screen.getByLabelText("First retry in seconds").closest(".route-limit")!;
    expect(retries).toHaveTextContent("No retries");
    // The fields take the stored retries in an effect after the render that shows them.
    await waitFor(() => expect(screen.getByLabelText("First retry in seconds")).toHaveValue(""));
    fireEvent.click(screen.getByRole("button", { name: "Change: Wait while the Mac is busy" }));
    expect(within(screen.getByLabelText("Load")).getByRole("option", { name: /^Never wait/ })).toBeInTheDocument();
    expect(within(screen.getByLabelText("Busy attempts")).getByRole("option", { name: /^None: record it at once/ })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("First retry in seconds"), { target: { value: "900" } });
    fireEvent.click(within(retries as HTMLElement).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls("khipu_config_set")).toContainEqual({ key: "routing_reset_check_retries", value: "[900]" }));
    fireEvent.change(screen.getByLabelText("First retry in seconds"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Second retry in seconds"), { target: { value: "" } });
    fireEvent.click(within(retries as HTMLElement).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls("khipu_config_set")).toContainEqual({ key: "routing_reset_check_retries", value: "[]" }));
  });

  it("offers no Check now for an account Khipu no longer reads (fix round 1, F3)", async () => {
    const v = view();
    v.accounts![0]!.configured = false;
    answers.khipu_route_view = v;
    await page();
    expect(within(row("Accounts", "Claude main")).queryByRole("button", { name: "Check now" })).toBeNull();
  });

  it("shows a Check now refusal beside its account", async () => {
    answers.khipu_route_check = { ok: false, error: "another usage check is running" };
    await page();
    fireEvent.click(within(row("Accounts", "Claude main")).getByRole("button", { name: "Check now" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("another usage check is running");
  });
});

describe("Settings -> Routing: job runner folders, lanes, the table and changes", () => {
  it("lists folders with their last read, stopped reporting, and removes one after asking", async () => {
    await page();
    await screen.findByRole("list", { name: "Job runner folders" });
    expect(row("Job runner folders", "Project A")).toHaveTextContent("Read just now · 3 lanes");
    expect(row("Job runner folders", "Project A")).toHaveTextContent("/runners/a · reports every 20 s");
    expect(row("Job runner folders", "Project B")).toHaveTextContent("Stopped reporting at 10:02 AM");
    expect(row("Job runner folders", "Project B")).toHaveTextContent("last good read 10:02 AM, 1 h 38 min ago");
    fireEvent.click(screen.getByRole("button", { name: "Remove the Project B folder" }));
    expect(calls("khipu_config_set")).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Remove Project B" }));
    await waitFor(() =>
      expect(calls("khipu_config_set")).toEqual([
        { key: "routing_sources", value: JSON.stringify([{ name: "Project A", kind: "lanes", path: "/runners/a" }]) },
      ]),
    );
  });

  it("adds a folder, refusing a relative path", async () => {
    await page();
    await screen.findByRole("list", { name: "Job runner folders" });
    fireEvent.click(screen.getByRole("button", { name: "Add a folder…" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Project C" } });
    fireEvent.change(screen.getByLabelText("Folder"), { target: { value: "runners/c" } });
    fireEvent.click(screen.getByRole("button", { name: "Add folder" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("starting with / or ~/");
    fireEvent.change(screen.getByLabelText("Folder"), { target: { value: "/runners/c" } });
    fireEvent.click(screen.getByRole("button", { name: "Add folder" }));
    await waitFor(() => expect(calls("khipu_config_set")).toHaveLength(1));
    expect(JSON.parse(calls("khipu_config_set")[0]!.value as string)).toEqual([
      { name: "Project A", kind: "lanes", path: "/runners/a" },
      { name: "Project B", kind: "lanes", path: "/runners/b" },
      { name: "Project C", kind: "lanes", path: "/runners/c" },
    ]);
  });

  it("matches an unmatched lane to an account", async () => {
    await page();
    const list = await screen.findByRole("list", { name: "Project B lanes" });
    const lane = within(list).getAllByRole("listitem").find((r) => r.textContent?.startsWith("opus-batch"))!;
    expect(lane).toHaveTextContent("Not matched");
    fireEvent.change(within(lane).getByLabelText("Account for opus-batch"), { target: { value: "Claude main" } });
    fireEvent.click(within(lane).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls("khipu_config_set")).toHaveLength(1));
    expect(JSON.parse(calls("khipu_config_set")[0]!.value as string)["Project B"]).toEqual({
      secondary: "Claude second",
      "opus-batch": "Claude main",
    });
  });

  it("shows the routing table file read only, and recent changes by day", async () => {
    await page();
    expect(await screen.findByText("/notes/routing/ROUTING-NOW.md")).toBeInTheDocument();
    expect(screen.getByText("Current-state section changed 9:20 AM")).toBeInTheDocument();
    const today = screen.getByRole("list", { name: "Today" });
    expect(within(today).getAllByRole("listitem")[0]).toHaveTextContent("Standing rule 6 removed");
    // Earlier days are loaded on request.
    expect(screen.queryByRole("list", { name: "Yesterday" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show earlier days" }));
    expect(screen.getByRole("list", { name: "Yesterday" })).toHaveTextContent("Grok Build paused by you");
  });
});

// ---- Fix round 2 ----------------------------------------------------------------------------

describe("Settings -> Routing: choosing the routing table file (R2-1)", () => {
  it("chooses a file when none is set, refusing a relative path", async () => {
    answers.khipu_config_show = config({ routing_table: setting(null, null) });
    await page();
    expect(await screen.findByText("No file set")).toBeInTheDocument();
    const choose = screen.getByRole("button", { name: "Choose the routing table file" });
    expect(choose).toHaveTextContent("Choose…");
    fireEvent.click(choose);
    const form = screen.getByRole("form", { name: "Change the routing table file" });
    const field = within(form).getByLabelText("File");
    expect(document.activeElement).toBe(field);
    fireEvent.change(field, { target: { value: "notes/ROUTING-NOW.md" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("starting with / or ~/");
    expect(calls("khipu_config_set")).toEqual([]);
    answers.khipu_config_show = () =>
      calls("khipu_config_set").length ? config() : config({ routing_table: setting(null, null) });
    fireEvent.change(field, { target: { value: " ~/notes/ROUTING-NOW.md " } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls("khipu_config_set")).toEqual([{ key: "routing_table", value: "~/notes/ROUTING-NOW.md" }]));
    expect(await screen.findByText("/notes/routing/ROUTING-NOW.md")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("form", { name: "Change the routing table file" })).toBeNull());
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Change the routing table file" })),
    );
  });

  it("changes the file, stops reading it, and shows the CLI's refusal", async () => {
    answers.khipu_config_set = { ok: false, error: "routing_table can't name a file inside a private folder" };
    await page();
    const change = await screen.findByRole("button", { name: "Change the routing table file" });
    expect(change).toHaveTextContent("Change…");
    fireEvent.click(change);
    const form = screen.getByRole("form", { name: "Change the routing table file" });
    expect(within(form).getByLabelText("File")).toHaveValue("/notes/routing/ROUTING-NOW.md");
    fireEvent.change(within(form).getByLabelText("File"), { target: { value: "/private/auth.json" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "Khipu didn't make that change: Routing table file can't name a file inside a private folder",
    );
    fireEvent.click(within(form).getByRole("button", { name: "Stop reading it" }));
    await waitFor(() => expect(calls("khipu_config_unset")).toEqual([{ key: "routing_table" }]));
  });
});

describe("Settings -> Routing: focus after a button removes itself (R2-2)", () => {
  /** The record as it reads after the write the test makes. */
  function afterWrite(cmd: string, change: (v: RouteView) => RouteView) {
    answers.khipu_route_view = () => (calls(cmd).length ? change(view()) : view());
  }

  it("Clear gives focus to the account's Mark paused until…", async () => {
    afterWrite("khipu_route_clear", (v) => ({
      ...v,
      accounts: v.accounts!.map((a) => (a.label === "Claude main" ? { ...a, state: "open", until: null } : a)),
    }));
    await page();
    const clear = screen.getByRole("button", { name: "Clear the limit for Claude main" });
    clear.focus();
    fireEvent.click(clear);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Clear the limit for Claude main" })).toBeNull());
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Mark Claude main paused until…" })),
    );
  });

  it("Lift gives focus to the account's Mark paused until…", async () => {
    afterWrite("khipu_route_lift", (v) => ({
      ...v,
      accounts: v.accounts!.map((a) => (a.label === "Grok Build" ? { ...a, state: "open", until: null } : a)),
    }));
    await page();
    const lift = screen.getByRole("button", { name: "Lift the pause for Grok Build" });
    lift.focus();
    fireEvent.click(lift);
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Mark Grok Build paused until…" })),
    );
  });

  it("Dismiss gives focus to the account's Mark paused until…", async () => {
    afterWrite("khipu_route_clear", (v) => ({
      ...v,
      accounts: v.accounts!.map((a) => (a.label === "Claude second" ? { ...a, disagreement: null } : a)),
    }));
    await page();
    const dismiss = within(screen.getByRole("group", { name: "Claude second: two reports disagree" })).getByRole(
      "button",
      { name: "Dismiss the runner's report" },
    );
    dismiss.focus();
    fireEvent.click(dismiss);
    await waitFor(() => expect(screen.queryByRole("group", { name: "Claude second: two reports disagree" })).toBeNull());
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Mark Claude second paused until…" })),
    );
  });

  it("Undo gives focus to the restored rule's Remove", async () => {
    answers.khipu_route_view = () => {
      const v = view();
      const done = calls("khipu_route_rule").map((c) => c!.action);
      return done.includes("retire") && !done.includes("restore") ? { ...v, rules: v.rules!.filter((r) => r.n !== 3) } : v;
    };
    await page();
    fireEvent.click(screen.getByRole("button", { name: "Remove rule 3" }));
    fireEvent.click(within(await screen.findByRole("group", { name: "Remove rule 3?" })).getByRole("button", { name: "Remove rule 3" }));
    const undo = await screen.findByRole("button", { name: "Undo" });
    undo.focus();
    fireEvent.click(undo);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Undo" })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Remove rule 3" })));
  });

  it("End now gives focus to the next hold's End now, then to Add hold", async () => {
    const second = { ...view().holds![0], id: "h2", scope_label: "all work", text: "Keep Codex for reviews." };
    answers.khipu_route_view = () => {
      const ended = calls("khipu_route_hold_end").map((c) => c!.id);
      const v = view();
      return { ...v, holds: [v.holds![0], second].filter((h) => !ended.includes(h.id)) };
    };
    await page();
    const first = await screen.findByRole("button", { name: "End the hold on this Mac" });
    first.focus();
    fireEvent.click(first);
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "End the hold on all work" })));
    fireEvent.click(screen.getByRole("button", { name: "End the hold on all work" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "End the hold on all work" })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Add hold" })));
  });

  it("leaves focus on a button the write keeps", async () => {
    answers.khipu_route_lift = { ok: false, error: "that account isn't paused" };
    await page();
    const lift = screen.getByRole("button", { name: "Lift the pause for Grok Build" });
    lift.focus();
    fireEvent.click(lift);
    expect(await screen.findByRole("alert")).toHaveTextContent("that account isn't paused");
    expect(document.activeElement).toBe(lift);
  });
});

describe("Settings -> Routing: a runner that went quiet on an earlier day (R2-3)", () => {
  it("says when it stopped reporting as a sentence", async () => {
    answers.khipu_route_view = () => {
      const v = view();
      const runners = v.sources!.runners!.map((r) => (r.name === "Project B" ? { ...r, heartbeat_at: "2026-10-13T14:02:00Z" } : r));
      return { ...v, sources: { ...v.sources!, runners } };
    };
    await page();
    await screen.findByRole("list", { name: "Job runner folders" });
    expect(row("Job runner folders", "Project B")).toHaveTextContent("Stopped reporting yesterday at 10:02 AM");
  });
});

describe("Settings -> Routing: no fixed cap on the owner's words (R2-4)", () => {
  it("sends a hold longer than 4,000 characters when Longest words is No limit, and says what the app can hand over", async () => {
    answers.khipu_config_show = config({ routing_hold_chars: setting(0, 300) });
    await page();
    expect(await screen.findByText("No limit on length; the app hands over up to 128 KB at once")).toBeInTheDocument();
    const words = "Keep this account for the nightly review only. ".repeat(100).trim();
    expect(words.length).toBeGreaterThan(4000);
    const form = screen.getByRole("form", { name: "Add a hold" });
    fireEvent.change(within(form).getByLabelText("Words"), { target: { value: words } });
    fireEvent.click(within(form).getByRole("button", { name: "Add hold" }));
    await waitFor(() => expect(calls("khipu_route_hold_add")).toHaveLength(1));
    expect(calls("khipu_route_hold_add")[0]!.text).toBe(words);
    expect(screen.getByText(/Khipu sets no limit on its\s+length/)).toBeInTheDocument();
  });
});

// ---- Fix round 1, F2: laid out in headless Chrome ------------------------------------------

const CWD = (globalThis as unknown as { process: { cwd(): string; env: Record<string, string | undefined> } }).process;
const CHROME = [
  CWD.env.KHIPU_TEST_CHROME,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
].find((p) => p && existsSync(p));

const GAPS = `(function () {
  var page = document.querySelector(".route-settings");
  var kids = Array.prototype.filter.call(page.children, function (e) { return e.getBoundingClientRect().height > 0; });
  var gaps = [];
  for (var i = 1; i < kids.length; i++) {
    gaps.push(Math.round(kids[i].getBoundingClientRect().top - kids[i - 1].getBoundingClientRect().bottom));
  }
  document.body.setAttribute("data-out", JSON.stringify({ gaps: gaps, n: kids.length }));
})();`;

describe.skipIf(!CHROME)("Settings -> Routing laid out (fix round 1)", () => {
  it("keeps the pane's space between the intro and every card, as the mock does (F2)", async () => {
    const { container } = render(<RoutingSettings active />);
    // Snapshot the page only once every read has landed: the accounts come
    // from the routing record, Needs you and the retry fields from the
    // settings, so an early snapshot on a busy machine had fewer cards.
    await screen.findByRole("list", { name: "Accounts" });
    await screen.findByRole("region", { name: "Needs you" });
    await waitFor(() => expect(screen.getByLabelText("First retry in seconds")).toHaveValue("600"));
    const css: string = readFileSync(`${CWD.cwd()}/src/App.css`, "utf8");
    const dir = mkdtempSync(`${tmpdir()}/khipu-routing-settings-`);
    try {
      const file = `${dir}/page.html`;
      writeFileSync(file, `<!doctype html><html lang="en"><head><meta charset="utf-8"><style>${css}</style></head>` +
        `<body><div id="root"><div class="settings-pane">${container.innerHTML}</div></div>` +
        `<script>${GAPS}</script></body></html>`);
      // The page measures itself in an inline script after the stylesheet in
      // its head (App.css loads no fonts or images), so no wait inside Chrome
      // decides the numbers. Chrome's exit code is not the verdict: on a busy
      // machine it can exit non-zero while shutting down after dumping the
      // page. The time limit is how long a stuck Chrome lasts, not a budget.
      const run = spawnSync(CHROME, [
        "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", `--user-data-dir=${dir}/profile`,
        "--window-size=1280,1600", "--dump-dom", `file://${file}`,
      ], { encoding: "utf8", timeout: 240000, maxBuffer: 64 * 1024 * 1024, stdio: ["ignore", "pipe", "pipe"] });
      const m = (run.stdout ?? "").match(/data-out="([^"]*)"/);
      if (!m) {
        throw new Error(`the page did not report its layout (status ${run.status}, signal ${run.signal}, ` +
          `${run.error?.message ?? "no error"}): ${(run.stderr ?? "").slice(-2000)}`);
      }
      const out = JSON.parse(m[1].replace(/&quot;/g, '"').replace(/&amp;/g, "&")) as { gaps: number[]; n: number };
      expect(out.n).toBeGreaterThanOrEqual(5);
      for (const g of out.gaps) expect(g).toBeGreaterThanOrEqual(12);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }, 300000);
});

// ---- Final fix round --------------------------------------------------------------------------

describe("Settings -> Routing: the routing table file in plain words", () => {
  it("names the setting as its row does, never by its key (S1)", async () => {
    answers.khipu_config_set = { ok: false, error: "routing_table needs the file's full path (starting with / or ~)" };
    await page();
    fireEvent.click(await screen.findByRole("button", { name: "Change the routing table file" }));
    const form = screen.getByRole("form", { name: "Change the routing table file" });
    fireEvent.change(within(form).getByLabelText("File"), { target: { value: "/notes/other.md" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    const alert = await within(form).findByRole("alert");
    expect(alert).toHaveTextContent("Khipu didn't make that change: Routing table file needs the file's full path");
    expect(alert.textContent).not.toContain("routing_table");
    expect(settingWords("set routing_table, not routing_tables", "routing_table", "Routing table file"))
      .toBe("set routing table file, not routing_tables");
  });

  it.each([
    ["missing", "Can't read it: no file at that path"],
    ["a folder, not a file", "Can't read it: that is a folder, not a file"],
    ["no NOW block", "Can't read it: it has no current-state section"],
  ])("says why the file can't be read: %s (S2)", async (error, words) => {
    answers.khipu_route_view = () => {
      const v = view();
      v.sources = { ...v.sources, table: { path: "/notes/routing/ROUTING-NOW.md", error } };
      return v;
    };
    await page();
    expect(await screen.findByText(words)).toBeInTheDocument();
  });

  it("maps the CLI's other phrases and never shows one it does not know (S2)", () => {
    expect(tableErrorWords("larger than 2 MB")).toBe("it is larger than 2 MB");
    expect(tableErrorWords("not allowed to read it")).toBe("Khipu isn't allowed to read it");
    expect(tableErrorWords("not a regular file")).toBe("that is not a plain file");
    expect(tableErrorWords("something new")).toBe("it can't be read");
  });
});

// ---- Cross-family (Codex) review of phase 4 -----------------------------------------------------

/** A khipu CLI stand-in for the routing settings: `config --set` changes
 *  what `khipu_config_show` answers, and every write waits for `release()`,
 *  so a second change can be made before the first one's re-read. */
function heldConfig() {
  let routing = config().routing as Record<string, ReturnType<typeof setting>>;
  let open: () => void = () => {};
  const gate = new Promise<void>((r) => {
    open = r;
  });
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    if (cmd === "khipu_config_show") return JSON.stringify({ routing });
    if (cmd === "khipu_config_set") {
      await gate;
      const key = String(args!.key);
      routing = { ...routing, [key]: setting(JSON.parse(String(args!.value)), routing[key]?.default) };
      return JSON.stringify({ ok: true });
    }
    const a = answers[cmd] ?? { ok: true };
    const out = typeof a === "function" ? await a(args) : a;
    return typeof out === "string" ? out : JSON.stringify(out);
  });
  return { release: () => open(), value: (key: string) => routing[key].value };
}

describe("Settings -> Routing: Codex review of phase 4", () => {
  it("keeps both lane changes made before the first one's re-read (finding 1)", async () => {
    const cli = heldConfig();
    await page();
    const b = await screen.findByRole("list", { name: "Project B lanes" });
    const opus = within(b).getAllByRole("listitem").find((r) => r.textContent?.startsWith("opus-batch"))!;
    fireEvent.change(within(opus).getByLabelText("Account for opus-batch"), { target: { value: "Claude main" } });
    fireEvent.click(within(opus).getByRole("button", { name: "Save" }));
    fireEvent.click(screen.getByRole("button", { name: "Change the account for Project A's codex lane" }));
    const a = screen.getByRole("list", { name: "Project A lanes" });
    const codex = within(a).getAllByRole("listitem").find((r) => r.textContent?.startsWith("codex"))!;
    fireEvent.change(within(codex).getByLabelText("Account for codex"), { target: { value: "Claude second" } });
    fireEvent.click(within(codex).getByRole("button", { name: "Save" }));
    cli.release();
    await waitFor(() => expect(calls("khipu_config_set")).toHaveLength(2));
    await waitFor(() =>
      expect(cli.value("routing_lane_map")).toEqual({
        "Project A": { codex: "Claude second", main: "Claude main", secondary: "Claude second" },
        "Project B": { secondary: "Claude second", "opus-batch": "Claude main" },
      }),
    );
  });

  it("keeps a folder added and one removed before the first re-read (finding 1)", async () => {
    const cli = heldConfig();
    await page();
    await screen.findByRole("list", { name: "Job runner folders" });
    fireEvent.click(screen.getByRole("button", { name: "Add a folder…" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Project C" } });
    fireEvent.change(screen.getByLabelText("Folder"), { target: { value: "/runners/c" } });
    fireEvent.click(screen.getByRole("button", { name: "Add folder" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove the Project B folder" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove Project B" }));
    cli.release();
    await waitFor(() => expect(calls("khipu_config_set")).toHaveLength(2));
    await waitFor(() =>
      expect(cli.value("routing_sources")).toEqual([
        { name: "Project A", kind: "lanes", path: "/runners/a" },
        { name: "Project C", kind: "lanes", path: "/runners/c" },
      ]),
    );
  });

  it("shows many holds without copying the list for each one (finding 2)", async () => {
    const many = Array.from({ length: 60 }, (_, i) => ({ ...view().holds![0], id: `h${i}`, scope_label: `scope ${i}` }));
    answers.khipu_route_view = { ...view(), holds: many };
    const slice = Array.prototype.slice;
    let copies = 0;
    const spy = vi.spyOn(Array.prototype, "slice").mockImplementation(function (this: unknown[], ...args: [number?, number?]) {
      if (this.length && (this[0] as { scope_label?: unknown })?.scope_label !== undefined) copies++;
      return slice.apply(this, args);
    });
    try {
      await page();
      await screen.findByRole("button", { name: "End the hold on scope 59" });
    } finally {
      spy.mockRestore();
    }
    expect(copies).toBeLessThan(many.length);
  });

  it("offers Dismiss on both sides of a model's disagreement, each with its own report (finding 3)", async () => {
    answers.khipu_route_view = () => {
      const v = view();
      v.accounts = [
        acct("Claude main", "claude", {
          state: "open",
          witness: { source: "probe", written_by: { kind: "probe" }, at: "2026-10-14T15:36:00Z" },
          models: [{
            model: "Opus", until: "2026-10-16T13:00:00Z", basis: "cli", source: "report", id: "r-opus",
            by: "a Codex conversation in project-b", at: "2026-10-14T15:20:00Z",
            disagreement: { id: "e-opus-open", state: "open", source: "report", by: "a Claude Code conversation in project-a", at: "2026-10-14T15:30:00Z" },
          }],
        }),
      ];
      return v;
    };
    await page();
    const panel = screen.getByRole("group", { name: "Claude main (Opus): two reports disagree" });
    expect(panel).toHaveTextContent("Limit reached: the Opus limit, reported by a Codex conversation in project-b 20 min ago.");
    expect(panel).toHaveTextContent("Open: reported by a Claude Code conversation in project-a 10 min ago.");
    const buttons = within(panel).getAllByRole("button");
    expect(buttons.map((x) => x.textContent)).toEqual([
      "Dismiss the conversation's “limit reached” report",
      "Dismiss the conversation's “open” report",
    ]);
    fireEvent.click(buttons[0]);
    await waitFor(() => expect(calls("khipu_route_clear")).toEqual([{ account: "claude|", report: "r-opus" }]));
  });

  it("words a model's limit and a disagreement in Recent changes, at the change's own time (finding 5)", () => {
    const v = view();
    v.changes = [
      { version: 20, at: "2026-10-13T15:00:00Z", entities: ["account:claude|"], items: [
        { entity: "account:claude|", source: "report", by: "a Codex conversation in project-b",
          now: { label: "Claude main", state: "open", disagreement: null, last_seen: null,
            models: [["Opus", "2026-10-13T19:00:00Z", "cli", null]] },
          was: { label: "Claude main", state: "open", disagreement: null, last_seen: null, models: [] } },
      ] },
      { version: 21, at: "2026-10-13T15:10:00Z", entities: ["account:claude|"], items: [
        { entity: "account:claude|", source: "report", by: "a Claude Code conversation in project-a",
          now: { label: "Claude main", state: "open", disagreement: null, last_seen: null,
            models: [["Opus", "2026-10-13T19:00:00Z", "cli", ["open", "report"]]] },
          was: { label: "Claude main", state: "open", disagreement: null, last_seen: null,
            models: [["Opus", "2026-10-13T19:00:00Z", "cli", null]] } },
      ] },
      { version: 22, at: "2026-10-13T15:20:00Z", entities: ["account:claude|/logins/claude-second"], items: [
        { entity: "account:claude|/logins/claude-second", source: "report", by: "a Codex conversation in project-b",
          now: { label: "Claude second", state: "limited", until: "2026-10-13T22:52:00Z", basis: "cli", disagreement: ["open", "report"], models: [] },
          was: { label: "Claude second", state: "limited", until: "2026-10-13T22:52:00Z", basis: "cli", disagreement: null, models: [] } },
      ] },
    ];
    const rows = changeDays(v, MORNING).flatMap((d) => d.rows).filter((r) => r.key.startsWith("2"));
    expect(rows.map((r) => [r.subject, r.text, r.aside])).toEqual([
      ["Claude second", "limit reached, back at 6:52 PM", "(a conversation said it is open)"],
      ["Claude main", "Opus limit reached, back at 3:00 PM; other models open", "(a conversation said Opus is open)"],
      ["Claude main", "Opus limit reached, back at 3:00 PM; other models open", undefined],
    ]);
  });

  it("shows every routing setting built so far, each changeable (finding 6)", async () => {
    answers.khipu_config_show = config({
      routing_report_default_limit: setting(1800),
      routing_start_budget_ms: setting(1000),
      routing_slow_read_pct: setting(1),
      routing_runner_silent_intervals: setting(5),
      routing_runner_tail_bytes: setting(524288),
      routing_runner_limited_lines: setting(200),
      routing_poll_interval: setting(10),
    });
    await page();
    await screen.findByText("512 KB");
    for (const [name, words] of [
      ["How long a limit reported without a time lasts", "30 minutes"],
      ["Time allowed at the start of a conversation", "1,000 ms"],
      ["Warn when reading before a prompt is too slow on more than", "1%"],
      ["Say a job runner stopped reporting after", "5 intervals"],
      ["How much of the end of a runner's events.log is read", "512 KB"],
      ["Newest limit lines kept from each runner", "200 lines"],
      ["Look at the folders and the routing table every", "10 seconds"],
    ]) {
      const btn = screen.getByRole("button", { name: `Change: ${name}` });
      expect(btn.closest(".route-limit")).toHaveTextContent(words);
    }
    fireEvent.click(screen.getByRole("button", { name: "Change: How much of the end of a runner's events.log is read" }));
    const select = screen.getByLabelText("Read the last");
    expect(within(select).getByRole("option", { name: "All of it" })).toBeInTheDocument();
    fireEvent.change(select, { target: { value: "0" } });
    fireEvent.click(within(screen.getByRole("form", { name: "Change: How much of the end of a runner's events.log is read" })).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls("khipu_config_set")).toEqual([{ key: "routing_runner_tail_bytes", value: "0" }]));
    fireEvent.click(screen.getByRole("button", { name: "Change: Warn when reading before a prompt is too slow on more than" }));
    fireEvent.change(screen.getByLabelText("Warn above"), { target: { value: "0.5" } });
    fireEvent.click(within(screen.getByRole("form", { name: "Change: Warn when reading before a prompt is too slow on more than" })).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls("khipu_config_set")[1]).toEqual({ key: "routing_slow_read_pct", value: "0.5" }));
  });

  it("says the master switch couldn't be read, and reads it again on Try again (finding 7)", async () => {
    let fail = true;
    answers.khipu_features_show = () => {
      if (fail) throw new Error("khipu features: timed out");
      return { features: { routing_push: { enabled: true, source: "file" } } };
    };
    await page();
    const alert = await screen.findByText(/Couldn't read this setting/);
    expect(alert).toHaveTextContent("Couldn't read this setting: khipu features: timed out");
    expect(screen.queryByRole("switch", { name: "Tell conversations about routing" })).toBeNull();
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getByRole("switch", { name: "Tell conversations about routing" })).toBeChecked());
    expect(screen.getByRole("switch", { name: "Tell conversations about routing" })).toBeEnabled();
    expect(screen.queryByText(/Couldn't read this setting/)).toBeNull();
  });

  it("says the master switch couldn't be read when the read after a change fails, not the old value (Codex re-check)", async () => {
    let reads = 0;
    answers.khipu_features_show = () => {
      reads += 1;
      if (reads > 1) throw new Error("khipu features: timed out");
      return { features: { routing_push: { enabled: true, source: "file" } } };
    };
    answers.khipu_feature_set = () => ({ ok: true });
    await page();
    const sw = await screen.findByRole("switch", { name: "Tell conversations about routing" });
    await waitFor(() => expect(sw).toBeChecked());
    fireEvent.click(sw);
    const alert = await screen.findByText(/Couldn't read this setting/);
    expect(alert).toHaveTextContent("khipu features: timed out");
    expect(screen.queryByRole("switch", { name: "Tell conversations about routing" })).toBeNull();
  });
});

// Settings clarity phase 1 (docs/plans/2026-10-15-settings-clarity.md, 6a):
// a switch whose value is not read yet is never drawn as off.
describe("Settings -> Routing: nothing shows a false off while it is read", () => {
  const SWITCHES = [
    ["route-push-all", "Tell conversations about routing"],
    ["route-push-start", "At the start of a conversation, and after it is compacted"],
    ["route-push-prompt", "Before each prompt"],
    ["route-push-delegation", "Before handing work to another agent"],
    ["route-reset-check", "Check an account at its reset time"],
  ] as const;

  function hold<T>() {
    let release!: (v: T) => void;
    const p = new Promise<T>((r) => (release = r));
    return { p, release };
  }

  it("shows Reading… (aria-busy) in place of each switch until its settings are read, then the real switch", async () => {
    const cfg = hold<unknown>();
    const feats = hold<unknown>();
    answers.khipu_config_show = () => cfg.p;
    answers.khipu_features_show = () => feats.p;
    render(<RoutingSettings active />);
    await screen.findByRole("list", { name: "Accounts" });
    for (const [id, name] of SWITCHES) {
      expect(screen.queryByRole("switch", { name })).toBeNull();
      const slot = screen.getByTestId(`${id}-reading`);
      expect(slot).toHaveAttribute("aria-busy", "true");
      expect(slot).toHaveTextContent("Reading…");
    }
    // Folders and lanes say Reading…, never "none yet", before the settings arrive.
    expect(screen.queryByText(/No job runner folders yet/)).toBeNull();
    expect(screen.queryByText(/Lanes show here once/)).toBeNull();
    expect(screen.getByText(/Looking for job runner folders/).closest("[aria-busy]")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByText(/Looking for job runner lanes/)).toBeInTheDocument();
    cfg.release(config({ routing_push_prompt: setting(false) }));
    feats.release({ features: { routing_push: { enabled: true, source: "file" } } });
    await waitFor(() => expect(screen.getByRole("switch", { name: "Before each prompt" })).not.toBeChecked());
    for (const [, name] of SWITCHES.filter(([id]) => id !== "route-push-prompt")) {
      expect(screen.getByRole("switch", { name })).toBeChecked();
    }
    expect(screen.queryByText("Reading…")).toBeNull();
  });

  it("says Can't read this setting in place of each switch when the settings read fails, with Try again", async () => {
    let fail = true;
    answers.khipu_config_show = () => {
      if (fail) throw new Error("khipu config: timed out");
      return config();
    };
    await page();
    for (const [id, name] of SWITCHES.filter(([id]) => id !== "route-push-all")) {
      await waitFor(() => expect(screen.getByTestId(`${id}-unread`)).toHaveTextContent("Can't read this setting"));
      expect(screen.queryByRole("switch", { name })).toBeNull();
    }
    expect(screen.getByRole("switch", { name: "Tell conversations about routing" })).toBeChecked();
    expect(screen.getByText(/Couldn't read job runner folders just now/)).toBeInTheDocument();
    fail = false;
    fireEvent.click(within(screen.getByTestId("route-push-start-unread")).getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getByRole("switch", { name: "Before each prompt" })).toBeChecked());
  });

  it("says Reading… for folders and lanes before the routing record is read", async () => {
    const rec = hold<unknown>();
    answers.khipu_route_view = () => rec.p;
    render(<RoutingSettings active />);
    await screen.findByText(/Looking for job runner lanes/);
    expect(screen.queryByText(/Lanes show here once/)).toBeNull();
    rec.release(view());
    await screen.findByRole("list", { name: "Accounts" });
    await waitFor(() => expect(screen.queryByText(/Looking for job runner lanes/)).toBeNull());
  });

  it("shows a conversation's change to a routing setting and answers it as the owner", async () => {
    const lanes = setting({ jobs: { r: "codex|" } }, {});
    answers.khipu_config_show = config({
      routing_lane_map: { ...lanes, words: "Which account each job lane uses",
        request: { request: "send lane r to Codex", by_words: "a Claude Code conversation in project-a", prior: {} } },
    });
    await page();
    const box = screen.getByRole("group", { name: "A conversation changed: Which account each job lane uses" });
    expect(box).toHaveTextContent("changed by a Claude Code conversation in project-a at your request");
    expect(box).toHaveTextContent("What it said you asked: “send lane r to Codex”");
    fireEvent.click(within(box).getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(calls("khipu_config_request")).toEqual([{ key: "routing_lane_map", action: "undo" }]));
    fireEvent.click(within(box).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(calls("khipu_config_request")).toHaveLength(2));
    expect(calls("khipu_config_request")[1]).toEqual({ key: "routing_lane_map", action: "confirm" });
  });

  it("shows no request box when nothing waits", async () => {
    await page();
    expect(screen.queryByRole("group", { name: /^A conversation changed:/ })).toBeNull();
  });
});
