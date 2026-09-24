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

export interface AuditEvent {
  id: string;
  createdAt: number;
  actorKind: string;
  actorKeyFingerprint: string | null;
  containerTag: string | null;
  orgId: string | null;
  action: string;
  resourceType: string | null;
  resourceId: string | null;
  outcome: string;
  metadata: Record<string, unknown>;
}

export interface UsageCounter {
  keyFingerprint: string;
  containerTag: string | null;
  orgId: string | null;
  requests: number;
  searches: number;
  documentWrites: number;
  factWrites: number;
  inputChars: number;
  createdAt: number;
  updatedAt: number;
}
