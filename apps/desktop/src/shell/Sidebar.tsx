// The window's navigation, one component in three shapes (CSS decides which):
//   900 px and wider  a left sidebar: brand, the places, then
//                     "On <this Mac>" and Settings at the foot;
//   640 to 899 px     a top bar with the same buttons in a row;
//   under 640 px      a slim top bar, with the places moved to <TabBar>.
// It knows nothing about any place's content: the app hands it items.
import type { ReactNode } from "react";
import { Laptop, Loader2 } from "lucide-react";
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
  /** The id of the screen on show (a place or settings). */
  current: string;
  onGo: (id: string) => void;
  /** The foot button (Settings). */
  settings: NavItem;
  /** This Mac's name ("M5"). Null or missing reads "On this Mac". */
  macName?: string | null;
};

export function Sidebar({ brand, items, current, onGo, settings, macName }: SidebarProps) {
  const go = onGo;
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
