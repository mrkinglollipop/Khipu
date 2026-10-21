/* Routing in the app (docs/plans/2026-10-14-routing-truth.md, phase 4): the
   routing record as `khipu route --json` prints it, read through one
   fixed-argv command (`khipu_route_view`), and the plain words the Home card,
   the health report's Routing row and the attention items use. Khipu reports
   which accounts are open; it never picks one, so nothing here ranks, picks
   or refuses anything. Every time is the viewer's own local time. The words
   follow the scope's table ("In the record" / "In the app"). */

import { useCallback, useEffect, useRef, useState } from "react";
import { callCli } from "./settingsCli";
import { fmtAgoSince } from "./time";

export type RouteAuthor = { kind?: string; harness?: string; project?: string; session?: string; name?: string };

export type RouteWitness = {
  model?: string | null;
  family?: string | null;
  transport?: string | null;
  id?: string;
  source?: string;
  written_by?: RouteAuthor;
  /** The author in words (`author_words`), set by the CLI. */
  by?: string;
  at?: string | null;
  message_time?: boolean;
  cleared?: boolean;
  observed?: boolean;
  /** A limit reported with no time: it lasts `report_default_limit`. */
  until_default?: boolean;
};

export type RouteReport = RouteWitness & {
  state?: string;
  since?: string | null;
  until?: string | null;
};

export type RouteModel = {
  model: string;
  until?: string | null;
  basis?: string | null;
  source?: string;
  at?: string | null;
  /** The report behind this model's limit, and who wrote it in words. */
  id?: string;
  by?: string;
  disagreement?: RouteReport | null;
};

export type RouteAccount = {
  key: string;
  label: string;
  harness: string;
  state: string;
  until?: string | null;
  basis?: string | null;
  reason?: string | null;
  checkable?: boolean;
  /** False when plan usage no longer reads the account (removed in
   *  Settings -> Models while limited): no reset check and no Check now. */
  configured?: boolean;
  usage_setup?: string;
  witness?: RouteWitness | null;
  disagreement?: RouteReport | null;
  last_seen?: RouteReport | null;
  models?: RouteModel[];
  silent?: boolean;
  next_check?: string | null;
  reset_check?: { checks?: number; busy_tries?: number; status?: string; next_at?: string | null; reason?: string | null } | null;
  /** One pace object per window Khipu reads (slice 4Pb), five-hour first. */
  pace?: Pace[];
  /** Each owner cap on this account with its verdict at this read (slice 17a). */
  cap_statuses?: CapStatus[];
  /** Khipu's launch advice for jobs (slice 17c). Advisory, never a gate. */
  advice?: RouteAdvice | null;
  /** Check advice uses its own pace allowance, or the job allowance. */
  check_advice?: RouteAdvice | null;
  /** The same advice for lead conversations (orchestration purpose). */
  lead_advice?: RouteAdvice | null;
};

export type RouteHold = {
  id: string;
  entity?: string;
  revision?: string;
  scope?: { kind?: string; account?: string };
  scope_label: string;
  reason: string;
  text?: string | null;
  until?: string | null;
  owner?: boolean;
  by?: string;
  at?: string | null;
  /** An owner hold older than the reminder setting (90 days by default). */
  remind?: boolean;
};

export type RouteRule = {
  n: number;
  text: string;
  /** The rule's stable id, as the advisory flags name it (`routing_advisory`). */
  entity?: string;
  seeded?: boolean;
  confirmed?: boolean;
  owner_revision?: string;
  /** Its live proposal's words are not shared on this Mac. */
  proposal_withheld?: boolean;
  /** A live conversation request awaiting the owner's reconfirmation. */
  pending?: boolean;
  pending_kind?: "added" | "changed" | null;
  pending_by?: string | null;
  /** The wording Undo restores for a pending change. */
  prior_text?: string | null;
  date?: string | null;
  /** Where the rule came from, in the owner's or the seed's words. */
  source?: string | null;
  via?: string | null;
  at?: string | null;
};

export type RouteRunnerFile = { error?: string | null; good_at?: string | null; since?: string | null };

export type RouteRunner = {
  name: string;
  path?: string;
  heartbeat_at?: string | null;
  /** Seconds between its heartbeats. */
  interval?: number | null;
  silent?: boolean;
  unreadable?: boolean;
  unreadable_since?: string | null;
  files?: Record<string, RouteRunnerFile>;
  lanes?: string[];
  unmatched_lanes?: string[];
  /** What the CLI's shared resolver says (routing.py `resolve_lane`): the
   *  account label it matched, how, the candidates when it couldn't, and why a
   *  declaration was refused (`reason`, e.g. "launcher evidence contradicts
   *  the declaration"). */
  lane_matches?: Record<string, {
    account: string | null;
    matched_by: string | null;
    candidates: string[];
    identity_state?: string | null;
    reason?: string | null;
    field_to_add?: string | null;
  }>;
};

export type RouteLane = { runner: string; lane: string; until?: string | null };

/** One changed entity in a version: what it says now and said before (its
 *  effective view; null when it ended or began) and who caused it. */
export type RouteChangeItem = {
  entity: string;
  now?: Record<string, unknown> | null;
  was?: Record<string, unknown> | null;
  source?: string | null;
  via?: string | null;
  /** The author in words, set by the CLI. */
  by?: string | null;
  /** A removed rule's number. */
  n?: number | null;
};

export type RouteChangeRecipients = {
  have_count: number;
  live_count: number;
  have: string[];
  waiting: string[];
};

export type RouteChange = {
  version: number;
  at: string;
  entities: string[];
  items?: RouteChangeItem[];
  /** Owner-local only (survey rows 8-11): who has this change, by the same
   *  friendly label the live rows use. Null when Khipu can't tell. */
  recipients?: RouteChangeRecipients | null;
};

/** One live delivery row from `route --json` (owner-local; never a session
 *  id, a Mac id or a path). */
export type RouteDeliveryRow = {
  label: string;
  harness?: string | null;
  component?: string | null;
  mac?: string | null;
  last_prompt_at?: string | null;
  status?: string;
  owed?: number | null;
  reasons?: string[];
  missing?: string[];
};

export type RouteDeliveryMac = {
  label: string;
  age_seconds?: number | null;
  behind?: boolean;
  freshness?: string;
};

export type RuleCheck = {
  rule: number;
  status: string;
  text: string;
  at?: string | null;
  /** The job runner it was judged on, and which of its files. */
  runner?: string;
  file?: "settings" | "events" | string;
};

// What a checked rule's last check found (`routing_sources.rule_results`).

/** One file a check depends on, on one job runner: ever read well, when, and
 *  whether the latest read failed or the good one is too old. */
export type RuleRead = { read?: boolean; good_at?: string | null; failed?: boolean; since?: string | null; stale?: boolean };

