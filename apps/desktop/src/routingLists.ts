// Settings -> Accounts and routing: sorting, filtering and searching its lists
// (settings clarity phase 4; mock docs/mockups/2026-10-15-settings-clarity/
// routing-open.html). They change only what the page shows: Khipu's own
// priority order is changed only by the Priority controls.

import { useState } from "react";
import { accountClassification } from "./routing";
import type { RouteAccount, RouteView } from "./routing";
import type { Problem } from "./routingOverview";
import type { ChangeAbout, ChangeBy } from "./routingWords";

export type AccountSort = "priority" | "soonest" | "left" | "pace" | "name";

export const ACCOUNT_SORTS: { key: AccountSort; label: string }[] = [
  { key: "priority", label: "Your priority" },
  { key: "soonest", label: "Back soonest" },
  { key: "left", label: "Most left" },
  { key: "pace", label: "Spend pace, fastest first" },
  { key: "name", label: "Name" },
];

export type AccountFilter = "all" | "needs" | "open" | "limit" | "paused";

export const ACCOUNT_FILTERS: { key: AccountFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "needs", label: "Needs a look" },
  { key: "open", label: "Open" },
  { key: "limit", label: "At the limit" },
  { key: "paused", label: "Paused" },
];

export type RuleFilter = "all" | "confirm" | "checked" | "broken";

export type ChangeByFilter = "all" | ChangeBy;
export type ChangeAboutFilter = "all" | ChangeAbout;

export const CHANGE_BY: { key: ChangeByFilter; label: string }[] = [
  { key: "all", label: "Everyone" },
  { key: "you", label: "You" },
  { key: "conversations", label: "Conversations" },
  { key: "runners", label: "Job runners" },
  { key: "khipu", label: "Khipu" },
];

export const CHANGE_ABOUT: { key: ChangeAboutFilter; label: string }[] = [
  { key: "all", label: "All kinds" },
  { key: "accounts", label: "Accounts" },
  { key: "rules", label: "Rules and note" },
  { key: "holds", label: "Holds" },
  { key: "settings", label: "Settings" },
];

/** A list longer than this gets a search box. */
export const SEARCH_OVER = 8;

export const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** Whether an account has any problem the Accounts group counts: the same
 *  problems as its closed line and Needs you, never a second reckoning. */
export function accountHasProblem(problems: Problem[], key: string): boolean {
  return problems.some((p) => p.group === "accounts" && (p.item === `account:${key}` || p.key === `${p.kind}:${key}`));
}

export function accountInFilter(a: RouteAccount, f: AccountFilter, problems: Problem[]): boolean {
  const classification = accountClassification(a);
  switch (f) {
    case "needs":
      return accountHasProblem(problems, a.key);
    case "open":
      return classification === "open";
    case "limit":
      return classification === "limited";
    case "paused":
      return classification === "paused";
    default:
      return true;
  }
}

function ms(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  return Number.isNaN(t) ? null : t;
}

/** When an account can be used again: now when it is open, its end time when
 *  one is known, else never (down, paused until cleared, unknown). */
function backAt(a: RouteAccount, now: Date): number {
  if (a.state === "open" || a.state === "probably_open") return now.getTime();
  const until = ms(a.until);
  return until === null || until === undefined ? Number.POSITIVE_INFINITY : Math.max(until, now.getTime());
}

/** A copy of `accounts` (already in the page's base order) in the chosen
 *  order; ties keep the base order. "Your priority" puts the priority list's
 *  accounts first, in its order, and never changes that list. */
