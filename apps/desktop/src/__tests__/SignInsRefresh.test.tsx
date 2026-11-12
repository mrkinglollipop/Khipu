// Settings › Sign-ins (fix round, item 6): after a login finishes the page reads
// the Macs again, so "Needs signing in" and the button go away with the new status.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { status9B } from "./accountsFixtures";

const calls: string[] = [];
let signedIn = false;

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(async (cmd: string) => {
    calls.push(cmd);
    switch (cmd) {
      case "khipu_macs":
        return JSON.stringify({
          macs: [{
            mac: "M5", name: "M5", this_mac: true, version: "0.4.15", seen_at: "x", age_s: 0, stale: false,
            saving: true, health: null, running: null, signins: [{ harness: "claude", label: "Work", signed_in: signedIn }],
          }],
          shared: true,
        });
      case "khipu_accounts_status":
        return JSON.stringify(status9B({ accounts: [{ label: "Work", harness: "claude", home: "~/.claude-work", key: "claude|~/.claude-work" }] }));
      case "khipu_account_sign_in":
        signedIn = true;
        return JSON.stringify({ ok: true, row: { harness: "claude", account: "Work", home: "~/.claude-work", ok: true } });
      case "khipu_health": return JSON.stringify({ areas: [], agent_note: "", last_backup_at: null });
      default: return JSON.stringify({});
    }
  }),
}));
vi.mock("@tauri-apps/plugin-autostart", () => ({ isEnabled: async () => false, enable: async () => {}, disable: async () => {} }));
vi.mock("@tauri-apps/plugin-opener", () => ({ openUrl: vi.fn(async () => {}) }));
vi.mock("@tauri-apps/plugin-updater", () => ({ check: async () => null }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: async () => {} }));

import { Settings } from "../settings/Settings";
import { clearHealthCache } from "../shared/healthCache";
import type { SettingsCtx } from "../settings/context";

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
  signedIn = false;
  clearHealthCache();
});
afterEach(cleanup);

describe("Settings › Sign-ins after a login", () => {
  it("reads the Macs again, so the page stops saying Needs signing in and drops the button", async () => {
    render(<Settings ctx={ctx} active />);
    fireEvent.click(await screen.findByRole("button", { name: /^Connections/ }));
    expect(await screen.findByText("Needs signing in")).toBeTruthy();
    const macsReads = () => calls.filter((c) => c === "khipu_macs").length;
    const before = macsReads();
    fireEvent.click(await screen.findByRole("button", { name: "Sign Work in again" }));
    await waitFor(() => expect(macsReads()).toBeGreaterThan(before));
    await waitFor(() => expect(screen.queryByText("Needs signing in")).toBeNull());
    expect(screen.getByText("Signed in")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Sign Work in again" })).toBeNull();
  });
});
