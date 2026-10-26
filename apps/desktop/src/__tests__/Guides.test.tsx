// The Guides page, the Grok Bot guide, the "How this works" popover and Home's
// hint. Tauri is stubbed: `invoke` answers each fixed command the guide uses.
// Every key and address below is made up.
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { invoke } from "@tauri-apps/api/core";
import { GuidesPage } from "../GuidesPage";
import { HowThisWorks } from "../HowThisWorks";
import { SetupHint } from "../SetupHint";
import { useGuideSignals, gatewayCheckWords, runGatewayCheck } from "../guideSignals";
import { guideStates, hintState, startItems, DEFAULT_PREFS } from "../guidesModel";
import type { GuideId } from "../guidesModel";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));

const ADDRESS = "https://khipu.example.org";
const KEY_LOOKALIKE = "made-up-key-9f3a7c11d2";
const SETTINGS = { mcpServers: { "khipu-cloud": { url: `${ADDRESS}/mcp`, headers: { Authorization: "Bearer ${env:KHIPU_GATEWAY_TOKEN}" } } } };

type World = { address: string; keySaved: boolean; copy: { ok: boolean; error?: string }; verify: unknown };
let world: World;
const calls: Array<{ cmd: string; args?: unknown }> = [];
/** App-setting stand-in for guides prefs (data_dir file in production). */
let prefsStore = "{}";

