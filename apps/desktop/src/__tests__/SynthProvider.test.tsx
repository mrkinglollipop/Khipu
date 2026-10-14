// The summariser in Settings (docs/plans/2026-10-13-synth-providers.md,
// phase 3): the provider list and its default model ids, the required model
// id refusal, the key / login status, and the daily ceiling with "Lift for
// today" calling the fixed-argv command.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import {
  SYNTH_PROVIDERS,
  SummariserBudgetCard,
  SynthProviderFields,
  canonicalSynthProvider,
  defaultSynthModel,
  synthFallbackLine,
  synthModelError,
} from "../SynthProvider";
import type { DoctorSynthProvider, SynthRow, SynthSecretsPresence } from "../SynthProvider";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

const calls: Array<[string, Record<string, unknown> | undefined]> = [];
let budget: Record<string, unknown>;

beforeEach(() => {
  calls.length = 0;
  budget = {
    synth: {
      day: "2026-10-13",
      calls: 1227,
      cap: 5000,
      cap_source: "default",
      exhausted: false,
      estimated_usd_today: 1.234,
      usage_by_provider: {
        gemini: { calls: 300, estimated_usd: 1.234 },
        "claude-cli": { calls: 927, estimated_usd: 0, billing: "subscription" },
      },
    },
  };
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    calls.push([cmd, args]);
    switch (cmd) {
      case "khipu_budget_status":
        return JSON.stringify(budget);
      case "khipu_budget_lift_synth_today":
        (budget.synth as Record<string, unknown>).calls = 0;
        return JSON.stringify({ ok: true, did: "reset today's synth counter (1227 calls cleared)" });
      case "khipu_config_set":
        (budget.synth as Record<string, unknown>).cap = Number(args?.value);
        (budget.synth as Record<string, unknown>).cap_source = "config";
        return JSON.stringify({ ok: true });
      case "set_khipu_secret":
        return JSON.stringify({ ok: true, account: args?.account, stored: true });
      default:
        throw new Error(`unexpected command ${cmd}`);
    }
  });
});

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
});

function Harness({
  initial,
  presence = null,
  doctorSynth = null,
}: {
  initial: SynthRow;
  presence?: SynthSecretsPresence | null;
  doctorSynth?: DoctorSynthProvider | null;
}) {
  const [row, setRow] = useState(initial);
  return (
    <>
      <SynthProviderFields
        row={row}
        onChange={(patch) => setRow((r) => ({ ...r, ...patch }))}
        presence={presence}
        doctorSynth={doctorSynth}
      />
      <output data-testid="row">{JSON.stringify(row)}</output>
    </>
  );
}

const rowOf = () => JSON.parse(screen.getByTestId("row").textContent ?? "{}") as SynthRow;

describe("provider list and defaults (pure)", () => {
  it("offers the seven providers with their labels, in order", () => {
    expect(SYNTH_PROVIDERS.map((p) => [p.id, p.label])).toEqual([
      ["gemini", "Gemini Flash (API key)"],
      ["anthropic", "Claude Haiku (API key)"],
      ["openai", "OpenAI (API key)"],
      ["openrouter", "OpenRouter (one key, any model)"],
      ["claude-cli", "Claude on my own login (claude -p)"],
      ["codex-cli", "ChatGPT on my own login (codex)"],
      ["local", "local"],
    ]);
  });

  it("prefills the CLI's default model ids and reads cloud as gemini", () => {
    expect(defaultSynthModel("gemini")).toBe("gemini-2.5-flash");
    expect(defaultSynthModel("cloud")).toBe("gemini-2.5-flash");
    expect(defaultSynthModel("anthropic")).toBe("claude-haiku-5-5");
    expect(defaultSynthModel("claude-cli")).toBe("haiku");
    expect(defaultSynthModel("openai")).toBe("");
    expect(canonicalSynthProvider("cloud")).toBe("gemini");
  });

  it("requires a model id for openai, openrouter and codex-cli only", () => {
    expect(synthModelError("openai", "")).toMatch(/gpt-6-luna/);
    expect(synthModelError("openrouter", " ")).toMatch(/openai\/gpt-6-luna/);
    expect(synthModelError("codex-cli", "")).toMatch(/needs a model id/);
    expect(synthModelError("codex-cli", "gpt-6-luna")).toBeNull();
    for (const p of ["gemini", "anthropic", "claude-cli", "local", "cloud"]) {
      expect(synthModelError(p, "")).toBeNull();
    }
  });
});

