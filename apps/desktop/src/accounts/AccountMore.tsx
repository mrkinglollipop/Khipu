/* The account page's "More for this account" (slice A11): what the older
   Accounts and routing screen offered on an account that the new page did not
   yet have. Each control keeps its old command and arguments:
   - ending something a rule or a conversation set to keep work off the
     account: `khipu_route_hold_end {id}`, after the same "End this now?"
     confirmation;
   - "It isn't limited: clear this report": `khipu_route_clear {account}`;
   - two reports that disagree, each dismissible: `khipu_route_clear
     {account, report}`;
   - Rename… and Stop tracking…: the old account finder's own dialogs.
   Adding new ones (a hold) is what Pause on the page now does (scope: holds
   become Pause). */
import { useState } from "react";
import type { ReactNode } from "react";
import { Loader2 } from "lucide-react";
import { callCli } from "../settingsCli";
import { accountSource, capitalise, disagreementWords, modelDisagreementWords, saidWhen, whenLocal } from "../routing";
import type { RouteAccount, RouteHold, RouteView } from "../routing";
import { holdKind, stateSaid } from "../routingWords";

type RouteReport = NonNullable<RouteAccount["disagreement"]>;
type RouteModel = NonNullable<RouteAccount["models"]>[number];

function limitClearable(a: RouteAccount, now: Date): boolean {
  if (a.state === "limited" || a.state === "down") return true;
  return (a.models ?? []).some((m) => m.until && new Date(m.until).getTime() > now.getTime());
}

const DISMISS_WHO: Record<string, string> = {
  report: "conversation's",
  runner: "runner's",
  probe: "plan check's",
  check: "reset check's",
};

function sideWords(state: string | undefined, by: string, at: string | null | undefined, now: Date): ReactNode {
  return (
    <>
      <b>{capitalise(stateSaid(state))}</b>: {by}
      {at ? ` ${saidWhen(at, now)}` : ""}.
    </>
  );
}

function useWrite(reread: () => Promise<unknown>) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<{ id: string; text: string } | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const run = async (id: string, command: string, args: Record<string, unknown>, said: string): Promise<boolean> => {
    if (busy) return false;
    setBusy(id);
    setError(null);
    setDone(null);
    const res = await callCli(command, args);
    if (res.ok) setDone(said);
    else setError({ id, text: res.error });
    await reread();
    setBusy(null);
    return res.ok;
  };
  return { busy, error, done, run };
}

const spin = (on: boolean) => (on ? <Loader2 size={14} className="spin" aria-hidden /> : null);

