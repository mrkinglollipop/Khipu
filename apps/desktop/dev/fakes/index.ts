// The dev preview's canned answers, one file per place. A slice that builds
// a place adds its own file here (fakes/today.ts, fakes/accounts.ts, ...) and
// one line to FAKE_HANDLERS below, so parallel slices never edit one big file.
//
// A handler gets the command name, its arguments and the page's `__khipuFake`
// switches (set from a dev state in main.tsx). It returns the answer shaped as
// the CLI prints it, or `undefined` to pass. The first handler that answers
// wins. Answers are sent as JSON text, like the real `invoke`; wrap one in
// `raw()` for a command whose real answer is a bare value (e.g. `true`).
import { shellFakes } from "./shell";
import { projectsFakes } from "./projects";

export type FakeSwitches = Record<string, unknown>;
export type FakeHandler = (cmd: string, args: Record<string, unknown> | undefined, flags: FakeSwitches) => unknown | undefined;

export { RAW, raw, isRaw } from "./raw";
export type { Raw } from "./raw";

export const FAKE_HANDLERS: FakeHandler[] = [shellFakes, projectsFakes];

export function fakeAnswer(cmd: string, args: Record<string, unknown> | undefined, flags: FakeSwitches): unknown | undefined {
  for (const handler of FAKE_HANDLERS) {
    const answer = handler(cmd, args, flags);
    if (answer !== undefined) return answer;
  }
  return undefined;
}
