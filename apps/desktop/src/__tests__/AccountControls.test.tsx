// The controls inside the Routing and Plan details (round 2, session A): caps,
// holds, the words and the place in the priority, Undo after a change, the
// asks-first states, how Khipu reads a plan, and Keep or Undo for a
// conversation's change to the accounts. Every value is made up; times are
// New York local.
(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { PlanDetailSheet, RoutingDetailSheet } from "../AccountDetail";
import { AccountsRequestBand } from "../AccountControls";
import type { RouteAccount, RouteView } from "../routing";
import type { UsageAccount } from "../PlansCard";
import type { Support } from "../accountsApi";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

// Mon 2026-03-09, 3:00 PM in New York.
const NOW = new Date("2026-03-09T19:00:00Z");

let answers: Record<string, unknown | ((args: Record<string, unknown>) => unknown)>;
/** The owner-action id each write answers with, as the CLI returns it; null
 *  makes a write answer without one. */
let nextId: string | null;
const OWNER_WRITES = ["khipu_route_cap", "khipu_route_pause", "khipu_route_lift", "khipu_route_clear", "khipu_route_priority", "khipu_route_hold_add", "khipu_route_hold_end"];
let issued: string[];

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  answers = {};
  issued = [];
  nextId = "a1b2c3d4";
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    const a = answers[cmd];
    const out = typeof a === "function" ? await (a as (x: Record<string, unknown>) => unknown)(args ?? {}) : a ?? { ok: true };
    const refused = out && typeof out === "object" && (out as { ok?: boolean }).ok === false;
    if (!refused && OWNER_WRITES.includes(cmd) && nextId) {
      issued.push(nextId);
      return JSON.stringify({ ...(out as object), owner_action: nextId });
    }
    return typeof out === "string" ? out : JSON.stringify(out);
  });
});

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
  vi.useRealTimers();
});

function calls(cmd: string) {
  return invokeMock.mock.calls.filter((c) => c[0] === cmd).map((c) => {
    const { op: _op, ...args } = (c[1] ?? {}) as Record<string, unknown>;
    return args;
  });
}

const codex: RouteAccount = {
  key: "codex|",
  label: "Codex",
  harness: "codex",
  state: "open",
  cap_statuses: [{ kind: "share", window: "weekly", pct: 95, used_pct: 40, confirmed_at: "2026-03-01T15:00:00Z" }],
};
const claude: RouteAccount = { key: "claude|", label: "Claude", harness: "claude", state: "open" };

function view(extra: Partial<RouteView> = {}): RouteView {
  return {
    record: true,
    accounts: [codex, claude],
    priority: {
      entries: [
        { account: "codex|", label: "Codex", role: "builds first", until: null },
        { account: "claude|", label: "Claude", role: "design and judgement", until: null },
      ],
      version: "p1",
    },
    priority_version: "p1",
    holds: [],
    ...extra,
  } as RouteView;
}

function sheet(v: RouteView = view(), a: RouteAccount = codex) {
  const onChanged = vi.fn();
  render(<RoutingDetailSheet account={a} view={v} now={NOW} onClose={() => {}} onChanged={onChanged} />);
  return { dialog: screen.getByRole("dialog"), onChanged };
}

const section = (dialog: HTMLElement, name: string) => within(within(dialog).getByRole("region", { name }));

