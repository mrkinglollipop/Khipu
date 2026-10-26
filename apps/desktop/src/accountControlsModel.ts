/* What the account controls in the Routing and Plan details send, as plain
   data with no React in it (docs: ui-controls scope, round 2). A cap form
   becomes the Tauri command's arguments here, checked the way
   `owner_act._values` checks them, so the user meets the rule before the CLI
   refuses; the priority helpers rebuild the whole list with one account
   changed, because `route priority set` takes the whole list. */

import type { CapStatus, RoutePriorityEntry } from "./routing";

// ---------------------------------------------------------------------------
// Caps: the five sentences
// ---------------------------------------------------------------------------

export type CapSentence = "share" | "per_day" | "pace_target" | "spend" | "concurrency";

export const CAP_SENTENCES: { id: CapSentence; label: string }[] = [
  { id: "share", label: "Use at most a share of the week or the five hours" },
  { id: "per_day", label: "Use at most a share of the week in one day" },
  { id: "pace_target", label: "Keep to an even pace" },
  { id: "spend", label: "Spend at most some dollars over some days" },
  { id: "concurrency", label: "Run at most some jobs at once" },
];

/** Every field as the form holds it: text, so a half-typed number is kept. */
export type CapDraft = {
  sentence: CapSentence;
  window: "weekly" | "five_hour";
  pct: string;
  slack: string;
  purpose: "job" | "check";
  tier: "" | "opus" | "sonnet" | "haiku";
  sensitiveOnly: boolean;
  usd: string;
  days: string;
  n: string;
};

export function emptyCapDraft(): CapDraft {
  return {
    sentence: "share",
    window: "weekly",
    pct: "",
    slack: "0",
    purpose: "job",
    tier: "",
    sensitiveOnly: false,
    usd: "",
    days: "7",
    n: "",
  };
}

function text(n: number | null | undefined): string {
  return typeof n === "number" && Number.isFinite(n) ? String(n) : "";
}

/** The form filled in from a cap Khipu already holds. */
export function draftFromCap(c: CapStatus): CapDraft {
  const base = emptyCapDraft();
  const sentence = (CAP_SENTENCES.some((s) => s.id === c.kind) ? c.kind : "share") as CapSentence;
  return {
    ...base,
    sentence,
    window: c.window === "five_hour" ? "five_hour" : "weekly",
    pct: sentence === "pace_target" ? "" : text(c.pct),
    slack: sentence === "pace_target" ? text(c.slack ?? 0) : "0",
    purpose: c.purpose === "check" ? "check" : "job",
    tier: c.tier ?? "",
    sensitiveOnly: c.sensitive_only === true,
    usd: text(c.cap_usd),
    days: text(c.days) || "7",
    n: text(c.cap_n),
  };
}

/** What `khipu_route_cap` takes under `cap`. */
export type CapArgs = {
  kind: CapSentence;
  window?: "weekly" | "five_hour";
  pct?: number;
  slack?: number;
  usd?: number;
  days?: number;
  n?: number;
  purpose?: "job" | "check";
  tier?: "opus" | "sonnet" | "haiku";
  sensitive_only?: boolean;
};

function number(raw: string): number | null {
  const t = raw.trim();
  if (!t) return null;
  const v = Number(t);
  return Number.isFinite(v) ? v : null;
}

/** The cap to send for a "set", or the first thing wrong in words. */
export function capSetArgs(d: CapDraft): { cap: CapArgs } | { error: string } {
  switch (d.sentence) {
    case "share":
    case "per_day": {
      const pct = number(d.pct);
      if (pct == null || pct <= 0 || pct > 100) return { error: "A percent from 1 to 100." };
      // A per-day cap is a share of the week; the CLI wants the window named.
      return { cap: { kind: d.sentence, window: d.sentence === "per_day" ? "weekly" : d.window, pct } };
    }
    case "pace_target": {
      const slack = number(d.slack);
      if (slack == null || slack < 0 || slack > 100) return { error: "Points ahead of an even pace, from 0 to 100." };
      const cap: CapArgs = { kind: "pace_target", window: d.window, pct: 100, slack, purpose: d.purpose };
      if (d.purpose === "check") {
        if (d.tier) cap.tier = d.tier;
        if (d.sensitiveOnly) cap.sensitive_only = true;
      }
      return { cap };
    }
    case "spend": {
      const usd = number(d.usd);
      const days = number(d.days);
      if (usd == null || usd <= 0) return { error: "Dollars above 0." };
      if (days == null || days <= 0 || days > 366) return { error: "Days from 1 to 366." };
      return { cap: { kind: "spend", usd, days } };
    }
    default: {
      const n = number(d.n);
      if (n == null || !Number.isInteger(n) || n < 1) return { error: "A whole number of jobs, at least 1." };
      return { cap: { kind: "concurrency", n } };
    }
  }
}

