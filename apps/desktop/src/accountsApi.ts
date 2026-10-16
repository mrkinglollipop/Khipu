/* Finding and adding accounts in the app (docs/plans/2026-10-15-settings-clarity.md,
   "Phase 9", slice 9C). The one typed interface every account surface uses:
   discovery and the owner's Check (`khipu usage --discover`), one read of an
   unsaved account (`--try`), Forget which logins conversations used, where
   Khipu finds each CLI (`cli_path.<cli>`, `node_folder`), and the account
   writes.

   Every account write is slice 9B's (`khipu accounts ...`: track, rename,
   stop, move, decline, offer again, the automatic-tracking switch), each
   carrying the version of the list it read (`khipu accounts status`). Track
   and Sign in now save through `khipu_accounts_track`, the one save path; no
   surface here writes `usage_accounts` any other way. */

import { useCallback, useEffect, useRef, useState } from "react";
import { callCli } from "./settingsCli";
import type { CliResult } from "./settingsCli";
import type { UsageAccount, UsageAccountEntry } from "./PlansCard";

// ---------------------------------------------------------------------------
// Shapes: exactly what the CLI prints (packages/cli/khipu/discover.py, and
// 9B's packages/cli/khipu/accounts.py).
// ---------------------------------------------------------------------------

export type CliId = "claude" | "codex" | "grok" | "muse";
export const CLI_IDS: CliId[] = ["claude", "codex", "grok", "muse"];

export type CandidateState = "signed_in" | "not_signed_in" | "couldnt_check" | "not_checked" | "pending";

export type Candidate = {
  id: string;
  cli: CliId;
  cli_name: string;
  /** "~/.claude-client" under the home folder, else the full path; null for Muse. */
  path: string | null;
  default: boolean;
  sources: string[];
  name: string | null;
  first_seen: string | null;
  last_checked: string | null;
  state: CandidateState;
  reason: string | null;
  plan: string | null;
  conversations_7d: number | null;
  needs_owner_check: boolean;
  offer_on_home: boolean;
};

export type LastRun = {
  at: string | null;
  checked?: number;
  couldnt_check?: number;
  timed_out?: number;
  left_for_later?: number;
  not_checked?: number;
};

export type CliState = {
  cli: CliId;
  cli_name: string;
  path: string | null;
  state: "installed" | "cant_run" | "not_installed";
  version: string | null;
  reason: string | null;
};

export type CheckAnswer = { ok: boolean; candidate?: Candidate; error?: string };

export type DiscoverResult = {
  ok: boolean;
  candidates: Candidate[];
  last_run: LastRun | null;
  looking: boolean;
  clis?: Partial<Record<CliId, CliState>>;
  check?: CheckAnswer;
  /** 9B: what automatic tracking tracked on this run. */
  auto_tracked?: Array<{ key: string; label: string; via?: string }>;
};

export type TryResult = { ok: boolean; default?: boolean; row?: UsageAccount | null; error?: string };

/** One account as 9B's `khipu_accounts_track` takes it (`AccountEntry`). */
export type AccountEntryIn = { harness: string; home?: string | null; label?: string | null };

export type DeclinedEntry = { cli: CliId; path: string | null; identity?: string; why: string; at: string | null };

export type Mention = { kind: "rule"; n: number } | { kind: "note" };

/** 9B's `khipu accounts status`. */
export type AccountsStatus9B = {
  ok: true;
  version: string;
  none: boolean;
  auto_track: boolean;
  value: unknown;
  accounts: Array<UsageAccountEntry & { key?: string }>;
  user_home: string | null;
  declined: DeclinedEntry[];
  request: unknown;
  lane_repairs?: unknown[];
};

export type TrackResult9B = {
  ok: true;
  version: string;
  accounts: Array<{ key: string; label: string }>;
  rows: UsageAccount[];
  record_error?: string;
};

export type RenameResult9B = {
  ok: true;
  version: string;
  key: string;
  label: string;
  was: string;
  mentions: Mention[];
  lanes: number;
};

