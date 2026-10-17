/* Settings -> Work (docs/plans/2026-10-15-work-view.md, phase 5; mock
   docs/mockups/2026-10-15-work-view/settings-work.html). Ledger projects
   (rename, hide), job runner summary files (add with a first read, change,
   turn off, remove, suggested, what jobs say they are doing), every cap and
   time with Change... and No limit, and Owed items hidden from Waiting on
   you. Every write is a fixed-argv command (src-tauri lib.rs); the page reads
   again after each one instead of trusting what it sent. */

import { type FormEvent, type ReactNode, useCallback, useEffect, useId, useRef, useState } from "react";
import { open as openFileDialog } from "@tauri-apps/plugin-dialog";
import { type ConfigShape, callCli } from "./settingsCli";
import { type LimitUnit, limitWords } from "./routingWords";
import { type WorkBoard, type WorkState, plural, shortProject, sourceLine } from "./work";
import "./Work.css";

type Setting = { value: unknown; default: unknown; source?: string };
type WorkConfig = ConfigShape & {
  work?: Record<string, Setting>;
  ledger?: Record<string, Setting>;
  work_suggested?: Array<{ name: string; file: string }>;
};
export type SummaryEntry = { project: string; name?: string; file: string; on: boolean; show_now: boolean };
type LedgerProject = {
  project: string;
  name: string;
  hidden: boolean;
  created_at?: string | null;
  rows: number;
  open: number;
  needs_owner: number;
};
type HiddenItem = { id: string; text: string | null; project: string | null; kind: string | null };
type FirstRead = { ok: boolean; problems?: string[]; counts?: Record<string, number> | null; project?: string | null; written_at?: string | null };

/** One write at a time per control id, with its refusal kept beside it. */
function useWrites(reread: () => Promise<unknown>) {
  const [busy, setBusy] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const run = useCallback(
    async (id: string, command: string, args: Record<string, unknown> | (() => Promise<Record<string, unknown> | string>)) => {
      setBusy(id);
      try {
        const built = typeof args === "function" ? await args() : args;
        const res = typeof built === "string" ? { ok: false as const, error: built } : await callCli(command, built);
        setErrors((e) => {
          const next = { ...e };
          if (res.ok) delete next[id];
          else next[id] = res.error;
          return next;
        });
        await reread();
        return res.ok;
      } finally {
        setBusy(null);
      }
    },
    [reread],
  );
  return { busy, errors, run };
}
type Writes = ReturnType<typeof useWrites>;

function WriteError({ w, id }: { w: Writes; id: string }) {
  const msg = w.errors[id];
  return msg ? (
    <p className="set-error" role="alert">
      Khipu didn't make that change: {msg}
    </p>
  ) : null;
}

function dayWords(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString([], { month: "short", day: "numeric" });
}

/** "Found 1 job, 4 pull requests and no release trains, updated 6 s ago." */
export function foundWords(read: FirstRead, now: Date): string {
  const c = read.counts;
  if (!c) return read.problems?.[0] ?? "Khipu couldn't read that file.";
  const part = (n: number, one: string, many: string) => (n ? plural(n, one, many) : `no ${many}`);
  const items = [part(c.jobs ?? 0, "job", "jobs"), part(c.prs ?? 0, "pull request", "pull requests"), part(c.trains ?? 0, "release train", "release trains")];
  let when = "";
  const t = read.written_at ? Date.parse(read.written_at) : NaN;
  if (!Number.isNaN(t)) {
    const s = Math.max(0, Math.round((now.getTime() - t) / 1000));
    when = `, updated ${s < 60 ? `${s} s` : s < 3600 ? `${Math.round(s / 60)} min` : `${Math.round(s / 3600)} h`} ago`;
  }
  return `Found ${items[0]}, ${items[1]} and ${items[2]}${when}.`;
}

/** The project key a summary file names, matched as work.py matches it:
 *  the whole key, or its last part ("owner/atlas" takes "atlas"). */
function sameProject(fileProject: string | null | undefined, project: string): boolean {
  if (!fileProject) return true;
  return fileProject === project || fileProject === project.split("/").pop();
}

