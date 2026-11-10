// Today's Needs you, receipts and All changes (slice A3). Each rule of the
// A3 brief has its own test here. Fakes only: nothing real is answered.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NeedsCards } from "../today/NeedsCards";
import { ChangesPanel, changeWords, hasRemovedWord } from "../today/ChangesPanel";
import { NO_ANSWER, parseNeeds, refusalSentence, useNeedsYou, type NeedsItem } from "../today/needs";
import { ACTION_LABELS, type OwnerAction } from "../OwnerActions";

const invokeMock = vi.hoisted(() => vi.fn());
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));

const item = (key: string, over: Partial<NeedsItem> = {}): NeedsItem => ({
  kind: "rule", key, words: `Words of ${key}.`, confirm_does: `Yes does ${key}.`, undo_does: `No does ${key}.`,
  settle: { confirm: { action: "route.rule.confirm", args: {} }, undo: { action: "route.rule.undo", args: {} } },
  from_words: "From a Codex conversation on M1.", mac: "M1", ago: "4 minutes ago", expect: `pin-${key}`, ...over,
});
const listOf = (items: NeedsItem[], extra: Record<string, unknown> = {}) =>
  JSON.stringify({ ok: true, total: items.length, items, more: 0, this_mac: "M5", ...extra });
const done = (over: Record<string, unknown> = {}) =>
  JSON.stringify({ ok: true, receipt: { text: "You said yes. Done here.", action_id: "a1b2c3d4", undoable: true, saved_on: ["M5"], not_reached: [], ...over } });

/** Routes the fake invoke: `reads` answers each read in turn (the last one
 *  repeats); `answers` each answer in turn. */
function route(reads: string[], answers: Array<string | Promise<string> | Error> = [], extra: Record<string, unknown> = {}) {
  let r = 0;
  let a = 0;
  invokeMock.mockImplementation((cmd: string, args?: Record<string, unknown>) => {
    if (cmd === "khipu_needs_you" && args?.key && typeof extra.whole === "function") return (extra.whole as (k: unknown) => unknown)(args.key);
    if (cmd === "khipu_needs_you") return Promise.resolve(reads[Math.min(r++, reads.length - 1)]);
    if (cmd === "khipu_needs_you_answer") {
      const next = answers[a++];
      if (next instanceof Error) return Promise.reject(next);
      return Promise.resolve(next);
    }
    if (cmd in extra) {
      const v = extra[cmd];
      return typeof v === "function" ? (v as () => unknown)() : Promise.resolve(v);
    }
    return Promise.reject(new Error(`unexpected ${cmd}`));
  });
}
const calls = (cmd: string) => invokeMock.mock.calls.filter((c) => c[0] === cmd && !(cmd === "khipu_needs_you" && c[1]?.key));
const wholeReads = () => invokeMock.mock.calls.filter((c) => c[0] === "khipu_needs_you" && c[1]?.key);

function Host({ onAllChanges = () => {} }: { onAllChanges?: () => void }) {
  const needs = useNeedsYou(true, true);
  return <NeedsCards needs={needs} onAllChanges={onAllChanges} />;
}
const card = (words: string) => screen.getByText(words).closest("article") as HTMLElement;

beforeEach(() => {
  invokeMock.mockReset();
});
afterEach(cleanup);

