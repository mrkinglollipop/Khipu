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
import { bytesWords, durationWords } from "./routingWords";

/** A conversation's change to a routing setting at your request, made and
 *  marked until you confirm it or undo it (the CLI's ``request``). */
export type SettingRequest = {
  request?: string | null;
  by?: RouteAuthor | null;
  by_words?: string | null;
  at?: string | null;
  prior?: unknown;
  /** The value the conversation asked for, as stored; `asked_set` false: it
   *  asked for the default. Both absent from an older CLI's read. */
  asked?: unknown;
  asked_set?: boolean;
};
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
  /** The difference in a line, where comparing the two boxes by eye is work. */
  changed: { cap: string; text: string } | null;
  ifIgnored: string;
  /** The button that keeps the change; null when it can't be kept here. */
  keep: string | null;
  /** The button that takes it back; null when nothing can be done here. */
  back: string | null;
  /** The second step before `back`, when taking it back drops the conversation's
   *  words for good; null when it can simply be set again. */
  backAsk: string | null;
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

const ordinal = (n: number) => {
  const tens = n % 100;
  if (tens >= 11 && tens <= 13) return `${n}th`;
  return `${n}${["th", "st", "nd", "rd"][n % 10] ?? "th"}`;
};

/** The entries that keep their order against the old list: whatever is left
 *  out moved. Longest increasing run of old places, so one move reads as one. */
function keptInOrder(places: number[]): Set<number> {
  const best = places.map(() => 1);
  const from = places.map(() => -1);
  places.forEach((place, i) => {
    for (let j = 0; j < i; j += 1) {
      if (places[j] < place && best[j] + 1 > best[i]) {
        best[i] = best[j] + 1;
        from[i] = j;
      }
    }
  });
  const kept = new Set<number>();
  let at = best.indexOf(Math.max(0, ...best));
  while (at >= 0) {
    kept.add(at);
    at = from[at];
  }
  return kept;
}

/** "Codex moved to 1st; Grok added; Muse removed": what a new order changes,
 *  so a long list needn't be compared by eye. Null when there is nothing to
 *  compare against. */
