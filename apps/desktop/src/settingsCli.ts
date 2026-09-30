/* Shared plumbing for the Settings screens that write CLI-owned config
   (docs/plans/2026-09-30-settings-parity.md). Every write goes through a
   fixed-argv Tauri command in src-tauri/src/lib.rs; the webview only ever
   names a command and passes a key and a value. The CLI is the source of
   truth, so a screen re-reads its state after each write instead of trusting
   what it just sent. */

import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";

export type CliResult<T> = { ok: true; data: T } | { ok: false; error: string };

function errText(e: unknown): string {
  if (typeof e === "string") return e;
  if (e instanceof Error) return e.message;
  return String(e);
}

/** The CLI reports a refused change as `{"ok": false, "error": ...}` on stdout;
 *  a failed install adds a per-job list. Surface the CLI's own words. */
function refusalText(data: Record<string, unknown>): string {
  const parts: string[] = [];
  if (typeof data.error === "string" && data.error) parts.push(data.error);
  if (Array.isArray(data.results)) {
    for (const r of data.results as Array<Record<string, unknown>>) {
      if (r && r.ok === false) {
        const why = r.error ?? r.detail ?? r.message;
        if (typeof why === "string" && why) parts.push(why);
      }
    }
  }
  return parts.join(": ") || "Khipu refused the change.";
}

export async function callCli<T = Record<string, unknown>>(
  command: string,
  args?: Record<string, unknown>,
): Promise<CliResult<T>> {
  let raw: unknown;
  try {
    raw = await invoke<string>(command, args);
  } catch (e) {
    return { ok: false, error: errText(e) };
  }
  if (typeof raw !== "string") return { ok: false, error: "Khipu did not answer." };
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return { ok: false, error: raw.trim().slice(0, 300) || "Khipu gave an unreadable answer." };
  }
  if (data && typeof data === "object" && (data as { ok?: unknown }).ok === false) {
    return { ok: false, error: refusalText(data as Record<string, unknown>) };
  }
  return { ok: true, data: data as T };
}

/** Load one read-only command's JSON while `active`, and expose `reload` for
 *  the re-read after a write. */
export function useCliState<T>(command: string, active: boolean) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const reload = useCallback(async (): Promise<T | null> => {
    setLoading(true);
    const res = await callCli<T>(command);
    setLoading(false);
    if (res.ok) {
      setData(res.data);
      setError(null);
      return res.data;
    }
    setError(res.error);
    return null;
  }, [command]);

  useEffect(() => {
    if (active) void reload();
  }, [active, reload]);

  return { data, error, loading, reload };
}

export type ConfigShape = {
  capture_mode?: string;
  capture_mode_source?: string;
  gateway_url?: string | null;
  paths?: Record<string, { value: string | null; source: string; exists: boolean }>;
  user_aliases?: string[];
  float_settings?: Record<string, { value: number; source: string; default: number }>;
  relevance_cosine_floor?: { value: number; source: string; default: number };
  config?: Record<string, unknown>;
};

export type FeatureState = { enabled: boolean; source: "env" | "file" | "default" };
export type FeaturesShape = { features: Record<string, FeatureState>; unknown?: string[] };

export type JobShape = {
  plist_loaded?: boolean | null;
  next_schedule?: string | null;
  last_run_iso?: string | null;
  last_exit?: number | null;
  on_demand?: boolean;
};
export type JobsShape = Record<string, JobShape>;
