// The strip above a place: its title on the left, then its views (a
// <ViewSwitch>) or buttons. `heading={false}` shows the title as plain text,
// for a screen that still draws its own heading (one h1 per page). 52 px tall, drags the window like the title bar.
// With only a title it hides under 900 px, where the top bar already shows
// the place.
import type { ReactNode } from "react";
import "./shell.css";

export function Toolbar({ title, heading = true, children }: { title: string; heading?: boolean; children?: ReactNode }) {
  return (
    <div className={children ? "sh-toolbar" : "sh-toolbar bare"} data-tauri-drag-region>
      {heading ? <h1 data-tauri-drag-region>{title}</h1> : <div className="sh-title" data-tauri-drag-region>{title}</div>}
      {children}
    </div>
  );
}

export type ViewSwitchOption<T extends string> = { id: T; label: string };

/** A row of views where one is on (Room right now / Running now / Order and
 *  rules). Buttons with aria-pressed, so it reads as a switch, not as tabs. */
export function ViewSwitch<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: ViewSwitchOption<T>[];
  value: T;
  onChange: (id: T) => void;
}) {
  return (
    <div className="sh-seg" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.id} type="button" aria-pressed={value === o.id} onClick={() => onChange(o.id)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}
