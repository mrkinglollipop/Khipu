// Setup's Accounts step (docs/plans/2026-10-15-settings-clarity.md, phase 7):
// nothing is written until Next, and only what the owner changed.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { SetupAccountsStep } from "../SetupAccounts";
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
};

function fake(o: Opts = {}) {
  const failing = new Set(o.failOnce ?? []);
  invokeMock.mockImplementation(async (cmd: string) => {
    if (o.hang) return new Promise(() => {});
    if (failing.delete(cmd)) throw new Error(`${cmd} failed`);
    switch (cmd) {
      case "khipu_usage_refresh":
        if (o.refreshFail) throw new Error("probe failed");
        return JSON.stringify(report(o.rows));
      case "khipu_usage_status":
        return JSON.stringify(report(o.rows));
      case "khipu_config_show":
        if (o.configFailAfterFirst && calls("khipu_config_show").length > 1) throw new Error("config failed");
        return JSON.stringify({ usage_accounts: { source: o.source ?? "default", accounts: o.accounts ?? ENTRIES, user_home: "/Users/test" } });
      case "khipu_features_show":
        if (o.featuresFail) throw new Error("no");
        return JSON.stringify({ features: { routing_push: { enabled: true, source: "default" } } });
      case "khipu_route_view":
        if (o.routeFail) throw new Error("route failed");
        return JSON.stringify({ accounts: [], priority: o.priority ?? null });
      default:
        return JSON.stringify({ ok: true });
    }
  });
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
  await screen.findByText("Signed in, Max, 62% of its week left");
  await screen.findAllByLabelText("Read this account's plan");
}

describe("Setup › Accounts step", () => {
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
    expect(screen.getByText("Not signed in. Sign in from Grok Build, then Check again.")).toBeInTheDocument();
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
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
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
    expect(screen.getByText("Signed in, Max, 62% of its week left")).toBeInTheDocument();
  });

  it("an account left out of your own list is not called not found", async () => {
    fake({
      source: "file",
      accounts: [ENTRIES[0]],
      rows: [report().accounts[0]],
    });
    mount();
    await screen.findByText("Signed in, Max, 62% of its week left");
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
