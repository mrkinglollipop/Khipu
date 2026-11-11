// Accounts › Running now (slice A5): the answer `khipu running --json` gives
// (slice B4) and the plain words the screen uses for it. Everything here is a
// read. The last answer is kept on this device and shown at once with its
// time; a refresh runs in the background and a slow or failed one changes
// nothing on screen.
import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";

export type JobState = "running" | "waiting" | "queued" | "unknown" | "done" | "failed" | "stopped";
const JOB_STATES: JobState[] = ["running", "waiting", "queued", "unknown", "done", "failed", "stopped"];

export type RunJob = {
  summary: string;
  now_line: string | null;
  now_at: string | null;
  state: JobState;
  quiet: boolean;
  quiet_for_s: number | null;
  account_key: string | null;
  account_label: string | null;
  model: string | null;
  started_at: string | null;
  updated_at: string | null;
  ended_at: string | null;
};

export type RunRunner = {
  name: string;
  heartbeat_at: string | null;
  silent: boolean;
  silent_since: string | null;
  last_finished_at: string | null;
  jobs: RunJob[];
};

export type RunMac = {
  mac: string;
  name: string;
  this_mac: boolean;
  as_of: string | null;
  stale: boolean;
  /** Set by `currentView`: this Mac's rows are from an earlier read, and the latest read failed. */
  old?: boolean;
  runners: RunRunner[];
};

export type Running = {
  updated_at: string | null;
  macs: RunMac[];
  note: string | null;
};

const str = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v : null);
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const rec = (v: unknown): Record<string, unknown> => (v && typeof v === "object" ? (v as Record<string, unknown>) : {});

function parseJob(v: unknown): RunJob {
  const j = rec(v);
  // A state we do not know is "unknown": never shown as running.
  const state = JOB_STATES.includes(j.state as JobState) ? (j.state as JobState) : "unknown";
  return {
    summary: str(j.summary) ?? "A job",
    now_line: str(j.now_line), now_at: str(j.now_at), state,
    quiet: j.quiet === true,
    quiet_for_s: typeof j.quiet_for_s === "number" ? j.quiet_for_s : null,
    account_key: str(j.account_key), account_label: str(j.account_label), model: str(j.model),
    started_at: str(j.started_at), updated_at: str(j.updated_at), ended_at: str(j.ended_at),
  };
}

function parseRunner(v: unknown): RunRunner {
  const r = rec(v);
  return {
    name: str(r.name) ?? "Unnamed runner",
    heartbeat_at: str(r.heartbeat_at), silent: r.silent === true,
    silent_since: str(r.silent_since), last_finished_at: str(r.last_finished_at),
    jobs: arr(r.jobs).map(parseJob),
  };
}

/** Reads the CLI's JSON; null when it is not a running answer. A Mac with no
 *  `stale: false` is treated as stale, so a missing field never reads as fresh. */
export function parseRunning(text: string): Running | null {
  try {
    const v = rec(JSON.parse(text));
    if (!Array.isArray(v.macs)) return null;
    return {
      updated_at: str(v.updated_at),
      note: str(v.note),
      macs: v.macs.map((m) => {
        const x = rec(m);
        return {
          mac: str(x.mac) ?? "", name: str(x.name) ?? "Another Mac", this_mac: x.this_mac === true,
          as_of: str(x.as_of), stale: x.stale !== false,
          runners: arr(x.runners).map(parseRunner),
        };
      }),
    };
  } catch {
    return null;
  }
}

// ---------- words ----------

const timeOf = (iso: string | null): Date | null => {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
};

/** "1:32 pm" today, "Fri 17 Oct, 6:40 pm" on another day. */
export function clockWords(iso: string | null, now: Date): string {
  const d = timeOf(iso);
  if (!d) return "a time Khipu didn’t record";
  const hm = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }).replace(/\s?([AP])M$/, (_m, c: string) => ` ${c.toLowerCase()}m`);
  if (d.toDateString() === now.toDateString()) return hm;
  const day = d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" }).replace(/,/g, "");
  return `${day}, ${hm}`;
}