// ---- projects in the ledger ---------------------------------------------------------

function ProjectRow({ p, w }: { p: LedgerProject; w: Writes }) {
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(p.name);
  const btn = useRef<HTMLButtonElement>(null);
  const fieldId = `${useId().replace(/:/g, "")}-name`;
  const id = `project:${p.project}`;
  const close = () => {
    setRenaming(false);
    requestAnimationFrame(() => btn.current?.focus());
  };
  async function save(e: FormEvent) {
    e.preventDefault();
    if (await w.run(id, "khipu_ledger_project_set", { project: p.project, name: name.trim() === p.project ? "" : name, hidden: null })) close();
  }
  const started = dayWords(p.created_at);
  return (
    <div className="row-item wk-proj-row" role="listitem">
      <span className="wk-proj-name">{p.name}</span>
      <span className="wk-proj-what">
        <span>
          {plural(p.rows, "row")} · {p.open.toLocaleString()} open · {p.needs_owner.toLocaleString()}{" "}
          {p.needs_owner === 1 ? "needs you" : "need you"}
        </span>
        <span className="set-help">
          {p.hidden ? <span className="tag">Hidden from the Work tab</span> : null}
          {p.hidden && started ? " " : null}
          {started ? `Started in Khipu on ${started}.` : null}
        </span>
      </span>
      <span className="wk-proj-acts">
        <button
          ref={btn}
          type="button"
          className="sm"
          aria-label={`Rename ${p.name}`}
          aria-expanded={renaming}
          disabled={w.busy !== null}
          onClick={() => {
            setName(p.name);
            setRenaming((v) => !v);
          }}
        >
          Rename
        </button>
        <button
          type="button"
          className="sm"
          aria-label={`${p.hidden ? "Show" : "Hide"} ${p.name}`}
          disabled={w.busy !== null}
          onClick={() => void w.run(id, "khipu_ledger_project_set", { project: p.project, name: null, hidden: !p.hidden })}
        >
          {p.hidden ? "Show" : "Hide"}
        </button>
      </span>
      {renaming ? (
        <form
          className="wk-add wk-span"
          aria-label={`Rename ${p.name}`}
          onSubmit={save}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              close();
            }
          }}
        >
          <div className="toolbar">
            <label htmlFor={fieldId}>Name</label>
            <input id={fieldId} autoFocus value={name} maxLength={80} onChange={(e) => setName(e.target.value)} />
            <button type="submit" className="sm primary" disabled={w.busy === id}>
              Save
            </button>
            <button type="button" className="sm" onClick={close}>
              Cancel
            </button>
          </div>
        </form>
      ) : null}
      <span className="wk-span">
        <WriteError w={w} id={id} />
      </span>
    </div>
  );
}

function ProjectsSection({ projects, error, w }: { projects: LedgerProject[] | null; error: string | null; w: Writes }) {
  return (
    <div className="section-card">
      <div className="section-head">Projects in the ledger</div>
      <div className="section-body">
        {error ? (
          <p className="set-error" role="alert">
            Khipu couldn't read the ledger's projects: {error}
          </p>
        ) : projects === null ? (
          <p className="set-help">Reading the ledger…</p>
        ) : projects.length === 0 ? (
          <p className="set-help">No projects yet.</p>
        ) : (
          <div className="rows" role="list" aria-label="Ledger projects">
            {projects.map((p) => (
              <ProjectRow key={p.project} p={p} w={w} />
            ))}
          </div>
        )}
        <p className="set-help">A project appears when its first row is added. Hide only takes a project off the Work tab; its rows stay.</p>
      </div>
    </div>
  );
}

// ---- job runner summary files -------------------------------------------------------

const NEW_PROJECT = "\u0000new";

async function chooseFile(): Promise<string | null> {
  const picked = await openFileDialog({ multiple: false, directory: false, filters: [{ name: "Summary file", extensions: ["json"] }] });
  return typeof picked === "string" ? picked : null;
}

