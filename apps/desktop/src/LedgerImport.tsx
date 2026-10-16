/* Settings -> Work -> Ledger -> Import a tracker... (docs/plans/2026-10-16-owed-and-ledger-ssot.md,
   slice 6 and Design (e); docs/plans/2026-10-15-work-view.md "Importing an existing tracker"; mock
   docs/mockups/2026-10-15-work-view/settings-work.html). Choose file opens in Khipu's ledger inbox,
   where a project's orchestrator copies its old tracker; Check this file is the dry run and Import
   the real one, both through lib.rs's fixed `ledger import --json --from-app` argv. Khipu reads the
   file and never changes, moves or deletes it. */

import { useEffect, useId, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { open as openFileDialog } from "@tauri-apps/plugin-dialog";
import { callCli } from "./settingsCli";
import { plural } from "./work";
import "./Work.css";

type Counts = {
  rows: number;
  history: number;
  with_parent: number;
  by_state: Record<string, number>;
  by_kind: Record<string, number>;
  missing_parent: string[];
};

/** `ledger_import.run_import`'s report, as much of it as the app shows. */
export type ImportReport = {
  ok?: boolean;
  result?: string;
  reason?: string;
  words?: string;
  error?: string;
  counts_in_file?: Counts;
  masked?: string[];
  other_keys?: string[];
  existing_ids?: string[];
  new_words?: { state?: Array<[string, string]>; kind?: string[] };
  in_inbox?: boolean;
  hub_checked?: boolean;
};

const COPY_NOTE =
  "This file is a copy in Khipu's ledger inbox. A copy never changes, so Bring in the changes is not used for it: the switch to Khipu is a hard cut.";

/** Whether `path` is a file directly in the inbox, as `ledger_import.in_inbox` decides. */
function inInbox(path: string, inbox: string | null): boolean {
  if (!inbox || !path) return false;
  const dir = inbox.replace(/\/+$/, "");
  return path.startsWith(`${dir}/`) && !path.slice(dir.length + 1).includes("/");
}

function listed(ids: string[]): string {
  const shown = ids.slice(0, 20).join(", ");
  return ids.length > 20 ? `${shown} and ${ids.length - 20} more` : shown;
}

function CountTable({ caption, counts }: { caption: string; counts: Record<string, number> }) {
  const rows = Object.entries(counts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  if (rows.length === 0) return null;
  return (
    <table className="wk-count">
      <caption className="sr-only">{caption}</caption>
      <thead>
        <tr>
          <th scope="col">{caption}</th>
          <th scope="col">Rows</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([word, n]) => (
          <tr key={word}>
            <td>{word}</td>
            <td>{n.toLocaleString()}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** What a dry run found, in the mock's words. */
function Found({ report }: { report: ImportReport }) {
  const c = report.counts_in_file;
  if (!c) return null;
  const masked = report.masked ?? [];
  const other = report.other_keys ?? [];
  const added = [
    ...(report.new_words?.state ?? []).map(([w, m]) => `${w} (counts as ${m})`),
    ...(report.new_words?.kind ?? []),
  ];
  return (
    <>
      <div className="wk-found">
        Found {plural(c.rows, "row")} and {plural(c.history, "history entry", "history entries")}. Nothing is
        written until you choose Import.
      </div>
      {report.hub_checked === false && report.words ? <p className="set-help">{report.words}</p> : null}
      <CountTable caption="Rows in each state" counts={c.by_state} />
      <CountTable caption="Rows of each kind" counts={c.by_kind} />
      <p className="set-help">{c.with_parent === 1 ? "1 row has a parent." : `${plural(c.with_parent, "row")} have a parent.`}</p>
      {c.missing_parent.length > 0 ? (
        <p className="set-help">
          {c.missing_parent.length === 1 ? "1 row names" : `${plural(c.missing_parent.length, "row")} name`} a parent that
          isn't in the file; they are kept as they are: {listed(c.missing_parent)}.
        </p>
      ) : null}
      {masked.length > 0 ? (
        <p className="set-help">
          {masked.length === 1 ? "1 field looks" : `${plural(masked.length, "field")} look`} like a key or password; that
          part will be hidden in Khipu and stays only in the old file: {listed(masked)}.
        </p>
      ) : null}
      {other.length > 0 ? <p className="set-help">Not rows, so not imported: {other.join(", ")}.</p> : null}
      {added.length > 0 ? <p className="set-help">Words new to this project: {added.join(", ")}.</p> : null}
    </>
  );
}

export function LedgerImport({ active, projects }: { active: boolean; projects: string[] }) {
  const [inbox, setInbox] = useState<string | null>(null);
  const [inboxError, setInboxError] = useState<string | null>(null);
  const [opened, setOpened] = useState(false);
  const [path, setPath] = useState("");
  const [project, setProject] = useState("");
  const [checked, setChecked] = useState<ImportReport | null>(null);
  const [outcome, setOutcome] = useState<{ ok: boolean; words: string } | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState<"check" | "import" | null>(null);
  const ids = useId();

  useEffect(() => {
    if (!active || inbox) return;
    invoke<string>("khipu_ledger_inbox")
      .then((p) => {
        setInbox(p);
        setInboxError(null);
      })
      .catch((e) => setInboxError(typeof e === "string" ? e : String(e)));
  }, [active, inbox]);

  // A new file or project needs a new check before Import.
  function reset() {
    setChecked(null);
    setOutcome(null);
    setProblem(null);
  }

  async function choose() {
    try {
      const picked = await openFileDialog({
        directory: false,
        multiple: false,
        defaultPath: inbox ?? undefined,
        filters: [{ name: "Tracker file", extensions: ["json"] }],
      });
      if (typeof picked === "string" && picked) {
        setPath(picked);
        reset();
      }
    } catch (e) {
      setProblem(typeof e === "string" ? e : String(e));
    }
  }

  async function run(dryRun: boolean) {
    setBusy(dryRun ? "check" : "import");
    setProblem(null);
    setOutcome(null);
    const out = await callCli<ImportReport>("khipu_ledger_import", { project: project.trim(), path, dryRun });
    setBusy(null);
    const rep = out.ok ? out.data : (out.refusal as ImportReport | undefined);
    if (dryRun) {
      if (out.ok && rep?.result === "dry run") {
        setChecked(rep);
      } else {
        setChecked(null);
        setProblem(rep?.words || (out.ok ? "Khipu gave an answer the app could not read." : out.error));
      }
      return;
    }
    if (out.ok && (rep?.result === "done" || rep?.result === "nothing new")) {
      setOutcome({ ok: true, words: rep.words ?? "Imported." });
    } else {
      setOutcome({ ok: false, words: rep?.words || (out.ok ? "Khipu gave an answer the app could not read." : out.error) });
    }
  }

  const ready = path !== "" && project.trim() !== "" && busy === null;
  return (
    <div className="section-card">
      <div className="section-head">Ledger</div>
      <div className="section-body">
        <p className="set-help">
          Bring in a tracker you already keep. Khipu reads the file, never changes it, and keeps every row and every
          history entry.
        </p>
        <div className="toolbar">
          <button type="button" className="sm" aria-expanded={opened} onClick={() => setOpened((v) => !v)}>
            Import a tracker…
          </button>
        </div>
        {opened ? (
          <div className="wk-import" role="group" aria-label="Import a tracker">
            <div className="toolbar">
              <label htmlFor={`${ids}-file`}>File</label>
              <input id={`${ids}-file`} className="mono" value={path} readOnly placeholder={inbox ?? ""} />
              <button type="button" className="sm" onClick={() => void choose()}>
                Choose file…
              </button>
            </div>
            {inboxError ? <p className="wk-problem">{inboxError}</p> : null}
            {inInbox(path, inbox) ? <p className="set-help">{COPY_NOTE}</p> : null}
            <div className="toolbar">
              <label htmlFor={`${ids}-project`}>Project</label>
              <input
                id={`${ids}-project`}
                list={`${ids}-projects`}
                value={project}
                placeholder="Pick a project or type a new one"
                onChange={(e) => {
                  setProject(e.target.value);
                  reset();
                }}
              />
              <datalist id={`${ids}-projects`}>
                {projects.map((p) => (
                  <option key={p} value={p} />
                ))}
              </datalist>
            </div>
            <div className="toolbar">
              <button type="button" className="sm" disabled={!ready} onClick={() => void run(true)}>
                {busy === "check" ? "Checking…" : "Check this file"}
              </button>
              <button
                type="button"
                className="sm primary"
                disabled={!ready || checked === null || outcome?.ok === true}
                onClick={() => void run(false)}
              >
                {busy === "import" ? "Importing…" : "Import"}
              </button>
            </div>
            {problem ? <p className="wk-problem">{problem}</p> : null}
            {checked ? <Found report={checked} /> : null}
            {outcome ? (
              <p role="status" className={outcome.ok ? "wk-found" : "wk-problem"}>
                {outcome.words}
              </p>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
