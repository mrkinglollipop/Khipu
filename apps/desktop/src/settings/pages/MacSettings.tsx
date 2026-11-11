// Settings › Your Macs, the parts that change something (slice A11):
// - the name of each Mac (`khipu_config_set` with `routing_mac_names`, the
//   key `khipu config` keeps the owner's Mac names under);
// - the settings the two Macs disagree on, and the changes another Mac holds
//   for an answer, moved here unchanged from the older routing screen's
//   Your Macs page (same commands and arguments: `khipu_config_set` with the
//   chosen value, `khipu_owner_answer` with the request id);
// - the switch that keeps the shared settings the same on both Macs
//   (`routing_replication_live`, as before).
import { useId, useRef, useState } from "react";
import { callCli, useCliState } from "../../settingsCli";
import { whenLocal } from "../../routing";
import { durationWords } from "../../routingWords";
import type { MacRow } from "../data";
import { macName } from "../data";

type Setting = { value?: unknown; kind?: string; pending?: boolean };
type SyncDifference = { key: string; name: string; local: unknown; remote: unknown };
type SyncRequest = {
  id?: string; request: string; mac: string; state?: string; words?: string | null; stale?: boolean;
  ask?: string | null; answerable?: boolean; decline_only?: boolean; reason?: string | null;
  asked_by?: string | null; asked_at?: string | null;
};
export type MacConfig = {
  routing?: Record<string, Setting>;
  settings_sync?: { enabled?: boolean; differences?: SyncDifference[]; requests?: SyncRequest[]; waiting?: SyncRequest[] };
};

const MAC_ID = /^[0-9a-f]{32}$/;
const NAMES_KEY = "routing_mac_names";

