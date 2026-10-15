/* The summariser ("synth" role) in Settings: which provider writes session
   summaries, its model id, whether its key (or its CLI) is there, and the
   daily call ceiling with a lift for today
   (docs/plans/2026-10-13-synth-providers.md, phase 3). The provider list and
   default model ids mirror `khipu/models.py` (`PROVIDERS`,
   `DEFAULT_SYNTH_MODELS`); the CLI validates again on save, so what is
   stored is always what the CLI accepted. */

import { useId, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Loader2 } from "lucide-react";
import { Tag } from "./ui";
import { callCli, useCliState } from "./settingsCli";
import { SettingField } from "./SettingsControls";
import { keyInfo, keyStatus, type KeyId } from "./KeysCard";
import { fmtClock } from "./time";
import type { ModelVerifyResult } from "./modelVerify";
import "./CaptureModels.css";

export type SynthRoute = "key" | "login" | "local";

export type SynthProviderInfo = {
  id: string;
  label: string;
  /** Short name, kind and one line for the provider rows in Settings. */
  name: string;
  kind?: string;
  line: string;
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
  { id: "gemini", label: "Gemini Flash (API key)", name: "Gemini Flash", kind: "API key", line: "Fastest, about 3 s, least accurate", route: "key", account: "gemini_api_key", secretPrefix: "gemini", defaultModel: "gemini-2.5-flash" },
  { id: "anthropic", label: "Claude Haiku (API key)", name: "Claude Haiku", kind: "API key", line: "Best memory, about 11 s a capture", route: "key", account: "anthropic_api_key", secretPrefix: "anthropic", defaultModel: "claude-haiku-5-5" },
  { id: "openai", label: "OpenAI (API key)", name: "OpenAI", kind: "API key", line: "Accurate, thinner on open loops, about 8 s", route: "key", account: "openai_api_key", secretPrefix: "openai", exampleModel: "gpt-6-luna" },
  { id: "openrouter", label: "OpenRouter (one key, any model)", name: "OpenRouter", kind: "API key", line: "One key, any model", route: "key", account: "openrouter_api_key", secretPrefix: "openrouter", exampleModel: "openai/gpt-6-luna" },
  { id: "claude-cli", label: "Claude on my own login (claude -p)", name: "Claude", kind: "your login", line: "Uses the login you already have; counts against that plan", route: "login", defaultModel: "haiku", binary: "claude" },
  { id: "codex-cli", label: "ChatGPT on my own login (codex)", name: "ChatGPT", kind: "your login", line: "Uses the login you already have; counts against that plan", route: "login", exampleModel: "gpt-6-luna", binary: "codex" },
  { id: "local", label: "local", name: "A local model", line: "A model on this Mac or your network", route: "local" },
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

const PROVIDER_KEY: Record<string, KeyId> = {
  gemini: "gemini",
  anthropic: "anthropic",
  openai: "openai",
  openrouter: "openrouter",
  local: "openai_compat",
};

/** Scroll to a key's row in Keys below and put focus on it. */
function showKeyRow(id: KeyId) {
  const el = document.getElementById(`key-row-${id}`);
  if (!el) return;
  el.scrollIntoView?.({ block: "center" });
  el.focus();
}

type TagView = { tone: "neutral" | "ok" | "warn" | "err"; dot: boolean; text: string };

/** The status words on a provider row. Key routes follow the key's status
 *  in Keys below; login routes follow the health report; local follows the
 *  endpoint check. */
function providerTag(
  info: SynthProviderInfo,
  presence: SynthSecretsPresence | null,
  verifyResult: ModelVerifyResult | null,
  doctorSynth: DoctorSynthProvider | null,
): TagView {
  const notChecked: TagView = { tone: "neutral", dot: false, text: "Not checked" };
  if (info.route === "login") {
    if (doctorSynth == null || canonicalSynthProvider(doctorSynth.provider ?? "") !== info.id) return notChecked;
    return doctorSynth.binary
      ? { tone: "ok", dot: true, text: "Found on this Mac" }
      : { tone: "err", dot: true, text: "Not found on this Mac" };
  }
  if (info.route === "local") {
    const st = keyStatus(presence, verifyResult, "openai_compat");
    if (st.check === "ok") return { tone: "ok", dot: true, text: "Works" };
    if (st.check === "failed") return { tone: "err", dot: true, text: "Check failed" };
    return notChecked;
  }
  const keyId = PROVIDER_KEY[info.id];
  const st = keyStatus(presence, verifyResult, keyId);
  if (st.source === "unknown") return notChecked;
  if (st.source === "none") return { tone: "neutral", dot: false, text: "No key" };
  if (st.check === "ok") return { tone: "ok", dot: true, text: "Key works" };
  if (st.check === "failed") return { tone: "err", dot: true, text: "Key check failed" };
  return keyInfo(keyId).checks.length ? notChecked : { tone: "neutral", dot: false, text: "Key stored" };
}

function KeyLine({
  info,
  presence,
  verifyResult,
  doctorSynth,
}: {
  info: SynthProviderInfo;
  presence: SynthSecretsPresence | null;
  verifyResult: ModelVerifyResult | null;
  doctorSynth: DoctorSynthProvider | null;
}) {
  if (info.route === "login") {
    const checked = doctorSynth != null && canonicalSynthProvider(doctorSynth.provider ?? "") === info.id;
    return (
      <p className="choice-key">
        <span>
          Runs the <code>{info.binary}</code> you are already signed into; no key is stored.
          {checked ? "" : " Save models, then the health report checks it is on this Mac."}
        </span>
      </p>
    );
  }
  if (info.route === "local") {
    return (
      <p className="choice-key">
        <span>
          If the endpoint asks for a key, add it as Local endpoint in Keys below. Save models, then use Check now
          there.
        </span>
      </p>
    );
  }
  const keyId = PROVIDER_KEY[info.id];
  const k = keyInfo(keyId);
  const st = keyStatus(presence, verifyResult, keyId);
  const checkable = k.checks.length > 0;
  let phrase: string;
  if (st.source === "unknown") phrase = "not read yet";
  else if (st.source === "none") phrase = "not stored";
  else if (st.check === "ok") phrase = "works";
  else if (st.check === "failed") phrase = "the check failed";
  else if (st.source === "env") phrase = "set in the environment";
  else phrase = checkable ? "stored, not checked yet" : "stored";
  const link =
    st.source === "none"
      ? "Add it in Keys below"
      : st.check === "failed" || (checkable && st.check == null && st.source !== "unknown")
        ? "Check it in Keys below"
        : "Show it in Keys below";
  return (
    <p className="choice-key">
      <span>
        Uses the {k.name} key: {phrase}.
      </span>
      <button type="button" className="link sm" onClick={() => showKeyRow(keyId)}>
        {link}
      </button>
    </p>
  );
}

function ChoiceBody({
  info,
  providerId,
  row,
  onChange,
  presence,
  verifyResult,
  doctorSynth,
}: {
  info: SynthProviderInfo | undefined;
  providerId: string;
  row: SynthRow;
  onChange: (patch: Partial<SynthRow>) => void;
  presence: SynthSecretsPresence | null;
  verifyResult: ModelVerifyResult | null;
  doctorSynth: DoctorSynthProvider | null;
}) {
  const endpointId = useId();
  const modelId = useId();
  const modelError = synthModelError(providerId, row.model_id);
  const help = info?.defaultModel
    ? `Default ${info.defaultModel}.`
    : info?.exampleModel
      ? `Required, for example ${info.exampleModel}.`
      : undefined;
  return (
    <div className="choice-body">
      {providerId === "local" ? (
        <div className="set-field">
          <div className="set-labelrow">
            <label className="set-label" htmlFor={endpointId}>
              Endpoint
            </label>
          </div>
          <div className="toolbar">
            <input
              id={endpointId}
              className="mono"
              value={row.endpoint}
              aria-describedby={`${endpointId}-help`}
              onChange={(e) => onChange({ endpoint: e.target.value })}
              placeholder="http://127.0.0.1:11434"
            />
          </div>
          <p id={`${endpointId}-help`} className="set-help">
            Any OpenAI-compatible address, such as Ollama on this Mac.
          </p>
        </div>
      ) : null}
      <div className="set-field">
        <div className="set-labelrow">
          <label className="set-label" htmlFor={modelId}>
            Model id
          </label>
        </div>
        <div className="toolbar">
          <input
            id={modelId}
            className="mono"
            value={row.model_id}
            aria-invalid={modelError ? true : undefined}
            aria-describedby={help ? `${modelId}-help` : undefined}
            onChange={(e) => onChange({ model_id: e.target.value })}
            placeholder={info?.exampleModel ?? info?.defaultModel ?? "model id"}
          />
        </div>
        {help ? (
          <p id={`${modelId}-help`} className="set-help">
            {help}
          </p>
        ) : null}
      </div>
      {modelError ? (
        <p className="set-error" role="alert">
          {modelError}
        </p>
      ) : null}
      {info ? (
        <KeyLine info={info} presence={presence} verifyResult={verifyResult} doctorSynth={doctorSynth} />
      ) : null}
    </div>
  );
}

/** The provider as a list of choices, one row each. Only the chosen row
 *  opens, and shows what that provider needs. Changing the provider prefills
 *  its default model id. */
export function SynthProviderChoices({
  row,
  onChange,
  presence,
  verifyResult,
  doctorSynth,
}: {
  row: SynthRow;
  onChange: (patch: Partial<SynthRow>) => void;
  presence: SynthSecretsPresence | null;
  verifyResult: ModelVerifyResult | null;
  doctorSynth: DoctorSynthProvider | null;
}) {
  const provider = canonicalSynthProvider(row.provider);
  const known = synthProviderInfo(provider);
  const legendId = useId();
  const body = (info: SynthProviderInfo | undefined) => (
    <ChoiceBody
      info={info}
      providerId={provider}
      row={row}
      onChange={onChange}
      presence={presence}
      verifyResult={verifyResult}
      doctorSynth={doctorSynth}
    />
  );
  return (
    <div className="choices" role="radiogroup" aria-labelledby={legendId} aria-describedby="synth-help">
      <span className="sr-only" id={legendId}>
        Which model writes the summaries
      </span>
      {known ? null : (
        <div className="choice on">
          <label className="choice-head">
            <input type="radio" name="synth-provider" value={provider} checked readOnly />
            <span className="choice-text">
              <span className="choice-name">{provider}</span>
              <span className="choice-line">Not a provider this version of Khipu lists</span>
            </span>
            <Tag tone="neutral">Not checked</Tag>
          </label>
          {body(undefined)}
        </div>
      )}
      {SYNTH_PROVIDERS.map((p) => {
        const on = p.id === provider;
        const tag = providerTag(p, presence, verifyResult, doctorSynth);
        return (
          <div key={p.id} className={on ? "choice on" : "choice"}>
            <label className="choice-head">
              <input
                type="radio"
                name="synth-provider"
                value={p.id}
                checked={on}
                onChange={() => onChange({ provider: p.id, model_id: defaultSynthModel(p.id) })}
              />
              <span className="choice-text">
                <span className="choice-name">
                  {p.name}
                  {p.kind ? (
                    <>
                      {" "}
                      <span className="meta">{p.kind}</span>
                    </>
                  ) : null}
                </span>
                <span className="choice-line">{p.line}</span>
              </span>
              <Tag tone={tag.tone} dot={tag.dot}>
                {tag.text}
              </Tag>
            </label>
            {on ? body(p) : null}
          </div>
        );
      })}
    </div>
  );
}

/** "If the provider fails": retry the window on Gemini Flash, or leave it
 *  queued. Saved straight away with `config --set synth_fallback`. */
export function SynthFallbackField({
  provider,
  doctorSynth,
  presence = null,
  verifyResult = null,
  onSaved,
}: {
  provider: string;
  doctorSynth: DoctorSynthProvider | null;
  presence?: SynthSecretsPresence | null;
  verifyResult?: ModelVerifyResult | null;
  onSaved?: () => Promise<void> | void;
}) {
  const stored = synthFallbackSetting(doctorSynth);
  const [pending, setPending] = useState<SynthFallback | null>(null);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const isGemini = canonicalSynthProvider(provider) === "gemini";
  const value = pending ?? stored;

  async function save(next: SynthFallback) {
    if (next === value) return;
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
  const refused = keyStatus(presence, verifyResult, "gemini").check === "failed";
  const off = isGemini || saving;
  const reading = pending == null && doctorSynth?.fallback == null;
  if (reading) {
    return (
      <fieldset className="set-field">
        <legend className="set-label">If the provider fails</legend>
        <p className="set-help">Reading…</p>
      </fieldset>
    );
  }
  return (
    <fieldset className="set-field">
      <legend className="set-label">If the provider fails</legend>
      <div className="radio-stack">
        <label className="radio-choice">
          <input
            type="radio"
            name="synth-fallback"
            value="gemini"
            checked={value === "gemini"}
            disabled={off}
            onChange={() => void save("gemini")}
          />
          Retry on Gemini Flash
        </label>
        <label className="radio-choice">
          <input
            type="radio"
            name="synth-fallback"
            value="none"
            checked={value === "none"}
            disabled={off}
            onChange={() => void save("none")}
          />
          Wait and retry later on the same provider
        </label>
      </div>
      {isGemini ? (
        <p className="set-help">Gemini is the provider; there is nothing to fall back to.</p>
      ) : value === "gemini" ? (
        <p className="set-help">
          Fallback uses the Gemini key, about $0.002 a conversation. Nothing is dropped either way. Saved when you
          choose.
          {refused ? " While that key is refused, a failed capture waits and is retried; nothing is dropped." : ""}
        </p>
      ) : (
        <p className="set-help">
          Nothing is dropped: a failed capture waits and is retried. Saved when you choose.
        </p>
      )}
      {noKey ? (
        <p className="set-help">
          <Tag tone="warn" dot>
            No Gemini key
          </Tag>{" "}
          Add a Gemini key in Keys below, or the fallback cannot run.
        </p>
      ) : null}
      {err ? (
        <p className="set-error" role="alert">
          {err}
        </p>
      ) : null}
    </fieldset>
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
  const plural = (n: number | null | undefined) => (n === 1 ? "call" : "calls");
  if (unlimited) return `${calls} ${plural(b.calls)} today, no ceiling${source ? ` (${source})` : ""}`;
  const cap = typeof b.cap === "number" ? b.cap.toLocaleString("en-US") : "?";
  return `${calls} of ${cap} ${plural(b.cap)} today${source ? ` (${source})` : ""}`;
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

function nextUtcMidnightHere(): string {
  const n = new Date();
  return fmtClock(new Date(Date.UTC(n.getUTCFullYear(), n.getUTCMonth(), n.getUTCDate() + 1)));
}

function fmtCount(n: number | null | undefined): string {
  return typeof n === "number" ? n.toLocaleString("en-US") : "?";
}

/** "Calls today: 12 of 5,000, about $0.03", with Change… for the daily limit
 *  and Lift for today. Lives inside the Session summaries card. */
export function SummariserCalls({ active }: { active: boolean }) {
  const st = useCliState<{ synth?: SynthBudget }>("khipu_budget_status", active);
  const b = st.data?.synth;
  const [lifting, setLifting] = useState(false);
  const [liftMsg, setLiftMsg] = useState<string | null>(null);
  const [liftErr, setLiftErr] = useState<string | null>(null);
  const [changing, setChanging] = useState(false);
  const limitId = useId();

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

  const midnight = nextUtcMidnightHere();
  const unlimited = b != null && (b.unlimited === true || b.cap === 0);
  const exhausted = b?.exhausted === true && !unlimited;
  const cost = b && typeof b.estimated_usd_today === "number" ? formatUsd(b.estimated_usd_today) : null;
  const source = b?.cap_source ? CAP_SOURCE[b.cap_source] ?? b.cap_source : null;
  const by = b?.usage_by_provider ? Object.entries(b.usage_by_provider) : [];
  const byText = by
    .map(([id, u]) => `${synthProviderInfo(id)?.name ?? id} ${fmtCount(u?.calls ?? 0)}`)
    .join(", ");

  let main: string;
  let sub: string | null = null;
  if (b) {
    if (exhausted) {
      main = `Calls today: ${fmtCount(b.calls)} of ${fmtCount(b.cap)}. Captures wait until midnight UTC; nothing is dropped.`;
      sub = `Midnight UTC is ${midnight} here.${cost ? ` About ${cost} today.` : ""}`;
    } else {
      main = unlimited
        ? `Calls today: ${fmtCount(b.calls)}, no daily limit${cost ? `, about ${cost}` : ""}`
        : `Calls today: ${fmtCount(b.calls)} of ${fmtCount(b.cap)}${cost ? `, about ${cost}` : ""}`;
      const limit = unlimited
        ? `No daily limit${source ? ` (${source})` : ""}.`
        : `Daily limit ${fmtCount(b.cap)}${source ? ` (${source})` : ""}; 0 means no limit.`;
      sub = `${byText ? `By provider: ${byText}. ` : ""}${limit} It starts again at midnight UTC, ${midnight} here.`;
    }
  } else if (st.error) {
    main = `Can't read today's calls: ${st.error}`;
  } else {
    main = "Calls today: Reading…";
  }

  return (
    <>
      <div className={exhausted ? "calls warn" : "calls"} role="group" aria-label="Calls today">
        <span className="calls-text">
          <span className="calls-main">{main}</span>
          {sub ? <span className="calls-sub">{sub}</span> : null}
        </span>
          <span className="calls-acts">
            <button
              type="button"
              className="sm"
              aria-label="Change the daily limit"
              aria-expanded={changing}
              aria-controls={limitId}
              onClick={() => setChanging((c) => !c)}
            >
              Change…
            </button>
            <button
              type="button"
              className={exhausted ? "sm primary" : "sm"}
              disabled={lifting}
              onClick={() => void liftToday()}
            >
              {lifting ? <Loader2 size={12} className="spin" aria-hidden /> : null}
              Lift for today
            </button>
          </span>
      </div>
      {exhausted ? (
        <p className="set-help">
          Lift for today starts today's count again, so the full limit is there until midnight UTC.
        </p>
      ) : null}
      {liftMsg ? <p className="set-help">{liftMsg}</p> : null}
      {liftErr ? (
        <p className="set-error" role="alert">
          {liftErr}
        </p>
      ) : null}
      {changing ? (
        <div id={limitId}>
          <SettingField
            label="Daily limit"
            mono
            narrow
            inputMode="decimal"
            value={b && typeof b.cap === "number" ? String(b.cap) : ""}
            help="0 means no limit. When the limit is reached, captures wait until midnight UTC; nothing is dropped."
            onSave={async (v) => {
              if (!/^\s*\d+\s*$/.test(v)) return "Enter a whole number, 0 or more.";
              const res = await callCli("khipu_config_set", { key: "synth_daily_calls", value: v.trim() });
              await st.reload();
              return res.ok ? null : res.error;
            }}
          />
        </div>
      ) : null}
    </>
  );
}
