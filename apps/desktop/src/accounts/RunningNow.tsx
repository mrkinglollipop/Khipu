/* Accounts › Running now (slice A5). Read-only: no control here changes
   anything. One table per Mac, a row group per job runner. A Mac whose
   answer is old, or missing, says so; a job whose runner went quiet is "Not
   known" with what it last said. */

import { useEffect, useState } from "react";
import { Body } from "../shell";
import {
  agoWords, clockWords, currentView, headline, isLive, jobWords,
  type RunJob, type RunMac, type RunRunner, type RunningHook,
} from "./running";
import "./running.css";

/** Re-words "N minutes ago" without asking Khipu again. Tests pin the clock. */
export function useClock(pinned?: Date): Date {
  const [clock, setClock] = useState(() => new Date());
  useEffect(() => {
    if (pinned) return;
    const t = window.setInterval(() => setClock(new Date()), 30_000);
    return () => window.clearInterval(t);
  }, [pinned]);
  return pinned ?? clock;
}

const dash = "–";

function JobRow({ job, runner, now }: { job: RunJob; runner: RunRunner; now: Date }) {
  const w = jobWords(job, runner, now);
  return (
    <tr className={w.unknown ? "rn-unknown" : undefined}>
      <td>
        <div className="rn-t">{job.summary}</div>
        {job.now_line ? <div className="rn-now">{w.unknown ? "Last said: " : ""}{job.now_line}</div> : null}
      </td>
      <td>{job.account_label ?? dash}</td>
      <td>{job.model ?? dash}</td>
      <td><span className="rn-w"><span className={`rn-dot ${w.dot}`} aria-hidden="true" />{w.word}</span></td>
      <td className="rn-when">{w.when}</td>
    </tr>
  );
}

function RunnerGroup({ runner, mac, now }: { runner: RunRunner; mac: RunMac; now: Date }) {
  const beat = mac.old
    ? `Not known now; last read ${clockWords(runner.silent_since ?? runner.heartbeat_at, now)}`
    : runner.silent
    ? `Stopped reporting at ${clockWords(runner.silent_since ?? runner.heartbeat_at, now)}`
    : runner.heartbeat_at ? `Reporting, last heard ${agoWords(runner.heartbeat_at, now)}` : "Reporting";
  return (
    <tbody>
      <tr className="rn-rg">
        <th colSpan={5} scope="colgroup">
          <div className="rn-rgh">
            <b>{runner.name} job runner</b>
            <span className={runner.silent ? "rn-beat warn" : "rn-beat"}>
              <span className={runner.silent ? "rn-dot warn" : "rn-dot ok"} aria-hidden="true" />{beat}
            </span>
          </div>
        </th>
      </tr>
      {runner.silent ? (
        <tr className="rn-note">
          <td colSpan={5}>
            <p className="rn-runner-note">
              <b>Khipu can’t tell if these are still running.</b>{" "}
              {mac.old ? "This is the last answer it could read." : `If this runner should be on, start it again on ${mac.name}.`}
            </p>
          </td>
        </tr>
      ) : null}
      {runner.jobs.length ? runner.jobs.map((j, i) => <JobRow key={i} job={j} runner={runner} now={now} />) : (
        <tr>
          <td colSpan={5} className="rn-muted">
            Nothing running.{runner.last_finished_at ? ` Last job finished at ${clockWords(runner.last_finished_at, now)}.` : ""}
          </td>
        </tr>
      )}
    </tbody>
  );
}

