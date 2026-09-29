// Mem0-compatible TypeScript SDK contract probe.
// Copyright (C) 2026 Memoratum contributors — SPDX-License-Identifier: MIT
// (see LICENSE-MIT; client code exception to repo AGPL).

export interface Mem0AddInput {
  messages: Array<{ role: "user" | "assistant" | "system"; content: string }>;
  user_id?: string;
  agent_id?: string;
  app_id?: string;
  run_id?: string;
  metadata?: Record<string, unknown>;
  infer?: boolean;
  [key: string]: unknown;
}

export interface Mem0AddResponse {
  event_id?: string;
  status?: "PENDING";
  results?: Array<{ id: string; memory?: string; event?: string }>;
}

export interface Mem0Memory {
  id: string;
  memory: string;
  metadata?: Record<string, unknown>;
  created_at?: number | string;
  updated_at?: number | string;
  version?: number;
  expiration_date?: string | null;
}

export interface Mem0GetAllInput {
  filters: { user_id?: string; agent_id?: string; app_id?: string; run_id?: string };
  page?: number;
  page_size?: number;
  show_expired?: boolean;
}

export interface Mem0GetAllResponse {
  count: number;
  next: number | null;
  previous: number | null;
  results: Mem0Memory[];
}

export interface Mem0UpdateInput {
  text?: string;
  metadata?: Record<string, unknown>;
  timestamp?: number | string;
  expiration_date?: string | null;
}

export interface Mem0HistoryEntry {
  id: string;
  memory_id: string;
  old_memory?: string | null;
  new_memory?: string | null;
  event: string;
  metadata?: Record<string, unknown>;
  version?: number;
  created_at?: number | string;
  updated_at?: number | string;
}

export interface Mem0BatchUpdateItem {
  memory_id: string;
  text?: string;
  metadata?: Record<string, unknown>;
}

export interface Mem0BatchDeleteItem {
  memory_id: string;
}

export interface Mem0BatchResponse {
  message: string;
}

export interface Mem0DeleteAllResponse {
  message: string;
  event_id: string;
}

export interface Mem0Project {
  id: string;
  org_id: string;
  name: string;
  description?: string | null;
  custom_instructions?: string | null;
  custom_categories?: unknown[];
  agent_custom_instructions?: string | null;
  multilingual?: boolean;
  decay?: boolean;
  created_at?: number | string;
  updated_at?: number | string;
}

export interface Mem0ProjectMember {
  email: string;
  role: "OWNER" | "READER";
  project_id?: string;
  created_at?: number | string;
  updated_at?: number | string;
}

export interface Mem0Webhook {
  id: string;
  webhook_id?: string;
  project_id: string;
  name: string;
  url: string;
  event_types: string[];
  is_active: boolean;
  secret?: string;
  created_at?: number | string;
  updated_at?: number | string;
}

export interface Mem0WebhookDelivery {
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

export interface Mem0SearchInput {
  query: string;
  filters: { user_id?: string; agent_id?: string; app_id?: string; run_id?: string };
  top_k?: number;
  threshold?: number;
  rerank?: boolean;
}

export interface Mem0SearchResponse {
  results: Array<{
    id: string;
    memory?: string;
    chunk?: string;
    score?: number;
    metadata?: Record<string, unknown>;
  }>;
}

export class Mem0CompatClient {
  // This is a local contract probe, not a replacement for the official SDK.

  private readonly baseURL: string;
  private readonly apiKey: string;
  private readonly fetchImpl: typeof fetch;

  constructor(baseURL: string, apiKey: string, fetchImpl: typeof fetch = fetch) {
    this.baseURL = baseURL;
    this.apiKey = apiKey;
    this.fetchImpl = fetchImpl;
  }

  private headers(): HeadersInit {
    return {
      "Content-Type": "application/json",
      Authorization: `Token ${this.apiKey}`,
    };
  }

  private async request<T>(
    path: string,
    init: { method?: string; body?: unknown } = {},
  ): Promise<T> {
    const method = init.method ?? "POST";
    const response = await this.fetchImpl(`${this.baseURL.replace(/\/+$/, "")}${path}`, {
      method,
      headers: this.headers(),
      ...(init.body === undefined ? {} : { body: JSON.stringify(init.body) }),
    });
    if (!response.ok) throw new Error(`Mem0 compatibility request failed: ${response.status}`);
    return (await response.json()) as T;
  }

  add(input: Mem0AddInput): Promise<Mem0AddResponse> {
    return this.request("/v3/memories/add/", { body: input });
  }

  search(input: Mem0SearchInput): Promise<Mem0SearchResponse> {
    return this.request("/v3/memories/search/", { body: input });
  }

  get(memoryID: string): Promise<Mem0Memory> {
    return this.request(`/v1/memories/${encodeURIComponent(memoryID)}/`, { method: "GET" });
  }

  getAll(input: Mem0GetAllInput): Promise<Mem0GetAllResponse> {
    const params = new URLSearchParams();
    if (input.page !== undefined) params.set("page", String(input.page));
    if (input.page_size !== undefined) params.set("page_size", String(input.page_size));
    const suffix = params.toString() ? `?${params.toString()}` : "";
    return this.request(`/v3/memories/${suffix}`, { body: input });
  }

  update(memoryID: string, input: Mem0UpdateInput): Promise<Mem0Memory> {
    return this.request(`/v1/memories/${encodeURIComponent(memoryID)}/`, {
      method: "PUT",
      body: input,
    });
  }

  delete(memoryID: string, deleteLinked = false): Promise<{ message: string; cascade_count?: number }> {
    const suffix = deleteLinked ? "?delete_linked=true" : "";
    return this.request(`/v1/memories/${encodeURIComponent(memoryID)}/${suffix}`, {
      method: "DELETE",
    });
  }

