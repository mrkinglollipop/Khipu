import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";

/** Unmissable "this is working, not frozen" strip. The elapsed clock is the proof. */
export function WorkingBanner({ label }: { label: string | null }) {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!label) {
      setElapsed(0);
      return;
    }
    setElapsed(0);
    const t0 = Date.now();
    const id = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - t0) / 1000));
    }, 250);
    return () => window.clearInterval(id);
  }, [label]);
  if (!label) return null;
  const clock =
    elapsed < 60
      ? `${elapsed}s`
      : `${Math.floor(elapsed / 60)}m ${String(elapsed % 60).padStart(2, "0")}s`;
  return (
    <div className="working-banner" role="status" aria-live="polite" aria-busy="true">
      <Loader2 size={14} className="spin" aria-hidden />
      <span className="working-banner-label">{label}</span>
      <span className="working-banner-elapsed">{clock}</span>
      {elapsed >= 8 ? (
        <span className="working-banner-hint">Still working — not frozen.</span>
      ) : null}
    </div>
  );
}
