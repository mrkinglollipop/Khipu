// Grok 2026-10-26 second-pass desktop leads. Each test FAILS if the lead is real.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { OptionalFeatures } from "../OptionalFeatures";
import { UsageAccountsField } from "../PlansCard";
import { DeliverySection, RoutingSettings } from "../RoutingSettings";
import { Welcome } from "../Welcome";
import { DELIVERY_HEALTH, DELIVERY_NOW, DELIVERY_VIEW } from "../../dev/deliveryFake";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke: (...a: unknown[]) => invokeMock(...a) }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn(async () => null), save: vi.fn(async () => null) }));
vi.mock("@tauri-apps/api/path", () => ({ resourceDir: vi.fn(async () => "/x") }));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: vi.fn(async () => "0.4.0") }));

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
});

describe("Optional features: a write that succeeds and a re-read that fails", () => {
  it("does not leave the old value on screen with no error", async () => {
    let enabled = true;
    let showFails = false;
    invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
      switch (cmd) {
        case "khipu_features_show":
          if (showFails) throw new Error("show failed");
          return JSON.stringify({ features: { rerank: { enabled, source: "file" } }, unknown: [] });
        case "khipu_feature_set":
          enabled = Boolean(args?.enabled);
          showFails = true;
          return JSON.stringify({ features: {}, unknown: [] });
        case "khipu_config_show":
          return JSON.stringify({ relevance_cosine_floor: { value: 0.65, source: "default", default: 0.65 }, config: {} });
        case "khipu_jobs_status":
          return JSON.stringify({ recall_daemon: { plist_loaded: false } });
        case "khipu_recall_status":
          return JSON.stringify({ ok: false, running: false });
        default:
          return "{}";
      }
    });
    render(<OptionalFeatures active />);
    const sw = await screen.findByRole("switch", { name: /Reorder top results/ });
    await waitFor(() => expect(sw).toBeChecked());
    fireEvent.click(sw);
    await waitFor(() => expect(invokeMock.mock.calls.some((c) => c[0] === "khipu_feature_set")).toBe(true));
    await new Promise((r) => setTimeout(r, 300));
    const stillOn = (screen.getByRole("switch", { name: /Reorder top results/ }) as HTMLInputElement).checked;
    const saysSo = screen.queryAllByRole("alert").length > 0 || /couldn.t (re-?read|read|refresh)|could not (re-?read|read)/i.test(document.body.textContent ?? "");
    expect(stillOn && !saysSo, "switch still shows ON after a successful OFF write and nothing says the re-read failed").toBe(false);
    expect(screen.getByRole("alert").textContent).toMatch(/Couldn't refresh; showing the last value/);
  });
});

describe("Telling conversations", () => {
  it("does not say nobody is live when the CLI's delivery_rows is null (damaged) but doctor lists a red identity", () => {
    const view = { ...DELIVERY_VIEW, delivery_rows: null } as typeof DELIVERY_VIEW;
    render(<DeliverySection data={view} health={DELIVERY_HEALTH} now={DELIVERY_NOW} />);
    const redIdentity = (DELIVERY_HEALTH.identities ?? []).some((i) => i.status === "red" || i.status === "unknown");
    expect(redIdentity, "fixture precondition: doctor lists a non-green identity").toBe(true);
    expect(screen.queryByText(/No live conversations/)).toBeNull();
    expect(screen.getByText("Khipu could not read live delivery rows.")).toBeInTheDocument();
  });

  it("says Can't be checked when delivery_rows is null even if the doctor report is green with no identities", () => {
    const view = { ...DELIVERY_VIEW, delivery_rows: null } as typeof DELIVERY_VIEW;
    const greenEmpty = {
      status: "green" as const,
      reasons: [],
      missing: [],
      identities: [],
      prompts: { status: "green" as const, count: 0, skipped: 0, rate_pct: 0 },
      replication: { status: "not_applicable" as const },
      gateway: { status: "not_applicable" as const },
    };
    render(<DeliverySection data={view} health={greenEmpty} now={DELIVERY_NOW} />);
    expect(screen.queryByText("No live conversations")).toBeNull();
    expect(screen.getByText("Khipu could not read live delivery rows.")).toBeInTheDocument();
  });

  it("does not fall through to doctor identities when the CLI returned an empty delivery_rows list", () => {
    const view = { ...DELIVERY_VIEW, delivery_rows: [] } as typeof DELIVERY_VIEW;
    render(<DeliverySection data={view} health={DELIVERY_HEALTH} now={DELIVERY_NOW} />);
    expect(screen.queryByRole("list", { name: "Live conversations" })).toBeNull();
    expect(screen.getByText("No live conversations")).toBeInTheDocument();
  });

  it("says Can't be checked, not 'No live conversations', when a non-empty delivery_rows list is malformed", () => {
    const view = { ...DELIVERY_VIEW, delivery_rows: [{ harness: "claude_code" }] } as unknown as typeof DELIVERY_VIEW;
    render(<DeliverySection data={view} health={DELIVERY_HEALTH} now={DELIVERY_NOW} />);
    expect(screen.queryByText("No live conversations")).toBeNull();
    expect(screen.getByText("Khipu could not read live delivery rows.")).toBeInTheDocument();
  });

  it("does not paint a green owed-0 row as 'No changes owed' while the doctor read failed", () => {
    const view = {
      ...DELIVERY_VIEW,
      delivery_rows: [{ label: "Fix the flaky build", harness: "claude_code", component: "main", mac: "This Mac",
        last_prompt_at: "2026-10-21T18:38:00Z", status: "green", owed: 0 }],
    } as typeof DELIVERY_VIEW;
    render(<DeliverySection data={view} health={null} healthError="doctor timed out" now={DELIVERY_NOW} />);
    expect(screen.queryByText("No changes owed")).toBeNull();
  });
});

