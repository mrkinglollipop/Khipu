import { useCallback, useEffect, useId, useRef } from "react";
import type { PostUpdateNotice } from "./postUpdateNotices";

type Props = {
  notice: PostUpdateNotice | null;
  onDismiss: () => void;
  onOpenIntegrations: () => void;
};

/** One-time notice shown after an in-app update, when `postUpdateNotices.ts`
 * has an entry for a version the app just crossed. Native `<dialog>` +
 * showModal(), same pattern as FeedbackForm: role, modality, focus trap and
 * background inertness come from the element itself, Escape maps to the
 * `cancel` event, and focus moves to the primary action on open. */
export function PostUpdateNoticeDialog({ notice, onDismiss, onOpenIntegrations }: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const primaryRef = useRef<HTMLButtonElement>(null);
  const headingId = useId();

  const close = useCallback(() => {
    const dlg = dialogRef.current;
    if (dlg?.open) dlg.close();
    onDismiss();
  }, [onDismiss]);

  useEffect(() => {
    const dlg = dialogRef.current;
    if (!dlg) return;
    if (notice && !dlg.open) {
      dlg.showModal();
      requestAnimationFrame(() => primaryRef.current?.focus());
    } else if (!notice && dlg.open) {
      dlg.close();
    }
  }, [notice]);

  useEffect(() => {
    const dlg = dialogRef.current;
    if (!dlg) return;
    const onCancel = (e: Event) => {
      e.preventDefault();
      close();
    };
    dlg.addEventListener("cancel", onCancel);
    return () => dlg.removeEventListener("cancel", onCancel);
  }, [close]);

  if (!notice) {
    // Keep the <dialog> mounted (open/close transitions need the same node);
    // render nothing inside it when there is no notice to show.
    return <dialog ref={dialogRef} className="post-update-dialog" />;
  }

  const hasAction = notice.action === "integrations";

  return (
    <dialog
      ref={dialogRef}
      className="post-update-dialog"
      role="dialog"
      aria-labelledby={headingId}
      onClose={onDismiss}
    >
      <div className="post-update-body">
        <header className="post-update-head">
          <h2 id={headingId}>{notice.title}</h2>
        </header>
        <p className="muted post-update-text">{notice.body}</p>
        <div className="post-update-actions">
          <button type="button" ref={hasAction ? undefined : primaryRef} onClick={close}>
            Got it
          </button>
          {hasAction ? (
            <button
              ref={primaryRef}
              type="button"
              className="primary"
              onClick={() => {
                onOpenIntegrations();
                close();
              }}
            >
              Open Integrations
            </button>
          ) : null}
        </div>
      </div>
    </dialog>
  );
}
