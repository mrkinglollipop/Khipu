import { useId, useState } from "react";
import { readLine } from "../accounts/accountsModel";
import type { UsageReport } from "../PlansCard";
import type { RouteView } from "../routing";
import { readUsageChoices, saveUsageChoices, usageTiles } from "./usage";

export function Usage({ view, plans, now, onOpenAccount, error }: {
  view: RouteView | null | undefined; plans: UsageReport | null | undefined; now: Date;
  onOpenAccount?: (key: string) => void; error?: string | null;
}) {
  const [choosing, setChoosing] = useState(false);
  const [choices, setChoices] = useState(readUsageChoices);
  const panelId = useId();
  const tiles = usageTiles(view, plans, choices, now);
  const hidden = tiles.filter((t) => !t.shown);
  return (
    <section className="td-usage" aria-labelledby="td-usage-h">
      <div className="td-usage-head">
        <h2 id="td-usage-h">Usage</h2>
        <span className="td-usage-meta">{readLine(view, now)?.replace(/\.$/, "")} {view ? "· " : ""}
          <button type="button" className="td-link" aria-expanded={choosing} aria-controls={panelId} onClick={() => setChoosing((v) => !v)}>
            {choosing ? "Done" : "Choose accounts"}
          </button>
        </span>
      </div>
      <div className="td-usage-choose" id={panelId} hidden={!choosing}>
        <ul>{tiles.map((t) => (
          <li key={t.key}><label>
            <span>{t.label}{t.ended ? <small>{t.planEnd}</small> : null}</span>
            <input type="checkbox" role="switch" aria-label={t.label} checked={t.shown} onChange={(e) => {
              const next = { ...choices, [t.key]: e.target.checked };
              setChoices(next); saveUsageChoices(next);
            }} />
          </label></li>
        ))}</ul>
        <p>Turning one off only takes it off this screen. Khipu keeps the account, and you can turn it back on here.</p>
      </div>
      {!view ? <p className="td-quiet" role="status">{error ?? "Reading your accounts…"}</p> : !tiles.length ? <p className="td-quiet">Khipu isn't tracking any accounts yet.</p> : null}
      {view && error ? <p className="td-quiet">{error}</p> : null}
      <div className="td-usage-grid">{tiles.filter((t) => t.shown).map((t) => (
        <button key={t.key} type="button" className="td-usage-tile" aria-label={[t.label, t.word, [t.line, t.window].filter(Boolean).join(" "), t.detail].filter(Boolean).join(", ")} onClick={() => onOpenAccount?.(t.key)}>
          <span className="td-usage-top"><span className="td-usage-name">{t.label}</span>
            <span className={`td-usage-pace ${t.tone}`}><i aria-hidden="true" />{t.word}</span>
          </span>
          {t.left != null ? <span className="td-usage-bar" aria-hidden="true"><i className={t.tone} style={{ width: `${t.left}%` }} /></span> : null}
          <span className="td-usage-left"><b>{t.line}</b>{t.window ? <span>{t.window}</span> : null}</span>
          {t.detail ? <span className="td-usage-detail">{t.detail}</span> : null}
        </button>
      ))}</div>
      {hidden.length ? <p className="td-usage-hidden">Not shown: {hidden.map((t) => t.label).join(", ")}</p> : null}
    </section>
  );
}
