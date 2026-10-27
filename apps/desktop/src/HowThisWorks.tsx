import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Info, X } from "lucide-react";

/** "How this works": a text link with an icon (never an icon alone) that opens
 *  a short explanation beside the thing, with a way into the full guide. It is
 *  a popover, not a modal: Escape or Close puts focus back on the link. */
export function HowThisWorks({
  title,
  items,
  openLabel = "Open the setup guide",
  onOpenGuide,
  className,
  linkLabel = "How this works",
}: {
  title: string;
  items: Array<{ term: string; text: ReactNode }>;
  openLabel?: string;
  /** Omitted for a harness with no guide page yet: the popover then has Close only. */
  onOpenGuide?: () => void;
  className?: string;
  linkLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const linkRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) popRef.current?.focus();
  }, [open]);

  const close = () => {
    setOpen(false);
    linkRef.current?.focus();
  };

  return (
    <>
      <button
        ref={linkRef}
        type="button"
        className={`link gd-how ${className ?? ""}`.trim()}
        aria-expanded={open}
        aria-controls={id}
        onClick={() => (open ? close() : setOpen(true))}
      >
        <Info size={14} aria-hidden /> {linkLabel}
      </button>
      {open ? (
        <div
          ref={popRef}
          id={id}
          className="gd-pop"
          role="dialog"
          aria-labelledby={`${id}-h`}
          tabIndex={-1}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.stopPropagation();
              close();
            }
          }}
        >
          <h4 id={`${id}-h`}>
            {title}
            <span className="spacer" />
            <button type="button" className="gd-iconbtn" aria-label="Close" onClick={close}>
              <X size={14} aria-hidden />
            </button>
          </h4>
          <dl>
            {items.map((it) => (
              <div key={it.term}>
                <dt>{it.term}</dt>
                <dd>{it.text}</dd>
              </div>
            ))}
          </dl>
          <div className="gd-pop-acts">
            {onOpenGuide ? (
              <button
                type="button"
                className="primary sm"
                onClick={() => {
                  setOpen(false);
                  onOpenGuide();
                }}
              >
                {openLabel}
              </button>
            ) : null}
            <button type="button" className="sm" onClick={close}>
              Close
            </button>
          </div>
        </div>
      ) : null}
    </>
  );
}
