// The working strip and the panel headings both stick to the top of `.main`.
// The heading must stick below the strip, never under it (the installed app
// showed "Reading the memory server" covering the Home heading).
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { BANNER_HEIGHT_VAR, WorkingBanner } from "../WorkingBanner";
// @ts-expect-error node:fs has no types here
import { readFileSync } from "node:fs";

const appCss: string = readFileSync(
  `${(globalThis as unknown as { process: { cwd(): string } }).process.cwd()}/src/App.css`,
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

  it("sticks the panel heading and the sub navigation below the strip", () => {
    expect(rule(".working-banner")).toMatch(/position: sticky;\s*top: 0;/);
    expect(rule(".panel-head")).toContain(`top: var(${BANNER_HEIGHT_VAR}, 0px);`);
    expect(rule(".subnav")).toContain(`var(${BANNER_HEIGHT_VAR}, 0px)`);
  });
});
