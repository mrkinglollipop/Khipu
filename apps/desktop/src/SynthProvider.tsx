/* The summariser ("synth" role) in Settings: which provider writes session
   summaries, its model id, whether its key (or its CLI) is there, and the
   daily call ceiling with a lift for today
   (docs/plans/2026-10-13-synth-providers.md, phase 3). The provider list and
   default model ids mirror `khipu/models.py` (`PROVIDERS`,
   `DEFAULT_SYNTH_MODELS`); the CLI validates again on save, so what is
   stored is always what the CLI accepted. */

import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Loader2 } from "lucide-react";
import { Tag } from "./ui";
import { callCli, useCliState } from "./settingsCli";
import { SettingField } from "./SettingsControls";

export type SynthRoute = "key" | "login" | "local";

export type SynthProviderInfo = {
  id: string;
  label: string;
  route: SynthRoute;
  /** Keychain account `khipu secrets --set` writes, for the key routes. */
  account?: string;
  /** Prefix of the `khipu secrets` status fields (`<prefix>_in_keychain`). */
  secretPrefix?: string;
  /** Model id the CLI uses when none is given. */
  defaultModel?: string;
  /** No default: the model id is required, and this is the example shown. */
  exampleModel?: string;
  /** The CLI a login route runs. */
  binary?: "claude" | "codex";
};

export const SYNTH_PROVIDERS: readonly SynthProviderInfo[] = [
  { id: "gemini", label: "Gemini Flash (API key)", route: "key", account: "gemini_api_key", secretPrefix: "gemini", defaultModel: "gemini-2.5-flash" },
  { id: "anthropic", label: "Claude Haiku (API key)", route: "key", account: "anthropic_api_key", secretPrefix: "anthropic", defaultModel: "claude-haiku-5-5" },
  { id: "openai", label: "OpenAI (API key)", route: "key", account: "openai_api_key", secretPrefix: "openai", exampleModel: "gpt-6-luna" },
  { id: "openrouter", label: "OpenRouter (one key, any model)", route: "key", account: "openrouter_api_key", secretPrefix: "openrouter", exampleModel: "openai/gpt-6-luna" },
  { id: "claude-cli", label: "Claude on my own login (claude -p)", route: "login", defaultModel: "haiku", binary: "claude" },
  { id: "codex-cli", label: "ChatGPT on my own login (codex)", route: "login", exampleModel: "gpt-6-luna", binary: "codex" },
  { id: "local", label: "local", route: "local" },
];

/** Older configs (and the CLI's alias) say `cloud` for Gemini. */
export function canonicalSynthProvider(provider: string): string {
  const p = (provider || "").trim().toLowerCase();
  return p === "cloud" || p === "" ? "gemini" : p;
}

export function synthProviderInfo(provider: string): SynthProviderInfo | undefined {
  const id = canonicalSynthProvider(provider);
  return SYNTH_PROVIDERS.find((p) => p.id === id);
}

/** The model id prefilled when the provider changes: the CLI's default, or
 *  empty where the CLI has none (the field then shows the example). */
export function defaultSynthModel(provider: string): string {
  return synthProviderInfo(provider)?.defaultModel ?? "";
}

/** Refuse a save the CLI would refuse: a provider with no default model id
 *  needs one typed in. Null when the row can be saved. */
export function synthModelError(provider: string, modelId: string): string | null {
  const info = synthProviderInfo(provider);
  if (!info || info.route === "local" || info.defaultModel) return null;
  if (modelId.trim()) return null;
  return `${info.label} needs a model id, for example ${info.exampleModel}.`;
}

export type SynthSecretsPresence = Partial<Record<string, boolean>>;

/** Where a provider's key was found, from the `khipu secrets` fields; null
 *  when presence is unknown. */
