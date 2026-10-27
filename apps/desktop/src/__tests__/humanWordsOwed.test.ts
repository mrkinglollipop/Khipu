import { describe, expect, it } from "vitest";
import { owedReasonWords } from "../humanWords";

describe("owedReasonWords", () => {
  it("reads the duplicate rule's token as plain words with the item id", () => {
    expect(owedReasonWords("duplicate-of-21201")).toBe("a duplicate of #21201");
    expect(owedReasonWords("Looked done: duplicate-of-21201")).toBe("Looked done: a duplicate of #21201");
    expect(owedReasonWords("all 2 pull requests it names are merged")).toBe("all 2 pull requests it names are merged");
    expect(owedReasonWords(null)).toBe("");
  });
});
