/* Home -> Routing (docs/plans/2026-10-14-routing-truth.md, phase 4; mock
   docs/mockups/2026-10-14-routing/home-routing*.html): one row per account
   in the scope's words, who said so and how long ago, the holds with their
   end and author, the owner's note word for word with its date, and the
   standing rules folded. Check now uses the reset-check path; the controls
   that change the record live in Settings ->
   Routing. Khipu reports this; agents decide where work goes. */

import { useId, useState } from "react";
import { Disclosure, EmptyState, ListRow, Tag } from "./ui";
import type { UsageReport } from "./PlansCard";
import { tightestWindow } from "./PlansCard";
import {
  accountSource,
  accountClassification,
  accountState,
  brokenRules,
  disagreementWords,
  holdReminder,
  holdSource,
  modelDisagreementWords,
  holdWords,
  notRefreshedWords,
  priorityHeadline,
  priorityPendingWords,
  whenLocal,
  windowWords,
} from "./routing";
import type { PlanFigure, RouteAccount, RoutePriority, RoutingState, RouteView } from "./routing";
import { callCli } from "./settingsCli";
import { holdChoiceIds, holdChoiceTargets } from "./routingProposals";

const NO_RECORD_HINT =
  "Khipu starts one when a plan check, a job runner folder or a conversation reports on an account. Until then no account is shown as open.";

/** The account rows in the Plans card's order, so the two read as a pair;
 *  accounts Plans does not list keep the record's order after them. */
export function inPlansOrder(accounts: RouteAccount[], plans?: UsageReport | null): RouteAccount[] {
  // Matched by the stable key, never the label: a name can be reused.
  const order = (plans?.accounts ?? []).map((row) => row.key ?? null);
  const rank = (a: RouteAccount) => {
    const i = order.indexOf(a.key);
    return i === -1 ? order.length : i;
  };
  return accounts
    .map((a, i) => ({ a, i }))
    .sort((x, y) => rank(x.a) - rank(y.a) || x.i - y.i)
    .map(({ a }) => a);
}

/** "58% of its week used" for an open account, from the Plans read. */
function planFigure(a: RouteAccount, plans: UsageReport | null | undefined, now: Date): PlanFigure | null {
  const row = (plans?.accounts ?? []).find((r) => r.key != null && r.key === a.key);
  const w = row ? tightestWindow(row, now) : null;
  if (!w || typeof w.used_pct !== "number") return null;
  return { pct: w.used_pct, window: windowWords(w.name) };
}

function AccountRow({
  a,
  now,
  plans,
  reportDefault, onCheck, checking, checkResult,
}: {
  a: RouteAccount;
  now: Date;
  plans?: UsageReport | null;
  reportDefault?: number;
  onCheck: (key: string) => void;
  checking: string | null;
  checkResult: { account: string; text: string; error: boolean } | null;
}) {
  const st = accountState(a, now);
  const src = accountSource(a, now, accountClassification(a) === "open" ? planFigure(a, plans, now) : null, reportDefault);
  // The account's own disagreement and each model's: the attention item
  // "Sources disagree about X (Opus)" points at one of these notes.
  const notes = [disagreementWords(a, now), ...modelDisagreementWords(a, now)].filter((d): d is string => !!d);
  return (
    <div className="row route-row" role="listitem">
      <span className="ellip">{a.label}</span>
      <span className="route-state">
        <span className={st.tone ? `hdot ${st.tone}` : "hdot"} aria-hidden />
        {st.text}
      </span>
      <span className="route-src">{src}</span>
      {a.next_check ? <span className="route-src">Khipu checks again at {whenLocal(a.next_check, now)}.</span> : null}
      {notes.map((dis) => (
        <div className="route-note" key={dis}>
          <Tag tone="warn" dot>
            Sources disagree
          </Tag>
          <span>{dis}</span>
        </div>
      ))}
      {a.checkable !== false && a.configured !== false && ["limited", "probably_open", "unknown"].includes(a.state) ? (
        <span className="route-acts">
          <button type="button" className="sm" disabled={checking !== null} onClick={() => onCheck(a.key)}>
            {checking === a.key ? "Checking…" : "Check now"}
          </button>
          {checkResult?.account === a.key ? <span className={checkResult.error ? "set-error" : "route-src"} role="status">{checkResult.text}</span> : null}
        </span>
      ) : null}
    </div>
  );
}

/** The owner's priority at the top of the card, in his order. One a
 *  conversation set at his request carries its mark and Confirm or Undo. */
