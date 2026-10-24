/* Forgets this Mac's copy is waiting on the hub for (fact provenance design,
   docs/plans/2026-10-23-fact-provenance.md, section 6, "The wait rule").

   This Mac shows no owed items, decisions or deliverables while one of its
   forgets has not reached the hub: one learned from a backup, or one whose
   hub step failed. `khipu episode forget-pending` lists them; the owner's
   "Send N forgets to the hub" runs `--send --from-app` (owner action
   `forget.send`). Shown in Activity, beside Forget. */

import { useCallback, useEffect, useState } from "react";
import { Loader2 } from "lucide-react";

type Pending = {
  ok?: boolean;
  count?: number;
  hub_unreachable?: boolean;
  error?: string;
};

type Sent = Pending & { pending?: number };

function parse<T>(raw: string): T | null {
  try {
    const value = JSON.parse(raw.trim().split("\n").pop() ?? "");
    return value && typeof value === "object" ? (value as T) : null;
  } catch {
    return null;
  }
}

function forgets(n: number): string {
  return n === 1 ? "1 forget" : `${n} forgets`;
}

/** `run` is the app's `khipu` runner (argv in, stdout out). `refreshKey`
 *  changes after a forget, so the count is read again. */
export function PendingForgets({
  run,
  refreshKey,
}: {
  run: (args: string[]) => Promise<string>;
  refreshKey?: number;
}) {
  const [count, setCount] = useState(0);
  const [hubAway, setHubAway] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const out = parse<Pending>(await run(["episode", "forget-pending"]));
      setCount(typeof out?.count === "number" ? out.count : 0);
      setHubAway(out?.hub_unreachable === true);
    } catch {
      setCount(0);
    }
  }, [run]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  const send = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const out = parse<Sent>(await run(["episode", "forget-pending", "--send", "--from-app"]));
      if (!out || out.ok !== true) {
        const left = typeof out?.pending === "number" ? out.pending : null;
        setError(
          out?.hub_unreachable
            ? "The hub isn't reachable. Try again when it is."
            : `${typeof out?.error === "string" ? out.error : "The hub didn't confirm every forget"}${
                left ? `; ${forgets(left)} still waiting` : ""
              }.`,
        );
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
      await load();
    }
  }, [run, load]);

  if (count <= 0 && !error) return null;
  return (
    <div className="callout warn" role="status">
      <div className="txt">
        <b>
          {forgets(count)} {count === 1 ? "hasn't" : "haven't"} reached the hub
        </b>
        <span>
          Until they do, this Mac shows no owed items, decisions or deliverables, so nothing you forgot can come
          back.{hubAway ? " The hub isn't reachable right now." : ""}
        </span>
        {error ? <span role="alert">{error}</span> : null}
      </div>
      {count > 0 ? (
        <div className="cta">
          <button type="button" className="sm primary" disabled={busy} aria-busy={busy || undefined} onClick={() => void send()}>
            {busy ? <Loader2 size={14} className="spin" aria-hidden /> : null}
            Send {forgets(count)} to the hub
          </button>
        </div>
      ) : null}
    </div>
  );
}
