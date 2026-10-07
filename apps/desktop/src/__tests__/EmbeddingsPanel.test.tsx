// Settings -> Embeddings. The webview names a fixed Tauri command and passes
// ids; the CLI (faked here, with JSON shaped like `embed profiles list`,
// `library list`, `embed jobs`, `embed estimate`, `embed status` and
// `library status`) owns all state, so every write is followed by a re-read.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { EmbeddingsPanel } from "../EmbeddingsPanel";
import type { JobRow, LibraryRow, ProfileRow } from "../EmbeddingsPanel";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));
const openMock = vi.fn();
vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: (...args: unknown[]) => openMock(...args),
}));

type Args = Record<string, unknown> | undefined;
const calls: Array<[string, Args]> = [];
const callsTo = (cmd: string) => calls.filter(([c]) => c === cmd).map(([, a]) => a);

const gemini = (over: Partial<ProfileRow> = {}): ProfileRow => ({
  id: "gemini-embedding-2@768",
  provider: "gemini",
  model: "gemini-embedding-2",
  dim: 768,
  normalize: "l2",
  endpoint: null,
  is_active: true,
  rows: { memory: 22271, library: 0 },
  in_use_by: ["memory"],
  coverage: { memory: { total: 22271, embedded: 22271, missing: 0, stale: 0, pct: 100 } },
  key_present: true,
  median_query_ms_24h: 412,
  price_per_million_usd: null,
  ...over,
});
const voyage = (over: Partial<ProfileRow> = {}): ProfileRow => ({
  id: "voyage-3@1024",
  provider: "voyage",
  model: "voyage-3",
  dim: 1024,
  normalize: "l2",
  endpoint: null,
  is_active: false,
  rows: { memory: 0, library: 329224 },
  in_use_by: ["biblical"],
  coverage: {
    memory: { total: 22271, embedded: 0, missing: 22271, stale: 0, pct: 0 },
    "library:biblical": { total: 329224, embedded: 329224, missing: 0, stale: 0, pct: 100 },
  },
  key_present: true,
  median_query_ms_24h: 180,
  price_per_million_usd: 0.06,
  ...over,
});
const nomic = (over: Partial<ProfileRow> = {}): ProfileRow => ({
  id: "nomic-embed-text@768",
  provider: "openai-compatible",
  model: "nomic-embed-text",
  dim: 768,
  normalize: "l2",
  endpoint: "http://localhost:11434",
  is_active: false,
  rows: { memory: 0, library: 0 },
  in_use_by: [],
  coverage: { memory: { total: 22271, embedded: 0, missing: 22271, stale: 0, pct: 0 } },
  key_present: false,
  median_query_ms_24h: 210,
  price_per_million_usd: 0,
  ...over,
});
const biblical = (over: Partial<LibraryRow> = {}): LibraryRow => ({
  name: "biblical",
  root: "/Users/me/biblical-library",
  profile: "voyage-3@1024",
  enabled: true,
  documents: 5581,
  chunks: 329224,
  embedded: 329224,
  missing: 0,
  stale: 0,
  pct: 100,
  ...over,
});
const job = (over: Partial<JobRow> = {}): JobRow => ({
  job: "app-1-aa",
  kind: "embed-backfill",
  profile: "nomic-embed-text@768",
  space: "memory",
  state: "running",
  done: 8120,
  total: 22271,
  failed: 0,
  started_at: new Date(Date.now() - 600_000).toISOString(),
  updated_at: new Date().toISOString(),
  error: null,
  pid: 4242,
  ...over,
});

let profiles: ProfileRow[];
let libraries: LibraryRow[];
let jobs: JobRow[];
let refuse: Record<string, string>;
let jobsReads: number;
let estimateChunks: number;
let estimatePrice: number | null;

