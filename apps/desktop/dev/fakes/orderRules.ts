// Accounts > Order and rules in the dev preview (slice A6): the routing record
// as `khipu route --json` prints it (accounts, priority, rules), the owner log
// as `owner-direction actions --json` prints it, and `route say` for a handful
// of phrases, answering in the exact JSON slice B6 documents. Fictional.
// Nothing here is real and nothing is written anywhere.
//
// Switches on window.__khipuFake (set by the dev state, or in the console):
//   orderRules: true            turn this file on (otherwise it passes)
//   sayRefusal: "changed" | "no_such_preview" | "already_used" | "other"
//                               what the next Save is refused with
//   sayOwnerOnly: true          every preview and apply is refused as `owner_only`
import type { FakeHandler } from "./index";

const iso = (min: number) => new Date(Date.now() - min * 60000).toISOString();
const ahead = (days: number) => new Date(Date.now() + days * 86400000).toISOString();

type Entry = { account: string; label: string; role?: string | null; until?: string | null; exceptions?: { text: string; until: string }[] };
type Rule = { n: number; text: string; entity: string; revision: string };
type Action = { id: string; at: string; action: string; target: string; auth: string; harness?: string; state: string | null; no_undo?: string };

const ACCOUNTS = ["Claude", "Codex", "Claude second", "Grok", "Kimi", "GLM", "DeepSeek"];
const keyOf = (label: string) => `${label.toLowerCase().replace(/\s+/g, "-")}|`;
const entriesOf = (labels: string[]): Entry[] => labels.map((label) => ({ account: keyOf(label), label }));

const state = {
  order: entriesOf(ACCOUNTS),
  rules: [
    { n: 1, text: "Send reviews of Claude’s work to Codex.", entity: "rule-1", revision: "r1" },
    { n: 2, text: "Keep Grok away from sign-in, privacy and money code.", entity: "rule-2", revision: "r1" },
    { n: 3, text: "Use Muse only for a second opinion on the riskiest changes.", entity: "rule-3", revision: "r1" },
    { n: 4, text: "Grok stops at 90% of its week so Grok Bot keeps room.", entity: "rule-4", revision: "r1" },
  ] as Rule[],
  actions: [
    { id: "a3", at: iso(60 * 24 * 7), action: "route.cap.set", target: "grok|", auth: "app", state: "applied" },
    { id: "a2", at: iso(60 * 24 * 6), action: "route.rule.add", target: "rule-3", auth: "direction", harness: "claude_code", state: "applied" },
    { id: "a1", at: iso(60 * 24 * 5), action: "route.pause", target: "glm|", auth: "app", state: "applied" },
  ] as Action[],
  undo: {} as Record<string, { order: Entry[]; rules: Rule[] }>,
  pending: {} as Record<string, { kind: string; text: string; words: string; order?: Entry[]; rules?: Rule[]; action: string; target: string }>,
  used: new Set<string>(),
  counter: 0,
};

const labelsIn = (words: string) => ACCOUNTS.filter((a) => new RegExp(`\\b${a}\\b`, "i").test(words) && !(a === "Claude" && /claude second/i.test(words)));
const orderLine = (e: Entry[]) => e.map((x, i) => `${i + 1} ${x.label}`).join(", ");
const refusal = (code: string, error: string, extra: Record<string, unknown> = {}) => ({ ok: false, code, error, ...(code === "not_understood" ? { reason: error } : {}), ...extra });

