/* Settings -> Accounts and routing: two small pieces for letting one
   conversation run ahead of pace (docs/plans/2026-11-03-conversation-overrides.md).

   `OverridesSection` (Conversations page) lists who may run ahead, where it
   stops, and ends one with a single click that shows an Undo. `CleanUpWalk`
   (Who gets what work page) goes through the priority clean-up items one at a
   time in plain words; each item has one Yes, or "Leave it", and the walk can
   be stopped and picked up again because only the items left aside are
   remembered. Every word shown comes from Khipu's own plain sentences. */

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Tag } from "./ui";
import { callCli } from "./settingsCli";
import { whenLocal } from "./routing";

export type OverrideRow = {
  id: string;
  label: string;
  who: string;
  until?: string | null;
  indefinite?: boolean;
  review_at?: string | null;
  review_due?: boolean;
  cap: string;
};

type OverridesAnswer = { overrides: OverrideRow[] };

export type WalkItem = {
  code: string;
  key: string;
  words: string;
  yes: string;
  number: number;
  of: number;
  came_back?: boolean;
  settle: { confirm: { action: string; args: Record<string, string> } | null };
};

export type Walk = {
  total: number;
  left_for_later: number;
  item: WalkItem | null;
  soon?: string[];
};

const spin = (on: boolean) => (on ? <Loader2 size={14} className="spin" aria-hidden /> : null);

function ownerLine(row: OverrideRow, now: Date): string {
  if (row.indefinite) return `No end. ${row.review_due ? "Time to look at it again." : `Khipu asks again ${whenLocal(row.review_at, now)}.`}`;
  return `Ends ${whenLocal(row.until, now)}.`;
}

export function OverridesSection({ now, nonce = 0 }: { now: Date; nonce?: number }) {
  const [rows, setRows] = useState<OverrideRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [readFailed, setReadFailed] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [ended, setEnded] = useState<{ row: OverrideRow; action: string } | null>(null);

  const load = useCallback(async () => {
    const res = await callCli<OverridesAnswer>("khipu_route_override_view");
    if (res.ok && Array.isArray(res.data.overrides)) {
      setRows(res.data.overrides);
      setReadFailed(false);
    } else {
      setReadFailed(true);
    }
  }, []);
  useEffect(() => { void load(); }, [load, nonce]);

  async function act(row: OverrideRow, verb: "end" | "keep") {
    setBusy(row.id);
    const res = await callCli<{ owner_action?: string }>("khipu_route_override_act", { verb, id: row.id });
    setBusy(null);
    if (!res.ok) {
      setError(res.error);
      return;
    }
    setError(null);
    setEnded(verb === "end" && res.data.owner_action ? { row, action: res.data.owner_action } : null);
    await load();
  }

  async function undo() {
    if (!ended) return;
    setBusy("undo");
    const res = await callCli("khipu_owner_direction_undo", { id: ended.action });
    setBusy(null);
    if (!res.ok) setError(res.error);
    else setEnded(null);
    await load();
  }

  return (
    <section className="route-page-group" aria-labelledby="conversation-overrides">
      <div className="route-page-group-head">
        <h3 id="conversation-overrides">Allowed to run ahead {rows ? <Tag>{rows.length}</Tag> : null}</h3>
      </div>
      <div className="route-page-group-body">
        <p className="set-help">
          A conversation you let run ahead of the pace on one account. Everyone else keeps the normal pace, and holds,
          pauses and limits still apply to it.
        </p>
        {rows && rows.length ? (
          <ul className="mrows" role="list" aria-label="Conversations allowed to run ahead">
            {rows.map((row) => (
              <li key={row.id} className="mrow override-row">
                <span className="name">
                  {row.who} <span className="override-on">on {row.label}</span>
                </span>
                <span className="end">
                  {row.review_due ? (
                    <button type="button" className="sm" disabled={busy === row.id} aria-label={`Keep the override on ${row.label} for another week`}
                      onClick={() => void act(row, "keep")}>
                      {spin(busy === row.id)}
                      Keep for a week
                    </button>
                  ) : null}
                  <button type="button" className="sm" disabled={busy === row.id} aria-label={`End the override on ${row.label} now`}
                    onClick={() => void act(row, "end")}>
                    {spin(busy === row.id && !row.review_due)}
                    End early
                  </button>
                </span>
                <span className="src override-detail">
                  {ownerLine(row, now)} {row.cap}.
                </span>
              </li>
            ))}
          </ul>
        ) : rows ? (
          <p className="set-help">No conversation is allowed to run ahead right now.</p>
        ) : readFailed ? (
          <p className="set-help">Khipu couldn't read the conversations that may run ahead.</p>
        ) : (
          <p className="set-help">Reading…</p>
        )}
        {ended ? (
          <div className="route-undo" role="status">
            <span>Ended the override on {ended.row.label}.</span>
            <button type="button" className="sm" disabled={busy === "undo"} onClick={() => void undo()}>
              {spin(busy === "undo")}
              Undo
            </button>
          </div>
        ) : null}
        {error ? <p className="set-error" role="alert">{error}</p> : null}
      </div>
    </section>
  );
}

