/* A project's ledger page (docs/plans/2026-10-15-work-view.md, phase 4;
   mock docs/mockups/2026-10-15-work-view/ledger.html). Rows arrive in
   ledger order (kind, then parent group, then the order added) and are
   never re-sorted. One row opens to its detail, its change form, its whole
   history newest first and Remove…; the Removed list restores a row for 30
   days. Every change is written as "you, in the app" and re-read at once. */

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Callout, Dialog } from "./ui";
import { callCli } from "./settingsCli";
import {
  ageWords,
  changedWords,
  daysSince,
  kindWords,
  meaningOf,
  ownerWords,
  plural,
  stampWords,
  stateTone,
  stateWords,
  updatedWords,
  usableRowId,
  wordFor,
} from "./work";
import type { HistoryEntry, LedgerList, LedgerRow, RemovedRow, Vocab, WorkView } from "./work";

/** The outbox notice, shown on the Work tab and the ledger page. */
export function OutboxNotice({ view, now }: { view: WorkView | null; now: Date }) {
  const n = view?.counts.ledger_outbox ?? 0;
  if (n <= 0) return null;
  const title =
    n === 1
      ? "1 ledger change is waiting for the database"
      : `${n.toLocaleString()} ledger changes are waiting for the database`;
  const at = view?.ledger_as_of ? changedWords(view.ledger_as_of, now) : "";
  return (
    <Callout tone="warn" stripe title={title}>
      They are saved on this Mac and reach the ledger when the database answers.{" "}
      {view?.ledger_offline
        ? `The ledger shows what it held${at ? ` at ${at}` : " last"}, with these changes on top. Nothing is lost.`
        : "What you see includes them."}
    </Callout>
  );
}

/** The ledger read from the offline copy: said whenever it is, waiting
 *  writes or not, with the time the copy was made. */
export function LedgerOfflineNotice({ asOf, now }: { asOf?: string | null; now: Date }) {
  const at = asOf ? changedWords(asOf, now) : "";
  return (
    <Callout tone="warn" stripe title="The ledger is showing its offline copy">
      The database didn't answer, so this is what the ledger held {at ? `at ${at}` : "when it was last copied"}. Changes
      you make are saved on this Mac and sent when it answers.
    </Callout>
  );
}

const PAGE = 100;
const STALE_DAYS = 7;
const NO_OWNER = ["", "none", "unassigned", "unowned", "-", "n/a"];

const FIELD_WORDS: Record<string, string> = {
  title: "Title",
  evidence: "Evidence",
  owner: "Owner",
  link: "Link",
  parent: "Parent",
  kind: "Kind",
  state: "State",
};

function isNoOwner(owner: string): boolean {
  return NO_OWNER.includes((owner ?? "").trim().toLowerCase());
}

function finished(vocab: Vocab | null, state: string): boolean {
  const m = meaningOf(vocab, state);
  return m === "done" || m === "dropped";
}

function sameForm(a: Form, b: Form): boolean {
  return (Object.keys(a) as Array<keyof Form>).every((k) => a[k] === b[k]);
}

