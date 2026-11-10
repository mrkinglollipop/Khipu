/* Accounts › Room right now (slice A4): the accounts in the order agents try
   them, each with how much is left, and the selected account's page beside
   them. The toolbar's view switch lives in App.tsx (it is the one control that
   moves between this and the other two views); Running now and Order and
   rules are later slices (A5, A6) and open their older screens until then.

   Reading changes nothing. Only a click on a control makes a change, through
   `useAccountWrites`. */

import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { Body, Split, useListKeys } from "../shell";
import { callCli } from "../settingsCli";
import type { RoutingState } from "../routing";
import type { UsageReport } from "../PlansCard";
import type { OverrideRow } from "../ConversationOverrides";
import { AccountPage, ReceiptLine, RunningAheadRows } from "./AccountPage";
import { orderedAccounts, readLine, rowModel } from "./accountsModel";
import type { RowModel } from "./accountsModel";
import { useAccountWrites } from "./useAccountWrites";
import "./accounts-new.css";

function Meter({ row }: { row: RowModel }) {
  if (row.left == null) return null;
  const reserve = row.stopAtPct != null ? 100 - row.stopAtPct : null;
  return (
    <div className="ac-meter" aria-hidden="true">
      <i className={row.tone === "warn" ? "warn" : row.tone === "err" ? "err" : undefined} style={{ width: `${row.left}%` }} />
      {reserve ? <s style={{ width: `${reserve}%` }} /> : null}
      {reserve ? <b style={{ left: `${reserve}%` }} /> : null}
    </div>
  );
}

function AccountRow({ row, selected, onSelect }: { row: RowModel; selected: boolean; onSelect: () => void }) {
  return (
    <button type="button" className="sh-row ac-row" aria-current={selected ? "true" : undefined} onClick={onSelect}>
      <span className="ac-name">
        {row.label}
        {row.sub ? <span className="ac-muted">{row.sub}</span> : null}
      </span>
      <span className={`ac-pace ${row.tone}`}>
        <span className="ac-dot" aria-hidden="true" />
        {row.word}
      </span>
      <span className="ac-bar">
        <Meter row={row} />
        <span className="ac-line">
          {row.line}
          {row.ahead ? ` · ${row.ahead === 1 ? "1 conversation" : `${row.ahead} conversations`} running ahead` : ""}
        </span>
      </span>
    </button>
  );
}

export function Accounts({ routing, plans, thisMac, onReloadPlans, now: nowProp, onOpenOlder }: {
  routing: RoutingState;
  plans: UsageReport | null | undefined;
  thisMac: string | null;
  onReloadPlans?: () => Promise<unknown> | void;
  /** Tests pin the clock; the app uses the real one. */
  now?: Date;
  onOpenOlder?: () => void;
}) {
  const [clock, setClock] = useState(() => new Date());
  useEffect(() => {
    if (nowProp) return;
    const t = window.setInterval(() => setClock(new Date()), 30_000);
    return () => window.clearInterval(t);
  }, [nowProp]);
  const now = nowProp ?? clock;

  const [overrides, setOverrides] = useState<OverrideRow[]>([]);
  const [overridesFailed, setOverridesFailed] = useState(false);
  const loadOverrides = useCallback(async () => {
    const res = await callCli<{ overrides?: OverrideRow[] }>("khipu_route_override_view");
    if (res.ok && Array.isArray(res.data.overrides)) {
      setOverrides(res.data.overrides);
      setOverridesFailed(false);
    } else {
      setOverridesFailed(true);
    }
  }, []);
  useEffect(() => { void loadOverrides(); }, [loadOverrides]);

  const reread = useCallback(async () => {
    await Promise.all([routing.load(true), loadOverrides()]);
  }, [routing, loadOverrides]);
  const writes = useAccountWrites(reread);

  const view = routing.data;
  const accounts = useMemo(() => orderedAccounts(view), [view]);
  const [picked, setPicked] = useState<string | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const selectedKey = accounts.some((a) => a.key === picked) ? picked : accounts[0]?.key ?? null;
  const selected = accounts.find((a) => a.key === selectedKey) ?? null;
  const rows = useMemo(
    () => accounts.map((a) => rowModel(a, view, plans, overrides.filter((o) => o.label === a.label).length, now)),
    [accounts, view, plans, overrides, now],
  );
  const keys = useListKeys();
  const thisMacName = thisMac ?? view?.this_mac ?? null;

  if (!view) {
    return (
      <Body>
        <div className="ac-empty" role={routing.error ? "alert" : "status"}>
          {routing.error ? (
            <>
              <p>{routing.error}</p>
              <button type="button" className="ac-btn" onClick={() => void routing.load(true)}>Try again</button>
            </>
          ) : (
            <p>Reading your accounts…</p>
          )}
        </div>
      </Body>
    );
  }
  if (!accounts.length) {
    return (
      <Body>
        <div className="ac-empty">
          <p>Khipu isn't tracking any accounts yet.</p>
          {onOpenOlder ? <button type="button" className="ac-btn" onClick={onOpenOlder}>Open the older Accounts screen to add one</button> : null}
        </div>
      </Body>
    );
  }

  const roomReceipt = writes.receipt && writes.receipt.where === "room" ? writes.receipt : null;
  const hasOrder = (view.priority?.entries ?? []).length > 0;
  const list = (
    <div>
      <p className="ac-cap">{hasOrder ? "In the order your agents try them." : "No order set. Shown as Khipu reads them."}</p>
      {roomReceipt ? <ReceiptLine receipt={roomReceipt} writes={writes} variant="block" /> : null}
      {overrides.length ? (
        <section className="ac-ahead" aria-labelledby="ac-ahead-h">
          <h2 id="ac-ahead-h">Running ahead</h2>
          <RunningAheadRows rows={overrides} writes={writes} now={now} />
        </section>
      ) : null}
      {overridesFailed ? <p className="ac-error" role="alert">Khipu couldn't read which conversations are running ahead.</p> : null}
      <div className="ac-list" {...keys}>
        {rows.map((r) => (
          <AccountRow key={r.key} row={r} selected={r.key === selectedKey}
            onSelect={() => { setPicked(r.key); setDetailOpen(true); }} />
        ))}
      </div>
      <p className="ac-cap ac-foot">{routing.error ? `${routing.error} ` : ""}{readLine(view, now) ?? ""}</p>
    </div>
  );
  const row = rows.find((r) => r.key === selectedKey) ?? rows[0];
  const detail = selected ? (
    <>
      <button type="button" className="ac-back" onClick={() => setDetailOpen(false)}>
        <ArrowLeft size={16} aria-hidden /> All accounts
      </button>
      <AccountPage a={selected} row={row} view={view} plans={plans} now={now} writes={writes} overrides={overrides}
        thisMac={thisMacName} onSignedIn={async () => { await Promise.all([reread(), onReloadPlans?.()]); }} />
    </>
  ) : null;
  return (
    <Body>
      <Split list={list} listLabel="Accounts" detail={detail} detailLabel={selected?.label ?? "Account"}
        detailOpen={detailOpen} wideDetail />
    </Body>
  );
}
