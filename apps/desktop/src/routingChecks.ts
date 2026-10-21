/* Checks and copies (routing slice 15d; docs/plans/2026-10-20-routing-first-class.md
   sections 9, 10 and 11; mocks docs/mockups/2026-10-21-routing/checks-*.html).
   Pure: the advisory flags `khipu route --json` carries (contradictions and
   rule upkeep, from routing_advisory) and rule 12's confirmed model table,
   in the page's own words. Advisory only: nothing here ranks, picks, refuses
   or writes anything, and no row is an owner action. */

import { whenLocal } from "./routing";
import type { RouteContradiction, RouteHygiene, RouteRule, RouteView } from "./routing";

export type ChecksRow = {
  /** Stable within a read; the row's `data-route-item` is `check:<key>`. */
  key: string;
  title: string;
  sub: string;
  kind: "contradiction" | "upkeep" | "unreached" | "mismatch";
  /** Mismatch rows only: the mock's "From the brief" or "Guess" tag. */
  guessed?: boolean;
};

export type TierRow = { label: string; tier: string };

export type FileCopiesModel = {
  status: string;
  scanned_at: string | null;
  files_checked: number;
  hits: { file: string; line: number; quote: string; superseded: boolean }[];
  hits_total: number;
  more_not_shown: number;
  too_large: string[];
};

export type ChecksModel = {
  mismatches: ChecksRow[];
  contradictions: ChecksRow[];
  upkeep: ChecksRow[];
  fileCopies: FileCopiesModel | null;
  unreached: ChecksRow[];
  /** The confirmed model table, or null: `route --json` carries only a
   *  confirmed one, so an absent table can mean none or one still waiting. */
  table: { rows: TierRow[]; line: string } | null;
};

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** Rule 12's task classes in the mock's order and words
 *  (docs/mockups/2026-10-21-routing/checks-light.html). */
const TASK_CLASSES: ReadonlyArray<[string, string]> = [
  ["scope", "Scopes and designs"],
  ["mock", "Mocks"],
  ["ruling", "Rulings"],
  ["hard-security", "Hardest security code"],
  ["failed-fix", "A fix that already failed"],
  ["build", "Builds"],
  ["fix-round", "Fix rounds"],
  ["tests", "Tests"],
  ["docs", "Docs"],
  ["synthesis", "Reports and summaries"],
  ["mechanical", "Searches and lookups"],
];

/** The mock's singular words for one dispatch ("A fix round went to Opus"). */
const MISMATCH_WORK: Record<string, string> = {
  scope: "A scope", mock: "A mock", ruling: "A ruling", "hard-security": "Hard security code",
  "failed-fix": "A fix that already failed", build: "A build", "fix-round": "A fix round", tests: "Tests",
  docs: "Docs", synthesis: "A report", mechanical: "A search",
};

function capitalise(word: string): string {
  return word ? word.charAt(0).toUpperCase() + word.slice(1) : word;
}

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Every account label the view knows, longest first (so "Claude second"
 *  wins over "Claude"). */
function knownLabels(view: RouteView): string[] {
  const labels = [
    ...(view.accounts ?? []).map((a) => a.label),
    ...(view.priority?.entries ?? []).map((e) => e.label),
  ].filter((l): l is string => !!l);
  return [...new Set(labels)].sort((a, b) => b.length - a.length);
}

/** The account label a flag's own sentence names, if any. The flags carry the
 *  account's stable id, which the app can't turn back into a key; the label
 *  in Khipu's sentence can be matched against the accounts it read. */
function namedLabel(text: string | undefined, view: RouteView): string | null {
  const hit = (label: string) =>
    new RegExp(`(?<![\\w-])${escapeRegExp(label)}(?![\\w-])`, "i").test(text ?? "");
  return knownLabels(view).find(hit) ?? null;
}

/** The stable id's harness part, for a flag whose sentence names no label. */
function harnessOf(id: string | undefined): string | null {
  if (!id) return null;
  const harness = id.split(":")[0];
  return harness || null;
}

function accountUntil(view: RouteView, label: string | null): string | null {
  return (view.accounts ?? []).find((a) => a.label === label)?.until ?? null;
}

/** "you paused until Fri 6:00 PM", "is held", "is down", "Khipu doesn't track". */
function stateWords(state: string | undefined, until: string | null, now: Date): string {
  switch (state) {
    case "held":
      return "is held";
    case "paused":
      return until ? `you paused until ${whenLocal(until, now)}` : "you paused";
    case "down":
      return "is down";
    case "untracked":
      return "Khipu doesn't track";
    default:
      return state ? `is ${state}` : "is not open";
  }
}

function stateSub(state: string | undefined, source: string | undefined): string {
  switch (state) {
    case "held":
      return "Your words and the hold can't both hold.";
    case "paused":
      return source === "rule" ? "Your rule names an account you paused." : "Your priority and your pause can't both hold.";
    case "down":
      return "It can't take work while it is down.";
    case "untracked":
      return "It can never be open or limited here.";
    default:
      return "Khipu can't show it as open while this stands.";
  }
}

