import { SummariserCalls, SynthFallbackField, SynthProviderChoices } from "../../SynthProvider";
import { KeysCard } from "../../KeysCard";
import { Callout } from "../../ui";
import type { SettingsCtx } from "../context";

/** The old "Session summaries" card and the keys card, with the same calls.
 *  Keys are only ever shown as set or not set. */
export function SummariesPage({ ctx, active }: { ctx: SettingsCtx; active: boolean }) {
  return (
    <>
      <p className="st-muted" id="synth-help">Which model writes the short summary of each conversation.</p>
      {ctx.modelsError ? (
        <Callout tone="err" title="The stored models setting could not be read">
          {ctx.modelsError}. Showing defaults; saving is blocked until it is fixed or cleared.
        </Callout>
      ) : null}
      <SynthProviderChoices
        row={ctx.synth}
        onChange={ctx.onSynthChange}
        presence={ctx.secretsPresence}
        verifyResult={ctx.modelCheck.result}
        runError={ctx.modelCheck.runError}
        doctorSynth={ctx.doctorSynth}
      />
      <div className="st-actions">
        <button type="button" className="primary" disabled={ctx.modelsSaving || ctx.modelsError != null} onClick={() => void ctx.onSaveModels()}>
          {ctx.modelsSaving ? "Saving…" : "Save models"}
        </button>
      </div>
      {ctx.modelsMsg ? <p className="st-muted" role="status">{ctx.modelsMsg}</p> : null}
      <SynthFallbackField
        provider={ctx.synth.provider}
        doctorSynth={ctx.doctorSynth}
        presence={ctx.secretsPresence}
        verifyResult={ctx.modelCheck.result}
        onSaved={ctx.onKeySaved}
      />
      <KeysCard
        active={active}
        presence={ctx.secretsPresence}
        presenceError={ctx.secretsPresenceMsg}
        verify={ctx.modelCheck}
        onKeySaved={ctx.onKeySaved}
      />
      <section className="st-group" aria-label="Summaries written today">
        <SummariserCalls active={active} />
      </section>
    </>
  );
}
