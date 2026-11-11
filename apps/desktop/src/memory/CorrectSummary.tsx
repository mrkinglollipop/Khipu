/* "Correct what it was about" (slice A11): the older Activity screen's edit of
   a conversation's summary, moved to the conversation page with the same call
   (`khipu episode edit ID --summary=TEXT`) and the same answers: a refusal is
   shown and nothing changes; a summary saved without new search vectors says
   search by meaning picks it up at the next nightly. */
import { useId, useState } from "react";

type Run = (args: string[]) => Promise<string>;

export function CorrectSummary({ id, current, run, onSaved }: {
  id: number;
  current: string;
  run: Run;
  onSaved: () => void;
}) {
  const fieldId = useId();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState(current);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: "err" | "note"; text: string } | null>(null);

  const save = async () => {
    if (busy || !text.trim()) return;
    setBusy(true);
    setMsg(null);
    try {
      const raw = await run(["episode", "edit", String(id), `--summary=${text}`]);
      let parsed: { ok?: boolean; reembedded?: boolean; error?: string } | null = null;
      try { parsed = JSON.parse(raw); } catch { parsed = null; }
      if (parsed && parsed.ok === false) {
        setMsg({ tone: "err", text: parsed.error ?? "The summary was not changed." });
        return;
      }
      setOpen(false);
      setMsg(parsed && parsed.reembedded === false
        ? { tone: "note", text: "Saved. Search by meaning picks it up at the next nightly." }
        : { tone: "note", text: "Saved." });
      onSaved();
    } catch (e) {
      setMsg({ tone: "err", text: String(e) });
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <>
        <button type="button" onClick={() => { setText(current); setMsg(null); setOpen(true); }}>
          Correct what it was about…
        </button>
        {msg ? <p className={msg.tone === "err" ? "mem-err" : "mem-note"} role={msg.tone === "err" ? "alert" : "status"}>{msg.text}</p> : null}
      </>
    );
  }
  return (
    <form className="mem-correct" onSubmit={(e) => { e.preventDefault(); void save(); }}>
      <label htmlFor={fieldId}>What this conversation was about</label>
      <textarea id={fieldId} rows={5} value={text} onChange={(e) => setText(e.target.value)} disabled={busy} />
      <div className="mem-actions">
        <button type="submit" className="primary" disabled={busy || !text.trim() || text === current}>
          {busy ? "Saving…" : "Save"}
        </button>
        <button type="button" onClick={() => setOpen(false)} disabled={busy}>Cancel</button>
      </div>
      {msg ? <p className="mem-err" role="alert">{msg.text}</p> : null}
    </form>
  );
}
