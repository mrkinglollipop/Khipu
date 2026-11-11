/* Accounts › Room right now (slice A4): the accounts in the order agents try
   them, each with how much is left, and the selected account's page beside
   them. The toolbar's view switch lives in App.tsx (it is the one control that
   moves between this and the other two views); Running now and Order and
   rules are later slices (A5, A6) and open their older screens until then.

   Reading changes nothing. Only a click on a control makes a change, through
   `useAccountWrites`. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import { RunningHere } from "./RunningNow";
import type { RunningHook } from "./running";
import { AccountMore } from "./AccountMore";
import { AutoTrackSwitch, CliSettingsRows, FinderTop, NotOfferedAgain, useAccountsFinder } from "../FoundAccounts";
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
        {row.warning ? <span className="ac-line" style={{ color: "var(--warn)" }}>{row.warning}</span> : null}
      </span>
    </button>
  );
}

export function Accounts({ routing, plans, thisMac, onReloadPlans, now: nowProp, running }: {
  routing: RoutingState;
  plans: UsageReport | null | undefined;
  thisMac: string | null;
  onReloadPlans?: () => Promise<unknown> | void;
  /** Tests pin the clock; the app uses the real one. */
  now?: Date;
  /** What job runners report (slice A5), for "Running here now" on the account page. */
  running?: RunningHook;
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
  // Adding, finding, renaming and no longer tracking accounts: the older
  // Accounts screen's finder, unchanged (its own commands and dialogs).
  // The job-slot map, read the way the older Accounts screen read it (`khipu config`),
  // so Stop tracking can say which slots use the account. Unread stays null.
  const [laneMap, setLaneMap] = useState<Record<string, Record<string, string>> | null>(null);
  const laneRead = useRef(0);
  const loadLaneMap = useCallback(async () => {
    const mine = ++laneRead.current;
    const res = await callCli<{ routing?: { routing_lane_map?: { value?: unknown } } }>("khipu_config_show");
    if (mine !== laneRead.current) return; // a newer read owns the answer
    const raw = res.ok ? res.data?.routing?.routing_lane_map?.value : undefined;
    setLaneMap(raw && typeof raw === "object" && !Array.isArray(raw) ? (raw as Record<string, Record<string, string>>) : null);
  }, []);
  useEffect(() => { void loadLaneMap(); }, [loadLaneMap]);
  const finder = useAccountsFinder(true, {
    view: routing.data,
    laneMap,
    onChanged: () => {
      void routing.load(true);
      void loadLaneMap();
      void onReloadPlans?.();
    },
  });
  const finderPanel = (
    <section className="ac-finder" aria-labelledby="ac-finder-h">
      <h2 id="ac-finder-h">Add or find accounts</h2>
      <FinderTop finder={finder} rows={plans?.accounts ?? []} />
      <details className="ac-finder-more">
        <summary>How Khipu finds accounts on this Mac</summary>
        <NotOfferedAgain support={finder.support} onOfferAgain={(e) => void finder.offerAgain(e)} />
        <AutoTrackSwitch support={finder.support} onChange={(on) => void finder.setAutoTrack(on)} />
        <CliSettingsRows clis={finder.disc.data?.clis} settings={finder.cfg?.cli_settings} checking={finder.disc.running}
          onSet={(k, v, o) => void finder.setCliPath(k, v, o)} onCheckAgain={() => void finder.disc.run(false)} />
      </details>
      {finder.dialogs}
    </section>
  );

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
        </div>
        {finderPanel}
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
      {finderPanel}
    </div>
  );
  const row = rows.find((r) => r.key === selectedKey) ?? rows[0];
  const keyHome = selected ? selected.key.slice(selected.key.indexOf("|") + 1) : "";
  const managed = selected
    ? { key: selected.key, label: selected.label, harness: selected.harness, home: selected.key.includes("|") && keyHome ? keyHome : null }
    : null;
  // Rename and Stop tracking wait, as they did on the older screen, for the account list to be read.
  const manageable = managed != null && finder.support.state === "here" && selected?.configured !== false;
  const detail = selected ? (
    <>
      <button type="button" className="ac-back" onClick={() => setDetailOpen(false)}>
        <ArrowLeft size={16} aria-hidden /> All accounts
      </button>
      <AccountPage a={selected} row={row} view={view} plans={plans} now={now} writes={writes} overrides={overrides}
        thisMac={thisMacName} onSignedIn={async () => { await Promise.all([reread(), onReloadPlans?.()]); }}
        runningHere={running ? <RunningHere accountKey={selected.key} accountLabel={selected.label} hook={running} now={nowProp} /> : undefined}
        more={<AccountMore a={selected} view={view} now={now} reread={reread}
          onRename={manageable ? () => finder.openRename(managed!) : undefined}
          onStop={manageable ? () => (setLaneMap(null), void loadLaneMap(), finder.openStop(managed!)) : undefined} />} />
    </>
  ) : null;
  return (
    <Body>
      <Split list={list} listLabel="Accounts" detail={detail} detailLabel={selected?.label ?? "Account"}
        detailOpen={detailOpen} wideDetail />
    </Body>
  );
}
