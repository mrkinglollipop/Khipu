/* Settings -> Advanced -> Health checks (docs/plans/2026-10-16-offline-and-self-healing.md,
   slice 1): how far back doctor's rate checks look. A past outage ages out of
   the window by itself, so a red light from an hour the network was down
   clears without anyone doing anything. Read from `khipu config`, written
   through `khipu_config_set` / `khipu_config_unset`, then re-read. */

import { SettingField } from "./SettingsControls";
import { callCli, useCliState } from "./settingsCli";
import type { ConfigShape } from "./settingsCli";

const WINDOW_KEY = "doctor_rate_window_minutes";

/** Client-side check for immediate feedback; the Tauri command and the CLI
 *  check the same range again. */
export function windowMinutesError(raw: string, min: number, max: number): string | null {
  const v = raw.trim();
  if (!/^\d+$/.test(v)) return "Enter a whole number of minutes.";
  const n = Number(v);
  if (n < min || n > max) return `Enter between ${min} and ${max} minutes.`;
  return null;
}

export function HealthChecksCard({ active }: { active: boolean }) {
  const cfg = useCliState<ConfigShape>("khipu_config_show", active);
  const row = cfg.data?.minute_settings?.[WINDOW_KEY];
  const min = row?.min ?? 5;
  const max = row?.max ?? 10080;
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
    </div>
  );
}
