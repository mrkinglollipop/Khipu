// A list on the left and the chosen item's page beside it, filling the
// window. Under 900 px it becomes one column. Wrap it (with an optional
// <Inspector>) in <Body>.
import type { ReactNode } from "react";
import "./shell.css";

/** The area under a Toolbar: a row that holds a <Split> or a page, plus a
 *  docked <Inspector> at the right when one is open. */
export function Body({ children }: { children: ReactNode }) {
  return <div className="sh-body">{children}</div>;
}

export type SplitProps = {
  list: ReactNode;
  /** The list's name for a screen reader ("Accounts"). */
  listLabel: string;
  detail: ReactNode;
  /** The detail's name for a screen reader (the open item's title). */
  detailLabel: string;
  /** Under 900 px only. true: show the detail alone (the place gives a Back
   *  button); false: show the list alone; left out: stack both. */
  detailOpen?: boolean;
  /** Let the detail run to 1080 px instead of 720 (Accounts at 1728). */
  wideDetail?: boolean;
};

export function Split({ list, listLabel, detail, detailLabel, detailOpen, wideDetail }: SplitProps) {
  return (
    <div className="sh-split" data-detail={detailOpen === undefined ? undefined : detailOpen ? "open" : "closed"}>
      <div className="sh-listpane" role="region" aria-label={listLabel}>
        {list}
      </div>
      <section className="sh-detail" aria-label={detailLabel}>
        <div className={wideDetail ? "sh-detail-in wide" : "sh-detail-in"}>{detail}</div>
      </section>
    </div>
  );
}
