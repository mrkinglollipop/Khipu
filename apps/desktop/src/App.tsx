import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { invoke } from "@tauri-apps/api/core";
import { getVersion } from "@tauri-apps/api/app";
import { check } from "@tauri-apps/plugin-updater";
import { relaunch } from "@tauri-apps/plugin-process";
import {
  confirm as confirmDialog,
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
  Disclosure,
  EmptyState,
  ListRow,
  Segmented,
  Tag,
  Tile,
} from "./ui";
import { ComponentsPanel } from "./ComponentsPanel";
import { IntegrationsPanel } from "./IntegrationsPanel";
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
  /** Optional per-signal explanation, if the CLI ever attaches one. Rendered
   * as plain text when present; absent from today's payload. */
  why?: string;
  signals?: unknown;
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
};

type OwedStatus = "open" | "closed" | "stale";

/** How many commitments one screenful asks for. A count that lands exactly on
 *  the cap is shown as "N+", because it is a floor, not a total. */
const OWED_LIMIT = 500;

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
    return `${harnessLabel(project.split(":")[0] ?? "")} session`;
  }
  const parts = project.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? project;
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
  const [liveness, setLiveness] = useState<{
    ok?: boolean;
    red?: string[];
    harnesses?: Record<string, { ok?: boolean; reasons?: string[] }>;
  } | null>(null);
  const [embedCoverage, setEmbedCoverage] = useState<Record<
    string,
    { total?: number; embedded?: number; missing?: number; pct?: number }
  > | null>(null);
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
      mirror_age_seconds?: number;
    }>
  >([]);
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
  const [episodeShowId, setEpisodeShowId] = useState("");
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
  // Separate from the shared `actionBusy`: the results area needs to know
  // that THIS search is in flight so it can show a loading state instead of
  // leaving the "type a query" empty state on screen (audit 2026-09-04).
  const [searchBusy, setSearchBusy] = useState(false);
  // Recall holds Search and the graph walk; the walk is a sub-view of a result,
  // not a peer destination (audit IA).
  const [recallView, setRecallView] = useState<"search" | "graph">("search");
  const [owedRows, setOwedRows] = useState<Commitment[]>([]);
  // Home previews the open ones; the Owed screen's own list follows its
  // filter, so the two must not share one array.
  const [owedOpenRows, setOwedOpenRows] = useState<Commitment[]>([]);
  const [owedStatus, setOwedStatus] = useState<OwedStatus>("open");
  const [owedProject, setOwedProject] = useState<string | null>(null);
  const [owedOpenCount, setOwedOpenCount] = useState<number | null>(null);
  const [owedLoading, setOwedLoading] = useState(false);
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
      const lv = (parsed as {
        capture_liveness?: {
          ok?: boolean;
          red?: string[];
          harnesses?: Record<string, { ok?: boolean; reasons?: string[] }>;
        };
      }).capture_liveness;
      setLiveness(lv ?? null);
      setEmbedCoverage(
        (parsed as { embed_coverage?: Record<string, { total?: number; embedded?: number; missing?: number; pct?: number }> })
          .embed_coverage ?? null,
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

  /** `khipu owed` — the open commitments a session left behind. Read-only in
   *  this phase: Done / Snooze / Reopen land in phase 4 with the CLI's
   *  `--close` / `--reopen` and the `due_after` update Snooze needs. */
  const loadOwed = useCallback(
    async (status: OwedStatus, force = false, forDisplay = true) => {
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
        if (status === "open") {
          setOwedOpenCount(rows.length);
          setOwedOpenRows(rows);
        }
        // The rail's badge fetch must not replace the rows the Owed screen is
        // showing for a different filter.
        if (!forDisplay) return;
        setOwedRows(rows);
        setOwedFetchedAt((prev) => ({ ...prev, [status]: Date.now() }));
      } catch (e) {
        setError(String(e));
      } finally {
        setOwedLoading(false);
      }
    },
    [owedFetchedAt],
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

  const loadActivity = useCallback(async (force = false) => {
    if (!needsFetch("activity", force)) return;
    markLoading("activity", true);
    setError(null);
    try {
      const raw = await runKhipu(["activity", "--limit", "40"]);
      const parsed = parseJson(raw) as {
        recent?: Array<{
          id: number;
          summary?: string;
          ts?: string;
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

  const showEpisode = useCallback(async () => {
    const id = Number(episodeShowId);
    if (!Number.isFinite(id) || id <= 0) {
      setError("Enter a valid id");
      return;
    }
    setActionBusy(true);
    setError(null);
    try {
      const raw = await runKhipu(["activity", "--show", String(id)]);
      setActivityText(prettyJson(raw));
      setActivityRawKey((k) => k + 1);
    } catch (e) {
      setError(String(e));
    } finally {
      setActionBusy(false);
    }
  }, [episodeShowId]);

  /** `khipu episode forget ID` — soft-deletes the episode and drops its
   * vectors. The pane's lede promised deleting was "planned"; this is it, and
   * it is the only write the Activity pane makes, so it goes behind a native
   * confirm (audit 2026-09-04). */
  const forgetEpisode = useCallback(
    async (rawId: string) => {
      const id = Number(rawId);
      if (!Number.isFinite(id) || id <= 0) {
        setError("Enter a valid id");
        return;
      }
      const yes = await confirmDialog(
        `Forget episode #${id}? It stops appearing in search and recall, and its vectors are deleted. The row is kept (soft delete), so this is not a shred.`,
        { title: "Forget episode", kind: "warning" },
      );
      if (!yes) return;
      setActionBusy(true);
      setError(null);
      try {
        const raw = await runKhipu(["episode", "forget", String(id)]);
        setActivityText(prettyJson(raw));
        setActivityRawKey((k) => k + 1);
        await loadActivity(true);
      } catch (e) {
        setError(String(e));
      } finally {
        setActionBusy(false);
      }
    },
    [loadActivity],
  );

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

  const doSearch = useCallback(async () => {
    if (!query.trim()) return;
    setActionBusy(true);
    setSearchBusy(true);
    setError(null);
    setSearchErr(null);
    try {
      // Always `--mode`, never the deprecated `--semantic` alias. Filters are
      // only passed when non-empty: an empty `--project ""` matches nothing.
      const args = ["search", "--mode", searchMode, "--limit", "20"];
      if (searchProject.trim()) {
        args.push("--project", searchProject.trim());
      }
      if (searchSince.trim()) {
        args.push("--since", searchSince.trim());
      }
      args.push("--", query.trim());
      const raw = await runKhipu(args);
      setSearchText(prettyJson(raw));
    } catch (e) {
      // Keep the query and any prior results — only the new attempt failed —
      // and surface it inline (below) so it survives the toast timing out.
      setError(String(e));
      setSearchErr(String(e));
    } finally {
      setActionBusy(false);
      setSearchBusy(false);
    }
  }, [query, searchMode, searchProject, searchSince]);

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

  useEffect(() => {
    if (!dsnOk) return;
    // Fire-and-forget: never block tab paint on CLI. Home is the merged
    // Status + Doctor screen, so it pulls all three read paths.
    if (tab === "home") {
      void loadStatus(false);
      void loadDoctor(false);
      void loadActivity(false);
    }
    if (tab === "revisions") void loadRevisions(false);
    if (tab === "activity") void loadActivity(false);
    if (tab === "owed") void loadOwed(owedStatus, false);
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
    void loadOwed("open", false, false);
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

  const searchResults = useMemo<SearchResult[]>(() => {
    const parsed = parseJson(searchText);
    if (Array.isArray(parsed)) return parsed as SearchResult[];
    if (parsed && typeof parsed === "object") {
      const results = (parsed as { results?: unknown }).results;
      if (Array.isArray(results)) return results as SearchResult[];
    }
    return [];
  }, [searchText]);

  const graphData = useMemo<{
    rootId: string | null;
    neighbors: GraphNeighbor[];
  }>(() => {
    const parsed = parseJson(graphText);
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
  }, [graphText]);

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

  // Owed: the project filter offers only projects the loaded rows actually
  // carry, so a chip can never select an empty list.
  const owedProjects = Array.from(
    new Set(owedRows.map((c) => c.project).filter((p): p is string => Boolean(p))),
  ).sort();
  const visibleOwed = owedProject
    ? owedRows.filter((c) => c.project === owedProject)
    : owedRows;

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
                  Owed
                  <span className="spacer" />
                  <button
                    type="button"
                    className="link sm"
                    onClick={() => setTab("owed")}
                  >
                    Open Owed
                  </button>
                </div>
                {owedOpenRows.length === 0 ? (
                  <EmptyState
                    title="Nothing owed yet"
                    hint="Follow-ups your sessions leave open will appear here."
                  />
                ) : (
                  owedOpenRows.slice(0, 4).map((c) => {
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

        <section className={panelClass("activity")}>
          <PanelHeader
            title="Activity"
            lede="Every session Khipu recorded. Open one to read it, or forget one to drop it from search and recall."
          >
            {loading.activity ? <Spinner /> : null}
            <button type="button" onClick={() => void loadActivity(true)}>
              <RefreshCw size={14} strokeWidth={1.75} aria-hidden />
              Refresh
            </button>
          </PanelHeader>
          <div className="panel-body">
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
                      <span className="row-meta">{formatTs(ev.created_at)}</span>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : null}

            <div className="toolbar">
              <input
                className="mono"
                value={episodeShowId}
                onChange={(e) => setEpisodeShowId(e.target.value)}
                placeholder="episode id"
                onKeyDown={(e) => {
                  if (e.key === "Enter") void showEpisode();
                }}
              />
              <button type="button" onClick={() => void showEpisode()}>
                Show full
              </button>
              <button
                type="button"
                className="danger"
                disabled={actionBusy || !episodeShowId.trim()}
                title="Soft-delete this episode and remove its vectors."
                onClick={() => void forgetEpisode(episodeShowId)}
              >
                <Trash2 size={14} strokeWidth={1.75} aria-hidden />
                Forget
              </button>
            </div>

            {activityList.length > 0 ? (
              <div className="rows">
                {activityList.slice(0, 20).map((ep) => (
                  <div key={ep.id} className="row-item">
                    <button
                      type="button"
                      className="id-chip"
                      onClick={() => {
                        setEpisodeShowId(String(ep.id));
                        void (async () => {
                          setActionBusy(true);
                          try {
                            const raw = await runKhipu([
                              "activity",
                              "--show",
                              String(ep.id),
                            ]);
                            setActivityText(prettyJson(raw));
                            setActivityRawKey((k) => k + 1);
                          } catch (e) {
                            setError(String(e));
                          } finally {
                            setActionBusy(false);
                          }
                        })();
                      }}
                    >
                      #{ep.id}
                    </button>
                    <span className="row-main">
                      {(ep.summary || "").slice(0, 120)}
                    </span>
                    <span className="row-meta">
                      {ep.ts ? formatTs(ep.ts) : ""}
                      {ep.mirror_age_seconds != null
                        ? `${ep.ts ? " · " : ""}lag ${Math.round(ep.mirror_age_seconds)}s`
                        : ""}
                    </span>
                  </div>
                ))}
              </div>
            ) : null}

            <RawJson text={activityText} openKey={activityRawKey} />
          </div>
        </section>

        <section className={panelClass("recall")}>
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
            <div className="toolbar">
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={
                  SEARCH_MODES.find((m) => m.mode === searchMode)?.placeholder ??
                  "ask in your own words"
                }
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

            <div className="toolbar">
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
              {searchProject.trim() || searchSince.trim() ? (
                <button
                  type="button"
                  onClick={() => {
                    setSearchProject("");
                    setSearchSince("");
                  }}
                >
                  Clear filters
                </button>
              ) : null}
            </div>

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

            {searchResults.length > 0 ? (
              <div className="results">
                {searchResults.map((r, i) => (
                  <div key={`${r.kind}-${r.id}-${i}`} className="result-card">
                    <div className="result-top">
                      <Tag kind tone={r.kind === "episode" ? "accent" : "neutral"}>
                        {r.kind ?? "item"}
                      </Tag>
                      <span className="result-label">
                        {r.label ?? String(r.id ?? "")}
                      </span>
                    </div>
                    {r.score != null && Number.isFinite(r.score) ? (
                      <div
                        className="result-score"
                        title={`Relevance score ${r.score}. Relative to the other hits in this response.`}
                      >
                        <span className="result-score-bar" aria-hidden>
                          <span
                            style={{
                              width: `${Math.max(
                                4,
                                Math.min(
                                  100,
                                  ((r.score ?? 0) /
                                    (searchResults[0]?.score || r.score || 1)) *
                                    100,
                                ),
                              )}%`,
                            }}
                          />
                        </span>
                        <span className="result-score-num mono">
                          {r.score.toFixed(3)}
                        </span>
                      </div>
                    ) : null}
                    {whyText(r) ? (
                      <p className="result-why muted">{whyText(r)}</p>
                    ) : null}
                    {r.snippet ? (
                      <p className="result-snippet">{r.snippet}</p>
                    ) : null}
                    {r.paths && r.paths.length > 0 ? (
                      <ul className="result-paths mono">
                        {r.paths.map((p) => (
                          <li key={p}>{p}</li>
                        ))}
                      </ul>
                    ) : null}
                    {r.neighbors && r.neighbors.length > 0 ? (
                      <ul className="result-neighbors mono">
                        {r.neighbors.map((n) => (
                          <li key={`${n.id}-${n.type ?? ""}`}>
                            {n.id}
                            {n.type ? ` (${n.type})` : ""}
                          </li>
                        ))}
                      </ul>
                    ) : null}
                    <div className="inline">
                      {r.id != null && String(r.id) !== r.label ? (
                        <span className="result-id mono">{String(r.id)}</span>
                      ) : null}
                      {r.kind === "node" || r.kind === "topic" ? (
                        <button
                          type="button"
                          className="link sm"
                          title="Open this in the graph view"
                          onClick={() => {
                            setNodeId(String(r.id ?? r.label ?? ""));
                            setRecallView("graph");
                            void doGraph(String(r.id ?? r.label ?? ""));
                          }}
                        >
                          <Waypoints size={14} strokeWidth={1.75} aria-hidden />
                          Walk the graph
                        </button>
                      ) : null}
                    </div>
                  </div>
                ))}
              </div>
            ) : searchBusy ? (
              // Without this the "type a query" empty state stayed on screen
              // for the whole round trip, so a slow search looked like a
              // search that had never been asked for (audit 2026-09-04).
              <div className="empty">
                <Loader2 size={22} className="spin" aria-hidden />
                <div className="empty-title">Searching…</div>
                <div className="empty-hint">
                  Searching for “{query.trim()}”.
                </div>
              </div>
            ) : !searchText ? (
              <div className="empty">
                <Search size={22} strokeWidth={1.75} aria-hidden />
                <div className="empty-title">
                  Search topics, episodes, and nodes
                </div>
                <div className="empty-hint">
                  Type a question in your own words and press Enter. Best match
                  weighs meaning, word overlap and exact text together.
                </div>
              </div>
            ) : (
              <div className="empty">
                <Search size={22} strokeWidth={1.75} aria-hidden />
                <div className="empty-title">No results</div>
                <div className="empty-hint">
                  {searchMode === "literal"
                    ? "Nothing contains those exact words. Try Best match, which also scores meaning and word overlap."
                    : searchMode === "semantic"
                      ? "Nothing in the vector index was close enough. Try Best match, which also matches the words themselves."
                      : "Nothing recorded matched that query by meaning, word overlap or exact text."}
                  {searchProject.trim() || searchSince.trim()
                    ? " The Project and Since filters are still applied."
                    : ""}
                </div>
              </div>
            )}

            <RawJson text={searchText} empty="Results appear here." />
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
              <div className="empty">
                <Waypoints size={22} strokeWidth={1.75} aria-hidden />
                <div className="empty-title">Explore the graph</div>
                <div className="empty-hint">
                  Enter a node id and walk its neighbors up to 4 hops out.
                </div>
              </div>
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
                  {
                    value: "open",
                    label:
                      owedOpenCount != null
                        ? `Open · ${owedOpenCount}${owedOpenCount >= OWED_LIMIT ? "+" : ""}`
                        : "Open",
                  },
                  { value: "closed", label: "Closed" },
                  { value: "stale", label: "Stale" },
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
                    {shortProject(proj)}
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
                    owedStatus === "open"
                      ? "Nothing owed yet"
                      : `Nothing ${owedStatus} here`
                  }
                  hint="Follow-ups, blockers, questions and promises your sessions leave open appear here."
                />
              </div>
            ) : (
              <div className="card">
                <div className="card-head">
                  <span className="w76">Kind</span>
                  <span className="grow">What you owe</span>
                  <span className="w104">Project</span>
                  <span className="w52">Opened</span>
                  <span className="w52">From</span>
                </div>
                {visibleOwed.map((c) => (
                  <ListRow key={c.id}>
                    <Tag
                      kind
                      tone={c.kind === "blocker" ? "warn" : "neutral"}
                      className="w76"
                    >
                      {OWED_KIND_LABEL[c.kind ?? ""] ?? c.kind ?? "Owed"}
                    </Tag>
                    <span className="grow ellip">{c.text}</span>
                    <span className="w104">
                      {c.project ? (
                        <Tag title={c.project}>{shortProject(c.project)}</Tag>
                      ) : (
                        <span className="meta">—</span>
                      )}
                    </span>
                    <span className="meta w52">
                      {c.opened_at
                        ? new Date(
                            String(c.opened_at).replace(" ", "T"),
                          ).toLocaleDateString([], {
                            month: "short",
                            day: "numeric",
                          })
                        : "—"}
                    </span>
                    <span className="meta mono w52">
                      {c.opened_episode != null ? `#${c.opened_episode}` : "—"}
                    </span>
                  </ListRow>
                ))}
              </div>
            )}
          </div>
        </section>

        <section className={panelClass("harnesses")}>
          <PanelHeader
            title="Harnesses"
            lede="Install Khipu into each harness on this Mac and verify it actually works. One native pack per harness; nothing shared, nothing forced."
          />
          <IntegrationsPanel
            runKhipu={runKhipu}
            onToast={(m) => setError(m)}
            active={tab === "harnesses"}
          />
        </section>

        <section className={panelClass("settings")}>
          <PanelHeader
            title="Settings"
            lede="Capture, models, data and this Mac."
          >
            <button type="button" onClick={() => setTab("first-run")}>
              Run setup again
            </button>
            <button type="button" onClick={() => void loadPaths()}>
              <RefreshCw size={14} strokeWidth={1.75} aria-hidden />
              Refresh paths
            </button>
          </PanelHeader>
          <div className="panel-body">
            <div className="section-card">
              <div className="section-head">Updates</div>
              <div className="section-body">
                <p className="muted">
                  Installed version <code>v{appVersion}</code>. Launch checks
                  GitHub Releases (fail-soft, no auto-install); use this
                  button to download and install. Each build is signed, and the
                  signature is verified before anything is replaced.
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

            <div className="section-card">
              <div className="section-head">Set up another Mac</div>
              <div className="section-body">
                <p className="muted">
                  Do this on the Mac that <strong>already works</strong>. Save a join kit
                  and AirDrop it to the new Mac — that file is enough. Passphrase is
                  optional (only if you want the file locked).
                </p>
                <ol className="welcome-list muted">
                  <li>
                    Click <strong>Save join kit…</strong>, then AirDrop the{" "}
                    <code>.khipujoin</code> file. On the new Mac: Welcome → Join existing
                    Khipu → Import join kit file.
                  </li>
                  <li>
                    Optional: <strong>Advertise nearby (PIN)</strong> on the same Wi‑Fi.
                  </li>
                </ol>
                <ul className="welcome-list muted">
                  <li>The Postgres hub must already be reachable from the new Mac (Tailscale / VPN / shared server).</li>
                  <li>Same repo cloned at two paths on one Mac = duplicate graph nodes in v1.</li>
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
                    Expected hub counts — episodes {setupJoinExpected.episodes ?? "?"},
                    topics {setupJoinExpected.topics ?? "?"}, nodes{" "}
                    {setupJoinExpected.nodes ?? "?"}
                  </p>
                ) : null}
                {advertiseInfo?.pin ? (
                  <p className="muted">
                    PIN for the new Mac: <strong className="mono">{advertiseInfo.pin}</strong>
                    {advertiseInfo.timeout_sec
                      ? ` · keep this open (~${Math.round(advertiseInfo.timeout_sec / 60)} min)`
                      : null}
                    {advertiseInfo.ipv4 ? ` · LAN ${advertiseInfo.ipv4}` : null}
                  </p>
                ) : null}
                {hubSnapshotHealth?.refreshed_at ? (
                  <p className="muted">
                    Hub snapshot refreshed{" "}
                    <code>{hubSnapshotHealth.refreshed_at}</code>
                    {typeof (hubSnapshotHealth.size_bytes ?? hubSnapshotHealth.bytes) === "number"
                      ? ` (${formatBytes(hubSnapshotHealth.size_bytes ?? hubSnapshotHealth.bytes ?? 0)})`
                      : null}
                  </p>
                ) : null}
                {setupJoinMsg ? <pre className="code">{setupJoinMsg}</pre> : null}
              </div>
            </div>

            <div className="section-card">
              <div className="section-head">Local data folder</div>
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
                </div>
                {dataFiles.length > 0 ? (
                  <div className="rows">
                    {dataFiles.map((f) => (
                      <div key={f.path} className="row-item">
                        <span className="row-main mono">{f.path}</span>
                        <span className="row-meta">
                          {formatBytes(f.bytes)}
                        </span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="muted">
                    No local files in the data folder yet.
                  </p>
                )}
              </div>
            </div>

            <div className="section-card">
              <div className="section-head">Backup now</div>
              <div className="section-body">
                <p className="muted">
                  Writes a zip of the Mac data folder (DSN file, certs, etc.).
                  Paste a destination path or a Downloads directory.
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
              <div className="section-head">Import</div>
              <div className="section-body">
                <p className="muted">
                  Restore from a previous zip or folder into the current data
                  directory (merge by default).
                </p>
                <div className="toolbar">
                  <input
                    className="mono"
                    value={importSource}
                    onChange={(e) => setImportSource(e.target.value)}
                    placeholder="~/Downloads/khipu-local-….zip"
                    // Deliberately no Enter-to-submit, unlike every other input
                    // here: this one writes over the current data folder, and a
                    // stray Return while typing a path should not start a
                    // restore. The button is the only way in.
                  />
                  <button type="button" onClick={() => void doImportLocal()}>
                    Import
                  </button>
                </div>
                {pathsMsg ? <pre className="code">{pathsMsg}</pre> : null}
              </div>
            </div>

            <div className="section-card">
              <div className="section-head">Graph sources</div>
              <div className="section-body">
                <p className="muted">
                  Choose which local folders feed the knowledge graph on the next
                  build. Takes effect on the next graph build (Status → Graph
                  build, or tonight&apos;s 02:17). Unchecking does not purge
                  Postgres — it only skips collectors on the next sqlite rebuild.
                  &quot;Embed images&quot; opts a rooted source into native Gemini
                  Embedding 2 PNG/JPEG ingest (default off; CLI{" "}
                  <code>khipu embed-media-backfill</code>).
                </p>
                {!graphSourcesProducer ? (
                  <p className="muted">
                    This Mac is not the graph producer, so sources are
                    read-only here. Make it the producer with{" "}
                    <code>khipu jobs install graph_build</code> (or set{" "}
                    <code>KHIPU_GRAPH_PRODUCER=1</code>).
                  </p>
                ) : null}
                <div className="rows">
                  {graphSources.map((row) => {
                    const unreachable = (graphSourcesResolved?.unreachable ?? [])
                      .some((u) => u.id === row.id);
                    const status = !row.enabled
                      ? "membership-off"
                      : unreachable
                        ? "path-unreachable"
                        : "ok";
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
                          <label className="row-meta" title="Native PNG/JPEG into active Gemini Embedding 2 profile">
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
                          <span className="row-meta muted">no root</span>
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
                      Add code root
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

            <div className="section-card">
              <div className="section-head">Models</div>
              <div className="section-body">
                <p className="muted">
                  Capture extract follows this card (Gemini cloud or a local
                  OpenAI-compatible endpoint). Nightly consolidate is mechanical;
                  monthly topic classify is DeepSeek v4-pro via{" "}
                  <code>conversation-memory-monthly.py</code> and is{" "}
                  <strong>not</strong> switched here.
                </p>
                {models.models_error ? (
                  <p className="muted">
                    Stored models config had a problem (showing defaults):{" "}
                    <code>{models.models_error}</code>. Save is blocked until
                    the stored models key is fixed or cleared.
                  </p>
                ) : null}

                {(
                  [
                    ["synth", "Synth (capture extract)"],
                    ["embed", "Embed (persist only)"],
                    ["vision", "Vision"],
                  ] as const
                ).map(([role, label]) => {
                  const row = models[role];
                  const visionOff = role === "vision" && row.provider === "off";
                  const providerOptions =
                    role === "vision"
                      ? (["cloud", "local", "off"] as const)
                      : (["cloud", "local"] as const);
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
                          {providerOptions.map((p) => (
                            <option key={p} value={p}>
                              {p}
                            </option>
                          ))}
                        </select>
                      </div>
                      <div className="toolbar">
                        <input
                          className="mono"
                          disabled={visionOff || row.provider === "cloud"}
                          value={row.endpoint}
                          onChange={(e) =>
                            updateModelRole(role, { endpoint: e.target.value })
                          }
                          placeholder={
                            visionOff
                              ? "n/a when off"
                              : "http://127.0.0.1:11434"
                          }
                        />
                        <input
                          className="mono"
                          disabled={visionOff}
                          value={row.model_id}
                          onChange={(e) =>
                            updateModelRole(role, { model_id: e.target.value })
                          }
                          placeholder={
                            role === "embed"
                              ? "runtime = active Gemini embed profile"
                              : visionOff
                                ? "n/a when off"
                                : "model id"
                          }
                        />
                      </div>
                    </div>
                  );
                })}

                <p className="muted">
                  Embed: saved; search still uses the{" "}
                  <strong>active Gemini embed profile</strong> until the profiles
                  cut. Vision: off — no ingest this version.
                </p>

                <label className="muted" htmlFor="openai-compat-key">
                  Optional local OpenAI-compat bearer (Ollama can leave blank).
                  Presence is shown under Secrets.
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
              <div className="section-head">Secrets</div>
              <div className="section-body">
                <p className="muted">
                  Keychain service <code>Khipu</code> (
                  <code>database_url</code>, <code>gemini_api_key</code>,{" "}
                  <code>openai_compat_api_key</code>).{" "}
                  <code>khipu secrets</code> shows presence only.
                </p>
                <p className="muted">
                  <strong>TLS:</strong> prefer <code>sslmode=verify-full</code>{" "}
                  + <code>root.crt</code> in the data folder.
                </p>

                <label className="muted" htmlFor="gemini-key">
                  Gemini API key — used for session summaries and embeddings.
                  Stored in the login Keychain; never written to the repo or a
                  config file.
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
                <p className="muted">
                  Currently stored: Gemini{" "}
                  <strong>
                    {presenceLabel(secretsPresence, "gemini_in_keychain")}
                  </strong>
                  ; OpenAI-compat{" "}
                  <strong>
                    {presenceLabel(
                      secretsPresence,
                      "openai_compat_in_keychain",
                    )}
                  </strong>
                  . An environment variable <code>GEMINI_API_KEY</code> takes
                  precedence over the Gemini Keychain item, if one is set.
                </p>
                {secretsPresenceMsg ? (
                  <p className="muted">{secretsPresenceMsg}</p>
                ) : null}

                <div className="toolbar">
                  <button type="button" onClick={() => void refreshDsn(true)}>
                    Recheck DSN
                  </button>
                </div>
              </div>
            </div>

            <div className="section-card">
              <div className="section-head">Help &amp; support</div>
              <div className="section-body">
                <p className="muted">
                  Stuck? Run setup again from the button at the top of this
                  screen, open the health report on <strong>Home</strong>, or
                  email <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>.
                  Include the health report if it is red — it never contains
                  secrets.
                </p>
              </div>
            </div>

            {/* Components used to be its own destination; it is maintenance,
                and belongs with the rest of what this Mac runs (audit IA). */}
            <div className="section-card">
              <div className="section-head">Components</div>
              <div className="section-body">
                <p className="muted">
                  Postgres and the graph builder upgrade independently of the
                  app. Versions come from Application Support and the
                  compatibility matrix.
                </p>
                <ComponentsPanel active={tab === "settings"} />
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
