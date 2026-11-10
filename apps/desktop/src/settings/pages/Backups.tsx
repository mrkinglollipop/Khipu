import { Tag } from "../../ui";
import type { HealthDoc, Loaded, MacsDoc } from "../data";
import { macName } from "../data";
import { fmtAgoSince } from "../../time";

/** Read-only: the newest successful backup and the free space, from the status
 *  commands. Nothing here says how often backups run; Khipu does not report it. */
export function BackupsPage({ macs, health }: { macs: Loaded<MacsDoc>; health: Loaded<HealthDoc> }) {
  const area = health.data?.areas.find((a) => a.key === "database") ?? null;
  const rows = macs.data?.macs ?? [];
  const mine = rows.find((m) => m.this_mac);
  const lastBackup = health.data?.last_backup_at ?? mine?.health?.last_backup_at ?? null;
  const when = (iso: string | null | undefined) => (iso ? fmtAgoSince(iso) ?? "at an unknown time" : "none recorded");
  if ((macs.loading && !macs.data) && (health.loading && !health.data)) return <p className="st-muted" role="status">Reading…</p>;
  return (
    <>
      <div className="st-kvs">
        <div className="st-kv"><div className="st-k">Last backup</div><div className="st-v">{when(lastBackup)}</div></div>
        {health.data?.free_gb != null ? (
          <div className="st-kv"><div className="st-k">Free space on this Mac</div><div className="st-v">{health.data.free_gb} GB</div></div>
        ) : null}
        {rows.filter((m) => !m.this_mac).map((m) => (
          <div className="st-kv" key={m.mac}>
            <div className="st-k">{macName(m)}{m.stale ? <Tag tone="warn">Not heard from lately</Tag> : null}</div>
            <div className="st-v">
              {m.health?.last_backup_at ? `Last backup ${when(m.health.last_backup_at)}` : "No backup reported"}
              {m.stale ? " (may be out of date)" : ""}
            </div>
          </div>
        ))}
      </div>
      {area ? (
        <p className={area.state === "ok" ? "st-muted" : undefined}>
          {area.line}{area.khipu_doing ? ` ${area.khipu_doing}` : ""}
        </p>
      ) : health.loading ? <p className="st-muted" role="status">Checking the database and disk…</p> : health.error ? <p className="st-muted">The full check did not finish: {health.error}</p> : null}
    </>
  );
}
