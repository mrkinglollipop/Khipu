// Accounts > Order and rules (slice A6). One test per rule the screen keeps.
// Nothing real is touched: `invoke` is a script that answers in the exact JSON
// slice B6 documents (packages/cli/khipu/routing_say.py).
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OrderAndRules } from "../accounts/OrderAndRules";
import { receiptWords, refusalWords, timedLines } from "../accounts/orderRulesModel";
import type { OwnerAction } from "../OwnerActions";

const invokeMock = vi.hoisted(() => vi.fn());
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));

const TOKEN = "0123456789abcdef0123456789abcdef";
const WORDS = "Put Codex first until Friday";

const ENTRIES = ["Claude", "Codex", "Claude second", "Grok"].map((label) => ({ account: `${label.toLowerCase()}|`, label }));
const VIEW = {
  record: true,
  version: 3,
  accounts: ENTRIES.map((e) => ({ key: e.account, label: e.label, harness: "x", state: "open" })),
  priority: { entries: ENTRIES, version: "p1" },
  rules: [
    { n: 1, text: "Send reviews of Claude’s work to Codex.", entity: "rule-1" },
    { n: 2, text: "Keep Grok away from sign-in, privacy and money code.", entity: "rule-2" },
    { n: 3, text: "Waiting for your yes.", entity: "rule-3", pending: true, pending_kind: "added" },
  ],
};

const PREVIEW = {
  ok: true,
  understood: "Put Codex first until Fri 13 Nov, 12:00 am",
  kind: "priority_first_until",
  fallback: false,
  account: "Codex",
  now: "1 Claude, 2 Codex, 3 Claude second, 4 Grok",
  after: "Codex goes first until Fri 13 Nov, 12:00 am, then the order goes back to: 1 Claude, 2 Codex, 3 Claude second, 4 Grok",
  ends: "2026-11-13T05:00:00Z",
  ends_words: "Fri 13 Nov, 12:00 am",
  token: TOKEN,
  expires_at: "2026-11-10T03:10:57Z",
  apply: { command: "route say", args: ["--from-app", "--apply", TOKEN] },
};
const FALLBACK = {
  ...PREVIEW,
  understood: "Add it as a rule in your words",
  kind: "add_rule",
  fallback: true,
  account: null,
  now: "2 rules",
  after: "3 rules, including: Use Codex for code review and Claude for planning",
  ends: null,
  ends_words: null,
};
const RECEIPT = { text: "Codex goes first until Fri 13 Nov, 12:00 am. Then the order goes back.", action_id: "234f21ab", undoable: true, saved_on: ["this Mac"], not_reached: [] };
const APPLIED = { ok: true, applied: true, kind: "priority_first_until", action_id: "234f21ab", receipt: RECEIPT };

type Answers = {
  preview?: unknown | (() => Promise<unknown>);
  apply?: unknown | (() => Promise<unknown>);
  undo?: unknown;
  actions?: OwnerAction[];
};
let calls: Array<{ cmd: string; args: Record<string, unknown> | undefined }>;

function script(a: Answers) {
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    calls.push({ cmd, args });
    const out = async (v: unknown) => JSON.stringify(typeof v === "function" ? await (v as () => Promise<unknown>)() : v);
    switch (cmd) {
      case "khipu_route_view":
        return JSON.stringify(VIEW);
      case "khipu_owner_direction_actions":
        return JSON.stringify({ ok: true, actions: a.actions ?? [] });
      case "khipu_route_say":
        return args?.mode === "preview" ? out(a.preview ?? PREVIEW) : out(a.apply ?? APPLIED);
      case "khipu_owner_direction_undo":
        return JSON.stringify(a.undo ?? { ok: true });
      default:
        return JSON.stringify({ ok: true });
    }
  });
}
const said = () => calls.filter((c) => c.cmd === "khipu_route_say");
const previews = () => said().filter((c) => c.args?.mode === "preview");
const applies = () => said().filter((c) => c.args?.mode === "apply");
const undos = () => calls.filter((c) => c.cmd === "khipu_owner_direction_undo");

function box() {
  return screen.getByRole("textbox", { name: "Change something by saying it" }) as HTMLInputElement;
}
function type(words: string) {
  fireEvent.change(box(), { target: { value: words } });
}
const previewButton = () => screen.getByRole("button", { name: "See what this does" });

