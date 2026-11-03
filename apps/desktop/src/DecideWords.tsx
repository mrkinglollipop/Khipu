/* The words of one Needs you decision (routingNeeds.ts), shared by Settings
   and Home's Confirm: what it is, before and now, who asked, and what happens
   if the owner does nothing. Buttons stay with whoever hosts it. */

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
  return (
    <>
      <h4 className="needs-title">{card.title}</h4>
      {card.changed ? (
        <p className="needs-changed">
          <b>{card.changed.cap}:</b> {card.changed.text}
        </p>
      ) : null}
      {card.before || card.now ? (
        <div className="needs-ba">
          {card.before ? (
            <div>
              <span className="cap">{card.before.cap}</span>
              <Lines text={card.before.text} />
            </div>
          ) : null}
          {card.now ? (
            <div>
              <span className="cap">{card.now.cap}</span>
              <Lines text={card.now.text} />
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
