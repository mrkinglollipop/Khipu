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
import {
  canonicalSynthProvider,
  formatUsd,
  synthCallsLine,
  synthFallbackLine,
  synthProviderInfo,
  usageByProviderLines,
} from "./SynthProvider";
import type { DoctorSynthProvider, SynthBudget } from "./SynthProvider";
import {
  PLAN_ALERT_PCT,
  onlyResetWindows,
  liveReadFailures,
  plansHealthDetail,
  roundPct,
  tightestOverall,
  unreadAccounts,
} from "./PlansCard";
import type { UsageReport } from "./PlansCard";
import { deliveryReason, brokenChecks, lastChangeWords, quietRunners, runnerWords, whenLocal } from "./routing";
import type { RouteView } from "./routing";
import { fmtAge, fmtAgo, fmtAgoSince, fmtClock } from "./time";
import { plural, shortProject, timeOnly } from "./work";

export type HealthRowStatus = "ok" | "err" | "skipped";

export type HealthRow = {
  /** The `*_ok` field name this row renders, e.g. "hub_ok". */
  key: string;
  status: HealthRowStatus;
  /** Plain-words title — never the raw field name. */
  label: string;
  /** What the check saw, from the doctor block paired with the flag. */
  detail?: string;
  /** Shown after the row's sentence whatever its status (the Plans row:
   *  accounts that could not be read beside the figures that were). */
  note?: string;
};

