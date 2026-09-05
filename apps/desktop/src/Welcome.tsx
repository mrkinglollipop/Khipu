import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { open as openFileDialog } from "@tauri-apps/plugin-dialog";
import { resourceDir } from "@tauri-apps/api/path";
import { Check, ChevronLeft, ChevronRight } from "lucide-react";
import { WorkingBanner } from "./WorkingBanner";
import { Callout, Tag } from "./ui";

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
type Presence = { gemini_in_keychain?: boolean; gemini_env?: boolean };
type HarnessRow = { harness: string; detected?: boolean; installed?: boolean };
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
  bundle_seal_ok?: boolean;
  hub_snapshot?: { ok?: boolean };
};

/** One plain sentence per check that can be red, so the last step of setup
 *  never hands a first-run user a field name (`capture_liveness_ok`) and a
 *  pane that no longer exists. Anything not named here is reported by its own
 *  key rather than silently dropped. */
const CHECK_IN_WORDS: Record<string, string> = {
  backup_ok: "No recent backup of the database has been recorded yet.",
  drift_ok: "Some note files no longer match the database.",
  graph_drift_ok: "The connections index is behind its source.",
  outbox_ok: "Some recorded sessions are still waiting to reach the database.",
  capture_liveness_ok: "A harness has stopped recording sessions.",
  git_sync_ok: "The nightly off-site copy of your notes is not landing.",
  dsn_file_ok: "The saved database connection cannot be read.",
  index_freshness_ok: "The index an agent reads first has not been rebuilt today.",
  embed_coverage_ok: "Some sessions are not in the search index yet.",
  graph_backup_ok: "The saved copy of the connections index is stale.",
  graph_offsite_ok: "The off-site copy of the connections index is stale.",
  recall_probe_ok:
    "Nothing has proved end to end that a session can be recorded and found again.",
  bundle_seal_ok: "This copy of Khipu has been altered since it was signed.",
};

function redChecks(doctor: DoctorPayload | null): string[] {
  if (!doctor) return [];
  const out: string[] = [];
  for (const [key, value] of Object.entries(doctor)) {
    if (!key.endsWith("_ok") || value !== false) continue;
    out.push(CHECK_IN_WORDS[key] ?? key.replace(/_ok$/, "").replace(/_/g, " "));
  }
  if (doctor.hub_snapshot?.ok === false) {
    out.push("The offline copy of your memory is behind.");
  }
  return out;
}

function soleBackupRedFlag(doctor: DoctorPayload | null): boolean {
  if (!doctor || doctor.ok) return false;
  if (doctor.backup_ok !== false) return false;
  for (const key of [
    "drift_ok",
    "graph_drift_ok",
    "outbox_ok",
    "capture_liveness_ok",
    "git_sync_ok",
    "dsn_file_ok",
    "index_freshness_ok",
    "embed_coverage_ok",
  ] as const) {
    if (doctor[key] === false) return false;
  }
  return true;
}

/** True when the bundle is on the shipped DMG volume, not /Applications or a
 *  checkout under `/Volumes/Cloud Storage`. Volume name is `Khipu` (see
 *  release_macos.sh DMG_VOLNAME). Duplicate mounts show up as `Khipu 1`. */
function launchedFromDiskImage(resourcePath: string): boolean {
  return /\/Volumes\/Khipu(?: \d+)?\//.test(resourcePath);
}

function payloadError(v: Record<string, unknown> | null): string | null {
  if (!v) return "Invalid response";
  if (v.ok === false) return String(v.error ?? "Request failed");
  return null;
}

