// "Keys on this Mac" and the composed Capture & models cards: one place for
// the keys, status in words, checks on open and from Check now, and a single
// Keychain sentence on the page.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { KeysCard, keyStatus } from "../KeysCard";
import { useModelVerify } from "../modelVerify";
import { CaptureTuningCard } from "../SettingsControls";
import { SummariserCalls, SynthFallbackField, SynthProviderChoices } from "../SynthProvider";
import type { ModelVerifyResult } from "../modelVerify";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

const calls: Array<[string, Record<string, unknown> | undefined]> = [];

beforeEach(() => {
  calls.length = 0;
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    calls.push([cmd, args]);
    switch (cmd) {
      case "set_khipu_secret":
        return JSON.stringify({ ok: true, account: args?.account, stored: true });
      case "khipu_config_show":
        return JSON.stringify({
          capture_mode: "dual",
          capture_mode_source: "default",
          user_aliases: [],
          float_settings: {
            dedup_similarity: { value: 0.92, source: "default", default: 0.92 },
            commitment_close_similarity: { value: 0.85, source: "default", default: 0.85 },
          },
          config: {},
        });
      case "khipu_budget_status":
        return JSON.stringify({ synth: { calls: 12, cap: 5000, cap_source: "default", exhausted: false } });
      default:
        throw new Error(`unexpected command ${cmd}`);
    }
  });
});

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
});

const okResult: ModelVerifyResult = {
  ok: true,
  checks: [
    { id: "gemini_generate", ok: true, title: "Key works", detail: "", model: "m", seconds: 1 },
    { id: "gemini_embed", ok: true, title: "Key works", detail: "", model: "m", seconds: 1 },
  ],
};
const badResult: ModelVerifyResult = {
  ok: false,
  checks: [
    { id: "gemini_generate", ok: false, title: "Key check failed", detail: "this key was refused.", model: "m", seconds: 1 },
    { id: "gemini_embed", ok: true, title: "Key works", detail: "", model: "m", seconds: 1 },
  ],
};
const NONE = {
  gemini_in_keychain: false,
  gemini_env: false,
  gemini_file_key_present: false,
  anthropic_in_keychain: false,
  anthropic_env: false,
  anthropic_file_key_present: false,
  openai_in_keychain: false,
  openai_env: false,
  openai_file_key_present: false,
  openrouter_in_keychain: false,
  openrouter_env: false,
  openrouter_file_key_present: false,
  openai_compat_in_keychain: false,
};

function verifyApi(over: Partial<{ result: ModelVerifyResult | null; checkedAt: number | null; verifying: boolean; runError: string | null }> = {}) {
  const verify = vi.fn(async () => {});
  return { api: { result: null, checkedAt: null, verifying: false, verify, ...over }, verify };
}

function renderKeys(
  presence: Record<string, boolean> | null,
  over: Parameters<typeof verifyApi>[0] = {},
  extra: { presenceError?: string | null; onKeySaved?: () => Promise<void> } = {},
) {
  const { api, verify } = verifyApi(over);
  const onKeySaved = extra.onKeySaved ?? vi.fn(async () => {});
  const utils = render(
    <KeysCard
      active
      presence={presence}
      presenceError={extra.presenceError ?? null}
      verify={api}
      onKeySaved={onKeySaved}
    />,
  );
  return { ...utils, verify, onKeySaved };
}
const rowOf = (name: string) => screen.getByText(name, { selector: ".key-name" }).closest(".key-row") as HTMLElement;

describe("keyStatus (pure)", () => {
  it("lets the environment win over the Keychain and ignores skipped checks", () => {
    const st = keyStatus({ ...NONE, gemini_env: true, gemini_in_keychain: true }, okResult, "gemini");
    expect(st).toMatchObject({ source: "env", keychainToo: true, check: "ok" });
    expect(keyStatus({ ...NONE, openai_file_key_present: true }, null, "openai")).toMatchObject({ source: "file" });
    expect(keyStatus(NONE, null, "anthropic").source).toBe("none");
    expect(keyStatus(null, null, "gemini").source).toBe("unknown");
    const skipped: ModelVerifyResult = {
      ok: true,
      checks: [{ id: "gemini_generate", ok: true, title: "Not checked", detail: "", model: null, seconds: 0 }],
    };
    expect(keyStatus({ ...NONE, gemini_in_keychain: true }, skipped, "gemini").check).toBeNull();
    expect(keyStatus({ ...NONE, gemini_in_keychain: true }, badResult, "gemini")).toMatchObject({
      check: "failed",
      detail: "this key was refused.",
    });
  });
});

