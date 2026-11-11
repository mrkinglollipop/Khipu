/* Accounts > Order and rules (slice A6; mock 1280-14 to 1280-17).

   One box to say a change in words, a preview that shows what Khipu understood
   and what will be different, Save, and a receipt with Undo; then the order,
   the owner's rules, and Recent changes beside them.

   The rules this screen keeps (each has a test in OrderAndRules.test.tsx):
   - Typing only types. "See what this does" asks for a preview, which writes
     nothing. Only a click on Save applies, once, with the token of the preview
     on screen, and the button is off while the call is out.
   - Editing the words after a preview throws that preview away.
   - The preview is Khipu's own words; nothing here rephrases it.
   - The words go to `khipu_route_say` and nowhere else: no suggestions, no log.
   - A refusal changes nothing on screen that would suggest a save. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Check, Loader2, Undo2 } from "lucide-react";
import { callCli, useCliState } from "../settingsCli";
import { useRouting } from "../routing";
import type { RouteRule } from "../routing";
import type { OwnerAction } from "../OwnerActions";
import {
  canUndoChange,
  changeWho,
  changeWhen,
  changeWords,
  receiptWords,
  ruleEnds,
  recentChanges,
  refusalWords,
  timedLines,
} from "./orderRulesModel";
import type { SayApplied, SayPreview, SayReceipt } from "./orderRulesModel";
import "./order-rules.css";

type Notice =
  | { kind: "not_understood"; reason: string; considered: string[] }
  | { kind: "refused"; text: string };

type ReceiptState = { receipt: SayReceipt; undone: boolean; undoing: boolean; undoError: string | null };

/** The preview on screen, with the words it was made from (the fallback shows
 *  them exactly, and Save only offers the preview of the words in the box). */
type ShownPreview = { data: SayPreview; words: string };

export const RECENT_SHOWN = 4;