export function Welcome({ dsnOk, refreshDsn, runKhipu, onFinish, openIntegrations }: {
  dsnOk: boolean | null;
  refreshDsn: () => Promise<void>;
  runKhipu: (args: string[]) => Promise<string>;
  onFinish: () => void;
  openIntegrations: () => void;
}) {
  const [step, setStep] = useState<StepId>("welcome");
  const idx = STEPS.findIndex((s) => s.id === step);
  const go = (n: number) => setStep(STEPS[Math.max(0, Math.min(STEPS.length - 1, idx + n))].id);

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
  const [remoteBusy, setRemoteBusy] = useState(false);
  const [remoteReady, setRemoteReady] = useState(false);

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
    } catch (e) {
      setDbMsg(String(e));
    } finally {
      setDbBusy(false);
    }
  }, [refreshDsn, loadPlan]);

  const saveRemoteDsn = useCallback(async () => {
    const value = remoteDsn.trim();
    if (!value) return;
    setRemoteBusy(true);
    setDbMsg(null);
    setRemoteReady(false);
    try {
      const out = parse(await invoke<string>("set_khipu_secret", { account: "database_url", value }));
      if (out?.ok !== true) {
        setDbMsg(String(out?.error ?? "Could not save the connection."));
        return;
      }
      await refreshDsn();
      let raw = await invoke<string>("check_remote_postgres", { full: false });
      let v = parse(raw);
      let err = payloadError(v);
      if (err) {
        setDbMsg(err);
        return;
      }
      await applyMigrations();
      raw = await invoke<string>("check_remote_postgres", { full: true });
      v = parse(raw);
      err = payloadError(v);
      if (err) {
        setDbMsg(err);
        return;
      }
      raw = await invoke<string>("select_compat_row", {
        mode: "remote",
        pgvectorExtversion: String(v?.pgvector ?? ""),
        serverVersion: String(v?.server_version ?? ""),
        pgvector: String(v?.pgvector ?? ""),
      });
      v = parse(raw);
      err = payloadError(v);
      if (err) {
        setDbMsg(err);
        return;
      }
      await loadPlan();
      setRemoteReady(true);
      setDbMsg("Connected. That server has everything Khipu needs.");
    } catch (e) {
      setDbMsg(String(e));
    } finally {
      setRemoteBusy(false);
    }
  }, [remoteDsn, refreshDsn, applyMigrations, loadPlan]);

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
      } catch (e) {
        setDbMsg(
          `Join kit is saved on this Mac — you can continue. The database check failed: ${String(e)}`,
        );
      }
      return true;
    },
    [refreshDsn, applyMigrations, loadPlan],
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
      } else {
        setKeyMsg(String(out?.error ?? "Could not save the key."));
      }
    } catch (e) {
      setKeyMsg(String(e));
    } finally {
      setSaving(false);
    }
  }, [key, loadPresence]);

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
        setGraphErr(String(out?.error ?? raw));
      }
    } catch (e) {
      setGraphOk(false);
      setGraphErr(String(e));
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
  useEffect(() => {
    if (step !== "finish") return;
    let alive = true;
    // Drop the last payload immediately. Leaving Finish and returning used to
    // keep doctor.ok / "Doctor is green" / canFinish true while a new
    // `khipu doctor` ran (first visit is already doctor == null).
    setDoctor(null);
    setDoctorErr(null);
    setDoctorBusy(true);
    void (async () => {
      try {
        const payload = parse(await runKhipu(["doctor"])) as DoctorPayload;
        if (!alive) return;
        setDoctor(payload);
      } catch (e) {
        if (!alive) return;
        setDoctorErr(String(e));
      } finally {
        if (alive) setDoctorBusy(false);
      }
    })();
    return () => {
      alive = false;
      setDoctor(null);
      setDoctorErr(null);
      setDoctorBusy(false);
    };
  }, [step, runKhipu]);

  const soleBackupRed = soleBackupRedFlag(doctor);

  const canFinish =
    !doctorBusy &&
    (doctor?.ok === true ||
      ((dbMode === "remote" || dbMode === "join") && soleBackupRed));

  const finish = (withWarnings = false) => {
    if (!canFinish) return;
    if (withWarnings && !soleBackupRed) return;
    try {
      window.localStorage.setItem(WELCOME_DONE_KEY, "1");
    } catch {
      /* private mode: the tutorial simply reopens next launch */
    }
    onFinish();
  };

  const pending = plan?.pending?.length ?? 0;
  const databaseReady =
    dbMode === "join"
      ? joinReady && dsnOk
      : dbMode === "local"
        ? localReady && dsnOk
        : remoteReady && dsnOk;

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
                <a href="https://docs.docker.com/desktop/setup/install/mac-install/" target="_blank" rel="noreferrer">
                  Docker Desktop
                </a>{" "}
                (or OrbStack / Colima with the <code>docker</code> CLI), start it, then recheck.
              </p>
              <div className="toolbar">
                <button type="button" onClick={() => void refreshDocker()}>Recheck Docker</button>
                <button type="button" className="primary" disabled={dbBusy} onClick={() => void runLocalSetup()}>
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
                Paste the connection string for your server. The password goes
                straight into the login Keychain — never into a file in the repo.
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
                  aria-label="Database connection string"
                />
                <button
                  type="button"
                  className="primary"
                  disabled={remoteBusy || !remoteDsn.trim()}
                  onClick={() => void saveRemoteDsn()}
                >
                  {remoteBusy ? "Connecting…" : "Save and verify"}
                </button>
              </div>
            </>
          )}

          {dbMsg ? <pre className="code">{dbMsg}</pre> : null}

          {dsnOk && databaseReady ? (
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
                  </div>
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
                  </div>
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
                  {h.installed ? (
                    <Tag tone="ok" dot>
                      Installed
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
            <p className="muted">Running a health check…</p>
          ) : (
            <Callout
              tone={doctor.ok ? "ok" : "warn"}
              title={
                doctor.ok
                  ? "Everything checked out"
                  : "One thing still needs attention"
              }
              action={
                doctor.ok ? undefined : (
                  <button type="button" onClick={onFinish}>
                    Open Home
                  </button>
                )
              }
            >
              {doctor.ok ? (
                doctor.not_configured?.length ? (
                  <>
                    Every check that applies to this Mac passed. Not checked here:{" "}
                    {doctor.not_configured.join(", ")} — normal on a fresh install.
                  </>
                ) : (
                  "Every check passed."
                )
              ) : (
                <>
                  {redChecks(doctor).join(" ")}
                  {" Home shows each one with the single action that fixes it."}
                  {soleBackupRed
                    ? " This is the only red one, and on a database someone else runs it is expected — you can finish anyway."
                    : null}
                </>
              )}
            </Callout>
          )}
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
            {soleBackupRed ? (
              <button type="button" disabled={doctorBusy} onClick={() => finish(true)}>Continue with warnings</button>
            ) : null}
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
