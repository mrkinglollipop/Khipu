// Needs you on Today (slice A3): the one list `khipu_needs_you` reads (the
// same list the MCP tool and every agent give), and the owner's Yes or No to
// one item through `khipu_needs_you_answer`. The JSON is B5's
// (jobs/2026-11-10/b5-needs-you.md).
//
// The rules this file keeps:
// - One answer is one item: the key and the `expect` pin of the item whose
//   words are on that card, as the read gave them. Nothing here batches.
// - Nothing is answered except from a click. A read never answers; a refused
//   or failed answer is never sent again on its own.
// - Khipu's words for an item are shown as given, never rewritten.
import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";

export type NeedsSide = { action: string; args?: Record<string, unknown> } | null;

export type NeedsItem = {
  kind: string;
  key: string;
  words: string;
  confirm_does?: string;
  undo_does?: string;
  if_ignored?: string;
  settle?: { confirm?: NeedsSide; undo?: NeedsSide };
  decline_only?: boolean;
  /** Class C: it can't be taken back. Always on its own card. */
  permanent?: boolean;
  /** Khipu shortened the words; the whole item is read by its key. */
  cut?: boolean;
  do?: string;
  asked_by?: { who?: string; at?: string | null; when?: string | null };
  from_words?: string;
  mac?: string;
  ago?: string | null;
  /** The pin the answer must send back, or null when the item has none. */
  expect?: string | null;
};

export type LastAnswer = { text: string; at?: string | null; ago?: string | null; action_id?: string | null; undoable?: boolean };

export type NeedsRead = {
  total: number;
  items: NeedsItem[];
  more: number;
  this_mac: string | null;
  last_answer: LastAnswer | null;
  unread: Array<{ what: string; why?: string }>;
};

/** Reads the CLI's JSON; null when it is not a Needs-you answer. An item
 *  without a key or words is dropped: there would be nothing to show. */
export function parseNeeds(text: string): NeedsRead | null {
  let v: Record<string, unknown>;
  try {
    v = JSON.parse(text) as Record<string, unknown>;
  } catch {
    return null;
  }
  if (!v || typeof v !== "object" || v.ok === false || !Array.isArray(v.items)) return null;
  const items = (v.items as NeedsItem[]).filter(
    (it) => it && typeof it.key === "string" && it.key && typeof it.words === "string",
  );
  const last = v.last_answer as LastAnswer | null | undefined;
  return {
    total: typeof v.total === "number" ? v.total : items.length,
    items,
    more: typeof v.more === "number" ? v.more : 0,
    this_mac: typeof v.this_mac === "string" ? v.this_mac : null,
    last_answer: last && typeof last.text === "string" ? last : null,
    unread: Array.isArray(v.unread) ? (v.unread as NeedsRead["unread"]) : [],
  };
}

/** Which answers an item offers: a button only where Khipu gave an action. */
export function offers(item: NeedsItem): { yes: boolean; no: boolean; signin: boolean } {
  if (item.kind === "signin") return { yes: false, no: false, signin: true };
  return { yes: !!item.settle?.confirm, no: !!item.settle?.undo, signin: false };
}

/** The account a sign-in item names (`plan:{label}`), for its button. */
export function signinLabel(item: NeedsItem): string {
  return item.key.startsWith("plan:") ? item.key.slice(5) : "";
}

export type Receipt = {
  text: string;
  action_id: string | null;
  undoable: boolean;
  saved_on: string[];
  not_reached: string[];
};

export type AnswerOutcome =
  | { kind: "done"; receipt: Receipt }
  | { kind: "signin"; account: { label: string; harness: string; home: string | null } }
  | { kind: "refused"; code: string; sentence: string; reread: boolean }
  | { kind: "failed"; sentence: string };

/** Each refusal code in one plain sentence. Every one says nothing changed,
 *  because a refused answer changed nothing. `reread`: read the list again
 *  before the owner answers. */
const REFUSALS: Record<string, { sentence: string; reread: boolean }> = {
  changed: { sentence: "This changed since you saw it, so nothing was changed. Read it again, then answer.", reread: true },
  version_moved: { sentence: "This changed since you saw it, so nothing was changed. Read it again, then answer.", reread: true },
  not_found: { sentence: "This is no longer waiting, so nothing was changed. It may have been answered somewhere else.", reread: true },
  not_pending: { sentence: "This was already settled, so nothing was changed.", reread: true },
  decline_only: { sentence: "This one can only be turned down, so nothing was changed. Press No to drop it.", reread: false },
  no_action: { sentence: "There is nothing for this answer to do, so nothing was changed.", reread: false },
  owner_only: { sentence: "Khipu couldn't tell that this answer came from the app, so nothing was changed.", reread: false },
  bad_args: { sentence: "Khipu didn't understand the answer, so nothing was changed.", reread: false },
  not_sent: { sentence: "Your answer couldn't be sent to the other Mac, so nothing was changed.", reread: false },
  refused: { sentence: "Khipu refused the change, so nothing was changed.", reread: false },
};

export function refusalSentence(code: string, error?: string): { sentence: string; reread: boolean } {
  const known = REFUSALS[code];
  const why = typeof error === "string" ? error.trim() : "";
  if (known) {
    // Khipu's own words add the reason for the codes whose sentence can't know it.
    const add = (code === "no_action" || code === "refused") && why ? ` ${why}` : "";
    return { sentence: known.sentence + add, reread: known.reread };
  }
  return { sentence: `Khipu refused the change, so nothing was changed.${why ? ` ${why}` : ""}`, reread: false };
}

/** A failed call (no answer, a timeout) may or may not have reached Khipu. */
export const NO_ANSWER =
  "Khipu didn't answer, so this may not have been applied. Check All changes before you answer it again.";