beforeEach(() => {
  calls.length = 0;
  jobsReads = 0;
  estimateChunks = 22271;
  estimatePrice = 0;
  refuse = {};
  profiles = [gemini(), voyage(), nomic()];
  libraries = [biblical()];
  jobs = [];
  openMock.mockReset();
  invokeMock.mockImplementation(async (cmd: string, args?: Args) => {
    calls.push([cmd, args]);
    if (refuse[cmd]) return JSON.stringify({ ok: false, error: refuse[cmd] });
    switch (cmd) {
      case "khipu_embed_profiles_list":
        return JSON.stringify({ profiles });
      case "khipu_library_list":
        return JSON.stringify({ libraries });
      case "khipu_embed_jobs":
        jobsReads += 1;
        return JSON.stringify({ jobs });
      case "khipu_embed_jobs_clear":
        jobs = jobs.filter((j) => j.state === "running");
        return JSON.stringify({ ok: true, removed: 1 });
      case "khipu_embed_estimate":
        return JSON.stringify({
          profile: args?.profile,
          provider: "openai-compatible",
          space: args?.space,
          chunks: estimateChunks,
          chars: 56_000_000,
          approx_tokens: 14_000_000,
          price_usd: estimatePrice,
          price_note: estimatePrice === 0 ? "local server, no charge" : "price unknown for this provider",
          approx_seconds: 2400,
          rate_source: "2 finished jobs for this profile",
        });
      case "khipu_embed_status":
        return JSON.stringify({
          profile: args?.profile,
          chunks: 22271,
          embedded: 21067,
          missing: 1120,
          stale: 84,
          pct: 94.5,
          sample_missing: ["episode:18402", "episode:18399"],
          sample_stale: ["topic:embedding-profiles"],
        });
      case "khipu_library_status":
        return JSON.stringify({ ...libraries[0], sample_missing: ["Heiser/Unseen Realm.txt"], sample_stale: [] });
      case "khipu_embed_activate":
        profiles = profiles.map((p) => ({ ...p, is_active: p.id === args?.profile }));
        return JSON.stringify({ ok: true, active_profile: args?.profile });
      case "khipu_job_start":
        return { ok: true, job_id: "app-9-zz", pid: 99, log_path: "/tmp/x.log" };
      case "khipu_job_cancel":
        return { ok: true, job: args?.job_id, pid: 4242 };
      case "khipu_embed_profile_delete":
        profiles = profiles.filter((p) => p.id !== args?.id);
        return JSON.stringify({ ok: true });
      case "khipu_embed_test_key":
        return JSON.stringify({ ok: true, provider: args?.provider, model: args?.model, ms: 196, dim: 768 });
      case "set_khipu_secret":
        return JSON.stringify({ ok: true });
      case "khipu_embed_profile_add":
        profiles = [...profiles, nomic({ id: String(args?.id), model: String(args?.model) })];
        return JSON.stringify({ ok: true });
      case "khipu_library_add":
        libraries = [...libraries, biblical({ name: String(args?.name), root: String(args?.root), chunks: 0, documents: 0, embedded: 0, pct: 0 })];
        return JSON.stringify({ ok: true, name: args?.name });
      case "khipu_library_scan":
        return JSON.stringify({ ok: true, added: 3, updated: 0, removed: 0, unchanged: 0, skipped: 1, documents: 3, chunks: 40 });
      case "khipu_library_import":
        return JSON.stringify({
          ok: true,
          read: 329859,
          imported: 329224,
          skipped_corrupt: 635,
          skipped_bad_vector: 0,
          skipped_bad_row: 0,
          unmapped: 0,
          documents: 5340,
        });
      case "khipu_library_set_profile":
        libraries = libraries.map((l) => (l.name === args?.name ? { ...l, profile: String(args?.profile) } : l));
        return JSON.stringify({ ok: true, name: args?.name, profile: args?.profile });
      case "khipu_library_remove":
        libraries = [];
        return JSON.stringify({ ok: true, removed: args?.name });
      default:
        throw new Error(`unexpected command ${cmd}`);
    }
  });
});

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
});

const model = (id: string) => screen.findByRole("group", { name: id });