async function showPreview(words = WORDS) {
  type(words);
  fireEvent.click(previewButton());
  await screen.findByRole("button", { name: "Save change" });
}

beforeEach(() => {
  calls = [];
  invokeMock.mockReset();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

async function openPage(a: Answers = {}, onSeeAll?: () => void) {
  script(a);
  render(<OrderAndRules active onSeeAll={onSeeAll} />);
  await screen.findByText("Claude second");
}

describe("Order and rules: typing and preview", () => {
  it("typing does nothing but type: no call, no preview, no Save", async () => {
    await openPage();
    type(WORDS);
    type(`${WORDS}, thanks`);
    expect(box().value).toBe(`${WORDS}, thanks`);
    expect(said()).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "Save change" })).toBeNull();
  });

  it("See what this does asks for one preview with the trimmed words, and writes nothing", async () => {
    await openPage();
    type(`  ${WORDS}  `);
    fireEvent.click(previewButton());
    await screen.findByRole("button", { name: "Save change" });
    expect(previews()).toHaveLength(1);
    expect(previews()[0].args).toEqual({ mode: "preview", value: WORDS });
    expect(applies()).toHaveLength(0);
    expect(undos()).toHaveLength(0);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("Enter in the box is the same as the button: a preview, never a save", async () => {
    await openPage();
    type(WORDS);
    fireEvent.submit(box().closest("form")!);
    await screen.findByRole("button", { name: "Save change" });
    expect(previews()).toHaveLength(1);
    expect(applies()).toHaveLength(0);
  });

  it("an empty box asks for words and calls nothing", async () => {
    await openPage();
    fireEvent.click(previewButton());
    expect(await screen.findByText("Type what you want to change first.")).toBeVisible();
    expect(said()).toHaveLength(0);
  });

  it("shows Khipu's own words, not a rewording: understood, now, after and when it ends", async () => {
    await openPage();
    await showPreview();
    const region = screen.getByRole("region", { name: "What will change" });
    for (const exact of [PREVIEW.understood, PREVIEW.now, PREVIEW.after, PREVIEW.ends_words]) {
      expect(within(region).getByText(exact)).toBeVisible();
    }
    expect(within(region).getByText("Now")).toBeVisible();
    expect(within(region).getByText("After")).toBeVisible();
    expect(within(region).getByText("Ends")).toBeVisible();
  });

  it("a change with no end shows no end row", async () => {
    await openPage({ preview: { ...PREVIEW, kind: "priority_first", ends: null, ends_words: null } });
    await showPreview("Put Codex first");
    expect(within(screen.getByRole("region", { name: "What will change" })).queryByText("Ends")).toBeNull();
  });

  it("the fallback says plainly what Khipu will do and shows the words exactly", async () => {
    const words = "Use Codex for code review and Claude for planning";
    await openPage({ preview: FALLBACK });
    await showPreview(words);
    const region = screen.getByRole("region", { name: "What will change" });
    expect(region.textContent).toContain("Khipu did not recognise a change it can make directly");
    expect(region.textContent).toContain("add your words as a rule for agents to follow");
    expect(region.querySelector("q")?.textContent).toBe(words);
    expect(within(region).getByText(FALLBACK.understood)).toBeVisible();
    expect(within(region).getByText(FALLBACK.after)).toBeVisible();
  });

  it("an ordinary preview does not say it is the fallback", async () => {
    await openPage();
    await showPreview();
    expect(screen.getByRole("region", { name: "What will change" }).textContent).not.toContain("did not recognise");
  });

  it("not_understood shows Khipu's reason and what it considered, and offers nothing to save", async () => {
    const reason = "That names more than one account (Codex and Grok), so I'm not guessing which to put first. Say it once for each.";
    await openPage({ preview: { ok: false, code: "not_understood", error: reason, reason, considered: ["Codex", "Grok"] } });
    type("put Codex and Grok first");
    fireEvent.click(previewButton());
    expect(await screen.findByText(reason)).toBeVisible();
    expect(screen.getByText("Khipu looked at: Codex, Grok.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Save change" })).toBeNull();
    expect(applies()).toHaveLength(0);
  });

  it("an owner_only refusal of a preview is said in Khipu's words and offers nothing to save", async () => {
    await openPage({ preview: { ok: false, code: "owner_only", error: "Only the Khipu app can change this." } });
    type(WORDS);
    fireEvent.click(previewButton());
    expect(await screen.findByText(/Only the Khipu app can change this\./)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Save change" })).toBeNull();
  });
});

describe("Order and rules: Save", () => {
  it("only an explicit click applies, once, with the token on screen", async () => {
    await openPage();
    await showPreview();
    expect(applies()).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Save change" }));
    await screen.findByRole("status");
    expect(applies()).toHaveLength(1);
    expect(applies()[0].args).toEqual({ mode: "apply", value: TOKEN });
  });

  it("is off while the call is out, and a second click does not send a second call", async () => {
    let release!: (v: unknown) => void;
    const held = new Promise((r) => (release = r));
    await openPage({ apply: () => held });
    await showPreview();
    const save = screen.getByRole("button", { name: "Save change" });
    fireEvent.click(save);
    fireEvent.click(save);
    await waitFor(() => expect(screen.getByRole("button", { name: "Save change" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(previewButton()).toBeDisabled();
    expect(applies()).toHaveLength(1);
    release(APPLIED);
    await screen.findByRole("status");
    expect(applies()).toHaveLength(1);
  });

  it("a saved change shows its receipt in Khipu's words, and the preview is gone", async () => {
    await openPage();
    await showPreview();
    fireEvent.click(screen.getByRole("button", { name: "Save change" }));
    const receipt = await screen.findByRole("status");
    expect(within(receipt).getByText(RECEIPT.text)).toBeVisible();
    expect(receipt.textContent).toContain("Saved on this Mac.");
    expect(screen.queryByRole("button", { name: "Save change" })).toBeNull();
    expect(box().value).toBe("");
  });

  it("a receipt names a Mac that was not reached", async () => {
    expect(receiptWords({ ...RECEIPT, saved_on: ["M5"], not_reached: ["M1"] }).saved).toContain("Not yet on M1.");
  });

  it("Cancel throws the preview away without a call", async () => {
    await openPage();
    await showPreview();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("button", { name: "Save change" })).toBeNull();
    expect(applies()).toHaveLength(0);
  });
});

describe("Order and rules: editing throws the preview away", () => {
  it("editing the words removes Save until a new preview of the new words", async () => {
    await openPage();
    await showPreview();
    type("Put Grok first until Friday");
    expect(screen.queryByRole("button", { name: "Save change" })).toBeNull();
    expect(screen.queryByRole("region", { name: "What will change" })).toBeNull();
    expect(applies()).toHaveLength(0);
    fireEvent.click(previewButton());
    await screen.findByRole("button", { name: "Save change" });
    expect(previews().map((c) => c.args?.value)).toEqual([WORDS, "Put Grok first until Friday"]);
  });

  it("a preview that arrives after the words changed is ignored", async () => {
    let release!: (v: unknown) => void;
    const held = new Promise((r) => (release = r));
    await openPage({ preview: () => held });
    type(WORDS);
    fireEvent.click(previewButton());
    await waitFor(() => expect(previews()).toHaveLength(1));
    type(`${WORDS} and more`);
    release(PREVIEW);
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole("button", { name: "Save change" })).toBeNull();
    expect(screen.queryByRole("region", { name: "What will change" })).toBeNull();
  });
});

describe("Order and rules: refusals", () => {
  const CASES: Array<[string, string, string, string]> = [
    ["changed", "This changed since you saw it, so nothing was changed. Preview it again.", "changed", "This changed since you saw it"],
    ["no_such_preview", "That preview has expired, or Khipu didn't make it. Preview the change again.", "no_such_preview", "has expired"],
    ["already_used", "That change was already saved. Preview it again to make it again.", "already_used", "already saved"],
  ];
  for (const [name, words, code, expected] of CASES) {
    it(`${name}: says so, changes nothing on screen that suggests a save, and offers no Save`, async () => {
      await openPage({ apply: { ok: false, code, error: words } });
      await showPreview();
      fireEvent.click(screen.getByRole("button", { name: "Save change" }));
      const alert = await screen.findByRole("alert");
      expect(alert.textContent).toContain(expected);
      expect(alert.textContent).toMatch(/Preview/);
      expect(screen.queryByRole("status")).toBeNull();
      expect(screen.queryByText(/Saved on/)).toBeNull();
      expect(screen.queryByRole("button", { name: "Save change" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
      expect(applies()).toHaveLength(1);
      // The order and rules on screen are the record's, not the previewed change.
      expect(screen.getAllByRole("listitem").some((li) => li.textContent === "1Claude")).toBe(true);
    });
  }

  it("any other refusal is shown in its own words with a way forward, and is not a save", async () => {
    await openPage({ apply: { ok: false, code: "rules_full", error: "You already have 40 rules, which is the most Khipu keeps. End one first." } });
    await showPreview();
    fireEvent.click(screen.getByRole("button", { name: "Save change" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("You already have 40 rules");
    expect(alert.textContent).toContain("Nothing was changed. Preview the change again.");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("the words for each refusal code come from one table", () => {
    expect(refusalWords("changed", "")).toContain("Preview it again");
    expect(refusalWords(undefined, "")).toContain("Nothing was changed");
  });
});

describe("Order and rules: Undo on the receipt", () => {
  async function saved(a: Answers = {}) {
    await openPage(a);
    await showPreview();
    fireEvent.click(screen.getByRole("button", { name: "Save change" }));
    return within(await screen.findByRole("status"));
  }

  it("calls the existing undo once, with the receipt's action id", async () => {
    const receipt = await saved();
    const undo = receipt.getByRole("button", { name: "Undo" });
    fireEvent.click(undo);
    fireEvent.click(undo);
    await screen.findByText("Undone. Your order and rules are back to how they were.");
    expect(undos()).toHaveLength(1);
    expect(undos()[0].args).toEqual({ id: "234f21ab" });
    expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
  });

  it("is not offered when the receipt says it cannot be undone", async () => {
    const receipt = await saved({ apply: { ...APPLIED, receipt: { ...RECEIPT, undoable: false } } });
    expect(receipt.queryByRole("button", { name: "Undo" })).toBeNull();
    expect(undos()).toHaveLength(0);
  });

  it("a refused undo says why, and does not say it was undone", async () => {
    const receipt = await saved({ undo: { ok: false, code: "newer_change", error: "A newer change touched this, so it was not undone." } });
    fireEvent.click(receipt.getByRole("button", { name: "Undo" }));
    expect(await screen.findByText("A newer change touched this, so it was not undone.")).toBeVisible();
    expect(screen.queryByText(/^Undone\./)).toBeNull();
  });

  it("makes no undo call until the owner clicks Undo", async () => {
    await saved();
    expect(undos()).toHaveLength(0);
  });
});

describe("Order and rules: the words go nowhere but khipu_route_say", () => {
  it("only the say command is ever sent the words, and nothing is logged", async () => {
    const secret = "Put Codex first until Friday, my private note";
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const info = vi.spyOn(console, "info").mockImplementation(() => {});
    const debug = vi.spyOn(console, "debug").mockImplementation(() => {});
    await openPage();
    await showPreview(secret);
    fireEvent.click(screen.getByRole("button", { name: "Save change" }));
    fireEvent.click(await screen.findByRole("button", { name: "Undo" }));
    await screen.findByText(/^Undone\./);
    for (const c of calls) {
      const sent = JSON.stringify(c.args ?? {});
      if (c.cmd === "khipu_route_say") continue;
      expect(sent).not.toContain("private note");
    }
    expect(applies()[0].args).toEqual({ mode: "apply", value: TOKEN });
    for (const spy of [log, warn, error, info, debug]) {
      expect(JSON.stringify(spy.mock.calls)).not.toContain("private note");
    }
  });

  it("typing never calls anything: no suggestion, no lookup", async () => {
    await openPage();
    const before = calls.length;
    for (const w of ["P", "Pu", "Put", "Put C", "Put Codex"]) type(w);
    expect(calls.length).toBe(before);
    expect(box().getAttribute("autocomplete")).toBe("off");
  });
});

describe("Order and rules: what the page shows", () => {
  it("shows the order and the rules the record holds, and not a rule still waiting for the owner", async () => {
    await openPage();
    expect(screen.getByRole("heading", { name: "Order your agents try" })).toBeVisible();
    expect(screen.getAllByRole("listitem").map((li) => li.textContent)).toEqual(
      expect.arrayContaining(["1Claude", "2Codex", "3Claude second", "4Grok"]),
    );
    expect(screen.getByText("Send reviews of Claude’s work to Codex.")).toBeVisible();
    expect(screen.queryByText("Waiting for your yes.")).toBeNull();
  });

  it("says when part of the order ends by itself", () => {
    const lines = timedLines(
      [{ account: "codex|", label: "Codex", exceptions: [{ text: "Codex goes first.", until: "2026-11-13T05:00:00Z" }] }],
      new Date("2026-11-10T12:00:00Z"),
    );
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain("Codex goes first.");
    expect(lines[0]).toContain("then the order goes back");
  });

  it("Recent changes: newest four, in plain words, with who and when, and no removed words", async () => {
    const at = (m: number) => new Date(Date.now() - m * 60000).toISOString();
    const rows: OwnerAction[] = [
      { id: "a1", at: at(50), action: "route.pause", target: "glm|", auth: "app", state: "applied" },
      { id: "a2", at: at(40), action: "route.rule.add", target: "rule-3", auth: "direction", harness: "claude_code", state: "applied" },
      { id: "a3", at: at(30), action: "route.cap.set", target: "grok|", auth: "app", state: "applied" },
      { id: "a4", at: at(20), action: "route.priority.set", target: "priority", auth: "app", state: "applied" },
      { id: "a5", at: at(10), action: "route.rule.retire", target: "rule-1", auth: "app", state: "applied" },
      { id: "a6", at: at(5), action: "ledger.add", target: "x", auth: "app", state: "applied" },
    ];
    await openPage({ actions: rows });
    const side = await screen.findByRole("complementary", { name: "Recent changes" });
    await within(side).findByText("Ended a rule");
    const text = side.textContent ?? "";
    expect(text).toContain("Ended a rule");
    expect(text).toContain("Changed your order");
    expect(text).toContain("Changed where agents stop on Grok");
    expect(text).toContain("Added a rule");
    expect(text).not.toContain("Paused GLM");
    expect(text).not.toContain("ledger");
    expect(text).toContain("You, in the app");
    expect(text).toContain("You, through Claude Code");
    for (const gone of ["harness", "hub", "capture", "episode", "job slot", "embedding", "gateway", "ledger", "owed", "tier", "hold", " cap", "override"]) {
      expect(text.toLowerCase()).not.toContain(gone);
    }
  });

  it("a change in Recent changes can be undone once with its own id", async () => {
    const row: OwnerAction = { id: "row1", at: new Date().toISOString(), action: "route.pause", target: "grok|", auth: "app", state: "applied" };
    await openPage({ actions: [row] });
    const undo = await screen.findByRole("button", { name: "Undo: Paused Grok" });
    fireEvent.click(undo);
    fireEvent.click(undo);
    await waitFor(() => expect(undos()).toHaveLength(1));
    expect(undos()[0].args).toEqual({ id: "row1" });
  });

  it("an undone change, and one that cannot be undone, offer no Undo", async () => {
    const at = new Date().toISOString();
    await openPage({
      actions: [
        { id: "u1", at, action: "route.pause", target: "glm|", auth: "app", state: "undone" },
        { id: "u2", at, action: "route.lift", target: "glm|", auth: "app", state: "applied", no_undo: "It was a lift." },
      ],
    });
    const side = await screen.findByRole("complementary", { name: "Recent changes" });
    await within(side).findByText(/undone/);
    expect(within(side).queryByRole("button", { name: /Undo/ })).toBeNull();
  });

  it("See all is there only when the Accounts place gives it somewhere to go", async () => {
    await openPage();
    expect(screen.queryByRole("button", { name: "See all" })).toBeNull();
    cleanup();
    const go = vi.fn();
    await openPage({}, go);
    fireEvent.click(screen.getByRole("button", { name: "See all" }));
    expect(go).toHaveBeenCalledTimes(1);
  });
});
