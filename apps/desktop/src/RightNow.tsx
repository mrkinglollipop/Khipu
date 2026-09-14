// D3: Home's "Right now" card. `session_capture.liveness_all()` already
// tracked pending turns, queue depth and (as of this phase) captured-today
// per harness — none of it reached Home before this (Home showed four tiles
// and five captures and nothing about what is waiting right now). Pulled
// into its own file, the same way `HealthReportRows` is, so it can be
// rendered and tested without the rest of App.tsx.
import { EmptyState, ListRow } from "./ui";
import { LABEL as HARNESS_LABEL } from "./IntegrationsPanel";
import type { LivenessPayload } from "./IntegrationsPanel";

/** `seconds` -> "4m" / "2h" / "3d", the same register `App.tsx`'s own
 *  `formatAge` uses — duplicated rather than imported to keep this file
 *  free of an App.tsx dependency (App.tsx already imports from here). */
function formatAge(seconds: number | null | undefined): string {
  if (seconds == null) return "an unknown time";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

export function RightNowCard({
  liveness,
  busy,
  message,
  onCaptureNow,
}: {
  liveness: LivenessPayload | null;
  busy: boolean;
  message: string | null;
  onCaptureNow: () => void;
}) {
  const harnesses = Object.entries(liveness?.harnesses ?? {}).filter(
    ([, h]) => h.seen,
  );
  return (
    <div className="card">
      <div className="card-head">
        Right now
        <span className="spacer" />
        <button
          type="button"
          className="sm"
          disabled={busy}
          onClick={onCaptureNow}
        >
          {busy ? "Capturing…" : "Capture now"}
        </button>
      </div>
      {harnesses.length > 0 ? (
        <>
          {harnesses.map(([id, h]) => {
            const pending = h.pending_turns ?? 0;
            return (
              <ListRow key={id}>
                <span
                  className={`hdot ${h.ok === false ? "err" : "ok"}`}
                  aria-hidden
                />
                <span className="grow ellip t2">
                  {(HARNESS_LABEL as Record<string, string>)[id] ?? id}
                </span>
                <span className="meta">
                  {pending > 0
                    ? `${pending} turn${pending === 1 ? "" : "s"} waiting for capture`
                    : "nothing waiting"}
                  {" · last capture "}
                  {h.last_captured_age_s != null
                    ? `${formatAge(h.last_captured_age_s)} ago`
                    : "never"}
                </span>
              </ListRow>
            );
          })}
          <ListRow>
            <span className="meta">
              Queue depth {liveness?.queue_depth ?? 0} · captured today{" "}
              {liveness?.captured_today ?? 0}
            </span>
          </ListRow>
        </>
      ) : (
        <EmptyState
          title="No live sessions yet"
          hint="A harness shows up here after its first hook run."
        />
      )}
      {message ? <div className="meta">{message}</div> : null}
    </div>
  );
}
