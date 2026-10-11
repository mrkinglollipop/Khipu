import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

describe("App — Move", () => {
  it("UI-19 attempts a failed automatic preview once until the user chooses Try again", async () => {
    let previews = 0;
    invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
      if (cmd === "dsn_configured") return true;
      if (cmd === "khipu_db_status") return JSON.stringify({ ok: true, host_kind: "remote" });
      if (cmd === "liveness_now") return JSON.stringify({ ok: true, harnesses: {} });
      if (cmd === "khipu_db_preflight") return JSON.stringify({ ok: true, stages: [] });
      if (cmd === "khipu_db_move" && args?.dryRun === true) {
        previews += 1;
        await new Promise((resolve) => setTimeout(resolve, 10));
        return JSON.stringify({ ok: false, error: "target refused" });
      }
      if (cmd === "run_khipu") return JSON.stringify({ ok: true, capture_liveness: { ok: true, harnesses: {} }, claude_homes: { homes: [] }, counts: {}, recent: [] });
      return JSON.stringify({ ok: true });
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    fireEvent.click(screen.getByRole("button", { name: /Move this memory to another database/ }));
    fireEvent.change(screen.getByLabelText("Target connection string"), { target: { value: "postgresql://target" } });
    fireEvent.click(screen.getByRole("button", { name: "Check the target" }));
    await screen.findByText("target refused");
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(previews).toBe(1);
    expect(screen.queryByText("Reading table counts from the target…")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(previews).toBe(2));
  });

  it("UI-20 does not offer an unsupported certificate picker for moves", async () => {
    invokeMock.mockResolvedValue(JSON.stringify({ ok: true, host_kind: "remote" }));
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    fireEvent.click(screen.getByRole("button", { name: /Move this memory to another database/ }));
    expect(screen.getByText(/Custom certificate files are not supported for moves yet/)).toBeInTheDocument();
    expect(screen.queryByText(/destination already trusts/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Certificate file/ })).not.toBeInTheDocument();
  });
});
