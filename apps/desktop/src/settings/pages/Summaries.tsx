import { SummariserCalls, SynthFallbackField } from "../../SynthProvider";
import { KeysCard } from "../../KeysCard";
import { Callout } from "../../ui";
import type { SettingsCtx } from "../context";
import { ModelChoices } from "./ModelChoices";
import { SearchByMeaningPage } from "./SearchByMeaning";

export function SummariesPage({ ctx, active }: { ctx: SettingsCtx; active: boolean }) {
  return <>
    <section className="st-regroup" aria-label="Summaries"><h2>Summaries</h2>
      <p className="st-muted" id="synth-help">Writes the short summary of each conversation.</p>
      <div className="st-card">
        {ctx.modelsError ? <Callout tone="err" title="The stored models setting could not be read">
          {ctx.modelsError}. Showing defaults; saving is blocked until it is fixed or cleared.
        </Callout> : null}
        <ModelChoices ctx={ctx} />
        <SynthFallbackField provider={ctx.synth.provider} doctorSynth={ctx.doctorSynth} presence={ctx.secretsPresence}
          verifyResult={ctx.modelCheck.result} onSaved={ctx.onKeySaved} />
        <SummariserCalls active={active} />
      </div>
    </section>
    <section className="st-regroup" aria-label="Search by meaning"><h2>Search by meaning</h2>
      <p className="st-muted">Matches what you meant, not only the words you typed.</p>
      <div className="st-card"><SearchByMeaningPage active={active} modelDetails /></div>
    </section>
    <section className="st-regroup" aria-label="API keys"><h2>API keys</h2>
      <KeysCard active={active} presence={ctx.secretsPresence} presenceError={ctx.secretsPresenceMsg}
        verify={ctx.modelCheck} onKeySaved={ctx.onKeySaved} />
    </section>
    <div className="st-actions st-save-models"><button type="button" className="primary" disabled={ctx.modelsSaving || ctx.modelsError != null} onClick={() => void ctx.onSaveModels()}>
      {ctx.modelsSaving ? "Saving…" : "Save models"}
    </button></div>
    {ctx.modelsMsg ? <p className="st-muted" role="status">{ctx.modelsMsg}</p> : null}
  </>;
}
