// The summariser in Settings (docs/plans/2026-10-13-synth-providers.md,
// phase 3): the provider list and its default model ids, the required model
// id refusal, the key / login status, and the daily ceiling with "Lift for
// today" calling the fixed-argv command.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import {
  SYNTH_PROVIDERS,
  SummariserCalls,
  SynthFallbackField,
  SynthProviderChoices,
  canonicalSynthProvider,
  defaultSynthModel,
  synthCallsLine,
  synthFallbackLine,
  synthModelError,
} from "../SynthProvider";
import type { DoctorSynthProvider, SynthRow, SynthSecretsPresence } from "../SynthProvider";
import type { ModelVerifyResult } from "../modelVerify";

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
  verifyResult = null,
}: {
  initial: SynthRow;
  presence?: SynthSecretsPresence | null;
  doctorSynth?: DoctorSynthProvider | null;
  verifyResult?: ModelVerifyResult | null;
}) {
  const [row, setRow] = useState(initial);
  return (
    <>
      <p id="synth-help">Which model writes the summary.</p>
      <SynthProviderChoices
        row={row}
        onChange={(patch) => setRow((r) => ({ ...r, ...patch }))}
        presence={presence}
        verifyResult={verifyResult}
        doctorSynth={doctorSynth}
      />
      <SynthFallbackField
        provider={row.provider}
        doctorSynth={doctorSynth}
        presence={presence}
        verifyResult={verifyResult}
      />
      <output data-testid="row">{JSON.stringify(row)}</output>
    </>
  );
}

const verifyOf = (checks: Array<[string, boolean, string?]>): ModelVerifyResult => ({
  ok: checks.every((c) => c[1]),
  checks: checks.map(([id, ok, detail]) => ({
    id,
    ok,
    title: ok ? "Key works" : "Key check failed",
    detail: detail ?? "",
    model: null,
    seconds: 1,
  })),
});
const NO_KEYS = {
  gemini_in_keychain: false,
  gemini_env: false,
  gemini_file_key_present: false,
  anthropic_in_keychain: false,
  anthropic_env: false,
  anthropic_file_key_present: false,
};
const choice = (name: RegExp | string) => screen.getByRole("radio", { name }) as HTMLInputElement;

const rowOf = () => JSON.parse(screen.getByTestId("row").textContent ?? "{}") as SynthRow;