describe("SynthProviderFields", () => {
  it("shows an old cloud config as Gemini and prefills the model on provider change", () => {
    render(<Harness initial={{ provider: "cloud", endpoint: "", model_id: "gemini-2.5-flash" }} />);
    const select = screen.getByLabelText("Provider") as HTMLSelectElement;
    expect(select.value).toBe("gemini");
    fireEvent.change(select, { target: { value: "anthropic" } });
    expect(rowOf()).toMatchObject({ provider: "anthropic", model_id: "claude-haiku-5-5" });
    fireEvent.change(select, { target: { value: "claude-cli" } });
    expect(rowOf()).toMatchObject({ provider: "claude-cli", model_id: "haiku" });
  });

  it("refuses an empty model id for codex-cli and shows the example", () => {
    render(<Harness initial={{ provider: "gemini", endpoint: "", model_id: "gemini-2.5-flash" }} />);
    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "codex-cli" } });
    expect(rowOf().model_id).toBe("");
    expect(screen.getByRole("alert").textContent).toMatch(/needs a model id, for example gpt-6-luna/);
    expect((screen.getByLabelText("Summariser model id") as HTMLInputElement).placeholder).toBe("gpt-6-luna");
    fireEvent.change(screen.getByLabelText("Summariser model id"), { target: { value: "gpt-6-luna" } });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows the quality note with the measurement date", () => {
    render(<Harness initial={{ provider: "gemini", endpoint: "", model_id: "gemini-2.5-flash" }} />);
    expect(screen.getByText(/Measured 2026-10-13 on 15 real windows/)).toBeTruthy();
    expect(screen.getByText(/Gemini stays the embedder/)).toBeTruthy();
  });

  it("reports key presence and sets a key through the stdin command", async () => {
    render(
      <Harness
        initial={{ provider: "anthropic", endpoint: "", model_id: "claude-haiku-5-5" }}
        presence={{ anthropic_in_keychain: false, anthropic_env: false, anthropic_file_key_present: false }}
      />,
    );
    expect(screen.getByText("No key")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Claude Haiku (API key) key"), { target: { value: "sk-ant-test" } });
    fireEvent.click(screen.getByRole("button", { name: "Set key" }));
    await waitFor(() => expect(screen.getByText("Saved to Keychain.")).toBeTruthy());
    expect(calls).toContainEqual(["set_khipu_secret", { account: "anthropic_api_key", value: "sk-ant-test" }]);
  });

  it("shows the env key source for OpenRouter", () => {
    render(
      <Harness
        initial={{ provider: "openrouter", endpoint: "", model_id: "openai/gpt-6-luna" }}
        presence={{ openrouter_in_keychain: false, openrouter_env: true }}
      />,
    );
    expect(screen.getByText("Key present")).toBeTruthy();
    expect(screen.getByText(/OPENROUTER_API_KEY/)).toBeTruthy();
  });

  it("shows whether the login CLI is found, and the no-key note", () => {
    render(
      <Harness
        initial={{ provider: "claude-cli", endpoint: "", model_id: "haiku" }}
        doctorSynth={{ provider: "claude-cli", model: "haiku", binary: "/usr/local/bin/claude", auth: "subscription" }}
      />,
    );
    expect(screen.getByText("claude found")).toBeTruthy();
    expect(screen.getByText(/no key is stored/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Set key" })).toBeNull();
  });

  it("says the codex CLI is missing when doctor found none", () => {
    render(
      <Harness
        initial={{ provider: "codex-cli", endpoint: "", model_id: "gpt-6-luna" }}
        doctorSynth={{ provider: "codex-cli", model: "gpt-6-luna", binary: null, auth: "subscription" }}
      />,
    );
    expect(screen.getByText("codex not found")).toBeTruthy();
  });
});

describe("summariser fallback (synth_fallback)", () => {
  const anthropicRow = { provider: "anthropic", endpoint: "", model_id: "claude-haiku-5-5" };
  const fallbackSelect = () => screen.getByLabelText("If the provider fails") as HTMLSelectElement;

  it("shows the stored value, and Gemini when doctor does not say", () => {
    render(
      <Harness
        initial={anthropicRow}
        doctorSynth={{ provider: "anthropic", fallback: { setting: "none", source: "config", gemini_key: true } }}
      />,
    );
    expect(fallbackSelect().value).toBe("none");
    expect(fallbackSelect().disabled).toBe(false);
    expect(screen.getByText(/count under By provider as gemini/)).toBeTruthy();
    cleanup();
    render(<Harness initial={anthropicRow} />);
    expect(fallbackSelect().value).toBe("gemini");
  });

  it("saves a change as synth_fallback and reloads", async () => {
    const reload = vi.fn();
    render(
      <SynthProviderFields
        row={anthropicRow}
        onChange={() => {}}
        presence={null}
        doctorSynth={{ provider: "anthropic", fallback: { setting: "gemini", source: "default", gemini_key: true } }}
        onKeySaved={reload}
      />,
    );
    fireEvent.change(fallbackSelect(), { target: { value: "none" } });
    await waitFor(() => expect(reload).toHaveBeenCalled());
    expect(calls).toContainEqual(["khipu_config_set", { key: "synth_fallback", value: "none" }]);
    expect(fallbackSelect().value).toBe("none");
  });

  it("shows the CLI error when the save fails", async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_config_set") throw new Error("synth_fallback must be gemini or none");
      throw new Error(`unexpected command ${cmd}`);
    });
    render(<Harness initial={anthropicRow} />);
    fireEvent.change(fallbackSelect(), { target: { value: "none" } });
    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(/must be gemini or none/));
    expect(fallbackSelect().value).toBe("gemini");
  });

  it("is disabled when Gemini is the provider", () => {
    render(
      <Harness
        initial={{ provider: "gemini", endpoint: "", model_id: "gemini-2.5-flash" }}
        doctorSynth={{ provider: "gemini", fallback: { setting: "gemini", source: "default", gemini_key: false } }}
      />,
    );
    expect(fallbackSelect().disabled).toBe(true);
    expect(screen.getByText("Gemini is the provider; there is nothing to fall back to.")).toBeTruthy();
    expect(screen.queryByText("No Gemini key")).toBeNull();
  });

  it("warns when the fallback is on and no Gemini key resolves", () => {
    render(
      <Harness
        initial={{ provider: "claude-cli", endpoint: "", model_id: "haiku" }}
        doctorSynth={{
          provider: "claude-cli",
          binary: "/usr/local/bin/claude",
          fallback: { setting: "gemini", source: "default", gemini_key: false },
        }}
      />,
    );
    expect(screen.getByText("No Gemini key")).toBeTruthy();
    expect(screen.getByText(/or the fallback cannot run/)).toBeTruthy();
    cleanup();
    render(
      <Harness
        initial={{ provider: "claude-cli", endpoint: "", model_id: "haiku" }}
        doctorSynth={{ provider: "claude-cli", fallback: { setting: "none", source: "config", gemini_key: false } }}
      />,
    );
    expect(screen.queryByText("No Gemini key")).toBeNull();
  });

  it("words the fallback for the health report", () => {
    expect(synthFallbackLine({ provider: "anthropic", fallback: { setting: "gemini", gemini_key: true } })).toBe(
      "Fallback: Gemini Flash (key present)",
    );
    expect(synthFallbackLine({ provider: "anthropic", fallback: { setting: "gemini", gemini_key: false } })).toBe(
      "Fallback: Gemini Flash, no key",
    );
    expect(synthFallbackLine({ provider: "codex-cli", fallback: { setting: "none", gemini_key: true } })).toBe(
      "Fallback: off",
    );
    expect(synthFallbackLine({ provider: "gemini", fallback: { setting: "gemini", gemini_key: true } })).toBeNull();
    expect(synthFallbackLine({ provider: "anthropic" })).toBeNull();
  });
});

