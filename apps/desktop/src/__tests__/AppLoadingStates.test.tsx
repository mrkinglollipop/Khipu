// All reads are faked. Visibility uses the shipped CSS, including hidden panes.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import App from "../App";
// @ts-expect-error node:fs has no types here
import { readFileSync } from "node:fs";
import { WELCOME_DONE_KEY } from "../Welcome";
import { parseStatusRows } from "../IntegrationsPanel";
import { openScreen } from "./nav";

const appCss: string = readFileSync(
  `${(globalThis as unknown as { process: { cwd(): string } }).process.cwd()}/src/App.css`,
  "utf8",
);

const invokeMock = vi.fn();
const boundaryFault = vi.hoisted(() => ({ enabled: false }));
let store: Map<string, string>;
let stylesheet: HTMLStyleElement;
let originalScrollIntoView: typeof HTMLElement.prototype.scrollIntoView;
const pending: Array<() => void> = [];

vi.mock("@tauri-apps/api/core", () => ({ invoke: (...args: unknown[]) => invokeMock(...args) }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn(async () => null), save: vi.fn(async () => null) }));
vi.mock("@tauri-apps/api/path", () => ({ resourceDir: vi.fn(async () => "/tmp/khipu-test-resources") }));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: vi.fn(async () => "0.4.15") }));
vi.mock("@tauri-apps/plugin-updater", () => ({ check: vi.fn(async () => null) }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: vi.fn(async () => {}) }));
vi.mock("../ComponentsPanel", async () => {
  const React = await import("react");
  return { ComponentsPanel: function ComponentsFixture() {
    const [draft, setDraft] = React.useState("");
    if (boundaryFault.enabled) throw new Error("fixture render failed");
    return <input aria-label="Component draft" value={draft} onChange={(e) => setDraft(e.target.value)} />;
  } };
});

type Mode = "ok" | "reject" | "native-timeout" | "empty" | "late";
const doctor = JSON.stringify({ ok: true, capture_liveness: { ok: true, red: [], harnesses: {} }, claude_homes: { homes: [] } });
const status = JSON.stringify({ counts: {}, recent_captures: [] });
const integrations = JSON.stringify([{ harness: "codex", detected: false, mcp: false, hook_stop: false, hook_precompact: false, recall_rule: "missing" }]);
const work = JSON.stringify({ format: "khipu-work-view", version: 1, generated_at: "2026-10-17T17:10:00Z", projects: [], live: [], waiting: [], counts: { waiting: 0, blocking: 0, running: 0, flagged: 0, stale_sources: 0, ledger_outbox: 0 }, problems: {}, refresh_s: 60 });

function answer(cmd: string, args: string[]) {
  if (cmd === "khipu_work_view") return work;
  if (cmd === "khipu_route_view") return JSON.stringify({ accounts: [], rules: [], holds: [] });
  if (cmd === "khipu_usage_status" || cmd === "khipu_usage_refresh") return JSON.stringify({ accounts: [] });
  if (cmd !== "run_khipu") return JSON.stringify({ ok: true });
  if (args[0] === "doctor") return doctor;
  if (args[0] === "status") return status;
  if (args[0] === "activity") return JSON.stringify({ episode_count: 0, recent: [] });
  if (args[0] === "integrations") return integrations;
  if (args[0] === "paths") return JSON.stringify({ data_dir: "", files: [] });
  if (args[0] === "sources") return JSON.stringify({ sources: [], resolved: { unreachable: [] } });
  if (args[0] === "owed" || args[0] === "revisions") return "[]";
  return "{}";
}

function script(mode: Mode, options: { integrations?: () => Promise<string> | string; status?: () => Promise<string> | string } = {}) {
  let statusReads = 0;
  invokeMock.mockImplementation(async (cmd: string, payload?: { args?: string[] }) => {
    if (cmd === "dsn_configured") return true;
    const args = payload?.args ?? [];
    if (cmd === "run_khipu" && args[0] === "integrations" && options.integrations) return options.integrations();
    if (cmd === "run_khipu" && args[0] === "status" && options.status) return options.status();
    if (mode === "reject") throw new Error("backend rejected the read");
    if (mode === "native-timeout") throw new Error("Khipu CLI timed out after 60 seconds");
    if (mode === "empty") return "";
    if (mode === "late") {
      // The first native read expires; subsequent reads finish later, after
      // navigation/retry. This preserves the native timeout configuration.
      if (cmd === "run_khipu" && args[0] === "status" && ++statusReads === 1) throw new Error("Khipu CLI timed out after 60 seconds");
      return new Promise<string>((resolve) => pending.push(() => resolve(answer(cmd, args))));
    }
    return answer(cmd, args);
  });
}

