/* Settings -> Advanced -> Health checks (docs/plans/2026-10-16-offline-and-self-healing.md,
   slice 1): how far back doctor's rate checks look. A past outage ages out of
   the window by itself, so a red light from an hour the network was down
   clears without anyone doing anything. Read from `khipu config`, written
   through `khipu_config_set` / `khipu_config_unset`, then re-read.
   "Update anyway" is one-time (slice 7, decision 3): the next update skips the
   red checks that held the last one back, once, then it clears itself. */

import { useState } from "react";
import { healthCheckLabel } from "./healthRows";
import { SettingField } from "./SettingsControls";
import { callCli, useCliState } from "./settingsCli";
import type { ConfigShape } from "./settingsCli";

const WINDOW_KEY = "doctor_rate_window_minutes";
const WAIT_KEY = "promote_hub_wait_minutes";
const IGNORE_KEY = "promote_gate_ignore";

/** Client-side check for immediate feedback; the Tauri command and the CLI
 *  check the same range again. */
export function windowMinutesError(raw: string, min: number, max: number): string | null {
  const v = raw.trim();
  if (!/^\d+$/.test(v)) return "Enter a whole number of minutes.";
  const n = Number(v);
  if (n < min || n > max) return `Enter between ${min} and ${max} minutes.`;
  return null;
}

/** The time a one-time override lapses, in this Mac's clock. */
export function lapseTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
}

export function HealthChecksCard({ active }: { active: boolean }) {
  const cfg = useCliState<ConfigShape>("khipu_config_show", active);
  const row = cfg.data?.minute_settings?.[WINDOW_KEY];
  const min = row?.min ?? 5;
  const max = row?.max ?? 10080;
  const wait = cfg.data?.minute_settings?.[WAIT_KEY];
  const ignore = cfg.data?.promote_gate_ignore;
  const pending = ignore?.value ?? [];
  const blocked = ignore?.blocked_by ?? [];
  const [ignoreError, setIgnoreError] = useState<string | null>(null);
  const readFailed = cfg.error != null && !cfg.data;

  return (
    <div className="section-card">
      <div className="section-head">Health checks</div>
      <div className="section-body">
        {readFailed ? (
          <p className="set-error" role="alert">
            Could not read the settings: {cfg.error}
          </p>
        ) : null}
        <SettingField
          label="How far back rate checks look"
          mono
          narrow
          inputMode="decimal"
          value={row ? String(row.value) : ""}
          help={
            <>
              In minutes. The checks for recall before each prompt and for search falling back to keyword-only judge
              only this window, so a red light from a past network outage clears by itself. Calls made while the
              database was out of reach are never counted as failures.
              {row ? ` Default ${row.default}.` : ""}
            </>
          }
          onSave={async (v) => {
            const bad = windowMinutesError(v, min, max);
            if (bad) return bad;
            const res = await callCli("khipu_config_set", { key: WINDOW_KEY, value: v.trim() });
            await cfg.reload();
            return res.ok ? null : res.error;
          }}
          canReset={row?.source === "file"}
          resetLabel="Use default"
          onReset={async () => {
            const res = await callCli("khipu_config_unset", { key: WINDOW_KEY });
            await cfg.reload();
            return res.ok ? null : res.error;
          }}
        />
      </div>
      <div className="section-head">Before a new version of Khipu goes live</div>
      <div className="section-body">
        <SettingField
          label="How long to wait for the database"
          mono
          narrow
          inputMode="decimal"
          value={wait ? String(wait.value) : ""}
          help={
            <>
              In minutes. If the database is out of reach while a new version is checked, Khipu waits this long, then
              checks again by itself once the database answers.
              {wait ? ` Default ${wait.default}.` : ""}
            </>
          }
          onSave={async (v) => {
            const bad = windowMinutesError(v, wait?.min ?? 0, wait?.max ?? 240);
            if (bad) return bad;
            const res = await callCli("khipu_config_set", { key: WAIT_KEY, value: v.trim() });
            await cfg.reload();
            return res.ok ? null : res.error;
          }}
          canReset={wait?.source === "file"}
          resetLabel="Use default"
          onReset={async () => {
            const res = await callCli("khipu_config_unset", { key: WAIT_KEY });
            await cfg.reload();
            return res.ok ? null : res.error;
          }}
        />
        {ignore ? (
          <div className="set-field">
            <div className="set-label">Update anyway</div>
            {pending.length > 0 ? (
              <>
                <p className="set-help">
                  The next update skips these checks once: {pending.map(healthCheckLabel).join(", ")}. It is named in
                  that update's record, then cleared.
                  {ignore.expires_at ? ` If no update runs, it lapses at ${lapseTime(ignore.expires_at)}.` : ""}
                </p>
                <button
                  type="button"
                  className="btn"
                  onClick={async () => {
                    const res = await callCli("khipu_config_unset", { key: IGNORE_KEY });
                    await cfg.reload();
                    setIgnoreError(res.ok ? null : res.error);
                  }}
                >
                  Cancel
                </button>
              </>
            ) : blocked.length > 0 ? (
              <>
                <p className="set-help">
                  The last update was held back by: {blocked.map(healthCheckLabel).join(", ")}. If you know these are
                  harmless, the next update can skip them once. Later updates check them again.
                </p>
                <button
                  type="button"
                  className="btn"
                  onClick={async () => {
                    const res = await callCli("khipu_config_set", { key: IGNORE_KEY, value: blocked.join(",") });
                    await cfg.reload();
                    setIgnoreError(res.ok ? null : res.error);
                  }}
                >
                  Update anyway
                </button>
              </>
            ) : (
              <p className="set-help">No health check is holding an update back.</p>
            )}
            {ignoreError ? (
              <p className="set-error" role="alert">
                {ignoreError}
              </p>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
