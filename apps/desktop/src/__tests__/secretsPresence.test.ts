import { describe, expect, it } from "vitest";
import { keyStatus } from "../KeysCard";
import { isSecretsPresence, pickSecretsPresence } from "../secretsPresence";

// What `khipu secrets` reports for the plan-usage keys (no values, only flags).
const CLI = {
  dsn_in_keychain: true,
  glm_in_keychain: true, glm_env: false, glm_file_key_present: false,
  kimi_in_keychain: true, kimi_env: true, kimi_file_key_present: false,
  config_dir: "/x",
};

describe("the app's secrets presence allowlist", () => {
  it("keeps the GLM and Kimi flags the Keys card reads", () => {
    expect(isSecretsPresence(CLI)).toBe(true);
    const kept = pickSecretsPresence(CLI as never);
    expect(keyStatus(kept, null, "glm").source).toBe("keychain");
    expect(keyStatus(kept, null, "kimi")).toMatchObject({ source: "env", keychainToo: true });
    const file = pickSecretsPresence({ ...CLI, kimi_in_keychain: false, kimi_env: false, kimi_file_key_present: true } as never);
    expect(keyStatus(file, null, "kimi").source).toBe("file");
    expect("config_dir" in kept).toBe(false);
  });

  it("refuses a non-boolean Kimi flag", () => {
    expect(isSecretsPresence({ ...CLI, kimi_env: "yes" })).toBe(false);
  });
});
