import { describe, expect, it } from "vitest";
import rustSource from "../../src-tauri/src/lib.rs?raw";

const sources = import.meta.glob<string>(["../**/*.ts", "../**/*.tsx", "!../**/__tests__/**"], {
  query: "?raw",
  import: "default",
  eager: true,
});

function camel(name: string) {
  return name.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase());
}

function objectKeys(source: string, start: number): string[] {
  const keys: string[] = [];
  let depth = 0;
  let quote = "";
  let fieldStart = start;
  const field = (end: number) => {
    const name = source.slice(fieldStart, end).trim().match(/^([A-Za-z_$][\w$]*)\s*(?=:|$)/)?.[1];
    if (name) keys.push(name);
  };
  for (let i = start; i < source.length; i += 1) {
    const char = source[i];
    if (quote) {
      if (char === "\\") i += 1;
      else if (char === quote) quote = "";
    } else if (char === '"' || char === "'" || char === "`") {
      quote = char;
    } else if (char === "}" && depth === 0) {
      field(i);
      return keys;
    } else if ("{[(".includes(char)) {
      depth += 1;
    } else if ("}])".includes(char)) {
      depth -= 1;
    } else if (char === "," && depth === 0) {
      field(i);
      fieldStart = i + 1;
    }
  }
  throw new Error("Unclosed command argument object");
}

function rustCommands(): Map<string, Set<string>> {
  const source = rustSource;
  const commands = new Map<string, Set<string>>();
  const marker = /#\[tauri::command\]\s*async fn\s+(\w+)\s*\(/g;
  for (let found; (found = marker.exec(source));) {
    let depth = 1;
    let end = marker.lastIndex;
    while (depth > 0 && end < source.length) {
      const char = source[end++];
      if (char === "(") depth += 1;
      if (char === ")") depth -= 1;
    }
    const params = source.slice(marker.lastIndex, end - 1)
      .split(",")
      .map((part) => part.trim().match(/^(\w+)\s*:/)?.[1])
      .filter((name): name is string => Boolean(name))
      .map(camel);
    commands.set(found[1], new Set(params));
    marker.lastIndex = end;
  }
  return commands;
}

describe("Tauri invoke contracts", () => {
  it("uses the Rust command's camelCase argument names for every literal wrapper call", () => {
    const commands = rustCommands();
    const violations: string[] = [];
    let checked = 0;
    for (const [path, source] of Object.entries(sources)) {
      // The installed `typescript` package exposes declarations only in this
      // checkout, so keep this source-level contract parser dependency-free.
      // A literal command followed by an object also covers wrappers which
      // accept a control id before the command. Nested payload keys are not
      // Tauri arguments; shorthand properties are.
      const calls = /["']([^"'\n]+)["']\s*,\s*\{/g;
      for (let found; (found = calls.exec(source));) {
        const command = found[1];
        const expected = commands.get(command);
        if (!expected) continue;
        checked += 1;
        const keys = objectKeys(source, calls.lastIndex);
        for (const key of keys) {
          if (!expected.has(key)) violations.push(`${path}: ${command}.${key}`);
        }
      }
    }
    expect(commands.size).toBeGreaterThan(100);
    expect(checked).toBeGreaterThan(100);
    expect(violations).toEqual([]);
  });
});
