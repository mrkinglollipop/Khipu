/* Which `khipu secrets` presence flags the app keeps: an allowlist, so a
   field the CLI adds later stays out until named here. */

// The summariser's API keys report from three places each (`khipu secrets`):
// the Keychain, the environment and a key file.
const SYNTH_KEY_FIELDS = [
  "gemini_env",
  "gemini_file_key_present",
  "anthropic_in_keychain",
  "anthropic_env",
  "anthropic_file_key_present",
  "openai_in_keychain",
  "openai_env",
  "openai_file_key_present",
  "openrouter_in_keychain",
  "openrouter_env",
  "openrouter_file_key_present",
] as const;

// The plan-usage keys the Keys card shows (GLM, Kimi), from the same three places.
const PLAN_KEY_FIELDS = [
  "glm_in_keychain",
  "glm_env",
  "glm_file_key_present",
  "kimi_in_keychain",
  "kimi_env",
  "kimi_file_key_present",
] as const;

export type SecretsPresence = {
  dsn_in_keychain?: boolean;
  gemini_in_keychain?: boolean;
  openai_compat_in_keychain?: boolean;
} & Partial<Record<(typeof SYNTH_KEY_FIELDS)[number] | (typeof PLAN_KEY_FIELDS)[number], boolean>>;

const SECRET_PRESENCE_KEYS = [
  "dsn_in_keychain",
  "gemini_in_keychain",
  "openai_compat_in_keychain",
  ...SYNTH_KEY_FIELDS,
  ...PLAN_KEY_FIELDS,
] as const satisfies ReadonlyArray<keyof SecretsPresence>;

export function isSecretsPresence(value: unknown): value is SecretsPresence {
  if (value == null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const obj = value as Record<string, unknown>;
  if ("error" in obj) {
    return false;
  }
  for (const key of SECRET_PRESENCE_KEYS) {
    const field = obj[key];
    if (field !== undefined && typeof field !== "boolean") {
      return false;
    }
  }
  return true;
}

export function pickSecretsPresence(value: SecretsPresence): SecretsPresence {
  const out: SecretsPresence = {};
  for (const key of SECRET_PRESENCE_KEYS) out[key] = value[key];
  return out;
}