export function agoWords(iso: string | null, now: Date): string | null {
  const d = timeOf(iso);
  if (!d) return null;
  const s = Math.max(0, Math.round((now.getTime() - d.getTime()) / 1000));
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return m === 1 ? "1 minute ago" : `${m} minutes ago`;
  const h = Math.round(m / 60);
  if (h < 48) return h === 1 ? "1 hour ago" : `${h} hours ago`;
  return `${Math.round(h / 24)} days ago`;
}

export type JobWords = { dot: "ok" | "warn" | "err" | "none"; word: string; when: string; unknown: boolean };

const LIVE: JobState[] = ["running", "waiting", "queued"];
export const isLive = (j: RunJob): boolean => LIVE.includes(j.state);

/** The state, in words. A job whose runner stopped reporting is "Not known",
 *  whatever the row says: Khipu cannot tell whether it is still running. */
export function jobWords(j: RunJob, runner: RunRunner, now: Date): JobWords {
  const state: JobState = runner.silent && isLive(j) ? "unknown" : j.state;
  if (state === "unknown") {
    return { dot: "none", word: "Not known", unknown: true, when: `last said ${clockWords(j.now_at ?? j.updated_at, now)}` };
  }
  if (j.quiet && isLive(j)) {
    const mins = j.quiet_for_s != null ? Math.max(1, Math.round(j.quiet_for_s / 60)) : null;
    return { dot: "warn", word: mins ? `No update for ${mins} min` : "No update for a while", unknown: false, when: `since ${clockWords(j.started_at, now)}` };
  }
  switch (state) {
    case "running": return { dot: "ok", word: "Running", unknown: false, when: `since ${clockWords(j.started_at, now)}` };
    case "waiting": return { dot: "none", word: "Waiting", unknown: false, when: `since ${clockWords(j.started_at, now)}` };
    case "queued": return { dot: "none", word: "Waiting to start", unknown: false, when: `asked for at ${clockWords(j.started_at, now)}` };
    case "failed": return { dot: "err", word: "Stopped with a problem", unknown: false, when: `at ${clockWords(j.ended_at ?? j.updated_at, now)}` };
    case "stopped": return { dot: "none", word: "Stopped", unknown: false, when: `at ${clockWords(j.ended_at ?? j.updated_at, now)}` };
    default: return { dot: "none", word: "Finished", unknown: false, when: `at ${clockWords(j.ended_at ?? j.updated_at, now)}` };
  }
}

const joinNames = (names: string[]): string =>
  names.length <= 1 ? names[0] ?? "" : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;

/** Live jobs a Mac's own, trusted runners report. A stale Mac, and a silent
 *  runner, count for nothing: Khipu can't tell. */
export function countLive(macs: RunMac[]): number {
  let n = 0;
  for (const m of macs) {
    if (m.stale) continue;
    for (const r of m.runners) if (!r.silent) n += r.jobs.filter(isLive).length;
  }
  return n;
}

/** The sentence above the tables. It never says "nothing" about a Mac it
 *  can't see, and the empty case says what is true: no runner is reporting. */
