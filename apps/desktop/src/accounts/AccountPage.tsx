/* The selected account's page (slice A4): how much is left in each window,
   sign-in, and the three things an agent may be told about this account:
   Pause until, Where agents stop, Let one conversation run ahead. Each
   control calls the old control's own command with the old arguments. */

import { useEffect, useId, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Check, Loader2, Undo2 } from "lucide-react";
import { callCli } from "../settingsCli";
import { plainError } from "../humanWords";
import { SignInControl, liveWords } from "../PlansCard";
import type { UsageAccount, UsageReport } from "../PlansCard";
import { whenLocal } from "../routing";
import type { RouteAccount, RouteView } from "../routing";
import { capRemoveArgs, ownerActionId } from "../accountControlsModel";
import { chosenEnd } from "../routingWords";
import type { EndChoice } from "../routingWords";
import type { OverrideRow } from "../ConversationOverrides";
import {
  accountWindows,
  grantEnds,
  pauseEnds,
  planLabel,
  ruleSentences,
  stopAt,
  stopAtOptions,
} from "./accountsModel";
import type { RowModel } from "./accountsModel";
import { ownerLine } from "./useAccountWrites";
import type { AccountWrites, Receipt } from "./useAccountWrites";

/** A conversation `route override candidates` offers. `subject` goes back to the grant unchanged. */
export type Candidate = {
  subject: Record<string, unknown>;
  title: string;
  mac: string | null;
  last_active: string | null;
};

type Candidates = { state: "reading" } | { state: "failed"; error: string } | { state: "ready"; list: Candidate[] };

const spin = (on: boolean) => (on ? <Loader2 size={14} className="spin" aria-hidden /> : null);

/** One receipt: what was done, and Undo while it can be undone. */
export function ReceiptLine({ receipt, writes, variant }: { receipt: Receipt; writes: AccountWrites; variant: "block" | "inline" }) {
  const undoing = receipt.state === "undoing";
  return (
    <div className={`ac-receipt ${variant}`} role="status">
      <Check size={16} aria-hidden />
      <div className="ac-receipt-body">
        <span>{receipt.text}</span>
        {receipt.state === "failed" && receipt.error ? <span className="ac-error" role="alert">{receipt.error}</span> : null}
      </div>
      {receipt.undoId && receipt.state !== "undone" ? (
        <button type="button" className="ac-quiet" disabled={undoing} onClick={() => void writes.undo()}>
          {undoing ? spin(true) : <Undo2 size={14} aria-hidden />}
          Undo
        </button>
      ) : null}
      <button type="button" className="ac-quiet ac-dismiss" aria-label="Dismiss this note" onClick={writes.dismissReceipt}>
        Dismiss
      </button>
    </div>
  );
}

function Err({ writes, id }: { writes: AccountWrites; id: string }) {
  const e = writes.errorOf(id);
  return e ? <p className="ac-error" role="alert">{e}</p> : null;
}

function EndSelect({ id, label, choices, value, onChange, custom, onCustom }: {
  id: string; label: string; choices: EndChoice[]; value: string; onChange: (v: string) => void;
  custom: string; onCustom: (v: string) => void;
}) {
  return (
    <>
      <label className="ac-field">
        <span>{label}</span>
        <select id={id} value={value} onChange={(e) => onChange(e.target.value)}>
          {choices.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
        </select>
      </label>
      {value === "custom" ? (
        <label className="ac-field">
          <span>Date and time</span>
          <input type="datetime-local" value={custom} onChange={(e) => onCustom(e.target.value)} />
        </label>
      ) : null}
    </>
  );
}

function PauseControl({ a, writes, now }: { a: RouteAccount; writes: AccountWrites; now: Date }) {
  const choices = pauseEnds(now);
  const [picked, setChoice] = useState("tomorrow");
  const choice = choices.some((c) => c.id === picked) ? picked : choices[0].id;
  const [custom, setCustom] = useState("");
  const id = `pause:${a.key}`;
  const liftId = `lift:${a.key}`;
  const paused = a.state === "paused";
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    // The times the owner was offered were worded from this clock, so the choice is read against it.
    const at = now;
    const end = chosenEnd(choice, custom, choices, at);
    if ("error" in end) {
      writes.fail(id, end.error);
      return;
    }
    await writes.run(id, "khipu_route_pause", { account: a.key, until: end.iso }, {
      op: true,
      receipt: (data) => ({
        where: a.key,
        text: `${a.label} is paused ${end.iso ? `until ${whenLocal(end.iso, at)}` : "until you resume it"}.`,
        undoId: ownerActionId(data),
      }),
    });
  };
  return (
    <div className="ac-ctl">
      <h4>Pause</h4>
      <p className="ac-help">
        {paused
          ? `Paused by you ${a.until ? `until ${whenLocal(a.until, now)}` : "until you resume it"}. Agents aren't sent here.`
          : "Agents aren't sent here while it's paused. Anything already running carries on."}
      </p>
      <form className="ac-doit" onSubmit={submit} aria-label={`Pause ${a.label}`}>
        <EndSelect id={`${id}-until`} label="Pause until" choices={choices} value={choice} onChange={setChoice} custom={custom} onCustom={setCustom} />
        <button type="submit" className="ac-btn" disabled={writes.isBusy(id)}>
          {spin(writes.isBusy(id))}
          {paused ? "Change the pause" : "Pause"}
        </button>
        {paused ? (
          <button type="button" className="ac-btn" disabled={writes.isBusy(liftId)}
            onClick={() => void writes.run(liftId, "khipu_route_lift", { account: a.key }, {
              op: true,
              receipt: (data) => ({ where: a.key, text: `${a.label} is open again.`, undoId: ownerActionId(data) }),
            })}>
            {spin(writes.isBusy(liftId))}
            Resume now
          </button>
        ) : null}
      </form>
      <Err writes={writes} id={id} />
      <Err writes={writes} id={liftId} />
    </div>
  );
}

