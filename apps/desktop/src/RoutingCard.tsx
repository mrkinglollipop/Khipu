/* Home -> Routing (docs/plans/2026-10-14-routing-truth.md, phase 4; mock
   docs/mockups/2026-10-14-routing/home-routing*.html): one row per account
   in the scope's words, who said so and how long ago, the holds with their
   end and author, the owner's note word for word with its date, and the
   standing rules folded. Read only: Check now arrives with the reset check
   (phase 5), and the controls that change the record live in Settings ->
   Routing. Khipu reports this; agents decide where work goes. */

import { useId } from "react";
import { Disclosure, EmptyState, ListRow, Tag } from "./ui";
import type { UsageReport } from "./PlansCard";
import { tightestWindow } from "./PlansCard";
import {
  accountSource,
  accountState,
  brokenChecks,
  disagreementWords,
  holdReminder,
  holdSource,
  modelDisagreementWords,
  holdWords,
  notRefreshedWords,
  whenLocal,
  windowWords,
} from "./routing";
import type { PlanFigure, RouteAccount, RoutingState } from "./routing";

const NO_RECORD_HINT =
  "Khipu starts one when a plan check, a job runner folder or a conversation reports on an account. Until then no account is shown as open.";

/** The account rows in the Plans card's order, so the two read as a pair;
 *  accounts Plans does not list keep the record's order after them. */
export function inPlansOrder(accounts: RouteAccount[], plans?: UsageReport | null): RouteAccount[] {
  const order = (plans?.accounts ?? []).map((row) => row.account || row.harness || "");
  const rank = (a: RouteAccount) => {
    const i = order.indexOf(a.label);
    return i === -1 ? order.length : i;
  };
  return accounts
    .map((a, i) => ({ a, i }))
    .sort((x, y) => rank(x.a) - rank(y.a) || x.i - y.i)
    .map(({ a }) => a);
}

/** "58% of its week used" for an open account, from the Plans read. */
function planFigure(a: RouteAccount, plans: UsageReport | null | undefined, now: Date): PlanFigure | null {
  const row = (plans?.accounts ?? []).find((r) => (r.account || r.harness) === a.label);
  const w = row ? tightestWindow(row, now) : null;
  if (!w || typeof w.used_pct !== "number") return null;
  return { pct: w.used_pct, window: windowWords(w.name) };
}

function AccountRow({
  a,
  now,
  plans,
  reportDefault,
}: {
  a: RouteAccount;
  now: Date;
  plans?: UsageReport | null;
  reportDefault?: number;
}) {
  const st = accountState(a, now);
  const src = accountSource(a, now, a.state === "open" ? planFigure(a, plans, now) : null, reportDefault);
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
      {notes.map((dis) => (
        <div className="route-note" key={dis}>
          <Tag tone="warn" dot>
            Sources disagree
          </Tag>
          <span>{dis}</span>
        </div>
      ))}
    </div>
  );
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
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
  const accounts = inPlansOrder(data.accounts ?? [], plans);
  const holds = data.holds ?? [];
  const rules = data.rules ?? [];
  const toConfirm = rules.filter((r) => r.seeded && !r.confirmed).length;
  const broken = brokenChecks(data);
  const brokenAt = new Map(broken.map((c) => [c.rule, c.at ?? null] as const));
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
      {accounts.length === 0 ? (
        <EmptyState
          title="No accounts to show"
          hint="The accounts listed under Plans show here once one is set up on this Mac."
        />
      ) : (
        <div role="list" aria-label="Accounts">
          {accounts.map((a) => (
            <AccountRow key={a.key} a={a} now={now} plans={plans} reportDefault={data.report_default_limit} />
          ))}
        </div>
      )}
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
                  <span>{holdWords(h)}</span>
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