/** One check on one job runner, from that runner's own reads and findings. */
export type RuleCell = {
  runner: string;
  outcome: "broken" | "not_read" | "failed" | "stale" | "no_lane" | "cannot_check" | "ok" | string;
  lanes?: number;
  unchecked?: number;
  reads?: { settings?: RuleRead; events?: RuleRead };
  text?: string | null;
  more?: number;
  why?: string | null;
  /** What a cap or a name Khipu can't use left out of this runner's files. */
  skipped?: { lanes?: number; fix_lanes?: number; kinds?: number; added?: number; unfinished_line?: number };
  /** The privacy check's read of the whole events log: bytes read of its size, lines read, finished. */
  scan?: { read?: number; size?: number; lines?: number; done?: boolean; reset?: string | null };
};

export type RuleCheckResult = Omit<RuleResult, "rule" | "checks"> & { check: string; cells?: RuleCell[] };

export type RuleResult = {
  rule: number;
  /** One entry per check the rule asks for; `result` is the worst of them. */
  checks?: RuleCheckResult[];
  /** Which of Khipu's four checks gave this result. */
  check?: "fast_tier" | "paid_xai" | "grok_private" | "grok_confirm" | string;
  result: "ok" | "broken" | "cannot_check" | "no_lane" | "no_runner" | "not_read" | "failed" | "stale" | "checking" | "incomplete" | string;
  /** When the check last ran on the files it judged; only for "ok". */
  at?: string | null;
  /** Lanes it judged, and lanes it is about but couldn't judge. */
  lanes?: number;
  unchecked?: number;
  /** A broken check's own words, and how many more broken lanes. */
  text?: string | null;
  more?: number;
  why?: string | null;
};

/** One account in the owner's priority, in his order (phase 4b). */
export type RoutePriorityEntry = {
  /** The account's key, as Settings -> Routing sends it back. */
  account: string;
  label: string;
  /** The owner's words for its role. */
  role?: string | null;
  until?: string | null;
  /** False when the account is no longer in Settings -> Models. */
  in_settings?: boolean;
  /** A level the owner worded into the role, while it lasts (`routing.py`
   *  keeps `level` beside the entry; the CLI marks stale words in its own
   *  lines). */
  level?: { claim: string; as_of?: string | null; until?: string | null } | null;
};

/** The owner's priority: his order of accounts, never Khipu's. A pending one
 *  was set by a conversation at his request and waits for his Confirm. */
export type RoutePriority = {
  entries: RoutePriorityEntry[];
  until?: string | null;
  at?: string | null;
  via?: string | null;
  pending?: boolean;
  confirmed?: boolean;
  pending_by?: string | null;
  proposal_withheld?: boolean;
  owner_request?: string | null;
  replaced?: { entries: RoutePriorityEntry[]; until?: string | null } | null;
  /** The priority as read; a write that carries it is refused once it moved. */
  version?: string | null;
};

/** What Khipu read of an account's plan (`routing.plan_fact`): its longest
 *  window, how much is left and the pace that would use it by its reset. */
export type PlanFact = {
  readable: boolean;
  why?: string;
  window?: string;
  used_pct?: number;
  left_pct?: number;
  resets_at?: string;
  seconds?: number;
  pace_pct_per_hour?: number;
  hurry?: boolean;
  words?: string;
  /** Read more than 15 minutes ago: shown with its age, never a pace. */
  stale?: boolean;
  figures_at?: string | null;
  /** Its longest window's reset time passed since it was read. */
  reset_passed?: boolean;
};

/** One window's pace (`routing_pace.compute_pace`, slice 4Pb): the trailing
 *  rate Khipu read, its verdict against the plan limit and each window-scoped
 *  cap, and `why` the rate cannot be told. */
export type PaceLimit = {
  verdict?: "unknown" | "limit_reached" | "cap_reached" | "outpacing" | "underpacing" | "on_pace" | string;
  needed_pct_per_h?: number | null;
  runs_out_at?: string | null;
  /** How long before the window's reset it runs out, in seconds. */
  before_reset_s?: number | null;
  unused_at_reset_pct?: number | null;
  revision?: string | null;
  pct?: number | null;
};

/** A per-day cap's own arithmetic: what the rolling day gained, where it
 *  peaks and when it reaches the cap. */
export type PaceDayCap = {
  revision?: string | null;
  pct?: number;
  verdict?: "over" | "near" | "under" | "unknown" | string;
  used_24h_pct?: number | null;
  observed_pct?: number | null;
  settles_at_pct?: number | null;
  peak_pct?: number | null;
  peak_at?: string | null;
  reaches_cap_at?: string | null;
  why?: string | null;
};

/** An even-pace cap (`pace_target`): how much the account may have used by
 *  now and where it stands against that allowance. */
export type PaceTarget = {
  revision?: string | null;
  pct?: number;
  slack?: number;
  allowance_pct?: number | null;
  used_pct?: number | null;
  verdict?: "ahead" | "within" | "recovering" | "unknown" | string;
  why?: string | null;
};

export type Pace = {
  window: "five_hour" | "weekly" | string;
  cycle_resets_at?: string | null;
  /** The trailing span the rate covers: 1 or 24. */
  trailing_h?: number;
  buckets?: number;
  valid_pct?: number;
  observed_pct?: number;
  gappy?: boolean;
  rate_pct_per_h?: number | null;
  /** Why no rate can be given, in the CLI's own words. */
  why?: string | null;
  used_pct?: number | null;
  plan?: PaceLimit;
  share_cap?: PaceLimit | null;
  day_cap?: PaceDayCap | null;
  pace_target?: PaceTarget | null;
  check_pace_target?: PaceTarget | null;
  /** Which target the window's verdict speaks: plan, share_cap or day_cap. */
  headline?: "plan" | "share_cap" | "day_cap" | string;
};

/** One owner cap with its verdict at this read (`routing.py cap_statuses`,
 *  slice 17a/17t): the fact as you confirmed it, plus the figures that
 *  judged it. A cap Khipu cannot read says `unknown` with no figure. */
export type CapStatus = {
  kind: "share" | "per_day" | "pace_target" | "spend" | "concurrency" | string;
  purpose?: "job" | "check";
  tier?: "opus" | "sonnet" | "haiku";
  sensitive_only?: boolean;
  /** The fact's opaque revision, as you confirmed it. */
  revision?: string | null;
  /** When you confirmed this cap (`route --json` only; absent elsewhere). */
  confirmed_at?: string | null;
  window?: "five_hour" | "weekly" | null;
  pct?: number | null;
  slack?: number | null;
  allowance_pct?: number | null;
  used_pct?: number | null;
  cap_usd?: number | null;
  days?: number | null;
  usd?: number | null;
  events?: number | null;
  /** Owner-display evidence only. Never a cap total or an advice input. */
  reported?: {
    usd: number;
    events: number;
    gaps: { runner: string; from: string; to: string; seconds: number }[];
  } | null;
  cap_n?: number | null;
  jobs?: number | null;
  verdict?: string;
  why?: string | null;
};

/** One account's launch advice (`routing_advice.account_advice`, slice 17c).
 *  `route --json` carries the job purpose as `advice` and orchestration as
 *  `lead_advice`. Advisory: it never blocks, permits or launches. */
