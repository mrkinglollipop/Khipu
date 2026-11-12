// The Accounts place (slice A4): its controls make exactly the old controls' calls,
// only a click changes anything, a control is off while its call is out, a refusal
// says so and the screen shows what is still true, and Undo calls the existing undo
// once. Every value is made up; invoke is a fake.
(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Accounts } from "../accounts/Accounts";
import { useRouting } from "../routing";
import type { RouteAccount, RouteView } from "../routing";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke: (...args: unknown[]) => invokeMock(...args) }));

const NOW = new Date("2026-11-10T19:00:00Z"); // 2:00 PM in New York
const at = (min: number) => new Date(NOW.getTime() + min * 60_000).toISOString();
const calls: Array<[string, Record<string, unknown> | undefined]> = [];
const WRITES = ["khipu_route_pause", "khipu_route_lift", "khipu_route_cap", "khipu_route_override_act", "khipu_owner_direction_undo"];
const writes = () => calls.filter(([c]) => WRITES.includes(c));
const count = (cmd: string) => calls.filter(([c]) => c === cmd).length;

let accounts: RouteAccount[];
let overrides: Array<Record<string, unknown>>;
let candidates: Array<Record<string, unknown>>;
/** Hold an answer until the test lets it go, to see the control while its call is out. */
let hold: { cmd: string; release: () => void } | null;
let refuse: string | null;

const claude = (over: Partial<RouteAccount> = {}): RouteAccount => ({
  key: "claude|", label: "Claude", harness: "claude", state: "open", cap_statuses: [],
  pace: [{ window: "weekly", used_pct: 38, cycle_resets_at: at(3000), plan: { verdict: "on_pace" } }], ...over,
});
const grok = (over: Partial<RouteAccount> = {}): RouteAccount => ({
  key: "grok|", label: "Grok", harness: "grok", state: "open", cap_statuses: [{ kind: "share", window: "weekly", pct: 90 }],
  pace: [{ window: "weekly", used_pct: 81, cycle_resets_at: at(3000), plan: { verdict: "on_pace" } }], ...over,
});

const SUBJECT = { kind: "thread", id: "thr-1", mac: "abc123" };

beforeEach(() => {
  calls.length = 0;
  hold = null;
  refuse = null;
  accounts = [claude(), grok()];
  overrides = [];
  candidates = [{ subject: SUBJECT, title: "Khipu app mocks", mac: "M5", last_active: at(-12) }];
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    calls.push([cmd, args]);
    if (hold && hold.cmd === cmd) await new Promise<void>((r) => { hold!.release = r; });
    const view: RouteView = {
      record: true, checked_at: at(-3), this_mac: "M5", accounts: JSON.parse(JSON.stringify(accounts)),
      priority: { entries: accounts.map((a) => ({ account: a.key, label: a.label })) },
    };
    switch (cmd) {
      case "khipu_route_view": return JSON.stringify(view);
      case "khipu_route_override_view": return JSON.stringify({ ok: true, overrides });
      case "khipu_route_override_candidates": return JSON.stringify({ ok: true, candidates });
      case "khipu_route_pause":
        if (refuse) return JSON.stringify({ ok: false, error: refuse });
        accounts = accounts.map((a) => (a.key === args?.account ? { ...a, state: "paused", until: args?.until as string | null } : a));
        return JSON.stringify({ ok: true, owner_action: "aaaa1111" });
      case "khipu_route_lift":
        accounts = accounts.map((a) => (a.key === args?.account ? { ...a, state: "open", until: null } : a));
        return JSON.stringify({ ok: true, owner_action: "bbbb2222" });
      case "khipu_route_cap": {
        const cap = args?.cap as { kind: string; window: string; pct?: number };
        accounts = accounts.map((a) => (a.key !== args?.account ? a : {
          ...a,
          cap_statuses: args?.action === "set"
            ? [...(a.cap_statuses ?? []).filter((c) => c.window !== cap.window), { kind: "share", window: cap.window, pct: cap.pct }]
            : (a.cap_statuses ?? []).filter((c) => c.window !== cap.window),
        }));
        return JSON.stringify({ ok: true, owner_action: "cccc3333" });
      }
      case "khipu_route_override_act":
        if (args?.verb === "grant") {
          overrides = [{ id: "ov-9", label: "Claude", who: "The T3 conversation “Khipu app mocks”", until: args.until, indefinite: false, review_due: false, cap: "x" }];
          return JSON.stringify({ ok: true, id: "ov-9", until: args.until, replaced: [], owner_action: "dddd4444" });
        }
        overrides = overrides.filter((o) => o.id !== args?.id);
        return JSON.stringify({ ok: true, owner_action: "eeee5555" });
      case "khipu_owner_direction_undo":
        return JSON.stringify({ ok: true });
      default:
        return JSON.stringify({ ok: true });
    }
  });
});
afterEach(() => cleanup());

