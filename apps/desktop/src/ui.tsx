/* ---------------------------------------------------------------------------
   Khipu UI kit — the one component set (desktop overhaul phase 1).

   Every visual value here comes from `mocks/tokens.css` in the approved
   overhaul mocks; the styles live in App.css under "UI kit". This file only
   owns the markup contract, so a screen never hand-rolls a pill, a card row
   or a modal again.

   Replaces, per the audit's component inventory:
     .pill / .chip / .kind-badge / .harness-installed-pill  ->  <Tag>
     Callout (App.tsx) + Note (Welcome.tsx)                 ->  <Callout>
     two hand-rolled <dialog> wrappers                      ->  <Dialog>
     bare <details className="raw"> on every screen         ->  <Disclosure>
--------------------------------------------------------------------------- */

import { useEffect, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";
import { ChevronRight } from "lucide-react";

export type Tone = "ok" | "warn" | "err" | "accent" | "neutral";

/** Status tag (dot variant) or kind tag (`kind`, uppercase micro-label).
 *  One component for what used to be four. */
export function Tag({
  tone = "neutral",
  dot = false,
  kind = false,
  className,
  title,
  children,
}: {
  tone?: Tone;
  dot?: boolean;
  kind?: boolean;
  className?: string;
  title?: string;
  children?: ReactNode;
}) {
  const cls = [
    "tag",
    kind ? "kind" : null,
    tone === "neutral" ? null : tone,
    className ?? null,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <span className={cls} title={title}>
      {dot ? <span className="dot" aria-hidden /> : null}
      {children}
    </span>
  );
}

/** Inline notice. `stripe` adds the severity bar the mocks use for an
 *  attention item on Home; `tone="neutral"` is a true absence (an empty
 *  result), not a pass. */
export function Callout({
  tone = "neutral",
  stripe = false,
  title,
  action,
  children,
}: {
  tone?: "ok" | "warn" | "err" | "neutral";
  stripe?: boolean;
  title: string;
  action?: ReactNode;
  children?: ReactNode;
}) {
  const cls = [
    "callout",
    tone === "neutral" ? null : tone,
    stripe ? `stripe ${tone === "neutral" ? "warn" : tone}` : null,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div className={cls}>
      <div className="txt">
        <b>{title}</b>
        {children ? <span>{children}</span> : null}
      </div>
      {action ? <div className="cta">{action}</div> : null}
    </div>
  );
}

/** The one modal wrapper. Modality, focus trap, background inertness and the
 *  Escape -> `cancel` mapping come from the native element; the backdrop comes
 *  from one `--backdrop` token instead of two independent rgba literals. */
export function Dialog({
  open,
  className,
  ariaLabelledBy,
  onCancel,
  onClose,
  initialFocusRef,
  children,
}: {
  open: boolean;
  className?: string;
  ariaLabelledBy?: string;
  /** Escape or a backdrop dismissal. */
  onCancel: () => void;
  /** The native `close` event, however it was closed. */
  onClose?: () => void;
  initialFocusRef?: RefObject<HTMLElement | null>;
  children?: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dlg = ref.current;
    if (!dlg) return;
    if (open && !dlg.open) {
      dlg.showModal();
      requestAnimationFrame(() => initialFocusRef?.current?.focus());
    } else if (!open && dlg.open) {
      dlg.close();
    }
  }, [open, initialFocusRef]);

  useEffect(() => {
    const dlg = ref.current;
    if (!dlg) return;
    const handler = (e: Event) => {
      e.preventDefault();
      onCancel();
    };
    dlg.addEventListener("cancel", handler);
    return () => dlg.removeEventListener("cancel", handler);
  }, [onCancel]);

  return (
    <dialog
      ref={ref}
      className={className}
      role={open ? "dialog" : undefined}
      aria-labelledby={ariaLabelledBy}
      onClose={onClose}
    >
      {children}
    </dialog>
  );
}

