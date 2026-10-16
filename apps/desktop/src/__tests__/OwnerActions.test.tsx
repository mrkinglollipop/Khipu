// Settings -> Owner actions. The webview names a fixed Tauri command and the
// CLI (faked here) owns the state: every write is followed by a re-read, and
// the screen shows what the re-read says.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { OwnerActions } from "../OwnerActions";
import type { OwnerAction, OwnerDirectionSettings } from "../OwnerActions";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

type Backend = { settings: OwnerDirectionSettings; actions: OwnerAction[]; refuse?: string };
let backend: Backend;
const calls: Array<[string, Record<string, unknown> | undefined]> = [];

function fresh(): Backend {
  return {
    settings: {
      enabled: true,
      latest_window_h: 6,
      earlier_window_min: 30,
      trusted: ["claude_code:cli", "codex:codex_cli_rs"],
      session_binding: true,
    },
    actions: [
      {
        id: "b2", at: "2026-10-16T14:05:00Z", action: "ledger.purge", target: "atlas#T-12", auth: "direction",
        words: "purge T-12 from the ledger", harness: "aegis", binding: "session", state: "scheduled",
        runs_at: "2026-10-17T14:05:00Z",
      },
      {
        id: "c3", at: "2026-10-16T14:03:00Z", action: "route.clear", target: "codex|", auth: "direction",
        words: "clear Codex please", harness: "claude_code", binding: "token", state: "applied",
        no_undo: "A clear or a dismissed report can't be undone; a new report about the account replaces it.",
      },
      {
        id: "a1", at: "2026-10-16T14:02:00Z", action: "route.pause", target: "codex|", auth: "direction",
        words: "pause Codex until tonight", harness: "claude_code", binding: "token", state: "applied",
      },
      { id: "z9", at: "2026-10-16T13:00:00Z", action: "route.note.set", target: "note", auth: "app", state: "applied" },
    ],
  };
}

beforeEach(() => {
  backend = fresh();
  calls.length = 0;
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    calls.push([cmd, args]);
    switch (cmd) {
      case "khipu_owner_direction_show":
        return JSON.stringify({
          ok: true,
          settings: backend.settings,
          turns: { claude_code: { turns: 4, eligible: 3, entrypoints: { cli: 3, "sdk-cli": 1 } } },
        });
      case "khipu_owner_direction_actions":
        return JSON.stringify({ ok: true, actions: backend.actions });
      case "khipu_owner_direction_undo": {
        if (backend.refuse) return JSON.stringify({ ok: false, error: backend.refuse });
        const row = backend.actions.find((a) => a.id === args?.id)!;
        row.state = row.state === "scheduled" ? "cancelled" : "undone";
        return JSON.stringify({ ok: true, undid: row.id });
      }
      case "khipu_owner_direction_switch":
        backend.settings.enabled = Boolean(args?.on);
        return JSON.stringify({ ok: true });
      case "khipu_owner_direction_set": {
        const { key, value } = args as { key: string; value: string };
        const s = backend.settings;
        if (key === "session_binding") s.session_binding = value === "on";
        else if (key === "latest_window_h") s.latest_window_h = Number(value);
        else if (key === "earlier_window_min") s.earlier_window_min = Number(value);
        else if (key === "trust") s.trusted = [...s.trusted, value];
        else if (key === "untrust") s.trusted = s.trusted.filter((t) => t !== value);
        return JSON.stringify({ ok: true, settings: s });
      }
      default:
        throw new Error(`unexpected command ${cmd}`);
    }
  });
});

afterEach(() => cleanup());

async function list() {
  return screen.findByRole("list", { name: "Done by your direction, newest first" });
}

