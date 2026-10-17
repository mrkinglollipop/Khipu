/* Owed, many at once, and the one-time clean-up
   (docs/plans/2026-10-16-owed-and-ledger-ssot.md, design (c) and (d), slice 5).
   Every write goes through a fixed-argv Tauri command in src-tauri/src/lib.rs
   (`khipu_owed_change`, `khipu_owed_tidy_plan|apply|undo`); the webview names
   ids, a reason and a project, never an argv or a path. Snoozing several at
   once is not offered: the CLI's snooze takes one item. "Not settled" is not
   offered either: no command records it yet. */

import { useEffect, useId, useState } from "react";
import { Loader2 } from "lucide-react";
import { Callout, Dialog, Disclosure } from "./ui";
import { callCli } from "./settingsCli";

export type OwedOwner = "user" | "assistant" | "agent";

const n = (v: number | undefined | null) => (v ?? 0).toLocaleString();
const items = (v: number) => `${n(v)} ${v === 1 ? "item" : "items"}`;

type ChangeAnswer = { ok?: boolean; changed?: number; waiting_to_sync?: unknown; results?: Array<{ id: number; ok: boolean; error?: string }> };

/** One change to several items: one call, one transaction, one result per id.
 *  Resolves to null when every item changed, or the words to show. */
export async function owedChange(
  action: "close" | "reassign",
  ids: number[],
  reason: string,
  to: OwedOwner | null = null,
  label: string | null = null,
): Promise<{ error: string | null; queued: boolean }> {
  const res = await callCli<ChangeAnswer>("khipu_owed_change", {
    action,
    ids: ids.map(String),
    reason,
    to,
    label,
  });
  if (!res.ok) return { error: res.error, queued: false };
  const failed = (res.data.results ?? []).filter((r) => !r.ok);
  const queued = Boolean(res.data.waiting_to_sync);
  if (failed.length === 0) return { error: null, queued };
  const why = failed[0]?.error ? `: ${failed[0].error}` : "";
  return { error: `${items(failed.length)} not changed${why}`, queued };
}

// ---------------------------------------------------------------------------
// The bar over a selection.
// ---------------------------------------------------------------------------

