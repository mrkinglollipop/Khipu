// Grok 2026-10-26 second-pass desktop leads. Each test FAILS if the lead is real.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { Welcome } from "../Welcome";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke: (...a: unknown[]) => invokeMock(...a) }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn(async () => null), save: vi.fn(async () => null) }));
vi.mock("@tauri-apps/api/path", () => ({ resourceDir: vi.fn(async () => "/x") }));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: vi.fn(async () => "0.4.0") }));

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
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