/** Add a summary file, or change one project's file: the project, the file
 *  (Choose file... or typed), and Khipu's first read of it before saving.
 *  Saving is allowed either way (the scope: Matt can save either way). */
function SummaryForm({
  fixedProject,
  projects,
  taken,
  initialFile,
  w,
  onSave,
  onClose,
}: {
  fixedProject?: string;
  projects: LedgerProject[];
  taken: string[];
  initialFile?: string;
  w: Writes;
  onSave: (project: string, file: string) => Promise<boolean>;
  onClose: () => void;
}) {
  const choices = projects.filter((p) => !taken.includes(p.project));
  const [project, setProject] = useState<string>(fixedProject ?? choices[0]?.project ?? NEW_PROJECT);
  const [newKey, setNewKey] = useState("");
  const [file, setFile] = useState(initialFile ?? "");
  const [read, setRead] = useState<FirstRead | null>(null);
  const [reading, setReading] = useState(false);
  const base = useId().replace(/:/g, "");
  const id = fixedProject ? `summary-change:${fixedProject}` : "summary-add";

  const check = useCallback(async (path: string) => {
    const p = path.trim();
    setRead(null);
    if (!p) return;
    setReading(true);
    const res = await callCli<FirstRead>("khipu_work_check", { path: p });
    setReading(false);
    if (res.ok) setRead(res.data);
    else {
      const r = res.refusal as FirstRead | undefined;
      setRead(r && Array.isArray(r.problems) ? r : { ok: false, problems: [res.error] });
    }
  }, []);
  useEffect(() => {
    if (initialFile) void check(initialFile);
  }, [initialFile, check]);
  useEffect(() => {
    if (!fixedProject && project === NEW_PROJECT && !newKey && read?.project) setNewKey(read.project);
  }, [read, project, newKey, fixedProject]);

  const key = project === NEW_PROJECT ? newKey.trim() : project;
  const mismatch = read?.project && key && !sameProject(read.project, key) ? `This file says it is for ${read.project}, not ${key}.` : null;
  async function save(e: FormEvent) {
    e.preventDefault();
    if (!key || !file.trim()) return;
    if (await onSave(key, file.trim())) onClose();
  }
  return (
    <form
      className="wk-add"
      aria-label={fixedProject ? `Change the summary file for ${fixedProject}` : "Add a summary file"}
      onSubmit={save}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          onClose();
        }
      }}
    >
      {fixedProject ? null : (
        <div className="toolbar">
          <label htmlFor={`${base}-proj`}>Project</label>
          <select id={`${base}-proj`} value={project} onChange={(e) => setProject(e.target.value)}>
            {choices.map((p) => (
              <option key={p.project} value={p.project}>
                {p.name}
              </option>
            ))}
            <option value={NEW_PROJECT}>A new project…</option>
          </select>
          {project === NEW_PROJECT ? (
            <input aria-label="The new project's key" placeholder="owner/repo" value={newKey} maxLength={200} onChange={(e) => setNewKey(e.target.value)} />
          ) : null}
        </div>
      )}
      <div className="toolbar">
        <label htmlFor={`${base}-file`}>Summary file</label>
        <input
          id={`${base}-file`}
          className="wk-path-input"
          value={file}
          autoFocus={!!fixedProject}
          placeholder="The file's full path"
          onChange={(e) => setFile(e.target.value)}
          onBlur={() => void check(file)}
        />
        <button
          type="button"
          className="sm"
          onClick={async () => {
            const picked = await chooseFile();
            if (picked) {
              setFile(picked);
              void check(picked);
            }
          }}
        >
          Choose file…
        </button>
      </div>
      {reading ? <p className="set-help">Reading the file…</p> : null}
      {read ? (
        read.counts ? (
          <div className="wk-found" role="status">
            {foundWords(read, new Date())}
            {read.problems?.length ? ` ${read.problems.join(" ")}` : ""}
          </div>
        ) : (
          <p className="set-help" role="status">
            {foundWords(read, new Date())} You can add it anyway; the board says what is wrong until it reads.
          </p>
        )
      ) : null}
      {mismatch ? (
        <p className="set-help" role="status">
          {mismatch} Nothing from it will be shown until the project matches.
        </p>
      ) : null}
      <div className="toolbar">
        <button type="submit" className="sm primary" disabled={!key || !file.trim() || w.busy === id}>
          {fixedProject ? "Save" : "Add"}
        </button>
        <button type="button" className="sm" onClick={onClose}>
          Cancel
        </button>
      </div>
      <WriteError w={w} id={id} />
    </form>
  );
}

