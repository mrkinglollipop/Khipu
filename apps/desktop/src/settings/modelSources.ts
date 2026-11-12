import { canonicalSynthProvider, defaultSynthModel, type SynthRow } from "../SynthProvider";

export const MODEL_NAMES = ["Claude Haiku", "Gemini Flash", "GPT", "Other model…"] as const;
export type ModelName = typeof MODEL_NAMES[number];
export const MODEL_SOURCES: Record<ModelName, readonly string[]> = {
  "Claude Haiku": ["Claude subscription", "Anthropic API key", "OpenRouter"],
  "Gemini Flash": ["Google API key", "OpenRouter"],
  GPT: ["ChatGPT subscription", "OpenAI API key", "OpenRouter"],
  "Other model…": ["OpenRouter", "This Mac or my network"],
};
const SOURCES: Record<string, string> = {
  "claude-cli": "Claude subscription", anthropic: "Anthropic API key", gemini: "Google API key",
  "codex-cli": "ChatGPT subscription", openai: "OpenAI API key", openrouter: "OpenRouter", local: "This Mac or my network",
};
export function modelSelection(row: SynthRow): { model: ModelName; source: string } {
  const provider = canonicalSynthProvider(row.provider);
  let model: ModelName = provider === "anthropic" || provider === "claude-cli" ? "Claude Haiku"
    : provider === "gemini" ? "Gemini Flash" : provider === "openai" || provider === "codex-cli" ? "GPT" : "Other model…";
  if (provider === "openrouter") {
    const id = row.model_id.toLowerCase();
    model = /claude.*haiku|haiku/.test(id) ? "Claude Haiku" : /gemini.*flash/.test(id) ? "Gemini Flash" : /(^|\/)gpt-/.test(id) ? "GPT" : "Other model…";
  }
  return { model, source: SOURCES[provider] ?? row.provider };
}
export function providerForSelection(model: ModelName, source: string): string | undefined {
  if (!MODEL_SOURCES[model].includes(source)) return undefined;
  return Object.keys(SOURCES).find((provider) => SOURCES[provider] === source);
}
export function selectionPatch(row: SynthRow, model: ModelName, source: string): Partial<SynthRow> {
  const provider = providerForSelection(model, source);
  if (!provider) return {};
  const before = modelSelection(row);
  // A source-only change preserves a custom model id. Defaults follow the new source.
  const generatedDefaults = ["haiku", "claude-haiku-5-5", "gemini-2.5-flash", "anthropic/claude-haiku-5-5", "google/gemini-2.5-flash", "openai/gpt-6-luna"];
  const custom = row.model_id.trim() && row.model_id !== defaultSynthModel(row.provider) && !generatedDefaults.includes(row.model_id);
  // OpenRouter ids carry a vendor prefix the direct sources reject, and the reverse, so a custom id
  // never crosses that line: the new source's default (or an empty, required field) takes its place.
  const crossesOpenRouter = (canonicalSynthProvider(row.provider) === "openrouter") !== (provider === "openrouter");
  const keep = before.model === model && custom && !crossesOpenRouter;
  let model_id = keep ? row.model_id : defaultSynthModel(provider);
  if (provider === "openrouter" && !keep) {
    model_id = model === "Claude Haiku" ? "anthropic/claude-haiku-5-5" : model === "Gemini Flash" ? "google/gemini-2.5-flash" : model === "GPT" ? "openai/gpt-6-luna" : "";
  }
  return { provider, model_id };
}
