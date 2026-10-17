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
import { LEVEL_WORDS, type Level } from "./StorageReportDialog";

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

type StorageSetting = { key: string; label: string; unit: string; max: number; whole: boolean; help: string };

/** The seven storage settings (slice 8), in the order the card shows them.
 *  Limits match the Tauri command's; the CLI checks again. */
export const STORAGE_SETTINGS: StorageSetting[] = [
  {
    key: "disk_warn_free_gb",
    label: "Warn when free space falls below (GB)",
    unit: "GB",
    max: 1_000_000,
    whole: false,
    help: "Doctor shows a warning when either the data folder's disk or the temporary-files disk drops under this much, or under the percent below, whichever bites first.",
  },
  {
    key: "disk_warn_free_percent",
    label: "Warn when free space falls below (percent)",
    unit: "percent",
    max: 100,
    whole: false,
    help: "A share of the whole disk, from 0 to 100.",
  },
  {
    key: "disk_fail_free_gb",
    label: "Report an error when free space falls below (GB)",
    unit: "GB",
    max: 1_000_000,
    whole: false,
    help: "Doctor turns red under this much free space, because writes may start to fail.",
  },
  {
    key: "disk_fail_free_percent",
    label: "Report an error when free space falls below (percent)",
    unit: "percent",
    max: 100,
    whole: false,
    help: "A share of the whole disk, from 0 to 100.",
  },
  {
    key: "commitments_backup_keep_days",
    label: "Keep commitments backups for (days)",
    unit: "days",
    max: 36_500,
    whole: true,
    help: "Older backups of the owed list are deleted. 0 keeps every backup.",
  },
  {
    key: "synth_usage_keep_days",
    label: "Keep summariser usage records for (days)",
    unit: "days",
    max: 36_500,
    whole: true,
    help: "Older lines of the summariser's call log are trimmed. 0 keeps every line.",
  },
  {
    key: "log_max_mb",
    label: "Largest size of a log file (MB)",
    unit: "MB",
    max: 1_000_000,
    whole: false,
    help: "A log over this size is trimmed to its newest lines. 0 means no cap.",
  },
];

/** Client-side check for a storage setting; the Tauri command and the CLI
 *  check the same range again. */
export function storageValueError(raw: string, s: Pick<StorageSetting, "max" | "whole" | "unit">): string | null {
  const v = raw.trim();
  const shape = s.whole ? /^\d+$/ : /^\d+(\.\d+)?$/;
  if (!shape.test(v)) return s.whole ? `Enter a whole number of ${s.unit}.` : `Enter a number of ${s.unit}, such as 10 or 2.5.`;
  if (Number(v) > s.max) return `Enter between 0 and ${s.max} ${s.unit}.`;
  return null;
}

/** The time a one-time override lapses, in this Mac's clock. */
export function lapseTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
}

export type DiskSpace = { level?: Level; message?: string } | null | undefined;

/** The disk row's sentence, from doctor's `disk_space` block. */
export function diskWords(ds: DiskSpace): string {
  if (!ds?.level) return "Doctor hasn't checked the disk yet. The storage report checks it now.";
  return ds.message ? `${LEVEL_WORDS[ds.level]}. ${ds.message}` : `${LEVEL_WORDS[ds.level]}.`;
}

export function HealthChecksCard({
  active,
  diskSpace,
  onOpenStorageReport,
}: {
  active: boolean;
  /** Doctor's last `disk_space` block, for the Storage section's disk row. */
  diskSpace?: DiskSpace;
  /** Opens the read-only storage report (`khipu storage`). */
  onOpenStorageReport?: () => void;
}) {
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
      <div className="section-head">Storage</div>
      <div className="section-body">
        <div className="set-field">
          <div className="set-label">Disk space</div>
          <p className="set-help">{diskWords(diskSpace)}</p>
          {onOpenStorageReport ? (
            <div className="toolbar">
              <button type="button" className="sm" onClick={onOpenStorageReport}>
                Open storage report
              </button>
            </div>
          ) : null}
        </div>
        {STORAGE_SETTINGS.map((s) => {
          const row = cfg.data?.storage?.[s.key];
          return (
            <SettingField
              key={s.key}
              label={s.label}
              mono
              narrow
              inputMode="decimal"
              value={row ? String(row.value) : ""}
              help={
                <>
                  {s.help}
                  {row ? ` Default ${row.default}.` : ""}
                  {row?.source === "invalid" ? " The saved value is not usable, so the default applies." : ""}
                </>
              }
              onSave={async (v) => {
                const bad = storageValueError(v, s);
                if (bad) return bad;
                const res = await callCli("khipu_config_set", { key: s.key, value: v.trim() });
                await cfg.reload();
                return res.ok ? null : res.error;
              }}
              canReset={row?.source === "file" || row?.source === "invalid"}
              resetLabel="Use default"
              onReset={async () => {
                const res = await callCli("khipu_config_unset", { key: s.key });
                await cfg.reload();
                return res.ok ? null : res.error;
              }}
            />
          );
        })}
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