export type RouteAdvice = {
  account?: string;
  label?: string | null;
  purpose?: "job" | string;
  verdict?: "go" | "hold_back" | "closed" | "not_eligible" | string;
  /** The verdict's cause: semicolon-joined "kind verdict" pairs, a state, a
 *  crossing, or the eligibility sentence in the CLI's words. */
  reason?: string | null;
  evidence?: "fresh" | "stale" | "unknown" | string;
  numbers?: Record<string, number>;
  caps?: (CapStatus & { applies?: boolean })[];
  eligible?: boolean;
  why_eligible?: string | null;
  advisory?: boolean;
  /** The newest usage reading's time behind the evidence (`route --json`). */
  newest_reading_at?: string | null;
};

/** Rule 12's model table, once the owner has confirmed it (slice 10). Only a
 *  confirmed table reaches `route --json`; a proposal waiting for the owner
 *  stays in the event log. */
export type RouteTierTable = {
  tiers: Record<string, "opus" | "sonnet" | "haiku" | string>;
  confirmed: boolean;
};

/** One advisory contradiction (`routing_advisory._contradictions`): a priority
 *  or rule naming a held or paused account, a down or untracked priority entry,
 *  or two structured directions that disagree. Never a gate. */
export type RouteContradiction = {
  kind: "account_state" | "opposite_directions" | string;
  /** The account's stable id. */
  account?: string;
  /** The ids of the lines that disagree (a rule's or priority's entity, or
   *  the two directions). */
  entities: string[];
  /** Khipu's own sentence; the app words the row itself where it can. */
  text: string;
  advisory: true;
  state?: string;
  source?: "priority" | "rule" | string;
};

/** One rule-upkeep flag (`routing_advisory._hygiene`): a dated rule, a passed
 *  date, two rules on one topic, a role restating a rule, or an edit a
 *  conversation made that has waited on the owner. */
export type RouteHygiene = {
  kind: "move_to_role" | "expired" | "overlap" | "role_restates_rule" | "long_pending" | string;
  /** The account's stable id, when the flag names one. */
  account?: string;
  /** The rule's `entity` (and, for an overlap or a restated role, the second
   *  rule or the priority's entity). */
  entities: string[];
  text: string;
  advisory: true;
  owner_direction?: { confirm?: string; drop?: string };
};

export type RouteProvider = {
  id: string;
  label: string;
  family: string;
  login?: string;
  probe?: string;
  lane_names?: string[];
  models_allowed?: string[];
  models_never?: string[];
  use?: string;
};

/** One conversation proposal on an entity (slice 5), as `khipu route
 *  --json` keeps it: which entity, its standing beside the owner's newest
 *  revision (`base`), its author, and how it ended when it is not live. */
export type RouteProposal = {
  id: string;
  entity: string;
  kind: "rule" | "priority" | string;
  base?: string | null;
  author?: RouteAuthor;
  written_by?: RouteAuthor;
  at?: string | null;
  n?: number | null;
  status: "live" | "replaced" | "crossed" | "overtaken" | "dismissed" | "expired" | "confirmed" | "undone" | string;
  replaced_by?: string | null;
  ended_at?: string | null;
};

/** Read-only delivery evidence from the `routing_delivery` block in `khipu
 * doctor`. `khipu route --json` deliberately does not carry this ledger. */
export type RouteDeliveryIdentity = {
  /** Opaque canonical identity. The health read has no friendly project or Mac label. */
  identity: string;
  /** The component whose receipt is current: main, mcp, or agent:<id>. */
  component: string;
  status: "green" | "red" | "unknown" | string;
  reasons?: string[];
  missing?: string[];
  owed?: number;
};

export type RouteDeliveryFreshness = {
  status?: "green" | "red" | "unknown" | "not_applicable" | string;
  age_seconds?: number | null;
};

export type RouteDeliveryHealth = {
  status: "green" | "red" | "unknown" | string;
  reasons?: string[];
  missing?: string[];
  identities?: RouteDeliveryIdentity[];
  prompts?: { status?: string; reasons?: string[]; missing?: string[]; count?: number; skipped?: number; rate_pct?: number };
  replication?: RouteDeliveryFreshness;
  gateway?: RouteDeliveryFreshness;
};

export type RouteView = {
  proposals?: Record<string, RouteProposal>;
  definitions?: { providers?: Record<string, RouteProvider>; accounts?: Record<string, unknown> };
  record?: boolean;
  version?: number;
  /** The priority's version, also when none is set (`version_moved` guard). */
  priority_version?: string | null;
  checked_at?: string;
  accounts?: RouteAccount[];
  holds?: RouteHold[];
  rules?: RouteRule[];
  note?: { text: string; at?: string | null } | null;
  unmatched?: RouteLane[];
  sources?: {
    runners?: RouteRunner[];
    refused?: string[];
    table?: { path?: string; stamped_at?: string | null; changed_at?: string | null; error?: string | null } | null;
  } | null;
  /** The numbers of the rules Khipu has a check for (`routing_sources.watched`). */
  watched_rules?: number[];
  /** The old hook's off switches as the app's CLI sees them. */
  old_switches?: { env?: boolean; file?: boolean };
  changes?: RouteChange[];
  rule_checks?: RuleCheck[];
  /** One result per checked rule (settings clarity phase 5). */
  rule_results?: RuleResult[];
  /** Seconds; 0 means never. */
  warn_after?: { disagreement?: number; not_refreshed?: number };
  /** Seconds a limit reported without a time lasts. */
  report_default_limit?: number;
  priority?: RoutePriority | null;
  /** Each account's plan facts, by its key. */
  plan_facts?: Record<string, PlanFact>;
  /** Whether conversations hear accounts in pace order when no priority is set. */
  pace_order?: boolean;
  /** Rule 12's table, only once the owner has confirmed it (slice 10). */
  tier_table?: RouteTierTable | null;
  /** Advisory contradictions and rule upkeep for this read (slice 10). Each
   *  is derived on every read, so a passed date or a long wait appears as
   *  soon as the clock passes it. */
  contradictions?: RouteContradiction[];
  rule_hygiene?: RouteHygiene[];
  /** Live delivery rows, other Macs and per-change recipients, projected
   *  for this Mac's owner only (survey rows 8-11). */
  delivery_rows?: RouteDeliveryRow[];
  delivery_macs?: RouteDeliveryMac[];
  /** Routing copies found in files, for this Mac's owner only (slice 13/15d). */
  routing_file_copies?: {
    status: string;
    scanned_at?: string | null;
    files_checked: number;
    hits: { file: string; line: number; quote: string; superseded: boolean }[];
    hits_total?: number;
    more_not_shown?: number;
    too_large?: string[];
  };
  /** Open owed directions, projected for this Mac's owner only (slice 19a). */
  unreached_directions?: {
    id: string;
    words: string;
    project?: string | null;
    join: string;
    reason?: string | null;
  }[];
  /** Today's rule 12 model mismatches, kept on this Mac only (labels, never
   *  brief text, session ids or paths). */
  model_mismatches?: {
    at: string;
    task: string | null;
    expected: string | null;
    actual: string | null;
    guessed: boolean;
    account: string | null;
    model: string | null;
    project: string | null;
  }[];
};

