/* The Work tab (docs/plans/2026-10-15-work-view.md, phase 4; mocks
   docs/mockups/2026-10-15-work-view/work.html and work-states.html):
   Waiting on you, Running now, Accounts (the Home Routing card's rows),
   one board per project, then the Owed section. Every write goes through a
   fixed-argv command and is followed by a fresh read of the view, so the
   rail badge moves at once. Khipu shows this; it never starts, stops or
   moves work. */

import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Dialog, EmptyState, Tag } from "./ui";
import { AccountRows } from "./RoutingCard";
import type { UsageReport } from "./PlansCard";
import { accountState } from "./routing";
import type { RoutingState } from "./routing";
import { callCli } from "./settingsCli";
import { LedgerPage, OutboxNotice } from "./LedgerPage";
import {
  WITHHELD,
  WORK_FOOT,
  WORK_LEDE,
  ageWords,
  boardFolds,
  boardMeta,
  canAct,
  changedWords,
  daysSince,
  foldedMeta,
  goalBar,
  jobAccount,
  jobFor,
  jobTone,
  meaningOf,
  plural,
  runTone,
  runWords,
  sourceLine,
  stateWords,
  timeOnly,
  updatedWords,
  wordFor,
} from "./work";
import type { Vocab, WaitItem, WorkBoard, WorkJob, WorkPr, WorkState, WorkView as WorkViewDoc } from "./work";
import "./Work.css";

const FIRST_RUN_HINT =
  "A project shows here once a conversation adds its first ledger row, or when you bring in a tracker you already keep or a job runner's summary file.";
const WAIT_FIRST = 8;

const OWED_KIND_WORDS: Record<string, string> = {
  blocker: "blocker",
  question: "question",
  followup: "follow-up",
  "follow-up": "follow-up",
  promise: "promise",
};

export type WorkViewProps = {
  work: WorkState;
  routing: RoutingState;
  plans?: UsageReport | null;
  /** The Owed section's body, rendered by App so its state stays there. */
  owedSection: ReactNode;
  /** Bumped by App to open Work at the Owed section. */
  owedFocusKey: number;
  /** Closes a commitment through App's Owed path. */
  onOwedClose: (id: number) => Promise<void>;
  /** Opens App's Snooze dialog for one commitment. */
  onSnooze: (id: string, title: string) => void;
  onOpenRouting: () => void;
};

type LedgerTarget = { project: string; name: string; row?: string };

/** A board's name; its key (already through work.py's check) when the name
 *  was withheld. */
function boardName(board: WorkBoard): string {
  return board.name ?? board.project ?? WITHHELD.name;
}

function projectName(view: WorkViewDoc, project: string | null): string {
  if (!project) return "Khipu";
  const board = view.projects.find((p) => p.project === project);
  if (board) return boardName(board);
  return project.split("/").pop() || project;
}

/** A waiting item's project words. A ledger row always has a project, so
 *  one without was withheld; any other item without one is Khipu's own. */
function waitProject(view: WorkViewDoc, item: WaitItem): string {
  if (item.source === "ledger" && !item.project) return WITHHELD.name;
  return projectName(view, item.project);
}

function DragHeader({ title, lede, children }: { title: string; lede: string; children?: ReactNode }) {
  return (
    <header className="panel-head" data-tauri-drag-region>
      <div className="panel-head-text" data-tauri-drag-region>
        <h1 data-tauri-drag-region>{title}</h1>
        <p className="lede" data-tauri-drag-region>
          {lede}
        </p>
      </div>
      <div className="panel-head-actions">{children}</div>
    </header>
  );
}

const PROBLEM_WORDS: Record<string, string> = {
  ledger: "The ledger couldn't be read just now; what shows from it may be out of date.",
  owed: "Owed couldn't be read just now; commitments may be missing from Waiting on you.",
  routing: "The routing record couldn't be read just now; routing items may be missing.",
  privacy:
    "This view is incomplete: Khipu couldn't load its list of login folders and user names, so every name, title and line that could hold one is left out.",
};

