// Memoratum TypeScript SDK — thin typed wrapper over the HTTP API.
// Zero dependencies (global fetch). AGPL-3.0-or-later.

export interface AddOptions {
  containerTag?: string;
  customId?: string | null;
  dreaming?: "dynamic" | "instant";
  metadata?: Record<string, unknown> | null;
}

export interface SearchOptions {
  containerTag?: string;
  searchMode?: "hybrid" | "memories" | "documents";
  limit?: number;
  threshold?: number;
  filters?: Record<string, unknown> | null;
  rerank?: boolean;
}

export interface SearchHit {
  id: string;
  memory?: string;
  chunk?: string;
  similarity: number;
}

export interface SearchResponse {
  results: SearchHit[];
  timing: number;
  total: number;
}

export class MemoratumError extends Error {}

export class Client {
  private baseURL: string;
  private apiKey: string;
  private timeoutMs: number;

  constructor(opts: { baseURL: string; apiKey?: string; timeoutMs?: number }) {
    this.baseURL = opts.baseURL.replace(/\/+$/, "");
    this.apiKey = opts.apiKey ?? "";
    this.timeoutMs = opts.timeoutMs ?? 30000;
  }

  private async post(path: string, body: unknown): Promise<unknown> {
    let last: unknown = null;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const res = await fetch(`${this.baseURL}${path}`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(this.apiKey ? { Authorization: `Bearer ${this.apiKey}` } : {}),
          },
          body: JSON.stringify(body),
          signal: AbortSignal.timeout(this.timeoutMs),
        });
        const data = (await res.json()) as { error?: { message?: string } };
        if (!res.ok) throw new MemoratumError(data?.error?.message ?? `HTTP ${res.status}`);
        return data;
      } catch (err) {
        if (err instanceof MemoratumError) throw err;
        last = err;
        await new Promise((r) => setTimeout(r, 2 ** attempt * 1000));
      }
    }
    throw new MemoratumError(String(last));
  }

  add(content: string, opts: AddOptions = {}): Promise<{ id: string; status: string }> {
    return this.post("/v3/documents", {
      content,
      containerTag: opts.containerTag ?? "default",
      customId: opts.customId ?? null,
      dreaming: opts.dreaming ?? "dynamic",
      metadata: opts.metadata ?? null,
    }) as Promise<{ id: string; status: string }>;
  }

  search(q: string, opts: SearchOptions = {}): Promise<SearchResponse> {
    return this.post("/v4/search", {
      q,
      containerTag: opts.containerTag ?? "default",
      searchMode: opts.searchMode ?? "hybrid",
      limit: opts.limit ?? 10,
      threshold: opts.threshold ?? 0,
      filters: opts.filters ?? null,
      rerank: opts.rerank ?? false,
    }) as Promise<SearchResponse>;
  }

  async profile(containerTag = "default"): Promise<unknown> {
    const res = await fetch(`${this.baseURL}/v4/profile?containerTag=${containerTag}`, {
      headers: this.apiKey ? { Authorization: `Bearer ${this.apiKey}` } : {},
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    if (!res.ok) throw new MemoratumError(`HTTP ${res.status}`);
    return res.json();
  }
}