function statusLine(entry: SummaryEntry, board: WorkBoard | undefined, now: Date): { text: string; tone: string } {
  if (!entry.on) return { text: "Turned off", tone: "off" };
  if (!board) return { text: "Not read yet.", tone: "" };
  const line = sourceLine(board, now);
  if (board.source.state !== "ok") return { text: line.text, tone: line.tone };
  const counts = [plural(board.jobs.length, "job"), plural(board.prs.length, "pull request"), plural(board.trains.length, "release train")].join(", ");
  return {
    text: `${line.text.replace(/\.$/, "")} · ${entry.show_now ? counts : "what jobs are doing is hidden"}`,
    tone: "ok",
  };
}

function SummarySection({
  entries,
  suggested,
  boards,
  projects,
  w,
  write,
}: {
  entries: SummaryEntry[];
  suggested: Array<{ name: string; file: string }>;
  boards: Map<string, WorkBoard>;
  projects: LedgerProject[];
  w: Writes;
  write: (id: string, edit: (now: SummaryEntry[]) => SummaryEntry[]) => Promise<boolean>;
}) {
  const [adding, setAdding] = useState<{ file?: string } | null>(null);
  const [changing, setChanging] = useState<string | null>(null);
  const addBtn = useRef<HTMLButtonElement>(null);
  const now = new Date();
  const nameOf = (e: SummaryEntry) => e.name || projects.find((p) => p.project === e.project)?.name || shortProject(e.project);
  const closeAdd = () => {
    setAdding(null);
    requestAnimationFrame(() => addBtn.current?.focus());
  };
  const save = (id: string) => (project: string, file: string) =>
    write(id, (list) => {
      const at = list.findIndex((e) => e.project === project);
      if (at >= 0) return list.map((e, i) => (i === at ? { ...e, file } : e));
      return [...list, { project, file, on: true, show_now: true }];
    });
  return (
    <div className="section-card">
      <div className="section-head">Job runner summary files</div>
      <div className="section-body">
        <p className="set-help">
          Each project's job runner can write one small summary file. Khipu only reads it: what is running, pull requests and release trains.
        </p>
        {entries.length ? (
          <div className="rows" role="list" aria-label="Summary files">
            {entries.map((e) => {
              const status = statusLine(e, boards.get(e.project), now);
              const id = `summary:${e.project}`;
              return (
                <div key={e.project} className="row-item wk-proj-row" role="listitem">
                  <span className="wk-proj-name">{nameOf(e)}</span>
                  <span className="wk-proj-what">
                    <span className="wk-path">{e.file}</span>
                    <span className="set-help">
                      {status.tone === "off" ? (
                        <span className="tag">Turned off</span>
                      ) : (
                        <>
                          {status.tone ? <span className={`hdot ${status.tone}`} aria-hidden="true" /> : null} {status.text}
                        </>
                      )}
                    </span>
                  </span>
                  <span className="wk-proj-acts">
                    {e.on ? (
                      <button type="button" className="sm" aria-label={`Change the summary file for ${nameOf(e)}`} aria-expanded={changing === e.project}
                        disabled={w.busy !== null} onClick={() => setChanging((c) => (c === e.project ? null : e.project))}>
                        Change file…
                      </button>
                    ) : null}
                    <button type="button" className="sm" aria-label={`${e.on ? "Turn off" : "Turn on"} the summary file for ${nameOf(e)}`}
                      disabled={w.busy !== null}
                      onClick={() => void write(id, (list) => list.map((x) => (x.project === e.project ? { ...x, on: !e.on } : x)))}>
                      {e.on ? "Turn off" : "Turn on"}
                    </button>
                    <button type="button" className="sm" aria-label={`Remove the summary file for ${nameOf(e)}`} disabled={w.busy !== null}
                      onClick={() => void write(id, (list) => list.filter((x) => x.project !== e.project))}>
                      Remove
                    </button>
                  </span>
                  {changing === e.project ? (
                    <span className="wk-span">
                      <SummaryForm fixedProject={e.project} projects={projects} taken={[]} initialFile={e.file} w={w}
                        onSave={save(`summary-change:${e.project}`)} onClose={() => setChanging(null)} />
                    </span>
                  ) : null}
                  <span className="wk-span">
                    <WriteError w={w} id={id} />
                  </span>
                </div>
              );
            })}
          </div>
        ) : (
          <p className="set-help">No summary files yet.</p>
        )}
        {entries.length ? (
          <div className="opt-list">
            {entries.map((e) => {
              const sid = `now-${e.project.replace(/[^A-Za-z0-9_-]/g, "-")}`;
              return (
                <div key={e.project} className="opt-row">
                  <input id={sid} className="switch" type="checkbox" role="switch" checked={e.show_now} aria-describedby="wk-now-what"
                    disabled={w.busy !== null}
                    onChange={(ev) => {
                      const show = ev.target.checked;
                      void write(`summary:${e.project}`, (list) => list.map((x) => (x.project === e.project ? { ...x, show_now: show } : x)));
                    }} />
                  <div className="opt-text">
                    <div className="opt-title">
                      <label className="opt-name" htmlFor={sid}>
                        Show what {nameOf(e)} jobs say they are doing
                      </label>
                    </div>
                  </div>
                </div>
              );
            })}
            <p id="wk-now-what" className="opt-what">
              One short line on what each job is doing, written by its runner; opening a job shows its last 3. One switch per project.
            </p>
          </div>
        ) : null}
        {adding ? (
          <SummaryForm key={adding.file ?? ""} projects={projects} taken={entries.map((e) => e.project)} initialFile={adding.file} w={w}
            onSave={save("summary-add")} onClose={closeAdd} />
        ) : (
          <div className="toolbar">
            <button ref={addBtn} type="button" className="sm" onClick={() => setAdding({})}>
              Add a summary file…
            </button>
          </div>
        )}
        {suggested.length ? (
          <p className="set-help">
            Suggested from your job runner folders:{" "}
            {suggested.map((s) => (
              <button key={s.file} type="button" className="sm link" onClick={() => setAdding({ file: s.file })}>
                Add the {s.name} runner
              </button>
            ))}
          </p>
        ) : null}
      </div>
    </div>
  );
}

