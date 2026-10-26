/* Settings -> Accounts and routing. Since settings clarity phase 3
   (docs/plans/2026-10-15-settings-clarity.md) the page is nine groups that
   fold, with Needs you on top; the closed lines and problems come from
   routingOverview.ts. Before that: Settings -> Routing
   (docs/plans/2026-10-14-routing-truth.md, phase 4,
   second half; mock docs/mockups/2026-10-14-routing/settings-routing.html).
   Every option phases 1a to 3c built, each usable here and each limit
   raisable or removable on the spot: the accounts (each line opens its
   Routing detail, AccountControls.tsx, where Pause, Lift, Clear, Dismiss one
   report, holds, caps and the words live), the owner's note, the standing rules, holds,
   telling conversations, the providers and lanes group with the job runner
   folders folded under it (routing slice 15c), the routing table file and
   recent changes.

   Khipu reports; it never routes. Nothing here sends, stops or reorders
   work. Every write goes through one fixed-argv Tauri command that passes
   `--from-app` (src-tauri/src/lib.rs, `route_*_argv`) or `config --set`,
   then the page re-reads the record: what it shows is what the CLI reports.
   The switch before handing work to another agent (phase 6) and the copy for
   cloud agents (phase 7) join with their phases. The Mac-to-Mac replication
   switch sits beside the proposal-text switch in Sizes and times. */