export function synthKeySources(
  presence: SynthSecretsPresence | null,
  provider: string,
): string[] | null {
  const info = synthProviderInfo(provider);
  if (!presence || !info?.secretPrefix) return null;
  const p = info.secretPrefix;
  const out: string[] = [];
  if (presence[`${p}_in_keychain`]) out.push("Keychain");
  if (presence[`${p}_env`]) out.push(`environment (${p.toUpperCase()}_API_KEY)`);
  if (presence[`${p}_file_key_present`]) out.push("key file");
  return out;
}

/** Doctor's `synth_provider` block (packages/cli/khipu/cli.py). */
export type DoctorSynthProvider = {
  provider?: string;
  model?: string;
  key_present?: boolean | null;
  auth?: string;
  binary?: string | null;
  /** Gemini Flash retry for a window the provider failed (`synth_fallback`).
   *  Present even when the provider is Gemini, where it means nothing. */
  fallback?: SynthFallbackStatus;
};

export type SynthFallback = "gemini" | "none";

export type SynthFallbackStatus = {
  setting?: SynthFallback;
  source?: "config" | "default";
  /** A Gemini key resolves (Keychain, env or key file), so the retry can run. */
  gemini_key?: boolean;
};

/** The stored fallback, Gemini when doctor does not say. */
export function synthFallbackSetting(doctorSynth: DoctorSynthProvider | null | undefined): SynthFallback {
  return doctorSynth?.fallback?.setting === "none" ? "none" : "gemini";
}

/** Health-report words for the fallback, or null where it does not apply
 *  (provider is Gemini, or doctor gave no fallback block). */
export function synthFallbackLine(sp: DoctorSynthProvider | null | undefined): string | null {
  if (!sp?.fallback || canonicalSynthProvider(sp.provider ?? "") === "gemini") return null;
  if (synthFallbackSetting(sp) === "none") return "Fallback: off";
  return sp.fallback.gemini_key === false ? "Fallback: Gemini Flash, no key" : "Fallback: Gemini Flash (key present)";
}

export function SynthQualityNote() {
  return (
    <div>
      <p className="set-help">
        Claude Haiku 5.5: the best memory, first in 10 of 15 judged windows, 3
        wrong or invented items, about 11 s a capture.
      </p>
      <p className="set-help">
        GPT-6 Luna: accurate where it answers (2 wrong items) but thin on open
        loops; about 8 s a capture at low effort, 50 s at high.
      </p>
      <p className="set-help">
        Gemini 2.5 Flash: the fastest, about 3 s, and last on quality: no first
        places, about 10 wrong or invented items.
      </p>
      <p className="set-help">
        Measured 2026-10-13 on 15 real windows. Gemini stays the embedder for
        search by meaning whichever summariser you pick.
      </p>
    </div>
  );
}

