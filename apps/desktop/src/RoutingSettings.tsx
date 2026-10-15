/* Settings -> Routing (docs/plans/2026-10-14-routing-truth.md, phase 4,
   second half; mock docs/mockups/2026-10-14-routing/settings-routing.html).
   Every option phases 1a to 3c built, each usable here and each limit
   raisable or removable on the spot: the accounts (Mark paused until, Lift,
   Clear, Dismiss one report), the owner's note, the standing rules, holds,
   telling conversations, job runner folders, which account each job lane
   uses, the routing table file and recent changes.

   Khipu reports; it never routes. Nothing here sends, stops or reorders
   work. Every write goes through one fixed-argv Tauri command that passes
   `--from-app` (src-tauri/src/lib.rs, `route_*_argv`) or `config --set`,
   then the page re-reads the record: what it shows is what the CLI reports.
   The switch before handing work to another agent (phase 6) and the copy for
   cloud agents (phase 7) join with their phases. */

import { useEffect, useId, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent, ReactNode, RefObject } from "react";
import { Loader2 } from "lucide-react";
import { EmptyState, Tag } from "./ui";
import { callCli, useCliState } from "./settingsCli";
import type { FeaturesShape } from "./settingsCli";
import type { UsageReport } from "./PlansCard";
import { inPlansOrder } from "./RoutingCard";
import {
  accountSource,
  accountState,
  capitalise,
  disagreementWords,
  holdWords,
  modelDisagreementWords,
  paceWords,
  pastWords,
  priorityPendingWords,
  ruleResultLines,
  saidWhen,
  useRouting,
  whenLocal,
} from "./routing";
import type {
  PlanFact,
  RouteAccount,
  RouteHold,
  RouteModel,
  RoutePriority,
  RoutePriorityEntry,
  RouteReport,
  RouteRule,
  RouteRunner,
  RouteView,
  RuleResult,
} from "./routing";
import {
  agoSpan,
  changeDays,
  chosenEnd,
  endChoices,
  holdKind,
  limitWords,
  ruleOrigin,
  settingWords,
  stateSaid,
  tableErrorWords,
} from "./routingWords";
import type { LimitUnit } from "./routingWords";

type RouteSetting = { value: unknown; default: unknown; kind: string; source: string };
type RouteConfig = { routing?: Record<string, RouteSetting> };

/** Where focus goes when the control that made a change is gone. */
type Back = () => HTMLElement | null | undefined;

/** The most words the app hands the CLI in one change, in KB of text: the
 *  size of one argument, not a limit the owner chose (src-tauri lib.rs,
 *  ROUTE_WORDS_MAX_BYTES; fix round 2, R2-4). */
const WORDS_MOST_KB = 128;

/** A write's arguments, or a function that builds them from state read just
 *  before the write runs (a string is a refusal in words: nothing is sent). */
type WriteArgs = Record<string, unknown> | undefined | (() => Promise<Record<string, unknown> | string>);

type Writer = {
  /** Whether a write under `id` is running or waiting its turn. */
  isBusy: (id: string) => boolean;
  /** Whether any write is running or waiting. */
  anyBusy: boolean;
  errors: Record<string, string>;
  results: Record<string, string>;
  /**
   * Run one write, re-read, and keep the CLI's error under `id`. Writes run
   * one at a time, in the order they were asked for, so a second change
   * made before the first one's re-read builds on the first (Codex review,
   * finding 1). Focus stays on the control that made the change; when the
   * change removes that control (Clear, Lift, Dismiss, End now, Undo),
   * focus goes to `back()`.
   */
  run: (
    id: string,
    command: string,
    args?: WriteArgs,
    after?: (() => Promise<unknown>) | null,
    back?: Back,
  ) => Promise<boolean>;
  fail: (id: string, message: string | null) => void;
};

function useWriter(reread: () => Promise<unknown>): Writer {
  // The writes running or waiting, oldest first.
  const [pending, setPending] = useState<string[]>([]);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [results, setResults] = useState<Record<string, string>>({});
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  // The control focused when a write began, and where focus goes if the
  // write takes that control away (fix round 2, R2-2). Settled after the
  // render that ends the write, the re-read's changes included.
  const keep = useRef<{ from: HTMLElement | null; back?: Back; done: boolean } | null>(null);
  useEffect(() => {
    const k = keep.current;
    if (!k || !k.done) return;
    keep.current = null;
    const at = document.activeElement;
    // Focus that moved on to another control stays where it is.
    if (at && at !== document.body && (at as HTMLElement).isConnected && at !== k.from) return;
    const usable = (el: HTMLElement | null | undefined): el is HTMLElement =>
      !!el && el.isConnected && !(el as HTMLButtonElement).disabled;
    const to = usable(k.from) ? k.from : k.back?.();
    if (usable(to) && to !== at) to.focus();
  });
  const fail = (id: string, message: string | null) =>
    setErrors((prev) => {
      const out = { ...prev };
      if (message) out[id] = message;
      else delete out[id];
      return out;
    });
  const run: Writer["run"] = (id, command, args, after, back) => {
    const at = document.activeElement;
    const from = at instanceof HTMLElement && at !== document.body ? at : null;
    setPending((p) => [...p, id]);
    const job = queue.current.then(async () => {
      const k = { from, back, done: false };
      keep.current = k;
      let ok = false;
      try {
        const built = typeof args === "function" ? await args() : args;
        if (typeof built === "string") {
          fail(id, built);
        } else {
          const res = await callCli(command, built);
          await (after ?? reread)();
          fail(id, res.ok ? null : res.error);
          setResults((previous) => {
            const next = { ...previous };
            if (!res.ok) delete next[id];
            else {
              const data = res.data as { reason?: unknown };
              next[id] = typeof data?.reason === "string" && data.reason ? data.reason : "Checked.";
            }
            return next;
          });
          ok = res.ok;
        }
      } finally {
        if (keep.current === k) k.done = true;
        setPending((p) => {
          const i = p.indexOf(id);
          return i < 0 ? p : [...p.slice(0, i), ...p.slice(i + 1)];
        });
      }
      return ok;
    });
    queue.current = job.catch(() => undefined);
    return job;
  };
  return { isBusy: (id) => pending.includes(id), anyBusy: pending.length > 0, errors, results, run, fail };
}

/** One routing setting's value as `khipu config` reports it now, read just
 *  before a write that edits it, or why it couldn't be read. */
async function currentSetting(key: string): Promise<{ value: unknown } | string> {
  const res = await callCli<RouteConfig>("khipu_config_show");
  if (!res.ok) return `Khipu couldn't read the setting before changing it: ${res.error}`;
  return { value: res.data.routing?.[key]?.value };
}

/** `say` puts a refusal in the control's own words, as a setting's name
 *  for its internal key. */
function WriteError({ w, id, say }: { w: Writer; id: string; say?: (msg: string) => string }) {
  const msg = w.errors[id];
  if (!msg) return null;
  return (
    <p className="set-error" role="alert">
      Khipu didn't make that change: {say ? say(msg) : msg}
    </p>
  );
}


function Busy({ on }: { on: boolean }) {
  return on ? <Loader2 size={14} className="spin" aria-hidden /> : null;
}

/** Escape cancels an inline form and gives focus back to its button. */
function escapeCloses(close: () => void) {
  return (e: KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      close();
    }
  };
}

/**
 * An inline question under the row it changes. Focus moves into it when it
 * opens, so Escape and Tab work from there; when it closes (answered,
 * cancelled or Escape) and focus would be lost with it, focus goes to
 * `back()`, the button that opened it or what took its place (fix round 1, F5).
 */
