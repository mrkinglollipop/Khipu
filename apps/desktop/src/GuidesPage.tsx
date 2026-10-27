import { useState } from "react";
import { Check, ChevronRight, ExternalLink } from "lucide-react";
import { GrokBotGuide } from "./GrokBotGuide";
import { OUTLINES } from "./guideOutlines";
import type { GuideTarget } from "./guideOutlines";
import { GROUP_TITLES, filterGuides, guideMeta, startProgress } from "./guidesModel";
import type { GuideGroup, GuideId, GuideState, StartItem, StartItemId } from "./guidesModel";
import type { useGuideSignals } from "./guideSignals";
import { Tag } from "./ui";

type Signals = ReturnType<typeof useGuideSignals>;

const GROUP_ORDER: GuideGroup[] = ["agents", "accounts", "memory", "safe"];

/** Where each Start here item takes the user. */
const START_TARGET: Record<StartItemId, GuideId> = {
  connect: "grok_bot",
  session: "saving",
  accounts: "add_account",
  recall: "saving",
};

function StartHere({
  items,
  setUpSeen,
  onHide,
  onOpen,
  onDismissAllSet,
}: {
  items: StartItem[];
  setUpSeen: boolean;
  onHide: () => void;
  onOpen: (id: GuideId) => void;
  onDismissAllSet: () => void;
}) {
  const { done, total } = startProgress(items);
  if (done === total && setUpSeen) return null;
  return (
    <section className="gd-card gd-start" aria-labelledby="gd-start-h">
      <h4 id="gd-start-h">{done === total ? "You're set up" : "Start here"}</h4>
      <button type="button" className="link meta" onClick={done === total ? onDismissAllSet : onHide}>
        {done === total ? "Close" : "Hide"}
      </button>
      <div className="gd-count gd-num">{done} of {total} done</div>
      <div className="gd-bar" role="progressbar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={done} aria-label="Setup progress">
        <i style={{ width: `${(done / total) * 100}%` }} />
      </div>
      <ul className="gd-checklist">
        {items.map((it) => (
          <li key={it.id}>
            <span className={it.done ? "gd-tick on" : "gd-tick"} aria-hidden>
              {it.done ? <Check size={12} /> : null}
            </span>
            {it.done ? (
              <>
                <span className="gd-done">{it.label}</span>
                <span className="sr-only">(done)</span>
              </>
            ) : (
              <button type="button" className="link" onClick={() => onOpen(START_TARGET[it.id])}>
                {it.label}
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function GuideRow({ id, state, featured, onOpen }: { id: GuideId; state: GuideState; featured: boolean; onOpen: () => void }) {
  const g = guideMeta(id);
  return (
    <button type="button" className={featured ? "gd-item featured" : "gd-item"} onClick={onOpen}>
      <b>{g.title}</b>
      <Tag tone={state.tone} dot>{state.label}</Tag>
      <span className="d">{g.blurb}</span>
      <ChevronRight size={16} className="chev" aria-hidden />
    </button>
  );
}

export function GuidesPage({
  runKhipu,
  signals,
  startItems,
  setupKnown = { prefs: true, signals: true },
  states,
  open,
  onOpen,
  onBack,
  onTarget,
}: {
  runKhipu: (args: string[]) => Promise<string>;
  signals: Signals;
  startItems: StartItem[];
  /** What has been read yet; the setup card waits for it instead of showing
   *  a count made of empty defaults. */
  setupKnown?: { prefs: boolean; signals: boolean };
  states: Record<GuideId, GuideState>;
  /** The guide on screen, with the step to open it at (Grok Bot). */
  open: { id: GuideId; step?: number } | null;
  onOpen: (id: GuideId) => void;
  onBack: () => void;
  onTarget: (t: GuideTarget) => void;
}) {
  const [query, setQuery] = useState("");
  const { prefs, updatePrefs } = signals;

  if (open) {
    const meta = guideMeta(open.id);
    const outline = OUTLINES[open.id];
    return (
      <div className="gd-page">
        <nav className="gd-crumb" aria-label="Breadcrumb">
          <button type="button" className="link" onClick={onBack}>Guides</button> / {GROUP_TITLES[meta.group]}
        </nav>
        {open.id === "grok_bot" ? (
          <GrokBotGuide
            runKhipu={runKhipu}
            signals={signals}
            startStep={open.step}
            onOpenGuide={onOpen}
            onOpenHarnesses={() => onTarget({ kind: "harnesses" })}
          />
        ) : outline ? (
          <article className="gd-outline">
            <h2 className="gd-title">{meta.title}</h2>
            <p className="gd-lede">{outline.lede}</p>
            <Tag tone={states[open.id].tone} dot>{states[open.id].label}</Tag>
            {outline.note ? <div className="gd-callout info"><span>{outline.note}</span></div> : null}
            <ol className="gd-steps plain">
              {outline.steps.map((s, i) => (
                <li key={s.title} className="gd-step todo">
                  <span className="gd-num-dot" aria-hidden>{i + 1}</span>
                  <div>
                    <h5>{s.title}</h5>
                    {s.text ? <p>{s.text}</p> : null}
                  </div>
                </li>
              ))}
            </ol>
            {outline.opens?.length ? (
              <div className="gd-row">
                {outline.opens.map((o) => (
                  <button key={o.label} type="button" onClick={() => onTarget(o.target)}>
                    <ExternalLink size={14} aria-hidden /> {o.label}
                  </button>
                ))}
              </div>
            ) : null}
            <p className="meta">This is the outline. The full step-by-step guide is coming.</p>
          </article>
        ) : null}
      </div>
    );
  }

  const shown = filterGuides(query);
  return (
    <div className="gd-page">
      {!setupKnown.prefs ? null : prefs.startHidden ? (
        <div className="gd-row gd-showagain">
          <button type="button" className="link" onClick={() => updatePrefs({ startHidden: false, hintHidden: false })}>
            Show setup steps again
          </button>
        </div>
      ) : !setupKnown.signals ? (
        <section className="gd-card gd-start" role="status" aria-busy="true" aria-label="Setup progress">
          <h4>Checking your setup</h4>
          <div className="gd-count">Looking at which agents and accounts are connected.</div>
        </section>
      ) : (
        <StartHere
          items={startItems}
          setUpSeen={prefs.setUpSeen}
          onHide={() => updatePrefs({ startHidden: true, hintHidden: true })}
          onOpen={onOpen}
          onDismissAllSet={() => updatePrefs({ setUpSeen: true })}
        />
      )}

      <div className="gd-searchrow">
        <label className="sr-only" htmlFor="gd-search">Search guides</label>
        <input
          id="gd-search"
          type="search"
          value={query}
          placeholder="Search guides, for example “Grok” or “backup”"
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {shown.length === 0 ? <p className="meta">No guide matches “{query}”.</p> : null}
      {GROUP_ORDER.map((group) => {
        const rows = shown.filter((g) => g.group === group);
        if (rows.length === 0) return null;
        return (
          <section key={group}>
            <div className="gd-group-h">{GROUP_TITLES[group]}</div>
            <div className="gd-list">
              {rows.map((g) => (
                <GuideRow key={g.id} id={g.id} state={states[g.id]} featured={g.id === "grok_bot"} onOpen={() => onOpen(g.id)} />
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}
