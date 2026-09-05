import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { invoke } from "@tauri-apps/api/core";
import { getVersion } from "@tauri-apps/api/app";
import { check } from "@tauri-apps/plugin-updater";
import { relaunch } from "@tauri-apps/plugin-process";
import {
  open as openDirectoryDialog,
  save as saveFileDialog,
} from "@tauri-apps/plugin-dialog";
import {
  CircleCheck,
  CircleMinus,
  Loader2,
  MessageSquare,
  RefreshCw,
  Search,
  Stethoscope,
  Trash2,
  TriangleAlert,
  Waypoints,
  X,
} from "lucide-react";
import khipuIcon from "./assets/khipu-icon.png";
import {
  Callout,
  Chip,
  Dialog,
  Disclosure,
  EmptyState,
  ListRow,
  Segmented,
  Tag,
  Tile,
} from "./ui";
import { ComponentsPanel } from "./ComponentsPanel";
import { IntegrationsPanel } from "./IntegrationsPanel";
import type { LivenessPayload, RecallProbeStatus } from "./IntegrationsPanel";
import { SUPPORT_EMAIL, Welcome, welcomeCompleted } from "./Welcome";
import { WorkingBanner } from "./WorkingBanner";
import { FeedbackForm } from "./FeedbackForm";
import { PostUpdateNoticeDialog } from "./PostUpdateNoticeDialog";
import {
  noticeForUpgrade,
  readLastNoticedVersion,
  writeLastNoticedVersion,
  type PostUpdateNotice,
} from "./postUpdateNotices";
import "./App.css";

/** Six destinations, one per user job, plus the first-run flow and Revisions —
 *  which is a drill-in from Home's "Conflicting edits" item, not a peer
 *  (audit 2026-09-04 IA). Status and Doctor are gone as screens; their data
 *  paths feed Home. */
type Tab =
  | "first-run"
  | "home"
  | "recall"
  | "owed"
  | "activity"
  | "harnesses"
  | "settings"
  | "revisions";

type CacheTab = "status" | "activity" | "revisions" | "doctor";

type ConflictSummary = {
  open_file_vs_pg?: number;
  ok?: boolean;
  note?: string;
  file_vs_pg?: Array<{ slug: string; issue: string }>;
  topics_with_multiple_revisions?: Array<{
    slug: string;
    revision_count: number;
    last_revised_at?: string;
  }>;
  revision_row_count?: number;
  /** How many topics the report actually compared, and the ones it could not
   * read. Without these on screen a green summary says nothing about its own
   * coverage — which is how a 40-of-622 sample read as "no drift". */
  topics_checked?: number;
  topic_files_unreadable?: string[];
};

/** One row of `revisions.recent` — what the slug filter actually narrows. */
type RecentRevision = {
  id: number;
  slug: string;
  revised_at?: string;
  source?: string;
  note?: string;
  preview?: string;
};

type Counts = {
  episodes?: number;
  topics?: number;
  nodes?: number;
  edges?: number;
  embeddings?: number;
  topic_revisions?: number;
};

type ModelRole = {
  provider: string;
  endpoint: string;
  model_id: string;
};

type ModelsState = {
  synth: ModelRole;
  embed: ModelRole;
  vision: ModelRole;
  models_error: string | null;
};

const DEFAULT_MODELS: ModelsState = {
  synth: {
    provider: "cloud",
    endpoint: "",
    model_id: "gemini-2.5-flash",
  },
  embed: { provider: "cloud", endpoint: "", model_id: "" },
  vision: { provider: "off", endpoint: "", model_id: "" },
  models_error: null,
};

type SearchResult = {
  kind?: string;
  id?: string | number;
  label?: string;
  snippet?: string;
  /** Fused RRF score from `khipu search` — already in the payload, never
   * rendered before (audit 2026-09-04). Higher is better; the scale is
   * relative to the other hits in the same response, so it is shown as a
   * relevance bar rather than a number pretending to be a percentage. */
  score?: number;
  paths?: string[];
  neighbors?: { id: string; type?: string }[];
  /** Row metadata `khipu search` gained in phase 5 so the footer can read
   * "date · project · harness" as the mock does. `ts` is an ISO string on
   * every kind that has one; `project` / `harness` exist on episodes only
   * (a topic has neither, and inventing one is the bug this replaced). */
  ts?: string;
  project?: string;
  harness?: string;
  /** Optional per-signal explanation, if the CLI ever attaches one. Rendered
   * as plain text when present; absent from today's payload. */
  why?: string;
  signals?: unknown;
};

/** Per-leg milliseconds from `hybrid_search` (phase 5 addendum). Every field
 * is optional: an offline/stale payload carries no timing at all. */
type SearchTiming = {
  embed_ms?: number;
  cosine_ms?: number;
  literal_ms?: number;
  lexical_ms?: number;
  fusion_ms?: number;
  enrich_ms?: number;
  total_ms?: number;
  /** "hit" (no embedding call), "miss" (embedded and cached) or "off". */
  embed_cache?: string;
  embed_error?: string;
};

/** Why search by meaning was skipped for this query, in the engine's words
 *  (`degraded` on the search payload): "embed-budget" (today's embedding
 *  budget is spent), "embed-unavailable" (the embedding service did not answer
 *  inside the query budget) or "no-embedding" (no index profile is active). */
const DEGRADED_COPY: Record<string, { title: string; hint: string }> = {
  "embed-budget": {
    title: "Showing exact-word matches only",
    hint: "Today's embedding ceiling was reached, so this search skipped meaning. It resets at midnight UTC; KHIPU_EMBED_DAILY_CALLS raises the ceiling (0 removes it).",
  },
  "embed-unavailable": {
    title: "Showing exact-word matches only",
    hint: "The embedding service did not answer in time (rate limit or network), so this search skipped meaning. Try again in a moment.",
  },
  "no-embedding": {
    title: "Showing exact-word matches only",
    hint: "No search index profile is active, so search by meaning is off. Settings › Search index shows the state.",
  },
};

const TIMING_LABEL: Record<string, string> = {
  embed_ms: "Embedding the query",
  cosine_ms: "Vector scan",
  literal_ms: "Exact-word scan",
  lexical_ms: "Word-overlap ranking",
  fusion_ms: "Fusing and filtering",
  enrich_ms: "Connections lookup",
  total_ms: "Engine total",
};

/** The three `khipu search --mode` values, in the order the toolbar offers
 * them. `hybrid` is the CLI's own default and so is the pane's. */
type SearchMode = "hybrid" | "semantic" | "literal";

const SEARCH_MODES: {
  mode: SearchMode;
  label: string;
  hint: string;
  placeholder: string;
}[] = [
  {
    mode: "hybrid",
    label: "Best match",
    hint: "Meaning, word overlap and exact text together — the recommended default.",
    placeholder: "ask in your own words",
  },
  {
    mode: "semantic",
    label: "By meaning",
    hint: "Vector similarity only. The words need not appear in the text.",
    placeholder: "describe what you are looking for",
  },
  {
    mode: "literal",
    label: "Exact words",
    hint: "Substring match only — for ids, hashes and error text.",
    placeholder: "exact words to match",
  },
];

/** A search hit's `why`, when the payload carries one. Kept tolerant: any of
 * a string, a list of strings, or an object of signal → value renders as one
 * readable line, and anything else renders as nothing at all. */
function whyText(r: SearchResult): string | null {
  if (typeof r.why === "string" && r.why.trim()) return r.why.trim();
  const sig = r.signals;
  if (typeof sig === "string" && sig.trim()) return sig.trim();
  if (Array.isArray(sig)) {
    const parts = sig.filter((x) => typeof x === "string" || typeof x === "number");
    return parts.length ? parts.join(" · ") : null;
  }
  if (sig && typeof sig === "object") {
    const parts = Object.entries(sig as Record<string, unknown>)
      .filter(([, v]) => typeof v === "string" || typeof v === "number")
      .map(([k, v]) => `${k} ${typeof v === "number" ? Number(v).toFixed(3) : String(v)}`);
    return parts.length ? parts.join(" · ") : null;
  }
  return null;
}

type GraphEdge = {
  src?: string;
  dst?: string;
  type?: string;
};

type GraphWalkRow = {
  node_id?: string;
  via?: string;
  type?: string;
  hops?: number;
};

type GraphNeighbor =
  | {
      mode: "walk";
      id: string;
      type?: string;
      hops?: number;
      via?: string;
    }
  | {
      mode: "edge";
      id: string;
      type?: string;
      src?: string;
      dst?: string;
    };

const CACHE_TTL_MS = 60_000;

function clampHops(value: number): number {
  if (!Number.isFinite(value)) return 1;
  return Math.min(4, Math.max(1, Math.trunc(value)));
}

/** Rail glyphs, traced from the approved mock (`rail.html`) rather than picked
 *  from an icon set — the six items are the design, not a lucide lookup. */
function RailIcon({ d }: { d: ReactNode }) {
  return (
    <svg
      viewBox="0 0 16 16"
      aria-hidden
      width={16}
      height={16}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {d}
    </svg>
  );
}

const NAV_ICONS: Record<Exclude<Tab, "first-run" | "revisions">, ReactNode> = {
  home: <RailIcon d={<path d="M3 8.5 8 4l5 4.5V13H3z" />} />,
  recall: (
    <RailIcon
      d={
        <>
          <circle cx="7" cy="7" r="4" />
          <path d="m10 10 3.5 3.5" />
        </>
      }
    />
  ),
  owed: (
    <RailIcon
      d={
        <>
          <path d="M3 4h10M3 8h7M3 12h5" />
          <path d="m11 11 1.5 1.5L15 10" />
        </>
      }
    />
  ),
  activity: (
    <RailIcon
      d={
        <>
          <circle cx="8" cy="8" r="5.5" />
          <path d="M8 5v3l2 1.5" />
        </>
      }
    />
  ),
  harnesses: (
    <RailIcon
      d={
        <>
          <path d="M5 3v3M11 3v3M3 6h10v3a5 5 0 0 1-10 0z" />
          <path d="M8 11v3" />
        </>
      }
    />
  ),
  settings: (
    <RailIcon
      d={
        <>
          <path d="M3 5h10M3 11h10" />
          <circle cx="6" cy="5" r="1.5" />
          <circle cx="10" cy="11" r="1.5" />
        </>
      }
    />
  ),
};

/** One row of `khipu owed --json` (khipu.commitments.list_owed). */
type Commitment = {
  id: number;
  text?: string;
  project?: string | null;
  owner?: string | null;
  kind?: string | null;
  opened_episode?: number | null;
  opened_at?: string | null;
  due_after?: string | null;
  status?: string | null;
  /** The close side of the lifecycle — `commitments.list_owed` has always
   *  returned these; the read-only phase never rendered them. `closed_at` is
   *  what the "Closed today" callout is built from. */
  closed_episode?: number | null;
  closed_at?: string | null;
  close_reason?: string | null;
  /** Commitment-quality fields (migrations 0012/0013, resolved in
   *  `commitments.list_owed`): who owes it, whether it names a future
   *  trigger, how it sorts, and how often a later capture has re-stated it.
   *  All optional — a pre-migration hub answers without them. */
  future_trigger?: boolean;
  priority?: number;
  seen_count?: number;
  last_seen_at?: string | null;
};

/** The three Owed groups (phase 4 addendum + phase 5 brief). "Needs you" is
 *  exactly `owner === "user"`, which the CLI resolves; the app never
 *  re-derives it. */
type OwedGroupId = "needs-you" | "promised" | "plan";

const OWED_GROUPS: ReadonlyArray<
  readonly [OwedGroupId, string, string]
> = [
  [
    "needs-you",
    "Needs you",
    "Questions and blockers first, then the follow-ups and promises you own.",
  ],
  [
    "promised",
    "Promised with a trigger",
    "The agent said it would do these when something happens.",
  ],
  [
    "plan",
    "This session's plan",
    "Steps the agent set itself. They close on their own when a later capture says so.",
  ],
] as const;

function owedGroupOf(c: Commitment): OwedGroupId {
  if ((c.owner ?? "").toLowerCase() === "user") return "needs-you";
  return c.future_trigger ? "promised" : "plan";
}

type OwedStatus = "open" | "closed" | "stale";

/** How many commitments one screenful asks for. A count that lands exactly on
 *  the cap is shown as "N+", because it is a floor, not a total. */
const OWED_LIMIT = 500;

/** How many captures one page of Activity shows. `khipu activity` has no
 *  `--offset`, so "Show 40 more" re-asks for a longer window (`--limit`) and
 *  renders all of it: the list is newest-first, so a longer limit is always a
 *  superset of a shorter one. */
const ACTIVITY_PAGE = 40;

/** The kinds `khipu search --kind` accepts on the modes this screen offers.
 *  `media` is semantic-only and `node` is literal/hybrid-only, so neither is
 *  offered as a chip: a filter that silently fails on the selected mode is
 *  worse than no filter. */
const SEARCH_KINDS = ["episode", "topic"] as const;

/** A named red check, in plain words, with at most one fix. Home renders one
 *  callout per entry; `action` is omitted where the app has no honest fix to
 *  offer and "Details" opens the Advanced disclosure instead. */
type Attention = {
  key: string;
  tone: "err" | "warn";
  title: string;
  cause: string;
  fix?: { label: string; kind: "reinstall-hook" | "recall-probe" | "revisions" };
  harness?: string;
};

/** Settings sub-navigation (overhaul phase 5, `mocks/main.settings.html`).
 *  Eight sections, one per thing a person comes here to change. */
type SettingsSection =
  | "database"
  | "capture"
  | "index"
  | "data"
  | "another-mac"
  | "components"
  | "updates"
  | "advanced";

const SETTINGS_SECTIONS: ReadonlyArray<readonly [SettingsSection, string]> = [
  ["database", "Database"],
  ["capture", "Capture & models"],
  ["index", "Search index"],
  ["data", "Data & backups"],
  ["another-mac", "Another Mac"],
  ["components", "Components"],
  ["updates", "Updates"],
  ["advanced", "Advanced"],
] as const;

/** The capture cadence, read-only: `khipu config` does not expose it, so
 *  these mirror `session_capture.MIN_TURNS` / `MIN_MINUTES` — the engine's
 *  own defaults, overridable only by the environment. Shown as text, never as
 *  an input that would not be saved anywhere. */
const CAPTURE_MIN_TURNS = 5;
const CAPTURE_MIN_MINUTES = 20;

/** The coverage rows `khipu doctor` reports, in the order they matter. */
const EMBED_KINDS: ReadonlyArray<readonly [string, string]> = [
  ["episodes", "Sessions"],
  ["topics", "Topic pages"],
  ["commitments", "Owed items"],
  ["media", "Images"],
] as const;

const HARNESS_LABEL: Record<string, string> = {
  claude_code: "Claude Code",
  cursor: "Cursor",
  aegis: "Aegis",
  codex: "Codex",
  grok_bot: "Grok Bot",
};

/** `commitments.kind` values, in the mocks' words. Anything the engine adds
 *  later falls through to its own raw kind rather than being hidden. */
const OWED_KIND_LABEL: Record<string, string> = {
  followup: "Follow-up",
  blocker: "Blocker",
  question: "Question",
  promise: "Promise",
  decision: "Decision",
};

function harnessLabel(id: string): string {
  return HARNESS_LABEL[id] ?? id;
}

/** The project a capture belongs to, from the episode's own `scope`. A scope is
 *  either a repo path on disk or free text the session named for itself; only
 *  the path form is a project, so free text carries no tag rather than a
 *  sentence squeezed into a pill. Never invented. */
function projectFromScope(scope: string | undefined | null): string | null {
  if (!scope || !scope.trim()) return null;
  const s = scope.trim();
  if (!s.startsWith("/")) return null;
  const parts = s.split("/").filter(Boolean);
  const codeAt = parts.lastIndexOf("Code");
  if (codeAt >= 0 && parts[codeAt + 1]) return parts[codeAt + 1];
  return parts[parts.length - 1] ?? null;
}

/** `commitments.project` is an owner/repo slug — or, when the capture could not
 *  resolve one, the raw `harness:uuid` session id it was opened under. The repo
 *  half identifies the first; the harness name identifies the second. The full
 *  value stays as the tooltip either way. */
function shortProject(project: string): string {
  if (project.includes(":")) {
    // A commitment scoped to a session rather than a repo. The pill is
    // 96px wide; "Claude Code session" overran it, so the label is the short
    // word and the harness stays in the pill's title (2026-09-05).
    return "Session";
  }
  const parts = project.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? project;
}

/** `session_id` is `harness:uuid`, so the harness is the half before the
 *  colon — the same split `khipu search --harness` does server-side. */
function harnessFromSession(sessionId: string | null | undefined): string | null {
  if (!sessionId) return null;
  const at = sessionId.indexOf(":");
  return at > 0 ? sessionId.slice(0, at) : null;
}

/** One full episode row from `khipu activity --show ID`. Everything here is a
 *  column the CLI actually returns; nothing is derived on the way in. */
type EpisodeDetail = {
  id: number;
  ts?: string | null;
  ingested_at?: string | null;
  session_id?: string | null;
  scope?: string | null;
  summary?: string | null;
  topics?: unknown;
  people?: unknown;
  decisions?: unknown;
  preferences?: unknown;
  edges?: unknown;
  raw?: unknown;
};

/** jsonb array columns carry whatever the capture wrote. Only the entries that
 *  are already text become bullets — an object is never stringified into one. */
function asStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const out: string[] = [];
  for (const item of value) {
    if (typeof item === "string" && item.trim()) out.push(item.trim());
    else if (typeof item === "number") out.push(String(item));
  }
  return out;
}

/** `raw.open_loops` — what THIS capture opened, in the capture's own words.
 *  Entries are strings or `{text, kind, due_after, owner}` objects
 *  (khipu.commitments._normalize_open_loop). */
function openLoopTexts(raw: unknown): string[] {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
  const loops = (raw as { open_loops?: unknown }).open_loops;
  if (!Array.isArray(loops)) return [];
  const out: string[] = [];
  for (const item of loops) {
    if (typeof item === "string" && item.trim()) out.push(item.trim());
    else if (item && typeof item === "object") {
      const text = (item as { text?: unknown }).text;
      if (typeof text === "string" && text.trim()) out.push(text.trim());
    }
  }
  return out;
}

function parseTs(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(String(iso).replace(" ", "T"));
  return Number.isNaN(d.getTime()) ? null : d;
}

/** Day heading for the Activity list: Today / Yesterday / "Sep 2". */
function dayLabel(iso: string | null | undefined): string {
  const d = parseTs(iso);
  if (!d) return "Undated";
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === yesterday.toDateString()) return "Yesterday";
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function timeLabel(iso: string | null | undefined): string {
  const d = parseTs(iso);
  return d ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
}

function shortDateLabel(iso: string | null | undefined): string {
  const d = parseTs(iso);
  return d ? d.toLocaleDateString([], { month: "short", day: "numeric" }) : "—";
}

function isToday(iso: string | null | undefined): boolean {
  const d = parseTs(iso);
  return d != null && d.toDateString() === new Date().toDateString();
}

/** Graph node ids are `prefix:rest`. The prefix IS the kind — it is minted by
 *  the graph builder, not guessed here — so a neighbour renders as a kind Tag
 *  plus the rest of its own id. */
const NODE_PREFIX_LABEL: Record<string, string> = {
  topic: "Topic",
  path: "Path",
  episode: "Episode",
  concept: "Concept",
  symbol: "Symbol",
  file: "File",
  note: "Note",
};

function nodeKindLabel(id: string): string {
  const at = id.indexOf(":");
  if (at <= 0) return "Node";
  const prefix = id.slice(0, at);
  return NODE_PREFIX_LABEL[prefix] ?? prefix;
}

function nodeTitle(id: string): string {
  const at = id.indexOf(":");
  return at > 0 ? id.slice(at + 1) : id;
}

/** Neighbourhood rows out of one `khipu graph` payload. Shared by the Graph
 *  sub-view and the Recall detail pane's "Connected" list so the two can never
 *  read the same payload differently. */
function graphNeighborsFrom(parsed: unknown): {
  rootId: string | null;
  neighbors: GraphNeighbor[];
} {
  if (parsed && typeof parsed === "object") {
    const obj = parsed as {
      id?: unknown;
      edges?: unknown;
      walk?: unknown;
      graph_table?: unknown;
    };
    const rootId = typeof obj.id === "string" ? obj.id : null;

    if (Array.isArray(obj.walk) && obj.walk.length > 0) {
      const neighbors: GraphNeighbor[] = [];
      for (const row of obj.walk as GraphWalkRow[]) {
        if (typeof row.node_id !== "string" || !row.node_id) continue;
        neighbors.push({
          id: row.node_id,
          via: typeof row.via === "string" ? row.via : undefined,
          type: typeof row.type === "string" ? row.type : undefined,
          hops: typeof row.hops === "number" ? row.hops : undefined,
          mode: "walk",
        });
      }
      return { rootId, neighbors };
    }

    const edges = Array.isArray(obj.edges) ? (obj.edges as GraphEdge[]) : [];
    const edgeRows =
      edges.length > 0
        ? edges
        : Array.isArray(obj.graph_table)
          ? (obj.graph_table as GraphEdge[])
          : [];
    return {
      rootId,
      neighbors: edgeRows.map((e) => ({
        id: e.dst === rootId ? (e.src ?? "") : (e.dst ?? e.src ?? ""),
        src: e.src,
        dst: e.dst,
        type: e.type,
        mode: "edge" as const,
      })),
    };
  }
  return { rootId: null, neighbors: [] };
}

// Any user-typed value reaches argparse as its own argv element, so a value
// beginning with "-" would be read as a FLAG rather than as the value. Callers
// pass positional input after a "--" separator and flag input as "--name=value"
// (one token, unambiguous even when the value starts with a dash). Typing
// "--limit" into the search box used to produce an argparse usage error instead
// of a search (audit 2026-08-17).
async function runKhipu(args: string[]): Promise<string> {
  return invoke<string>("run_khipu", { args });
}

