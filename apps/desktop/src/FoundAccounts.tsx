/* Found accounts and managing tracked ones (docs/plans/2026-10-15-settings-clarity.md,
   "Phase 9", sections 1, 4, 7 and 9; mocks add-account-found.html,
   add-account-manage.html and add-account-states.html).

   Settings → Accounts and routing → Accounts gains: Look for accounts now and
   Add an account… with the last run's line and Forget which logins
   conversations used; a Found row per undecided login (Found, Found not
   checked with Check, Couldn't check with Check again, Is this X, moved?);
   Not offered again with Offer again; Track new accounts automatically;
   where Khipu finds each program; Rename… and Stop tracking… on each account,
   the folder-not-found and signed-out lines; and "No accounts tracked".

   Home gains one Found card (or the automatic-tracking card) between What's
   new and the attention callouts. Close hides it on this Mac until a
   different folder is found; it turns nothing down.

   Every write is slice 9B's `khipu accounts ...`; a control that writes is
   off only until the account list has been read. */

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ChevronRight, Loader2 } from "lucide-react";
import { Tag } from "./ui";
import { callCli } from "./settingsCli";
import {
  COMING,
  LIST_MOVED,
  checkErrorWords,
  checkHelp,
  cliName,
  conversationsWords,
  forgetLoginPaths,
  foundAgo,
  lastRunLine,
  listOf,
  nameProblem,
  planWord,
  plural,
  readVersion,
  reasonWords,
  saveCliSetting,
  serviceName,
  useAccountsSupport,
  useDiscovery,
  versionRefused,
  CLI_IDS,
} from "./accountsApi";

/** The refusal line of a save refused for its version, with Reload. */
function MovedError({ text, stale, busy, onReload }: { text: string; stale: boolean; busy?: boolean; onReload?: () => void }) {
  return (
    <p className="set-error" role="alert">
      {text}
      {stale && onReload ? (
        <>
          {" "}
          <button type="button" className="sm" disabled={busy} onClick={onReload}>
            Reload
          </button>
        </>
      ) : null}
    </p>
  );
}
import type {
  Candidate,
  CliId,
  CliSettings,
  CliState,
  ConfigAccounts,
  DeclinedEntry,
  DiscoverResult,
  LastRun,
  Mention,
  RenameResult9B,
  StopResult9B,
  Support,
} from "./accountsApi";
import { AccountsDialog, AddAccountDialog, pickPath } from "./AddAccountDialog";
import type { AddAccountStart } from "./AddAccountDialog";
import type { UsageAccount, UsageAccountEntry } from "./PlansCard";
import type { RouteAccount, RouteView } from "./routing";
import "./accounts.css";

// ---------------------------------------------------------------------------
// Per-Mac memory for Home's cards (decision 4: nothing here syncs)
// ---------------------------------------------------------------------------

const CLOSED_KEY = "khipu.accounts.foundClosed";
const QUIET_KEY = "khipu.accounts.quiet";
const AUTO_KEY = "khipu.accounts.autoCards";

function readList<T>(key: string, ok: (v: unknown) => v is T): T[] {
  try {
    const raw = window.localStorage.getItem(key);
    if (raw == null) return [];
    const v: unknown = JSON.parse(raw);
    return Array.isArray(v) ? v.filter(ok) : [];
  } catch {
    return [];
  }
}

function writeList(key: string, v: unknown[]): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(v));
  } catch {
    // Not remembered on this Mac this time; the card shows again next time.
  }
}

const isString = (v: unknown): v is string => typeof v === "string";
/** A login left unticked in setup and its conversation count then. */
export type QuietLogin = { id: string; seen: number };
const isQuiet = (v: unknown): v is QuietLogin | string =>
  typeof v === "string" || (!!v && typeof v === "object" && typeof (v as QuietLogin).id === "string" && typeof (v as QuietLogin).seen === "number");
type AutoCard = { key: string; label: string; path?: string | null; conversations?: number | null };
const isAutoCard = (v: unknown): v is AutoCard =>
  !!v && typeof v === "object" && typeof (v as AutoCard).key === "string" && typeof (v as AutoCard).label === "string";

/** Logins left unticked in setup: Settings only, not Home, until a
 *  conversation uses them (decision 12). An entry without a count reads as
 *  "not used since" until Home first sees its count. */
export function quietLogins(): QuietLogin[] {
  return readList(QUIET_KEY, isQuiet).map((q) => (typeof q === "string" ? { id: q, seen: Number.MAX_SAFE_INTEGER } : q));
}

export function addQuietLogins(logins: Array<{ id: string; conversations_7d?: number | null }>): void {
  if (!logins.length) return;
  const now = new Map(quietLogins().map((q) => [q.id, q]));
  logins.forEach((c) => now.set(c.id, { id: c.id, seen: c.conversations_7d ?? 0 }));
  writeList(QUIET_KEY, [...now.values()]);
}

/** The week's count is rolling, so it can fall: keep each quiet login's
 *  count at the lowest seen, so that any later conversation raises it. */
function lowerQuiet(d: DiscoverResult | null | undefined): QuietLogin[] {
  const quiet = quietLogins();
  let moved = false;
  const next = quiet.map((q) => {
    const c = (d?.candidates ?? []).find((x) => x.id === q.id);
    const n = c?.conversations_7d ?? 0;
    if (!c || n >= q.seen) return q;
    moved = true;
    return { id: q.id, seen: n };
  });
  if (moved) writeList(QUIET_KEY, next);
  return next;
}

// ---------------------------------------------------------------------------
// Which finds show where
// ---------------------------------------------------------------------------

/** The Found rows in Settings: every undecided login except one that is not
 *  signed in (that one shows only in the dialog's list). */
export function foundRowsOf(d: DiscoverResult | null | undefined): Candidate[] {
  return (d?.candidates ?? []).filter((c) => c.state !== "not_signed_in");
}

/** Home's Found card: signed-in finds a conversation used, minus the logins
 *  setup left unticked that no conversation used since. */
export function homeOffersOf(d: DiscoverResult | null | undefined, quiet: QuietLogin[] = []): Candidate[] {
  return (d?.candidates ?? []).filter((c) => {
    if (!c.offer_on_home) return false;
    const q = quiet.find((x) => x.id === c.id);
    return !q || (c.conversations_7d ?? 0) > q.seen;
  });
}

/** A tracked account whose login folder is gone, paired with an untracked
 *  signed-in login of the same CLI: "Is this X, moved?". */
export type Moved = { candidate: Candidate; account: { key: string | null; label: string; home: string | null } };

export function movedPairs(rows: UsageAccount[], candidates: Candidate[]): Moved[] {
  const out: Moved[] = [];
  const used = new Set<string>();
  for (const r of rows) {
    const code = r.ok === false ? r.error : null;
    if (code !== "login_folder_missing" || !r.harness) continue;
    const c = candidates.find((x) => x.cli === r.harness && x.state === "signed_in" && !x.default && !used.has(x.id));
    if (!c) continue;
    used.add(c.id);
    out.push({ candidate: c, account: { key: r.key ?? null, label: r.account || serviceName(r.harness), home: r.home ?? null } });
  }
  return out;
}

/** "2 found" for the Accounts group's closed line (rule 6 amended: a neutral
 *  tag, never a problem). */