describe("one click answers exactly one item, with its pin", () => {
  it("sends only the clicked card's key and the pin the read gave it", async () => {
    route([listOf([item("rule:1"), item("rule:2")]), listOf([item("rule:1")])], [done()]);
    render(<Host />);
    await screen.findByText("Words of rule:2.");
    await act(async () => { fireEvent.click(within(card("Words of rule:2.")).getByRole("button", { name: "Yes" })); });
    expect(calls("khipu_needs_you_answer")).toEqual([["khipu_needs_you_answer", { key: "rule:2", answer: "yes", expect: "pin-rule:2" }]]);
    await screen.findByText("You said yes. Done here.");
    expect(screen.getByText("Words of rule:1.")).toBeTruthy();
  });
  it("sends no pin for an item that has none, and No sends no", async () => {
    route([listOf([item("held:r1", { expect: null })]), listOf([])], [done({ undoable: false, action_id: null })]);
    render(<Host />);
    await screen.findByText("Words of held:r1.");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "No" })); });
    expect(calls("khipu_needs_you_answer")).toEqual([["khipu_needs_you_answer", { key: "held:r1", answer: "no", expect: null }]]);
  });
  it("never answers on load, on focus, or on a held key", async () => {
    route([listOf([item("rule:1")])]);
    render(<Host />);
    await screen.findByText("Words of rule:1.");
    const yes = screen.getByRole("button", { name: "Yes" });
    act(() => { yes.focus(); });
    const notCancelled = fireEvent.keyDown(yes, { key: "Enter", repeat: true });
    expect(notCancelled).toBe(false); // the held key's repeat is cancelled
    await act(async () => { await new Promise((r) => setTimeout(r, 30)); });
    expect(calls("khipu_needs_you_answer")).toHaveLength(0);
    expect(document.activeElement).toBe(yes);
  });
  it("shows Khipu's words for the item exactly as given, and what each answer does", async () => {
    const words = "A conversation proposed a standing rule: “Keep Grok away from sign-in, privacy and money code.”";
    route([listOf([item("rule:3", { words, confirm_does: "Keeps the rule.", undo_does: "Drops the proposed rule. Nothing changes." })])]);
    render(<Host />);
    expect((await screen.findByText(words)).textContent).toBe(words);
    expect(screen.getByRole("button", { name: "Yes" }).getAttribute("aria-describedby")).toBeTruthy();
    expect(screen.getByText("Keeps the rule.")).toBeTruthy();
    expect(screen.getByText("Drops the proposed rule. Nothing changes.")).toBeTruthy();
  });
});

describe("changed", () => {
  it("answers nothing, reads again and shows the item again with a line that it changed", async () => {
    const before = item("rule:1");
    const after = item("rule:1", { words: "New words of rule:1.", expect: "pin-new" });
    route([listOf([before]), listOf([after])], [JSON.stringify({ ok: false, code: "changed", key: "rule:1", error: "This changed since you saw it." })]);
    render(<Host />);
    await screen.findByText("Words of rule:1.");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Yes" })); });
    await screen.findByText("New words of rule:1.");
    expect(screen.getByRole("alert").textContent).toContain("This changed since you saw it, so nothing was changed.");
    expect(calls("khipu_needs_you_answer")).toHaveLength(1); // never sent again on its own
    expect(calls("khipu_needs_you").length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByRole("status", { name: /You said/ })).toBeNull();
    // The next answer is the owner's own click, with the new pin.
    route([listOf([after]), listOf([])], [done()]);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Yes" })); });
    const answers = calls("khipu_needs_you_answer");
    expect(answers[answers.length - 1]).toEqual(["khipu_needs_you_answer", { key: "rule:1", answer: "yes", expect: "pin-new" }]);
  });
});

describe("an item that can't be taken back", () => {
  it("sits on its own card, says so before its button, and Yes answers only it", async () => {
    const c = item("forget_send", {
      kind: "forget_send", permanent: true, expect: null, words: "You forgot 2 conversations on this Mac.",
      confirm_does: "Sends the forgets. This can't be undone.", settle: { confirm: { action: "forget.send", args: {} }, undo: null },
    });
    route([listOf([item("rule:1"), c]), listOf([item("rule:1")])], [done({ action_id: null, undoable: false, text: "Started." })]);
    render(<Host />);
    await screen.findByText("You forgot 2 conversations on this Mac.");
    const box = card("You forgot 2 conversations on this Mac.");
    expect(box.getAttribute("data-permanent")).toBe("true");
    const warn = within(box).getByText(/can’t be taken back/);
    const yes = within(box).getByRole("button", { name: "Yes" });
    expect(warn.compareDocumentPosition(yes) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(box).queryByRole("button", { name: "No" })).toBeNull(); // Khipu gave no undo
    expect(within(box).queryAllByRole("article")).toHaveLength(0);
    // No "yes to all": the only buttons are each card's own and the link to All changes.
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["Yes", "No", "Yes", "See all changes"]);
    await act(async () => { fireEvent.click(yes); });
    expect(calls("khipu_needs_you_answer")).toEqual([["khipu_needs_you_answer", { key: "forget_send", answer: "yes", expect: null }]]);
    await screen.findByText("Started.");
    expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
  });
});