describe("Setup guide: Connect your agents", () => {
  it("does not say no harness exists when the status read throws", async () => {
    const runKhipu = vi.fn(async (args: string[]) => {
      if (args[0] === "integrations" && args[1] === "status") throw new Error("integrations status failed");
      return "{}";
    });
    render(<Welcome dsnOk refreshDsn={async () => {}} runKhipu={runKhipu} onFinish={() => {}}
      openIntegrations={() => {}} initialStep="agents" initialStepKey={1} />);
    await waitFor(() => expect(runKhipu.mock.calls.some((c) => c[0][1] === "status")).toBe(true));
    await new Promise((r) => setTimeout(r, 200));
    expect(screen.queryByText(/No supported harness was found on this Mac yet/)).toBeNull();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});

describe("Re-read banners after a failed refresh with last-good data", () => {
  it("Routing settings uses neutral refresh wording, not a false Saved claim", async () => {
    let failNextShow = false;
    const open = [
      "accounts", "pace", "priority", "rules", "holds", "checks", "telling", "resets", "runners", "table", "changes",
      "sub:hears", "sub:sizes", "sub:folders",
    ];
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_route_view") {
        return JSON.stringify({
          record: true, priority: null, accounts: [], holds: [], rules: [], watched_rules: [],
          unmatched: [], sources: { runners: [], table: null }, old_switches: { env: false, file: false },
          changes: [], rule_checks: [], warn_after: { disagreement: 1800, not_refreshed: 300 },
          report_default_limit: 1800,
        });
      }
      if (cmd === "khipu_features_show") {
        return JSON.stringify({ features: { routing_push: { enabled: true, source: "file" } } });
      }
      if (cmd === "khipu_config_show") {
        if (failNextShow) throw new Error("config re-read failed");
        const setting = (value: unknown) => ({ value, default: value, kind: "x", source: "default" });
        return JSON.stringify({
          routing: {
            routing_stale_after: setting(43200),
            routing_push_start: setting(true),
            routing_push_prompt: setting(true),
            routing_push_delegation: setting(true),
            routing_reset_check: setting(true),
          },
        });
      }
      if (cmd === "khipu_config_set") {
        failNextShow = true;
        return JSON.stringify({ ok: true });
      }
      if (cmd === "khipu_feature_set") {
        failNextShow = true;
        return JSON.stringify({ ok: true });
      }
      return "{}";
    });
    const store = new Map<string, string>([["khipu.routing.open", JSON.stringify(open)]]);
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => { store.set(k, String(v)); },
      removeItem: (k: string) => { store.delete(k); },
      clear: () => { store.clear(); },
    });
    render(<RoutingSettings active />);
    const sw = await screen.findByRole("switch", { name: "Before each prompt" });
    fireEvent.click(sw);
    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(/Couldn't refresh; showing the last value/));
    expect(screen.queryByText(/Saved, but/)).toBeNull();
    vi.unstubAllGlobals();
  });

  it("Plans account list uses neutral refresh wording, not a false Saved claim", async () => {
    let failNextShow = false;
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_config_show") {
        if (failNextShow) throw new Error("config re-read failed");
        return JSON.stringify({
          usage_accounts: {
            source: "file", version: "av1", user_home: "/Users/test",
            accounts: [{ label: "Claude", harness: "claude", home: null }],
          },
        });
      }
      if (cmd === "khipu_config_set" || cmd === "khipu_config_unset") {
        failNextShow = true;
        return JSON.stringify({ ok: true });
      }
      return "{}";
    });
    render(<UsageAccountsField active />);
    await screen.findByText("Your list");
    fireEvent.click(screen.getByRole("button", { name: "Remove Claude" }));
    await waitFor(() => {
      const texts = screen.getAllByRole("alert").map((n) => n.textContent ?? "");
      expect(texts.some((t) => /Couldn't refresh; showing the last value/.test(t))).toBe(true);
      expect(texts.some((t) => /Could not re-read the account list/.test(t))).toBe(true);
    });
    expect(screen.queryByText(/Saved, but/)).toBeNull();
  });
});