function StopAtControl({ a, writes }: { a: RouteAccount; writes: AccountWrites }) {
  const stop = stopAt(a);
  const current = stop.kind === "level" ? stop.pct : null;
  const [value, setValue] = useState(current == null ? "" : String(current));
  // The shown level follows the record, so a change made elsewhere is not hidden by an old choice.
  useEffect(() => setValue(current == null ? "" : String(current)), [current, a.key]);
  const id = `stop:${a.key}`;
  const others = stop.kind === "level" ? stop.others : stop.caps;
  const changed = stop.kind === "level" && value !== (current == null ? "" : String(current));
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (stop.kind !== "level" || !changed) return;
    if (value === "") {
      if (!stop.cap) return;
      await writes.run(id, "khipu_route_cap", { action: "remove", account: a.key, cap: capRemoveArgs(stop.cap) }, {
        op: true,
        receipt: (data) => ({ where: a.key, text: `Agents no longer stop early on ${a.label}.`, undoId: ownerActionId(data) }),
      });
      return;
    }
    const pct = Number(value);
    await writes.run(id, "khipu_route_cap", { action: "set", account: a.key, cap: { kind: "share", window: stop.window, pct } }, {
      op: true,
      receipt: (data) => ({ where: a.key, text: `Agents now stop at ${pct}% on ${a.label}.`, undoId: ownerActionId(data) }),
    });
  };
  return (
    <div className="ac-ctl">
      <h4>Where agents stop</h4>
      {stop.kind === "level" ? (
        <>
          <p className="ac-help">Agents stop using this account once this much of the limit is used. Anything above it is left for you.</p>
          <form className="ac-doit" onSubmit={save} aria-label={`Where agents stop on ${a.label}`}>
            <label className="ac-field">
              <span>Stop at</span>
              <select value={value} onChange={(e) => setValue(e.target.value)}>
                {stopAtOptions(current).map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </label>
            <button type="submit" className="ac-btn" disabled={!changed || writes.isBusy(id)}>
              {spin(writes.isBusy(id))}
              Save
            </button>
          </form>
        </>
      ) : (
        <p className="ac-help"><strong>Set by your rules.</strong> This account has more than one limit, so there is no single level to pick here. They stay as they are.</p>
      )}
      {others.length ? (
        <>
          <p className="ac-help">{stop.kind === "level" ? "Also set by your rules:" : "Its limits:"}</p>
          <ul className="ac-rules">{ruleSentences(others).map((s, i) => <li key={i}>{s}</li>)}</ul>
        </>
      ) : null}
      <Err writes={writes} id={id} />
    </div>
  );
}