it("renders the same-account warning for both successful usage rows", async () => {
  accounts = [claude(), claude({ key: "claude|/synthetic/second", label: "Claude second" })];
  const notes = accounts.map((a, i) => ({ key: a.key, ok: true,
    same_account_as: accounts[1 - i].label,
    note: `Reads the same account as ${accounts[1 - i].label}: both folders are signed in to one account. Sign one of them in to the other account.`,
  }));
  function SameAccounts() {
    const routing = useRouting(true);
    return <Accounts routing={routing} plans={{ accounts: notes }} thisMac="M5" now={NOW} />;
  }
  const { container } = render(<SameAccounts />);
  await waitFor(() => expect(screen.getAllByText("On pace").length).toBeGreaterThanOrEqual(2));
  for (const row of notes) expect(screen.getAllByText(row.note).length).toBeGreaterThanOrEqual(1);
  expect(container.querySelectorAll(".ac-pace.warn")).toHaveLength(2);
  expect(container.querySelectorAll(".ac-meter")).toHaveLength(2);
  expect(screen.getByRole("article", { name: "Claude" }).textContent).toContain("62% left");
  expect(within(screen.getByRole("article", { name: "Claude" })).getByText(notes[0].note)).toBeTruthy();
  expect(writes()).toEqual([]);
});

it.each([false, true])("omits the warning from the list and page when a nonempty note is not paired with a mark (%s)", async (marked) => {
  const note = "Reads the same account as Claude second: both folders are signed in to one account.";
  const usage = marked ? { same_account_as: "Claude second", note: "" } : { note };
  function NoWarning() {
    const routing = useRouting(true);
    return <Accounts routing={routing} plans={{ accounts: [{ key: "claude|", ok: true, ...usage }] }} thisMac="M5" now={NOW} />;
  }
  render(<NoWarning />);
  const button = await screen.findByRole("button", { name: /^Claude/ });
  expect(button.querySelector(".ac-pace.warn")).toBeNull();
  expect(button.textContent).not.toMatch(/same account/i);
  fireEvent.click(button);
  const page = await screen.findByRole("article", { name: "Claude" });
  expect(page.textContent).not.toMatch(/same account/i);
  expect(screen.queryByText(note)).toBeNull();
  expect(writes()).toEqual([]);
});

it.each([["limited", "Out of room"], ["paused", "Paused"], ["down", "Down"]])(
  "keeps a duplicate %s account's state and windows", async (state, word) => {
    accounts = [claude({ state, until: at(70) })];
    const note = "Reads the same account as Claude second: both folders are signed in to one account.";
    function SameAccounts() {
      const routing = useRouting(true);
      return <Accounts routing={routing} plans={{ accounts: [{ key: "claude|", ok: true,
        same_account_as: "Claude second", note }] }} thisMac="M5" now={NOW} />;
    }
    render(<SameAccounts />);
    const button = await screen.findByRole("button", { name: /^Claude/ });
    expect(button.textContent).toContain(word);
    expect(within(button).getByText(note)).toBeTruthy();
    fireEvent.click(button);
    const page = await screen.findByRole("article", { name: "Claude" });
    expect(page.textContent).toContain(word);
    if (state !== "down") expect(page.textContent).toContain("62% left");
    expect(within(page).getByText(note)).toBeTruthy();
    expect(writes()).toEqual([]);
  },
);

function Place() {
  const routing = useRouting(true);
  return <Accounts routing={routing} plans={null} thisMac="M5" now={NOW} />;
}

async function open(name = "Claude") {
  render(<Place />);
  const row = await screen.findByRole("button", { name: new RegExp(`^${name}`) });
  fireEvent.click(row);
  await screen.findByRole("article", { name });
  return screen.getByRole("article", { name });
}

