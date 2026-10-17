import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";

/** The custom property a sticky panel heading offsets its `top` by, so it
 *  sticks below the strip instead of under it (App.css `.panel-head`). */
export const BANNER_HEIGHT_VAR = "--working-banner-h";

/** Unmissable "this is working, not frozen" strip. The elapsed clock is the proof. */
export function WorkingBanner({ label }: { label: string | null }) {
  const [elapsed, setElapsed] = useState(0);
  const strip = useRef<HTMLDivElement>(null);
  // The strip and the panel headings both stick to the top of the same
  // scroller. Its height changes with the label and the window width (the
  // hint wraps), so it is measured, and the scroller carries it while shown.
  useLayoutEffect(() => {
    const el = strip.current;
    const host = el?.parentElement;
    if (!el || !host) return;
    const measure = () => host.style.setProperty(BANNER_HEIGHT_VAR, `${el.offsetHeight}px`);
    measure();
    const ro = typeof ResizeObserver === "function" ? new ResizeObserver(measure) : null;
    ro?.observe(el);
    return () => {
      ro?.disconnect();
      host.style.removeProperty(BANNER_HEIGHT_VAR);
    };
  }, [label]);
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
    <div ref={strip} className="working-banner" role="status" aria-live="polite" aria-busy="true">
      <Loader2 size={14} className="spin" aria-hidden />
      <span className="working-banner-label">{label}</span>
      <span className="working-banner-elapsed">{clock}</span>
      {elapsed >= 8 ? (
        <span className="working-banner-hint">Still working — not frozen.</span>
      ) : null}
    </div>
  );
}
