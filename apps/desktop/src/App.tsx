import { Settings } from "./settings/Settings";
import type { SettingsCtx } from "./settings/context";
import { Component, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { invoke } from "@tauri-apps/api/core";
import { getVersion } from "@tauri-apps/api/app";
import { save as saveFileDialog } from "@tauri-apps/plugin-dialog";
import {
  
  CircleCheck,
  Gauge,
  
  
  RefreshCw,
  Search,
  SlidersHorizontal,
  
  
  
  X,
} from "lucide-react";
import khipuIcon from "./assets/khipu-icon.png";
import { Sidebar, TabBar, Toolbar, ViewSwitch } from "./shell";
import { Accounts } from "./accounts/Accounts";
import { RunningNow } from "./accounts/RunningNow";
import { useRunning } from "./accounts/running";
import { Today } from "./today/Today";
import { useNeedsYou } from "./today/needs";
import { OrderAndRules } from "./accounts/OrderAndRules";
import type { NavItem } from "./shell";
import {
  Callout,
  
  Dialog,
  
  
  
  Segmented,
  
  
} from "./ui";
import type { LivenessPayload, RecallProbeStatus, T3Health } from "./IntegrationsPanel";
import { t3HealthGap, } from "./railHealth";
import { Welcome, welcomeCompleted } from "./Welcome";
import { SetupStages, type SetupPhase, type SetupPipelineResult } from "./SetupStages";
import { buildAttention, } from "./doctorAttention";
import { synthModelError } from "./SynthProvider";
import { isSecretsPresence, pickSecretsPresence, type SecretsPresence } from "./secretsPresence";
import { liveWords, usePlanUsage } from "./PlansCard";
import { toastWords } from "./humanWords";
import { useRouting } from "./routing";
import type { DoctorSynthProvider } from "./SynthProvider";
import { useModelVerify } from "./modelVerify";
import { WorkingBanner } from "./WorkingBanner";
import { FeedbackForm } from "./FeedbackForm";
import { PostUpdateNoticeDialog } from "./PostUpdateNoticeDialog";
import { Memory } from "./memory/Memory";
import {
  
  GatewayUrlCard,
  
  
  
} from "./SettingsControls";
import {
  launchWhatsNew,
  recordVersionOnSetupFinish,
  routingNeedsSetup,
  writeCardState,
  type CardState,
  type LaunchPlan,
  type NoticeTarget,
} from "./postUpdateNotices";
import { WhatsNewCard, WhatsNewHistory, } from "./WhatsNew";
import { GuidesPage } from "./GuidesPage";
import { useGuideSignals } from "./guideSignals";
import { guideStates, startItems as buildStartItems } from "./guidesModel";
import type { GuideId } from "./guidesModel";
import type { GuideTarget } from "./guideOutlines";
import "./App.css";
import "./guides.css";

/** Six destinations, one per user job, plus the first-run flow and Revisions —
 *  which is a drill-in from Home's "Conflicting edits" item, not a peer
 *  (audit 2026-09-04 IA). Status and Doctor are gone as screens; their data
 *  paths feed Home. */
type Tab =
  | "first-run"
  | "home"
  | "memory"
  | "routing"
  | "settings"
  | "guides";

type PaneBoundaryProps = {
  title: string;
  onRetry: () => Promise<void>;
  children: ReactNode;
};

class PaneBoundary extends Component<PaneBoundaryProps, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }

  private retry = () => {
    void this.props.onRetry();
  };

  render() {
    if (this.state.failed) {
      return <div className="panel-body wide" role="alert">
        <h1>{this.props.title}</h1>
        <Callout tone="err" stripe title={`Could not render ${this.props.title}`}
          action={<button type="button" onClick={this.retry}><RefreshCw size={14} strokeWidth={1.75} aria-hidden /> Retry</button>}>
          This screen could not finish loading. Try again.
        </Callout>
      </div>;
    }
    return this.props.children;
  }
}

const PANE_TITLES: Record<Tab, string> = {
  "first-run": "Setup", home: "Today", memory: "Memory", routing: "Accounts", settings: "Settings", guides: "Guides",
};

type CacheTab = "status" | "activity" | "revisions" | "doctor" | "rightNow";

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
    provider: "gemini",
    endpoint: "",
    model_id: "gemini-2.5-flash",
  },
  embed: { provider: "cloud", endpoint: "", model_id: "" },
  vision: { provider: "off", endpoint: "", model_id: "" },
  models_error: null,
};

const CACHE_TTL_MS = 60_000;

// Any user-typed value reaches argparse as its own argv element, so a value
// beginning with "-" would be read as a FLAG rather than as the value. Callers
// pass positional input after a "--" separator and flag input as "--name=value"
// (one token, unambiguous even when the value starts with a dash). Typing
// "--limit" into the search box used to produce an argparse usage error instead
// of a search (audit 2026-08-17).
async function runKhipu(args: string[]): Promise<string> {
  return invoke<string>("run_khipu", { args });
}

