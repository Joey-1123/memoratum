const API = "";

function headers(): Record<string, string> {
  const key = sessionStorage.getItem("memoratum_key") ?? "";
  return {
    "Content-Type": "application/json",
    ...(key ? { Authorization: `Bearer ${key}` } : {}),
  };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: { ...headers(), ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const msg = (body as { error?: { message?: string } }).error?.message ?? `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return res.json() as Promise<T>;
}

type FactsResponse = { facts: import("./types").GraphFact[]; total: number };
type CachedFacts = { expires: number; value: FactsResponse };
const factsCache = new Map<string, CachedFacts>();
const factsInFlight = new Map<string, Promise<FactsResponse>>();
const FACT_CACHE_MS = 5_000;

async function loadFacts(tag: string, limit: number): Promise<FactsResponse> {
  const all: import("./types").GraphFact[] = [];
  let total = 0;
  let offset = 0;
  const pageSize = 500;
  do {
    const page = await request<FactsResponse>(
      `/v4/facts?containerTag=${encodeURIComponent(tag)}&limit=${pageSize}&offset=${offset}`
    );
    all.push(...page.facts);
    total = page.total;
    offset += page.facts.length;
    if (page.facts.length === 0) break;
  } while (all.length < limit && offset < total);
  return { facts: all.slice(0, limit), total };
}

function factsForTag(tag: string, limit: number): Promise<FactsResponse> {
  const key = `${tag}:${limit}`;
  const cached = factsCache.get(key);
  if (cached && cached.expires > Date.now()) return Promise.resolve(cached.value);
  const inFlight = factsInFlight.get(key);
  if (inFlight) return inFlight;
  const promise = loadFacts(tag, limit)
    .then((value) => {
      factsCache.set(key, { expires: Date.now() + FACT_CACHE_MS, value });
      return value;
    })
    .finally(() => factsInFlight.delete(key));
  factsInFlight.set(key, promise);
  return promise;
}

export const api = {
  setKey(key: string) {
    if (key) sessionStorage.setItem("memoratum_key", key);
    else sessionStorage.removeItem("memoratum_key");
    factsCache.clear();
    factsInFlight.clear();
  },
  profile: (tag: string) =>
    request<import("./types").Profile>(`/v4/profile?containerTag=${encodeURIComponent(tag)}`),
  search: (body: Record<string, unknown>) =>
    request<{ results: import("./types").SearchHit[]; total: number }>("/v4/search", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  factsPage: (tag: string, limit = 100, offset = 0) =>
    request<FactsResponse>(
      `/v4/facts?containerTag=${encodeURIComponent(tag)}&limit=${limit}&offset=${offset}`
    ),
  facts: (tag: string, limit = 10000) => factsForTag(tag, limit),
  audit: (limit = 50, offset = 0) =>
    request<{ events: import("./types").AuditEvent[]; total: number }>(
      `/v4/audit?limit=${limit}&offset=${offset}`
    ),
  usage: (limit = 50, offset = 0) =>
    request<{ usage: import("./types").UsageCounter[]; total: number }>(
      `/v4/usage?limit=${limit}&offset=${offset}`
    ),
};
