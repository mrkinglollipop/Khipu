// Setup's Accounts step (docs/plans/2026-10-15-settings-clarity.md, phase 7):
// nothing is written until Next, and only what the owner changed.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { accountId, planAccountList, SetupAccountsStep } from "../SetupAccounts";
import { Welcome } from "../Welcome";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn(async () => null), save: vi.fn(async () => null) }));
vi.mock("@tauri-apps/api/path", () => ({ resourceDir: vi.fn(async () => "/Applications/Khipu.app/Contents/Resources") }));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: vi.fn(async () => "0.4.0") }));
vi.mock("@tauri-apps/plugin-updater", () => ({ check: vi.fn(async () => null) }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: vi.fn(async () => {}) }));

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
});

const FAR = "2099-01-01T00:00:00Z";
const ENTRIES = [
  { label: "Claude Code", harness: "claude" },
  { label: "Codex", harness: "codex" },
  { label: "Grok Build", harness: "grok" },
  { label: "Muse", harness: "muse" },
];

function report(rows?: unknown[]) {
  return {
    generated_at: "2026-10-15T12:00:00Z",
    max_age_s: 120,
    refreshing: false,
    accounts: rows ?? [
      { harness: "claude", account: "Claude Code", home: null, ok: true, plan: "max", windows: [{ name: "weekly", used_pct: 38, resets_at: FAR, window_mins: 10080 }] },
      { harness: "codex", account: "Codex", home: null, ok: true, plan: "pro", windows: [] },
      { harness: "grok", account: "Grok Build", home: null, ok: false, error: "not_signed_in", windows: [] },
      { harness: "muse", account: "Muse", home: null, ok: true, windows: [], note: "no turn observed yet" },
    ],
  };
}

type Opts = {
  rows?: unknown[];
  accounts?: unknown[];
  featuresFail?: boolean;
  hang?: boolean;
  routeFail?: boolean;
  priority?: unknown;
  refreshFail?: boolean;
  source?: "file" | "default";
  configFailAfterFirst?: boolean;
  failOnce?: string[];
  /** Writes whose next call finds the thing changed elsewhere first. */
  raceOnce?: string[];
};

/** What the fake Khipu holds now: writes change it, and a test can change
 *  it "elsewhere" while the step is open. */
type World = {
  source: "file" | "default";
  accounts: Array<{ label: string; harness: string; home?: string | null }>;
  priority: { entries: Array<{ account: string; label: string; role?: string | null; until?: string | null }>; until?: string | null } | null;
  push: boolean;
  rows: unknown[];
  /** Bumped on every write, as the CLI's versions move. */
  listVersion: number;
  priorityVersion: number;
};
let world: World;
/** Commands whose next answer is captured when asked and held until released. */
let holding: Set<string>;
let held: Array<() => void>;

