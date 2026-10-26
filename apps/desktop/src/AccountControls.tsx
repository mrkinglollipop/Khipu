/* The controls inside the Routing detail and the Plan detail (ui-controls
   scope, round 2, session A). Each control sits beside the fact it changes
   and calls the same fixed-argv Tauri commands Settings uses, through the
   same writer, so a change made here is one owner-action line like any other.

   A change that can be taken back applies at once and leaves an Undo bar in
   its own section. The write's own answer carries the id of the owner-action
   line it made (`owner_action`), and Undo runs `owner-direction undo` on
   exactly that id; an answer without one gets a bar with no Undo. */

import { useEffect, useId, useRef, useState } from "react";
import type { FormEvent } from "react";
import { invoke } from "@tauri-apps/api/core";
import {
  Busy,
  ConfirmGroup,
  DisagreePanel,
  EndFields,
  EndOrKeepChoices,
  HOLD_REASON_ARGS,
  WriteError,
  escapeCloses,
  useEndOrKeep,
  useWriter,
} from "./RoutingSettings";
import type { Back, Writer } from "./RoutingSettings";
import { callCli } from "./settingsCli";
import { plainError } from "./humanWords";
import { chosenEnd, endChoices, holdKind } from "./routingWords";
import { whenLocal } from "./routing";
import type { CapStatus, RouteAccount, RouteHold, RouteView } from "./routing";
import type { CapRow } from "./accountDetailModel";
import type { UsageAccount } from "./PlansCard";
import type { Support } from "./accountsApi";
import {
  CAP_SENTENCES,
  capRemoveArgs,
  capSentence,
  capSetArgs,
  charactersWords,
  checkedHost,
  draftFromCap,
  emptyCapDraft,
  movedBy,
  ownerActionId,
  withAccountLast,
  withRole,
  withoutAccount,
} from "./accountControlsModel";
import type { CapDraft, CapSentence } from "./accountControlsModel";

// ---------------------------------------------------------------------------
// The writer with Undo
// ---------------------------------------------------------------------------

export type Section = "state" | "holds" | "caps" | "words";

type Bar = { section: Section; text: string; id: string | null; seq: number; done?: boolean };

type Attempt = (
  id: string,
  command: string,
  args: Record<string, unknown>,
  after?: (() => Promise<unknown>) | null,
  back?: Back,
  onRetrySuccess?: () => void,
) => Promise<boolean>;

type Change = {
  section: Section;
  writeId: string;
  command: string;
  args: Record<string, unknown>;
  /** The owner action this write makes, as the log names it (for readers; Undo uses the returned id). */
  owner: string;
  target?: string;
  /** What the Undo bar says was done. */
  text: string;
  back?: Back;
  /** A write that may be refused until held or paused accounts are answered. */
  attempt?: Attempt;
};

export function useAccountControls(reread: () => Promise<unknown>) {
  const w = useWriter(reread);
  const eok = useEndOrKeep(w);
  const [bar, setBar] = useState<Bar | null>(null);
  const seq = useRef(0);
  useEffect(() => {
    if (!bar) return;
    const t = setTimeout(() => setBar(null), 30_000);
    return () => clearTimeout(t);
  }, [bar]);

  const change = async (o: Change): Promise<boolean> => {
    // A button keeps focus and shows a spinner while its write runs (aria-busy,
    // never disabled), so a second click on it is ignored here.
    if (w.isBusy(o.writeId)) return false;
    // Undo runs on the id this very write returned. An answer without one gives
    // the bar no Undo (it says where to take the change back instead).
    const finish = () => setBar({ section: o.section, text: o.text, id: ownerActionId(w.dataOf(o.writeId)), seq: ++seq.current });
    const ok = o.attempt
      ? await o.attempt(o.writeId, o.command, o.args, null, o.back, finish)
      : await w.run(o.writeId, o.command, o.args, null, o.back);
    if (ok) finish();
    return ok;
  };

  const undo = async () => {
    if (!bar?.id) return;
    const done = bar;
    const ok = await w.run(`undo:${done.section}`, "khipu_owner_direction_undo", { id: done.id });
    if (ok) setBar({ section: done.section, id: null, done: true, text: "Undone.", seq: ++seq.current });
  };

  return { w, eok, bar, setBar, change, undo };
}

export type Controls = ReturnType<typeof useAccountControls>;