describe("EmbeddingsPanel rows", () => {
  it("renders one row per model with provider, use, coverage, key, delay and price", async () => {
    render(<EmbeddingsPanel active />);
    const g = await model("gemini-embedding-2@768");
    expect(within(g).getByText("Active for memory")).toBeInTheDocument();
    expect(within(g).getByText(/Gemini · 768 dimensions · Key: saved/)).toBeInTheDocument();
    expect(within(g).getByText("412 ms")).toBeInTheDocument();
    expect(within(g).getByText("22,271 of 22,271 pieces")).toBeInTheDocument();
    expect(within(g).getByText("Complete")).toBeInTheDocument();

    const v = await model("voyage-3@1024");
    expect(within(v).getByText("Library: biblical")).toBeInTheDocument();
    expect(within(v).getByText(/\$0.06 per 1M tokens/)).toBeInTheDocument();
    expect(within(v).getByText("329,224 of 329,224 pieces")).toBeInTheDocument();
    // Library-only vectors: no memory row.
    expect(within(v).queryByRole("progressbar", { name: "Memory coverage" })).toBeNull();

    const n = await model("nomic-embed-text@768");
    expect(within(n).getByText("Not used")).toBeInTheDocument();
    expect(within(n).getByText(/No key needed/)).toBeInTheDocument();
    expect(within(n).getByText("Not started")).toBeInTheDocument();
    expect(within(n).getByText("0 of 22,271 pieces")).toBeInTheDocument();
  });

  it("renders the library row with its counts and actions", async () => {
    render(<EmbeddingsPanel active />);
    const l = await screen.findByRole("group", { name: "Library biblical" });
    expect(within(l).getByText("100% embedded")).toBeInTheDocument();
    expect(within(l).getByText("/Users/me/biblical-library")).toBeInTheDocument();
    expect(within(l).getByText("5,581")).toBeInTheDocument();
    expect(within(l).getByText("329,224")).toBeInTheDocument();
    for (const name of ["Scan", "Embed missing", "Re-embed biblical with…", "Import an index", "Remove"]) {
      expect(within(l).getByRole("button", { name })).toBeInTheDocument();
    }
    expect(within(l).getByRole("button", { name: "Embed missing" })).toBeDisabled();
    expect(within(l).getByText("Nothing missing.")).toBeInTheDocument();
  });

  it("shows the reason under each disabled button", async () => {
    render(<EmbeddingsPanel active />);
    const g = await model("gemini-embedding-2@768");
    expect(within(g).getByRole("button", { name: "Delete index" })).toBeDisabled();
    expect(within(g).getByText("Memory search uses it.")).toBeInTheDocument();
    const v = await model("voyage-3@1024");
    expect(within(v).getByText("Used by biblical.")).toBeInTheDocument();
  });

  it("shows a read error in words", async () => {
    refuse.khipu_embed_profiles_list = "database is not reachable";
    render(<EmbeddingsPanel active />);
    expect(await screen.findByText(/Could not read the embeddings: database is not reachable/)).toBeInTheDocument();
  });

  it("reads the redo list (missing and stale) when the redo button is opened", async () => {
    profiles = [gemini({ coverage: { memory: { total: 22271, embedded: 21067, missing: 1120, stale: 84, pct: 94.5 } } })];
    render(<EmbeddingsPanel active />);
    const g = await model("gemini-embedding-2@768");
    fireEvent.click(within(g).getByRole("button", { name: "1,204 to redo" }));
    const panel = await within(g).findByRole("group", { name: "Pieces of text to redo" });
    expect(within(panel).getByText("Session 18402")).toBeInTheDocument();
    expect(within(panel).getByText("Topic page · embedding-profiles")).toBeInTheDocument();
    expect(within(panel).getByText(/missing \(never embedded\)/)).toBeInTheDocument();
    expect(callsTo("khipu_embed_status")).toContainEqual({ profile: "gemini-embedding-2@768" });
  });
});

describe("Make active", () => {
  it("is disabled with the missing count until coverage is 100 percent", async () => {
    profiles = [gemini(), nomic({ coverage: { memory: { total: 22271, embedded: 8120, missing: 14151, stale: 0, pct: 36.4 } }, rows: { memory: 8120, library: 0 } })];
    render(<EmbeddingsPanel active />);
    const n = await model("nomic-embed-text@768");
    const button = within(n).getByRole("button", { name: "Make active for memory" });
    expect(button).toBeDisabled();
    expect(within(n).getByText("14,151 missing")).toBeInTheDocument();
    fireEvent.click(button);
    expect(callsTo("khipu_embed_activate")).toHaveLength(0);
  });

  it("is enabled at 100 percent, and flips the pointer without force", async () => {
    profiles = [gemini(), nomic({ coverage: { memory: { total: 22271, embedded: 22271, missing: 0, stale: 0, pct: 100 } }, rows: { memory: 22271, library: 0 } })];
    render(<EmbeddingsPanel active />);
    const n = await model("nomic-embed-text@768");
    const button = within(n).getByRole("button", { name: "Make active for memory" });
    expect(button).toBeEnabled();
    expect(within(n).queryByText(/\d missing/)).toBeNull();
    fireEvent.click(button);
    await waitFor(() => expect(callsTo("khipu_embed_activate")).toEqual([{ profile: "nomic-embed-text@768" }]));
    const refreshed = await model("nomic-embed-text@768");
    await waitFor(() => expect(within(refreshed).getByText("Active for memory")).toBeInTheDocument());
  });

  it("shows the CLI's refusal as text", async () => {
    profiles = [gemini(), nomic({ coverage: { memory: { total: 5, embedded: 5, missing: 0, stale: 0, pct: 100 } }, rows: { memory: 5, library: 0 } })];
    refuse.khipu_embed_activate = "refusing to activate nomic-embed-text@768: 2 episodes still missing vectors";
    render(<EmbeddingsPanel active />);
    const n = await model("nomic-embed-text@768");
    fireEvent.click(within(n).getByRole("button", { name: "Make active for memory" }));
    expect(await within(n).findByRole("alert")).toHaveTextContent("2 episodes still missing vectors");
  });
});

