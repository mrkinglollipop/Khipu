/* Needs you, Decide band (docs/plans/2026-11-03-settings-reorg.md, slice A; mock
   docs/mockups/2026-11-03-settings/index.html): each change a conversation
   made that waits for the owner, as one card. What it is, before and now, who
   asked, what happens if he does nothing, and buttons that name the result.
   Pure, and the one source of these words: Settings and Home's Confirm both
   read it. Sentences here never carry an id, a setting's key or raw JSON.

   feat/needs-you-in-conversation builds the same read for conversations in
   packages/cli/khipu/needs_you.py; its "It said you asked" and "stays waiting"
   wording is matched here. Unify the two once it lands. */

import { capitalise, saidWhen } from "./routing";
import type { RouteAuthor, RoutePriority, RouteProposal, RouteRule, RouteView } from "./routing";
import { priorityWords, proposalAuthor } from "./routingProposals";
import { durationWords } from "./routingWords";

/** A conversation's change to a routing setting at your request, made and
 *  marked until you confirm it or undo it (the CLI's ``request``). */
export type SettingRequest = { request?: string | null; by?: RouteAuthor | null; by_words?: string | null; at?: string | null; prior?: unknown };
export type RouteSetting = {
  value: unknown;
  default: unknown;
  kind: string;
  source: string;
  words?: string;
  request?: SettingRequest | null;
  /** A conversation's change is waiting for the owner; `value` is still the live state. */
  pending?: boolean;
};

export type DecideTarget =
  | { type: "priority" }
  | { type: "rule"; rule: RouteRule | null; proposal: RouteProposal | null }
  | { type: "setting"; key: string }
  | { type: "accounts" };

export type DecideCard = {
  /** Its `data-route-item`. */
  key: string;
  target: DecideTarget;
  title: string;
  before: { cap: string; text: string } | null;
  now: { cap: string; text: string } | null;
  /** "A Codex conversation in project-c, today at 12:51 PM". */
  who: string;
  /** What the conversation says he asked, word for word. */
  asked: string | null;
  ifIgnored: string;
  /** The button that keeps the change; null when it can't be kept here. */
  keep: string | null;
  /** The button that takes it back; null when nothing can be done here. */
  back: string | null;
  edit: string | null;
};

const WITHHELD = "Its words stay on the Mac it came from, so they can't be shown here. Keep it on that Mac if you want it.";
const STAYS_WAITING = "this stays here until you answer";

function whoWords(author: string, at: string | null | undefined, now: Date): string {
  const when = at ? saidWhen(at, now) : "";
  // saidWhen says "at 2:52 PM" for today, which reads as unfinished after a comma.
  return capitalise(author) + (when ? `, ${when.startsWith("at ") ? `today ${when}` : when}` : "");
}

