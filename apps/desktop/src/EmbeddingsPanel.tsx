/* Settings -> Embeddings (docs/plans/2026-10-07-library-sources-byoe.md, "The
   user's view"; approved mock docs/mockups/2026-10-07-embeddings/).

   One place to see every embedding model (profile), what uses it, how much of
   each search space it covers, and to change any of that. Every change is a
   job with a receipt: an estimate first, then progress with Cancel, then a
   receipt that says partial when it is partial. The CLI owns all state; this
   screen reads `embed profiles list`, `library list` and `embed jobs`, and
   re-reads after every write. Writes go through fixed-argv Tauri commands, and
   a key is stored in the Keychain and never shown again. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { invoke } from "@tauri-apps/api/core";
import { open as openPathDialog } from "@tauri-apps/plugin-dialog";
import { Loader2 } from "lucide-react";
import { Dialog, Tag } from "./ui";
import { callCli } from "./settingsCli";

// ---------------------------------------------------------------------------
// Shapes: exactly what the CLI prints (packages/cli/khipu/embed_ops.py,
// embed_jobs.py, library.py, embed.py).
// ---------------------------------------------------------------------------

export type Coverage = {
  total: number;
  embedded: number;
  missing: number;
  stale: number;
  pct: number;
};

export type ProfileRow = {
  id: string;
  provider: string;
  model: string;
  dim: number;
  normalize?: string;
  endpoint?: string | null;
  is_active: boolean;
  rows?: { memory: number; library: number };
  in_use_by?: string[];
  coverage?: Record<string, Coverage>;
  key_present?: boolean;
  median_query_ms_24h?: number | null;
  price_per_million_usd?: number | null;
};

export type LibraryRow = {
  name: string;
  root: string;
  profile: string;
  enabled: boolean;
  documents: number;
  chunks: number;
  embedded: number;
  missing: number;
  stale: number;
  pct: number;
};

export type JobRow = {
  job: string;
  kind?: string;
  profile?: string;
  space: string;
  state: "running" | "done" | "failed" | "cancelled";
  done: number;
  total: number;
  failed: number;
  started_at?: string;
  updated_at?: string;
  error?: string | null;
  pid?: number;
};

export type Estimate = {
  profile: string;
  provider: string;
  space: string;
  chunks: number;
  chars: number;
  approx_tokens: number;
  price_usd: number | null;
  price_note: string;
  approx_seconds: number | null;
  rate_source: string | null;
};

type GapSample = {
  chunks?: number;
  missing?: number;
  stale?: number;
  sample_missing?: string[];
  sample_stale?: string[];
};

type ImportReceipt = {
  read: number;
  imported: number;
  skipped_corrupt: number;
  skipped_bad_vector?: number;
  skipped_bad_row?: number;
  unmapped: number;
  documents?: number;
  note?: string;
};

type ScanReceipt = {
  added: number;
  updated: number;
  removed: number;
  unchanged: number;
  skipped: number;
  documents: number;
  chunks: number;
};

// ---------------------------------------------------------------------------
// Small helpers.
// ---------------------------------------------------------------------------

const POLL_MS = 2000;
const PROVIDER_LABEL: Record<string, string> = {
  gemini: "Gemini",
  voyage: "Voyage",
  "openai-compatible": "OpenAI-compatible",
};
const KEY_ACCOUNT: Record<string, string> = {
  gemini: "gemini_api_key",
  voyage: "voyage_api_key",
  "openai-compatible": "openai_compat_api_key",
};
const LIBRARY_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,39}$/;
const FRESH_RECEIPT_MS = 10 * 60 * 1000;

const num = (n: number | null | undefined) => (n ?? 0).toLocaleString("en-US");

function errText(e: unknown): string {
  if (typeof e === "string") return e;
  if (e instanceof Error) return e.message;
  return String(e);
}

/** `job_start` and `job_cancel` answer with an object, not a JSON string. */
async function callObject(command: string, args: Record<string, unknown>): Promise<
  { ok: true; data: Record<string, unknown> } | { ok: false; error: string }
> {
  try {
    const raw = await invoke<unknown>(command, args);
    const data = typeof raw === "string" ? JSON.parse(raw) : raw;
    if (data && typeof data === "object" && (data as { ok?: unknown }).ok === false) {
      return { ok: false, error: String((data as { error?: unknown }).error ?? "Khipu refused.") };
    }
    return { ok: true, data: (data ?? {}) as Record<string, unknown> };
  } catch (e) {
    return { ok: false, error: errText(e) };
  }
}

function isLoopback(endpoint?: string | null): boolean {
  return /^http:\/\/(localhost|127\.0\.0\.1)(:|\/|$)/.test(endpoint ?? "");
}

function pricePhrase(p: ProfileRow): string {
  const price = p.price_per_million_usd;
  if (price === 0) return "local, no charge";
  if (price == null) return "price unknown";
  return `$${price} per 1M tokens`;
}

function delayPhrase(p: ProfileRow): string {
  return typeof p.median_query_ms_24h === "number" ? `${num(Math.round(p.median_query_ms_24h))} ms` : "not measured yet";
}

function modelOptionLabel(p: ProfileRow): string {
  return `${p.id} · ${delayPhrase(p)} · ${pricePhrase(p)}`;
}

function tokensPhrase(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  return num(n);
}

function durationPhrase(seconds: number): string {
  if (seconds < 60) return "less than a minute";
  const mins = Math.round(seconds / 60);
  if (mins < 90) return `${mins} min`;
  const hours = seconds / 3600;
  return `${hours.toFixed(hours >= 10 ? 0 : 1)} h`;
}

function minutesLeft(job: JobRow, now: number): string | null {
  const started = job.started_at ? Date.parse(job.started_at) : NaN;
  if (!Number.isFinite(started) || job.done <= 0 || job.total <= job.done) return null;
  const elapsed = (now - started) / 1000;
  if (elapsed <= 5) return null;
  return durationPhrase(((job.total - job.done) / (job.done / elapsed)));
}

function refLabel(ref: string): string {
  if (ref.startsWith("episode:")) return `Session ${ref.slice("episode:".length)}`;
  if (ref.startsWith("topic:")) return `Topic page · ${ref.slice("topic:".length)}`;
  if (ref.startsWith("commitment:")) return `Commitment ${ref.slice("commitment:".length)}`;
  return ref;
}

function lastSegment(path: string): string {
  const parts = path.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] ?? "";
}

function suggestName(folder: string): string {
  return lastSegment(folder).toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^[-_]+|[-_]+$/g, "").slice(0, 40);
}

function jobIsFresh(job: JobRow, now: number): boolean {
  const t = job.updated_at ? Date.parse(job.updated_at) : NaN;
  return Number.isFinite(t) && now - t < FRESH_RECEIPT_MS;
}

/** A finished job shows while it needs attention (failed, partial) or just
 *  finished; an old clean receipt is noise. */
function showsReceipt(job: JobRow, now: number): boolean {
  if (job.state === "running") return true;
  if (job.state === "failed") return true;
  if (job.state === "done" && (job.failed > 0 || job.done < job.total)) return true;
  return jobIsFresh(job, now);
}

/** A model id that only breaks after the "@", never at a hyphen. */
function ModelId({ id }: { id: string }) {
  const at = id.lastIndexOf("@");
  if (at < 0) return <span className="idpart">{id}</span>;
  return (
    <>
      <span className="idpart">{id.slice(0, at)}</span>
      <wbr />
      <span className="idpart">{id.slice(at)}</span>
    </>
  );
}

function deleteIsEmpty(p: ProfileRow): boolean {
  return (p.rows?.memory ?? 0) + (p.rows?.library ?? 0) === 0;
}

function JobBar({ label, done, total }: { label: string; done: number; total: number }) {
  const pct = total > 0 ? Math.min(100, Math.round((done * 100) / total)) : 0;
  return (
    <div className="bar" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={total} aria-valuenow={done}>
      <i style={{ width: `${pct}%` }} />
    </div>
  );
}

