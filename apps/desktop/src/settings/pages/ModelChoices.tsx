import { useId } from "react";
import { ChoiceBody, canonicalSynthProvider, providerTag, synthProviderInfo } from "../../SynthProvider";
import { Tag } from "../../ui";
import type { SettingsCtx } from "../context";
import { MODEL_NAMES, MODEL_SOURCES, modelSelection, selectionPatch, type ModelName } from "../modelSources";

export function ModelChoices({ ctx }: { ctx: SettingsCtx }) {
  const id = useId();
  const row = ctx.synth;
  const { model, source } = modelSelection(row);
  const info = synthProviderInfo(row.provider);
  const tag = info ? providerTag(info, ctx.secretsPresence, ctx.modelCheck.result, ctx.doctorSynth, ctx.modelCheck.runError) : null;
  const sources = MODEL_SOURCES[model];
  return <>
    <div className="st-choice-row"><label htmlFor={`${id}-model`}>Model</label>
      <select id={`${id}-model`} value={model} onChange={(e) => {
        const next = e.target.value as ModelName;
        const nextSource = MODEL_SOURCES[next].includes(source) ? source : MODEL_SOURCES[next][0];
        ctx.onSynthChange(selectionPatch(row, next, nextSource));
      }}>{MODEL_NAMES.map((name) => <option key={name}>{name}</option>)}</select></div>
    <div className="st-choice-row"><div><label htmlFor={`${id}-source`}>Source</label><p className="st-muted">A subscription counts against that plan. An API key is billed by use.</p></div>
      <select id={`${id}-source`} value={source} onChange={(e) => ctx.onSynthChange(selectionPatch(row, model, e.target.value))}>
        {!sources.includes(source) ? <option>{source}</option> : null}
        {sources.map((name) => <option key={name}>{name}</option>)}
      </select></div>
    {tag ? <div className="st-choice-row"><span>Access</span><Tag tone={tag.tone} dot={tag.dot}>{tag.text}</Tag></div> : <p className="st-muted">This version does not list the stored source.</p>}
    <ChoiceBody modelLabel="Model name" info={info} providerId={canonicalSynthProvider(row.provider)} row={row} onChange={ctx.onSynthChange}
      presence={ctx.secretsPresence} verifyResult={ctx.modelCheck.result} doctorSynth={ctx.doctorSynth} runError={ctx.modelCheck.runError} />
  </>;
}
