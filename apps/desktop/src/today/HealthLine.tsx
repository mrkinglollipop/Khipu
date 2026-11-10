// The one status sentence at the top of Today.
import { agoWords, type HealthHook, type Sentence } from "./health";

export function HealthLine({
  sentence, hook, now, panelOpen, onTogglePanel,
}: { sentence: Sentence; hook: HealthHook; now: number; panelOpen: boolean; onTogglePanel: () => void }) {
  const mac = hook.health?.mac;
  const ago = agoWords(hook.answeredAt, now);
  const checked = ago ? `Checked ${ago}${mac ? ` on ${mac}` : ""}.` : "";
  const link = sentence.attention > 0 ? "See what’s happening" : "See the five checks";
  return (
    <header className="td-line" data-tone={sentence.tone}>
      <span className="td-dot" aria-hidden />
      <div>
        <h1 className="td-sentence">{sentence.text}</h1>
        {sentence.sub ? <p className="td-sub">{sentence.sub}</p> : null}
        <p className="td-sub td-checked" role="status" aria-live="polite">
          {checked}
          {hook.checking ? (ago ? " Checking again…" : "") : hook.failed && ago ? " Couldn’t refresh just now; Khipu will try again." : ""}
        </p>
        <button type="button" className="td-link" aria-expanded={panelOpen} onClick={onTogglePanel}>
          {panelOpen ? "Hide the five checks" : link}
        </button>
      </div>
    </header>
  );
}