  deleteAll(filters: Mem0GetAllInput["filters"]): Promise<Mem0DeleteAllResponse> {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined) params.set(key, value);
    }
    return this.request(`/v1/memories/?${params.toString()}`, { method: "DELETE" });
  }

  history(memoryID: string): Promise<Mem0HistoryEntry[]> {
    return this.request(`/v1/memories/${encodeURIComponent(memoryID)}/history/`, {
      method: "GET",
    });
  }

  batchUpdate(memories: Mem0BatchUpdateItem[]): Promise<Mem0BatchResponse> {
    return this.request("/v1/batch/", { method: "PUT", body: { memories } });
  }

  batchDelete(memories: Mem0BatchDeleteItem[]): Promise<Mem0BatchResponse> {
    return this.request("/v1/batch/", { method: "DELETE", body: { memories } });
  }

  async event(eventID: string): Promise<{ event_id: string; status: string; results: unknown }> {
    return this.request(`/v1/event/${encodeURIComponent(eventID)}/`, { method: "GET" });
  }

  ping(): Promise<{ status: string; org_id: string; project_id: string; user_email?: string | null }> {
    return this.request("/v1/ping/", { method: "GET" });
  }

  listProjects(orgID: string): Promise<Mem0Project[]> {
    return this.request(`/api/v1/orgs/organizations/${encodeURIComponent(orgID)}/projects/`, {
      method: "GET",
    });
  }

  getProject(orgID: string, projectID: string): Promise<Mem0Project> {
    return this.request(
      `/api/v1/orgs/organizations/${encodeURIComponent(orgID)}/projects/${encodeURIComponent(projectID)}/`,
      { method: "GET" },
    );
  }

  createProject(
    orgID: string,
    input: { name: string; description?: string | null },
  ): Promise<Mem0Project> {
    return this.request(`/api/v1/orgs/organizations/${encodeURIComponent(orgID)}/projects/`, {
      body: input,
    });
  }

  updateProject(
    orgID: string,
    projectID: string,
    input: Partial<
      Pick<Mem0Project, "name" | "description" | "custom_instructions" | "custom_categories">
    >,
  ): Promise<Mem0Project> {
    return this.request(
      `/api/v1/orgs/organizations/${encodeURIComponent(orgID)}/projects/${encodeURIComponent(projectID)}/`,
      { method: "PATCH", body: input },
    );
  }

  listMembers(orgID: string, projectID: string): Promise<{ members: Mem0ProjectMember[] }> {
    return this.request(
      `/api/v1/orgs/organizations/${encodeURIComponent(orgID)}/projects/${encodeURIComponent(projectID)}/members/`,
      { method: "GET" },
    );
  }

  addMember(
    orgID: string,
    projectID: string,
    input: { email: string; role: "OWNER" | "READER" },
  ): Promise<Mem0ProjectMember> {
    return this.request(
      `/api/v1/orgs/organizations/${encodeURIComponent(orgID)}/projects/${encodeURIComponent(projectID)}/members/`,
      { body: input },
    );
  }

  updateMember(
    orgID: string,
    projectID: string,
    input: { email: string; role: "OWNER" | "READER" },
  ): Promise<Mem0ProjectMember> {
    return this.request(
      `/api/v1/orgs/organizations/${encodeURIComponent(orgID)}/projects/${encodeURIComponent(projectID)}/members/`,
      { method: "PUT", body: input },
    );
  }

  removeMember(orgID: string, projectID: string, email: string): Promise<{ removed: boolean }> {
    const params = new URLSearchParams({ email });
    return this.request(
      `/api/v1/orgs/organizations/${encodeURIComponent(orgID)}/projects/${encodeURIComponent(projectID)}/members/?${params}`,
      { method: "DELETE" },
    );
  }

  listWebhooks(projectID: string): Promise<Mem0Webhook[]> {
    return this.request(`/api/v1/webhooks/projects/${encodeURIComponent(projectID)}/`, {
      method: "GET",
    });
  }

  createWebhook(
    projectID: string,
    input: { url: string; name: string; event_types: string[] },
  ): Promise<Mem0Webhook> {
    return this.request(`/api/v1/webhooks/projects/${encodeURIComponent(projectID)}/`, { body: input });
  }

  getWebhook(webhookID: string): Promise<Mem0Webhook> {
    return this.request(`/api/v1/webhooks/${encodeURIComponent(webhookID)}/`, { method: "GET" });
  }

  updateWebhook(
    webhookID: string,
    input: Partial<Pick<Mem0Webhook, "name" | "url" | "event_types">>,
  ): Promise<{ message: string; webhook: Mem0Webhook }> {
    return this.request(`/api/v1/webhooks/${encodeURIComponent(webhookID)}/`, {
      method: "PUT",
      body: input,
    });
  }

  deleteWebhook(webhookID: string): Promise<{ message: string; deleted: boolean }> {
    return this.request(`/api/v1/webhooks/${encodeURIComponent(webhookID)}/`, { method: "DELETE" });
  }

  listWebhookDeliveries(projectID: string): Promise<{ deliveries: Mem0WebhookDelivery[] }> {
    return this.request(`/v4/projects/${encodeURIComponent(projectID)}/webhooks/deliveries`, {
      method: "GET",
    });
  }

  replayWebhookDelivery(deliveryID: string): Promise<{ id: string; status: string; replayed: boolean }> {
    return this.request(`/v4/webhooks/deliveries/${encodeURIComponent(deliveryID)}/replay`, {
      method: "POST",
    });
  }
}