/** A setting's value in words a person reads: never "[object Object]". */
function valueWords(value: unknown, kind?: string): string {
  if (kind === "duration" && typeof value === "number") return durationWords(value);
  if (value === true) return "on";
  if (value === false) return "off";
  if (value === null || value === undefined) return "not set";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

/** A value as `khipu_config_set` takes it: a switch is the word on or off (the
 *  bridge refuses "true" and "false"); anything else is its JSON. */
function settingText(value: unknown): string {
  if (typeof value === "boolean") return value ? "on" : "off";
  return JSON.stringify(value);
}

function givenNames(cfg: MacConfig | null): Record<string, string> {
  const v = cfg?.routing?.[NAMES_KEY]?.value;
  if (!v || typeof v !== "object" || Array.isArray(v)) return {};
  const out: Record<string, string> = {};
  for (const [k, name] of Object.entries(v as Record<string, unknown>)) if (typeof name === "string") out[k] = name;
  return out;
}

/** Names two or more Macs share, compared without case or outer spaces. */
export function sharedNames(macs: MacRow[]): Array<{ name: string; macs: MacRow[] }> {
  const by = new Map<string, MacRow[]>();
  for (const m of macs) {
    const k = macName(m).trim().toLowerCase();
    by.set(k, [...(by.get(k) ?? []), m]);
  }
  return [...by.values()].filter((l) => l.length > 1).map((l) => ({ name: macName(l[0]), macs: l }));
}

/** One write at a time on this page; the screen is read again after each. */
function useWrites(after: () => Promise<unknown>) {
  const [busy, setBusy] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const run = async (id: string, command: string, args: Record<string, unknown>): Promise<boolean> => {
    if (busy) return false;
    setBusy(id);
    setErrors((e) => { const n = { ...e }; delete n[id]; return n; });
    const res = await callCli(command, args);
    if (!res.ok) setErrors((e) => ({ ...e, [id]: res.error }));
    await after();
    setBusy(null);
    return res.ok;
  };
  return { busy, errors, run };
}

export function MacSettings({ macs, active, onNamed }: { macs: MacRow[]; active: boolean; onNamed: () => Promise<unknown> | void }) {
  const cfg = useCliState<MacConfig>("khipu_config_show", active);
  const w = useWrites(async () => { await cfg.reload(); });
  const fields = useRef<Record<string, HTMLInputElement | null>>({});
  const names = givenNames(cfg.data);
  const shared = sharedNames(macs);
  const sync = cfg.data?.settings_sync;
  const differences = sync?.differences ?? [];
  const waiting = sync?.waiting ?? [];
  const requests = sync?.requests ?? [];
  const copyOn = cfg.data?.routing?.routing_replication_live?.value === true;
  const copyId = useId();

  const saveName = async (m: MacRow, typed: string) => {
    const next = { ...names };
    const name = typed.trim();
    if (name) next[m.mac] = name;
    else delete next[m.mac];
    const ok = await w.run(`name:${m.mac}`, "khipu_config_set", { key: NAMES_KEY, value: JSON.stringify(next) });
    if (ok) await onNamed();
  };

  return (
    <>
      {shared.map((s) => {
        const target = s.macs.find((m) => !m.this_mac && MAC_ID.test(m.mac)) ?? s.macs.find((m) => MAC_ID.test(m.mac));
        return (
          <div className="st-alert" role="alert" key={s.name}>
            <p>
              {s.macs.length === 2 ? "Two" : s.macs.length} of your Macs are called “{s.name}”, so you can’t tell them apart here
              or in what your agents say. Give one of them its own name below.
            </p>
            {target ? (
              <button type="button" className="link" onClick={() => fields.current[target.mac]?.focus()}>
                Name {target.this_mac ? "this Mac" : "the other Mac"}
              </button>
            ) : null}
          </div>
        );
      })}

      <section className="st-group" aria-labelledby="st-names-h">
        <h3 id="st-names-h">Names</h3>
        <p className="st-muted">
          The name you give each Mac shows in Khipu and in what your agents tell you. Each Mac shares the
          name it has for itself. So give each Mac its name on that Mac too.
        </p>
        {cfg.error && !cfg.data ? <p className="st-err" role="alert">Khipu couldn’t read the names: {cfg.error}</p> : null}
        {macs.filter((m) => MAC_ID.test(m.mac)).map((m) => (
          <NameRow key={m.mac} m={m} given={names[m.mac] ?? ""} disabled={!cfg.data || w.busy != null}
            busy={w.busy === `name:${m.mac}`} error={w.errors[`name:${m.mac}`]}
            inputRef={(el) => { fields.current[m.mac] = el; }} onSave={(t) => void saveName(m, t)} />
        ))}
      </section>

      <section className="st-group" aria-labelledby="st-copy-h">
        <h3 id="st-copy-h">Keep settings the same on both Macs</h3>
        <label className="st-switch" htmlFor={copyId}>
          <input id={copyId} type="checkbox" checked={copyOn} disabled={!cfg.data || w.busy != null}
            onChange={(e) => void w.run("copy", "khipu_config_set", { key: "routing_replication_live", value: e.target.checked ? "on" : "off" })} />
          Copy your account order, rules and shared settings between your Macs
        </label>
        <p className="st-muted">
          {sync?.enabled !== true
            ? "Not shared now: each Mac keeps its own settings."
            : differences.length
              ? `${differences.length} setting${differences.length === 1 ? " differs" : "s differ"} between your Macs. Nothing changes until you choose.`
              : "Settled: shared settings are the same on this Mac and your other Mac."}
        </p>
        {w.errors.copy ? <p className="st-err" role="alert">{w.errors.copy}</p> : null}
      </section>

      {differences.length ? (
        <section className="st-group" aria-labelledby="st-diff-h">
          <h3 id="st-diff-h">Settings your Macs disagree on</h3>
          <p className="st-muted">Nothing was overwritten. Choose one value for each.</p>
          <div className="st-kvs">
            {differences.map((d) => {
              const kind = cfg.data?.routing?.[d.key]?.kind;
              return (
                <div className="st-kv" key={d.key} role="group" aria-label={d.name}>
                  <div className="st-k">{d.name}</div>
                  <div className="st-v">This Mac: <b>{valueWords(d.local, kind)}</b> · Other Mac: <b>{valueWords(d.remote, kind)}</b></div>
                  <div className="st-actions">
                    <button type="button" className="sm" disabled={w.busy != null}
                      onClick={() => void w.run(`sync-local-${d.key}`, "khipu_config_set", { key: d.key, value: settingText(d.local) })}>
                      Keep {valueWords(d.local, kind)}
                    </button>
                    <button type="button" className="sm" disabled={w.busy != null}
                      onClick={() => void w.run(`sync-remote-${d.key}`, "khipu_config_set", { key: d.key, value: settingText(d.remote) })}>
                      Use {valueWords(d.remote, kind)}
                    </button>
                  </div>
                  {w.errors[`sync-local-${d.key}`] || w.errors[`sync-remote-${d.key}`] ? (
                    <p className="st-err" role="alert">{w.errors[`sync-local-${d.key}`] ?? w.errors[`sync-remote-${d.key}`]}</p>
                  ) : null}
                </div>
              );
            })}
          </div>
          <div className="st-actions">
            <button type="button" disabled={w.busy != null} onClick={() => void (async () => {
              for (const d of differences) await w.run(`sync-all-${d.key}`, "khipu_config_set", { key: d.key, value: settingText(d.remote) });
            })()}>Use the other Mac’s for all</button>
          </div>
        </section>
      ) : null}

      {waiting.length ? (
        <section className="st-group" aria-labelledby="st-wait-h">
          <h3 id="st-wait-h">Waiting for your answer on another Mac</h3>
          <p className="st-muted">Your answer goes to that Mac, which makes the change within a few minutes.</p>
          <div className="st-kvs">
            {waiting.map((r) => (
              <div className="st-kv" key={r.id ?? r.request} role="group" aria-label={r.mac}>
                <div className="st-k">{r.mac}</div>
                <div className="st-v">
                  {r.asked_by ? `${r.asked_by[0].toUpperCase()}${r.asked_by.slice(1)} asked${r.asked_at ? ` ${whenLocal(r.asked_at, new Date())}` : ""}. ` : ""}
                  {r.ask ?? "Its words stay on that Mac: answer it there."}
                  {r.reason ? ` ${r.reason}` : ""}
                </div>
                {r.answerable && r.id ? (
                  <div className="st-actions">
                    <button type="button" className="sm" disabled={w.busy != null}
                      onClick={() => void w.run(`sync-no-${r.id}`, "khipu_owner_answer", { request: r.id, answer: "no" })}>No</button>
                    {r.decline_only ? null : (
                      <button type="button" className="sm" disabled={w.busy != null}
                        onClick={() => void w.run(`sync-yes-${r.id}`, "khipu_owner_answer", { request: r.id, answer: "yes" })}>Yes</button>
                    )}
                  </div>
                ) : null}
                {w.errors[`sync-no-${r.id}`] || w.errors[`sync-yes-${r.id}`] ? (
                  <p className="st-err" role="alert">{w.errors[`sync-no-${r.id}`] ?? w.errors[`sync-yes-${r.id}`]}</p>
                ) : null}
              </div>
            ))}
          </div>
        </section>
      ) : null}

      {requests.length ? (
        <section className="st-group" aria-labelledby="st-req-h">
          <h3 id="st-req-h">Answers you gave for your other Mac</h3>
          <div className="st-kvs">
            {requests.map((r) => (
              <div className="st-kv" key={r.request} data-stale={r.stale ? "true" : undefined}>
                <div className="st-k">{r.mac}</div>
                <div className="st-v">{r.words}</div>
              </div>
            ))}
          </div>
        </section>
      ) : null}
    </>
  );
}

function NameRow({ m, given, disabled, busy, error, inputRef, onSave }: {
  m: MacRow; given: string; disabled: boolean; busy: boolean; error?: string;
  inputRef: (el: HTMLInputElement | null) => void; onSave: (typed: string) => void;
}) {
  const id = useId();
  const [typed, setTyped] = useState<string | null>(null);
  const value = typed ?? given;
  const changed = value.trim() !== given.trim();
  return (
    <form className="st-name" onSubmit={(e) => { e.preventDefault(); if (changed) { onSave(value); setTyped(null); } }}>
      <label htmlFor={id} className="st-k">
        {m.this_mac ? "This Mac" : "Your other Mac"}
        <span className="st-sub"> · shown now as “{macName(m)}”</span>
      </label>
      <div className="st-actions">
        <input id={id} ref={inputRef} className="st-input" type="text" maxLength={64} value={value}
          placeholder={m.this_mac ? "For example M5" : "For example M1"} disabled={disabled}
          onChange={(e) => setTyped(e.target.value)} />
        <button type="submit" disabled={disabled || !changed} aria-busy={busy || undefined}>
          {busy ? "Saving…" : !value.trim() && given ? "Clear name" : "Save name"}
        </button>
      </div>
      {!given ? <p className="st-muted">No name given, so Khipu shows its computer name.</p> : null}
      {error ? <p className="st-err" role="alert">{error}</p> : null}
    </form>
  );
}
