// Slice A3: `khipu_needs_you`, `khipu_needs_you_answer`, the owner-actions
// list and its undo, shaped as B5's notes give them
// (jobs/2026-11-10/b5-needs-you.md). Fictional; nothing real is answered.
// Switches (set by a dev state):
//   quiet          nothing waits; the last answer shows
//   needsMore      adds an item that can't be taken back, a sign-in, and a
//                  shortened item
//   needsChanged   the rule changes after the read, so its first answer is
//                  refused as changed
//   needsSlow      every answer takes four seconds
import type { FakeHandler } from "./index";

type Item = Record<string, unknown> & { key: string; expect: string | null };

// The shortened item's words whole, as `khipu_needs_you(key)` gives them.
const WHOLE_ASK = "A conversation asked to change the priority, and it is held until you answer. It asks: “Put Codex first until Friday, then Claude, then Claude second, then Grok for audits only, then Kimi for first reviews where the checks allow it, then GLM one job at a time, then DeepSeek only when every other subscription is held or out. Keep Muse for blind second opinions on the riskiest pull requests until its plan ends on 13 November, and never send privacy, sign-in, permission or money code to Grok, GLM, DeepSeek or Kimi.”";
type Line = Record<string, unknown> & { id: string };

const iso = (minAgo: number) => new Date(Date.now() - minAgo * 60000).toISOString();

function start(flags: Record<string, unknown>): Item[] {
  if (flags.quiet) return [];
  const items: Item[] = [
    {
      kind: "held", key: "held:req-0a1b2c3d4e5f", sort: "confirmation",
      words: "A conversation asked to let the “Khipu app mocks” conversation run ahead of pace on Claude until 6 pm, and it is held until you answer.",
      confirm_does: "Makes the change: it runs ahead until 6 pm.",
      undo_does: "Drops the held request. Nothing changes.", if_ignored: "It stays waiting until you answer.",
      settle: { confirm: { action: "direction.confirm", args: { request: "req-0a1b2c3d4e5f" } }, undo: { action: "direction.decline", args: { request: "req-0a1b2c3d4e5f" } } },
      asked_by: { who: "a Claude Code conversation in khipu", at: iso(12), when: "12 minutes ago" },
      from_words: "From a Claude Code conversation in khipu on M5.", mac: "M5", ago: "12 minutes ago", expect: null,
    },
    {
      kind: "rule", key: "rule:7", sort: "confirmation",
      words: "A conversation proposed a standing rule: “Send reviews of Claude’s work to Codex.”",
      confirm_does: "Keeps the rule. Agents follow it from their next message.",
      undo_does: "Drops the proposed rule. Nothing changes.", if_ignored: "It stays waiting until you answer.",
      settle: { confirm: { action: "route.rule.confirm", args: { rule: "7", expect: "rr-5f2a9c01" } }, undo: { action: "route.rule.undo", args: { rule: "7", expect: "rr-5f2a9c01" } } },
      asked_by: { who: "a Codex conversation in khipu", at: iso(60 * 20), when: "yesterday" },
      from_words: "From a Codex conversation in khipu on M1.", mac: "M1", ago: "yesterday", expect: "rr-5f2a9c01",
    },
  ];
  if (flags.needsMore) {
    items.push(
      {
        kind: "forget_send", key: "forget_send", sort: "confirmation", permanent: true,
        words: "You forgot 2 conversations on this Mac. The hub has not recorded 3 identifying marks for them yet, so a copy sent from another Mac might not be refused.",
        confirm_does: "Sends the forgets to the hub. This can't be undone: the hub keeps them forgotten.",
        undo_does: "No undo to choose: if you leave it, it stays waiting here.",
        settle: { confirm: { action: "forget.send", args: {} }, undo: null },
        asked_by: { who: "Khipu noticed this", at: iso(90), when: "2 hours ago" },
        from_words: "Khipu noticed this on M5.", mac: "M5", ago: "2 hours ago", expect: null,
      },
      {
        kind: "signin", key: "plan:Kimi", sort: "look",
        words: "Kimi is not signed in on this Mac, so Khipu can't read its plan here.",
        confirm_does: "Nothing to confirm.", undo_does: "Nothing to undo.", do: "Sign in with Kimi's own program on this Mac.",
        settle: { confirm: null, undo: null },
        asked_by: { who: "Khipu noticed this", at: null, when: null },
        from_words: "Khipu noticed this on M5.", mac: "M5", ago: null, expect: null,
      },
      {
        kind: "held", key: "held:req-9f8e7d6c5b4a", sort: "confirmation", cut: true, whole: WHOLE_ASK,
        words: "A conversation asked to change the priority, and it is held until you answer. It asks: “Put Codex first until Friday, then Claude, then Claude second, then Grok for audits only, then …”",
        confirm_does: "Makes the change: change the priority.", undo_does: "Drops the held request. Nothing changes.",
        settle: { confirm: { action: "direction.confirm", args: { request: "req-9f8e7d6c5b4a" } }, undo: { action: "direction.decline", args: { request: "req-9f8e7d6c5b4a" } } },
        asked_by: { who: "a Claude Code conversation in khipu", at: iso(30), when: "30 minutes ago" },
        from_words: "From a Claude Code conversation in khipu on M5.", mac: "M5", ago: "30 minutes ago", expect: null,
      },
    );
  }
  return items;
}

