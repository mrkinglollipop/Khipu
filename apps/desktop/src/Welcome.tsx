import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { open as openFileDialog } from "@tauri-apps/plugin-dialog";
import { resourceDir } from "@tauri-apps/api/path";
import { Check, ChevronLeft, ChevronRight, Loader2 } from "lucide-react";
import { WorkingBanner } from "./WorkingBanner";
import { Callout, Tag } from "./ui";
import { SetupStages, plainWords } from "./SetupStages";
import type { SetupPhase, SetupPipelineResult } from "./SetupStages";
import { buildAttention, type Attention } from "./doctorAttention";
import type { RecallProbeStatus } from "./IntegrationsPanel";
import { ModelCheckRow, modelCheckFor, type ModelVerifyResult } from "./modelVerify";

/** `docs/plans/2026-09-05-setup-that-cannot-strand-you.md` stage ids.
 *  Preflight (`khipu db preflight`) only ever reaches `reach..graph`; a full
 *  connect (`khipu db connect`) runs all nine. */
export const PREFLIGHT_STAGE_IDS = ["reach", "version", "privileges", "schema", "graph"] as const;
export const CONNECT_STAGE_IDS = [
  "reach",
  "version",
  "privileges",
  "schema",
  "graph",
  "store",
  "upkeep",
  "prove",
  "summary",
] as const;

/** Persisted so the tutorial opens itself only until it has been finished once;
 *  it stays reachable from Setup → Welcome afterwards. */
export const WELCOME_DONE_KEY = "khipu.welcome.completed";

export function welcomeCompleted(): boolean {
  try {
    return window.localStorage.getItem(WELCOME_DONE_KEY) === "1";
  } catch {
    return false;
  }
}

export const SUPPORT_EMAIL = "support@kinglollipop.com";

type StepId = "welcome" | "database" | "model" | "graph" | "agents" | "finish";
type DbMode = "join" | "local" | "remote";

const STEPS: { id: StepId; label: string }[] = [
  { id: "welcome", label: "Welcome" },
  { id: "database", label: "Database" },
  { id: "model", label: "Model" },
  { id: "graph", label: "Graph" },
  { id: "agents", label: "Agents" },
  { id: "finish", label: "Finish" },
];

function joinFailMessage(
  out: Record<string, unknown> | null,
  fallback: string,
): string {
  const counts = out?.counts as { mismatches?: unknown } | undefined;
  const mismatch = Array.isArray(counts?.mismatches)
    ? (counts.mismatches as string[]).join("; ")
    : "";
  return String(out?.error || out?.warning || mismatch || fallback);
}

