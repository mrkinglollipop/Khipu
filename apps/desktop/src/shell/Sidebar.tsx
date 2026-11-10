// The window's navigation, one component in three shapes (CSS decides which):
//   900 px and wider  a left sidebar: brand, the places, then "Older screens",
//                     "On <this Mac>" and Settings at the foot;
//   640 to 899 px     a top bar with the same buttons in a row;
//   under 640 px      a slim top bar, with the places moved to <TabBar>.
// It knows nothing about any place's content: the app hands it items.
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ChevronDown, Ellipsis, Laptop, Loader2 } from "lucide-react";
import "./shell.css";

export type NavItem = {
  id: string;
  label: string;
  icon?: ReactNode;
  /** A count beside the label (Today's questions). Hidden when 0 or null. */
  count?: number | null;
  /** What a screen reader says for the count, e.g. "2 need you". */
  countLabel?: string;
  /** A quieter count (grey, not rust). */
  quietCount?: boolean;
  /** Spinner after the label while this place is reading. */
  busy?: boolean;
  /** A ready-made badge, used instead of `count` (the old screens keep theirs). */
  badge?: ReactNode;
  /** Hover text. */
  hint?: string;
};

function CountBadge({ item }: { item: NavItem }) {
  if (item.badge) return <>{item.badge}</>;
  if (!item.count) return null;
  return (
    <span className={item.quietCount ? "sh-count quiet" : "sh-count"} aria-label={item.countLabel}>
      {item.count.toLocaleString()}
    </span>
  );
}

function NavButton({ item, current, onGo, className }: { item: NavItem; current: string; onGo: (id: string) => void; className?: string }) {
  return (
    <button
      type="button"
      className={className}
      title={item.hint}
      aria-current={current === item.id ? "page" : undefined}
      onClick={() => onGo(item.id)}
    >
      {item.icon}
      <span className="sh-label">{item.label}</span>
      <CountBadge item={item} />
      {item.busy ? <Loader2 size={12} className="spin sh-spin" aria-hidden /> : null}
    </button>
  );
}

export type SidebarProps = {
  /** The logo mark; the sidebar adds the word "Khipu". */
  brand: ReactNode;
  /** The places, in order (Today, Accounts, Memory). */
  items: NavItem[];
  /** The id of the screen on show (a place, an older screen, or settings). */
  current: string;
  onGo: (id: string) => void;
  /** The foot button (Settings). */
  settings: NavItem;
  /** This Mac's name ("M5"). Null or missing reads "On this Mac". */
  macName?: string | null;
  /** Screens that are being replaced. Omit once none are left. */
  older?: {
    items: NavItem[];
    /** Anything else the old rail carried (health line, Feedback, version),
     *  shown inside the Older screens group until its new home lands. */
    extra?: ReactNode;
  };
};

/** True while the window is 900 px or wider. jsdom has no matchMedia: wide. */
function useWide(): boolean {
  const query = "(min-width: 900px)";
  const [wide, setWide] = useState(() => (typeof window.matchMedia === "function" ? window.matchMedia(query).matches : true));
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia(query);
    const on = () => setWide(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return wide;
}

export function Sidebar({ brand, items, current, onGo, settings, macName, older }: SidebarProps) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const olderCurrent = !!older?.items.some((i) => i.id === current);
  const wide = useWide();
  // Wide: the group stays open while one of its screens is on show. Narrow:
  // it is a popover, open only after a click.
  const shown = open || (wide && olderCurrent);

  // Narrow widths show the list as a popover: a click outside, or Escape, closes it.
  useEffect(() => {
    if (!open) return;
    const away = (e: PointerEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  const go = (id: string) => {
    setOpen(false);
    onGo(id);
  };

  return (
    <nav className="sh-side" aria-label="Main" data-tauri-drag-region>
      <div className="sh-lights" data-tauri-drag-region />
      <div className="sh-brand" data-tauri-drag-region>
        {brand}
        <span data-tauri-drag-region>Khipu</span>
      </div>
      <div className="sh-snav sh-places">
        {items.map((i) => (
          <NavButton key={i.id} item={i} current={current} onGo={go} />
        ))}
      </div>
      <div className="sh-side-foot">
        {older ? (
          <div
            className="sh-older"
            ref={wrap}
            data-open={shown ? "true" : "false"}
          >
            <button
              type="button"
              className="sh-older-btn"
              aria-label="Older screens"
              aria-expanded={shown}
              aria-controls="sh-older-list"
              title="Screens that are being replaced"
              onClick={() => setOpen(!shown)}
            >
              <Ellipsis className="sh-older-dots" size={16} aria-hidden />
              <span className="sh-label">Older screens</span>
              <ChevronDown className="sh-older-chev" size={14} aria-hidden />
            </button>
            <div className="sh-older-list" id="sh-older-list">
              <div className="sh-snav">
                {older.items.map((i) => (
                  <NavButton key={i.id} item={i} current={current} onGo={go} />
                ))}
              </div>
              {older.extra ? <div className="sh-older-extra">{older.extra}</div> : null}
            </div>
          </div>
        ) : null}
        <div className="sh-onmac" title="Which Mac this window is on">
          <Laptop size={16} aria-hidden />
          <span>{macName ? `On ${macName}` : "On this Mac"}</span>
        </div>
        <div className="sh-snav sh-settings">
          <NavButton item={settings} current={current} onGo={go} />
        </div>
      </div>
    </nav>
  );
}

/** The bottom tab bar, shown under 640 px (hidden by CSS above that). Same
 *  items as the sidebar's places; the count sits on the icon. Put it as the
 *  last child of the window grid, after <main>. */
export function TabBar({ items, current, onGo }: { items: NavItem[]; current: string; onGo: (id: string) => void }) {
  return (
    <nav className="sh-tabs" aria-label="Places">
      {items.map((i) => (
        <NavButton key={i.id} item={i} current={current} onGo={onGo} />
      ))}
    </nav>
  );
}
