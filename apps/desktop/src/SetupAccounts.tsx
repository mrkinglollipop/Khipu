/* Setup's Accounts step (docs/plans/2026-10-15-settings-clarity.md, section 5
   and phase 7; mock docs/mockups/2026-10-15-settings-clarity/setup-accounts.html).

   Everything here is optional and nothing is written until Next. The owner's
   edits are kept as operations (read or don't read account X, by identity;
   put X just above or below the account it passed; set the telling switch), never as a copy of a list. At
   Next, and at every retry, the account list, the priority and the switch
   are read again, the operations are applied to what is saved now, and only
   what then differs is written: an operation already satisfied writes
   nothing, and a change made elsewhere while setup is open (an account
   renamed, removed or added) is kept. Skip for now writes nothing. The
   writes are the ones Settings makes: `config --set usage_accounts`,
   `route priority set` and the `routing_push` feature switch. The first two
   carry the version they read, so a change saved elsewhere between that read
   and the write is refused, never overwritten. */

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Loader2 } from "lucide-react";
import { Callout, Tag } from "./ui";
import { callCli } from "./settingsCli";
import type { FeaturesShape } from "./settingsCli";
import type { RoutePriority, RoutePriorityEntry, RouteView } from "./routing";
import {
  USAGE_HARNESS_OPTIONS,
  fmtPct,
  liveWindows,
  loginKey,
  usageErrorPhrase,
  usePlanUsage,
  windowLabel,
} from "./PlansCard";
import type { UsageAccount, UsageAccountEntry } from "./PlansCard";
import { COMING, cliName, planWord, plural, reasonWords, suggestedName, useAccountsSupport, useDiscovery } from "./accountsApi";
import type { Candidate } from "./accountsApi";
import { AddAccountDialog } from "./AddAccountDialog";
import { AutoTrackSwitch, addQuietLogins } from "./FoundAccounts";
import { signInOutcome } from "./PlansCard";

/** `version` names the list as read: a save carries it (`expect`) and is
 *  refused (`version_moved`) when the list changed after it was read. */
type AccountsStatus = {
  source: "file" | "default" | "none";
  accounts: UsageAccountEntry[];
  user_home?: string | null;
  version?: string | null;
};

/** A login discovery found that Khipu doesn't track yet, as a plan row the
 *  step can list and tick (phase 9): signed in starts ticked, the rest off. */
type FoundRow = UsageAccount & { found: Candidate };

function foundRowOf(c: Candidate): FoundRow {
  return {
    harness: c.cli,
    account: suggestedName(c),
    home: c.default || c.cli === "muse" ? null : c.path,
    ok: c.state === "signed_in",
    error: c.state === "not_signed_in" ? "not_signed_in" : c.state === "couldnt_check" ? (c.reason ?? "no_answer") : null,
    plan: c.plan,
    windows: [],
    found: c,
  };
}

function isFound(r: UsageAccount): r is FoundRow {
  return (r as FoundRow).found != null;
}

/** A found login's name and line, as the setup mock words them. */
function foundWords(c: Candidate): { name: string; line: string; tone: "ok" | "warn" | null } {
  const where = c.cli === "muse" ? "Muse" : c.default ? "Default login" : c.path ?? "Its folder";
  if (c.state === "signed_in") {
    const conv = c.conversations_7d ? ` ${plural(c.conversations_7d, "conversation", "conversations")} this week ran on it.` : "";
    return {
      name: `${cliName(c.cli)}, ${c.default ? "default login" : "second login"}`,
      line: `${where}: signed in${planWord(c.plan) ? `, ${planWord(c.plan)}` : ""}.${conv}`,
      tone: "ok",
    };
  }
  if (c.state === "not_signed_in") {
    return { name: `${cliName(c.cli)}, another folder`, line: `${where}: not signed in. Leave it off, or sign in to it after setup.`, tone: null };
  }
  return {
    name: `${cliName(c.cli)}, another folder`,
    line: `${where}: couldn't check (${reasonWords(c.cli, c.reason)}). Leave it off, or check it later in Settings.`,
    tone: "warn",
  };
}

const CLI_TITLE: Record<string, string> = Object.fromEntries(USAGE_HARNESS_OPTIONS);

function cliTitle(harness: string | undefined): string {
  return CLI_TITLE[harness ?? ""] ?? harness ?? "its CLI";
}

/** One account's identity for matching the plan read, the account list and
 *  the priority: its CLI and its login folder as `khipu config` compares it. */
export function accountId(harness: string | undefined, home: string | null | undefined, userHome?: string | null): string {
  return `${harness ?? ""}|${loginKey(home, userHome)}`;
}

/** The account as `khipu route priority` names it: `harness|login folder`. */
function routeKey(row: UsageAccount): string {
  return `${row.harness ?? ""}|${(row.home ?? "").trim()}`;
}