const ACTIONS0: Line[] = [
  { id: "c1d2e3f4", at: iso(60 * 24 * 5), action: "route.pause", target: "glm|", auth: "app", state: "applied" },
  { id: "d2e3f4a5", at: iso(60 * 24 * 6), action: "route.rule.add", target: "6", auth: "direction", harness: "claude_code", state: "applied", after: { n: 6 }, no_undo: "a standing rule added by an agent is undone by retiring it" },
  { id: "e3f4a5b6", at: iso(60 * 24 * 7), action: "route.cap.set", target: "grok|", auth: "app", state: "applied", mac: "M1" },
];

let items: Item[] | null = null;
let lines: Line[] = [];
let changedOnce = false;

function list(flags: Record<string, unknown>): Item[] {
  if (items === null) {
    items = start(flags);
    lines = ACTIONS0.map((l) => ({ ...l }));
  }
  return items;
}

const no = (code: string, error: string, extra: Record<string, unknown> = {}) => ({ ok: false, code, error, ...extra });

function answer(args: Record<string, unknown>, flags: Record<string, unknown>) {
  const all = list(flags);
  const key = String(args.key ?? "");
  const said = args.answer === "yes" ? "yes" : "no";
  if (flags.needsChanged && !changedOnce && key === "rule:7") {
    // Someone changed the rule after the app read it.
    changedOnce = true;
    const rule = all.find((it) => it.key === key);
    if (rule) {
      rule.words = "A conversation proposed a standing rule: “Send reviews of Claude’s work to Codex, and Codex’s work to Claude.”";
      rule.expect = "rr-77b0e412";
    }
  }
  const item = all.find((it) => it.key === key);
  if (!item) return no("not_found", "That item is no longer waiting. Nothing was changed; read the list again.");
  if (item.expect !== null && args.expect !== item.expect) {
    return no("changed", "This changed since you saw it, so nothing was changed. Look at it again, then answer.", { key });
  }
  if (item.kind === "signin") {
    if (said === "no") return no("no_action", "Nothing to say no to: the account stays signed out until you sign in.");
    return { ok: true, applied: false, signin_account: { label: "Kimi", harness: "kimi", home: null }, line: "Nothing was changed. Sign in with the account's own program." };
  }
  const settle = item.settle as { confirm: unknown; undo: unknown };
  if (!(said === "yes" ? settle.confirm : settle.undo)) return no("no_action", String(item.undo_does ?? "This item has no action to apply."));
  items = all.filter((it) => it.key !== key);
  if (item.permanent) {
    return { ok: true, key, answer: said, started: true, receipt: { text: "Started. It finishes in the background; Needs you says if it doesn't.", action_id: null, undoable: false, saved_on: ["M5"], not_reached: [] } };
  }
  const id = Math.random().toString(16).slice(2, 10).padEnd(8, "0");
  lines.unshift({ id, at: new Date().toISOString(), action: key === "rule:7" ? "route.rule.confirm" : "route.override.grant", target: key === "rule:7" ? "7" : "claude|", auth: "terminal", state: "applied" });
  const text = said === "yes" ? `You said yes. ${item.confirm_does}` : `You said no. ${item.undo_does}`;
  return { ok: true, key, answer: said, applied: said === "yes", receipt: { text, action_id: id, undoable: said === "yes", saved_on: ["M5"], not_reached: [] } };
}

function read(flags: Record<string, unknown>, key?: unknown) {
  const all = list(flags);
  if (typeof key === "string") {
    const one = all.find((it) => it.key === key);
    if (!one) return { ok: false, code: "not_found", key, error: "That item is no longer waiting. Nothing was changed; read the list again." };
    const { whole: words, ...rest } = one;
    return { ok: true, key, total: 1, matching: 1, this_mac: "M5", items: [{ ...rest, words: words ?? one.words, cut: false }] };
  }
  const out: Record<string, unknown> = {
    ok: true, total: all.length, matching: all.length, offset: 0, counts: {}, items: all.map(({ whole: _whole, ...it }) => ({ cut: false, ...it })), more: 0,
    this_mac: "M5",
  };
  if (!all.length) {
    out.last_answer = { text: "You said yes: let a conversation run ahead of pace.", at: iso(60 * 24 * 2), ago: "2 days ago", action_id: "c1d2e3f4", undoable: true };
  }
  return out;
}

const later = <T,>(value: T, ms: number) => new Promise<T>((r) => setTimeout(() => r(value), ms));

export const needsFakes: FakeHandler = (cmd, args, flags) => {
  switch (cmd) {
    case "khipu_needs_you":
      return read(flags, args?.key);
    case "khipu_needs_you_answer": {
      const out = answer(args ?? {}, flags);
      return flags.needsSlow ? later(out, 4000) : out;
    }
    case "khipu_account_sign_in":
      return later({ ok: true }, 1500);
    case "khipu_owner_direction_actions":
      list(flags);
      return { ok: true, actions: lines };
    case "khipu_owner_direction_undo": {
      const line = lines.find((l) => l.id === args?.id);
      if (!line) return { ok: false, code: "not_found", error: "There is no such change." };
      line.state = "undone";
      return { ok: true, undid: line.id };
    }
    default:
      return undefined;
  }
};
