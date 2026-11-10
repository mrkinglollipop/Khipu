// Today (slice A2): the status sentence and, docked at the right, the five
// checks. Needs you, receipts and All changes arrive with slice A3.
import { useEffect, useState } from "react";
import { Body } from "../shell";
import { HealthLine } from "./HealthLine";
import { HealthPanel } from "./HealthPanel";
import { sentenceFor, useHealth } from "./health";
import "./today.css";

export function Today({
  active, onOpen, onOlderHome,
}: { active: boolean; onOpen: (link: string) => void; onOlderHome: () => void }) {
  const hook = useHealth(active);
  const [open, setOpen] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now()), 30000);
    return () => window.clearInterval(id);
  }, [active]);
  const sentence = sentenceFor(hook.health);
  return (
    <Body>
      <div className="td-page">
        <div className="td-page-in">
          <HealthLine sentence={sentence} hook={hook} now={now} panelOpen={open} onTogglePanel={() => setOpen((v) => !v)} />
          {/* Slice A3: Needs you (question cards, receipts, "That's everything")
              goes in this section. Keep the id; A3 replaces the paragraph. */}
          <section className="td-needs" id="today-needs-you" data-slice="A3" aria-labelledby="td-needs-h">
            <h2 id="td-needs-h">Needs you</h2>
            <p className="td-sub">
              Questions for you are moving to this page. Until then they are on the older Home screen.{" "}
              <button type="button" className="td-link" onClick={onOlderHome}>Open the older Home</button>
            </p>
          </section>
        </div>
      </div>
      {open ? <HealthPanel hook={hook} now={now} onClose={() => setOpen(false)} onOpen={onOpen} /> : null}
    </Body>
  );
}
