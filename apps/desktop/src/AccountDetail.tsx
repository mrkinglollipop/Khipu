/* The two detail sheets that open from Home (and the Routing detail from
   Settings): a Plan detail for a Plans row and a Routing detail for a Routing
   row or a priority name. They are different views on purpose: the plan view
   is about use and how Khipu knows it, the routing view about who may take
   work and on whose word. See accountDetailModel.ts for the data. */

import { useCallback, useEffect, useRef, useState } from "react";
import { Sheet, Tag } from "./ui";
import { Meter, SignInControl, fmtPct } from "./PlansCard";
import type { UsageAccount, UsageReport } from "./PlansCard";
import { paceWords, whenLocal } from "./routing";
import type { RouteAccount, RoutingState, RouteView } from "./routing";
import { planDetail, routingDetail } from "./accountDetailModel";
import { callCli } from "./settingsCli";
import { plainError } from "./humanWords";
import { CapControls, HoldControls, ReadingControls, StateControls, WordsControls, useAccountControls } from "./AccountControls";

/** Opening a detail remembers what had focus, and closing puts it back there
 *  (the row, or the priority name), as a modal should. */
export function useDetailHost<T>() {
  const [target, setTarget] = useState<T | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  const open = useCallback((t: T) => {
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setTarget(t);
  }, []);
  const close = useCallback(() => {
    setTarget(null);
    const el = opener.current;
    requestAnimationFrame(() => {
      if (el && el.isConnected) el.focus();
    });
  }, []);
  return { target, open, close };
}

function Section({ title, id, children }: { title: string; id?: string; children: React.ReactNode }) {
  return (
    <section className="sheet-sec" id={id} aria-label={title}>
      <h3>{title}</h3>
      {children}
    </section>
  );
}

const TONE_DOT: Record<string, string> = { ok: "hdot ok", warn: "hdot warn", err: "hdot err" };

