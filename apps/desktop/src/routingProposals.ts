/* Proposals and holds (routing slice 15b; plan section 3, mock
   docs/mockups/2026-10-21-routing/proposals*.html): what the group shows,
   derived from the slice 5 proposal records and the rule or priority they
   overlay. Pure: a live proposal's words are the entity's words as read;
   the owner's words are the ones underneath it. Nothing here writes. */

import { saidWhen, whenLocal } from "./routing";
import type { RouteAuthor, RouteProposal, RouteView } from "./routing";

const HARNESS_WORDS: Record<string, string> = {
  claude_code: "Claude Code",
  codex: "Codex",
  grok_build: "Grok Build",
  aegis: "Aegis",
  cursor: "Cursor",
  shell: "shell",
};

/** A proposal's author in words, from Khipu's stamp alone (never any text
 *  the writer chose beyond its recorded project name). */
export function proposalAuthor(author: RouteAuthor | undefined): string {
  if (!author) return "a conversation";
  if (author.kind === "owner") return "you";
  const harness = author.harness ? (HARNESS_WORDS[author.harness] ?? null) : null;
  const words = harness ? `a ${harness} conversation` : "a conversation";
  return author.project ? `${words} in ${author.project}` : words;
}

export type ProposalRow = {
  proposal: RouteProposal;
  /** "Rule 1", "Priority 1" or "Your priority". */
  name: string;
  /** Who proposed it and when, in words. */
  by: string;
  /** The proposed words, when they are shared on this Mac. */
  proposed: string | null;
  /** The owner's words underneath; null when the proposal adds new words. */
  owner: string | null;
  /** The proposal's words stay on the Mac they came from. */
  withheld: boolean;
};

function priorityWords(entries: { label: string; role?: string | null; until?: string | null }[] | undefined, now: Date): string | null {
  if (!entries?.length) return null;
  return entries
    .map((e, i) => `${i + 1}. ${e.label}${e.role ? `: ${e.role}` : ""}${e.until ? ` (until ${whenLocal(e.until, now)})` : ""}`)
    .join("\n");
}

function rowFor(view: RouteView, p: RouteProposal, now: Date): ProposalRow {
  const author = proposalAuthor(p.author ?? p.written_by);
  const when = p.at ? saidWhen(p.at, now) : "";
  const by = `Proposed by ${author}${when ? `, ${when}` : ""}`;
  if (p.kind === "rule") {
    const rule = (view.rules ?? []).find((r) => r.entity === p.entity) ?? (view.rules ?? []).find((r) => r.n === p.n);
    const live = p.status === "live";
    return {
      proposal: p,
      name: rule?.n != null ? `Rule ${rule.n}` : p.n != null ? `Rule ${p.n}` : "A rule",
      by,
      proposed: live && rule && !rule.proposal_withheld ? rule.text : null,
      owner: live && rule ? (rule.pending_kind === "added" ? null : (rule.prior_text ?? null)) : null,
      withheld: !!(live && rule?.proposal_withheld),
    };
  }
  if (p.kind === "priority") {
    const priority = view.priority ?? null;
    const live = p.status === "live";
    return {
      proposal: p,
      name: "Your priority",
      by: priority?.pending_by ? `Proposed by ${priority.pending_by}${when ? `, ${when}` : ""}` : by,
      proposed: live && priority && !priority.proposal_withheld ? priorityWords(priority.entries, now) : null,
      owner: live && priority ? priorityWords(priority.replaced?.entries, now) : null,
      withheld: !!(live && priority?.proposal_withheld),
    };
  }
  return { proposal: p, name: p.kind, by, proposed: null, owner: null, withheld: false };
}

export type ProposalsModel = {
  live: ProposalRow[];
  /** Crossed only, still shown beside the live ones; a crossed proposal moves
   *  to the ended once it is older than two days. An overtaken proposal has
   *  ended: the owner wrote over it, so it belongs with the ended. */
  stale: ProposalRow[];
  /** Everything that has ended (crossed apart until it is two days old),
   *  newest last as the record keeps it. */
  ended: ProposalRow[];
  endedToday: number;
};

