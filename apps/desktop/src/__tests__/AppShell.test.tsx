// The app in the new window (slice A1): the places show the old screens, the
// old screens sit under Older screens, and first-run Welcome still opens.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
    for (const name of ["Work", "Activity", "Harnesses", "Guides", "Accounts and routing"]) {
      const button = await openScreen(name);
      await waitFor(() => expect(button).toHaveAttribute("aria-current", "page"));
    }
  });

  it("Accounts is the new place, with its view switch; Running now and Order and rules open the older screens until their slices land", async () => {
    store.set("khipu.welcome.completed", "1");
    answers(true);
    render(<App />);
    const main = await screen.findByRole("navigation", { name: "Main" });
    fireEvent.click(within(main).getByRole("button", { name: "Accounts" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Accounts" })).toBeVisible();
    const views = screen.getByRole("group", { name: "Accounts view" });
    expect(within(views).getByRole("button", { name: "Room right now" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(within(views).getByRole("button", { name: "Order and rules" }));
    // Hidden panels keep their headings in jsdom (no stylesheet), so the older screen is told by its sidebar item.
    const older0 = within(main).getByRole("button", { name: "Older screens" });
    if (older0.getAttribute("aria-expanded") !== "true") fireEvent.click(older0);
    await waitFor(() => expect(within(main).getByRole("button", { name: "Accounts and routing" })).toHaveAttribute("aria-current", "page"));
    fireEvent.click(within(main).getByRole("button", { name: "Accounts" }));
    fireEvent.click(within(screen.getByRole("group", { name: "Accounts view" })).getByRole("button", { name: "Running now" }));
    const older = within(main).getByRole("button", { name: "Older screens" });
    if (older.getAttribute("aria-expanded") !== "true") fireEvent.click(older);
    await waitFor(() => expect(within(main).getByRole("button", { name: "Work" })).toHaveAttribute("aria-current", "page"));
  });
});