/** What one Yes does, as the Tauri command that does it. A twin pair has no
 *  change to make: its Yes just stops the question. */
export function settleCall(item: WalkItem): { command: string; args: Record<string, unknown> } {
  const confirm = item.settle.confirm;
  if (!confirm) return { command: "khipu_route_cleanup_walk", args: { keep: item.key } };
  if (confirm.action === "route.override.end") return { command: "khipu_route_override_act", args: { verb: "end", id: confirm.args.override } };
  if (confirm.action === "route.override.keep") return { command: "khipu_route_override_act", args: { verb: "keep", id: confirm.args.override } };
  return {
    command: "khipu_route_exception_end",
    args: { account: confirm.args.account, id: confirm.args.end_exception, expect: confirm.args.expect },
  };
}

export function CleanUpWalk({ onChanged }: { onChanged?: () => void }) {
  const [walk, setWalk] = useState<Walk | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [readFailed, setReadFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [started, setStarted] = useState(false);
  const headRef = useRef<HTMLHeadingElement>(null);

  const read = useCallback(async (args: Record<string, unknown> = {}) => {
    const res = await callCli<Walk>("khipu_route_cleanup_walk", args);
    if (res.ok && typeof res.data.total === "number") {
      setWalk(res.data);
      setReadFailed(false);
    } else {
      setReadFailed(true);
    }
  }, []);
  useEffect(() => { void read(); }, [read]);

  async function step(args: Record<string, unknown> | null, then: Record<string, unknown> = {}) {
    setBusy(true);
    if (args) {
      const call = await callCli(args.command as string, args.args as Record<string, unknown>);
      if (!call.ok) {
        setBusy(false);
        setError(call.error);
        return;
      }
      onChanged?.();
    }
    await read(then);
    setBusy(false);
  }

  const item = walk?.item ?? null;
  return (
    <section className="route-page-group" aria-labelledby="route-cleanup">
      <div className="route-page-group-head">
        <h3 id="route-cleanup" ref={headRef} tabIndex={-1}>Tidy up {walk && walk.total ? <Tag tone="warn">{walk.total}</Tag> : null}</h3>
      </div>
      <div className="route-page-group-body">
        <p className="set-help">
          Repeated or out-of-date temporary instructions and overrides, one at a time. Nothing changes until you say yes,
          and you can stop and come back.
        </p>
        {!walk ? (
          <p className="set-help">{readFailed ? "Khipu couldn't read the clean-up." : "Reading…"}</p>
        ) : !item ? (
          <p className="set-help cleanup-done">Nothing to tidy up.</p>
        ) : !started ? (
          <div className="toolbar">
            <button type="button" className="sm" onClick={() => setStarted(true)}>
              {walk.total === 1 ? "Look at it" : `Go through ${walk.total} items`}
            </button>
            {walk.left_for_later ? <span className="set-help">{walk.left_for_later} set aside earlier.</span> : null}
          </div>
        ) : (
          <div className="cleanup-card" role="group" aria-label={`Tidy-up item ${item.number} of ${item.of}`}>
            <p className="src cleanup-step">Item {item.number} of {item.of}{item.came_back ? ", set aside before" : ""}</p>
            <p className="cleanup-words">{item.words}</p>
            <div className="toolbar">
              <button type="button" className="sm primary" disabled={busy} autoFocus onClick={() => void step(settleCall(item))}>
                {spin(busy)}
                {item.yes.replace(/\.$/, "")}
              </button>
              <button type="button" className="sm" disabled={busy} onClick={() => void step(null, { leave: item.key })}>
                Leave it for now
              </button>
              <button type="button" className="sm link" disabled={busy} onClick={() => setStarted(false)}>
                Stop here
              </button>
            </div>
          </div>
        )}
        {walk && walk.left_for_later && started ? (
          <p className="set-help">
            {walk.left_for_later} set aside.{" "}
            <button type="button" className="sm link" disabled={busy} onClick={() => void step(null, { restart: true })}>
              Start over
            </button>
          </p>
        ) : null}
        {walk?.soon?.length ? <ul className="set-help cleanup-soon">{walk.soon.map((s) => <li key={s}>{s}</li>)}</ul> : null}
        {error ? <p className="set-error" role="alert">{error}</p> : null}
      </div>
    </section>
  );
}