export function OrderAndRules({ active, onSeeAll }: { active: boolean; onSeeAll?: () => void }) {
  const route = useRouting(active);
  const log = useCliState<{ ok?: boolean; actions: OwnerAction[] }>("khipu_owner_direction_actions", active);

  const [words, setWords] = useState("");
  const [asking, setAsking] = useState(false);
  const [preview, setPreview] = useState<ShownPreview | null>(null);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [empty, setEmpty] = useState(false);
  const [receipt, setReceipt] = useState<ReceiptState | null>(null);
  const [rowBusy, setRowBusy] = useState<string | null>(null);
  const [rowError, setRowError] = useState<{ id: string; text: string } | null>(null);

  // A preview answer that arrives after the words changed is out of date.
  const edit = useRef(0);
  // One Save and one Undo at a time, even when a second click lands before React redraws.
  const savingRef = useRef(false);
  const undoingRef = useRef(false);
  const rowRef = useRef(false);
  const input = useRef<HTMLInputElement>(null);

  const reread = useCallback(async () => {
    await Promise.all([route.load(true), log.reload()]);
  }, [route, log]);

  function onType(next: string) {
    // Nothing but typing: the words change, and whatever was previewed from the
    // old words is gone, so Save cannot be pressed for words nobody previewed.
    edit.current += 1;
    setWords(next);
    setPreview(null);
    setNotice(null);
    setEmpty(false);
    setAsking(false);
  }

  async function askPreview(e?: FormEvent) {
    e?.preventDefault();
    const said = words.trim();
    if (!said) {
      setEmpty(true);
      return;
    }
    if (asking || saving) return;
    const mine = ++edit.current;
    setAsking(true);
    setPreview(null);
    setNotice(null);
    setEmpty(false);
    setReceipt(null);
    const res = await callCli<SayPreview>("khipu_route_say", { mode: "preview", value: said });
    if (edit.current !== mine) return;
    setAsking(false);
    if (res.ok) {
      setPreview({ data: res.data, words: said });
      return;
    }
    const code = res.refusal?.code as string | undefined;
    if (code === "not_understood") {
      const reason = typeof res.refusal?.reason === "string" ? (res.refusal.reason as string) : res.error;
      const considered = Array.isArray(res.refusal?.considered) ? (res.refusal.considered as unknown[]).map(String) : [];
      setNotice({ kind: "not_understood", reason, considered });
    } else {
      setNotice({ kind: "refused", text: refusalWords(code, res.error) });
    }
  }

  async function save() {
    if (!preview || savingRef.current) return;
    savingRef.current = true;
    const token = preview.data.token;
    setSaving(true);
    const res = await callCli<SayApplied>("khipu_route_say", { mode: "apply", value: token });
    // The token is spent whatever happened, so the preview goes either way.
    setPreview(null);
    if (res.ok && res.data.receipt) {
      setNotice(null);
      setWords("");
      edit.current += 1;
      setReceipt({ receipt: res.data.receipt, undone: false, undoing: false, undoError: null });
    } else {
      const code = res.ok ? undefined : (res.refusal?.code as string | undefined);
      setNotice({ kind: "refused", text: refusalWords(code, res.ok ? "" : res.error) });
    }
    setSaving(false);
    savingRef.current = false;
    await reread();
  }

  async function undoReceipt() {
    if (!receipt || !receipt.receipt.undoable || receipt.undone || undoingRef.current) return;
    undoingRef.current = true;
    setReceipt({ ...receipt, undoing: true, undoError: null });
    const res = await callCli("khipu_owner_direction_undo", { id: receipt.receipt.action_id });
    setReceipt((cur) =>
      cur ? { ...cur, undoing: false, undone: res.ok, undoError: res.ok ? null : res.error } : cur,
    );
    undoingRef.current = false;
    await reread();
  }

  async function undoRow(a: OwnerAction) {
    if (rowRef.current) return;
    rowRef.current = true;
    setRowBusy(a.id);
    setRowError(null);
    const res = await callCli("khipu_owner_direction_undo", { id: a.id });
    if (!res.ok) setRowError({ id: a.id, text: res.error });
    setRowBusy(null);
    rowRef.current = false;
    await reread();
  }

  useEffect(() => {
    if (!active) return;
    // Back on this view: read the order and the log again.
    void log.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  const now = new Date();
  const view = route.data;
  const entries = view?.priority?.entries ?? [];
  const rules = useMemo<RouteRule[]>(
    () => (view?.rules ?? []).filter((r) => !(r.pending && r.pending_kind === "added")),
    [view?.rules],
  );
  const recent = recentChanges(log.data?.actions, RECENT_SHOWN);
  const timed = timedLines(entries, now);

  return (
    <div className="or-page">
      <div className="or-cols">
        <div className="or-main">
          <section aria-labelledby="or-say-h">
            <form className="or-say" onSubmit={askPreview}>
              <label id="or-say-h" htmlFor="or-say-in">
                Change something by saying it
              </label>
              <div className="or-say-row">
                <input
                  id="or-say-in"
                  ref={input}
                  className="or-field"
                  type="text"
                  value={words}
                  placeholder="e.g. put Codex first"
                  autoComplete="off"
                  autoCorrect="off"
                  spellCheck={false}
                  aria-describedby="or-say-hint"
                  onChange={(e) => onType(e.target.value)}
                />
                <button type="submit" className="primary or-btn" disabled={asking || saving}>
                  {asking ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                  See what this does
                </button>
              </div>
              <p id="or-say-hint" className="or-hint">
                Nothing changes until you save. Or say the same words to any agent.
              </p>
              {empty ? (
                <p className="or-hint" role="status">
                  Type what you want to change first.
                </p>
              ) : null}
            </form>

            {preview ? <PreviewBox shown={preview} saving={saving} onSave={() => void save()} onCancel={() => setPreview(null)} /> : null}
            {notice ? <NoticeBox notice={notice} /> : null}
            {receipt ? <ReceiptBox state={receipt} onUndo={() => void undoReceipt()} /> : null}
          </section>

          <section className="or-sec" aria-labelledby="or-order-h">
            <h2 id="or-order-h">Order your agents try</h2>
            {entries.length ? (
              <ol className="or-order">
                {entries.map((e, i) => (
                  <li key={`${e.account}-${i}`}>
                    <b aria-hidden>{i + 1}</b>
                    {e.label}
                  </li>
                ))}
              </ol>
            ) : (
              <p className="or-muted">{view ? "You haven't set an order." : route.error ? route.error : "Reading your order…"}</p>
            )}
            {timed.map((t) => (
              <p key={t} className="or-hint">
                {t}
              </p>
            ))}
          </section>

          <section className="or-sec" aria-labelledby="or-rules-h">
            <h2 id="or-rules-h">Your rules</h2>
            {rules.length ? (
              <ul className="or-rules">
                {rules.map((r) => (
                  <li key={r.entity ?? r.n}>
                    <span>{r.text}</span>
                    {ruleEnds(r, now) ? <span className="or-until">until {ruleEnds(r, now)}</span> : null}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="or-muted">{view ? "You have no rules." : ""}</p>
            )}
          </section>
        </div>

        <aside className="or-side" aria-labelledby="or-recent-h">
          <div className="or-sechead">
            <h2 id="or-recent-h">Recent changes</h2>
            {onSeeAll ? (
              <button type="button" className="or-link" onClick={onSeeAll}>
                See all
              </button>
            ) : null}
          </div>
          {recent.length ? (
            recent.map((a) => (
              <div key={a.id} className={a.state === "undone" ? "or-change undone" : "or-change"}>
                <div>
                  <div>{changeWords(a, view?.accounts)}</div>
                  <div className="or-who">
                    {changeWho(a)} · {changeWhen(a, now)}
                    {a.state === "undone" ? " · undone" : ""}
                  </div>
                  {rowError?.id === a.id ? (
                    <div className="or-error" role="alert">
                      {rowError.text}
                    </div>
                  ) : null}
                </div>
                {canUndoChange(a) ? (
                  <button type="button" className="or-quiet" disabled={rowBusy !== null} onClick={() => void undoRow(a)} aria-label={`Undo: ${changeWords(a, view?.accounts)}`}>
                    <Undo2 size={15} aria-hidden />
                    Undo
                  </button>
                ) : null}
              </div>
            ))
          ) : (
            <p className="or-muted">{log.data ? "No changes yet." : log.error ?? ""}</p>
          )}
        </aside>
      </div>
    </div>
  );
}

function PreviewBox({
  shown,
  saving,
  onSave,
  onCancel,
}: {
  shown: ShownPreview;
  saving: boolean;
  onSave: () => void;
  onCancel: () => void;
}) {
  const p = shown.data;
  return (
    <div className="or-preview" role="region" aria-label="What will change" aria-live="polite">
      <h2>Here’s what will change</h2>
      {p.fallback ? (
        <p className="or-fallback">
          Khipu did not recognise a change it can make directly. It will add your words as a rule for agents to follow:
          <q className="or-words">{shown.words}</q>
        </p>
      ) : null}
      <p className="or-understood">{p.understood}</p>
      <dl className="or-ba">
        <dt>Now</dt>
        <dd>{p.now}</dd>
        <dt>After</dt>
        <dd className="after">{p.after}</dd>
        {p.ends_words ? (
          <>
            <dt>Ends</dt>
            <dd>{p.ends_words}</dd>
          </>
        ) : null}
      </dl>
      <div className="or-btnrow">
        <button type="button" className="primary or-btn" disabled={saving} onClick={onSave}>
          {saving ? <Loader2 size={14} className="spin" aria-hidden /> : null}
          Save change
        </button>
        <button type="button" className="or-btn" disabled={saving} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function NoticeBox({ notice }: { notice: Notice }) {
  if (notice.kind === "refused") {
    return (
      <div className="or-notice" role="alert">
        <p>{notice.text}</p>
      </div>
    );
  }
  return (
    <div className="or-notice" role="alert">
      <p>{notice.reason}</p>
      {notice.considered.length ? <p className="or-hint">Khipu looked at: {notice.considered.join(", ")}.</p> : null}
      <p className="or-hint">Nothing was changed. Say it another way and preview it again.</p>
    </div>
  );
}

function ReceiptBox({ state, onUndo }: { state: ReceiptState; onUndo: () => void }) {
  if (state.undone) {
    return (
      <div className="or-receipt plain" role="status">
        <Undo2 size={18} aria-hidden />
        <div className="or-receipt-body">Undone. Your order and rules are back to how they were.</div>
      </div>
    );
  }
  const w = receiptWords(state.receipt);
  return (
    <div className="or-receipt" role="status">
      <Check size={18} aria-hidden />
      <div className="or-receipt-body">
        <div>{w.text}</div>
        <div className="or-hint">{w.saved}</div>
        {state.undoError ? <div className="or-error">{state.undoError}</div> : null}
      </div>
      {state.receipt.undoable ? (
        <button type="button" className="or-quiet" disabled={state.undoing} onClick={onUndo}>
          {state.undoing ? <Loader2 size={14} className="spin" aria-hidden /> : <Undo2 size={15} aria-hidden />}
          Undo
        </button>
      ) : null}
    </div>
  );
}
