/* "Keys on this Mac": one row per model key, in one place (docs/plans/
   2026-10-15-settings-clarity.md, section 1). The provider rows above only
   show status and point here. A key's value goes to the CLI over stdin
   (`set_khipu_secret`), never into argv, and is cleared once saved. */

import { useEffect, useRef, useState, type ReactNode } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Loader2 } from "lucide-react";
import { modelCheckFor, type ModelVerifyResult } from "./modelVerify";
import { fmtAge } from "./time";
import "./CaptureModels.css";

export type KeyId = "gemini" | "anthropic" | "openai" | "openrouter" | "openai_compat";

export type KeyInfo = {
  id: KeyId;
  name: string;
  /** Keychain account `khipu secrets --set` writes. */
  account: string;
  uses: string;
  env: string;
  /** Ids of the checks in `khipu secrets verify` that exercise this key. */
  checks: readonly string[];
  optional?: boolean;
  /** Where "Add" is spoken: "an OpenAI key", "a local endpoint key". */
  addLabel: string;
};

export const KEYS: readonly KeyInfo[] = [
  {
    id: "gemini",
    name: "Gemini",
    account: "gemini_api_key",
    uses: "Summaries, the fallback and search by meaning",
    env: "GEMINI_API_KEY",
    checks: ["gemini_generate", "gemini_embed"],
    addLabel: "a Gemini key",
  },
  {
    id: "anthropic",
    name: "Claude API",
    account: "anthropic_api_key",
    uses: "Summaries with Claude Haiku",
    env: "ANTHROPIC_API_KEY",
    checks: ["anthropic_models"],
    addLabel: "a Claude API key",
  },
  {
    id: "openai",
    name: "OpenAI",
    account: "openai_api_key",
    uses: "Summaries with OpenAI",
    env: "OPENAI_API_KEY",
    checks: ["openai_models"],
    addLabel: "an OpenAI key",
  },
  {
    id: "openrouter",
    name: "OpenRouter",
    account: "openrouter_api_key",
    uses: "Summaries through OpenRouter",
    env: "OPENROUTER_API_KEY",
    checks: ["openrouter_models"],
    addLabel: "an OpenRouter key",
  },
  {
    id: "openai_compat",
    name: "Local endpoint",
    account: "openai_compat_api_key",
    uses: "A local model, only if its endpoint asks for a key (Ollama doesn't)",
    env: "",
    checks: ["openai_compat_generate"],
    optional: true,
    addLabel: "a local endpoint key",
  },
];

export function keyInfo(id: KeyId): KeyInfo {
  return KEYS.find((k) => k.id === id) as KeyInfo;
}

export type KeyPresence = Partial<Record<string, boolean>>;

export type KeyState = {
  /** Where the key in use comes from; "unknown" when presence is unread. */
  source: "env" | "keychain" | "file" | "none" | "unknown";
  /** A Keychain key is also stored, but the variable wins. */
  keychainToo: boolean;
  /** The last real check, from `khipu secrets verify`. */
  check: "ok" | "failed" | null;
  detail: string;
};

/** What Khipu knows about one key. The environment wins over the Keychain
 *  for every provider key (`keychain.secrets_status`). A check titled
 *  "Not checked" means the CLI skipped it, so it counts as no check. */
export function keyStatus(
  presence: KeyPresence | null | undefined,
  verifyResult: ModelVerifyResult | null | undefined,
  id: KeyId,
): KeyState {
  const info = keyInfo(id);
  const real = info.checks
    .map((c) => modelCheckFor(verifyResult ?? null, c))
    .filter((c): c is NonNullable<typeof c> => c != null && c.title !== "Not checked");
  const failed = real.find((c) => !c.ok);
  const check: KeyState["check"] = failed ? "failed" : real.length ? "ok" : null;
  const detail = failed ? failed.detail || failed.title : "";
  if (!presence) return { source: "unknown", keychainToo: false, check, detail };
  const keychain = presence[`${id}_in_keychain`] === true;
  const env = id !== "openai_compat" && presence[`${id}_env`] === true;
  const file = id !== "openai_compat" && presence[`${id}_file_key_present`] === true;
  const source = env ? "env" : keychain ? "keychain" : file ? "file" : "none";
  return { source, keychainToo: env && keychain, check, detail };
}

