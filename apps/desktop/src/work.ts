/* Work (docs/plans/2026-10-15-work-view.md, phase 4): the shapes of the
   `khipu-work-view` document and the ledger reads, and the words the Work
   tab and a ledger page show for them. Every time arrives as ISO 8601 UTC
   and is worded here in local time; nothing here reaches the CLI.

   work.py's last pass withholds any string that still holds a path, a login
   folder, an account key or a secret: such a field arrives as null (a path
   inside text arrives as "(a path)"), and when its privacy list can't be
   loaded every name and line does, with problems.privacy set. The app shows
   WITHHELD's words in its place and offers no action that needs it. */

import { whenLocal } from "./routing";

export type Vocab = { state: Array<[string, string]>; kind: string[] };
export type Meaning = "open" | "done" | "dropped" | "needs" | null;

export type JobLine = { at: string; text: string | null };
export type WorkJob = {
  id: string;
  project: string | null;
  state: string | null;
  state_known: boolean;
  state_words: string;
  started_at: string;
  updated_at: string;
  ended_at?: string | null;
  summary: string | null;
  kind?: string | null;
  for: { pr?: string; train?: string; ledger?: string; task?: string };
  account?: string | null;
  model?: string | null;
  lane?: string | null;
  now: JobLine[];
  quiet?: boolean;
  account_ref?: { id?: string; label?: string | null; matched: boolean };
};
export type WorkPr = {
  id: string;
  stage: string | null;
  stage_known: boolean;
  stage_words: string;
  title: string | null;
  stage_at: string;
  merged_at?: string | null;
  link?: string | null;
  summary?: string | null;
  verdict?: { outcome: string; first_line?: string | null; by?: string | null; at?: string | null } | null;
  disagrees?: string | null;
};
export type WorkRun = { name: string | null; state: string | null; state_known: boolean; at: string; summary?: string | null };
export type WorkTrain = {
  id: string;
  name: string | null;
  state: string | null;
  state_known: boolean;
  state_words: string;
  updated_at: string;
  prs: string[];
  runs: WorkRun[];
  summary?: string | null;
};
export type LedgerItem = {
  id: string;
  title: string | null;
  kind: string;
  parent: string | null;
  state: string;
  state_words: string;
  meaning: Meaning;
  owner: string | null;
  updated_at: string | null;
  stale?: boolean;
  parent_missing?: boolean;
};
export type LedgerGoal = LedgerItem & { children: number; by_state: Record<string, number>; verified: number; done: number };
export type BoardLedger = {
  rows: number;
  by_state: Record<string, number>;
  goals: LedgerGoal[];
  loose_ends: LedgerItem[];
  loose_end_count: number;
  changed_today: LedgerItem[];
  vocab?: Vocab;
};
export type SourceState =
  | "ok"
  | "stale"
  | "gone"
  | "missing"
  | "too_big"
  | "unreadable"
  | "newer_format"
  | "wrong_project"
  | "refused"
  | "off"
  | "none";
export type WorkBoard = {
  project: string | null;
  name: string | null;
  source: {
    state: SourceState;
    problem?: string | null;
    as_of?: string | null;
    dropped?: number;
    other_project?: number;
    capped?: Record<string, { shown: number; of: number }>;
  };
  jobs: WorkJob[];
  prs: WorkPr[];
  trains: WorkTrain[];
  merged: Array<{ id: string; title: string | null; merged_at?: string | null; link?: string | null }>;
  ledger: BoardLedger | null;
  flagged: number;
};
export type WaitAction =
  | "done"
  | "drop"
  | "answered"
  | "open_ledger"
  | "open_link"
  | "open_owed"
  | "snooze"
  | "hide"
  | "same"
  | "not_same"
  | "open_routing";