// Canonical order + copy for the checks the bundled doctor emits today
// (packages/cli/khipu/cli.py's `ok` aggregate). A key doctor adds later
// falls through to `humanize()` below and sorts after these by status group.
const LABELS: Record<string, string> = {
  hub_ok: "Database reachable",
  disk_space_ok: "Enough free disk space",
  hub_snapshot_ok: "Offline copy of memory",
  ledger_copy_ok: "Offline copy of the ledger",
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
  schema_migrations_ok: "Database has every update this version expects",
  // Phase 6 (honesty): per-nightly-step evidence (D1), instead of trusting
  // only the legacy driver's exit code.
  notes_reconcile_ok: "Notes reconciled by the nightly",
  embed_provider_ok: "Embedding provider reachable",
  synth_provider_ok: "Summariser provider ready",
  synth_budget_ok: "Summariser within today's call ceiling",
  commitments_hygiene_ok: "Commitments hygiene ran",
  mark_stale_ok: "Stale commitments aged out",
  // D6/F2/F4: topic-embedding lag and the search-degrade rate.
  topics_embed_lag_ok: "Search index catching up on time",
  degraded_rate_ok: "Search rarely falls back to keyword-only",
  prompt_recall_outcomes_ok: "Recall answers before each prompt in time",
  prompt_recall_snapshot_ok: "Recall before each prompt reads this Mac's copy",
  // Not a doctor field: read from `khipu usage --json` (plansHealthRow).
  plans: "Plans",
  // Not a doctor field: read from `khipu route --json` (routingHealthRow).
  routing: "Routing",
  routing_delivery_ok: "Routing delivery",
  // Not a `*_ok` field: doctor's `ledger` and `work` blocks (workHealthRow).
  work: "Work",
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

/** A check's plain-words name from its bare name ("outbox"), as the rows show it. */
export function healthCheckLabel(name: string): string {
  return LABELS[`${name}_ok`] ?? humanize(`${name}_ok`);
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

/** The `synth_provider` block: provider, model, and key present or CLI found. */
export function synthProviderDetail(sp: DoctorSynthProvider | undefined): string | undefined {
  if (!sp || typeof sp !== "object" || !sp.provider) return undefined;
  const info = synthProviderInfo(sp.provider);
  const parts = [info?.label ?? canonicalSynthProvider(sp.provider)];
  if (sp.model) parts.push(sp.model);
  if (info?.route === "login" || sp.auth === "subscription") {
    const bin = info?.binary ?? "CLI";
    parts.push(sp.binary ? `${bin} found` : `${bin} not found`);
  } else if (info?.route !== "local") {
    parts.push(sp.key_present ? "key present" : "no key");
  }
  const fallback = synthFallbackLine(sp);
  if (fallback) parts.push(fallback);
  return parts.join(" · ");
}

/** The `synth_budget` block: calls / cap / source, cost, per provider. */
export function synthBudgetDetail(b: SynthBudget | undefined): string | undefined {
  if (!b || typeof b !== "object") return undefined;
  if (b.error && b.calls == null) return b.error;
  const parts = [synthCallsLine(b), `about ${formatUsd(b.estimated_usd_today)} today`];
  const lines = usageByProviderLines(b.usage_by_provider);
  if (lines.length) parts.push(lines.join("; "));
  return parts.join(" · ");
}

/** Doctor's `hub_snapshot` block (packages/cli/khipu/hub_snapshot.py
 *  `snapshot_freshness`): one overall state and, per kind of change, how
 *  far the offline copy is behind the database. */
export type HubSnapshotTable = {
  lag_seconds?: number | null;
  missing?: number;
  truncated?: boolean;
  error?: string;
  not_applicable?: string;
};
export type HubSnapshotBlock = {
  ok?: boolean;
  state?: string;
  reason?: string;
  age_seconds?: number | null;
  tables?: Record<string, HubSnapshotTable>;
  last_known?: { measured_at?: string; checked_at?: string; state?: string } | null;
  refresh_error?: { at?: string; error?: string } | null;
};

const SNAPSHOT_STATE_WORDS: Record<string, string> = {
  fresh: "in step with the database",
  behind: "behind the database",
  stale: "out of date",
  unknown: "not compared with the database just now",
};

const SNAPSHOT_TABLE_WORDS: Record<string, string> = {
  episodes: "episodes",
  tombstones: "forgotten items",
  topics: "topics",
  decisions: "decisions",
  deliverables: "deliverables",
  vectors: "search vectors",
};

/** "behind the database · rebuilt in full 7 h ago · topics 7 h behind, 2 to
 *  catch up". Undefined when doctor sent no block. */
export function hubSnapshotDetail(block: HubSnapshotBlock | undefined): string | undefined {
  if (!block || typeof block !== "object") return undefined;
  const parts: string[] = [];
  const state = block.state ?? "";
  if (SNAPSHOT_STATE_WORDS[state]) parts.push(SNAPSHOT_STATE_WORDS[state]);
  if (block.age_seconds != null) parts.push(`rebuilt in full ${fmtAgo(block.age_seconds)}`);
  if (block.reason === "snapshot_refresh_failed" && block.refresh_error?.error) {
    parts.push(`the last rebuild failed: ${block.refresh_error.error}`);
  }
  const lagging: string[] = [];
  const unmeasured: string[] = [];
  for (const [name, t] of Object.entries(block.tables ?? {})) {
    if (!t || typeof t !== "object") continue;
    const words = SNAPSHOT_TABLE_WORDS[name] ?? name;
    if (t.error) {
      unmeasured.push(words);
      continue;
    }
    const missing = typeof t.missing === "number" ? t.missing : 0;
    if (missing > 0) {
      const more = t.truncated ? " or more" : "";
      lagging.push(`${words} ${fmtAge(t.lag_seconds ?? 0)} behind, ${missing}${more} to catch up`);
    } else if (t.truncated) {
      lagging.push(`${words} too many changes to compare`);
    }
  }
  parts.push(...lagging);
  if (unmeasured.length) parts.push(`could not compare ${unmeasured.join(", ")}`);
  if (state === "fresh" && !lagging.length) parts.push("every kind of change in step");
  const last = block.last_known;
  const was = SNAPSHOT_STATE_WORDS[last?.state ?? ""];
  if (state === "unknown" && was) {
    // measured_at is the database's clock; only checked_at (this Mac's) can
    // be aged against this Mac's now.
    const ago = fmtAgoSince(last?.checked_at);
    if (ago) parts.push(`${ago} it was ${was}`);
    else if (last?.measured_at) parts.push(`when last compared it was ${was}`);
  }
  return parts.length ? parts.join(" · ") : undefined;
}

/** Doctor's `ledger_copy` block (packages/cli/khipu/replica_heal.py
 *  `ledger_copy_status`): the ledger part of the offline copy, refreshed on
 *  its own retry timer. */
export type LedgerCopyBlock = {
  ok?: boolean;
  state?: string;
  age_seconds?: number | null;
  error?: string;
  last_error?: { error?: string; since?: string | null; next_retry_at?: string | null } | null;
};

const LEDGER_COPY_STATE_WORDS: Record<string, string> = {
  fresh: "in step",
  retrying: "an update failed, trying again",
  failing: "updates keep failing",
  old: "out of date",
  unknown: "could not be checked",
};

/** "updates keep failing · last caught up 3 h ago · the last update failed:
 *  database is locked · failing since 45 min ago · next try at 9:06 PM". */
export function ledgerCopyDetail(block: LedgerCopyBlock | undefined): string | undefined {
  if (!block || typeof block !== "object") return undefined;
  if (block.state === "not_applicable") return "no ledger in the offline copy yet";
  const parts: string[] = [];
  const words = LEDGER_COPY_STATE_WORDS[block.state ?? ""];
  if (words) parts.push(words);
  if (block.error) parts.push(block.error);
  parts.push(block.age_seconds != null ? `last caught up ${fmtAgo(block.age_seconds)}` : "never caught up");
  const err = block.last_error;
  if (err) {
    parts.push(`the last update failed: ${err.error ?? "unknown error"}`);
    const since = fmtAgoSince(err.since);
    if (since) parts.push(`failing since ${since}`);
    const next = err.next_retry_at ? new Date(err.next_retry_at) : null;
    if (next && !Number.isNaN(next.getTime())) parts.push(`next try at ${fmtClock(next)}`);
  }
  return parts.join(" · ");
}

function detailFor(key: string, parsed: Record<string, unknown>): string | undefined {
  if (key === "routing_delivery_ok") {
    const delivery = parsed.routing_delivery as { status?: unknown; reasons?: unknown; missing?: unknown } | undefined;
    if (!delivery || typeof delivery !== "object") return "delivery evidence was not reported";
    const values = delivery.status === "unknown" ? delivery.missing : delivery.reasons;
    return Array.isArray(values) && values.every((value) => typeof value === "string") && values.length
      ? values.map((value) => deliveryReason(value)).join("; ")
      : delivery.status === "unknown" ? "delivery evidence could not be checked" : undefined;
  }
  if (key === "ledger_copy_ok") return ledgerCopyDetail(parsed.ledger_copy as LedgerCopyBlock | undefined);
  if (key === "hub_snapshot_ok") return hubSnapshotDetail(parsed.hub_snapshot as HubSnapshotBlock | undefined);
  if (key === "synth_provider_ok") return synthProviderDetail(parsed.synth_provider as DoctorSynthProvider | undefined);
  if (key === "synth_budget_ok") return synthBudgetDetail(parsed.synth_budget as SynthBudget | undefined);
  // A warning keeps the flag true, so the row stays green and says why; red
  // takes its sentence from the attention item instead.
  if (key === "disk_space_ok") {
    const ds = parsed.disk_space as { level?: string; message?: string } | undefined;
    return ds?.level === "amber" && ds.message ? ds.message : undefined;
  }
  if (key === "prompt_recall_snapshot_ok") {
    const prs = parsed.prompt_recall_snapshot as { level?: string; message?: string } | undefined;
    return prs?.level === "amber" && prs.message ? prs.message : undefined;
  }
  return undefined;
}

/** The "Plans" row, from `khipu_usage_status` (doctor's own `plan_usage`
 *  block is the cache only, for agents): the tightest window still in force
 *  across accounts, red at 90%. A window whose reset has passed counts for
 *  nothing. Green only on evidence: when no account gave a figure and any
 *  failed, the row is red and names them; failed accounts, and accounts
 *  showing a fallback because their live read failed, are named beside the
 *  figures that were read. Null until the usage report has been read. */
export function plansHealthRow(plans: UsageReport | null | undefined, now: Date = new Date()): HealthRow | null {
  if (!plans || !Array.isArray(plans.accounts)) return null;
  if (plans.accounts.length === 0) return { key: "plans", status: "skipped", label: LABELS.plans };
  const unread = unreadAccounts(plans, now);
  const tight = tightestOverall(plans, now);
  // A live read that failed (Codex signed out) is named even though its
  // last-turn figures were read, so the owner learns to sign it in.
  const live = liveReadFailures(plans);
  const notes = [...(unread.length ? [`could not read ${unread.join(", ")}`] : []), ...live];
  const note = notes.length ? { note: notes.join("; ") } : {};
  if (!tight) {
    if (unread.length) {
      return {
        key: "plans",
        status: "err",
        label: LABELS.plans,
        detail: `could not read any plan: ${unread.join(", ")}`,
        ...(live.length ? { note: live.join("; ") } : {}),
      };
    }
    const allReset = plans.accounts.some((row) => onlyResetWindows(row, now));
    return {
      key: "plans",
      status: "ok",
      label: LABELS.plans,
      detail: allReset ? "every limit has reset since it was read" : "no plan limits reported",
      ...note,
    };
  }
  const pct = roundPct(tight.window.used_pct ?? 0);
  return {
    key: "plans",
    status: pct >= PLAN_ALERT_PCT ? "err" : "ok",
    label: LABELS.plans,
    detail: plansHealthDetail(plans, now) ?? undefined,
    ...note,
  };
}

/** The "Routing" row, from `khipu_route_view` (routing truth phase 4): the
 *  last change, any standing rule that may be broken and any job runner that
 *  stopped reporting or can't be read. Red only for those two; it never
 *  blocks anything. Grey with no routing record on this Mac; no row until
 *  the record has been read. */
export function routingHealthRow(view: RouteView | null | undefined, now: Date = new Date()): HealthRow | null {
  if (!view || !Array.isArray(view.accounts)) return null;
  if (!view.record) return { key: "routing", status: "skipped", label: LABELS.routing };
  const runners = quietRunners(view).map((r) => runnerWords(r, now));
  const broken = brokenChecks(view).map((c) => (/[.!?]$/.test(c.text) ? c.text : `${c.text}.`));
  const parts = [...runners];
  const last = lastChangeWords(view, now);
  if (last) parts.push(last);
  parts.push(...broken);
  if (!broken.length && (view.rules ?? []).length) parts.push("No standing rule looks broken.");
  return {
    key: "routing",
    status: runners.length || broken.length ? "err" : "ok",
    label: LABELS.routing,
    detail: parts.join(" ") || "Nothing has changed yet.",
  };
}

type WorkSource = { project?: string; state?: string; as_of?: string | null };
/** Summary states that warn (the scope's source-state table). */
const WORK_SOURCE_WORDS: Record<string, (s: WorkSource) => string> = {
  stale: (s) => `not updating since ${timeOnly(s.as_of) || "a while"}`,
  gone: (s) => `not reporting since ${s.as_of ? whenLocal(s.as_of, new Date()) : "a while"}`,
  too_big: () => "over its size limit",
  unreadable: () => "can't be read",
  refused: () => "not read: inside a private folder",
  newer_format: () => "in a newer format",
  wrong_project: () => "for another project",
  missing: () => "no file yet",
};

/** The "Work" row (Work view phase 5, mock work-states.html section 6),
 *  from doctor's `ledger` and `work` blocks: where the ledger is kept, any
 *  ledger changes waiting for the database, and how each summary stands.
 *  Red when a summary is not fresh or changes wait; it never gates doctor.
 *  Grey with no ledger and no summary file; no row until doctor has both. */
export function workHealthRow(parsed: Record<string, unknown>): HealthRow | null {
  const ledger = parsed.ledger as
    | { projects?: Record<string, { rows?: number }>; in_offline_copy?: boolean; outbox?: { pending?: number | null } }
    | undefined;
  const work = parsed.work as { sources?: WorkSource[] } | undefined;
  if (!ledger || !work) return null;
  const projects = Object.values(ledger.projects ?? {});
  const sources = Array.isArray(work.sources) ? work.sources : [];
  if (!projects.length && !sources.length) return { key: "work", status: "skipped", label: LABELS.work };
  const parts: string[] = [];
  if (projects.length) {
    const rows = projects.reduce((n, p) => n + (p.rows ?? 0), 0);
    const where = ledger.in_offline_copy
      ? "in the offline copy and the nightly backup"
      : "in the nightly backup, not in the offline copy yet";
    parts.push(`Ledger: ${plural(rows, "row")} in ${plural(projects.length, "project")}, ${where}.`);
  }
  const pending = ledger.outbox?.pending ?? 0;
  if (pending) parts.push(`${plural(pending, "ledger change")} ${pending === 1 ? "is" : "are"} waiting for the database.`);
  const fresh = sources.filter((s) => s.state === "ok").length;
  const off = sources.filter((s) => s.state === "off").length;
  const bad = sources.filter((s) => s.state && WORK_SOURCE_WORDS[s.state]);
  if (sources.length) {
    const words = [
      ...(fresh ? [`${fresh} fresh`] : []),
      ...bad.map((s) => `1 ${WORK_SOURCE_WORDS[s.state!](s)} (${shortProject(s.project ?? "")})`),
      ...(off ? [`${off} turned off`] : []),
    ];
    parts.push(`Summaries: ${words.join(", ")}.`);
  } else {
    parts.push("No summary files.");
  }
  const red = bad.some((s) => s.state !== "missing") || pending > 0;
  return { key: "work", status: red ? "err" : "ok", label: LABELS.work, detail: parts.join(" ") };
}

function sentenceCase(s: string): string {
  const t = s.trim();
  return t ? `${t.charAt(0).toUpperCase()}${t.slice(1)}${/[.!?]$/.test(t) ? "" : "."}` : t;
}

/** Pure: every top-level boolean `*_ok` field in `parsed` becomes one row —
 *  green, red, or (if `not_configured` names its check) grey "not set up".
 *  Sorted red first, then not-set-up, then green; stable within each group
 *  in the order above, unknown keys last in their group. */
export function healthRows(
  parsed: Record<string, unknown>,
  plans?: UsageReport | null,
  routing?: RouteView | null,
): HealthRow[] {
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
    const detail = detailFor(key, parsed);
    const extra = detail ? { detail } : {};
    if (isNotConfigured(key, notConfigured)) {
      return { key, status: "skipped", label, ...extra };
    }
    // Delivery's top-level boolean is false for both a confirmed delivery
    // problem and an unreadable report. Keep the latter grey so Home never
    // claims a failure without the doctor's evidence.
    const deliveryBlock = parsed.routing_delivery;
    if (
      key === "routing_delivery_ok" &&
      (!deliveryBlock || typeof deliveryBlock !== "object" || (deliveryBlock as { status?: unknown }).status === "unknown")
    ) {
      return { key, status: "skipped", label, ...extra };
    }
    return { key, status: parsed[key] ? "ok" : "err", label, ...extra };
  });
  const plansRow = plansHealthRow(plans);
  if (plansRow) rows.push(plansRow);
  const routingRow = routingHealthRow(routing);
  if (routingRow) rows.push(routingRow);
  const workRow = workHealthRow(parsed);
  if (workRow) rows.push(workRow);

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
  synth_budget_ok: "synth_budget",
  synth_provider_ok: "synth_provider",
  recall_probe_ok: "recall_probe",
  bundle_seal_ok: "bundle_seal",
  dsn_file_ok: "dsn_file",
  git_sync_ok: "git_sync",
};

