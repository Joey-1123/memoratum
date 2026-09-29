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

export interface MemoryRecord {
  id: string;
  memory: string;
  metadata: Record<string, unknown>;
  created_at: number;
  updated_at: number;
  version: number;
  expiration_date: string | null;
}

export interface MemoryPage {
  count: number;
  next: number | null;
  previous: number | null;
  results: MemoryRecord[];
}

export interface MemoryHistoryEntry {
  id: string;
  memory_id: string;
  old_memory: string | null;
  new_memory: string | null;
  event: string;
  metadata: Record<string, unknown>;
  version: number;
  created_at: number;
  updated_at: number;
  content_hash?: string;
}

export interface WebhookDelivery {
  id: string;
  event_id: string;
  webhook_id: string;
  project_id: string;
  event_type: string;
  memory_id: string | null;
  status: string;
  attempts: number;
  next_attempt_at: number | null;
  last_error: string | null;
  response_status: number | null;
  created_at: number;
  updated_at: number;
}