function ruleByEntity(view: RouteView): Map<string, RouteRule> {
  const map = new Map<string, RouteRule>();
  for (const r of view.rules ?? []) if (r.entity) map.set(r.entity, r);
  return map;
}

function ruleFor(flag: { entities: string[] }, rules: Map<string, RouteRule>): RouteRule | null {
  for (const entity of flag.entities ?? []) {
    const rule = rules.get(entity);
    if (rule) return rule;
  }
  return null;
}

/** One contradiction in the page's words. */
function contradictionRow(c: RouteContradiction, i: number, view: RouteView, rules: Map<string, RouteRule>, now: Date): ChecksRow {
  const key = `${c.kind}:${c.account ?? ""}:${(c.entities ?? []).join("|")}:${i}`;
  const label = namedLabel(c.text, view);
  if (c.kind === "opposite_directions") {
    const harness = harnessOf(c.account);
    const same = (view.accounts ?? []).filter((a) => a.harness === harness).map((a) => a.label);
    const who = label ?? (same.length === 1 ? same[0] : null);
    return {
      key,
      kind: "contradiction",
      title: who ? `Two of your directions for ${who} disagree` : "Two of your directions disagree",
      sub: "You told Khipu both; they can't both hold.",
    };
  }
  if (c.kind === "account_state") {
    const state = c.state;
    const rule = c.source === "rule" ? ruleFor(c, rules) : null;
    // A priority entry Khipu doesn't track is no account in the view, so its
    // name comes from Khipu's sentence ("The priority names Antigravity…").
    const spoken = c.text.match(/\bnames\s+(.+?),\s+which\b/i)?.[1] ?? null;
    const owner = label ?? spoken ?? "an account";
    const entry = (view.priority?.entries ?? []).find((e) => e.label === owner);
    const index = entry ? (view.priority?.entries ?? []).indexOf(entry) + 1 : null;
    const until = (state === "paused" ? (entry?.until ?? accountUntil(view, owner)) : null) ?? null;
    const which = stateWords(state, until, now);
    let title: string;
    if (c.source === "rule") {
      title = rule ? `Rule ${rule.n} names ${owner}, which ${which}` : `A standing rule names ${owner}, which ${which}`;
    } else {
      title = index ? `Priority ${index} names ${owner}, which ${which}` : `Your priority names ${owner}, which ${which}`;
    }
    return { key, kind: "contradiction", title, sub: stateSub(state, c.source) };
  }
  const rule = ruleFor(c, rules);
  return {
    key,
    kind: "contradiction",
    title: c.text,
    sub: rule ? `It comes from rule ${rule.n}.` : "",
  };
}

/** The deadline wording a rule may carry, as the mock quotes it ("until Friday"). */
function deadlineWords(text: string): string | null {
  const match = text.match(/\b(?:until|through|by|expires(?:\s+on)?)\s+(?:on\s+)?([^.;\n]+)/i);
  return match ? match[0].replace(/\s+/g, " ").trim() : null;
}

/** One rule-upkeep flag in the page's words. */
function upkeepRow(h: RouteHygiene, i: number, view: RouteView, rules: Map<string, RouteRule>, now: Date): ChecksRow {
  const key = `${h.kind}:${(h.entities ?? []).join("|")}:${i}`;
  const rule = ruleFor(h, rules);
  const n = rule?.n;
  switch (h.kind) {
    case "move_to_role": {
      const quote = rule ? deadlineWords(rule.text) : null;
      return {
        key,
        kind: "upkeep",
        title: n != null ? (quote ? `Rule ${n} says “${quote}”` : `Rule ${n} has a date in its words`) : h.text,
        sub: "A dated limit belongs on the account's priority, with an end date.",
      };
    }
    case "expired": {
      const quote = rule ? deadlineWords(rule.text) : null;
      return {
        key,
        kind: "upkeep",
        title: n != null ? (quote ? `Rule ${n} is past its date, “${quote}”` : `Rule ${n} is past its date`) : h.text,
        sub: "A rule with a date stops applying when the date passes.",
      };
    }
    case "overlap": {
      const pair = (h.entities ?? []).map((e) => rules.get(e)).filter((r): r is RouteRule => !!r);
      const numbers = pair.map((r) => r.n);
      const label = knownLabels(view).find((l) =>
        pair.some((r) => new RegExp(`(?<![\\w-])${escapeRegExp(l)}(?![\\w-])`, "i").test(r.text)),
      );
      let title = h.text;
      if (numbers.length === 2) title = `Rules ${numbers[0]} and ${numbers[1]} both cover ${label ?? "one account"}`;
      return { key, kind: "upkeep", title, sub: "Two rules on one account and topic." };
    }
    case "role_restates_rule":
      return {
        key,
        kind: "upkeep",
        title: n != null ? `Rule ${n} repeats a priority role` : h.text,
        sub: "Your priority and the rule say the same thing; one is enough.",
      };
    case "long_pending": {
      const at = rule?.at ? new Date(rule.at).getTime() : NaN;
      const hours = Number.isNaN(at) ? null : Math.max(1, Math.floor((now.getTime() - at) / 3_600_000));
      const waited = hours != null ? `has waited ${hours} h` : "has waited";
      return {
        key,
        kind: "upkeep",
        title: n != null ? `A conversation's edit to rule ${n} ${waited}` : `A conversation's edit ${waited}`,
        sub: "It is in use until you confirm or drop it.",
      };
    }
    default:
      return { key, kind: "upkeep", title: h.text, sub: "Khipu flags this so you can fix it." };
  }
}