export function OwedSelectionBar({
  count,
  onClose,
  onMove,
  onClear,
}: {
  count: number;
  onClose: () => void;
  onMove: () => void;
  onClear: () => void;
}) {
  return (
    <div className="card owed-selbar" role="toolbar" aria-label="Selected items">
      <div className="row">
        <strong>{n(count)} selected</strong>
        <span className="spacer" />
        <button type="button" className="sm" onClick={onClose}>
          Close…
        </button>
        <button type="button" className="sm" onClick={onMove}>
          Move to…
        </button>
        <button type="button" className="sm link" onClick={onClear}>
          Clear
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Close… and Move to…, each with a reason.
// ---------------------------------------------------------------------------

const MOVE_CHOICES: ReadonlyArray<readonly [OwedOwner, string]> = [
  ["agent", "An agent or a job"],
  ["assistant", "The assistant"],
  ["user", "You (Needs you)"],
];

export function OwedChangeDialog({
  mode,
  count,
  onCancel,
  onConfirm,
}: {
  mode: "close" | "move" | null;
  count: number;
  onCancel: () => void;
  /** Resolves to null when done (the dialog closes), or the error to show. */
  onConfirm: (reason: string, to: OwedOwner | null, label: string | null) => Promise<string | null>;
}) {
  const id = useId();
  const [reason, setReason] = useState("");
  const [to, setTo] = useState<OwedOwner>("agent");
  const [label, setLabel] = useState("orchestrator");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (mode) {
      setReason("");
      setTo("agent");
      setLabel("orchestrator");
      setError(null);
    }
  }, [mode]);

  const what = items(count);
  const title = mode === "move" ? `Move ${what}` : `Close ${what}`;
  const ready = reason.trim().length > 0 && !busy;

  async function confirm() {
    if (!ready || !mode) return;
    setBusy(true);
    const why = await onConfirm(reason.trim(), mode === "move" ? to : null, mode === "move" && to === "agent" ? label.trim() || null : null);
    setBusy(false);
    setError(why);
  }

  return (
    <Dialog open={mode != null} className="kit-dialog" ariaLabelledBy={`${id}-title`} onCancel={onCancel}>
      <div className="kit-dialog-body">
        <h2 id={`${id}-title`}>{title}</h2>
        {mode === "move" ? (
          <>
            <div className="set-field">
              <label className="set-label" htmlFor={`${id}-to`}>
                Move to
              </label>
              <select id={`${id}-to`} value={to} onChange={(e) => setTo(e.target.value as OwedOwner)}>
                {MOVE_CHOICES.map(([value, words]) => (
                  <option key={value} value={value}>
                    {words}
                  </option>
                ))}
              </select>
            </div>
            {to === "agent" ? (
              <div className="set-field">
                <label className="set-label" htmlFor={`${id}-label`}>
                  Who
                </label>
                <input
                  id={`${id}-label`}
                  value={label}
                  maxLength={60}
                  placeholder="orchestrator, reviewer, builder"
                  onChange={(e) => setLabel(e.target.value)}
                />
              </div>
            ) : null}
          </>
        ) : (
          <p>They stay searchable and are listed under Closed with your reason. Reopen any of them there.</p>
        )}
        <div className="set-field">
          <label className="set-label" htmlFor={`${id}-why`}>
            Why
          </label>
          <input
            id={`${id}-why`}
            value={reason}
            maxLength={500}
            placeholder={mode === "move" ? "A job's own items" : "Done in the last release"}
            onChange={(e) => setReason(e.target.value)}
          />
        </div>
        {error ? (
          <p className="set-error" role="alert">
            {error}
          </p>
        ) : null}
        <div className="kit-dialog-actions">
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="primary" disabled={!ready} onClick={() => void confirm()}>
            {busy ? <Loader2 size={14} className="spin" aria-hidden /> : null}
            {mode === "move" ? `Move ${n(count)}` : `Close ${n(count)}`}
          </button>
        </div>
      </div>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Tidy this list…: plan, preview, Apply or Not now, then Undo.
// ---------------------------------------------------------------------------

type Sample = { id: number; kind?: string; text?: string; reason?: string };

export type TidyPlan = {
  format: string;
  version: number;
  project: string;
  totals: {
    open: number;
    stay_user: number;
    keep_other: number;
    move: { total: number; agent: number; assistant: number; user: number };
    close: { total: number; pr: number; decision: number; duplicate: number };
    /** A part of stay_user: changes only suggested for your own items, never applied. */
    proposed?: { total: number; move: number; close: number };
  };
  model?: { used?: boolean; calls?: number; cap?: number; candidates?: number; unjudged?: number };
  samples?: Partial<Record<string, Sample[]>>;
  notes?: string[];
};

type ApplyAnswer = { ok?: boolean; batch?: string; changed?: number; counts?: { close?: number; reassign?: number }; skipped?: unknown[] };
type UndoAnswer = { ok?: boolean; restored?: number; left_alone?: unknown[] };

const SAMPLE_GROUPS: ReadonlyArray<readonly [string, string]> = [
  ["stay_user", "Examples that stay yours"],
  ["proposed", "Suggested changes to your own items"],
  ["move", "Examples that move"],
  ["close_pr", "Examples closed because their pull requests merged"],
  ["close_decision", "Examples a decision settled"],
  ["close_duplicate", "Examples closed as duplicates"],
  ["keep_other", "Examples kept for others"],
];

const BATCH_KEY = "khipu.owedTidyBatch";

function storedBatch(): { project: string; batch: string } | null {
  try {
    const raw = localStorage.getItem(BATCH_KEY);
    const v = raw ? (JSON.parse(raw) as { project?: unknown; batch?: unknown }) : null;
    return v && typeof v.project === "string" && typeof v.batch === "string" ? { project: v.project, batch: v.batch } : null;
  } catch {
    return null;
  }
}

function storeBatch(v: { project: string; batch: string } | null) {
  try {
    if (v) localStorage.setItem(BATCH_KEY, JSON.stringify(v));
    else localStorage.removeItem(BATCH_KEY);
  } catch {
    /* a remembered Undo is a convenience; the batch stays on the hub */
  }
}

function isPlan(v: unknown): v is TidyPlan {
  const p = v as TidyPlan | null;
  return Boolean(p && p.format === "khipu-owed-tidy-plan" && p.version >= 1 && p.totals);
}

export function OwedTidyDialog({
  open,
  projects,
  defaultProject,
  onCancel,
  onChanged,
}: {
  open: boolean;
  /** Projects the loaded Owed list names. */
  projects: string[];
  defaultProject: string | null;
  onCancel: () => void;
  /** After an apply or an undo: read Owed and Work again. */
  onChanged: () => Promise<unknown>;
}) {
  const id = useId();
  const [project, setProject] = useState("");
  const [plan, setPlan] = useState<TidyPlan | null>(null);
  const [busy, setBusy] = useState<"plan" | "apply" | "undo" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [applied, setApplied] = useState<{ project: string; batch: string; words: string; skipped: string | null } | null>(null);
  const [undone, setUndone] = useState<string | null>(null);
  const [last, setLast] = useState<{ project: string; batch: string } | null>(null);

  useEffect(() => {
    if (!open) return;
    setProject(defaultProject ?? projects[0] ?? "");
    setPlan(null);
    setError(null);
    setApplied(null);
    setUndone(null);
    setLast(storedBatch());
    // Opened afresh each time; a list that grows while it is open keeps the
    // project chosen.
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  async function makePlan() {
    if (!project) return;
    setBusy("plan");
    setError(null);
    setPlan(null);
    setApplied(null);
    setUndone(null);
    const res = await callCli<TidyPlan>("khipu_owed_tidy_plan", { project });
    setBusy(null);
    if (!res.ok) return setError(res.error);
    if (!isPlan(res.data)) return setError("The plan came back in a form this app does not read. Update Khipu.");
    setPlan(res.data);
  }

  async function apply() {
    if (!plan) return;
    setBusy("apply");
    setError(null);
    const res = await callCli<ApplyAnswer>("khipu_owed_tidy_apply", { project: plan.project });
    setBusy(null);
    if (!res.ok || !res.data.batch) return setError(res.ok ? "The clean-up was not applied." : res.error);
    const skipped = Array.isArray(res.data.skipped) ? res.data.skipped.length : 0;
    const done = { project: plan.project, batch: res.data.batch };
    storeBatch(done);
    setLast(done);
    setApplied({
      ...done,
      words: `Closed ${n(res.data.counts?.close)} and moved ${n(res.data.counts?.reassign)}.`,
      skipped: skipped ? `${items(skipped)} changed after the plan and ${skipped === 1 ? "was" : "were"} left alone.` : null,
    });
    await onChanged();
  }

  async function undo(target: { project: string; batch: string }) {
    setBusy("undo");
    setError(null);
    const res = await callCli<UndoAnswer>("khipu_owed_tidy_undo", target);
    setBusy(null);
    if (!res.ok) return setError(res.error);
    const left = Array.isArray(res.data.left_alone) ? res.data.left_alone.length : 0;
    storeBatch(null);
    setLast(null);
    setUndone(
      `Put back ${items(res.data.restored ?? 0)}.` +
        (left ? ` ${items(left)} changed since the clean-up and ${left === 1 ? "was" : "were"} left alone.` : ""),
    );
    await onChanged();
  }

  const t = plan?.totals;
  const m = plan?.model;
  const undoTarget = applied ?? (last && last.project === project ? last : null);

  return (
    <Dialog open={open} className="kit-dialog owed-tidy" ariaLabelledBy={`${id}-title`} onCancel={onCancel}>
      <div className="kit-dialog-body">
        <h2 id={`${id}-title`}>Tidy this list</h2>
        <p>
          Khipu sorts a project's open items by its rules: items a job left for its orchestrator move there, items
          whose pull requests merged or a decision settled close, and duplicates close. Nothing changes until you
          apply the plan, nothing is deleted, and the whole clean-up can be undone.
        </p>
        <div className="set-field">
          <label className="set-label" htmlFor={`${id}-project`}>
            Project
          </label>
          <div className="toolbar">
            <select
              id={`${id}-project`}
              value={project}
              disabled={busy != null}
              onChange={(e) => {
                setProject(e.target.value);
                setPlan(null);
                setApplied(null);
                setUndone(null);
              }}
            >
              {projects.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
            <button type="button" disabled={!project || busy != null} onClick={() => void makePlan()}>
              {busy === "plan" ? <Loader2 size={14} className="spin" aria-hidden /> : null}
              Make a plan
            </button>
          </div>
          {busy === "plan" ? <p className="set-help">Planning. This can take a minute on a long list.</p> : null}
        </div>

        {t ? (
          <div className="owed-tidy-plan">
            <p>
              <strong>{n(t.open)} open items</strong>
            </p>
            <div className="card">
              <div className="row">
                <span className="grow">Stay yours</span>
                <span className="mono">{n(t.stay_user)}</span>
              </div>
              <div className="row">
                <span className="grow">
                  Move to another owner
                  <span className="meta">
                    {" "}
                    {n(t.move.agent)} to agents and jobs, {n(t.move.assistant)} to the assistant
                    {t.move.user ? `, ${n(t.move.user)} to you` : ""}
                  </span>
                </span>
                <span className="mono">{n(t.move.total)}</span>
              </div>
              <div className="row">
                <span className="grow">
                  Close
                  <span className="meta">
                    {" "}
                    {n(t.close.pr)} with every pull request merged, {n(t.close.decision)} settled by a decision,{" "}
                    {n(t.close.duplicate)} duplicates
                  </span>
                </span>
                <span className="mono">{n(t.close.total)}</span>
              </div>
              {t.proposed?.total ? (
                <div className="row">
                  <span className="grow">
                    Of yours, with a suggested change
                    <span className="meta">
                      {" "}
                      {n(t.proposed.move)} to move, {n(t.proposed.close)} to close. Applying changes none of them; you
                      decide each one in Owed.
                    </span>
                  </span>
                  <span className="mono">{n(t.proposed.total)}</span>
                </div>
              ) : null}
              <div className="row">
                <span className="grow">Kept for others as they are</span>
                <span className="mono">{n(t.keep_other)}</span>
              </div>
            </div>
            {m?.unjudged ? (
              <p className="set-help">
                {items(m.unjudged)} were left as they are: the plan reached its {n(m.cap)} model calls. Raise "Most
                model calls for one clean-up plan" in Settings, Capture & models, to sort them too.
              </p>
            ) : null}
            {(plan?.notes ?? []).map((note) => (
              <p key={note} className="set-help">
                {note}
              </p>
            ))}
            {SAMPLE_GROUPS.map(([key, words]) => {
              const rows = plan?.samples?.[key] ?? [];
              if (rows.length === 0) return null;
              return (
                <Disclosure key={key} label={`${words} (${rows.length})`}>
                  <div className="card">
                    {rows.map((s) => (
                      <div key={s.id} className="row">
                        <span className="grow">{s.text}</span>
                        {s.reason ? <span className="meta">{s.reason}</span> : null}
                      </div>
                    ))}
                  </div>
                </Disclosure>
              );
            })}
          </div>
        ) : null}

        {applied ? (
          <Callout tone="ok" title={`Clean-up applied to ${applied.project}`}>
            {applied.words}
            {applied.skipped ? ` ${applied.skipped}` : ""}
          </Callout>
        ) : null}
        {undone ? <Callout tone="ok" title="Clean-up undone">{undone}</Callout> : null}
        {error ? (
          <p className="set-error" role="alert">
            {error}
          </p>
        ) : null}

        <div className="kit-dialog-actions">
          {undoTarget && !undone ? (
            <button type="button" disabled={busy != null} onClick={() => void undo({ project: undoTarget.project, batch: undoTarget.batch })}>
              {busy === "undo" ? <Loader2 size={14} className="spin" aria-hidden /> : null}
              Undo this clean-up
            </button>
          ) : null}
          <span className="spacer" />
          {plan && !applied ? (
            <>
              <button type="button" disabled={busy != null} onClick={onCancel}>
                Not now
              </button>
              <button type="button" className="primary" disabled={busy != null} onClick={() => void apply()}>
                {busy === "apply" ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                Apply
              </button>
            </>
          ) : (
            <button type="button" disabled={busy === "apply" || busy === "undo"} onClick={onCancel}>
              {applied || undone ? "Done" : "Not now"}
            </button>
          )}
        </div>
      </div>
    </Dialog>
  );
}
