// Routing slice 15b: Proposals and holds. A conversation's proposal shows
// beside the owner's words and becomes the owner's only through his own
// action on the existing owner-action commands; a write that names a held
// or paused account waits for his end-or-keep choice for each one.
(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { RoutingSettings, replicationStatusWords } from "../RoutingSettings";
import { endedWords, holdChoiceIds, holdChoiceTargets, proposalsModel } from "../routingProposals";
import type { RouteView } from "../routing";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

const MORNING = new Date("2026-10-14T15:40:00Z");

function view(extra: Partial<RouteView> = {}): RouteView {
  return {
    record: true,
    version: 12,
    checked_at: "2026-10-14T15:40:00Z",
    accounts: [
      { key: "codex|", label: "Codex", harness: "codex", state: "open", models: [], witness: null, disagreement: null, last_seen: null },
      { key: "grok|", label: "Grok", harness: "grok", state: "paused", until: "2026-10-16T13:00:00Z",
        witness: { id: "pause-1", source: "owner", written_by: { kind: "owner" }, at: "2026-10-14T13:00:00Z" },
        models: [], disagreement: null, last_seen: null },
    ],
    holds: [
      { id: "h-grok", scope_label: "Grok", reason: "keep", text: "Keep Grok for Grok Bot this week",
        until: "2026-10-16T22:00:00Z", owner: true, by: "you", at: "2026-10-14T13:14:00Z" },
    ],
    rules: [
      { n: 1, entity: "rule-a", text: "Use at most 76% of the xAI week.", pending: true, pending_kind: "changed",
        pending_by: "a Claude Code conversation in project-a", prior_text: "Use at most 80% of the xAI week." },
    ],
    priority: {
      entries: [{ account: "codex|", label: "Codex", role: "Paced: at most 15 points ahead of an even pace" }],
      pending: true, pending_by: "a Codex conversation in project-b", owner_request: "loosen Codex's pace",
      replaced: { entries: [{ account: "codex|", label: "Codex", role: "Paced: at most 10 points ahead of an even pace" }], until: null },
      version: "p3",
    },
    priority_version: "p3",
    proposals: {
      "prop-rule": { id: "prop-rule", entity: "rule-a", kind: "rule", n: 1, status: "live", at: "2026-10-14T15:15:00Z",
        author: { kind: "conversation", harness: "claude_code", project: "project-a" } },
      "prop-prio": { id: "prop-prio", entity: "priority", kind: "priority", status: "live", at: "2026-10-14T14:40:00Z",
        author: { kind: "conversation", harness: "codex", project: "project-b" } },
      "prop-crossed": { id: "prop-crossed", entity: "rule-b", kind: "rule", n: 6, status: "crossed", at: "2026-10-14T12:00:00Z",
        author: { kind: "conversation", harness: "grok_build", project: "project-d" } },
      "prop-done": { id: "prop-done", entity: "rule-c", kind: "rule", n: 9, status: "dismissed", at: "2026-10-13T12:00:00Z",
        ended_at: "2026-10-14T09:00:00Z", author: { kind: "conversation", harness: "codex", project: "project-b" } },
    },
    note: null,
    changes: [],
    ...extra,
  };
}

function setting(value: unknown, def: unknown = value) {
  return { value, default: def, kind: "x", source: "default" };
}

function config() {
  return {
    routing: {
      routing_conversation_hold: setting(86400),
      routing_conversation_hold_max: setting(604800),
      routing_owner_hold_reminder: setting(7776000),
      routing_hold_chars: setting(300),
      routing_proposal_text_between_macs: setting(true),
      routing_replication_live: setting(false),
      routing_push_start: setting(true),
      routing_push_prompt: setting(true),
      routing_push_delegation: setting(true),
    },
  };
}

type Answer = string | Record<string, unknown> | ((args: Record<string, unknown> | undefined) => unknown);
let answers: Record<string, Answer>;

function memoryStorage() {
  const m = new Map<string, string>();
  return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, String(v)), removeItem: (k: string) => void m.delete(k), clear: () => m.clear() };
}