export function priorityChanges(p: RoutePriority): string | null {
  const old = p.replaced?.entries ?? [];
  if (!old.length || p.proposal_withheld) return null;
  const now = p.entries;
  const out: string[] = [];
  const inOld = now.filter((e) => old.some((o) => o.account === e.account));
  const kept = keptInOrder(inOld.map((e) => old.findIndex((o) => o.account === e.account)));
  inOld.forEach((e, i) => {
    if (!kept.has(i)) out.push(`${e.label} moved to ${ordinal(now.indexOf(e) + 1)}`);
  });
  for (const e of now) if (!old.some((o) => o.account === e.account)) out.push(`${e.label} added as ${ordinal(now.indexOf(e) + 1)}`);
  for (const o of old) if (!now.some((e) => e.account === o.account)) out.push(`${o.label} removed`);
  for (const e of inOld) {
    const o = old.find((x) => x.account === e.account)!;
    if ((o.role ?? "") !== (e.role ?? "")) out.push(`${e.label}'s role reworded`);
    if ((o.until ?? null) !== (e.until ?? null)) out.push(`${e.label}'s end date changed`);
  }
  return out.length ? out.join("; ") : null;
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
    changed: (() => {
      const text = priorityChanges(p);
      return text ? { cap: "What changed", text } : null;
    })(),
    ifIgnored: day
      ? `The new order stays in use until ${day}, then ${had ? "your order comes back" : "it ends: you had no order before"}.`
      : `The new order stays in use, and ${STAYS_WAITING}.`,
    keep: p.proposal_withheld ? null : "Keep the new order",
    back: had ? "Go back to my order" : "Remove this order",
    backAsk: had
      ? "Go back to your order? The new order is dropped, and the conversation that set it is told."
      : "Remove this order? You will have no order set, and the conversation that set it is told.",
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
    changed: null,
    ifIgnored: day
      ? changed
        ? `The new words stay in use until ${day}, then your words come back.`
        : `This rule stays in use until ${day}, then it goes away.`
      : `It stays in use, and ${STAYS_WAITING}.`,
    keep: withheld ? null : changed ? "Keep the new words" : "Keep this rule",
    back: changed ? "Keep my words" : "Remove it",
    backAsk: changed
      ? "Keep your words? The conversation's words are dropped, and it is told."
      : "Remove this rule? Nothing is added, and the conversation that proposed it is told.",
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
    changed: null,
    ifIgnored: "It ends by itself seven days after it was made.",
    keep: null,
    back: null,
    backAsk: null,
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

type Unit = readonly [one: string, many: string];
const CHARS: Unit = ["character", "characters"];
const MS: Unit = ["millisecond", "milliseconds"];

/** Every routing setting as a card names it: a phrase that fits "A conversation
 *  changed …" or "turned on …", what its number counts, and what 0 means. The
 *  CLI's `SETTINGS` words are written for a row with a label beside them; a test
 *  fails when a setting there has no entry here. */
const SETTING_WORDS: Record<string, { name: string; unit?: Unit; zero?: string; format?: (n: number) => string }> = {
  routing_stale_after: { name: "how long until a report counts as out of date", zero: "Never" },
  routing_report_default_limit: { name: "how long a reported limit lasts when the report gives no time" },
  routing_conversation_hold: { name: "how long a conversation's temporary instruction lasts" },
  routing_conversation_hold_max: { name: "the longest temporary instruction a conversation can set", zero: "No maximum" },
  routing_hold_chars: { name: "how long the words of a temporary instruction can be", unit: CHARS, zero: "No limit" },
  routing_owner_hold_reminder: { name: "when Khipu reminds you about your own temporary instructions", zero: "Never" },
  routing_disagreement_warn_after: { name: "how long two reports can disagree before Khipu warns you", zero: "Never" },
  routing_summary_chars: { name: "how long the summary at the start of a conversation can be, not counting your rules and note", unit: CHARS, zero: "No limit" },
  routing_line_chars: { name: "how long one line of routing news can be, not counting your rules and note", unit: CHARS, zero: "No limit" },
  routing_rule_chars: { name: "how long one of your rules can be", unit: CHARS, zero: "No limit" },
  routing_rules_max: { name: "how many rules you can have", unit: ["rule", "rules"], zero: "No limit" },
  routing_note_chars: { name: "how long your note can be", unit: CHARS, zero: "No limit" },
  routing_rules_chars: { name: "how much of your rules, note and order a conversation sees when it starts", unit: CHARS, zero: "No limit" },
  routing_rules_seed: { name: "the file your rules were first copied from" },
  routing_lane_map: { name: "which account a job slot uses" },
  routing_sources: { name: "your job runner folders" },
  routing_table: { name: "your old routing file" },
  routing_runner_silent_intervals: { name: "how many missed check-ins mean a job runner has stopped", unit: ["missed check-in", "missed check-ins"] },
  routing_runner_tail_bytes: { name: "how much of a job runner's log Khipu reads", format: bytesWords, zero: "All of it" },
  routing_runner_limited_lines: { name: "how many limit lines Khipu keeps from each job runner", unit: ["line", "lines"], zero: "No limit" },
  routing_push_start: { name: "routing news at the start of a conversation" },
  routing_push_prompt: { name: "routing news before each prompt" },
  routing_push_delegation: { name: "routing news before a conversation hands work to a helper" },
  routing_dispatch_scripts: { name: "your scripts that hand work to a helper" },
  routing_delivery_live_after: { name: "how long a quiet conversation still counts as running" },
  routing_delivery_slack: { name: "how far behind a conversation can fall before Khipu warns you", unit: ["change", "changes"] },
  routing_ledger_keep_days: { name: "how long Khipu remembers what an unused conversation has heard", unit: ["day", "days"], zero: "Forever" },
  routing_delivery_lease: { name: "how long Khipu waits for a conversation to say it heard a change" },
  routing_prompt_chars: { name: "how long the update before a prompt can be, not counting your rules and note", unit: CHARS, zero: "No limit" },
  routing_child_chars: { name: "how long the routing news for a helper can be", unit: CHARS, zero: "No limit" },
  routing_prompt_budget_ms: { name: "how long Khipu may take before a prompt", unit: MS },
  routing_start_budget_ms: { name: "how long Khipu may take at the start of a conversation", unit: MS },
  routing_resend_after_versions: { name: "how many changes before a conversation is sent everything again", unit: ["change", "changes"], zero: "Never" },
  routing_resend_after: { name: "how long before a conversation is sent everything again", zero: "Never" },
  routing_not_refreshed_after: { name: "how long before Khipu says the routing record isn't up to date", zero: "Never" },
  routing_poll_interval: { name: "how often Khipu looks at job runner folders and your old routing file" },
  routing_stale_view_cap: { name: "how old a saved record can be before a prompt says routing is updating", zero: "Never" },
  routing_slow_read_pct: { name: "how many slow reads before a prompt Khipu allows before warning you", format: (n) => `${n.toLocaleString()}% of prompts` },
  routing_reset_check: { name: "checking an account at its reset time" },
  routing_reset_check_wait: { name: "how long Khipu waits after a reset before checking" },
  routing_reset_check_retries: { name: "when Khipu checks again after a reset" },
  routing_busy_load: { name: "how busy the Mac must be before Khipu waits", format: (n) => `A load of ${n.toLocaleString()}`, zero: "Never wait" },
  routing_busy_wait: { name: "how long Khipu waits while the Mac is busy" },
  routing_busy_tries: { name: "how many times Khipu waits while the Mac is busy", unit: ["time", "times"] },
  routing_pace_order: { name: "listing accounts by pace when you have no order" },
  routing_priority_unconfirmed_warn_after: { name: "how long your order can wait for an answer before Khipu warns you", zero: "Never" },
  routing_cloud_copy: { name: "the copy for cloud agents" },
  routing_cloud_copy_hold_words: { name: "temporary instruction words in the copy for cloud agents" },
  routing_cloud_copy_rules: { name: "your rules and note in the copy for cloud agents" },
  routing_proposal_text_between_macs: { name: "sharing what conversations propose between your Macs" },
  routing_replication_live: { name: "copying routing between your Macs" },
  routing_cloud_copy_stale_after: { name: "how long until the copy for cloud agents counts as out of date" },
  routing_cloud_copy_debounce: { name: "how long Khipu waits for a burst of changes to end before copying" },
  routing_usage_login_grok: { name: "reading Grok's usage from its own login" },
  routing_usage_login_cursor: { name: "reading Cursor's usage from its login in the Keychain" },
  routing_usage_login_opencode: { name: "reading OpenCode Go's usage from its own login" },
  routing_mac_names: { name: "the names of your Macs" },
};

/** Which setting keys have words, for the test that holds this to the CLI's list. */
export const SETTING_WORDS_KEYS = Object.keys(SETTING_WORDS);

/** A setting's value in words, by its kind: never its stored form. A setting
 *  with no words here falls back on the CLI's own "(0: …)". */
export function settingValueWords(key: string, kind: string, value: unknown, words?: string): string {
  if (kind === "switch") return value === true ? "On" : "Off";
  if (value === null || value === undefined || value === "") return "Not set";
  const plain = SETTING_WORDS[key];
  if (typeof value === "number") {
    const zero = plain?.zero ?? /\(0: ([^)]*)\)\s*$/.exec(words ?? "")?.[1];
    if (value === 0 && zero) return capitalise(zero);
    if (plain?.format) return plain.format(value);
    if (plain?.unit) return `${value.toLocaleString()} ${value === 1 ? plain.unit[0] : plain.unit[1]}`;
    return kind === "duration" ? durationWords(value) : value.toLocaleString();
  }
  if (Array.isArray(value)) {
    if (!value.length) return "None";
    if (kind === "durations" && value.every((v) => typeof v === "number")) return capitalise(`after ${(value as number[]).map(durationWords).join(", then ")}`);
    const names = value.map(nameOf);
    return names.every((n): n is string => !!n) ? names.join(", ") : plural(value.length, "item", "items");
  }
  if (typeof value === "object") {
    const values = Object.values(value as Record<string, unknown>);
    if (!values.length) return "None";
    // A Mac's name, never its salted id.
    if (kind === "mac_names") return values.map((v) => String(v)).join(", ");
    return plural(values.length, "item", "items");
  }
  return String(value);
}

