// The Harnesses card's toasts, driven through App's real wiring: `onToast`
// reaches App's `setError` and `refreshHealth` is the real `loadDoctor(true)`,
// whose first act is to clear that same message. Each action's result must
// still be on screen once its reloads have settled.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import App from "../App";
import { WELCOME_DONE_KEY } from "../Welcome";

const invokeMock = vi.fn();

vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));
vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: vi.fn(async () => null),
  save: vi.fn(async () => null),
}));
vi.mock("@tauri-apps/api/path", () => ({
  resourceDir: vi.fn(async () => "/Applications/Khipu.app/Contents/Resources"),
}));
vi.mock("@tauri-apps/api/app", () => ({
  getVersion: vi.fn(async () => "0.4.0"),
}));
vi.mock("@tauri-apps/plugin-updater", () => ({
  check: vi.fn(async () => null),
}));
vi.mock("@tauri-apps/plugin-process", () => ({
  relaunch: vi.fn(async () => {}),
}));

beforeEach(() => {
  invokeMock.mockReset();
  const store = new Map<string, string>([[WELCOME_DONE_KEY, "1"]]);
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const DEFAULT_PATH = "/Users/me/.claude";
const T3_PATH = "/Users/me/.claude-t3-second";

const homeRow = (path: string, label: string, installed: boolean) => ({
  path,
  label,
  source: label,
  is_default: path === DEFAULT_PATH,
  exists: true,
  linked_to: null,
  hook_stop: installed,
  hook_precompact: installed,
  recall_rule: installed ? "installed" : "missing",
  memory_tools_ok: installed,
  hooks_ok: installed,
  installed,
});

const STATUS = [
  {
    harness: "claude_code",
    detected: true,
    mcp: false,
    hook_stop: false,
    hook_precompact: false,
    recall_rule: "missing",
    homes: [homeRow(DEFAULT_PATH, "Default", true), homeRow(T3_PATH, "T3 · Secondary", false)],
  },
];

const LIVE = { ok: true, red: [], harnesses: { claude_code: { ok: true, seen: true, captures: 3 } } };
const DOCTOR = JSON.stringify({
  ok: true,
  capture_liveness: LIVE,
  claude_homes: { homes: [{ exists: true, installed: true }] },
});

function script(over: { install?: string; verify?: string; uninstall?: string }) {
  invokeMock.mockImplementation(async (cmd: string, payload?: { args?: string[] }) => {
    if (cmd === "dsn_configured") return true;
    if (cmd !== "run_khipu") return JSON.stringify({ ok: true });
    const args = payload?.args ?? [];
    if (args[0] === "doctor") return DOCTOR;
    if (args[0] === "integrations" && args[1] === "status") return JSON.stringify(STATUS);
    if (args[0] === "integrations" && args[1] === "install") {
      return over.install ?? '[{"harness":"claude_code","detected":true,"changes":[]}]';
    }
    if (args[0] === "integrations" && args[1] === "verify") return over.verify ?? "[]";
    if (args[0] === "integrations" && args[1] === "uninstall") {
      return over.uninstall ?? '[{"harness":"claude_code","changes":[]}]';
    }
    return "{}";
  });
}

const doctorCalls = () =>
  invokeMock.mock.calls.filter((c) => c[0] === "run_khipu" && c[1]?.args?.[0] === "doctor").length;

async function openHarnessesCard(): Promise<HTMLElement> {
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: /Harnesses/ }));
  const findCard = () =>
    Array.from(document.querySelectorAll<HTMLElement>(".hcard")).find((c) => within(c).queryByText("Claude Code"));
  await waitFor(() => expect(findCard()).toBeTruthy());
  return findCard() as HTMLElement;
}

/** Waits for the action's own doctor read, then lets the toast go up. */
async function settled(before: number) {
  await waitFor(() => expect(doctorCalls()).toBeGreaterThan(before));
  await new Promise((r) => setTimeout(r, 50));
}

describe("App — Harnesses toasts survive the reload that follows an action", () => {
  it("keeps the per-home Install result", async () => {
    script({});
    const card = await openHarnessesCard();
    const before = doctorCalls();
    fireEvent.click(within(card).getByRole("button", { name: "Install Khipu in T3 · Secondary" }));
    await settled(before);
    expect(
      screen.getByText("Installed in T3 · Secondary. Restart its sessions to load it; Verify on the card checks it."),
    ).toBeInTheDocument();
  });

  it("keeps the card-level Install's failed-verify result", async () => {
    script({
      verify:
        '[{"harness":"claude_code","detected":true,"ok":false,"components":{"hook":{"ok":false,"error":"Stop hook not found"}}}]',
    });
    const card = await openHarnessesCard();
    const before = doctorCalls();
    fireEvent.click(within(card).getByRole("button", { name: /^(Install|Reinstall)$/ }));
    await settled(before);
    expect(screen.getByText(/Installed; verify failed: claude_code hook: Stop hook not found/)).toBeInTheDocument();
  });

  it("keeps a Remove's hooks-kept result", async () => {
    script({
      uninstall:
        '[{"harness":"claude_code","changes":[],"homes":[{"home":"/Users/me/.claude","label":"Default","hooks_kept":"still used by T3 · Secondary"}]}]',
    });
    const card = await openHarnessesCard();
    const before = doctorCalls();
    fireEvent.click(within(card).getByRole("button", { name: "Remove Khipu from Default" }));
    await settled(before);
    expect(screen.getByText(/hooks were kept \(still used by T3 · Secondary\)/)).toBeInTheDocument();
  });
});