function parseJson(raw: unknown): Record<string, unknown> | null {
  if (typeof raw !== "string") return null;
  try {
    const v = JSON.parse(raw);
    return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** Key present / absent and "Set key" for the API routes; CLI found / not
 *  found and the subscription note for the two login routes. */
export function SynthAccess({
  provider,
  presence,
  doctorSynth,
  onKeySaved,
}: {
  provider: string;
  presence: SynthSecretsPresence | null;
  doctorSynth: DoctorSynthProvider | null;
  onKeySaved?: () => Promise<void> | void;
}) {
  const info = synthProviderInfo(provider);
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  if (!info || info.route === "local") return null;

  if (info.route === "login") {
    const checked = doctorSynth != null && canonicalSynthProvider(doctorSynth.provider ?? "") === info.id;
    return (
      <div className="set-field">
        <div className="inline">
          {checked ? (
            doctorSynth?.binary ? (
              <Tag tone="ok" dot>
                {info.binary} found
              </Tag>
            ) : (
              <Tag tone="err" dot>
                {info.binary} not found
              </Tag>
            )
          ) : (
            <Tag tone="neutral">Not checked</Tag>
          )}
          {checked && doctorSynth?.binary ? <span className="meta mono">{doctorSynth.binary}</span> : null}
        </div>
        <p className="set-help">
          Runs the <code>{info.binary}</code> you are already signed into; no key is stored. Counts
          against that plan's limits.
        </p>
        {!checked ? (
          <p className="set-help">Save, then recheck the health report to see whether it is found on this Mac.</p>
        ) : null}
      </div>
    );
  }

  const sources = synthKeySources(presence, info.id);
  const account = info.account as string;

  async function save() {
    const v = value.trim();
    if (!v || saving) return;
    setSaving(true);
    setMsg(null);
    try {
      // Same path as the Gemini key: the value goes to the CLI's stdin
      // (`khipu secrets --set <account>`), never into argv.
      const out = parseJson(await invoke<string>("set_khipu_secret", { account, value: v }));
      if (out?.ok) {
        setValue("");
        setMsg("Saved to Keychain.");
        await onKeySaved?.();
      } else {
        setMsg(String(out?.error ?? "Could not save the key."));
      }
    } catch (e) {
      setMsg(String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="set-field">
      <div className="inline">
        {sources == null ? (
          <Tag tone="neutral">Key not checked</Tag>
        ) : sources.length ? (
          <Tag tone="ok" dot>
            Key present
          </Tag>
        ) : (
          <Tag tone="warn" dot>
            No key
          </Tag>
        )}
        {sources && sources.length ? <span className="meta">{sources.join(", ")}</span> : null}
      </div>
      {info.id === "gemini" ? (
        <p className="set-help">The Gemini key is set under Keys below; the same key serves the search index.</p>
      ) : (
        <>
          <div className="toolbar">
            <input
              className="mono"
              type="password"
              autoComplete="off"
              spellCheck={false}
              aria-label={`${info.label} key`}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void save();
              }}
              placeholder={sources && sources.length ? "A key is stored — type a new one to replace it" : "Paste the key"}
            />
            <button type="button" disabled={saving || !value.trim()} onClick={() => void save()}>
              {saving ? "Saving…" : "Set key"}
            </button>
          </div>
          <p className="set-help">Stored in the login Keychain; never written to the repo or a config file.</p>
        </>
      )}
      {msg ? <pre className="code">{msg}</pre> : null}
    </div>
  );
}

export type SynthRow = { provider: string; endpoint: string; model_id: string };

/** Provider select, endpoint (local only), model id, the quality note and the
 *  key / login status. Changing the provider prefills its default model id. */
export function SynthProviderFields({
  row,
  onChange,
  presence,
  doctorSynth,
  onKeySaved,
}: {
  row: SynthRow;
  onChange: (patch: Partial<SynthRow>) => void;
  presence: SynthSecretsPresence | null;
  doctorSynth: DoctorSynthProvider | null;
  onKeySaved?: () => Promise<void> | void;
}) {
  const provider = canonicalSynthProvider(row.provider);
  const info = synthProviderInfo(provider);
  const modelError = synthModelError(provider, row.model_id);
  return (
    <div className="rows" style={{ marginBottom: 12 }}>
      <div className="rows-head">Session summaries</div>
      <div className="toolbar">
        <label className="muted" htmlFor="models-synth-provider">
          Provider
        </label>
        <select
          id="models-synth-provider"
          value={provider}
          onChange={(e) =>
            onChange({ provider: e.target.value, model_id: defaultSynthModel(e.target.value) })
          }
        >
          {info ? null : <option value={provider}>{provider}</option>}
          {SYNTH_PROVIDERS.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </select>
      </div>
      <SynthQualityNote />
      <div className="toolbar">
        <input
          className="mono"
          disabled={provider !== "local"}
          value={row.endpoint}
          aria-label="Summariser endpoint"
          onChange={(e) => onChange({ endpoint: e.target.value })}
          placeholder="http://127.0.0.1:11434"
        />
        <input
          className="mono"
          value={row.model_id}
          aria-label="Summariser model id"
          aria-invalid={modelError ? true : undefined}
          onChange={(e) => onChange({ model_id: e.target.value })}
          placeholder={info?.exampleModel ?? info?.defaultModel ?? "model id"}
        />
      </div>
      {modelError ? (
        <p className="set-error" role="alert">
          {modelError}
        </p>
      ) : null}
      <SynthAccess
        key={provider}
        provider={provider}
        presence={presence}
        doctorSynth={doctorSynth}
        onKeySaved={onKeySaved}
      />
      <SynthFallbackField provider={provider} doctorSynth={doctorSynth} onSaved={onKeySaved} />
    </div>
  );
}

/** "If the provider fails": retry the window on Gemini Flash, or leave it
 *  queued. Saved straight away with `config --set synth_fallback`. */
export function SynthFallbackField({
  provider,
  doctorSynth,
  onSaved,
}: {
  provider: string;
  doctorSynth: DoctorSynthProvider | null;
  onSaved?: () => Promise<void> | void;
}) {
  const stored = synthFallbackSetting(doctorSynth);
  const [pending, setPending] = useState<SynthFallback | null>(null);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const isGemini = canonicalSynthProvider(provider) === "gemini";
  const value = pending ?? stored;

  async function save(next: SynthFallback) {
    setPending(next);
    setSaving(true);
    setErr(null);
    const res = await callCli("khipu_config_set", { key: "synth_fallback", value: next });
    if (res.ok) {
      await onSaved?.();
    } else {
      setErr(res.error);
      setPending(null);
    }
    setSaving(false);
  }

  const noKey =
    !isGemini && value === "gemini" && doctorSynth?.fallback != null && doctorSynth.fallback.gemini_key === false;

  return (
    <div className="set-field">
      <div className="toolbar">
        <label className="muted" htmlFor="models-synth-fallback">
          If the provider fails
        </label>
        <select
          id="models-synth-fallback"
          value={value}
          disabled={isGemini || saving}
          onChange={(e) => void save(e.target.value as SynthFallback)}
        >
          <option value="gemini">Retry that window on Gemini Flash</option>
          <option value="none">Do not fall back; the capture waits and is retried later</option>
        </select>
        {noKey ? (
          <Tag tone="warn" dot>
            No Gemini key
          </Tag>
        ) : null}
      </div>
      {isGemini ? (
        <p className="set-help">Gemini is the provider; there is nothing to fall back to.</p>
      ) : (
        <p className="set-help">
          Fallback calls are billed to the Gemini key, about $0.002 per window, and count under By provider
          as gemini. Nothing is ever dropped either way: with no fallback the capture waits in the queue and is
          retried.
        </p>
      )}
      {noKey ? (
        <p className="set-help">
          Set a Gemini key (choose Gemini Flash above to see its key field) or the fallback cannot run.
        </p>
      ) : null}
      {err ? (
        <p className="set-error" role="alert">
          {err}
        </p>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Today's calls, cost and the ceiling. Shared with the health report rows.
// ---------------------------------------------------------------------------

export type ProviderUsage = {
  calls?: number;
  estimated_usd?: number | null;
  billing?: string;
};

export type SynthBudget = {
  calls?: number | null;
  cap?: number | null;
  cap_source?: string;
  unlimited?: boolean;
  exhausted?: boolean;
  estimated_usd_today?: number | null;
  usage_by_provider?: Record<string, ProviderUsage>;
  error?: string;
};

const CAP_SOURCE: Record<string, string> = {
  config: "set in Settings",
  env: "set by KHIPU_SYNTH_DAILY_CALLS",
  default: "default",
};

export function formatUsd(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "unknown";
  return n > 0 && n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

/** "12 of 5,000 calls today (default)", or "12 calls today, no ceiling". */
export function synthCallsLine(b: SynthBudget): string {
  const calls = typeof b.calls === "number" ? b.calls.toLocaleString("en-US") : "?";
  const source = b.cap_source ? CAP_SOURCE[b.cap_source] ?? b.cap_source : null;
  const unlimited = b.unlimited === true || b.cap === 0;
  if (unlimited) return `${calls} calls today, no ceiling${source ? ` (${source})` : ""}`;
  const cap = typeof b.cap === "number" ? b.cap.toLocaleString("en-US") : "?";
  return `${calls} of ${cap} calls today${source ? ` (${source})` : ""}`;
}

/** One line per provider used today: calls and cost, or "subscription". */
export function usageByProviderLines(by: Record<string, ProviderUsage> | undefined): string[] {
  if (!by || typeof by !== "object") return [];
  return Object.entries(by).map(([id, u]) => {
    const name = synthProviderInfo(id)?.label ?? id;
    const calls = `${u?.calls ?? 0} call${u?.calls === 1 ? "" : "s"}`;
    const cost = u?.billing === "subscription" ? "subscription" : formatUsd(u?.estimated_usd);
    return `${name}: ${calls}, ${cost}`;
  });
}

export function SummariserBudgetCard({ active }: { active: boolean }) {
  const st = useCliState<{ synth?: SynthBudget }>("khipu_budget_status", active);
  const b = st.data?.synth;
  const [lifting, setLifting] = useState(false);
  const [liftMsg, setLiftMsg] = useState<string | null>(null);
  const [liftErr, setLiftErr] = useState<string | null>(null);

  async function liftToday() {
    setLifting(true);
    setLiftMsg(null);
    setLiftErr(null);
    const res = await callCli<{ did?: string }>("khipu_budget_lift_synth_today");
    if (res.ok) setLiftMsg(res.data.did ? `Done: ${res.data.did}.` : "Done.");
    else setLiftErr(res.error);
    await st.reload();
    setLifting(false);
  }

  const lines = usageByProviderLines(b?.usage_by_provider);
  return (
    <div className="section-card">
      <div className="section-head">Summariser calls per day</div>
      <div className="section-body">
        {st.error && !b ? (
          <p className="set-error" role="alert">
            Could not read the ceiling: {st.error}
          </p>
        ) : null}
        {b ? (
          <div className="rows">
            <div className="row-item">
              <span className="row-main">Today</span>
              <span className="row-meta">{synthCallsLine(b)}</span>
              {b.exhausted ? <Tag tone="warn">Reached</Tag> : null}
            </div>
            <div className="row-item">
              <span className="row-main">Estimated cost today</span>
              <span className="row-meta">{formatUsd(b.estimated_usd_today)}</span>
            </div>
            {lines.map((line) => (
              <div key={line} className="row-item">
                <span className="row-main">By provider</span>
                <span className="row-meta wrap">{line}</span>
              </div>
            ))}
          </div>
        ) : null}
        <SettingField
          label="Daily ceiling"
          mono
          narrow
          inputMode="decimal"
          value={b && typeof b.cap === "number" ? String(b.cap) : ""}
          help="0 means no ceiling. When the ceiling is reached, captures wait in the queue until UTC midnight; nothing is dropped."
          onSave={async (v) => {
            if (!/^\s*\d+\s*$/.test(v)) return "Enter a whole number, 0 or more.";
            const res = await callCli("khipu_config_set", { key: "synth_daily_calls", value: v.trim() });
            await st.reload();
            return res.ok ? null : res.error;
          }}
        />
        <div className="toolbar">
          <button type="button" disabled={lifting} onClick={() => void liftToday()}>
            {lifting ? <Loader2 size={14} className="spin" aria-hidden /> : null}
            Lift for today
          </button>
          <span className="muted">Resets today's count, so the full ceiling is available again until UTC midnight.</span>
        </div>
        {liftMsg ? <p className="set-help">{liftMsg}</p> : null}
        {liftErr ? (
          <p className="set-error" role="alert">
            {liftErr}
          </p>
        ) : null}
      </div>
    </div>
  );
}
