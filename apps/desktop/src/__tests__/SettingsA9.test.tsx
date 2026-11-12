import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const calls: Array<{ cmd: string; args?: unknown }> = [];
let macsDoc: unknown;
let libsOn = false;
let healthMode: "ok" | "slow" | "fail" = "ok";
let healthResolve: ((v: string) => void) | null = null;
let healthNote = "NOTE-TEXT";
let searchProfiles: unknown[] | null = [];

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(async (cmd: string, args?: unknown) => {
    calls.push({ cmd, args });
    switch (cmd) {
      case "khipu_macs": return JSON.stringify(macsDoc);
      case "khipu_health":
        if (healthMode === "fail") throw new Error("hub down");
        if (healthMode === "slow") return new Promise<string>((r) => { healthResolve = r; });
        return JSON.stringify({ areas: [], agent_note: healthNote, last_backup_at: null });
      case "khipu_library_list":
        return JSON.stringify({ libraries: [{ name: "Bible", enabled: libsOn }, { name: "Notes", enabled: libsOn }] });
      case "khipu_library_enable": libsOn = true; return JSON.stringify({ ok: true });
      case "khipu_library_disable": libsOn = false; return JSON.stringify({ ok: true });
      case "khipu_embed_profiles_list": return JSON.stringify(searchProfiles === null ? {} : { profiles: searchProfiles });
      default: return JSON.stringify({});
    }
  }),
}));
vi.mock("@tauri-apps/plugin-autostart", () => ({ isEnabled: async () => false, enable: async () => {}, disable: async () => {} }));
vi.mock("@tauri-apps/plugin-opener", () => ({ openUrl: vi.fn(async () => {}) }));
vi.mock("@tauri-apps/plugin-updater", () => ({ check: async () => null }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: async () => {} }));

import { Settings, ROW_IDS, settingsPage } from "../settings/Settings";
import { clearHealthCache, saveHealth } from "../shared/healthCache";
import type { SettingsCtx } from "../settings/context";

const row = (name: string, here: boolean, ageS: number) => ({
  mac: name, name, this_mac: here, version: "0.4.15", seen_at: "x", age_s: ageS, stale: ageS > 900,
  saving: true, health: null, running: null, signins: [{ harness: "claude", label: "Work", signed_in: true }],
});

const ctx = {
  appVersion: "0.4.15", thisMac: "M5", runKhipu: async () => "{}", onToast: () => {},
  liveness: null, recallProbe: null, t3: null, refreshHealth: () => {}, onSearchT3Captures: () => {}, onOpenGuide: () => {},
  synth: { provider: "anthropic", endpoint: "", model_id: "claude-haiku-5-5" }, modelsError: null, modelsSaving: false,
  modelsMsg: null, onSynthChange: () => {}, onSaveModels: () => {}, secretsPresence: null, secretsPresenceMsg: null,
  modelCheck: { result: null, runError: null, verifying: false, verify: async () => {} },
  onKeySaved: () => {}, doctorSynth: null, dsnOk: true, dbReachable: true, onMoveMemory: vi.fn(), onOpenGuides: vi.fn(),
  onFeedback: vi.fn(), onOpenOldSettings: () => {},
} as unknown as SettingsCtx;

beforeEach(() => {
  calls.length = 0;
  libsOn = false;
  healthMode = "ok";
  healthResolve = null;
  healthNote = "NOTE-TEXT";
  searchProfiles = [];
  clearHealthCache();
  macsDoc = { macs: [row("M5", true, 0), row("M1", false, 2400)], shared: true };
});
afterEach(cleanup);