export function WorkView(props: WorkViewProps) {
  const { work, owedFocusKey } = props;
  const [ledger, setLedger] = useState<LedgerTarget | null>(null);
  const lastFocus = useRef(owedFocusKey);
  const now = new Date();

  useEffect(() => {
    if (owedFocusKey === lastFocus.current) return;
    lastFocus.current = owedFocusKey;
    setLedger(null);
    requestAnimationFrame(() => {
      const el = document.getElementById("work-owed");
      if (el && typeof el.scrollIntoView === "function") el.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }, [owedFocusKey]);

  const view = work.data;
  const reload = () => work.load(true);

  if (ledger) {
    return (
      <LedgerPage
        key={ledger.project}
        project={ledger.project}
        name={ledger.name}
        initialRow={ledger.row}
        view={view}
        onBack={() => setLedger(null)}
        onChanged={reload}
        onOwedClose={props.onOwedClose}
      />
    );
  }

  const openLedger = (project: string, row?: string) => {
    if (!view) return;
    setLedger({ project, name: projectName(view, project), row });
  };
  const openOwed = () => {
    const el = document.getElementById("work-owed");
    if (el && typeof el.scrollIntoView === "function") el.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const header = (
    <DragHeader title="Work" lede={WORK_LEDE}>
      {work.readAt != null ? <span className="meta">Updated {updatedWords(new Date(work.readAt).toISOString(), now)}</span> : null}
    </DragHeader>
  );

  const owed = (
    <section id="work-owed" className="wk-owed" aria-labelledby="work-owed-title">
      <div className="wk-owed-head">
        <h2 id="work-owed-title">Owed</h2>
      </div>
      {props.owedSection}
    </section>
  );

  if (!view) {
    return (
      <>
        {header}
        <div className="panel-body wide wk-panel">
          <div className="card">
            {work.error ? (
              <EmptyState title="Could not read the work view" hint={work.error} />
            ) : (
              <EmptyState title={work.loading ? "Reading the work view" : "Work view not read yet"} hint={WORK_LEDE} />
            )}
          </div>
          {owed}
          <p className="wk-foot">{WORK_FOOT}</p>
        </div>
      </>
    );
  }

  const problems = Object.keys(view.problems ?? {}).filter((k) => PROBLEM_WORDS[k]);
  const firstRun = view.projects.length === 0 && view.waiting.length === 0;
  const waitingBy = new Map<string, number>();
  for (const w of view.waiting) {
    if (w.project) waitingBy.set(w.project, (waitingBy.get(w.project) ?? 0) + 1);
  }

  return (
    <>
      {header}
      <div className="panel-body wide wk-panel">
        {view.counts.ledger_outbox > 0 || problems.length || work.error ? (
          <div className="wk-notices">
            <OutboxNotice view={view} now={now} />
            {problems.map((k) => (
              <p key={k} className="wk-problem">
                {PROBLEM_WORDS[k]}
              </p>
            ))}
            {work.error ? <p className="wk-problem">{work.error}</p> : null}
          </div>
        ) : null}
        {firstRun ? (
          <div className="card">
            <EmptyState
              icon={
                <svg width="28" height="28" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"
                  strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                  <rect x="2.5" y="3" width="11" height="10" rx="1.5" />
                  <path d="M6 3v10M10 3v10" />
                </svg>
              }
              title="No projects yet"
              hint={FIRST_RUN_HINT}
            />
          </div>
        ) : (
          <>
            <WaitingCard
              view={view}
              now={now}
              reload={reload}
              onOpenLedger={openLedger}
              onOpenOwed={openOwed}
              onOwedClose={props.onOwedClose}
              onSnooze={props.onSnooze}
              onOpenRouting={props.onOpenRouting}
            />
            <RunningCard view={view} routing={props.routing} now={now} />
            <div className="card routing-card">
              <div className="card-head">
                <h2 className="wk-head">Accounts</h2>
                <span className="spacer" />
                <button type="button" className="sm link" onClick={props.onOpenRouting}>
                  Routing settings
                </button>
              </div>
              {props.routing.data?.record ? (
                <AccountRows state={props.routing} plans={props.plans} now={now} />
              ) : props.routing.data ? (
                <EmptyState title="No routing record on this Mac" hint="Accounts show here once Khipu keeps a routing record." />
              ) : (
                <EmptyState
                  title={props.routing.error ? "Could not read the routing record" : "Reading the routing record"}
                  hint={props.routing.error ?? "Which accounts can take work right now, and who said so."}
                />
              )}
            </div>
            {view.projects.length ? (
              <div className="wk-boards">
                {view.projects.map((b, i) => (
                  <Board key={b.project ?? `withheld-${i}`} board={b} waiting={b.project ? (waitingBy.get(b.project) ?? 0) : 0}
                    now={now} onOpenLedger={b.project ? () => openLedger(b.project!) : null} />
                ))}
              </div>
            ) : null}
          </>
        )}
        {owed}
        <p className="wk-foot">{WORK_FOOT}</p>
      </div>
    </>
  );
}

/* ---- Waiting on you ---------------------------------------------------------- */

function sourceWords(item: WaitItem): string {
  if (item.source === "ledger") return "Ledger: needs you";
  if (item.source === "routing") return "Routing";
  const kind = item.kind ? (OWED_KIND_WORDS[item.kind] ?? item.kind) : null;
  return kind ? `Owed: ${kind}` : "Owed";
}

function vocabOf(view: WorkViewDoc, project: string | null): Vocab | null {
  return view.projects.find((p) => p.project === project)?.ledger?.vocab ?? null;
}

function needsWord(vocab: Vocab | null): string | undefined {
  return vocab?.state.find(([, m]) => m === "needs")?.[0];
}

function WaitingCard({
  view,
  now,
  reload,
  onOpenLedger,
  onOpenOwed,
  onOwedClose,
  onSnooze,
  onOpenRouting,
}: {
  view: WorkViewDoc;
  now: Date;
  reload: () => Promise<void>;
  onOpenLedger: (project: string, row?: string) => void;
  onOpenOwed: () => void;
  onOwedClose: (id: number) => Promise<void>;
  onSnooze: (id: string, title: string) => void;
  onOpenRouting: () => void;
}) {
  const [filter, setFilter] = useState<string>("all");
  const [all, setAll] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [answering, setAnswering] = useState<WaitItem | null>(null);
  const items = view.waiting;
  const nameOf = (w: WaitItem) => waitProject(view, w);
  const groups = useMemo(() => {
    const counts = new Map<string, number>();
    for (const w of items) {
      const n = waitProject(view, w);
      counts.set(n, (counts.get(n) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  }, [items, view]);
  const blocking = items.filter((w) => w.blocking).length;
  const shown = items.filter((w) =>
    filter === "all" ? true : filter === "blocking" ? w.blocking : nameOf(w) === filter.slice(2),
  );
  const visible = all ? shown : shown.slice(0, WAIT_FIRST);

  const write = async (key: string | null, run: () => Promise<{ ok: boolean; error?: string }>) => {
    if (busy || key == null) return;
    setBusy(key);
    setRefusal(null);
    try {
      const out = await run();
      if (!out.ok) setRefusal(out.error ?? "Khipu refused the change.");
    } finally {
      setBusy(null);
      await reload();
    }
  };

  const setLedgerState = async (item: WaitItem, state: string, note?: string) => {
    const vocab = vocabOf(view, item.project);
    const ifState = needsWord(vocab);
    const out = await callCli("khipu_ledger_set", {
      project: item.project,
      id: item.id,
      change: { state, ...(note && note.trim() ? { note } : {}), ...(ifState ? { if_state: ifState } : {}) },
    });
    if (!out.ok) return out;
    const meaning = meaningOf(vocab, state);
    if (item.also_owed && (meaning === "done" || meaning === "dropped")) {
      await onOwedClose(Number(item.also_owed));
    }
    return out;
  };

  const drop = (item: WaitItem) => {
    const word = wordFor(vocabOf(view, item.project), "dropped");
    if (!word) {
      setRefusal("Khipu didn't find this project's word for dropped.");
      return;
    }
    void write(item.key, () => setLedgerState(item, word));
  };

  const done = (item: WaitItem) => {
    if (item.source === "ledger") {
      const word = wordFor(vocabOf(view, item.project), "done");
      if (!word) {
        setRefusal("Khipu didn't find this project's word for done.");
        return;
      }
      void write(item.key, () => setLedgerState(item, word));
    } else if (item.source === "commitment") {
      void write(item.key, async () => {
        await onOwedClose(Number(item.id));
        return { ok: true };
      });
    }
  };

  return (
    <div className="card">
      <div className="card-head">
        <h2 className="wk-head">Waiting on you</h2>
        {items.length ? (
          <span className="meta">
            {" "}
            {items.length.toLocaleString()} across {plural(groups.length, "project")}
            {blocking ? ` · ${blocking.toLocaleString()} blocking` : ""}
          </span>
        ) : null}
        <span className="spacer" />
      </div>
      {items.length === 0 ? (
        <div className="row">
          <span className="hdot ok" aria-hidden />
          <span className="grow t2">Nothing is waiting on you in any project.</span>
        </div>
      ) : (
        <>
          <div className="wk-filters">
            <div className="seg" role="group" aria-label="Show">
              {[
                ["all", `All ${items.length.toLocaleString()}`],
                ["blocking", `Blocking ${blocking.toLocaleString()}`],
                ...groups.map(([n, c]) => [`p:${n}`, `${n} ${c.toLocaleString()}`]),
              ].map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  className={filter === value ? "on" : undefined}
                  aria-pressed={filter === value}
                  onClick={() => setFilter(value)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          {refusal ? (
            <div className="set-error wk-alert" role="alert">
              Khipu didn't make that change: {refusal}
            </div>
          ) : null}
          <div role="list" aria-label="Waiting on you">
            {visible.map((item, i) => {
              const age = ageWords(item.since, now);
              const long = (daysSince(item.since, now) ?? 0) >= 2;
              const isBusy = item.key != null && busy === item.key;
              // A change needs the item's key, id and (for a ledger row) project.
              const canWrite = canAct(item);
              return (
                <div key={item.key ?? `withheld-${i}`} className="row wk-wait" role="listitem">
                  <span className="wk-wait-main">
                    <span className="wk-wait-line">
                      {item.blocking ? <Tag tone="err">Blocking</Tag> : null}
                      <span className={item.title == null ? "wk-wait-text t2" : "wk-wait-text"}>
                        {item.title ?? WITHHELD.title}
                      </span>
                    </span>
                    <span className="wk-wait-sub">
                      <span>{nameOf(item)}</span>
                      <span aria-hidden>·</span>
                      <span>{sourceWords(item)}</span>
                      {item.evidence ? (
                        <>
                          <span aria-hidden>·</span>
                          <span>{item.evidence}</span>
                        </>
                      ) : null}
                      {item.also_owed ? <Tag>Also in Owed</Tag> : null}
                      {item.source === "commitment" && item.looks_like && item.looks_like.id && canWrite ? (
                        <>
                          <span aria-hidden>·</span>
                          <span>
                            {item.looks_like.title != null
                              ? `Looks like “${item.looks_like.title}” in the ledger`
                              : "Looks like a ledger row whose title is withheld"}
                          </span>
                          {canAct(item, "same") ? (
                            <>
                              <button type="button" className="sm link" disabled={busy !== null}
                                onClick={() => void write(item.key, () => callCli("khipu_work_same", {
                                  project: item.project,
                                  row: item.looks_like!.id,
                                  commitment: item.id,
                                  same: true,
                                }))}>
                                These are the same
                              </button>
                              <button type="button" className="sm link" disabled={busy !== null}
                                onClick={() => void write(item.key, () => callCli("khipu_work_same", {
                                  project: item.project,
                                  row: item.looks_like!.id,
                                  commitment: item.id,
                                  same: false,
                                }))}>
                                Not the same
                              </button>
                            </>
                          ) : null}
                        </>
                      ) : null}
                    </span>
                  </span>
                  <span className={long ? "wk-age long" : "wk-age"}>{age}</span>
                  <span className="wk-acts">
                    {item.source === "ledger" && canWrite ? (
                      <>
                        {canAct(item, "done", vocabOf(view, item.project)) ? (
                          <button type="button" className="sm primary" disabled={busy !== null} onClick={() => done(item)}>
                            {isBusy ? "Saving…" : "Done"}
                          </button>
                        ) : null}
                        {canAct(item, "drop", vocabOf(view, item.project)) ? (
                          <button type="button" className="sm" disabled={busy !== null} onClick={() => drop(item)}>
                            Drop
                          </button>
                        ) : null}
                        <button type="button" className="sm" disabled={busy !== null} onClick={() => setAnswering(item)}>
                          Answered…
                        </button>
                        <button type="button" className="sm" onClick={() => onOpenLedger(item.project!, item.id!)}>
                          Open in ledger
                        </button>
                        {item.link ? (
                          <a className="sm" href={item.link} target="_blank" rel="noreferrer">
                            Open link
                          </a>
                        ) : null}
                      </>
                    ) : null}
                    {item.source === "commitment" && canWrite ? (
                      <>
                        <button type="button" className="sm primary" disabled={busy !== null} onClick={() => done(item)}>
                          {isBusy ? "Saving…" : "Done"}
                        </button>
                        <button type="button" className="sm" onClick={() => onSnooze(item.id!, item.title ?? WITHHELD.title)}>
                          Snooze
                        </button>
                        <button type="button" className="sm" disabled={busy !== null}
                          onClick={() => void write(item.key, () => callCli("khipu_work_hide", { id: item.id, hide: true }))}>
                          Hide until it changes
                        </button>
                        <button type="button" className="sm link" onClick={onOpenOwed}>
                          Open in Owed
                        </button>
                      </>
                    ) : null}
                    {item.source === "routing" && canWrite ? (
                      <button type="button" className="sm" onClick={onOpenRouting}>
                        Open Routing settings
                      </button>
                    ) : null}
                  </span>
                </div>
              );
            })}
          </div>
          {shown.length > WAIT_FIRST ? (
            <div className="row wk-more">
              <button type="button" className="sm" aria-expanded={all} onClick={() => setAll((v) => !v)}>
                {all ? "Show fewer" : `Show all ${shown.length.toLocaleString()}`}
              </button>
            </div>
          ) : null}
        </>
      )}
      <AnswerDialog
        item={answering}
        vocab={answering ? vocabOf(view, answering.project) : null}
        onCancel={() => setAnswering(null)}
        onSave={(state, note) => {
          const item = answering;
          setAnswering(null);
          if (item) void write(item.key, () => setLedgerState(item, state, note));
        }}
      />
    </div>
  );
}

const DEFAULT_WORDS: Array<[string, string]> = [
  ["DONE", "done"],
  ["DROPPED", "dropped"],
];

function AnswerDialog({
  item,
  vocab,
  onCancel,
  onSave,
}: {
  item: WaitItem | null;
  vocab: Vocab | null;
  onCancel: () => void;
  onSave: (state: string, note: string) => void;
}) {
  const id = useId();
  const words = vocab?.state.length ? vocab.state : DEFAULT_WORDS;
  const doneWord = wordFor(vocab, "done") ?? "DONE";
  const [state, setState] = useState(doneWord);
  const [note, setNote] = useState("");
  const key = item?.key ?? "";
  useEffect(() => {
    setState(doneWord);
    setNote("");
  }, [key, doneWord]);
  const meaning = meaningOf(vocab, state);
  return (
    <Dialog open={item != null} className="kit-dialog" ariaLabelledBy={`${id}-t`} onCancel={onCancel}>
      <form
        className="kit-dialog-body"
        onSubmit={(e) => {
          e.preventDefault();
          onSave(state, note);
        }}
      >
        <h2 id={`${id}-t`}>Answered</h2>
        <p>{item ? (item.title ?? WITHHELD.title) : null}</p>
        <label className="wk-field" htmlFor={`${id}-s`}>
          New state
          <select id={`${id}-s`} value={state} onChange={(e) => setState(e.target.value)}>
            {words
              .filter(([, m]) => m !== "needs")
              .map(([w]) => (
                <option key={w} value={w}>
                  {stateWords(w)}
                </option>
              ))}
          </select>
        </label>
        <label className="wk-field" htmlFor={`${id}-n`}>
          Your note
          <textarea id={`${id}-n`} value={note} onChange={(e) => setNote(e.target.value)} />
        </label>
        <p className="meta">
          Saved as “you, in the app”.
          {item?.also_owed && (meaning === "done" || meaning === "dropped") ? " The Owed item closes too." : ""}
        </p>
        <div className="kit-dialog-actions">
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
          <button type="submit" className="primary">
            Save
          </button>
        </div>
      </form>
    </Dialog>
  );
}

/* ---- Running now ------------------------------------------------------------- */

function newestLines(job: WorkJob): WorkJob["now"] {
  return [...(job.now ?? [])].sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
}

function RunningCard({ view, routing, now }: { view: WorkViewDoc; routing: RoutingState; now: Date }) {
  const [open, setOpen] = useState<Set<string>>(new Set());
  const jobs = view.live;
  const projects = new Set(jobs.map((j, i) => j.project ?? `withheld-${i}`));
  const reporting = view.projects.filter((p) => p.source.state === "ok").length;
  const accounts = routing.data?.accounts ?? [];
  const toggle = (key: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  return (
    <div className="card">
      <div className="card-head">
        <h2 className="wk-head">Running now</h2>
        {jobs.length ? (
          <span className="meta">
            {" "}
            {plural(jobs.length, "job")} in {plural(projects.size, "project")}
          </span>
        ) : null}
      </div>
      {jobs.length === 0 ? (
        <div className="row">
          <span className="hdot idle" aria-hidden />
          <span className="grow t2">
            No jobs are running.
            {reporting ? ` ${plural(reporting, "project")} reported in the last minute.` : ""}
          </span>
        </div>
      ) : (
        <div role="list" aria-label="Running jobs">
          {jobs.map((job, i) => {
            const key = job.project != null ? `${job.project}#${job.id}` : `withheld-${i}#${job.id}`;
            const board = job.project != null ? view.projects.find((p) => p.project === job.project) : undefined;
            const label = (job.account_ref?.label ?? job.account ?? "").toLowerCase();
            const acct = label ? accounts.find((a) => a.label.toLowerCase() === label) : undefined;
            const tone = acct ? accountState(acct, now).tone : null;
            const lines = newestLines(job);
            const isOpen = open.has(key);
            const forWords = jobFor(job);
            return (
              <div key={key} className="row wk-job" role="listitem">
                <span className="wk-proj">{board ? boardName(board) : (job.project ?? WITHHELD.name)}</span>
                <span className="wk-job-what">
                  <span>
                    <button type="button" className="wk-job-title" aria-expanded={isOpen} onClick={() => toggle(key)}>
                      {job.summary ?? WITHHELD.summary}
                      {forWords ? (
                        <>
                          {" "}
                          <span className="wk-for">· {forWords}</span>
                        </>
                      ) : null}
                    </button>
                    {board?.source.state === "stale" ? (
                      <>
                        {" "}
                        <Tag tone="warn">{`as of ${timeOnly(board.source.as_of)}`}</Tag>
                      </>
                    ) : null}
                  </span>
                </span>
                <span className="wk-acct">
                  <span className={tone ? `hdot ${tone}` : "hdot"} aria-hidden />
                  {jobAccount(job)}
                </span>
                <span className="wk-dur">{ageWords(job.started_at, now)}</span>
                <span className="wk-state">
                  <span className={jobTone(job.state)} aria-hidden />
                  {job.state_words}
                </span>
                {job.quiet ? (
                  <span className="wk-quiet">No word from this job for {ageWords(job.updated_at, now)}</span>
                ) : lines.length ? (
                  <ul className="wk-now" aria-label="What it says it is doing">
                    {(isOpen ? lines.slice(0, 3) : lines.slice(0, 1)).map((l, n) => (
                      <li key={`${l.at}-${n}`}>
                        <time dateTime={l.at}>{timeOnly(l.at)}</time>
                        <span>{l.text ?? WITHHELD.line}</span>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/* ---- Boards ------------------------------------------------------------------ */

function prTag(pr: WorkPr): { text: string; tone: "warn" | "ok" | "neutral" } {
  if (pr.stage === "flagged") return { text: pr.stage_words, tone: "warn" };
  if (pr.verdict?.outcome === "flagged") return { text: `Flagged · ${pr.stage_words.toLowerCase()}`, tone: "warn" };
  if (pr.stage === "cleared" || pr.stage === "merged") return { text: pr.stage_words, tone: "ok" };
  return { text: pr.stage_words, tone: "neutral" };
}

function sentence(text: string): string {
  return /[.!?]$/.test(text) ? text : `${text}.`;
}

function Board({
  board,
  waiting,
  now,
  onOpenLedger,
}: {
  board: WorkBoard;
  waiting: number;
  now: Date;
  /** Null when the board's project key was withheld: its ledger can't be named. */
  onOpenLedger: (() => void) | null;
}) {
  const folds = boardFolds(board, waiting);
  const [unfolded, setUnfolded] = useState(false);
  const folded = folds && !unfolded;
  const line = sourceLine(board, now);
  const meta = folded ? foldedMeta(board, waiting, now) : boardMeta(board, waiting, now);
  const led = board.ledger;
  const vocab = led?.vocab ?? null;
  const openStates = led
    ? Object.entries(led.by_state)
        .filter(([w, n]) => n > 0 && meaningOf(vocab, w) === "open")
        .sort((a, b) => {
          const order = (vocab?.state ?? []).map(([w]) => w);
          return order.indexOf(a[0]) - order.indexOf(b[0]);
        })
    : [];
  const oldest = (led?.loose_ends ?? [])
    .filter((r) => r.stale)
    .sort((a, b) => Date.parse(a.updated_at ?? "") - Date.parse(b.updated_at ?? ""))[0];
  const oldestDays = oldest ? daysSince(oldest.updated_at, now) : null;
  const name = boardName(board);
  return (
    <div className={folded ? "card wk-board folded" : "card wk-board"}>
      <div className="card-head">
        <h2 className="wk-head">{name}</h2>
        {meta ? <span className="meta">{` · ${meta}`}</span> : null}
        <span className="spacer" />
        {folds ? (
          <button type="button" className="sm" aria-expanded={!folded} onClick={() => setUnfolded((v) => !v)}>
            {folded ? "Show" : "Fold"}
          </button>
        ) : null}
        {led && !folded && onOpenLedger ? (
          <button type="button" className="sm" onClick={onOpenLedger}>
            Open the ledger
          </button>
        ) : null}
      </div>
      {folded ? null : (
        <>
          <div className={line.tone && line.tone !== "ok" ? `wk-src ${line.tone}` : "wk-src"}>
            <span className={line.tone ? `hdot ${line.tone}` : "hdot"} aria-hidden />
            <span>{line.text}</span>
          </div>
          {board.source.state === "stale" ? (
            <div className="row">
              <span className="meta">
                Its jobs show with “as of {timeOnly(board.source.as_of)}”.{led ? " The ledger parts are live." : ""}
              </span>
            </div>
          ) : null}
          {led && led.goals.length ? (
            <>
              <h3 className="wk-sec">GOALS</h3>
              {led.goals.map((g) => {
                const bar = goalBar(g, vocab);
                return (
                  <div key={g.id} className="row wk-goal">
                    <span className="wk-goal-title">{g.title ?? WITHHELD.title}</span>
                    <span className="wk-bar" role="img" aria-label={bar.label}>
                      {bar.parts.map((p) => (
                        <i key={p.cls} className={p.cls} style={{ width: `${p.pct}%` }} />
                      ))}
                    </span>
                    <span className="wk-goal-n">{bar.words}</span>
                  </div>
                );
              })}
            </>
          ) : null}
          {led && led.loose_end_count > 0 ? (
            <>
              <h3 className="wk-sec">LOOSE ENDS</h3>
              <div className="wk-loose">
                {openStates.map(([w, n]) => (
                  <span key={w}>
                    <b>{n.toLocaleString()}</b> {stateWords(w).toLowerCase()}
                  </span>
                ))}
                {oldest && oldestDays != null ? (
                  <span className="t2">
                    {oldest.title != null ? `Oldest: “${oldest.title}”` : "Oldest: a row whose title is withheld"},{" "}
                    no change for {plural(oldestDays, "day")}
                  </span>
                ) : null}
              </div>
            </>
          ) : null}
          {board.prs.length ? (
            <>
              <h3 className="wk-sec">PULL REQUESTS</h3>
              <div role="list" aria-label={`${name} pull requests`}>
                {board.prs.map((pr) => {
                  const tag = prTag(pr);
                  const flagged = tag.tone === "warn";
                  const v = pr.verdict;
                  const by = [v?.by, v?.at ? timeOnly(v.at) : null].filter(Boolean).join(", ");
                  return (
                    <div key={pr.id} className={flagged ? "row wk-pr flagged" : "row wk-pr"} role="listitem">
                      <span className="wk-pr-n">{pr.id}</span>
                      <span className="wk-pr-title">{pr.title ?? WITHHELD.title}</span>
                      <Tag tone={tag.tone}>{tag.text}</Tag>
                      {v?.first_line ? (
                        <span className="wk-pr-verdict">
                          {by ? <b>{by}:</b> : null} {v.first_line}
                        </span>
                      ) : null}
                      {pr.disagrees ? <span className="wk-disagree">{sentence(pr.disagrees)}</span> : null}
                    </div>
                  );
                })}
              </div>
            </>
          ) : null}
          {board.trains.length ? (
            <>
              <h3 className="wk-sec">RELEASE TRAINS</h3>
              {board.trains.map((t) => (
                <div key={t.id} className="row wk-train">
                  <span className="wk-train-name">{t.name ?? WITHHELD.name}</span>
                  <Tag tone={t.state === "shipped" ? "ok" : t.state === "blocked" ? "warn" : "neutral"}>{t.state_words}</Tag>
                  {t.runs.length ? (
                    <ul className="wk-runs" aria-label="Test runs">
                      {t.runs.map((r, n) => (
                        <li key={`${r.name ?? ""}-${n}`}>
                          <span className={runTone(r)} aria-hidden />
                          {runWords(r)}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  {t.summary ? <span className="wk-train-sum">{t.summary}</span> : null}
                </div>
              ))}
            </>
          ) : null}
          {board.merged.length ? (
            <>
              <h3 className="wk-sec">MERGED THIS WEEK</h3>
              {board.merged.map((m) => (
                <div key={m.id} className="row wk-merge">
                  <span className="wk-pr-n">{m.id}</span>
                  <span className="t2 ellip">{m.title ?? WITHHELD.title}</span>
                  <span className="meta">{changedWords(m.merged_at, now)}</span>
                </div>
              ))}
            </>
          ) : null}
        </>
      )}
    </div>
  );
}
