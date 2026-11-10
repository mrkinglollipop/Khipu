/* Memory › Project work lists (slice A8). The projects on the left and the
   chosen project's list beside it: Not done, Done this week, and See
   everything. A line can be marked done (or not done) from here; the change
   is written the way the old ledger page writes it (`khipu_ledger_set`,
   with `if_state` so a line that moved meanwhile is never overwritten) and
   the list is read again at once, so what shows is what Khipu holds.
   Self-contained: it reads and writes only through `callCli`, and brings its
   own CSS. It draws no title bar; the Memory place around it does. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { callCli } from "../settingsCli";
import { Body, Split, useListKeys } from "../shell";
import { appliedFinished, changedWords, meaningOf, plural, stateWords, unappliedWords } from "../work";
import type { LedgerList, LedgerRow, LedgerSetReply, Vocab } from "../work";
import "./projectLists.css";

export type WorkProject = {
  project: string;
  name: string;
  hidden?: boolean;
  rows: number;
  open: number;
  needs_owner?: number;
};

const WEEK_MS = 7 * 86400000;

function isFinished(vocab: Vocab | null, state: string): boolean {
  const m = meaningOf(vocab, state);
  return m === "done" || m === "dropped";
}

/** The word to write when a line is marked done or not done, from the
 *  project's own words: DONE when it has it, else its first done word; and
 *  for not done, its first word that means open. */
function targetState(vocab: Vocab | null, to: "done" | "open"): string | null {
  const words = (vocab?.state ?? []).filter(([, m]) => m === to).map(([w]) => w);
  if (to === "done") return words.includes("DONE") ? "DONE" : (words[0] ?? null);
  return words.includes("NOT STARTED") ? "NOT STARTED" : (words[0] ?? null);
}

function byYou(row: LedgerRow): boolean {
  const by = row.history?.[0]?.by ?? "";
  return /^you\b/i.test(by);
}

function lineNote(row: LedgerRow, vocab: Vocab | null, now: Date, marked: "done" | "open" | undefined): string {
  const m = meaningOf(vocab, row.state);
  const finished = m === "done" || m === "dropped";
  if (marked === "done" || (finished && byYou(row))) return m === "dropped" ? "Dropped by you" : "Marked done by you";
  if (marked === "open" || (!finished && byYou(row) && row.history?.[0]?.to === row.state)) return "Marked not done by you";
  if (finished) return changedWords(row.updated, now);
  const owner = (row.owner ?? "").trim();
  const hasOwner = owner && !["none", "unassigned", "unowned", "-", "n/a"].includes(owner.toLowerCase());
  if (m === "needs") return "Waiting for your yes";
  if (hasOwner && row.state === "IN FLIGHT") return `${owner} is on it`;
  return stateWords(row.state);
}