function RunAhead({ a, writes, now, overrides, thisMac }: {
  a: RouteAccount; writes: AccountWrites; now: Date; overrides: OverrideRow[]; thisMac: string | null;
}) {
  const [cands, setCands] = useState<Candidates>({ state: "reading" });
  const [pick, setPick] = useState(0);
  const [picked, setChoice] = useState("soon");
  const [custom, setCustom] = useState("");
  const base = useId();
  // Reading the list changes nothing; it runs when the page opens for this account.
  useEffect(() => {
    let live = true;
    setCands({ state: "reading" });
    setPick(0);
    void callCli<{ candidates?: Candidate[] }>("khipu_route_override_candidates", { account: a.key }).then((res) => {
      if (!live) return;
      if (res.ok && Array.isArray(res.data.candidates)) setCands({ state: "ready", list: res.data.candidates });
      else setCands({ state: "failed", error: res.ok ? "unreadable" : plainError(res.error) });
    });
    return () => { live = false; };
  }, [a.key]);
  const ends = grantEnds(now);
  // A choice that has since gone (Tonight, after 9 PM) falls back to the first one offered.
  const choice = ends.some((c) => c.id === picked) ? picked : ends[0].id;
  const mine = overrides.filter((o) => o.label === a.label);
  const id = `grant:${a.key}`;
  const list = cands.state === "ready" ? cands.list : [];
  const chosen = list[pick] ?? null;
  const grant = async (e: FormEvent) => {
    e.preventDefault();
    if (!chosen) return;
    // The times the owner was offered were worded from this clock, so the choice is read against it.
    const at = now;
    const end = chosenEnd(choice, custom, ends, at);
    // The app never offers "no end": a grant always carries a time.
    if ("error" in end || !end.iso) {
      writes.fail(id, "error" in end ? end.error : "Choose when it ends.");
      return;
    }
    const until = end.iso;
    await writes.run(id, "khipu_route_override_act", { verb: "grant", account: a.key, subject: chosen.subject, until }, {
      receipt: (data) => ({
        where: a.key,
        text: `“${chosen.title}” may run ahead on ${a.label} until ${whenLocal(until, at)}.`,
        undoId: ownerLine(data),
      }),
    });
  };
  return (
    <div className="ac-ctl">
      <h4>Let one conversation run ahead</h4>
      <p className="ac-help">That conversation may keep using {a.label} past its pace until the time you pick. Everything else keeps to pace.</p>
      {mine.length ? <RunningAheadRows rows={mine} writes={writes} now={now} whereKey={a.key} /> : null}
      {cands.state === "reading" ? (
        <p className="ac-help" role="status">Looking for conversations…</p>
      ) : cands.state === "failed" ? (
        <p className="ac-error" role="alert">Khipu couldn't list the conversations: {cands.error}</p>
      ) : list.length === 0 ? (
        <p className="ac-help">No conversation on {thisMac ?? "this Mac"} has used {a.label} in the last day, so there is none to pick.</p>
      ) : (
        <form className="ac-doit" onSubmit={grant} aria-label={`Let a conversation run ahead on ${a.label}`}>
          <label className="ac-field">
            <span>Which conversation</span>
            <select value={pick} onChange={(e) => setPick(Number(e.target.value))}>
              {list.map((c, i) => <option key={i} value={i}>{c.title}{c.mac ? ` (${c.mac})` : ""}</option>)}
            </select>
          </label>
          <EndSelect id={`${base}-until`} label="Until" choices={ends} value={choice} onChange={setChoice} custom={custom} onCustom={setCustom} />
          <button type="submit" className="ac-btn" disabled={writes.isBusy(id)}>
            {spin(writes.isBusy(id))}
            Let it run ahead
          </button>
        </form>
      )}
      {cands.state === "ready" && list.length ? <p className="ac-help">Conversations on {thisMac ?? "this Mac"} only.</p> : null}
      <Err writes={writes} id={id} />
    </div>
  );
}

