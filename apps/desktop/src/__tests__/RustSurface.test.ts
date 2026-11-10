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

  // Presence guard only: the cargo tests next to checked_path test behaviour.
  it("a path operand is refused when it points into a credential folder or climbs with ..", () => {
    const body = fnBody("checked_path");
    const gate = fnBody("in_credential_folder");
    expect(lib).toMatch(/const CREDENTIAL_DIRS: &\[&str\] = &\[".ssh"/);
    expect(body).toMatch(/ParentDir/);
    expect(body).toMatch(/in_credential_folder\(/);
    expect(gate).toMatch(/CREDENTIAL_DIRS/);
    expect(gate).toMatch(/canonicalize/);
    expect(gate).toMatch(/to_lowercase/);
  });

  it("every settable path key goes through checked_path", () => {
    expect(lib).toMatch(/PATH_KEYS\.contains\(&k\) => Ok\(set\(k, checked_path\(/);
  });

  it("KHIPU_DATA_DIR style paths are not expanded with .. left in", () => {
    const body = fnBody("expand_home_path");
    expect(body).toMatch(/canonicalize/);
  });
});

describe("the run-ahead grant and its picker", () => {
  // Presence guard only: the cargo tests next to route_override_act_argv test behaviour.
  it("the app's grant names an account, a conversation and an end, and is the owner path's own --from-app", () => {
    const body = fnBody("route_override_act_argv");
    expect(body).toMatch(/a run-ahead needs an end time/);
    expect(body).toMatch(/route_override_grant_argv\(/);
    const grant = fnBody("route_override_grant_argv");
    expect(grant).toMatch(/route_base\("override"\)/);
    expect(grant).toMatch(/checked_route_until\(until\)/);
    expect(grant).toMatch(/checked_override_subject\(subject\)/);
    expect(grant).not.toMatch(/indefinite/);
  });

  it("a subject is rebuilt from checked fields: a conversation only, never a folder or an extra key", () => {
    const body = fnBody("checked_override_subject");
    expect(body).toMatch(/"thread" && kind != "session"/);
    expect(body).toMatch(/"kind" \| "id" \| "harness" \| "mac"/);
  });

  it("the picker's read and the grant are registered commands", () => {
    expect(lib).toMatch(/async fn khipu_route_override_candidates\(account: String\)/);
    expect(lib).toMatch(/async fn khipu_route_override_act\(\s*verb: String,\s*id: Option<String>,/);
    const handler = lib.slice(lib.indexOf("generate_handler!"));
    expect(handler).toMatch(/khipu_route_override_act,\s*khipu_route_override_candidates,/);
  });
});