export type Tone = "ok" | "warn" | "err" | null;
export type AccountClassification = "open" | "probably_open" | "limited" | "down" | "paused" | "unknown";

/** The one owner-facing classification for account details and summaries. */
export function accountClassification(a: RouteAccount): AccountClassification {
  switch (a.state) {
    case "open":
    case "probably_open":
    case "limited":
    case "down":
    case "paused":
      return a.state;
    default:
      return "unknown";
  }
}

const MINUTE_MS = 60_000;
const DAY_MS = 24 * 60 * MINUTE_MS;

function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

function clock(d: Date): string {
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/** "" today, "tomorrow", "yesterday", "Sat" within six days, else "Oct 21". */
function dayWords(d: Date, now: Date): string {
  const days = Math.round((startOfDay(d) - startOfDay(now)) / DAY_MS);
  if (days === 0) return "";
  if (days === 1) return "tomorrow";
  if (days === -1) return "yesterday";
  if (Math.abs(days) < 7) return d.toLocaleDateString([], { weekday: "short" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

/** "2:52 PM" today, else "Sat 8:00 PM" or "tomorrow 9:40 AM". */
export function whenLocal(iso: string | null | undefined, now: Date): string {
  const d = parse(iso);
  if (!d) return "";
  const day = dayWords(d, now);
  return day ? `${day} ${clock(d)}` : clock(d);
}

/** "at 2:52 PM" today, else "Sat 8:00 PM": a time ahead. */
export function aheadWords(iso: string | null | undefined, now: Date): string {
  const d = parse(iso);
  if (!d) return "";
  return dayWords(d, now) ? whenLocal(iso, now) : `at ${clock(d)}`;
}

/** "at 1:52 PM" today, else "yesterday at 1:52 PM" or "Mon at 8:00 PM":
 *  a time that has passed, as the end of a sentence. */
export function pastWords(iso: string | null | undefined, now: Date): string {
  const d = parse(iso);
  if (!d) return "";
  const day = dayWords(d, now);
  return day ? `${day} at ${clock(d)}` : `at ${clock(d)}`;
}

/** "just now", "4 min ago" within the hour, "at 11:13 AM" today, else
 *  "yesterday at 6:05 PM": when something was said. */
export function saidWhen(iso: string | null | undefined, now: Date): string {
  const d = parse(iso);
  if (!d) return "";
  const ago = now.getTime() - d.getTime();
  if (ago >= 0 && ago < MINUTE_MS) return "just now";
  if (ago >= 0 && ago < 60 * MINUTE_MS) return `${Math.round(ago / MINUTE_MS)} min ago`;
  const day = dayWords(d, now);
  return day ? `${day} at ${clock(d)}` : `at ${clock(d)}`;
}

/** "14 h ago", "3 days ago": how old a last sighting is. */
function ageWords(iso: string | null | undefined, now: Date): string {
  const d = parse(iso);
  if (!d) return "";
  const mins = Math.max(0, Math.round((now.getTime() - d.getTime()) / MINUTE_MS));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

/** "30 minutes", "2 hours". */
export function spanWords(seconds: number): string {
  const mins = Math.round(seconds / 60);
  if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"}`;
  const hours = Math.round(mins / 60);
  return `${hours} hour${hours === 1 ? "" : "s"}`;
}

export function capitalise(text: string): string {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

/** A reset-check reason after a semicolon: its first word in lower case
    unless it is a name ("Khipu", "Claude"), as the CLI's `_mid_sentence`. */
export function midSentence(reason: string): string {
  const space = reason.indexOf(" ");
  const first = space < 0 ? reason : reason.slice(0, space);
  if (first === "This" || first === "The" || first === "Another") return first.toLowerCase() + reason.slice(first.length);
  return reason;
}

const CLI_NAMES: Record<string, string> = { claude: "Claude", codex: "Codex", grok: "Grok", muse: "Muse", glm: "GLM", cursor: "Cursor" };

function cliName(harness: string): string {
  return CLI_NAMES[harness] ?? harness;
}

function ahead(iso: string | null | undefined, now: Date): boolean {
  const d = parse(iso);
  return d != null && d.getTime() > now.getTime();
}

function modelLimits(a: RouteAccount, now: Date): string[] {
  return (a.models ?? [])
    .filter((m) => ahead(m.until, now))
    .map((m) =>
      m.basis === "estimated"
        ? `${m.model} limit reached, back about ${whenLocal(m.until, now)} (a guess)`
        : `${m.model} limit reached, back ${aheadWords(m.until, now)}`,
    );
}

function stateWord(state: string | undefined): string {
  if (state === "limited" || state === "down" || state === "paused" || state === "open") return state;
  return "in an unknown state";
}

/** The state of one account in the scope's words, and its dot's tone. */
export function accountState(a: RouteAccount, now: Date): { text: string; tone: Tone } {
  const classification = accountClassification(a);
  const models = modelLimits(a, now);
  let text: string;
  let tone: Tone = null;
  switch (classification) {
    case "paused":
      text = a.until ? `Paused by you until ${whenLocal(a.until, now)}` : "Paused by you until you clear it";
      break;
    case "limited":
      tone = "warn";
      if (!a.until) text = "Limit reached";
      else if (a.basis === "estimated") text = `Limit reached, back about ${whenLocal(a.until, now)} (a guess)`;
      else text = `Limit reached, back ${aheadWords(a.until, now)}`;
      break;
    case "down":
      tone = "err";
      text = `Down: ${a.reason || "out of service"}`;
      break;
    case "open":
      if (models.length) return { text: `${models.join("; ")}; other models open`, tone: "warn" };
      return { text: "Open", tone: "ok" };
    case "probably_open":
      // A reset check's outcome is the state's own words, as the CLI's line
      // says it (fix round 2, R2-2): never "not checked yet" beside "Checked by Khipu".
      text = a.reset_check?.reason
        ? `Its reset time passed ${pastWords(a.until, now)}; ${midSentence(a.reset_check.reason)}`
        : a.configured === false
          ? `Probably open: its limit was due to end ${pastWords(a.until, now)}; Khipu can't check this account: it isn't tracked in Accounts and routing`
          : a.checkable === false
            ? `Its reset time passed ${pastWords(a.until, now)}; this account can't be checked without using a turn`
            : `Probably open: its limit was due to end ${pastWords(a.until, now)}, not checked yet`;
      break;
    default:
      if (a.silent) text = "Unknown: its only reporter stopped";
      else if (a.last_seen?.cleared) text = `Unknown: last cleared by you ${ageWords(a.last_seen.at, now)}`;
      else if (a.last_seen) text = `Unknown: last seen ${stateWord(a.last_seen.state)} ${ageWords(a.last_seen.at, now)}`;
      else if (a.usage_setup) text = a.usage_setup[0].toUpperCase() + a.usage_setup.slice(1);
      else text = "Unknown: no report yet";
  }
  if (models.length) text += `; ${models.join("; ")}`;
  return { text, tone };
}

/** Who said so, in words: "Read from Claude", "From Claude's own message,
 *  seen by your Project A job runner", "Reported by a Claude Code
 *  conversation in project-a". */
function witnessWords(w: RouteWitness, harness: string, state: string): string {
  const cli = cliName(harness);
  switch (w.source) {
    case "probe":
      if (w.observed) return `Read from ${cli}'s last turn`;
      return state === "down" ? "Khipu's plan check" : `Read from ${cli}`;
    case "check":
      return "Checked by Khipu";
    case "owner":
      return w.cleared ? "Cleared by you" : "Set by you";
    case "runner": {
      const name = w.written_by?.name;
      const runner = name ? `your ${name} job runner` : "a job runner";
      return w.message_time ? `From ${cli}'s own message, seen by ${runner}` : `Seen by ${runner}`;
    }
    default: {
      const by = w.by || "a conversation";
      if (w.message_time) return `From ${cli}'s own message, reported by ${by}`;
      return w.until_default ? `Reported without a time by ${by}` : `Reported by ${by}`;
    }
  }
}

/** The plan figure an open account's row adds: "58% of its week used". */
export type PlanFigure = { pct: number; window: string };

const WINDOW_WORDS: Record<string, string> = { five_hour: "five hours", weekly: "week", daily: "day" };

export function windowWords(name: string | undefined): string {
  return (name && WINDOW_WORDS[name]) || (name ?? "window").replace(/_/g, " ");
}

/** Who said so and when, plus what else the row should say. `reportDefault`
 *  is the record's `report_default_limit` (seconds), named when the limit
 *  shown came from a report without a time. */
export function accountSource(
  a: RouteAccount,
  now: Date,
  plan?: PlanFigure | null,
  reportDefault: number = 30 * 60,
  includeResetReason: boolean = true,
): string {
  const parts: string[] = [];
  const classification = accountClassification(a);
  const w = classification === "unknown" ? a.last_seen : a.witness;
  if (w) {
    const when = saidWhen(w.at, now);
    let said = `${witnessWords(w, a.harness, classification)}${when ? ` ${when}` : ""}`;
    if (classification === "limited" && w.until_default && w.source !== "runner" && !w.message_time) {
      said += `; Khipu keeps a report like that for ${spanWords(reportDefault)}`;
    }
    parts.push(said);
  }
  // A probably-open row already says its reset check's reason in its state.
  if (a.reset_check?.reason && includeResetReason) {
    if (classification !== "probably_open") parts.push(a.reset_check.reason);
  }
  else if (classification === "limited" && a.checkable === false) parts.push("can't be checked without using a turn");
  if (classification === "open" && a.witness?.source === "probe" && plan) {
    parts.push(`${Math.round(plan.pct)}% of its ${plan.window} used`);
  }
  return parts.join(" · ");
}

/** Which report shows over the other, when two sources disagree: the
 *  source of the one shown and whether its time is the CLI's own. */
function strongerWords(source: string | undefined, ownTime: boolean, harness: string): string {
  const cli = cliName(harness);
  if (source === "probe") return `Khipu's read of ${cli}`;
  if (source === "check") return "Khipu's check";
  if (source === "owner") return "Your own word";
  if (ownTime) return `${cli}'s own message`;
  return "The stronger report";
}

function weakerWords(d: RouteReport): string {
  if (d.source === "report") return "a conversation's report";
  if (d.source === "runner") return "a job runner's report";
  return "the other report";
}

function disagreementSentence(
  d: RouteReport,
  harness: string,
  subject: string,
  stronger: string,
  shown: string,
  now: Date,
): string {
  const by = d.by ? capitalise(d.by) : capitalise(witnessWords(d, harness, d.state ?? ""));
  return (
    `${by} reported ${subject} ${stateWord(d.state)} ${saidWhen(d.at, now)}. ` +
    `${stronger} outranks ${weakerWords(d)}, so ${shown} shows here; conversations are told both.`
  );
}

/** "A Claude Code conversation in project-b reported it open at 11:31 AM.
 *  Claude's own message outranks a conversation's report, so the limit
 *  shows here; conversations are told both." Null when nothing disagrees. */
export function disagreementWords(a: RouteAccount, now: Date): string | null {
  const d = a.disagreement;
  if (!d) return null;
  const shown = a.state === "limited" ? "the limit" : `"${stateWord(a.state)}"`;
  const w = a.witness;
  return disagreementSentence(d, a.harness, "it", strongerWords(w?.source, !!w?.message_time, a.harness), shown, now);
}

/** The same for each model whose own limit a report disagrees with: "A
 *  Claude Code conversation in project-c reported Opus open at 1:00 PM.
 *  Claude's own message outranks a conversation's report, so the Opus limit
 *  shows here; conversations are told both." Only models whose limit is
 *  shown (its end is ahead), as the attention item names them. */
export function modelDisagreementWords(a: RouteAccount, now: Date): string[] {
  return (a.models ?? [])
    .filter((m) => m.disagreement && ahead(m.until, now))
    .map((m) => {
      const stronger = strongerWords(m.source, m.basis === "cli" && m.source !== "probe", a.harness);
      return disagreementSentence(m.disagreement!, a.harness, m.model, stronger, `the ${m.model} limit`, now);
    });
}

/** A hold's end and author: "Ends tomorrow 9:40 AM · set by a Claude Code
 *  conversation in project-a". */
export function holdSource(h: RouteHold, now: Date): string {
  const end = h.until ? `Ends ${whenLocal(h.until, now)}` : "Until you clear it";
  const by = h.owner ? `set by you ${saidWhen(h.at, now)}`.trim() : `set by ${h.by || "a conversation"}`;
  return `${end} · ${by}`;
}

/** The reminder the owner gets on his own old hold (the CLI sets `remind`
 *  once it is older than the reminder setting, 90 days by default): "You
 *  set this 120 days ago. Is it still needed?" Null otherwise. */
export function holdReminder(h: RouteHold, now: Date): string | null {
  if (!h.remind) return null;
  const at = parse(h.at);
  const days = at ? Math.floor((now.getTime() - at.getTime()) / DAY_MS) : null;
  const when = days != null && days > 0 ? `${days} day${days === 1 ? "" : "s"} ago` : "a while ago";
  return `You set this ${when}. Is it still needed?`;
}

const HOLD_REASON_WORDS: Record<string, string> = {
  machine_busy: "The Mac is busy",
  disk_low: "The disk is low",
  keep: "Kept for one purpose",
  outage: "An outage",
};

export function holdWords(h: RouteHold): string {
  return h.text || HOLD_REASON_WORDS[h.reason] || "A hold";
}

/** Disagreements older than the warn time (0 means never). */
export function longDisagreements(view: RouteView, now: Date): { label: string; since: string }[] {
  const after = view.warn_after?.disagreement ?? 30 * 60;
  if (!after) return [];
  const out: { label: string; since: string }[] = [];
  for (const a of view.accounts ?? []) {
    const sides: [string, RouteReport | null | undefined][] = [[a.label, a.disagreement]];
    // Only a model whose limit the card shows (its end is ahead), so the
    // item always points at a note on the card (modelDisagreementWords).
    for (const m of a.models ?? []) if (ahead(m.until, now)) sides.push([`${a.label} (${m.model})`, m.disagreement]);
    for (const [label, d] of sides) {
      const began = parse(d?.since ?? d?.at);
      if (began && now.getTime() - began.getTime() > after * 1000) out.push({ label, since: began.toISOString() });
    }
  }
  return out;
}

export function brokenChecks(view: RouteView): RuleCheck[] {
  return (view.rule_checks ?? []).filter((c) => c.status === "broken");
}

/** The broken checks grouped by rule, in the order each rule first appears:
 *  one rule broken on two lanes is one broken rule, not two. */
export function brokenRules(view: RouteView): { rule: number; checks: RuleCheck[] }[] {
  const out: { rule: number; checks: RuleCheck[] }[] = [];
  for (const c of brokenChecks(view)) {
    const g = out.find((x) => x.rule === c.rule);
    if (g) g.checks.push(c);
    else out.push({ rule: c.rule, checks: [c] });
  }
  return out;
}

const BROKEN_LEAD = "A standing rule may be broken: ";

// Why a check could not judge any lane, by check (`routing_sources.rule_checks`).
const CANNOT_SEE: Record<string, string> = {
  fast_tier: "the model is set inside the launch script, which Khipu doesn't read.",
  paid_xai: "the launch environment is set inside the launch script, which Khipu doesn't read.",
  grok_confirm: "the confirming lane's kind isn't set in your job runner's settings.",
  grok_private: "Khipu hasn't read your job runner's list of added work yet.",
};

// What each check is about, for a rule with several checks.
const CHECK_FOR: Record<string, string> = {
  fast_tier: "for models ending in -fast",
  paid_xai: "for the paid xAI API",
  grok_private: "for Grok on private work",
  grok_confirm: "for re-checks of Grok work",
};

const FILE_WORDS: Record<string, string> = { settings: "settings", events: "list of added work" };
const NOT_NOW = new Set(["not_read", "failed", "stale"]);

const count = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

// What caps left out, runner by runner, so a part-read check is never a pass.
function leftOutLine(cells: RuleCell[]): string {
  const parts = cells
    .filter((c) => c.outcome === "incomplete" && c.skipped)
    .map((c) => {
      const k = c.skipped ?? {};
      const items = [
        k.lanes ? count(k.lanes, "lane", "lanes") : "",
        k.kinds ? count(k.kinds, "lane whose kind it can't name", "lanes whose kind it can't name") : "",
        k.fix_lanes ? count(k.fix_lanes, "default fix lane", "default fix lanes") : "",
        k.added ? count(k.added, "line of added work", "lines of added work") : "",
        k.unfinished_line ? "an unfinished last line of added work" : "",
      ].filter(Boolean);
      const list = items.length > 1 ? `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}` : items[0] ?? "some input";
      return `${list} in ${c.runner}`;
    });
  return `Khipu left out ${parts.join("; and ") || "part of what it reads"}.`;
}

// Why a check has not run now: each runner it could not use, and each of
// that runner's files it never read, couldn't read, or read too long ago.
function notNowLine(cells: RuleCell[], nowMs: number): string {
  const parts = cells
    .filter((c) => NOT_NOW.has(c.outcome))
    .flatMap((c) =>
      Object.entries(c.reads ?? {}).flatMap(([key, r]) => {
        const file = `${c.runner}'s ${FILE_WORDS[key] ?? key}`;
        const ago = fmtAgoSince(r?.good_at, nowMs);
        if (!r || r.read === false) return [`Khipu has never read ${file}`];
        if (r.failed) return [`Khipu couldn't read ${file}${ago ? `; it last read them ${ago}` : ""}`];
        if (r.stale) return [`Khipu last read ${file} ${ago ?? "a long time ago"}`];
        return [];
      }),
    );
  return `${parts.join(" and ") || "Khipu can't tell how recent its read was"}.`;
}

type Line = { text: string; tone: "ok" | "warn" | "" };

// One result's line. `f` names the check ("" for a rule with one check);
// `top` says "nothing wrong found" (every check passed), else an ok check
// says "passed".
function resultLine(result: RuleResult | RuleCheckResult, f: string, nowMs: number, top: boolean): Line {
  const by = `Checked by Khipu${f}`;
  const cells = "cells" in result && result.cells ? result.cells : "checks" in result && result.checks ? result.checks.flatMap((c) => c.cells ?? []) : [];
  switch (result.result) {
    case "broken": {
      const words = (result.text ?? "").replace(BROKEN_LEAD, "");
      const more = result.more ? ` ${result.more === 1 ? "1 more lane" : `${result.more} more lanes`} too.` : "";
      return { text: `${by}: may be broken.${words ? ` ${words.charAt(0).toUpperCase()}${words.slice(1)}` : ""}${more}`, tone: "warn" };
    }
    case "ok": {
      const ago = fmtAgoSince(result.at, nowMs);
      const n = result.unchecked ?? 0;
      const rest = n ? ` ${n === 1 ? "1 lane" : `${n} lanes`} couldn't be checked: ${CANNOT_SEE[result.check ?? ""] ?? CANNOT_SEE.fast_tier}` : "";
      const head = top ? `${by} in your job runners' settings: nothing wrong found` : `${by}: passed`;
      const scanned = cells.filter((c) => c.scan?.done);
      const lines = scanned.reduce((sum, c) => sum + (c.scan?.lines ?? 0), 0);
      const covered = scanned.length ? ` It read all ${lines.toLocaleString("en-US")} ${lines === 1 ? "line" : "lines"} of added work.` : "";
      return { text: `${head}${ago ? `, ${ago}` : ""}.${rest}${covered}`, tone: "ok" };
    }
    case "not_read":
    case "failed":
    case "stale":
      return { text: `${by}, but not now: ${notNowLine(cells, nowMs)}`, tone: "" };
    case "checking": {
      const parts = cells
        .filter((c) => c.outcome === "checking")
        .map((c) => {
          const pct = c.scan?.size ? Math.floor(((c.scan.read ?? 0) / c.scan.size) * 100) : 0;
          return `${pct}% of ${c.runner}'s list of added work`;
        });
      return { text: `${by}, but still checking: Khipu has read ${parts.join(" and ") || "part of your job runners' added work"} so far.`, tone: "" };
    }
    case "incomplete":
      return { text: `${by}, but not fully: ${leftOutLine(cells)}`, tone: "" };
    case "cannot_check":
      return { text: `${by}, but not here: ${CANNOT_SEE[result.check ?? ""] ?? CANNOT_SEE.fast_tier}`, tone: "" };
    case "no_lane":
      return { text: `${by}, but not yet: none of your job runners has a lane this rule is about.`, tone: "" };
    case "no_runner":
      return { text: `${by}, but not yet: add a job runner folder so it has something to read.`, tone: "" };
    case "unreadable": // an older Khipu's word for not_read, with no cells to name
      return { text: `${by}, but not now: Khipu can't read your job runners' settings.`, tone: "" };
    default:
      return { text: `${by}. Reading what the last check found…`, tone: "" };
  }
}

const NOT_CHECKED: Line = { text: "Passed to every conversation as written. Khipu has no way to check this one.", tone: "" };

/** The line under a standing rule: what Khipu's check found, or that Khipu
 *  has no check for it. `tone` is "ok", "warn" or "" (grey). A rule Khipu
 *  checks says "nothing wrong found" only from a result of "ok", which the
 *  CLI gives only after every check ran and read fresh files. */
export function ruleResultLine(watched: boolean, result: RuleResult | undefined, nowMs: number = Date.now()): Line {
  if (!watched) return NOT_CHECKED;
  return result ? resultLine(result, "", nowMs, true) : resultLine({ rule: 0, result: "" }, "", nowMs, true);
}

/** Every line under a standing rule: one per check, each from its own
 *  cells, naming its check when the rule has several. "Nothing wrong found"
 *  only when every check passed; else a passing check says "passed". */
export function ruleResultLines(watched: boolean, result: RuleResult | undefined, nowMs: number = Date.now()): Line[] {
  const checks = result?.checks ?? [];
  if (!watched || !result || !checks.length) return [ruleResultLine(watched, result, nowMs)];
  const allOk = checks.every((c) => c.result === "ok");
  if (checks.length === 1) return [resultLine(checks[0], "", nowMs, allOk)];
  return checks.map((c) => resultLine(c, ` ${CHECK_FOR[c.check] ?? ""}`.trimEnd(), nowMs, allOk));
}

// Words a rule commonly opens with that are not names, so "Never a -fast
// tier." reads "never a -fast tier" after "Your rule says". Any other first
// word keeps its letter as written: "Grok", "Khipu" or "Codex" is a name.
const RULE_OPENERS = new Set([
  "a", "after", "all", "always", "an", "any", "ask", "at", "avoid", "before", "both", "check", "do",
  "don't", "each", "every", "for", "from", "give", "if", "in", "keep", "leave", "let", "make", "never",
  "no", "not", "nothing", "on", "once", "one", "only", "prefer", "put", "read", "route", "run", "send",
  "some", "stop", "the", "treat", "until", "use", "wait", "when", "while", "with", "without", "write",
]);

/** A rule's text as it reads after "Your rule says": the closing period
 *  dropped and a common opening word lower-cased; a name stays as written. */
export function ruleWords(text: string): string {
  const t = text.trim().replace(/\.$/, "");
  const first = t.split(/\s/, 1)[0] ?? "";
  const plain = /^[A-Z][a-z']*[,:;]?$/.test(first) && RULE_OPENERS.has(first.replace(/[,:;]$/, "").toLowerCase());
  return plain ? t.charAt(0).toLowerCase() + t.slice(1) : t;
}

export function quietRunners(view: RouteView): RouteRunner[] {
  return (view.sources?.runners ?? []).filter((r) => r.silent || r.unreadable);
}

/** "Your Project B job runner stopped reporting at 2:12 PM." */
export function runnerWords(r: RouteRunner, now: Date): string {
  if (r.silent) {
    return r.heartbeat_at
      ? `Your ${r.name} job runner stopped reporting ${pastWords(r.heartbeat_at, now)}.`
      : `Your ${r.name} job runner has not reported yet.`;
  }
  return r.unreadable_since
    ? `Khipu can't read ${r.name}'s files since ${whenLocal(r.unreadable_since, now)}.`
    : `Khipu can't read ${r.name}'s files.`;
}

/** What one changed entity now says: "Grok Build limit reached (a guess)". */
function entityWords(entity: string, view: RouteView): string {
  const [kind, ...rest] = entity.split(":");
  const ident = rest.join(":");
  if (kind === "account") {
    const a = (view.accounts ?? []).find((x) => x.key === ident);
    if (!a) return "an account is no longer tracked";
    const word: Record<string, string> = {
      limited: a.basis === "estimated" ? "limit reached (a guess)" : "limit reached",
      open: "open",
      paused: "paused by you",
      down: "down",
      probably_open: "probably open",
    };
    return `${a.label} ${word[a.state] ?? "unknown"}`;
  }
  if (kind === "hold") {
    const h = (view.holds ?? []).find((x) => x.id === ident);
    return h ? `hold on ${h.scope_label} set` : "a hold ended";
  }
  if (kind === "rule") return `standing rule ${ident} changed`;
  if (entity === "note") return view.note ? "your note changed" : "your note ended";
  if (kind === "runner") {
    const r = (view.sources?.runners ?? []).find((x) => x.name === ident);
    return r?.silent ? `${ident} job runner stopped reporting` : `${ident} job runner reporting`;
  }
  if (kind === "lane") {
    const [runner, lane] = ident.split("/");
    return `${runner}'s ${lane} lane changed`;
  }
  if (entity === "table") return "the routing table's current-state section changed";
  return entity;
}

/** "Last change 3:00 PM: Grok Build limit reached (a guess)." or null. */
export function lastChangeWords(view: RouteView, now: Date): string | null {
  const changes = view.changes ?? [];
  const last = changes[changes.length - 1];
  if (!last || !last.entities?.length) return null;
  const first = capitalise(entityWords(last.entities[0], view));
  const more = last.entities.length - 1;
  return `Last change ${whenLocal(last.at, now)}: ${first}${more > 0 ? ` and ${more} more` : ""}.`;
}

export const ROUTE_READ_FAILED = "Khipu could not read the routing record. It tries again every minute.";
const ROUTE_READ_UNREADABLE = "The routing record gave an answer Khipu could not read.";

/** The record, read whenever `active` turns true, again every minute while
 *  it stays so, and on `load()`. A failed read keeps the last good view;
 *  the card then says since when it is old. */
export function useRouting(active: boolean) {
  const [data, setData] = useState<RouteView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [lastOkAt, setLastOkAt] = useState<string | null>(null);
  // True from the moment `active` turns true until the read it starts
  // settles, so the card does not call a record old while that read is under
  // way. Set during render, not in an effect, so not even one frame shows it.
  const [wasActive, setWasActive] = useState(active);
  const [firstRead, setFirstRead] = useState(active);
  if (active !== wasActive) {
    setWasActive(active);
    if (active) setFirstRead(true);
  }
  const inflight = useRef<Promise<void> | null>(null);

  const load = useCallback((fresh = false): Promise<void> => {
    // After a write, a read already under way may predate it: wait for it,
    // then read again.
    if (inflight.current) return fresh ? inflight.current.then(() => load(false)) : inflight.current;
    setLoading(true);
    const p = (async () => {
      try {
        const res = await callCli<RouteView>("khipu_route_view");
        if (!res.ok) {
          console.error("khipu route failed:", res.error);
          setError(ROUTE_READ_FAILED);
          return;
        }
        const view = res.data;
        if (view && typeof view === "object" && Array.isArray(view.accounts)) {
          setData(view);
          setError(null);
          setLastOkAt(new Date().toISOString());
        } else {
          setError(ROUTE_READ_UNREADABLE);
        }
      } finally {
        inflight.current = null;
        setLoading(false);
        setFirstRead(false);
      }
    })();
    inflight.current = p;
    return p;
  }, []);

  useEffect(() => {
    if (!active) return;
    void load();
    const tick = window.setInterval(() => void load(), MINUTE_MS);
    return () => window.clearInterval(tick);
  }, [active, load]);

  return { data, error, loading, lastOkAt, firstRead, load };
}

export type RoutingState = ReturnType<typeof useRouting>;

/** "Khipu's record has not been refreshed since 2:58 PM" when a read failed
 *  and the last good one is older than the record's own bound (5 minutes by
 *  default), or when the record's own `checked_at` is past that bound. Never
 *  while the first read after Home turns active again is under way: the view
 *  is old then only because nobody was looking. */
export function notRefreshedWords(
  state: { data: RouteView | null; lastOkAt: string | null; error?: string | null; firstRead?: boolean },
  now: Date,
): string | null {
  const { data, lastOkAt, error, firstRead } = state;
  if (!data || !lastOkAt || !data.record || firstRead) return null;
  const bound = data.warn_after?.not_refreshed ?? 5 * 60;
  if (!bound) return null;
  const past = (iso: string | null | undefined) => {
    const at = parse(iso);
    return !!at && now.getTime() - at.getTime() > bound * 1000;
  };
  if (!(error && past(lastOkAt)) && !past(data.checked_at)) return null;
  return `Khipu's record has not been refreshed since ${whenLocal(data.checked_at ?? lastOkAt, now)}`;
}

// ---------------------------------------------------------------------------
// The owner's priority and the spend pace (phase 4b). The priority is the
// owner's own order, shown as he gave it; the pace is arithmetic on figures
// Khipu read. Neither is a choice Khipu makes.
// ---------------------------------------------------------------------------

/** "Claude second until Sat 3:00 PM, then Codex, then Claude". */
export function priorityOrder(p: RoutePriority, now: Date): string {
  return p.entries
    .map((e) => (e.until ? `${e.label} until ${whenLocal(e.until, now)}` : e.label))
    .join(", then ");
}

/** "Priority: Claude second until Sat 3:00 PM, then Codex". */
export function priorityHeadline(p: RoutePriority, now: Date): string {
  const whole = p.until ? ` (all of it until ${whenLocal(p.until, now)})` : "";
  return `Priority: ${priorityOrder(p, now)}${whole}`;
}

/** Who set a priority that waits for the owner, in his words. */
export function priorityPendingWords(p: RoutePriority): string {
  return `Set by ${p.pending_by || "a conversation"} at your request; you haven't confirmed it yet.`;
}

/** "when read 1 h ago" for figures that may be out of date. */
export function readWhen(fact: PlanFact, now: Date): string {
  const ago = fmtAgoSince(fact.figures_at, now.getTime());
  return ago ? `when read ${ago}` : "when last read";
}

/** A pace in words: "1.7", "12" or "less than 0.01". */
export function rateWords(pace: number): string {
  if (pace >= 10) return pace.toFixed(0);
  if (pace >= 0.1) return String(Math.round(pace * 10) / 10);
  return pace >= 0.01 ? String(Math.round(pace * 100) / 100) : "less than 0.01";
}

/** "88% of its week left, resets Sat 3:00 PM: about 1.7% an hour to use it
 *  all (a lot left, little time)", or why there is no pace. */
export function paceWords(fact: PlanFact | null | undefined, now: Date): string | null {
  if (!fact) return null;
  if (!fact.readable) return capitalise(fact.why || "plan figures can't be read");
  if (fact.stale) {
    return `${fact.left_pct}% of its ${fact.window} left ${readWhen(fact, now)}, resets ${whenLocal(fact.resets_at, now)}: ` +
      "these figures may be out of date, so no pace is given";
  }
  const head = `${fact.left_pct}% of its ${fact.window} left, resets ${whenLocal(fact.resets_at, now)}`;
  if (!fact.left_pct || fact.left_pct <= 0) return `${head}: nothing left to use until then`;
  return `${head}: about ${rateWords(fact.pace_pct_per_hour ?? 0)}% an hour to use it all (${fact.words})`;
}

export function deliveryReason(reason: string): string {
  const words: Record<string, string> = {
    "ack-unshown": "a change was acknowledged before it was shown",
    "budget-stall": "a change is still owed past its delivery budget",
    "lease-expired-twice": "a change's delivery lease expired twice",
    "prompt-skips": "too many prompts skipped routing",
    "cross-mac-stale": "the cross-Mac copy is behind",
    "gateway-stale": "the cloud copy is behind",
    "unmatched-limit": "an unmatched lane has a limit for over an hour",
  };
  return words[reason] ?? reason.replace(/-/g, " ");
}

const DELIVERY_HARNESS_WORDS: ReadonlyArray<readonly [string, string]> = [
  ["claude_code", "Claude"],
  ["claude-code", "Claude"],
  ["claude", "Claude"],
  ["codex", "Codex"],
  ["grok", "Grok"],
  ["cursor", "Cursor"],
  ["glm", "GLM"],
  ["muse", "Muse"],
  ["deepseek", "DeepSeek"],
  ["aegis", "Aegis"],
  ["antigravity", "Antigravity"],
  ["gemini", "Gemini"],
];

function deliveryHarness(identity: string): string | null {
  const lower = identity.toLowerCase();
  for (const [prefix, word] of DELIVERY_HARNESS_WORDS) {
    if (lower === prefix) return word;
    if (lower.startsWith(prefix) && ":_-/.".includes(lower.charAt(prefix.length))) return word;
  }
  return null;
}

/** A harness code (`claude_code`, `codex`, ...) in owner words, or null. */
export function deliveryHarnessName(harness: string | null | undefined): string | null {
  return harness ? deliveryHarness(harness) : null;
}

export type DeliveryConversationLabel = {
  conversation: string;
  harness: string | null;
  component: string;
};

/** Owner-facing labels for delivery rows. Raw identities and component ids
 * can hold session ids, so they are never rendered: a conversation is named
 * by its stable first-appearance order, its harness, and a generic component
 * word only. */
export function deliveryConversationLabels(
  rows: ReadonlyArray<{ identity?: unknown; component?: unknown }> | null | undefined,
): DeliveryConversationLabel[] {
  const numbers = new Map<string, number>();
  return (rows ?? []).map((row) => {
    const identity = typeof row?.identity === "string" ? row.identity : "";
    if (!numbers.has(identity)) numbers.set(identity, numbers.size + 1);
    const component = typeof row?.component === "string" ? row.component : "";
    return {
      conversation: `Conversation ${numbers.get(identity)}`,
      harness: identity ? deliveryHarness(identity) : null,
      component:
        component === "main"
          ? "main"
          : component === "mcp"
            ? "MCP helper"
            : component === "agent" || component.startsWith("agent:")
              ? "a helper agent"
              : "another component",
    };
  });
}
