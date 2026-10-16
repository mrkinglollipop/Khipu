/* Settings -> Owner actions (docs/plans/2026-10-16-owner-direction.md, 2.5
   2.8 and section 4). "Every owner action" lists every owner change, however
   it was made (by direction in a conversation, in the app, on a terminal),
   newest first, with one Undo per line that can be undone, and a banner when
   the log's hash chain is broken; below it, the switches and fields of the
   check itself.

   The app is the owner, so every change here applies at once: each write is
   one fixed-argv Tauri command that runs `khipu owner-direction ... --from-app`,
   then the section re-reads `owner-direction show` and `actions --json`. What
   it shows is what the CLI reports, never what it sent. */

import { useState } from "react";
import { Loader2 } from "lucide-react";
import { Tag } from "./ui";
import { callCli, useCliState } from "./settingsCli";
import { SettingField } from "./SettingsControls";

export type OwnerDirectionSettings = {
  enabled: boolean;
  latest_window_h: number;
  earlier_window_min: number;
  trusted: string[];
  session_binding: boolean;
};

export type OwnerDirectionShow = {
  ok?: boolean;
  settings: OwnerDirectionSettings;
  turns: Record<string, { turns: number; eligible: number; entrypoints: Record<string, number> }>;
  actions?: number;
  by_direction?: number;
};

export type OwnerAction = {
  id: string;
  at: string;
  action: string;
  target: string;
  auth?: string;
  words?: string;
  harness?: string;
  binding?: "token" | "session";
  via_session?: string;
  state?: string | null;
  runs_at?: string;
  no_undo?: string;
  after?: Record<string, unknown> | null;
  facts?: Record<string, unknown> | null;
};

const HARNESS: Record<string, string> = { claude_code: "Claude Code", codex: "Codex", aegis: "Aegis" };

/** The front ends Khipu knows by name (`harness:entrypoint`). */
const FRONT_ENDS: Record<string, string> = {
  "claude_code:cli": "Claude Code, typed in a terminal or Remote Control",
  "claude_code:t3": "Claude Code in T3 Code",
  "claude_code:sdk-cli": "Claude Code jobs (claude -p)",
  "codex:codex_cli_rs": "Codex, typed in a terminal",
  "codex:codex_desktop": "The Codex app",
  "codex:codex_vscode": "Codex in VS Code",
  "codex:codex_exec": "Codex jobs (codex exec)",
  "codex:t3": "Codex in T3 Code",
  "aegis:aegis": "Aegis and Grok Build",
};

export function frontEndWords(key: string): string {
  if (FRONT_ENDS[key]) return FRONT_ENDS[key];
  const [harness, entry] = key.split(":");
  return `${HARNESS[harness] ?? harness}, ${entry}`;
}

/** An account key (`harness|login folder`) in words: "Codex", "Claude (claude-second)". */
function accountWords(key: string): string {
  const [harness, home] = key.split("|");
  const name = harness ? harness[0].toUpperCase() + harness.slice(1) : key;
  if (!home) return name;
  const folder = home.split("/").filter(Boolean).pop() ?? home;
  return `${name} (${folder.replace(/^\./, "")})`;
}

const SETTING_WORDS: Record<string, string> = {
  latest_window_h: "the latest-turn window",
  earlier_window_min: "the earlier-turn window",
  session_binding: "session-only binding",
};