// ---- limits -------------------------------------------------------------------------

type LimitField = { key: string; label: string; unit: LimitUnit; options: Array<number | "auto">; zero?: string };
type Limit = { name: string; help: string; fields: LimitField[] };

const MIN = 60;
const HOUR = 3600;
const DAY = 86400;
/** Every cap and time of Work (scope, "Caps, staleness and what Khipu
 *  shows", and the ledger's limits), in the mock's order first. */
export const WORK_LIMITS: Limit[] = [
  { name: "Largest summary file Khipu reads", help: "A bigger file is not read; the last good read shows.",
    fields: [{ key: "work_file_max_bytes", label: "Largest file", unit: "bytes", options: [64 * 1024, 256 * 1024, 1024 * 1024, 4 * 1024 * 1024, 0], zero: "No limit" }] },
  { name: "Jobs, pull requests, trains and test runs shown per project", help: "The newest are kept, and the board says how many were left out.",
    fields: [
      { key: "work_max_jobs", label: "Jobs", unit: "changes", options: [20, 50, 100, 200, 0], zero: "No limit" },
      { key: "work_max_prs", label: "Pull requests", unit: "changes", options: [50, 200, 500, 0], zero: "No limit" },
      { key: "work_max_trains", label: "Trains", unit: "changes", options: [10, 20, 50, 0], zero: "No limit" },
      { key: "work_max_runs", label: "Test runs", unit: "changes", options: [10, 20, 50, 0], zero: "No limit" },
    ] },
  { name: "A summary is out of date after", help: "Automatic: five of the runner's own intervals, at least 2 minutes.",
    fields: [{ key: "work_stale_after", label: "Out of date after", unit: "time", options: ["auto", 2 * MIN, 10 * MIN, HOUR, 0], zero: "No limit" }] },
  { name: "A job is quiet after", help: "A running job with no word for this long says so.",
    fields: [{ key: "work_job_quiet_after", label: "Quiet after", unit: "time", options: [5 * MIN, 15 * MIN, 30 * MIN, HOUR, 0], zero: "No limit" }] },
  { name: "A project has stopped reporting after", help: "Its board folds and its jobs leave Running now. Its ledger stays.",
    fields: [{ key: "work_gone_after", label: "Stopped after", unit: "time", options: [HOUR, 6 * HOUR, 24 * HOUR, 3 * DAY, 0], zero: "No limit" }] },
  { name: "Longest ledger title", help: "A longer title is refused with its length, never cut.",
    fields: [{ key: "ledger_title_chars", label: "Longest title", unit: "chars", options: [500, 2000, 5000, 0], zero: "No limit" }] },
  { name: "An open row has had no change after", help: "Shown as “No change for 9 days” on the row.",
    fields: [{ key: "ledger_stale_after", label: "No change after", unit: "time", options: [DAY, 3 * DAY, 7 * DAY, 14 * DAY, 30 * DAY, 0], zero: "No limit" }] },
  { name: "Longest summary line", help: "Longer text from a summary file is cut with an ellipsis. Ledger text is never cut.",
    fields: [{ key: "work_line_chars", label: "Longest line", unit: "chars", options: [100, 200, 400, 0], zero: "No limit" }] },
  { name: "Finished jobs show for", help: "How long an ended job stays under Just finished.",
    fields: [{ key: "work_finished_for", label: "Show for", unit: "time", options: [10 * MIN, 30 * MIN, HOUR, 0], zero: "No limit" }] },
  { name: "Merged pull requests and shipped trains show for", help: "How long they stay under Merged this week.",
    fields: [{ key: "work_merged_for", label: "Show for", unit: "time", options: [DAY, 7 * DAY, 30 * DAY, 0], zero: "No limit" }] },
  { name: "A time ahead of this Mac's clock", help: "Within this, a time is shown as now; further ahead is also counted in the health report.",
    fields: [{ key: "work_clock_slack", label: "Shown as now within", unit: "time", options: [MIN, 5 * MIN, 15 * MIN, HOUR] }] },
  { name: "The open Work tab refreshes every", help: "How often the Work tab asks for the view while it is open.",
    fields: [{ key: "work_refresh", label: "Every", unit: "time", options: [2, 5, 15, 30, MIN] }] },
  { name: "Longest ledger row id", help: "A longer id is refused with its length, never cut.",
    fields: [{ key: "ledger_id_chars", label: "Longest id", unit: "chars", options: [80, 200, 0], zero: "No limit" }] },
  { name: "Longest ledger evidence", help: "Longer evidence is refused with its length, never cut.",
    fields: [{ key: "ledger_evidence_chars", label: "Longest evidence", unit: "chars", options: [500, 1000, 4000, 0], zero: "No limit" }] },
  { name: "A removed row can be restored for", help: "After this, Khipu's nightly removes it for good.",
    fields: [{ key: "ledger_purge_after", label: "Restore for", unit: "time", options: [7 * DAY, 30 * DAY, 90 * DAY, 0], zero: "No limit" }] },
];