describe("provider list and defaults (pure)", () => {
  it("gives each provider a short name, kind and line for its row", () => {
    expect(SYNTH_PROVIDERS.map((p) => [p.name, p.kind ?? null, p.line])).toEqual([
      ["Gemini Flash", "API key", "Fastest, about 3 s, least accurate"],
      ["Claude Haiku", "API key", "Best memory, about 11 s a capture"],
      ["OpenAI", "API key", "Accurate, thinner on open loops, about 8 s"],
      ["OpenRouter", "API key", "One key, any model"],
      ["Claude", "your login", "Uses the login you already have; counts against that plan"],
      ["ChatGPT", "your login", "Uses the login you already have; counts against that plan"],
      ["A local model", null, "A model on this Mac or your network"],
    ]);
  });

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

describe("SynthProviderChoices", () => {
  const gemini = { provider: "gemini", endpoint: "", model_id: "gemini-2.5-flash" };

  it("shows an old cloud config as Gemini and prefills the model when the choice changes", () => {
    render(<Harness initial={{ ...gemini, provider: "cloud" }} />);
    expect(choice(/^Gemini Flash/).checked).toBe(true);
    fireEvent.click(choice(/Claude Haiku/));
    expect(rowOf()).toMatchObject({ provider: "anthropic", model_id: "claude-haiku-5-5" });
    fireEvent.click(choice(/^Claude your login/));
    expect(rowOf()).toMatchObject({ provider: "claude-cli", model_id: "haiku" });
  });

  it("opens only the chosen row, with the model id and no key field", () => {
    const { container } = render(<Harness initial={{ ...gemini, provider: "anthropic", model_id: "claude-haiku-5-5" }} />);
    expect(container.querySelectorAll(".choice-body")).toHaveLength(1);
    expect(container.querySelector(".choice.on .choice-body")).not.toBeNull();
    expect((screen.getByLabelText("Model id") as HTMLInputElement).value).toBe("claude-haiku-5-5");
    expect(screen.getByText("Default claude-haiku-5-5.")).toBeTruthy();
    expect(screen.queryByLabelText("Endpoint")).toBeNull();
    expect(container.querySelector('input[type="password"]')).toBeNull();
    expect(screen.queryByRole("button", { name: "Set key" })).toBeNull();
  });

  it("shows the endpoint for a local model only", () => {
    render(<Harness initial={{ provider: "local", endpoint: "http://127.0.0.1:11434", model_id: "qwen3:8b" }} />);
    expect((screen.getByLabelText("Endpoint") as HTMLInputElement).value).toBe("http://127.0.0.1:11434");
    expect(screen.getByText("Any OpenAI-compatible address, such as Ollama on this Mac.")).toBeTruthy();
    expect(screen.getByText(/add it as Local endpoint in Keys below/)).toBeTruthy();
    cleanup();
    render(<Harness initial={gemini} />);
    expect(screen.queryByLabelText("Endpoint")).toBeNull();
  });

  it("refuses an empty model id for ChatGPT and shows the example", () => {
    render(<Harness initial={gemini} />);
    fireEvent.click(choice(/ChatGPT/));
    expect(rowOf().model_id).toBe("");
    expect(screen.getByRole("alert").textContent).toMatch(/needs a model id, for example gpt-6-luna/);
    expect((screen.getByLabelText("Model id") as HTMLInputElement).placeholder).toBe("gpt-6-luna");
    expect(screen.getByText("Required, for example gpt-6-luna.")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Model id"), { target: { value: "gpt-6-luna" } });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("gives an unknown stored provider its own checked row", () => {
    render(<Harness initial={{ provider: "mystery", endpoint: "", model_id: "m" }} />);
    expect(choice(/mystery/).checked).toBe(true);
    expect(screen.getAllByRole("radio").filter((r) => (r as HTMLInputElement).name === "synth-provider")).toHaveLength(8);
  });

  it("puts words on every key status", () => {
    const stored = { ...NO_KEYS, gemini_in_keychain: true, anthropic_in_keychain: true };
    render(
      <Harness
        initial={gemini}
        presence={stored}
        verifyResult={verifyOf([["gemini_generate", true], ["gemini_embed", true]])}
      />,
    );
    expect(screen.getByText("Key works")).toBeTruthy();
    expect(screen.queryByText("Key stored")).toBeNull();
    expect(screen.getAllByText("Not checked").length).toBeGreaterThan(0);
    expect(screen.getAllByText("No key")).toHaveLength(2);
    cleanup();
    render(
      <Harness
        initial={gemini}
        presence={stored}
        verifyResult={verifyOf([["gemini_generate", false, "Gemini: this key was refused."], ["gemini_embed", true]])}
      />,
    );
    expect(screen.getByText("Key check failed")).toBeTruthy();
    cleanup();
    render(<Harness initial={gemini} presence={stored} />);
    expect(screen.getAllByText("Not checked").length).toBeGreaterThan(0);
    cleanup();
    render(<Harness initial={gemini} presence={null} />);
    expect(screen.queryByText("No key")).toBeNull();
  });

  it("ignores a skipped check titled Not checked", () => {
    const skipped: ModelVerifyResult = {
      ok: true,
      checks: [{ id: "gemini_generate", ok: true, title: "Not checked", detail: "", model: null, seconds: 0 }],
    };
    render(<Harness initial={gemini} presence={{ ...NO_KEYS, gemini_in_keychain: true }} verifyResult={skipped} />);
    expect(screen.queryByText("Key works")).toBeNull();
  });

  it("says whether the login CLI is found, with no key field", () => {
    render(
      <Harness
        initial={{ provider: "claude-cli", endpoint: "", model_id: "haiku" }}
        doctorSynth={{ provider: "claude-cli", model: "haiku", binary: "/usr/local/bin/claude", auth: "subscription" }}
      />,
    );
    expect(screen.getByText("Found on this Mac")).toBeTruthy();
    expect(screen.getByText(/no key is stored/)).toBeTruthy();
    expect(screen.queryByText(/Save models, then the health report/)).toBeNull();
    cleanup();
    render(
      <Harness
        initial={{ provider: "codex-cli", endpoint: "", model_id: "gpt-6-luna" }}
        doctorSynth={{ provider: "codex-cli", model: "gpt-6-luna", binary: null, auth: "subscription" }}
      />,
    );
    expect(screen.getByText("Not found on this Mac")).toBeTruthy();
    cleanup();
    render(<Harness initial={{ provider: "codex-cli", endpoint: "", model_id: "gpt-6-luna" }} />);
    expect(screen.getByText(/Save models, then the health report checks it is on this Mac/)).toBeTruthy();
  });

  it("words the local endpoint check", () => {
    const row = { provider: "local", endpoint: "http://x", model_id: "m" };
    render(<Harness initial={row} verifyResult={verifyOf([["openai_compat_generate", true]])} />);
    expect(screen.getByText("Works")).toBeTruthy();
    cleanup();
    render(<Harness initial={row} verifyResult={verifyOf([["openai_compat_generate", false, "refused"]])} />);
    expect(screen.getByText("Check failed")).toBeTruthy();
  });

  it("says which key the open row uses and scrolls to its row in Keys", () => {
    render(
      <>
        <div id="key-row-anthropic" tabIndex={-1} data-testid="keyrow" />
        <Harness initial={{ ...gemini, provider: "anthropic", model_id: "claude-haiku-5-5" }} presence={NO_KEYS} />
      </>,
    );
    expect(screen.getByText("Uses the Claude API key: not stored.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Add it in Keys below" }));
    expect(document.activeElement).toBe(screen.getByTestId("keyrow"));
    cleanup();
    render(
      <Harness
        initial={{ ...gemini, provider: "anthropic", model_id: "claude-haiku-5-5" }}
        presence={{ ...NO_KEYS, anthropic_env: true }}
      />,
    );
    expect(screen.getByText("Uses the Claude API key: set in the environment.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Check it in Keys below" })).toBeTruthy();
    cleanup();
    render(
      <Harness
        initial={gemini}
        presence={{ ...NO_KEYS, gemini_in_keychain: true }}
        verifyResult={verifyOf([["gemini_generate", false, "refused"]])}
      />,
    );
    expect(screen.getByText("Uses the Gemini key: the check failed.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Check it in Keys below" })).toBeTruthy();
  });

  it("keeps the measured line out of the rows and the old quality paragraphs gone", () => {
    render(<Harness initial={gemini} />);
    expect(screen.queryByText(/Gemini stays the embedder/)).toBeNull();
    expect(screen.queryByText(/first in 10 of 15/)).toBeNull();
  });
});

