// Settings › Look: the colour choice and the login-item switch. The login item
// is a mock here: nothing in this file changes the Mac.
// @ts-expect-error node:fs has no types here
import { readFileSync } from "node:fs";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const auto = vi.hoisted(() => ({ on: false, fail: false }));
vi.mock("@tauri-apps/plugin-autostart", () => ({
  isEnabled: vi.fn(async () => auto.on),
  enable: vi.fn(async () => {
    if (auto.fail) throw new Error("denied");
    auto.on = true;
  }),
  disable: vi.fn(async () => {
    if (auto.fail) throw new Error("denied");
    auto.on = false;
  }),
}));

import { LookPage } from "../settings/pages/Look";
import { LOOK_KEY, applyStoredLook, readLook } from "../settings/lookModel";

const css = readFileSync("src/App.css", "utf8") as string;

beforeEach(() => {
  auto.on = false;
  auto.fail = false;
  // This test environment's own localStorage is not a full one: use a plain map.
  const store = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
  });
  document.documentElement.removeAttribute("data-theme");
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Look", () => {
  it("starts on follow-the-Mac with no override and no stored value", () => {
    render(<LookPage />);
    expect(screen.getByRole("radio", { name: "Follows this Mac" })).toHaveAttribute("aria-checked", "true");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  it("Dark sets data-theme and stores it; Follow clears both", () => {
    render(<LookPage />);
    fireEvent.click(screen.getByRole("radio", { name: "Dark" }));
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(window.localStorage.getItem(LOOK_KEY)).toBe("dark");
    fireEvent.click(screen.getByRole("radio", { name: "Follows this Mac" }));
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    expect(window.localStorage.getItem(LOOK_KEY)).toBeNull();
  });

  it("applies a stored choice at start and ignores junk", () => {
    window.localStorage.setItem(LOOK_KEY, "light");
    applyStoredLook();
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    window.localStorage.setItem(LOOK_KEY, "purple");
    expect(readLook()).toBe("system");
  });

  it("reacts when the Mac's appearance changes while following", async () => {
    let listener: (() => void) | null = null;
    const mq = { matches: false, addEventListener: (_: string, f: () => void) => (listener = f), removeEventListener: () => {} };
    vi.stubGlobal("matchMedia", () => mq);
    render(<LookPage />);
    expect(screen.getByText(/which is light right now/)).toBeTruthy();
    mq.matches = true;
    listener!();
    await waitFor(() => expect(screen.getByText(/which is dark right now/)).toBeTruthy());
  });

  it("turns the login item on and off through the plugin and shows what it reports", async () => {
    render(<LookPage />);
    const sw = await screen.findByRole("switch", { name: /Open Khipu when this Mac starts/ });
    await waitFor(() => expect(sw).not.toBeDisabled());
    fireEvent.click(sw);
    await waitFor(() => expect(sw).toBeChecked());
    expect(auto.on).toBe(true);
    fireEvent.click(sw);
    await waitFor(() => expect(sw).not.toBeChecked());
  });

  it("says so when the Mac refuses the login item, and leaves the switch off", async () => {
    auto.fail = true;
    render(<LookPage />);
    const sw = await screen.findByRole("switch");
    await waitFor(() => expect(sw).not.toBeDisabled());
    fireEvent.click(sw);
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn't change this/);
    expect(sw).not.toBeChecked();
  });
});

describe("the data-theme block in App.css", () => {
  const tokens = (block: string) => [...block.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map((m) => `${m[1]}:${m[2].trim()}`).sort();
  const pick = (re: RegExp) => css.match(re)![1];
  it("carries the same dark tokens as the media block, plus the select tint", () => {
    const media = tokens(pick(/@media \(prefers-color-scheme: dark\) \{\s*:root \{([^}]+)\}/));
    const forced = tokens(pick(/:root\[data-theme="dark"\] \{([^}]+)\}/)).filter((t) => !t.startsWith("--select"));
    expect(forced).toEqual(media);
  });
  it("carries the same light tokens as :root", () => {
    const base = tokens(pick(/:root \{([^}]+)\}/)).filter((t) => /^--(bg|surface|surface-2|rail|border|border-strong|text|text-2|muted|accent|accent-hover|accent-text|accent-soft|on-accent|ok|ok-soft|warn|warn-soft|err|err-soft|ring|shadow-sm|shadow-md|backdrop):/.test(t));
    const forced = tokens(pick(/:root\[data-theme="light"\] \{([^}]+)\}/)).filter((t) => !t.startsWith("--select"));
    expect(forced).toEqual(base);
  });
});
