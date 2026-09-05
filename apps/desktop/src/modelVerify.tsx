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
