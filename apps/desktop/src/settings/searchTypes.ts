// The shapes Settings › Search by meaning and Books read from `khipu embed
// profiles list` and `khipu library list` (moved here from the Embeddings
// screen, which A11 removed).
export type Coverage = { total: number; embedded: number; missing: number; stale: number; pct: number };

export type ProfileRow = {
  id: string;
  provider: string;
  model: string;
  dim: number;
  normalize?: string;
  endpoint?: string | null;
  is_active: boolean;
  rows?: { memory: number; library: number };
  in_use_by?: string[];
  coverage?: Record<string, Coverage>;
  key_present?: boolean;
  median_query_ms_24h?: number | null;
  price_per_million_usd?: number | null;
};

export type LibraryRow = {
  name: string;
  root: string;
  profile: string;
  enabled: boolean;
  documents: number;
  chunks: number;
  embedded: number;
  missing: number;
  stale: number;
  pct: number;
};
