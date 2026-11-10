// Today: the status sentence (slice A2) and Needs you (slice A3). The five
// checks and All changes open docked at the right, one at a time.
import { useEffect, useState } from "react";
import { Body } from "../shell";
import { HealthLine } from "./HealthLine";
import { HealthPanel } from "./HealthPanel";
import { ChangesPanel } from "./ChangesPanel";
import { NeedsCards } from "./NeedsCards";
import { sentenceFor, useHealth } from "./health";
import type { NeedsHook } from "./needs";
import "./today.css";

export function Today({
  active, needs, onOpen,
}: { active: boolean; needs: NeedsHook; onOpen: (link: string) => void }) {
  const hook = useHealth(active);
  const [panel, setPanel] = useState<"health" | "changes" | null>(null);
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
          <HealthLine
            sentence={sentence} hook={hook} now={now} panelOpen={panel === "health"}
            onTogglePanel={() => setPanel((p) => (p === "health" ? null : "health"))}
          />
          <NeedsCards needs={needs} onAllChanges={() => setPanel("changes")} />
        </div>
      </div>
      {panel === "health" ? <HealthPanel hook={hook} now={now} onClose={() => setPanel(null)} onOpen={onOpen} /> : null}
      {panel === "changes" ? <ChangesPanel onClose={() => setPanel(null)} onChanged={() => void needs.reload()} /> : null}
    </Body>
  );
}
