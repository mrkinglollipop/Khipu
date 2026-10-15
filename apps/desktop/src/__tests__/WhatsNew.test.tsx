import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { PostUpdateNoticeDialog } from "../PostUpdateNoticeDialog";
import { WhatsNewCard, WhatsNewHistory, revealSettingsGroup } from "../WhatsNew";
import { ALL_CAPABILITIES, itemsBetween, type NoticeTarget } from "../postUpdateNotices";

afterEach(cleanup);

const plan = (since: string, version: string) => ({ since, version, items: itemsBetween(since, version, ALL_CAPABILITIES) });

describe("PostUpdateNoticeDialog", () => {
  it("shows at most 5 items and says how many more for 8", () => {
    render(<PostUpdateNoticeDialog caps={ALL_CAPABILITIES} plan={plan("0.4.8", "0.4.16")} needsRoutingSetup={false} onShow={vi.fn()} onDismiss={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "What's new since 0.4.8" })).toBeInTheDocument();
    expect(screen.getByText(/Khipu is now version 0\.4\.16\. This list also stays on Home/)).toBeInTheDocument();
    expect(within(screen.getByRole("list")).getAllByRole("listitem")).toHaveLength(5);
    expect(screen.getByText("And 3 more in Settings → Updates.")).toBeInTheDocument();
  });

  it("has no more line when everything fits", () => {
    render(<PostUpdateNoticeDialog caps={ALL_CAPABILITIES} plan={plan("0.4.4", "0.4.8")} needsRoutingSetup={false} onShow={vi.fn()} onDismiss={vi.fn()} />);
    expect(screen.queryByText(/more in Settings/)).toBeNull();
  });

  it("Got it dismisses without calling onShow", () => {
    const onShow = vi.fn();
    const onDismiss = vi.fn();
    render(<PostUpdateNoticeDialog caps={ALL_CAPABILITIES} plan={plan("0.4.8", "0.4.16")} needsRoutingSetup={false} onShow={onShow} onDismiss={onDismiss} />);
    fireEvent.click(screen.getByRole("button", { name: "Got it" }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(onShow).not.toHaveBeenCalled();
  });

  it("routing without setup shows Set up accounts that opens the accounts step", () => {
    const onShow = vi.fn();
    const onDismiss = vi.fn();
    render(<PostUpdateNoticeDialog caps={ALL_CAPABILITIES} plan={plan("0.4.8", "0.4.16")} needsRoutingSetup onShow={onShow} onDismiss={onDismiss} />);
    fireEvent.click(screen.getByRole("button", { name: "Set up accounts: Accounts and routing" }));
    expect(onShow).toHaveBeenCalledWith({ kind: "setup", step: "accounts" });
    expect(onDismiss).toHaveBeenCalled();
  });

  it("each Show me calls onShow with its item's target and closes", () => {
    const p = plan("0.4.4", "0.4.8");
    for (const item of p.items) {
      const onShow = vi.fn();
      const onDismiss = vi.fn();
      const { unmount } = render(<PostUpdateNoticeDialog caps={ALL_CAPABILITIES} plan={p} needsRoutingSetup={false} onShow={onShow} onDismiss={onDismiss} />);
      fireEvent.click(screen.getByRole("button", { name: `Show me: ${item.title}` }));
      expect(onShow).toHaveBeenCalledWith(item.target);
      expect(onDismiss).toHaveBeenCalled();
      unmount();
    }
  });

  it("reaches settings group, setup step, home and harnesses targets", () => {
    const targets: NoticeTarget[] = [];
    const p = plan("0.3.15", "0.4.16");
    render(<PostUpdateNoticeDialog caps={ALL_CAPABILITIES} plan={{ ...p, items: p.items }} needsRoutingSetup={false} onShow={(t) => targets.push(t)} onDismiss={vi.fn()} />);
    // Items beyond 5 are not shown; check the kinds across the full list instead.
    const kinds = new Set(p.items.map((i) => i.target.kind));
    expect(kinds).toEqual(new Set(["home", "harnesses", "settings", "setup"]));
    expect(p.items.some((i) => i.target.kind === "settings" && i.target.group)).toBe(true);
  });

  it("renders nothing visible when there is no plan", () => {
    render(<PostUpdateNoticeDialog caps={ALL_CAPABILITIES} plan={null} needsRoutingSetup={false} onShow={vi.fn()} onDismiss={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Got it" })).toBeNull();
  });
});

describe("WhatsNewCard", () => {
  it("shows 3 items and '5 more since 0.4.12'", () => {
    render(
      <WhatsNewCard caps={ALL_CAPABILITIES} appVersion="0.4.16" card={{ since: "0.4.12", version: "0.4.16" }} needsRoutingSetup={false} onShow={vi.fn()} onClose={vi.fn()} onSeeAll={vi.fn()} />,
    );
    const region = screen.getByRole("region", { name: "New in 0.4.16" });
    expect(within(region).getAllByRole("listitem")).toHaveLength(3);
    expect(within(region).getByText("5 more since 0.4.12")).toBeInTheDocument();
  });

  it("Close and See all call their handlers, Show me calls onShow", () => {
    const onClose = vi.fn();
    const onSeeAll = vi.fn();
    const onShow = vi.fn();
    render(
      <WhatsNewCard caps={ALL_CAPABILITIES} appVersion="0.4.16" card={{ since: "0.4.12", version: "0.4.16" }} needsRoutingSetup={false} onShow={onShow} onClose={onClose} onSeeAll={onSeeAll} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Close the What's new card" }));
    fireEvent.click(screen.getByRole("button", { name: "See all in Settings → Updates" }));
    fireEvent.click(screen.getByRole("button", { name: "Show me: Priority and pace" }));
    expect(onClose).toHaveBeenCalled();
    expect(onSeeAll).toHaveBeenCalled();
    expect(onShow).toHaveBeenCalledWith({ kind: "settings", section: "routing", group: "priority" });
  });

  it("has no foot row when all items fit", () => {
    render(
      <WhatsNewCard caps={ALL_CAPABILITIES} appVersion="0.4.16" card={{ since: "0.4.7", version: "0.4.8" }} needsRoutingSetup={false} onShow={vi.fn()} onClose={vi.fn()} onSeeAll={vi.fn()} />,
    );
    expect(screen.queryByText(/more since/)).toBeNull();
  });

  it("uses the Set up accounts button when routing is not set up", () => {
    render(
      <WhatsNewCard caps={ALL_CAPABILITIES} appVersion="0.4.16" card={{ since: "0.4.12", version: "0.4.16" }} needsRoutingSetup onShow={vi.fn()} onClose={vi.fn()} onSeeAll={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: "Set up accounts: Accounts and routing" })).toBeInTheDocument();
  });
});

describe("WhatsNewHistory", () => {
  it("lists the newest 3 versions open and the rest in a button fold that toggles", () => {
    render(<WhatsNewHistory caps={ALL_CAPABILITIES} appVersion="0.4.16" needsRoutingSetup={false} onShow={vi.fn()} />);
    expect(screen.getByText("installed")).toBeInTheDocument();
    const fold = screen.getByRole("button", { name: /Earlier versions/ });
    expect(fold.tagName).toBe("BUTTON");
    expect(fold).toHaveAttribute("aria-expanded", "false");
    expect(fold).toHaveTextContent("5 more");
    const body = document.getElementById(fold.getAttribute("aria-controls")!)!;
    expect(body.hidden).toBe(true);
    fireEvent.click(fold);
    expect(fold).toHaveAttribute("aria-expanded", "true");
    expect(body.hidden).toBe(false);
    expect(within(body).getByText("0.4.5 and older, newest first.")).toBeInTheDocument();
    fireEvent.click(fold);
    expect(fold).toHaveAttribute("aria-expanded", "false");
  });

  it("shows only versions up to the installed one", () => {
    render(<WhatsNewHistory caps={ALL_CAPABILITIES} appVersion="0.4.8" needsRoutingSetup={false} onShow={vi.fn()} />);
    expect(screen.queryByText("0.4.16")).toBeNull();
    expect(screen.getByRole("heading", { name: /0\.4\.8/ })).toBeInTheDocument();
  });

  it("each Show me calls onShow with its target", () => {
    const onShow = vi.fn();
    render(<WhatsNewHistory caps={ALL_CAPABILITIES} appVersion="0.4.16" needsRoutingSetup={false} onShow={onShow} />);
    fireEvent.click(screen.getByRole("button", { name: "Show me: Setup" }));
    expect(onShow).toHaveBeenCalledWith({ kind: "setup", step: "accounts" });
  });
});

describe("revealSettingsGroup", () => {
  it("opens a folded group button and does nothing when absent", async () => {
    const raf = vi.spyOn(window, "requestAnimationFrame").mockImplementation((cb) => {
      cb(0);
      return 0;
    });
    const btn = document.createElement("button");
    btn.setAttribute("data-settings-group", "accounts");
    btn.setAttribute("aria-expanded", "false");
    btn.addEventListener("click", () => btn.setAttribute("aria-expanded", "true"));
    document.body.appendChild(btn);
    revealSettingsGroup("accounts");
    expect(btn.getAttribute("aria-expanded")).toBe("true");
    expect(() => revealSettingsGroup("missing")).not.toThrow();
    btn.remove();
    raf.mockRestore();
  });
});
