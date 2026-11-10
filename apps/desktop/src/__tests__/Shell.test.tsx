// The window shell (slice A1): sidebar, tab bar, toolbar, split, inspector and
// the list keys. Fictional places; no app content.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
// @ts-expect-error node:fs has no types here
import { readFileSync } from "node:fs";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Body, Inspector, Sidebar, Split, TabBar, Toolbar, useListKeys } from "../shell";
import type { NavItem } from "../shell";

afterEach(cleanup);

const places: NavItem[] = [
  { id: "today", label: "Today", count: 2, countLabel: "2 need you" },
  { id: "accounts", label: "Accounts" },
  { id: "memory", label: "Memory" },
];
const settings: NavItem = { id: "settings", label: "Settings" };

function Harness({ start = "today", mac }: { start?: string; mac?: string | null }) {
  const [current, setCurrent] = useState(start);
  return (
    <div>
      <Sidebar brand={<i />} items={places} current={current} onGo={setCurrent} settings={settings} macName={mac} />
      <TabBar items={places} current={current} onGo={setCurrent} />
    </div>
  );
}

describe("Sidebar", () => {
  it("lists the places with Today's count, marks the one on show, and goes where asked", () => {
    render(<Harness />);
    const main = screen.getByRole("navigation", { name: "Main" });
    expect(within(main).getByRole("button", { name: /^Today/ })).toHaveAttribute("aria-current", "page");
    expect(within(main).getByLabelText("2 need you")).toHaveTextContent("2");
    fireEvent.click(within(main).getByRole("button", { name: "Accounts" }));
    expect(within(main).getByRole("button", { name: "Accounts" })).toHaveAttribute("aria-current", "page");
    expect(within(main).getByRole("button", { name: /^Today/ })).not.toHaveAttribute("aria-current");
  });

  it("names the Mac, or says 'this Mac' while the name is not known", () => {
    const { rerender } = render(<Harness />);
    expect(screen.getByText("On this Mac")).toBeInTheDocument();
    rerender(<Harness mac="M5" />);
    expect(screen.getByText("On M5")).toBeInTheDocument();
  });

  it("has no Older screens entry", () => {
    render(<Harness />);
    expect(screen.queryByRole("button", { name: "Older screens" })).toBeNull();
  });

  it("has a tab bar with the same places", () => {
    render(<Harness />);
    const tabs = screen.getByRole("navigation", { name: "Places" });
    fireEvent.click(within(tabs).getByRole("button", { name: "Memory" }));
    expect(within(tabs).getByRole("button", { name: "Memory" })).toHaveAttribute("aria-current", "page");
  });
});

describe("Toolbar", () => {
  it("is a heading by default and plain text when the screen draws its own", () => {
    const { rerender } = render(<Toolbar title="Accounts" />);
    expect(screen.getByRole("heading", { name: "Accounts" })).toBeInTheDocument();
    rerender(<Toolbar title="Accounts" heading={false} />);
    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.getByText("Accounts")).toBeInTheDocument();
  });
});

describe("Split and Inspector", () => {
  it("names both panes and tells the narrow layout which one to show", () => {
    const { container, rerender } = render(<Split list={<p>rows</p>} listLabel="Accounts" detail={<p>page</p>} detailLabel="Claude" detailOpen />);
    expect(screen.getByRole("region", { name: "Accounts" })).toHaveTextContent("rows");
    expect(screen.getByRole("region", { name: "Claude" })).toHaveTextContent("page");
    expect(container.querySelector(".sh-split")).toHaveAttribute("data-detail", "open");
    rerender(<Split list={<p>rows</p>} listLabel="Accounts" detail={<p>page</p>} detailLabel="Claude" />);
    expect(container.querySelector(".sh-split")).not.toHaveAttribute("data-detail");
  });

  it("docks the inspector and closes it with the button or Escape", () => {
    const onClose = vi.fn();
    render(<Body><Inspector title="How Khipu is doing" onClose={onClose}><p>five checks</p></Inspector></Body>);
    const panel = screen.getByRole("complementary", { name: "How Khipu is doing" });
    fireEvent.click(within(panel).getByRole("button", { name: "Close" }));
    fireEvent.keyDown(within(panel).getByText("five checks"), { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});

describe("useListKeys", () => {
  function List() {
    const keys = useListKeys();
    const [on, setOn] = useState("a");
    return (
      <div {...keys} role="group" aria-label="rows">
        {["a", "b", "c"].map((id) => (
          <button key={id} type="button" aria-current={on === id} onClick={() => setOn(id)}>{id}</button>
        ))}
      </div>
    );
  }
  it("moves the focus and the selection with the arrow keys, and stops at the ends", () => {
    render(<List />);
    const [a, b, c] = ["a", "b", "c"].map((n) => screen.getByRole("button", { name: n }));
    a.focus();
    fireEvent.keyDown(a, { key: "ArrowDown" });
    expect(b).toHaveFocus();
    expect(b).toHaveAttribute("aria-current", "true");
    fireEvent.keyDown(b, { key: "End" });
    expect(c).toHaveFocus();
    fireEvent.keyDown(c, { key: "ArrowDown" });
    expect(c).toHaveFocus();
    fireEvent.keyDown(c, { key: "ArrowUp" });
    expect(b).toHaveFocus();
    fireEvent.keyDown(b, { key: "Home" });
    expect(a).toHaveAttribute("aria-current", "true");
  });
});

describe("shell.css breakpoints", () => {
  const cwd = (globalThis as unknown as { process: { cwd(): string } }).process.cwd();
  const css: string = readFileSync(`${cwd}/src/shell/shell.css`, "utf8");
  it("turns into a top bar under 900 px and a tab bar under 640 px", () => {
    expect(css).toMatch(/@media \(max-width: 899px\)/);
    expect(css).toMatch(/@media \(max-width: 639px\)/);
    expect(css).toMatch(/\.sh-tabs\s*\{\s*display: none;/);
  });
});