beforeEach(() => {
  world = { address: ADDRESS, keySaved: true, copy: { ok: true }, verify: [{ harness: "grok_bot", detected: true, components: { mcp: { ok: true } } }] };
  calls.length = 0;
  prefsStore = "{}";
  const store = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  });
  vi.mocked(invoke).mockImplementation(async (cmd: string, args?: unknown) => {
    calls.push({ cmd, args });
    switch (cmd) {
      case "khipu_config_show":
        return JSON.stringify({ gateway_url: world.address, config: { gateway_url: world.address } });
      case "khipu_gateway_token_status":
        return JSON.stringify({ path: "/tmp/made-up/gateway-token", exists: world.keySaved });
      case "khipu_recall_log":
        return JSON.stringify([]);
      case "khipu_gateway_token_copy":
        // Deliberately put a lookalike in a field a leaky UI might render.
        return JSON.stringify(
          world.copy.ok
            ? { ok: true, clear_after_s: 60, token: KEY_LOOKALIKE }
            : { ok: false, error: world.copy.error, token: KEY_LOOKALIKE },
        );
      case "khipu_guides_prefs_get":
        return prefsStore;
      case "khipu_guides_prefs_set": {
        prefsStore = String((args as { json?: string })?.json ?? "{}");
        return prefsStore;
      }
      case "khipu_gateway_token_set":
        world.keySaved = true;
        return JSON.stringify({ ok: true, exists: true });
      case "khipu_config_set":
        world.address = String((args as { value?: string })?.value ?? "");
        return JSON.stringify({ ok: true });
      default:
        return "{}";
    }
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function makeRunKhipu() {
  return vi.fn(async (args: string[]) => {
    if (args[1] === "install") return JSON.stringify([{ harness: "grok_bot", detected: true, changes: [], account_level: { config: SETTINGS } }]);
    if (args[1] === "verify") return JSON.stringify(world.verify);
    return "{}";
  });
}

/** The page the way App mounts it: signals from the real hook. */
function Harness({ runKhipu, initial = null }: { runKhipu: (a: string[]) => Promise<string>; initial?: { id: GuideId; step?: number } | null }) {
  const signals = useGuideSignals("t");
  const items = startItems({ liveness: null, gatewayCheckOk: signals.prefs.grokCheck?.ok === true, episodes: 0, accountsRead: 0, agentAsked: signals.agentAsked });
  const states = guideStates({ liveness: null, gatewayCheckOk: false, episodes: 0, accountsRead: 0, agentAsked: false, grok: signals.grok, problems: 0 });
  const [open, setOpen] = useState(initial);
  return (
    <GuidesPage
      runKhipu={runKhipu}
      signals={signals}
      startItems={items}
      states={states}
      open={open}
      onOpen={(id) => setOpen({ id })}
      onBack={() => setOpen(null)}
      onTarget={() => {}}
    />
  );
}

describe("Guides page", () => {
  it("lists guides in groups with live state and filters by search", async () => {
    render(<Harness runKhipu={makeRunKhipu()} />);
    expect(await screen.findByText("Connect your agents")).toBeInTheDocument();
    expect(screen.getByText("Keep it safe")).toBeInTheDocument();
    expect(screen.getByText("0 of 4 done")).toBeInTheDocument();
    // Address and key alone (shared with Aegis) do not mean Grok Bot setup started.
    expect(await screen.findByRole("button", { name: /Grok Bot/ })).toHaveTextContent("Not set up");

    fireEvent.change(screen.getByLabelText("Search guides"), { target: { value: "backup" } });
    expect(screen.getByText("Backups and copies on this Mac")).toBeInTheDocument();
    expect(screen.queryByText("Codex")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search guides"), { target: { value: "zzzz" } });
    expect(screen.getByText(/No guide matches/)).toBeInTheDocument();
  });

  it("hides Start here, remembers it, and brings it back", async () => {
    render(<Harness runKhipu={makeRunKhipu()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Hide" }));
    expect(screen.queryByText("Start here")).not.toBeInTheDocument();
    await waitFor(() => expect(JSON.parse(prefsStore)).toMatchObject({ startHidden: true, hintHidden: true }));
    fireEvent.click(screen.getByRole("button", { name: "Show setup steps again" }));
    expect(screen.getByText("Start here")).toBeInTheDocument();
  });

  it("opens an outline guide, and Grok Build says it is not supported yet", async () => {
    render(<Harness runKhipu={makeRunKhipu()} />);
    fireEvent.click(await screen.findByRole("button", { name: /Grok Build/ }));
    expect(screen.getAllByText("Not supported yet").length).toBeGreaterThan(0);
    expect(screen.getByText(/no Khipu connection of its own yet/)).toBeInTheDocument();
  });
});

describe("Grok Bot guide", () => {
  it("opens at the first unfinished step and shows the CLI's server settings with Copy", async () => {
    render(<Harness runKhipu={makeRunKhipu()} initial={{ id: "grok_bot" }} />);
    const step3 = (await screen.findByText("Add the key to Cursor as a secret")).closest("li");
    await waitFor(() => expect(step3).toHaveAttribute("aria-current", "step"));
    const cfg = await screen.findByLabelText("Server settings to paste");
    expect(cfg.textContent).toContain('"khipu-cloud"');
    expect(cfg.textContent).toContain("${env:KHIPU_GATEWAY_TOKEN}");
    expect(screen.getByRole("button", { name: "Copy the server settings" })).toBeInTheDocument();
  });

  it("copies the key through the app-only command and never shows or receives it", async () => {
    render(<Harness runKhipu={makeRunKhipu()} initial={{ id: "grok_bot" }} />);
    fireEvent.click(await screen.findByRole("button", { name: /Copy gateway key/ }));
    expect(await screen.findByText(/clears it from your clipboard in 60 seconds/)).toBeInTheDocument();
    const copyCalls = calls.filter((c) => c.cmd === "khipu_gateway_token_copy");
    expect(copyCalls).toHaveLength(1);
    expect(copyCalls[0].args).toBeUndefined();
    // No command that could return the key was used, and the page holds none.
    expect(calls.some((c) => c.cmd === "khipu_gateway_token_get")).toBe(false);
    expect(document.body.textContent).not.toContain(KEY_LOOKALIKE);
    for (const input of Array.from(document.querySelectorAll("input"))) {
      expect((input as HTMLInputElement).value).not.toContain(KEY_LOOKALIKE);
    }
  });

  it("says plainly when there is no key to copy", async () => {
    world.keySaved = false;
    world.copy = { ok: false, error: "No gateway key is saved on this Mac yet." };
    render(<Harness runKhipu={makeRunKhipu()} initial={{ id: "grok_bot" }} />);
    fireEvent.click(await screen.findByRole("button", { name: /Copy gateway key/ }));
    expect(await screen.findAllByText(/No gateway key is saved on this Mac yet/)).not.toHaveLength(0);
    // With no key, the existing save field is offered right there.
    expect(await screen.findByLabelText("Bearer token")).toBeInTheDocument();
  });

  it("'I've added it' moves focus to the next step without drawing a check mark", async () => {
    render(<Harness runKhipu={makeRunKhipu()} initial={{ id: "grok_bot" }} />);
    const step3 = (await screen.findByText("Add the key to Cursor as a secret")).closest("li") as HTMLElement;
    fireEvent.click(within(step3).getByRole("button", { name: "I've added it" }));
    const step4 = screen.getByText("Tell Cursor where Khipu is").closest("li");
    await waitFor(() => expect(step4).toHaveAttribute("aria-current", "step"));
    expect(step3).not.toHaveClass("done");
    expect(within(step3).getByText(/You said you added it/)).toBeInTheDocument();
  });

  it("Check the gateway runs the existing verify and words the answer", async () => {
    const run = makeRunKhipu();
    render(<Harness runKhipu={run} initial={{ id: "grok_bot" }} />);
    fireEvent.click(await screen.findByRole("button", { name: "Check the gateway" }));
    expect(await screen.findByText(/Gateway answered and accepted the key/)).toBeInTheDocument();
    expect(run).toHaveBeenCalledWith(["integrations", "verify", "grok_bot"]);
    await waitFor(() => expect(JSON.parse(prefsStore).grokCheck).toMatchObject({ ok: true, address: ADDRESS }));
  });

  it("maps a refused key and an unreachable gateway to their troubleshooting words", async () => {
    const refused = await runGatewayCheck(async () =>
      JSON.stringify([{ detected: true, components: { mcp: { ok: false, http_status: 401, error: "HTTP 401: Unauthorized" } } }]),
    );
    expect(gatewayCheckWords(refused)?.text).toMatch(/refused the key/);
    const silent = await runGatewayCheck(async () =>
      JSON.stringify([{ detected: true, components: { mcp: { ok: false, http_status: null, error: "URLError" } } }]),
    );
    expect(gatewayCheckWords(silent)?.text).toMatch(/didn't answer/);
    const nothing = await runGatewayCheck(async () => JSON.stringify([{ detected: false }]));
    expect(nothing.ok).toBe(false);
    expect(gatewayCheckWords(nothing)?.text).toMatch(/no gateway address/);
  });

  it("lists what it can and can't do, and says per-app keys and the 'reached Khipu' line are coming", async () => {
    render(<Harness runKhipu={makeRunKhipu()} initial={{ id: "grok_bot" }} />);
    expect(await screen.findByText("It can")).toBeInTheDocument();
    expect(screen.getByText("It can't")).toBeInTheDocument();
    expect(screen.getByText(/Grok Bot only remembers what it saves/)).toBeInTheDocument();
    expect(screen.getByText(/Coming:/)).toBeInTheDocument();
  });
});

describe("How this works", () => {
  it("opens beside the card, opens the guide, and Escape closes it", () => {
    const onOpen = vi.fn();
    render(<HowThisWorks title="How Grok Bot connects" items={[{ term: "Nothing is installed", text: "It runs in the cloud." }]} onOpenGuide={onOpen} />);
    const link = screen.getByRole("button", { name: /How this works/ });
    expect(link).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(link);
    expect(link).toHaveAttribute("aria-expanded", "true");
    const dialog = screen.getByRole("dialog", { name: "How Grok Bot connects" });
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.click(link);
    fireEvent.click(screen.getByRole("button", { name: "Open the setup guide" }));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});

describe("Home hint", () => {
  const items = startItems({ liveness: null, gatewayCheckOk: false, episodes: 0, accountsRead: 0, agentAsked: false });

  it("names the next step with one action, a link to all steps, and a way to hide it", () => {
    const onAct = vi.fn();
    const onSeeAll = vi.fn();
    const onHide = vi.fn();
    render(<SetupHint state={hintState(items, DEFAULT_PREFS)} onAct={onAct} onSeeAll={onSeeAll} onHide={onHide} onDismissAllSet={() => {}} />);
    expect(screen.getByText(/Next: connect your first agent/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Connect an agent" }));
    expect(onAct).toHaveBeenCalledWith("connect");
    fireEvent.click(screen.getByRole("button", { name: "See all setup steps" }));
    expect(onSeeAll).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Hide this hint" }));
    expect(onHide).toHaveBeenCalled();
  });

  it("renders nothing once hidden", () => {
    const { container } = render(
      <SetupHint state={hintState(items, { ...DEFAULT_PREFS, hintHidden: true })} onAct={() => {}} onSeeAll={() => {}} onHide={() => {}} onDismissAllSet={() => {}} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