function settingOf(cfg: WorkConfig | null, key: string): Setting | undefined {
  return (key.startsWith("ledger_") ? cfg?.ledger : cfg?.work)?.[key];
}

function valueWords(v: number | "auto", f: LimitField): string {
  if (v === "auto") return "Automatic";
  if (f.unit === "changes") return v === 0 && f.zero ? f.zero : v.toLocaleString();
  return limitWords(v, f.unit, f.zero);
}

function asValue(v: unknown): number | "auto" | null {
  return v === "auto" ? "auto" : typeof v === "number" ? v : null;
}

function LimitRow({ limit, cfg, w }: { limit: Limit; cfg: WorkConfig | null; w: Writes }) {
  const [open, setOpen] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const btn = useRef<HTMLButtonElement>(null);
  const formId = `wk-lim-${useId().replace(/:/g, "")}`;
  const values = limit.fields.map((f) => asValue(settingOf(cfg, f.key)?.value));
  const id = `limit:${limit.fields[0].key}`;
  const close = () => {
    setOpen(false);
    requestAnimationFrame(() => btn.current?.focus());
  };
  async function save(e: FormEvent) {
    e.preventDefault();
    for (const [i, f] of limit.fields.entries()) {
      const next = drafts[f.key];
      if (next == null || next === String(values[i])) continue;
      if (!(await w.run(id, "khipu_config_set", { key: f.key, value: next }))) return;
    }
    close();
  }
  return (
    <div className="row-item wk-limit" role="listitem">
      <span className="wk-limit-text">
        <span className="wk-limit-name">{limit.name}</span>
        <span className="set-help">{limit.help}</span>
      </span>
      <span className="wk-val">{limit.fields.map((f, i) => (values[i] == null ? "…" : valueWords(values[i]!, f))).join(" · ")}</span>
      <button
        ref={btn}
        type="button"
        className="sm"
        aria-label={`Change: ${limit.name}`}
        aria-expanded={open}
        aria-controls={open ? formId : undefined}
        disabled={values.some((v) => v == null)}
        onClick={() => {
          setDrafts(Object.fromEntries(limit.fields.map((f, i) => [f.key, String(values[i] ?? "")])));
          setOpen((v) => !v);
        }}
      >
        Change…
      </button>
      {open ? (
        <form
          className="wk-add wk-span"
          id={formId}
          aria-label={`Change: ${limit.name}`}
          onSubmit={save}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              close();
            }
          }}
        >
          <div className="toolbar">
            {limit.fields.map((f, i) => {
              const def = asValue(settingOf(cfg, f.key)?.default);
              const opts = [...new Set([...f.options, ...(values[i] != null ? [values[i]!] : [])])];
              const selId = `${formId}-${f.key}`;
              return (
                <span key={f.key} className="toolbar">
                  <label htmlFor={selId}>{f.label}</label>
                  <select id={selId} autoFocus={i === 0} value={drafts[f.key] ?? ""} onChange={(e) => setDrafts((d) => ({ ...d, [f.key]: e.target.value }))}>
                    {opts.map((o) => (
                      <option key={String(o)} value={String(o)}>
                        {valueWords(o, f)}
                        {o === def ? " (the default)" : ""}
                      </option>
                    ))}
                  </select>
                </span>
              );
            })}
            <button type="submit" className="sm primary" disabled={w.busy === id}>
              Save
            </button>
            <button type="button" className="sm" onClick={close}>
              Cancel
            </button>
          </div>
          <WriteError w={w} id={id} />
        </form>
      ) : null}
    </div>
  );
}

