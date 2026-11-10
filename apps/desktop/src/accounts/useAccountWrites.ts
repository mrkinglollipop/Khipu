/* Every change the Accounts place can make goes through here, so the rules
   hold in one place:

   - a change is made only by a click on a control, never on load, focus or
     selection;
   - a control is disabled while its call is out, and a second click on it is
     ignored until the call ends (a ref, so even two clicks in one frame make
     one call);
   - after every call, a refused or failed one included, the record is read
     again, so the screen shows what is true, not what was asked for;
   - a refusal or failure says so in plain words;
   - a receipt's Undo is the existing `khipu_owner_direction_undo` on the id
     the change itself returned, called once per click.

   The commands and arguments are the old controls' own (AccountControls.tsx,
   ConversationOverrides.tsx): the route commands carry an `op` like the old
   writer's, the override commands do not, as the old section did not. */

import { useCallback, useRef, useState } from "react";
import { callCli } from "../settingsCli";
import { plainError } from "../humanWords";

export type Receipt = {
  /** The account's key, or "room" for the list's own receipt. */
  where: string;
  text: string;
  /** The owner-action line this change made; null when the answer carried none (no Undo). */
  undoId: string | null;
  state: "ready" | "undoing" | "undone" | "failed";
  error?: string;
};

export type Made = { where: string; text: string; undoId: string | null };

type Options = {
  /** Add the `op` the old route writer adds (not for the override commands). */
  op?: boolean;
  /** The receipt this change leaves, from the answer. */
  receipt?: (data: Record<string, unknown>) => Made | null;
};

const MOVED = "This was changed somewhere else after this page read it. Khipu read it again; check it and try once more.";

export function useAccountWrites(rereadAll: () => Promise<unknown>) {
  const inflight = useRef(new Set<string>());
  const [busy, setBusy] = useState<string[]>([]);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [receipt, setReceipt] = useState<Receipt | null>(null);

  const mark = (id: string, on: boolean) => {
    if (on) inflight.current.add(id);
    else inflight.current.delete(id);
    setBusy([...inflight.current]);
  };
  const setError = (id: string, message: string | null) =>
    setErrors((prev) => {
      const next = { ...prev };
      if (message) next[id] = message;
      else delete next[id];
      return next;
    });

  const run = useCallback(
    async (id: string, command: string, args: Record<string, unknown>, opts: Options = {}): Promise<boolean> => {
      if (inflight.current.has(id)) return false;
      mark(id, true);
      setError(id, null);
      try {
        const res = await callCli<Record<string, unknown>>(command, opts.op ? { ...args, op: crypto.randomUUID() } : args);
        // The screen shows what is true after any answer.
        await rereadAll();
        if (!res.ok) {
          setError(
            id,
            res.refusal?.code === "version_moved"
              ? MOVED
              : `Khipu didn't make that change: ${plainError(res.error)}`,
          );
          return false;
        }
        const made = opts.receipt?.(res.data ?? {});
        if (made) setReceipt({ ...made, state: "ready" });
        return true;
      } finally {
        mark(id, false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rereadAll],
  );

  const undo = useCallback(async () => {
    const r = receipt;
    if (!r || !r.undoId || r.state === "undoing" || r.state === "undone" || inflight.current.has("undo")) return;
    mark("undo", true);
    setReceipt({ ...r, state: "undoing", error: undefined });
    try {
      const res = await callCli("khipu_owner_direction_undo", { id: r.undoId });
      await rereadAll();
      if (res.ok) setReceipt({ ...r, state: "undone", text: "Undone.", undoId: null });
      else setReceipt({ ...r, state: "failed", error: `Khipu couldn't undo that: ${plainError(res.error)}` });
    } finally {
      mark("undo", false);
    }
  }, [receipt, rereadAll]);

  return {
    isBusy: (id: string) => busy.includes(id),
    errorOf: (id: string) => errors[id] ?? null,
    clearError: (id: string) => setError(id, null),
    fail: setError,
    run,
    receipt,
    dismissReceipt: () => setReceipt(null),
    undo,
  };
}

export type AccountWrites = ReturnType<typeof useAccountWrites>;

/** The owner-action id a write's answer carries (any non-empty string; the
 *  route commands' are 8 hex digits). */
export function ownerLine(data: Record<string, unknown>): string | null {
  const id = data.owner_action;
  return typeof id === "string" && id ? id : null;
}
