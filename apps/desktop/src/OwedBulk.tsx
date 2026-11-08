/* Explicit Owed bulk changes
   (docs/plans/2026-10-16-owed-and-ledger-ssot.md, design (c) and (d), slice 5).
   Every write goes through a fixed-argv Tauri command in src-tauri/src/lib.rs
   (`khipu_owed_change`); the webview names
   ids, a reason and a project, never an argv or a path. Snoozing several at
   once is not offered: the CLI's snooze takes one item. "Not settled" is not
   offered either: no command records it yet. */

import { useId, useState } from "react";
import { Loader2 } from "lucide-react";
import { Callout, Dialog } from "./ui";
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

