// Dev preview stand-in for @tauri-apps/plugin-autostart. It only remembers the
// answer in memory: the preview never adds a login item to the Mac.
let on = false;
export async function isEnabled(): Promise<boolean> {
  return on;
}
export async function enable(): Promise<void> {
  on = true;
}
export async function disable(): Promise<void> {
  on = false;
}
