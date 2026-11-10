// The hook that makes Order and rules reachable from the Accounts place (slice
// A6): a view switch on the Accounts toolbar. "Room right now" is still the old
// routing screen until its replacement lands.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";

const invokeMock = vi.hoisted(() => vi.fn());
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: async () => "0.4.15" }));
vi.mock("@tauri-apps/plugin-updater", () => ({ check: async () => null }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: async () => {} }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ save: async () => null, open: async () => null, ask: async () => false, message: async () => {} }));
vi.mock("@tauri-apps/api/path", () => ({ resourceDir: async () => "/Applications/Khipu.app/Contents/Resources" }));

const ENTRIES = ["Claude", "Codex"].map((label) => ({ account: `${label.toLowerCase()}|`, label }));

let store: Map<string, string>;
beforeEach(() => {
  invokeMock.mockReset();
  invokeMock.mockImplementation(async (cmd: string) => {
    if (cmd === "dsn_configured") return true;
    if (cmd === "khipu_route_view") {
      return JSON.stringify({ record: true, accounts: ENTRIES.map((e) => ({ key: e.account, label: e.label, harness: "x", state: "open" })), priority: { entries: ENTRIES }, rules: [] });
    }
    if (cmd === "khipu_owner_direction_actions") return JSON.stringify({ ok: true, actions: [] });
    return JSON.stringify({ ok: true });
  });
  store = new Map([["khipu.welcome.completed", "1"]]);
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

describe("Accounts place: Order and rules", () => {
  it("is one view of Accounts, and the old routing screen is still the other", async () => {
    render(<App />);
    const main = await screen.findByRole("navigation", { name: "Main" });
    fireEvent.click(within(main).getByRole("button", { name: "Accounts" }));
    const views = await screen.findByRole("group", { name: "Accounts view" });
    expect(within(views).getByRole("button", { name: "Room right now" })).toHaveAttribute("aria-pressed", "true");
    expect(await screen.findByRole("heading", { name: "Accounts and routing" })).toBeVisible();

    fireEvent.click(within(views).getByRole("button", { name: "Order and rules" }));
    expect(await screen.findByRole("textbox", { name: "Change something by saying it" })).toBeVisible();
    await waitFor(() => expect(document.querySelector(".or-order")?.textContent).toContain("Codex"));
    expect(screen.queryByRole("heading", { name: "Accounts and routing" })).toBeNull();
    // The toolbar names the place with the page's heading while the old header is gone.
    expect(document.querySelector(".sh-toolbar h1")?.textContent).toBe("Accounts");

    fireEvent.click(within(views).getByRole("button", { name: "Room right now" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Accounts and routing" })).toBeVisible());
    expect(screen.queryByRole("textbox", { name: "Change something by saying it" })).toBeNull();
  });
});
