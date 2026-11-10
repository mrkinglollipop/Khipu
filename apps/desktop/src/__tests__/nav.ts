// Going to a screen in the new window. Tests that used the old rail names
// ("Home", "Recall", "Accounts and routing", ...) say the old name; this finds
// the button now carrying it, opening "Older screens" first when the screen
// has moved there. Slice A11 deletes the older screens and these names with them.
import { fireEvent, screen, within } from "@testing-library/react";

const PLACES: Record<string, string> = { Recall: "Memory", "Accounts and routing": "Accounts", Settings: "Settings" };
const OLDER = ["Home", "Work", "Activity", "Harnesses", "Guides", "Conflicting edits"];

export async function openScreen(old: string | RegExp): Promise<HTMLElement> {
  const main = await screen.findByRole("navigation", { name: "Main" });
  const label = typeof old === "string" ? old : OLDER.find((o) => old.test(o));
  if (label && OLDER.includes(label)) {
    const toggle = within(main).getByRole("button", { name: "Older screens" });
    if (toggle.getAttribute("aria-expanded") !== "true") fireEvent.click(toggle);
    const button = await within(main).findByRole("button", { name: old });
    fireEvent.click(button);
    return button;
  }
  const button = await within(main).findByRole("button", { name: typeof old === "string" ? PLACES[old] ?? old : old });
  fireEvent.click(button);
  return button;
}