describe("SummariserBudgetCard", () => {
  it("shows calls, cap, source, cost and the per-provider breakdown", async () => {
    render(<SummariserBudgetCard active />);
    await waitFor(() => expect(screen.getByText("1,227 of 5,000 calls today (default)")).toBeTruthy());
    expect(screen.getByText("$1.23")).toBeTruthy();
    expect(screen.getByText("Gemini Flash (API key): 300 calls, $1.23")).toBeTruthy();
    expect(screen.getByText("Claude on my own login (claude -p): 927 calls, subscription")).toBeTruthy();
  });

  it("lifts for today through the fixed-argv command and shows the result", async () => {
    render(<SummariserBudgetCard active />);
    await waitFor(() => expect(screen.getByText(/1,227 of 5,000/)).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Lift for today" }));
    await waitFor(() => expect(screen.getByText(/Done: reset today's synth counter/)).toBeTruthy());
    expect(calls.map(([c]) => c)).toContain("khipu_budget_lift_synth_today");
    expect(calls.find(([c]) => c === "khipu_budget_lift_synth_today")?.[1]).toBeUndefined();
    await waitFor(() => expect(screen.getByText("0 of 5,000 calls today (default)")).toBeTruthy());
  });

  it("saves the ceiling as synth_daily_calls and refuses a non-number", async () => {
    render(<SummariserBudgetCard active />);
    await waitFor(() => expect(screen.getByText(/1,227 of 5,000/)).toBeTruthy());
    const field = screen.getByLabelText("Daily ceiling");
    fireEvent.change(field, { target: { value: "-3" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByText("Enter a whole number, 0 or more.")).toBeTruthy());
    expect(calls.map(([c]) => c)).not.toContain("khipu_config_set");
    fireEvent.change(field, { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByText(/calls today, no ceiling \(set in Settings\)/)).toBeTruthy());
    expect(calls).toContainEqual(["khipu_config_set", { key: "synth_daily_calls", value: "0" }]);
  });
});
