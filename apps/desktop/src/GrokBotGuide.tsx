import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { BookOpen, Check, CircleDashed, Clock, Copy, ExternalLink, Info, Loader2, Minus, TriangleAlert } from "lucide-react";
import { GatewayUrlCard, GatewayTokenCard } from "./SettingsControls";
import { callCli } from "./settingsCli";
import { gatewayCheckWords, runGatewayCheck } from "./guideSignals";
import type { useGuideSignals } from "./guideSignals";
import type { GuideId } from "./guidesModel";
import { fmtAgo } from "./time";

const CURSOR_SECRETS_URL = "https://cursor.com/dashboard/cloud-agents";
const CURSOR_AGENTS_URL = "https://cursor.com/agents";
const SECRET_NAME = "KHIPU_GATEWAY_TOKEN";
const TEST_QUESTION = "Call khipu_status and tell me what it says.";

type Signals = ReturnType<typeof useGuideSignals>;

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** `integrations install grok_bot` with no folder only prints what to paste
 *  (`account_level.config`); `--dry-run` makes sure nothing is written. The
 *  text holds `${env:…}`, never the key. */
export async function readServerSettings(
  runKhipu: (args: string[]) => Promise<string>,
): Promise<{ text: string | null; error: string | null }> {
  try {
    const rows = JSON.parse(await runKhipu(["integrations", "install", "grok_bot", "--dry-run", "--no-verify"])) as Array<{
      account_level?: { config?: unknown };
    }>;
    const config = Array.isArray(rows) ? rows[0]?.account_level?.config : undefined;
    if (config == null) return { text: null, error: null };
    return { text: JSON.stringify(config, null, 2), error: null };
  } catch (e) {
    return { text: null, error: String(e).slice(0, 160) };
  }
}

function CopyButton({ text, label = "Copy", aria }: { text: string; label?: string; aria?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="link"
      aria-label={aria}
      onClick={async () => {
        setDone(await copyText(text));
        window.setTimeout(() => setDone(false), 2500);
      }}
    >
      <Copy size={13} aria-hidden /> {done ? "Copied" : label}
    </button>
  );
}

function Step({
  n,
  title,
  state,
  where,
  children,
}: {
  n: number;
  title: string;
  state: "done" | "current" | "todo";
  where?: ReactNode;
  children: ReactNode;
}) {
  return (
    <li className={`gd-step ${state}`} id={`gd-s${n}`} aria-current={state === "current" ? "step" : undefined}>
      <span className="gd-num-dot" aria-hidden>
        {state === "done" ? <Check size={13} /> : n}
      </span>
      <div>
        <h5>
          {title}
          {state === "done" ? <span className="sr-only"> (done)</span> : null}
        </h5>
        {where ? <div className="gd-where">{where}</div> : null}
        {children}
      </div>
    </li>
  );
}

function Success({ done, children, busy = false }: { done: boolean; children: ReactNode; busy?: boolean }) {
  return (
    <div className={done ? "gd-success" : "gd-success pending"} role="status">
      {busy ? <Loader2 size={14} className="spin" aria-hidden /> : done ? <Check size={14} aria-hidden /> : <CircleDashed size={14} aria-hidden />}
      <span>{children}</span>
    </div>
  );
}