export function ProjectLists({ onOwedClose }: { onOwedClose?: (id: number) => Promise<void> | void }) {
  const keys = useListKeys();
  const [projects, setProjects] = useState<WorkProject[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [opened, setOpened] = useState(false);

  const loadProjects = useCallback(async () => {
    const res = await callCli<{ projects?: WorkProject[] }>("khipu_ledger_projects");
    if (res.ok && Array.isArray(res.data.projects)) {
      setProjects(res.data.projects.filter((p) => !p.hidden));
      setError(null);
    } else {
      setError(res.ok ? "Khipu's answer had no list of projects." : res.error);
    }
  }, []);
  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

  useEffect(() => {
    if (selected || !projects) return;
    const first = projects.find((p) => p.rows > 0);
    if (first) setSelected(first.project);
  }, [projects, selected]);

  const current = projects?.find((p) => p.project === selected) ?? null;

  const list = (
    <>
      <p className="pl-cap">What is done and not done in each project. Agents keep it as they work; you can change any line.</p>
      {error ? <p className="pl-error" role="alert">Can't show the projects: {error}</p> : null}
      {projects && projects.length === 0 ? <p className="pl-empty">No projects yet.</p> : null}
      {projects === null && !error ? <p className="pl-empty">Reading the projects…</p> : null}
      <div className="pl-projects" {...keys}>
        {(projects ?? []).map((p) => {
          const has = p.rows > 0;
          const done = Math.max(0, p.rows - p.open);
          const on = has && selected === p.project;
          return (
            <button
              key={p.project}
              type="button"
              className="sh-row pl-proj"
              aria-current={on ? "true" : undefined}
              aria-disabled={has ? undefined : "true"}
              onClick={() => {
                if (!has) return;
                setSelected(p.project);
                setOpened(true);
              }}
            >
              <span className="pl-name">{p.name}</span>
              <span className={has ? "pl-count" : "pl-count muted"}>{has ? `${p.open} not done · ${done} done` : "No work list yet"}</span>
            </button>
          );
        })}
      </div>
    </>
  );

  const detail = current ? (
    <ProjectPage key={current.project} project={current} onBack={() => setOpened(false)} onChanged={loadProjects} onOwedClose={onOwedClose} />
  ) : (
    <p className="pl-empty">{projects === null ? "" : "Pick a project to see its list."}</p>
  );

  return (
    <Body>
      <Split list={list} listLabel="Projects" detail={detail} detailLabel={current ? `${current.name} work list` : "Work list"} detailOpen={opened && !!current} />
    </Body>
  );
}

function ProjectPage({
  project,
  onBack,
  onChanged,
  onOwedClose,
}: {
  project: WorkProject;
  onBack: () => void;
  onChanged: () => Promise<void>;
  onOwedClose?: (id: number) => Promise<void> | void;
}) {
  const id = project.project;
  const now = new Date();
  const [data, setData] = useState<LedgerList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [everything, setEverything] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [marked, setMarked] = useState<Record<string, "done" | "open">>({});
  const [refusal, setRefusal] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const latest = useRef(0);

  const read = useCallback(async () => {
    const mine = ++latest.current;
    const res = await callCli<LedgerList>("khipu_ledger_list", { project: id });
    if (mine !== latest.current) return;
    if (res.ok && Array.isArray(res.data.rows)) {
      setData(res.data);
      setError(null);
    } else {
      setError(res.ok ? "Khipu's answer could not be read." : res.error);
    }
  }, [id]);
  useEffect(() => {
    void read();
  }, [read]);

  const vocab = data?.vocab ?? null;
  const { open, doneWeek, finished } = useMemo(() => {
    const rows = data?.rows ?? [];
    const open: LedgerRow[] = [];
    const finished: LedgerRow[] = [];
    for (const r of rows) (isFinished(vocab, r.state) ? finished : open).push(r);
    finished.sort((a, b) => Date.parse(b.updated) - Date.parse(a.updated));
    const doneWeek = finished.filter((r) => Date.now() - Date.parse(r.updated) <= WEEK_MS);
    return { open, doneWeek, finished };
  }, [data, vocab]);

  const mark = async (row: LedgerRow, to: "done" | "open") => {
    if (busy) return;
    const state = targetState(vocab, to);
    if (!state) {
      setRefusal(`This project has no word for "${to === "done" ? "done" : "not done"}", so the line was left as it is.`);
      return;
    }
    setBusy(row.id);
    setRefusal(null);
    setNotice(null);
    try {
      const out = await callCli<LedgerSetReply>("khipu_ledger_set", { project: id, id: row.id, change: { state, if_state: row.state } });
      if (!out.ok) {
        setRefusal(out.error);
      } else {
        const reply = out.data;
        const commitment = row.refs?.commitment;
        if (to === "done" && commitment != null && commitment !== "" && appliedFinished(reply, vocab) && onOwedClose) {
          await onOwedClose(Number(commitment));
        }
        setNotice(unappliedWords(reply, false));
        if (!reply?.queued && !reply?.late) setMarked((m) => ({ ...m, [row.id]: to }));
      }
      await read();
      await onChanged();
    } finally {
      setBusy(null);
    }
  };

  const line = (r: LedgerRow, kind: "open" | "done") => (
    <li key={r.id} className="pl-line">
      <span className="pl-title">{r.title}</span>
      <span className="pl-note">{lineNote(r, vocab, now, marked[r.id])}</span>
      {kind === "open" ? (
        <button type="button" className="pl-act" disabled={busy !== null} aria-label={`Mark done: ${r.title}`} onClick={() => void mark(r, "done")}>
          Mark done
        </button>
      ) : (
        <button type="button" className="pl-act" disabled={busy !== null} aria-label={`Mark not done: ${r.title}`} onClick={() => void mark(r, "open")}>
          Mark not done
        </button>
      )}
    </li>
  );

  const shown = everything ? finished : doneWeek;
  const lastChange = data?.rows.reduce<string | null>((acc, r) => (!acc || Date.parse(r.updated) > Date.parse(acc) ? r.updated : acc), null);

  return (
    <div className="pl-page">
      <button type="button" className="pl-back" onClick={onBack}>
        <ArrowLeft size={16} aria-hidden /> Projects
      </button>
      <header className="pl-head">
        <h2>{project.name}</h2>
        <p className="pl-sub">Work list</p>
      </header>
      {error ? <p className="pl-error" role="alert">Can't show this list: {error}</p> : null}
      {data?.offline ? (
        <p className="pl-note-box" role="status">The database didn't answer, so this is the copy Khipu kept on this Mac. Changes you make are saved here and sent when it answers.</p>
      ) : null}
      {data ? (
        <p className="pl-kept">Kept by your agents as they work.{lastChange ? ` Last change ${changedWords(lastChange, now)}.` : ""}</p>
      ) : !error ? (
        <p className="pl-kept">Reading the list…</p>
      ) : null}
      {refusal ? <p className="pl-error" role="alert">Not changed: {refusal}</p> : null}
      {notice ? <p className="pl-note-box" role="status">{notice}</p> : null}
      {data ? (
        <>
          <section aria-labelledby={`pl-open-${id}`}>
            <h3 id={`pl-open-${id}`}>Not done</h3>
            {open.length ? <ul className="pl-lines">{open.map((r) => line(r, "open"))}</ul> : <p className="pl-empty">Nothing is waiting.</p>}
          </section>
          <section aria-labelledby={`pl-done-${id}`}>
            <h3 id={`pl-done-${id}`}>{everything ? "Everything done" : "Done this week"}</h3>
            {shown.length ? <ul className="pl-lines">{shown.map((r) => line(r, "done"))}</ul> : <p className="pl-empty">Nothing was finished this week.</p>}
          </section>
          <p className="pl-foot">
            {plural(finished.length, "line")} done in all.{" "}
            {finished.length > doneWeek.length || everything ? (
              <button type="button" className="pl-link" aria-pressed={everything} onClick={() => setEverything((v) => !v)}>
                {everything ? "Back to this week" : "See everything"}
              </button>
            ) : null}
          </p>
        </>
      ) : null}
    </div>
  );
}