type LaneMap = Record<string, Record<string, unknown>>;
const laneMapOf = (raw: unknown): LaneMap => (raw && typeof raw === "object" && !Array.isArray(raw) ? (raw as LaneMap) : {});

/** A lane map's target in words: an account's label from the view, whichever
 *  form it is stored in. A stored key with no account is never shown as is. */
function laneTarget(target: unknown, view: RouteView | null): string {
  if (typeof target !== "string" || !target) return "no account";
  const account = (view?.accounts ?? []).find((a) => a.key === target || a.label === target);
  if (account) return account.label;
  return target.includes("|") ? "an account Khipu no longer tracks" : target;
}

/** Only the job slots the change touches, each in the words the mock uses:
 *  "opus-batch in Project B → Claude". */
function laneMapChanges(was: unknown, is: unknown, view: RouteView | null): { before: string; now: string } | null {
  const a = laneMapOf(was);
  const b = laneMapOf(is);
  const before: string[] = [];
  const now: string[] = [];
  for (const runner of new Set([...Object.keys(a), ...Object.keys(b)])) {
    const lanes = laneMapOf({ ...(a[runner] ?? {}), ...(b[runner] ?? {}) });
    for (const lane of Object.keys(lanes)) {
      const from = laneTarget(a[runner]?.[lane], view);
      const to = laneTarget(b[runner]?.[lane], view);
      if (from === to) continue;
      before.push(`${lane} in ${runner} → ${from}`);
      now.push(`${lane} in ${runner} → ${to}`);
    }
  }
  return now.length ? { before: before.join("\n"), now: now.join("\n") } : null;
}