function listOf(names: string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** What Khipu could read of one account, in one line, and its dot's tone. */
export function accountLine(row: UsageAccount, now: Date = new Date()): { text: string; tone: "ok" | "warn" | null } {
  const code = row.ok === false ? row.error ?? null : null;
  const cli = cliTitle(row.harness);
  if (code === "not_signed_in") return { text: `Not signed in. Sign in from ${cli}, then Check again.`, tone: "warn" };
  if (code === "pending") return { text: "Reading it for the first time.", tone: null };
  if (code === "invalid_account") {
    return { text: "This entry in the account list isn't valid. Fix it later in Settings → Accounts and routing.", tone: "warn" };
  }
  if (code === "no_plan_limits") return { text: "Signed in, but it reports no plan limits.", tone: "warn" };
  if (row.ok === false) return { text: `Found, but its plan can't be read just now: it ${usageErrorPhrase(code)}.`, tone: "warn" };
  const windows = liveWindows(row, now);
  if (row.harness === "muse" && windows.length === 0) {
    return { text: "Found. It reports its plan after its first turn.", tone: "ok" };
  }
  const parts = ["Signed in"];
  const plan = (row.plan ?? "").trim();
  if (plan) parts.push(`${plan.charAt(0).toUpperCase()}${plan.slice(1)}`);
  const week = windows.find((w) => w.name === "weekly")
    ?? [...windows].sort((a, b) => (b.window_mins ?? 0) - (a.window_mins ?? 0))[0];
  if (week && typeof week.used_pct === "number") {
    parts.push(`${fmtPct(Math.max(0, 100 - week.used_pct))} of its ${windowLabel(week)} left`);
  }
  return { text: parts.join(", "), tone: "ok" };
}

function samePriority(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((k, i) => k === b[i]);
}

type ConfigRead = { usage_accounts?: AccountsStatus };
type Read<T> = { ok: true; data: T } | { ok: false; error: string };

/** A read whose answer is used only when no later read of the same thing has
 *  started since: the first read, Check again and Next share one counter, so
 *  an old answer arriving late never replaces a newer one. */
function useLatestRead<T>(command: string, active: boolean) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const seq = useRef(0);
  const read = useCallback(async (): Promise<Read<T>> => {
    const mine = ++seq.current;
    setLoading(true);
    const res = await callCli<T>(command);
    if (mine !== seq.current) return { ok: false, error: "a newer read started" };
    setLoading(false);
    if (res.ok) {
      setData(res.data);
      setError(null);
      return { ok: true, data: res.data };
    }
    setError(res.error);
    return { ok: false, error: res.error };
  }, [command]);
  useEffect(() => {
    if (active) void read();
  }, [active, read]);
  return { data, error, loading, read };
}

type StoredEntry = { label: string; harness: string; home?: string };
/** Move up or down: `key` goes just before (-1) or after (1) `past`, the
 *  account it passed. Relative, so an account checked again later comes back
 *  in its place without shifting the moves around it. */
type Move = { key: string; past: string; dir: -1 | 1 };

/** The account list once the read operations are applied to `status`, the
 *  list saved now: unchecking removes that account, checking adds it back
 *  (as its plan row names it) only when it is missing. `changed` is false
 *  when every operation is already satisfied. */
export function planAccountList(
  status: AccountsStatus,
  readOps: Map<string, boolean>,
  rows: UsageAccount[],
): { list: StoredEntry[]; ids: Set<string>; changed: boolean } {
  const uh = status.user_home ?? null;
  const list: StoredEntry[] = status.accounts
    .filter((e) => !e.invalid)
    .map((e) => (e.home ? { label: e.label, harness: e.harness, home: e.home } : { label: e.label, harness: e.harness }));
  let changed = false;
  for (const [id, on] of readOps) {
    const at = list.findIndex((e) => accountId(e.harness, e.home, uh) === id);
    if (!on && at >= 0) {
      list.splice(at, 1);
      changed = true;
    } else if (on && at < 0) {
      const r = rows.find((row) => accountId(row.harness, row.home, uh) === id);
      if (r?.harness) {
        const label = r.account || cliTitle(r.harness);
        list.push(r.home ? { label, harness: r.harness, home: r.home } : { label, harness: r.harness });
        changed = true;
      }
    }
  }
  return { list, ids: new Set(list.map((e) => accountId(e.harness, e.home, uh))), changed };
}

type ListStart = { source: "file" | "default" | "none"; ids: Set<string> };
type ListWrite = { kind: "none" } | { kind: "set"; list: StoredEntry[] } | { kind: "unset"; list: StoredEntry[] };

/** What Next writes to the account list given `status`, the list saved now.
 *  When this step turned the default list into an explicit one and the
 *  operations now give back exactly the accounts it started with, Next goes
 *  back to the default list (every CLI found), as at the start. */