function MacSection({ mac, now }: { mac: RunMac; now: Date }) {
  const id = `rn-mac-${mac.mac || mac.name}`.replace(/[^a-zA-Z0-9_-]/g, "-");
  const ago = agoWords(mac.as_of, now);
  return (
    <section className="rn-mac" aria-labelledby={id}>
      <h2 id={id}>On {mac.name}</h2>
      {mac.stale ? (
        <p className="rn-stale">
          {ago ? `${mac.name} last reported ${ago}.` : `${mac.name} hasn’t reported yet.`} Khipu can’t tell what is running there now.
        </p>
      ) : !mac.runners.length ? (
        <p className="rn-stale">No job runner is reporting. If agents are working on {mac.name}, Khipu can’t see them yet.</p>
      ) : (
        <table className="rn-jobs">
          <thead>
            <tr>
              <th scope="col">Job</th><th scope="col" className="rn-c-acct">Account</th><th scope="col" className="rn-c-model">Model</th>
              <th scope="col" className="rn-c-state">State</th><th scope="col" className="rn-c-when">When</th>
            </tr>
          </thead>
          {mac.runners.map((r) => <RunnerGroup key={r.name} runner={r} mac={mac} now={now} />)}
        </table>
      )}
    </section>
  );
}

export function RunningNow({ hook, now: nowProp }: { hook: RunningHook; now?: Date }) {
  const now = useClock(nowProp);
  const data = hook.data;
  if (!data) {
    return (
      <Body>
        <div className="rn-page" role="status">
          <p className="rn-sub">
            {hook.unread ? "Khipu couldn’t read what is running yet. It will try again." : "Reading what is running…"}
          </p>
        </div>
      </Body>
    );
  }
  const updated = agoWords(data.updated_at, now);
  const shown = currentView(hook) ?? data;
  return (
    <Body>
      <div className="rn-page">
        {hook.readFailed ? (
          <p className="rn-stale" role="alert">
            Khipu couldn’t read what is running just now. This is from {clockWords(data.updated_at, now)}, so what is running now is not known.
          </p>
        ) : null}
        <p className="rn-sub">{headline(shown)}</p>
        {data.note ? <p className="rn-note-line">{data.note}</p> : null}
        {shown.macs.map((m) => <MacSection key={m.mac || m.name} mac={m} now={now} />)}
        <p className="rn-foot">
          {updated ? `Updated ${updated}. ` : ""}Only jobs a job runner reports to Khipu show here.
        </p>
      </div>
    </Body>
  );
}

/** The account page's "Running here now": the same answer, this account's jobs. */
export function RunningHere({ accountKey, accountLabel, hook, now: nowProp }: {
  accountKey: string; accountLabel: string; hook: RunningHook; now?: Date;
}) {
  const now = useClock(nowProp);
  const data = currentView(hook);
  let body: React.ReactNode;
  if (!data) {
    body = <div className="ac-v ac-muted">{hook.unread ? "Khipu couldn’t read what is running yet." : "Reading what is running…"}</div>;
  } else {
    // The job record names its account by id and label; the routing record may spell the id differently, so either one matches.
    const mine = (j: RunJob) => j.account_key === accountKey || j.account_label === accountLabel;
    const rows: Array<{ mac: RunMac; runner: RunRunner; job: RunJob }> = [];
    const unsure: string[] = [];
    for (const mac of data.macs) {
      if (mac.stale) { unsure.push(`${mac.name} last reported ${agoWords(mac.as_of, now) ?? "a while ago"}, so Khipu can’t tell what is running there.`); continue; }
      for (const runner of mac.runners) {
        for (const job of runner.jobs) {
          if (!mine(job)) continue;
          if (isLive(job) || job.state === "unknown") rows.push({ mac, runner, job });
        }
      }
    }
    body = (
      <>
        {rows.length ? rows.map(({ mac, runner, job }, i) => {
          const w = jobWords(job, runner, now);
          return (
            <div key={i} className="ac-v rn-here">
              <span className={`rn-dot ${w.dot}`} aria-hidden="true" />
              <span><b>{job.summary}</b> · {mac.name} · {w.word}, {w.when}</span>
            </div>
          );
        }) : (
          <div className="ac-v ac-muted">
            {data.macs.some((m) => !m.stale && m.runners.length) ? "No job runner reports a job on this account." : "No job runner is reporting, so Khipu can’t see what is using it."}
          </div>
        )}
        {unsure.map((u) => <div key={u} className="ac-v ac-muted">{u}</div>)}
      </>
    );
  }
  return (
    <section className="rn-here-sec" aria-labelledby="rn-here-h">
      <h3 id="rn-here-h">Running here now</h3>
      {body}
    </section>
  );
}
