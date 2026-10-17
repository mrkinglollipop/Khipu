// Layout fixes on the Work tab from the 2026-10-16 look at the installed app:
// the Accounts card's Check now and the Owed table's "seen" line. jsdom does
// not lay out a grid, so the placement rules are read from the stylesheet the
// way Work.test reads Work.css.
// @ts-expect-error node:fs has no types here
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import App from "../App";
import { WELCOME_DONE_KEY } from "../Welcome";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke: (...args: unknown[]) => invokeMock(...args) }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn(async () => null), save: vi.fn(async () => null) }));
vi.mock("@tauri-apps/api/path", () => ({ resourceDir: vi.fn(async () => "/Applications/Khipu.app/Contents/Resources") }));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: vi.fn(async () => "0.4.0") }));
vi.mock("@tauri-apps/plugin-updater", () => ({ check: vi.fn(async () => null) }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: vi.fn(async () => {}) }));

const css = readFileSync(`${(globalThis as unknown as { process: { cwd(): string } }).process.cwd()}/src/App.css`, "utf8");
/** Every declaration block for an exact selector list, media blocks included. */
function rules(selector: string): string[] {
  const esc = selector.replace(/[.*+?^${}()|[\]\\>]/g, "\\$&").replace(/\s+/g, "\\s*");
  return [...css.matchAll(new RegExp(`(?:^|[},\\n])\\s*${esc}\\s*(?:,[^{]*)?\\{([^}]*)\\}`, "g"))].map((m) => m[1]);
}

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

describe("the Accounts card", () => {
  it("puts Check now under the account's state, not under its name", () => {
    const placed = rules(".card .row.route-row > .route-acts");
    // Wide (three columns) and the 760px stack (one column).
    expect(placed.some((r) => /grid-column:\s*2\s*\/\s*-1/.test(r))).toBe(true);
    expect(placed.some((r) => /grid-column:\s*1\s*\/\s*-1/.test(r))).toBe(true);
  });

  it("keeps \"Khipu checks again\" in the source column at every width", () => {
    const placed = rules(".card .row.route-row > .route-src + .route-src");
    expect(placed.some((r) => /grid-column:\s*3\s*\/\s*-1/.test(r))).toBe(true);
    // The 1040px grid has two columns: column 3 there would add a third.
    expect(placed.some((r) => /grid-column:\s*2\s*\/\s*-1/.test(r))).toBe(true);
    expect(placed.some((r) => /grid-column:\s*1\s*\/\s*-1/.test(r))).toBe(true);
  });

  it("an account with no source words leaves no empty line above Check now", () => {
    expect(rules(".card .row.route-row > .route-src:empty").some((r) => /display:\s*none/.test(r))).toBe(true);
  });
});

describe("the Owed table", () => {
  it("dates a restated item under its own text, in the same cell", async () => {
    const restated = {
      id: 7, text: "Send the signed form", project: "atlas", kind: "followup", status: "open",
      owner: "user", opened_episode: 17, opened_at: "2026-10-01T12:00:00Z",
      seen_count: 3, last_seen_at: "2026-10-13T12:00:00Z",
    };
    invokeMock.mockImplementation(async (cmd: string, payload?: { args?: string[] }) => {
      if (cmd === "dsn_configured") return true;
      if (cmd !== "run_khipu") return JSON.stringify({ ok: true });
      return payload?.args?.[0] === "owed" ? JSON.stringify([restated]) : "{}";
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /^Work/ }));
    const text = (await screen.findAllByText("Send the signed form")).find((el) => el.classList.contains("owed-text"));
    expect(text).toBeDefined();
    const seen = text!.parentElement!.querySelector(".owed-seen");
    expect(seen?.textContent).toMatch(/^seen /);
    expect(text!.parentElement!.classList.contains("owed-main")).toBe(true);
  });

  it("stacks the text cell, not the bare text, when the row wraps", () => {
    expect(rules(".owed-main").some((r) => /flex-direction:\s*column/.test(r) && /min-width:\s*0/.test(r))).toBe(true);
    expect(rules(".owed-main").some((r) => /flex:\s*1 1 100%/.test(r))).toBe(true);
  });
});