function parse(raw: string): Record<string, unknown> | null {
  try {
    const v = JSON.parse(raw);
    return v && typeof v === "object" ? (v as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

type MigratePlan = { applied?: string[]; pending?: string[]; ran?: string[] };
type Presence = {
  gemini_in_keychain?: boolean;
  gemini_env?: boolean;
  openai_compat_in_keychain?: boolean;
};
type HarnessRow = {
  harness: string;
  detected?: boolean;
  installed?: boolean;
  mcp?: boolean;
  hook_stop?: boolean;
  hook_precompact?: boolean;
  last_beat_at?: string | null;
};
type DoctorPayload = {
  ok?: boolean;
  not_configured?: string[];
  backup_ok?: boolean;
  drift_ok?: boolean;
  graph_drift_ok?: boolean;
  outbox_ok?: boolean;
  capture_liveness_ok?: boolean;
  git_sync_ok?: boolean;
  dsn_file_ok?: boolean;
  index_freshness_ok?: boolean;
  embed_coverage_ok?: boolean;
  graph_backup_ok?: boolean;
  graph_offsite_ok?: boolean;
  recall_probe_ok?: boolean;
  recall_probe?: RecallProbeStatus;
  bundle_seal_ok?: boolean;
  hub_snapshot?: { ok?: boolean };
};

/** True once at least one harness's own last probe (`probe-<harness>.json`)
 *  passed and is not stale — real evidence that recall works end to end,
 *  even when the single most-recent probe across every harness (what
 *  `recall_probe_ok` gates on) happens to be a different, older or failed
 *  run. Finish only runs its own app-labeled probe when this is false: if a
 *  harness has already proven the round trip, running a second one from
 *  Finish would just be noise. */
function anyHarnessVerified(rp: RecallProbeStatus | null | undefined): boolean {
  const harnesses = rp?.harnesses;
  if (!harnesses) return false;
  return Object.values(harnesses).some((v) => v?.ok === true && v?.stale !== true);
}

/** True when the bundle is on the shipped DMG volume, not /Applications or a
 *  checkout on an external volume. Volume name is `Khipu` (see
 *  release_macos.sh DMG_VOLNAME). Duplicate mounts show up as `Khipu 1`. */
function launchedFromDiskImage(resourcePath: string): boolean {
  return /\/Volumes\/Khipu(?: \d+)?\//.test(resourcePath);
}

function payloadError(v: Record<string, unknown> | null): string | null {
  if (!v) return "Invalid response";
  if (v.ok === false) return String(v.error ?? "Request failed");
  return null;
}

export function Welcome({
  dsnOk,
  refreshDsn,
  runKhipu,
  onFinish,
  openIntegrations,
  initialStep,
  initialStepKey,
}: {
  dsnOk: boolean | null;
  refreshDsn: () => Promise<void>;
  runKhipu: (args: string[]) => Promise<string>;
  onFinish: () => void;
  openIntegrations: () => void;
  /** Jump to this step instead of the usual "welcome" start — Settings ›
   *  Database's "Connect to a server you run or another Mac…" reopens this
   *  flow at the Database step rather than restarting from step 1. */
  initialStep?: StepId;
  /** Bumped by the caller on every click so revisiting the same
   *  `initialStep` (the user backed out and clicked the Settings link again)
   *  still jumps — `initialStep` alone would not change value the second
   *  time, so nothing would tell this component to re-read it. */
  initialStepKey?: number;
}) {
  const [step, setStep] = useState<StepId>(initialStep ?? "welcome");
  const idx = STEPS.findIndex((s) => s.id === step);
  const go = (n: number) => setStep(STEPS[Math.max(0, Math.min(STEPS.length - 1, idx + n))].id);

  useEffect(() => {
    if (initialStepKey) setStep(initialStep ?? "welcome");
    // Only the bump should retrigger this — `initialStep` alone can repeat
    // ("database" twice in a row) without a fresh navigation to react to.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialStepKey]);

  const [fromDiskImage, setFromDiskImage] = useState(false);
  useEffect(() => {
    void resourceDir()
      .then((p) => setFromDiskImage(launchedFromDiskImage(p)))
      .catch(() => setFromDiskImage(false));
  }, []);

  const [dbMode, setDbMode] = useState<DbMode>("join");
  const [dockerOk, setDockerOk] = useState<boolean | null>(null);
  const [dockerErr, setDockerErr] = useState<string | null>(null);
  const [diskWarn, setDiskWarn] = useState<string | null>(null);
  const [dbBusy, setDbBusy] = useState(false);
  const [dbMsg, setDbMsg] = useState<string | null>(null);
  const [localReady, setLocalReady] = useState(false);

  const [remoteDsn, setRemoteDsn] = useState("");
  const [remoteRootCrt, setRemoteRootCrt] = useState("");
  const [remoteBusy, setRemoteBusy] = useState(false);
  const [remoteReady, setRemoteReady] = useState(false);
  const [remoteStageIds, setRemoteStageIds] =
    useState<readonly string[]>(PREFLIGHT_STAGE_IDS);
  const [remotePhase, setRemotePhase] = useState<SetupPhase>("idle");
  const [remoteResult, setRemoteResult] = useState<SetupPipelineResult>(null);

  /** After `install_local_postgres`/a join import already stored the DSN,
   *  `khipu_db_connect_stored` finishes the pipeline (upkeep + prove) that
   *  neither of those calls ran on its own — audit gap #1. Shared by both
   *  modes since both just need "run the rest of the pipeline now". */
  const [finishStageResult, setFinishStageResult] = useState<SetupPipelineResult>(null);
  const [finishPhase, setFinishPhase] = useState<SetupPhase>("idle");
  const finishStoredConnect = useCallback(async () => {
    setFinishPhase("running");
    setFinishStageResult(null);
    try {
      const raw = await invoke<string>("khipu_db_connect_stored");
      const out = parse(raw) as SetupPipelineResult;
      setFinishStageResult(out);
      return out?.ok === true;
    } catch (e) {
      setFinishStageResult({ ok: false, error: String(e) });
      return false;
    } finally {
      setFinishPhase("done");
    }
  }, []);

  const [joinPassphrase, setJoinPassphrase] = useState("");
  const [joinPin, setJoinPin] = useState("");
  const [joinBusy, setJoinBusy] = useState(false);
  const [joinReady, setJoinReady] = useState(false);
  const [joinedHub, setJoinedHub] = useState(false);
  const [joinExpected, setJoinExpected] = useState<Record<string, number> | null>(null);
  const [joinLive, setJoinLive] = useState<Record<string, number> | null>(null);

  const [newCodeRoot, setNewCodeRoot] = useState("");
  const [graphSourcesMsg, setGraphSourcesMsg] = useState<string | null>(null);

  const [plan, setPlan] = useState<MigratePlan | null>(null);
  const [planErr, setPlanErr] = useState<string | null>(null);
  const [migrating, setMigrating] = useState(false);

  const loadPlan = useCallback(async () => {
    setPlanErr(null);
    try {
      setPlan(parse(await invoke<string>("khipu_migrate", { dryRun: true })) as MigratePlan);
    } catch (e) {
      setPlan(null);
      setPlanErr(String(e));
    }
  }, []);

  const applyMigrations = useCallback(async () => {
    setMigrating(true);
    setPlanErr(null);
    try {
      setPlan(parse(await invoke<string>("khipu_migrate", { dryRun: false })) as MigratePlan);
    } catch (e) {
      setPlanErr(String(e));
    } finally {
      setMigrating(false);
    }
  }, []);

  const refreshDocker = useCallback(async () => {
    setDockerErr(null);
    try {
      const raw = await invoke<string>("components_status");
      const v = parse(raw);
      const docker = v?.docker as { ok?: boolean; error?: string } | undefined;
      setDockerOk(Boolean(docker?.ok));
      if (!docker?.ok) setDockerErr(String(docker?.error ?? "Docker is not running"));
    } catch (e) {
      setDockerOk(false);
      setDockerErr(String(e));
    }
  }, []);

  useEffect(() => {
    if (step === "database" && dbMode === "local") void refreshDocker();
  }, [step, dbMode, refreshDocker]);

  // "the Docker step polls every 5 s after the download link opens and
  // advances by itself" — once the user clicks the download link, this Mac
  // does not yet have Docker, so there is nothing to do but wait; polling
  // means the person never has to remember to come back and click Recheck.
  // Capped at 15 minutes (an install this Mac cannot finish is a Docker
  // problem to go solve, not something to poll forever for).
  const [dockerLinkClicked, setDockerLinkClicked] = useState(false);
  const dockerPollCount = useRef(0);
  useEffect(() => {
    if (!(step === "database" && dbMode === "local" && dockerLinkClicked) || dockerOk === true) {
      dockerPollCount.current = 0;
      return;
    }
    const DOCKER_POLL_MS = 5_000;
    const DOCKER_POLL_MAX = 180; // 15 min at 5 s
    const id = window.setInterval(() => {
      dockerPollCount.current += 1;
      if (dockerPollCount.current > DOCKER_POLL_MAX) {
        window.clearInterval(id);
        return;
      }
      void refreshDocker();
    }, DOCKER_POLL_MS);
    return () => window.clearInterval(id);
  }, [step, dbMode, dockerLinkClicked, dockerOk, refreshDocker]);

  useEffect(() => {
    if (step === "database" && dsnOk) void loadPlan();
  }, [step, dsnOk, loadPlan]);

  const runLocalSetup = useCallback(async () => {
    setDbBusy(true);
    setDbMsg(null);
    setDiskWarn(null);
    setLocalReady(false);
    try {
      const statusRaw = await invoke<string>("components_status");
      const status = parse(statusRaw);
      const docker = status?.docker as { ok?: boolean; error?: string } | undefined;
      setDockerOk(Boolean(docker?.ok));
      if (!docker?.ok) {
        setDockerErr(String(docker?.error ?? "Docker is not running"));
        setDbMsg(docker?.error ? String(docker.error) : "Install Docker Desktop, start it, then recheck.");
        return;
      }
      let raw = await invoke<string>("select_compat_row", { mode: "local_docker" });
      let v = parse(raw);
      let err = payloadError(v);
      if (err) {
        setDbMsg(err);
        return;
      }
      raw = await invoke<string>("install_local_postgres");
      v = parse(raw);
      err = payloadError(v);
      if (err) {
        setDbMsg(err);
        return;
      }
      const disk = v?.disk as { warning?: string; free_gib?: number } | undefined;
      if (disk?.warning === "low_disk_space") {
        setDiskWarn(
          `Free disk is about ${disk.free_gib ?? "?"} GiB — the database needs headroom. You can continue after freeing space.`,
        );
      }
      raw = await invoke<string>("bootstrap_local_backup");
      v = parse(raw);
      err = payloadError(v);
      if (err) {
        setDbMsg(`The backup test failed: ${err}`);
        return;
      }
      await refreshDsn();
      await loadPlan();
      setLocalReady(true);
      setDbMsg("The database on this Mac is running, and a test restore of its backup worked.");
      // install_local_postgres already stored the DSN itself; finish the rest
      // of the pipeline (nightly upkeep + a real capture-to-search round
      // trip) the same way a remote connect does — this never ran from the
      // local-setup path before (audit gap #1).
      await finishStoredConnect();
    } catch (e) {
      setDbMsg(String(e));
    } finally {
      setDbBusy(false);
    }
  }, [refreshDsn, loadPlan, finishStoredConnect]);

  /** "Connect to a database I already run" — the one-pipeline flow from
   *  docs/plans/2026-09-05-setup-that-cannot-strand-you.md: preflight
   *  (reach..graph, nothing written) first, and only if all five pass does
   *  the real connect run (store, upkeep, prove, summary). Each call is a
   *  single round trip, so the five/nine rows go to "running" together and
   *  fill together — there is no per-stage progress to stream. */
  const connectRemote = useCallback(async (overrideRootCrt?: string) => {
    const dsn = remoteDsn.trim();
    if (!dsn) return;
    // Accepts an override so a freshly-picked certificate path can be used
    // immediately, without waiting a render for `remoteRootCrt` state (which
    // this closure would otherwise still see as stale in the same tick).
    const rootCrt = (overrideRootCrt ?? remoteRootCrt).trim() || undefined;
    setRemoteBusy(true);
    setRemoteReady(false);
    setRemoteStageIds(PREFLIGHT_STAGE_IDS);
    setRemotePhase("running");
    setRemoteResult(null);
    try {
      const preRaw = await invoke<string>("khipu_db_preflight", { dsn });
      const pre = parse(preRaw) as SetupPipelineResult;
      setRemoteResult(pre);
      if (!pre || pre.ok !== true) {
        setRemotePhase("done");
        return;
      }

      setRemoteStageIds(CONNECT_STAGE_IDS);
      setRemotePhase("running");
      setRemoteResult(null);
      const connRaw = await invoke<string>("khipu_db_connect", { dsn, rootCrt });
      const conn = parse(connRaw) as SetupPipelineResult;
      setRemoteResult(conn);
      if (conn?.ok === true) {
        await refreshDsn();
        await loadPlan();
        setRemoteReady(true);
      }
    } catch (e) {
      setRemoteResult({ ok: false, error: String(e) });
    } finally {
      setRemotePhase("done");
      setRemoteBusy(false);
    }
  }, [remoteDsn, remoteRootCrt, refreshDsn, loadPlan]);

  const pickRemoteRootCrt = useCallback(async () => {
    try {
      const selected = await openFileDialog({ multiple: false });
      if (typeof selected === "string" && selected.trim()) {
        const path = selected.trim();
        setRemoteRootCrt(path);
        await connectRemote(path);
      }
    } catch (e) {
      setDbMsg(String(e));
    }
  }, [connectRemote]);

  const completeJoinAfterImport = useCallback(
    async (
      summary: Record<string, unknown> | undefined,
      counts: Record<string, unknown> | undefined,
      warning?: string,
    ) => {
      setJoinExpected((summary?.expected as Record<string, number>) ?? null);
      if (counts?.live && typeof counts.live === "object") {
        setJoinLive(counts.live as Record<string, number>);
      }
      await refreshDsn();
      setJoinReady(true);
      setJoinedHub(true);
      const mismatches = Array.isArray(counts?.mismatches)
        ? (counts.mismatches as string[])
        : [];
      try {
        let raw = await invoke<string>("check_remote_postgres", { full: false });
        let v = parse(raw);
        let err = payloadError(v);
        if (err) {
          setDbMsg(
            warning
              ? `${warning}\n\nJoin kit is saved on this Mac — you can continue. The database is not reachable yet: ${err}`
              : `Join kit is saved on this Mac — you can continue. The database is not reachable yet: ${err}`,
          );
          return true;
        }
        await applyMigrations();
        raw = await invoke<string>("check_remote_postgres", { full: true });
        v = parse(raw);
        err = payloadError(v);
        if (err) {
          setDbMsg(
            `Join kit is saved on this Mac — you can continue. Database check: ${err}`,
          );
          return true;
        }
        raw = await invoke<string>("select_compat_row", {
          mode: "remote",
          pgvectorExtversion: String(v?.pgvector ?? ""),
          serverVersion: String(v?.server_version ?? ""),
          pgvector: String(v?.pgvector ?? ""),
        });
        const selErr = payloadError(parse(raw));
        await loadPlan();
        if (selErr) {
          setDbMsg(
            `Joined — the graph builder's version did not save (${selErr}). You can retry on the Graph step.`,
          );
        } else {
          setDbMsg(
            mismatches.length
              ? `Joined — counts differ from the join kit: ${mismatches.join("; ")}`
              : "Joined — the counts match the join kit exactly.",
          );
        }
        // The join import already stored the DSN itself; finish the rest of
        // the pipeline (nightly upkeep + a real capture-to-search round
        // trip) the same way a remote connect does — this never ran from the
        // join path before (audit gap #1).
        await finishStoredConnect();
      } catch (e) {
        setDbMsg(
          `Join kit is saved on this Mac — you can continue. The database check failed: ${String(e)}`,
        );
      }
      return true;
    },
    [refreshDsn, applyMigrations, loadPlan, finishStoredConnect],
  );

  const importJoinFromFile = useCallback(async () => {
    const passphrase = joinPassphrase.trim();
    setJoinBusy(true);
    setDbMsg(null);
    try {
      const selected = await openFileDialog({
        multiple: false,
        filters: [{ name: "Khipu join kit", extensions: ["khipujoin"] }],
      });
      if (typeof selected !== "string" || !selected.trim()) return;
      const raw = await invoke<string>("join_import", {
        passphrase,
        filePath: selected.trim(),
      });
      const out = parse(raw);
      if (out?.ok !== true && out?.kit_imported !== true) {
        setDbMsg(joinFailMessage(out, "Join import failed."));
        return;
      }
      await completeJoinAfterImport(
        out.summary as Record<string, unknown> | undefined,
        out.counts as Record<string, unknown> | undefined,
        typeof out.warning === "string" ? out.warning : undefined,
      );
    } catch (e) {
      setDbMsg(String(e));
    } finally {
      setJoinBusy(false);
    }
  }, [joinPassphrase, completeJoinAfterImport]);

  const receiveJoinNearby = useCallback(async () => {
    const passphrase = joinPassphrase.trim();
    const pin = joinPin.trim();
    if (!/^\d{6}$/.test(pin)) {
      setDbMsg("Enter the six-digit PIN from the other Mac.");
      return;
    }
    setJoinBusy(true);
    setDbMsg(null);
    try {
      const raw = await invoke<string>("join_receive", { passphrase, pin, outPath: null });
      const out = parse(raw);
      if (out?.ok !== true && out?.kit_imported !== true) {
        setDbMsg(joinFailMessage(out, "Nearby join failed."));
        return;
      }
      await completeJoinAfterImport(
        out.summary as Record<string, unknown> | undefined,
        out.counts as Record<string, unknown> | undefined,
        typeof out.warning === "string" ? out.warning : undefined,
      );
    } catch (e) {
      setDbMsg(String(e));
    } finally {
      setJoinBusy(false);
    }
  }, [joinPassphrase, joinPin, completeJoinAfterImport]);

  const addGraphCodeRootPath = useCallback(
    async (path: string) => {
      const trimmed = path.trim();
      if (!trimmed) return;
      setGraphSourcesMsg(null);
      try {
        const raw = await runKhipu(["sources", "add", `--root=${trimmed}`]);
        const out = parse(raw);
        if (out?.ok !== true) {
          setGraphSourcesMsg(String(out?.error ?? "Could not add folder."));
          return;
        }
        setNewCodeRoot("");
        setGraphSourcesMsg(`Added ${trimmed} — runs on the next graph build.`);
      } catch (e) {
        setGraphSourcesMsg(String(e));
      }
    },
    [runKhipu],
  );

  const pickGraphCodeRoot = useCallback(async () => {
    try {
      const selected = await openFileDialog({ directory: true, multiple: false });
      if (typeof selected === "string" && selected.trim()) {
        await addGraphCodeRootPath(selected);
      }
    } catch (e) {
      setGraphSourcesMsg(String(e));
    }
  }, [addGraphCodeRootPath]);

  const [presence, setPresence] = useState<Presence | null>(null);
  const [key, setKey] = useState("");
  const [keyMsg, setKeyMsg] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  type ModelChoice = "cloud" | "local" | "skip";
  const [synthChoice, setSynthChoice] = useState<ModelChoice>("cloud");
  const [embedChoice, setEmbedChoice] = useState<ModelChoice>("cloud");
  const [localSynthEndpoint, setLocalSynthEndpoint] = useState("http://127.0.0.1:11434/v1");
  const [localSynthModel, setLocalSynthModel] = useState("");
  const [localEmbedEndpoint, setLocalEmbedEndpoint] = useState("");
  const [localEmbedModel, setLocalEmbedModel] = useState("");
  const [openaiKey, setOpenaiKey] = useState("");
  const [modelMsg, setModelMsg] = useState<string | null>(null);
  const [modelSaving, setModelSaving] = useState(false);
  const loadPresence = useCallback(async () => {
    try {
      setPresence(parse(await invoke<string>("secrets_presence")) as Presence);
    } catch {
      setPresence(null);
    }
  }, []);
  useEffect(() => {
    if (step === "model") void loadPresence();
  }, [step, loadPresence]);

  const [modelVerify, setModelVerify] = useState<ModelVerifyResult | null>(null);
  const [verifying, setVerifying] = useState(false);
  /** "A model key is proven on save with one real call" — one cheap, real
   *  API call per configured provider (`khipu.modelcheck.check_model_keys`
   *  via the `khipu_secrets_verify` fixed-argv command), never the key
   *  itself. Runs right after a key save, and again on "Check again". */
  const verifyModelKeys = useCallback(async () => {
    setVerifying(true);
    try {
      const raw = await invoke<string>("khipu_secrets_verify");
      const out = parse(raw) as unknown as ModelVerifyResult | null;
      setModelVerify(out);
    } catch (e) {
      setModelVerify({
        ok: false,
        checks: [{ id: "error", ok: false, title: "Key check failed", detail: String(e), model: null, seconds: 0 }],
      });
    } finally {
      setVerifying(false);
    }
  }, []);

  const saveKey = useCallback(async () => {
    const value = key.trim();
    if (!value) return;
    setSaving(true);
    setKeyMsg(null);
    try {
      const out = parse(await invoke<string>("set_khipu_secret", { account: "gemini_api_key", value }));
      if (out?.ok) {
        setKey("");
        setKeyMsg("Saved to Keychain.");
        await loadPresence();
        await verifyModelKeys();
      } else {
        setKeyMsg(String(out?.error ?? "Could not save the key."));
      }
    } catch (e) {
      setKeyMsg(String(e));
    } finally {
      setSaving(false);
    }
  }, [key, loadPresence, verifyModelKeys]);

  const saveOpenaiKey = useCallback(async () => {
    const value = openaiKey.trim();
    if (!value) return;
    setSaving(true);
    setKeyMsg(null);
    try {
      const out = parse(await invoke<string>("set_khipu_secret", {
        account: "openai_compat_api_key",
        value,
      }));
      if (out?.ok) {
        setOpenaiKey("");
        setKeyMsg("OpenAI-compat key saved to Keychain.");
        await loadPresence();
        await verifyModelKeys();
      } else {
        setKeyMsg(String(out?.error ?? "Could not save the key."));
      }
    } catch (e) {
      setKeyMsg(String(e));
    } finally {
      setSaving(false);
    }
  }, [openaiKey, loadPresence]);

  const applyModelStep = useCallback(async (): Promise<boolean> => {
    setModelSaving(true);
    setModelMsg(null);
    try {
      if (synthChoice === "local" && (!localSynthEndpoint.trim() || !localSynthModel.trim())) {
        setModelMsg("Local synth needs a base URL and model id.");
        return false;
      }
      if (embedChoice === "local" && (!localEmbedEndpoint.trim() || !localEmbedModel.trim())) {
        setModelMsg("Local embed needs a base URL and model id.");
        return false;
      }
      const payload = {
        synth_choice: synthChoice,
        embed_choice: embedChoice,
        synth_endpoint: localSynthEndpoint.trim(),
        synth_model_id: localSynthModel.trim(),
        embed_endpoint: localEmbedEndpoint.trim(),
        embed_model_id: localEmbedModel.trim(),
      };
      const raw = await runKhipu(["models", "welcome", JSON.stringify(payload)]);
      const out = parse(raw);
      if (out?.ok !== true) {
        setModelMsg(String(out?.error ?? out?.models_error ?? "models welcome failed"));
        return false;
      }
      setModelMsg("Model preferences saved.");
      return true;
    } catch (e) {
      setModelMsg(String(e));
      return false;
    } finally {
      setModelSaving(false);
    }
  }, [
    synthChoice,
    embedChoice,
    localSynthEndpoint,
    localSynthModel,
    localEmbedEndpoint,
    localEmbedModel,
    runKhipu,
  ]);

  const [graphMsg, setGraphMsg] = useState<string | null>(null);
  const [graphErr, setGraphErr] = useState<string | null>(null);
  const [graphInstalling, setGraphInstalling] = useState(false);
  const [graphOk, setGraphOk] = useState<boolean | null>(null);
  // A Mac that can't reach the Graphify download (offline, blocked network,
  // no release for this arch) has no way to get graphOk true — mirrors the
  // model step's "skip for now" so setup isn't a dead end (audit 2026-08-31).
  const [graphSkipped, setGraphSkipped] = useState(false);
  const installGraphify = useCallback(async () => {
    setGraphInstalling(true);
    setGraphErr(null);
    setGraphMsg(null);
    try {
      const raw = await invoke<string>("install_graphify");
      const out = parse(raw);
      if (out?.ok) {
        setGraphOk(true);
        setGraphMsg(
          typeof out.semver === "string"
            ? `Graph builder ${out.semver} installed.`
            : "Graph builder installed.",
        );
      } else {
        setGraphOk(false);
        const title = typeof out?.title === "string" ? plainWords(out.title) : null;
        const detail = typeof out?.detail === "string" ? plainWords(out.detail) : null;
        const fix = typeof out?.fix === "string" ? plainWords(out.fix) : null;
        const words = [title, detail, fix].filter((s): s is string => Boolean(s)).join(" ");
        setGraphErr(words || plainWords(String(out?.error ?? raw)));
      }
    } catch (e) {
      setGraphOk(false);
      setGraphErr(plainWords(String(e)));
    } finally {
      setGraphInstalling(false);
    }
  }, []);
  useEffect(() => {
    if (step === "graph" && graphOk == null && !graphInstalling) {
      void installGraphify();
    }
  }, [step, graphOk, graphInstalling, installGraphify]);

  const [harnesses, setHarnesses] = useState<HarnessRow[] | null>(null);
  useEffect(() => {
    if (step !== "agents") return;
    void (async () => {
      try {
        const v = JSON.parse(await runKhipu(["integrations", "status"])) as unknown;
        const rows: HarnessRow[] = Array.isArray(v)
          ? (v as HarnessRow[])
          : Array.isArray((v as { harnesses?: unknown }).harnesses)
            ? (v as { harnesses: HarnessRow[] }).harnesses
            : Object.entries(v as Record<string, HarnessRow>).map(([harness, r]) => ({ ...r, harness }));
        setHarnesses(rows.filter((r) => r.harness !== "grok_bot"));
      } catch {
        setHarnesses([]);
      }
    })();
  }, [step, runKhipu]);

  const [doctor, setDoctor] = useState<DoctorPayload | null>(null);
  const [doctorErr, setDoctorErr] = useState<string | null>(null);
  const [doctorBusy, setDoctorBusy] = useState(false);
  const [probeSeconds, setProbeSeconds] = useState<number | null>(null);
  const [fixBusy, setFixBusy] = useState<string | null>(null);
  // Invalidates any in-flight `reloadDoctor()` run when the user leaves
  // Finish and comes back (or the component unmounts) — a stale response
  // landing after a fresh run started used to resurrect the previous
  // payload (doctor.ok / canFinish) as if nothing had changed.
  const doctorRunId = useRef(0);

  const reloadDoctor = useCallback(async () => {
    const myRun = ++doctorRunId.current;
    setDoctorErr(null);
    setDoctorBusy(true);
    try {
      let payload = parse(await runKhipu(["doctor"])) as DoctorPayload | null;
      if (doctorRunId.current !== myRun) return;
      // "Finish runs the app probe itself" (scope doc, "Finish, keys,
      // harnesses, Docker"): a red recall_probe_ok that no harness has
      // separately proven is not a wall — Finish earns the gate itself
      // rather than sending the user hunting for a Verify button first.
      if (payload && payload.recall_probe_ok === false && !anyHarnessVerified(payload.recall_probe)) {
        try {
          const probed = parse(
            await runKhipu(["doctor", "--probe", "--harness", "app"]),
          ) as DoctorPayload | null;
          if (doctorRunId.current !== myRun) return;
          if (probed) {
            payload = probed;
            const seconds = probed.recall_probe?.last_probe?.seconds;
            if (typeof seconds === "number") setProbeSeconds(seconds);
          }
        } catch {
          // Keep the plain doctor payload; its row still shows the fix.
        }
      }
      if (doctorRunId.current !== myRun) return;
      setDoctor(payload);
    } catch (e) {
      if (doctorRunId.current === myRun) setDoctorErr(String(e));
    } finally {
      if (doctorRunId.current === myRun) setDoctorBusy(false);
    }
  }, [runKhipu]);

  useEffect(() => {
    if (step !== "finish") return;
    // Invalidate any run from a previous visit and drop its payload
    // immediately — leaving Finish and returning used to keep doctor.ok /
    // canFinish true while a new `khipu doctor` ran.
    doctorRunId.current++;
    setDoctor(null);
    setDoctorErr(null);
    setProbeSeconds(null);
    void reloadDoctor();
  }, [step, reloadDoctor]);

  const attentionItems: Attention[] = doctor
    ? buildAttention(doctor as unknown as Record<string, unknown>).items
    : [];

  /** Finish's list of what "Continue anyway" is continuing past — a title
   *  per row, plus the health check's own state when it has not produced a
   *  row list at all yet. Never empty while anything is unproven, so the
   *  button's own label ("lists what is not yet proven") always has content
   *  to point at. */
  const notYetProven: string[] = doctorErr
    ? [`The health check could not run: ${doctorErr}`]
    : doctor == null
      ? ["The health check has not finished running yet."]
      : attentionItems.map((item) => item.title);

  const runFinishFix = useCallback(
    async (item: Attention) => {
      if (!item.fix || item.fix.kind === "revisions") return;
      setFixBusy(item.key);
      try {
        if (item.fix.kind === "reinstall-hook" && item.harness) {
          await runKhipu(["integrations", "install", item.harness]);
        } else if (item.fix.kind === "recall-probe") {
          await runKhipu(["doctor", "--probe"]);
        }
      } catch {
        // reloadDoctor below reports whatever state actually resulted.
      } finally {
        setFixBusy(null);
        await reloadDoctor();
      }
    },
    [runKhipu, reloadDoctor],
  );

  // "Finish" (the real, all-clear exit) still requires a green doctor.
  // "Continue anyway" never traps the user, so it is ALWAYS enabled — see
  // the render below and `notYetProven` for what it is continuing past.
  const canFinish = doctor?.ok === true;

  const finish = (withWarnings = false) => {
    if (!withWarnings && !canFinish) return;
    try {
      window.localStorage.setItem(WELCOME_DONE_KEY, "1");
    } catch {
      /* private mode: the tutorial simply reopens next launch */
    }
    onFinish();
  };

  const pending = plan?.pending?.length ?? 0;
  // A Mac that is already connected (Settings › Database opens this step
  // to change or inspect the connection) must never find Next disabled with
  // nothing to do: keeping the working connection is a complete answer.
  const alreadyConnected = dsnOk === true && !joinBusy && !dbBusy && !remoteBusy;
  const databaseReady =
    alreadyConnected ||
    (dbMode === "join"
      ? joinReady && dsnOk
      : dbMode === "local"
        ? localReady && dsnOk
        : remoteReady && dsnOk);

  const workingLabel =
    joinBusy
      ? "Importing join kit — reaching the database…"
      : dbBusy
        ? "Setting up the database…"
        : remoteBusy
          ? "Connecting to the database…"
          : migrating
            ? "Applying database schema…"
            : saving
              ? "Saving key…"
              : modelSaving
                ? "Saving models…"
                : graphInstalling
                  ? "Installing the graph builder…"
                  : doctorBusy && step === "finish"
                    ? "Running doctor…"
                    : null;

  return (
    <div className="onboard welcome" data-step={step}>
      <WorkingBanner label={workingLabel} />
      <ol className="welcome-steps" aria-label="Setup progress">
        {STEPS.map((s, i) => (
          <li key={s.id} className={i < idx ? "done" : i === idx ? "current" : ""} aria-current={i === idx ? "step" : undefined}>
            <button type="button" onClick={() => setStep(s.id)}>
              <span className="welcome-step-n">{i < idx ? <Check size={12} strokeWidth={2.5} aria-hidden /> : i + 1}</span>
              {s.label}
            </button>
          </li>
        ))}
      </ol>

      {step === "welcome" ? (
        <>
          <h1>Welcome to Khipu</h1>
          {fromDiskImage ? (
            <Callout tone="warn" title="Drag Khipu into Applications">
              You opened the app from the disk image. Drag <strong>Khipu.app</strong> into
              the <strong>Applications</strong> folder, eject the disk image, then launch
              Khipu from Applications — running it from the DMG is not an install.
            </Callout>
          ) : (
            <p className="muted">
              Install: open the disk image and drag <strong>Khipu.app</strong> into the{" "}
              <strong>Applications</strong> folder, then launch it from Applications
              (not from the disk image).
            </p>
          )}
          <p className="muted">
            Khipu gives your coding agents a memory that outlives the session.
            Each agent records what happened, the database keeps it as searchable
            text and a map of what connects to what, and the next session reads it
            back — from any of your Macs.
          </p>
          <p className="muted">
            Setup is six steps. Each one checks itself, so you can close this and
            come back; nothing here has to be finished in one go.
          </p>
          <ul className="welcome-list">
            <li><strong>Database</strong> — join a Khipu you already have, set one up on this Mac, or connect to one you run.</li>
            <li><strong>Model</strong> — cloud Gemini, a local model, or skip for now (recording waits, nothing is lost).</li>
            <li><strong>Graph</strong> — installs the graph builder next to the app.</li>
            <li><strong>Agents</strong> — one click per harness to wire in recording.</li>
            <li><strong>Finish</strong> — a health check, and where to get help.</li>
          </ul>
        </>
      ) : null}

      {step === "database" ? (
        <>
          <h1>Connect the database</h1>
          {alreadyConnected && !joinReady && !localReady && !remoteReady ? (
            <Callout tone="ok" title="This Mac is already connected">
              Keep the current connection and press Next, or pick an option below
              to connect somewhere else. Settings › Database › Move copies your
              memory to another database without losing anything.
            </Callout>
          ) : null}
          <p className="muted">
            Everything Khipu remembers lives in one database. Choose how you
            want to run it.
          </p>
          <div className="toolbar" role="radiogroup" aria-label="Database setup mode">
            <label className="mono">
              <input
                type="radio"
                name="db-mode"
                checked={dbMode === "join"}
                onChange={() => setDbMode("join")}
              />
              {" "}Join a Khipu I already have
            </label>
            <label className="mono">
              <input
                type="radio"
                name="db-mode"
                checked={dbMode === "local"}
                onChange={() => setDbMode("local")}
              />
              {" "}Set up a new database on this Mac (needs Docker)
            </label>
            <label className="mono">
              <input
                type="radio"
                name="db-mode"
                checked={dbMode === "remote"}
                onChange={() => setDbMode("remote")}
              />
              {" "}Connect to a database I already run
            </label>
          </div>

          {dbMode === "join" ? (
            <>
              <p className="muted">
                Use this on the <strong>new</strong> Mac. The working Mac: Settings →
                <strong> Set up another Mac</strong> → <strong>Save join kit…</strong>,
                then AirDrop the <code>.khipujoin</code> file here.
              </p>
              <ol className="welcome-list muted">
                <li>
                  Click <strong>Import join kit file…</strong> and pick that file.
                  A passphrase is optional — only if you typed one when saving.
                </li>
                <li>
                  Nearby PIN is optional (same Wi‑Fi). File import is enough to continue.
                </li>
              </ol>
              <div className="toolbar">
                <button
                  type="button"
                  className="primary"
                  disabled={joinBusy}
                  onClick={() => void importJoinFromFile()}
                >
                  {joinBusy ? "Importing…" : "Import join kit file…"}
                </button>
              </div>
              <p className="muted" style={{ marginTop: "0.75rem" }}>
                Optional passphrase (only if you set one when saving the file):
              </p>
              <div className="toolbar" style={{ width: "100%" }}>
                <input
                  className="mono"
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  value={joinPassphrase}
                  onChange={(e) => setJoinPassphrase(e.target.value)}
                  placeholder="Leave blank unless the file is locked"
                  aria-label="Optional join passphrase"
                />
              </div>
              <p className="muted" style={{ marginTop: "0.75rem" }}>
                Optional — nearby wireless (same Wi‑Fi):
              </p>
              <div className="toolbar" style={{ width: "100%" }}>
                <input
                  className="mono"
                  value={joinPin}
                  onChange={(e) => setJoinPin(e.target.value.replace(/\D/g, "").slice(0, 6))}
                  placeholder="6-digit PIN from the other Mac"
                  aria-label="Nearby join PIN"
                  maxLength={6}
                />
                <button
                  type="button"
                  disabled={joinBusy || joinPin.length !== 6}
                  onClick={() => void receiveJoinNearby()}
                >
                  {joinBusy ? "Connecting…" : "Find nearby Mac"}
                </button>
              </div>
              {joinExpected ? (
                <Callout tone="ok" title="What the other Mac has">
                  episodes {joinExpected.episodes ?? "?"} · topics {joinExpected.topics ?? "?"} ·
                  nodes {joinExpected.nodes ?? "?"}
                  {joinLive
                    ? ` — live: episodes ${joinLive.episodes ?? "?"}, topics ${joinLive.topics ?? "?"}, nodes ${joinLive.nodes ?? "?"}`
                    : null}
                </Callout>
              ) : null}
            </>
          ) : dbMode === "local" ? (
            <>
              <p className="muted">
                Install{" "}
                <a
                  href="https://docs.docker.com/desktop/setup/install/mac-install/"
                  target="_blank"
                  rel="noreferrer"
                  onClick={() => setDockerLinkClicked(true)}
                >
                  Docker Desktop
                </a>{" "}
                (or OrbStack / Colima with the <code>docker</code> CLI), start it — this page
                notices on its own once it's ready.
              </p>
              {dockerLinkClicked && dockerOk !== true ? (
                <p className="muted">
                  <Loader2 size={14} className="spin" aria-hidden /> Waiting for Docker
                  Desktop to finish installing…
                </p>
              ) : null}
              <div className="toolbar">
                <button type="button" onClick={() => void refreshDocker()}>Recheck now</button>
                <button
                  type="button"
                  className="primary"
                  disabled={dbBusy || dockerOk !== true}
                  onClick={() => void runLocalSetup()}
                >
                  {dbBusy ? "Setting up…" : "Set up the database on this Mac"}
                </button>
              </div>
              {dockerOk === false ? (
                <Callout tone="warn" title="Docker not ready">{dockerErr ?? "Start Docker Desktop, then recheck."}</Callout>
              ) : null}
              {diskWarn ? <Callout tone="warn" title="Low disk space">{diskWarn}</Callout> : null}
            </>
          ) : (
            <>
              <p className="muted">
                <strong>Connection string</strong> — looks like{" "}
                <code>postgresql://user:password@host:5432/dbname</code>; your
                host shows it in the database's connection details.
              </p>
              <div className="toolbar" style={{ width: "100%" }}>
                <input
                  className="mono"
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  value={remoteDsn}
                  onChange={(e) => setRemoteDsn(e.target.value)}
                  placeholder="postgresql://USER:PASSWORD@HOST:5432/DB?sslmode=verify-full"
                  aria-label="Connection string"
                />
              </div>
              <p className="muted" style={{ marginTop: "0.5rem" }}>
                <strong>Certificate file</strong> — only if your host gave you
                one (often called <code>root.crt</code> or <code>ca.pem</code>).
              </p>
              <div className="toolbar">
                <button type="button" onClick={() => void pickRemoteRootCrt()}>
                  {remoteRootCrt ? "Change certificate file…" : "Choose certificate file…"}
                </button>
                {remoteRootCrt ? (
                  <span className="muted mono ellip">{remoteRootCrt}</span>
                ) : null}
                <button
                  type="button"
                  className="primary"
                  disabled={remoteBusy || !remoteDsn.trim()}
                  onClick={() => void connectRemote()}
                >
                  {remoteBusy ? "Connecting…" : "Connect"}
                </button>
              </div>
              {remotePhase !== "idle" ? (
                <SetupStages
                  stageIds={remoteStageIds}
                  result={remoteResult}
                  phase={remotePhase}
                  busy={remoteBusy}
                  onRetry={() => void connectRemote()}
                  onPasteCertificate={() => void pickRemoteRootCrt()}
                />
              ) : null}
            </>
          )}

          {dbMode !== "remote" && dbMsg ? (
            /^[a-z]+(_[a-z]+)+$/.test(dbMsg.trim()) ? (
              <Callout tone="warn" title="Setup could not finish">
                Something went wrong at this step (details are in the health
                report). Try again; if it repeats, ask for help with the exact
                message from Home › Full health report.
              </Callout>
            ) : /^\{/.test(dbMsg.trim()) || /error|failed|denied|refused|timeout/i.test(dbMsg) ? (
              <Callout tone="warn" title="Setup could not finish">
                {dbMsg.replace(/\s+/g, " ").slice(0, 400)}
              </Callout>
            ) : (
              <Callout tone="neutral" title="Status">{dbMsg.replace(/\s+/g, " ").slice(0, 400)}</Callout>
            )
          ) : null}

          {dbMode !== "remote" && dsnOk && databaseReady ? (
            <Callout
              tone="ok"
              title="Database ready"
              action={pending > 0 ? (
                <button type="button" className="primary" disabled={migrating} onClick={() => void applyMigrations()}>
                  {migrating ? "Applying…" : "Apply schema"}
                </button>
              ) : undefined}
            >
              {planErr
                ? `Could not read the schema state: ${planErr}`
                : plan == null
                  ? "Checking the schema…"
                  : pending === 0
                    ? `Schema is up to date (${plan.applied?.length ?? 0} migrations applied).`
                    : `${pending} migration${pending === 1 ? "" : "s"} to apply: ${plan.pending?.join(", ")}`}
            </Callout>
          ) : null}

          {dbMode === "local" || dbMode === "join" ? (
            finishPhase !== "idle" ? (
              <SetupStages
                stageIds={CONNECT_STAGE_IDS.filter((id) => id === "store" || id === "upkeep" || id === "prove" || id === "summary")}
                result={finishStageResult}
                phase={finishPhase}
                busy={finishPhase === "running"}
                onRetry={() => void finishStoredConnect()}
              />
            ) : null
          ) : null}
        </>
      ) : null}

      {step === "model" ? (
        <>
          <h1>Give it a model</h1>
          <p className="muted">
            Session summaries and search by meaning can use cloud Gemini, a
            local model on this Mac, or nothing for now — recording waits until a
            key exists, and nothing is lost meanwhile.
          </p>
          <div className="section-card">
            <div className="section-head">Session summaries</div>
            <div className="section-body">
              <div className="toolbar" role="radiogroup" aria-label="Synth provider">
                <label className="mono">
                  <input
                    type="radio"
                    name="synth-choice"
                    checked={synthChoice === "cloud"}
                    onChange={() => setSynthChoice("cloud")}
                  />
                  {" "}Cloud Gemini
                </label>
                <label className="mono">
                  <input
                    type="radio"
                    name="synth-choice"
                    checked={synthChoice === "local"}
                    onChange={() => setSynthChoice("local")}
                  />
                  {" "}Local OpenAI-compat
                </label>
                <label className="mono">
                  <input
                    type="radio"
                    name="synth-choice"
                    checked={synthChoice === "skip"}
                    onChange={() => setSynthChoice("skip")}
                  />
                  {" "}Skip for now
                </label>
              </div>
              {synthChoice === "cloud" ? (
                <>
                  <p className="muted">
                    Paste a Gemini API key (optional now — capture queues without one).
                  </p>
                  <div className="toolbar" style={{ width: "100%" }}>
                    <input
                      className="mono"
                      type="password"
                      autoComplete="off"
                      spellCheck={false}
                      value={key}
                      onChange={(e) => setKey(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") void saveKey(); }}
                      placeholder={presence?.gemini_in_keychain ? "Key stored — paste to replace" : "AIza…"}
                      aria-label="Gemini API key"
                    />
                    <button type="button" className="primary" disabled={saving || !key.trim()} onClick={() => void saveKey()}>
                      {saving ? "Saving…" : "Save Gemini key"}
                    </button>
                    {(presence?.gemini_in_keychain || presence?.gemini_env) && !modelVerify ? (
                      <button type="button" disabled={verifying} onClick={() => void verifyModelKeys()}>
                        {verifying ? "Checking…" : "Check key now"}
                      </button>
                    ) : null}
                  </div>
                  <ModelCheckRow
                    check={modelCheckFor(modelVerify, "gemini_generate")}
                    verifying={verifying}
                    onRetry={() => void verifyModelKeys()}
                  />
                </>
              ) : null}
              {synthChoice === "local" ? (
                <>
                  <div className="toolbar" style={{ width: "100%" }}>
                    <input
                      className="mono"
                      value={localSynthEndpoint}
                      onChange={(e) => setLocalSynthEndpoint(e.target.value)}
                      placeholder="http://127.0.0.1:11434/v1"
                      aria-label="Local synth base URL"
                    />
                    <input
                      className="mono"
                      value={localSynthModel}
                      onChange={(e) => setLocalSynthModel(e.target.value)}
                      placeholder="model id"
                      aria-label="Local synth model id"
                    />
                  </div>
                  <div className="toolbar" style={{ width: "100%" }}>
                    <input
                      className="mono"
                      type="password"
                      autoComplete="off"
                      value={openaiKey}
                      onChange={(e) => setOpenaiKey(e.target.value)}
                      placeholder="Optional API key (Keychain)"
                      aria-label="OpenAI-compat API key"
                    />
                    <button type="button" disabled={saving || !openaiKey.trim()} onClick={() => void saveOpenaiKey()}>
                      Save compat key
                    </button>
                    {presence?.openai_compat_in_keychain && !modelVerify ? (
                      <button type="button" disabled={verifying} onClick={() => void verifyModelKeys()}>
                        {verifying ? "Checking…" : "Check key now"}
                      </button>
                    ) : null}
                  </div>
                  <ModelCheckRow
                    check={modelCheckFor(modelVerify, "openai_compat_generate")}
                    verifying={verifying}
                    onRetry={() => void verifyModelKeys()}
                  />
                </>
              ) : null}
            </div>
          </div>
          <div className="section-card">
            <div className="section-head">Search by meaning</div>
            <div className="section-body">
              <div className="toolbar" role="radiogroup" aria-label="Embed provider">
                <label className="mono">
                  <input
                    type="radio"
                    name="embed-choice"
                    checked={embedChoice === "cloud"}
                    onChange={() => setEmbedChoice("cloud")}
                  />
                  {" "}Gemini Embedding 2 @768
                </label>
                <label className="mono">
                  <input
                    type="radio"
                    name="embed-choice"
                    checked={embedChoice === "local"}
                    onChange={() => setEmbedChoice("local")}
                  />
                  {" "}Local (configure later)
                </label>
                <label className="mono">
                  <input
                    type="radio"
                    name="embed-choice"
                    checked={embedChoice === "skip"}
                    onChange={() => setEmbedChoice("skip")}
                  />
                  {" "}Skip — search empty until a profile is active
                </label>
              </div>
              {embedChoice === "local" ? (
                <div className="toolbar" style={{ width: "100%" }}>
                  <input
                    className="mono"
                    value={localEmbedEndpoint}
                    onChange={(e) => setLocalEmbedEndpoint(e.target.value)}
                    placeholder="https://…/v1"
                    aria-label="Local embed base URL"
                  />
                  <input
                    className="mono"
                    value={localEmbedModel}
                    onChange={(e) => setLocalEmbedModel(e.target.value)}
                    placeholder="embed model id"
                    aria-label="Local embed model id"
                  />
                </div>
              ) : null}
              {embedChoice === "cloud" && !presence?.gemini_in_keychain && !presence?.gemini_env ? (
                <Callout tone="warn" title="No Gemini key yet">
                  Semantic search stays empty until a key is set; synth capture can still queue.
                </Callout>
              ) : null}
              {embedChoice === "cloud" &&
              synthChoice !== "cloud" &&
              (presence?.gemini_in_keychain || presence?.gemini_env) &&
              !modelVerify ? (
                <div className="toolbar">
                  <button type="button" disabled={verifying} onClick={() => void verifyModelKeys()}>
                    {verifying ? "Checking…" : "Check key now"}
                  </button>
                  <span className="muted">A Gemini key is already stored — prove it with one real call.</span>
                </div>
              ) : null}
              <ModelCheckRow
                check={modelCheckFor(modelVerify, "gemini_embed")}
                verifying={verifying}
                onRetry={() => void verifyModelKeys()}
              />
            </div>
          </div>
          {keyMsg ? <pre className="code">{keyMsg}</pre> : null}
          {modelMsg ? <pre className="code">{modelMsg}</pre> : null}
        </>
      ) : null}

      {step === "graph" ? (
        <>
          <h1>Install the graph builder</h1>
          <p className="muted" title="Graphify">
            The graph builder works out how what you talked about connects — to
            files, topics and each other. It installs next to the app, not inside
            it, so it can be updated on its own.
          </p>
          {graphOk ? (
            <Callout tone="ok" title="The graph builder is ready">
              {graphMsg ?? "Installed."}
            </Callout>
          ) : graphInstalling ? (
            <p className="muted">Downloading and unpacking the graph builder…</p>
          ) : graphSkipped ? (
            <Callout
              tone="warn"
              title="Graph builder skipped"
              action={
                <button
                  type="button"
                  className="primary"
                  onClick={() => {
                    setGraphSkipped(false);
                    void installGraphify();
                  }}
                >
                  Retry
                </button>
              }
            >
              Recording and search still work. Connections stay empty until the
              graph builder is installed, which you can do later from Settings →
              Components.
            </Callout>
          ) : (
            <Callout
              tone="warn"
              title="The graph builder did not finish installing"
              action={
                <>
                  <button type="button" className="primary" onClick={() => void installGraphify()}>
                    Retry
                  </button>
                  <button type="button" onClick={() => setGraphSkipped(true)}>
                    Skip for now
                  </button>
                </>
              }
            >
              {graphErr ?? "It is not installed yet — retry, or skip it and install it later from Settings → Components."}
            </Callout>
          )}
          {joinedHub ? (
            <div className="section-card">
              <div className="section-head">Add folders on this Mac</div>
              <div className="section-body">
                <p className="muted">
                  Optional — add code folders on this Mac for the next graph build.
                  Skip it to read only (search and connections still work from the
                  shared database). The same repo in two places on one Mac makes
                  duplicate entries.
                </p>
                <div className="toolbar">
                  <input
                    className="mono"
                    value={newCodeRoot}
                    onChange={(e) => setNewCodeRoot(e.target.value)}
                    placeholder="/absolute/path/to/code"
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && newCodeRoot.trim()) {
                        void addGraphCodeRootPath(newCodeRoot);
                      }
                    }}
                  />
                  <button
                    type="button"
                    disabled={!newCodeRoot.trim()}
                    onClick={() => void addGraphCodeRootPath(newCodeRoot)}
                  >
                    Add folder
                  </button>
                  <button type="button" onClick={() => void pickGraphCodeRoot()}>
                    Choose folder…
                  </button>
                </div>
                {graphSourcesMsg ? <pre className="code">{graphSourcesMsg}</pre> : null}
              </div>
            </div>
          ) : null}
        </>
      ) : null}

      {step === "agents" ? (
        <>
          <h1>Connect your agents</h1>
          <p className="muted">
            Each agent gets recording and the memory tools. Install writes the
            settings; Verify proves they work by recording a throwaway session and
            finding it again.
          </p>
          {harnesses == null ? (
            <p className="muted">Looking for harnesses…</p>
          ) : harnesses.length === 0 ? (
            <p className="muted">No supported harness was found on this Mac yet.</p>
          ) : (
            <ul className="welcome-list welcome-harness-list">
              {harnesses.map((h) => (
                <li key={h.harness} className="welcome-harness-row">
                  <strong>{h.harness.replace("_", " ")}</strong>
                  {h.installed ?? (h.harness === "grok_bot" ? h.mcp : h.mcp && h.hook_stop && h.hook_precompact) ? (
                    <Tag tone="ok" dot>
                      {h.last_beat_at ? "Installed · recording" : "Installed"}
                    </Tag>
                  ) : (
                    <span className="muted">
                      {h.detected ? "found, not installed" : "not found on this Mac"}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
          <div className="toolbar">
            <button type="button" className="primary" onClick={openIntegrations}>
              Open Harnesses to install and verify
            </button>
          </div>
        </>
      ) : null}

      {step === "finish" ? (
        <>
          <h1>You're set</h1>
          {graphSkipped ? (
            <Callout tone="warn" title="Graph builder skipped">
              Recording and search work now. Install the graph builder later from
              Settings → Components to fill in the connections.
            </Callout>
          ) : null}
          {doctorErr ? (
            <p className="muted">The health check could not run: {doctorErr}</p>
          ) : doctor == null ? (
            <p className="muted">{doctorBusy ? "Running a health check…" : "Health check not run yet."}</p>
          ) : attentionItems.length === 0 ? (
            <Callout tone="ok" title="Everything checked out">
              {doctor.not_configured?.length ? (
                <>
                  Every check that applies to this Mac passed. Not checked here:{" "}
                  {doctor.not_configured.join(", ")} — normal on a fresh install.
                </>
              ) : (
                "Every check passed."
              )}
            </Callout>
          ) : (
            <>
              <Callout tone="warn" title={`${attentionItems.length} thing${attentionItems.length === 1 ? "" : "s"} still need attention`}>
                Each row below is one thing that is not proven yet, with the one action that fixes it.
              </Callout>
              {attentionItems.map((item) => (
                <Callout
                  key={item.key}
                  tone={item.tone}
                  title={item.title}
                  action={
                    item.fix && item.fix.kind !== "revisions" ? (
                      <button
                        type="button"
                        disabled={fixBusy != null}
                        onClick={() => void runFinishFix(item)}
                      >
                        {fixBusy === item.key ? "Working…" : item.fix.label}
                      </button>
                    ) : item.fix ? (
                      <span className="muted">{item.fix.label} — from Home, after you finish</span>
                    ) : (
                      <span className="muted">See Home for more, after you finish</span>
                    )
                  }
                >
                  {item.cause}
                </Callout>
              ))}
            </>
          )}
          {probeSeconds != null ? (
            <Callout tone="ok" title="Memory round trip proved">
              Memory round trip: {probeSeconds.toFixed(1)} s
            </Callout>
          ) : null}
          <p className="muted">
            From here: your agents record on their own, <strong>Recall</strong> is
            where you ask questions, and <strong>Home</strong> is the one place to
            look when something feels off.
          </p>
          <p className="muted">
            Need help? Email <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>.
            You can reopen this tutorial any time from Settings → Advanced → Run
            setup again.
          </p>
        </>
      ) : null}

      <div className="toolbar welcome-nav">
        <button type="button" onClick={() => go(-1)} disabled={idx === 0}>
          <ChevronLeft size={14} aria-hidden /> Back
        </button>
        {step === "finish" ? (
          <>
            {notYetProven.length ? (
              <span className="muted push">Not yet proven: {notYetProven.join("; ")}</span>
            ) : null}
            {/* Finish must never trap the user on this screen: unlike every
             *  other step's Next, this button carries no `disabled` at all —
             *  it is reachable from any doctor state (loading, errored, red,
             *  or green). */}
            <button type="button" onClick={() => finish(true)}>
              Continue anyway
            </button>
            <button type="button" className="primary" disabled={!canFinish} onClick={() => finish(false)}>
              Finish
            </button>
          </>
        ) : (
          <button
            type="button"
            className="primary"
            disabled={
              modelSaving ||
              (step === "database" && !databaseReady) ||
              (step === "graph" && graphOk !== true && !graphSkipped)
            }
            onClick={() => {
              if (step === "model") {
                void applyModelStep().then((ok) => {
                  if (ok) go(1);
                });
                return;
              }
              go(1);
            }}
          >
            {step === "welcome" ? "Start" : modelSaving && step === "model" ? "Saving…" : "Next"}{" "}
            <ChevronRight size={14} aria-hidden />
          </button>
        )}
      </div>
    </div>
  );
}
