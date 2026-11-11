// The Memory place (slice A7): search, the switches, the conversation beside
// the list, and Forget. Every call goes to a fake `run`; nothing is forgotten
// except through it.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Memory } from "../memory/Memory";

// A fixed local clock (2:00 pm), so "Today" and "Yesterday" never depend on the
// hour the suite runs: an hour or two ago is today, thirty hours ago is yesterday.
const NOW = new Date(2026, 10, 11, 14, 0, 0);
const ago = (h: number) => new Date(NOW.getTime() - h * 3600_000).toISOString();

const RECENT = [
  { id: 11, ts: ago(1), summary: "About one.", title: "Making the app simpler", tool: "Claude Code", mac: "M5", project: "Khipu", quote: "first line" },
  { id: 12, ts: ago(2), summary: "About two.", title: "Review of the clean-up", tool: "Codex", mac: "M5", project: "Khipu", quote: null },
  { id: 13, ts: ago(30), summary: "About three.", title: "Sunday slides", tool: "Claude Code", mac: "M1", project: "Church slides", quote: null },
];
const HIT = { kind: "episode", id: "11", title: "Making the app simpler", quote: "I want room for Grok Bot to breathe", tool: "Claude Code", mac: "M5", project: "Khipu", ts: ago(1) };
const SHOW = { id: 11, summary: "About one.", decisions: ["Three places."], verbatim: { quotes: ["I want room for Grok Bot to breathe", "Second line"] } };

type Call = string[];
function fakeRun(over: (args: Call) => unknown | undefined = () => undefined) {
  const calls: Call[] = [];
  const run = vi.fn(async (args: Call) => {
    calls.push(args);
    const custom = over(args);
    if (custom instanceof Promise) return custom as Promise<string>;
    if (custom !== undefined) return typeof custom === "string" ? custom : JSON.stringify(custom);
    if (args[0] === "activity" && args[1] === "--limit") return JSON.stringify({ recent: RECENT });
    if (args[0] === "activity" && args[1]?.startsWith("--show=")) return JSON.stringify(SHOW);
    if (args[0] === "search") return JSON.stringify({ ok: true, results: [HIT] });
    if (args[0] === "episode" && args[1] === "forget") return JSON.stringify({ ok: true, soft_deleted: true, partial: [], snapshot: { ok: true }, legacy_file: { ok: true } });
    return "{}";
  });
  return { run, calls };
}

const searchCalls = (calls: Call[]) => calls.filter((c) => c[0] === "search");
// The pending-forgets list (moved here in A11) reads `episode forget-pending`; it forgets nothing.
const forgetCalls = (calls: Call[]) => calls.filter((c) => c[0] === "episode" && c[1] !== "forget-pending");

