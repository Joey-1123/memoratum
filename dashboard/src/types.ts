export interface ProfileStats {
  documents: number;
  chunks: number;
  facts: number;
}

export interface Profile {
  containerTag: string;
  facts: string[];
  stats: ProfileStats;
}

export interface SearchHit {
  id: string;
  memory?: string;
  chunk?: string;
  similarity: number;
}

export interface GraphFact {
  id: string;
  subject: string;
  predicate: string;
  object: string;
  document_id: string | null;
  metadata: Record<string, unknown>;
  valid_from: number;
  valid_to: number | null;
  superseded_by: string | null;
}