describe("while an answer is out", () => {
  it("every Yes and No ignores presses, and a second press sends nothing", async () => {
    let release: (v: string) => void = () => {};
    const pending = new Promise<string>((r) => { release = r; });
    route([listOf([item("rule:1"), item("rule:2")]), listOf([item("rule:2")])], [pending]);
    render(<Host />);
    await screen.findByText("Words of rule:1.");
    const yes1 = within(card("Words of rule:1.")).getByRole("button", { name: "Yes" });
    fireEvent.click(yes1);
    fireEvent.click(yes1);
    for (const b of screen.getAllByRole("button", { name: /^(Yes|No)$/ })) expect(b.getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(within(card("Words of rule:2.")).getByRole("button", { name: "No" }));
    expect(calls("khipu_needs_you_answer")).toHaveLength(1);
    expect(screen.getByText("Sending your answer…")).toBeTruthy();
    await act(async () => { release(done()); });
    await screen.findByText("You said yes. Done here.");
    expect(within(card("Words of rule:2.")).getByRole("button", { name: "No" }).getAttribute("aria-disabled")).toBeNull();
  });
});

describe("a refused or failed answer", () => {
  it("says so in plain words and leaves the card as it was", async () => {
    route([listOf([item("rule:1")])], [JSON.stringify({ ok: false, code: "no_action", error: "Answer it in its conversation." })]);
    render(<Host />);
    await screen.findByText("Words of rule:1.");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Yes" })); });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("There is nothing for this answer to do, so nothing was changed. Answer it in its conversation.");
    expect(screen.getByText("Words of rule:1.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Yes" }).getAttribute("aria-disabled")).toBeNull();
    expect(screen.queryByText(/That’s everything/)).toBeNull();
    expect(calls("khipu_needs_you_answer")).toHaveLength(1);
  });
  it("a call that never answered says it may not have been applied, and is not sent again", async () => {
    route([listOf([item("rule:1")])], [new Error("khipu timed out")]);
    render(<Host />);
    await screen.findByText("Words of rule:1.");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "No" })); });
    expect((await screen.findByRole("alert")).textContent).toBe(NO_ANSWER);
    await act(async () => { await new Promise((r) => setTimeout(r, 30)); });
    expect(calls("khipu_needs_you_answer")).toHaveLength(1);
  });
  it("every refusal code reads as a sentence that says nothing was changed", () => {
    for (const code of ["changed", "version_moved", "not_found", "not_pending", "decline_only", "no_action", "owner_only", "bad_args", "not_sent", "refused", "something_new"]) {
      const s = refusalSentence(code, "why").sentence;
      expect(s, code).toMatch(/nothing was changed/);
      expect(s, code).not.toMatch(/_/);
    }
  });
});

describe("receipts and Undo", () => {
  it("Undo runs the existing undo on the receipt's action once, then is gone", async () => {
    let release: (v: string) => void = () => {};
    route([listOf([item("rule:1")]), listOf([])], [done()], {
      khipu_owner_direction_undo: () => new Promise<string>((r) => { release = r; }),
    });
    render(<Host />);
    await screen.findByText("Words of rule:1.");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Yes" })); });
    await screen.findByText("You said yes. Done here.");
    expect(screen.getByText("Saved on M5.")).toBeTruthy();
    expect(screen.getByText(/That’s everything/)).toBeTruthy();
    const undo = screen.getByRole("button", { name: "Undo" });
    fireEvent.click(undo);
    fireEvent.click(undo);
    expect(calls("khipu_owner_direction_undo")).toEqual([["khipu_owner_direction_undo", { id: "a1b2c3d4" }]]);
    await act(async () => { release(JSON.stringify({ ok: true, undid: "a1b2c3d4" })); });
    await screen.findByText("Undone.");
    expect(screen.queryByRole("button", { name: /Undo/ })).toBeNull();
  });
  it("offers no Undo when Khipu says the answer can't be undone", async () => {
    route([listOf([item("rule:1")]), listOf([])], [done({ undoable: false })]);
    render(<Host />);
    await screen.findByText("Words of rule:1.");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Yes" })); });
    await screen.findByText("You said yes. Done here.");
    expect(screen.queryByRole("button", { name: /Undo/ })).toBeNull();
  });
});

