// The answer `khipu_health` gives (slice B1) and how Today reads it.
// The check can take many seconds, so the last answer is kept and shown at
// once with its time; a refresh runs in the background and never blocks.
import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";

export type HealthState = "ok" | "warn" | "err" | "unknown";

export type HealthArea = {
  key: string;
  name: string;
  state: HealthState;
  line: string;
  khipu_doing: string;
  you_do: string;
  link: string;
};

export type Health = {
  checked_at: string;
  mac: string;
  headline: string;
  areas: HealthArea[];
  jobs_running: number;
  silent_runners: Array<{ name: string; mac: string; since: string }>;
  free_gb: number | null;
  last_backup_at: string | null;
  agent_note: string;
};

const STATES: HealthState[] = ["ok", "warn", "err", "unknown"];
const STORE_KEY = "khipu.today.health.v1";
const REFRESH_MS = 5 * 60 * 1000;

/** Reads the CLI's JSON; null when it is not a health answer. An area with a
 *  state we do not know counts as "unknown", never as fine. */
export function parseHealth(text: string): Health | null {
  try {
    const v = JSON.parse(text) as Partial<Health>;
    if (!v || typeof v !== "object" || !Array.isArray(v.areas) || typeof v.checked_at !== "string") return null;
    const areas = v.areas.map((a) => ({
      key: String(a?.key ?? ""),
      name: String(a?.name ?? ""),
      state: STATES.includes(a?.state as HealthState) ? (a.state as HealthState) : "unknown",
      line: String(a?.line ?? ""),
      khipu_doing: String(a?.khipu_doing ?? ""),
      you_do: String(a?.you_do ?? ""),
      link: String(a?.link ?? ""),
    }));
    return {
      checked_at: v.checked_at,
      mac: typeof v.mac === "string" ? v.mac : "",
      headline: typeof v.headline === "string" ? v.headline : "",
      areas,
      jobs_running: typeof v.jobs_running === "number" ? v.jobs_running : 0,
      silent_runners: Array.isArray(v.silent_runners) ? v.silent_runners : [],
      free_gb: typeof v.free_gb === "number" ? v.free_gb : null,
      last_backup_at: typeof v.last_backup_at === "string" ? v.last_backup_at : null,
      agent_note: typeof v.agent_note === "string" ? v.agent_note : "",
    };
  } catch {
    return null;
  }
}

function readStored(): Health | null {
  try {
    const raw = window.localStorage.getItem(STORE_KEY);
    return raw ? parseHealth(raw) : null;
  } catch {
    return null;
  }
}

function writeStored(text: string): void {
  try {
    window.localStorage.setItem(STORE_KEY, text);
  } catch {
    /* no storage: the answer just is not kept between visits */
  }
}

/** The one sentence on Today, built from the five areas. */
export type Sentence = {
  tone: "ok" | "warn" | "err" | "unknown";
  text: string;
  sub: string;
  /** Number of areas that need a look (warn or err). */
  attention: number;
  /** Names of areas we could not check. */
  unchecked: string[];
};

export function sentenceFor(h: Health | null): Sentence {
  if (!h || h.areas.length === 0) {
    return { tone: "unknown", text: "Checking how Khipu is doing.", sub: "The first check takes a moment.", attention: 0, unchecked: [] };
  }
  const bad = h.areas.filter((a) => a.state === "err" || a.state === "warn");
  const unknown = h.areas.filter((a) => a.state === "unknown");
  const worst = bad.find((a) => a.state === "err") ?? bad[0];
  if (worst) {
    const you = worst.you_do.trim();
    const nothing = !you || /^nothing\b/i.test(you);
    const doing = worst.khipu_doing.trim();
    const sub = [doing, nothing ? "Nothing for you to do." : `For you: ${you}`].filter(Boolean).join(" ");
    return { tone: worst.state === "err" ? "err" : "warn", text: worst.line, sub, attention: bad.length, unchecked: unknown.map((a) => a.name) };
  }
  if (unknown.length === h.areas.length) {
    return { tone: "unknown", text: "Couldn't check how Khipu is doing.", sub: "Khipu will try again on its own.", attention: 0, unchecked: unknown.map((a) => a.name) };
  }
  if (unknown.length > 0) {
    return {
      tone: "unknown",
      text: "Everything we could check is working.",
      sub: `Couldn't check: ${unknown.map((a) => a.name).join(", ")}.`,
      attention: 0,
      unchecked: unknown.map((a) => a.name),
    };
  }
  return { tone: "ok", text: "Everything is working.", sub: "", attention: 0, unchecked: [] };
}

export type HealthHook = {
  health: Health | null;
  /** A check is running now (the window is never blocked by it). */
  checking: boolean;
  /** The last try failed; the last answer, if any, stays on screen. */
  failed: boolean;
  /** When this Mac last got an answer (ms), for "Checked N minutes ago". */
  answeredAt: number | null;
  refresh: () => void;
};

/** The last answer at once, a refresh in the background on open and every
 *  five minutes while Today is on screen. A failure or a slow answer keeps
 *  what is shown; it is never an error screen. */
export function useHealth(active: boolean): HealthHook {
  const [health, setHealth] = useState<Health | null>(() => readStored());
  const [checking, setChecking] = useState(false);
  const [failed, setFailed] = useState(false);
  const [answeredAt, setAnsweredAt] = useState<number | null>(() => {
    const h = readStored();
    const t = h ? Date.parse(h.checked_at) : NaN;
    return Number.isNaN(t) ? null : t;
  });
  const running = useRef(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  const refresh = useCallback(() => {
    if (running.current) return;
    running.current = true;
    setChecking(true);
    void (async () => {
      try {
        const text = await invoke<string>("khipu_health");
        const parsed = parseHealth(text);
        if (!alive.current) return;
        if (parsed) {
          writeStored(text);
          setHealth(parsed);
          const t = Date.parse(parsed.checked_at);
          setAnsweredAt(Number.isNaN(t) ? Date.now() : t);
          setFailed(false);
        } else {
          setFailed(true);
        }
      } catch {
        if (alive.current) setFailed(true);
      } finally {
        running.current = false;
        if (alive.current) setChecking(false);
      }
    })();
  }, []);

  useEffect(() => {
    if (!active) return;
    refresh();
    const id = window.setInterval(refresh, REFRESH_MS);
    return () => window.clearInterval(id);
  }, [active, refresh]);

  return { health, checking, failed, answeredAt, refresh };
}

/** "2 minutes ago", "just now", "3 hours ago" for a time in ms. */
export function agoWords(thenMs: number | null, nowMs: number): string | null {
  if (thenMs == null) return null;
  const s = Math.max(0, Math.round((nowMs - thenMs) / 1000));
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return m === 1 ? "1 minute ago" : `${m} minutes ago`;
  const h = Math.round(m / 60);
  if (h < 48) return h === 1 ? "1 hour ago" : `${h} hours ago`;
  const d = Math.round(h / 24);
  return `${d} days ago`;
}
