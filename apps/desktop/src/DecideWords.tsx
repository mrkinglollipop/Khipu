/* The words of one Needs you decision (routingNeeds.ts), shared by Settings
   and Home's Confirm: what it is, before and now, who asked, and what happens
   if the owner does nothing. Buttons stay with whoever hosts it. */

import { useState } from "react";
import type { DecideCard } from "./routingNeeds";

/** One line to a line; in a list, wrapping under its own text rather than its number. */
function Lines({ text }: { text: string }) {
  const lines = text.split("\n");
  return (
    <>
      {lines.map((line, i) => (
        <span key={i} className={lines.length > 1 ? "needs-line hang" : "needs-line"}>{line}</span>
      ))}
    </>
  );
}

export function DecideWords({ card }: { card: DecideCard }) {
  const [showAll, setShowAll] = useState(false);
  const priority = card.target.type === "priority";
  const before = priority && !showAll ? card.changedBefore ?? card.before : card.before;
  const after = priority && !showAll ? card.changedNow ?? card.now : card.now;
  return (
    <>
      <h4 className="needs-title">{card.title}</h4>
      {card.changed ? (
        <p className="needs-changed">
          <b>{card.changed.cap}:</b> {card.changed.text}
        </p>
      ) : null}
      {(before || after) ? (
        <div className="needs-ba">
          {before ? (
            <div>
              <span className="cap">{before.cap}</span>
              <Lines text={before.text} />
            </div>
          ) : null}
          {after ? (
            <div>
              <span className="cap">{after.cap}</span>
              <Lines text={after.text} />
            </div>
          ) : null}
        </div>
      ) : null}
      {priority && !showAll && (card.before || card.now) ? (
        <button type="button" className="sm" onClick={() => setShowAll(true)}>
          Show all {card.detailCount ?? "priority entries"}
        </button>
      ) : null}
      <dl className="needs-facts">
        <dt>Who</dt>
        <dd>{card.who}</dd>
        {card.asked ? (
          <>
            <dt>It says you asked</dt>
            <dd>“{card.asked}”</dd>
          </>
        ) : null}
        <dt>If you do nothing</dt>
        <dd>{card.ifIgnored}</dd>
      </dl>
    </>
  );
}