describe("KeysCard", () => {
  it("lists the five keys with what uses them and says Keychain once", () => {
    renderKeys({ ...NONE, gemini_in_keychain: true }, { result: okResult, checkedAt: Date.now() - 120_000 });
    expect(screen.getAllByRole("listitem")).toHaveLength(5);
    expect(screen.getByText("Summaries, the fallback and search by meaning")).toBeTruthy();
    expect(screen.getByText("Works, checked 2 min ago")).toBeTruthy();
    expect(screen.getAllByText(/login Keychain/)).toHaveLength(1);
    expect(screen.getByText("Keys are kept in this Mac's login Keychain, never in a file.")).toBeTruthy();
    expect(within(rowOf("OpenAI")).getByText("Not stored")).toBeTruthy();
    expect(within(rowOf("Local endpoint")).getByText("Optional, not stored")).toBeTruthy();
  });

  it("says just now for a fresh check and never a negative age", () => {
    renderKeys({ ...NONE, gemini_in_keychain: true }, { result: okResult, checkedAt: Date.now() + 60_000 });
    expect(screen.getByText("Works, checked just now")).toBeTruthy();
  });

  it("puts words beside every status, and a key with no check says where it is stored", () => {
    renderKeys({ ...NONE, anthropic_in_keychain: true, openai_file_key_present: true });
    expect(within(rowOf("Claude API")).getByText("Stored, not checked")).toBeTruthy();
    expect(within(rowOf("OpenAI")).getByText("Stored, not checked")).toBeTruthy();
    expect(within(rowOf("Gemini")).getByText("Not stored")).toBeTruthy();
    expect(within(rowOf("Claude API")).getByRole("button", { name: "Check the Claude API key now" })).toBeTruthy();
  });

  it("shows an environment key as the one used, with no Add or Replace", () => {
    renderKeys(
      { ...NONE, gemini_env: true, gemini_in_keychain: true },
      { result: okResult, checkedAt: Date.now() - 120_000 },
    );
    const row = rowOf("Gemini");
    expect(row.textContent).toContain("Set by GEMINI_API_KEY in the environment; that one is used.");
    expect(row.textContent).toContain("Works, checked 2 min ago");
    expect(row.textContent).toContain("A key stored in the Keychain is kept but not used while the variable is set.");
    expect(within(row).queryByRole("button", { name: /Replace|Add/ })).toBeNull();
    expect(within(row).getByRole("button", { name: "Check the Gemini key now" })).toBeTruthy();
  });

  it("marks a refused key in red with the check's words", () => {
    renderKeys({ ...NONE, gemini_in_keychain: true }, { result: badResult, checkedAt: Date.now() });
    const row = rowOf("Gemini");
    expect(row.className).toMatch(/\berr\b/);
    expect(row.textContent).toContain("Gemini: this key was refused.");
  });

  it("says Reading… and, if presence cannot be read, that it cannot be", () => {
    renderKeys(null);
    expect(within(rowOf("Gemini")).getByText("Reading…")).toBeTruthy();
    cleanup();
    renderKeys(null, {}, { presenceError: "Could not read secrets presence." });
    expect(within(rowOf("Gemini")).getByText("Can't read whether a key is stored.")).toBeTruthy();
  });

  it("checks once on open when a checkable key was never checked", async () => {
    const { verify } = renderKeys({ ...NONE, gemini_in_keychain: true });
    await waitFor(() => expect(verify).toHaveBeenCalledTimes(1));
  });

  it("does not check on open when a check already exists, or no checkable key is stored", async () => {
    const first = renderKeys({ ...NONE, gemini_in_keychain: true }, { result: okResult, checkedAt: Date.now() });
    await act(async () => {});
    expect(first.verify).not.toHaveBeenCalled();
    cleanup();
    const second = renderKeys(NONE);
    await act(async () => {});
    expect(second.verify).not.toHaveBeenCalled();
  });

  it("checks once on open when a stored key has only a skipped result, even with a timestamp", async () => {
    const skipped: ModelVerifyResult = {
      ok: true,
      checks: [
        { id: "openai_compat_generate", ok: true, title: "Not checked", detail: "", model: null, seconds: 0 },
      ],
    };
    const { verify } = renderKeys(
      { ...NONE, openai_compat_in_keychain: true },
      { result: skipped, checkedAt: Date.now() },
    );
    await waitFor(() => expect(verify).toHaveBeenCalledTimes(1));
    await act(async () => {});
    expect(verify).toHaveBeenCalledTimes(1);
  });

  it("gives Claude API, OpenAI and OpenRouter real checks like Gemini", () => {
    const res: ModelVerifyResult = {
      ok: true,
      checks: ["anthropic_models", "openai_models", "openrouter_models"].map((id) => ({
        id, ok: true, title: "Key works", detail: "", model: null, seconds: 1,
      })),
    };
    renderKeys(
      { ...NONE, anthropic_in_keychain: true, openai_in_keychain: true, openrouter_in_keychain: true },
      { result: res, checkedAt: Date.now() - 120_000 },
    );
    for (const name of ["Claude API", "OpenAI", "OpenRouter"]) {
      expect(within(rowOf(name)).getByText("Works, checked 2 min ago")).toBeTruthy();
      expect(within(rowOf(name)).getByRole("button", { name: `Check the ${name} key now` })).toBeTruthy();
    }
  });

  it("checks from Check now, and shows Checking… while it runs", () => {
    const first = renderKeys({ ...NONE, gemini_in_keychain: true }, { result: okResult, checkedAt: Date.now() });
    fireEvent.click(screen.getByRole("button", { name: "Check the Gemini key now" }));
    expect(first.verify).toHaveBeenCalledTimes(1);
    cleanup();
    renderKeys({ ...NONE, gemini_in_keychain: true }, { result: okResult, checkedAt: Date.now(), verifying: true });
    const busy = screen.getByRole("button", { name: "Check the Gemini key now" });
    expect(busy.textContent).toContain("Checking…");
    expect(busy).toBeDisabled();
  });

  it("adds a key through the stdin command, clears the value, then re-reads and checks", async () => {
    const { verify, onKeySaved } = renderKeys(NONE, { result: okResult, checkedAt: Date.now() });
    const add = screen.getByRole("button", { name: "Add an OpenAI key" });
    expect(add.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(add);
    expect(add.getAttribute("aria-expanded")).toBe("true");
    const input = screen.getByLabelText("Value for an OpenAI key") as HTMLInputElement;
    expect(input.type).toBe("password");
    fireEvent.change(input, { target: { value: "sk-test-123" } });
    fireEvent.click(screen.getByRole("button", { name: "Save key" }));
    await waitFor(() => expect(onKeySaved).toHaveBeenCalled());
    expect(calls).toContainEqual(["set_khipu_secret", { account: "openai_api_key", value: "sk-test-123" }]);
    expect(screen.queryByLabelText("Value for an OpenAI key")).toBeNull();
    // OpenAI has a real check now, so saving it runs the check.
    await waitFor(() => expect(verify).toHaveBeenCalledTimes(1));
  });

  it("checks after saving a Gemini key, and Enter saves while Escape cancels", async () => {
    const { verify, onKeySaved } = renderKeys({ ...NONE, gemini_in_keychain: true }, { result: okResult, checkedAt: Date.now() });
    fireEvent.click(screen.getByRole("button", { name: "Replace the Gemini key" }));
    const input = screen.getByLabelText("New value for the Gemini key");
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByLabelText("New value for the Gemini key")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Replace the Gemini key" }));
    const again = screen.getByLabelText("New value for the Gemini key");
    fireEvent.change(again, { target: { value: "AIza-new" } });
    fireEvent.keyDown(again, { key: "Enter" });
    await waitFor(() => expect(verify).toHaveBeenCalled());
    expect(onKeySaved).toHaveBeenCalled();
    expect(calls).toContainEqual(["set_khipu_secret", { account: "gemini_api_key", value: "AIza-new" }]);
  });

  it("saves the local endpoint key under its own account and shows a refusal", async () => {
    renderKeys(NONE, { result: okResult, checkedAt: Date.now() });
    fireEvent.click(screen.getByRole("button", { name: "Add a local endpoint key" }));
    invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
      calls.push([cmd, args]);
      return JSON.stringify({ ok: false, error: "Keychain is locked" });
    });
    fireEvent.change(screen.getByLabelText("Value for a local endpoint key"), { target: { value: "sk-local" } });
    fireEvent.click(screen.getByRole("button", { name: "Save key" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Keychain is locked");
    expect(calls).toContainEqual(["set_khipu_secret", { account: "openai_compat_api_key", value: "sk-local" }]);
  });
});

describe("a check that could not run", () => {
  it("shows the failure in words on a checkable stored key", () => {
    renderKeys({ ...NONE, gemini_in_keychain: true, anthropic_in_keychain: true }, { runError: "spawn failed" });
    const row = rowOf("Gemini");
    expect(row.className).toMatch(/\berr\b/);
    expect(row.textContent).toContain("Gemini: the check could not run: spawn failed");
    expect(rowOf("Claude API").className).toMatch(/\berr\b/);
    expect(rowOf("Claude API").textContent).toContain("Claude API: the check could not run: spawn failed");
    expect(rowOf("OpenAI").className).not.toMatch(/\berr\b/);
  });

  it("is not stored as a check, so the open-time check still runs next time", async () => {
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, v: string) => void store.set(key, v),
      removeItem: (key: string) => void store.delete(key),
    });
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_secrets_verify") throw new Error("spawn failed");
      throw new Error(`unexpected command ${cmd}`);
    });
    function Probe() {
      const v = useModelVerify();
      return (
        <>
          <button onClick={() => void v.verify()}>go</button>
          <output data-testid="s">{JSON.stringify([v.checkedAt, v.result, v.runError])}</output>
        </>
      );
    }
    render(<Probe />);
    fireEvent.click(screen.getByText("go"));
    await waitFor(() => expect(screen.getByTestId("s").textContent).toBe(JSON.stringify([null, null, "Error: spawn failed"])));
    expect(store.has("khipu.modelVerify.v1")).toBe(false);
    vi.unstubAllGlobals();
  });
});

