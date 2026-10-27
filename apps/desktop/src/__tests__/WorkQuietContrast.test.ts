// @ts-expect-error node:fs has no types here
import { readFileSync } from "node:fs";
import { expect, it } from "vitest";

const cwd = (globalThis as unknown as { process: { cwd(): string } }).process.cwd();
const appCss: string = readFileSync(`${cwd}/src/App.css`, "utf8");
const workCss: string = readFileSync(`${cwd}/src/Work.css`, "utf8");
const quietRule = workCss.match(/\.nav-badge\.quiet\s*\{([^}]+)\}/)?.[1];

function badgeToken(property: string): string {
  const token = quietRule?.match(new RegExp(`(?:^|;)\\s*${property}:\\s*var\\((--[\\w-]+)\\)`))?.[1];
  if (!token) throw new Error(`Quiet badge has no ${property} token`);
  return token;
}

function themeTokens(block: string): Map<string, string> {
  return new Map([...block.matchAll(/(--[\w-]+):\s*(#[\da-f]{6})\s*;/gi)].map((m) => [m[1], m[2]]));
}

function luminance(hex: string): number {
  const rgb = hex.slice(1).match(/../g)!.map((channel) => {
    const value = parseInt(channel, 16) / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2];
}

it.each(["light", "dark"])("quiet badge small text has 4.5:1 contrast in %s mode", (theme) => {
  const roots = [...appCss.matchAll(/:root\s*\{([^}]+)\}/g)];
  expect(roots).toHaveLength(2);
  expect(appCss.slice(roots[0].index! + roots[0][0].length, roots[1].index)).toContain("prefers-color-scheme: dark");
  const tokens = themeTokens(roots[0][1]);
  if (theme === "dark") for (const [key, value] of themeTokens(roots[1][1])) tokens.set(key, value);
  const fg = tokens.get(badgeToken("color"));
  const bg = tokens.get(badgeToken("background"));
  expect(fg).toBeDefined();
  expect(bg).toBeDefined();
  const [lighter, darker] = [luminance(fg!), luminance(bg!)].sort((a, b) => b - a);
  const ratio = (lighter + 0.05) / (darker + 0.05);
  expect(ratio, `${theme} quiet badge ${fg} on ${bg}: ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5);
});
