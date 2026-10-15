/* Setup's Accounts step (docs/plans/2026-10-15-settings-clarity.md, section 5
   and phase 7; mock docs/mockups/2026-10-15-settings-clarity/setup-accounts.html).

   Everything here is optional and nothing is written until Next: Skip for
   now writes nothing, an unchanged order writes no priority, and each of the
   three settings (which accounts are read, the priority, the telling switch)
   is written only when the owner changed it. The writes are the same ones
   Settings makes: `config --set usage_accounts`, `route priority set` and the
   `routing_push` feature switch. */

import { useEffect, useMemo, useState } from "react";
import { ChevronLeft, ChevronRight, Loader2 } from "lucide-react";
import { Callout, Tag } from "./ui";
import { callCli, useCliState } from "./settingsCli";
import type { FeaturesShape } from "./settingsCli";
import { useRouting } from "./routing";
import type { RoutePriorityEntry } from "./routing";
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

type AccountsStatus = { source: "file" | "default"; accounts: UsageAccountEntry[]; user_home?: string | null };

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

export function SetupAccountsStep({ active, onBack, onNext }: { active: boolean; onBack: () => void; onNext: () => void }) {
  const usage = usePlanUsage(active);
  const cfg = useCliState<{ usage_accounts?: AccountsStatus }>("khipu_config_show", active);
  const features = useCliState<FeaturesShape>("khipu_features_show", active);
  const route = useRouting(active);
  const status = cfg.data?.usage_accounts ?? null;
  const userHome = status?.user_home ?? null;
  const now = new Date();

  const rows = usage.data?.accounts ?? null;
  const found = useMemo(() => (rows ?? []).filter((r) => !(r.ok === false && r.error === "not_installed")), [rows]);
  // "Not found" only on evidence: a not_installed row, or, with the default
  // list (every CLI whose command is on this Mac), a CLI with no row at all.
  // A CLI left out of the owner's own list is only not listed.
  const absent = rows ? USAGE_HARNESS_OPTIONS.filter(([h]) => !found.some((r) => r.harness === h)) : [];
  const notInstalled = new Set((rows ?? []).filter((r) => r.ok === false && r.error === "not_installed").map((r) => r.harness));
  const notFound = absent
    .filter(([h]) => notInstalled.has(h) || status?.source === "default")
    .map(([, name]) => name);
  const notListed = status?.source === "file" ? absent.filter(([h]) => !notInstalled.has(h)).map(([, name]) => name) : [];

  const [unread, setUnread] = useState<Set<string>>(new Set());
  const reading = found.filter((r) => !unread.has(accountId(r.harness, r.home, userHome)));

  // The order starts as the priority already set (its accounts first, in its
  // order), then every other account in the order Khipu found them.
  const held = route.data?.priority ?? null;
  const heldEntries: RoutePriorityEntry[] = held?.entries ?? [];
  const startOrder = useMemo(() => {
    const rank = (r: UsageAccount) => {
      const i = heldEntries.findIndex((e) => e.account === routeKey(r) || e.label === r.account);
      return i < 0 ? heldEntries.length : i;
    };
    return found
      .map((r, i) => ({ r, i }))
      .sort((a, b) => rank(a.r) - rank(b.r) || a.i - b.i)
      .map(({ r }) => routeKey(r));
  }, [found, heldEntries]);
  // `moved` is always an order of every account found, so an account
  // unchecked and checked again comes back in its place; the list shown and
  // the priority written are the accounts still read, in that order.
  const [moved, setMoved] = useState<string[] | null>(null);
  const byKey = new Map(found.map((r) => [routeKey(r), r]));
  const full = moved
    ? [...moved.filter((k) => byKey.has(k)), ...startOrder.filter((k) => !moved.includes(k))]
    : startOrder;
  const readingKeys = new Set(reading.map(routeKey));
  const start = startOrder.filter((k) => readingKeys.has(k));
  const order = full.filter((k) => readingKeys.has(k));
  // The order can be changed only once the priority already set has been
  // read, so a write never drops its roles, ends or accounts.
  const routeRead = route.data != null;
  const routeFailed = !routeRead && route.error != null && !route.loading;

  const master = features.data?.features?.routing_push;
  const [tell, setTell] = useState<boolean | null>(null);
  const tellNow = tell ?? master?.enabled === true;

  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    if (!active) setErr(null);
  }, [active]);

  const move = (i: number, by: number) => {
    const a = order[i];
    const b = order[i + by];
    if (a == null || b == null) return;
    const next = [...full];
    const ia = next.indexOf(a);
    const ib = next.indexOf(b);
    [next[ia], next[ib]] = [next[ib], next[ia]];
    setMoved(next);
  };

  const checkAgain = () => {
    void usage.refresh();
    void cfg.reload();
  };

  async function next() {
    setSaving(true);
    setErr(null);
    try {
      const listed = status?.accounts.filter((e) => !e.invalid) ?? [];
      const keep = listed.filter((e) => !unread.has(accountId(e.harness, e.home, userHome)));
      if (unread.size > 0 && keep.length > 0 && keep.length < listed.length) {
        const value = keep.map((e) => (e.home ? { label: e.label, harness: e.harness, home: e.home } : { label: e.label, harness: e.harness }));
        const res = await callCli("khipu_config_set", { key: "usage_accounts", value: JSON.stringify(value) });
        if (!res.ok) {
          console.error("khipu config --set usage_accounts failed:", res.error);
          setErr("Khipu couldn't save which accounts to read. Try Next again, or Skip for now and change it later in Settings.");
          return;
        }
      }
      if (routeRead && order.length > 0 && !samePriority(order, start)) {
        const entries = order.map((k) => {
          const r = byKey.get(k);
          const was = heldEntries.find((e) => e.account === k || (r && e.label === r.account));
          return { account: k, role: was?.role ?? null, until: was?.until ?? null };
        });
        // Priority accounts with no plan row here keep their place.
        heldEntries.forEach((e, h) => {
          if (found.some((r) => e.account === routeKey(r) || e.label === r.account)) return;
          entries.splice(Math.min(h, entries.length), 0, { account: e.account, role: e.role ?? null, until: e.until ?? null });
        });
        const res = await callCli("khipu_route_priority", { action: "set", entries, until: held?.until ?? null });
        if (!res.ok) {
          console.error("khipu route priority failed:", res.error);
          setErr("Khipu couldn't save the order. Try Next again, or Skip for now and set it later in Settings.");
          return;
        }
      }
      if (tell != null && master && tell !== (master.enabled === true)) {
        const res = await callCli("khipu_feature_set", { name: "routing_push", enabled: tell });
        if (!res.ok) {
          console.error("khipu feature set routing_push failed:", res.error);
          setErr("Khipu couldn't change whether conversations are told. Try Next again, or Skip for now.");
          return;
        }
      }
      onNext();
    } finally {
      setSaving(false);
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
          action={<button type="button" className="sm" disabled={usage.refreshing} onClick={checkAgain}>Check again</button>}
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
            <button type="button" className="sm" disabled={usage.refreshing} aria-busy={usage.refreshing || undefined} onClick={checkAgain}>
              {usage.refreshing ? <Loader2 size={14} className="spin" aria-hidden /> : null}
              Check again
            </button>
          }
        >
          Khipu looked for {listOf(USAGE_HARNESS_OPTIONS.map(([, n]) => n))}. Install one and choose Check again, or carry
          on: nothing on this step is required.
        </Callout>
      ) : (
        <>
          <ul className="acct-list" aria-label="Accounts found on this Mac">
            {found.map((r) => {
              const id = accountId(r.harness, r.home, userHome);
              const line = accountLine(r, now);
              const name = r.account || cliTitle(r.harness);
              const box = `setup-read-${id.replace(/[^a-z0-9]+/gi, "-")}`;
              return (
                <li key={id} className="acct-row">
                  <span className={line.tone ? `hdot ${line.tone}` : "hdot"} aria-hidden="true" />
                  <div className="acct-main">
                    <strong id={`${box}-name`}>{name}</strong>
                    <span className="acct-line">{line.text}</span>
                    {r.ok === false && r.error === "not_signed_in" ? (
                      <span className="acct-act">
                        <button type="button" className="sm" disabled={usage.refreshing} aria-busy={usage.refreshing || undefined}
                          aria-label={`Check ${name} again`} onClick={checkAgain}>
                          {usage.refreshing ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                          Check again
                        </button>
                      </span>
                    ) : null}
                  </div>
                  <div className="acct-read">
                    {status ? (
                      <>
                        <input
                          type="checkbox"
                          id={box}
                          checked={!unread.has(id)}
                          disabled={saving}
                          aria-describedby={`${box}-name`}
                          onChange={(e) =>
                            setUnread((prev) => {
                              const out = new Set(prev);
                              if (e.target.checked) out.delete(id);
                              else out.add(id);
                              return out;
                            })
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
          {usage.error ? (
            <p className="set-error" role="alert">
              The last check didn't work: {usage.error} The lines above are from the check before it.
            </p>
          ) : null}
          {reading.length === 0 ? (
            <p className="set-help">Khipu reads at least one account. With none chosen, it keeps reading every account it finds.</p>
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
      <p className="set-help">Standing rules and holds can be added later in Settings → Accounts and routing.</p>
      {err ? <p className="set-error" role="alert">{err}</p> : null}

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
