// Needs you on Today (slice A3): one card per item, side by side, with Yes
// and No lined up and what each does written under it. Answering a card
// leaves a receipt in its place. Rules (jobs/2026-11-10 A3 brief):
// - One click answers exactly the item on that card, with the pin the read
//   gave it. While an answer is out, every Yes and No ignores presses.
// - `changed` (or another "read it again" refusal): nothing is answered; the
//   list is read again and the card says it changed. Nothing is ever sent
//   again on its own, and nothing answers on load, focus or a held key.
// - An item that can't be taken back (`permanent`) sits alone on its own
//   card and says so before its buttons. There is no "yes to all".
// - Khipu's words for an item are shown as given.
import { useRef, useState, type KeyboardEvent } from "react";
import { callCli } from "../settingsCli";
import { signInOutcome } from "../PlansCard";
import { Receipt } from "./Receipt";
import {
  mergeOrder, offers, sendAnswer, signinLabel,
  type NeedsHook, type NeedsItem, type Receipt as ReceiptData,
} from "./needs";

type Answered = { receipt: ReceiptData; undone: boolean };

/** A held key never answers twice: only the first press of Enter clicks. */
const noRepeat = (e: KeyboardEvent) => {
  if (e.repeat) e.preventDefault();
};

function whoLine(item: NeedsItem): string {
  const from = (item.from_words || item.asked_by?.who || "").trim().replace(/\.$/, "");
  const ago = item.ago ?? item.asked_by?.when ?? "";
  return [from, ago].filter(Boolean).join(" · ");
}

function plural(n: number): string {
  return n === 1 ? "1 question" : `${n} questions`;
}

function AskCard({
  item, note, busy, anyBusy, signing, onAnswer, onSignIn,
}: {
  item: NeedsItem;
  note: string | null;
  busy: boolean;
  anyBusy: boolean;
  signing: boolean;
  onAnswer: (item: NeedsItem, said: "yes" | "no") => void;
  onSignIn: (item: NeedsItem) => void;
}) {
  const can = offers(item);
  const id = `td-ask-${item.key.replace(/[^A-Za-z0-9_-]/g, "_")}`;
  const who = whoLine(item);
  // A shortened item is not offered here: the owner would agree to words they
  // could not read. The app's read can't fetch the whole item yet.
  const cut = !!item.cut;
  const ignore = anyBusy || signing;
  return (
    <article
      className="td-ask"
      data-permanent={item.permanent ? "true" : undefined}
      aria-labelledby={`${id}-q`}
      aria-busy={busy || signing ? "true" : undefined}
    >
      <div className="td-ask-h">
        <p className="td-ask-q" id={`${id}-q`}>{item.words}</p>
        {who ? <p className="td-ask-who">{who}</p> : null}
      </div>
      <div className="td-ask-notes">
        {item.permanent ? (
          <p className="td-ask-c">This can’t be taken back. Yes answers only this question.</p>
        ) : null}
        {cut ? (
          <p className="td-ask-c">
            Khipu shortened these words, and the app can’t show the whole question yet. Ask an agent to read it to you
            in full, and answer there.
          </p>
        ) : null}
        {note ? <p className="td-ask-err" role="alert">{note}</p> : null}
        {busy ? <p className="td-ask-sending" role="status">Sending your answer…</p> : null}
        {signing ? <p className="td-ask-sending" role="status">Finish signing in in the window that opened.</p> : null}
      </div>
      <div className="td-ask-choices">
        {cut ? null : can.signin ? (
          <div className="td-choice">
            <button
              type="button"
              className="primary"
              aria-disabled={ignore ? "true" : undefined}
              aria-describedby={item.do ? `${id}-s` : undefined}
              onKeyDown={noRepeat}
              onClick={() => { if (!ignore) onSignIn(item); }}
            >
              {signinLabel(item) ? `Sign in to ${signinLabel(item)}` : "Sign in"}
            </button>
            {item.do ? <p id={`${id}-s`}>{item.do}</p> : null}
          </div>
        ) : can.yes || can.no ? (
          <>
            {can.yes ? (
              <div className="td-choice">
                <button
                  type="button"
                  className="primary"
                  aria-disabled={ignore ? "true" : undefined}
                  aria-describedby={`${id}-y`}
                  onKeyDown={noRepeat}
                  onClick={() => { if (!ignore) onAnswer(item, "yes"); }}
                >
                  Yes
                </button>
                <p id={`${id}-y`}>{item.confirm_does}</p>
              </div>
            ) : null}
            {can.no ? (
              <div className="td-choice">
                <button
                  type="button"
                  aria-disabled={ignore ? "true" : undefined}
                  aria-describedby={`${id}-n`}
                  onKeyDown={noRepeat}
                  onClick={() => { if (!ignore) onAnswer(item, "no"); }}
                >
                  No
                </button>
                <p id={`${id}-n`}>{item.undo_does}</p>
              </div>
            ) : null}
          </>
        ) : item.do ? (
          <p className="td-ask-do">{item.do}</p>
        ) : null}
      </div>
    </article>
  );
}

