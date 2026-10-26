/* The live evidence the Guides page, the rail badge and Home's hint share:
   the saved gateway address, whether a gateway key is saved, the last
   "Check the gateway" answer, and whether an agent has asked Khipu anything.
   Every value is read from Khipu; nothing is a "mark done" click. */

import { useCallback, useEffect, useMemo, useState } from "react";
import { callCli, useCliState } from "./settingsCli";
import type { ConfigShape } from "./settingsCli";
import {
  DEFAULT_PREFS,
  grokBotProgress,
  LEGACY_STORE_KEY,
  liveGrokCheck,
  parsePrefs,
  prefsJson,
} from "./guidesModel";
import type { GrokBotProgress, GuidesPrefs } from "./guidesModel";

type TokenStatus = { exists?: boolean };
type LogEntry = { harness?: string; ts?: string };

/** What the gateway check said, in the words the guide shows. */
export function gatewayCheckWords(check: GuidesPrefs["grokCheck"]): { tone: "ok" | "err"; text: string } | null {
  if (!check) return null;
  if (check.ok) return { tone: "ok", text: "Gateway answered and accepted the key. (Checked from this Mac.)" };
  if (check.status === 401 || check.status === 403) {
    return { tone: "err", text: "The gateway refused the key. The key in Cursor must match the one on your gateway." };
  }
  if (check.status == null) {
    const err = check.error ?? "";
    // Address-missing must win over the broader "no gateway…" key message.
    if (err.startsWith("no gateway address")) {
      return { tone: "err", text: "There is no gateway address saved yet. Save it in step 1." };
    }
    if (err.startsWith("no gateway")) {
      return { tone: "err", text: "There is no gateway key saved on this Mac yet. Save it in step 2." };
    }
    return { tone: "err", text: "The gateway didn't answer. The address may be wrong, or the server is down." };
  }
  return { tone: "err", text: `The gateway answered with an error (${check.status}).` };
}

/** A search log harness that means an agent asked — not this app or the doctor. */
export function isAgentRecallHarness(harness: string | undefined): boolean {
  const h = (harness ?? "").trim();
  return h !== "" && h !== "doctor" && h !== "desktop";
}

type McpComponent = { ok?: boolean; error?: string; http_status?: number | null };

/** `khipu integrations verify grok_bot`, reduced to the one fact the guide
 *  needs: does the gateway answer this Mac, and does it accept the key. */
export async function runGatewayCheck(
  runKhipu: (args: string[]) => Promise<string>,
): Promise<NonNullable<GuidesPrefs["grokCheck"]>> {
  const at = Date.now();
  try {
    const rows = JSON.parse(await runKhipu(["integrations", "verify", "grok_bot"])) as Array<{
      detected?: boolean;
      components?: { mcp?: McpComponent };
    }>;
    const row = Array.isArray(rows) ? rows[0] : undefined;
    if (!row || row.detected === false) {
      return { ok: false, at, error: "no gateway address saved", status: null };
    }
    const mcp = row.components?.mcp;
    if (mcp?.ok === true) return { ok: true, at };
    return { ok: false, at, error: String(mcp?.error ?? "no answer").slice(0, 160), status: typeof mcp?.http_status === "number" ? mcp.http_status : null };
  } catch (e) {
    return { ok: false, at, error: String(e).slice(0, 160), status: null };
  }
}

async function loadPrefsFromApp(): Promise<GuidesPrefs> {
  const res = await callCli<Record<string, unknown>>("khipu_guides_prefs_get");
  if (res.ok && res.data && typeof res.data === "object" && Object.keys(res.data).length > 0) {
    return parsePrefs(res.data);
  }
  // One-shot migrate from the old webview store so a hide survives the move.
  try {
    const legacy = window.localStorage.getItem(LEGACY_STORE_KEY);
    if (legacy) {
      const parsed = parsePrefs(legacy);
      await callCli("khipu_guides_prefs_set", { json: prefsJson(parsed) });
      window.localStorage.removeItem(LEGACY_STORE_KEY);
      return parsed;
    }
  } catch {
    /* private mode / missing localStorage */
  }
  return DEFAULT_PREFS;
}

export function useGuideSignals(refreshKey: unknown) {
  // `active` stays false: the effect below owns the loading, keyed on where
  // the user is, so a screen change re-reads once, not twice.
  const cfg = useCliState<ConfigShape>("khipu_config_show", false);
  const token = useCliState<TokenStatus>("khipu_gateway_token_status", false);
  const [log, setLog] = useState<LogEntry[] | null>(null);
  const [prefs, setPrefsState] = useState<GuidesPrefs>(DEFAULT_PREFS);

  const reloadCfg = cfg.reload;
  const reloadToken = token.reload;
  const reloadLog = useCallback(async () => {
    const res = await callCli<LogEntry[]>("khipu_recall_log");
    if (res.ok && Array.isArray(res.data)) setLog(res.data);
  }, []);

  const reloadPrefs = useCallback(async () => {
    setPrefsState(await loadPrefsFromApp());
  }, []);

  // Re-read whenever the user comes back to a screen that shows these.
  useEffect(() => {
    void reloadCfg();
    void reloadToken();
    void reloadLog();
    void reloadPrefs();
  }, [refreshKey, reloadCfg, reloadToken, reloadLog, reloadPrefs]);

  const updatePrefs = useCallback((patch: Partial<GuidesPrefs>) => {
    setPrefsState((prev) => {
      const next = { ...prev, ...patch };
      void callCli("khipu_guides_prefs_set", { json: prefsJson(next) });
      return next;
    });
  }, []);

  const gatewayUrl = cfg.data?.gateway_url ?? "";
  const keySaved = token.data ? token.data.exists === true : null;

  // Drop a check that no longer matches the saved address (step 5 must go stale).
  useEffect(() => {
    if (prefs.grokCheck && liveGrokCheck(prefs.grokCheck, gatewayUrl) == null) {
      updatePrefs({ grokCheck: null });
    }
  }, [gatewayUrl, prefs.grokCheck, updatePrefs]);

  const grok: GrokBotProgress = useMemo(
    () => grokBotProgress({ gatewayUrl, keySaved, check: prefs.grokCheck, ack: prefs.grokAck }),
    [gatewayUrl, keySaved, prefs.grokCheck, prefs.grokAck],
  );

  // The search log carries a harness label for each call. The doctor's own
  // probes and this app's Recall searches (KHIPU_HARNESS=desktop) do not count.
  const agentAsked = (log ?? []).some((e) => isAgentRecallHarness(e.harness));

  return {
    prefs,
    updatePrefs,
    gatewayUrl,
    keySaved,
    grok,
    agentAsked,
    reload: useCallback(async () => {
      await Promise.all([reloadCfg(), reloadToken(), reloadLog(), reloadPrefs()]);
    }, [reloadCfg, reloadToken, reloadLog, reloadPrefs]),
  };
}
