// The one destructive action in Memory. The owner types "forget" before the
// button works; Cancel, Escape and the backdrop all keep the conversation.
// The call itself (`forgetConversation`) is the app's existing one.
import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Loader2, Trash2 } from "lucide-react";
import { Dialog } from "../ui";
import type { MemoryRow } from "./model";

export const FORGET_WORD = "forget";

export function ForgetDialog({
  row,
  busy,
  error,
  onCancel,
  onConfirm,
}: {
  row: MemoryRow | null;
  busy: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const [typed, setTyped] = useState("");
  const field = useRef<HTMLInputElement>(null);
  useEffect(() => {
    setTyped("");
  }, [row?.id]);
  const ready = typed.trim().toLowerCase() === FORGET_WORD && !busy;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (ready) onConfirm();
  };

  return (
    <Dialog open={row != null} className="kit-dialog" ariaLabelledBy="forget-title" onCancel={onCancel} initialFocusRef={field}>
      <form className="kit-dialog-body" onSubmit={submit}>
        <h2 id="forget-title">Forget “{row?.title ?? ""}”?</h2>
        <p>
          It stops showing up in search and in what your agents remember. Khipu keeps the hidden text, so this is not a
          shredder.
        </p>
        <p>
          <b>This can’t be undone from the app.</b> Type <b>{FORGET_WORD}</b> to confirm.
        </p>
        <input
          ref={field}
          className="mem-forget-field"
          aria-label={`Type ${FORGET_WORD} to confirm`}
          value={typed}
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
          disabled={busy}
          onChange={(e) => setTyped(e.target.value)}
        />
        {error ? <p role="alert">{error}</p> : null}
        <div className="kit-dialog-actions">
          <button type="button" onClick={onCancel} disabled={busy}>
            Keep it
          </button>
          <button type="submit" className="danger" disabled={!ready} aria-busy={busy || undefined}>
            {busy ? <Loader2 size={14} className="spin" aria-hidden /> : <Trash2 size={14} strokeWidth={1.75} aria-hidden />}
            Forget it
          </button>
        </div>
      </form>
    </Dialog>
  );
}
