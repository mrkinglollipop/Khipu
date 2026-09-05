import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { getVersion } from "@tauri-apps/api/app";
import { Loader2, RefreshCw } from "lucide-react";
import { WorkingBanner } from "./WorkingBanner";

function parse(raw: string): Record<string, unknown> | null {
  try {
    const v = JSON.parse(raw);
    return v && typeof v === "object" ? (v as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

type UpgradeInfo = {
  available?: boolean;
  current?: string;
  target?: string;
  current_tag?: string;
  target_tag?: string;
};

type StatusPayload = {
  ok?: boolean;
  khipu_app?: string;
  cli?: string;
  docker?: { ok?: boolean; error?: string };
  postgres?: {
    mode?: string;
    source?: string;
    image?: string;
    server_version?: string;
    pgvector?: string;
    port?: number;
  };
  postgres_probe?: { ok?: boolean; error?: string } | null;
  graphify?: { semver?: string; path?: string; source?: string };
  postgres_upgrade?: UpgradeInfo | null;
  graphify_upgrade?: UpgradeInfo | null;
  error?: string;
};

export function ComponentsPanel({ active }: { active: boolean }) {
  const [status, setStatus] = useState<StatusPayload | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  // Tauri's own package version — the last-resort answer, and the only one
  // that is true by construction.
  const [appVersion, setAppVersion] = useState<string | null>(null);

  useEffect(() => {
    void getVersion()
      .then(setAppVersion)
      .catch(() => setAppVersion(null));
  }, []);

  const reload = useCallback(async () => {
    setMsg(null);
    setBusy((prev) => prev ?? "status");
    try {
      const raw = await invoke<string>("components_status");
      setStatus(parse(raw) as StatusPayload);
    } catch (e) {
      setStatus(null);
      setMsg(String(e));
    } finally {
      setBusy((prev) => (prev === "status" ? null : prev));
    }
  }, []);

  // Panels are CSS-hidden on tab switch, not unmounted, so a mount-only
  // effect never re-fetches — the pane goes stale as soon as something
  // changes elsewhere (0.3.12 fixed the same staleness for Status/Doctor by
  // keying the load on tab activation instead of mount; audit 2026-08-31).
  useEffect(() => {
    if (!active) return;
    void reload();
  }, [active, reload]);

  const runUpgrade = useCallback(async (kind: "postgres" | "graphify") => {
    setBusy(kind);
    setMsg(null);
    try {
      const cmd = kind === "postgres" ? "upgrade_postgres" : "upgrade_graphify";
      const raw = await invoke<string>(cmd);
      const out = parse(raw);
      if (out?.ok) {
        setMsg(
          kind === "postgres"
            ? `Postgres upgraded to ${String(out.image ?? out.server_version ?? "new image")}.`
            : `Graphify upgraded to ${String(out.semver ?? "new version")}.`,
        );
      } else {
        setMsg(String(out?.error ?? raw));
      }
      await reload();
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy(null);
    }
  }, [reload]);

  // The CLI answers "unknown" when it has no version to report (and older
  // builds answered a hardcoded release string, which is why this line lied);
  // either way, only a real version wins over Tauri's own getVersion().
  const cliVersion = [status?.cli, status?.khipu_app].find(
    (v) => typeof v === "string" && v.trim() && v.trim() !== "unknown",
  );

  const pg = status?.postgres;
  const gy = status?.graphify;
  const docker = status?.docker;
  const postgresProbe = status?.postgres_probe;
  const pgUp = status?.postgres_upgrade;
  const gyUp = status?.graphify_upgrade;
  const pgRemote = pg?.mode === "remote" || pg?.source === "dsn";
  const gyExternal = gy?.source === "env";

  return (
    <div className="panel-body">
      <WorkingBanner
        label={
          busy === "status"
            ? "Checking components…"
            : busy === "postgres"
              ? "Upgrading Postgres…"
              : busy === "graphify"
                ? "Upgrading Graphify…"
                : null
        }
      />
      <div className="section-card">
        <div className="section-head">App + CLI version</div>
        <div className="section-body">
          <p className="muted">
            {/* The CLI's own answer first: it now reads KHIPU_APP_VERSION,
                which the Rust side sets from the app's CARGO_PKG_VERSION on
                every invocation, so the two agree. Before that env var the
                CLI fell through to a hard-coded "0.3.14" and this line
                confidently reported a version nobody was running (audit
                2026-09-04). Tauri's getVersion() is the fallback. */}
            Upgrade the desktop app from Settings → Updates. Version:{" "}
            <code>{cliVersion ?? appVersion ?? "…"}</code>
          </p>
        </div>
      </div>

      <div className="section-card">
        <div className="section-head">
          PostgreSQL 19
        </div>
        <div className="section-body">
          <div className="toolbar">
            <button type="button" disabled={busy != null} onClick={() => void reload()}>
              <RefreshCw size={14} aria-hidden /> Refresh
            </button>
          </div>
          {pgRemote ? (
            <p className="muted">
              Remote mode — image tag not tracked locally. Server:{" "}
              <code>{pg?.server_version ?? "?"}</code>, pgvector{" "}
              <code>{pg?.pgvector ?? "?"}</code>.
            </p>
          ) : pg?.image ? (
            <p className="muted">
              Local Docker image <code>{pg.image}</code>
              {pg.port ? <> on port <code>{pg.port}</code></> : null}
              {pg.pgvector ? <> · pgvector <code>{pg.pgvector}</code></> : null}
            </p>
          ) : postgresProbe && postgresProbe.ok === false ? (
            <p className="muted">
              Could not reach the configured Postgres DSN: {postgresProbe.error ?? "unknown error"}.
            </p>
          ) : docker && docker.ok === false ? (
            <p className="muted">
              Docker not found — install Docker Desktop to set up local Postgres, or configure a remote DSN in Settings.
            </p>
          ) : (
            <p className="muted">No local Postgres component installed yet.</p>
          )}
          <div className="toolbar">
            <button
              type="button"
              className="primary"
              disabled={
                busy != null ||
                pgRemote ||
                !pgUp?.available ||
                !pg?.image
              }
              onClick={() => void runUpgrade("postgres")}
            >
              {busy === "postgres" ? (
                <Loader2 size={14} className="spin" aria-hidden />
              ) : null}
              {pgUp?.available
                ? `Upgrade to ${pgUp.target_tag ?? pgUp.target ?? "new tag"}`
                : "Postgres up to date"}
            </button>
          </div>
        </div>
      </div>

      <div className="section-card">
        <div className="section-head">Graphify</div>
        <div className="section-body">
          {gyExternal ? (
            <p className="muted">
              External Graphify install — <code>{gy?.path ?? "?"}</code> (managed outside
              the app; upgrades via the maintainer tree).
            </p>
          ) : gy?.semver ? (
            <p className="muted">
              Installed <code>{gy.semver}</code>
              {gy.path ? (
                <>
                  {" "}
                  at <code>{gy.path}</code>
                </>
              ) : null}
            </p>
          ) : (
            <p className="muted">Graphify not installed — finish Welcome → Graph first.</p>
          )}
          {gyExternal ? null : (
            <div className="toolbar">
              <button
                type="button"
                className="primary"
                disabled={busy != null || !gyUp?.available || !gy?.semver}
                onClick={() => void runUpgrade("graphify")}
              >
                {busy === "graphify" ? (
                  <Loader2 size={14} className="spin" aria-hidden />
                ) : null}
                {gyUp?.available
                  ? `Upgrade to ${gyUp.target ?? "new version"}`
                  : "Graphify up to date"}
              </button>
            </div>
          )}
        </div>
      </div>

      {msg ? <pre className="code">{msg}</pre> : null}
    </div>
  );
}