/** What was done, in words. */
export function actionWords(a: OwnerAction): string {
  const t = a.target;
  const n = a.after && typeof a.after.n === "number" ? String(a.after.n) : t;
  const row = t.includes("#") ? t.split("#")[1] : t;
  switch (a.action) {
    case "route.pause":
      return `Paused ${accountWords(t)}`;
    case "route.lift":
      return `Lifted the pause on ${accountWords(t)}`;
    case "route.clear":
      return a.facts && typeof a.facts.report === "string"
        ? `Dismissed a report on ${accountWords(t)}`
        : `Cleared the report on ${accountWords(t)}`;
    case "route.note.set":
      return "Set the note";
    case "route.note.end":
      return "Ended the note";
    case "route.rule.add":
      return `Added standing rule ${n}`;
    case "route.rule.edit":
      return `Changed standing rule ${t}`;
    case "route.rule.retire":
      return `Retired standing rule ${t}`;
    case "route.rule.restore":
      return `Restored standing rule ${t}`;
    case "route.rule.confirm":
      return `Confirmed standing rule ${t}`;
    case "route.rule.undo":
      return `Undid the change to standing rule ${t}`;
    case "route.rule.dismiss":
      return `Dismissed standing rule ${t}`;
    case "route.hold.add":
      return "Added a hold";
    case "route.hold.end":
      return "Ended a hold";
    case "route.priority.set":
      return "Set the priority";
    case "route.priority.confirm":
      return "Confirmed the priority";
    case "route.priority.undo":
      return "Undid the priority change";
    case "route.priority.clear":
      return "Cleared the priority";
    case "accounts.track":
      return "Started tracking an account";
    case "accounts.rename":
      return `Renamed ${accountWords(t)}`;
    case "accounts.stop":
      return `Stopped tracking ${accountWords(t)}`;
    case "accounts.move":
      return `Moved ${accountWords(t)} to another login folder`;
    case "accounts.decline":
      return "Stopped offering an account";
    case "accounts.offer_again":
      return "Offered an account again";
    case "accounts.auto_track":
      return `Turned automatic tracking ${t}`;
    case "accounts.request.confirm":
    case "config.answer_request.confirm":
      return "Kept a change a conversation asked for";
    case "accounts.request.undo":
    case "config.answer_request.undo":
      return "Undid a change a conversation asked for";
    case "config.usage_accounts":
      return "Changed the tracked accounts";
    case "config.routing_setting":
      return `Changed the setting ${t}`;
    case "promote.gate_ignore":
      return "Let the next update skip a check";
    case "promote.gate_ignore.cancel":
      return "Cancelled the update's skipped check";
    case "ledger.import":
      return "Imported rows into the ledger";
    case "ledger.restore":
      return `Restored ${row} in the ledger`;
    case "ledger.purge":
      return `Removed ${row} from the ledger for good`;
    case "ledger.add":
      return `Added ${row} to the ledger`;
    case "ledger.set":
      return `Changed ${row} in the ledger`;
    case "ledger.remove":
      return `Removed ${row} from the ledger`;
    case "work.hide":
      return `Hid commitment ${t}`;
    case "work.unhide":
      return `Showed commitment ${t} again`;
    case "work.same":
      return "Marked two commitments as the same";
    case "work.not_same":
      return "Marked two commitments as different";
    case "direction.off":
      return "Turned owner direction off";
    case "direction.set": {
      if (t.startsWith("trust ")) return `Trusted ${frontEndWords(t.slice(6))}`;
      if (t.startsWith("untrust ")) return `Stopped trusting ${frontEndWords(t.slice(8))}`;
      return `Changed ${SETTING_WORDS[t] ?? t}`;
    }
    default:
      return a.action;
  }
}

function when(iso: string | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
}

/** Which kind of conversation, in the harness's words only. */
function conversationWords(a: OwnerAction): string {
  const h = a.harness ? HARNESS[a.harness] : undefined;
  const base = h ? `${/^[AEIO]/.test(h) ? "an" : "a"} ${h} conversation` : "a conversation";
  return a.via_session ? `a job started from ${base}` : base;
}

function stateTag(a: OwnerAction) {
  // Failed before no undo: a change that did not happen has nothing to undo either way.
  if (a.state === "failed") return <Tag tone="neutral">Did not happen</Tag>;
  if (a.no_undo) return <Tag tone="neutral">No undo</Tag>;
  switch (a.state) {
    case "unconfirmed":
      return (
        <Tag tone="warn" dot title="Khipu wrote this change's Undo before making it, but not that it finished. Check the account, and undo it if it is not what you want.">
          Unconfirmed
        </Tag>
      );
    case "undone":
      return <Tag tone="neutral">Undone</Tag>;
    case "cancelled":
      return <Tag tone="neutral">Purge cancelled</Tag>;
    case "scheduled":
      return (
        <Tag tone="warn" dot>
          Purge scheduled for {when(a.runs_at)}
        </Tag>
      );
    default:
      return (
        <Tag tone="ok" dot>
          Done
        </Tag>
      );
  }
}

function canUndo(a: OwnerAction): boolean {
  return (
    !a.no_undo &&
    (a.state === "applied" || a.state === "scheduled" || a.state === "unconfirmed" || a.state == null)
  );
}

/** The log's hash chain (`owner-direction actions --json`, `chain`). */
export type ChainState = {
  ok: boolean;
  lines: number;
  first_bad: { line: number; id?: string | null; kind?: string | null; why: string } | null;
  note?: string;
};

