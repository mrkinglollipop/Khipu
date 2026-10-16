/* The Owed tab when the hub is away (docs/plans/2026-10-16-offline-and-self-healing.md,
   slice 6). `khipu owed --envelope` answers {results, stale, snapshot_refreshed_at,
   waiting_to_sync}; a load that times out or cannot connect is shown as the
   same kind of banner, never as an endless spinner or a raw error. */

import { Tag } from "./ui";

export type OwedOffline =
  | { kind: "stale"; refreshedAt: string | null }
  | { kind: "unreachable" };

export type OwedAnswer = {
  rows: unknown[];
  stale: boolean;
  refreshedAt: string | null;
  waitingToSync: boolean;
};

/** Reads either the envelope or, for safety, a bare list. */
export function parseOwedAnswer(parsed: unknown): OwedAnswer | null {
  if (Array.isArray(parsed)) return { rows: parsed, stale: false, refreshedAt: null, waitingToSync: false };
  if (parsed && typeof parsed === "object") {
    const o = parsed as Record<string, unknown>;
    if (Array.isArray(o.results)) {
      return {
        rows: o.results,
        stale: o.stale === true,
        refreshedAt: typeof o.snapshot_refreshed_at === "string" ? o.snapshot_refreshed_at : null,
        waitingToSync: o.waiting_to_sync === true,
      };
    }
  }
  return null;
}

/** The app's own timeout message, or the hub's connection errors. */
export function isHubUnreachable(message: string): boolean {
  return /Khipu did not answer within|connection (timeout|refused|failed|is bad)|timeout expired|could not (connect|translate host)|server closed the connection|hub (is )?not reachable|OperationalError/i.test(
    message,
  );
}

/** Local time as HH:MM, or null when the stamp is missing or unreadable. */
export function clockTime(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const two = (n: number) => String(n).padStart(2, "0");
  return `${two(d.getHours())}:${two(d.getMinutes())}`;
}

export function OfflineBanner({ state }: { state: OwedOffline | null }) {
  if (!state) return null;
  let text: string;
  if (state.kind === "stale") {
    const at = clockTime(state.refreshedAt);
    text = `Hub not reachable. Showing this Mac's copy${at ? ` from ${at}` : ""}.`;
  } else {
    text = "Hub not reachable. Khipu will try again.";
  }
  return (
    <div className="callout warn stripe offline-banner" role="status">
      <div className="txt">
        <b>{text}</b>
      </div>
    </div>
  );
}

/** Shown after an owner write that was queued instead of sent. */
export function SavedOnThisMac({ show }: { show: boolean }) {
  if (!show) return null;
  return (
    <div className="callout stripe neutral offline-banner" role="status">
      <div className="txt">
        <b>Saved on this Mac. It will sync when the hub is back.</b>
      </div>
    </div>
  );
}

/** A row's queued change, in words (not color only). */
export function WaitingToSync({ pending }: { pending: unknown }) {
  if (!pending) return null;
  return (
    <Tag tone="warn" title="This change is saved on this Mac and will reach the hub when it is back.">
      Waiting to sync
    </Tag>
  );
}