describe("estimate, start, progress and cancel", () => {
  it("shows an estimate first, then starts a memory job with the right arguments", async () => {
    render(<EmbeddingsPanel active />);
    const n = await model("nomic-embed-text@768");
    fireEvent.click(within(n).getByRole("button", { name: "Re-embed memory with this model" }));
    const dlg = await screen.findByRole("dialog", { name: "Re-embed memory with nomic-embed-text?" });
    expect(await within(dlg).findByText("22,271 pieces of text, about 14M tokens")).toBeInTheDocument();
    expect(within(dlg).getByText("about 40 min at the measured rate")).toBeInTheDocument();
    expect(within(dlg).getByText(/none \(a local server costs nothing\)/)).toBeInTheDocument();
    expect(within(dlg).getByText("Search keeps using gemini-embedding-2 until you choose Make active.")).toBeInTheDocument();
    expect(callsTo("khipu_job_start")).toHaveLength(0);
    expect(callsTo("khipu_embed_estimate")).toEqual([{ profile: "nomic-embed-text@768", space: "memory", stale: true }]);

    fireEvent.click(within(dlg).getByRole("button", { name: "Start" }));
    await waitFor(() =>
      expect(callsTo("khipu_job_start")).toEqual([
        { kind: "memory", profile: "nomic-embed-text@768", library: null, stale: true },
      ]),
    );
    expect(await within(n).findByText("Starting…")).toBeInTheDocument();
  });

  it("starts a library job under the library name", async () => {
    libraries = [biblical({ embedded: 300000, missing: 29224, pct: 91.1 })];
    render(<EmbeddingsPanel active />);
    const l = await screen.findByRole("group", { name: "Library biblical" });
    fireEvent.click(within(l).getByRole("button", { name: "Embed missing" }));
    const dlg = await screen.findByRole("dialog", { name: /Embed the missing pieces of biblical with voyage-3/ });
    await within(dlg).findByText(/pieces of text, about/);
    expect(callsTo("khipu_embed_estimate")).toEqual([{ profile: "voyage-3@1024", space: "library:biblical", stale: false }]);
    fireEvent.click(within(dlg).getByRole("button", { name: "Start" }));
    await waitFor(() =>
      expect(callsTo("khipu_job_start")).toEqual([
        { kind: "library", profile: "voyage-3@1024", library: "biblical", stale: false },
      ]),
    );
  });

  it("shows progress for a running job and Cancel calls khipu_job_cancel with its id", async () => {
    jobs = [job()];
    profiles = [gemini(), nomic({ coverage: { memory: { total: 22271, embedded: 8120, missing: 14151, stale: 0, pct: 36.4 } }, rows: { memory: 8120, library: 0 } })];
    render(<EmbeddingsPanel active />);
    const n = await model("nomic-embed-text@768");
    const bar = await within(n).findByRole("progressbar", { name: "Embedding memory" });
    expect(bar).toHaveAttribute("aria-valuenow", "8120");
    expect(bar).toHaveAttribute("aria-valuemax", "22271");
    expect(within(n).getByText(/8,120 of 22,271 pieces of text/)).toBeInTheDocument();
    expect(within(n).getByText(/Search keeps using gemini-embedding-2/)).toBeInTheDocument();
    // Nothing else can change while it runs.
    expect(within(n).getByRole("button", { name: /Embed missing/ })).toBeDisabled();
    expect(within(n).getByRole("button", { name: "Delete index" })).toBeDisabled();
    fireEvent.click(within(n).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(callsTo("khipu_job_cancel")).toEqual([{ job_id: "app-1-aa" }]));
  });

  it("polls the job files every two seconds while a job runs", async () => {
    jobs = [job()];
    render(<EmbeddingsPanel active />);
    await model("nomic-embed-text@768");
    const first = jobsReads;
    jobs = [job({ done: 12000 })];
    await waitFor(() => expect(jobsReads).toBeGreaterThan(first), { timeout: 4500 });
    const n = await model("nomic-embed-text@768");
    await waitFor(() => expect(within(n).getByText(/12,000 of 22,271/)).toBeInTheDocument(), { timeout: 4500 });
  }, 12000);

  it("does not poll when nothing is running", async () => {
    render(<EmbeddingsPanel active />);
    await model("nomic-embed-text@768");
    const reads = jobsReads;
    await new Promise((r) => setTimeout(r, 2300));
    expect(jobsReads).toBe(reads);
  });

  it("shows a failed job with its error, partial coverage and Retry", async () => {
    jobs = [job({ state: "failed", error: "the server at localhost:11434 did not answer" })];
    profiles = [gemini(), nomic({ coverage: { memory: { total: 22271, embedded: 8120, missing: 14151, stale: 0, pct: 36.4 } }, rows: { memory: 8120, library: 0 } })];
    render(<EmbeddingsPanel active />);
    const n = await model("nomic-embed-text@768");
    const alert = await within(n).findByRole("alert");
    expect(alert).toHaveTextContent("Embedding stopped");
    expect(alert).toHaveTextContent("the server at localhost:11434 did not answer");
    expect(within(n).getByText("Partial")).toBeInTheDocument();
    expect(within(n).getByText("Cancel or retry the job first.")).toBeInTheDocument();
    fireEvent.click(within(n).getByRole("button", { name: "Retry" }));
    const dlg = await screen.findByRole("dialog", { name: /Embed the missing pieces of memory with nomic-embed-text/ });
    expect(dlg).toBeInTheDocument();
  });

  it("says partial for a finished job that failed some chunks", async () => {
    jobs = [job({ state: "done", done: 20000, total: 22271, failed: 2271 })];
    render(<EmbeddingsPanel active />);
    const n = await model("nomic-embed-text@768");
    expect(await within(n).findByText("Embedding finished, partly")).toBeInTheDocument();
    expect(within(n).getByText(/2,271 failed/)).toBeInTheDocument();
  });

  it("says the price is unknown when the CLI has none and the endpoint is not local", async () => {
    estimatePrice = null;
    profiles = [gemini(), nomic({ provider: "openai-compatible", endpoint: "https://api.example.com" })];
    render(<EmbeddingsPanel active />);
    const n = await model("nomic-embed-text@768");
    fireEvent.click(within(n).getByRole("button", { name: "Re-embed memory with this model" }));
    const dlg = await screen.findByRole("dialog");
    expect(await within(dlg).findByText("unknown for this provider")).toBeInTheDocument();
  });

  it("shows the estimate error instead of a Start button that works", async () => {
    refuse.khipu_embed_estimate = "unknown embedding profile 'nomic-embed-text@768'";
    render(<EmbeddingsPanel active />);
    const n = await model("nomic-embed-text@768");
    fireEvent.click(within(n).getByRole("button", { name: "Re-embed memory with this model" }));
    const dlg = await screen.findByRole("dialog");
    expect(await within(dlg).findByRole("alert")).toHaveTextContent("unknown embedding profile");
    expect(within(dlg).getByRole("button", { name: "Start" })).toBeDisabled();
  });
});