/** The cap to send for a "remove": only what names it. */
export function capRemoveArgs(c: CapStatus): CapArgs {
  const kind = c.kind as CapSentence;
  const cap: CapArgs = { kind };
  if (kind === "share" || kind === "per_day" || kind === "pace_target") cap.window = c.window === "five_hour" ? "five_hour" : "weekly";
  if (kind === "pace_target") cap.purpose = c.purpose === "check" ? "check" : "job";
  return cap;
}

/** A cap as one plain sentence, for the Undo bar and the remove line. */
export function capSentence(c: CapStatus): string {
  const who = c.purpose === "check" ? "Checks" : "Jobs";
  const win = c.window === "five_hour" ? "five hours" : "week";
  switch (c.kind) {
    case "share":
      return `At most ${c.pct ?? "?"}% of the ${win}`;
    case "per_day":
      return `At most ${c.pct ?? "?"}% of the week in one day`;
    case "pace_target":
      return c.slack && c.slack > 0 ? `${who}: up to ${c.slack} points over an even pace` : `${who}: an even pace`;
    case "spend":
      return `At most $${c.cap_usd ?? "?"} every ${c.days ?? 7} days`;
    case "concurrency":
      return `At most ${c.cap_n ?? "?"} jobs at once`;
    default:
      return "A cap";
  }
}

// ---------------------------------------------------------------------------
// The priority, one account changed
// ---------------------------------------------------------------------------

export type PriorityItem = { account: string; role: string | null; until: string | null };

export function priorityItems(entries: RoutePriorityEntry[]): PriorityItem[] {
  return entries.map((e) => ({ account: e.account, role: e.role ?? null, until: e.until ?? null }));
}

/** The list with this account's words changed (blank: no words). */
export function withRole(entries: RoutePriorityEntry[], account: string, role: string): PriorityItem[] {
  return priorityItems(entries).map((e) => (e.account === account ? { ...e, role: role.trim() || null } : e));
}

/** The list with this account moved one place up (-1) or down (1). */
export function movedBy(entries: RoutePriorityEntry[], account: string, by: -1 | 1): PriorityItem[] | null {
  const items = priorityItems(entries);
  const i = items.findIndex((e) => e.account === account);
  const j = i + by;
  if (i < 0 || j < 0 || j >= items.length) return null;
  [items[i], items[j]] = [items[j], items[i]];
  return items;
}

export function withoutAccount(entries: RoutePriorityEntry[], account: string): PriorityItem[] {
  return priorityItems(entries).filter((e) => e.account !== account);
}

export function withAccountLast(entries: RoutePriorityEntry[], account: string): PriorityItem[] {
  return [...priorityItems(entries).filter((e) => e.account !== account), { account, role: null, until: null }];
}

/** "1,412 characters": the length of the words as the user sees it. */
export function charactersWords(words: string): string {
  const n = [...words].length;
  return `${n.toLocaleString()} character${n === 1 ? "" : "s"}`;
}

// ---------------------------------------------------------------------------
// Undo: find the owner-action line a write just made
// ---------------------------------------------------------------------------

export type LoggedAction = {
  id: string;
  action: string;
  target?: string;
  auth?: string;
  state?: string | null;
  no_undo?: string;
};

/** The write returns no owner-action id for a change made in the app (only a
 *  direction's does), so the line is found in the log: the newest line of this
 *  action that was not there before the write, made in the app and still
 *  open to undo. `before` is the ids seen before the write; null when that
 *  read failed, which accepts the newest matching app line. */
export function findNewAction(after: LoggedAction[], before: Set<string> | null, action: string, target?: string): LoggedAction | null {
  const fresh = after.filter(
    (a) =>
      a.action === action &&
      a.auth === "app" &&
      !a.no_undo &&
      a.state !== "undone" &&
      a.state !== "failed" &&
      (before == null || !before.has(a.id)),
  );
  if (target) {
    const named = fresh.find((a) => a.target === target);
    if (named) return named;
  }
  return fresh[0] ?? null;
}

// ---------------------------------------------------------------------------
// Setting up how Khipu reads a plan
// ---------------------------------------------------------------------------

/** A quota host as the CLI accepts it: letters, digits, dots and hyphens, no
 *  scheme and no path. Returns the host or an error in words. */
export function checkedHost(raw: string): { host: string } | { error: string } {
  const host = raw.trim().replace(/^https:\/\//i, "").replace(/\/+$/, "");
  if (!host) return { error: "Enter the address the provider documents for its usage." };
  if (host.length > 253 || host.includes("..") || !/^[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?$/.test(host)) {
    return { error: "Just the address, like api.example.com: no path, no port, no spaces." };
  }
  return { host };
}