export function sortAccounts(accounts: RouteAccount[], sort: AccountSort, view: RouteView, now: Date): RouteAccount[] {
  const facts = view.plan_facts ?? {};
  const readable = (a: RouteAccount) => (facts[a.key]?.readable ? facts[a.key] : undefined);
  let score: (a: RouteAccount) => number;
  switch (sort) {
    case "priority": {
      const order = (view.priority?.entries ?? []).map((e) => e.account);
      score = (a) => {
        const i = order.indexOf(a.key);
        return i === -1 ? order.length : i;
      };
      break;
    }
    case "soonest":
      score = (a) => backAt(a, now);
      break;
    case "left":
      // Most left first; an account whose plan can't be read goes last.
      score = (a) => -(readable(a)?.left_pct ?? Number.NEGATIVE_INFINITY);
      break;
    case "pace":
      score = (a) => -(readable(a)?.pace_pct_per_hour ?? Number.NEGATIVE_INFINITY);
      break;
    default:
      return accounts
        .map((a, i) => ({ a, i }))
        .sort((x, y) => x.a.label.localeCompare(y.a.label, undefined, { sensitivity: "base" }) || x.i - y.i)
        .map(({ a }) => a);
  }
  return accounts
    .map((a, i) => ({ a, i, s: score(a) }))
    .sort((x, y) => (x.s === y.s ? 0 : x.s < y.s ? -1 : 1) || x.i - y.i)
    .map(({ a }) => a);
}

/** Every word of the query appears somewhere in the text, ignoring case. */
export function matchesQuery(query: string, ...texts: (string | null | undefined)[]): boolean {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const hay = texts.filter(Boolean).join(" ").toLowerCase();
  return words.every((w) => hay.includes(w));
}

// ---------------------------------------------------------------------------
// What this Mac remembers: the sort and the filters (not the search words,
// which would hide rows with nothing on screen to say why after a restart).
// ---------------------------------------------------------------------------

export type ListPrefs = {
  accountSort: AccountSort;
  accountFilter: AccountFilter;
  ruleFilter: RuleFilter;
  changeBy: ChangeByFilter;
  changeAbout: ChangeAboutFilter;
};

const DEFAULTS: ListPrefs = { accountSort: "priority", accountFilter: "all", ruleFilter: "all", changeBy: "all", changeAbout: "all" };

const ALLOWED: { [K in keyof ListPrefs]: readonly ListPrefs[K][] } = {
  accountSort: ACCOUNT_SORTS.map((x) => x.key),
  accountFilter: ACCOUNT_FILTERS.map((x) => x.key),
  ruleFilter: ["all", "confirm", "checked", "broken"],
  changeBy: CHANGE_BY.map((x) => x.key),
  changeAbout: CHANGE_ABOUT.map((x) => x.key),
};

export const LISTS_KEY = "khipu.routing.lists";

export function readListPrefs(): ListPrefs {
  const out = { ...DEFAULTS };
  try {
    const raw = window.localStorage.getItem(LISTS_KEY);
    const v: unknown = raw == null ? null : JSON.parse(raw);
    if (v && typeof v === "object") {
      for (const k of Object.keys(ALLOWED) as (keyof ListPrefs)[]) {
        const x = (v as Record<string, unknown>)[k];
        if ((ALLOWED[k] as readonly unknown[]).includes(x)) (out as Record<string, unknown>)[k] = x;
      }
    }
  } catch {
    // Nothing remembered, or storage can't be read: the defaults.
  }
  return out;
}

function writeListPrefs(p: ListPrefs): void {
  try {
    window.localStorage.setItem(LISTS_KEY, JSON.stringify(p));
  } catch {
    // Not remembered on this Mac this time; the page works the same.
  }
}

/** The page's list controls: the remembered sort and filters, and the two
 *  search boxes' words. `reveal` clears whatever could hide a problem's item
 *  before Show moves focus to it. */
export function useListControls() {
  const [prefs, setPrefs] = useState<ListPrefs>(readListPrefs);
  const [ruleQuery, setRuleQuery] = useState("");
  const [laneQuery, setLaneQuery] = useState("");
  const set = <K extends keyof ListPrefs>(k: K, v: ListPrefs[K]) =>
    setPrefs((prev) => {
      const next = { ...prev, [k]: v };
      writeListPrefs(next);
      return next;
    });
  return {
    prefs,
    set,
    ruleQuery,
    setRuleQuery,
    laneQuery,
    setLaneQuery,
    reveal: (group: string) => {
      if (group === "accounts" && prefs.accountFilter !== "all") set("accountFilter", "all");
      if (group === "rules") {
        if (prefs.ruleFilter !== "all") set("ruleFilter", "all");
        setRuleQuery("");
      }
      if (group === "runners") setLaneQuery("");
    },
  };
}

export type ListControls = ReturnType<typeof useListControls>;
