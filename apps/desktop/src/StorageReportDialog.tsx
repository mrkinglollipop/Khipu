/* "Open storage report" (docs/plans/2026-10-16-offline-and-self-healing.md,
   slice 8): runs the read-only `khipu storage` and says, in plain words, how
   much room each place Khipu writes to has left and how much Khipu itself
   holds. The caller owns the open flag; the answer is fetched each time the
   dialog opens, so it is never stale. */

import { useEffect, useId, useRef, useState } from "react";
import { Dialog } from "./ui";

export type Level = "ok" | "amber" | "red";
export type StorageVolume = {
  label: string;
  path?: string;
  free_bytes: number;
  total_bytes: number;
  free_percent: number;
  level: Level;
};
export type StorageAnswer = {
  disk_space?: { level?: Level; message?: string; volumes?: StorageVolume[] };
  storage?: {
    total_bytes?: number;
    line?: string;
    places?: Array<{ name: string; bytes: number }>;
  };
};

const PLACE_WORDS: Record<string, string> = {
  replica: "This Mac's copy of your memory",
  logs: "Logs",
  backups: "Backups",
  temp: "Temporary files",
};

export const LEVEL_WORDS: Record<Level, string> = {
  ok: "Plenty of room",
  amber: "Running low",
  red: "Almost full",
};

export function sizeWords(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "unknown";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = bytes;
  let i = 0;
  while (n >= 1000 && i < units.length - 1) {
    n /= 1000;
    i += 1;
  }
  return `${i === 0 || n >= 100 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

export function volumeWords(v: StorageVolume): string {
  const pct = Math.round(v.free_percent * 10) / 10;
  return `${sizeWords(v.free_bytes)} free of ${sizeWords(v.total_bytes)} (${pct}% free)`;
}

export function StorageReportDialog({
  open,
  run,
  onClose,
}: {
  open: boolean;
  run: (args: string[]) => Promise<string>;
  onClose: () => void;
}) {
  const headingId = useId();
  const closeRef = useRef<HTMLButtonElement>(null);
  const [answer, setAnswer] = useState<StorageAnswer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) return;
    let live = true;
    setAnswer(null);
    setError(null);
    setLoading(true);
    run(["storage"])
      .then((raw) => {
        if (!live) return;
        try {
          setAnswer(JSON.parse(raw) as StorageAnswer);
        } catch {
          setError("Khipu gave an answer the app could not read.");
        }
      })
      .catch((e) => {
        if (live) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [open, run]);

  const volumes = answer?.disk_space?.volumes ?? [];
  const places = answer?.storage?.places ?? [];
  return (
    <Dialog open={open} className="kit-dialog" ariaLabelledBy={headingId} initialFocusRef={closeRef} onCancel={onClose}>
      <div className="kit-dialog-body">
        <h2 id={headingId}>Storage report</h2>
        {loading ? <p role="status">Checking the disk...</p> : null}
        {error ? (
          <p role="alert">Could not read the storage report: {error}</p>
        ) : null}
        {answer ? (
          <>
            {answer.disk_space?.message ? <p>{answer.disk_space.message}</p> : null}
            <div className="rows-head">Free space</div>
            <div className="rows">
              {volumes.length === 0 ? <p className="muted">No disk figures were reported.</p> : null}
              {volumes.map((v) => (
                <div key={v.label} className="row-item">
                  <span className="row-main">{v.label}</span>
                  <span className="row-meta wrap">
                    {volumeWords(v)}. {LEVEL_WORDS[v.level] ?? v.level}.
                  </span>
                </div>
              ))}
            </div>
            <div className="rows-head">What Khipu holds</div>
            <div className="rows">
              {places.length === 0 ? <p className="muted">No sizes were reported.</p> : null}
              {places.map((p) => (
                <div key={p.name} className="row-item">
                  <span className="row-main">{PLACE_WORDS[p.name] ?? p.name}</span>
                  <span className="row-meta">{sizeWords(p.bytes)}</span>
                </div>
              ))}
              {typeof answer.storage?.total_bytes === "number" ? (
                <div className="row-item">
                  <span className="row-main">Total</span>
                  <span className="row-meta">{sizeWords(answer.storage.total_bytes)}</span>
                </div>
              ) : null}
            </div>
          </>
        ) : null}
        <div className="kit-dialog-actions">
          <button type="button" ref={closeRef} onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </Dialog>
  );
}