describe("other cards", () => {
  it("a sign-in card answers its own item, then starts the account's sign-in", async () => {
    const s = item("plan:Kimi", { kind: "signin", expect: null, words: "Kimi is not signed in on this Mac.", do: "Sign in with Kimi's own program on this Mac.", settle: { confirm: null, undo: null } });
    route([listOf([s]), listOf([])], [JSON.stringify({ ok: true, applied: false, signin_account: { label: "Kimi", harness: "kimi", home: null } })], {
      khipu_account_sign_in: JSON.stringify({ ok: true }),
    });
    render(<Host />);
    await screen.findByText("Kimi is not signed in on this Mac.");
    expect(screen.queryByRole("button", { name: "No" })).toBeNull();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Sign in to Kimi" })); });
    expect(calls("khipu_needs_you_answer")).toEqual([["khipu_needs_you_answer", { key: "plan:Kimi", answer: "yes", expect: null }]]);
    await waitFor(() => expect(calls("khipu_account_sign_in")).toEqual([["khipu_account_sign_in", { harness: "kimi", home: null }]]));
    await screen.findByText(/^Signed in/);
  });
  it("a shortened item offers Read all of it and no Yes or No until it is read whole", async () => {
    route([listOf([item("held:r2", { cut: true })])]);
    render(<Host />);
    await screen.findByText("Words of held:r2.");
    expect(screen.queryByRole("button", { name: /^(Yes|No)$/ })).toBeNull();
    expect(screen.getByText(/shortened these words/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Read all of it" })).toBeTruthy();
    expect(wholeReads()).toHaveLength(0); // never read on load
  });
  it("the empty list shows the last answer and opens All changes", async () => {
    const onAll = vi.fn();
    route([listOf([], { last_answer: { text: "You said yes: let a conversation run ahead of pace.", ago: "2 days ago", action_id: "c1d2e3f4", undoable: true } })]);
    render(<Host onAllChanges={onAll} />);
    await screen.findByText("Nothing needs you.");
    expect(screen.getByText(/Your last answer, 2 days ago: You said yes: let a conversation run ahead of pace\./)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "See all changes" }));
    expect(onAll).toHaveBeenCalled();
  });
  it("counts what is waiting from the same read", async () => {
    route([listOf([item("rule:1"), item("rule:2")])]);
    render(<Host />);
    expect(await screen.findByText("2 questions")).toBeTruthy();
    expect(parseNeeds(listOf([item("rule:1"), item("rule:2")]))?.total).toBe(2);
  });
});

describe("All changes", () => {
  const line = (over: Partial<OwnerAction> & { mac?: string }): OwnerAction => ({ id: "c1d2e3f4", at: "2026-11-04T21:12:00Z", action: "route.pause", target: "glm|", auth: "app", state: "applied", ...over });
  it("lists every change newest first, with one Undo per press", async () => {
    let actions = [line({}), line({ id: "d2e3f4a5", action: "route.rule.add", target: "6", no_undo: "a reason", mac: "M1" })];
    invokeMock.mockImplementation((cmd: string) => {
      if (cmd === "khipu_owner_direction_actions") return Promise.resolve(JSON.stringify({ ok: true, actions }));
      if (cmd === "khipu_owner_direction_undo") {
        actions = [{ ...actions[0], state: "undone" }, actions[1]];
        return Promise.resolve(JSON.stringify({ ok: true }));
      }
      return Promise.reject(new Error(cmd));
    });
    const onChanged = vi.fn();
    render(<ChangesPanel onClose={() => {}} onChanged={onChanged} />);
    await screen.findByText("Paused Glm");
    expect(screen.getByText(/You, on M1|You, in the app, on M1|You, through an agent, on M1/)).toBeTruthy();
    const undos = screen.getAllByRole("button", { name: /^Undo:/ });
    expect(undos).toHaveLength(1); // the second line has no undo
    await act(async () => { fireEvent.click(undos[0]); fireEvent.click(undos[0]); });
    expect(invokeMock.mock.calls.filter((c) => c[0] === "khipu_owner_direction_undo")).toEqual([["khipu_owner_direction_undo", { id: "c1d2e3f4" }]]);
    await waitFor(() => expect(screen.queryAllByRole("button", { name: /^Undo:/ })).toHaveLength(0));
    expect(onChanged).toHaveBeenCalledTimes(1);
  });
  it("never shows a word the new app dropped", () => {
    for (const action of Object.keys(ACTION_LABELS)) {
      for (const target of ["claude|", "7", "khipu#12", "trust claude_code:cli", "latest_window_h", "on"]) {
        const words = changeWords({ id: "x", at: "", action, target });
        expect(hasRemovedWord(words), `${action} ${target}: ${words}`).toBe(false);
      }
    }
  });
});