describe("Capture & models, composed", () => {
  it("says the Keychain sentence once across every card", async () => {
    const { container } = render(
      <>
        <CaptureTuningCard active />
        <div className="section-card" id="summaries">
          <p id="synth-help">Which model writes the summary.</p>
          <SynthProviderChoices
            row={{ provider: "anthropic", endpoint: "", model_id: "claude-haiku-5-5" }}
            onChange={() => {}}
            presence={{ ...NONE, anthropic_in_keychain: true }}
            verifyResult={null}
            doctorSynth={null}
          />
          <SynthFallbackField provider="anthropic" doctorSynth={null} />
          <SummariserCalls active />
        </div>
        <KeysCard
          active
          presence={{ ...NONE, anthropic_in_keychain: true }}
          presenceError={null}
          verify={{ result: null, checkedAt: Date.now(), verifying: false, verify: async () => {} }}
          onKeySaved={async () => {}}
        />
      </>,
    );
    await screen.findByText("Calls today: 12 of 5,000");
    const text = container.textContent ?? "";
    expect(text.match(/login Keychain/g)).toHaveLength(1);
    expect(text).not.toMatch(/never written to the repo/);
    expect(text.match(/never in a file/g)).toHaveLength(1);
    // One field per setting, no key field inside the provider rows.
    expect(container.querySelectorAll(".choices input[type=password]")).toHaveLength(0);
  });
});

