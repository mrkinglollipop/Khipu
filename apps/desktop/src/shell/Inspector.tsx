// A panel docked at the right of <Body> (380 px), for things that open beside
// the page: the five checks, All changes. Under 900 px it covers the window
// as a sheet instead. Render it only while open; Escape and the close button
// call onClose.
import type { KeyboardEvent, ReactNode } from "react";
import { X } from "lucide-react";
import "./shell.css";

export function Inspector({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const key = (e: KeyboardEvent) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      onClose();
    }
  };
  return (
    <aside className="sh-insp" aria-label={title} onKeyDown={key}>
      <div className="sh-insp-h">
        <h2>{title}</h2>
        <button type="button" className="sh-insp-close" aria-label="Close" onClick={onClose}>
          <X size={16} aria-hidden />
        </button>
      </div>
      <div className="sh-insp-b">{children}</div>
    </aside>
  );
}