function StateLine({ text, tone, extra }: { text: string; tone: string | null; extra?: string }) {
  return (
    <span className="sheet-state">
      <span className={(tone && TONE_DOT[tone]) || "hdot"} aria-hidden />
      <b>{text}</b>
      {extra ? <span className="meta"> · {extra}</span> : null}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Plan detail
// ---------------------------------------------------------------------------

export function PlanDetailSheet({
  row, view, now, onClose, onRefresh, refreshing, onOpenSettings, onChanged,
}: {
  row: UsageAccount;
  view: RouteView | null | undefined;
  now: Date;
  onClose: () => void;
  onRefresh?: () => void;
  refreshing?: boolean;
  onOpenSettings?: () => void;
  /** Called after a change to how Khipu reads this plan, so the record is read again. */
  onChanged?: () => Promise<unknown> | void;
}) {
  const d = planDetail(row, view, now);
  const routeAccount = (view?.accounts ?? []).find((x) => x.key === row.key) ?? null;
  const titleId = "plan-detail-title";
  return (
    <Sheet
      titleId={titleId}
      onClose={onClose}
      title={<>{d.name}{d.plan ? <>{" "}<span className="sheet-plan">{d.plan}</span></> : null}</>}
      subtitle={<StateLine text={d.state.text} tone={d.state.tone} />}
      footer={
        <>
          {onRefresh ? (
            <button type="button" className="sm" disabled={refreshing} onClick={onRefresh}>
              {refreshing ? "Refreshing…" : "Refresh plan"}
            </button>
          ) : null}
          <span className="spacer" />
          {onOpenSettings ? (
            <button type="button" className="link sm" onClick={() => { onClose(); onOpenSettings(); }}>
              Open in Settings
            </button>
          ) : null}
        </>
      }
    >
      <div className="plan-detail" data-view="plan">
        <Section title="How much is left">
          {d.windows.length ? (
            <ul className="sheet-wins">
              {d.windows.map((w) => (
                <li key={w.name}>
                  <span className="nm">{w.name}</span>
                  <span className="rt">
                    {w.stale ? (
                      <b className="left">Figure is from before the reset</b>
                    ) : (
                      <b className={`left ${w.tone ?? ""}`}>{fmtPct(w.left)} left</b>
                    )}{" "}
                    · {w.resets}
                  </span>
                  <Meter pct={w.used} label={`${d.name} ${w.name}`} tone={w.tone} dim={w.stale} />
                </li>
              ))}
            </ul>
          ) : (
            <p className="meta">Khipu has no usage figures for this plan.</p>
          )}
          {d.credits.map((c) => (
            <p key={c} className="meta sheet-note">{c}</p>
          ))}
        </Section>
        <Section title="Activity and forecast">
          <ul className="sheet-lines">
            {d.activity.map((s) => <li key={s}>{s}</li>)}
          </ul>
        </Section>
        <Section title="How Khipu knows">
          <dl className="sheet-facts">
            {d.knows.map((f) => (
              <div key={f.k}>
                <dt>{f.k}</dt>
                <dd>{f.v}</dd>
              </div>
            ))}
          </dl>
          <div className="toolbar">
            <SignInControl row={row} onSignedIn={onRefresh} />
          </div>
          <ReadingControls account={routeAccount} row={row} view={view} onChanged={async () => { await onChanged?.(); onRefresh?.(); }} />
        </Section>
      </div>
    </Sheet>
  );
}

// ---------------------------------------------------------------------------
// Routing detail
// ---------------------------------------------------------------------------

export function RoutingDetailSheet({
  account, view, plans, now, onClose, onChanged, onOpenSettings, focusWords, onRename, onStop, onChooseFolder, onSeePlan,
}: {
  account: RouteAccount;
  view: RouteView;
  plans?: UsageAccount;
  now: Date;
  onClose: () => void;
  /** Called after Check now and after every change here, so the record is read again. */
  onChanged?: () => Promise<unknown> | void;
  onOpenSettings?: () => void;
  /** Scroll to the owner's words when the sheet opens (a priority name). */
  focusWords?: boolean;
  /** Settings hosts Rename and Stop tracking (their own dialogs) in More. */
  onRename?: () => void;
  onStop?: () => void;
  /** Offered when the login folder is missing: pick the folder it moved to. */
  onChooseFolder?: () => void;
  /** Opens this account's Plan detail. */
  onSeePlan?: () => void;
}) {
  const d = routingDetail(account, view, plans, now);
  const titleId = "routing-detail-title";
  const ctl = useAccountControls(async () => { await onChanged?.(); });
  const [more, setMore] = useState(false);
  const holds = (view.holds ?? []).filter((h) => h.scope?.account === account.key);
  const fact = view.plan_facts?.[account.key];
  const words = useRef<HTMLElement | null>(null);
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState<{ text: string; error: boolean } | null>(null);
  useEffect(() => {
    if (focusWords) words.current?.scrollIntoView?.({ block: "start" });
  }, [focusWords]);
  const canCheck = account.checkable !== false && account.configured !== false && ["limited", "probably_open", "unknown"].includes(account.state);
  const check = async () => {
    setChecking(true);
    setResult(null);
    try {
      const out = await callCli<{ reason?: string }>("khipu_route_check", { account: account.key });
      setResult({ text: out.ok ? (out.data?.reason ?? "Checked.") : plainError(out.error), error: !out.ok });
      await onChanged?.();
    } finally {
      setChecking(false);
    }
  };
  return (
    <Sheet
      titleId={titleId}
      onClose={onClose}
      title={d.name}
      subtitle={<StateLine text={d.state.text} tone={d.state.tone} />}
      footer={
        <>
          {canCheck ? (
            <button type="button" className="sm" disabled={checking} onClick={() => void check()}>
              {checking ? "Checking…" : "Check now"}
            </button>
          ) : null}
          {result ? <span className={result.error ? "set-error" : "meta"} role={result.error ? "alert" : "status"}>{result.text}</span> : null}
          <span className="spacer" />
          {onSeePlan ? (
            <button type="button" className="link sm" onClick={onSeePlan}>
              See the plan
            </button>
          ) : null}
          {onRename || onStop || onChooseFolder ? (
            <button type="button" className="link sm" aria-expanded={more} onClick={() => setMore((v) => !v)}>
              More
            </button>
          ) : null}
          {more && onChooseFolder ? (
            <button type="button" className="sm" onClick={() => { onClose(); onChooseFolder(); }}>
              Use another login folder…
            </button>
          ) : null}
          {more && onRename ? (
            <button type="button" className="sm" onClick={() => { onClose(); onRename(); }}>
              Rename…
            </button>
          ) : null}
          {more && onStop ? (
            <button type="button" className="sm" onClick={() => { onClose(); onStop(); }}>
              Stop tracking…
            </button>
          ) : null}
          {onOpenSettings ? (
            <button type="button" className="link sm" onClick={() => { onClose(); onOpenSettings(); }}>
              Open in Settings
            </button>
          ) : null}
        </>
      }
    >
      <div className="routing-detail" data-view="routing">
        <Section title="State and how it was read">
          <p className="sheet-p">{d.source || "Khipu has no report on this account yet."}</p>
          {account.next_check ? <p className="meta">Khipu checks again at {whenLocal(account.next_check, now)}.</p> : null}
          {fact && account.configured !== false ? <p className="meta">{paceWords(fact, now)}</p> : null}
          <StateControls ctl={ctl} a={account} now={now} />
        </Section>
        <Section title="Exceptions in force">
          {d.exceptions.length || holds.length ? (
            d.exceptions.map((e) => (
              <div key={e.text} className="sheet-item warn">
                <b>{e.text}</b>
                <span className="end"><strong>{e.ends}</strong>{e.by ? ` · ${e.by}` : ""}</span>
              </div>
            ))
          ) : d.exceptionsUnknown ? (
            <p className="meta">Not stored as rows yet. Any exception is inside your words below.</p>
          ) : (
            <p className="meta">None.</p>
          )}
          <HoldControls ctl={ctl} a={account} view={view} now={now} />
        </Section>
        <Section title="Your caps">
          <CapControls ctl={ctl} a={account} rows={d.caps} />
        </Section>
        <Section title="Khipu's advice">
          {d.advice.length ? (
            <dl className="sheet-facts">
              {d.advice.map((a) => (
                <div key={a.k}>
                  <dt>{a.k}</dt>
                  <dd><b>{a.verdict}</b>{a.why ? ` · ${a.why}` : ""}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="meta">No advice yet.</p>
          )}
        </Section>
        <section className="sheet-sec" id="routing-words" aria-label="Your words for this account" ref={words}>
          <h3>Your words for this account{d.rank ? ` · ${d.rank}` : ""}</h3>
          {d.role ? <WordsQuote text={d.role} /> : <p className="meta">{d.rank ? "No words written." : "Not in your priority."}</p>}
          <WordsControls ctl={ctl} a={account} view={view} now={now} />
        </section>
        <Section title="Recent changes">
          {d.history.length ? (
            <ul className="sheet-hist">
              {d.history.map((h, i) => (
                <li key={`${h.at}-${i}`}><span className="t">{h.at}</span><span>{h.text}</span></li>
              ))}
            </ul>
          ) : (
            <p className="meta">{d.historyFrom ? `No changes since ${d.historyFrom}, the oldest one Khipu keeps.` : "No changes recorded."}</p>
          )}
        </Section>
        <Section title="Limit reports">
          {d.limits.length ? (
            <ul className="sheet-hist">
              {d.limits.map((h, i) => (
                <li key={`${h.at}-${i}`}><span className="t">{h.at}</span><span>{h.text}</span></li>
              ))}
            </ul>
          ) : (
            <p className="meta">{d.historyFrom ? `No limit reports since ${d.historyFrom}.` : "No limit reports recorded."}</p>
          )}
        </Section>
        {d.gaps.length ? (
          <p className="sheet-gap">
            <Tag>Not shown yet</Tag> {d.gaps.join(" ")}
          </p>
        ) : null}
      </div>
    </Sheet>
  );
}

/** One host for the Routing detail: `open(key)` from a row, `open(key, true)`
 *  from a priority name (scrolls to the owner's words), and `node` to render
 *  once wherever the rows live. */
export function useRoutingDetail({ state, plans, onOpenSettings }: {
  state: RoutingState;
  plans?: UsageReport | null;
  onOpenSettings?: () => void;
}) {
  const host = useDetailHost<{ key: string; words: boolean }>();
  const view = state.data;
  const account = host.target && view ? (view.accounts ?? []).find((a) => a.key === host.target?.key) : undefined;
  const node =
    host.target && view && account ? (
      <RoutingDetailSheet
        account={account}
        view={view}
        plans={(plans?.accounts ?? []).find((r) => r.key != null && r.key === account.key)}
        now={new Date()}
        onClose={host.close}
        onChanged={() => state.load(true)}
        onOpenSettings={onOpenSettings}
        focusWords={host.target.words}
      />
    ) : null;
  return { open: (key: string, words = false) => host.open({ key, words }), node };
}

/** The owner's words, folded after a few lines. */
function WordsQuote({ text }: { text: string }) {
  const [all, setAll] = useState(false);
  const long = text.length > 280;
  return (
    <>
      <blockquote className={all || !long ? "sheet-quote" : "sheet-quote clamp"}>{text}</blockquote>
      {long ? (
        <button type="button" className="link sm" aria-expanded={all} onClick={() => setAll((v) => !v)}>
          {all ? "Show less" : "Show all your words"}
        </button>
      ) : null}
    </>
  );
}
