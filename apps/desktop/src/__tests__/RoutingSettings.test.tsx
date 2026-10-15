// Routing truth phase 4, second half (docs/plans/2026-10-14-routing-truth.md):
// Settings -> Routing as in docs/mockups/2026-10-14-routing/settings-routing.html.
// Each control renders, checks what it can before it writes, calls its
// fixed-argv command with the right fields, re-reads, and shows the CLI's
// own error in plain words when the CLI refuses. Times are New York local.
(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { RoutingSettings } from "../RoutingSettings";
import type { RouteAccount, RouteView } from "../routing";
import { changeDays, chosenEnd, durationWords, endChoices, isoLocal, limitWords, ruleOrigin } from "../routingWords";
// The stylesheet as written, for the headless Chrome layout check (vitest
// hands CSS imports over empty). The app has no @types/node.
// @ts-expect-error node:fs has no types here
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
// @ts-expect-error node:child_process has no types here
import { execFileSync } from "node:child_process";
// @ts-expect-error node:os has no types here
import { tmpdir } from "node:os";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

// Wed 2026-10-14, 11:40 AM in New York (the mock's time).
const MORNING = new Date("2026-10-14T15:40:00Z");

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

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(MORNING);
  answers = {
    khipu_route_view: view(),
    khipu_config_show: config(),
    khipu_features_show: { features: { routing_push: { enabled: true, source: "file" } } },
  };
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    const a = answers[cmd] ?? { ok: true };
    const out = typeof a === "function" ? a(args) : a;
    return typeof out === "string" ? out : JSON.stringify(out);
  });
});

afterEach(() => {
  cleanup();
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
});

describe("Settings -> Routing: accounts", () => {
  it("shows each account with its state and who said so, and no Check now", async () => {
    await page();
    expect(row("Accounts", "Claude main")).toHaveTextContent("Limit reached, back at 1:52 PM");
    expect(row("Accounts", "Claude main")).toHaveTextContent("Read from Claude 4 min ago");
    expect(row("Accounts", "Grok Build")).toHaveTextContent("Paused by you until Fri 9:00 AM");
    expect(screen.queryByRole("button", { name: /check/i })).toBeNull();
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
    expect(three).toHaveTextContent("Watched");
    const seven = row("Standing rules", "7.");
    expect(seven).toHaveTextContent("Confirmed");
    expect(seven).toHaveTextContent("Not watched by Khipu");
    expect(within(seven).queryByRole("button", { name: "Confirm rule 7" })).toBeNull();
  });

  it("confirms a rule only after asking", async () => {
    await page();
    fireEvent.click(screen.getByRole("button", { name: "Confirm rule 3" }));
    expect(calls("khipu_route_rule")).toEqual([]);
    const ask = screen.getByRole("group", { name: "Confirm rule 3?" });
    fireEvent.click(within(ask).getByRole("button", { name: "Confirm rule 3" }));
    await waitFor(() => expect(calls("khipu_route_rule")).toEqual([{ action: "confirm", n: 3 }]));
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
  it("has the master switch and the two moments, and no delegation switch", async () => {
    await page();
    expect(screen.getByRole("switch", { name: "Tell conversations about routing" })).toBeChecked();
    expect(screen.queryByText(/handing work to another agent/)).toBeNull();
    expect(screen.queryByText(/Copy for cloud agents/)).toBeNull();
    expect(screen.queryByText(/Checking for resets/)).toBeNull();
    fireEvent.click(screen.getByRole("switch", { name: "Tell conversations about routing" }));
    await waitFor(() => expect(calls("khipu_feature_set")).toEqual([{ name: "routing_push", enabled: false }]));
    fireEvent.click(screen.getByRole("switch", { name: "Before each prompt" }));
    await waitFor(() =>
      expect(calls("khipu_config_set")).toEqual([{ key: "routing_push_prompt", value: "off" }]),
    );
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
    const list = await screen.findByRole("list", { name: "Project B" });
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
    expect(screen.getByText("NOW block changed 9:20 AM")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /routing table/i })).toBeNull();
    const today = screen.getByRole("list", { name: "Today" });
    expect(within(today).getAllByRole("listitem")[0]).toHaveTextContent("Standing rule 6 removed");
    expect(screen.getByRole("list", { name: "Yesterday" })).toHaveTextContent("Grok Build paused by you");
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
    await screen.findByRole("list", { name: "Accounts" });
    const css: string = readFileSync(`${CWD.cwd()}/src/App.css`, "utf8");
    const dir = mkdtempSync(`${tmpdir()}/khipu-routing-settings-`);
    try {
      const file = `${dir}/page.html`;
      writeFileSync(file, `<!doctype html><html lang="en"><head><meta charset="utf-8"><style>${css}</style></head>` +
        `<body><div id="root"><div class="settings-pane">${container.innerHTML}</div></div>` +
        `<script>${GAPS}</script></body></html>`);
      const dom: string = execFileSync(CHROME, [
        "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", `--user-data-dir=${dir}/profile`,
        "--window-size=1280,1600", "--virtual-time-budget=2000", "--dump-dom", `file://${file}`,
      ], { encoding: "utf8", timeout: 60000, stdio: ["ignore", "pipe", "ignore"] });
      const m = dom.match(/data-out="([^"]*)"/);
      if (!m) throw new Error("the page did not report its layout");
      const out = JSON.parse(m[1].replace(/&quot;/g, '"').replace(/&amp;/g, "&")) as { gaps: number[]; n: number };
      expect(out.n).toBeGreaterThanOrEqual(5);
      for (const g of out.gaps) expect(g).toBeGreaterThanOrEqual(12);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }, 90000);
});