describe("delete an index", () => {
  it("names the row count, keeps the text, and calls the delete verb", async () => {
    profiles = [gemini(), nomic({ rows: { memory: 22271, library: 0 }, coverage: { memory: { total: 22271, embedded: 22271, missing: 0, stale: 0, pct: 100 } } })];
    render(<EmbeddingsPanel active />);
    const n = await model("nomic-embed-text@768");
    fireEvent.click(within(n).getByRole("button", { name: "Delete index" }));
    const dlg = await screen.findByRole("dialog", { name: "Delete the index for nomic-embed-text?" });
    expect(within(dlg).getByText(/22,271 pieces of text\. The text stays/)).toBeInTheDocument();
    fireEvent.click(within(dlg).getByRole("button", { name: "Delete index" }));
    await waitFor(() => expect(callsTo("khipu_embed_profile_delete")).toEqual([{ id: "nomic-embed-text@768" }]));
  });

  it("shows the CLI's refusal inside the dialog", async () => {
    profiles = [gemini(), nomic({ rows: { memory: 5, library: 0 }, coverage: { memory: { total: 5, embedded: 5, missing: 0, stale: 0, pct: 100 } } })];
    refuse.khipu_embed_profile_delete = "profile 'nomic-embed-text@768' is in use by memory";
    render(<EmbeddingsPanel active />);
    const n = await model("nomic-embed-text@768");
    fireEvent.click(within(n).getByRole("button", { name: "Delete index" }));
    const dlg = await screen.findByRole("dialog");
    fireEvent.click(within(dlg).getByRole("button", { name: "Delete index" }));
    expect(await within(dlg).findByRole("alert")).toHaveTextContent("is in use by memory");
  });
});