/** How long a crossed proposal stays beside the live ones. Two Macs writing
 *  at once is worth seeing for a while; after two days it is history. */
const CROSSED_MAIN_MS = 48 * 60 * 60 * 1000;

/** Whether a crossed proposal's written time puts it past that window. A
 *  proposal with no readable written time stays where it is. */
function crossedEnded(r: ProposalRow, now: Date): boolean {
  const t = r.proposal.at ? new Date(r.proposal.at).getTime() : NaN;
  return !Number.isNaN(t) && now.getTime() - t > CROSSED_MAIN_MS;
}

const ENDED_WORDS: Record<string, string> = {
  replaced: "replaced by a newer proposal",
  dismissed: "dismissed",
  expired: "ended after 7 days",
  confirmed: "confirmed",
  undone: "undone",
  overtaken: "replaced by you",
  crossed: "crossed",
};

export function endedWords(status: string): string {
  return ENDED_WORDS[status] ?? status;
}

export function proposalsModel(view: RouteView, now: Date): ProposalsModel {
  const all = Object.values(view.proposals ?? {}).map((p) => rowFor(view, p, now));
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const crossed = all.filter((r) => r.proposal.status === "crossed");
  const ended = all.filter((r) => r.proposal.status !== "live" && (r.proposal.status !== "crossed" || crossedEnded(r, now)));
  // Only a recorded ended time says when a proposal ended; the CLI does not
  // set one for every ending, and the write time is not the ending.
  const endedToday = ended.filter((r) => {
    const t = r.proposal.ended_at ? new Date(r.proposal.ended_at).getTime() : NaN;
    return !Number.isNaN(t) && t >= today;
  }).length;
  return {
    live: all.filter((r) => r.proposal.status === "live"),
    stale: crossed.filter((r) => !crossedEnded(r, now)),
    ended,
    endedToday,
  };
}

/** The hold or pause ids a refused owner write named, from the CLI's own
 *  refusal ("say end or keep in holds for: h1, e2"). Empty otherwise: the
 *  CLI decides which accounts a write names, never this file. */
export function holdChoiceIds(message: string | null | undefined): string[] {
  const m = /say end or keep in holds for: (.+)$/.exec((message ?? "").trim());
  if (!m) return [];
  return m[1].split(",").map((x) => x.trim()).filter(Boolean);
}

export type HoldChoiceTarget = {
  id: string;
  /** "Grok: held by you until Fri 6:00 PM" or a paused account's line. */
  legend: string;
  endLabel: string;
  keepLabel: string;
};

/** What each id a refusal named is, in this read: a hold by its id, or a
 *  paused account by the id of the pause that set it. An id this read no
 *  longer knows still gets a choice, in plain words. */
export function holdChoiceTargets(view: RouteView, ids: string[], now: Date): HoldChoiceTarget[] {
  return ids.map((id) => {
    const hold = (view.holds ?? []).find((h) => h.id === id || h.entity === id || h.revision === id);
    if (hold) {
      const end = hold.until ? `until ${whenLocal(hold.until, now)}` : "until you end it";
      return {
        id,
        legend: `${hold.scope_label}: held ${hold.owner ? "by you" : `by ${hold.by || "a conversation"}`} ${end}`,
        endLabel: "End the hold now",
        keepLabel: "Keep it",
      };
    }
    const account = (view.accounts ?? []).find((a) => a.witness?.id === id);
    if (account) {
      const end = account.until ? `until ${whenLocal(account.until, now)}` : "until you clear it";
      return {
        id,
        legend: `${account.label}: paused by you ${end}`,
        endLabel: "End the pause now",
        keepLabel: "Keep it",
      };
    }
    return { id, legend: "A hold or pause that has since changed", endLabel: "End it now", keepLabel: "Keep it" };
  });
}
