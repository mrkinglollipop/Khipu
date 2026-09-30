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

  it("returns the 0.4.5 notice (decision validity) when upgrading from 0.4.4", () => {
    const notice = noticeForUpgrade("0.4.4", "0.4.5");
    expect(notice).not.toBeNull();
    expect(notice?.version).toBe("0.4.5");
    expect(notice?.action).toBe("home");
    expect(notice?.title).toMatch(/reversed/i);
  });

  it("returns the 0.4.7 notice (Optional features in Settings) when upgrading from 0.4.6", () => {
    const notice = noticeForUpgrade("0.4.6", "0.4.7");
    expect(notice).not.toBeNull();
    expect(notice?.version).toBe("0.4.7");
    expect(notice?.action).toBe("settings");
    expect(notice?.title).toMatch(/switches in Settings/i);
  });

  it("shows only the newest notice when several were skipped", () => {
    expect(noticeForUpgrade("0.4.4", "0.4.7")?.version).toBe("0.4.7");
  });

  it("returns null once 0.4.7 was already noticed", () => {
    expect(noticeForUpgrade("0.4.7", "0.4.7")).toBeNull();
  });
});
