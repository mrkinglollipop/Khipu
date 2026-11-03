/* Settings -> Accounts and routing: Needs you in three bands (slice A of
   docs/plans/2026-11-03-settings-reorg.md; mock
   docs/mockups/2026-11-03-settings/index.html). Decide: a conversation's change,
   before and now, with buttons that name the result. Fix: what isn't working.
   When you have time: what can wait. The problems come from routingOverview.ts,
   the decisions from routingNeeds.ts. */

import { useRef, useState } from "react";
import type { ReactNode } from "react";
import { DecideWords } from "./DecideWords";
import { Busy, ConfirmGroup, EndOrKeepChoices, WriteError, escapeCloses, ruleTarget, useEndOrKeep } from "./RoutingSettings";
import type { Back, Writer } from "./RoutingSettings";
import type { RouteRule, RouteView } from "./routing";
import type { DecideCard } from "./routingNeeds";
import type { Problem } from "./routingOverview";

type EndOrKeep = ReturnType<typeof useEndOrKeep>;

function RuleEdit({ card, rule, w, eok, onClose }: { card: DecideCard; rule: RouteRule; w: Writer; eok: EndOrKeep; onClose: () => void }) {
  const [draft, setDraft] = useState(rule.text);
  const [sure, setSure] = useState(false);
  const id = `${card.key}:edit`;
  return (
    <form className={sure ? "route-confirm" : "route-pause"} aria-label={`Edit: ${card.title}`} onKeyDown={escapeCloses(onClose)}
      onSubmit={async (e) => {
        e.preventDefault();
        if (!draft.trim()) {
          w.fail(id, "Write the words first.");
          return;
        }
        if (!sure) {
          w.fail(id, null);
          setSure(true);
          return;
        }
        if (await eok.attempt(id, "khipu_route_rule", { action: "edit", ...ruleTarget(rule), text: draft }, null, undefined, onClose)) onClose();
      }}>
      <div className="toolbar">
        <label htmlFor={id}>Words</label>
        <input id={id} value={draft} autoFocus autoComplete="off" readOnly={sure} onChange={(e) => setDraft(e.target.value)} />
      </div>
      {sure ? <span>Make these your words? They replace what the conversation wrote.</span> : null}
      <WriteError w={w} id={id} />
      <div className="toolbar">
        <button type="submit" className="primary sm" disabled={w.isBusy(id)}>
          <Busy on={w.isBusy(id)} />
          {sure ? "Make them mine" : "Save…"}
        </button>
        <button type="button" className="sm link" onClick={onClose}>Cancel</button>
      </div>
    </form>
  );
}

function DecideItem({ card, data, w, eok, reread, back }: {
  card: DecideCard;
  data: RouteView | null;
  w: Writer;
  eok: EndOrKeep;
  reread: () => Promise<unknown>;
  back: Back;
}) {
  const [editing, setEditing] = useState(false);
  const [asking, setAsking] = useState(false);
  const backBtn = useRef<HTMLButtonElement>(null);
  const keepId = `${card.key}:keep`;
  const backId = `${card.key}:back`;
  const t = card.target;
  const expect = data?.priority?.version ?? data?.priority_version ?? null;
  const keep = () => {
    if (t.type === "priority") return eok.attempt(keepId, "khipu_route_priority", { action: "confirm", expect }, null, back);
    if (t.type === "rule" && t.rule) return eok.attempt(keepId, "khipu_route_rule", { action: "confirm", ...ruleTarget(t.rule), expect: t.rule.revision }, null, back);
    if (t.type === "setting") return w.run(keepId, "khipu_config_request", { key: t.key, action: "confirm" }, reread, back);
    return Promise.resolve(false);
  };
  const undo = () => {
    if (t.type === "priority") return w.run(backId, "khipu_route_priority", { action: "undo", expect }, null, back);
    if (t.type === "rule" && t.rule) {
      // A live proposal is dismissed; a pending rule with no proposal record has its own undo.
      const action = t.proposal || t.rule.pending_kind === "added" ? "dismiss" : "undo";
      return w.run(backId, "khipu_route_rule", { action, ...ruleTarget(t.rule) }, null, back);
    }
    if (t.type === "setting") return w.run(backId, "khipu_config_request", { key: t.key, action: "undo" }, reread, back);
    return Promise.resolve(false);
  };
  const rule = t.type === "rule" ? t.rule : null;
  return (
    <article className="needs-card" data-route-item={card.key} tabIndex={-1} aria-label={card.title}>
      <DecideWords card={card} />
      {card.keep || card.back || card.edit ? (
        <div className="needs-acts">
          {card.keep ? (
            <button type="button" className="sm primary" disabled={w.isBusy(keepId)} onClick={() => void keep()}>
              <Busy on={w.isBusy(keepId)} />
              {card.keep}
            </button>
          ) : null}
          {card.edit && rule ? (
            <button type="button" className="sm" aria-expanded={editing} onClick={() => setEditing((v) => !v)}>
              {card.edit}
            </button>
          ) : null}
          {card.back ? (
            <button ref={backBtn} type="button" className="sm" disabled={w.isBusy(backId)} aria-expanded={card.backAsk ? asking : undefined}
              onClick={() => { if (card.backAsk) setAsking((v) => !v); else void undo(); }}>
              {card.backAsk ? null : <Busy on={w.isBusy(backId)} />}
              {card.back}
            </button>
          ) : null}
        </div>
      ) : null}
      {asking && card.back && card.backAsk ? (
        <ConfirmGroup label={`${card.back}?`} onClose={() => setAsking(false)} back={() => backBtn.current}>
          <span>{card.backAsk}</span>
          <div className="toolbar">
            <button type="button" className="sm danger" disabled={w.isBusy(backId)}
              onClick={async () => { if (await undo()) setAsking(false); }}>
              <Busy on={w.isBusy(backId)} />
              Yes, {card.back.charAt(0).toLowerCase()}{card.back.slice(1)}
            </button>
            <button type="button" className="sm link" onClick={() => setAsking(false)}>Cancel</button>
          </div>
        </ConfirmGroup>
      ) : null}
      {editing && rule ? <RuleEdit card={card} rule={rule} w={w} eok={eok} onClose={() => setEditing(false)} /> : null}
      <WriteError w={w} id={keepId} />
      <WriteError w={w} id={backId} />
    </article>
  );
}