/** The health-report rows a Home callout (by its attention key) is about,
 *  so its Details button can show that row instead of the whole report. */
export function healthKeysForAttention(attentionKey: string): string[] {
  if (attentionKey.startsWith("liveness:")) return ["capture_liveness_ok"];
  if (attentionKey.startsWith("routing")) return ["routing", "routing_delivery_ok"];
  if (attentionKey.startsWith("work:")) return ["work"];
  if (attentionKey.startsWith("plans")) return ["plans"];
  // A queued promotion has no health row; markHealthRows then shows the
  // whole report instead of highlighting nothing.
  if (attentionKey === "promotion") return [];
  const fromOk = Object.entries(ATTENTION_KEY_FOR_OK).find(([, v]) => v === attentionKey)?.[0];
  return [fromOk ?? `${attentionKey}_ok`];
}

/** The rows each Home tile summarises. */
export const TILE_HEALTH_KEYS = {
  database: ["hub_ok", "hub_snapshot_ok", "schema_migrations_ok"],
  recording: ["capture_liveness_ok", "outbox_ok"],
  search: ["embed_coverage_ok", "index_freshness_ok", "topics_embed_lag_ok", "degraded_rate_ok", "drift_ok"],
  backups: ["backup_ok", "graph_backup_ok", "graph_offsite_ok"],
} as const;

