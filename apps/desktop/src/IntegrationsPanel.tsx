import { useCallback, useEffect, useState } from "react";
import {
  CircleCheck,
  CircleDashed,
  CircleMinus,
  Loader2,
  RefreshCw,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";

/**
 * Integrations — the pane the agent-integration note locked on 2026-08-17.
 *
 * Thin wrapper over `khipu integrations status|install|verify|uninstall`
 * (packages/cli/khipu/integrations.py). One card per DETECTED harness; each
 * card shows its native components (MCP, capture hook, recall rule) with a
 * state that is only ever green after a real probe passed. Undetected
 * harnesses are shown quietly, never as errors. Every write the CLI does is
 * backed up first, so Uninstall is a full rollback.
 *
 * Nothing here edits a legacy capture hook: Install adds Khipu's entries
 * ALONGSIDE them (dual-write). Removing legacy is a later, soak-gated step
 * and is deliberately not a button on this pane yet.
 */

type HarnessId = "claude_code" | "cursor" | "aegis" | "codex" | "grok_bot";

type StatusRow = {
  harness: HarnessId;
  detected: boolean;
  mcp: boolean;
  hook_stop: boolean;
  hook_precompact: boolean;
  recall_rule: string;
  /** "legacy" = the harness's existing hooks extract; "installed"/"missing" = Khipu's own (Aegis). */
  extract?: string;
};

type Probe = { ok?: boolean; error?: string; ms?: number; episodes?: number; exit?: number; chars?: number };

type VerifyRow = {
  harness: HarnessId;
  detected: boolean;
  ok?: boolean;
  components?: { mcp?: Probe; hook?: Probe; recall?: Probe; extract?: Probe };
  /** Evidence from REAL sessions (not probes): the harness ran the hook, captures
   *  landed, nothing is stuck. `ok=false` + reasons is the "runs but records
   *  nothing" case — the silent failure the pane exists to make loud. */
  runtime?: Runtime;
  /** Older name for `runtime` on the Aegis card. */
  aegis?: Runtime;
};

type Runtime = {
  ok?: boolean;
  reasons?: string[];
  last_dispatch?: string | null;
  last_dispatch_age_s?: number | null;
  last_captured_age_s?: number | null;
  captures?: number;
  dispatches?: number;
  pending_turns?: number;
  queue_depth?: number | null;
  sessions_tracked?: number | null;
  note?: string | null;
  error?: string;
};

const LABEL: Record<HarnessId, string> = {
  claude_code: "Claude Code",
  cursor: "Cursor",
  aegis: "Aegis",
  codex: "Codex",
  grok_bot: "Grok Bot (Cursor cloud)",
};

const WHERE: Record<HarnessId, string> = {
  claude_code: "~/.claude.json · ~/.claude/settings.json",
  cursor: "~/.cursor/mcp.json · ~/.cursor/hooks.json",
  aegis: "~/.grok/config.toml",
  codex: "~/.codex/config.toml · ~/.codex/hooks.json",
  grok_bot: "per repo: .cursor/mcp.json → your Khipu HTTPS gateway",
};

type ComponentState = "verified" | "installed" | "failed" | "missing" | "na";

function Dot({ state }: { state: ComponentState }) {
  const size = 16;
  if (state === "verified")
    return <CircleCheck size={size} strokeWidth={1.75} aria-hidden className="ok" />;
  if (state === "failed")
    return <TriangleAlert size={size} strokeWidth={1.75} aria-hidden className="err" />;
  if (state === "installed")
    return <ShieldCheck size={size} strokeWidth={1.75} aria-hidden className="warn" />;
  // "Nothing to do here" and "absent" are different answers to "is it working?"
  // and had shared one grey dashed circle, so a card working exactly as designed
  // read as half-broken (2026-08-17). Solid minus = by design; dashed +
  // warn = genuinely missing.
  if (state === "na")
    return <CircleMinus size={size} strokeWidth={1.75} aria-hidden className="muted" />;
  return <CircleDashed size={size} strokeWidth={1.75} aria-hidden className="warn" />;
}

function fmtAge(s: number | null | undefined): string {
  if (s == null) return "an unknown time";
  if (s < 90) return `${s}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 172800) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

const STATE_TEXT: Record<ComponentState, string> = {
  verified: "Installed and verified",
  installed: "Installed, not yet verified",
  failed: "Installed, verify failed",
  missing: "Not installed",
  na: "Nothing to install here — by design",
};

export function IntegrationsPanel({
  runKhipu,
  onToast,
}: {
  runKhipu: (args: string[]) => Promise<string>;
  onToast: (msg: string) => void;
}) {
  const [rows, setRows] = useState<StatusRow[] | null>(null);
  const [verify, setVerify] = useState<Record<string, VerifyRow>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const raw = await runKhipu(["integrations", "status"]);
      setRows(JSON.parse(raw) as StatusRow[]);
    } catch (e) {
      // A failed status read is NOT "nothing installed" — keep the last rows
      // and surface the failure as its own state.
      setLoadError(String(e));
    }
  }, [runKhipu]);

  useEffect(() => {
    void load();
  }, [load]);

  const act = useCallback(
    async (harness: HarnessId | "all", cmd: "install" | "verify" | "uninstall") => {
      setBusy(`${cmd}:${harness}`);
      try {
        const raw = await runKhipu(["integrations", cmd, harness]);
        if (cmd === "verify") {
          const list = JSON.parse(raw) as VerifyRow[];
          setVerify((prev) => {
            const next = { ...prev };
            for (const v of list) next[v.harness] = v;
            return next;
          });
        } else if (cmd === "install") {
          // install prints two JSON docs: results, then {verify:[...]}
          const idx = raw.indexOf('{\n  "verify"');
          const verifyDoc = idx >= 0 ? (JSON.parse(raw.slice(idx)) as { verify: VerifyRow[] }) : null;
          if (verifyDoc) {
            setVerify((prev) => {
              const next = { ...prev };
              for (const v of verifyDoc.verify) next[v.harness] = v;
              return next;
            });
          }
          onToast("Installed. Restart each harness to load the change.");
        } else {
          setVerify((prev) => {
            const next = { ...prev };
            if (harness === "all") return {};
            delete next[harness];
            return next;
          });
          onToast("Removed Khipu entries. Backups kept next to each file.");
        }
        await load();
      } catch (e) {
        onToast(`${cmd} failed: ${String(e).slice(0, 160)}`);
      } finally {
        setBusy(null);
      }
    },
    [runKhipu, load, onToast],
  );

  const detected = (rows ?? []).filter((r) => r.detected);
  const undetected = (rows ?? []).filter((r) => !r.detected);

  return (
    <div className="panel-body">
      {loadError ? (
        <div className="doctor-card err" role="alert">
          <TriangleAlert size={24} strokeWidth={1.75} aria-hidden />
          <div>
            <div className="doctor-title">Couldn't read integration status</div>
            <p className="doctor-sub muted">
              The CLI didn't answer. Showing the last known state. Retry, or check that
              Python 3.11 and the Khipu repo path are set in Settings.
            </p>
            <div className="toolbar">
              <button type="button" onClick={() => void load()}>
                <RefreshCw size={14} strokeWidth={1.75} aria-hidden /> Retry
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <div className="toolbar">
        <button
          type="button"
          className="primary"
          disabled={busy != null || detected.length === 0}
          onClick={() => void act("all", "install")}
        >
          {busy === "install:all" ? <Loader2 size={14} className="spin" aria-hidden /> : null}
          Install all
        </button>
        <button
          type="button"
          disabled={busy != null || detected.length === 0}
          onClick={() => void act("all", "verify")}
        >
          {busy === "verify:all" ? <Loader2 size={14} className="spin" aria-hidden /> : null}
          Verify all
        </button>
        <button type="button" disabled={busy != null} onClick={() => void load()}>
          <RefreshCw size={14} strokeWidth={1.75} aria-hidden /> Refresh
        </button>
      </div>

      {rows == null && !loadError ? (
        <p className="muted">Reading harness configs…</p>
      ) : null}

      {detected.map((r) => {
        const v = verify[r.harness];
        // A passed probe outranks the config read: verify talks to the server,
        // status only looks at a file. Grok Bot's server lives in the Cursor
        // cloud UI, which this Mac cannot read, so "no repo pin" is not "absent".
        const mcpState: ComponentState = v?.components?.mcp
          ? v.components.mcp.ok
            // A passing grok_bot probe proves the GATEWAY answers this Mac, not
            // that the cloud agent is pointed at it — never award that row the
            // same green as a harness whose own client we handshook with.
            ? (r.harness === "grok_bot" ? "installed" : "verified")
            : "failed"
          : r.mcp
            ? "installed"
            : r.harness === "grok_bot"
              ? "na"
              : "missing";
        const hookInstalled = r.hook_stop && r.hook_precompact;
        const hookState: ComponentState = r.harness === "grok_bot"
          ? "na"                       // cloud agent: capture is the khipu_capture tool over the gateway
          : !hookInstalled
          ? "missing"
          : v?.components?.hook
            ? v.components.hook.ok
              ? "verified"
              : "failed"
            : "installed";
        const ruleState: ComponentState =
          r.recall_rule === "n/a"
            ? "na"
            : r.recall_rule === "installed"
              ? v?.components?.recall
                ? v.components.recall.ok
                  ? "verified"
                  : "failed"
                : "installed"
              : r.recall_rule === "project_scoped" || r.recall_rule === "mcp_instructions"
                ? "na"
                : "missing";
        const rt: Runtime | undefined = v?.runtime ?? v?.aegis;
        // Red on evidence of failure only; "unseen" is a note, not a warning.
        const rtState: ComponentState = !rt
          ? "installed"
          : rt.error || rt.ok === false
            ? "failed"
            : rt.note
              ? "installed"
              : "verified";
        const extractState: ComponentState =
          r.extract === "installed"
            ? v?.components?.extract
              ? v.components.extract.ok
                ? "verified"
                : "failed"
              : "installed"
            : r.extract === "missing"
              ? "missing"
              : "na";
        return (
          <div className="section-card" key={r.harness}>
            <div className="section-head">
              {LABEL[r.harness]}
              <span className="muted mono" style={{ marginLeft: 8, fontWeight: 400 }}>
                {WHERE[r.harness]}
              </span>
            </div>
            <div className="section-body">
              <div className="rows">
                <div className="row-item">
                  <Dot state={mcpState} />
                  <div className="row-main">
                    <div>MCP server <code>khipu</code></div>
                    <div className="row-meta muted">
                      {r.harness === "grok_bot"
                        ? mcpState === "installed"
                          ? "Gateway answered this Mac with the real token. Whether Grok Bot itself is pointed at it is a Cursor cloud setting, not readable from here"
                          : "Configured in the Cursor cloud UI (account level, every repo) — not readable from this Mac. Verify probes the gateway itself."
                        : STATE_TEXT[mcpState]}
                      {v?.components?.mcp?.ok && v.components.mcp.ms != null
                        ? ` · handshake ${v.components.mcp.ms} ms · ${v.components.mcp.episodes} episodes`
                        : ""}
                      {mcpState === "failed" && v?.components?.mcp?.error
                        ? ` · ${v.components.mcp.error.slice(0, 120)}`
                        : ""}
                    </div>
                  </div>
                </div>
                <div className="row-item">
                  <Dot state={hookState} />
                  <div className="row-main">
                    <div>Capture hook (Stop + PreCompact)</div>
                    <div className="row-meta muted">
                      {r.harness === "grok_bot" ? (
                        "An ephemeral cloud VM has no hooks. The agent captures with the khipu_capture tool (hub mode on the gateway) — the recall rule tells it to."
                      ) : (
                        <>
                          {STATE_TEXT[hookState]}
                          {v?.components?.hook?.ok && v.components.hook.ms != null
                            ? ` · fired in ${v.components.hook.ms} ms`
                            : ""}
                          {hookState === "failed" && v?.components?.hook
                            ? ` · exit ${v.components.hook.exit ?? "?"}`
                            : ""}
                          {hookInstalled
                            ? r.harness === "aegis"
                              ? " · queues the session inside Aegis's sandbox; never writes outside ~/.grok"
                              : " · captures natively, then runs alongside your existing capture hook — never replaces it"
                            : ""}
                        </>
                      )}
                    </div>
                  </div>
                </div>
                <div className="row-item">
                  <Dot state={extractState} />
                  <div className="row-main">
                    <div>Session extraction (what becomes an episode)</div>
                    <div className="row-meta muted">
                      {r.extract === "mcp_capture"
                        ? "The agent calls khipu_capture over the HTTPS gateway (hub mode: PG row + vector, no file). Verify probes the public gateway with the real token."
                        : r.extract === "legacy" || r.extract == null
                        ? "Done by this harness's existing capture hooks; Khipu syncs and embeds what they write."
                        : STATE_TEXT[extractState] +
                          (r.extract === "installed"
                            ? r.harness === "aegis"
                              ? " · Khipu-native: Stop (every 5 turns / 20 min), PreCompact and SessionEnd queue the session inside Aegis's sandbox; drained to gemini-2.5-flash → khipu capture outside it"
                              : " · Khipu-native, hook-driven: Stop (every 5 turns / 20 min), PreCompact and SessionEnd read the transcript and capture in the same Stop. Nothing for the model to remember."
                            : "") +
                          (v?.components?.extract?.ok && v.components.extract.ms != null
                            ? ` · probe in ${v.components.extract.ms} ms`
                            : "") +
                          (extractState === "failed" && v?.components?.extract?.error
                            ? ` · ${v.components.extract.error.slice(0, 120)}`
                            : "")}
                    </div>
                  </div>
                </div>
                {rt ? (
                  <div className="row-item">
                    <Dot state={rtState} />
                    <div className="row-main">
                      <div>Recording (evidence from real sessions, not probes)</div>
                      <div className="row-meta muted">
                        {rt.error
                          ? `Couldn't read liveness: ${rt.error}`
                          : rt.note
                            ? `Not yet observed: ${rt.note}. A probe shows the hook works; only a real session shows ${LABEL[r.harness]} runs it.`
                            : rt.ok
                              ? `${LABEL[r.harness]} last ran the hook ${fmtAge(rt.last_dispatch_age_s)} ago · ` +
                                (rt.captures
                                  ? `${rt.captures} capture(s), last ${fmtAge(rt.last_captured_age_s)} ago`
                                  : "no capture landed yet") +
                                (rt.queue_depth ? ` · ${rt.queue_depth} queued` : "") +
                                (rt.pending_turns ? ` · ${rt.pending_turns} turn(s) toward the next` : "")
                              : `NOT RECORDING — ${(rt.reasons ?? []).join("; ")}. Hook last ran ${fmtAge(rt.last_dispatch_age_s)} ago.`}
                      </div>
                    </div>
                  </div>
                ) : null}
                <div className="row-item">
                  <Dot state={ruleState} />
                  <div className="row-main">
                    <div>Prompt-time recall rule</div>
                    <div className="row-meta muted">
                      {r.recall_rule === "n/a"
                        ? "Not applicable here: this harness discards hook output at prompt time. Recall is via the MCP tools."
                        : r.recall_rule === "mcp_instructions"
                        ? "Served by the gateway as the MCP instructions field on initialize — every client gets it in every repo, with nothing committed."
                        : r.recall_rule === "project_scoped"
                          ? "Per project: Cursor keeps rules inside each repo. Run khipu integrations install cursor --project <dir> to add .cursor/rules/khipu.mdc there."
                          : STATE_TEXT[ruleState] +
                            (v?.components?.recall?.ok && v.components.recall.ms != null
                              ? ` · SessionStart injects ${v.components.recall.chars} chars in ${v.components.recall.ms} ms`
                              : "")}
                    </div>
                  </div>
                </div>
              </div>
              {r.harness === "grok_bot" ? (
                <div className="row-meta muted" style={{ marginTop: 6 }}>
                  Repo-scoped: run <code>khipu integrations install grok_bot --project &lt;repo&gt;</code> for each repo Grok Bot works in.
                  The bearer token lives in Cursor cloud secrets as <code>KHIPU_GATEWAY_TOKEN</code>, never in the repo.
                </div>
              ) : null}
              <div className="toolbar">
                <button
                  type="button"
                  className="primary"
                  disabled={busy != null || r.harness === "grok_bot"}
                  onClick={() => void act(r.harness, "install")}
                >
                  {busy === `install:${r.harness}` ? (
                    <Loader2 size={14} className="spin" aria-hidden />
                  ) : null}
                  {r.mcp && hookInstalled ? "Reinstall" : "Install"}
                </button>
                <button
                  type="button"
                  disabled={busy != null || !(r.mcp || hookInstalled) || r.harness === "grok_bot"}
                  onClick={() => void act(r.harness, "verify")}
                >
                  {busy === `verify:${r.harness}` ? (
                    <Loader2 size={14} className="spin" aria-hidden />
                  ) : null}
                  Verify
                </button>
                <button
                  type="button"
                  disabled={busy != null || !(r.mcp || hookInstalled)}
                  onClick={() => void act(r.harness, "uninstall")}
                >
                  Uninstall
                </button>
              </div>
            </div>
          </div>
        );
      })}

      {undetected.length > 0 ? (
        <div className="section-card">
          <div className="section-head muted">Not on this Mac</div>
          <div className="section-body">
            <p className="muted">
              {undetected.map((r) => LABEL[r.harness]).join(", ")}: config folder not found.
              Nothing to install here.
            </p>
          </div>
        </div>
      ) : null}

      <p className="muted" style={{ marginTop: 12 }}>
        Install writes Khipu's own entries next to whatever is already there and backs each
        file up first. Your existing capture hooks keep running; both write during the
        migration. Restart a harness after installing so it loads the change.
      </p>
    </div>
  );
}