describe("remove a model with no index entries", () => {
  it("offers Remove model, confirms in plain words and runs the same delete command", async () => {
    render(<EmbeddingsPanel active />);
    const n = await model("nomic-embed-text@768");
    const button = within(n).getByRole("button", { name: "Remove model" });
    expect(button).toBeEnabled();
    expect(within(n).queryByRole("button", { name: "Delete index" })).toBeNull();
    fireEvent.click(button);
    const dlg = await screen.findByRole("dialog", { name: "Remove nomic-embed-text?" });
    expect(within(dlg).getByText("It has no index entries.")).toBeInTheDocument();
    fireEvent.click(within(dlg).getByRole("button", { name: "Remove model" }));
    await waitFor(() => expect(callsTo("khipu_embed_profile_delete")).toEqual([{ id: "nomic-embed-text@768" }]));
  });

  it("stays disabled while the model is in use", async () => {
    profiles = [gemini({ rows: { memory: 0, library: 0 } })];
    render(<EmbeddingsPanel active />);
    const g = await model("gemini-embedding-2@768");
    expect(within(g).getByRole("button", { name: "Remove model" })).toBeDisabled();
    expect(within(g).getByText("Memory search uses it.")).toBeInTheDocument();
  });
});

describe("Make active for a library", () => {
  const withOtherModel = () => {
    libraries = [biblical({ profile: "voyage-3@1024", chunks: 100, embedded: 100, documents: 5 })];
    profiles = [
      gemini(),
      voyage(),
      nomic({ rows: { memory: 0, library: 60 }, in_use_by: [], coverage: { memory: { total: 22271, embedded: 0, missing: 22271, stale: 0, pct: 0 } } }),
    ];
  };

  it("is enabled only when the other model covers every chunk, and moves the pointer", async () => {
    withOtherModel();
    estimateChunks = 0;
    render(<EmbeddingsPanel active />);
    const l = await screen.findByRole("group", { name: "Library biblical" });
    const button = await within(l).findByRole("button", { name: "Make active for biblical" });
    expect(button).toBeEnabled();
    expect(within(l).getByText(/Moves search to nomic-embed-text/)).toBeInTheDocument();
    fireEvent.click(button);
    await waitFor(() =>
      expect(callsTo("khipu_library_set_profile")).toEqual([{ name: "biblical", profile: "nomic-embed-text@768" }]),
    );
    await waitFor(() => expect(within(l).getByText("nomic-embed-text@768")).toBeInTheDocument());
  });

  it("is disabled with the missing count while the other model is partial", async () => {
    withOtherModel();
    estimateChunks = 40;
    render(<EmbeddingsPanel active />);
    const l = await screen.findByRole("group", { name: "Library biblical" });
    const button = await within(l).findByRole("button", { name: "Make active for biblical" });
    expect(button).toBeDisabled();
    expect(within(l).getByText("40 missing under nomic-embed-text")).toBeInTheDocument();
    fireEvent.click(button);
    expect(callsTo("khipu_library_set_profile")).toHaveLength(0);
  });

  it("is not offered when no other model holds any of the library", async () => {
    render(<EmbeddingsPanel active />);
    const l = await screen.findByRole("group", { name: "Library biblical" });
    expect(within(l).queryByRole("button", { name: /Make active for biblical/ })).toBeNull();
  });
});

