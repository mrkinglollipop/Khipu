// Home → "Full health report" used to render red items as rows and then
// ALWAYS dump the raw doctor JSON below them — with nothing red the whole
// report was a wall of raw JSON (audit: scratchpad/visual-042/03_health_report.png,
// 2026-09-05). This turns every top-level `*_ok` boolean the bundled doctor
// emits into one plain-words row: green (checked and clean), red (checked
// and broken — with the same sentence/action Home's attention callouts use,
// via `doctorAttention.ts`), or grey (not configured on this Mac, so never
// checked at all — distinct from both).
import { Check, CircleMinus, TriangleAlert } from "lucide-react";
import type { Attention } from "./doctorAttention";

export type HealthRowStatus = "ok" | "err" | "skipped";

export type HealthRow = {
  /** The `*_ok` field name this row renders, e.g. "hub_ok". */
  key: string;
  status: HealthRowStatus;
  /** Plain-words title — never the raw field name. */
  label: string;
};

// Canonical order + copy for the checks the bundled doctor emits today
// (packages/cli/khipu/cli.py's `ok` aggregate). A key doctor adds later
// falls through to `humanize()` below and sorts after these by status group.
const LABELS: Record<string, string> = {
  hub_ok: "Database reachable",
  dsn_file_ok: "Connection stored on this Mac",
  drift_ok: "Memory index matches the database",
  graph_drift_ok: "Connections index matches the database",
  outbox_ok: "Queued captures delivered",
  capture_liveness_ok: "Capture hook is recording",
  git_sync_ok: "Notes repository in sync",
  backup_ok: "Database backup fresh",
  graph_backup_ok: "Connections snapshot fresh",
  graph_offsite_ok: "Off-site copy fresh",
  index_freshness_ok: "Search index fresh",
  embed_coverage_ok: "Search index complete",
  recall_probe_ok: "Memory round trip works",
  bundle_seal_ok: "App bundle intact",
};

const ORDER = Object.keys(LABELS);

// `not_configured` names the CHECK's input, not the `*_ok` field — doctor
// says "memory_root"/"graph_sqlite" (packages/cli/khipu/cli.py `not_configured
// .append(...)`), matching `NOT_CONFIGURED_LABEL` in App.tsx, not "drift"/
// "graph_drift". Everything else lines up with the `_ok` key's own base name.
const NOT_CONFIGURED_NAME_FOR_OK: Record<string, string> = {
  drift_ok: "memory_root",
  graph_drift_ok: "graph_sqlite",
};

/** `foo_bar_ok` -> "Foo bar" for a key this file doesn't have plain-words
 *  copy for yet — better than showing the raw field name. */
function humanize(key: string): string {
  const base = key.replace(/_ok$/, "");
  const words = base.split("_").filter(Boolean);
  if (words.length === 0) return key;
  return words
    .map((w, i) => (i === 0 ? w.charAt(0).toUpperCase() + w.slice(1) : w))
    .join(" ");
}

function isNotConfigured(key: string, notConfigured: string[]): boolean {
  if (notConfigured.length === 0) return false;
  const candidates = [
    NOT_CONFIGURED_NAME_FOR_OK[key],
    key.replace(/_ok$/, ""),
    key,
  ];
  return candidates.some((c) => c !== undefined && notConfigured.includes(c));
}

/** Pure: every top-level boolean `*_ok` field in `parsed` becomes one row —
 *  green, red, or (if `not_configured` names its check) grey "not set up".
 *  Sorted red first, then not-set-up, then green; stable within each group
 *  in the order above, unknown keys last in their group. */
