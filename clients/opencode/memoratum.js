// Memoratum OpenCode plugin — recall-on-prompt + store-on-write against a
// Memoratum server. Config via env: MEMORATUM_URL (default
// http://localhost:6767), MEMORATUM_API_KEY (optional), MEMORATUM_TAG
// (default "opencode").
// Copyright (C) 2026 Memoratum contributors — SPDX-License-Identifier: MIT
// (see LICENSE-MIT; client code exception to repo AGPL).
// ponytail: mirrors the supermemory port's shape; talks to Memoratum's
// compat API instead. Upgrade path: none needed — same repo.
const URL = (process.env.MEMORATUM_URL || "http://localhost:6767").replace(/\/+$/, "");
const KEY = process.env.MEMORATUM_API_KEY || "";
const TAG = process.env.MEMORATUM_TAG || "opencode";
const TIMEOUT_MS = 15000;
const STORE_TOOLS = new Set(["write", "edit"]);

async function api(path, body) {
  const res = await fetch(`${URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(KEY ? { Authorization: `Bearer ${KEY}` } : {}) },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  if (!res.ok) throw new Error(`memoratum ${path} -> ${res.status}`);
  return res.json();
}

export default {
  id: "memoratum",
  async setup(ctx) {
    await ctx.session.hook("prompt", async (event) => {
      try {
        const text = event?.prompt?.text;
        if (typeof text !== "string" || !text.trim()) return;
        const data = await api("/v4/search", { q: text.slice(0, 500), containerTag: TAG, limit: 3, searchMode: "hybrid" });
        const top = (data.results || []).slice(0, 3);
        if (!top.length) return;
        const lines = top.map((m) => `- ${String(m.memory || m.chunk || "").replace(/\s+/g, " ").slice(0, 200)}`);
        event.prompt.text = `${text}\n\n[memoratum]\nRelevant memories:\n${lines.join("\n")}`;
      } catch (err) {
        console.warn(`[memoratum] recall failed: ${err instanceof Error ? err.message : String(err)}`);
      }
    });
    await ctx.tool.hook("execute.after", (event) => {
      try {
        if (event?.status !== "completed") return;
        if (!STORE_TOOLS.has(String(event.tool || "").toLowerCase())) return;
        const input = event.input || {};
        const snippet = String(input.content || input.text || input.command || JSON.stringify(input)).slice(0, 500);
        if (!snippet.trim()) return;
        const file = input.file || input.filePath || input.path || "unknown file";
        void api("/v3/documents", { content: `Edited ${file}: ${snippet}`, containerTag: TAG }).catch((err) =>
          console.warn(`[memoratum] store failed: ${err instanceof Error ? err.message : String(err)}`)
        );
      } catch {
        // never throw from hooks
      }
    });
  },
};
