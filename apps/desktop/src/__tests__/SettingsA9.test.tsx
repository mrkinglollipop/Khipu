import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const calls: Array<{ cmd: string; args?: unknown }> = [];
let macsDoc: unknown;
let libsOn = false;

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(async (cmd: string, args?: unknown) => {
    calls.push({ cmd, args });
    switch (cmd) {
      case "khipu_macs": return JSON.stringify(macsDoc);
      case "khipu_health": return JSON.stringify({ areas: [], agent_note: "NOTE-TEXT", last_backup_at: null });
      case "khipu_library_list":
        return JSON.stringify({ libraries: [{ name: "Bible", enabled: libsOn }, { name: "Notes", enabled: libsOn }] });
      case "khipu_library_enable": libsOn = true; return JSON.stringify({ ok: true });
      case "khipu_library_disable": libsOn = false; return JSON.stringify({ ok: true });
      case "khipu_embed_profiles_list": return JSON.stringify({ profiles: [] });
      default: return JSON.stringify({});
    }
  }),
}));
vi.mock("@tauri-apps/plugin-autostart", () => ({ isEnabled: async () => false, enable: async () => {}, disable: async () => {} }));
vi.mock("@tauri-apps/plugin-opener", () => ({ openUrl: vi.fn(async () => {}) }));
vi.mock("@tauri-apps/plugin-updater", () => ({ check: async () => null }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: async () => {} }));

import { Settings, ROW_IDS } from "../settings/Settings";
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
  macsDoc = { macs: [row("M5", true, 0), row("M1", false, 2400)], shared: true };
});
afterEach(cleanup);

describe("Settings (A9)", () => {
  it("lists the 13 rows", () => {
    render(<Settings ctx={ctx} active />);
    expect(ROW_IDS).toHaveLength(13);
    const list = screen.getByRole("region", { name: "Settings" });
    expect(list.querySelectorAll("button.st-row")).toHaveLength(13);
  });

  it("shows a quiet other Mac as quiet, in words", async () => {
    render(<Settings ctx={ctx} active />);
    expect(await screen.findByText(/last heard from 40 min ago, so this may be out of date/)).toBeTruthy();
    expect(screen.getAllByText("Not heard from lately").length).toBeGreaterThan(0);
  });

  it("says the other Mac is absent when only this Mac reported", async () => {
    macsDoc = { macs: [row("M5", true, 0)], shared: true, note: "No other Mac has reported yet, so only this Mac is shown." };
    render(<Settings ctx={ctx} active />);
    expect(await screen.findByText(/No other Mac has reported yet/)).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("the books switch enables then disables each library with the old commands", async () => {
    render(<Settings ctx={ctx} active />);
    fireEvent.click(screen.getByRole("button", { name: /Include your books in search/ }));
    const sw = await screen.findByRole("switch", { name: "Include your books in search" });
    fireEvent.click(sw);
    await waitFor(() => expect(calls.filter((c) => c.cmd === "khipu_library_enable").map((c) => c.args)).toEqual([{ name: "Bible" }, { name: "Notes" }]));
    await waitFor(() => expect((screen.getByRole("switch", { name: "Include your books in search" }) as HTMLInputElement).checked).toBe(true));
    fireEvent.click(screen.getByRole("switch", { name: "Include your books in search" }));
    await waitFor(() => expect(calls.filter((c) => c.cmd === "khipu_library_disable")).toHaveLength(2));
  });

  it("copies the agent note", async () => {
    const writeText = vi.fn(async () => {});
    Object.assign(navigator, { clipboard: { writeText } });
    render(<Settings ctx={ctx} active />);
    fireEvent.click(screen.getByRole("button", { name: /Details for an agent/ }));
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
    fireEvent.click(screen.getByRole("button", { name: /Where memory is kept/ }));
    fireEvent.click(screen.getByRole("button", { name: /Move this memory to another database/ }));
    expect(ctx.onMoveMemory).toHaveBeenCalled();
  });
});
