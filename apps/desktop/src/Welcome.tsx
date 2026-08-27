import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { resourceDir } from "@tauri-apps/api/path";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  CircleCheck,
  TriangleAlert,
} from "lucide-react";

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
type DbMode = "local" | "remote";

const STEPS: { id: StepId; label: string }[] = [
  { id: "welcome", label: "Welcome" },
  { id: "database", label: "Database" },
  { id: "model", label: "Model" },
  { id: "graph", label: "Graph" },
  { id: "agents", label: "Agents" },
  { id: "finish", label: "Finish" },
];

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
};

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

function Note({ tone, title, children, action }: {
  tone: "ok" | "warn"; title: string; children?: React.ReactNode; action?: React.ReactNode;
}) {
  const Icon = tone === "ok" ? CircleCheck : TriangleAlert;
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

/** True when the bundle is on the shipped DMG volume, not /Applications or a
 *  checkout under `/Volumes/Cloud Storage`. Volume name is `Khipu` (see
 *  release_macos.sh DMG_VOLNAME). Duplicate mounts show up as `Khipu 1`. */
function launchedFromDiskImage(resourcePath: string): boolean {
  return /\/Volumes\/Khipu(?: \d+)?\//.test(resourcePath);
}

type DockerStatus = {
  ok?: boolean;
  error?: string;
  code?: string;
  app_installed?: boolean;
  action?: string;
  cli?: string | null;
};

function dockerNoteTitle(docker: DockerStatus): string {
  const code = docker.code ?? docker.action;
  if (code === "docker_not_found" || code === "need_install") {
    return "Docker Desktop isn’t installed";
  }
  if (code === "docker_dmg_opened") {
    return "Drag Docker into Applications";
  }
  if (code === "docker_daemon_stopped" || code === "docker_starting") {
    return "Starting Docker Desktop";
  }
  if (code === "docker_download_failed") {
    return "Couldn’t download Docker Desktop";
  }
  return "Docker isn’t ready";
}

function dockerNoteBody(docker: DockerStatus): string {
  const code = docker.code ?? "";
  if (code === "docker_not_found" || code === "need_install") {
    return "Khipu can download Docker Desktop and open it. First launch may ask for your password — that’s Docker’s installer, not us.";
  }
  if (code === "docker_dmg_opened") {
    return docker.error ?? "Open the Docker disk image, drag Docker.app into Applications, then recheck.";
  }
  if (code === "docker_daemon_stopped" || code === "docker_starting") {
    return "Docker Desktop is installed. Finish its first-launch prompts if they appear, then recheck.";
  }
  const err = docker.error ?? "";
  if (err && err !== "docker_not_found") return err;
  return "Start Docker Desktop, then recheck.";
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

  const [dbMode, setDbMode] = useState<DbMode>("local");
  const [dockerOk, setDockerOk] = useState<boolean | null>(null);
  const [dockerErr, setDockerErr] = useState<string | null>(null);
  const [dockerStatus, setDockerStatus] = useState<DockerStatus | null>(null);
  const [dockerBusy, setDockerBusy] = useState(false);
  const autoStartedDocker = useRef(false);
  const [diskWarn, setDiskWarn] = useState<string | null>(null);
  const [dbBusy, setDbBusy] = useState(false);
  const [dbMsg, setDbMsg] = useState<string | null>(null);
  const [localReady, setLocalReady] = useState(false);

  const [remoteDsn, setRemoteDsn] = useState("");
  const [remoteBusy, setRemoteBusy] = useState(false);
  const [remoteReady, setRemoteReady] = useState(false);

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

  const refreshDocker = useCallback(async (opts?: { startIfInstalled?: boolean }) => {
    setDockerErr(null);
    try {
      const raw = await invoke<string>("components_status");
      const v = parse(raw);
      let docker = (v?.docker ?? null) as DockerStatus | null;
      if (v && v.ok === false && v.error && !docker) {
        docker = { ok: false, error: String(v.error), code: "status_failed" };
      }
      setDockerStatus(docker);
      setDockerOk(Boolean(docker?.ok));
      if (!docker?.ok) setDockerErr(String(docker?.error ?? "Docker is not running"));
      if (
        opts?.startIfInstalled &&
        docker &&
        !docker.ok &&
        docker.app_installed &&
        !autoStartedDocker.current
      ) {
        autoStartedDocker.current = true;
        setDockerBusy(true);
        try {
          const ensuredRaw = await invoke<string>("ensure_docker", { install: false });
          const ensured = parse(ensuredRaw) as DockerStatus | null;
          if (ensured) {
            setDockerStatus(ensured);
            setDockerOk(Boolean(ensured.ok));
            if (!ensured.ok) setDockerErr(String(ensured.error ?? "Docker is not running"));
          }
        } finally {
          setDockerBusy(false);
        }
      }
    } catch (e) {
      setDockerOk(false);
      setDockerErr(String(e));
      setDockerStatus({ ok: false, error: String(e), code: "status_failed" });
    }
  }, []);

  const setupDocker = useCallback(async () => {
    setDockerBusy(true);
    setDockerErr(null);
    setDbMsg(null);
    try {
      const install = !dockerStatus?.app_installed;
      const raw = await invoke<string>("ensure_docker", { install });
      const ensured = parse(raw) as DockerStatus | null;
      if (ensured) {
        setDockerStatus(ensured);
        setDockerOk(Boolean(ensured.ok));
        if (!ensured.ok) setDockerErr(String(ensured.error ?? "Docker is not running"));
      }
    } catch (e) {
      setDockerOk(false);
      setDockerErr(String(e));
      setDockerStatus({ ok: false, error: String(e), code: "status_failed" });
    } finally {
      setDockerBusy(false);
    }
  }, [dockerStatus?.app_installed]);

  useEffect(() => {
    if (step === "database" && dbMode === "local") {
      void refreshDocker({ startIfInstalled: true });
    }
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
      let docker = (status?.docker ?? null) as DockerStatus | null;
      setDockerStatus(docker);
      setDockerOk(Boolean(docker?.ok));
      if (!docker?.ok) {
        setDockerBusy(true);
        setDbMsg(
          docker?.app_installed
            ? "Starting Docker Desktop…"
            : "Downloading Docker Desktop (~600 MB) and opening it…",
        );
        try {
          const ensuredRaw = await invoke<string>("ensure_docker", { install: true });
          docker = parse(ensuredRaw) as DockerStatus | null;
          setDockerStatus(docker);
          setDockerOk(Boolean(docker?.ok));
          if (!docker?.ok) {
            setDockerErr(String(docker?.error ?? "Docker is not running"));
            setDbMsg(dockerNoteBody(docker ?? { ok: false }));
            return;
          }
        } finally {
          setDockerBusy(false);
        }
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
          `Free disk is about ${disk.free_gib ?? "?"} GiB — Postgres needs headroom. You can continue after freeing space.`,
        );
      }
      raw = await invoke<string>("bootstrap_local_backup");
      v = parse(raw);
      err = payloadError(v);
      if (err) {
        setDbMsg(`Backup drill failed: ${err}`);
        return;
      }
      await refreshDsn();
      await loadPlan();
      setLocalReady(true);
      setDbMsg("Local PostgreSQL 19 is running and the backup drill passed.");
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
        setDbMsg(String(out?.error ?? "Could not save the DSN."));
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
      setDbMsg("Remote PostgreSQL 19 connected and compatible.");
    } catch (e) {
      setDbMsg(String(e));
    } finally {
      setRemoteBusy(false);
    }
  }, [remoteDsn, refreshDsn, applyMigrations, loadPlan]);

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
      const synth =
        synthChoice === "skip"
          ? { provider: "cloud", endpoint: "", model_id: "" }
          : synthChoice === "local"
            ? {
                provider: "local",
                endpoint: localSynthEndpoint.trim(),
                model_id: localSynthModel.trim(),
              }
            : { provider: "cloud", endpoint: "", model_id: "gemini-2.5-flash" };
      const embed =
        embedChoice === "skip"
          ? { provider: "cloud", endpoint: "", model_id: "" }
          : embedChoice === "local"
            ? {
                provider: "local",
                endpoint: localEmbedEndpoint.trim(),
                model_id: localEmbedModel.trim(),
              }
            : { provider: "cloud", endpoint: "", model_id: "gemini-embedding-2" };
      const payload = {
        synth,
        embed,
        vision: { provider: "off", endpoint: "", model_id: "" },
      };
      const raw = await runKhipu(["models", "set", JSON.stringify(payload)]);
      const out = parse(raw);
      if (out?.ok !== true) {
        setModelMsg(String(out?.error ?? out?.models_error ?? "models set failed"));
        return false;
      }
      if (embedChoice === "cloud") {
        const act = parse(await runKhipu(["embed", "activate", "gemini-embedding-2@768", "--force"]));
        if (act?.ok !== true && act?.error) {
          setModelMsg(String(act.error));
          return false;
        }
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
            ? `Graphify ${out.semver} installed.`
            : "Graph engine installed.",
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
  const reloadDoctor = useCallback(async () => {
    setDoctorErr(null);
    try {
      setDoctor(parse(await runKhipu(["doctor"])) as DoctorPayload);
    } catch (e) {
      setDoctorErr(String(e));
    }
  }, [runKhipu]);

  useEffect(() => {
    if (step !== "finish") return;
    void reloadDoctor();
  }, [step, reloadDoctor]);

  const soleBackupRed = soleBackupRedFlag(doctor);

  const canFinish =
    doctor?.ok === true ||
    (dbMode === "remote" && soleBackupRed);

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
  const databaseReady = dbMode === "local" ? localReady && dsnOk : remoteReady && dsnOk;

  return (
    <div className="onboard welcome" data-step={step}>
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
            <Note tone="warn" title="Drag Khipu into Applications">
              You opened the app from the disk image. Drag <strong>Khipu.app</strong> into
              the <strong>Applications</strong> folder, eject the disk image, then launch
              Khipu from Applications — running it from the DMG is not an install.
            </Note>
          ) : (
            <p className="muted">
              Install: open the disk image and drag <strong>Khipu.app</strong> into the{" "}
              <strong>Applications</strong> folder, then launch it from Applications
              (not from the disk image).
            </p>
          )}
          <p className="muted">
            Khipu gives your coding agents a memory that outlives the session. A
            hook in each agent captures what happened; PostgreSQL 19 stores it as
            searchable prose plus a property graph; the next session searches it
            — from any of your Macs.
          </p>
          <p className="muted">
            Setup is six steps. Each one checks itself, so you can close this and
            come back; nothing here has to be finished in one go.
          </p>
          <ul className="welcome-list">
            <li><strong>Database</strong> — local PostgreSQL 19 or connect an existing server.</li>
            <li><strong>Model</strong> — cloud Gemini, local OpenAI-compat, or skip (capture queues).</li>
            <li><strong>Graph</strong> — installs the Graphify engine under Application Support.</li>
            <li><strong>Agents</strong> — one click per harness to wire the hooks in.</li>
            <li><strong>Finish</strong> — a health check, and where to get help.</li>
          </ul>
        </>
      ) : null}

      {step === "database" ? (
        <>
          <h1>Connect the database</h1>
          <p className="muted">
            Khipu needs PostgreSQL 19 with pgvector and SQL/PGQ property graphs.
            Choose how you want to run it on this Mac.
          </p>
          <div className="toolbar" role="radiogroup" aria-label="Database setup mode">
            <label className="mono">
              <input
                type="radio"
                name="db-mode"
                checked={dbMode === "local"}
                onChange={() => setDbMode("local")}
              />
              {" "}Start a local Postgres 19 (Docker Desktop)
            </label>
            <label className="mono">
              <input
                type="radio"
                name="db-mode"
                checked={dbMode === "remote"}
                onChange={() => setDbMode("remote")}
              />
              {" "}I already have PostgreSQL 19
            </label>
          </div>

          {dbMode === "local" ? (
            <>
              <p className="muted">
                Local Postgres runs in Docker. If Docker Desktop isn’t on this Mac,
                Khipu downloads it from Docker and opens it — first launch may ask
                for your password.
              </p>
              <div className="toolbar">
                <button
                  type="button"
                  className="primary"
                  disabled={dockerBusy || dbBusy || dockerOk === true}
                  onClick={() => void setupDocker()}
                >
                  {dockerBusy
                    ? "Setting up Docker…"
                    : dockerStatus?.app_installed
                      ? "Start Docker Desktop"
                      : "Install Docker Desktop"}
                </button>
                <button type="button" disabled={dockerBusy} onClick={() => void refreshDocker()}>Recheck Docker</button>
                <button type="button" className="primary" disabled={dbBusy || dockerBusy} onClick={() => void runLocalSetup()}>
                  {dbBusy ? "Setting up…" : "Install local Postgres 19"}
                </button>
              </div>
              {dockerOk === false && dockerStatus ? (
                <Note tone="warn" title={dockerNoteTitle(dockerStatus)}>{dockerNoteBody(dockerStatus)}</Note>
              ) : dockerOk === false && dockerErr ? (
                <Note tone="warn" title="Docker isn’t ready">{dockerErr}</Note>
              ) : null}
              {diskWarn ? <Note tone="warn" title="Low disk space">{diskWarn}</Note> : null}
            </>
          ) : (
            <>
              <p className="muted">
                Paste a PostgreSQL 19 connection string. The password is stored in the
                login Keychain via <code>set_khipu_secret</code>, never in a config file.
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
            <Note
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
            </Note>
          ) : null}
        </>
      ) : null}

      {step === "model" ? (
        <>
          <h1>Give it a model</h1>
          <p className="muted">
            Session summaries (synth) and semantic search (embed) can use cloud
            Gemini, a local OpenAI-compatible server, or be skipped — capture
            queues until credentials exist; nothing is lost.
          </p>
          <div className="section-card">
            <div className="section-head">Summaries (synth)</div>
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
            <div className="section-head">Embeddings (search)</div>
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
                <Note tone="warn" title="No Gemini key yet">
                  Semantic search stays empty until a key is set; synth capture can still queue.
                </Note>
              ) : null}
            </div>
          </div>
          {keyMsg ? <pre className="code">{keyMsg}</pre> : null}
          {modelMsg ? <pre className="code">{modelMsg}</pre> : null}
        </>
      ) : null}

      {step === "graph" ? (
        <>
          <h1>Install the graph engine</h1>
          <p className="muted">
            Graphify builds the knowledge graph from folders you choose later.
            It installs as a separate, upgradable component under Application
            Support — not inside the app bundle.
          </p>
          {graphOk ? (
            <Note tone="ok" title="Graph engine ready">
              {graphMsg ?? "Graphify is installed."}
            </Note>
          ) : graphInstalling ? (
            <p className="muted">Downloading and unpacking Graphify…</p>
          ) : (
            <Note
              tone="warn"
              title="Graph install did not finish"
              action={
                <button type="button" className="primary" onClick={() => void installGraphify()}>
                  Retry
                </button>
              }
            >
              {graphErr ?? "Complete the Database step first so pending Graphify version is set."}
            </Note>
          )}
        </>
      ) : null}

      {step === "agents" ? (
        <>
          <h1>Connect your agents</h1>
          <p className="muted">
            Each harness gets a capture hook and the Khipu MCP tools. Install
            writes the config; verify proves it works by exercising it.
          </p>
          {harnesses == null ? (
            <p className="muted">Looking for harnesses…</p>
          ) : harnesses.length === 0 ? (
            <p className="muted">No supported harness was found on this Mac yet.</p>
          ) : (
            <ul className="welcome-list">
              {harnesses.map((h) => (
                <li key={h.harness}>
                  <strong>{h.harness.replace("_", " ")}</strong>
                  {" — "}
                  {h.installed ? "installed" : h.detected ? "found, not installed" : "not found on this Mac"}
                </li>
              ))}
            </ul>
          )}
          <div className="toolbar">
            <button type="button" className="primary" onClick={openIntegrations}>
              Open Integrations to install and verify
            </button>
          </div>
        </>
      ) : null}

      {step === "finish" ? (
        <>
          <h1>You're set</h1>
          {doctorErr ? (
            <p className="muted">Doctor could not run: {doctorErr}</p>
          ) : doctor == null ? (
            <p className="muted">Running a health check…</p>
          ) : (
            <Note tone={doctor.ok ? "ok" : "warn"} title={doctor.ok ? "Doctor is green" : "Doctor found something"}>
              {doctor.not_configured?.length
                ? <>Skipped (not configured on this Mac): <code>{doctor.not_configured.join(", ")}</code>. Expected on a fresh install.</>
                : "Every configured check ran."}
              {!doctor.ok ? " See the Doctor pane for details." : null}
              {soleBackupRed ? (
                <div className="callout-body">
                  Only <code>backup_ok</code> is red — server-operator backups are not recorded yet.
                  You may continue with warnings on a remote database.
                </div>
              ) : null}
            </Note>
          )}
          <p className="muted">
            From here: agents capture on their own, and <strong>Search</strong>{" "}
            is where you ask questions. <strong>Doctor</strong> is the one place
            to look when something feels off.
          </p>
          <p className="muted">
            Need help? Email <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>.
            You can reopen this tutorial any time from Setup → Welcome.
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
              <button type="button" onClick={() => finish(true)}>Continue with warnings</button>
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
              (step === "graph" && graphOk !== true)
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