async function settleLateReads() {
  await act(async () => {
    while (pending.length) {
      pending.splice(0).forEach((finish) => finish());
      await Promise.resolve();
    }
  });
}

beforeEach(() => {
  invokeMock.mockReset();
  boundaryFault.enabled = false;
  pending.length = 0;
  stylesheet = document.createElement("style");
  stylesheet.textContent = appCss;
  document.head.append(stylesheet);
  originalScrollIntoView = HTMLElement.prototype.scrollIntoView;
  HTMLElement.prototype.scrollIntoView = vi.fn();
  store = new Map([[WELCOME_DONE_KEY, "1"]]);
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key), clear: () => store.clear(),
  });
});
afterEach(async () => {
  await settleLateReads();
  cleanup();
  stylesheet.remove();
  HTMLElement.prototype.scrollIntoView = originalScrollIntoView;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const places = ["Today", "Accounts", "Memory", "Settings"] as const;
function visiblePanel() {
  const panels = [...document.querySelectorAll<HTMLElement>("main section.panel")].filter((pane) => !pane.classList.contains("is-hidden"));
  expect(panels).toHaveLength(1);
  expect(panels[0]).toBeVisible();
  return panels[0];
}
function assertPlace() {
  expect(visiblePanel().textContent!.trim().length).toBeGreaterThan(0);
  expect(screen.getByRole("navigation", { name: "Main" })).toBeVisible();
}

describe("App loading and recovery", () => {
  // After A11 the window has three places and Settings. Whatever the reads do
  // (answer late, fail, time out, come back empty), each place still draws,
  // and so do the sidebar and the first-run Welcome.
  it.each(["late", "reject", "native-timeout", "empty"] as const)("keeps every place and Welcome on screen for %s reads", async (mode) => {
    script(mode);
    render(<App />);
    for (const name of places) {
      await openScreen(new RegExp(`^${name}`));
      if (mode === "late") await settleLateReads();
      await waitFor(() => assertPlace());
    }
    cleanup();
    store.clear();
    render(<App />);
    await waitFor(() => expect(within(visiblePanel()).getByRole("heading", { name: "Welcome to Khipu" })).toBeVisible());
    if (mode === "late") await settleLateReads();
    assertPlace();
  });
});

describe("integrations status", () => {
  it.each(["", "null", "{}", '{"ok":false}', "[null]", "[{}]"])("rejects an unreadable integrations status: %s", (raw) => {
    expect(parseStatusRows(raw)).toBeNull();
  });
  it("accepts an empty status list and a valid status row", () => {
    expect(parseStatusRows("[]")).toEqual([]);
    expect(parseStatusRows(integrations)).toEqual(JSON.parse(integrations));
  });
});


it("keeps the loading banner outside the content scroll area", async () => {
  script("late");
  render(<App />);
  // Settings reads the health check, which shows the banner while it runs.
  await openScreen("Settings");
  const main = screen.getByRole("main");
  await waitFor(() => expect(main.querySelector(".working-banner")).not.toBeNull());
  const banner = main.querySelector<HTMLElement>(".working-banner")!;
  const scroller = main.querySelector(".main-content");
  expect(scroller).not.toBeNull();
  expect(banner.parentElement).toBe(main);
  expect(scroller!.parentElement).toBe(main);
  expect(banner.nextElementSibling).toBe(scroller);
  expect(scroller).not.toContainElement(banner);
  // jsdom does not expand the overflow shorthand, so read the shipped rules themselves.
  const rule = (selector: string) =>
    appCss.match(new RegExp(`^\\${selector}\\s*\\{[^}]*\\}`, "m"))?.[0].replace(/\/\*[\s\S]*?\*\//g, "") ?? "";
  expect(rule(".main")).toMatch(/\boverflow:\s*hidden;/);
  expect(rule(".main-content")).toMatch(/\boverflow-y:\s*auto;/);
  expect(scroller!.querySelector("section.panel")).not.toBeNull();
});