describe("replacing a key drops its old check", () => {
  it("does not leave Key works on the provider row after a rejected re-check", async () => {
    const store = new Map<string, string>();
    store.set("khipu.modelVerify.v1", JSON.stringify({ result: okResult, at: Date.now() - 60_000 }));
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, v: string) => void store.set(key, v),
      removeItem: (key: string) => void store.delete(key),
    });
    invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
      calls.push([cmd, args]);
      if (cmd === "set_khipu_secret") return JSON.stringify({ ok: true });
      if (cmd === "khipu_secrets_verify") throw new Error("spawn failed");
      throw new Error(`unexpected command ${cmd}`);
    });
    const presence = { ...NONE, gemini_in_keychain: true };
    function Page() {
      const mv = useModelVerify();
      return (
        <>
          <SynthProviderChoices
            row={{ provider: "gemini", endpoint: "", model_id: "gemini-2.5-flash" }}
            onChange={() => {}}
            presence={presence}
            verifyResult={mv.result}
            runError={mv.runError}
            doctorSynth={null}
          />
          <KeysCard active presence={presence} presenceError={null} verify={mv} onKeySaved={async () => {}} />
        </>
      );
    }
    const { container } = render(<Page />);
    expect(screen.getByText("Key works")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Replace the Gemini key" }));
    const input = screen.getByLabelText("New value for the Gemini key");
    fireEvent.change(input, { target: { value: "AIza-new" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(rowOf("Gemini").textContent).toContain("the check could not run: Error: spawn failed"));
    expect(screen.queryByText("Key works")).toBeNull();
    expect(screen.getByText("Check could not run")).toBeTruthy();
    const words = container.querySelector(".choice-key")?.textContent ?? "";
    expect(words).not.toMatch(/works/);
    expect(words).toMatch(/could not run/);
    expect(store.get("khipu.modelVerify.v1") ?? "").not.toContain("gemini_generate");
    vi.unstubAllGlobals();
  });
});