function parseJson(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
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

/** A Python traceback in a toast is its last non-empty line (the exception);
 *  the full text stays in the toast's title. */
function toastLine(text: string): string {
  return toastWords(text);
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

export default function App() {
  const [tab, setTab] = useState<Tab>("home");
  const [paneRetryKeys, setPaneRetryKeys] = useState<Record<Tab, number>>({
    "first-run": 0, home: 0, memory: 0, routing: 0, settings: 0, guides: 0,
  });
  // Every screen shares one scroll container (`.main-content`), so a screen
  // would open scrolled to wherever the last one was left. A layout effect runs
  // before the screens' own scroll-to-section effects, which still win.
  useLayoutEffect(() => {
    document.querySelector<HTMLElement>(".main-content")?.scrollTo?.({ top: 0 });
  }, [tab]);
  // Configuration presence lets setup proceed; it does not prove the database answers.
  const [dsnOk, setDsnOk] = useState<boolean | null>(null);
  // Needs you (slice A3): one read for Today's cards and the sidebar count.
  const needs = useNeedsYou(dsnOk === true && tab !== "first-run", tab === "home");
  const [dbReachable, setDbReachable] = useState<boolean | null>(null);
  const [loading, setLoading] = useState<Partial<Record<CacheTab, boolean>>>(
    {},
  );
  const [error, setError] = useState<string | null>(null);
  const fetchedAt = useRef<Partial<Record<CacheTab, number>>>({});
  // Doctor reads can overlap (a forced read after an install while a TTL read
  // is still running); only the newest request may write its answer.
  const doctorSeq = useRef(0);
  const dsnSeq = useRef(0);
  const feedbackButtonRef = useRef<HTMLButtonElement>(null);

  // The same report as `doctorText`, parsed — `HealthReportRows` iterates its
  // `*_ok` fields directly rather than re-parsing the pretty-printed string.
  const [doctorParsed, setDoctorParsed] = useState<Record<
    string,
    unknown
  > | null>(null);
  // Home's four tiles read these straight off the doctor payload; nothing here
  // is derived from a field the report does not carry.
  const [liveness, setLiveness] = useState<LivenessPayload | null>(null);
  const [t3Health, setT3Health] = useState<T3Health | null>(null);
  // The last recorded end-to-end recall probe. Harnesses reads it for its
  // "Verified …" line; nothing about that line is app-local state.
  const [recallProbe, setRecallProbe] = useState<RecallProbeStatus | null>(null);
  const [secretsPresence, setSecretsPresence] = useState<SecretsPresence | null>(
    null,
  );
  const [secretsPresenceMsg, setSecretsPresenceMsg] = useState<string | null>(
    null,
  );
  const [models, setModels] = useState<ModelsState>(DEFAULT_MODELS);
  const [modelsMsg, setModelsMsg] = useState<string | null>(null);
  const [modelsSaving, setModelsSaving] = useState(false);
  const modelCheck = useModelVerify();
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
    const seq = ++dsnSeq.current;
    try {
      const configured = await invoke<boolean>("dsn_configured", { force });
      if (seq !== dsnSeq.current) return;
      setDsnOk(configured);
      if (!configured) {
        setDbReachable(false);
        setTab("first-run");
        return;
      }
      const status = parseJson(await invoke<string>("khipu_db_status")) as
        | { ok?: unknown }
        | null;
      if (seq !== dsnSeq.current) return;
      setDbReachable(
        status?.ok === true ? true : status?.ok === false ? false : null,
      );
    } catch (e) {
      if (seq !== dsnSeq.current) return;
      setDbReachable(false);
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


  const loadDoctor = useCallback(async (force = false): Promise<string | null> => {
    if (!needsFetch("doctor", force)) return null;
    const seq = ++doctorSeq.current;
    markLoading("doctor", true);
    setError(null);
    try {
      const raw = await runKhipu(["doctor"]);
      if (seq !== doctorSeq.current) return null;
      const parsed = parseJson(raw) as Record<string, unknown> | null;
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        setError("Khipu's health check gave an unreadable answer.");
        return "Khipu's health check gave an unreadable answer.";
      }
      setDoctorParsed(parsed);
      setLiveness((parsed as { capture_liveness?: LivenessPayload }).capture_liveness ?? null);
      setT3Health((parsed as { t3?: T3Health }).t3 ?? null);
      setRecallProbe((parsed as { recall_probe?: RecallProbeStatus }).recall_probe ?? null);
      fetchedAt.current.doctor = Date.now();
      return null;
    } catch (e) {
      if (seq !== doctorSeq.current) return null;
      setError(String(e));
      return String(e);
    } finally {
      if (seq === doctorSeq.current) markLoading("doctor", false);
    }
  }, []);
  /** A key was saved: re-read what is stored and the health report. */
  const onKeySaved = useCallback(async () => {
    await loadSecretsPresence();
    await loadDoctor(true);
  }, [loadSecretsPresence, loadDoctor]);

  /** Memory › Project work lists: a finished line closes the owed item it
   *  carries (`khipu owed --close=ID`, as before). */
  const owedWrite = useCallback(async (id: number, args: string[]) => {
    try {
      const parsed = parseJson(await runKhipu(["owed", ...args])) as { ok?: boolean; error?: string } | null;
      if (parsed && parsed.ok === false) setError(parsed.error ?? `Item #${id} was not updated.`);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  /** The apps page's "search what T3 saved" opens Memory. */
  const openT3Captures = useCallback(() => setTab("memory"), []);

  // The apps page and the guides read capture liveness, T3 and the stored
  // recall probe out of the health report.
  useEffect(() => {
    if (!dsnOk) return;
    if (tab === "settings" || tab === "guides") void loadDoctor(false);
  }, [tab, dsnOk, loadDoctor]);

  const PLACE_TITLES: Partial<Record<Tab, string>> = {
    home: "Today", routing: "Accounts", memory: "Memory", settings: "Settings", guides: "Guides",
  };

  const [appVersion, setAppVersion] = useState<string>("…");
  const [noticeDialog, setNoticeDialog] = useState<LaunchPlan["dialog"]>(null);
  const [noticeCard, setNoticeCard] = useState<CardState | null>(null);
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [feedbackSending, setFeedbackSending] = useState(false);
  const [setupJoinPassphrase, setSetupJoinPassphrase] = useState("");
  const [setupJoinAllowUnencrypted, setSetupJoinAllowUnencrypted] = useState(false);
  const [setupJoinMsg, setSetupJoinMsg] = useState<string | null>(null);
  const [setupJoinExpected, setSetupJoinExpected] = useState<Counts | null>(null);
  const [advertiseBusy, setAdvertiseBusy] = useState(false);
  const [advertiseInfo, setAdvertiseInfo] = useState<{
    pin?: string;
    timeout_sec?: number;
    expires_at?: number;
    ipv4?: string;
  } | null>(null);
  // Settings opens on one row when a notice, a guide or a link asks for it.
  const [settingsRow, setSettingsRow] = useState<{ id: string; n: number } | null>(null);
  const openSettingsRow = useCallback((id: string) => {
    setSettingsRow((r) => ({ id, n: (r?.n ?? 0) + 1 }));
    setTab("settings");
  }, []);
  /** Where each section of the older Settings (and its routing screen) now
   *  lives, for the notices and guide steps that still name an old section. */
  const openOldSection = useCallback((section: string) => {
    if (section === "routing") { setAcctView("room"); setTab("routing"); return; }
    if (section === "projects") { setTab("memory"); return; }
    const row: Record<string, string> = {
      database: "storage", capture: "models", index: "models", features: "general", data: "storage",
      "another-mac": "connections", components: "storage", updates: "general", look: "general", advanced: "help",
      owner: "connections", work: "connections",
    };
    openSettingsRow(row[section] ?? "macs");
  }, [openSettingsRow]);

  // Plan usage (docs/plans/2026-10-14-plan-usage.md, phase 2). Doctor carries
  // only the cached figures; the app reads `khipu usage --json` itself (it can
  // probe on demand) and adds one attention item when any window still in
  // force is at or above 90%. One reader shared by Home and Settings ->
  // Accounts and routing, so the two never start the subscription CLIs twice; it
  // re-reads on entering either view. The item is re-judged every minute
  // while it shows and at its earliest reset, so it never outlives a reset
  // or freezes its countdown while Home stays open.
  const plans = usePlanUsage(
    tab === "guides" ? "guides" : tab === "home" ? "home" : tab === "routing" ? "settings" : null,
  );
  // Guides: the guide on screen (Grok Bot also takes the step to open at), and
  // the live evidence the page, the rail badge and Home's hint all read. It
  // re-reads when the user comes to one of the screens that shows it.
  const [guideOpen, setGuideOpen] = useState<{ id: GuideId; step?: number } | null>(null);
  const guideSignals = useGuideSignals(tab === "guides" ? "guides" : "other");
  const openGuide = (id: GuideId, step?: number) => {
    setGuideOpen({ id, step });
    setTab("guides");
  };
  const openGuideTarget = (t: GuideTarget) => {
    if (t.kind === "guide") openGuide(t.id);
    else if (t.kind === "harnesses") openSettingsRow("apps");
    else openOldSection(t.section);
  };
  // Routing truth phase 4: the routing record (`khipu route --json`), read on
  // Home and every minute while Home stays open. Its attention items warn and
  // block nothing (the record is guidance, not a gate).
  // Accounts and routing is its own rail destination; Work shows the accounts too.
  const routing = useRouting(tab === "home" || tab === "routing");
  // Accounts view: Room right now or Running now (Order and rules is still its older screen).
  const [todayAccountKey, setTodayAccountKey] = useState<string | null>(null);
  const [acctView, setAcctView] = useState<"room" | "running" | "rules">("room");
  const runningNow = useRunning(tab === "routing");
  // "Connect to a server you run or another Mac…" reopens the first-run flow
  // at the Database step rather than restarting it — `welcomeInitialStepKey`
  // is bumped on every click so a second visit still jumps even when
  // `welcomeInitialStep` itself does not change value.
  const [welcomeInitialStep, setWelcomeInitialStep] = useState<string | undefined>(undefined);
  const [welcomeInitialStepKey, setWelcomeInitialStepKey] = useState(0);
  useEffect(() => { if (tab !== "routing") setTodayAccountKey(null); }, [tab]);
  const needsRoutingSetup = routingNeedsSetup(routing.data);
  const openNoticeTarget = useCallback((target: NoticeTarget) => {
    if (target.kind === "home") setTab("home");
    else if (target.kind === "harnesses") openSettingsRow("apps");
    else if (target.kind === "settings") openOldSection(target.section);
    else {
      setWelcomeInitialStep(target.step);
      setWelcomeInitialStepKey((k) => k + 1);
      setTab("first-run");
    }
  }, []);
  const openDatabaseStepInFirstRun = useCallback(() => {
    setWelcomeInitialStep("database");
    setWelcomeInitialStepKey((k) => k + 1);
    setTab("first-run");
  }, []);
  const [dbHostKind, setDbHostKind] = useState<string | null>(null);
  const loadDbHostKind = useCallback(async () => {
    try {
      const out = parseJson(await invoke<string>("khipu_db_status")) as
        | { ok?: boolean; host_kind?: string }
        | null;
      setDbHostKind(out?.ok && typeof out.host_kind === "string" ? out.host_kind : null);
    } catch {
      setDbHostKind(null);
    }
  }, []);
  useEffect(() => {
    // Your Macs (another Mac) and the move dialog's target step read it.
    if (tab === "settings") void loadDbHostKind();
  }, [tab, loadDbHostKind]);

  // Settings › Database › "Move this memory to another database…" —
  // docs/plans/2026-09-05-setup-that-cannot-strand-you.md, "Move the
  // database": check the target, copy every table, then switch. The source
  // is never written to at any step.
  const [moveOpen, setMoveOpen] = useState(false);
  const [moveTarget, setMoveTarget] = useState<"server" | "local">("server");
  const [moveDsn, setMoveDsn] = useState("");
  const [moveStep, setMoveStep] = useState<"target" | "copy" | "done">("target");
  const [movePreflight, setMovePreflight] = useState<SetupPipelineResult>(null);
  const [movePreflightPhase, setMovePreflightPhase] = useState<SetupPhase>("idle");
  const [moveDryRun, setMoveDryRun] = useState<
    { name: string; source_rows: number }[] | null
  >(null);
  const [moveDryRunBusy, setMoveDryRunBusy] = useState(false);
  const [moveBusy, setMoveBusy] = useState(false);
  const [moveResult, setMoveResult] = useState<Record<string, unknown> | null>(null);
  const [moveError, setMoveError] = useState<string | null>(null);
  const moveAutoDryRunTarget = useRef<"server" | "local" | null>(null);

  const resetMoveDialog = useCallback(() => {
    setMoveOpen(false);
    setMoveTarget("server");
    setMoveDsn("");
    moveAutoDryRunTarget.current = null;
    setMoveStep("target");
    setMovePreflight(null);
    setMovePreflightPhase("idle");
    setMoveDryRun(null);
    setMoveResult(null);
    setMoveError(null);
  }, []);

  const checkMoveTarget = useCallback(async () => {
    const dsn = moveDsn.trim();
    if (!dsn) return;
    setMovePreflightPhase("running");
    setMovePreflight(null);
    setMoveError(null);
    try {
      const out = parseJson(await invoke<string>("khipu_db_preflight", { dsn })) as SetupPipelineResult;
      setMovePreflight(out);
      if (out?.ok === true) setMoveStep("copy");
    } catch (e) {
      setMoveError(String(e));
    } finally {
      setMovePreflightPhase("done");
    }
  }, [moveDsn]);

  const runMoveDryRun = useCallback(async () => {
    const dsn = moveDsn.trim();
    if (!dsn) return;
    setMoveDryRunBusy(true);
    setMoveError(null);
    try {
      const out = parseJson(
        await invoke<string>("khipu_db_move", { targetDsn: dsn, dryRun: true }),
      ) as { ok?: boolean; error?: string; detail?: string; tables?: { name: string; source_rows: number }[] } | null;
      if (out?.ok !== true) {
        setMoveError(String(out?.detail ?? out?.error ?? "Could not read the target's table counts."));
        return;
      }
      setMoveDryRun(out.tables ?? []);
    } catch (e) {
      setMoveError(String(e));
    } finally {
      setMoveDryRunBusy(false);
    }
  }, [moveDsn]);

  useEffect(() => {
    if (moveOpen && moveStep === "copy" && moveTarget === "server" && !moveDryRun && !moveDryRunBusy && moveAutoDryRunTarget.current !== moveTarget) {
      moveAutoDryRunTarget.current = moveTarget;
      void runMoveDryRun();
    }
  }, [moveOpen, moveStep, moveTarget, moveDryRun, moveDryRunBusy, runMoveDryRun]);

  const confirmMoveCopy = useCallback(async () => {
    setMoveBusy(true);
    setMoveError(null);
    try {
      const raw =
        moveTarget === "local"
          ? await invoke<string>("khipu_db_move_new_local", { dryRun: false })
          : await invoke<string>("khipu_db_move", { targetDsn: moveDsn.trim(), dryRun: false });
      const out = parseJson(raw) as Record<string, unknown> | null;
      if (!out || out.ok !== true) {
        setMoveError(
          String((out?.detail as string | undefined) ?? (out?.error as string | undefined) ?? "The move failed."),
        );
        return;
      }
      setMoveResult(out);
      setMoveStep("done");
      await refreshDsn(true);
      await loadDbHostKind();
    } catch (e) {
      setMoveError(String(e));
    } finally {
      setMoveBusy(false);
    }
  }, [moveTarget, moveDsn, refreshDsn, loadDbHostKind]);

  const moveConnectSummary = (moveResult?.connect as { stages?: { id?: string; detail?: string }[] } | undefined)
    ?.stages?.find((s) => s.id === "summary")?.detail;
  const moveRemaining = Array.isArray(moveResult?.remaining) ? (moveResult?.remaining as string[]) : [];
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
    const synthRefusal = synthModelError(models.synth.provider, models.synth.model_id);
    if (synthRefusal) {
      setModelsMsg(synthRefusal);
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
      // Re-read the health report so the provider's key / CLI status follows.
      void loadDoctor(true);
    } catch (e) {
      setModelsMsg(String(e));
    } finally {
      setModelsSaving(false);
    }
  }, [models, loadModels, loadDoctor]);

  useEffect(() => {
    // First-run needs this too: it tells the user which file to create, and
    // the data folder is relocatable from Settings. Loading it only for
    // Settings meant the onboarding screen named ~/.config/khipu no matter
    // where the DSN was actually supposed to go. `khipu paths` needs no DSN,
    // so it works on exactly the screen that exists because there isn't one.
    if (tab === "settings") {
      void loadModels();
      void loadSecretsPresence();
    }
  }, [tab, loadModels, loadSecretsPresence]);

  useEffect(() => {
    void getVersion()
      .then((v) => {
        setAppVersion(v);
        // What's new: the dialog shows once, the Home card stays until it is
        // closed there, then remember this version (see postUpdateNotices.ts).
        const plan = launchWhatsNew(v);
        setNoticeDialog(plan.dialog);
        setNoticeCard(plan.card);
      })
      .catch(() => setAppVersion("unknown"));
  }, []);

  // A database only this Mac can reach cannot be joined; otherwise a kit is
  // made only with a passphrase or the owner's explicit unencrypted opt-out.
  const joinKitBlocked = dbHostKind === "this-mac" || dbHostKind === "local-docker";
  const joinKitReady = setupJoinPassphrase.trim() !== "" || setupJoinAllowUnencrypted;

  const exportJoinKit = useCallback(async () => {
    const passphrase = setupJoinAllowUnencrypted ? "" : setupJoinPassphrase.trim();
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
        allowUnencrypted: setupJoinAllowUnencrypted,
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
      setSetupJoinPassphrase("");
      setSetupJoinMsg(
        `Saved ${passphrase ? "an encrypted" : "an unencrypted"} join kit to ${parsed.out ?? dest}`,
      );
    } catch (e) {
      setSetupJoinMsg(String(e));
    }
  }, [setupJoinPassphrase, setupJoinAllowUnencrypted]);

  const startJoinAdvertise = useCallback(async () => {
    const passphrase = setupJoinAllowUnencrypted ? "" : setupJoinPassphrase.trim();
    setAdvertiseBusy(true);
    setSetupJoinMsg(null);
    setAdvertiseInfo(null);
    try {
      const raw = await invoke<string>("join_advertise", {
        passphrase,
        timeout: 600,
        allowUnencrypted: setupJoinAllowUnencrypted,
      });
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
      setSetupJoinPassphrase("");
      setAdvertiseInfo({
        pin: parsed.pin,
        timeout_sec: parsed.timeout_sec,
        expires_at: parsed.expires_at,
        ipv4: typeof parsed.ipv4 === "string" ? parsed.ipv4 : undefined,
      });
      setSetupJoinMsg(
        `Nearby setup started — on the new Mac: Join existing Khipu → PIN ${parsed.pin ?? "?"} → Find nearby Mac. Connection has not been checked.`,
      );
    } catch (e) {
      setSetupJoinMsg(String(e));
    } finally {
      setAdvertiseBusy(false);
    }
  }, [setupJoinPassphrase, setupJoinAllowUnencrypted]);

  const workingLabel = (() => {
    if (feedbackSending) return "Sending feedback…";
    if (advertiseBusy) return "Advertising a join PIN…";
    // The health check reads for a long time on Settings; the rail item shows
    // it busy and the page's own rows say "Checking…", so no pill sits over text.
    return null;
  })();
  const tabBusy = (id: Tab): boolean => id === "settings" && Boolean(loading.doctor);

  const panelClass = (id: Tab) =>
    tab === id ? "panel" : "panel is-hidden";

  const retryActivePane = async (): Promise<void> => {
    const activeTab = tab;
    switch (activeTab) {
      case "home": await Promise.all([refreshDsn(true), routing.load()]); break;
      case "routing": await Promise.all([routing.load(true), plans.load()]); break;
      case "settings": await Promise.all([refreshDsn(true), loadDoctor(true), loadModels(), loadSecretsPresence()]); break;
      case "first-run": await refreshDsn(true); break;
      case "guides": await guideSignals.reload(); break;
      case "memory": break;
    }
    setPaneRetryKeys((keys) => ({ ...keys, [activeTab]: keys[activeTab] + 1 }));
  };

  const [counts, setCounts] = useState<Counts | null>(null);
  useEffect(() => {
    if (!dsnOk || tab !== "guides") return;
    let alive = true;
    void runKhipu(["status"])
      .then((raw) => {
        const parsed = parseJson(raw) as { counts?: Counts } | null;
        if (alive) setCounts(parsed?.counts ?? null);
      })
      .catch(() => {});
    return () => { alive = false; };
  }, [tab, dsnOk]);
  const t3Gap = t3HealthGap(t3Health);
  // Guides: four "Start here" items ticked from live state, each guide row's
  // state, and the rail badge (setups started and not finished, plus problems).
  const accountsRead = (plans.data?.accounts ?? []).filter((a) => liveWords(a).live).length;
  const guideStartSignals = {
    liveness,
    gatewayCheckOk: guideSignals.prefs.grokCheck?.ok === true,
    episodes: counts?.episodes ?? null,
    accountsRead,
    agentAsked: guideSignals.agentAsked,
  };
  const guideStartItems = buildStartItems(guideStartSignals);
  const guideProblems = liveness ? (liveness.red ?? []).length : null;
  // "When something turns red" follows doctor itself, not only capture
  // liveness: it must not say "No problems" while doctor is red.
  const doctorProblems = (() => {
    if (!doctorParsed) return null;
    const n = buildAttention(doctorParsed).items.length + ((t3Health?.warnings ?? []).length > 0 ? 1 : 0);
    return n > 0 ? n : doctorParsed.ok === false ? 1 : 0;
  })();
  const guideRowStates = guideStates({
    ...guideStartSignals,
    grok: guideSignals.grok,
    problems: guideProblems,
    doctorProblems,
    t3: t3Gap,
  });
  // Every setup item reads live state that arrives after first paint. Until
  // each source has answered (or failed, or the database is unreachable) the
  // hint says it is checking, instead of "2 of 4 done" from empty defaults.
  const setupSignalsKnown =
    guideSignals.logSettled &&
    (liveness != null || error != null || dbReachable === false) &&
    (counts != null || error != null || dbReachable === false) &&
    (plans.data != null || plans.error != null || dbReachable === false);
  const goTab = (id: string) => setTab(id as Tab);
  // The name the owner gave this Mac comes with the routing record (`this_mac`);
  // until it has been read the sidebar says "On this Mac".
  const thisMac: string | null = routing.data?.this_mac ?? null;
  const placeItem = (id: Tab, icon: ReactNode, hint: string, extra: Partial<NavItem> = {}): NavItem => ({
    id, label: PLACE_TITLES[id] ?? id, icon, hint, busy: tabBusy(id), ...extra,
  });
  // Today's count is the Needs-you list itself (slice A3), the same read Today shows.
  const needsCount = needs.data ? needs.data.total : null;
  const places: NavItem[] = [
    placeItem("home", <CircleCheck size={18} aria-hidden />, "What needs you, and whether Khipu is working", {
      count: needsCount,
      countLabel: needsCount === 1 ? "1 needs you" : `${needsCount ?? 0} need you`,
    }),
    placeItem("routing", <Gauge size={18} aria-hidden />, "Which AI has room, and who gets what work"),
    placeItem("memory", <Search size={18} aria-hidden />, "Find what you said"),
  ];
  const settingsItem = placeItem("settings", <SlidersHorizontal size={18} aria-hidden />, "General, models, storage, connections and help");
  const toolbarTitle = PLACE_TITLES[tab];
  // Settings › Your Macs › Add another Mac: the older Settings' Another Mac section, unchanged.
  const anotherMacSection = (
                  <>
                  <GatewayUrlCard active={tab === "settings"} />
                  <div className="section-card">
                    <div className="section-head">Set up another Mac</div>
                    <div className="section-body">
                      {dbHostKind === "this-mac" || dbHostKind === "local-docker" ? (
                        <Callout
                          tone="warn"
                          title="This database lives only on this Mac, so another Mac cannot reach it."
                          action={
                            <button type="button" className="primary" onClick={() => setMoveOpen(true)}>
                              Move it to a server…
                            </button>
                          }
                        />
                      ) : null}
                      <p className="muted">
                        Do this on the Mac that <strong>already works</strong>. Save
                        a join kit and AirDrop it to the new Mac; that file is
                        enough. The kit holds the database password and any API
                        keys, so it is locked with a passphrase you type on the new
                        Mac.
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
                      <label className="muted" htmlFor="join-kit-passphrase">
                        Join kit passphrase
                      </label>
                      <div className="toolbar" style={{ width: "100%" }}>
                        <input
                          id="join-kit-passphrase"
                          className="mono"
                          type="password"
                          autoComplete="off"
                          spellCheck={false}
                          value={setupJoinPassphrase}
                          onChange={(e) => setSetupJoinPassphrase(e.target.value)}
                          placeholder="Type a passphrase for the new Mac"
                          disabled={joinKitBlocked || setupJoinAllowUnencrypted}
                        />
                      </div>
                      <label className="row-meta">
                        <input
                          type="checkbox"
                          checked={setupJoinAllowUnencrypted}
                          disabled={joinKitBlocked}
                          onChange={(e) => setSetupJoinAllowUnencrypted(e.target.checked)}
                        />{" "}
                        I understand: make it unencrypted
                      </label>
                      {setupJoinAllowUnencrypted ? (
                        <Callout
                          tone="warn"
                          title="Anyone who gets this file can read the database password and API keys."
                        />
                      ) : !joinKitReady && !joinKitBlocked ? (
                        <p className="muted">Type a passphrase to save or advertise a join kit.</p>
                      ) : null}
                      <div className="toolbar">
                        <button
                          type="button"
                          className="primary"
                          disabled={joinKitBlocked || !joinKitReady}
                          onClick={() => void exportJoinKit()}
                        >
                          Save join kit…
                        </button>
                      </div>
                      <p className="muted">
                        Both Macs must be on the same Wi‑Fi or network; guest and
                        hotel networks usually block this.
                      </p>
                      <div className="toolbar">
                        <button
                          type="button"
                          disabled={advertiseBusy || joinKitBlocked || !joinKitReady}
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
                      {setupJoinMsg ? <pre className="code">{setupJoinMsg}</pre> : null}
                    </div>
                  </div>
                  </>
  );
  const settingsCtx: SettingsCtx = {
    appVersion,
    thisMac,
    runKhipu,
    onToast: (m) => setError(m),
    liveness,
    recallProbe,
    t3: t3Health,
    refreshHealth: () => loadDoctor(true),
    onSearchT3Captures: openT3Captures,
    onOpenGuide: (id) => openGuide(id, id === "grok_bot" ? guideSignals.grok.current : undefined),
    synth: models.synth,
    modelsError: models.models_error,
    modelsSaving,
    modelsMsg,
    onSynthChange: (patch) => updateModelRole("synth", patch),
    onSaveModels: saveModels,
    secretsPresence,
    secretsPresenceMsg,
    modelCheck,
    onKeySaved,
    doctorSynth: (doctorParsed?.synth_provider as DoctorSynthProvider | undefined) ?? null,
    dsnOk,
    dbReachable,
    onMoveMemory: () => setMoveOpen(true),
    onOpenGuides: () => setTab("guides"),
    onFeedback: () => setFeedbackOpen(true),
    onDatabaseStep: openDatabaseStepInFirstRun,
    onRunSetup: () => setTab("first-run"),
    anotherMac: anotherMacSection,
    whatsNew: (
      <>
        {noticeCard ? (
          <WhatsNewCard
            card={noticeCard}
            appVersion={appVersion}
            needsRoutingSetup={needsRoutingSetup}
            onShow={openNoticeTarget}
            onClose={() => {
              writeCardState(null);
              setNoticeCard(null);
            }}
            onSeeAll={() => openSettingsRow("updates")}
          />
        ) : null}
        <WhatsNewHistory appVersion={appVersion} needsRoutingSetup={needsRoutingSetup} onShow={openNoticeTarget} />
      </>
    ),
    openRow: settingsRow,
  };

  return (
    <div className="shell sh-window">
      <Sidebar
        brand={<BrandMark />}
        items={places}
        current={tab}
        onGo={goTab}
        settings={settingsItem}
        macName={thisMac}
      />

      <main className="main" aria-busy={workingLabel != null || Boolean(loading.doctor)}>
        {tab === "routing" ? (
          <Toolbar title="Accounts">
            <ViewSwitch
              label="Accounts view"
              options={[
                { id: "room", label: "Room right now" },
                { id: "running", label: "Running now" },
                { id: "rules", label: "Order and rules" },
              ]}
              value={acctView}
              onChange={setAcctView}
            />
          </Toolbar>
        ) : toolbarTitle && tab !== "memory" && tab !== "guides" ? <Toolbar title={toolbarTitle} heading={false} /> : null}
        <WorkingBanner label={workingLabel} />
        {/* The visible pill is gone, but the health-check read is still announced. */}
        <span className="sr-only" role="status">{loading.doctor ? "Reading the health check…" : ""}</span>
        <div className="main-content">
        <section className={panelClass("first-run")}>
          <PaneBoundary key={paneRetryKeys["first-run"]} title={PANE_TITLES["first-run"]} onRetry={retryActivePane}>
          <div className="panel-body onboard-wrap" data-tauri-drag-region>
            <Welcome
              dsnOk={dsnOk}
              refreshDsn={() => refreshDsn(true)}
              runKhipu={runKhipu}
              onFinish={() => {
                // Finishing setup covers what's new, so record the version.
                recordVersionOnSetupFinish(appVersion);
                setTab("home");
              }}
              openIntegrations={() => openSettingsRow("apps")}
              initialStep={welcomeInitialStep}
              initialStepKey={welcomeInitialStepKey}
            />
          </div>
          </PaneBoundary>
        </section>

        <section className={tab === "home" ? "panel fill today-panel" : "panel is-hidden"}>
          <PaneBoundary key={paneRetryKeys["home"]} title="Today" onRetry={retryActivePane}>
            <Today
              active={tab === "home"}
              needs={needs}
              view={routing.data} plans={plans.data} routingError={routing.error}
              onOpenAccount={(key) => { setTodayAccountKey(key); setAcctView("room"); setTab("routing"); }}
              onOpen={(link) => {
                if (link === "running-now") { setAcctView("running"); setTab("routing"); }
                else if (link === "accounts") setTab("routing");
                else if (link === "search") setTab("memory");
                else setTab("settings");
              }}
            />
          </PaneBoundary>
        </section>

        <section className={`${panelClass("memory")} fill`}>
          <PaneBoundary key={paneRetryKeys["memory"]} title={PANE_TITLES["memory"]} onRetry={retryActivePane}>
            {tab === "memory" ? (
              <Memory
                run={runKhipu}
                onOwedClose={(id) => owedWrite(id, [`--close=${id}`])}
              />
            ) : null}
          </PaneBoundary>
        </section>

        <section className={panelClass("guides")}>
          <PaneBoundary key={paneRetryKeys["guides"]} title={PANE_TITLES["guides"]} onRetry={retryActivePane}>
          <PanelHeader
            title="Guides"
            lede="Step-by-step help for setting up Khipu and each tool it works with."
          />
          <div className="panel-body wide">
            <GuidesPage
              runKhipu={runKhipu}
              signals={guideSignals}
              startItems={guideStartItems}
              setupKnown={{ prefs: guideSignals.prefsLoaded, signals: setupSignalsKnown }}
              states={guideRowStates}
              hasT3={t3Health != null}
              open={guideOpen}
              onOpen={(id) => openGuide(id)}
              onBack={() => setGuideOpen(null)}
              onTarget={openGuideTarget}
            />
          </div>
          </PaneBoundary>
        </section>

        <section className={`${panelClass("routing")} ac-panel`}>
          <PaneBoundary key={paneRetryKeys.routing} title={PANE_TITLES.routing} onRetry={retryActivePane}>
            {tab === "routing" && acctView === "running" ? <RunningNow hook={runningNow} /> : null}
            {tab === "routing" && acctView === "rules" ? <OrderAndRules active /> : null}
            {tab === "routing" && acctView === "room" ? (
              <Accounts
                initialAccountKey={todayAccountKey}
                routing={routing}
                plans={plans.data}
                thisMac={thisMac}
                onReloadPlans={() => plans.load()}
                running={runningNow}
              />
            ) : null}
          </PaneBoundary>
        </section>

        <section className={panelClass("settings")}>
          <PaneBoundary key={paneRetryKeys["settings"]} title={PANE_TITLES["settings"]} onRetry={retryActivePane}>
          <Settings ctx={settingsCtx} active={tab === "settings"} />
          </PaneBoundary>
        </section>
        </div>
      </main>
      <TabBar items={places} current={tab} onGo={goTab} />

      <FeedbackForm
        open={feedbackOpen}
        appVersion={appVersion}
        onClose={() => setFeedbackOpen(false)}
        onSendingChange={setFeedbackSending}
        returnFocusRef={feedbackButtonRef}
      />

      <PostUpdateNoticeDialog
        plan={noticeDialog}
        needsRoutingSetup={needsRoutingSetup}
        onShow={openNoticeTarget}
        onDismiss={() => setNoticeDialog(null)}
      />

      {/* Settings › Database › "Move this memory to another database…" —
          docs/plans/2026-09-05-setup-that-cannot-strand-you.md, "Move the
          database". Three steps: check the target, copy, done. */}
      <Dialog
        open={moveOpen}
        className="kit-dialog"
        ariaLabelledBy="move-title"
        onCancel={resetMoveDialog}
      >
        <div className="kit-dialog-body">
          <h2 id="move-title">Move this memory to another database</h2>
          <p className="muted">
            The database you use now is never modified — Khipu only reads
            from it, and only until the copy finishes.
          </p>

          {moveStep === "target" ? (
            <>
              <Segmented
                ariaLabel="Move target"
                value={moveTarget}
                onChange={(v) => {
                  setMoveTarget(v);
                  moveAutoDryRunTarget.current = null;
                }}
                options={[
                  { value: "server", label: "A server I run" },
                  { value: "local", label: "A new database on this Mac" },
                ]}
              />
              {moveTarget === "server" ? (
                <>
                  <p className="muted" style={{ marginTop: "0.5rem" }}>
                    <strong>Connection string</strong> — looks like{" "}
                    <code>postgresql://user:password@host:5432/dbname</code>;
                    your host shows it in the database's connection details.
                  </p>
                  <div className="toolbar" style={{ width: "100%" }}>
                    <input
                      className="mono"
                      type="password"
                      autoComplete="off"
                      spellCheck={false}
                      value={moveDsn}
                      onChange={(e) => setMoveDsn(e.target.value)}
                      placeholder="postgresql://USER:PASSWORD@HOST:5432/DB?sslmode=verify-full"
                      aria-label="Target connection string"
                    />
                  </div>
                  <p className="muted">
                    Custom certificate files are not supported for moves yet.
                  </p>
                  <div className="toolbar">
                    <button
                      type="button"
                      className="primary"
                      disabled={!moveDsn.trim() || movePreflightPhase === "running"}
                      onClick={() => void checkMoveTarget()}
                    >
                      {movePreflightPhase === "running" ? "Checking…" : "Check the target"}
                    </button>
                  </div>
                  {movePreflightPhase !== "idle" ? (
                    <SetupStages
                      stageIds={["reach", "version", "privileges", "schema", "graph"]}
                      result={movePreflight}
                      phase={movePreflightPhase}
                      busy={movePreflightPhase === "running"}
                      onRetry={() => void checkMoveTarget()}
                    />
                  ) : null}
                </>
              ) : (
                <div className="toolbar" style={{ marginTop: "0.5rem" }}>
                  <button
                    type="button"
                    className="primary"
                    disabled={moveBusy}
                    onClick={() => void confirmMoveCopy()}
                  >
                    {moveBusy ? "Setting up and copying…" : "Set up a new local database and copy everything"}
                  </button>
                </div>
              )}
              {moveError ? (
                <Callout tone="err" title="Needs you">
                  {moveError}
                </Callout>
              ) : null}
            </>
          ) : null}

          {moveStep === "copy" ? (
            <>
              {moveDryRunBusy ? (
                <p className="muted">Reading table counts from the target…</p>
              ) : moveDryRun ? (
                <>
                  <div className="rows">
                    {moveDryRun.map((t) => (
                      <div key={t.name} className="row-item">
                        <span className="row-main mono">{t.name}</span>
                        <span className="row-meta">{t.source_rows} rows</span>
                      </div>
                    ))}
                  </div>
                  <div className="toolbar">
                    <button
                      type="button"
                      className="primary"
                      disabled={moveBusy}
                      onClick={() => void confirmMoveCopy()}
                    >
                      {moveBusy
                        ? "Copying…"
                        : `Copy ${moveDryRun.length} table${moveDryRun.length === 1 ? "" : "s"}`}
                    </button>
                  </div>
                </>
              ) : null}
              {moveError ? (
                <Callout
                  tone="err"
                  title="Needs you"
                  action={
                    <button type="button" className="primary" onClick={() => void runMoveDryRun()}>
                      Try again
                    </button>
                  }
                >
                  {moveError}
                </Callout>
              ) : null}
            </>
          ) : null}

          {moveStep === "done" ? (
            <>
              <Callout tone="ok" title="Working">
                {moveConnectSummary ?? "Working."}
              </Callout>
              {moveRemaining.length ? (
                <Callout
                  tone="warn"
                  title="Other Macs need a new join kit"
                  action={
                    <button
                      type="button"
                      onClick={() => {
                        resetMoveDialog();
                        openSettingsRow("macs");
                      }}
                    >
                      Open Another Mac…
                    </button>
                  }
                >
                  {moveRemaining[0]}
                </Callout>
              ) : null}
            </>
          ) : null}

          <div className="kit-dialog-actions">
            <button type="button" onClick={resetMoveDialog}>
              {moveStep === "done" ? "Close" : "Cancel"}
            </button>
          </div>
        </div>
      </Dialog>

      {error ? (
        <div className="toast-err" role="alert">
          <span className="toast-msg" title={error}>
            {toastLine(error)}
          </span>
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