// ---- hidden items -------------------------------------------------------------------

function HiddenSection({ items, error, w }: { items: HiddenItem[] | null; error: string | null; w: Writes }) {
  return (
    <div className="section-card">
      <div className="section-head">Hidden from Waiting on you</div>
      <div className="section-body">
        {error ? (
          <p className="set-error" role="alert">
            Khipu couldn't read the hidden items: {error}
          </p>
        ) : items && items.length ? (
          <div className="rows" role="list" aria-label="Hidden items">
            {items.map((h) => (
              <div key={h.id} className="row-item wk-hidden" role="listitem">
                <span>
                  <span className="row-main">{h.text ?? "An Owed item"}</span>{" "}
                  <span className="meta">Owed{h.project ? ` · ${shortProject(h.project)}` : ""}</span>
                </span>
                <button type="button" className="sm" aria-label={`Unhide ${h.text ?? "this item"}`} disabled={w.busy !== null}
                  onClick={() => void w.run(`hidden:${h.id}`, "khipu_work_hide", { id: h.id, hide: false })}>
                  Unhide
                </button>
                <span className="wk-span">
                  <WriteError w={w} id={`hidden:${h.id}`} />
                </span>
              </div>
            ))}
          </div>
        ) : items ? (
          <p className="set-help">Nothing is hidden.</p>
        ) : null}
        <p className="set-help">Only Owed items can be hidden; they come back by themselves when they change. Ledger rows are closed, not hidden.</p>
      </div>
    </div>
  );
}