describe("reading changes nothing", () => {
  it("opens the account requested by a Today tile without changing routing", async () => {
    function FromToday() {
      const routing = useRouting(true);
      return <Accounts routing={routing} plans={null} thisMac="M5" now={NOW} initialAccountKey="grok|" />;
    }
    const { container } = render(<FromToday />);
    await screen.findByRole("article", { name: "Grok" });
    expect(screen.getByRole("button", { name: /^Grok/ }).getAttribute("aria-current")).toBe("true");
    expect(container.querySelector(".sh-split")?.getAttribute("data-detail")).toBe("open");
    expect(writes()).toEqual([]);
  });
  it("opening the place, selecting accounts and reading the conversation list make no write", async () => {
    await open("Claude");
    fireEvent.click(screen.getByRole("button", { name: /^Grok/ }));
    await screen.findByRole("article", { name: "Grok" });
    await waitFor(() => expect(count("khipu_route_override_candidates")).toBeGreaterThanOrEqual(2));
    expect(writes()).toEqual([]);
  });

  it("shows percent left, the reset and the pace word as Khipu gave them", async () => {
    render(<Place />);
    const row = await screen.findByRole("button", { name: /^Claude/ });
    expect(row.textContent).toContain("62% left of the week");
    expect(row.textContent).toContain("On pace");
    expect(row.textContent).not.toMatch(/runs out|will run out/i);
  });
});

describe("Pause until", () => {
  it("calls khipu_route_pause with the account and a time, once, with the button off while it is out", async () => {
    const page = await open();
    hold = { cmd: "khipu_route_pause", release: () => {} };
    const pause = within(page).getByRole("button", { name: "Pause" });
    fireEvent.click(pause);
    fireEvent.click(pause);
    await waitFor(() => expect(count("khipu_route_pause")).toBe(1));
    await waitFor(() => expect(pause.hasAttribute("disabled")).toBe(true));
    expect(count("khipu_route_pause")).toBe(1);
    await act(async () => hold!.release());
    const [, args] = calls.find(([c]) => c === "khipu_route_pause")!;
    expect(args).toMatchObject({ account: "claude|", until: "2026-11-11T09:00:00-05:00" });
    expect(typeof args?.op).toBe("string");
    expect(await screen.findByText("Claude is paused until tomorrow 9:00 AM.")).toBeTruthy();
  });

  it("Until I clear it pauses with no end, as the old control did", async () => {
    const page = await open();
    fireEvent.change(within(page).getByLabelText("Pause until"), { target: { value: "never" } });
    fireEvent.click(within(page).getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(count("khipu_route_pause")).toBe(1));
    expect(calls.find(([c]) => c === "khipu_route_pause")![1]).toMatchObject({ account: "claude|", until: null });
    expect(await screen.findByText("Claude is paused until you resume it.")).toBeTruthy();
  });

  it("Resume now lifts the pause", async () => {
    accounts = [claude({ state: "paused", until: at(600) }), grok()];
    const page = await open();
    fireEvent.click(within(page).getByRole("button", { name: "Resume now" }));
    await waitFor(() => expect(count("khipu_route_lift")).toBe(1));
    expect(calls.find(([c]) => c === "khipu_route_lift")![1]).toMatchObject({ account: "claude|" });
  });

  it("a refusal says so in words, leaves no receipt, and the screen shows what is still true", async () => {
    refuse = "Khipu can't write the routing record right now.";
    const page = await open();
    const reads = count("khipu_route_view");
    fireEvent.click(within(page).getByRole("button", { name: "Pause" }));
    const alert = await within(page).findByRole("alert");
    expect(alert.textContent).toContain("Khipu didn't make that change");
    expect(alert.textContent).toContain("can't write the routing record");
    expect(screen.queryByText(/is paused/)).toBeNull();
    // It read the record again, and the row still says what is true.
    expect(count("khipu_route_view")).toBeGreaterThan(reads);
    expect(screen.getByRole("button", { name: /^Claude/ }).textContent).toContain("On pace");
    // The control is back on, so the owner can try again.
    expect(within(page).getByRole("button", { name: "Pause" }).hasAttribute("disabled")).toBe(false);
  });
});

describe("a receipt's Undo", () => {
  it("calls the existing undo once with the id the change returned, and does not repeat", async () => {
    const page = await open();
    fireEvent.click(within(page).getByRole("button", { name: "Pause" }));
    const receipt = await screen.findByText("Claude is paused until tomorrow 9:00 AM.");
    const undo = within(receipt.closest("[role=status]") as HTMLElement).getByRole("button", { name: "Undo" });
    hold = { cmd: "khipu_owner_direction_undo", release: () => {} };
    fireEvent.click(undo);
    fireEvent.click(undo);
    await waitFor(() => expect(count("khipu_owner_direction_undo")).toBe(1));
    expect(undo.hasAttribute("disabled")).toBe(true);
    await act(async () => hold!.release());
    expect(await screen.findByText("Undone.")).toBeTruthy();
    expect(calls.filter(([c]) => c === "khipu_owner_direction_undo")).toEqual([["khipu_owner_direction_undo", { id: "aaaa1111" }]]);
    expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
  });
});

