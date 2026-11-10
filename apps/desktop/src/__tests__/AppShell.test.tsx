// The app in the new window (slice A1): the places show the old screens, the
// old screens sit under Older screens, and first-run Welcome still opens.
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { openScreen } from "./nav";

const invokeMock = vi.hoisted(() => vi.fn());
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: async () => "0.4.15" }));
vi.mock("@tauri-apps/plugin-updater", () => ({ check: async () => null }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: async () => {} }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ save: async () => null, open: async () => null, ask: async () => false, message: async () => {} }));
vi.mock("@tauri-apps/api/path", () => ({ resourceDir: async () => "/Applications/Khipu.app/Contents/Resources" }));

function answers(configured: boolean) {
  invokeMock.mockImplementation(async (cmd: string) => {
    if (cmd === "dsn_configured") return configured;
    return JSON.stringify({ ok: true });
  });
}

let store: Map<string, string>;
beforeEach(() => {
  invokeMock.mockReset();
  store = new Map();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("App in the new window", () => {
  it("opens first-run Welcome when nothing is set up, with the sidebar beside it", async () => {
    answers(false);
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Welcome to Khipu" })).toBeVisible();
    const main = screen.getByRole("navigation", { name: "Main" });
    expect(within(main).getByRole("button", { name: /^Today/ })).not.toHaveAttribute("aria-current");
    expect(document.querySelector(".sh-toolbar")).toBeNull();
  });

  it("starts on Today, which is the old Home, and names the places", async () => {
    store.set("khipu.welcome.completed", "1");
    answers(true);
    render(<App />);
    const main = await screen.findByRole("navigation", { name: "Main" });
    await waitFor(() => expect(within(main).getByRole("button", { name: /^Today/ })).toHaveAttribute("aria-current", "page"));
    for (const name of ["Accounts", "Memory", "Settings", "Older screens"]) {
      expect(within(main).getByRole("button", { name })).toBeInTheDocument();
    }
    expect(screen.getByRole("heading", { name: "Home" })).toBeVisible();
  });

  it("keeps every old screen reachable under Older screens", async () => {
    store.set("khipu.welcome.completed", "1");
    answers(true);
    render(<App />);
    for (const name of ["Work", "Recall", "Activity", "Harnesses", "Guides"]) {
      const button = await openScreen(name);
      await waitFor(() => expect(button).toHaveAttribute("aria-current", "page"));
    }
  });
});