const LOGIN_SWITCH = /^routing_usage_login_/;
const REPLICATION = "routing_replication_live";

/** A login switch: the CLI keeps the value in use while a change waits
 *  (`usage_logins.login_switch`), so `value` says nothing about what was asked
 *  and a request can even match it. The answer comes only from `asked`. */
function loginCard(base: Pick<DecideCard, "key" | "target" | "who" | "asked" | "edit" | "backAsk">, s: RouteSetting, name: string, request: SettingRequest): DecideCard {
  const inUse = s.value === true ? "on" : "off";
  const now = { cap: "In use now", text: inUse === "on" ? "On" : "Off" };
  const stays = `It stays ${inUse}, and ${STAYS_WAITING}.`;
  if (request.asked === true || request.asked === false) {
    const asked = request.asked ? "on" : "off";
    const same = asked === inUse;
    return {
      ...base,
      title: `A conversation asked to turn ${asked} ${name}${same ? `, which is already ${asked}` : ""}`,
      before: null,
      now,
      changed: null,
      ifIgnored: stays,
      keep: same ? `Keep it ${asked}` : `Turn it ${asked}`,
      back: same ? "Drop the request" : `Leave it ${inUse}`,
    };
  }
  if (request.asked_set === false) {
    return {
      ...base,
      title: `A conversation asked to put ${name} back to its default`,
      before: null,
      now,
      changed: null,
      ifIgnored: stays,
      keep: "Use the default",
      back: `Leave it ${inUse}`,
    };
  }
  return {
    ...base,
    title: `A conversation asked to change ${name}`,
    before: null,
    now,
    changed: { cap: "What it asks", text: "This version of Khipu can't tell whether it turns it on or off. Go by what it says you asked." },
    ifIgnored: stays,
    keep: "Accept the change",
    back: "Leave it as it is",
  };
}

