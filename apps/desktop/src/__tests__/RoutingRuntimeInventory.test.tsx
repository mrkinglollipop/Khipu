import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import App from "../App";
import { revealSettingsGroup } from "../WhatsNew";
import { WELCOME_DONE_KEY } from "../Welcome";
import { checksView, lanesConfig, lanesView, setting } from "./routingSettingsFixtures";
import { discovery, status9B } from "./accountsFixtures";
// @ts-expect-error node:fs has no types in the desktop project
import { readFileSync } from "node:fs";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke: (...args: unknown[]) => invokeMock(...args) }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn(async () => null), save: vi.fn(async () => null) }));
vi.mock("@tauri-apps/api/path", () => ({ resourceDir: vi.fn(async () => "/tmp/khipu-fixture-resources") }));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: vi.fn(async () => "0.4.15") }));
vi.mock("@tauri-apps/plugin-updater", () => ({ check: vi.fn(async () => null) }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: vi.fn(async () => {}) }));

const cwd = (globalThis as unknown as { process: { cwd(): string } }).process.cwd();
const css = readFileSync(`${cwd}/src/App.css`, "utf8") + readFileSync(`${cwd}/src/routingOverview.css`, "utf8");
const scopeRows: string[] = readFileSync(`${cwd}/../../docs/plans/2026-11-03-settings-reorg.md`, "utf8")
  .split("## Inventory")[1].split("## Settings sync")[0].split("\n").filter((line: string) => line.startsWith("| ") && !line.startsWith("| Setting"))
  .map((line: string) => line.split("|")[1].trim());
let style: HTMLStyleElement;
let record: ReturnType<typeof lanesView>;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-14T15:40:00Z"));
  style = document.createElement("style");
  style.textContent = css;
  document.head.append(style);
  const values = new Map([[WELCOME_DONE_KEY, "1"]]);
  vi.stubGlobal("localStorage", { getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => void values.set(key, value), removeItem: (key: string) => void values.delete(key) });
  record = lanesView();
  const checks = checksView();
  record.priority = checks.priority;
  record.rules = [{ n: 3, text: "Keep reviews on Claude.", pending: true, pending_kind: "changed", prior_text: "Use Codex for reviews.", pending_by: "a fixture conversation" }];
  record.rules.push(...Array.from({ length: 8 }, (_, i) => ({ n: i + 10, text: `Reviewed fixture rule ${i + 10}`, confirmed: true })));
  record.contradictions = checks.contradictions;
  record.rule_hygiene = checks.rule_hygiene;
  record.tier_table = checks.tier_table;
  record.accounts![0].next_check = "2026-10-15T18:00:00Z";
  record.sources!.runners![0].lanes!.push("fixture-9", "fixture-10", "fixture-11", "fixture-12", "fixture-13");
  const cfg = lanesConfig();
  const routing = cfg.routing as Record<string, unknown>;
  routing.routing_line_chars = { ...setting(200), pending: true, words: "Longest line", request: { prior: 100, by_words: "a fixture conversation" } };
  routing.routing_replication_live = setting(false);
  routing.routing_proposal_text_between_macs = setting(true);
  routing.routing_usage_login_grok = { ...setting(true), login_found: true, ask: false };
  routing.routing_usage_login_cursor = { ...setting(false), login_found: true, ask: false };
  routing.routing_usage_login_opencode = { ...setting(false), login_found: true, ask: false };
  const source: string = readFileSync(`${cwd}/src/RoutingSettings.tsx`, "utf8");
  for (const match of source.matchAll(/key: "(routing_[a-z_]+)"/g)) if (!(match[1] in routing)) routing[match[1]] = setting(50);
  const health = { status: "red", identities: [{ identity: "codex:fixture", component: "main", status: "red", owed: 2 }],
    replication: { status: "green", age_seconds: 60 }, gateway: { status: "red", cleanup_gave_up: true } };
  const live = { ok: true, red: [], harnesses: {} };
  invokeMock.mockImplementation(async (cmd: string, payload?: { args?: string[] }) => {
    if (cmd === "dsn_configured") return true;
    if (cmd === "khipu_route_view") return JSON.stringify(record);
    if (cmd === "khipu_config_show") return JSON.stringify(cfg);
    if (cmd === "khipu_features_show") return JSON.stringify({ features: { routing_push: { enabled: true } } });
    if (cmd === "khipu_accounts_status") return JSON.stringify(status9B({ declined: [{ cli: "codex", path: "/tmp/fixture-declined-login", why: "not this account", at: "2026-10-13T14:00:00Z" }], request: { request: "track the fixture account", by: { label: "a fixture conversation" } } } as never));
    if (cmd === "khipu_accounts_discover") return JSON.stringify(discovery());
    if (cmd === "khipu_usage_status" || cmd === "khipu_usage_refresh") return JSON.stringify({ accounts: [{ key: "codex|", harness: "codex", account: "Codex", home: null, ok: true, windows: [] }] });
    if (cmd === "run_khipu") {
      const args = payload?.args ?? [];
      if (args[0] === "doctor") return JSON.stringify({ ok: true, capture_liveness: live, routing_delivery: health, claude_homes: { homes: [] } });
      if (args[0] === "status") return JSON.stringify({ counts: {}, recent_captures: [] });
      if (args[0] === "integrations" || args[0] === "owed" || args[0] === "revisions") return "[]";
      if (args[0] === "paths") return JSON.stringify({ files: [] });
      if (args[0] === "sources") return JSON.stringify({ sources: [], resolved: { unreachable: [] } });
      return "{}";
    }
    return JSON.stringify({ ok: true });
  });
});
afterEach(() => { cleanup(); style.remove(); invokeMock.mockReset(); vi.unstubAllGlobals(); vi.useRealTimers(); });

