import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { modelSelection, providerForSelection, selectionPatch, MODEL_SOURCES, type ModelName } from "../settings/modelSources";
import { ModelChoices } from "../settings/pages/ModelChoices";
import type { SynthRow } from "../SynthProvider";
import type { SettingsCtx } from "../settings/context";

afterEach(cleanup);
const cases: Array<[string, ModelName, string, string]> = [
  ["claude-cli", "Claude Haiku", "Claude subscription", "haiku"],
  ["anthropic", "Claude Haiku", "Anthropic API key", "claude-haiku-5-5"],
  ["gemini", "Gemini Flash", "Google API key", "gemini-2.5-flash"],
  ["codex-cli", "GPT", "ChatGPT subscription", "gpt-custom"],
  ["openai", "GPT", "OpenAI API key", "gpt-custom"],
  ["openrouter", "Other model…", "OpenRouter", "vendor/custom"],
  ["local", "Other model…", "This Mac or my network", "my-model"],
];
function Host({ provider, model_id }: { provider: string; model_id: string }) {
  const [row, setRow] = useState<SynthRow>({ provider, model_id, endpoint: "http://localhost:11434" });
  const ctx = { synth: row, onSynthChange: (patch: Partial<SynthRow>) => setRow((old) => ({ ...old, ...patch })),
    secretsPresence: null, doctorSynth: null, modelCheck: { result: null, runError: null } } as SettingsCtx;
  return <><ModelChoices ctx={ctx} /><output data-testid="stored">{JSON.stringify(row)}</output></>;
}
describe("Models and sources retain the seven stored providers", () => {
  it.each(cases)("maps %s in both directions", (provider, model, source, model_id) => {
    const row = { provider, model_id, endpoint: "http://localhost:11434" };
    expect(modelSelection(row)).toEqual({ model, source });
    expect(providerForSelection(model, source)).toBe(provider);
    expect({ ...row, ...selectionPatch(row, model, source) }).toEqual(row);
    render(<Host provider={provider} model_id={model_id} />);
    expect((screen.getByLabelText("Model") as HTMLSelectElement).value).toBe(model);
    expect((screen.getByLabelText("Source") as HTMLSelectElement).value).toBe(source);
    expect((screen.getByLabelText("Model name") as HTMLInputElement).value).toBe(model_id);
  });
  it("offers OpenRouter for every family and restricts sources to the model", () => {
    for (const model of Object.keys(MODEL_SOURCES) as ModelName[]) expect(providerForSelection(model, "OpenRouter")).toBe("openrouter");
    expect(providerForSelection("GPT", "Anthropic API key")).toBeUndefined();
    render(<Host provider="anthropic" model_id="claude-haiku-5-5" />);
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "GPT" } });
    expect(within(screen.getByLabelText("Source")).getAllByRole("option").map((o) => o.textContent)).toEqual(MODEL_SOURCES.GPT);
    expect(screen.getByTestId("stored").textContent).toContain('"provider":"codex-cli"');
    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "OpenAI API key" } });
    expect(screen.getByTestId("stored").textContent).toContain('"provider":"openai"');
    expect(within(screen.getByLabelText("Model")).getAllByRole("option").map((o) => o.textContent).join(" ")).not.toMatch(/login|claude -p|codex|\(API key\)/);
  });
  it.each([
    ["Claude Haiku", "anthropic/claude-haiku-5-5", "Claude subscription", "claude-cli", "haiku"],
    ["Claude Haiku", "anthropic/claude-haiku-5-5", "Anthropic API key", "anthropic", "claude-haiku-5-5"],
    ["Gemini Flash", "google/gemini-2.5-flash", "Google API key", "gemini", "gemini-2.5-flash"],
    ["GPT", "openai/gpt-6-luna", "OpenAI API key", "openai", ""],
    ["GPT", "openai/gpt-6-luna", "ChatGPT subscription", "codex-cli", ""],
  ] as const)("moves generated %s names from OpenRouter to %s using destination defaults", (model, model_id, source, provider, expected) => {
    expect(selectionPatch({ provider: "openrouter", model_id, endpoint: "" }, model, source)).toEqual({ provider, model_id: expected });
  });
  it.each([
    ["openai", "gpt-6-luna", "GPT", "OpenRouter", "openrouter", "openai/gpt-6-luna"],
    ["anthropic", "claude-custom-2026", "Claude Haiku", "OpenRouter", "openrouter", "anthropic/claude-haiku-5-5"],
    ["openrouter", "anthropic/claude-haiku-4", "Claude Haiku", "Anthropic API key", "anthropic", "claude-haiku-5-5"],
    ["openrouter", "openai/gpt-custom", "GPT", "OpenAI API key", "openai", ""],
  ] as const)("never carries a custom %s id (%s) across the OpenRouter line", (from, model_id, model, source, provider, expected) => {
    expect(selectionPatch({ provider: from, model_id, endpoint: "" }, model, source)).toEqual({ provider, model_id: expected });
  });
  it("keeps custom model ids and endpoints when only their source changes", () => {
    render(<Host provider="anthropic" model_id="claude-custom-2026" />);
    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "Claude subscription" } });
    expect(JSON.parse(screen.getByTestId("stored").textContent!)).toEqual({ provider: "claude-cli", model_id: "claude-custom-2026", endpoint: "http://localhost:11434" });
  });
  it.each([['anthropic/claude-haiku-5-5', 'Claude Haiku'], ['google/gemini-2.5-flash', 'Gemini Flash'], ['openai/gpt-6-luna', 'GPT']] as const)("reads OpenRouter family %s", (model_id, model) => {
    expect(modelSelection({ provider: "openrouter", model_id, endpoint: "" })).toEqual({ model, source: "OpenRouter" });
  });
});