/** Marks the rendered rows for `keys` (the whole report when none render) and
 *  returns them, first in page order, so the caller can scroll to it. */
export function markHealthRows(root: ParentNode, keys: readonly string[]): Element[] {
  const rows = keys.flatMap((k) => Array.from(root.querySelectorAll(`[data-health-key="${k}"]`)));
  const marked = rows.length ? rows : Array.from(root.querySelectorAll("[data-health-report]"));
  marked.forEach((el) => el.classList.add("health-hl"));
  return marked;
}

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
  plans,
  routing,
  onFixAction,
  busy,
}: {
  parsed: Record<string, unknown>;
  attention: Attention[];
  /** `khipu usage --json`, for the Plans row; no row until it is read. */
  plans?: UsageReport | null;
  /** `khipu route --json`, for the Routing row; no row until it is read. */
  routing?: RouteView | null;
  /** Fired when a row's fix button is pressed; the caller owns what each
   *  `fix.kind` actually does (reinstall hook / run probe / open revisions —
   *  see the Home attention callouts in App.tsx). Optional so this component
   *  renders fine, action-less, wherever no handler is wired (e.g. tests). */
  onFixAction?: (fix: NonNullable<Attention["fix"]>, harness?: string) => void;
  busy?: boolean;
}) {
  const rows = healthRows(parsed, plans, routing);
  return (
    <div className="rows" data-health-report>
      {rows.map((row) => {
        if (row.status === "ok") {
          const text = [row.detail, row.note].filter(Boolean).join("; ");
          return (
            <div key={row.key} className="row-item" data-health-key={row.key}>
              <Check
                size={16} strokeWidth={1.75} aria-hidden className="ok" />
              <span className="row-main">{row.label}</span>
              {text ? <span className="row-meta wrap">{text}</span> : null}
            </div>
          );
        }
        if (row.status === "skipped") {
          return (
            <div key={row.key} className="row-item" data-health-key={row.key}>
              <CircleMinus
                size={16}
                strokeWidth={1.75}
                aria-hidden
                className="muted"
              />
              <span className="row-main">{row.label}</span>
              <span className="row-meta">{row.key === "routing_delivery_ok" ? `can't be checked${row.detail ? `: ${row.detail}` : ""}` : "not set up"}</span>
            </div>
          );
        }
        const matches = findAttention(row.key, attention);
        const sentence = matches.length
          ? matches.map((m) => m.cause).join(" ")
          : "needs attention — see the raw report";
        const single = matches.length === 1 ? matches[0] : undefined;
        // The Plans attention cause already names every window at 90%; with
        // none (nothing could be read) the row's own detail says why.
        // The Routing row says what it saw itself: its attention items are
        // the longer form of the same facts.
        const text = row.key === "routing" ? (row.detail ?? sentence) :
          row.key === "plans"
            ? [matches.length ? sentence : sentenceCase(row.detail ?? sentence), row.note ? sentenceCase(row.note) : ""]
                .filter(Boolean)
                .join(" ")
            : row.detail
              ? `${sentence} ${row.detail}`
              : sentence;
        return (
          <div key={row.key} className="row-item" data-health-key={row.key}>
            <TriangleAlert
              size={16}
              strokeWidth={1.75}
              aria-hidden
              className="err"
            />
            <span className="row-main">{row.label}</span>
            <span className={row.key === "routing" ? "row-meta wrap" : "row-meta"}>{text}</span>
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
