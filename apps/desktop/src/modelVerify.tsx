import { useCallback, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Callout } from "./ui";

// Shared model-key verify presentation (docs/plans/2026-09-05-setup-that-
// cannot-strand-you.md, "A model key is proven on save with one real call").
// Both the Welcome Model step and Settings › Capture & models call the
// `khipu_secrets_verify` fixed-argv command (`khipu secrets verify --json`,
// `khipu.modelcheck.check_model_keys`) right after a key save and render its
// result the same way, so this is the one place that shape becomes a row.

/** `khipu secrets verify --json` — one real, cheap call per configured
 *  provider. `title` is already "Key works · <model>" on success; `detail`
 *  is the plain-words failure on a miss (empty on success). Never carries
 *  the key itself. */
export type ModelCheck = {
  id: string;
  ok: boolean;
  title: string;
  detail: string;
  model: string | null;
  seconds: number;
  fix?: string;
};
export type ModelVerifyResult = { ok: boolean; checks: ModelCheck[] };

export function modelCheckFor(result: ModelVerifyResult | null, id: string): ModelCheck | undefined {
  return result?.checks.find((c) => c.id === id);
}

// The last check is kept on this Mac so Settings can say "checked 2 min ago"
// and run the check on open only for a key that has never been checked. It
// holds titles and plain-words details, never a key.
const VERIFY_STORE = "khipu.modelVerify.v1";

function readStoredVerify(): { result: ModelVerifyResult; at: number } | null {
  try {
    const raw = localStorage.getItem(VERIFY_STORE);
    if (!raw) return null;
    const v = JSON.parse(raw) as { result?: ModelVerifyResult; at?: number };
    if (!v || typeof v.at !== "number" || !Array.isArray(v.result?.checks)) return null;
    return { result: v.result as ModelVerifyResult, at: v.at };
  } catch {
    return null;
  }
}

export function useModelVerify() {
  const [stored] = useState(readStoredVerify);
  const [result, setResult] = useState<ModelVerifyResult | null>(stored?.result ?? null);
  const [checkedAt, setCheckedAt] = useState<number | null>(stored?.at ?? null);
  const [verifying, setVerifying] = useState(false);
  // Set when a run could not execute at all; such a run is never stored as a check.
  const [runError, setRunError] = useState<string | null>(null);
  const latest = useRef(0);

  /** One cheap, real API call per configured provider
   *  (`khipu.modelcheck.check_model_keys` via the `khipu_secrets_verify`
   *  fixed-argv command), never the key itself. */
  const verify = useCallback(async () => {
    // Only the newest run may report; an older one finishing late is dropped.
    const run = ++latest.current;
    setVerifying(true);
    let out: ModelVerifyResult | null = null;
    let failure: string | null = null;
    try {
      const raw = await invoke<string>("khipu_secrets_verify");
      try {
        out = JSON.parse(raw) as ModelVerifyResult;
      } catch {
        failure = "the answer could not be read";
      }
    } catch (e) {
      failure = String(e);
    }
    if (run !== latest.current) return;
    if (!out || !Array.isArray(out.checks)) {
      setRunError(failure ?? "the answer could not be read");
      setVerifying(false);
      return;
    }
    const at = Date.now();
    setRunError(null);
    setResult(out);
    setCheckedAt(at);
    setVerifying(false);
    try {
      if (out && Array.isArray(out.checks)) localStorage.setItem(VERIFY_STORE, JSON.stringify({ result: out, at }));
    } catch {
      // Storage can be unavailable; the check still shows for this session.
    }
  }, []);

  /** A key was replaced: its earlier results no longer describe it. Drop them
   *  from state and from the copy kept on this Mac. */
  const forget = useCallback((ids: readonly string[]) => {
    setResult((prev) => {
      if (!prev) return prev;
      const next = { ...prev, checks: prev.checks.filter((c) => !ids.includes(c.id)) };
      next.ok = next.checks.every((c) => c.ok);
      try {
        const raw = localStorage.getItem(VERIFY_STORE);
        const at = raw ? (JSON.parse(raw) as { at?: number }).at : undefined;
        localStorage.setItem(VERIFY_STORE, JSON.stringify({ result: next, at: typeof at === "number" ? at : Date.now() }));
      } catch {
        // Storage can be unavailable; the state above is still right.
      }
      return next;
    });
  }, []);

  return { result, checkedAt, verifying, verify, runError, forget };
}

/** "Not checked" (`modelcheck._skip`) means no key is configured for this
 *  provider yet — nothing to show here; the radio/section above already
 *  says so. Anything else is a real, just-run verdict. */
export function ModelCheckRow({
  check,
  verifying,
  onRetry,
}: {
  check: ModelCheck | undefined;
  verifying: boolean;
  onRetry: () => void;
}) {
  if (!check || check.title === "Not checked") return null;
  return (
    <Callout
      tone={check.ok ? "ok" : "err"}
      title={check.title}
      action={
        check.ok ? undefined : (
          <button type="button" disabled={verifying} onClick={onRetry}>
            {verifying ? "Checking…" : "Check again"}
          </button>
        )
      }
    >
      {check.detail || null}
    </Callout>
  );
}