/** Every advisory row and the model table, in the group's order. */
export function checksModel(view: RouteView | null, now: Date): ChecksModel {
  if (!view) return { mismatches: [], contradictions: [], upkeep: [], fileCopies: null, unreached: [], table: null };
  const rules = ruleByEntity(view);
  const contradictions = (view.contradictions ?? []).map((c, i) => contradictionRow(c, i, view, rules, now));
  const upkeep = (view.rule_hygiene ?? []).map((h, i) => upkeepRow(h, i, view, rules, now));
  const unreached: ChecksRow[] = (view.unreached_directions ?? []).map((d) => ({
    key: `unreached:${d.id}`,
    kind: "unreached",
    title: `“${d.words}”`,
    sub: `You said this in a conversation${d.project ? ` in ${d.project}` : ""}. ` +
      (d.join === "unjoined"
        ? `The owner turn could not be matched${d.reason ? `: ${d.reason}` : ""}.`
        : "Nothing in Khipu matches it."),
  }));
  const mismatches: ChecksRow[] = (view.model_mismatches ?? []).map((m, i) => {
    const work = m.task ? (MISMATCH_WORK[m.task] ?? m.task) : "Work";
    const where = [m.account, m.project ? `in ${m.project}` : ""].filter(Boolean).join(" ");
    return {
      key: `mismatch:${i}:${m.at}`,
      kind: "mismatch",
      guessed: m.guessed,
      title: `${work} went to ${capitalise(m.actual ?? "a different model")}; your table says ${capitalise(m.expected ?? "another")}`,
      sub: `${where ? `${where}, ` : ""}${whenLocal(m.at, now)} · ` +
        (m.guessed ? "guessed from the brief's words" : "the brief named the kind of work"),
    };
  });
  const copies = view.routing_file_copies;
  const fileCopies: FileCopiesModel | null = copies
    ? { status: copies.status, scanned_at: copies.scanned_at ?? null,
        files_checked: copies.files_checked ?? 0, hits: copies.hits ?? [],
        hits_total: copies.hits_total ?? copies.hits?.length ?? 0,
        more_not_shown: copies.more_not_shown ?? 0,
        too_large: copies.too_large ?? [] }
    : null;
  let table: ChecksModel["table"] = null;
  const tiers = view.tier_table?.tiers;
  if (tiers && Object.keys(tiers).length && view.tier_table?.confirmed !== false) {
    const known = TASK_CLASSES.filter(([k]) => tiers[k]).map(([k, label]) => ({ label, tier: capitalise(tiers[k]) }));
    const extra = Object.keys(tiers)
      .filter((k) => !TASK_CLASSES.some(([knownKey]) => knownKey === k))
      .sort()
      .map((k) => ({ label: k, tier: capitalise(tiers[k]) }));
    const rows = [...known, ...extra];
    table = { rows, line: `${plural(rows.length, "kind", "kinds")} of work · confirmed by you` };
  }
  return { mismatches, contradictions, upkeep, fileCopies, unreached, table };
}

/** The group's closed line: what is flagged, in the parts Khipu can see. */
export function checksLine(view: RouteView | null, now: Date): string {
  const model = checksModel(view, now);
  const parts: string[] = [];
  if (model.mismatches.length) parts.push(`${plural(model.mismatches.length, "model mismatch", "model mismatches")} today`);
  if (model.contradictions.length) parts.push(plural(model.contradictions.length, "contradiction", "contradictions"));
  if (model.upkeep.length) parts.push(`${plural(model.upkeep.length, "rule", "rules")} to look at`);
  if (model.fileCopies?.hits_total) parts.push(`${plural(model.fileCopies.hits_total, "copy", "copies")} in files`);
  if (model.unreached.length) parts.push(`${plural(model.unreached.length, "direction", "directions")} not in Khipu`);
  if (parts.length) return parts.join(" · ");
  if (model.fileCopies && model.fileCopies.status !== "checked") return "Files not looked at yet";
  if (model.table) return "Your model table · nothing flagged";
  return "Nothing flagged in Khipu's record";
}
