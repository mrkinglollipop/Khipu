// Forgetting one conversation: the app's existing call and its checks, moved
// here unchanged so the Memory place and the old screens do the same thing.
// `khipu episode forget ID` soft-deletes the row; the answer must say so
// (`soft_deleted: true`), and a copy left behind on this Mac is reported, not
// hidden. This is the only destructive action in Memory, and it is called
// only from ForgetDialog after the owner has typed "forget".
import type { Run } from "./model";

export type ForgetResult = { ok: true; warning: string | null } | { ok: false; error: string };

function parseJson(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

type Verdict = { ok?: unknown; error?: unknown; reason?: unknown } | null | undefined;

function failureOf(v: Verdict): string {
  if (typeof v?.error === "string") return v.error;
  if (typeof v?.reason === "string") return v.reason;
  return "missing success verdict";
}

export async function forgetConversation(run: Run, id: number): Promise<ForgetResult> {
  let raw: string;
  try {
    raw = await run(["episode", "forget", String(id)]);
  } catch (e) {
    return { ok: false, error: String(e) };
  }
  const parsed = parseJson(raw) as {
    soft_deleted?: unknown;
    partial?: unknown;
    error?: unknown;
    snapshot?: Verdict;
    legacy_file?: Verdict;
  } | null;
  if (!parsed || typeof parsed !== "object" || parsed.soft_deleted !== true) {
    return {
      ok: false,
      error: typeof parsed?.error === "string" ? parsed.error : "Could not check that the conversation was forgotten.",
    };
  }
  let leftover: string | null = null;
  const remaining = Array.isArray(parsed.partial) ? parsed.partial.filter((s): s is string => typeof s === "string") : [];
  if (!parsed.snapshot || parsed.snapshot.ok !== true) {
    leftover = failureOf(parsed.snapshot);
  } else if (
    parsed.legacy_file?.ok === false ||
    (typeof parsed.legacy_file?.reason === "string" && parsed.legacy_file.reason.startsWith("unreadable:"))
  ) {
    leftover = failureOf(parsed.legacy_file);
  }
  if (remaining.length > 0) leftover = `remaining stores: ${remaining.join(", ")}`;
  return {
    ok: true,
    warning: leftover ? `Forgotten, but a copy kept on this Mac could not be removed: ${leftover}` : null,
  };
}