describe("Settings (A9)", () => {
  it("lists only the five approved pages", () => {
    render(<Settings ctx={ctx} active />);
    expect(ROW_IDS).toHaveLength(5);
    const list = screen.getByRole("region", { name: "Settings" });
    expect(list.querySelectorAll("button.st-row")).toHaveLength(5);
  });

  it("keeps all thirteen legacy links and groups their controls on the new pages", async () => {
    const destinations = { macs: "connections", updates: "general", signins: "connections", apps: "connections", summaries: "models", meaning: "general", books: "general", backups: "storage", where: "storage", look: "general", login: "general", help: "help", agent: "help" };
    for (const [old, page] of Object.entries(destinations)) expect(settingsPage(old)).toBe(page);
    render(<Settings ctx={ctx} active />);
    const nav = screen.getByRole("region", { name: "Settings" });
    expect(Array.from(nav.querySelectorAll("button.st-row")).map((b) => b.querySelector(".st-rk")?.textContent)).toEqual(["General", "Models", "Storage", "Connections", "Help"]);
    for (const old of ["Your Macs", "Sign-ins", "Apps that use Khipu", "Models and API keys", "Where memory is kept", "Details for an agent"]) expect(nav.textContent).not.toContain(old);
    for (const group of ["Appearance", "Starting up", "Search", "Updates"]) expect(screen.getByRole("region", { name: group })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^Models/ }));
    for (const group of ["Summaries", "Search by meaning", "API keys"]) expect(screen.getByRole("region", { name: group })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^Storage/ }));
    for (const group of ["Database", "Backups"]) expect(screen.getByRole("region", { name: group })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^Connections/ }));
    for (const group of ["Apps", "Accounts", "Macs"]) expect(screen.getByRole("region", { name: group })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^Help/ }));
    expect(screen.getByRole("region", { name: "When something is wrong" })).toBeTruthy();
  });

  it("shows a quiet other Mac as quiet, in words", async () => {
    render(<Settings ctx={ctx} active />);
    fireEvent.click(screen.getByRole("button", { name: /^Connections/ }));
    expect(await screen.findByText(/last heard from 40 min ago, so this may be out of date/)).toBeTruthy();
    expect(screen.getAllByText("Not heard from lately").length).toBeGreaterThan(0);
  });

  it("says the other Mac is absent when only this Mac reported", async () => {
    macsDoc = { macs: [row("M5", true, 0)], shared: true, note: "No other Mac has reported yet, so only this Mac is shown." };
    render(<Settings ctx={ctx} active />);
    fireEvent.click(screen.getByRole("button", { name: /^Connections/ }));
    expect((await screen.findAllByText(/No other Mac has reported yet/)).length).toBeGreaterThan(0);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("the books switch enables then disables each library with the old commands", async () => {
    render(<Settings ctx={ctx} active />);
    fireEvent.click(screen.getByRole("button", { name: /^General/ }));
    const sw = await screen.findByRole("switch", { name: "Include your books" });
    fireEvent.click(sw);
    await waitFor(() => expect(calls.filter((c) => c.cmd === "khipu_library_enable").map((c) => c.args)).toEqual([{ name: "Bible" }, { name: "Notes" }]));
    await waitFor(() => expect((screen.getByRole("switch", { name: "Include your books" }) as HTMLInputElement).checked).toBe(true));
    fireEvent.click(screen.getByRole("switch", { name: "Include your books" }));
    await waitFor(() => expect(calls.filter((c) => c.cmd === "khipu_library_disable")).toHaveLength(2));
  });

  it("copies the agent note", async () => {
    const writeText = vi.fn(async () => {});
    Object.assign(navigator, { clipboard: { writeText } });
    render(<Settings ctx={ctx} active />);
    fireEvent.click(screen.getByRole("button", { name: /^Help/ }));
    await screen.findByText("NOTE-TEXT");
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("NOTE-TEXT"));
  });

  it("Help opens the website and keeps Guides and Feedback", async () => {
    const { openUrl } = await import("@tauri-apps/plugin-opener");
    render(<Settings ctx={ctx} active />);
    fireEvent.click(screen.getByRole("button", { name: /^Help/ }));
    fireEvent.click(screen.getByRole("button", { name: "Open the website" }));
    expect(openUrl).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Guides in this app" }));
    expect(ctx.onOpenGuides).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Send feedback" }));
    expect(ctx.onFeedback).toHaveBeenCalled();
  });

  it("the move button calls the old move flow", () => {
    render(<Settings ctx={ctx} active />);
    fireEvent.click(screen.getByRole("button", { name: /^Storage/ }));
    fireEvent.click(screen.getByRole("button", { name: /Move this memory to another database/ }));
    expect(ctx.onMoveMemory).toHaveBeenCalled();
  });

  describe("the health answer", () => {
    const openAgent = () => fireEvent.click(screen.getByRole("button", { name: /^Help/ }));

    it("shows the last answer at once with its time, then swaps the new one in", async () => {
      saveHealth({ areas: [], agent_note: "OLD-NOTE" }, Date.now() - 4 * 60000);
      healthMode = "slow";
      render(<Settings ctx={ctx} active />);
      openAgent();
      expect(screen.getByText("OLD-NOTE")).toBeTruthy();
      expect(screen.getByText(/Checked 4 min ago/)).toBeTruthy();
      expect((screen.getByRole("button", { name: "Copy" }) as HTMLButtonElement).disabled).toBe(false);
      await waitFor(() => expect(healthResolve).not.toBeNull());
      healthResolve!(JSON.stringify({ areas: [], agent_note: "NEW-NOTE" }));
      await screen.findByText("NEW-NOTE");
      expect(screen.queryByText("OLD-NOTE")).toBeNull();
      expect(screen.getByText(/Checked just now/)).toBeTruthy();
    });

    it("a failed refresh keeps the old answer and shows no error", async () => {
      saveHealth({ areas: [], agent_note: "OLD-NOTE" }, Date.now() - 4 * 60000);
      healthMode = "fail";
      render(<Settings ctx={ctx} active />);
      openAgent();
      await waitFor(() => expect(calls.some((c) => c.cmd === "khipu_health")).toBe(true));
      await new Promise((r) => setTimeout(r, 20));
      expect(screen.getByText("OLD-NOTE")).toBeTruthy();
      expect(screen.getByText(/Checked 4 min ago/)).toBeTruthy();
      expect(screen.queryByText(/could not run its checks/)).toBeNull();
      expect(screen.queryByRole("alert")).toBeNull();
    });

    it("first open with no earlier answer shows checking and keeps Copy disabled until a note arrives", async () => {
      healthMode = "slow";
      render(<Settings ctx={ctx} active />);
      openAgent();
      expect(screen.getByText(/Checking… this can take a minute/)).toBeTruthy();
      expect((screen.getByRole("button", { name: "Copy" }) as HTMLButtonElement).disabled).toBe(true);
      await waitFor(() => expect(healthResolve).not.toBeNull());
      healthResolve!(JSON.stringify({ areas: [], agent_note: "FRESH" }));
      await screen.findByText("FRESH");
      expect((screen.getByRole("button", { name: "Copy" }) as HTMLButtonElement).disabled).toBe(false);
    });

    it("Backups and Where memory is kept show the last answer's time too", async () => {
      saveHealth({ areas: [], last_backup_at: null }, Date.now() - 4 * 60000);
      healthMode = "slow";
      render(<Settings ctx={ctx} active />);
      fireEvent.click(screen.getByRole("button", { name: /^Storage/ }));
      expect(screen.getAllByText(/Checked 4 min ago/)).toHaveLength(2);
    });
  });

  it("keeps an unreadable search status from crashing General", async () => {
    searchProfiles = null;
    render(<Settings ctx={ctx} active />);
    expect(await screen.findByText("Khipu could not read the search model just now.")).toBeTruthy();
    expect(screen.getByRole("region", { name: "Appearance" })).toBeTruthy();
  });

  it.each([["cloud", "Google API key"], ["local", "This Mac or my network"], ["custom-provider", "custom-provider"]])("shows active embedding model and %s source without rewriting it", async (provider, source) => {
    searchProfiles = [{ id: "custom@768", provider, model: "custom-search-model", is_active: true, dim: 768,
      coverage: { memory: { total: 10, embedded: 8, missing: 2, stale: 0, pct: 80 } } }];
    render(<Settings ctx={ctx} active />);
    fireEvent.click(screen.getByRole("button", { name: /^Models/ }));
    expect(await screen.findByText("custom-search-model")).toBeTruthy();
    expect(screen.getByText(source)).toBeTruthy();
    expect(screen.getByText("80% of your memory (2 still to do)")).toBeTruthy();
  });

  it("Search by meaning says in one sentence what is true", async () => {
    render(<Settings ctx={ctx} active />);
    fireEvent.click(screen.getByRole("button", { name: /^General/ }));
    expect(await screen.findByText(/Search by meaning is off because no search model is set up/)).toBeTruthy();
  });
});