describe("caps", () => {
  it("adds an even-pace check cap, applying at once and offering Undo that undoes exactly that line", async () => {
    const { dialog, onChanged } = sheet();
    const caps = section(dialog, "Your caps");
    fireEvent.click(caps.getByRole("button", { name: "Add a cap…" }));
    fireEvent.change(caps.getByLabelText("What kind of cap"), { target: { value: "pace_target" } });
    fireEvent.change(caps.getByLabelText("Points ahead of an even pace allowed"), { target: { value: "5" } });
    fireEvent.change(caps.getByLabelText("Applies to"), { target: { value: "check" } });
    fireEvent.click(caps.getByRole("button", { name: "Save cap" }));
    await waitFor(() => expect(calls("khipu_route_cap")).toEqual([
      { action: "set", account: "codex|", cap: { kind: "pace_target", window: "weekly", pct: 100, slack: 5, purpose: "check" } },
    ]));
    const undo = await caps.findByRole("button", { name: "Undo" });
    expect(caps.getByRole("status")).toHaveTextContent("Cap saved on Codex: Checks: up to 5 points over an even pace.");
    expect(undo).toHaveFocus();
    expect(onChanged).toHaveBeenCalled();
    fireEvent.click(undo);
    await waitFor(() => expect(calls("khipu_owner_direction_undo")).toEqual([{ id: "a1b2c3d4" }]));
    // The log is never searched for a line to undo.
    expect(calls("khipu_owner_direction_actions")).toEqual([]);
    expect(await caps.findByText("Undone.")).toBeInTheDocument();
    expect(caps.queryByRole("button", { name: "Undo" })).toBeNull();
  });

  it("meets the rule before the CLI does: a percent over 100 is refused in words without a call", async () => {
    const { dialog } = sheet();
    const caps = section(dialog, "Your caps");
    fireEvent.click(caps.getByRole("button", { name: "Add a cap…" }));
    fireEvent.change(caps.getByLabelText("Percent"), { target: { value: "150" } });
    fireEvent.click(caps.getByRole("button", { name: "Save cap" }));
    expect(await caps.findByRole("alert")).toHaveTextContent("A percent from 1 to 100.");
    expect(calls("khipu_route_cap")).toEqual([]);
  });

  it("changes a cap from its own row, and removes it with an Undo bar", async () => {
    const { dialog } = sheet();
    const caps = section(dialog, "Your caps");
    fireEvent.click(caps.getByRole("button", { name: "Change the cap: At most 95% of the week" }));
    const pct = caps.getByLabelText("Percent");
    expect(pct).toHaveValue(95);
    // The kind is fixed while changing: a different kind is a different cap.
    expect(caps.getByLabelText("What kind of cap")).toBeDisabled();
    fireEvent.change(pct, { target: { value: "90" } });
    fireEvent.click(caps.getByRole("button", { name: "Save cap" }));
    await waitFor(() => expect(calls("khipu_route_cap")[0]).toEqual({ action: "set", account: "codex|", cap: { kind: "share", window: "weekly", pct: 90 } }));
    fireEvent.click(await caps.findByRole("button", { name: "Change the cap: At most 95% of the week" }));
    fireEvent.click(caps.getByRole("button", { name: "Remove this cap" }));
    await waitFor(() => expect(calls("khipu_route_cap")[1]).toEqual({ action: "remove", account: "codex|", cap: { kind: "share", window: "weekly" } }));
    expect(await caps.findByRole("status")).toHaveTextContent("Cap removed: At most 95% of the week.");
    expect(caps.getByRole("button", { name: "Undo" })).toBeInTheDocument();
  });

  it("shows no Undo when the write's answer names no owner action, and says where to take it back", async () => {
    nextId = null;
    const { dialog } = sheet();
    const caps = section(dialog, "Your caps");
    fireEvent.click(caps.getByRole("button", { name: "Add a cap…" }));
    fireEvent.change(caps.getByLabelText("What kind of cap"), { target: { value: "concurrency" } });
    fireEvent.change(caps.getByLabelText("Jobs at once"), { target: { value: "2" } });
    fireEvent.click(caps.getByRole("button", { name: "Save cap" }));
    expect(await caps.findByRole("status")).toHaveTextContent("To take it back, use Settings → Owner actions.");
    expect(caps.queryByRole("button", { name: "Undo" })).toBeNull();
  });

  it("explains a refused Undo instead of failing quietly", async () => {
    answers.khipu_owner_direction_undo = { ok: false, error: "a newer change touched the same cap" };
    const { dialog } = sheet();
    const caps = section(dialog, "Your caps");
    fireEvent.click(caps.getByRole("button", { name: "Add a cap…" }));
    fireEvent.change(caps.getByLabelText("Percent"), { target: { value: "80" } });
    fireEvent.click(caps.getByRole("button", { name: "Save cap" }));
    fireEvent.click(await caps.findByRole("button", { name: "Undo" }));
    expect(await caps.findByRole("alert")).toHaveTextContent("Can't undo: a newer change touched the same cap");
  });
});

describe("state: pause, lift", () => {
  it("pauses with an end and offers Undo", async () => {
    const { dialog } = sheet();
    const state = section(dialog, "State and how it was read");
    fireEvent.click(state.getByRole("button", { name: "Pause Codex until…" }));
    fireEvent.change(state.getByLabelText("Pause Codex until"), { target: { value: "tomorrow" } });
    fireEvent.click(state.getByRole("button", { name: "Pause Codex" }));
    await waitFor(() => expect(calls("khipu_route_pause")).toEqual([{ account: "codex|", until: "2026-03-10T09:00:00-04:00" }]));
    expect(await state.findByRole("status")).toHaveTextContent("Codex is paused until tomorrow 9:00 AM.");
    expect(state.getByRole("button", { name: "Undo" })).toBeInTheDocument();
  });
});

