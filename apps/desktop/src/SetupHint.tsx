import { Sparkles, X } from "lucide-react";
import type { HintState, StartItemId } from "./guidesModel";

/** The one first-run hint on Home: an inline band above the cards, never a
 *  modal. It names the next unfinished "Start here" item with one action that
 *  does the step, and a link to the whole list. × hides it for good. */
export function SetupHint({
  state,
  onAct,
  onSeeAll,
  onHide,
  onDismissAllSet,
}: {
  state: HintState;
  onAct: (id: StartItemId) => void;
  onSeeAll: () => void;
  onHide: () => void;
  onDismissAllSet: () => void;
}) {
  if (state.kind === "hidden") return null;

  if (state.kind === "all-set") {
    return (
      <div className="gd-hint" role="region" aria-label="Setup hint">
        <Sparkles size={16} className="gd-hint-lead" aria-hidden />
        <div>
          <b>You're set up</b>
          <p>Every setup step is done. The guides stay in the rail whenever you want them.</p>
        </div>
        <button type="button" className="gd-iconbtn" aria-label="Hide this hint" onClick={onDismissAllSet}>
          <X size={14} aria-hidden />
        </button>
      </div>
    );
  }

  const { item, done, total } = state;
  const action: Record<StartItemId, string> = {
    connect: "Connect an agent",
    session: "See how saving works",
    accounts: "Add an account",
    recall: "See how recall works",
  };
  return (
    <div className="gd-hint" role="region" aria-label="Setup hint">
      <Sparkles size={16} className="gd-hint-lead" aria-hidden />
      <div>
        <b>Next: {item.label.charAt(0).toLowerCase() + item.label.slice(1)}</b>
        <p>
          {item.why} <span className="gd-num">{done} of {total}</span> setup steps done.
        </p>
        <div className="gd-hint-row">
          <button type="button" className="primary sm" onClick={() => onAct(item.id)}>
            {action[item.id]}
          </button>
          <button type="button" className="link" onClick={onSeeAll}>
            See all setup steps
          </button>
        </div>
      </div>
      <button type="button" className="gd-iconbtn" aria-label="Hide this hint" onClick={onHide}>
        <X size={14} aria-hidden />
      </button>
    </div>
  );
}