export type StopResult9B = {
  ok: true;
  version: string;
  key: string;
  label: string;
  none: boolean;
  priority: "removed" | "kept" | "pending" | null;
  pause: "ended" | "kept" | null;
  lanes_removed: Array<{ runner: string; lane: string }>;
  lanes_kept: Array<{ runner: string; lane: string }>;
  holds_ended: string[];
  holds_kept: string[];
  mentions: Mention[];
};

/** `khipu config`'s `usage_accounts` on main (9B adds version and none). */
export type ConfigAccounts = {
  value?: unknown;
  source: "file" | "default" | "none";
  accounts: UsageAccountEntry[];
  user_home?: string | null;
  version?: string;
  none?: boolean;
};

export type CliSettings = Record<string, { value: string | null; usable: boolean }>;

// ---------------------------------------------------------------------------
// Words
// ---------------------------------------------------------------------------

/** The program's name, as Settings says it: "Claude Code", "Codex". */
export const CLI_NAME: Record<CliId, string> = { claude: "Claude Code", codex: "Codex", grok: "Grok Build", muse: "Muse" };
/** The service's name, as the dialog's step 1 says it: "Claude", "Codex". */
export const SERVICE_NAME: Record<CliId, string> = { claude: "Claude", codex: "Codex", grok: "Grok Build", muse: "Muse" };

/** Download pages checked into the code (plan section 1): Claude Code and
 *  Codex only; Grok Build and Muse get no link until one is verified. */
export const DOWNLOAD_PAGE: Partial<Record<CliId, string>> = {
  claude: "https://code.claude.com/docs/en/setup",
  codex: "https://developers.openai.com/codex/cli",
};

export function cliName(cli: string | null | undefined): string {
  return CLI_NAME[(cli ?? "") as CliId] ?? cli ?? "its program";
}

export function serviceName(cli: string | null | undefined): string {
  return SERVICE_NAME[(cli ?? "") as CliId] ?? cli ?? "This service";
}

/** "1 folder", "2 folders". */
export function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function listOf(names: string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** Why a folder or a CLI couldn't be checked, as the rest of a sentence that
 *  starts with the program: "Codex didn't answer in time". Every fixed code
 *  `discover.py` and `usage.py` give; never the CLI's own text. */
export function reasonWords(cli: string | null | undefined, code: string | null | undefined): string {
  const name = cliName(cli);
  switch (code) {
    case "timeout":
      return `${name} didn't answer in time`;
    case "start_failed":
    case "cant_start":
      return `Khipu couldn't start ${name}`;
    case "node_missing":
      return `${name} needs Node, and Khipu couldn't find it`;
    case "not_installed":
      return `${name} is not installed on this Mac`;
    case "bad_reply":
      return `${name} answered in a way Khipu can't read`;
    case "internal_error":
      return "Khipu couldn't run the check";
    case "cli_error":
      return `${name} reported an error when asked`;
    case "no_plan_limits":
      return `${name} reports no plan limits for it`;
    case "no_plan_figures":
      return `${name} didn't send its plan figures`;
    case "login_folder_missing":
      return "its login folder is not there";
    case "no_answer":
      return `${name} stopped without an answer`;
    default:
      if (code && /^(exit|signal)_\d+$/.test(code)) return `${name} stopped without an answer`;
      return `${name} couldn't be asked`;
  }
}

/** Why an owner's Check (or Choose another folder…) did not run. */
export function checkErrorWords(code: string | null | undefined, cli?: string | null): string {
  switch (code) {
    case "bad_folder":
      return "That isn't a folder Khipu can use. Choose a folder in your home folder, or give its full path.";
    case "folder_not_found":
      return "Khipu can't find that folder.";
    case "not_a_login_folder":
      return "That folder's name doesn't look like a Claude Code, Codex or Grok Build login (such as .claude-work). Choose the login folder itself.";
    case "already_tracked":
      return "Khipu already tracks that login.";
    case "turned_down":
      return "You chose Not this one for that login. Choose Offer again under Not offered again first.";
    case "looking":
      return "Khipu is looking for accounts right now. Try again in a moment.";
    case "timeout":
      return `${cli ? cliName(cli) : "The program"} didn't answer in time. Try again.`;
    default:
      return "Khipu couldn't check that folder. Try again in a minute.";
  }
}

/** The help line under Check (decision 20): its consent, said per CLI. */
export function checkHelp(cli: string): string {
  const name = cliName(cli);
  return `Check asks ${name} whether this login is signed in. ${name} may create a settings file in that folder when it is asked.`;
}

function whenWords(iso: string | null | undefined, now: Date): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const sameDay = d.toDateString() === now.toDateString();
  return sameDay ? time : `${d.toLocaleDateString([], { month: "short", day: "numeric" })}, ${time}`;
}