describe("holds on this account", () => {
  const hold = { id: "h1", scope: { kind: "account", account: "codex|" }, scope_label: "Codex", reason: "keep", text: "Keep it for the review", owner: true, until: "2026-03-10T13:00:00Z" };

  it("adds a hold on this account only, with an end", async () => {
    const { dialog } = sheet();
    const holds = section(dialog, "Exceptions in force");
    fireEvent.click(holds.getByRole("button", { name: "Hold Codex…" }));
    fireEvent.change(holds.getByLabelText("What conversations should know"), { target: { value: "Reserve it for tests" } });
    fireEvent.click(holds.getByRole("button", { name: "Add hold" }));
    await waitFor(() => expect(calls("khipu_route_hold_add")).toHaveLength(1));
    expect(calls("khipu_route_hold_add")[0]).toMatchObject({ text: "Reserve it for tests", reason: "other", scope: "account", account: "codex|" });
    expect(await holds.findByRole("status")).toHaveTextContent("Hold added on Codex.");
  });

  it("asks before ending a hold, then ends it with Undo", async () => {
    const { dialog } = sheet(view({ holds: [hold] } as Partial<RouteView>));
    const holds = section(dialog, "Exceptions in force");
    expect(holds.getByText("Hold: Keep it for the review")).toBeInTheDocument();
    fireEvent.click(holds.getByRole("button", { name: /^End the hold on Codex/ }));
    expect(calls("khipu_route_hold_end")).toEqual([]);
    fireEvent.click(within(holds.getByRole("group", { name: "End the hold on Codex?" })).getByRole("button", { name: "End the hold" }));
    await waitFor(() => expect(calls("khipu_route_hold_end")).toEqual([{ id: "h1" }]));
    expect(await holds.findByRole("status")).toHaveTextContent("Hold ended on Codex.");
  });

  it("leaves other accounts' holds to their own detail", () => {
    const other = { ...hold, id: "h2", scope: { kind: "account", account: "claude|" } };
    const { dialog } = sheet(view({ holds: [other] } as Partial<RouteView>));
    expect(within(dialog).queryByText(/Keep it for the review/)).toBeNull();
  });
});

