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
    for (const name of ["Work", "Recall", "Activity", "Harnesses", "Guides", "Accounts and routing"]) {
      const button = await openScreen(name);
      await waitFor(() => expect(button).toHaveAttribute("aria-current", "page"));
    }
  });

  it("Accounts is the new place, with its view switch; Running now is drawn in Accounts and Order and rules opens its older screen until its slice lands", async () => {
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
    // Running now stays in Accounts (slice A5): the switch shows it and Work is not opened.
    fireEvent.click(within(screen.getByRole("group", { name: "Accounts view" })).getByRole("button", { name: "Running now" }));
    expect(within(screen.getByRole("group", { name: "Accounts view" })).getByRole("button", { name: "Running now" })).toHaveAttribute("aria-pressed", "true");
    expect(within(main).getByRole("button", { name: "Accounts" })).toHaveAttribute("aria-current", "page");
  });
  it("the five checks' link on Today opens Running now in Accounts, not the old Work screen", async () => {
    store.set("khipu.welcome.completed", "1");
    const area = (key: string, name: string, link: string) => ({ key, name, state: "ok", line: `${name}.`, khipu_doing: "", you_do: "Nothing.", link });
    const health = { checked_at: new Date().toISOString(), mac: "M5", headline: "Everything is working.", jobs_running: 0, silent_runners: [], free_gb: 80, last_backup_at: null, agent_note: "",
      areas: [area("saving", "Saving memory", "where-memory-is-kept"), area("finding", "Finding things", "search"), area("routing", "Routing", "accounts"), area("database", "Database and disk", "backups"), area("jobs", "Jobs and other checks", "running-now")] };
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "dsn_configured") return true;
      if (cmd === "khipu_health") return JSON.stringify(health);
      if (cmd === "khipu_running") return JSON.stringify({ updated_at: new Date().toISOString(), macs: [{ mac: "m5", name: "M5", this_mac: true, as_of: new Date().toISOString(), stale: false, runners: [] }] });
      return JSON.stringify({ ok: true });
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "See the five checks" }));
    fireEvent.click(await screen.findByRole("button", { name: "Open Running now" }));
    const main = screen.getByRole("navigation", { name: "Main" });
    await waitFor(() => expect(within(main).getByRole("button", { name: "Accounts" })).toHaveAttribute("aria-current", "page"));
    expect(within(screen.getByRole("group", { name: "Accounts view" })).getByRole("button", { name: "Running now" })).toHaveAttribute("aria-pressed", "true");
    expect((await screen.findAllByText(/No job runner is reporting on M5\./)).length).toBeGreaterThan(0);
  });
});