describe("Owner actions", () => {
  it("lists only what was done by direction, newest first, with the words, the conversation and the state", async () => {
    render(<OwnerActions active />);
    const items = within(await list()).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items[0].textContent).toContain("Removed T-12 from the ledger for good");
    expect(items[0].textContent).toContain("Purge scheduled for");
    expect(items[0].textContent).toContain("You said this in an Aegis conversation; checked by conversation only.");
    expect(items[0].textContent).toContain("checked by conversation only");
    expect(items[1].textContent).toContain("No undo");
    expect(items[1].textContent).toContain("can't be undone");
    expect(within(items[1]).queryByRole("button")).toBeNull();
    expect(items[2].textContent).toContain("Paused Codex");
    expect(items[2].textContent).toContain("“pause Codex until tonight”");
    expect(items[2].textContent).toContain("You said this in a Claude Code conversation.");
    expect(items[2].textContent).not.toContain("checked by conversation only");
  });

  it("Undo calls the undo command with the line's id, then shows the re-read state", async () => {
    render(<OwnerActions active />);
    const items = within(await list()).getAllByRole("listitem");
    fireEvent.click(within(items[2]).getByRole("button", { name: "Undo: Paused Codex" }));
    await waitFor(() => expect(within(items[2]).getByText("Undone")).toBeTruthy());
    expect(calls).toContainEqual(["khipu_owner_direction_undo", { id: "a1" }]);
    expect(within(items[2]).queryByRole("button")).toBeNull();
  });

  it("a scheduled purge's button cancels it", async () => {
    render(<OwnerActions active />);
    const items = within(await list()).getAllByRole("listitem");
    fireEvent.click(within(items[0]).getByRole("button", { name: /Undo: Removed T-12/ }));
    await waitFor(() => expect(within(items[0]).getByText("Purge cancelled")).toBeTruthy());
    expect(calls).toContainEqual(["khipu_owner_direction_undo", { id: "b2" }]);
  });

  it("a refused undo shows the CLI's words", async () => {
    backend.refuse = "the purge b2 already ran";
    render(<OwnerActions active />);
    const items = within(await list()).getAllByRole("listitem");
    fireEvent.click(within(items[0]).getByRole("button", { name: /Undo/ }));
    expect((await within(items[0]).findByRole("alert")).textContent).toContain("already ran");
  });

  it("the switches and windows call set at once", async () => {
    render(<OwnerActions active />);
    const enabled = (await screen.findByRole("switch", { name: "Owner direction" })) as HTMLInputElement;
    await waitFor(() => expect(enabled.checked).toBe(true));
    fireEvent.click(enabled);
    await waitFor(() => expect(enabled.checked).toBe(false));
    expect(calls).toContainEqual(["khipu_owner_direction_switch", { on: false }]);

    const session = screen.getByRole("switch", { name: /by conversation only/ }) as HTMLInputElement;
    await waitFor(() => expect(session.disabled).toBe(false));
    fireEvent.click(session);
    await waitFor(() => expect(session.checked).toBe(false));
    expect(calls).toContainEqual(["khipu_owner_direction_set", { key: "session_binding", value: "off" }]);

    const latest = screen.getByLabelText(/latest message counts for/) as HTMLInputElement;
    fireEvent.change(latest, { target: { value: "2" } });
    fireEvent.submit(latest.closest("form")!);
    await waitFor(() =>
      expect(calls).toContainEqual(["khipu_owner_direction_set", { key: "latest_window_h", value: "2" }]),
    );
  });

  it("removes a trusted front end and adds one seen recently", async () => {
    render(<OwnerActions active />);
    const trusted = await screen.findByRole("list", { name: "Front ends that count as you" });
    expect(within(trusted).getAllByRole("listitem")).toHaveLength(2);
    const seen = screen.getByRole("list", { name: "Front ends seen in the last day" });
    fireEvent.click(within(seen).getByRole("button", { name: "Count Claude Code jobs (claude -p) as you" }));
    await waitFor(() => expect(within(trusted).getAllByRole("listitem")).toHaveLength(3));
    expect(calls).toContainEqual(["khipu_owner_direction_set", { key: "trust", value: "claude_code:sdk-cli" }]);
    fireEvent.click(within(trusted).getByRole("button", { name: "Remove Codex, typed in a terminal" }));
    await waitFor(() => expect(within(trusted).getAllByRole("listitem")).toHaveLength(2));
    expect(calls).toContainEqual(["khipu_owner_direction_set", { key: "untrust", value: "codex:codex_cli_rs" }]);
  });
});