function preview(words: string) {
  const w = words.trim();
  const found = labelsIn(w);
  const token = `fake${String(++state.counter).padStart(28, "0")}`;
  const expires_at = new Date(Date.now() + 10 * 60000).toISOString();
  const base = { ok: true, token, expires_at, ends: null as string | null, ends_words: null as string | null };
  const apply = { command: "route say", args: ["--from-app", "--apply", token] };

  if (/\b(first|top)\b/i.test(w) && !/\bfor (code|review|plan|doc)/i.test(w)) {
    if (found.length !== 1) return refusal("not_understood", found.length ? `That names more than one account (${found.join(" and ")}), so I'm not guessing which to put first. Say it once for each.` : "I couldn't tell which account you mean. Say its name: " + ACCOUNTS.slice(0, 4).join(", ") + ".", { considered: found });
    const label = found[0];
    const first = [...state.order.filter((e) => e.label === label), ...state.order.filter((e) => e.label !== label)];
    const timed = /\buntil\b/i.test(w);
    if (timed) {
      const end = ahead(3);
      const endWords = new Date(end).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
      state.pending[token] = { kind: "priority_first_until", text: `${label} goes first until ${endWords}. Then the order goes back.`, words: w, order: state.order.map((e) => (e.label === label ? { ...e, exceptions: [{ text: `${label} goes first.`, until: end }] } : e)), action: "route.priority.amend", target: keyOf(label) };
      return { ...base, understood: `Put ${label} first until ${endWords}`, kind: "priority_first_until", fallback: false, account: label, now: orderLine(state.order), after: `${label} goes first until ${endWords}, then the order goes back to: ${orderLine(state.order)}`, ends: end, ends_words: endWords, apply };
    }
    state.pending[token] = { kind: "priority_first", text: `${label} is first in your order.`, words: w, order: first, action: "route.priority.set", target: "priority" };
    return { ...base, understood: `Put ${label} first`, kind: "priority_first", fallback: false, account: label, now: orderLine(state.order), after: orderLine(first), apply };
  }
  if (/\bend (the )?rule\b/i.test(w)) {
    const n = Number((/rule (\d+)/i.exec(w) ?? [])[1]);
    const rule = state.rules.find((r) => r.n === n);
    if (!rule) return refusal("not_understood", `You have no rule ${n || "like that"}.`, { reason: `You have no rule ${n || "like that"}.`, considered: state.rules.map((r) => `rule ${r.n}`) });
    state.pending[token] = { kind: "end_rule", text: `Ended rule ${n}: ${rule.text}`, words: w, rules: state.rules.filter((r) => r.n !== n), action: "route.rule.retire", target: rule.entity };
    return { ...base, understood: `End rule ${n}`, kind: "end_rule", fallback: false, account: null, now: `Rule ${n}: ${rule.text}`, after: `Rule ${n} ends. Undo brings it back.`, apply };
  }
  if (/\b(pause|stop using|don't use)\b/i.test(w) && !/\bwhen\b/i.test(w)) {
    return refusal("not_understood", found.length > 1 ? `That names more than one account (${found.join(" and ")}), so I'm not guessing which one. Say it once for each.` : "Every pause ends by itself, and I didn't hear when. Say it with an end, for example \"pause Grok until Monday\" or \"pause Grok for 2 hours\".", { considered: found });
  }
  // Anything else is the fallback: the words become a rule.
  const n = state.rules.length + 1;
  const rules = [...state.rules, { n, text: w, entity: `rule-${n}`, revision: "r1" }];
  state.pending[token] = { kind: "add_rule", text: `Added the rule: ${w}`, words: w, rules, action: "route.rule.add", target: `rule-${n}` };
  return { ...base, understood: "Add it as a rule in your words", kind: "add_rule", fallback: true, account: null, now: `${state.rules.length} rules`, after: `${rules.length} rules, including: ${w}`, apply };
}

function apply(token: string, flags: Record<string, unknown>) {
  const plan = state.pending[token];
  if (state.used.has(token)) return refusal("already_used", "That change was already saved. Preview it again to make it again.");
  if (!plan) return refusal("no_such_preview", "That preview has expired, or Khipu didn't make it. Preview the change again.");
  const r = flags.sayRefusal;
  if (r === "changed") { state.used.add(token); return refusal("changed", "This changed since you saw it, so nothing was changed. Preview it again."); }
  if (r === "no_such_preview") return refusal("no_such_preview", "That preview has expired, or Khipu didn't make it. Preview the change again.");
  if (r === "already_used") { state.used.add(token); return refusal("already_used", "That change was already saved. Preview it again to make it again."); }
  if (r === "other") { state.used.add(token); return refusal("rules_full", "You already have 40 rules, which is the most Khipu keeps. End one first."); }
  state.used.add(token);
  const id = `n${++state.counter}`;
  state.undo[id] = { order: state.order, rules: state.rules };
  if (plan.order) state.order = plan.order;
  if (plan.rules) state.rules = plan.rules;
  state.actions.unshift({ id, at: new Date().toISOString(), action: plan.action, target: plan.target, auth: "app", state: "applied" });
  return { ok: true, applied: true, kind: plan.kind, action_id: id, receipt: { text: plan.text, action_id: id, undoable: true, saved_on: ["this Mac"], not_reached: [] } };
}

export const orderRulesFakes: FakeHandler = (cmd, args, flags) => {
  if (!flags.orderRules) return undefined;
  switch (cmd) {
    case "khipu_route_view":
      return {
        record: true, this_mac: "M5", version: 14, checked_at: new Date().toISOString(), priority_version: "p5",
        accounts: ACCOUNTS.map((label) => ({ key: keyOf(label), label, harness: label.split(" ")[0].toLowerCase(), state: "open" })),
        holds: [], priority: { entries: state.order, version: "p5" }, rules: state.rules,
      };
    case "khipu_owner_direction_actions":
      return { ok: true, actions: state.actions };
    case "khipu_owner_direction_undo": {
      const id = String(args?.id);
      const before = state.undo[id];
      if (before) { state.order = before.order; state.rules = before.rules; delete state.undo[id]; }
      const row = state.actions.find((a) => a.id === id);
      if (row) row.state = "undone";
      return { ok: true };
    }
    case "khipu_route_say": {
      if (flags.sayOwnerOnly) return refusal("owner_only", "Only the Khipu app can change this. Say it in the app, or ask the app to make the change.");
      const mode = String(args?.mode);
      const value = String(args?.value ?? "");
      return mode === "preview" ? preview(value) : apply(value, flags);
    }
    default:
      return undefined;
  }
};