function CoverageBar({
  label,
  cov,
  tag,
  onTag,
  tagExpanded,
}: {
  label: string;
  cov: Coverage;
  tag?: { tone: "ok" | "warn" | "neutral"; text: string };
  onTag?: () => void;
  tagExpanded?: boolean;
}) {
  const pct = cov.total > 0 ? Math.round((cov.embedded * 100) / cov.total) : 0;
  const complete = cov.total > 0 && cov.pct >= 100;
  return (
    <>
      <span>{label}</span>
      <div
        className={complete ? "bar ok" : "bar"}
        role="progressbar"
        aria-label={`${label} coverage`}
        aria-valuemin={0}
        aria-valuemax={cov.total}
        aria-valuenow={cov.embedded}
      >
        <i style={{ width: `${pct}%` }} />
      </div>
      <span className="num">
        {num(cov.embedded)} of {num(cov.total)} pieces
      </span>
      <span>
        {tag ? (
          onTag ? (
            <button type="button" className="sm" aria-expanded={tagExpanded} onClick={onTag}>
              {tag.text}
            </button>
          ) : (
            <Tag tone={tag.tone} dot>
              {tag.text}
            </Tag>
          )
        ) : null}
      </span>
    </>
  );
}

function Action({
  label,
  why,
  onClick,
  disabled,
  tone,
  busy,
  note,
}: {
  label: string;
  why?: string;
  /** Shown under the button while it is enabled. */
  note?: string;
  onClick: () => void;
  disabled?: boolean;
  tone?: "primary" | "danger";
  busy?: boolean;
}) {
  const whyId = useRef(`why-${Math.random().toString(36).slice(2, 9)}`).current;
  return (
    <div className="act">
      <button
        type="button"
        className={tone}
        disabled={disabled || busy}
        aria-describedby={disabled && why ? whyId : undefined}
        onClick={onClick}
      >
        {busy ? <Loader2 size={14} className="spin" aria-hidden /> : null}
        {label}
      </button>
      {disabled && why ? (
        <span id={whyId} className="why">
          {why}
        </span>
      ) : !disabled && note ? (
        <span className="why">{note}</span>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Dialog state.
// ---------------------------------------------------------------------------

type EstimateTarget = {
  title: string;
  profile: string;
  space: string;
  stale: boolean;
  kind: "memory" | "library";
  library?: string;
  /** What the person is told about what search does meanwhile. */
  note: string;
};

type DialogState =
  | { kind: "add-model" }
  | { kind: "estimate"; target: EstimateTarget }
  | { kind: "delete-index"; profile: ProfileRow }
  | { kind: "remove-library"; library: LibraryRow }
  | { kind: "re-embed-with"; library: LibraryRow }
  | { kind: "import"; library: LibraryRow }
  | null;

// ---------------------------------------------------------------------------
// The screen.
// ---------------------------------------------------------------------------

export function EmbeddingsPanel({ active }: { active: boolean }) {
  const [profiles, setProfiles] = useState<ProfileRow[] | null>(null);
  const [libraries, setLibraries] = useState<LibraryRow[] | null>(null);
  const [jobs, setJobs] = useState<JobRow[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<DialogState>(null);
  const [busy, setBusy] = useState<string | null>(null);
  /** An error per row key (`profile:<id>` / `library:<name>`) or `screen`. */
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [starting, setStarting] = useState<Array<{ id: string; space: string; profile: string; since: number }>>([]);
  const [redoOpen, setRedoOpen] = useState<Record<string, GapSample | "loading" | "error">>({});
  const [receipts, setReceipts] = useState<Record<string, { title: string; lines: ReactNode[]; tone?: "ok" | "warn" }>>({});
  /** Per library: other models that already hold some of its pieces, and how
   *  many each still lacks (from `embed estimate`; 0 means complete). */
  const [libCandidates, setLibCandidates] = useState<Record<string, Array<{ profile: string; remaining: number }>>>({});
  const [adding, setAdding] = useState({ folder: "", name: "", model: "" });
  const [addError, setAddError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const mounted = useRef(true);
  const lastRunning = useRef<Set<string>>(new Set());

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const setError = useCallback((key: string, message: string | null) => {
    setErrors((prev) => {
      const out = { ...prev };
      if (message) out[key] = message;
      else delete out[key];
      return out;
    });
  }, []);

  const loadAll = useCallback(async () => {
    const [p, l, j] = await Promise.all([
      callCli<{ profiles: ProfileRow[] }>("khipu_embed_profiles_list"),
      callCli<{ libraries: LibraryRow[] }>("khipu_library_list"),
      callCli<{ jobs: JobRow[] }>("khipu_embed_jobs"),
    ]);
    if (!mounted.current) return;
    if (p.ok) setProfiles(p.data.profiles ?? []);
    if (l.ok) setLibraries(l.data.libraries ?? []);
    if (j.ok) setJobs(j.data.jobs ?? []);
    const first = !p.ok ? p.error : !l.ok ? `Libraries: ${l.error}` : null;
    setLoadError(first);
    if (p.ok && l.ok) {
      const out: Record<string, Array<{ profile: string; remaining: number }>> = {};
      await Promise.all(
        (l.data.libraries ?? []).map(async (lib) => {
          const others = (p.data.profiles ?? []).filter((x) => x.id !== lib.profile && (x.rows?.library ?? 0) > 0);
          const found = await Promise.all(
            others.map(async (x) => {
              const est = await callCli<Estimate>("khipu_embed_estimate", {
                profile: x.id,
                space: `library:${lib.name}`,
                stale: true,
              });
              return est.ok && lib.chunks > 0 && est.data.chunks < lib.chunks
                ? { profile: x.id, remaining: est.data.chunks }
                : null;
            }),
          );
          out[lib.name] = found.filter((x): x is { profile: string; remaining: number } => x != null);
        }),
      );
      if (mounted.current) setLibCandidates(out);
    }
  }, []);

  const loadJobs = useCallback(async () => {
    const j = await callCli<{ jobs: JobRow[] }>("khipu_embed_jobs");
    if (mounted.current && j.ok) setJobs(j.data.jobs ?? []);
    if (mounted.current) setNow(Date.now());
  }, []);

  useEffect(() => {
    if (active) void loadAll();
  }, [active, loadAll]);

  const anyRunning = jobs.some((j) => j.state === "running") || starting.length > 0;

  // Poll the job files every 2 s while a job runs; reload everything the
  // moment one stops, so coverage and the Make active gate catch up.
  useEffect(() => {
    if (!active || !anyRunning) return;
    const t = setInterval(() => void loadJobs(), POLL_MS);
    return () => clearInterval(t);
  }, [active, anyRunning, loadJobs]);

  useEffect(() => {
    const running = new Set(jobs.filter((j) => j.state === "running").map((j) => j.job));
    const stoppedNow = [...lastRunning.current].some((id) => !running.has(id));
    lastRunning.current = running;
    // A job the app started has appeared in the list: it is no longer "starting".
    setStarting((prev) => {
      const seen = new Set(jobs.map((j) => j.job));
      const next = prev.filter((s) => !seen.has(s.id) && now - s.since < 60_000);
      return next.length === prev.length ? prev : next;
    });
    if (stoppedNow) void loadAll();
  }, [jobs, now, loadAll]);

  const activeProfile = profiles?.find((p) => p.is_active) ?? null;

  const jobFor = useCallback(
    (space: string, profile?: string): JobRow | null => {
      const hit = jobs.find((j) => j.space === space && (profile === undefined || j.profile === profile));
      return hit && showsReceipt(hit, now) ? hit : null;
    },
    [jobs, now],
  );

  const startingFor = useCallback(
    (space: string, profile?: string) =>
      starting.find((s) => s.space === space && (profile === undefined || s.profile === profile)) ?? null,
    [starting],
  );

  async function startJob(target: EstimateTarget, rowKey: string) {
    const res = await callObject("khipu_job_start", {
      kind: target.kind,
      profile: target.profile,
      library: target.library ?? null,
      stale: target.stale,
    });
    if (!res.ok) {
      setError(rowKey, res.error);
      return;
    }
    setError(rowKey, null);
    const id = String(res.data.job_id ?? "");
    setStarting((prev) => [...prev, { id, space: target.space, profile: target.profile, since: Date.now() }]);
    setNow(Date.now());
    setTimeout(() => void loadJobs(), 1500);
  }

  async function cancelJob(jobId: string, rowKey: string) {
    setBusy(`cancel:${jobId}`);
    const res = await callObject("khipu_job_cancel", { job_id: jobId });
    setError(rowKey, res.ok ? null : res.error);
    await loadJobs();
    setBusy(null);
  }

  async function dismissJobs() {
    await callCli("khipu_embed_jobs_clear");
    await loadJobs();
  }

  async function makeActive(p: ProfileRow) {
    setBusy(`activate:${p.id}`);
    const res = await callCli("khipu_embed_activate", { profile: p.id });
    setError(`profile:${p.id}`, res.ok ? null : res.error);
    await loadAll();
    setBusy(null);
  }

  async function toggleRedo(key: string, loader: () => Promise<GapSample | null>) {
    if (redoOpen[key]) {
      setRedoOpen((prev) => {
        const out = { ...prev };
        delete out[key];
        return out;
      });
      return;
    }
    setRedoOpen((prev) => ({ ...prev, [key]: "loading" }));
    const sample = await loader();
    if (mounted.current) setRedoOpen((prev) => ({ ...prev, [key]: sample ?? "error" }));
  }

  // ----- Models ------------------------------------------------------------

  function memoryTarget(p: ProfileRow, replace: boolean): EstimateTarget {
    return {
      title: replace
        ? `Re-embed memory with ${p.model}?`
        : `Embed the missing pieces of memory with ${p.model}?`,
      profile: p.id,
      space: "memory",
      stale: true,
      kind: "memory",
      note: p.is_active
        ? "Search uses each piece as soon as it is embedded."
        : `Search keeps using ${activeProfile?.model ?? "the current model"} until you choose Make active.`,
    };
  }

  function renderModel(p: ProfileRow) {
    const key = `profile:${p.id}`;
    const mem = p.coverage?.memory;
    const memJob = jobFor("memory", p.id);
    const memStarting = startingFor("memory", p.id);
    const libUsers = (p.in_use_by ?? []).filter((u) => u !== "memory");
    const libRows = p.rows?.library ?? 0;
    const memRows = p.rows?.memory ?? 0;
    const inUse = (p.in_use_by ?? []).length > 0;
    const jobRunning = memJob?.state === "running" || memStarting != null;
    const libJobBusy = libUsers.some((n) => jobFor(`library:${n}`)?.state === "running" || startingFor(`library:${n}`));
    const anyBusy = jobRunning || libJobBusy;
    const showMemory =
      p.is_active ||
      (mem?.embedded ?? 0) > 0 ||
      memJob != null ||
      memStarting != null ||
      (libRows === 0 && !inUse);
    const toRedo = mem ? mem.missing + mem.stale : 0;
    const redo = redoOpen[`memory:${p.id}`];
    const memTag = !mem
      ? undefined
      : mem.total > 0 && mem.pct >= 100
        ? ({ tone: "ok", text: "Complete" } as const)
        : p.is_active && toRedo > 0
          ? ({ tone: "warn", text: `${num(toRedo)} to redo` } as const)
          : mem.embedded === 0
            ? ({ tone: "warn", text: "Not started" } as const)
            : ({ tone: "warn", text: memJob?.state === "running" ? "In progress" : "Partial" } as const);

    const failedJob = memJob && memJob.state === "failed" ? memJob : null;
    const deleteWhy = anyBusy
      ? "A job is running."
      : failedJob
        ? "Cancel or retry the job first."
        : inUse
        ? p.is_active && libUsers.length === 0
          ? "Memory search uses it."
          : `Used by ${(p.in_use_by ?? []).map((u) => (u === "memory" ? "memory" : u)).join(", ")}.`
        : "";
    const isEmpty = memRows + libRows === 0;
    const canDelete = !anyBusy && !failedJob && !inUse;

    const pills: ReactNode[] = [];
    if (p.is_active) pills.push(<Tag key="a" tone="accent">Active for memory</Tag>);
    for (const n of libUsers) pills.push(<Tag key={`l-${n}`} tone="neutral">Library: {n}</Tag>);
    if (!inUse) pills.push(<Tag key="n" tone="neutral">Not used</Tag>);

    const keyText =
      p.provider === "openai-compatible" && isLoopback(p.endpoint)
        ? "No key needed"
        : p.key_present
          ? "Key: saved"
          : "Key: missing";

    return (
      <div key={p.id} className="emb-row" role="group" aria-label={p.id}>
        <div className="emb-top">
          <div className="emb-id">
            <span className="job-name mono emb-name">
              <ModelId id={p.id} />
            </span>
            {pills}
          </div>
          <div className="emb-q">
            Typical search delay <b>{delayPhrase(p)}</b>
          </div>
        </div>
        <span className="job-meta">
          {PROVIDER_LABEL[p.provider] ?? p.provider} · {p.dim} dimensions
          {p.endpoint ? (
            <>
              {" · "}
              <span className="mono">{p.endpoint}</span>
            </>
          ) : null}
          {" · "}
          {keyText}
          {" · "}
          {pricePhrase(p)}
        </span>

        <div className="emb-cov">
          {mem && showMemory ? (
            <CoverageBar
              label="Memory"
              cov={mem}
              tag={memTag}
              onTag={
                p.is_active && toRedo > 0 && !memJob
                  ? () =>
                      void toggleRedo(`memory:${p.id}`, async () => {
                        const res = await callCli<GapSample>("khipu_embed_status", { profile: p.id });
                        return res.ok ? res.data : null;
                      })
                  : undefined
              }
              tagExpanded={redo != null}
            />
          ) : null}
          {libUsers.map((n) => {
            const c = p.coverage?.[`library:${n}`];
            return c ? <CoverageBar key={n} label={`Library “${n}”`} cov={c} tag={c.pct >= 100 && c.total > 0 ? { tone: "ok", text: "Complete" } : { tone: "warn", text: "Partial" }} /> : null;
          })}
          {!inUse && libRows > 0 && !showMemory ? (
            <>
              <span>Index</span>
              <span className="sub-sm" style={{ gridColumn: "2 / 5" }}>
                {num(libRows)} pieces of text. No library or memory uses it.
              </span>
            </>
          ) : null}
        </div>

        {redo != null ? (
          <div className="receipt" role="group" aria-label="Pieces of text to redo">
            <div className="top emb-top">
              <div className="job-main">
                <span className="job-name">Redo some</span>
                <span className="job-meta">
                  {redo === "loading"
                    ? "Looking for gaps…"
                    : redo === "error"
                      ? "Could not read the gaps."
                      : `${num(toRedo)} pieces of text have no index entry or an out-of-date one.`}
                </span>
              </div>
            </div>
            {redo !== "loading" && redo !== "error" ? (
              <>
                <div className="line">
                  <span>
                    <b>{num(redo.missing)}</b> missing (never embedded)
                  </span>
                  <span>
                    <b>{num(redo.stale)}</b> stale (the text changed after it was embedded)
                  </span>
                </div>
                <ul className="redo-list" aria-label="Examples">
                  {(redo.sample_missing ?? []).map((r) => (
                    <li key={`m-${r}`}>
                      <span>{refLabel(r)}</span>
                      <Tag tone="warn">Missing</Tag>
                    </li>
                  ))}
                  {(redo.sample_stale ?? []).map((r) => (
                    <li key={`s-${r}`}>
                      <span>{refLabel(r)}</span>
                      <Tag tone="neutral">Stale</Tag>
                    </li>
                  ))}
                  <li>
                    <span>Examples only; the full list is longer.</span>
                  </li>
                </ul>
              </>
            ) : null}
          </div>
        ) : null}

        {memStarting && !memJob ? (
          <div className="emb-job" role="status">
            <div className="top">
              <div className="job-main">
                <span className="job-name">Starting…</span>
                <span className="job-meta">The job is starting. Search keeps using {activeProfile?.model ?? "the current model"}.</span>
              </div>
              <Loader2 size={14} className="spin" aria-hidden />
            </div>
          </div>
        ) : null}
        {memJob ? (
          <JobPanel
            job={memJob}
            now={now}
            noun="memory"
            profileLabel={p.is_active ? null : activeProfile?.model ?? null}
            busy={busy === `cancel:${memJob.job}`}
            onCancel={() => void cancelJob(memJob.job, key)}
            onRetry={() => setDialog({ kind: "estimate", target: memoryTarget(p, false) })}
            onDismiss={() => void dismissJobs()}
          />
        ) : null}

        <div className="emb-acts">
          {mem && showMemory ? (
            <>
              {!p.is_active ? (
                <Action
                  label="Make active for memory"
                  tone={mem.pct >= 100 ? "primary" : undefined}
                  disabled={mem.pct < 100 || anyBusy || failedJob != null}
                  why={
                    anyBusy
                      ? "A job is running."
                      : mem.missing > 0
                        ? `${num(mem.missing)} missing`
                        : mem.stale > 0
                          ? `${num(mem.stale)} out of date`
                          : "Nothing is embedded yet."
                  }
                  note="Takes effect on the next search."
                  busy={busy === `activate:${p.id}`}
                  onClick={() => void makeActive(p)}
                />
              ) : null}
              {!p.is_active && mem.embedded === 0 && !failedJob ? (
                <Action
                  label="Re-embed memory with this model"
                  disabled={anyBusy}
                  why="A job is running."
                  onClick={() => setDialog({ kind: "estimate", target: memoryTarget(p, true) })}
                />
              ) : !failedJob ? (
                <Action
                  label={toRedo > 0 ? `Embed missing (${num(toRedo)})` : "Embed missing"}
                  disabled={anyBusy || toRedo === 0}
                  why={anyBusy ? "A job is running." : "Nothing missing."}
                  onClick={() => setDialog({ kind: "estimate", target: memoryTarget(p, false) })}
                />
              ) : null}
            </>
          ) : null}
          <Action
            label={isEmpty ? "Remove model" : "Delete index"}
            tone={canDelete ? "danger" : undefined}
            disabled={!canDelete}
            why={deleteWhy}
            onClick={() => setDialog({ kind: "delete-index", profile: p })}
          />
        </div>
        {errors[key] ? (
          <p className="set-error" role="alert">
            {errors[key]}
          </p>
        ) : null}
      </div>
    );
  }

  // ----- Libraries ---------------------------------------------------------

  async function scanLibrary(l: LibraryRow) {
    const key = `library:${l.name}`;
    setBusy(`scan:${l.name}`);
    const res = await callCli<ScanReceipt>("khipu_library_scan", { name: l.name });
    if (res.ok) {
      setError(key, null);
      setReceipts((prev) => ({
        ...prev,
        [key]: {
          title: "Scanned the folder",
          tone: "ok",
          lines: [
            <span key="a"><b>{num(res.data.added)}</b> new</span>,
            <span key="u"><b>{num(res.data.updated)}</b> changed</span>,
            <span key="r"><b>{num(res.data.removed)}</b> gone</span>,
            <span key="n"><b>{num(res.data.unchanged)}</b> unchanged</span>,
            <span key="s"><b>{num(res.data.skipped)}</b> skipped</span>,
          ],
        },
      }));
    } else {
      setError(key, res.error);
    }
    await loadAll();
    setBusy(null);
  }

  async function setLibraryProfile(l: LibraryRow, profile: string) {
    setBusy(`setprofile:${l.name}`);
    const res = await callCli("khipu_library_set_profile", { name: l.name, profile });
    setError(`library:${l.name}`, res.ok ? null : res.error);
    await loadAll();
    setBusy(null);
  }

  async function setLibraryEnabled(l: LibraryRow, enabled: boolean) {
    setBusy(`enable:${l.name}`);
    const res = await callCli(enabled ? "khipu_library_enable" : "khipu_library_disable", { name: l.name });
    setError(`library:${l.name}`, res.ok ? null : res.error);
    await loadAll();
    setBusy(null);
  }

  function libraryTarget(l: LibraryRow, profile: string, replace: boolean): EstimateTarget {
    const p = profiles?.find((x) => x.id === profile);
    const own = profile === l.profile;
    return {
      title: replace
        ? `Re-embed ${l.name} with ${p?.model ?? profile}?`
        : `Embed the missing pieces of ${l.name} with ${p?.model ?? profile}?`,
      profile,
      space: `library:${l.name}`,
      stale: l.stale > 0 || replace,
      kind: "library",
      library: l.name,
      note: own
        ? "Search uses each piece as soon as it is embedded."
        : `Search keeps using ${profiles?.find((x) => x.id === l.profile)?.model ?? l.profile} until you choose Make active for ${l.name}.`,
    };
  }

  function renderLibrary(l: LibraryRow) {
    const key = `library:${l.name}`;
    const space = `library:${l.name}`;
    const job = jobFor(space);
    const jobStarting = startingFor(space);
    const running = job?.state === "running" || jobStarting != null || busy === `import:${l.name}` || busy === `scan:${l.name}`;
    const gaps = l.missing + l.stale;
    const redo = redoOpen[space];
    const receipt = receipts[key];
    const libProfile = profiles?.find((p) => p.id === l.profile);
    const cand = [...(libCandidates[l.name] ?? [])].sort((a, b) => a.remaining - b.remaining)[0];
    const candModel = cand ? profiles?.find((p) => p.id === cand.profile)?.model ?? cand.profile : "";
    const tag =
      l.chunks === 0
        ? ({ tone: "neutral", text: "Nothing indexed yet" } as const)
        : l.pct >= 100
          ? ({ tone: "ok", text: "100% embedded" } as const)
          : ({ tone: "warn", text: `${l.pct}% embedded` } as const);
    return (
      <div key={l.name} className="lib-row" role="group" aria-label={`Library ${l.name}`}>
        <div className="emb-top">
          <div className="emb-id">
            <span className="job-name">{l.name}</span>
            <Tag tone={tag.tone} dot>
              {tag.text}
            </Tag>
            {!l.enabled ? <Tag tone="neutral">Search off</Tag> : null}
          </div>
        </div>
        <span className="path">{l.root}</span>
        <div className="lib-stats">
          <span>
            <b>{num(l.documents)}</b> documents
          </span>
          <span>
            <b>{num(l.chunks)}</b> pieces of text
          </span>
          <span>
            Model <b style={{ fontFamily: "var(--mono)", fontSize: "var(--t-sm)" }}>{libProfile?.id ?? l.profile}</b>
          </span>
          {gaps > 0 && l.chunks > 0 ? (
            <button
              type="button"
              className="sm"
              aria-expanded={redo != null}
              onClick={() =>
                void toggleRedo(space, async () => {
                  const res = await callCli<GapSample>("khipu_library_status", { name: l.name });
                  return res.ok ? res.data : null;
                })
              }
            >
              {num(gaps)} to redo
            </button>
          ) : null}
        </div>

        {redo != null ? (
          <div className="receipt" role="group" aria-label={`Pieces of ${l.name} to redo`}>
            <div className="top emb-top">
              <div className="job-main">
                <span className="job-name">Redo some</span>
                <span className="job-meta">
                  {redo === "loading"
                    ? "Looking for gaps…"
                    : redo === "error"
                      ? "Could not read the gaps."
                      : `${num(gaps)} pieces of text have no index entry or an out-of-date one.`}
                </span>
              </div>
            </div>
            {redo !== "loading" && redo !== "error" ? (
              <>
                <div className="line">
                  <span>
                    <b>{num(l.missing)}</b> missing (never embedded)
                  </span>
                  <span>
                    <b>{num(l.stale)}</b> stale (the text changed after it was embedded)
                  </span>
                </div>
                <ul className="redo-list" aria-label="Examples">
                  {(redo.sample_missing ?? []).map((r) => (
                    <li key={`m-${r}`}>
                      <span className="mono">{r}</span>
                      <Tag tone="warn">Missing</Tag>
                    </li>
                  ))}
                  {(redo.sample_stale ?? []).map((r) => (
                    <li key={`s-${r}`}>
                      <span className="mono">{r}</span>
                      <Tag tone="neutral">Stale</Tag>
                    </li>
                  ))}
                  <li>
                    <span>Examples only; the full list is longer.</span>
                  </li>
                </ul>
              </>
            ) : null}
          </div>
        ) : null}

        {jobStarting && !job ? (
          <div className="emb-job" role="status">
            <div className="top">
              <div className="job-main">
                <span className="job-name">Starting…</span>
              </div>
              <Loader2 size={14} className="spin" aria-hidden />
            </div>
          </div>
        ) : null}
        {job ? (
          <JobPanel
            job={job}
            now={now}
            noun={l.name}
            profileLabel={null}
            busy={busy === `cancel:${job.job}`}
            onCancel={() => void cancelJob(job.job, key)}
            onRetry={() => setDialog({ kind: "estimate", target: libraryTarget(l, job.profile || l.profile, false) })}
            onDismiss={() => void dismissJobs()}
          />
        ) : null}
        {busy === `import:${l.name}` ? (
          <div className="emb-job" role="status">
            <div className="top">
              <div className="job-main">
                <span className="job-name">Importing an index…</span>
                <span className="job-meta">This can take several minutes for a large index. You can leave this screen open.</span>
              </div>
              <Loader2 size={14} className="spin" aria-hidden />
            </div>
          </div>
        ) : null}

        <div className="emb-acts">
          <Action
            label="Scan"
            disabled={running}
            why={running ? "A job is running." : undefined}
            busy={busy === `scan:${l.name}`}
            onClick={() => void scanLibrary(l)}
          />
          <Action
            label="Embed missing"
            disabled={running || gaps === 0}
            why={running ? "A job is running." : "Nothing missing."}
            onClick={() => setDialog({ kind: "estimate", target: libraryTarget(l, l.profile, false) })}
          />
          <Action
            label={`Re-embed ${l.name} with…`}
            disabled={running}
            why="A job is running."
            onClick={() => setDialog({ kind: "re-embed-with", library: l })}
          />
          {cand ? (
            <Action
              label={`Make active for ${l.name}`}
              tone={cand.remaining === 0 ? "primary" : undefined}
              disabled={cand.remaining > 0 || running}
              why={running ? "A job is running." : `${num(cand.remaining)} missing under ${candModel}`}
              note={`Moves search to ${candModel}. Takes effect on the next search.`}
              busy={busy === `setprofile:${l.name}`}
              onClick={() => void setLibraryProfile(l, cand.profile)}
            />
          ) : null}
          <Action
            label="Import an index"
            disabled={running}
            why="A job is running."
            onClick={() => setDialog({ kind: "import", library: l })}
          />
          {!l.enabled ? (
            <Action
              label="Turn on search"
              disabled={running}
              why="A job is running."
              busy={busy === `enable:${l.name}`}
              onClick={() => void setLibraryEnabled(l, true)}
            />
          ) : null}
          <Action
            label="Remove"
            tone="danger"
            disabled={running}
            why="A job is running."
            onClick={() => setDialog({ kind: "remove-library", library: l })}
          />
        </div>
        {receipt ? (
          <div className="receipt" role="group" aria-label="Receipt">
            <div className="top emb-top">
              <div className="job-main">
                <span className="job-name">{receipt.title}</span>
              </div>
              {receipt.tone ? (
                <Tag tone={receipt.tone} dot>
                  {receipt.tone === "ok" ? "Done" : "Partial"}
                </Tag>
              ) : null}
              <button
                type="button"
                className="sm"
                onClick={() =>
                  setReceipts((prev) => {
                    const out = { ...prev };
                    delete out[key];
                    return out;
                  })
                }
              >
                Dismiss
              </button>
            </div>
            <div className="line">{receipt.lines}</div>
          </div>
        ) : null}
        {errors[key] ? (
          <p className="set-error" role="alert">
            {errors[key]}
          </p>
        ) : null}
      </div>
    );
  }

  // ----- Add a library -----------------------------------------------------

  const modelChoice = adding.model || profiles?.find((p) => p.is_active)?.id || profiles?.[0]?.id || "";
  const nameOk = LIBRARY_NAME_RE.test(adding.name);
  const canAddLibrary = adding.folder.trim().length > 0 && nameOk && modelChoice !== "";

  async function chooseFolder() {
    try {
      const picked = await openPathDialog({ directory: true, multiple: false });
      if (typeof picked === "string" && picked) {
        setAdding((a) => ({ ...a, folder: picked, name: a.name || suggestName(picked) }));
      }
    } catch (e) {
      setAddError(errText(e));
    }
  }

  async function addLibrary() {
    setAddError(null);
    if (!nameOk) {
      setAddError("Use lowercase letters, digits, - and _ for the name (up to 40), starting with a letter or digit.");
      return;
    }
    setBusy("add-library");
    const res = await callCli("khipu_library_add", {
      name: adding.name,
      root: adding.folder.trim(),
      profile: modelChoice,
    });
    if (!res.ok) {
      setAddError(res.error);
      setBusy(null);
      return;
    }
    const name = adding.name;
    setAdding({ folder: "", name: "", model: "" });
    // A library with no scan has no documents, so look at the folder now (no model call).
    await scanLibrary({ name } as LibraryRow);
    setBusy(null);
  }

  // ----- Render ------------------------------------------------------------

  const profileList = profiles ?? [];
  return (
    <>
      <p className="set-help set-intro">
        Khipu turns text into numbers so it can search by meaning. Each model below is one way of doing that.
      </p>
      {loadError ? (
        <p className="set-error" role="alert">
          Could not read the embeddings: {loadError}
        </p>
      ) : null}
      {errors.screen ? (
        <p className="set-error" role="alert">
          {errors.screen}
        </p>
      ) : null}

      <div className="section-card">
        <div className="section-head">
          Models
          <span className="spacer" />
          <button type="button" className="primary" onClick={() => setDialog({ kind: "add-model" })}>
            Add a model
          </button>
        </div>
        <div className="emb-list">
          {profiles == null && !loadError ? (
            <p className="set-help help-pad" style={{ paddingBottom: "var(--s3)" }}>
              Reading the models…
            </p>
          ) : null}
          {profiles != null && profileList.length === 0 ? (
            <p className="set-help help-pad" style={{ paddingBottom: "var(--s3)" }}>
              No models yet. Add one to search by meaning.
            </p>
          ) : null}
          {profileList.map(renderModel)}
        </div>
        <p className="help-line">Nothing is embedded until you confirm an estimate.</p>
      </div>

      <div className="section-card" id="libs">
        <div className="section-head">Libraries</div>
        <p className="set-help help-pad">
          A library is a folder of .txt and .md files that search can read. Each library has its own model.
        </p>
        {libraries != null && libraries.length === 0 ? (
          <p className="set-help help-pad" style={{ paddingBottom: "var(--s3)" }}>
            No libraries yet.
          </p>
        ) : null}
        {(libraries ?? []).map(renderLibrary)}
        <div className="lib-add">
          <span className="set-label" id="addlib">
            Add a library
          </span>
          <div className="fields" role="group" aria-labelledby="addlib">
            <div className="set-field">
              <label className="set-label" htmlFor="lib-folder">
                Folder
              </label>
              <div className="folder">
                <input
                  id="lib-folder"
                  className="mono"
                  value={adding.folder}
                  placeholder="/path/to/folder"
                  spellCheck={false}
                  onChange={(e) => setAdding((a) => ({ ...a, folder: e.target.value }))}
                />
                <button type="button" onClick={() => void chooseFolder()}>
                  Choose…
                </button>
              </div>
            </div>
            <div className="set-field">
              <label className="set-label" htmlFor="lib-name">
                Name
              </label>
              <input
                id="lib-name"
                value={adding.name}
                placeholder="Short name"
                spellCheck={false}
                aria-invalid={adding.name !== "" && !nameOk ? true : undefined}
                aria-describedby={adding.name !== "" && !nameOk ? "lib-name-err" : undefined}
                onChange={(e) => setAdding((a) => ({ ...a, name: e.target.value }))}
              />
            </div>
            <div className="set-field full">
              <label className="set-label" htmlFor="lib-model">
                Model
              </label>
              <select
                id="lib-model"
                value={modelChoice}
                onChange={(e) => setAdding((a) => ({ ...a, model: e.target.value }))}
              >
                {profileList.map((p) => (
                  <option key={p.id} value={p.id}>
                    {modelOptionLabel(p)}
                  </option>
                ))}
              </select>
            </div>
          </div>
          {adding.name !== "" && !nameOk ? (
            <p id="lib-name-err" className="set-error" role="alert">
              Use lowercase letters, digits, - and _ for the name (up to 40), starting with a letter or digit.
            </p>
          ) : null}
          <div className="emb-acts" style={{ marginTop: 0 }}>
            <Action
              label="Add library"
              tone="primary"
              disabled={!canAddLibrary || busy === "add-library"}
              why={canAddLibrary ? undefined : "Choose a folder and a name."}
              busy={busy === "add-library"}
              onClick={() => void addLibrary()}
            />
          </div>
          {addError ? (
            <p className="set-error" role="alert">
              {addError}
            </p>
          ) : null}
        </div>
        <p className="help-line">
          Embed missing and Re-embed show an estimate first. Nothing is embedded until you confirm it.
        </p>
      </div>

      <AddModelDialog
        open={dialog?.kind === "add-model"}
        onClose={() => setDialog(null)}
        onAdded={async () => {
          setDialog(null);
          await loadAll();
        }}
      />
      <EstimateDialog
        target={dialog?.kind === "estimate" ? dialog.target : null}
        onClose={() => setDialog(null)}
        onStart={async (t) => {
          setDialog(null);
          await startJob(t, t.kind === "library" ? `library:${t.library}` : `profile:${t.profile}`);
        }}
      />
      <ConfirmDialog
        id="delete-index"
        open={dialog?.kind === "delete-index"}
        title={
          dialog?.kind === "delete-index"
            ? deleteIsEmpty(dialog.profile)
              ? `Remove ${dialog.profile.model}?`
              : `Delete the index for ${dialog.profile.model}?`
            : ""
        }
        confirmLabel={dialog?.kind === "delete-index" && deleteIsEmpty(dialog.profile) ? "Remove model" : "Delete index"}
        body={
          dialog?.kind === "delete-index"
            ? deleteIsEmpty(dialog.profile)
              ? "It has no index entries."
              : `This removes the index entries for ${num((dialog.profile.rows?.memory ?? 0) + (dialog.profile.rows?.library ?? 0))} pieces of text. The text stays. You can embed again later.`
            : ""
        }
        onCancel={() => setDialog(null)}
        onConfirm={async () => {
          if (dialog?.kind !== "delete-index") return null;
          const res = await callCli("khipu_embed_profile_delete", { id: dialog.profile.id });
          if (res.ok) {
            setDialog(null);
            await loadAll();
            return null;
          }
          return res.error;
        }}
      />
      <ConfirmDialog
        id="remove-library"
        open={dialog?.kind === "remove-library"}
        title={`Remove the library ${dialog?.kind === "remove-library" ? dialog.library.name : ""}?`}
        confirmLabel="Remove library"
        body={
          dialog?.kind === "remove-library"
            ? `This removes ${num(dialog.library.documents)} documents, ${num(dialog.library.chunks)} pieces of text and ${num(dialog.library.embedded)} index entries from Khipu. The files in the folder stay.`
            : ""
        }
        onCancel={() => setDialog(null)}
        onConfirm={async () => {
          if (dialog?.kind !== "remove-library") return null;
          const res = await callCli("khipu_library_remove", { name: dialog.library.name });
          if (res.ok) {
            setDialog(null);
            await loadAll();
            return null;
          }
          return res.error;
        }}
      />
      <ReEmbedWithDialog
        library={dialog?.kind === "re-embed-with" ? dialog.library : null}
        profiles={profileList}
        onCancel={() => setDialog(null)}
        onContinue={(profileId) => {
          if (dialog?.kind !== "re-embed-with") return;
          setDialog({ kind: "estimate", target: libraryTarget(dialog.library, profileId, true) });
        }}
      />
      <ImportDialog
        library={dialog?.kind === "import" ? dialog.library : null}
        onCancel={() => setDialog(null)}
        onImport={async (l, path, strip) => {
          setDialog(null);
          const key = `library:${l.name}`;
          setBusy(`import:${l.name}`);
          const res = await callCli<ImportReceipt>("khipu_library_import", {
            name: l.name,
            path,
            strip_prefix: strip || null,
            profile: null,
          });
          if (res.ok) {
            setError(key, null);
            const d = res.data;
            const unreadable = (d.skipped_bad_vector ?? 0) + (d.skipped_bad_row ?? 0);
            setReceipts((prev) => ({
              ...prev,
              [key]: {
                title: "Imported an index",
                tone: d.imported > 0 ? "ok" : "warn",
                lines: [
                  <span key="r">Read <b>{num(d.read)}</b> rows</span>,
                  <span key="s1">·</span>,
                  <span key="i">Imported <b>{num(d.imported)}</b></span>,
                  <span key="s2">·</span>,
                  <span key="c">Skipped <b>{num(d.skipped_corrupt)}</b> corrupt</span>,
                  ...(unreadable > 0
                    ? [<span key="s3">·</span>, <span key="u"><b>{num(unreadable)}</b> unreadable</span>]
                    : []),
                  <span key="s4">·</span>,
                  <span key="m"><b>{num(d.unmapped)}</b> with no matching file</span>,
                  ...(d.note ? [<span key="n" style={{ flexBasis: "100%" }}>{d.note}</span>] : []),
                ],
              },
            }));
          } else {
            setError(key, res.error);
          }
          await loadAll();
          setBusy(null);
        }}
      />
    </>
  );
}

// ---------------------------------------------------------------------------
// A running, failed, cancelled or finished job.
// ---------------------------------------------------------------------------

function JobPanel({
  job,
  now,
  noun,
  profileLabel,
  busy,
  onCancel,
  onRetry,
  onDismiss,
}: {
  job: JobRow;
  now: number;
  noun: string;
  /** For a model that search is not using yet: what search keeps using. */
  profileLabel: string | null;
  busy: boolean;
  onCancel: () => void;
  onRetry: () => void;
  onDismiss: () => void;
}) {
  const progress = `${num(job.done)} of ${num(job.total)} pieces of text`;
  if (job.state === "running") {
    const left = minutesLeft(job, now);
    return (
      <div className="emb-job" role="group" aria-label="Embedding job">
        <div className="top">
          <div className="job-main">
            <span className="job-name">Embedding {noun}</span>
            <span className="job-meta">
              {progress}
              {left ? `, about ${left} left` : ""}.
              {profileLabel ? ` Search keeps using ${profileLabel}.` : ""}
            </span>
          </div>
          <div className="job-actions">
            <button type="button" className="sm" disabled={busy} onClick={onCancel}>
              {busy ? <Loader2 size={14} className="spin" aria-hidden /> : null}
              Cancel
            </button>
          </div>
        </div>
        <JobBar label={`Embedding ${noun}`} done={job.done} total={job.total} />
      </div>
    );
  }
  if (job.state === "failed") {
    return (
      <div className="emb-job err-job" role="alert">
        <div className="top">
          <div className="job-main">
            <span className="job-name">Embedding stopped</span>
            <span className="job-meta" style={{ color: "var(--text)" }}>
              Embedded {progress.replace(" pieces of text", "")}, stopped: {job.error ?? "the job failed"}
            </span>
          </div>
          <div className="job-actions">
            <button type="button" className="sm primary" onClick={onRetry}>
              Retry
            </button>
            <button type="button" className="sm" onClick={onDismiss}>
              Cancel
            </button>
          </div>
        </div>
      </div>
    );
  }
  const partial = job.state === "done" && (job.failed > 0 || job.done < job.total);
  const title =
    job.state === "cancelled" ? "Embedding cancelled" : partial ? "Embedding finished, partly" : "Embedding finished";
  const detail =
    job.state === "cancelled"
      ? `Embedded ${progress} before it was cancelled. Embed missing continues from there.`
      : partial
        ? `Embedded ${progress}; ${num(job.failed)} failed. Embed missing tries the rest again.`
        : `Embedded ${num(job.done)} pieces of text.`;
  return (
    <div className="emb-job" role="group" aria-label="Embedding receipt">
      <div className="top">
        <div className="job-main">
          <span className="job-name">{title}</span>
          <span className="job-meta">{detail}</span>
        </div>
        <div className="job-actions">
          {partial ? (
            <button type="button" className="sm primary" onClick={onRetry}>
              Retry
            </button>
          ) : null}
          <button type="button" className="sm" onClick={onDismiss}>
            Dismiss
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Dialogs.
// ---------------------------------------------------------------------------

function EstimateDialog({
  target,
  onClose,
  onStart,
}: {
  target: EstimateTarget | null;
  onClose: () => void;
  onStart: (t: EstimateTarget) => Promise<void>;
}) {
  const [est, setEst] = useState<Estimate | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const key = target ? `${target.profile}|${target.space}|${target.stale}` : "";

  useEffect(() => {
    setEst(null);
    setError(null);
    setStarting(false);
    if (!target) return;
    let live = true;
    void (async () => {
      const res = await callCli<Estimate>("khipu_embed_estimate", {
        profile: target.profile,
        space: target.space,
        stale: target.stale,
      });
      if (!live) return;
      if (res.ok) setEst(res.data);
      else setError(res.error);
    })();
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const price = !est
    ? ""
    : est.price_usd == null
      ? (est.price_note || "price unknown for this provider").replace(/^price /, "")
      : est.price_usd === 0
        ? "none (a local server costs nothing)"
        : `about $${est.price_usd < 0.01 ? est.price_usd.toFixed(4) : est.price_usd.toFixed(2)} at list price`;
  const time = !est
    ? ""
    : est.approx_seconds == null
      ? "not measured yet; Khipu learns the rate from this run"
      : `about ${durationPhrase(est.approx_seconds)} at the measured rate`;

  return (
    <Dialog
      open={target != null}
      className="kit-dialog emb-dialog"
      ariaLabelledBy="est-title"
      onCancel={onClose}
      initialFocusRef={cancelRef}
    >
      <div className="kit-dialog-body">
        <h2 id="est-title">{target?.title ?? ""}</h2>
        {error ? (
          <p className="set-error" role="alert">
            {error}
          </p>
        ) : !est ? (
          <p className="set-help" role="status">
            <Loader2 size={14} className="spin" aria-hidden /> Counting the pieces of text…
          </p>
        ) : (
          <>
            <dl className="dl">
              <dt>Size</dt>
              <dd>
                {num(est.chunks)} pieces of text, about {tokensPhrase(est.approx_tokens)} tokens
              </dd>
              <dt>Time</dt>
              <dd>{est.chunks === 0 ? "none" : time}</dd>
              <dt>Price</dt>
              <dd>{est.chunks === 0 ? "none" : price}</dd>
            </dl>
            <p>{est.chunks === 0 ? "Nothing is missing, so there is nothing to embed." : target?.note}</p>
          </>
        )}
        <div className="kit-dialog-actions">
          <button type="button" ref={cancelRef} onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="primary"
            disabled={!est || est.chunks === 0 || starting}
            onClick={() => {
              if (!target) return;
              setStarting(true);
              void onStart(target);
            }}
          >
            {starting ? <Loader2 size={14} className="spin" aria-hidden /> : null}
            Start
          </button>
        </div>
      </div>
    </Dialog>
  );
}

function ConfirmDialog({
  id,
  open,
  title,
  body,
  confirmLabel,
  onCancel,
  onConfirm,
}: {
  id: string;
  open: boolean;
  title: string;
  body: string;
  confirmLabel: string;
  onCancel: () => void;
  /** Resolves to the error text, or null when it worked. */
  onConfirm: () => Promise<string | null>;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (open) {
      setBusy(false);
      setError(null);
    }
  }, [open]);
  return (
    <Dialog
      open={open}
      className="kit-dialog emb-dialog"
      ariaLabelledBy={`${id}-title`}
      onCancel={onCancel}
      initialFocusRef={cancelRef}
    >
      <div className="kit-dialog-body">
        <h2 id={`${id}-title`}>{title}</h2>
        <p>{body}</p>
        {error ? (
          <p className="set-error" role="alert">
            {error}
          </p>
        ) : null}
        <div className="kit-dialog-actions">
          <button type="button" ref={cancelRef} onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className="danger"
            disabled={busy}
            onClick={() => {
              setBusy(true);
              void onConfirm().then((e) => {
                setError(e);
                setBusy(false);
              });
            }}
          >
            {busy ? <Loader2 size={14} className="spin" aria-hidden /> : null}
            {confirmLabel}
          </button>
        </div>
      </div>
    </Dialog>
  );
}

function ReEmbedWithDialog({
  library,
  profiles,
  onCancel,
  onContinue,
}: {
  library: LibraryRow | null;
  profiles: ProfileRow[];
  onCancel: () => void;
  onContinue: (profileId: string) => void;
}) {
  const [choice, setChoice] = useState("");
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (library) setChoice("");
  }, [library]);
  const value = choice || library?.profile || profiles[0]?.id || "";
  return (
    <Dialog
      open={library != null}
      className="kit-dialog emb-dialog"
      ariaLabelledBy="reembed-title"
      onCancel={onCancel}
      initialFocusRef={cancelRef}
    >
      <div className="kit-dialog-body">
        <h2 id="reembed-title">Re-embed {library?.name ?? ""} with…</h2>
        <div className="field-grid">
          <div className="set-field">
            <label className="set-label" htmlFor="reembed-model">
              Model
            </label>
            <select id="reembed-model" value={value} onChange={(e) => setChoice(e.target.value)}>
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {modelOptionLabel(p)}
                </option>
              ))}
            </select>
          </div>
          <p className="set-help">You will see an estimate before anything is embedded.</p>
        </div>
        <div className="kit-dialog-actions">
          <button type="button" ref={cancelRef} onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="primary" disabled={!value} onClick={() => onContinue(value)}>
            Continue
          </button>
        </div>
      </div>
    </Dialog>
  );
}

function ImportDialog({
  library,
  onCancel,
  onImport,
}: {
  library: LibraryRow | null;
  onCancel: () => void;
  onImport: (l: LibraryRow, path: string, strip: string) => Promise<void>;
}) {
  const [path, setPath] = useState("");
  const [strip, setStrip] = useState("");
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (library) {
      setPath("");
      setStrip("");
      setError(null);
    }
  }, [library]);

  async function choose() {
    try {
      const picked = await openPathDialog({
        directory: false,
        multiple: false,
        filters: [{ name: "Index file", extensions: ["sqlite", "db", "sqlite3", "jsonl"] }],
      });
      if (typeof picked === "string" && picked) setPath(picked);
    } catch (e) {
      setError(errText(e));
    }
  }

  const bad = path.trim().startsWith("-") || /[\u0000-\u001f]/.test(path) || /[\u0000-\u001f]/.test(strip);
  return (
    <Dialog open={library != null} className="kit-dialog emb-dialog" ariaLabelledBy="import-emb-title" onCancel={onCancel}>
      <div className="kit-dialog-body">
        <h2 id="import-emb-title">Import an index into {library?.name ?? ""}</h2>
        <p>
          Bring in vectors that were already computed elsewhere, so nothing is embedded again. Use a graphify SQLite
          file or a .jsonl file.
        </p>
        <div className="field-grid">
          <div className="set-field">
            <label className="set-label" htmlFor="import-path">
              Index file
            </label>
            <div className="folder">
              <input
                id="import-path"
                className="mono"
                value={path}
                placeholder="/path/to/index.sqlite"
                spellCheck={false}
                onChange={(e) => setPath(e.target.value)}
              />
              <button type="button" onClick={() => void choose()}>
                Choose…
              </button>
            </div>
          </div>
          <div className="set-field">
            <label className="set-label" htmlFor="import-strip">
              Remove this start from each file path (optional)
            </label>
            <input
              id="import-strip"
              className="mono"
              value={strip}
              placeholder="Biblical System/corpus/"
              spellCheck={false}
              onChange={(e) => setStrip(e.target.value)}
            />
          </div>
        </div>
        {error ? (
          <p className="set-error" role="alert">
            {error}
          </p>
        ) : null}
        <div className="kit-dialog-actions">
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className="primary"
            disabled={path.trim() === "" || bad}
            onClick={() => {
              if (library) void onImport(library, path.trim(), strip);
            }}
          >
            Import
          </button>
        </div>
      </div>
    </Dialog>
  );
}

type KeyTest = { state: "idle" } | { state: "testing" } | { state: "ok"; ms: number; dim: number } | { state: "error"; message: string };

const PROVIDER_DEFAULTS: Record<string, { model: string; dim: string; endpoint: string }> = {
  gemini: { model: "gemini-embedding-2", dim: "768", endpoint: "" },
  voyage: { model: "voyage-3", dim: "1024", endpoint: "" },
  "openai-compatible": { model: "", dim: "", endpoint: "http://localhost:11434" },
};

function AddModelDialog({
  open,
  onClose,
  onAdded,
}: {
  open: boolean;
  onClose: () => void;
  onAdded: () => Promise<void>;
}) {
  const [provider, setProvider] = useState("openai-compatible");
  const [model, setModel] = useState("");
  const [dim, setDim] = useState("");
  const [endpoint, setEndpoint] = useState("http://localhost:11434");
  const [key, setKey] = useState("");
  const [test, setTest] = useState<KeyTest>({ state: "idle" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const firstRef = useRef<HTMLSelectElement>(null);

  useEffect(() => {
    if (open) {
      setProvider("openai-compatible");
      setModel("");
      setDim("");
      setEndpoint("http://localhost:11434");
      setKey("");
      setTest({ state: "idle" });
      setBusy(false);
      setError(null);
    }
  }, [open]);

  function changeProvider(next: string) {
    const d = PROVIDER_DEFAULTS[next];
    setProvider(next);
    setModel(d.model);
    setDim(d.dim);
    setEndpoint(d.endpoint);
    setTest({ state: "idle" });
    setError(null);
  }

  const dimNum = /^[0-9]{1,4}$/.test(dim.trim()) ? Number(dim.trim()) : NaN;
  const dimOk = Number.isInteger(dimNum) && dimNum >= 1 && dimNum <= 8192;
  const modelOk = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$/.test(model.trim());
  const isCompat = provider === "openai-compatible";
  const endpointOk = !isCompat || /^(https:\/\/\S+|http:\/\/(localhost|127\.0\.0\.1)([:/]\S*)?)$/.test(endpoint.trim());
  const formOk = modelOk && dimOk && endpointOk;

  const fieldProblem = !modelOk && model !== ""
    ? "Model id: letters, digits and . _ : / - only."
    : !dimOk && dim !== ""
      ? "Dimensions: a whole number from 1 to 8192."
      : !endpointOk && endpoint !== ""
        ? "Endpoint must start with https:// (http:// only for localhost or 127.0.0.1)."
        : null;

  /** Store a typed key (Keychain, over stdin) then make one tiny embedding call. */
  async function runTest(): Promise<KeyTest> {
    setTest({ state: "testing" });
    setError(null);
    if (key.trim()) {
      try {
        await invoke("set_khipu_secret", { account: KEY_ACCOUNT[provider], value: key.trim() });
      } catch (e) {
        const t: KeyTest = { state: "error", message: `Could not save the key: ${errText(e)}` };
        setTest(t);
        return t;
      }
    }
    const res = await callCli<{ ok: boolean; ms: number; dim: number }>("khipu_embed_test_key", {
      provider,
      endpoint: isCompat ? endpoint.trim() : null,
      model: model.trim(),
    });
    const t: KeyTest = res.ok
      ? { state: "ok", ms: res.data.ms, dim: res.data.dim }
      : { state: "error", message: res.error };
    setTest(t);
    if (t.state === "ok" && !dim.trim()) setDim(String(t.dim));
    return t;
  }

  async function add() {
    setBusy(true);
    setError(null);
    let t: KeyTest = test;
    if (t.state !== "ok") t = await runTest();
    if (t.state !== "ok") {
      setBusy(false);
      return;
    }
    if (dimOk && t.dim !== dimNum) {
      setError(`This model returns ${t.dim} dimensions, not ${dimNum}. Change Dimensions to ${t.dim}.`);
      setBusy(false);
      return;
    }
    const res = await callCli("khipu_embed_profile_add", {
      id: `${model.trim()}@${dimNum}`,
      provider,
      model: model.trim(),
      dim: dimNum,
      endpoint: isCompat ? endpoint.trim() : null,
    });
    if (!res.ok) {
      setError(res.error);
      setBusy(false);
      return;
    }
    setKey("");
    setBusy(false);
    await onAdded();
  }

  return (
    <Dialog open={open} className="kit-dialog emb-dialog" ariaLabelledBy="add-model-title" onCancel={onClose} initialFocusRef={firstRef}>
      <div className="kit-dialog-body">
        <h2 id="add-model-title">Add a model</h2>
        <div className="field-grid">
          <div className="set-field">
            <label className="set-label" htmlFor="am-provider">
              Provider
            </label>
            <select id="am-provider" ref={firstRef} value={provider} onChange={(e) => changeProvider(e.target.value)}>
              <option value="gemini">Gemini</option>
              <option value="voyage">Voyage</option>
              <option value="openai-compatible">OpenAI-compatible</option>
            </select>
          </div>
          <div className="two">
            <div className="set-field">
              <label className="set-label" htmlFor="am-model">
                Model id
              </label>
              <input
                id="am-model"
                className="mono"
                value={model}
                spellCheck={false}
                onChange={(e) => {
                  setModel(e.target.value);
                  setTest({ state: "idle" });
                }}
              />
            </div>
            <div className="set-field">
              <label className="set-label" htmlFor="am-dim">
                Dimensions
              </label>
              <input
                id="am-dim"
                className="mono"
                inputMode="numeric"
                value={dim}
                aria-describedby="am-dim-help"
                onChange={(e) => setDim(e.target.value)}
              />
            </div>
          </div>
          <p id="am-dim-help" className="set-help" style={{ marginTop: "calc(var(--s2) * -1)" }}>
            Dimensions: ask your provider; wrong values are refused.
          </p>
          {isCompat ? (
            <div className="set-field">
              <label className="set-label" htmlFor="am-endpoint">
                Endpoint
              </label>
              <input
                id="am-endpoint"
                className="mono"
                value={endpoint}
                spellCheck={false}
                onChange={(e) => {
                  setEndpoint(e.target.value);
                  setTest({ state: "idle" });
                }}
              />
            </div>
          ) : null}
          <div className="set-field">
            <label className="set-label" htmlFor="am-key">
              Key
            </label>
            <div className="test-line">
              <input
                id="am-key"
                type="password"
                value={key}
                autoComplete="new-password"
                aria-describedby="am-key-help"
                style={{ flex: 1 }}
                onChange={(e) => {
                  setKey(e.target.value);
                  setTest({ state: "idle" });
                }}
              />
              <button type="button" disabled={!modelOk || !endpointOk || test.state === "testing" || busy} onClick={() => void runTest()}>
                {test.state === "testing" ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                Test key
              </button>
              {test.state === "ok" ? (
                <Tag tone="ok" dot>
                  ok, {test.ms} ms
                </Tag>
              ) : null}
            </div>
            <p id="am-key-help" className="set-help">
              {isCompat ? "Optional for a local server. " : "Leave empty to test the key already saved. "}
              Stored in the Keychain and never shown again. Test key saves a typed key first, then makes one tiny call.
            </p>
          </div>
        </div>
        {fieldProblem ? (
          <p className="set-error" role="alert">
            {fieldProblem}
          </p>
        ) : null}
        {test.state === "error" ? (
          <p className="set-error" role="alert">
            {test.message}
          </p>
        ) : null}
        {error ? (
          <p className="set-error" role="alert">
            {error}
          </p>
        ) : null}
        <div className="kit-dialog-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="primary" disabled={!formOk || busy} onClick={() => void add()}>
            {busy ? <Loader2 size={14} className="spin" aria-hidden /> : null}
            Add model
          </button>
        </div>
      </div>
    </Dialog>
  );
}

/** For tests: the pure helpers worth pinning. */
export const __test = { refLabel, suggestName, durationPhrase, tokensPhrase, showsReceipt };
