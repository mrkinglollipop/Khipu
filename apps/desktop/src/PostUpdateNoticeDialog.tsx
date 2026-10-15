import { useCallback, useId, useRef } from "react";
import {
  BUILD_CAPABILITIES,
  DIALOG_MAX_ITEMS,
  itemAction,
  type Capability,
  type LaunchPlan,
  type NoticeTarget,
} from "./postUpdateNotices";
import { Dialog } from "./ui";
import "./whatsNew.css";

type Props = {
  plan: LaunchPlan["dialog"];
  /** True when this Mac has no routing record or no account yet. */
  needsRoutingSetup: boolean;
  /** What this build has; defaults to BUILD_CAPABILITIES. */
  caps?: ReadonlySet<Capability>;
  /** A Show me button: go to the place; the dialog closes too. */
  onShow: (target: NoticeTarget) => void;
  onDismiss: () => void;
};

/** The one-time "What's new since X" list, shown on the first launch after an
 * update. Native `<dialog>` + showModal() (see `ui.Dialog`): modality, focus
 * trap and Escape come from the element. Got it, Escape and Show me all close
 * it; none of them touches the Home card. */
export function PostUpdateNoticeDialog({ plan, needsRoutingSetup, caps = BUILD_CAPABILITIES, onShow, onDismiss }: Props) {
  const primaryRef = useRef<HTMLButtonElement>(null);
  const headingId = useId();
  const close = useCallback(() => onDismiss(), [onDismiss]);

  if (!plan) {
    // Keep the <dialog> mounted so open/close uses the same node.
    return <Dialog open={false} className="post-update-dialog wn-dialog" onCancel={close} />;
  }

  const shown = plan.items.slice(0, DIALOG_MAX_ITEMS);
  const more = plan.items.length - shown.length;

  return (
    <Dialog
      open
      className="post-update-dialog wn-dialog"
      ariaLabelledBy={headingId}
      initialFocusRef={primaryRef}
      onCancel={close}
      onClose={onDismiss}
    >
      <div className="post-update-body">
        <header className="post-update-head">
          <h2 id={headingId}>What's new since {plan.since}</h2>
          <p className="muted wn-sub">
            Khipu is now version {plan.version}. This list also stays on Home until you close it there.
          </p>
        </header>
        <ul className="wn-list" role="list">
          {shown.map((item) => {
            const action = itemAction(item, needsRoutingSetup, caps);
            return (
              <li className="wn-item" key={`${item.title}-${item.text}`}>
                <div className="wn-text">
                  <b>{item.title}</b>
                  <span>{item.text}</span>
                </div>
                {action ? (
                  <button
                    type="button"
                    className="sm"
                    aria-label={`${action.label}: ${item.title}`}
                    onClick={() => {
                      onShow(action.target);
                      close();
                    }}
                  >
                    {action.label}
                  </button>
                ) : null}
              </li>
            );
          })}
        </ul>
        {more > 0 ? <p className="set-help wn-more">And {more} more in Settings → Updates.</p> : null}
        <div className="post-update-actions">
          <button type="button" className="primary" ref={primaryRef} onClick={close}>
            Got it
          </button>
        </div>
      </div>
    </Dialog>
  );
}