function settingCard(key: string, s: RouteSetting, view: RouteView | null, now: Date): DecideCard {
  const plain = SETTING_WORDS[key];
  const fallback = (s.words ?? "").replace(/\s*\(0: [^)]*\)\s*$/, "");
  const name = plain?.name ?? (fallback ? `“${fallback}”` : "a routing setting");
  const request = s.request ?? {};
  const author = request.by?.project ? proposalAuthor(request.by) : (request.by_words || "a conversation");
  const base = {
    key: `setting:${key}`,
    target: { type: "setting", key } as DecideTarget,
    who: whoWords(author, request.at, now),
    asked: request.request ?? null,
    edit: null,
    // A setting can simply be set again, so taking it back is one click.
    backAsk: null,
  };
  const prior = request.prior ?? s.default;
  if (LOGIN_SWITCH.test(key)) return loginCard(base, s, name, request);
  // Asked on: it stays off until he says yes (a waiting word never shows on).
  if (key === REPLICATION && prior !== true) {
    return {
      ...base,
      title: `A conversation asked to turn on ${name}`,
      before: null,
      now: { cap: "In use now", text: "Off" },
      changed: null,
      ifIgnored: `It stays off, and ${STAYS_WAITING}.`,
      keep: "Turn it on",
      back: "Leave it off",
    };
  }
  const was = settingValueWords(key, s.kind, prior, s.words);
  const is = settingValueWords(key, s.kind, s.value, s.words);
  if (s.kind === "switch") {
    const on = s.value === true;
    return {
      ...base,
      title: `A conversation turned ${on ? "on" : "off"} ${name}`,
      before: { cap: "Before", text: was },
      now: { cap: "In use now", text: is },
      changed: null,
      ifIgnored: `It stays ${on ? "on" : "off"}, and ${STAYS_WAITING}.`,
      keep: `Keep it ${on ? "on" : "off"}`,
      back: `Turn it back ${on ? "off" : "on"}`,
    };
  }
  if (s.kind === "lane_map") {
    const lanes = laneMapChanges(prior, s.value, view);
    return {
      ...base,
      title: `A conversation changed ${name}`,
      before: { cap: "Before", text: lanes?.before ?? "No job slots matched" },
      now: { cap: "In use now", text: lanes?.now ?? "No job slots matched" },
      changed: null,
      ifIgnored: `The new match stays in use, and ${STAYS_WAITING}.`,
      keep: "Keep the new match",
      back: "Go back to the old match",
    };
  }
  const short = typeof prior === "number";
  return {
    ...base,
    title: `A conversation changed ${name}`,
    before: { cap: "Before", text: was },
    now: { cap: "In use now", text: is },
    changed: null,
    ifIgnored: `The new value stays in use, and ${STAYS_WAITING}.`,
    keep: "Keep the new value",
    back: short ? `Go back to ${/^[A-Z][a-z]/.test(was) ? was.charAt(0).toLowerCase() + was.slice(1) : was}` : "Go back to the old value",
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
  for (const [key, s] of Object.entries(routing ?? {})) if (s.request) out.push(settingCard(key, s, view, now));
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
    changed: null,
    ifIgnored: `The new list stays in use, and ${STAYS_WAITING}.`,
    keep: "Keep the new list",
    back: "Go back to the old list",
    backAsk: null,
    edit: null,
  };
}
