import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import App from "../App";
import { WELCOME_DONE_KEY } from "../Welcome";
import type { WaitItem, WorkView } from "../work";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke: (...args: unknown[]) => invokeMock(...args) }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn(async () => null), save: vi.fn(async () => null) }));
vi.mock("@tauri-apps/api/path", () => ({ resourceDir: vi.fn(async () => "/Applications/Khipu.app/Contents/Resources") }));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: vi.fn(async () => "0.4.0") }));
vi.mock("@tauri-apps/plugin-updater", () => ({ check: vi.fn(async () => null) }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: vi.fn(async () => {}) }));

beforeEach(() => {
  const store = new Map([[WELCOME_DONE_KEY, "1"]]);
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); invokeMock.mockReset(); });

const quiet: WaitItem & { quiet: boolean } = {
  source: "commitment", key: "commitment:7", project: "atlas", id: "7",
  title: "Old commitment", blocking: true, quiet: true, actions: [],
};
const active: WaitItem = {
  source: "commitment", key: "commitment:8", project: "atlas", id: "8",
  title: "Current blocker", blocking: true, actions: [],
};
const linked: WaitItem = {
  source: "ledger", key: "ledger:atlas#decision", project: "atlas", id: "decision",
  title: "Linked ledger need", blocking: true, also_owed: "9", actions: [],
};

it.each([
  { name: "all quiet", items: [quiet], waiting: 0, blocking: 0 },
  { name: "mixed quiet and active", items: [active, quiet], waiting: 1, blocking: 1 },
  // Backend links an old commitment onto the active ledger need; it does not
  // carry the commitment's quiet flag onto the ledger row.
  { name: "quiet plus linked ledger need", items: [linked, quiet], waiting: 1, blocking: 1 },
])("keeps the navigation and Work counts consistent for $name", async ({ items, waiting, blocking }) => {
  const doc: WorkView = {
    format: "khipu-work-view", version: 1, generated_at: "2026-10-26T12:00:00Z",
    projects: [], live: [], waiting: items,
    counts: { waiting, blocking, quiet: 1, running: 0, flagged: 0, stale_sources: 0, ledger_outbox: 0 },
    problems: {}, ledger_offline: false, refresh_s: 60,
  } as WorkView;
  invokeMock.mockImplementation(async (cmd: string) => {
    if (cmd === "dsn_configured") return true;
    if (cmd === "khipu_work_view") return JSON.stringify(doc);
    return JSON.stringify({ ok: true });
  });
  render(<App />);
  const nav = await screen.findByRole("button", { name: /^Work/ });
  fireEvent.click(nav);
  const label = `${waiting} waiting on you${blocking ? `, ${blocking} blocking` : ""}, 1 quiet`;
  const badge = await within(nav).findByLabelText(label);
  expect(badge.textContent).toBe(String(items.length));
  expect(badge.querySelector(".bdot") !== null).toBe(blocking > 0);
  if (!waiting) expect(badge).toHaveClass("quiet");
  expect(await screen.findByText(`${waiting} waiting · 1 quiet across 1 project${blocking ? ` · ${blocking} blocking` : ""}`)).toBeInTheDocument();
  const list = screen.getByRole("list", { name: "Waiting on you" });
  const old = within(list).getByText("Old commitment").closest('[role="listitem"]')!;
  expect(within(old as HTMLElement).getByText("Quiet")).toBeInTheDocument();
  expect(within(old as HTMLElement).queryByText("Blocking")).toBeNull();
  expect(within(list).queryAllByText("Blocking")).toHaveLength(blocking);
  fireEvent.click(screen.getByRole("button", { name: `Blocking ${blocking}` }));
  expect(within(list).queryByText("Old commitment")).toBeNull();
  expect(within(list).queryAllByRole("listitem")).toHaveLength(blocking);
  if (items.includes(linked)) {
    expect(within(list).getByText("Linked ledger need")).toBeInTheDocument();
    expect(within(list).getByText("Also in Owed")).toBeInTheDocument();
  }
});