export function NeedsCards({ needs, onAllChanges }: { needs: NeedsHook; onAllChanges: () => void }) {
  const data = needs.data;
  const [answered, setAnswered] = useState<Record<string, Answered>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [gone, setGone] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [signing, setSigning] = useState<string | null>(null);
  // Set at once on a press, before React re-renders, so a second press in the
  // same moment never sends a second answer.
  const sending = useRef<string | null>(null);

  const listed = data?.items ?? [];
  const listedKeys = listed.map((it) => it.key);
  // A receipt stays where its card was; an undone one gives the card back if
  // the item is waiting again.
  const shownReceipts = Object.keys(answered).filter((k) => !(answered[k].undone && listedKeys.includes(k)));
  // Cards keep their places between reads; a new item goes after them.
  const order = useRef<string[]>([]);
  order.current = mergeOrder(order.current, [...new Set([...listedKeys, ...shownReceipts])]);

  const setNote = (key: string, text: string | null) =>
    setNotes((prev) => {
      const out = { ...prev };
      if (text) out[key] = text;
      else delete out[key];
      return out;
    });

  const answer = async (item: NeedsItem, said: "yes" | "no") => {
    if (sending.current) return;
    sending.current = item.key;
    setBusy(item.key);
    setNote(item.key, null);
    setGone(null);
    try {
      const out = await sendAnswer(item, said);
      if (out.kind === "done") {
        setAnswered((prev) => ({ ...prev, [item.key]: { receipt: out.receipt, undone: false } }));
        await needs.reload();
        return;
      }
      if (out.kind === "signin") {
        // A sign-in's yes changes nothing; it hands back the account, and the
        // app starts the sign-in it already has.
        sending.current = null;
        setBusy(null);
        setSigning(item.key);
        try {
          const res = await callCli<{ ok?: boolean; error?: string }>("khipu_account_sign_in", {
            harness: out.account.harness, home: out.account.home,
          });
          const outcome = signInOutcome(res as Parameters<typeof signInOutcome>[0]);
          if (res.ok && res.data.ok) {
            setAnswered((prev) => ({
              ...prev,
              [item.key]: { receipt: { text: outcome, action_id: null, undoable: false, saved_on: [], not_reached: [] }, undone: false },
            }));
          } else {
            setNote(item.key, outcome);
          }
          await needs.reload();
        } finally {
          setSigning(null);
        }
        return;
      }
      setNote(item.key, out.sentence);
      if (out.kind === "failed" || out.reread) {
        const fresh = await needs.reload();
        if (fresh && !fresh.items.some((it) => it.key === item.key)) setGone(out.sentence);
      }
    } finally {
      if (sending.current === item.key) {
        sending.current = null;
        setBusy(null);
      }
    }
  };

  const signIn = (item: NeedsItem) => void answer(item, "yes");
  const undone = (key: string) => {
    setAnswered((prev) => (prev[key] ? { ...prev, [key]: { ...prev[key], undone: true } } : prev));
    void needs.reload();
  };

  const waiting = listed.filter((it) => !shownReceipts.includes(it.key));
  const left = Math.max(0, (data?.total ?? 0) - (listed.length - waiting.length));
  const byKey = new Map(listed.map((it) => [it.key, it]));
  const slots = order.current.filter((k) => shownReceipts.includes(k) || byKey.has(k));
  const unread = data?.unread ?? [];

  return (
    <section className="td-needs" id="today-needs-you" aria-labelledby="td-needs-h">
      <div className="td-needs-head">
        <h2 id="td-needs-h">Needs you</h2>
        {left > 0 ? <span className="td-count">{plural(left)}</span> : null}
      </div>
      {gone ? <p className="td-ask-err" role="alert">{gone}</p> : null}
      {unread.length ? (
        <p className="td-quiet">Khipu couldn’t read part of the list ({unread.map((u) => u.what).join(", ")}). What it could read is here.</p>
      ) : null}
      {!data ? (
        <p className="td-quiet" role="status">
          {needs.failed ? "Couldn’t read the list just now; Khipu will try again." : "Reading what needs you…"}
        </p>
      ) : slots.length === 0 ? (
        <div className="td-empty">
          <p className="td-empty-h">{unread.length ? "Nothing Khipu could read needs you." : "Nothing needs you."}</p>
          <p className="td-quiet">When an agent needs a yes or no from you, it shows up here.</p>
          {data.last_answer ? (
            <p className="td-last">
              Your last answer{data.last_answer.ago ? `, ${data.last_answer.ago}` : ""}: {data.last_answer.text}{" "}
              <button type="button" className="td-link" onClick={onAllChanges}>See all changes</button>
            </p>
          ) : null}
        </div>
      ) : (
        <div className="td-asks">
          {slots.map((k) => {
            const a = answered[k];
            if (a && shownReceipts.includes(k)) {
              return (
                <div key={k} className="td-ask td-ask-done">
                  <Receipt data={a.receipt} onUndone={() => undone(k)} />
                </div>
              );
            }
            const item = byKey.get(k) as NeedsItem;
            return (
              <AskCard
                key={k}
                item={item}
                note={notes[k] ?? null}
                busy={busy === k}
                anyBusy={busy !== null}
                signing={signing === k}
                onAnswer={(it, said) => void answer(it, said)}
                onSignIn={signIn}
              />
            );
          })}
          {waiting.length === 0 && left === 0 ? (
            <p className="td-everything"><b>That’s everything.</b> Nothing else needs you.</p>
          ) : null}
        </div>
      )}
      {data && data.more > 0 ? (
        <p className="td-quiet">{data.more} more are waiting. Answer these to see the rest.</p>
      ) : null}
      {needs.failed && data ? <p className="td-quiet">Couldn’t refresh the list just now; Khipu will try again.</p> : null}
      <p className="td-foot">
        Same list on each of your Macs and in any agent.{" "}
        {data && slots.length > 0 ? <button type="button" className="td-link" onClick={onAllChanges}>See all changes</button> : null}
      </p>
    </section>
  );
}
