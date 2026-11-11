// Going to a place in the window (Today, Accounts, Memory, Settings) and, in
// Settings, to one of its rows. The older screens are gone (slice A11).
import { fireEvent, screen, within } from "@testing-library/react";

export async function openScreen(place: string | RegExp): Promise<HTMLElement> {
  const main = await screen.findByRole("navigation", { name: "Main" });
  const button = await within(main).findByRole("button", { name: place });
  fireEvent.click(button);
  return button;
}

/** Settings, then the row whose label starts with `row` ("Your Macs", "Apps that use Khipu", …). */
export async function openSettingsRow(row: string): Promise<HTMLElement> {
  await openScreen("Settings");
  const list = await screen.findByRole("region", { name: "Settings" }).catch(() => null);
  const scope = list ?? document.body;
  const esc = row.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const button = await within(scope as HTMLElement).findByRole("button", { name: new RegExp(`^${esc}`) });
  fireEvent.click(button);
  return button;
}
