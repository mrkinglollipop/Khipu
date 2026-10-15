// Settings clarity phase 1 (docs/plans/2026-10-15-settings-clarity.md, 6c):
// no age is ever negative, and a commitment's age is counted on the hub's
// clock, not by subtracting the hub's timestamp from this Mac's clock.
import { describe, expect, it } from "vitest";
import { fmtAge, fmtAgo, fmtAgoFromRead, fmtAgoSince } from "../time";

const MAC_NOW = Date.parse("2026-10-15T12:00:00Z");

describe("ages", () => {
  it("reads a timestamp 40 s in the future as just now, never negative", () => {
    expect(fmtAgoSince("2026-10-15T12:00:40Z", MAC_NOW)).toBe("just now");
    expect(fmtAgo(-40)).toBe("just now");
    expect(fmtAge(-40)).toBe("0s");
    expect(fmtAgoSince("2026-10-15T11:59:15Z", MAC_NOW)).toBe("45s ago");
    expect(fmtAgoSince("not a time", MAC_NOW)).toBeNull();
  });

  it("ages a commitment from the hub's age at the read plus the time since this Mac read it", () => {
    // The hub's clock runs 3 minutes ahead of this Mac: it stamped the row
    // at 12:02 Mac time and, at the read, said it was 40 s old.
    const readAt = MAC_NOW - 20_000;
    expect(fmtAgoFromRead(40, readAt, "2026-10-15T12:02:00Z", MAC_NOW)).toBe("60s ago");
    // The same row aged by the Mac's clock alone would read "just now".
    expect(fmtAgoSince("2026-10-15T12:02:00Z", MAC_NOW)).toBe("just now");
    // A Mac clock that went backwards since the read adds nothing.
    expect(fmtAgoFromRead(40, MAC_NOW + 5_000, null, MAC_NOW)).toBe("40s ago");
    // A hub too old to send ages falls back to this Mac's clock.
    expect(fmtAgoFromRead(null, readAt, "2026-10-15T11:58:00Z", MAC_NOW)).toBe("2 min ago");
  });
});