export type WaitItem = {
  source: "ledger" | "commitment" | "routing";
  key: string | null;
  project: string | null;
  id: string | null;
  title: string | null;
  kind?: string | null;
  since?: string | null;
  evidence?: string | null;
  link?: string | null;
  blocking: boolean;
  /** Old standalone commitments remain visible without counting as active work. */
  quiet?: boolean;
  also_owed?: string | null;
  looks_like?: { id: string | null; title: string | null } | null;
  /** The item has a project, but the view withheld its name. */
  masked?: boolean;
  actions: WaitAction[];
};
// A row id as the ledger accepts one (ledger.py's _ID_RE). An id the
// projection changed ("(a path)", "[redacted]") is not one, and the app
// never looks it up or acts on it.
const ROW_ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]*$/;
export function usableRowId(id: string | null | undefined): id is string {
  return id != null && ROW_ID.test(id);
}

export type WorkCounts = {
  waiting: number;
  quiet?: number;
  blocking: number;
  running: number;
  flagged: number;
  stale_sources: number;
  ledger_outbox: number;
  ledger_refused?: number;
};
export type WorkView = {
  format: string;
  version: number;
  generated_at: string;
  valid_until?: string | null;
  projects: WorkBoard[];
  live: WorkJob[];
  waiting: WaitItem[];
  counts: WorkCounts;
  problems?: Record<string, string>;
  ledger_offline?: boolean;
  ledger_as_of?: string | null;
  /** The hub is away and this Mac has no offline copy of the ledger yet. */
  ledger_copy_unavailable?: boolean;
  refresh_s?: number;
};

/** One row of `khipu ledger list --json` or `get --json`. */
export type LedgerRow = {
  id: string;
  title: string;
  kind: string;
  parent: string;
  state: string;
  evidence: string;
  owner: string;
  updated: string;
  link: string;
  refs: { pr?: string; train?: string; commitment?: string | number };
  project: string;
  history: HistoryEntry[];
  queued?: boolean;
  /** The hub's row version: it changes with every change, even two in one second. */
  version?: number | null;
};
export type HistoryEntry = {
  ts?: string;
  at?: string;
  from?: string | null;
  to?: string | null;
  note?: string | null;
  event?: string;
  by?: string;
  changes?: Record<string, unknown>;
};
export type LedgerQueue = {
  pending: number | null;
  busy: boolean;
  stopped_early: boolean;
  refused: number;
  unreadable: string | null;
};
export type LedgerList = {
  ok: boolean;
  project: string;
  offline: boolean;
  as_of?: string | null;
  /** The hub is away and this Mac has no offline copy yet: only waiting writes show. */
  copy_unavailable?: boolean;
  /** Writes saved on this Mac that have not reached the database yet. */
  pending?: number | null;
  queue?: LedgerQueue;
  vocab: Vocab;
  rows: LedgerRow[];
};
/** What `khipu ledger set --json` answered. */
export type LedgerSetReply = { ok?: boolean; state?: string | null; queued?: boolean; late?: boolean; retried?: boolean };

/** Whether a ledger set reply is a change the ledger applied, to a finished
 *  (done or dropped) state: only then may the Owed item close. A change still
 *  waiting to be sent, or one that arrived late and was kept as history only,
 *  leaves it open. */
export function appliedFinished(reply: LedgerSetReply | null | undefined, vocab: Vocab | null): boolean {
  if (!reply || reply.ok === false || reply.queued || reply.late || !reply.state) return false;
  const m = meaningOf(vocab, reply.state);
  return m === "done" || m === "dropped";
}

/** Words for a set reply that did not apply now, or null. */
export function unappliedWords(reply: LedgerSetReply | null | undefined, owed: boolean): string | null {
  if (!reply) return null;
  const tail = owed ? " The work list item stays open until the project list takes it." : "";
  if (reply.queued) return `Saved on this Mac; the change is waiting to be sent to the database.${tail}`;
  if (reply.late) {
    const where = reply.state ? ` to ${stateWords(reply.state)}` : "";
    return `That change arrived late: the row had moved${where}, so it was kept in the row's history and not applied.${tail}`;
  }
  return null;
}

export type RemovedRow = {
  project: string;
  id: string;
  title: string;
  kind: string;
  state: string;
  removed_at: string;
  removed_by: string;
  note: string;
  days_left: number | null;
};

export const WORK_LEDE = "What is done, what is running, and what is waiting on you";
export const WORK_FOOT =
  "Khipu keeps your ledger and shows what your job runners report. It never starts, stops or moves work.";

