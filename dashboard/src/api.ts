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

export const api = {
  setKey(key: string) {
    if (key) sessionStorage.setItem("memoratum_key", key);
    else sessionStorage.removeItem("memoratum_key");
  },
  profile: (tag: string) =>
    request<import("./types").Profile>(`/v4/profile?containerTag=${encodeURIComponent(tag)}`),
  search: (body: Record<string, unknown>) =>
    request<{ results: import("./types").SearchHit[]; total: number }>("/v4/search", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  facts: (tag: string, limit = 10000) =>
    request<{ facts: import("./types").GraphFact[]; total: number }>(
      `/v4/facts?containerTag=${encodeURIComponent(tag)}&limit=${limit}`
    ),
  audit: (limit = 50, offset = 0) =>
    request<{ events: import("./types").AuditEvent[]; total: number }>(
      `/v4/audit?limit=${limit}&offset=${offset}`
    ),
  usage: (limit = 50, offset = 0) =>
    request<{ usage: import("./types").UsageCounter[]; total: number }>(
      `/v4/usage?limit=${limit}&offset=${offset}`
    ),
};
