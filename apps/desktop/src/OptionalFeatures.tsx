/* Settings -> Optional features (docs/plans/2026-09-30-settings-parity.md).
   One switch per entry in `khipu.features.FEATURES`, in the order the gate
   record recommends them, then the three that were measured and rejected.
   The wording of each cost and each measurement comes from
   docs/research/switch-gates-2026-09-29.md; nothing here is a new number.

   The CLI owns the state. A toggle calls `khipu features --set`, then the
   section re-reads `khipu features`, so a switch the environment decides
   (source "env") is shown locked and can never look changed when it is not. */

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Tag } from "./ui";
import { callCli, useCliState } from "./settingsCli";
import type { ConfigShape, FeaturesShape, JobsShape } from "./settingsCli";
import { SettingField, unitRangeError } from "./SettingsControls";

type FeatureRow = {
  name: string;
  label: string;
  what: string;
  /** What it costs, where it costs anything. */
  cost?: string;
  /** What the gate found. Shown only for the measured-and-rejected group. */
  found?: string;
  /** A switch this one depends on. */
  needs?: { name: string; label: string };
};

export const RECOMMENDED_FEATURES: ReadonlyArray<FeatureRow> = [
  {
    name: "validity_ranking",
    label: "Rank current decisions above reversed ones",
    what: "Recall tells a decision that still stands from one that was later reversed, and ranks the reversed one lower.",
  },
  {
    name: "time_interpretation",
    label: "Understand time phrases in a search",
    what: "A phrase such as “last week” in a search narrows and ranks the results by date.",
  },
  {
    name: "relevance_floor",
    label: "Return nothing when nothing matches",
    what: "When the best matches are not close to what you asked, search comes back empty instead of showing the nearest unrelated notes.",
  },
  {
    name: "rerank",
    label: "Reorder top results with a model",
    what: "A model reads the top search results and puts the best match first.",
    cost: "Adds about 0.8 s to a search and makes one model call per search.",
  },
  {
    name: "reflect",
    label: "Answer questions from memory, with sources",
    what: "Ask a question and get an answer in which every claim points to a memory it came from.",
    cost: "One model call per question, and only when you ask.",
  },
  {
    name: "briefs",
    label: "Build a sourced summary page per topic",
    what: "The nightly job writes a short brief for each topic, and each claim in it cites its source.",
    cost: "Model calls in the nightly job, up to 40 topics a night.",
  },
];

export const REJECTED_FEATURES: ReadonlyArray<FeatureRow> = [
  {
    name: "graph_candidates",
    label: "Add results found through the graph",
    what: "Search also offers results reached by following links in the knowledge graph.",
    found: "A blind judge preferred the results without it 59 to 4, with 18 ties.",
  },
  {
    name: "decision_details",
    label: "Record a reason and a time for each decision",
    what: "When a session is captured, each decision also records why it was made and when it took effect.",
    found:
      "With it on, 1 transcript in 10 lost all its decisions. Decisions per run went from 4.6 to 4.3.",
  },
  {
    name: "auto_supersede",
    label: "Mark a decision replaced without asking",
    what: "When a new decision reverses an earlier one, the earlier one is marked replaced automatically.",
    found: "It relies on the switch above, so it carries the same risk: with that one on, 1 transcript in 10 lost all its decisions.",
    needs: { name: "decision_details", label: "“Record a reason and a time for each decision”" },
  },
];

const RECALL_JOB = "recall_daemon";

type RecallStatus = { ok?: boolean; running?: boolean; served?: number; pid?: number };

export function envName(feature: string): string {
  return `KHIPU_FEATURE_${feature.toUpperCase()}`;
}