async function home(name: string) {
  const button = screen.getByRole("button", { name });
  fireEvent.click(button);
  expect(button).toHaveAttribute("aria-current", "page");
  const page = document.querySelector<HTMLElement>('[data-routing-page][data-active="true"]')!;
  expect(page).toBeVisible();
  return within(page);
}
async function rare(name: string) {
  const page = await home("Rarely needed");
  const button = page.getByRole("button", { name });
  if (button.getAttribute("aria-expanded") === "false") fireEvent.click(button);
  return page;
}
function buttons(page: ReturnType<typeof within>, names: string[]) {
  for (const name of names) expect(page.getByRole("button", { name: `Change: ${name}` })).toBeVisible();
}
type Probe = () => Promise<void>;
const probes: Probe[] = [
  async () => { const p = await home("Needs you"); expect(p.getByRole("article", { name: "A conversation changed which accounts Khipu reads" })).toBeVisible(); expect(p.getByRole("article", { name: /changed how long one line/ })).toBeVisible(); },
  async () => { expect(screen.queryByRole("button", { name: "Open all groups" })).toBeNull(); expect(screen.queryByRole("button", { name: "Close all groups" })).toBeNull(); expect(screen.getByRole("navigation", { name: "Accounts and routing pages" }).querySelectorAll("button")).toHaveLength(8); },
  async () => { const p = await home("Your accounts"); const list = p.getByRole("list", { name: "Accounts" }); fireEvent.click(within(list).getByRole("button", { name: /^Codex/ })); const d = within(screen.getByRole("dialog")); expect(d.getByRole("button", { name: "Pause Codex until…" })).toBeVisible(); fireEvent.click(d.getByRole("button", { name: "Pause Codex until…" })); expect(d.getByLabelText("Pause Codex until")).toBeVisible(); fireEvent.click(d.getByRole("button", { name: "Change…" })); expect(d.getByLabelText("Your words for Codex")).toBeVisible(); fireEvent.click(d.getByRole("button", { name: "Add a cap…" })); expect(d.getByRole("form", { name: "Add a cap for Codex" })).toBeVisible(); },
  async () => { const p = await home("Your accounts"); expect(p.getByRole("heading", { name: "Pace, caps and advice" })).toBeVisible(); expect(p.getByText(/Khipu checks again at/)).toBeVisible(); },
  async () => { const p = await home("Your accounts"); expect(p.getByLabelText("Sort by")).toBeVisible(); expect(p.getByRole("group", { name: "Show accounts" })).toBeVisible(); expect(p.getByRole("button", { name: "Check all now" })).toBeVisible(); },
  async () => { const p = await home("Your accounts"); expect(p.getByRole("button", { name: "Look for accounts now" })).toBeVisible(); expect(p.getByRole("button", { name: "Add an account…" })).toBeVisible(); },
  async () => { const p = await home("Your accounts"); expect(p.getByRole("switch", { name: "Track new accounts automatically" })).toBeVisible(); fireEvent.click(p.getByRole("button", { name: /Not offered again/ })); expect(p.getByRole("button", { name: "Offer /tmp/fixture-declined-login again" })).toBeVisible(); },
  async () => { const p = await rare("Reading usage from logins"); for (const name of ["Read Grok's usage from its own login", "Read Cursor's usage from its Keychain login", "Read OpenCode Go's usage from its own login"]) expect(p.getByRole("switch", { name })).toBeVisible(); },
  async () => { const p = await home("Who gets what work"); expect(p.getByRole("list", { name: "Your priority, first to last" })).toBeVisible(); expect(p.getByRole("button", { name: "Change the end…" })).toBeVisible(); expect(p.getByRole("button", { name: "Clear the priority" })).toBeVisible(); },
  async () => { expect((await home("Who gets what work")).getByRole("switch", { name: /With no priority set/ })).toBeVisible(); buttons(await rare("Priority and hold limits"), ["Warn when a priority a conversation set is still unconfirmed after"]); },
  async () => { const p = await home("Who gets what work"); expect(p.getByRole("button", { name: "Edit note" })).toBeVisible(); expect(p.getByRole("list", { name: "Standing rules" })).toBeVisible(); expect(p.getByRole("group", { name: "Show rules" })).toBeVisible(); expect(p.getByRole("searchbox", { name: "Search standing rules" })).toBeVisible(); expect(p.getByRole("button", { name: "Edit rule 3" })).toBeVisible(); fireEvent.click(p.getByRole("button", { name: "Add rule…" })); expect(p.getByLabelText("New rule")).toBeVisible(); },
  async () => { const p = await home("Needs you"); expect(p.getByRole("button", { name: "Keep the new words" })).toBeVisible(); expect(p.getByRole("button", { name: "Edit them first" })).toBeVisible(); expect(p.getByRole("button", { name: "Keep my words" })).toBeVisible(); },
  async () => { const p = await home("Who gets what work"); expect(p.getByRole("list", { name: "Holds" })).toBeVisible(); expect(p.getByRole("button", { name: /End the hold on/ })).toBeVisible(); fireEvent.click(p.getByRole("button", { name: "Add hold…" })); expect(p.getByLabelText("Words")).toBeVisible(); },
  async () => buttons(await rare("Priority and hold limits"), ["How long a conversation's hold lasts", "Longest words in a hold", "Remind me about my own holds after"]),
  async () => { const p = await home("Something's wrong"); expect(p.getByRole("list", { name: "Contradictions and out-of-date words" })).toBeVisible(); expect(p.getByRole("list", { name: "Rule upkeep" })).toBeVisible(); },
  async () => { const p = await rare("Model table"); expect(p.getByText("Scopes and designs")).toBeVisible(); expect(p.getByText("Searches and lookups")).toBeVisible(); },
  async () => { const p = await home("Conversations"); expect(p.getByRole("list", { name: "Live conversations" })).toBeVisible(); expect(p.getByRole("list", { name: "What each kind of conversation hears" })).toBeVisible(); expect(p.getByText(/old.*routing off.*file/)).toBeVisible(); },
  async () => { const p = await home("Your Macs"); expect(p.getByRole("list", { name: "Other Macs and the cloud" })).toBeVisible(); },
  async () => { const p = await home("Conversations"); for (const name of ["Tell conversations about routing", "At the start of a conversation, and after it is compacted", "Before each prompt", "Before handing work to another agent"]) expect(p.getByRole("switch", { name })).toBeVisible(); },
  async () => { const p = await rare("Sizes and timing"); expect(p.queryByRole("button", { name: "Change your hand-off scripts" })).toBeNull(); expect(p.queryByText("Your hand-off scripts")).toBeNull(); },
  async () => buttons(await rare("Sizes and timing"), ["Longest summary at the start of a conversation", "Longest update before a prompt", "Longest routing block for a subagent at its start", "Longest line", "Longest standing rule", "Most standing rules", "Longest note", "Most of your rules, note and priority shown at the start of a conversation"]),
  async () => buttons(await rare("Sizes and timing"), ["Send everything again when a conversation is this far behind", "Treat a report as out of date after", "How long a limit reported without a time lasts"]),
  async () => buttons(await rare("Sizes and timing"), ["Warn when reading before a prompt is too slow on more than", "Warn when two reports disagree for", "Say the record is not refreshed after"]),
  async () => buttons(await rare("Sizes and timing"), ["Time allowed before a prompt", "Time allowed at the start of a conversation", "Remove a conversation's delivery record after it is unused for"]),
  async () => { const p = await home("Your Macs"); expect(p.getByRole("switch", { name: "Copy routing between your Macs through the hub" })).toBeVisible(); expect(p.getByRole("switch", { name: "Share proposal words between your Macs" })).toBeVisible(); expect(p.getByRole("button", { name: "Retry cleanup" })).toBeVisible(); },
  async () => { const p = await rare("Checking accounts when they reset"); expect(p.getByRole("switch", { name: "Check an account at its reset time" })).toBeVisible(); buttons(p, ["Wait after the reset time"]); expect(p.getByLabelText("First retry in seconds")).toBeVisible(); expect(p.getByLabelText("Second retry in seconds")).toBeVisible(); },
  async () => { const p = await rare("Checking accounts when they reset"); fireEvent.click(p.getByRole("button", { name: "Change: Wait while the Mac is busy" })); for (const label of ["Load", "Wait", "Busy attempts"]) expect(within(p.getByRole("form", { name: "Change: Wait while the Mac is busy" })).getByLabelText(label)).toBeVisible(); },
  async () => { const p = await rare("Providers and account setup"); expect(p.getByRole("list", { name: "Providers" })).toBeVisible(); expect(p.getByRole("button", { name: "Add provider account" })).toBeVisible(); const w = await home("Something's wrong"); expect(w.getByRole("list", { name: "Job slots" })).toBeVisible(); expect(w.getByRole("searchbox", { name: /Search.*job slots/ })).toBeVisible(); expect(w.getAllByRole("button", { name: /Change the account for/ }).length).toBeGreaterThan(0); },
  async () => { const p = await home("Something's wrong"); expect(p.getByRole("list", { name: "Job runner folders" })).toBeVisible(); expect(p.getByRole("button", { name: "Add a folder…" })).toBeVisible(); const r = await rare("Old routing table file"); expect(r.getByRole("button", { name: "Change the routing table file" })).toBeVisible(); },
  async () => { const p = await home("What changed"); expect(p.getByRole("group", { name: "Changes by" })).toBeVisible(); expect(p.getByRole("group", { name: "Changes to" })).toBeVisible(); expect(p.getByRole("button", { name: "Bring back standing rule 6" })).toBeVisible(); },
];

