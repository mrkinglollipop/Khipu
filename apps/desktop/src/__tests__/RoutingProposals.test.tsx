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
// @ts-expect-error node:fs has no types here
import { readFileSync } from "node:fs";
import { SETTING_WORDS_KEYS, decideCards, priorityChanges, settingValueWords } from "../routingNeeds";
import type { RoutePriority, RouteRule } from "../routing";
import type { RouteSetting } from "../routingNeeds";
import { proposalAuthor } from "../routingProposals";

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
      { n: 1, entity: "rule-a", revision: "shown-rule-1", text: "Use at most 76% of the xAI week.", pending: true, pending_kind: "changed",
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
  await screen.findByRole("region", { name: "Needs you" });
}

async function proposalHistory() {
  fireEvent.click(screen.getByRole("button", { name: "What changed" }));
  return screen.findByRole("list", { name: "Proposals" });
}

describe("Proposals and holds: proposals beside the owner's words", () => {
  it("keeps a live note proposal in What changed because it has no Decide card", async () => {
    answers.khipu_route_view = view({
      note: { text: "Keep reviews on this Mac." },
      proposals: {
        "prop-note": { id: "prop-note", entity: "note", kind: "note", status: "live", at: "2026-10-14T15:15:00Z" },
      },
    });
    await page();
    const list = await proposalHistory();
    expect(within(list).getByRole("listitem")).toHaveAttribute("data-route-item", "proposal:prop-note");
    expect(list).toHaveTextContent("In use");
  });

  it("shows changed priority lines first, and reveals every original entry with Show all 20", async () => {
    const entries = Array.from({ length: 20 }, (_, i) => ({ account: `account-${i + 1}|`, label: `Account ${i + 1}`, role: `role-${i + 1}` }));
    const next = [entries[7], ...entries.slice(0, 7), ...entries.slice(8)].map((e) => e.account === "account-3|" ? { ...e, role: "Changed review role" } : e);
    answers.khipu_route_view = view({ priority: { entries: next, pending: true, version: "shown-20", replaced: { entries, until: null } } });
    await page();
    const card = await screen.findByRole("article", { name: /A conversation put Account/ });
    expect(card).toHaveTextContent("3. Account 3: role-3");
    expect(card).toHaveTextContent("4. Account 3: Changed review role");
    expect(within(card).queryByText(/Account 10: role-10/)).toBeNull();
    fireEvent.click(within(card).getByRole("button", { name: "Show all 20" }));
    expect(within(card).getAllByText(/Account 10: role-10/)).toHaveLength(2);
    expect(card).toHaveTextContent("20. Account 20: role-20");
    expect(calls("khipu_route_priority")).toEqual([]);
  });

  it("has one active proposal presentation on Needs you and keeps Edit and Dismiss reachable", async () => {
    await page();
    const card = await screen.findByRole("article", { name: "A conversation reworded your rule 1" });
    expect(within(screen.getByRole("region", { name: "Needs you" })).queryByRole("list", { name: "Proposals" })).toBeNull();
    expect(within(card).getByRole("button", { name: "Edit them first" })).toBeVisible();
    expect(within(card).getByRole("button", { name: "Keep my words" })).toBeVisible();
    expect(card).not.toHaveTextContent("Answer it on Needs you");
  });

  it("shows each live proposal beside the owner's words, crossed apart, ended folded away", async () => {
    await page();
    const rule = await screen.findByRole("article", { name: "A conversation reworded your rule 1" });
    expect(rule).toHaveTextContent("Use at most 80% of the xAI week.");
    expect(rule).toHaveTextContent("Use at most 76% of the xAI week.");
    expect(rule).toHaveTextContent("The words in use now");
    const prio = screen.getByRole("article", { name: /A conversation changed your order/ });
    expect(prio).toHaveTextContent("at most 10 points ahead");
    expect(prio).toHaveTextContent("at most 15 points ahead");
    expect(prio).toHaveTextContent("It says you asked“loosen Codex's pace”");
    const list = await proposalHistory();
    expect(within(list).queryByText("Rule 1")).toBeNull();
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

  it("confirms a rule proposal only through the owner's button, on the existing rule command", async () => {
    await page();
    const card = screen.getByRole("article", { name: "A conversation reworded your rule 1" });
    expect(card).toHaveTextContent("Your wordsUse at most 80% of the xAI week.");
    expect(card).toHaveTextContent("The words in use nowUse at most 76% of the xAI week.");
    expect(card).toHaveTextContent(/then your words come back/);
    fireEvent.click(within(card).getByRole("button", { name: "Keep the new words" }));
    await waitFor(() => expect(calls("khipu_route_rule")).toEqual([{ action: "confirm", n: 1, expect: "shown-rule-1" }]));
  });

  it("goes back to the owner's priority in two steps, through Undo on the existing priority command", async () => {
    await page();
    fireEvent.click(screen.getByRole("button", { name: "Go back to my order" }));
    expect(calls("khipu_route_priority")).toEqual([]);
    const group = await screen.findByRole("group", { name: "Go back to my order?" });
    fireEvent.click(within(group).getByRole("button", { name: "Yes, go back to my order" }));
    await waitFor(() => expect(calls("khipu_route_priority")).toEqual([{ action: "undo", expect: "p3" }]));
  });

  it("edits a rule proposal's words, then makes them the owner's in two steps", async () => {
    await page();
    fireEvent.click(screen.getByRole("button", { name: "Edit them first" }));
    const form = screen.getByRole("form", { name: "Edit: A conversation reworded your rule 1" });
    fireEvent.change(within(form).getByLabelText("Words"), { target: { value: "Use at most 70% of the xAI week." } });
    fireEvent.click(within(form).getByRole("button", { name: "Save…" }));
    expect(calls("khipu_route_rule")).toEqual([]);
    fireEvent.click(within(form).getByRole("button", { name: "Make them mine" }));
    await waitFor(() => expect(calls("khipu_route_rule")).toEqual([{ action: "edit", n: 1, text: "Use at most 70% of the xAI week." }]));
  });

  it("a proposal whose words stay on another Mac can be taken back, never kept here", async () => {
    answers.khipu_route_view = view({
      rules: [{ n: 1, entity: "rule-a", text: "Use at most 80% of the xAI week.", pending: true, pending_kind: "changed",
        pending_by: "a Claude Code conversation in project-a", prior_text: "Use at most 80% of the xAI week.", proposal_withheld: true }],
    });
    await page();
    const card = screen.getByRole("article", { name: "A conversation on another Mac changed a rule" });
    expect(within(card).queryByRole("button", { name: "Keep the new words" })).toBeNull();
    expect(card).toHaveTextContent(/Its words stay on the Mac it came from/);
    expect(screen.getByText(/Share proposal words between your Macs” is off/)).toBeInTheDocument();
    fireEvent.click(within(card).getByRole("button", { name: "Keep my words" }));
    expect(calls("khipu_route_rule")).toEqual([]);
    fireEvent.click(within(card).getByRole("button", { name: "Yes, keep my words" }));
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
    fireEvent.click(screen.getByRole("button", { name: "Keep the new order" }));
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
    expect(document.body.textContent).not.toContain("It says you asked");
    expect(screen.queryByRole("button", { name: "Keep the new order" })).toBeNull();
    const prio = screen.getByRole("article", { name: "A conversation on another Mac changed your order of accounts" });
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
    const list = await proposalHistory();
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
    const list = await proposalHistory();
    const row = within(list).getByText("Rule 4").closest(".mrow")! as HTMLElement;
    expect(row).toHaveTextContent(/isn't in this read/);
    expect(within(row).queryByRole("button")).toBeNull();
    const card = screen.getByRole("article", { name: "A conversation proposed a change to a rule" });
    expect(card).toHaveTextContent(/isn't in this read/);
    expect(within(card).queryByRole("button")).toBeNull();
    expect(calls("khipu_route_priority")).toEqual([]);
    expect(calls("khipu_route_rule")).toEqual([]);
  });

  it("gives each thing to decide one card in Needs you", async () => {
    await page();
    const needs = screen.getByRole("region", { name: "Needs you" });
    const titles = within(needs).getAllByRole("article").map((a) => a.getAttribute("aria-label"));
    expect(titles).toEqual(["A conversation changed your order of accounts", "A conversation reworded your rule 1"]);
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
    expect(screen.getByText(/Routing copy: checking…/)).toBeInTheDocument();
    fireEvent.click(sw);
    await waitFor(() => expect(calls("khipu_config_set")).toContainEqual({ key: "routing_replication_live", value: "on" }));
  });

  it("while a conversation's request waits, the toggle shows the live state and the row says it waits", async () => {
    answers.khipu_config_show = {
      routing: {
        ...config().routing,
        routing_replication_live: {
          ...setting(false), pending: true,
          request: { request: "turn replication on", by_words: "a conversation", at: "2026-10-14T15:00:00Z" },
        },
      },
    };
    await page();
    expect(screen.getByRole("switch", { name: /Copy routing between your Macs through the hub/ })).not.toBeChecked();
    expect(screen.getByText(/Routing copy: waiting for your answer on Needs you/)).toBeInTheDocument();
    expect(screen.queryByText(/Routing copy: off/)).not.toBeInTheDocument();
  });

  it("shows Retry cleanup only when the copy cleanup gave up, and runs the cleanup command", async () => {
    const { unmount } = render(<RoutingSettings active deliveryHealth={{ status: "green", gateway: { status: "not_applicable" } }} />);
    await screen.findByRole("region", { name: "Needs you" });
    expect(screen.queryByRole("button", { name: /Retry cleanup/ })).toBeNull();
    unmount();
    render(<RoutingSettings active deliveryHealth={{ status: "red", gateway: {
      status: "red", cleanup_gave_up: true,
      reason_words: "Routing copy cleanup stopped after 6 failed tries; a copy may still be on the hub. Retry it from Settings.",
    } }} />);
    await screen.findByRole("region", { name: "Needs you" });
    expect(screen.getByText(/stopped after 6 failed tries/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Retry cleanup/ }));
    await waitFor(() => expect(invokeMock.mock.calls.filter((c) => c[0] === "khipu_gateway_copy_cleanup")).toHaveLength(1));
  });

  it("shows Retry cleanup when an interrupted copy cleanup was never confirmed", async () => {
    render(<RoutingSettings active deliveryHealth={{ status: "red", gateway: {
      status: "red", cleanup_interrupted: true,
      reason_words: "Routing copy cleanup was interrupted; a copy may still be on the hub. Retry it from Settings.",
    } }} />);
    await screen.findByRole("region", { name: "Needs you" });
    expect(screen.getByText(/cleanup was interrupted/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Retry cleanup/ }));
    await waitFor(() => expect(invokeMock.mock.calls.filter((c) => c[0] === "khipu_gateway_copy_cleanup")).toHaveLength(1));
  });

  it("shows Retry cleanup when too many copy cleanups are unresolved", async () => {
    render(<RoutingSettings active deliveryHealth={{ status: "red", gateway: {
      status: "red", cleanup_full: true,
      reason_words: "Too many unresolved routing copy cleanups; copies may still be on the hub. Retry cleanup from Settings.",
    } }} />);
    await screen.findByRole("region", { name: "Needs you" });
    expect(screen.getByText(/Too many unresolved/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Retry cleanup/ }));
    await waitFor(() => expect(invokeMock.mock.calls.filter((c) => c[0] === "khipu_gateway_copy_cleanup")).toHaveLength(1));
  });

  it("words a waiting request ahead of every other replication state", () => {
    expect(replicationStatusWords({ status: "not_applicable" }, null, true)).toBe("waiting for your answer on Needs you");
    expect(replicationStatusWords({ status: "not_applicable" }, null, false)).toBe("off");
  });

  it("words replication status as caught up, stale or never synced", () => {
    expect(replicationStatusWords({ status: "green", caught_up_at: "2026-10-14T15:00:00Z", stale: false })).toBe("caught up");
    expect(replicationStatusWords({ status: "red", caught_up_at: "2026-10-14T15:00:00Z", stale: true })).toBe("stale");
    expect(replicationStatusWords({ status: "unknown", age_seconds: null, caught_up_at: null })).toBe("never synced");
  });

  it("words replication status as off when not applicable and checking while loading", () => {
    expect(replicationStatusWords({ status: "not_applicable" })).toBe("off");
    expect(replicationStatusWords(undefined)).toBe("checking…");
    expect(replicationStatusWords(null)).toBe("checking…");
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

describe("Needs you: what a card says", () => {
  const now = new Date("2026-11-03T15:00:00Z");
  const request = { by_words: "a Codex conversation", at: "2026-11-03T14:30:00Z" };
  const card = (key: string, s: Record<string, unknown>, routeView: RouteView | null = null) =>
    decideCards(routeView, { [key]: { default: null, source: "file", ...s } as RouteSetting }, now)[0];

  it("has words for every setting the CLI knows, and a unit for every count", () => {
    const source = readFileSync("../../packages/cli/khipu/routing.py", "utf8");
    const table = source.slice(source.indexOf("SETTINGS: dict[str"), source.indexOf("_UNITS = {"));
    const entries = [...table.matchAll(/"(routing_[a-z_]+)":\s*\("([a-z_]+)",/g)].map((m) => [m[1], m[2]]);
    expect(entries.length).toBeGreaterThan(50);
    expect(entries.map(([key]) => key).filter((key) => !SETTING_WORDS_KEYS.includes(key))).toEqual([]);
    for (const [key, kind] of entries) {
      if (kind !== "count" && kind !== "number") continue;
      // A bare number is what the owner couldn't read: 200 of what?
      expect(settingValueWords(key, kind, 7), key).not.toMatch(/^[\d,.]+$/);
    }
  });

  it("gives a setting's old and new value in words a person can read, never its stored form", () => {
    const zero = card("routing_line_chars", { kind: "count", value: 0, default: 200, words: "Longest line, rules and note aside (0: no limit)", request: { ...request, prior: null } });
    expect(zero.title).toBe("A conversation changed how long one line of routing news can be, not counting your rules and note");
    expect(zero.before?.text).toBe("200 characters");
    expect(zero.now?.text).toBe("No limit");
    expect(zero.back).toBe("Go back to 200 characters");
    const time = card("routing_stale_after", { kind: "duration", value: 3600, words: "Treat a report as out of date after (0: never)", request: { ...request, prior: 43200 } });
    expect(time.before?.text).toBe("12 hours");
    expect(time.now?.text).toBe("1 hour");
    expect(settingValueWords("routing_runner_tail_bytes", "count", 524288)).toMatch(/KB|MB/);
    expect(settingValueWords("routing_busy_load", "number", 75)).toBe("A load of 75");
    expect(settingValueWords("routing_slow_read_pct", "number", 1)).toBe("1% of prompts");
    expect(settingValueWords("routing_mac_names", "mac_names", { "abc123": "MacBook Air" })).toBe("MacBook Air");
    const unknown = card("routing_new_thing", { kind: "count", value: 4, request });
    expect(unknown.title).toBe("A conversation changed a routing setting");
  });

  it("names the job slot and account a lane map change touches, by label, and only those", () => {
    const routeView = { accounts: [{ key: "claude|", label: "Claude" }, { key: "codex|", label: "Codex" }] } as unknown as RouteView;
    const lanes = card("routing_lane_map", { kind: "lane_map", default: {}, words: "Which account each job lane uses",
      value: { "Project B": { "opus-batch": "claude|", same: "codex|" }, "Project C": { gone: "old|~/x" } },
      request: { ...request, prior: { "Project B": { same: "codex|" } } } }, routeView);
    expect(lanes.title).toBe("A conversation changed which account a job slot uses");
    expect(lanes.before?.text).toBe("opus-batch in Project B → no account\ngone in Project C → no account");
    expect(lanes.now?.text).toBe("opus-batch in Project B → Claude\ngone in Project C → an account Khipu no longer tracks");
    expect([lanes.keep, lanes.back]).toEqual(["Keep the new match", "Go back to the old match"]);
  });

  it("names a switch's result on its buttons", () => {
    const off = card("routing_push_prompt", { kind: "switch", value: false, default: true, words: "Before each prompt", request: { ...request, prior: true } });
    expect(off.title).toBe("A conversation turned off routing news before each prompt");
    expect([off.keep, off.back]).toEqual(["Keep it off", "Turn it back on"]);
    expect(off.ifIgnored).toBe("It stays off, and this stays here until you answer.");
  });

  it("says replication isn't in use until he answers", () => {
    const on = card("routing_replication_live", { kind: "switch", value: false, default: false, request: { ...request, prior: null } });
    expect(on.title).toBe("A conversation asked to turn on copying routing between your Macs");
    expect(on.now?.text).toBe("Off");
    expect([on.keep, on.back]).toEqual(["Turn it on", "Leave it off"]);
    const off = card("routing_replication_live", { kind: "switch", value: false, default: false, request: { ...request, prior: true } });
    expect([off.keep, off.back]).toEqual(["Keep it off", "Turn it back on"]);
  });

  it("says which way a login switch goes from what was asked, never by guessing from the value", () => {
    const login = (value: boolean, asked: Record<string, unknown>) =>
      card("routing_usage_login_cursor", { kind: "switch", value, default: false, request: { ...request, ...asked } });
    const on = login(false, { asked: true, asked_set: true, prior: null });
    expect(on.title).toBe("A conversation asked to turn on reading Cursor's usage from its login in the Keychain");
    expect([on.keep, on.back]).toEqual(["Turn it on", "Leave it off"]);
    expect(on.ifIgnored).toBe("It stays off, and this stays here until you answer.");
    const off = login(true, { asked: false, asked_set: true, prior: true });
    expect([off.keep, off.back]).toEqual(["Turn it off", "Leave it on"]);
    // The refusal of a request that changes nothing covers only replication.
    const same = login(true, { asked: true, asked_set: true, prior: true });
    expect(same.title).toMatch(/, which is already on$/);
    expect([same.keep, same.back]).toEqual(["Keep it on", "Drop the request"]);
    expect(login(true, { asked: null, asked_set: false, prior: true }).keep).toBe("Use the default");
    const older = login(false, { prior: null });
    expect(older.title).toBe("A conversation asked to change reading Cursor's usage from its login in the Keychain");
    expect(older.changed?.text).toMatch(/can't tell whether it turns it on or off/);
    expect([older.keep, older.back]).toEqual(["Accept the change", "Leave it as it is"]);
  });

  it("says what a new order changed in a line: moved, added, removed, reworded", () => {
    const e = (account: string, role?: string) => ({ account, label: account, role });
    const p = (entries: ReturnType<typeof e>[], old: ReturnType<typeof e>[]) =>
      ({ entries, pending: true, replaced: { entries: old } }) as unknown as RoutePriority;
    expect(priorityChanges(p([e("B"), e("A"), e("C")], [e("A"), e("B"), e("C")]))).toBe("A moved to 2nd");
    expect(priorityChanges(p([e("C"), e("A"), e("B")], [e("A"), e("B"), e("C")]))).toBe("C moved to 1st");
    expect(priorityChanges(p([e("A"), e("D")], [e("A"), e("B")]))).toBe("D added as 2nd; B removed");
    expect(priorityChanges(p([e("A", "new")], [e("A", "old")]))).toBe("A's role reworded");
    expect(priorityChanges(p([e("A")], [e("A")]))).toBeNull();
    expect(priorityChanges({ entries: [e("A")], pending: true } as unknown as RoutePriority)).toBeNull();
  });

  it("asks a second time before dropping a conversation's words, not before a setting that can be set again", () => {
    const rule = { n: 1, text: "New.", pending: true, pending_kind: "added" } as RouteRule;
    const [added] = decideCards({ rules: [rule] } as unknown as RouteView, null, now);
    expect(added.back).toBe("Remove it");
    expect(added.backAsk).toMatch(/^Remove this rule\?/);
    expect(card("routing_push_prompt", { kind: "switch", value: false, request: { ...request, prior: true } }).backAsk).toBeNull();
  });

  it("says who in words, with today's time and a project when there is one", () => {
    const named = card("routing_push_prompt", { kind: "switch", value: false, words: "X", request: { ...request, by: { kind: "conversation", harness: "claude_code", project: "project-a" }, prior: true } });
    expect(named.who).toMatch(/^A Claude Code conversation in project-a, 30 min ago$/);
    expect(proposalAuthor({ kind: "conversation", harness: "aegis" })).toBe("an Aegis conversation");
    expect(proposalAuthor({ kind: "conversation", harness: "shell" })).toBe("a shell command");
    expect(proposalAuthor({ kind: "conversation", harness: "made_up" })).toBe("a conversation");
  });
});