describe("Where agents stop", () => {
  it("shows the account's one level, and Save stays off until the choice changes", async () => {
    const page = await open("Grok");
    const select = within(page).getByLabelText("Stop at") as HTMLSelectElement;
    expect(select.value).toBe("90");
    const save = within(page).getByRole("button", { name: "Save" });
    expect(save.hasAttribute("disabled")).toBe(true);
    fireEvent.change(select, { target: { value: "85" } });
    expect(save.hasAttribute("disabled")).toBe(false);
    expect(writes()).toEqual([]);
  });

  it("saves a level as a share limit on the same window, as the old cap form did", async () => {
    const page = await open("Grok");
    fireEvent.change(within(page).getByLabelText("Stop at"), { target: { value: "85" } });
    fireEvent.click(within(page).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(count("khipu_route_cap")).toBe(1));
    expect(calls.find(([c]) => c === "khipu_route_cap")![1]).toMatchObject({
      action: "set", account: "grok|", cap: { kind: "share", window: "weekly", pct: 85 },
    });
    expect(await screen.findByText("Agents now stop at 85% on Grok.")).toBeTruthy();
  });

  it("No limit removes that share limit with the arguments the old form used", async () => {
    const page = await open("Grok");
    fireEvent.change(within(page).getByLabelText("Stop at"), { target: { value: "" } });
    fireEvent.click(within(page).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(count("khipu_route_cap")).toBe(1));
    expect(calls.find(([c]) => c === "khipu_route_cap")![1]).toMatchObject({
      action: "remove", account: "grok|", cap: { kind: "share", window: "weekly" },
    });
  });

  it("limits that are not one level are shown as set by your rules, listed, with no control that would drop them", async () => {
    accounts = [claude({ cap_statuses: [{ kind: "share", window: "five_hour", pct: 60 }, { kind: "share", window: "weekly", pct: 91 }] }), grok()];
    const page = await open("Claude");
    expect(within(page).getByText(/Set by your rules\./)).toBeTruthy();
    expect(within(page).queryByLabelText("Stop at")).toBeNull();
    expect(within(page).getByText("At most 60% of the five hours")).toBeTruthy();
    expect(within(page).getByText("At most 91% of the week")).toBeTruthy();
    expect(screen.getByRole("button", { name: /^Claude/ }).textContent).toContain("Set by your rules");
  });

  it("another kind of limit stays listed beside the level", async () => {
    accounts = [claude({ cap_statuses: [{ kind: "pace_target", purpose: "job", window: "weekly", pct: 100, slack: 5 }] }), grok()];
    const page = await open("Claude");
    expect(within(page).getByLabelText("Stop at")).toBeTruthy();
    expect(within(page).getByText("Also set by your rules:")).toBeTruthy();
    expect(within(page).getByText("Jobs: up to 5 points over an even pace")).toBeTruthy();
  });
});

describe("Let one conversation run ahead", () => {
  it("offers the conversations Khipu listed, and grants with the subject unchanged and always an end time", async () => {
    const page = await open();
    const form = await within(page).findByRole("form", { name: "Let a conversation run ahead on Claude" });
    expect(within(form).getByRole("option", { name: "Khipu app mocks (M5)" })).toBeTruthy();
    // The app never offers "no end".
    const until = within(form).getByLabelText("Until") as HTMLSelectElement;
    expect(Array.from(until.options).map((o) => o.textContent)).not.toContain("Until I clear it");
    expect(calls.find(([c]) => c === "khipu_route_override_candidates")![1]).toEqual({ account: "claude|" });
    fireEvent.click(within(form).getByRole("button", { name: "Let it run ahead" }));
    await waitFor(() => expect(count("khipu_route_override_act")).toBe(1));
    const [, args] = calls.find(([c]) => c === "khipu_route_override_act")!;
    expect(args).toMatchObject({ verb: "grant", account: "claude|", subject: SUBJECT });
    expect(args).not.toHaveProperty("id");
    expect(args).not.toHaveProperty("indefinite");
    expect(args?.until).toMatch(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$/);
    expect(await screen.findByText(/“Khipu app mocks” may run ahead on Claude until/)).toBeTruthy();
    // It now shows under Running ahead.
    expect(await screen.findByRole("heading", { name: "Running ahead" })).toBeTruthy();
  });

  it("the grant button is off while its call is out, and a second click makes no second call", async () => {
    const page = await open();
    const form = await within(page).findByRole("form", { name: "Let a conversation run ahead on Claude" });
    hold = { cmd: "khipu_route_override_act", release: () => {} };
    const go = within(form).getByRole("button", { name: "Let it run ahead" });
    fireEvent.click(go);
    fireEvent.click(go);
    await waitFor(() => expect(count("khipu_route_override_act")).toBe(1));
    await waitFor(() => expect(go.hasAttribute("disabled")).toBe(true));
    await act(async () => hold!.release());
    expect(count("khipu_route_override_act")).toBe(1);
  });

  it("with no conversation to pick it says so and offers no button", async () => {
    candidates = [];
    const page = await open();
    expect(await within(page).findByText(/No conversation on M5 has used Claude in the last day/)).toBeTruthy();
    expect(within(page).queryByRole("button", { name: "Let it run ahead" })).toBeNull();
  });

  it("a custom end with nothing chosen is refused before any call", async () => {
    const page = await open();
    const form = await within(page).findByRole("form", { name: "Let a conversation run ahead on Claude" });
    fireEvent.change(within(form).getByLabelText("Until"), { target: { value: "custom" } });
    fireEvent.click(within(form).getByRole("button", { name: "Let it run ahead" }));
    expect(await within(page).findByRole("alert")).toBeTruthy();
    expect(count("khipu_route_override_act")).toBe(0);
  });
});