describe("your words and your place", () => {
  it("changes the words with a live count and sends the whole list with the version it read", async () => {
    const { dialog } = sheet();
    const words = section(dialog, "Your words for this account");
    fireEvent.click(words.getByRole("button", { name: "Change…" }));
    const box = words.getByLabelText("Your words for Codex");
    expect(box).toHaveFocus();
    fireEvent.change(box, { target: { value: "builds, tests and reviews" } });
    expect(words.getByText("25 characters")).toBeInTheDocument();
    fireEvent.click(words.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls("khipu_route_priority")).toEqual([{
      action: "set",
      entries: [
        { account: "codex|", role: "builds, tests and reviews", until: null },
        { account: "claude|", role: "design and judgement", until: null },
      ],
      until: null,
      expect: "p1",
    }]));
    expect(await words.findByRole("status")).toHaveTextContent("Your words for Codex are saved.");
  });

  it("moves down in the priority at once, and the first account can't move up", async () => {
    const { dialog } = sheet();
    const words = section(dialog, "Your words for this account");
    fireEvent.click(words.getByRole("button", { name: "Change…" }));
    expect(words.getByRole("button", { name: "Move Codex up" })).toBeDisabled();
    fireEvent.click(words.getByRole("button", { name: "Move Codex down" }));
    await waitFor(() => expect(calls("khipu_route_priority")[0]).toMatchObject({
      action: "set",
      entries: [{ account: "claude|" }, { account: "codex|" }],
    }));
    expect(await words.findByRole("status")).toHaveTextContent("Codex moved down in your priority.");
  });

  it("adds an account that isn't in the priority at the end", async () => {
    const v = view({ priority: { entries: [{ account: "claude|", label: "Claude", role: null, until: null }], version: "p2" } } as Partial<RouteView>);
    const { dialog } = sheet(v);
    fireEvent.click(within(dialog).getByRole("button", { name: "Add to your priority" }));
    await waitFor(() => expect(calls("khipu_route_priority")).toEqual([{
      action: "set",
      entries: [{ account: "claude|", role: null, until: null }, { account: "codex|", role: null, until: null }],
      until: null,
      expect: "p2",
    }]));
  });

  it("removes one of several accounts at once, with Undo", async () => {
    const { dialog } = sheet();
    const words = section(dialog, "Your words for this account");
    fireEvent.click(words.getByRole("button", { name: "Change…" }));
    fireEvent.click(words.getByRole("button", { name: "Remove Codex from your priority" }));
    await waitFor(() => expect(calls("khipu_route_priority")[0]).toMatchObject({ action: "set", entries: [{ account: "claude|" }] }));
    expect(await words.findByRole("status")).toHaveTextContent("Codex is out of your priority.");
  });

  it("asks first before clearing a priority that has only this account", async () => {
    const v = view({ priority: { entries: [{ account: "codex|", label: "Codex", role: "all of it", until: null }], version: "p3" } } as Partial<RouteView>);
    const { dialog } = sheet(v);
    const words = section(dialog, "Your words for this account");
    fireEvent.click(words.getByRole("button", { name: "Change…" }));
    fireEvent.click(words.getByRole("button", { name: "Remove Codex from your priority" }));
    const ask = words.getByRole("group", { name: "Clear your priority?" });
    expect(calls("khipu_route_priority")).toEqual([]);
    expect(within(ask).getByRole("button", { name: "Keep it" })).toBeInTheDocument();
    fireEvent.click(within(ask).getByRole("button", { name: "Clear priority" }));
    await waitFor(() => expect(calls("khipu_route_priority")).toEqual([{ action: "clear", expect: "p3" }]));
  });

  it("asks which holds or pauses end when the CLI names them, and sends the choice", async () => {
    let first = true;
    answers.khipu_route_priority = () => {
      if (first) {
        first = false;
        return { ok: false, error: "say end or keep in holds for: h9" };
      }
      return { ok: true };
    };
    const held = { id: "h9", scope: { kind: "account", account: "codex|" }, scope_label: "Codex", reason: "keep", text: "Reserved", owner: true, until: null };
    const { dialog } = sheet(view({ holds: [held] } as Partial<RouteView>));
    const words = section(dialog, "Your words for this account");
    fireEvent.click(words.getByRole("button", { name: "Change…" }));
    fireEvent.click(words.getByRole("button", { name: "Move Codex down" }));
    const choice = await words.findByRole("group", { name: "End or keep each hold" });
    expect(choice).toHaveTextContent("That change wasn't saved yet.");
    fireEvent.click(within(choice).getAllByRole("radio")[0]);
    fireEvent.click(within(choice).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls("khipu_route_priority")).toHaveLength(2));
    expect(calls("khipu_route_priority")[1]).toMatchObject({ action: "set", holds: { h9: "end" } });
    // The retry that went through still leaves an Undo bar.
    expect(await words.findByRole("status")).toHaveTextContent("Codex moved down in your priority.");
  });

  it("holds the controls back while a conversation's priority change waits for an answer", () => {
    const v = view();
    v.priority = { ...v.priority!, pending: true };
    const { dialog } = sheet(v);
    expect(within(dialog).getByText(/A conversation changed your priority and it waits for you/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Change…" })).toBeNull();
  });
});

describe("the More menu and See the plan", () => {
  it("offers Rename and Stop tracking only where a host gives them, and closes first", () => {
    const rename = vi.fn();
    const stop = vi.fn();
    const onClose = vi.fn();
    render(<RoutingDetailSheet account={codex} view={view()} now={NOW} onClose={onClose} onRename={rename} onStop={stop} />);
    expect(screen.queryByRole("button", { name: "Rename…" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "More" }));
    fireEvent.click(screen.getByRole("button", { name: "Stop tracking…" }));
    expect(onClose).toHaveBeenCalled();
    expect(stop).toHaveBeenCalled();
    expect(rename).not.toHaveBeenCalled();
  });

  it("shows no More menu on Home, where no host gives Rename or Stop", () => {
    sheet();
    expect(screen.queryByRole("button", { name: "More" })).toBeNull();
  });
});