beforeEach(() => {
  vi.stubGlobal("localStorage", memoryStorage());
  localStorage.setItem("khipu.routing.open", JSON.stringify(["holds", "telling", "sub:sizes"]));
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
  return invokeMock.mock.calls.filter((c) => c[0] === cmd).map((c) => {
    const { op, ...args } = (c[1] ?? {}) as Record<string, unknown>;
    if (cmd.startsWith("khipu_route_")) expect(typeof op === "string" && op.length > 0, `${cmd} carries an op id`).toBe(true);
    return args;
  });
}

async function page() {
  render(<RoutingSettings active />);
  await screen.findByRole("list", { name: "Proposals" });
}

describe("Proposals and holds: proposals beside the owner's words", () => {
  it("shows each live proposal beside the owner's words, crossed apart, ended folded away", async () => {
    await page();
    const list = screen.getByRole("list", { name: "Proposals" });
    const rule = within(list).getByText("Rule 1").closest(".mrow")!;
    expect(rule).toHaveTextContent("Use at most 80% of the xAI week.");
    expect(rule).toHaveTextContent("Use at most 76% of the xAI week.");
    expect(rule).toHaveTextContent("In use");
    const prio = within(list).getByText("Your priority").closest(".mrow")!;
    expect(prio).toHaveTextContent("at most 10 points ahead");
    expect(prio).toHaveTextContent("at most 15 points ahead");
    expect(prio).toHaveTextContent("What it said you asked: “loosen Codex's pace”");
    const crossed = within(list).getByText("Rule 6").closest(".mrow")!;
    expect(crossed).toHaveTextContent("Crossed");
    expect(crossed).toHaveTextContent("never took effect");
    expect(within(list).queryByRole("button", { name: "Confirm the proposed rule 6" })).toBeNull();
    expect(screen.queryByRole("list", { name: "Ended proposals" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show" }));
    const ended = await screen.findByRole("list", { name: "Ended proposals" });
    expect(ended).toHaveTextContent("Rule 9");
    expect(screen.getByText(/Ended today: 1 \(1 dismissed\)/)).toBeInTheDocument();
  });

  it("confirms a rule proposal only through the owner's Confirm, on the existing rule command", async () => {
    await page();
    fireEvent.click(screen.getByRole("button", { name: "Confirm the proposed rule 1" }));
    await waitFor(() => expect(calls("khipu_route_rule")).toEqual([{ action: "confirm", n: 1 }]));
  });

  it("dismisses a priority proposal in two steps, through Undo on the existing priority command", async () => {
    await page();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss the proposed your priority" }));
    expect(calls("khipu_route_priority")).toEqual([]);
    const group = await screen.findByRole("group", { name: "Dismiss the proposed your priority?" });
    fireEvent.click(within(group).getByRole("button", { name: "Dismiss it" }));
    await waitFor(() => expect(calls("khipu_route_priority")).toEqual([{ action: "undo", expect: "p3" }]));
  });

  it("edits a rule proposal's words, then makes them the owner's in two steps", async () => {
    await page();
    fireEvent.click(screen.getByRole("button", { name: "Edit the proposed rule 1" }));
    const form = screen.getByRole("form", { name: "Edit the proposed rule 1" });
    fireEvent.change(within(form).getByLabelText("Words"), { target: { value: "Use at most 70% of the xAI week." } });
    fireEvent.click(within(form).getByRole("button", { name: "Save…" }));
    expect(calls("khipu_route_rule")).toEqual([]);
    fireEvent.click(within(form).getByRole("button", { name: "Make them mine" }));
    await waitFor(() => expect(calls("khipu_route_rule")).toEqual([{ action: "edit", n: 1, text: "Use at most 70% of the xAI week." }]));
  });

  it("a proposal whose words stay on another Mac offers Dismiss, never Confirm", async () => {
    answers.khipu_route_view = view({
      rules: [{ n: 1, entity: "rule-a", text: "Use at most 80% of the xAI week.", pending: true, pending_kind: "changed",
        pending_by: "a Claude Code conversation in project-a", prior_text: "Use at most 80% of the xAI week.", proposal_withheld: true }],
    });
    await page();
    expect(screen.queryByRole("button", { name: "Confirm the proposed rule 1" })).toBeNull();
    expect(screen.getByText(/Its words stay on the Mac it came from/)).toBeInTheDocument();
    expect(screen.getByText(/Share proposal words between your Macs” is off/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss the proposed rule 1" }));
    const group = await screen.findByRole("group", { name: "Dismiss the proposed rule 1?" });
    fireEvent.click(within(group).getByRole("button", { name: "Dismiss it" }));
    await waitFor(() => expect(calls("khipu_route_rule")).toEqual([{ action: "dismiss", n: 1 }]));
  });

  it("asks end or keep for each held or paused account a confirm names, then retries the same write with the choices", async () => {
    let tried = false;
    answers.khipu_route_priority = () => {
      if (!tried) {
        tried = true;
        return { ok: false, error: "say end or keep in holds for: h-grok, pause-1" };
      }
      return { ok: true };
    };
    await page();
    fireEvent.click(screen.getByRole("button", { name: "Confirm the proposed your priority" }));
    const panel = await screen.findByRole("group", { name: "End or keep each hold" });
    expect(within(panel).getByText(/Grok: held by you/)).toBeInTheDocument();
    expect(within(panel).getByText(/Grok: paused by you/)).toBeInTheDocument();
    const save = within(panel).getByRole("button", { name: "Save and confirm" });
    expect(save).toBeDisabled();
    const fieldsets = within(panel).getAllByRole("group");
    expect(fieldsets).toHaveLength(2);
    fireEvent.click(within(fieldsets[0]).getByLabelText("End the hold now"));
    fireEvent.click(within(fieldsets[1]).getByLabelText("Keep it"));
    expect(save).not.toBeDisabled();
    fireEvent.click(save);
    await waitFor(() => expect(calls("khipu_route_priority")).toEqual([
      { action: "confirm", expect: "p3" },
      { action: "confirm", expect: "p3", holds: { "h-grok": "end", "pause-1": "keep" } },
    ]));
  });

  it("never shows the owner-request words of a withheld priority proposal, in either priority section", async () => {
    answers.khipu_route_view = view({
      priority: {
        entries: [{ account: "codex|", label: "Codex", role: "Paced: at most 15 points ahead of an even pace" }],
        pending: true, pending_by: "a Codex conversation in project-b", proposal_withheld: true,
        owner_request: "SECRET OWNER WORDS",
        replaced: { entries: [{ account: "codex|", label: "Codex", role: "Paced: at most 10 points ahead of an even pace" }], until: null },
        version: "p3",
      },
    });
    await page();
    expect(document.body.textContent).not.toContain("SECRET OWNER WORDS");
    expect(document.body.textContent).not.toContain("What it said you asked");
    const list = screen.getByRole("list", { name: "Proposals" });
    const prio = within(list).getByText("Your priority").closest(".mrow")!;
    expect(prio).toHaveTextContent("Its words stay on the Mac it came from");
  });

  it("puts an overtaken proposal in the ended group, labelled as replaced by the owner, not in the main list", async () => {
    answers.khipu_route_view = view({
      proposals: {
        "prop-crossed": { id: "prop-crossed", entity: "rule-b", kind: "rule", n: 6, status: "crossed", at: "2026-10-14T12:00:00Z",
          author: { kind: "conversation", harness: "grok_build", project: "project-d" } },
        "prop-overtaken": { id: "prop-overtaken", entity: "rule-z", kind: "rule", n: 8, status: "overtaken", at: "2026-10-13T12:00:00Z",
          author: { kind: "conversation", harness: "codex", project: "project-b" } },
      },
    });
    await page();
    const list = screen.getByRole("list", { name: "Proposals" });
    expect(within(list).getByText("Rule 6")).toBeInTheDocument();
    expect(within(list).queryByText("Rule 8")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show" }));
    const ended = await screen.findByRole("list", { name: "Ended proposals" });
    const row = within(ended).getByText("Rule 8").closest(".mrow")!;
    expect(row).toHaveTextContent(/replaced by you/i);
  });

  it("refuses a rule proposal whose rule is not in this read, and never sends the priority command", async () => {
    answers.khipu_route_view = view({
      proposals: {
        "prop-missing": { id: "prop-missing", entity: "rule-missing", kind: "rule", n: 4, status: "live", at: "2026-10-14T15:15:00Z",
          author: { kind: "conversation", harness: "claude_code", project: "project-a" } },
      },
    });
    await page();
    const list = screen.getByRole("list", { name: "Proposals" });
    const row = within(list).getByText("Rule 4").closest(".mrow")! as HTMLElement;
    expect(row).toHaveTextContent(/isn't in this read/);
    expect(within(row).queryByRole("button", { name: "Confirm the proposed rule 4" })).toBeNull();
    expect(within(row).queryByRole("button", { name: "Dismiss the proposed rule 4" })).toBeNull();
    expect(calls("khipu_route_priority")).toEqual([]);
    expect(calls("khipu_route_rule")).toEqual([]);
  });

  it("lists each thing to confirm once in Needs you, under the proposals line alone", async () => {
    await page();
    const needs = screen.getByRole("region", { name: "Needs you" });
    const lines = within(needs).getAllByRole("listitem").map((li) => li.querySelector("span")!.textContent);
    expect(lines).toEqual(["2 proposals from conversations to confirm"]);
  });

  it("does not call an ended proposal 'today' when it has no ended time, however recent its write time", async () => {
    answers.khipu_route_view = view({
      proposals: {
        "prop-undated": { id: "prop-undated", entity: "rule-c", kind: "rule", n: 9, status: "dismissed", at: "2026-10-14T09:00:00Z",
          author: { kind: "conversation", harness: "codex", project: "project-b" } },
      },
    });
    render(<RoutingSettings active />);
    const show = await screen.findByRole("button", { name: "Show" });
    expect(screen.queryByText(/Ended today/)).toBeNull();
    fireEvent.click(show);
    const ended = await screen.findByRole("list", { name: "Ended proposals" });
    expect(ended).toHaveTextContent("Rule 9");
  });

  it("shows the proposal-text switch in Telling conversations, on by default", async () => {
    await page();
    const sw = screen.getByRole("switch", { name: /Share proposal words between your Macs/ });
    expect(sw).toBeChecked();
    fireEvent.click(sw);
    await waitFor(() => expect(calls("khipu_config_set")).toContainEqual({ key: "routing_proposal_text_between_macs", value: "off" }));
  });

  it("shows the replication switch off by default, beside the proposal-text switch, with its status", async () => {
    await page();
    const sw = screen.getByRole("switch", { name: /Copy routing between your Macs through the hub/ });
    expect(sw).not.toBeChecked();
    expect(screen.getByText(/Mac-to-Mac copy, separate from the copy for cloud agents/)).toBeInTheDocument();
    expect(screen.getByText(/Replication: never synced/)).toBeInTheDocument();
    fireEvent.click(sw);
    await waitFor(() => expect(calls("khipu_config_set")).toContainEqual({ key: "routing_replication_live", value: "on" }));
  });

  it("words replication status as caught up, stale or never synced", () => {
    expect(replicationStatusWords({ status: "green", caught_up_at: "2026-10-14T15:00:00Z", stale: false })).toBe("caught up");
    expect(replicationStatusWords({ status: "red", caught_up_at: "2026-10-14T15:00:00Z", stale: true })).toBe("stale");
    expect(replicationStatusWords({ status: "unknown", age_seconds: null, caught_up_at: null })).toBe("never synced");
    expect(replicationStatusWords(null)).toBe("never synced");
  });
});

describe("Proposals and holds: the pure model", () => {
  it("parses only the CLI's end-or-keep refusal", () => {
    expect(holdChoiceIds("say end or keep in holds for: h1, e2")).toEqual(["h1", "e2"]);
    expect(holdChoiceIds("the priority changed after it was read; read it again")).toEqual([]);
    expect(holdChoiceIds(null)).toEqual([]);
  });

  it("names a hold and a paused account by the ids a refusal uses", () => {
    const targets = holdChoiceTargets(view(), ["h-grok", "pause-1", "gone"], MORNING);
    expect(targets[0].legend).toContain("Grok: held by you");
    expect(targets[1].legend).toContain("Grok: paused by you");
    expect(targets[1].endLabel).toBe("End the pause now");
    expect(targets[2].endLabel).toBe("End it now");
  });

  it("keeps live, crossed and ended proposals apart", () => {
    const model = proposalsModel(view(), MORNING);
    expect(model.live.map((r) => r.name)).toEqual(["Rule 1", "Your priority"]);
    expect(model.stale.map((r) => r.proposal.status)).toEqual(["crossed"]);
    expect(model.ended.map((r) => r.proposal.status)).toEqual(["dismissed"]);
    expect(model.live[0].owner).toBe("Use at most 80% of the xAI week.");
    expect(model.live[0].proposed).toBe("Use at most 76% of the xAI week.");
  });

  it("moves a crossed proposal to the ended once it is older than 48 hours by its written time", () => {
    const older = new Date(MORNING.getTime() - 49 * 3600 * 1000).toISOString();
    const edge = new Date(MORNING.getTime() - 48 * 3600 * 1000).toISOString();
    const v = view({
      proposals: {
        "prop-old": { id: "prop-old", entity: "rule-b", kind: "rule", n: 6, status: "crossed", at: older,
          author: { kind: "conversation", harness: "grok_build", project: "project-d" } },
        "prop-edge": { id: "prop-edge", entity: "rule-e", kind: "rule", n: 7, status: "crossed", at: edge,
          author: { kind: "conversation", harness: "grok_build", project: "project-d" } },
      },
    });
    const model = proposalsModel(v, MORNING);
    expect(model.stale.map((r) => r.proposal.id)).toEqual(["prop-edge"]);
    expect(model.ended.map((r) => r.proposal.id)).toEqual(["prop-old"]);
  });

  it("counts overtaken as ended, with a label that says the owner replaced it", () => {
    const v = view({
      proposals: {
        "prop-overtaken": { id: "prop-overtaken", entity: "rule-z", kind: "rule", n: 8, status: "overtaken", at: "2026-10-13T12:00:00Z",
          author: { kind: "conversation", harness: "codex", project: "project-b" } },
      },
    });
    const model = proposalsModel(v, MORNING);
    expect(model.live).toEqual([]);
    expect(model.stale).toEqual([]);
    expect(model.ended.map((r) => r.proposal.status)).toEqual(["overtaken"]);
    expect(endedWords("overtaken")).toMatch(/replaced by you/i);
  });

  it("counts Ended today only from an ended time, never from the write time", () => {
    const undated = view({
      proposals: {
        "prop-undated": { id: "prop-undated", entity: "rule-c", kind: "rule", n: 9, status: "dismissed", at: "2026-10-14T09:00:00Z",
          author: { kind: "conversation", harness: "codex", project: "project-b" } },
      },
    });
    expect(proposalsModel(undated, MORNING).endedToday).toBe(0);
    const dated = view({
      proposals: {
        "prop-dated": { id: "prop-dated", entity: "rule-c", kind: "rule", n: 9, status: "dismissed", at: "2026-10-13T09:00:00Z",
          ended_at: "2026-10-14T09:00:00Z", author: { kind: "conversation", harness: "codex", project: "project-b" } },
      },
    });
    expect(proposalsModel(dated, MORNING).endedToday).toBe(1);
  });
});
