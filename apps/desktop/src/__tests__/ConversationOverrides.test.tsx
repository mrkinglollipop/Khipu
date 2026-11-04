// Conversations allowed to run ahead, and the tidy-up walk. Every value is made up.
(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { CleanUpWalk, OverridesSection, settleCall } from "../ConversationOverrides";
import type { WalkItem } from "../ConversationOverrides";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke: (...args: unknown[]) => invokeMock(...args) }));

const NOW = new Date("2026-11-04T15:00:00Z");
const calls: Array<[string, Record<string, unknown> | undefined]> = [];
let overrides: Array<Record<string, unknown>>;
let walk: Record<string, unknown>;

const item = (over: Partial<WalkItem> = {}): WalkItem => ({
  code: "duplicate", key: "dup:codex|:x2", number: 1, of: 2, yes: "End the second one and keep the first.",
  words: "Codex has \"re-check of PRs #3 and #11\" twice (ends Nov 5 14:00Z and Nov 5 15:00Z).",
  settle: { confirm: { action: "route.priority.amend", args: { account: "codex|", end_exception: "x2", expect: "p4" } } },
  ...over,
});

beforeEach(() => {
  calls.length = 0;
  overrides = [{ id: "ov-1", label: "Codex", who: "The T3 conversation \"Review\"", until: "2026-11-04T19:40:00Z", indefinite: false,
    review_due: false, cap: "Stops when the account reaches 60% of its week" },
  { id: "ov-2", label: "Grok", who: "Every conversation in the folder khipu-slices", until: null, indefinite: true,
    review_at: "2026-11-04T09:00:00Z", review_due: true, cap: "Stops at the account's saved share cap if it has one, otherwise at its plan limit" }];
  walk = { ok: true, total: 2, left_for_later: 0, item: item(), soon: [] };
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    calls.push([cmd, args]);
    if (cmd === "khipu_route_override_view") return JSON.stringify({ ok: true, overrides });
    if (cmd === "khipu_route_override_act") {
      overrides = overrides.filter((o) => o.id !== args?.id);
      return JSON.stringify({ ok: true, owner_action: "line-1" });
    }
    if (cmd === "khipu_route_cleanup_walk") return JSON.stringify(walk);
    return JSON.stringify({ ok: true });
  });
});
afterEach(() => cleanup());

describe("OverridesSection", () => {
  it("lists who may run ahead in plain words, with the end and where it stops", async () => {
    render(<OverridesSection now={NOW} />);
    expect(await screen.findByText(/The T3 conversation "Review"/)).toBeTruthy();
    expect(screen.getByText(/Ends 2:40 PM\. Stops when the account reaches 60% of its week\./)).toBeTruthy();
    expect(screen.getByText(/No end\. Time to look at it again\./)).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/route\.|override\.|ahead_of_pace|\{/);
  });

  it("ends one with a single click and offers Undo, which puts it back", async () => {
    render(<OverridesSection now={NOW} />);
    fireEvent.click(await screen.findByLabelText("End the override on Codex now"));
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("Ended the override on Codex."));
    expect(calls.filter(([c]) => c === "khipu_route_override_act")).toEqual([["khipu_route_override_act", { verb: "end", id: "ov-1" }]]);
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(calls.some(([c, a]) => c === "khipu_owner_direction_undo" && a?.id === "line-1")).toBe(true));
  });

  it("offers to keep one whose review is due", async () => {
    render(<OverridesSection now={NOW} />);
    fireEvent.click(await screen.findByLabelText("Keep the override on Grok for another week"));
    await waitFor(() => expect(calls).toContainEqual(["khipu_route_override_act", { verb: "keep", id: "ov-2" }]));
  });

  it("says plainly when none is in force", async () => {
    overrides = [];
    render(<OverridesSection now={NOW} />);
    expect(await screen.findByText("No conversation is allowed to run ahead right now.")).toBeTruthy();
  });
});

describe("CleanUpWalk", () => {
  it("shows one item at a time with one Yes, then reads again", async () => {
    render(<CleanUpWalk />);
    fireEvent.click(await screen.findByRole("button", { name: "Go through 2 items" }));
    expect(screen.getByText(/Item 1 of 2/)).toBeTruthy();
    walk = { ok: true, total: 1, left_for_later: 0, item: item({ key: "twin:x1:x5", code: "twin", of: 1, yes: "Keep both; stop asking about this pair.",
      words: "The same instruction is on both Codex and Claude.", settle: { confirm: null } }), soon: [] };
    fireEvent.click(screen.getByRole("button", { name: "End the second one and keep the first" }));
    await waitFor(() => expect(calls).toContainEqual(["khipu_route_exception_end", { account: "codex|", id: "x2", expect: "p4" }]));
    expect(await screen.findByText(/The same instruction is on both Codex and Claude/)).toBeTruthy();
    walk = { ok: true, total: 0, left_for_later: 0, item: null, soon: [] };
    fireEvent.click(screen.getByRole("button", { name: "Keep both; stop asking about this pair" }));
    await waitFor(() => expect(calls).toContainEqual(["khipu_route_cleanup_walk", { keep: "twin:x1:x5" }]));
    expect(await screen.findByText("Nothing to tidy up.")).toBeTruthy();
  });

  it("can set an item aside and start over", async () => {
    render(<CleanUpWalk />);
    fireEvent.click(await screen.findByRole("button", { name: "Go through 2 items" }));
    walk = { ok: true, total: 2, left_for_later: 1, item: item({ key: "other", of: 2 }), soon: [] };
    fireEvent.click(screen.getByRole("button", { name: "Leave it for now" }));
    await waitFor(() => expect(calls).toContainEqual(["khipu_route_cleanup_walk", { leave: "dup:codex|:x2" }]));
    fireEvent.click(await screen.findByRole("button", { name: "Start over" }));
    await waitFor(() => expect(calls).toContainEqual(["khipu_route_cleanup_walk", { restart: true }]));
  });

  it("maps each Yes to the one command that does it", () => {
    expect(settleCall(item({ settle: { confirm: { action: "route.override.end", args: { override: "o1" } } } }))).toEqual(
      { command: "khipu_route_override_act", args: { verb: "end", id: "o1" } });
    expect(settleCall(item({ settle: { confirm: { action: "route.override.keep", args: { override: "o1" } } } }))).toEqual(
      { command: "khipu_route_override_act", args: { verb: "keep", id: "o1" } });
  });
});
