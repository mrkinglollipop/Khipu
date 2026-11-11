// The receipt that takes an answered card's place (slice A3). Undo runs the
// existing undo (`khipu_owner_direction_undo`) on the receipt's `action_id`,
// and only when Khipu said the answer can be undone. One press sends one
// undo; a failed undo says so and is never sent again on its own.
import { useRef, useState } from "react";
import { Check, Undo2 } from "lucide-react";
import { callCli } from "../settingsCli";
import type { Receipt as ReceiptData } from "./needs";

/** Where the answer was saved, in words; empty when Khipu named no Mac. */
export function whereWords(r: ReceiptData): string {
  const parts: string[] = [];
  if (r.saved_on.length) parts.push(`Saved on ${r.saved_on.join(" and ")}.`);
  if (r.not_reached.length) parts.push(`Not on ${r.not_reached.join(" or ")} yet.`);
  return parts.join(" ");
}

export function undoRefusal(code: unknown, error: string): string {
  if (code === "newer_change") {
    return "A newer change touched the same thing, so this wasn't undone. All changes can undo it over the newer one.";
  }
  if (code === "undone") return "This was already undone.";
  return `This wasn't undone. ${error}`.trim();
}

export function Receipt({ data, onUndone }: { data: ReceiptData; onUndone: () => void }) {
  const [state, setState] = useState<"idle" | "busy" | "done">("idle");
  const [error, setError] = useState<string | null>(null);
  const sending = useRef(false);
  const canUndo = data.undoable && !!data.action_id && state !== "done";

  const undo = async () => {
    if (sending.current || !canUndo) return;
    sending.current = true;
    setState("busy");
    setError(null);
    const res = await callCli("khipu_owner_direction_undo", { id: data.action_id });
    if (res.ok) {
      setState("done");
      onUndone();
    } else {
      setState("idle");
      setError(undoRefusal(res.refusal?.code, res.error));
      sending.current = false;
    }
  };

  const where = whereWords(data);
  return (
    <div className="td-receipt" data-undone={state === "done" ? "true" : undefined} role="status">
      {state === "done" ? <Undo2 size={16} aria-hidden /> : <Check size={16} aria-hidden />}
      <div className="td-receipt-b">
        <p>{data.text}</p>
        {where ? <p className="td-receipt-w">{where}</p> : null}
        {state === "done" ? <p className="td-receipt-w">Undone.</p> : null}
        {error ? <p className="td-receipt-err" role="alert">{error}</p> : null}
      </div>
      {canUndo ? (
        <button
          type="button"
          className="td-undo"
          aria-disabled={state === "busy" ? "true" : undefined}
          onKeyDown={(e) => { if (e.repeat) e.preventDefault(); }}
          onClick={() => void undo()}
        >
          <Undo2 size={15} aria-hidden />
          {state === "busy" ? "Undoing…" : "Undo"}
        </button>
      ) : null}
    </div>
  );
}