function PriorityRow({ p, view, now, reload }: { p: RoutePriority; view: RouteView; now: Date; reload: () => Promise<void> }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [holdIds, setHoldIds] = useState<string[] | null>(null);
  const [holds, setHolds] = useState<Record<string, "end" | "keep">>({});
  const targets = holdIds ? holdChoiceTargets(view, holdIds, now) : [];
  const act = async (action: "confirm" | "undo", choices?: Record<string, "end" | "keep">) => {
    if (busy) return;
    setBusy(action);
    setError(null);
    try {
      const out = await callCli("khipu_route_priority", { action, expect: p.version ?? null, ...(choices ? { holds: choices } : {}) });
      if (!out.ok) {
        const refused = action === "confirm" ? holdChoiceIds(out.error) : [];
        if (refused.length) {
          setHoldIds(refused);
          setHolds({});
        } else {
          setError(out.error);
        }
      } else {
        setHoldIds(null);
      }
      await reload();
    } finally {
      setBusy(null);
    }
  };
  return (
    <ListRow className="route-priority">
      <span className="route-state">{priorityHeadline(p, now)}</span>
      {p.pending ? (
        <>
          <span className="route-note">
            <Tag tone="warn" dot>
              Not confirmed
            </Tag>
            <span>{priorityPendingWords(p)}</span>
          </span>
          <span className="route-acts">
            <button type="button" className="sm" disabled={busy !== null} onClick={() => void act("confirm")}>
              {busy === "confirm" ? "Confirming…" : "Confirm"}
            </button>
            <button type="button" className="sm" disabled={busy !== null} onClick={() => void act("undo")}>
              {busy === "undo" ? "Undoing…" : "Undo"}
            </button>
          </span>
          {error ? (
            <span className="set-error" role="alert">
              Khipu didn't make that change: {error}
            </span>
          ) : null}
          {holdIds ? (
            <div className="route-choice" role="group" aria-label="End or keep each hold">
              <p className="set-help">That change wasn't saved yet. Say whether each hold or pause ends or stays.</p>
              {targets.map((target) => (
                <fieldset key={target.id}>
                  <legend>{target.legend}</legend>
                  <label>
                    <input type="radio" name={`priority-hold-${target.id}`} checked={holds[target.id] === "end"}
                      onChange={() => setHolds((current) => ({ ...current, [target.id]: "end" }))} />
                    {target.endLabel}
                  </label>
                  <label>
                    <input type="radio" name={`priority-hold-${target.id}`} checked={holds[target.id] === "keep"}
                      onChange={() => setHolds((current) => ({ ...current, [target.id]: "keep" }))} />
                    {target.keepLabel}
                  </label>
                </fieldset>
              ))}
              <div className="toolbar">
                <button type="button" className="primary sm" disabled={busy !== null || !targets.every((target) => holds[target.id])}
                  onClick={() => void act("confirm", holds)}>
                  {busy === "confirm" ? "Confirming…" : "Save and confirm"}
                </button>
                <button type="button" className="sm link" onClick={() => setHoldIds(null)}>Cancel</button>
              </div>
            </div>
          ) : null}
        </>
      ) : null}
    </ListRow>
  );
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** The account rows with their Check now, as the Home Routing card shows
 *  them. The Work tab's Accounts card reuses this list, never a copy. */
export function AccountRows({ state, plans, now }: { state: RoutingState; plans?: UsageReport | null; now: Date }) {
  const [checking, setChecking] = useState<string | null>(null);
  const [checkResult, setCheckResult] = useState<{ account: string; text: string; error: boolean } | null>(null);
  const data = state.data;
  const check = async (account: string) => {
    if (checking !== null) return;
    setChecking(account); setCheckResult(null);
    try {
      const out = await callCli<{ reason?: string }>("khipu_route_check", { account });
      setCheckResult({ account, text: out.ok ? (out.data?.reason ?? "Checked.") : out.error, error: !out.ok });
      await state.load(true);
    } finally { setChecking((active) => active === account ? null : active); }
  };
  const accounts = inPlansOrder(data?.accounts ?? [], plans);
  if (accounts.length === 0) {
    return (
      <EmptyState
        title="No accounts to show"
        hint="The accounts listed under Plans show here once one is set up on this Mac."
      />
    );
  }
  return (
    <div role="list" aria-label="Accounts">
      {accounts.map((a) => (
        <AccountRow key={a.key} a={a} now={now} plans={plans} reportDefault={data?.report_default_limit}
          onCheck={check} checking={checking} checkResult={checkResult} />
      ))}
    </div>
  );
}

export function RoutingCard({ state, plans }: { state: RoutingState; plans?: UsageReport | null }) {
  const { data, error, loading } = state;
  const holdsId = useId();
  const now = new Date();
  const head = <div className="card-head">Routing</div>;
  if (data == null) {
    return (
      <div className="card routing-card">
        {head}
        {error ? (
          <EmptyState title="Could not read the routing record" hint={error} />
        ) : (
          <EmptyState
            title={loading ? "Reading the routing record" : "Routing record not read yet"}
            hint="Which accounts can take work right now, and who said so."
          />
        )}
      </div>
    );
  }
  if (!data.record) {
    return (
      <div className="card routing-card">
        {head}
        <EmptyState title="No routing record on this Mac" hint={NO_RECORD_HINT} />
      </div>
    );
  }
  const holds = data.holds ?? [];
  const rules = data.rules ?? [];
  const toConfirm = rules.filter((r) => r.pending || (r.seeded && !r.confirmed)).length;
  // Counted per rule: one rule broken on two lanes is one rule that may be
  // broken. Its mark shows the latest sighting.
  const broken = brokenRules(data);
  const brokenAt = new Map(
    broken.map((g) => {
      const seen = g.checks.map((c) => c.at).filter((at): at is string => !!at && !Number.isNaN(Date.parse(at)));
      const latest = seen.sort((x, y) => Date.parse(x) - Date.parse(y)).pop() ?? null;
      return [g.rule, latest] as const;
    }),
  );
  const stale = notRefreshedWords(state, now);
  return (
    <div className="card routing-card">
      {head}
      {stale ? (
        <ListRow className="route-stale">
          <span className="route-state">
            <span className="hdot warn" aria-hidden />
            {stale}
          </span>
          {error ? <span className="route-src">{error}</span> : null}
        </ListRow>
      ) : null}
      {data.priority ? <PriorityRow p={data.priority} view={data} now={now} reload={() => state.load(true)} /> : null}
      <AccountRows state={state} plans={plans} now={now} />
      {holds.length ? (
        <div className="route-sub">
          <h2 className="route-sub-head" id={holdsId}>
            Holds
          </h2>
          <ul className="route-holds" aria-labelledby={holdsId}>
            {holds.map((h) => {
              const remind = holdReminder(h, now);
              return (
                <li key={h.id} className="route-hold">
                  <span>{h.scope_label.charAt(0).toUpperCase() + h.scope_label.slice(1)}</span>
                  <span className="route-hold-words">{holdWords(h)}</span>
                  <span className="route-src">{holdSource(h, now)}</span>
                  {remind ? (
                    <span className="route-note route-remind">
                      <Tag tone="warn" dot>
                        Reminder
                      </Tag>
                      <span>{remind}</span>
                    </span>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
      {data.note ? (
        <div className="route-sub">
          <h2 className="route-sub-head">
            Your note to every conversation
            <span className="meta">
              {" "}
              · {noteDate(data.note.at, now)}shown word for word after your rules
            </span>
          </h2>
          <blockquote className="route-quote">“{data.note.text}”</blockquote>
        </div>
      ) : null}
      {rules.length ? (
        <div className="row route-rules">
          <Disclosure
            label={
              <>
                {plural(rules.length, "standing rule", "standing rules")}, shown first to every conversation
                {toConfirm ? <Tag>{`${toConfirm} not confirmed yet`}</Tag> : null}
                {broken.length ? (
                  <Tag tone="warn" dot>
                    {`${broken.length} may be broken`}
                  </Tag>
                ) : null}
              </>
            }
          >
            <ol>
              {rules.map((r) => {
                const isBroken = brokenAt.has(r.n);
                const at = brokenAt.get(r.n);
                return (
                  <li key={r.n} value={r.n} className={isBroken ? "route-rule-broken" : undefined}>
                    {r.text}
                    {isBroken ? (
                      <>
                        {" "}
                        <Tag tone="warn">{at ? `may be broken, ${whenLocal(at, now)}` : "may be broken"}</Tag>
                      </>
                    ) : null}
                  </li>
                );
              })}
            </ol>
          </Disclosure>
        </div>
      ) : null}
      <ListRow className="route-foot">
        <span className="meta">Khipu reports this; your agents decide where work goes.</span>
      </ListRow>
    </div>
  );
}

/** "Oct 13, " for the note's date; empty when it has none. */
function noteDate(iso: string | null | undefined, now: Date): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const sameYear = d.getFullYear() === now.getFullYear();
  return `${d.toLocaleDateString([], sameYear ? { month: "short", day: "numeric" } : { month: "short", day: "numeric", year: "numeric" })}, `;
}