type JobEntry = {
  plist_label?: string;
  log_path?: string;
  last_run_iso?: string | null;
  last_run_mtime?: number | null;
  plist_loaded?: boolean;
  plist_current?: boolean | null;
  next_schedule?: string;
  last_exit?: number | null;
};

type DoctorJobs = {
  nightly?: JobEntry;
  monthly?: JobEntry;
  graph_build?: JobEntry;
  embed_media_backfill?: JobEntry;
};

// graph_backup / graph_offsite from khipu.graph_backup.local_health() /
// offsite_health(): "skipped" means non-producer machine (by design, not a
// failure); a configured-but-failing check is skipped:false, ok:false.
type DoctorHealthCheck = {
  ok?: boolean;
  skipped?: boolean;
  reason?: string;
  age_seconds?: number;
  max_age_hours?: number;
  max_age_days?: number;
  latest?: { age_seconds?: number };
};

const NOT_CONFIGURED_LABEL: Record<string, string> = {
  memory_root: "File ↔ PG drift (legacy wiki)",
  graph_sqlite: "Graph mirror drift",
};

function formatAge(seconds: number | null | undefined): string {
  if (seconds == null) return "an unknown time";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

/** Coarse age of an ISO timestamp, in the mocks' "2 d" register. */
/** Milliseconds, as a person reads them: sub-second in ms, above that in
 *  seconds to one decimal. */
function formatMs(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return "—";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

function ageSince(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const t = new Date(String(iso).replace(" ", "T")).getTime();
  if (Number.isNaN(t)) return null;
  return formatAge((Date.now() - t) / 1000);
}

async function spawnKhipu(subcommand: string): Promise<{
  ok?: boolean;
  pid?: number;
  log_path?: string;
  engine_log_path?: string;
  subcommand?: string;
}> {
  return invoke("spawn_khipu", { subcommand });
}

function prettyJson(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

function parseJson(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

type SecretsPresence = {
  dsn_in_keychain?: boolean;
  gemini_in_keychain?: boolean;
  openai_compat_in_keychain?: boolean;
};

const SECRET_PRESENCE_KEYS = [
  "dsn_in_keychain",
  "gemini_in_keychain",
  "openai_compat_in_keychain",
] as const satisfies ReadonlyArray<keyof SecretsPresence>;

function isSecretsPresence(value: unknown): value is SecretsPresence {
  if (value == null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const obj = value as Record<string, unknown>;
  if ("error" in obj) {
    return false;
  }
  for (const key of SECRET_PRESENCE_KEYS) {
    const field = obj[key];
    if (field !== undefined && typeof field !== "boolean") {
      return false;
    }
  }
  return true;
}

function pickSecretsPresence(value: SecretsPresence): SecretsPresence {
  return {
    dsn_in_keychain: value.dsn_in_keychain,
    gemini_in_keychain: value.gemini_in_keychain,
    openai_compat_in_keychain: value.openai_compat_in_keychain,
  };
}

function presenceLabel(
  presence: SecretsPresence | null,
  key: keyof SecretsPresence,
): string {
  if (presence == null) return "—";
  const value = presence[key];
  if (typeof value !== "boolean") return "—";
  return value ? "yes" : "no";
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTs(raw: string): string {
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return raw;
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const sameDay = d.toDateString() === new Date().toDateString();
  if (sameDay) return time;
  return `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

function BrandMark({ size = 26 }: { size?: number }) {
  return (
    <img
      className="brand-icon"
      src={khipuIcon}
      width={size}
      height={size}
      alt=""
      aria-hidden
      draggable={false}
      data-tauri-drag-region
    />
  );
}

function Spinner() {
  return <Loader2 size={14} className="spin" aria-hidden />;
}

type Tone = "ok" | "warn" | "err" | "neutral";

function opsStatusTone(status: string | undefined): Tone {
  if (status === "ok") return "ok";
  if (status === "fail" || status === "error" || status === "failed") {
    return "err";
  }
  return "warn";
}

function PanelHeader({
  title,
  lede,
  children,
}: {
  title: string;
  lede?: string;
  children?: ReactNode;
}) {
  return (
    // data-tauri-drag-region only fires on the exact event target, so the
    // attribute has to be repeated on every non-interactive child.
    <header className="panel-head" data-tauri-drag-region>
      <div className="panel-head-text" data-tauri-drag-region>
        <h1 data-tauri-drag-region>{title}</h1>
        {lede ? (
          <p className="lede" data-tauri-drag-region>
            {lede}
          </p>
        ) : null}
      </div>
      <div className="panel-head-actions">{children}</div>
    </header>
  );
}

/** The JSON body itself. Kept as its own piece so Home can put two of them
 *  inside one disclosure without nesting two more. */
function RawBlock({ text, empty }: { text: string; empty?: string }) {
  const body = text && text !== "\u2026" ? text : empty || "No data yet.";
  return <pre className="code tall">{body}</pre>;
}

/** Raw JSON, behind a collapsed "Advanced" disclosure. It used to sit expanded
 *  on every primary screen — a debug affordance as permanent furniture
 *  (audit 2026-09-04). */
function RawJson({
  text,
  empty,
  openKey = 0,
  label = "Advanced",
}: {
  text: string;
  empty?: string;
  openKey?: number;
  label?: string;
}) {
  return (
    <Disclosure label={label} openKey={openKey}>
      <RawBlock text={text} empty={empty} />
    </Disclosure>
  );
}

// Positive health row for a check that can be ok / red / skipped-by-design
// (graph_backup, graph_offsite). Skipped is always neutral gray — folding it
// into green is exactly the bug this row exists to avoid (audit 2026-08-31).
function renderHealthRow(
  label: string,
  check: DoctorHealthCheck | null,
  detail: (c: DoctorHealthCheck) => string,
) {
  const tone: "ok" | "err" | "muted" = !check
    ? "muted"
    : check.skipped
      ? "muted"
      : check.ok
        ? "ok"
        : "err";
  const Icon =
    tone === "ok" ? CircleCheck : tone === "err" ? TriangleAlert : CircleMinus;
  const text = !check
    ? "Run Refresh to check."
    : check.skipped
      ? `Not checked — ${check.reason ?? "not the graph producer on this Mac"}`
      : detail(check);
  return (
    <div key={label} className="row-item">
      <Icon size={16} strokeWidth={1.75} aria-hidden className={tone} />
      <span className="row-main">{label}</span>
      <span className="row-meta">{text}</span>
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>("home");
  const [dsnOk, setDsnOk] = useState<boolean | null>(null);
  const [loading, setLoading] = useState<Partial<Record<CacheTab, boolean>>>(
    {},
  );
  const [error, setError] = useState<string | null>(null);
  const fetchedAt = useRef<Partial<Record<CacheTab, number>>>({});
  const feedbackButtonRef = useRef<HTMLButtonElement>(null);

  const [statusText, setStatusText] = useState("…");
  const [counts, setCounts] = useState<Counts | null>(null);
  // Split so Status sample-0 cannot clobber Revisions-derived drift UI.
  const [statusConflicts, setStatusConflicts] =
    useState<ConflictSummary | null>(null);
  const [revisionsConflicts, setRevisionsConflicts] =
    useState<ConflictSummary | null>(null);
  const [recentCaptures, setRecentCaptures] = useState<
    Array<{ id: number; summary?: string; ts?: string; scope?: string }>
  >([]);
  const [doctorText, setDoctorText] = useState("…");
  const [doctorOk, setDoctorOk] = useState<boolean | null>(null);
  // Named failures, so a red Doctor says WHAT is red without a trip to the raw
  // JSON. Capture liveness is the one that matters most: a harness whose
  // hook runs but records nothing must be loud here (2026-08-17).
  const [doctorIssues, setDoctorIssues] = useState<string[]>([]);
  // The same failures as `doctorIssues`, but shaped for Home: plain-language
  // title, the cause in a sentence, and at most one fix action.
  const [attention, setAttention] = useState<Attention[]>([]);
  const [doctorCheckCount, setDoctorCheckCount] = useState(0);
  // Home's four tiles read these straight off the doctor payload; nothing here
  // is derived from a field the report does not carry.
  const [liveness, setLiveness] = useState<LivenessPayload | null>(null);
  // The last recorded end-to-end recall probe. Harnesses reads it for its
  // "Verified …" line; nothing about that line is app-local state.
  const [recallProbe, setRecallProbe] = useState<RecallProbeStatus | null>(null);
  const [embedCoverage, setEmbedCoverage] = useState<Record<
    string,
    { total?: number; embedded?: number; missing?: number; pct?: number }
  > | null>(null);
  // `coverage()` also reports today's embedding API budget (local to this
  // Mac) and the hub's query-vector cache; both are informational.
  const [embedBudget, setEmbedBudget] = useState<{
    calls?: number; cap?: number; remaining?: number; exhausted?: boolean;
  } | null>(null);
  const [queryCache, setQueryCache] = useState<{
    available?: boolean; rows?: number; hits?: number;
  } | null>(null);
  // `coverage()` reports the active index profile alongside the per-kind
  // rows; Settings names it rather than saying "the index" and leaving the
  // person to guess which one.
  const [embedActiveProfile, setEmbedActiveProfile] = useState<string | null>(null);
  const [backupHealth, setBackupHealth] = useState<{
    ok?: boolean;
    freshest_backup_age_seconds?: number;
  } | null>(null);
  const [snapshotHealth, setSnapshotHealth] = useState<{
    ok?: boolean;
    age_seconds?: number;
  } | null>(null);
  const [probeBusy, setProbeBusy] = useState(false);
  const [doctorJobs, setDoctorJobs] = useState<DoctorJobs | null>(null);
  // Checks the CLI never ran on this machine (`not_configured`) plus the two
  // health checks that can be skipped by design (graph_backup/graph_offsite
  // on a non-producer Mac). Kept separate from doctorIssues so a skip never
  // gets folded into either "all green" or the red issue list.
  const [doctorNotConfigured, setDoctorNotConfigured] = useState<string[]>(
    [],
  );
  const [doctorGraphBackup, setDoctorGraphBackup] =
    useState<DoctorHealthCheck | null>(null);
  const [doctorGraphOffsite, setDoctorGraphOffsite] =
    useState<DoctorHealthCheck | null>(null);
  const [doctorSkipReasons, setDoctorSkipReasons] = useState<
    Record<string, string | undefined>
  >({});
  const [jobSpawnMsg, setJobSpawnMsg] = useState<string | null>(null);
  const [revisionsText, setRevisionsText] = useState("…");
  const [activityText, setActivityText] = useState("…");
  const [activityList, setActivityList] = useState<
    Array<{
      id: number;
      summary?: string;
      ts?: string;
      ingested_at?: string;
      session_id?: string;
      scope?: string;
      mirror_age_seconds?: number;
    }>
  >([]);
  const [activityCount, setActivityCount] = useState<number | null>(null);
  // Held in a ref, not state, so Home's own `loadActivity(false)` can never
  // shrink a list the Activity screen has already paged out.
  const activityLimitRef = useRef(ACTIVITY_PAGE);
  const [activityLimit, setActivityLimit] = useState(ACTIVITY_PAGE);
  const [activityFilter, setActivityFilter] = useState("");
  const [activityProject, setActivityProject] = useState<string | null>(null);
  const [activityHarness, setActivityHarness] = useState<string | null>(null);
  const [activitySince, setActivitySince] = useState<"today" | "7d" | null>(null);
  const [activitySelected, setActivitySelected] = useState<number | null>(null);
  const [activityDetail, setActivityDetail] = useState<EpisodeDetail | null>(
    null,
  );
  const [activityDetailLoading, setActivityDetailLoading] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [editText, setEditText] = useState("");
  const [editSaving, setEditSaving] = useState(false);
  // One confirm for both screens' Forget button, through the shared Dialog.
  const [forgetTarget, setForgetTarget] = useState<number | null>(null);
  const [opsEvents, setOpsEvents] = useState<
    Array<{ kind?: string; status?: string; created_at?: string }>
  >([]);
  const [secretsPresence, setSecretsPresence] = useState<SecretsPresence | null>(
    null,
  );
  const [secretsPresenceMsg, setSecretsPresenceMsg] = useState<string | null>(
    null,
  );
  const [geminiKey, setGeminiKey] = useState("");
  const [geminiMsg, setGeminiMsg] = useState<string | null>(null);
  const [geminiSaving, setGeminiSaving] = useState(false);
  const [models, setModels] = useState<ModelsState>(DEFAULT_MODELS);
  const [modelsMsg, setModelsMsg] = useState<string | null>(null);
  const [modelsSaving, setModelsSaving] = useState(false);
  const [openaiCompatKey, setOpenaiCompatKey] = useState("");
  const [openaiCompatMsg, setOpenaiCompatMsg] = useState<string | null>(null);
  const [openaiCompatSaving, setOpenaiCompatSaving] = useState(false);
  const [revSlug, setRevSlug] = useState("");
  const [revRecent, setRevRecent] = useState<RecentRevision[]>([]);
  const [revShowId, setRevShowId] = useState("");
  const [query, setQuery] = useState("");
  const [searchText, setSearchText] = useState("");
  // A failed search used to clear searchText, which the render reads as
  // "never searched" — the failure vanished with the toast and the panel
  // looked like an empty first run. Kept separate so it renders its own
  // inline, retryable state instead (audit 2026-08-31).
  const [searchErr, setSearchErr] = useState<string | null>(null);
  // The CLI's search default is `--mode hybrid` (cosine + token overlap +
  // literal, fused by RRF). The pane offered a "Semantic" checkbox that sent
  // the DEPRECATED `--semantic` alias when on and, when off, silently fell
  // back to literal-only — so neither position of the switch could reach the
  // mode the CLI itself considers best, and the empty state told people
  // literal keyword matching was the default (audit 2026-08-17, 2026-09-04).
  // Three named modes, hybrid first, mapped 1:1 onto `--mode`.
  const [searchMode, setSearchMode] = useState<SearchMode>("hybrid");
  const [searchProject, setSearchProject] = useState("");
  const [searchSince, setSearchSince] = useState("");
  const [searchKind, setSearchKind] = useState("");
  const [searchHarness, setSearchHarness] = useState("");
  // The mock's chips row: a chip is the applied filter, "+ Filter" reveals the
  // inputs that set them.
  const [searchFiltersOpen, setSearchFiltersOpen] = useState(false);
  // The app's own round-trip time. Not a CLI field — it is measured here and
  // labelled as what it is.
  const [searchElapsedMs, setSearchElapsedMs] = useState<number | null>(null);
  const [recallSelected, setRecallSelected] = useState<string | null>(null);
  const [recallDetail, setRecallDetail] = useState<EpisodeDetail | null>(null);
  const [recallDetailLoading, setRecallDetailLoading] = useState(false);
  const [recallNeighbors, setRecallNeighbors] = useState<GraphNeighbor[]>([]);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  // Separate from the shared `actionBusy`: the results area needs to know
  // that THIS search is in flight so it can show a loading state instead of
  // leaving the "type a query" empty state on screen (audit 2026-09-04).
  const [searchBusy, setSearchBusy] = useState(false);
  // Recall holds Search and the graph walk; the walk is a sub-view of a result,
  // not a peer destination (audit IA).
  const [recallView, setRecallView] = useState<"search" | "graph">("search");
  // One cached array per status. Home's preview, the rail badge and the Owed
  // screen's own table all read this, so a write that moves a row between two
  // statuses cannot leave one of them stale (phase 4: the segmented control
  // shows all three counts at once, so all three are fetched).
  const [owedByStatus, setOwedByStatus] = useState<
    Partial<Record<OwedStatus, Commitment[]>>
  >({});
  const [owedStatus, setOwedStatus] = useState<OwedStatus>("open");
  const [owedProject, setOwedProject] = useState<string | null>(null);
  const [owedKind, setOwedKind] = useState<string | null>(null);
  const [owedLoading, setOwedLoading] = useState(false);
  const [owedBusyId, setOwedBusyId] = useState<number | null>(null);
  const [snoozeTarget, setSnoozeTarget] = useState<Commitment | null>(null);
  // Bumped by an attention item with no fix action, and by "Details": opens
  // Home's one disclosure instead of dead-ending.
  const [homeAdvancedKey, setHomeAdvancedKey] = useState(0);
  const [owedFetchedAt, setOwedFetchedAt] = useState<Partial<Record<OwedStatus, number>>>({});
  const [nodeId, setNodeId] = useState("");
  const [hops, setHops] = useState(1);
  const [graphText, setGraphText] = useState("");
  const [actionBusy, setActionBusy] = useState(false);
  const [activityRawKey, setActivityRawKey] = useState(0);
  const [revisionsRawKey, setRevisionsRawKey] = useState(0);

  // Declared here, not next to the render, because the rail-badge effect below
  // reads owedOpenCount in its dependency list.
  const owedOpenRows = owedByStatus.open ?? [];
  // The rail badge and Home's preview count "Needs you" ONLY — the global
  // open total is ~330 rows of the agent's own plan steps, which is exactly
  // the number that got the screen ignored (phase 4 addendum).
  const needsYouRows = owedOpenRows.filter((c) => owedGroupOf(c) === "needs-you");
  const owedOpenCount = owedByStatus.open ? needsYouRows.length : null;
  const owedRows = owedByStatus[owedStatus] ?? [];

  const markLoading = (key: CacheTab, on: boolean) => {
    setLoading((prev) => ({ ...prev, [key]: on }));
  };

  const needsFetch = (key: CacheTab, force: boolean) => {
    if (force) return true;
    const at = fetchedAt.current[key];
    if (at == null) return true;
    return Date.now() - at > CACHE_TTL_MS;
  };

  const refreshDsn = useCallback(async (force = false) => {
    try {
      const ok = await invoke<boolean>("dsn_configured", { force });
      setDsnOk(ok);
      if (!ok) setTab("first-run");
    } catch (e) {
      setDsnOk(false);
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    void refreshDsn(false);
  }, [refreshDsn]);

  // First launch: the tutorial opens itself until it has been finished once.
  // A missing DSN also routes here (refreshDsn), so a broken setup lands on
  // the step that explains it rather than on an empty Status pane.
  useEffect(() => {
    if (!welcomeCompleted()) setTab("first-run");
  }, []);
  const loadStatus = useCallback(async (force = false) => {
    if (!needsFetch("status", force)) return;
    markLoading("status", true);
    setError(null);
    try {
      // Default --sample 0: PG stats only (no NAS file-hash walk).
      const raw = await runKhipu(["status"]);
      const parsed = parseJson(raw) as {
        counts?: Counts;
        conflicts?: ConflictSummary;
        recent_captures?: Array<{
          id: number;
          summary?: string;
          ts?: string;
          scope?: string;
        }>;
      } | null;
      if (
        parsed === null ||
        typeof parsed !== "object" ||
        Array.isArray(parsed)
      ) {
        // Keep last-good KPIs; do not stamp fetchedAt so retry is not TTL-blocked.
        setError("Unexpected response from hub");
        return;
      }
      setStatusText(prettyJson(raw));
      setCounts(parsed.counts ?? null);
      setStatusConflicts(parsed.conflicts ?? null);
      setRecentCaptures(parsed.recent_captures ?? []);
      fetchedAt.current.status = Date.now();
    } catch (e) {
      // Preserve last-good KPIs/lists; toast only.
      setError(String(e));
    } finally {
      markLoading("status", false);
    }
  }, []);

  const loadSecretsPresence = useCallback(async () => {
    try {
      const raw = await invoke<string>("secrets_presence");
      const parsed = parseJson(raw);
      if (!isSecretsPresence(parsed)) {
        setSecretsPresence(null);
        setSecretsPresenceMsg("Secrets presence unknown.");
        return;
      }
      setSecretsPresence(pickSecretsPresence(parsed));
      setSecretsPresenceMsg(null);
    } catch {
      setSecretsPresence(null);
      setSecretsPresenceMsg("Could not read secrets presence.");
    }
  }, []);

  const saveGeminiKey = useCallback(async () => {
    const value = geminiKey.trim();
    if (!value) {
      setGeminiMsg("Enter a key first.");
      return;
    }
    setGeminiSaving(true);
    setGeminiMsg(null);
    try {
      // Dedicated command, not runKhipu: the value goes to the CLI's stdin so it
      // never appears in argv. Clear it from component state on success so the
      // key does not sit in the renderer any longer than the request needs.
      const raw = await invoke<string>("set_khipu_secret", {
        account: "gemini_api_key",
        value,
      });
      const parsed = parseJson(raw) as { ok?: boolean; error?: string } | null;
      if (parsed?.ok) {
        setGeminiKey("");
        setGeminiMsg("Saved to Keychain.");
        fetchedAt.current.activity = undefined;
        await loadSecretsPresence();
        await loadStatus(true);
      } else {
        setGeminiMsg(parsed?.error ?? "Could not save the key.");
      }
    } catch (e) {
      setGeminiMsg(String(e));
    } finally {
      setGeminiSaving(false);
    }
  }, [geminiKey, loadSecretsPresence, loadStatus]);

  const saveOpenaiCompatKey = useCallback(async () => {
    const value = openaiCompatKey.trim();
    if (!value) {
      setOpenaiCompatMsg("Enter a key first (or leave blank for Ollama).");
      return;
    }
    setOpenaiCompatSaving(true);
    setOpenaiCompatMsg(null);
    try {
      const raw = await invoke<string>("set_khipu_secret", {
        account: "openai_compat_api_key",
        value,
      });
      const parsed = parseJson(raw) as { ok?: boolean; error?: string } | null;
      if (parsed?.ok) {
        setOpenaiCompatKey("");
        setOpenaiCompatMsg("Saved to Keychain.");
        fetchedAt.current.activity = undefined;
        await loadSecretsPresence();
        await loadStatus(true);
      } else {
        setOpenaiCompatMsg(parsed?.error ?? "Could not save the key.");
      }
    } catch (e) {
      setOpenaiCompatMsg(String(e));
    } finally {
      setOpenaiCompatSaving(false);
    }
  }, [openaiCompatKey, loadSecretsPresence, loadStatus]);

  const loadDoctor = useCallback(async (force = false) => {
    if (!needsFetch("doctor", force)) return;
    markLoading("doctor", true);
    setError(null);
    try {
      const raw = await runKhipu(["doctor"]);
      const parsed = parseJson(raw) as { ok?: boolean } | null;
      if (
        parsed === null ||
        typeof parsed !== "object" ||
        Array.isArray(parsed)
      ) {
        // Keep last-good doctorOk; do not stamp fetchedAt so retry is not TTL-blocked.
        setError("Unexpected response from hub");
        return;
      }
      setDoctorText(prettyJson(raw));
      setDoctorOk(typeof parsed.ok === "boolean" ? parsed.ok : null);
      const issues: string[] = [];
      const items: Attention[] = [];
      const lv = (parsed as { capture_liveness?: LivenessPayload })
        .capture_liveness;
      setLiveness(lv ?? null);
      setRecallProbe(
        (parsed as { recall_probe?: RecallProbeStatus }).recall_probe ?? null,
      );
      const cov = (parsed as {
        embed_coverage?: Record<
          string,
          { total?: number; embedded?: number; missing?: number; pct?: number }
        > & { active_profile?: unknown; budget?: unknown; query_cache?: unknown };
      }).embed_coverage;
      setEmbedCoverage(cov ?? null);
      setEmbedActiveProfile(
        typeof cov?.active_profile === "string" ? cov.active_profile : null,
      );
      const budget = cov?.budget;
      setEmbedBudget(
        budget && typeof budget === "object" && !Array.isArray(budget)
          ? (budget as { calls?: number; cap?: number; remaining?: number; exhausted?: boolean })
          : null,
      );
      const qc = cov?.query_cache;
      setQueryCache(
        qc && typeof qc === "object" && !Array.isArray(qc)
          ? (qc as { available?: boolean; rows?: number; hits?: number })
          : null,
      );
      setBackupHealth(
        (parsed as { backup?: { ok?: boolean; freshest_backup_age_seconds?: number } }).backup ?? null,
      );
      setSnapshotHealth(
        (parsed as { hub_snapshot?: { ok?: boolean; age_seconds?: number } }).hub_snapshot ?? null,
      );
      if (lv && lv.ok === false) {
        for (const h of lv.red ?? []) {
          const why = (lv.harnesses?.[h]?.reasons ?? []).join("; ");
          issues.push(`Not recording ${h}: ${why || "see report"}`);
          items.push({
            key: `liveness:${h}`,
            tone: "err",
            title: `${harnessLabel(h)} has stopped recording sessions`,
            cause: why
              ? `${why}. Reinstalling the hook puts it back without losing the session.`
              : "Its capture hook is no longer reporting. Reinstalling the hook puts it back without losing the session.",
            fix: { label: "Reinstall hook", kind: "reinstall-hook" },
            harness: h,
          });
        }
      }
      const gs = (parsed as { git_sync?: { ok?: boolean; reasons?: string[] } }).git_sync;
      if (gs && gs.ok === false) {
        const why = (gs.reasons ?? []).join("; ");
        issues.push(`Git sync not landing: ${why || "see report"}`);
        items.push({
          key: "git_sync",
          tone: "warn",
          title: "The nightly copy of your notes is not reaching GitHub",
          cause: why
            ? `${why}. Everything is still in the database; only the off-site copy of the note files is behind.`
            : "Everything is still in the database; only the off-site copy of the note files is behind.",
        });
      }
      if ((parsed as { drift_ok?: boolean }).drift_ok === false) {
        issues.push("Out-of-sync files");
        items.push({
          key: "drift",
          tone: "warn",
          title: "Some note files no longer match the database",
          cause:
            "A file was edited in two places, so one version is not the one search returns. Conflicting edits lists them.",
          fix: { label: "Conflicting edits", kind: "revisions" },
        });
      }
      if ((parsed as { graph_drift_ok?: boolean }).graph_drift_ok === false) {
        issues.push("Graph mirror drift");
        items.push({
          key: "graph_drift",
          tone: "warn",
          title: "The connections index is behind its source",
          cause:
            "The graph builder's own copy and the database disagree. The next graph build reconciles them; run it from the health report below.",
        });
      }
      if ((parsed as { outbox_ok?: boolean }).outbox_ok === false) {
        issues.push("Captures waiting to sync");
        items.push({
          key: "outbox",
          tone: "warn",
          title: "Some captures are still waiting to reach the database",
          cause:
            "They were recorded while the database was unreachable and are queued on this Mac. Refresh retries them; nothing is lost meanwhile.",
        });
      }
      if ((parsed as { backup_ok?: boolean }).backup_ok === false) {
        issues.push("Backup test");
        items.push({
          key: "backup",
          tone: "err",
          title: "The newest backup is older than it should be",
          cause:
            "Backups run on a schedule off this Mac. Until one lands, a restore would lose recent sessions.",
        });
      }
      if ((parsed as { graph_backup_ok?: boolean }).graph_backup_ok === false) {
        issues.push("Graph snapshot");
        items.push({
          key: "graph_backup",
          tone: "warn",
          title: "The connections snapshot is stale",
          cause: "The last saved copy of the connections index is older than its limit.",
        });
      }
      if ((parsed as { graph_offsite_ok?: boolean }).graph_offsite_ok === false) {
        issues.push("Graph offsite");
        items.push({
          key: "graph_offsite",
          tone: "warn",
          title: "The off-site copy of the connections index is stale",
          cause: "The most recent copy sent off this Mac is older than its limit.",
        });
      }
      if ((parsed as { index_freshness_ok?: boolean }).index_freshness_ok === false) {
        issues.push("Memory index stale vs nightly");
        items.push({
          key: "index_freshness",
          tone: "warn",
          title: "The memory index has not been rebuilt since the last nightly run",
          cause: "Recall still works; the summary index an agent reads first is a day behind.",
        });
      }
      if ((parsed as { embed_coverage_ok?: boolean }).embed_coverage_ok === false) {
        issues.push("Search index catching up");
        const cov = (parsed as {
          embed_coverage?: Record<string, { missing?: number }>;
        }).embed_coverage;
        const missing = cov
          ? Object.entries(cov)
              .filter(([, v]) => v && typeof v === "object" && (v.missing ?? 0) > 0)
              .map(([k, v]) => `${v.missing} ${k}`)
              .join(", ")
          : "";
        items.push({
          key: "embed_coverage",
          tone: "warn",
          title: "The search index is behind",
          cause: missing
            ? `${missing} not indexed yet. They are still findable by their exact words; search by meaning misses them until the nightly catches up.`
            : "Some rows are not indexed yet. They are still findable by their exact words until the nightly catches up.",
        });
      }
      // Every red field doctor aggregates into `ok` needs a named issue here,
      // or the card says "Issues found" over an empty list and the person is
      // sent to the raw JSON to find out what broke (audit 2026-09-04).
      if ((parsed as { recall_probe_ok?: boolean }).recall_probe_ok === false) {
        const rp = (parsed as { recall_probe?: { reason?: string; error?: string } })
          .recall_probe;
        const why = rp?.reason || rp?.error;
        issues.push(
          `Recall probe failed or is stale: run a probe${why ? ` (${why})` : ""}`,
        );
        items.push({
          key: "recall_probe",
          tone: "err",
          title: "Nothing has proved that recall works end to end lately",
          cause: why
            ? `${why}. The probe records a session, searches for it and removes it again.`
            : "The check that records a session, searches for it and removes it again has not run in the last seven days.",
          fix: { label: "Run recall probe", kind: "recall-probe" },
        });
      }
      if ((parsed as { bundle_seal_ok?: boolean }).bundle_seal_ok === false) {
        issues.push("App bundle signature is broken: reinstall from the DMG");
        items.push({
          key: "bundle_seal",
          tone: "err",
          title: "This copy of Khipu has been altered since it was signed",
          cause:
            "macOS will refuse to open it after the next restart. Reinstall from the downloaded disk image to repair it.",
        });
      }
      if ((parsed as { dsn_file_ok?: boolean }).dsn_file_ok === false) {
        issues.push("Database connection file missing or unreadable");
        items.push({
          key: "dsn_file",
          tone: "err",
          title: "The saved database connection cannot be read",
          cause:
            "Harnesses that cannot reach the Keychain fall back to this file. Set the connection again under Settings.",
        });
      }
      const snap = (parsed as { hub_snapshot?: { ok?: boolean; reason?: string } })
        .hub_snapshot;
      if (snap && snap.ok === false) {
        issues.push(
          `Offline copy is stale${snap.reason ? `: ${snap.reason}` : ""}`,
        );
        items.push({
          key: "hub_snapshot",
          tone: "warn",
          title: "The offline copy of your memory is behind",
          cause:
            "It is what recall falls back to when the database is unreachable. Refreshing it is a Settings action.",
        });
      }
      setDoctorJobs((parsed as { jobs?: DoctorJobs }).jobs ?? null);
      const notConfiguredRaw = (parsed as { not_configured?: unknown })
        .not_configured;
      setDoctorNotConfigured(
        Array.isArray(notConfiguredRaw)
          ? notConfiguredRaw.filter((x): x is string => typeof x === "string")
          : [],
      );
      setDoctorGraphBackup(
        (parsed as { graph_backup?: DoctorHealthCheck }).graph_backup ??
          null,
      );
      setDoctorGraphOffsite(
        (parsed as { graph_offsite?: DoctorHealthCheck }).graph_offsite ??
          null,
      );
      setDoctorSkipReasons({
        memory_root: (parsed as { drift?: { skipped?: string } }).drift
          ?.skipped,
        graph_sqlite: (parsed as { graph_drift?: { skipped?: string } })
          .graph_drift?.skipped,
      });
      setDoctorIssues(issues);
      setAttention(items);
      // The number of boolean `*_ok` verdicts in this report — what "all
      // checks passed" is actually a claim about.
      setDoctorCheckCount(
        Object.entries(parsed as Record<string, unknown>).filter(
          ([k, v]) => k.endsWith("_ok") && typeof v === "boolean",
        ).length,
      );
      fetchedAt.current.doctor = Date.now();
    } catch (e) {
      // Preserve last-good doctorOk/text; toast only.
      setError(String(e));
    } finally {
      markLoading("doctor", false);
    }
  }, []);


  /** `khipu doctor --probe` — the only doctor invocation that WRITES: it runs
   * a fresh capture-then-search round trip and records the result, which
   * plain `doctor` only reads. Without a button here the recall_probe check
   * could only go stale and stay red (audit 2026-09-04). */
  const runRecallProbe = useCallback(async () => {
    setProbeBusy(true);
    setError(null);
    try {
      await runKhipu(["doctor", "--probe"]);
    } catch (e) {
      // A probe that exits non-zero is a FAILED probe, not a broken button —
      // the reload below shows the recorded verdict either way.
      setError(String(e));
    } finally {
      setProbeBusy(false);
      await loadDoctor(true);
    }
  }, [loadDoctor]);

  /** `khipu owed` — the commitments a session left behind, cached per status
   *  so the rail badge, Home's preview and this screen's table are one fetch
   *  rather than three racing over one array. */
  const loadOwed = useCallback(
    async (status: OwedStatus, force = false) => {
      const at = owedFetchedAt[status];
      if (!force && at != null && Date.now() - at < CACHE_TTL_MS) return;
      setOwedLoading(true);
      try {
        const raw = await runKhipu([
          "owed",
          `--status=${status}`,
          `--limit=${OWED_LIMIT}`,
        ]);
        const parsed = parseJson(raw);
        const rows = Array.isArray(parsed) ? (parsed as Commitment[]) : [];
        setOwedByStatus((prev) => ({ ...prev, [status]: rows }));
        setOwedFetchedAt((prev) => ({ ...prev, [status]: Date.now() }));
      } catch (e) {
        setError(String(e));
      } finally {
        setOwedLoading(false);
      }
    },
    [owedFetchedAt],
  );

  /** Done / Reopen / Snooze. Every write refetches all three statuses, because
   *  each one can move a row between them — a stale count on the segmented
   *  control is the same lie as a stale row in the table. */
  const owedWrite = useCallback(
    async (id: number, args: string[]) => {
      setOwedBusyId(id);
      setError(null);
      try {
        const raw = await runKhipu(["owed", ...args]);
        const parsed = parseJson(raw) as
          | { ok?: boolean; error?: string }
          | null;
        if (parsed && parsed.ok === false) {
          setError(parsed.error ?? `Commitment #${id} was not updated.`);
        }
      } catch (e) {
        setError(String(e));
      } finally {
        setOwedBusyId(null);
        await Promise.all([
          loadOwed("open", true),
          loadOwed("closed", true),
          loadOwed("stale", true),
        ]);
      }
    },
    [loadOwed],
  );

  /** Home's one fix action for a harness that stopped recording: the same
   *  install the Harnesses screen runs, then a fresh health read. */
  const reinstallHook = useCallback(
    async (harness: string) => {
      setActionBusy(true);
      setError(null);
      try {
        await runKhipu(["integrations", "install", harness]);
      } catch (e) {
        setError(String(e));
      } finally {
        setActionBusy(false);
        await loadDoctor(true);
      }
    },
    [loadDoctor],
  );

  const loadActivity = useCallback(async (force = false, limit?: number) => {
    if (limit != null) activityLimitRef.current = limit;
    if (!needsFetch("activity", force)) return;
    markLoading("activity", true);
    setError(null);
    try {
      const raw = await runKhipu([
        "activity",
        `--limit=${activityLimitRef.current}`,
      ]);
      const parsed = parseJson(raw) as {
        episode_count?: number;
        recent?: Array<{
          id: number;
          summary?: string;
          ts?: string;
          ingested_at?: string;
          session_id?: string;
          scope?: string;
          mirror_age_seconds?: number;
        }>;
        ops_events?: Array<{
          kind?: string;
          status?: string;
          created_at?: string;
        }>;
        secrets?: unknown;
      } | null;
      if (
        parsed === null ||
        typeof parsed !== "object" ||
        Array.isArray(parsed)
      ) {
        // Keep last-good lists/KPIs; do not stamp fetchedAt so retry is not TTL-blocked.
        setError("Unexpected response from hub");
        return;
      }
      setActivityText(prettyJson(raw));
      setActivityList(parsed.recent ?? []);
      setActivityCount(
        typeof parsed.episode_count === "number" ? parsed.episode_count : null,
      );
      setOpsEvents(parsed.ops_events ?? []);
      if (isSecretsPresence(parsed.secrets)) {
        setSecretsPresence(pickSecretsPresence(parsed.secrets));
        setSecretsPresenceMsg(null);
      } else {
        setSecretsPresence(null);
        setSecretsPresenceMsg("Secrets presence unknown.");
      }
      fetchedAt.current.activity = Date.now();
    } catch (e) {
      // Preserve last-good activity lists/KPIs; toast only.
      setError(String(e));
    } finally {
      markLoading("activity", false);
    }
  }, []);

  const loadRevisions = useCallback(
    async (force = false) => {
      if (!needsFetch("revisions", force) && !revSlug.trim()) return;
      markLoading("revisions", true);
      setError(null);
      try {
        // No --sample: the pane compares every topic. Passing 40 here checked
        // the alphabetically-first 40 of 622 and then rendered "Drift & LWW"
        // green for the whole corpus (audit 2026-08-17). The full pass costs
        // 1.3 s on this machine, which is the right trade for a pane that only
        // runs when someone opens it.
        const args = ["revisions", "--limit", "40"];
        if (revSlug.trim()) {
          args.push("--slug", revSlug.trim());
        }
        const raw = await runKhipu(args);
        const parsed = parseJson(raw) as
          | { conflicts?: ConflictSummary; recent?: RecentRevision[] }
          | null;
        if (
          parsed === null ||
          typeof parsed !== "object" ||
          Array.isArray(parsed)
        ) {
          // Keep last-good conflicts; do not stamp fetchedAt so retry is not TTL-blocked.
          setError("Unexpected response from hub");
          return;
        }
        setRevisionsText(prettyJson(raw));
        setRevisionsConflicts(parsed.conflicts ?? null);
        // The slug filter narrows `recent`, and nothing rendered `recent` — so
        // typing a slug and pressing List changed only data the pane never
        // showed, and the control looked broken (audit 2026-08-17).
        setRevRecent(
          ((parsed as { recent?: RecentRevision[] }).recent ?? []).slice(0, 40),
        );
        fetchedAt.current.revisions = Date.now();
      } catch (e) {
        // Preserve last-good revisions conflicts/text; toast only.
        setError(String(e));
      } finally {
        markLoading("revisions", false);
      }
    },
    [revSlug],
  );

  /** One full episode row, for both detail panes. `activity --show` is the
   *  only CLI read that returns decisions / open loops / where. */
  const loadEpisodeDetail = useCallback(
    async (id: number): Promise<EpisodeDetail | null> => {
      const raw = await runKhipu(["activity", `--show=${id}`]);
      const parsed = parseJson(raw);
      return parsed && typeof parsed === "object" && !Array.isArray(parsed)
        ? (parsed as EpisodeDetail)
        : null;
    },
    [],
  );

  const selectEpisode = useCallback(
    async (id: number) => {
      setActivitySelected(id);
      setActivityDetail(null);
      setActivityDetailLoading(true);
      setError(null);
      try {
        const detail = await loadEpisodeDetail(id);
        setActivityDetail(detail);
        setActivityText(prettyJson(JSON.stringify(detail ?? {}, null, 2)));
      } catch (e) {
        setError(String(e));
      } finally {
        setActivityDetailLoading(false);
      }
    },
    [loadEpisodeDetail],
  );

  /** `khipu episode edit ID --summary TEXT` — correcting a summary a model got
   *  wrong. The CLI re-embeds it in the same transaction; when it could not
   *  (no key, no profile) it says so, and so do we, rather than implying the
   *  correction is searchable when it is not. */
  const saveSummary = useCallback(async () => {
    const id = activitySelected;
    const text = editText.trim();
    if (id == null || !text) return;
    setEditSaving(true);
    setError(null);
    try {
      const raw = await runKhipu([
        "episode",
        "edit",
        String(id),
        `--summary=${text}`,
      ]);
      const parsed = parseJson(raw) as
        | { ok?: boolean; reembedded?: boolean; error?: string }
        | null;
      if (parsed && parsed.ok === false) {
        setError(parsed.error ?? `Episode #${id} was not updated.`);
        return;
      }
      setEditOpen(false);
      setActivityDetail(await loadEpisodeDetail(id));
      await loadActivity(true);
      if (parsed && parsed.reembedded === false) {
        setError(
          "Summary saved. Search by meaning picks it up at the next nightly.",
        );
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setEditSaving(false);
    }
  }, [activitySelected, editText, loadEpisodeDetail, loadActivity]);

  /** `khipu episode forget ID` — soft-deletes the episode and drops its
   * vectors. The only destructive write either pane makes, so it goes behind
   * the shared confirm Dialog (audit 2026-09-04). */
  const confirmForget = useCallback(async () => {
    const id = forgetTarget;
    if (id == null) return;
    setActionBusy(true);
    setError(null);
    try {
      const raw = await runKhipu(["episode", "forget", String(id)]);
      setActivityText(prettyJson(raw));
      setActivityRawKey((k) => k + 1);
      setForgetTarget(null);
      if (activitySelected === id) {
        setActivitySelected(null);
        setActivityDetail(null);
      }
      if (recallDetail?.id === id) {
        setRecallSelected(null);
        setRecallDetail(null);
        setRecallNeighbors([]);
      }
      await loadActivity(true);
    } catch (e) {
      setError(String(e));
    } finally {
      setActionBusy(false);
    }
  }, [forgetTarget, activitySelected, recallDetail, loadActivity]);

  const showRevision = useCallback(async (explicitId?: string) => {
    const id = Number(explicitId ?? revShowId);
    if (!Number.isFinite(id) || id <= 0) {
      setError("Enter a valid id");
      return;
    }
    setActionBusy(true);
    setError(null);
    try {
      const raw = await runKhipu(["revisions", "--show", String(id)]);
      setRevisionsText(prettyJson(raw));
      setRevisionsRawKey((k) => k + 1);
      setRevShowId(String(id));
    } catch (e) {
      setError(String(e));
    } finally {
      setActionBusy(false);
    }
  }, [revShowId]);

  /** `override` is how Activity's "Open in Recall" searches for an episode id
   *  without waiting a render for the query and mode it just set. */
  const doSearch = useCallback(
    async (override?: { query?: string; mode?: SearchMode }) => {
      const q = (override?.query ?? query).trim();
      const mode = override?.mode ?? searchMode;
      if (!q) return;
      setActionBusy(true);
      setSearchBusy(true);
      setError(null);
      setSearchErr(null);
      setRecallSelected(null);
      setRecallDetail(null);
      setRecallNeighbors([]);
      const startedAt = Date.now();
      try {
        // Always `--mode`, never the deprecated `--semantic` alias. Filters are
        // only passed when non-empty (an empty `--project=` matches nothing)
        // and always as one `--name=value` token, so a value beginning with a
        // dash cannot be read as a flag.
        const args = ["search", "--mode", mode, "--limit", "20"];
        if (searchProject.trim()) args.push(`--project=${searchProject.trim()}`);
        if (searchSince.trim()) args.push(`--since=${searchSince.trim()}`);
        if (searchKind) args.push(`--kind=${searchKind}`);
        if (searchHarness.trim()) args.push(`--harness=${searchHarness.trim()}`);
        args.push("--", q);
        const raw = await runKhipu(args);
        setSearchText(prettyJson(raw));
        setSearchElapsedMs(Date.now() - startedAt);
      } catch (e) {
        // Keep the query and any prior results — only the new attempt failed —
        // and surface it inline (below) so it survives the toast timing out.
        setError(String(e));
        setSearchErr(String(e));
      } finally {
        setActionBusy(false);
        setSearchBusy(false);
      }
    },
    [query, searchMode, searchProject, searchSince, searchKind, searchHarness],
  );

  /** `explicitId` is how a Recall result opens its own neighbourhood — the
   *  graph view used to be reachable only by pasting an id you had to already
   *  know (audit 2026-09-04). */
  const doGraph = useCallback(async (explicitId?: string) => {
    const target = (explicitId ?? nodeId).trim();
    if (!target) return;
    setActionBusy(true);
    setError(null);
    try {
      const raw = await runKhipu([
        "graph",
        "--hops",
        String(clampHops(hops)),
        "--limit",
        "40",
        "--",
        target,
      ]);
      setGraphText(prettyJson(raw));
    } catch (e) {
      setError(String(e));
      setGraphText("");
    } finally {
      setActionBusy(false);
    }
  }, [nodeId, hops]);

  /** Fill the detail pane for one hit: the full episode row when it is an
   *  episode, and its one-hop neighbourhood either way. A topic or node has no
   *  CLI read beyond what the search payload already carried, so its pane
   *  shows that and its neighbours — nothing invented to fill the space. */
  const selectRecallResult = useCallback(
    async (r: SearchResult) => {
      const id = String(r.id ?? r.label ?? "");
      if (!id) return;
      setRecallSelected(`${r.kind ?? "item"}:${id}`);
      setRecallDetail(null);
      setRecallNeighbors([]);
      setRecallDetailLoading(true);
      try {
        if (r.kind === "episode") {
          setRecallDetail(await loadEpisodeDetail(Number(id)));
        }
        const raw = await runKhipu([
          "graph",
          "--hops",
          "1",
          "--limit",
          "20",
          "--",
          id,
        ]);
        setRecallNeighbors(graphNeighborsFrom(parseJson(raw)).neighbors);
      } catch (e) {
        setError(String(e));
      } finally {
        setRecallDetailLoading(false);
      }
    },
    [loadEpisodeDetail],
  );

  const copyId = useCallback(async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopiedId(value);
      window.setTimeout(
        () => setCopiedId((c) => (c === value ? null : c)),
        1500,
      );
    } catch (e) {
      setError(String(e));
    }
  }, []);

  /** Activity → Recall: the episode id, matched as exact words. */
  const openInRecall = useCallback(
    (id: number) => {
      setTab("recall");
      setRecallView("search");
      setSearchMode("literal");
      setQuery(String(id));
      void doSearch({ query: String(id), mode: "literal" });
    },
    [doSearch],
  );

  /** Owed → Activity: the capture that opened the commitment. */
  const openInActivity = useCallback(
    (id: number) => {
      setTab("activity");
      void selectEpisode(id);
    },
    [selectEpisode],
  );

  useEffect(() => {
    if (!dsnOk) return;
    // Fire-and-forget: never block tab paint on CLI. Home is the merged
    // Status + Doctor screen, so it pulls all three read paths.
    if (tab === "home") {
      void loadStatus(false);
      void loadDoctor(false);
      void loadActivity(false);
    }
    // Harnesses reads capture liveness and the stored recall probe out of the
    // health report, so it needs the same read Home does.
    if (tab === "harnesses") void loadDoctor(false);
    if (tab === "revisions") void loadRevisions(false);
    if (tab === "activity") void loadActivity(false);
    if (tab === "owed") {
      // All three, because the segmented control shows all three counts.
      void loadOwed(owedStatus, false);
      void loadOwed("open", false);
      void loadOwed("closed", false);
      void loadOwed("stale", false);
    }
  }, [
    tab,
    dsnOk,
    owedStatus,
    loadStatus,
    loadDoctor,
    loadRevisions,
    loadActivity,
    loadOwed,
  ]);

  // The rail's Owed badge is a count of open commitments, so it has to be
  // known before anyone opens Owed.
  useEffect(() => {
    if (!dsnOk || owedOpenCount != null) return;
    void loadOwed("open", false);
  }, [dsnOk, owedOpenCount, loadOwed]);

  /** Six destinations, one per named job (audit IA). Revisions is reachable
   *  from Home, not from here; Welcome is a first-run flow with a "Run setup
   *  again" button in Settings. */
  const navItems = useMemo(
    () =>
      [
        ["home", "Home", "Is memory working right now?"],
        ["recall", "Recall", "What does the agent remember about this?"],
        ["owed", "Owed", "What you still owe on each project"],
        ["activity", "Activity", "Every session Khipu recorded"],
        ["harnesses", "Harnesses", "Claude Code · Cursor · Aegis · Codex"],
        ["settings", "Settings", "Capture, models, data and this Mac"],
      ] as const,
    [],
  );

  const [dataDir, setDataDir] = useState("");
  const [dataFiles, setDataFiles] = useState<
    Array<{ path: string; bytes: number }>
  >([]);
  const [backupOut, setBackupOut] = useState("~/Downloads");
  const [importSource, setImportSource] = useState("");
  const [pathsMsg, setPathsMsg] = useState<string | null>(null);
  const [graphSources, setGraphSources] = useState<
    Array<{
      id: string;
      kind: string;
      enabled?: boolean;
      root?: string;
      embed_media?: boolean;
    }>
  >([]);
  const [graphSourcesResolved, setGraphSourcesResolved] = useState<{
    unreachable?: Array<{ id?: string; root?: string }>;
  } | null>(null);
  const [graphSourcesProducer, setGraphSourcesProducer] = useState(true);
  const [graphSourcesMsg, setGraphSourcesMsg] = useState<string | null>(null);
  const [newCodeRoot, setNewCodeRoot] = useState("");
  const [appVersion, setAppVersion] = useState<string>("…");
  const [postUpdateNotice, setPostUpdateNotice] = useState<PostUpdateNotice | null>(null);
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [feedbackSending, setFeedbackSending] = useState(false);
  const [updateMsg, setUpdateMsg] = useState<string | null>(null);
  const [updateBusy, setUpdateBusy] = useState(false);
  const [setupJoinPassphrase, setSetupJoinPassphrase] = useState("");
  const [setupJoinMsg, setSetupJoinMsg] = useState<string | null>(null);
  const [setupJoinExpected, setSetupJoinExpected] = useState<Counts | null>(null);
  const [advertiseBusy, setAdvertiseBusy] = useState(false);
  const [advertiseInfo, setAdvertiseInfo] = useState<{
    pin?: string;
    timeout_sec?: number;
    expires_at?: number;
    ipv4?: string;
  } | null>(null);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("database");
  // "Index now" and "Restore" both do something a person cannot undo with the
  // same button, so each goes through a confirm Dialog.
  const [indexBusy, setIndexBusy] = useState(false);
  const [indexMsg, setIndexMsg] = useState<string | null>(null);
  const [indexConfirm, setIndexConfirm] = useState(false);
  const [importConfirm, setImportConfirm] = useState(false);
  const [hubSnapBusy, setHubSnapBusy] = useState(false);
  const [hubSnapshotHealth, setHubSnapshotHealth] = useState<{
    refreshed_at?: string;
    size_bytes?: number;
    bytes?: number;
    ok?: boolean;
  } | null>(null);

  const loadPaths = useCallback(async () => {
    try {
      const raw = await runKhipu(["paths"]);
      const parsed = parseJson(raw) as {
        data_dir?: string;
        files?: Array<{ path: string; bytes: number }>;
      } | null;
      setDataDir(parsed?.data_dir ?? "");
      setDataFiles(parsed?.files ?? []);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  const loadGraphSources = useCallback(async () => {
    try {
      const raw = await runKhipu(["sources", "list"]);
      const parsed = parseJson(raw) as {
        sources?: Array<{
          id: string;
          kind: string;
          enabled?: boolean;
          root?: string;
          embed_media?: boolean;
        }>;
        resolved?: { unreachable?: Array<{ id?: string; root?: string }> };
        graph_producer?: boolean;
      } | null;
      setGraphSources(parsed?.sources ?? []);
      setGraphSourcesResolved(parsed?.resolved ?? null);
      setGraphSourcesProducer(Boolean(parsed?.graph_producer));
      setGraphSourcesMsg(null);
    } catch (e) {
      setGraphSourcesMsg(String(e));
    }
  }, []);

  const loadModels = useCallback(async () => {
    try {
      const raw = await runKhipu(["models"]);
      const parsed = parseJson(raw) as Partial<ModelsState> | null;
      if (!parsed || typeof parsed !== "object") {
        setModelsMsg("Unexpected models response");
        return;
      }
      setModels({
        synth: {
          ...DEFAULT_MODELS.synth,
          ...(parsed.synth ?? {}),
        },
        embed: {
          ...DEFAULT_MODELS.embed,
          ...(parsed.embed ?? {}),
        },
        vision: {
          ...DEFAULT_MODELS.vision,
          ...(parsed.vision ?? {}),
        },
        models_error:
          typeof parsed.models_error === "string" ? parsed.models_error : null,
      });
      setModelsMsg(null);
    } catch (e) {
      setModelsMsg(String(e));
    }
  }, []);

  const updateModelRole = useCallback(
    (role: "synth" | "embed" | "vision", patch: Partial<ModelRole>) => {
      setModels((prev) => ({
        ...prev,
        [role]: { ...prev[role], ...patch },
      }));
    },
    [],
  );

  const saveModels = useCallback(async () => {
    if (models.models_error) {
      setModelsMsg(
        "Save is blocked until the stored models error is cleared.",
      );
      return;
    }
    setModelsSaving(true);
    setModelsMsg(null);
    try {
      const payload = {
        synth: models.synth,
        embed: models.embed,
        vision: models.vision,
      };
      const raw = await runKhipu(["models", "set", JSON.stringify(payload)]);
      const parsed = parseJson(raw) as {
        ok?: boolean;
        error?: string;
        models?: ModelsState;
      } | null;
      if (!parsed?.ok) {
        setModelsMsg(parsed?.error ?? "models set failed");
        return;
      }
      if (parsed.models) {
        setModels({
          synth: { ...DEFAULT_MODELS.synth, ...parsed.models.synth },
          embed: { ...DEFAULT_MODELS.embed, ...parsed.models.embed },
          vision: { ...DEFAULT_MODELS.vision, ...parsed.models.vision },
          models_error: parsed.models.models_error ?? null,
        });
      } else {
        await loadModels();
      }
      setModelsMsg("Saved.");
    } catch (e) {
      setModelsMsg(String(e));
    } finally {
      setModelsSaving(false);
    }
  }, [models, loadModels]);

  const toggleGraphSource = useCallback(
    async (id: string, enabled: boolean) => {
      if (!graphSourcesProducer) return;
      setGraphSourcesMsg(null);
      try {
        const raw = await runKhipu([
          "sources",
          enabled ? "enable" : "disable",
          id,
        ]);
        const parsed = parseJson(raw) as { ok?: boolean; error?: string } | null;
        if (!parsed?.ok) {
          setGraphSourcesMsg(parsed?.error ?? "sources command failed");
          return;
        }
        await loadGraphSources();
      } catch (e) {
        setGraphSourcesMsg(String(e));
      }
    },
    [graphSourcesProducer, loadGraphSources],
  );

  const toggleEmbedMedia = useCallback(
    async (id: string, embedMedia: boolean) => {
      if (!graphSourcesProducer) return;
      setGraphSourcesMsg(null);
      try {
        const raw = await runKhipu([
          "sources",
          "set-embed-media",
          id,
          embedMedia ? "on" : "off",
        ]);
        const parsed = parseJson(raw) as { ok?: boolean; error?: string } | null;
        if (!parsed?.ok) {
          setGraphSourcesMsg(parsed?.error ?? "set-embed-media failed");
          return;
        }
        await loadGraphSources();
      } catch (e) {
        setGraphSourcesMsg(String(e));
      }
    },
    [graphSourcesProducer, loadGraphSources],
  );

  const addGraphCodeRootPath = useCallback(
    async (path: string) => {
      const trimmed = path.trim();
      if (!graphSourcesProducer || !trimmed) return;
      setGraphSourcesMsg(null);
      try {
        const raw = await runKhipu(["sources", "add", `--root=${trimmed}`]);
        const parsed = parseJson(raw) as { ok?: boolean; error?: string } | null;
        if (!parsed?.ok) {
          setGraphSourcesMsg(parsed?.error ?? "add failed");
          return;
        }
        setNewCodeRoot("");
        await loadGraphSources();
      } catch (e) {
        setGraphSourcesMsg(String(e));
      }
    },
    [graphSourcesProducer, loadGraphSources],
  );

  const addGraphCodeRoot = useCallback(
    () => addGraphCodeRootPath(newCodeRoot),
    [addGraphCodeRootPath, newCodeRoot],
  );

  const pickGraphCodeRoot = useCallback(async () => {
    if (!graphSourcesProducer) return;
    try {
      const selected = await openDirectoryDialog({
        directory: true,
        multiple: false,
      });
      if (typeof selected === "string" && selected.trim()) {
        await addGraphCodeRootPath(selected);
      }
    } catch (e) {
      setGraphSourcesMsg(String(e));
    }
  }, [graphSourcesProducer, addGraphCodeRootPath]);

  const removeGraphSource = useCallback(
    async (id: string) => {
      if (!graphSourcesProducer) return;
      setGraphSourcesMsg(null);
      try {
        const raw = await runKhipu(["sources", "remove", id]);
        const parsed = parseJson(raw) as { ok?: boolean; error?: string } | null;
        if (!parsed?.ok) {
          setGraphSourcesMsg(parsed?.error ?? "remove failed");
          return;
        }
        await loadGraphSources();
      } catch (e) {
        setGraphSourcesMsg(String(e));
      }
    },
    [graphSourcesProducer, loadGraphSources],
  );

  useEffect(() => {
    // First-run needs this too: it tells the user which file to create, and
    // the data folder is relocatable from Settings. Loading it only for
    // Settings meant the onboarding screen named ~/.config/khipu no matter
    // where the DSN was actually supposed to go. `khipu paths` needs no DSN,
    // so it works on exactly the screen that exists because there isn't one.
    let alive = true;
    if (tab === "settings" || tab === "first-run") {
      void loadPaths();
      void loadGraphSources();
    }
    if (tab === "settings") {
      void loadModels();
      void loadSecretsPresence();
      void (async () => {
        setHubSnapBusy(true);
        try {
          const raw = await runKhipu(["status"]);
          if (!alive) return;
          const parsed = parseJson(raw) as { hub_snapshot?: typeof hubSnapshotHealth } | null;
          setHubSnapshotHealth(parsed?.hub_snapshot ?? null);
        } catch {
          if (!alive) return;
          setHubSnapshotHealth(null);
        } finally {
          if (alive) setHubSnapBusy(false);
        }
      })();
    }
    return () => {
      alive = false;
      setHubSnapBusy(false);
    };
  }, [tab, loadPaths, loadGraphSources, loadModels, loadSecretsPresence, runKhipu]);

  useEffect(() => {
    void getVersion()
      .then((v) => {
        setAppVersion(v);
        // Show the notice for this version, if any, then remember we did —
        // once per install, never re-shown on a later launch of the same
        // version (see postUpdateNotices.ts).
        const notice = noticeForUpgrade(readLastNoticedVersion(), v);
        if (notice) setPostUpdateNotice(notice);
        writeLastNoticedVersion(v);
      })
      .catch(() => setAppVersion("unknown"));
  }, []);

  const checkForUpdates = useCallback(async () => {
    setUpdateBusy(true);
    setUpdateMsg(null);
    try {
      const update = await check();
      if (!update) {
        setUpdateMsg(`Up to date (v${appVersion}).`);
        return;
      }
      setUpdateMsg(`Downloading v${update.version}…`);
      await update.downloadAndInstall();
      setUpdateMsg("Update installed — relaunching…");
      await relaunch();
    } catch (e) {
      setUpdateMsg(
        e instanceof Error
          ? e.message
          : "Update check failed (no release feed yet is OK).",
      );
    } finally {
      setUpdateBusy(false);
    }
  }, [appVersion]);

  const applyDataDir = useCallback(async () => {
    if (!dataDir.trim()) return;
    setActionBusy(true);
    setPathsMsg(null);
    try {
      const raw = await runKhipu(["paths", `--set=${dataDir.trim()}`]);
      setPathsMsg(prettyJson(raw));
      await loadPaths();
    } catch (e) {
      setError(String(e));
    } finally {
      setActionBusy(false);
    }
  }, [dataDir, loadPaths]);

  const doBackupLocal = useCallback(async () => {
    if (!backupOut.trim()) return;
    setActionBusy(true);
    setPathsMsg(null);
    try {
      const raw = await runKhipu(["backup-local", `--out=${backupOut.trim()}`]);
      setPathsMsg(prettyJson(raw));
    } catch (e) {
      setError(String(e));
    } finally {
      setActionBusy(false);
    }
  }, [backupOut]);

  const exportJoinKit = useCallback(async () => {
    const passphrase = setupJoinPassphrase.trim();
    setSetupJoinMsg(null);
    try {
      const dest = await saveFileDialog({
        defaultPath: "Khipu-join.khipujoin",
        filters: [{ name: "Khipu join kit", extensions: ["khipujoin"] }],
      });
      if (typeof dest !== "string" || !dest.trim()) return;
      const raw = await invoke<string>("join_export", {
        passphrase,
        outPath: dest.trim(),
      });
      const parsed = parseJson(raw) as {
        ok?: boolean;
        error?: string;
        out?: string;
        expected?: Counts;
      } | null;
      if (parsed?.ok !== true) {
        setSetupJoinMsg(String(parsed?.error ?? "Export failed."));
        return;
      }
      setSetupJoinExpected(parsed.expected ?? null);
      setSetupJoinMsg(`Saved join kit to ${parsed.out ?? dest}`);
    } catch (e) {
      setSetupJoinMsg(String(e));
    }
  }, [setupJoinPassphrase]);

  const startJoinAdvertise = useCallback(async () => {
    const passphrase = setupJoinPassphrase.trim();
    setAdvertiseBusy(true);
    setSetupJoinMsg(null);
    setAdvertiseInfo(null);
    try {
      const raw = await invoke<string>("join_advertise", { passphrase, timeout: 600 });
      const parsed = parseJson(raw) as {
        ok?: boolean;
        error?: string;
        pin?: string;
        timeout_sec?: number;
        expires_at?: number;
        ipv4?: string;
      } | null;
      if (parsed?.ok !== true) {
        setSetupJoinMsg(String(parsed?.error ?? "Advertise failed."));
        return;
      }
      setAdvertiseInfo({
        pin: parsed.pin,
        timeout_sec: parsed.timeout_sec,
        expires_at: parsed.expires_at,
        ipv4: typeof parsed.ipv4 === "string" ? parsed.ipv4 : undefined,
      });
      setSetupJoinMsg(
        `Advertising nearby — on the new Mac: Join existing Khipu → PIN ${parsed.pin ?? "?"} → Find nearby Mac`,
      );
    } catch (e) {
      setSetupJoinMsg(String(e));
    } finally {
      setAdvertiseBusy(false);
    }
  }, [setupJoinPassphrase]);

  const runScheduledJob = useCallback(async (subcommand: string) => {
    setJobSpawnMsg(null);
    setActionBusy(true);
    try {
      const out = await spawnKhipu(subcommand);
      setJobSpawnMsg(
        out.ok
          ? `Started ${subcommand} (pid ${out.pid ?? "?"}). Engine log: ${out.engine_log_path ?? "—"}${out.log_path ? ` (wrapper: ${out.log_path})` : ""}`
          : `Could not start ${subcommand}`,
      );
      void loadDoctor(true);
    } catch (e) {
      setError(String(e));
    } finally {
      setActionBusy(false);
    }
  }, [loadDoctor]);

  const doImportLocal = useCallback(async () => {
    if (!importSource.trim()) return;
    setActionBusy(true);
    setPathsMsg(null);
    try {
      const raw = await runKhipu(["import-local", `--source=${importSource.trim()}`]);
      setPathsMsg(prettyJson(raw));
      await loadPaths();
    } catch (e) {
      setError(String(e));
    } finally {
      setActionBusy(false);
    }
  }, [importSource, loadPaths]);

  /** "Index now" — `khipu embed backfill`, through its own dedicated Tauri
   *  command with a FIXED argv. `embed` is deliberately not in the webview's
   *  subcommand allowlist (it spends money and deletes vectors), so the
   *  button gets one exact command and no arguments of its own. */
  const runIndexNow = useCallback(async () => {
    setIndexBusy(true);
    setIndexMsg(null);
    try {
      const raw = await invoke<string>("khipu_embed_backfill");
      const parsed = parseJson(raw) as
        | { embedded?: number; chunks?: number; error?: string }
        | null;
      setIndexMsg(
        parsed && typeof parsed === "object"
          ? `Indexed ${parsed.embedded ?? 0} item(s), ${parsed.chunks ?? 0} chunk(s).`
          : prettyJson(raw),
      );
      await loadDoctor(true);
    } catch (e) {
      setIndexMsg(String(e));
    } finally {
      setIndexBusy(false);
    }
  }, [loadDoctor]);

  /** What Advanced shows as "raw configuration": only values this app already
   *  holds, never a fresh read that could disagree with the screen above it. */
  const settingsRawText = useMemo(
    () =>
      JSON.stringify(
        {
          app_version: appVersion,
          data_dir: dataDir,
          data_files: dataFiles,
          models,
          secrets_present: secretsPresence,
          search_index_profile: embedActiveProfile,
          capture_cadence: {
            turns: CAPTURE_MIN_TURNS,
            minutes: CAPTURE_MIN_MINUTES,
          },
        },
        null,
        2,
      ),
    [appVersion, dataDir, dataFiles, models, secretsPresence, embedActiveProfile],
  );

  const searchResults = useMemo<SearchResult[]>(() => {
    const parsed = parseJson(searchText);
    if (Array.isArray(parsed)) return parsed as SearchResult[];
    if (parsed && typeof parsed === "object") {
      const results = (parsed as { results?: unknown }).results;
      if (Array.isArray(results)) return results as SearchResult[];
    }
    return [];
  }, [searchText]);

  /** The engine's own per-leg timing, when the payload carries it. The chips
   *  row still shows the app's round trip; this says which leg spent it. */
  const searchTiming = useMemo<SearchTiming | null>(() => {
    const parsed = parseJson(searchText);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    const t = (parsed as { timing?: unknown }).timing;
    if (!t || typeof t !== "object" || Array.isArray(t)) return null;
    return t as SearchTiming;
  }, [searchText]);
  const searchDegraded = useMemo<string | null>(() => {
    const parsed = parseJson(searchText);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    const d = (parsed as { degraded?: unknown }).degraded;
    return typeof d === "string" && d ? d : null;
  }, [searchText]);

  const graphData = useMemo(
    () => graphNeighborsFrom(parseJson(graphText)),
    [graphText],
  );

  const openDrift = revisionsConflicts?.open_file_vs_pg ?? 0;
  const unreadableTopics = revisionsConflicts?.topic_files_unreadable ?? [];
  const multiCount =
    revisionsConflicts?.topics_with_multiple_revisions?.length ?? 0;
  const anyLoading = Object.values(loading).some(Boolean) || actionBusy;
  const workingLabel = (() => {
    if (feedbackSending) return "Sending feedback…";
    if (advertiseBusy) return "Advertising a join PIN…";
    if (updateBusy) return "Checking for updates…";
    if (actionBusy) return "Working…";
    const bits: string[] = [];
    if (loading.status) bits.push("counts");
    if (loading.doctor) bits.push("health");
    if (loading.activity) bits.push("recent sessions");
    if (loading.revisions) bits.push("conflicting edits");
    if (hubSnapBusy && !loading.status && tab === "settings") bits.push("counts");
    if (bits.length) return `Reading the memory server — ${bits.join(" · ")}`;
    return null;
  })();
  const tabBusy = (id: Tab): boolean => {
    if (id === "home") {
      return Boolean(loading.status) || Boolean(loading.doctor);
    }
    if (id === "activity") return Boolean(loading.activity);
    if (id === "owed") return owedLoading;
    if (id === "revisions") return Boolean(loading.revisions);
    if (id === "settings") return hubSnapBusy;
    return false;
  };

  const panelClass = (id: Tab) =>
    tab === id ? "panel" : "panel is-hidden";

  // Total checks that never ran on this machine — not_configured entries
  // plus graph_backup/graph_offsite when this Mac isn't the graph producer.
  // A skip must never read as a clean pass (audit 2026-08-31).
  const doctorSkipCount =
    doctorNotConfigured.length +
    (doctorGraphBackup?.skipped ? 1 : 0) +
    (doctorGraphOffsite?.skipped ? 1 : 0);

  /* ---- Home tiles. Every value below comes from a field `khipu status` or
     `khipu doctor` actually returns; where one is absent the element is
     dropped rather than filled with a placeholder. -------------------------- */

  const harnessIds = liveness?.harnesses ? Object.keys(liveness.harnesses) : [];
  const harnessRed = liveness?.red ?? [];
  const recordingValue = harnessIds.length
    ? `${harnessIds.length - harnessRed.length} of ${harnessIds.length}`
    : "—";
  const recordingSub = harnessIds.length
    ? harnessIds.map(harnessLabel).join(" · ")
    : undefined;

  // The rail's health line, and the plain-language replacement for "DSN ok".
  const railHealth: { tone: "ok" | "warn" | "err"; text: string } =
    dsnOk === false
      ? { tone: "err", text: "Database not reachable" }
      : harnessRed.length === 0 && liveness != null
        ? { tone: "ok", text: "All harnesses recording" }
        : harnessRed.length > 0
          ? {
              tone: "err",
              text: `${harnessRed.length} harness${harnessRed.length === 1 ? "" : "es"} not recording`,
            }
          : { tone: "warn", text: "Checking harnesses…" };

  const coverage = (() => {
    if (!embedCoverage) return null;
    let total = 0;
    let embedded = 0;
    let missing = 0;
    for (const v of Object.values(embedCoverage)) {
      if (!v || typeof v !== "object") continue;
      if (typeof v.total !== "number") continue;
      total += v.total;
      embedded += v.embedded ?? 0;
      missing += v.missing ?? 0;
    }
    if (total === 0) return null;
    return { pct: (embedded / total) * 100, missing };
  })();

  const backupAge = backupHealth?.freshest_backup_age_seconds;
  const offsiteAge = doctorGraphOffsite?.latest?.age_seconds;
  const backupSub = [
    backupAge != null ? `Database ${formatAge(backupAge)}` : null,
    offsiteAge != null ? `off-site copy ${formatAge(offsiteAge)}` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  const jobEntries: Array<[string, JobEntry | undefined]> = [
    ["Nightly consolidate", doctorJobs?.nightly],
    ["Graph build", doctorJobs?.graph_build],
    ["Monthly consolidate", doctorJobs?.monthly],
  ];
  const jobsOk = jobEntries.filter(
    ([, e]) => e && e.plist_loaded !== false && (e.last_exit ?? 0) === 0,
  ).length;
  const nextJob = doctorJobs?.nightly?.next_schedule;
  const jobsSummary = doctorJobs
    ? `Scheduled jobs (${jobsOk} ok${nextJob ? `, next ${nextJob}` : ""}) · Full health report`
    : "Full health report";

  const openOwed = owedOpenCount ?? 0;

  // Owed: the project and kind filters offer only values the loaded rows
  // actually carry, so a chip can never select an empty list.
  const owedProjects = Array.from(
    new Set(owedRows.map((c) => c.project).filter((p): p is string => Boolean(p))),
  ).sort();
  const owedKinds = Array.from(
    new Set(owedRows.map((c) => c.kind).filter((k): k is string => Boolean(k))),
  ).sort();
  const visibleOwed = owedRows.filter(
    (c) =>
      (!owedProject || c.project === owedProject) &&
      (!owedKind || c.kind === owedKind),
  );
  const owedCount = (status: OwedStatus): string => {
    const rows = owedByStatus[status];
    if (!rows) return "";
    return ` · ${rows.length}${rows.length >= OWED_LIMIT ? "+" : ""}`;
  };
  // The mock's ok callout: commitments a later capture closed today. Built
  // only from `closed_at`, which `commitments.list_owed` returns.
  const closedToday = (owedByStatus.closed ?? []).filter((c) =>
    isToday(c.closed_at),
  );

  // Activity: the filter row narrows the fetched page in the browser; the
  // fetch itself is `--limit` only (the CLI has no --offset and no text
  // filter), which is why "Show 40 more" re-asks for a longer window.
  const activityProjects = Array.from(
    new Set(
      activityList
        .map((ep) => projectFromScope(ep.scope))
        .filter((p): p is string => Boolean(p)),
    ),
  ).sort();
  const activityHarnesses = Array.from(
    new Set(
      activityList
        .map((ep) => harnessFromSession(ep.session_id))
        .filter((h): h is string => Boolean(h)),
    ),
  ).sort();
  const activityFilterText = activityFilter.trim().toLowerCase();
  const visibleActivity = activityList.filter((ep) => {
    if (activityProject && projectFromScope(ep.scope) !== activityProject) {
      return false;
    }
    if (activityHarness && harnessFromSession(ep.session_id) !== activityHarness) {
      return false;
    }
    if (activitySince) {
      const when = parseTs(ep.ts ?? ep.ingested_at);
      if (!when) return false;
      if (activitySince === "today") {
        if (when.toDateString() !== new Date().toDateString()) return false;
      } else if (Date.now() - when.getTime() > 7 * 24 * 3600 * 1000) {
        return false;
      }
    }
    if (
      activityFilterText &&
      !(ep.summary ?? "").toLowerCase().includes(activityFilterText) &&
      !String(ep.id).includes(activityFilterText)
    ) {
      return false;
    }
    return true;
  });
  const sortedActivity = [...visibleActivity].sort((a, b) => {
    const at = parseTs(a.ts ?? a.ingested_at)?.getTime() ?? 0;
    const bt = parseTs(b.ts ?? b.ingested_at)?.getTime() ?? 0;
    return bt - at;
  });
  const activityDays: Array<{ label: string; rows: typeof activityList }> = [];
  for (const ep of sortedActivity) {
    const label = dayLabel(ep.ts ?? ep.ingested_at);
    const last = activityDays[activityDays.length - 1];
    if (last && last.label === label) last.rows.push(ep);
    else activityDays.push({ label, rows: [ep] });
  }
  /** ok / warn from the harness liveness the doctor payload already carries;
   *  a harness it says nothing about gets the neutral dot, never a green one. */
  const harnessDotClass = (sessionId: string | undefined): string => {
    const h = harnessFromSession(sessionId);
    const known = h ? liveness?.harnesses?.[h] : undefined;
    if (!known || known.ok == null) return "hdot";
    return known.ok ? "hdot ok" : "hdot warn";
  };

  // Recall: the relevance bar is relative to the top hit in THIS response —
  // the CLI's fused score has no absolute scale.
  const topScore = searchResults[0]?.score ?? null;
  const selectedResult =
    searchResults.find((r) => `${r.kind ?? "item"}:${r.id}` === recallSelected) ??
    null;
  const openLoopsFor = (episodeId: number | null | undefined) =>
    episodeId == null
      ? []
      : owedOpenRows.filter((c) => c.opened_episode === episodeId);

  /** One Owed table. Same columns for every group and every status; only the
   *  actions differ (a closed row reopens, an open one closes or snoozes). */
  const renderOwedTable = (rows: Commitment[]) => (
    <div className="card">
      <div className="card-head">
        <span className="w88">Kind</span>
        <span className="grow">What you owe</span>
        <span className="w96">Project</span>
        <span className="w52">Opened</span>
        <span className="w44">Due</span>
        <span className="w50">From</span>
        <span className="w132" />
      </div>
      {rows.map((c) => {
        // "Seen again" is evidence a later session re-stated the item; it is
        // shown only when it actually happened (seen_count > 1), and a
        // pre-migration hub answers 1 for every row.
        const seen = (c.seen_count ?? 1) > 1 ? ageSince(c.last_seen_at) : null;
        return (
          <ListRow key={c.id}>
            <span className="w88">
              <Tag kind tone={c.kind === "blocker" ? "warn" : "neutral"}>
                {OWED_KIND_LABEL[c.kind ?? ""] ?? c.kind ?? "Owed"}
              </Tag>
            </span>
            <span className="grow ellip" title={c.text}>
              {c.text}
            </span>
            {seen ? (
              <span className="meta" title={`Restated by ${c.seen_count} captures`}>
                seen {seen} ago
              </span>
            ) : null}
            <span className="w96">
              {c.project ? (
                <Tag
                  title={
                    c.project.includes(":")
                      ? `${harnessLabel(c.project.split(":")[0] ?? "")} session · ${c.project}`
                      : c.project
                  }
                >
                  {shortProject(c.project)}
                </Tag>
              ) : (
                <span className="meta">—</span>
              )}
            </span>
            <span className="meta w52">{shortDateLabel(c.opened_at)}</span>
            <span className="meta w44">{shortDateLabel(c.due_after)}</span>
            <span className="w50">
              {c.opened_episode != null ? (
                <button
                  type="button"
                  className="link sm mono"
                  title="Open the capture that opened this"
                  onClick={() => openInActivity(c.opened_episode ?? 0)}
                >
                  #{c.opened_episode}
                </button>
              ) : (
                <span className="meta mono">—</span>
              )}
            </span>
            <span className="w132 acts-cell">
              {c.status === "closed" ? (
                <button
                  type="button"
                  className="sm"
                  disabled={owedBusyId === c.id}
                  onClick={() => void owedWrite(c.id, [`--reopen=${c.id}`])}
                >
                  Reopen
                </button>
              ) : (
                <>
                  <button
                    type="button"
                    className="sm"
                    disabled={owedBusyId === c.id}
                    onClick={() => void owedWrite(c.id, [`--close=${c.id}`])}
                  >
                    Done
                  </button>
                  <button
                    type="button"
                    className="sm"
                    disabled={owedBusyId === c.id}
                    onClick={() => setSnoozeTarget(c)}
                  >
                    Snooze
                  </button>
                </>
              )}
            </span>
          </ListRow>
        );
      })}
    </div>
  );

  const renderOnDemandJobRow = (
    label: string,
    entry: JobEntry | undefined,
  ) => (
    <div key={label} className="row-item">
      <span className="row-main">{label}</span>
      <span className="row-meta">
        on demand
        {entry?.last_run_iso
          ? ` · last ${formatTs(entry.last_run_iso)}`
          : " · never"}
        {entry?.last_exit != null && entry.last_exit !== 0
          ? ` · exit ${entry.last_exit}`
          : ""}
      </span>
    </div>
  );

  const renderJobRow = (
    label: string,
    entry: JobEntry | undefined,
    spawnName: string,
  ) => (
    <div key={label} className="row-item">
      <span className="row-main">{label}</span>
      <span className="row-meta">
        {entry?.last_run_iso
          ? `last ${formatTs(entry.last_run_iso)}`
          : entry?.last_run_mtime
            ? "ran (mtime only)"
            : "never"}
        {entry?.next_schedule ? ` · next ${entry.next_schedule}` : ""}
        {entry?.plist_loaded === false ? " · agent not loaded" : ""}
        {entry?.plist_current === false ? " · needs re-render, relaunch Khipu" : ""}
        {entry?.last_exit != null && entry.last_exit !== 0
          ? ` · exit ${entry.last_exit}`
          : ""}
      </span>
      <button type="button" onClick={() => void runScheduledJob(spawnName)}>
        Run now
      </button>
    </div>
  );

  return (
    <div className="shell">
      <nav className="rail" aria-label="Primary" data-tauri-drag-region>
        <div className="rail-drag" data-tauri-drag-region />
        <div className="brand" data-tauri-drag-region>
          <BrandMark />
          <span className="brand-name" data-tauri-drag-region>
            Khipu
          </span>
        </div>
        <div className="rail-nav" data-tauri-drag-region>
          <div className="nav-group" data-tauri-drag-region>
            {navItems.map(([id, label, hint]) => {
              const badge =
                id === "owed"
                  ? openOwed > 0
                    ? { n: openOwed, quiet: true }
                    : null
                  : id === "harnesses"
                    ? harnessRed.length > 0
                      ? { n: harnessRed.length, quiet: false }
                      : null
                    : null;
              return (
                <button
                  key={id}
                  type="button"
                  className={tab === id ? "nav active" : "nav"}
                  title={hint}
                  aria-current={tab === id ? "page" : undefined}
                  onClick={() => setTab(id)}
                >
                  {NAV_ICONS[id]}
                  <span className="nav-label">{label}</span>
                  {badge ? (
                    <span
                      className={badge.quiet ? "nav-count quiet" : "nav-count"}
                    >
                      {badge.n}
                      {id === "owed" && badge.n >= OWED_LIMIT ? "+" : ""}
                    </span>
                  ) : null}
                  {tabBusy(id) ? (
                    <Loader2 size={12} className="spin nav-spin" aria-hidden />
                  ) : null}
                </button>
              );
            })}
          </div>
        </div>
        <div className="rail-foot" data-tauri-drag-region>
          <div className="rail-health">
            <span className={`hdot ${railHealth.tone}`} aria-hidden />
            <span>{railHealth.text}</span>
            {anyLoading ? (
              <Loader2 size={12} className="spin rail-spin" aria-hidden />
            ) : null}
          </div>
          <div className="rail-meta">
            <button
              ref={feedbackButtonRef}
              type="button"
              className="rail-feedback"
              onClick={() => setFeedbackOpen(true)}
            >
              <MessageSquare size={14} aria-hidden />
              Feedback
            </button>
            <span className="version-chip">v{appVersion}</span>
          </div>
        </div>
      </nav>

      <main className="main" aria-busy={workingLabel != null}>
        <WorkingBanner label={workingLabel} />
        <section className={panelClass("first-run")}>
          <div className="panel-body onboard-wrap" data-tauri-drag-region>
            <Welcome
              dsnOk={dsnOk}
              refreshDsn={() => refreshDsn(true)}
              runKhipu={runKhipu}
              onFinish={() => setTab("home")}
              openIntegrations={() => setTab("harnesses")}
            />
          </div>
        </section>

        <section className={panelClass("home")}>
          <PanelHeader title="Home" lede="Is memory working right now?">
            {loading.status || loading.doctor ? <Spinner /> : null}
            <button
              type="button"
              onClick={() => {
                void loadStatus(true);
                void loadDoctor(true);
                void loadActivity(true);
              }}
            >
              <RefreshCw size={14} strokeWidth={1.75} aria-hidden />
              Refresh
            </button>
          </PanelHeader>
          <div className="panel-body wide">
            {attention.map((a) => (
              <Callout
                key={a.key}
                tone={a.tone}
                stripe
                title={a.title}
                action={
                  <>
                    {a.fix ? (
                      <button
                        type="button"
                        className="primary sm"
                        disabled={actionBusy || probeBusy}
                        onClick={() => {
                          if (a.fix?.kind === "reinstall-hook" && a.harness) {
                            void reinstallHook(a.harness);
                          } else if (a.fix?.kind === "recall-probe") {
                            void runRecallProbe();
                          } else if (a.fix?.kind === "revisions") {
                            setTab("revisions");
                          }
                        }}
                      >
                        {a.fix.label}
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="sm"
                        onClick={() => setHomeAdvancedKey((k) => k + 1)}
                      >
                        Details
                      </button>
                    )}
                  </>
                }
              >
                {a.cause}
              </Callout>
            ))}
            {attention.length === 0 && doctorOk ? (
              <Callout
                tone="ok"
                title="Everything is recording and in sync"
                action={
                  <button
                    type="button"
                    className="sm"
                    onClick={() => setHomeAdvancedKey((k) => k + 1)}
                  >
                    Details
                  </button>
                }
              >
                {doctorCheckCount} checks passed
                {doctorSkipCount > 0
                  ? `, ${doctorSkipCount} not configured on this Mac`
                  : ""}
                .
              </Callout>
            ) : null}

            <div className="tiles">
              <Tile
                label="Database"
                value={
                  dsnOk == null
                    ? "…"
                    : dsnOk
                      ? "Connected"
                      : "Not reachable"
                }
                sub={
                  snapshotHealth?.age_seconds != null
                    ? `Offline copy ${formatAge(snapshotHealth.age_seconds)} old`
                    : undefined
                }
                tone={dsnOk === false ? "err" : "neutral"}
              />
              <Tile
                label="Recording"
                value={recordingValue}
                sub={recordingSub}
                tone={harnessRed.length > 0 ? "err" : "neutral"}
              />
              <Tile
                label="Search index"
                value={coverage ? `${coverage.pct.toFixed(1)}%` : "—"}
                sub={
                  coverage
                    ? coverage.missing > 0
                      ? `${coverage.missing} rows not indexed yet · catching up`
                      : "Everything indexed"
                    : undefined
                }
                tone={coverage && coverage.missing > 0 ? "warn" : "neutral"}
              />
              <Tile
                label="Backups"
                value={
                  backupHealth?.ok == null
                    ? "—"
                    : backupHealth.ok
                      ? "Fresh"
                      : "Stale"
                }
                sub={backupSub || undefined}
                tone={backupHealth?.ok === false ? "err" : "neutral"}
              />
            </div>

            <div className="cols">
              <div className="card col">
                <div className="card-head">
                  Recent captures
                  <span className="spacer" />
                  <button
                    type="button"
                    className="link sm"
                    onClick={() => setTab("activity")}
                  >
                    Open Activity
                  </button>
                </div>
                {recentCaptures.length === 0 ? (
                  <EmptyState
                    title="No captures yet"
                    hint="Sessions your harnesses record show up here within a minute."
                  />
                ) : (
                  recentCaptures.slice(0, 5).map((ep) => {
                    const project = projectFromScope(ep.scope);
                    return (
                      <ListRow key={ep.id}>
                        <span className="hdot ok" aria-hidden />
                        {ep.ts ? (
                          <span className="meta">{formatTs(ep.ts)}</span>
                        ) : null}
                        {project ? <Tag>{project}</Tag> : null}
                        <span className="grow ellip t2">
                          {ep.summary || `Episode ${ep.id}`}
                        </span>
                      </ListRow>
                    );
                  })
                )}
              </div>

              <div className="card col">
                <div className="card-head">
                  Needs you
                  <span className="spacer" />
                  <button
                    type="button"
                    className="link sm"
                    onClick={() => setTab("owed")}
                  >
                    Open Owed
                  </button>
                </div>
                {needsYouRows.length === 0 ? (
                  <EmptyState
                    title="Nothing needs you"
                    hint="Questions, blockers and the follow-ups you own appear here."
                  />
                ) : (
                  needsYouRows.slice(0, 4).map((c) => {
                    const age = ageSince(c.opened_at);
                    return (
                      <ListRow key={c.id}>
                        <Tag kind tone={c.kind === "blocker" ? "warn" : "neutral"}>
                          {OWED_KIND_LABEL[c.kind ?? ""] ?? c.kind ?? "Owed"}
                        </Tag>
                        <span className="grow ellip t2">{c.text}</span>
                        {age ? <span className="meta">{age}</span> : null}
                      </ListRow>
                    );
                  })
                )}
              </div>
            </div>

            <Disclosure label={jobsSummary} openKey={homeAdvancedKey}>
              {doctorJobs ? (
                <div className="rows">
                  <div className="rows-head">Scheduled jobs</div>
                  {renderJobRow("Nightly consolidate", doctorJobs.nightly, "nightly")}
                  {renderJobRow("Graph build", doctorJobs.graph_build, "graph-build")}
                  {renderJobRow("Monthly consolidate", doctorJobs.monthly, "monthly")}
                  <div className="rows-head">On demand</div>
                  {renderOnDemandJobRow(
                    "Embed media backfill",
                    doctorJobs.embed_media_backfill,
                  )}
                </div>
              ) : null}
              {jobSpawnMsg ? <pre className="code">{jobSpawnMsg}</pre> : null}

              <div className="rows">
                <div className="rows-head">Backups of the connections index</div>
                {renderHealthRow("Snapshot", doctorGraphBackup, (c) =>
                  c.ok
                    ? `Fresh — newest copy ${formatAge(c.age_seconds)} old (limit ${c.max_age_hours}h)`
                    : (c.reason ?? "stale or missing"),
                )}
                {renderHealthRow("Off-site copy", doctorGraphOffsite, (c) =>
                  c.ok
                    ? `Fresh — last copy ${formatAge(c.latest?.age_seconds)} old (limit ${c.max_age_days}d)`
                    : (c.reason ?? "stale or missing"),
                )}
              </div>

              {doctorNotConfigured.length ? (
                <div className="rows">
                  <div className="rows-head">Not checked on this Mac</div>
                  {doctorNotConfigured.map((name) => (
                    <div key={name} className="row-item">
                      <CircleMinus
                        size={16}
                        strokeWidth={1.75}
                        aria-hidden
                        className="muted"
                      />
                      <span className="row-main">
                        {NOT_CONFIGURED_LABEL[name] ?? name}
                      </span>
                      <span className="row-meta">
                        {doctorSkipReasons[name] ?? `${name} not configured`}
                      </span>
                    </div>
                  ))}
                </div>
              ) : null}

              <div className="chips">
                <Tag>episodes {counts?.episodes ?? "—"}</Tag>
                <Tag>topics {counts?.topics ?? "—"}</Tag>
                <Tag>connections {counts?.edges ?? "—"}</Tag>
                <Tag>indexed rows {counts?.embeddings ?? "—"}</Tag>
                <button
                  type="button"
                  className="link sm"
                  onClick={() => setTab("revisions")}
                  title="Topics edited in two places, and the older versions kept"
                >
                  Conflicting edits
                  {statusConflicts
                    ? ` (${statusConflicts.open_file_vs_pg ?? 0})`
                    : ""}
                </button>
              </div>

              <div className="inline">
                <button
                  type="button"
                  disabled={probeBusy}
                  title="Record a throwaway session, search for it, then forget it — proves recall works end to end."
                  onClick={() => void runRecallProbe()}
                >
                  {probeBusy ? (
                    <Loader2 size={14} className="spin" aria-hidden />
                  ) : (
                    <Stethoscope size={14} strokeWidth={1.75} aria-hidden />
                  )}
                  Run recall probe
                </button>
              </div>

              <div className="rows-head">Full health report</div>
              {doctorIssues.length ? (
                <div className="rows">
                  {doctorIssues.map((issue) => (
                    <div key={issue} className="row-item">
                      <TriangleAlert
                        size={16}
                        strokeWidth={1.75}
                        aria-hidden
                        className="warn"
                      />
                      <span className="row-main">{issue}</span>
                    </div>
                  ))}
                </div>
              ) : null}
              <RawBlock text={doctorText} />
              <div className="rows-head">Database report</div>
              <RawBlock text={statusText} />
            </Disclosure>
          </div>
        </section>

        <section className={`${panelClass("activity")} fill`}>
          <PanelHeader
            title="Activity"
            lede="Every capture, newest first. Open one to read, correct, or forget it."
          >
            {loading.activity ? <Spinner /> : null}
            <button type="button" onClick={() => void loadActivity(true)}>
              <RefreshCw size={14} strokeWidth={1.75} aria-hidden />
              Refresh
            </button>
          </PanelHeader>
          <div className="panel-body wide">
            <div className="inline">
              <input
                className="activity-filter"
                value={activityFilter}
                onChange={(e) => setActivityFilter(e.target.value)}
                placeholder="Filter captures"
                aria-label="Filter captures"
              />
              <div className="chips">
                {activityProjects.map((proj) => (
                  <Chip
                    key={`ap-${proj}`}
                    on={activityProject === proj}
                    onClick={() =>
                      setActivityProject(activityProject === proj ? null : proj)
                    }
                    onRemove={
                      activityProject === proj
                        ? () => setActivityProject(null)
                        : undefined
                    }
                  >
                    Project · {proj}
                  </Chip>
                ))}
                {activityHarnesses.map((h) => (
                  <Chip
                    key={`ah-${h}`}
                    on={activityHarness === h}
                    onClick={() =>
                      setActivityHarness(activityHarness === h ? null : h)
                    }
                    onRemove={
                      activityHarness === h
                        ? () => setActivityHarness(null)
                        : undefined
                    }
                  >
                    Harness · {harnessLabel(h)}
                  </Chip>
                ))}
                {(["today", "7d"] as const).map((w) => (
                  <Chip
                    key={`as-${w}`}
                    on={activitySince === w}
                    onClick={() =>
                      setActivitySince(activitySince === w ? null : w)
                    }
                    onRemove={
                      activitySince === w
                        ? () => setActivitySince(null)
                        : undefined
                    }
                  >
                    Since · {w === "today" ? "Today" : "7 days"}
                  </Chip>
                ))}
              </div>
              <span className="meta push">
                {visibleActivity.length === activityList.length
                  ? `${activityList.length} loaded`
                  : `${visibleActivity.length} of ${activityList.length} loaded`}
                {activityCount != null
                  ? ` · ${activityCount.toLocaleString()} captures in all`
                  : ""}
              </span>
            </div>

            <div className="split">
              <div className="card col">
                <div className="card-scroll">
                  {loading.activity && activityList.length === 0 ? (
                    <EmptyState
                      icon={<Loader2 size={22} className="spin" aria-hidden />}
                      title="Reading captures…"
                    />
                  ) : visibleActivity.length === 0 ? (
                    <EmptyState
                      title={
                        activityList.length === 0
                          ? "No captures yet"
                          : "Nothing matches those filters"
                      }
                      hint={
                        activityList.length === 0
                          ? "Sessions appear here as each harness's capture hook records them."
                          : "Clear a chip or the text filter to see the rest of this page."
                      }
                    />
                  ) : (
                    activityDays.map((day) => (
                      <div key={day.label}>
                        <div className="day">{day.label}</div>
                        {day.rows.map((ep) => {
                          const harness = harnessFromSession(ep.session_id);
                          const project = projectFromScope(ep.scope);
                          return (
                            <button
                              key={ep.id}
                              type="button"
                              className={
                                activitySelected === ep.id ? "row on" : "row"
                              }
                              onClick={() => void selectEpisode(ep.id)}
                            >
                              <span className="meta w60">
                                {timeLabel(ep.ts ?? ep.ingested_at)}
                              </span>
                              <span
                                className={harnessDotClass(ep.session_id)}
                                aria-hidden
                              />
                              <span className="meta w78">
                                {harness ? harnessLabel(harness) : "—"}
                              </span>
                              {project ? (
                                <Tag title={ep.scope}>{project}</Tag>
                              ) : null}
                              <span className="grow ellip t2">
                                {ep.summary || "(no summary)"}
                              </span>
                            </button>
                          );
                        })}
                      </div>
                    ))
                  )}
                  {activityList.length >= activityLimit &&
                  (activityCount == null ||
                    activityList.length < activityCount) ? (
                    <div className="row" style={{ justifyContent: "center" }}>
                      <button
                        type="button"
                        className="link sm"
                        disabled={Boolean(loading.activity)}
                        onClick={() => {
                          const next = activityLimit + ACTIVITY_PAGE;
                          setActivityLimit(next);
                          void loadActivity(true, next);
                        }}
                      >
                        Show {ACTIVITY_PAGE} more
                      </button>
                    </div>
                  ) : null}
                </div>
              </div>

              <div className="card col">
                {activityDetailLoading ? (
                  <EmptyState
                    icon={<Loader2 size={22} className="spin" aria-hidden />}
                    title="Opening…"
                  />
                ) : activityDetail ? (
                  <>
                    <div className="card-head">
                      Episode {activityDetail.id}
                      <span className="spacer" />
                      <span className="meta">
                        {timeLabel(activityDetail.ts)}
                        {harnessFromSession(activityDetail.session_id)
                          ? ` · ${harnessLabel(
                              harnessFromSession(activityDetail.session_id) ??
                                "",
                            )}`
                          : ""}
                      </span>
                    </div>
                    <div className="detail">
                      <div className="section">
                        <h3>Summary</h3>
                        <p>
                          {activityDetail.summary || "No summary was recorded."}
                        </p>
                      </div>
                      {asStrings(activityDetail.decisions).length > 0 ? (
                        <div className="section">
                          <h3>Decisions</h3>
                          <ul>
                            {asStrings(activityDetail.decisions).map((d) => (
                              <li key={d}>{d}</li>
                            ))}
                          </ul>
                        </div>
                      ) : null}
                      {openLoopTexts(activityDetail.raw).length > 0 ? (
                        <div className="section">
                          <h3>Opened</h3>
                          <ul>
                            {openLoopTexts(activityDetail.raw).map((t) => (
                              <li key={t}>
                                {t} <Tag kind>Owed</Tag>
                              </li>
                            ))}
                          </ul>
                        </div>
                      ) : null}
                      <div className="section">
                        <h3>Where</h3>
                        <p className="t2">
                          {[
                            projectFromScope(activityDetail.scope),
                            activityDetail.scope,
                            activityDetail.session_id
                              ? `session ${activityDetail.session_id}`
                              : null,
                          ]
                            .filter(Boolean)
                            .join(" · ") || "Not recorded"}
                        </p>
                      </div>
                    </div>
                    <div className="acts">
                        <button
                          type="button"
                          className="sm"
                          onClick={() => openInRecall(activityDetail.id)}
                        >
                          <Search size={14} strokeWidth={1.75} aria-hidden />
                          Open in Recall
                        </button>
                        <button
                          type="button"
                          className="sm"
                          onClick={() => {
                            setEditText(activityDetail.summary ?? "");
                            setEditOpen(true);
                          }}
                        >
                          Edit summary
                        </button>
                        <button
                          type="button"
                          className="sm danger push"
                          onClick={() => setForgetTarget(activityDetail.id)}
                        >
                          <Trash2 size={14} strokeWidth={1.75} aria-hidden />
                          Forget
                        </button>
                    </div>
                  </>
                ) : (
                  <EmptyState
                    title="Pick a capture"
                    hint="Its summary, decisions, what it left open and where it ran appear here."
                  />
                )}
              </div>
            </div>

            <Disclosure label="Advanced" openKey={activityRawKey}>
              {opsEvents.length > 0 ? (
                <div className="rows">
                  <div className="rows-head">System check-ins</div>
                  {opsEvents.slice(0, 8).map((ev, i) => (
                    <div
                      key={`${ev.kind}-${ev.created_at}-${i}`}
                      className="row-item"
                    >
                      <span className="row-main mono">{ev.kind}</span>
                      <Tag dot tone={opsStatusTone(ev.status)}>
                        {ev.status ?? "?"}
                      </Tag>
                      {ev.created_at ? (
                        <span className="row-meta">
                          {formatTs(ev.created_at)}
                        </span>
                      ) : null}
                    </div>
                  ))}
                </div>
              ) : null}
              <RawBlock text={activityText} />
            </Disclosure>
          </div>
        </section>

        <section className={`${panelClass("recall")} fill`}>
          <PanelHeader
            title="Recall"
            lede="What does the agent remember about this?"
          >
            <Segmented
              ariaLabel="Recall view"
              value={recallView}
              onChange={setRecallView}
              options={[
                { value: "search", label: "Search", hint: "Find episodes, topics and nodes." },
                {
                  value: "graph",
                  label: "Graph",
                  hint: "Walk what a topic or node is connected to.",
                },
              ]}
            />
          </PanelHeader>
          <div className="panel-body wide">
            {recallView === "search" ? (
              <>
            <div className="inline">
              <input
                className="grow"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={
                  SEARCH_MODES.find((m) => m.mode === searchMode)?.placeholder ??
                  "ask in your own words"
                }
                aria-label="Search"
                onKeyDown={(e) => {
                  if (e.key === "Enter") void doSearch();
                }}
              />
              <Segmented
                ariaLabel="Search mode"
                value={searchMode}
                onChange={setSearchMode}
                options={SEARCH_MODES.map((m) => ({
                  value: m.mode,
                  label: m.label,
                  hint: m.hint,
                }))}
              />
              <button
                type="button"
                className="primary"
                onClick={() => void doSearch()}
              >
                {searchBusy ? (
                  <Loader2 size={14} className="spin" aria-hidden />
                ) : (
                  <Search size={14} strokeWidth={1.75} aria-hidden />
                )}
                Search
              </button>
            </div>

            <div className="chips">
              <Chip
                on={Boolean(searchProject.trim())}
                title="Match the episode's project or repo path"
                onClick={() => setSearchFiltersOpen(true)}
                onRemove={
                  searchProject.trim()
                    ? () => {
                        setSearchProject("");
                        void doSearch();
                      }
                    : undefined
                }
              >
                Project · {searchProject.trim() || "Any"}
              </Chip>
              <Chip
                on={Boolean(searchSince.trim())}
                title="ISO date (2026-08-01) or a relative window: 7d, 24h, 30m"
                onClick={() => setSearchFiltersOpen(true)}
                onRemove={
                  searchSince.trim()
                    ? () => {
                        setSearchSince("");
                        void doSearch();
                      }
                    : undefined
                }
              >
                Since · {searchSince.trim() || "Any"}
              </Chip>
              <Chip
                on={Boolean(searchKind)}
                onClick={() => setSearchFiltersOpen(true)}
                onRemove={
                  searchKind
                    ? () => {
                        setSearchKind("");
                        void doSearch();
                      }
                    : undefined
                }
              >
                Kind · {searchKind ? `${searchKind[0].toUpperCase()}${searchKind.slice(1)}` : "Any"}
              </Chip>
              <Chip
                on={Boolean(searchHarness.trim())}
                title="The half of a session id before the colon"
                onClick={() => setSearchFiltersOpen(true)}
                onRemove={
                  searchHarness.trim()
                    ? () => {
                        setSearchHarness("");
                        void doSearch();
                      }
                    : undefined
                }
              >
                Harness ·{" "}
                {searchHarness.trim()
                  ? harnessLabel(searchHarness.trim())
                  : "Any"}
              </Chip>
              <Chip onClick={() => setSearchFiltersOpen((o) => !o)}>
                {searchFiltersOpen ? "Hide filters" : "+ Filter"}
              </Chip>
              {searchText ? (
                <span className="meta push">
                  {searchResults.length} result
                  {searchResults.length === 1 ? "" : "s"}
                  {searchElapsedMs != null
                    ? ` · ${(searchElapsedMs / 1000).toFixed(1)} s`
                    : ""}
                </span>
              ) : null}
            </div>

            {searchFiltersOpen ? (
              <div className="filter-row">
                <label className="filter-label">
                  Project
                  <input
                    className="filter-input"
                    value={searchProject}
                    onChange={(e) => setSearchProject(e.target.value)}
                    placeholder="any"
                    onKeyDown={(e) => {
                      if (e.key === "Enter") void doSearch();
                    }}
                  />
                </label>
                <label
                  className="filter-label"
                  title="ISO date (2026-08-01) or a relative window: 7d, 24h, 30m."
                >
                  Since
                  <input
                    className="filter-input"
                    value={searchSince}
                    onChange={(e) => setSearchSince(e.target.value)}
                    placeholder="any · e.g. 7d"
                    onKeyDown={(e) => {
                      if (e.key === "Enter") void doSearch();
                    }}
                  />
                </label>
                <label className="filter-label">
                  Harness
                  <input
                    className="filter-input"
                    value={searchHarness}
                    onChange={(e) => setSearchHarness(e.target.value)}
                    placeholder="any · e.g. claude_code"
                    onKeyDown={(e) => {
                      if (e.key === "Enter") void doSearch();
                    }}
                  />
                </label>
                <div className="chips">
                  <Chip
                    tiny
                    on={!searchKind}
                    onClick={() => setSearchKind("")}
                  >
                    Any kind
                  </Chip>
                  {SEARCH_KINDS.map((k) => (
                    <Chip
                      key={k}
                      tiny
                      on={searchKind === k}
                      onClick={() => setSearchKind(searchKind === k ? "" : k)}
                    >
                      {k[0].toUpperCase()}
                      {k.slice(1)}
                    </Chip>
                  ))}
                </div>
                <button type="button" onClick={() => void doSearch()}>
                  Apply
                </button>
              </div>
            ) : null}

            {searchDegraded && !searchErr ? (
              <Callout
                tone="warn"
                title={
                  (DEGRADED_COPY[searchDegraded] ?? DEGRADED_COPY["embed-unavailable"]).title
                }
              >
                {(DEGRADED_COPY[searchDegraded] ?? DEGRADED_COPY["embed-unavailable"]).hint}
              </Callout>
            ) : null}

            {searchErr ? (
              <Callout
                tone="warn"
                title="Search failed"
                action={
                  <button type="button" onClick={() => void doSearch()}>
                    Retry
                  </button>
                }
              >
                {searchErr}
              </Callout>
            ) : null}

            <div className="split">
              <div className="card col">
                <div className="card-scroll">
                  {searchBusy ? (
                    <EmptyState
                      icon={<Loader2 size={22} className="spin" aria-hidden />}
                      title="Searching…"
                      hint={`Searching for “${query.trim()}”.`}
                    />
                  ) : searchResults.length > 0 ? (
                    searchResults.map((r, i) => {
                      const key = `${r.kind ?? "item"}:${r.id}`;
                      const why = whyText(r);
                      const width =
                        r.score != null &&
                        Number.isFinite(r.score) &&
                        topScore
                          ? Math.max(
                              6,
                              Math.min(100, (r.score / topScore) * 100),
                            )
                          : null;
                      return (
                        <button
                          key={`${key}-${i}`}
                          type="button"
                          className={
                            recallSelected === key ? "result on" : "result"
                          }
                          onClick={() => void selectRecallResult(r)}
                        >
                          <span className="tl">
                            <Tag
                              kind
                              tone={r.kind === "episode" ? "accent" : "neutral"}
                            >
                              {r.kind ?? "item"}
                            </Tag>
                            <span className="title ellip grow">
                              {r.label ?? String(r.id ?? "")}
                            </span>
                          </span>
                          {r.snippet ? (
                            <span className="snip">{r.snippet}</span>
                          ) : null}
                          <span className="foot">
                            {/* date · project · harness — the mock's footer,
                                every part straight off the row and simply
                                absent when the payload has no value for it. */}
                            {r.ts ? <span>{shortDateLabel(r.ts)}</span> : null}
                            {r.ts && r.project ? <span>·</span> : null}
                            {r.project ? (
                              <span title={r.project}>{shortProject(r.project)}</span>
                            ) : null}
                            {(r.ts || r.project) && r.harness ? (
                              <span>·</span>
                            ) : null}
                            {r.harness ? (
                              <span>{harnessLabel(r.harness)}</span>
                            ) : null}
                            {!r.ts && !r.project && !r.harness && r.id != null ? (
                              <span className="mono">
                                {r.kind === "episode"
                                  ? `#${r.id}`
                                  : String(r.id)}
                              </span>
                            ) : null}
                            {width != null ? (
                              <span
                                className="score"
                                title={`Relevance ${r.score?.toFixed(4)}, relative to the top hit in this response.`}
                              >
                                {why ? <span>{why}</span> : null}
                                <span className="bar" aria-hidden>
                                  <i style={{ width: `${width}%` }} />
                                </span>
                              </span>
                            ) : null}
                          </span>
                        </button>
                      );
                    })
                  ) : !searchText ? (
                    <EmptyState
                      icon={<Search size={22} strokeWidth={1.75} aria-hidden />}
                      title="Search topics, episodes, and nodes"
                      hint="Type a question in your own words and press Enter. Best match weighs meaning, word overlap and exact text together."
                    />
                  ) : (
                    <EmptyState
                      icon={<Search size={22} strokeWidth={1.75} aria-hidden />}
                      title="No results"
                      hint={
                        (searchMode === "literal"
                          ? "Nothing contains those exact words. Try Best match, which also scores meaning and word overlap."
                          : searchMode === "semantic"
                            ? "Nothing in the vector index was close enough. Try Best match, which also matches the words themselves."
                            : "Nothing recorded matched that query by meaning, word overlap or exact text.") +
                        (searchProject.trim() ||
                        searchSince.trim() ||
                        searchKind ||
                        searchHarness.trim()
                          ? " The filter chips are still applied."
                          : "")
                      }
                    />
                  )}
                </div>
              </div>

              <div className="card col">
                {recallDetailLoading ? (
                  <EmptyState
                    icon={<Loader2 size={22} className="spin" aria-hidden />}
                    title="Opening…"
                  />
                ) : selectedResult ? (
                  <>
                    <div className="card-head">
                      {recallDetail
                        ? `Episode ${recallDetail.id}`
                        : (selectedResult.kind ?? "item")}
                      <span className="spacer" />
                      {recallDetail?.ts ? (
                        <span className="meta">
                          {shortDateLabel(recallDetail.ts)} ·{" "}
                          {timeLabel(recallDetail.ts)}
                        </span>
                      ) : null}
                    </div>
                    <div className="detail">
                      <div className="section">
                        <h3>Summary</h3>
                        <p>
                          {recallDetail?.summary ||
                            selectedResult.snippet ||
                            "Nothing more than the title is recorded here."}
                        </p>
                      </div>
                      {asStrings(recallDetail?.decisions).length > 0 ? (
                        <div className="section">
                          <h3>Decisions</h3>
                          <ul>
                            {asStrings(recallDetail?.decisions).map((d) => (
                              <li key={d}>{d}</li>
                            ))}
                          </ul>
                        </div>
                      ) : null}
                      {openLoopsFor(recallDetail?.id).length > 0 ? (
                        <div className="section">
                          <h3>Still open</h3>
                          <ul>
                            {openLoopsFor(recallDetail?.id).map((c) => (
                              <li key={c.id}>
                                {c.text} <Tag kind>Owed</Tag>
                              </li>
                            ))}
                          </ul>
                        </div>
                      ) : null}
                      {recallNeighbors.length > 0 ? (
                        <div className="section">
                          <h3>Connected</h3>
                          {recallNeighbors.slice(0, 8).map((n, i) => (
                            <div
                              key={`${n.id}-${i}`}
                              className="row plain"
                            >
                              <Tag kind>{nodeKindLabel(n.id)}</Tag>
                              <span className="grow ellip t2" title={n.id}>
                                {nodeTitle(n.id)}
                              </span>
                            </div>
                          ))}
                        </div>
                      ) : null}
                    </div>
                    <div className="acts">
                        <button
                          type="button"
                          className="sm"
                          onClick={() => {
                            const id = String(
                              selectedResult.id ?? selectedResult.label ?? "",
                            );
                            setNodeId(id);
                            setRecallView("graph");
                            void doGraph(id);
                          }}
                        >
                          <Waypoints size={14} strokeWidth={1.75} aria-hidden />
                          Walk the graph
                        </button>
                        <button
                          type="button"
                          className="sm"
                          onClick={() =>
                            void copyId(String(selectedResult.id ?? ""))
                          }
                        >
                          {copiedId === String(selectedResult.id ?? "")
                            ? "Copied"
                            : "Copy id"}
                        </button>
                        {recallDetail ? (
                          <button
                            type="button"
                            className="sm danger push"
                            onClick={() => setForgetTarget(recallDetail.id)}
                          >
                            <Trash2 size={14} strokeWidth={1.75} aria-hidden />
                            Forget
                          </button>
                        ) : null}
                    </div>
                  </>
                ) : (
                  <EmptyState
                    title="Pick a result"
                    hint="What it says, what it decided, what it left open and what it connects to appear here."
                  />
                )}
              </div>
            </div>

            <Disclosure label="Advanced">
              {searchTiming ? (
                <div className="rows">
                  <div className="rows-head">Where the time went</div>
                  {Object.entries(TIMING_LABEL)
                    .filter(([key]) => typeof searchTiming[key as keyof SearchTiming] === "number")
                    .map(([key, label]) => (
                      <div key={key} className="row-item">
                        <span className="row-main">{label}</span>
                        <span className="row-meta">
                          {formatMs(searchTiming[key as keyof SearchTiming] as number)}
                        </span>
                      </div>
                    ))}
                  {searchTiming.embed_cache ? (
                    <div className="row-item">
                      <span className="row-main">Query embedding</span>
                      <span className="row-meta">
                        {searchTiming.embed_cache === "hit"
                          ? "reused from cache · no API call"
                          : searchTiming.embed_cache === "miss"
                            ? "computed and cached for next time"
                            : "not cached (hub not migrated)"}
                      </span>
                    </div>
                  ) : null}
                  {searchElapsedMs != null ? (
                    <div className="row-item">
                      <span className="row-main">App round trip</span>
                      <span className="row-meta">{formatMs(searchElapsedMs)}</span>
                    </div>
                  ) : null}
                </div>
              ) : null}
              <RawBlock text={searchText} empty="Results appear here." />
            </Disclosure>
          </>
            ) : (
              <>
            <div className="toolbar">
              <input
                className="mono"
                value={nodeId}
                onChange={(e) => setNodeId(e.target.value)}
                placeholder="node id — e.g. topic:khipu"
                onKeyDown={(e) => {
                  if (e.key === "Enter") void doGraph();
                }}
              />
              <label className="hops-label">
                hops
                <input
                  type="number"
                  className="hops-input"
                  min={1}
                  max={4}
                  value={hops}
                  onChange={(e) =>
                    setHops(clampHops(Number(e.target.value)))
                  }
                  onKeyDown={(e) => {
                    if (e.key === "Enter") void doGraph();
                  }}
                />
              </label>
              <button
                type="button"
                className="primary"
                onClick={() => void doGraph()}
              >
                {actionBusy ? (
                  <Loader2 size={14} className="spin" aria-hidden />
                ) : (
                  <Waypoints size={14} strokeWidth={1.75} aria-hidden />
                )}
                Walk
              </button>
            </div>

            {!graphText ? (
              <EmptyState
                icon={<Waypoints size={22} strokeWidth={1.75} aria-hidden />}
                title="Explore the graph"
                hint="Enter a node id and walk its neighbours up to 4 hops out."
              />
            ) : null}

            {graphData.neighbors.length > 0 ? (
              <div className="rows">
                <div className="rows-head">
                  Neighbors ({graphData.neighbors.length})
                </div>
                {graphData.neighbors.slice(0, 40).map((n, i) => {
                  switch (n.mode) {
                    case "walk":
                      return (
                        <div
                          key={`${n.id}-${n.via}-${n.hops}-${i}`}
                          className="row-item"
                        >
                          <Tag kind>{n.type ?? "walk"}</Tag>
                          <span className="row-main mono">{n.id}</span>
                          <span className="row-meta">
                            {n.hops != null
                              ? `${n.hops} hop${n.hops === 1 ? "" : "s"}`
                              : ""}
                            {n.via
                              ? `${n.hops != null ? " · " : ""}via ${n.via}`
                              : ""}
                          </span>
                        </div>
                      );
                    case "edge": {
                      const outbound = n.src === graphData.rootId;
                      const inbound = n.dst === graphData.rootId;
                      const other = outbound
                        ? n.dst
                        : inbound
                          ? n.src
                          : n.id || null;
                      return (
                        <div
                          key={`${n.src}-${n.dst}-${i}`}
                          className="row-item"
                        >
                          <Tag kind>{n.type ?? "edge"}</Tag>
                          <span className="edge-arrow" aria-hidden>
                            {outbound ? "→" : inbound ? "←" : "↔"}
                          </span>
                          <span className="row-main mono">
                            {other ?? `${n.src} → ${n.dst}`}
                          </span>
                        </div>
                      );
                    }
                    default: {
                      const _exhaustive: never = n;
                      void _exhaustive;
                      return null;
                    }
                  }
                })}
              </div>
            ) : graphText ? (
              // "No edges" is an absence, not a pass — the ok/success callout
              // read as "the walk found something good" (audit 2026-09-04).
              <Callout tone="neutral" title="No edges">
                This node has no neighbors at the requested hop count. Check the
                id, or try more hops.
              </Callout>
            ) : null}

            <RawJson text={graphText} empty="Neighborhood JSON." />
          </>
            )}
          </div>
        </section>

        <section className={panelClass("revisions")}>
          <PanelHeader
            title="Conflicting edits"
            lede="When the same topic is edited in two places, the newest wins and the older version is kept. Every note file is compared against the database."
          >
            {loading.revisions ? <Spinner /> : null}
            <button type="button" onClick={() => void loadRevisions(true)}>
              <RefreshCw size={14} strokeWidth={1.75} aria-hidden />
              Refresh
            </button>
          </PanelHeader>
          <div className="panel-body">
            {revisionsConflicts ? (
              <Callout
                tone={openDrift > 0 || unreadableTopics.length > 0 ? "warn" : "ok"}
                title="Out-of-sync files"
              >
                Out of sync: <strong>{openDrift}</strong> · Topics compared:{" "}
                <strong>{revisionsConflicts.topics_checked ?? 0}</strong> ·
                Topics with more than one version: <strong>{multiCount}</strong>
                {unreadableTopics.length > 0 ? (
                  <>
                    {" "}
                    · Unreadable:{" "}
                    <strong>{unreadableTopics.join(", ")}</strong>
                  </>
                ) : null}
              </Callout>
            ) : null}

            <div className="toolbar">
              <input
                className="mono"
                value={revSlug}
                onChange={(e) => setRevSlug(e.target.value)}
                placeholder="filter slug (optional)"
                onKeyDown={(e) => {
                  if (e.key === "Enter") void loadRevisions(true);
                }}
              />
              <button type="button" onClick={() => void loadRevisions(true)}>
                List
              </button>
            </div>
            <div className="toolbar">
              <input
                className="mono"
                value={revShowId}
                onChange={(e) => setRevShowId(e.target.value)}
                placeholder="revision id"
                onKeyDown={(e) => {
                  if (e.key === "Enter") void showRevision();
                }}
              />
              <button type="button" onClick={() => void showRevision()}>
                Show body
              </button>
            </div>

            {revRecent.length > 0 ? (
              <div className="rows">
                <div className="rows-head">
                  {revSlug.trim()
                    ? `Revisions of ${revSlug.trim()} (${revRecent.length})`
                    : `Recent revisions (${revRecent.length})`}
                </div>
                {revRecent.map((r) => (
                  <div className="row-item" key={r.id}>
                    <button
                      type="button"
                      className="id-chip"
                      onClick={() => void showRevision(String(r.id))}
                    >
                      #{r.id}
                    </button>
                    <Tag className="mono">{r.slug}</Tag>
                    <span className="row-main">
                      {r.note || r.source || "revision"}
                    </span>
                    {r.revised_at ? (
                      <span className="row-meta">{formatTs(r.revised_at)}</span>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : revSlug.trim() ? (
              <Callout tone="neutral" title="No older versions for that topic">
                Nothing in the edit history matches “{revSlug.trim()}”.
              </Callout>
            ) : null}

            {multiCount > 0 ? (
              <div className="rows">
                <div className="rows-head">Topics with multiple revisions</div>
                {(revisionsConflicts?.topics_with_multiple_revisions ?? [])
                  .slice(0, 12)
                  .map((t) => (
                    <div key={t.slug} className="row-item">
                      <button
                        type="button"
                        className="id-chip"
                        onClick={() => {
                          setRevSlug(t.slug);
                          setTab("revisions");
                          void (async () => {
                            setActionBusy(true);
                            setError(null);
                            try {
                              // No --sample, for the same reason loadRevisions
                              // dropped it: sampling 40 of 622 topics renders
                              // the conflicts summary green for the whole
                              // corpus off a partial pass (audit 2026-08-17).
                              const raw = await runKhipu([
                                "revisions",
                                "--slug",
                                t.slug,
                                "--limit",
                                "20",
                              ]);
                              const parsed = parseJson(raw) as {
                                conflicts?: ConflictSummary;
                              } | null;
                              if (
                                parsed === null ||
                                typeof parsed !== "object" ||
                                Array.isArray(parsed)
                              ) {
                                setError("Unexpected response from hub");
                                return;
                              }
                              setRevisionsText(prettyJson(raw));
                              setRevisionsConflicts(parsed.conflicts ?? null);
                              setRevisionsRawKey((k) => k + 1);
                            } catch (e) {
                              setError(String(e));
                            } finally {
                              setActionBusy(false);
                            }
                          })();
                        }}
                      >
                        {t.slug}
                      </button>
                      <span className="row-main">
                        {t.revision_count} revs
                      </span>
                      {t.last_revised_at ? (
                        <span className="row-meta">
                          last {formatTs(t.last_revised_at)}
                        </span>
                      ) : null}
                    </div>
                  ))}
              </div>
            ) : null}

            <RawJson text={revisionsText} openKey={revisionsRawKey} />
          </div>
        </section>

        <section className={panelClass("owed")}>
          <PanelHeader
            title="Owed"
            lede="What you still owe on each project, kept from your own sessions."
          >
            {owedLoading ? <Spinner /> : null}
            <button type="button" onClick={() => void loadOwed(owedStatus, true)}>
              <RefreshCw size={14} strokeWidth={1.75} aria-hidden />
              Refresh
            </button>
          </PanelHeader>
          <div className="panel-body wide">
            <div className="inline">
              <Segmented
                ariaLabel="Commitment status"
                value={owedStatus}
                onChange={(next) => {
                  setOwedStatus(next);
                  void loadOwed(next, false);
                }}
                options={[
                  { value: "open", label: `Open${owedCount("open")}` },
                  { value: "closed", label: `Closed${owedCount("closed")}` },
                  { value: "stale", label: `Stale${owedCount("stale")}` },
                ]}
              />
              <div className="chips">
                {owedProjects.map((proj) => (
                  <Chip
                    key={proj}
                    title={proj}
                    on={owedProject === proj}
                    onClick={() =>
                      setOwedProject(owedProject === proj ? null : proj)
                    }
                    onRemove={
                      owedProject === proj ? () => setOwedProject(null) : undefined
                    }
                  >
                    Project · {shortProject(proj)}
                  </Chip>
                ))}
                {owedKinds.map((k) => (
                  <Chip
                    key={`ok-${k}`}
                    on={owedKind === k}
                    onClick={() => setOwedKind(owedKind === k ? null : k)}
                    onRemove={owedKind === k ? () => setOwedKind(null) : undefined}
                  >
                    Kind · {OWED_KIND_LABEL[k] ?? k}
                  </Chip>
                ))}
              </div>
              <span className="meta push">
                Closed automatically when a later capture says it was done
              </span>
            </div>

            {visibleOwed.length === 0 ? (
              <div className="card">
                <EmptyState
                  title={
                    owedProject
                      ? `Nothing owed on ${shortProject(owedProject)}`
                      : owedStatus === "open"
                        ? "Nothing owed yet"
                        : `Nothing ${owedStatus} here`
                  }
                  hint="Questions, blockers, follow-ups and promises your sessions leave open appear here."
                />
              </div>
            ) : owedStatus === "open" ? (
              // Open items are grouped by who owes them: "Needs you" first and
              // never buried under the agent's own plan steps, which is what
              // made a 300-row list unusable (phase 4 addendum).
              OWED_GROUPS.map(([id, title, hint]) => {
                const rows = visibleOwed.filter((c) => owedGroupOf(c) === id);
                if (rows.length === 0) return null;
                return (
                  <Disclosure
                    key={id}
                    className="group"
                    defaultOpen={id === "needs-you"}
                    label={
                      <>
                        <strong>{title}</strong>
                        <span className="nav-count quiet">{rows.length}</span>
                        <span className="meta wrap">{hint}</span>
                      </>
                    }
                  >
                    {renderOwedTable(rows)}
                  </Disclosure>
                );
              })
            ) : (
              renderOwedTable(visibleOwed)
            )}

            {closedToday.length > 0 ? (
              <Callout
                tone="ok"
                title={`Closed today · ${closedToday.length}`}
                action={
                  owedStatus === "closed" ? undefined : (
                    <button
                      type="button"
                      className="sm"
                      onClick={() => {
                        setOwedStatus("closed");
                        void loadOwed("closed", false);
                      }}
                    >
                      Show them
                    </button>
                  )
                }
              >
                A later capture said these were done, so they were closed.
                Reopen one if that was wrong.
              </Callout>
            ) : null}
            {owedStatus === "closed" && closedToday.length > 0 ? (
              <div className="card">
                <div className="card-head">Closed today</div>
                {closedToday.map((c) => (
                  <ListRow key={`ct-${c.id}`}>
                    <span className="grow ellip" title={c.text}>
                      {c.text}
                    </span>
                    <span className="meta">{timeLabel(c.closed_at)}</span>
                    <button
                      type="button"
                      className="sm"
                      disabled={owedBusyId === c.id}
                      onClick={() => void owedWrite(c.id, [`--reopen=${c.id}`])}
                    >
                      Reopen
                    </button>
                  </ListRow>
                ))}
              </div>
            ) : null}
          </div>
        </section>

        <section className={panelClass("harnesses")}>
          <PanelHeader
            title="Harnesses"
            lede="Which agents are wired in, and whether each one is actually recording."
          />
          <IntegrationsPanel
            runKhipu={runKhipu}
            onToast={(m) => setError(m)}
            active={tab === "harnesses"}
            liveness={liveness}
            recallProbe={recallProbe}
            refreshHealth={() => void loadDoctor(true)}
            onAnotherMac={() => {
              setSettingsSection("another-mac");
              setTab("settings");
            }}
          />
        </section>

        <section className={panelClass("settings")}>
          <PanelHeader
            title="Settings"
            lede="Capture, models, data and this Mac."
          />
          <div className="panel-body wide settings-body">
            <div className="settings-split">
              <div className="subnav" role="tablist" aria-label="Settings sections">
                {SETTINGS_SECTIONS.map(([id, label]) => (
                  <button
                    key={id}
                    type="button"
                    role="tab"
                    aria-selected={settingsSection === id}
                    className={settingsSection === id ? "on" : undefined}
                    onClick={() => setSettingsSection(id)}
                  >
                    {label}
                  </button>
                ))}
              </div>

              <div className="settings-pane">
                {settingsSection === "database" ? (
                  <>
                    <div className="section-card">
                      <div className="section-head">Connection</div>
                      <div className="section-body">
                        <div className="inline">
                          <Tag tone={dsnOk === false ? "err" : dsnOk ? "ok" : "neutral"} dot>
                            {dsnOk === false
                              ? "Not reachable"
                              : dsnOk
                                ? "Connected"
                                : "Checking…"}
                          </Tag>
                          <span className="meta">
                            Stored in the login Keychain (service{" "}
                            <code>Khipu</code>), never in a file in the repo.
                          </span>
                        </div>
                        <p className="muted">
                          Currently stored: database connection{" "}
                          <strong>
                            {presenceLabel(secretsPresence, "dsn_in_keychain")}
                          </strong>
                          . Harnesses that cannot reach the Keychain read the copy
                          in the data folder instead.
                        </p>
                        <p className="muted">
                          <strong>Encryption:</strong> prefer{" "}
                          <code>sslmode=verify-full</code> with{" "}
                          <code>root.crt</code> in the data folder.
                        </p>
                        {secretsPresenceMsg ? (
                          <p className="muted">{secretsPresenceMsg}</p>
                        ) : null}
                        <div className="toolbar">
                          <button type="button" onClick={() => void refreshDsn(true)}>
                            <RefreshCw size={14} strokeWidth={1.75} aria-hidden />
                            Recheck connection
                          </button>
                          <button type="button" onClick={() => setTab("first-run")}>
                            Change the connection
                          </button>
                        </div>
                      </div>
                    </div>
                    <Callout title="Changing this is a setup step">
                      The connection is set during setup, so it is checked and
                      migrated in one pass rather than saved half-applied.
                    </Callout>
                  </>
                ) : null}

                {settingsSection === "capture" ? (
                  <>
                    <div className="section-card">
                      <div className="section-head">Capture</div>
                      <div className="section-body">
                        <div className="rows">
                          <div className="row-item">
                            <span className="row-main">Capture a session after</span>
                            <span className="row-meta">
                              {CAPTURE_MIN_TURNS} turns, or {CAPTURE_MIN_MINUTES} minutes
                            </span>
                          </div>
                          <div className="row-item">
                            <span className="row-main">Secrets</span>
                            <span className="row-meta">
                              API keys, tokens, private keys and passwords in
                              connection strings are masked before a transcript
                              reaches the summariser and before any capture is
                              stored
                            </span>
                          </div>
                        </div>
                        <p className="muted">
                          Always captures before a compaction and when a session
                          ends. This cadence is the engine's own, the same on every
                          harness; it is not editable from here.
                        </p>
                      </div>
                    </div>

                    <div className="section-card">
                      <div className="section-head">Models</div>
                      <div className="section-body">
                        <p className="muted">
                          Session summaries follow this card (Gemini cloud or a
                          local OpenAI-compatible endpoint). The monthly topic
                          pass runs its own model and is not switched here.
                        </p>
                        {models.models_error ? (
                          <Callout tone="err" title="The stored models setting could not be read">
                            {models.models_error}. Showing defaults; saving is
                            blocked until it is fixed or cleared.
                          </Callout>
                        ) : null}

                        {(
                          [
                            ["synth", "Session summaries"],
                            ["embed", "Search index"],
                          ] as const
                        ).map(([role, label]) => {
                          const row = models[role];
                          return (
                            <div key={role} className="rows" style={{ marginBottom: 12 }}>
                              <div className="rows-head">{label}</div>
                              <div className="toolbar">
                                <label className="muted" htmlFor={`models-${role}-provider`}>
                                  Provider
                                </label>
                                <select
                                  id={`models-${role}-provider`}
                                  value={row.provider}
                                  onChange={(e) =>
                                    updateModelRole(role, { provider: e.target.value })
                                  }
                                >
                                  {(["cloud", "local"] as const).map((provider) => (
                                    <option key={provider} value={provider}>
                                      {provider}
                                    </option>
                                  ))}
                                </select>
                              </div>
                              <div className="toolbar">
                                <input
                                  className="mono"
                                  disabled={row.provider === "cloud"}
                                  value={row.endpoint}
                                  onChange={(e) =>
                                    updateModelRole(role, { endpoint: e.target.value })
                                  }
                                  placeholder="http://127.0.0.1:11434"
                                />
                                <input
                                  className="mono"
                                  value={row.model_id}
                                  onChange={(e) =>
                                    updateModelRole(role, { model_id: e.target.value })
                                  }
                                  placeholder={
                                    role === "embed"
                                      ? "search uses the active index profile"
                                      : "model id"
                                  }
                                />
                              </div>
                            </div>
                          );
                        })}

                        <p className="muted">
                          The search-index model is saved here, but search still
                          reads the active index profile — the one named under
                          Search index — until a profile switch is supported.
                        </p>

                        <div className="toolbar">
                          <button
                            type="button"
                            className="primary"
                            disabled={modelsSaving || models.models_error != null}
                            onClick={() => void saveModels()}
                          >
                            {modelsSaving ? "Saving…" : "Save models"}
                          </button>
                        </div>
                        {modelsMsg ? <pre className="code">{modelsMsg}</pre> : null}
                      </div>
                    </div>

                    <div className="section-card">
                      <div className="section-head">Keys</div>
                      <div className="section-body">
                        <label className="muted" htmlFor="gemini-key">
                          Gemini API key — session summaries and the search index.
                          Stored in the login Keychain; never written to the repo
                          or a config file.
                        </label>
                        <div className="toolbar">
                          <input
                            id="gemini-key"
                            className="mono"
                            type="password"
                            autoComplete="off"
                            spellCheck={false}
                            value={geminiKey}
                            onChange={(e) => setGeminiKey(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") void saveGeminiKey();
                            }}
                            placeholder={
                              secretsPresence?.gemini_in_keychain
                                ? "A key is stored — type a new one to replace it"
                                : "AIza…"
                            }
                          />
                          <button
                            type="button"
                            disabled={geminiSaving || !geminiKey.trim()}
                            onClick={() => void saveGeminiKey()}
                          >
                            {geminiSaving ? "Saving…" : "Save key"}
                          </button>
                        </div>
                        {geminiMsg ? <pre className="code">{geminiMsg}</pre> : null}

                        <label className="muted" htmlFor="openai-compat-key">
                          Optional key for a local OpenAI-compatible endpoint
                          (Ollama can leave this blank).
                        </label>
                        <div className="toolbar">
                          <input
                            id="openai-compat-key"
                            className="mono"
                            type="password"
                            autoComplete="off"
                            spellCheck={false}
                            value={openaiCompatKey}
                            onChange={(e) => setOpenaiCompatKey(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") void saveOpenaiCompatKey();
                            }}
                            placeholder="sk-… (optional)"
                          />
                          <button
                            type="button"
                            disabled={openaiCompatSaving || !openaiCompatKey.trim()}
                            onClick={() => void saveOpenaiCompatKey()}
                          >
                            {openaiCompatSaving ? "Saving…" : "Save local key"}
                          </button>
                        </div>
                        {openaiCompatMsg ? (
                          <pre className="code">{openaiCompatMsg}</pre>
                        ) : null}
                        <p className="muted">
                          Currently stored: Gemini{" "}
                          <strong>
                            {presenceLabel(secretsPresence, "gemini_in_keychain")}
                          </strong>
                          ; local endpoint{" "}
                          <strong>
                            {presenceLabel(secretsPresence, "openai_compat_in_keychain")}
                          </strong>
                          . An environment variable <code>GEMINI_API_KEY</code>{" "}
                          takes precedence over the stored Gemini key.
                        </p>
                      </div>
                    </div>
                  </>
                ) : null}

                {settingsSection === "index" ? (
                  <>
                    <div className="section-card">
                      <div className="section-head">
                        Search index
                        <span className="spacer" />
                        {embedActiveProfile ? (
                          <Tag tone="accent">{embedActiveProfile}</Tag>
                        ) : null}
                      </div>
                      <div className="section-body">
                        <p className="muted">
                          Search by meaning reads this index. It is rebuilt every
                          night; anything not indexed yet is still findable by its
                          exact words.
                        </p>
                        {embedCoverage ? (
                          <div className="rows">
                            {EMBED_KINDS.map(([key, label]) => {
                              const row = embedCoverage[key];
                              if (!row || typeof row.total !== "number") return null;
                              return (
                                <div key={key} className="row-item">
                                  <span className="row-main">{label}</span>
                                  <span className="row-meta">
                                    {row.embedded ?? 0} of {row.total} indexed
                                    {row.missing ? ` · ${row.missing} waiting` : ""}
                                  </span>
                                </div>
                              );
                            })}
                            {embedBudget && typeof embedBudget.cap === "number" ? (
                              <div className="row-item">
                                <span className="row-main">Embedding calls today</span>
                                <span className="row-meta">
                                  {embedBudget.calls ?? 0}
                                  {embedBudget.cap > 0 ? ` of a ${embedBudget.cap} runaway ceiling` : " · no ceiling"}
                                  {embedBudget.exhausted
                                    ? " · ceiling reached, meaning search resumes at midnight UTC"
                                    : " · this Mac, resets at midnight UTC"}
                                </span>
                              </div>
                            ) : null}
                            {queryCache?.available ? (
                              <div className="row-item">
                                <span className="row-main">Cached questions</span>
                                <span className="row-meta">
                                  {queryCache.rows ?? 0} stored · reused {queryCache.hits ?? 0}{" "}
                                  {(queryCache.hits ?? 0) === 1 ? "time" : "times"} · unused ones
                                  expire after 30 days
                                </span>
                              </div>
                            ) : null}
                          </div>
                        ) : (
                          <p className="muted">
                            Coverage comes from the health report — open Home once
                            to fill it in.
                          </p>
                        )}
                        <div className="toolbar">
                          <button
                            type="button"
                            className="primary"
                            disabled={indexBusy}
                            onClick={() => setIndexConfirm(true)}
                          >
                            {indexBusy ? (
                              <Loader2 size={14} className="spin" aria-hidden />
                            ) : null}
                            Index now
                          </button>
                          <span className="meta">
                            Indexes everything that is missing or changed, using
                            the Gemini key above.
                          </span>
                        </div>
                        {indexMsg ? <pre className="code">{indexMsg}</pre> : null}
                      </div>
                    </div>
                  </>
                ) : null}

                {settingsSection === "data" ? (
                  <>
                    <div className="section-card">
                      <div className="section-head">Data folder</div>
                      <div className="section-body">
                        <div className="toolbar">
                          <input
                            className="mono"
                            value={dataDir}
                            onChange={(e) => setDataDir(e.target.value)}
                            placeholder="~/.config/khipu"
                            onKeyDown={(e) => {
                              if (e.key === "Enter") void applyDataDir();
                            }}
                          />
                          <button
                            type="button"
                            className="primary"
                            onClick={() => void applyDataDir()}
                          >
                            Set folder
                          </button>
                          <button type="button" onClick={() => void loadPaths()}>
                            <RefreshCw size={14} strokeWidth={1.75} aria-hidden />
                            Refresh
                          </button>
                        </div>
                        {dataFiles.length > 0 ? (
                          <div className="rows">
                            {dataFiles.map((f) => (
                              <div key={f.path} className="row-item">
                                <span className="row-main mono">{f.path}</span>
                                <span className="row-meta">{formatBytes(f.bytes)}</span>
                              </div>
                            ))}
                          </div>
                        ) : (
                          <p className="muted">No local files in the data folder yet.</p>
                        )}
                      </div>
                    </div>

                    <div className="section-card">
                      <div className="section-head">Backup</div>
                      <div className="section-body">
                        <p className="muted">
                          Writes a zip of this Mac's data folder (connection file,
                          certificates, local state). Paste a destination folder.
                        </p>
                        <div className="toolbar">
                          <input
                            className="mono"
                            value={backupOut}
                            onChange={(e) => setBackupOut(e.target.value)}
                            placeholder="~/Downloads"
                            onKeyDown={(e) => {
                              if (e.key === "Enter") void doBackupLocal();
                            }}
                          />
                          <button
                            type="button"
                            className="primary"
                            onClick={() => void doBackupLocal()}
                          >
                            Backup
                          </button>
                        </div>
                      </div>
                    </div>

                    <div className="section-card">
                      <div className="section-head">Restore</div>
                      <div className="section-body">
                        <p className="muted">
                          Restores a previous zip or folder into the current data
                          folder, merging with what is there.
                        </p>
                        <div className="toolbar">
                          <input
                            className="mono"
                            value={importSource}
                            onChange={(e) => setImportSource(e.target.value)}
                            placeholder="~/Downloads/khipu-local-….zip"
                            // Deliberately no Enter-to-submit, unlike every other
                            // input here: this one writes over the current data
                            // folder, and a stray Return while typing a path
                            // should not start a restore.
                          />
                          <button
                            type="button"
                            disabled={!importSource.trim()}
                            onClick={() => setImportConfirm(true)}
                          >
                            Restore…
                          </button>
                        </div>
                        {pathsMsg ? <pre className="code">{pathsMsg}</pre> : null}
                      </div>
                    </div>
                  </>
                ) : null}

                {settingsSection === "another-mac" ? (
                  <div className="section-card">
                    <div className="section-head">Set up another Mac</div>
                    <div className="section-body">
                      <p className="muted">
                        Do this on the Mac that <strong>already works</strong>. Save
                        a join kit and AirDrop it to the new Mac — that file is
                        enough. The passphrase is optional (only if you want the
                        file locked).
                      </p>
                      <ol className="welcome-list muted">
                        <li>
                          Click <strong>Save join kit…</strong>, then AirDrop the{" "}
                          <code>.khipujoin</code> file. On the new Mac: Welcome →
                          Join existing Khipu → Import join kit file.
                        </li>
                        <li>
                          Optional: <strong>Advertise nearby (PIN)</strong> on the
                          same Wi‑Fi.
                        </li>
                      </ol>
                      <ul className="welcome-list muted">
                        <li>
                          The database must already be reachable from the new Mac
                          (Tailscale, VPN or a shared server).
                        </li>
                        <li>
                          The same repo cloned at two paths on one Mac makes
                          duplicate entries in the graph.
                        </li>
                      </ul>
                      <div className="toolbar" style={{ width: "100%" }}>
                        <input
                          className="mono"
                          type="password"
                          autoComplete="off"
                          spellCheck={false}
                          value={setupJoinPassphrase}
                          onChange={(e) => setSetupJoinPassphrase(e.target.value)}
                          placeholder="Optional passphrase (leave blank for an unlocked file)"
                          aria-label="Optional join export passphrase"
                        />
                      </div>
                      <div className="toolbar">
                        <button
                          type="button"
                          className="primary"
                          onClick={() => void exportJoinKit()}
                        >
                          Save join kit…
                        </button>
                        <button
                          type="button"
                          disabled={advertiseBusy}
                          onClick={() => void startJoinAdvertise()}
                        >
                          {advertiseBusy ? "Advertising…" : "Advertise nearby (PIN)"}
                        </button>
                      </div>
                      {setupJoinExpected ? (
                        <p className="muted mono">
                          Expected counts — episodes {setupJoinExpected.episodes ?? "?"},
                          topics {setupJoinExpected.topics ?? "?"}, nodes{" "}
                          {setupJoinExpected.nodes ?? "?"}
                        </p>
                      ) : null}
                      {advertiseInfo?.pin ? (
                        <p className="muted">
                          PIN for the new Mac:{" "}
                          <strong className="mono">{advertiseInfo.pin}</strong>
                          {advertiseInfo.timeout_sec
                            ? ` · keep this open (~${Math.round(advertiseInfo.timeout_sec / 60)} min)`
                            : null}
                          {advertiseInfo.ipv4 ? ` · LAN ${advertiseInfo.ipv4}` : null}
                        </p>
                      ) : null}
                      {hubSnapshotHealth?.refreshed_at ? (
                        <p className="muted">
                          Offline copy refreshed{" "}
                          <code>{hubSnapshotHealth.refreshed_at}</code>
                          {typeof (hubSnapshotHealth.size_bytes ?? hubSnapshotHealth.bytes) === "number"
                            ? ` (${formatBytes(hubSnapshotHealth.size_bytes ?? hubSnapshotHealth.bytes ?? 0)})`
                            : null}
                        </p>
                      ) : null}
                      {setupJoinMsg ? <pre className="code">{setupJoinMsg}</pre> : null}
                    </div>
                  </div>
                ) : null}

                {settingsSection === "components" ? (
                  <>
                    <div className="section-card">
                      <div className="section-head">Components</div>
                      <div className="section-body">
                        <p className="muted">
                          The database and the graph builder upgrade
                          independently of the app. Versions come from
                          Application Support and the compatibility matrix.
                        </p>
                        <ComponentsPanel active={tab === "settings"} />
                      </div>
                    </div>

                    <div className="section-card">
                      <div className="section-head">What feeds the graph</div>
                      <div className="section-body">
                        <p className="muted">
                          Which local folders the graph builder reads on its next
                          run. Turning one off skips it next time; nothing already
                          in the database is deleted. &quot;Embed images&quot;
                          includes PNG and JPEG files from a folder in the search
                          index.
                        </p>
                        {!graphSourcesProducer ? (
                          <p className="muted">
                            This Mac does not build the graph, so these are
                            read-only here.
                          </p>
                        ) : null}
                        <div className="rows">
                          {graphSources.map((row) => {
                            const unreachable = (graphSourcesResolved?.unreachable ?? [])
                              .some((u) => u.id === row.id);
                            const status = !row.enabled
                              ? "Turned off"
                              : unreachable
                                ? "Folder not found"
                                : "On";
                            const userCode =
                              row.kind === "code_ast" && row.id !== "code:claude";
                            const hasRoot = Boolean(row.root);
                            return (
                              <div key={row.id} className="row-item">
                                <label className="row-main">
                                  <input
                                    type="checkbox"
                                    checked={Boolean(row.enabled)}
                                    disabled={!graphSourcesProducer}
                                    onChange={(e) =>
                                      void toggleGraphSource(row.id, e.target.checked)
                                    }
                                  />
                                  <span className="mono">{row.id}</span>
                                  {row.root ? (
                                    <span className="muted mono"> {row.root}</span>
                                  ) : null}
                                </label>
                                {hasRoot ? (
                                  <label className="row-meta">
                                    <input
                                      type="checkbox"
                                      checked={Boolean(row.embed_media)}
                                      disabled={!graphSourcesProducer}
                                      onChange={(e) =>
                                        void toggleEmbedMedia(row.id, e.target.checked)
                                      }
                                    />{" "}
                                    Embed images
                                  </label>
                                ) : (
                                  <span className="row-meta muted">no folder</span>
                                )}
                                <span className="row-meta">{status}</span>
                                {graphSourcesProducer && userCode ? (
                                  <button
                                    type="button"
                                    onClick={() => void removeGraphSource(row.id)}
                                  >
                                    Remove
                                  </button>
                                ) : null}
                              </div>
                            );
                          })}
                        </div>
                        {graphSourcesProducer ? (
                          <div className="toolbar">
                            <input
                              className="mono"
                              value={newCodeRoot}
                              onChange={(e) => setNewCodeRoot(e.target.value)}
                              placeholder="/absolute/path/to/code"
                              onKeyDown={(e) => {
                                if (e.key === "Enter") void addGraphCodeRoot();
                              }}
                            />
                            <button type="button" onClick={() => void addGraphCodeRoot()}>
                              Add folder
                            </button>
                            <button type="button" onClick={() => void pickGraphCodeRoot()}>
                              Choose folder…
                            </button>
                          </div>
                        ) : null}
                        {graphSourcesMsg ? (
                          <pre className="code">{graphSourcesMsg}</pre>
                        ) : null}
                      </div>
                    </div>
                  </>
                ) : null}

                {settingsSection === "updates" ? (
                  <div className="section-card">
                    <div className="section-head">Updates</div>
                    <div className="section-body">
                      <p className="muted">
                        Installed version <code>v{appVersion}</code>. Launch checks
                        for a new release (fail-soft, nothing installs itself); use
                        this button to download and install. Each build is signed,
                        and the signature is checked before anything is replaced.
                      </p>
                      <div className="toolbar">
                        <button
                          type="button"
                          className="primary"
                          disabled={updateBusy}
                          onClick={() => void checkForUpdates()}
                        >
                          {updateBusy ? (
                            <Loader2 size={14} className="spin" aria-hidden />
                          ) : null}
                          {updateBusy ? "Checking…" : "Check for updates"}
                        </button>
                      </div>
                      {updateMsg ? <pre className="code">{updateMsg}</pre> : null}
                    </div>
                  </div>
                ) : null}

                {settingsSection === "advanced" ? (
                  <>
                    <div className="section-card">
                      <div className="section-head">This Mac</div>
                      <div className="section-body">
                        <div className="toolbar">
                          <button type="button" onClick={() => setTab("first-run")}>
                            Run setup again
                          </button>
                          <button type="button" onClick={() => setTab("revisions")}>
                            Conflicting edits
                          </button>
                          <button type="button" onClick={() => setFeedbackOpen(true)}>
                            <MessageSquare size={14} aria-hidden />
                            Send feedback
                          </button>
                        </div>
                        <p className="muted">
                          Stuck? Open the health report on <strong>Home</strong>, or
                          email <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>.
                          Include the health report if it is red — it never
                          contains secrets.
                        </p>
                      </div>
                    </div>

                    <RawJson
                      text={settingsRawText}
                      label="Raw configuration"
                      empty="Nothing read yet."
                    />
                  </>
                ) : null}
              </div>
            </div>
          </div>
        </section>
      </main>

      <FeedbackForm
        open={feedbackOpen}
        appVersion={appVersion}
        onClose={() => setFeedbackOpen(false)}
        onSendingChange={setFeedbackSending}
        returnFocusRef={feedbackButtonRef}
      />

      <PostUpdateNoticeDialog
        notice={postUpdateNotice}
        onDismiss={() => setPostUpdateNotice(null)}
        onOpenIntegrations={() => setTab("harnesses")}
      />

      {/* Forget — the one destructive write either list makes. */}
      <Dialog
        open={forgetTarget != null}
        className="kit-dialog"
        ariaLabelledBy="forget-title"
        onCancel={() => setForgetTarget(null)}
      >
        <div className="kit-dialog-body">
          <h2 id="forget-title">Forget episode #{forgetTarget}?</h2>
          <p>
            It stops appearing in search and recall, and its vectors are
            deleted. The row itself is kept, so this is not a shred.
          </p>
          <div className="kit-dialog-actions">
            <button type="button" onClick={() => setForgetTarget(null)}>
              Cancel
            </button>
            <button
              type="button"
              className="danger"
              disabled={actionBusy}
              onClick={() => void confirmForget()}
            >
              <Trash2 size={14} strokeWidth={1.75} aria-hidden />
              Forget
            </button>
          </div>
        </div>
      </Dialog>

      {/* Edit summary — `khipu episode edit`, which re-embeds in the same
          transaction so the correction is findable, not just visible. */}
      <Dialog
        open={editOpen}
        className="kit-dialog"
        ariaLabelledBy="edit-title"
        onCancel={() => setEditOpen(false)}
      >
        <div className="kit-dialog-body">
          <h2 id="edit-title">Edit summary</h2>
          <p>
            This is what search reads. Saving re-indexes the capture straight
            away.
          </p>
          <textarea
            className="edit-summary"
            value={editText}
            onChange={(e) => setEditText(e.target.value)}
            aria-label="Episode summary"
          />
          <div className="kit-dialog-actions">
            <button type="button" onClick={() => setEditOpen(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="primary"
              disabled={editSaving || !editText.trim()}
              onClick={() => void saveSummary()}
            >
              {editSaving ? (
                <Loader2 size={14} className="spin" aria-hidden />
              ) : null}
              Save
            </button>
          </div>
        </div>
      </Dialog>

      {/* Snooze — `khipu owed --snooze ID --until`. Presets only: the CLI
          refuses free text rather than silently clearing a due date. */}
      <Dialog
        open={snoozeTarget != null}
        className="kit-dialog"
        ariaLabelledBy="snooze-title"
        onCancel={() => setSnoozeTarget(null)}
      >
        <div className="kit-dialog-body">
          <h2 id="snooze-title">Snooze until</h2>
          <p>{snoozeTarget?.text}</p>
          <div className="kit-dialog-actions">
            <button type="button" onClick={() => setSnoozeTarget(null)}>
              Cancel
            </button>
            {(
              [
                ["7d", "A week"],
                ["2w", "Two weeks"],
                ["1m", "A month"],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                type="button"
                disabled={owedBusyId != null}
                onClick={() => {
                  const target = snoozeTarget;
                  setSnoozeTarget(null);
                  if (target) {
                    void owedWrite(target.id, [
                      `--snooze=${target.id}`,
                      `--until=${value}`,
                    ]);
                  }
                }}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      </Dialog>

      {/* Index now — paid API calls, so it asks first. */}
      <Dialog
        open={indexConfirm}
        className="kit-dialog"
        ariaLabelledBy="index-title"
        onCancel={() => setIndexConfirm(false)}
      >
        <div className="kit-dialog-body">
          <h2 id="index-title">Index everything that is missing?</h2>
          <p>
            This sends every unindexed session and topic page to the model
            behind your Gemini key, which costs money and can take a few
            minutes. The nightly run does the same thing on its own.
          </p>
          <div className="kit-dialog-actions">
            <button type="button" onClick={() => setIndexConfirm(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="primary"
              disabled={indexBusy}
              onClick={() => {
                setIndexConfirm(false);
                void runIndexNow();
              }}
            >
              Index now
            </button>
          </div>
        </div>
      </Dialog>

      {/* Restore — writes over the current data folder. */}
      <Dialog
        open={importConfirm}
        className="kit-dialog"
        ariaLabelledBy="import-title"
        onCancel={() => setImportConfirm(false)}
      >
        <div className="kit-dialog-body">
          <h2 id="import-title">Restore into the data folder?</h2>
          <p>
            Files from <code>{importSource.trim()}</code> are merged into{" "}
            <code>{dataDir || "the data folder"}</code>. A file with the same
            name is overwritten, and nothing here undoes that.
          </p>
          <div className="kit-dialog-actions">
            <button type="button" onClick={() => setImportConfirm(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="danger"
              onClick={() => {
                setImportConfirm(false);
                void doImportLocal();
              }}
            >
              Restore
            </button>
          </div>
        </div>
      </Dialog>

      {error ? (
        <div className="toast-err" role="alert">
          <span className="toast-msg">{error}</span>
          <button
            type="button"
            className="toast-close"
            onClick={() => setError(null)}
            aria-label="Dismiss error"
          >
            <X size={14} aria-hidden />
          </button>
        </div>
      ) : null}
    </div>
  );
}
