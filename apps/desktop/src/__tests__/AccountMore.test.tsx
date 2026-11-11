// Accounts › an account's "More for this account" (slice A11): what the older
// Accounts and routing screen offered on an account, with its old commands.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const calls: Array<{ cmd: string; args?: Record<string, unknown> }> = [];
vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(async (cmd: string, args?: Record<string, unknown>) => {
    calls.push({ cmd, args });
    return JSON.stringify({ ok: true });
  }),
}));

import { AccountMore } from "../accounts/AccountMore";
import type { RouteAccount, RouteView } from "../routing";

const NOW = new Date("2026-11-10T12:00:00Z");
const acct = (over: Partial<RouteAccount> = {}) => ({ key: "claude", label: "Claude", harness: "claude_code", state: "open", ...over }) as RouteAccount;
const view = (holds: unknown[] = []) => ({ accounts: [], holds }) as unknown as RouteView;

beforeEach(() => { calls.length = 0; });
afterEach(cleanup);

describe("More for this account", () => {
  it("ends what keeps work off the account only after the same confirmation, with the old command", async () => {
    const reread = vi.fn(async () => {});
    render(<AccountMore a={acct()} view={view([{ id: "h1", scope: { account: "claude" }, scope_label: "Claude", reason: "owner", text: "No big jobs", owner: true }])}
      now={NOW} reread={reread} />);
    fireEvent.click(screen.getByRole("button", { name: "End it on Claude: No big jobs" }));
    expect(calls).toEqual([]);
    expect(screen.getByText("End this now? Conversations stop seeing it on their next prompt.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^End it$/ }));
    await waitFor(() => expect(calls).toEqual([{ cmd: "khipu_route_hold_end", args: { id: "h1" } }]));
    expect(reread).toHaveBeenCalled();
  });

  it("Keep it ends nothing", () => {
    render(<AccountMore a={acct()} view={view([{ id: "h1", scope: { account: "claude" }, scope_label: "Claude", reason: "owner", text: "No big jobs" }])}
      now={NOW} reread={async () => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "End it on Claude: No big jobs" }));
    fireEvent.click(screen.getByRole("button", { name: "Keep it" }));
    expect(calls).toEqual([]);
  });

  it("clears a limit report with the old command, and offers it only when the account is limited or down", async () => {
    const { rerender } = render(<AccountMore a={acct()} view={view()} now={NOW} reread={async () => {}} />);
    expect(screen.queryByRole("button", { name: /clear this report/ })).toBeNull();
    rerender(<AccountMore a={acct({ state: "limited" })} view={view()} now={NOW} reread={async () => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "It isn't limited: clear this report" }));
    await waitFor(() => expect(calls).toEqual([{ cmd: "khipu_route_clear", args: { account: "claude" } }]));
  });

  it("offers Rename and Stop tracking when the finder can manage the account", () => {
    const onRename = vi.fn();
    const onStop = vi.fn();
    render(<AccountMore a={acct()} view={view()} now={NOW} reread={async () => {}} onRename={onRename} onStop={onStop} />);
    fireEvent.click(screen.getByRole("button", { name: "Rename…" }));
    fireEvent.click(screen.getByRole("button", { name: "Stop tracking…" }));
    expect(onRename).toHaveBeenCalledTimes(1);
    expect(onStop).toHaveBeenCalledTimes(1);
    expect(calls).toEqual([]);
  });

  it("draws nothing when there is nothing to offer", () => {
    const { container } = render(<AccountMore a={acct()} view={view()} now={NOW} reread={async () => {}} />);
    expect(container).toBeEmptyDOMElement();
  });
});
