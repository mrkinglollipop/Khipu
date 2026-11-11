// Times, lengths and the ends a pause can take.
// Moved in slice A11 from RoutingSettings.test.tsx when its screen was removed; these test helpers
// that live code still uses, and need no removed screen.

import { describe, expect, it } from "vitest";
import { chosenEnd, durationWords, endChoices, isoLocal, limitWords, ruleOrigin } from "../routingWords";

const MORNING = new Date("2026-10-14T15:40:00Z");

describe("pause and hold words and choices (from the older routing settings)", () => {
  it("words times, lengths and a rule's origin in local time", () => {
    expect(isoLocal(new Date("2026-10-16T13:00:00Z"))).toBe("2026-10-16T09:00:00-04:00");
    expect(durationWords(120)).toBe("2 minutes");
    expect(durationWords(43200)).toBe("12 hours");
    expect(durationWords(7776000)).toBe("90 days");
    expect(limitWords(1500, "chars")).toBe("1,500 characters");
    expect(limitWords(0, "chars", "No limit")).toBe("No limit");
    expect(ruleOrigin("from your routing table", "2026-10-13", MORNING)).toBe("From your routing table, Oct 13");
  });

  it("offers the ends a pause or a hold can take, and checks a typed one", () => {
    const choices = endChoices(MORNING, 1);
    expect(choices.map((c) => c.label)).toEqual([
      "In 1 hour, 12:40 PM", "Tonight, 9:00 PM", "Tomorrow, 9:00 AM", "Friday, 9:00 AM", "A date and time…",
      "Until I clear it",
    ]);
    expect(chosenEnd("tomorrow", "", choices, MORNING)).toEqual({ iso: "2026-10-15T09:00:00-04:00" });
    expect(chosenEnd("never", "", choices, MORNING)).toEqual({ iso: null });
    expect(chosenEnd("custom", "", choices, MORNING)).toEqual({ error: "Choose the date and time it ends." });
    expect(chosenEnd("custom", "2026-10-01T09:00", choices, MORNING)).toEqual({ error: "Choose a time that hasn't passed yet." });
    expect(chosenEnd("custom", "2026-10-20T08:30", choices, MORNING)).toEqual({ iso: "2026-10-20T08:30:00-04:00" });
  });

});
