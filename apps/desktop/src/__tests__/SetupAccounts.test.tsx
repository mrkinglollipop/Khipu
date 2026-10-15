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

type Opts = { rows?: unknown[]; accounts?: unknown[]; featuresFail?: boolean; hang?: boolean };

function fake(o: Opts = {}) {
  invokeMock.mockImplementation(async (cmd: string) => {
    if (o.hang) return new Promise(() => {});
    switch (cmd) {
      case "khipu_usage_status":
      case "khipu_usage_refresh":
        return JSON.stringify(report(o.rows));
      case "khipu_config_show":
        return JSON.stringify({ usage_accounts: { source: "default", accounts: o.accounts ?? ENTRIES, user_home: "/Users/test" } });
      case "khipu_features_show":
        if (o.featuresFail) throw new Error("no");
        return JSON.stringify({ features: { routing_push: { enabled: true, source: "default" } } });
      case "khipu_route_view":
        return JSON.stringify({ accounts: [], priority: null });
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
