import { useState } from "react";
import type { HealthDoc, Loaded } from "../data";

/** `agent_note` from the health command, with one Copy. */
export function AgentNotePage({ health }: { health: Loaded<HealthDoc> }) {
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
  if (health.loading && !health.data) return <p className="st-muted" role="status">Khipu is running its checks. This can take a minute.</p>;
  if (!health.data) return <p className="st-muted" role="status">Khipu could not run its checks just now{health.error ? `: ${health.error}` : "."}</p>;
  return (
    <>
      <p className="st-muted">Everything an agent needs to look into a problem. Paste it into any conversation.</p>
      <pre className="st-note" aria-label="Details for an agent">{note}</pre>
      <div className="st-actions">
        <button type="button" className="primary" onClick={() => void copy()}>Copy</button>
      </div>
      {copied === "yes" ? <p className="st-muted" role="status">Copied.</p> : null}
      {copied === "no" ? <p className="st-muted" role="alert">Khipu could not copy. Select the text above and copy it.</p> : null}
    </>
  );
}