function asReceipt(v: unknown, said: "yes" | "no"): Receipt {
  const r = (v && typeof v === "object" ? v : {}) as Partial<Receipt>;
  return {
    text: typeof r.text === "string" && r.text ? r.text : said === "yes" ? "You said yes." : "You said no.",
    action_id: typeof r.action_id === "string" && r.action_id ? r.action_id : null,
    undoable: r.undoable === true,
    saved_on: Array.isArray(r.saved_on) ? r.saved_on.map(String) : [],
    not_reached: Array.isArray(r.not_reached) ? r.not_reached.map(String) : [],
  };
}

/** The answer's JSON as an outcome. */
export function readAnswer(text: unknown, said: "yes" | "no"): AnswerOutcome {
  if (typeof text !== "string") return { kind: "failed", sentence: NO_ANSWER };
  let v: Record<string, unknown>;
  try {
    v = JSON.parse(text) as Record<string, unknown>;
  } catch {
    return { kind: "failed", sentence: NO_ANSWER };
  }
  if (!v || typeof v !== "object") return { kind: "failed", sentence: NO_ANSWER };
  if (v.ok !== true) {
    const code = typeof v.code === "string" ? v.code : "refused";
    return { kind: "refused", code, ...refusalSentence(code, typeof v.error === "string" ? v.error : undefined) };
  }
  const acct = v.signin_account as { label?: unknown; harness?: unknown; home?: unknown } | undefined;
  if (acct && typeof acct.harness === "string") {
    return {
      kind: "signin",
      account: { label: String(acct.label ?? ""), harness: acct.harness, home: typeof acct.home === "string" ? acct.home : null },
    };
  }
  return { kind: "done", receipt: asReceipt(v.receipt, said) };
}

/** One answer to one item: its key and the pin the read gave it. */
export async function sendAnswer(item: NeedsItem, said: "yes" | "no"): Promise<AnswerOutcome> {
  let text: unknown;
  try {
    text = await invoke<string>("khipu_needs_you_answer", { key: item.key, answer: said, expect: item.expect ?? null });
  } catch {
    return { kind: "failed", sentence: NO_ANSWER };
  }
  return readAnswer(text, said);
}

export type WholeRead =
  | { kind: "whole"; item: NeedsItem }
  | { kind: "gone"; sentence: string }
  | { kind: "failed"; sentence: string };

export const WHOLE_GONE = "This is no longer waiting, so there is nothing to answer. Nothing was changed.";
export const WHOLE_FAILED =
  "Khipu couldn’t give the whole question just now, so it can’t be answered here yet. Nothing was changed.";

/** One item read whole by its key (`khipu_needs_you(key)`). Only an answer
 *  with that same key, its words whole (`cut: false`), counts as whole. */
export async function readWhole(key: string): Promise<WholeRead> {
  let text: unknown;
  try {
    text = await invoke<string>("khipu_needs_you", { key });
  } catch {
    return { kind: "failed", sentence: WHOLE_FAILED };
  }
  let v: Record<string, unknown>;
  try {
    v = JSON.parse(String(text)) as Record<string, unknown>;
  } catch {
    return { kind: "failed", sentence: WHOLE_FAILED };
  }
  if (v && v.ok === false) {
    return v.code === "not_found" ? { kind: "gone", sentence: WHOLE_GONE } : { kind: "failed", sentence: WHOLE_FAILED };
  }
  const items = Array.isArray(v?.items) ? (v.items as NeedsItem[]) : [];
  const one = items.length === 1 ? items[0] : null;
  if (!one || one.key !== key || one.cut !== false || typeof one.words !== "string") {
    return { kind: "failed", sentence: WHOLE_FAILED };
  }
  return { kind: "whole", item: one };
}

export type NeedsHook = {
  data: NeedsRead | null;
  /** The last read failed; what was read before stays on screen. */
  failed: boolean;
  loading: boolean;
  reload: () => Promise<NeedsRead | null>;
};

const REREAD_MS = 2 * 60 * 1000;

/** The list, read on open, when Today comes on screen and every two minutes.
 *  App.tsx holds the one copy, so the sidebar count and Today read the same
 *  list. Reading never answers anything. */
export function useNeedsYou(enabled: boolean, onScreen: boolean): NeedsHook {
  const [data, setData] = useState<NeedsRead | null>(null);
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(false);
  const seq = useRef(0);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  const reload = useCallback(async () => {
    const mine = ++seq.current;
    setLoading(true);
    let parsed: NeedsRead | null = null;
    try {
      parsed = parseNeeds(await invoke<string>("khipu_needs_you"));
    } catch {
      parsed = null;
    }
    // A newer read started meanwhile: it wins.
    if (!alive.current || mine !== seq.current) return parsed;
    if (parsed) setData(parsed);
    setFailed(!parsed);
    setLoading(false);
    return parsed;
  }, []);

  useEffect(() => {
    if (!enabled) return;
    void reload();
    const id = window.setInterval(() => void reload(), REREAD_MS);
    return () => window.clearInterval(id);
  }, [enabled, reload]);

  // Read again when Today comes back on screen (not on the first render: the
  // effect above has just read).
  const wasOnScreen = useRef(onScreen);
  useEffect(() => {
    const came = onScreen && !wasOnScreen.current;
    wasOnScreen.current = onScreen;
    if (enabled && came) void reload();
  }, [enabled, onScreen, reload]);

  return { data, failed, loading, reload };
}

/** Card order: what was on screen keeps its place, new items go after it. */
export function mergeOrder(prev: string[], shown: string[]): string[] {
  const keep = prev.filter((k) => shown.includes(k));
  return [...keep, ...shown.filter((k) => !keep.includes(k))];
}
