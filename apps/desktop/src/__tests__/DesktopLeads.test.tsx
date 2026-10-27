// Grok 2026-10-26 desktop-audit leads. Each test FAILS if the lead is real.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useRoutingDetail } from "../AccountDetail";
import { PlansCard, usePlanUsage } from "../PlansCard";
import type { UsageReport } from "../PlansCard";
import type { RouteView, RoutingState } from "../routing";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
});

function view(withAlpha: boolean): RouteView {
  return {
    record: true, version: 1, checked_at: "2026-10-14T15:40:00Z",
    accounts: withAlpha
      ? [{ key: "claude|", label: "Alpha", harness: "claude", state: "open", checkable: true, models: [], witness: null, disagreement: null, last_seen: null }]
      : [],
    holds: [], note: null, rules: [], watched_rules: [], unmatched: [],
    priority: { entries: [], pending: false, confirmed: true, replaced: { entries: [] } },
    sources: { runners: [], table: null }, old_switches: { env: false, file: false },
  } as unknown as RouteView;
}

function Host({ v }: { v: RouteView }) {
  const state = { data: v, load: vi.fn() } as unknown as RoutingState;
  const d = useRoutingDetail({ state });
  return (
    <>
      <button type="button" onClick={() => d.open("claude|")}>open-alpha</button>
      {d.node}
    </>
  );
}

describe("routing detail host", () => {
  it("stays closed when its account leaves and later returns (Plans does)", async () => {
    const { rerender } = render(<Host v={view(true)} />);
    fireEvent.click(screen.getByText("open-alpha"));
    await waitFor(() => expect(document.querySelector("dialog")).not.toBeNull());
    rerender(<Host v={view(false)} />);
    await waitFor(() => expect(document.querySelector("dialog")).toBeNull());
    rerender(<Host v={view(true)} />);
    await new Promise((r) => setTimeout(r, 50));
    expect(document.querySelector("dialog"), "sheet came back uninvited").toBeNull();
  });
});

describe("kept plan row wording", () => {
  it("a kept row names the same quantity its accessible name does", async () => {
    const report = {
      generated_at: "2026-10-14T13:57:29Z", max_age_s: 120, refreshing: false, summary_text: [],
      accounts: [{
        key: "codex|", harness: "codex", account: "Codex", home: null, plan: "pro",
        windows: [{ name: "weekly", used_pct: 40, resets_at: "2999-01-01T00:00:00Z", window_mins: 10080, slot: "primary" }],
        credits: null, version: "1", source: "live", observed_at: null, error: "timeout", note: null, ok: false,
        probed_at: "2026-10-14T13:57:29Z", ms: 0, last_attempt_at: "2026-10-14T13:59:30Z", last_error: "timeout",
        last_good_at: "2026-10-12T14:00:00Z", cached: false, stale: true,
      }],
    } as unknown as UsageReport;
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_usage_status") return JSON.stringify(report);
      if (cmd === "khipu_config_show") return JSON.stringify({});
      throw new Error(`unexpected ${cmd}`);
    });
    function Home() {
      const state = usePlanUsage(true);
      return <PlansCard state={state} />;
    }
    render(<Home />);
    const btn = await screen.findByRole("button", { name: /Open usage details/ });
    const row = btn.closest(".plan-row") as HTMLElement;
    const aria = btn.getAttribute("aria-label") ?? "";
    const pct = row.querySelector(".plan-pct")?.textContent ?? "";
    expect(aria).toMatch(/left/);
    expect(pct, `aria says "${aria}" but the row prints "${pct}"`).toMatch(/left/);
  });
});
