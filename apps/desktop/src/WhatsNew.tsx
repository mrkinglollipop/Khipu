import { useId, useState } from "react";
import { ChevronRight } from "lucide-react";
import {
  CARD_MAX_ITEMS,
  itemAction,
  noticesBetween,
  type CardState,
  type NoticeItem,
  type NoticeTarget,
} from "./postUpdateNotices";
import "./whatsNew.css";

/** Open and scroll to the Settings group named `group`, if it exists. The
 * element carries `data-settings-group="<group>"`; a folded group is a button
 * with `aria-expanded="false"` and is opened first. Tries for a few frames
 * because the section renders after the tab switches. Does nothing when no
 * such element appears (the groups are added by the routing overview). */
export function revealSettingsGroup(group: string, frames = 12): void {
  let left = frames;
  const selector = `[data-settings-group="${group.replace(/["\\]/g, "\\$&")}"]`;
  const tick = () => {
    const el = document.querySelector<HTMLElement>(selector);
    if (el) {
      if (el instanceof HTMLButtonElement && el.getAttribute("aria-expanded") === "false") el.click();
      el.scrollIntoView?.({ block: "start" });
      el.focus?.();
      return;
    }
    left -= 1;
    if (left > 0) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

type ShowProps = {
  needsRoutingSetup: boolean;
  onShow: (target: NoticeTarget) => void;
};

function ItemRow({ item, needsRoutingSetup, onShow }: ShowProps & { item: NoticeItem }) {
  const action = itemAction(item, needsRoutingSetup);
  return (
    <li className="wn-row">
      <span className="wn-t">
        <b>{item.title}</b> <span className="t2">{item.short ?? item.text}</span>
      </span>
      <button
        type="button"
        className="sm"
        aria-label={`${action.label}: ${item.title}`}
        onClick={() => onShow(action.target)}
      >
        {action.label}
      </button>
    </li>
  );
}

/** The Home card: the first three items since the last version shown. It
 * stays until Close is pressed here; the dialog is a separate thing. */
export function WhatsNewCard({
  card,
  needsRoutingSetup,
  onShow,
  onClose,
  onSeeAll,
}: ShowProps & { card: CardState; onClose: () => void; onSeeAll: () => void }) {
  const headingId = useId();
  const items = noticesBetween(card.since, card.version).flatMap((n) => n.items);
  if (items.length === 0) return null;
  const shown = items.slice(0, CARD_MAX_ITEMS);
  const more = items.length - shown.length;
  return (
    <div className="card wn-card" role="region" aria-labelledby={headingId}>
      <div className="card-head">
        <h2 id={headingId} className="wn-card-h">
          New in {card.version}
        </h2>
        <span className="spacer" />
        <button type="button" className="sm" aria-label="Close the What's new card" onClick={onClose}>
          Close
        </button>
      </div>
      <ul className="wn-rows" role="list">
        {shown.map((item) => (
          <ItemRow key={`${item.title}-${item.text}`} item={item} needsRoutingSetup={needsRoutingSetup} onShow={onShow} />
        ))}
      </ul>
      {more > 0 ? (
        <div className="row wn-card-foot">
          <span className="t2">
            {more} more since {card.since}
          </span>
          <span className="spacer" />
          <button type="button" className="link sm" onClick={onSeeAll}>
            See all in Settings → Updates
          </button>
        </div>
      ) : null}
    </div>
  );
}

const OPEN_VERSIONS = 3;

/** Settings -> Updates: every notice up to the installed version, newest
 * first. The newest three versions are open; older ones sit in a fold. */
export function WhatsNewHistory({ appVersion, needsRoutingSetup, onShow }: ShowProps & { appVersion: string }) {
  const [open, setOpen] = useState(false);
  const foldId = useId();
  const versions = /^\d/.test(appVersion) ? noticesBetween("", appVersion) : [];
  if (versions.length === 0) return null;
  const head = versions.slice(0, OPEN_VERSIONS);
  const rest = versions.slice(OPEN_VERSIONS);
  const block = (n: (typeof versions)[number]) => (
    <div key={n.version}>
      <h2 className="wn-ver">
        {n.version}
        {n.version === appVersion ? (
          <>
            {" "}
            <span className="meta">installed</span>
          </>
        ) : null}
      </h2>
      <ul className="wn-rows" role="list">
        {n.items.map((item) => (
          <ItemRow key={`${item.title}-${item.text}`} item={item} needsRoutingSetup={needsRoutingSetup} onShow={onShow} />
        ))}
      </ul>
    </div>
  );
  return (
    <div className="section-card wn-history">
      <div className="section-head">
        What's new
        <span className="spacer" />
        <span className="meta">Every notice, newest first</span>
      </div>
      <div className="section-body">
        {head.map(block)}
        {rest.length > 0 ? (
          <div className="disclose fold" data-open={open ? "true" : "false"}>
            <button
              type="button"
              className="disclosure fold-btn"
              aria-expanded={open}
              aria-controls={foldId}
              onClick={() => setOpen((o) => !o)}
            >
              <ChevronRight className="disclosure-chevron" size={14} strokeWidth={2} aria-hidden />
              <span>
                Earlier versions<span className="tag">{rest.length} more</span>
              </span>
            </button>
            <div className="fold-body" id={foldId} hidden={!open}>
              <div className="disclose-body">
                <p className="set-help">{rest[0].version} and older, newest first.</p>
                {rest.map(block)}
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
