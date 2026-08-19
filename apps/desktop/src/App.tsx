import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { invoke } from "@tauri-apps/api/core";
import { getVersion } from "@tauri-apps/api/app";
import { check } from "@tauri-apps/plugin-updater";
import { relaunch } from "@tauri-apps/plugin-process";
import {
  Blocks,
  ChevronRight,
  CircleCheck,
  Gauge,
  GitBranch,
  History,
  Loader2,
  PlugZap,
  RefreshCw,
  Search,
  Settings2,
  Stethoscope,
  TriangleAlert,
  Waypoints,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import khipuIcon from "./assets/khipu-icon.png";
import { IntegrationsPanel } from "./IntegrationsPanel";
import { SUPPORT_EMAIL, Welcome, welcomeCompleted } from "./Welcome";
import "./App.css";

type Tab =
  | "first-run"
  | "status"
  | "activity"
  | "search"
  | "graph"
  | "revisions"
  | "doctor"
  | "settings"
  | "integrations";

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

type SearchResult = {
  kind?: string;
  id?: string | number;
  label?: string;
  snippet?: string;
  score?: number;
  paths?: string[];
  neighbors?: { id: string; type?: string }[];
};

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

const NAV_ICONS: Record<Tab, LucideIcon> = {
  status: Gauge,
  activity: History,
  search: Search,
  graph: Waypoints,
  revisions: GitBranch,
  doctor: Stethoscope,
  settings: Settings2,
  integrations: Blocks,
  "first-run": PlugZap,
};

// Any user-typed value reaches argparse as its own argv element, so a value
// beginning with "-" would be read as a FLAG rather than as the value. Callers
// pass positional input after a "--" separator and flag input as "--name=value"
// (one token, unambiguous even when the value starts with a dash). Typing
// "--limit" into the search box used to produce an argparse usage error instead
// of a search (audit 2026-08-17).
async function runKhipu(args: string[]): Promise<string> {
  return invoke<string>("run_khipu", { args });
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

function StatusPill({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className={tone === "neutral" ? "pill" : `pill ${tone}`}>
      <span className="pill-dot" />
      {children}
    </span>
  );
}

function Callout({
  tone,
  title,
  action,
  children,
}: {
  tone: "ok" | "warn";
  title: string;
  action?: ReactNode;
  children?: ReactNode;
}) {
  const Icon = tone === "warn" ? TriangleAlert : CircleCheck;
  return (
    <div className={`callout ${tone}`}>
      <Icon size={16} strokeWidth={1.75} className="callout-icon" aria-hidden />
      <div className="callout-content">
        <div className="callout-title">{title}</div>
        {children ? <div className="callout-body">{children}</div> : null}
      </div>
      {action ? <div className="callout-action">{action}</div> : null}
    </div>
  );
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

function RawJson({
  text,
  empty,
  openKey = 0,
}: {
  text: string;
  empty?: string;
  openKey?: number;
}) {
  const ref = useRef<HTMLDetailsElement>(null);
  const [open, setOpen] = useState(false);
  // Bump openKey → open once; onToggle keeps user collapse/expand free.
  useEffect(() => {
    if (openKey > 0) {
      setOpen(true);
      requestAnimationFrame(() => {
        ref.current?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }
  }, [openKey]);
  const body = text && text !== "…" ? text : empty || "No data yet.";
  return (
    <details
      ref={ref}
      className="raw"
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary>
        <ChevronRight size={14} className="raw-chevron" aria-hidden />
        Raw JSON
      </summary>
      <pre className="code tall">{body}</pre>
    </details>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>("status");
  const [dsnOk, setDsnOk] = useState<boolean | null>(null);
  const [loading, setLoading] = useState<Partial<Record<CacheTab, boolean>>>(
    {},
  );
  const [error, setError] = useState<string | null>(null);
  const fetchedAt = useRef<Partial<Record<CacheTab, number>>>({});

  const [statusText, setStatusText] = useState("…");
  const [counts, setCounts] = useState<Counts | null>(null);
  // True capture -> PG mirror latency (drift.py `mirror_lag_seconds`), distinct
  // from time-since-last-capture (`time_since_last_capture_seconds`, not shown
  // as its own KPI today) — see plan.md P2a / audit F2.
  const [mirrorLag, setMirrorLag] = useState<number | null>(null);
  // Split so Status sample-0 cannot clobber Revisions-derived drift UI.
  const [statusConflicts, setStatusConflicts] =
    useState<ConflictSummary | null>(null);
  const [revisionsConflicts, setRevisionsConflicts] =
    useState<ConflictSummary | null>(null);
  const [recentCaptures, setRecentCaptures] = useState<
    Array<{ id: number; summary?: string; ts?: string }>
  >([]);
  const [dsnSource, setDsnSource] = useState<string | null>(null);
  const [doctorText, setDoctorText] = useState("…");
  const [doctorOk, setDoctorOk] = useState<boolean | null>(null);
  // Named failures, so a red Doctor says WHAT is red without a trip to the raw
  // JSON. Capture liveness is the one that matters most: a harness whose
  // hook runs but records nothing must be loud here (2026-08-17).
  const [doctorIssues, setDoctorIssues] = useState<string[]>([]);
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
  const [secretsPresence, setSecretsPresence] = useState<{
    dsn_in_keychain?: boolean;
    gemini_in_keychain?: boolean;
  } | null>(null);
  const [geminiKey, setGeminiKey] = useState("");
  const [geminiMsg, setGeminiMsg] = useState<string | null>(null);
  const [geminiSaving, setGeminiSaving] = useState(false);
  const [episodeShowId, setEpisodeShowId] = useState("");
  const [revSlug, setRevSlug] = useState("");
  const [revRecent, setRevRecent] = useState<RecentRevision[]>([]);
  const [revShowId, setRevShowId] = useState("");
  const [query, setQuery] = useState("");
  const [searchText, setSearchText] = useState("");
  // Literal keyword match is the CLI default. The pane offered no alternative,
  // so a natural phrase whose words never appear verbatim ("capture liveness")
  // returned "Nothing matched that query in the hub index" — a false statement,
  // since the semantic index scores those very episodes at 0.64 (audit
  // 2026-08-17). Semantic is the better default for a person typing a sentence.
  const [semantic, setSemantic] = useState(true);
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
        mirror_lag_seconds?: number;
        conflicts?: ConflictSummary;
        recent_captures?: Array<{ id: number; summary?: string; ts?: string }>;
        dsn_source?: string;
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
      setMirrorLag(
        typeof parsed.mirror_lag_seconds === "number"
          ? parsed.mirror_lag_seconds
          : null,
      );
      setStatusConflicts(parsed.conflicts ?? null);
      setRecentCaptures(parsed.recent_captures ?? []);
      setDsnSource(parsed.dsn_source ?? null);
      fetchedAt.current.status = Date.now();
    } catch (e) {
      // Preserve last-good KPIs/lists; toast only.
      setError(String(e));
    } finally {
      markLoading("status", false);
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
        await loadStatus(true);
      } else {
        setGeminiMsg(parsed?.error ?? "Could not save the key.");
      }
    } catch (e) {
      setGeminiMsg(String(e));
    } finally {
      setGeminiSaving(false);
    }
  }, [geminiKey, loadStatus]);

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
      const lv = (parsed as { capture_liveness?: { ok?: boolean; red?: string[]; harnesses?: Record<string, { reasons?: string[] }> } }).capture_liveness;
      if (lv && lv.ok === false) {
        for (const h of lv.red ?? []) {
          issues.push(`Not recording ${h}: ${(lv.harnesses?.[h]?.reasons ?? []).join("; ") || "see report"}`);
        }
      }
      const gs = (parsed as { git_sync?: { ok?: boolean; reasons?: string[] } }).git_sync;
      if (gs && gs.ok === false) issues.push(`Git sync not landing: ${(gs.reasons ?? []).join("; ") || "see report"}`);
      if ((parsed as { drift_ok?: boolean }).drift_ok === false) issues.push("File ↔ PG drift");
      if ((parsed as { graph_drift_ok?: boolean }).graph_drift_ok === false) issues.push("Graph mirror drift");
      if ((parsed as { outbox_ok?: boolean }).outbox_ok === false) issues.push("Outbox has captures PG does not have yet");
      if ((parsed as { backup_ok?: boolean }).backup_ok === false) issues.push("Backup / restore drill");
      setDoctorIssues(issues);
      fetchedAt.current.doctor = Date.now();
    } catch (e) {
      // Preserve last-good doctorOk/text; toast only.
      setError(String(e));
    } finally {
      markLoading("doctor", false);
    }
  }, []);

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
        secrets?: {
          dsn_in_keychain?: boolean;
          gemini_in_keychain?: boolean;
        };
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
      setSecretsPresence(parsed.secrets ?? null);
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
    setError(null);
    try {
      const raw = await runKhipu([
        "search",
        ...(semantic ? ["--semantic"] : []),
        "--limit",
        "20",
        "--",
        query.trim(),
      ]);
      setSearchText(prettyJson(raw));
    } catch (e) {
      setError(String(e));
      setSearchText("");
    } finally {
      setActionBusy(false);
    }
  }, [query, semantic]);

  const doGraph = useCallback(async () => {
    if (!nodeId.trim()) return;
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
        nodeId.trim(),
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
    // Fire-and-forget: never block tab paint on CLI.
    if (tab === "status") void loadStatus(false);
    if (tab === "doctor") void loadDoctor(false);
    if (tab === "revisions") void loadRevisions(false);
    if (tab === "activity") void loadActivity(false);
  }, [tab, dsnOk, loadStatus, loadDoctor, loadRevisions, loadActivity]);

  const navGroups = useMemo(
    () =>
      [
        {
          label: "Browse",
          items: [
            ["status", "Status", "Health & counts"],
            ["activity", "Activity", "Recent captures"],
            ["search", "Search", "Find topics"],
            ["graph", "Graph", "Neighbors"],
          ],
        },
        {
          label: "Ops",
          items: [
            ["revisions", "Revisions", "Drift & LWW"],
            ["doctor", "Doctor", "Backup check"],
          ],
        },
        {
          label: "Setup",
          items: [
            ["settings", "Settings", "Keys & data folder"],
            ["integrations", "Integrations", "Claude · Cursor · Aegis · Codex"],
            ["first-run", "Welcome", "Tutorial & setup"],
          ],
        },
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
  const [appVersion, setAppVersion] = useState<string>("…");
  const [updateMsg, setUpdateMsg] = useState<string | null>(null);
  const [updateBusy, setUpdateBusy] = useState(false);

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

  useEffect(() => {
    // First-run needs this too: it tells the user which file to create, and
    // the data folder is relocatable from Settings. Loading it only for
    // Settings meant the onboarding screen named ~/.config/khipu no matter
    // where the DSN was actually supposed to go. `khipu paths` needs no DSN,
    // so it works on exactly the screen that exists because there isn't one.
    if (tab === "settings" || tab === "first-run") void loadPaths();
  }, [tab, loadPaths]);

  useEffect(() => {
    void getVersion()
      .then(setAppVersion)
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

  const statusOpenDrift = statusConflicts?.open_file_vs_pg ?? 0;
  const openDrift = revisionsConflicts?.open_file_vs_pg ?? 0;
  const unreadableTopics = revisionsConflicts?.topic_files_unreadable ?? [];
  const multiCount =
    revisionsConflicts?.topics_with_multiple_revisions?.length ?? 0;
  const anyLoading = Object.values(loading).some(Boolean) || actionBusy;

  const lagLabel =
    mirrorLag == null
      ? "—"
      : mirrorLag < 120
        ? `${Math.round(mirrorLag)}s`
        : `${Math.round(mirrorLag / 60)}m`;

  const panelClass = (id: Tab) =>
    tab === id ? "panel" : "panel is-hidden";

  // SLO (plan.md): mirror lag p95 <= 30s. This is a single latest-sample
  // reading, not a true p95, but the same threshold is the honest bar.
  const lagFresh = mirrorLag != null && mirrorLag <= 30;
  const lagTone: Tone =
    mirrorLag == null ? "neutral" : lagFresh ? "ok" : "warn";
  const lagStatusLabel =
    mirrorLag == null ? "—" : lagFresh ? "fresh" : "stale";

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
          {navGroups.map((group) => (
            <div key={group.label} className="nav-group" data-tauri-drag-region>
              <div className="nav-group-label" data-tauri-drag-region>
                {group.label}
              </div>
              {group.items.map(([id, label, hint]) => {
                const Icon = NAV_ICONS[id];
                return (
                  <button
                    key={id}
                    type="button"
                    className={tab === id ? "nav active" : "nav"}
                    title={hint}
                    aria-current={tab === id ? "page" : undefined}
                    onClick={() => setTab(id)}
                  >
                    <Icon size={16} strokeWidth={1.75} aria-hidden />
                    <span className="nav-label">{label}</span>
                  </button>
                );
              })}
            </div>
          ))}
        </div>
        <div className="rail-foot" data-tauri-drag-region>
          <span className="version-chip">v{appVersion}</span>
          <StatusPill
            tone={dsnOk == null ? "neutral" : dsnOk ? "ok" : "err"}
          >
            {dsnOk == null ? "DSN…" : dsnOk ? "DSN ok" : "DSN missing"}
          </StatusPill>
          {anyLoading ? (
            <Loader2 size={12} className="spin rail-spin" aria-hidden />
          ) : null}
        </div>
      </nav>

      <main className="main">
        <section className={panelClass("first-run")}>
          <div className="panel-body onboard-wrap" data-tauri-drag-region>
            <Welcome
              dsnOk={dsnOk}
              refreshDsn={() => refreshDsn(true)}
              runKhipu={runKhipu}
              onFinish={() => setTab("status")}
              openIntegrations={() => setTab("integrations")}
            />
          </div>
        </section>

        <section className={panelClass("status")}>
          <PanelHeader
            title="Status"
            lede="Is the hub alive? Counts, mirror lag, and recent captures from Postgres."
          >
            {loading.status ? <Spinner /> : null}
            <button type="button" onClick={() => void loadStatus(true)}>
              <RefreshCw size={14} strokeWidth={1.75} aria-hidden />
              Refresh
            </button>
          </PanelHeader>
          <div className="panel-body">
            <div className="kpis">
              <div className="kpi">
                <span className="kpi-label">Postgres</span>
                <span className="kpi-value">
                  {dsnOk == null
                    ? "…"
                    : dsnOk
                      ? "Connected"
                      : "DSN missing"}
                </span>
                <StatusPill
                  tone={dsnOk == null ? "neutral" : dsnOk ? "ok" : "err"}
                >
                  {dsnOk == null
                    ? "checking…"
                    : dsnOk
                      ? (dsnSource ?? "dsn")
                      : "not configured"}
                </StatusPill>
              </div>
              <div className="kpi">
                <span className="kpi-label">Mirror lag</span>
                <span className="kpi-value mono">{lagLabel}</span>
                <StatusPill tone={lagTone}>{lagStatusLabel}</StatusPill>
              </div>
              <div className="kpi">
                <span className="kpi-label">File↔pg drift</span>
                <span className="kpi-value mono">
                  {statusConflicts
                    ? statusOpenDrift === 0
                      ? "none"
                      : `${statusOpenDrift} open`
                    : "—"}
                </span>
                <StatusPill
                  tone={
                    statusConflicts == null
                      ? "neutral"
                      : statusOpenDrift > 0
                        ? "warn"
                        : "ok"
                  }
                >
                  {statusConflicts == null
                    ? "—"
                    : statusOpenDrift > 0
                      ? "open drift"
                      : "clean"}
                </StatusPill>
                {statusConflicts && statusOpenDrift === 0 ? (
                  <span className="kpi-hint">
                    PG stats only — refresh Revisions to hash files.
                  </span>
                ) : null}
              </div>
              <div className="kpi">
                <span className="kpi-label">Episodes</span>
                <span className="kpi-value mono">
                  {counts?.episodes ?? "—"}
                </span>
                <StatusPill
                  tone={counts?.episodes == null ? "neutral" : "ok"}
                >
                  {counts?.episodes == null ? "—" : "mirrored"}
                </StatusPill>
              </div>
            </div>

            <div className="chips">
              <span className="chip">topics {counts?.topics ?? "—"}</span>
              <span className="chip">nodes {counts?.nodes ?? "—"}</span>
              <span className="chip">edges {counts?.edges ?? "—"}</span>
              <span className="chip">
                embeddings {counts?.embeddings ?? "—"}
              </span>
            </div>

            {recentCaptures.length > 0 ? (
              <>
                <Callout
                  tone="ok"
                  title="Recent captures"
                  action={
                    <button type="button" onClick={() => setTab("activity")}>
                      Open Activity
                    </button>
                  }
                >
                  Latest episodes mirrored into Postgres.
                </Callout>
                <div className="rows">
                  {recentCaptures.slice(0, 5).map((ep) => (
                    <div key={ep.id} className="row-item">
                      <button
                        type="button"
                        className="id-chip"
                        onClick={() => {
                          setEpisodeShowId(String(ep.id));
                          setTab("activity");
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
                        {(ep.summary || "").slice(0, 100)}
                      </span>
                      {ep.ts ? (
                        <span className="row-meta">{formatTs(ep.ts)}</span>
                      ) : null}
                    </div>
                  ))}
                </div>
              </>
            ) : null}

            {statusConflicts ? (
              <Callout
                tone={statusOpenDrift > 0 ? "warn" : "ok"}
                title="Conflicts"
                action={
                  <button type="button" onClick={() => setTab("revisions")}>
                    Open Revisions
                  </button>
                }
              >
                file↔pg open: <strong>{statusOpenDrift}</strong>
                {statusOpenDrift === 0 ? " (none in last sample)" : ""} ·
                multi-revision topics:{" "}
                <strong>
                  {statusConflicts.topics_with_multiple_revisions?.length ?? 0}
                </strong>{" "}
                · revision rows:{" "}
                <strong>{statusConflicts.revision_row_count ?? "—"}</strong>
                {statusConflicts.note ? (
                  <span className="callout-note">{statusConflicts.note}</span>
                ) : null}
              </Callout>
            ) : null}

            <RawJson text={statusText} />
          </div>
        </section>

        <section className={panelClass("activity")}>
          <PanelHeader
            title="Activity"
            lede="Sessions become episodes in Postgres via Khipu's capture hook, alongside the legacy writer. Read/inspect only — editing and deleting from here is planned."
          >
            {loading.activity ? <Spinner /> : null}
            <button type="button" onClick={() => void loadActivity(true)}>
              <RefreshCw size={14} strokeWidth={1.75} aria-hidden />
              Refresh
            </button>
          </PanelHeader>
          <div className="panel-body">
            {secretsPresence ? (
              <div className="chips">
                <span className="chip">
                  DSN keychain:{" "}
                  {secretsPresence.dsn_in_keychain ? "yes" : "no"}
                </span>
                <span className="chip">
                  Gemini keychain:{" "}
                  {secretsPresence.gemini_in_keychain ? "yes" : "no"}
                </span>
              </div>
            ) : null}

            {opsEvents.length > 0 ? (
              <div className="rows">
                <div className="rows-head">Ops heartbeats</div>
                {opsEvents.slice(0, 8).map((ev, i) => (
                  <div
                    key={`${ev.kind}-${ev.created_at}-${i}`}
                    className="row-item"
                  >
                    <span className="row-main mono">{ev.kind}</span>
                    <StatusPill tone={opsStatusTone(ev.status)}>
                      {ev.status ?? "?"}
                    </StatusPill>
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

        <section className={panelClass("search")}>
          <PanelHeader
            title="Search"
            lede="Find topics, episodes, and nodes across the hub."
          />
          <div className="panel-body">
            <div className="toolbar">
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={semantic ? "ask in your own words" : "exact words to match"}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void doSearch();
                }}
              />
              <label className="toggle" title="Meaning-based search over the vector index. Off = literal keyword match.">
                <input
                  type="checkbox"
                  checked={semantic}
                  onChange={(e) => setSemantic(e.target.checked)}
                />
                Semantic
              </label>
              <button
                type="button"
                className="primary"
                onClick={() => void doSearch()}
              >
                {actionBusy ? (
                  <Loader2 size={14} className="spin" aria-hidden />
                ) : (
                  <Search size={14} strokeWidth={1.75} aria-hidden />
                )}
                Search
              </button>
            </div>

            {searchResults.length > 0 ? (
              <div className="results">
                {searchResults.map((r, i) => (
                  <div key={`${r.kind}-${r.id}-${i}`} className="result-card">
                    <div className="result-top">
                      <span className={`kind-badge ${r.kind ?? ""}`}>
                        {r.kind ?? "item"}
                      </span>
                      <span className="result-label">
                        {r.label ?? String(r.id ?? "")}
                      </span>
                    </div>
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
                    {r.id != null && String(r.id) !== r.label ? (
                      <span className="result-id mono">{String(r.id)}</span>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : !searchText ? (
              <div className="empty">
                <Search size={22} strokeWidth={1.75} aria-hidden />
                <div className="empty-title">
                  Search topics, episodes, and nodes
                </div>
                <div className="empty-hint">
                  Type a query and press Enter — results come from the hub
                  index.
                </div>
              </div>
            ) : (
              <div className="empty">
                <Search size={22} strokeWidth={1.75} aria-hidden />
                <div className="empty-title">No results</div>
                <div className="empty-hint">
                  {semantic
                    ? "Nothing in the hub index was close enough to that query."
                    : "No exact keyword match. Turn on Semantic to search by meaning — the words need not appear verbatim."}
                </div>
              </div>
            )}

            <RawJson text={searchText} empty="Results appear here." />
          </div>
        </section>

        <section className={panelClass("graph")}>
          <PanelHeader
            title="Graph"
            lede="Walk the neighborhood around a node id."
          />
          <div className="panel-body">
            <div className="toolbar">
              <input
                className="mono"
                value={nodeId}
                onChange={(e) => setNodeId(e.target.value)}
                placeholder="node id"
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
                          <span className="kind-badge">
                            {n.type ?? "walk"}
                          </span>
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
                          <span className="kind-badge">
                            {n.type ?? "edge"}
                          </span>
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
              <Callout tone="ok" title="No edges">
                This node has no neighbors at the requested hop count.
              </Callout>
            ) : null}

            <RawJson text={graphText} empty="Neighborhood JSON." />
          </div>
        </section>

        <section className={panelClass("revisions")}>
          <PanelHeader
            title="Revisions"
            lede="LWW keeps losers in topic_revisions. Every topic file is hashed and compared against Postgres."
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
                title="Drift summary"
              >
                Open file↔pg: <strong>{openDrift}</strong> · Topics compared:{" "}
                <strong>{revisionsConflicts.topics_checked ?? 0}</strong> ·
                Topics with ≥2 revisions: <strong>{multiCount}</strong>
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
                    <span className="pill mono">{r.slug}</span>
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
              <Callout tone="ok" title="No revisions for that slug">
                Nothing in topic_revisions matches “{revSlug.trim()}”.
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
                              const raw = await runKhipu([
                                "revisions",
                                "--slug",
                                t.slug,
                                "--limit",
                                "20",
                                "--sample",
                                "40",
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

        <section className={panelClass("doctor")}>
          <PanelHeader
            title="Doctor"
            lede="Backup freshness and hub reachability checks."
          >
            {loading.doctor ? <Spinner /> : null}
            <button type="button" onClick={() => void loadDoctor(true)}>
              <RefreshCw size={14} strokeWidth={1.75} aria-hidden />
              Refresh
            </button>
          </PanelHeader>
          <div className="panel-body">
            <div
              className={`doctor-card ${
                doctorOk == null ? "" : doctorOk ? "ok" : "err"
              }`}
            >
              {doctorOk == null ? (
                <Stethoscope size={24} strokeWidth={1.75} aria-hidden />
              ) : doctorOk ? (
                <CircleCheck size={24} strokeWidth={1.75} aria-hidden />
              ) : (
                <TriangleAlert size={24} strokeWidth={1.75} aria-hidden />
              )}
              <div>
                <div className="doctor-title">
                  {doctorOk == null
                    ? "—"
                    : doctorOk
                      ? "All checks passed"
                      : "Issues found"}
                </div>
                <p className="doctor-sub muted">
                  {doctorOk == null
                    ? "Run Refresh to check the hub."
                    : doctorOk
                      ? "Drift, graph mirror, outbox, backup, capture liveness for every harness, and the nightly git sync — all green. Details below."
                      : doctorIssues.length
                        ? doctorIssues.join(" · ")
                        : "Details in the raw report below."}
                </p>
              </div>
            </div>

            <RawJson text={doctorText} />
          </div>
        </section>

        <section className={panelClass("integrations")}>
          <PanelHeader
            title="Integrations"
            lede="Install Khipu into each harness on this Mac and verify it actually works. One native pack per harness; nothing shared, nothing forced."
          />
          <IntegrationsPanel runKhipu={runKhipu} onToast={(m) => setError(m)} />
        </section>

        <section className={panelClass("settings")}>
          <PanelHeader
            title="Settings"
            lede="Keys, Mac-local files, and backup/import. The database is separate — this folder is only what lives on this Mac."
          >
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
              <div className="section-head">Secrets</div>
              <div className="section-body">
                <p className="muted">
                  Keychain service <code>Khipu</code> (
                  <code>database_url</code>, <code>gemini_api_key</code>).{" "}
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
                  Currently stored:{" "}
                  <strong>
                    {secretsPresence?.gemini_in_keychain ? "yes" : "no"}
                  </strong>
                  . An environment variable <code>GEMINI_API_KEY</code> takes
                  precedence over this, if one is set.
                </p>

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
                  Stuck? Reopen the <strong>Welcome</strong> tutorial from Setup, check{" "}
                  <strong>Doctor</strong>, or email{" "}
                  <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>. Include the
                  Doctor output if it is red — it never contains secrets.
                </p>
              </div>
            </div>

            <div className="section-card">
              <div className="section-head">
                Local LLM
                <span className="badge-muted">planned</span>
              </div>
              <div className="section-body">
                <label className="muted" htmlFor="local-llm">
                  Endpoint (not available yet)
                </label>
                <div className="toolbar">
                  <input
                    id="local-llm"
                    className="mono"
                    disabled
                    placeholder="http://127.0.0.1:11434/v1 (disabled)"
                    value=""
                    readOnly
                  />
                </div>
              </div>
            </div>
          </div>
        </section>
      </main>

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
