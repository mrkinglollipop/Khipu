// The last answer of `khipu health --json`, kept so a screen can show it at
// once and refresh in the background (the real check takes about a minute).
// Shared by Today and Settings, so both show the same answer and neither
// waits for the check. Held in memory and mirrored to localStorage so the next
// launch has something to show too; storage that is missing or refuses is
// ignored.
import { useEffect, useRef, useState } from "react";
import { callCli } from "../settingsCli";

export type CachedHealth<T> = { doc: T; at: number };

const KEY = "khipu.health.last";
let memo: CachedHealth<unknown> | null = null;
let loadedFromStorage = false;
const listeners = new Set<() => void>();

function fromStorage(): CachedHealth<unknown> | null {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return null;
    const v = JSON.parse(raw) as CachedHealth<unknown>;
    if (v && typeof v.at === "number" && v.doc && typeof v.doc === "object" && Array.isArray((v.doc as { areas?: unknown }).areas)) return v;
  } catch {
    /* ignore */
  }
  return null;
}

export function lastHealth<T = unknown>(): CachedHealth<T> | null {
  if (!memo && !loadedFromStorage) {
    loadedFromStorage = true;
    memo = fromStorage();
  }
  return memo as CachedHealth<T> | null;
}

export function saveHealth(doc: unknown, at: number = Date.now()): void {
  memo = { doc, at };
  loadedFromStorage = true;
  try {
    window.localStorage.setItem(KEY, JSON.stringify(memo));
  } catch {
    /* ignore */
  }
  listeners.forEach((l) => l());
}

/** Hear about every newly kept answer (a screen shows it at once). */
export function subscribeHealth(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

export type RefreshResult = { ok: true; doc: Record<string, unknown> & { areas: unknown[] } } | { ok: false; error: string };
let inflight: Promise<RefreshResult> | null = null;
let inflightSince = 0;
/** A check that has not answered in this long is given up on, so one that hangs
 *  can never stop every later refresh. */
const INFLIGHT_MAX_MS = 3 * 60 * 1000;

/** Ask Khipu for a new answer and keep it. Screens that ask while a check is
 *  already running share it, so the (slow) check never runs twice at once. */
export function refreshHealth(): Promise<RefreshResult> {
  if (inflight && Date.now() - inflightSince < INFLIGHT_MAX_MS) return inflight;
  const run: Promise<RefreshResult> = callCli<Record<string, unknown> & { areas: unknown[] }>("khipu_health")
    .then((res): RefreshResult => {
      if (res.ok && res.data && Array.isArray(res.data.areas)) {
        saveHealth(res.data);
        return { ok: true, doc: res.data };
      }
      return { ok: false, error: res.ok ? "Khipu gave an unreadable answer." : res.error };
    })
    .finally(() => {
      if (inflight === run) inflight = null;
    });
  inflight = run;
  inflightSince = Date.now();
  return run;
}

/** Test helper: forget the kept answer. */
export function clearHealthCache(): void {
  memo = null;
  inflight = null;
  loadedFromStorage = true;
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
}

export type HealthView<T> = {
  /** The newest answer we have, from this session or an earlier one. */
  doc: T | null;
  /** When that answer arrived (ms since epoch). */
  at: number | null;
  /** A refresh is running. */
  refreshing: boolean;
  /** Only set when there is no earlier answer to show and the read failed. */
  error: string | null;
};

/** Show the last answer at once, refresh once when `active` turns on, swap the
 *  new answer in when it arrives. A failed or slow refresh leaves the old
 *  answer and says nothing. */
export function useHealth<T extends { areas: unknown[] }>(active: boolean): HealthView<T> {
  const [cached, setCached] = useState<CachedHealth<T> | null>(() => lastHealth<T>());
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const started = useRef(false);

  useEffect(() => {
    const on = () => setCached(lastHealth<T>());
    listeners.add(on);
    return () => {
      listeners.delete(on);
    };
  }, []);

  useEffect(() => {
    if (!active || started.current) return;
    started.current = true;
    let live = true;
    setRefreshing(true);
    void refreshHealth().then((res) => {
      if (!live) return;
      setRefreshing(false);
      setError(res.ok ? null : res.error);
    });
    return () => {
      live = false;
    };
  }, [active]);

  return { doc: cached?.doc ?? null, at: cached?.at ?? null, refreshing, error: cached ? null : error };
}