function checkedAgo(checkedAt: number | null): string {
  if (checkedAt == null) return "";
  const s = Math.max(0, (Date.now() - checkedAt) / 1000);
  return s < 5 ? "checked just now" : `checked ${fmtAge(s)} ago`;
}

type VerifyApi = {
  result: ModelVerifyResult | null;
  checkedAt: number | null;
  verifying: boolean;
  verify: () => Promise<void>;
  /** A run that could not execute; shown on every checkable stored key. */
  runError?: string | null;
  /** Drops the earlier results for these check ids when a key is replaced. */
  forget?: (ids: readonly string[]) => void;
};

function parseOk(raw: unknown): { ok?: boolean; error?: string } | null {
  if (typeof raw !== "string") return null;
  try {
    const v = JSON.parse(raw);
    return v && typeof v === "object" ? (v as { ok?: boolean; error?: string }) : null;
  } catch {
    return null;
  }
}

function KeyRow({
  info,
  presence,
  presenceError,
  api,
  onKeySaved,
}: {
  info: KeyInfo;
  presence: KeyPresence | null;
  presenceError: string | null;
  api: VerifyApi;
  onKeySaved: () => Promise<void> | void;
}) {
  const st = keyStatus(presence, api.result, info.id);
  const checkable = info.checks.length > 0;
  const stored = st.source === "env" || st.source === "keychain" || st.source === "file";
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const addBtn = useRef<HTMLButtonElement | null>(null);
  const fieldId = `key-edit-${info.id}`;

  function cancel() {
    setEditing(false);
    setValue("");
    setErr(null);
    addBtn.current?.focus();
  }

  async function save() {
    const v = value.trim();
    if (!v || saving) return;
    setSaving(true);
    setErr(null);
    try {
      const out = parseOk(await invoke<string>("set_khipu_secret", { account: info.account, value: v }));
      if (out?.ok) {
        setValue("");
        setEditing(false);
        api.forget?.(info.checks);
        await onKeySaved();
        if (checkable) await api.verify();
      } else {
        setErr(out?.error ?? "Could not save the key.");
      }
    } catch (e) {
      setErr(String(e));
    } finally {
      setSaving(false);
    }
  }

  const age = checkedAgo(api.checkedAt);
  let tone: "ok" | "err" | "neutral" | "muted" = "neutral";
  let body: ReactNode;
  if (st.source === "unknown") {
    tone = "muted";
    body = presenceError ? "Can't read whether a key is stored." : "Reading…";
  } else if (st.source === "none") {
    tone = "muted";
    body = info.optional ? "Optional, not stored" : "Not stored";
  } else if (api.runError) {
    tone = "err";
    body = `${info.name}: the check could not run: ${api.runError}`;
  } else {
    const result =
      st.check === "ok"
        ? `Works${age ? `, ${age}` : ""}`
        : st.check === "failed"
          ? `${info.name}: ${st.detail}`
          : st.source === "env"
            ? "Not checked"
            : "Stored, not checked";
    tone = st.check === "ok" ? "ok" : st.check === "failed" ? "err" : "neutral";
    if (st.source === "env") {
      body = (
        <span>
          Set by <code>{info.env}</code> in the environment; that one is used.
          {result ? ` ${result}${st.check === "ok" ? "." : ""}` : ""}
        </span>
      );
    } else {
      body = result;
    }
  }

  const failed = stored && (st.check === "failed" || Boolean(api.runError));
  return (
    <div
      className={`row-item key-row${failed ? " err" : ""}`}
      role="listitem"
      id={`key-row-${info.id}`}
      tabIndex={-1}
    >
      <span className="key-text">
        <span className="key-name">{info.name}</span>
        <span className="key-uses">{info.uses}</span>
        <span className={`key-status${tone === "err" ? " err" : tone === "muted" ? " muted" : ""}`}>
          {tone === "muted" ? null : (
            <span className={`hdot${tone === "ok" ? " ok" : tone === "err" ? " err" : ""}`} aria-hidden="true" />
          )}
          {body}
        </span>
        {st.keychainToo ? (
          <span className="key-uses">A key stored in the Keychain is kept but not used while the variable is set.</span>
        ) : null}
      </span>
      <span className="key-acts">
        {st.source !== "env" ? (
          <button
            ref={addBtn}
            type="button"
            className="sm"
            aria-label={st.source === "keychain" || st.source === "file" ? `Replace the ${info.name} key` : `Add ${info.addLabel}`}
            aria-expanded={editing}
            aria-controls={fieldId}
            disabled={st.source === "unknown"}
            onClick={() => setEditing((e) => !e)}
          >
            {st.source === "keychain" || st.source === "file" ? "Replace…" : "Add…"}
          </button>
        ) : null}
        {checkable && stored ? (
          <button
            type="button"
            className="sm"
            aria-label={`Check the ${info.name} key now`}
            disabled={api.verifying}
            onClick={() => void api.verify()}
          >
            {api.verifying ? <Loader2 size={12} className="spin" aria-hidden /> : null}
            {api.verifying ? "Checking…" : "Check now"}
          </button>
        ) : null}
      </span>
      {editing ? (
        <div className="key-edit" id={fieldId}>
          <label className="sr-only" htmlFor={`${fieldId}-input`}>
            {stored ? `New value for the ${info.name} key` : `Value for ${info.addLabel}`}
          </label>
          <input
            id={`${fieldId}-input`}
            className="mono"
            type="password"
            autoComplete="off"
            spellCheck={false}
            autoFocus
            value={value}
            placeholder="Paste the key"
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void save();
              } else if (e.key === "Escape") {
                e.preventDefault();
                cancel();
              }
            }}
          />
          <button type="button" className="sm primary" disabled={saving || !value.trim()} onClick={() => void save()}>
            {saving ? "Saving…" : "Save key"}
          </button>
          <button type="button" className="sm" onClick={cancel}>
            Cancel
          </button>
          {err ? (
            <p className="set-error" role="alert">
              {err}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export function KeysCard({
  active,
  presence,
  presenceError,
  verify,
  onKeySaved,
}: {
  active: boolean;
  presence: KeyPresence | null;
  presenceError: string | null;
  verify: VerifyApi;
  onKeySaved: () => Promise<void> | void;
}) {
  // A stored key with no real check is checked once when the page opens.
  const autoChecked = useRef(false);
  useEffect(() => {
    if (!active || autoChecked.current || !presence || verify.verifying) return;
    // Any stored key with no real result yet (a skipped "Not checked" is not
    // one) is checked once, whatever the last run's time says.
    const anyUnchecked = KEYS.some((k) => {
      if (k.checks.length === 0) return false;
      const st = keyStatus(presence, verify.result, k.id);
      return st.source !== "none" && st.source !== "unknown" && st.check == null;
    });
    if (!anyUnchecked) return;
    autoChecked.current = true;
    void verify.verify();
  }, [active, presence, verify]);

  return (
    <div className="section-card" id="keys">
      <div className="section-head">Keys on this Mac</div>
      <div className="section-body">
        <div className="rows" role="list" aria-label="Keys on this Mac">
          {KEYS.map((k) => (
            <KeyRow
              key={k.id}
              info={k}
              presence={presence}
              presenceError={presenceError}
              api={verify}
              onKeySaved={onKeySaved}
            />
          ))}
        </div>
        <p className="set-help">Keys are kept in this Mac's login Keychain, never in a file.</p>
      </div>
    </div>
  );
}