import { useEffect, useId, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent, ReactNode, RefObject } from "react";
import { ChevronRight, Loader2 } from "lucide-react";
import { EmptyState, Tag } from "./ui";
import { callCli, useCliState } from "./settingsCli";
import type { FeaturesShape } from "./settingsCli";
import { PlanBlock, RefreshButton, UsageAccountsField, planPauses } from "./PlansCard";
import type { PlanUsageState, UsageAccount, UsageReport } from "./PlansCard";
import { inPlansOrder } from "./RoutingCard";
import { PlanDetailSheet, RoutingDetailSheet, useDetailHost } from "./AccountDetail";
import { AccountsRequestBand } from "./AccountControls";
import { GROUPS, accountTags, accountWeek, lanesWithoutAccount, providerRows, routingOverview, staleAfterSeconds } from "./routingOverview";
import type { GroupKey, GroupTag, OverviewSettings, Problem, ProviderRow } from "./routingOverview";
import { paceModel } from "./routingPace";
import type { PaceTrend, PaceWindow } from "./routingPace";
import { checksModel, fileCopySkipWords } from "./routingChecks";
import { endedWords, holdChoiceIds, holdChoiceTargets, proposalsModel } from "./routingProposals";
import type { ProposalRow } from "./routingProposals";
import {
  ACCOUNT_FILTERS,
  ACCOUNT_SORTS,
  CHANGE_ABOUT,
  CHANGE_BY,
  SEARCH_OVER,
  accountHasProblem,
  accountInFilter,
  matchesQuery,
  plural,
  sortAccounts,
  useListControls,
} from "./routingLists";
import type { AccountFilter, AccountSort, ChangeAboutFilter, ChangeByFilter, ListControls, RuleFilter } from "./routingLists";
import "./routingOverview.css";
import { FinderBottom, FinderTop, foundCount, useAccountsFinder } from "./FoundAccounts";
import type { AccountsFinder, FinderConfig } from "./FoundAccounts";
import {
  accountSource,
  accountState,
  capitalise,
  disagreementWords,
  deliveryConversationLabels,
  deliveryHarnessName,
  deliveryReason,
  holdWords,
  modelDisagreementWords,
  pastWords,
  ROUTE_READ_FAILED,
  priorityPendingWords,
  ruleResultLines,
  saidWhen,
  useRouting,
  whenLocal,
} from "./routing";
import type {
  RouteAccount,
  RouteDeliveryFreshness,
  RouteDeliveryHealth,
  RouteDeliveryIdentity,
  RouteDeliveryRow,
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
import type { ChangeRow, LimitUnit } from "./routingWords";

/** A conversation's change to a routing setting at your request, made and
 *  marked until you confirm it or undo it (the CLI's ``request``). */
type SettingRequest = { request?: string | null; by_words?: string | null; at?: string | null; prior?: unknown };
type RouteSetting = {
  value: unknown;
  default: unknown;
  kind: string;
  source: string;
  words?: string;
  request?: SettingRequest | null;
  /** A conversation's change is waiting for the owner; `value` is still the live state. */
  pending?: boolean;
};
type RouteConfig = { routing?: Record<string, RouteSetting> };

/** Where focus goes when the control that made a change is gone. */
export type Back = () => HTMLElement | null | undefined;

/** The most words the app hands the CLI in one change, in KB of text: the
 *  size of one argument, not a limit the owner chose (src-tauri lib.rs,
 *  ROUTE_WORDS_MAX_BYTES; fix round 2, R2-4). */
const WORDS_MOST_KB = 128;

/** A write's arguments, or a function that builds them from state read just
 *  before the write runs (a string is a refusal in words: nothing is sent). */
export type WriteArgs = Record<string, unknown> | undefined | (() => Promise<Record<string, unknown> | string>);

export type Writer = {
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
  /** The error under `id` as it stands now, readable in the same tick a
   *  write's promise settles (the state above lands a render later). */
  errorOf: (id: string) => string | undefined;
};

export function useWriter(reread: () => Promise<unknown>): Writer {
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
  const errorsNow = useRef<Record<string, string>>({});
  const fail = (id: string, message: string | null) => {
    if (message) errorsNow.current[id] = message;
    else delete errorsNow.current[id];
    setErrors((prev) => {
      const out = { ...prev };
      if (message) out[id] = message;
      else delete out[id];
      return out;
    });
  };
  const run: Writer["run"] = (id, command, args, after, back) => {
    const at = document.activeElement;
    const from = at instanceof HTMLElement && at !== document.body ? at : null;
    setPending((p) => [...p, id]);
    const op = crypto.randomUUID();
    const job = queue.current.then(async () => {
      const k = { from, back, done: false };
      keep.current = k;
      let ok = false;
      try {
        const built = typeof args === "function" ? await args() : args;
        if (typeof built === "string") {
          fail(id, built);
        } else {
          const res = await callCli(command, command.startsWith("khipu_route_") && command !== "khipu_route_check"
            ? { ...built, op } : built);
          await (after ?? reread)();
          fail(
            id,
            res.ok
              ? null
              : res.refusal?.code === "version_moved"
                ? "This was changed somewhere else after this page read it. Khipu read it again; check it and try once more."
                : res.error,
          );
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
  return { isBusy: (id) => pending.includes(id), anyBusy: pending.length > 0, errors, results, run, fail, errorOf: (id: string) => errorsNow.current[id] };
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
export function WriteError({ w, id, say }: { w: Writer; id: string; say?: (msg: string) => string }) {
  const msg = w.errors[id];
  if (!msg) return null;
  return (
    <p className="set-error" role="alert">
      Khipu didn't make that change: {say ? say(msg) : msg}
    </p>
  );
}


/** Gives focus to `el()` once the click that removed the focused button has
 *  rendered, so keyboard focus never drops to the page. */
export function focusAfter(el: () => HTMLElement | null | undefined): void {
  requestAnimationFrame(() => el()?.focus());
}

export function Busy({ on }: { on: boolean }) {
  return on ? <Loader2 size={14} className="spin" aria-hidden /> : null;
}

/** Escape cancels an inline form and gives focus back to its button. */
export function escapeCloses(close: () => void) {
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
export function ConfirmGroup({
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
// End or keep: a write that names a held or paused account is refused until
// the owner says, for each one, whether it ends or stays (plan section 2,
// decision 4). The CLI names the ids in its refusal; this asks and retries
// the same write with the choices. Khipu never reads a release from prose.
// ---------------------------------------------------------------------------

type HoldAsk = {
  ids: string[];
  retry: (holds: Record<string, "end" | "keep">) => Promise<boolean>;
};

/** `attempt` runs a write; on the end-or-keep refusal it keeps the retry
 *  (with the choices merged into the same write's arguments) for the panel
 *  below. Returns the write's own result. */
export function useEndOrKeep(w: Writer) {
  const [ask, setAsk] = useState<HoldAsk | null>(null);
  const attempt = async (
    id: string,
    command: string,
    args: Record<string, unknown>,
    after?: (() => Promise<unknown>) | null,
    back?: Back,
    onRetrySuccess?: () => void,
  ): Promise<boolean> => {
    const ok = await w.run(id, command, args, after, back);
    if (ok) return true;
    const ids = holdChoiceIds(w.errorOf(id));
    if (!ids.length) return false;
    setAsk({
      ids,
      retry: async (holds) => {
        const done = await w.run(id, command, { ...args, holds }, after, back);
        if (done) {
          setAsk(null);
          onRetrySuccess?.();
          return true;
        }
        const again = holdChoiceIds(w.errorOf(id));
        if (again.length) setAsk((cur) => (cur ? { ...cur, ids: again } : cur));
        return false;
      },
    });
    return false;
  };
  return { ask, setAsk, attempt };
}

export function EndOrKeepChoices({
  view,
  ask,
  now,
  busy,
  saveLabel,
  onCancel,
}: {
  view: RouteView;
  ask: HoldAsk;
  now: Date;
  busy: boolean;
  saveLabel: string;
  onCancel: () => void;
}) {
  const [choices, setChoices] = useState<Record<string, "end" | "keep">>({});
  const targets = holdChoiceTargets(view, ask.ids, now);
  const ready = targets.every((t) => choices[t.id]);
  return (
    <div className="route-choice" role="group" aria-label="End or keep each hold">
      <p className="set-help">
        <b>That change wasn't saved yet.</b> It names {targets.length === 1 ? "an account that is held or paused" : "accounts that are held or paused"}.
        Say for each one whether its hold or pause ends or stays.
      </p>
      {targets.map((t) => (
        <fieldset key={t.id}>
          <legend>{t.legend}</legend>
          <label>
            <input type="radio" name={`hold-choice-${t.id}`} checked={choices[t.id] === "end"}
              onChange={() => setChoices((c) => ({ ...c, [t.id]: "end" }))} />
            {t.endLabel}
          </label>
          <label>
            <input type="radio" name={`hold-choice-${t.id}`} checked={choices[t.id] === "keep"}
              onChange={() => setChoices((c) => ({ ...c, [t.id]: "keep" }))} />
            {t.keepLabel}
          </label>
        </fieldset>
      ))}
      <div className="toolbar">
        <button type="button" className="primary sm" disabled={!ready || busy}
          onClick={() => void ask.retry(choices)}>
          <Busy on={busy} />
          {saveLabel}
        </button>
        <button type="button" className="sm link" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// When something ends: the pause form and the hold form share these choices.
// ---------------------------------------------------------------------------

export function EndFields({
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

export function DisagreePanel({ a, w, now, back }: { a: RouteAccount; w: Writer; now: Date; back: Back }) {
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

/** Each routing setting a conversation changed at your request: what it
 *  said you asked, and Confirm or Undo (the value it replaced comes back). */
function SettingRequests({ cfg, w, reread }: { cfg: RouteConfig | null; w: Writer; reread: () => Promise<unknown> }) {
  const waiting = Object.entries(cfg?.routing ?? {}).filter(([, s]) => s.request);
  if (!waiting.length) return null;
  return (
    <>
      {waiting.map(([key, s]) => (
        <div key={key} className="route-disagree" role="group" aria-label={`A conversation changed: ${s.words ?? key}`}
          data-route-item={`setting:${key}`} tabIndex={-1}>
          <span>
            <b>
              {s.words ?? key}: changed by {s.request?.by_words || "a conversation"} at your request; you haven't
              confirmed it yet.
            </b>
          </span>
          {s.request?.request ? <span>What it said you asked: “{s.request.request}”</span> : null}
          <div className="toolbar">
            <button type="button" className="sm primary" disabled={w.isBusy(`setting:${key}:confirm`)}
              onClick={() => void w.run(`setting:${key}:confirm`, "khipu_config_request", { key, action: "confirm" }, reread)}>
              <Busy on={w.isBusy(`setting:${key}:confirm`)} />
              Confirm
            </button>
            <button type="button" className="sm" disabled={w.isBusy(`setting:${key}:undo`)}
              onClick={() => void w.run(`setting:${key}:undo`, "khipu_config_request", { key, action: "undo" }, reread)}>
              <Busy on={w.isBusy(`setting:${key}:undo`)} />
              Undo
            </button>
          </div>
          <WriteError w={w} id={`setting:${key}:confirm`} />
          <WriteError w={w} id={`setting:${key}:undo`} />
        </div>
      ))}
    </>
  );
}

function ResetChecksSection({ data, cfg, cfgError, w, reread }: { data: RouteView | null; cfg: RouteConfig | null; cfgError?: string | null; w: Writer; reread: () => Promise<unknown> }) {
  const enabled = cfg?.routing?.routing_reset_check?.value === true;
  const next = (data?.accounts ?? []).filter((a) => a.next_check).map((a) => `${a.label}: ${whenLocal(a.next_check!, new Date())}`);
  return (
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
  attempt,
  onClose,
}: {
  p: RoutePriority;
  i: number;
  w: Writer;
  attempt: (id: string, command: string, args: Record<string, unknown>) => Promise<boolean>;
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
    if (await attempt(id, "khipu_route_priority", { action: "set", entries: next, until: p.until ?? null, expect: p.version ?? null })) onClose();
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

function PriorityEndForm({ p, w, attempt, onClose }: { p: RoutePriority; w: Writer; attempt: (id: string, command: string, args: Record<string, unknown>) => Promise<boolean>; onClose: () => void }) {
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
    if (await attempt(id, "khipu_route_priority", { action: "set", entries: priorityItems(p.entries), until: end.iso, expect: p.version ?? null })) onClose();
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
  // The version of the priority this page loaded, never one fetched at save time.
  const expect = p?.version ?? data.priority_version ?? null;
  const eok = useEndOrKeep(w);
  const send = (id: string, next: PriorityItem[], back?: Back) =>
    next.length
      ? eok.attempt(id, "khipu_route_priority", { action: "set", entries: next, until: p?.until ?? null, expect }, null, back)
      : w.run(id, "khipu_route_priority", { action: "clear", expect }, null, back);
  const move = (i: number, by: number) => {
    const next = priorityItems(entries);
    const [item] = next.splice(i, 1);
    next.splice(i + by, 0, item);
    void send(`priority:move:${entries[i].account}`, next);
  };
  const paceOn = cfg?.routing?.routing_pace_order?.value !== false;
  const ids = entries.map((e) => `priority:move:${e.account}`).concat(entries.map((e) => `priority:remove:${e.account}`));
  return (
    <div className="section-body route-priority-set" role="group" aria-label="Your priority">
      <p className="set-help">
        Your order of accounts, each with your words for its role and an end if you want one. Conversations hear it
        first, before the account lines, word for word. Khipu keeps your order as you set it and never picks or reorders
        for you; if the first account is limited, paused or down, conversations hear that beside it.
      </p>
      {p?.pending ? (
        <div className="route-disagree" role="group" aria-label="A conversation set your priority"
          data-route-item="priority:pending" tabIndex={-1}>
          <span>
            <b>{priorityPendingWords(p)}</b>
          </span>
          {p.owner_request && !p.proposal_withheld ? <span>What it said you asked: “{p.owner_request}”</span> : null}
          <span>
            It replaced:{" "}
            {p.replaced?.entries.length
              ? p.replaced.entries.map((e, i) => `${i + 1}. ${e.label}${e.role ? `: ${e.role}` : ""}`).join("; ")
              : "no priority"}
            .
          </span>
          <div className="toolbar">
            <button type="button" className="sm primary" disabled={w.isBusy("priority:confirm")}
              onClick={() => void eok.attempt("priority:confirm", "khipu_route_priority", { action: "confirm", expect })}>
              <Busy on={w.isBusy("priority:confirm")} />
              Confirm
            </button>
            <button type="button" className="sm" disabled={w.isBusy("priority:undo")}
              onClick={() => void w.run("priority:undo", "khipu_route_priority", { action: "undo", expect }, null, () => addSelect.current)}>
              <Busy on={w.isBusy("priority:undo")} />
              Undo
            </button>
          </div>
          <WriteError w={w} id="priority:confirm" />
          <WriteError w={w} id="priority:undo" />
        </div>
      ) : null}
      {eok.ask ? (
        <EndOrKeepChoices view={data} ask={eok.ask} now={now} busy={w.anyBusy} saveLabel="Save priority"
          onCancel={() => eok.setAsk(null)} />
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
                  {e.in_settings === false ? " · no longer tracked" : ""}
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
              {editing === i && p ? <PriorityEntryForm p={p} i={i} w={w} attempt={eok.attempt} onClose={() => setEditing(null)} /> : null}
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
      {endOpen && p ? <PriorityEndForm p={p} w={w} attempt={eok.attempt} onClose={() => setEndOpen(false)} /> : null}
      {clearing && p ? (
        <ConfirmGroup label="Clear your priority?" onClose={() => setClearing(false)} back={() => addSelect.current}>
          <span>Clear your priority? Conversations hear "No priority set" on their next prompt. It stays in Recent changes.</span>
          <WriteError w={w} id="priority:clear" />
          <div className="toolbar">
            <button type="button" className="sm danger" disabled={w.isBusy("priority:clear")}
              onClick={async () => {
                if (await w.run("priority:clear", "khipu_route_priority", { action: "clear", expect }, null, () => addSelect.current)) setClearing(false);
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

/** Which groups, account lines and inner folds are open, by key. */
type Folds = { isOpen: (key: string) => boolean; toggle: (key: string) => void; openKeys: (keys: string[]) => void };

/** One account as a line: name, state in words with its dot, the week and
 *  pace, and its problem tags. It opens the Routing detail, where every
 *  control for the account lives (one place, not a second copy here); Rename
 *  and Stop tracking open from its More menu. */
function AccountLine({
  a,
  data,
  plan,
  tags,
  staleAfterS,
  now,
  reread,
  finder,
  onPlansChanged,
}: {
  a: RouteAccount;
  data: RouteView;
  plan?: UsageAccount;
  tags: GroupTag[];
  staleAfterS: number;
  now: Date;
  reread: () => Promise<unknown>;
  finder?: AccountsFinder | null;
  onPlansChanged?: () => Promise<unknown> | void;
}) {
  const key = `account:${a.key}`;
  const st = accountState(a, now);
  const fact = data.plan_facts?.[a.key];
  const { week, pace } = a.configured !== false ? accountWeek(fact, now, plan, staleAfterS) : { week: null, pace: null };
  const detail = useDetailHost<true>();
  const planSheet = useDetailHost<true>();
  const managed = { key: a.key, label: a.label, harness: a.harness, home: homeOfKey(a.key) };
  // Rename and Stop tracking wait, as they did in the fold, for the account list to be read.
  const manageable = !!finder && finder.support.state === "here" && a.configured !== false;
  return (
    <div role="listitem">
      <div className="fold route-aline" data-open={false}>
        <button type="button" className="fold-btn" aria-haspopup="dialog" data-route-item={key}
          onClick={() => detail.open(true)}>
          <ChevronRight size={14} className="disclosure-chevron" aria-hidden />
          <span className="an">{a.label}</span>
          <span className="as">
            <span className={st.tone ? `hdot ${st.tone}` : "hdot"} aria-hidden />
            <span>{st.text}</span>
          </span>
          <span className="ap">
            {week ? <span className="ap-week">{week}</span> : null}
            {pace ? <span className="ap-pace">{pace}</span> : null}
          </span>
          <span className="at">
            {tags.map((t) => (
              <Tag key={t.text} tone={t.tone} dot>
                {t.text}
              </Tag>
            ))}
          </span>
        </button>
        {detail.target ? (
          <RoutingDetailSheet account={a} view={data} plans={plan} now={now} onClose={detail.close} onChanged={reread}
            onRename={manageable ? () => finder.openRename(managed) : undefined}
            onStop={manageable ? () => finder.openStop(managed) : undefined}
            onChooseFolder={manageable && plan?.ok === false && plan.error === "login_folder_missing" ? () => void finder.chooseFolderFor(managed, null) : undefined}
            onSeePlan={plan ? () => { detail.close(); planSheet.open(true); } : undefined} />
        ) : null}
        {planSheet.target && plan ? (
          <PlanDetailSheet row={plan} view={data} now={now} onClose={planSheet.close} onChanged={reread} onRefresh={onPlansChanged ? () => void onPlansChanged() : undefined} />
        ) : null}
      </div>
    </div>
  );
}

/** The login folder in an account key (`harness|folder`); null for a
 *  default login. */
function homeOfKey(key: string): string | null {
  const i = key.indexOf("|");
  return i >= 0 && key.slice(i + 1) ? key.slice(i + 1) : null;
}

function AddProviderAccount({ data, w }: { data: RouteView; w: Writer }) {
  const [open, setOpen] = useState(false);
  const [provider, setProvider] = useState("glm");
  const [label, setLabel] = useState("");
  const [slug, setSlug] = useState("main");
  const [saving, setSaving] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const prefix = useId();
  const id = "provider-account";
  const close = () => {
    if (saving) return;
    setOpen(false);
    focusAfter(() => trigger.current);
  };
  return <div className="route-form">
    {!open ? <button type="button" className="sm" ref={trigger} onClick={() => {
      w.fail(id, null);
      w.fail(`${id}:provider`, null);
      w.fail(`${id}:account`, null);
      setOpen(true);
    }}>Add provider account</button> : null}
    {open ? <form className="route-pause" aria-label="Add provider account" onKeyDown={escapeCloses(close)}
      onSubmit={async (e) => {
        e.preventDefault();
        if (saving) return;
        const accountId = `${provider}:${slug.trim()}`;
        if (!label.trim() || !/^[a-z0-9][a-z0-9-]*$/.test(slug.trim())) {
          w.fail(id, "Enter an account name and an ID using lowercase letters, numbers or hyphens.");
          return;
        }
        if (data.definitions?.accounts?.[accountId] || data.accounts?.some((a) => a.key === accountId)) {
          w.fail(id, "That account ID is already tracked. Choose another ID.");
          return;
        }
        w.fail(id, null);
        setSaving(true);
        try {
          // Keep owner facts already in the record, including restrictions absent from this form.
          if (!data.definitions?.providers?.[provider]) {
            const value = provider === "glm"
              ? { id: "glm", label: "GLM", family: "Zhipu", login: "none", probe: "none", lane_names: ["glm"],
                  models_allowed: ["glm-5.3", "glm-5.3-flash"] }
              : { id: "deepseek", label: "DeepSeek", family: "DeepSeek", login: "none", probe: "none", lane_names: ["deepseek"],
                  models_allowed: ["deepseek-flash"] };
            if (!await w.run(`${id}:provider`, "khipu_route_definition", { definition: "provider", value })) return;
          }
          if (await w.run(`${id}:account`, "khipu_route_definition", { definition: "account",
            value: { id: accountId, label: label.trim(), provider } }, null, () => trigger.current)) {
            setOpen(false);
            setLabel("");
            setSlug("main");
            focusAfter(() => trigger.current);
          }
        } finally { setSaving(false); }
      }}>
      <div className="toolbar">
        <label htmlFor={`${prefix}-provider`}>Provider</label>
        <select id={`${prefix}-provider`} value={provider} disabled={saving} autoFocus onChange={(e) => setProvider(e.target.value)}>
          <option value="glm">GLM</option><option value="deepseek">DeepSeek</option>
        </select>
        <label htmlFor={`${prefix}-name`}>Account name</label>
        <input id={`${prefix}-name`} value={label} disabled={saving} onChange={(e) => setLabel(e.target.value)} />
        <label htmlFor={`${prefix}-id`}>Account ID</label>
        <input id={`${prefix}-id`} value={slug} disabled={saving} onChange={(e) => setSlug(e.target.value)} />
      </div>
      <p className="set-help">{provider === "glm" ? "Models: glm-5.3, glm-5.3-flash." : "Model: deepseek-flash."} Existing provider rules are kept. Usage comes from runner reports.</p>
      <WriteError w={w} id={id} /><WriteError w={w} id={`${id}:provider`} /><WriteError w={w} id={`${id}:account`} />
      <div className="toolbar">
        <button type="submit" className="primary sm" disabled={saving}><Busy on={saving} />Add account</button>
        <button type="button" className="sm link" disabled={saving} onClick={close}>Cancel</button>
      </div>
    </form> : null}
  </div>;
}

function AccountsSection({
  data,
  plansState,
  problems,
  lists,
  active,
  staleAfterS,
  w,
  now,
  reread,
  finder,
}: {
  data: RouteView;
  plansState?: PlanUsageState | null;
  problems: Problem[];
  lists: ListControls;
  staleAfterS: number;
  active: boolean;
  w: Writer;
  now: Date;
  reread: () => Promise<unknown>;
  finder?: AccountsFinder | null;
}) {
  const plans = plansState?.data ?? null;
  const accounts = inPlansOrder(data.accounts ?? [], plans);
  const { accountSort, accountFilter } = lists.prefs;
  // Sorting and filtering only change what shows; nothing is written.
  const sorted = sortAccounts(accounts, accountSort, data, now);
  const shownAccounts = sorted.filter((a) => accountInFilter(a, accountFilter, problems));
  const filterCounts = Object.fromEntries(
    ACCOUNT_FILTERS.map((f) => [f.key, accounts.filter((a) => accountInFilter(a, f.key, problems)).length]),
  ) as Record<AccountFilter, number>;
  const hiddenProblems = sorted.filter((a) => !accountInFilter(a, accountFilter, problems) && accountHasProblem(problems, a.key)).length;
  const sortId = `route-sort-${useId().replace(/:/g, "")}`;
  const filterLabel = useId();
  const rows = plans?.accounts ?? [];
  const pauses = planPauses(data, now);
  const routed = (row: UsageAccount) => row.key != null && accounts.some((a) => a.key === row.key);
  const loose = rows.filter((row) => !routed(row));
  const [changing, setChanging] = useState(false);
  const fieldId = `route-accounts-read-${useId().replace(/:/g, "")}`;
  const acctChips = useRef<HTMLDivElement>(null);
  const showAll = () => {
    lists.set("accountFilter", "all");
    focusAfter(() => acctChips.current?.querySelector("button"));
  };
  // Whose plans Khipu reads: the plan read's names, else the record's.
  const names = (plans ? rows.map((r) => r.account || r.harness || "an account") : accounts.map((a) => a.label)).join(", ");
  return (
    <div className="section-body">
      {finder ? <AccountsRequestBand support={finder.support} reload={finder.reload} onChanged={() => void reread()} /> : null}
      {finder ? <FinderTop finder={finder} rows={rows} /> : null}
      <AddProviderAccount data={data} w={w} />
      {accounts.length ? (
        <>
          <div className="route-filter">
            <label className="lbl" htmlFor={sortId}>
              Sort by
            </label>
            <select id={sortId} className="sort" value={accountSort} onChange={(e) => lists.set("accountSort", e.target.value as AccountSort)}>
              {ACCOUNT_SORTS.map((o) => (
                <option key={o.key} value={o.key}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
          <div className="route-filter">
            <span className="lbl" id={filterLabel}>
              Show accounts
            </span>
            <div className="chips" role="group" aria-labelledby={filterLabel} ref={acctChips}>
              {ACCOUNT_FILTERS.map((f) => (
                <button
                  key={f.key}
                  type="button"
                  className={`chip${accountFilter === f.key ? " on" : ""}`}
                  aria-pressed={accountFilter === f.key}
                  aria-label={`${f.label}, ${plural(filterCounts[f.key], "account", "accounts")}`}
                  onClick={() => lists.set("accountFilter", f.key)}
                >
                  {f.label} <span className="n" aria-hidden>{filterCounts[f.key]}</span>
                </button>
              ))}
            </div>
          </div>
          {shownAccounts.length ? (
            <div className="route-alist" role="list" aria-label="Accounts">
              {shownAccounts.map((a) => (
                <AccountLine key={a.key} a={a} data={data} plan={rows.find((r) => r.key != null && r.key === a.key)}
                  tags={accountTags(problems, a.key)} staleAfterS={staleAfterS} now={now} reread={reread} finder={finder}
                  onPlansChanged={plansState?.reread} />
              ))}
            </div>
          ) : (
            <p className="set-help" role="status">
              No accounts here.{" "}
              <button type="button" className="sm link" onClick={showAll}>
                Show all {accounts.length} accounts
              </button>
            </p>
          )}
          {hiddenProblems ? (
            <p className="set-help" role="status">
              {hiddenProblems === 1 ? "1 account that needs a look is" : `${hiddenProblems} accounts that need a look are`} hidden by
              this filter.{" "}
              <button type="button" className="sm link" onClick={showAll}>
                Show all accounts
              </button>
            </p>
          ) : null}
        </>
      ) : finder?.none ? null : (
        <EmptyState
          title="No accounts to show"
          hint="Add an account under Which accounts Khipu reads, at the end of this group, and it shows here."
        />
      )}
      {loose.length ? (
        <div className="plan-blocks">
          {loose.map((row, i) => (
            <div key={`${row.harness}|${row.home ?? ""}|${i}`} data-route-item={`plan:${row.key ?? (row.account || row.harness || "An account")}`}
              tabIndex={-1}>
              <PlanBlock row={row} now={now} pause={row.key ? pauses.get(row.key) : undefined} onSignedIn={plansState?.reread} />
            </div>
          ))}
        </div>
      ) : null}
      {plansState ? (
        <>
          {plansState.error ? (
            <p className="set-error" role="alert">
              {plansState.error}
            </p>
          ) : null}
          {plansState.loading && plansState.data == null ? (
            <p className="set-help" role="status">
              Reading plan usage…
            </p>
          ) : null}
          <div className="toolbar">
            <RefreshButton busy={plansState.refreshing || plansState.loading} label="Check all now"
              onClick={() => void plansState.refresh()} />
            <span className="muted">Asks every CLI again now; takes up to about seven seconds. No model call is made.</span>
          </div>
        </>
      ) : null}
      <p className="set-help">
        The same accounts as Plans, from the list at the end of this group. Reports come from four places: each
        account's plan check, your job runners, conversations, and you. Your routing table is not one of them.
      </p>
      <p className="set-help">
        Mark paused tells conversations you want an account left alone; Lift ends it. Clear ends a limit or a
        "down" report at once, and Khipu treats the account as open until something newer reports on it; if Claude
        or a job runner reports the limit again, it comes back.
      </p>
      <div className="acct-foot">
        <span>
          Which accounts Khipu reads:{" "}
          {plans ? names || "none" : names || (plansState?.error ? "Khipu couldn't read the list" : "Reading…")}
        </span>
        <span className="spacer" />
        <button type="button" className="sm" aria-label="Change which accounts Khipu reads" aria-expanded={changing}
          aria-controls={changing ? fieldId : undefined} onClick={() => setChanging((v) => !v)}>
          Change…
        </button>
      </div>
      {changing ? (
        <div id={fieldId}>
          <UsageAccountsField active={active} onChanged={plansState?.reread} />
        </div>
      ) : null}
      {finder ? <FinderBottom finder={finder} /> : null}
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
    <div className="section-body">
        <h2 className="sub-title">
          Your note to every conversation
          {written ? <span className="meta">{` · ${written}`}</span> : null}
        </h2>
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
  );
}

// ---------------------------------------------------------------------------
// Standing rules
// ---------------------------------------------------------------------------

type RuleAsk = { id: string; kind: "confirm" | "edit" | "remove" } | null;

/** A replicated rule can be live before it has a display number. Its entity
 * is the stable CLI target for every action during that interval. */
function ruleTarget(r: RouteRule): Record<string, unknown> {
  return r.number_pending && r.entity ? { entity: r.entity } : { n: r.n };
}

/** A replicated rule can have an entity before a display number. Controls
 * must still name the rule in the owner's words, never expose `null`. */
function ruleName(r: RouteRule): string {
  return typeof r.n === "number" ? String(r.n) : r.text || r.entity || "this rule";
}

/** UI identity stays distinct while a replicated rule awaits its display number. */
function ruleIdentity(r: RouteRule): string {
  return r.entity ?? String(r.n);
}

/** A standing rule's Remove button: where focus goes once the rule is back. */
function ruleRemoveButton(id: string, n?: number): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[data-route-item="rule:${selectorValue(id)}"] button[aria-label^="Remove rule "]`)
    ?? (n == null ? null : document.querySelector<HTMLElement>(`.route-rule-list button[aria-label="Remove rule ${n}"]`));
}

function RuleRow({
  r,
  watched,
  result,
  ask,
  setAsk,
  w,
  attempt,
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
  attempt: (id: string, command: string, args: Record<string, unknown>, after?: (() => Promise<unknown>) | null, back?: Back, onRetrySuccess?: () => void) => Promise<boolean>;
  now: Date;
  onRemoved: (r: RouteRule) => void;
  back: Back;
}) {
  const [draft, setDraft] = useState(r.text);
  const [editConfirm, setEditConfirm] = useState(false);
  const identity = ruleIdentity(r);
  const mine = ask?.id === identity ? ask.kind : null;
  const pending = !!r.pending;
  const toConfirm = pending || (r.seeded && !r.confirmed);
  const name = ruleName(r);
  const id = `rule:${identity}`;
  // Overview links use the durable display number for standing rules; only a
  // replicated rule without one needs its entity as the DOM target.
  const routeItem = `rule:${typeof r.n === "number" ? r.n : identity}`;
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
    <li className={`row-item route-rule${watched && result?.result === "broken" ? " broken" : ""}`}
      data-route-item={routeItem} tabIndex={-1}>
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
          <button ref={confirmBtn} type="button" className="sm" aria-label={`Confirm rule ${name}`} aria-expanded={mine === "confirm"} onClick={() => setAsk({ id: identity, kind: "confirm" })}>
            Confirm
          </button>
        ) : null}
        {pending ? (
          <button
            type="button"
            className="sm"
            disabled={w.isBusy(`${id}:undo`)}
            aria-label={`Undo pending rule ${name}`}
            onClick={() => void w.run(`${id}:undo`, "khipu_route_rule", { action: r.pending_kind === "added" ? "dismiss" : "undo", ...ruleTarget(r) }, null, back)}
          >
            <Busy on={w.isBusy(`${id}:undo`)} />
            Undo
          </button>
        ) : null}
        <button
          ref={editBtn}
          type="button"
          className="sm"
          aria-label={`Edit rule ${name}`}
          aria-expanded={mine === "edit"}
          onClick={() => {
            setDraft(r.text);
            setEditConfirm(false);
            setAsk({ id: identity, kind: "edit" });
          }}
        >
          Edit
        </button>
        <button ref={removeBtn} type="button" className="sm" aria-label={`Remove rule ${name}`} aria-expanded={mine === "remove"} onClick={() => setAsk({ id: identity, kind: "remove" })}>
          Remove
        </button>
      </span>
      {pending ? <WriteError w={w} id={`${id}:undo`} /> : null}
      {mine === "confirm" ? (
        <ConfirmGroup label={`Confirm rule ${name}?`} onClose={close} back={() => confirmBtn.current ?? editBtn.current}>
          <span>{pending ? `Confirm this pending ${r.pending_kind === "changed" ? "change" : "addition"}?` : `Confirm rule ${name} as written? It loses its Seeded mark.`} It stays first in every conversation.</span>
          <WriteError w={w} id={id} />
          <div className="toolbar">
            <button
              type="button"
              className="primary sm"
              disabled={w.isBusy(id)}
              onClick={async () => {
                if (await attempt(id, "khipu_route_rule", { action: "confirm", ...ruleTarget(r), expect: r.revision }, null, undefined, close)) close();
              }}
            >
              <Busy on={w.isBusy(id)} />
              Confirm rule {name}
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
          aria-label={`Edit rule ${name}`}
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
            if (await attempt(id, "khipu_route_rule", { action: "edit", ...ruleTarget(r), text: draft }, null, undefined, close)) close();
          }}
        >
          <div className="toolbar">
            <label htmlFor={fieldId}>Rule {name}</label>
            <input id={fieldId} value={draft} autoFocus autoComplete="off" readOnly={editConfirm} onChange={(e) => setDraft(e.target.value)} />
          </div>
          {editConfirm ? (
            <span>Change rule {name} to these words? Every conversation sees them first, in full, from its next prompt.</span>
          ) : null}
          <WriteError w={w} id={id} />
          <div className="toolbar">
            <button type="submit" className="primary sm" disabled={w.isBusy(id)}>
              <Busy on={w.isBusy(id)} />
              {editConfirm ? `Change rule ${name}` : "Save…"}
            </button>
            <button type="button" className="sm link" onClick={close}>
              Cancel
            </button>
          </div>
        </form>
      ) : null}
      {mine === "remove" ? (
        <ConfirmGroup label={`Remove rule ${name}?`} onClose={close} back={() => removeBtn.current}>
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
                if (await w.run(id, "khipu_route_rule", { action: "retire", ...ruleTarget(r) })) {
                  close();
                  onRemoved(r);
                }
              }}
            >
              <Busy on={w.isBusy(id)} />
              Remove rule {name}
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

const RULE_FILTERS: { key: RuleFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "confirm", label: "To confirm" },
  { key: "checked", label: "Checked by Khipu" },
  { key: "broken", label: "May be broken" },
];

const rulesWord = (n: number) => `${n} rule${n === 1 ? "" : "s"}`;

function RulesSection({ data, lists, w, now }: { data: RouteView; lists: ListControls; w: Writer; now: Date }) {
  const rules = data.rules ?? [];
  const results = data.rule_results ?? [];
  // Which rules Khipu checks: those with a result (the same matrix the lines
  // come from); an older Khipu without results names them in watched_rules.
  const watchedRules = data.rule_results ? results.map((x) => x.rule) : (data.watched_rules ?? []);
  const resultOf = (n: number) => results.find((x) => x.rule === n);
  const filter = lists.prefs.ruleFilter;
  const setFilter = (f: RuleFilter) => lists.set("ruleFilter", f);
  const query = lists.ruleQuery;
  const filterLabel = useId();
  const searchId = `rule-search-${useId().replace(/:/g, "")}`;
  const ruleSearch = useRef<HTMLInputElement>(null);
  const [ask, setAsk] = useState<RuleAsk>(null);
  const [removed, setRemoved] = useState<RouteRule | null>(null);
  const [draft, setDraft] = useState("");
  const [adding, setAdding] = useState(false);
  const fieldId = useId();
  const addBtn = useRef<HTMLButtonElement>(null);
  const eok = useEndOrKeep(w);
  const resetAdd = () => {
    setDraft("");
    setAdding(false);
  };
  const toConfirm = rules.filter((r) => r.pending || (r.seeded && !r.confirmed)).length;
  const inFilter: Record<RuleFilter, (r: RouteRule) => boolean> = {
    all: () => true,
    confirm: (r) => !!r.pending || (!!r.seeded && !r.confirmed),
    checked: (r) => watchedRules.includes(r.n),
    broken: (r) => watchedRules.includes(r.n) && resultOf(r.n)?.result === "broken",
  };
  const counts = Object.fromEntries(RULE_FILTERS.map((f) => [f.key, rules.filter(inFilter[f.key]).length])) as Record<RuleFilter, number>;
  const inFilterRules = rules.filter(inFilter[filter]);
  const searching = rules.length > SEARCH_OVER;
  const shown = searching && query.trim() ? inFilterRules.filter((r) => matchesQuery(query, r.text)) : inFilterRules;
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
    if (await eok.attempt("rule-add", "khipu_route_rule", { action: "add", text: draft }, null, undefined, resetAdd)) resetAdd();
  }
  const restoreId = removed ? `rule-restore:${ruleIdentity(removed)}` : "";
  return (
    <div className="section-body">
        <h2 className="sub-title">
          Standing rules{" "}
          <Tag>
            {`${rules.length} rule${rules.length === 1 ? "" : "s"}`}
            {toConfirm ? ` · ${toConfirm} to confirm` : ""}
          </Tag>
        </h2>
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
        {searching ? (
          <div className="search-row">
            <label className="sr-only" htmlFor={searchId}>
              Search standing rules
            </label>
            <input id={searchId} ref={ruleSearch} className="filter-input" type="search" placeholder={`Search the ${rules.length} rules`}
              autoComplete="off" value={query} onChange={(e) => lists.setRuleQuery(e.target.value)} />
          </div>
        ) : null}
        {shown.length ? (
          <ol className="rows route-rule-list" role="list" aria-label="Standing rules">
            {shown.map((r) => (
              <RuleRow key={r.entity ?? r.n} r={r} watched={watchedRules.includes(r.n)} result={resultOf(r.n)} ask={ask} setAsk={setAsk} w={w} attempt={eok.attempt} now={now} onRemoved={setRemoved} back={() => addBtn.current} />
            ))}
          </ol>
        ) : searching && query.trim() && inFilterRules.length ? (
          <p className="set-help" role="status">
            {`No rules match “${query.trim()}”. `}
            <button type="button" className="sm link" onClick={() => {
              lists.setRuleQuery("");
              focusAfter(() => ruleSearch.current);
            }}>
              Clear search
            </button>
          </p>
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
        {removed && !rules.some((r) => ruleIdentity(r) === ruleIdentity(removed)) ? (
          <div className="route-undo" role="status">
            <span>Rule {ruleName(removed)} removed.</span>
            {/* It appears only after a removal here: focus lands on it (F5). */}
            <button
              type="button"
              className="sm"
              autoFocus
              disabled={w.isBusy(restoreId)}
              onClick={async () => {
                const n = typeof removed.n === "number" ? removed.n : undefined;
                if (await eok.attempt(restoreId, "khipu_route_rule", { action: "restore", ...ruleTarget(removed) }, null, () => ruleRemoveButton(ruleIdentity(removed), n), () => setRemoved(null)))
                  setRemoved(null);
              }}
            >
              <Busy on={w.isBusy(restoreId)} />
              Undo
            </button>
          </div>
        ) : null}
        {removed ? <WriteError w={w} id={restoreId} /> : null}
        {eok.ask ? (
          <EndOrKeepChoices view={data} ask={eok.ask} now={now} busy={w.anyBusy} saveLabel="Save rule" onCancel={() => eok.setAsk(null)} />
        ) : null}
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
// Proposals from conversations (routing slice 15b)
// ---------------------------------------------------------------------------

/** Seven days after a proposal was written: when it ends on its own. */
function proposalEndsAt(at: string | null | undefined): string | null {
  if (!at) return null;
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return null;
  return new Date(d.getTime() + 7 * 24 * 3600 * 1000).toISOString();
}

function ProposalRowView({
  row,
  data,
  w,
  attempt,
  ask,
  setAsk,
  now,
}: {
  row: ProposalRow;
  data: RouteView;
  w: Writer;
  attempt: (id: string, command: string, args: Record<string, unknown>, after?: (() => Promise<unknown>) | null, back?: Back, onRetrySuccess?: () => void) => Promise<boolean>;
  ask: string | null;
  setAsk: (a: string | null) => void;
  now: Date;
}) {
  const p = row.proposal;
  const live = p.status === "live";
  const [draft, setDraft] = useState(row.proposed ?? "");
  const [editConfirm, setEditConfirm] = useState(false);
  const confirmId = `proposal:confirm:${p.id}`;
  const dismissId = `proposal:dismiss:${p.id}`;
  const editId = `proposal:edit:${p.id}`;
  const rule = p.kind === "rule" ? (data.rules ?? []).find((r) => r.entity === p.entity) ?? (data.rules ?? []).find((r) => r.n === p.n) : undefined;
  const expect = data.priority?.version ?? data.priority_version ?? null;
  const ends = proposalEndsAt(p.at);
  const close = () => {
    setAsk(null);
    setEditConfirm(false);
  };
  const ruleMissing = p.kind === "rule" && !rule;
  const refuse = (id: string) => {
    w.fail(id, "That rule isn't in this read, so nothing was sent. Read the routing record again, then try again.");
    return Promise.resolve(false);
  };
  const confirm = () =>
    ruleMissing
      ? refuse(confirmId)
      : p.kind === "rule" && rule
        ? attempt(confirmId, "khipu_route_rule", { action: "confirm", ...ruleTarget(rule), expect: rule.revision })
        : attempt(confirmId, "khipu_route_priority", { action: "confirm", expect });
  const dismiss = () =>
    ruleMissing
      ? refuse(dismissId)
      : p.kind === "rule" && rule
        ? w.run(dismissId, "khipu_route_rule", { action: "dismiss", ...ruleTarget(rule) })
        : w.run(dismissId, "khipu_route_priority", { action: "undo", expect });
  return (
    <div className="mrow prop" role="listitem" data-route-item={`proposal:${p.id}`} tabIndex={-1}>
      <span className="name">{row.name}</span>
      <span>
        {live ? (
          <>
            {row.by}.{" "}
            {row.withheld
              ? "Its words stay on the Mac it came from, so confirm it there. It is already in use everywhere."
              : `In use now${row.owner ? " on top of your words" : ""}${ends ? `; ends ${whenLocal(ends, now)} unless you confirm it` : ""}.`}
            {p.kind === "priority" && !row.withheld && data.priority?.owner_request ? (
              <> What it said you asked: “{data.priority.owner_request}”</>
            ) : null}
          </>
        ) : p.status === "crossed" ? (
          <>{row.by}. It was written against an older version, so it never took effect. Its author was told.</>
        ) : (
          <>{row.by}. You replaced it with your own write, so it has ended.</>
        )}
      </span>
      {live && ruleMissing ? (
        <span>That rule isn't in this read, so this proposal can't be confirmed or dismissed here. Read the routing record again, then try again.</span>
      ) : null}
      <span className="end">
        {live ? <Tag tone="accent" dot>In use</Tag> : p.status === "crossed" ? <Tag tone="warn" dot>Crossed</Tag> : <Tag dot>{endedWords(p.status)}</Tag>}
      </span>
      {live && !row.withheld ? (
        <div className="prop-side">
          <div>
            <span className="cap">{row.owner ? "Your words" : "Your words now"}</span>
            {row.owner ?? "Nothing yet: this adds new words of yours once confirmed."}
          </div>
          <div>
            <span className="cap">Proposed words</span>
            {row.proposed ?? ""}
          </div>
        </div>
      ) : null}
      {live && !ruleMissing ? (
        <div className="acts">
          {!row.withheld ? (
            <button type="button" className="sm primary" disabled={w.isBusy(confirmId)}
              aria-label={`Confirm the proposed ${row.name.toLowerCase()}`} onClick={() => void confirm()}>
              <Busy on={w.isBusy(confirmId)} />
              Confirm
            </button>
          ) : null}
          {p.kind === "rule" && !row.withheld ? (
            <button type="button" className="sm" aria-expanded={ask === "edit"}
              aria-label={`Edit the proposed ${row.name.toLowerCase()}`} onClick={() => { setDraft(row.proposed ?? ""); setEditConfirm(false); setAsk(ask === "edit" ? null : "edit"); }}>
              Edit, then confirm
            </button>
          ) : null}
          <button type="button" className="sm" aria-expanded={ask === "dismiss"}
            aria-label={`Dismiss the proposed ${row.name.toLowerCase()}`} onClick={() => setAsk(ask === "dismiss" ? null : "dismiss")}>
            Dismiss
          </button>
        </div>
      ) : null}
      {live && ask === "dismiss" ? (
        <ConfirmGroup label={`Dismiss the proposed ${row.name.toLowerCase()}?`} onClose={close} back={() => null}>
          <span>
            Dismiss this proposal? {row.owner ? "Your words stay as they are." : "Nothing is added."} The conversation
            that proposed it is told.
          </span>
          <WriteError w={w} id={dismissId} />
          <div className="toolbar">
            <button type="button" className="sm danger" disabled={w.isBusy(dismissId)}
              onClick={async () => { if (await dismiss()) close(); }}>
              <Busy on={w.isBusy(dismissId)} />
              Dismiss it
            </button>
            <button type="button" className="sm link" onClick={close}>
              Keep it
            </button>
          </div>
        </ConfirmGroup>
      ) : null}
      {live && ask === "edit" && rule ? (
        <form className={editConfirm ? "route-confirm" : "route-pause"} aria-label={`Edit the proposed ${row.name.toLowerCase()}`}
          onKeyDown={escapeCloses(close)}
          onSubmit={async (e) => {
            e.preventDefault();
            if (!draft.trim()) {
              w.fail(editId, "Write the words first.");
              return;
            }
            if (!editConfirm) {
              w.fail(editId, null);
              setEditConfirm(true);
              return;
            }
            if (await attempt(editId, "khipu_route_rule", { action: "edit", ...ruleTarget(rule), text: draft }, null, undefined, close)) close();
          }}>
          <div className="toolbar">
            <label htmlFor={editId}>Words</label>
            <input id={editId} value={draft} autoFocus autoComplete="off" readOnly={editConfirm} onChange={(e) => setDraft(e.target.value)} />
          </div>
          {editConfirm ? <span>Make these your words for {row.name.toLowerCase()}? They replace the proposal.</span> : null}
          <WriteError w={w} id={editId} />
          <div className="toolbar">
            <button type="submit" className="primary sm" disabled={w.isBusy(editId)}>
              <Busy on={w.isBusy(editId)} />
              {editConfirm ? "Make them mine" : "Save…"}
            </button>
            <button type="button" className="sm link" onClick={close}>
              Cancel
            </button>
          </div>
        </form>
      ) : null}
      <WriteError w={w} id={confirmId} />
    </div>
  );
}

function ProposalsSection({ data, w, now }: { data: RouteView; w: Writer; now: Date }) {
  const model = proposalsModel(data, now);
  const eok = useEndOrKeep(w);
  const [ask, setAsk] = useState<{ id: string; kind: string } | null>(null);
  const [showEnded, setShowEnded] = useState(false);
  const ended = model.ended;
  const endedCounts = ended
    .filter((r) => {
      const at = r.proposal.ended_at;
      return at != null && new Date(at).getTime() >= new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    })
    .reduce<Record<string, number>>((acc, r) => ({ ...acc, [r.proposal.status]: (acc[r.proposal.status] ?? 0) + 1 }), {});
  const endedToday = model.endedToday;
  const anyDatedEnd = ended.some((r) => r.proposal.ended_at);
  const rows = [...model.live, ...model.stale];
  return (
    <div className="section-body">
      <h2 className="sub-title">Proposals from conversations</h2>
      <p className="set-help">A conversation's change is in use until you confirm or dismiss it.</p>
      {rows.length ? (
        <div className="mrows" role="list" aria-label="Proposals">
          {rows.map((row) => (
            <ProposalRowView key={row.proposal.id} row={row} data={data} w={w} attempt={eok.attempt} now={now}
              ask={ask?.id === row.proposal.id ? ask.kind : null}
              setAsk={(kind) => setAsk(kind ? { id: row.proposal.id, kind } : null)} />
          ))}
        </div>
      ) : (
        <div className="empty">
          <b>Nothing waiting for you</b>
          <span>When a conversation proposes a change to a rule, your note or your priority, it shows here beside your words.</span>
        </div>
      )}
      {eok.ask ? (
        <EndOrKeepChoices view={data} ask={eok.ask} now={now} busy={w.anyBusy} saveLabel="Save and confirm"
          onCancel={() => eok.setAsk(null)} />
      ) : null}
      {ended.length ? (
        <p className="set-help">
          {anyDatedEnd ? (
            <>
              Ended today: {endedToday}
              {Object.keys(endedCounts).length
                ? ` (${Object.entries(endedCounts).map(([s, n]) => `${n} ${endedWords(s)}`).join(", ")})`
                : ""}
              .{" "}
            </>
          ) : (
            <>Ended: {ended.length}. </>
          )}
          <button type="button" className="sm" aria-expanded={showEnded} onClick={() => setShowEnded((v) => !v)}>
            {showEnded ? "Hide" : "Show"}
          </button>
        </p>
      ) : null}
      {showEnded ? (
        <div className="mrows" role="list" aria-label="Ended proposals">
          {ended.map((row) => (
            <div key={row.proposal.id} className="mrow prop" role="listitem">
              <span className="name">{row.name}</span>
              <span>{row.by}. {endedWords(row.proposal.status)}.</span>
              <span className="end"><Tag>{endedWords(row.proposal.status)}</Tag></span>
            </div>
          ))}
        </div>
      ) : null}
      {model.live.some((r) => r.withheld) ? (
        <p className="set-help">
          Proposal words are kept on the Mac they came from because “Share proposal words between your Macs” is off in
          Telling conversations.
        </p>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Holds
// ---------------------------------------------------------------------------

export const HOLD_REASON_ARGS: { value: string; label: string }[] = [
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
  const [ending, setEnding] = useState<string | null>(null);
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
    <div className="section-body">
        <h2 className="sub-title">Holds</h2>
        <p className="set-help">Short notes about one account, this Mac or all work. Every hold from a conversation ends on its own.</p>
        {holds.length ? (
          <div className="rows" role="list" aria-label="Holds">
            {holds.map((h, i) => {
              const id = `hold-end:${h.id}`;
              return (
                <div key={h.id} className="row-item route-acct" role="listitem" data-route-item={`hold:${h.id}`} tabIndex={-1}>
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
                      aria-expanded={ending === h.id}
                      disabled={w.isBusy(id)}
                      onClick={() => setEnding((v) => (v === h.id ? null : h.id))}
                    >
                      <Busy on={w.isBusy(id)} />
                      End now
                    </button>
                  </span>
                  {ending === h.id ? (
                    <ConfirmGroup label={`End the hold on ${h.scope_label}?`} onClose={() => setEnding(null)}
                      back={() => endBtns.current[h.id] ?? addBtn.current}>
                      <span>
                        End the hold on {h.scope_label} now? Conversations stop seeing it on their next prompt. It
                        stays in Recent changes.
                      </span>
                      <WriteError w={w} id={id} />
                      <div className="toolbar">
                        <button type="button" className="sm danger" disabled={w.isBusy(id)}
                          onClick={() => {
                            // Once this hold ends, focus goes to the next
                            // hold's End now, else the one before, else Add
                            // hold (Codex review, finding 2).
                            const from = holds;
                            void w.run(id, "khipu_route_hold_end", { id: h.id }, null, () => {
                              const usable = (j: number) => {
                                const b = endBtns.current[from[j].id];
                                return b?.isConnected ? b : null;
                              };
                              for (let j = i + 1; j < from.length; j++) if (usable(j)) return usable(j);
                              for (let j = i - 1; j >= 0; j--) if (usable(j)) return usable(j);
                              return addBtn.current;
                            }).then((ok) => { if (ok) setEnding(null); });
                          }}>
                          <Busy on={w.isBusy(id)} />
                          End the hold
                        </button>
                        <button type="button" className="sm link" onClick={() => setEnding(null)}>
                          Keep it
                        </button>
                      </div>
                    </ConfirmGroup>
                  ) : w.errors[id] ? (
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
  );
}

// ---------------------------------------------------------------------------
// Telling conversations
// ---------------------------------------------------------------------------

/** `routing_delivery` is intentionally an evidence report, not a routing
 * decision. Its raw identity and component can hold session ids, so they are
 * never rendered: rows use the generic labels from
 * `deliveryConversationLabels` (doctor carries no friendly conversation,
 * project, Mac, or last-prompt label for these rows). */

function deliveryStrings(value: unknown): string[] | null {
  return Array.isArray(value) && value.every((item) => typeof item === "string") ? value : null;
}

function deliveryIdentities(value: unknown): RouteDeliveryIdentity[] | null {
  if (!Array.isArray(value)) return null;
  return value.every((row) => {
    if (!row || typeof row !== "object") return false;
    const identity = row as RouteDeliveryIdentity;
    return typeof identity.identity === "string" && typeof identity.component === "string" && typeof identity.status === "string"
      && (identity.reasons == null || deliveryStrings(identity.reasons) != null)
      && (identity.missing == null || deliveryStrings(identity.missing) != null)
      && (identity.owed == null || (typeof identity.owed === "number" && Number.isFinite(identity.owed) && identity.owed >= 0));
  }) ? value as RouteDeliveryIdentity[] : null;
}

function deliveryAge(seconds: number | null | undefined): string | null {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return null;
  if (seconds < 60) return `${Math.round(seconds)} s old`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min old`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours} h${minutes % 60 ? ` ${minutes % 60} min` : ""} old` : `${Math.round(hours / 24)} days old`;
}

function freshnessWords(label: string, freshness: RouteDeliveryFreshness | undefined): { label: string; text: string; tone: "ok" | "warn" | "err" | undefined } | null {
  if (!freshness || freshness.status === "not_applicable") return null;
  const age = deliveryAge(freshness.age_seconds);
  if (freshness.status === "green" && age) return { label, text: `Current · ${age}`, tone: "ok" };
  if (freshness.status === "red") return { label, text: age ? `Behind · ${age}` : "Behind", tone: "err" };
  return { label, text: "Can't be checked", tone: undefined };
}

function identityWords(row: RouteDeliveryIdentity, tellingOff = false): { text: string; detail?: string; tone: "ok" | "warn" | "err" | undefined } {
  const owed = typeof row.owed === "number" && Number.isFinite(row.owed) && row.owed >= 0 ? row.owed : null;
  const owedWords = owed == null ? null : `${owed} ${owed === 1 ? "change" : "changes"} owed`;
  if (tellingOff && row.status === "green" && owed === 0 && !(row.missing?.length)) return { text: "Not being told", tone: undefined };
  if (row.status === "green" && owed != null && !(row.missing?.length)) return { text: owed ? owedWords! : "No changes owed", tone: owed ? undefined : "ok" };
  if (row.status === "red") {
    const reason = (deliveryStrings(row.reasons) ?? []).map(deliveryReason).join("; ");
    return { text: owedWords ?? "Delivery needs attention", detail: reason || undefined, tone: "err" };
  }
  const missing = deliveryStrings(row.missing);
  return { text: "Can't be checked", detail: missing?.length ? `Can't check: ${missing.join(", ")}` : undefined, tone: undefined };
}

function deliveryComponentWord(component: string | null | undefined): string {
  return component === "main" ? "main" : component === "mcp" ? "MCP helper" : "a helper agent";
}

function ownerDeliveryRows(value: unknown): RouteDeliveryRow[] | null {
  if (!Array.isArray(value)) return null;
  return value.every((row) => row && typeof row === "object" && typeof (row as RouteDeliveryRow).label === "string")
    ? (value as RouteDeliveryRow[])
    : null;
}

function deliveryStatus(health: RouteDeliveryHealth): "green" | "red" | "unknown" {
  if (health.status !== "green" && health.status !== "red" && health.status !== "unknown") return "unknown";
  const missing = health.missing == null ? [] : deliveryStrings(health.missing);
  const identities = deliveryIdentities(health.identities);
  return health.status === "green" && (missing == null || missing.length > 0 || identities == null || identities.some((row) => row.status !== "green"))
    ? "unknown"
    : health.status;
}

/** Delivery and health (routing slice 15a): presentation only. The route view
 * supplies recent changes; `khipu doctor` supplies receipt evidence. Nothing
 * here acknowledges delivery, changes settings, or decides where work goes. */
export function DeliverySection({ data, health, healthError, healthReadAt, tellingOff, now, liveAfterSeconds, onRetry }: {
  data: RouteView | null;
  health?: RouteDeliveryHealth | null;
  healthError?: string | null;
  /** When the (possibly cached) doctor report behind `health` was read. */
  healthReadAt?: Date | number | null;
  /** The routing push is off, so no conversation is being told anything. */
  tellingOff?: boolean;
  now: Date;
  liveAfterSeconds?: number;
  onRetry?: () => Promise<unknown>;
}) {
  // A failed doctor read must never leave last-good delivery evidence looking
  // current. The routing record can still show its own recent changes.
  const readHealth = healthError ? null : health;
  const status = readHealth ? deliveryStatus(readHealth) : "unknown";
  const identities = deliveryIdentities(readHealth?.identities) ?? [];
  const changes = data ? changeDays(data, now).flatMap((day) => day.rows).slice(0, 3) : [];
  const freshness = readHealth ? [
    readHealth.replication?.status === "not_applicable" ? null : freshnessWords("Other Macs", readHealth.replication) ?? { label: "Other Macs", text: "Can't be checked", tone: undefined },
    readHealth.gateway?.status === "not_applicable" ? null : freshnessWords("Cloud copy", readHealth.gateway) ?? { label: "Cloud copy", text: "Can't be checked", tone: undefined },
  ].filter((row): row is NonNullable<typeof row> => row != null) : [];
  const labels = deliveryConversationLabels(identities);
  // Owner-local rows from `route --json` (friendly name, Mac, last prompt,
  // per-change recipients) when the CLI carries them; the doctor's generic
  // rows above remain the fallback for an older CLI. Display only.
  const ownerRows = ownerDeliveryRows(data?.delivery_rows);
  const otherMacs = (data?.delivery_macs ?? []).filter((mac) => mac && typeof mac.label === "string");
  const readAt = healthReadAt == null ? null : healthReadAt instanceof Date ? healthReadAt : new Date(healthReadAt);
  const readAtWords = readAt && !Number.isNaN(readAt.getTime())
    ? readAt.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : null;
  const healthReasons = (deliveryStrings(readHealth?.reasons) ?? []).map(deliveryReason);
  const liveLabel = liveAfterSeconds == null
    ? "recent delivery points"
    : liveAfterSeconds === 0 ? "delivery points without an expiry" : `delivery points in the last ${limitWords(liveAfterSeconds, "time")}`;
  const [retrying, setRetrying] = useState(false);
  return (
    <div className="delivery-section">
      {healthError ? (
        <div className="set-error" role="alert">
          Couldn&apos;t read delivery health: {healthError}. Nothing below is a guess.
          {onRetry ? <button type="button" className="sm" disabled={retrying} onClick={async () => { setRetrying(true); try { await onRetry(); } finally { setRetrying(false); } }}><Busy on={retrying} />Try again</button> : null}
        </div>
      ) : !readHealth ? (
        <div className="empty" role="status">
          <b>Delivery health hasn&apos;t been reported yet</b>
          <span>Khipu cannot tell which conversations have been told what until its doctor report includes delivery evidence.</span>
        </div>
      ) : status === "red" ? (
        <div className="callout err" role="alert">
          <div className="txt">
            <b>Some routing changes have not reached conversations</b>
            <span>{healthReasons.map(capitalise).join(". ") + (healthReasons.length ? "." : "") || "Khipu has delivery evidence that needs attention."} Nothing is stopped.</span>
          </div>
        </div>
      ) : status === "unknown" ? (
        <div className="callout" role="status">
          <div className="txt">
            <b>Some delivery evidence can&apos;t be checked</b>
            <span>{(deliveryStrings(readHealth.missing) ?? []).join(", ") || "Khipu did not return enough evidence to say what conversations have."}</span>
          </div>
        </div>
      ) : null}

      {readHealth && readAtWords ? (
        <p className="set-help">Delivery health from the health report read at {readAtWords}.</p>
      ) : null}

      <div className="section-body">
        <h2 className="sub-title">Live conversations <span className="meta">{liveLabel}</span></h2>
        {ownerRows ? (
          ownerRows.length ? (
            <div className="mrows" role="list" aria-label="Live conversations">
              {ownerRows.map((row, index) => {
                const state = identityWords({ identity: "", component: "", status: row.status ?? "unknown", owed: row.owed ?? undefined, reasons: row.reasons, missing: row.missing }, tellingOff);
                return (
                  <div key={`owner-delivery-${index}`} className="mrow delivery" role="listitem">
                    <span><span className="name">{row.label}</span><br /><span className="src">{[deliveryHarnessName(row.harness), deliveryComponentWord(row.component)].filter(Boolean).join(" · ")}</span></span>
                    <span className="mac-col">{row.mac ?? "This Mac"}</span>
                    <span className="num">{row.last_prompt_at ? agoSpan(row.last_prompt_at, now) : "Can't be checked"}</span>
                    <span className="delivery-state"><Tag tone={state.tone} dot>{state.text}</Tag>{state.detail ? <span className="src">{state.detail}</span> : null}</span>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="empty">
              <b>No live conversations</b>
              <span>No delivery identity has a recent delivery point.</span>
            </div>
          )
        ) : identities.length ? (
          <div className="mrows" role="list" aria-label="Live conversations">
            {identities.map((row, index) => {
              const state = identityWords(row, tellingOff);
              const label = labels[index];
              return (
                <div key={`delivery-${index}`} className="mrow delivery" role="listitem">
                  <span><span className="name">{label.conversation}</span><br /><span className="src">{[label.harness, label.component].filter(Boolean).join(" · ")}</span></span>
                  <span className="delivery-state"><Tag tone={state.tone} dot>{state.text}</Tag>{state.detail ? <span className="src">{state.detail}</span> : null}</span>
                </div>
              );
            })}
          </div>
        ) : readHealth ? (
          <div className="empty">
            <b>{status === "green" ? "No live conversations" : "No live conversations can be confirmed"}</b>
            <span>{status === "green" ? "No delivery identity has a recent delivery point." : "Khipu did not return enough delivery evidence to prove that no conversation is live."}</span>
          </div>
        ) : null}
      </div>

      <div className="section-body">
        <h2 className="sub-title">Recent changes and who has them</h2>
        {!data ? (
          <p className="set-help">The routing record is still being read.</p>
        ) : !changes.length ? (
          <p className="set-help">No routing changes yet.</p>
        ) : (
          <div className="mrows" role="list" aria-label="Recent changes and who has them">
            {changes.map((change, index) => (
              <div key={`${change.subject}:${change.text}:${index}`} className="mrow delivery-change" role="listitem">
                <span className="when">{whenLocal(change.at, now)}</span>
                <span>{change.subject} {change.text}<br /><span className="src">{change.who}</span></span>
                {change.recipients ? (
                  <span className="end">
                    <Tag tone={change.recipients.waiting.length ? "warn" : "ok"} dot>{change.recipients.have_count} of {change.recipients.live_count} have it</Tag>
                    {change.recipients.waiting.length ? <span className="src">Waiting: {change.recipients.waiting.join(", ")}</span> : null}
                  </span>
                ) : (
                  <span className="end"><Tag>Who has it isn&apos;t available</Tag></span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {freshness.length || otherMacs.length ? (
        <div className="section-body">
          <h2 className="sub-title">Other Macs and the cloud</h2>
          <div className="mrows" role="list" aria-label="Other Macs and the cloud">
            {otherMacs.map((mac, index) => {
              const age = deliveryAge(mac.age_seconds);
              return (
                <div key={`mac-${mac.label}-${index}`} className="mrow delivery-freshness" role="listitem">
                  <span className="name">{mac.label}</span>
                  <span>{age ? `Its copy is ${age}` : "Its copy can't be checked"}</span>
                  <span className="end"><Tag tone={mac.behind ? "err" : age ? "ok" : undefined} dot>{mac.behind ? "Behind" : age ? "Current" : "Can't be checked"}</Tag></span>
                </div>
              );
            })}
            {(otherMacs.length ? freshness.filter((row) => row.label === "Cloud copy") : freshness).map((row) => (
              <div key={row.label} className="mrow delivery-freshness" role="listitem">
                <span className="name">{row.label}</span>
                <span className="end"><Tag tone={row.tone} dot>{row.text}</Tag></span>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

const HEARS: [string, string][] = [
  ["Claude Code", "At the start and before each prompt. During a turn, routing arrives with the tool result after the call has run. A subagent hears the summary when it starts."],
  ["Codex", "At the start and before each prompt, once you approve Khipu's hooks in Codex. During a turn, routing arrives with the tool result after the call has run."],
  ["Cursor", "At the start only; Cursor has no step before each prompt."],
  ["Grok Build", "On its next Khipu tool call; hook output is discarded."],
  ["Aegis", "On its next Khipu tool call."],
  ["Cloud agents", "On their next Khipu tool call from the hub copy."],
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

/** Telling's sizes and times, each a row with Change…. */
const SIZES: { name: string; help: string; fields: LimitField[]; join?: string }[] = [
  {
    name: "Longest summary at the start of a conversation",
    help: "Your rules and your note are never cut; this limits what follows them.",
    fields: [{ key: "routing_summary_chars", label: "Longest summary", unit: "chars", options: [1000, 1500, 3000, 6000, 0], zero: "No limit" }],
  },
  {
    name: "Longest update before a prompt",
    help: "Rules and your note are never cut.",
    fields: [{ key: "routing_prompt_chars", label: "Longest update", unit: "chars", options: [200, 400, 800, 1600, 0], zero: "No limit" }],
  },
  {
    name: "Longest routing block for a subagent at its start",
    help: "Account states and holds come first, then your rules and your note whole where they fit; the rest arrives on the subagent's own tool results.",
    fields: [{ key: "routing_child_chars", label: "Longest child block", unit: "chars", options: [1000, 2000, 4000, 8000, 0], zero: "No limit" }],
  },
  {
    name: "Longest line",
    help: "Each line about an account, a hold or a lane. Rules and your note are never cut.",
    fields: [{ key: "routing_line_chars", label: "Longest line", unit: "chars", options: [100, 200, 400, 800, 0], zero: "No limit" }],
  },
  {
    name: "Time allowed before a prompt",
    help: "If reading takes longer, the update waits for the next prompt; memory search is not slowed. Never more than one second.",
    fields: [{ key: "routing_prompt_budget_ms", label: "Time allowed", unit: "ms", options: [25, 50, 100, 200, 500, 1000] }],
  },
  {
    name: "Warn when reading before a prompt is too slow on more than",
    help: "Of prompts in the last 7 days; the warning shows in the health report.",
    fields: [{ key: "routing_slow_read_pct", label: "Warn above", unit: "pct", options: [0.5, 1, 2, 5, 10, 0], zero: "Never" }],
  },
  {
    name: "Time allowed at the start of a conversation",
    help: "How long Khipu works on the summary when a conversation starts, so its memory still arrives on time. Never more than 8 seconds.",
    fields: [{ key: "routing_start_budget_ms", label: "Time allowed at the start", unit: "ms", options: [250, 500, 1000, 2000, 4000, 8000] }],
  },
  {
    name: "Send everything again when a conversation is this far behind",
    help: "Also when it comes back after being idle.",
    fields: [
      { key: "routing_resend_after_versions", label: "Changes", unit: "changes", options: [10, 20, 50, 100, 0], zero: "Never by count" },
      { key: "routing_resend_after", label: "Or time", unit: "time", options: [H, 3 * H, 6 * H, 12 * H, 24 * H, 0], zero: "Never by time" },
    ],
    join: " or ",
  },
  {
    name: "Treat a report as out of date after",
    help: "An account with only out-of-date reports shows as Unknown, never as open.",
    fields: [{ key: "routing_stale_after", label: "Out of date after", unit: "time", options: [H, 6 * H, 12 * H, 24 * H, 48 * H, 0], zero: "Never" }],
  },
  {
    name: "How long a limit reported without a time lasts",
    help: "When a conversation reports a limit but not when it ends. A newer report replaces it sooner.",
    fields: [{ key: "routing_report_default_limit", label: "Lasts", unit: "time", options: [10 * M, 30 * M, H, 2 * H, 5 * H, 24 * H] }],
  },
  {
    name: "Warn when two reports disagree for",
    help: "The warning shows on Home and in the health report.",
    fields: [{ key: "routing_disagreement_warn_after", label: "Warn after", unit: "time", options: [10 * M, 30 * M, H, 2 * H, 0], zero: "Never" }],
  },
  {
    name: "Say the record is not refreshed after",
    help: "Conversations are told when Khipu itself has stopped updating.",
    fields: [{ key: "routing_not_refreshed_after", label: "Not refreshed after", unit: "time", options: [2 * M, 5 * M, 10 * M, 30 * M, 0], zero: "Never" }],
  },
];

/** "11 settings, 2 changed from the default", from the values Khipu read. */
function sizesLine(cfg: RouteConfig | null, cfgError?: string | null): string {
  if (!cfg) return cfgError ? "Couldn't read its settings" : "Reading…";
  const changed = SIZES.filter((r) =>
    r.fields.some((f) => {
      const v = settingNumber(cfg, f.key);
      const d = settingDefault(cfg, f.key);
      return v != null && d != null && v !== d;
    }),
  ).length;
  const n = SIZES.length;
  return `${n} ${n === 1 ? "setting" : "settings"}, ${changed ? `${changed} changed from the default` : "none changed from the default"}`;
}

/** A fold inside a group: closed by default, its closed line beside its name. */
function SubFold({ k, name, line, folds, children }: { k: string; name: string; line: string; folds: Folds; children: ReactNode }) {
  const open = folds.isOpen(k);
  const bodyId = `route-sub-${useId().replace(/:/g, "")}`;
  return (
    <div className="fold disclose route-sub-fold" data-open={open}>
      <button type="button" className="disclosure fold-btn" aria-expanded={open} aria-controls={bodyId} onClick={() => folds.toggle(k)}>
        <ChevronRight size={14} className="disclosure-chevron" aria-hidden />
        <span>{name}</span>
        <span className="sline">{line}</span>
      </button>
      <div className="fold-body" id={bodyId} role="region" aria-label={name} hidden={!open}>
        {children}
      </div>
    </div>
  );
}

/** The replication status line under the Mac-to-Mac copy switch, in the
 *  words of `khipu route replication status`: caught up, stale, or never
 *  synced. Off (the doctor block's `not_applicable`) and still loading
 *  (no block yet) say so instead. The doctor block carries the same
 *  status; no new read is made. */
export function replicationStatusWords(
  freshness: RouteDeliveryFreshness | null | undefined,
  healthError?: string | null,
  pending?: boolean,
): string {
  if (pending) return "waiting for your OK";
  if (healthError) return "can't be checked";
  if (!freshness) return "checking…";
  if (freshness.status === "not_applicable") return "off";
  if (freshness.caught_up_at) return freshness.stale || freshness.status === "red" ? "stale" : "caught up";
  if (freshness.status === "green") return "caught up";
  if (freshness.status === "red") return "stale";
  return "never synced";
}

function TellingSection({
  data,
  deliveryHealth,
  deliveryHealthError,
  deliveryHealthReadAt,
  deliveryLiveAfterSeconds,
  onRetryDelivery,
  now,
  cfg,
  features,
  featuresError,
  cfgError,
  w,
  reread,
  rereadFeatures,
  folds,
}: {
  data: RouteView | null;
  deliveryHealth?: RouteDeliveryHealth | null;
  deliveryHealthError?: string | null;
  deliveryHealthReadAt?: number | null;
  deliveryLiveAfterSeconds?: number;
  onRetryDelivery?: () => Promise<unknown>;
  now: Date;
  cfg: RouteConfig | null;
  features: FeaturesShape | null;
  featuresError?: string | null;
  cfgError?: string | null;
  w: Writer;
  reread: () => Promise<unknown>;
  rereadFeatures: () => Promise<unknown>;
  folds: Folds;
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
    <div className="section-body">
      <DeliverySection data={data} health={deliveryHealth} healthError={deliveryHealthError} healthReadAt={deliveryHealthReadAt} tellingOff={master != null && master.enabled !== true} now={now} liveAfterSeconds={deliveryLiveAfterSeconds} onRetry={onRetryDelivery} />
      <SubFold k="sub:hears" name="What each kind of conversation hears" line={`${HEARS.length} kinds`} folds={folds}>
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
      <div className="rows">
        <DispatchScriptsRow cfg={cfg} w={w} reread={reread} />
      </div>
          <div className="rows" role="list" aria-label="What each kind of conversation hears">
            {HEARS.map(([name, words]) => (
              <div key={name} className="row-item route-hear" role="listitem">
                <span className="route-name">{name}</span>
                <span className="set-help">{words}</span>
              </div>
            ))}
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
        </SubFold>
        <SubFold k="sub:sizes" name="Sizes and times" line={sizesLine(cfg, cfgError)} folds={folds}>
          <div className="rows">
            {SIZES.map((r) => (
              <LimitRow key={r.name} name={r.name} help={r.help} fields={r.fields} join={r.join} cfg={cfg} w={w} reread={reread} />
            ))}
          </div>
          <div className="opt-list">
            <SwitchRow
              id="route-proposal-text"
              label="Share proposal words between your Macs"
              what="A conversation's proposed words travel between your Macs, so you can confirm them anywhere. Off, they stay on the Mac they came from until you confirm them there. They never go to cloud agents."
              tag={<Tag>Between your Macs</Tag>}
              checked={cfg?.routing?.routing_proposal_text_between_macs?.value !== false}
              disabled={!cfg || w.anyBusy}
              read={cfgRead}
              busy={w.isBusy("proposal-text")}
              onChange={(next) =>
                void w.run("proposal-text", "khipu_config_set", { key: "routing_proposal_text_between_macs", value: next ? "on" : "off" }, reread)
              }
              error={<WriteError w={w} id="proposal-text" />}
            />
            <SwitchRow
              id="route-replication-live"
              label="Copy routing between your Macs through the hub"
              what="Sends your routing record to the hub so your other Macs can pick it up, and brings their routing changes here. This is the Mac-to-Mac copy, separate from the copy for cloud agents."
              tag={<Tag>Between your Macs</Tag>}
              checked={cfg?.routing?.routing_replication_live?.value === true}
              disabled={!cfg || w.anyBusy}
              read={cfgRead}
              busy={w.isBusy("replication-live")}
              onChange={(next) =>
                void w.run("replication-live", "khipu_config_set", { key: "routing_replication_live", value: next ? "on" : "off" }, reread)
              }
              error={<WriteError w={w} id="replication-live" />}
            />
            <p className="set-help" role="status">
              Replication: {replicationStatusWords(deliveryHealth?.replication, deliveryHealthError, cfg?.routing?.routing_replication_live?.pending === true)}
              {deliveryAge(deliveryHealth?.replication?.age_seconds) ? ` · ${deliveryAge(deliveryHealth?.replication?.age_seconds)}` : ""}
            </p>
            {deliveryHealth?.gateway?.cleanup_gave_up || deliveryHealth?.gateway?.cleanup_interrupted
              || deliveryHealth?.gateway?.cleanup_full ? (
              <div className="set-help" role="alert">
                <p>{deliveryHealth.gateway.reason_words ?? "Routing copy cleanup stopped; a copy may still be on the hub."}</p>
                <button
                  type="button"
                  className="btn"
                  disabled={w.anyBusy}
                  onClick={() => void w.run("gateway-cleanup", "khipu_gateway_copy_cleanup", {}, async () => {
                    await reread();
                    await onRetryDelivery?.();
                  })}
                >
                  {w.isBusy("gateway-cleanup") ? "Retrying…" : "Retry cleanup"}
                </button>
                <WriteError w={w} id="gateway-cleanup" />
              </div>
            ) : null}
          </div>
        </SubFold>

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

// ---------------------------------------------------------------------------
// Providers and lanes (routing slice 15c; mocks
// docs/mockups/2026-10-21-routing/lanes-light.html and lanes-states-light.html):
// the providers Khipu knows, every lane with the account it uses, and the job
// runner folders under a fold of their own. The group only reports: the writes
// it keeps (adding or removing a folder, mapping a lane) are the ones the page
// already had.
// ---------------------------------------------------------------------------

/** How a provider's plan is read, in the mock's words. */
const PROBE_WORDS: Record<string, string> = {
  claude: "plan read with the Claude command line",
  codex: "plan read with the Codex command line",
  grok: "plan read with Grok's command line; it starts an agent, so every 30 min",
  "http-quota": "plan read from a usage address you gave",
  none: "plan not read; job runner lines and reports only",
};

/** Muse's harness sends no job runner lines of its own: reports only. */
const MUSE_PLAN_WORDS = "plan not read; reports only";

function providerPlanWords(p: ProviderRow): string {
  if (p.id === "muse") return MUSE_PLAN_WORDS;
  return PROBE_WORDS[p.probe ?? "none"] ?? PROBE_WORDS.none;
}

/** A short list in words: "glm-5.3 and glm-5.3-flash". */
function listWords(items: string[]): string {
  return items.length <= 1 ? (items[0] ?? "") : `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

function ProvidersList({ view }: { view: RouteView }) {
  const rows = providerRows(view);
  if (!rows.length) return <p className="set-help">No providers yet. Add an account and its provider shows here.</p>;
  return (
    <div className="mrows" role="list" aria-label="Providers">
      {rows.map((p) => (
        <div key={p.id} className="mrow prov" role="listitem">
          <span className="name">{p.label}</span>
          <span>
            {[p.family, providerPlanWords(p)].filter(Boolean).join(" · ")}
            <br />
            <span className="src">
              {!p.accounts.length
                ? "No accounts yet"
                : p.accounts.length === 1
                  ? "1 account"
                  : `${p.accounts.length} accounts: ${p.accounts.join(", ")}`}
              {p.models_allowed?.length ? ` · only ${listWords(p.models_allowed)}` : ""}
              {p.models_never?.length ? ` · never ${listWords(p.models_never)}` : ""}
            </span>
          </span>
          <span className="end">{p.use === "on_request" ? <Tag>On request</Tag> : null}</span>
        </div>
      ))}
    </div>
  );
}

/** Checks and copies (routing slice 15d; mocks
 *  docs/mockups/2026-10-21-routing/checks-light.html and checks-states-*.html):
 *  the advisory flags `khipu route --json` carries, and rule 12's confirmed
 *  model table. Display only: a row states what disagrees; the owner's fix
 *  stays in the group that owns the words, and nothing here sends, stops or
 *  refuses work. The checks `route --json` does not carry yet (model
 *  stale level words) are left out rather than guessed.
 *  Today's model mismatches, open owed directions and routing copies
 *  found in files stay local to the owner's Mac. */
function ChecksSection({ data, now, folds }: { data: RouteView; now: Date; folds: Folds }) {
  const model = checksModel(data, now);
  const row = (r: { key: string; title: string; sub: string }, tag?: ReactNode) => (
    <div key={r.key} className="mrow adv" role="listitem" data-route-item={`check:${r.key}`} tabIndex={-1}>
      <span>{r.title}</span>
      {tag ? <span className="end">{tag}</span> : null}
      {r.sub ? <span className="sub">{r.sub}</span> : null}
    </div>
  );
  return (
    <>
      <p className="set-help">Khipu flags these so you can fix them. None of them stops any work.</p>
      {model.mismatches.length ? (
        <div className="section-body">
          <h2 className="sub-title">Model mismatches today <span className="meta">{model.mismatches.length}</span></h2>
          <div className="mrows" role="list" aria-label="Model mismatches today">
            {model.mismatches.map((r) => row(r, <Tag>{r.guessed ? "Guess" : "From the brief"}</Tag>))}
          </div>
        </div>
      ) : null}
      {model.table ? (
        <div className="section-body">
          <SubFold k="sub:model-table" name="Your model table" line={model.table.line} folds={folds}>
            <dl className="tiers">
              {model.table.rows.map((t) => (
                <div key={t.label}>
                  <dt>{t.label}</dt>
                  <dd>{t.tier}</dd>
                </div>
              ))}
            </dl>
          </SubFold>
        </div>
      ) : null}
      {model.contradictions.length ? (
        <div className="section-body">
          <h2 className="sub-title">Contradictions and out-of-date words</h2>
          <div className="mrows" role="list" aria-label="Contradictions and out-of-date words">
            {model.contradictions.map((r) => row(r, <Tag tone="warn" dot>Contradiction</Tag>))}
          </div>
        </div>
      ) : null}
      {model.upkeep.length ? (
        <div className="section-body">
          <h2 className="sub-title">Rule upkeep</h2>
          <div className="mrows" role="list" aria-label="Rule upkeep">
            {model.upkeep.map((r) => row(r))}
          </div>
        </div>
      ) : null}
      {model.fileCopies ? (
        <div className="section-body">
          <h2 className="sub-title">Routing written in files{model.fileCopies.status !== "not_checked" ? <span className="meta">{model.fileCopies.hits_total} found</span> : null}</h2>
          {model.fileCopies.status === "not_checked" ? (
            <div className="empty">
              <b>Khipu hasn't looked at your files yet</b>
              <span>Nothing is known about copies until the first look. It reads only, and never edits a file.</span>
            </div>
          ) : (
            <>
              <p className="set-help">
                Files last looked at{model.fileCopies.scanned_at && whenLocal(model.fileCopies.scanned_at, now)
                  ? ` ${whenLocal(model.fileCopies.scanned_at, now)}` : " at an unknown time"}: {model.fileCopies.files_checked} files, {model.fileCopies.hits_total} {model.fileCopies.hits_total === 1 ? "copy" : "copies"}.
              </p>
              {model.fileCopies.status === "stale" ? (
                <p className="set-help">Scan is out of date. These are retained results from the last look.</p>
              ) : null}
              {model.fileCopies.status === "incomplete" || model.fileCopies.skipped.length || model.fileCopies.too_large.length ? (
                <p className="set-help">Scan incomplete. Some files were not read.</p>
              ) : null}
              {model.fileCopies.hits.length ? (
                <>
                  <p className="set-help">Conversations are told Khipu's record wins over these.</p>
                  <div className="mrows" role="list" aria-label="Copies of routing found in files">
                    {model.fileCopies.hits.map((h) => row({
                      key: `copy:${h.file}:${h.line}`,
                      title: `${h.file} line ${h.line}`,
                      sub: `“${h.quote}”`,
                    }))}
                  </div>
                  {model.fileCopies.more_not_shown ? (
                    <p className="set-help">{model.fileCopies.more_not_shown} more not shown.</p>
                  ) : null}
                </>
              ) : null}
              {model.fileCopies.too_large.length ? (
                <p className="set-help">Too large to read: {model.fileCopies.too_large.join(", ")}.</p>
              ) : null}
              {model.fileCopies.skipped.length ? (
                <ul className="set-help" aria-label="Files not checked">
                  {model.fileCopies.skipped.map((skip, index) => (
                    <li key={`${skip.file}:${skip.reason}:${index}`}>{skip.file}: {fileCopySkipWords(skip)}</li>
                  ))}
                </ul>
              ) : null}
            </>
          )}
        </div>
      ) : null}
      {model.unreached.length ? (
        <div className="section-body">
          <h2 className="sub-title">Said in a conversation, not in Khipu <span className="meta">{model.unreached.length}</span></h2>
          <div className="mrows" role="list" aria-label="Said in a conversation, not in Khipu">
            {model.unreached.map((r) => row(r))}
          </div>
        </div>
      ) : null}
      {!model.table && !model.mismatches.length && !model.contradictions.length && !model.upkeep.length && !model.unreached.length
        && !model.fileCopies?.hits.length && !(model.fileCopies && model.fileCopies.status !== "checked") ? (
        <div className="section-body">
          <EmptyState title="Nothing to fix" hint="No priority, rule or direction disagrees with an account." />
        </div>
      ) : null}
    </>
  );
}

function ProvidersLanesSection({
  data,
  cfg,
  lists,
  recordRead = "ready",
  w,
  now,
  reread,
  folds,
}: {
  data: RouteView | null;
  cfg: RouteConfig | null;
  lists: ListControls;
  recordRead?: RecordRead;
  w: Writer;
  now: Date;
  reread: () => Promise<unknown>;
  folds: Folds;
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
  const laneRaw = cfg?.routing?.routing_lane_map?.value;
  const map: LaneMap = laneRaw && typeof laneRaw === "object" && !Array.isArray(laneRaw) ? (laneRaw as LaneMap) : {};
  const accounts = data?.accounts ?? [];
  const lanesOf = (name: string) =>
    [...new Set([...(runners.find((x) => x.name === name)?.lanes ?? []), ...Object.keys(map[name] ?? {})])].sort();
  // Lanes of a runner the folders don't name still show, after the folders'.
  const others = [...new Set([...runners.map((r) => r.name), ...refused, ...Object.keys(map)])].filter(
    (name) => !entries.some((e) => e.name === name),
  );
  const unmatchedLimits = data?.unmatched ?? [];
  // Every lane once: folder order, then lanes of runners no folder names.
  const laneEntries = [...entries.map((e) => e.name), ...others].flatMap((name) =>
    lanesOf(name).map((lane) => ({ runner: name, lane })),
  );
  const totalLanes = laneEntries.length;
  // Searching lanes: by lane, runner and mapped account.
  const searchLanes = recordRead === "ready" && totalLanes > SEARCH_OVER;
  const query = searchLanes ? lists.laneQuery.trim() : "";
  const laneSearchId = `lane-search-${base}`;
  const laneSearch = useRef<HTMLInputElement>(null);
  const laneMatches = (name: string, lane: string) => !query || matchesQuery(query, lane, name, map[name]?.[lane], runners.find((r) => r.name === name)?.lane_matches?.[lane]?.account ?? undefined);
  const shownLanes = laneEntries.filter(({ runner, lane }) => laneMatches(runner, lane));
  const hiddenNoAccount = data
    ? lanesWithoutAccount(data, { routing: cfg?.routing ?? null, push: null }).filter(
        (x) => lanesOf(x.runner).includes(x.lane) && !laneMatches(x.runner, x.lane),
      ).length
    : 0;
  // What the Job runner folders fold says closed: every folder, then any
  // problem with one, or that all of them are reporting.
  const folderProblems = entries.map((entry): string | null => {
    const r = runners.find((x) => x.name === entry.name);
    if (refused.includes(entry.name)) return "refused";
    if (r?.unreadable) return "can't be read";
    if (r?.silent) return "stopped reporting";
    return null;
  }).filter((x): x is string => !!x);
  const foldersLine = !entries.length
    ? "No folders added"
    : `${plural(entries.length, "folder", "folders")}, ${
        folderProblems.length === 0
          ? entries.length === 2
            ? "both reporting"
            : "all reporting"
          : folderProblems.length === 1
            ? `1 ${folderProblems[0]}`
            : `${plural(folderProblems.length, "folder", "folders")} need a look`
      }`;
  return (
    <>
      <div className="section-body">
        <h2 className="sub-title">Providers</h2>
        {data ? (
          <ProvidersList view={data} />
        ) : (
          <RecordReadLine read={recordRead === "failed" ? "failed" : "reading"} what="the providers" />
        )}
      </div>
      <div className="section-body">
        <h2 className="sub-title">
          Lanes{" "}
          {totalLanes ? (
            <span className="meta">
              {plural(totalLanes, "lane", "lanes")}
              {entries.length ? ` in ${plural(entries.length, "job runner folder", "job runner folders")}` : ""}
            </span>
          ) : null}
        </h2>
        {recordRead !== "ready" ? (
          // Before the lane map is read every lane would look unmatched.
          <RecordReadLine read={recordRead} what="job runner lanes" />
        ) : totalLanes ? (
          <>
            {searchLanes ? (
              <>
                <div className="search-row">
                  <label className="sr-only" htmlFor={laneSearchId}>
                    Search job runner lanes
                  </label>
                  <input id={laneSearchId} ref={laneSearch} className="filter-input" type="search" placeholder={`Search the ${totalLanes} lanes`}
                    autoComplete="off" value={lists.laneQuery} onChange={(e) => lists.setLaneQuery(e.target.value)} />
                </div>
                {query ? (
                  <p className="set-help" role="status">
                    {shownLanes.length
                      ? `${shownLanes.length} of ${totalLanes} lanes match.`
                      : `No lanes match “${query}”.`}
                    {hiddenNoAccount
                      ? ` ${plural(hiddenNoAccount, "lane without an account is", "lanes without an account are")} hidden by this search.`
                      : ""}{" "}
                    <button type="button" className="sm link" onClick={() => {
                      lists.setLaneQuery("");
                      focusAfter(() => laneSearch.current);
                    }}>
                      Clear search
                    </button>
                  </p>
                ) : null}
              </>
            ) : null}
            {shownLanes.length ? (
              <div className="mrows" role="list" aria-label="Lanes">
                {shownLanes.map(({ runner, lane }) => (
                  <LaneRow key={`${runner}/${lane}`} runner={runner} lane={lane} target={map[runner]?.[lane]}
                    match={runners.find((r) => r.name === runner)?.lane_matches?.[lane]}
                    accounts={accounts} w={w} reread={reread} />
                ))}
              </div>
            ) : null}
            <p className="set-help">A lane without an account still runs; its limits just count toward no account.</p>
          </>
        ) : (
          <div className="empty">
            <b>No job runners yet</b>
            <span>Add a job runner folder and its lanes show here with the account each one uses.</span>
            <div className="toolbar">
              <button
                type="button"
                className="sm primary"
                disabled={!cfg}
                onClick={() => {
                  folds.openKeys(["sub:folders"]);
                  setAdding(true);
                }}
              >
                Add a folder…
              </button>
            </div>
          </div>
        )}
      </div>
      <div className="section-body">
        <SubFold k="sub:folders" name="Job runner folders" line={foldersLine} folds={folds}>
          {entries.length ? (
            <div className="rows" role="list" aria-label="Job runner folders">
              {entries.map((entry) => {
                const r = runners.find((x) => x.name === entry.name);
                const st = refused.includes(entry.name)
                  ? { text: "Refused: it is inside a private folder", tone: "err" as const }
                  : runnerState(r, now);
                const id = `folder-remove:${entry.name}`;
                return (
                  <div key={entry.name} className="row-item route-acct" role="listitem" data-route-item={`runner:${entry.name}`}
                    tabIndex={-1}>
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
          {others.map((name) => {
            const r = runners.find((x) => x.name === name);
            const isRefused = refused.includes(name);
            // A runner Khipu reports on that isn't in your folders list: its
            // state still shows, so a problem with it has a place here.
            const st = isRefused ? { text: "Refused: it is inside a private folder", tone: "err" as const } : r ? runnerState(r, now) : null;
            return st ? (
              <div key={name} className="rows">
                <div className="row-item route-acct" data-route-item={`runner:${name}`} tabIndex={-1}>
                  <span className="route-name">{name}</span>
                  <span className="route-what">
                    <span className="route-state">
                      <span className={st.tone ? `hdot ${st.tone}` : "hdot"} aria-hidden />
                      {st.text}
                    </span>
                    <span className="route-src">
                      Not in your job runner folders{r?.path ? ` · ${r.path}` : ""}
                    </span>
                  </span>
                </div>
              </div>
            ) : null;
          })}
          {unmatchedLimits.length ? (
            <p className="set-help" role="status">
              {unmatchedLimits.length === 1 ? "A limit came" : `${unmatchedLimits.length} limits came`} from a lane that
              isn't matched to an account. Match it above and the limit counts for that account.
            </p>
          ) : null}
          <div className="toolbar">
            <button ref={addBtn} type="button" className="sm" aria-expanded={adding} disabled={!cfg} onClick={() => setAdding((v) => !v)}>
              Add a folder…
            </button>
          </div>
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
          <p className="set-help">
            A limit on one lane closes every lane on the same account. A lane that isn't matched is never guessed;
            conversations are told a limit came from an unmatched lane.
          </p>
        </SubFold>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Which account each job lane uses
// ---------------------------------------------------------------------------

type LaneMap = Record<string, Record<string, string>>;

/** What the CLI's resolver says about one lane, as `khipu route --json`
 *  reports it (routing.ts, `RouteRunner.lane_matches`). */
type LaneMatch = NonNullable<RouteRunner["lane_matches"]>[string];

/** The resolver's refusal reasons, in plain words (routing.py `resolve_lane`). */
const LANE_REASON_WORDS: Record<string, string> = {
  "launcher evidence contradicts the declaration": "the runner's settings and its login name different accounts",
  "invalid or stale login binding": "the runner's login binding is invalid or stale",
  "explicit provider conflicts with model provider": "the runner names a provider its model isn't from",
  "model family has no unique provider": "the model's family has no unique provider",
  "login binding conflicts with model or provider": "the runner's login conflicts with its model or provider",
  "provider has no unique account": "the provider has no unique account",
  "account, provider or login binding conflict": "the runner's account, provider and login conflict",
  "override has no unique current binding": "the account you mapped it to has no unique binding",
};

const LANE_MAP_HELP = "Add an account line for this lane in the runner's settings, or map it here.";

/** The how-matched line, the tag beside it and the candidates row under a
 *  lane, in the mock's words. `undefined` tag: none. */
function laneMatchWords(target: string | undefined, match: LaneMatch | undefined): {
  how: string;
  tag: { text: string; tone?: "ok" | "warn" } | null;
  cands: string | null;
} {
  if (target) return { how: "you mapped it", tag: { text: "Yours" }, cands: null };
  if (match?.account) {
    if (match.matched_by === "declaration") {
      return match.identity_state === "confirmed"
        ? { how: `the runner says so; a job from it used ${match.account}'s login`, tag: { text: "Confirmed", tone: "ok" }, cands: null }
        : { how: "the runner says so; no job has proved it yet", tag: { text: "Claimed" }, cands: null };
    }
    if (match.matched_by === "name") return { how: "matched by name", tag: { text: "By name" }, cands: null };
    if (match.matched_by === "login_home") return { how: "the runner's login names it", tag: { text: "By login" }, cands: null };
    if (match.matched_by === "provider") return { how: "the runner names the provider; it has one account", tag: { text: "By provider" }, cands: null };
    return { how: `matched by ${match.matched_by}`, tag: null, cands: null };
  }
  const reason = match?.reason ?? "";
  const n = match?.candidates.length ?? 0;
  // A declaration its launcher contradicts: the candidates say who, but not
  // which side the runner named, so the row says it that way.
  if (reason === "launcher evidence contradicts the declaration") {
    return { how: LANE_REASON_WORDS[reason], tag: { text: "Conflict", tone: "warn" },
      cands: "Fix the runner's settings, or pick the account here." };
  }
  if (n >= 2) {
    return { how: `${n === 2 ? "two" : n} accounts match the name`, tag: { text: "Pick one", tone: "warn" },
      cands: `Could be: ${match!.candidates.join(", ")}` };
  }
  if (reason) return { how: LANE_REASON_WORDS[reason] ?? reason, tag: { text: "No account", tone: "warn" }, cands: LANE_MAP_HELP };
  return { how: "no name match and nothing declared", tag: { text: "No account", tone: "warn" }, cands: LANE_MAP_HELP };
}

function LaneRow({
  runner,
  lane,
  target,
  match,
  accounts,
  w,
  reread,
}: {
  runner: string;
  lane: string;
  target: string | undefined;
  match?: LaneMatch;
  accounts: RouteAccount[];
  w: Writer;
  reread: () => Promise<unknown>;
}) {
  const resolved = target || match?.account;
  const known = resolved ? accounts.find((a) => a.label === resolved || a.key === resolved) : undefined;
  // Closed, as the mock draws it: the row says how the lane matched, and
  // Change… opens the map form (no lane opens one on its own).
  const [open, setOpen] = useState(false);
  const [choice, setChoice] = useState(known?.label ?? "");
  const selId = `lane-${useId().replace(/:/g, "")}`;
  const id = `lane:${runner}/${lane}`;
  const words = laneMatchWords(target, match);
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
    <div className="mrow lane" role="listitem" data-route-item={`lane:${runner}/${lane}`} tabIndex={-1}>
      <span>
        <span className="mono">{lane}</span>
        <br />
        <span className="src">{runner} runner</span>
      </span>
      <span className="arrow" aria-hidden>→</span>
      {open ? (
        <form className="lane-form" aria-label={`Account for ${runner}'s ${lane} lane`} onSubmit={save}>
          {!resolved ? <Tag tone="warn">No account</Tag> : null}
          {!resolved && !!match?.candidates.length ? <span>Could be: {match.candidates.join(", ")}</span> : null}
          <label className="sr-only" htmlFor={selId}>
            Account for {lane}
          </label>
          <select id={selId} value={choice} onChange={(e) => setChoice(e.target.value)}>
            <option value="">{target ? "No account" : "Choose an account…"}</option>
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
        <>
          <span>
            {resolved ?? "No account"}
            {!known && resolved ? (
              <>
                {" "}
                <Tag tone="warn">Not an account Khipu knows</Tag>
              </>
            ) : null}
          </span>
          <span className="src">{words.how}</span>
          <span className="end">
            {words.tag ? (
              <Tag tone={words.tag.tone} dot>
                {words.tag.text}
              </Tag>
            ) : null}
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
        </>
      )}
      {!open && words.cands ? <div className="cands">{words.cands}</div> : null}
      {w.errors[id] ? (
        <div className="route-pause">
          <WriteError w={w} id={id} />
        </div>
      ) : null}
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
  else if (table?.changed_at) val = `Current-state section changed ${whenLocal(table.changed_at, now)}`;
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
    <div className="section-body">
        <div className="rows">
          <div className="row-item route-limit">
            <span className="route-limit-text">
              <span className="route-limit-name mono">{path ?? "No file set"}</span>
              <span className="set-help">
                An old file you kept by hand before Khipu had its own record. Khipu reads it but never writes to it and
                never takes an account's state from it; it only notes when the file's current-state section changes.
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
  );
}

function ChangesSection({ data, lists, w, now }: { data: RouteView; lists: ListControls; w: Writer; now: Date }) {
  const eok = useEndOrKeep(w);
  const days = changeDays(data, now);
  const active = new Set((data.rules ?? []).map((r) => r.n));
  const { changeBy, changeAbout } = lists.prefs;
  // Earlier days are loaded on request: how many have been asked for.
  const [more, setMore] = useState(0);
  const chipsRef = useRef<HTMLDivElement>(null);
  const byId = useId().replace(/:/g, "");
  const filtered = days
    .map((d) => ({ ...d, rows: d.rows.filter((r) => (changeBy === "all" || r.by === changeBy) && (changeAbout === "all" || r.about === changeAbout)) }))
    .filter((d) => d.rows.length);
  const today = filtered.find((d) => d.label === "Today");
  const earlier = filtered.filter((d) => d.label !== "Today");
  const shownEarlier = earlier.slice(0, more);
  const next = earlier[more];
  const filtering = changeBy !== "all" || changeAbout !== "all";
  const setBy = (v: ChangeByFilter) => {
    setMore(0);
    lists.set("changeBy", v);
  };
  const setAbout = (v: ChangeAboutFilter) => {
    setMore(0);
    lists.set("changeAbout", v);
  };
  const chipGroup = <K extends string>(id: string, label: string, options: { key: K; label: string }[], value: K, pick: (k: K) => void) => (
    <div className="route-filter">
      <span className="lbl" id={id}>
        {label}
      </span>
      <div className="chips" role="group" aria-labelledby={id}>
        {options.map((o) => (
          <button key={o.key} type="button" className={`chip${value === o.key ? " on" : ""}`} aria-pressed={value === o.key}
            onClick={() => pick(o.key)}>
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
  const dayBlock = (day: { key: string; label: string; rows: ChangeRow[] }) => {
    const headId = `route-day-${day.key}`;
    return (
      <div key={day.key} className="rows">
        <h2 className="rows-head route-day" id={headId} tabIndex={-1}>
          {day.label}
        </h2>
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
                          void eok.attempt(id, "khipu_route_rule", { action: "restore", n }, null, () => ruleRemoveButton(String(n), n));
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
  };
  return (
    <div className="section-body route-days">
      {days.length ? (
        <>
          <div className="who-kind" ref={chipsRef}>
            {chipGroup(`${byId}-by`, "Changes by", CHANGE_BY, changeBy, setBy)}
            {chipGroup(`${byId}-about`, "Changes to", CHANGE_ABOUT, changeAbout, setAbout)}
          </div>
          {filtered.length ? (
            <>
              {today ? (
                dayBlock(today)
              ) : (
                <div className="rows">
                  <h2 className="rows-head route-day">Today</h2>
                  <p className="set-help">{filtering ? "No changes today match these filters." : "No changes today."}</p>
                </div>
              )}
              {shownEarlier.map(dayBlock)}
              {next ? (
                <div className="toolbar">
                  <button type="button" className="sm" onClick={() => {
                    setMore(more + 1);
                    focusAfter(() => document.getElementById(`route-day-${next.key}`));
                  }}>
                    Show earlier days
                  </button>
                  <span className="route-src">{`${next.label} has ${plural(next.rows.length, "more change", "more changes")}`}</span>
                </div>
              ) : null}
            </>
          ) : (
            <p className="set-help" role="status">
              No changes match these filters.{" "}
              <button type="button" className="sm link" onClick={() => {
                setMore(0);
                lists.set("changeBy", "all");
                lists.set("changeAbout", "all");
                focusAfter(() => chipsRef.current?.querySelector("button"));
              }}>
                Show every change
              </button>
            </p>
          )}
        </>
      ) : (
        <p className="set-help">No changes yet.</p>
      )}
      {eok.ask ? (
        <EndOrKeepChoices
          view={data}
          ask={eok.ask}
          now={now}
          busy={w.anyBusy}
          saveLabel="Restore rule"
          onCancel={() => eok.setAsk(null)}
        />
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

const FOLDS_KEY = "khipu.routing.open";
const READING_NEEDS = "Reading what needs you…";

/** Which folds are open, as this Mac remembers them; null when nothing is
 *  remembered or storage can't be read (the page then opens Accounts only). */
function readFolds(): string[] | null {
  try {
    const raw = window.localStorage.getItem(FOLDS_KEY);
    if (raw == null) return null;
    const v: unknown = JSON.parse(raw);
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : null;
  } catch {
    return null;
  }
}

function writeFolds(keys: string[]): void {
  try {
    window.localStorage.setItem(FOLDS_KEY, JSON.stringify(keys));
  } catch {
    // Not remembered on this Mac this time; the page works the same.
  }
}

/** One window's small trend: the used line is the current level only (the
 *  pace object carries no history). The dashed line is the rate that lands
 *  on the target and the cap line is an owner cap, as
 *  docs/mockups/2026-10-21-routing draws it. */
function PaceTrendSvg({ trend }: { trend: PaceTrend }) {
  return (
    <svg className="trend" viewBox="0 0 120 28" role="img" aria-label={trend.label} preserveAspectRatio="none">
      {trend.capY != null ? <line className="cap" x1={0} x2={120} y1={trend.capY} y2={trend.capY} /> : null}
      {trend.need ? <line className="need" x1={0} y1={trend.need.y1} x2={120} y2={trend.need.y2} /> : null}
      <polyline className="used" points={trend.used} />
    </svg>
  );
}

/** A window row: its use, rate, the rate that lands on the limit or cap, and
 *  the trend. A reading that can't be placed says so and draws no trend. */
function PaceWindowRow({ w }: { w: PaceWindow }) {
  const tone = w.meterTone;
  return (
    <li className="pace-win">
      <span className="wname">{w.name}</span>
      {w.usedPct != null ? (
        <>
          <span className={["plan-meter", tone].filter(Boolean).join(" ")} role="progressbar"
            aria-label={`${w.name} used`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={w.usedPct}>
            <i style={{ width: `${Math.min(100, Math.max(0, w.usedPct))}%` }} />
          </span>
          <span className={["plan-pct", tone].filter(Boolean).join(" ")}>{w.usedPct}%</span>
        </>
      ) : (
        <>
          <span />
          <span />
        </>
      )}
      <span className="pace-rate">
        <span className="r1">
          {w.rateTag ? (
            <>
              {`${w.rate ?? "Can't tell"} · `}
              <Tag tone={w.rateTag.tone} dot={false}>{w.rateTag.text}</Tag>
            </>
          ) : (
            `${w.rate ?? "Can't tell"}${w.ratePlain ? ` · ${w.ratePlain}` : ""}`
          )}
        </span>
        <span className="r2">{w.r2}</span>
      </span>
      {w.trend ? <PaceTrendSvg trend={w.trend} /> : <span className="trend-none">no trend</span>}
    </li>
  );
}

/** Pace, caps and advice: one block for each account the approved mock opens
 *  (its plan outpacing or a cap within 6 h), then one line for the rest. Display
 *  only: caps are facts the owner confirmed in the CLI, so no control here
 *  adds, confirms or removes one, and advice is advisory. */
export function PaceSection({ data, plans, now, folds }: {
  data: RouteView;
  plans?: UsageReport | null;
  now: Date;
  folds: Folds;
}) {
  const model = paceModel(data, plans, now);
  const [openRows, setOpenRows] = useState<string[]>(() => model.rows.filter((r) => r.open).map((r) => r.key));
  const toggle = (key: string) => setOpenRows((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]));
  const open = model.rows.filter((r) => openRows.includes(r.key));
  const closed = model.rows.filter((r) => !openRows.includes(r.key));
  return (
    <div className="section-body pace-section">
      <p className="set-help">
        How fast each account is being used against its limit and your caps, at the last 24 hours&apos; rate (the last
        hour&apos;s for five-hour windows). Khipu&apos;s advice for jobs is advisory; it starts and stops nothing.
      </p>
      <div className="legend" aria-hidden="true">
        <span><i />used now</span>
        <span><i className="need" />rate that lands on the limit</span>
        <span><i className="cap" />your cap</span>
      </div>
      {model.empty ? (
        <EmptyState
          title="No usage readings yet"
          hint="Pace needs at least 3 readings in a day. Khipu reads plans every 30 min for a week, every 10 min for five hours."
        />
      ) : null}
      {open.length ? (
        <div className="pace-blocks">
          {open.map((row) => (
            <div key={row.key} className={`plan-block pace-block${row.tag.tone === "warn" || row.tag.tone === "err" ? " warn" : ""}`}
              role="group" aria-label={`${row.label} pace`} data-route-item={`pace:${row.key}`} tabIndex={-1}>
              <h3 className="plan-block-head">
                {row.label}
                {row.planLabel ? <span className="meta">{row.planLabel}</span> : null}
                <span className="spacer" />
                <Tag tone={row.tag.tone} dot>{row.tag.text}</Tag>
                <button type="button" className="pace-fold" aria-expanded="true" aria-label={`Close ${row.label} pace`}
                  onClick={() => toggle(row.key)}>
                  <ChevronRight size={14} className="disclosure-chevron" aria-hidden />
                </button>
              </h3>
              <p className="pace-say">
                {row.say.map((part, i) => part.bold ? <b key={i}>{part.text}</b> : <span key={i}>{part.text}</span>)}
              </p>
              {row.windows.length ? (
                <ul className="pace-wins" aria-label={`${row.label} windows`}>
                  {row.windows.map((w) => <PaceWindowRow key={w.key} w={w} />)}
                </ul>
              ) : null}
              {row.caps.length ? (
                <div className="pace-sec">
                  <h4>Your caps <span className="evid">· {row.capsEvid}</span></h4>
                  {row.caps.map((cap) => (
                    <div className="cap-row" key={cap.key}>
                      <span className="cname">{cap.name}</span>
                      <span className="cstate">
                        {cap.state}
                        {cap.tag ? <> · <Tag tone={cap.tag.tone} dot>{cap.tag.text}</Tag></> : null}
                      </span>
                      <span className="end" />
                    </div>
                  ))}
                </div>
              ) : null}
              {row.advice ? (
                <div className="pace-sec">
                  <h4>Khipu&apos;s advice <span className="evid">· {row.advice.evid}</span></h4>
                  <div className="adv-row">
                    <span className="who">Jobs</span>
                    <span className={`word ${row.advice.tone}`}>{row.advice.word}</span>
                    <span className="why">{row.advice.why}</span>
                  </div>
                  {row.advice.checks ? (
                    <div className="adv-row">
                      <span className="who">{row.advice.checks.label}</span>
                      <span className={`word ${row.advice.checks.tone}`}>{row.advice.checks.word}</span>
                      <span className="why">{row.advice.checks.why}</span>
                    </div>
                  ) : null}
                  {row.advice.lead ? (
                    <div className="adv-row">
                      <span className="who">Lead conversations</span>
                      <span className={`word ${row.advice.lead.tone}`}>{row.advice.lead.word}</span>
                      <span className="why">{row.advice.lead.why}</span>
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
      {closed.length ? (
        <div className="route-alist" role="list" aria-label="Other accounts">
          {closed.map((row) => (
            <div key={row.key} role="listitem">
              <div className="fold route-aline" data-open="false">
                <button type="button" className="fold-btn" aria-expanded="false" data-route-item={`pace:${row.key}`}
                  onClick={() => toggle(row.key)}>
                  <ChevronRight size={14} className="disclosure-chevron" aria-hidden />
                  <span className="an">{row.label}</span>
                  <span className="as"><Tag tone={row.tag.tone} dot>{row.tag.text}</Tag></span>
                  <span className="ap"><span className="ap-week">{row.line.week}</span></span>
                  <span className="at"><span className="ap-pace">{row.line.advice}</span></span>
                </button>
              </div>
            </div>
          ))}
        </div>
      ) : null}
      <SubFold k="sub:pace-push" name="When pace changes are sent to conversations"
        line="At a verdict change, and 24 h, 6 h, 1 h and 15 min before a limit" folds={folds}>
        <p className="set-help">
          A conversation hears when a window&apos;s verdict changes, and when its forecast crosses 24 hours, 6 hours,
          1 hour or 15 minutes. It hears each forecast once, and the advice never stops a job.
        </p>
      </SubFold>
    </div>
  );
}

function useFolds() {
  const [open, setOpen] = useState<string[]>(() => readFolds() ?? ["accounts"]);
  const change = (edit: (prev: string[]) => string[]) =>
    setOpen((prev) => {
      const next = edit(prev);
      writeFolds(next);
      return next;
    });
  const groups = GROUPS.map((g) => g.key as string);
  return {
    isOpen: (key: string) => open.includes(key),
    toggle: (key: string) => change((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key])),
    openKeys: (keys: string[]) => change((prev) => [...prev, ...keys.filter((k) => !prev.includes(k))]),
    setGroups: (on: boolean) =>
      change((prev) => (on ? [...prev, ...groups.filter((k) => !prev.includes(k))] : prev.filter((k) => !groups.includes(k)))),
  };
}

/** A tag on a group's closed line: its problems, and phase 9's neutral "N found". */
type ShownTag = { text: string; tone: GroupTag["tone"] | "accent" };

/** One group: a fold button with its name, closed line and problem tags, and
 *  its body, kept mounted while closed so a half-filled form keeps its words. */
function Group({
  k,
  name,
  line,
  tags,
  folds,
  children,
}: {
  k: GroupKey;
  name: string;
  line: string;
  tags: ShownTag[];
  folds: Folds;
  children: ReactNode;
}) {
  const open = folds.isOpen(k);
  const bodyId = `route-group-${k}`;
  return (
    <div className="fold disclose route-group" data-open={open}>
      <button type="button" className="disclosure fold-btn" aria-expanded={open} aria-controls={bodyId} data-settings-group={k}
        onClick={() => folds.toggle(k)}>
        <ChevronRight size={14} className="disclosure-chevron" aria-hidden />
        <span className="gname">{name}</span>
        <span className="gline">{line === "Reading…" ? <span className="reading">Reading…</span> : line}</span>
        <span className="gtags">
          {tags.map((t) => (
            <Tag key={t.text} tone={t.tone} dot>
              {t.text}
            </Tag>
          ))}
        </span>
      </button>
      <div className="fold-body" id={bodyId} role="region" aria-label={name} hidden={!open}>
        <div className="disclose-body">{children}</div>
      </div>
    </div>
  );
}

/** The routing record's read failure as one sentence's cause. */
function recordCause(error: string): string {
  if (error === ROUTE_READ_FAILED) return "Khipu's routing command failed";
  const t = error.trim().replace(/\.$/, "");
  return t.charAt(0).toLowerCase() + t.slice(1);
}

function selectorValue(v: string): string {
  return v.replace(/["\\]/g, "\\$&");
}

/** Settings → Accounts and routing (settings clarity phase 3): the intro,
 *  Needs you, and nine groups that fold, each saying on its closed line what
 *  it holds and every problem inside it. `plansState` is the plan read Home
 *  shares; `onSetUpAccounts` opens setup at its accounts step. */
export function RoutingSettings({
  active,
  plansState,
  onSetUpAccounts,
  deliveryHealth,
  deliveryHealthError,
  deliveryHealthReadAt,
  onRetryDelivery,
}: {
  active: boolean;
  plansState?: PlanUsageState | null;
  onSetUpAccounts?: () => void;
  /** The current app-wide `khipu doctor` delivery block, if it has been read. */
  deliveryHealth?: RouteDeliveryHealth | null;
  deliveryHealthError?: string | null;
  /** When that doctor report was read (ms epoch); it may be a cached report. */
  deliveryHealthReadAt?: number | null;
  /** Re-reads the existing app-wide doctor report; it changes no routing state. */
  onRetryDelivery?: () => Promise<unknown>;
}) {
  const route = useRouting(active);
  const config = useCliState<RouteConfig>("khipu_config_show", active);
  const features = useCliState<FeaturesShape>("khipu_features_show", active);
  const rereadRoute = () => route.load(true);
  const rereadConfig = async () => {
    await config.reload();
    await route.load(true);
  };
  const w = useWriter(rereadRoute);
  const folds = useFolds();
  const laneRaw = config.data?.routing?.routing_lane_map?.value;
  const finder = useAccountsFinder(active, {
    view: route.data,
    laneMap: laneRaw && typeof laneRaw === "object" && !Array.isArray(laneRaw) ? (laneRaw as Record<string, Record<string, string>>) : null,
    config: (config.data as FinderConfig | null) ?? null,
    rereadConfig: config.reload,
    onChanged: () => {
      void plansState?.reread();
      void route.load(true);
    },
    onShowAccount: (label) => {
      const a = (route.data?.accounts ?? []).find((x) => x.label === label);
      folds.openKeys(["accounts", ...(a ? [`account:${a.key}`] : [])]);
      setFocusItem({ item: a ? `account:${a.key}` : null, group: "accounts" });
    },
    onEdit: () => {
      folds.openKeys(["rules"]);
      setFocusItem({ item: null, group: "rules" });
    },
  });
  const lists = useListControls();
  const [focusItem, setFocusItem] = useState<{ item: string | null; group: GroupKey } | null>(null);
  const [retrying, setRetrying] = useState(false);
  const now = new Date();
  const data = route.data;
  const deliveryLiveAfter = config.data?.routing?.routing_delivery_live_after?.value;
  const deliveryLiveAfterSeconds = typeof deliveryLiveAfter === "number" && Number.isFinite(deliveryLiveAfter) && deliveryLiveAfter >= 0
    ? deliveryLiveAfter
    : undefined;
  // Folders and lanes come from both the routing record and the settings;
  // until both are read they say so, never "none yet".
  const recordRead: RecordRead =
    data && config.data ? "ready" : (!data && route.error) || (!config.data && config.error) ? "failed" : "reading";
  const push = features.data?.features?.routing_push ?? null;
  const settings: OverviewSettings = {
    routing: config.data?.routing ?? null,
    routingError: config.data ? null : config.error,
    push,
    pushError: features.error ?? (features.data && !push ? "Khipu's features list has no routing switch" : null),
    delivery: deliveryHealth ?? null,
    deliveryError: deliveryHealthError,
  };
  const recordError = data ? null : route.error;
  const overview = routingOverview(data, recordError, plansState?.data, plansState?.error, settings, now);
  // Phase 9: "Looking…" while discovery runs, never "0 found"; "N found" is a
  // neutral tag on the closed line, never a problem (rule 6 amended).
  if (finder.disc.looking) overview.lines.accounts = "Looking…";
  const nFound = foundCount(finder.disc.data);
  const foundTag: ShownTag | null = nFound && !finder.disc.looking ? { text: `${nFound} found`, tone: "accent" } : null;
  const needs = overview.problems.filter((p) => p.needs);
  // "Nothing here needs you" only once every read the problems come from is
  // in: the record, the settings (lanes, folders) and plan usage (plans,
  // sign-ins). A read still under way says so; a failed one says which
  // checks didn't run. Problems already found still list.
  const recordFailed = !data && !!route.error;
  const settingsFailed = !config.data && !!config.error;
  const plansFailed = !!plansState && plansState.data == null && !!plansState.error;
  const stillReading =
    (!data && !route.error) || (!config.data && !config.error) || !plansState || (plansState.data == null && !plansState.error);
  const notRun = [settingsFailed ? "its settings" : null, plansFailed ? "plan usage" : null].filter((x): x is string => !!x);
  const needsNote = recordFailed
    ? null
    : stillReading
      ? READING_NEEDS
      : notRun.length
        ? `Khipu couldn't read ${notRun.join(" or ")}, so some checks didn't run.`
        : needs.length
          ? null
          : "Nothing here needs you.";

  useEffect(() => {
    if (!focusItem) return;
    setFocusItem(null);
    const el =
      (focusItem.item ? document.querySelector<HTMLElement>(`[data-route-item="${selectorValue(focusItem.item)}"]`) : null) ??
      document.querySelector<HTMLElement>(`[data-settings-group="${focusItem.group}"]`);
    el?.scrollIntoView?.({ block: "center" });
    el?.focus();
  }, [focusItem]);

  const show = (p: Problem) => {
    // Clear whatever filter or search could hide the item Show points at.
    lists.reveal(p.group);
    // A job runner's line lives inside the Providers and lanes group's fold.
    folds.openKeys([p.group, ...(p.item?.startsWith("account:") ? [p.item] : []), ...(p.item?.startsWith("runner:") ? ["sub:folders"] : [])]);
    setFocusItem({ item: p.item ?? null, group: p.group });
  };

  const unread = route.error ? (
    <p className="set-error">Khipu couldn't read the routing record, so this stays blank until the read works.</p>
  ) : (
    <p className="set-help" role="status">
      Reading the routing record…
    </p>
  );

  const body: Record<GroupKey, ReactNode> = {
    accounts: data ? (
      <AccountsSection data={data} plansState={plansState} problems={overview.problems} lists={lists} active={active}
        staleAfterS={staleAfterSeconds(settings)} w={w} now={now} reread={rereadRoute}
        finder={finder} />
    ) : (
      unread
    ),
    pace: data ? <PaceSection data={data} plans={plansState?.data ?? null} now={now} folds={folds} /> : unread,
    priority: data ? <PrioritySection data={data} cfg={config.data} w={w} now={now} reread={rereadConfig} /> : unread,
    rules: data ? (
      <>
        <NoteSection data={data} w={w} now={now} />
        <RulesSection data={data} lists={lists} w={w} now={now} />
      </>
    ) : (
      unread
    ),
    holds: data ? (
      <>
        <ProposalsSection data={data} w={w} now={now} />
        <HoldsSection data={data} cfg={config.data} w={w} now={now} reread={rereadConfig} />
      </>
    ) : unread,
    checks: data ? (
      <ChecksSection data={data} now={now} folds={folds} />
    ) : route.error ? (
      <p className="set-error">
        Couldn't read the checks: {recordCause(route.error)}. That is not the same as nothing found.
      </p>
    ) : (
      <p className="set-help" role="status">
        Reading the checks…
      </p>
    ),
    telling: (
      <TellingSection
        data={data}
        deliveryHealth={deliveryHealth}
        deliveryHealthError={deliveryHealthError}
        deliveryHealthReadAt={deliveryHealthReadAt}
        deliveryLiveAfterSeconds={deliveryLiveAfterSeconds}
        onRetryDelivery={onRetryDelivery}
        now={now}
        cfg={config.data}
        features={features.data}
        featuresError={features.error}
        cfgError={config.error}
        w={w}
        reread={rereadConfig}
        rereadFeatures={features.reload}
        folds={folds}
      />
    ),
    resets: <ResetChecksSection data={data} cfg={config.data} cfgError={config.error} w={w} reread={rereadConfig} />,
    runners: <ProvidersLanesSection data={data} cfg={config.data} lists={lists} recordRead={recordRead} w={w} now={now} reread={rereadConfig} folds={folds} />,
    table: <TableSection data={data} cfg={config.data} w={w} now={now} reread={rereadConfig} />,
    changes: data ? <ChangesSection data={data} lists={lists} w={w} now={now} /> : unread,
  };

  return (
    <div className="route-settings">
      <div className="route-top">
        <p className="set-help set-intro">
          Khipu tells your conversations which accounts have room, what you paused and what your rules are. Your agents
          decide where work goes.
        </p>
        <div className="grp-btns">
          <button type="button" className="sm" onClick={() => folds.setGroups(true)}>
            Open all groups
          </button>
          <button type="button" className="sm" onClick={() => folds.setGroups(false)}>
            Close all groups
          </button>
        </div>
      </div>
      <SettingRequests cfg={config.data} w={w} reread={rereadConfig} />
      {route.error ? (
        <div className="set-error err-inline" role="alert">
          <span>
            {data
              ? `Couldn't read the routing record just now: ${recordCause(route.error)}. What shows below is from the last read.`
              : `Couldn't read the routing record: ${recordCause(route.error)}. Nothing below is a guess; it stays blank until the read works.`}
          </span>
          <button
            type="button"
            className="sm"
            disabled={retrying}
            aria-busy={retrying || undefined}
            onClick={async () => {
              setRetrying(true);
              try {
                await route.load(true);
              } finally {
                setRetrying(false);
              }
            }}
          >
            <Busy on={retrying} />
            Try again
          </button>
        </div>
      ) : null}
      {data && data.record === false ? (
        <div className="callout route-none" role="region" aria-label="No routing record on this Mac">
          <div className="txt">
            <b>No routing record on this Mac</b>
            <span>
              Khipu starts one when a plan check, a job runner folder or a conversation reports on an account, or when
              you add a rule, a note or a hold here. Until then no account is shown as open.
            </span>
            {onSetUpAccounts ? (
              <div className="toolbar">
                <button type="button" className="primary sm" onClick={onSetUpAccounts}>
                  Set up accounts
                </button>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
      {needs.length ? (
        <div className="callout warn needs" role="region" aria-label="Needs you">
          <div className="txt">
            <b>Needs you</b>
            <ul>
              {needs.map((p) => (
                <li key={p.key}>
                  <span>{p.needs}</span>
                  <button type="button" className="sm" aria-label={`Show ${p.show ?? "it"}`} onClick={() => show(p)}>
                    Show
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
      {needsNote ? (
        <p className="needs-none" role={needsNote === READING_NEEDS ? "status" : undefined}>
          {needsNote}
        </p>
      ) : null}
      {GROUPS.map((g) => (
        <Group key={g.key} k={g.key} name={g.name} line={overview.lines[g.key]}
          tags={g.key === "accounts" && foundTag ? [foundTag, ...overview.tags[g.key]] : overview.tags[g.key]} folds={folds}>
          {body[g.key]}
        </Group>
      ))}
      {config.error && !config.data ? (
        <p className="set-error" role="alert">
          Could not read the settings: {config.error}
        </p>
      ) : null}
    </div>
  );
}
