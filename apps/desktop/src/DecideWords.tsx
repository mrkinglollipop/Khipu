/* The words of one Needs you decision (routingNeeds.ts), shared by Settings
   and Home's Confirm: what it is, before and now, who asked, and what happens
   if the owner does nothing. Buttons stay with whoever hosts it. */

import type { DecideCard } from "./routingNeeds";

export function DecideWords({ card }: { card: DecideCard }) {
  return (
    <>
      <h4 className="needs-title">{card.title}</h4>
      {card.before || card.now ? (
        <div className="needs-ba">
          {card.before ? (
            <div>
              <span className="cap">{card.before.cap}</span>
              {card.before.text}
            </div>
          ) : null}
          {card.now ? (
            <div>
              <span className="cap">{card.now.cap}</span>
              {card.now.text}
            </div>
          ) : null}
        </div>
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