export function AccountMore({ a, view, now, reread, onRename, onStop }: {
  a: RouteAccount;
  view: RouteView;
  now: Date;
  reread: () => Promise<unknown>;
  onRename?: () => void;
  onStop?: () => void;
}) {
  const w = useWrite(reread);
  const [ending, setEnding] = useState<string | null>(null);
  const holds: RouteHold[] = (view.holds ?? []).filter((h) => h.scope?.account === a.key);
  const canClear = a.state !== "paused" && limitClearable(a, now);

  const sides: { d: RouteReport; model?: RouteModel; sentence: string }[] = [];
  const own = disagreementWords(a, now);
  if (a.disagreement && own) sides.push({ d: a.disagreement, sentence: own });
  const modelWords = modelDisagreementWords(a, now);
  (a.models ?? [])
    .filter((m) => m.disagreement && m.until && new Date(m.until).getTime() > now.getTime())
    .forEach((m, i) => sides.push({ d: m.disagreement!, model: m, sentence: modelWords[i] ?? "" }));
  const said = accountSource(a, now).split(" · ")[0];
  const shownBy = said ? said.charAt(0).toLowerCase() + said.slice(1) : "the stronger report";

  if (!holds.length && !canClear && !sides.length && !onRename && !onStop) return null;
  const err = (id: string) => (w.error?.id === id ? <p className="ac-error" role="alert">{w.error.text}</p> : null);

  return (
    <section className="ac-sec ac-more" aria-labelledby={`ac-more-${a.key}`}>
      <h3 id={`ac-more-${a.key}`}>More for this account</h3>
      {w.done ? <p className="ac-note" role="status">{w.done}</p> : null}

      {holds.map((h) => {
        const id = `hold-end:${h.id}`;
        const what = h.text?.trim() || holdKind(h.reason);
        return (
          <div key={h.id} className="ac-more-item" role="group" aria-label={`Kept off ${a.label}: ${what}`}>
            <p>
              <b>Kept off {a.label}: {what}</b>
              <span className="ac-note"> · {h.until ? `Ends ${whenLocal(h.until, now)}` : "Until you end it"} · {h.owner ? "you said so" : `set by ${h.by || "a conversation"}`}</span>
            </p>
            {ending === h.id ? (
              <div className="ac-more-confirm" role="group" aria-label={`End it on ${a.label}?`}>
                <span>End this now? Conversations stop seeing it on their next prompt.</span>
                <div className="ac-actions">
                  <button type="button" className="ac-btn danger" disabled={w.busy != null} aria-busy={w.busy === id || undefined}
                    onClick={() => void w.run(id, "khipu_route_hold_end", { id: h.id }, `Ended on ${a.label}.`).then((ok) => { if (ok) setEnding(null); })}>
                    {spin(w.busy === id)} End it
                  </button>
                  <button type="button" className="ac-btn" onClick={() => setEnding(null)}>Keep it</button>
                </div>
              </div>
            ) : (
              <button type="button" className="ac-btn" aria-label={`End it on ${a.label}: ${what}`} onClick={() => setEnding(h.id)}>
                End it…
              </button>
            )}
            {err(id)}
          </div>
        );
      })}

      {canClear ? (
        <div className="ac-more-item">
          <button type="button" className="ac-btn" disabled={w.busy != null} aria-busy={w.busy === `clear:${a.key}` || undefined}
            onClick={() => void w.run(`clear:${a.key}`, "khipu_route_clear", { account: a.key },
              `The ${a.state === "down" ? "down" : "limit"} report on ${a.label} is cleared.`)}>
            {spin(w.busy === `clear:${a.key}`)} It isn't limited: clear this report
          </button>
          {err(`clear:${a.key}`)}
        </div>
      ) : null}

      {sides.map(({ d, model: m, sentence }) => {
        const model = m?.model;
        const shown = m
          ? { id: m.id, source: m.source, cleared: false, state: "limited" }
          : { ...(a.witness ?? {}), state: a.state };
        const sidesOf = [
          ...(shown.id && shown.source && shown.source !== "owner" && !shown.cleared
            ? [{ id: shown.id, state: shown.state, source: shown.source }]
            : []),
          ...(d.id && d.source && d.source !== "owner" ? [{ id: d.id, state: d.state ?? "", source: d.source }] : []),
        ];
        const named = sidesOf.map((x) => `Dismiss the ${DISMISS_WHO[x.source] ?? "other"} report`);
        const dismissible = sidesOf.map((x, i) => ({
          ...x,
          label: named.indexOf(named[i]) !== named.lastIndexOf(named[i])
            ? `Dismiss the ${DISMISS_WHO[x.source] ?? "other"} “${stateSaid(x.state)}” report`
            : named[i],
        }));
        return (
          <div key={d.id ?? model ?? "account"} className="ac-more-item" role="group"
            aria-label={`${a.label}${model ? ` (${model})` : ""}: two reports disagree`}>
            <p><b>Two reports disagree{model ? ` about ${model}` : ""}.</b></p>
            <ul>
              {m ? (
                <li>{sideWords("limited", `the ${m.model} limit, reported by ${m.by || "another source"}`, m.at, now)}</li>
              ) : (
                <li>{sideWords(a.state, shownBy, null, now)}</li>
              )}
              <li>{sideWords(d.state, `reported by ${d.by || "another source"}`, d.at, now)}</li>
            </ul>
            <p className="ac-note">{sentence} Dismiss removes only that report; Clear ends the shown limit or down report.</p>
            {dismissible.length ? (
              <div className="ac-actions">
                {dismissible.map((s) => (
                  <button key={s.id} type="button" className="ac-btn" disabled={w.busy != null} aria-busy={w.busy === `dismiss:${s.id}` || undefined}
                    onClick={() => void w.run(`dismiss:${s.id}`, "khipu_route_clear", { account: a.key, report: s.id }, "That report is dismissed.")}>
                    {spin(w.busy === `dismiss:${s.id}`)} {s.label}
                  </button>
                ))}
              </div>
            ) : null}
            {dismissible.map((s) => <span key={s.id}>{err(`dismiss:${s.id}`)}</span>)}
          </div>
        );
      })}

      {onRename || onStop ? (
        <div className="ac-actions">
          {onRename ? <button type="button" className="ac-btn" onClick={onRename}>Rename…</button> : null}
          {onStop ? <button type="button" className="ac-btn" onClick={onStop}>Stop tracking…</button> : null}
        </div>
      ) : null}
    </section>
  );
}