/** What shows where the view withheld a field. */
export const WITHHELD = {
  name: "Name withheld",
  title: "Title withheld",
  summary: "Summary withheld",
  line: "Line withheld" } as const;

/** One rule for a project's words, in Owed and in Work: `commitments.project`
 *  is an owner/repo slug (its repo half), or the raw `harness:uuid` session id
 *  a capture opened it under when it could not resolve one ("Session"). */
export function shortProject(project: string): string {
  if (project.includes(":")) {
    // The Owed pill is 96px wide; "Claude Code session" overran it, so the
    // label is the short word and the harness stays in the pill's title.
    return "Session";
  }
  const parts = project.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? project;
}

/** Owed's words for an item with no project. */
export const NO_PROJECT = "No project";

/** "1 row", "1,120 rows". */
export function plural(n: number, one: string, many?: string): string {
  return `${n.toLocaleString()} ${n === 1 ? one : (many ?? `${one}s`)}`;
}

const STATE_APP_WORDS: Record<string, string> = {
  VERIFIED: "Verified",
  "MERGED-UNVERIFIED": "Merged, not verified",
  "BUILT-UNMERGED": "Built, not merged",
  "IN FLIGHT": "In progress",
  QUEUED: "Queued",
  "NOT STARTED": "Not started",
  "NEEDS OWNER": "Needs you",
  "NEEDS MATT": "Needs you",
  DROPPED: "Dropped",
  DONE: "Done" };

/** A ledger state in the app's words; a project's own word shows as written. */
export function stateWords(word: string): string {
  return STATE_APP_WORDS[word] ?? word;
}

const KIND_WORDS: Record<string, [string, string]> = {
  goal: ["Goal", "Goals"],
  slice: ["Slice", "Slices"],
  review: ["Review", "Reviews"],
  "loose-end": ["Loose end", "Loose ends"],
  "needs-owner": ["Needs you", "Needs you"],
  "needs-matt": ["Needs you", "Needs you"],
  "audit-item": ["Audit item", "Audit items"],
  drive: ["Drive", "Drives"],
  wave: ["Wave", "Waves"],
  train: ["Train", "Trains"] };

export function kindWords(kind: string, many = false): string {
  const w = KIND_WORDS[kind];
  if (w) return w[many ? 1 : 0];
  return kind;
}

export function meaningOf(vocab: Vocab | undefined | null, state: string): Meaning {
  const hit = vocab?.state.find(([w]) => w === state);
  return (hit ? hit[1] : null) as Meaning;
}

/** A ledger state's dot. The words always sit beside it. */
export function stateTone(state: string, meaning: Meaning): string {
  if (meaning === "needs") return "hdot err";
  if (meaning === "done") return "hdot ok";
  if (meaning === "dropped") return "hdot idle";
  if (state === "IN FLIGHT") return "hdot run";
  if (state === "BUILT-UNMERGED" || state === "MERGED-UNVERIFIED") return "hdot warn";
  return "hdot";
}

const NO_OWNER = ["none", "unassigned", "unowned", "-", "n/a"];

export function ownerWords(owner: string | null | undefined): string {
  const o = (owner ?? "").trim();
  if (!o || NO_OWNER.includes(o.toLowerCase())) return "No one";
  return o;
}

function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** Whole days since a time; null when there is no time. */
export function daysSince(iso: string | null | undefined, now: Date): number | null {
  const d = parse(iso);
  if (!d) return null;
  return Math.floor((now.getTime() - d.getTime()) / 86_400_000);
}