export function planListWrite(
  status: AccountsStatus,
  readOps: Map<string, boolean>,
  rows: UsageAccount[],
  start: ListStart | null,
  convertedHere: boolean,
): ListWrite {
  const plan = planAccountList(status, readOps, rows);
  const sameAsStart = start != null && plan.ids.size === start.ids.size && [...plan.ids].every((id) => start.ids.has(id));
  if (convertedHere && status.source === "file" && start?.source === "default" && sameAsStart) {
    return { kind: "unset", list: plan.list };
  }
  if (plan.changed && plan.list.length) return { kind: "set", list: plan.list };
  return { kind: "none" };
}

function sameWrite(a: ListWrite | null, b: ListWrite | null): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** The priority's entries as an account identity, compared as `khipu config`
 *  compares logins. Labels are display names a different account may take
 *  later, so they never match. */
function heldIdOf(e: RoutePriorityEntry, userHome: string | null): string {
  const [h, ...rest] = e.account.split("|");
  return accountId(h, rest.join("|") || null, userHome);
}

/** The order shown before any move: the priority's accounts first, in its
 *  order, then every other account in the order Khipu found them. */
function startOrderOf(held: RoutePriorityEntry[], visible: UsageAccount[], userHome: string | null): string[] {
  const rank = (r: UsageAccount) => {
    const i = held.findIndex((e) => heldIdOf(e, userHome) === accountId(r.harness, r.home, userHome));
    return i < 0 ? held.length : i;
  };
  return visible
    .map((r, i) => ({ r, i }))
    .sort((a, b) => rank(a.r) - rank(b.r) || a.i - b.i)
    .map(({ r }) => routeKey(r));
}

function applyMoves(order: string[], moves: Move[]): string[] {
  const out = [...order];
  for (const m of moves) {
    const at = out.indexOf(m.key);
    if (at < 0 || !out.includes(m.past)) continue;
    out.splice(at, 1);
    const p = out.indexOf(m.past);
    out.splice(m.dir < 0 ? p : p + 1, 0, m.key);
  }
  return out;
}

/** The priority to write, or null when the moves leave it as it is saved. */
export function planPriority(
  priority: RoutePriority | null,
  visible: UsageAccount[],
  moves: Move[],
  userHome: string | null,
): { entries: { account: string; role: string | null; until: string | null }[]; until: string | null } | null {
  const held = priority?.entries ?? [];
  const start = startOrderOf(held, visible, userHome);
  const order = applyMoves(start, moves);
  if (samePriority(order, start)) return null;
  const byKey = new Map(visible.map((r) => [routeKey(r), r]));
  const isRow = (e: RoutePriorityEntry, r: UsageAccount) => heldIdOf(e, userHome) === accountId(r.harness, r.home, userHome);
  const entries = order.map((k) => {
    const r = byKey.get(k);
    const was = held.find((e) => r != null && isRow(e, r));
    return { account: k, role: was?.role ?? null, until: was?.until ?? null };
  });
  // Priority accounts with no plan row here keep their place.
  held.forEach((e, h) => {
    if (visible.some((r) => isRow(e, r))) return;
    entries.splice(Math.min(h, entries.length), 0, { account: e.account, role: e.role ?? null, until: e.until ?? null });
  });
  return { entries, until: priority?.until ?? null };
}

/** The found accounts the order is about: those the list will read after
 *  Next (as saved now when the operations would leave it empty, since Next
 *  then writes nothing), or every found one before the list is read. */
function visibleRows(status: AccountsStatus | null, readOps: Map<string, boolean>, found: UsageAccount[]): UsageAccount[] {
  if (!status) return found.filter((r) => readOps.get(accountId(r.harness, r.home, null)) !== false);
  const plan = planAccountList(status, readOps, found);
  const ids = plan.list.length ? plan.ids : planAccountList(status, new Map(), found).ids;
  return found.filter((r) => ids.has(accountId(r.harness, r.home, status.user_home ?? null)));
}

