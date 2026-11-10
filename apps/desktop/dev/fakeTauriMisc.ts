// The other Tauri modules the full app imports (app version, updater,
// relaunch), stubbed so the dev preview can mount <App />.
export async function getVersion(): Promise<string> {
  return "0.4.15";
}
export async function check(): Promise<null> {
  return null;
}
export async function relaunch(): Promise<void> {}
export async function resourceDir(): Promise<string> {
  return "/Applications/Khipu.app/Contents/Resources";
}
