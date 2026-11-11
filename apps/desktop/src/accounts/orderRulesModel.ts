/* Order and rules: the shapes `khipu route say --from-app` prints (slice B6,
   packages/cli/khipu/routing_say.py), and the plain words the Recent changes
   column uses. Nothing here talks to Khipu; the screen does, through
   `khipu_route_say` (preview, apply) and the existing owner-direction commands. */

import type { RouteAccount, RoutePriorityEntry } from "../routing";
import { whenLocal } from "../routing";
import type { OwnerAction } from "../OwnerActions";

export type SayKind =
  | "priority_first"
  | "priority_first_until"
  | "pause"
  | "resume"
  | "stop_at"
  | "end_rule"
  | "add_rule";

/** A preview, exactly as Khipu printed it. The screen shows `understood`, `now`,
 *  `after` and `ends_words` as given. `token` is what Save sends back. */
export type SayPreview = {
  ok: true;
  understood: string;
  kind: SayKind;
  fallback: boolean;
  account: string | null;
  now: string;
  after: string;
  ends: string | null;
  ends_words: string | null;
  token: string;
  expires_at: string;
};

export type SayReceipt = {
  text: string;
  action_id: string;
  undoable: boolean;
  saved_on: string[];
  not_reached: string[];
};

export type SayApplied = { ok: true; applied: true; kind: SayKind; action_id: string; receipt: SayReceipt };

/** The refusals Khipu gives, by `code`. Any other code is the existing
 *  command's own refusal and is shown in its own words. */
export type SayRefusalCode =
  | "owner_only"
  | "bad_args"
  | "not_understood"
  | "no_such_preview"
  | "already_used"
  | "changed";

/** What the screen says for a refusal that is not `not_understood`. Every one
 *  of them changed nothing, and every one asks for a new preview. */
export function refusalWords(code: string | undefined, error: string): string {
  switch (code) {
    case "changed":
      return "This changed since you saw it, so nothing was changed. Preview it again.";
    case "no_such_preview":
      return "That preview has expired, or Khipu didn't make it, so nothing was changed. Preview the change again.";
    case "already_used":
      return "That change was already saved. Preview it again to make it again.";
    default: {
      const said = error.trim();
      const reason = said || "Khipu did not make the change.";
      return /preview/i.test(reason) ? reason : `${reason} Nothing was changed. Preview the change again.`;
    }
  }
}

export function receiptWords(r: SayReceipt): { text: string; saved: string } {
  const where = r.saved_on.length ? r.saved_on.join(" and ") : "this Mac";
  const missed = r.not_reached.length ? ` Not yet on ${r.not_reached.join(" and ")}.` : "";
  return { text: r.text, saved: `Saved on ${where}.${missed} Each conversation gets it with its next message.` };
}

// ---------------------------------------------------------------------------
// Recent changes
// ---------------------------------------------------------------------------

/** Plain words for the owner changes this page is about. Anything else the
 *  owner log holds belongs to All changes, so it is left out here. */
const CHANGE_WORDS: Record<string, string> = {
  "route.pause": "Paused {account}",
  "route.lift": "Opened {account} again",
  "route.priority.set": "Changed your order",
  "route.priority.amend": "Changed {account} in your order",
  "route.priority.confirm": "Kept the order a conversation asked for",
  "route.priority.undo": "Undid a change to your order",
  "route.priority.clear": "Cleared your order",
  "route.cap.set": "Changed where agents stop on {account}",
  "route.cap.remove": "Took away where agents stop on {account}",
  "route.rule.add": "Added a rule",
  "route.rule.edit": "Changed a rule",
  "route.rule.retire": "Ended a rule",
  "route.rule.restore": "Brought a rule back",
  "route.rule.confirm": "Kept a rule a conversation asked for",
  "route.rule.undo": "Undid a change to a rule",
  "route.rule.dismiss": "Turned down a rule a conversation asked for",
  "route.override.grant": "Let a conversation run ahead on {account}",
  "route.override.end": "Ended a run ahead early",
  "route.override.keep": "Kept a run ahead for another week",
  "route.hold.add": "Paused {account}",
  "route.hold.end": "Opened {account} again",
};

/** The account's label for an owner-log target ("codex|", "claude|/Users/x/.claude-second"). */
export function accountLabelFor(target: string, accounts: RouteAccount[] | undefined): string {
  const found = accounts?.find((a) => a.key === target);
  if (found) return found.label;
  const harness = target.split("|")[0];
  return harness ? harness[0].toUpperCase() + harness.slice(1) : "an account";
}

export function isOrderOrRuleChange(a: OwnerAction): boolean {
  return a.action in CHANGE_WORDS;
}

export function changeWords(a: OwnerAction, accounts: RouteAccount[] | undefined): string {
  const template = CHANGE_WORDS[a.action] ?? "Changed your accounts or rules";
  return template.replace("{account}", accountLabelFor(a.target, accounts));
}

/** "You, in the app", or the conversation it came through. */
export function changeWho(a: OwnerAction): string {
  switch (a.auth) {
    case "app":
      return "You, in the app";
    case "terminal":
      return "You, on a terminal";
    case "direction": {
      const harness = a.harness === "claude_code" ? "Claude Code" : a.harness === "codex" ? "Codex" : a.harness === "aegis" ? "Aegis" : null;
      return harness ? `You, through ${harness}` : "You, through a conversation";
    }
    default:
      return "You";
  }
}

export function changeWhen(a: OwnerAction, now: Date): string {
  return whenLocal(a.at, now);
}

/** The newest first, order and rules changes only. */
export function recentChanges(actions: OwnerAction[] | undefined, limit: number): OwnerAction[] {
  return (actions ?? [])
    .filter(isOrderOrRuleChange)
    .filter((a) => a.state !== "failed")
    .sort((x, y) => (x.at < y.at ? 1 : x.at > y.at ? -1 : 0))
    .slice(0, limit);
}

export function canUndoChange(a: OwnerAction): boolean {
  return !a.no_undo && (a.state === "applied" || a.state === "unconfirmed" || a.state == null);
}

// ---------------------------------------------------------------------------
// The order, and what is timed in it
// ---------------------------------------------------------------------------

/** One line a timed change adds under the order: the entry's own exception
 *  words and when it ends. Nothing is shown for an entry with none. */
export function timedLines(entries: RoutePriorityEntry[], now: Date): string[] {
  const out: string[] = [];
  for (const e of entries) {
    for (const x of e.exceptions ?? []) {
      if (!x.until) continue;
      const text = x.text.trim();
      out.push(`${text} Until ${whenLocal(x.until, now)}, then the order goes back.`.replace(/\s+/g, " "));
    }
    if (e.until) out.push(`${e.label} is in your order until ${whenLocal(e.until, now)}.`);
  }
  return out;
}

/** A rule's end, when the record gives it one (`until`). The record's rules
 *  carry none today, so a rule shows no end until Khipu supplies it. */
export function ruleEnds(rule: object, now: Date): string | null {
  const until = (rule as { until?: string | null }).until;
  return until ? whenLocal(until, now) || null : null;
}