function fake(o: Opts = {}): World {
  const failing = new Set(o.failOnce ?? []);
  const defaults = (o.accounts ?? ENTRIES) as World["accounts"];
  world = {
    source: o.source ?? "default",
    accounts: [...defaults],
    priority: (o.priority as World["priority"]) ?? null,
    push: true,
    rows: (o.rows ?? report().accounts) as unknown[],
    listVersion: 1,
    priorityVersion: 0,
  };
  const racing = new Set(o.raceOnce ?? []);
  const moved = { ok: false, error: "the account list changed after it was read; read it again", code: "version_moved" };
  // The CLI's compare-and-swap: a write that names a version is refused once it moved.
  const listMoved = (cmd: string, args: Record<string, unknown> | undefined) => {
    if (racing.delete(cmd)) world.listVersion += 1;
    return args?.expect != null && args.expect !== `a${world.listVersion}`;
  };
  holding = new Set();
  held = [];
  const answer = (cmd: string, args: Record<string, unknown> | undefined): string => {
    switch (cmd) {
      case "khipu_usage_refresh":
        if (o.refreshFail) throw new Error("probe failed");
        return JSON.stringify(report(world.rows));
      case "khipu_usage_status":
        return JSON.stringify(report(world.rows));
      case "khipu_config_show":
        if (o.configFailAfterFirst && calls("khipu_config_show").length > 1) throw new Error("config failed");
        return JSON.stringify({
          usage_accounts: { source: world.source, accounts: world.accounts, user_home: "/Users/test", version: `a${world.listVersion}` },
        });
      case "khipu_config_set":
        if (listMoved(cmd, args)) return JSON.stringify(moved);
        world.accounts = JSON.parse(String(args?.value));
        world.source = "file";
        world.listVersion += 1;
        return JSON.stringify({ ok: true });
      case "khipu_config_unset":
        if (listMoved(cmd, args)) return JSON.stringify(moved);
        world.accounts = [...defaults];
        world.source = "default";
        world.listVersion += 1;
        return JSON.stringify({ ok: true });
      case "khipu_features_show":
        if (o.featuresFail) throw new Error("no");
        return JSON.stringify({ features: { routing_push: { enabled: world.push, source: "default" } } });
      case "khipu_account_sign_in_offers":
        return JSON.stringify({ claude: { offered: true }, codex: { offered: false }, grok: { offered: false }, muse: { offered: false } });
      case "khipu_feature_set":
        world.push = args?.enabled === true;
        return JSON.stringify({ ok: true });
      case "khipu_route_view":
        if (o.routeFail) throw new Error("route failed");
        return JSON.stringify({
          accounts: [],
          priority: world.priority ? { ...world.priority, version: `p${world.priorityVersion}` } : null,
          priority_version: `p${world.priorityVersion}`,
        });
      case "khipu_route_priority": {
        if (racing.delete(cmd)) world.priorityVersion += 1;
        if (args?.expect != null && args.expect !== `p${world.priorityVersion}`) {
          return JSON.stringify({ ...moved, error: "the priority changed after it was read; read it again" });
        }
        world.priorityVersion += 1;
        const entries = (args?.entries ?? []) as Array<{ account: string; role?: string | null; until?: string | null }>;
        world.priority = { entries: entries.map((e) => ({ ...e, label: e.account })), until: (args?.until as string | null) ?? null };
        return JSON.stringify({ ok: true });
      }
      default:
        return JSON.stringify({ ok: true });
    }
  };
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    if (o.hang) return new Promise(() => {});
    if (failing.delete(cmd)) throw new Error(`${cmd} failed`);
    if (holding.delete(cmd)) {
      const now = (() => {
        try {
          return { ok: true as const, v: answer(cmd, args) };
        } catch (e) {
          return { ok: false as const, e };
        }
      })();
      return new Promise((resolve, reject) => held.push(() => (now.ok ? resolve(now.v) : reject(now.e))));
    }
    return answer(cmd, args);
  });
  return world;
}

const WRITES = ["khipu_config_set", "khipu_config_unset", "khipu_route_priority", "khipu_feature_set"];
const calls = (name: string) => invokeMock.mock.calls.filter((c) => c[0] === name);

function mount() {
  const onBack = vi.fn();
  const onNext = vi.fn();
  render(<SetupAccountsStep active onBack={onBack} onNext={onNext} />);
  return { onBack, onNext };
}

async function ready() {
  await screen.findByText("Default login: signed in, Max, 62% of its week left");
  await screen.findAllByLabelText("Read this account's plan");
}

