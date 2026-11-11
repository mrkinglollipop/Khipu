/* Forgets this Mac holds that the hub lacks (fact provenance design,
   docs/plans/2026-10-23-fact-provenance.md, section 6.2, "Forgets the hub
   lacks").

   This Mac shows no owed items, decisions or deliverables while one is
   listed: a forget learned from a backup, or one whose hub step failed.
   `khipu episode forget-pending --from-app` lists them (summaries only for
   the app itself or the owner's conversation), as the last fact copy found
   them. The owner's actions (each `--from-app`, owner actions
   `forget.send`, `forget.here` and `forget.not_same`): "Send to the hub"
   for the listed forgets, and for a forgotten episode number whose hub
   episode is live, "Forget it here" or "Not the same conversation". Shown in
   Activity, beside Forget. */

import { useCallback, useEffect, useState } from "react";
import { Loader2 } from "lucide-react";

type Listed = {
  ok?: boolean;
  strings?: string[];
  ids?: { id: number; summary?: string | null }[];
  count?: number;
  error?: string;
};

type Done = { ok?: boolean; pending?: number; error?: string; hub_unreachable?: boolean };

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
 *  changes after a forget, so the list is read again. */
export function PendingForgets({
  run,
  refreshKey,
}: {
  run: (args: string[]) => Promise<string>;
  refreshKey?: number;
}) {
  const [listed, setListed] = useState<Listed>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setListed(parse<Listed>(await run(["episode", "forget-pending", "--from-app"])) ?? {});
    } catch {
      setListed({});
    }
  }, [run]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  const act = useCallback(
    async (key: string, argv: string[], failed: string) => {
      setBusy(key);
      setError(null);
      try {
        const out = parse<Done>(await run(["episode", "forget-pending", ...argv, "--from-app"]));
        if (!out || out.ok !== true) {
          setError(
            out?.hub_unreachable
              ? "Khipu can't reach your saved memory right now. Try again in a moment."
              : `${typeof out?.error === "string" ? out.error : failed}.`,
          );
        }
      } catch (e) {
        setError(String(e));
      } finally {
        setBusy(null);
        await load();
      }
    },
    [run, load],
  );

  const strings = listed.strings ?? [];
  const ids = listed.ids ?? [];
  const count = strings.length + ids.length;
  if (count <= 0 && !error) return null;
  return (
    <div className="callout warn" role="status">
      <div className="txt">
        <b>
          {forgets(count)} {count === 1 ? "hasn't" : "haven't"} reached your saved memory
        </b>
        <span>
          Until they do, this Mac holds back what it remembers from your conversations, so nothing you forgot can
          come back.
        </span>
        {ids.map((item) => (
          <span key={item.id}>
            You forgot conversation {item.id} on this Mac, but your saved memory still has a conversation {item.id}
            {item.summary ? `: "${item.summary}"` : ""}. Forget it here too if it is the same one.
          </span>
        ))}
        {error ? <span role="alert">{error}</span> : null}
      </div>
      <div className="cta">
        {strings.length > 0 ? (
          <button
            type="button"
            className="sm primary"
            disabled={busy !== null}
            aria-busy={busy === "send" || undefined}
            onClick={() => void act("send", ["--send"], "Not every forget was sent")}
          >
            {busy === "send" ? <Loader2 size={14} className="spin" aria-hidden /> : null}
            Send {forgets(strings.length)} now
          </button>
        ) : null}
        {ids.map((item) => (
          <span key={item.id} className="row">
            <button
              type="button"
              className="sm"
              disabled={busy !== null}
              aria-busy={busy === `here-${item.id}` || undefined}
              onClick={() =>
                void act(`here-${item.id}`, ["--forget-here", String(item.id)], `Conversation ${item.id} wasn't forgotten`)
              }
            >
              {busy === `here-${item.id}` ? <Loader2 size={14} className="spin" aria-hidden /> : null}
              Forget conversation {item.id} there too
            </button>
            <button
              type="button"
              className="sm"
              disabled={busy !== null}
              aria-busy={busy === `same-${item.id}` || undefined}
              onClick={() =>
                void act(`same-${item.id}`, ["--not-same", String(item.id)], `Conversation ${item.id} is still listed`)
              }
            >
              {busy === `same-${item.id}` ? <Loader2 size={14} className="spin" aria-hidden /> : null}
              Not the same conversation
            </button>
          </span>
        ))}
      </div>
    </div>
  );
}