/** The Undo bar of one section: it moves focus to Undo when it appears. */
export function UndoBar({ section, ctl }: { section: Section; ctl: Controls }) {
  const { bar, w, undo, setBar } = ctl;
  const btn = useRef<HTMLButtonElement>(null);
  const seq = bar?.section === section ? bar.seq : null;
  useEffect(() => {
    if (seq != null) btn.current?.focus();
  }, [seq]);
  if (!bar || bar.section !== section) return null;
  const errId = `undo:${section}`;
  const err = w.errors[errId];
  return (
    <div className={err ? "undo err" : "undo"} role={err ? "alert" : "status"}>
      <span>
        {err
          ? `Can't undo: ${plainError(err)} You can see the change in Settings → Owner actions.`
          : bar.id || bar.done
            ? bar.text
            : `${bar.text} To take it back, use Settings → Owner actions.`}
      </span>
      {bar.id && !bar.done ? (
        <button ref={btn} type="button" className="sm" disabled={w.isBusy(errId)} aria-busy={w.isBusy(errId) || undefined} onClick={() => void undo()}>
          <Busy on={w.isBusy(errId)} />
          Undo
        </button>
      ) : null}
      <button type="button" className="link sm" onClick={() => setBar(null)}>
        Dismiss
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// State: pause, lift, clear
// ---------------------------------------------------------------------------

function PauseForm({ a, formId, ctl, back, onClose }: { a: RouteAccount; formId: string; ctl: Controls; back: Back; onClose: () => void }) {
  const { w, change } = ctl;
  const [choice, setChoice] = useState("tomorrow");
  const [custom, setCustom] = useState("");
  const selectRef = useRef<HTMLSelectElement>(null);
  useEffect(() => selectRef.current?.focus(), []);
  const id = `pause:${a.key}`;
  async function submit(e: FormEvent) {
    e.preventDefault();
    const now = new Date();
    const end = chosenEnd(choice, custom, endChoices(now, 1), now);
    if ("error" in end) {
      w.fail(id, end.error);
      return;
    }
    const ok = await change({
      section: "state",
      writeId: id,
      command: "khipu_route_pause",
      args: { account: a.key, until: end.iso },
      owner: "route.pause",
      target: a.key,
      text: `${a.label} is paused ${end.iso ? `until ${whenLocal(end.iso, now)}` : "until you lift it"}.`,
      back,
    });
    if (ok) onClose();
  }
  return (
    <form className="route-pause" id={formId} aria-label={`Pause ${a.label}`} onSubmit={submit} onKeyDown={escapeCloses(onClose)}>
      <div className="toolbar">
        <EndFields idBase={formId} label={`Pause ${a.label} until`} firstHours={1} choice={choice} setChoice={setChoice}
          custom={custom} setCustom={setCustom} selectRef={selectRef} />
      </div>
      <p className="set-help">
        This doesn't stop anything that is running; it tells conversations you want {a.label} left alone. They hear it on
        their next prompt, or when they ask.
      </p>
      <WriteError w={w} id={id} />
      <div className="toolbar">
        <button type="submit" className="primary sm" disabled={w.isBusy(id)}>
          <Busy on={w.isBusy(id)} />
          Pause {a.label}
        </button>
        <button type="button" className="sm link" onClick={onClose}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function limitClearable(a: RouteAccount, now: Date): boolean {
  if (a.state === "limited" || a.state === "down") return true;
  return (a.models ?? []).some((m) => m.until && new Date(m.until).getTime() > now.getTime());
}

export function StateControls({ ctl, a, now }: { ctl: Controls; a: RouteAccount; now: Date }) {
  const { w, change } = ctl;
  const [pausing, setPausing] = useState(false);
  const pauseBtn = useRef<HTMLButtonElement>(null);
  const formId = `pause-${useId().replace(/:/g, "")}`;
  const paused = a.state === "paused";
  const canClear = !paused && limitClearable(a, now);
  const liftId = `lift:${a.key}`;
  const clearId = `clear:${a.key}`;
  const back: Back = () => pauseBtn.current;
  const close = () => {
    setPausing(false);
    requestAnimationFrame(() => pauseBtn.current?.focus());
  };
  return (
    <div className="ctl">
      <div className="toolbar">
        <button ref={pauseBtn} type="button" className="sm" aria-expanded={pausing} aria-controls={pausing ? formId : undefined}
          onClick={() => setPausing((v) => !v)}>
          {paused ? "Change the pause…" : `Pause ${a.label} until…`}
        </button>
        {paused ? (
          <button type="button" className="sm" aria-busy={w.isBusy(liftId) || undefined}
            onClick={() => void change({ section: "state", writeId: liftId, command: "khipu_route_lift", args: { account: a.key },
              owner: "route.lift", target: a.key, text: `The pause on ${a.label} is lifted.`, back })}>
            <Busy on={w.isBusy(liftId)} />
            Lift the pause
          </button>
        ) : null}
        {canClear ? (
          <button type="button" className="sm" aria-busy={w.isBusy(clearId) || undefined}
            onClick={() => void change({ section: "state", writeId: clearId, command: "khipu_route_clear", args: { account: a.key },
              owner: "route.clear", target: a.key, text: `The ${a.state === "down" ? "down" : "limit"} report on ${a.label} is cleared.`, back })}>
            <Busy on={w.isBusy(clearId)} />
            It isn't limited: clear this report
          </button>
        ) : null}
      </div>
      {pausing ? <PauseForm a={a} formId={formId} ctl={ctl} back={back} onClose={close} /> : null}
      <DisagreePanel a={a} w={w} now={now} back={back} />
      <WriteError w={w} id={liftId} />
      <WriteError w={w} id={clearId} />
      <UndoBar section="state" ctl={ctl} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Holds on this account
// ---------------------------------------------------------------------------

function holdEnd(h: RouteHold, now: Date): string {
  return h.until ? `Ends ${whenLocal(h.until, now)}` : "Until you end it";
}

function HoldForm({ a, ctl, back, onClose }: { a: RouteAccount; ctl: Controls; back: Back; onClose: () => void }) {
  const { w, change } = ctl;
  const base = useId().replace(/:/g, "");
  const [text, setText] = useState("");
  const [reason, setReason] = useState("other");
  const [choice, setChoice] = useState("tomorrow");
  const [custom, setCustom] = useState("");
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => input.current?.focus(), []);
  const id = `hold-add:${a.key}`;
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!text.trim()) {
      w.fail(id, "Write what conversations should know.");
      return;
    }
    const now = new Date();
    const end = chosenEnd(choice, custom, endChoices(now, 4), now);
    if ("error" in end) {
      w.fail(id, end.error);
      return;
    }
    const ok = await change({
      section: "holds",
      writeId: id,
      command: "khipu_route_hold_add",
      args: { text, reason, scope: "account", account: a.key, until: end.iso },
      owner: "route.hold.add",
      text: `Hold added on ${a.label}.`,
      back,
    });
    if (ok) onClose();
  }
  return (
    <form className="route-pause" aria-label={`Hold ${a.label}`} onSubmit={submit} onKeyDown={escapeCloses(onClose)}>
      <div className="toolbar">
        <span className="route-field">
          <label htmlFor={`${base}-text`}>What conversations should know</label>
          <input id={`${base}-text`} ref={input} value={text} autoComplete="off" placeholder="Keep it free for the review"
            onChange={(e) => setText(e.target.value)} />
        </span>
      </div>
      <div className="toolbar">
        <span className="route-field">
          <label htmlFor={`${base}-kind`}>Kind</label>
          <select id={`${base}-kind`} value={reason} onChange={(e) => setReason(e.target.value)}>
            {HOLD_REASON_ARGS.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
          </select>
        </span>
        <EndFields idBase={`${base}-hold`} label="Ends" firstHours={4} choice={choice} setChoice={setChoice} custom={custom} setCustom={setCustom} />
      </div>
      <WriteError w={w} id={id} />
      <div className="toolbar">
        <button type="submit" className="primary sm" disabled={w.isBusy(id)}>
          <Busy on={w.isBusy(id)} />
          Add hold
        </button>
        <button type="button" className="sm link" onClick={onClose}>
          Cancel
        </button>
      </div>
    </form>
  );
}

/** The holds on this one account, each with End hold, and Hold this account. */
export function HoldControls({ ctl, a, view, now }: { ctl: Controls; a: RouteAccount; view: RouteView; now: Date }) {
  const { w, change } = ctl;
  const holds = (view.holds ?? []).filter((h) => h.scope?.account === a.key);
  const [adding, setAdding] = useState(false);
  const [ending, setEnding] = useState<string | null>(null);
  const addBtn = useRef<HTMLButtonElement>(null);
  const back: Back = () => addBtn.current;
  return (
    <div className="ctl">
      {holds.map((h) => {
        const id = `hold-end:${h.id}`;
        return (
          <div key={h.id} className="sheet-item warn">
            <b>Hold: {h.text?.trim() || holdKind(h.reason)}</b>
            <span className="end">
              <strong>{holdEnd(h, now)}</strong> · {h.owner ? "you said so" : `set by ${h.by || "a conversation"}`}
            </span>
            <div className="toolbar">
              <button type="button" className="sm" aria-expanded={ending === h.id} aria-label={`End the hold on ${a.label}: ${h.text?.trim() || holdKind(h.reason)}`}
                disabled={w.isBusy(id)} onClick={() => setEnding((v) => (v === h.id ? null : h.id))}>
                End hold
              </button>
            </div>
            {ending === h.id ? (
              <ConfirmGroup label={`End the hold on ${a.label}?`} onClose={() => setEnding(null)} back={back}>
                <span>End this hold now? Conversations stop seeing it on their next prompt.</span>
                <WriteError w={w} id={id} />
                <div className="toolbar">
                  <button type="button" className="sm danger" disabled={w.isBusy(id)}
                    onClick={() => void change({ section: "holds", writeId: id, command: "khipu_route_hold_end", args: { id: h.id },
                      owner: "route.hold.end", text: `Hold ended on ${a.label}.`, back }).then((ok) => { if (ok) setEnding(null); })}>
                    <Busy on={w.isBusy(id)} />
                    End the hold
                  </button>
                  <button type="button" className="sm link" onClick={() => setEnding(null)}>
                    Keep it
                  </button>
                </div>
              </ConfirmGroup>
            ) : null}
          </div>
        );
      })}
      {adding ? (
        <HoldForm a={a} ctl={ctl} back={back} onClose={() => { setAdding(false); requestAnimationFrame(() => addBtn.current?.focus()); }} />
      ) : (
        <div className="toolbar">
          <button ref={addBtn} type="button" className="sm" onClick={() => setAdding(true)}>
            Hold {a.label}…
          </button>
        </div>
      )}
      <UndoBar section="holds" ctl={ctl} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Caps
// ---------------------------------------------------------------------------

function CapForm({ a, cap, ctl, back, onClose }: { a: RouteAccount; cap: CapStatus | null; ctl: Controls; back: Back; onClose: () => void }) {
  const { w, change } = ctl;
  const base = useId().replace(/:/g, "");
  const [d, setD] = useState<CapDraft>(() => (cap ? draftFromCap(cap) : emptyCapDraft()));
  const first = useRef<HTMLSelectElement>(null);
  useEffect(() => first.current?.focus(), []);
  const set = <K extends keyof CapDraft>(k: K, v: CapDraft[K]) => setD((prev) => ({ ...prev, [k]: v }));
  const editing = cap != null;
  const id = `cap:${a.key}`;
  async function submit(e: FormEvent) {
    e.preventDefault();
    const out = capSetArgs(d);
    if ("error" in out) {
      w.fail(id, out.error);
      return;
    }
    const ok = await change({
      section: "caps",
      writeId: id,
      command: "khipu_route_cap",
      args: { action: "set", account: a.key, cap: out.cap },
      owner: "route.cap.set",
      target: a.key,
      text: `Cap saved on ${a.label}: ${capSentence({ kind: out.cap.kind, purpose: out.cap.purpose, window: out.cap.window ?? null, pct: out.cap.pct ?? null,
        slack: out.cap.slack ?? null, cap_usd: out.cap.usd ?? null, days: out.cap.days ?? null, cap_n: out.cap.n ?? null })}.`,
      back,
    });
    if (ok) onClose();
  }
  async function remove() {
    if (!cap) return;
    const ok = await change({
      section: "caps",
      writeId: id,
      command: "khipu_route_cap",
      args: { action: "remove", account: a.key, cap: capRemoveArgs(cap) },
      owner: "route.cap.remove",
      target: a.key,
      text: `Cap removed: ${capSentence(cap)}.`,
      back,
    });
    if (ok) onClose();
  }
  const winSelect = (
    <span className="route-field">
      <label htmlFor={`${base}-win`}>Counted over</label>
      <select id={`${base}-win`} value={d.window} disabled={editing} onChange={(e) => set("window", e.target.value as CapDraft["window"])}>
        <option value="weekly">The week</option>
        <option value="five_hour">The five hours</option>
      </select>
    </span>
  );
  const num = (key: "pct" | "slack" | "usd" | "days" | "n", label: string, unit?: string) => (
    <span className="route-field">
      <label htmlFor={`${base}-${key}`}>{label}</label>
      <input id={`${base}-${key}`} className="num" type="number" inputMode="decimal" value={d[key]} onChange={(e) => set(key, e.target.value)} />
      {unit ? <span className="meta">{unit}</span> : null}
    </span>
  );
  return (
    <form className="route-pause" aria-label={editing ? `Change a cap for ${a.label}` : `Add a cap for ${a.label}`} onSubmit={submit}
      onKeyDown={escapeCloses(onClose)}>
      <div className="toolbar">
        <span className="route-field">
          <label htmlFor={`${base}-kind`}>What kind of cap</label>
          <select id={`${base}-kind`} ref={first} value={d.sentence} disabled={editing} onChange={(e) => set("sentence", e.target.value as CapSentence)}>
            {CAP_SENTENCES.map((s) => (
              <option key={s.id} value={s.id}>
                {s.label}
              </option>
            ))}
          </select>
        </span>
      </div>
      <div className="toolbar">
        {d.sentence === "share" ? <>{num("pct", "Percent", "%")}{winSelect}</> : null}
        {d.sentence === "per_day" ? num("pct", "Percent of the week in one day", "%") : null}
        {d.sentence === "pace_target" ? (
          <>
            {num("slack", "Points ahead of an even pace allowed", "points (0 is exactly even)")}
            <span className="route-field">
              <label htmlFor={`${base}-purpose`}>Applies to</label>
              <select id={`${base}-purpose`} value={d.purpose} disabled={editing} onChange={(e) => set("purpose", e.target.value as CapDraft["purpose"])}>
                <option value="job">Jobs</option>
                <option value="check">Checks</option>
              </select>
            </span>
            {winSelect}
          </>
        ) : null}
        {d.sentence === "spend" ? <>{num("usd", "Dollars", "$")}{num("days", "Every", "days")}</> : null}
        {d.sentence === "concurrency" ? num("n", "Jobs at once") : null}
      </div>
      {d.sentence === "pace_target" && d.purpose === "check" ? (
        <div className="toolbar">
          <span className="route-field">
            <label htmlFor={`${base}-tier`}>Only this model tier</label>
            <select id={`${base}-tier`} value={d.tier} onChange={(e) => set("tier", e.target.value as CapDraft["tier"])}>
              <option value="">Any</option>
              <option value="opus">Opus</option>
              <option value="sonnet">Sonnet</option>
              <option value="haiku">Haiku</option>
            </select>
          </span>
          <label className="check-line">
            <input type="checkbox" checked={d.sensitiveOnly} onChange={(e) => set("sensitiveOnly", e.target.checked)} />
            Only checks of sensitive work
          </label>
        </div>
      ) : null}
      <p className="set-help">A cap lasts until you remove it.</p>
      <WriteError w={w} id={id} />
      <div className="toolbar">
        <button type="submit" className="primary sm" disabled={w.isBusy(id)}>
          <Busy on={w.isBusy(id)} />
          Save cap
        </button>
        <button type="button" className="sm link" onClick={onClose}>
          Cancel
        </button>
        <span className="spacer" />
        {editing ? (
          <button type="button" className="sm link" disabled={w.isBusy(id)} onClick={() => void remove()}>
            Remove this cap
          </button>
        ) : null}
      </div>
    </form>
  );
}

/** The caps on this account, each with Change, and Add a cap. */
export function CapControls({ ctl, a, rows }: { ctl: Controls; a: RouteAccount; rows: CapRow[] }) {
  const caps = a.cap_statuses ?? [];
  const [editing, setEditing] = useState<number | "new" | null>(null);
  const addBtn = useRef<HTMLButtonElement>(null);
  const changeBtns = useRef<Record<number, HTMLButtonElement | null>>({});
  const back: Back = () => addBtn.current;
  const close = (i: number | "new") => {
    setEditing(null);
    requestAnimationFrame(() => (i === "new" ? addBtn.current : changeBtns.current[i] ?? addBtn.current)?.focus());
  };
  return (
    <div className="ctl">
      {rows.length ? (
        rows.map((c, i) => (
          <div key={`${c.title}-${i}`}>
            <div className="sheet-item">
              <b>{c.title}</b>
              {c.now ? <span className="end">{c.now}</span> : null}
              <span className="end">{c.ends}</span>
              {caps[i] ? (
                <div className="toolbar">
                  <button ref={(el) => { changeBtns.current[i] = el; }} type="button" className="sm" aria-expanded={editing === i}
                    aria-label={`Change the cap: ${c.title}`} onClick={() => setEditing((v) => (v === i ? null : i))}>
                    Change…
                  </button>
                </div>
              ) : null}
            </div>
            {editing === i && caps[i] ? <CapForm a={a} cap={caps[i]} ctl={ctl} back={back} onClose={() => close(i)} /> : null}
          </div>
        ))
      ) : (
        <p className="meta">No caps set.</p>
      )}
      {editing === "new" ? (
        <CapForm a={a} cap={null} ctl={ctl} back={back} onClose={() => close("new")} />
      ) : (
        <div className="toolbar">
          <button ref={addBtn} type="button" className="sm" onClick={() => setEditing("new")}>
            Add a cap…
          </button>
        </div>
      )}
      <UndoBar section="caps" ctl={ctl} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Your words and your place in the priority
// ---------------------------------------------------------------------------

function WordsForm({ a, view, ctl, now, onClose }: { a: RouteAccount; view: RouteView; ctl: Controls; now: Date; onClose: () => void }) {
  const { w, eok, change } = ctl;
  const p = view.priority;
  const entries = p?.entries ?? [];
  const i = entries.findIndex((e) => e.account === a.key);
  const [role, setRole] = useState(entries[i]?.role ?? "");
  const [removing, setRemoving] = useState(false);
  const area = useRef<HTMLTextAreaElement>(null);
  useEffect(() => area.current?.focus(), []);
  const base = useId().replace(/:/g, "");
  const expect = p?.version ?? view.priority_version ?? null;
  // The list and version the draft started from, taken together. If the priority
  // moves while the form is open, the draft is never sent under the newer version
  // (that would overwrite whoever changed it); the owner reconciles first.
  const [started, setStarted] = useState({ entries, version: expect, until: p?.until ?? null });
  const moved = started.version !== expect;
  const id = `words:${a.key}`;
  const moveId = `move:${a.key}`;
  const removeId = `remove:${a.key}`;
  const save = (items: ReturnType<typeof withRole>, writeId: string, text: string, back?: Back) =>
    change({ section: "words", writeId, command: "khipu_route_priority",
      args: { action: "set", entries: items, until: p?.until ?? null, expect }, owner: "route.priority.set", text, attempt: eok.attempt, back });
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (moved) {
      w.fail(id, "Your priority changed after you opened this, so your draft was not saved.");
      return;
    }
    const ok = await change({ section: "words", writeId: id, command: "khipu_route_priority",
      args: { action: "set", entries: withRole(started.entries, a.key, role), until: started.until, expect: started.version },
      owner: "route.priority.set", text: `Your words for ${a.label} are saved.`, attempt: eok.attempt });
    if (ok) onClose();
  }
  const seeNewer = () => {
    setStarted({ entries, version: expect, until: p?.until ?? null });
    w.fail(id, null);
    area.current?.focus();
  };
  const move = (by: -1 | 1) => {
    const items = movedBy(entries, a.key, by);
    if (items) void save(items, moveId, `${a.label} moved ${by < 0 ? "up" : "down"} in your priority.`);
  };
  async function remove() {
    const rest = withoutAccount(entries, a.key);
    const ok = rest.length
      ? await save(rest, removeId, `${a.label} is out of your priority.`)
      : await change({ section: "words", writeId: removeId, command: "khipu_route_priority", args: { action: "clear", expect },
          owner: "route.priority.clear", text: "Your priority is cleared." });
    if (ok) onClose();
  }
  return (
    <form className="route-pause" aria-label={`Change your words for ${a.label}`} onSubmit={submit}
      onKeyDown={(e) => { if (!removing) escapeCloses(onClose)(e); }}>
      <div className="route-field wide">
        <label htmlFor={`${base}-words`}>Your words for {a.label}</label>
        <textarea id={`${base}-words`} ref={area} rows={5} value={role} aria-describedby={`${base}-count`} onChange={(e) => setRole(e.target.value)} />
        <span className="meta" id={`${base}-count`}>{charactersWords(role)}</span>
      </div>
      {moved ? (
        <p className="set-error" role="alert">
          Your priority changed after you opened this. Your draft is kept below.{" "}
          <button type="button" className="sm" onClick={seeNewer}>
            Use my draft on the newer version
          </button>
        </p>
      ) : null}
      <div className="toolbar">
        <span className="meta">Place in your priority: {i + 1} of {entries.length}</span>
        <button type="button" className="sm" disabled={i <= 0 || w.anyBusy} aria-label={`Move ${a.label} up`} onClick={() => move(-1)}>
          Move up
        </button>
        <button type="button" className="sm" disabled={i < 0 || i >= entries.length - 1 || w.anyBusy} aria-label={`Move ${a.label} down`} onClick={() => move(1)}>
          Move down
        </button>
      </div>
      <WriteError w={w} id={moveId} />
      <WriteError w={w} id={id} />
      {eok.ask ? (
        <EndOrKeepChoices view={view} ask={eok.ask} now={now} busy={w.anyBusy} saveLabel="Save" onCancel={() => eok.setAsk(null)} />
      ) : null}
      <div className="toolbar">
        <button type="submit" className="primary sm" disabled={w.isBusy(id)}>
          <Busy on={w.isBusy(id)} />
          Save
        </button>
        <button type="button" className="sm link" onClick={onClose}>
          Cancel
        </button>
        <span className="spacer" />
        <button type="button" className="sm link" aria-expanded={removing} disabled={w.anyBusy} onClick={() => (entries.length > 1 ? void remove() : setRemoving((v) => !v))}>
          Remove {a.label} from your priority
        </button>
      </div>
      {removing ? (
        <ConfirmGroup label="Clear your priority?" onClose={() => setRemoving(false)} back={() => area.current}>
          <b>Clear your priority?</b>
          <span>{a.label} is the only account in it. Clearing removes it with its words. Conversations hear "No priority set" on their next prompt.</span>
          <WriteError w={w} id={removeId} />
          <div className="toolbar">
            <button type="button" className="sm danger" disabled={w.isBusy(removeId)} onClick={() => void remove()}>
              <Busy on={w.isBusy(removeId)} />
              Clear priority
            </button>
            <button type="button" className="sm primary" onClick={() => setRemoving(false)}>
              Keep it
            </button>
          </div>
        </ConfirmGroup>
      ) : null}
    </form>
  );
}

/** The words and the place: Change… when the account is in the priority,
 *  Add to your priority when it is not. */
export function WordsControls({ ctl, a, view, now }: { ctl: Controls; a: RouteAccount; view: RouteView; now: Date }) {
  const { w, eok, change } = ctl;
  const p = view.priority;
  const entries = p?.entries ?? [];
  const inIt = entries.some((e) => e.account === a.key);
  const [editing, setEditing] = useState(false);
  const btn = useRef<HTMLButtonElement>(null);
  const addId = `prio-add:${a.key}`;
  const expect = p?.version ?? view.priority_version ?? null;
  if (p?.pending) {
    return (
      <div className="ctl">
        <p className="meta">A conversation changed your priority and it waits for you. Keep or undo it in Settings → Accounts and routing first.</p>
      </div>
    );
  }
  return (
    <div className="ctl">
      <div className="toolbar">
        {inIt ? (
          <button ref={btn} type="button" className="sm" aria-expanded={editing} onClick={() => setEditing((v) => !v)}>
            Change…
          </button>
        ) : (
          <button ref={btn} type="button" className="sm" aria-busy={w.isBusy(addId) || undefined} onClick={() => void change({
            section: "words", writeId: addId, command: "khipu_route_priority",
            args: { action: "set", entries: withAccountLast(entries, a.key), until: p?.until ?? null, expect },
            owner: "route.priority.set", text: `${a.label} is at the end of your priority.`, attempt: eok.attempt })}>
            <Busy on={w.isBusy(addId)} />
            Add to your priority
          </button>
        )}
      </div>
      <WriteError w={w} id={addId} />
      {editing && inIt ? (
        <WordsForm a={a} view={view} ctl={ctl} now={now} onClose={() => { setEditing(false); requestAnimationFrame(() => btn.current?.focus()); }} />
      ) : null}
      {!editing && eok.ask ? <EndOrKeepChoices view={view} ask={eok.ask} now={now} busy={w.anyBusy} saveLabel="Save" onCancel={() => eok.setAsk(null)} /> : null}
      <UndoBar section="words" ctl={ctl} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Plan detail: how Khipu reads this plan
// ---------------------------------------------------------------------------

/** Set up reading (a provider whose usage address isn't set) and Replace the
 *  key (a stored key Khipu can store from the app). */
export function ReadingControls({ account, row, view, onChanged }: {
  account: RouteAccount | null;
  row: UsageAccount;
  view: RouteView | null | undefined;
  onChanged?: () => Promise<unknown> | void;
}) {
  const w = useWriter(async () => {
    await onChanged?.();
  });
  const provider = account ? view?.definitions?.providers?.[account.harness] : undefined;
  // A quota host is the generic HTTP reader's missing piece. DeepSeek has its own
  // reader that ignores a host and needs its key, so its guidance stays as the CLI
  // words it; a provider already marked http-quota is offered the host it lacks.
  const hostless = provider?.probe !== "http-quota" || !provider.host;
  const needsReader = !!account && account.configured !== false && !!account.usage_setup && !!provider
    && account.harness !== "deepseek" && hostless;
  const canReplaceKey = row.harness === "glm" && (row.error === "no_key" || row.error === "key_rejected");
  if (!needsReader && !canReplaceKey) return null;
  return (
    <div className="ctl">
      {needsReader && account && provider ? <SetUpReading w={w} account={account} provider={provider as Record<string, unknown>} /> : null}
      {canReplaceKey ? <ReplaceKey row={row} onChanged={onChanged} /> : null}
    </div>
  );
}

function SetUpReading({ w, account, provider }: { w: Writer; account: RouteAccount; provider: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  const [host, setHost] = useState("");
  const [asking, setAsking] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const base = useId().replace(/:/g, "");
  const btn = useRef<HTMLButtonElement>(null);
  const id = `reader:${account.key}`;
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { if (open) input.current?.focus(); }, [open]);
  const name = account.label;
  async function save(h: string) {
    // The provider as the record holds it, with only the reader added.
    const ok = await w.run(id, "khipu_route_definition", { definition: "provider", value: { ...provider, probe: "http-quota", host: h } }, null, () => btn.current);
    if (ok) {
      setSaved(`Saved. Khipu will read ${name}'s plan at ${h} on its next check.`);
      setAsking(null);
      setOpen(false);
    }
  }
  return (
    <div>
      <p className="meta">{account.usage_setup ? "Khipu has no way to read this plan's usage yet." : ""}</p>
      {saved ? <p className="meta" role="status">{saved}</p> : null}
      {!open ? (
        <button ref={btn} type="button" className="sm" onClick={() => { setSaved(null); setOpen(true); }}>
          Set up reading…
        </button>
      ) : (
        <form className="route-pause" aria-label={`Set up reading for ${name}`} onKeyDown={(e) => { if (!asking) escapeCloses(() => { setOpen(false); })(e); }}
          onSubmit={(e) => {
            e.preventDefault();
            const out = checkedHost(host);
            if ("error" in out) {
              w.fail(id, out.error);
              return;
            }
            w.fail(id, null);
            setAsking(out.host);
          }}>
          <div className="toolbar">
            <span className="route-field">
              <label htmlFor={`${base}-host`}>Where Khipu asks for usage</label>
              <input id={`${base}-host`} ref={input} value={host} autoComplete="off" placeholder="api.example.com" onChange={(e) => setHost(e.target.value)} />
            </span>
          </div>
          <p className="set-help">
            The address from the provider's docs, for example the host of its usage or quota page. Khipu sends your key only to this address, over HTTPS.
          </p>
          <WriteError w={w} id={id} />
          {asking ? (
            <ConfirmGroup label="Send your key to this address?" onClose={() => setAsking(null)} back={() => input.current}>
              <b>Send your {name} key to {asking}?</b>
              <span>Khipu sends it only there, over HTTPS, to read your usage.</span>
              <div className="toolbar">
                <button type="button" className="sm primary" onClick={() => setAsking(null)}>
                  Cancel
                </button>
                <button type="button" className="sm" disabled={w.isBusy(id)} onClick={() => void save(asking)}>
                  <Busy on={w.isBusy(id)} />
                  Use this address
                </button>
              </div>
            </ConfirmGroup>
          ) : (
            <div className="toolbar">
              <button type="submit" className="primary sm">Continue</button>
              <button type="button" className="sm link" onClick={() => setOpen(false)}>Cancel</button>
            </div>
          )}
        </form>
      )}
    </div>
  );
}

/** The key goes to the CLI over stdin (`set_khipu_secret`), never into argv
 *  or the page, and the field is emptied as soon as it is sent. */
function ReplaceKey({ row, onChanged }: { row: UsageAccount; onChanged?: () => Promise<unknown> | void }) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const keyInput = useRef<HTMLInputElement>(null);
  const base = useId().replace(/:/g, "");
  const name = row.account || "GLM";
  async function send() {
    setBusy(true);
    setError(null);
    // The value leaves the page's state the moment it is handed over, not when
    // the write and the re-read have both finished.
    const handed = value;
    setValue("");
    setAsking(false);
    try {
      const raw = await invoke<string>("set_khipu_secret", { account: "glm_api_key", value: handed });
      let failed: string | null = null;
      try {
        const parsed = JSON.parse(raw) as { ok?: boolean; error?: string };
        if (parsed && parsed.ok === false) failed = parsed.error ?? "Khipu couldn't store the key.";
      } catch {
        // A plain "ok" line is a success.
      }
      if (failed) setError(failed);
      else {
        setSaved(true);
        setOpen(false);
        await onChanged?.();
      }
    } catch (e) {
      setError(typeof e === "string" ? e : "Khipu couldn't store the key.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div>
      {saved ? <p className="meta" role="status">The key is replaced. Khipu reads the plan again with it.</p> : null}
      {!open ? (
        <button type="button" className="sm" onClick={() => { setSaved(false); setOpen(true); }}>
          Replace the key…
        </button>
      ) : (
        <form className="route-pause" aria-label={`Replace the ${name} key`}
          onKeyDown={(e) => { if (!asking) escapeCloses(() => { setOpen(false); setValue(""); })(e); }}
          onSubmit={(e) => { e.preventDefault(); if (value.trim()) setAsking(true); }}>
          <div className="toolbar">
            <span className="route-field">
              <label htmlFor={`${base}-key`}>New {name} key</label>
              <input id={`${base}-key`} ref={keyInput} type="password" autoComplete="off" spellCheck={false} value={value} onChange={(e) => setValue(e.target.value)} />
            </span>
          </div>
          <p className="set-help">Stored in this Mac's Keychain. Khipu never shows it to a model or prints it.</p>
          {error ? <p className="set-error" role="alert">{error}</p> : null}
          {asking ? (
            <ConfirmGroup label={`Replace the ${name} key?`} onClose={() => setAsking(false)} back={() => keyInput.current}>
              <b>Replace the {name} key?</b>
              <span>The old one is removed from the Keychain.</span>
              <div className="toolbar">
                <button type="button" className="sm primary" onClick={() => setAsking(false)}>Keep the old key</button>
                <button type="button" className="sm" disabled={busy} onClick={() => void send()}>
                  <Busy on={busy} />
                  Replace the key
                </button>
              </div>
            </ConfirmGroup>
          ) : (
            <div className="toolbar">
              <button type="submit" className="primary sm" disabled={!value.trim()}>Continue</button>
              <button type="button" className="sm link" onClick={() => { setOpen(false); setValue(""); }}>Cancel</button>
            </div>
          )}
        </form>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Settings → Accounts: a conversation changed the list of accounts
// ---------------------------------------------------------------------------

type AccountsRequest = { request?: string | null; by?: unknown; at?: string | null };

function requestBy(by: unknown): string {
  if (by && typeof by === "object") {
    const o = by as { label?: unknown; name?: unknown; harness?: unknown };
    for (const v of [o.label, o.name, o.harness]) if (typeof v === "string" && v) return v;
  }
  return typeof by === "string" && by ? by : "a conversation";
}

/** Keep or Undo for the change a conversation made to the accounts Khipu
 *  reads (`accounts request confirm|undo`, answered against the version the
 *  owner saw). */
export function AccountsRequestBand({ support, reload, onChanged }: {
  support: Support;
  reload: () => Promise<unknown>;
  onChanged?: () => void;
}) {
  const [busy, setBusy] = useState<"confirm" | "undo" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const status = support.state === "here" ? support.status : null;
  const request = (status?.request ?? null) as AccountsRequest | null;
  if (!status || !request || typeof request !== "object") return null;
  const answer = async (action: "confirm" | "undo") => {
    setBusy(action);
    setError(null);
    try {
      const res = await callCli("khipu_accounts_request", { action, expect: status.version });
      if (!res.ok) setError(plainError(res.error));
      await reload();
      if (res.ok) onChanged?.();
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="route-disagree" role="group" aria-label="A conversation changed your accounts" tabIndex={-1}>
      <span>
        <b>A conversation changed your accounts.</b> {requestBy(request.by)} changed which accounts Khipu reads at your request, and you haven't answered yet.
      </span>
      {request.request ? <span>What it said you asked: “{request.request}”</span> : null}
      <div className="toolbar">
        <button type="button" className="sm primary" disabled={busy != null} onClick={() => void answer("confirm")}>
          <Busy on={busy === "confirm"} />
          Keep
        </button>
        <button type="button" className="sm" disabled={busy != null} onClick={() => void answer("undo")}>
          <Busy on={busy === "undo"} />
          Undo
        </button>
      </div>
      {error ? <p className="set-error" role="alert">Khipu didn't make that change: {error}</p> : null}
    </div>
  );
}