describe("Runtime parity for every approved scope inventory row", () => {
  it("keeps a probe for every row, including the deliberately removed fold controls", () => expect(probes).toHaveLength(scopeRows.length));
  it.each(scopeRows.map((row, i) => [i + 1, row, probes[i]] as const))("row %i: %s", async (_i, _row, probe) => {
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Accounts and routing" }));
    await waitFor(() => expect(document.querySelector('[data-settings-group="resets"]')).toHaveTextContent("On"));
    await probe();
  });
  it.each([['accounts', 'accounts'], ['priority', 'work'], ['rules', 'work'], ['holds', 'work'], ['checks', 'wrong'], ['telling', 'conversations'], ['resets', 'rare'], ['runners', 'wrong'], ['table', 'rare'], ['changes', 'changes'], ['pace', 'accounts']])("legacy link %s selects %s and focuses its visible control", async (group, page) => {
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Accounts and routing" }));
    await waitFor(() => expect(document.querySelector('[data-settings-group="resets"]')).toHaveTextContent("On"));
    revealSettingsGroup(group);
    const target = document.querySelector<HTMLElement>(`[data-settings-group="${group}"]`)!;
    await waitFor(() => expect(document.activeElement).toBe(target));
    expect(target).toBeVisible();
    expect(target.closest('[data-routing-page]')).toHaveAttribute('data-routing-page', page);
    expect(target.closest('[data-routing-page]')).toHaveAttribute('data-active', 'true');
  });
  it("removing the last ordered account reaches the existing clear writer", async () => {
    record.priority = { entries: [{ account: "codex|", label: "Codex", role: "Builds" }], version: "last-entry" };
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Accounts and routing" }));
    await waitFor(() => expect(document.querySelector('[data-settings-group="resets"]')).toHaveTextContent("On"));
    const p = await home("Who gets what work");
    fireEvent.click(p.getByRole("button", { name: "Remove Codex from your priority" }));
    await waitFor(() => expect(invokeMock).toHaveBeenCalledWith("khipu_route_priority", expect.objectContaining({ action: "clear", expect: "last-entry" })));
  });
  it("the start-of-conversation switch reaches its unchanged writer", async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Accounts and routing" }));
    await waitFor(() => expect(document.querySelector('[data-settings-group="resets"]')).toHaveTextContent("On"));
    const p = await home("Conversations");
    fireEvent.click(p.getByRole("switch", { name: "At the start of a conversation, and after it is compacted" }));
    await waitFor(() => expect(invokeMock).toHaveBeenCalledWith("khipu_config_set", expect.objectContaining({ key: "routing_push_start", value: "off" })));
  });
  it("finds settings by their old homes and opens the matching page", async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Accounts and routing" }));
    await waitFor(() => expect(document.querySelector('[data-settings-group="resets"]')).toHaveTextContent("On"));
    const find = screen.getByRole("searchbox", { name: "Find a setting" });
    fireEvent.change(find, { target: { value: "routing copy" } });
    const option = screen.getByRole("option", { name: /Copy routing between your Macs/ });
    expect(option).toHaveTextContent("Your Macs");
    fireEvent.click(option);
    const macs = document.querySelector<HTMLElement>('[data-routing-page="macs"]')!;
    await waitFor(() => expect(macs).toHaveAttribute("data-active", "true"));
    expect(within(macs).getByRole("switch", { name: "Copy routing between your Macs through the hub" })).toBeVisible();
    fireEvent.change(find, { target: { value: "telling" } });
    fireEvent.click(screen.getByRole("option", { name: /Tell conversations about routing/ }));
    const conversations = document.querySelector<HTMLElement>('[data-routing-page="conversations"]')!;
    await waitFor(() => expect(conversations).toHaveAttribute("data-active", "true"));
    expect(within(conversations).getByRole("switch", { name: "Tell conversations about routing" })).toBeVisible();
  });

  it("says on each rarely needed setting whether it is the same on every Mac or this Mac only", async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Accounts and routing" }));
    await waitFor(() => expect(document.querySelector('[data-settings-group="resets"]')).toHaveTextContent("On"));
    const p = await rare("Sizes and timing");
    const row = (name: string) => p.getByRole("button", { name: `Change: ${name}` }).closest(".route-limit") as HTMLElement;
    expect(row("Longest line")).toHaveTextContent("Same on my Macs");
    expect(row("Time allowed before a prompt")).toHaveTextContent("This Mac only");
  });

  it("lists copied old-file rules once under When you have time without asking for confirmation", async () => {
    record.rules = [{ n: 77, text: "Never use a fast tier.", seeded: true, confirmed: false }];
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Accounts and routing" }));
    await waitFor(() => expect(document.querySelector('[data-settings-group="resets"]')).toHaveTextContent("On"));
    const needs = await home("Needs you");
    const later = needs.getByRole("list", { name: "When you have time" });
    expect(within(later).getByText("1 rule copied from your old routing file was never reviewed")).toBeVisible();
    fireEvent.click(within(later).getByRole("button", { name: "Read them" }));
    expect(within(later).getByText("Never use a fast tier.")).toBeVisible();
    expect(within(later).queryByRole("button", { name: "Keep as written" })).toBeNull();
    const work = await home("Who gets what work");
    expect(work.queryByRole("button", { name: "Confirm rule 77" })).toBeNull();
  });
  it("the rule's More control reveals all words and origin in place without changing its writer buttons", async () => {
    record.rules![0].text = "Keep the complete original wording, including extraordinarilylongunbrokenwordsandpaths0123456789. ".repeat(40);
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Accounts and routing" }));
    await waitFor(() => expect(document.querySelector('[data-settings-group="resets"]')).toHaveTextContent("On"));
    const p = await home("Who gets what work");
    const more = p.getByRole("button", { name: "More about rule 3" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(more);
    expect(p.getByRole("button", { name: "Less about rule 3" })).toHaveAttribute("aria-expanded", "true");
    const row = more.closest("li")!;
    expect(row.querySelector(".rule-preview")?.textContent).toBe(record.rules![0].text);
    expect(within(row).getByRole("button", { name: "Edit rule 3" })).toBeVisible();
    expect(within(row).getByRole("button", { name: "Remove rule 3" })).toBeVisible();
    expect(invokeMock.mock.calls.filter(([name]) => name === "khipu_route_rule")).toHaveLength(0);
  });

  it("page changes reset the shared scroll container, and the work jump row focuses its destination", async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Accounts and routing" }));
    await waitFor(() => expect(document.querySelector('[data-settings-group="resets"]')).toHaveTextContent("On"));
    const main = document.querySelector<HTMLElement>(".main-content")!;
    Object.defineProperty(main, "scrollTo", { configurable: true, value: ({ top }: { top: number }) => { main.scrollTop = top; } });
    for (const page of ["Your accounts", "Who gets what work", "Your Macs", "Conversations", "What changed", "Something's wrong", "Rarely needed", "Needs you"]) {
      main.scrollTop = 500;
      await home(page);
      expect(main.scrollTop, page).toBe(0);
    }
    const p = await home("Who gets what work");
    const target = document.getElementById("route-work-rules")!;
    target.scrollIntoView = vi.fn();
    fireEvent.click(p.getByRole("button", { name: "Rules" }));
    expect(target.scrollIntoView).toHaveBeenCalledWith({ block: "start" });
    expect(document.activeElement).toBe(target);
  });

});
