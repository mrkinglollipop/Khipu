// Fictional pace, caps and advice data for the dev preview and headless
// screenshots, shaped as `khipu route --json` prints it. The numbers are the
// approved mock's (docs/mockups/2026-10-21-routing): Claude 42% of its
// five-hour window and 31% of its week at 2:40 PM on Wednesday 2026-10-21.
import type { RouteView } from "../src/routing";
import type { UsageReport } from "../src/PlansCard";

export const PACE_NOW = new Date(2026, 9, 21, 14, 40);

const at = (day: number, hour: number, minute = 0) => new Date(2026, 9, day, hour, minute).toISOString();

export const PACE_VIEW: RouteView = {
  record: true,
  priority: null,
  accounts: [
    {
      key: "claude|",
      label: "Claude",
      harness: "claude",
      state: "open",
      pace: [
        {
          window: "five_hour", cycle_resets_at: at(21, 17, 10), trailing_h: 1, buckets: 12,
          valid_pct: 100, observed_pct: 100, gappy: false, rate_pct_per_h: 9, why: null, used_pct: 42,
          plan: { verdict: "on_pace", needed_pct_per_h: 7, runs_out_at: null, before_reset_s: null, unused_at_reset_pct: null },
          share_cap: { revision: "r-claude-5h", pct: 60, verdict: "outpacing", needed_pct_per_h: 7.2, runs_out_at: at(21, 16, 40), before_reset_s: 1800, unused_at_reset_pct: null },
          day_cap: null, pace_target: null, headline: "share_cap",
        },
        {
          window: "weekly", cycle_resets_at: at(26, 11), trailing_h: 24, buckets: 48,
          valid_pct: 100, observed_pct: 100, gappy: false, rate_pct_per_h: 0.82, why: null, used_pct: 31,
          plan: { verdict: "outpacing", needed_pct_per_h: 0.59, runs_out_at: at(25, 2, 46), before_reset_s: 32 * 3600, unused_at_reset_pct: null },
          share_cap: { revision: "r-claude-w", pct: 91, verdict: "outpacing", needed_pct_per_h: 0.51, runs_out_at: at(24, 15, 52), before_reset_s: 43 * 3600 + 8 * 60, unused_at_reset_pct: null },
          day_cap: { revision: "r-claude-day", pct: 13, verdict: "near", used_24h_pct: 11.4, observed_pct: 100, settles_at_pct: 19.68, peak_pct: 13, peak_at: at(21, 17, 10), reaches_cap_at: at(21, 17, 10), why: null },
          pace_target: { revision: "r-claude-target", pct: 65, slack: 5, allowance_pct: 25, used_pct: 31, verdict: "ahead", why: null },
          headline: "plan",
        },
      ],
      cap_statuses: [
        { kind: "share", revision: "r-claude-5h", window: "five_hour", pct: 60, verdict: "outpacing" },
        { kind: "share", revision: "r-claude-w", window: "weekly", pct: 91, verdict: "outpacing" },
        { kind: "pace_target", revision: "r-claude-target", window: "weekly", pct: 65, slack: 5, allowance_pct: 25, used_pct: 31, verdict: "ahead", why: null },
        { kind: "per_day", revision: "r-claude-day", window: "weekly", pct: 13, verdict: "near" },
      ],
      advice: {
        account: "claude", label: "Claude", purpose: "job", verdict: "hold_back",
        reason: "pace_target ahead", evidence: "fresh",
        numbers: { allowance_pct: 25, used_pct: 31 }, eligible: true, why_eligible: "provider facts allow this work", advisory: true,
      },
    },
    {
      key: "codex|",
      label: "Codex",
      harness: "codex",
      state: "open",
      pace: [
        {
          window: "five_hour", cycle_resets_at: at(21, 16, 57), trailing_h: 1, buckets: 12,
          valid_pct: 100, observed_pct: 100, gappy: false, rate_pct_per_h: 6, why: null, used_pct: 12,
          plan: { verdict: "underpacing", needed_pct_per_h: 38.6, runs_out_at: null, before_reset_s: null, unused_at_reset_pct: 80 },
          share_cap: null, day_cap: null, pace_target: null, headline: "plan",
        },
        {
          window: "weekly", cycle_resets_at: at(26, 4, 57), trailing_h: 24, buckets: 48,
          valid_pct: 100, observed_pct: 100, gappy: false, rate_pct_per_h: 0.55, why: null, used_pct: 46,
          plan: { verdict: "on_pace", needed_pct_per_h: 0.49, runs_out_at: null, before_reset_s: null, unused_at_reset_pct: null },
          share_cap: null, day_cap: null,
          pace_target: { revision: "r-codex-target", pct: 100, slack: 10, allowance_pct: 44.3, used_pct: 46, verdict: "ahead", why: null },
          headline: "plan",
        },
      ],
      cap_statuses: [
        { kind: "pace_target", revision: "r-codex-target", window: "weekly", pct: 100, slack: 10, allowance_pct: 44.3, used_pct: 46, verdict: "ahead", why: null },
      ],
      advice: {
        account: "codex", label: "Codex", purpose: "job", verdict: "hold_back",
        reason: "pace_target ahead", evidence: "fresh",
        numbers: { allowance_pct: 44.3, used_pct: 46 }, eligible: true, why_eligible: "provider facts allow this work", advisory: true,
      },
    },
    {
      key: "grok|",
      label: "Grok",
      harness: "grok",
      state: "open",
      pace: [
        {
          window: "five_hour", cycle_resets_at: null, trailing_h: 1, buckets: 0,
          valid_pct: 0, observed_pct: 0, gappy: false, rate_pct_per_h: null, why: "this account can't be probed", used_pct: null,
          plan: { verdict: "unknown", needed_pct_per_h: null, runs_out_at: null, before_reset_s: null, unused_at_reset_pct: null },
          share_cap: null, day_cap: null, pace_target: null, headline: "plan",
        },
        {
          window: "weekly", cycle_resets_at: at(23, 21), trailing_h: 24, buckets: 48,
          valid_pct: 100, observed_pct: 100, gappy: false, rate_pct_per_h: 0.38, why: null, used_pct: 61,
          plan: { verdict: "on_pace", needed_pct_per_h: 0.36, runs_out_at: null, before_reset_s: null, unused_at_reset_pct: null },
          share_cap: { revision: "r-grok-w", pct: 76, verdict: "outpacing", needed_pct_per_h: 0.28, runs_out_at: at(23, 6, 10), before_reset_s: 15 * 3600, unused_at_reset_pct: null },
          day_cap: null, pace_target: null, headline: "share_cap",
        },
      ],
      cap_statuses: [
        { kind: "share", revision: "r-grok-w", window: "weekly", pct: 76, verdict: "outpacing" },
      ],
      advice: {
        account: "grok", label: "Grok", purpose: "job", verdict: "go",
        reason: "provider facts allow this work", evidence: "fresh", numbers: {},
        eligible: true, why_eligible: "provider facts allow this work", advisory: true,
      },
    },
    {
      key: "claude|second",
      label: "Claude second",
      harness: "claude",
      state: "open",
      pace: [
        {
          window: "five_hour", cycle_resets_at: at(21, 18, 30), trailing_h: 1, buckets: 12,
          valid_pct: 100, observed_pct: 100, gappy: false, rate_pct_per_h: 0, why: null, used_pct: 0,
          plan: { verdict: "underpacing", needed_pct_per_h: 66.7, runs_out_at: null, before_reset_s: null, unused_at_reset_pct: 100 },
          share_cap: null, day_cap: null, pace_target: null, headline: "plan",
        },
        {
          window: "weekly", cycle_resets_at: at(24, 15), trailing_h: 24, buckets: 48,
          valid_pct: 100, observed_pct: 100, gappy: false, rate_pct_per_h: 0.41, why: null, used_pct: 22,
          plan: { verdict: "underpacing", needed_pct_per_h: 1.08, runs_out_at: null, before_reset_s: null, unused_at_reset_pct: 48 },
          share_cap: null, day_cap: null, pace_target: null, headline: "plan",
        },
      ],
      cap_statuses: [],
      advice: {
        account: "claude-second", label: "Claude second", purpose: "job", verdict: "go",
        reason: "provider facts allow this work", evidence: "fresh", numbers: {},
        eligible: true, why_eligible: "provider facts allow this work", advisory: true,
      },
    },
    {
      key: "glm|",
      label: "GLM",
      harness: "glm",
      state: "open",
      pace: [],
      cap_statuses: [
        { kind: "concurrency", revision: "r-glm-conc", window: null, cap_n: 1, jobs: 1, verdict: "full", why: null },
      ],
      advice: {
        account: "glm", label: "GLM", purpose: "job", verdict: "hold_back",
        reason: "concurrency full", evidence: "fresh", numbers: { active_jobs: 1, concurrency_cap: 1 },
        eligible: true, why_eligible: "provider facts allow this work", advisory: true,
      },
    },
    {
      key: "deepseek|",
      label: "DeepSeek",
      harness: "deepseek",
      state: "open",
      pace: [],
      cap_statuses: [
        { kind: "spend", revision: "r-deep-spend", window: null, cap_usd: 35, days: 7, usd: null, events: null, verdict: "unknown", why: "project-c has a gap in its spend coverage for this account" },
      ],
      advice: {
        account: "deepseek", label: "DeepSeek", purpose: "job", verdict: "go",
        reason: "provider facts allow this work", evidence: "unknown", numbers: {},
        eligible: true, why_eligible: "provider facts allow this work", advisory: true,
      },
    },
    {
      key: "muse|",
      label: "Muse",
      harness: "muse",
      state: "limited",
      until: at(26, 0),
      pace: [
        {
          window: "weekly", cycle_resets_at: at(26, 0), trailing_h: 24, buckets: 48,
          valid_pct: 100, observed_pct: 100, gappy: false, rate_pct_per_h: null, why: null, used_pct: 100,
          plan: { verdict: "limit_reached", needed_pct_per_h: null, runs_out_at: null, before_reset_s: null, unused_at_reset_pct: null },
          share_cap: null, day_cap: null, pace_target: null, headline: "plan",
        },
      ],
      cap_statuses: [],
      advice: {
        account: "muse", label: "Muse", purpose: "job", verdict: "closed",
        reason: "limited", evidence: "fresh", numbers: {},
        eligible: true, why_eligible: "provider facts allow this work", advisory: true,
      },
    },
  ],
};

export const PACE_PLANS: UsageReport = {
  accounts: [
    { key: "claude|", harness: "claude", account: "Claude", plan: "max" },
    { key: "codex|", harness: "codex", account: "Codex", plan: "pro" },
    { key: "grok|", harness: "grok", account: "Grok", plan: "ultra" },
    { key: "claude|second", harness: "claude", account: "Claude second", plan: "max 20x" },
    { key: "glm|", harness: "glm", account: "GLM", plan: "coding" },
    { key: "deepseek|", harness: "deepseek", account: "DeepSeek", plan: "payg" },
    { key: "muse|", harness: "muse", account: "Muse", plan: "power" },
  ],
};