type ActionsShape = { ok?: boolean; actions: OwnerAction[]; chain?: ChainState };

/** How the change was authorised, in words. */
function sourceWords(a: OwnerAction): string {
  switch (a.auth) {
    case "direction":
      return `You said this in ${conversationWords(a)}${a.binding === "session" ? "; checked by conversation only" : ""}.`;
    case "app":
      return "Done in the Khipu app.";
    case "terminal":
      return "Done on a terminal, with a typed yes.";
    default:
      return "How this was authorised is not recorded.";
  }
}

/** The broken chain's banner text: which entry, and what that means. */
export function chainWords(chain: ChainState | undefined): string | null {
  const bad = chain && !chain.ok ? chain.first_bad : null;
  if (!bad) return null;
  const which = `line ${bad.line}${bad.id ? ` (${bad.id})` : ""}`;
  return (
    `The owner-actions log was changed outside Khipu at ${which}: ${bad.why}. ` +
    "That entry and the ones after it may not be what Khipu wrote, so check them before you rely on them."
  );
}

export function OwnerActions({ active }: { active: boolean }) {
  const show = useCliState<OwnerDirectionShow>("khipu_owner_direction_show", active);
  const log = useCliState<ActionsShape>("khipu_owner_direction_actions", active);
  const [busy, setBusy] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});

  async function write(id: string, command: string, args: Record<string, unknown>): Promise<string | null> {
    setBusy(id);
    const res = await callCli(command, args);
    await Promise.all([show.reload(), log.reload()]);
    setErrors((prev) => {
      const out = { ...prev };
      if (res.ok) delete out[id];
      else out[id] = res.error;
      return out;
    });
    setBusy(null);
    return res.ok ? null : res.error;
  }

  const conf = show.data?.settings;
  const rows = log.data?.actions ?? [];
  const broken = chainWords(log.data?.chain);
  const seen = Object.entries(show.data?.turns ?? {}).flatMap(([h, row]) =>
    Object.keys(row.entrypoints ?? {}).map((e) => `${h}:${e}`),
  );
  const untrusted = seen.filter((k) => conf && !conf.trusted.includes(k));

  function sw(id: string, label: string, what: string, on: boolean, flip: (next: boolean) => void) {
    return (
      <div className="opt-row">
        <input
          id={id}
          className="switch"
          type="checkbox"
          role="switch"
          checked={on}
          disabled={!conf || busy !== null}
          aria-describedby={`${id}-what`}
          onChange={(e) => flip(e.target.checked)}
        />
        <div className="opt-text">
          <div className="opt-title">
            <label className="opt-name" htmlFor={id}>
              {label}
            </label>
            {busy === id ? <Loader2 size={14} className="spin" aria-hidden /> : null}
          </div>
          <p id={`${id}-what`} className="opt-what">
            {what}
          </p>
          {errors[id] ? (
            <p className="set-error" role="alert">
              {errors[id]}
            </p>
          ) : null}
        </div>
      </div>
    );
  }

  return (
    <>
      <p className="set-help set-intro">
        A conversation can make an owner's change when you asked for it in your own typed words. Every owner change,
        made that way, in the app or on a terminal, is listed here, and you can undo it.
      </p>
      {show.error && !show.data ? (
        <p className="set-error" role="alert">
          Could not read owner direction: {show.error}
        </p>
      ) : null}

      <div className="section-card">
        <div className="section-head">Every owner action</div>
        <div className="section-body">
          {broken ? (
            <p className="set-error" role="alert">
              {broken}
            </p>
          ) : null}
          {log.error && !log.data ? (
            <p className="set-error" role="alert">
              Could not read the list: {log.error}
            </p>
          ) : null}
          {log.data && !rows.length ? (
            <p className="set-help">Nothing yet. Each owner change shows here once it is made.</p>
          ) : null}
          {rows.length ? (
            <div className="rows" role="list" aria-label="Every owner action, newest first">
              {rows.map((a) => {
                const id = `undo:${a.id}`;
                const what = actionWords(a);
                return (
                  <div key={a.id} className="row-item route-acct owner-act" role="listitem">
                    <span className="route-name">{when(a.at)}</span>
                    <span className="route-what">
                      <span className="route-state">
                        <span>{what}</span>
                        {stateTag(a)}
                      </span>
                      {a.words ? <blockquote className="route-quote">“{a.words}”</blockquote> : null}
                      <span className="route-src">{sourceWords(a)}</span>
                      {a.no_undo ? <span className="route-src">{a.no_undo}</span> : null}
                    </span>
                    <span className="route-acts">
                      {canUndo(a) ? (
                        <button
                          type="button"
                          className="sm"
                          aria-label={`Undo: ${what}`}
                          disabled={busy !== null}
                          onClick={() => void write(id, "khipu_owner_direction_undo", { id: a.id })}
                        >
                          {busy === id ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                          {a.state === "scheduled" ? "Cancel purge" : "Undo"}
                        </button>
                      ) : null}
                    </span>
                    {errors[id] ? (
                      <div className="route-pause">
                        <p className="set-error" role="alert">
                          {errors[id]}
                        </p>
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          ) : null}
        </div>
      </div>

      <div className="section-card">
        <div className="section-head">The check</div>
        <div className="opt-list">
          {sw(
            "od-enabled",
            "Owner direction",
            "Conversations may make an owner's change when your own typed words in that conversation ask for it. Off: only this app, or a yes typed in a terminal, can.",
            conf?.enabled === true,
            (next) => void write("od-enabled", "khipu_owner_direction_switch", { on: next }),
          )}
          {sw(
            "od-session",
            "Check by conversation only where there is no turn token",
            "Aegis and Grok Build can't carry a turn token, so their words are matched by conversation alone. Off: those conversations can't make owner's changes.",
            conf?.session_binding === true,
            (next) =>
              void write("od-session", "khipu_owner_direction_set", {
                key: "session_binding",
                value: next ? "on" : "off",
              }),
          )}
          <div className="opt-row">
            <div className="opt-text">
              <SettingField
                label="Your latest message counts for (hours)"
                narrow
                inputMode="decimal"
                value={conf ? String(conf.latest_window_h) : ""}
                help="How long your newest message in a conversation can still be acted on. The default is 6."
                onSave={(v) => write("od-latest", "khipu_owner_direction_set", { key: "latest_window_h", value: v.trim() })}
              />
              <SettingField
                label="An earlier message counts for (minutes)"
                narrow
                inputMode="decimal"
                value={conf ? String(conf.earlier_window_min) : ""}
                help="How long a message you have since followed with another can still be acted on. The default is 30."
                onSave={(v) =>
                  write("od-earlier", "khipu_owner_direction_set", { key: "earlier_window_min", value: v.trim() })
                }
              />
            </div>
          </div>
        </div>
      </div>

      <div className="section-card">
        <div className="section-head">Front ends that count as you</div>
        <div className="section-body">
          <p className="set-help">
            Words typed in these count as yours. Jobs a conversation starts never count.
          </p>
          {conf ? (
            <div className="rows" role="list" aria-label="Front ends that count as you">
              {conf.trusted.map((k) => {
                const id = `untrust:${k}`;
                return (
                  <div key={k} className="row-item" role="listitem">
                    <span className="row-main">{frontEndWords(k)}</span>
                    <button
                      type="button"
                      className="sm"
                      aria-label={`Remove ${frontEndWords(k)}`}
                      disabled={busy !== null}
                      onClick={() => void write(id, "khipu_owner_direction_set", { key: "untrust", value: k })}
                    >
                      {busy === id ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                      Remove
                    </button>
                  </div>
                );
              })}
            </div>
          ) : null}
          {untrusted.length ? (
            <>
              <h3 className="sub-title">Seen in the last day, not counted</h3>
              <div className="rows" role="list" aria-label="Front ends seen in the last day">
                {untrusted.map((k) => {
                  const id = `trust:${k}`;
                  return (
                    <div key={k} className="row-item" role="listitem">
                      <span className="row-main">{frontEndWords(k)}</span>
                      <button
                        type="button"
                        className="sm"
                        aria-label={`Count ${frontEndWords(k)} as you`}
                        disabled={busy !== null}
                        onClick={() => void write(id, "khipu_owner_direction_set", { key: "trust", value: k })}
                      >
                        {busy === id ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                        Count as you
                      </button>
                    </div>
                  );
                })}
              </div>
            </>
          ) : null}
          {Object.entries(errors)
            .filter(([k]) => k.startsWith("trust:") || k.startsWith("untrust:"))
            .map(([k, e]) => (
              <p key={k} className="set-error" role="alert">
                {e}
              </p>
            ))}
        </div>
      </div>
    </>
  );
}
