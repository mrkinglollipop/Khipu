import { useState } from "react";
import type { HealthState } from "../data";
import { CheckedLine } from "./CheckedLine";

/** `agent_note` from the health command, with one Copy. */
export function AgentNotePage({ health }: { health: HealthState }) {
  const [copied, setCopied] = useState<"yes" | "no" | null>(null);
  const note = health.data?.agent_note ?? "";
  async function copy() {
    try {
      await navigator.clipboard.writeText(note);
      setCopied("yes");
    } catch {
      setCopied("no");
    }
  }
  const none = health.data == null;
  return (
    <>
      <p className="st-muted">Everything an agent needs to look into a problem. Paste it into any conversation.</p>
      {none ? (
        <p className="st-muted" role="status">
          {health.error ? `Khipu could not run its checks just now: ${health.error}` : "Checking… this can take a minute. Copy is ready when the details are."}
        </p>
      ) : (
        <>
          <pre className="st-note" aria-label="Details for an agent">{note}</pre>
          <CheckedLine health={health} />
        </>
      )}
      <div className="st-actions">
        <button type="button" className="primary" disabled={none || !note} onClick={() => void copy()}>Copy</button>
      </div>
      {copied === "yes" ? <p className="st-muted" role="status">Copied.</p> : null}
      {copied === "no" ? <p className="st-muted" role="alert">Khipu could not copy. Select the text above and copy it.</p> : null}
    </>
  );
}