function fieldValue(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

function HistoryItem({ h, now }: { h: HistoryEntry; now: Date }) {
  const when = stampWords(h.ts ?? h.at ?? null, now);
  const by = h.by || "imported, author not recorded";
  if (h.event) {
    return (
      <li>
        <span className="h-what">{h.event}</span>
        <span className="h-meta">
          {when} · {by}
        </span>
      </li>
    );
  }
  const lines: ReactNode[] = [];
  if (!h.from) {
    if (h.to) lines.push(<span key="added" className="h-what">Added as {stateWords(h.to)}</span>);
  } else if (h.to && h.from !== h.to) {
    lines.push(
      <span key="state" className="h-what">
        {stateWords(h.from)} → {stateWords(h.to)}
      </span>,
    );
  }
  for (const [field, change] of Object.entries(h.changes ?? {})) {
    if (field === "state") continue;
    const c = (change && typeof change === "object" ? change : {}) as { old?: unknown; new?: unknown };
    const name = FIELD_WORDS[field] ?? field;
    lines.push(
      <span key={`f-${field}`} className="h-what">
        {name} changed
      </span>,
      <span key={`v-${field}`} className="t2">
        {fieldValue(c.old) ? <span className="h-old">{fieldValue(c.old)}</span> : null}
        {fieldValue(c.old) ? " → " : ""}
        {fieldValue(c.new) || "(empty)"}
      </span>,
    );
  }
  if (!lines.length) lines.push(<span key="note" className="h-what">Note</span>);
  return (
    <li>
      {lines}
      {h.note ? <span className="t2">“{h.note}”</span> : null}
      <span className="h-meta">
        {when} · {by}
      </span>
    </li>
  );
}

type Form = { state: string; title: string; evidence: string; owner: string; link: string; note: string };

function initialForm(row: LedgerRow, vocab: Vocab | null): Form {
  const needs = meaningOf(vocab, row.state) === "needs";
  return {
    state: needs ? (wordFor(vocab, "done") ?? row.state) : row.state,
    title: row.title,
    evidence: row.evidence ?? "",
    owner: row.owner ?? "",
    link: row.link ?? "",
    note: "",
  };
}

export function LedgerPage({
  project,
  name,
  initialRow,
  view,
  onBack,
  onChanged,
  onOwedClose,
}: {
  project: string;
  name: string;
  initialRow?: string;
  view: WorkView | null;
  onBack: () => void;
  onChanged: () => Promise<void>;
  onOwedClose: (id: number) => Promise<void>;
}) {
  const ids = useId();
  const now = new Date();
  const [list, setList] = useState<LedgerList | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [removed, setRemoved] = useState<RemovedRow[]>([]);
  const [q, setQ] = useState("");
  const [stateF, setStateF] = useState("");
  const [kindF, setKindF] = useState("");
  const [ownerF, setOwnerF] = useState("");
  const [staleOnly, setStaleOnly] = useState(false);
  const [hideFinished, setHideFinished] = useState(true);
  const [limit, setLimit] = useState(PAGE);
  const [selected, setSelected] = useState<string | null>(usableRowId(initialRow) ? initialRow : null);
  const [detail, setDetail] = useState<LedgerRow | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  // The row open now: a detail answer for any other row arrived late and is
  // dropped, so it can never become the target of a change or a removal.
  const openRow = useRef(selected);
  openRow.current = selected;
  const [form, setForm] = useState<Form | null>(null);
  const [saving, setSaving] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [removeOpen, setRemoveOpen] = useState(false);
  const [removedOpen, setRemovedOpen] = useState(false);

  const loadList = useCallback(async () => {
    const res = await callCli<LedgerList>("khipu_ledger_list", { project });
    if (res.ok && res.data && Array.isArray(res.data.rows)) {
      setList(res.data);
      setListError(null);
    } else {
      setListError(res.ok ? "The ledger gave an answer Khipu could not read." : res.error);
    }
  }, [project]);

  const loadRemoved = useCallback(async () => {
    const res = await callCli<{ removed?: RemovedRow[] }>("khipu_ledger_removed", { project });
    if (res.ok && Array.isArray(res.data?.removed)) setRemoved(res.data.removed);
  }, [project]);

  const loadDetail = useCallback(
    async (id: string) => {
      const res = await callCli<LedgerRow>("khipu_ledger_get", { project, id });
      if (openRow.current !== id) return null;
      if (res.ok && res.data && typeof res.data.id === "string") {
        setDetail(res.data);
        setDetailError(null);
        return res.data;
      }
      setDetail(null);
      setDetailError(res.ok ? "Khipu could not read this row." : res.error);
      return null;
    },
    [project],
  );

  useEffect(() => {
    void loadList();
    void loadRemoved();
  }, [loadList, loadRemoved]);

  const vocab = list?.vocab ?? null;
  // A fresh list hands an equal vocab as a new object: the form resets on
  // its words, never on its identity.
  const vocabKey = JSON.stringify(vocab);

  // What the poll may touch: an edit under way or a save keeps the open
  // row's detail (and so the form) as it is.
  const editing = useRef(false);
  editing.current = saving || (!!form && !!detail && !sameForm(form, initialForm(detail, vocab)));

  // The list (and the open row, when it is not being edited) is read again
  // refresh_s after the previous read ends, so an open page follows changes
  // made elsewhere and a slow read never has another start behind it.
  const every = Math.max(1, view?.refresh_s ?? 5) * 1000;
  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      const id = openRow.current;
      await Promise.all([loadList(), usableRowId(id) && !editing.current ? loadDetail(id) : null]);
      if (!stopped) timer = window.setTimeout(() => void poll(), every);
    };
    timer = window.setTimeout(() => void poll(), every);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [every, loadList, loadDetail]);
  const rows = useMemo(() => list?.rows ?? [], [list]);

  useEffect(() => {
    setRefusal(null);
    if (!usableRowId(selected)) {
      setDetail(null);
      setForm(null);
      return;
    }
    void loadDetail(selected);
  }, [selected, loadDetail]);

  useEffect(() => {
    if (detail) setForm(initialForm(detail, vocab));
    // The form resets when another row opens or the row itself changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail?.id, detail?.updated, detail?.state, vocabKey]);

  const byId = useMemo(() => new Map(rows.map((r) => [r.id, r])), [rows]);
  const owners = useMemo(
    () => [...new Set(rows.map((r) => r.owner).filter((o) => !isNoOwner(o)))].sort((a, b) => a.localeCompare(b)),
    [rows],
  );
  const counts = useMemo(() => {
    let open = 0;
    let needs = 0;
    let newest: string | null = null;
    for (const r of rows) {
      const m = meaningOf(vocab, r.state);
      if (m === "open") open += 1;
      if (m === "needs") needs += 1;
      if (r.updated && (!newest || Date.parse(r.updated) > Date.parse(newest))) newest = r.updated;
    }
    return { open, needs, newest };
  }, [rows, vocab]);

  const { filtered, hiddenFinished } = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const rest = rows.filter((r) => {
      if (needle && !r.title.toLowerCase().includes(needle)) return false;
      if (stateF === "__open" && meaningOf(vocab, r.state) !== "open") return false;
      if (stateF && stateF !== "__open" && r.state !== stateF) return false;
      if (kindF && r.kind !== kindF) return false;
      if (ownerF === "__none" && !isNoOwner(r.owner)) return false;
      if (ownerF && ownerF !== "__none" && r.owner !== ownerF) return false;
      if (staleOnly) {
        const d = daysSince(r.updated, now);
        if (meaningOf(vocab, r.state) !== "open" || d == null || d < STALE_DAYS) return false;
      }
      return true;
    });
    if (!hideFinished) return { filtered: rest, hiddenFinished: 0 };
    const kept = rest.filter((r) => !finished(vocab, r.state));
    return { filtered: kept, hiddenFinished: rest.length - kept.length };
    // `now` moves every render; the stale chip only needs the day.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, vocab, q, stateF, kindF, ownerF, staleOnly, hideFinished]);

  useEffect(() => {
    setLimit(PAGE);
  }, [q, stateF, kindF, ownerF, staleOnly, hideFinished]);

  const visible = filtered.slice(0, limit);
  const children = detail ? rows.filter((r) => r.parent === detail.id) : [];

  const afterWrite = async (id: string | null) => {
    await Promise.all([loadList(), id ? loadDetail(id) : Promise.resolve(null), onChanged()]);
  };

  const save = async () => {
    if (!detail || !form || saving) return;
    const change: Record<string, string> = {};
    if (form.state !== detail.state) change.state = form.state;
    if (form.title !== detail.title) change.title = form.title;
    if (form.evidence !== (detail.evidence ?? "")) change.evidence = form.evidence;
    if (form.owner !== (detail.owner ?? "")) change.owner = form.owner;
    if (form.link !== (detail.link ?? "")) change.link = form.link;
    if (form.note.trim()) change.note = form.note;
    if (!Object.keys(change).length) {
      setRefusal("change a field or write a note first.");
      return;
    }
    change.if_state = detail.state;
    setSaving(true);
    setRefusal(null);
    try {
      const out = await callCli("khipu_ledger_set", { project, id: detail.id, change });
      if (!out.ok) {
        setRefusal(out.error);
      } else {
        const commitment = detail.refs?.commitment;
        if (change.state && commitment != null && commitment !== "" && finished(vocab, change.state)) {
          await onOwedClose(Number(commitment));
        }
      }
      await afterWrite(detail.id);
    } finally {
      setSaving(false);
    }
  };

  const formMeaning = form ? meaningOf(vocab, form.state) : null;
  const isNeeds = detail ? meaningOf(vocab, detail.state) === "needs" : false;
  const commitment = detail?.refs?.commitment;
  const parentRow = detail?.parent ? byId.get(detail.parent) : undefined;
  const openChildren = children.filter((c) => !finished(vocab, c.state));
  const childKinds = new Set(openChildren.map((c) => c.kind));
  const waitingWords = openChildren.length
    ? `${plural(
        openChildren.length,
        childKinds.size === 1 ? kindWords([...childKinds][0]).toLowerCase() : "row",
        childKinds.size === 1 ? kindWords([...childKinds][0], true).toLowerCase() : "rows",
      )}: ${openChildren.map((c) => c.id).join(", ")}`
    : null;

  let lastKind: string | null = null;
  let lastParent: string | null = null;
  const listItems: ReactNode[] = [];
  for (const r of visible) {
    if (r.kind !== lastKind) {
      listItems.push(
        <div key={`g-${r.kind}-${r.id}`} role="listitem" className="lg-group">
          {kindWords(r.kind, true)}
        </div>,
      );
      lastKind = r.kind;
      lastParent = null;
    }
    if (r.parent && r.parent !== lastParent) {
      const p = byId.get(r.parent);
      listItems.push(
        <div key={`p-${r.parent}-${r.id}`} role="listitem" className="lg-parent">
          {p ? `Under: ${p.title}` : `Parent not found: ${r.parent}`}
        </div>,
      );
    }
    lastParent = r.parent || null;
    const m = meaningOf(vocab, r.state);
    const days = daysSince(r.updated, now);
    const old = m === "open" && days != null && days >= STALE_DAYS;
    const when = old ? plural(days ?? 0, "day") : m === "needs" ? ageWords(r.updated, now) : changedWords(r.updated, now);
    const sel = r.id === selected;
    // A withheld id can't be looked up: its row shows, with no action.
    const usable = usableRowId(r.id);
    listItems.push(
      <div key={r.id} role="listitem">
        <button
          type="button"
          className={["row", "lg-row", r.parent ? "child" : null, sel ? "sel" : null].filter(Boolean).join(" ")}
          aria-current={sel ? "true" : undefined}
          disabled={!usable}
          title={usable ? undefined : "This row's id is withheld, so it can't be opened here"}
          onClick={usable ? () => setSelected(r.id) : undefined}
        >
          <span className="lg-id">{r.id}</span>
          <span className="lg-title">{r.title}</span>
          <span className="lg-state">
            <span className={stateTone(r.state, m)} aria-hidden />
            {stateWords(r.state)}
          </span>
          {r.queued ? (
            <span className="lg-when" title="Saved on this Mac; the database hasn't taken it yet">Not sent yet</span>
          ) : (
            <span className={old ? "lg-when old" : "lg-when"}>{when}</span>
          )}
        </button>
      </div>,
    );
  }

  const pagingWords = [
    filtered.length > visible.length
      ? `Showing ${visible.length.toLocaleString()} of ${plural(filtered.length, "row")}.`
      : null,
    hiddenFinished ? `${plural(hiddenFinished, "finished row is hidden.", "finished rows are hidden.")}` : null,
  ].filter(Boolean);

  return (
    <>
      <header className="panel-head" data-tauri-drag-region>
        <div className="panel-head-text" data-tauri-drag-region>
          <h1 data-tauri-drag-region>{name} ledger</h1>
          <p className="lede" data-tauri-drag-region>
            Every goal, slice, review and loose end in {name}, in the order they were added
          </p>
        </div>
        <div className="panel-head-actions">
          <button type="button" className="sm" onClick={onBack}>
            Back to Work
          </button>
        </div>
      </header>
      <div className="panel-body wide lg-page">
        {(view && view.counts.ledger_outbox > 0) || list?.offline || (list?.pending ?? 0) > 0 ? (
          <div className="wk-notices">
            <OutboxNotice view={view} now={now} />
            {list?.offline && !(view && view.counts.ledger_outbox > 0 && view.ledger_offline) ? (
              <LedgerOfflineNotice asOf={list.as_of} now={now} />
            ) : null}
            {list && !list.offline && (list.pending ?? 0) > 0 ? (
              <Callout tone="warn" stripe title={plural(list.pending ?? 0, "change is waiting to be sent", "changes are waiting to be sent")}>
                Saved on this Mac and marked Not sent yet below until the database takes{" "}
                {(list.pending ?? 0) === 1 ? "it" : "them"}.
              </Callout>
            ) : null}
          </div>
        ) : null}
        {listError ? (
          <p className="set-error" role="alert">
            Khipu couldn't read this ledger: {listError}
          </p>
        ) : null}
        <div className="lg-split">
          <div className="card">
            <div className="card-head">
              <h2 className="wk-head">{plural(rows.length, "row")}</h2>
              <span className="meta">
                {" "}
                · {counts.open.toLocaleString()} open · {plural(counts.needs, "needs you", "need you")}
                {counts.newest ? ` · last change ${updatedWords(counts.newest, now)}` : ""}
              </span>
              <span className="spacer" />
              {removed.length ? (
                <button type="button" className="sm link" onClick={() => setRemovedOpen(true)}>
                  Removed ({removed.length.toLocaleString()})
                </button>
              ) : null}
            </div>
            <div className="lg-filters">
              <label className="sr-only" htmlFor={`${ids}-q`}>
                Search titles
              </label>
              <input id={`${ids}-q`} type="search" placeholder="Search titles" value={q} onChange={(e) => setQ(e.target.value)} />
              <label className="sr-only" htmlFor={`${ids}-state`}>
                State
              </label>
              <select id={`${ids}-state`} value={stateF} onChange={(e) => setStateF(e.target.value)}>
                <option value="">Any state</option>
                <option value="__open">Open (loose ends)</option>
                {(vocab?.state ?? []).map(([w]) => (
                  <option key={w} value={w}>
                    {stateWords(w)}
                  </option>
                ))}
              </select>
              <label className="sr-only" htmlFor={`${ids}-kind`}>
                Kind
              </label>
              <select id={`${ids}-kind`} value={kindF} onChange={(e) => setKindF(e.target.value)}>
                <option value="">Any kind</option>
                {(vocab?.kind ?? []).map((k) => (
                  <option key={k} value={k}>
                    {kindWords(k)}
                  </option>
                ))}
              </select>
              <label className="sr-only" htmlFor={`${ids}-owner`}>
                Owner
              </label>
              <select id={`${ids}-owner`} value={ownerF} onChange={(e) => setOwnerF(e.target.value)}>
                <option value="">Anyone</option>
                {owners.map((o) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
                <option value="__none">No one</option>
              </select>
              <button type="button" className={staleOnly ? "chip on" : "chip"} aria-pressed={staleOnly}
                onClick={() => setStaleOnly((v) => !v)}>
                No change for a week
              </button>
              <button type="button" className={hideFinished ? "chip on" : "chip"} aria-pressed={hideFinished}
                onClick={() => setHideFinished((v) => !v)}>
                Hide finished
              </button>
            </div>
            {list && visible.length === 0 ? (
              <div className="row">
                <span className="grow t2">No rows match these filters.</span>
              </div>
            ) : null}
            {!list && !listError ? (
              <div className="row">
                <span className="grow t2">Reading the ledger…</span>
              </div>
            ) : null}
            <div role="list" aria-label={`${name} ledger rows`}>
              {listItems}
            </div>
            {pagingWords.length ? (
              <div className="row wk-more">
                <span className="meta">{pagingWords.join(" ")}</span>
                {filtered.length > visible.length ? (
                  <button type="button" className="sm" onClick={() => setLimit((n) => n + PAGE)}>
                    Show the next {Math.min(PAGE, filtered.length - visible.length).toLocaleString()}
                  </button>
                ) : null}
              </div>
            ) : null}
          </div>

          <div className="card lg-detail" aria-live="polite">
            {!selected ? (
              <div className="row">
                <span className="grow t2">Choose a row to see its detail, its history and what you can change.</span>
              </div>
            ) : detailError ? (
              <div className="row">
                <span className="set-error" role="alert">
                  {detailError}
                </span>
              </div>
            ) : !detail || !form ? (
              <div className="row">
                <span className="grow t2">Reading the row…</span>
              </div>
            ) : (
              <>
                <div className="card-head">
                  <h2 className="wk-head">{detail.id}</h2>
                  <span className="spacer" />
                  <span className="lg-state">
                    <span className={stateTone(detail.state, meaningOf(vocab, detail.state))} aria-hidden />
                    {stateWords(detail.state)}
                  </span>
                </div>
                <p className="lg-title-full">{detail.title}</p>
                <dl className="lg-fields">
                  <dt>Kind</dt>
                  <dd>{kindWords(detail.kind)}</dd>
                  {detail.parent ? (
                    <>
                      <dt>Under</dt>
                      <dd>{parentRow ? `${parentRow.title} (${detail.parent})` : `Parent not found (${detail.parent})`}</dd>
                    </>
                  ) : null}
                  {isNeeds && waitingWords ? (
                    <>
                      <dt>Waiting on it</dt>
                      <dd>{waitingWords}</dd>
                    </>
                  ) : null}
                  {!isNeeds && children.length ? (
                    <>
                      <dt>Under it</dt>
                      <dd>
                        {plural(children.length, "row")}: {children.map((c) => c.id).join(", ")}
                      </dd>
                    </>
                  ) : null}
                  <dt>Owner</dt>
                  <dd>{ownerWords(detail.owner)}</dd>
                  {detail.evidence ? (
                    <>
                      <dt>Evidence</dt>
                      <dd>{detail.evidence}</dd>
                    </>
                  ) : null}
                  {detail.link ? (
                    <>
                      <dt>Link</dt>
                      <dd>
                        <a href={detail.link} target="_blank" rel="noreferrer">
                          {detail.link.replace(/^https?:\/\//, "")}
                        </a>
                      </dd>
                    </>
                  ) : null}
                  {commitment != null && commitment !== "" ? (
                    <>
                      <dt>Also in Owed</dt>
                      <dd>Owed item {String(commitment)}</dd>
                    </>
                  ) : null}
                </dl>
                <form
                  className="lg-change"
                  aria-label={isNeeds ? "Answer this" : "Change this row"}
                  onSubmit={(e) => {
                    e.preventDefault();
                    void save();
                  }}
                >
                  <div className="toolbar">
                    <label htmlFor={`${ids}-to`}>New state</label>
                    <select id={`${ids}-to`} value={form.state} onChange={(e) => setForm({ ...form, state: e.target.value })}>
                      {(vocab?.state ?? [[detail.state, ""]]).map(([w]) => (
                        <option key={w} value={w}>
                          {stateWords(w)}
                        </option>
                      ))}
                    </select>
                  </div>
                  {(
                    [
                      ["title", "Title"],
                      ["evidence", "Evidence"],
                      ["owner", "Owner"],
                      ["link", "Link"],
                    ] as const
                  ).map(([field, label]) => (
                    <div key={field} className="toolbar">
                      <label htmlFor={`${ids}-${field}`}>{label}</label>
                      <input
                        id={`${ids}-${field}`}
                        value={form[field]}
                        onChange={(e) => setForm({ ...form, [field]: e.target.value })}
                      />
                    </div>
                  ))}
                  <label className="sr-only" htmlFor={`${ids}-note`}>
                    Your note
                  </label>
                  <textarea
                    id={`${ids}-note`}
                    placeholder="Your note"
                    value={form.note}
                    onChange={(e) => setForm({ ...form, note: e.target.value })}
                  />
                  <div className="toolbar">
                    <button type="submit" className="sm primary" disabled={saving}>
                      {saving ? "Saving…" : "Save"}
                    </button>
                    <button type="button" className="sm" disabled={saving}
                      onClick={() => {
                        setForm(initialForm(detail, vocab));
                        setRefusal(null);
                      }}>
                      Cancel
                    </button>
                    <span className="meta">
                      Saved as “you, in the app”.
                      {commitment != null && commitment !== "" && (formMeaning === "done" || formMeaning === "dropped")
                        ? " The Owed item closes too."
                        : ""}
                    </span>
                  </div>
                  {refusal ? (
                    <p className="set-error" role="alert">
                      Khipu didn't make that change: {refusal}
                    </p>
                  ) : null}
                </form>
                <div className="lg-remove-row">
                  <button type="button" className="sm danger" onClick={() => setRemoveOpen(true)}>
                    Remove…
                  </button>
                </div>
                <h3 className="wk-sec">HISTORY, NEWEST FIRST</h3>
                <ol className="lg-hist" reversed aria-label={`History of ${detail.id}`}>
                  {[...(detail.history ?? [])].reverse().map((h, i) => (
                    <HistoryItem key={`${h.ts ?? h.at ?? ""}-${i}`} h={h} now={now} />
                  ))}
                </ol>
                <div className="wk-board-foot">
                  <span className="meta">
                    Rows are never moved. Dropped rows stay with their history; a removed row can be restored for 30 days.
                  </span>
                </div>
                <RemoveDialog
                  open={removeOpen}
                  row={detail}
                  under={children}
                  parentTitle={parentRow?.title ?? null}
                  project={project}
                  onCancel={() => setRemoveOpen(false)}
                  onRemoved={async () => {
                    setRemoveOpen(false);
                    setSelected(null);
                    await Promise.all([loadList(), loadRemoved(), onChanged()]);
                  }}
                />
              </>
            )}
          </div>
        </div>
        <RemovedDialog
          open={removedOpen}
          rows={removed}
          project={project}
          now={now}
          onClose={() => setRemovedOpen(false)}
          onRestored={async () => {
            await Promise.all([loadList(), loadRemoved(), onChanged()]);
          }}
        />
      </div>
    </>
  );
}

function RemoveDialog({
  open,
  row,
  under,
  parentTitle,
  project,
  onCancel,
  onRemoved,
}: {
  open: boolean;
  row: LedgerRow;
  under: LedgerRow[];
  parentTitle: string | null;
  project: string;
  onCancel: () => void;
  onRemoved: () => Promise<void>;
}) {
  const id = useId();
  const [choice, setChoice] = useState<"move-up" | "remove">("move-up");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  useEffect(() => {
    if (open) {
      setChoice("move-up");
      setNote("");
      setRefusal(null);
    }
  }, [open]);
  const n = under.length;
  const submit = async () => {
    if (busy) return;
    setBusy(true);
    setRefusal(null);
    try {
      const args: Record<string, unknown> = { project, id: row.id };
      if (note.trim()) args.note = note;
      if (n) args.children = choice;
      const out = await callCli("khipu_ledger_remove", args);
      if (!out.ok) {
        setRefusal(out.error);
        return;
      }
      await onRemoved();
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open={open} className="kit-dialog" ariaLabelledBy={`${id}-t`} onCancel={onCancel}>
      <form
        className="kit-dialog-body lg-remove"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <h2 id={`${id}-t`}>Remove {row.id}?</h2>
        <p>It leaves every list at once. You can restore it from the Removed list for 30 days.</p>
        {n ? (
          <fieldset>
            <legend>{n === 1 ? "The 1 row under it" : `The ${n.toLocaleString()} rows under it`}</legend>
            <label>
              <input type="radio" name={`${id}-c`} value="remove" checked={choice === "remove"}
                onChange={() => setChoice("remove")} />
              {n === 1 ? "Remove it too" : "Remove them too"}
            </label>
            <label>
              <input type="radio" name={`${id}-c`} value="move-up" checked={choice === "move-up"}
                onChange={() => setChoice("move-up")} />
              {n === 1 ? "Move it up to " : "Move them up to "}
              {parentTitle ?? "the top level"}
            </label>
          </fieldset>
        ) : null}
        <label className="wk-field" htmlFor={`${id}-n`}>
          Your note
          <textarea id={`${id}-n`} value={note} onChange={(e) => setNote(e.target.value)} />
        </label>
        {refusal ? (
          <p className="set-error" role="alert">
            Khipu didn't make that change: {refusal}
          </p>
        ) : null}
        <div className="kit-dialog-actions">
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
          <button type="submit" className="danger" disabled={busy}>
            {busy ? "Removing…" : "Remove"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function RemovedDialog({
  open,
  rows,
  project,
  now,
  onClose,
  onRestored,
}: {
  open: boolean;
  rows: RemovedRow[];
  project: string;
  now: Date;
  onClose: () => void;
  onRestored: () => Promise<void>;
}) {
  const id = useId();
  const [busy, setBusy] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const restore = async (rowId: string) => {
    if (busy || !usableRowId(rowId)) return;
    setBusy(rowId);
    setRefusal(null);
    try {
      const out = await callCli("khipu_ledger_restore", { project, id: rowId });
      if (!out.ok) setRefusal(out.error);
      await onRestored();
    } finally {
      setBusy(null);
    }
  };
  return (
    <Dialog open={open} className="kit-dialog" ariaLabelledBy={`${id}-t`} onCancel={onClose}>
      <div className="kit-dialog-body">
        <h2 id={`${id}-t`}>Removed rows</h2>
        {rows.length === 0 ? <p>No removed rows to restore.</p> : null}
        <ul className="lg-removed-list" aria-label="Removed rows">
          {rows.map((r) => (
            <li key={r.id}>
              <span>
                <span className="lg-id">{r.id}</span> <span className="lg-removed-title">{r.title}</span>
              </span>
              {usableRowId(r.id) ? (
                <button type="button" className="sm" disabled={busy !== null} aria-label={`Restore ${r.id}`}
                  onClick={() => void restore(r.id)}>
                  {busy === r.id ? "Restoring…" : "Restore"}
                </button>
              ) : (
                <span className="meta">Its id was withheld, so it can't be restored here.</span>
              )}
              <span className="meta">
                Removed {changedWords(r.removed_at, now)} by {r.removed_by || "someone not recorded"}
                {r.days_left != null ? `. Can be restored for ${plural(r.days_left, "more day")}.` : "."}
              </span>
            </li>
          ))}
        </ul>
        {refusal ? (
          <p className="set-error" role="alert">
            Khipu didn't make that change: {refusal}
          </p>
        ) : null}
        <div className="kit-dialog-actions">
          <button type="button" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </Dialog>
  );
}
