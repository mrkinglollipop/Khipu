// "How Khipu is doing": the five areas, docked at the right of Today.
import { useState } from "react";
import { Inspector } from "../shell";
import { agoWords, type Health, type HealthArea, type HealthHook } from "./health";

const STATE_WORD: Record<HealthArea["state"], string> = {
  ok: "Working", warn: "Needs a look", err: "Needs attention", unknown: "Couldn’t check",
};

/** Where an area's link goes. Running now is in Accounts. */
export const LINK_LABEL: Record<string, string> = {
  "running-now": "Open Running now",
  accounts: "Open Accounts",
  search: "Open Memory",
  backups: "Open Settings",
  "where-memory-is-kept": "Open Settings",
};

function clock(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
}

function extra(area: HealthArea, h: Health): string[] {
  if (area.key === "database") {
    const out: string[] = [];
    if (h.free_gb != null) out.push(`${h.free_gb} GB free on ${h.mac || "this Mac"}.`);
    out.push(h.last_backup_at ? `Last backup ${clock(h.last_backup_at)}.` : "No backup yet.");
    return out;
  }
  if (area.key === "jobs") {
    return h.silent_runners.map((r) => `${r.name} on ${r.mac || "this Mac"} stopped reporting ${clock(r.since)}.`);
  }
  return [];
}

export function HealthPanel({
  hook, now, onClose, onOpen,
}: { hook: HealthHook; now: number; onClose: () => void; onOpen: (link: string) => void }) {
  const h = hook.health;
  const [copied, setCopied] = useState(false);
  const bad = h ? h.areas.filter((a) => a.state === "warn" || a.state === "err").length : 0;
  const ago = agoWords(hook.answeredAt, now);
  const unchecked = h ? h.areas.filter((a) => a.state === "unknown").length : 0;
  const couldnt = unchecked === 0 ? "" : unchecked === 1 ? "One check couldn’t run." : `${unchecked} checks couldn’t run.`;
  const summary = !h
    ? "Khipu is running its checks."
    : [
        bad > 0 ? `${bad === 1 ? "One check needs" : `${bad} checks need`} attention. Khipu is on ${bad === 1 ? "it" : "them"}.` : unchecked === 0 ? "Nothing needs attention." : "",
        couldnt,
      ].filter(Boolean).join(" ");
  const copy = async () => {
    if (!h?.agent_note) return;
    try {
      await navigator.clipboard.writeText(h.agent_note);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2500);
    } catch {
      setCopied(false);
    }
  };
  return (
    <Inspector title="How Khipu is doing" onClose={onClose}>
      <div className="td-panel">
        <p className="td-panel-sum">
          {summary}
          {ago ? ` Checked ${ago}.` : ""}
          {hook.checking && h ? " Checking again…" : ""}
        </p>
        <ul className="td-areas">
          {(h?.areas ?? []).map((a) => (
            <li key={a.key} className="td-area" data-state={a.state}>
              <span className="td-dot" aria-hidden />
              <div>
                <h3>{a.name}<span className="td-state"> · {STATE_WORD[a.state]}</span></h3>
                <p>{a.state === "unknown" ? "Couldn’t check this just now." : a.line}</p>
                {a.state === "unknown" ? null : extra(a, h as Health).map((t) => <p key={t} className="td-more">{t}</p>)}
                {a.khipu_doing ? <p><b>Khipu:</b> {a.khipu_doing}</p> : null}
                {a.state === "warn" || a.state === "err" ? <p><b>You:</b> {a.you_do || "Nothing."}</p> : null}
                {LINK_LABEL[a.link] && (a.key === "jobs" || a.state === "warn" || a.state === "err") ? (
                  <button type="button" className="td-link" onClick={() => onOpen(a.link)}>{LINK_LABEL[a.link]}</button>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
        <div className="td-panel-foot">
          <button type="button" className="td-copy" onClick={() => void copy()} disabled={!h?.agent_note}>
            {copied ? "Copied" : "Copy a note for an agent"}
          </button>
          <p className="td-more">Plain text for an agent to read. Nothing private is in it.</p>
        </div>
      </div>
    </Inspector>
  );
}
