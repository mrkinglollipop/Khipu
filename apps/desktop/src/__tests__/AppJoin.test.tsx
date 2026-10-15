import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import App from "../App";
import { WELCOME_DONE_KEY } from "../Welcome";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke: (...args: unknown[]) => invokeMock(...args) }));
const dialog = vi.hoisted(() => ({ savePath: null as string | null }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn(async () => null), save: vi.fn(async () => dialog.savePath) }));
vi.mock("@tauri-apps/api/path", () => ({ resourceDir: vi.fn(async () => "/Resources") }));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: vi.fn(async () => "0.4.0") }));
vi.mock("@tauri-apps/plugin-updater", () => ({ check: vi.fn(async () => null) }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: vi.fn(async () => {}) }));

beforeEach(() => {
  dialog.savePath = null;
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
    fireEvent.change(await screen.findByLabelText("Join kit passphrase"), { target: { value: "phrase" } });
    fireEvent.click(await screen.findByRole("button", { name: /Advertise nearby/ }));
    expect(await screen.findByText(/Nearby setup started/)).toBeInTheDocument();
    expect(screen.getByText(/Connection has not been checked/)).toBeInTheDocument();
    expect(screen.queryByText(/Advertising nearby/)).not.toBeInTheDocument();
  });
});

function mockRemoteHub() {
  invokeMock.mockImplementation(async (cmd: string) => {
    if (cmd === "dsn_configured") return true;
    if (cmd === "khipu_db_status") return JSON.stringify({ ok: true, host_kind: "remote" });
    if (cmd === "liveness_now") return JSON.stringify({ ok: true, harnesses: {} });
    if (cmd === "join_advertise") return JSON.stringify({ ok: true, pin: "123456", timeout_sec: 600 });
    if (cmd === "join_export") return JSON.stringify({ ok: true, out: "/tmp/k.khipujoin", expected: {} });
    if (cmd === "run_khipu") return JSON.stringify({ ok: true, capture_liveness: { ok: true, harnesses: {} }, claude_homes: { homes: [] }, counts: {}, recent: [] });
    return JSON.stringify({ ok: true });
  });
}

async function openAnotherMac() {
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
  fireEvent.click(screen.getByRole("tab", { name: "Another Mac" }));
  await screen.findByText("Set up another Mac");
  // IntegrationsPanel has its own "Save join kit…" shortcut; scope to this card.
  return within(screen.getByText("Set up another Mac").closest(".section-card") as HTMLElement);
}

describe("App — Join kit passphrase", () => {
  it("keeps Save join kit and Advertise disabled until a passphrase is typed", async () => {
    mockRemoteHub();
    const section = await openAnotherMac();
    const save = section.getByRole("button", { name: /Save join kit/ });
    const advertise = section.getByRole("button", { name: /Advertise nearby/ });
    expect(save).toBeDisabled();
    expect(advertise).toBeDisabled();
    expect(screen.getByText("Type a passphrase to save or advertise a join kit.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Join kit passphrase"), { target: { value: "  " } });
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Join kit passphrase"), { target: { value: "correct horse" } });
    expect(save).toBeEnabled();
    expect(advertise).toBeEnabled();
    expect(screen.queryByText(/Anyone who gets this file/)).not.toBeInTheDocument();
  });

  it("enables Save join kit with the unencrypted opt-out and shows the warning", async () => {
    mockRemoteHub();
    const section = await openAnotherMac();
    const save = section.getByRole("button", { name: /Save join kit/ });
    expect(screen.queryByText(/Anyone who gets this file/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "I understand: make it unencrypted" }));
    expect(save).toBeEnabled();
    expect(
      screen.getByText("Anyone who gets this file can read the database password and API keys."),
    ).toBeInTheDocument();
    fireEvent.click(section.getByRole("button", { name: /Advertise nearby/ }));
    expect(await screen.findByText(/Nearby setup started/)).toBeInTheDocument();
    expect(invokeMock).toHaveBeenCalledWith("join_advertise", {
      passphrase: "",
      timeout: 600,
      allowUnencrypted: true,
    });
  });
});

describe("App — Join kit passphrase is not kept", () => {
  it("clears the passphrase after a successful export", async () => {
    mockRemoteHub();
    dialog.savePath = "/tmp/k.khipujoin";
    const section = await openAnotherMac();
    const field = section.getByLabelText("Join kit passphrase") as HTMLInputElement;
    fireEvent.change(field, { target: { value: "correct horse" } });
    fireEvent.click(section.getByRole("button", { name: /Save join kit/ }));
    expect(await screen.findByText(/Saved an encrypted join kit/)).toBeInTheDocument();
    expect(invokeMock).toHaveBeenCalledWith("join_export", {
      passphrase: "correct horse",
      outPath: "/tmp/k.khipujoin",
      allowUnencrypted: false,
    });
    expect(field.value).toBe("");
  });

  it("clears the passphrase after advertising starts", async () => {
    mockRemoteHub();
    const section = await openAnotherMac();
    const field = section.getByLabelText("Join kit passphrase") as HTMLInputElement;
    fireEvent.change(field, { target: { value: "correct horse" } });
    fireEvent.click(section.getByRole("button", { name: /Advertise nearby/ }));
    expect(await screen.findByText(/Nearby setup started/)).toBeInTheDocument();
    expect(field.value).toBe("");
  });
});
