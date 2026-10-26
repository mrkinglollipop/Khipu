// Grok 2026-10-26 second-pass desktop leads. Each test FAILS if the lead is real.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { OptionalFeatures } from "../OptionalFeatures";
import { DeliverySection } from "../RoutingSettings";
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
    const saysSo = screen.queryAllByRole("alert").length > 0 || /couldn.t (re-?read|read)|could not (re-?read|read)/i.test(document.body.textContent ?? "");
    expect(stillOn && !saysSo, "switch still shows ON after a successful OFF write and nothing says the re-read failed").toBe(false);
  });
});

describe("Telling conversations", () => {
  it("does not say nobody is live when the CLI's delivery_rows is an empty damaged-read list but doctor lists a red identity", () => {
    const view = { ...DELIVERY_VIEW, delivery_rows: [] } as typeof DELIVERY_VIEW;
    render(<DeliverySection data={view} health={DELIVERY_HEALTH} now={DELIVERY_NOW} />);
    const redIdentity = (DELIVERY_HEALTH.identities ?? []).some((i) => i.status === "red" || i.status === "unknown");
    expect(redIdentity, "fixture precondition: doctor lists a non-green identity").toBe(true);
    expect(screen.queryByText(/No live conversations/)).toBeNull();
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
  });
});
