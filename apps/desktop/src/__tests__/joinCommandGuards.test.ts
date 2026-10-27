// The join commands' guards live inside the Rust commands themselves: the
// page can send any argument, so lib.rs is read as text.
import lib from "../../src-tauri/src/lib.rs?raw";
import { describe, expect, it } from "vitest";

function fnBody(name: string): string {
  const start = lib.indexOf(`fn ${name}(`);
  expect(start, `fn ${name} not found`).toBeGreaterThan(-1);
  const next = lib.indexOf("\nfn ", start + 10);
  return lib.slice(start, next > -1 ? next : undefined);
}

describe("join_advertise", () => {
  it("bounds the advertise window the page asks for", () => {
    const body = fnBody("join_advertise_sync");
    expect(body).toMatch(/timeout\.(clamp|min)\(|\.min\(\s*\d+\s*\)/);
  });
});

describe("join_export", () => {
  it("asks for a native confirmation before writing an unencrypted kit", () => {
    const body = fnBody("join_export_sync");
    // a native dialog / ask inside the Rust command, not a bool from the page
    expect(body).toMatch(/dialog|\.ask\(|blocking_show|confirm/i);
  });
});