/** The conversations allowed to run ahead, each with End now (and Keep for a week when Khipu asks). */
export function RunningAheadRows({ rows, writes, now, whereKey }: {
  rows: OverrideRow[]; writes: AccountWrites; now: Date;
  /** The account's key when the rows sit on its page; left out in the list's own section. */
  whereKey?: string;
}) {
  const compact = whereKey != null;
  return (
    <div className={compact ? "ac-ahead-rows compact" : "ac-ahead-rows"}>
      {rows.map((row) => {
        const end = `end:${row.id}`;
        const keep = `keep:${row.id}`;
        const act = async (verb: "end" | "keep") => {
          await writes.run(verb === "end" ? end : keep, "khipu_route_override_act", { verb, id: row.id }, {
            receipt: (data) => ({
              where: whereKey ?? "room",
              text: verb === "end"
                ? `Ended. ${row.who} is back to the normal pace on ${row.label}.`
                : `Kept for another week: ${row.who} on ${row.label}.`,
              undoId: verb === "end" ? ownerLine(data) : null,
            }),
          });
        };
        const until = row.indefinite
          ? `No end. ${row.review_due ? "Time to look at it again." : `Khipu asks again ${whenLocal(row.review_at, now)}.`}`
          : `Until ${whenLocal(row.until, now)}`;
        return (
          <div key={row.id} className="ac-ahead-row">
            <div>
              <div>{compact ? row.who : `${row.who} on ${row.label}`}</div>
              <div className="ac-muted">{until}</div>
              <Err writes={writes} id={end} />
              <Err writes={writes} id={keep} />
            </div>
            <div className="ac-row-acts">
              {row.review_due ? (
                <button type="button" className="ac-btn" disabled={writes.isBusy(keep)} onClick={() => void act("keep")}>
                  {spin(writes.isBusy(keep))}
                  Keep for a week
                </button>
              ) : null}
              <button type="button" className="ac-btn" disabled={writes.isBusy(end)} aria-label={`End now: ${row.who} on ${row.label}`} onClick={() => void act("end")}>
                {spin(writes.isBusy(end))}
                End now
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function SignInSection({ usage, onSignedIn }: { usage: UsageAccount | undefined; onSignedIn: () => Promise<unknown> }) {
  if (!usage) {
    return <div className="ac-kv"><div className="ac-k">Sign-in</div><div className="ac-v">Khipu hasn't read this account's sign-in yet.</div></div>;
  }
  const words = liveWords(usage);
  return (
    <div className="ac-kv">
      <div className="ac-k">Sign-in</div>
      <div className="ac-v">{words.text}</div>
      <div className="ac-signin"><SignInControl row={usage} onSignedIn={onSignedIn} /></div>
    </div>
  );
}

export function AccountPage({ a, row, view, plans, now, writes, overrides, thisMac, onSignedIn, runningHere, more }: {
  a: RouteAccount;
  row: RowModel;
  view: RouteView;
  plans: UsageReport | null | undefined;
  now: Date;
  writes: AccountWrites;
  overrides: OverrideRow[];
  thisMac: string | null;
  onSignedIn: () => Promise<unknown>;
  /** Slice A5 fills this with "Running here now". */
  runningHere?: ReactNode;
  /** More for this account (A11): ending what keeps work off it, clearing a report, rename, stop tracking. */
  more?: ReactNode;
}) {
  const usage = (plans?.accounts ?? []).find((r) => r.key === a.key);
  // A reading is shown only while it is a true picture of the account: not when Khipu can't read it
  // (signed out), doesn't know its state, or only thinks it is open.
  const windows = ["room", "paused", "limited"].includes(row.kind) ? accountWindows(a, view, now) : [];
  const plan = planLabel(a, plans);
  const receipt = writes.receipt && writes.receipt.where === a.key ? writes.receipt : null;
  return (
    <article className="ac-page" aria-label={a.label}>
      <header className="ac-dhead">
        <h2>{a.label}</h2>
        {plan ? <p className="ac-muted">{plan}</p> : null}
      </header>
      {receipt ? <ReceiptLine receipt={receipt} writes={writes} variant="inline" /> : null}
      <div className="ac-dgrid">
        <div>
          <h3>How much is left</h3>
          {windows.length ? windows.map((w) => (
            <div key={w.key} className="ac-kv">
              <div className="ac-k">{w.name}</div>
              <div className="ac-v">{w.left}% left{w.resets ? ` · resets ${w.resets}` : ""} · <b>{w.word}</b></div>
              {w.note ? <div className="ac-v ac-muted">{w.note}</div> : null}
            </div>
          )) : null}
          {row.kind !== "room" ? (
            <div className="ac-kv"><div className="ac-k">Now</div><div className="ac-v">{row.line} · <b>{row.word}</b></div></div>
          ) : null}
          <SignInSection usage={usage} onSignedIn={onSignedIn} />
          {runningHere}
        </div>
        <div>
          <h3>What agents may do here</h3>
          <PauseControl a={a} writes={writes} now={now} />
          <StopAtControl a={a} writes={writes} />
          <RunAhead a={a} writes={writes} now={now} overrides={overrides} thisMac={thisMac} />
          {more}
        </div>
      </div>
    </article>
  );
}