/** Rules copied from the old routing file, read and kept where they are listed. */
function SeededRules({ rules, w, eok, onEdit, back }: { rules: RouteRule[]; w: Writer; eok: EndOrKeep; onEdit: () => void; back: Back }) {
  return (
    <ul className="needs-seeded">
      {rules.map((r) => {
        const id = `needs:seeded:${r.entity ?? r.n}`;
        return (
          <li key={id}>
            <span className="needs-seeded-text">{r.text}</span>
            <span className="needs-row-acts">
              <button type="button" className="sm" disabled={w.isBusy(id)}
                onClick={() => void eok.attempt(id, "khipu_route_rule", { action: "confirm", ...ruleTarget(r), expect: r.revision }, null, back)}>
                <Busy on={w.isBusy(id)} />
                Keep as written
              </button>
              <button type="button" className="sm" onClick={onEdit}>Edit or remove</button>
            </span>
            <WriteError w={w} id={id} />
          </li>
        );
      })}
    </ul>
  );
}

function Band({ title, sub, children }: { title: string; sub: string; children: ReactNode }) {
  return (
    <>
      <h3 className="needs-band">
        {title} <small>{sub}</small>
      </h3>
      {children}
    </>
  );
}

export function NeedsYou({ cards, accounts, problems, seeded, data, w, reread, onShow, back }: {
  cards: DecideCard[];
  /** The accounts decision, which reads and answers its own request. */
  accounts: ReactNode;
  /** Every problem with a Needs you line. */
  problems: Problem[];
  seeded: RouteRule[];
  data: RouteView | null;
  w: Writer;
  reread: () => Promise<unknown>;
  onShow: (p: Problem) => void;
  back: Back;
}) {
  const eok = useEndOrKeep(w);
  const [readSeeded, setReadSeeded] = useState(false);
  const fix = problems.filter((p) => p.band !== "later");
  const later = problems.filter((p) => p.band === "later");
  const row = (p: Problem) => {
    const verb = p.act ?? "Show";
    const isSeeded = p.kind === "confirm" && seeded.length > 0;
    return (
      <li key={p.key} className="needs-row">
        {p.band === "later" ? null : <span className={`hdot ${p.tone}`} aria-hidden />}
        <span className="needs-row-text">
          <b>{p.needs}</b>
          {p.detail ? <span>{p.detail}</span> : null}
        </span>
        <span className="needs-row-acts">
          {isSeeded ? (
            <button type="button" className="sm" aria-expanded={readSeeded} onClick={() => setReadSeeded((v) => !v)}>
              {readSeeded ? "Hide them" : "Read them"}
            </button>
          ) : (
            <button type="button" className="sm" aria-label={`${verb}: ${p.show ?? "it"}`} onClick={() => onShow(p)}>
              {verb}
            </button>
          )}
        </span>
        {isSeeded && readSeeded ? <SeededRules rules={seeded} w={w} eok={eok} onEdit={() => onShow(p)} back={back} /> : null}
      </li>
    );
  };
  return (
    <section className="needs" role="region" aria-label="Needs you">
      <h2 className="needs-head">Needs you</h2>
      {cards.length || accounts ? (
        <Band title="Decide" sub="A conversation changed something, and it waits for your answer.">
          {cards.map((card) => (
            <DecideItem key={card.key} card={card} data={data} w={w} eok={eok} reread={reread} back={back} />
          ))}
          {accounts}
          {eok.ask && data ? (
            <EndOrKeepChoices view={data} ask={eok.ask} now={new Date()} busy={w.anyBusy} saveLabel="Save and confirm"
              onCancel={() => eok.setAsk(null)} />
          ) : null}
        </Band>
      ) : null}
      {fix.length ? (
        <Band title="Fix" sub="Something isn't working or is about to run out. Nothing was stopped.">
          <ul className="needs-rows" aria-label="Fix">{fix.map(row)}</ul>
        </Band>
      ) : null}
      {later.length ? (
        <Band title="When you have time" sub="Nothing is wrong. These can wait.">
          <ul className="needs-rows" aria-label="When you have time">{later.map(row)}</ul>
        </Band>
      ) : null}
    </section>
  );
}