describe("a shortened item, read whole", () => {
  const short = item("rule:9", { cut: true, words: "A conversation proposed a standing rule: “Send every …”", expect: "pin-r9" });
  const full = item("rule:9", { cut: false, words: "A conversation proposed a standing rule: “Send every review of Claude’s work to Codex, and every review of Codex’s work to Claude, until Friday.”", expect: "pin-r9" });
  const wholeOf = (it: NeedsItem) => () => Promise.resolve(JSON.stringify({ ok: true, key: it.key, total: 1, items: [it] }));

  it("shows the whole words, then Yes and No below them, and answers with the full read's pin", async () => {
    route([listOf([item("rule:1"), short]), listOf([item("rule:1")])], [done()], { whole: wholeOf(full) });
    render(<Host />);
    await screen.findByText(short.words);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Read all of it" })); });
    expect(wholeReads()).toEqual([["khipu_needs_you", { key: "rule:9" }]]);
    const words = await screen.findByText(full.words);
    expect(screen.queryByText(short.words)).toBeNull();
    const box = card(full.words);
    const yes = within(box).getByRole("button", { name: "Yes" });
    const no = within(box).getByRole("button", { name: "No" });
    for (const b of [yes, no]) expect(words.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(box).queryByRole("button", { name: "Read all of it" })).toBeNull();
    await act(async () => { fireEvent.click(yes); });
    expect(calls("khipu_needs_you_answer")).toEqual([["khipu_needs_you_answer", { key: "rule:9", answer: "yes", expect: "pin-r9" }]]);
    await screen.findByText("You said yes. Done here.");
  });
  it("a whole read that finds nothing says so, reads the list again, and offers no Yes or No", async () => {
    route([listOf([short]), listOf([])], [], {
      whole: () => Promise.resolve(JSON.stringify({ ok: false, code: "not_found", key: "rule:9", error: "gone" })),
    });
    render(<Host />);
    await screen.findByText(short.words);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Read all of it" })); });
    expect((await screen.findByRole("alert")).textContent).toContain("no longer waiting");
    expect(calls("khipu_needs_you")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: /^(Yes|No)$/ })).toBeNull();
    expect(calls("khipu_needs_you_answer")).toHaveLength(0);
  });
  it("a whole read that fails, or comes back still shortened, says so and offers no Yes or No", async () => {
    for (const whole of [() => Promise.reject(new Error("timeout")), wholeOf({ ...full, cut: true }), wholeOf({ ...full, key: "rule:other" })]) {
      invokeMock.mockReset();
      route([listOf([short])], [], { whole });
      const view = render(<Host />);
      await screen.findByText(short.words);
      await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Read all of it" })); });
      expect((await screen.findByRole("alert")).textContent).toContain("couldn’t give the whole question");
      expect(screen.queryByRole("button", { name: /^(Yes|No)$/ })).toBeNull();
      expect(screen.getByRole("button", { name: "Read all of it" })).toBeTruthy(); // the owner may try again
      expect(calls("khipu_needs_you").length).toBeGreaterThanOrEqual(2); // the list was read again
      expect(calls("khipu_needs_you_answer")).toHaveLength(0);
      view.unmount();
    }
  });
  it("keeps the rules: in flight every button ignores presses, one press sends one answer", async () => {
    let release: (v: string) => void = () => {};
    route([listOf([item("rule:1"), short]), listOf([item("rule:1")])], [new Promise<string>((r) => { release = r; })], { whole: wholeOf(full) });
    render(<Host />);
    await screen.findByText(short.words);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Read all of it" })); });
    await screen.findByText(full.words);
    const no = within(card(full.words)).getByRole("button", { name: "No" });
    fireEvent.click(no);
    fireEvent.click(no);
    fireEvent.click(within(card("Words of rule:1.")).getByRole("button", { name: "Yes" }));
    for (const b of screen.getAllByRole("button", { name: /^(Yes|No)$/ })) expect(b.getAttribute("aria-disabled")).toBe("true");
    expect(calls("khipu_needs_you_answer")).toEqual([["khipu_needs_you_answer", { key: "rule:9", answer: "no", expect: "pin-r9" }]]);
    await act(async () => { release(done({ undoable: false })); });
    await screen.findByText("You said yes. Done here.");
  });
  it("changed: reads the list again and the card is shortened again until it is read again", async () => {
    const shortNew = { ...short, words: "A conversation proposed a standing rule: “Send no …”", expect: "pin-r9b" };
    route([listOf([short]), listOf([shortNew])], [JSON.stringify({ ok: false, code: "changed", key: "rule:9", error: "changed" })], { whole: wholeOf(full) });
    render(<Host />);
    await screen.findByText(short.words);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Read all of it" })); });
    await screen.findByText(full.words);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Yes" })); });
    await screen.findByText(shortNew.words);
    expect(screen.getByRole("alert").textContent).toContain("This changed since you saw it, so nothing was changed.");
    expect(screen.queryByText(full.words)).toBeNull();
    expect(screen.queryByRole("button", { name: /^(Yes|No)$/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Read all of it" })).toBeTruthy();
    expect(calls("khipu_needs_you_answer")).toHaveLength(1);
  });
});
