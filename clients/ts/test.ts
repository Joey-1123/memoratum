// Mem0 TypeScript SDK contract (RED). Run: node --experimental-strip-types test.ts
import { strict as assert } from "node:assert";
import { Client } from "./index.ts";
import { Mem0CompatClient } from "./mem0-contract.ts";

const calls: Array<[string, unknown]> = [];
// @ts-ignore stub fetch
globalThis.fetch = async (url: string, opts: { body?: string; headers?: Record<string, string> }) => {
  const body = JSON.parse(opts.body ?? "{}");
  calls.push([url, body]);
  const payload = url.endsWith("/v3/documents")
    ? { id: "d1", status: "done" }
    : url.endsWith("/v3/memories/add/")
      ? { event_id: "e1", status: "PENDING" }
      : { results: [{ memory: "user loves Paris", similarity: 0.9 }], timing: 1, total: 1 };
  return { ok: true, status: 200, json: async () => payload };
};

const c = new Client({ baseURL: "http://x", apiKey: "k" });
const doc = await c.add("hello", { containerTag: "u1" });
assert.equal(doc.id, "d1");
assert.equal(calls[0][1].containerTag, "u1");
const res = await c.search("paris", { containerTag: "u1", searchMode: "memories" });
assert.equal(res.total, 1);
assert.equal(res.results[0].memory, "user loves Paris");

const mem0 = new Mem0CompatClient("http://x", "k", globalThis.fetch);
const added = await mem0.add({
  messages: [{ role: "user", content: "hello" }],
  user_id: "u1",
});
assert.ok(added.status === "PENDING" || Array.isArray(added.results));
assert.equal(calls[2][0], "http://x/v3/memories/add/");
assert.equal((calls[2][1] as { user_id: string }).user_id, "u1");
const searched = await mem0.search({ query: "hello", filters: { user_id: "u1" } });
assert.equal(searched.results.length, 1);
assert.equal(calls[3][0], "http://x/v3/memories/search/");
console.log("TS_SDK_ASSERTS_OK");
