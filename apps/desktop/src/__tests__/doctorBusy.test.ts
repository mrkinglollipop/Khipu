// While the health check reads, the pill is gone but main stays aria-busy and a visually hidden status says so.
import { describe, expect, it } from "vitest";
// @ts-expect-error node:fs has no types here
import { readFileSync } from "node:fs";

const appSource: string = readFileSync(
  `${(globalThis as unknown as { process: { cwd(): string } }).process.cwd()}/src/App.tsx`,
  "utf8",
);

describe("health check read is exposed to assistive technology", () => {
  it("main is aria-busy while loading.doctor", () => {
    expect(appSource).toMatch(/<main className="main" aria-busy=\{[^}]*loading\.doctor[^}]*\}>/);
  });
  it("a visually hidden role=status names the read", () => {
    expect(appSource).toMatch(/className="sr-only" role="status">\{loading\.doctor \? "Reading the health check…" : ""\}/);
  });
});
