// Settings -> Owner actions. The webview names a fixed Tauri command and the
// CLI (faked here) owns the state: every write is followed by a re-read, and
// the screen shows what the re-read says.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { OwnerActions } from "../OwnerActions";
import type { ChainState, OwnerAction, OwnerDirectionSettings } from "../OwnerActions";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

type Backend = { settings: OwnerDirectionSettings; actions: OwnerAction[]; refuse?: string; chain?: ChainState };
let backend: Backend;
const calls: Array<[string, Record<string, unknown> | undefined]> = [];

function fresh(): Backend {
  return {
    settings: {
      enabled: true,
      latest_window_h: 6,
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
        id: "c3", at: "2026-10-16T14:03:00Z", action: "route.rule.confirm", target: "2", auth: "direction",
        harness: "claude_code", binding: "hook", state: "applied",
        no_undo: "Confirming a seeded rule can't be undone; the rule can be retired.",
      },
      // A line written under the earlier design keeps the words it was asked with.
      {
        id: "a1", at: "2026-10-16T14:02:00Z", action: "route.pause", target: "codex|", auth: "direction",
        words: "pause Codex until tonight", harness: "claude_code", binding: "token", state: "applied",
      },
      { id: "z9", at: "2026-10-16T13:00:00Z", action: "route.note.set", target: "note", auth: "app", state: "applied" },
      { id: "t7", at: "2026-10-16T12:00:00Z", action: "route.lift", target: "grok|", auth: "terminal", state: "applied" },
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
        return JSON.stringify({
          ok: true,
          actions: backend.actions,
          chain: backend.chain ?? { ok: true, lines: backend.actions.length, first_bad: null },
        });
      case "khipu_owner_direction_undo": {
        if (backend.refuse) return JSON.stringify({ ok: false, error: backend.refuse });
        const row = backend.actions.find((a) => a.id === args?.id)!;
        row.state = row.state === "scheduled" ? "cancelled" : "undone";
        return JSON.stringify({ ok: true, undid: row.id });
      }
      case "khipu_owner_direction_acknowledge":
        backend.chain = { ok: true, lines: backend.actions.length, first_bad: null };
        return JSON.stringify({ ok: true });
      case "khipu_owner_direction_switch":
        backend.settings.enabled = Boolean(args?.on);
        return JSON.stringify({ ok: true });
      case "khipu_owner_direction_set": {
        const { key, value } = args as { key: string; value: string };
        const s = backend.settings;
        if (key === "session_binding") s.session_binding = value === "on";
        else if (key === "latest_window_h") s.latest_window_h = Number(value);
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
  return screen.findByRole("list", { name: "Every owner action, newest first" });
}

describe("Owner actions", () => {
  it("lists every owner action, newest first, with the conversation and the state", async () => {
    render(<OwnerActions active />);
    const items = within(await list()).getAllByRole("listitem");
    expect(items).toHaveLength(5);
    expect(items[0].textContent).toContain("Removed T-12 from the ledger for good");
    expect(items[0].textContent).toContain("Purge scheduled for");
    expect(items[0].textContent).toContain("You asked for this in an Aegis conversation; checked by conversation only.");
    expect(items[0].textContent).toContain("checked by conversation only");
    expect(items[1].textContent).toContain("No undo");
    expect(items[1].textContent).toContain("can't be undone");
    expect(within(items[1]).queryByRole("button")).toBeNull();
    expect(items[2].textContent).toContain("Paused Codex");
    expect(items[2].textContent).toContain("“pause Codex until tonight”");
    expect(items[2].textContent).toContain("You asked for this in a Claude Code conversation.");
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

  it("shows the app's and a terminal's changes with how they were made, each with Undo", async () => {
    render(<OwnerActions active />);
    const items = within(await list()).getAllByRole("listitem");
    expect(items[3].textContent).toContain("Set the note");
    expect(items[3].textContent).toContain("Done in the Khipu app.");
    expect(items[4].textContent).toContain("Lifted the pause on Grok");
    expect(items[4].textContent).toContain("Done on a terminal, with a typed yes.");
    fireEvent.click(within(items[4]).getByRole("button", { name: "Undo: Lifted the pause on Grok" }));
    await waitFor(() => expect(within(items[4]).getByText("Undone")).toBeTruthy());
    expect(calls).toContainEqual(["khipu_owner_direction_undo", { id: "t7" }]);
    expect(within(items[3]).getByRole("button", { name: "Undo: Set the note" })).toBeTruthy();
  });

  it("an unconfirmed change says so and keeps its Undo", async () => {
    backend.actions[2].state = "unconfirmed";
    render(<OwnerActions active />);
    const items = within(await list()).getAllByRole("listitem");
    expect(items[2].textContent).toContain("Unconfirmed");
    fireEvent.click(within(items[2]).getByRole("button", { name: "Undo: Paused Codex" }));
    await waitFor(() => expect(within(items[2]).getByText("Undone")).toBeTruthy());
  });

  it("a change that did not happen has no Undo", async () => {
    backend.actions[2].state = "failed";
    render(<OwnerActions active />);
    const items = within(await list()).getAllByRole("listitem");
    expect(items[2].textContent).toContain("Did not happen");
    expect(within(items[2]).queryByRole("button")).toBeNull();
  });

  it("a clear and a dismissed report each have an Undo", async () => {
    backend.actions.push(
      { id: "k1", at: "2026-10-16T11:00:00Z", action: "route.clear", target: "codex|", auth: "app", state: "applied" },
      {
        id: "k2", at: "2026-10-16T10:00:00Z", action: "route.clear", target: "codex|", auth: "app", state: "applied",
        facts: { report: "e12" },
      },
    );
    render(<OwnerActions active />);
    const items = within(await list()).getAllByRole("listitem");
    expect(within(items[5]).getByRole("button", { name: "Undo: Cleared the report on Codex" })).toBeTruthy();
    expect(within(items[6]).getByRole("button", { name: "Undo: Dismissed a report on Codex" })).toBeTruthy();
  });

  it("a close of several Owed items by direction says how many, with an Undo", async () => {
    backend.actions.push({
      id: "o1", at: "2026-10-16T09:00:00Z", action: "owed.change", target: "11,12,13", auth: "direction",
      harness: "claude_code", binding: "hook", state: "applied", facts: { change: "close" },
    });
    render(<OwnerActions active />);
    const items = within(await list()).getAllByRole("listitem");
    fireEvent.click(within(items[5]).getByRole("button", { name: "Undo: Closed 3 Owed items" }));
    await waitFor(() => expect(within(items[5]).getByText("Undone")).toBeTruthy());
    expect(calls).toContainEqual(["khipu_owner_direction_undo", { id: "o1" }]);
  });

  it("no banner while the log's chain holds", async () => {
    render(<OwnerActions active />);
    await list();
    expect(screen.queryByText(/changed outside Khipu/)).toBeNull();
  });

  it("a broken chain shows a banner naming the first bad entry", async () => {
    backend.chain = {
      ok: false,
      lines: 5,
      first_bad: { line: 2, id: "c3", kind: "action", why: "it was changed after Khipu wrote it" },
    };
    render(<OwnerActions active />);
    await list();
    const alert = await screen.findByText(/changed outside Khipu/);
    expect(alert.getAttribute("role")).toBe("alert");
    expect(alert.textContent).toContain("at line 2 (c3): it was changed after Khipu wrote it.");
  });

  it("a kept tamper alarm has an Acknowledge button that clears it", async () => {
    backend.chain = {
      ok: false,
      lines: 5,
      first_bad: { line: 3, id: null, kind: null, why: "1 line Khipu wrote is missing from the end of the log" },
      alarm: { at: "2026-10-16T14:00:00Z", first_bad: { line: 3 } },
    };
    render(<OwnerActions active />);
    await list();
    fireEvent.click(await screen.findByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(screen.queryByText(/changed outside Khipu/)).toBeNull());
    expect(calls).toContainEqual(["khipu_owner_direction_acknowledge", {}]);
    expect(screen.queryByRole("button", { name: "Acknowledge" })).toBeNull();
  });

  it("a broken chain with no kept alarm has no Acknowledge button", async () => {
    backend.chain = {
      ok: false,
      lines: 5,
      first_bad: { line: 2, id: "c3", kind: "action", why: "it was changed after Khipu wrote it" },
    };
    render(<OwnerActions active />);
    await screen.findByText(/changed outside Khipu/);
    expect(screen.queryByRole("button", { name: "Acknowledge" })).toBeNull();
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

    expect(screen.queryByLabelText(/earlier message/)).toBeNull();
    const latest = screen.getByLabelText(/After your last message/) as HTMLInputElement;
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
