import { describe, expect, it } from "vitest";
import { parseCliResponse, requireCliSuccess, requireMigrationSuccess } from "../settingsCli";

describe("fixed-command repair responses", () => {
  it("rejects the CLI's JSON refusal instead of treating it as a completed repair", () => {
    const raw = JSON.stringify({ ok: false, error: "fixture refresh refused" });
    expect(parseCliResponse(raw)).toMatchObject({ ok: false, error: "fixture refresh refused" });
    expect(() => requireCliSuccess(raw)).toThrow("fixture refresh refused");
  });

  it("rejects a migration answer that leaves named work pending", () => {
    expect(() => requireMigrationSuccess(JSON.stringify({ applied: [], pending: ["fixture_0002"] })))
      .toThrow("fixture_0002");
  });
});