export function SetupAccountsStep({ active, onBack, onNext }: { active: boolean; onBack: () => void; onNext: () => void }) {
  const usage = usePlanUsage(active);
  const cfg = useLatestRead<ConfigRead>("khipu_config_show", active);
  const features = useLatestRead<FeaturesShape>("khipu_features_show", active);
  const route = useLatestRead<RouteView>("khipu_route_view", active);
  const status = cfg.data?.usage_accounts ?? null;
  const userHome = status?.user_home ?? null;
  const now = new Date();
  const uid = useId();

  const rows = usage.data?.accounts ?? null;
  // Phase 9: what discovery found besides the accounts read now. Folders
  // found only by name wait for Check in Settings (decision 20).
  const disc = useDiscovery(active);
  const { support } = useAccountsSupport(active);
  const extra = useMemo(
    () =>
      (disc.data?.candidates ?? [])
        .filter((c) => c.state === "signed_in" || c.state === "not_signed_in" || c.state === "couldnt_check")
        .map(foundRowOf),
    [disc.data],
  );
  const found = useMemo(
    () => [...(rows ?? []).filter((r) => !(r.ok === false && r.error === "not_installed")), ...(rows ? extra : [])],
    [rows, extra],
  );
  // "Not found" only on evidence: a not_installed row, or, with the default
  // list (every CLI whose command is on this Mac), a CLI with no row at all.
  // A CLI left out of the owner's own list is only not listed.
  const absent = rows ? USAGE_HARNESS_OPTIONS.filter(([h]) => !found.some((r) => r.harness === h)) : [];
  const notInstalled = new Set((rows ?? []).filter((r) => r.ok === false && r.error === "not_installed").map((r) => r.harness));
  const notFound = absent
    .filter(([h]) => notInstalled.has(h) || status?.source === "default")
    .map(([, name]) => name);
  const notListed = status?.source === "file" ? absent.filter(([h]) => !notInstalled.has(h)).map(([, name]) => name) : [];

  // The owner's edits, as operations on whatever is saved when Next runs. A
  // signed-in found login starts ticked: ticked plus Next is the owner's
  // Track (section 1); one left unticked is neither tracked nor turned down.
  const [ownOps, setReadOps] = useState<Map<string, boolean>>(new Map());
  const readOps = useMemo(() => {
    const m = new Map<string, boolean>();
    for (const r of extra) {
      if (r.found.state === "signed_in") m.set(accountId(r.harness, r.home, userHome), true);
    }
    for (const [k, v] of ownOps) m.set(k, v);
    return m;
  }, [extra, ownOps, userHome]);
  const [addOpen, setAddOpen] = useState(0);
  const [autoOn, setAutoOn] = useState<boolean | null>(null);
  const [signing, setSigning] = useState<string | null>(null);
  const [signSaid, setSignSaid] = useState<string | null>(null);
  const [moves, setMoves] = useState<Move[]>([]);
  const [tell, setTell] = useState<boolean | null>(null);

  const listedIds = status ? planAccountList(status, new Map(), found).ids : null;
  const isChecked = (r: UsageAccount) => {
    const id = accountId(r.harness, r.home, userHome);
    return readOps.get(id) ?? (isFound(r) ? false : listedIds ? listedIds.has(id) : true);
  };
  // The list as first read, and whether this step turned the default list
  // into an explicit one: going back to the default is then this step's to do.
  const startList = useRef<ListStart | null>(null);
  if (status && !startList.current) {
    startList.current = { source: status.source, ids: planAccountList(status, new Map(), found).ids };
  }
  const convertedHere = useRef(false);
  const listPlan = status && readOps.size ? planAccountList(status, readOps, found) : null;
  // Ticked found logins are saved by Track (`khipu_accounts_track`, the one
  // save path); the list write covers only the accounts Khipu already reads.
  const foundIds = useMemo(() => new Set(extra.map((r) => accountId(r.harness, r.home, userHome))), [extra, userHome]);
  const listOps = useMemo(() => new Map([...readOps].filter(([id]) => !foundIds.has(id))), [readOps, foundIds]);
  const trackRows = (st: AccountsStatus) => {
    const listed = planAccountList(st, listOps, found).ids;
    return extra.filter((r) => {
      const id = accountId(r.harness, r.home, st.user_home ?? null);
      return readOps.get(id) === true && !listed.has(id);
    });
  };
  const listWrite = status && readOps.size ? planListWrite(status, listOps, found, startList.current, convertedHere.current) : null;
  const trackWrite = status ? trackRows(status).map((r) => r.found.id) : [];

  const held = route.data?.priority ?? null;
  const visible = visibleRows(status, readOps, found);
  const order = applyMoves(startOrderOf(held?.entries ?? [], visible, userHome), moves);
  const byKey = new Map(found.map((r) => [routeKey(r), r]));
  // The order can be changed only once the priority already set has been
  // read, so a write never drops its roles, ends or accounts.
  const routeRead = route.data != null;
  const routeFailed = !routeRead && route.error != null && !route.loading;

  const master = features.data?.features?.routing_push;
  const tellNow = tell ?? master?.enabled === true;

  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Set when a save was refused because what it read changed meanwhile: the
  // error then offers Reload, which reads both again.
  const [moved, setMoved] = useState(false);
  const [reloading, setReloading] = useState(false);
  useEffect(() => {
    if (!active) {
      setErr(null);
      setMoved(false);
    }
  }, [active]);

  async function reload() {
    setReloading(true);
    try {
      const [c, r] = await Promise.all([cfg.read(), route.read()]);
      if (c.ok && r.ok) {
        setErr(null);
        setMoved(false);
      }
    } finally {
      setReloading(false);
    }
  }

  const move = (i: number, by: -1 | 1) => {
    const key = order[i];
    const past = order[i + by];
    if (key == null || past == null) return;
    setMoves((m) => [...m, { key, past, dir: by < 0 ? -1 : 1 }]);
  };

  const checkAgain = () => {
    void usage.refresh();
    void cfg.read();
    void disc.run(false);
  };

  /** Sign in now on a default login that is tracked but not signed in: the
   *  existing sign-in for an account in the list, then a fresh read. */
  async function signInDefault(r: UsageAccount) {
    const name = r.account || cliTitle(r.harness);
    setSigning(name);
    setSignSaid(null);
    try {
      const res = await callCli<{ ok?: boolean; error?: string; row?: UsageAccount }>("khipu_account_sign_in", {
        harness: r.harness,
        home: r.home ?? null,
      });
      setSignSaid(`${name}: ${signInOutcome(res)}`);
      void usage.refresh();
    } finally {
      setSigning(null);
    }
  }

  async function next() {
    setSaving(true);
    setErr(null);
    setMoved(false);
    // What the screen promised when Next was pressed: the help line's list
    // and the order shown. A fresh read that gives a different plan writes
    // nothing; the screen then shows the new plan and Next is pressed again.
    const promisedList = listWrite;
    const promisedTrack = trackWrite;
    const promisedOrder = order;
    const done: string[] = [];
    let advanced = false;
    const say = (cant: string) =>
      setErr(
        `${done.length ? `Khipu saved ${listOf(done)}, but` : "Khipu"} couldn't ${cant}. Try Next again, or Skip for now and change it later in Settings.`,
      );
    const refusedAsMoved = (res: { refusal?: Record<string, unknown> }, what: string): boolean => {
      if (res.refusal?.code !== "version_moved") return false;
      setErr(
        `${done.length ? `Khipu saved ${listOf(done)}, but ` : ""}${what} was changed somewhere else after setup read it, ` +
          "so Khipu didn't save over it. Reload to see what is saved now, then press Next again.",
      );
      setMoved(true);
      return true;
    };
    const changedElsewhere = (what: string) =>
      setErr(
        `${done.length ? `Khipu saved ${listOf(done)}. ` : ""}${what} changed while setup was open, so Khipu saved nothing more. ` +
          "What is shown now is what Next will save; press Next again to save it.",
      );
    try {
      // Each setting is read again just before it is written, never earlier.
      let listNow: AccountsStatus | null = null;
      if (readOps.size) {
        const c = await cfg.read();
        const fresh = c.ok ? c.data.usage_accounts ?? null : null;
        if (!fresh) {
          console.error("khipu config show failed:", c.ok ? "no usage_accounts" : c.error);
          say("read the account list, so which accounts to read was not saved");
          return;
        }
        const plan = planListWrite(fresh, listOps, found, startList.current, convertedHere.current);
        const toTrack = trackRows(fresh);
        if (!sameWrite(plan, promisedList) || JSON.stringify(toTrack.map((r) => r.found.id)) !== JSON.stringify(promisedTrack)) {
          changedElsewhere("The account list");
          return;
        }
        if (plan.kind !== "none") {
          const res =
            plan.kind === "unset"
              ? await callCli("khipu_config_unset", { key: "usage_accounts", expect: fresh.version ?? null })
              : await callCli("khipu_config_set", { key: "usage_accounts", value: JSON.stringify(plan.list), expect: fresh.version ?? null });
          if (!res.ok) {
            if (refusedAsMoved(res, "the account list")) return;
            console.error("khipu config usage_accounts failed:", res.error);
            say("save which accounts to read");
            return;
          }
          if (plan.kind === "set" && fresh.source === "default") convertedHere.current = true;
          if (plan.kind === "unset") convertedHere.current = false;
          done.push("which accounts to read");
          // Read back at once: a change made elsewhere right after this write
          // is kept as it is, and said.
          const after = await cfg.read();
          const back = after.ok ? after.data.usage_accounts ?? null : null;
          const wrote = plan.kind === "unset" ? null : JSON.stringify(plan.list);
          if (back && back.source === "file" && wrote != null && JSON.stringify(planAccountList(back, new Map(), found).list) !== wrote) {
            changedElsewhere("The account list");
            return;
          }
          listNow = back;
        } else {
          listNow = fresh;
        }
        if (toTrack.length) {
          const st = listNow ?? fresh;
          const args: Record<string, unknown> = {
            entries: toTrack.map((r) => ({ harness: r.harness, home: r.home ?? null, label: r.account || cliTitle(r.harness) })),
            expect: st.version ?? "",
          };
          if (st.source === "default") {
            args.shownDefaults = st.accounts.filter((e) => !e.invalid).map((e) => ({ harness: e.harness, home: null, label: e.label }));
          }
          const res = await callCli("khipu_accounts_track", args);
          if (!res.ok) {
            if (refusedAsMoved(res, "the account list")) return;
            console.error("khipu accounts track failed:", res.error);
            say(`track ${listOf(toTrack.map((r) => r.account || cliTitle(r.harness)))}`);
            return;
          }
          done.push(listOf(toTrack.map((r) => r.account || cliTitle(r.harness))));
          const after = await cfg.read();
          listNow = after.ok ? after.data.usage_accounts ?? listNow : listNow;
        }
      }
      if (moves.length) {
        if (!listNow) {
          const c = await cfg.read();
          listNow = c.ok ? c.data.usage_accounts ?? null : null;
        }
        const r = await route.read();
        if (!r.ok) {
          console.error("khipu route failed:", r.error);
          say("read your current order, so the order was not saved");
          return;
        }
        const vis = visibleRows(listNow, readOps, found);
        const uh = listNow?.user_home ?? null;
        const freshOrder = applyMoves(startOrderOf(r.data.priority?.entries ?? [], vis, uh), moves);
        if (!samePriority(freshOrder, promisedOrder)) {
          changedElsewhere("Your order");
          return;
        }
        const plan = planPriority(r.data.priority ?? null, vis, moves, uh);
        if (plan) {
          const res = await callCli("khipu_route_priority", {
            action: "set",
            entries: plan.entries,
            until: plan.until,
            expect: r.data.priority?.version ?? r.data.priority_version ?? null,
          });
          if (!res.ok) {
            if (refusedAsMoved(res, "your order")) return;
            console.error("khipu route priority failed:", res.error);
            say("save the order");
            return;
          }
          done.push("the order");
        }
        // Saved (or already so): the moves are done and never replayed.
        setMoves([]);
        void route.read();
      }
      if (autoOn != null && support.state === "here" && autoOn !== support.status.auto_track) {
        const res = await callCli("khipu_accounts_auto_track", { on: autoOn });
        if (!res.ok) {
          console.error("khipu accounts auto-track failed:", res.error);
          say("change Track new accounts automatically");
          return;
        }
        done.push("Track new accounts automatically");
      }
      // A found login left unticked stays in Settings only, never on Home
      // (decision 12).
      addQuietLogins(extra.filter((r) => !isChecked(r)).map((r) => r.found));
      if (tell != null) {
        const f = await features.read();
        const sw = f.ok ? f.data.features?.routing_push : undefined;
        if (!sw) {
          console.error("khipu features failed:", f.ok ? "no routing_push" : f.error);
          say("read whether conversations are told, so that switch was not changed");
          return;
        }
        if (sw.source !== "env" && sw.enabled !== tell) {
          const res = await callCli("khipu_feature_set", { name: "routing_push", enabled: tell });
          if (!res.ok) {
            console.error("khipu feature set routing_push failed:", res.error);
            say("change whether conversations are told");
            return;
          }
        }
      }
      advanced = true;
      onNext();
    } finally {
      setSaving(false);
      if (!advanced && done.length) void route.read();
    }
  }

  const readFailed = rows == null && usage.error != null && !usage.loading;
  const stillReading = rows == null && !readFailed;
  const tellLocked = master?.source === "env";

  return (
    <>
      <h1>Your accounts</h1>
      <p className="muted">
        Khipu can see how much of each plan is left and tell your agents which accounts have room. It never sends or
        stops work; your agents decide.
      </p>

      {readFailed ? (
        <Callout
          tone="warn"
          title="Khipu couldn't read your accounts just now"
          action={<button type="button" className="sm" disabled={usage.refreshing || saving} onClick={checkAgain}>Check again</button>}
        >
          {usage.error} Nothing on this step is required.
        </Callout>
      ) : stillReading ? (
        <ul className="acct-list" aria-label="Accounts found on this Mac" aria-busy="true">
          {USAGE_HARNESS_OPTIONS.map(([h, name]) => (
            <li key={h} className="acct-row">
              <span className="hdot" aria-hidden="true" />
              <div className="acct-main">
                <strong>{name}</strong>
                <span className="acct-line">Reading…</span>
              </div>
              <div className="acct-read"><Tag>Reading…</Tag></div>
            </li>
          ))}
        </ul>
      ) : found.length === 0 ? (
        <Callout
          title="No accounts found on this Mac"
          action={
            <button type="button" className="sm" disabled={usage.refreshing || saving} aria-busy={usage.refreshing || undefined} onClick={checkAgain}>
              {usage.refreshing ? <Loader2 size={14} className="spin" aria-hidden /> : null}
              Check again
            </button>
          }
        >
          Khipu looked for {listOf(USAGE_HARNESS_OPTIONS.map(([, n]) => n))}. Install one and choose Check again, add one kept
          somewhere unusual, or carry on: nothing on this step is required.{" "}
          <button type="button" className="sm" disabled={saving} onClick={() => setAddOpen((n) => n + 1)}>
            Add another account…
          </button>
        </Callout>
      ) : (
        <>
          <ul className="acct-list" aria-label="Accounts found on this Mac">
            {found.map((r, i) => {
              const id = accountId(r.harness, r.home, userHome);
              const fw = isFound(r) ? foundWords(r.found) : null;
              const read = accountLine(r, now);
              // A default login's line names it, as the setup mock does.
              const line = fw
                ? { text: fw.line, tone: fw.tone }
                : !r.home && r.harness !== "muse"
                  ? { text: `Default login: ${read.text.charAt(0).toLowerCase()}${read.text.slice(1)}`, tone: read.tone }
                  : read;
              const name = fw ? fw.name : r.account || cliTitle(r.harness);
              const signable = !fw && r.ok === false && r.error === "not_signed_in" && (r.harness === "claude" || r.harness === "codex");
              const byCode = !fw && r.ok === false && r.error === "not_signed_in" && (r.harness === "grok" || r.harness === "muse");
              // Ids by position: two distinct accounts never share one.
              const box = `${uid}-read-${i}`;
              return (
                <li key={id} className="acct-row">
                  <span className={line.tone ? `hdot ${line.tone}` : "hdot"} aria-hidden="true" />
                  <div className="acct-main">
                    <strong id={`${box}-name`}>{name}</strong>
                    <span className="acct-line">{line.text}</span>
                    {!fw && r.ok === false && r.error === "not_signed_in" ? (
                      <span className="acct-act">
                        {signable ? (
                          <button type="button" className="sm primary" disabled={signing != null || saving} aria-busy={signing === name || undefined}
                            aria-label={`Sign in to ${name} now`} onClick={() => void signInDefault(r)}>
                            {signing === name ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                            Sign in now
                          </button>
                        ) : null}
                        {byCode ? (
                          <button type="button" className="sm primary" disabled aria-describedby={`${box}-code`}>
                            Sign in with a code
                          </button>
                        ) : null}
                        <button type="button" className="sm" disabled={usage.refreshing || saving} aria-busy={usage.refreshing || undefined}
                          aria-label={`Check ${name} again`} onClick={checkAgain}>
                          {usage.refreshing ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                          Check again
                        </button>
                        {byCode ? <span className="aa-coming" id={`${box}-code`}>{COMING.codeSignIn}</span> : null}
                      </span>
                    ) : null}
                  </div>
                  <div className="acct-read">
                    {status ? (
                      <>
                        <input
                          type="checkbox"
                          id={box}
                          checked={isChecked(r)}
                          disabled={saving}
                          aria-describedby={`${box}-name`}
                          onChange={(e) =>
                            setReadOps((prev) => new Map(prev).set(id, e.target.checked))
                          }
                        />
                        <label htmlFor={box}>Read this account's plan</label>
                      </>
                    ) : cfg.error ? (
                      <span className="meta">Can't read the account list</span>
                    ) : (
                      <Tag>Reading…</Tag>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
          {signSaid ? (
            <p className="set-help" role="status">
              {signSaid}
            </p>
          ) : signing ? (
            <p className="set-help" role="status">
              Finish signing in to {signing} in your browser; Khipu reads the account again when you're done.
            </p>
          ) : null}
          <div className="toolbar">
            <button type="button" className="sm" disabled={saving} onClick={() => setAddOpen((n) => n + 1)}>
              Add another account…
            </button>
            <span className="t2">Sign in to a second Claude or Codex account, or choose a login kept somewhere else.</span>
          </div>
          {listPlan && listWrite ? (
            <p className="set-help" role="status">
              {listWrite.kind === "unset"
                ? "Next goes back to reading every account Khipu finds on this Mac."
                : listWrite.kind === "set" || trackWrite.length
                  ? `Next saves the account list as: ${listOf(listPlan.list.map((e) => e.label))}.`
                  : listPlan.list.length === 0
                    ? "With none chosen, Next leaves the account list as it is saved."
                    : "The account list is already saved this way."}
            </p>
          ) : null}
          {notFound.length ? (
            <p className="acct-none">
              <span className="hdot" aria-hidden="true" />
              Not found on this Mac: {listOf(notFound)}. Add {notFound.length === 1 ? "it" : "them"} later in Settings →
              Accounts and routing if you install {notFound.length === 1 ? "it" : "them"}.
            </p>
          ) : null}
          {notListed.length ? (
            <p className="acct-none">
              <span className="hdot" aria-hidden="true" />
              Not in your account list: {listOf(notListed)}. Khipu reads only the accounts listed in Settings →
              Accounts and routing.
            </p>
          ) : null}
          {order.length > 1 ? (
            <div className="acct-block">
              <h2 className="acct-h" id="setup-prio-h">
                Which should your agents try first? <span className="meta">Optional</span>
              </h2>
              <p className="set-help">
                {routeFailed
                  ? "Khipu couldn't read your current order, so it can't be changed here. Set it later in Settings → Accounts and routing."
                  : !routeRead
                    ? "Reading your current order…"
                    : held ? "This is the order you set before. Change it, or leave it as it is." : "Leave the order as it is and Khipu sets no priority."}
              </p>
              <ol className="order-list" aria-labelledby="setup-prio-h">
                {order.map((k, i) => {
                  const r = byKey.get(k);
                  const name = r?.account || cliTitle(r?.harness);
                  return (
                    <li key={k} className="order-row">
                      <span className="order-n" aria-hidden="true">{i + 1}.</span>
                      <span className="order-name">{name}</span>
                      <span className="order-btns">
                        <button type="button" className="sm" aria-label={`Move ${name} up`} disabled={i === 0 || saving || !routeRead} onClick={() => move(i, -1)}>
                          Move up
                        </button>
                        <button type="button" className="sm" aria-label={`Move ${name} down`} disabled={i === order.length - 1 || saving || !routeRead}
                          onClick={() => move(i, 1)}>
                          Move down
                        </button>
                      </span>
                    </li>
                  );
                })}
              </ol>
            </div>
          ) : null}
        </>
      )}

      {!readFailed && usage.error ? (
        <p className="set-error" role="alert">
          The last check didn't work: {usage.error} What is shown above is from the check before it.
        </p>
      ) : null}
      {status && cfg.error ? (
        <p className="set-error" role="alert">
          Khipu couldn't read the account list again. The checkboxes show it as it was last read.
        </p>
      ) : null}
      <p className="set-help">
        Khipu also works out how fast each week is being used, so your agents can spend what would otherwise expire.
      </p>
      <div className="opt-list acct-switch">
        <div className="opt-row">
          {master ? (
            <input
              id="setup-tell-all"
              className="switch"
              type="checkbox"
              role="switch"
              checked={tellNow}
              disabled={tellLocked || saving}
              aria-describedby="setup-tell-what"
              onChange={(e) => setTell(e.target.checked)}
            />
          ) : features.error ? (
            <Tag tone="warn">Can't read this setting</Tag>
          ) : (
            <span aria-busy="true"><Tag>Reading…</Tag></span>
          )}
          <div className="opt-text">
            <div className="opt-title">
              <label className="opt-name" htmlFor="setup-tell-all">Tell my conversations about this</label>
              {tellLocked ? <Tag>Locked</Tag> : null}
            </div>
            <p id="setup-tell-what" className="opt-what">
              Conversations hear which accounts have room at the start and before each prompt. Switch it off and
              nothing is told.
            </p>
          </div>
        </div>
      </div>
      <AutoTrackSwitch setup support={support} value={autoOn ?? (support.state === "here" ? support.status.auto_track : false)}
        onChange={(on) => setAutoOn(on)} />
      <p className="set-help">
        Signed-in logins are on by default; a login that is not signed in starts off. Standing rules and holds can be added
        later in Settings → Accounts and routing.
      </p>
      {addOpen ? (
        <AddAccountDialog key={addOpen} open onClose={() => setAddOpen(0)} discovery={disc.data} onRediscover={(n) => disc.run(n)}
          onCheck={async (folder) => {
            const r = await disc.check(folder);
            return r.ok ? { ok: true, candidate: r.candidate } : { ok: false, error: r.error };
          }}
          tracked={status?.accounts ?? []} shownDefaults={status?.source === "default" ? status.accounts : null} support={support}
          onSaved={() => {
            void cfg.read();
            void usage.refresh();
            void disc.run(false);
          }} />
      ) : null}
      {err ? (
        <div className="set-error" role="alert">
          <p>{err}</p>
          {moved ? (
            <button type="button" className="sm" onClick={() => void reload()} disabled={reloading || saving} aria-busy={reloading || undefined}>
              {reloading ? <Loader2 size={14} className="spin" aria-hidden /> : null}
              Reload
            </button>
          ) : null}
        </div>
      ) : null}

      <div className="toolbar welcome-nav">
        <button type="button" onClick={onBack} disabled={saving}>
          <ChevronLeft size={14} aria-hidden /> Back
        </button>
        <span className="nav-right">
          <button type="button" onClick={onNext} disabled={saving}>
            Skip for now
          </button>
          <button type="button" className="primary" onClick={() => void next()} disabled={saving} aria-busy={saving || undefined}>
            {saving ? "Saving…" : "Next"} <ChevronRight size={14} aria-hidden />
          </button>
        </span>
      </div>
    </>
  );
}