/** "Nov 10": when a proposal ends by itself, seven days after it was written. */
function endsDay(at: string | null | undefined): string | null {
  if (!at) return null;
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return null;
  return new Date(d.getTime() + 7 * 24 * 3600 * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
}

function liveProposal(view: RouteView, match: (p: RouteProposal) => boolean): RouteProposal | null {
  return Object.values(view.proposals ?? {}).find((p) => p.status === "live" && match(p)) ?? null;
}

function priorityTitle(p: RoutePriority): string {
  const old = p.replaced?.entries ?? [];
  if (p.proposal_withheld) return "A conversation on another Mac changed your order of accounts";
  if (!old.length) return "A conversation set your order of accounts";
  const first = p.entries[0];
  if (first && first.account !== old[0].account) return `A conversation put ${first.label} ahead of ${old[0].label}`;
  return "A conversation changed your order of accounts";
}

/** The priority a conversation set, as Settings and Home's Confirm both say it. */
export function priorityCard(view: RouteView, now: Date): DecideCard | null {
  const p = view.priority;
  if (!p?.pending) return null;
  const proposal = liveProposal(view, (x) => x.kind === "priority");
  const had = !!p.replaced?.entries.length;
  const author = p.pending_author?.project
    ? proposalAuthor(p.pending_author)
    : proposal?.author
      ? proposalAuthor(proposal.author)
      : (p.pending_by || "a conversation");
  const day = endsDay(proposal?.at);
  return {
    key: "needs:priority",
    target: { type: "priority" },
    title: priorityTitle(p),
    before: { cap: "Your order before", text: priorityWords(p.replaced?.entries, now) ?? "You had no order set." },
    now: { cap: "The order in use now", text: p.proposal_withheld ? WITHHELD : (priorityWords(p.entries, now) ?? "") },
    who: whoWords(author, proposal?.at ?? p.at, now),
    asked: p.proposal_withheld ? null : (p.owner_request ?? null),
    ifIgnored: day
      ? `The new order stays in use until ${day}, then ${had ? "your order comes back" : "it ends: you had no order before"}.`
      : `The new order stays in use, and ${STAYS_WAITING}.`,
    keep: p.proposal_withheld ? null : "Keep the new order",
    back: had ? "Go back to my order" : "Remove this order",
    edit: null,
  };
}

function ruleCard(rule: RouteRule, proposal: RouteProposal | null, now: Date): DecideCard {
  const changed = rule.pending_kind !== "added" && !!rule.prior_text;
  const named = typeof rule.n === "number" ? `your rule ${rule.n}` : "one of your rules";
  const withheld = !!rule.proposal_withheld;
  const day = endsDay(proposal?.at);
  const author = proposal?.author ?? proposal?.written_by;
  const words = author ? proposalAuthor(author) : (rule.pending_by || "a conversation");
  return {
    key: `needs:rule:${rule.entity ?? rule.n}`,
    target: { type: "rule", rule, proposal },
    title: withheld
      ? "A conversation on another Mac changed a rule"
      : changed ? `A conversation reworded ${named}` : "A conversation added a rule",
    before: changed ? { cap: "Your words", text: rule.prior_text! } : null,
    now: { cap: changed ? "The words in use now" : "The rule in use now", text: withheld ? WITHHELD : rule.text },
    who: whoWords(words, proposal?.at ?? rule.at, now),
    asked: null,
    ifIgnored: day
      ? changed
        ? `The new words stay in use until ${day}, then your words come back.`
        : `This rule stays in use until ${day}, then it goes away.`
      : `It stays in use, and ${STAYS_WAITING}.`,
    keep: withheld ? null : changed ? "Keep the new words" : "Keep this rule",
    back: changed ? "Keep my words" : "Remove it",
    edit: withheld ? null : changed ? "Edit them first" : "Edit it first",
  };
}

/** A live proposal whose rule isn't in this read: it can't be answered yet. */
function missingRuleCard(proposal: RouteProposal, now: Date): DecideCard {
  return {
    key: `needs:rule:${proposal.entity}`,
    target: { type: "rule", rule: null, proposal },
    title: "A conversation proposed a change to a rule",
    before: null,
    now: { cap: "Not in this read", text: "That rule isn't in this read, so this can't be answered here. Read the routing record again, then try again." },
    who: whoWords(proposalAuthor(proposal.author ?? proposal.written_by), proposal.at, now),
    asked: null,
    ifIgnored: "It ends by itself seven days after it was made.",
    keep: null,
    back: null,
    edit: null,
  };
}

const plural = (n: number, one: string, many: string) => `${n.toLocaleString()} ${n === 1 ? one : many}`;

function nameOf(item: unknown): string | null {
  if (typeof item === "string") return item;
  if (item && typeof item === "object") {
    const o = item as Record<string, unknown>;
    for (const k of ["label", "account", "name", "path"]) if (typeof o[k] === "string" && o[k]) return o[k] as string;
  }
  return null;
}

/** A setting's value in words, by its kind: never its stored form. */
export function settingValueWords(kind: string, value: unknown, words?: string): string {
  if (kind === "switch") return value === true ? "On" : "Off";
  if (value === null || value === undefined || value === "") return "Not set";
  if (typeof value === "number") {
    const zero = /\(0: ([^)]*)\)\s*$/.exec(words ?? "");
    if (value === 0 && zero) return capitalise(zero[1]);
    return kind === "duration" ? durationWords(value) : value.toLocaleString();
  }
  if (Array.isArray(value)) {
    if (!value.length) return "None";
    if (kind === "durations" && value.every((v) => typeof v === "number")) return (value as number[]).map(durationWords).join(", ");
    const names = value.map(nameOf);
    return names.every((n): n is string => !!n) ? names.join(", ") : plural(value.length, "item", "items");
  }
  if (typeof value === "object") {
    if (kind === "lane_map") {
      const n = Object.values(value as Record<string, unknown>)
        .reduce<number>((sum, lanes) => sum + (lanes && typeof lanes === "object" ? Object.keys(lanes).length : 0), 0);
      return n ? `${plural(n, "job slot", "job slots")} matched to an account` : "No job slots matched";
    }
    const n = Object.keys(value).length;
    return n ? plural(n, "item", "items") : "None";
  }
  return String(value);
}

/** The settings whose change isn't in use until the owner answers: the CLI
 *  keeps the value it replaced live while one waits (`settings_status`,
 *  `usage_logins.login_switch`). */
const LOGIN_SWITCH = /^routing_usage_login_/;
const REPLICATION = "routing_replication_live";

