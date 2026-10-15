import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ACCOUNTS_SETUP,
  CARD_MAX_ITEMS,
  DIALOG_MAX_ITEMS,
  POST_UPDATE_NOTICES,
  itemAction,
  itemsBetween,
  noticesBetween,
  planLaunch,
  readCardState,
  readLastNoticedVersion,
  recordVersionOnSetupFinish,
  routingNeedsSetup,
  versionGreaterThan,
  writeCardState,
  writeLastNoticedVersion,
} from "../postUpdateNotices";

// jsdom here has no usable localStorage; an in-memory one stands in.
beforeEach(() => {
  const store = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  });
});

describe("planLaunch", () => {
  it("lists every item from all skipped versions, newest first (0.4.4 to 0.4.8)", () => {
    const plan = planLaunch("0.4.4", "0.4.8", null);
    expect(plan.dialog?.items.map((i) => i.title)).toEqual([
      "Embeddings has its own screen in Settings",
      "Optional features now have switches in Settings",
      "Recall tells current decisions from reversed ones",
    ]);
    expect(plan.dialog?.since).toBe("0.4.4");
    expect(plan.dialog?.version).toBe("0.4.8");
    expect(plan.card).toEqual({ since: "0.4.4", version: "0.4.8" });
  });

  it("shows no dialog and no card on a fresh install", () => {
    expect(planLaunch("", "0.4.16", null)).toEqual({ dialog: null, card: null });
  });

  it("never brings the dialog back for the same version, and leaves the card", () => {
    const card = { since: "0.4.12", version: "0.4.16" };
    const plan = planLaunch("0.4.16", "0.4.16", card);
    expect(plan.dialog).toBeNull();
    expect(plan.card).toEqual(card);
  });

  it("closing the card leaves no dialog", () => {
    writeCardState({ since: "0.4.12", version: "0.4.16" });
    writeCardState(null);
    expect(readCardState()).toBeNull();
    expect(planLaunch("0.4.16", "0.4.16", readCardState())).toEqual({ dialog: null, card: null });
  });

  it("keeps an older open card start so nothing it listed drops off", () => {
    const plan = planLaunch("0.4.8", "0.4.16", { since: "0.4.4", version: "0.4.8" });
    expect(plan.card).toEqual({ since: "0.4.4", version: "0.4.16" });
  });

  it("shows nothing when the update added no items", () => {
    expect(planLaunch("0.4.16", "0.4.17", null)).toEqual({ dialog: null, card: null });
  });
});

describe("recording the version", () => {
  it("finishing setup records the running version", () => {
    recordVersionOnSetupFinish("0.4.16");
    expect(readLastNoticedVersion()).toBe("0.4.16");
  });
  it("does not record while the version is unknown", () => {
    recordVersionOnSetupFinish("…");
    recordVersionOnSetupFinish("unknown");
    expect(readLastNoticedVersion()).toBe("");
  });
  it("round-trips the stored version", () => {
    writeLastNoticedVersion("0.4.8");
    expect(readLastNoticedVersion()).toBe("0.4.8");
  });
});

describe("routing setup and item buttons", () => {
  const routing = POST_UPDATE_NOTICES.flatMap((n) => n.items).find((i) => i.setUp === "routing")!;
  it("routingNeedsSetup: unknown, no record, no accounts, accounts", () => {
    expect(routingNeedsSetup(null)).toBe(false);
    expect(routingNeedsSetup({ record: false })).toBe(true);
    expect(routingNeedsSetup({ record: true, accounts: [] })).toBe(true);
    expect(routingNeedsSetup({ record: true, accounts: [{}] })).toBe(false);
  });
  it("the routing item says Set up accounts and targets the accounts step when needed", () => {
    expect(itemAction(routing, true)).toEqual({ label: "Set up accounts", target: ACCOUNTS_SETUP });
    expect(ACCOUNTS_SETUP).toEqual({ kind: "setup", step: "accounts" });
    expect(itemAction(routing, false)).toEqual({ label: "Show me", target: routing.target });
  });
});

describe("the notice list", () => {
  it("has no em-dashes and no empty text", () => {
    expect(JSON.stringify(POST_UPDATE_NOTICES)).not.toContain("—");
    for (const i of POST_UPDATE_NOTICES.flatMap((n) => n.items)) {
      expect(i.title.length).toBeGreaterThan(0);
      expect(i.text.length).toBeGreaterThan(0);
    }
  });
  it("orders versions numerically, newest first", () => {
    expect(versionGreaterThan("0.4.10", "0.4.9")).toBe(true);
    const v = noticesBetween("", "0.4.16").map((n) => n.version);
    expect(v[0]).toBe("0.4.16");
    expect(v[v.length - 1]).toBe("0.3.16");
  });
  it("this release has more items than the dialog and card show", () => {
    const items = itemsBetween("0.4.8", "0.4.16");
    expect(items.length).toBe(8);
    expect(DIALOG_MAX_ITEMS).toBe(5);
    expect(CARD_MAX_ITEMS).toBe(3);
  });
});