async function searchFor(text: string) {
  const box = await screen.findByRole("searchbox", { name: "Search what you and your agents said" });
  fireEvent.change(box, { target: { value: text } });
  fireEvent.submit(box.closest("form")!);
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  HTMLElement.prototype.scrollIntoView = vi.fn();
  // jsdom has no <dialog>.showModal
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("Memory: recent conversations", () => {
  it("lists them by day when the box is empty, and opens one", async () => {
    const { run, calls } = fakeRun();
    render(<Memory run={run} />);
    expect(await screen.findByRole("heading", { name: "Recent conversations" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Today" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Yesterday" })).toBeVisible();
    expect(calls.filter((c) => c[1] !== "forget-pending")[0]).toEqual(["activity", "--limit", "40"]);
    fireEvent.click(screen.getByRole("button", { name: /Making the app simpler/ }));
    expect(await screen.findByRole("heading", { name: "What you said" })).toBeVisible();
    expect(calls).toContainEqual(["activity", "--show=11"]);
    expect(screen.getByText(/Three places\./)).toBeVisible();
    expect(screen.getByRole("heading", { name: "Making the app simpler", level: 2 })).toBeVisible();
  });

  it("moves through the list with the arrow keys, opening each one", async () => {
    const { run, calls } = fakeRun();
    render(<Memory run={run} />);
    const first = await screen.findByRole("button", { name: /Making the app simpler/ });
    first.focus();
    fireEvent.keyDown(first, { key: "ArrowDown" });
    await waitFor(() => expect(calls).toContainEqual(["activity", "--show=12"]));
    expect(screen.getByRole("button", { name: /Review of the clean-up/ })).toHaveAttribute("aria-current", "true");
  });

  it("offers the Try links and says nothing in the old words", async () => {
    const { run, calls } = fakeRun();
    render(<Memory run={run} />);
    fireEvent.click(await screen.findByRole("button", { name: "room for Grok Bot" }));
    await waitFor(() => expect(searchCalls(calls)).toHaveLength(1));
    expect(searchCalls(calls)[0].slice(-1)[0]).toBe("room for Grok Bot");
    const text = document.body.textContent ?? "";
    for (const word of ["harness", "hub", "capture", "episode", "embedding", "gateway", "ledger"]) {
      expect(text.toLowerCase()).not.toContain(word);
    }
  });
});

describe("Memory: search", () => {
  it("passes --owner-words while Only my words is on (the default), and not when it is off", async () => {
    const { run, calls } = fakeRun();
    render(<Memory run={run} />);
    expect(await screen.findByRole("button", { name: "Only my words" })).toHaveAttribute("aria-pressed", "true");
    await searchFor("room for Grok Bot");
    await waitFor(() => expect(searchCalls(calls)).toHaveLength(1));
    expect(searchCalls(calls)[0]).toEqual(["search", "--mode", "hybrid", "--limit", "20", "--owner-words", "--", "room for Grok Bot"]);
    fireEvent.click(screen.getByRole("button", { name: "Only my words" }));
    await waitFor(() => expect(searchCalls(calls)).toHaveLength(2));
    expect(searchCalls(calls)[1]).not.toContain("--owner-words");
    expect(searchCalls(calls)[1]).toEqual(["search", "--mode", "hybrid", "--limit", "20", "--", "room for Grok Bot"]);
  });

  it("Exact words searches by literal mode, and a dash-led query stays after --", async () => {
    const { run, calls } = fakeRun();
    render(<Memory run={run} />);
    fireEvent.click(await screen.findByRole("button", { name: "Exact words" }));
    await searchFor("--limit");
    await waitFor(() => expect(searchCalls(calls)).toHaveLength(1));
    const args = searchCalls(calls)[0];
    expect(args.slice(0, 3)).toEqual(["search", "--mode", "literal"]);
    expect(args.slice(-2)).toEqual(["--", "--limit"]);
  });

  it("shows the match with the quote marked, and 'You said' only while Only my words is on", async () => {
    const { run } = fakeRun();
    render(<Memory run={run} />);
    await searchFor("room for Grok Bot");
    const hit = await screen.findByRole("button", { name: /Making the app simpler/ });
    expect(within(hit).getByText("room for Grok Bot", { selector: "mark" })).toBeVisible();
    expect(hit.textContent).toContain("You said · ");
    expect(screen.getByText("1 conversation")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Only my words" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /Making the app simpler/ }).textContent).not.toContain("You said"));
  });

  it("says nothing matched when the search ran and found nothing", async () => {
    const { run } = fakeRun((a) => (a[0] === "search" ? { ok: true, results: [] } : undefined));
    render(<Memory run={run} />);
    await searchFor("purple elephant");
    expect(await screen.findByRole("heading", { name: "Nothing matched “purple elephant”." })).toBeVisible();
  });

  it("when Only my words cannot run, says so plainly (not 'no match') and offers to search everything", async () => {
    const { run, calls } = fakeRun((a) =>
      a[0] === "search" && a.includes("--owner-words")
        ? { ok: false, error: "searching only your words needs the hub; the offline snapshot cannot do it" }
        : undefined,
    );
    render(<Memory run={run} />);
    await searchFor("room for Grok Bot");
    expect(await screen.findByRole("heading", { name: /Searching only your words isn’t available/ })).toBeVisible();
    expect(screen.queryByText(/Nothing matched/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Search everything instead" }));
    await waitFor(() => expect(searchCalls(calls)).toHaveLength(2));
    expect(searchCalls(calls)[1]).not.toContain("--owner-words");
    expect(await screen.findByRole("button", { name: /Making the app simpler/ })).toBeVisible();
    expect(screen.getByRole("button", { name: "Only my words" })).toHaveAttribute("aria-pressed", "false");
  });

  it("a search that throws is a failure with Try again, never an empty result", async () => {
    let fail = true;
    const { run } = fakeRun((a) => {
      if (a[0] !== "search") return undefined;
      if (fail) throw new Error("spawn failed");
      return undefined;
    });
    render(<Memory run={run} />);
    await searchFor("anything");
    expect(await screen.findByRole("heading", { name: "Search didn’t finish." })).toBeVisible();
    expect(screen.getByText(/spawn failed/)).toBeVisible();
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: /Making the app simpler/ })).toBeVisible();
  });

  it("going back to an empty box brings back the recent conversations", async () => {
    const { run } = fakeRun();
    render(<Memory run={run} />);
    await searchFor("room");
    await screen.findByText("1 conversation");
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "" } });
    expect(await screen.findByRole("heading", { name: "Recent conversations" })).toBeVisible();
  });

  it("an older search that finishes late does not replace a newer one", async () => {
    const releases: Array<() => void> = [];
    const { run } = fakeRun((a) => {
      if (a[0] !== "search") return undefined;
      const q = a[a.length - 1];
      return new Promise<string>((resolve) => {
        releases.push(() => resolve(JSON.stringify({ ok: true, results: [{ ...HIT, id: q === "first" ? "21" : "22", title: q === "first" ? "Old answer" : "New answer" }] })));
      });
    });
    render(<Memory run={run} />);
    await searchFor("first");
    await searchFor("second");
    await waitFor(() => expect(releases).toHaveLength(2));
    await act(async () => { releases[1](); });
    await act(async () => { releases[0](); });
    expect(await screen.findByRole("button", { name: /New answer/ })).toBeVisible();
    expect(screen.queryByRole("button", { name: /Old answer/ })).toBeNull();
  });
});

