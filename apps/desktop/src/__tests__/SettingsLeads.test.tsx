// Grok 2026-10-26 settings-audit leads. Each test FAILS if the lead is real.
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { IntegrationsPanel } from "../IntegrationsPanel";
import type { RecallProbeStatus, StatusRow } from "../IntegrationsPanel";
import { PlansSettingsCard, UsageAccountsField, usePlanUsage } from "../PlansCard";
import type { UsageReport } from "../PlansCard";
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

describe.each([{ rereadFails: false }, { rereadFails: true }])("Plans settings account list $rereadFails", ({ rereadFails }) => {
  it("shows the saved account or tells the owner the re-read failed", async () => {
    const report = {
      generated_at: "2026-10-14T13:57:29Z", max_age_s: 120, refreshing: false, summary_text: [],
      accounts: [{ key: "claude|", harness: "claude", account: "Claude", home: null, plan: "max", windows: [],
        credits: null, version: "1", source: "live", observed_at: null, error: null, note: null, ok: true,
        probed_at: "2026-10-14T13:57:29Z", ms: 0, last_attempt_at: "2026-10-14T13:57:29Z", last_error: null,
        last_good_at: "2026-10-14T13:57:29Z", cached: false, stale: false }],
    } as unknown as UsageReport;
    let saved = false;
    let shows = 0;
    const { invoke } = await import("@tauri-apps/api/core");
    vi.mocked(invoke).mockImplementation((async (cmd: string) => {
      if (cmd === "khipu_usage_status" || cmd === "khipu_usage_refresh") return JSON.stringify(report);
      if (cmd === "khipu_config_show") {
        shows += 1;
        if (saved && rereadFails) throw "khipu exited 1: hub busy";
        return JSON.stringify({ usage_accounts: { value: null, source: "default", version: "av1",
          accounts: [{ label: "Claude", harness: "claude", home: null }, ...(saved ? [{ label: "Claude work", harness: "claude", home: "~/.claude-work" }] : [])] } });
      }
      if (cmd === "khipu_config_set") {
        saved = true;
        return JSON.stringify({ ok: true });
      }
      throw new Error(`unexpected ${cmd}`);
    }) as never);
    function Settings() {
      const state = usePlanUsage("settings");
      return <PlansSettingsCard state={state} active />;
    }
    render(<Settings />);
    await waitFor(() => expect(screen.getByLabelText("Name")).toBeTruthy());
    await waitFor(() => expect(shows).toBeGreaterThan(0));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Claude work" } });
    fireEvent.change(screen.getByLabelText("Login folder"), { target: { value: "~/.claude-work" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(saved).toBe(true));
    await new Promise((r) => setTimeout(r, 400));
    const showsNewAccount = !!screen.queryByText("Claude work");
    const alerts = screen.queryAllByRole("alert");
    expect(showsNewAccount || alerts.length > 0, "saved account is not shown and no error is raised").toBe(true);
  });
});

describe("Plans settings save re-read", () => {
  it("does not claim the re-read failed when a newer reload supersedes it", async () => {
    let saved = false;
    const pending: Array<(raw: string) => void> = [];
    const onChanged = vi.fn();
    const body = (withWork: boolean) =>
      JSON.stringify({
        usage_accounts: {
          value: null,
          source: "default",
          version: "av1",
          accounts: [
            { label: "Claude", harness: "claude", home: null },
            ...(withWork ? [{ label: "Claude work", harness: "claude", home: "~/.claude-work" }] : []),
          ],
        },
      });
    const { invoke } = await import("@tauri-apps/api/core");
    vi.mocked(invoke).mockImplementation((async (cmd: string) => {
      if (cmd === "khipu_config_show") {
        if (!saved) return body(false);
        return new Promise<string>((resolve) => pending.push(resolve));
      }
      if (cmd === "khipu_config_set") {
        saved = true;
        return JSON.stringify({ ok: true });
      }
      throw new Error(`unexpected ${cmd}`);
    }) as never);

    const { rerender } = render(<UsageAccountsField active onChanged={onChanged} />);
    await waitFor(() => expect(screen.getByLabelText("Name")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Claude work" } });
    fireEvent.change(screen.getByLabelText("Login folder"), { target: { value: "~/.claude-work" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(saved).toBe(true));
    await waitFor(() => expect(pending).toHaveLength(1));
    // A newer reload (active flip) while the save's re-read is still in flight.
    rerender(<UsageAccountsField active={false} onChanged={onChanged} />);
    rerender(<UsageAccountsField active onChanged={onChanged} />);
    await waitFor(() => expect(pending).toHaveLength(2));
    await act(async () => pending[1](body(true)));
    await act(async () => pending[0](body(true)));
    await waitFor(() => expect(screen.getByText("Claude work")).toBeTruthy());
    expect(
      screen.queryByText(/Could not re-read the account list/),
      "superseded reload was treated as a failed re-read",
    ).toBeNull();
    expect(onChanged).toHaveBeenCalled();
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
