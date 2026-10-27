import { Loader2 } from "lucide-react";
import { HowThisWorks } from "./HowThisWorks";
import { FunctionChecks } from "./HarnessRows";
import { projectLine } from "./harnessFunctions";
import type { AntigravityProject, FunctionRow } from "./harnessFunctions";
import { Tag } from "./ui";
import type { Tone } from "./ui";

/**
 * The Antigravity card (slice G, approved mock 2026-10-27).
 *
 * Two places Khipu can be wired in, kept apart because they behave differently:
 * "On this Mac" is the Antigravity app's shared folder, and "Inside T3, per
 * project" is the three files Khipu adds to each project the owner picks, since
 * T3 keeps its own settings private and Khipu never writes them.
 *
 * Presentational: the panel owns the data and every action, so each button here
 * is one of the owner actions the rest of the page already runs.
 */

export const AG_HOW = {
  title: "How Antigravity connects",
  items: [
    { term: "On this Mac", text: "Install adds Khipu's hooks and memory tools to Antigravity's shared folder. Quit and reopen Antigravity to load them." },
    { term: "Inside T3 Code", text: "T3 keeps its own private settings, which Khipu never touches. Khipu adds three files to each project you pick, and T3's Antigravity reads those." },
    { term: "Trust the folder", text: "If Antigravity asks whether to trust the workspace's hooks, choose Trust." },
  ],
};

/** The card's one-word verdict. Red only for a heartbeat that went red. */
export function antigravityStatus(args: {
  detected: boolean;
  macInstalled: boolean;
  projectsInstalled: number;
  t3Present: boolean;
  lvOk: boolean | undefined;
  recording: boolean;
}): { tone: Tone; label: string } {
  const any = args.macInstalled || args.projectsInstalled > 0;
  if (any && args.lvOk === false) return { tone: "err", label: "Not recording" };
  if (any && args.recording) return { tone: "ok", label: args.macInstalled ? "Recording" : "Recording in T3" };
  if (args.macInstalled) return { tone: "neutral", label: "No sessions yet" };
  if (args.projectsInstalled > 0) return { tone: "warn", label: "In T3 only" };
  if (args.detected) return { tone: "neutral", label: "Not installed" };
  if (args.t3Present) return { tone: "neutral", label: "Not added yet" };
  return { tone: "neutral", label: "Not on this Mac" };
}

export type TrustSwitch = {
  /** The owner-direction entry this switch writes (`antigravity:t3`). */
  on: boolean | null;
  /** A real run through T3 has shown the prompt hook reaches Khipu. */
  proven: boolean;
};

export function AntigravityCard({
  status,
  detected,
  macInstalled,
  macRows,
  t3Visible,
  t3Rows,
  projects,
  trust,
  busy,
  onInstallMac,
  onRemoveMac,
  onAddProject,
  onRemoveProject,
  onVerify,
  onToggleTrust,
  trustError,
}: {
  status: { tone: Tone; label: string };
  detected: boolean;
  macInstalled: boolean;
  macRows: FunctionRow[];
  t3Visible: boolean;
  t3Rows: FunctionRow[];
  projects: AntigravityProject[];
  trust: TrustSwitch;
  /** The action in flight, or null. */
  busy: string | null;
  onInstallMac: () => void;
  onRemoveMac: () => void;
  onAddProject: () => void;
  onRemoveProject: (project: string) => void;
  onVerify: () => void;
  onToggleTrust: (next: boolean) => void;
  trustError: string | null;
}) {
  const idle = busy != null;
  const canVerify = macInstalled || projects.length > 0;
  const switchOn = trust.on === true;
  // Turning it off is always allowed; turning it on waits for the proof.
  const switchDisabled = idle || trust.on == null || (!switchOn && !trust.proven);
  return (
    <div className="hcard ag-card" id="harness-antigravity" tabIndex={-1}>
      <div className="top">
        <span className="name">
          Antigravity <span className="ftag new">new</span>
        </span>
        <Tag tone={status.tone} dot>
          {status.label}
        </Tag>
      </div>

      <div className="ag-sec first">
        <div className="ag-head">On this Mac</div>
        <div className="checks flush">
          <FunctionChecks rows={macRows} />
        </div>
        {detected ? (
          <div className="ag-acts">
            <button type="button" className={`sm${macInstalled ? "" : " primary"}`} disabled={idle} onClick={onInstallMac}>
              {busy === "install:antigravity" ? <Loader2 size={14} className="spin" aria-hidden /> : null}
              {macInstalled ? "Reinstall" : "Install on this Mac"}
            </button>
            {macInstalled ? (
              <button type="button" className="sm link" disabled={idle} onClick={onRemoveMac}>
                Remove from this Mac
              </button>
            ) : null}
          </div>
        ) : null}
      </div>

      {t3Visible ? (
        <div className="ag-sec">
          <div className="ag-head">Inside T3, per project</div>
          <div className="checks flush">
            <FunctionChecks rows={t3Rows} />
          </div>
          {projects.length > 0 ? (
            <div className="ag-projects" role="list" aria-label="Projects Khipu was added to">
              {projects.map((p) => {
                const line = projectLine(p);
                return (
                  <div className="ag-project" role="listitem" key={p.project} title={p.project}>
                    <span className="ag-pname">{line.name}</span>
                    <span className={line.warn ? "ag-pnote warn" : "ag-pnote"}>· {line.note}</span>
                    <button
                      type="button"
                      className="sm link push"
                      aria-label={`Remove Khipu from ${line.name}`}
                      disabled={idle}
                      onClick={() => onRemoveProject(p.project)}
                    >
                      {busy === `remove:${p.project}` ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                      Remove
                    </button>
                  </div>
                );
              })}
            </div>
          ) : null}
          <p className="ag-note">
            Adding a project writes three files in its .agents folder. Antigravity asks you to trust the folder the first time.
          </p>
        </div>
      ) : null}

      {t3Visible ? (
        <div className="ag-sec ag-trust">
          <input
            id="ag-trust-switch"
            className="switch"
            type="checkbox"
            role="switch"
            checked={switchOn}
            disabled={switchDisabled}
            aria-describedby="ag-trust-what"
            onChange={(e) => onToggleTrust(e.target.checked)}
          />
          <div className="opt-text">
            <label className="opt-name" htmlFor="ag-trust-switch">
              Count prompts typed here as your directions
            </label>
            <span id="ag-trust-what" className="ag-note flat">
              {switchOn
                ? "On: what you type to Antigravity inside T3 counts as your word when other agents act."
                : trust.proven
                  ? "Off. Turn it on and what you type to Antigravity inside T3 counts as your word."
                  : "Off until a real run proves it"}
            </span>
            {trustError ? (
              <span className="set-error" role="alert">
                {trustError}
              </span>
            ) : null}
          </div>
        </div>
      ) : null}

      <div className="hacts">
        {t3Visible ? (
          <button
            type="button"
            className={`sm${projects.length === 0 ? " primary" : ""}`}
            disabled={idle}
            onClick={onAddProject}
          >
            {busy === "add:antigravity" ? <Loader2 size={14} className="spin" aria-hidden /> : null}
            Add to a project…
          </button>
        ) : null}
        <button type="button" className="sm" disabled={idle || !canVerify} onClick={onVerify}>
          {busy === "verify:antigravity" ? <Loader2 size={14} className="spin" aria-hidden /> : null}
          Verify
        </button>
        {!detected && !t3Visible ? <span className="meta">Nothing to install here.</span> : null}
      </div>
      <div className="gd-how-row">
        <HowThisWorks title={AG_HOW.title} items={AG_HOW.items} />
      </div>
    </div>
  );
}