describe("Memory: Forget", () => {
  async function openFirstAndAskToForget(run: (a: string[]) => Promise<string>) {
    render(<Memory run={run} />);
    fireEvent.click(await screen.findByRole("button", { name: /Making the app simpler/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Forget this conversation" }));
    return screen.findByRole("textbox", { name: "Type forget to confirm" });
  }

  it("asks first, and the button stays off until the owner types forget", async () => {
    const { run, calls } = fakeRun();
    const field = await openFirstAndAskToForget(run);
    const go = screen.getByRole("button", { name: "Forget it" });
    expect(go).toBeDisabled();
    expect(forgetCalls(calls)).toHaveLength(0);
    fireEvent.change(field, { target: { value: "forge" } });
    expect(go).toBeDisabled();
    fireEvent.click(go);
    expect(forgetCalls(calls)).toHaveLength(0);
    fireEvent.change(field, { target: { value: "Forget " } });
    expect(go).toBeEnabled();
  });

  it("Keep it forgets nothing", async () => {
    const { run, calls } = fakeRun();
    const field = await openFirstAndAskToForget(run);
    fireEvent.change(field, { target: { value: "forget" } });
    fireEvent.click(screen.getByRole("button", { name: "Keep it" }));
    expect(forgetCalls(calls)).toHaveLength(0);
    expect(screen.getByRole("button", { name: /Making the app simpler/ })).toBeVisible();
  });

  it("forgets through the app's one existing call, then drops it from the list", async () => {
    const { run, calls } = fakeRun();
    const onForgotten = vi.fn();
    render(<Memory run={run} onForgotten={onForgotten} />);
    fireEvent.click(await screen.findByRole("button", { name: /Making the app simpler/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Forget this conversation" }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Type forget to confirm" }), { target: { value: "forget" } });
    fireEvent.click(screen.getByRole("button", { name: "Forget it" }));
    await waitFor(() => expect(onForgotten).toHaveBeenCalledWith(11));
    expect(forgetCalls(calls)).toEqual([["episode", "forget", "11"]]);
    expect(screen.queryByRole("button", { name: /Making the app simpler/ })).toBeNull();
    expect(screen.getByText("Forgot “Making the app simpler”.")).toBeVisible();
  });

  it("a forget that is not confirmed by the backend stays in the dialog with its error, and changes nothing", async () => {
    const { run } = fakeRun((a) => (a[0] === "episode" ? { ok: false, error: "database is read-only" } : undefined));
    const onForgotten = vi.fn();
    render(<Memory run={run} onForgotten={onForgotten} />);
    fireEvent.click(await screen.findByRole("button", { name: /Making the app simpler/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Forget this conversation" }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Type forget to confirm" }), { target: { value: "forget" } });
    fireEvent.click(screen.getByRole("button", { name: "Forget it" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("database is read-only");
    expect(onForgotten).not.toHaveBeenCalled();
    expect(screen.getAllByText(/Making the app simpler/).length).toBeGreaterThan(0);
  });

  it("says when a copy kept on this Mac could not be removed", async () => {
    const { run } = fakeRun((a) =>
      a[0] === "episode" ? { ok: true, soft_deleted: true, partial: [], snapshot: { ok: false, error: "disk full" }, legacy_file: { ok: true } } : undefined,
    );
    render(<Memory run={run} />);
    fireEvent.click(await screen.findByRole("button", { name: /Making the app simpler/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Forget this conversation" }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Type forget to confirm" }), { target: { value: "forget" } });
    fireEvent.click(screen.getByRole("button", { name: "Forget it" }));
    expect(await screen.findByText(/a copy kept on this Mac could not be removed: disk full/)).toBeVisible();
  });
});

describe("Memory: Copy a link", () => {
  it("copies text an agent can open, and says so", async () => {
    const writeText = vi.fn(async (_text: string) => {});
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    const { run } = fakeRun();
    render(<Memory run={run} />);
    fireEvent.click(await screen.findByRole("button", { name: /Making the app simpler/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Copy a link for an agent" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(writeText.mock.calls[0][0]).toContain("khipu_get");
    expect(writeText.mock.calls[0][0]).toContain("11");
    expect(await screen.findByText(/Copied\. Paste it into any conversation/)).toBeVisible();
    vi.unstubAllGlobals();
  });
});

describe("Memory: correct what a conversation was about (moved from the older Activity screen)", () => {
  const editCalls = (calls: Call[]) => calls.filter((c) => c[0] === "episode" && c[1] === "edit");

  it("saves the corrected summary with the old call, only on Save, then reads the conversation again", async () => {
    const { run, calls } = fakeRun((a) => (a[0] === "episode" && a[1] === "edit" ? { ok: true, reembedded: false } : undefined));
    render(<Memory run={run} />);
    fireEvent.click(await screen.findByRole("button", { name: /Making the app simpler/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Correct what it was about…" }));
    const box = screen.getByRole("textbox", { name: "What this conversation was about" });
    expect(box).toHaveValue("About one.");
    fireEvent.change(box, { target: { value: "-- about two" } });
    expect(editCalls(calls)).toEqual([]);
    const shows = calls.filter((c) => c[1] === "--show=11").length;
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(editCalls(calls)).toEqual([["episode", "edit", "11", "--summary=-- about two"]]));
    expect(await screen.findByText("Saved. Search by meaning picks it up at the next nightly.")).toBeInTheDocument();
    await waitFor(() => expect(calls.filter((c) => c[1] === "--show=11").length).toBe(shows + 1));
  });

  it("a refusal is shown and Cancel changes nothing", async () => {
    const { run, calls } = fakeRun((a) => (a[0] === "episode" && a[1] === "edit" ? { ok: false, error: "summary too long" } : undefined));
    render(<Memory run={run} />);
    fireEvent.click(await screen.findByRole("button", { name: /Making the app simpler/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Correct what it was about…" }));
    fireEvent.change(screen.getByRole("textbox", { name: "What this conversation was about" }), { target: { value: "New" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("summary too long");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(editCalls(calls)).toHaveLength(1);
  });
});
