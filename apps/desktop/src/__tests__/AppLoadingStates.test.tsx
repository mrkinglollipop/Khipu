// All reads are faked. Visibility uses the shipped CSS, including hidden panes.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import App from "../App";
// @ts-expect-error node:fs has no types here
import { readFileSync } from "node:fs";
import { WELCOME_DONE_KEY } from "../Welcome";
import { parseStatusRows } from "../IntegrationsPanel";

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
type Deferred = { promise: Promise<string>; resolve: (value: string) => void; reject: (error: Error) => void };
function deferred(): Deferred {
  let resolve!: (value: string) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<string>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}
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

const primaryViews = ["Home", "Work", "Recall", "Activity", "Harnesses", "Settings"] as const;
function visiblePanel() {
  const panels = [...document.querySelectorAll<HTMLElement>("main section.panel")].filter((pane) => !pane.classList.contains("is-hidden"));
  expect(panels).toHaveLength(1);
  expect(panels[0]).toBeVisible();
  return panels[0];
}
function assertPane(heading: string) {
  expect(within(visiblePanel()).getByRole("heading", { name: heading })).toBeVisible();
  expect(visiblePanel().textContent!.trim().length).toBeGreaterThan(heading.length);
  expect(screen.getByRole("navigation", { name: "Primary" })).toBeVisible();
}
async function select(label: typeof primaryViews[number]) {
  fireEvent.click(screen.getByRole("button", { name: label }));
  await waitFor(() => assertPane(label));
}
async function everyPane(mode: Mode) {
  for (const label of primaryViews) {
    await select(label);
    if (mode === "late") await settleLateReads();
    assertPane(label);
    if (mode === "late") expect(document.querySelector("main > .working-banner")).not.toBeInTheDocument();
  }
  await select("Home");
  const summary = [...visiblePanel().querySelectorAll("summary")].find((el) => el.textContent?.includes("Full health report"))!;
  expect(summary).toBeInTheDocument();
  fireEvent.click(summary);
  fireEvent.click(within(visiblePanel()).getByRole("button", { name: /^Conflicting edits/ }));
  await waitFor(() => assertPane("Conflicting edits"));
  if (mode === "late") await settleLateReads();
  assertPane("Conflicting edits");

  cleanup();
  store.clear();
  render(<App />);
  await waitFor(() => assertPane("Welcome to Khipu"));
  if (mode === "late") await settleLateReads();
  assertPane("Welcome to Khipu");
}

describe("App loading and recovery", () => {
  it.each(["late", "reject", "native-timeout", "empty"] as const)("keeps all eight top-level panes visible for %s reads", async (mode) => {
    script(mode);
    render(<App />);
    if (mode === "late") {
      const failed = await within(visiblePanel()).findByText("Couldn't read recent captures");
      fireEvent.click(within(failed.closest<HTMLElement>(".callout")!).getByRole("button", { name: "Retry" }));
    }
    await everyPane(mode);
  });

  it("keeps the timeout error when the same underlying read resolves late, then recovers with retry", async () => {
    const raw = deferred();
    const deadline = deferred();
    let retryReady = false;
    script("ok", { status: () => retryReady ? status : Promise.race([raw.promise, deadline.promise]) });
    render(<App />);
    await waitFor(() => expect(document.querySelector(".working-banner")).toHaveTextContent("counts"));
    await act(async () => { deadline.reject(new Error("Khipu CLI timed out after 60 seconds")); });
    const failure = await within(visiblePanel()).findByText("Couldn't read recent captures");
    await act(async () => { raw.resolve(status); });
    expect(failure).toBeVisible();
    assertPane("Home");
    retryReady = true;
    fireEvent.click(within(failure.closest<HTMLElement>(".callout")!).getByRole("button", { name: "Retry" }));
    expect(await within(visiblePanel()).findByText("No captures yet")).toBeVisible();
    await waitFor(() => expect(document.querySelector("main > .working-banner")).not.toBeInTheDocument());
    assertPane("Home");
  });

  it("keeps Settings and the rail when a hidden Harnesses read returns a malformed envelope", async () => {
    const malformed = deferred();
    let reads = 0;
    script("ok", { integrations: () => ++reads === 1 ? integrations : reads === 2 ? malformed.promise : integrations });
    render(<App />);
    await select("Harnesses");
    await waitFor(() => expect(within(visiblePanel()).getByRole("button", { name: "Refresh" })).toBeEnabled());
    fireEvent.click(within(visiblePanel()).getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(reads).toBe(2));
    await select("Settings");
    await act(async () => { malformed.resolve('{"ok":false,"error":"unreadable response"}'); });
    assertPane("Settings");
    const hiddenHarnesses = [...document.querySelectorAll<HTMLElement>("main section.panel")].find((pane) => pane.querySelector("h1")?.textContent === "Harnesses")!;
    await waitFor(() => expect(hiddenHarnesses).toHaveTextContent("Khipu returned an unreadable harness status."));
    expect(hiddenHarnesses).toHaveClass("is-hidden");
    const retry = [...hiddenHarnesses.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent?.trim() === "Retry");
    expect(retry).toBeDefined();
    if (!retry) throw new Error("Harnesses retry button was missing");
    expect(retry).toBeEnabled();
    fireEvent.click(retry);
    await waitFor(() => expect(reads).toBe(3));
    await waitFor(() => expect(hiddenHarnesses).not.toHaveTextContent("Khipu returned an unreadable harness status."));
    assertPane("Settings");
    expect(hiddenHarnesses).toHaveClass("is-hidden");
  });

  it("provides a working retry for a failed first Work read", async () => {
    script("reject");
    render(<App />);
    await select("Work");
    const failure = await within(visiblePanel()).findByText("Could not read the work view");
    expect(failure).toBeVisible();
    script("ok");
    fireEvent.click(within(failure.closest<HTMLElement>(".card")!).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(within(visiblePanel()).queryByText("Could not read the work view")).not.toBeInTheDocument());
    assertPane("Work");
  });

  it("isolates a pane render failure, recovers on retry, and preserves sibling state across navigation", async () => {
    const errorLog = vi.spyOn(console, "error").mockImplementation(() => {});
    script("ok");
    render(<App />);
    await select("Settings");
    fireEvent.click(within(visiblePanel()).getByRole("tab", { name: "Components" }));
    fireEvent.change(within(visiblePanel()).getByRole("textbox", { name: "Component draft" }), { target: { value: "retained draft" } });
    await select("Home");
    await select("Settings");
    expect(within(visiblePanel()).getByRole("textbox", { name: "Component draft" })).toHaveValue("retained draft");
    boundaryFault.enabled = true;
    await select("Home");
    await select("Settings");
    expect(await within(visiblePanel()).findByRole("alert")).toHaveTextContent("Could not render Settings");
    expect(errorLog).toHaveBeenCalled();
    await select("Home");
    assertPane("Home");
    boundaryFault.enabled = false;
    await select("Settings");
    fireEvent.click(within(visiblePanel()).getByRole("button", { name: "Retry" }));
    expect(await within(visiblePanel()).findByRole("textbox", { name: "Component draft" })).toBeVisible();
  });

  it.each(["", "null", "{}", '{"ok":false}', "[null]", "[{}]"])("rejects an unreadable integrations status: %s", (raw) => {
    expect(parseStatusRows(raw)).toBeNull();
  });
  it("accepts an empty status list and a valid status row", () => {
    expect(parseStatusRows("[]")).toEqual([]);
    expect(parseStatusRows(integrations)).toEqual(JSON.parse(integrations));
  });
});


it("keeps Home's loading banner outside the content scroll area", async () => {
  script("late");
  render(<App />);
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