export function headline(data: Running): string {
  const old = data.macs.filter((m) => m.old);
  if (old.length) {
    const others = data.macs.filter((m) => m.stale && !m.old);
    const otherLine = others.length ? ` ${joinNames(others.map((m) => m.name))} ${others.length === 1 ? "hasn’t" : "haven’t"} reported recently either.` : "";
    return `Activity on ${joinNames(old.map((m) => m.name))} is unknown: Khipu couldn’t read what is running just now.${otherLine}`;
  }
  const fresh = data.macs.filter((m) => !m.stale);
  const stale = data.macs.filter((m) => m.stale);
  if (!data.macs.length) return "No Mac has reported yet, so Khipu can’t tell what is running.";
  const staleLine = stale.length
    ? ` ${joinNames(stale.map((m) => m.name))} ${stale.length === 1 ? "hasn’t" : "haven’t"} reported recently, so Khipu can’t tell what ${stale.length === 1 ? "it is" : "they are"} running.`
    : "";
  if (!fresh.length) return staleLine.trim();
  const withRunner = fresh.filter((m) => m.runners.length > 0);
  const noRunner = fresh.filter((m) => m.runners.length === 0);
  const noRunnerLine = noRunner.length ? `No job runner is reporting on ${joinNames(noRunner.map((m) => m.name))}.` : "";
  if (!withRunner.length) return `${noRunnerLine}${staleLine}`;
  const n = countLive(data.macs);
  // A runner that stopped reporting counts for nothing, so its Mac is never "quiet":
  // Khipu can't tell, and the headline keeps saying so, as the table does.
  const silent = withRunner.filter((m) => m.runners.some((r) => r.silent));
  const clear = withRunner.filter((m) => !silent.includes(m));
  const silentLine = silent.length
    ? `Activity on ${joinNames(silent.map((m) => m.name))} is unknown: a job runner there stopped reporting.`
    : "";
  const tail = [noRunnerLine, silentLine].filter(Boolean).map((x) => ` ${x}`).join("") + staleLine;
  if (n === 0) {
    if (!clear.length) return tail.trim();
    return `Nothing is running right now on ${joinNames(clear.map((m) => m.name))}.${tail}`;
  }
  return `${n === 1 ? "1 job" : `${n} jobs`} running or waiting on ${joinNames(withRunner.map((m) => m.name))}.${tail}`;
}

/** The answer as it can be trusted now. When the latest read failed and an
 *  earlier answer is still on screen, the rows stay, but nothing in them is
 *  current: every runner reads as not reporting, so a job is "Not known",
 *  never "Running", and no runner is "Reporting". */
export function currentView(hook: Pick<RunningHook, "data" | "readFailed">): Running | null {
  const data = hook.data;
  if (!data || !hook.readFailed) return data;
  return {
    ...data,
    macs: data.macs.map((m) => (m.stale ? m : {
      ...m, old: true,
      runners: m.runners.map((r) => ({ ...r, silent: true, silent_since: r.silent_since ?? data.updated_at ?? m.as_of })),
    })),
  };
}

// ---------- the last answer, kept on this device ----------

const STORE_KEY = "khipu.accounts.running.v1";
const REFRESH_MS = 60 * 1000;

function readStored(): { text: string; data: Running } | null {
  try {
    const text = window.localStorage.getItem(STORE_KEY);
    const data = text ? parseRunning(text) : null;
    return text && data ? { text, data } : null;
  } catch {
    return null;
  }
}
function writeStored(text: string): void {
  try { window.localStorage.setItem(STORE_KEY, text); } catch { /* storage may be blocked */ }
}

export type RunningHook = {
  data: Running | null;
  /** The last try failed and there is no earlier answer to show. */
  unread: boolean;
  /** The last try failed while an earlier answer is on screen: that answer is old. */
  readFailed?: boolean;
  /** When the answer on screen was made, in ms. */
  answeredAt: number | null;
};

/** The last answer at once, a refresh on open and every minute while `active`.
 *  A failed or slow refresh keeps what is shown and says nothing. */
export function useRunning(active: boolean): RunningHook {
  const first = useRef(readStored());
  const [data, setData] = useState<Running | null>(first.current?.data ?? null);
  const [failed, setFailed] = useState(false);
  const last = useRef<string | null>(first.current?.text ?? null);
  const busy = useRef(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  const refresh = useCallback(() => {
    if (busy.current) return;
    busy.current = true;
    void (async () => {
      try {
        const text = await invoke<string>("khipu_running");
        const parsed = parseRunning(text);
        if (!alive.current) return;
        if (!parsed) { setFailed(true); return; }
        setFailed(false);
        // Same answer: nothing re-renders, so nothing flickers.
        if (text !== last.current) {
          last.current = text;
          writeStored(text);
          setData(parsed);
        }
      } catch {
        if (alive.current) setFailed(true);
      } finally {
        busy.current = false;
      }
    })();
  }, []);

  useEffect(() => {
    if (!active) return;
    refresh();
    const id = window.setInterval(refresh, REFRESH_MS);
    return () => window.clearInterval(id);
  }, [active, refresh]);

  const t = data?.updated_at ? Date.parse(data.updated_at) : NaN;
  return { data, unread: failed && !data, readFailed: failed && !!data, answeredAt: Number.isNaN(t) ? null : t };
}