describe("how Khipu reads a plan (Plan detail)", () => {
  const kimiRow = { harness: "kimi", key: "kimi:main", account: "Kimi", plan: null, windows: [], ok: false, error: "not_configured", home: null } as unknown as UsageAccount;
  const kimi: RouteAccount = { key: "kimi:main", label: "Kimi", harness: "kimi", state: "unknown", configured: true, usage_setup: "no usage reader set up; confirm its quota host in Settings → Accounts" };
  const kimiView = () => view({
    accounts: [kimi],
    definitions: { providers: { kimi: { id: "kimi", label: "Kimi", family: "Moonshot", login: "none", probe: "none", models_allowed: ["k3.1-pro"] } } },
  } as Partial<RouteView>);

  it("sets up reading: asks before sending the key to the address, then saves the provider with the reader", async () => {
    const onChanged = vi.fn();
    render(<PlanDetailSheet row={kimiRow} view={kimiView()} now={NOW} onClose={() => {}} onChanged={onChanged} />);
    fireEvent.click(screen.getByRole("button", { name: "Set up reading…" }));
    fireEvent.change(screen.getByLabelText("Where Khipu asks for usage"), { target: { value: "api.example.com/usage" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Just the address");
    fireEvent.change(screen.getByLabelText("Where Khipu asks for usage"), { target: { value: "https://api.example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    const ask = screen.getByRole("group", { name: "Send your key to this address?" });
    expect(ask).toHaveTextContent("Send your Kimi key to api.example.com?");
    expect(calls("khipu_route_definition")).toEqual([]);
    fireEvent.click(within(ask).getByRole("button", { name: "Use this address" }));
    await waitFor(() => expect(calls("khipu_route_definition")).toEqual([{
      definition: "provider",
      value: { id: "kimi", label: "Kimi", family: "Moonshot", login: "none", probe: "http-quota", models_allowed: ["k3.1-pro"], host: "api.example.com" },
    }]));
    expect(await screen.findByText("Saved. Khipu will read Kimi's plan at api.example.com on its next check.")).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalled();
  });

  it("offers nothing to set up for a plan that already has its reader", () => {
    const set = view({ accounts: [{ ...kimi, usage_setup: undefined }], definitions: { providers: { kimi: { id: "kimi", label: "Kimi", family: "Moonshot", probe: "http-quota", host: "api.example.com" } } } } as Partial<RouteView>);
    render(<PlanDetailSheet row={kimiRow} view={set} now={NOW} onClose={() => {}} />);
    expect(screen.queryByRole("button", { name: "Set up reading…" })).toBeNull();
  });

  it("replaces a GLM key through the Keychain command, asks first, and never keeps the value", async () => {
    const glmRow = { harness: "glm", key: "glm|", account: "GLM", plan: null, windows: [], ok: false, error: "key_rejected", home: null } as unknown as UsageAccount;
    const onRefresh = vi.fn();
    render(<PlanDetailSheet row={glmRow} view={view({ accounts: [{ key: "glm|", label: "GLM", harness: "glm", state: "unknown" }] } as Partial<RouteView>)} now={NOW} onClose={() => {}} onRefresh={onRefresh} />);
    fireEvent.click(screen.getByRole("button", { name: "Replace the key…" }));
    const field = screen.getByLabelText("New GLM key");
    expect(field).toHaveAttribute("type", "password");
    fireEvent.change(field, { target: { value: "made-up-key-value" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(invokeMock.mock.calls.some((c) => c[0] === "set_khipu_secret")).toBe(false);
    fireEvent.click(within(screen.getByRole("group", { name: "Replace the GLM key?" })).getByRole("button", { name: "Replace the key" }));
    await waitFor(() => expect(invokeMock).toHaveBeenCalledWith("set_khipu_secret", { account: "glm_api_key", value: "made-up-key-value" }));
    expect(await screen.findByText(/The key is replaced/)).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain("made-up-key-value");
    expect(onRefresh).toHaveBeenCalled();
  });

  it("does not offer a key to replace for an account whose key the app can't store yet", () => {
    const row = { harness: "kimi", key: "kimi:main", account: "Kimi", plan: null, windows: [], ok: false, error: "no_key", home: null } as unknown as UsageAccount;
    render(<PlanDetailSheet row={row} view={view({ accounts: [{ ...kimi, usage_setup: undefined }] } as Partial<RouteView>)} now={NOW} onClose={() => {}} />);
    expect(screen.queryByRole("button", { name: "Replace the key…" })).toBeNull();
  });
});

describe("a conversation's change to the accounts", () => {
  const support = (request: unknown): Support => ({
    state: "here",
    status: { ok: true, version: "v7", none: false, auto_track: true, value: null, accounts: [], user_home: null, declined: [], request },
  });

  it("shows nothing when no conversation changed the list", () => {
    const { container } = render(<AccountsRequestBand support={support(null)} reload={async () => {}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("keeps the change against the version the owner saw", async () => {
    const reload = vi.fn(async () => {});
    const changed = vi.fn();
    render(<AccountsRequestBand support={support({ request: "track my Kimi account", by: { label: "a Codex conversation" } })} reload={reload} onChanged={changed} />);
    const band = screen.getByRole("group", { name: "A conversation changed your accounts" });
    expect(band).toHaveTextContent("a Codex conversation changed which accounts Khipu reads at your request");
    expect(band).toHaveTextContent("“track my Kimi account”");
    fireEvent.click(within(band).getByRole("button", { name: "Keep" }));
    await waitFor(() => expect(calls("khipu_accounts_request")).toEqual([{ action: "confirm", expect: "v7" }]));
    await waitFor(() => expect(reload).toHaveBeenCalled());
    expect(changed).toHaveBeenCalled();
  });

  it("undoes the change and shows a refusal in words", async () => {
    answers.khipu_accounts_request = { ok: false, error: "the list changed after you read it" };
    render(<AccountsRequestBand support={support({ request: "stop tracking Muse" })} reload={async () => {}} />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    });
    await waitFor(() => expect(calls("khipu_accounts_request")).toEqual([{ action: "undo", expect: "v7" }]));
    expect(await screen.findByRole("alert")).toHaveTextContent("Khipu didn't make that change");
  });
});

describe("review round 2: identity, stale versions, levels, keys", () => {
  it("undoes exactly the id each write returned, so two holds never cross", async () => {
    const { dialog } = sheet();
    const holds = section(dialog, "Exceptions in force");
    nextId = "11111111";
    fireEvent.click(holds.getByRole("button", { name: "Hold Codex…" }));
    fireEvent.change(holds.getByLabelText("What conversations should know"), { target: { value: "first" } });
    fireEvent.click(holds.getByRole("button", { name: "Add hold" }));
    await holds.findByRole("button", { name: "Undo" });
    // Another hold (any account) lands meanwhile; this bar still belongs to the first.
    nextId = "22222222";
    fireEvent.click(holds.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(calls("khipu_owner_direction_undo")).toEqual([{ id: "11111111" }]));
  });

  it("shows no Undo when the answer's id is not an owner-action id", async () => {
    nextId = "not-an-id";
    const { dialog } = sheet();
    const caps = section(dialog, "Your caps");
    fireEvent.click(caps.getByRole("button", { name: "Add a cap…" }));
    fireEvent.change(caps.getByLabelText("Percent"), { target: { value: "80" } });
    fireEvent.click(caps.getByRole("button", { name: "Save cap" }));
    await caps.findByRole("status");
    expect(caps.queryByRole("button", { name: "Undo" })).toBeNull();
  });

  it("never sends an old draft under a newer version, until the owner chooses to", async () => {
    const onChanged = vi.fn();
    const { rerender } = render(<RoutingDetailSheet account={codex} view={view()} now={NOW} onClose={() => {}} onChanged={onChanged} />);
    const words = section(screen.getByRole("dialog"), "Your words for this account");
    fireEvent.click(words.getByRole("button", { name: "Change…" }));
    fireEvent.change(words.getByLabelText("Your words for Codex"), { target: { value: "my draft" } });
    // A conversation edits the priority meanwhile: the page reads version p2.
    const newer = view();
    newer.priority = { entries: [
      { account: "codex|", label: "Codex", role: "their newer words", until: null },
      { account: "claude|", label: "Claude", role: "design and judgement", until: null },
    ], version: "p2" };
    rerender(<RoutingDetailSheet account={codex} view={newer} now={NOW} onClose={() => {}} onChanged={onChanged} />);
    expect(await words.findByRole("alert")).toHaveTextContent("Your priority changed after you opened this");
    fireEvent.click(words.getByRole("button", { name: "Save" }));
    expect(calls("khipu_route_priority")).toEqual([]);
    expect(words.getByLabelText("Your words for Codex")).toHaveValue("my draft");
    fireEvent.click(words.getByRole("button", { name: "Use my draft on the newer version" }));
    fireEvent.click(words.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls("khipu_route_priority")).toHaveLength(1));
    expect(calls("khipu_route_priority")[0]).toMatchObject({ action: "set", expect: "p2",
      entries: [{ account: "codex|", role: "my draft" }, { account: "claude|", role: "design and judgement" }] });
  });

  it("keeps another account's level when it rewrites the whole list", async () => {
    const level = { claim: "Opus is limited today", as_of: "2026-03-09T12:00:00Z", until: "2026-03-10T12:00:00Z" };
    const v = view();
    v.priority = { ...v.priority!, entries: [
      { account: "codex|", label: "Codex", role: "builds first", until: null },
      { account: "claude|", label: "Claude", role: "design", until: null, level },
    ] };
    const { dialog } = sheet(v);
    const words = section(dialog, "Your words for this account");
    fireEvent.click(words.getByRole("button", { name: "Change…" }));
    fireEvent.click(words.getByRole("button", { name: "Move Codex down" }));
    await waitFor(() => expect(calls("khipu_route_priority")).toHaveLength(1));
    expect(calls("khipu_route_priority")[0].entries).toEqual([
      { account: "claude|", role: "design", until: null, level },
      { account: "codex|", role: "builds first", until: null },
    ]);
  });

  it("empties the key field the moment the value is handed over, while the write is still pending", async () => {
    let finish: (v: string) => void = () => {};
    invokeMock.mockImplementation((cmd: string) =>
      cmd === "set_khipu_secret" ? new Promise<string>((r) => { finish = r; }) : Promise.resolve(JSON.stringify({ ok: true })));
    const glmRow = { harness: "glm", key: "glm|", account: "GLM", plan: null, windows: [], ok: false, error: "key_rejected", home: null } as unknown as UsageAccount;
    render(<PlanDetailSheet row={glmRow} view={view({ accounts: [{ key: "glm|", label: "GLM", harness: "glm", state: "unknown" }] } as Partial<RouteView>)} now={NOW} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Replace the key…" }));
    fireEvent.change(screen.getByLabelText("New GLM key"), { target: { value: "made-up-key-value" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fireEvent.click(within(screen.getByRole("group", { name: "Replace the GLM key?" })).getByRole("button", { name: "Replace the key" }));
    await waitFor(() => expect(invokeMock).toHaveBeenCalledWith("set_khipu_secret", { account: "glm_api_key", value: "made-up-key-value" }));
    expect(screen.getByLabelText("New GLM key")).toHaveValue("");
    finish(JSON.stringify({ ok: true }));
    await screen.findByText(/The key is replaced/);
  });

  it("Keep the old key returns focus to the key field, and Escape closes only the question", () => {
    const glmRow = { harness: "glm", key: "glm|", account: "GLM", plan: null, windows: [], ok: false, error: "no_key", home: null } as unknown as UsageAccount;
    render(<PlanDetailSheet row={glmRow} view={view({ accounts: [{ key: "glm|", label: "GLM", harness: "glm", state: "unknown" }] } as Partial<RouteView>)} now={NOW} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Replace the key…" }));
    const field = screen.getByLabelText("New GLM key");
    fireEvent.change(field, { target: { value: "made-up-key-value" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fireEvent.keyDown(screen.getByRole("group", { name: "Replace the GLM key?" }), { key: "Escape" });
    expect(screen.queryByRole("group", { name: "Replace the GLM key?" })).toBeNull();
    expect(screen.getByLabelText("New GLM key")).toHaveValue("made-up-key-value");
  });
});

describe("review round 3: a changed cap keeps what it did not change", () => {
  it("saves a 65% even-pace cap with its 65% target when only the slack changes", async () => {
    const a: RouteAccount = { ...codex, cap_statuses: [{ kind: "pace_target", purpose: "job", window: "weekly", pct: 65, slack: 5 }] };
    const { dialog } = sheet(view({ accounts: [a, claude] } as Partial<RouteView>), a);
    const caps = section(dialog, "Your caps");
    fireEvent.click(caps.getByRole("button", { name: /^Change the cap:/ }));
    fireEvent.change(caps.getByLabelText("Points ahead of an even pace allowed"), { target: { value: "8" } });
    fireEvent.click(caps.getByRole("button", { name: "Save cap" }));
    await waitFor(() => expect(calls("khipu_route_cap")).toEqual([
      { action: "set", account: "codex|", cap: { kind: "pace_target", window: "weekly", pct: 65, slack: 8, purpose: "job" } },
    ]));
  });
});
