import { useState } from "react";
import { Tag } from "../../ui";
import type { HealthState, Loaded, MacsDoc } from "../data";
import { CheckedLine } from "./CheckedLine";
import { macName } from "../data";
import { fmtAgoSince } from "../../time";

type BackupState =
  | { kind: "idle" }
  | { kind: "busy" }
  | { kind: "done"; archive: string | null; files: number | null }
  | { kind: "error"; message: string };

/** The newest successful backup and the free space, from the status commands,
 *  plus Back up now: the same `backup-local --out=~/Downloads` the older Data
 *  screen ran. Nothing here says how often backups run; Khipu does not report it. */
export function BackupsPage({ macs, health, run, onBackedUp }: {
  macs: Loaded<MacsDoc>;
  health: HealthState;
  run: (args: string[]) => Promise<string>;
  onBackedUp?: () => void;
}) {
  const [state, setState] = useState<BackupState>({ kind: "idle" });
  const backUp = async () => {
    setState({ kind: "busy" });
    try {
      const raw = await run(["backup-local", "--out=~/Downloads"]);
      let doc: { ok?: boolean; error?: string; archive?: string; files?: number } | null = null;
      try { doc = JSON.parse(raw); } catch { doc = null; }
      if (!doc || typeof doc !== "object" || doc.ok !== true) {
        setState({ kind: "error", message: (doc && doc.error) || "Khipu did not say the backup was written." });
        return;
      }
      setState({ kind: "done", archive: doc.archive ?? null, files: typeof doc.files === "number" ? doc.files : null });
      onBackedUp?.();
    } catch (e) {
      setState({ kind: "error", message: String(e) });
    }
  };
  const area = health.data?.areas.find((a) => a.key === "database") ?? null;
  const rows = macs.data?.macs ?? [];
  const mine = rows.find((m) => m.this_mac);
  const lastBackup = health.data?.last_backup_at ?? mine?.health?.last_backup_at ?? null;
  const when = (iso: string | null | undefined) => (iso ? fmtAgoSince(iso) ?? "at an unknown time" : "none recorded");
  if (macs.loading && !macs.data && !health.data) return <p className="st-muted" role="status">Reading…</p>;
  return (
    <>
      <div className="st-actions">
        <button type="button" className="primary" disabled={state.kind === "busy"} onClick={() => void backUp()}>
          {state.kind === "busy" ? "Backing up…" : "Back up now"}
        </button>
      </div>
      <div>
        {state.kind === "busy" ? <p className="st-muted" role="status">Writing a zip of this Mac's Khipu files to Downloads…</p> : null}
        {state.kind === "done" ? (
          <p role="status">
            Backed up{state.files != null ? ` ${state.files} files` : ""}{state.archive ? ` to ${state.archive}` : ""}.
          </p>
        ) : null}
        {state.kind === "error" ? <p role="alert">The backup did not finish: {state.message}</p> : null}
      </div>
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
      ) : null}
      <CheckedLine health={health} />
    </>
  );
}