describe("Running ahead, End now", () => {
  beforeEach(() => {
    overrides = [{ id: "ov-1", label: "Claude", who: "The T3 conversation “Khipu app mocks”", until: at(180), indefinite: false, review_due: false, cap: "x" }];
  });

  it("ends one with the old call, leaves a receipt in the list, and Undo calls the existing undo once", async () => {
    render(<Place />);
    // The list's Running ahead section and Claude's page (when it is open) both offer it; here the page shows Claude.
    const [end] = await screen.findAllByRole("button", { name: /End now: .*Khipu app mocks.* on Claude/ });
    fireEvent.click(end);
    await waitFor(() => expect(count("khipu_route_override_act")).toBe(1));
    expect(calls.find(([c]) => c === "khipu_route_override_act")![1]).toEqual({ verb: "end", id: "ov-1" });
    expect(await screen.findByText(/Ended\. .*Khipu app mocks.* is back to the normal pace on Claude\./)).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Running ahead" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(count("khipu_owner_direction_undo")).toBe(1));
    expect(calls.find(([c]) => c === "khipu_owner_direction_undo")![1]).toEqual({ id: "eeee5555" });
  });

  it("the row says how many conversations run ahead on the account", async () => {
    render(<Place />);
    const row = await screen.findByRole("button", { name: /^Claude/ });
    expect(row.textContent).toContain("1 conversation running ahead");
  });
});

describe("states that are not room", () => {
  it("a paused account, one at its limit, one whose state is unknown and one with no reading each say so", async () => {
    accounts = [
      claude({ state: "paused", until: at(60 * 24) }),
      grok({ state: "limited", until: at(70) }),
      { key: "x|", label: "Odd", harness: "claude", state: "mystery", pace: [{ window: "weekly", used_pct: 1, cycle_resets_at: at(3000), plan: { verdict: "underpacing" } }] },
      { key: "y|", label: "Fresh", harness: "glm", state: "open", pace: [], cap_statuses: [] },
    ];
    render(<Place />);
    const text = async (name: RegExp) => (await screen.findByRole("button", { name })).textContent ?? "";
    expect(await text(/^Claude/)).toContain("Paused");
    expect(await text(/^Grok/)).toContain("Out of room");
    const odd = await text(/^Odd/);
    expect(odd).toContain("Not known");
    expect(odd).not.toMatch(/% left/);
    expect(await text(/^Fresh/)).toContain("No reading yet");
    // Only an account with a reading draws a meter.
    expect(document.querySelectorAll(".ac-meter").length).toBe(0);
  });

  it("when the record can't be read it says so, with a way to try again, and shows no accounts", async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      calls.push([cmd, undefined]);
      if (cmd === "khipu_route_view") throw new Error("down");
      return JSON.stringify({ ok: true, overrides: [] });
    });
    render(<Place />);
    expect(await screen.findByRole("button", { name: "Try again" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Claude/ })).toBeNull();
  });
});

describe("the words on the screen", () => {
  it("use none of the words the scope removes", async () => {
    overrides = [{ id: "ov-1", label: "Claude", who: "The T3 conversation “Khipu app mocks”", until: at(180), indefinite: false, review_due: false, cap: "x" }];
    await open("Grok");
    const text = document.body.textContent ?? "";
    for (const word of ["harness", "hub", "capture", "episode", "job slot", "embedding", "gateway", "ledger", "owed", "tier", "hold", "cap", "override"]) {
      expect(text.toLowerCase()).not.toMatch(new RegExp(`\\b${word}s?\\b`));
    }
  });
});
