// What the Settings pages read from the two status commands.
//   khipu_macs   -> one entry per Mac (this one live, the other from the shared record)
//   khipu_health -> the five checks, free space, last backup, and the agent note
// Shapes are exactly what `khipu macs --json` and `khipu health --json` print.
import { useCallback, useEffect, useRef, useState } from "react";
import { callCli } from "../settingsCli";

export type AreaState = "ok" | "warn" | "err" | "unknown";

export type MacRow = {
  mac: string;
  name: string | null;
  this_mac: boolean;
  version: string | null;
  seen_at: string | null;
  age_s: number | null;
  stale: boolean;
  saving: boolean | null;
  health: {
    checked_at?: string | null;
    state?: AreaState | null;
    areas?: Array<{ key: string; state: AreaState }>;
    routing_version?: string | null;
    free_gb?: number | null;
    last_backup_at?: string | null;
  } | null;
  running: { count: number; silent: Array<{ name: string; since: string | null }> } | null;
  signins: Array<{ harness: string; label: string; signed_in: boolean }> | null;
};

export type MacsDoc = {
  checked_at?: string;
  macs: MacRow[];
  shared?: boolean;
  stale_after_s?: number;
  note?: string;
};

export type HealthArea = {
  key: string;
  name: string;
  state: AreaState;
  line: string;
  khipu_doing: string;
  you_do: string;
  link: string;
};

export type HealthDoc = {
  checked_at?: string;
  mac?: string;
  headline?: string;
  areas: HealthArea[];
  jobs_running?: number;
  free_gb?: number | null;
  last_backup_at?: string | null;
  agent_note?: string;
};

export type Loaded<T> = { data: T | null; error: string | null; loading: boolean };

/** Read both documents once the page is open. `khipu health` runs the full
 *  doctor (about a minute with a live hub), so the Macs list shows first and
 *  the health fields fill in when they arrive. */
export function useSettingsData(active: boolean) {
  const [macs, setMacs] = useState<Loaded<MacsDoc>>({ data: null, error: null, loading: false });
  const [health, setHealth] = useState<Loaded<HealthDoc>>({ data: null, error: null, loading: false });
  const seq = useRef(0);

  const reload = useCallback(() => {
    const mine = ++seq.current;
    setMacs((p) => ({ ...p, loading: true }));
    setHealth((p) => ({ ...p, loading: true }));
    void callCli<MacsDoc>("khipu_macs").then((res) => {
      if (mine !== seq.current) return;
      if (res.ok && res.data && Array.isArray(res.data.macs)) setMacs({ data: res.data, error: null, loading: false });
      else setMacs({ data: null, error: res.ok ? "Khipu gave an unreadable answer." : res.error, loading: false });
    });
    void callCli<HealthDoc>("khipu_health").then((res) => {
      if (mine !== seq.current) return;
      if (res.ok && res.data && Array.isArray(res.data.areas)) setHealth({ data: res.data, error: null, loading: false });
      else setHealth({ data: null, error: res.ok ? "Khipu gave an unreadable answer." : res.error, loading: false });
    });
  }, []);

  const started = useRef(false);
  useEffect(() => {
    if (active && !started.current) {
      started.current = true;
      reload();
    }
  }, [active, reload]);

  return { macs, health, reload };
}

export function thisMacRow(doc: MacsDoc | null): MacRow | null {
  return doc?.macs.find((m) => m.this_mac) ?? null;
}

export function macName(m: MacRow): string {
  return m.name || (m.this_mac ? "This Mac" : "Your other Mac");
}
