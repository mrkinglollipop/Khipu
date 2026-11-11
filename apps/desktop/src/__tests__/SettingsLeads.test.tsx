// Grok 2026-10-26 settings-audit leads. Each test FAILS if the lead is real.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { IntegrationsPanel } from "../IntegrationsPanel";
import type { RecallProbeStatus, StatusRow } from "../IntegrationsPanel";
import { FinderBottom, FinderTop, useAccountsFinder } from "../FoundAccounts";

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(async () => "{}"),
}));
vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: vi.fn(async () => "/tmp/not-a-real-cli"),
  save: vi.fn(async () => null),
}));

afterEach(() => cleanup());

describe("Harnesses card", () => {
  it("does not keep saying Verified after a later Verify fails", async () => {
    let installed = false;
    const rows = (): StatusRow[] => [{
      harness: "cursor", detected: true, mcp: installed, hook_stop: installed, hook_precompact: installed,
      recall_rule: installed ? "installed" : "missing",
    } as unknown as StatusRow];
    let verifyOk = true;
    const runKhipu = vi.fn(async (args: string[]) => {
      if (args[1] === "status") return JSON.stringify(rows());
      if (args[1] === "install") {
        installed = true;
        return JSON.stringify([{ harness: "cursor", detected: true, changes: [] }]);
      }
      if (args[1] === "verify") {
        return JSON.stringify([{ harness: "cursor", detected: true, ok: verifyOk,
          components: verifyOk ? { mcp: { ok: true }, hook: { ok: true }, recall_probe: { ok: true } }
            : { mcp: { ok: true }, hook: { ok: false, error: "Stop hook not found" } } }]);
      }
      return "{}";
    });
    const live = { ok: true, harnesses: { cursor: { ok: true, seen: true, captures: 4, last_captured_age_s: 60 } } };
    const props = { runKhipu, onToast: () => {}, active: true, liveness: live, refreshHealth: () => {}, onAnotherMac: () => {} };
    const { rerender } = render(<IntegrationsPanel {...props} recallProbe={null} />);
    const card = () => Array.from(document.querySelectorAll<HTMLElement>(".hcard")).find((c) => c.textContent?.includes("Cursor"))!;
    await waitFor(() => expect(document.querySelector(".hcard")).not.toBeNull());
    fireEvent.click(await screen.findByRole("button", { name: "Install" }));
    await waitFor(() => expect(installed).toBe(true));
    const probe = { ok: true, harnesses: { cursor: { ok: true, ts: new Date(Date.now() + 60_000).toISOString() } } } as unknown as RecallProbeStatus;
    rerender(<IntegrationsPanel {...props} recallProbe={probe} />);
    await waitFor(() => expect(within(card()).queryAllByText(/^Verified$/).length,
      `precondition: tag says Verified; card: ${card()?.textContent}`).toBeGreaterThan(0));
    verifyOk = false;
    fireEvent.click(within(card()).getByRole("button", { name: "Verify" }));
    await waitFor(() => expect(runKhipu.mock.calls.filter((c) => c[0][1] === "verify").length).toBeGreaterThan(1));
    await new Promise((r) => setTimeout(r, 300));
    expect(within(card()).queryAllByText(/^Verified$/), `tag still green after a failing Verify; card: ${card().textContent}`).toHaveLength(0);
  });
});



describe("program pick refusal wording", () => {
  it("shows the real refusal when it is not a path problem", async () => {
    const { invoke } = await import("@tauri-apps/api/core");
    vi.mocked(invoke).mockImplementation((async (cmd: string) => {
      if (cmd === "khipu_accounts_discover") {
        return JSON.stringify({
          candidates: [],
          clis: { codex: { state: "cant_run", reason: "start_failed", path: "/opt/codex" } },
          last_run: null,
          looking: false,
        });
      }
      if (cmd === "khipu_accounts_status") {
        return JSON.stringify({ version: "v1", accounts: [], declined: [], auto_track: false });
      }
      if (cmd === "khipu_config_show") {
        return JSON.stringify({
          usage_accounts: { source: "default", accounts: [], user_home: "/home/someone" },
          cli_settings: { "cli_path.codex": { value: null, usable: false } },
        });
      }
      if (cmd === "khipu_config_set") {
        return JSON.stringify({ ok: false, error: "hub busy; try again in a minute", key: "cli_path.codex" });
      }
      return JSON.stringify({ ok: true });
    }) as never);

    function Host() {
      const finder = useAccountsFinder(true);
      return (
        <>
          <FinderTop finder={finder} />
          <FinderBottom finder={finder} />
        </>
      );
    }
    render(<Host />);
    const change = await screen.findByRole("button", { name: /Change the program for Codex/ });
    fireEvent.click(change);
    await waitFor(() => {
      expect(screen.getByText(/hub busy/i), "path-only wording hid the real refusal").toBeTruthy();
    });
    expect(screen.queryByText(/isn't a program Khipu can run/)).toBeNull();
  });
});
