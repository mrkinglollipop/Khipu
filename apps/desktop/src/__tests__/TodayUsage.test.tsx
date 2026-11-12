(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { usageTiles, planEnd, USAGE_CHOICES_KEY } from "../today/usage";
import { Usage } from "../today/Usage";
import { Today } from "../today/Today";
import type { RouteAccount, RouteView, Pace } from "../routing";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(() => new Promise(() => {})) }));
const now = new Date("2026-11-12T19:00:00Z");
const ago = (mins: number) => new Date(now.getTime() - mins * 60000).toISOString();
const win = (window = "weekly", used_pct = 38, verdict = "on_pace"): Pace => ({ window, used_pct, cycle_resets_at: "2026-11-20T19:00:00Z", plan: { verdict } });
const account = (over: Partial<RouteAccount> = {}): RouteAccount => ({ key: "claude|", label: "Claude", harness: "claude", state: "open", pace: [win()], ...over });
const view = (accounts = [account()]): RouteView => ({ accounts, checked_at: ago(1) });
const tile = (a = account(), v = view([a]), plans = {}) => usageTiles({ ...v, accounts: [a] }, plans, {}, now)[0];
const store = new Map<string, string>();
beforeEach(() => {
  store.clear();
  vi.stubGlobal("localStorage", { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => store.set(k, v), removeItem: (k: string) => store.delete(k) });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("Today usage words", () => {
  it("uses the tightest remaining window and keeps Accounts' pace and reset words", () => {
    const t = tile(account({ pace: [win("five_hour", 85, "outpacing"), win()] }));
    expect(t).toMatchObject({ left: 15, line: "15% left", word: "Ahead of pace", tone: "warn", detail: "This week: 62% left" });
    expect(t.window).toContain("next 5 hours · resets");
    expect(tile()).toMatchObject({ left: 62, word: "On pace", tone: "ok" });
  });
  it("keeps a limit reached red with zero left", () => {
    expect(tile(account({ state: "limited", pace: [win("weekly", 100, "limit_reached")] }))).toMatchObject({ left: 0, word: "Out of room", tone: "err" });
  });
  it("keeps unknown and no-reading accounts without bars or implied room", () => {
    expect(tile(account({ state: "unknown" }))).toMatchObject({ left: null, word: "Not known" });
    expect(tile(account({ pace: [] }))).toMatchObject({ left: null, word: "No reading yet" });
  });
  it("never claims an account has no limit: an account with no reading says so", () => {
    const a = account({ pace: [] });
    const plans = { accounts: [{ key: a.key, ok: true, credits: { unlimited: true } }] };
    expect(tile(a, view([a]), plans)).toMatchObject({ left: null, word: "No reading yet" });
  });
  it("shows no percent beside a limit that is reached, even when an older reading has room", () => {
    const t = tile(account({ state: "limited", pace: [win("weekly", 80, "ahead")] }));
    expect(t).toMatchObject({ left: 0, word: "Out of room", tone: "err", window: "" });
    expect(t.line).not.toMatch(/% left/);
  });
  it("keeps a paused account's figures", () => {
    const t = tile(account({ state: "paused", pace: [win("weekly", 38, "on_pace")] }));
    expect(t.left).toBe(62);
    expect(t.line).toBe("62% left");
  });
  it("prioritizes plan end over old reading over the second window", () => {
    const a = account({ pace: [win(), win("daily", 10)], terms: { ends: "2026-11-13" } });
    const v = { ...view([a]), plan_facts: { [a.key]: { readable: true, figures_at: ago(20), stale: true } } };
    expect(tile(a, v).detail).toBe("Plan ends tomorrow, 13 Nov");
    expect(tile({ ...a, terms: undefined }, v).detail).toBe("Read 20 minutes ago");
    expect(tile({ ...a, terms: undefined }, { ...v, plan_facts: { [a.key]: { readable: true, figures_at: ago(6) } } }).detail).toBe("Read 6 minutes ago");
    expect(tile({ ...a, terms: undefined }, { ...v, plan_facts: { [a.key]: { readable: true, figures_at: ago(5) } } }).detail).toBe("Today: 90% left");
  });
  it("uses the record check time when no account reading time was carried", () => {
    expect(tile(account(), { ...view(), checked_at: ago(8) }).detail).toBe("Read 8 minutes ago");
  });
  it.each([["2026-11-13", false, "Plan ends tomorrow, 13 Nov"], ["2026-11-12", false, "Plan ends today"], ["2026-11-11", true, "Plan ended 11 Nov"]])("keeps local end day %s until that day is over", (ends, ended, line) => {
    expect(planEnd(account({ terms: { ends } }), now)).toEqual({ ended, line });
  });
  it("keeps a date-only plan through its late local evening and accepts explicit choices in either direction", () => {
    const a = account({ terms: { ends: "2026-11-12" } });
    expect(usageTiles(view([a]), null, {}, new Date("2026-11-13T04:59:00Z"))[0].shown).toBe(true);
    expect(usageTiles(view([a]), null, {}, new Date("2026-11-13T05:00:00Z"))[0].shown).toBe(false);
    expect(usageTiles(view([a]), null, { [a.key]: true }, new Date("2026-11-13T05:00:00Z"))[0].shown).toBe(true);
    expect(usageTiles(view([a]), null, { [a.key]: false }, now)[0].shown).toBe(false);
  });
});

describe("Today usage interactions", () => {
  it("puts usage between the status sentence and Needs you and opens the chosen account", () => {
    const open = vi.fn();
    const { container } = render(<Today active={false} needs={{ data: null, failed: false, loading: false, reload: async () => null }} onOpen={() => {}} view={view()} onOpenAccount={open} />);
    expect([...container.querySelector(".td-page-in")!.children].map((e) => e.className)).toEqual(["td-line", "td-usage", "td-needs"]);
    fireEvent.click(screen.getByRole("button", { name: /Claude/ }));
    expect(open).toHaveBeenCalledWith("claude|");
  });
  it("persists only explicit boolean choices, and keeps hidden accounts in the chooser", () => {
    const v = view();
    const r = render(<Usage view={v} plans={null} now={now} />);
    expect(store.size).toBe(0);
    const choose = screen.getByRole("button", { name: "Choose accounts" });
    expect(choose.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(choose);
    expect(document.getElementById(choose.getAttribute("aria-controls")!)?.hidden).toBe(false);
    fireEvent.click(screen.getByRole("switch", { name: "Claude" }));
    expect(screen.queryByRole("button", { name: /Claude/ })).toBeNull();
    expect(screen.getByText("Not shown: Claude")).toBeTruthy();
    expect(JSON.parse(store.get(USAGE_CHOICES_KEY)!)).toEqual({ "claude|": false });
    expect(store.size).toBe(1);
    r.unmount();
    render(<Usage view={v} plans={null} now={now} />);
    expect(screen.queryByRole("button", { name: /Claude/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Choose accounts" }));
    fireEvent.click(screen.getByRole("switch", { name: "Claude" }));
    expect(screen.getByRole("button", { name: /Claude/ })).toBeTruthy();
  });
  it("defaults ended accounts off, labels their end, and allows an explicit on after remount", () => {
    const v = view([account({ terms: { ends: "2026-11-11" } })]);
    const r = render(<Usage view={v} plans={null} now={now} />);
    expect(screen.queryByRole("button", { name: /Claude/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Choose accounts" }));
    expect(screen.getByText(/Plan ended/)).toBeTruthy();
    fireEvent.click(screen.getByRole("switch", { name: "Claude" }));
    r.unmount(); render(<Usage view={v} plans={null} now={now} />);
    expect(screen.getByRole("button", { name: /Claude/ })).toBeTruthy();
  });
  it("survives storage reads and writes throwing", () => {
    vi.stubGlobal("localStorage", { getItem: () => { throw new Error("denied"); }, setItem: () => { throw new Error("full"); } });
    render(<Usage view={view()} plans={null} now={now} />);
    fireEvent.click(screen.getByRole("button", { name: "Choose accounts" }));
    fireEvent.click(screen.getByRole("switch", { name: "Claude" }));
    expect(screen.getByText("Not shown: Claude")).toBeTruthy();
  });
});
