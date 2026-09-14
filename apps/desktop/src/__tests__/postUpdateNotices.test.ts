import { describe, it, expect } from "vitest";
import { noticeForUpgrade } from "../postUpdateNotices";

describe("noticeForUpgrade", () => {
  it("returns the 0.4.2 notice when upgrading from 0.4.1", () => {
    const notice = noticeForUpgrade("0.4.1", "0.4.2");
    expect(notice).not.toBeNull();
    expect(notice?.version).toBe("0.4.2");
    expect(notice?.action).toBe("home");
  });

  it("returns null when 0.4.2 was already noticed", () => {
    expect(noticeForUpgrade("0.4.2", "0.4.2")).toBeNull();
  });

  it("returns null on a fresh install (empty stored version)", () => {
    expect(noticeForUpgrade("", "0.4.2")).toBeNull();
  });

  it("returns the 0.4.4 notice (Right now / Capture now) when upgrading from 0.4.3", () => {
    const notice = noticeForUpgrade("0.4.3", "0.4.4");
    expect(notice).not.toBeNull();
    expect(notice?.version).toBe("0.4.4");
    expect(notice?.action).toBe("home");
    expect(notice?.title).toMatch(/prior work/i);
  });
});
