/* ---------------------------------------------------------------------------
   SetupStages — the connect-pipeline checklist shared by the Welcome
   Database step, Settings › Database › Move, and (via the same shape) any
   future caller of `khipu.setup.connect_database` / `move_database`.

   docs/plans/2026-09-05-setup-that-cannot-strand-you.md, "the rule every
   screen follows": every step ends in exactly one of three states — Working
   (proved), Needs you (one action), or Fixing it… (progress). This component
   is that rule rendered: one row per stage with a state mark, its detail in
   plain words, and — for a failed stage — a Callout carrying the engine's
   `fix` text and, where the app can act on it, exactly one button.

   It never renders a raw code. `khipu.setup`/`khipu.dbmove` promise every
   failure carries a plain-words `title`/`detail`/`fix` (see
   packages/cli/khipu/setup.py's module docstring), but a payload is still
   external input — a future stage, a CLI error string, an `out.error` code
   like `target_not_empty` reaching a row directly — so this component
   defends the promise itself: any string that is nothing but
   `/^[a-z_]+$/` is swapped for a generic sentence before it reaches the DOM.
--------------------------------------------------------------------------- */

import { Check, X, Minus, Circle, Loader2 } from "lucide-react";
import { Callout, ListRow, Tag } from "./ui";
import type { Tone } from "./ui";

export type SetupStageResult = {
  id: string;
  ok?: boolean;
  status?: "skipped" | string;
  title?: string;
  detail?: string;
  fix?: string;
  seconds?: number;
};

/** The connect pipeline's own shape (`connect_database` / `move_database`'s
 *  preflight sub-call) — `docs/plans/2026-09-05-setup-that-cannot-strand-you.md`
 *  stages 1-9. A top-level `error`/`detail` (not stage-shaped — e.g.
 *  `target_preflight_failed`) is handled the same way as a stage failure. */
export type SetupPipelineResult = {
  ok?: boolean;
  stages?: SetupStageResult[];
  summary?: Record<string, unknown>;
  error?: string;
  detail?: string;
} | null;

export type SetupPhase = "idle" | "running" | "done";

/** Stage id -> plain-words row title, independent of the engine's own
 *  `title` (which is itself plain words on success, but a caller may want a
 *  stable label while a row is still pending/running and has no `title` yet). */
const STAGE_LABEL: Record<string, string> = {
  reach: "Reach the server",
  version: "Postgres version",
  privileges: "Privileges",
  schema: "Schema",
  graph: "Graph",
  store: "Save the connection",
  upkeep: "Nightly upkeep",
  prove: "Memory round trip",
  summary: "Working",
};

const RAW_CODE_RE = /^[a-z_]+$/;

/** Never hand the DOM a bare code. Anything that is nothing but
 *  lowercase-and-underscores (`vector_extension_missing`, `target_not_empty`,
 *  `dsn_required`) is a stage id or an error code, not a sentence — swap it
 *  for a sentence that still tells the truth: something failed and where to
 *  look, never a made-up explanation of what.
 *
 *  Applied only to whole payload fields (a stage's `title`/`detail`/`fix`, or
 *  a top-level `error`/`detail`) — never to surrounding prose this component
 *  writes itself, so an ordinary short word never collides with it. */
function plainWords(text: string | undefined | null): string | null {
  if (text == null) return null;
  const trimmed = text.trim();
  if (!trimmed) return null;
  if (RAW_CODE_RE.test(trimmed)) {
    return "Something went wrong and Khipu could not describe it in plain words (details in the health report).";
  }
  return text;
}

type StageState = "ok" | "skipped" | "failed" | "pending" | "running";

function stateOf(entry: SetupStageResult | undefined, phase: SetupPhase): StageState {
  if (entry) {
    if (entry.status === "skipped") return "skipped";
    return entry.ok === false ? "failed" : "ok";
  }
  return phase === "running" ? "running" : "pending";
}

