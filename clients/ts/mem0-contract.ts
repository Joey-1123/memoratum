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

  private async request<T>(path: string, body: unknown): Promise<T> {
    const response = await this.fetchImpl(`${this.baseURL.replace(/\/+$/, "")}${path}`, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`Mem0 compatibility request failed: ${response.status}`);
    return (await response.json()) as T;
  }

  add(input: Mem0AddInput): Promise<Mem0AddResponse> {
    return this.request("/v3/memories/add/", input);
  }

  search(input: Mem0SearchInput): Promise<Mem0SearchResponse> {
    return this.request("/v3/memories/search/", input);
  }

  async event(eventID: string): Promise<{ event_id: string; status: string; results: unknown }> {
    const response = await this.fetchImpl(
      `${this.baseURL.replace(/\/+$/, "")}/v1/event/${encodeURIComponent(eventID)}/`,
      { headers: { Authorization: `Token ${this.apiKey}` } },
    );
    if (!response.ok) throw new Error(`Mem0 compatibility event failed: ${response.status}`);
    return response.json();
  }
}
