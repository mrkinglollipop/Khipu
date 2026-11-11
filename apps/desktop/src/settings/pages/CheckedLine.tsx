import { fmtAgo } from "../../time";
import type { HealthState } from "../data";

/** "Checked 4 minutes ago", and a quiet note while a newer check runs. */
export function CheckedLine({ health }: { health: HealthState }) {
  if (!health.data) {
    return health.refreshing ? <p className="st-muted" role="status">Checking…</p> : null;
  }
  const ago = health.at == null ? null : fmtAgo((Date.now() - health.at) / 1000);
  return (
    <p className="st-muted" role="status">
      {ago ? `Checked ${ago}.` : "Checked earlier."}
      {health.refreshing ? " Checking again…" : ""}
    </p>
  );
}