export function GrokBotGuide({
  runKhipu,
  signals,
  startStep,
  onOpenGuide,
  onOpenHarnesses,
}: {
  runKhipu: (args: string[]) => Promise<string>;
  signals: Signals;
  /** Open with this step in focus (the Harnesses card's "Set up…" passes the
   *  first unfinished one). */
  startStep?: number;
  onOpenGuide: (id: GuideId) => void;
  onOpenHarnesses: () => void;
}) {
  const { grok, prefs, updatePrefs, gatewayUrl } = signals;
  // Until the user picks a step, focus follows the first one still open, so a
  // step that just finished hands focus to the next without a click.
  const [pickedStep, setFocusStep] = useState<number | null>(startStep ?? null);
  const focusStep = pickedStep ?? grok.current;
  const [copyNote, setCopyNote] = useState<{ ok: boolean; text: string } | null>(null);
  const [checking, setChecking] = useState(false);
  const [settings, setSettings] = useState<{ text: string | null; error: string | null } | null>(null);
  const lastStart = useRef(startStep);

  useEffect(() => {
    if (startStep != null && startStep !== lastStart.current) {
      lastStart.current = startStep;
      setFocusStep(startStep);
    }
  }, [startStep]);

  // "Set up…" pins a step; once that step finishes, follow live progress again.
  useEffect(() => {
    if (pickedStep == null) return;
    const s = grok.steps[pickedStep - 1];
    if (s && (s.done || s.acknowledged)) setFocusStep(null);
  }, [pickedStep, grok.steps]);

  // Cursor's pages are outside Khipu. When the window comes back into focus,
  // read Khipu's own state again so a finished step shows without a click.
  const { reload } = signals;
  useEffect(() => {
    const onFocus = () => void reload();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [reload]);

  // The server settings text depends on the saved address.
  useEffect(() => {
    let live = true;
    void readServerSettings(runKhipu).then((r) => live && setSettings(r));
    return () => {
      live = false;
    };
  }, [runKhipu, gatewayUrl]);

  const stateOf = useCallback(
    (n: 1 | 2 | 3 | 4 | 5): "done" | "current" | "todo" => {
      const s = grok.steps[n - 1];
      if (s.done) return "done";
      return focusStep === n ? "current" : "todo";
    },
    [grok.steps, focusStep],
  );

  const copyKey = async () => {
    const res = await callCli("khipu_gateway_token_copy");
    setCopyNote(
      res.ok
        ? { ok: true, text: "Copied. Khipu clears it from your clipboard in 60 seconds." }
        : { ok: false, text: res.error },
    );
  };

  const check = async () => {
    setChecking(true);
    try {
      const result = await runGatewayCheck(runKhipu);
      updatePrefs({ grokCheck: { ...result, address: gatewayUrl.trim() } });
    } finally {
      setChecking(false);
    }
  };

  const afterSettingSaved = () => {
    // Address or key changed: any prior gateway check is about the old setup.
    updatePrefs({ grokCheck: null });
    void reload();
  };

  const ack = (step: 3 | 4) => {
    updatePrefs({ grokAck: Math.max(prefs.grokAck, step) as 3 | 4 });
    setFocusStep(step + 1);
    window.requestAnimationFrame(() => document.getElementById(`gd-s${step + 1}`)?.scrollIntoView?.({ block: "nearest" }));
  };

  const liveCheck = prefs.grokCheck?.address === gatewayUrl.trim() ? prefs.grokCheck : null;
  const words = gatewayCheckWords(liveCheck);
  const checkedAgo = liveCheck ? fmtAgo(Math.max(0, (Date.now() - liveCheck.at) / 1000)) : "";

  return (
    <div className="gd-guide">
      <article>
        <h2 className="gd-title">Use Khipu with Grok Bot</h2>
        <p className="gd-lede">
          Grok Bot is the Grok agent that runs in Cursor's cloud. It can't see this Mac, so it reaches your memory over
          the internet, through your Khipu gateway. Once it's connected, it can search what your other agents remember
          and save what it learns.
        </p>
        <div className="gd-facts">
          <span><Clock size={14} aria-hidden /> About 10 minutes</span>
          <span><Info size={14} aria-hidden /> You need a Cursor account with cloud agents, and a gateway address</span>
          <span className={`tag ${grok.finished ? "ok" : "warn"}`}>
            <span className="dot" aria-hidden />
            <span className="gd-num">{grok.passed} of 5 steps done</span>
          </span>
        </div>

        <ol className="gd-steps">
          <Step n={1} title="Have a gateway address" state={stateOf(1)}>
            <p>The gateway is the one part of Khipu that's open to the internet. Cloud agents talk to it; your database stays private.</p>
            <GatewayUrlCard active onSaved={afterSettingSaved} />
            <Success done={grok.steps[0].done}>
              {grok.steps[0].done ? (
                <>
                  <b>An https address is saved.</b> It's the same setting as Settings → Another Mac → Gateway address.
                  {liveCheck?.ok ? ` The gateway answered this Mac ${checkedAgo}.` : " Step 5 checks that it answers."}
                </>
              ) : (
                <>
                  <b>Success looks like:</b> an address starting with https:// is saved here.
                </>
              )}
            </Success>
            <div className="gd-callout info">
              <Info size={14} aria-hidden />
              <span>
                No gateway yet? It runs on a server you control, and setting one up happens outside Khipu today.{" "}
                <button type="button" className="link" onClick={() => onOpenGuide("gateway")}>
                  How to run a gateway
                </button>
              </span>
            </div>
          </Step>

          <Step n={2} title="Copy your gateway key" state={stateOf(2)}>
            <p>Grok Bot proves it's you with a secret key. Copy it here and paste it into Cursor in the next step.</p>
            <div className="gd-panel">
              <div className="gd-row">
                <button type="button" onClick={() => void copyKey()}>
                  <Copy size={14} aria-hidden /> Copy gateway key
                </button>
                {copyNote ? (
                  <span className={copyNote.ok ? "meta" : "gd-bad"} role="status">{copyNote.text}</span>
                ) : null}
              </div>
              {grok.steps[1].done ? null : (
                <>
                  <p className="meta gd-tight">No key is saved on this Mac yet. Paste the one from your gateway once; Khipu keeps it and never shows it again.</p>
                  <GatewayTokenCard active onSaved={afterSettingSaved} />
                </>
              )}
            </div>
            <Success done={grok.steps[1].done}>
              {grok.steps[1].done ? (
                <><b>A gateway key is saved on this Mac.</b> Khipu never displays it.</>
              ) : (
                <><b>Success looks like:</b> the key is saved on this Mac.</>
              )}
            </Success>
            <div className="gd-callout warn">
              <TriangleAlert size={14} aria-hidden />
              <span>Anyone with this key can read your memory. Paste it only into Cursor's Secrets, never into a chat or a file in a repo.</span>
            </div>
          </Step>

          <Step n={3} title="Add the key to Cursor as a secret" state={stateOf(3)} where={<>In Cursor's website: <b>Dashboard → Cloud Agents → Secrets → Add secret</b></>}>
            <div className="gd-panel">
              <div className="gd-row">
                <a className="gd-btn primary" href={CURSOR_SECRETS_URL} target="_blank" rel="noreferrer">
                  <ExternalLink size={14} aria-hidden /> Open Cursor's Secrets page
                </a>
              </div>
              <ol className="gd-sub">
                <li>
                  For the name, paste <code>{SECRET_NAME}</code> <CopyButton text={SECRET_NAME} aria="Copy the secret name" />. It must match exactly.
                </li>
                <li>For the value, paste the key you copied in step 2.</li>
                <li>Save.</li>
              </ol>
              <Success done={false}>
                <b>Success looks like:</b> <code>{SECRET_NAME}</code> appears in Cursor's list of secrets. Khipu can't see Cursor's settings, so step 5 checks this for you.
              </Success>
            </div>
            {grok.steps[2].acknowledged ? (
              <p className="meta">You said you added it. Step 5 is what checks.</p>
            ) : (
              <button type="button" onClick={() => ack(3)}>I've added it</button>
            )}
          </Step>

          <Step n={4} title="Tell Cursor where Khipu is" state={stateOf(4)} where={<>In Cursor's website: <b>Agents → MCP menu → Add a server</b></>}>
            <p>Paste this text. It holds no secret: Cursor fills in the key from the secret you added.</p>
            <div className="gd-panel">
              <div className="gd-row">
                <span className="meta">Server settings</span>
                <span className="spacer" />
                {settings?.text ? <CopyButton text={settings.text} label="Copy" aria="Copy the server settings" /> : null}
                <a className="gd-btn" href={CURSOR_AGENTS_URL} target="_blank" rel="noreferrer">
                  <ExternalLink size={14} aria-hidden /> Open Cursor's Agents page
                </a>
              </div>
              {settings?.text ? (
                <pre className="gd-cfg" aria-label="Server settings to paste">{settings.text}</pre>
              ) : (
                <p className="meta gd-tight" role="status">
                  {settings?.error
                    ? `Khipu couldn't make the settings text: ${settings.error}`
                    : "Save your gateway address in step 1 and the text to paste appears here."}
                </p>
              )}
              <Success done={false}>
                <b>Success looks like:</b> <code>khipu-cloud</code> appears in Cursor's MCP list. This applies to every repo Grok Bot opens.
              </Success>
            </div>
            <p className="meta">Only want Khipu in one repo? Per-repo setup from the app is coming.</p>
            {grok.steps[3].acknowledged ? (
              <p className="meta">You said you added it. Step 5 is what checks.</p>
            ) : (
              <button type="button" onClick={() => ack(4)}>I've added it</button>
            )}
          </Step>

          <Step n={5} title="Check that it works" state={stateOf(5)}>
            <div className="gd-panel">
              <div className="gd-row">
                <button type="button" disabled={checking} onClick={() => void check()}>
                  {checking ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                  Check the gateway
                </button>
                <span className="meta">Is the gateway up, and does it accept the key?</span>
              </div>
              {words ? (
                <div className={words.tone === "ok" ? "gd-success" : "gd-success bad"} role="status">
                  {words.tone === "ok" ? <Check size={14} aria-hidden /> : <TriangleAlert size={14} aria-hidden />}
                  <span><b>{words.text}</b> {checkedAgo ? `Checked ${checkedAgo}.` : ""}</span>
                </div>
              ) : null}
            </div>
            <div className="gd-panel">
              <p className="gd-tight">Then start a Grok Bot task in Cursor and ask it:</p>
              <div className="gd-row">
                <code>{TEST_QUESTION}</code>
                <CopyButton text={TEST_QUESTION} aria="Copy the test question" />
              </div>
              <Success done={false}>
                <b>Coming:</b> a line here that turns green by itself when Grok Bot's first call reaches Khipu. For now, Grok Bot's answer is the proof.
              </Success>
            </div>
          </Step>
        </ol>

        <h3 className="gd-sec">What Grok Bot can and can't do with Khipu</h3>
        <div className="gd-can">
          <div>
            <h6>It can</h6>
            <ul>
              <li><Check size={14} aria-hidden /><span>Search what all your agents remember, and open any result</span></li>
              <li><Check size={14} aria-hidden /><span>Save a summary of its work when it calls <code>khipu_capture</code></span></li>
              <li><Check size={14} aria-hidden /><span>Read and close open to-dos and decisions</span></li>
              <li><Check size={14} aria-hidden /><span>See which accounts are open, once the cloud copy of routing is available (not available yet)</span></li>
            </ul>
          </div>
          <div className="no">
            <h6>It can't</h6>
            <ul>
              <li><Minus size={14} aria-hidden /><span>See your plan usage or how much is left</span></li>
              <li><Minus size={14} aria-hidden /><span>Change your priority, rules, caps or pauses</span></li>
              <li><Minus size={14} aria-hidden /><span>Take actions on your behalf in Khipu</span></li>
              <li><Minus size={14} aria-hidden /><span>Forget sessions saved on this Mac</span></li>
            </ul>
          </div>
        </div>
        <div className="gd-callout info">
          <Info size={14} aria-hidden />
          <span><b>Grok Bot only remembers what it saves.</b> Nothing records its sessions automatically. End a task with "Save what you did to Khipu."</span>
        </div>

        <h3 className="gd-sec">If it doesn't work</h3>
        <details className="gd-trouble">
          <summary>"The gateway didn't answer"</summary>
          <p>The address is wrong or the server is down. Check the address in step 1. If it's right, the server running the gateway needs attention.</p>
        </details>
        <details className="gd-trouble">
          <summary>"The gateway refused the key"</summary>
          <p>The key in Cursor doesn't match your gateway's. Copy it again in step 2 and replace the secret in Cursor.</p>
        </details>
        <details className="gd-trouble">
          <summary>Grok Bot says it has no Khipu tools</summary>
          <p>Cursor doesn't have the server settings. Repeat step 4 and check that <code>khipu-cloud</code> is in the MCP list.</p>
        </details>
        <details className="gd-trouble">
          <summary>Grok Bot can't reach Khipu even though the check passed</summary>
          <p>The gateway works but Grok Bot isn't reaching it. Most often the secret's name has a typo; it must be exactly <code>{SECRET_NAME}</code>.</p>
        </details>
      </article>

      <aside className="gd-toc" aria-label="On this page">
        <h6>Steps</h6>
        <ol>
          {["Gateway address", "Copy the key", "Add the secret", "Point Cursor at Khipu", "Check it works"].map((label, i) => {
            const n = i + 1;
            const st = stateOf(n as 1 | 2 | 3 | 4 | 5);
            return (
              <li key={label} className={st}>
                <button
                  type="button"
                  className="link"
                  onClick={() => {
                    setFocusStep(n);
                    window.requestAnimationFrame(() => document.getElementById(`gd-s${n}`)?.scrollIntoView?.({ block: "nearest" }));
                  }}
                >
                  {label}
                </button>
              </li>
            );
          })}
        </ol>
        <div className="gd-box">
          <p>See it on the Harnesses page</p>
          <button type="button" onClick={onOpenHarnesses}>
            <BookOpen size={14} aria-hidden /> Open Grok Bot's card
          </button>
        </div>
      </aside>
    </div>
  );
}