/** A row inside a `.card`. Content shape is the caller's; the row only owns
 *  the rhythm (height, gap, divider). */
export function ListRow({
  className,
  children,
}: {
  className?: string;
  children?: ReactNode;
}) {
  return <div className={className ? `row ${className}` : "row"}>{children}</div>;
}

/** One number with a label and a supporting line. `tone="err"` outlines it. */
export function Tile({
  label,
  value,
  sub,
  tone = "neutral",
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "neutral" | "err" | "warn";
}) {
  return (
    <div className={tone === "neutral" ? "tile" : `tile ${tone}`}>
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub ? <div className="sub">{sub}</div> : null}
    </div>
  );
}

export type SegmentedOption<T extends string> = {
  value: T;
  label: string;
  hint?: string;
};

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  ariaLabel,
}: {
  options: ReadonlyArray<SegmentedOption<T>>;
  value: T;
  onChange: (next: T) => void;
  ariaLabel: string;
}) {
  return (
    <div className="seg" role="radiogroup" aria-label={ariaLabel}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          className={value === o.value ? "on" : undefined}
          title={o.hint}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** Filter chip. `on` is the applied state; `onRemove` adds the clear affordance
 *  the mocks show as a trailing ×. */
export function Chip({
  on = false,
  tiny = false,
  title,
  onClick,
  onRemove,
  children,
}: {
  on?: boolean;
  tiny?: boolean;
  title?: string;
  onClick?: () => void;
  onRemove?: () => void;
  children?: ReactNode;
}) {
  const cls = ["chip", on ? "on" : null, tiny ? "tiny" : null]
    .filter(Boolean)
    .join(" ");
  const body = (
    <>
      {children}
      {onRemove ? (
        <span
          className="x"
          aria-hidden
          onClick={(e) => {
            e.stopPropagation();
            onRemove();
          }}
        >
          ×
        </span>
      ) : null}
    </>
  );
  if (!onClick && !onRemove) {
    return (
      <span className={cls} title={title}>
        {body}
      </span>
    );
  }
  return (
    <button
      type="button"
      className={cls}
      title={title}
      aria-pressed={on}
      onClick={onClick}
    >
      {body}
    </button>
  );
}

/** Nothing here — and what would put something here. */
export function EmptyState({
  icon,
  title,
  hint,
}: {
  icon?: ReactNode;
  title: string;
  hint?: ReactNode;
}) {
  return (
    <div className="empty">
      {icon}
      <b>{title}</b>
      {hint ? <span>{hint}</span> : null}
    </div>
  );
}

/** Collapsed-by-default detail. Every raw-JSON dump on every screen lives
 *  inside one of these, labelled "Advanced" (audit: a debug affordance was
 *  permanently expanded on primary surfaces).
 *
 *  `openKey` bumps open it once — the Activity/Revisions "show this row" flow
 *  needs the panel it just filled to be visible. */
export function Disclosure({
  label,
  openKey = 0,
  defaultOpen = false,
  className,
  children,
}: {
  label: ReactNode;
  openKey?: number;
  defaultOpen?: boolean;
  /** Extra class on the `details` — `group` is the Owed section header, which
   *  is a heading rather than a quiet "Advanced" line. */
  className?: string;
  children?: ReactNode;
}) {
  const ref = useRef<HTMLDetailsElement>(null);
  const [open, setOpen] = useState(defaultOpen);
  useEffect(() => {
    if (openKey > 0) {
      setOpen(true);
      requestAnimationFrame(() => {
        ref.current?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }
  }, [openKey]);
  return (
    <details
      ref={ref}
      className={className ? `disclose ${className}` : "disclose"}
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary className="disclosure">
        <ChevronRight size={14} className="disclosure-chevron" aria-hidden />
        <span>{label}</span>
      </summary>
      <div className="disclose-body">{children}</div>
    </details>
  );
}
