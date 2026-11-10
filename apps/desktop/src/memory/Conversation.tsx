// The open conversation: what it was about, what you said, what was decided,
// and the two things you can do with it. "Forget" opens the confirmation (the
// owner types "forget"); it never forgets by itself.
import { useRef, useState } from "react";
import type { ReactNode } from "react";
import { ArrowLeft, Check, Loader2 } from "lucide-react";
import { Marked } from "./Results";
import type { Searched } from "./Results";
import { metaLabel } from "./model";
import type { Conversation, MemoryRow } from "./model";

const SAID_SHOWN = 8;

/** What an agent can paste to open this conversation (it reads it with
 *  `khipu_get`, which takes the id). */
export function linkFor(row: MemoryRow): string {
  return `Khipu conversation ${row.id}: “${row.title}”. Open it with khipu_get, id ${row.id}.`;
}

export function ConversationPage({
  row,
  page,
  searched,
  onBack,
  onForget,
  correct,
}: {
  row: MemoryRow;
  page: { state: "busy" | "done" | "error"; data: Conversation | null };
  searched: Searched | null;
  onBack: () => void;
  onForget: () => void;
  /** "Correct what it was about…" (moved from the older Activity screen). */
  correct?: ReactNode;
}) {
  const [copied, setCopied] = useState<"yes" | "no" | null>(null);
  const [all, setAll] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const data = page.data;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(linkFor(row));
      setCopied("yes");
    } catch {
      setCopied("no");
    }
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(null), 4000);
  };

  const said = data ? (all ? data.said : data.said.slice(0, SAID_SHOWN)) : [];

  return (
    <article className="mem-conv" key={row.id}>
      <button type="button" className="mem-back" onClick={onBack}>
        <ArrowLeft size={16} aria-hidden /> Back to the list
      </button>
      <header>
        <h2>{row.title}</h2>
        <p className="mem-when">{metaLabel(row) || "Conversation"}</p>
      </header>

      {page.state === "busy" ? (
        <p className="mem-note" role="status">
          <Loader2 size={14} className="spin" aria-hidden /> Opening…
        </p>
      ) : null}
      {page.state === "error" ? <p className="mem-note" role="alert">Couldn’t open this conversation. Pick it again to retry.</p> : null}

      {data?.about ? (
        <section>
          <h3>What it was about</h3>
          <p>{data.about}</p>
        </section>
      ) : null}
      {said.length > 0 ? (
        <section>
          <h3>What you said</h3>
          {said.map((line, i) => (
            <p key={i}>
              “<Marked value={line} searched={searched} />”
            </p>
          ))}
          {data && data.said.length > SAID_SHOWN && !all ? (
            <button type="button" className="mem-link" onClick={() => setAll(true)}>
              Show all {data.said.length}
            </button>
          ) : null}
        </section>
      ) : null}
      {data && data.decided.length > 0 ? (
        <section>
          <h3>What was decided</h3>
          {data.decided.map((d, i) => (
            <p key={i}>{d}</p>
          ))}
        </section>
      ) : null}

      <section>
        <h3>Other things you can do</h3>
        <div className="mem-actions">
          <button type="button" onClick={() => void copy()}>
            Copy a link for an agent
          </button>
          <button type="button" onClick={onForget}>
            Forget this conversation
          </button>
          {correct}
        </div>
        <p className="mem-copied" role="status">
          {copied === "yes" ? (
            <>
              <Check size={14} aria-hidden /> Copied. Paste it into any conversation and the agent can open it.
            </>
          ) : copied === "no" ? (
            "Couldn’t copy. Select the title and copy it by hand."
          ) : null}
        </p>
      </section>
    </article>
  );
}