describe("summariser fallback (synth_fallback)", () => {
  const anthropicRow = { provider: "anthropic", endpoint: "", model_id: "claude-haiku-5-5" };
  const retry = () => screen.getByRole("radio", { name: "Retry on Gemini Flash" }) as HTMLInputElement;
  const wait = () => screen.getByRole("radio", { name: "Wait and retry later on the same provider" }) as HTMLInputElement;

  it("shows the stored value, and Gemini when doctor does not say", () => {
    render(
      <Harness
        initial={anthropicRow}
        doctorSynth={{ provider: "anthropic", fallback: { setting: "none", source: "config", gemini_key: true } }}
      />,
    );
    expect(wait().checked).toBe(true);
    expect(wait().disabled).toBe(false);
    expect(screen.getByText(/Nothing is dropped: a failed capture waits and is retried/)).toBeTruthy();
    expect(screen.getByRole("group", { name: "If the provider fails" })).toBeTruthy();
    cleanup();
    render(<Harness initial={anthropicRow} doctorSynth={{ provider: "anthropic", fallback: {} }} />);
    expect(retry().checked).toBe(true);
    expect(screen.getByText(/Fallback uses the Gemini key, about \$0.002 a conversation/)).toBeTruthy();
  });

  it("saves a change as synth_fallback and reloads", async () => {
    const reload = vi.fn();
    render(
      <SynthFallbackField
        provider="anthropic"
        onSaved={reload}
        doctorSynth={{ provider: "anthropic", fallback: { setting: "gemini", source: "default", gemini_key: true } }}
      />,
    );
    fireEvent.click(wait());
    await waitFor(() => expect(reload).toHaveBeenCalled());
    expect(calls).toContainEqual(["khipu_config_set", { key: "synth_fallback", value: "none" }]);
    expect(wait().checked).toBe(true);
  });

  it("shows the CLI error when the save fails", async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_config_set") throw new Error("synth_fallback must be gemini or none");
      throw new Error(`unexpected command ${cmd}`);
    });
    render(<Harness initial={anthropicRow} doctorSynth={{ provider: "anthropic", fallback: {} }} />);
    fireEvent.click(wait());
    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(/must be gemini or none/));
    expect(retry().checked).toBe(true);
  });

  it("is disabled when Gemini is the provider", () => {
    render(
      <Harness
        initial={{ provider: "gemini", endpoint: "", model_id: "gemini-2.5-flash" }}
        doctorSynth={{ provider: "gemini", fallback: { setting: "gemini", source: "default", gemini_key: false } }}
      />,
    );
    expect(retry().disabled).toBe(true);
    expect(wait().disabled).toBe(true);
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
    expect(screen.getByText(/Add a Gemini key in Keys below, or the fallback cannot run/)).toBeTruthy();
    cleanup();
    render(
      <Harness
        initial={{ provider: "claude-cli", endpoint: "", model_id: "haiku" }}
        doctorSynth={{ provider: "claude-cli", fallback: { setting: "none", source: "config", gemini_key: false } }}
      />,
    );
    expect(screen.queryByText("No Gemini key")).toBeNull();
  });

  it("says a refused Gemini key leaves a failed capture waiting", () => {
    render(
      <Harness
        initial={anthropicRow}
        doctorSynth={{ provider: "anthropic", fallback: {} }}
        presence={{ ...NO_KEYS, gemini_in_keychain: true }}
        verifyResult={verifyOf([["gemini_generate", false, "refused"]])}
      />,
    );
    expect(screen.getByText(/While that key is refused, a failed capture waits and is retried/)).toBeTruthy();
  });

  it("shows Reading… only while the config read is in flight, then both choices", async () => {
    let release: (v: string) => void = () => {};
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_config_show") return new Promise<string>((r) => (release = r));
      throw new Error(`unexpected command ${cmd}`);
    });
    render(<Harness initial={anthropicRow} doctorSynth={{ provider: "anthropic" }} />);
    expect(screen.queryByRole("radio", { name: "Retry on Gemini Flash" })).toBeNull();
    expect(screen.getByRole("group", { name: "If the provider fails" }).textContent).toContain("Reading…");
    release(JSON.stringify({ config: { synth_fallback: "none" } }));
    await waitFor(() => expect(wait().checked).toBe(true));
    expect(screen.queryByText("Reading…")).toBeNull();
  });

  it("reads the stored fallback from config when doctor has no fallback block", async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_config_show") return JSON.stringify({ config: {} });
      throw new Error(`unexpected command ${cmd}`);
    });
    render(<Harness initial={anthropicRow} doctorSynth={{ provider: "anthropic", key_present: true }} />);
    await waitFor(() => expect(retry().checked).toBe(true));
    expect(wait().disabled).toBe(false);
  });

  it("keeps both choices usable, with a failure sentence, when the config read fails", async () => {
    invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
      calls.push([cmd, args]);
      if (cmd === "khipu_config_show") throw new Error("config unreadable");
      if (cmd === "khipu_config_set") return JSON.stringify({ ok: true });
      throw new Error(`unexpected command ${cmd}`);
    });
    render(<Harness initial={anthropicRow} doctorSynth={{ provider: "anthropic" }} />);
    await screen.findByText(/Can't read the stored choice/);
    expect(retry().disabled).toBe(false);
    fireEvent.click(wait());
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_set", { key: "synth_fallback", value: "none" }]));
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

describe("SummariserCalls", () => {
  it("shows calls, limit, source, cost and the per-provider breakdown", async () => {
    render(<SummariserCalls active />);
    await waitFor(() => expect(screen.getByText("Calls today: 1,227 of 5,000, about $1.23")).toBeTruthy());
    const sub = screen.getByText(/By provider:/).textContent ?? "";
    expect(sub).toMatch(/By provider: Gemini Flash 300, Claude 927\./);
    expect(sub).toMatch(/Daily limit 5,000 \(default\); 0 means no limit\./);
    expect(sub).toMatch(/It starts again at midnight UTC, .+ here\./);
    expect(screen.getByRole("group", { name: "Calls today" })).toBeTruthy();
  });

  it("says Reading… before the read and the error when it fails", async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_budget_status") throw new Error("budget file unreadable");
      throw new Error(`unexpected command ${cmd}`);
    });
    render(<SummariserCalls active />);
    expect(screen.getByText("Calls today: Reading…")).toBeTruthy();
    await waitFor(() => expect(screen.getByText(/Can't read today's calls: .*budget file unreadable/)).toBeTruthy());
  });

  it("makes Lift for today the main action when the limit is reached", async () => {
    (budget.synth as Record<string, unknown>).calls = 5000;
    (budget.synth as Record<string, unknown>).exhausted = true;
    const { container } = render(<SummariserCalls active />);
    await waitFor(() =>
      expect(screen.getByText("Calls today: 5,000 of 5,000. Captures wait until midnight UTC; nothing is dropped.")).toBeTruthy(),
    );
    expect(container.querySelector(".calls.warn")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Lift for today" }).className).toMatch(/primary/);
    expect(screen.getByText(/Midnight UTC is .+ here\. About \$1\.23 today\./)).toBeTruthy();
    expect(screen.getByText(/starts today's count again/)).toBeTruthy();
  });

  it("keeps Change… and Lift for today when the read fails, with an empty limit field", async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_budget_status") throw new Error("budget file unreadable");
      throw new Error(`unexpected command ${cmd}`);
    });
    render(<SummariserCalls active />);
    expect(screen.getByRole("button", { name: "Lift for today" })).toBeTruthy();
    await waitFor(() => expect(screen.getByText(/Can't read today's calls/)).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Change the daily limit" }));
    expect((screen.getByLabelText("Daily limit") as HTMLInputElement).value).toBe("");
    expect(screen.getByRole("button", { name: "Lift for today" })).toBeTruthy();
  });

  it("pluralises the calls line by number", () => {
    expect(synthCallsLine({ calls: 1, cap: 5000 })).toBe("1 of 5,000 calls today");
    expect(synthCallsLine({ calls: 1, cap: 1 })).toBe("1 of 1 call today");
    expect(synthCallsLine({ calls: 1, cap: 0 })).toBe("1 call today, no ceiling");
    expect(synthCallsLine({ calls: 2, cap: 0 })).toBe("2 calls today, no ceiling");
  });

  it("says there is no limit when the cap is 0", async () => {
    (budget.synth as Record<string, unknown>).cap = 0;
    render(<SummariserCalls active />);
    await waitFor(() => expect(screen.getByText("Calls today: 1,227, no daily limit, about $1.23")).toBeTruthy());
  });

  it("lifts for today through the fixed-argv command and shows the result", async () => {
    render(<SummariserCalls active />);
    await waitFor(() => expect(screen.getByText(/1,227 of 5,000/)).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Lift for today" }));
    await waitFor(() => expect(screen.getByText(/Done: reset today's synth counter/)).toBeTruthy());
    expect(calls.find(([c]) => c === "khipu_budget_lift_synth_today")?.[1]).toBeUndefined();
    await waitFor(() => expect(screen.getByText(/Calls today: 0 of 5,000/)).toBeTruthy());
  });

  it("opens the daily limit from Change… and saves it as synth_daily_calls", async () => {
    render(<SummariserCalls active />);
    await waitFor(() => expect(screen.getByText(/1,227 of 5,000/)).toBeTruthy());
    const change = screen.getByRole("button", { name: "Change the daily limit" });
    expect(change.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByLabelText("Daily limit")).toBeNull();
    fireEvent.click(change);
    expect(change.getAttribute("aria-expanded")).toBe("true");
    expect(document.getElementById(change.getAttribute("aria-controls") ?? "")).not.toBeNull();
    const field = screen.getByLabelText("Daily limit");
    fireEvent.change(field, { target: { value: "-3" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByText("Enter a whole number, 0 or more.")).toBeTruthy());
    expect(calls.map(([c]) => c)).not.toContain("khipu_config_set");
    fireEvent.change(field, { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByText(/Calls today: 1,227, no daily limit/)).toBeTruthy());
    expect(calls).toContainEqual(["khipu_config_set", { key: "synth_daily_calls", value: "0" }]);
  });
});
