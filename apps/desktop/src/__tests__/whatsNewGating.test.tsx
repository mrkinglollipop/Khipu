import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { PostUpdateNoticeDialog } from "../PostUpdateNoticeDialog";
import { WhatsNewCard, WhatsNewHistory } from "../WhatsNew";
import {
  ALL_CAPABILITIES,
  BUILD_CAPABILITIES,
  POST_UPDATE_NOTICES,
  cardView,
  itemAction,
  itemsBetween,
  launchWhatsNew,
  readLastNoticedVersion,
  recordVersionOnSetupFinish,
  writeCardState,
  writeLastNoticedVersion,
  type Capability,
} from "../postUpdateNotices";

beforeEach(() => {
  const store = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  });
});
afterEach(cleanup);

describe("a downgrade never shows news from a newer version", () => {
  it("drops a retained card whose items are all newer than the running version", () => {
    expect(cardView({ since: "0.4.8", version: "0.4.16" }, "0.4.8", ALL_CAPABILITIES)).toBeNull();
  });

  it("trims a retained card to the running version", () => {
    const view = cardView({ since: "0.4.4", version: "0.4.16" }, "0.4.8", ALL_CAPABILITIES);
    expect(view?.version).toBe("0.4.8");
    expect(view?.items.map((i) => i.title)).not.toContain("Updates");
  });

  it("the Home card renders nothing after a downgrade", () => {
    render(
      <WhatsNewCard
        card={{ since: "0.4.8", version: "0.4.16" }}
        appVersion="0.4.8"
        needsRoutingSetup={false}
        onShow={vi.fn()}
        onClose={vi.fn()}
        onSeeAll={vi.fn()}
      />,
    );
    expect(screen.queryByText(/New in/)).toBeNull();
  });
});

describe("the shown-version watermark only moves up", () => {
  it("downgrade then re-upgrade does not bring back dismissed news", () => {
    writeLastNoticedVersion("0.4.8");
    const first = launchWhatsNew("0.4.16");
    expect(first.dialog).not.toBeNull();
    writeCardState(null); // the card closed; Got it closed the dialog
    expect(launchWhatsNew("0.4.8").dialog).toBeNull();
    expect(readLastNoticedVersion()).toBe("0.4.16");
    const again = launchWhatsNew("0.4.16");
    expect(again.dialog).toBeNull();
    expect(again.card).toBeNull();
  });

  it("finishing setup on an older build never lowers it", () => {
    writeLastNoticedVersion("0.4.16");
    recordVersionOnSetupFinish("0.4.8");
    expect(readLastNoticedVersion()).toBe("0.4.16");
  });
});

describe("only what is in this build", () => {
  it("every capability an item needs is a known one", () => {
    for (const n of POST_UPDATE_NOTICES) {
      for (const item of n.items) {
        if (item.requires) expect(ALL_CAPABILITIES.has(item.requires)).toBe(true);
      }
    }
  });

  it("leaves out items whose feature is not in the build", () => {
    const none = new Set<Capability>();
    expect(itemsBetween("0.4.15", "0.4.16", none).map((i) => i.title)).toEqual(["Priority and pace", "Updates"]);
    expect(itemsBetween("0.4.15", "0.4.16", ALL_CAPABILITIES)).toHaveLength(8);
  });

  it("the default build has no news for a feature it lacks", () => {
    const titles = itemsBetween("0.4.15", "0.4.16").map((i) => i.title);
    if (!BUILD_CAPABILITIES.has("setup-accounts-step")) expect(titles).not.toContain("Setup");
    if (!BUILD_CAPABILITIES.has("routing-overview")) expect(titles).not.toContain("Accounts and routing");
  });

  it("gives no button when the target is missing, and falls back from Set up accounts", () => {
    const none = new Set<Capability>();
    const item = (title: string) => itemsBetween("0.4.15", "0.4.16", ALL_CAPABILITIES).find((i) => i.title === title)!;
    expect(itemAction(item("Priority and pace"), false, none)).toBeNull();
    expect(itemAction(item("Setup"), false, none)).toBeNull();
    expect(itemAction(item("Updates"), false, none)?.label).toBe("Show me");
    // Set up accounts needs the setup step; without it the routing item has no button.
    expect(itemAction(item("Accounts and routing"), true, new Set<Capability>(["routing-overview"]))).toBeNull();
    expect(itemAction(item("Accounts and routing"), true, ALL_CAPABILITIES)?.label).toBe("Set up accounts");
  });

  it("the dialog, card and history show no Show me for a missing target", () => {
    const none = new Set<Capability>();
    const items = itemsBetween("0.4.15", "0.4.16", none);
    render(
      <PostUpdateNoticeDialog
        plan={{ since: "0.4.15", version: "0.4.16", items }}
        needsRoutingSetup={false}
        caps={none}
        onShow={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );
    const rows = within(screen.getByRole("list")).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).queryByRole("button")).toBeNull();
    expect(within(rows[1]).getByRole("button", { name: "Show me: Updates" })).toBeInTheDocument();
    cleanup();

    render(
      <WhatsNewCard
        card={{ since: "0.4.15", version: "0.4.16" }}
        appVersion="0.4.16"
        caps={none}
        needsRoutingSetup={false}
        onShow={vi.fn()}
        onClose={vi.fn()}
        onSeeAll={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: /Priority and pace/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Show me: Updates" })).toBeInTheDocument();
    cleanup();

    render(<WhatsNewHistory appVersion="0.4.16" caps={none} needsRoutingSetup={false} onShow={vi.fn()} />);
    expect(screen.queryByText("Standing rules")).toBeNull();
    expect(screen.queryByRole("button", { name: /Priority and pace/ })).toBeNull();
  });
});