/** "18 min", "5 h", "2 days". */
export function ageWords(iso: string | null | undefined, now: Date): string {
  const d = parse(iso);
  if (!d) return "";
  const mins = Math.max(0, Math.floor((now.getTime() - d.getTime()) / 60_000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} h`;
  return plural(Math.floor(hours / 24), "day");
}

/** "12 s ago", "4 min ago", then "at <local time>". */
export function updatedWords(iso: string | null | undefined, now: Date): string {
  const d = parse(iso);
  if (!d) return "";
  const secs = Math.max(0, Math.round((now.getTime() - d.getTime()) / 1000));
  if (secs < 60) return `${secs} s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)} min ago`;
  return `at ${whenLocal(iso, now)}`;
}

/** "today 3:12 PM", "Tue 6:40 PM", "Oct 2". */
export function changedWords(iso: string | null | undefined, now: Date): string {
  const d = parse(iso);
  if (!d) return "";
  const days = Math.round((now.getTime() - d.getTime()) / 86_400_000);
  if (days > 6) return d.toLocaleDateString([], { month: "short", day: "numeric" });
  const w = whenLocal(iso, now);
  return /^\d/.test(w) ? `today ${w}` : w;
}

export function timeOnly(iso: string | null | undefined): string {
  const d = parse(iso);
  return d ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
}

/** A local date and time for a history entry: "today 3:12 PM", "Oct 9, 11:15 AM". */
export function stampWords(iso: string | null | undefined, now: Date): string {
  const d = parse(iso);
  if (!d) return iso ?? "";
  const days = Math.round((now.getTime() - d.getTime()) / 86_400_000);
  if (days > 6) {
    return `${d.toLocaleDateString([], { month: "short", day: "numeric" })}, ${timeOnly(iso)}`;
  }
  return changedWords(iso, now);
}

export function jobFor(job: WorkJob): string {
  const f = job.for ?? {};
  if (f.pr) return `pull request ${f.pr}`;
  if (f.train) return `train ${f.train}`;
  if (f.ledger) return `ledger ${f.ledger}`;
  return f.task ?? "";
}

export function jobAccount(job: WorkJob): string {
  const label = job.account_ref?.label ?? job.account ?? null;
  return [label ?? "An account it didn't name", job.model].filter(Boolean).join(" · ");
}

export function jobTone(state: string | null): string {
  if (state === "running") return "hdot run";
  if (state === "failed") return "hdot err";
  if (state === "done") return "hdot ok";
  return "hdot idle";
}

const RUN_WORDS: Record<string, string> = {
  passed: "passed",
  running: "running",
  failed: "failed",
  skipped: "skipped",
  queued: "waiting" };

/** "Unit tests passed", "Screenshots waiting". */
export function runWords(run: WorkRun): string {
  const word = run.state_known && run.state ? RUN_WORDS[run.state] : undefined;
  const name = run.name ?? "A test run (name withheld)";
  return word ? `${name} ${word}` : `${name}: state not known to this Khipu`;
}

export function runTone(run: WorkRun): string {
  if (!run.state_known) return "hdot";
  if (run.state === "passed") return "hdot ok";
  if (run.state === "running") return "hdot run";
  if (run.state === "failed") return "hdot err";
  return "hdot idle";
}

export type LineTone = "" | "warn" | "err" | "ok";

/** "2 running · 1 flagged · 9 waiting on you". */
export function boardMeta(board: WorkBoard, waiting: number, now: Date): string {
  const running = board.jobs.filter((j) => j.state === "running").length;
  const bits: string[] = [];
  if (board.source.state === "gone") bits.push(`Not reporting since ${whenLocal(board.source.as_of, now)}`);
  if (running) bits.push(`${running.toLocaleString()} running`);
  if (board.flagged) bits.push(`${board.flagged.toLocaleString()} flagged`);
  if (waiting) bits.push(`${waiting.toLocaleString()} waiting on you`);
  return bits.join(" · ");
}

export function foldedMeta(board: WorkBoard, waiting: number, now: Date): string {
  if (board.source.state === "gone") return boardMeta(board, waiting, now);
  const bits = ["Nothing running, nothing waiting"];
  if (board.ledger) bits.push(`${plural(board.ledger.loose_end_count, "open row")} in the ledger`);
  if (board.source.state === "ok") bits.push(`updated ${updatedWords(board.source.as_of, now)}`);
  return bits.join(" · ");
}

export const WORK_READ_FAILED = "Khipu couldn't read the work view. It tries again shortly.";
export const WORK_READ_UNREADABLE = "The work view gave an answer Khipu could not read.";