describe("Setup › Accounts step", () => {
  it("uses the same offered sign-in capabilities as Add an account", async () => {
    fake();
    mount();
    await ready();
    expect(await screen.findByText("Sign in to a second Claude account, or choose a login kept somewhere else.")).toBeInTheDocument();
  });

  it("Skip for now writes nothing", async () => {
    fake();
    const { onNext } = mount();
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    expect(onNext).toHaveBeenCalled();
    for (const w of WRITES) expect(calls(w)).toHaveLength(0);
  });

  it("Next with the list unchanged writes no priority", async () => {
    fake();
    const { onNext } = mount();
    await ready();
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    for (const w of WRITES) expect(calls(w)).toHaveLength(0);
  });

  it("moving an account then Next writes the new order", async () => {
    fake();
    const { onNext } = mount();
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Move Codex up" }));
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    const pri = calls("khipu_route_priority");
    expect(pri).toHaveLength(1);
    expect(pri[0][1].action).toBe("set");
    expect(pri[0][1].entries.map((e: { account: string }) => e.account)).toEqual(["codex|", "claude|", "grok|", "muse|"]);
  });

  it("unchecking an account's plan read then Next writes the list without it", async () => {
    fake();
    const { onNext } = mount();
    await ready();
    fireEvent.click(screen.getAllByLabelText("Read this account's plan")[1]);
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    const set = calls("khipu_config_set");
    expect(set).toHaveLength(1);
    expect(set[0][1].key).toBe("usage_accounts");
    const value = JSON.parse(set[0][1].value);
    expect(value.map((e: { harness: string }) => e.harness)).toEqual(["claude", "grok", "muse"]);
  });

  it("a not signed in account shows its sign-in words", async () => {
    fake();
    mount();
    await ready();
    expect(screen.getByText("Default login: not signed in. Sign in from Grok Build, then Check again.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check Grok Build again" })).toBeInTheDocument();
  });

  it("reads each account in plain words", async () => {
    fake();
    mount();
    await ready();
    expect(screen.getByText("Found. It reports its plan after its first turn.")).toBeInTheDocument();
  });

  it("while reads are pending the rows say Reading… and there is no checkbox or switch", async () => {
    fake({ hang: true });
    mount();
    expect(screen.getAllByText("Reading…").length).toBeGreaterThanOrEqual(4);
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("a features read failure shows Can't read this setting instead of a switch", async () => {
    fake({ featuresFail: true });
    mount();
    await ready();
    expect(await screen.findByText("Can't read this setting")).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: "Tell my conversations about this" })).not.toBeInTheDocument();
  });

  it("with no installed accounts the No accounts found callout shows", async () => {
    fake({ rows: [] });
    mount();
    expect(await screen.findByText("No accounts found on this Mac")).toBeInTheDocument();
  });

  it("with every account not installed the No accounts found callout shows", async () => {
    fake({ rows: ENTRIES.map((e) => ({ harness: e.harness, account: e.label, home: null, ok: false, error: "not_installed", windows: [] })) });
    mount();
    expect(await screen.findByText("No accounts found on this Mac")).toBeInTheDocument();
  });
});