function ConfirmGroup({
  label,
  onClose,
  back,
  children,
}: {
  label: string;
  onClose: () => void;
  back: () => HTMLElement | null | undefined;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const backRef = useRef(back);
  backRef.current = back;
  useEffect(() => {
    ref.current?.focus();
    return () => {
      requestAnimationFrame(() => {
        const at = document.activeElement;
        if (at && at !== document.body) return;
        const to = backRef.current();
        if (to?.isConnected) to.focus();
      });
    };
  }, []);
  return (
    <div ref={ref} tabIndex={-1} className="route-confirm" role="group" aria-label={label} onKeyDown={escapeCloses(onClose)}>
      {children}
    </div>
  );
}

function useFocusFirst<T extends HTMLElement>(): RefObject<T | null> {
  const ref = useRef<T>(null);
  useEffect(() => {
    ref.current?.focus();
  }, []);
  return ref;
}

// ---------------------------------------------------------------------------
// When something ends: the pause form and the hold form share these choices.
// ---------------------------------------------------------------------------

function EndFields({
  idBase,
  label,
  firstHours,
  choice,
  setChoice,
  custom,
  setCustom,
  selectRef,
}: {
  idBase: string;
  label: string;
  firstHours: number;
  choice: string;
  setChoice: (v: string) => void;
  custom: string;
  setCustom: (v: string) => void;
  selectRef?: RefObject<HTMLSelectElement | null>;
}) {
  const choices = endChoices(new Date(), firstHours);
  return (
    <span className="route-field">
      <label htmlFor={`${idBase}-until`}>{label}</label>
      <select id={`${idBase}-until`} ref={selectRef} value={choice} onChange={(e) => setChoice(e.target.value)}>
        {choices.map((c) => (
          <option key={c.id} value={c.id}>
            {c.label}
          </option>
        ))}
      </select>
      {choice === "custom" ? (
        <>
          <label className="sr-only" htmlFor={`${idBase}-at`}>
            Date and time it ends
          </label>
          <input
            id={`${idBase}-at`}
            type="datetime-local"
            value={custom}
            onChange={(e) => setCustom(e.target.value)}
          />
        </>
      ) : null}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Accounts
// ---------------------------------------------------------------------------

function PauseForm({
  a,
  formId,
  w,
  onClose,
}: {
  a: RouteAccount;
  formId: string;
  w: Writer;
  onClose: () => void;
}) {
  const [choice, setChoice] = useState("tomorrow");
  const [custom, setCustom] = useState("");
  const selectRef = useFocusFirst<HTMLSelectElement>();
  const id = `pause:${a.key}`;
  async function submit(e: FormEvent) {
    e.preventDefault();
    const now = new Date();
    const end = chosenEnd(choice, custom, endChoices(now, 1), now);
    if ("error" in end) {
      w.fail(id, end.error);
      return;
    }
    const ok = await w.run(id, "khipu_route_pause", { account: a.key, until: end.iso });
    if (ok) onClose();
  }
  return (
    <form className="route-pause" id={formId} aria-label={`Mark ${a.label} paused`} onSubmit={submit} onKeyDown={escapeCloses(onClose)}>
      <div className="toolbar">
        <EndFields
          idBase={formId}
          label={`Mark ${a.label} paused until`}
          firstHours={1}
          choice={choice}
          setChoice={setChoice}
          custom={custom}
          setCustom={setCustom}
          selectRef={selectRef}
        />
      </div>
      <p className="set-help">
        This doesn't stop anything that is running; it tells conversations you want {a.label} left alone. They hear
        it on their next prompt, or when they ask.
      </p>
      <WriteError w={w} id={id} />
      <div className="toolbar">
        <button type="submit" className="primary sm" disabled={w.isBusy(id)}>
          <Busy on={w.isBusy(id)} />
          Mark {a.label} paused
        </button>
        <button type="button" className="sm link" onClick={onClose}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function clearable(a: RouteAccount, now: Date): boolean {
  if (a.state === "limited" || a.state === "down") return true;
  return (a.models ?? []).some((m) => m.until && new Date(m.until).getTime() > now.getTime());
}

/** One side of a disagreement as the panel lists it. */
function sideWords(state: string | undefined, by: string, at: string | null | undefined, now: Date): ReactNode {
  return (
    <>
      <b>{capitalise(stateSaid(state))}</b>: {by}
      {at ? ` ${saidWhen(at, now)}` : ""}.
    </>
  );
}

/** Whose report a Dismiss button removes, by its source, as the mock words it. */
const DISMISS_WHO: Record<string, string> = {
  report: "conversation's",
  runner: "runner's",
  probe: "plan check's",
  check: "reset check's",
};

function DisagreePanel({ a, w, now, back }: { a: RouteAccount; w: Writer; now: Date; back: Back }) {
  const sides: { d: RouteReport; model?: RouteModel; sentence: string }[] = [];
  const own = disagreementWords(a, now);
  if (a.disagreement && own) sides.push({ d: a.disagreement, sentence: own });
  const modelWords = modelDisagreementWords(a, now);
  (a.models ?? [])
    .filter((m) => m.disagreement && m.until && new Date(m.until).getTime() > now.getTime())
    .forEach((m, i) => sides.push({ d: m.disagreement!, model: m, sentence: modelWords[i] ?? "" }));
  if (!sides.length) return null;
  const said = accountSource(a, now).split(" · ")[0];
  const shownBy = said ? said.charAt(0).toLowerCase() + said.slice(1) : "the stronger report";
  return (
    <>
      {sides.map(({ d, model: m, sentence }) => {
        const model = m?.model;
        // The side that shows: the account's own report, or for a model the
        // report behind that model's limit (Codex review, finding 3).
        const shown = m
          ? { id: m.id, source: m.source, cleared: false, state: "limited" }
          : { ...(a.witness ?? {}), state: a.state };
        // Either side can be dismissed, a job runner's limit too (F6); an
        // owner's own clear is not a report.
        const sidesOf = [
          ...(shown.id && shown.source && shown.source !== "owner" && !shown.cleared
            ? [{ id: shown.id, state: shown.state, source: shown.source }]
            : []),
          ...(d.id && d.source && d.source !== "owner" ? [{ id: d.id, state: d.state ?? "", source: d.source }] : []),
        ];
        const named = sidesOf.map((x) => `Dismiss the ${DISMISS_WHO[x.source] ?? "other"} report`);
        const dismissible = sidesOf.map((x, i) => ({
          ...x,
          // Two sides from the same kind of source are told apart by what they say.
          label: named.indexOf(named[i]) !== named.lastIndexOf(named[i])
            ? `Dismiss the ${DISMISS_WHO[x.source] ?? "other"} “${stateSaid(x.state)}” report`
            : named[i],
        }));
        return (
          <div
            key={d.id ?? model ?? "account"}
            className="route-disagree"
            role="group"
            aria-label={`${a.label}${model ? ` (${model})` : ""}: two reports disagree`}
          >
            <span>
              <b>Two reports disagree{model ? ` about ${model}` : ""}.</b>
            </span>
            <ul>
              {m ? (
                <li>{sideWords("limited", `the ${m.model} limit, reported by ${m.by || "another source"}`, m.at, now)}</li>
              ) : (
                <li>{sideWords(a.state, shownBy, null, now)}</li>
              )}
              <li>{sideWords(d.state, `reported by ${d.by || "another source"}`, d.at, now)}</li>
            </ul>
            <span>
              {sentence} Dismiss removes only that report; Clear ends the shown limit or down report.
            </span>
            {dismissible.length ? (
              <div className="toolbar">
                {dismissible.map((s) => {
                  const id = `dismiss:${s.id}`;
                  return (
                    <button
                      key={s.id}
                      type="button"
                      className="sm"
                      disabled={w.isBusy(id)}
                      onClick={() => void w.run(id, "khipu_route_clear", { account: a.key, report: s.id }, null, back)}
                    >
                      <Busy on={w.isBusy(id)} />
                      {s.label}
                    </button>
                  );
                })}
              </div>
            ) : null}
            {dismissible.map((s) => (
              <WriteError key={s.id} w={w} id={`dismiss:${s.id}`} />
            ))}
          </div>
        );
      })}
    </>
  );
}

function AccountRow({
  a,
  w,
  now,
  reportDefault,
  fact,
}: {
  a: RouteAccount;
  w: Writer;
  now: Date;
  reportDefault?: number;
  fact?: PlanFact;
}) {
  const [pausing, setPausing] = useState(false);
  const pauseBtn = useRef<HTMLButtonElement>(null);
  const formId = `pause-${useId().replace(/:/g, "")}`;
  const st = accountState(a, now);
  const src = accountSource(a, now, null, reportDefault);
  const close = () => {
    setPausing(false);
    requestAnimationFrame(() => pauseBtn.current?.focus());
  };
  const paused = a.state === "paused";
  const liftId = `lift:${a.key}`;
  const clearId = `clear:${a.key}`;
  return (
    <div className="row-item route-acct" role="listitem">
      <span className="route-name">{a.label}</span>
      <span className="route-what">
        <span className="route-state">
          <span className={st.tone ? `hdot ${st.tone}` : "hdot"} aria-hidden />
          <span>{st.text}</span>
          {a.disagreement ? (
            <Tag tone="warn" dot>
              Sources disagree
            </Tag>
          ) : null}
        </span>
        {src ? <span className="route-src">{src}</span> : null}
        {a.next_check ? <span className="route-src">Khipu checks again at {whenLocal(a.next_check, now)}.</span> : null}
        {fact && a.configured !== false ? <span className="route-src">{paceWords(fact, now)}</span> : null}
      </span>
      <span className="route-acts">
        {a.checkable !== false && a.configured !== false && ["limited", "probably_open", "unknown"].includes(a.state) ? (
          <>
            <button type="button" className="sm" disabled={w.anyBusy}
              onClick={() => void w.run(`check:${a.key}`, "khipu_route_check", { account: a.key })}>
              <Busy on={w.isBusy(`check:${a.key}`)} />
              Check now
            </button>
            {w.results[`check:${a.key}`] ? <span className="route-src" role="status">{w.results[`check:${a.key}`]}</span> : null}
          </>
        ) : null}
        <button
          ref={pauseBtn}
          type="button"
          className="sm"
          aria-expanded={pausing}
          aria-controls={pausing ? formId : undefined}
          aria-label={paused ? `Change the pause for ${a.label}` : `Mark ${a.label} paused until…`}
          onClick={() => setPausing((v) => !v)}
        >
          {paused ? "Change…" : "Mark paused until…"}
        </button>
        {paused ? (
          <button
            type="button"
            className="sm"
            aria-label={`Lift the pause for ${a.label}`}
            disabled={w.isBusy(liftId)}
            onClick={() => void w.run(liftId, "khipu_route_lift", { account: a.key }, null, () => pauseBtn.current)}
          >
            <Busy on={w.isBusy(liftId)} />
            Lift the pause
          </button>
        ) : null}
        {!paused && clearable(a, now) ? (
          <button
            type="button"
            className="sm"
            aria-label={`Clear ${a.state === "down" ? "the down report" : "the limit"} for ${a.label}`}
            disabled={w.isBusy(clearId)}
            onClick={() => void w.run(clearId, "khipu_route_clear", { account: a.key }, null, () => pauseBtn.current)}
          >
            <Busy on={w.isBusy(clearId)} />
            Clear
          </button>
        ) : null}
      </span>
      {pausing ? <PauseForm a={a} formId={formId} w={w} onClose={close} /> : null}
      <DisagreePanel a={a} w={w} now={now} back={() => pauseBtn.current} />
      {w.errors[`check:${a.key}`] || w.errors[liftId] || w.errors[clearId] ? (
        <div className="route-pause" role="group" aria-label={`${a.label}: the change failed`}>
          <WriteError w={w} id={`check:${a.key}`} />
          <WriteError w={w} id={liftId} />
          <WriteError w={w} id={clearId} />
        </div>
      ) : null}
    </div>
  );
}

function ResetChecksSection({ data, cfg, cfgError, w, reread }: { data: RouteView | null; cfg: RouteConfig | null; cfgError?: string | null; w: Writer; reread: () => Promise<unknown> }) {
  const enabled = cfg?.routing?.routing_reset_check?.value === true;
  const next = (data?.accounts ?? []).filter((a) => a.next_check).map((a) => `${a.label}: ${whenLocal(a.next_check!, new Date())}`);
  return (
    <div className="section-card">
      <div className="section-head">Checking for resets</div>
      <div className="section-body">
        <SwitchRow id="route-reset-check" label="Check an account at its reset time"
          what="Khipu reads only an account's usage. It never sends a model turn."
          tag={<Tag>{enabled ? "On" : "Off"}</Tag>} checked={enabled} disabled={!cfg || w.anyBusy}
          busy={w.isBusy("reset-check")}
          read={switchRead(cfg, cfgError, reread)}
          onChange={(on) => void w.run("reset-check", "khipu_config_set", { key: "routing_reset_check", value: on ? "on" : "off" }, reread)}
          error={<WriteError w={w} id="reset-check" />} />
        <div className="rows">
          <LimitRow name="Wait after the reset time" help="Khipu checks after this wait." fields={[{ key: "routing_reset_check_wait", label: "Wait", unit: "time", options: [60, 120, 300] }]} cfg={cfg} w={w} reread={reread} />
          <RetryTimesRow cfg={cfg} w={w} reread={reread} />
          <LimitRow name="Wait while the Mac is busy" help="If load is over this number, Khipu waits before checking." fields={[{ key: "routing_busy_load", label: "Load", unit: "load", options: [50, 75, 90, 0], zero: "Never wait" }, { key: "routing_busy_wait", label: "Wait", unit: "time", options: [60, 300, 600, 0], zero: "Try again at the next look" }, { key: "routing_busy_tries", label: "Busy attempts", unit: "attempts", options: [1, 3, 5, 0], zero: "None: record it at once" }]} cfg={cfg} w={w} reread={reread} />
        </div>
        <p className="set-help">{next.length ? `Khipu checks again at ${next.join("; ")}.` : "No reset checks are scheduled."}</p>
        <p className="set-help">{(data?.accounts ?? []).filter((a) => a.reset_check?.reason).map((a) => `${a.label}: ${a.reset_check?.reason}`).join("; ") || "No account has a reset-check reason."}</p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Your priority (phase 4b): your order of accounts, each with your words for
// its role and an optional end. Every change sends the whole list in the
// order shown; Khipu keeps it as sent and never reorders it.
// ---------------------------------------------------------------------------

type PriorityItem = { account: string; role: string | null; until: string | null };

/** The list as the CLI takes it back: account key, role and end only. */
function priorityItems(entries: RoutePriorityEntry[]): PriorityItem[] {
  return entries.map((e) => ({ account: e.account, role: e.role ?? null, until: e.until ?? null }));
}

/** "until Sat 3:00 PM", or "no end". */
function endText(until: string | null | undefined, now: Date): string {
  return until ? `until ${whenLocal(until, now)}` : "no end";
}

/** The end choices of a priority form: keep the end it has, the usual ends,
 *  a date and time, or none. */
function PriorityEndField({
  idBase,
  label,
  current,
  choice,
  setChoice,
  custom,
  setCustom,
  now,
}: {
  idBase: string;
  label: string;
  current: string | null;
  choice: string;
  setChoice: (v: string) => void;
  custom: string;
  setCustom: (v: string) => void;
  now: Date;
}) {
  const choices = endChoices(now, 24).map((c) => (c.id === "never" ? { ...c, label: "No end" } : c));
  return (
    <span className="route-field">
      <label htmlFor={`${idBase}-until`}>{label}</label>
      <select id={`${idBase}-until`} value={choice} onChange={(e) => setChoice(e.target.value)}>
        {current ? <option value="keep">Keep: until {whenLocal(current, now)}</option> : null}
        {choices.map((c) => (
          <option key={c.id} value={c.id}>
            {c.label}
          </option>
        ))}
      </select>
      {choice === "custom" ? (
        <>
          <label className="sr-only" htmlFor={`${idBase}-at`}>
            Date and time it ends
          </label>
          <input id={`${idBase}-at`} type="datetime-local" value={custom} onChange={(e) => setCustom(e.target.value)} />
        </>
      ) : null}
    </span>
  );
}

function pickedEnd(choice: string, custom: string, current: string | null, now: Date): { iso: string | null } | { error: string } {
  if (choice === "keep") return { iso: current };
  return chosenEnd(choice, custom, endChoices(now, 24), now);
}

function PriorityEntryForm({
  p,
  i,
  w,
  onClose,
}: {
  p: RoutePriority;
  i: number;
  w: Writer;
  onClose: () => void;
}) {
  const e = p.entries[i];
  const [role, setRole] = useState(e.role ?? "");
  const [choice, setChoice] = useState(e.until ? "keep" : "never");
  const [custom, setCustom] = useState("");
  const roleRef = useFocusFirst<HTMLInputElement>();
  const formId = `prio-${useId().replace(/:/g, "")}`;
  const id = `priority:edit:${e.account}`;
  async function submit(ev: FormEvent) {
    ev.preventDefault();
    const now = new Date();
    const end = pickedEnd(choice, custom, e.until ?? null, now);
    if ("error" in end) {
      w.fail(id, end.error);
      return;
    }
    const next = priorityItems(p.entries);
    next[i] = { account: e.account, role: role.trim() || null, until: end.iso };
    if (await w.run(id, "khipu_route_priority", { action: "set", entries: next, until: p.until ?? null })) onClose();
  }
  return (
    <form className="route-pause" id={formId} aria-label={`Change ${e.label} in your priority`} onSubmit={submit} onKeyDown={escapeCloses(onClose)}>
      <div className="toolbar">
        <span className="route-field">
          <label htmlFor={`${formId}-role`}>Its role, in your words</label>
          <input id={`${formId}-role`} ref={roleRef} type="text" value={role} placeholder="all Claude work first"
            onChange={(ev) => setRole(ev.target.value)} />
        </span>
        <PriorityEndField idBase={formId} label={`${e.label} keeps its place`} current={e.until ?? null} choice={choice}
          setChoice={setChoice} custom={custom} setCustom={setCustom} now={new Date()} />
      </div>
      <WriteError w={w} id={id} />
      <div className="toolbar">
        <button type="submit" className="primary sm" disabled={w.isBusy(id)}>
          <Busy on={w.isBusy(id)} />
          Save
        </button>
        <button type="button" className="sm link" onClick={onClose}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function PriorityEndForm({ p, w, onClose }: { p: RoutePriority; w: Writer; onClose: () => void }) {
  const [choice, setChoice] = useState(p.until ? "keep" : "never");
  const [custom, setCustom] = useState("");
  const formId = `prio-end-${useId().replace(/:/g, "")}`;
  const id = "priority:end";
  async function submit(ev: FormEvent) {
    ev.preventDefault();
    const end = pickedEnd(choice, custom, p.until ?? null, new Date());
    if ("error" in end) {
      w.fail(id, end.error);
      return;
    }
    if (await w.run(id, "khipu_route_priority", { action: "set", entries: priorityItems(p.entries), until: end.iso })) onClose();
  }
  return (
    <form className="route-pause" aria-label="When your whole priority ends" onSubmit={submit} onKeyDown={escapeCloses(onClose)}>
      <div className="toolbar">
        <PriorityEndField idBase={formId} label="Your whole priority ends" current={p.until ?? null} choice={choice}
          setChoice={setChoice} custom={custom} setCustom={setCustom} now={new Date()} />
      </div>
      <WriteError w={w} id={id} />
      <div className="toolbar">
        <button type="submit" className="primary sm" disabled={w.isBusy(id)}>
          <Busy on={w.isBusy(id)} />
          Save
        </button>
        <button type="button" className="sm link" onClick={onClose}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function PrioritySection({
  data,
  cfg,
  w,
  now,
  reread,
}: {
  data: RouteView;
  cfg: RouteConfig | null;
  w: Writer;
  now: Date;
  reread: () => Promise<unknown>;
}) {
  const p = data.priority ?? null;
  const entries = p?.entries ?? [];
  const [editing, setEditing] = useState<number | null>(null);
  const [endOpen, setEndOpen] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [adding, setAdding] = useState("");
  const addId = useId();
  const addSelect = useRef<HTMLSelectElement>(null);
  const candidates = (data.accounts ?? []).filter((a) => a.configured !== false && !entries.some((e) => e.account === a.key));
  const choice = candidates.some((a) => a.key === adding) ? adding : candidates[0]?.key ?? "";
  const send = (id: string, next: PriorityItem[], back?: Back) =>
    next.length
      ? w.run(id, "khipu_route_priority", { action: "set", entries: next, until: p?.until ?? null }, null, back)
      : w.run(id, "khipu_route_priority", { action: "clear" }, null, back);
  const move = (i: number, by: number) => {
    const next = priorityItems(entries);
    const [item] = next.splice(i, 1);
    next.splice(i + by, 0, item);
    void send(`priority:move:${entries[i].account}`, next);
  };
  const paceOn = cfg?.routing?.routing_pace_order?.value !== false;
  const ids = entries.map((e) => `priority:move:${e.account}`).concat(entries.map((e) => `priority:remove:${e.account}`));
  return (
    <div className="route-priority-set" role="group" aria-label="Your priority">
      <h3 className="sub-title">Priority</h3>
      <p className="set-help">
        Your order of accounts, each with your words for its role and an end if you want one. Conversations hear it
        first, before the account lines, word for word. Khipu keeps your order as you set it and never picks or reorders
        for you; if the first account is limited, paused or down, conversations hear that beside it.
      </p>
      {p?.pending ? (
        <div className="route-disagree" role="group" aria-label="A conversation set your priority">
          <span>
            <b>{priorityPendingWords(p)}</b>
          </span>
          {p.owner_request ? <span>What it said you asked: “{p.owner_request}”</span> : null}
          <span>
            It replaced:{" "}
            {p.replaced?.entries.length
              ? p.replaced.entries.map((e, i) => `${i + 1}. ${e.label}${e.role ? `: ${e.role}` : ""}`).join("; ")
              : "no priority"}
            .
          </span>
          <div className="toolbar">
            <button type="button" className="sm primary" disabled={w.isBusy("priority:confirm")}
              onClick={() => void w.run("priority:confirm", "khipu_route_priority", { action: "confirm" })}>
              <Busy on={w.isBusy("priority:confirm")} />
              Confirm
            </button>
            <button type="button" className="sm" disabled={w.isBusy("priority:undo")}
              onClick={() => void w.run("priority:undo", "khipu_route_priority", { action: "undo" }, null, () => addSelect.current)}>
              <Busy on={w.isBusy("priority:undo")} />
              Undo
            </button>
          </div>
          <WriteError w={w} id="priority:confirm" />
          <WriteError w={w} id="priority:undo" />
        </div>
      ) : null}
      {entries.length ? (
        <ol className="rows route-priority-list" aria-label="Your priority, first to last">
          {entries.map((e, i) => (
            <li key={e.account} className="row-item route-acct">
              <span className="route-name">
                {i + 1}. {e.label}
              </span>
              <span className="route-what">
                <span>{e.role ? e.role : "No role written"}</span>
                <span className="route-src">
                  {endText(e.until, now)}
                  {e.in_settings === false ? " · not in Settings → Models" : ""}
                </span>
              </span>
              <span className="route-acts">
                <button type="button" className="sm" aria-label={`Move ${e.label} up`} disabled={i === 0 || w.anyBusy}
                  onClick={() => move(i, -1)}>
                  Up
                </button>
                <button type="button" className="sm" aria-label={`Move ${e.label} down`}
                  disabled={i === entries.length - 1 || w.anyBusy} onClick={() => move(i, 1)}>
                  Down
                </button>
                <button type="button" className="sm" aria-label={`Change ${e.label}'s role or end`} aria-expanded={editing === i}
                  onClick={() => setEditing((v) => (v === i ? null : i))}>
                  Change…
                </button>
                <button type="button" className="sm" aria-label={`Remove ${e.label} from your priority`} disabled={w.anyBusy}
                  onClick={() => void send(`priority:remove:${e.account}`, priorityItems(entries).filter((_, j) => j !== i), () => addSelect.current)}>
                  Remove
                </button>
              </span>
              {editing === i && p ? <PriorityEntryForm p={p} i={i} w={w} onClose={() => setEditing(null)} /> : null}
            </li>
          ))}
        </ol>
      ) : (
        <p className="set-help">No priority set. Conversations hear the accounts in their usual order.</p>
      )}
      {ids.map((id) => (
        <WriteError key={id} w={w} id={id} />
      ))}
      <div className="toolbar">
        {candidates.length ? (
          <>
            <label htmlFor={addId}>Add to the end</label>
            <select id={addId} ref={addSelect} value={choice} onChange={(e) => setAdding(e.target.value)}>
              {candidates.map((a) => (
                <option key={a.key} value={a.key}>
                  {a.label}
                </option>
              ))}
            </select>
            <button type="button" className="sm" disabled={!choice || w.isBusy("priority:add")}
              onClick={() => void send("priority:add", [...priorityItems(entries), { account: choice, role: null, until: null }])}>
              <Busy on={w.isBusy("priority:add")} />
              Add
            </button>
          </>
        ) : null}
        {p ? (
          <>
            <span className="route-src">Whole priority: {endText(p.until, now)}</span>
            <button type="button" className="sm" aria-expanded={endOpen} onClick={() => setEndOpen((v) => !v)}>
              Change the end…
            </button>
            <button type="button" className="sm" aria-expanded={clearing} onClick={() => setClearing((v) => !v)}>
              Clear the priority
            </button>
          </>
        ) : null}
      </div>
      <WriteError w={w} id="priority:add" />
      {endOpen && p ? <PriorityEndForm p={p} w={w} onClose={() => setEndOpen(false)} /> : null}
      {clearing && p ? (
        <ConfirmGroup label="Clear your priority?" onClose={() => setClearing(false)} back={() => addSelect.current}>
          <span>Clear your priority? Conversations hear "No priority set" on their next prompt. It stays in Recent changes.</span>
          <WriteError w={w} id="priority:clear" />
          <div className="toolbar">
            <button type="button" className="sm danger" disabled={w.isBusy("priority:clear")}
              onClick={async () => {
                if (await w.run("priority:clear", "khipu_route_priority", { action: "clear" }, null, () => addSelect.current)) setClearing(false);
              }}>
              <Busy on={w.isBusy("priority:clear")} />
              Clear it
            </button>
            <button type="button" className="sm link" onClick={() => setClearing(false)}>
              Keep it
            </button>
          </div>
        </ConfirmGroup>
      ) : null}
      <SwitchRow id="route-pace-order" label="With no priority set, list accounts by how soon their allowance runs out unused"
        what="Khipu works out, for each account it can read, the pace that would use what is left of its longest window by the time it resets, and lists the fastest first under a heading that says it is arithmetic, not your priority. Each account's pace is told either way. A priority you set always comes first and is never reordered."
        tag={<Tag>{paceOn ? "On" : "Off"}</Tag>} checked={paceOn} disabled={!cfg || w.anyBusy} busy={w.isBusy("pace-order")}
        onChange={(on) => void w.run("pace-order", "khipu_config_set", { key: "routing_pace_order", value: on ? "on" : "off" }, reread)}
        error={<WriteError w={w} id="pace-order" />} />
      <div className="rows">
        <LimitRow name="Warn when a priority a conversation set is still unconfirmed after"
          help="Khipu's health report reminds you to confirm or undo it."
          fields={[{ key: "routing_priority_unconfirmed_warn_after", label: "Warn after", unit: "time", options: [12 * H, D, 3 * D, 0], zero: "Never" }]}
          cfg={cfg} w={w} reread={reread} />
      </div>
    </div>
  );
}

function AccountsSection({
  data,
  plans,
  cfg,
  w,
  now,
  reread,
}: {
  data: RouteView;
  plans?: UsageReport | null;
  cfg: RouteConfig | null;
  w: Writer;
  now: Date;
  reread: () => Promise<unknown>;
}) {
  const accounts = inPlansOrder(data.accounts ?? [], plans);
  return (
    <div className="section-card">
      <div className="section-head">Accounts</div>
      <div className="section-body">
        <PrioritySection data={data} cfg={cfg} w={w} now={now} reread={reread} />
        <p className="set-help">
          The same accounts as Plans, from the list in Settings → Capture &amp; models. Reports come from four
          places: each account's plan check, your job runners, conversations, and you. Your routing table is not one
          of them.
        </p>
        {accounts.length ? (
          <div className="rows" role="list" aria-label="Accounts">
            {accounts.map((a) => (
              <AccountRow key={a.key} a={a} w={w} now={now} reportDefault={data.report_default_limit}
                fact={data.plan_facts?.[a.key]} />
            ))}
          </div>
        ) : (
          <EmptyState
            title="No accounts to show"
            hint="Add an account under Plans in Settings → Capture & models, and it shows here."
          />
        )}
        <p className="set-help">
          Mark paused tells conversations you want an account left alone; Lift ends it. Clear ends a limit or a
          "down" report at once, and Khipu treats the account as open until something newer reports on it; if Claude
          or a job runner reports the limit again, it comes back.
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The owner's note
// ---------------------------------------------------------------------------

function NoteSection({ data, w, now }: { data: RouteView; w: Writer; now: Date }) {
  const note = data.note ?? null;
  const [editing, setEditing] = useState(false);
  const [ending, setEnding] = useState(false);
  const [draft, setDraft] = useState("");
  const editBtn = useRef<HTMLButtonElement>(null);
  const endBtn = useRef<HTMLButtonElement>(null);
  const fieldId = useId();
  const close = () => {
    const from = ending ? "end" : "edit";
    setEditing(false);
    setEnding(false);
    // Back to the button that opened it; once the note ended, to Write a note.
    requestAnimationFrame(() => ((from === "end" ? endBtn.current : null) ?? editBtn.current)?.focus());
  };
  async function save(e: FormEvent) {
    e.preventDefault();
    if (!draft.trim()) {
      w.fail("note", "Write the note first.");
      return;
    }
    if (await w.run("note", "khipu_route_note_set", { text: draft })) close();
  }
  const written = note?.at ? `written ${whenLocal(note.at, now) || "today"}` : null;
  return (
    <div className="section-card">
      <div className="section-head">
        Your note to every conversation<span className="spacer" />
        {written ? <span className="meta">{written}</span> : null}
      </div>
      <div className="section-body">
        {note ? (
          <blockquote className="route-quote">“{note.text}”</blockquote>
        ) : (
          <p className="set-help">No note now. Write one to give every conversation your direction in your words.</p>
        )}
        {editing ? (
          <form className="route-pause route-note-form" aria-label="Edit your note" onSubmit={save} onKeyDown={escapeCloses(close)}>
            <label htmlFor={fieldId}>Your note</label>
            <textarea
              id={fieldId}
              value={draft}
              autoFocus
              spellCheck
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void save(e);
              }}
            />
            <WriteError w={w} id="note" />
            <div className="toolbar">
              <button type="submit" className="primary sm" disabled={w.isBusy("note")}>
                <Busy on={w.isBusy("note")} />
                Save note
              </button>
              <button type="button" className="sm link" onClick={close}>
                Cancel
              </button>
            </div>
          </form>
        ) : (
          <div className="toolbar">
            <button
              ref={editBtn}
              type="button"
              className="sm"
              onClick={() => {
                setDraft(note?.text ?? "");
                setEnding(false);
                setEditing(true);
              }}
            >
              {note ? "Edit note" : "Write a note"}
            </button>
            {note ? (
              <button ref={endBtn} type="button" className="sm" aria-expanded={ending} onClick={() => setEnding((v) => !v)}>
                End this note
              </button>
            ) : null}
          </div>
        )}
        {ending && note ? (
          <ConfirmGroup label="End your note?" onClose={close} back={() => endBtn.current ?? editBtn.current}>
            <span>End your note? Conversations stop seeing it on their next prompt. It stays in Recent changes.</span>
            <WriteError w={w} id="note-end" />
            <div className="toolbar">
              <button
                type="button"
                className="sm danger"
                disabled={w.isBusy("note-end")}
                onClick={async () => {
                  if (await w.run("note-end", "khipu_route_note_end")) close();
                }}
              >
                <Busy on={w.isBusy("note-end")} />
                End the note
              </button>
              <button type="button" className="sm link" onClick={close}>
                Keep it
              </button>
            </div>
          </ConfirmGroup>
        ) : null}
        <p className="set-help">
          Conversations see this word for word, right after your standing rules, and it is never shortened. Khipu
          never turns it into a choice of account. Earlier notes stay in Recent changes. Khipu sets no limit on its
          length; the app hands over up to {WORDS_MOST_KB} KB of text in one change.
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Standing rules
// ---------------------------------------------------------------------------

type RuleAsk = { n: number; kind: "confirm" | "edit" | "remove" } | null;

/** A standing rule's Remove button: where focus goes once the rule is back. */
function ruleRemoveButton(n: number): HTMLElement | null {
  return document.querySelector<HTMLElement>(`.route-rule-list button[aria-label="Remove rule ${n}"]`);
}

function RuleRow({
  r,
  watched,
  result,
  ask,
  setAsk,
  w,
  now,
  onRemoved,
  back,
}: {
  r: RouteRule;
  watched: boolean;
  result: RuleResult | undefined;
  ask: RuleAsk;
  setAsk: (a: RuleAsk) => void;
  w: Writer;
  now: Date;
  onRemoved: (r: RouteRule) => void;
  back: Back;
}) {
  const [draft, setDraft] = useState(r.text);
  const [editConfirm, setEditConfirm] = useState(false);
  const mine = ask?.n === r.n ? ask.kind : null;
  const pending = !!r.pending;
  const toConfirm = pending || (r.seeded && !r.confirmed);
  const id = `rule:${r.n}`;
  const fieldId = useId();
  const confirmBtn = useRef<HTMLButtonElement>(null);
  const editBtn = useRef<HTMLButtonElement>(null);
  const removeBtn = useRef<HTMLButtonElement>(null);
  const close = () => {
    const from = mine;
    setAsk(null);
    setEditConfirm(false);
    if (from === "edit") {
      // The edit form is not a ConfirmGroup: its field takes focus itself.
      requestAnimationFrame(() => {
        if (!document.activeElement || document.activeElement === document.body) editBtn.current?.focus();
      });
    }
  };
  const origin = ruleOrigin(r.source, r.date, now);
  const checks = ruleResultLines(watched, result, now.getTime());
  return (
    <li className={`row-item route-rule${result?.result === "broken" && checks.some((c) => c.tone === "warn") ? " broken" : ""}`}>
      <span className="route-rule-n" aria-hidden>
        {r.n}.
      </span>
      <span className="route-rule-body">
        <span className="row-main">{r.text}</span>
        <span className="route-rule-meta">
          {origin}
          {pending ? <Tag tone="warn">{r.pending_kind === "changed" ? "Changed, reconfirm" : "Added, confirm"}</Tag> : null}
          {!pending && toConfirm ? <Tag tone="warn">Seeded, confirm</Tag> : null}
          {r.seeded && r.confirmed ? <Tag tone="ok">Confirmed</Tag> : null}
          {pending ? (
            <span className="set-help">
              {`${r.pending_kind === "changed" ? "Changed" : "Added"} by ${r.pending_by ?? "a conversation"} at the owner's request; the owner has not confirmed it yet.`}
              {r.pending_kind === "changed" && r.prior_text ? ` It replaced: “${r.prior_text}”` : ""}
            </span>
          ) : null}
        </span>
        {checks.map((c) => (
          <span key={c.text} className={`rule-result${c.tone ? ` ${c.tone}` : ""}`}>{c.text}</span>
        ))}
      </span>
      <span className="route-acts">
        {toConfirm ? (
          <button ref={confirmBtn} type="button" className="sm" aria-label={`Confirm rule ${r.n}`} aria-expanded={mine === "confirm"} onClick={() => setAsk({ n: r.n, kind: "confirm" })}>
            Confirm
          </button>
        ) : null}
        {pending ? (
          <button
            type="button"
            className="sm"
            disabled={w.isBusy(`${id}:undo`)}
            aria-label={`Undo pending rule ${r.n}`}
            onClick={() => void w.run(`${id}:undo`, "khipu_route_rule", { action: r.pending_kind === "added" ? "dismiss" : "undo", n: r.n }, null, back)}
          >
            <Busy on={w.isBusy(`${id}:undo`)} />
            Undo
          </button>
        ) : null}
        <button
          ref={editBtn}
          type="button"
          className="sm"
          aria-label={`Edit rule ${r.n}`}
          aria-expanded={mine === "edit"}
          onClick={() => {
            setDraft(r.text);
            setEditConfirm(false);
            setAsk({ n: r.n, kind: "edit" });
          }}
        >
          Edit
        </button>
        <button ref={removeBtn} type="button" className="sm" aria-label={`Remove rule ${r.n}`} aria-expanded={mine === "remove"} onClick={() => setAsk({ n: r.n, kind: "remove" })}>
          Remove
        </button>
      </span>
      {pending ? <WriteError w={w} id={`${id}:undo`} /> : null}
      {mine === "confirm" ? (
        <ConfirmGroup label={`Confirm rule ${r.n}?`} onClose={close} back={() => confirmBtn.current ?? editBtn.current}>
          <span>{pending ? `Confirm this pending ${r.pending_kind === "changed" ? "change" : "addition"}?` : `Confirm rule ${r.n} as written? It loses its Seeded mark.`} It stays first in every conversation.</span>
          <WriteError w={w} id={id} />
          <div className="toolbar">
            <button
              type="button"
              className="primary sm"
              disabled={w.isBusy(id)}
              onClick={async () => {
                if (await w.run(id, "khipu_route_rule", { action: "confirm", n: r.n })) close();
              }}
            >
              <Busy on={w.isBusy(id)} />
              Confirm rule {r.n}
            </button>
            <button type="button" className="sm link" onClick={close}>
              Cancel
            </button>
          </div>
        </ConfirmGroup>
      ) : null}
      {mine === "edit" ? (
        <form
          className={editConfirm ? "route-confirm" : "route-pause"}
          aria-label={`Edit rule ${r.n}`}
          onKeyDown={escapeCloses(close)}
          onSubmit={async (e) => {
            e.preventDefault();
            if (!draft.trim()) {
              w.fail(id, "Write the rule's words first.");
              return;
            }
            if (!editConfirm) {
              w.fail(id, null);
              setEditConfirm(true);
              return;
            }
            if (await w.run(id, "khipu_route_rule", { action: "edit", n: r.n, text: draft })) close();
          }}
        >
          <div className="toolbar">
            <label htmlFor={fieldId}>Rule {r.n}</label>
            <input id={fieldId} value={draft} autoFocus autoComplete="off" readOnly={editConfirm} onChange={(e) => setDraft(e.target.value)} />
          </div>
          {editConfirm ? (
            <span>Change rule {r.n} to these words? Every conversation sees them first, in full, from its next prompt.</span>
          ) : null}
          <WriteError w={w} id={id} />
          <div className="toolbar">
            <button type="submit" className="primary sm" disabled={w.isBusy(id)}>
              <Busy on={w.isBusy(id)} />
              {editConfirm ? `Change rule ${r.n}` : "Save…"}
            </button>
            <button type="button" className="sm link" onClick={close}>
              Cancel
            </button>
          </div>
        </form>
      ) : null}
      {mine === "remove" ? (
        <ConfirmGroup label={`Remove rule ${r.n}?`} onClose={close} back={() => removeBtn.current}>
          <span>
            Remove this rule? Conversations stop seeing it on their next prompt. You can undo this here or from Recent
            changes.
          </span>
          <WriteError w={w} id={id} />
          <div className="toolbar">
            <button
              type="button"
              className="sm danger"
              disabled={w.isBusy(id)}
              onClick={async () => {
                if (await w.run(id, "khipu_route_rule", { action: "retire", n: r.n })) {
                  close();
                  onRemoved(r);
                }
              }}
            >
              <Busy on={w.isBusy(id)} />
              Remove rule {r.n}
            </button>
            <button type="button" className="sm link" onClick={close}>
              Keep it
            </button>
          </div>
        </ConfirmGroup>
      ) : null}
    </li>
  );
}

type RuleFilter = "all" | "confirm" | "checked" | "broken";

const RULE_FILTERS: { key: RuleFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "confirm", label: "To confirm" },
  { key: "checked", label: "Checked by Khipu" },
  { key: "broken", label: "May be broken" },
];

const rulesWord = (n: number) => `${n} rule${n === 1 ? "" : "s"}`;

function RulesSection({ data, w, now }: { data: RouteView; w: Writer; now: Date }) {
  const rules = data.rules ?? [];
  const watchedRules = data.watched_rules ?? [];
  const results = data.rule_results ?? [];
  const resultOf = (n: number) => results.find((x) => x.rule === n);
  const [filter, setFilter] = useState<RuleFilter>("all");
  const filterLabel = useId();
  const [ask, setAsk] = useState<RuleAsk>(null);
  const [removed, setRemoved] = useState<RouteRule | null>(null);
  const [draft, setDraft] = useState("");
  const [adding, setAdding] = useState(false);
  const fieldId = useId();
  const addBtn = useRef<HTMLButtonElement>(null);
  const toConfirm = rules.filter((r) => r.pending || (r.seeded && !r.confirmed)).length;
  const inFilter: Record<RuleFilter, (r: RouteRule) => boolean> = {
    all: () => true,
    confirm: (r) => !!r.pending || (!!r.seeded && !r.confirmed),
    checked: (r) => watchedRules.includes(r.n),
    broken: (r) => watchedRules.includes(r.n) && resultOf(r.n)?.result === "broken",
  };
  const counts = Object.fromEntries(RULE_FILTERS.map((f) => [f.key, rules.filter(inFilter[f.key]).length])) as Record<RuleFilter, number>;
  const shown = rules.filter(inFilter[filter]);
  const checkable = rules.filter((r) => watchedRules.includes(r.n)).length;
  async function add(e: FormEvent) {
    e.preventDefault();
    if (!draft.trim()) {
      w.fail("rule-add", "Write the rule's words first.");
      return;
    }
    if (!adding) {
      w.fail("rule-add", null);
      setAdding(true);
      return;
    }
    if (await w.run("rule-add", "khipu_route_rule", { action: "add", text: draft })) {
      setDraft("");
      setAdding(false);
    }
  }
  const restoreId = removed ? `rule-restore:${removed.n}` : "";
  return (
    <div className="section-card">
      <div className="section-head">
        Standing rules<span className="spacer" />
        <Tag>
          {`${rules.length} rule${rules.length === 1 ? "" : "s"}`}
          {toConfirm ? ` · ${toConfirm} to confirm` : ""}
        </Tag>
      </div>
      <div className="section-body">
        <p className="set-help">
          {"Every conversation sees these rules first, in full. "}
          {checkable
            ? `Khipu can check ${checkable} of them by reading your job runners' settings; if one looks broken you get a warning.`
            : "Khipu can check rules about -fast models, the paid xAI API and Grok work by reading your job runners' settings; none of these is one."}
          {" Khipu never stops any work."}
        </p>
        <p className="set-help">
          Rules marked Seeded were copied from your routing table and your instructions; confirm each one, or edit or
          remove it. A conversation can add or change a rule at your request; those rows show what changed and stay
          pending until you confirm or undo them. Rules are never shortened or cut, and Khipu sets no limit on their
          length (the app hands over up to {WORDS_MOST_KB} KB of text at once).
        </p>
        {rules.length ? (
          <div className="route-filter">
            <span className="lbl" id={filterLabel}>
              Show rules
            </span>
            <div className="chips" role="group" aria-labelledby={filterLabel}>
              {RULE_FILTERS.map((f) => (
                <button
                  key={f.key}
                  type="button"
                  className={`chip${filter === f.key ? " on" : ""}`}
                  aria-pressed={filter === f.key}
                  aria-label={`${f.label}, ${rulesWord(counts[f.key])}`}
                  onClick={() => setFilter(f.key)}
                >
                  {f.label} <span className="n" aria-hidden>{counts[f.key]}</span>
                </button>
              ))}
            </div>
          </div>
        ) : null}
        {shown.length ? (
          <ol className="rows route-rule-list" role="list" aria-label="Standing rules">
            {shown.map((r) => (
              <RuleRow key={r.n} r={r} watched={watchedRules.includes(r.n)} result={resultOf(r.n)} ask={ask} setAsk={setAsk} w={w} now={now} onRemoved={setRemoved} back={() => addBtn.current} />
            ))}
          </ol>
        ) : rules.length ? (
          <p className="set-help">
            {`No rules here. `}
            <button type="button" className="sm link" onClick={() => setFilter("all")}>
              Show all {rulesWord(rules.length)}
            </button>
          </p>
        ) : (
          <p className="set-help">No standing rules yet. Add one below.</p>
        )}
        {removed && !rules.some((r) => r.n === removed.n) ? (
          <div className="route-undo" role="status">
            <span>Rule {removed.n} removed.</span>
            {/* It appears only after a removal here: focus lands on it (F5). */}
            <button
              type="button"
              className="sm"
              autoFocus
              disabled={w.isBusy(restoreId)}
              onClick={async () => {
                const n = removed.n;
                if (await w.run(restoreId, "khipu_route_rule", { action: "restore", n }, null, () => ruleRemoveButton(n)))
                  setRemoved(null);
              }}
            >
              <Busy on={w.isBusy(restoreId)} />
              Undo
            </button>
          </div>
        ) : null}
        {removed ? <WriteError w={w} id={restoreId} /> : null}
        <form className="route-form" aria-label="Add a standing rule" onSubmit={add}>
          <div className="toolbar">
            <label htmlFor={fieldId}>New rule</label>
            <input
              id={fieldId}
              value={draft}
              autoComplete="off"
              placeholder="A rule every conversation sees first…"
              readOnly={adding}
              onChange={(e) => setDraft(e.target.value)}
            />
            {!adding ? (
              <button ref={addBtn} type="submit" className="sm">
                Add rule…
              </button>
            ) : null}
          </div>
          {adding ? (
            <ConfirmGroup label="Add this rule?" onClose={() => setAdding(false)} back={() => addBtn.current}>
              <span>Add this rule? Every conversation sees it first, in full, from its next prompt.</span>
              <div className="toolbar">
                <button type="submit" className="primary sm" disabled={w.isBusy("rule-add")}>
                  <Busy on={w.isBusy("rule-add")} />
                  Add the rule
                </button>
                <button type="button" className="sm link" onClick={() => setAdding(false)}>
                  Cancel
                </button>
              </div>
            </ConfirmGroup>
          ) : null}
          <WriteError w={w} id="rule-add" />
        </form>
        <p className="set-help">
          Khipu doesn't enforce rules. A rule Checked by Khipu that looks broken shows a warning here, on Home and in
          the health report; nothing is stopped.
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sizes and times: one row per limit, each with Change… and its choices.
// ---------------------------------------------------------------------------

type LimitField = { key: string; label: string; unit: LimitUnit; options: number[]; zero?: string };

function settingNumber(cfg: RouteConfig | null, key: string): number | null {
  const v = cfg?.routing?.[key]?.value;
  return typeof v === "number" ? v : null;
}

function settingDefault(cfg: RouteConfig | null, key: string): number | null {
  const v = cfg?.routing?.[key]?.default;
  return typeof v === "number" ? v : null;
}

function LimitRow({
  name,
  help,
  fields,
  cfg,
  w,
  join = " · ",
  reread,
}: {
  name: string;
  help: string;
  fields: LimitField[];
  cfg: RouteConfig | null;
  w: Writer;
  join?: string;
  reread: () => Promise<unknown>;
}) {
  const [open, setOpen] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const btn = useRef<HTMLButtonElement>(null);
  const formId = `lim-${useId().replace(/:/g, "")}`;
  const values = fields.map((f) => settingNumber(cfg, f.key));
  const shown = fields
    .map((f, i) => (values[i] == null ? "…" : limitWords(values[i]!, f.unit, f.zero)))
    .join(join);
  const id = `limit:${fields[0].key}`;
  const close = () => {
    setOpen(false);
    requestAnimationFrame(() => btn.current?.focus());
  };
  async function save(e: FormEvent) {
    e.preventDefault();
    for (const f of fields) {
      const next = drafts[f.key];
      if (next == null || Number(next) === settingNumber(cfg, f.key)) continue;
      const ok = await w.run(id, "khipu_config_set", { key: f.key, value: next }, reread);
      if (!ok) return;
    }
    close();
  }
  return (
    <div className="row-item route-limit">
      <span className="route-limit-text">
        <span className="route-limit-name">{name}</span>
        <span className="set-help">{help}</span>
      </span>
      <span className="route-val">{shown}</span>
      <button
        ref={btn}
        type="button"
        className="sm"
        aria-label={`Change: ${name}`}
        aria-expanded={open}
        aria-controls={open ? formId : undefined}
        disabled={values.some((v) => v == null)}
        onClick={() => {
          setDrafts(Object.fromEntries(fields.map((f, i) => [f.key, String(values[i] ?? "")])));
          setOpen((v) => !v);
        }}
      >
        Change…
      </button>
      {open ? (
        <form className="route-pause" id={formId} aria-label={`Change: ${name}`} onSubmit={save} onKeyDown={escapeCloses(close)}>
          <div className="toolbar">
            {fields.map((f, i) => {
              const def = settingDefault(cfg, f.key);
              const opts = [...new Set([...f.options, ...(values[i] != null ? [values[i]!] : [])])].sort(
                (x, y) => (x === 0 ? Infinity : x) - (y === 0 ? Infinity : y),
              );
              const selId = `${formId}-${f.key}`;
              return (
                <span key={f.key} className="toolbar">
                  <label htmlFor={selId}>{f.label}</label>
                  <select
                    id={selId}
                    autoFocus={i === 0}
                    value={drafts[f.key] ?? ""}
                    onChange={(e) => setDrafts((d) => ({ ...d, [f.key]: e.target.value }))}
                  >
                    {opts.map((o) => (
                      <option key={o} value={String(o)}>
                        {limitWords(o, f.unit, f.zero)}
                        {o === def ? " (the default)" : ""}
                      </option>
                    ))}
                  </select>
                </span>
              );
            })}
            <button type="submit" className="primary sm" disabled={w.isBusy(id)}>
              <Busy on={w.isBusy(id)} />
              Save
            </button>
            <button type="button" className="sm link" onClick={close}>
              Cancel
            </button>
          </div>
          <WriteError w={w} id={id} />
        </form>
      ) : null}
    </div>
  );
}

function RetryTimesRow({ cfg, w, reread }: { cfg: RouteConfig | null; w: Writer; reread: () => Promise<unknown> }) {
  const raw = cfg?.routing?.routing_reset_check_retries?.value;
  const values = Array.isArray(raw) && raw.every((v) => typeof v === "number") ? raw as number[] : null;
  const [first, setFirst] = useState("600");
  const [second, setSecond] = useState("1800");
  const [dirty, setDirty] = useState(false);
  useEffect(() => {
    if (values && !dirty) {
      setFirst(values[0] != null ? String(values[0]) : "");
      setSecond(values[1] != null ? String(values[1]) : "");
    }
  }, [values?.length, values?.[0], values?.[1], dirty]);
  const id = "reset-retries";
  // None, one or two retries: both empty removes them (fix round 1, F7).
  const a = first.trim();
  const b = second.trim();
  const whole = (t: string) => /^\d+$/.test(t) && Number(t) > 0;
  const valid = (a === "" && b === "") || (whole(a) && (b === "" || (whole(b) && Number(b) > Number(a))));
  const value = `[${[a, b].filter((t) => t !== "").join(",")}]`;
  const shown = values == null ? "…" : values.length === 0 ? "No retries" : values.map((v) => limitWords(v, "time")).join(" and ");
  return <div className="row-item route-limit route-retries">
    <span className="route-limit-text"><span className="route-limit-name">Try again after</span><span className="set-help">Up to two retry times when the same limit remains. Leave both empty for no retries.</span></span>
    <span className="route-val">{shown}</span>
    <span className="toolbar"><label>First retry (seconds)<input aria-label="First retry in seconds" value={first} onChange={(e) => { setDirty(true); setFirst(e.target.value); }} /></label><label>Second retry (seconds)<input aria-label="Second retry in seconds" value={second} onChange={(e) => { setDirty(true); setSecond(e.target.value); }} /></label><button type="button" className="sm" disabled={!valid || w.isBusy(id)} onClick={() => void w.run(id, "khipu_config_set", { key: "routing_reset_check_retries", value }, async () => { await reread(); setDirty(false); })}><Busy on={w.isBusy(id)} />Save</button></span>
    {!valid ? <span className="set-error" role="alert">Use positive whole seconds, the second later than the first, or leave both empty for no retries.</span> : null}<WriteError w={w} id={id} />
  </div>;
}

const M = 60;
const H = 3600;
const D = 86400;

// ---------------------------------------------------------------------------
// Holds
// ---------------------------------------------------------------------------

const HOLD_REASON_ARGS: { value: string; label: string }[] = [
  { value: "machine-busy", label: "Machine busy" },
  { value: "disk-low", label: "Disk low" },
  { value: "keep", label: "Keep for one purpose" },
  { value: "outage", label: "Outage" },
  { value: "other", label: "Other" },
];

function holdSourceWords(h: RouteHold, now: Date): string {
  const by = h.owner ? `set by you ${saidWhen(h.at, now)}`.trim() : `set by ${h.by || "a conversation"}${h.at ? ` ${saidWhen(h.at, now)}` : ""}`;
  const end = h.until ? `ends ${whenLocal(h.until, now)}` : "until you end it";
  return `${holdKind(h.reason)} · ${by} · ${end}`;
}

/** A hold's length as the CLI counts it: its words on one line (runs of
 * white space as one space, trimmed), in characters. */
function holdWordsLength(text: string): number {
  return [...text.replace(/\s+/g, " ").trim()].length;
}

function HoldsSection({
  data,
  cfg,
  w,
  now,
  reread,
}: {
  data: RouteView;
  cfg: RouteConfig | null;
  w: Writer;
  now: Date;
  reread: () => Promise<unknown>;
}) {
  const holds = data.holds ?? [];
  const accounts = data.accounts ?? [];
  const [text, setText] = useState("");
  const [about, setAbout] = useState("all");
  const [reason, setReason] = useState("other");
  const [choice, setChoice] = useState("tomorrow");
  const [custom, setCustom] = useState("");
  const base = useId().replace(/:/g, "");
  const endBtns = useRef<Record<string, HTMLButtonElement | null>>({});
  const addBtn = useRef<HTMLButtonElement>(null);
  // The longest words a hold may have (0: no limit), as the CLI enforces it.
  const most = settingNumber(cfg, "routing_hold_chars");
  async function add(e: FormEvent) {
    e.preventDefault();
    if (!text.trim()) {
      w.fail("hold-add", "Write what conversations should know.");
      return;
    }
    const words = holdWordsLength(text);
    if (most && words > most) {
      w.fail(
        "hold-add",
        `A hold's words are at most ${limitWords(most, "chars")} (these are ${limitWords(words, "chars")}). ` +
          "Shorten them, or raise the limit under Longest words in a hold below.",
      );
      return;
    }
    const at = new Date();
    const end = chosenEnd(choice, custom, endChoices(at, 4), at);
    if ("error" in end) {
      w.fail("hold-add", end.error);
      return;
    }
    const scope = about === "all" || about === "machine" ? about : "account";
    const ok = await w.run("hold-add", "khipu_route_hold_add", {
      text,
      reason,
      scope,
      account: scope === "account" ? about : null,
      until: end.iso,
    });
    if (ok) setText("");
  }
  return (
    <div className="section-card">
      <div className="section-head">Holds</div>
      <div className="section-body">
        <p className="set-help">Short notes about one account, this Mac or all work. Every hold from a conversation ends on its own.</p>
        {holds.length ? (
          <div className="rows" role="list" aria-label="Holds">
            {holds.map((h, i) => {
              const id = `hold-end:${h.id}`;
              return (
                <div key={h.id} className="row-item route-acct" role="listitem">
                  <span className="route-name">{capitalise(h.scope_label)}</span>
                  <span className="route-what">
                    <span className="route-state">{holdWords(h)}</span>
                    <span className="route-src">{holdSourceWords(h, now)}</span>
                  </span>
                  <span className="route-acts">
                    <button
                      ref={(el) => {
                        endBtns.current[h.id] = el;
                      }}
                      type="button"
                      className="sm"
                      aria-label={`End the hold on ${h.scope_label}`}
                      disabled={w.isBusy(id)}
                      onClick={() => {
                        // Once this hold ends, focus goes to the next hold's
                        // End now, else the one before, else Add hold. Worked
                        // out on the click, from this hold's place, so a long
                        // list of holds costs nothing to show (Codex review,
                        // finding 2).
                        const from = holds;
                        void w.run(id, "khipu_route_hold_end", { id: h.id }, null, () => {
                          const usable = (j: number) => {
                            const b = endBtns.current[from[j].id];
                            return b?.isConnected ? b : null;
                          };
                          for (let j = i + 1; j < from.length; j++) if (usable(j)) return usable(j);
                          for (let j = i - 1; j >= 0; j--) if (usable(j)) return usable(j);
                          return addBtn.current;
                        });
                      }}
                    >
                      <Busy on={w.isBusy(id)} />
                      End now
                    </button>
                  </span>
                  {w.errors[id] ? (
                    <div className="route-pause">
                      <WriteError w={w} id={id} />
                    </div>
                  ) : null}
                </div>
              );
            })}
          </div>
        ) : (
          <p className="set-help">No holds now.</p>
        )}
        <p className="set-help">
          Conversations are told who set each hold, what kind it is and when it ends. The words of a hold set by a
          conversation show here and when a conversation asks; only your own holds are sent word for word.
        </p>
        <form className="route-form" aria-label="Add a hold" onSubmit={add}>
          <div className="toolbar">
            <label htmlFor={`${base}-text`}>Words</label>
            <input
              id={`${base}-text`}
              value={text}
              placeholder="What conversations should know…"
              autoComplete="off"
              aria-describedby={`${base}-most`}
              onChange={(e) => setText(e.target.value)}
            />
            <span className="set-help" id={`${base}-most`}>
              {most == null
                ? ""
                : most
                  ? `At most ${limitWords(most, "chars")}`
                  : `No limit on length; the app hands over up to ${WORDS_MOST_KB} KB at once`}
            </span>
          </div>
          <div className="toolbar">
            <span className="route-field">
              <label htmlFor={`${base}-about`}>About</label>
              <select id={`${base}-about`} value={about} onChange={(e) => setAbout(e.target.value)}>
                <option value="all">All work</option>
                <option value="machine">This Mac</option>
                {accounts.map((a) => (
                  <option key={a.key} value={a.key}>
                    {a.label}
                  </option>
                ))}
              </select>
            </span>
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
            <EndFields
              idBase={`${base}-hold`}
              label="Ends"
              firstHours={4}
              choice={choice}
              setChoice={setChoice}
              custom={custom}
              setCustom={setCustom}
            />
          </div>
          <WriteError w={w} id="hold-add" />
          <div className="toolbar">
            <button ref={addBtn} type="submit" className="sm" disabled={w.isBusy("hold-add")}>
              <Busy on={w.isBusy("hold-add")} />
              Add hold
            </button>
          </div>
        </form>
        <div className="rows">
          <LimitRow
            name="How long a conversation's hold lasts"
            help="When a conversation doesn't say; it can ask for longer, up to the most."
            fields={[
              { key: "routing_conversation_hold", label: "Usually", unit: "time", options: [H, 4 * H, 12 * H, 24 * H, 48 * H] },
              {
                key: "routing_conversation_hold_max",
                label: "At most",
                unit: "time",
                options: [24 * H, 3 * D, 7 * D, 14 * D, 30 * D, 0],
                zero: "No maximum",
              },
            ]}
            join=" · at most "
            cfg={cfg}
            w={w}
            reread={reread}
          />
          <LimitRow
            name="Longest words in a hold"
            help="Yours and conversations' alike."
            fields={[{ key: "routing_hold_chars", label: "Longest words", unit: "chars", options: [150, 300, 600, 1200, 0], zero: "No limit" }]}
            cfg={cfg}
            w={w}
            reread={reread}
          />
          <LimitRow
            name="Remind me about my own holds after"
            help="A reminder on Home; your hold stays until you end it."
            fields={[
              {
                key: "routing_owner_hold_reminder",
                label: "Remind me after",
                unit: "time",
                options: [30 * D, 60 * D, 90 * D, 180 * D, 365 * D, 0],
                zero: "Never",
              },
            ]}
            cfg={cfg}
            w={w}
            reread={reread}
          />
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Telling conversations
// ---------------------------------------------------------------------------

const HEARS: [string, string][] = [
  ["Claude Code", "At the start, before each prompt and before handing work to another agent, on every Claude account. A subagent hears the summary when it starts."],
  ["Codex", "At the start, before each prompt and before handing work to another agent, once you approve Khipu's hooks in Codex."],
  ["Cursor", "At the start only; Cursor has no step before each prompt."],
  ["Grok Build", "When it asks Khipu."],
  ["Aegis", "When its own client asks Khipu."],
  ["Cloud agents", "Not yet: they will hear it from a copy for cloud agents, in a later release."],
];

/** Where a switch's value stands: read, still being read, or not readable.
 *  Until it is read the switch is not drawn at all, so it never shows a
 *  false off. */
type SwitchRead = { state: "ready" } | { state: "reading" } | { state: "unread"; error?: string | null; retry: () => Promise<unknown> };

/** A switch's read state from the read that feeds it. */
function switchRead(value: unknown, error: string | null | undefined, retry: () => Promise<unknown>): SwitchRead {
  if (value != null) return { state: "ready" };
  if (error != null) return { state: "unread", error, retry };
  return { state: "reading" };
}

/** Try again after a failed settings read, busy while it reads. */
function UnreadNote({ id, read }: { id: string; read: Extract<SwitchRead, { state: "unread" }> }) {
  const [retrying, setRetrying] = useState(false);
  return (
    <div className="toolbar">
      <p className="set-error" role="alert" id={`${id}-unread`}>
        Couldn't read this setting{read.error ? `: ${read.error}` : "."}
      </p>
      <button
        type="button"
        className="sm"
        disabled={retrying}
        aria-busy={retrying || undefined}
        onClick={async () => {
          setRetrying(true);
          try {
            await read.retry();
          } finally {
            setRetrying(false);
          }
        }}
      >
        <Busy on={retrying} />
        Try again
      </button>
    </div>
  );
}

function SwitchRow({
  id,
  label,
  what,
  tag,
  checked,
  disabled,
  busy,
  onChange,
  error,
  read = { state: "ready" },
}: {
  id: string;
  label: string;
  what: string;
  tag: ReactNode;
  checked: boolean;
  disabled: boolean;
  busy: boolean;
  onChange: (next: boolean) => void;
  error?: ReactNode;
  read?: SwitchRead;
}) {
  if (read.state !== "ready") {
    const reading = read.state === "reading";
    return (
      <div className="opt-row" aria-busy={reading || undefined} data-testid={`${id}-${read.state}`}>
        <span className="sw-slot">
          <Tag tone={reading ? "neutral" : "err"}>{reading ? "Reading…" : "Can't read this setting"}</Tag>
        </span>
        <div className="opt-text">
          <div className="opt-title">
            <span className="opt-name" id={`${id}-name`}>
              {label}
            </span>
          </div>
          <p id={`${id}-what`} className="opt-what">
            {what}
          </p>
          {read.state === "unread" ? <UnreadNote id={id} read={read} /> : null}
        </div>
      </div>
    );
  }
  return (
    <div className="opt-row">
      <input
        id={id}
        className="switch"
        type="checkbox"
        role="switch"
        checked={checked}
        disabled={disabled}
        aria-describedby={`${id}-tag ${id}-what`}
        onChange={(e) => onChange(e.target.checked)}
      />
      <div className="opt-text">
        <div className="opt-title">
          <label className="opt-name" htmlFor={id}>
            {label}
          </label>
          <Busy on={busy} />
          <span id={`${id}-tag`}>{tag}</span>
        </div>
        <p id={`${id}-what`} className="opt-what">
          {what}
        </p>
        {error}
      </div>
    </div>
  );
}

function dispatchScripts(cfg: RouteConfig | null): string[] | null {
  const raw = cfg?.routing?.routing_dispatch_scripts?.value;
  return Array.isArray(raw) && raw.every((v) => typeof v === "string") ? (raw as string[]) : null;
}

function DispatchScriptsRow({ cfg, w, reread }: { cfg: RouteConfig | null; w: Writer; reread: () => Promise<unknown> }) {
  const names = dispatchScripts(cfg);
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const btn = useRef<HTMLButtonElement>(null);
  const formId = `dispatch-${useId().replace(/:/g, "")}`;
  const id = "dispatch-scripts";
  const close = () => {
    setOpen(false);
    w.fail(id, null);
    requestAnimationFrame(() => btn.current?.focus());
  };
  async function save(e: FormEvent) {
    e.preventDefault();
    const next = [...new Set(draft.split(",").map((t) => t.trim()).filter((t) => t !== ""))];
    if (next.some((t) => !/^[A-Za-z0-9._-]{1,64}$/.test(t) || t === "." || t === ".." || t.startsWith("-"))) {
      w.fail(id, "Give file names only, such as dispatch.py: letters, digits, dots, dashes and underscores, no folder.");
      return;
    }
    if (next.length > 16) {
      w.fail(id, "At most 16 scripts.");
      return;
    }
    if (await w.run(id, "khipu_config_set", { key: "routing_dispatch_scripts", value: JSON.stringify(next) }, reread)) close();
  }
  return (
    <div className="row-item route-limit">
      <span className="route-limit-text">
        <span className="route-limit-name">Your dispatch scripts</span>
        <span className="set-help">
          Scripts of your own that hand work to another agent. A shell command that runs one counts as handing work on,
          like claude -p or codex exec.
        </span>
      </span>
      <span className="route-val">{names == null ? "…" : names.length ? names.join(", ") : "None"}</span>
      <button
        ref={btn}
        type="button"
        className="sm"
        aria-label="Change your dispatch scripts"
        aria-expanded={open}
        aria-controls={open ? formId : undefined}
        disabled={names == null}
        onClick={() => {
          if (open) {
            close();
            return;
          }
          setDraft((names ?? []).join(", "));
          w.fail(id, null);
          setOpen(true);
        }}
      >
        Change…
      </button>
      {open ? (
        <form className="route-pause" id={formId} aria-label="Change your dispatch scripts" onSubmit={save} onKeyDown={escapeCloses(close)}>
          <div className="toolbar">
            <label htmlFor={`${formId}-names`}>File names</label>
            <input
              id={`${formId}-names`}
              className="mono"
              value={draft}
              autoFocus
              autoComplete="off"
              spellCheck={false}
              placeholder="dispatch.py, run-job.sh…"
              onChange={(e) => setDraft(e.target.value)}
            />
          </div>
          <p className="set-help">File names only, separated by commas. Leave it empty for none.</p>
          <WriteError w={w} id={id} say={(m) => settingWords(m, "routing_dispatch_scripts", "Your dispatch scripts")} />
          <div className="toolbar">
            <button type="submit" className="primary sm" disabled={w.isBusy(id)}>
              <Busy on={w.isBusy(id)} />
              Save
            </button>
            <button type="button" className="sm link" onClick={close}>
              Cancel
            </button>
          </div>
        </form>
      ) : null}
    </div>
  );
}

function TellingSection({
  data,
  cfg,
  features,
  featuresError,
  cfgError,
  w,
  reread,
  rereadFeatures,
}: {
  data: RouteView | null;
  cfg: RouteConfig | null;
  features: FeaturesShape | null;
  featuresError?: string | null;
  cfgError?: string | null;
  w: Writer;
  reread: () => Promise<unknown>;
  rereadFeatures: () => Promise<unknown>;
}) {
  const master = features?.features?.routing_push;
  // The switch's state could not be read (the read failed, or it has no
  // answer for this switch): say so and offer to read it again, never a
  // silent off (Codex review, finding 7). A failed read after a write also
  // counts: the switch may show a value that is no longer true, so it is
  // marked unread and held until a read succeeds.
  const masterRead: SwitchRead =
    featuresError != null || (!master && features != null)
      ? { state: "unread", error: featuresError, retry: rereadFeatures }
      : !master
        ? { state: "reading" }
        : { state: "ready" };
  const cfgRead = switchRead(cfg, cfgError, reread);
  const locked = master?.source === "env";
  const moment = (key: string) => cfg?.routing?.[key]?.value === true;
  const old = data?.old_switches ?? {};
  const oldSet = [old.env ? "env" : null, old.file ? "file" : null].filter(Boolean);
  return (
    <div className="section-card">
      <div className="section-head">Telling conversations</div>
      <p className="opt-group-note">
        A running conversation hears a change as soon as its app allows. Any conversation can also ask Khipu.
        Switching these off stops only the telling.
      </p>
      <div className="opt-list">
        <SwitchRow
          id="route-push-all"
          label="Tell conversations about routing"
          what="Turns all of the telling on or off. The same switch is in Optional features."
          tag={locked ? <Tag>Locked</Tag> : <Tag>Also in Optional features</Tag>}
          checked={master?.enabled === true}
          disabled={!master || locked || w.anyBusy}
          read={masterRead}
          busy={w.isBusy("push")}
          onChange={(next) => void w.run("push", "khipu_feature_set", { name: "routing_push", enabled: next }, rereadFeatures)}
          error={
            <>
              {locked ? (
                <p className="opt-cost">
                  The environment variable <code>KHIPU_FEATURE_ROUTING_PUSH</code> decides this switch. Unset it to
                  change it here.
                </p>
              ) : null}
              <WriteError w={w} id="push" />
            </>
          }
        />
        <SwitchRow
          id="route-push-start"
          label="At the start of a conversation, and after it is compacted"
          what="The full summary: your rules, your note, every account and the holds."
          tag={<Tag tone="ok">Start</Tag>}
          checked={moment("routing_push_start")}
          disabled={!cfg || w.anyBusy}
          read={cfgRead}
          busy={w.isBusy("push-start")}
          onChange={(next) =>
            void w.run("push-start", "khipu_config_set", { key: "routing_push_start", value: next ? "on" : "off" }, reread)
          }
          error={<WriteError w={w} id="push-start" />}
        />
        <SwitchRow
          id="route-push-prompt"
          label="Before each prompt"
          what="Only what changed since the conversation last heard, as it stands now. A conversation that fell far behind gets the full summary again."
          tag={<Tag tone="ok">Next prompt</Tag>}
          checked={moment("routing_push_prompt")}
          disabled={!cfg || w.anyBusy}
          read={cfgRead}
          busy={w.isBusy("push-prompt")}
          onChange={(next) =>
            void w.run("push-prompt", "khipu_config_set", { key: "routing_push_prompt", value: next ? "on" : "off" }, reread)
          }
          error={<WriteError w={w} id="push-prompt" />}
        />
        <SwitchRow
          id="route-push-delegation"
          label="Before handing work to another agent"
          what="Reaches an orchestrator in the middle of a long turn when it starts a subagent, a delegated task or a job such as claude -p or codex exec. It arrives with the call's result and never stops the call. A subagent hears the full summary when it starts."
          tag={<Tag tone="ok">Delegation</Tag>}
          checked={moment("routing_push_delegation")}
          disabled={!cfg || w.anyBusy}
          read={cfgRead}
          busy={w.isBusy("push-delegation")}
          onChange={(next) =>
            void w.run("push-delegation", "khipu_config_set", { key: "routing_push_delegation", value: next ? "on" : "off" }, reread)
          }
          error={<WriteError w={w} id="push-delegation" />}
        />
      </div>
      <div className="section-body">
        <div className="rows">
          <DispatchScriptsRow cfg={cfg} w={w} reread={reread} />
        </div>
      </div>
      <div className="section-body">
        <h3 className="sub-title" id="route-hear-h">
          What each kind of conversation hears
        </h3>
        <div className="rows" role="list" aria-labelledby="route-hear-h">
          {HEARS.map(([name, words]) => (
            <div key={name} className="row-item route-hear" role="listitem">
              <span className="route-name">{name}</span>
              <span className="set-help">{words}</span>
            </div>
          ))}
        </div>
        <h3 className="sub-title">Sizes and times</h3>
        <div className="rows">
          <LimitRow
            name="Longest summary at the start of a conversation"
            help="Your rules and your note are never cut; this limits what follows them."
            fields={[{ key: "routing_summary_chars", label: "Longest summary", unit: "chars", options: [1000, 1500, 3000, 6000, 0], zero: "No limit" }]}
            cfg={cfg}
            w={w}
            reread={reread}
          />
          <LimitRow
            name="Longest update before a prompt"
            help="Rules and your note are never cut."
            fields={[{ key: "routing_prompt_chars", label: "Longest update", unit: "chars", options: [200, 400, 800, 1600, 0], zero: "No limit" }]}
            cfg={cfg}
            w={w}
            reread={reread}
          />
          <LimitRow
            name="Longest line"
            help="Each line about an account, a hold or a lane. Rules and your note are never cut."
            fields={[{ key: "routing_line_chars", label: "Longest line", unit: "chars", options: [100, 200, 400, 800, 0], zero: "No limit" }]}
            cfg={cfg}
            w={w}
            reread={reread}
          />
          <LimitRow
            name="Time allowed before a prompt"
            help="If reading takes longer, the update waits for the next prompt; memory search is not slowed. Never more than one second."
            fields={[{ key: "routing_prompt_budget_ms", label: "Time allowed", unit: "ms", options: [25, 50, 100, 200, 500, 1000] }]}
            cfg={cfg}
            w={w}
            reread={reread}
          />
          <LimitRow
            name="Warn when reading before a prompt is too slow on more than"
            help="Of prompts in the last 7 days; the warning shows in the health report."
            fields={[{ key: "routing_slow_read_pct", label: "Warn above", unit: "pct", options: [0.5, 1, 2, 5, 10, 0], zero: "Never" }]}
            cfg={cfg}
            w={w}
            reread={reread}
          />
          <LimitRow
            name="Time allowed at the start of a conversation"
            help="How long Khipu works on the summary when a conversation starts, so its memory still arrives on time. Never more than 8 seconds."
            fields={[{ key: "routing_start_budget_ms", label: "Time allowed at the start", unit: "ms", options: [250, 500, 1000, 2000, 4000, 8000] }]}
            cfg={cfg}
            w={w}
            reread={reread}
          />
          <LimitRow
            name="Send everything again when a conversation is this far behind"
            help="Also when it comes back after being idle."
            fields={[
              { key: "routing_resend_after_versions", label: "Changes", unit: "changes", options: [10, 20, 50, 100, 0], zero: "Never by count" },
              { key: "routing_resend_after", label: "Or time", unit: "time", options: [H, 3 * H, 6 * H, 12 * H, 24 * H, 0], zero: "Never by time" },
            ]}
            join=" or "
            cfg={cfg}
            w={w}
            reread={reread}
          />
          <LimitRow
            name="Treat a report as out of date after"
            help="An account with only out-of-date reports shows as Unknown, never as open."
            fields={[{ key: "routing_stale_after", label: "Out of date after", unit: "time", options: [H, 6 * H, 12 * H, 24 * H, 48 * H, 0], zero: "Never" }]}
            cfg={cfg}
            w={w}
            reread={reread}
          />
          <LimitRow
            name="How long a limit reported without a time lasts"
            help="When a conversation reports a limit but not when it ends. A newer report replaces it sooner."
            fields={[{ key: "routing_report_default_limit", label: "Lasts", unit: "time", options: [10 * M, 30 * M, H, 2 * H, 5 * H, 24 * H] }]}
            cfg={cfg}
            w={w}
            reread={reread}
          />
          <LimitRow
            name="Warn when two reports disagree for"
            help="The warning shows on Home and in the health report."
            fields={[{ key: "routing_disagreement_warn_after", label: "Warn after", unit: "time", options: [10 * M, 30 * M, H, 2 * H, 0], zero: "Never" }]}
            cfg={cfg}
            w={w}
            reread={reread}
          />
          <LimitRow
            name="Say the record is not refreshed after"
            help="Conversations are told when Khipu itself has stopped updating."
            fields={[{ key: "routing_not_refreshed_after", label: "Not refreshed after", unit: "time", options: [2 * M, 5 * M, 10 * M, 30 * M, 0], zero: "Never" }]}
            cfg={cfg}
            w={w}
            reread={reread}
          />
        </div>
        {oldSet.length ? (
          <p className="set-help" role="status">
            <b>Your old "routing off" switch is set, so nothing is told.</b>{" "}
            {old.env ? (
              <>
                The setting <span className="mono">ROUTING_NOW_OFF=1</span> is in the environment Khipu runs in;
                remove it from your shell profile or launch settings.{" "}
              </>
            ) : null}
            {old.file ? (
              <>
                The file <span className="mono">/tmp/.routing_now_disable</span> exists; delete it to turn the telling
                back on.
              </>
            ) : null}
          </p>
        ) : (
          // Khipu sees only its own environment, not the shell a conversation
          // starts from, so it never says ROUTING_NOW_OFF is unset (fix round 1, F4).
          <p className="set-help">
            Your old "routing off" file, <span className="mono">/tmp/.routing_now_disable</span>, isn't there. Khipu
            can't see your shell's settings: if <span className="mono">ROUTING_NOW_OFF=1</span> is set there,
            conversations started from that shell are told nothing. Setting either one turns the telling off.
          </p>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Job runner folders
// ---------------------------------------------------------------------------

type SourceEntry = { name: string; kind: string; path: string };

const FILE_WORDS: Record<string, string> = {
  config: "settings",
  events: "events.log",
  global: "limits file",
  heartbeat: "heartbeat",
};

function runnerState(r: RouteRunner | undefined, now: Date): { text: string; tone: "ok" | "warn" | "err" | null } {
  if (!r) return { text: "Not read yet", tone: null };
  if (r.unreadable) {
    return {
      text: r.unreadable_since ? `Can't read its files since ${whenLocal(r.unreadable_since, now)}` : "Can't read its files",
      tone: "err",
    };
  }
  if (r.silent) {
    return {
      // "at 10:02 AM" today, "yesterday at 10:02 AM" before (fix round 2, R2-3).
      text: r.heartbeat_at ? `Stopped reporting ${pastWords(r.heartbeat_at, now)}` : "Has not reported yet",
      tone: "err",
    };
  }
  const lanes = r.lanes?.length ?? 0;
  const read = r.heartbeat_at ? `Read ${saidWhen(r.heartbeat_at, now)}` : "Read";
  return { text: `${read} · ${lanes} lane${lanes === 1 ? "" : "s"}`, tone: "ok" };
}

function runnerDetail(entry: SourceEntry, r: RouteRunner | undefined, now: Date): string {
  const parts = [entry.path];
  if (r?.interval) parts.push(`reports every ${Math.round(r.interval)} s`);
  for (const [name, f] of Object.entries(r?.files ?? {})) {
    if (f?.error) {
      const good = f.good_at ? `last good read ${whenLocal(f.good_at, now)}` : "never read";
      parts.push(`${FILE_WORDS[name] ?? name}: ${f.error}; ${good}`);
    }
  }
  if (r && !r.unreadable && r.silent) {
    const good = Object.values(r.files ?? {})
      .map((f) => f?.good_at)
      .filter((t): t is string => !!t)
      .sort()
      .pop();
    if (good) parts.push(`last good read ${whenLocal(good, now)}, ${agoSpan(good, now)}`);
  }
  return parts.join(" · ");
}

/** Whether the routing record and the settings a section needs are read. */
type RecordRead = "ready" | "reading" | "failed";

/** "Reading…" before the record is read, or that the read failed: in place of
 *  a list's empty state, which would otherwise say "none" before anything
 *  was read. */
function RecordReadLine({ read, what }: { read: Exclude<RecordRead, "ready">; what: string }) {
  return read === "reading" ? (
    <p className="set-help" aria-busy="true" role="status">
      <Tag>Reading…</Tag> Looking for {what}.
    </p>
  ) : (
    <p className="set-error">Couldn't read {what} just now.</p>
  );
}

function FoldersSection({
  data,
  cfg,
  recordRead = "ready",
  w,
  now,
  reread,
}: {
  data: RouteView | null;
  cfg: RouteConfig | null;
  recordRead?: RecordRead;
  w: Writer;
  now: Date;
  reread: () => Promise<unknown>;
}) {
  const raw = cfg?.routing?.routing_sources?.value;
  const entries: SourceEntry[] = Array.isArray(raw) ? (raw as SourceEntry[]) : [];
  const runners = data?.sources?.runners ?? [];
  const refused = data?.sources?.refused ?? [];
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const addBtn = useRef<HTMLButtonElement>(null);
  const removeBtns = useRef<Record<string, HTMLButtonElement | null>>({});
  const base = useId().replace(/:/g, "");
  // Each change is made to the folders as Khipu has them just before it is
  // written, never to the list this page last read: an add and a remove
  // made before the first one's re-read both stay (Codex review, finding 1).
  const write = (edit: (now: SourceEntry[]) => SourceEntry[] | string, id: string) =>
    w.run(
      id,
      "khipu_config_set",
      async () => {
        const cur = await currentSetting("routing_sources");
        if (typeof cur === "string") return cur;
        const next = edit(Array.isArray(cur.value) ? (cur.value as SourceEntry[]) : []);
        return typeof next === "string" ? next : { key: "routing_sources", value: JSON.stringify(next) };
      },
      reread,
    );
  const closeAdd = () => {
    setAdding(false);
    requestAnimationFrame(() => addBtn.current?.focus());
  };
  async function add(e: FormEvent) {
    e.preventDefault();
    const n = name.trim();
    const p = path.trim();
    if (!n || !p) {
      w.fail("folder-add", "Give the folder a name and its full path.");
      return;
    }
    if (!(p.startsWith("/") || p.startsWith("~/"))) {
      w.fail("folder-add", "Give the folder's full path, starting with / or ~/.");
      return;
    }
    const taken = (list: SourceEntry[]) => list.some((x) => x.name.toLowerCase() === n.toLowerCase());
    if (taken(entries)) {
      w.fail("folder-add", `A folder is already named ${n}.`);
      return;
    }
    const added = await write(
      (now) => (taken(now) ? `A folder is already named ${n}.` : [...now, { name: n, kind: "lanes", path: p }]),
      "folder-add",
    );
    if (added) {
      setName("");
      setPath("");
      closeAdd();
    }
  }
  return (
    <div className="section-card">
      <div className="section-head">
        Job runner folders<span className="spacer" />
        <button
          ref={addBtn}
          type="button"
          className="sm"
          aria-expanded={adding}
          disabled={!cfg}
          onClick={() => setAdding((v) => !v)}
        >
          Add a folder…
        </button>
      </div>
      <div className="section-body">
        {adding ? (
          <form className="route-pause route-folder-form" aria-label="Add a job runner folder" onSubmit={add} onKeyDown={escapeCloses(closeAdd)}>
            <div className="toolbar">
              <label htmlFor={`${base}-name`}>Name</label>
              <input id={`${base}-name`} value={name} autoFocus autoComplete="off" placeholder="Project A…" onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="toolbar">
              <label htmlFor={`${base}-path`}>Folder</label>
              <input
                id={`${base}-path`}
                className="mono"
                value={path}
                autoComplete="off"
                spellCheck={false}
                placeholder="/path/to/the/runner's state folder…"
                onChange={(e) => setPath(e.target.value)}
              />
            </div>
            <WriteError w={w} id="folder-add" />
            <div className="toolbar">
              <button type="submit" className="primary sm" disabled={w.isBusy("folder-add")}>
                <Busy on={w.isBusy("folder-add")} />
                Add folder
              </button>
              <button type="button" className="sm link" onClick={closeAdd}>
                Cancel
              </button>
            </div>
          </form>
        ) : null}
        {entries.length ? (
          <div className="rows" role="list" aria-label="Job runner folders">
            {entries.map((entry) => {
              const r = runners.find((x) => x.name === entry.name);
              const st = refused.includes(entry.name)
                ? { text: "Refused: it is inside a private folder", tone: "err" as const }
                : runnerState(r, now);
              const id = `folder-remove:${entry.name}`;
              return (
                <div key={entry.name} className="row-item route-acct" role="listitem">
                  <span className="route-name">{entry.name}</span>
                  <span className="route-what">
                    <span className="route-state">
                      <span className={st.tone ? `hdot ${st.tone}` : "hdot"} aria-hidden />
                      {st.text}
                    </span>
                    <span className="route-src">{runnerDetail(entry, r, now)}</span>
                  </span>
                  <span className="route-acts">
                    <button
                      ref={(el) => {
                        removeBtns.current[entry.name] = el;
                      }}
                      type="button"
                      className="sm"
                      aria-label={`Remove the ${entry.name} folder`}
                      aria-expanded={removing === entry.name}
                      onClick={() => setRemoving(removing === entry.name ? null : entry.name)}
                    >
                      Remove
                    </button>
                  </span>
                  {removing === entry.name ? (
                    <ConfirmGroup
                      label={`Remove the ${entry.name} folder?`}
                      onClose={() => setRemoving(null)}
                      back={() => removeBtns.current[entry.name] ?? addBtn.current}
                    >
                      <span>
                        Stop reading {entry.name}? Its limits and heartbeat stop counting; the folder itself is not
                        touched.
                      </span>
                      <WriteError w={w} id={id} />
                      <div className="toolbar">
                        <button
                          type="button"
                          className="sm danger"
                          disabled={w.isBusy(id)}
                          onClick={async () => {
                            if (await write((now) => now.filter((x) => x.name !== entry.name), id)) setRemoving(null);
                          }}
                        >
                          <Busy on={w.isBusy(id)} />
                          Remove {entry.name}
                        </button>
                        <button type="button" className="sm link" onClick={() => setRemoving(null)}>
                          Keep it
                        </button>
                      </div>
                    </ConfirmGroup>
                  ) : null}
                </div>
              );
            })}
          </div>
        ) : recordRead !== "ready" ? (
          <RecordReadLine read={recordRead} what="job runner folders" />
        ) : (
          <p className="set-help">No job runner folders yet. Add one to let Khipu read a runner's limits and heartbeat.</p>
        )}
        <div className="rows">
          <LimitRow
            name="Say a job runner stopped reporting after"
            help="Counted in the runner's own heartbeat intervals."
            fields={[{ key: "routing_runner_silent_intervals", label: "Stopped after", unit: "intervals", options: [2, 3, 5, 10, 20, 0], zero: "Never" }]}
            cfg={cfg}
            w={w}
            reread={reread}
          />
          <LimitRow
            name="How much of the end of a runner's events.log is read"
            help="Older lines are left unread."
            fields={[
              {
                key: "routing_runner_tail_bytes",
                label: "Read the last",
                unit: "bytes",
                options: [128 * 1024, 512 * 1024, 1024 * 1024, 4 * 1024 * 1024, 16 * 1024 * 1024, 0],
                zero: "All of it",
              },
            ]}
            cfg={cfg}
            w={w}
            reread={reread}
          />
          <LimitRow
            name="Newest limit lines kept from each runner"
            help="The runner's lines that say a lane reached a limit."
            fields={[{ key: "routing_runner_limited_lines", label: "Keep", unit: "lines", options: [50, 100, 200, 500, 1000, 0], zero: "No limit" }]}
            cfg={cfg}
            w={w}
            reread={reread}
          />
          <LimitRow
            name="Look at the folders and the routing table every"
            help="Off: Khipu still updates when a limit or a hold runs out, and whenever it is asked."
            fields={[{ key: "routing_poll_interval", label: "Look every", unit: "time", options: [5, 10, 30, 60, 5 * M, 0], zero: "Off" }]}
            cfg={cfg}
            w={w}
            reread={reread}
          />
        </div>
        <p className="set-help">
          Khipu only reads these folders. It never writes to them, never changes a runner's settings, and reads only
          its limits, its job starts, its heartbeat and the few lane settings it needs to watch your rules. Every line
          it keeps is checked for secrets first. A folder inside API Keys, My Portfolio or any sign-in file is refused.
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Which account each job lane uses
// ---------------------------------------------------------------------------

type LaneMap = Record<string, Record<string, string>>;

function LaneRow({
  runner,
  lane,
  target,
  accounts,
  w,
  reread,
}: {
  runner: string;
  lane: string;
  target: string | undefined;
  accounts: RouteAccount[];
  w: Writer;
  reread: () => Promise<unknown>;
}) {
  const known = target ? accounts.find((a) => a.label === target || a.key === target) : undefined;
  const [open, setOpen] = useState(!target);
  const [choice, setChoice] = useState(known?.label ?? "");
  const selId = `lane-${useId().replace(/:/g, "")}`;
  const id = `lane:${runner}/${lane}`;
  async function save(e: FormEvent) {
    e.preventDefault();
    // The change is made to the lane map as Khipu has it just before the
    // write, so two lanes saved before the first re-read both keep their
    // accounts (Codex review, finding 1).
    const ok = await w.run(
      id,
      "khipu_config_set",
      async () => {
        const cur = await currentSetting("routing_lane_map");
        if (typeof cur === "string") return cur;
        const raw = cur.value;
        const next: LaneMap = raw && typeof raw === "object" && !Array.isArray(raw) ? JSON.parse(JSON.stringify(raw)) : {};
        const lanes = { ...(next[runner] ?? {}) };
        if (choice) lanes[lane] = choice;
        else delete lanes[lane];
        if (Object.keys(lanes).length) next[runner] = lanes;
        else delete next[runner];
        return { key: "routing_lane_map", value: JSON.stringify(next) };
      },
      reread,
    );
    if (ok && choice) setOpen(false);
  }
  return (
    <div className="row-item route-lane" role="listitem">
      <span className="mono">{lane}</span>
      <span aria-hidden>→</span>
      {open ? (
        <form className="toolbar" aria-label={`Account for ${runner}'s ${lane} lane`} onSubmit={save}>
          {!target ? <Tag tone="warn">Not matched</Tag> : null}
          <label className="sr-only" htmlFor={selId}>
            Account for {lane}
          </label>
          <select id={selId} value={choice} onChange={(e) => setChoice(e.target.value)}>
            <option value="">{target ? "Not matched" : "Choose an account…"}</option>
            {accounts.map((a) => (
              <option key={a.key} value={a.label}>
                {a.label}
              </option>
            ))}
          </select>
          <button type="submit" className="sm primary" disabled={w.isBusy(id) || (!target && !choice)}>
            <Busy on={w.isBusy(id)} />
            Save
          </button>
          {target ? (
            <button type="button" className="sm link" onClick={() => setOpen(false)}>
              Cancel
            </button>
          ) : null}
        </form>
      ) : (
        <span>
          {known?.label ?? target}
          {!known ? (
            <>
              {" "}
              <Tag tone="warn">Not an account Khipu knows</Tag>
            </>
          ) : null}
        </span>
      )}
      {!open ? (
        <span className="route-acts">
          <button
            type="button"
            className="sm"
            aria-label={`Change the account for ${runner}'s ${lane} lane`}
            onClick={() => {
              setChoice(known?.label ?? "");
              setOpen(true);
            }}
          >
            Change…
          </button>
        </span>
      ) : (
        <span />
      )}
      {w.errors[id] ? (
        <div className="route-pause">
          <WriteError w={w} id={id} />
        </div>
      ) : null}
    </div>
  );
}

function LanesSection({
  data,
  cfg,
  recordRead = "ready",
  w,
  reread,
}: {
  data: RouteView | null;
  cfg: RouteConfig | null;
  recordRead?: RecordRead;
  w: Writer;
  reread: () => Promise<unknown>;
}) {
  const raw = cfg?.routing?.routing_lane_map?.value;
  const map: LaneMap = raw && typeof raw === "object" && !Array.isArray(raw) ? (raw as LaneMap) : {};
  const runners = data?.sources?.runners ?? [];
  const accounts = data?.accounts ?? [];
  const names = [...new Set([...runners.map((r) => r.name), ...Object.keys(map)])];
  const unmatchedLimits = data?.unmatched ?? [];
  return (
    <div className="section-card">
      <div className="section-head">Which account each job lane uses</div>
      <div className="section-body">
        {recordRead !== "ready" ? (
          // Before the lane map is read every lane would look unmatched.
          <RecordReadLine read={recordRead} what="job runner lanes" />
        ) : names.length ? (
          names.map((name) => {
            const r = runners.find((x) => x.name === name);
            const lanes = [...new Set([...(r?.lanes ?? []), ...Object.keys(map[name] ?? {})])].sort();
            const headId = `lanes-${name.replace(/[^A-Za-z0-9_-]/g, "_")}`;
            return (
              <div key={name} className="rows">
                <h3 className="rows-head route-day" id={headId}>
                  {name}
                </h3>
                {lanes.length ? (
                  <div role="list" aria-labelledby={headId}>
                    {lanes.map((lane) => (
                      <LaneRow
                        key={lane}
                        runner={name}
                        lane={lane}
                        target={map[name]?.[lane]}
                        accounts={accounts}
                        w={w}
                        reread={reread}
                      />
                    ))}
                  </div>
                ) : (
                  <p className="set-help route-lanes-none">Khipu hasn't read any lanes from this runner's settings yet.</p>
                )}
              </div>
            );
          })
        ) : (
          <p className="set-help">Lanes show here once a job runner folder is added and read.</p>
        )}
        {unmatchedLimits.length ? (
          <p className="set-help" role="status">
            {unmatchedLimits.length === 1 ? "A limit came" : `${unmatchedLimits.length} limits came`} from a lane that
            isn't matched to an account. Match it above and the limit counts for that account.
          </p>
        ) : null}
        <p className="set-help">
          A limit on one lane closes every lane on the same account. A lane that isn't matched is never guessed;
          conversations are told a limit came from an unmatched lane.
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The routing table file and recent changes
// ---------------------------------------------------------------------------

function TableSection({
  data,
  cfg,
  w,
  now,
  reread,
}: {
  data: RouteView | null;
  cfg: RouteConfig | null;
  w: Writer;
  now: Date;
  reread: () => Promise<unknown>;
}) {
  const raw = cfg?.routing?.routing_table?.value;
  const path = typeof raw === "string" && raw ? raw : null;
  const table = data?.sources?.table ?? null;
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const btn = useRef<HTMLButtonElement>(null);
  const formId = `table-${useId().replace(/:/g, "")}`;
  const id = "table-file";
  let val = "Not read yet";
  if (table?.error) val = `Can't read it: ${tableErrorWords(table.error)}`;
  else if (table?.changed_at) val = `NOW block changed ${whenLocal(table.changed_at, now)}`;
  else if (table) val = "Read; no change seen yet";
  const close = () => {
    setOpen(false);
    w.fail(id, null);
    requestAnimationFrame(() => btn.current?.focus());
  };
  async function save(e: FormEvent) {
    e.preventDefault();
    const p = draft.trim();
    if (!(p.startsWith("/") || p.startsWith("~/"))) {
      w.fail(id, "Give the file's full path, starting with / or ~/.");
      return;
    }
    if (p === path) {
      close();
      return;
    }
    if (await w.run(id, "khipu_config_set", { key: "routing_table", value: p }, reread)) close();
  }
  async function stop() {
    if (await w.run(id, "khipu_config_unset", { key: "routing_table" }, reread)) close();
  }
  return (
    <div className="section-card">
      <div className="section-head">Routing table file</div>
      <div className="section-body">
        <div className="rows">
          <div className="row-item route-limit">
            <span className="route-limit-text">
              <span className="route-limit-name mono">{path ?? "No file set"}</span>
              <span className="set-help">
                Read only. Khipu notes when its NOW block changes; it never takes an account's state from it, and never
                writes to it.
              </span>
            </span>
            <span className="route-val">{path ? val : ""}</span>
            <button
              ref={btn}
              type="button"
              className="sm"
              aria-label={path ? "Change the routing table file" : "Choose the routing table file"}
              aria-expanded={open}
              aria-controls={open ? formId : undefined}
              disabled={!cfg}
              onClick={() => {
                if (open) {
                  close();
                  return;
                }
                setDraft(path ?? "");
                w.fail(id, null);
                setOpen(true);
              }}
            >
              {path ? "Change…" : "Choose…"}
            </button>
            {open ? (
              <form
                className="route-pause"
                id={formId}
                aria-label="Change the routing table file"
                onSubmit={save}
                onKeyDown={escapeCloses(close)}
              >
                <div className="toolbar">
                  <label htmlFor={`${formId}-path`}>File</label>
                  <input
                    id={`${formId}-path`}
                    className="mono"
                    value={draft}
                    autoFocus
                    autoComplete="off"
                    spellCheck={false}
                    placeholder="/path/to/ROUTING-NOW.md…"
                    onChange={(e) => setDraft(e.target.value)}
                  />
                </div>
                <p className="set-help">
                  The file's full path, starting with / or ~/. Khipu won't read a file inside a private folder.
                </p>
                <WriteError w={w} id={id} say={(m) => settingWords(m, "routing_table", "Routing table file")} />
                <div className="toolbar">
                  <button type="submit" className="primary sm" disabled={w.isBusy(id)}>
                    <Busy on={w.isBusy(id)} />
                    Save
                  </button>
                  {path ? (
                    <button type="button" className="sm" disabled={w.isBusy(id)} onClick={() => void stop()}>
                      Stop reading it
                    </button>
                  ) : null}
                  <button type="button" className="sm link" onClick={close}>
                    Cancel
                  </button>
                </div>
              </form>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  );
}

function ChangesSection({ data, w, now }: { data: RouteView; w: Writer; now: Date }) {
  const days = changeDays(data, now);
  const active = new Set((data.rules ?? []).map((r) => r.n));
  return (
    <div className="section-card">
      <div className="section-head">Recent changes</div>
      <div className="section-body route-days">
        {days.length ? (
          days.map((day) => {
            const headId = `route-day-${day.key}`;
            return (
              <div key={day.key} className="rows">
                <h3 className="rows-head route-day" id={headId}>
                  {day.label}
                </h3>
                <div role="list" aria-labelledby={headId}>
                  {day.rows.map((row) => {
                    const id = row.restore != null ? `rule-restore:${row.restore}` : "";
                    return (
                      <div key={row.key} className="row-item route-event" role="listitem">
                        <span className="row-meta">
                          {new Date(row.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}
                        </span>
                        <span className="row-main">
                          <b>{row.subject}</b> {row.text}
                          {row.aside ? <span className="t2"> {row.aside}</span> : null}
                          {row.restore != null && !active.has(row.restore) ? (
                            <>
                              {" "}
                              <button
                                type="button"
                                className="sm"
                                disabled={w.isBusy(id)}
                                aria-label={`Bring back standing rule ${row.restore}`}
                                onClick={() => {
                                  const n = row.restore!;
                                  void w.run(id, "khipu_route_rule", { action: "restore", n }, null, () => ruleRemoveButton(n));
                                }}
                              >
                                <Busy on={w.isBusy(id)} />
                                Bring it back
                              </button>
                              <WriteError w={w} id={id} />
                            </>
                          ) : null}
                        </span>
                        <span className="route-who">{row.who}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })
        ) : (
          <p className="set-help">No changes yet.</p>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

export function RoutingSettings({ active, plans }: { active: boolean; plans?: UsageReport | null }) {
  const route = useRouting(active);
  const config = useCliState<RouteConfig>("khipu_config_show", active);
  const features = useCliState<FeaturesShape>("khipu_features_show", active);
  const rereadRoute = () => route.load(true);
  const rereadConfig = async () => {
    await config.reload();
    await route.load(true);
  };
  const w = useWriter(rereadRoute);
  const now = new Date();
  const data = route.data;
  // Folders and lanes come from both the routing record and the settings;
  // until both are read they say so, never "none yet".
  const recordRead: RecordRead =
    data && config.data ? "ready" : (!data && route.error) || (!config.data && config.error) ? "failed" : "reading";
  return (
    <div className="route-settings">
      <p className="set-help set-intro">
        Khipu tells every conversation which accounts can take work, what you have paused and what your rules are. It
        never sends, stops or reorders work; your agents decide.
      </p>
      {route.error ? (
        <p className="set-error" role="alert">
          {route.error}
        </p>
      ) : null}
      {data && data.record === false ? (
        <div className="section-card">
          <div className="section-head">No routing record on this Mac</div>
          <div className="section-body">
            <p className="set-help">
              Khipu starts one when a plan check, a job runner folder or a conversation reports on an account, or when
              you add a rule, a note or a hold here. Until then no account is shown as open.
            </p>
          </div>
        </div>
      ) : null}
      {data ? (
        <>
          <AccountsSection data={data} plans={plans} cfg={config.data} w={w} now={now} reread={rereadConfig} />
          <NoteSection data={data} w={w} now={now} />
          <RulesSection data={data} w={w} now={now} />
          <HoldsSection data={data} cfg={config.data} w={w} now={now} reread={rereadConfig} />
          <ResetChecksSection data={data} cfg={config.data} cfgError={config.error} w={w} reread={rereadConfig} />
        </>
      ) : !route.error ? (
        <p className="set-help" role="status">
          Reading the routing record…
        </p>
      ) : null}
      <TellingSection
        data={data}
        cfg={config.data}
        features={features.data}
        featuresError={features.error}
        cfgError={config.error}
        w={w}
        reread={rereadConfig}
        rereadFeatures={features.reload}
      />
      <FoldersSection data={data} cfg={config.data} recordRead={recordRead} w={w} now={now} reread={rereadConfig} />
      <LanesSection data={data} cfg={config.data} recordRead={recordRead} w={w} reread={rereadConfig} />
      <TableSection data={data} cfg={config.data} w={w} now={now} reread={rereadConfig} />
      {data ? <ChangesSection data={data} w={w} now={now} /> : null}
      {config.error && !config.data ? (
        <p className="set-error" role="alert">
          Could not read the settings: {config.error}
        </p>
      ) : null}
    </div>
  );
}
