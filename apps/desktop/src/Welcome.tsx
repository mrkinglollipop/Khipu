import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
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

type StepId = "welcome" | "database" | "model" | "agents" | "finish";

const STEPS: { id: StepId; label: string }[] = [
  { id: "welcome", label: "Welcome" },
  { id: "database", label: "Database" },
  { id: "model", label: "Model" },
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

  // ---- database: connection + schema ----
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
  useEffect(() => {
    if (step === "database" && dsnOk) void loadPlan();
  }, [step, dsnOk, loadPlan]);

  // ---- model: Gemini key ----
  const [presence, setPresence] = useState<Presence | null>(null);
  const [key, setKey] = useState("");
  const [keyMsg, setKeyMsg] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
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
      // Same stdin path as Settings → Secrets: the key never appears in argv.
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

  // ---- agents ----
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

  // ---- finish: doctor ----
  const [doctor, setDoctor] = useState<{ ok?: boolean; not_configured?: string[] } | null>(null);
  const [doctorErr, setDoctorErr] = useState<string | null>(null);
  useEffect(() => {
    if (step !== "finish") return;
    void (async () => {
      try {
        setDoctor(parse(await runKhipu(["doctor"])) as { ok?: boolean; not_configured?: string[] });
      } catch (e) {
        setDoctorErr(String(e));
      }
    })();
  }, [step, runKhipu]);

  const finish = () => {
    try {
      window.localStorage.setItem(WELCOME_DONE_KEY, "1");
    } catch {
      /* private mode: the tutorial simply reopens next launch */
    }
    onFinish();
  };

  const pending = plan?.pending?.length ?? 0;

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
          <p className="muted">
            Khipu gives your coding agents a memory that outlives the session. A
            hook in each agent captures what happened; PostgreSQL stores it as
            searchable prose plus a knowledge graph; the next session searches it
            — from any of your Macs.
          </p>
          <p className="muted">
            Setup is four short steps. Each one checks itself, so you can close
            this and come back; nothing here has to be finished in one go.
          </p>
          <ul className="welcome-list">
            <li><strong>Database</strong> — where memory lives (PostgreSQL 19 + pgvector).</li>
            <li><strong>Model</strong> — the Gemini key that writes summaries and embeddings.</li>
            <li><strong>Agents</strong> — one click per harness to wire the hooks in.</li>
            <li><strong>Finish</strong> — a health check, and where to get help.</li>
          </ul>
        </>
      ) : null}

      {step === "database" ? (
        <>
          <h1>Connect the database</h1>
          <p className="muted">
            Khipu needs a PostgreSQL 19 server with pgvector that only your
            machines can reach. Store its connection string in the login Keychain
            — the password never touches a config file:
          </p>
          <pre className="code">{`printf '%s' 'postgresql://USER:PASSWORD@HOST:5432/DB?sslmode=verify-full' | khipu secrets --set database_url`}</pre>
          <div className="toolbar">
            <button type="button" onClick={() => void refreshDsn()}>Recheck connection</button>
          </div>
          {dsnOk ? (
            <Note
              tone="ok"
              title="Connected"
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
          ) : (
            <Note tone="warn" title="Not connected yet">Run the command above, then recheck.</Note>
          )}
        </>
      ) : null}

      {step === "model" ? (
        <>
          <h1>Give it a model</h1>
          <p className="muted">
            Session summaries and semantic search use Gemini. Paste an API key
            here — it goes straight to the login Keychain, never to a file or the
            command line. An environment variable <code>GEMINI_API_KEY</code>{" "}
            takes precedence if you set one.
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
              placeholder={presence?.gemini_in_keychain ? "A key is stored — paste a new one to replace it" : "AIza…"}
              aria-label="Gemini API key"
            />
            <button type="button" className="primary" disabled={saving || !key.trim()} onClick={() => void saveKey()}>
              {saving ? "Saving…" : "Save key"}
            </button>
          </div>
          {keyMsg ? <pre className="code">{keyMsg}</pre> : null}
          {presence?.gemini_in_keychain || presence?.gemini_env ? (
            <Note tone="ok" title={presence.gemini_env ? "Key found in the environment" : "Key stored in the Keychain"} />
          ) : (
            <Note tone="warn" title="No key yet">Capture will queue until one is set; nothing is lost.</Note>
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
                ? <>Skipped (not configured on this Mac): <code>{doctor.not_configured.join(", ")}</code>. Expected unless you are migrating from a file-based memory.</>
                : "Every check ran."}
              {!doctor.ok ? " See the Doctor pane for details." : null}
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
          <button type="button" className="primary" onClick={finish}>Finish</button>
        ) : (
          <button type="button" className="primary" onClick={() => go(1)}>
            {step === "welcome" ? "Start" : "Next"} <ChevronRight size={14} aria-hidden />
          </button>
        )}
      </div>
    </div>
  );
}
