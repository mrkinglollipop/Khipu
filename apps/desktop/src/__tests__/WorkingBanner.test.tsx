// The working strip lives above the panel scroller. Keeping them in separate
// layout tracks prevents scrolled panel contents from painting over the strip.
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { BANNER_HEIGHT_VAR, WorkingBanner } from "../WorkingBanner";
// @ts-expect-error node:fs has no types here
import { readFileSync } from "node:fs";

const appCss: string = readFileSync(
  `${(globalThis as unknown as { process: { cwd(): string } }).process.cwd()}/src/App.css`,
  "utf8",
);
const appSource: string = readFileSync(
  `${(globalThis as unknown as { process: { cwd(): string } }).process.cwd()}/src/App.tsx`,
  "utf8",
);
const rule = (selector: string) => {
  const at = appCss.indexOf(`\n${selector} {`);
  expect(at).toBeGreaterThanOrEqual(0);
  return appCss.slice(at, appCss.indexOf("}", at));
};

afterEach(cleanup);

describe("WorkingBanner", () => {
  it("gives its scroller its height while shown and takes it back when gone", () => {
    const height = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 52 });
    try {
      const { container, rerender } = render(
        <main>
          <WorkingBanner label="Reading the memory server" />
        </main>,
      );
      const main = container.querySelector("main")!;
      expect(main.style.getPropertyValue(BANNER_HEIGHT_VAR)).toBe("52px");
      rerender(
        <main>
          <WorkingBanner label={null} />
        </main>,
      );
      expect(main.style.getPropertyValue(BANNER_HEIGHT_VAR)).toBe("");
    } finally {
      if (height) Object.defineProperty(HTMLElement.prototype, "offsetHeight", height);
    }
  });

  it("keeps the banner outside the panel scroller", () => {
    const { container } = render(
      <main className="main">
        <WorkingBanner label="Reading the memory server" />
        <div className="main-content"><section>Scrolled panel</section></div>
      </main>,
    );
    const main = container.querySelector("main")!;
    const banner = container.querySelector<HTMLElement>(".working-banner")!;
    const scroller = container.querySelector(".main-content")!;
    expect(banner.parentElement).toBe(main);
    expect(scroller.parentElement).toBe(main);
    expect(banner.nextElementSibling).toBe(scroller);
    expect(scroller).not.toContainElement(banner);
    const bannerAt = appSource.indexOf('<WorkingBanner label={workingLabel} />');
    const scrollerAt = appSource.indexOf('<div className="main-content">');
    expect(bannerAt).toBeGreaterThanOrEqual(0);
    expect(scrollerAt).toBeGreaterThan(bannerAt);
    expect(rule(".main")).toContain("overflow: hidden;");
    expect(rule(".main-content")).toContain("overflow-y: auto;");
    // Overlay, not a row: it must not push the page down when it appears.
    expect(rule(".working-banner")).toContain("position: absolute;");
    expect(rule(".working-banner")).toContain("pointer-events: none;");
    expect(rule(".main")).toContain("position: relative;");
    expect(rule(".panel-head")).toContain("top: 0;");
  });
});