export function foundCount(d: DiscoverResult | null | undefined): number {
  return foundRowsOf(d).length;
}

// ---------------------------------------------------------------------------
// Pieces
// ---------------------------------------------------------------------------

function ComingNote({ id, text }: { id: string; text: string }) {
  return (
    <span className="aa-coming" id={id}>
      {text}
    </span>
  );
}

function loginTitle(c: Candidate): ReactNode {
  if (c.cli === "muse") return <>Muse, one login on this Mac</>;
  if (c.default) {
    return (
      <>
        {serviceName(c.cli)} default login <span className="aa-mono">{c.path}</span>
      </>
    );
  }
  return (
    <>
      {serviceName(c.cli)} {c.state === "signed_in" ? "account" : "login"} at <span className="aa-mono">{c.path}</span>
    </>
  );
}

function shownPath(c: Candidate): string {
  return c.cli === "muse" ? "Muse" : c.path ?? `the ${serviceName(c.cli)} default login`;
}

/** The Found rows, as listitems inside the Accounts list. */
export function FoundRows({
  candidates,
  moved = [],
  support,
  checking,
  onTrack,
  onCheck,
  onDecline,
  onMove,
  now = new Date(),
}: {
  candidates: Candidate[];
  moved?: Moved[];
  support: Support;
  checking?: string | null;
  onTrack: (c: Candidate) => void;
  onCheck: (c: Candidate) => void;
  onDecline: (c: Candidate) => void;
  onMove?: (m: Moved) => void;
  now?: Date;
}) {
  const uid = useId().replace(/:/g, "");
  const here = support.state === "here";
  if (!candidates.length) return null;
  const decline = (c: Candidate) => (
    <button type="button" className="sm" disabled={!here}
      aria-label={`Not this one: never offer ${shownPath(c)} again`} onClick={() => onDecline(c)}>
      Not this one
    </button>
  );
  return (
    <div className="aa-found-list" role="list" aria-label="Found accounts">
      {candidates.map((c) => {
        const m = moved.find((x) => x.candidate.id === c.id);
        if (m) {
          return (
            <div key={c.id} className="aa-found" role="listitem">
              <Tag tone="accent">Found</Tag>
              <span className="aa-ft">
                <b>
                  Is this {m.account.label}, moved? <span className="aa-mono">{c.path}</span>
                </b>
                <span className="t2">
                  {m.account.label}'s folder {m.account.home ?? "its login folder"} is gone, and this new {serviceName(c.cli)} login is
                  signed in{planWord(c.plan) ? ` (${planWord(c.plan)})` : ""}.
                </span>
              </span>
              <span className="aa-btns">
                <button type="button" className="sm primary" disabled={!here}
                  aria-label={`Yes, ${c.path} is ${m.account.label}, moved`} onClick={() => onMove?.(m)}>
                  Yes, it moved
                </button>
                <button type="button" className="sm" aria-label={`No, track ${c.path} as a new account`} onClick={() => onTrack(c)}>
                  No, it's new
                </button>
              </span>
            </div>
          );
        }
        if (c.state === "not_checked" || c.state === "pending") {
          const helpId = `${uid}-help-${c.id.replace(/[^a-zA-Z0-9_-]/g, "_")}`;
          const owner = c.state === "not_checked";
          return (
            <div key={c.id} className="aa-found" role="listitem">
              <Tag>Found, not checked</Tag>
              <span className="aa-ft">
                <b>{loginTitle(c)}</b>
                <span className="t2">
                  {owner
                    ? `Its folder name looks like a ${cliName(c.cli)} login; no conversation has used it, so Khipu has not asked ${cliName(c.cli)} about it yet.`
                    : "Khipu checks it on its next look."}
                </span>
                {owner ? (
                  <span className="t2" id={helpId}>
                    {checkHelp(c.cli)}
                  </span>
                ) : null}
              </span>
              <span className="aa-btns">
                {owner ? (
                  <button type="button" className="sm primary" aria-describedby={helpId} disabled={checking != null}
                    aria-busy={checking === c.path || undefined} aria-label={`Check the ${cliName(c.cli)} login at ${c.path}`}
                    onClick={() => onCheck(c)}>
                    {checking === c.path ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                    Check
                  </button>
                ) : null}
                {decline(c)}
              </span>
            </div>
          );
        }
        if (c.state === "couldnt_check") {
          return (
            <div key={c.id} className="aa-found" role="listitem">
              <Tag tone="warn" dot>Couldn't check</Tag>
              <span className="aa-ft">
                <b>
                  {serviceName(c.cli)} login at <span className="aa-mono">{shownPath(c)}</span>
                </b>
                <span className="t2">
                  Couldn't check {shownPath(c)}: {reasonWords(c.cli, c.reason)}. Khipu tries again tomorrow, or now.
                </span>
              </span>
              <span className="aa-btns">
                <button type="button" className="sm" disabled={checking != null} aria-busy={checking === (c.path ?? c.id) || undefined}
                  aria-label={`Check ${shownPath(c)} again`} onClick={() => onCheck(c)}>
                  {checking === (c.path ?? c.id) ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                  Check again
                </button>
              </span>
            </div>
          );
        }
        const facts = [
          `Signed in${planWord(c.plan) ? ` (${planWord(c.plan)})` : ""}.`,
          conversationsWords(c.conversations_7d) ? `${conversationsWords(c.conversations_7d)}.` : null,
          foundAgo(c.first_seen, now) ? `${foundAgo(c.first_seen, now)}.` : null,
        ].filter(Boolean);
        return (
          <div key={c.id} className="aa-found" role="listitem">
            <Tag tone="accent">Found</Tag>
            <span className="aa-ft">
              <b>{loginTitle(c)}</b>
              <span className="t2">{facts.join(" ")}</span>
            </span>
            <span className="aa-btns">
              <button type="button" className="sm primary" aria-label={`Track the ${serviceName(c.cli)} account at ${shownPath(c)}`}
                onClick={() => onTrack(c)}>
                Track…
              </button>
              {decline(c)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

/** The Accounts group's bar: the last run, Forget, Look for accounts now and
 *  Add an account…. */
export function AccountsGroupBar({
  looking,
  lastRun,
  onLookNow,
  onAdd,
  onForget,
  now = new Date(),
}: {
  looking: boolean;
  lastRun: LastRun | null;
  onLookNow: () => void;
  onAdd: () => void;
  onForget?: () => Promise<string | null>;
  now?: Date;
}) {
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const line = lastRunLine(lastRun, now);
  return (
    <>
      <div className="aa-groupbar">
        {looking ? (
          <span className="aa-wait" role="status">
            <span className="aa-spin" aria-hidden />
            Looking for accounts…
          </span>
        ) : (
          <span className="t2">
            {line ? `${line}.` : "Khipu reads each account's plan with the login that account already has. It never reads passwords or keys."}
            {onForget && line ? (
              <>
                {" "}
                <button type="button" className="link sm" aria-expanded={asking} onClick={() => setAsking((v) => !v)}>
                  Forget which logins conversations used
                </button>
              </>
            ) : null}
          </span>
        )}
        <span className="spacer" />
        <button type="button" className="sm" disabled={looking} onClick={onLookNow}>
          Look for accounts now
        </button>
        <button type="button" className="sm primary" onClick={onAdd}>
          Add an account…
        </button>
      </div>
      {asking && onForget ? (
        <div className="aa-confirm" role="group" aria-label="Forget which logins conversations used">
          <span>
            Khipu forgets which login each saved conversation ran under. Found logins that came only from conversations go
            away until a conversation uses them again. Your accounts and conversations are not changed.
          </span>
          <button type="button" className="sm" disabled={busy} aria-busy={busy || undefined}
            onClick={async () => {
              setBusy(true);
              try {
                setSaid(await onForget());
                setAsking(false);
              } finally {
                setBusy(false);
              }
            }}>
            Forget them
          </button>
          <button type="button" className="sm" onClick={() => setAsking(false)}>
            Cancel
          </button>
        </div>
      ) : null}
      {said ? (
        <p className="set-help" role="status">
          {said}
        </p>
      ) : null}
    </>
  );
}

/** Not offered again: the logins the owner said no to or stopped tracking. */
export function NotOfferedAgain({
  support,
  onOfferAgain,
}: {
  support: Support;
  onOfferAgain: (e: DeclinedEntry) => void;
}) {
  const id = useId().replace(/:/g, "");
  const [open, setOpen] = useState(false);
  const here = support.state === "here";
  const list = here ? support.status.declined ?? [] : [];
  const meta = here ? (list.length ? ` · ${plural(list.length, "login", "logins")} you said no to` : " · none") : "";
  return (
    <div className="fold disclose" data-open={open}>
      <button type="button" className="disclosure fold-btn" aria-expanded={open} aria-controls={`${id}-body`} onClick={() => setOpen((v) => !v)}>
        <ChevronRight size={14} className="disclosure-chevron" aria-hidden />
        <span>
          Not offered again<span className="meta">{meta}</span>
        </span>
      </button>
      <div className="fold-body" id={`${id}-body`} hidden={!open}>
        <div className="disclose-body">
          {!here ? (
            <p className="set-help">{support.state === "error" ? "Khipu couldn't read this list just now." : "Reading…"}</p>
          ) : list.length === 0 ? (
            <p className="set-help">No logins. When you choose Not this one or stop tracking an account, its login is listed here.</p>
          ) : (
            <div className="aa-rows" role="list" aria-label="Logins Khipu won't offer again">
              {list.map((e, i) => {
                const where = e.cli === "muse" ? "Muse" : e.path ?? `${serviceName(e.cli)} default login`;
                const when = e.at ? new Date(e.at) : null;
                const date = when && !Number.isNaN(when.getTime()) ? ` on ${when.toLocaleDateString([], { month: "short", day: "numeric" })}` : "";
                return (
                  <div key={`${e.cli}|${e.path ?? ""}|${i}`} className="aa-row" role="listitem">
                    <span className="aa-mono">{where}</span>
                    <span className="aa-what">
                      {serviceName(e.cli)} · {e.why}
                      {date}
                    </span>
                    <span className="aa-acts">
                      <button type="button" className="sm" aria-label={`Offer ${where} again`} onClick={() => onOfferAgain(e)}>
                        Offer again
                      </button>
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/** Track new accounts automatically (per Mac, off by default). */
export function AutoTrackSwitch({
  support,
  onChange,
  setup = false,
  value,
}: {
  support: Support;
  onChange: (on: boolean) => void;
  /** Setup's wording. */
  setup?: boolean;
  /** Setup holds its own unsaved value. */
  value?: boolean;
}) {
  const id = useId().replace(/:/g, "");
  const here = support.state === "here";
  const on = value ?? (here ? support.status.auto_track : false);
  return (
    <div className={setup ? "opt-list acct-switch" : "opt-list aa-switch"}>
      <div className="opt-row">
        <input id={`${id}-auto`} className="switch" type="checkbox" role="switch" checked={on} disabled={!here}
          aria-describedby={`${id}-what`} onChange={(e) => onChange(e.target.checked)} />
        <div className="opt-text">
          <div className="opt-title">
            <label className="opt-name" htmlFor={`${id}-auto`}>
              Track new accounts automatically
            </label>
            <Tag>{on ? "On" : "Off"}</Tag>
          </div>
          <p id={`${id}-what`} className="opt-what">
            {setup
              ? "Off: when Khipu later finds another signed-in account your conversations use, it asks on Home first."
              : "When Khipu finds a signed-in account that your conversations use, it starts reading its plan and tells you on Home, with Undo. Off: it asks first."}
          </p>
        </div>
      </div>
    </div>
  );
}

/** Where Khipu finds each program: the owner's settings and the CLIs it
 *  can't start, each with Change…, Clear or Check again. */
export function CliSettingsRows({
  clis,
  settings,
  onSet,
  onCheckAgain,
  checking = false,
}: {
  clis: Partial<Record<CliId, CliState>> | null | undefined;
  settings: CliSettings | null | undefined;
  onSet: (key: string, value: string | null, opener: HTMLElement | null) => void;
  onCheckAgain: () => void;
  checking?: boolean;
}) {
  const node = settings?.node_folder;
  const rows = CLI_IDS.filter((c) => settings?.[`cli_path.${c}`]?.value || clis?.[c]?.state === "cant_run");
  if (!rows.length && !node?.value) return null;
  return (
    <div className="set-field">
      <span className="set-label">Where Khipu finds each program</span>
      <div className="aa-rows" role="list" aria-label="Where Khipu finds each program">
        {rows.map((c) => {
          const set = settings?.[`cli_path.${c}`];
          const st = clis?.[c];
          const broken = st?.state === "cant_run" && st.reason !== "node_missing" && !set?.value && st.reason === "start_failed";
          return (
            <div key={c} className="aa-row" role="listitem">
              <span>{cliName(c)}</span>
              <span className="aa-what">
                {broken ? (
                  <span>Install looks incomplete: its program file is missing. Reinstall it from its maker.</span>
                ) : (
                  <span>
                    Program: {set?.value ? `${set.value} (set by you)${set.usable ? "" : "; Khipu can't run it"}` : "found automatically"}
                  </span>
                )}
                {st?.state === "cant_run" && !broken ? <span>Khipu can't start it: {reasonWords(c, st.reason)}.</span> : null}
              </span>
              <span className="aa-acts">
                <button type="button" className="sm" aria-label={`Change the program for ${cliName(c)}`}
                  onClick={(e) => onSet(`cli_path.${c}`, "pick", e.currentTarget)}>
                  Change…
                </button>
                {set?.value ? (
                  <button type="button" className="sm" aria-label={`Clear the program for ${cliName(c)}`}
                    onClick={(e) => onSet(`cli_path.${c}`, null, e.currentTarget)}>
                    Clear
                  </button>
                ) : null}
                {st?.state === "cant_run" ? (
                  <button type="button" className="sm" disabled={checking} aria-label={`Check ${cliName(c)} again`} onClick={onCheckAgain}>
                    Check again
                  </button>
                ) : null}
              </span>
            </div>
          );
        })}
        {node?.value ? (
          <div className="aa-row" role="listitem">
            <span>Node</span>
            <span className="aa-what">
              <span>
                Node folder: {node.value} (set by you){node.usable ? "" : "; Khipu can't find node there"}
              </span>
            </span>
            <span className="aa-acts">
              <button type="button" className="sm" aria-label="Change the Node folder" onClick={(e) => onSet("node_folder", "pick", e.currentTarget)}>
                Change…
              </button>
              <button type="button" className="sm" aria-label="Clear the Node folder" onClick={(e) => onSet("node_folder", null, e.currentTarget)}>
                Clear
              </button>
            </span>
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** Plans, Home and Accounts once the owner stopped tracking every account
 *  (decision 14). */
export function NoAccountsTracked({ onAdd }: { onAdd: () => void }) {
  return (
    <div className="empty">
      <b>No accounts tracked</b>
      <span>You stopped tracking every account on this Mac. Add one to see how much of its plan is left.</span>
      <button type="button" className="sm" style={{ marginTop: "var(--s2)" }} onClick={onAdd}>
        Add an account…
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// One tracked account: its folder-not-found and signed-out lines, Rename…
// and Stop tracking…
// ---------------------------------------------------------------------------

function daysAgo(iso: string | null | undefined, now: Date): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const days = Math.floor((now.getTime() - d.getTime()) / 86_400_000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  return `${days} days ago`;
}

/** "Last read yesterday at 6:10 PM · nothing read since" for a login folder
 *  that is gone; "Nothing read from it yet" without a good read. */
function lastReadWords(iso: string | null | undefined, now: Date): string {
  const day = daysAgo(iso, now);
  if (!day || !iso) return "Nothing read from it yet";
  const time = new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return `Last read ${day} at ${time} · nothing read since`;
}

export function AccountExtras({
  label,
  harness,
  home,
  row,
  support,
  onRename,
  onStop,
  onChooseFolder,
  now = new Date(),
}: {
  label: string;
  harness: string;
  home: string | null;
  row?: UsageAccount | null;
  support: Support;
  onRename: () => void;
  onStop: () => void;
  onChooseFolder?: (opener: HTMLElement | null) => void;
  now?: Date;
}) {
  const id = useId().replace(/:/g, "");
  const here = support.state === "here";
  const code = row?.ok === false ? row.error ?? null : null;
  const missing = code === "login_folder_missing";
  const signedOut = code === "not_signed_in" && !!row?.last_good_at;
  const comingNew = `${id}-new-coming`;
  const stop = (
    <button type="button" className="sm" disabled={!here}
      aria-label={`Stop tracking ${label}`} onClick={onStop}>
      Stop tracking…
    </button>
  );
  return (
    <div className="aa-acct-acts">
      {missing ? (
        <span className="aa-state">
          <span className="hdot warn" aria-hidden />
          <span>Khipu can't find the login folder {home ?? "for it"}</span>
          <span className="meta">{lastReadWords(row?.last_good_at, now)}</span>
        </span>
      ) : signedOut ? (
        <span className="aa-state">
          <span className="hdot warn" aria-hidden />
          <span>Signed out; figures from {daysAgo(row?.last_good_at, now)}</span>
        </span>
      ) : null}
      {missing ? (
        <>
          <button type="button" className="sm" disabled={!here}
            aria-label={`Choose the folder for ${label}`} onClick={(e) => onChooseFolder?.(e.currentTarget)}>
            Choose the folder…
          </button>
          {harness === "claude" || harness === "codex" ? (
            <button type="button" className="sm" disabled aria-describedby={comingNew}
              aria-label={`Sign in to ${label} again in a new ${home ?? "login folder"}`}>
              Sign in here again
            </button>
          ) : null}
        </>
      ) : (
        <button type="button" className="sm" disabled={!here}
          aria-label={`Rename ${label}`} onClick={onRename}>
          Rename…
        </button>
      )}
      {stop}
      {missing && (harness === "claude" || harness === "codex") ? <ComingNote id={comingNew} text={COMING.signInNew} /> : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Rename and Stop tracking dialogs (9B's commands)
// ---------------------------------------------------------------------------

export type ManagedAccount = { key: string; label: string; harness: string; home: string | null };

/** What a rename or stop touches, read from the routing record and the lane
 *  map: rules and the note that name it (free text, never rewritten),
 *  holds, lanes, its priority place and pause. */
export type AccountRefs = {
  rules: Array<{ n: number; text: string }>;
  note: boolean;
  holds: Array<{ id: string; text: string; until: string | null }>;
  lanes: string[];
  priority: { place: number; next: string | null } | null;
  pausedUntil: string | null | undefined;
  last: boolean;
};

export function accountRefs(
  a: ManagedAccount,
  view: RouteView | null | undefined,
  laneMap: Record<string, Record<string, string>> | null | undefined,
  trackedCount: number,
): AccountRefs {
  const word = a.label.toLowerCase();
  const rules = (view?.rules ?? []).filter((r) => r.text.toLowerCase().includes(word)).map((r) => ({ n: r.n, text: r.text }));
  const note = !!view?.note?.text && view.note.text.toLowerCase().includes(word);
  const holds = (view?.holds ?? [])
    .filter((h) => {
      const scope = (h as { scope?: { kind?: string; account?: string } }).scope;
      return scope?.account === a.key || h.scope_label === a.label;
    })
    .map((h) => ({ id: h.id, text: h.text || h.reason, until: h.until ?? null }));
  const lanes: string[] = [];
  for (const [runner, m] of Object.entries(laneMap ?? {})) {
    for (const [lane, target] of Object.entries(m ?? {})) if (target === a.key || target === a.label) lanes.push(lane || runner);
  }
  const entries = view?.priority?.entries ?? [];
  const at = entries.findIndex((e) => e.account === a.key);
  const ra: RouteAccount | undefined = (view?.accounts ?? []).find((x) => x.key === a.key);
  return {
    rules,
    note,
    holds,
    lanes,
    priority: at >= 0 ? { place: at + 1, next: entries[at + 1]?.label ?? null } : null,
    pausedUntil: ra?.state === "paused" ? (ra.until ?? null) : undefined,
    last: trackedCount <= 1,
  };
}

function ordinal(n: number): string {
  return ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"][n - 1] ?? `number ${n}`;
}

function whenLocal(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
}

/** Each Edit link closes the dialog first: the editor it opens is behind the
 *  modal, which keeps it inert while the dialog is open. */
function mentionLinks(rules: Array<{ n: number }>, note: boolean, onClose: () => void, onEdit?: (what: Mention) => void): ReactNode {
  return (
    <>
      {rules.map((r) => (
        <button key={r.n} type="button" className="link sm" onClick={() => {
            onClose();
            onEdit?.({ kind: "rule", n: r.n });
          }}>
          Edit rule {r.n}
        </button>
      ))}{" "}
      {note ? (
        <button type="button" className="link sm" onClick={() => {
            onClose();
            onEdit?.({ kind: "note" });
          }}>
          Edit your note
        </button>
      ) : null}
    </>
  );
}

export function RenameDialog({
  open,
  account,
  refs,
  taken,
  support,
  onClose,
  onRenamed,
  onEdit,
  onReload,
}: {
  open: boolean;
  account: ManagedAccount;
  refs: AccountRefs;
  taken: string[];
  support: Support;
  onClose: () => void;
  onRenamed: (r: RenameResult9B) => void;
  onEdit?: (what: Mention) => void;
  /** Reads the account list again after a save refused for its version. */
  onReload?: () => Promise<unknown>;
}) {
  const id = useId().replace(/:/g, "");
  const [name, setName] = useState(account.label);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [stale, setStale] = useState(false);
  const here = support.state === "here";
  const others = taken.filter((t) => t.toLowerCase() !== account.label.toLowerCase());
  const problem = name.trim() === account.label ? null : nameProblem(name, !account.home, others);
  const updated = [
    "Plans, Accounts and routing, your priority list",
    refs.holds.length ? plural(refs.holds.length, "hold", "holds") : null,
    refs.lanes.length ? plural(refs.lanes.length, "lane", "lanes") : null,
  ].filter(Boolean) as string[];
  const named = [
    ...refs.rules.map((r) => `standing rule ${r.n} (“${r.text}”)`),
    refs.note ? "your note" : null,
  ].filter(Boolean) as string[];
  async function rename() {
    if (!here || problem || !name.trim() || name.trim() === account.label) return;
    const expect = readVersion(support);
    setErr(expect ? null : LIST_MOVED);
    setStale(!expect);
    if (!expect) return;
    setBusy(true);
    try {
      const res = await callCli<RenameResult9B>("khipu_accounts_rename", { account: account.key, name: name.trim(), expect });
      if (!res.ok) {
        console.error("khipu accounts rename refused:", res.error);
        const code = typeof res.refusal?.code === "string" ? res.refusal.code : "";
        const moved = versionRefused(res);
        setStale(moved);
        setErr(moved ? LIST_MOVED : code === "name_taken" ? `Another account is already named ${name.trim()}.`
          : "Khipu could not rename it. Try again in a minute.");
        return;
      }
      onRenamed(res.data);
      onClose();
    } finally {
      setBusy(false);
    }
  }
  return (
    <AccountsDialog open={open} labelledBy={`${id}-t`} onCancel={onClose}>
      <h2 id={`${id}-t`}>
        <span className="aa-step">Accounts</span>Rename {account.label}
      </h2>
      <div className="aa-field">
        <label htmlFor={`${id}-n`}>Name</label>
        <input id={`${id}-n`} value={name} maxLength={64} autoComplete="off" spellCheck={false} aria-invalid={problem ? true : undefined}
          aria-describedby={`${id}-h`} onChange={(e) => setName(e.target.value)} />
        <p className="set-help" id={`${id}-h`}>
          {problem ? <span className="aa-err">{problem}</span> : "1 to 64 characters, not the same as another account or a program name."}
        </p>
      </div>
      <ul className="aa-dos">
        <li>
          <b>Updated:</b> {listOf(updated)}. Conversations hear “{account.label} is now called{" "}
          {name.trim() && name.trim() !== account.label ? name.trim() : "its new name"}”.
        </li>
        {named.length ? (
          <li>
            <b>Not changed:</b> {listOf(named)} still {named.length === 1 ? "says" : "say"} {account.label}.{" "}
            {mentionLinks(refs.rules, refs.note, onClose, onEdit)}
          </li>
        ) : null}
        <li>
          <b>Not changed:</b> scripts and agent briefs outside Khipu that use the old name.
        </li>
      </ul>
      {err ? (
        <MovedError text={err} stale={stale} busy={busy}
          onReload={onReload && (async () => { await onReload(); setErr(null); setStale(false); })} />
      ) : null}
      <div className="aa-actions">
        <button type="button" className="aa-left" onClick={onClose}>
          Cancel
        </button>
        <button type="button" className="primary" disabled={!here || busy || !!problem || name.trim() === account.label || !name.trim()}
          aria-busy={busy || undefined} onClick={() => void rename()}>
          Rename
        </button>
      </div>
    </AccountsDialog>
  );
}

export function StopTrackingDialog({
  open,
  account,
  refs,
  support,
  onClose,
  onStopped,
  onEdit,
  onReload,
}: {
  open: boolean;
  account: ManagedAccount;
  refs: AccountRefs;
  support: Support;
  onClose: () => void;
  onStopped: (r: StopResult9B) => void;
  onEdit?: (what: Mention) => void;
  /** Reads the account list again after a save refused for its version. */
  onReload?: () => Promise<unknown>;
}) {
  const id = useId().replace(/:/g, "");
  const here = support.state === "here";
  const [prio, setPrio] = useState(true);
  const [pause, setPause] = useState(true);
  const [lanes, setLanes] = useState(true);
  const [holds, setHolds] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const folder = account.harness === "muse" ? "Muse's one login" : account.home ?? `the default login ${account.harness === "claude" ? "~/.claude" : account.harness === "codex" ? "~/.codex" : "~/.grok"}`;
  async function stop() {
    if (!here) return;
    const expect = readVersion(support);
    setErr(expect ? null : LIST_MOVED);
    setStale(!expect);
    if (!expect) return;
    setBusy(true);
    try {
      const res = await callCli<StopResult9B>("khipu_accounts_stop", {
        account: account.key,
        expect,
        keepPriority: !prio,
        keepPause: !pause,
        keepLanes: !lanes,
        endHolds: holds,
        confirmLast: refs.last,
      });
      if (!res.ok) {
        console.error("khipu accounts stop refused:", res.error);
        const moved = versionRefused(res);
        setStale(moved);
        setErr(moved ? LIST_MOVED : "Khipu could not stop tracking it. Try again in a minute.");
        return;
      }
      onStopped(res.data);
      onClose();
    } finally {
      setBusy(false);
    }
  }
  const title = refs.last ? "Stop tracking your last account?" : `Stop tracking ${account.label}?`;
  return (
    <AccountsDialog open={open} labelledBy={`${id}-t`} onCancel={onClose}>
      <h2 id={`${id}-t`}>
        <span className="aa-step">Accounts</span>
        {title}
      </h2>
      {refs.last ? (
        <p>
          {account.label} is the only account Khipu tracks on this Mac. After this, Khipu reads no plans, and your conversations
          hear about no accounts until you add one.
        </p>
      ) : (
        <p>Khipu stops reading its plan on this Mac and stops telling your conversations about it.</p>
      )}
      {refs.priority || refs.pausedUntil !== undefined || refs.lanes.length || refs.holds.length ? (
        <fieldset className="aa-checks">
          <legend>Also</legend>
          {refs.priority ? (
            <div>
              <input type="checkbox" id={`${id}-p`} checked={prio} onChange={(e) => setPrio(e.target.checked)} />
              <label htmlFor={`${id}-p`}>
                Remove it from your priority list (it is {ordinal(refs.priority.place)}
                {refs.priority.next ? `; ${refs.priority.next} moves up` : ""})
              </label>
            </div>
          ) : null}
          {refs.pausedUntil !== undefined ? (
            <div>
              <input type="checkbox" id={`${id}-z`} checked={pause} onChange={(e) => setPause(e.target.checked)} />
              <label htmlFor={`${id}-z`}>
                End its pause now{whenLocal(refs.pausedUntil) ? ` (paused until ${whenLocal(refs.pausedUntil)})` : ""}
              </label>
            </div>
          ) : null}
          {refs.lanes.length ? (
            <div>
              <input type="checkbox" id={`${id}-l`} checked={lanes} onChange={(e) => setLanes(e.target.checked)} />
              <label htmlFor={`${id}-l`}>
                Remove it from {plural(refs.lanes.length, "lane", "lanes")}: {refs.lanes.join(", ")}
              </label>
            </div>
          ) : null}
          {refs.holds.map((h) => (
            <div key={h.id}>
              <input type="checkbox" id={`${id}-h-${h.id}`} checked={holds.includes(h.id)}
                onChange={(e) => setHolds((prev) => (e.target.checked ? [...prev, h.id] : prev.filter((x) => x !== h.id)))} />
              <label htmlFor={`${id}-h-${h.id}`}>
                End the hold “{h.text}”{whenLocal(h.until) ? ` (ends ${whenLocal(h.until)})` : ""}
              </label>
            </div>
          ))}
        </fieldset>
      ) : null}
      <ul className="aa-dos">
        {refs.last ? (
          <>
            <li>
              <b>Not changed:</b> {account.home ? "the login folder " : ""}
              <span className="aa-mono">{folder}</span> and its sign-in.
            </li>
            <li>Khipu won't go back to reading every installed program on its own.</li>
          </>
        ) : (
          <li>
            <b>Not changed:</b> {account.home ? "the login folder " : ""}
            <span className="aa-mono">{folder}</span> and its sign-in. Conversations that run on it keep working, and Khipu still
            saves them.
          </li>
        )}
        {refs.rules.length || refs.note ? (
          <li>
            <b>Not changed:</b>{" "}
            {listOf([...refs.rules.map((r) => `standing rule ${r.n}`), ...(refs.note ? ["your note"] : [])])}, which name it.{" "}
            {mentionLinks(refs.rules, refs.note, onClose, onEdit)}
          </li>
        ) : null}
        {refs.last ? null : (
          <>
            <li>
              <b>Kept:</b> its history in Recent changes. Anything left unticked keeps it in routing as “no longer tracked” until it ends.
            </li>
            <li>
              <b>Not offered again</b> as a found account, until you choose Offer again.
            </li>
          </>
        )}
      </ul>
      {err ? (
        <MovedError text={err} stale={stale} busy={busy}
          onReload={onReload && (async () => { await onReload(); setErr(null); setStale(false); })} />
      ) : null}
      <div className="aa-actions">
        <button type="button" className="aa-left" onClick={onClose}>
          Cancel
        </button>
        <button type="button" className="primary" disabled={!here || busy} aria-busy={busy || undefined} onClick={() => void stop()}>
          Stop tracking
        </button>
      </div>
    </AccountsDialog>
  );
}

// ---------------------------------------------------------------------------
// Home's cards
// ---------------------------------------------------------------------------

/** One card listing every find a conversation used, with Track…, Not this one
 *  and Close. */
export function HomeFoundCard({
  offers,
  support,
  onTrack,
  onDecline,
  onClose,
}: {
  offers: Candidate[];
  support: Support;
  onTrack: (c: Candidate) => void;
  onDecline: (c: Candidate) => void;
  onClose: () => void;
}) {
  const here = support.state === "here";
  if (!offers.length) return null;
  const close = (
    <button type="button" className="sm" aria-label="Close this card; Khipu shows it again only when it finds a different folder" onClick={onClose}>
      Close
    </button>
  );
  const btns = (c: Candidate) => (
    <>
      <button type="button" className="sm primary" aria-label={`Track the ${serviceName(c.cli)} account at ${shownPath(c)}`} onClick={() => onTrack(c)}>
        Track…
      </button>
      <button type="button" className="sm" disabled={!here}
        aria-label={`Not this one: never offer ${shownPath(c)} again`} onClick={() => onDecline(c)}>
        Not this one
      </button>
    </>
  );
  if (offers.length === 1) {
    const c = offers[0];
    const plan = planWord(c.plan);
    return (
      <div className="callout aa-home-found" role="region" aria-label="Found an account">
        <div className="txt">
          <b>
            Found a {serviceName(c.cli)} account you use: <span className="aa-mono">{shownPath(c)}</span>
          </b>
          <span>
            {c.conversations_7d != null ? `${plural(c.conversations_7d, "of your conversations", "of your conversations")} this week ran on it, and it` : "It"} is
            signed in{plan ? ` (${plan})` : ""}. Track it to see its plan here and tell your agents when it has room.
          </span>
        </div>
        <div className="cta">
          {close}
          {btns(c)}
        </div>
      </div>
    );
  }
  return (
    <div className="callout aa-home-found" role="region" aria-label="Found accounts">
      <div className="txt">
        <b>Found {plural(offers.length, "account", "accounts")} your conversations use</b>
        <span>Track one to see its plan here and tell your agents when it has room.</span>
        <ul>
          {offers.map((c) => (
            <li key={c.id}>
              <span>
                {serviceName(c.cli)} at <span className="aa-mono">{shownPath(c)}</span>
                {c.conversations_7d != null ? `, ${plural(c.conversations_7d, "conversation", "conversations")} this week` : ""}
                {planWord(c.plan) ? `, ${planWord(c.plan)}` : ""}
              </span>
              {btns(c)}
            </li>
          ))}
        </ul>
      </div>
      <div className="cta">{close}</div>
    </div>
  );
}

export function HomeAutoCard({
  card,
  onUndo,
  onRename,
  onClose,
}: {
  card: AutoCard;
  onUndo: () => void;
  onRename: () => void;
  onClose: () => void;
}) {
  return (
    <div className="callout ok" role="status">
      <div className="txt">
        <b>Khipu started tracking {card.label}</b>
        <span>
          Found signed in{card.path ? <> at <span className="aa-mono">{card.path}</span></> : null}
          {card.conversations ? ` and used by ${plural(card.conversations, "conversation", "conversations")} this week` : ""}. Its plan is now
          in Plans and in Accounts and routing. You turned on Track new accounts automatically.
        </span>
      </div>
      <div className="cta">
        <button type="button" className="sm" aria-label={`Undo: stop tracking ${card.label}`} onClick={onUndo}>
          Undo
        </button>
        <button type="button" className="sm" aria-label={`Rename ${card.label}`} onClick={onRename}>
          Rename…
        </button>
        <button type="button" className="sm" aria-label="Close this notice" onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The state and actions every surface shares
// ---------------------------------------------------------------------------

export type FinderConfig = { usage_accounts?: ConfigAccounts; cli_settings?: CliSettings; routing_lane_map?: unknown; config?: Record<string, unknown> };

/** Discovery, the 9B probe, the account list and the dialogs, for one screen.
 *  `onChanged` runs after any write (the plan read, the routing record). */
export function useAccountsFinder(
  active: boolean,
  opts: {
    intervalMs?: number;
    view?: RouteView | null;
    laneMap?: Record<string, Record<string, string>> | null;
    onChanged?: () => void;
    onShowAccount?: (label: string) => void;
    onEdit?: (what: Mention) => void;
    /** The screen's own `khipu config` read, when it has one (Settings);
     *  `rereadConfig` reads it again after a write. */
    config?: FinderConfig | null;
    rereadConfig?: () => Promise<unknown>;
  } = {},
) {
  const disc = useDiscovery(active, opts.intervalMs);
  const { support, reload: reloadSupport } = useAccountsSupport(active);
  const shared = opts.config !== undefined;
  const [ownCfg, setCfg] = useState<FinderConfig | null>(null);
  const cfg = shared ? (opts.config ?? null) : ownCfg;
  const rereadShared = opts.rereadConfig;
  const readCfg = useCallback(async () => {
    if (shared) {
      await rereadShared?.();
      return null;
    }
    const res = await callCli<FinderConfig>("khipu_config_show");
    if (res.ok) setCfg(res.data);
    return res.ok ? res.data : null;
  }, [shared, rereadShared]);
  useEffect(() => {
    if (active && !shared) void readCfg();
  }, [active, shared, readCfg]);
  const [add, setAdd] = useState<{ start?: AddAccountStart; shown: UsageAccountEntry[] | null; n: number } | null>(null);
  const [rename, setRename] = useState<ManagedAccount | null>(null);
  const [stop, setStop] = useState<ManagedAccount | null>(null);
  const [message, setMessageText] = useState<string | null>(null);
  // Set when the shown message is a save refused for its version: Reload.
  const [stale, setStale] = useState(false);
  const setMessage = useCallback((m: string | null) => {
    setMessageText(m);
    setStale(false);
  }, []);
  const refused = useCallback((m: string) => {
    setMessageText(m);
    setStale(true);
  }, []);
  const nAdd = useRef(0);

  const usage = cfg?.usage_accounts ?? null;
  const tracked: UsageAccountEntry[] =
    support.state === "here" ? support.status.accounts : usage?.accounts ?? [];
  const none = support.state === "here" ? support.status.none : usage?.source === "none";

  const changed = useCallback(async () => {
    await Promise.all([disc.run(false), reloadSupport(), readCfg()]);
    opts.onChanged?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [disc.run, reloadSupport, readCfg, opts.onChanged]);

  const openAdd = useCallback(
    (start?: AddAccountStart) => {
      const shown = usage && usage.source === "default" ? usage.accounts : null;
      setAdd({ start, shown, n: ++nAdd.current });
    },
    [usage],
  );

  const decline = useCallback(
    async (c: Candidate) => {
      if (support.state !== "here") return;
      const res = await callCli("khipu_accounts_decline", { harness: c.cli, folder: c.default ? null : c.path });
      if (!res.ok) {
        console.error("khipu accounts decline failed:", res.error);
        setMessage("Khipu couldn't save Not this one. Try again in a minute.");
        return;
      }
      setMessage(`Khipu won't offer ${shownPath(c)} again. Undo it under Not offered again.`);
      await changed();
    },
    [support, changed],
  );

  const offerAgain = useCallback(
    async (e: DeclinedEntry) => {
      if (support.state !== "here") return;
      const res = await callCli("khipu_accounts_offer_again", { harness: e.cli, folder: e.path });
      if (!res.ok) {
        console.error("khipu accounts offer-again failed:", res.error);
        setMessage("Khipu couldn't offer it again. Try again in a minute.");
        return;
      }
      await changed();
    },
    [support, changed],
  );

  const setAutoTrack = useCallback(
    async (on: boolean) => {
      if (support.state !== "here") return;
      const res = await callCli("khipu_accounts_auto_track", { on });
      if (!res.ok) {
        console.error("khipu accounts auto-track failed:", res.error);
        setMessage("Khipu couldn't change that switch. Try again in a minute.");
      }
      await reloadSupport();
    },
    [support, reloadSupport],
  );

  const check = useCallback(
    async (c: Candidate) => {
      setMessage(null);
      if (!c.path) {
        await disc.run(true);
        return;
      }
      const res = await disc.check(c.path);
      if (!res.ok) {
        setMessage(checkErrorWords(res.error, c.cli));
        return;
      }
      const after = res.candidate;
      if (after?.state === "not_signed_in") setMessage(`${c.path} is not signed in. It shows in Add an account… if you want it.`);
      else if (after?.state === "couldnt_check") setMessage(`Couldn't check ${c.path}: ${reasonWords(c.cli, after.reason)}.`);
      else if (after?.state === "signed_in") setMessage(`${c.path} is signed in${planWord(after.plan) ? ` (${planWord(after.plan)})` : ""}. Track it if you want Khipu to read its plan.`);
    },
    [disc],
  );

  const forget = useCallback(async () => {
    const res = await forgetLoginPaths();
    const out = res.ok ? res.data : (res.refusal as { note?: string } | undefined);
    await disc.run(false);
    if (out && typeof out.note === "string") return out.note;
    return res.ok ? "Khipu forgot which logins your conversations used." : "Khipu couldn't forget them just now. Try again in a minute.";
  }, [disc]);

  const setCliPath = useCallback(
    async (key: string, value: string | null, opener: HTMLElement | null) => {
      let v = value;
      if (value === "pick") {
        v = await pickPath(opener, key === "node_folder" ? "folder" : "file");
        if (!v) return;
      }
      const res = await saveCliSetting(key, v);
      if (!res.ok) {
        console.error(`khipu config ${key} refused:`, res.error);
        setMessage(key === "node_folder" ? "That folder has no node program Khipu can run." : "That file isn't a program Khipu can run.");
        return;
      }
      setMessage(v ? "Saved. Khipu uses it from the next check." : "Cleared. Khipu looks for it by itself again.");
      await readCfg();
      await disc.run(false);
    },
    [readCfg, disc],
  );

  const move = useCallback(
    async (m: Moved) => {
      if (support.state !== "here" || !m.account.key || !m.candidate.path) return;
      const expect = readVersion(support);
      if (!expect) return refused(LIST_MOVED);
      const res = await callCli("khipu_accounts_move", { account: m.account.key, folder: m.candidate.path, expect });
      if (!res.ok) {
        console.error("khipu accounts move refused:", res.error);
        if (versionRefused(res)) return refused(LIST_MOVED);
        setMessage("Khipu couldn't move the account. Try again in a minute.");
        return;
      }
      setMessage(`${m.account.label}'s login folder is now ${m.candidate.path}.`);
      await changed();
    },
    [support, changed, refused, setMessage],
  );

  const chooseFolderFor = useCallback(
    async (a: ManagedAccount, opener: HTMLElement | null) => {
      if (support.state !== "here") return;
      const picked = await pickPath(opener, "folder");
      if (!picked) return;
      const expect = readVersion(support);
      if (!expect) return refused(LIST_MOVED);
      const res = await callCli("khipu_accounts_move", { account: a.key, folder: picked, expect });
      if (!res.ok) {
        console.error("khipu accounts move refused:", res.error);
        if (versionRefused(res)) return refused(LIST_MOVED);
        setMessage("Khipu couldn't use that folder for it. Choose the login folder itself.");
        return;
      }
      await changed();
    },
    [support, changed, refused, setMessage],
  );

  const view = opts.view ?? null;
  const laneMap = opts.laneMap ?? null;
  const refs = useMemo(
    () => (a: ManagedAccount) => accountRefs(a, view, laneMap, tracked.filter((t) => !t.invalid).length),
    [view, laneMap, tracked],
  );

  const dialogs = (
    <>
      {add ? (
        <AddAccountDialog key={add.n} open onClose={() => setAdd(null)} discovery={disc.data} onRediscover={(n) => disc.run(n)}
          onCheck={async (folder) => {
            const r = await disc.check(folder);
            return r.ok ? { ok: true, candidate: r.candidate } : { ok: false, error: r.error };
          }}
          tracked={tracked} shownDefaults={add.shown} support={support} start={add.start}
          onSaved={(label) => {
            setMessage(`Khipu now tracks ${label}.`);
            void changed();
          }}
          onShowAccount={opts.onShowAccount} onReload={reloadSupport} />
      ) : null}
      {rename ? (
        <RenameDialog open account={rename} refs={refs(rename)} taken={tracked.map((t) => t.label)} support={support}
          onClose={() => setRename(null)} onEdit={opts.onEdit} onReload={reloadSupport}
          onRenamed={(r) => {
            setMessage(`${r.was} is now called ${r.label}.`);
            void changed();
          }} />
      ) : null}
      {stop ? (
        <StopTrackingDialog open account={stop} refs={refs(stop)} support={support} onClose={() => setStop(null)} onEdit={opts.onEdit} onReload={reloadSupport}
          onStopped={(r) => {
            setMessage(r.none ? "Khipu tracks no accounts on this Mac now." : `Khipu stopped tracking ${r.label}.`);
            void changed();
          }} />
      ) : null}
    </>
  );

  return {
    disc,
    support,
    cfg,
    tracked,
    none,
    message,
    setMessage,
    stale,
    refused,
    reload: async () => {
      await reloadSupport();
      setMessage(null);
    },
    openAdd,
    openRename: setRename,
    openStop: setStop,
    decline,
    offerAgain,
    setAutoTrack,
    check,
    forget,
    setCliPath,
    move,
    chooseFolderFor,
    changed,
    dialogs,
  };
}

export type AccountsFinder = ReturnType<typeof useAccountsFinder>;

/** Settings → Accounts: the bar and the Found rows, above the accounts. */
export function FinderTop({ finder, rows = [] }: { finder: AccountsFinder; rows?: UsageAccount[] }) {
  const d = finder.disc.data;
  const found = foundRowsOf(d);
  const moved = movedPairs(rows, found);
  return (
    <>
      <AccountsGroupBar looking={finder.disc.looking} lastRun={d?.last_run ?? null} onLookNow={() => void finder.disc.run(true)}
        onAdd={() => finder.openAdd()} onForget={finder.forget} />
      {finder.disc.error ? (
        <p className="set-error" role="alert">
          {finder.disc.error}
        </p>
      ) : null}
      <FoundRows candidates={found} moved={moved} support={finder.support} checking={finder.disc.checking}
        onTrack={(c) => finder.openAdd({ candidate: c })} onCheck={(c) => void finder.check(c)} onDecline={(c) => void finder.decline(c)}
        onMove={(m) => void finder.move(m)} />
      {finder.message ? (
        finder.stale ? (
          <MovedError text={finder.message} stale onReload={() => void finder.reload()} />
        ) : (
          <p className="set-help" role="status">
            {finder.message}
          </p>
        )
      ) : null}
      {finder.none ? <NoAccountsTracked onAdd={() => finder.openAdd()} /> : null}
    </>
  );
}

/** Settings → Accounts: Not offered again, the switch and where Khipu finds
 *  each program, below the accounts. */
export function FinderBottom({ finder }: { finder: AccountsFinder }) {
  return (
    <>
      <NotOfferedAgain support={finder.support} onOfferAgain={(e) => void finder.offerAgain(e)} />
      <AutoTrackSwitch support={finder.support} onChange={(on) => void finder.setAutoTrack(on)} />
      <CliSettingsRows clis={finder.disc.data?.clis} settings={finder.cfg?.cli_settings} checking={finder.disc.running}
        onSet={(k, v, o) => void finder.setCliPath(k, v, o)} onCheckAgain={() => void finder.disc.run(false)} />
      {finder.dialogs}
    </>
  );
}

/** Home: the Found card or the automatic-tracking card, between What's new
 *  and the attention callouts. */
export function HomeAccountCards({
  active,
  onChanged,
  onOpenAccounts,
  onNone,
  addSignal = 0,
  intervalMs = 6 * 60 * 60 * 1000,
}: {
  active: boolean;
  onChanged?: () => void;
  onOpenAccounts?: () => void;
  /** Whether the owner tracks no account on this Mac ("Read no accounts"). */
  onNone?: (none: boolean) => void;
  /** Bumped by Plans' Add an account…: opens the dialog here. */
  addSignal?: number;
  intervalMs?: number;
}) {
  const finder = useAccountsFinder(active, { intervalMs, onChanged, onShowAccount: () => onOpenAccounts?.() });
  const { openAdd, none } = finder;
  useEffect(() => {
    onNone?.(none);
  }, [none, onNone]);
  useEffect(() => {
    if (addSignal > 0) openAdd();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [addSignal]);
  const [closed, setClosed] = useState<string[]>(() => readList(CLOSED_KEY, isString));
  const [autos, setAutos] = useState<AutoCard[]>(() => readList(AUTO_KEY, isAutoCard));
  const d = finder.disc.data;
  // What automatic tracking tracked on a run stays on Home until Close or Undo.
  useEffect(() => {
    const fresh = d?.auto_tracked ?? [];
    if (!fresh.length) return;
    setAutos((prev) => {
      const next = [...prev];
      for (const a of fresh) {
        if (next.some((x) => x.key === a.key)) continue;
        const path = a.key.slice(a.key.indexOf("|") + 1) || null;
        next.push({ key: a.key, label: a.label, path });
      }
      writeList(AUTO_KEY, next);
      return next;
    });
  }, [d]);
  const quiet = useMemo(() => lowerQuiet(d), [d]);
  const offers = homeOffersOf(d, quiet);
  const showFound = offers.some((c) => !closed.includes(c.id));
  const dropAuto = (key: string) =>
    setAutos((prev) => {
      const next = prev.filter((x) => x.key !== key);
      writeList(AUTO_KEY, next);
      return next;
    });
  const auto = autos[0];
  return (
    <>
      {auto ? (
        <HomeAutoCard card={auto}
          onUndo={async () => {
            if (finder.support.state !== "here") return;
            const expect = readVersion(finder.support);
            if (!expect) return finder.refused(LIST_MOVED);
            const res = await callCli("khipu_accounts_stop", { account: auto.key, expect });
            if (!res.ok) {
              console.error("khipu accounts stop (undo) refused:", res.error);
              if (versionRefused(res)) return finder.refused(LIST_MOVED);
              finder.setMessage("Khipu couldn't undo it. Use Stop tracking… in Settings → Accounts and routing.");
              return;
            }
            dropAuto(auto.key);
            await finder.changed();
          }}
          onRename={() => finder.openRename({ key: auto.key, label: auto.label, harness: auto.key.split("|")[0], home: auto.path ?? null })}
          onClose={() => dropAuto(auto.key)} />
      ) : showFound ? (
        <HomeFoundCard offers={offers} support={finder.support} onTrack={(c) => finder.openAdd({ candidate: c })}
          onDecline={(c) => void finder.decline(c)}
          onClose={() => {
            const ids = offers.map((c) => c.id);
            writeList(CLOSED_KEY, ids);
            setClosed(ids);
          }} />
      ) : null}
      {finder.message && (auto || showFound) ? (
        finder.stale ? (
          <MovedError text={finder.message} stale onReload={() => void finder.reload()} />
        ) : (
          <p className="set-help" role="status">
            {finder.message}
          </p>
        )
      ) : null}
      {finder.dialogs}
    </>
  );
}