function settingCard(key: string, s: RouteSetting, now: Date): DecideCard {
  const name = (s.words ?? "a routing setting").replace(/\s*\(0: [^)]*\)\s*$/, "").replace(/\bjob lane\b/, "job slot");
  const quoted = s.words ? `“${name}”` : name;
  const request = s.request ?? {};
  const author = request.by?.project ? proposalAuthor(request.by) : (request.by_words || "a conversation");
  const base = {
    key: `setting:${key}`,
    target: { type: "setting", key } as DecideTarget,
    who: whoWords(author, request.at, now),
    asked: request.request ?? null,
    edit: null,
  };
  const prior = request.prior ?? s.default;
  // The login switch's asked-for value isn't in the read, only the one in use.
  if (LOGIN_SWITCH.test(key)) {
    return {
      ...base,
      title: `A conversation asked to change ${quoted}`,
      before: null,
      now: { cap: "In use now", text: settingValueWords(s.kind, s.value, s.words) },
      ifIgnored: `It stays as it is, and ${STAYS_WAITING}. The conversation's change isn't in use until you accept it.`,
      keep: "Accept the change",
      back: "Leave it as it is",
    };
  }
  // Asked on: it stays off until he says yes (a waiting word never shows on).
  if (key === REPLICATION && prior !== true) {
    return {
      ...base,
      title: `A conversation asked to turn on ${quoted}`,
      before: null,
      now: { cap: "In use now", text: "Off" },
      ifIgnored: `It stays off, and ${STAYS_WAITING}.`,
      keep: "Turn it on",
      back: "Leave it off",
    };
  }
  const was = settingValueWords(s.kind, prior, s.words);
  const is = settingValueWords(s.kind, s.value, s.words);
  if (s.kind === "switch") {
    const on = s.value === true;
    return {
      ...base,
      title: `A conversation turned ${on ? "on" : "off"} ${quoted}`,
      before: { cap: "Before", text: was },
      now: { cap: "In use now", text: is },
      ifIgnored: `It stays ${on ? "on" : "off"}, and ${STAYS_WAITING}.`,
      keep: `Keep it ${on ? "on" : "off"}`,
      back: `Turn it back ${on ? "off" : "on"}`,
    };
  }
  const short = typeof prior === "number" || s.kind === "number";
  return {
    ...base,
    title: `A conversation changed ${quoted}`,
    before: { cap: "Before", text: was },
    now: { cap: "In use now", text: is },
    ifIgnored: `The new value stays in use, and ${STAYS_WAITING}.`,
    keep: "Keep the new value",
    back: short ? `Go back to ${was.charAt(0).toLowerCase()}${was.slice(1)}` : "Go back to the old value",
  };
}

/** Each change a conversation made to the routing record or a routing setting,
 *  oldest kinds first: the priority, then rules, then settings. */
export function decideCards(view: RouteView | null, routing: Record<string, RouteSetting> | null | undefined, now: Date): DecideCard[] {
  const out: DecideCard[] = [];
  if (view) {
    const priority = priorityCard(view, now);
    if (priority) out.push(priority);
    const rules = view.rules ?? [];
    const seen = new Set<RouteRule>();
    for (const p of Object.values(view.proposals ?? {})) {
      if (p.status !== "live" || p.kind !== "rule") continue;
      const rule = rules.find((r) => r.entity === p.entity) ?? rules.find((r) => r.n === p.n) ?? null;
      if (rule) seen.add(rule);
      out.push(rule ? ruleCard(rule, p, now) : missingRuleCard(p, now));
    }
    for (const r of rules) if (r.pending && !seen.has(r)) out.push(ruleCard(r, null, now));
  }
  for (const [key, s] of Object.entries(routing ?? {})) if (s.request) out.push(settingCard(key, s, now));
  return out;
}

type AccountsRead = { value?: unknown; none?: boolean; accounts?: unknown[]; request?: unknown };

function accountNames(list: unknown[]): string[] {
  return list.map((e) => {
    const name = nameOf(e);
    if (name) return name;
    const harness = (e as { harness?: unknown } | null)?.harness;
    return typeof harness === "string" && harness ? capitalise(harness) : "An account";
  });
}

function accountsAuthor(by: unknown): string {
  if (typeof by === "string" && by) return by;
  if (by && typeof by === "object") {
    const label = (by as { label?: unknown }).label;
    if (typeof label === "string" && label) return label;
    return proposalAuthor(by as RouteAuthor);
  }
  return "a conversation";
}

/** A conversation's change to the accounts Khipu reads; null when none waits. */
export function accountsCard(status: AccountsRead | null | undefined, now: Date): DecideCard | null {
  const request = status?.request as { request?: string | null; by?: unknown; at?: string | null; prior?: unknown; prior_none?: unknown } | null | undefined;
  if (!status || !request || typeof request !== "object") return null;
  const was = Array.isArray(request.prior) ? accountNames(request.prior) : null;
  const is = status.none ? [] : accountNames(status.accounts ?? []);
  const added = was ? is.filter((a) => !was.includes(a)) : [];
  const stopped = was ? was.filter((a) => !is.includes(a)) : [];
  const title = added.length === 1 && !stopped.length
    ? `A conversation added ${added[0]} to the accounts Khipu reads`
    : stopped.length === 1 && !added.length
      ? `A conversation stopped Khipu reading ${stopped[0]}`
      : "A conversation changed which accounts Khipu reads";
  const beforeText = was
    ? was.join(", ") || "No accounts"
    : request.prior_none === true ? "No accounts" : "The accounts Khipu finds by itself";
  return {
    key: "needs:accounts",
    target: { type: "accounts" },
    title,
    before: { cap: "Before", text: beforeText },
    now: { cap: "In use now", text: is.join(", ") || "No accounts" },
    who: whoWords(accountsAuthor(request.by), request.at, now),
    asked: request.request ?? null,
    ifIgnored: `The new list stays in use, and ${STAYS_WAITING}.`,
    keep: "Keep the new list",
    back: "Go back to the old list",
    edit: null,
  };
}