export function healthRows(parsed: Record<string, unknown>): HealthRow[] {
  const notConfiguredRaw = (parsed as { not_configured?: unknown })
    .not_configured;
  const notConfigured = Array.isArray(notConfiguredRaw)
    ? notConfiguredRaw.filter((x): x is string => typeof x === "string")
    : [];

  const okKeys = Object.keys(parsed).filter(
    (k) => k.endsWith("_ok") && typeof parsed[k] === "boolean",
  );

  const rows: HealthRow[] = okKeys.map((key) => {
    const label = LABELS[key] ?? humanize(key);
    if (isNotConfigured(key, notConfigured)) {
      return { key, status: "skipped", label };
    }
    return { key, status: parsed[key] ? "ok" : "err", label };
  });

  const statusRank: Record<HealthRowStatus, number> = {
    err: 0,
    skipped: 1,
    ok: 2,
  };
  const orderIndex = (key: string) => {
    const i = ORDER.indexOf(key);
    return i === -1 ? ORDER.length + okKeys.indexOf(key) : i;
  };

  return [...rows].sort((a, b) => {
    const byStatus = statusRank[a.status] - statusRank[b.status];
    if (byStatus !== 0) return byStatus;
    return orderIndex(a.key) - orderIndex(b.key);
  });
}

// Attention items are keyed by the CHECK, not the `*_ok` field — mostly the
// same string, minus the suffix (see doctorAttention.ts). Capture liveness is
// the one exception: it fans out into one item per red harness, keyed
// "liveness:<harness>", because a harness name is not known ahead of time.
const ATTENTION_KEY_FOR_OK: Record<string, string> = {
  drift_ok: "drift",
  graph_drift_ok: "graph_drift",
  outbox_ok: "outbox",
  backup_ok: "backup",
  graph_backup_ok: "graph_backup",
  graph_offsite_ok: "graph_offsite",
  index_freshness_ok: "index_freshness",
  embed_coverage_ok: "embed_coverage",
  recall_probe_ok: "recall_probe",
  bundle_seal_ok: "bundle_seal",
  dsn_file_ok: "dsn_file",
  git_sync_ok: "git_sync",
};

function findAttention(okKey: string, attention: Attention[]): Attention[] {
  if (okKey === "capture_liveness_ok") {
    return attention.filter((a) => a.key.startsWith("liveness:"));
  }
  const base = ATTENTION_KEY_FOR_OK[okKey] ?? okKey.replace(/_ok$/, "");
  return attention.filter((a) => a.key === base);
}

/** Renders `healthRows(parsed)` as the report's row list: a check mark for
 *  green, a muted dash + "not set up" for grey, and for red the SAME
 *  sentence and (at most one) fix action Home's attention callouts already
 *  show — pulled from `attention` by key, never invented here. A red row
 *  with no matching attention entry falls back to pointing at the raw
 *  report rather than staying silent about what broke. */
export function HealthReportRows({
  parsed,
  attention,
  onFixAction,
  busy,
}: {
  parsed: Record<string, unknown>;
  attention: Attention[];
  /** Fired when a row's fix button is pressed; the caller owns what each
   *  `fix.kind` actually does (reinstall hook / run probe / open revisions —
   *  see the Home attention callouts in App.tsx). Optional so this component
   *  renders fine, action-less, wherever no handler is wired (e.g. tests). */
  onFixAction?: (fix: NonNullable<Attention["fix"]>, harness?: string) => void;
  busy?: boolean;
}) {
  const rows = healthRows(parsed);
  return (
    <div className="rows">
      {rows.map((row) => {
        if (row.status === "ok") {
          return (
            <div key={row.key} className="row-item">
              <Check size={16} strokeWidth={1.75} aria-hidden className="ok" />
              <span className="row-main">{row.label}</span>
            </div>
          );
        }
        if (row.status === "skipped") {
          return (
            <div key={row.key} className="row-item">
              <CircleMinus
                size={16}
                strokeWidth={1.75}
                aria-hidden
                className="muted"
              />
              <span className="row-main">{row.label}</span>
              <span className="row-meta">not set up</span>
            </div>
          );
        }
        const matches = findAttention(row.key, attention);
        const sentence = matches.length
          ? matches.map((m) => m.cause).join(" ")
          : "needs attention — see the raw report";
        const single = matches.length === 1 ? matches[0] : undefined;
        return (
          <div key={row.key} className="row-item">
            <TriangleAlert
              size={16}
              strokeWidth={1.75}
              aria-hidden
              className="err"
            />
            <span className="row-main">{row.label}</span>
            <span className="row-meta">{sentence}</span>
            {single?.fix ? (
              <button
                type="button"
                className="sm"
                disabled={busy}
                onClick={() => onFixAction?.(single.fix!, single.harness)}
              >
                {single.fix.label}
              </button>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