// ---- the page -----------------------------------------------------------------------

export function WorkSettings({ active, work, importCard }: { active: boolean; work: WorkState; importCard?: ReactNode }) {
  const [cfg, setCfg] = useState<WorkConfig | null>(null);
  const [cfgError, setCfgError] = useState<string | null>(null);
  const [projects, setProjects] = useState<LedgerProject[] | null>(null);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [hidden, setHidden] = useState<HiddenItem[] | null>(null);
  const [hiddenError, setHiddenError] = useState<string | null>(null);
  const { load: loadWork } = work;

  const reread = useCallback(async () => {
    const [c, p, h] = await Promise.all([
      callCli<WorkConfig>("khipu_config_show"),
      callCli<{ projects?: LedgerProject[] }>("khipu_ledger_projects"),
      callCli<{ hidden?: HiddenItem[] }>("khipu_work_hidden"),
    ]);
    if (c.ok) {
      setCfg(c.data);
      setCfgError(null);
    } else setCfgError(c.error);
    if (p.ok && Array.isArray(p.data.projects)) {
      setProjects(p.data.projects);
      setProjectsError(null);
    } else if (!p.ok) setProjectsError(p.error);
    if (h.ok && Array.isArray(h.data.hidden)) {
      setHidden(h.data.hidden);
      setHiddenError(null);
    } else if (!h.ok) setHiddenError(h.error);
    await loadWork(true);
  }, [loadWork]);
  useEffect(() => {
    if (active) void reread();
  }, [active, reread]);
  const w = useWrites(reread);

  const raw = cfg?.work?.work_projects?.value;
  const entries: SummaryEntry[] = Array.isArray(raw) ? (raw as SummaryEntry[]) : [];
  const boards = new Map((work.data?.projects ?? []).filter((b) => b.project).map((b) => [b.project as string, b]));
  // Each change is made to the list as Khipu has it just before the write,
  // never to the one this page last read, so two quick changes both stay.
  const writeEntries = (id: string, edit: (now: SummaryEntry[]) => SummaryEntry[]) =>
    w.run(id, "khipu_config_set", async () => {
      const cur = await callCli<WorkConfig>("khipu_config_show");
      if (!cur.ok) return `Khipu couldn't read the summary files before changing them: ${cur.error}`;
      const list = cur.data.work?.work_projects?.value;
      return { key: "work_projects", value: JSON.stringify(edit(Array.isArray(list) ? (list as SummaryEntry[]) : [])) };
    });

  return (
    <>
      <p className="set-help set-intro">
        Work keeps the ledger of what is done and what is not in each project, and shows what your job runners report. Conversations write the
        ledger through Khipu and you can change any row. Khipu never starts, stops or moves work.
      </p>
      {cfgError ? (
        <p className="set-error" role="alert">
          Khipu couldn't read its settings: {cfgError}
        </p>
      ) : null}
      <ProjectsSection projects={projects} error={projectsError} w={w} />
      {/* Import a tracker…, Last import and Old tracker files: the mock's second card. */}
      {importCard}
      <SummarySection entries={entries} suggested={cfg?.work_suggested ?? []} boards={boards} projects={projects ?? []} w={w} write={writeEntries} />
      <div className="section-card">
        <div className="section-head">Limits</div>
        <div className="section-body">
          <div className="rows" role="list" aria-label="Limits">
            {WORK_LIMITS.map((l) => (
              <LimitRow key={l.fields[0].key} limit={l} cfg={cfg} w={w} />
            ))}
          </div>
        </div>
      </div>
      <HiddenSection items={hidden} error={hiddenError} w={w} />
    </>
  );
}