describe("Setup › Accounts step, review fixes", () => {
  it("a failed routing read never writes a priority, and Next still saves other changes", async () => {
    fake({ routeFail: true });
    const { onNext } = mount();
    await ready();
    await screen.findByText(/couldn't read your current order/);
    expect(screen.getByRole("button", { name: "Move Codex up" })).toBeDisabled();
    fireEvent.click(screen.getAllByLabelText("Read this account's plan")[3]);
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(calls("khipu_route_priority")).toHaveLength(0);
    expect(calls("khipu_config_set")).toHaveLength(1);
  });

  it("rechecking an account after a move keeps it in the order and the priority", async () => {
    fake();
    const { onNext } = mount();
    await ready();
    const boxes = () => screen.getAllByLabelText("Read this account's plan");
    fireEvent.click(boxes()[1]);
    fireEvent.click(screen.getByRole("button", { name: "Move Muse up" }));
    fireEvent.click(boxes()[1]);
    expect(screen.getByRole("button", { name: "Move Codex up" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    const pri = calls("khipu_route_priority");
    expect(pri).toHaveLength(1);
    expect(pri[0][1].entries.map((e: { account: string }) => e.account)).toEqual(["claude|", "codex|", "muse|", "grok|"]);
  });

  it("a priority entry with no plan row is kept when the order is saved", async () => {
    fake({
      priority: {
        entries: [
          { account: "claude|/Users/test/.claude-second", label: "Claude second", role: "builds", until: null },
          { account: "claude|", label: "Claude Code", role: null, until: null },
        ],
        until: null,
      },
    });
    const { onNext } = mount();
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Move Codex up" }));
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    const entries = calls("khipu_route_priority")[0][1].entries;
    expect(entries).toContainEqual({ account: "claude|/Users/test/.claude-second", role: "builds", until: null });
  });

  it("a failed Check again says so beside the figures it keeps", async () => {
    fake({ refreshFail: true });
    mount();
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Check Grok Build again" }));
    expect(await screen.findByText(/last check didn't work/)).toBeInTheDocument();
    expect(screen.getByText("Default login: signed in, Max, 62% of its week left")).toBeInTheDocument();
  });

  it("an account left out of your own list is not called not found", async () => {
    fake({
      source: "file",
      accounts: [ENTRIES[0]],
      rows: [report().accounts[0]],
    });
    mount();
    await screen.findByText("Default login: signed in, Max, 62% of its week left");
    await screen.findByText(/Not in your account list: Codex, Grok Build and Muse/);
    expect(screen.queryByText(/Not found on this Mac/)).not.toBeInTheDocument();
  });

  it("an explicit not installed result is called not found", async () => {
    fake({ source: "file", rows: [report().accounts[0], { harness: "codex", account: "Codex", ok: false, error: "not_installed" }],
      accounts: ENTRIES.slice(0, 2) });
    mount();
    await screen.findByText(/Not found on this Mac: Codex\./);
  });
});

describe("Setup › Accounts step, review round 2", () => {
  it("a priority entry is matched by its account, never by a label another account now has", async () => {
    fake({
      priority: {
        entries: [{ account: "codex|/Users/test/.codex-old", label: "Codex", role: "reviews", until: "2099-01-01T00:00:00Z" }],
        until: null,
      },
    });
    const { onNext } = mount();
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Move Muse up" }));
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    const entries = calls("khipu_route_priority")[0][1].entries;
    expect(entries).toContainEqual({ account: "codex|/Users/test/.codex-old", role: "reviews", until: "2099-01-01T00:00:00Z" });
    expect(entries).toContainEqual({ account: "codex|", role: null, until: null });
  });

  it("a priority entry with a tilde login folder matches its account by login folder", async () => {
    const rows = [...report().accounts, { harness: "claude", account: "Claude second", home: "~/.claude-second", ok: true, windows: [] }];
    fake({
      rows,
      accounts: [...ENTRIES, { label: "Claude second", harness: "claude", home: "~/.claude-second" }],
      priority: { entries: [{ account: "claude|/Users/test/.claude-second", label: "Renamed", role: "builds", until: null }], until: null },
    });
    const { onNext } = mount();
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Move Codex up" }));
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    const entries = calls("khipu_route_priority")[0][1].entries;
    expect(entries[0]).toEqual({ account: "claude|~/.claude-second", role: "builds", until: null });
    expect(entries).toHaveLength(5);
  });

  it("a failed Check again shows even when the last good check found no accounts", async () => {
    fake({ rows: [], refreshFail: true });
    mount();
    await screen.findByText("No accounts found on this Mac");
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByText(/last check didn't work/)).toBeInTheDocument();
  });

  it("a failed re-read of the account list says so beside the checkboxes it keeps", async () => {
    fake({ refreshFail: true, configFailAfterFirst: true });
    mount();
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Check Grok Build again" }));
    expect(await screen.findByText(/couldn't read the account list again/)).toBeInTheDocument();
  });
});

describe("Setup › Accounts step, review round 3", () => {
  const TWO_CODEX = [
    { label: "Codex A", harness: "codex", home: "/tmp/a-b" },
    { label: "Codex B", harness: "codex", home: "/tmp/a/b" },
  ];
  const twoCodexRows = () => [
    report().accounts[0],
    { harness: "codex", account: "Codex A", home: "/tmp/a-b", ok: true, plan: "pro", windows: [] },
    { harness: "codex", account: "Codex B", home: "/tmp/a/b", ok: true, plan: "pro", windows: [] },
  ];

  it("two accounts whose login folders differ only in punctuation get their own checkbox", async () => {
    fake({ source: "file", accounts: [ENTRIES[0], ...TWO_CODEX], rows: twoCodexRows() });
    const { onNext } = mount();
    await ready();
    const labels = screen.getAllByText("Read this account's plan");
    fireEvent.click(labels[2]);
    const boxes = screen.getAllByLabelText("Read this account's plan") as HTMLInputElement[];
    expect(boxes.map((b) => b.checked)).toEqual([true, true, false]);
    expect(new Set(boxes.map((b) => b.id)).size).toBe(3);
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    const sent = JSON.parse(calls("khipu_config_set")[0][1].value);
    expect(sent.map((e: { label: string }) => e.label)).toEqual(["Claude Code", "Codex A"]);
  });

  it("a retry after a partial save re-sends a rechecked account", async () => {
    fake({ failOnce: ["khipu_route_priority"] });
    const { onNext } = mount();
    await ready();
    const boxes = () => screen.getAllByLabelText("Read this account's plan");
    fireEvent.click(boxes()[1]);
    fireEvent.click(screen.getByRole("button", { name: "Move Muse up" }));
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await screen.findByText(/couldn't save the order/);
    expect(calls("khipu_config_set")).toHaveLength(1);
    fireEvent.click(boxes()[1]);
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    // Back to every account, as at the start: the default list again.
    expect(calls("khipu_config_unset")).toHaveLength(1);
    expect(world.source).toBe("default");
  });

  it("a retry after a partial save sends only what is still unsaved", async () => {
    fake({ failOnce: ["khipu_route_priority"] });
    const { onNext } = mount();
    await ready();
    fireEvent.click(screen.getAllByLabelText("Read this account's plan")[1]);
    fireEvent.click(screen.getByRole("button", { name: "Move Muse up" }));
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await screen.findByText(/saved which accounts to read, but couldn't save the order/);
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(calls("khipu_config_set")).toHaveLength(1);
    expect(calls("khipu_route_priority")).toHaveLength(2);
  });

  it("a saved order is not sent again when only the telling switch failed", async () => {
    fake({ failOnce: ["khipu_feature_set"] });
    const { onNext } = mount();
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Move Muse up" }));
    fireEvent.click(screen.getByRole("switch", { name: "Tell my conversations about this" }));
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await screen.findByText(/saved the order, but couldn't change whether conversations are told/);
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(calls("khipu_route_priority")).toHaveLength(1);
    expect(calls("khipu_feature_set")).toHaveLength(2);
  });
});

describe("Setup › Accounts step, changes as operations on what is saved now", () => {
  const rowOf = (h: string) => (report().accounts as Array<{ harness: string }>).find((r) => r.harness === h)!;
  const checkAgain = () => fireEvent.click(screen.getByRole("button", { name: "Check Grok Build again" }));
  const next = () => fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
  const box = (i: number) => screen.getAllByLabelText("Read this account's plan")[i] as HTMLInputElement;

  it("an account removed elsewhere while setup is open is not written back", async () => {
    fake();
    const { onNext } = mount();
    await ready();
    world.source = "file";
    world.accounts = ENTRIES.filter((e) => e.harness !== "codex");
    world.rows = world.rows.filter((r) => (r as { harness: string }).harness !== "codex");
    checkAgain();
    await waitFor(() => expect(screen.queryAllByText("Codex")).toHaveLength(0));
    next();
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(world.accounts.map((e) => e.harness)).toEqual(["claude", "grok", "muse"]);
  });

  it("an account renamed elsewhere keeps its new name when another account is unchecked", async () => {
    fake();
    const { onNext } = mount();
    await ready();
    world.source = "file";
    world.accounts = ENTRIES.map((e) => (e.harness === "codex" ? { ...e, label: "Codex work" } : e));
    world.rows = world.rows.map((r) => ((r as { harness: string }).harness === "codex" ? { ...(r as object), account: "Codex work" } : r));
    checkAgain();
    await screen.findAllByText("Codex work");
    fireEvent.click(box(3));
    next();
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(world.accounts.map((e) => e.label)).toEqual(["Claude Code", "Codex work", "Grok Build"]);
  });

  it("an account added elsewhere is kept", async () => {
    fake();
    const { onNext } = mount();
    await ready();
    fireEvent.click(box(3));
    world.source = "file";
    world.accounts = [...ENTRIES, { label: "Claude second", harness: "claude", home: "~/.claude-second" }];
    next();
    await screen.findByText(/changed while setup was open/);
    next();
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(world.accounts.map((e) => e.label)).toEqual(["Claude Code", "Codex", "Grok Build", "Claude second"]);
  });

  it("an old account-list answer arriving late never undoes a retry", async () => {
    fake({ failOnce: ["khipu_route_priority"] });
    const { onNext } = mount();
    await ready();
    holding.add("khipu_config_show");
    checkAgain();
    fireEvent.click(box(1));
    fireEvent.click(screen.getByRole("button", { name: "Move Muse up" }));
    next();
    await screen.findByText(/couldn't save the order/);
    expect(world.accounts.map((e) => e.harness)).toEqual(["claude", "grok", "muse"]);
    held.forEach((release) => release());
    await waitFor(() => expect(box(1).checked).toBe(false));
    fireEvent.click(box(1));
    next();
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(world.accounts.map((e) => e.harness).sort()).toEqual(["claude", "codex", "grok", "muse"]);
  });

  it("a stale read never wins on screen", async () => {
    fake();
    mount();
    await ready();
    holding.add("khipu_config_show");
    checkAgain();
    world.source = "file";
    world.accounts = ENTRIES.filter((e) => e.harness !== "muse");
    await waitFor(() => expect(screen.getByRole("button", { name: "Check Grok Build again" })).not.toBeDisabled());
    checkAgain();
    await waitFor(() => expect(box(3).checked).toBe(false));
    held.forEach((release) => release());
    await new Promise((r) => setTimeout(r, 50));
    expect(box(3).checked).toBe(false);
  });

  it("with only an uninstalled account left, the help says what Next saves and Next does that", async () => {
    fake({
      source: "file",
      accounts: ENTRIES.slice(0, 2),
      rows: [rowOf("claude"), { harness: "codex", account: "Codex", home: null, ok: false, error: "not_installed", windows: [] }],
    });
    const { onNext } = mount();
    await ready();
    fireEvent.click(box(0));
    expect(await screen.findByText("Next saves the account list as: Codex.")).toBeInTheDocument();
    next();
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(world.accounts.map((e) => e.label)).toEqual(["Codex"]);
  });

  it("a retry after any partial failure converges, then writes nothing more", async () => {
    fake({ failOnce: ["khipu_config_set", "khipu_route_priority", "khipu_feature_set"] });
    const { onNext } = mount();
    await ready();
    fireEvent.click(box(1));
    fireEvent.click(screen.getByRole("button", { name: "Move Muse up" }));
    fireEvent.click(screen.getByRole("switch", { name: "Tell my conversations about this" }));
    for (const said of [/couldn't save which accounts/, /couldn't save the order/, /couldn't change whether conversations are told/]) {
      next();
      await screen.findByText(said);
    }
    next();
    await waitFor(() => expect(onNext).toHaveBeenCalledTimes(1));
    expect(world.accounts.map((e) => e.harness)).toEqual(["claude", "grok", "muse"]);
    expect(world.priority?.entries.map((e) => e.account)).toEqual(["claude|", "muse|", "grok|"]);
    expect(world.push).toBe(false);
    const writes = WRITES.map((w) => calls(w).length);
    next();
    await waitFor(() => expect(onNext).toHaveBeenCalledTimes(2));
    expect(WRITES.map((w) => calls(w).length)).toEqual(writes);
  });
});

describe("Setup › Accounts step, review round 5", () => {
  const next = () => fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
  const box = (i: number) => screen.getAllByLabelText("Read this account's plan")[i] as HTMLInputElement;

  it("a retry never replays a move that was already saved", async () => {
    fake({ failOnce: ["khipu_feature_set"] });
    const { onNext } = mount();
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Move Codex up" }));
    fireEvent.click(screen.getByRole("button", { name: "Move Claude Code down" }));
    fireEvent.click(screen.getByRole("switch", { name: "Tell my conversations about this" }));
    next();
    await screen.findByText(/saved the order, but couldn't change whether conversations are told/);
    expect(world.priority?.entries.map((e) => e.account)).toEqual(["codex|", "grok|", "claude|", "muse|"]);
    next();
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(world.priority?.entries.map((e) => e.account)).toEqual(["codex|", "grok|", "claude|", "muse|"]);
    expect(calls("khipu_route_priority")).toHaveLength(1);
  });

  it("checking every account again after a partial save goes back to the default list", async () => {
    fake({ failOnce: ["khipu_route_priority"] });
    const { onNext } = mount();
    await ready();
    fireEvent.click(box(1));
    fireEvent.click(screen.getByRole("button", { name: "Move Muse up" }));
    next();
    await screen.findByText(/couldn't save the order/);
    expect(world.source).toBe("file");
    fireEvent.click(box(1));
    expect(await screen.findByText(/Next goes back to reading every account Khipu finds/)).toBeInTheDocument();
    next();
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(world.source).toBe("default");
  });

  it("changes made elsewhere while Next waits on the order are kept", async () => {
    fake();
    const { onNext } = mount();
    await ready();
    fireEvent.click(box(3));
    fireEvent.click(screen.getByRole("button", { name: "Move Codex up" }));
    holding.add("khipu_route_view");
    next();
    await waitFor(() => expect(held.length).toBe(1));
    world.accounts = world.accounts.map((e) => (e.harness === "codex" ? { ...e, label: "Codex work" } : e));
    world.accounts.push({ label: "Claude second", harness: "claude", home: "~/.claude-second" });
    held.forEach((release) => release());
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(world.accounts.map((e) => e.label)).toEqual(["Claude Code", "Codex work", "Grok Build", "Claude second"]);
  });

  it("when the saved list changed after the help line was shown, Next writes nothing and shows the new plan", async () => {
    fake();
    const { onNext } = mount();
    await ready();
    fireEvent.click(box(3));
    expect(screen.getByText("Next saves the account list as: Claude Code, Codex and Grok Build.")).toBeInTheDocument();
    world.source = "file";
    world.accounts = [...ENTRIES, { label: "Claude second", harness: "claude", home: "~/.claude-second" }];
    next();
    await screen.findByText(/changed while setup was open/);
    expect(calls("khipu_config_set")).toHaveLength(0);
    expect(onNext).not.toHaveBeenCalled();
    expect(screen.getByText("Next saves the account list as: Claude Code, Codex, Grok Build and Claude second.")).toBeInTheDocument();
  });
});

describe("Setup › Accounts step, saves carry the version they read", () => {
  it("the account list is saved with the version Next read", async () => {
    fake();
    const { onNext } = mount();
    await ready();
    fireEvent.click(screen.getAllByLabelText("Read this account's plan")[1]);
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    const set = calls("khipu_config_set");
    expect(set).toHaveLength(1);
    expect(set[0][1].expect).toBe("a1");
  });

  it("going back to the default list is saved with the version read", async () => {
    fake({ failOnce: ["khipu_route_priority"] });
    const { onNext } = mount();
    await ready();
    const box = () => screen.getAllByLabelText("Read this account's plan")[1];
    fireEvent.click(box());
    fireEvent.click(screen.getByRole("button", { name: "Move Grok Build up" }));
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await screen.findByText(/couldn't save the order/);
    fireEvent.click(box());
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    const unset = calls("khipu_config_unset");
    expect(unset).toHaveLength(1);
    expect(unset[0][1].expect).toBe("a2");
  });

  it("the order is saved with the priority version read, also when no priority is set", async () => {
    fake();
    const { onNext } = mount();
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Move Codex up" }));
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(calls("khipu_route_priority")[0][1].expect).toBe("p0");
  });

  it("the order is saved with the version of the priority already set", async () => {
    const w = fake({ priority: { entries: [{ account: "grok|", label: "Grok Build" }], until: null } });
    w.priorityVersion = 7;
    const { onNext } = mount();
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Move Codex up" }));
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(calls("khipu_route_priority")[0][1].expect).toBe("p7");
  });

  it("a list changed elsewhere after Next read it is refused in plain words, and Reload then Next saves it", async () => {
    const w = fake({ raceOnce: ["khipu_config_set"] });
    const { onNext } = mount();
    await ready();
    fireEvent.click(screen.getAllByLabelText("Read this account's plan")[1]);
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    const alert = await screen.findByText(/the account list was changed somewhere else after setup read it/);
    expect(alert.textContent).toContain("Reload to see what is saved now, then press Next again.");
    expect(alert.textContent).not.toContain("read it again;");
    expect(onNext).not.toHaveBeenCalled();
    expect(w.accounts.map((e) => e.harness)).toEqual(["claude", "codex", "grok", "muse"]);
    const shows = calls("khipu_config_show").length;
    fireEvent.click(screen.getByRole("button", { name: "Reload" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Reload" })).toBeNull());
    expect(calls("khipu_config_show").length).toBeGreaterThan(shows);
    expect(screen.queryByText(/changed somewhere else/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(w.accounts.map((e) => e.harness)).toEqual(["claude", "grok", "muse"]);
  });

  it("an order changed elsewhere after Next read it is refused, says what was saved, and Reload clears it", async () => {
    const w = fake({ raceOnce: ["khipu_route_priority"] });
    const { onNext } = mount();
    await ready();
    fireEvent.click(screen.getAllByLabelText("Read this account's plan")[3]);
    fireEvent.click(screen.getByRole("button", { name: "Move Codex up" }));
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    const alert = await screen.findByText(/your order was changed somewhere else after setup read it/);
    expect(alert.textContent).toMatch(/^Khipu saved which accounts to read, but /);
    expect(w.priority).toBeNull();
    expect(onNext).not.toHaveBeenCalled();
    const routes = calls("khipu_route_view").length;
    fireEvent.click(screen.getByRole("button", { name: "Reload" }));
    await waitFor(() => expect(screen.queryByRole("alert", { name: /changed somewhere else/ })).toBeNull());
    await waitFor(() => expect(screen.queryByRole("button", { name: "Reload" })).toBeNull());
    expect(calls("khipu_route_view").length).toBeGreaterThan(routes);
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(w.priority?.entries.map((e) => e.account)).toEqual(["codex|", "claude|", "grok|"]);
  });

  it("any other refusal offers no Reload", async () => {
    fake({ failOnce: ["khipu_config_set"] });
    mount();
    await ready();
    fireEvent.click(screen.getAllByLabelText("Read this account's plan")[1]);
    fireEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await screen.findByText(/couldn't save which accounts to read/);
    expect(screen.queryByRole("button", { name: "Reload" })).toBeNull();
  });
});

describe("Setup › the rail", () => {
  it("the rail shows seven steps with Accounts sixth, and Welcome lists the Accounts line", async () => {
    invokeMock.mockImplementation(async () => "{}");
    render(
      <Welcome
        dsnOk={null}
        refreshDsn={async () => {}}
        runKhipu={async () => "{}"}
        onFinish={() => {}}
        openIntegrations={() => {}}
      />,
    );
    const rail = screen.getByRole("list", { name: "Setup progress" });
    const items = within(rail).getAllByRole("listitem");
    expect(items).toHaveLength(7);
    expect(items[5]).toHaveTextContent("Accounts");
    expect(screen.getByText(/Setup is seven steps/)).toBeInTheDocument();
    expect(screen.getByText(/see how much of each plan is left/)).toBeInTheDocument();
  });
});

describe("Setup › Accounts step, entries it did not change go back as stored (review round 4)", () => {
  const untouched = { harness: "claude", label: "Untouched", home: ".claude-legacy" };
  const status = {
    source: "file" as const,
    version: "av1",
    user_home: "/Users/me",
    value: [{ label: " Codex ", harness: "codex" }, untouched, 7],
    accounts: [
      { label: "Codex", harness: "codex", home: null },
      { label: "Untouched", harness: "claude", home: ".claude-legacy", invalid: "relative folder" },
      { label: "?", harness: "?", home: null, invalid: "this entry is not an account" },
    ],
  };

  it("adds an account without dropping an invalid one or rewriting a valid one", () => {
    const rows = [{ harness: "claude", home: "~/.claude-client", account: "Client" }] as never[];
    const plan = planAccountList(status, new Map([[accountId("claude", "~/.claude-client", "/Users/me"), true]]), rows);
    expect(plan.changed).toBe(true);
    expect(plan.list).toEqual([
      { label: " Codex ", harness: "codex" },
      untouched,
      7,
      { label: "Client", harness: "claude", home: "~/.claude-client" },
    ]);
    expect(plan.labels).toEqual(["Codex", "Untouched", "?", "Client"]);
  });

  it("removes only the account unchecked, never matching an invalid entry", () => {
    const plan = planAccountList(
      status,
      new Map([
        [accountId("codex", null, "/Users/me"), false],
        [accountId("claude", ".claude-legacy", "/Users/me"), false],
      ]),
      [],
    );
    expect(plan.list).toEqual([untouched, 7]);
    expect([...plan.ids]).toEqual([]);
  });
});
