import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import App from "../App";
import { WELCOME_DONE_KEY } from "../Welcome";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke: (...args: unknown[]) => invokeMock(...args) }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn(async () => null), save: vi.fn(async () => null) }));
vi.mock("@tauri-apps/api/path", () => ({ resourceDir: vi.fn(async () => "/Resources") }));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: vi.fn(async () => "0.4.0") }));
vi.mock("@tauri-apps/plugin-updater", () => ({ check: vi.fn(async () => null) }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: vi.fn(async () => {}) }));

beforeEach(() => {
  invokeMock.mockReset();
  vi.stubGlobal("localStorage", { getItem: (key: string) => key === WELCOME_DONE_KEY ? "1" : null, setItem: () => {}, removeItem: () => {}, clear: () => {} });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("App — Join advertise", () => {
  it("RS-5 treats the first advertise banner as setup started, not confirmed nearby advertising", async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "dsn_configured") return true;
      if (cmd === "khipu_db_status") return JSON.stringify({ ok: true, host_kind: "remote" });
      if (cmd === "liveness_now") return JSON.stringify({ ok: true, harnesses: {} });
      if (cmd === "join_advertise") return JSON.stringify({ ok: true, pin: "123456", timeout_sec: 600 });
      if (cmd === "run_khipu") return JSON.stringify({ ok: true, capture_liveness: { ok: true, harnesses: {} }, claude_homes: { homes: [] }, counts: {}, recent: [] });
      return JSON.stringify({ ok: true });
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    fireEvent.click(screen.getByRole("tab", { name: "Another Mac" }));
    fireEvent.click(await screen.findByRole("button", { name: /Advertise nearby/ }));
    expect(await screen.findByText(/Nearby setup started/)).toBeInTheDocument();
    expect(screen.getByText(/Connection has not been checked/)).toBeInTheDocument();
    expect(screen.queryByText(/Advertising nearby/)).not.toBeInTheDocument();
  });
});