export function OptionalFeatures({ active }: { active: boolean }) {
  const features = useCliState<FeaturesShape>("khipu_features_show", active);
  const config = useCliState<ConfigShape>("khipu_config_show", active);
  const jobs = useCliState<JobsShape>("khipu_jobs_status", active);
  const [recall, setRecall] = useState<RecallStatus | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [recallBusy, setRecallBusy] = useState(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const readRecall = useCallback(async (): Promise<boolean> => {
    const res = await callCli<RecallStatus>("khipu_recall_status");
    const running = res.ok && res.data.ok !== false;
    if (mounted.current) setRecall(res.ok ? res.data : { ok: false, running: false });
    return running;
  }, []);

  useEffect(() => {
    if (active) void readRecall();
  }, [active, readRecall]);

  async function toggle(name: string, next: boolean) {
    setBusy(name);
    const res = await callCli("khipu_feature_set", { name, enabled: next });
    await features.reload();
    setErrors((prev) => {
      const out = { ...prev };
      if (res.ok) delete out[name];
      else out[name] = res.error;
      return out;
    });
    setBusy(null);
  }

  async function toggleRecall(next: boolean) {
    setRecallBusy(true);
    const res = await callCli(next ? "khipu_job_install" : "khipu_job_uninstall", { name: RECALL_JOB });
    await jobs.reload();
    setErrors((prev) => {
      const out = { ...prev };
      if (res.ok) delete out[RECALL_JOB];
      else out[RECALL_JOB] = res.error;
      return out;
    });
    let running = await readRecall();
    // The service needs a moment to open its socket after it is loaded.
    for (let i = 0; next && res.ok && !running && i < 3 && mounted.current; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      running = await readRecall();
    }
    if (mounted.current) setRecallBusy(false);
  }

  const installed = jobs.data?.[RECALL_JOB]?.plist_loaded === true;
  const running = recall?.ok === true;
  const floor = config.data?.relevance_cosine_floor;

  function renderRow(row: FeatureRow, group: "recommended" | "rejected") {
    const state = features.data?.features?.[row.name];
    const locked = state?.source === "env";
    const on = state?.enabled === true;
    const id = `feat-${row.name}`;
    const needsOff = row.needs != null && features.data?.features?.[row.needs.name]?.enabled !== true;
    return (
      <div key={row.name} className={`opt-row${group === "rejected" ? " rejected" : ""}`}>
        <input
          id={id}
          className="switch"
          type="checkbox"
          role="switch"
          checked={on}
          disabled={!state || locked || busy !== null}
          aria-describedby={`${id}-what`}
          onChange={(e) => {
            if (locked) return;
            void toggle(row.name, e.target.checked);
          }}
        />
        <div className="opt-text">
          <div className="opt-title">
            <label className="opt-name" htmlFor={id}>
              {row.label}
            </label>
            {busy === row.name ? <Loader2 size={14} className="spin" aria-hidden /> : null}
            {locked ? <Tag tone="neutral">Locked</Tag> : null}
          </div>
          <p id={`${id}-what`} className="opt-what">
            {row.what}
          </p>
          {row.cost ? (
            <p className="opt-cost">
              <b>Cost.</b> {row.cost}
            </p>
          ) : null}
          {row.found ? (
            <p className="opt-found">
              <b>Measured.</b> {row.found}
            </p>
          ) : null}
          {row.needs ? (
            <p className="opt-cost">
              <b>Needs.</b> {row.needs.label}
              {needsOff ? ", which is off" : ""}.
            </p>
          ) : null}
          {locked ? (
            <p className="opt-cost">
              The environment variable <code>{envName(row.name)}</code> decides this switch. Unset it to change it
              here.
            </p>
          ) : null}
          {row.name === "relevance_floor" ? (
            <SettingField
              label="Lowest match score that counts"
              mono
              narrow
              inputMode="decimal"
              value={floor ? String(floor.value) : ""}
              canReset={floor?.source === "file"}
              resetLabel="Use default"
              help={
                <>
                  Used only while this switch is on. On the current index, unrelated subjects scored 0.57 to 0.64
                  at best and real prompts scored 0.67 to 0.82. The default is {floor ? String(floor.default) : "0.65"}.
                </>
              }
              onSave={async (v) => {
                const bad = unitRangeError(v, false);
                if (bad) return bad;
                const res = await callCli("khipu_config_set", { key: "relevance.cosine_floor", value: v });
                await config.reload();
                return res.ok ? null : res.error;
              }}
              onReset={async () => {
                const res = await callCli("khipu_config_unset", { key: "relevance.cosine_floor" });
                await config.reload();
                return res.ok ? null : res.error;
              }}
            />
          ) : null}
          {errors[row.name] ? (
            <p className="set-error" role="alert">
              {errors[row.name]}
            </p>
          ) : null}
        </div>
      </div>
    );
  }

  return (
    <>
      <p className="set-help set-intro">
        Each switch is saved on this Mac. A switch that an environment variable decides is locked and names the
        variable.
      </p>
      {features.error && !features.data ? (
        <p className="set-error" role="alert">
          Could not read the switches: {features.error}
        </p>
      ) : null}

      <div className="section-card">
        <div className="section-head">Search and memory</div>
        <div className="opt-list">{RECOMMENDED_FEATURES.map((r) => renderRow(r, "recommended"))}</div>
      </div>

      <div className="section-card">
        <div className="section-head">Local recall service</div>
        <div className="opt-list">
          <div className="opt-row">
            <input
              id="feat-recall-service"
              className="switch"
              type="checkbox"
              role="switch"
              checked={installed}
              disabled={!jobs.data || recallBusy}
              aria-describedby="feat-recall-service-what"
              onChange={(e) => void toggleRecall(e.target.checked)}
            />
            <div className="opt-text">
              <div className="opt-title">
                <label className="opt-name" htmlFor="feat-recall-service">
                  Keep recall warm on this Mac
                </label>
                {recallBusy ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                {installed ? (
                  running ? (
                    <Tag tone="ok" dot>
                      Running
                    </Tag>
                  ) : (
                    <Tag tone="warn" dot>
                      Not running
                    </Tag>
                  )
                ) : null}
              </div>
              <p id="feat-recall-service-what" className="opt-what">
                A small background process answers the recall lookup before each prompt, so a prompt no longer starts
                a new interpreter or reloads the vectors. Without it, recall still works through the slower one-shot
                path. In one measurement, the prompt hook took a median of 418 ms with the service and 701 ms without it.
              </p>
              {running && typeof recall?.served === "number" ? (
                <p className="opt-cost">It has answered {recall.served} lookups since it started.</p>
              ) : null}
              {errors[RECALL_JOB] ? (
                <p className="set-error" role="alert">
                  {errors[RECALL_JOB]}
                </p>
              ) : null}
            </div>
          </div>
        </div>
      </div>

      <div className="section-card rejected">
        <div className="section-head">Measured, not recommended</div>
        <p className="opt-group-note">
          Each of these was tested before release and stays off by default. They remain here for anyone who wants to
          try them.
        </p>
        <div className="opt-list">{REJECTED_FEATURES.map((r) => renderRow(r, "rejected"))}</div>
      </div>
    </>
  );
}