/** "Last looked 3:40 PM; 1 folder could not be checked". Null before the
 *  first run. */
export function lastRunLine(run: LastRun | null | undefined, now: Date = new Date()): string | null {
  if (!run) return null;
  const when = whenWords(run.at, now);
  const parts = [when ? `Last looked ${when}` : "Looked before"];
  const failed = run.couldnt_check ?? 0;
  if (failed) parts.push(`${plural(failed, "folder", "folders")} could not be checked`);
  const later = run.left_for_later ?? 0;
  if (later) parts.push(`${plural(later, "folder", "folders")} left for the next look`);
  return parts.join("; ");
}

/** "Found 20 min ago" from a first-seen time. */
export function foundAgo(iso: string | null | undefined, now: Date = new Date()): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const mins = Math.max(0, Math.round((now.getTime() - d.getTime()) / 60_000));
  if (mins < 1) return "Found just now";
  if (mins < 60) return `Found ${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `Found ${plural(hours, "hour", "hours")} ago`;
  return `Found ${plural(Math.round(hours / 24), "day", "days")} ago`;
}

export function planWord(plan: string | null | undefined): string | null {
  const p = (plan ?? "").trim();
  return p ? `${p.charAt(0).toUpperCase()}${p.slice(1)}` : null;
}

/** "4 of your conversations this week ran on it", or null when not counted. */
export function conversationsWords(n: number | null | undefined): string | null {
  if (n == null) return null;
  if (n === 0) return "No conversation this week ran on it";
  return `${n} of your conversations this week ran on it`;
}

/** The login's folder as a row names it. */
export function folderWords(c: Pick<Candidate, "path" | "default" | "cli">): string {
  if (c.cli === "muse") return "one login on this Mac";
  return c.path ?? "its default login";
}

// ---------------------------------------------------------------------------
// The commands
// ---------------------------------------------------------------------------

/** One discovery run (`now`: Look for accounts now, which ignores the
 *  once-a-day wait). */
export function discoverAccounts(now = false): Promise<CliResult<DiscoverResult>> {
  return callCli<DiscoverResult>("khipu_accounts_discover", { now });
}

/** The owner's Check on one found folder, or a folder they picked. */
export function checkFolder(folder: string): Promise<CliResult<DiscoverResult>> {
  return callCli<DiscoverResult>("khipu_accounts_check", { folder });
}

/** One read of an account that is not saved (the dialog's step 3). */
export function tryAccount(harness: string, folder: string | null): Promise<CliResult<TryResult>> {
  return callCli<TryResult>("khipu_accounts_try", { harness, folder });
}

export function forgetLoginPaths(): Promise<CliResult<{ ok: boolean; finished?: boolean; note?: string }>> {
  return callCli("khipu_accounts_forget_login_paths");
}

/** `cli_path.<cli>` or `node_folder`; an empty value clears it. */
export function saveCliSetting(key: string, value: string | null): Promise<CliResult<Record<string, unknown>>> {
  return value ? callCli("khipu_config_set", { key, value }) : callCli("khipu_config_unset", { key });
}

/** A discovery answer as the CLI printed it, or a `{ok: true}` with no list
 *  (an older Khipu): a list is always there. */
export function normalDiscover(d: DiscoverResult | null | undefined): DiscoverResult | null {
  if (!d || typeof d !== "object") return null;
  return {
    ...d,
    candidates: Array.isArray(d.candidates) ? d.candidates : [],
    last_run: d.last_run && typeof d.last_run === "object" ? d.last_run : null,
    looking: d.looking === true,
  };
}

// ---------------------------------------------------------------------------
// The account list as 9B reads it (`khipu accounts status`): every write
// carries its `version`
// ---------------------------------------------------------------------------

export type Support =
  | { state: "reading" }
  | { state: "here"; status: AccountsStatus9B }
  | { state: "error"; error: string };

function shaped9B(d: unknown): d is AccountsStatus9B {
  const s = d as AccountsStatus9B | null;
  return !!s && typeof s === "object" && typeof s.version === "string" && Array.isArray(s.accounts);
}

export async function readSupport(): Promise<Support> {
  const res = await callCli<AccountsStatus9B>("khipu_accounts_status");
  if (res.ok) return shaped9B(res.data) ? { state: "here", status: res.data } : { state: "error", error: "unreadable account status" };
  return { state: "error", error: res.error };
}

export function useAccountsSupport(active: boolean) {
  const [support, setSupport] = useState<Support>({ state: "reading" });
  const seq = useRef(0);
  const reload = useCallback(async () => {
    const mine = ++seq.current;
    const s = await readSupport();
    if (mine === seq.current) setSupport(s);
    return s;
  }, []);
  useEffect(() => {
    if (active) void reload();
  }, [active, reload]);
  return { support, reload };
}

/** Words for a control that needs a later update (slice 9D), by what it does. */
export const COMING = {
  signInNew: "Signing in to another account from Khipu comes in a later update.",
  codeSignIn: "Signing in with a code comes in a later update; sign in from the program itself for now.",
} as const;

// ---------------------------------------------------------------------------
// Discovery as a hook
// ---------------------------------------------------------------------------

/** A Check answers for one folder and may leave out where each CLI is; the
 *  last full run's `clis` stay until the next run reads them again. */
function withClis(d: DiscoverResult, prev: DiscoverResult | null): DiscoverResult {
  return d.clis || !prev?.clis ? d : { ...d, clis: prev.clis };
}

/** Discovery for a screen: one run when `active` turns on and every
 *  `intervalMs` after (Home: 6 hours), Look for accounts now, and the owner's
 *  Check. `running` is true while a run this screen started is going. */
export function useDiscovery(active: boolean, intervalMs?: number) {
  const [data, setData] = useState<DiscoverResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [checking, setChecking] = useState<string | null>(null);
  const seq = useRef(0);
  const run = useCallback(async (now = false) => {
    const mine = ++seq.current;
    setRunning(true);
    const res = await discoverAccounts(now);
    if (mine !== seq.current) return null;
    setRunning(false);
    if (res.ok) {
      const d = normalDiscover(res.data);
      setData(d);
      setError(null);
      return d;
    }
    console.error("khipu usage --discover failed:", res.error);
    setError("Khipu couldn't look for accounts just now.");
    return null;
  }, []);
  const check = useCallback(async (folder: string) => {
    setChecking(folder);
    try {
      const res = await checkFolder(folder);
      if (!res.ok) {
        console.error("khipu usage --discover --check failed:", res.error);
        const d = normalDiscover(res.refusal as DiscoverResult | undefined);
        if (d && d.candidates.length) setData((prev) => withClis(d, prev));
        return { ok: false as const, error: (d?.check?.error ?? null) as string | null };
      }
      const d = normalDiscover(res.data);
      if (d) setData((prev) => withClis(d, prev));
      return d?.check?.ok ? { ok: true as const, candidate: d.check.candidate ?? null } : { ok: false as const, error: d?.check?.error ?? null };
    } finally {
      setChecking(null);
    }
  }, []);
  useEffect(() => {
    if (!active) return;
    void run(false);
    if (!intervalMs) return;
    const t = window.setInterval(() => void run(false), intervalMs);
    return () => window.clearInterval(t);
  }, [active, intervalMs, run]);
  const looking = running || data?.looking === true;
  return { data, error, running, looking, checking, run, check, setData };
}

// ---------------------------------------------------------------------------
// The one save path (plan section 5): Track…, the dialog's Save, Sign in now
// ---------------------------------------------------------------------------

export type NewAccount = { harness: string; home: string | null; label: string };

export type SaveProblem = {
  code: string;
  message: string;
  field: "name" | "home" | null;
  /** login_taken: the name of the account that already reads this login. */
  other?: string | null;
  /** The list moved after it was read (or no version was read): Reload. */
  reload?: boolean;
};

export type SaveOutcome =
  | { ok: true; harness: string; home: string | null; label: string; key: string | null }
  | { ok: false; problem: SaveProblem };

const NOT_SAVED = "Khipu could not save the account. Try again in a minute.";

/** A save refused because the account list moved after it was read. */
export const LIST_MOVED =
  "The account list was changed somewhere else after Khipu read it, so nothing was saved. Reload to see it as it is now, then try again.";

/** The version of the list as this screen read it, or null when none was
 *  read: a save never goes out without one (the CLI and the app's commands
 *  refuse it). */
export function readVersion(support: Support): string | null {
  const v = support.state === "here" ? support.status.version : null;
  return typeof v === "string" && v.trim() ? v : null;
}

/** Whether a refused save was refused for its version: the CLI's codes, or
 *  the app's own command refusing a missing or malformed one (no code). */
export function versionRefused(res: { ok: boolean; error?: string; refusal?: Record<string, unknown> }): boolean {
  if (res.ok) return false;
  const code = res.refusal?.code;
  if (code === "version_moved" || code === "version_needed") return true;
  const text = res.error ?? "";
  return !res.refusal && (text.includes("needs the version it read") || text.includes("not a version Khipu knows"));
}

/** 9B's refusal codes in Khipu's own words; `entry` is the account sent. */
export function trackRefusalWords(code: string, entry: NewAccount, other?: string | null): SaveProblem {
  switch (code) {
    case "name_taken":
      return { code, field: "name", message: `Another account is already named ${entry.label}. Choose a different name.` };
    case "login_taken":
      return { code, field: "home", other: other ?? null,
        message: other ? `Khipu already tracks this login as ${other}.` : "Khipu already tracks this login." };
    case "name_is_cli":
      return { code, field: "name", message: "That name is a program's own name. Choose another, such as one with the folder in it." };
    case "name_has_bar":
      return { code, field: "name", message: "A name can't hold |." };
    case "name_is_id":
      return { code, field: "name", message: "That name is another account's id. Choose another." };
    case "not_one_line":
      return { code, field: "name", message: "The name must be one line of plain text." };
    case "too_long":
      return { code, field: "name", message: "That name is too long: use 64 characters or fewer." };
    case "name_needed":
      return { code, field: "name", message: "Give this account a name." };
    case "folder_not_found":
    case "no_such_folder":
      return { code, field: "home", message: "Khipu can't find that login folder." };
    case "version_moved":
    case "version_needed":
      return { code, field: null, message: LIST_MOVED, reload: true };
    case "owner_only":
      return { code, field: null, message: "Only you can change the account list, from the app. Nothing was saved." };
    default:
      return { code: code || "not_saved", field: null, message: NOT_SAVED };
  }
}

/** Saves one new account through 9B's one save path, `khipu_accounts_track`
 *  (the defaults the app showed are written out first when no list is
 *  saved). The CLI keeps every stored entry, a hand-edited invalid one too;
 *  only the new entry is sent. Nothing else runs when the save is refused. */
export async function saveNewAccount(
  entry: NewAccount,
  support: Support,
  shownDefaults: UsageAccountEntry[] | null,
): Promise<SaveOutcome> {
  const known = support.state === "here" ? support : await readSupport();
  if (known.state !== "here") {
    if (known.state === "error") console.error("khipu accounts status failed:", known.error);
    return { ok: false, problem: { code: "not_read", field: null, message: "Khipu couldn't read the account list, so nothing was saved. Try again in a minute." } };
  }
  const st = known.status;
  const expect = readVersion(known);
  if (!expect) return { ok: false, problem: trackRefusalWords("version_needed", entry) };
  const args: Record<string, unknown> = {
    entries: [{ harness: entry.harness, home: entry.home, label: entry.label }],
    expect,
  };
  if (st.value == null && !st.none) {
    args.shownDefaults = (shownDefaults ?? st.accounts)
      .filter((e) => !e.invalid)
      .map((e) => ({ harness: e.harness, home: e.home ?? null, label: e.label }));
  }
  const res = await callCli<TrackResult9B>("khipu_accounts_track", args);
  if (!res.ok) {
    console.error("khipu accounts track refused:", res.error);
    const r = res.refusal ?? {};
    const code = versionRefused(res) ? "version_moved" : typeof r.code === "string" ? r.code : "";
    const otherIdx = typeof r.other === "number" ? r.other : null;
    const other = otherIdx != null ? (st.accounts[otherIdx]?.label ?? null) : null;
    return { ok: false, problem: trackRefusalWords(code, entry, other) };
  }
  const added = res.data.accounts?.[0];
  const key = added?.key ?? null;
  const home = key ? key.slice(key.indexOf("|") + 1) || null : entry.home;
  return { ok: true, harness: entry.harness, home, label: added?.label ?? entry.label, key };
}

export type SignInNowOutcome =
  | { saved: false; problem: SaveProblem }
  | { saved: true; account: Extract<SaveOutcome, { ok: true }>; signIn: CliResult<{ ok?: boolean; error?: string; row?: UsageAccount }> };

/** Sign in now on a login Khipu doesn't track yet: save it first (the one
 *  save path), then the unchanged `khipu_account_sign_in` for it. A refused
 *  save starts no sign-in. */
export async function signInNow(
  entry: NewAccount,
  support: Support,
  shownDefaults: UsageAccountEntry[] | null,
): Promise<SignInNowOutcome> {
  const saved = await saveNewAccount(entry, support, shownDefaults);
  if (!saved.ok) return { saved: false, problem: saved.problem };
  const signIn = await callCli<{ ok?: boolean; error?: string; row?: UsageAccount }>("khipu_account_sign_in", {
    harness: saved.harness,
    home: saved.home,
  });
  return { saved: true, account: saved, signIn };
}

// ---------------------------------------------------------------------------
// Name rules (plan section 5), checked before anything is sent
// ---------------------------------------------------------------------------

const BARE = new Set(["claude", "codex", "grok", "muse"]);

/** Why `name` can't be used, in plain words, or null. `isDefault`: the CLI's
 *  own default login, which may keep the plain name. `taken` are the names in
 *  use (any case). */
export function nameProblem(name: string, isDefault: boolean, taken: string[]): string | null {
  const n = name.trim();
  if (!n) return "Give this account a name.";
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(n)) return "The name must be one line of plain text.";
  if (n.length > 64) return "That name is too long: use 64 characters or fewer.";
  if (n.includes("|")) return "A name can't hold |.";
  const clash = taken.find((t) => t.trim().toLowerCase() === n.toLowerCase());
  if (clash) return `Another account is already named ${clash}. Choose a different name${suggestionWords(n, taken)}.`;
  if (BARE.has(n.toLowerCase()) && !isDefault) return "That name is a program's own name. Choose another, such as one with the folder in it.";
  return null;
}

/** ", such as Codex work" when a free name is easy to suggest. */
function suggestionWords(n: string, taken: string[]): string {
  const lower = new Set(taken.map((t) => t.trim().toLowerCase()));
  for (const extra of ["work", "2", "3", "4"]) {
    const s = `${n} ${extra}`;
    if (!lower.has(s.toLowerCase()) && s.length <= 64) return `, such as ${s}`;
  }
  return "";
}

/** The name a login gets by default: discovery's, else "<CLI> <folder>". */
export function suggestedName(c: { cli: string; path: string | null; default: boolean; name?: string | null }): string {
  if (c.name) return c.name;
  const svc = serviceName(c.cli);
  if (c.default || !c.path) return svc;
  const base = c.path.replace(/\/+$/, "").split("/").pop() ?? "";
  const m = base.match(/^\.(?:claude|codex|grok)-(.+)$/);
  return `${svc} ${m ? m[1] : base.replace(/^\./, "")}`.slice(0, 64);
}
