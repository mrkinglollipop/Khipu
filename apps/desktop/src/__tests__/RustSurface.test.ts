// Webview-facing gates in the Rust shell, read from source. The behaviour
// itself is covered by the cargo tests next to `checked_path`.
import { describe, expect, it } from "vitest";
import lib from "../../src-tauri/src/lib.rs?raw";
import tauriConf from "../../src-tauri/tauri.conf.json?raw";

function fnBody(name: string): string {
  const start = lib.indexOf(`fn ${name}(`);
  expect(start, `fn ${name} not found`).toBeGreaterThan(-1);
  const next = lib.indexOf("\nfn ", start + 10);
  return lib.slice(start, next > -1 ? next : undefined);
}

describe("webview-facing path and policy gates", () => {
  it("the window has a content-security policy limited to its own assets", () => {
    const conf = JSON.parse(tauriConf);
    const csp = conf.app.security.csp;
    expect(csp).not.toBeNull();
    expect(csp).toMatch(/default-src 'self'/);
    expect(csp).toMatch(/ipc:/);
  });

  it("a path operand is refused when it points into a credential folder or climbs with ..", () => {
    const body = fnBody("checked_path");
    expect(lib).toMatch(/const CREDENTIAL_DIRS: &\[&str\] = &\[".ssh"/);
    expect(body).toMatch(/CREDENTIAL_DIRS/);
    expect(body).toMatch(/ParentDir/);
    expect(body).toMatch(/canonicalize/);
  });

  it("every settable path key goes through checked_path", () => {
    expect(lib).toMatch(/PATH_KEYS\.contains\(&k\) => Ok\(set\(k, checked_path\(/);
  });

  it("KHIPU_DATA_DIR style paths are not expanded with .. left in", () => {
    const body = fnBody("expand_home_path");
    expect(body).toMatch(/canonicalize/);
  });
});