describe("libraries", () => {
  it("keeps Add library disabled, with a reason, until there is a folder and a valid name", async () => {
    render(<EmbeddingsPanel active />);
    const add = await screen.findByRole("button", { name: "Add library" });
    expect(add).toBeDisabled();
    expect(screen.getByText("Choose a folder and a name.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Folder"), { target: { value: "/Users/me/talks" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "My Talks" } });
    expect(add).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent(/lowercase letters, digits/);
    expect(callsTo("khipu_library_add")).toHaveLength(0);
  });

  it("adds a library with the chosen model, then scans it and shows the receipt", async () => {
    render(<EmbeddingsPanel active />);
    await screen.findByRole("button", { name: "Add library" });
    fireEvent.change(screen.getByLabelText("Folder"), { target: { value: "/Users/me/talks" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "talks" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Model" }), { target: { value: "voyage-3@1024" } });
    const add = screen.getByRole("button", { name: "Add library" });
    expect(add).toBeEnabled();
    fireEvent.click(add);
    await waitFor(() =>
      expect(callsTo("khipu_library_add")).toEqual([{ name: "talks", root: "/Users/me/talks", profile: "voyage-3@1024" }]),
    );
    await waitFor(() => expect(callsTo("khipu_library_scan")).toEqual([{ name: "talks" }]));
    const row = await screen.findByRole("group", { name: "Library talks" });
    expect(await within(row).findByText("Scanned the folder")).toBeInTheDocument();
  });

  it("fills the name from a chosen folder", async () => {
    openMock.mockResolvedValue("/Users/me/Old Talks");
    render(<EmbeddingsPanel active />);
    await screen.findByRole("button", { name: "Add library" });
    fireEvent.click(screen.getByRole("button", { name: "Choose…" }));
    await waitFor(() => expect((screen.getByLabelText("Folder") as HTMLInputElement).value).toBe("/Users/me/Old Talks"));
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("old-talks");
  });

  it("shows the CLI's error when adding is refused", async () => {
    refuse.khipu_library_add = "root '/nope' is not a directory";
    render(<EmbeddingsPanel active />);
    await screen.findByRole("button", { name: "Add library" });
    fireEvent.change(screen.getByLabelText("Folder"), { target: { value: "/nope" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "nope" } });
    fireEvent.click(screen.getByRole("button", { name: "Add library" }));
    expect(await screen.findByText("root '/nope' is not a directory")).toBeInTheDocument();
  });

  it("imports an index and shows the receipt with rows read, imported, skipped and unmapped", async () => {
    render(<EmbeddingsPanel active />);
    const l = await screen.findByRole("group", { name: "Library biblical" });
    fireEvent.click(within(l).getByRole("button", { name: "Import an index" }));
    const dlg = await screen.findByRole("dialog", { name: "Import an index into biblical" });
    const importButton = within(dlg).getByRole("button", { name: "Import" });
    expect(importButton).toBeDisabled();
    fireEvent.change(within(dlg).getByLabelText("Index file"), { target: { value: "/Users/me/graphify.sqlite" } });
    fireEvent.change(within(dlg).getByLabelText(/Remove this start from each file path/), {
      target: { value: "Biblical System/corpus/" },
    });
    fireEvent.click(importButton);
    await waitFor(() =>
      expect(callsTo("khipu_library_import")).toEqual([
        { name: "biblical", path: "/Users/me/graphify.sqlite", strip_prefix: "Biblical System/corpus/", profile: null },
      ]),
    );
    const receipt = await within(l).findByRole("group", { name: "Receipt" });
    expect(receipt).toHaveTextContent("Imported an index");
    expect(receipt).toHaveTextContent("Read 329,859 rows");
    expect(receipt).toHaveTextContent("Imported 329,224");
    expect(receipt).toHaveTextContent("Skipped 635 corrupt");
    expect(receipt).toHaveTextContent("0 with no matching file");
  });

  it("confirms before removing a library and names what goes", async () => {
    render(<EmbeddingsPanel active />);
    const l = await screen.findByRole("group", { name: "Library biblical" });
    fireEvent.click(within(l).getByRole("button", { name: "Remove" }));
    const dlg = await screen.findByRole("dialog", { name: "Remove the library biblical?" });
    expect(within(dlg).getByText(/5,581 documents, 329,224 pieces of text and 329,224 index entries/)).toBeInTheDocument();
    expect(callsTo("khipu_library_remove")).toHaveLength(0);
    fireEvent.click(within(dlg).getByRole("button", { name: "Remove library" }));
    await waitFor(() => expect(callsTo("khipu_library_remove")).toEqual([{ name: "biblical" }]));
  });

  it("re-embeds a library with another model through the estimate", async () => {
    render(<EmbeddingsPanel active />);
    const l = await screen.findByRole("group", { name: "Library biblical" });
    fireEvent.click(within(l).getByRole("button", { name: "Re-embed biblical with…" }));
    const pick = await screen.findByRole("dialog", { name: "Re-embed biblical with…" });
    fireEvent.change(within(pick).getByLabelText("Model"), { target: { value: "nomic-embed-text@768" } });
    fireEvent.click(within(pick).getByRole("button", { name: "Continue" }));
    const dlg = await screen.findByRole("dialog", { name: "Re-embed biblical with nomic-embed-text?" });
    await within(dlg).findByText(/pieces of text, about/);
    expect(within(dlg).getByText("Search keeps using voyage-3 until you choose Make active for biblical.")).toBeInTheDocument();
    fireEvent.click(within(dlg).getByRole("button", { name: "Start" }));
    await waitFor(() =>
      expect(callsTo("khipu_job_start")).toEqual([
        { kind: "library", profile: "nomic-embed-text@768", library: "biblical", stale: true },
      ]),
    );
  });
});

describe("Add a model", () => {
  async function openDialog() {
    render(<EmbeddingsPanel active />);
    fireEvent.click(await screen.findByRole("button", { name: "Add a model" }));
    return screen.findByRole("dialog", { name: "Add a model" });
  }

  it("tests the key, shows the measured time, never puts the key in a command argument list, and adds the model", async () => {
    const dlg = await openDialog();
    fireEvent.change(within(dlg).getByLabelText("Model id"), { target: { value: "mxbai-embed-large" } });
    fireEvent.change(within(dlg).getByLabelText("Dimensions"), { target: { value: "768" } });
    fireEvent.change(within(dlg).getByLabelText("Key"), { target: { value: "sk-example-secret" } });
    fireEvent.click(within(dlg).getByRole("button", { name: "Test key" }));
    expect(await within(dlg).findByText("ok, 196 ms")).toBeInTheDocument();
    expect(callsTo("set_khipu_secret")).toEqual([{ account: "openai_compat_api_key", value: "sk-example-secret" }]);
    expect(callsTo("khipu_embed_test_key")).toEqual([
      { provider: "openai-compatible", endpoint: "http://localhost:11434", model: "mxbai-embed-large" },
    ]);
    fireEvent.click(within(dlg).getByRole("button", { name: "Add model" }));
    await waitFor(() =>
      expect(callsTo("khipu_embed_profile_add")).toEqual([
        {
          id: "mxbai-embed-large@768",
          provider: "openai-compatible",
          model: "mxbai-embed-large",
          dim: 768,
          endpoint: "http://localhost:11434",
        },
      ]),
    );
    // The key travels only to set_khipu_secret.
    for (const [cmd, args] of calls) {
      if (cmd !== "set_khipu_secret") expect(JSON.stringify(args ?? {})).not.toContain("sk-example-secret");
    }
  });

  it("tests automatically when Add model is pressed and stops on a failed test", async () => {
    refuse.khipu_embed_test_key = "HTTP 401: invalid API key";
    const dlg = await openDialog();
    fireEvent.change(within(dlg).getByLabelText("Model id"), { target: { value: "nomic-embed-text:latest" } });
    fireEvent.change(within(dlg).getByLabelText("Dimensions"), { target: { value: "768" } });
    fireEvent.click(within(dlg).getByRole("button", { name: "Add model" }));
    expect(await within(dlg).findByText("HTTP 401: invalid API key")).toBeInTheDocument();
    expect(callsTo("khipu_embed_profile_add")).toHaveLength(0);
  });

  it("refuses dimensions that differ from what the model returned", async () => {
    const dlg = await openDialog();
    fireEvent.change(within(dlg).getByLabelText("Model id"), { target: { value: "nomic-embed-text" } });
    fireEvent.change(within(dlg).getByLabelText("Dimensions"), { target: { value: "1024" } });
    fireEvent.click(within(dlg).getByRole("button", { name: "Add model" }));
    expect(await within(dlg).findByText(/returns 768 dimensions, not 1024/)).toBeInTheDocument();
    expect(callsTo("khipu_embed_profile_add")).toHaveLength(0);
  });

  it("keeps Add model disabled for bad input and says why", async () => {
    const dlg = await openDialog();
    const add = within(dlg).getByRole("button", { name: "Add model" });
    expect(add).toBeDisabled();
    fireEvent.change(within(dlg).getByLabelText("Model id"), { target: { value: "ok-model" } });
    fireEvent.change(within(dlg).getByLabelText("Dimensions"), { target: { value: "99999" } });
    expect(add).toBeDisabled();
    expect(within(dlg).getByRole("alert")).toHaveTextContent(/from 1 to 8192/);
    fireEvent.change(within(dlg).getByLabelText("Dimensions"), { target: { value: "768" } });
    fireEvent.change(within(dlg).getByLabelText("Endpoint"), { target: { value: "http://example.com" } });
    expect(add).toBeDisabled();
    expect(within(dlg).getByRole("alert")).toHaveTextContent(/https:\/\//);
  });

  it("fills the usual model and size for Voyage and hides the endpoint", async () => {
    const dlg = await openDialog();
    fireEvent.change(within(dlg).getByLabelText("Provider"), { target: { value: "voyage" } });
    expect((within(dlg).getByLabelText("Model id") as HTMLInputElement).value).toBe("voyage-3");
    expect((within(dlg).getByLabelText("Dimensions") as HTMLInputElement).value).toBe("1024");
    expect(within(dlg).queryByLabelText("Endpoint")).toBeNull();
    fireEvent.click(within(dlg).getByRole("button", { name: "Test key" }));
    await within(dlg).findByText("ok, 196 ms");
    expect(callsTo("set_khipu_secret")).toHaveLength(0);
    expect(callsTo("khipu_embed_test_key")).toEqual([{ provider: "voyage", endpoint: null, model: "voyage-3" }]);
  });
});