const STATE_TONE: Record<StageState, Tone> = {
  ok: "ok",
  skipped: "neutral",
  failed: "err",
  pending: "neutral",
  running: "accent",
};

const STATE_ICON: Record<StageState, typeof Check> = {
  ok: Check,
  skipped: Minus,
  failed: X,
  pending: Circle,
  running: Loader2,
};

const STATE_WORD: Record<StageState, string> = {
  ok: "Working",
  skipped: "Skipped",
  failed: "Needs you",
  pending: "Waiting",
  running: "Checking…",
};

/** True when a stage's `fix` describes something the picker below Move
 *  dialog / Database step can act on directly, rather than an instruction
 *  for outside this app (ask your host, upgrade the server). */
function fixWantsCertificate(fix: string | undefined): boolean {
  return Boolean(fix && /certificate|root\.crt|ca\.pem/i.test(fix));
}

export function SetupStages({
  stageIds,
  result,
  phase,
  onRetry,
  onPasteCertificate,
  busy = false,
}: {
  /** Which stage ids this call puts in play, in order — the preflight ids
   *  (`reach`..`graph`) or the full connect pipeline (`reach`..`summary`). */
  stageIds: readonly string[];
  result: SetupPipelineResult;
  phase: SetupPhase;
  /** Re-run the whole pipeline from the top — the only correct response to
   *  "try again" here, since a later stage's success can depend on an
   *  earlier one having just been fixed outside the app. */
  onRetry?: () => void;
  onPasteCertificate?: () => void;
  busy?: boolean;
}) {
  const byId = new Map((result?.stages ?? []).map((s) => [s.id, s]));
  const topLevelError =
    phase === "done" && result && result.ok === false && !result.stages?.length
      ? plainWords(result.detail || result.error)
      : null;

  return (
    <div className="setup-stages card">
      <div className="setup-stages-body">
        {stageIds.map((id) => {
          const entry = byId.get(id);
          const state = stateOf(entry, phase);
          const Icon = STATE_ICON[state];
          const title = entry?.title ? plainWords(entry.title) : STAGE_LABEL[id] ?? id;
          const detail = entry ? plainWords(entry.detail) : null;
          const fix = state === "failed" ? plainWords(entry?.fix) : null;
          return (
            <ListRow key={id} className="setup-stage-row">
              <Tag tone={STATE_TONE[state]} title={STATE_WORD[state]}>
                <Icon
                  size={13}
                  strokeWidth={2.25}
                  className={state === "running" ? "spin" : undefined}
                  aria-hidden
                />
                {STATE_WORD[state]}
              </Tag>
              <div className="setup-stage-text">
                <b>{STAGE_LABEL[id] ?? title}</b>
                {detail ? <span className="muted">{detail}</span> : null}
                {fix ? (
                  <Callout
                    tone="err"
                    title="Needs you"
                    action={
                      fixWantsCertificate(entry?.fix) && onPasteCertificate ? (
                        <button
                          type="button"
                          className="primary"
                          disabled={busy}
                          onClick={onPasteCertificate}
                        >
                          Paste the certificate file…
                        </button>
                      ) : onRetry ? (
                        <button type="button" className="primary" disabled={busy} onClick={onRetry}>
                          {busy ? "Trying again…" : "Try again"}
                        </button>
                      ) : undefined
                    }
                  >
                    {fix}
                  </Callout>
                ) : null}
              </div>
            </ListRow>
          );
        })}
      </div>
      {topLevelError ? (
        // Top-level errors (a payload that failed before it reached the
        // stage list, e.g. dbmove's `target_preflight_failed`) still land
        // inside the same card, not floating outside it.
        <Callout
          tone="err"
          title="Needs you"
          action={
            onRetry ? (
              <button type="button" className="primary" disabled={busy} onClick={onRetry}>
                {busy ? "Trying again…" : "Try again"}
              </button>
            ) : undefined
          }
        >
          {topLevelError}
        </Callout>
      ) : null}
    </div>
  );
}
