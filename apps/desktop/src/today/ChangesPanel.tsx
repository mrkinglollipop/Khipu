// All changes (slice A3): every owner change, from the app or from an agent,
// newest first, docked at the right of Today. Each line that can be undone
// has one Undo, which runs the existing `khipu_owner_direction_undo` once per
// press. The list is what `owner-direction actions --json` reports, read
// again after each undo; nothing here is kept from what the app sent.
import { useCallback, useEffect, useRef, useState } from "react";
import { Undo2 } from "lucide-react";
import { Inspector } from "../shell";
import { callCli } from "../settingsCli";
import { actionWords, type OwnerAction } from "../OwnerActions";
import { undoRefusal } from "./Receipt";

/** Words the new app no longer shows (scope.md, "Words that leave the screen"). */
export const REMOVED_WORDS = [
  "harness", "hub", "capture", "episode", "job slot", "embedding", "gateway", "ledger", "owed", "tier", "hold", "cap",
  "override",
];

export function hasRemovedWord(text: string): boolean {
  return REMOVED_WORDS.some((w) => new RegExp(`\\b${w}s?\\b`, "i").test(text));
}

function accountName(key: string): string {
  const [harness, home] = key.split("|");
  const name = harness ? harness[0].toUpperCase() + harness.slice(1) : key;
  if (!home) return name;
  const folder = home.split("/").filter(Boolean).pop() ?? home;
  return `${name} (${folder.replace(/^\./, "")})`;
}

/** One line per change in the new app's words. The old Settings list keeps
 *  its own wording (`actionWords`); only the lines that use a word the new
 *  screens drop are said differently here. */
export function changeWords(a: OwnerAction): string {
  const t = a.target ?? "";
  const row = t.includes("#") ? t.split("#")[1] : t;
  switch (a.action) {
    case "route.cap.set": return `Set where agents stop on ${accountName(t)}`;
    case "route.cap.remove": return `Removed where agents stop on ${accountName(t)}`;
    case "route.hold.add": return "Kept work away from an account or a kind of work";
    case "route.hold.end": return "Let work go there again";
    case "route.override.grant": return `Let a conversation run ahead on ${accountName(t)}`;
    case "route.override.end": return "Ended a conversation running ahead";
    case "route.override.keep": return "Let a conversation keep running ahead for another week";
    case "route.tier_table.set": return "Changed which models do which work";
    case "route.tier_table.confirm": return "Confirmed which models do which work";
    case "route.tier_table.clear": return "Cleared which models do which work";
    case "ledger.import": return "Brought rows into a project work list";
    case "ledger.restore": return `Restored ${row} in a project work list`;
    case "ledger.purge": return `Removed ${row} from a project work list for good`;
    case "ledger.add": return `Added ${row} to a project work list`;
    case "ledger.set": return `Changed ${row} in a project work list`;
    case "ledger.remove": return `Removed ${row} from a project work list`;
    case "ledger.project": return "Changed a project work list's project";
    case "owed.change": return "Changed items on your list";
    case "owed.tidy": return "Tidied your list";
    case "owed.correct_job_origin": case "owed.correct_job_origin.confirm": return "Corrected where an item on your list came from";
    case "forget.send": return "Sent this Mac's forgotten conversations to your other Macs";
    case "forget.here": return `Forgot conversation ${t} everywhere`;
    case "forget.not_same": return `Kept conversation ${t}: it is not the one forgotten here`;
    case "outbox.merge_preferences": return "Merged saved notes from conversations still waiting to be saved";
    case "outbox.span_covered": return "Dropped waiting conversations another one already covers";
    case "direction.set":
      if (t.startsWith("trust ")) return "Let another app answer for you";
      if (t.startsWith("untrust ")) return "Stopped another app answering for you";
      return "Changed how Khipu knows it's you";
    default: {
      const words = actionWords(a);
      return hasRemovedWord(words) ? "Changed a setting" : words;
    }
  }
}

function when(iso: string | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
}

/** Who made it and how. `mac` comes with backend slice B8; until then a line
 *  says how, without the Mac. */
export function whoWords(a: OwnerAction & { mac?: string }): string {
  const on = a.mac ? `, on ${a.mac}` : "";
  const how =
    a.auth === "app" ? `You, in the app${on}`
      : a.auth === "terminal" ? `You, with a typed yes${on}`
      : a.auth === "direction" ? `You, through an agent${on}`
      : `You${on}`;
  return [how, when(a.at)].filter(Boolean).join(" · ");
}

export function canUndoLine(a: OwnerAction): boolean {
  return !a.no_undo && (a.state === "applied" || a.state === "scheduled" || a.state === "unconfirmed" || a.state == null);
}

function stateWords(a: OwnerAction): string | null {
  if (a.state === "failed") return "Didn’t happen";
  if (a.state === "undone") return "Undone";
  if (a.state === "cancelled") return "Cancelled";
  if (a.state === "unconfirmed") return "Not known if it finished";
  return null;
}

export function ChangesPanel({ onClose, onChanged }: { onClose: () => void; onChanged: () => void }) {
  const [rows, setRows] = useState<OwnerAction[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [lineError, setLineError] = useState<Record<string, string>>({});
  const sending = useRef<string | null>(null);

  const load = useCallback(async () => {
    const res = await callCli<{ actions?: OwnerAction[] }>("khipu_owner_direction_actions");
    if (res.ok) {
      setRows(Array.isArray(res.data.actions) ? res.data.actions : []);
      setError(null);
    } else {
      setError("Couldn’t read the list of changes just now.");
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const undo = async (a: OwnerAction) => {
    if (sending.current) return;
    sending.current = a.id;
    setBusy(a.id);
    setLineError((p) => { const o = { ...p }; delete o[a.id]; return o; });
    const res = await callCli("khipu_owner_direction_undo", { id: a.id });
    if (!res.ok) setLineError((p) => ({ ...p, [a.id]: undoRefusal(res.refusal?.code, res.error) }));
    await load();
    if (res.ok) onChanged();
    sending.current = null;
    setBusy(null);
  };

  return (
    <Inspector title="All changes" onClose={onClose}>
      <p className="td-panel-sum">Every change to your accounts and rules, from the app or from an agent, newest first.</p>
      {error ? <p className="td-ask-err" role="alert">{error}</p> : null}
      {rows === null && !error ? <p className="td-quiet" role="status">Reading the list…</p> : null}
      {rows && rows.length === 0 ? <p className="td-quiet">No changes yet. Each one shows here once it is made.</p> : null}
      {rows && rows.length ? (
        <ul className="td-changes" aria-label="All changes, newest first">
          {rows.map((a) => {
            const st = stateWords(a);
            return (
              <li key={a.id} className="td-change" data-undone={a.state === "undone" ? "true" : undefined}>
                <div>
                  <p>{changeWords(a)}</p>
                  <p className="td-change-w">{whoWords(a)}{st ? ` · ${st}` : ""}</p>
                  {lineError[a.id] ? <p className="td-ask-err" role="alert">{lineError[a.id]}</p> : null}
                </div>
                {canUndoLine(a) ? (
                  <button
                    type="button"
                    className="td-undo"
                    aria-disabled={busy !== null ? "true" : undefined}
                    aria-label={`Undo: ${changeWords(a)}`}
                    onKeyDown={(e) => { if (e.repeat) e.preventDefault(); }}
                    onClick={() => { if (busy === null) void undo(a); }}
                  >
                    <Undo2 size={15} aria-hidden />
                    {busy === a.id ? "Undoing…" : "Undo"}
                  </button>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}
    </Inspector>
  );
}
